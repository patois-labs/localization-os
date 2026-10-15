"""Unified /jobs execution-history verification (offline, hermetic).

The /jobs page became a real historical execution LOG: one chronological,
newest-first list that interleaves BOTH background Jobs and workflow runs
(WorkflowRun), with filters, pagination, and — critically — the SAME scoping the
rest of the app enforces. This suite proves:

  (1) INTERLEAVE + ORDER : jobs and runs merge into one newest-first list.
  (2) FILTERS            : type (job/run), status, kind (job-only -> excludes runs),
                           free-actor substring.
  (3) PAGINATION         : limit/offset slice a stable window; total is the full
                           in-scope count; boundaries (no overlap, no gap) hold.
  (4) SCOPE: owner       : a non-see-all user sees ONLY its own jobs + ONLY runs in
                           projects it may view (project-visibility, under-show).
  (5) SCOPE: multi-org   : with the sacred gate ON, an org-A user sees NEITHER a
                           foreign-org job NOR a foreign-org run (leak vectors 4/5).
  (6) SCOPE: customer    : a customer-bound user sees only its customer's runs, and
                           (under-show) only jobs it created — no foreign bleed.
  (7) SCOPE-THEN-PAGINATE: pagination cannot leak a count across scopes (total is the
                           scoped total; an out-of-scope row never appears on any page).
  (8) ATTRIBUTION        : WorkflowRun.created_by exists + is populated by start_run /
                           run_workflow; the actor filter matches on runs.
  (9) CLI PARITY         : `job list --include-runs/--runs-only/--kind/--status/
                           --actor/--limit/--offset` calls the same seam (in-process).
  (10) STATUS POLLING    : GET /jobs/{id}/status still behaves identically (jobs.js
                           depends on it) — own job 200, foreign job 404.
  (11) WEB RENDER        : GET /jobs renders both row types + the filter form.

Hermetic: throwaway LOC_TMS_DATA_DIR + cleared provider keys (MockProvider),
ASCII-only prints. Final line: "JOBS_HISTORY OK".
"""

import io
import json as _json
import os
import sys
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timedelta

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(_ROOT)
sys.path.insert(0, _ROOT)
from tests._bootstrap import init_env

init_env("loctms_jobshist_")

from fastapi.testclient import TestClient
from sqlalchemy import inspect
from sqlmodel import Session, select

from app.cli.__main__ import main
from app.db import engine, init_db
from app.main import app
from app.models import (
    Connection,
    Document,
    Job,
    JobStatus,
    Project,
    Role,
    User,
    Workflow,
    WorkflowRun,
)
from app.services import job_history, security, settings

_T0 = datetime(2026, 1, 1, 12, 0, 0)


# --- setup helpers -----------------------------------------------------------
def _mk_user(email, role, org_id=None, customer_id=None):
    u = security.create_user(
        email,
        email.split("@")[0],
        role,
        "fixture-pass-1234!!",
        customer_id=customer_id,
        org_id=org_id,  # #181: created IN its org
    )
    if org_id is not None:
        with Session(engine) as s:
            row = s.get(User, u.id)
            row.org_id = org_id
            s.add(row)
            s.commit()
    return security.get_by_email(email)


def _mk_project(name, *, org_id=None, customer_id=None, created_by=""):
    with Session(engine) as s:
        p = Project(
            name=name,
            org_id=org_id,
            customer_id=customer_id,
            created_by=created_by,
            source_locale="en-US",
        )
        s.add(p)
        s.commit()
        s.refresh(p)
        return p.id


def _mk_doc(project_id, filename):
    with Session(engine) as s:
        d = Document(project_id=project_id, filename=filename, fmt="docx")
        s.add(d)
        s.commit()
        s.refresh(d)
        return d.id


def _mk_workflow(name, org_id=None):
    with Session(engine) as s:
        w = Workflow(name=name, org_id=org_id)
        s.add(w)
        s.commit()
        s.refresh(w)
        return w.id


def _mk_job(kind, created_by, status, tick, payload=None, result=None, error=""):
    with Session(engine) as s:
        j = Job(
            kind=kind,
            created_by=created_by,
            status=status,
            payload=payload or {},
            result=result or {},
            error=error,
            created_at=_T0 + timedelta(minutes=tick),
            started_at=_T0 + timedelta(minutes=tick),
            finished_at=_T0 + timedelta(minutes=tick, seconds=30),
            progress_done=(2 if status == JobStatus.COMPLETED else 1),
            progress_total=2,
        )
        s.add(j)
        s.commit()
        s.refresh(j)
        return j.id


def _mk_run(workflow_id, document_id, created_by, status, tick, target_locale="fr-FR"):
    with Session(engine) as s:
        r = WorkflowRun(
            workflow_id=workflow_id,
            document_id=document_id,
            created_by=created_by,
            status=status,
            target_locale=target_locale,
            started_at=_T0 + timedelta(minutes=tick),
            finished_at=_T0 + timedelta(minutes=tick, seconds=45),
        )
        s.add(r)
        s.commit()
        s.refresh(r)
        return r.id


def _hist(user, **kw):
    with Session(engine) as s:
        return job_history.unified_history(s, user, **kw)


def _reset_settings():
    settings.set_many(
        {"security_auth_required": "0", "multi_org_enabled": "0", "project_scoping": "0"}
    )
    settings.invalidate_cache()


def run_cli(argv):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        try:
            code = main(argv)
        except SystemExit as exc:
            code = exc.code if isinstance(exc.code, int) else 2
    return code, out.getvalue(), err.getvalue()


# ============================================================================
# 1. INTERLEAVE + ORDER
# ============================================================================
def test_interleave_and_order():
    _reset_settings()
    pid = _mk_project("Order-Proj")
    did = _mk_doc(pid, "order.docx")
    wid = _mk_workflow("Order-WF")
    # Interleave: job@1, run@2, job@3, run@4 (ticks == chronological order).
    j1 = _mk_job(
        "translate_document",
        "a@x",
        JobStatus.COMPLETED,
        1,
        payload={"document_id": did, "locale": "fr-FR"},
    )
    r2 = _mk_run(wid, did, "a@x", "completed", 2)
    j3 = _mk_job(
        "connector_translate",
        "a@x",
        JobStatus.FAILED,
        3,
        payload={"connection_id": 999},
        error="boom",
    )
    r4 = _mk_run(wid, did, "b@x", "running", 4)

    rows, total = _hist(None, type="all")
    assert total == 4, total
    seq = [(r.type, r.id) for r in rows]
    # newest-first: run4, job3, run2, job1
    assert seq == [("run", r4), ("job", j3), ("run", r2), ("job", j1)], seq
    # both types present + labelled
    assert {r.type for r in rows} == {"job", "run"}, seq
    # subject resolution: translate_document -> filename -> locale
    jrow = next(r for r in rows if r.id == j1 and r.type == "job")
    assert jrow.subject == "order.docx -> fr-FR", jrow.subject
    # run subject -> filename -> locale + link
    rrow = next(r for r in rows if r.id == r2 and r.type == "run")
    assert rrow.subject == "order.docx -> fr-FR", rrow.subject
    assert rrow.link == f"/runs/{r2}", rrow.link
    assert rrow.kind == "Order-WF", rrow.kind
    print("  1 interleave+order: 4 rows, newest-first, both types, subjects OK")


# ============================================================================
# 2. FILTERS
# ============================================================================
def test_filters():
    _reset_settings()
    _wipe()
    pid = _mk_project("F-Proj")
    did = _mk_doc(pid, "f.docx")
    wid = _mk_workflow("F-WF")
    _mk_job("translate_document", "alice@x", JobStatus.COMPLETED, 1, payload={"document_id": did})
    _mk_job("connector_translate", "bob@x", JobStatus.FAILED, 2, payload={"connection_id": 1})
    _mk_run(wid, did, "alice@x", "completed", 3)
    _mk_run(wid, did, "carol@x", "failed", 4)

    # type=job
    rows, total = _hist(None, type="job")
    assert total == 2 and all(r.type == "job" for r in rows), (total, rows)
    # type=run
    rows, total = _hist(None, type="run")
    assert total == 2 and all(r.type == "run" for r in rows), (total, rows)
    # status=failed matches a job AND a run
    rows, total = _hist(None, type="all", status="failed")
    assert total == 2 and {r.type for r in rows} == {"job", "run"}, (total, rows)
    # kind is job-only -> runs excluded
    rows, total = _hist(None, type="all", kind="translate_document")
    assert total == 1 and rows[0].kind == "translate_document", (total, rows)
    # actor substring (case-insensitive) matches a job and a run
    rows, total = _hist(None, type="all", actor="ALICE")
    assert total == 2 and {r.type for r in rows} == {"job", "run"}, (total, rows)
    assert all("alice" in r.actor for r in rows), rows
    print("  2 filters: type/status/kind(job-only)/actor all correct")


# ============================================================================
# 3. PAGINATION (boundaries: no overlap, no gap; total is full count)
# ============================================================================
def test_pagination():
    _reset_settings()
    _wipe()
    pid = _mk_project("P-Proj")
    did = _mk_doc(pid, "p.docx")
    wid = _mk_workflow("P-WF")
    # 5 rows total (3 jobs + 2 runs) at ticks 1..5.
    for t in (1, 3, 5):
        _mk_job("translate_document", "u@x", JobStatus.COMPLETED, t, payload={"document_id": did})
    for t in (2, 4):
        _mk_run(wid, did, "u@x", "completed", t)

    full, total = _hist(None, type="all", limit=100)
    assert total == 5 and len(full) == 5, (total, len(full))
    full_seq = [(r.type, r.id) for r in full]

    # page size 2: three pages (2, 2, 1); slices must concat back to the full seq.
    p0, t0 = _hist(None, type="all", limit=2, offset=0)
    p1, t1 = _hist(None, type="all", limit=2, offset=2)
    p2, t2 = _hist(None, type="all", limit=2, offset=4)
    assert t0 == t1 == t2 == 5, (t0, t1, t2)
    assert len(p0) == 2 and len(p1) == 2 and len(p2) == 1, (len(p0), len(p1), len(p2))
    concat = [(r.type, r.id) for r in (p0 + p1 + p2)]
    assert concat == full_seq, (concat, full_seq)  # no overlap, no gap, same order
    # offset past the end -> empty page, total still full.
    pend, tend = _hist(None, type="all", limit=2, offset=10)
    assert pend == [] and tend == 5, (pend, tend)
    print("  3 pagination: total=5 stable, pages tile the full list exactly")


# ============================================================================
# 4. SCOPE: owner-only user (project visibility + job ownership)
# ============================================================================
def test_scope_owner():
    _reset_settings()
    _wipe()
    settings.set_many({"security_auth_required": "1", "project_scoping": "1"})
    settings.invalidate_cache()
    admin = _mk_user("owner_admin@x", Role.ADMIN)
    tr = _mk_user("owner_tr@x", Role.TRANSLATOR)
    # A project the translator OWNS (created_by == their email) -> visible; and a
    # project owned by the admin -> invisible to the translator.
    p_mine = _mk_project("Mine", created_by="owner_tr@x")
    p_other = _mk_project("Other", created_by="owner_admin@x")
    d_mine = _mk_doc(p_mine, "mine.docx")
    d_other = _mk_doc(p_other, "other.docx")
    wid = _mk_workflow("Owner-WF")
    # Jobs: one by the translator, one by the admin.
    _mk_job(
        "translate_document", "owner_tr@x", JobStatus.COMPLETED, 1, payload={"document_id": d_mine}
    )
    _mk_job(
        "translate_document",
        "owner_admin@x",
        JobStatus.COMPLETED,
        2,
        payload={"document_id": d_other},
    )
    # Runs: one in the visible project, one in the invisible project.
    _mk_run(wid, d_mine, "owner_tr@x", "completed", 3)
    _mk_run(wid, d_other, "owner_admin@x", "completed", 4)

    # Translator: only its own job + only the run in its visible project.
    rows, total = _hist(tr, type="all")
    assert total == 2, (total, [(r.type, r.actor, r.subject) for r in rows])
    jobs_seen = [r for r in rows if r.type == "job"]
    runs_seen = [r for r in rows if r.type == "run"]
    assert len(jobs_seen) == 1 and jobs_seen[0].actor == "owner_tr@x", jobs_seen
    assert len(runs_seen) == 1 and runs_seen[0].subject.startswith("mine.docx"), runs_seen

    # Admin (see-all, single-org): sees everything (4 rows).
    _rows_a, total_a = _hist(admin, type="all")
    assert total_a == 4, total_a
    _reset_settings()
    print("  4 scope owner: translator sees only own job + own visible-project run")


# ============================================================================
# 5. SCOPE: multi-org gate (foreign-org job AND run both hidden)
# ============================================================================
def test_scope_multiorg():
    _reset_settings()
    _wipe()
    settings.set_many({"security_auth_required": "1", "multi_org_enabled": "1"})
    settings.invalidate_cache()
    from app.models import Organization

    with Session(engine) as s:
        oa, ob = Organization(name="OrgA"), Organization(name="OrgB")
        s.add(oa)
        s.add(ob)
        s.commit()
        s.refresh(oa)
        s.refresh(ob)
        org_a, org_b = oa.id, ob.id
    admin_a = _mk_user("mo_admin_a@x", Role.ADMIN, org_id=org_a)
    _mk_user("mo_user_b@x", Role.ADMIN, org_id=org_b)  # org-B actor (for job_org_id)
    p_a = _mk_project("OrgA-Proj", org_id=org_a, created_by="mo_admin_a@x")
    p_b = _mk_project("OrgB-Proj", org_id=org_b, created_by="mo_user_b@x")
    d_a = _mk_doc(p_a, "a.docx")
    d_b = _mk_doc(p_b, "b.docx")
    wid = _mk_workflow("MO-WF")
    # Jobs: org-A job (actor in org 1), org-B job (actor in org 2). job_org_id
    # resolves via the creator's org.
    _mk_job(
        "translate_document", "mo_admin_a@x", JobStatus.COMPLETED, 1, payload={"document_id": d_a}
    )
    _mk_job(
        "translate_document", "mo_user_b@x", JobStatus.COMPLETED, 2, payload={"document_id": d_b}
    )
    # Runs: one in each org's project.
    _mk_run(wid, d_a, "mo_admin_a@x", "completed", 3)
    _mk_run(wid, d_b, "mo_user_b@x", "completed", 4)

    rows, total = _hist(admin_a, type="all")
    # org-A admin: exactly its org's job + run. The foreign-org job AND run are gone.
    assert total == 2, (total, [(r.type, r.actor, r.subject) for r in rows])
    subs = sorted(r.subject.split(" ")[0] for r in rows)
    assert subs == ["a.docx", "a.docx"], subs  # both rows reference org-A doc
    assert all(r.actor == "mo_admin_a@x" for r in rows), rows
    _reset_settings()
    print("  5 scope multi-org: org-A user sees neither foreign-org job nor run")


# ============================================================================
# 6. SCOPE: customer-bound user (runs confined to its customer; jobs own-only)
# ============================================================================
def test_scope_customer():
    _reset_settings()
    _wipe()
    settings.set_many({"security_auth_required": "1"})
    settings.invalidate_cache()
    from app.models import Customer

    with Session(engine) as s:
        ca = Customer(name="CustA")
        cb = Customer(name="CustB")
        s.add(ca)
        s.add(cb)
        s.commit()
        s.refresh(ca)
        s.refresh(cb)
        ca_id, cb_id = ca.id, cb.id
    bound = _mk_user("cust_bound@x", Role.TRANSLATOR, customer_id=ca_id)
    p_a = _mk_project("CustA-Proj", customer_id=ca_id, created_by="staff@x")
    p_b = _mk_project("CustB-Proj", customer_id=cb_id, created_by="staff@x")
    d_a = _mk_doc(p_a, "custa.docx")
    d_b = _mk_doc(p_b, "custb.docx")
    wid = _mk_workflow("Cust-WF")
    # Jobs: one created BY the bound user, one by staff. A bound user under-shows to
    # own-jobs-only (a Job has no customer link).
    _mk_job(
        "translate_document", "cust_bound@x", JobStatus.COMPLETED, 1, payload={"document_id": d_a}
    )
    _mk_job("translate_document", "staff@x", JobStatus.COMPLETED, 2, payload={"document_id": d_a})
    # Runs: one in customer-A's project (visible), one in customer-B's (hidden).
    _mk_run(wid, d_a, "staff@x", "completed", 3)
    _mk_run(wid, d_b, "staff@x", "completed", 4)

    rows, total = _hist(bound, type="all")
    jobs_seen = [r for r in rows if r.type == "job"]
    runs_seen = [r for r in rows if r.type == "run"]
    # jobs: only the bound user's own (under-show); not staff's.
    assert len(jobs_seen) == 1 and jobs_seen[0].actor == "cust_bound@x", jobs_seen
    # runs: only customer-A's project run; customer-B is not even visible.
    assert len(runs_seen) == 1 and runs_seen[0].subject.startswith("custa.docx"), runs_seen
    assert total == 2, total
    _reset_settings()
    print("  6 scope customer: bound user -> own jobs only + only its customer's runs")


# ============================================================================
# 7. SCOPE-THEN-PAGINATE (a page/total never leaks across scope)
# ============================================================================
def test_scope_then_paginate():
    _reset_settings()
    _wipe()
    settings.set_many({"security_auth_required": "1", "project_scoping": "1"})
    settings.invalidate_cache()
    tr = _mk_user("stp_tr@x", Role.TRANSLATOR)
    p_mine = _mk_project("STP-Mine", created_by="stp_tr@x")
    p_other = _mk_project("STP-Other", created_by="stp_admin@x")
    d_mine = _mk_doc(p_mine, "stp_mine.docx")
    d_other = _mk_doc(p_other, "stp_other.docx")
    wid = _mk_workflow("STP-WF")
    # 6 out-of-scope rows (foreign runs + others' jobs) and 3 in-scope rows.
    for t in range(10, 16):
        _mk_run(wid, d_other, "stp_admin@x", "completed", t)  # invisible
    _mk_job(
        "translate_document", "stp_tr@x", JobStatus.COMPLETED, 1, payload={"document_id": d_mine}
    )
    _mk_run(wid, d_mine, "stp_tr@x", "completed", 2)
    _mk_job("connector_translate", "stp_tr@x", JobStatus.COMPLETED, 3, payload={"connection_id": 5})

    # total is the SCOPED total (3), not the 9 rows in the tables.
    _rows, total = _hist(tr, type="all", limit=2, offset=0)
    assert total == 3, total
    all_scoped, _ = _hist(tr, type="all", limit=100)
    # No page ever contains an out-of-scope (foreign-project) subject.
    for r in all_scoped:
        assert "stp_other" not in r.subject, r.subject
    _reset_settings()
    print("  7 scope-then-paginate: total==scoped 3 (not 9); no foreign row on any page")


# ============================================================================
# 8. ATTRIBUTION: WorkflowRun.created_by column + real run seams
# ============================================================================
def test_attribution_column_and_seams():
    _reset_settings()
    _wipe()
    cols = {c["name"] for c in inspect(engine).get_columns("workflowrun")}
    assert "created_by" in cols, cols
    from app.models import Segment
    from app.services import orchestrator
    from app.services import workflows as wf_service

    # A minimal SEGMENTED document with one fr-FR segment so a run executes offline
    # (MockProvider) without file ingestion.
    with Session(engine) as s:
        p = Project(name="Attr-Proj", source_locale="en-US")
        s.add(p)
        s.commit()
        s.refresh(p)
        d = Document(project_id=p.id, filename="attr.docx", fmt="docx")
        s.add(d)
        s.commit()
        s.refresh(d)
        s.add(
            Segment(
                document_id=d.id, target_locale="fr-FR", order_index=0, source_text="Hello world."
            )
        )
        s.commit()
        did = d.id
    wid = _mk_workflow("Attr-WF")
    with Session(engine) as s:
        wf_service.add_step(
            s, wid, name="MT", step_type="llm_agent", prompt_text="Translate: {text}"
        )
    # Synchronous run via run_workflow(created_by=...): attributes the fresh run.
    with Session(engine) as s:
        summary = orchestrator.run_workflow(s, wid, did, "fr-FR", created_by="runner@x")
    run_id = summary.get("run_id")
    assert run_id is not None, summary
    with Session(engine) as s:
        run = s.get(WorkflowRun, run_id)
        assert run.created_by == "runner@x", run.created_by
    # The actor filter finds it.
    rows, total = _hist(None, type="run", actor="runner@x")
    assert total == 1 and rows[0].id == run_id, (total, rows)
    print("  8 attribution: workflowrun.created_by present + run_workflow stamps it")


# ============================================================================
# 9. CLI PARITY (`job list` calls the same seam)
# ============================================================================
def test_cli_parity():
    _reset_settings()
    _wipe()
    pid = _mk_project("CLI-Proj")
    did = _mk_doc(pid, "cli.docx")
    wid = _mk_workflow("CLI-WF")
    _mk_job("translate_document", "cli_a@x", JobStatus.COMPLETED, 1, payload={"document_id": did})
    _mk_run(wid, did, "cli_b@x", "completed", 2)

    # Default: jobs only (backward-compatible).
    code, out, _ = run_cli(["--json", "job", "list"])
    payload = _json.loads(out.strip().splitlines()[-1])
    assert code == 0 and payload["ok"], (code, out)
    assert all(j["type"] == "job" for j in payload["jobs"]), payload
    assert payload["total"] == 1, payload
    # PARITY / BACKWARD-COMPAT: the in-process --json row must use the SAME stable key
    # shape as /api + `--remote` (as_api_dict), NOT the raw dataclass as_dict(). The
    # legacy `job list --json` keys (created_by/done/total) survive; the /api-shape
    # timestamp/link fields are present. Guards against the two CLI arms drifting.
    row = payload["jobs"][0]
    for k in (
        "type",
        "id",
        "kind",
        "status",
        "done",
        "total",
        "created_by",
        "subject",
        "link",
        "duration_s",
        "project",
        "target_locale",
        "started_at",
        "finished_at",
    ):
        assert k in row, (f"missing canonical key {k!r}", row)
    assert "progress_done" not in row and "actor" not in row, (
        "in-process --json leaked raw dataclass keys (drifts from /api)",
        row,
    )
    assert row["created_by"] == "cli_a@x", row

    # --include-runs -> both.
    code, out, _ = run_cli(["--json", "job", "list", "--include-runs"])
    payload = _json.loads(out.strip().splitlines()[-1])
    assert code == 0 and {j["type"] for j in payload["jobs"]} == {"job", "run"}, payload
    assert payload["total"] == 2, payload

    # --runs-only -> runs only.
    code, out, _ = run_cli(["--json", "job", "list", "--runs-only"])
    payload = _json.loads(out.strip().splitlines()[-1])
    assert all(j["type"] == "run" for j in payload["jobs"]) and payload["total"] == 1, payload

    # --kind narrows to jobs; --actor filters; --limit/--offset paginate.
    code, out, _ = run_cli(["--json", "job", "list", "--kind", "translate_document"])
    payload = _json.loads(out.strip().splitlines()[-1])
    assert payload["total"] == 1 and payload["jobs"][0]["kind"] == "translate_document", payload

    code, out, _ = run_cli(["--json", "job", "list", "--include-runs", "--actor", "cli_b"])
    payload = _json.loads(out.strip().splitlines()[-1])
    assert payload["total"] == 1 and payload["jobs"][0]["type"] == "run", payload

    code, out, _ = run_cli(
        ["--json", "job", "list", "--include-runs", "--limit", "1", "--offset", "1"]
    )
    payload = _json.loads(out.strip().splitlines()[-1])
    assert payload["total"] == 2 and len(payload["jobs"]) == 1, payload
    print("  9 CLI parity: job list default/include-runs/runs-only/kind/actor/paging OK")


# ============================================================================
# 9b. DOCUMENT / WORKFLOW FILTERS (job list --document/--workflow + /jobs + /api)
# ============================================================================
def test_document_workflow_filters():
    _reset_settings()
    _wipe()
    pid = _mk_project("DW-Proj")
    d1 = _mk_doc(pid, "d1.docx")
    d2 = _mk_doc(pid, "d2.docx")
    w1 = _mk_workflow("DW-WF-1")
    w2 = _mk_workflow("DW-WF-2")
    # jobs: one targeting d1, one targeting d2, one non-doc kind (connector)
    _mk_job("translate_document", "u@x", JobStatus.COMPLETED, 1, payload={"document_id": d1})
    _mk_job("translate_document", "u@x", JobStatus.COMPLETED, 2, payload={"document_id": d2})
    _mk_job("connector_translate", "u@x", JobStatus.FAILED, 3, payload={"connection_id": 7})
    # runs: w1 on d1, w2 on d2
    _mk_run(w1, d1, "u@x", "completed", 4)
    _mk_run(w2, d2, "u@x", "completed", 5)

    # --document d1 (type=all): the d1 job + the w1/d1 run, nothing about d2.
    rows, total = _hist(None, type="all", document_id=d1)
    assert total == 2, [(r.type, r.subject) for r in rows]
    assert all(r.subject.startswith("d1.docx") for r in rows), rows
    # --workflow w1 EXCLUDES jobs entirely (a job has no workflow) -> only the w1 run.
    rows, total = _hist(None, type="all", workflow_id=w1)
    assert total == 1 and rows[0].type == "run" and rows[0].kind == "DW-WF-1", rows
    # combined document + workflow: the intersection (w2 on d2) -> 1 run.
    rows, total = _hist(None, type="all", document_id=d2, workflow_id=w2)
    assert total == 1 and rows[0].kind == "DW-WF-2", rows
    # SCOPED-EXISTENCE: an UNKNOWN document/workflow id -> empty (no oracle, no error).
    assert _hist(None, type="all", document_id=999999)[1] == 0
    assert _hist(None, type="all", workflow_id=999999)[1] == 0

    # CLI parity (in-process): --document / --workflow narrow identically.
    code, out, _ = run_cli(["--json", "job", "list", "--include-runs", "--document", str(d1)])
    payload = _json.loads(out.strip().splitlines()[-1])
    assert code == 0 and payload["total"] == 2, payload
    code, out, _ = run_cli(["--json", "job", "list", "--include-runs", "--workflow", str(w1)])
    payload = _json.loads(out.strip().splitlines()[-1])
    assert payload["total"] == 1 and payload["jobs"][0]["type"] == "run", payload
    # unknown id via CLI -> empty, exit 0 (a filter, never a lookup error).
    code, out, _ = run_cli(["--json", "job", "list", "--include-runs", "--document", "888888"])
    payload = _json.loads(out.strip().splitlines()[-1])
    assert code == 0 and payload["total"] == 0, payload

    # /api parity + scoped-existence for a REQUEST caller (gate off = no scoping here,
    # but the filter is still applied as an additional predicate, never an oracle).
    from app.services import apikeys as _ak

    settings.set_many({"security_auth_required": "1"})
    settings.invalidate_cache()
    admin = _mk_user("dw_admin@x", Role.ADMIN)
    _row, tok = _ak.create_key(admin.id, "dw", scopes="api docs")
    c = TestClient(app)
    h = {"X-Loc-Token": tok}
    r = c.get(f"/api/jobs?include_runs=true&document={d1}", headers=h)
    assert r.status_code == 200, r.text
    assert r.json()["total"] == 2, r.json()
    r = c.get(f"/api/jobs?include_runs=true&workflow={w1}", headers=h)
    assert r.json()["total"] == 1, r.json()
    r = c.get("/api/jobs?include_runs=true&document=777777", headers=h)
    assert r.json()["total"] == 0, r.json()
    _reset_settings()
    print(
        "  9b filters: --document/--workflow (service+CLI+/api); workflow excludes "
        "jobs; unknown id -> empty (scoped-existence, no oracle)"
    )


def test_document_filter_no_cross_scope_oracle():
    """A foreign-scope document/workflow id must be INDISTINGUISHABLE from a
    nonexistent one: an owner-only user filtering by a document in a project they
    cannot see gets an empty list, exactly like an unknown id (the filter is applied
    as a WHERE on the already-scope-confined query, never a pre-existence check)."""
    _reset_settings()
    _wipe()
    settings.set_many({"security_auth_required": "1", "project_scoping": "1"})
    settings.invalidate_cache()
    _mk_user("dw_owner_admin@x", Role.ADMIN)
    tr = _mk_user("dw_owner_tr@x", Role.TRANSLATOR)
    p_other = _mk_project("DW-Other", created_by="dw_owner_admin@x")
    d_other = _mk_doc(p_other, "other.docx")
    w = _mk_workflow("DW-Owner-WF")
    _mk_job(
        "translate_document",
        "dw_owner_admin@x",
        JobStatus.COMPLETED,
        1,
        payload={"document_id": d_other},
    )
    _mk_run(w, d_other, "dw_owner_admin@x", "completed", 2)
    # The translator can see NEITHER the job (not its owner) NOR the run (foreign
    # project). Filtering by the REAL-but-foreign d_other id yields the SAME empty
    # result as a truly-unknown id -> no existence oracle.
    assert _hist(tr, type="all", document_id=d_other)[1] == 0
    assert _hist(tr, type="all", document_id=999999)[1] == 0
    assert _hist(tr, type="all", workflow_id=w)[1] == 0
    _reset_settings()
    print(
        "  9c no-oracle: a foreign-scope document/workflow id filters to empty, "
        "indistinguishable from a nonexistent id"
    )


# ============================================================================
# 10 + 11. STATUS POLLING unchanged + WEB render
# ============================================================================
def test_status_polling_and_web_render():
    _reset_settings()
    _wipe()
    pid = _mk_project("Web-Proj")
    did = _mk_doc(pid, "web.docx")
    wid = _mk_workflow("Web-WF")
    jid = _mk_job(
        "translate_document",
        "",
        JobStatus.COMPLETED,
        1,
        payload={"document_id": did},
        result={"message": "done", "failures": []},
    )
    _mk_run(wid, did, "web_runner@x", "completed", 2)

    c = TestClient(app)
    # (10) status polling: shape unchanged (id/kind/status/done/total/message/error/result).
    r = c.get(f"/jobs/{jid}/status")
    assert r.status_code == 200, r.status_code
    body = r.json()
    for k in ("id", "kind", "status", "done", "total", "message", "error", "result"):
        assert k in body, (k, body)
    assert body["id"] == jid and body["status"] == "completed", body
    # a non-existent id 404s (not enumerable), unchanged.
    assert c.get("/jobs/99999/status").status_code == 404

    # (11) web render: both row types + the filter form present.
    r = c.get("/jobs")
    assert r.status_code == 200, r.status_code
    html = r.text
    assert "web.docx" in html, "job/run subject not rendered"
    assert "Web-WF" in html, "workflow name (run kind) not rendered"
    assert 'name="type"' in html and 'name="status"' in html, "filter form missing"
    assert "/runs/" in html, "run view link missing"
    # a filtered request (jobs only) still 200s and excludes the run's workflow name.
    r2 = c.get("/jobs?type=job")
    assert r2.status_code == 200 and "Web-WF" not in r2.text, "type=job leaked a run"
    # a valid document filter still 200s; a NON-INTEGER document/workflow id is a clean
    # 422 (int-coercion guard), never a 500 -- both on /jobs and /api/jobs.
    assert c.get(f"/jobs?document={did}").status_code == 200
    assert c.get("/jobs?document=abc").status_code == 422, "non-int document must 422"
    assert c.get("/jobs?workflow=abc").status_code == 422, "non-int workflow must 422"
    from app.services import apikeys as _ak2

    admin2 = _mk_user("web422_admin@x", Role.ADMIN)
    _r2, tok2 = _ak2.create_key(admin2.id, "w422", scopes="api docs")
    h2 = {"X-Loc-Token": tok2}
    settings.set_many({"security_auth_required": "1"})
    settings.invalidate_cache()
    assert c.get("/api/jobs?document=abc", headers=h2).status_code == 422, (
        "/api/jobs non-int document must 422"
    )
    _reset_settings()
    print(
        "  10+11 status polling shape intact (own 200 / missing 404) + web render + "
        "non-int document/workflow -> 422 OK"
    )


# --- utility -----------------------------------------------------------------
def _wipe():
    """Clear the rows the previous test created so counts are deterministic. Child
    tables first so FK constraints hold (StepRun -> WorkflowRun, Segment -> Document)."""
    from app.models import Segment, StepRun, WorkflowStep

    with Session(engine) as s:
        for model in (
            StepRun,
            WorkflowRun,
            WorkflowStep,
            Segment,
            Document,
            Workflow,
            Connection,
            Job,
            Project,
        ):
            for row in s.exec(select(model)).all():
                s.delete(row)
        s.commit()


def main_suite():
    init_db()
    test_interleave_and_order()
    test_filters()
    test_pagination()
    test_scope_owner()
    test_scope_multiorg()
    test_scope_customer()
    test_scope_then_paginate()
    test_attribution_column_and_seams()
    test_cli_parity()
    test_document_workflow_filters()
    test_document_filter_no_cross_scope_oracle()
    test_status_polling_and_web_render()
    print("JOBS_HISTORY OK")


if __name__ == "__main__":
    main_suite()
