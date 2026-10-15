"""The dispatch-and-wait engine for the EXTERNAL workflow step (PROPOSAL-external-
processing-step §3/§9; combined build Wave A).

Transitional compatibility shim for one release (added 2026-09-15, remove the re-export block after
the next tagged release): `__all__` re-exports log, GLOSSARY_TERM_CAP, _glossary_asset,
_TEST_TRANSPORT, set_test_transport, _refuse_when_vendors_off, _effective_transport,
assert_dispatch_egress_allowed, resolve_vendor, _QUERIES_PER_SEGMENT, _POSTS_PER_QUERY,
_queries_enabled, collect_outbound_queries, build_contract, NATIVE_PROTOCOL, MANUAL_PROTOCOL,
_NativeTransport, _ManualTransport, _PROTOCOLS, protocol_names, is_known_protocol, vendor_protocol,
leg_protocol, transport_for_protocol, transport_for, dispatch, _native_send, post_signed_message,
record_dispatch, _FROZEN_PACKAGE_KEEP_STATUSES, _leg_is_wireless, leg_is_wireless,
_clear_or_keep_frozen, dispatch_external_step from app.services.vendor_dispatch_hub,
app.services.vendor_dispatch_hub2.

Transitional compatibility shim for one release (added 2026-09-14, remove the re-export block after
the next tagged release): `__all__` re-exports log, CONTRACT_VERSION, _PLACEHOLDER_RE,
_placeholders, VendorDispatchError, VendorPolicyRefusal, _RETRYABLE_STATUS, _is_retryable_status,
_parse_retry_after, new_claim_token, _author_role, _as_segment_int, _FORBIDDEN_CONFIG_HEADERS,
_safe_config_headers, _endpoint_host, RETRY_BACKOFF_SCHEDULE, RETRY_AFTER_FLOOR_SECONDS,
next_backoff, _URL_RE, _BEARER_RE, _SECRETISH_RE, _PATH_RE, sanitize_error, _manual_send,
wait_hours_for, _frozen_package, _TERMINAL_RUN_STATUSES, is_run_terminal, _BLOCKING_PAUSE_STATES,
blocks_human_resume, vendor_pause_state, due_retry_legs, _run_paused_at_leg_step, expiry_for,
_touch_vendor_error, _escalate_run_to_human, open_leg_at_current_pause from
app.services.vendor_dispatch_common, app.services.vendor_dispatch_part2.

This is the SEND half of the shared substrate (the guarded RETURN is Wave B). It:
  * resolves the step's vendor FAIL-CLOSED against the document's customer's linked
    vendors (vendors.vendor_for_customer);
  * builds the versioned JSON CONTRACT (segments + ids + locales + optional glossary/
    style/context) — the §3.2 shape, with a per-segment `state`/`state_qualifier`/
    `translate` vocabulary aligned to XLIFF 1.2 so the vendor XLIFF profile slots in
    later (the XLIFF lock-interchange research);
  * mints a high-entropy CLAIM TOKEN (Wave B matches a return to the exact run/step/
    locale by it);
  * DISPATCHES outbound through the EXISTING SSRF guard (connectors.base._validate_target)
    with an HMAC signature over the raw body — the injectable httpx transport seam every
    connector uses, so hermetic tests pass an httpx.MockTransport and NO real network is
    touched;
  * records a VendorDispatch (claim token + persisted scoping estimate + host-only
    endpoint) so Wave B can reconcile the vendor's reported actual against the estimate.

EXPIRY (decision 3): a parked leg whose vendor never returns is NEVER hard-failed; on
expiry it ESCALATES to a human_review pause so a person decides. `sweep_expired` is the
service entry point (Wave B wires it into the poller/scheduler); it reuses the human-
review notification.

Documented in: docs/architecture/vendor-outsourcing.md
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import re
import secrets
from datetime import datetime, timedelta
from urllib.parse import urlsplit

import httpx
from sqlmodel import Session, select

from app.models import (
    Document,
    Project,
    RunStatus,
    StepRun,
    Vendor,
    VendorDispatch,
    VendorDispatchStatus,
    VendorStatus,
    WorkflowRun,
    WorkflowStep,
)
from app.services import egress, vendor_cost, vendors
from app.services.vendor_dispatch_common import (
    _BEARER_RE,
    _BLOCKING_PAUSE_STATES,
    _FORBIDDEN_CONFIG_HEADERS,
    _PATH_RE,
    _PLACEHOLDER_RE,
    _RETRYABLE_STATUS,
    _SECRETISH_RE,
    _TERMINAL_RUN_STATUSES,
    _URL_RE,
    CONTRACT_VERSION,
    RETRY_AFTER_FLOOR_SECONDS,
    RETRY_BACKOFF_SCHEDULE,
    VendorDispatchError,
    VendorPolicyRefusal,
    _as_segment_int,
    _author_role,
    _endpoint_host,
    _frozen_package,
    _is_retryable_status,
    _manual_send,
    _parse_retry_after,
    _placeholders,
    _run_paused_at_leg_step,
    _safe_config_headers,
    blocks_human_resume,
    due_retry_legs,
    is_run_terminal,
    new_claim_token,
    next_backoff,
    sanitize_error,
    vendor_pause_state,
    wait_hours_for,
)
from app.services.vendor_dispatch_hub import (
    _FROZEN_PACKAGE_KEEP_STATUSES,
    _POSTS_PER_QUERY,
    _PROTOCOLS,
    _QUERIES_PER_SEGMENT,
    _TEST_TRANSPORT,
    GLOSSARY_TERM_CAP,
    MANUAL_PROTOCOL,
    NATIVE_PROTOCOL,
    _effective_transport,
    _glossary_asset,
    _leg_is_wireless,
    _ManualTransport,
    _native_send,
    _NativeTransport,
    _queries_enabled,
    _refuse_when_vendors_off,
    assert_dispatch_egress_allowed,
    build_contract,
    collect_outbound_queries,
    dispatch,
    is_known_protocol,
    leg_is_wireless,
    leg_protocol,
    post_signed_message,
    protocol_names,
    record_dispatch,
    resolve_vendor,
    set_test_transport,
    transport_for,
    transport_for_protocol,
    vendor_protocol,
)
from app.services.vendor_dispatch_hub2 import _clear_or_keep_frozen, dispatch_external_step
from app.services.vendor_dispatch_part2 import (
    _escalate_run_to_human,
    _touch_vendor_error,
    expiry_for,
    open_leg_at_current_pause,
)

__all__ = [
    "CONTRACT_VERSION",
    "GLOSSARY_TERM_CAP",
    "MANUAL_PROTOCOL",
    "NATIVE_PROTOCOL",
    "RETRY_AFTER_FLOOR_SECONDS",
    "RETRY_BACKOFF_SCHEDULE",
    "_BEARER_RE",
    "_BLOCKING_PAUSE_STATES",
    "_FORBIDDEN_CONFIG_HEADERS",
    "_FROZEN_PACKAGE_KEEP_STATUSES",
    "_OPEN_LEG_STATUSES",
    "_PATH_RE",
    "_PLACEHOLDER_RE",
    "_POSTS_PER_QUERY",
    "_PROTOCOLS",
    "_QUERIES_PER_SEGMENT",
    "_RETRYABLE_STATUS",
    "_SECRETISH_RE",
    "_TERMINAL_RUN_STATUSES",
    "_TEST_TRANSPORT",
    "_URL_RE",
    "RunStatus",
    "StepRun",
    "VendorDispatchError",
    "VendorPolicyRefusal",
    "_ManualTransport",
    "_NativeTransport",
    "_as_segment_int",
    "_author_role",
    "_clear_or_keep_frozen",
    "_effective_transport",
    "_endpoint_host",
    "_escalate_run_to_human",
    "_frozen_package",
    "_glossary_asset",
    "_is_retryable_status",
    "_leg_is_wireless",
    "_manual_send",
    "_native_send",
    "_parse_retry_after",
    "_placeholders",
    "_queries_enabled",
    "_refuse_when_vendors_off",
    "_run_paused_at_leg_step",
    "_safe_config_headers",
    "_touch_vendor_error",
    "assert_dispatch_egress_allowed",
    "blocks_human_resume",
    "build_contract",
    "close_open_legs_for_run",
    "collect_outbound_queries",
    "dead_letter_leg",
    "dispatch",
    "dispatch_external_step",
    "due_retry_legs",
    "egress",
    "escalate_leg",
    "expired_legs",
    "expiry_for",
    "hashlib",
    "hmac",
    "is_known_protocol",
    "is_run_terminal",
    "leg_is_wireless",
    "leg_protocol",
    "new_claim_token",
    "next_backoff",
    "open_leg_at_current_pause",
    "post_signed_message",
    "protocol_names",
    "re",
    "record_dispatch",
    "resolve_vendor",
    "retry_due_legs",
    "retry_leg",
    "sanitize_error",
    "secrets",
    "set_test_transport",
    "sweep_expired",
    "transport_for",
    "transport_for_protocol",
    "urlsplit",
    "vendor_cost",
    "vendor_pause_state",
    "vendor_protocol",
    "vendors",
    "wait_hours_for",
]


log = logging.getLogger("loc_tms.vendor_dispatch")


# --- expiry escalation (decision 3: ALWAYS escalate to a human_review pause) --
def escalate_leg(session: Session, leg: VendorDispatch, *, actor: str = "system") -> bool:
    """Escalate ONE expired dispatched leg into a human_review pause (decision 3). The
    run is ALREADY PAUSED (the external dispatch parked it); escalation reuses that pause
    — it flips the parked leg's semantics from "awaiting a vendor" to "awaiting a person",
    notifies the human-review assignee (reusing the human-review notification), and stamps
    the leg ESCALATED so the sweep never re-fires. Idempotent: returns False (no-op) when
    the leg is not a still-open (DISPATCHED or RETRY_WAIT) leg. NEVER hard-fails the run.

    HIGH-D: a RETRY_WAIT leg is escalatable too, so the ABOVE-gate expiry sweep expires a
    retrying leg even when the outbound-sync kill switch is OFF (the retry sweep is below
    that switch). Escalating clears the held retry_contract + next_attempt_at."""
    if leg is None or leg.status not in _OPEN_LEG_STATUSES:
        return False
    run = session.get(WorkflowRun, leg.workflow_run_id)
    # H4 (armed trap): NEVER resurrect a run that already moved on. If the run is gone or
    # already terminal (completed/failed) -- because a later run superseded it, a kill
    # switch failed it, or (Wave B) a return resumed+finished it -- the leg is stale: CLOSE
    # it (CANCELLED) so no later sweep touches it, and NEVER flip a finished run back to
    # PAUSED with its finished_at still set.
    if run is None or is_run_terminal(run.status):
        leg.status = VendorDispatchStatus.CANCELLED
        leg.escalated_at = datetime.utcnow()
        # A stale leg for a terminal run is CANCELLED -> no delivery pending -> clear the held
        # package (native re-send OR a wire-less frozen package). Single-sourced lifecycle.
        _clear_or_keep_frozen(leg, VendorDispatchStatus.CANCELLED)
        leg.next_attempt_at = None
        session.add(leg)
        session.commit()
        return False
    leg.status = VendorDispatchStatus.ESCALATED
    leg.escalated_at = datetime.utcnow()
    # A native RETRY_WAIT leg escalated to a human is no longer re-sent (cleared); a WIRE-LESS
    # leg KEEPS its operator-delivery package -- ESCALATED is still workable, the operator must
    # be able to download the correct file (returns are refused on ESCALATED, but the delivery
    # file must never silently regress to the empty mid-run rebuild). Single-sourced lifecycle.
    _clear_or_keep_frozen(leg, VendorDispatchStatus.ESCALATED)
    leg.next_attempt_at = None
    _escalate_run_to_human(
        session,
        leg,
        run,
        note=(
            f"external dispatch to vendor {leg.vendor_id} expired "
            f"({leg.target_locale}); escalated to human review"
        ),
        action="vendor.dispatch_escalated",
        actor=actor,
    )
    return True


#: Non-terminal, open leg statuses a terminal run must close (H4). RETRY_WAIT is included
#: (Wave C, interop R5): a run that finished while a leg was still parked for a backoff
#: retry must have that leg closed too, so the retry sweep can never drag it back to
#: PAUSED. Its held retry_contract is cleared on close. QUOTED is included (Wave E R3): a
#: quote-gated leg awaiting a quote/approval parks the run PAUSED exactly like a dispatched
#: one, so cancelling the run must CLOSE it, the expiry sweep must escalate it if nobody
#: quotes in time, and it must never drag a terminal run back to PAUSED.
_OPEN_LEG_STATUSES = frozenset(
    {VendorDispatchStatus.DISPATCHED, VendorDispatchStatus.RETRY_WAIT, VendorDispatchStatus.QUOTED}
)


def close_open_legs_for_run(session: Session, run_id: int) -> int:
    """H4: CLOSE (CANCELLED) any still-open leg (DISPATCHED or RETRY_WAIT) of a run that has
    left the external step / finished, so a later expiry OR retry sweep can never drag a
    terminal run back to PAUSED. Best-effort; returns the number closed. Wired into the
    run-finalizing wrapper's finally (the ONE seam every launcher funnels through) so it
    fires on completion, failure, and mid-run exit alike. Idempotent (only open legs are
    touched)."""
    n = 0
    try:
        run = session.get(WorkflowRun, run_id) if run_id is not None else None
        if run is None or not is_run_terminal(run.status):
            return 0
        legs = session.exec(
            select(VendorDispatch).where(
                VendorDispatch.workflow_run_id == run_id,
                VendorDispatch.status.in_(tuple(_OPEN_LEG_STATUSES)),
            )
        ).all()
        for leg in legs:
            leg.status = VendorDispatchStatus.CANCELLED
            # Cancelling a leg for a terminal run has no delivery pending -> clear the held
            # package (native re-send OR wire-less frozen package). Single-sourced lifecycle.
            _clear_or_keep_frozen(leg, VendorDispatchStatus.CANCELLED)
            leg.next_attempt_at = None
            session.add(leg)
            n += 1
        if n:
            session.commit()
    except Exception:
        log.exception("close_open_legs_for_run failed for run %s", run_id)
    return n


def expired_legs(session: Session, *, now: datetime | None = None) -> list[VendorDispatch]:
    """Every still-OPEN leg (DISPATCHED or RETRY_WAIT) whose bounded wait has elapsed
    (expires_at in the past). Legs with a NULL expires_at (bounded wait disabled) are never
    expired. A terminal-run leg is INCLUDED here so the sweep visits it -- escalate_leg then
    CLOSES it (H4) rather than escalating, so a stale leg is cleaned up, never resurrecting
    the finished run.

    HIGH-D: RETRY_WAIT is included so this ABOVE-gate expiry sweep expires a retrying leg
    even when the outbound-sync kill switch is OFF (the retry sweep is BELOW that switch, so
    without this a RETRY_WAIT leg would be immortal while sync is disabled). expires_at is
    the leg's overall dispatch deadline, unaffected by the retry backoff clock."""
    now = now or datetime.utcnow()
    rows = session.exec(
        select(VendorDispatch).where(VendorDispatch.status.in_(tuple(_OPEN_LEG_STATUSES)))
    ).all()
    return [d for d in rows if d.expires_at is not None and d.expires_at <= now]


def sweep_expired(session: Session, *, now: datetime | None = None, actor: str = "system") -> int:
    """Escalate every expired dispatched leg to a human_review pause (decision 3). The
    service entry point a poller/scheduler calls (Wave B wires the cadence). Returns the
    number escalated. Best-effort per leg — one bad leg never aborts the sweep."""
    n = 0
    for leg in expired_legs(session, now=now):
        try:
            if escalate_leg(session, leg, actor=actor):
                n += 1
        except Exception:
            log.exception("sweep failed to escalate dispatch %s", leg.id)
    return n


# --- dead-letter / backoff retry sweep (interop R5, Wave C) ------------------
def dead_letter_leg(
    session: Session,
    leg: VendorDispatch,
    run: WorkflowRun,
    exc,
    *,
    actor: str = "scheduler",
    note: str | None = None,
) -> bool:
    """Terminal exhaustion of a RETRY_WAIT leg's backoff window: flip it DEAD_LETTER, clear
    the held package, and ESCALATE to a human_review pause (decision 3 — vendor
    unreachability is NEVER hard-failed; a person decides). Reuses the SAME escalation body
    as expiry. Returns True.

    #6: `note` overrides the escalation note appended to run.notes for a dead-letter that is
    NOT a retry-window exhaustion (e.g. a transport-protocol / endpoint config change while the
    leg was parked), so the human reads an HONEST reason instead of the false 'exhausted its
    retry window'. When None the retry-exhaustion note is used (the default for the genuine
    backoff-window case)."""
    leg.status = VendorDispatchStatus.DEAD_LETTER
    leg.escalated_at = datetime.utcnow()
    leg.next_attempt_at = None
    leg.last_error = sanitize_error(exc)
    # DEAD_LETTER has no pending delivery -> clear the held package. A dead-lettered leg is a
    # native RETRY_WAIT leg exhausting its backoff (wire-less legs never reach RETRY_WAIT), so the
    # single-sourced helper always clears here -- byte-identical to the historic direct write.
    _clear_or_keep_frozen(leg, VendorDispatchStatus.DEAD_LETTER)
    _note = note or (
        f"external dispatch to vendor {leg.vendor_id} ({leg.target_locale}) "
        f"exhausted its retry window ({leg.attempt_count} attempts); dead-lettered "
        f"and escalated to human review"
    )
    _escalate_run_to_human(
        session, leg, run, note=_note, action="vendor.dispatch_dead_lettered", actor=actor
    )
    log.warning(
        "vendor dispatch leg %s dead-lettered after %s attempts: %s",
        leg.id,
        leg.attempt_count,
        leg.last_error,
    )
    return True


def retry_leg(
    session: Session,
    leg: VendorDispatch,
    *,
    now: datetime | None = None,
    transport: httpx.BaseTransport | None = None,
    actor: str = "scheduler",
) -> str:
    """Re-attempt ONE due RETRY_WAIT leg's outbound dispatch (interop R5). Returns one of
    'dispatched' (succeeded -> normal DISPATCHED flow, the run stays parked awaiting the
    return), 'retry_wait' (failed again, still inside the backoff window), 'dead_letter'
    (window exhausted, a policy refusal appeared, the endpoint changed, or egress is now
    blocked -> escalated to a human), 'closed' (the run finished/vanished/advanced -> leg
    CANCELLED), or 'noop' (not a due RETRY_WAIT leg).

    The exact package is re-sent from the leg's `retry_contract` (re-signed fresh, so a
    rotated secret is honored) — never rebuilt from drifted Segment rows.

    CV-2 (L7): this re-dispatch is DELIBERATELY NOT project-budget-gated. It is the
    re-attempt of an ALREADY-COMMITTED commission (the leg was dispatched earlier, its
    spend already committed/counted) — not a NEW submission — so refusing it at 100%
    would strand committed work rather than prevent new spend. Only NEW vendor dispatches
    (dispatch_external_step) carry the budget gate."""
    now = now or datetime.utcnow()
    if leg is None or leg.status != VendorDispatchStatus.RETRY_WAIT:
        return "noop"
    run = session.get(WorkflowRun, leg.workflow_run_id)
    # HIGH-A: the run must be terminal-safe AND STILL PARKED AT THIS LEG'S STEP. A run that
    # finished, vanished, or ADVANCED past the external step (resumed to a later step / is
    # running) must NOT get its package re-sent -- a real vendor would receive + bill work
    # for a run that moved on. CLOSE the leg (CANCELLED) + drop the held package.
    if run is None or is_run_terminal(run.status) or not _run_paused_at_leg_step(session, run, leg):
        leg.status = VendorDispatchStatus.CANCELLED
        leg.escalated_at = now
        # The run moved on -> the leg is CANCELLED, no delivery pending -> clear the held
        # package. (A RETRY_WAIT leg is native, so this always clears; single-sourced.)
        _clear_or_keep_frozen(leg, VendorDispatchStatus.CANCELLED)
        leg.next_attempt_at = None
        session.add(leg)
        session.commit()
        # Record WHY a held package will never send (reviewer note): the run finished,
        # vanished, or advanced past the external step while the leg waited to retry.
        from app.services import audit

        audit.log(
            actor=actor,
            action="vendor.dispatch_retry_cancelled",
            target=f"dispatch {leg.id} (vendor {leg.vendor_id})",
            detail=f"run {leg.workflow_run_id} no longer parked at this step; "
            "retry package dropped, leg cancelled",
            customer_id=leg.customer_id,
        )
        return "closed"
    vendor = session.get(Vendor, leg.vendor_id)
    contract = dict(leg.retry_contract or {})
    if vendor is None or not contract:
        # The vendor was deleted, or (defensively) the package is missing -- a retry cannot
        # proceed and never will, so dead-letter to a human rather than spin forever.
        return (
            "dead_letter"
            if dead_letter_leg(
                session, leg, run, "vendor or retry package no longer available", actor=actor
            )
            else "noop"
        )
    # HIGH-B: the endpoint host is PINNED at dispatch time (leg.endpoint_host). If the
    # vendor's endpoint was edited while the leg was parked, the content + claim token would
    # ship to a DIFFERENT host than every surface reports -- refuse to send, treat it as a
    # policy change -> DEAD_LETTER + escalate so a person re-decides.
    if _endpoint_host(vendor.endpoint_url) != (leg.endpoint_host or ""):
        return (
            "dead_letter"
            if dead_letter_leg(
                session,
                leg,
                run,
                "vendor endpoint changed while leg parked; refusing to re-send to a new host",
                actor=actor,
            )
            else "noop"
        )
    # F2 (belt-and-braces, DOCUMENTED DECISION): the transport protocol is PINNED at dispatch
    # time (the leg's `protocol` snapshot). A RETRY_WAIT leg is by construction a NATIVE leg
    # (the wireless manual transport never touches the wire, so it never raises a transient
    # failure to park RETRY_WAIT). If the vendor was flipped to a DIFFERENT registered protocol
    # while the leg was parked -- native -> manual -- re-dispatching through the vendor's CURRENT
    # transport would run the manual NO-OP and then flip the leg DISPATCHED: a PHANTOM delivery
    # of a package that never left the process. Rather than silently re-send native content
    # through the changed config, we treat a protocol flip EXACTLY like the HIGH-B endpoint
    # change above -- a deliberate operator config change a person must re-decide -> DEAD_LETTER
    # + escalate. (Pinning the retry to the leg's snapshot transport via `impl=` below would
    # also stop the phantom by keeping the retry NATIVE, but dead-lettering is the more coherent
    # choice: the operator who switched to manual has abandoned the native endpoint, so a person,
    # not an automatic retry, should own what happens to the in-flight leg. A legacy leg with a
    # NULL snapshot now reads leg_protocol == NATIVE -- unambiguous history: `manual` did not
    # exist before the snapshot column, so a pre-migration leg was native by construction. It
    # therefore MISMATCHES a native->manual flip and dead-letters here EXACTLY like a snapshotted
    # native leg, which is the point -- the old NULL-fallback-to-live-protocol read was the very
    # phantom this guard prevents.)
    snap_proto = leg_protocol(leg, vendor)
    if vendor_protocol(vendor) != snap_proto:
        return (
            "dead_letter"
            if dead_letter_leg(
                session,
                leg,
                run,
                f"vendor transport protocol changed while leg parked (dispatched as "
                f"{snap_proto!r}, now {vendor_protocol(vendor)!r}); refusing to re-send through a "
                "different transport",
                actor=actor,
                # #6: an HONEST escalation note (this is a config change a person must adjudicate,
                # NOT a retry-window exhaustion -- the default note would misreport it).
                note=(
                    f"vendor transport protocol changed while the leg was parked "
                    f"(dispatched {snap_proto!r}, vendor now {vendor_protocol(vendor)!r}); "
                    "a person must decide"
                ),
            )
            else "noop"
        )
    # M5: re-run the mock-egress perimeter on the retry (it was only checked at first
    # dispatch). If the provider is no longer live / a key was pulled, re-sending offline
    # placeholder text to a real vendor must be refused -> DEAD_LETTER as policy.
    from app.services.connectors.egress_guard import MockEgressBlocked

    try:
        _proj = None
        if leg.document_id is not None:
            _doc = session.get(Document, leg.document_id)
            _proj = session.get(Project, _doc.project_id) if _doc is not None else None
        assert_dispatch_egress_allowed(
            session, _proj, leg.target_locale, _effective_transport(transport)
        )
    except MockEgressBlocked as exc:
        return "dead_letter" if dead_letter_leg(session, leg, run, exc, actor=actor) else "noop"
    from app.services.connectors.base import ConnectorError

    try:
        # #4 (belt-and-braces, DOCUMENTED — NOT a live second defense): pin the re-dispatch to
        # the LEG's snapshotted transport. This line is UNREACHABLE as a defense against a flip:
        # the protocol-mismatch guard above already dead-lettered any native<->manual change, so
        # snap_proto == vendor_protocol(vendor) by the time we get here and this resolves to the
        # vendor's live transport regardless. It is kept purely as explicit intent -- the retry
        # routes through the transport the leg was DISPATCHED under, never a re-read of the
        # vendor's current config -- so a future edit that weakens the guard cannot silently
        # reintroduce a cross-transport re-dispatch.
        dispatch(vendor, contract, transport=transport, impl=transport_for_protocol(snap_proto))
    except (ConnectorError, VendorPolicyRefusal) as exc:
        # A config change turned the transient failure into a POLICY refusal (endpoint edited
        # to non-https / SSRF, an unregistered protocol, or a non-retryable vendor response).
        # A retry can never clear it -> dead-letter + escalate.
        _touch_vendor_error(session, vendor, exc)
        return "dead_letter" if dead_letter_leg(session, leg, run, exc, actor=actor) else "noop"
    except VendorDispatchError as exc:
        leg.attempt_count = int(leg.attempt_count or 0) + 1
        leg.last_error = sanitize_error(exc)
        backoff = next_backoff(leg.attempt_count)
        _touch_vendor_error(session, vendor, exc)
        if backoff is None:
            return "dead_letter" if dead_letter_leg(session, leg, run, exc, actor=actor) else "noop"
        # M4: honor a Retry-After the vendor sent (429/503), but CAP it at the next backoff
        # step so a hostile/huge value can never strand the leg past its bounded window, and
        # (L-b) FLOOR it at RETRY_AFTER_FLOOR_SECONDS so a `Retry-After: 0` can't skip the
        # backoff into an immediate re-hammer.
        wait = backoff
        ra = getattr(exc, "retry_after", None)
        if ra is not None and ra >= 0:
            # #448: clamp on the NUMBER, not the timedelta. timedelta(seconds=1e20) raises
            # OverflowError (a non-finite hint is already rejected to None by
            # _parse_retry_after) -- and by here attempt_count is already incremented and
            # committed by _touch_vendor_error -- so the exception would strand the leg with a
            # PAST next_attempt_at and re-send it every sweep, bypassing both the bounded
            # backoff and RETRY_AFTER_FLOOR. Bounding ra to the window first folds a huge hint
            # onto the cap instead of crashing; the floor still lifts a tiny value off zero.
            capped = min(ra, backoff.total_seconds())
            wait = max(timedelta(seconds=capped), timedelta(seconds=RETRY_AFTER_FLOOR_SECONDS))
        leg.next_attempt_at = now + wait
        session.add(leg)
        session.commit()
        log.info(
            "vendor dispatch leg %s retry %s failed; next attempt at %s",
            leg.id,
            leg.attempt_count,
            leg.next_attempt_at,
        )
        return "retry_wait"
    # SUCCESS -> normal DISPATCHED flow. The run is already parked PAUSED (a RETRY_WAIT leg
    # parked it exactly like a delivered dispatch), so there is nothing to resume: flip the
    # leg DISPATCHED, drop the held package, clear the error/backoff, and reset the wait
    # window from actual delivery.
    step = session.get(WorkflowStep, leg.step_id) if leg.step_id else None
    # Wave E R3: a quote-gated leg that was awaiting its FIRST successful delivery restores to
    # QUOTED (awaiting a quote + approval), not DISPATCHED -- the package only just reached the
    # vendor, so work has not been authorized. A leg whose quote was ALREADY approved before a
    # (later, hypothetical) transient failure goes back to DISPATCHED.
    leg.status = (
        VendorDispatchStatus.QUOTED
        if (leg.requires_quote and leg.quote_approved_at is None)
        else VendorDispatchStatus.DISPATCHED
    )
    # A SUCCESSFUL retry restores a native RETRY_WAIT leg to DISPATCHED/QUOTED; its re-send
    # package is spent, so clear it. A native leg is never wire-less, so the single-sourced
    # helper clears here even though DISPATCHED/QUOTED are keep-states -- byte-identical to the
    # historic direct write (a wire-less leg never reaches RETRY_WAIT, so it never lands here).
    _clear_or_keep_frozen(leg, leg.status)
    leg.next_attempt_at = None
    leg.last_error = ""  # L1: a delivered leg carries no lingering failure reason
    leg.dispatched_at = now
    if step is not None:
        leg.expires_at = expiry_for(step, now=now)
    session.add(leg)
    session.commit()
    try:
        vendor.status = VendorStatus.CONNECTED
        vendor.last_message = (
            f"dispatched {leg.segment_count} segment(s) to "
            f"{leg.endpoint_host} (after {leg.attempt_count} retr"
            f"{'y' if leg.attempt_count == 1 else 'ies'})"
        )
        session.add(vendor)
        session.commit()
    except Exception:
        log.exception("vendor status touch failed for vendor %s", vendor.id)
    from app.services import audit

    audit.log(
        actor=actor,
        action="vendor.dispatch_retry_ok",
        target=f"dispatch {leg.id} (vendor {leg.vendor_id})",
        detail=f"run {leg.workflow_run_id} {leg.target_locale} "
        f"after {leg.attempt_count} attempt(s)",
        customer_id=leg.customer_id,
    )
    return "dispatched"


def retry_due_legs(
    session: Session,
    *,
    now: datetime | None = None,
    transport: httpx.BaseTransport | None = None,
    actor: str = "scheduler",
) -> dict:
    """Re-attempt every due RETRY_WAIT leg (interop R5). Best-effort per leg — one bad leg
    never aborts the sweep. The scheduler wires the cadence BELOW the global sync kill
    switch (it is network egress). Returns a tally."""
    tally = {
        "retried": 0,
        "dispatched": 0,
        "still_waiting": 0,
        "dead_lettered": 0,
        "closed": 0,
        "failed": 0,
    }
    for leg in due_retry_legs(session, now=now):
        tally["retried"] += 1
        try:
            outcome = retry_leg(session, leg, now=now, transport=transport, actor=actor)
        except Exception:
            tally["failed"] += 1
            log.exception("retry sweep failed for dispatch %s", leg.id)
            continue
        if outcome == "dispatched":
            tally["dispatched"] += 1
        elif outcome == "retry_wait":
            tally["still_waiting"] += 1
        elif outcome == "dead_letter":
            tally["dead_lettered"] += 1
        elif outcome == "closed":
            tally["closed"] += 1
    return tally
