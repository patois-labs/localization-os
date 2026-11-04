"""Step hand-off: a workflow run gives its execution slot back between step visits (S2, ADR 0092).

Before S2 a run held one capacity slot (`orchestrator.RUN_SLOTS`) from its first step to its
last, on one daemon thread. A run with twenty slow steps therefore kept every run queued behind
it waiting for all twenty, and the order in which waiting work got a slot was decided once, at
admission. With hand-off a run executes in SEGMENTS:

* A segment is one stretch of `run_workflow_stream` on one thread under one slot. The run's
  generator, its database Session and its context variables live on the `RunSegment` object, not
  on the thread, so the next segment continues the SAME generator: the workflow graph (loops,
  gates, branches, parallel groups, human review pauses, visit caps) cannot behave differently,
  because nothing is re-entered or re-derived. Each advance runs inside the run's own
  `contextvars.Context`, so a context variable a step sets is seen by the next step whichever
  thread runs it (a new thread starts with an empty context, exactly as the run thread always
  did).
* At a completed step visit (a "step" or "step_skipped" event, emitted right after the visit's
  checkpoint commit, S1) a segment that has run for at least `config.STEP_QUANTUM_S` seconds
  PARKS when another customer's work is waiting and its Session holds no unflushed change (the
  rules in `_should_park`, ADR 0098): it ends the read
  transaction (so a parked run pins no pooled connection), registers with `PARKED`, hands its
  heartbeat to the parked holder (`run_resume.park`) and releases its slot. Its thread ends. The
  run row stays RUNNING with its owner and heartbeat, so recovery, batch width and cancel see an
  in-flight run.
* The admission scheduler (`orchestrator.ADMISSION`) treats parked runs as waiting work beside
  QUEUED rows and picks between them with the same fair order (customer, then fan-out, then
  language). Picking a parked run takes a slot and starts a new thread, named like the first one
  so `_run_thread_alive` and the batch joins keep working, that adopts the heartbeat
  (`run_resume.adopt`) and continues the generator.

Durability is S1's: every completed visit is checkpointed in the database, so if the process
dies while a run is parked or executing, the boot recovery and the live sweep resume it from its
last checkpoint (or fail it where S1 cannot resume, as before). Parked continuations themselves
are in memory: a second process cannot pick up a run another live process parked. That is the
S3 boundary (worker processes claim QUEUED rows; a run stays in the process that started it until
it pauses, ends, or its process dies).

Hand-off is only offered when the process's admission daemon is alive to continue the run (a
synchronous CLI batch never parks), and never on a synchronous `workflow run`.

Documented in: docs/architecture/workflow-engine.md
"""

from __future__ import annotations

import contextvars
import logging
import threading
import time

from app import config

log = logging.getLogger("loc_tms.run_segments")

#: Events a run loop yields right after a step visit's commit: the only places a run parks.
BOUNDARY_EVENTS = frozenset({"step", "step_skipped"})
#: Events a worker process announces to the web role's run streams (S3, `wake.run_changed`).
_PUBLISHED_EVENTS = frozenset({"run_started", "step", "step_skipped", "paused", "done", "error"})

_lock = threading.Lock()
#: run id -> parked RunSegment, in park order (dicts keep insertion order).
PARKED: dict[int, RunSegment] = {}
#: Process counters (tests and /health detail read them): parks and continuations so far.
STATS = {"parks": 0, "continues": 0}
#: (batch group, step index, boundary count) -> (whether the first language of the batch to
#: reach that place parked, when) (ADR 0099): its siblings at the same place follow
#: it. An entry expires after _PLACE_DECISION_TTL_S, so a later execution of the same batch (a
#: resume after review restarts the boundary count; a retry round reuses its group) decides
#: afresh (gate L1).
_PLACE_DECISIONS: dict[tuple, tuple[bool, float]] = {}
_PLACE_DECISION_TTL_S = 300.0
#: run id -> the RunSegment executing or parked in THIS process, until its execution ends (the
#: scheduler continues a batch's parked languages once none of them is still finishing the
#: step, ADR 0099).
LIVE: dict[int, RunSegment] = {}


def live() -> list[RunSegment]:
    with _lock:
        return list(LIVE.values())


def is_live(run_id) -> bool:
    if run_id is None:
        return False
    with _lock:
        return int(run_id) in LIVE


def heading_to_park(seg: RunSegment, cursor: int) -> bool:
    """True when the executing `seg` may still park at step `cursor` or before: it is on an
    earlier step, or it is at that boundary and has decided to park (between the decision and
    the park) or not decided yet. A run that decided at that boundary not to park (its next step
    is a review, a sibling's decision did not apply) is not heading."""
    if is_parked(seg.run_id) or seg.done.is_set():
        return False
    if seg.is_parked_now:
        return True  # between `_parked = True` and its registration in PARKED
    if seg.cursor < cursor:
        return True
    if seg.cursor != cursor or seg.parked_boundary == seg.boundaries:
        return False  # past it, or it parked here already and was continued
    boundary, decision = seg.decided
    return decision if boundary == seg.boundaries else True


def handoff_mode() -> str:
    return getattr(config, "STEP_HANDOFF", "auto") or "auto"


def quantum_s() -> float:
    return float(getattr(config, "STEP_QUANTUM_S", 30.0) or 0.0)


def is_parked(run_id) -> bool:
    if run_id is None:
        return False
    with _lock:
        return int(run_id) in PARKED


def parked() -> list[RunSegment]:
    with _lock:
        return list(PARKED.values())


def take_parked(run_id: int) -> RunSegment | None:
    """Remove and return the parked segment of `run_id` (the dispatcher is about to continue it)."""
    with _lock:
        return PARKED.pop(int(run_id), None)


class SlotProxy:
    """The capacity token a run's generator was created with, swappable between segments.

    `run_workflow_stream` captures the `capacity_slot` it is given for the run's whole life; a
    parked run releases its token and the next segment runs under a new one, so the generator is
    handed this proxy and the proxy points at whichever token the current segment holds."""

    __slots__ = ("token",)

    def __init__(self, token) -> None:
        self.token = token

    @property
    def handed_off(self) -> bool:
        return bool(getattr(self.token, "handed_off", False))

    @handed_off.setter
    def handed_off(self, value: bool) -> None:
        self.token.handed_off = value

    @property
    def released(self) -> bool:
        return bool(getattr(self.token, "released", False))

    def release(self, *, wake: bool = False) -> bool:
        return self.token.release(wake=wake)


class RunSegment:
    """One run's execution, driven segment by segment (see the module docstring).

    `make_stream(session, slot)` returns the run's event generator. `on_crash(exc)` is the
    launcher's crash handler (it marks the run FAILED with the honest note), called after the
    Session is closed, exactly where the old per-run thread called it. `on_end()` runs once when
    the run's execution is over (not at a park). The slot is released when the execution ends
    or parks."""

    def __init__(
        self,
        *,
        run_id: int | None,
        thread_name: str,
        make_stream,
        on_crash,
        on_end=None,
        slot,
        handoff: bool,
        engine=None,
        owns_slot: bool = True,
    ) -> None:
        self.run_id = run_id
        self._engine = engine
        #: A caller-owned first token (a direct `_run_member(slot=...)`) is released by its
        #: caller, never here; every token a continuation takes is the segment's own.
        self._caller_token = None if owns_slot else slot
        self.thread_name = thread_name
        self._make_stream = make_stream
        self._on_crash = on_crash
        self._on_end = on_end
        self.slot = SlotProxy(slot)
        self.handoff = bool(handoff) and run_id is not None
        self.ctx = contextvars.Context()
        self.session = None
        self.gen = None
        self.actor = None
        self.group: str | None = None
        self.customer: str = ""
        self.locale: str = ""
        self.batch_id: int | None = None
        self.parked_at = 0.0
        #: The step index of the last completed visit (a parked run's place).
        self.cursor = -1
        #: How many step boundaries this run has passed (with `cursor`, its place in a loop),
        #: and the count at its last park.
        self.boundaries = 0
        self.parked_boundary = -1
        #: (boundary count, decision) of the last `_should_park` call.
        self.decided: tuple[int, bool] = (-1, False)
        self.segments = 0
        self._parked = False
        self.done = threading.Event()
        #: Set each time a new segment thread starts (batch joins wait on the latest thread).
        self.thread: threading.Thread | None = None

    # -- the parked holder run_resume keeps beating for --------------------------------
    def is_alive(self) -> bool:
        return self._parked and not self.done.is_set()

    @property
    def is_parked_now(self) -> bool:
        return self._parked

    # -- execution ---------------------------------------------------------------------
    def drive(self) -> str:
        """Run one segment on the CURRENT thread. Returns "parked" or "done"."""
        from app.services import run_resume

        first = self.gen is None
        self.segments += 1
        started = time.monotonic()
        if first:
            self.thread = threading.current_thread()
            if self.run_id is not None:
                with _lock:
                    LIVE[int(self.run_id)] = self
            if self.actor is None:
                self.actor = _current_actor()
        else:
            run_resume.adopt(self.run_id)
            # Parked until HERE (gate S2 L1/L2): until this thread holds the heartbeat, the
            # parked holder must stay live and a batch join must keep waiting.
            self._parked = False
        try:
            if first:
                from sqlmodel import Session

                if self._engine is None:
                    from app.db import engine as default_engine

                    self._engine = default_engine
                self.session = Session(self._engine)
                self.gen = self.ctx.run(self._make_stream, self.session, self.slot)
            while True:
                try:
                    event = self.ctx.run(next, self.gen)
                except StopIteration:
                    break
                if isinstance(event, dict) and event.get("type") in _PUBLISHED_EVENTS:
                    from app.services import wake

                    wake.run_changed(self.run_id, event.get("type"))
                if (
                    self.handoff
                    and isinstance(event, dict)
                    and event.get("type") in BOUNDARY_EVENTS
                    and self._should_park(started, event)
                ):
                    if self._park():
                        return "parked"
                    # Could not park after all: siblings waiting on this run must not wait out
                    # the deferral (gate R2 L1).
                    self.decided = (self.boundaries, False)
                    from app.services import orchestrator

                    orchestrator.ADMISSION.notify()
        except Exception as exc:
            self._close_session()
            try:
                self._on_crash(exc)
            except Exception:
                log.exception("crash handler for run %s failed", self.run_id)
        except BaseException:
            self._close_session()
            self._finish()
            raise
        self._close_session()
        self._finish()
        return "done"

    def _finish(self) -> None:
        try:
            if self.slot.token is not self._caller_token:
                self.slot.release()
        finally:
            # Drop the run's generator, context and callbacks NOW. A per-run thread used to take
            # its context with it when it ended; a finished segment can outlive its execution (a
            # batch handle keeps it for the joins), and a context variable or generator frame it
            # still held kept a pooled connection checked out until the garbage collector ran:
            # measured on Linux as a 50 s stall of the next request that needed the pool.
            self.gen = None
            self.ctx = contextvars.Context()
            self._make_stream = None
            self._on_crash = None
            self.done.set()
            if self.run_id is not None:
                with _lock:
                    if LIVE.get(int(self.run_id)) is self:
                        del LIVE[int(self.run_id)]
            from app.services import wake

            wake.run_changed(self.run_id, "end")  # S3: a crash ends a stream at once too
            if self._on_end is not None:
                try:
                    self._on_end()
                except Exception:
                    log.exception("end hook for run %s failed", self.run_id)
                self._on_end = None

    def _close_session(self) -> None:
        s, self.session = self.session, None
        if s is not None:
            try:
                s.close()
            except Exception:
                log.debug("closing run %s session failed", self.run_id, exc_info=True)

    # -- hand-off ----------------------------------------------------------------------
    def _should_park(self, started: float, event: dict | None = None) -> bool:
        """Whether to hand the slot back at this completed step visit.

        The rules (real-LLM flood, 2026-10-23), in order:
          * never before a step that does no work for the slot (human review, an external
            vendor hand-off, or the end): the run would pause or end right away, so a park
            there only adds a wait;
          * a document's languages park together: the first language of a batch to reach a
            place (step index and boundary count) decides, and its siblings at the same place
            follow that decision (ADR 0099: deciding each on its own, one language found no
            other customer waiting and went on while its sibling, a moment later, parked, and
            the two reached review a step apart); and when a sibling is parked at this step or
            an earlier one, this run parks too, so they continue together;
          * otherwise only after the quantum, and only when ANOTHER customer's work waits:
            within one customer, started work finishes before new documents start (the
            scheduler continues a customer's parked runs before admitting its new work), so
            parking for that customer's own queue would only add a wait."""
        # The place first, before any early return, so it never drifts (gate L2).
        if isinstance(event, dict) and isinstance(event.get("step_index"), int):
            self.cursor = int(event["step_index"])
        self.boundaries += 1
        decision = self._decide_park(started)
        # What the scheduler reads to know this run will not park here (`heading_to_park`).
        self.decided = (self.boundaries, decision)
        batch = (self.group or "").startswith(("batch:", "batch-retry:"))
        if not decision and batch and any(seg.group == self.group for seg in parked()):
            # Its parked siblings were waiting for this run's decision: wake the scheduler.
            from app.services import orchestrator

            orchestrator.ADMISSION.notify()
        return decision

    def _decide_park(self, started: float) -> bool:
        mode = handoff_mode()
        if mode == "off":
            return False
        from app.services import orchestrator

        if mode == "always":
            # The proof knob: park at every boundary, starting the scheduler that continues it.
            return orchestrator.ADMISSION.alive() or orchestrator.ADMISSION.ensure_started()
        if not orchestrator.ADMISSION.alive():
            return False  # nothing would continue it
        if self.group is None and not self._describe():
            return False
        # This run's own next step, never a sibling's: languages can go different ways from one
        # place (a gate that loops one back, a per-locale variant), and a shared "park" must not
        # park a run right before its review (gate H1).
        if self._next_step_is_pause_only():
            return False
        place = None
        if (self.group or "").startswith(("batch:", "batch-retry:")):
            place = (self.group, self.cursor, self.boundaries)
            now = time.monotonic()
            with _lock:
                prior = _PLACE_DECISIONS.get(place)
            if prior is not None and now - prior[1] <= _PLACE_DECISION_TTL_S:
                return prior[0]
        decision = self._sibling_parked_behind() or (
            time.monotonic() - started >= quantum_s()
            and orchestrator.ADMISSION.has_waiting(
                exclude_run=self.run_id, other_customer_than=self.customer
            )
        )
        if place is not None:
            with _lock:
                if len(_PLACE_DECISIONS) > 4096:
                    cutoff = now - _PLACE_DECISION_TTL_S
                    for key in [k for k, v in _PLACE_DECISIONS.items() if v[1] < cutoff]:
                        del _PLACE_DECISIONS[key]
                    if len(_PLACE_DECISIONS) > 4096:
                        _PLACE_DECISIONS.clear()
                prior = _PLACE_DECISIONS.get(place)
                if prior is not None and now - prior[1] <= _PLACE_DECISION_TTL_S:
                    return prior[0]
                _PLACE_DECISIONS[place] = (decision, now)
        return decision

    def _sibling_parked_behind(self) -> bool:
        """True when another language of this run's batch is parked at this step or before."""
        if not (self.group or "").startswith(("batch:", "batch-retry:")):
            return False
        return any(
            seg.run_id != self.run_id and seg.group == self.group and seg.cursor <= self.cursor
            for seg in parked()
        )

    def _next_step_is_pause_only(self) -> bool:
        """True when the step the run goes to next does no work for a slot: a human review or
        an external (vendor) step, which pause the run, or the end of the workflow."""
        try:
            from sqlmodel import Session

            from app.models import StepType, WorkflowRun
            from app.services.orchestrator_part2 import get_steps

            with Session(self._engine) as s:
                run = s.get(WorkflowRun, self.run_id)
                if run is None:
                    return True
                steps = get_steps(s, run.workflow_id)
                nxt = int(run.current_step_index or 0)
                if nxt >= len(steps) or nxt < 0:
                    return True
                return steps[nxt].step_type in (StepType.HUMAN_REVIEW, StepType.EXTERNAL)
        except Exception:
            log.debug(
                "run %s: could not read its next step; not parking", self.run_id, exc_info=True
            )
            return True

    def _park(self) -> bool:
        s = self.session
        if s is None or s.new or s.dirty or s.deleted:
            return False
        try:
            if s.in_transaction():
                s.rollback()  # end the read transaction: a parked run pins no connection
        except Exception:
            log.debug("run %s: could not end its transaction; not parking", self.run_id)
            return False
        from app.services import orchestrator, run_resume

        if self.group is None and not self._describe():
            return False
        # The token THIS segment holds: once the run is registered below, the scheduler may
        # continue it (and swap the proxy to a new token) before this thread gets here again.
        held = self.slot.token
        self._parked = True
        self.parked_at = time.monotonic()
        self.parked_boundary = self.boundaries
        run_resume.park(self.run_id, self)
        with _lock:
            PARKED[int(self.run_id)] = self
            STATS["parks"] += 1
        # Release AFTER registering, so the scheduler the release wakes already sees this run.
        held.release(wake=True)
        orchestrator.ADMISSION.notify()
        return True

    def _describe(self) -> bool:
        """Load what the fair scheduler orders a parked run by: its fan-out (the QUEUED-row
        group key, `queue_group` or the run id), batch, language and customer."""
        try:
            from sqlmodel import Session

            from app.models import Document, Project, WorkflowRun

            with Session(self._engine) as s:
                run = s.get(WorkflowRun, self.run_id)
                if run is None:
                    return False
                self.group = run.queue_group or str(run.id)
                self.batch_id = run.batch_id
                self.locale = run.target_locale or ""
                doc = s.get(Document, run.document_id) if run.document_id else None
                proj = s.get(Project, doc.project_id) if doc is not None else None
                cid = getattr(proj, "customer_id", None)
                self.customer = "" if cid is None else str(cid)
            return True
        except Exception:
            log.debug("run %s: could not describe it; not parking", self.run_id, exc_info=True)
            return False

    def continue_on(self, token) -> threading.Thread:
        """Continue this parked run under `token` on a new thread (the dispatcher calls this
        after `take_parked`). Raises if the thread cannot start; the run is then parked again
        and the token released by the caller."""
        self.slot.token = token
        token.handed_off = True
        th = threading.Thread(target=self._continue, name=self.thread_name, daemon=True)
        if self.actor is not None:
            # A thread started by the scheduler would inherit the SCHEDULER's actor: carry the
            # run's own (keys_common reads this attribute when nothing else is in scope).
            try:
                from app.services.keys_common import _ACTOR_ATTR

                setattr(th, _ACTOR_ATTR, self.actor)
            except Exception:  # pragma: no cover - attribution must never block a run
                log.debug("could not carry the actor of run %s", self.run_id, exc_info=True)
        previous, self.thread = self.thread, th
        try:
            th.start()
        except BaseException:
            # Parked again; the caller's slot owner releases the token (not handed off).
            self.thread = previous
            token.handed_off = False
            with _lock:
                PARKED[int(self.run_id)] = self
            raise
        with _lock:
            STATS["continues"] += 1
        return th

    def _continue(self) -> None:
        try:
            self.drive()
        except BaseException:  # pragma: no cover - drive() handles Exception itself
            log.exception("run %s segment died", self.run_id)

    def join(self, timeout: float | None = None) -> None:
        """Wait for the run's execution to end, across segments (batch joins)."""
        deadline = None if timeout is None else time.monotonic() + timeout
        while not self.done.is_set():
            th = self.thread
            remaining = None if deadline is None else max(0.0, deadline - time.monotonic())
            if th is not None and th is not threading.current_thread() and th.is_alive():
                th.join(remaining)
            elif self._parked:
                self.done.wait(min(0.2, remaining) if remaining is not None else 0.2)
            else:
                break
            if deadline is not None and time.monotonic() >= deadline:
                return


def _current_actor() -> str:
    try:
        from app.services.keys_common import current_actor

        return current_actor()
    except Exception:  # pragma: no cover
        return ""
