"""/api routes: vendors

Documented in: docs/architecture/surfaces-and-parity.md
"""

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from sqlmodel import Session, select

from app.db import engine
from app.kernel import (
    vendors,
)
from app.web import deps
from app.web.api_cli_common import (
    _NOT_FOUND,
    _admin_block,
    _gate,
    _json,
    _list_truncate,
    _list_window,
    _r3_gate,
)

router = APIRouter()


# ============================================================================
# Vendors — first-class external translation/review vendors (PROPOSAL-external-
# processing-step §9 Wave A). Thin JSON shells over app/services/vendors.py; every
# row is org-scoped through _gate (vendors.visible / list_vendors take org_id). RBAC
# mirrors the web /vendors panel: EVERY route here is admin-only (_admin_block), reads
# included (H1 below). The Fernet signing secret is NEVER echoed - to_dict
# omits the value and carries only a has_secret presence flag, so no response body
# on this surface can leak a credential.
# ============================================================================
@router.get("/api/vendors")
def api_vendors_list(request: Request):
    # H1: vendors are an OPERATOR-ADMIN surface (they hold dispatch endpoints + signing
    # secrets whose theft repoints every linked customer's content to an attacker host).
    # The /api gate MIRRORS the web /vendors panel exactly -- admin-only, INCLUDING the
    # reads: the roster (endpoints/hosts/linked customers) is itself an intel oracle a
    # translator must not enumerate.
    blocked = deps.feature_api_block("vendors")
    if blocked:
        return blocked
    ident, org_id, blocked = _gate(request)
    if blocked:
        return blocked
    blocked = _admin_block(ident)
    if blocked:
        return blocked
    include_archived = str(request.query_params.get("include_archived", "")).lower() in (
        "1",
        "true",
        "yes",
    )
    with Session(engine) as s:
        rows = vendors.list_vendors(s, org_id=org_id, include_archived=include_archived)
        payload = [vendors.to_dict(s, v) for v in rows]
    return JSONResponse({"ok": True, "vendors": payload})


@router.post("/api/vendors")
async def api_vendors_create(request: Request):
    """Create a vendor (only `name` is required). ADMIN-only, mirroring the web /vendors
    panel (H1: the whole vendor surface is operator-admin — endpoints + signing secrets).
    The connection secret and rate card are filled in later via the update / rate-card
    routes."""
    blocked = deps.feature_api_block("vendors")
    if blocked:
        return blocked
    ident, org_id, blocked = _gate(request)
    if blocked:
        return blocked
    blocked = _admin_block(ident)
    if blocked:
        return blocked
    body = await _json(request)
    cfg = body.get("config")
    if cfg is not None and not isinstance(cfg, dict):
        return JSONResponse({"ok": False, "error": "config must be an object."}, status_code=400)
    with Session(engine) as s:
        try:
            v = vendors.create(
                s,
                str(body.get("name") or ""),
                description=str(body.get("description") or ""),
                endpoint_url=str(body.get("endpoint_url") or ""),
                config_json=cfg if isinstance(cfg, dict) else None,
                protocol=body.get("protocol"),  # interop R1 (None -> 'native')
                org_id=org_id,
                created_by=ident["actor"],
                actor=ident["actor"],
                ip=deps._ip(request),
            )
        except vendors.VendorError as exc:
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
        out = vendors.to_dict(s, v)
    return JSONResponse({"ok": True, "vendor": out})


# ============================================================================
# Vendor DISPATCH LEGS + the guarded RETURN (PROPOSAL-external-processing-step Wave B).
# ADMIN-only like the rest of the vendor surface (H1): a leg names the run, document and
# locale a vendor is holding, and the manual-return verb ADMITS CONTENT into storage.
# Neither is agent-actionable — see the MCP registry, where the return route is in
# NEVER_COVER for the same reason the human-review resume is.
# ============================================================================
@router.get("/api/vendors/dispatches")
def api_vendor_dispatches(request: Request):
    """List dispatch legs (open by default; `?status=all|dispatched|returned|...`,
    `?vendor_id=`). Carries the endpoint HOST only and never the claim token — the token
    is a bearer credential for admitting content, so no read surface echoes it. BOUNDED
    (AU-7): optional `limit` (default and hard cap 1000) + `offset`; the response carries
    `truncated`, `limit` and `offset`."""
    blocked = deps.feature_api_block("vendors")
    if blocked:
        return blocked
    ident, org_id, blocked = _gate(request)
    if blocked:
        return blocked
    blocked = _admin_block(ident)
    if blocked:
        return blocked
    from app.kernel import vendor_return
    from app.models import VendorDispatch

    want = str(request.query_params.get("status", "") or "").strip().lower()
    raw_vid = request.query_params.get("vendor_id")
    try:
        vid = int(raw_vid) if raw_vid not in (None, "") else None
    except (TypeError, ValueError):
        return JSONResponse(
            {"ok": False, "error": "vendor_id must be an integer."}, status_code=400
        )
    # AU-7: bound the leg list (was unbounded). The explicit-status branch pushes
    # limit/offset into SQL; the default `open_legs` branch (a service list) is sliced
    # in memory -- both surface a `truncated` flag rather than a silent cutoff.
    limit, offset, err = _list_window(
        request.query_params.get("limit"), request.query_params.get("offset")
    )
    if err:
        return err
    with Session(engine) as s:
        if want in ("", "open", "dispatched"):
            legs = vendor_return.open_legs(s, vendor_id=vid, org_id=org_id)
            rows, truncated = _list_truncate(legs[offset : offset + limit + 1], limit)
        else:
            q = select(VendorDispatch)
            if org_id is not None:
                q = q.where(VendorDispatch.org_id == org_id)
            if vid is not None:
                q = q.where(VendorDispatch.vendor_id == vid)
            if want != "all":
                q = q.where(VendorDispatch.status == want)
            fetched = list(
                s.exec(q.order_by(VendorDispatch.id.desc()).offset(offset).limit(limit + 1)).all()
            )
            rows, truncated = _list_truncate(fetched, limit)
        payload = [vendor_return.leg_to_dict(s, leg) for leg in rows]
    return JSONResponse(
        {
            "ok": True,
            "dispatches": payload,
            "truncated": truncated,
            "limit": limit,
            "offset": offset,
        }
    )


@router.get("/api/vendors/dispatches/{did}")
def api_vendor_dispatch_detail(did: int, request: Request):
    """One dispatch leg's full detail + its TM-leverage band breakdown (interop R6). This
    is the shareable analysis artifact behind the Human-spend number: leg_to_dict (host
    only, never the claim token / secret / full URL) PLUS the persisted `estimate` band
    breakdown and a render-ready `band_rows` table. READ-ONLY and ADMIN-only like the
    dispatches list (H1) — NOT the R3 triple gate (that is only for secret-set). Org-scoped:
    a leg in another org is an identical 404 (never enumerable)."""
    blocked = deps.feature_api_block("vendors")
    if blocked:
        return blocked
    ident, org_id, blocked = _gate(request)
    if blocked:
        return blocked
    blocked = _admin_block(ident)
    if blocked:
        return blocked
    from app.kernel import vendor_cost, vendor_return
    from app.models import VendorDispatch

    with Session(engine) as s:
        leg = s.get(VendorDispatch, did)
        # Cross-tenant wall: a scoped caller (org_id set) may only see its own org's leg.
        if leg is None or (org_id is not None and leg.org_id != org_id):
            return JSONResponse(_NOT_FOUND, status_code=404)
        out = vendor_return.leg_to_dict(s, leg)
        out["estimate"] = leg.estimate or {}
        out["band_rows"] = vendor_cost.estimate_band_rows(leg.estimate)
    return JSONResponse({"ok": True, "dispatch": out})


@router.get("/api/vendors/dispatches/{did}/xliff")
def api_vendor_dispatch_xliff(did: int, request: Request):
    """Download the XLIFF 1.2 rendition of one dispatch leg (Vendor Wave D) — the operator
    hands the .xlf to a CAT tool (OmegaT / Weblate / Trados). It is a SERIALIZATION of the
    same segments the JSON contract carried (source + prefilled target + provenance-driven
    state/lock markers); the signed JSON contract on the wire stays canonical. READ-ONLY,
    ADMIN-only, org-scoped (a foreign-org leg is an identical 404), and — like the leg-detail
    read — it never carries the claim token or any secret. An operator-download surface, so
    it is EXEMPT from MCP tool coverage (no agent task attaches to a file download in v1)."""
    blocked = deps.feature_api_block("vendors")
    if blocked:
        return blocked
    ident, org_id, blocked = _gate(request)
    if blocked:
        return blocked
    blocked = _admin_block(ident)
    if blocked:
        return blocked
    import logging as _logging

    from fastapi.responses import Response

    from app.kernel import vendor_xliff
    from app.models import VendorDispatch

    with Session(engine) as s:
        leg = s.get(VendorDispatch, did)
        if leg is None or (org_id is not None and leg.org_id != org_id):
            return JSONResponse(_NOT_FOUND, status_code=404)
        try:
            data = vendor_xliff.render_leg_xliff(s, leg)
        except vendor_xliff.XliffRenderError as exc:
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=409)
        except Exception:
            _logging.getLogger("loc_tms.api").exception(
                "vendor dispatch %s XLIFF render failed", did
            )
            return JSONResponse(
                {"ok": False, "error": "Could not render the XLIFF for this dispatch."},
                status_code=500,
            )
    return Response(
        content=data,
        media_type="application/xliff+xml",
        headers={"Content-Disposition": f'attachment; filename="dispatch-{did}.xlf"'},
    )


async def _read_body_capped(request: Request):
    """Stream a request body, refusing anything over config.WEBHOOK_MAX_BODY_BYTES BEFORE
    buffering it — the SAME defense the unauthenticated vendor-return webhook enforces
    (app/web/api.py::vendor_return_callback), reusing its CONSTANT so the number is never
    duplicated. Checks a declared Content-Length over the cap first (fast reject), then
    bounds the actual stream (the no/lying-Content-Length case). Returns the body bytes, or
    None when over cap (the caller answers 413). AUDIT INFO-1: the admin-only manual return
    lacked this, so an oversized body was fully buffered before parse."""
    from app import config

    cap = config.WEBHOOK_MAX_BODY_BYTES
    declared = request.headers.get("content-length")
    if declared is not None:
        try:
            if int(declared) > cap:
                return None
        except ValueError:
            pass
    chunks, total = [], 0
    async for piece in request.stream():
        total += len(piece)
        if total > cap:
            return None
        chunks.append(piece)
    return b"".join(chunks)


@router.post("/api/vendors/returns")
async def api_vendor_return(request: Request):
    """MANUAL RE-UPLOAD of a vendor return contract (the always-available fallback for a
    system that never calls back). Body: the return contract itself, or
    `{"claim_token": ..., "payload": {...}}`; optional `dispatch_id` pins which leg the
    file is allowed to belong to.

    Vendor Wave D: the body may ALSO be an XLIFF 1.2 file (Content-Type application/xml, or
    a body that sniffs as XML). Since XLIFF has no JSON envelope, the `claim_token` and
    optional `dispatch_id` are then read from the QUERY STRING. The XLIFF is transport-
    decoded into the same {id, target} contract and judged by the identical gates.

    ADMIN-only, and the operator's identity IS the authentication — but the VALIDATION is
    byte-identical to the signed callback: the transport never decides how content is
    judged. Every returned string still goes through `quality.commit_mt_output` with every
    guard lane on, a source-echo is still a no-op, and the id accounting still fails the
    whole return on an unknown/duplicate id. Resumes SYNCHRONOUSLY (the api_cli
    convention, mirroring POST /api/runs/{id}/resume) so the caller sees the outcome."""
    blocked = deps.feature_api_block("vendors")
    if blocked:
        return blocked
    ident, org_id, blocked = _gate(request)
    if blocked:
        return blocked
    blocked = _admin_block(ident)
    if blocked:
        return blocked
    from app.kernel import vendor_return, vendor_xliff

    raw = await _read_body_capped(request)
    if raw is None:
        from app import config

        return JSONResponse(
            {
                "ok": False,
                "error": "Return payload too large (limit %d bytes)."  # noqa: UP031
                % config.WEBHOOK_MAX_BODY_BYTES,
            },
            status_code=413,
        )
    if vendor_xliff._looks_like_xliff(raw):
        # XLIFF transport: no JSON envelope, so the leg-binding token + pin ride the query.
        inner = raw
        claim_token = str(request.query_params.get("claim_token") or "")
        dispatch_id = request.query_params.get("dispatch_id")
    else:
        import json as _json_mod

        try:
            body = _json_mod.loads(raw.decode("utf-8")) if raw else {}
        except (ValueError, UnicodeDecodeError):
            body = {}
        if not isinstance(body, dict):
            body = {}
        inner = body.get("payload") if isinstance(body.get("payload"), dict) else body
        claim_token = str(body.get("claim_token") or "")
        dispatch_id = body.get("dispatch_id")
    with Session(engine) as s:
        try:
            summary = vendor_return.admit_manual(
                s,
                inner,
                claim_token=claim_token,
                org_id=org_id,
                actor=ident["actor"],
                resume="sync",
                dispatch_id=dispatch_id,
            )
        except vendor_return.ReturnAuthError as exc:
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=404)
        except vendor_return.ReturnStateError as exc:
            return JSONResponse({"ok": False, "error": str(exc), "applied": False}, status_code=409)
        except vendor_return.ReturnContractError as exc:
            return JSONResponse({"ok": False, "error": str(exc), "applied": False}, status_code=400)
    return JSONResponse(summary)


@router.post("/api/vendors/poll")
async def api_vendor_poll(request: Request):
    """POLL vendors for ready returns (transport 3, decision 7). Body: optional
    `vendor_id` / `dispatch_id`, optional `force` to ignore the per-leg throttle window.

    Outbound, so it runs through the SAME SSRF guard + https allowlist as dispatch. A
    vendor with no `config.poll_url` is never polled — polling is an explicit per-vendor
    opt-in, and no vendor is GET-probed on an intake URL it only agreed to receive POSTs
    on. ADMIN-only (it initiates egress on the install's behalf)."""
    blocked = deps.feature_api_block("vendors")
    if blocked:
        return blocked
    ident, org_id, blocked = _gate(request)
    if blocked:
        return blocked
    blocked = _admin_block(ident)
    if blocked:
        return blocked
    from app.kernel import vendor_return
    from app.models import VendorDispatch

    body = await _json(request)
    try:
        did = int(body["dispatch_id"]) if body.get("dispatch_id") is not None else None
        vid = int(body["vendor_id"]) if body.get("vendor_id") is not None else None
    except (TypeError, ValueError):
        return JSONResponse(
            {"ok": False, "error": "dispatch_id / vendor_id must be integers."}, status_code=400
        )
    with Session(engine) as s:
        if did is not None:
            leg = s.get(VendorDispatch, did)
            if leg is None or (org_id is not None and leg.org_id != org_id):
                return JSONResponse(_NOT_FOUND, status_code=404)
            try:
                res = vendor_return.poll_leg(s, leg, actor=ident["actor"], resume="sync")
            except Exception as exc:
                return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
            return JSONResponse(
                {
                    "ok": True,
                    "polled": 1,
                    "admitted": 1 if res else 0,
                    "results": [res] if res else [],
                }
            )
        out = vendor_return.poll_due_legs(
            s,
            vendor_id=vid,
            org_id=org_id,
            throttle=not bool(body.get("force")),
            actor=ident["actor"],
            resume="sync",
        )
    return JSONResponse({"ok": True, **out})


# --- Wave E: quote-approval gate (R3) + reject/rework (R4) -------------------
def _leg_or_404(s, did, org_id):
    """Resolve a dispatch leg by id, org-scoped (a foreign-org leg is an identical 404, never
    enumerable). Returns (leg, None) or (None, JSONResponse 404)."""
    from app.models import VendorDispatch

    leg = s.get(VendorDispatch, did)
    if leg is None or (org_id is not None and leg.org_id != org_id):
        return None, JSONResponse(_NOT_FOUND, status_code=404)
    return leg, None


@router.post("/api/vendors/dispatches/{did}/quote")
async def api_vendor_quote_record(did: int, request: Request):
    """Record a QUOTE on a quote-gated (QUOTED) leg -- the vendor/operator's price for the
    work (Wave E R3). Body: `{quoted_cost_usd, currency?, note?}`. Recording NEVER moves money
    and NEVER advances the leg -- approval is the separate spend decision. ADMIN-only,
    org-scoped."""
    blocked = deps.feature_api_block("vendors")
    if blocked:
        return blocked
    ident, org_id, blocked = _gate(request)
    if blocked:
        return blocked
    blocked = _admin_block(ident)
    if blocked:
        return blocked
    from app.kernel import vendor_quote

    body = await _json(request)
    with Session(engine) as s:
        leg, err = _leg_or_404(s, did, org_id)
        if err:
            return err
        try:
            out = vendor_quote.record_quote(
                s,
                leg,
                quoted_cost_usd=body.get("quoted_cost_usd"),
                currency=str(body.get("currency") or ""),
                note=str(body.get("note") or ""),
                org_id=org_id,
                actor=ident["actor"],
                source="manual",
            )
        except vendor_quote.QuoteStateError as exc:
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=409)
        except vendor_quote.QuoteContractError as exc:
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
    return JSONResponse(out)


@router.post("/api/vendors/dispatches/{did}/approve-quote")
async def api_vendor_quote_approve(did: int, request: Request):
    """APPROVE a quote (Wave E R3) -- a SPEND decision. Records the approved amount, flips the
    leg QUOTED -> DISPATCHED so work proceeds and a return may be admitted, restarts the wait
    clock, and best-effort notifies the vendor. Body: optional `{quoted_cost_usd, currency?,
    note?}` (approve + set the amount in one step). ADMIN-only, org-scoped."""
    blocked = deps.feature_api_block("vendors")
    if blocked:
        return blocked
    ident, org_id, blocked = _gate(request)
    if blocked:
        return blocked
    blocked = _admin_block(ident)
    if blocked:
        return blocked
    from app.kernel import vendor_quote

    body = await _json(request)
    with Session(engine) as s:
        leg, err = _leg_or_404(s, did, org_id)
        if err:
            return err
        try:
            out = vendor_quote.approve_quote(
                s,
                leg,
                quoted_cost_usd=body.get("quoted_cost_usd"),
                currency=str(body.get("currency") or ""),
                note=str(body.get("note") or ""),
                org_id=org_id,
                actor=ident["actor"],
            )
        except vendor_quote.QuoteStateError as exc:
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=409)
        except vendor_quote.QuoteContractError as exc:
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
    return JSONResponse(out)


@router.post("/api/vendors/dispatches/{did}/decline-quote")
async def api_vendor_quote_decline(did: int, request: Request):
    """DECLINE a quote (Wave E R3): the operator will not proceed at this price. Reuses the
    escalate-to-a-human-review-pause transition (decision 3) -- the leg ESCALATES and a person
    owns the parked run. Body: optional `{reason?}`. ADMIN-only, org-scoped."""
    blocked = deps.feature_api_block("vendors")
    if blocked:
        return blocked
    ident, org_id, blocked = _gate(request)
    if blocked:
        return blocked
    blocked = _admin_block(ident)
    if blocked:
        return blocked
    from app.kernel import vendor_quote

    body = await _json(request)
    with Session(engine) as s:
        leg, err = _leg_or_404(s, did, org_id)
        if err:
            return err
        try:
            out = vendor_quote.decline_quote(
                s, leg, reason=str(body.get("reason") or ""), org_id=org_id, actor=ident["actor"]
            )
        except vendor_quote.QuoteStateError as exc:
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=409)
    return JSONResponse(out)


@router.post("/api/vendors/dispatches/{did}/reject-rework")
async def api_vendor_reject_rework(did: int, request: Request):
    """REJECT a delivered (RETURNED) leg and RE-DISPATCH a rework to the SAME vendor with
    structured feedback (Wave E R4). A SPEND decision (a rework re-dispatch is billable human
    work). Caps rework rounds (default 1); at the cap it escalates to a human instead. Body:
    optional `{note?, feedback?: [{id, issues?, note?}]}`. ADMIN-only, org-scoped. The rework
    return funnels through the SINGLE guarded admission seam -- this route never injects
    content, it re-sends a job."""
    blocked = deps.feature_api_block("vendors")
    if blocked:
        return blocked
    ident, org_id, blocked = _gate(request)
    if blocked:
        return blocked
    blocked = _admin_block(ident)
    if blocked:
        return blocked
    from app.kernel import vendor_rework

    body = await _json(request)
    fb = body.get("feedback")
    with Session(engine) as s:
        leg, err = _leg_or_404(s, did, org_id)
        if err:
            return err
        try:
            out = vendor_rework.reject_and_rework(
                s,
                leg,
                feedback=fb if isinstance(fb, list) else None,
                note=str(body.get("note") or ""),
                org_id=org_id,
                actor=ident["actor"],
            )
        except vendor_rework.ReworkStateError as exc:
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=409)
    return JSONResponse(out)


@router.get("/api/vendors/{vid}")
def api_vendors_show(vid: int, request: Request):
    # H1/M8: admin-only read (the detail carries endpoint host + linked customers).
    blocked = deps.feature_api_block("vendors")
    if blocked:
        return blocked
    ident, org_id, blocked = _gate(request)
    if blocked:
        return blocked
    blocked = _admin_block(ident)
    if blocked:
        return blocked
    with Session(engine) as s:
        v = vendors.visible(s, vid, org_id=org_id)
        if v is None:
            return JSONResponse(_NOT_FOUND, status_code=404)
        out = vendors.to_dict(s, v)
    return JSONResponse({"ok": True, "vendor": out})


@router.post("/api/vendors/{vid}")
async def api_vendors_update(vid: int, request: Request):
    """Partial update of the vendor's TEXT/CONFIG fields (a present key changes it; a
    blank string clears it). ADMIN-only, mirroring the web vendor settings update (H1).

    R3 RETRO-GATE (SPEC-full-remote-parity, deliberate hardening): the connection
    SECRET (`signing_secret`/`auth_token`) is NO LONGER settable here. This route is
    docs-scoped, and a docs key setting a vendor secret is an egress-redirect risk (the
    signing secret + endpoint host are what point every linked customer's content at a
    host). Presenting either field is now a clean 400 that names the dedicated
    admin-gated path: POST /api/vendors/{vid}/secret (admin scope + remote_admin_enabled,
    write-only). A docs key can no longer reach the Fernet secret at all."""
    blocked = deps.feature_api_block("vendors")
    if blocked:
        return blocked
    ident, org_id, blocked = _gate(request)
    if blocked:
        return blocked
    blocked = _admin_block(ident)  # H1: operator-admin, mirrors web
    if blocked:
        return blocked
    body = await _json(request)
    # RETRO-GATE: refuse the secret fields on this docs-scoped route (moved behind the
    # R3 admin gate). Refused BEFORE any write, so nothing is persisted.
    if any(f in body for f in ("signing_secret", "auth_token")):
        return JSONResponse(
            {
                "ok": False,
                "error": "signing_secret/auth_token are no longer settable "  # noqa: UP031
                "here; use POST /api/vendors/%d/secret (admin scope + remote_admin_enabled, "
                "write-only)." % vid,
            },
            status_code=400,
        )
    cfg_present = "config" in body
    cfg = body.get("config")
    if cfg_present and cfg is not None and not isinstance(cfg, dict):
        return JSONResponse({"ok": False, "error": "config must be an object."}, status_code=400)
    with Session(engine) as s:
        if vendors.visible(s, vid, org_id=org_id) is None:
            return JSONResponse(_NOT_FOUND, status_code=404)
        try:
            if any(
                k in body for k in ("name", "description", "endpoint_url", "config", "protocol")
            ):
                vendors.update(
                    s,
                    vid,
                    name=body["name"] if "name" in body else None,  # noqa: SIM401
                    description=body["description"] if "description" in body else None,  # noqa: SIM401
                    endpoint_url=body["endpoint_url"] if "endpoint_url" in body else None,  # noqa: SIM401
                    config_json=cfg if cfg_present else None,
                    protocol=body["protocol"] if "protocol" in body else None,  # noqa: SIM401
                    org_id=org_id,
                    actor=ident["actor"],
                    ip=deps._ip(request),
                )
        except vendors.VendorError as exc:
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
        v = vendors.visible(s, vid, org_id=org_id)
        out = vendors.to_dict(s, v)
    return JSONResponse({"ok": True, "vendor": out})


@router.post("/api/vendors/{vid}/rate-card")
async def api_vendors_rate_card(vid: int, request: Request):
    """Set the vendor's MSA rate card. Body: `currency` + a `bands` map (per-band cost
    per word). Normalized on the way in (unknown bands dropped, missing -> 0.0).
    ADMIN-only (H1)."""
    blocked = deps.feature_api_block("vendors")
    if blocked:
        return blocked
    ident, org_id, blocked = _gate(request)
    if blocked:
        return blocked
    blocked = _admin_block(ident)  # H1: operator-admin, mirrors web
    if blocked:
        return blocked
    body = await _json(request)
    bands = body.get("bands")
    with Session(engine) as s:
        if vendors.visible(s, vid, org_id=org_id) is None:
            return JSONResponse(_NOT_FOUND, status_code=404)
        try:
            # H3: a non-finite / negative / non-numeric / unknown-band card is refused
            # (nothing stored) with a clean 400 naming the bad band.
            v = vendors.set_rate_card(
                s,
                vid,
                {
                    "currency": body.get("currency"),
                    "bands": bands if isinstance(bands, dict) else {},
                },
                org_id=org_id,
                actor=ident["actor"],
                ip=deps._ip(request),
            )
        except vendors.VendorError as exc:
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
        out = vendors.to_dict(s, v)
    return JSONResponse({"ok": True, "vendor": out})


@router.post("/api/vendors/{vid}/link-customer")
async def api_vendors_link_customer(vid: int, request: Request):
    """Link a vendor to a customer (the fail-closed scoping seam — an external step may
    only select a vendor linked to the document's customer). Idempotent. Body:
    `customer_id`. ADMIN-only (H1). A cross-org / unknown id is a clean 400."""
    blocked = deps.feature_api_block("vendors")
    if blocked:
        return blocked
    ident, org_id, blocked = _gate(request)
    if blocked:
        return blocked
    blocked = _admin_block(ident)  # H1: operator-admin, mirrors web
    if blocked:
        return blocked
    body = await _json(request)
    with Session(engine) as s:
        # Org-agnostic existence check for LINK (cross-org linking, 2026-08-08): any active
        # vendor is a valid target; the service enforces the customer-side (buyer) authority.
        # Non-enumerable 404 for an unknown/archived id; NO vendor-org gate here.
        # #73: access.UNSCOPED is the DELIBERATE cross-org scope (a bare None now fails
        # closed in selectable/visible under the gate); the customer-side ring authorizes.
        from app.kernel import access as _access

        if vendors.selectable(s, vid, org_id=_access.UNSCOPED) is None:
            return JSONResponse(_NOT_FOUND, status_code=404)
        try:
            linked, v = vendors.link_customer(
                s,
                vid,
                body.get("customer_id"),
                org_id=org_id,
                actor=ident["actor"],
                ip=deps._ip(request),
            )
        except vendors.VendorError as exc:
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
        # Ring-safe view: full to_dict for an own-org (or single-org) vendor; a minimal,
        # org-filtered view for a cross-org link target (no foreign connection/config/ids).
        out = vendors.scoped_link_view(s, v, org_id=org_id)
    return JSONResponse({"ok": True, "linked": linked, "vendor": out})


@router.post("/api/vendors/{vid}/unlink-customer")
async def api_vendors_unlink_customer(vid: int, request: Request):
    """Remove a vendor<->customer link. Idempotent (removed=False when the link did
    not exist). Body: `customer_id`. ADMIN-only (H1)."""
    blocked = deps.feature_api_block("vendors")
    if blocked:
        return blocked
    ident, org_id, blocked = _gate(request)
    if blocked:
        return blocked
    blocked = _admin_block(ident)  # H1: operator-admin, mirrors web
    if blocked:
        return blocked
    body = await _json(request)
    with Session(engine) as s:
        # Org-agnostic existence check for UNLINK (cross-org linking, 2026-08-08): the
        # service authorizes on EITHER party's org (buyer or vendor admin). Non-enumerable
        # 404 for an unknown id; NO vendor-org gate here (archived vendor permitted).
        # #73: access.UNSCOPED is the DELIBERATE cross-org scope (a bare None now fails
        # closed in visible() under the gate); the service enforces either-party authority.
        from app.kernel import access as _access

        if vendors.visible(s, vid, org_id=_access.UNSCOPED) is None:
            return JSONResponse(_NOT_FOUND, status_code=404)
        removed, v = vendors.unlink_customer(
            s,
            vid,
            body.get("customer_id"),
            org_id=org_id,
            actor=ident["actor"],
            ip=deps._ip(request),
        )
        # Ring-safe view (same rule as link): a cross-org vendor never leaks its
        # connection/config or another org's customer ids in the unlink response.
        out = vendors.scoped_link_view(s, v, org_id=org_id) if v is not None else None
    return JSONResponse({"ok": True, "removed": removed, "vendor": out})


@router.post("/api/vendors/{vid}/delete")
async def api_vendors_delete(vid: int, request: Request):
    """Delete a vendor. ADMIN-only. FAILS CLOSED (409) with an itemized reference count
    while a workflow step selects it or a dispatch leg references it — never a cascade.
    Customer links (which only scope the vendor) are removed as part of the delete.
    Org bound via vendors.visible; ring-before-role like the customer delete."""
    blocked = deps.feature_api_block("vendors")
    if blocked:
        return blocked
    ident, org_id, blocked = _gate(request)
    if blocked:
        return blocked
    with Session(engine) as s:
        # Ring BEFORE role (permanent route-sweep finding): a foreign-org id is 404
        # (not enumerable), never a 403 that leaks its existence.
        if vendors.visible(s, vid, org_id=org_id) is None:
            return JSONResponse(_NOT_FOUND, status_code=404)
        blocked = _admin_block(ident)
        if blocked:
            return blocked
        try:
            vendors.delete(s, vid, org_id=org_id, actor=ident["actor"], ip=deps._ip(request))
        except vendors.VendorDeleteBlocked as exc:
            return JSONResponse(
                {"ok": False, "error": str(exc), "references": exc.refs}, status_code=409
            )
    return JSONResponse({"ok": True, "vendor_id": vid, "deleted": True})


# --- F. vendor secret -------------------------------------------------------
@router.post("/api/vendors/{vid}/secret")
async def api_vendor_secret_set(request: Request, vid: int):
    """Set a vendor's Fernet-encrypted connection secret (signing_secret / auth_token).
    admin scope + is_admin + remote_admin_enabled, vendor RINGED by org. This is the
    write-only secret path RETRO-GATED off the Wave-A docs-scoped update route (a docs
    key must NOT set a vendor secret - an egress-redirect risk). Body: {"signing_secret":
    ..., "auth_token": ...} (either/both; merged into the encrypted secret). WRITE-ONLY:
    the response is the masked to_dict (has_secret flag only, never a value)."""
    blocked = deps.feature_api_block("vendors")  # ffadmin Wave 3 (MEDIUM-1): flag first
    if blocked:
        return blocked
    ident, org_id, blocked = _r3_gate(request)
    if blocked:
        return blocked
    body = await _json(request)
    with Session(engine) as s:
        # SCOPE BEFORE VALIDATE (audit F4): ring the vendor to a 404 FIRST, so a
        # foreign/missing vendor is refused before the body is judged — matching
        # every sibling route (vendor.delete: "Ring BEFORE role"). Previously a
        # malformed body 400'd ahead of the ring; harmless (not an oracle — a
        # missing vendor + malformed body also 400'd) but inconsistent.
        if vendors.visible(s, vid, org_id=org_id) is None:
            return JSONResponse(_NOT_FOUND, status_code=404)
        secret = {f: body[f] for f in ("signing_secret", "auth_token") if f in body}
        if not secret:
            return JSONResponse(
                {"ok": False, "error": "body must carry signing_secret and/or auth_token."},
                status_code=400,
            )
        try:
            vendors.set_secret(
                s, vid, secret, org_id=org_id, actor=ident.get("actor", ""), ip=deps._ip(request)
            )
        except vendors.VendorError as exc:
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
        out = vendors.to_dict(s, vendors.visible(s, vid, org_id=org_id))
    return JSONResponse({"ok": True, "vendor": out})
