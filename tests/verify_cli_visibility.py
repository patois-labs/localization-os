"""CLI visibility of the operator's own work (issues #16 + #25) — hermetic.

The in-process CLI is the documented local-root operator (docs/CLI.md: "no login
and no RBAC"). Two distinct defects hid the operator's OWN work from their OWN
surfaces once `init` turns the login wall ON; this suite pins both fixes and,
critically, proves the WEB/API arms stay scoped (the operator seam must be
unreachable from a request path):

  (1) #16a JOB LIST RBAC-BLIND  : with the login wall ON, an in-process
        `job list` (trusted_operator=True) sees the operator's jobs; the SAME call
        with user=None and NO trusted_operator flag (the WEB "no user" semantic)
        sees NOTHING. Fail-pre-fix: cmd_job passed no flag, so user=None went blind.
  (2) #16a RUN LIST RBAC-BLIND  : with the wall ON, `job list --include-runs`
        (trusted_operator) shows a completed workflow run; the un-flagged call hides it.
  (3) #16b REPORTS BLIND TO CLI : a real (mock) `doc translate` driven through the
        CLI now writes a translate_document Job row, so report run cost/tokens/volume
        AND `job list` all reflect it (they read Job rows) — previously EMPTY because
        the CLI-sync path wrote no Job row. Cost is $0 on mock; volume/tokens rows must
        exist and volume words must be > 0.
  (4) WEB/API STILL SCOPED      : a non-privileged web user must NOT see another
        user's jobs, and the un-flagged user=None (web) sees nothing — the seam that
        gives the operator everything cannot be reached from a request path.
  (5) #25 UNKNOWN KIND ERRORS   : `report run <bogus>` errors on the CLI (argparse
        choices=) AND the service (reports.validate_kind / normalized) raises
        UnknownReportKind; a valid kind still runs.

Hermetic: throwaway LOC_TMS_DATA_DIR + blanked provider keys (MockProvider),
ASCII-only prints. Final line: "CLI_VISIBILITY OK".
"""

import json as _json
import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(_ROOT)
sys.path.insert(0, _ROOT)
from tests._bootstrap import init_env

init_env("loctms_clivis_")

from sqlmodel import Session, select

from app.db import engine, init_db
from app.models import (
    Customer,
    Document,
    Job,
    JobStatus,
    Project,
    Role,
    Segment,
    User,
)
from app.services import (
    content,
    job_history,
    orchestrator,
    reports,
    security,
    settings,
)
from app.services import workflows as wf_service
from tests._cli_runner import run_cli


# --- helpers -----------------------------------------------------------------
def _reset_settings():
    settings.set_many(
        {"security_auth_required": "0", "multi_org_enabled": "0", "project_scoping": "0"}
    )
    settings.invalidate_cache()


def _auth_on():
    settings.set_many({"security_auth_required": "1"})
    settings.invalidate_cache()


def _mk_customer(name):
    with Session(engine) as s:
        c = Customer(name=name)
        s.add(c)
        s.commit()
        s.refresh(c)
        return c.id


def _mk_project(name, customer_id, created_by="admin@x"):
    with Session(engine) as s:
        p = content.project_create(
            s, name, customer_id=str(customer_id), target_locales="fr-FR", created_by=created_by
        )
        return p.id


def _mk_source_doc(pid, filename, sources):
    """A segmented document with UNtranslated fr-FR segments (target blank), so a
    real `doc translate` actually fills them via MockProvider."""
    with Session(engine) as s:
        d = Document(
            project_id=pid, filename=filename, fmt="txt", mode="segmented", status="parsed"
        )
        s.add(d)
        s.commit()
        s.refresh(d)
        for i, src in enumerate(sources):
            s.add(
                Segment(
                    document_id=d.id,
                    source_text=src,
                    target_text="",
                    target_locale="fr-FR",
                    order_index=i,
                )
            )
        s.commit()
        return d.id


def _mk_job(kind, created_by, status=JobStatus.COMPLETED, payload=None):
    with Session(engine) as s:
        j = Job(kind=kind, created_by=created_by, status=status, payload=payload or {})
        s.add(j)
        s.commit()
        s.refresh(j)
        return j.id


def _hist(user, **kw):
    with Session(engine) as s:
        return job_history.unified_history(s, user, **kw)


def _wipe():
    """Clear the rows prior tests created so job/report counts are deterministic.
    FK checks are relaxed for the duration (project_create fans out to child tables
    like TargetLocale), so order doesn't matter — this is a hermetic test DB."""
    from sqlalchemy import text

    from app.models import (
        Customer,
        Document,
        Job,
        Segment,
        StepRun,
        TargetLocale,
        Workflow,
        WorkflowRun,
        WorkflowStep,
    )

    with Session(engine) as s:
        s.exec(text("PRAGMA foreign_keys=OFF"))
        for model in (
            StepRun,
            WorkflowRun,
            WorkflowStep,
            Workflow,
            Segment,
            TargetLocale,
            Document,
            Job,
            Project,
            Customer,
        ):
            for row in s.exec(select(model)).all():
                s.delete(row)
        s.commit()


# ============================================================================
# 1. #16a — job list is RBAC-blind to the operator once the login wall is ON
# ============================================================================
def test_job_list_visible_post_init_inprocess():
    _reset_settings()
    _wipe()
    _auth_on()  # the login wall that used to blind the in-process operator
    try:
        jid = _mk_job("translate_document", "cli", payload={"document_id": 1, "locale": "fr-FR"})

        # WEB semantic (user=None, NO trusted_operator flag): sees NOTHING. This is
        # the pre-fix behavior the operator was wrongly getting.
        web_rows, web_total = _hist(None, type="job")
        assert web_total == 0 and web_rows == [], (
            "user=None from a request path MUST stay sees-nothing under the wall",
            web_total,
        )

        # TRUSTED OPERATOR (the CLI arm): sees its job.
        op_rows, op_total = _hist(None, type="job", trusted_operator=True)
        assert op_total == 1 and op_rows[0].id == jid, (op_total, op_rows)

        # And end-to-end through the real CLI (`job list`), which now threads the flag.
        code, out, _ = run_cli(["--json", "job", "list"])
        payload = _json.loads(out.strip().splitlines()[-1])
        assert code == 0 and payload["ok"], (code, out)
        assert payload["total"] == 1 and payload["jobs"][0]["id"] == jid, payload
    finally:
        _reset_settings()
    print("  1 #16a job list: login-wall ON -> web user=None blind, CLI operator sees it")


# ============================================================================
# 2. #16a — workflow run visible via --include-runs with the wall ON
# ============================================================================
def test_run_visible_include_runs_login_wall_on():
    _reset_settings()
    _wipe()
    # Build a runnable one-step workflow over a segmented doc (offline MockProvider).
    cid = _mk_customer("RunCust")
    pid = _mk_project("RunProj", cid)
    did = _mk_source_doc(pid, "run.txt", ["Hello world."])
    with Session(engine) as s:
        from app.models import Workflow

        wf = Workflow(name="Run-WF")
        s.add(wf)
        s.commit()
        s.refresh(wf)
        wid = wf.id
        wf_service.add_step(
            s, wid, name="MT", step_type="llm_agent", prompt_text="Translate: {text}"
        )
    with Session(engine) as s:
        summary = orchestrator.run_workflow(s, wid, did, "fr-FR", created_by="cli")
    run_id = summary.get("run_id")
    assert run_id is not None, summary

    _auth_on()
    try:
        # WEB semantic: the completed run is hidden (visible_project_ids(None) == []).
        web_rows, _web_total = _hist(None, type="all")
        assert not any(r.type == "run" for r in web_rows), (
            "a request-path user=None must NOT see runs under the wall",
            web_rows,
        )

        # TRUSTED OPERATOR via --include-runs: the run shows.
        op_rows, op_total = _hist(None, type="all", trusted_operator=True)
        runs = [r for r in op_rows if r.type == "run"]
        assert len(runs) == 1 and runs[0].id == run_id, (op_total, op_rows)

        # End-to-end CLI parity: `job list --include-runs` shows the run.
        code, out, _ = run_cli(["--json", "job", "list", "--include-runs"])
        payload = _json.loads(out.strip().splitlines()[-1])
        assert code == 0, (code, out)
        assert any(j["type"] == "run" and j["id"] == run_id for j in payload["jobs"]), payload
    finally:
        _reset_settings()
    print("  2 #16a runs: login-wall ON -> web blind to run, CLI --include-runs sees it")


# ============================================================================
# 3. #16b — a real CLI `doc translate` now surfaces in reports + job list
# ============================================================================
def test_report_reflects_cli_translation():
    _reset_settings()
    _wipe()
    cid = _mk_customer("TxCust")
    pid = _mk_project("TxProj", cid)
    did = _mk_source_doc(pid, "tx.txt", ["The quick brown fox", "lazy dog"])

    # No translate_document Job exists yet -> every report kind is empty (the bug).
    with Session(engine) as s:
        pre = reports.run_report(
            s, reports.ReportQuery(kind="volume", group_by="none", customer_id=cid), user=None
        )
    assert pre["rows"] == [], ("pre-translate: report is empty (no Job rows)", pre)

    # Drive a REAL synchronous CLI translation (MockProvider fills the segments).
    code, out, err = run_cli(["doc", "translate", str(did), "--locale", "fr-FR"])
    assert code == 0, (code, out, err)

    # The CLI-sync path now recorded a translate_document Job row (the fix).
    with Session(engine) as s:
        jobs = s.exec(select(Job).where(Job.kind == "translate_document")).all()
        assert len(jobs) == 1, ("doc translate must record ONE Job row", jobs)
        assert jobs[0].payload.get("document_id") == did, jobs[0].payload
        assert jobs[0].created_by == "cli", jobs[0].created_by
        assert jobs[0].status == JobStatus.COMPLETED, jobs[0].status

    # VOLUME report now has a row with derived words > 0 (from the translated segments).
    with Session(engine) as s:
        vol = reports.run_report(
            s, reports.ReportQuery(kind="volume", group_by="none", customer_id=cid), user=None
        )
    assert vol["rows"], ("volume report must reflect the CLI translation", vol)
    vrow = vol["rows"][0]
    assert vrow["words_derived"] > 0 and vrow["segments"] >= 1, vrow

    # TOKENS report: a row exists (mock may report 0 tokens, but the JOB is counted).
    with Session(engine) as s:
        tok = reports.run_report(
            s, reports.ReportQuery(kind="tokens", group_by="none", customer_id=cid), user=None
        )
    assert tok["rows"] and tok["rows"][0]["jobs"] == 1, tok

    # COST report: a row exists (cost may be $0 on mock — the point is it is no longer EMPTY).
    with Session(engine) as s:
        cost = reports.run_report(
            s, reports.ReportQuery(kind="cost", group_by="none", customer_id=cid), user=None
        )
    assert cost["rows"] and cost["rows"][0]["jobs"] == 1, cost

    # And it shows in `job list` (via the same Job row) + web /reports parity: the web
    # surface reads the identical run_report seam, so the same row appears there.
    code, out, _ = run_cli(["--json", "job", "list"])
    payload = _json.loads(out.strip().splitlines()[-1])
    assert payload["total"] == 1 and payload["jobs"][0]["kind"] == "translate_document", payload
    print("  3 #16b reports: a CLI `doc translate` now shows in cost/tokens/volume + job list")


# ============================================================================
# 4. WEB/API arms stay scoped — the operator seam can't leak from a request
# ============================================================================
def test_web_api_arms_still_scoped():
    _reset_settings()
    _wipe()
    _auth_on()
    settings.set_many({"project_scoping": "1"})
    settings.invalidate_cache()
    try:
        # A non-privileged translator, and a job created by SOMEONE ELSE.
        tr = security.create_user("scoped_tr@x", "TR", Role.TRANSLATOR, "fixture-pass-1234!!")
        cid = _mk_customer("Scoped jobs")
        pid = _mk_project("Scoped jobs", cid, created_by=tr.email)
        did = _mk_source_doc(pid, "scoped.txt", ["hello"])
        _mk_job(
            "translate_document", "other_admin@x", payload={"document_id": did, "locale": "fr-FR"}
        )
        _mk_job(
            "translate_document", "scoped_tr@x", payload={"document_id": did, "locale": "fr-FR"}
        )
        with Session(engine) as s:
            tr_row = s.get(User, tr.id)

        # The translator (a REQUEST-path user, NO trusted_operator) sees ONLY its own
        # job — never the other admin's. The owner filter still confines it.
        rows, total = _hist(tr_row, type="job")
        assert total == 1 and rows[0].actor == "scoped_tr@x", (
            "a non-privileged web user must NOT see others' jobs",
            total,
            rows,
        )

        # user=None from a request path (no flag) sees NOTHING — the web semantic.
        _none_rows, none_total = _hist(None, type="job")
        assert none_total == 0, ("web user=None must be sees-nothing", none_total)

        # The trusted operator (CLI) DOES see both — proving the two arms diverge only
        # via the keyword-only flag no request path sets.
        _op, op_total = _hist(None, type="job", trusted_operator=True)
        assert op_total == 2, op_total
    finally:
        _reset_settings()
    print(
        "  4 web/api scoped: translator sees only own job; web user=None blind; only "
        "the CLI flag unlocks the full view"
    )


# ============================================================================
# 5. #25 — unknown report kind errors on the CLI AND the service
# ============================================================================
def test_unknown_report_kind_errors():
    _reset_settings()
    # Service: normalized() / validate_kind raise on an unknown kind (no silent 'cost').
    raised = False
    try:
        reports.ReportQuery(kind="token").normalized()  # the classic 'tokens' typo
    except reports.UnknownReportKind:
        raised = True
    assert raised, "reports.ReportQuery(kind='token').normalized() must raise UnknownReportKind"
    # A valid kind still normalizes fine.
    assert reports.ReportQuery(kind="tokens").normalized().kind == "tokens"
    # run_report itself refuses an unknown kind (defense in depth for direct callers).
    raised = False
    try:
        with Session(engine) as s:
            reports.run_report(s, reports.ReportQuery(kind="bogus"), user=None)
    except reports.UnknownReportKind:
        raised = True
    assert raised, "run_report must raise on an unknown kind"

    # CLI: `report run bogus` is rejected (argparse choices=) with a non-zero exit and
    # NO traceback; it must NOT run the cost report.
    code, out, err = run_cli(["report", "run", "bogus_kind"])
    assert code != 0, ("bad kind must be a non-zero exit", code, out)
    assert "Traceback" not in err, ("no traceback for a bad kind", err)
    # A valid kind runs (exit 0).
    code, out, _ = run_cli(["--json", "report", "run", "cost"])
    assert code == 0, (code, out)
    print("  5 #25 unknown kind: service raises UnknownReportKind; CLI rejects bogus, runs valid")


def main_suite():
    init_db()
    test_job_list_visible_post_init_inprocess()
    test_run_visible_include_runs_login_wall_on()
    test_report_reflects_cli_translation()
    test_web_api_arms_still_scoped()
    test_unknown_report_kind_errors()
    print("CLI_VISIBILITY OK")


if __name__ == "__main__":
    main_suite()
