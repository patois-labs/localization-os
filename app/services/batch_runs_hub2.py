"""batch_runs: second pass part 2 (layered pure move)

Documented in: docs/architecture/workflow-engine.md
"""

from __future__ import annotations

import threading
from datetime import datetime

from sqlalchemy import exists as sa_exists
from sqlalchemy import update as sa_update
from sqlmodel import select

from app import db
from app.models import (
    RunStatus,
    WorkflowBatch,
    WorkflowRun,
)
from app.services import tap_envelope
from app.services.batch_runs_common import (
    _clear_coverage_notes,
    _members_by_locale_latest,
)
from app.services.batch_runs_hub import (
    _BATCH_FINISH_LOCK,
    _BATCH_STATE_LOCK,
    _DISPATCH_INFLIGHT,
    _FINALIZE_DONE_EVENTS,
    _FINALIZING,
    FINALIZE_DONE,
    FINALIZE_NOT_READY,
    FINALIZE_OWNED,
    _batch_session,
    _clear_wait_notes,
    _finish_start_batch_dispatch,
    _notes,
    _release_dispatch_inflight,
    _rollup_status,
)


def _finalize(batch_id: int, skipped: list[tuple[str, str]], stop: dict) -> str:
    """Compute the rollup + notes and stamp them with finished_at (the terminal
    authority). Called once, after every launched member of THIS dispatch has joined.

    STATUS + COVERAGE come from the DB's LATEST-per-locale member rows (not this
    dispatch's launched list), so a RE-RUN dispatch — which launches only a subset —
    still re-finalizes the WHOLE batch correctly (a previously-unlaunched locale that a
    re-run covered drops out of 'not launched'; a FAILED locale a re-run fixed re-rolls to
    COMPLETED). The `skipped`/`stop` signals are THIS dispatch's early-stop reasons, used
    only for the NOTES.

    Locking (round 12): NO lock is held across the session, the reads, the terminal write
    or the envelope close. _BATCH_STATE_LOCK and _BATCH_FINISH_LOCK are taken only for the
    in-memory guards (_DISPATCH_INFLIGHT, _FINALIZING). Single-finalize is enforced by the
    DB: the terminal stamp is one conditional UPDATE guarded on `finished_at IS NULL AND no
    active member`, so a second finalizer, or a finalizer that races a reopen's fresh
    members, updates zero rows and does nothing (no re-stamp, no duplicate note, no second
    envelope close).

    Returns a TRI-state (gate round 10 HIGH-2), never collapsing "another thread owns the
    transition" into "not ready":
      * FINALIZE_DONE      -- terminal now (THIS call stamped it, or it was already terminal
                              / gone);
      * FINALIZE_OWNED     -- another thread holds the claim and has not landed the terminal
                              stamp yet (a synchronous waiter waits on its completion Event);
      * FINALIZE_NOT_READY -- a member is still active, or a dispatch is in flight."""
    # (1) A dispatch (initial or reopen) is creating this batch's members; it is not
    #     eligible for finalization yet. In-memory only.
    with _BATCH_STATE_LOCK:
        if batch_id in _DISPATCH_INFLIGHT:
            return FINALIZE_NOT_READY
    # (2) At most one thread does the terminal DB work per batch at a time. In-memory only;
    #     correctness still rests on the conditional UPDATE below, not on this claim. The
    #     claimer creates the completion Event here so an OWNED caller can wait on it.
    with _BATCH_FINISH_LOCK:
        claimed = batch_id not in _FINALIZING
        if claimed:
            _FINALIZING.add(batch_id)
            _FINALIZE_DONE_EVENTS[batch_id] = threading.Event()
    if not claimed:
        # Another thread owns the terminal transition. If it has already landed (or the batch
        # is gone) report DONE; otherwise OWNED, NOT not-ready, so a synchronous waiter waits
        # for the owner's completion signal instead of giving up (HIGH-2). Checkout here is
        # OUTSIDE every lock.
        with _batch_session() as s:
            other = s.get(WorkflowBatch, batch_id)
            if other is None or other.finished_at is not None:
                return FINALIZE_DONE
        return FINALIZE_OWNED
    finalized = False
    org_id = document_id = None
    try:
        # Phase A -- READ and compute in one short session, then CLOSE it, so no connection
        # is held while the dispatch guard is re-checked below.
        with _batch_session() as s:
            batch = s.get(WorkflowBatch, batch_id)
            if batch is None or batch.finished_at is not None:
                return FINALIZE_DONE
            requested = list(batch.requested_locales or [])
            members = list(
                s.exec(
                    select(WorkflowRun)
                    .where(WorkflowRun.batch_id == batch_id)
                    .order_by(WorkflowRun.id)
                ).all()
            )
            if any(
                m.status in (RunStatus.QUEUED, RunStatus.RUNNING, RunStatus.PENDING)
                for m in members
            ):
                return FINALIZE_NOT_READY
            latest = _members_by_locale_latest(members)
            skipped_locales = [loc for loc, _r in skipped]
            new_status = _rollup_status(requested, latest)
            org_id, document_id = batch.org_id, batch.document_id
        # Re-check the dispatch guard with NO connection held. A reopen adds the batch to
        # _DISPATCH_INFLIGHT BEFORE it clears finished_at, so if we observed a NULL
        # finished_at that a reopen had just cleared, we see the mark here and skip. (A
        # first-time finalize -- finished_at NULL from creation -- cannot be racing a reopen,
        # which requires finished_at to be set to begin.)
        with _BATCH_STATE_LOCK:
            if batch_id in _DISPATCH_INFLIGHT:
                return FINALIZE_NOT_READY
        now = datetime.utcnow()
        active_member = (
            sa_exists()
            .where(WorkflowRun.batch_id == batch_id)
            .where(WorkflowRun.status.in_((RunStatus.QUEUED, RunStatus.RUNNING, RunStatus.PENDING)))
        )
        # Phase B -- the terminal write is ONE conditional UPDATE: it stamps only while the
        # batch is still unfinished AND has no active member, so a second finalizer, or one
        # racing a reopen's fresh members, updates zero rows and changes nothing.
        with _batch_session() as s:
            # LOW-5 (gate round 10): derive the notes in the SAME phase that WRITES them, from
            # the row's CURRENT notes, so the read-modify-write window for notes is this one
            # short session rather than spanning the phase-A read, a session close and a lock
            # acquire. Status + coverage still come from phase A's terminal member rows.
            current = s.get(WorkflowBatch, batch_id)
            if current is None or current.finished_at is not None:
                return FINALIZE_DONE
            new_notes = (
                " ".join(
                    filter(
                        None,
                        [
                            _clear_coverage_notes(_clear_wait_notes(current.notes)),
                            _notes(requested, latest, skipped_locales, stop),
                        ],
                    )
                )
                or None
            )
            result = s.execute(
                sa_update(WorkflowBatch)
                .where(
                    WorkflowBatch.id == batch_id,
                    WorkflowBatch.finished_at.is_(None),
                    ~active_member,
                )
                .values(status=new_status, notes=new_notes, finished_at=now)
                .execution_options(synchronize_session=False)
            )
            s.commit()
            if result.rowcount != 1:
                # We HOLD the claim, so no other thread is mid-transition for this batch; a
                # zero-row UPDATE therefore means either it is already terminal (someone
                # finalized before we claimed) -> DONE, or a reopen gave it a fresh active
                # member between our read and our write -> NOT_READY (finished_at still NULL).
                fresh = s.get(WorkflowBatch, batch_id)
                return (
                    FINALIZE_DONE
                    if (fresh is None or fresh.finished_at is not None)
                    else FINALIZE_NOT_READY
                )
            finalized = True
    finally:
        # Release the claim and WAKE any synchronous waiter parked on this batch's completion
        # Event (gate round 10 HIGH-2). The terminal stamp is already committed by here, so a
        # woken waiter re-reads a finished batch. Set the Event outside the lock.
        with _BATCH_FINISH_LOCK:
            _FINALIZING.discard(batch_id)
            done_event = _FINALIZE_DONE_EVENTS.pop(batch_id, None)
        if done_event is not None:
            done_event.set()
    # Run-envelope close (proposal D1): OUTSIDE every lock and the session. Every member
    # thread has JOINED by the time _finalize runs, so this is the single race-free point to
    # (a) close a ZERO-leg batch action that no member ever attached a run to -- spend gate /
    # fail-fast / all-already-running -- as ABANDONED instead of leaving it 'running' forever
    # (M1), and (b) assemble the action's per-locale asset manifest from every leg's
    # committed run.detail.manifest (M5). Validated on the batch's own tenant identity
    # (org/document) so a rowid-reused batch_id cannot touch another tenant's action (B1).
    # Best-effort: an observer close must never break batch finalization. Fires ONCE because
    # only the finalizer whose conditional UPDATE won (rowcount == 1) reaches here.
    if finalized:
        # Gate round 10 HIGH-1: the close runs INSIDE bookkeeping.scope() -- still NOT under
        # any lock and NOT holding the phase-B session, but inside the scope whose engine
        # policy routes every helper Session finalize_batch_action opens (find_batch_action,
        # finalize_action_if_all_runs_done, assemble_batch_manifest, the terminal close) to
        # the UNBOUNDED emergency engine when the pool is saturated. Without the scope those
        # helpers are plain pooled Sessions; on a full pool each waits the whole pool timeout,
        # raises, and is swallowed by the best-effort guard below -- stranding the TapAction
        # 'running' forever (the M1 hole this close exists to close, re-opened) and turning
        # the admission daemon's drain pass into a per-batch pool-timeout stall.
        from app.services import bookkeeping

        with bookkeeping.scope():
            try:  # noqa: SIM105
                tap_envelope.finalize_batch_action(
                    batch_id,
                    org_id=org_id,
                    document_id=document_id,
                )
            except Exception:  # noqa: S110 -- tap manifest is observability; batch is finalized
                pass
    return FINALIZE_DONE


def _start_batch_dispatch(
    session,
    *,
    batch,
    wf,
    project,
    chosen,
    plan,
    doc,
    created_by,
    synchronous,
    stage_batch_insert,
):
    """Assign the batch PK, mark it in-flight, commit it, open its audit envelope, then
    persist its complete queue plan. No lock is held across any checkout, envelope call,
    dispatch or audit write (round 12)."""

    what = f"launch batch for workflow {wf.id} / document {doc.id}"
    # Assign the batch PK WITHOUT publishing the row (flush, not commit), so it can be marked
    # in-flight BEFORE any finalizer can observe it. A finalizer skips a batch in
    # _DISPATCH_INFLIGHT, so the admission daemon cannot roll up a just-created batch whose
    # QUEUED members do not exist yet. The INSERT is the one writer-lock point; flush_launch
    # carries the launch-write retry, and the commit below runs under the lock it already holds.
    db.flush_launch(session, stage=stage_batch_insert, what=what)
    batch_id = batch.id
    with _BATCH_STATE_LOCK:
        _DISPATCH_INFLIGHT[batch_id] += 1
    try:
        return _finish_start_batch_dispatch(
            session,
            batch=batch,
            batch_id=batch_id,
            wf=wf,
            project=project,
            chosen=chosen,
            plan=plan,
            doc=doc,
            created_by=created_by,
            synchronous=synchronous,
            stage_batch_insert=stage_batch_insert,
            what=what,
        )
    finally:
        with _BATCH_STATE_LOCK:
            _release_dispatch_inflight(batch_id)
