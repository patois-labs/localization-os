"""vendor_return: part 2 (layered pure move)

Documented in: docs/architecture/vendor-outsourcing.md
"""

from __future__ import annotations

from sqlmodel import Session

from app.models import (
    Document,
    RunStatus,
    Vendor,
    VendorDispatch,
    VendorDispatchStatus,
    WorkflowRun,
)
from app.services import vendor_dispatch
from app.services.vendor_return_common import (
    ReturnStateError,
    _aligned_slots,
    _resume_leg,
    _segment_index,
    dispatched_ids,
    outstanding_ids,
    poll_url_for,
    returned_ids,
)


def redrive_returned_leg(
    session: Session, leg: VendorDispatch, *, actor: str = "system", mode: str = "sync"
) -> dict:
    """MEDIUM-4 operator recovery: re-drive the resume of a leg that RETURNED but whose run
    never resumed (the resume threw and was swallowed above). The content is ALREADY
    committed, so this only CONTINUES the parked run past the external step -- it re-commits
    nothing (every leg-unit slot is skipped, seeded from its committed target). Raises
    ReturnStateError when the leg is not a RETURNED leg still parked at its own step."""
    if (leg.status or "") != VendorDispatchStatus.RETURNED:
        raise ReturnStateError(
            f"dispatch {leg.id} is {leg.status!r}, not RETURNED; nothing to re-drive."
        )
    run = session.get(WorkflowRun, leg.workflow_run_id)
    if run is None or run.status != RunStatus.PAUSED:
        raise ReturnStateError(
            f"dispatch {leg.id}: run {leg.workflow_run_id} is not paused; nothing to "
            "re-drive (it already moved on)."
        )
    from app.services import orchestrator

    paused_sr = orchestrator._latest_paused_steprun(session, run.id)
    if paused_sr is None or (leg.step_id is not None and paused_sr.step_id != leg.step_id):
        raise ReturnStateError(
            f"dispatch {leg.id}: run {run.id} is not parked at this leg's step; nothing "
            "to re-drive."
        )
    doc = session.get(Document, leg.document_id) if leg.document_id else None
    if doc is None:
        raise ReturnStateError(f"dispatch {leg.id}: its document no longer exists.")
    segments = orchestrator._load_segments(session, doc.id, leg.target_locale)
    slots = _aligned_slots(session, leg, run, segments)
    by_id = _segment_index(segments, leg)
    leg_positions = set()
    for _sid in dispatched_ids(leg):
        _p = by_id.get(_sid, (None,))[0]
        if _p is not None and 0 <= _p < len(segments):
            leg_positions.add(_p)
            slots[_p] = segments[_p].target_text or ""  # authoritative committed content
    resumed = _resume_leg(
        session,
        leg,
        run,
        slots,
        actor=actor,
        mode=mode,
        skip_positions=leg_positions,
        tm_ineligible_positions=set(),
    )
    from app.services import audit

    audit.log(
        actor=actor or "system",
        action="vendor.return_redriven",
        target=f"dispatch {leg.id} (run {run.id})",
        detail=f"re-drive resumed={resumed}",
        customer_id=getattr(leg, "customer_id", None),
    )
    return {"ok": bool(resumed), "dispatch_id": leg.id, "run_id": run.id, "resumed": bool(resumed)}


# --- serialization ------------------------------------------------------------
def leg_to_dict(session: Session, leg: VendorDispatch) -> dict:
    """A JSON/CLI-serialisable view of a dispatch leg. Carries the host only (never the
    endpoint URL or the signing secret) and NEVER the claim token -- the token is a
    bearer credential for admitting content, so it stays in the row, exactly as Wave A
    kept it out of the SSE envelope."""
    vendor = session.get(Vendor, leg.vendor_id)
    return {
        "id": leg.id,
        "vendor_id": leg.vendor_id,
        "vendor_name": (vendor.name if vendor else ""),
        "workflow_run_id": leg.workflow_run_id,
        "step_key": leg.step_key or "",
        "document_id": leg.document_id,
        "target_locale": leg.target_locale or "",
        "status": leg.status,
        "endpoint_host": leg.endpoint_host or "",
        "segment_count": leg.segment_count,
        "word_count": leg.word_count,
        "estimate_cost_usd": leg.estimate_cost_usd,
        "estimate_currency": leg.estimate_currency,
        "actual_cost_usd": leg.actual_cost_usd,
        "dispatched_at": leg.dispatched_at.isoformat() if leg.dispatched_at else None,
        "expires_at": leg.expires_at.isoformat() if leg.expires_at else None,
        "returned_at": leg.returned_at.isoformat() if leg.returned_at else None,
        "escalated_at": leg.escalated_at.isoformat() if leg.escalated_at else None,
        "dispatched_ids": len(dispatched_ids(leg)),
        "returned_ids": len(returned_ids(leg)),
        "outstanding_ids": len(outstanding_ids(leg)),
        # `pollable` reflects whether the AUTOMATIC sweep can poll this leg: a poll_url AND a
        # native transport (a `manual` leg is never polled even if a poll_url is present). F2:
        # key off the LEG's protocol snapshot, so a manual-dispatched leg reads pollable=False
        # even after the vendor is flipped to native.
        "pollable": bool(poll_url_for(vendor))
        and vendor_dispatch.leg_protocol(leg, vendor) == vendor_dispatch.NATIVE_PROTOCOL,
        "customer_id": leg.customer_id,
        # Wave C (interop R1 + R5): the transport protocol + the dead-letter/backoff state.
        # F2: report the LEG's protocol SNAPSHOT (how it was actually dispatched), falling back
        # to the vendor's live protocol on a legacy leg -- NOT the vendor's current protocol,
        # which may have been flipped after this leg was sent. last_error is stored already-
        # sanitized (no URLs/secrets); attempt_count / next_attempt_at describe a RETRY_WAIT
        # leg's position in the backoff window.
        "protocol": vendor_dispatch.leg_protocol(leg, vendor),
        "attempt_count": leg.attempt_count or 0,
        "next_attempt_at": (leg.next_attempt_at.isoformat() if leg.next_attempt_at else None),
        "last_error": leg.last_error or "",
        # Wave E R3 (quote gate): the estimate -> QUOTE -> actual reconciliation chain +
        # quote-gate state. All 0/NULL/'' on a non-quote-gated leg.
        "requires_quote": bool(leg.requires_quote),
        "quoted_cost_usd": leg.quoted_cost_usd,
        "quote_currency": leg.quote_currency or "",
        "quoted_at": leg.quoted_at.isoformat() if leg.quoted_at else None,
        "quote_approved_at": (leg.quote_approved_at.isoformat() if leg.quote_approved_at else None),
        "quote_note": leg.quote_note or "",
        # Wave E R4 (reject/rework): the rework lineage. 0/NULL on an original leg.
        "rework_round": leg.rework_round or 0,
        "rework_of": leg.rework_of,
        "rework_feedback": leg.rework_feedback or None,
    }
