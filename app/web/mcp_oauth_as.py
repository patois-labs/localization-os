"""MCP OAuth authorization server routes (#753). Off by default (app/services/mcp_as.py).

Public, session-blind machine endpoints (exempt from the sign-in wall and from Origin/CSRF in
app/main.py, like /api, because an MCP client carries neither a session nor a CSRF token; each
one authenticates what it needs itself and reads no request.session):
  GET  /connectors/oauth/client-metadata.json    our CLIENT metadata document (#758, mcp_cms)
  GET  /.well-known/oauth-authorization-server   RFC 8414 metadata, 404 while off
  POST /oauth/token                              PKCE / refresh / resource-server exchange
  POST /oauth/register                           RFC 7591, only with mcp_oauth_dcr_enabled
  GET  /oauth/authorize                          validates, then hands the browser to consent

Session pages (normal sign-in wall, CSRF on every POST):
  GET/POST /oauth/consent                        approve or deny (shows client + redirect host)
  GET  /account/connected-apps, POST .../{gid}/revoke     a person's own grants
  GET/POST /security/mcp-oauth, POST /security/mcp-oauth/rs-secret   admin settings
/api (token-authed): GET /api/mcp-oauth/grants, POST /api/mcp-oauth/grants/{gid}/revoke.

Documented in: docs/architecture/mcp-oauth.md
"""

from __future__ import annotations

import json
from urllib.parse import quote

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, RedirectResponse

from app.kernel import audit, mcp_as, security, settings
from app.web.deps import _admin_block, _error_page, _ip, api_identity, templates
from app.web.security_headers import allow_form_action

router = APIRouter()
api_router = APIRouter()

_NO_STORE = {"Cache-Control": "no-store", "Pragma": "no-cache"}
_MAX_FORM = 16 * 1024


def _not_found():
    return JSONResponse({"error": "not_found"}, status_code=404)


def _real_user(request: Request):
    me = security.session_user(request)
    if not security.auth_required() or not me or not getattr(me, "id", 0):
        return None
    return me


# --------------------------------------------------------------------------- metadata
@router.get("/.well-known/oauth-authorization-server")
def as_metadata():
    if not mcp_as.active():
        return _not_found()
    return JSONResponse(mcp_as.metadata(), headers={"Cache-Control": "public, max-age=300"})


# --------------------------------------------------------------------------- mcp_cms client
@router.get("/connectors/oauth/client-metadata.json")
def connector_oauth_client_metadata():
    """Our OAuth Client ID Metadata Document as an MCP CLIENT (#758, the mcp_cms connector;
    MCP 2026-07-28 client registration). Lives here, not in connectors_common, because that
    router is spliced into app/web/connectors.py by fixed route slices.

    PUBLIC by design (app/main.py _ALLOW_EXACT): an MCP server's authorization server fetches
    it, carrying no session. It holds only what the authorization request already reveals
    (client name, our callback URL, public-client facts) and is built from the configured
    app_base_url alone, never from the request. 404 when app_base_url is not a public https
    URL, which is exactly when the client falls back to dynamic registration."""
    from app.kernel.connectors import oauth as conn_oauth

    doc = conn_oauth.mcp_client_metadata_document()
    if doc is None:
        return _not_found()
    return JSONResponse(doc, headers={"Cache-Control": "public, max-age=3600"})


# --------------------------------------------------------------------------- authorize
@router.get("/oauth/authorize")
def oauth_authorize(request: Request):
    """Validate the request (client, exact redirect URI, PKCE S256, resource, scope), sign it,
    and send the browser to the consent page; through sign-in first when needed. A request
    whose client or redirect URI cannot be trusted is shown here and never redirected."""
    if not mcp_as.active():
        return _error_page(request, 404, "Not found.")
    # Gate: an unauthenticated request can make the server fetch a client metadata document.
    wait = mcp_as.rate_limited("authorize", _ip(request), mcp_as.AUTHORIZE_PER_MINUTE)
    if wait:
        resp = _error_page(request, 429, "Too many connection attempts; try again shortly.")
        resp.headers["Retry-After"] = str(wait)
        return resp
    try:
        req = mcp_as.validate_authorize(request.query_params)
    except mcp_as.OAuthError as e:
        if e.redirect:
            return RedirectResponse(
                mcp_as.response_url(
                    e.target["redirect_uri"],
                    {
                        "error": e.error,
                        "error_description": e.description,
                        "state": e.target.get("state") or "",
                    },
                ),
                status_code=303,
            )
        return _error_page(request, e.status, "This app cannot connect: " + e.description)
    target = "/oauth/consent?r=" + quote(mcp_as.sign_request(req), safe="")
    if _real_user(request) is None:
        return RedirectResponse("/auth/login?next=" + quote(target, safe=""), status_code=303)
    return RedirectResponse(target, status_code=303)


def _consent_req(request: Request, blob):
    req = mcp_as.load_request(blob)
    if req is None:
        return None, _error_page(
            request, 400, "This sign-in request expired. Start the connection again from the app."
        )
    return req, None


@router.get("/oauth/consent")
def oauth_consent(request: Request, r: str = ""):
    if not mcp_as.active():
        return _error_page(request, 404, "Not found.")
    me = _real_user(request)
    if me is None:
        return _error_page(request, 403, "Sign in with your own account to connect an app.")
    req, err = _consent_req(request, r)
    if err:
        return err
    # The approve/deny POST redirects to the client: CSP form-action must allow its origin.
    allow_form_action(request, req["redirect_uri"])
    return templates.TemplateResponse(
        request,
        "oauth_consent.html",
        {"request": request, "me": me, "r": r, "v": mcp_as.consent_view(req)},
    )


@router.post("/oauth/consent")
async def oauth_consent_post(request: Request):
    if not mcp_as.active():
        return _error_page(request, 404, "Not found.")
    me = _real_user(request)
    if me is None:
        return _error_page(request, 403, "Sign in with your own account to connect an app.")
    form = await request.form()
    req, err = _consent_req(request, form.get("r"))
    if err:
        return err
    if form.get("decision") != "approve":
        audit.log(
            actor=me.email,
            action="mcp_oauth.denied",
            target=req["client_name"][:100],
            ip=_ip(request),
        )
        return RedirectResponse(mcp_as.client_error_redirect(req, "access_denied"), status_code=303)
    chosen = {s for s in form.getlist("scope") if s in req["scopes"]}
    try:
        url = mcp_as.approve(me, req, chosen)
    except mcp_as.OAuthError as e:
        return _error_page(request, e.status, e.description)
    audit.log(
        actor=me.email,
        action="mcp_oauth.approved",
        target=req["client_name"][:100],
        detail="scopes="
        + " ".join(sorted(chosen | {"api"}))
        + " redirect_host="
        + (mcp_as.consent_view(req)["redirect_host"] or "-"),
        ip=_ip(request),
    )
    return RedirectResponse(url, status_code=303)


# --------------------------------------------------------------------------- token / register
async def _bounded_body(request: Request) -> bytes | None:
    raw = await request.body()
    return None if len(raw) > _MAX_FORM else raw


@router.post("/oauth/token")
async def oauth_token(request: Request):
    if not mcp_as.active():
        return _not_found()
    raw = await _bounded_body(request)
    if raw is None:
        return JSONResponse({"error": "invalid_request"}, status_code=413, headers=_NO_STORE)
    from urllib.parse import parse_qs

    try:
        parsed = parse_qs(raw.decode("utf-8"), keep_blank_values=True, max_num_fields=20)
    except (UnicodeDecodeError, ValueError):
        return JSONResponse({"error": "invalid_request"}, status_code=400, headers=_NO_STORE)
    # RFC 6749 3.2: a repeated parameter is an error.
    if any(len(v) > 1 for v in parsed.values()):
        return JSONResponse({"error": "invalid_request"}, status_code=400, headers=_NO_STORE)
    form = {k: v[0] for k, v in parsed.items()}
    if form.get("grant_type") != mcp_as.TOKEN_EXCHANGE:
        # Gate: per client IP for the code and refresh grants. The resource server's exchange
        # is authenticated with its secret and all comes from one address, so it is not charged.
        wait = mcp_as.rate_limited("token", _ip(request), mcp_as.TOKEN_PER_MINUTE)
        if wait:
            return JSONResponse(
                {"error": "slow_down"},
                status_code=429,
                headers={**_NO_STORE, "Retry-After": str(wait)},
            )
    status, body = mcp_as.token(form, request.headers.get("authorization") or "")
    headers = dict(_NO_STORE)
    if status == 401:
        headers["WWW-Authenticate"] = 'Basic realm="mcp-oauth"'
    return JSONResponse(body, status_code=status, headers=headers)


@router.post("/oauth/register")
async def oauth_register(request: Request):
    if not (mcp_as.active() and mcp_as.dcr_enabled()):
        return _not_found()
    wait = mcp_as.rate_limited("register", _ip(request), mcp_as.REGISTER_PER_MINUTE)
    if wait:
        return JSONResponse(
            {"error": "slow_down"}, status_code=429, headers={"Retry-After": str(wait)}
        )
    raw = await _bounded_body(request)
    if raw is None:
        return JSONResponse({"error": "invalid_client_metadata"}, status_code=413)
    try:
        body = json.loads(raw.decode("utf-8"))
        out = mcp_as.register(body)
    except (ValueError, UnicodeDecodeError):
        return JSONResponse(
            {"error": "invalid_client_metadata", "error_description": "Body is not JSON."},
            status_code=400,
        )
    except mcp_as.OAuthError as e:
        return JSONResponse({"error": e.error, "error_description": e.description}, status_code=400)
    return JSONResponse(out, status_code=201, headers=_NO_STORE)


# --------------------------------------------------------------------------- a person's grants
@router.get("/account/connected-apps")
def connected_apps(request: Request):
    me = _real_user(request)
    if me is None:
        return RedirectResponse("/account", status_code=303)
    return templates.TemplateResponse(
        request,
        "mcp_connected_apps.html",
        {
            "request": request,
            "me": me,
            "grants": mcp_as.list_grants(me.id),
            "server_on": mcp_as.active(),
            "revoked": request.query_params.get("revoked") == "1",
        },
    )


@router.post("/account/connected-apps/{gid}/revoke")
def connected_app_revoke(gid: int, request: Request):
    me = _real_user(request)
    if me is None:
        return RedirectResponse("/account", status_code=303)
    if mcp_as.revoke_grant(gid, user_id=me.id):
        audit.log(
            actor=me.email, action="mcp_oauth.revoked", target=f"grant #{gid}", ip=_ip(request)
        )
    return RedirectResponse("/account/connected-apps?revoked=1", status_code=303)


# --------------------------------------------------------------------------- admin settings
@router.get("/security/mcp-oauth")
def mcp_oauth_admin(request: Request):
    _me, blocked = _admin_block(request)
    if blocked:
        return blocked
    from app.web import one_time_stash

    _had, secret = one_time_stash.pop(request, "mcp_rs_secret_ref")
    return templates.TemplateResponse(
        request,
        "mcp_oauth_admin.html",
        {
            "request": request,
            "st": mcp_as.status(),
            "vals": {
                k: settings.get(k)
                for k in (
                    "mcp_oauth_enabled",
                    "mcp_oauth_resource",
                    "mcp_oauth_dcr_enabled",
                    "mcp_oauth_cimd_hosts",
                )
            },
            "new_secret": secret,
            "saved": request.query_params.get("saved") == "1",
            "refusal": request.session.pop("mcp_oauth_refusal", None),
            "grants": mcp_as.list_grants(None) if not _org_scoped() else [],
        },
    )


def _org_scoped() -> bool:
    from app.kernel import access

    return access.multi_org_active()


@router.post("/security/mcp-oauth")
async def mcp_oauth_admin_save(request: Request):
    me, blocked = _admin_block(request)
    if blocked:
        return blocked
    form = await request.form()
    items = {
        "mcp_oauth_enabled": "1" if form.get("mcp_oauth_enabled") == "1" else "0",
        "mcp_oauth_resource": str(form.get("mcp_oauth_resource") or "").strip(),
        "mcp_oauth_dcr_enabled": "1" if form.get("mcp_oauth_dcr_enabled") == "1" else "0",
        "mcp_oauth_cimd_hosts": str(form.get("mcp_oauth_cimd_hosts") or "").strip(),
    }
    try:
        settings.validate_values(items)
        before = {k: settings.get(k) for k in items}
        settings.set_many(items, actor=me.email)
    except settings.InvalidSettingValue as e:
        request.session["mcp_oauth_refusal"] = str(e)[:300]
        return RedirectResponse("/security/mcp-oauth", status_code=303)
    except PermissionError as e:
        request.session["mcp_oauth_refusal"] = str(e)[:300]
        return RedirectResponse("/security/mcp-oauth", status_code=303)
    changed = [k for k in items if before.get(k) != items[k]]
    if changed:
        audit.log(
            actor=me.email,
            action="settings.update",
            target="mcp_oauth",
            detail=", ".join(f"{k}={items[k]}" for k in changed)[:500],
            ip=_ip(request),
        )
    return RedirectResponse("/security/mcp-oauth?saved=1", status_code=303)


@router.post("/security/mcp-oauth/rs-secret")
def mcp_oauth_rs_secret(request: Request):
    me, blocked = _admin_block(request)
    if blocked:
        return blocked
    try:
        secret = mcp_as.generate_rs_secret(actor=me.email)
    except PermissionError as e:
        request.session["mcp_oauth_refusal"] = str(e)[:300]
        return RedirectResponse("/security/mcp-oauth", status_code=303)
    audit.log(actor=me.email, action="mcp_oauth.rs_secret_rotated", ip=_ip(request))
    # Shown once on the next render: the secret stays server-side (one_time_stash, the #476
    # lsu_ key pattern); only a random reference rides in the signed, not encrypted, session.
    from app.web import one_time_stash

    one_time_stash.put(request, "mcp_rs_secret_ref", secret)
    return RedirectResponse("/security/mcp-oauth", status_code=303)


# --------------------------------------------------------------------------- /api
@api_router.get("/api/mcp-oauth/grants")
def api_grants(request: Request):
    """The caller's own MCP OAuth grants (metadata only, never a token or digest)."""
    ident = api_identity(request)
    if not ident or ident.get("kind") != "user" or ident.get("user") is None:
        return JSONResponse({"ok": False, "error": "unauthorized"}, status_code=401)
    return JSONResponse(
        {
            "ok": True,
            "server_active": mcp_as.active(),
            "grants": mcp_as.list_grants(ident["user"].id),
        }
    )


@api_router.post("/api/mcp-oauth/grants/{gid}/revoke")
def api_grant_revoke(gid: int, request: Request):
    """Revoke one of the caller's own grants. A foreign or missing id answers the same 404."""
    ident = api_identity(request)
    if not ident or ident.get("kind") != "user" or ident.get("user") is None:
        return JSONResponse({"ok": False, "error": "unauthorized"}, status_code=401)
    user = ident["user"]
    if not mcp_as.revoke_grant(gid, user_id=user.id):
        return JSONResponse({"ok": False, "error": "not found"}, status_code=404)
    audit.log(actor=user.email, action="mcp_oauth.revoked", target=f"grant #{gid}")
    return JSONResponse({"ok": True, "revoked": gid})
