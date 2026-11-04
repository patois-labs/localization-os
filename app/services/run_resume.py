"""Restart resume for workflow runs (S1 engine reliability, ADR 0090).

Before S1 a server restart killed every run that was mid-step and boot recovery marked it
FAILED ("Interrupted by a server restart."). Production deploys swap containers, so every
deploy failed the runs in flight. This module makes an interrupted run RESUME instead, from
the step it was on, without landing any completed step twice.

THREE PIECES

1. Checkpoint per step visit. Every step-completion write (the StepRun insert plus the
   run's cursor advance) also stamps the StepRun with `idempotency_key`
   ("<run>:<cursor>:<seq>", UNIQUE) and `checkpoint` (the run's resumable state right after
   the visit: next cursor, working texts, the run-local records a human-review pause also
   persists, the run context). Both ride in the SAME commit as the visit, so a crash leaves
   either the visit and its checkpoint or neither: a committed visit is never re-run.
   `VisitWriter` is the one helper every seam uses.

2. Owner + heartbeat. Each execution stamps `WorkflowRun.owner` with a token
   (`<host>:<pid>:<uuid8>/<nonce>`) and renews `heartbeat_at` about every 30 s from one
   daemon thread per process. Before a visit commits, the writer checks it still owns the
   run (a fresh read on its own connection); a run thread whose run was taken over stops
   without writing (the UNIQUE key is the backstop for the check-then-commit window).

3. Recovery. `plan()` decides per interrupted RUNNING/PENDING run that S1 code executed
   (it has an owner record; older rows keep the old fail rule): SKIP when it is live
   (a thread here, another live process on this host, or a fresh heartbeat from another
   host); RESUME when the newest StepRun carries a checkpoint whose cursor is the run's
   cursor (or nothing ran yet); otherwise FAIL as before (no checkpoint: a run from before
   this change, or a seam that writes none; an EXTERNAL step in flight, which would
   re-dispatch to a vendor; too many restart resumes in a row). RESUME re-queues the run as
   QUEUED with `run.context["_resume_kind"] = "restart"`; the admission scheduler starts it
   like any queued run and the run loop seeds its working texts from the checkpoint. PAUSED
   and QUEUED runs are never touched. A live sweep (`start_sweeper`) does the same for runs
   whose heartbeat went stale while this server is up (a crashed second process, an old
   container that died after a fast swap).

WHAT IS NOT IDEMPOTENT (accepted, ADR 0090): the visit that was IN FLIGHT at the crash
re-runs, so its provider calls are made again and billed again (the first calls really
happened). Completed visits are never re-run or re-billed. Finer-grained (per segment)
resume is S2 (durable step queue).

Documented in: docs/architecture/workflow-engine.md
"""

from __future__ import annotations

import logging
import os
import re
import socket
import threading
import time
import uuid
from datetime import datetime, timedelta

from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

log = logging.getLogger("loc_tms.run_resume")

#: How often the heartbeat thread renews `heartbeat_at` for runs executing in this process.
HEARTBEAT_SECONDS = 30.0
#: A heartbeat older than this means the owner is gone (three missed beats).
HEARTBEAT_STALE = timedelta(seconds=90)
#: How often the live sweep looks for runs with a stale heartbeat.
SWEEP_SECONDS = 60.0
#: Restart resumes allowed IN A ROW (no new checkpoint between them) before recovery fails the run
#: instead: a step that kills the process every time must not crash-loop a server forever.
MAX_RESTART_RESUMES = 3
#: The run.context marker recovery sets and the run loop consumes.
RESUME_KIND = "restart"
CHECKPOINT_VERSION = 1

_lock = threading.Lock()
#: (run id, thread ident) -> (owner token, executing thread) for executions in THIS process.
#: Keyed by thread too, so a successor in the same process never hides its predecessor's token.
_active: dict[tuple[int, int], tuple[str, threading.Thread]] = {}
#: Entries whose run was taken over (or ended): kept so the thread still knows its token, but
#: never heart-beaten again. Dropped when the thread dies.
_quiet: set[tuple[int, int]] = set()
_heartbeat_thread: threading.Thread | None = None
_sweeper_thread: threading.Thread | None = None


def _instance() -> str:
    from app.services import jobs

    return jobs.instance_id()


def new_token() -> str:
    """A fresh owner token for one execution of one run."""
    return f"{_instance()}/{uuid.uuid4().hex[:8]}"


def stamp(run, token: str) -> None:
    """Stage `run` as owned by `token` with a fresh heartbeat (the caller commits)."""
    run.owner = token
    run.heartbeat_at = datetime.utcnow()


def register(run_id: int | None, token: str) -> None:
    """Record that the CURRENT thread executes `run_id` as `token`, and make sure the
    heartbeat thread runs. Called right after the owner stamp commits."""
    if run_id is None:
        return
    th = threading.current_thread()
    key = (int(run_id), th.ident or 0)
    with _lock:
        _active[key] = (token, th)
        _quiet.discard(key)
    _ensure_heartbeat()


def held_by_this_thread(run_id: int | None) -> str | None:
    """The token the CURRENT thread holds for `run_id`, else None."""
    if run_id is None:
        return None
    th = threading.current_thread()
    with _lock:
        entry = _active.get((int(run_id), th.ident or 0))
    if entry and entry[1] is th:
        return entry[0]
    return None


#: The `_active` key slot of a run parked between step visits (S2 step hand-off, ADR 0092).
_PARKED_IDENT = -1


def park(run_id: int | None, holder) -> None:
    """Move the CURRENT thread's registration of `run_id` onto `holder` (an object with
    `is_alive()`), so a run waiting between step visits for its next slot keeps its heartbeat
    and still reads as live to the sweep. `adopt` moves it onto the thread that continues it."""
    if run_id is None:
        return
    th = threading.current_thread()
    with _lock:
        key = (int(run_id), th.ident or 0)
        entry = _active.pop(key, None)
        _quiet.discard(key)
        if entry is not None:
            _active[(int(run_id), _PARKED_IDENT)] = (entry[0], holder)


def adopt(run_id: int | None) -> None:
    """The CURRENT thread continues a parked run: it now holds the run's token."""
    if run_id is None:
        return
    th = threading.current_thread()
    with _lock:
        pkey = (int(run_id), _PARKED_IDENT)
        entry = _active.pop(pkey, None)
        quiet = pkey in _quiet
        _quiet.discard(pkey)
        if entry is not None:
            key = (int(run_id), th.ident or 0)
            _active[key] = (entry[0], th)
            if quiet:
                _quiet.add(key)
    _ensure_heartbeat()


def _committed_owner(run_id: int) -> tuple[bool, str | None]:
    """(row exists, owner) from COMMITTED state, read on a separate connection."""
    from sqlmodel import Session

    from app.db import engine

    with Session(engine) as s:
        row = s.execute(
            text("SELECT owner FROM workflowrun WHERE id = :id"), {"id": run_id}
        ).first()
    return (row is not None, row[0] if row is not None else None)


def still_owner(run_id: int, token: str | None) -> bool:
    """True while `token` still owns the run. A token of None (an execution that never
    stamped) is treated as owner, so callers outside the stamped seams behave as before."""
    if not token:
        return True
    exists, owner = _committed_owner(run_id)
    return exists and owner == token


def taken_over(run_id: int | None) -> bool:
    """True when the current thread executed `run_id` but another execution now owns it.
    Crash handlers check this before failing a run, so a superseded thread can never fail
    the run its successor is executing."""
    token = held_by_this_thread(run_id)
    if not token:
        return False
    try:
        exists, owner = _committed_owner(int(run_id))
    except Exception:
        return False
    return exists and owner is not None and owner != token


def _is_idempotency_conflict(exc: BaseException) -> bool:
    return isinstance(exc, IntegrityError) and "idempotency_key" in str(exc)


class VisitWriter:
    """The checkpoint + ownership seam every step-visit commit goes through.

    `snapshot` is a closure returning the run loop's resumable state (a dict); `stamp`
    puts the key and checkpoint on the visit's StepRun; `commit` refuses to write when the
    run was taken over (returns False after rolling back) and otherwise runs the seam's own
    commit callable. A UNIQUE-key conflict means a rival execution landed this very visit:
    also False, rolled back."""

    def __init__(self, run, token: str | None, seq: int, snapshot):
        self.run = run
        self.token = token
        self.seq = int(seq or 0)
        self._snapshot = snapshot

    def stamp(self, step_run, cursor: int, next_cursor: int, resumable: bool = True, **override):
        """Key the visit; attach the checkpoint unless the run state is not resumable here
        (the visit still gets its key, so it can never land twice)."""
        if step_run is None:
            return
        step_run.idempotency_key = f"{self.run.id}:{int(cursor)}:{self.seq}"
        if not resumable:
            step_run.checkpoint = None
            return
        state = dict(self._snapshot() or {})
        state.update(override)
        run_ctx = self.run.context
        step_run.checkpoint = {
            "v": CHECKPOINT_VERSION,
            "cursor": int(next_cursor),
            "seq": self.seq + 1,
            "resume_count": int(self.run.resume_count or 0),
            "run_context": dict(run_ctx) if isinstance(run_ctx, dict) else None,
            **state,
        }

    def commit(self, session, commit_fn) -> bool:
        if not still_owner(self.run.id, self.token):
            session.rollback()
            log.warning("run %s: taken over by another execution; this one stops", self.run.id)
            return False
        try:
            commit_fn()
        except IntegrityError as exc:
            if not _is_idempotency_conflict(exc):
                raise
            session.rollback()
            log.warning("run %s: this step visit already landed; this execution stops", self.run.id)
            return False
        self.seq += 1
        return True


def superseded_event(run_id) -> dict:
    return {
        "type": "superseded",
        "run_id": run_id,
        "error": "Another execution of this run took over (restart resume); this one stopped.",
    }


def initial_seq(session, run_id: int | None) -> int:
    """The visit sequence an execution starts from: the number of keyed visits so far."""
    if run_id is None:
        return 0
    row = session.execute(
        text(
            "SELECT COUNT(*) FROM steprun WHERE workflow_run_id = :id "
            "AND idempotency_key IS NOT NULL"
        ),
        {"id": run_id},
    ).first()
    return int(row[0] or 0) if row else 0


def latest_checkpoint(session, run_id: int) -> dict | None:
    """The checkpoint on the run's NEWEST StepRun, or None when the newest row has none
    (or the run has no StepRun at all)."""
    from sqlmodel import select

    from app.models import StepRun

    sr = session.exec(
        select(StepRun).where(StepRun.workflow_run_id == run_id).order_by(StepRun.id.desc())
    ).first()
    if sr is None or not isinstance(sr.checkpoint, dict):
        return None
    return dict(sr.checkpoint)


def has_step_runs(session, run_id: int) -> bool:
    row = session.execute(
        text("SELECT 1 FROM steprun WHERE workflow_run_id = :id LIMIT 1"), {"id": run_id}
    ).first()
    return row is not None


def clear_checkpoints(run_id: int | None) -> None:
    """Drop the checkpoint blobs of a run that ended (best effort, own connection)."""
    if run_id is None:
        return
    from sqlmodel import Session

    from app.db import engine

    try:
        with Session(engine) as s:
            s.execute(
                text(
                    "UPDATE steprun SET checkpoint = NULL WHERE workflow_run_id = :id "
                    "AND checkpoint IS NOT NULL AND EXISTS (SELECT 1 FROM workflowrun w "
                    "WHERE w.id = :id AND w.status IN ('completed', 'failed', 'cancelled'))"
                ),
                {"id": run_id},
            )
            s.commit()
    except Exception:
        log.debug("could not clear checkpoints of run %s", run_id, exc_info=True)


# --- liveness ---------------------------------------------------------------------------
_TOKEN_RE = re.compile(r"([^:/]+):([0-9]{1,10}):[0-9a-f]{8}/[0-9a-f]{8}")


def _pid_alive(pid: int) -> bool:
    from app.ports import jobqueue

    return jobqueue._pid_alive(pid)


def owner_liveness(owner: str | None, heartbeat_at, now: datetime, *, live_sweep: bool) -> str:
    """Classify a run's recorded owner: "live" (leave it alone), "dead" (reclaimable now),
    "own" (this process: the caller's thread check decides) or "unknown" (no owner or an
    unparsable one: the caller falls back to the activity window)."""
    m = _TOKEN_RE.fullmatch(owner or "")
    if m is None:
        return "unknown"
    host, pid_text = m.groups()
    pid = int(pid_text)
    fresh = heartbeat_at is not None and (now - heartbeat_at) < HEARTBEAT_STALE
    if host == socket.gethostname():
        if pid == os.getpid():
            if live_sweep:
                with _lock:
                    entry = next(
                        (e for e in _active.values() if e[0] == owner and e[1].is_alive()), None
                    )
                return "live" if entry is not None else ("live" if fresh else "dead")
            return "own"
        if not _pid_alive(pid):
            return "dead"
        # A live PID is the owner only while its heartbeat is recent: a reused PID (a later,
        # unrelated process) must not keep a dead run RUNNING forever. The window is the
        # activity grace, not HEARTBEAT_STALE, so a CLI process blocked on the database for a
        # few minutes is not mistaken for a dead one.
        from app.services.orchestrator_part2 import RECOVERY_ACTIVITY_GRACE

        recent = heartbeat_at is not None and (now - heartbeat_at) < RECOVERY_ACTIVITY_GRACE
        return "live" if recent else "dead"
    return "live" if fresh else "dead"


# --- recovery ---------------------------------------------------------------------------
def plan(session, run, *, alive_fn, now: datetime | None = None, live_sweep: bool = False):
    """("skip" | "resume" | "fail" | "legacy", detail) for one interrupted RUNNING/PENDING run.

    "legacy" = no usable owner record (the run was executed by code from before S1, so it
    has no checkpoints): the caller applies its pre-S1 rule unchanged (fail it)."""
    now = now or datetime.utcnow()
    if alive_fn(run.id):
        return "skip", "a thread in this process is executing it"
    who = owner_liveness(run.owner, run.heartbeat_at, now, live_sweep=live_sweep)
    if who == "live":
        return "skip", "its owner is still alive"
    if who == "unknown":
        return "legacy", "no owner recorded"
    return resumable(session, run)


def resumable(session, run):
    """("resume", checkpoint-or-None) or ("fail", why) for a run whose execution is gone."""
    from app.models import StepType
    from app.services.orchestrator_part2 import get_steps

    steps = get_steps(session, run.workflow_id)
    cursor = int(run.current_step_index or 0)
    if cursor < len(steps) and steps[cursor].step_type == StepType.EXTERNAL:
        return "fail", "an external (vendor) step was in flight"
    ck = latest_checkpoint(session, run.id)
    if ck is None:
        if cursor == 0 and not has_step_runs(session, run.id):
            if (run.resume_count or 0) >= MAX_RESTART_RESUMES:
                return "fail", f"resumed {run.resume_count} times in a row without progress"
            return "resume", None
        return "fail", "no checkpoint for the step it was on"
    if int(ck.get("cursor", -1)) != cursor:
        return "fail", "the checkpoint does not match the run's step"
    # The cap counts restart resumes IN A ROW: a checkpoint written after the last resume
    # records the resume count it ran under, so the difference is resumes without progress.
    in_a_row = (run.resume_count or 0) - int(ck.get("resume_count") or 0)
    if in_a_row >= MAX_RESTART_RESUMES:
        return "fail", f"resumed {in_a_row} times in a row without progress"
    return "resume", ck


def requeue(run, ck: dict | None) -> None:
    """Stage `run` as QUEUED for a restart resume (the caller commits and notifies the
    admission scheduler). Restores the run context captured with the checkpoint."""
    from app.models import RunStatus
    from app.services.orchestrator_part2 import RESTART_RESUME_NOTE, _stamp_note

    base = None
    if ck is not None:
        base = ck.get("ctx") if isinstance(ck.get("ctx"), dict) else ck.get("run_context")
    if not isinstance(base, dict):
        base = run.context if isinstance(run.context, dict) else {}
    ctx = dict(base)
    ctx["_resume_kind"] = RESUME_KIND
    run.context = ctx
    run.status = RunStatus.QUEUED
    run.owner = None
    run.heartbeat_at = None
    run.finished_at = None
    run.resume_count = (run.resume_count or 0) + 1
    _stamp_note(run, RESTART_RESUME_NOTE)


# --- heartbeat + live sweep ---------------------------------------------------------------
def _beat_once() -> None:
    from sqlmodel import Session

    from app.db import engine

    with _lock:
        entries = list(_active.items())
    if not entries:
        return
    now = datetime.utcnow()
    gone = []
    quiet = []
    with Session(engine) as s:
        for key, (token, thread) in entries:
            run_id = key[0]
            if not thread.is_alive():
                gone.append((key, token))
                continue
            if key in _quiet:
                continue
            res = s.execute(
                text(
                    "UPDATE workflowrun SET heartbeat_at = :now WHERE id = :id AND owner = :tok "
                    "AND status IN ('running', 'pending')"
                ),
                {"now": now, "id": run_id, "tok": token},
            )
            if res.rowcount == 0:
                quiet.append(key)  # taken over or ended: keep the token, stop beating
        s.commit()
    with _lock:
        for key in quiet:
            if key in _active:
                _quiet.add(key)
        for key, token in gone:  # the thread is dead: nothing can ask for its token again
            if _active.get(key, (None,))[0] == token:
                _active.pop(key, None)
                _quiet.discard(key)


def _heartbeat_loop() -> None:
    while True:
        time.sleep(HEARTBEAT_SECONDS)
        try:
            _beat_once()
        except Exception:
            log.warning("run heartbeat failed; retrying next beat", exc_info=True)


def _ensure_heartbeat() -> None:
    global _heartbeat_thread
    with _lock:
        if _heartbeat_thread is not None and _heartbeat_thread.is_alive():
            return
        _heartbeat_thread = threading.Thread(
            target=_heartbeat_loop, name="loc-run-heartbeat", daemon=True
        )
        _heartbeat_thread.start()


def sweep_stale_runs(now: datetime | None = None) -> int:
    """Re-queue RUNNING runs whose heartbeat went stale (their owner died) for a restart
    resume. Live-safe: a run renewed in the last HEARTBEAT_STALE is never touched. A plain
    run that cannot be resumed is failed with the restart note; a batch member that cannot
    is left to the batch healer at the next boot (or `db recover-jobs`), which also
    finalizes its batch; until then the member reads RUNNING. Returns the number
    re-queued."""
    from sqlmodel import Session, select

    from app.db import engine
    from app.models import RunStatus, WorkflowRun
    from app.services import orchestrator_part4

    now = now or datetime.utcnow()
    cutoff = now - HEARTBEAT_STALE
    n = 0
    with Session(engine) as s:
        rows = s.exec(
            select(WorkflowRun).where(
                WorkflowRun.status == RunStatus.RUNNING,
                WorkflowRun.finished_at.is_(None),
                WorkflowRun.heartbeat_at.is_not(None),
                WorkflowRun.heartbeat_at < cutoff,
            )
        ).all()
        for run in rows:
            verdict, detail = plan(
                s, run, alive_fn=orchestrator_part4._run_thread_alive, now=now, live_sweep=True
            )
            if verdict not in ("resume", "fail"):
                continue
            if verdict == "fail" and run.batch_id is not None:
                continue  # the batch healer fails a batch member and finalizes its batch
            # Compare-and-set on the stale owner and heartbeat, so two sweepers (or a run that
            # renewed just now) never both act on the row.
            res = s.execute(
                text(
                    "UPDATE workflowrun SET owner = NULL WHERE id = :id AND status = 'running' "
                    "AND owner = :owner AND heartbeat_at < :cutoff"
                ),
                {"id": run.id, "owner": run.owner, "cutoff": cutoff},
            )
            if res.rowcount != 1:
                s.rollback()
                continue
            if verdict == "fail":
                from app.services.orchestrator_part2 import RESTART_NOTE, _stamp_note

                run.status = RunStatus.FAILED
                run.finished_at = datetime.utcnow()
                _stamp_note(run, RESTART_NOTE)
                s.add(run)
                s.commit()
                log.warning("run %s: owner gone and not resumable (%s); failed", run.id, detail)
                continue
            requeue(run, detail)
            s.add(run)
            s.commit()
            n += 1
            log.warning("run %s: owner heartbeat stale; re-queued to resume", run.id)
    if n:
        from app.services import orchestrator

        orchestrator.ADMISSION.ensure_started()
        orchestrator.ADMISSION.notify()
    return n


def _sweep_jobs() -> int:
    """The durable job queue's live sweep (lease-only, `recover(at_boot=False)`): a job whose
    worker died stops renewing its lease and is handed back once the lease expires. A no-op
    on the in-memory queue (nothing is armed)."""
    from app.ports import jobqueue

    if not jobqueue.DB_BACKEND_ARMED:
        return 0
    from app.services import jobs

    return jobs.recover_interrupted(at_boot=False)


def _sweep_loop() -> None:
    while True:
        time.sleep(SWEEP_SECONDS)
        try:
            sweep_stale_runs()
        except Exception:
            log.warning("stale-run sweep failed; retrying next pass", exc_info=True)
        try:
            n = _sweep_jobs()
            if n:
                log.warning("job sweep: %s job(s) with an expired lease handed back", n)
        except Exception:
            log.warning("expired-lease job sweep failed; retrying next pass", exc_info=True)


def start_sweeper() -> None:
    """Start the live engine sweep (idempotent; called from the boot reconcile): every
    SWEEP_SECONDS it re-queues runs whose owner's heartbeat went stale and, on the durable
    job queue, hands back jobs whose lease expired."""
    global _sweeper_thread
    with _lock:
        if _sweeper_thread is not None and _sweeper_thread.is_alive():
            return
        _sweeper_thread = threading.Thread(
            target=_sweep_loop, name="loc-run-resume-sweep", daemon=True
        )
        _sweeper_thread.start()


def pending_step_run(session):
    """The StepRun this seam just added to `session` (the newest pending one)."""
    from app.models import StepRun

    rows = [o for o in session.new if isinstance(o, StepRun)]
    return rows[-1] if rows else None


def fail_unresumable(session, run) -> dict:
    """Fail a run that was re-queued for a restart resume but whose checkpoint no longer
    fits the document (the run loop found it unusable). Commits; returns the error event."""
    from app.models import RunStatus
    from app.services.orchestrator_part2 import RESTART_NOTE, _stamp_note

    run.status = RunStatus.FAILED
    run.finished_at = datetime.utcnow()
    _stamp_note(run, RESTART_NOTE)
    session.add(run)
    session.commit()
    return {
        "type": "error",
        "run_id": run.id,
        "error": "Interrupted by a server restart; its checkpoint could not be resumed.",
    }
