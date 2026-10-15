"""Editable page layouts: the web (session) routes and the /api (token) routes
(SPEC-project-layout-editor section 9, train L1).

Two front doors over ONE service (app/services/page_layouts.py), both strictly
self-service: the caller can only ever read or write THEIR OWN layout. The owner is
derived from the identity the request already carries and is never read from a path,
query or body field:

  * web (`router`, under /account/layouts): the signed-in session user, or the
    install's single "local" owner when sign-in is OFF. These are session-cookie
    routes, so every PUT/DELETE passes the app-wide Origin + synchronizer-token CSRF
    check in app.main._security_audit (the page's fetch sends X-CSRF-Token). A viewer
    may save their own layout: app.main._self_service_writable admits exactly these two
    verbs on exactly this path past the blanket can_write gate.
  * /api (`api_router`, under /api/account/layouts): the lsu_ token's user (docs scope,
    the same gate as GET /api/account), or the "local" owner when sign-in is OFF so the
    remote CLI edits the same layout the web page shows. No /api route reads the session.

Documented in: docs/architecture/surfaces-and-parity.md
"""

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from app.kernel import audit, page_layouts, security
from app.web.api_cli_common import _creator, _gate

router = APIRouter()
api_router = APIRouter()


def _web_owner(request: Request):
    """(owner, user_id, actor) for a session request, or None when there is no
    signed-in user (the middleware normally redirects that request before it lands)."""
    if not security.auth_required():
        owner, uid = page_layouts.owner_for_user(None)
        return owner, uid, "local"
    me = security.session_user(request)
    if me is None or not getattr(me, "id", None):
        return None
    owner, uid = page_layouts.owner_for_user(me)
    return owner, uid, me.email


def _api_owner(ident: dict):
    """(owner, user_id, actor) for a token request (auth off -> the local owner)."""
    if not security.auth_required():
        owner, uid = page_layouts.owner_for_user(None)
    else:
        owner, uid = page_layouts.owner_for_user(_creator(ident))
    return owner, uid, ident.get("actor") or "api"


def _err(status: int, message: str) -> JSONResponse:
    return JSONResponse({"ok": False, "error": message}, status_code=status)


def _get(owner: str, page_key: str) -> JSONResponse:
    try:
        view = page_layouts.get_layout(owner, page_key)
    except page_layouts.UnknownPage as exc:
        return _err(404, str(exc))
    return JSONResponse({"ok": True, **view})


async def _put(request: Request, owner: str, uid, actor: str, page_key: str) -> JSONResponse:
    try:
        page_layouts.page_spec(page_key)
        payload = page_layouts.parse_body(await request.body())
        view = page_layouts.save_layout(owner, uid, page_key, payload)
    except page_layouts.UnknownPage as exc:
        return _err(404, str(exc))
    except page_layouts.LayoutError as exc:
        return _err(400, str(exc))
    audit.log(actor=actor, action="account.layout_save", target=page_key, detail="")
    return JSONResponse({"ok": True, **view})


def _delete(owner: str, actor: str, page_key: str) -> JSONResponse:
    try:
        existed = page_layouts.reset_layout(owner, page_key)
        view = page_layouts.get_layout(owner, page_key)
    except page_layouts.UnknownPage as exc:
        return _err(404, str(exc))
    audit.log(actor=actor, action="account.layout_reset", target=page_key, detail="")
    return JSONResponse({"ok": True, "reset": existed, **view})


# --------------------------------------------------------------------------- #
# Web (session) routes: the page's edit mode calls these with fetch.
# --------------------------------------------------------------------------- #
@router.get("/account/layouts/{page_key}")
def web_layout_get(page_key: str, request: Request):
    who = _web_owner(request)
    if who is None:
        return _err(401, "Sign in to use page layouts.")
    return _get(who[0], page_key)


@router.put("/account/layouts/{page_key}")
async def web_layout_put(page_key: str, request: Request):
    who = _web_owner(request)
    if who is None:
        return _err(401, "Sign in to use page layouts.")
    owner, uid, actor = who
    return await _put(request, owner, uid, actor, page_key)


@router.delete("/account/layouts/{page_key}")
def web_layout_delete(page_key: str, request: Request):
    who = _web_owner(request)
    if who is None:
        return _err(401, "Sign in to use page layouts.")
    owner, _uid, actor = who
    return _delete(owner, actor, page_key)


# --------------------------------------------------------------------------- #
# /api (token) routes: the remote CLI `layout` group calls these.
# --------------------------------------------------------------------------- #
@api_router.get("/api/account/layouts")
def api_layouts_list(request: Request):
    ident, _org_id, blocked = _gate(request)
    if blocked:
        return blocked
    owner, _uid, _actor = _api_owner(ident)
    return JSONResponse({"ok": True, "layouts": page_layouts.list_layouts(owner)})


@api_router.get("/api/account/layouts/{page_key}")
def api_layout_get(page_key: str, request: Request):
    ident, _org_id, blocked = _gate(request)
    if blocked:
        return blocked
    owner, _uid, _actor = _api_owner(ident)
    return _get(owner, page_key)


@api_router.put("/api/account/layouts/{page_key}")
async def api_layout_put(page_key: str, request: Request):
    ident, _org_id, blocked = _gate(request)
    if blocked:
        return blocked
    owner, uid, actor = _api_owner(ident)
    return await _put(request, owner, uid, actor, page_key)


@api_router.delete("/api/account/layouts/{page_key}")
def api_layout_delete(page_key: str, request: Request):
    ident, _org_id, blocked = _gate(request)
    if blocked:
        return blocked
    owner, _uid, actor = _api_owner(ident)
    return _delete(owner, actor, page_key)
