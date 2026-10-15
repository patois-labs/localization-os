"""orchestrator: part10

Documented in: docs/architecture/translation-pipeline.md
"""

import logging
import time
from datetime import datetime

from sqlmodel import Session, select

from app import db as _db
from app.models import (
    RunStatus,
    StepRun,
    WorkflowRun,
    WorkflowStep,
)
from app.services.orchestrator_part2 import _TERMINAL, _observer_session, _step_event, get_steps
from app.services.orchestrator_part3 import _release_scope
from app.services.orchestrator_part4 import _replay_done_text, _replay_final_blob
from app.services.orchestrator_part5 import _failure_reason
from app.services.orchestrator_part6 import _progress_snapshot
from app.services.orchestrator_part8 import (
    _bump_run_progress,
    _flush_progress,
    _gate_release_mask_in_scope,
    _progress_forget,
    run_progress,
    run_reason,
)
from app.services.orchestrator_part9 import publish_run_progress

log = logging.getLogger("loc_tms.orchestrator")


def _gate_release_mask(
    session: Session, run, unit_texts: list[str], *, held_families: list | None = None
) -> list[bool]:
    """Per unit: True where THIS run's own check PASSED over the text about to be committed.

    GATE C ROUND 3, M1 -- the release rule the five surfaces promise is "a later run whose
    OWN check PASSES **over the new text**", and until this mask existed the code only
    asked whether a check passed SOMEWHERE in the run. `mt -> lqa(pass) -> post-edit` (the
    ordinary "translate, check, then polish" shape, and every `step:<key>` correction
    workflow) cleared a standing hold and downloaded the POST-EDIT body at 200 -- text no
    judge ever saw. The copy describes the code again.

    Three cases, in order:
      * the judged blob IS the committed text -> every unit releases (the common healthy
        shape: the check is the run's last text-touching event);
      * the blob splits into exactly this many units (it was built with `SEG_JOINER` from
        the same `working` list) -> release UNIT BY UNIT, so a post-edit that rewrote one
        segment does not re-hold the others;
      * anything else (no verdict at all, a unit count that does not line up) -> release
        NOTHING. The safe direction: a stale badge is a reviewer click away, a cleared one
        is a delivery.

    `held_families` (gate C round 4, M2) is the per-unit list of the families already on the
    unit's badge (`quality.gate_hold_families`). Units whose scopes differ are evaluated in
    separate groups over the SAME unit list, so the blob split stays aligned; the ordinary
    all-`lqa` document is one group and one pair of reads, exactly as in round 3."""
    n = len(unit_texts)
    groups: dict[frozenset, list[int]] = {}
    for j in range(n):
        held = held_families[j] if held_families is not None and j < len(held_families) else None
        groups.setdefault(_release_scope(held), []).append(j)
    out = [False] * n
    for scope, idxs in groups.items():
        sub = _gate_release_mask_in_scope(session, run, unit_texts, scope)
        for j in idxs:
            out[j] = sub[j]
    return out


class _ProgressTicker:
    """The per-step counter behind `run_progress` (GitHub #101).

    Constructed at the top of a long step with the number of units it will do:

      * `start()` publishes 0/total at once, so a surface shows "0 of 102 segments"
        the moment the step begins rather than nothing at all -- and publishes the TOTAL
        and the STEP it belongs to, which is what an operator (or a monitor) needs to read
        a slow run;
      * `tick()` is called as each unit lands and only ever touches MEMORY, so a fan-out
        worker never opens a Session (#100);
      * `on_done(future)` is the `add_done_callback` adapter: it ticks only for a future
        that actually RETURNED. A future that raised or was cancelled is NOT a completed
        unit, and counting it is how a FAILED step came to report a cosmetic 102/102;
      * `flush()` queues a persist on the background writer (never a DB call on the
        caller's thread), at most once per PROGRESS_MIN_INTERVAL_S;
      * `finish(session, run)` stamps the final count on the run's OWN ORM object so it
        rides the step's own commit, then drops the live state.

    Every method is exception-proof: this is an observer bolted onto a translation step and
    may never perturb it."""

    __slots__ = ("run_id", "step_index", "step_name", "total", "unit")

    def __init__(self, run_id, total: int, unit: str, step=None):
        self.run_id = run_id
        self.total = max(0, int(total or 0))
        self.unit = unit
        name = getattr(step, "name", None) if step is not None else None
        if not name and step is not None:
            _st = getattr(step, "step_type", "")
            name = getattr(_st, "value", _st)
        self.step_name = str(name or "")[:64]
        try:
            self.step_index = max(0, int(getattr(step, "order_index", 0) or 0))
        except Exception:
            self.step_index = 0

    def _live(self) -> bool:
        return bool(self.run_id) and bool(self.total)

    def start(self) -> None:
        if self._live():
            try:
                publish_run_progress(
                    self.run_id,
                    0,
                    self.total,
                    self.unit,
                    force=True,
                    step=self.step_name,
                    step_index=self.step_index,
                )
            except Exception:
                log.debug("run progress start failed for run %s", self.run_id, exc_info=True)

    def tick(self, n: int = 1) -> None:
        """One unit landed. MEMORY ONLY -- safe to call from any thread."""
        if not self._live():
            return
        try:
            _bump_run_progress(self.run_id, n)
        except Exception:
            log.debug("run progress tick failed for run %s", self.run_id, exc_info=True)

    def on_done(self, fut=None) -> None:
        """`Future.add_done_callback` adapter. Runs on the WORKER THREAD that finished the
        call, which is the point -- it is what makes the counter move DURING the fan-out
        instead of only when the DB thread collects results. Memory only, by the rule above.

        A future that RAISED or was CANCELLED is not a completed unit and is not counted
        (gate F round 1, M2): the pool's `shutdown(wait=True)` fires every callback before
        the step's `except DeadEngineError` runs, so counting them made a step that died on
        segment 2 of 6 persist a cosmetic 6/6 beside `status: failed`."""
        try:
            if fut is not None:
                if fut.cancelled():
                    return
                if fut.exception() is not None:
                    return
        except Exception:
            return
        self.tick()

    def flush(self) -> None:
        """Queue a persist if the throttle allows. Never blocks, never opens a Session."""
        if not self._live():
            return
        try:
            _flush_progress(self.run_id)
        except Exception:
            log.debug("run progress flush failed for run %s", self.run_id, exc_info=True)

    def finish(self, session: Session | None = None, run: WorkflowRun | None = None) -> None:
        """Persist where the step ACTUALLY reached and drop the live state.

        The counter is NOT advanced to total: a step that ended early (a refusal, a dead
        engine, a fan-out future that raised) keeps reporting the honest 5-of-6 that landed.

        THE FINAL COUNT RIDES THE RUN'S OWN COMMIT. The columns are stamped on the run ORM
        object the caller is already holding, so the step boundary costs no second
        connection, no pool checkout and no writer-lock acquisition of its own -- which is
        also why it cannot deadlock with a session that is mid-write (gate F round 1, H1).

        THERE IS NO NO-RUN FALLBACK, AND THIS DOCSTRING NO LONGER CLAIMS ONE (gate F round
        2, L1). The earlier text advertised "falls back to queueing a background write",
        which could not work: `_flush_progress(force=True)` only marks the id dirty, and the
        `finally` below pops the state one line later -- before the writer thread can read
        it -- so `_persist_progress` returned without writing and the row silently kept the
        stale count `start()` published. A step that did 4 of 6 units read as one that never
        moved, which is worse than reporting nothing. Both call sites hold `(session, run)`;
        a third caller that does not must persist the final count itself."""
        try:
            snap = _progress_snapshot(self.run_id)
            if snap is not None:
                if run is not None:
                    run.progress_done = snap["done"]
                    run.progress_total = snap["total"]
                    run.progress_unit = snap["unit"] or ""
                    run.progress_step = snap["step"] or ""
                    run.progress_step_index = snap["step_index"] or 0
                    run.progress_at = snap["at"] or datetime.utcnow()
                    if session is not None:
                        session.add(run)
                else:
                    log.debug(
                        "run progress finish for run %s had no run object: the final count "
                        "(%s/%s) was NOT persisted -- the step boundary rides the run's own "
                        "commit and has nothing to ride here",
                        self.run_id,
                        snap["done"],
                        snap["total"],
                    )
        except Exception:
            log.debug("run progress finish failed for run %s", self.run_id, exc_info=True)
        finally:
            _progress_forget(self.run_id)


def observe_run(run_id: int, poll_interval: float = 0.4, max_seconds: float = 900.0):
    """Read-only SSE generator: poll a run's StepRun/WorkflowRun rows and yield
    the same event shapes run_view.html consumes (run_started, step, paused,
    done, closed). Never executes anything — reconnects/second tabs are safe.
    Emits already-recorded steps immediately (replay), then tails new ones until
    the run reaches a terminal/paused state (or max_seconds elapses).

    THE 'closed' EVENT NOW CARRIES THE VERDICT (GitHub #44/#47, additive). It used
    to be a bare {"type": "closed"} for EVERY ending -- completed, failed, paused,
    timed out -- so a consumer that had not been watching from the start could not
    tell a finished run from a dead one. A FAILED run therefore ended in silence:
    exactly the prod #74 story, where a 12-minute fr-FR run died at the fix step and
    no surface ever said so. `closed` now also carries `run_id`, `status` (the run's
    real RunStatus, or None when the run row is gone) and, for a FAILED run, `error`
    -- the failing step's message, falling back to the run's own notes (a
    restart/staleness sweep has no StepRun to blame). The fields were ADDITIVE when
    they landed, but they are LOAD-BEARING NOW (gate F round 4, LOW-4): run_view.html's
    `closed` branch DISPATCHES on `status` -- a status in its live list means "this was
    my observer ceiling, not an ending", so the page reopens the stream instead of
    settling the progress line -- and it renders `reason.text` beside the status word.
    `verify_run_observability` [P8] pins that coupling by driving a real ceiling close
    through this generator and checking the word against the page's own list."""
    # max_seconds bounds how long a single observer generator tails a run stuck in
    # RUNNING (a crashed background thread that never reached a terminal state).
    # Each active next() uses one dedicated streaming worker for up to max_seconds
    # (15 min by default). app.web.streaming bounds these calls with
    # LOC_TMS_STREAM_WORKERS (default 32), one token per open SSE tab or waiting caller.
    # Streams and long waits are off the default anyio limiter used by sync routes.
    # LOC_TMS_STREAM_WAIT_S (default 5s) bounds admission: SSE sheds with stream_capacity;
    # wait=true falls back to current state with waited: false and retry_after.
    # THE TOTAL BOUND IS PER VISIBLE TAB, NOT PER GENERATOR (gate F round 4, MED-1).
    # run_view.html reopens the stream when this ceiling closes on a still-live run, so
    # one watched page can chain up to MAX_CEILING_REOPENS = 24 windows = 6 h before it
    # stops and asks for a refresh. A HIDDEN tab does not renew: the reopen is gated on
    # `document.visibilityState === 'visible'` and parks until the tab is looked at
    # again, so a forgotten background tab costs nothing after its current window.
    # ACCEPTED at that bound (a stuck run is already an anomaly recover_interrupted()
    # addresses at restart), and it is the WORKER THREAD only: the DB pool is NOT held.
    # Measured with a live stream open through TestClient, `engine.pool.checkedout()`
    # stayed 0 -- FastAPI closes `yield` dependencies before the response body streams,
    # so `get_session`'s connection is returned before the first event, and every read
    # below opens its own short-lived Session.
    #
    # NOT AN ORDINARY REQUEST SCOPE (gate A round 2, MED-2). This generator opens a Session
    # per poll — up to ~2250 checkouts over its 900s ceiling — inside the SSE request's
    # context. The per-request pool-wait budget is sized for a page load, so one momentary
    # saturation anywhere in the stream would refuse every later read for the REST of the
    # stream even after the pool went completely free, killing the operator's only live
    # progress view exactly under the load where it matters (and its reconnect is then shed
    # at the door). A stream gets the per-CHECKOUT timeout instead: it loses one poll to a
    # blip and picks the run up again on the next tick.
    # RE-ASSERTED EVERY POLL, not once (gate A round 3, MED-2). app.web.streaming
    # drives each next() on the producer's worker with a fresh copy of request context.
    # A ContextVar.set(None) inside one next() is discarded before the next poll.
    # The exemption
    # therefore lives at the top of the LOOP, where it is re-set inside whichever `next()`
    # this poll belongs to and cannot be lost, and `_observer_session` supplies the retry the
    # old justification claimed the stream already had.
    _db.exempt_pool_budget()
    from app.db import engine

    deadline = time.time() + max_seconds
    emitted: set[int] = set()
    started = False
    # GitHub #101: the last progress tuple this generator emitted, so a `progress` event is
    # sent only when the counter actually MOVES (the poll runs 2-3x a second; the counter is
    # written at most once every PROGRESS_MIN_INTERVAL_S). None until the run publishes any.
    last_progress = None
    while True:
        _db.exempt_pool_budget()
        try:
            _poll_session = _observer_session(engine)
        except Exception:
            # A TRANSIENT SATURATION COSTS ONE TICK, NOT THE STREAM (MED-2). Round 2
            # justified the exemption with "a stream that loses one poll retries on the next
            # tick", and the gate was right that no such retry existed: any checkout failure
            # propagated out of the generator and killed the observer. Now it does — bounded
            # by the same `deadline` as everything else, so a permanently exhausted pool
            # still ends the stream honestly instead of tailing forever.
            if time.time() > deadline:
                yield None, {"type": "closed", "run_id": run_id, "status": None}
                return
            time.sleep(poll_interval)
            continue
        with _poll_session as s:
            run = s.get(WorkflowRun, run_id)
            if run is None:
                yield run, {"type": "closed", "run_id": run_id, "status": None}
                return
            if run.status == RunStatus.QUEUED:
                yield run, {"type": "closed", "run_id": run_id, "status": RunStatus.QUEUED}
                return
            steps = get_steps(s, run.workflow_id)
            if not started:
                started = True
                yield (
                    run,
                    {"type": "run_started", "run_id": run.id, "steps": len(steps), "start": 0},
                )
            step_runs = s.exec(
                select(StepRun).where(StepRun.workflow_run_id == run_id).order_by(StepRun.id)
            ).all()
            status = run.status
            # GitHub #101: IN-STEP PROGRESS on the wire. Before this, a single long
            # `llm_agent` step streamed `run_started` and then SILENCE for 10-25 minutes --
            # this generator only emits when a StepRun ROW appears, which happens when a step
            # ENDS. Now the run publishes "N of M" from inside the step and the stream
            # forwards it on CHANGE (never on every poll). Additive: a consumer that does not
            # know the event ignores it.
            progress = run_progress(run)
            # Among possibly-MULTIPLE PAUSED StepRuns that share a step (a Phase-3 loop
            # revisits a human_review step, persisting ONE PAUSED row PER pause — all
            # with the SAME order_index), the LIVE pause is the MOST RECENT one (highest
            # id). Picking the first id-order match instead would replay a PRIOR loop
            # iteration's STALE input_text on a reconnect. Compute the live-pause id once
            # (only meaningful when the run itself is PAUSED); older same-step PAUSED rows
            # are resolved prior-iteration pauses and fall through to the stale-skip path.
            _oidx = {st.id: st.order_index for st in steps}
            live_pause_id = None
            if status == RunStatus.PAUSED:
                for _sr in step_runs:  # ascending id -> last match = newest
                    if (
                        _sr.status == RunStatus.PAUSED
                        and _oidx.get(_sr.step_id) == run.current_step_index
                    ):
                        live_pause_id = _sr.id
            if progress is not None:
                # The STEP is part of the identity: a step boundary republishes 0-of-M, and
                # without the step in the tuple a 0/5 following a 0/5 would be swallowed as
                # "unchanged" and the page would keep showing the previous step's line.
                _ptuple = (
                    progress["done"],
                    progress["total"],
                    progress["unit"],
                    progress["step"],
                    progress["step_index"],
                )
                if _ptuple != last_progress:
                    last_progress = _ptuple
                    yield run, {"type": "progress", "run_id": run_id, **progress}
            for sr in step_runs:
                if sr.id in emitted:
                    continue
                # A human_review step persists a PAUSED StepRun that is NEVER
                # marked resolved — so a run paused at its SECOND human step still
                # has the FIRST human step's StepRun sitting at status=PAUSED. On a
                # fresh reconnect `emitted` is empty and we iterate id-order, so a
                # naive "first PAUSED row is the live pause" would surface the stale
                # step-1 pause instead of the real step-2 one. Guard with BOTH:
                #   - the RUN itself must be paused (status == PAUSED), and
                #   - this StepRun's step must be the one the run is paused AT, i.e.
                #     its WorkflowStep.order_index == run.current_step_index (set to
                #     the human step's index when the run pauses; advanced on resume).
                # Any other PAUSED StepRun is a resolved/stale human step — skip it.
                if sr.status == RunStatus.PAUSED:
                    step = s.get(WorkflowStep, sr.step_id)
                    # The live pause is the NEWEST PAUSED row for the paused-at step
                    # (highest id) — see live_pause_id above. Older same-order_index
                    # PAUSED rows are stale prior-loop-iteration pauses.
                    is_live_pause = (
                        status == RunStatus.PAUSED and step is not None and sr.id == live_pause_id
                    )
                    if is_live_pause:
                        emitted.add(sr.id)
                        yield (
                            run,
                            {
                                "type": "paused",
                                "run_id": run.id,
                                "step_index": step.order_index,
                                "step_name": step.name or "Human review",
                                "text": sr.input_text,
                            },
                        )
                        # The verdict rides here too (review NEW-2). This is the
                        # NORMAL close for a freshly-connected paused run -- the most
                        # common paused path there is -- and it was the one `closed`
                        # site still emitting a bare event. A consumer that reads the
                        # status therefore saw "no status", treated the close as an
                        # expired observer window, reconnected, burned its second
                        # window and relabelled a PAUSED run "still running".
                        yield run, {"type": "closed", "run_id": run_id, "status": status}
                        return
                    # Not the live pause. Only mark it consumed once the RUN state
                    # is settled (paused at some step, or terminal) — then it is
                    # genuinely a stale resolved human step. While the run still
                    # reads RUNNING, this row may be the LIVE pause racing our
                    # status read: the pause commit (StepRun + status, atomic on
                    # the writer side) can land BETWEEN this poll's `run` SELECT
                    # and its `step_runs` SELECT (the generator also suspends at
                    # every yield, widening that window). Marking it emitted then
                    # would skip it forever and the PAUSED branch below would
                    # close the stream WITHOUT ever emitting 'paused' (the CI-only
                    # verify_chunk5 finding-3 flake). Defer judgment: leave it
                    # un-emitted so the next poll re-evaluates with fresh status.
                    if status == RunStatus.PAUSED or status in _TERMINAL:
                        emitted.add(sr.id)
                    continue
                if sr.status == RunStatus.SKIPPED:
                    # A guard-SKIPPED step (SPEC §5, Phase 2): replay the SAME additive
                    # step_skipped event the live generator emitted, so a reconnect
                    # shows the skip honestly rather than as a run step. A plain run
                    # never produces a SKIPPED row, so this branch is inert (byte-stable).
                    step = s.get(WorkflowStep, sr.step_id)
                    emitted.add(sr.id)
                    yield (
                        run,
                        {
                            "type": "step_skipped",
                            "run_id": sr.workflow_run_id,
                            "step_index": step.order_index if step else 0,
                            "step_name": (step.name or step.step_type) if step else "step",
                            "step_type": step.step_type if step else "",
                            "condition": (step.run_condition if step else None),
                            "output": sr.output_text,
                        },
                    )
                    continue
                emitted.add(sr.id)
                yield run, _step_event(s, sr)
            # snapshot the final text (last completed step output) for 'done'
            # Only a COMPLETED run ever emits 'done', so only it pays for the full-width blob
            # lookup (a segment-count query + load): this block runs on EVERY poll of a live
            # run, and a RUNNING observer must stay as cheap as the old last-row read.
            final_text = (
                _replay_final_blob(s, run, step_runs)
                if step_runs and status == RunStatus.COMPLETED
                else (step_runs[-1].output_text if step_runs else "")
            )
            # F4 (display parity): reconstruct the terminal text a LIVE run emitted —
            # withheld slots blanked to "" — from the persisted blob, keyed to the
            # WITHHELD SLOTS (not the marker text), so a legit committed translation
            # that equals/contains the marker is NOT corrupted (see _replay_done_text).
            # Storage + the per-step `step` trace are untouched; only the replayed
            # 'done' text changes. Computed inside the session block (needs the run's
            # segments); only for a COMPLETED run, which is the only status that emits
            # 'done'.
            if status == RunStatus.COMPLETED and step_runs:
                final_text = _replay_done_text(s, run, final_text)
            # The verdict a 'closed' event now carries (see the docstring). Computed
            # inside the session block, where the StepRun rows and the run's notes are
            # still readable; only meaningful for a FAILED run.
            fail_error = _failure_reason(run, step_runs) if status == RunStatus.FAILED else None
            # GitHub #106: WHY this run is terminal, when the reason came from a RECONCILER
            # rather than a step (a boot reconcile after a crash, the staleness sweep, an
            # operator cancel). Those runs have no StepRun to blame -- `error` above is
            # empty for them by construction, and a CANCELLED run never carried a reason at
            # all -- so the stream said `cancelled` / `failed` and nothing else.
            close_reason = run_reason(run)
        if status == RunStatus.PAUSED:
            # paused with no unseen PAUSED StepRun (already replayed) — just close
            yield None, {"type": "closed", "run_id": run_id, "status": status}
            return
        if status in _TERMINAL:
            if status == RunStatus.COMPLETED:
                yield None, {"type": "done", "run_id": run_id, "text": final_text}
            closed = {"type": "closed", "run_id": run_id, "status": status}
            if fail_error:
                closed["error"] = fail_error
            if close_reason:
                closed["reason"] = close_reason  # #106
            yield None, closed
            return
        if time.time() > deadline:
            # The observer gave up tailing; the RUN is still whatever it is (usually
            # RUNNING). Reported honestly rather than as a terminal verdict.
            yield None, {"type": "closed", "run_id": run_id, "status": status}
            return
        time.sleep(poll_interval)
