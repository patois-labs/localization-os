"""api_discovery: shared leaf helpers (layered pure move)

Documented in: docs/architecture/surfaces-and-parity.md
"""

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from sqlmodel import Session

from app import config
from app.db import engine
from app.web import deps

router = APIRouter()


def _app_version() -> str:
    from app.release import app_version

    return app_version()


_FAILURE_KEYS = ("issue_key", "page_id", "record_id", "article_id", "locale", "title", "error")


_FAILURE_IDS = ("issue_key", "page_id", "record_id", "article_id")


_FAILURE_CAP = 500


_FAILURE_COUNT_CAP = 1000


# Keys a failure entry may carry, per connector kind (see store.*_translate).
# `article_id` covers every article-style flow (zendesk/document360/freshdesk/zoho/
# intercom/salesforce/notion/helpscout/strapi/contentful) — without it _clean_failures
# dropped every such item and the API retry 400'd (review round-3 finding 4).
# Any of these identifies a retryable item — an entry with none is unusable.
# per-value length cap, so a hostile payload can't bloat the row
# Entry-COUNT cap (the per-value cap above bounds each field, not the list length). The
# only_pairs retry bypass now reaches all 13 flow kinds, so an uncapped list would fan a
# hostile caller's request into an unbounded materialize/write loop. A real retry targets a
# prior run's failures/incomplete/blocked set, which the WriteBudget already bounds well
# below this, so 1000 is a generous ceiling that only trips on abuse.
def _clean_failures(raw) -> list[dict]:
    """Sanitize caller-supplied failures before they seed a retry job.

    Must be a list of dicts; each entry keeps only the expected string keys,
    every value coerced to str and length-capped; entries with no usable id
    (issue_key/page_id/record_id) are dropped. Returns [] for anything malformed
    so the caller can reject with a 400 rather than enqueue junk."""
    if not isinstance(raw, list):
        return []
    out: list[dict] = []
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        clean = {}
        for k in _FAILURE_KEYS:
            if k in entry and entry[k] is not None:
                clean[k] = str(entry[k])[:_FAILURE_CAP]
        if any(clean.get(k, "").strip() for k in _FAILURE_IDS):
            out.append(clean)
    return out


def checked_retry_failures(raw):
    """The ONE retry-failures gate shared by POST /api/connectors/{cid}/retry and the generic
    POST /api/jobs/run connector_translate kind (#370). Entry-count cap BEFORE the O(n)
    sanitize loop (a distinct 400 naming the cap), then `_clean_failures`; an empty or
    malformed result is a 400. Returns (clean_list, None) or (None, JSONResponse)."""
    if isinstance(raw, list) and len(raw) > _FAILURE_COUNT_CAP:
        return None, JSONResponse(
            {"ok": False, "error": f"too many failed items to retry (max {_FAILURE_COUNT_CAP})"},
            status_code=400,
        )
    clean = _clean_failures(raw)
    if not clean:
        return None, JSONResponse(
            {"ok": False, "error": "no failed items to retry"}, status_code=400
        )
    return clean, None


@router.post("/webhooks/vendor-returns")
async def vendor_return_callback(request: Request):
    """SIGNED VENDOR RETURN CALLBACK — the inbound half of the dispatch-and-wait step
    (PROPOSAL-external-processing-step Wave B, decision 7).

    UNAUTHENTICATED AT THE SESSION/API LAYER BY DESIGN, and therefore SIGNED: a
    translation vendor is not a user of this install and cannot carry our session cookie
    or an API key. It lives under `/webhooks/` precisely because that prefix's documented
    invariant (app/main.py) is "every route here MUST verify a per-caller signature
    before doing any work" — this one verifies the per-VENDOR `signing_secret`, the same
    shared secret Wave A signs its outbound dispatch with.

    THE ORDER IS THE SECURITY PROPERTY:
      1. body-size cap FIRST, on the streamed request, before a single byte is retained
         past the cap — an unbounded read here would be a memory-exhaustion vector
         reachable with no credentials at all (the sibling connector intake's lesson);
      2. claim token off the `X-LocStudio-Claim` HEADER, which selects the leg and hence
         the signing key while the body is still opaque bytes;
      3. HMAC-SHA256 over those RAW bytes, constant-time;
      4. only then is anything deserialized. So a malformed body with a bad signature
         fails as a SIGNATURE error and never reaches the JSON parser.

    Every authentication failure — unknown token, missing/short/wrong signature, vendor
    with no secret — answers with the SAME generic 403, so an unauthenticated prober
    cannot use this endpoint to learn which claim tokens exist. The LOUD, specific
    refusals (cancelled leg, late return, unknown segment id) are only reachable AFTER
    the signature verified, which is exactly the population entitled to them.

    Tenant scoping is structural: the claim token IS the leg, and the leg names the one
    document + locale + customer its content may reach."""
    # ffadmin Wave 3 (MEDIUM-4): layer-2 kill switch, BEFORE any body read or signature
    # work -- a JSON 404 byte-identical to the middleware's (deps.feature_block routes
    # /webhooks/ errors to JSON), so a switched-off vendor webhook is non-enumerable at
    # both layers.
    blocked = deps.feature_block(request, "vendors")
    if blocked:
        return blocked
    from app.kernel import vendor_return

    cap = config.WEBHOOK_MAX_BODY_BYTES
    declared = request.headers.get("content-length")
    if declared is not None:
        try:
            if int(declared) > cap:
                return JSONResponse(
                    {"ok": False, "error": f"Return payload too large (limit {cap} bytes)."},
                    status_code=413,
                )
        except ValueError:
            pass
    chunks, total = [], 0
    async for piece in request.stream():
        total += len(piece)
        if total > cap:
            return JSONResponse(
                {"ok": False, "error": f"Return payload too large (limit {cap} bytes)."},
                status_code=413,
            )
        chunks.append(piece)
    body = b"".join(chunks)
    headers = {k: v for k, v in request.headers.items()}  # noqa: C416
    with Session(engine) as s:
        try:
            leg = vendor_return.authenticate_callback(s, headers, body)
        except vendor_return.ReturnAuthError as exc:
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=403)
        try:
            # Vendor Wave D: an XLIFF 1.2 return body is transport-decoded into the same
            # contract dict a JSON body yields (and stamped with THIS authenticated leg's
            # claim token); a JSON body passes through untouched. A malformed XLIFF raises
            # ReturnContractError -> the 400 below (one loud refusal, nothing admitted).
            decoded = vendor_return.decode_return_body(body, leg=leg)
            summary = vendor_return.admit_return(
                s, leg, decoded, actor="vendor-callback", transport="webhook", resume="async"
            )
        except vendor_return.ReturnStateError as exc:
            # L2: the vendor is authenticated but UNTRUSTED, so the webhook response must not
            # leak internal ids (run/document/dispatch/customer). Log the specific reason;
            # answer the vendor with a generic-but-honest category.
            import logging as _logging

            _logging.getLogger("loc_tms.api").warning(
                "vendor return webhook dropped (leg %s): %s", getattr(leg, "id", None), exc
            )
            return JSONResponse(
                {
                    "ok": False,
                    "applied": False,
                    "error": "This return could not be applied: the dispatch is no longer "
                    "open or its run has moved on.",
                },
                status_code=409,
            )
        except vendor_return.ReturnContractError as exc:
            import logging as _logging

            _logging.getLogger("loc_tms.api").warning(
                "vendor return webhook rejected (leg %s): %s", getattr(leg, "id", None), exc
            )
            return JSONResponse(
                {
                    "ok": False,
                    "applied": False,
                    "error": "The return payload was rejected by contract validation.",
                },
                status_code=400,
            )
    return JSONResponse(summary, status_code=200)


@router.post("/webhooks/vendor-quotes")
async def vendor_quote_callback(request: Request):
    """SIGNED VENDOR QUOTE CALLBACK — the wire path for the Wave E R3 quote gate (over-the-
    wire quote intake). A quote-gated vendor SUBMITS its quote for a parked leg here before
    starting work.

    IDENTICAL security posture to /webhooks/vendor-returns and for the same reasons: a vendor
    is not a session user, so this is UNAUTHENTICATED at the session/API layer and therefore
    SIGNED. The order is the security property — body-cap FIRST on the stream, then the claim
    token off the `X-LocStudio-Claim` HEADER selects the leg + signing key while the body is
    still opaque bytes, then HMAC-SHA256 over the raw bytes (vendor_return.authenticate_
    callback, the SAME gate), and only THEN is the quote message parsed. Every auth failure is
    the same generic 403 (no claim-token existence oracle). Recording a quote NEVER moves money
    and NEVER advances the leg (approval is the separate, admin-gated spend decision) -- so a
    signed quote message is a low-privilege write bounded to one leg."""
    # ffadmin Wave 3 (MEDIUM-4): layer-2 kill switch, BEFORE any body read or signature
    # work -- a JSON 404 byte-identical to the middleware's.
    blocked = deps.feature_block(request, "vendors")
    if blocked:
        return blocked
    from app.kernel import vendor_quote, vendor_return

    cap = config.WEBHOOK_MAX_BODY_BYTES
    declared = request.headers.get("content-length")
    if declared is not None:
        try:
            if int(declared) > cap:
                return JSONResponse(
                    {"ok": False, "error": f"Quote payload too large (limit {cap} bytes)."},
                    status_code=413,
                )
        except ValueError:
            pass
    chunks, total = [], 0
    async for piece in request.stream():
        total += len(piece)
        if total > cap:
            return JSONResponse(
                {"ok": False, "error": f"Quote payload too large (limit {cap} bytes)."},
                status_code=413,
            )
        chunks.append(piece)
    body = b"".join(chunks)
    headers = {k: v for k, v in request.headers.items()}  # noqa: C416
    with Session(engine) as s:
        try:
            leg = vendor_return.authenticate_callback(s, headers, body)
        except vendor_return.ReturnAuthError as exc:
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=403)
        try:
            summary = vendor_quote.apply_quote_message(s, leg, body)
        except vendor_quote.QuoteStateError as exc:
            import logging as _logging

            _logging.getLogger("loc_tms.api").warning(
                "vendor quote webhook dropped (leg %s): %s", getattr(leg, "id", None), exc
            )
            return JSONResponse(
                {
                    "ok": False,
                    "applied": False,
                    "error": "This quote could not be recorded: the dispatch is not awaiting a "
                    "quote.",
                },
                status_code=409,
            )
        except vendor_quote.QuoteContractError as exc:
            import logging as _logging

            _logging.getLogger("loc_tms.api").warning(
                "vendor quote webhook rejected (leg %s): %s", getattr(leg, "id", None), exc
            )
            return JSONResponse(
                {
                    "ok": False,
                    "applied": False,
                    "error": "The quote message was rejected by contract validation.",
                },
                status_code=400,
            )
    return JSONResponse(summary, status_code=200)
