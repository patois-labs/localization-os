"""batch_runs: shared helpers, second pass (layered pure move)

Documented in: docs/architecture/workflow-engine.md
"""

from __future__ import annotations

import logging
import threading
from collections import Counter
from contextlib import contextmanager
from datetime import datetime

from sqlmodel import Session, select

from app import config, db
from app.models import (
    BatchStatus,
    Document,
    Project,
    RunStatus,
    StepRun,
    Workflow,
    WorkflowBatch,
    WorkflowRun,
    WorkflowStep,
)
from app.services import spend, tap_envelope
from app.services import workflows as wf_service
from app.services.batch_runs_common import (
    _ACTIVE,
    BatchBudgetExhausted,
    BatchError,
    BatchMissing,
    _batch_result_session,
    _dispatcher_confirmed_dead,
    _dispatcher_owner,
    _members_by_locale_latest,
)

log = logging.getLogger("loc_tms.batch_runs")


# --- the document's runnable target locales ---------------------------------------
def document_target_locales(session: Session, doc: Document) -> list[str]:
    """The document's runnable target locales — the SAME set the single-run trigger
    validates a locale against (the distinct target_locale over the document's segments;
    orchestrator.document_has_no_content / the web+CLI+api single-run routes all gate on
    exactly this). For a normally-ingested document this equals the project's configured
    TargetLocale rows (segments are seeded per target at ingest), so a batch's default
    'all locales' covers precisely the project's targets that actually have content to run.
    Sorted for a stable order; excludes the sentinel '—'."""
    from app.models import Segment

    locales = sorted(
        {
            loc
            for loc in session.exec(
                select(Segment.target_locale).where(Segment.document_id == doc.id)
            ).all()
            if loc and loc != "—"
        }
    )
    return locales


def _norm_locale(code) -> str:
    from app.services import languages_catalog

    return languages_catalog.normalize(code)


def _stopped_locale_norms(session: Session, project_id) -> set[str]:
    """Normalized codes of the languages a connected project has stopped (empty for a plain
    project, so its default batch is unchanged)."""
    if project_id is None:
        return set()
    from app.models import TargetLocale

    return {
        _norm_locale(tl.locale)
        for tl in session.exec(
            select(TargetLocale).where(
                TargetLocale.project_id == project_id, TargetLocale.stopped_at.is_not(None)
            )
        ).all()
    }


# --- validation (reuses the single-run path's own posture) ------------------------
def _validate(
    session: Session, workflow_id: int, doc_id: int, locales: list[str] | None
) -> tuple[Workflow, Document, Project, list[str]]:
    """Validate the batch request the way the single-run trigger does, then resolve the
    effective locale list. RBAC / org / customer ring is enforced by the ROUTE (exactly as
    the single-run route enforces it before calling orchestrator.start_run) — this service
    validates the batch-specific inputs. Returns (workflow, doc, project, locales)."""
    doc = session.get(Document, doc_id)
    if doc is None:
        raise BatchError(f"no such document: {doc_id}")
    wf = session.get(Workflow, workflow_id)
    if wf is None:
        raise BatchError(f"no such workflow: {workflow_id}")
    project = session.get(Project, doc.project_id)
    # Archive gate (residual sweep): an archived workflow or project is BLOCKED from
    # launching a NEW batch (mirrors the single-run start_run guard). Raised as a
    # BatchError so every batch surface (web/CLI/api) renders it cleanly, like any other
    # batch-input refusal. In-flight/rerun of an already-running batch is not this path.
    if getattr(wf, "archived_at", None) is not None:
        raise BatchError(
            f"workflow {wf.id} ({wf.name}) is archived -- unarchive it before running "
            f"new translations."
        )
    from app.services import workflows as _wf_svc

    try:
        _wf_svc.refuse_if_workflow_customer_mismatch(session, workflow_id, doc=doc, project=project)
    except _wf_svc.WorkflowCustomerMismatchError as exc:
        raise BatchError(str(exc)) from exc
    if project is not None and getattr(project, "archived_at", None) is not None:
        raise BatchError(
            f"project {project.id} ({project.name}) is archived -- unarchive it before "
            f"running new translations."
        )
    # New submissions still obey the project's monthly spend cap.
    if doc.project_id is not None:
        from app.services import project_budgets

        _pb_ok, _pb_rem, _pb_msg = project_budgets.project_check(doc.project_id)
        if not _pb_ok:
            # H1: the blocked batch is the 100% crossing — alert before refusing.
            project_budgets.evaluate_thresholds(doc.project_id, actor="system")
            raise BatchBudgetExhausted(_pb_msg)  # M2: surfaces map to 429
        project_budgets.evaluate_thresholds(doc.project_id, actor="system")
    available = document_target_locales(session, doc)
    if not available:
        raise BatchError("this document has no translatable segments in any locale.")
    if locales is None:
        # default: ALL the document's target locales, except the ones a connected project
        # has stopped (explicitly naming one below is still allowed: a person asked for it).
        _stopped = _stopped_locale_norms(session, doc.project_id)
        chosen = [x for x in available if _norm_locale(x) not in _stopped]
        if not chosen:
            raise BatchError("every target locale of this document is stopped on its project.")
    else:
        # explicit list — validate EACH is a real configured target of THIS document,
        # de-duplicated, order-preserved (a bogus locale is refused, never silently run).
        seen: set[str] = set()
        chosen = []
        for loc in locales:
            loc = (loc or "").strip()
            if not loc or loc in seen:
                continue
            if loc not in available:
                raise BatchError(
                    f"locale {loc!r} is not a target of document #{doc_id} "
                    f"(targets: {', '.join(available) or 'none'})."
                )
            seen.add(loc)
            chosen.append(loc)
    if not chosen:
        raise BatchError("select at least one target locale to run.")
    return wf, doc, project, chosen


def _plan(
    session: Session, wf: Workflow, project: Project, locales: list[str]
) -> list[tuple[str, int]]:
    """[(locale, resolved_workflow_id)] — the per-locale variant dispatch would run.
    resolve_variant per locale (an ungrouped workflow resolves to itself — the sacred
    legacy path; a grouped one picks its exact-pair / default variant)."""
    src = project.source_locale if project else None
    cust = getattr(project, "customer_id", None)
    return [
        (loc, wf_service.resolve_variant(session, wf.id, loc, src, doc_customer_id=cust))
        for loc in locales
    ]


# --- member run helpers (in-flight state queried per launch) ----------------------
def _any_member_failed(session: Session, batch_id: int) -> bool:
    """True if any member of this batch has already reached FAILED — the fail_fast gate."""
    return (
        session.exec(
            select(WorkflowRun.id).where(
                WorkflowRun.batch_id == batch_id, WorkflowRun.status == RunStatus.FAILED
            )
        ).first()
        is not None
    )


def _active_run_elsewhere(
    session: Session, workflow_id: int, doc_id: int, locale: str, batch_id: int
) -> bool:
    """True if an ACTIVE run on this (document, locale) already exists under ANY
    workflow — the precise 'this exact segment set is already in flight' test. Such a
    locale is SKIPPED with a note; we never adopt the run into the batch.

    GitHub #213 part 2 widened this from SAME-workflow-only to ANY workflow, matching
    the launch guard: a run of a DIFFERENT workflow on the same (document, locale)
    writes back over one segment set, and the batch layer's answer for a busy locale
    is skip + report ("Skipped (already running elsewhere): ..." in the batch notes),
    never a rival member. The test stays per-locale, so two locales that resolve to
    the same universal variant still do NOT falsely collide. `workflow_id` is retained
    for the call-site/probe signature; it deliberately no longer narrows the query.

    Blocks on ANY active run for the pair, INCLUDING a member of THIS batch created by a
    CONCURRENT dispatcher (a double-clicked 'Re-run', two overlapping retry-failed calls).
    An earlier version excluded same-batch members; that left a same-batch double-run open:
    two reruns that both cleared rerun_batch's pre-dispatch active check would EACH create a
    second RUNNING member for the locale under _RUN_CREATE_LOCK, because neither dispatcher
    could see the other's just-committed sibling. Excluding same-batch members was never
    needed for the INITIAL dispatch — a locale is launched exactly once per plan, so no
    same-batch sibling for that locale exists at check time — so blocking it is pure gain:
    check-and-create serialized by _RUN_CREATE_LOCK now makes 'never double-run a segment
    set' hold for reruns INTO one batch, not only across batches. `batch_id` is retained for
    the call-site/probe signature; it deliberately no longer narrows the query.

    THE SAME STALENESS FLOOR A SINGLE LAUNCH HONOURS (GitHub #213 fix round 1): a rival row
    is skipped only on the floor's own three conditions -- a non-batch RUNNING row with no
    live thread in this process, past STALE_RUN_AFTER (`_is_stale_run` is True for RUNNING
    only, so PAUSED/PENDING/QUEUED rivals and live threads always hold the door). Without
    it a 7h-old corpse skipped its locale in a batch while a single launch on the very
    same row went straight through -- one segment set, two answers."""
    from app.services import orchestrator as orch

    rows = session.exec(
        select(WorkflowRun)
        .where(
            WorkflowRun.document_id == doc_id,
            WorkflowRun.target_locale == locale,
            WorkflowRun.status.in_(_ACTIVE),
        )
        .order_by(WorkflowRun.id)
    ).all()
    for run in rows:
        if (
            run.batch_id is None
            and not orch._run_thread_alive(run.id)
            and orch._is_stale_run(session, run)
        ):
            continue
        return True
    return False


def _create_member(
    session: Session,
    workflow_id: int,
    doc_id: int,
    locale: str,
    created_by: str,
    batch_id: int,
    *,
    ignore_fail_fast=False,
) -> int:
    """Persist one QUEUED batch member without acquiring an execution slot.

    The caller holds the shared creation lock around its active-member check.
    """
    run = WorkflowRun(
        workflow_id=workflow_id,
        document_id=doc_id,
        target_locale=locale,
        created_by=created_by or "",
        status=RunStatus.QUEUED,
        current_step_index=0,
        batch_id=batch_id,
        queue_group=f"batch-retry:{batch_id}" if ignore_fail_fast else f"batch:{batch_id}",
    )

    # Retry transient writer contention. The dispatch seam records a failed member
    # if staging still fails, or propagates an error if no durable outcome can be written.
    def _stage_member_insert():
        session.add(run)

    # keep_loaded=True (fold-in): read run.id without a post-commit refresh() SELECT (which
    # is on the launch hot path and could itself lose the writer lock).
    db.commit_with_retry(session, stage=_stage_member_insert, keep_loaded=True)
    return run.id


def _run_member(engine, run_id: int, workflow_id: int, doc_id: int, locale: str, slot=None) -> None:
    """Drain one member run to a terminal/paused state SYNCHRONOUSLY on the caller's
    thread, with its own Session (the repo ORM rule). Mirrors _run_in_background._work:
    the DB rows it writes are the source of truth; a crash marks the run FAILED so it
    never hangs the batch as a phantom-RUNNING member.

    The scheduler supplies one counted slot for this execution. Its thread wrapper
    releases the token on exit. Direct callers take and release their own token.
    """
    from app.services import orchestrator as _orch
    from app.services.orchestrator import RUN_SLOTS, run_workflow_stream

    token = slot if slot is not None else RUN_SLOTS.take()[0]
    try:
        with Session(engine) as s:
            # is_resume=False (R1): a batch member run_id is a FRESH leg pre-created by
            # _create_member, NOT a resume. Without this, `run_id is not None` would
            # misclassify it as a resume and -- with SQLite rowid reuse on the same
            # (doc, locale) -- the leg could adopt a deleted run's envelope.
            for _ev in run_workflow_stream(
                s,
                workflow_id,
                doc_id,
                locale,
                run_id=run_id,
                is_resume=False,
                capacity_slot=token,
            ):
                pass
    except Exception as exc:
        log.exception("Batch member run (wf=%s doc=%s loc=%s) crashed", workflow_id, doc_id, locale)
        try:
            # _bookkeeping_session, not a second pooled Session: when the crash IS pool
            # exhaustion (#100), a pooled session here times out too and the member is left
            # RUNNING forever with the batch never finalizing — the same silence the plain
            # run path hit. The pool-less fallback makes the honest FAILED marking land.
            from app.services.orchestrator import _bookkeeping_session

            with _bookkeeping_session() as s:
                run = s.get(WorkflowRun, run_id)
                # CANCELLED joins COMPLETED/PAUSED here for the same reason it does in
                # orchestrator._run_in_background: an operator cancel that raced a crashing
                # member must stay cancelled, never be resurrected to FAILED.
                if run and run.status not in (
                    RunStatus.COMPLETED,
                    RunStatus.PAUSED,
                    RunStatus.CANCELLED,
                ):
                    # GitHub #106 (gate F round 1, H2): STAMP WHY. This is the sibling of
                    # orchestrator._run_in_background's crash handler, and it is the path
                    # #106's own evidence names -- a per-segment `engines.get_profile`
                    # raising sqlalchemy TimeoutError under pool exhaustion (#100) is
                    # neither DeadEngineError nor UnderTranslationError, so it escapes
                    # run_workflow_stream and a BATCH member lands here. Without a note the
                    # member ends `failed` with reason null, failure_shape [], step_states []
                    # and warnings [] -- the exact reasonless shape #106 was filed about.
                    # Same note selection, in the same order, as the sibling (GitHub #350:
                    # the earlier cut also stamped the crash note on a pool timeout, so a
                    # member crashed on pool exhaustion reported `run_crashed` while a
                    # plain run ended reasonless). Pool timeout -> POOL_EXHAUSTED_NOTE,
                    # retryable write contention -> WRITE_CONTENTION_NOTE, anything else
                    # -> the bounded crash text; one phrase means one thing across plain
                    # runs, batches and jobs, and `run_reason` turns it into
                    # `pool_exhausted` / `write_contention` / `run_crashed` on every
                    # surface. Guarded against a double append the way the sibling's
                    # _stage is.
                    note = _orch._crash_terminal_note(exc)
                    if note not in (run.notes or ""):
                        _orch._stamp_note(run, note)
                    run.status = RunStatus.FAILED
                    run.finished_at = datetime.utcnow()
                    s.add(run)
                    s.commit()
        except Exception:
            log.exception("Failed to mark batch member run failed after crash")
    finally:
        if slot is None:
            token.release()
        # #101 hygiene (gate F round 1, L1): this executor has no ticker step-boundary of its
        # own on a hard crash, and the member runs on a long-lived dispatcher thread.
        _orch._progress_forget(run_id)


# --- the dispatcher core (shared by web daemon + CLI inline) ----------------------
def _dispatch(
    batch_id: int,
    plan: list[tuple[str, int]],
    doc_id: int,
    created_by: str,
    *,
    ignore_fail_fast: bool = False,
    synchronous: bool = False,
) -> None:
    """Persist every member before notifying the shared admission scheduler.

    Kept as the common initial/rerun seam. It never waits or starts a dispatcher.
    Synchronous callers wait separately, after releasing their request Session.
    """
    from app.db import engine
    from app.services import orchestrator as orch

    skipped = []
    with Session(engine) as s, orch._RUN_CREATE_LOCK:
        for locale, workflow_id in plan:
            if _active_run_elsewhere(s, workflow_id, doc_id, locale, batch_id):
                skipped.append((locale, "already-running"))
                continue
            try:
                _create_member(
                    s,
                    workflow_id,
                    doc_id,
                    locale,
                    created_by,
                    batch_id,
                    ignore_fail_fast=ignore_fail_fast,
                )
            except Exception as exc:
                s.rollback()
                # The plan still has a visible outcome when staging this member fails.
                # If even this write fails, propagate the request error rather than
                # acknowledging a member that does not exist.
                row = WorkflowRun(
                    workflow_id=workflow_id,
                    document_id=doc_id,
                    target_locale=locale,
                    created_by=created_by,
                    batch_id=batch_id,
                    queue_group=f"batch-retry:{batch_id}"
                    if ignore_fail_fast
                    else f"batch:{batch_id}",
                    status=RunStatus.FAILED,
                    finished_at=datetime.utcnow(),
                    notes=orch.QUEUE_LAUNCH_FAILED_NOTE % str(exc)[:200],
                )
                s.add(row)
                db.commit_launch(s, keep_loaded=True, what="record failed batch queue member")
        batch = s.get(WorkflowBatch, batch_id)
        if skipped:
            batch.notes = _notes(
                list(batch.requested_locales or []), {}, [loc for loc, _ in skipped], {}
            )
            s.add(batch)
            s.commit()
    from app.services import audit

    with Session(engine) as s:
        rows = s.exec(select(WorkflowRun).where(WorkflowRun.batch_id == batch_id)).all()
        queued_ids = [r.id for r in rows if r.status == RunStatus.QUEUED]
        failed_ids = [r.id for r in rows if r.status == RunStatus.FAILED]
    audit.log(
        actor=created_by,
        action="workflow.batch",
        target=f"batch {batch_id}",
        detail=f"launched=0 queued={len(queued_ids)} queued_runs={queued_ids} "
        f"failed_runs={failed_ids}",
        customer_id=audit.customer_for_document(doc_id),
    )
    if not synchronous:
        orch.ADMISSION.ensure_started()
        orch.ADMISSION.notify()


_BATCH_STATE_LOCK = threading.RLock()


_BATCH_FINISH_LOCK = threading.RLock()


_DISPATCH_INFLIGHT: Counter = Counter()


_FINALIZING: set[int] = set()


_FINALIZE_DONE_EVENTS: dict[int, threading.Event] = {}


FINALIZE_DONE = "done"


FINALIZE_OWNED = "owned_by_other"


FINALIZE_NOT_READY = "not_ready"


_SYNC_FINALIZE_ATTEMPTS = 3


_SYNC_FINALIZE_SLEEP_S = 0.05


_SYNC_OWNER_WAIT_S = max(1.0, config.DB_POOL_TIMEOUT_S) * 2


DRAIN_TIMEOUT_NOTE = (
    "Synchronous wait deadline expired; unfinished members remain queued "
    "or active without a local thread."
)


_MEMBER_THREADS = {}


_MEMBER_THREADS_LOCK = threading.Lock()


# --- Locking discipline (fix train A, round 12) --------------------------------------
# _BATCH_STATE_LOCK and _BATCH_FINISH_LOCK protect IN-MEMORY state transitions ONLY. Under
# EITHER lock a thread MUST NOT: check out a pooled connection, open any Session, join a
# thread, call _dispatch, tap_envelope.begin_batch_action / finalize_batch_action, audit.log
# or any other envelope I/O. The shape EVERYWHERE is: take the lock, read and mutate the
# in-memory set, snapshot what the DB write needs, RELEASE the lock, then do the DB work
# with the snapshot. A thread never holds a connection while waiting on a lock, and never
# holds a lock while waiting on a connection. This is the class of the 2026-09-06 outage
# (#99/#100): a thread that pins a connection while it queues behind a mutex converts lock
# contention into pool exhaustion. Pinned by
# tests/verify_stress_liveness_pool.py::test_round9_no_checkout_under_batch_locks.
#
# _BATCH_STATE_LOCK guards _DISPATCH_INFLIGHT -- batch ids whose members are being created
#   right now (an initial start_batch dispatch or a rerun reopen). A batch in this set MUST
#   NOT be finalized: its fresh QUEUED members do not exist yet, so a finalizer would see
#   "no active member" and close a batch that is really just starting. A dispatch adds the
#   id BEFORE the batch becomes finalize-eligible (before the INSERT is published, or before
#   a reopen clears finished_at) and removes it AFTER the members are committed. _finalize
#   re-checks the set immediately before its terminal write, so any finalizer that observed
#   a just-cleared finished_at also observes the in-flight mark the reopen set first.
# _BATCH_FINISH_LOCK guards _FINALIZING -- batch ids a thread is finalizing right now, so
#   the admission daemon's sweep and a synchronous wait never do the terminal DB work for
#   one batch twice. Single-finalize CORRECTNESS is enforced at the DB level (the
#   conditional terminal UPDATE, guarded on finished_at IS NULL AND no active member); this
#   in-memory set only removes redundant work and never gates correctness.
#: batch ids with a dispatch window (initial launch or reopen) open right now -- a REFCOUNT,
#: not a set (gate round 10 LOW-4): the mark is added before the members exist and removed
#: after they commit, and if two windows for the SAME id were ever open at once (two racing
#: reruns) a set would let the first to finish clear the mark while the second is still
#: creating members, and a finalizer could then close the batch mid-reopen -- the exact
#: window the mark exists to protect. Counting means a window releases the mark only when the
#: LAST open window for that id finishes; a key is deleted at zero so `id in _DISPATCH_INFLIGHT`
#: still reads as "a window is open". Guarded by _BATCH_STATE_LOCK.
#: The terminal-transition completion signal for each batch a thread is finalizing right now
#: (gate round 10 HIGH-2): the owning finalizer creates this Event when it claims the batch
#: and sets + pops it when it releases the claim, AFTER the terminal stamp commits. A
#: synchronous waiter that finds the claim already held (FINALIZE_OWNED) waits on this Event
#: instead of mistaking "another thread owns it" for "not ready". Guarded by _BATCH_FINISH_LOCK.
#: _finalize outcomes (gate round 10 HIGH-2). The terminal transition is TRI-state and the
#: synchronous waiter must not collapse "another thread owns it" into "not ready": collapsing
#: them made an all-COMPLETED batch report drain_incomplete whenever a concurrent finalizer
#: held the claim for longer than the old 3 x 0.05 s budget.
# terminal now: this call stamped it, or it was already terminal / gone
# another thread holds the claim and has not landed it yet
# a member is still active, or a dispatch is in flight
#: Bounded retries when the unlocked reconcile read and the locked finalize disagree (a
#: member reappearing between the two): a few passes with a short sleep, then record the
#: honest incomplete state instead of spinning (gate A round 9 LOW-2).
#: How long a synchronous waiter blocks on the owning finalizer's completion signal before it
#: stops waiting and records the honest incomplete state (gate round 10 HIGH-2). The owner
#: reaches the UNBOUNDED bookkeeping engine (HIGH-1), so it lands as fast as the DB answers;
#: this bound is a small multiple of the per-checkout pool timeout so a genuinely wedged owner
#: cannot pin the caller forever. It is a PER-ATTEMPT bound: _sync_wait_error can wait it once
#: per pass, so a wedged owner is waited on up to _SYNC_FINALIZE_ATTEMPTS times, and the tail
#: past QUEUE_DRAIN_WAIT_S is cumulative (up to 3 x _SYNC_OWNER_WAIT_S). No pooled connection is
#: held across the wait. See docs/architecture/configuration.md "Queue admission and emergency
#: relief" and gate round 11 LOW-1/LOW-2.
def run_queued_member(run_id, workflow_id, doc_id, locale, token):
    """Start one admitted execution; the shared scheduler owns all waiting."""
    from app.db import engine

    def work():
        try:
            _run_member(engine, run_id, workflow_id, doc_id, locale, slot=token)
        finally:
            token.release()

    thread = threading.Thread(target=work, name=f"loc-batch-member-{run_id}", daemon=True)
    try:
        token.handed_off = True
        with _MEMBER_THREADS_LOCK:
            # Completed async members need no further join and must not accumulate.
            for rid, previous in list(_MEMBER_THREADS.items()):
                if not previous.is_alive():
                    _MEMBER_THREADS.pop(rid, None)
            _MEMBER_THREADS[run_id] = thread
            thread.start()
    except BaseException:
        with _MEMBER_THREADS_LOCK:
            _MEMBER_THREADS.pop(run_id, None)
        token.release()
        raise


SYNC_POOL_NOTE = "pool saturated during synchronous wait;"


SYNC_INTERNAL_NOTE = "synchronous wait internal error: "


SYNC_INTERNAL_END = "\n[end synchronous wait error]"


SYNC_RECONCILE_S = 2.0


def _clear_wait_notes(notes):
    import re

    notes = re.sub(
        re.escape(SYNC_INTERNAL_NOTE) + r".*?" + re.escape(SYNC_INTERNAL_END),
        "",
        notes or "",
        flags=re.DOTALL,
    )
    return (
        re.sub(
            re.escape(SYNC_POOL_NOTE) + r" \d+ member\(s\) still queued",
            "",
            notes.replace(DRAIN_TIMEOUT_NOTE, ""),
        ).strip()
        or None
    )


@contextmanager
def _batch_session():
    from app.services import bookkeeping

    with bookkeeping.session() as session:
        yield session


def _release_dispatch_inflight(batch_id):
    """Drop one dispatch-window refcount for batch_id, deleting the key at zero (gate round
    10 LOW-4). Caller holds _BATCH_STATE_LOCK. Deleting at zero keeps `id in _DISPATCH_INFLIGHT`
    meaning 'a window is open', so the membership checks in _finalize stay correct."""
    _DISPATCH_INFLIGHT[batch_id] -= 1
    if _DISPATCH_INFLIGHT[batch_id] <= 0:
        del _DISPATCH_INFLIGHT[batch_id]


def _await_finalize_owner(batch_id):
    """Block until the thread that OWNS batch_id's terminal transition lands it, bounded.

    Gate round 10 HIGH-2. A synchronous waiter that observed FINALIZE_OWNED must not treat it
    as "not ready": the batch may have completed and another finalizer is mid-transition. It
    waits on that finalizer's per-batch completion Event -- set in _finalize's finally the
    instant it releases the claim, AFTER the terminal stamp commits -- bounded by
    _SYNC_OWNER_WAIT_S so a wedged owner cannot pin the caller. If the owner already landed
    (the Event is gone) this returns at once and the caller re-reads finished_at."""
    with _BATCH_FINISH_LOCK:
        done_event = _FINALIZE_DONE_EVENTS.get(batch_id)
    if done_event is not None:
        done_event.wait(_SYNC_OWNER_WAIT_S)


def _join_batch_members(ids):
    with _MEMBER_THREADS_LOCK:
        threads = [(rid, _MEMBER_THREADS.get(rid)) for rid in ids]
    joined = False
    for rid, thread in threads:
        if thread is not None:
            thread.join()
            joined = True
            with _MEMBER_THREADS_LOCK:
                if _MEMBER_THREADS.get(rid) is thread:
                    _MEMBER_THREADS.pop(rid, None)
    return joined


def _sync_batch_result(session, batch_id, error):
    with _batch_result_session(session):
        session.expire_all()
        result = session.get(WorkflowBatch, batch_id)
        if result is None:
            raise BatchMissing(batch_id)
        result.__dict__["drain_error"] = error
        return result


def queue_member_allowed(session, run):
    """Admission-time batch width, fail-fast and spend gates, on durable members."""
    batch = session.get(WorkflowBatch, run.batch_id)
    if batch is None:
        return False
    active = session.exec(
        select(WorkflowRun.id).where(
            WorkflowRun.batch_id == batch.id,
            WorkflowRun.status.in_((RunStatus.RUNNING, RunStatus.PENDING)),
        )
    ).all()
    if len(active) >= batch.max_parallel:
        return False
    blocked, spent, cap = spend.would_block()
    stop = (
        {"reason": "spend", "spent": spent, "cap": cap}
        if blocked
        else {"reason": "fail_fast"}
        if batch.fail_fast
        and not (run.queue_group or "").startswith("batch-retry:")
        and _any_member_failed(session, batch.id)
        else {}
    )
    if stop:
        from sqlalchemy import update

        from app.services import orchestrator as orch

        note = _notes([], {}, [], stop)
        policy_note = (
            orch.SPEND_CAP_STOP_NOTE if stop["reason"] == "spend" else orch.FAIL_FAST_STOP_NOTE
        )
        changed = False
        for row in session.exec(
            select(WorkflowRun).where(
                WorkflowRun.batch_id == batch.id, WorkflowRun.status == RunStatus.QUEUED
            )
        ).all():
            if stop["reason"] == "fail_fast" and (row.queue_group or "").startswith("batch-retry:"):
                continue
            result = session.exec(
                update(WorkflowRun)
                .where(WorkflowRun.id == row.id, WorkflowRun.status == RunStatus.QUEUED)
                .values(
                    status=RunStatus.FAILED,
                    finished_at=datetime.utcnow(),
                    notes=((row.notes or "") + " " + policy_note).strip(),
                )
                .execution_options(synchronize_session=False)
            )
            changed = changed or result.rowcount > 0
        if changed:
            batch.notes = ((batch.notes or "") + " " + note).strip()
            session.add(batch)
            session.commit()
        return False
    return True


def _rollup_status(requested: list[str], latest: dict[str, WorkflowRun]) -> str:
    """The rollup BatchStatus from the latest-per-locale member rows vs the request.
    Shared by _finalize (initial + re-run dispatch) and the startup healer so the two
    can never drift: no coverage / short coverage -> PARTIAL; full coverage all-COMPLETED
    -> COMPLETED; full coverage all-FAILED -> FAILED; full coverage mixed -> PARTIAL.
    Derived from the LATEST attempt per locale, so a re-run that turns a FAILED locale
    COMPLETED re-rolls the whole batch honestly (the superseded FAILED run never counts)."""
    from app.services import orchestrator as orch

    if any(
        (orch.run_reason(m) or {}).get("code") in ("spend_cap_stop", "fail_fast_stop")
        for m in latest.values()
    ):
        return BatchStatus.PARTIAL
    n = len(latest)
    if n == 0 or n != len(requested):
        return BatchStatus.PARTIAL
    n_completed = sum(1 for m in latest.values() if m.status == RunStatus.COMPLETED)
    n_failed = sum(1 for m in latest.values() if m.status == RunStatus.FAILED)
    if n_completed == n:
        return BatchStatus.COMPLETED
    if n_failed == n:
        return BatchStatus.FAILED
    return BatchStatus.PARTIAL


def _notes(
    requested: list[str], latest: dict[str, WorkflowRun], skipped_locales: list[str], stop: dict
) -> str:
    """A human-readable explanation of any early stop / skip, for WorkflowBatch.notes.
    Empty string when the batch ran clean to full coverage. `unlaunched` is derived from
    the DB (requested locales with NO member at all), so it stays correct across a re-run
    that later launches a locale that was un-launched on the first dispatch.
    Admission calls this with a spend/fail-fast stop to persist the policy note;
    finalization calls it with an empty stop to add the coverage rollup."""
    parts: list[str] = []
    launched_locales = set(latest.keys())
    unlaunched = [
        loc for loc in requested if loc not in launched_locales and loc not in skipped_locales
    ]
    reason = stop.get("reason")
    if reason == "spend":
        parts.append(
            "Spend cap reached (${:.2f} of ${:.2f}) — stopped launching further locales.".format(
                stop.get("spent", 0.0), stop.get("cap", 0.0)
            )
        )
    elif reason == "fail_fast":
        parts.append(
            "Fail-fast: a member failed, so no further locales were launched "
            "(in-flight members were allowed to finish)."
        )
    if skipped_locales:
        parts.append("Skipped (already running elsewhere): " + ", ".join(skipped_locales) + ".")
    if unlaunched:
        parts.append("Not launched: " + ", ".join(unlaunched) + ".")
    return " ".join(parts)


# --- startup heal (restart-interrupted batches) -----------------------------------
def recover_interrupted_batches(*, at_boot: bool = False, recover_orphaned: bool = False) -> int:
    """Called at startup (alongside jobs.recover_interrupted): a batch whose dispatcher
    thread was cut off by a server restart is left with finished_at NULL and its members
    stuck RUNNING — so batch_status() reads RUNNING forever and the batch page auto-reloads
    without end. The in-memory dispatcher is gone (single-process; nothing is executing at
    startup), exactly the assumption jobs.recover_interrupted makes for the job queue.

    For each unfinished batch: fail its ORPHANED active members (RUNNING/PENDING -> FAILED,
    finished_at stamped; a genuinely PAUSED member is a durable human-review state and is
    LEFT untouched, like a paused single run), then finalize the batch (recompute the rollup
    from the healed member rows + stamp finished_at + a batch note: "Interrupted by a server
    restart." at boot, or "dispatcher exited; N member(s) failed" for the manual sweep).
    Scoped to WorkflowBatch rows + THEIR members (batch_id set), so a plain single
    run is never touched HERE — that gap (GitHub #44) is now closed by its own sibling
    healer, `orchestrator.recover_interrupted_runs` (`batch_id IS NULL`), which app/main.py
    runs immediately after this one and `loc-tms db recover-jobs` runs alongside it. The
    two are deliberately disjoint by `batch_id`, so no run is healed twice.
    A bare call is non-destructive. The CLI opts into recover_orphaned: only a confirmed-dead
    owning process on this host permits recovery. Live, remote-host and legacy/unknown owners
    remain untouched. Startup explicitly opts into the no-live-worker assumption with at_boot.
    QUEUED members survive for admission and keep their batch unfinished. At boot their
    batch adopts this process owner before the admission scheduler resumes.
    Returns the count finalized."""
    if not at_boot and not recover_orphaned:
        return 0

    # Lazy, like every other orchestrator use here (import cycle): _stamp_note +
    # RESTART_NOTE are the shared "why did this run end" helpers (#106).
    from app.services import orchestrator as _orch

    touched = 0
    with _batch_session() as s:
        batches = s.exec(select(WorkflowBatch).where(WorkflowBatch.finished_at.is_(None))).all()
        for batch in batches:
            if not at_boot and not _dispatcher_confirmed_dead(batch.dispatcher_owner):
                continue
            recovery_note = (
                _orch.RESTART_NOTE
                if at_boot
                else _orch._crash_note(RuntimeError("Dispatcher process exited."))
            )
            members = s.exec(select(WorkflowRun).where(WorkflowRun.batch_id == batch.id)).all()
            members = list(members)
            members.sort(key=lambda m: m.id or 0)  # id-ascending for latest-per-locale
            failed_members = 0
            for m in members:
                if m.status in (RunStatus.RUNNING, RunStatus.PENDING):
                    failed_members += 1
                    m.status = RunStatus.FAILED
                    m.finished_at = datetime.utcnow()
                    # GitHub #106: STAMP THE REASON ON THE MEMBER RUN, not only on the
                    # batch. This reconciler failed the member without the step machinery
                    # ever having a say, so it has no StepRun to blame and `failure_shape`
                    # is empty by construction -- and the note went to the BATCH row, which
                    # `GET /api/runs/{id}` and `workflow show-run` do not read. An operator
                    # asking "why did run 293 fail?" got `failed`, a timestamp and nothing
                    # else. Shared restart/crash renderers keep run_reason populated on
                    # every surface without pretending that manual recovery restarted a server.
                    _orch._stamp_note(m, recovery_note)
                    s.add(m)
            if any(m.status == RunStatus.QUEUED for m in members):
                if at_boot:
                    # Durable members will resume in this process. Retaining the dead
                    # owner's PID would let a manual sweep fail newly admitted work.
                    batch.dispatcher_owner = _dispatcher_owner()
                    s.add(batch)
                continue
            requested = list(batch.requested_locales or [])
            # Same rollup as _finalize (the SHARED _rollup_status over the LATEST member
            # per locale), minus the launch-time stop/skip signals (lost with the crashed
            # dispatcher): a re-run-heavy batch cut off by a restart still re-rolls from the
            # latest attempt per locale, never double-counting a superseded run.
            status = _rollup_status(requested, _members_by_locale_latest(members))
            batch.status = status
            batch_note = (
                recovery_note
                if at_boot
                else f"dispatcher exited; {failed_members} member(s) failed"
            )
            batch.notes = ((batch.notes + " ") if batch.notes else "") + batch_note
            batch.finished_at = datetime.utcnow()
            s.add(batch)
            touched += 1
        s.commit()
    return touched


def _finish_start_batch_dispatch(
    session,
    *,
    batch,
    batch_id,
    wf,
    project,
    chosen,
    plan,
    doc,
    created_by,
    synchronous,
    stage_batch_insert,
    what,
):
    # keep_loaded=True (fold-in): the batch's PK + attrs stay populated after the commit, so
    # we read batch.id (and the async path returns a usable `batch`) WITHOUT a post-commit
    # refresh() SELECT — that refresh is on the launch hot path and could itself lose the lock.
    # The row was already flushed under the writer lock, so this commit finalizes that same
    # transaction (batch.id is unchanged) and does not re-acquire the lock.
    #
    # attempts=1 (gate round 10 MED-1): the caller captured `batch_id` from this row BEFORE
    # this commit and every downstream write (begin_batch_action, _dispatch, the audit row,
    # the _DISPATCH_INFLIGHT mark) uses that id. commit_with_retry's retry ROLLS BACK on a
    # lost writer lock, which EXPUNGES the flushed INSERT, and `stage` then re-INSERTs it as a
    # NEW row with a new PK -- so a retry here would silently strand every downstream write on
    # a rolled-back id (an orphan batch frozen 'running', the #99 outcome class). flush_launch
    # already carried the retry for the one write that can actually contend; this commit only
    # finalizes it, so a lost lock here is pathological -- raise it (as WriteContentionError)
    # rather than re-INSERT under a different id.
    db.commit_launch(
        session,
        stage=stage_batch_insert,
        keep_loaded=True,
        what=what,
        attempts=1,
    )

    # Run envelope grain (proposal D1): ONE TapAction per translate CLICK. A batch fans a
    # single click out to N language pairs, so the action is created ONCE here -- eagerly,
    # single-threaded, before any member dispatches -- carrying ALL requested locales.
    # Each member leg then attaches only its own TapRun to this action
    # (begin_workflow_envelope's batch branch finds it by batch_id). Best-effort: a None
    # here degrades to each leg find-or-creating it; it never affects the batch itself.
    try:
        steps = session.exec(
            select(WorkflowStep)
            .where(WorkflowStep.workflow_id == wf.id)
            .order_by(WorkflowStep.order_index)
        ).all()
        tap_envelope.begin_batch_action(
            batch_id=batch_id,
            requested_locales=list(chosen),
            initiator=created_by or "",
            surface="batch",
            org_id=getattr(project, "org_id", None),
            customer_id=getattr(project, "customer_id", None),
            project_id=doc.project_id,
            document_id=doc.id,
            source_locale=getattr(project, "source_locale", "") or "",
            workflow_id=wf.id,
            workflow_name=wf.name or "",
            step_fingerprint=tap_envelope.step_fingerprint(steps),
        )
    except Exception:  # noqa: S110 -- tap envelope is observability; the batch launch proceeds
        pass

    # Release the caller's checkout before queue creation or the CLI's wait.
    doc_id = doc.id
    session.commit()
    _dispatch(batch_id, plan, doc_id, created_by or "", synchronous=synchronous)
    return batch


# --- derive-on-read status + serialization ----------------------------------------
def batch_status(session: Session, batch: WorkflowBatch) -> str:
    """The status to DISPLAY. finished_at is the terminal authority: once it is stamped
    the STORED rollup status is authoritative (the dispatcher computed it with full
    knowledge of spend-stop / fail-fast / skips). Until then the batch reads RUNNING —
    the dispatcher owns finalization, so a finished batch never shows a stale 'running'
    and an in-flight one never shows a premature terminal state. (A dispatcher that
    crashed before finalizing is the same anomaly class as orchestrator.observe_run's
    stuck-RUNNING run — surfaced as RUNNING, not silently 'completed'.)"""
    if batch.finished_at is not None:
        return batch.status
    return BatchStatus.RUNNING


def batch_members(session: Session, batch: WorkflowBatch) -> list[WorkflowRun]:
    """The member runs of a batch, oldest first (dispatch order)."""
    return list(
        session.exec(
            select(WorkflowRun).where(WorkflowRun.batch_id == batch.id).order_by(WorkflowRun.id)
        ).all()
    )


def _member_step_runs(session: Session, member: WorkflowRun) -> list[dict]:
    """One entry per step of the member's RESOLVED-variant plan (get_steps order), with the
    step's run state folded in — the per-member `step_runs` detail the 5b matrix cell reads.
    A step that has not run yet is 'pending' with a NULL score/gate; a step visited more than
    once by a correction loop reports the LATEST visit (highest outcome_index) plus a `visits`
    count (the 'x2' badge). Keyed on the member's OWN variant steps so a structurally-diverged
    variant contributes its own columns — a cell is NEVER placed under a foreign step."""
    from app.services import orchestrator

    steps = orchestrator.get_steps(session, member.workflow_id)
    runs = session.exec(
        select(StepRun).where(StepRun.workflow_run_id == member.id).order_by(StepRun.id)
    ).all()
    by_step: dict[int, list] = {}
    for sr in runs:
        by_step.setdefault(sr.step_id, []).append(sr)
    out = []

    def _nsum(field: str, rows: list):
        # None-safe sum across a step's visits: None when every visit is NULL (legacy /
        # untracked — honest), else the total of the non-NULL values (feat/workflow-spend).
        vals = [getattr(r, field) for r in rows if getattr(r, field, None) is not None]
        return sum(vals) if vals else None

    for st in steps:
        srs = by_step.get(st.id, [])
        if srs:
            latest = max(srs, key=lambda r: ((r.outcome_index or 0), r.id or 0))
            out.append(
                {
                    "step_id": st.id,
                    "order_index": st.order_index,
                    "name": st.name or "",
                    "step_type": st.step_type,
                    "status": latest.status,
                    "score": latest.score,
                    "gate": latest.gate,
                    "outcome_index": latest.outcome_index or 0,
                    "visits": len(srs),
                    "latency_ms": latest.latency_ms,
                    # Real per-step spend (feat/workflow-spend): summed across the step's
                    # visits; NULL where untracked. Additive cell keys (5a/5b subset-check).
                    "cost_usd": _nsum("cost_usd", srs),
                    "tokens_in": _nsum("tokens_in", srs),
                    "tokens_out": _nsum("tokens_out", srs),
                }
            )
        else:
            out.append(
                {
                    "step_id": st.id,
                    "order_index": st.order_index,
                    "name": st.name or "",
                    "step_type": st.step_type,
                    "status": "pending",
                    "score": None,
                    "gate": None,
                    "outcome_index": 0,
                    "visits": 0,
                    "latency_ms": None,
                    "cost_usd": None,
                    "tokens_in": None,
                    "tokens_out": None,
                }
            )
    return out


# --- re-run / retry-failed (5b) — re-dispatch INTO an existing batch ------------------
def rerun_unavailable_reason(session: Session, batch: WorkflowBatch) -> str:
    """Shared wait reason for the page and every re-dispatch entry point."""
    if batch.finished_at is not None:
        return ""
    from app.services.tm_delivery import _TERMINAL_RUN_STATUSES

    latest = _members_by_locale_latest(batch_members(session, batch))
    reopened = batch.reopened_members if isinstance(batch.reopened_members, dict) else {}
    requested = batch.requested_locales if isinstance(batch.requested_locales, list) else []
    in_flight = 0
    for locale in (locale for locale in requested if isinstance(locale, str)):
        member = latest.get(locale)
        boundary = reopened.get(locale, 0)
        if type(boundary) is not int:
            boundary = 0
        if member is None or member.status not in _TERMINAL_RUN_STATUSES or member.id <= boundary:
            in_flight += 1
    if not in_flight:
        from app.services.tm_delivery import KIND_BATCH, run_ref_wait_move

        return (
            "Re-run is available once the batch has finished; "
            "the batch has not been finalized yet. " + run_ref_wait_move((KIND_BATCH, batch.id))
        )
    return f"Re-run is available once the batch has finished; {in_flight} locale(s) still in flight"


def failed_locales(session: Session, batch: WorkflowBatch) -> list[str]:
    """The locales whose LATEST member is FAILED — the retry-failed target set, in the
    batch's requested order."""
    latest = _members_by_locale_latest(batch_members(session, batch))
    requested = list(batch.requested_locales or [])
    return [loc for loc in requested if loc in latest and latest[loc].status == RunStatus.FAILED]
