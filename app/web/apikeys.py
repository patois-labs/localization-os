"""Self-service per-user API keys (managed from /account).

The full key is surfaced exactly once: it is kept server-side in
app/web/one_time_stash.py and only a random reference rides in the session (which
is signed, NOT encrypted, so the key itself must never go there, #476); the very
next GET /account render pops it. Never in a URL either, where it would land in
access logs, browser history and Referer headers.
When auth is OFF the session user is the synthetic SYSTEM admin (no row id), so
there is no real user to bind a key to and both actions redirect away without
doing anything.

Documented in: docs/architecture/identity-and-access.md
"""

from fastapi import APIRouter, Form, Request
from fastapi.responses import RedirectResponse

from app.kernel import apikeys, audit, security
from app.web import one_time_stash
from app.web.deps import _ip

router = APIRouter()


def _real_user(request: Request):
    """The signed-in DB-backed user, or None (auth off / not signed in)."""
    me = security.session_user(request)
    if not security.auth_required() or not me or not getattr(me, "id", 0):
        return None
    return me


@router.post("/account/apikeys")
def apikeys_create(
    request: Request,
    name: str = Form(""),
    docs: str = Form(""),
    keys: str = Form(""),
    expires_in_days: str = Form(""),
):
    me = _real_user(request)
    if me is None:
        return RedirectResponse("/account", status_code=303)

    # A ticked "documents" box mints a docs-scoped key for the MCP front door
    # (upload/translate/download); otherwise the default "api"-only key.
    #
    # SPEC-key-based-projects section 5: "keys" is a SEPARATE grant, never implied by
    # "docs". A CI sync consumer polling the delta feed has no business uploading and
    # downloading documents, and a document integration has no business reading the
    # whole string catalogue -- so each box grants exactly its own surface.
    def _on(v):
        return str(v).strip() in ("1", "on", "true", "yes")

    scopes = " ".join(["api"] + (["docs"] if _on(docs) else []) + (["keys"] if _on(keys) else []))
    # Optional lifetime (#270 r2-apiauth-10): blank = no expiry; otherwise a positive
    # whole number of days from now. The org-level maximum (apikey_max_age_days) is
    # enforced at the shared mint seam; its refusal (and a malformed value) flashes
    # once on /account, session-stashed like the key itself, never in a URL.
    try:
        expires_at = apikeys.expiry_from_days(expires_in_days)
    except ValueError:
        request.session["new_key_refusal"] = (
            f"Expiry must be a whole number of days from 1 to {apikeys.MAX_LIFETIME_DAYS} "
            "(leave it blank for no expiry)."
        )
        return RedirectResponse("/account", status_code=303)
    try:
        key, full_key = apikeys.create_key(me.id, name, scopes=scopes, expires_at=expires_at)
    except ValueError as exc:
        request.session["new_key_refusal"] = str(exc)
        return RedirectResponse("/account", status_code=303)
    audit.log(
        actor=me.email,
        action="apikey.create",
        target=key.prefix,
        detail=f"{key.name} [{key.scopes}]",
        ip=_ip(request),
    )
    # one-time display (#476): the key stays server-side; only a random reference goes in
    # the (signed, not encrypted) session, popped by GET /account; never in a URL.
    one_time_stash.put(request, "new_key_ref", full_key)
    if key.expires_at:
        request.session["new_key_expires_at"] = key.expires_at.strftime("%Y-%m-%d %H:%M UTC")
    return RedirectResponse("/account?saved=key", status_code=303)


@router.post("/account/apikeys/{kid}/revoke")
def apikeys_revoke(kid: int, request: Request):
    me = _real_user(request)
    if me is None:
        return RedirectResponse("/account", status_code=303)
    # user_id guard: users may only revoke their OWN keys
    if apikeys.revoke(kid, user_id=me.id):
        audit.log(actor=me.email, action="apikey.revoke", target=f"key #{kid}", ip=_ip(request))
    return RedirectResponse("/account", status_code=303)
