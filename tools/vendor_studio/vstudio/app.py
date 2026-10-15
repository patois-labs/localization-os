"""Vendor Studio — the FastAPI app (PM inbox, CAT editor, QA engine, delivery).

Wire-only isolated: this package NEVER imports app/ or integrations/. Auth reuses Loc
Studio identity via a wire-level SSO handoff (no code/cookie sharing).
"""

from __future__ import annotations

import json
import os
import time

import httpx
from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import (
    HTMLResponse,
    JSONResponse,
    RedirectResponse,
    Response,
)
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlmodel import Session, select

from vstudio import advisor, delivery, editor, intake, qa, qa_engine, security, xliff
from vstudio import contract as C
from vstudio.config import settings
from vstudio.db import get_session, init_db
from vstudio.models import (
    VAdvisorEvent,
    VProject,
    VProjectStatus,
    VSegment,
    VSegStatus,
    WireLog,
)

_HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_TEMPLATES = Jinja2Templates(directory=os.path.join(_HERE, "templates"))
_STATIC = os.path.join(_HERE, "static")

# M2: no interactive API docs / OpenAPI schema — this app has no API consumers that need
# them, and the schema would enumerate every route to an unauthenticated caller.
app = FastAPI(title="Vendor Studio", docs_url=None, redoc_url=None, openapi_url=None)
app.mount("/static", StaticFiles(directory=_STATIC), name="static")

# M4: a shared cap on JSON request bodies for the app routes (intake streams its own,
# larger cap). 256 KiB is ample for a segment edit / QA action / advice request.
_MAX_JSON_BODY = 256 * 1024


@app.on_event("startup")
def _startup() -> None:
    # M1/M7: refuse to serve a non-loopback bind without an explicit session secret + a
    # https Localization OS URL (a real deployment must be configured, not defaulted).
    settings.assert_production_ready()
    init_db()


# M8: security headers on every response. CSP forbids inline SCRIPT (all JS lives in
# /static/*.js); inline style attributes (progress-bar widths) are allowed. HSTS only
# when cookies are Secure (i.e. served over https).
@app.middleware("http")
async def _security_headers(request: Request, call_next):
    resp = await call_next(request)
    resp.headers["Content-Security-Policy"] = (
        "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
        "img-src 'self' data:; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
    )
    resp.headers["X-Frame-Options"] = "DENY"
    resp.headers["X-Content-Type-Options"] = "nosniff"
    resp.headers["Referrer-Policy"] = "no-referrer"
    if settings.cookie_secure:
        resp.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
    return resp


# ---------------------------------------------------------------------------
# session / auth
# ---------------------------------------------------------------------------
def _session_of(request: Request) -> dict | None:
    return security.decode_session(request.cookies.get(security.COOKIE_NAME, ""))


def require_session(request: Request) -> dict:
    """Every app page requires the session. Unauthenticated = a minimal 404-style wall
    that leaks no app detail (no login redirect on an API call)."""
    sess = _session_of(request)
    if not sess or not sess.get("authed"):
        raise HTTPException(status_code=404, detail="Not found")
    return sess


def require_role(request: Request, *roles: str) -> dict:
    """require_session plus a role gate (#516). The role comes ONLY from the signed session
    cookie, which login derives from the Localization OS key's real role; fail closed with
    403 for any other role."""
    sess = require_session(request)
    if sess.get("role") not in roles:
        raise HTTPException(status_code=403, detail="Forbidden")
    return sess


#: Localization OS role -> Vendor Studio persona (#516): one table for the API-key path and
#: the SSO claim, defined in security.py.
_LOC_ROLE_TO_STUDIO = security.LOC_ROLE_TO_STUDIO


def _set_session(resp: Response, data: dict) -> None:
    # M1: HttpOnly + SameSite=Lax + Secure (env-overridable for loopback dev) + a browser
    # max-age matching the timed signer's max_age.
    resp.set_cookie(
        security.COOKIE_NAME,
        security.encode_session(data),
        httponly=True,
        samesite="lax",
        secure=settings.cookie_secure,
        path="/",
        max_age=settings.session_max_age,
    )


# ---------------------------------------------------------------------------
# health (unauthenticated, no DB work)
# ---------------------------------------------------------------------------
@app.get("/health")
def health() -> JSONResponse:
    return JSONResponse({"ok": True, "app": "vendor-studio"})


# ---------------------------------------------------------------------------
# SSO handoff + fallback login
# ---------------------------------------------------------------------------
# L1: the SSO handoff is a POST (token in the request BODY, not the URL query string, so
# it never lands in access logs / referrers / browser history). GET /sso just shows the
# login page. The Localization OS mint train submits a form POST to /sso (README spec).
@app.post("/sso")
def sso(request: Request, token: str = Form("")):
    fail = HTMLResponse(_render_login(request, error="Single sign-on failed."), status_code=403)
    # Login-CSRF gate (#557): the browser-supplied Origin must be this Studio or the
    # configured Localization OS. Checked before the token so a refused request burns nothing.
    own = f"{request.url.scheme}://{request.headers.get('host', '')}"
    if not security.sso_origin_ok(
        request.headers.get("origin"), request.headers.get("sec-fetch-site"), own
    ):
        return fail
    claims = security.verify_sso_claims(token)
    # Do not reveal whether the token was expired/malformed/replayed/etc. The role comes ONLY
    # from the signed claim; no claim, no session. The jti is consumed last: a token is single
    # use, and only a token that would otherwise have signed the user in is spent.
    if not claims or not security.consume_sso_jti(claims["jti"], claims["exp"]):
        return fail
    user = claims["user"]
    resp = RedirectResponse(url="/", status_code=303)
    _set_session(resp, {"authed": True, "user": user, "name": user, "role": claims["role"]})
    return resp


@app.get("/sso", response_class=HTMLResponse)
def sso_get(request: Request):
    # A token must arrive by POST; a bare GET just shows the login wall.
    return HTMLResponse(_render_login(request))


@app.get("/login", response_class=HTMLResponse)
def login_page(request: Request):
    return HTMLResponse(_render_login(request))


#: M7: per-IP rate limit on the API-key verification endpoint (a brute-force / oracle
#: perimeter). Process-local, best-effort. This (like intake._UNAUTH_BUCKET) is correct
#: only because Vendor Studio runs as a SINGLE process (one replica, no autoscale). A
#: multi-process/multi-machine deploy would need a shared store; the single-process
#: constraint is load-bearing here.
_APIKEY_MAX_PER_MINUTE = 5
_APIKEY_BUCKET: dict = {}


def _apikey_rate_ok(ip: str) -> bool:
    minute = int(time.time() // 60)
    for k in [k for k in _APIKEY_BUCKET if k[1] < minute - 1]:
        _APIKEY_BUCKET.pop(k, None)
    key = (ip or "?", minute)
    n = _APIKEY_BUCKET.get(key, 0)
    if n >= _APIKEY_MAX_PER_MINUTE:
        return False
    _APIKEY_BUCKET[key] = n + 1
    return True


@app.post("/login/apikey")
async def login_apikey(request: Request, api_key: str = Form("")):
    """Fallback login: verify a Localization OS API key by calling /api over the wire. The key
    is NEVER logged (not to WireLog, not to the app log). Rate-limited per IP."""
    ip = request.client.host if request.client else "?"
    if not _apikey_rate_ok(ip):
        return HTMLResponse(
            _render_login(request, error="Too many attempts; wait a minute."), status_code=429
        )
    api_key = (api_key or "").strip()
    studio_role = None
    if api_key and settings.locstudio_url:
        try:
            with httpx.Client(timeout=15.0) as client:
                # Localization OS's /api authenticates ONLY via the X-Loc-Token header
                # (app/web/deps.py) — a Bearer Authorization header is ignored, so the
                # fallback could never succeed. The key is still NEVER logged.
                # /api/account returns the key owner's REAL role (#516): the Studio role
                # is bound to it, never self-assigned.
                r = client.get(
                    f"{settings.locstudio_url}/api/account", headers={"X-Loc-Token": api_key}
                )
            if 200 <= r.status_code < 300:
                body = r.json()
                if isinstance(body, dict) and body.get("ok") is not False:
                    role = body.get("role")
                    # Org scope (#557): /api/account adds org_id + multi_org only when the
                    # install is multi-org. A pinned VSTUDIO_ORG_ID must match; a multi-org
                    # install with no pin is refused; a build that omits org_id cannot satisfy
                    # a pin.
                    if isinstance(role, str) and security.org_scope_ok(
                        body.get("org_id"), bool(body.get("multi_org"))
                    ):
                        studio_role = _LOC_ROLE_TO_STUDIO.get(role)
        except (httpx.HTTPError, ValueError):
            studio_role = None
    if not studio_role:
        return HTMLResponse(
            _render_login(request, error="Could not verify that Localization OS API key."),
            status_code=403,
        )
    resp = RedirectResponse(url="/", status_code=303)
    _set_session(
        resp,
        {"authed": True, "user": "api-key user", "name": "api-key user", "role": studio_role},
    )
    return resp


def _safe_local_redirect(request: Request) -> str:
    """The Referer, but only as a same-origin PATH (no open redirect, #516)."""
    from urllib.parse import urlsplit

    ref = request.headers.get("referer") or ""
    try:
        parts = urlsplit(ref)
    except ValueError:
        return "/"
    host = request.headers.get("host") or ""
    if parts.scheme and parts.netloc != host:
        return "/"
    path = parts.path or "/"
    if not path.startswith("/") or path.startswith("//") or "\\" in path:
        return "/"
    return path + (f"?{parts.query}" if parts.query else "")


@app.post("/persona")
def set_persona(request: Request, role: str = Form("PM"), name: str = Form("")):
    """Persona switch (demo/dev convenience). A session may only move WITHIN what it was
    granted at login (#516): the entitlement is the signed `granted` field (first switch
    records the login role). Only a PM session may switch to another persona; every other
    session is pinned to its own role, so a self-assigned role in the request is ignored."""
    sess = require_session(request)
    granted = sess.get("granted") or sess.get("role")
    if role not in ("PM", "Translator", "Reviewer") or granted != "PM":
        role = sess.get("role") if granted != "PM" else "PM"
    sess = {
        **sess,
        "role": role,
        "granted": granted,
        "name": (name or "").strip() or sess.get("user", "user"),
    }
    resp = RedirectResponse(url=_safe_local_redirect(request), status_code=303)
    _set_session(resp, sess)
    return resp


# L2: logout is a POST (a GET link is CSRF-triggerable / prefetchable). GET shows a small
# confirmation form.
@app.post("/logout")
def logout():
    resp = RedirectResponse(url="/login", status_code=303)
    resp.delete_cookie(security.COOKIE_NAME, path="/")
    return resp


@app.get("/logout", response_class=HTMLResponse)
def logout_confirm(request: Request):
    return HTMLResponse(
        "<!doctype html><meta charset=utf-8>"
        "<link rel=stylesheet href=/static/style.css>"
        "<main class=login><h1>Log out?</h1>"
        "<form method=post action=/logout><button type=submit>Log out</button></form>"
        "<p><a href=/>Cancel</a></p></main>"
    )


def _render_login(request: Request, error: str = "") -> str:
    return _TEMPLATES.get_template("login.html").render(
        request=request,
        error=error,
        sso_enabled=bool(settings.sso_secret),
        locstudio_url=settings.locstudio_url,
    )


# ---------------------------------------------------------------------------
# INTAKE (wire-authenticated, no session)
# ---------------------------------------------------------------------------
_MAX_BODY = 8 * 1024 * 1024


@app.post("/intake/dispatch")
async def intake_dispatch(request: Request):
    """Verify HMAC over the RAW body BEFORE any deserialization, then store the project.
    An unsigned / mis-signed dispatch is 401 with a visible WireLog row."""
    # Body-size cap first, on the streamed request.
    chunks, total = [], 0
    async for piece in request.stream():
        total += len(piece)
        if total > _MAX_BODY:
            return JSONResponse({"ok": False, "error": "payload too large"}, status_code=413)
        chunks.append(piece)
    body = b"".join(chunks)
    sig = request.headers.get(C.SIGNATURE_HEADER, "")
    src_ip = request.client.host if request.client else "?"
    with get_session() as s:
        if not security.verify_signature(settings.signing_secret, body, sig):
            # H2: an UNAUTHENTICATED reject — coalesced per source per minute so a
            # signature-flood cannot bloat the WireLog table.
            intake.log_wire(
                s,
                direction="in",
                kind="dispatch",
                verdict="rejected",
                reason="bad or missing signature",
                status_code=401,
                rate_source=src_ip,
            )
            return JSONResponse({"ok": False, "error": "invalid signature"}, status_code=401)
        try:
            payload = json.loads(body.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            intake.log_wire(
                s,
                direction="in",
                kind="dispatch",
                verdict="rejected",
                reason="unparseable body (post-signature)",
                status_code=400,
                rate_source=src_ip,
            )
            return JSONResponse({"ok": False, "error": "bad JSON"}, status_code=400)
        try:
            proj = intake.store_dispatch(s, payload)
        except intake.IntakeConflict as exc:
            # L4: same claim token, different payload — a conflict, logged (authenticated).
            intake.log_wire(
                s,
                direction="in",
                kind="dispatch",
                verdict="conflict",
                reason=str(exc)[:200],
                status_code=409,
            )
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=409)
        except intake.IntakeError as exc:
            intake.log_wire(
                s,
                direction="in",
                kind="dispatch",
                verdict="rejected",
                reason=str(exc)[:200],
                status_code=400,
            )
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
        intake.log_wire(
            s,
            direction="in",
            kind="dispatch",
            verdict="accepted",
            reason=f"{len(payload.get('segments') or [])} segment(s)",
            status_code=200,
            project_id=proj.id,
        )
        return JSONResponse(
            {"ok": True, "project_id": proj.id, "status": proj.status}, status_code=200
        )


@app.post("/intake/quote-decision")
async def intake_quote_decision(request: Request):
    """Vendor Wave E / R3 — receive Localization OS's signed quote decision for a quote-gated
    project. Same verify-before-parse posture as intake_dispatch: HMAC over the RAW body is
    checked BEFORE any deserialization, and a bad/missing signature is 401 with a WireLog row.

    Body: {"type":"quote_decision","message_version":"1.0","claim_token":"...",
           "decision":"approved"|"declined","quoted_cost_usd":<n>,"quote_currency":"USD",
           "reason":"<optional>"}. On `approved` the project's quote clears so the PM may
    start work; on `declined` the project is CANCELLED."""
    chunks, total = [], 0
    async for piece in request.stream():
        total += len(piece)
        if total > _MAX_BODY:
            return JSONResponse({"ok": False, "error": "payload too large"}, status_code=413)
        chunks.append(piece)
    body = b"".join(chunks)
    sig = request.headers.get(C.SIGNATURE_HEADER, "")
    src_ip = request.client.host if request.client else "?"
    with get_session() as s:
        if not security.verify_signature(settings.signing_secret, body, sig):
            intake.log_wire(
                s,
                direction="in",
                kind="quote_decision",
                verdict="rejected",
                reason="bad or missing signature",
                status_code=401,
                rate_source=src_ip,
            )
            return JSONResponse({"ok": False, "error": "invalid signature"}, status_code=401)
        try:
            payload = json.loads(body.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            intake.log_wire(
                s,
                direction="in",
                kind="quote_decision",
                verdict="rejected",
                reason="unparseable body (post-signature)",
                status_code=400,
                rate_source=src_ip,
            )
            return JSONResponse({"ok": False, "error": "bad JSON"}, status_code=400)
        claim = str((payload or {}).get("claim_token") or "").strip()
        decision = str((payload or {}).get("decision") or "").strip().lower()
        if not claim or decision not in ("approved", "declined"):
            intake.log_wire(
                s,
                direction="in",
                kind="quote_decision",
                verdict="rejected",
                reason="missing claim_token or invalid decision",
                status_code=400,
            )
            return JSONResponse({"ok": False, "error": "invalid quote decision"}, status_code=400)
        proj = s.exec(select(VProject).where(VProject.claim_token == claim)).first()
        if proj is None:
            # An authenticated (signed) caller may know the token exists elsewhere; there is
            # no oracle risk here, so we answer 404 plainly.
            intake.log_wire(
                s,
                direction="in",
                kind="quote_decision",
                verdict="unknown",
                reason="no project for claim token",
                status_code=404,
            )
            return JSONResponse({"ok": False, "error": "unknown claim token"}, status_code=404)
        # Idempotency / ordering guard (finding #14): once a TERMINAL quote decision is
        # recorded, a later (replayed or out-of-order) decision is a NO-OP. Without this a
        # replayed 'declined' arriving AFTER an 'approved' would cancel an active project.
        if proj.quote_status in ("approved", "declined"):
            intake.log_wire(
                s,
                direction="in",
                kind="quote_decision",
                verdict="duplicate",
                reason=f"quote already {proj.quote_status}; ignoring {decision}",
                status_code=200,
                project_id=proj.id,
            )
            return JSONResponse(
                {
                    "ok": True,
                    "project_id": proj.id,
                    "applied": False,
                    "quote_status": proj.quote_status,
                    "status": proj.status,
                },
                status_code=200,
            )
        reason = str((payload or {}).get("reason") or "").strip()
        if decision == "approved":
            proj.quote_status = "approved"
            qc = _num_or_none((payload or {}).get("quoted_cost_usd"))
            if qc is not None:
                proj.quoted_cost_usd = qc
            s.add(proj)
            s.commit()
        else:  # declined
            proj.quote_status = "declined"
            if reason:
                proj.cancel_reason = reason[:500]
            try:
                intake.set_status(s, proj, VProjectStatus.CANCELLED)
            except ValueError:
                # Already terminal (e.g. re-delivered decision) — keep the quote_status flip.
                s.add(proj)
                s.commit()
        intake.log_wire(
            s,
            direction="in",
            kind="quote_decision",
            verdict=decision,
            reason=f"quote {decision}",
            status_code=200,
            project_id=proj.id,
        )
        return JSONResponse(
            {
                "ok": True,
                "project_id": proj.id,
                "quote_status": proj.quote_status,
                "status": proj.status,
            },
            status_code=200,
        )


@app.get("/intake/poll")
def intake_poll(request: Request):
    """Poll target compatible with vendor_return.poll_leg: the claim token rides the
    X-LocStudio-Claim header. Serves the signed return of a project that is READY
    (COMPLETED or already DELIVERED), else 204.

    M6: an unknown claim token answers 204 (NOT 404) — same-path-every-failure, so the
    endpoint is not an existence oracle. A COMPLETED project's return is served and the
    project is flipped DELIVERED on the FIRST successful serve (this is the entire point of
    the poll transport for a no-egress vendor). The 204 no-op is NOT logged (H2)."""
    claim = request.headers.get(C.CLAIM_HEADER, "")
    if not claim:
        return Response(status_code=204)
    with get_session() as s:
        proj = s.exec(select(VProject).where(VProject.claim_token == claim)).first()
        if proj is None:
            return Response(status_code=204)  # M6(a): no existence oracle
        if proj.status not in (VProjectStatus.COMPLETED, VProjectStatus.DELIVERED):
            return Response(status_code=204)  # nothing ready (not logged — H2)
        if proj.status == VProjectStatus.COMPLETED:
            # #522 quote gate, defense in depth: a COMPLETED project whose quote is not
            # approved (e.g. completed before the gate existed) is not served.
            try:
                delivery.require_quote_cleared(proj)
            except ValueError:
                return Response(status_code=204)
        ids = [sg.contract_id for sg in delivery.project_segments(s, proj)]
        payload = delivery.build_return_payload(s, proj, ids, cost=proj.actual_cost_usd)
        raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        # M6(b): mark DELIVERED on the first serve of a COMPLETED project (the poll IS the
        # delivery for a pull-model vendor). Idempotent: a re-poll of a DELIVERED project
        # serves the same bytes.
        if proj.status == VProjectStatus.COMPLETED:
            proj.delivered_ids = list(ids)
            proj.status = VProjectStatus.DELIVERED
            from datetime import datetime

            proj.delivered_at = datetime.utcnow()
            s.add(proj)
            s.commit()
        intake.log_wire(
            s, direction="in", kind="poll", verdict="served", status_code=200, project_id=proj.id
        )
        return Response(
            content=raw,
            media_type="application/json",
            headers={C.SIGNATURE_HEADER: security.compute_signature(settings.signing_secret, raw)},
        )


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
def _get_project(s: Session, pid: int) -> VProject:
    proj = s.get(VProject, pid)
    if proj is None:
        raise HTTPException(status_code=404, detail="Not found")
    return proj


def _get_segment(s: Session, pid: int, sid: int) -> VSegment:
    seg = s.get(VSegment, sid)
    if seg is None or seg.project_id != pid:
        raise HTTPException(status_code=404, detail="Not found")
    return seg


def _project_public(proj: VProject) -> dict:
    """A project view that NEVER carries the claim token."""
    return {
        "id": proj.id,
        "customer_label": proj.customer_label,
        "source_locale": proj.source_locale,
        "target_locale": proj.target_locale,
        "status": proj.status,
        "run_ref": proj.run_ref,
        "step_name": proj.step_name,
        "actual_cost_usd": proj.actual_cost_usd,
        "actual_currency": proj.actual_currency,
        "expiry_at": proj.expiry_at.isoformat() if proj.expiry_at else None,
        "estimate": proj.estimate,
    }


# ---------------------------------------------------------------------------
# PM inbox
# ---------------------------------------------------------------------------
@app.get("/", response_class=HTMLResponse)
def inbox(request: Request):
    sess = require_session(request)
    with get_session() as s:
        projs = list(s.exec(select(VProject).order_by(VProject.created_at.desc())).all())
        rows = []
        for p in projs:
            segs = delivery.project_segments(s, p)
            rows.append({"p": p, "progress": editor.progress(s, p), "seg_count": len(segs)})
    return _TEMPLATES.TemplateResponse(
        request,
        "inbox.html",
        {"request": request, "session": sess, "rows": rows, "STATUS": VProjectStatus},
    )


@app.get("/projects/{pid}", response_class=HTMLResponse)
def project_view(request: Request, pid: int):
    sess = require_session(request)
    with get_session() as s:
        proj = _get_project(s, pid)
        segs = delivery.project_segments(s, proj)
        bands = intake.band_breakdown(segs)
        prog = editor.progress(s, proj)
        outstanding = delivery.outstanding_ids(s, proj)
    return _TEMPLATES.TemplateResponse(
        request,
        "project.html",
        {
            "request": request,
            "session": sess,
            "proj": proj,
            "bands": bands,
            "progress": prog,
            "seg_count": len(segs),
            "outstanding": len(outstanding),
            "STATUS": VProjectStatus,
        },
    )


@app.post("/projects/{pid}/claim")
def claim_project(request: Request, pid: int):
    sess = require_session(request)
    with get_session() as s:
        proj = _get_project(s, pid)
        try:
            delivery.require_quote_cleared(proj)
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        if proj.status == VProjectStatus.NEW:
            intake.set_status(s, proj, VProjectStatus.CLAIMED)
        proj.claimed_by = sess.get("name") or sess.get("user") or "translator"
        if proj.status in (VProjectStatus.CLAIMED,):
            intake.set_status(s, proj, VProjectStatus.IN_PROGRESS)
        s.add(proj)
        s.commit()
    return RedirectResponse(url=f"/projects/{pid}/editor", status_code=303)


@app.post("/projects/{pid}/assign")
def assign_project(request: Request, pid: int, assignee: str = Form("")):
    require_role(request, "PM")
    with get_session() as s:
        proj = _get_project(s, pid)
        proj.assigned_to = (assignee or "").strip()
        if proj.status == VProjectStatus.NEW:
            intake.set_status(s, proj, VProjectStatus.CLAIMED)
        s.add(proj)
        s.commit()
    return RedirectResponse(url=f"/projects/{pid}", status_code=303)


@app.post("/projects/{pid}/cost")
def set_cost(request: Request, pid: int, actual_cost_usd: str = Form("")):
    require_role(request, "PM")
    with get_session() as s:
        proj = _get_project(s, pid)
        try:
            proj.actual_cost_usd = float(actual_cost_usd) if actual_cost_usd else None
        except ValueError as e:
            raise HTTPException(status_code=400, detail="cost must be a number") from e
        s.add(proj)
        s.commit()
    return RedirectResponse(url=f"/projects/{pid}", status_code=303)


@app.post("/projects/{pid}/quote")
def submit_quote_route(
    request: Request, pid: int, quoted_cost_usd: str = Form(""), note: str = Form("")
):
    """Vendor Wave E / R3 — the PM submits a quote for a quote-gated project. Signs + POSTs
    it to Localization OS's quote webhook and marks quote_status "submitted". Only applies to a
    quote-gated project that is not already approved (delivery.submit_quote enforces this)."""
    require_role(request, "PM")
    with get_session() as s:
        proj = _get_project(s, pid)
        try:
            amount = float(quoted_cost_usd) if quoted_cost_usd else None
        except ValueError as e:
            raise HTTPException(status_code=400, detail="quote must be a number") from e
        if amount is None:
            raise HTTPException(status_code=400, detail="a quote amount is required")
        try:
            delivery.submit_quote(s, proj, amount, note=(note or "").strip())
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except httpx.HTTPError as e:
            raise HTTPException(status_code=502, detail="could not reach Localization OS") from e
    return RedirectResponse(url=f"/projects/{pid}", status_code=303)


@app.post("/projects/{pid}/cancel")
def cancel_project(request: Request, pid: int, reason: str = Form("")):
    require_role(request, "PM")
    with get_session() as s:
        proj = _get_project(s, pid)
        proj.cancel_reason = (reason or "").strip()
        try:
            intake.set_status(s, proj, VProjectStatus.CANCELLED)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        s.add(proj)
        s.commit()
    return RedirectResponse(url=f"/projects/{pid}", status_code=303)


# ---------------------------------------------------------------------------
# CAT editor
# ---------------------------------------------------------------------------
@app.get("/projects/{pid}/editor", response_class=HTMLResponse)
def editor_view(request: Request, pid: int):
    sess = require_session(request)
    with get_session() as s:
        proj = _get_project(s, pid)
        segs = delivery.project_segments(s, proj)
        findings = qa_engine.run_findings(s, proj)
        seg_views = []
        for seg in segs:
            seg_views.append(
                {
                    "seg": seg,
                    # Wave 3 Train E (§7.2/§7.5): buyer queries render above the persona's own
                    # comment list, marked as coming from the buyer, each with a reply affordance.
                    "queries_in": editor.inbound_queries_for(s, seg),
                    "comments": editor.local_comments_for(s, seg),
                    "findings": findings.get(seg.id, []),
                    "badge": _provenance_badge(seg),
                    "feedback": seg.feedback,
                }
            )
        gate = qa_engine.gate(s, proj)
        prog = editor.progress(s, proj)
    return _TEMPLATES.TemplateResponse(
        request,
        "editor.html",
        {
            "request": request,
            "session": sess,
            "proj": proj,
            "segs": seg_views,
            "gate": gate,
            "progress": prog,
            "glossary": proj.glossary,
            "STATUS": VProjectStatus,
            "SEG": VSegStatus,
        },
    )


def _provenance_badge(seg: VSegment) -> dict:
    if seg.locked:
        return {"label": "locked", "cls": "locked"}
    p = seg.provenance
    if p == "tm_exact":
        return {"label": "TM 100%", "cls": "tm"}
    if p == "tm_fuzzy":
        score = int(seg.match_score) if seg.match_score else 0
        return {"label": f"TM {score}%" if score else "TM fuzzy", "cls": "tmf"}
    if p == "mt":
        return {"label": "MT - post-edit", "cls": "mt"}
    if (seg.prefill or "").strip():
        return {"label": "prefilled", "cls": "pre"}
    return {"label": "new", "cls": "new"}


def _seg_json(seg: VSegment) -> dict:
    return {
        "id": seg.id,
        "contract_id": seg.contract_id,
        "status": seg.status,
        "target": seg.target,
        "locked": seg.locked,
        "issue_tag": seg.issue_tag,
        "confirmed_by": seg.confirmed_by,
        "badge": _provenance_badge(seg),
    }


@app.get("/projects/{pid}/segments")
def list_segments(request: Request, pid: int):
    """JSON list of a project's segments (never carries the claim token)."""
    require_session(request)
    with get_session() as s:
        proj = _get_project(s, pid)
        segs = delivery.project_segments(s, proj)
        return JSONResponse(
            {
                "project_id": proj.id,
                "status": proj.status,
                "segments": [
                    {
                        "id": x.id,
                        "contract_id": x.contract_id,
                        "source": x.source,
                        "target": x.target,
                        "status": x.status,
                        "locked": x.locked,
                        "badge": _provenance_badge(x),
                    }
                    for x in segs
                ],
            }
        )


@app.patch("/projects/{pid}/segments/{sid}")
async def patch_segment(request: Request, pid: int, sid: int):
    require_session(request)
    data = await _json_body(request)
    with get_session() as s:
        proj = _get_project(s, pid)
        seg = _get_segment(s, pid, sid)
        try:
            editor.patch_target(s, proj, seg, data.get("target", ""), actor=_actor(request))
        except editor.EditError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return JSONResponse(_seg_json(seg))


@app.post("/projects/{pid}/segments/{sid}/confirm")
def confirm_segment(request: Request, pid: int, sid: int):
    require_session(request)
    with get_session() as s:
        proj = _get_project(s, pid)
        seg = _get_segment(s, pid, sid)
        try:
            editor.confirm_segment(s, proj, seg, actor=_actor(request))
        except editor.EditError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return JSONResponse(_seg_json(seg))


@app.post("/projects/{pid}/segments/{sid}/copy-source")
def copy_source(request: Request, pid: int, sid: int):
    require_session(request)
    with get_session() as s:
        proj = _get_project(s, pid)
        seg = _get_segment(s, pid, sid)
        try:
            editor.copy_source(s, proj, seg, actor=_actor(request))
        except editor.EditError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return JSONResponse(_seg_json(seg))


@app.post("/projects/{pid}/segments/{sid}/approve")
def approve_segment(request: Request, pid: int, sid: int):
    require_role(request, "Reviewer")
    with get_session() as s:
        proj = _get_project(s, pid)
        seg = _get_segment(s, pid, sid)
        try:
            editor.reviewer_approve(s, proj, seg, actor=_actor(request))
        except editor.EditError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return JSONResponse(_seg_json(seg))


@app.post("/projects/{pid}/segments/{sid}/reject")
async def reject_segment(request: Request, pid: int, sid: int):
    require_role(request, "Reviewer")
    data = await _json_body(request)
    with get_session() as s:
        proj = _get_project(s, pid)
        seg = _get_segment(s, pid, sid)
        try:
            editor.reviewer_reject(s, proj, seg, data.get("issue_tag", ""), actor=_actor(request))
        except editor.EditError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return JSONResponse(_seg_json(seg))


@app.post("/projects/{pid}/segments/{sid}/comment")
async def comment_segment(request: Request, pid: int, sid: int):
    sess = require_session(request)
    data = await _json_body(request)
    with get_session() as s:
        proj = _get_project(s, pid)
        seg = _get_segment(s, pid, sid)
        try:
            # Wave 3 Train E (§7.5): `share` is the consent bit (default OFF) that lets the
            # comment cross back on the return; `external_ref` (a buyer thread id) makes it a
            # reply to that thread. add_comment guards on project status only (a LOCKED
            # segment stays commentable).
            c = editor.add_comment(
                s,
                proj,
                seg,
                author=sess.get("name", "user"),
                role=sess.get("role", "PM"),
                body=data.get("body", ""),
                share=bool(data.get("share")),
                external_ref=str(data.get("external_ref") or ""),
            )
        except editor.EditError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return JSONResponse(
            {
                "id": c.id,
                "author": c.author,
                "role": c.role,
                "body": c.body,
                "share": c.share,
                "external_ref": c.external_ref,
            }
        )


# ---------------------------------------------------------------------------
# QA
# ---------------------------------------------------------------------------
@app.get("/projects/{pid}/qa")
def qa_findings(request: Request, pid: int):
    require_session(request)
    with get_session() as s:
        proj = _get_project(s, pid)
        findings = qa_engine.run_findings(s, proj)
        gate = qa_engine.gate(s, proj)
        flat = [f for flist in findings.values() for f in flist]
    return JSONResponse({"findings": flat, "gate": gate})


@app.post("/projects/{pid}/qa/ignore")
async def qa_ignore(request: Request, pid: int):
    require_session(request)
    data = await _json_body(request)
    # M5: validate segment_id present + integer BEFORE touching the DB (422, not a 500).
    raw_sid = data.get("segment_id")
    try:
        seg_id = int(raw_sid)
    except (TypeError, ValueError) as e:
        raise HTTPException(
            status_code=422, detail="segment_id is required and must be an integer."
        ) from e
    with get_session() as s:
        proj = _get_project(s, pid)
        seg = s.get(VSegment, seg_id)
        if seg is None or seg.project_id != pid:
            raise HTTPException(
                status_code=422, detail="segment_id does not belong to this project."
            )
        try:
            qa_engine.ignore_finding(
                s,
                proj,
                seg_id,
                data.get("check_key", ""),
                data.get("detail_sig", ""),
                data.get("reason", ""),
            )
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
    return JSONResponse({"ok": True})


# M9: batch-advice guardrails — cap the fan-out, bound each live call, and cap total
# wall-clock; return partial results + a remaining count rather than hanging on a slow LLM.
_ADVICE_BATCH_CAP = 25
_ADVICE_PER_CALL_TIMEOUT = 5.0
_ADVICE_WALLCLOCK_BUDGET = 60.0


@app.post("/projects/{pid}/qa/advice")
async def qa_advice(request: Request, pid: int):
    require_session(request)
    data = await _json_body(request)
    sid = data.get("segment_id")
    with get_session() as s:
        proj = _get_project(s, pid)
        segs = [x for x in delivery.project_segments(s, proj) if not x.locked]
        if sid is not None:
            try:
                sid_i = int(sid)
            except (TypeError, ValueError) as e:
                raise HTTPException(status_code=422, detail="segment_id must be an integer") from e
            segs = [x for x in segs if x.id == sid_i]
        eligible = len(segs)
        batch = segs[:_ADVICE_BATCH_CAP]
        out, deadline = [], time.monotonic() + _ADVICE_WALLCLOCK_BUDGET
        done = 0
        for seg in batch:
            if time.monotonic() > deadline:
                break
            out.append(
                advisor.advise(
                    seg.id,
                    seg.source,
                    seg.target,
                    proj.glossary,
                    proj.target_locale,
                    timeout=_ADVICE_PER_CALL_TIMEOUT,
                )
            )
            done += 1
    remaining = eligible - done
    return JSONResponse(
        {"suggestions": out, "remaining": max(0, remaining), "capped": eligible > _ADVICE_BATCH_CAP}
    )


@app.post("/projects/{pid}/segments/{sid}/advice/accept")
async def advice_accept(request: Request, pid: int, sid: int):
    require_session(request)
    data = await _json_body(request)
    with get_session() as s:
        proj = _get_project(s, pid)
        seg = _get_segment(s, pid, sid)
        suggestion = data.get("suggested", "")
        try:
            editor.patch_target(s, proj, seg, suggestion, actor=_actor(request))
        except editor.EditError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        s.add(
            VAdvisorEvent(
                segment_id=seg.id,
                action="advisor_accepted",
                suggestion=suggestion,
                rationale=data.get("rationale", ""),
            )
        )
        s.commit()
        return JSONResponse(_seg_json(seg))


@app.post("/projects/{pid}/segments/{sid}/advice/ignore")
async def advice_ignore(request: Request, pid: int, sid: int):
    require_session(request)
    data = await _json_body(request)
    with get_session() as s:
        _get_project(s, pid)
        seg = _get_segment(s, pid, sid)
        s.add(
            VAdvisorEvent(
                segment_id=seg.id,
                action="advisor_ignored",
                suggestion=data.get("suggested", ""),
                rationale=data.get("rationale", ""),
            )
        )
        s.commit()
    return JSONResponse({"ok": True})


# ---------------------------------------------------------------------------
# QA settings
# ---------------------------------------------------------------------------
@app.get("/settings/qa", response_class=HTMLResponse)
def qa_settings_page(request: Request):
    sess = require_session(request)
    with get_session() as s:
        sev = qa_engine.severities(s)
    rows = [{"key": k, "severity": sev.get(k, "warning")} for k in qa.ALL_CHECKS]
    return _TEMPLATES.TemplateResponse(
        request, "qa_settings.html", {"request": request, "session": sess, "rows": rows}
    )


@app.post("/settings/qa")
def qa_settings_update(
    request: Request, check_key: str = Form(""), severity: str = Form("warning")
):
    require_role(request, "PM")
    with get_session() as s:
        try:
            qa_engine.set_severity(s, check_key, severity)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
    return RedirectResponse(url="/settings/qa", status_code=303)


# ---------------------------------------------------------------------------
# delivery
# ---------------------------------------------------------------------------
@app.post("/projects/{pid}/deliver")
async def deliver_full(request: Request, pid: int):
    require_role(request, "PM")
    data = await _json_body(request, allow_empty=True)
    cost = _num_or_none(data.get("cost"))
    with get_session() as s:
        proj = _get_project(s, pid)
        # QA is advisory-only: delivery NEVER refuses on findings (decided 2026-08-08). The
        # only 409 here is a structural refusal (nothing outstanding to deliver / cancelled).
        # `ids` from the caller is IGNORED (server chooses the delivered set). The advisory
        # findings snapshot rides the response as `qa_snapshot`.
        try:
            out = delivery.deliver(s, proj, mode="full", cost=cost, currency=data.get("currency"))
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
    return JSONResponse(out)


@app.post("/projects/{pid}/deliver-partial")
async def deliver_partial(request: Request, pid: int):
    require_role(request, "PM")
    data = await _json_body(request, allow_empty=True)
    cost = _num_or_none(data.get("cost"))
    with get_session() as s:
        proj = _get_project(s, pid)
        # Partial delivery ships ONLY the server-computed CONFIRMED/APPROVED subset (a
        # caller-supplied id list is NOT honoured). QA is advisory: no findings-based 409;
        # the only 409 is a structural refusal (no confirmed subset to deliver / cancelled).
        try:
            out = delivery.deliver(
                s, proj, mode="partial", cost=cost, currency=data.get("currency")
            )
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
    return JSONResponse(out)


@app.post("/projects/{pid}/complete")
async def mark_complete(request: Request, pid: int):
    """Mark the job READY (COMPLETED) for a PULL-model vendor. A poll (GET /intake/poll) then
    serves + flips DELIVERED. Use this instead of /deliver when Localization OS pulls rather than
    Vendor Studio pushing. QA is advisory-only (decided 2026-08-08): completion NEVER blocks on
    findings — the advisory snapshot is surfaced, and Localization OS's admission seam is the
    enforcing side. The only 409 is the status guard (already DELIVERED/CANCELLED)."""
    require_role(request, "PM")
    with get_session() as s:
        proj = _get_project(s, pid)
        if proj.status in (VProjectStatus.DELIVERED, VProjectStatus.CANCELLED):
            raise HTTPException(
                status_code=409, detail=f"project is {proj.status}; cannot complete."
            )
        try:
            delivery.require_quote_cleared(proj)
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        outstanding = set(delivery.outstanding_ids(s, proj))
        seg_ids = {
            sg.id for sg in delivery.project_segments(s, proj) if sg.contract_id in outstanding
        }
        qa_snapshot = qa_engine.gate(s, proj, only_ids=seg_ids)
        try:
            intake.set_status(s, proj, VProjectStatus.COMPLETED)
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
    return JSONResponse(
        {"ok": True, "status": VProjectStatus.COMPLETED, "qa_snapshot": qa_snapshot}
    )


@app.get("/projects/{pid}/package")
def download_package(request: Request, pid: int, format: str = ""):
    """Download the received dispatch PACKAGE (source + assets), for the vendor's own
    records / offline CAT import. Never carries the claim token.

    Vendor Wave D: `?format=xliff` returns the CAT-openable XLIFF 1.2 rendition (.xlf)
    instead of the JSON, produced by Vendor Studio's OWN independent XLIFF writer
    (vstudio.xliff -- no shared code with Localization OS, the point being that two independent
    implementations cross-validate)."""
    require_session(request)
    with get_session() as s:
        proj = _get_project(s, pid)
        segs = delivery.project_segments(s, proj)
        if str(format).strip().lower() == "xliff":
            blob = xliff.render_package_xliff(proj, segs)
            return Response(
                content=blob,
                media_type="application/xliff+xml",
                headers={"Content-Disposition": f'attachment; filename="package-{pid}.xlf"'},
            )
        pkg = {
            "source_locale": proj.source_locale,
            "target_locale": proj.target_locale,
            "customer": proj.customer_label,
            "run_ref": proj.run_ref,
            "glossary": proj.glossary,
            "style_guide": proj.style_guide,
            "context": proj.context,
            "estimate": proj.estimate,
            "segments": [
                {
                    "id": x.contract_id,
                    "source": x.source,
                    "target": x.target,
                    "provenance": x.provenance,
                    "match_score": x.match_score,
                    "locked": x.locked,
                    "locked_reason": x.locked_reason,
                    "char_limit": x.char_limit,
                }
                for x in segs
            ],
        }
    blob = json.dumps(pkg, ensure_ascii=False, indent=2).encode("utf-8")
    return Response(
        content=blob,
        media_type="application/json",
        headers={"Content-Disposition": f'attachment; filename="package-{pid}.json"'},
    )


@app.get("/projects/{pid}/export")
def export_return(request: Request, pid: int, format: str = ""):
    """Export the outbound RETURN (the delivered tranche). JSON is canonical; Vendor Wave D
    adds `?format=xliff` for an XLIFF 1.2 (.xlf) variant of the same return, via Vendor
    Studio's own independent XLIFF writer. Intake stays JSON-canonical -- this is an EXPORT
    convenience only."""
    require_session(request)
    with get_session() as s:
        proj = _get_project(s, pid)
        if str(format).strip().lower() == "xliff":
            tranche = delivery.outstanding_ids(s, proj)
            by_id = {seg.contract_id: seg for seg in delivery.project_segments(s, proj)}
            units = [
                {"id": cid, "source": by_id[cid].source, "target": by_id[cid].target}
                for cid in tranche
                if cid in by_id
            ]
            blob = xliff.render_return_xliff(proj, units)
            return Response(
                content=blob,
                media_type="application/xliff+xml",
                headers={"Content-Disposition": f'attachment; filename="return-{pid}.xlf"'},
            )
        blob = delivery.export_return_file(s, proj)
    return Response(
        content=blob,
        media_type="application/json",
        headers={"Content-Disposition": f'attachment; filename="return-{pid}.json"'},
    )


# ---------------------------------------------------------------------------
# wire log
# ---------------------------------------------------------------------------
@app.get("/wirelog", response_class=HTMLResponse)
def wirelog_page(request: Request):
    sess = require_role(request, "PM")
    with get_session() as s:
        rows = list(s.exec(select(WireLog).order_by(WireLog.created_at.desc()).limit(200)).all())
    return _TEMPLATES.TemplateResponse(
        request, "wirelog.html", {"request": request, "session": sess, "rows": rows}
    )


@app.get("/api/wirelog")
def wirelog_api(request: Request):
    require_role(request, "PM")
    with get_session() as s:
        rows = list(s.exec(select(WireLog).order_by(WireLog.created_at.desc()).limit(200)).all())
    return JSONResponse(
        {
            "events": [
                {
                    "direction": r.direction,
                    "kind": r.kind,
                    "verdict": r.verdict,
                    "status_code": r.status_code,
                    "reason": r.reason,
                    "project_id": r.project_id,
                    "created_at": r.created_at.isoformat(),
                }
                for r in rows
            ]
        }
    )


# ---------------------------------------------------------------------------
# autopilot
# ---------------------------------------------------------------------------
@app.post("/autopilot/run")
async def autopilot_run(request: Request):
    require_role(request, "PM")
    # The route delivers gate-bypassing adversarial payloads by design; it is a test-only
    # affordance and stays 404 unless VSTUDIO_AUTOPILOT_ENABLED is set (master-switch OFF by
    # default, mirroring the main app's remote_admin_enabled). The CLI + roundtrip harness
    # are unaffected (the CLI calls the service directly; the harness sets the env).
    if not settings.autopilot_enabled:
        raise HTTPException(status_code=404, detail="Not found")
    data = await _json_body(request)
    from vstudio import autopilot as ap

    scenario = data.get("scenario", "")
    if scenario not in ap.SCENARIOS:
        raise HTTPException(status_code=400, detail=f"unknown scenario {scenario!r}")
    pid = data.get("project_id")
    with get_session() as s:
        try:
            out = ap.run_scenario(s, scenario, project_id=int(pid) if pid else None)
        except ap.AutopilotError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
    return JSONResponse(out)


# ---------------------------------------------------------------------------
# small utils
# ---------------------------------------------------------------------------
async def _json_body(request: Request, *, allow_empty: bool = False) -> dict:
    # M4 + L3: cap the body on EVERY JSON route (not just intake). Check the declared
    # length first, then STREAM with a running total so a chunked body (no/lying
    # content-length) can never buffer past the cap before it is measured — the same
    # posture the intake route uses.
    declared = request.headers.get("content-length")
    if declared is not None:
        try:
            if int(declared) > _MAX_JSON_BODY:
                raise HTTPException(status_code=413, detail="request body too large")
        except ValueError:
            pass
    chunks, total = [], 0
    async for piece in request.stream():
        total += len(piece)
        if total > _MAX_JSON_BODY:
            raise HTTPException(status_code=413, detail="request body too large")
        chunks.append(piece)
    raw = b"".join(chunks)
    try:
        if not raw and allow_empty:
            return {}
        out = json.loads(raw.decode("utf-8"))
        return out if isinstance(out, dict) else {}
    except (ValueError, UnicodeDecodeError) as e:
        if allow_empty:
            return {}
        raise HTTPException(status_code=400, detail="body must be JSON") from e


def _actor(request: Request) -> str:
    sess = _session_of(request) or {}
    return sess.get("name") or sess.get("user") or "user"


def _num_or_none(v):
    if v is None or v == "":
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None
