"""Per-project MONEY budgets (CV-2, offline).

Hermetic: throwaway LOC_TMS_DATA_DIR, provider keys cleared (forces MockProvider),
ASCII-only prints, FastAPI TestClient.

Covers:
  * CRUD: upsert (create + in-place update, one row per project), get, delete,
    validate_usd_limit bound (huge / NaN -> BudgetLimitError).
  * spend accounting (project_spend_this_month): AI job cost (translate_document,
    priced via reports._job_cost) + workflow StepRun.cost_usd; SETTLED vendor human
    ('actual' grade) enforced, COMMITTED (estimate/quote on open legs) shown but not
    enforced; last-month spend excluded; ANOTHER project's spend excluded (tenant
    scoping -- the reports-adjacent money seam must not leak across projects).
  * project_check(): unlimited (no row / limit <= 0); under cap allowed; at/over cap
    blocked with EXHAUSTED_MESSAGE; org A's cap never gates org B.
  * budget_state(): render fields (figure/fill/tick/over) for unlimited/under/over.
  * evaluate_thresholds(): crossing 80% then 100% each alerts ONCE (audit row +
    idempotent state); a fresh period re-arms.
  * API: an over-budget project 429s POST translate (zero jobs enqueued); the
    /api/projects/{id}/budget routes are admin-only; the project-show budget block is
    manager+ only.
"""

import os
import sys
from datetime import datetime
from io import BytesIO

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(_ROOT)
sys.path.insert(0, _ROOT)
from tests._bootstrap import init_env

init_env("loctms_projbudgets_")

from docx import Document as Docx
from fastapi.testclient import TestClient
from sqlmodel import Session, select

from app.db import engine, init_db
from app.main import app
from app.models import (
    Document,
    Job,
    JobStatus,
    Project,
    ProjectBudget,
    Role,
    StepRun,
    Vendor,
    VendorDispatch,
    VendorDispatchStatus,
    Workflow,
    WorkflowRun,
    WorkflowStep,
)

# Shared FK parents (a Workflow + step + vendor) so seeded runs/legs satisfy the FK
# constraints the test DB enforces. Populated once by _setup_refs() in main().
_REF = {"workflow_id": None, "step_id": None, "vendor_id": None}


def _setup_refs():
    with Session(engine) as s:
        wf = Workflow(name="wf")
        s.add(wf)
        s.commit()
        s.refresh(wf)
        st = WorkflowStep(workflow_id=wf.id)
        s.add(st)
        s.commit()
        s.refresh(st)
        v = Vendor(name="vendor")
        s.add(v)
        s.commit()
        s.refresh(v)
        _REF.update(workflow_id=wf.id, step_id=st.id, vendor_id=v.id)


from app.services import apikeys, project_budgets, security, settings
from app.services.providers import Provider, register_provider


class LiveStubProvider(Provider):
    name = "claude"

    def complete(self, prompt, system=None, task=None, payload=None, **kw):
        if task == "translate":
            payload = payload or {}
            return f"[{payload.get('target', 'xx')}] {payload.get('text', '')}"
        return (prompt or "").strip()


def dx(paras):
    d = Docx()
    for p in paras:
        d.add_paragraph(p)
    b = BytesIO()
    d.save(b)
    return b.getvalue()


# --- seeding helpers ---------------------------------------------------------
def _mk_project(name, org_id=None):
    with Session(engine) as s:
        p = Project(name=name, org_id=org_id)
        s.add(p)
        s.commit()
        s.refresh(p)
        return p.id


def _mk_doc(project_id):
    with Session(engine) as s:
        d = Document(project_id=project_id, filename="d.docx", fmt="docx")
        s.add(d)
        s.commit()
        s.refresh(d)
        return d.id


def _seed_job(
    document_id, tin, tout, created_at=None, status=JobStatus.COMPLETED, provider="claude"
):
    """A priced translate_document Job (reports._job_cost reads provider_used+tokens)."""
    with Session(engine) as s:
        j = Job(
            kind="translate_document",
            payload={"document_id": document_id, "locale": "fr-FR"},
            status=status,
            result={"provider_used": {provider: 1}, "tokens_in": tin, "tokens_out": tout},
            created_by="u@x.co",
            created_at=created_at or datetime.utcnow(),
        )
        s.add(j)
        s.commit()
        s.refresh(j)
        return j.id


def _seed_workflow_step(document_id, cost_usd, created_at=None):
    """A WorkflowRun + one StepRun stamping cost_usd (workflow AI spend)."""
    when = created_at or datetime.utcnow()
    with Session(engine) as s:
        run = WorkflowRun(
            workflow_id=_REF["workflow_id"],
            document_id=document_id,
            finished_at=when,
            started_at=when,
        )
        s.add(run)
        s.commit()
        s.refresh(run)
        sr = StepRun(
            workflow_run_id=run.id,
            step_id=_REF["step_id"],
            cost_usd=cost_usd,
            tokens_in=10,
            tokens_out=10,
        )
        s.add(sr)
        s.commit()
        return run.id


def _seed_leg(
    document_id,
    status,
    *,
    actual=None,
    estimate=0.0,
    dispatched_at=None,
    returned_at=None,
    workflow_run_id=None,
):
    when = dispatched_at or datetime.utcnow()
    with Session(engine) as s:
        if workflow_run_id is None:
            run = WorkflowRun(
                workflow_id=_REF["workflow_id"],
                document_id=document_id,
                finished_at=when,
                started_at=when,
            )
            s.add(run)
            s.commit()
            s.refresh(run)
            workflow_run_id = run.id
        leg = VendorDispatch(
            vendor_id=_REF["vendor_id"],
            workflow_run_id=workflow_run_id,
            document_id=document_id,
            status=status,
            actual_cost_usd=actual,
            estimate_cost_usd=estimate,
            dispatched_at=when,
            returned_at=returned_at,
        )
        s.add(leg)
        s.commit()
        s.refresh(leg)
        return leg.id


def _last_month():
    now = datetime.utcnow()
    return (
        datetime(now.year - 1, 12, 15) if now.month == 1 else datetime(now.year, now.month - 1, 15)
    )


# --- tests -------------------------------------------------------------------
def test_crud():
    pid = _mk_project("crud-proj")
    with Session(engine) as s:
        row, created = project_budgets.upsert_project_budget(s, pid, 100.0, actor="a@x.co")
        assert created and row.monthly_usd_limit == 100.0, row
        # in-place update, same row (one per project)
        row2, created2 = project_budgets.upsert_project_budget(s, pid, 250.0, actor="a@x.co")
        assert (not created2) and row2.id == row.id and row2.monthly_usd_limit == 250.0
        assert project_budgets.get_project_budget(s, pid).id == row.id
        n = len(s.exec(select(ProjectBudget).where(ProjectBudget.project_id == pid)).all())
        assert n == 1, f"one row per project, got {n}"
        # bound + NaN
        for bad in (10**15, float("nan"), float("inf")):
            try:
                project_budgets.validate_usd_limit(bad)
                assert False, f"expected BudgetLimitError for {bad}"
            except project_budgets.BudgetLimitError:
                pass
        assert project_budgets.delete_project_budget(s, row.id, actor="a@x.co")
        assert project_budgets.get_project_budget(s, pid) is None
        assert not project_budgets.delete_project_budget(s, row.id)  # gone -> no-op
    print("  crud: upsert (create+update, one row/project), get, delete, bound+NaN reject")


def test_spend_and_scoping():
    pa = _mk_project("spend-A")
    pb = _mk_project("spend-B")
    da, db = _mk_doc(pa), _mk_doc(pb)
    # A: an AI job + a workflow step this month; a job LAST month (excluded).
    _seed_job(da, 1000, 1000)
    _seed_workflow_step(da, 3.50)
    _seed_job(da, 500, 500, created_at=_last_month())
    # B: its own AI job (must NOT leak into A).
    _seed_job(db, 2000, 2000)
    with Session(engine) as s:
        sa = project_budgets.project_spend_this_month(s, pa)
        sb = project_budgets.project_spend_this_month(s, pb)
    assert sa["ai_usd"] > 3.50, sa  # job cost + $3.50 step, last-month excluded
    job_only = sa["ai_usd"] - 3.50
    assert job_only > 0, sa
    assert sa["human_settled_usd"] == 0.0 and sa["committed_usd"] == 0.0, sa
    assert sa["enforced_usd"] == round(sa["ai_usd"], 6), sa
    # tenant scoping: B's spend is its own, independent of A.
    assert sb["ai_usd"] > 0 and abs(sb["ai_usd"] - sa["ai_usd"]) > 0.0001, (sa, sb)
    print("  spend: AI job+StepRun this month; last-month excluded; cross-project scoped")


def test_human_spend():
    p = _mk_project("human-proj")
    d = _mk_doc(p)
    _seed_leg(
        d, VendorDispatchStatus.RETURNED, actual=50.0, estimate=40.0, returned_at=datetime.utcnow()
    )  # settled -> enforced
    _seed_leg(d, VendorDispatchStatus.DISPATCHED, estimate=30.0)  # committed -> display
    with Session(engine) as s:
        sp = project_budgets.project_spend_this_month(s, p)
    assert sp["human_settled_usd"] == 50.0, sp
    assert sp["committed_usd"] == 30.0, sp
    assert sp["enforced_usd"] == 50.0, sp  # committed NOT enforced
    assert sp["total_display_usd"] == 80.0, sp
    print("  human: settled actual enforced ($50); open estimate committed ($30, display-only)")


def test_check():
    p = _mk_project("check-proj")
    d = _mk_doc(p)
    # No row -> unlimited.
    ok, rem, msg = project_budgets.project_check(p)
    assert ok and rem == -1.0 and msg == "", (ok, rem, msg)
    # A settled $50 human leg + a $100 cap -> under, remaining ~50.
    _seed_leg(d, VendorDispatchStatus.RETURNED, actual=50.0, returned_at=datetime.utcnow())
    with Session(engine) as s:
        project_budgets.upsert_project_budget(s, p, 100.0)
    ok, rem, msg = project_budgets.project_check(p)
    assert ok and abs(rem - 50.0) < 0.001 and msg == "", (ok, rem, msg)
    # Push over: another settled $60 -> spend 110 >= 100 -> blocked.
    _seed_leg(d, VendorDispatchStatus.RETURNED, actual=60.0, returned_at=datetime.utcnow())
    ok, rem, msg = project_budgets.project_check(p)
    assert (not ok) and rem == 0.0 and msg == project_budgets.EXHAUSTED_MESSAGE, (ok, rem, msg)
    # limit <= 0 -> unlimited even with spend.
    with Session(engine) as s:
        project_budgets.upsert_project_budget(s, p, 0.0)
    ok, rem, msg = project_budgets.project_check(p)
    assert ok and rem == -1.0, (ok, rem, msg)
    print("  check: unset/0 unlimited; under->allowed+remaining; at/over->blocked")


def test_org_from_project():
    """H2: the budget row's org is a property of the PROJECT, and enforcement keys on
    the unique project_id (never the caller's org). A row whose org_id MISMATCHES the
    project — the exact gate probe — must STILL enforce; upsert stamps the project's org."""
    from app.models import Organization

    with Session(engine) as s:
        org = Organization(name="Org2")
        s.add(org)
        s.commit()
        s.refresh(org)
        org2 = org.id
    p = _mk_project("org2-proj", org_id=org2)
    d = _mk_doc(p)
    with Session(engine) as s:
        row, _ = project_budgets.upsert_project_budget(s, p, 100.0)
        # upsert stamps the PROJECT's org, not the caller/None.
        assert row.org_id == org2, (row.org_id, org2)
        # Simulate a legacy/mismatched row (org_id blanked) — enforcement must not care.
        row.org_id = None
        s.add(row)
        s.commit()
    _seed_leg(d, VendorDispatchStatus.RETURNED, actual=150.0, returned_at=datetime.utcnow())
    ok, _, msg = project_budgets.project_check(p)
    assert (not ok) and msg == project_budgets.EXHAUSTED_MESSAGE, (
        "a mismatched-org row must STILL enforce (H2)"
    )
    # get_project_budget finds it regardless of the stored org.
    with Session(engine) as s:
        assert project_budgets.get_project_budget(s, p).project_id == p
    print(
        "  org: keyed on project_id; mismatched-org row still enforces; upsert stamps project org"
    )


def test_budget_state():
    p = _mk_project("state-proj")
    d = _mk_doc(p)
    with Session(engine) as s:
        st = project_budgets.budget_state(s, p)
        assert (not st["has_budget"]) and st["unlimited"], st
        project_budgets.upsert_project_budget(s, p, 100.0)
    _seed_leg(d, VendorDispatchStatus.RETURNED, actual=40.0, returned_at=datetime.utcnow())
    _seed_leg(d, VendorDispatchStatus.DISPATCHED, estimate=20.0)
    with Session(engine) as s:
        st = project_budgets.budget_state(s, p)
    assert st["has_budget"] and not st["unlimited"], st
    assert st["enforced_usd"] == 40.0 and st["committed_usd"] == 20.0, st
    assert abs(st["fill_pct"] - 40.0) < 0.01 and not st["over"], st
    assert st["committed_tick_pct"] is not None and abs(st["committed_tick_pct"] - 60.0) < 0.01, st
    assert "$40.00" in st["figure"] and "$100.00" in st["figure"], st
    print("  state: figure/fill/committed-tick render; unlimited when no row")


def test_spend_survives_out_of_range_job_id():
    """Regression (gate battery): a cost-kind Job carrying an OUT-OF-RANGE document_id
    (a refusal-probe job, as the key-surface tests create) must NOT crash budget_state /
    project_spend — attribution is set-membership via ids.coerce_id, never a raw
    session.get that OverflowErrors on a huge id and 422s every project page."""
    p = _mk_project("oor-proj")
    _mk_doc(p)
    huge = 10**25  # far beyond SQLite's signed-64-bit range
    with Session(engine) as s:
        s.add(
            Job(
                kind="translate_document",
                payload={"document_id": huge, "locale": "fr-FR"},
                status=JobStatus.COMPLETED,
                result={"provider_used": {"claude": 1}, "tokens_in": 10, "tokens_out": 10},
                created_by="x@x.co",
                created_at=datetime.utcnow(),
            )
        )
        s.commit()
        # Must return cleanly (the huge-id job is simply not attributed to this project).
        st = project_budgets.budget_state(s, p)
        assert st["ai_usd"] == 0.0, st
    print("  regression: out-of-range job document_id does not crash budget_state")


def test_thresholds_concurrent_single_fire():
    """N1: N threads crossing 100% at once must write EXACTLY ONE budget.threshold audit
    row + fire the band once (atomic compare-and-swap, not read-then-write)."""
    import threading

    from app.models import AuditLog

    settings.set_many({"audit_enabled": "1"})
    p = _mk_project("concurrent-proj")
    d = _mk_doc(p)
    with Session(engine) as s:
        project_budgets.upsert_project_budget(s, p, 100.0)
    _seed_leg(d, VendorDispatchStatus.RETURNED, actual=150.0, returned_at=datetime.utcnow())
    N = 8
    barrier = threading.Barrier(N)
    results = [None] * N

    def _worker(i):
        barrier.wait()  # all fire the crossing simultaneously
        results[i] = project_budgets.evaluate_thresholds(p, actor="sys")

    threads = [threading.Thread(target=_worker, args=(i,)) for i in range(N)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    fired = [r for r in results if r is not None]
    assert fired == [100], f"exactly one thread must fire band=100, got {fired}"
    with Session(engine) as s:
        rows = [
            a
            for a in s.exec(select(AuditLog).where(AuditLog.action == "budget.threshold")).all()
            if f"project={p} " in (a.detail or "")
        ]
    assert len(rows) == 1, f"exactly one audit row under {N}-way race, got {len(rows)}"
    print("  n1: %d concurrent 100%% crossings -> exactly 1 audit row + 1 fire" % N)  # noqa: UP031


def test_first_hit_attribution():
    """N4: a job carrying TWO conflicting keys attributes by FIRST-HIT (payload.project_id
    wins over document_id), never double-counted — matching reports._job_project_id."""
    pa = _mk_project("fh-A")
    pb = _mk_project("fh-B")
    da = _mk_doc(pa)
    # A job whose payload.project_id points at B but whose document_id is A's doc.
    # First-hit: project_id wins -> attributes to B, NOT A (no double count on A).
    with Session(engine) as s:
        s.add(
            Job(
                kind="translate_document",
                payload={"project_id": pb, "document_id": da, "locale": "fr-FR"},
                status=JobStatus.COMPLETED,
                result={"provider_used": {"claude": 1}, "tokens_in": 1000, "tokens_out": 1000},
                created_by="x@x.co",
                created_at=datetime.utcnow(),
            )
        )
        s.commit()
        sa = project_budgets.project_spend_this_month(s, pa)
        sb = project_budgets.project_spend_this_month(s, pb)
    assert sa["ai_usd"] == 0.0, ("first-hit: project_id wins, A must not count", sa)
    assert sb["ai_usd"] > 0.0, ("the job attributes to B by project_id", sb)
    print("  n4: conflicting-key job attributes first-hit (project_id), never double-counted")


def test_job_project_id_safe_bounds():
    """N5: job_project_id_safe never binds an out-of-range id to the DB (the hook stays
    live on hostile ids instead of silently dying on OverflowError)."""
    p = _mk_project("n5-proj")
    d = _mk_doc(p)
    with Session(engine) as s:
        # in-range document_id resolves; huge one returns None (no OverflowError).
        j_ok = Job(
            kind="translate_document", payload={"document_id": d}, status=JobStatus.COMPLETED
        )
        j_huge = Job(
            kind="translate_document", payload={"document_id": 10**25}, status=JobStatus.COMPLETED
        )
        assert project_budgets.job_project_id_safe(s, j_ok) == p
        assert project_budgets.job_project_id_safe(s, j_huge) is None
    print("  n5: job_project_id_safe is bound-safe (huge id -> None, no OverflowError)")


def test_seam_rerun_batch():
    """N2 pin — batch_runs.rerun_batch (the shared re-run / retry-failed launcher):
    a re-run over budget raises BatchBudgetExhausted BEFORE any fresh member run.
    Revert-probe: delete the project_check at the top of rerun_batch and this goes green."""
    from app.models import WorkflowBatch, _utcnow
    from app.services import batch_runs

    _p, d = _over_budget_project("seam-rerun")
    with Session(engine) as s:
        batch = WorkflowBatch(
            workflow_id=_REF["workflow_id"],
            document_id=d,
            requested_locales=["fr-FR"],
            finished_at=_utcnow(),
        )
        s.add(batch)
        s.commit()
        s.refresh(batch)
        try:
            batch_runs.rerun_batch(s, batch=batch, locales=["fr-FR"], synchronous=False)
            assert False, "rerun_batch must refuse over budget"
        except batch_runs.BatchBudgetExhausted as exc:
            assert str(exc) == project_budgets.EXHAUSTED_MESSAGE, exc
    print("  seam: rerun_batch / retry-failed refuses over budget (N2, BatchBudgetExhausted)")


def test_seam_keys_launch_translation():
    """N3 pin — the key-project fan-out seam (keys.launch_translation, used by /api + CLI):
    over budget raises KeyBudgetError BEFORE the launch loop (no half-launch). Revert-probe:
    delete the pre-loop project_check in launch_translation and this goes green."""
    from app.models import Project, TargetLocale
    from app.services import keys as keys_svc

    p, _d = _over_budget_project("seam-keys")
    with Session(engine) as s:
        s.add(TargetLocale(project_id=p, locale="fr-FR"))
        s.commit()
        project = s.get(Project, p)
        try:
            keys_svc.launch_translation(s, project, _REF["workflow_id"], "fr-FR", actor="test@x.co")
            assert False, "launch_translation must refuse over budget"
        except keys_svc.KeyBudgetError as exc:
            assert str(exc) == project_budgets.EXHAUSTED_MESSAGE, exc
    print("  seam: keys.launch_translation fan-out refuses over budget (N3, KeyBudgetError)")


def test_seam_web_keys_translate():
    """N3 pin (second surface) — the WEB key-translate route (its own inline loop) refuses
    over budget with the error banner BEFORE launching. Revert-probe: delete the pre-loop
    project_check in the web keys_translate route and the redirect carries no budget err."""
    from app.models import Project, TargetLocale

    settings.set_many({"security_auth_required": "0", "key_projects_enabled": "1"})
    try:
        p, _d = _over_budget_project("seam-webkeys")
        with Session(engine) as s:
            proj = s.get(Project, p)
            proj.project_type = "key_based"
            s.add(proj)
            s.add(TargetLocale(project_id=p, locale="fr-FR"))
            s.commit()
        c = TestClient(app, follow_redirects=False)
        r = c.post(
            f"/projects/{p}/keys/translate",
            data={"workflow_id": _REF["workflow_id"], "locale": "fr-FR"},
        )
        assert r.status_code == 303, (r.status_code, r.text[:200])
        loc = r.headers.get("location", "")
        assert "err=" in loc and "budget" in loc.lower(), (
            f"the over-budget key-translate must redirect with a budget error, got {loc!r}"
        )
        print("  seam: WEB keys translate refuses over budget (N3, error banner, no launch)")
    finally:
        settings.set_many({"security_auth_required": "1"})  # restore for later tests


def test_thresholds():
    from app.models import AuditLog

    p = _mk_project("thresh-proj")
    d = _mk_doc(p)
    settings.set_many({"audit_enabled": "1"})
    with Session(engine) as s:
        project_budgets.upsert_project_budget(s, p, 100.0)
    # Cross 80%: a settled $85 leg. evaluate_thresholds takes NO session (own Session).
    _seed_leg(d, VendorDispatchStatus.RETURNED, actual=85.0, returned_at=datetime.utcnow())
    assert project_budgets.evaluate_thresholds(p, actor="sys") == 80
    assert project_budgets.evaluate_thresholds(p, actor="sys") is None  # idempotent
    # Cross 100%: another $20 -> 105.
    _seed_leg(d, VendorDispatchStatus.RETURNED, actual=20.0, returned_at=datetime.utcnow())
    assert project_budgets.evaluate_thresholds(p, actor="sys") == 100
    assert project_budgets.evaluate_thresholds(p, actor="sys") is None
    with Session(engine) as s:
        n = len(s.exec(select(AuditLog).where(AuditLog.action == "budget.threshold")).all())
    assert n == 2, f"exactly 2 crossings (80,100) audited, got {n}"

    # M4: raising the cap RE-ARMS the alerts (spend 105 now < 80% of a $1000 cap ->
    # notified state reset; re-crossing must fire again).
    with Session(engine) as s:
        project_budgets.upsert_project_budget(s, p, 1000.0)
        row = project_budgets.get_project_budget(s, p)
        assert row.notified_pct == 0 and row.notified_period is None, "cap change must re-arm"
    _seed_leg(
        d, VendorDispatchStatus.RETURNED, actual=900.0, returned_at=datetime.utcnow()
    )  # 1005/1000
    assert project_budgets.evaluate_thresholds(p, actor="sys") == 100
    print("  thresholds: 80 then 100 fire once each (2 audit rows); raising cap re-arms (M4)")


def test_evaluate_own_session():
    """M3: evaluate_thresholds persists crossing state in its OWN session, so a caller
    whose transaction later ROLLS BACK does not lose the alert (the spend really landed)."""
    p = _mk_project("m3-proj")
    d = _mk_doc(p)
    with Session(engine) as s:
        project_budgets.upsert_project_budget(s, p, 100.0)
    _seed_leg(d, VendorDispatchStatus.RETURNED, actual=90.0, returned_at=datetime.utcnow())
    # Call inside a caller session that we then ROLL BACK with a pending dirty row.
    with Session(engine) as caller:
        caller.add(_mk_stub_pending())  # a pending, uncommitted row in the caller session
        band = project_budgets.evaluate_thresholds(p, actor="sys")
        caller.rollback()  # caller's own work is discarded
    assert band == 80, band
    with Session(engine) as s:
        row = project_budgets.get_project_budget(s, p)
    assert row.notified_pct == 80, "crossing state must survive the caller's rollback (M3)"
    print("  m3: threshold state persists in its own session (survives caller rollback)")


def _mk_stub_pending():
    from app.models import Project

    return Project(name="pending-never-committed")


def _over_budget_project(name):
    """A project + document with a $1 cap already blown by a settled $2 vendor leg.
    Returns (project_id, doc_id)."""
    p = _mk_project(name)
    d = _mk_doc(p)
    with Session(engine) as s:
        project_budgets.upsert_project_budget(s, p, 1.0)
    _seed_leg(d, VendorDispatchStatus.RETURNED, actual=2.0, returned_at=datetime.utcnow())
    return p, d


def test_seam_orchestrator_start_run():
    """H3 pin — the orchestrator.start_run CALL SITE (web/api single-run launcher):
    over budget, start_run itself raises ProjectBudgetExhausted BEFORE any run row.
    Revert-probe: delete the _refuse_project_budget call in start_run -> a run row is
    created and this goes GREEN (no raise) i.e. RED here."""
    from app.services import orchestrator

    _p, d = _over_budget_project("seam-start-run")
    try:
        orchestrator.start_run(Session(engine), _REF["workflow_id"], d, "fr-FR")
        assert False, "start_run must refuse over budget"
    except orchestrator.ProjectBudgetExhausted as exc:
        assert str(exc) == project_budgets.EXHAUSTED_MESSAGE, exc
    print("  seam: orchestrator.start_run refuses over budget (call site)")


def test_seam_orchestrator_run_workflow():
    """H3 pin — the orchestrator.run_workflow CALL SITE (CLI synchronous launcher)."""
    from app.services import orchestrator

    _p, d = _over_budget_project("seam-run-workflow")
    with Session(engine) as s:
        try:
            orchestrator.run_workflow(s, _REF["workflow_id"], d, "fr-FR")
            assert False, "run_workflow must refuse over budget"
        except orchestrator.ProjectBudgetExhausted as exc:
            assert str(exc) == project_budgets.EXHAUSTED_MESSAGE, exc
    print("  seam: orchestrator.run_workflow refuses over budget (call site)")


def test_seam_batch_validate():
    """H3 pin — batch_runs._validate (all batch surfaces): over budget raises
    BatchBudgetExhausted (-> 429 on the HTTP surfaces, M2)."""
    from app.services import batch_runs

    _p, d = _over_budget_project("seam-batch")
    with Session(engine) as s:
        try:
            batch_runs.start_batch(
                s, workflow_id=_REF["workflow_id"], doc_id=d, locales=["fr-FR"], synchronous=False
            )
            assert False, "start_batch must refuse over budget"
        except batch_runs.BatchBudgetExhausted as exc:
            assert str(exc) == project_budgets.EXHAUSTED_MESSAGE, exc
    print("  seam: batch_runs._validate refuses over budget (BatchBudgetExhausted -> 429)")


def test_seam_vendor_dispatch():
    """H3 pin — vendor_dispatch.dispatch_external_step: a NEW dispatch over budget
    raises VendorError before resolve_vendor / any egress."""
    from app.models import WorkflowRun, WorkflowStep
    from app.services import vendor_dispatch, vendors

    _p, d = _over_budget_project("seam-vendor")
    with Session(engine) as s:
        run = WorkflowRun(workflow_id=_REF["workflow_id"], document_id=d)
        s.add(run)
        s.commit()
        s.refresh(run)
        step = s.get(WorkflowStep, _REF["step_id"])
        try:
            vendor_dispatch.dispatch_external_step(
                s,
                run=run,
                step=step,
                document_id=d,
                segments=[],
                source_locale="en-US",
                target_locale="fr-FR",
                project=None,
            )
            assert False, "dispatch_external_step must refuse over budget"
        except vendors.VendorError as exc:
            assert project_budgets.EXHAUSTED_MESSAGE in str(exc), exc
    print("  seam: vendor_dispatch.dispatch_external_step refuses over budget (VendorError)")


def test_seam_web_document_translate():
    """H3 pin — the WEB POST /documents/{id}/translate seam (documents.py) -> 429 page.
    Revert-probe: delete the project_check in documents.translate_document -> this POST
    303-redirects to a job instead of 429, i.e. RED here."""
    from tests._csrf import csrf_for

    _p, d = _over_budget_project("seam-web-doc")
    admin = security.create_user("webadmin@x.co", "Web Admin", Role.ADMIN, "web-admin-pw-123")
    cl = TestClient(app, follow_redirects=False)
    csrf_for(cl)
    r = cl.post(
        "/auth/login", data={"email": admin.email, "password": "web-admin-pw-123", "next": "/"}
    )
    assert r.status_code == 303, ("login", r.status_code, r.text)
    csrf_for(cl)
    r = cl.post(f"/documents/{d}/translate", data={"locale": "fr-FR"})
    # Revert-probe: neuter the project_check here and this becomes a 303 redirect to a
    # job (the normal enqueue path), so the 429 is the discriminating signal.
    assert r.status_code == 429, (r.status_code, r.text[:300])
    print("  seam: WEB document translate over budget -> 429")


def test_seam_cli_doc_translate():
    """H3 pin — the in-process CLI `doc translate` seam (_doc_translate) -> CliError."""
    from types import SimpleNamespace

    from app.cli import cmd_doc
    from app.cli._common import CliError

    _p, d = _over_budget_project("seam-cli-doc")

    class _Emit:
        as_json = False

        def say(self, *a, **k):
            pass

        def result(self, *a, **k):
            pass

        def json(self, *a, **k):
            pass

        def table(self, *a, **k):
            pass

    args = SimpleNamespace(
        doc_id=d, locale="fr-FR", actor="cli", remote=None, json=False, _op_org_id=None, org=None
    )
    try:
        cmd_doc._doc_translate(args, _Emit())
        assert False, "CLI doc translate must refuse over budget"
    except CliError as exc:
        assert project_budgets.EXHAUSTED_MESSAGE in str(exc), exc
    print("  seam: CLI doc translate over budget -> CliError")


def test_h1_block_fires_100():
    """H1 regression: the BLOCKED (429) submission IS the 100% crossing — it must write a
    band=100 budget.threshold audit row EXACTLY ONCE, not be unreachable dead code, and a
    second blocked submission must NOT re-fire (idempotent)."""
    from app.models import AuditLog

    settings.set_many({"audit_enabled": "1"})
    c = TestClient(app)
    admin = security.get_by_email("webadmin@x.co") or security.create_user(
        "h1admin@x.co", "H1", Role.ADMIN, "pw-fixture-4417"
    )
    _k, ak = apikeys.create_key(admin.id, "h1 docs", scopes="api docs")
    HA = {"X-Loc-Token": ak}
    register_provider("claude", LiveStubProvider())
    try:
        r = c.post(
            "/api/documents",
            headers=HA,
            files={
                "file": (
                    "h1.docx",
                    dx(["Line."]),
                    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                )
            },
        )
        assert r.status_code == 200, r.text
        doc_id = r.json()["document_id"]
        with Session(engine) as s:
            pid = s.get(Document, doc_id).project_id
            project_budgets.upsert_project_budget(s, pid, 1.0)
        _seed_leg(
            _mk_doc(pid), VendorDispatchStatus.RETURNED, actual=5.0, returned_at=datetime.utcnow()
        )

        def _band100_rows():
            with Session(engine) as s:
                return [
                    a
                    for a in s.exec(
                        select(AuditLog).where(AuditLog.action == "budget.threshold")
                    ).all()
                    if f"project={pid} " in (a.detail or "") and "band=100%" in (a.detail or "")
                ]

        assert _band100_rows() == [] or len(_band100_rows()) == 0
        r = c.post(f"/api/documents/{doc_id}/translate", headers=HA, json={"targets": ["fr-FR"]})
        assert r.status_code == 429, r.text
        assert len(_band100_rows()) == 1, "the blocked submission must fire band=100 once"
        # A SECOND blocked submission does not re-fire.
        r = c.post(f"/api/documents/{doc_id}/translate", headers=HA, json={"targets": ["fr-FR"]})
        assert r.status_code == 429, r.text
        assert len(_band100_rows()) == 1, "band=100 must not re-fire (idempotent)"
        print("  h1: the blocked 429 submission fires band=100 audit exactly once (reachable)")
    finally:
        from app.services import providers as _prov

        _prov._OVERRIDES.pop("claude", None)


def test_api_budget_foreign_project_404_before_validation():
    """Tenant-ring: POST /api/projects/{pid}/budget with an INVALID body to a project the
    caller cannot see must 404 (visibility ring FIRST), never leak reachability via a 400
    validation error. Revert-probe: move the float validation before _project_in_org and
    this returns 400 -> RED."""
    c = TestClient(app)
    admin = security.get_by_email("webadmin@x.co") or security.create_user(
        "ringadmin@x.co", "Ring", Role.ADMIN, "pw-fixture-4417"
    )
    _k, ak = apikeys.create_key(admin.id, "ring docs", scopes="api docs admin")
    HA = {"X-Loc-Token": ak}
    # A non-existent-by-id project is the foreign-by-id case: _project_in_org -> None.
    ghost = 999999
    r = c.post(
        f"/api/projects/{ghost}/budget", headers=HA, json={"monthly_usd_limit": "not-a-number"}
    )
    assert r.status_code == 404, (r.status_code, r.text)
    # GET + delete are ring-first too (no body): 404 for the same ghost.
    assert c.get(f"/api/projects/{ghost}/budget", headers=HA).status_code == 404
    assert c.post(f"/api/projects/{ghost}/budget/delete", headers=HA).status_code == 404
    print("  ring: foreign/ghost by-id budget verb -> 404 before any body validation")


def test_api_enforcement_and_rbac():
    c = TestClient(app)
    # Admin key (manage budgets) + a plain translator key.
    admin = security.get_by_email("admin@example.com") or security.get_by_email("admin@x.co")
    if admin is None:
        admin = security.create_user("admin2@x.co", "Admin2", Role.ADMIN, "pw-fixture-4417")
    _k, admin_docs = apikeys.create_key(admin.id, "admin docs", scopes="api docs admin")
    HA = {"X-Loc-Token": admin_docs}
    tr = security.create_user("tr@x.co", "Tr", Role.TRANSLATOR, "pw-fixture-4417")
    _k, tr_docs = apikeys.create_key(tr.id, "tr docs", scopes="api docs")
    HT = {"X-Loc-Token": tr_docs}

    register_provider("claude", LiveStubProvider())
    try:
        # Upload a doc (admin) -> get its project.
        files = {
            "file": (
                "a.docx",
                dx(["One line."]),
                "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            )
        }
        r = c.post("/api/documents", headers=HA, files=files)
        assert r.status_code == 200, r.text
        doc_id = r.json()["document_id"]
        with Session(engine) as s:
            pid = s.get(Document, doc_id).project_id

        # RBAC: budget routes admin-only. The translator is not a member of this
        # project, so #219 answers the missing-id 404 before the role check's 403;
        # either way the translator is refused.
        assert c.post(
            f"/api/projects/{pid}/budget", headers=HT, json={"monthly_usd_limit": 5}
        ).status_code in (403, 404)
        assert c.get(f"/api/projects/{pid}/budget", headers=HT).status_code in (403, 404)
        # Admin sets a tiny budget already exceeded by a seeded settled leg.
        _seed_leg(
            _mk_doc(pid), VendorDispatchStatus.RETURNED, actual=99.0, returned_at=datetime.utcnow()
        )
        r = c.post(f"/api/projects/{pid}/budget", headers=HA, json={"monthly_usd_limit": 1})
        assert r.status_code == 200 and r.json()["created"], r.text
        # GET reflects it (admin).
        rg = c.get(f"/api/projects/{pid}/budget", headers=HA)
        assert rg.status_code == 200 and rg.json()["over"] is True, rg.text

        # Over-budget project -> translate 429, zero jobs enqueued.
        with Session(engine) as s:
            before = len(s.exec(select(Job).where(Job.kind == "translate_document")).all())
        r = c.post(
            f"/api/documents/{doc_id}/translate", headers=HA, json={"targets": ["fr-FR", "de-DE"]}
        )
        assert r.status_code == 429, (r.status_code, r.text)
        assert r.json()["error"] == project_budgets.EXHAUSTED_MESSAGE, r.text
        with Session(engine) as s:
            after = len(s.exec(select(Job).where(Job.kind == "translate_document")).all())
        assert after == before, f"over-budget translate enqueued jobs: {before}->{after}"

        # project-show budget block: manager+ only (translator omits it, admin has it).
        assert "budget" not in c.get(f"/api/projects/{pid}", headers=HT).json()
        assert "budget" in c.get(f"/api/projects/{pid}", headers=HA).json()

        # DELETE (admin) removes it -> translate allowed again (202).
        assert c.post(f"/api/projects/{pid}/budget/delete", headers=HA).status_code == 200
        r = c.post(f"/api/documents/{doc_id}/translate", headers=HA, json={"targets": ["fr-FR"]})
        assert r.status_code == 202, (r.status_code, r.text)
        print(
            "  api: admin-only CRUD; over-budget 429 (zero jobs); manager+ show block; delete re-opens"  # noqa: E501
        )
    finally:
        from app.services import providers as _prov

        _prov._OVERRIDES.pop("claude", None)


# --- Advanced Workflows (#357) ------------------------------------------------
class PricedStubProvider(Provider):
    """A priced offline provider registered over "claude": 100k in + 20k out per call."""

    name = "claude"
    calls = 0

    def complete(self, prompt, system=None, task=None, payload=None, **kw):
        PricedStubProvider.calls += 1
        self.last_usage = {
            "input_tokens": 100000,
            "output_tokens": 20000,
            "cache_read_tokens": 0,
            "cache_write_tokens": 0,
        }
        return "Reservar"


def main():
    init_db()
    security.seed_initial()
    _setup_refs()
    settings.set_many({"audit_enabled": "1"})
    test_crud()
    test_spend_and_scoping()
    test_human_spend()
    test_check()
    test_org_from_project()
    test_budget_state()
    test_spend_survives_out_of_range_job_id()
    test_first_hit_attribution()
    test_job_project_id_safe_bounds()
    test_thresholds()
    test_evaluate_own_session()
    test_thresholds_concurrent_single_fire()
    # H3/N2/N3 seam pins (one per enforcement seam; each goes red when its check is neutered).
    test_seam_orchestrator_start_run()
    test_seam_orchestrator_run_workflow()
    test_seam_batch_validate()
    test_seam_rerun_batch()
    test_seam_vendor_dispatch()
    test_seam_keys_launch_translation()
    test_seam_web_keys_translate()
    test_seam_web_document_translate()
    test_seam_cli_doc_translate()
    test_h1_block_fires_100()
    test_api_budget_foreign_project_404_before_validation()
    test_api_enforcement_and_rbac()
    print("OK")


if __name__ == "__main__":
    main()
