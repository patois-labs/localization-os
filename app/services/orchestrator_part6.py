"""orchestrator: part6

Documented in: docs/architecture/translation-pipeline.md
"""

import logging
import threading
import time
from datetime import datetime

from sqlalchemy import update as sa_update
from sqlmodel import Session, select

from app.models import (
    RunStatus,
    StepRun,
    StepType,
    WorkflowRun,
    WorkflowStep,
)
from app.services.orchestrator_part2 import (
    RECOVERY_ACTIVITY_GRACE,
    RESTART_NOTE,
    _bookkeeping_session,
    _last_activity_at,
    _stamp_note,
)
from app.services.orchestrator_part3 import _standing_gate_verdicts
from app.services.orchestrator_part4 import (
    _CANCELLABLE_RUN_STATUSES,
    CANCEL_NOTE,
    RunCancelError,
    RunGuard,
    _run_thread_alive,
    _write_progress_row,
)
from app.services.orchestrator_part5 import _branch_cursor, _record_gate_hold, _run_gate_outcome
from app.services.orchestrator_terminology_backen import (
    _DEFAULT_VERDICT_SCOPE,
    GATE_HOLD_UNREPAIRED,
    _DeletedGateStep,
    _gate_hold_of,
    _verdict_family,
)

log = logging.getLogger("loc_tms.orchestrator")


_DELETED_GATE_STEP = _DeletedGateStep()


def _completed_text_steps(session: Session, run_id: int, by_id: dict) -> list:
    """`(graph index, StepRun.id)` of every text-MODIFYING step visit that actually RAN to
    completion in this run (#156), oldest first. An `llm_agent` step is the only kind that
    rewrites the working text; a step whose `run_condition` guard skipped it records SKIPPED
    and changed nothing, which is why the status is checked and not merely the presence of a
    StepRun row.

    Graph indexes alone cannot say WHEN a repair ran, and a loop revisits the graph: a repair
    from an earlier pass sits at the same index as one that would have repaired a later
    failure. #505 item 5: the StepRun id (monotonic per run) is returned beside the index so
    the caller can ask the question that matters: did a repair land AFTER the verdict that
    failed and BEFORE the verdict that later passed."""
    rows = session.exec(
        select(StepRun.step_id, StepRun.id)
        .join(WorkflowStep, WorkflowStep.id == StepRun.step_id, isouter=True)
        .where(
            StepRun.workflow_run_id == run_id,
            StepRun.status == RunStatus.COMPLETED,
            WorkflowStep.step_type == StepType.LLM_AGENT,
        )
        .order_by(StepRun.id)
    ).all()
    return [(by_id[sid][0], srid) for sid, srid in rows if sid in by_id]


def _standing_verdict_ids(session: Session, run_id: int) -> dict:
    """`{step_id: StepRun.id}` of the verdict row `_standing_gate_verdicts` reports for each
    document-level check (same filter, same ordering): WHEN each standing verdict was
    written, so a supersede decision can compare timing instead of graph position only."""
    rows = session.exec(
        select(StepRun.step_id, StepRun.id, WorkflowStep.step_type, WorkflowStep.qe_gate)
        .join(WorkflowStep, WorkflowStep.id == StepRun.step_id, isouter=True)
        .where(
            StepRun.workflow_run_id == run_id,
            StepRun.gate.in_(("passed", "failed")),
        )
        .order_by(StepRun.id)
    ).all()
    out: dict = {}
    for step_id, srid, step_type, qe_gate in rows:
        if _verdict_family(step_type, qe_gate) in _DEFAULT_VERDICT_SCOPE:
            out[step_id] = srid
    return out


def _terminal_gate_hold(session: Session, run, steps) -> dict | None:
    """THE POSTURE, decided ONCE at the end of the run (gate C round 2, M2 -- ratified).

    Returns the run's gate hold, recording an `unrepaired` one first if the run is ending
    with a gate's STANDING VERDICT still FAILED and no hold has been recorded yet.

    Round 1 held only the three endings that have a correction LOOP (the carried FAIL, the
    exhausted loop-back, the capped check). The endings with no loop shipped their text at
    200 while the run read `completed`: `on_fail: "end"` -- the most explicit way an author
    can say "if the check fails, stop" -- plus `on_fail: "next"`, a branch-less check and a
    dangling loop-back target. The harm keys on the standing VERDICT; only the round-1
    posture keyed on the loop's SHAPE.

    WHY HERE AND NOT AT THE BRANCH SEAM. Stopping the cursor at the failing gate would
    delete the canonical Phase-2 correction shape (SPEC §4.1): a branch-less check followed
    by a `run_condition: prev_lqa:failed` fixer, which is precisely a repair the author
    wrote by hand. Routing stays exactly as §5.4 always said; what changed is that the run
    does not DELIVER text its own check is still failing. Both write-backs consult this, so
    the segmented and FULL_TEXT paths cannot drift.

    Silent (returns None) whenever every gate passed, or no gate ran at all -- so a plain
    workflow and a healthy correction loop are byte-identical to before."""
    existing = _gate_hold_of(run)
    if existing is not None:
        return existing
    standing = _standing_gate_verdicts(session, run.id)
    failed_ids = [sid for sid, g in standing.items() if g == "failed"]
    if not failed_ids:
        return None
    by_id = {getattr(st, "id", None): (i, st) for i, st in enumerate(steps or [])}
    # #156: DROP THE FAILURES A LATER REPAIR SUPERSEDED.
    #
    # `_standing_gate_verdicts` says "a loop that failed and then passed stands at passed",
    # and that is true of the ONE-CHECK loop it was written for, where the same check is the
    # loop-back target and re-runs. The shape the app ships by default has TWO checks:
    # `lqa2` loops back to `fix`, so `lqa1` never re-runs and stands `failed` for the life of
    # the run -- while its verdict is about text the fix step has since REPLACED. Held on
    # that, a correction loop doing exactly what it exists to do (fail, repair, pass) was
    # undeliverable forever, and the refusal said there was "no correction loop"
    # (prod run #208, workflow #61).
    #
    # A failure is superseded only when BOTH are true, later in THIS run:
    #   * a text-modifying step actually RAN (an llm_agent step that COMPLETED -- a step
    #     whose run_condition skipped it writes SKIPPED and repairs nothing), and
    #   * a gate AFTER that step passed.
    # The second half alone would let any unrelated later check clear a failure nothing
    # tried to repair; the first half alone would clear it with no one re-judging the
    # repair. Both together mean exactly: the text this gate judged is gone, and what
    # replaced it was checked and passed.
    #
    # #505 item 5: graph position is necessary but not sufficient. A loop re-enters the graph,
    # so a repair that ran in an EARLIER pass still sits "between" a check and a later one by
    # index even when the check's LATEST failing verdict was written after it, and the later
    # check's pass predates that failure. A failure is therefore superseded only when the
    # repair is also AFTER the failing verdict and BEFORE the passing verdict in time (the
    # StepRun ids, monotonic within a run).
    _verdict_at = _standing_verdict_ids(session, run.id)
    _passed = sorted(
        (by_id[sid][0], _verdict_at.get(sid, -1))
        for sid, g in standing.items()
        if g == "passed" and sid in by_id
    )
    if _passed:
        _repairs = _completed_text_steps(session, run.id, by_id)
        failed_ids = [
            sid
            for sid in failed_ids
            if not (
                sid in by_id
                and any(
                    by_id[sid][0] < r_idx < p_idx and _verdict_at.get(sid, -1) < r_id < p_id
                    for r_idx, r_id in _repairs
                    for p_idx, p_id in _passed
                )
            )
        ]
        if not failed_ids:
            return None
    # The LAST failing gate in graph order is the one that stopped this run being clean.
    named = sorted((by_id[sid] for sid in failed_ids if sid in by_id), key=lambda p: p[0])
    if named:
        idx, step = named[-1]
    else:
        # FAIL CLOSED, not open: the failing gate's step row is gone from the graph (it was
        # deleted while the run was executing). We still know a gate FAILED this text, so
        # the hold stands -- it just cannot be named beyond "a quality check". Returning
        # None here would have made "delete the step mid-run" a way past the hold.
        idx, step = -1, _DELETED_GATE_STEP
    _record_gate_hold(session, run, step=step, step_index=idx, cause=GATE_HOLD_UNREPAIRED)
    return _gate_hold_of(run)


def _may_clear_gate_hold(session: Session, run, scope: frozenset | None = None) -> bool:
    """True when THIS run has earned the right to clear a standing gate-hold badge off the
    units it produced: it recorded no hold of its own AND its own gate PASSED. See
    `_run_gate_outcome` for the rule and for what round 1 got wrong (H1).

    The RUN-LEVEL half only. The per-unit half -- the check must have passed over the text
    the write-back is about to commit -- is `_gate_release_mask` (round 3, M1)."""
    return _gate_hold_of(run) is None and _run_gate_outcome(session, run.id, scope) == "passed"


def _next_cursor(
    step: WorkflowStep,
    cursor: int,
    gate: str | None = None,
    key_index: dict | None = None,
    steps=None,
    guard: "RunGuard | None" = None,
) -> int:
    """`_branch_cursor`'s cursor half, for the callers that can never produce a hold: a
    plain producer step's linear advance (no gate), and the `no_progress` carried-PASS
    site (a PASS never holds). `_branch_cursor` returns a non-None hold cause on exactly
    ONE route -- a FAILED gate whose `on_fail` loop-back target sits at its ceiling -- so
    the discarded half is always None for these two callers. Never call this with a FAILED
    gate, or that D4 hold would be silently dropped."""
    return _branch_cursor(step, cursor, gate=gate, key_index=key_index, steps=steps, guard=guard)[0]


PROGRESS_MIN_INTERVAL_S = 5.0


_progress_state: dict = {}


_progress_lock = threading.Lock()


_progress_dirty: set = set()


_progress_wake = threading.Event()


_progress_idle = threading.Event()


_progress_writer_lock = threading.Lock()


_progress_writer_thread: threading.Thread | None = None


PROGRESS_WRITER_THREAD_NAME = "loc-progress-writer"


#: How often a long step may PERSIST its progress counter to the run row (GitHub #101).
#:
#: The counter itself moves in memory on EVERY unit -- that costs a dict update under a
#: short lock and nothing else. Persisting is the expensive half: one UPDATE on its own
#: connection, i.e. one more checkout from the SQLAlchemy pool and one more acquisition of
#: SQLite's single writer lock, on an install where pool exhaustion under 4-6 concurrent
#: runs is itself a HIGH bug (#100). So a 258-segment step persists a handful of times
#: rather than 258. Five seconds is far finer than the 10-25 minutes of silence #101 is
#: about and coarse enough to be invisible next to the provider calls it reports on.
#: run_id -> {"done", "total", "unit", "step", "step_index", "at", "flushed_done",
#: "flushed_at", "attempt_at"}: the live counter for every step THIS process is executing,
#: and the throttle state for its persists. THE THROTTLE IS `attempt_at` + `flushed_done`
#: ALONE (gate F round 2, L9): `flushed_at` is stamped by `_persist_progress` after a write
#: actually lands and is read by nothing -- it is kept deliberately, as the only in-process
#: record of when this run's counter was last really written, for a debugger looking at a
#: run whose telemetry went quiet under pool exhaustion.
#:
#: Dropped by `_progress_forget` when a step's ticker finishes and again in
#: `run_workflow_stream`'s finally (every launcher funnels through that seam), so no
#: executor path can leak an entry into a long-lived server.
#: THE PROGRESS PERSIST NEVER RUNS ON A TRANSLATION THREAD (gate F round 1, H1).
#:
#: The first cut opened `Session(engine)` inline on the run's own thread. That is a
#: QueuePool checkout with SQLAlchemy's default `pool_timeout=30`, so under the exhaustion
#: of the sibling HIGH (#100 -- 4-6 concurrent runs) EVERY flush blocked the translation
#: thread for a full 30 s and then swallowed a TimeoutError at debug: in the segmented
#: collection loop, once per collected future. A telemetry counter added to prove a run is
#: alive would have been the thing that stopped it moving.
#:
#: So the run's thread now only ever (a) updates a dict and (b) sets an Event. One
#: long-lived daemon thread does the writing, coalescing per run id (last value wins --
#: telemetry is idempotent), and it SKIPS the write outright when the pool has no free slot
#: (`_progress_pool_has_slot`) rather than queueing behind translations for it. At a step
#: boundary the ticker does not use this path at all: it stamps the columns on the run's OWN
#: already-open ORM object so the final count rides the step's own commit, with no second
#: connection anywhere.
def _progress_snapshot(run_id) -> dict | None:
    """The in-memory counter for `run_id`, or None. `at` is a datetime (not yet isoformat)."""
    if not run_id:
        return None
    with _progress_lock:
        st = _progress_state.get(run_id)
        if not st:
            return None
        return {
            "done": st["done"],
            "total": st["total"],
            "unit": st["unit"],
            "step": st["step"],
            "step_index": st["step_index"],
            "at": st["at"],
        }


def _persist_progress(run_id) -> None:
    """One coalesced persist for `run_id`, on the writer thread. `flushed_*` is stamped
    ONLY after the row was actually written, so a skipped write is retried on the next
    tick instead of being silently marked done."""
    with _progress_lock:
        st = _progress_state.get(run_id)
        if st is None:
            return
        payload = (st["done"], st["total"], st["unit"], st["at"], st["step"], st["step_index"])
    if not _write_progress_row(run_id, *payload):
        return
    with _progress_lock:
        st = _progress_state.get(run_id)
        if st is not None:
            st["flushed_done"] = payload[0]
            st["flushed_at"] = time.monotonic()


def _progress_writer_loop() -> None:
    """Drain the dirty set forever. Daemon: it owns nothing an interpreter exit must wait
    for, and every write it makes is telemetry."""
    while True:
        try:
            _progress_wake.wait()
            _progress_wake.clear()
            while True:
                with _progress_lock:
                    ids = list(_progress_dirty)
                    _progress_dirty.clear()
                if not ids:
                    break
                for rid in ids:
                    _persist_progress(rid)
            if not _progress_wake.is_set():
                _progress_idle.set()
        except Exception:  # pragma: no cover - the thread must outlive any single failure
            log.debug("run progress writer iteration failed", exc_info=True)


def _progress_writer_start() -> None:
    """Start the single writer thread on first use (never at import: a CLI that runs no
    workflow should not grow a thread)."""
    global _progress_writer_thread
    t = _progress_writer_thread
    if t is not None and t.is_alive():
        return
    with _progress_writer_lock:
        t = _progress_writer_thread
        if t is not None and t.is_alive():
            return
        t = threading.Thread(
            target=_progress_writer_loop, name=PROGRESS_WRITER_THREAD_NAME, daemon=True
        )
        _progress_writer_thread = t
        t.start()


def recover_interrupted_runs() -> int:
    """Startup heal for PLAIN single runs -- the gap `recover_interrupted_batches`
    documents but deliberately does not close (it is scoped to WorkflowBatch rows and
    THEIR members, so `batch_id IS NULL` runs were never touched).

    THE BUG THIS CLOSES (GitHub #44, reproduced hermetically and observed in prod on
    run #74). A plain run executes on a daemon thread. A server restart -- a Fly
    redeploy, a machine auto-stop, a local `run.py` bounce -- kills that thread with
    the WorkflowRun row still RUNNING and finished_at NULL. Nothing ever revisits it.
    Worse than a cosmetic zombie: `start_run`'s double-submit guard re-attaches to any
    RUNNING/PAUSED/PENDING run for the same (workflow, document, locale), so every
    later attempt at that locale returns the corpse's id and reports success while
    translating nothing, forever.

    Fails orphaned rows (stamping RESTART_NOTE so the run page can say why) and leaves
    PAUSED ALONE -- a paused run is durable HUMAN state waiting on a reviewer, not an
    orphan, exactly as the batch healer treats a paused member.

    AND LEAVES QUEUED ALONE (gate A round 3, HIGH-3), which is why the status is distinct
    from PENDING rather than reusing it. A QUEUED row is a fan-out launch that has not been
    given a slot yet: it never had a thread, so a restart cost it nothing, and failing it
    here would turn "your 72-locale translate all survived the redeploy" into 64 failed runs.
    `adopt_queued_runs` (called from the same startup reconcile) simply picks them up again.

    TWO GUARDS ON "IS IT REALLY ORPHANED", because the job and batch healers'
    founding assumption -- single process, nothing is executing at startup -- is only
    true of THIS process (review F4):

      1. `_run_thread_alive`: never fail a run this interpreter is executing. At
         boot there are none, so this is inert there; it is what makes the same
         function safe to expose as `loc-tms db recover-jobs` on a LIVE server.
      2. RECOVERY_ACTIVITY_GRACE: never fail a run that showed activity in the last
         ten minutes. This is the cross-process guard. A synchronous CLI
         `workflow run` against the same DB keeps executing while the server
         reboots, and is invisible to guard 1; it stamps a StepRun per step, so it
         reads as active and is left alone. A true boot corpse's newest StepRun is
         as old as the crash.

    RESIDUAL, ACCEPTED AND DOCUMENTED: a second-process run sitting inside ONE
    long LLM call for more than ten minutes writes no StepRun in that window, so a
    server booting at exactly that moment would still fail its row. The run's own
    thread then finds the row failed and aborts its write-back rather than
    resurrecting it (see `_abort_if_superseded`), so the outcome is an honest failed
    run, never a corrupted one. Closing it properly needs a heartbeat column; that is
    not this change.

    Returns the number of runs healed. Also reachable from the CLI as
    `loc-tms db recover-jobs` (web+CLI parity), alongside the other two healers."""
    touched = 0
    cutoff = datetime.utcnow() - RECOVERY_ACTIVITY_GRACE
    with _bookkeeping_session() as s:
        runs = s.exec(
            select(WorkflowRun).where(
                WorkflowRun.status.in_((RunStatus.RUNNING, RunStatus.PENDING)),
                WorkflowRun.batch_id.is_(None),
                WorkflowRun.finished_at.is_(None),
            )
        ).all()
        for run in runs:
            if _run_thread_alive(run.id):
                continue  # this process is executing it right now
            seen = _last_activity_at(s, run)
            if seen is not None and seen > cutoff:
                continue  # something wrote for it very recently
            run.status = RunStatus.FAILED
            run.finished_at = datetime.utcnow()
            _stamp_note(run, RESTART_NOTE)
            s.add(run)
            touched += 1
        s.commit()
    return touched


def cancel_run(session: Session, run_id: int, *, actor: str = "") -> dict:
    """ABORT an in-flight run (PENDING/QUEUED/RUNNING/PAUSED -> CANCELLED). The operator escape
    hatch (AUDIT BUG-A205): a run stuck PAUSED at a human_review or external-vendor step
    can be terminated, which ALSO unblocks re-authoring — a CANCELLED run does not count
    as in-flight (workflows._ACTIVE_RUN_STATUSES), so move/edit/delete/add-step stop being
    refused. This is the real action the mid-flight guards' "cancel them, then ..." has
    always pointed at.

    Terminal + non-resumable: resume_run_async / resume_workflow refuse a non-PAUSED run,
    and start_run only re-attaches to PENDING/QUEUED/RUNNING/PAUSED rows, so after this a fresh
    run may be launched but this one never runs again.

    WHAT IT DOES, in one transaction then a leg-close:
      1. set status=CANCELLED + finished_at + stamp CANCEL_NOTE;
      2. flip THIS run's PAUSED StepRun row(s) to CANCELLED (stamping finished_at) so no
         stale 'paused' review row survives to lie on the timeline or be picked up by
         paused_review / the observer — the run was aborted at that step, not reviewed;
      3. commit;
      4. close_open_legs_for_run: any open vendor leg (DISPATCHED/RETRY_WAIT) is closed
         CANCELLED and its held retry_contract cleared, so no expiry/retry sweep drags the
         run back to PAUSED and a late vendor return is DROPPED loudly (the leg is
         CANCELLED and the run is terminal — assert_leg_admitting covers both).

    Already-incurred AI/Human spend is NOT refunded (each step committed its own ledger
    row as it ran); vendor legs closed CANCELLED are excluded from Human spend by the
    existing vendor_spend rule.

    A RUNNING run's execution thread (if any) finds the row CANCELLED at its write-back
    checkpoint (_abort_if_superseded) and discards its results rather than flipping the
    row to COMPLETED. In-flight steps already committed still stand; the terminal verdict
    is CANCELLED.

    Raises RunCancelError when the run is missing or already terminal (the caller maps it
    to a 4xx). Org/tenant scoping is the CALLER's job (this seam has no identity)."""
    run = session.get(WorkflowRun, run_id)
    if run is None:
        raise RunCancelError(f"run {run_id} does not exist.")
    if run.status not in _CANCELLABLE_RUN_STATUSES:
        raise RunCancelError(
            f"run {run_id} is {run.status} (already terminal); only a pending/queued/running/paused run can be "  # noqa: E501
            "cancelled."
        )
    # Issue #492: compare-and-set. The check above read the status; the execution thread's
    # guarded terminal commit may land before this write. A conditional UPDATE from the
    # cancellable statuses means a run that completed (or failed) first keeps its verdict.
    claimed = session.execute(
        sa_update(WorkflowRun)
        .where(WorkflowRun.id == run_id, WorkflowRun.status.in_(tuple(_CANCELLABLE_RUN_STATUSES)))
        .values(status=RunStatus.CANCELLED, finished_at=datetime.utcnow())
    )
    if not claimed.rowcount:
        session.rollback()
        session.expire_all()
        now = session.get(WorkflowRun, run_id)
        raise RunCancelError(
            f"run {run_id} is {now.status if now is not None else 'gone'} (already terminal); "
            "only a pending/queued/running/paused run can be cancelled."
        )
    session.refresh(run)
    _stamp_note(run, CANCEL_NOTE)
    session.add(run)
    # Retire any PAUSED StepRun of THIS run so the terminal run carries no stale 'paused'
    # row (a run aborted at a human_review/external step was not 'reviewed').
    for sr in session.exec(
        select(StepRun).where(StepRun.workflow_run_id == run_id, StepRun.status == RunStatus.PAUSED)
    ).all():
        sr.status = RunStatus.CANCELLED
        if sr.finished_at is None:
            sr.finished_at = datetime.utcnow()
        session.add(sr)
    session.commit()
    # Close any open vendor leg now that the run is terminal (fires only because the run
    # is CANCELLED, which is_run_terminal now recognises).
    legs_closed = 0
    try:
        from app.services import vendor_dispatch as _vd

        legs_closed = _vd.close_open_legs_for_run(session, run_id)
    except Exception:
        log.exception("cancel_run: closing open legs for run %s failed", run_id)
    if actor:
        try:
            from app.services import audit

            audit.log(
                actor=actor,
                action="workflow.run_cancelled",
                target=f"run {run_id}",
                detail=f"workflow {run.workflow_id} doc {run.document_id}",
            )
        except Exception:
            log.exception("cancel_run: audit log for run %s failed", run_id)
    log.info(
        "Run %s cancelled by %s (%d open leg(s) closed)", run_id, actor or "operator", legs_closed
    )
    return {"ok": True, "run_id": run_id, "status": RunStatus.CANCELLED, "legs_closed": legs_closed}
