"""vendor_dispatch: part 2 (layered pure move)

Documented in: docs/architecture/vendor-outsourcing.md
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta

from sqlmodel import Session

from app.models import (
    RunStatus,
    Vendor,
    VendorDispatch,
    VendorStatus,
    WorkflowRun,
    WorkflowStep,
)
from app.services.vendor_dispatch_common import (
    blocks_human_resume,
    sanitize_error,
    vendor_pause_state,
    wait_hours_for,
)

log = logging.getLogger("loc_tms.vendor_dispatch")


def expiry_for(step: WorkflowStep, *, now: datetime | None = None) -> datetime | None:
    """The absolute expiry instant for a leg dispatched now, or None when the bounded
    wait is disabled (wait_hours == 0)."""
    hours = wait_hours_for(step)
    if hours <= 0:
        return None
    return (now or datetime.utcnow()) + timedelta(hours=hours)


def _touch_vendor_error(session: Session, vendor: Vendor, exc) -> None:
    """Best-effort: mark the vendor ERROR with the SANITIZED reason (matching the CONNECTED
    touch on success). Never raises -- the caller's own outcome is authoritative."""
    try:
        vendor.status = VendorStatus.ERROR
        vendor.last_message = f"dispatch failed: {sanitize_error(exc)}"[:500]
        session.add(vendor)
        session.commit()
    except Exception:
        session.rollback()


def _escalate_run_to_human(
    session: Session, leg: VendorDispatch, run: WorkflowRun, *, note: str, action: str, actor: str
) -> None:
    """The SHARED escalate-to-a-human-review-pause body (decision 3), reused by the expiry
    escalation (ESCALATED) and the dead-letter exhaustion (DEAD_LETTER). The caller has
    ALREADY set leg.status + leg.escalated_at; this persists the leg, appends the reason to
    run.notes (never clobbering another seam's note), keeps the run PAUSED (it already is —
    both DISPATCHED and RETRY_WAIT legs parked it), fires the human-review notification
    (guarded), and audits. NEVER hard-fails the run."""
    step = session.get(WorkflowStep, leg.step_id) if leg.step_id else None
    session.add(leg)
    # L3: APPEND to run.notes (never clobber a note another seam -- the restart/staleness
    # recovery -- may have written).
    run.notes = f"{run.notes}; {note}" if (run.notes or "").strip() else note
    if run.status != RunStatus.PAUSED:
        run.status = RunStatus.PAUSED
    session.add(run)
    session.commit()
    # Reuse the human-review notification (guarded — a mail failure never breaks this).
    try:
        from app.services import notifications

        if run is not None and step is not None:
            notifications.on_human_review_paused(run, step)
    except Exception:
        log.exception("escalation notification failed for dispatch %s", leg.id)
    from app.services import audit

    audit.log(
        actor=actor,
        action=action,
        target=f"dispatch {leg.id} (vendor {leg.vendor_id})",
        detail=f"run {leg.workflow_run_id} {leg.target_locale}",
        customer_id=leg.customer_id,
    )


def open_leg_at_current_pause(session: Session, run_id: int) -> VendorDispatch | None:
    """Back-compat thin wrapper: the vendor leg BLOCKING the run's current pause from a
    human resume (DISPATCHED / RETRY_WAIT / wedged-RETURNED), or None. A DEAD_LETTER leg is
    NOT blocking (it has escalated to a human), so this returns None for it. See
    vendor_pause_state / blocks_human_resume for the why."""
    leg, state = vendor_pause_state(session, run_id)
    return leg if blocks_human_resume(state) else None
