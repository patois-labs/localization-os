"""#578: perf and run-state follow-ups from the perf-orchestrator gate. Hermetic + offline.

  [a] client_ip     the audit middleware's security.client_ip does no blocking work: no DNS, no
                    file read, and on a warm settings cache no database query at all (the
                    periodic cache probe is shared by every settings.get in the process).
                    Documented as cheap; this pins it so it cannot grow a lookup unnoticed.
  [b] cancel note   a note another session stamps on the run (a cancel) while a step-write
                    lock-retry sleeps is kept: the retry re-applies the step's own additions
                    ON TOP of what the database holds instead of overwriting `notes`.
  [c] resume race   a resume in ANOTHER process wins the PAUSED -> RUNNING flip between this
                    process's PAUSED check and its UPDATE (the per-process _RUN_CREATE_LOCK
                    cannot see it): the flip is one conditional UPDATE ... WHERE status =
                    'paused', so the loser is refused, runs nothing and advances nothing. Both
                    twins: resume_workflow (CLI) and resume_run_async (web / API).

Final line: "verify_run_state_578 OK".
"""

import os
import socket
import sys
import time
from unittest import mock

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(_ROOT)
sys.path.insert(0, _ROOT)
from tests._bootstrap import init_env

init_env("loctms_runstate578_")

from sqlalchemy import event, text
from sqlalchemy.exc import OperationalError
from sqlmodel import Session
from starlette.datastructures import Headers

from app import db as _db
from app.db import engine, init_db
from app.models import (
    Document,
    Project,
    RunStatus,
    Segment,
    SegStatus,
    StepType,
    TargetLocale,
    TransMode,
    Workflow,
    WorkflowRun,
    WorkflowStep,
)
from app.services import orchestrator, security, settings

init_db()
settings.set_many({"security_auth_required": "0", "multi_org_enabled": "0"})
settings.invalidate_cache()

_FAILS = []
_N = [0]


def _check(name, ok, detail=""):
    print(("PASS " if ok else "FAIL ") + name + (f"  {detail}" if detail else ""))
    if not ok:
        _FAILS.append(name)


def _seq():
    _N[0] += 1
    return _N[0]


def _mk_run(status=RunStatus.RUNNING, notes=None, step_index=0):
    with Session(engine) as s:
        p = Project(name=f"RS578-{_seq()}", source_locale="en-US", default_provider="mock")
        s.add(p)
        s.commit()
        s.refresh(p)
        s.add(TargetLocale(project_id=p.id, locale="fr-FR"))
        d = Document(project_id=p.id, filename="d.txt", mode=TransMode.SEGMENTED)
        s.add(d)
        wf = Workflow(name=f"RS578WF-{_seq()}", scope="global")
        s.add(wf)
        s.commit()
        s.refresh(d)
        s.refresh(wf)
        run = WorkflowRun(
            workflow_id=wf.id,
            document_id=d.id,
            target_locale="fr-FR",
            status=status,
            notes=notes,
            current_step_index=step_index,
        )
        s.add(run)
        s.commit()
        s.refresh(run)
        return run.id


# =============================================================================================
# [a] client_ip does no blocking work
# =============================================================================================
class _Peer:
    host = "10.1.2.3"


class _Req:
    client = _Peer()
    headers = Headers({"x-forwarded-for": "198.51.100.7"})


def _no_net(*a, **kw):
    raise AssertionError("client_ip must not resolve names")


queries = []


def _count(conn, cursor, statement, *a, **kw):
    queries.append(statement)


security.client_ip(_Req())  # warm the settings cache
settings.get("security_auth_required")
event.listen(engine, "before_cursor_execute", _count)
try:
    with (
        mock.patch.object(socket, "getaddrinfo", _no_net),
        mock.patch.object(socket, "gethostbyname", _no_net),
        mock.patch("builtins.open", side_effect=AssertionError("client_ip must not read files")),
    ):
        # a fresh probe window, so the (shared) periodic cache probe cannot land in the loop
        import app.services.settings as _settings_mod

        _settings_mod._checked_at = time.monotonic()
        t0 = time.perf_counter()
        for _ in range(500):
            security.client_ip(_Req())
        per_call = (time.perf_counter() - t0) / 500
finally:
    event.remove(engine, "before_cursor_execute", _count)
_check("[a] no DNS, no file read, no query on a warm cache", not queries, str(queries[:2]))
_check(
    "[a] a warm call costs well under a millisecond", per_call < 0.001, f"{per_call * 1e6:.1f}us"
)

# =============================================================================================
# [b] a note stamped during a lock-retry sleep survives the retry
# =============================================================================================
CANCEL = "stop: cancelled by operator"
rid = _mk_run(notes="pool note")
s1 = Session(engine)
run = s1.get(WorkflowRun, rid)
run.notes = "pool note step note"  # what the step staged, appended to the committed note
attrs = orchestrator._pending_run_attrs(run)
calls = {"n": 0}
real_commit = s1.commit


def _stage():
    s1.add(run)
    orchestrator._reapply_run_attrs(run, attrs)
    run.current_step_index = 2
    s1.add(run)


def _flaky_commit():
    calls["n"] += 1
    if calls["n"] == 1:
        # another session cancels the run while this one would sleep before retrying
        with Session(engine) as s2:
            other = s2.get(WorkflowRun, rid)
            other.notes = (other.notes + " " + CANCEL).strip()
            other.status = RunStatus.CANCELLED
            s2.commit()
        raise OperationalError("COMMIT", {}, Exception("database is locked"))
    return real_commit()


s1.commit = _flaky_commit
_db.commit_with_retry(s1, stage=_stage)
s1.close()
with Session(engine) as s:
    after = s.get(WorkflowRun, rid)
    got_notes, got_status, got_cursor = after.notes, after.status, after.current_step_index
_check("[b] the retry committed once more", calls["n"] == 2, str(calls["n"]))
_check("[b] the cancel note is kept", CANCEL in (got_notes or ""), str(got_notes))
_check("[b] the step's own note is kept", "step note" in (got_notes or ""), str(got_notes))
_check("[b] the earlier committed note is kept", "pool note" in (got_notes or ""), str(got_notes))
_check("[b] the cancel status is not clobbered", got_status == RunStatus.CANCELLED, str(got_status))
_check("[b] the step cursor advanced", got_cursor == 2, str(got_cursor))
# the unraced shape is byte-identical: nothing foreign in the database -> exactly the staged text
try:
    _merged_ok = (
        orchestrator._merge_staged_notes("a", "a b", "a") == "a b"
        and orchestrator._merge_staged_notes(None, "x", None) == "x"
        and orchestrator._merge_staged_notes("a", "a b", "a c") == "a c b"
    )
except AttributeError:  # the tree before the fix has no merge helper
    _merged_ok = False
_check("[b] no foreign write: the staged text is committed unchanged", _merged_ok)

# =============================================================================================
# [c] resume race against another process
# =============================================================================================


def _paused_run():
    pid_run = _mk_run(status=RunStatus.PAUSED, step_index=1)
    with Session(engine) as s:
        r = s.get(WorkflowRun, pid_run)
        wf = s.get(Workflow, r.workflow_id)
        for i, (name, st) in enumerate(
            (("T", StepType.LLM_AGENT), ("H", StepType.HUMAN_REVIEW), ("T2", StepType.LLM_AGENT))
        ):
            s.add(
                WorkflowStep(
                    workflow_id=wf.id,
                    order_index=i,
                    name=name,
                    step_type=st,
                    provider="mock",
                    prompt_text="x {text}",
                    chunk_policy="auto",
                    retry_max=1,
                    backoff_base=0.0,
                )
            )
        s.add(
            Segment(
                document_id=r.document_id,
                target_locale="fr-FR",
                order_index=0,
                source_text="Alpha one.",
                status=SegStatus.NEW,
            )
        )
        s.commit()
    return pid_run


real_commit_launch = _db.commit_launch


def _rival_wins_first(session, **kw):
    """Another PROCESS resumes the run after this one read PAUSED and before its UPDATE."""
    with Session(engine) as other:
        # plain SQL, not the production helper: the rival is another PROCESS, and this must
        # also run (and fail) against the tree before the fix
        res = other.execute(
            text(
                "UPDATE workflowrun SET status = :run, current_step_index = current_step_index + 1"
                " WHERE id = :id AND status = :paused"
            ),
            {"run": RunStatus.RUNNING, "paused": RunStatus.PAUSED, "id": state["run_id"]},
        )
        won = res.rowcount == 1
        other.commit()
    state["rival_won"] = won
    return real_commit_launch(session, **kw)


def _stream_factory(executed, run_id):
    def stream(*a, **kw):
        executed.append("stream")
        yield {"type": "done", "run_id": run_id, "text": "x"}

    return stream


for label in ("resume_workflow (CLI)", "resume_run_async (web / API)"):
    run_id = _paused_run()
    state = {"run_id": run_id, "rival_won": None}
    executed = []

    no_stream = _stream_factory(executed, run_id)
    with (
        mock.patch.object(orchestrator._db, "commit_launch", _rival_wins_first),
        mock.patch.object(orchestrator, "run_workflow_stream", no_stream),
        mock.patch.object(
            orchestrator, "_run_in_background", lambda *a, _e=executed, **kw: _e.append("thread")
        ),
        Session(engine) as s,
    ):
        if label.startswith("resume_workflow"):
            out = orchestrator.resume_workflow(s, run_id, "fr-FR", "")
            refused = out.get("status") == "rejected"
        else:
            out = orchestrator.resume_run_async(s, run_id, "fr-FR", "")
            refused = out is False
    with Session(engine) as s:
        final = s.get(WorkflowRun, run_id)
        cursor, status = final.current_step_index, final.status
    _check(f"[c] {label}: the rival won the flip", state["rival_won"] is True)
    _check(f"[c] {label}: the loser is refused", refused, str(out))
    _check(f"[c] {label}: nothing executed", not executed, str(executed))
    _check(f"[c] {label}: the cursor advanced exactly once", cursor == 2, str(cursor))
    _check(f"[c] {label}: the run is RUNNING (the rival's)", status == RunStatus.RUNNING, status)

# and an uncontended resume still works end to end
run_id = _paused_run()
executed = []


def _ok_stream(*a, **kw):
    executed.append("stream")
    yield {"type": "done", "run_id": run_id, "text": "x"}


with mock.patch.object(orchestrator, "run_workflow_stream", _ok_stream), Session(engine) as s:
    out = orchestrator.resume_workflow(s, run_id, "fr-FR", "")
with Session(engine) as s:
    cursor = s.get(WorkflowRun, run_id).current_step_index
_check(
    "[c] an uncontended resume runs once and advances once",
    out.get("status") == "completed" and executed == ["stream"] and cursor == 2,
    f"{out.get('status')} {executed} {cursor}",
)

if _FAILS:
    print("FAILED:", "; ".join(_FAILS))
    sys.exit(1)
print("verify_run_state_578 OK")
