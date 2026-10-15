"""The quote-approval gate for the vendor dispatch-and-wait step (PROPOSAL-vendor-portal-
interop R3; Vendor Wave E).

R3 adds an OPTIONAL state between dispatch and work: DISPATCHED -> QUOTED -> (operator
approves) -> work proceeds. It is behind the per-vendor `requires_quote_approval` flag
(vendors.requires_quote_approval, default OFF); with the flag OFF a leg is NEVER QUOTED and
the ENTIRE dispatch/return lifecycle is byte-identical. The QUOTED leg parks the run PAUSED
reusing the SAME parked-leg/human-resume machinery -- no second parking mechanism.

THREE THINGS HAPPEN AT A QUOTED LEG:
  1. QUOTE INTAKE -- the vendor's (or operator's) quote for the leg is recorded on
     `quoted_cost_usd` (+ currency + note). Two paths, ONE service function (`record_quote`):
       (a) MANUAL operator entry (web / CLI / /api / MCP), and
       (b) OVER THE WIRE -- the signed `POST /webhooks/vendor-quotes` callback carries a
           quote message (`apply_quote_message`), authenticated by the SAME HMAC + claim-
           token machinery the return webhook uses (vendor_return.authenticate_callback).
     Recording a quote NEVER moves money and NEVER advances the leg -- it only fills the
     figure the operator will decide on. The estimate -> QUOTE -> actual reconciliation chain
     sits alongside `estimate_cost_usd` (untouched) and `actual_cost_usd` (set on return).
  2. APPROVAL (`approve_quote`) -- a SPEND decision. It records the approved amount, stamps
     `quote_approved_at`, flips QUOTED -> DISPATCHED (work proceeds; a return may now be
     admitted), restarts the bounded-wait clock (the work window begins now), and best-effort
     NOTIFIES the vendor (a signed `quote_decision` control message) so it may start work.
  3. DECLINE (`decline_quote`) -- the operator will not proceed at this quote. It reuses the
     EXISTING escalate-to-a-human-review-pause transition (decision 3): the leg -> ESCALATED,
     a person owns the parked run, and the vendor is best-effort notified it was declined. No
     new terminal state for decline.

TENANT ISOLATION mirrors the return path exactly: the webhook authenticates by the vendor's
HMAC secret + the claim token (no session); the manual/`/api`/MCP callers are admin-gated and
org-ring the leg by its BUYER org. A quote message that does not name the exact leg it was
signed for is refused (the F-MED-3 body-claim binding, reused here).

Documented in: docs/architecture/vendor-outsourcing.md
"""

from __future__ import annotations

import json
import logging
import math
from datetime import datetime

from sqlmodel import Session

from app.models import (
    Vendor,
    VendorDispatch,
    VendorDispatchStatus,
    WorkflowRun,
    WorkflowStep,
)
from app.services import vendor_dispatch, vendor_return

log = logging.getLogger("loc_tms.vendor_quote")

#: The quote-message wire version (independent of the dispatch CONTRACT_VERSION, which stays
#: 1.0 -- the quote message is a NEW wire message, versioned in its own field).
QUOTE_MESSAGE_VERSION = "1.0"


# --- errors -------------------------------------------------------------------
class VendorQuoteError(RuntimeError):
    """Base for every quote-gate refusal. Carries a clean, surface-shared message. The CLI /
    /api / MCP callers catch the BASE and map it to a usage/4xx error, exactly as the return
    surfaces catch VendorReturnError."""


class QuoteStateError(VendorQuoteError):
    """The leg cannot take this quote action: it is not a quote-gated QUOTED leg (never
    quote-gated, already approved, cancelled, returned, or escalated), or its run moved on."""


class QuoteContractError(VendorQuoteError):
    """The quote message/amount is malformed: a non-finite/negative amount, a claim token
    that does not bind to the leg, or an unparseable body."""


class QuoteAuthError(VendorQuoteError):
    """The signed quote webhook could not prove it holds the leg (bad/absent claim token or
    signature). DELIBERATELY UNIFORM on the public webhook, exactly like ReturnAuthError."""


# --- amount validation --------------------------------------------------------
def _finite_amount(value):
    """A quote amount coerced to a sane float, or None. Refuses NaN/Infinity/negatives the
    same way the rate-card + actual-cost validators do (a stored NaN permanently 500s every
    JSON surface that renders the leg; a negative quote is not a thing)."""
    if value is None:
        return None
    try:
        f = float(value)
    except (TypeError, ValueError) as e:
        raise QuoteContractError(f"quoted_cost_usd must be a number (got {value!r}).") from e
    if not math.isfinite(f) or f < 0:
        raise QuoteContractError(f"quoted_cost_usd must be a finite number >= 0 (got {value!r}).")
    return f


def _clean_currency(value) -> str:
    """A 3-letter ISO-4217-shaped currency label, uppercased, or '' when absent/malformed
    (an over-long / non-alpha value is dropped, never stored -- mirrors the return path's
    actual_currency validation)."""
    if value is None:
        return ""
    cur = str(value).strip().upper()
    return cur if (len(cur) == 3 and cur.isascii() and cur.isalpha()) else ""


# --- leg-state guard ----------------------------------------------------------
def assert_quotable(leg: VendorDispatch) -> None:
    """Raise QuoteStateError unless `leg` is a quote-gated leg currently AWAITING a quote /
    approval (status QUOTED). A leg that was never quote-gated, already approved (DISPATCHED),
    returned, cancelled or escalated cannot take a quote action."""
    status = leg.status or ""
    if status == VendorDispatchStatus.QUOTED:
        return
    if not leg.requires_quote:
        raise QuoteStateError(
            f"dispatch {leg.id} is not quote-gated (the vendor does not require quote "
            "approval); there is no quote to record or approve."
        )
    raise QuoteStateError(
        f"dispatch {leg.id} is in state {status!r}, not awaiting a quote; no quote action "
        "applies (it may have been approved, declined, returned or cancelled)."
    )


# --- quote intake (manual + wire share this) ---------------------------------
def record_quote(
    session: Session,
    leg: VendorDispatch,
    *,
    quoted_cost_usd,
    currency: str = "",
    note: str = "",
    org_id=None,
    actor: str = "system",
    source: str = "manual",
) -> dict:
    """Record a quote on a QUOTED leg (the vendor/operator's price for the work). Does NOT
    advance the leg -- approval is the separate spend decision. `quoted_cost_usd` is required
    and validated (finite, >= 0). `org_id`, when given, scopes the leg to the caller's org.
    Raises QuoteStateError / QuoteContractError. Returns a summary dict."""
    if org_id is not None and getattr(leg, "org_id", None) != org_id:
        raise QuoteStateError(f"no such dispatch: {leg.id}")  # non-enumerable, like returns
    assert_quotable(leg)
    amount = _finite_amount(quoted_cost_usd)
    if amount is None:
        raise QuoteContractError("a quote must carry a numeric `quoted_cost_usd`.")
    leg.quoted_cost_usd = amount
    leg.quote_currency = _clean_currency(currency) or leg.quote_currency or "USD"
    leg.quoted_at = datetime.utcnow()
    if note:
        leg.quote_note = str(note)[:2000]
    session.add(leg)
    session.commit()
    from app.services import audit

    audit.log(
        actor=actor or "system",
        action="vendor.quote_recorded",
        target=f"dispatch {leg.id} (vendor {leg.vendor_id})",
        detail=f"{source}: {amount} {leg.quote_currency}",
        customer_id=leg.customer_id,
    )
    return {
        "ok": True,
        "dispatch_id": leg.id,
        "run_id": leg.workflow_run_id,
        "status": leg.status,
        "quoted_cost_usd": leg.quoted_cost_usd,
        "quote_currency": leg.quote_currency,
        "estimate_cost_usd": leg.estimate_cost_usd,
        "source": source,
    }


# --- the signed quote webhook (transport b) ----------------------------------
def apply_quote_message(session: Session, leg: VendorDispatch, raw) -> dict:
    """Parse + apply a signed quote message that arrived on `POST /webhooks/vendor-quotes`
    for the AUTHENTICATED leg (the webhook route already ran vendor_return.authenticate_
    callback, so `leg` is HMAC-proven). The body must be JSON `{type?: "quote", claim_token,
    quoted_cost_usd, currency?, note?}`; the embedded claim_token MUST bind to `leg` (the
    F-MED-3 body-claim binding, so a body signed for leg A cannot be replayed into leg B of
    the same vendor). Raises QuoteContractError / QuoteStateError."""
    if isinstance(raw, dict):
        data = raw
    else:
        try:
            data = json.loads(
                raw.decode("utf-8") if isinstance(raw, (bytes, bytearray)) else str(raw)
            )
        except (ValueError, UnicodeDecodeError) as exc:
            raise QuoteContractError(f"quote message is not valid JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise QuoteContractError("quote message must be a JSON object.")
    claim = data.get("claim_token")
    if claim is None or not str(claim).strip():
        raise QuoteContractError(
            "the quote message must carry a `claim_token` binding it to the dispatch leg."
        )
    if not vendor_return._ct_eq(str(claim).strip(), leg.claim_token):
        raise QuoteContractError(
            "the claim token in the quote message does not match the leg it was "
            "authenticated against; the quote is refused."
        )
    return record_quote(
        session,
        leg,
        quoted_cost_usd=data.get("quoted_cost_usd"),
        currency=data.get("currency") or data.get("quote_currency") or "",
        note=data.get("note") or "",
        actor="vendor-quote-callback",
        source="wire",
    )


# --- approval (a spend decision) ---------------------------------------------
def approve_quote(
    session: Session,
    leg: VendorDispatch,
    *,
    quoted_cost_usd=None,
    currency: str = "",
    note: str = "",
    org_id=None,
    actor: str = "system",
    transport=None,
) -> dict:
    """Approve a QUOTED leg's quote (a SPEND decision): record the approved amount (if one is
    passed and none was recorded, or to override), stamp `quote_approved_at`, flip QUOTED ->
    DISPATCHED so work proceeds and a return may be admitted, restart the bounded-wait clock
    (the work window begins now), and best-effort NOTIFY the vendor so it may start work.

    A quote figure must exist (recorded earlier, or supplied here) -- approving a $0/unknown
    quote is refused, since approval is a spend authorization. Raises QuoteStateError /
    QuoteContractError. Returns a summary dict."""
    if org_id is not None and getattr(leg, "org_id", None) != org_id:
        raise QuoteStateError(f"no such dispatch: {leg.id}")
    assert_quotable(leg)
    # Finding #4: mirror decline_quote's run-terminal guard. If the run is gone or terminal
    # (cancelled / finished), approval must NOT re-open the leg DISPATCHED, restart the clock,
    # or notify the vendor -- CLOSE the leg CANCELLED and refuse, so approval never resurrects
    # a run that moved on or authorizes work on a killed run.
    run = session.get(WorkflowRun, leg.workflow_run_id)
    if run is None or vendor_dispatch.is_run_terminal(run.status):
        leg.status = VendorDispatchStatus.CANCELLED
        leg.escalated_at = datetime.utcnow()
        # The run moved on -> the leg is CANCELLED, no delivery pending -> clear any held package
        # (a wire-less QUOTED leg's frozen operator-delivery package). Single-sourced lifecycle
        # (vendor_dispatch._clear_or_keep_frozen), mirroring decline_quote's run-terminal branch.
        vendor_dispatch._clear_or_keep_frozen(leg, VendorDispatchStatus.CANCELLED)
        session.add(leg)
        session.commit()
        raise QuoteStateError(
            f"dispatch {leg.id}: its run is no longer active; the quote cannot be approved "
            "(the leg was closed)."
        )
    if quoted_cost_usd is not None:
        amount = _finite_amount(quoted_cost_usd)
        leg.quoted_cost_usd = amount
        leg.quoted_at = leg.quoted_at or datetime.utcnow()
        cur = _clean_currency(currency)
        if cur:
            leg.quote_currency = cur
    # Finding #10: a quote figure must exist AND be positive -- approval is a SPEND
    # authorization, so a $0/unknown quote is refused (aligning the guard with the docstring).
    if leg.quoted_cost_usd is None or leg.quoted_cost_usd <= 0:
        raise QuoteContractError(
            f"dispatch {leg.id} has no positive quote to approve; record the vendor's quote "
            "(> 0) first, or pass the amount to approve in one step."
        )
    if note:
        leg.quote_note = str(note)[:2000]
    now = datetime.utcnow()
    leg.quote_approved_at = now
    leg.status = VendorDispatchStatus.DISPATCHED
    # Restart the bounded-wait clock from approval: the vendor's WORK window begins now (the
    # quote-wait window is over). Reuses expiry_for exactly like a delivered/retried dispatch.
    step = session.get(WorkflowStep, leg.step_id) if leg.step_id else None
    if step is not None:
        leg.expires_at = vendor_dispatch.expiry_for(step, now=now)
    session.add(leg)
    session.commit()
    from app.services import audit

    audit.log(
        actor=actor or "system",
        action="vendor.quote_approved",
        target=f"dispatch {leg.id} (vendor {leg.vendor_id})",
        detail=f"approved {leg.quoted_cost_usd} {leg.quote_currency or 'USD'}; work proceeds",
        customer_id=leg.customer_id,
    )
    notify_quote_decision(session, leg, decision="approved", transport=transport)
    return {
        "ok": True,
        "dispatch_id": leg.id,
        "run_id": leg.workflow_run_id,
        "status": leg.status,
        "approved": True,
        "quoted_cost_usd": leg.quoted_cost_usd,
        "quote_currency": leg.quote_currency or "USD",
        "estimate_cost_usd": leg.estimate_cost_usd,
    }


# --- decline (reuse the escalate-to-human transition) ------------------------
def decline_quote(
    session: Session,
    leg: VendorDispatch,
    *,
    reason: str = "",
    org_id=None,
    actor: str = "system",
    transport=None,
) -> dict:
    """Decline a QUOTED leg's quote: the operator will not proceed at this price. Reuses the
    EXISTING escalate-to-a-human-review-pause transition (decision 3) -- the leg -> ESCALATED
    and a person owns the parked run to re-route or cancel it -- rather than a new terminal
    decline state. Best-effort NOTIFIES the vendor it was declined. Raises QuoteStateError.
    Returns a summary dict."""
    if org_id is not None and getattr(leg, "org_id", None) != org_id:
        raise QuoteStateError(f"no such dispatch: {leg.id}")
    assert_quotable(leg)
    run = session.get(WorkflowRun, leg.workflow_run_id)
    note = (
        f"vendor quote for dispatch {leg.id} ({leg.target_locale}) was DECLINED"
        + (f": {str(reason)[:200]}" if reason else "")
        + "; escalated to human review"
    )
    if run is None or vendor_dispatch.is_run_terminal(run.status):
        # The run moved on (cancelled / finished) -- just close the leg, never resurrect it.
        leg.status = VendorDispatchStatus.CANCELLED
        leg.escalated_at = datetime.utcnow()
        # The run moved on -> the leg is CANCELLED, no delivery pending -> clear any held
        # package (a wire-less QUOTED leg's frozen operator-delivery package). Single-sourced
        # lifecycle (vendor_dispatch._clear_or_keep_frozen).
        vendor_dispatch._clear_or_keep_frozen(leg, VendorDispatchStatus.CANCELLED)
        session.add(leg)
        session.commit()
        notify_quote_decision(session, leg, decision="declined", transport=transport)
        return {
            "ok": True,
            "dispatch_id": leg.id,
            "run_id": leg.workflow_run_id,
            "status": leg.status,
            "declined": True,
            "escalated": False,
        }
    leg.status = VendorDispatchStatus.ESCALATED
    leg.escalated_at = datetime.utcnow()
    # ESCALATED is still workable: a WIRE-LESS leg KEEPS its operator-delivery package (the
    # operator must still be able to download the correct file), consistent with escalate_leg;
    # a native leg holds none. Single-sourced lifecycle (vendor_dispatch._clear_or_keep_frozen).
    vendor_dispatch._clear_or_keep_frozen(leg, VendorDispatchStatus.ESCALATED)
    vendor_dispatch._escalate_run_to_human(
        session, leg, run, note=note, action="vendor.quote_declined", actor=actor or "system"
    )
    notify_quote_decision(session, leg, decision="declined", reason=reason, transport=transport)
    return {
        "ok": True,
        "dispatch_id": leg.id,
        "run_id": leg.workflow_run_id,
        "status": leg.status,
        "declined": True,
        "escalated": True,
    }


# --- vendor notification (best-effort) ---------------------------------------
def notify_quote_decision(
    session: Session, leg: VendorDispatch, *, decision: str, reason: str = "", transport=None
) -> bool:
    """Best-effort: tell the vendor of an approve/decline decision so it may start (or stop)
    work. Sends a signed `quote_decision` control message (NO translation content) to the
    vendor's endpoint via vendor_dispatch.post_signed_message. NEVER raises and NEVER changes
    the caller's outcome -- the decision is already committed on OUR side, and the return-side
    gate (a QUOTED leg admits no return) is the real enforcement; this is a courtesy so Vendor
    Studio's inbox can react. Returns True when the notice was delivered, else False."""
    vendor = session.get(Vendor, leg.vendor_id) if leg.vendor_id else None
    if vendor is None or not (vendor.endpoint_url or "").strip():
        return False
    if vendor_dispatch.vendor_protocol(vendor) != vendor_dispatch.NATIVE_PROTOCOL:
        # F7 (ACCEPTED RESIDUAL): this keys off the vendor's LIVE protocol rather than the
        # leg's F2 snapshot, on purpose. The quote-decision notice is a best-effort COURTESY
        # to the vendor's inbox -- it carries NO translation content and makes no admission
        # decision (the QUOTED-leg return gate is the real enforcement), so if the vendor was
        # flipped to a wireless protocol after the leg was dispatched, silently skipping the
        # courtesy notice is harmless and correct. There is nothing protocol-sensitive here to
        # protect, so snapshotting the leg's protocol would add machinery for no behavior change.
        return False  # a future adapter maps this onto the vendor's own API; native only now
    body = {
        "type": "quote_decision",
        "message_version": QUOTE_MESSAGE_VERSION,
        "claim_token": leg.claim_token,
        "decision": decision,
        "quoted_cost_usd": leg.quoted_cost_usd,
        "quote_currency": leg.quote_currency or "USD",
    }
    if reason:
        body["reason"] = str(reason)[:500]
    try:
        vendor_dispatch.post_signed_message(vendor, body, transport=transport)
        return True
    except Exception as exc:
        log.info(
            "quote-decision notice to vendor %s (dispatch %s) failed (best-effort): %s",
            leg.vendor_id,
            leg.id,
            vendor_dispatch.sanitize_error(exc),
        )
        return False


__all__ = [
    "QUOTE_MESSAGE_VERSION",
    "QuoteAuthError",
    "QuoteContractError",
    "QuoteStateError",
    "VendorQuoteError",
    "apply_quote_message",
    "approve_quote",
    "assert_quotable",
    "decline_quote",
    "notify_quote_decision",
    "record_quote",
]
