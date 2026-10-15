"""vendor_dispatch: second pass part 2 (layered pure move)

Documented in: docs/architecture/vendor-outsourcing.md
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta

import httpx
from sqlmodel import Session

from app.models import (
    Document,
    VendorDispatch,
    VendorDispatchStatus,
    VendorStatus,
    WorkflowRun,
    WorkflowStep,
)
from app.services import vendor_cost, vendors
from app.services.vendor_dispatch_common import (
    VendorDispatchError,
    VendorPolicyRefusal,
    _endpoint_host,
    _frozen_package,
    new_claim_token,
    next_backoff,
    sanitize_error,
)
from app.services.vendor_dispatch_hub import (
    _FROZEN_PACKAGE_KEEP_STATUSES,
    _effective_transport,
    _leg_is_wireless,
    _queries_enabled,
    _refuse_when_vendors_off,
    assert_dispatch_egress_allowed,
    build_contract,
    collect_outbound_queries,
    dispatch,
    record_dispatch,
    resolve_vendor,
    transport_for,
)
from app.services.vendor_dispatch_part2 import (
    _touch_vendor_error,
    expiry_for,
)

log = logging.getLogger("loc_tms.vendor_dispatch")


def _clear_or_keep_frozen(leg, new_status) -> None:
    """SINGLE SOURCE OF TRUTH for the `retry_contract` column's LIFECYCLE at EVERY status
    transition. The column carries TWO status-keyed meanings, each with its own lifetime:

      * NATIVE re-send package (a RETRY_WAIT leg): the exact contract a scheduled retry will
        re-send. CLEARED on EVERY exit from RETRY_WAIT. A native leg is never wire-less, so it
        always falls to the CLEAR branch below -- byte-identical to the historic scattered
        `leg.retry_contract = None` writes.
      * WIRE-LESS operator-delivery package (a `manual` leg): the frozen DISPATCH-TIME contract
        the operator downloads AS the delivery (AUDIT-omegat-roundtrip-2026-08-10; see
        _frozen_package). It must survive as long as the leg is WORKABLE -- the operator must
        still be able to download the CORRECT file while the leg is DISPATCHED / QUOTED /
        ESCALATED (a return is refused on ESCALATED, but the delivery file must never silently
        regress to the empty mid-run rebuild). It is CLEARED only when the leg reaches a state
        with NO pending delivery (RETURNED / REJECTED / CANCELLED / DEAD_LETTER).

    Wire-less-ness is read from the leg's PROTOCOL SNAPSHOT (never the live vendor). Callers
    pass the status the leg is transitioning INTO; the helper does NOT set leg.status (the
    caller owns that) -- it only decides `retry_contract`."""
    if _leg_is_wireless(leg) and new_status in _FROZEN_PACKAGE_KEEP_STATUSES:
        return
    leg.retry_contract = None


# --- the one entry point the EXTERNAL branch calls ---------------------------
def dispatch_external_step(
    session: Session,
    *,
    run: WorkflowRun,
    step: WorkflowStep,
    document_id,
    segments: list[dict],
    source_locale: str,
    target_locale: str,
    customer_id=None,
    org_id=None,
    project=None,
    glossary=None,
    style_guide: str = "",
    context: str = "",
    transport: httpx.BaseTransport | None = None,
) -> VendorDispatch:
    """Resolve the vendor (fail-closed), price the packaging, build + sign + send the
    contract through the SSRF guard, and record the dispatch leg. Returns the
    VendorDispatch on success; raises VendorError (config / fail-closed), ConnectorError
    (SSRF/bad scheme) or VendorDispatchError (delivery) on failure — the EXTERNAL branch
    turns any of those into a FAILED step, and only parks PAUSED on success.

    `segments` are the caller's in-flight per-segment dicts (so the estimate + contract
    describe EXACTLY what is dispatched, not a re-query)."""
    # ffadmin Wave 3 (MEDIUM-3): refuse at the TOP, BEFORE resolve_vendor and any vendor
    # bookkeeping. The inner dispatch() seam raises the same ConnectorError, but its
    # `except ConnectorError` branch below runs _touch_vendor_error, which would stamp
    # status=ERROR + last_message onto a HEALTHY vendor merely because the feature is off.
    # Refusing here (the run FAILS via the orchestrator's ConnectorError handler with a
    # readable reason) leaves every vendor row untouched. dispatch()/post_signed_message()
    # keep their own seam as depth (they also guard retry_leg's direct dispatch() call).
    _refuse_when_vendors_off()
    # --- per-PROJECT USD budget (CV-2) -----------------------------------------
    # A NEW vendor dispatch commissions paid HUMAN work, so it is gated by the same
    # project money cap as a machine-translate submission: at 100% of the project's
    # monthly_usd_limit (ENFORCED spend = AI + settled vendor human), refuse the
    # dispatch. Fail-closed BEFORE resolve_vendor / any egress, raising VendorError so
    # the orchestrator's EXTERNAL branch FAILS the step with a readable reason (never a
    # half-sent contract). The project is `project.id` when the caller passed the row,
    # else resolved from the dispatched document. Human review/editing is not here and
    # is never blocked; an in-flight run's ALREADY-dispatched legs are untouched.
    _pb_project_id = getattr(project, "id", None)
    if _pb_project_id is None and document_id is not None:
        _pb_doc = session.get(Document, document_id)
        _pb_project_id = getattr(_pb_doc, "project_id", None)
    if _pb_project_id is not None:
        from app.services import project_budgets

        # Keyed on the project's UNIQUE id (org resolved from the project, never the
        # caller — H2): one project, one enforcement answer at every seam. project_check
        # opens its own session; evaluate_thresholds opens its own too (M3).
        _pb_ok, _pb_rem, _pb_msg = project_budgets.project_check(_pb_project_id)
        if not _pb_ok:
            # H1: the blocked dispatch is the 100% crossing — alert before refusing.
            project_budgets.evaluate_thresholds(_pb_project_id, actor="system")
            raise vendors.VendorError(
                f"{_pb_msg} New vendor dispatches are blocked until the project's "
                f"monthly spend budget is raised or the month rolls over."
            )
        project_budgets.evaluate_thresholds(_pb_project_id, actor="system")
    vendor = resolve_vendor(session, step, customer_id=customer_id, org_id=org_id)
    # H2: the mock-egress perimeter -- refuse offline/mock content to a REAL endpoint
    # BEFORE anything is built or sent. Uses the SAME effective transport dispatch() will.
    # (POLICY refusal -- MockEgressBlocked propagates and FAILS the step, decision below.)
    # SKIPPED for a WIRELESS transport (`manual` file-exchange): the perimeter exists to stop
    # deterministic offline placeholder text reaching a REAL vendor ENDPOINT over the wire, and
    # a manual vendor has no endpoint and no wire -- nothing leaves the process, the operator
    # mediates any hand-off (they see the content when they download it). There is simply no
    # egress to guard here.
    # F5: resolve the transport implementation ONCE and key BOTH the egress-skip and the
    # host-recording decisions off its `wireless` flag (rather than re-deriving the protocol at
    # each site). The leg's protocol snapshot is captured from the SAME vendor by record_dispatch.
    impl = transport_for(vendor)
    # #7c: read `wireless` through getattr, FAILING CLOSED to wired (False) — an impl missing
    # the attribute is treated as a wired transport, so a defect can never silently skip the
    # egress perimeter / drop the endpoint host for something that actually rides the wire.
    _is_manual = getattr(impl, "wireless", False)
    if not _is_manual:
        assert_dispatch_egress_allowed(
            session, project, target_locale, _effective_transport(transport)
        )
    claim_token = new_claim_token()
    estimate = vendor_cost.estimate_from_segments(segments, vendors.rate_card_for(vendor))
    # Wave 3 Train E (§7.2): resolve the buyer query carry here (we hold the session), then
    # hand build_contract a plain mapping (it stays DB-free + purely testable). The whole
    # assembly is guarded: a failure degrades to NO queries, never a failed dispatch, and
    # the `queries_enabled` kill switch (§5.2) OMITS the carry entirely when off.
    queries_by_segment = None
    if _queries_enabled():
        try:
            queries_by_segment = collect_outbound_queries(
                session, [s.get("id") for s in (segments or [])], target_locale
            )
        except Exception:
            log.warning("outbound query carry failed; dispatching without queries", exc_info=True)
            queries_by_segment = None
    contract = build_contract(
        run=run,
        step=step,
        segments=segments,
        source_locale=source_locale,
        target_locale=target_locale,
        claim_token=claim_token,
        glossary=glossary,
        style_guide=style_guide,
        context=context,
        vendor=vendor,
        queries_by_segment=queries_by_segment,
    )
    dispatched_ids = [s.get("id") for s in contract.get("segments") or []]
    # VP-0: the ids whose target was BLANKED by omit_locked_targets (exactly the locked
    # segments when the flag is on). A blank return for one of these is a return-side no-op.
    omitted_ids = (
        [s.get("id") for s in contract.get("segments") or [] if s.get("locked")]
        if vendors.omit_locked_targets(vendor)
        else []
    )
    # Wave E R3: snapshot the quote-gate flag now (the leg remembers it even if the vendor
    # config changes while parked). A DELIVERED quote-gated dispatch is recorded QUOTED (not
    # DISPATCHED): the run parks awaiting a quote + operator approval before work proceeds.
    _requires_quote = vendors.requires_quote_approval(vendor)
    _delivered_status = (
        VendorDispatchStatus.QUOTED if _requires_quote else VendorDispatchStatus.DISPATCHED
    )
    # A manual (file-exchange) leg records NO host: there is no endpoint. Even if a manual
    # vendor harmlessly carries endpoint_url (ignored by the transport), the leg -- and every
    # surface that shows it -- must report an empty host, since nothing was sent anywhere.
    host = "" if _is_manual else _endpoint_host(vendor.endpoint_url)
    # Wave C two-class dispatch outcome (interop R5, decision 3):
    #   * POLICY refusal  (ConnectorError = SSRF guard / bad scheme / non-https) is
    #     deterministic -- a retry cannot help -- so it FAILS the step exactly as before.
    #     (VendorError / MockEgressBlocked, the other policy refusals, are raised earlier
    #     by resolve_vendor / the egress perimeter and propagate straight through.)
    #   * TRANSIENT failure (VendorDispatchError = network error / timeout / non-2xx from
    #     the vendor) parks a RETRY_WAIT leg (the run pauses exactly like a delivered
    #     dispatch) that scheduler.retry_due_legs re-attempts on the bounded backoff; on
    #     exhaustion it dead-letters + escalates to a human (never hard-fails).
    from app.services.connectors.base import ConnectorError

    try:
        dispatch(vendor, contract, transport=transport, impl=impl)
    except ConnectorError as exc:
        # A POLICY delivery refusal from OUR guard (SSRF / bad scheme / non-https) marks the
        # vendor ERROR with the reason (best-effort) and propagates to FAIL the step --
        # byte-identical to the pre-Wave-C behavior (a misconfiguration to fix, not vendor
        # unreachability).
        _touch_vendor_error(session, vendor, exc)
        raise
    except VendorPolicyRefusal as exc:
        # M4/decision 3: a NON-RETRYABLE vendor response (3xx / non-retryable 4xx / an
        # unregistered protocol) is not silently failed and not retried -- record a
        # DEAD_LETTER leg (no retry_contract; it is never re-sent) and let the run PARK
        # PAUSED via the orchestrator's normal flow, i.e. ESCALATED to a human who decides.
        leg = record_dispatch(
            session,
            vendor=vendor,
            run=run,
            step=step,
            document_id=document_id,
            target_locale=target_locale,
            claim_token=claim_token,
            endpoint_host=host,
            estimate=estimate,
            segment_count=len(segments or []),
            word_count=int(estimate.get("total_words") or 0),
            expires_at=expiry_for(step),
            org_id=org_id,
            customer_id=customer_id,
            dispatched_ids=dispatched_ids,
            omitted_ids=omitted_ids,
            status=VendorDispatchStatus.DEAD_LETTER,
            attempt_count=1,
            last_error=sanitize_error(exc),
            retry_contract=None,
            # #7c: snapshot the transport ACTUALLY used (impl.name), not a re-read of the
            # vendor — structural, so the leg names the transport it was dispatched through.
            requires_quote=_requires_quote,
            protocol=impl.name,
        )
        leg.escalated_at = datetime.utcnow()
        _note = (
            f"external dispatch to vendor {leg.vendor_id} ({target_locale}) was "
            f"refused (non-retryable); dead-lettered and escalated to human review"
        )
        run.notes = f"{run.notes}; {_note}" if (run.notes or "").strip() else _note
        session.add(leg)
        session.add(run)
        session.commit()
        _touch_vendor_error(session, vendor, exc)
        from app.services import audit

        audit.log(
            actor="system",
            action="vendor.dispatch_dead_lettered",
            target=f"dispatch {leg.id} (vendor {leg.vendor_id})",
            detail=f"run {run.id} {target_locale}: policy refusal at first dispatch",
            customer_id=customer_id,
        )
        log.warning(
            "vendor dispatch to %s dead-lettered at first dispatch (leg %s): %s",
            host,
            leg.id,
            sanitize_error(exc),
        )
        return leg
    except VendorDispatchError as exc:
        # TRANSIENT: park a RETRY_WAIT leg holding the exact package to re-send.
        leg = record_dispatch(
            session,
            vendor=vendor,
            run=run,
            step=step,
            document_id=document_id,
            target_locale=target_locale,
            claim_token=claim_token,
            endpoint_host=host,
            estimate=estimate,
            segment_count=len(segments or []),
            word_count=int(estimate.get("total_words") or 0),
            expires_at=expiry_for(step),
            org_id=org_id,
            customer_id=customer_id,
            dispatched_ids=dispatched_ids,
            omitted_ids=omitted_ids,
            status=VendorDispatchStatus.RETRY_WAIT,
            attempt_count=1,
            next_attempt_at=datetime.utcnow() + (next_backoff(1) or timedelta(minutes=1)),
            last_error=sanitize_error(exc),
            retry_contract=contract,
            requires_quote=_requires_quote,
            protocol=impl.name,
        )  # #7c: transport used
        _touch_vendor_error(session, vendor, exc)
        log.warning(
            "vendor dispatch to %s parked RETRY_WAIT (leg %s): %s",
            host,
            leg.id,
            sanitize_error(exc),
        )
        return leg
    leg = record_dispatch(
        session,
        vendor=vendor,
        run=run,
        step=step,
        document_id=document_id,
        target_locale=target_locale,
        claim_token=claim_token,
        endpoint_host=host,
        estimate=estimate,
        segment_count=len(segments or []),
        word_count=int(estimate.get("total_words") or 0),
        expires_at=expiry_for(step),
        org_id=org_id,
        customer_id=customer_id,
        # Wave B: freeze the id set the contract actually carried (read off the built
        # contract, never re-derived from segments, so the ledger and the wire agree).
        dispatched_ids=dispatched_ids,
        omitted_ids=omitted_ids,
        # Wave E R3: QUOTED (awaiting a quote + approval) when the vendor is quote-gated,
        # else DISPATCHED (today's behavior, byte-identical when the flag is off).
        # #7c: snapshot the transport ACTUALLY used (impl.name) rather than re-reading the
        # vendor's live protocol — structural, and == vendor_protocol(vendor) at this instant.
        status=_delivered_status,
        requires_quote=_requires_quote,
        protocol=impl.name,
        # FREEZE the dispatch-time package on a WIRE-LESS (manual) leg so the operator
        # download renders it verbatim (AUDIT-omegat-roundtrip-2026-08-10): the download IS
        # the delivery for a manual vendor, and rebuilding from the live Segment rows mid-run
        # yields EMPTY targets. A NATIVE leg stores NOTHING here (retry_contract stays NULL,
        # byte-identical) -- its signed wire JSON remains authoritative. See _frozen_package.
        retry_contract=(_frozen_package(contract) if _is_manual else None),
    )
    # Best-effort status touch (never blocks dispatch): mark the vendor CONNECTED.
    try:
        vendor.status = VendorStatus.CONNECTED
        _dest = host or "manual file-exchange (download + hand off)"
        vendor.last_message = f"dispatched {leg.segment_count} segment(s) to {_dest}"
        session.add(vendor)
        session.commit()
    except Exception:
        log.exception("vendor status touch failed for vendor %s", vendor.id)
    return leg
