"""Fan-out batch dispatch — run a workflow across a document's target locales as one
observable, spend-capped, concurrency-bounded batch (SPEC-workflows-redesign Phase 5a).

Transitional compatibility shim for one release (added 2026-09-15, remove the re-export block after
the next tagged release): `__all__` re-exports log, document_target_locales, _validate, _plan,
_run_member, _dispatch, _BATCH_STATE_LOCK, _BATCH_FINISH_LOCK, _DISPATCH_INFLIGHT, _FINALIZING,
_FINALIZE_DONE_EVENTS, FINALIZE_DONE, FINALIZE_OWNED, FINALIZE_NOT_READY, _SYNC_FINALIZE_ATTEMPTS,
_SYNC_FINALIZE_SLEEP_S, _SYNC_OWNER_WAIT_S, DRAIN_TIMEOUT_NOTE, _MEMBER_THREADS,
_MEMBER_THREADS_LOCK, run_queued_member, SYNC_POOL_NOTE, SYNC_INTERNAL_NOTE, SYNC_INTERNAL_END,
SYNC_RECONCILE_S, _clear_wait_notes, _batch_session, _release_dispatch_inflight,
_await_finalize_owner, _join_batch_members, _sync_batch_result, queue_member_allowed,
_rollup_status, _notes, recover_interrupted_batches, _finish_start_batch_dispatch, batch_status,
batch_members, _member_step_runs, rerun_unavailable_reason, failed_locales, _finalize,
_start_batch_dispatch from app.services.batch_runs_hub, app.services.batch_runs_hub2.

Transitional compatibility shim for one release (added 2026-09-14, remove the re-export block after
the next tagged release): `__all__` re-exports log, _MAX_PARALLEL_CEIL, _MAX_PARALLEL_DEFAULT,
_ACTIVE, BatchError, BatchBudgetExhausted, BatchMissing, default_max_parallel, clamp_max_parallel,
_batch_result_session, _members_by_locale_latest, _ordinals, _clear_coverage_notes,
_dispatcher_owner, _dispatcher_confirmed_dead, _step_identities, _TERMINAL, _col_key,
_parse_skipped_already_running, _unlaunched_reason, build_matrix from
app.services.batch_runs_common.

WHAT THIS IS (and is not). Before 5a a multilingual "run" was N independent client-
driven single-locale submissions with ZERO correlation: `POST /documents/{id}/run` took
ONE locale, `resolve_variant` returned ONE workflow id, each member was its own
`WorkflowRun` with no shared id. This module is the batch layer ABOVE that: `start_batch`
creates ONE `WorkflowBatch` and dispatches one member `WorkflowRun` PER requested locale,
each stamped with `batch_id`. A NULL `batch_id` is still a plain single run — byte-
identical to every pre-5a path (this module is the ONLY writer of a non-NULL batch_id).

ADMISSION: both web and CLI persist the complete member plan as QUEUED rows before
returning or waiting. One process-wide orchestrator admission daemon promotes members
as global slots and batch max_parallel allow. Execution threads own only admitted work.
The web returns immediately; the synchronous CLI admits its own queued members using
the same slot reserve and timed yield, then joins admitted threads.
Boot recovery retains queued members.

THE HONEST LIMITS (verified against the codebase, documented deliberately):
  * fail_fast can only stop LAUNCHING more locales; it CANNOT kill an in-flight member —
    _run_in_background / run_workflow_stream have no cancellation hook. So fail_fast means
    "stop launching further locales once a member has FAILED"; members already in flight run
    to completion. Noted in batch.notes when it fires.
  * spend tie-in is spend.would_block() checked BEFORE each launch (the same predicate the
    provider seam uses). A trip stops LAUNCHING; in-flight members finish. Un-launched
    locales remain visible as FAILED rows with a named reason.
  * DOUBLE-SUBMIT: orchestrator.start_run re-attaches to an existing active run for
    (workflow_id, document_id, target_locale). This module does NOT reuse that seam for
    members (it would collapse two locales that resolve to the SAME universal variant, and
    it would mutate a foreign run's ownership). Instead, before creating a member we check
    for an active run of the SAME (resolved_workflow_id, document_id, target_locale) from
    ANY owner — a single run, another concurrent batch, or a sibling attempt THIS batch's own
    concurrent dispatcher just committed; if one exists we SKIP that locale with an "already-
    running" note and surface it — we NEVER adopt a foreign run into the batch. The check
    (_active_run_elsewhere) and the create (_create_member) are made ATOMIC across every
    dispatcher in this process by orchestrator._RUN_CREATE_LOCK (the SAME lock start_run
    holds around its guard+create), so two dispatchers racing over one (wf, doc, locale)
    cannot both read "nothing active" and both launch a member — the second sees the first's
    committed member and skips. This covers BOTH two batches dispatched concurrently over one
    document AND two reruns of the same locale racing INTO one batch (a double-clicked
    'Re-run' / overlapping retry-failed): the guard does not exclude same-batch members, so a
    same-batch sibling is seen and skipped just like a cross-batch one. That closes the check-
    and-create TOCTOU and makes "never double-run a segment set" a real in-process guarantee.
    It is in-PROCESS: a second OS process (a CLI batch run while the server is live) is not
    serialized by this lock — that residual cross-process window would need a DB partial-
    unique index to close, and is documented here rather than closed.

STATUS AUTHORITY SPLIT (batch_status): WorkflowBatch.finished_at is the terminal authority.
While it is NULL the batch reads RUNNING (the dispatcher owns finalization); once the
dispatcher stamps finished_at it ALSO writes the authoritative rollup status, so the read
path (page / api / history) never shows a stale 'running' for a finished batch. The coarse
status enum is for the header; the live per-locale counts always come from the member rows.

CONCURRENCY BUDGET: two independent ceilings MULTIPLY. Per-segment MT concurrency
(mt_concurrency, default 4, clamp 1..16 in orchestrator._mt_workers) runs WITHIN one step of
one member; batch max_parallel (default 3, clamp 1..8 here) runs members in parallel. Worst
case in-flight provider calls ~= max_parallel * mt_concurrency (e.g. 8 * 16 = 128). Both are
clamped conservatively; the batch default of 3 keeps the default worst case at 3*4 = 12.

Documented in: docs/architecture/workflow-engine.md
"""

from __future__ import annotations

import logging
import os
import socket
import sys
import threading
import time
from collections import Counter
from collections.abc import Callable
from contextlib import contextmanager
from datetime import datetime

from sqlalchemy import exists as sa_exists
from sqlalchemy import update as sa_update
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
    _MAX_PARALLEL_CEIL,
    _MAX_PARALLEL_DEFAULT,
    _TERMINAL,
    BatchBudgetExhausted,
    BatchError,
    BatchMissing,
    _batch_result_session,
    _clear_coverage_notes,
    _col_key,
    _dispatcher_confirmed_dead,
    _dispatcher_owner,
    _members_by_locale_latest,
    _ordinals,
    _parse_skipped_already_running,
    _step_identities,
    _unlaunched_reason,
    build_matrix,
    clamp_max_parallel,
    default_max_parallel,
)
from app.services.batch_runs_hub import (
    _BATCH_FINISH_LOCK,
    _BATCH_STATE_LOCK,
    _DISPATCH_INFLIGHT,
    _FINALIZE_DONE_EVENTS,
    _FINALIZING,
    _MEMBER_THREADS,
    _MEMBER_THREADS_LOCK,
    _SYNC_FINALIZE_ATTEMPTS,
    _SYNC_FINALIZE_SLEEP_S,
    _SYNC_OWNER_WAIT_S,
    DRAIN_TIMEOUT_NOTE,
    FINALIZE_DONE,
    FINALIZE_NOT_READY,
    FINALIZE_OWNED,
    SYNC_INTERNAL_END,
    SYNC_INTERNAL_NOTE,
    SYNC_POOL_NOTE,
    SYNC_RECONCILE_S,
    _active_run_elsewhere,
    _any_member_failed,
    _await_finalize_owner,
    _batch_session,
    _clear_wait_notes,
    _create_member,
    _dispatch,
    _finish_start_batch_dispatch,
    _join_batch_members,
    _member_step_runs,
    _notes,
    _plan,
    _release_dispatch_inflight,
    _rollup_status,
    _run_member,
    _sync_batch_result,
    _validate,
    batch_members,
    batch_status,
    document_target_locales,
    failed_locales,
    queue_member_allowed,
    recover_interrupted_batches,
    rerun_unavailable_reason,
    run_queued_member,
)
from app.services.batch_runs_hub2 import _finalize, _start_batch_dispatch

__all__ = [
    "DRAIN_TIMEOUT_NOTE",
    "FINALIZE_DONE",
    "FINALIZE_NOT_READY",
    "FINALIZE_OWNED",
    "SYNC_INTERNAL_END",
    "SYNC_INTERNAL_NOTE",
    "SYNC_POOL_NOTE",
    "SYNC_RECONCILE_S",
    "_ACTIVE",
    "_BATCH_FINISH_LOCK",
    "_BATCH_STATE_LOCK",
    "_DISPATCH_INFLIGHT",
    "_FINALIZE_DONE_EVENTS",
    "_FINALIZING",
    "_MAX_PARALLEL_CEIL",
    "_MAX_PARALLEL_DEFAULT",
    "_MEMBER_THREADS",
    "_MEMBER_THREADS_LOCK",
    "_SYNC_FINALIZE_ATTEMPTS",
    "_SYNC_FINALIZE_SLEEP_S",
    "_SYNC_OWNER_WAIT_S",
    "_TERMINAL",
    "BatchBudgetExhausted",
    "BatchError",
    "BatchMissing",
    "Counter",
    "StepRun",
    "WorkflowStep",
    "_active_run_elsewhere",
    "_any_member_failed",
    "_await_finalize_owner",
    "_batch_result_session",
    "_batch_session",
    "_clear_coverage_notes",
    "_clear_wait_notes",
    "_col_key",
    "_create_member",
    "_dispatch",
    "_dispatcher_confirmed_dead",
    "_dispatcher_owner",
    "_finalize",
    "_finish_start_batch_dispatch",
    "_join_batch_members",
    "_member_step_runs",
    "_members_by_locale_latest",
    "_notes",
    "_ordinals",
    "_parse_skipped_already_running",
    "_plan",
    "_release_dispatch_inflight",
    "_rollup_status",
    "_run_member",
    "_start_batch_dispatch",
    "_step_identities",
    "_sync_batch_result",
    "_unlaunched_reason",
    "_validate",
    "batch_members",
    "batch_status",
    "build_matrix",
    "clamp_max_parallel",
    "config",
    "contextmanager",
    "datetime",
    "default_max_parallel",
    "document_target_locales",
    "failed_locales",
    "os",
    "queue_member_allowed",
    "recover_interrupted_batches",
    "rerun_unavailable_reason",
    "run_queued_member",
    "sa_exists",
    "socket",
    "spend",
    "sys",
    "tap_envelope",
    "wf_service",
]


log = logging.getLogger("loc_tms.batch_runs")


def _finish_wait_batch(batch_id):
    # No lock across the member join or _finalize (gate A round 9 MED-2): a synchronous
    # waiter that held _BATCH_FINISH_LOCK across a member-thread join or a finalize stalled
    # the admission daemon's drain_once() (which finalizes ready batches as its first act)
    # for the length of that join. _sync_wait_error joins and finalizes lock-free; _finalize
    # takes _BATCH_FINISH_LOCK only for its in-memory de-dup, never across DB work.
    return _sync_wait_error(batch_id)


def _sync_wait_error(batch_id, *, pool_failed=False, internal_error=None, member_ids=None):
    if member_ids is None:
        with _batch_session() as s:
            member_ids = s.exec(
                select(WorkflowRun.id).where(WorkflowRun.batch_id == batch_id)
            ).all()
    # Cleanup can finish the batch after the failed read. Join before deciding
    # whether there is still unfinished work to attach an error to. No lock is held.
    _join_batch_members(member_ids)

    def _record_incomplete(s, batch):
        queued = list(
            s.exec(
                select(WorkflowRun.id).where(
                    WorkflowRun.batch_id == batch_id, WorkflowRun.status == RunStatus.QUEUED
                )
            ).all()
        )
        reason = (
            f"{SYNC_INTERNAL_NOTE}{type(internal_error).__name__}: {internal_error}"
            if internal_error is not None
            else f"{SYNC_POOL_NOTE} {len(queued)} member(s) still queued"
            if pool_failed
            else DRAIN_TIMEOUT_NOTE
        )
        if reason not in (batch.notes or ""):
            note = (
                (_clear_wait_notes(batch.notes) or "")
                + " "
                + reason
                + (SYNC_INTERNAL_END if internal_error is not None else "")
            ).strip()

            def stage_note(batch=batch, note=note):
                batch.notes = note
                s.add(batch)

            db.commit_with_retry(s, stage=stage_note)
        return {
            "code": "wait_internal_error"
            if internal_error is not None
            else "db_pool_exhausted"
            if pool_failed
            else "drain_incomplete",
            "queued_members": queued,
            "reason": reason,
        }

    # Bounded retry (LOW-2): the unlocked read below can see "no unfinished member" while
    # _finalize's locked read sees one reappear (a rerun in flight). Re-read a few times
    # with a short sleep, then record the honest incomplete state rather than hot-spinning.
    for attempt in range(_SYNC_FINALIZE_ATTEMPTS):
        with _batch_session() as s:
            batch = s.get(WorkflowBatch, batch_id)
            if batch is None:
                return {
                    "code": "batch_missing",
                    "queued_members": [],
                    "reason": f"Batch {batch_id} no longer exists",
                }
            if batch.finished_at is not None:
                return None
            unfinished = s.exec(
                select(WorkflowRun.id).where(
                    WorkflowRun.batch_id == batch_id,
                    WorkflowRun.status.in_(
                        (RunStatus.QUEUED, RunStatus.RUNNING, RunStatus.PENDING)
                    ),
                )
            ).first()
            if unfinished is not None:
                return _record_incomplete(s, batch)
            s.close()
        outcome = _finalize(batch_id, [], {})
        if outcome == FINALIZE_DONE:
            return None
        if outcome == FINALIZE_OWNED:
            # Gate round 10 HIGH-2: another finalizer owns the terminal transition and this
            # batch may already be complete. WAIT for the owner to land it (bounded) instead
            # of treating it as "not ready" and burning the 3 x 0.05 s budget -- doing that
            # made an all-COMPLETED batch report drain_incomplete under a busy pool. The top
            # of the next pass (or the fall-through below) returns None the instant the
            # owner's stamp is visible.
            _await_finalize_owner(batch_id)
            continue
        # FINALIZE_NOT_READY: the unlocked read and the locked read disagree (a member
        # reappeared, a rerun in flight). Reconcile again after a short sleep.
        if attempt + 1 < _SYNC_FINALIZE_ATTEMPTS:
            time.sleep(_SYNC_FINALIZE_SLEEP_S)
    with _batch_session() as s:
        batch = s.get(WorkflowBatch, batch_id)
        if batch is None:
            return {
                "code": "batch_missing",
                "queued_members": [],
                "reason": f"Batch {batch_id} no longer exists",
            }
        if batch.finished_at is not None:
            return None
        return _record_incomplete(s, batch)


def _wait_batch(batch_id, on_member_done=None):
    """Wait on member joins; reconcile at most once per two seconds otherwise.

    A reconcile reads members with one checkout. Admission uses those candidates,
    the same fair slot gate and conditional claim, and a caller-owned scheduler.
    No pooled connection is held while waiting. The deadline bounds admission and
    reconciliation of active rows with no local thread;
    every admitted member is joined through cleanup, including on error exits.
    """
    from app.db import engine
    from app.services import bookkeeping
    from app.services import orchestrator as orch

    scheduler = orch._AdmissionScheduler()
    seen, ids = set(), set()
    deadline = time.monotonic() + orch.QUEUE_DRAIN_WAIT_S
    try:
        while True:
            if ids and time.monotonic() >= deadline:
                return _sync_wait_error(batch_id, member_ids=ids)
            saturated = db.pool_has_no_capacity()
            try:
                with (
                    bookkeeping.scope(),
                    db.emergency_session() if saturated else Session(engine) as s,
                ):
                    batch = s.get(WorkflowBatch, batch_id)
                    if batch is None:
                        return _sync_wait_error(batch_id)
                    width = batch.max_parallel
                    rows = list(
                        s.exec(
                            select(WorkflowRun)
                            .where(WorkflowRun.batch_id == batch_id)
                            .order_by(WorkflowRun.id)
                        ).all()
                    )
                    ids.update(r.id for r in rows)
                    queued = [r.id for r in rows if r.status == RunStatus.QUEUED]
                    active = [
                        r.id for r in rows if r.status in (RunStatus.RUNNING, RunStatus.PENDING)
                    ]
                    done = ids - set(queued) - set(active)
            except Exception as exc:
                log.warning(
                    "Synchronous batch reconcile failed for batch %s: %s",
                    batch_id,
                    exc,
                    exc_info=True,
                )
                return _sync_wait_error(
                    batch_id,
                    pool_failed=db.is_pool_timeout_error(exc),
                    internal_error=None if db.is_pool_timeout_error(exc) else exc,
                )
            for rid in done - seen:
                if on_member_done:
                    on_member_done(rid)
            seen.update(done)
            if not queued and not active:
                _join_batch_members(ids)
                return _finish_wait_batch(batch_id)
            if time.monotonic() >= deadline:
                return _sync_wait_error(batch_id, member_ids=ids)
            for rid in queued[: max(0, width - len(active))]:
                if time.monotonic() >= deadline or db.pool_has_no_capacity():
                    break
                token = orch.RUN_SLOTS.acquire_for_queue(timeout=0)
                if token is None:
                    break
                with orch.fanout_slot(token):
                    if not scheduler._admit(rid, token):
                        break
            # A join wakes exactly when a member completes. If no local member is
            # running (capacity elsewhere, external process, policy stop), reconcile
            # only after two seconds or the admission deadline, whichever is first.
            if not _join_batch_members(ids):
                threading.Event().wait(min(SYNC_RECONCILE_S, max(0, deadline - time.monotonic())))
    except Exception as exc:
        log.warning("Synchronous batch wait failed for batch %s: %s", batch_id, exc, exc_info=True)
        return _sync_wait_error(batch_id, internal_error=exc)
    finally:
        # A failed first checkout may precede learning any member ids.
        if not ids:
            with _batch_session() as s:
                ids.update(
                    s.exec(select(WorkflowRun.id).where(WorkflowRun.batch_id == batch_id)).all()
                )
        _join_batch_members(ids)


def finalize_ready_batches():
    """The admission daemon also rolls up batches after their last execution ends.

    No lock is held here (gate A round 9 MED-2): the candidate scan runs in its own short
    session, and each _finalize self-guards (its DB conditional write is idempotent and its
    in-memory _FINALIZING set de-dups concurrent finalizers), so the daemon's drain pass
    never holds a lock across a session or a member's terminal write, and a synchronous
    wait finishing at the same time cannot stall it."""
    with _batch_session() as s:
        ids = list(
            s.exec(select(WorkflowBatch.id).where(WorkflowBatch.finished_at.is_(None))).all()
        )
        ready = []
        for bid in ids:
            active = s.exec(
                select(WorkflowRun.id).where(
                    WorkflowRun.batch_id == bid,
                    WorkflowRun.status.in_(
                        (RunStatus.QUEUED, RunStatus.RUNNING, RunStatus.PENDING)
                    ),
                )
            ).first()
            if active is None:
                ready.append(bid)
    for bid in ready:
        _finalize(bid, [], {})


# --- the public entry point -------------------------------------------------------
def start_batch(
    session: Session,
    *,
    workflow_id: int,
    doc_id: int,
    locales: list[str] | None,
    created_by: str = "",
    max_parallel=None,
    fail_fast: bool = False,
    synchronous: bool = False,
    on_member_done: Callable[[int], None] | None = None,
) -> WorkflowBatch:
    """Create a WorkflowBatch and dispatch one member run per requested locale.

    `locales=None` fans out to ALL the document's target locales; an explicit list is
    validated (each must be a real target of the document; a bogus one is refused). RBAC /
    org / customer ring is enforced by the CALLING route, exactly as the single-run route
    gates before orchestrator.start_run.

    `synchronous=False` (WEB): persist all queued members and return the batch immediately.
    The shared admission daemon launches members and finalizes the batch.
    `synchronous=True` (CLI): drive admission with short sessions and join all members;
    report queued ids explicitly if the total admission deadline expires — never a daemon that dies
    with a one-shot process. Returns the finalized batch.

    Raises BatchError on an invalid request (mapped to 400 / usage error by the caller).
    """
    wf, doc, project, chosen = _validate(session, workflow_id, doc_id, locales)
    mp = clamp_max_parallel(max_parallel)
    plan = _plan(session, wf, project, chosen)

    batch = WorkflowBatch(
        dispatcher_owner=_dispatcher_owner(),
        org_id=getattr(project, "org_id", None),
        workflow_id=wf.id,
        document_id=doc.id,
        created_by=created_by or "",
        requested_locales=list(chosen),
        max_parallel=mp,
        fail_fast=bool(fail_fast),
        status=BatchStatus.RUNNING,
    )

    # flood R2-F1: the batch-row INSERT is a LAUNCH write; ride out a transient writer lock
    # with the bounded jittered retry and, on exhaustion, raise WriteContentionError so the
    # request surfaces an honest retryable 503 rather than a 500. Pending set = {WorkflowBatch}
    # (_validate/_plan above only READ); `_stage` re-adds after a retry's rollback.
    def _stage_batch_insert():
        session.add(batch)

    # No lock across the dispatch (round 12): _start_batch_dispatch marks the batch
    # in-flight in memory (holding _BATCH_STATE_LOCK only for that set op) and does every
    # checkout, envelope call, dispatch and audit write OUTSIDE any lock, so a launch never
    # queues behind another batch's envelope I/O or pins a connection while waiting on a lock.
    result = _start_batch_dispatch(
        session,
        batch=batch,
        wf=wf,
        project=project,
        chosen=chosen,
        plan=plan,
        doc=doc,
        created_by=created_by,
        synchronous=synchronous,
        stage_batch_insert=_stage_batch_insert,
    )
    if synchronous:
        batch_id = result.id
        session.commit()
        error = _wait_batch(batch_id, on_member_done)
        return _sync_batch_result(session, batch_id, error)
    return result


def batch_as_dict(session: Session, batch: WorkflowBatch, detail: bool = False) -> dict:
    with _batch_result_session(session):
        return _batch_as_dict(session, batch, detail)


def _batch_as_dict(session: Session, batch: WorkflowBatch, detail: bool = False) -> dict:
    """The ONE canonical JSON shape for a batch, shared by /api GET /api/batches/{id}, the
    CLI --json output, the web monitor poll, and (5b) build_matrix. Batch fields + members[]
    each {locale, run_id, status, current_step_index, resolved_workflow_id, attempt,
    is_latest}. The aggregate counts are always LIVE from the member rows, computed over the
    LATEST attempt per locale (5b re-runs give a locale >1 member — a superseded run never
    double-counts). `attempt` is the member's 1-based ordinal among its locale's members;
    `is_latest` marks the row the matrix shows.

    detail=True (the ?detail=steps flag) additionally attaches, per member, `step_runs` (the
    per-step cell detail above) and, top-level, `requested_steps` (the requested workflow's
    step identities — the matrix's baseline column axis). The LIGHT shape (detail=False) is an
    ADDITIVE SUPERSET of 5a's — every member gains `attempt` + `is_latest` (the matrix's
    latest-per-locale markers) but NOT the heavy `step_runs`/`requested_steps`, and the counts
    switch to latest-per-locale semantics (a no-op until a re-run exists). The added member
    keys are purely additive — 5a consumers subset-check member keys and tolerate extras — so
    the light poll stays light while remaining backward-compatible with every 5a reader."""
    members = batch_members(session, batch)
    latest = _members_by_locale_latest(members)
    ordinals = _ordinals(members)
    latest_ids = {m.id for m in latest.values()}
    n_completed = sum(1 for m in latest.values() if m.status == RunStatus.COMPLETED)
    n_failed = sum(1 for m in latest.values() if m.status == RunStatus.FAILED)
    n_paused = sum(1 for m in latest.values() if m.status == RunStatus.PAUSED)
    n_running = sum(1 for m in latest.values() if m.status in _ACTIVE)
    requested = list(batch.requested_locales or [])
    unlaunched = [
        loc
        for loc in requested
        if loc not in latest
        or (
            latest[loc].status == RunStatus.FAILED
            and any(
                reason in (latest[loc].notes or "") for reason in ("Spend cap reached", "Fail-fast")
            )
        )
    ]
    n_queued = sum(m.status == RunStatus.QUEUED for m in latest.values())
    status = batch_status(session, batch)

    def _member_dict(m: WorkflowRun) -> dict:
        d = {
            "locale": m.target_locale,
            "run_id": m.id,
            "status": m.status,
            "current_step_index": m.current_step_index,
            "resolved_workflow_id": m.workflow_id,
            "attempt": ordinals.get(m.id, 1),
            "is_latest": m.id in latest_ids,
        }
        if detail:
            d["step_runs"] = _member_step_runs(session, m)
        return d

    out = {
        "id": batch.id,
        "error": (
            getattr(batch, "drain_error", None)
            or (
                {
                    "code": "wait_internal_error"
                    if SYNC_INTERNAL_NOTE in (batch.notes or "")
                    else "db_pool_exhausted"
                    if SYNC_POOL_NOTE in (batch.notes or "")
                    else "drain_incomplete",
                    "queued_members": [m.id for m in members if m.status == RunStatus.QUEUED],
                    "reason": (
                        SYNC_INTERNAL_NOTE
                        + batch.notes.split(SYNC_INTERNAL_NOTE, 1)[1].split(SYNC_INTERNAL_END, 1)[0]
                        if SYNC_INTERNAL_NOTE in (batch.notes or "")
                        else f"{SYNC_POOL_NOTE} "
                        f"{sum(m.status == RunStatus.QUEUED for m in members)} "
                        "member(s) still queued"
                        if SYNC_POOL_NOTE in (batch.notes or "")
                        else DRAIN_TIMEOUT_NOTE
                    ),
                }
                if batch.finished_at is None
                and any(
                    note in (batch.notes or "")
                    for note in (DRAIN_TIMEOUT_NOTE, SYNC_POOL_NOTE, SYNC_INTERNAL_NOTE)
                )
                else None
            )
        )
        if n_queued or any(m.status in (RunStatus.RUNNING, RunStatus.PENDING) for m in members)
        else None,
        "kind": "batch",
        "workflow_id": batch.workflow_id,
        "document_id": batch.document_id,
        "status": status,
        "created_by": batch.created_by or "",
        "created_at": batch.created_at.isoformat() if batch.created_at else None,
        "finished_at": batch.finished_at.isoformat() if batch.finished_at else None,
        "requested_locales": requested,
        "max_parallel": batch.max_parallel,
        "fail_fast": bool(batch.fail_fast),
        "notes": batch.notes or "",
        "spend_stopped": bool(batch.notes and "Spend cap reached" in batch.notes),
        "counts": {
            "requested": len(requested),
            "launched": len(latest) - n_queued - sum(loc in latest for loc in unlaunched),
            "queued": n_queued,
            "completed": n_completed,
            "failed": n_failed,
            "paused": n_paused,
            "running": n_running,
            "unlaunched": len(unlaunched),
        },
        "unlaunched_locales": unlaunched,
        "members": [_member_dict(m) for m in members],
    }
    if detail:
        from app.services import orchestrator

        out["requested_steps"] = _step_identities(
            orchestrator.get_steps(session, batch.workflow_id)
        )
    return out


def rerun_batch(
    session: Session,
    *,
    batch: WorkflowBatch,
    locales: list[str],
    created_by: str = "",
    synchronous: bool = False,
    on_member_done: Callable[[int], None] | None = None,
) -> WorkflowBatch:
    """Re-dispatch FRESH member runs for `locales` INTO an existing batch (SPEC Phase 5b).
    Each locale gets a NEW WorkflowRun (batch_id set, a fresh attempt); the prior run(s) stay
    in history and the matrix shows the LATEST per locale with an attempt count. REUSES
    _dispatch — there is NO second execution path (the same member core, spend gate, already-
    running skip, atomic check-and-create, and finalize as start_batch).

    REOPEN semantics: a re-run on a FINISHED batch clears finished_at (the batch reads RUNNING
    again via batch_status) for the duration of the re-dispatch, then _dispatch re-finalizes
    (re-stamps finished_at + the re-rolled status from the LATEST attempt per locale). fail_fast
    is NOT applied to a re-run — an explicit re-attempt tries every named locale; the spend gate
    still applies per launch inside _dispatch. RBAC is the CALLER's job (mirrors start_batch).

    Raises BatchError: an empty/invalid locale set, a locale not in the batch's request, or a
    locale whose LATEST member is still ACTIVE, or an unfinished batch (another dispatcher
    still owns its intent; never replace that intent with a competing re-run)."""
    reason = rerun_unavailable_reason(session, batch)
    if reason:
        raise BatchError(reason)
    wf = session.get(Workflow, batch.workflow_id)
    doc = session.get(Document, batch.document_id)
    if wf is None or doc is None:
        raise BatchError("this batch's workflow or document no longer exists.")
    # BACKLOG 2026-09-04 Item A (defense in depth): a re-run / retry launches FRESH member
    # runs, so it is a NEW machine-translate submission and gets the SAME FULL_TEXT x
    # tm_match refusal the initial batch launch does. Gate H1: this is evaluated on the
    # re-run's own RESOLVED plan (below, right after _plan), NOT on the parent workflow --
    # the dispatcher re-runs the resolved variant, so gating the parent here missed a clean
    # parent whose variant carries tm_match and falsely refused the mirror case. A batch of
    # this shape can no longer be CREATED, so this only fires on a batch that predates the
    # guard (or whose variants gained a tm_match step after the batch was created).
    project = session.get(Project, doc.project_id)
    # #619: the same archive gate the normal run path (`_validate`) applies, same wording.
    # A re-run / retry-failed starts FRESH work, so it must not reach a retired workflow or
    # project. Checked before any state change (no reopen, no run row).
    if getattr(wf, "archived_at", None) is not None:
        raise BatchError(
            f"workflow {wf.id} ({wf.name}) is archived -- unarchive it before running "
            f"new translations."
        )
    # #542 item 3: a re-run launches FRESH member runs, so a workflow that now belongs to a
    # different customer than the document (re-assigned since the batch started) is refused.
    try:
        wf_service.refuse_if_workflow_customer_mismatch(
            session, batch.workflow_id, doc=doc, project=project
        )
    except wf_service.WorkflowCustomerMismatchError as exc:
        raise BatchError(str(exc)) from exc
    # Review: same order as `_validate` (workflow archived, customer mismatch, project
    # archived), so a batch failing two gates answers the same refusal on both paths.
    if project is not None and getattr(project, "archived_at", None) is not None:
        raise BatchError(
            f"project {project.id} ({project.name}) is archived -- unarchive it before "
            f"running new translations."
        )
    # CV-2 (N2): a re-run / retry-failed launches FRESH member WorkflowRuns (new AI
    # spend), so it is a NEW machine-translate submission and is refused at 100% of the
    # project's monthly USD budget, BEFORE any run row is created — the same gate
    # start_batch/_validate apply. Raised as BatchBudgetExhausted so every batch surface
    # (rerun + retry-failed, both wait arms, web + /api) answers 429 (M2). The per-launch
    # WriteBudget spend CAP inside _dispatch is a DIFFERENT gate (the global $ kill switch),
    # not this per-project USD budget. (retry_leg — a committed vendor re-send — stays
    # un-gated per L7; that is a separate class.)
    if doc.project_id is not None:
        from app.services import project_budgets

        _pb_ok, _pb_rem, _pb_msg = project_budgets.project_check(doc.project_id)
        if not _pb_ok:
            project_budgets.evaluate_thresholds(doc.project_id, actor="system")
            raise BatchBudgetExhausted(_pb_msg)
        project_budgets.evaluate_thresholds(doc.project_id, actor="system")
    requested = list(batch.requested_locales or [])
    seen: set = set()
    chosen: list[str] = []
    for loc in locales or []:
        loc = (loc or "").strip()
        if not loc or loc in seen:
            continue
        if loc not in requested:
            raise BatchError(
                f"locale {loc!r} is not part of batch #{batch.id} "
                f"(request: {', '.join(requested) or 'none'})."
            )
        seen.add(loc)
        chosen.append(loc)
    if not chosen:
        raise BatchError("select at least one locale to re-run.")
    # Never re-run an in-flight member (its latest run must be terminal). An un-launched
    # locale (no member yet) is allowed — a re-run LAUNCHES it (e.g. after a spend stop).
    latest = _members_by_locale_latest(batch_members(session, batch))
    for loc in chosen:
        m = latest.get(loc)
        if m is not None and m.status in _ACTIVE:
            raise BatchError(
                f"locale {loc!r} is still {m.status}; wait for it to finish "
                f"(or resume it) before re-running."
            )
    plan = _plan(session, wf, project, chosen)
    # Claim the validated finished timestamp atomically so a competing rerun cannot
    # replace this dispatch's locale intent with an obsolete member boundary.
    # Mark the batch in-flight (in memory) BEFORE clearing finished_at, and keep the mark
    # until the fresh members are committed, so a finalizer that observes the just-cleared
    # finished_at also sees the mark and cannot close the batch during the reopen window.
    # No lock is held across the reopen UPDATE, the commit, the refresh or _dispatch (round
    # 12): the in-memory mark, not a lock held across DB work, protects the window.
    batch_id, doc_id = batch.id, doc.id
    with _BATCH_STATE_LOCK:
        _DISPATCH_INFLIGHT[batch_id] += 1
    try:
        result = session.execute(
            sa_update(WorkflowBatch)
            .where(
                WorkflowBatch.id == batch_id,
                WorkflowBatch.finished_at.is_not(None),
                WorkflowBatch.finished_at == batch.finished_at,
            )
            .values(
                finished_at=None,
                notes=_clear_wait_notes(batch.notes),
                status=BatchStatus.RUNNING,
                reopened_members={loc: (latest[loc].id if loc in latest else 0) for loc in chosen},
                dispatcher_owner=_dispatcher_owner(),
            )
            .execution_options(synchronize_session=False)
        )
        if result.rowcount != 1:
            session.rollback()
            session.refresh(batch)
            raise BatchError(
                rerun_unavailable_reason(session, batch)
                or "this batch changed; refresh it before re-running."
            )
        session.commit()
        session.refresh(batch)
        batch.__dict__.pop("drain_error", None)
        # Release the refreshed caller's checkout before staging in a short session.
        session.commit()
        _dispatch(
            batch_id, plan, doc_id, created_by or "", ignore_fail_fast=True, synchronous=synchronous
        )
    finally:
        with _BATCH_STATE_LOCK:
            _release_dispatch_inflight(batch_id)
    if synchronous:
        error = _wait_batch(batch_id, on_member_done)
        return _sync_batch_result(session, batch_id, error)
    return batch
