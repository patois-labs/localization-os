"""orchestrator: part4

Documented in: docs/architecture/translation-pipeline.md
"""

import logging
import threading
from datetime import datetime

from sqlmodel import Session, select

from app import config
from app import db as _db
from app.models import (
    Document,
    RunStatus,
    StepRun,
    StepType,
    TransMode,
    WorkflowRun,
    WorkflowStep,
)
from app.services import (
    refusal,
    tap_envelope,
)
from app.services.lqa_safety import LQA_EMPTY_SCOPE_PREFIX
from app.services.orchestrator_part2 import (
    _IN_FLIGHT_RUN_STATUSES,
    _RC_STEP_PREFIX,
    DEFAULT_MAX_VISITS,
    DEFAULT_TOTAL_STEP_CAP,
    QUEUE_LAUNCH_FAILED_NOTE,
    RUN_THREAD_PREFIX,
    STALE_NOTE_NO_SUCCESSOR,
    STALE_RUN_AFTER,
    SUPERSEDED_ABORT_NOTE,
    WRITE_CONTENTION_NOTE,
    ProjectBudgetExhausted,
    _bookkeeping_session,
    _caused_by_pool_timeout,
    _int_setting,
    _is_step_ref,
    _last_activity_at,
    _load_segments,
    _null_scope,
    _progress_pool_has_slot,
    _spec_for_step,
    _split_for_chunks,
    _stamp_note,
    _step_ref,
)
from app.services.orchestrator_terminology_backen import SEG_JOINER
from app.services.providers import (
    DeadEngineError,
    get_provider,
)

log = logging.getLogger("loc_tms.orchestrator")


class RunGuard:
    """Loop-safety + per-run scratchpad for the cursor machine (SPEC §4.2 / §3.3).

    Tracks, in WorkflowRun.context (resume-safe — survives a human pause), each step's
    visit count and the run's total step-execution count, and enforces D4's caps:
      - run-level total-step cap  -> HARD: enter() returns 'loop_cap_reached' -> FAILED.
      - per-step max_visits       -> SOFT: at_visit_cap() tells the branch seam to STOP
        looping back (fall through) so the run COMPLETES gracefully, flagged.
    On a NON-graph (plain / Phase-2) workflow the guard is a pure no-op: it never
    mutates run.context and never trips a cap (a linear run executes <= len(steps)
    steps, well under the run-level cap)."""

    def __init__(self, session: Session, run: WorkflowRun, active: bool):
        self.session = session
        self.run = run
        self.active = active
        self.default_max = _int_setting("workflow_max_visits", DEFAULT_MAX_VISITS)
        self.total_cap = _int_setting("workflow_total_step_cap", DEFAULT_TOTAL_STEP_CAP)
        ctx = dict(run.context or {})
        self.visits = dict(ctx.get("visits", {}))
        self.total = int(ctx.get("total_steps_run", 0))
        self.loop_ended = list(ctx.get("loop_ended", []))

    def _persist(self) -> None:
        """Reassign run.context (a JSON column is NOT in-place-mutation-tracked) so a
        following session.commit() persists the scratchpad. Only when active — a plain
        run leaves run.context NULL (byte-stable). enter() commits its own write inline
        (see there); at_visit_cap()'s loop_ended write rides the immediately-following
        end-of-step commit (no provider call sits between them, so it is never held)."""
        if not self.active:
            return
        ctx = dict(self.run.context or {})
        ctx["visits"] = dict(self.visits)
        ctx["total_steps_run"] = self.total
        if self.loop_ended:
            ctx["loop_ended"] = list(self.loop_ended)
        self.run.context = ctx
        self.session.add(self.run)

    def enter(self, step) -> str | None:
        """Count one execution of `step` at the top of the loop. Bumps the run total +
        this step's visit count, persists the scratchpad, and COMMITS it immediately.
        Returns 'loop_cap_reached' when the run-level total-step cap is EXCEEDED (the
        caller fails the run cleanly), else None. The 0-based visit index of this
        execution is via visit_index().

        QA-1: the commit here is LOAD-BEARING, not just tidiness. Without it the guard's
        `session.add(run)` (a JSON UPDATE workflowrun) stays dirty and the loop's next
        ORM read autoflushes it — `_eval_run_condition`'s query for a conditional step,
        or `tm.lookup_batch` for the order-0 translate step — which under SQLite takes
        the single WRITER lock UNCOMMITTED and holds it straight through the step's
        provider call. Inside that call spend.record_usage opens its own Session to
        write the ledger and deadlocks (`database is locked`). Committing the scratchpad
        BEFORE any provider dispatch releases the writer lock, closing the window for
        BOTH loops (FULL_TEXT + segmented) and every step kind uniformly. A no-op when
        inactive (a plain/linear run wrote nothing) -> byte-identical to today.

        CRASH-WINDOW: this makes the visit increment DURABLE at step START instead of at
        the step's end-of-step commit. It strictly IMPROVES the guard's crash-survival
        (the scratchpad is persisted sooner, and graceful pause/resume is unchanged — the
        pause path already committed, and resume advances the cursor past the paused
        step). The one behavioral change: a crash DURING a step's provider call now leaves
        that step's visit counted, so a manual re-run of the still-`RUNNING` run counts it
        again (net 2 for that step vs. 1 before). That is the SAFE direction for a
        loop-safety cap — over-counting can only make the total-step / max_visits ceilings
        trip EARLIER, never let a loop spin longer."""
        self.total += 1
        ref = _step_ref(step)
        self.visits[ref] = self.visits.get(ref, 0) + 1
        self._persist()
        if self.active:
            # Commit the scratchpad NOW so the uncommitted UPDATE workflowrun is never
            # held (via a later autoflush) across the step's provider call (QA-1). No-op
            # when inactive: _persist() added nothing, so there is nothing to commit and
            # a plain run stays byte-identical.
            self.session.commit()
        if self.active and self.total > self.total_cap:
            return "loop_cap_reached"
        return None

    def visit_index(self, step) -> int:
        """The 0-based visit number of the CURRENT execution of `step` (0 on the first
        visit, 1 on the second loop pass, ...). Written to StepRun.outcome_index; stays 0
        on a linear run (each step entered exactly once) -> byte-identical."""
        return max(0, self.visits.get(_step_ref(step), 1) - 1)

    def at_visit_cap(self, step) -> bool:
        """True iff `step` has ALREADY been executed its effective max_visits times, so a
        loop-back INTO it must be refused (the branch seam falls through instead, ending
        the loop). Records the graceful loop-end for the run timeline. Effective ceiling
        = the step's own max_visits, else the run-level default.

        THE `< 1` BELT (gate C round 1, L2). `add_step`/`update_step` refuse a max_visits
        below 1 on every supported surface, so a stored 0 is not reachable today -- but the
        ceiling is now consulted BEFORE a step executes (#94), and a 0/negative stored by a
        legacy row, a restore or a future importer would make the FIRST step of the
        workflow "already at its ceiling": zero steps run, every target stays empty and the
        run reports `completed`. A non-positive ceiling is therefore treated as UNSET and
        inherits the run-level default, exactly like NULL."""
        cap = step.max_visits if step.max_visits is not None else self.default_max
        if not isinstance(cap, int) or cap < 1:
            cap = self.default_max
        if self.visits.get(_step_ref(step), 0) >= cap:
            ref = _step_ref(step)
            if ref not in self.loop_ended:
                self.loop_ended.append(ref)
                self._persist()
            return True
        return False


def _spend_block_reason(step: WorkflowStep) -> str | None:
    """SPEC §2 spend gate (Phase 1 prerequisite; closes an earlier open bug). Returns
    the terminal 'Spend cap reached' message ONLY when the step's ENTIRE effective
    provider chain (primary + optional fallback) is spend-blocked; else None.

    This MUST mirror run_completion's per-TARGET chain semantics exactly, or Phase 1
    stops being a no-op: run_completion (providers.py) checks `spend.blocked_for` on
    EACH target in order and runs the FIRST unblocked one, so a capped primary with an
    exempt/under-cap FALLBACK still SUCCEEDS (via the fallback) — main COMPLETES that
    run. So the gate resolves the same target list and returns a reason only when NO
    target is runnable; if ANY target is exempt/unblocked it returns None (let
    run_completion run it, byte-identical to today). Applied ONLY to the llm_agent
    path: run_completion returns text=""+error on a fully-blocked llm_agent step, which
    the run already turns into a FAILED run with this exact reason — so the gate merely
    fails FAST (before fanning out per-segment calls), matching main's outcome, and
    gives Phase 3 loops a per-visit pre-check. (LQA is NOT gated: lqa.score_text
    soft-degrades a blocked judge to a clean no-LLM scorecard and COMPLETES on main, so
    gating it would diverge — the reviewer's byte-identical bar.)

    MockProvider is exempt (spend.blocked_for decides on the RESOLVED instance), so a
    keyless/offline install and every hermetic test are a no-op (None). Also a no-op
    when the cap is off. A dead engine:<id> ref raises DeadEngineError from
    get_provider — that is the step body's OWN failure path (it names the dead ref);
    the gate defers to it (returns None) rather than pre-empting it, so dead-ref
    behavior is unchanged, exactly as run_completion (which also does not catch it)."""
    from app.services import spend

    spec = _spec_for_step(step)
    # The SAME target list run_completion builds: primary, then fallback if present.
    targets = [spec.provider]
    if spec.fallback_provider:
        targets.append(spec.fallback_provider)
    reason = None
    for provider_name in targets:
        try:
            prov = get_provider(provider_name)
        except DeadEngineError:
            # Resolving a target raised (dead ref) — defer entirely to the step body's
            # own DeadEngineError path (run_completion does not catch it either).
            return None
        msg = spend.blocked_for(prov)
        if msg is None:
            # A runnable/exempt target exists -> run_completion would run it. NOT
            # blocked; byte-identical to today (no fail-fast).
            return None
        reason = msg  # this target is blocked; keep scanning the rest of the chain
    return reason  # every target blocked -> fail fast with the (last) cap message


def _latest_outcome_steprun(session: Session, run: WorkflowRun, ref: str):
    """The MOST-RECENT StepRun in THIS run carrying a Phase-1 outcome for `ref`:
    an LQA step (prev_lqa), a QE-gated step (prev_qe), or the step carrying a given
    key (`step:<key>`), whose durable gate/score is populated. 'Most recent' = highest
    StepRun.id — rows are inserted in execution order so id is monotonic with execution
    (resume-safe: the row is committed and survives the generator exit on a human pause;
    Phase-3-loop-safe: a later visit's row has a higher id; PARALLEL-safe: the join
    persists a group's members in order_index order inside ONE commit, so id stays
    monotonic with a DETERMINISTIC order — see `_run_parallel_group`). Returns the
    StepRun or None (no qualifying outcome yet)."""
    step_key = ref[len(_RC_STEP_PREFIX) :] if _is_step_ref(ref) else None
    if step_key is not None and not step_key:
        # `step:` with an empty key (adversarial review, LOW-7). Without this the
        # `(st.key or "") != step_key` test below MATCHES every KEYLESS step, so a
        # malformed ref would silently read some unrelated step's gate. Save-time
        # validation already refuses a blank key; this is the defense in depth, and it
        # fails to None (the caller's fail-open contract), never to a guess.
        return None
    rows = session.exec(
        select(StepRun, WorkflowStep)
        .where(StepRun.workflow_run_id == run.id)
        .where(WorkflowStep.id == StepRun.step_id)
        .order_by(StepRun.id.desc())
    ).all()
    for sr, st in rows:
        # An empty-scope skip is a known no-verdict outcome, not a missing check.
        # Return it for both ref forms so guards cannot revive a stale failure or
        # fail open and run a fixer on text that nothing judged. Ordinary conditional
        # skips retain their historical lookup semantics below.
        if (
            st.step_type == StepType.LQA
            and (ref == "prev_lqa" or (step_key is not None and st.key == step_key))
            and sr.status == RunStatus.SKIPPED
            and (sr.error or "").startswith(LQA_EMPTY_SCOPE_PREFIX)
        ):
            return sr
        if step_key is not None:
            if (st.key or "") != step_key:
                continue
            # A KEYED ref names ONE step, so the first row that matches IS that step's
            # LATEST visit -- and if that visit produced no outcome (a SKIPPED row: its
            # guard evaluated false, so it never ran), the honest answer is "no outcome",
            # not the gate from an EARLIER visit (adversarial review, MEDIUM-5). Falling
            # through used to hand a later guard a STALE verdict the named step did not
            # reach this time round -- in a correction loop, "the check failed" from a
            # previous iteration after the check was skipped entirely. Returning None
            # puts it on `_eval_run_condition`'s existing fail-open path (the step runs),
            # exactly as a never-run ref already behaves.
            #
            # SCOPED TO KEYED REFS ON PURPOSE. `prev_lqa`/`prev_qe` mean "the most recent
            # step OF A KIND", not one named step: a skipped LQA there must keep falling
            # through to the previous LQA, which is pre-parallel condwf behaviour and
            # stays byte-identical.
            return sr if (sr.gate is not None or sr.score is not None) else None
        elif (ref == "prev_lqa" and st.step_type != StepType.LQA) or (
            ref == "prev_qe" and not getattr(st, "qe_gate", False)
        ):
            continue
        if sr.gate is None and sr.score is None:
            continue
        return sr
    return None


def _seg_chunks(step: WorkflowStep, text: str) -> list[str]:
    """Chunk one segment's text for a step. A single segment is normally sub-
    MAX_CHUNK_TOKENS so this degenerates to [text]; _split_for_chunks only kicks
    in for a pathologically large segment (and only when chunk_policy != none)."""
    if step.chunk_policy == "none":
        return [text]
    return _split_for_chunks(text, config.MAX_CHUNK_TOKENS)


def _refuse_project_budget(session: Session, doc) -> None:
    """Eager per-project USD budget gate for a NEW workflow-run submission (CV-2),
    mirroring _refuse_archived: raise ProjectBudgetExhausted before any WorkflowRun row
    when the document's project is at 100% of its monthly spend cap. UNSCOPED by org on
    purpose -- the lookup is by the document's unique project_id (already the caller's
    own, visibility-confined at the surface), so there is exactly one budget row and no
    cross-org leak; passing None matches it whether stamped None or backfilled. A
    passing launch also evaluates the 80/100 threshold alert (idempotent)."""
    project_id = getattr(doc, "project_id", None)
    if project_id is None:
        return
    from app.services import project_budgets

    allowed, _remaining, msg = project_budgets.project_check(project_id)
    if not allowed:
        # H1: the blocked launch is the 100% crossing — alert before refusing.
        project_budgets.evaluate_thresholds(project_id, actor="system")
        raise ProjectBudgetExhausted(msg)
    project_budgets.evaluate_thresholds(project_id, actor="system")


def _queue_actor_scope(wf_id: int, locale: str, actor: str):
    """The KeyChangeLog actor scope a queued launch runs under — the same label the inline
    half of the fan-out stamps (`workflow:wf<id>:<locale> by <actor>`), rebuilt from the
    durable row so a write-back is attributed identically whether its run started inline or
    an hour later out of the queue."""
    try:
        from app.services.keys import actor_scope

        return actor_scope("workflow:wf%d:%s by %s" % (wf_id, locale, actor))  # noqa: UP031
    except Exception:  # pragma: no cover - attribution must never block a launch
        log.debug("could not build the queued launch actor scope", exc_info=True)
        return _null_scope()


def _mark_queue_launch_failed(run_id: int, exc: BaseException) -> None:
    """Record on the ROW that this queued launch never started, and why (HIGH-2).

    The sibling of `_run_in_background`'s crash handler, with the same note selection so one
    phrase means one thing everywhere: a pool timeout gets POOL_EXHAUSTED_NOTE, a lost writer
    lock gets WRITE_CONTENTION_NOTE (both retryable and both saying nothing was half-written),
    anything else gets QUEUE_LAUNCH_FAILED_NOTE with the bounded exception text. Written
    through `_bookkeeping_session`, which falls back to the pool-less engine, because the
    failure this most often records IS pool exhaustion — a pooled write here would time out
    too and leave the row queued forever with no reason, which is the exact silence GitHub
    #99/#100 were filed about."""
    try:
        with _bookkeeping_session() as s:
            run = s.get(WorkflowRun, run_id)
            if run is None or run.status not in (RunStatus.QUEUED, RunStatus.RUNNING):
                return  # cancelled or already terminal: never resurrect it to FAILED
            # Pool detection is the crash handlers' own rule (`_caused_by_pool_timeout`,
            # GitHub #350): bare, or the explicit `__cause__` of a wrapper, so a wrapped
            # pool timeout reads `pool_exhausted` here exactly as it does on a run crash.
            if _caused_by_pool_timeout(exc):
                note = _db.POOL_EXHAUSTED_NOTE
            elif isinstance(exc, _db.WriteContentionError):
                note = WRITE_CONTENTION_NOTE
            else:
                message = str(exc).strip().splitlines()[0] if str(exc).strip() else ""
                note = QUEUE_LAUNCH_FAILED_NOTE % f"{type(exc).__name__}: {message}"

            def stage():
                if run.status not in (RunStatus.QUEUED, RunStatus.RUNNING):
                    return
                run.status = RunStatus.FAILED
                run.finished_at = datetime.utcnow()
                if note[:400] not in (run.notes or ""):
                    _stamp_note(run, note[:400])
                s.add(run)

            _db.commit_with_retry(s, stage=stage)
    except Exception:
        log.exception("Could not record the failure of queued run %s", run_id)


def _sweep_stale_at_capacity(session, corpse, workflow_id: int, doc_id: int, locale: str) -> None:
    """FAIL a stale corpse even though the replacement launch is being refused (LOW-5).

    Best-effort, exactly like the successor-naming sweep on the admitted path: the refusal
    is the caller's answer either way, so a writer-lock loss here must never turn a clean
    503 into a 500. The row simply stays RUNNING for the next sweep."""
    try:

        def _stage():
            corpse.status = RunStatus.FAILED
            corpse.finished_at = datetime.utcnow()
            _stamp_note(corpse, STALE_NOTE_NO_SUCCESSOR)
            session.add(corpse)

        _db.commit_launch(session, stage=_stage, what=f"sweep stale run {corpse.id} at capacity")
        log.warning(
            "Swept stale run %s (wf=%s doc=%s %s) while at run capacity; no replacement started",
            corpse.id,
            workflow_id,
            doc_id,
            locale,
        )
    except Exception:
        log.warning(
            "Could not sweep stale run %s at capacity; leaving it for the next pass", corpse.id
        )
        session.rollback()


def _run_thread_alive(run_id) -> bool:
    """True while THIS PROCESS is executing `run_id` on a live background thread.

    The guard both sweepers check before failing a run (review F1). Scope is honest
    and deliberately narrow: it can only see this interpreter's threads, so a run
    owned by a SECOND process (a synchronous CLI `workflow run` against the same DB)
    is invisible here — that case is covered by the activity window instead. A False
    answer therefore means "not running HERE", never "definitely dead"; every caller
    pairs it with a second, activity-based test."""
    if run_id is None:
        return False
    needle = "%s%d-" % (RUN_THREAD_PREFIX, int(run_id))  # noqa: UP031
    return any(t.name.startswith(needle) for t in threading.enumerate() if t.is_alive())


def _write_progress_row(
    run_id, done: int, total: int, unit: str, at, step: str = "", step_index: int = 0
) -> bool:
    """Persist one progress tuple onto the run row; True only when a row was written.

    BEST-EFFORT TELEMETRY BY CONSTRUCTION: every failure (an exhausted pool, a locked DB
    under contention, a deleted run) is skipped or swallowed at debug, because a progress
    write must never fail, slow or alter a translation.

    CALLED ONLY FROM THE BACKGROUND WRITER THREAD (and from tests). It opens its own short
    Session -- never the run's, whose staged writes must not be committed early -- which is
    safe here precisely because it is a DIFFERENT thread: the run's thread never waits on
    this one, so a SQLite writer lock held by the run cannot deadlock with it (it just
    fails and is retried on the next tick).

    MONOTONIC BY TIMESTAMP. A write whose `at` is not newer than the row's own
    `progress_at` is dropped. That is what stops an in-flight background write from landing
    AFTER the step boundary's ORM stamp and clobbering the honest final count with an older
    one."""
    if not _progress_pool_has_slot():
        log.debug(
            "run progress persist skipped for run %s: no free connection (telemetry never "
            "waits on the pool)",
            run_id,
        )
        return False
    try:
        from app.db import engine

        with Session(engine) as s:
            run = s.get(WorkflowRun, run_id)
            if run is None:
                return False
            stamp = at or datetime.utcnow()
            if run.progress_at is not None and run.progress_at >= stamp:
                return False  # a newer publication already landed (see the docstring)
            run.progress_done = max(0, int(done))
            run.progress_total = max(0, int(total))
            run.progress_unit = str(unit or "")[:32]
            run.progress_step = str(step or "")[:64]
            run.progress_step_index = max(0, int(step_index or 0))
            run.progress_at = stamp
            s.add(run)
            s.commit()
            return True
    except Exception:
        # Telemetry only: a lost persist costs one refresh of a counter, never the run.
        log.debug("run progress persist failed for run %s", run_id, exc_info=True)
        return False


def _abort_if_superseded(session: Session, run: WorkflowRun) -> bool:
    """True when SOMETHING ELSE terminated this run's row while this thread was still
    executing it -- in which case the caller must write NOTHING and finish quietly.

    THE DEFECT THIS CLOSES (review F1b). A sweep is a DB write; it cannot stop a
    thread. So a swept run's thread carried on and, at the end, unconditionally set
    status = COMPLETED and flushed its segment write-back -- resurrecting a row that
    had been declared dead, stamping a COMPLETED run with "Interrupted by a server
    restart.", and racing the REPLACEMENT run's writes over the same segment set. Two
    runs, one segment set, last-writer-wins.

    The thread-liveness guard in both sweepers is the primary defence and makes this
    unreachable in-process; this is the second line, and the only one that covers a
    sweep from ANOTHER process.

    WHAT IT DOES NOT DO, precisely (review NEW-3):

      * It is a SINGLE POINT CHECK immediately before the write-back, not a lock. A
        cross-process sweep that lands DURING the write-back window is not caught --
        the flush is already running. In-process that window is unreachable (the
        thread guard means no sweeper will touch a run whose thread is alive), so
        this is the two-process case only, and closing it needs row-level locking or
        a claim token rather than a check.
      * It unwinds NOTHING that was already committed mid-run. StepRun rows, the
        spend ledger, TM USAGE COUNTERS (`tm.bump_usage`) and LLMemory captures are
        written as the run goes, and they stay: the provider calls really happened
        and the money was really spent, so erasing that record would be the
        dishonest option. What it prevents is the part that would be WRONG -- a
        FAILED row flipping itself to COMPLETED, and target_text / KeyChangeLog /
        TM entries landing on top of a newer run's.

    ALSO covers an operator CANCEL (run-cancel train): cancel_run flips a RUNNING run's
    row to CANCELLED from the request thread while this execution thread is still working.
    Without this check that thread would reach its terminal write-back and flip the row
    back to COMPLETED — resurrecting a run a person had just aborted (the same class of
    defect the FAILED case closes). A CANCELLED run already carries its own note +
    finished_at, so here we only DISCARD the write-back (no extra note); the FAILED case
    still stamps SUPERSEDED_ABORT_NOTE.

    Reads through a SEPARATE session so the answer is committed state rather than
    this session's identity map (which still holds the row as we last saw it)."""
    from app.db import engine

    with Session(engine) as probe:
        fresh = probe.get(WorkflowRun, run.id)
        if fresh is None or fresh.status not in (RunStatus.FAILED, RunStatus.CANCELLED):
            return False
        # Capture the reason BEFORE the session closes -- reading fresh.status after the
        # `with` exits raises DetachedInstanceError (the row is no longer bound).
        reason = fresh.status
        if reason == RunStatus.FAILED:
            _stamp_note(fresh, SUPERSEDED_ABORT_NOTE)
            probe.add(fresh)
            probe.commit()
    log.warning("Run %s was superseded mid-flight (%s); discarding its write-back", run.id, reason)
    return True


def _commit_run_terminal(session: Session, run: WorkflowRun, final_status: str) -> bool:
    """GUARDED terminal write-back (run-cancel train, LOW-1). Flip the run to `final_status`
    and commit its staged write-back, but ONLY while the row is still non-terminal
    (running/pending). Returns False -- rolling the write-back back -- when another actor
    made the run terminal first, so the finishing thread's COMPLETED can never RESURRECT a
    run that already moved on (a cancel, a recovery-sweep FAIL).

    THE WINDOW THIS CLOSES. `_abort_if_superseded` fires early (before the segment
    write-back); a cancel landing between it and this terminal commit would otherwise be
    overwritten by the thread's unconditional `UPDATE ... status = completed`. This re-reads
    the COMMITTED status through a SEPARATE session (a fresh transaction sees another
    connection's committed cancel; the run's own session may not, under SQLite's isolation)
    IMMEDIATELY before the terminal commit and aborts if the row is already terminal --
    exactly the pattern `_abort_if_superseded` uses, and the fallback the review blessed.
    The residual sliver between the re-read and the single fast ORM commit is microseconds
    (vs the whole write-back before); a true conditional-UPDATE CAS was tried first but its
    held SQLite write lock across the exec->commit gap regressed the concurrent-run suites
    ("database is locked"), so the proven separate-session re-check is used instead.

    Does NOT change the normal completion path: a run still RUNNING passes the guard, flips,
    and commits byte-identically to the old `run.status = ...; session.commit()`. Any segment
    write-back staged on this session (FULL_TEXT) is committed here on success and rolled
    back on abort; a segmented run's per-segment content was already committed incrementally
    and stands, exactly as `_abort_if_superseded` documents."""
    from app.db import engine

    with Session(engine) as probe:
        cur = probe.get(WorkflowRun, run.id)
        cur_status = cur.status if cur is not None else None
    if cur_status not in (RunStatus.RUNNING, RunStatus.PENDING):
        session.rollback()
        log.warning(
            "Run %s was made terminal by another actor (%s) while it was finishing; "
            "its terminal write-back was DISCARDED, not resurrected to %s",
            run.id,
            cur_status,
            final_status,
        )
        return False
    # THE FINALIZE RACE (fix/tap-finalize-race). Finalize the ENVELOPE in the SAME transaction
    # as the run's own terminal status commit below. Without this, the run committed terminal
    # here (inside the impl) while the TapAction/TapRun envelope stayed 'running' until the
    # wrapper's `finally` (_finalize_run_envelope) fired -- a window in which a status-polling
    # consumer (UI / API / MCP / the background async driver) could read a COMPLETED run whose
    # telemetry envelope was still 'running', and a crash in that window orphaned the envelope.
    # ORDER (N1): write run.status FIRST so the run row is DIRTY before we stage. The stage's
    # own `session.flush()` then writes the run as the connection's FIRST lock operation,
    # taking the SQLite writer lock from NONE (busy_timeout applies, so it rides out a briefly-
    # held writer lock). If we staged first, its SELECTs would take a SHARED lock and the run
    # write below would be a lock UPGRADE -- SQLITE_BUSY returns IMMEDIATELY on upgrades
    # (busy_timeout does NOT apply), aborting a completing run in ~0.02s under any held writer
    # lock. This mirrors _commit_run_terminal_direct, which writes run first for the same
    # reason. Safe w.r.t. the CAS (it already returned False above on a raced terminal).
    run.status = final_status
    run.finished_at = datetime.utcnow()
    session.add(run)
    # stage_run_finalize_in_session locates the leg and stages its (and its action's) terminal
    # status onto THIS session -- no new connection (all on `session`, riding the run's own
    # commit; a separate write here was PROVEN to deadlock a batch's concurrent member sessions
    # with 'database is locked'). It surfaces a caller-side flush failure as itself and guards
    # only its OWN envelope work (SAVEPOINT-isolated), so it is not wrapped here: an observer
    # must not swallow the CALLER's error into a lost-root-cause PendingRollbackError at the
    # commit below. TOTALS + enrichment stay on the wrapper's `finally`, the idempotent
    # reconciling backstop on every non-happy path.
    tap_envelope.stage_run_finalize_in_session(
        session,
        workflow_run_id=run.id,
        document_id=run.document_id,
        target_locale=run.target_locale,
        status=tap_envelope.status_for_run_status(final_status),
    )
    session.commit()
    return True


def _stamp_tag_repair_spend(session: Session, run: WorkflowRun, sink: list) -> None:
    """#596: fold the AI tag-repair rung's billed spend into the run's cost.

    The rung runs inside the write-back (`quality.commit_mt_output`), AFTER every StepRun
    was persisted, so its provider calls reached the ledger but no StepRun, and the run
    cost, the project budget and the reports (all sums of `StepRun.cost_usd`) missed
    them. `sink` is the `[cost_usd, tokens_in, tokens_out]` list the run's repairers filled
    (every repair and placement-verify call, rejected candidates included). The spend is
    ADDED to the run's last cost-bearing StepRun, so it is counted exactly once: a
    repairer's own certified `RepairResult.cost_usd` is never added on top. No-op when
    nothing was billed. Stages onto `session`; the caller's commit persists it."""
    cost, tin, tout = float(sink[0] or 0.0), int(sink[1] or 0), int(sink[2] or 0)
    if cost <= 0.0 and tin <= 0 and tout <= 0:
        return
    rows = session.exec(
        select(StepRun).where(StepRun.workflow_run_id == run.id).order_by(StepRun.id.desc())
    ).all()
    target = next((r for r in rows if r.cost_usd is not None), rows[0] if rows else None)
    if target is None:
        return
    target.cost_usd = (target.cost_usd or 0.0) + cost
    target.tokens_in = (target.tokens_in or 0) + tin
    target.tokens_out = (target.tokens_out or 0) + tout
    session.add(target)


# #596 review: run id -> the run's tag-repair spend sink `[cost, tokens_in, tokens_out, settled]`.
# The success path stamps the sink onto the terminal transaction (`settled` flips True once that
# commit lands). A run that crashes, fails or is cancelled AFTER paying for repairs never reaches
# it, so the run wrappers' `finally` calls `_settle_tag_repair_spend`, which stamps whatever is
# still unsettled from a bookkeeping session. Keyed by run id: one sink per run, never shared.
_TAG_SPEND_SINKS: dict[int, list] = {}
_TAG_SPEND_LOCK = threading.Lock()


def _register_tag_spend(run_id: int, sink: list) -> None:
    with _TAG_SPEND_LOCK:
        _TAG_SPEND_SINKS[run_id] = sink


def _settle_tag_repair_spend(
    run_id: int | None, session: Session | None = None, crashed: bool = False
) -> None:
    """Drop the run's sink; if it still holds spend the terminal commit never carried, stamp it
    now on an isolated session. Best-effort and never raises (a finalizer must not mask the
    run's own outcome)."""
    if run_id is None:
        return
    with _TAG_SPEND_LOCK:
        sink = _TAG_SPEND_SINKS.pop(run_id, None)
    if sink is None or (len(sink) > 3 and sink[3]):
        return
    if float(sink[0] or 0.0) <= 0.0 and int(sink[1] or 0) <= 0 and int(sink[2] or 0) <= 0:
        return
    try:
        if crashed and session is not None:
            # A crash mid write-back leaves the run's staged FULL_TEXT body uncommitted on its
            # session, which holds the SQLite writer lock; release it (the caller's own crash
            # handler rolls back too) or the isolated stamp below would be the lock victim.
            session.rollback()
        with _bookkeeping_session() as s:
            row = s.get(WorkflowRun, run_id)
            if row is None:
                return

            def _stage(_s=s, _row=row):
                _stamp_tag_repair_spend(_s, _row, sink)

            _db.commit_with_retry(s, stage=_stage)
            sink[3:] = [True]
    except Exception:
        log.exception("could not stamp tag-repair spend onto run %s", run_id)


def _is_stale_run(session: Session, run: WorkflowRun) -> bool:
    """True when `run` is a RUNNING row that has shown NO SIGN OF LIFE for longer
    than STALE_RUN_AFTER -- a corpse rather than work in flight.

    Measured from `_last_activity_at`, not from the row's age (review F2): a run
    resumed after an overnight human pause still carries an old creation time, and
    judging it on that swept a run that was executing at that instant. An unknown
    last-activity (None -- only reachable on a hand-made row) is treated as NOT
    stale, so missing information can never cost a live run its life."""
    if run.status != RunStatus.RUNNING:
        return False
    seen = _last_activity_at(session, run)
    if seen is None:
        return False
    return (datetime.utcnow() - seen) > STALE_RUN_AFTER


CANCEL_NOTE = "Run cancelled by an operator."


_CANCELLABLE_RUN_STATUSES = _IN_FLIGHT_RUN_STATUSES


#: The note stamped on a run an operator cancelled (run-cancel train), so the run page /
#: envelope can say WHY it is terminal (mirrors RESTART_NOTE / STALE_NOTE for the other
#: non-happy terminal paths).
#: Statuses a run may be cancelled FROM — the same in-flight set the authoring guards use
#: (workflows._ACTIVE_RUN_STATUSES). Anything else is already terminal and a cancel is a
#: clean no-op-refusal (RunCancelError), never a crash.
#:
#: QUEUED is in the set BY DERIVATION, not by a second list (gate A round 3, HIGH-3): a
#: fan-out's queued rows are cancellable through the ORDINARY cancel path on every surface
#: — the row goes CANCELLED and `_AdmissionScheduler._admit` re-reads the status under
#: `_RUN_CREATE_LOCK` and walks away, so a cancelled row is never executed. That is the
#: escape hatch round 2's in-memory queue did not have: cancelling the runs that WERE
#: executing simply fed the drainer the slots it freed.
class RunCancelError(Exception):
    """Raised by cancel_run when a run cannot be cancelled: it does not exist, or it is
    already terminal (completed / failed / cancelled). The caller (web / api / CLI / MCP)
    turns it into a clean 4xx — cancelling an already-finished run is a refusal, not a
    crash, and cancelling an already-cancelled run is idempotent-ish (a 409, not a 500)."""


def _replay_final_blob(session: Session, run: WorkflowRun, step_runs) -> str:
    """The persisted blob a COMPLETED run's replayed 'done' text is rebuilt from.

    #505 item 4. Normally the LAST step's output. But with TM-LQA exclusion on, a scoped LQA
    step stores only the units it JUDGED (a compact blob), and a fully-excluded LQA step is
    SKIPPED with an empty blob; the live `done` text carried EVERY segment, so replaying the
    last row gave reconnecting observers and `/api` wait=true a shorter (or empty) text. For a
    SEGMENTED document, when the last blob does not have one slot per segment, fall back to
    the newest COMPLETED step whose blob does (the text the LQA step judged over: an LQA step
    never changes it)."""
    if not step_runs:
        return ""
    last = step_runs[-1].output_text or ""
    doc = session.get(Document, run.document_id)
    if doc is None or doc.mode != TransMode.SEGMENTED:
        return last
    expected = len(_load_segments(session, run.document_id, run.target_locale))
    if not expected:
        return last
    if step_runs[-1].status == RunStatus.COMPLETED and len(last.split(SEG_JOINER)) == expected:
        return last
    for sr in reversed(step_runs):
        blob = sr.output_text or ""
        if sr.status == RunStatus.COMPLETED and len(blob.split(SEG_JOINER)) == expected:
            return blob
    return last


def _replay_done_text(session: Session, run: WorkflowRun, blob: str) -> str:
    """Reconstruct the terminal 'done' text a LIVE run emitted, from the persisted
    last-step output `blob`, for observe_run REPLAY (F4 display parity).

    The live 'done' event blanks a REFUSED/withheld unit's slot to "" (segmented:
    `emitted[j] = ""`; FULL_TEXT: `done_text = "" if refused`), while the persisted
    StepRun.output_text blob keeps the WITHHELD_OUTPUT marker at that slot — the C4
    per-step trace disposition, correct + unchanged on BOTH live and replay. So replay
    must blank exactly the withheld slots to match live.

    THE COLLISION THIS AVOIDS (adversarial-review LOW): a blind global
    `blob.replace(WITHHELD_OUTPUT, "")` also strips the phrase out of a LEGITIMATE
    committed translation whose own text equals/contains the marker string — is_refusal
    never flags the marker text itself, so such a translation commits normally and shows
    on the live 'done' verbatim, yet replay would silently blank/corrupt it (live/replay
    diverge for a COMPLETED run). Fix: key the blank to the WITHHELD SLOT, not the marker
    text. The blob is a SEG_JOINER-delimited join, one slot per segment in order_index
    order (FULL_TEXT = a single slot). A slot is genuinely withheld iff its stored value
    equals WITHHELD_OUTPUT AND the segment at that position carries a refusal qa_flag (the
    guard's own persisted per-segment record that it withheld this unit). A committed slot
    that merely equals the marker has NO refusal flag → left untouched → replays identical
    to live.

    run-190 round-3 (H-1): the OVER-TRANSLATION withhold has its OWN marker
    (WITHHELD_OUTPUT_OVER_TRANSLATION) and rides the `nontranslation` family, NOT the refusal
    family, so the original single-marker/single-family logic blanked it on neither path -- the
    marker leaked into the replayed `done` text and (via _drain_run_to_terminal) into the
    `/api` `wait=true` body, the CLI `--remote` run and the MCP run tool. Both the fast path
    and the slot test now know BOTH markers.

    run-190 round-4 (M-1): round 3 keyed the slot blank on the segment's LIVE qa_flag (a
    refusal flag / an over-translation `nontranslation` reason). But
    `quality.clear_over_translation_flag` REMOVES that flag on the next clean commit, so an OLD
    run replayed AFTER a later clean re-run of the same segment lost its evidence and the marker
    leaked AGAIN. The blank now keys on the DURABLE per-run evidence: the withhold marker at the
    slot in THIS run's persisted blob (which a later run, writing its OWN StepRun, cannot touch)
    PLUS the collision guard that the segment's committed `target_text` is not that same marker.
    A withheld unit never had target_text written (the withhold invariant), and a later clean run
    overwrites it with good text that still differs from the marker, so the blank survives the
    clear; a legitimate translation that committed the marker verbatim keeps target_text ==
    marker and is never blanked."""
    _markers = (
        refusal.WITHHELD_OUTPUT,
        refusal.WITHHELD_OUTPUT_OVER_TRANSLATION,
        refusal.WITHHELD_OUTPUT_GUARD,
    )
    if not any(m in blob for m in _markers):
        return blob  # fast path: nothing was withheld -> byte-identical to today
    slots = blob.split(SEG_JOINER)
    segments = _load_segments(session, run.document_id, run.target_locale)

    def _blank(slot: str, seg) -> bool:
        # run-190 round 4 (M-1): key the blank on the DURABLE per-run evidence stored WITH the
        # output -- the withhold marker at this slot in THIS run's persisted StepRun blob --
        # NOT on the segment's LIVE qa_flag. Round 3 keyed it on the segment's `nontranslation`
        # over-translation flag (and refusal flag), but `quality.clear_over_translation_flag`
        # REMOVES that flag the next time the segment commits cleanly (a LATER run over the same
        # unit). So an OLD run replayed AFTER a later clean commit lost its evidence and the
        # marker leaked back into the replayed `done` text and the `/api` wait=true drain (the
        # H-1 leak's sibling). The blob marker cannot be cleared by a later run (that run writes
        # its OWN StepRun), so it is the correct durable key.
        # COLLISION SAFETY kept WITHOUT the clearable flag: a withheld unit NEVER had target_text
        # written (the "output_blob is WITHHELD iff target_text was not written" invariant), so a
        # slot is genuinely withheld iff it equals a marker AND the segment's committed
        # target_text is not that SAME marker (the only way a LEGITIMATE committed translation
        # could equal the sentinel string). A later clean run OVERWRITES target_text with good
        # text, which still differs from the marker -> the blank SURVIVES the clear; a legit
        # translation that committed the marker verbatim keeps target_text == marker -> NOT
        # blanked, exactly as before.
        if slot not in _markers:
            return False
        return (getattr(seg, "target_text", "") or "") != slot

    if len(slots) == len(segments):
        out = [
            "" if _blank(slot, seg) else slot for slot, seg in zip(slots, segments, strict=False)
        ]
    else:
        # Defensive fallback (the last-step blob's slot count always equals the segment
        # count for both the segmented and FULL_TEXT paths). Blank ONLY slots whose whole
        # value equals EITHER marker -- never a global substring replace, which would corrupt
        # a legit slot that merely CONTAINS the phrase.
        out = ["" if slot in _markers else slot for slot in slots]
    return SEG_JOINER.join(out)
