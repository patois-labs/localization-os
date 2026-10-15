"""Vendors UI (/vendors) — the operator-admin surface for first-class external
translation/review vendors (PROPOSAL-external-processing-step §9 decision 6; combined
build Wave A).

Thin web layer over app/services/vendors.py + app/services/vendor_cost.py (the ONE source
of truth — no vendor logic lives here). Every list/lookup is org-scoped via _my_org_id;
every mutation is a POST that redirects back with ?saved=1 (or ?err=<quoted> on a caught
vendors.VendorError). Vendors are an operator-admin surface, so every route is gated by
_admin_block. The credentials blob is write-only: it never reaches a template (to_dict
carries only a has_secret presence flag).

Audit is done INSIDE the vendors service (create/update/set_secret/rate_card/link/…), so
the routes never double-audit — mirroring content_types.py's split with its service.

Documented in: docs/architecture/vendor-outsourcing.md
"""

import html
from urllib.parse import quote

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlmodel import Session, select

from app import branding, config
from app.db import get_session
from app.kernel import (
    access,
    oidc,
    security,
    vendor_cost,
    vendor_dispatch,
    vendor_return,
    vendors,
)
from app.models import Customer
from app.services import vendor_studio_sso
from app.web import page_guard
from app.web._forms import VendorProtocolForm
from app.web.deps import (
    _actor_email,
    _admin_block,
    _error_page,
    _ip,
    _my_org_id,
    feature_block,
    read_capped_upload,
    templates,
)
from app.web.form_errors import redirect_with_error

router = APIRouter()


def _flag_off_block(request: Request):
    """Layer-2 WEB kill switch for the `vendors` feature (the middleware in app/main is
    layer 1): a themed 404 (non-enumerable, never 403) when vendors_enabled is off, else
    None. Delegated to the ONE shared deps.feature_block, mirroring images.py/keys.py --
    called FIRST on every vendor route (before _admin_block) so a disabled feature is
    indistinguishable from a route that was never built, even to an admin prober."""
    return feature_block(request, "vendors")


def _safe_next(nxt, fallback: str) -> str:
    """Open-redirect guard for a form-supplied post-action `next` target (feat/run-doc-xliff-
    surfaces): a value survives ONLY if it is a same-site absolute path — exactly ONE leading
    '/', not '//', no scheme (':') and no backslash — mirroring oidc._sanitize_next's strict
    check. Anything else (a scheme, a protocol-relative '//host', a '/\\host' backslash trick,
    or an empty field) falls back to the vendor page. The POST route stays independently
    admin-gated regardless of where it redirects — this only bounds the redirect target."""
    nxt = (nxt or "").strip()
    # The ONE shared test (security review 2026-10 web-01): also rejects TAB, CR, LF and
    # every other control character, which browsers drop inside a URL.
    if oidc.is_unsafe_local_target(nxt):
        return fallback
    return nxt


def _with_query(base: str, qs: str) -> str:
    """Append a query string to a validated redirect base, preserving the existing
    ?saved=1 / ?err=<quoted> convention even when `base` (a `next` like
    /documents/1?locale=es-ES) already carries a query string.

    FRAGMENT-AWARE (feat/run-doc-xliff-surfaces round 2): a `next` may carry a
    #fragment anchor (e.g. /runs/1#vendor-handoff). The query must be spliced BEFORE
    the fragment (`...?saved=1#vendor-handoff`) — a naive append would produce the
    invalid `...#vendor-handoff?saved=1` (the '?' becomes part of the fragment and the
    banner never sees the param)."""
    base, sep, frag = base.partition("#")
    joiner = "&" if "?" in base else "?"
    return f"{base}{joiner}{qs}{sep}{frag}"


def _customers(request, session):
    """Org-scoped ACTIVE customer list for the link-customer picker. org_filter is a
    no-op with the multi-org gate off (byte-identical to single-tenant). Archived
    customers are retired from the picker (a vendor can't be freshly linked to one)."""
    from app.kernel import customers as customer_service

    me = security.session_user(request)
    q = customer_service.active_only(access.org_filter(select(Customer), Customer, me)).order_by(
        Customer.name
    )
    return list(session.exec(q).all())


def _vendor_or_404(request, session, vid):
    """Fetch a vendor, org-scoped (a foreign / missing id is an identical 404 — not
    enumerable). Returns (vendor, blocked). The ADMINISTRATION gate for the vendor-owning
    routes (detail/update/secret/rate-card/archive/delete) — a vendor is administered
    within its org."""
    v = vendors.visible(session, vid, org_id=_my_org_id(request))
    if v is None:
        return None, _error_page(request, 404, "Vendor not found.")
    return v, None


def _leg_foreign_org(request, leg) -> bool:
    """#488 item 1: is this dispatch leg another org's? `VendorDispatch.org_id` is the BUYER's
    org and, with cross-org vendor linking, the vendor's owner may live in a different org than
    the leg. The vendor check (`leg.vendor_id == v.id`) therefore does not confine a leg: every
    leg route also needs this buyer-org check, the same wall the /api and CLI leg routes apply
    (`leg.org_id != org_id`). _my_org_id is None with the gate off (no scoping, byte-identical)
    and the -1 poison for an org-less user under it (matches no leg: fail closed)."""
    my_org = _my_org_id(request)
    return my_org is not None and leg.org_id != my_org


def _link_target_vendor_or_404(request, session, vid, *, active: bool):
    """Resolve a vendor for a LINK/UNLINK route ORG-AGNOSTICALLY (cross-org linking,
    2026-08-08): the SERVICE owns authorization (the customer ring on link, the either-party
    rule on unlink), so the route must NOT add a vendor-org gate — any vendor install-wide
    is a valid link target. Existence stays non-enumerable (an unknown id is an identical
    404). `active=True` (link) also 404s an archived vendor (not linkable); `active=False`
    (unlink) permits an archived vendor so its stale link can still be severed. Returns
    (vendor, blocked).

    #73: access.UNSCOPED is the DELIBERATE cross-org scope -- a bare None now fails
    closed in selectable/visible under the gate, which would 404 every cross-org link
    target. The service still owns authorization (customer ring / either-party rule)."""
    from app.kernel import access

    resolve = vendors.selectable if active else vendors.visible
    v = resolve(session, vid, org_id=access.UNSCOPED)
    if v is None:
        return None, _error_page(request, 404, "Vendor not found.")
    return v, None


# --- list + create -----------------------------------------------------------
@router.get("/vendors")
def vendors_index(request: Request, session: Session = Depends(get_session)):
    blocked = _flag_off_block(request)
    if blocked:
        return blocked
    _me, blocked = _admin_block(request)
    if blocked:
        return blocked
    rows = [
        vendors.to_dict(session, v)
        for v in vendors.list_vendors(session, org_id=_my_org_id(request), include_archived=True)
    ]
    return templates.TemplateResponse(
        request,
        "vendors.html",
        {
            "request": request,
            "rows": rows,
            "protocols": vendor_dispatch.protocol_names(),
            "vendor_studio_configured": vendor_studio_sso.is_configured(),
        },
    )


@router.post("/vendors")
def vendor_create(
    request: Request,
    name: str = Form(""),
    description: str = Form(""),
    endpoint_url: str = Form(""),
    protocol: str = Form("native"),
    session: Session = Depends(get_session),
):
    blocked = _flag_off_block(request)
    if blocked:
        return blocked
    _me, blocked = _admin_block(request)
    if blocked:
        return blocked
    if not (name or "").strip():
        # a11y T7: the one field this form requires is a field error (code + field).
        return redirect_with_error("/vendors", "vendor_name_required")
    try:
        v = vendors.create(
            session,
            name,
            description=description,
            endpoint_url=endpoint_url,
            protocol=protocol,
            org_id=_my_org_id(request),
            created_by=_actor_email(request),
            actor=_actor_email(request),
            ip=_ip(request),
        )
    except vendors.VendorError as exc:
        return RedirectResponse("/vendors?err=" + quote(str(exc)), status_code=303)
    return RedirectResponse(f"/vendors/{v.id}?saved=1", status_code=303)


# --- detail (edit / connection / rate card / customer links) -----------------
@router.get("/vendors/{vid}")
def vendor_detail(vid: int, request: Request, session: Session = Depends(get_session)):
    blocked = _flag_off_block(request)
    if blocked:
        return blocked
    _me, blocked = _admin_block(request)
    if blocked:
        return blocked
    v, blocked = _vendor_or_404(request, session, vid)
    if blocked:
        return blocked
    vd = vendors.to_dict(session, v)
    all_customers = _customers(request, session)
    linked_ids = set(vd["customer_ids"])
    by_id = {c.id: c for c in all_customers}
    # Linked-customer rows (name-resolved). A link to a customer outside the active
    # picker set (e.g. one archived after linking) is still shown so it can be unlinked.
    # NAME-LEAK GUARD (cross-org linking, 2026-08-08): a linked customer may now live in
    # another org; never render a FOREIGN customer's NAME here. `by_id` (this viewer's ring)
    # resolves own-org names (incl. archived); a fallback fetch degrades to a bare id label
    # when the row is outside the viewer's org.
    my_org = _my_org_id(request)
    linked = []
    for cid in vd["customer_ids"]:
        c = by_id.get(cid)
        if c is None:
            row = session.get(Customer, cid)
            if row is not None and my_org is not None and getattr(row, "org_id", None) != my_org:
                row = None  # foreign -> id label, never the name
            c = row
        linked.append({"id": cid, "name": (c.name if c else f"customer {cid}")})
    # Customers still available to link (org set minus those already linked).
    available = [c for c in all_customers if c.id not in linked_ids]
    # Wave B: the dispatch legs this vendor is holding, so the manual-return fallback
    # (and the poll trigger) have somewhere to live. leg_to_dict carries the endpoint
    # HOST and never the claim token — the token admits content, so no read surface
    # echoes it, exactly as the dispatch SSE envelope withholds it.
    legs = []
    for leg in vendor_return.open_legs(session, vendor_id=v.id, org_id=_my_org_id(request)):
        d = vendor_return.leg_to_dict(session, leg)
        d["band_rows"] = vendor_cost.estimate_band_rows(leg.estimate)  # interop R6
        legs.append(d)
    # Wave E R4: recently DELIVERED (RETURNED) legs this vendor can still be asked to rework.
    # A reject re-dispatches a rework round to the same vendor with structured feedback.
    from app.models import VendorDispatch as _VD

    _q = select(_VD).where(_VD.vendor_id == v.id, _VD.status == "returned")
    if my_org is not None:
        _q = _q.where(_VD.org_id == my_org)
    returned_legs = [
        vendor_return.leg_to_dict(session, leg)
        for leg in session.exec(_q.order_by(_VD.id.desc()).limit(25)).all()
    ]
    return templates.TemplateResponse(
        request,
        "vendor_detail.html",
        {
            "request": request,
            "v": vd,
            "linked": linked,
            "available": available,
            "bands": vendor_cost.BANDS,
            "band_labels": vendor_cost.XLIFF_STATE_QUALIFIER,
            "covered_bands": vendor_cost.COVERED_BANDS,
            "legs": legs,
            "returned_legs": returned_legs,
            "protocols": vendor_dispatch.protocol_names(),
        },
    )


@router.post("/vendors/{vid}")
def vendor_update(
    vid: int,
    request: Request,
    name: str = Form(""),
    description: str = Form(""),
    endpoint_url: str = Form(""),
    protocol: VendorProtocolForm = None,
    session: Session = Depends(get_session),
):
    blocked = _flag_off_block(request)
    if blocked:
        return blocked
    _me, blocked = _admin_block(request)
    if blocked:
        return blocked
    _v, blocked = _vendor_or_404(request, session, vid)
    if blocked:
        return blocked
    try:
        vendors.update(
            session,
            vid,
            name=name,
            description=description,
            endpoint_url=endpoint_url,
            protocol=protocol,
            org_id=_my_org_id(request),
            actor=_actor_email(request),
            ip=_ip(request),
        )
    except vendors.VendorError as exc:
        return RedirectResponse(f"/vendors/{vid}?err=" + quote(str(exc)), status_code=303)
    return RedirectResponse(f"/vendors/{vid}?saved=1", status_code=303)


@router.post("/vendors/{vid}/secret")
def vendor_secret(
    vid: int,
    request: Request,
    signing_secret: str = Form(""),
    auth_token: str = Form(""),
    session: Session = Depends(get_session),
):
    """Set the vendor's write-only credentials (signing secret + auth token). A BLANK
    field is OMITTED, so it keeps the stored value (the connector "leave blank to keep"
    convention); a submitted value replaces it. The plaintext is never echoed back."""
    blocked = _flag_off_block(request)
    if blocked:
        return blocked
    _me, blocked = _admin_block(request)
    if blocked:
        return blocked
    _v, blocked = _vendor_or_404(request, session, vid)
    if blocked:
        return blocked
    # Only non-blank fields are sent -> a blank field is a no-op keep (set_secret merges
    # over the stored blob; an omitted key is left untouched).
    secret = {
        k: val
        for k, val in (("signing_secret", signing_secret), ("auth_token", auth_token))
        if val.strip()
    }
    vendors.set_secret(
        session,
        vid,
        secret,
        org_id=_my_org_id(request),
        actor=_actor_email(request),
        ip=_ip(request),
    )
    return RedirectResponse(f"/vendors/{vid}?saved=1", status_code=303)


@router.post("/vendors/{vid}/dispatch-options")
def vendor_dispatch_options(
    vid: int,
    request: Request,
    omit_locked_targets: str = Form(None),
    requires_quote_approval: str = Form(None),
    session: Session = Depends(get_session),
):
    """VP-0 per-vendor dispatch option: `omit_locked_targets` (a checkbox). When set, a
    dispatched LOCKED (covered-band) segment ships its source + locked marker but an EMPTY
    target. Wave E R3 adds `requires_quote_approval` (a checkbox): a dispatch to this vendor
    parks QUOTED and awaits a quote + operator approval before work proceeds. Both are stored
    on the free-form vendor config blob, MERGED so any other config keys (poll_url / headers)
    survive."""
    blocked = _flag_off_block(request)
    if blocked:
        return blocked
    _me, blocked = _admin_block(request)
    if blocked:
        return blocked
    v, blocked = _vendor_or_404(request, session, vid)
    if blocked:
        return blocked
    cfg = dict(v.config or {})
    if omit_locked_targets:
        cfg["omit_locked_targets"] = True
    else:
        cfg.pop("omit_locked_targets", None)
    if requires_quote_approval:
        cfg["requires_quote_approval"] = True
    else:
        cfg.pop("requires_quote_approval", None)
    try:
        vendors.update(
            session,
            vid,
            config_json=cfg,
            org_id=_my_org_id(request),
            actor=_actor_email(request),
            ip=_ip(request),
        )
    except vendors.VendorError as exc:
        return RedirectResponse(f"/vendors/{vid}?err=" + quote(str(exc)), status_code=303)
    return RedirectResponse(f"/vendors/{vid}?saved=1", status_code=303)


@router.post("/vendors/{vid}/rate-card")
async def vendor_rate_card(vid: int, request: Request, session: Session = Depends(get_session)):
    """Set the MSA rate card: one cost-per-word per band + a currency. Read the band
    fields dynamically off the form (the band set is vendor_cost.BANDS), so the form and
    the model can't drift. set_rate_card normalizes (unknown dropped, missing -> 0.0)."""
    blocked = _flag_off_block(request)
    if blocked:
        return blocked
    _me, blocked = _admin_block(request)
    if blocked:
        return blocked
    _v, blocked = _vendor_or_404(request, session, vid)
    if blocked:
        return blocked
    form = await request.form()
    bands = {b: (form.get(f"band_{b}") or "").strip() for b in vendor_cost.BANDS}
    rate_card = {"currency": (form.get("currency") or "").strip(), "bands": bands}
    try:
        vendors.set_rate_card(
            session,
            vid,
            rate_card,
            org_id=_my_org_id(request),
            actor=_actor_email(request),
            ip=_ip(request),
        )
    except vendors.VendorError as exc:
        return RedirectResponse(f"/vendors/{vid}?err=" + quote(str(exc)), status_code=303)
    return RedirectResponse(f"/vendors/{vid}?saved=1", status_code=303)


@router.post("/vendors/{vid}/link-customer")
def vendor_link_customer(
    vid: int, request: Request, customer_id: str = Form(""), session: Session = Depends(get_session)
):
    blocked = _flag_off_block(request)
    if blocked:
        return blocked
    _me, blocked = _admin_block(request)
    if blocked:
        return blocked
    # Org-agnostic vendor resolution for LINK: any active vendor is a valid target; the
    # service enforces the customer-side (buyer) authority. Vendor-org gate deliberately absent.
    _v, blocked = _link_target_vendor_or_404(request, session, vid, active=True)
    if blocked:
        return blocked
    try:
        vendors.link_customer(
            session,
            vid,
            customer_id,
            org_id=_my_org_id(request),
            actor=_actor_email(request),
            ip=_ip(request),
        )
    except vendors.VendorError as exc:
        return RedirectResponse(f"/vendors/{vid}?err=" + quote(str(exc)), status_code=303)
    return RedirectResponse(f"/vendors/{vid}?saved=1", status_code=303)


@router.post("/vendors/{vid}/unlink-customer")
def vendor_unlink_customer(
    vid: int, request: Request, customer_id: str = Form(""), session: Session = Depends(get_session)
):
    blocked = _flag_off_block(request)
    if blocked:
        return blocked
    _me, blocked = _admin_block(request)
    if blocked:
        return blocked
    # Org-agnostic vendor resolution for UNLINK: the service authorizes on EITHER party's
    # org (buyer or vendor admin). No vendor-org gate added here; archived vendor permitted.
    _v, blocked = _link_target_vendor_or_404(request, session, vid, active=False)
    if blocked:
        return blocked
    vendors.unlink_customer(
        session,
        vid,
        customer_id,
        org_id=_my_org_id(request),
        actor=_actor_email(request),
        ip=_ip(request),
    )
    return RedirectResponse(f"/vendors/{vid}?saved=1", status_code=303)


# --- the guarded RETURN: manual re-upload + poll (Wave B) --------------------
@router.post("/vendors/{vid}/dispatches/{did}/return")
async def vendor_dispatch_return(
    vid: int, did: int, request: Request, session: Session = Depends(get_session)
):
    """MANUAL RE-UPLOAD of a vendor's return contract against ONE parked leg — the
    always-available fallback (§3.3) for a vendor system that never calls back.

    The operator's admin session is the authentication here (there is no signature to
    check on a file a person pasted), but the VALIDATION is byte-identical to the signed
    callback: the transport decides how bytes arrive, never how content is judged. The id
    accounting still fails the whole return on an unknown/duplicate id, every admitted
    string still goes through `quality.commit_mt_output` with all guard lanes on, and a
    source-echo is still a no-op that leaves the prior target alone.

    `did` PINS the leg: a paste of the wrong vendor's file is refused, not applied.
    Resumes asynchronously (the web resume convention) so the request never executes a
    workflow inline."""
    blocked = _flag_off_block(request)
    if blocked:
        return blocked
    _me, blocked = _admin_block(request)
    if blocked:
        return blocked
    v, blocked = _vendor_or_404(request, session, vid)
    if blocked:
        return blocked
    # The leg must be THIS vendor's: `did` is caller-supplied, so a leg id belonging to
    # another vendor (or another org) must 404 here rather than be pinned against.
    from app.models import VendorDispatch

    leg = session.get(VendorDispatch, did)
    if leg is None or leg.vendor_id != v.id or _leg_foreign_org(request, leg):
        return _error_page(request, 404, "Dispatch not found.")
    form = await request.form()
    # feat/run-doc-xliff-surfaces: an optional `next` lets the run-view / document-page
    # hand-off forms send the operator back to the page they acted from (a /runs/{id} or
    # /documents/{id}?locale=... anchor) instead of the vendor page. Validated STRICTLY
    # (open-redirect guard); default (no `next`) is byte-identical to the prior behaviour.
    dest = _safe_next(form.get("next"), f"/vendors/{vid}")
    raw = form.get("payload") or ""
    upload = form.get("file")
    file_named = False
    if hasattr(upload, "read"):
        # LOW-2: pass the file BYTES straight through (never .decode("utf-8"), which mojibaked
        # a UTF-16 .xlf); the JSON/XLIFF decoders detect the encoding themselves.
        # feat/run-doc-xliff-surfaces round 2 (real-browser paste): a browser submits an EMPTY
        # `filename=""` part for an UNTOUCHED file input, and Starlette still hands us an
        # UploadFile — so an unconditional `raw = await upload.read()` clobbered a valid PASTED
        # payload with b"" and the operator got "paste ... before submitting". Let the file win
        # ONLY when it actually carries bytes; otherwise keep the paste. (Fixes the same latent
        # bug on the vendor page, which shares this route.)
        file_named = bool((getattr(upload, "filename", "") or "").strip())
        # #275: bounded read at the upload cap (this used to read the whole file).
        _b = await read_capped_upload(upload, config.MAX_UPLOAD_BYTES)
        if _b is None:
            msg = f"the chosen file is too large (limit {config.MAX_UPLOAD_BYTES} bytes)."
            return RedirectResponse(_with_query(dest, "err=" + quote(msg)), status_code=303)
        if _b.strip():
            raw = _b
    empty = not (raw.strip() if isinstance(raw, (bytes, bytearray)) else str(raw).strip())
    if empty:
        # When empty, the paste is always blank (a non-blank paste would have kept `raw`). So a
        # NON-EMPTY filename that produced no bytes is a chosen-but-empty file -- name that real
        # problem instead of "before submitting"; an untouched input (blank filename) keeps the
        # honest no-file-no-paste copy.
        msg = (
            "the chosen file is empty — pick the finished .xlf (or paste the contract JSON)."
            if file_named
            else "paste the return contract JSON (or choose a file) before submitting."
        )
        return RedirectResponse(_with_query(dest, "err=" + quote(msg)), status_code=303)
    # Vendor Wave D: an XLIFF 1.2 return file carries no claim token (no JSON envelope), so
    # bind the upload to the URL-pinned leg by passing its token as the claim arg. This is
    # safe for JSON too: the arg selects the leg, and a wrong-file JSON body whose own token
    # mismatches is still refused by check_contract (the leg is already pinned by `did`).
    try:
        summary = vendor_return.admit_manual(
            session,
            raw,
            claim_token=leg.claim_token,
            org_id=_my_org_id(request),
            actor=_actor_email(request),
            resume="async",
            dispatch_id=did,
        )
    except vendor_return.VendorReturnError as exc:
        return RedirectResponse(_with_query(dest, "err=" + quote(str(exc))), status_code=303)
    # Wave 3 Train E (gate M2): surface the vendor-comment buckets on the WEB return, not just
    # the /api + CLI payloads. A rejected/skipped/omitted comment must never be lost silently.
    _rej = len(summary.get("comments_rejected") or []) if isinstance(summary, dict) else 0
    _adm = int(summary.get("comments_admitted") or 0) if isinstance(summary, dict) else 0
    _skip = summary.get("comments_skipped") if isinstance(summary, dict) else ""
    _up = int(summary.get("comments_omitted_upstream") or 0) if isinstance(summary, dict) else 0
    if _rej or _adm or _skip or _up:
        bits = []
        if _adm:
            bits.append(f"{_adm} vendor comment(s) admitted")
        if _rej:
            bits.append(f"{_rej} rejected (see the return report)")
        if _up:
            bits.append(f"{_up} not sent by the vendor (over cap / malformed reply)")
        if _skip:
            bits.append(f"comments skipped: {_skip}")
        return RedirectResponse(
            _with_query(dest, "saved=1&notice=" + quote("; ".join(bits))), status_code=303
        )
    return RedirectResponse(_with_query(dest, "saved=1"), status_code=303)


@router.get("/vendors/{vid}/dispatches/{did}/xliff")
def vendor_dispatch_xliff(
    vid: int, did: int, request: Request, session: Session = Depends(get_session)
):
    """Download the XLIFF 1.2 rendition of ONE dispatch leg (Vendor Wave D) so the operator
    can hand a real .xlf to a CAT tool (OmegaT / Weblate / Trados). A serialization of the
    same segments the JSON contract carried; never the claim token or any secret. The leg
    must be THIS vendor's (a foreign leg id 404s, not a cross-vendor read)."""
    from fastapi.responses import Response

    blocked = _flag_off_block(request)
    if blocked:
        return blocked
    _me, blocked = _admin_block(request)
    if blocked:
        return blocked
    v, blocked = _vendor_or_404(request, session, vid)
    if blocked:
        return blocked
    from app.kernel import vendor_xliff
    from app.models import VendorDispatch

    leg = session.get(VendorDispatch, did)
    if leg is None or leg.vendor_id != v.id or _leg_foreign_org(request, leg):
        return _error_page(request, 404, "Dispatch not found.")
    try:
        data = vendor_xliff.render_leg_xliff(session, leg)
    except vendor_xliff.XliffRenderError as exc:
        # A GET: the page guard only believes free text a GET route records itself.
        page_guard.attest(request, "err", str(exc))
        return RedirectResponse(f"/vendors/{vid}?err=" + quote(str(exc)), status_code=303)
    except Exception:
        import logging

        logging.getLogger("loc_tms.web").exception("vendor dispatch %s XLIFF render failed", did)
        failed = "Could not render the XLIFF for this dispatch."
        page_guard.attest(request, "err", failed)
        return RedirectResponse(f"/vendors/{vid}?err=" + quote(failed), status_code=303)
    return Response(
        content=data,
        media_type="application/xliff+xml",
        headers={"Content-Disposition": f'attachment; filename="dispatch-{did}.xlf"'},
    )


@router.post("/vendors/{vid}/dispatches/{did}/poll")
def vendor_dispatch_poll(
    vid: int, did: int, request: Request, session: Session = Depends(get_session)
):
    """Ask the vendor whether THIS leg's return is ready (transport 3). Outbound, so it
    runs through the same SSRF guard + https allowlist as dispatch; a vendor with no
    `config.poll_url` is never polled (polling is an explicit per-vendor opt-in)."""
    blocked = _flag_off_block(request)
    if blocked:
        return blocked
    _me, blocked = _admin_block(request)
    if blocked:
        return blocked
    v, blocked = _vendor_or_404(request, session, vid)
    if blocked:
        return blocked
    from app.models import VendorDispatch

    leg = session.get(VendorDispatch, did)
    if leg is None or leg.vendor_id != v.id or _leg_foreign_org(request, leg):
        return _error_page(request, 404, "Dispatch not found.")
    try:
        res = vendor_return.poll_leg(session, leg, actor=_actor_email(request), resume="async")
    except Exception as exc:
        return RedirectResponse(f"/vendors/{vid}?err=" + quote(str(exc)), status_code=303)
    return RedirectResponse(
        f"/vendors/{vid}?saved=1&polled={'return' if res else 'nothing'}", status_code=303
    )


# --- Wave E: quote-approval gate (R3) + reject/rework (R4) -------------------
def _leg_of_vendor_or_404(request, session, vid, did):
    """Resolve (vendor, leg) with the admin gate, the org-scoped vendor wall, and the leg
    pinned to THIS vendor (a foreign leg id 404s). Returns (v, leg, None) or (None, None,
    response)."""
    blocked = _flag_off_block(request)
    if blocked:
        return None, None, blocked
    _me, blocked = _admin_block(request)
    if blocked:
        return None, None, blocked
    v, blocked = _vendor_or_404(request, session, vid)
    if blocked:
        return None, None, blocked
    from app.models import VendorDispatch

    leg = session.get(VendorDispatch, did)
    if leg is None or leg.vendor_id != v.id or _leg_foreign_org(request, leg):
        return None, None, _error_page(request, 404, "Dispatch not found.")
    return v, leg, None


@router.post("/vendors/{vid}/dispatches/{did}/quote")
async def vendor_dispatch_quote(
    vid: int, did: int, request: Request, session: Session = Depends(get_session)
):
    """Record a QUOTE on a quote-gated leg (Wave E R3). Moves no money; approval is separate."""
    _v, leg, blocked = _leg_of_vendor_or_404(request, session, vid, did)
    if blocked:
        return blocked
    from app.kernel import vendor_quote

    form = await request.form()
    try:
        vendor_quote.record_quote(
            session,
            leg,
            quoted_cost_usd=form.get("quoted_cost_usd"),
            currency=str(form.get("currency") or ""),
            note=str(form.get("note") or ""),
            org_id=_my_org_id(request),
            actor=_actor_email(request),
            source="manual",
        )
    except vendor_quote.VendorQuoteError as exc:
        return RedirectResponse(f"/vendors/{vid}?err=" + quote(str(exc)), status_code=303)
    return RedirectResponse(f"/vendors/{vid}?saved=1", status_code=303)


@router.post("/vendors/{vid}/dispatches/{did}/approve-quote")
async def vendor_dispatch_approve_quote(
    vid: int, did: int, request: Request, session: Session = Depends(get_session)
):
    """APPROVE a quote (Wave E R3, a spend decision): work proceeds; notify the vendor."""
    _v, leg, blocked = _leg_of_vendor_or_404(request, session, vid, did)
    if blocked:
        return blocked
    from app.kernel import vendor_quote

    form = await request.form()
    amount = form.get("quoted_cost_usd")
    try:
        vendor_quote.approve_quote(
            session,
            leg,
            quoted_cost_usd=amount if amount not in (None, "") else None,
            currency=str(form.get("currency") or ""),
            note=str(form.get("note") or ""),
            org_id=_my_org_id(request),
            actor=_actor_email(request),
        )
    except vendor_quote.VendorQuoteError as exc:
        return RedirectResponse(f"/vendors/{vid}?err=" + quote(str(exc)), status_code=303)
    return RedirectResponse(f"/vendors/{vid}?saved=1", status_code=303)


@router.post("/vendors/{vid}/dispatches/{did}/decline-quote")
async def vendor_dispatch_decline_quote(
    vid: int, did: int, request: Request, session: Session = Depends(get_session)
):
    """DECLINE a quote (Wave E R3): escalate the parked run to a human_review pause."""
    _v, leg, blocked = _leg_of_vendor_or_404(request, session, vid, did)
    if blocked:
        return blocked
    from app.kernel import vendor_quote

    form = await request.form()
    try:
        vendor_quote.decline_quote(
            session,
            leg,
            reason=str(form.get("reason") or ""),
            org_id=_my_org_id(request),
            actor=_actor_email(request),
        )
    except vendor_quote.VendorQuoteError as exc:
        return RedirectResponse(f"/vendors/{vid}?err=" + quote(str(exc)), status_code=303)
    return RedirectResponse(f"/vendors/{vid}?saved=1", status_code=303)


@router.post("/vendors/{vid}/dispatches/{did}/reject-rework")
async def vendor_dispatch_reject_rework(
    vid: int, did: int, request: Request, session: Session = Depends(get_session)
):
    """REJECT a delivered leg and re-dispatch a rework to the same vendor (Wave E R4)."""
    _v, leg, blocked = _leg_of_vendor_or_404(request, session, vid, did)
    if blocked:
        return blocked
    from app.kernel import vendor_rework

    form = await request.form()
    try:
        vendor_rework.reject_and_rework(
            session,
            leg,
            note=str(form.get("note") or ""),
            org_id=_my_org_id(request),
            actor=_actor_email(request),
        )
    except vendor_rework.VendorReworkError as exc:
        return RedirectResponse(f"/vendors/{vid}?err=" + quote(str(exc)), status_code=303)
    return RedirectResponse(f"/vendors/{vid}?saved=1", status_code=303)


# --- Vendor Studio SSO handoff (VP-0 / SSO train) ----------------------------
def _sso_user(request) -> str:
    """The display identity minted into the handoff token: the operator's email, else a
    username/name, else a generic label (never blank)."""
    email = _actor_email(request)
    if email:
        return email
    me = security.session_user(request)
    return getattr(me, "username", None) or getattr(me, "name", None) or "operator"


@router.post("/vendor-studio/open")
def vendor_studio_open(request: Request):
    """Mint a short-lived SSO handoff token and hand the operator off to Vendor Studio via
    an auto-submitting POST FORM (the token rides the request BODY, never a query string —
    a token in a URL lands in access logs / referrers / history).

    Admin-only (_admin_block). When the handoff is unconfigured (either the URL or the
    shared secret is unset) the route 404s — non-enumerable, exactly like the hidden
    button. The token is `b64url(payload).hex(hmac)` per Vendor Studio's README, valid for
    60s; Vendor Studio verifies it constant-time against the shared secret."""
    blocked = _flag_off_block(request)
    if blocked:
        return blocked
    _me, blocked = _admin_block(request)
    if blocked:
        return blocked
    if not vendor_studio_sso.is_configured():
        return _error_page(request, 404, "Vendor Studio is not configured.")
    from app.web.security_headers import allow_form_action

    studio_url = vendor_studio_sso.studio_url()
    try:
        allow_form_action(request, studio_url)
    except ValueError:
        return _error_page(request, 404, "Vendor Studio is not configured.")
    me = security.session_user(request)
    # The role is the operator's real role, signed into the token (#557). Org scope rides
    # along so a multi-org Vendor Studio can pin itself to one org.
    role = str(getattr(me, "role", "") or "").strip()
    if not role:  # fail closed: never invent a role for the signed claim
        return _error_page(
            request, 403, f"Vendor Studio sign-in needs a {branding.brand_name()} role."
        )
    token = vendor_studio_sso.mint_token(
        _sso_user(request),
        role=role,
        org_id=access.current_org_id(me),
        multi_org=access.multi_org_active(),
    )
    action = html.escape(studio_url + "/sso", quote=True)
    tok = html.escape(token, quote=True)
    body = (
        '<!doctype html><html><head><meta charset="utf-8">'
        "<title>Opening Vendor Studio…</title></head>"
        '<body style="font-family:system-ui,sans-serif;margin:3rem;">'
        f'<form id="sso" method="post" action="{action}">'
        f'<input type="hidden" name="token" value="{tok}">'
        '<noscript><button type="submit">Continue to Vendor Studio</button></noscript>'
        "</form>"
        "<p>Opening Vendor Studio… "
        '<button type="submit" form="sso">Continue</button></p>'
        f'<script nonce="{request.state.csp_nonce}">'
        "document.getElementById('sso').submit();</script>"
        "</body></html>"
    )
    return HTMLResponse(body)


# --- archive / unarchive -----------------------------------------------------
@router.post("/vendors/{vid}/archive")
def vendor_archive(vid: int, request: Request, session: Session = Depends(get_session)):
    blocked = _flag_off_block(request)
    if blocked:
        return blocked
    _me, blocked = _admin_block(request)
    if blocked:
        return blocked
    _v, blocked = _vendor_or_404(request, session, vid)
    if blocked:
        return blocked
    try:
        vendors.archive(
            session, vid, org_id=_my_org_id(request), actor=_actor_email(request), ip=_ip(request)
        )
    except vendors.VendorError as exc:
        return RedirectResponse(f"/vendors/{vid}?err=" + quote(str(exc)), status_code=303)
    return RedirectResponse(f"/vendors/{vid}?saved=1", status_code=303)


@router.post("/vendors/{vid}/unarchive")
def vendor_unarchive(vid: int, request: Request, session: Session = Depends(get_session)):
    blocked = _flag_off_block(request)
    if blocked:
        return blocked
    _me, blocked = _admin_block(request)
    if blocked:
        return blocked
    _v, blocked = _vendor_or_404(request, session, vid)
    if blocked:
        return blocked
    try:
        vendors.unarchive(
            session, vid, org_id=_my_org_id(request), actor=_actor_email(request), ip=_ip(request)
        )
    except vendors.VendorError as exc:
        return RedirectResponse(f"/vendors/{vid}?err=" + quote(str(exc)), status_code=303)
    return RedirectResponse(f"/vendors/{vid}?saved=1", status_code=303)


# --- fail-closed delete ------------------------------------------------------
@router.post("/vendors/{vid}/delete")
def vendor_delete(vid: int, request: Request, session: Session = Depends(get_session)):
    blocked = _flag_off_block(request)
    if blocked:
        return blocked
    _me, blocked = _admin_block(request)
    if blocked:
        return blocked
    _v, blocked = _vendor_or_404(request, session, vid)
    if blocked:
        return blocked
    try:
        vendors.delete(
            session, vid, org_id=_my_org_id(request), actor=_actor_email(request), ip=_ip(request)
        )
    except vendors.VendorDeleteBlocked as exc:
        return RedirectResponse(f"/vendors/{vid}?err=" + quote(str(exc)), status_code=303)
    except vendors.VendorError as exc:
        return RedirectResponse(f"/vendors/{vid}?err=" + quote(str(exc)), status_code=303)
    return RedirectResponse("/vendors?saved=1", status_code=303)
