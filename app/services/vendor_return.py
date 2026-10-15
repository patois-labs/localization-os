"""The guarded RETURN path for the EXTERNAL / vendor dispatch-and-wait step
(PROPOSAL-external-processing-step §3.6/§3.7/§9; combined build Wave B).

Transitional compatibility shim for one release (added 2026-09-15, remove the re-export block after
the next tagged release): `__all__` re-exports log, GENERIC_AUTH_MESSAGE, _COMMENTS_PER_SEGMENT_MAX,
_COMMENT_BODY_MAX, _COMMENT_LABEL_MAX, _COMMENT_REF_MAX, _queries_enabled, _validate_comments,
authenticate_callback, check_contract, admit_return, _admit_unit_comments, _admit_units,
admit_manual, _TEST_RESOLVER, set_test_resolver, _resolve_host_ips, _guard_poll_target from
app.services.vendor_return_hub.

Transitional compatibility shim for one release (added 2026-09-14, remove the re-export block after
the next tagged release): `__all__` re-exports log, SIGNATURE_HEADER, CLAIM_HEADER,
PROVENANCE_VENDOR, PROVENANCE_EXTERNAL, POLL_MIN_INTERVAL_MINUTES, POLL_MAX_PER_SWEEP,
POLL_TIMEOUT_SECONDS, VendorReturnError, ReturnAuthError, ReturnStateError, ReturnContractError,
_norm_id, _ct_eq, _base_lang, _finite_cost, leg_for_claim, signing_secret_for, verify_signature,
parse_payload, decode_return_body, _ledger, dispatched_ids, returned_ids, outstanding_ids,
omitted_ids, assert_leg_admitting, _aligned_slots, _provenance, is_source_echo, _glossary_terms,
_segment_index, _notify_query_posts, _resume_leg, _LAST_POLL, VendorPollError, poll_url_for,
open_legs, pollable_legs, redrive_returned_leg, leg_to_dict from app.services.vendor_return_common,
app.services.vendor_return_part2.

Wave A sent content out. This is the half that lets it back IN -- and the whole point
of the feature: "anyone can POST a payload; the value is that returning content
re-enters through the exact same guarded write seam as a provider reply" (§1).

THREE TRANSPORTS, ONE VALIDATION PIPELINE (decision 7). The transport decides only how
bytes arrive; it NEVER decides how they are judged:

  * signed webhook callback  -- app/web/api.py POST /webhooks/vendor-returns. HMAC-SHA256
    over the RAW request body with the vendor's `signing_secret`, mirroring the
    `sha256=<hex>` signature Wave A's dispatch() already SENDS outbound (one shared
    secret, both directions). Unauthenticated at the session/API layer BY DESIGN -- a
    vendor is not a user and cannot carry our cookie or API key -- and therefore signed.
  * polling                  -- `poll_leg` / `poll_due_legs`: an OUTBOUND GET to the
    vendor's declared `poll_url`, through the SAME SSRF guard as dispatch
    (connectors.base._validate_target) plus the https-only allowlist, on the injectable
    httpx transport seam (vendor_dispatch.set_test_transport) so tests never touch a wire.
  * manual re-upload         -- the operator pastes/uploads the return contract JSON on
    the vendor panel, `vendor return` in the CLI, or POST /api/vendors/returns. The
    always-available fallback for a system that never answers.

THE GATE ORDER (each one is a real gate; none may be reordered):

  0. TRANSPORT AUTHENTICATION. For the webhook: look the leg up by the claim token that
     rides in the `X-LocStudio-Claim` HEADER -- this is a KEY SELECTION step, not a
     trust decision, and it deliberately happens with the body still an opaque byte
     string so that NOTHING is deserialized before the signature is checked. Then verify
     the HMAC over those raw bytes with hmac.compare_digest. Only after that is the body
     parsed. So a malformed body with a bad signature fails as a SIGNATURE error, never
     a parse error, and an attacker cannot use our JSON parser at all.
  1. LEG STATE. Only a still-DISPATCHED leg whose run exists and is not terminal may
     admit anything. CANCELLED / ESCALATED / already-RETURNED, or a run that completed,
     failed or moved on -> the return is DROPPED, LOUDLY (matched-token-or-drop, §3.7):
     aborting a run kills its claim token's admitting power.
  2. CONTRACT ID ACCOUNTING -- THE FIRST CONTENT GATE, before any guard reads a single
     character (§3.2 "the schema check is the first gate"). Every dispatched id must come
     back EXACTLY ONCE, in the right locale, with no extras and no unknown ids, CUMULATIVE
     across partial returns (the `contract_ids` ledger). A malformed return fails the
     WHOLE return -- never a partial silent admit of a bad payload.
  3. CONTENT ADMISSION through `quality.commit_mt_output` and NOTHING ELSE, with every
     guard lane on (refusal, lang_guard, tag/placeholder integrity, glossary/DNT, number,
     length). There is no fast path and no per-caller "trust me": an external engine's
     reply is UNTRUSTED INPUT. This is the `translate.py:774` anti-pattern the proposal
     names explicitly (§3.6) and refuses to copy.
  4. SOURCE-ECHO IS A NO-OP (decision 4, the K1 law). A returned target equal to the
     source for a non-English target is "not translated", never an overwrite: the prior
     stored target survives untouched and the segment is flagged needs_review. Same for
     a blank target. This is the `doc set-paragraph` data-loss class made impossible by
     construction on this path.

PARTIAL RETURNS are first-class per leg (§3.7): a valid subset admits what it covers and
the leg stays PAUSED until its full dispatched id set is accounted; the COMPLETING return
flips the leg RETURNED, stamps `returned_at`, reconciles `actual_cost_usd` (decision 5c)
and resumes the run through the EXISTING human_review resume machinery -- exactly once,
because only one return can make the guarded DISPATCHED -> RETURNED transition.

TENANT ISOLATION is structural, not a filter: the claim token IS the leg, the leg carries
its document/locale/customer/org, and every admitted segment must belong to THAT document
and THAT locale. A token minted for one customer's run has no vocabulary in which to name
another customer's segment.

Documented in: docs/architecture/vendor-outsourcing.md
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import math
from datetime import datetime, timedelta

import httpx
from sqlmodel import Session, select

from app.models import (
    Document,
    Project,
    RunStatus,
    TransMode,
    Vendor,
    VendorDispatch,
    VendorDispatchStatus,
    WorkflowRun,
)
from app.services import egress, postedit, quality, vendor_dispatch, vendor_xliff, vendors
from app.services.vendor_return_common import (
    _LAST_POLL,
    _LAST_POLL_LEG,
    CLAIM_HEADER,
    POLL_MAX_PER_SWEEP,
    POLL_MIN_INTERVAL_MINUTES,
    POLL_TIMEOUT_SECONDS,
    PROVENANCE_EXTERNAL,
    PROVENANCE_VENDOR,
    SIGNATURE_HEADER,
    ReturnAuthError,
    ReturnContractError,
    ReturnStateError,
    VendorPollError,
    VendorReturnError,
    _aligned_slots,
    _base_lang,
    _ct_eq,
    _finite_cost,
    _glossary_terms,
    _ledger,
    _norm_id,
    _notify_query_posts,
    _provenance,
    _resume_leg,
    _segment_index,
    assert_leg_admitting,
    decode_return_body,
    dispatched_ids,
    is_source_echo,
    leg_for_claim,
    omitted_ids,
    open_legs,
    outstanding_ids,
    parse_payload,
    poll_url_for,
    pollable_legs,
    returned_ids,
    signing_secret_for,
    verify_signature,
)
from app.services.vendor_return_hub import (
    _COMMENT_BODY_MAX,
    _COMMENT_LABEL_MAX,
    _COMMENT_REF_MAX,
    _COMMENTS_PER_SEGMENT_MAX,
    _TEST_RESOLVER,
    GENERIC_AUTH_MESSAGE,
    _admit_unit_comments,
    _admit_units,
    _guard_poll_target,
    _queries_enabled,
    _resolve_host_ips,
    _validate_comments,
    admit_manual,
    admit_return,
    authenticate_callback,
    check_contract,
    set_test_resolver,
)
from app.services.vendor_return_part2 import leg_to_dict, redrive_returned_leg

__all__ = [
    "CLAIM_HEADER",
    "GENERIC_AUTH_MESSAGE",
    "POLL_MAX_PER_SWEEP",
    "POLL_MIN_INTERVAL_MINUTES",
    "POLL_TIMEOUT_SECONDS",
    "PROVENANCE_EXTERNAL",
    "PROVENANCE_VENDOR",
    "SIGNATURE_HEADER",
    "_COMMENTS_PER_SEGMENT_MAX",
    "_COMMENT_BODY_MAX",
    "_COMMENT_LABEL_MAX",
    "_COMMENT_REF_MAX",
    "_LAST_POLL",
    "_LAST_POLL_LEG",
    "_TEST_RESOLVER",
    "Document",
    "Project",
    "ReturnAuthError",
    "ReturnContractError",
    "ReturnStateError",
    "RunStatus",
    "TransMode",
    "VendorDispatchStatus",
    "VendorPollError",
    "VendorReturnError",
    "WorkflowRun",
    "_admit_unit_comments",
    "_admit_units",
    "_aligned_slots",
    "_base_lang",
    "_ct_eq",
    "_finite_cost",
    "_glossary_terms",
    "_guard_poll_target",
    "_ledger",
    "_norm_id",
    "_notify_query_posts",
    "_provenance",
    "_queries_enabled",
    "_resolve_host_ips",
    "_resume_leg",
    "_segment_index",
    "_validate_comments",
    "admit_manual",
    "admit_return",
    "assert_leg_admitting",
    "authenticate_callback",
    "check_contract",
    "decode_return_body",
    "dispatched_ids",
    "hashlib",
    "hmac",
    "is_source_echo",
    "json",
    "leg_for_claim",
    "leg_to_dict",
    "math",
    "omitted_ids",
    "open_legs",
    "outstanding_ids",
    "parse_payload",
    "poll_due_legs",
    "poll_leg",
    "poll_url_for",
    "pollable_legs",
    "postedit",
    "quality",
    "redrive_returned_leg",
    "returned_ids",
    "select",
    "set_test_resolver",
    "signing_secret_for",
    "timedelta",
    "vendor_xliff",
    "verify_signature",
]


log = logging.getLogger("loc_tms.vendor_return")


def poll_leg(
    session: Session,
    leg: VendorDispatch,
    *,
    transport: httpx.BaseTransport | None = None,
    actor: str = "system",
    resume: str = "async",
    timeout: float = POLL_TIMEOUT_SECONDS,
) -> dict | None:
    """Ask the vendor whether THIS leg's return is ready; admit it if so.

    Egress posture, stated explicitly because it differs from dispatch: a poll sends NO
    CONTENT -- only the claim token the vendor already holds -- so the mock-egress
    perimeter (`vendor_dispatch.assert_dispatch_egress_allowed`, which exists to stop
    deterministic MockProvider placeholder text reaching a real vendor) has nothing to
    protect here and is deliberately NOT applied. The SSRF guard very much does apply: a
    poll URL is a caller-influenced outbound target exactly like a dispatch URL.

    Returns the admit summary, or None when the vendor says "nothing ready" (204, 404, or
    an empty body). Raises VendorPollError on a transport/config failure."""
    from app import config

    vendor = session.get(Vendor, leg.vendor_id)
    if vendor is None:
        raise VendorPollError(f"dispatch {leg.id}: its vendor no longer exists.")
    # Dispatch-by-protocol seam (interop R1): a poll is a TRANSPORT operation, so it routes
    # through the vendor's protocol. `native` (the only registered protocol) pulls our own
    # signed JSON contract from the vendor's poll_url, exactly as below. A future adapter
    # protocol would fetch via the vendor's client API here -- but STILL admit through the
    # SAME admit_return -> commit_mt_output seam (the R1 invariant; no protocol gets its own
    # admission path). Until an adapter exists, a non-native protocol has no poll transport.
    # F2: key off the LEG's protocol SNAPSHOT, not the vendor's live protocol. A leg dispatched
    # `manual` stays non-pollable even if the vendor was later flipped to `native` (its content
    # was never put on the wire, so there is nothing to poll for and forwarding the bearer +
    # claim token would leak credentials for a package never delivered); a leg dispatched
    # `native` stays pollable even if the vendor was flipped to `manual`.
    proto = vendor_dispatch.leg_protocol(leg, vendor)
    if proto != vendor_dispatch.NATIVE_PROTOCOL:
        raise VendorPollError(
            f"dispatch {leg.id} was dispatched under transport protocol {proto!r}, which "
            "has no poll implementation (a manual/file-exchange leg is returned by upload; "
            "per-LSP adapters are a later wave)."
        )
    raw_url = poll_url_for(vendor)
    if not raw_url:
        raise VendorPollError(
            f"vendor {vendor.id} ({vendor.name}) has no `poll_url` in its config, so "
            "polling is off for it. Set config.poll_url to enable the poll transport."
        )
    url = _guard_poll_target(raw_url)  # F-MED-1: resolve-and-validate SSRF
    # L1: the poll forwards the vendor's bearer auth_token + our claim token, so its host is
    # PINNED to the vendor's dispatch endpoint host. Otherwise a config-only `poll_url` edit
    # (no secret touched) could redirect a live credential to an arbitrary public host.
    # Gate round 1 LOW-1 (#265): pinned to the endpoint's ORIGIN (scheme, host, effective
    # port), not the hostname, so another port on the same host is refused too.
    from app.services.connectors.base import ConnectorError
    from app.services.credential_origin import endpoint_origin

    ep_origin = endpoint_origin(vendor.endpoint_url or "")
    poll_origin = endpoint_origin(url)
    # Gate round 2 MEDIUM-1: fail CLOSED. An endpoint or poll_url whose origin cannot be
    # read is refused, never waved through.
    if not ep_origin or poll_origin != ep_origin:
        raise ConnectorError(
            f"vendor poll_url {poll_origin!r} must match the vendor's endpoint host "
            f"{ep_origin!r} (scheme, host and port); a poll may not send credentials to a "
            "different server."
        )
    secret = vendors.secret_dict(vendor)
    headers = {
        CLAIM_HEADER: leg.claim_token or "",
        "x-locstudio-contract-version": vendor_dispatch.CONTRACT_VERSION,
        "accept": "application/json",
    }
    if secret.get("auth_token"):
        headers["authorization"] = f"Bearer {secret['auth_token']}"
    tport = vendor_dispatch._effective_transport(transport)
    # Stamp the throttle BEFORE the request, not after: an unreachable vendor is exactly
    # the case that must not be retried every 30-second scheduler tick.
    _LAST_POLL[leg.vendor_id] = datetime.utcnow()  # MEDIUM-5: throttle per VENDOR, not leg
    _LAST_POLL_LEG[leg.id] = _LAST_POLL[leg.vendor_id]  # #447: rotate a vendor's legs
    # F-MED-2: the response body is CAPPED as it streams, mirroring the webhook intake --
    # `resp.content` would buffer an unbounded body from a compromised/hostile vendor into
    # memory before a single byte was validated. Over the cap -> discarded, never processed.
    cap = config.WEBHOOK_MAX_BODY_BYTES
    try:
        with httpx.Client(  # noqa: SIM117
            timeout=timeout,
            follow_redirects=False,
            **egress.guarded_transport_kwargs(
                "vendor_return", transport=tport, error_type=ConnectorError
            ),
        ) as client:
            with client.stream("GET", url, headers=headers) as resp:
                status = resp.status_code
                sig = resp.headers.get(SIGNATURE_HEADER)
                if status in (204, 404):
                    return None  # nothing ready yet
                if not (200 <= status < 300):
                    raise VendorPollError(f"poll of dispatch {leg.id} returned HTTP {status}.")
                chunks, total = [], 0
                for piece in resp.iter_bytes():
                    total += len(piece)
                    if total > cap:
                        raise VendorPollError(
                            f"poll of dispatch {leg.id}: response body exceeds the "
                            f"{cap}-byte cap; discarded before processing."
                        )
                    chunks.append(piece)
                body = b"".join(chunks)
    except httpx.HTTPError as exc:
        raise VendorPollError(
            f"poll of dispatch {leg.id} failed: {type(exc).__name__}: {exc}"
        ) from exc
    if not body.strip():
        return None
    # A pull we initiated is already authenticated by the SSRF-guarded https target plus
    # our bearer; a signature is therefore OPTIONAL here. But if the vendor DOES sign the
    # response, a mismatch is fatal -- accepting a body whose signature we checked and
    # failed would be worse than never checking.
    if sig:  # noqa: SIM102
        if not verify_signature(signing_secret_for(vendor), body, sig):
            raise ReturnAuthError(
                f"poll of dispatch {leg.id}: the vendor signed its response and the "
                "signature does not verify; the body is discarded."
            )
    return admit_return(session, leg, body, actor=actor, transport="poll", resume=resume)


def poll_due_legs(
    session: Session,
    *,
    vendor_id=None,
    org_id=None,
    transport: httpx.BaseTransport | None = None,
    now: datetime | None = None,
    throttle: bool = True,
    actor: str = "scheduler",
    resume: str = "async",
) -> dict:
    """Poll every due leg. Best-effort per leg -- one unreachable vendor never aborts the
    sweep -- and every failure is logged, never raised. Returns a tally."""
    polled = admitted = failed = 0
    results = []
    for leg in pollable_legs(
        session, vendor_id=vendor_id, org_id=org_id, now=now, throttle=throttle
    ):
        polled += 1
        try:
            res = poll_leg(session, leg, transport=transport, actor=actor, resume=resume)
        except Exception as exc:
            failed += 1
            log.warning("vendor poll failed for dispatch %s: %s", leg.id, exc)
            continue
        if res is not None:
            admitted += 1
            results.append(res)
    return {"polled": polled, "admitted": admitted, "failed": failed, "results": results}
