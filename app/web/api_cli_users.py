"""/api routes: users

Documented in: docs/architecture/surfaces-and-parity.md
"""

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from sqlmodel import Session, select

from app.db import engine
from app.web.api_cli_common import (
    _NOT_FOUND,
    _admin_block,
    _customer_in_org,
    _gate,
    _gate_admin,
    _int_id,
    _json,
    _org_list,
    _remote_admin_block,
    _user_in_org,
)

router = APIRouter()


def _user_row(u) -> dict:
    """The stable, SAFE JSON row for a user — NEVER the password hash. Mirrors the
    CLI `user` row so the surfaces cannot drift."""
    return {
        "id": u.id,
        "email": u.email,
        "name": u.name,
        "role": u.role,
        "department": u.department,
        "active": u.active,
        "auth_source": u.auth_source,
        "customer_id": u.customer_id,
    }


# --- users -----------------------------------------------------------------
@router.post("/api/users")
async def api_user_create(request: Request):
    """Create a user (mirrors `user create`). is_admin, ADMIN scope (#270
    r2-apiauth-10: creating an account with a role is an account-security action,
    so a docs-scoped (MCP) key gets the same refusal the other admin-scope routes
    give). Body: email (required), role (required), name, department, password,
    customer_id. The role<->customer-binding invariant + password policy live in
    security.create_user, so both surfaces inherit them; a bound customer id is
    RINGED to the caller's org."""
    from app.kernel import audit, security
    from app.models import Role

    ident, org_id, blocked = _gate_admin(request)
    if blocked:
        return blocked
    b = _admin_block(ident)
    if b:
        return b
    body = await _json(request)
    email = (body.get("email") or "").strip()
    role = (body.get("role") or "").strip()
    if not email:
        return JSONResponse({"ok": False, "error": "email is required."}, status_code=400)
    if role not in Role.ALL:
        return JSONResponse(
            {
                "ok": False,
                "error": "unknown role: {} (one of: {})".format(role, ", ".join(Role.ALL)),
            },
            status_code=400,
        )
    cust_id = None
    with Session(engine) as s:
        raw_cid = body.get("customer_id")
        if raw_cid not in (None, "", 0, "0"):
            from app.kernel.security import _coerce_customer_id

            coerced = _coerce_customer_id(raw_cid)
            # Ring the customer to the caller's org: a foreign/missing customer is a
            # clean 400 (the caller is unbound staff, so no existence-oracle worry —
            # but the binding must never cross the org boundary).
            if coerced is None or _customer_in_org(s, coerced, org_id, ident) is None:
                return JSONResponse(
                    {"ok": False, "error": f"no such customer: {raw_cid}"},
                    status_code=400,  # noqa: RUF100, UP032
                )
            cust_id = coerced
    # Stamp the new user into the caller's org (multi-org) AT creation. #181: this used to
    # create org-less and stamp afterwards, so an org-less caller minted an orphan that
    # only the token wall kept out of reach; create_user now refuses that itself. #73:
    # create_user's stampable_org_id collapses the BLOCKED_ORG_ID read-poison to None, so
    # a broken caller can never plant an org_id=-1 account.
    try:
        user = security.create_user(
            email,
            body.get("name") or "",
            role,
            body.get("password") or "",
            customer_id=cust_id,
            org_id=org_id,
        )
    except ValueError as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
    dept = (body.get("department") or "").strip()
    if dept:
        with Session(engine) as s:
            security.set_department(s, user.id, dept, actor=ident.get("actor", ""), org_id=org_id)
    audit.log(
        actor=ident.get("actor", ""),
        action="user.create",
        target=user.email,
        detail=("customer %d" % cust_id if cust_id else ""),  # noqa: UP031
    )
    fresh = security.get_by_email(user.email)
    return JSONResponse({"ok": True, **_user_row(fresh)})


@router.get("/api/users")
def api_user_list(request: Request):
    """List users (mirrors `user list`), org-scoped. is_admin, docs scope. NEVER the
    password hash. `active_keys` counts each user's live API keys."""
    from app.kernel import apikeys
    from app.models import User

    ident, org_id, blocked = _gate(request)
    if blocked:
        return blocked
    b = _admin_block(ident)
    if b:
        return b
    with Session(engine) as s:
        stmt = _org_list(select(User), User, org_id).order_by(User.created_at)
        users = s.exec(stmt).all()
        payload = []
        for u in users:
            keys = sum(
                1 for k in apikeys.list_keys(u.id) if k.active and not apikeys.key_is_expired(k)
            )
            payload.append({**_user_row(u), "active_keys": keys})
    return JSONResponse({"ok": True, "users": payload})


@router.get("/api/users/invites")
def api_user_invites(request: Request, expired: bool = False):
    """List pending (or ?expired=1) invitations without tokens (admin, docs scope)."""
    from app.kernel import security

    ident, org_id, blocked = _gate(request)
    if blocked:
        return blocked
    blocked = _admin_block(ident)
    if blocked:
        return blocked
    org_id = getattr(
        ident.get("user"), "org_id", ident.get("org_id")
    )  # #129: NULL is a confined invite scope, even under the gate.
    return JSONResponse(
        {
            "ok": True,
            "invites": [
                security.invite_metadata(inv)
                for inv in (security.expired_invites if expired else security.pending_invites)(
                    org_id
                )
            ],
        }
    )


@router.delete("/api/users/invites/{invite_id}")
def api_user_invite_revoke(request: Request, invite_id: int):
    """Revoke (soft-delete) a pending or expired invitation (admin, docs scope);
    accepted, already revoked or foreign IDs are 404."""
    from app.kernel import security

    ident, org_id, blocked = _gate(request)
    if blocked:
        return blocked
    blocked = _admin_block(ident)
    if blocked:
        return blocked
    org_id = getattr(ident.get("user"), "org_id", ident.get("org_id"))
    expired_ids = {inv.id for inv in security.expired_invites(org_id)}
    if invite_id not in {inv.id for inv in security.pending_invites(org_id)} | expired_ids:
        return JSONResponse(_NOT_FOUND, status_code=404)
    inv = security.revoke_invite(
        invite_id,
        ident.get("actor", ""),
        org_id=org_id,
        allow_expired=invite_id in expired_ids,
    )
    if inv is None:
        return JSONResponse(_NOT_FOUND, status_code=404)
    return JSONResponse(
        {
            "ok": True,
            "email": inv.email,
            "revoked_at": inv.revoked_at.isoformat(),
            "expired": inv.expires_at < inv.revoked_at,
        }
    )


@router.get("/api/users/{uid}")
def api_user_show(request: Request, uid: int):
    """Show one user (mirrors `user show`), org-ringed. is_admin, docs scope. Lists the
    user's API-key METADATA only (never key material/hash)."""
    from app.kernel import apikeys

    ident, org_id, blocked = _gate(request)
    if blocked:
        return blocked
    b = _admin_block(ident)
    if b:
        return b
    with Session(engine) as s:
        u = _user_in_org(s, _int_id(uid), org_id)
        if u is None:
            return JSONResponse(_NOT_FOUND, status_code=404)
        keys = [
            {
                "id": k.id,
                "name": k.name,
                "prefix": k.prefix,
                "scopes": k.scopes,
                "active": k.active,
                "expires_at": k.expires_at.isoformat() if k.expires_at else None,
                "expired": apikeys.key_is_expired(k),
            }
            for k in apikeys.list_keys(u.id)
        ]
        info = {
            **_user_row(u),
            "created_at": u.created_at.isoformat() if u.created_at else None,
            "last_login": u.last_login.isoformat() if u.last_login else None,
            "api_keys": keys,
        }
    return JSONResponse({"ok": True, **info})


@router.post("/api/users/{uid}/role")
async def api_user_role(request: Request, uid: int):
    """Set a user's role (mirrors `user role`), org-ringed. is_admin, ADMIN scope
    (#270 r2-apiauth-10: a role change is an account-security action, so a
    docs-scoped (MCP) key is refused here). Body: role. PRESERVES the last-admin
    guard: demoting the last active admin away from admin is refused (409), so the
    install can never be locked out of administration."""
    from app.kernel import audit, security
    from app.models import Role

    ident, org_id, blocked = _gate_admin(request)
    if blocked:
        return blocked
    b = _admin_block(ident)
    if b:
        return b
    body = await _json(request)
    role = (body.get("role") or "").strip()
    if role not in Role.ALL:
        return JSONResponse(
            {
                "ok": False,
                "error": "unknown role: {} (one of: {})".format(role, ", ".join(Role.ALL)),
            },
            status_code=400,
        )
    with Session(engine) as s:
        u = _user_in_org(s, _int_id(uid), org_id)
        if u is None:
            return JSONResponse(_NOT_FOUND, status_code=404)
        # Last-admin guard: #178-1 moved it into security.set_role, which counts the
        # TARGET's own org, so org A cannot self-lock its administration just because
        # another org still has an admin, on any surface. This route keeps its wording.
        res = security.set_role(s, u.id, role, actor=ident.get("actor", ""), org_id=org_id)
    if not res["ok"]:
        if res.get("reason") == "last_admin":
            return JSONResponse(
                {"ok": False, "error": "refusing to demote the last active administrator."},
                status_code=409,
            )
        if res.get("reason") == "role_bound":
            return JSONResponse(
                {
                    "ok": False,
                    "error": "a customer-bound account may only be a "
                    "translator, linguist or viewer.",
                },
                status_code=400,
            )
        return JSONResponse({"ok": False, "error": "could not set role."}, status_code=400)
    audit.log(
        actor=ident.get("actor", ""),
        action="user.role",
        target=res["email"],
        detail="{}->{}".format(res["old"], res["new"]),
    )
    return JSONResponse({"ok": True, "email": res["email"], "old": res["old"], "new": res["new"]})


@router.post("/api/users/{uid}/department")
async def api_user_department(request: Request, uid: int):
    """Set a user's department (mirrors `user department`), org-ringed. is_admin, docs
    scope. Body: department ('' clears). Stored lower-cased (the one canonical form)."""
    from app.kernel import audit, security

    ident, org_id, blocked = _gate(request)
    if blocked:
        return blocked
    b = _admin_block(ident)
    if b:
        return b
    body = await _json(request)
    with Session(engine) as s:
        u = _user_in_org(s, _int_id(uid), org_id)
        if u is None:
            return JSONResponse(_NOT_FOUND, status_code=404)
        res = security.set_department(
            s, u.id, body.get("department") or "", actor=ident.get("actor", ""), org_id=org_id
        )
    audit.log(
        actor=ident.get("actor", ""),
        action="user.department",
        target=res["email"],
        detail="{}->{}".format(res["old"] or "(none)", res["new"] or "(none)"),
    )
    return JSONResponse({"ok": True, "email": res["email"], "old": res["old"], "new": res["new"]})


@router.post("/api/users/{uid}/customer")
async def api_user_customer(request: Request, uid: int):
    """Bind a user to a customer or clear the binding (mirrors `user customer`),
    org-ringed. is_admin, docs scope. Body: customer_id (bind) OR clear:true. A bound
    account may only be translator/linguist/viewer; the target customer is RINGED to
    the caller's org (a foreign customer id is a clean 400, never a cross-org bind)."""
    from app.kernel import audit, security

    ident, org_id, blocked = _gate(request)
    if blocked:
        return blocked
    b = _admin_block(ident)
    if b:
        return b
    body = await _json(request)
    # #485: the contract is customer_id OR clear:true. An empty body or a null/blank
    # customer_id is NOT a clear (set_customer reads None as "unbind"), so it is a 400.
    raw_clear = body.get("clear")
    if raw_clear is not None and not isinstance(raw_clear, bool):
        return JSONResponse(
            {"ok": False, "error": '"clear" must be true or false.'}, status_code=400
        )
    clearing = raw_clear is True
    target = "" if clearing else body.get("customer_id")
    if not clearing and (
        target is None or isinstance(target, bool) or str(target).strip() in ("", "0")
    ):
        return JSONResponse(
            {"ok": False, "error": 'send a customer_id to bind, or "clear": true to unbind.'},
            status_code=400,
        )
    with Session(engine) as s:
        u = _user_in_org(s, _int_id(uid), org_id)
        if u is None:
            return JSONResponse(_NOT_FOUND, status_code=404)
        if not clearing and target not in (None, "", 0, "0"):
            from app.kernel.security import _coerce_customer_id

            coerced = _coerce_customer_id(target)
            if coerced is None or _customer_in_org(s, coerced, org_id, ident) is None:
                return JSONResponse(
                    {"ok": False, "error": f"no such customer: {target}"},
                    status_code=400,  # noqa: RUF100, UP032
                )
        res = security.set_customer(s, u.id, target, actor=ident.get("actor", ""), org_id=org_id)
    if not res["ok"]:
        reason = res.get("reason")
        if reason == "role_bound":
            return JSONResponse(
                {
                    "ok": False,
                    "error": "only a translator, linguist or viewer "
                    "account can be bound to a customer.",
                },
                status_code=400,
            )
        if reason in ("no_customer", "archived_customer"):
            return JSONResponse(
                {"ok": False, "error": f"no such customer: {target}"},
                status_code=400,  # noqa: RUF100, UP032
            )
        return JSONResponse(
            {"ok": False, "error": "could not update the customer binding."}, status_code=400
        )
    audit.log(
        actor=ident.get("actor", ""),
        action="user.customer",
        target=res["email"],
        detail="{}->{}".format(res["old"] or "(none)", res["new"] or "(none)"),
    )
    return JSONResponse({"ok": True, "email": res["email"], "old": res["old"], "new": res["new"]})


@router.post("/api/users/{uid}/enable")
def api_user_enable(request: Request, uid: int):
    """Re-activate a user (mirrors `user enable`), org-ringed. is_admin, ADMIN scope. Since
    #422 disabling ENDS the account's sessions and revokes its API keys, so re-enabling
    revives no credential (the person signs in again and creates new keys). Enable keeps
    the admin scope from the #270 ruling: turning an account back on is still an
    account-security action a docs-scoped (MCP) key must not take."""
    return _user_set_active(request, uid, True)


@router.post("/api/users/{uid}/disable")
def api_user_disable(request: Request, uid: int):
    """Deactivate a user (mirrors `user disable`), org-ringed. is_admin, docs scope.
    PRESERVES the last-admin guard: disabling the last active admin is refused (409)."""
    # #178-1: the org-scoped last-admin guard lives in security.set_active now (it counts
    # the target's own org); _user_set_active maps its refusal to this route's 409.
    return _user_set_active(request, uid, False)


def _user_set_active(request: Request, uid, active: bool):
    from app.kernel import audit, security

    # enable = admin scope (an account-security action); disable stays docs scope
    # (a protective, containment action, still behind the last-admin guard).
    ident, org_id, blocked = _gate_admin(request) if active else _gate(request)
    if blocked:
        return blocked
    b = _admin_block(ident)
    if b:
        return b
    with Session(engine) as s:
        u = _user_in_org(s, _int_id(uid), org_id)
        if u is None:
            return JSONResponse(_NOT_FOUND, status_code=404)
        res = security.set_active(s, u.id, active, actor=ident.get("actor", ""), org_id=org_id)
    if res.get("reason") == "last_admin":
        return JSONResponse(
            {"ok": False, "error": "refusing to disable the last active administrator."},
            status_code=409,
        )
    revoked = int(res.get("keys_revoked") or 0)
    audit.log(
        actor=ident.get("actor", ""),
        action="user.toggle",
        target=res["email"],
        detail="active" if res["active"] else f"disabled; sessions ended, {revoked} key(s) revoked",
    )
    body = {"ok": True, "email": res["email"], "active": res["active"], "keys_revoked": revoked}
    scim_tokens = res.get("scim_tokens_revoked") or []
    if scim_tokens:
        # #422 gate: the account owned a SCIM token, so identity provider sync stops.
        body["scim_tokens_revoked"] = len(scim_tokens)
        body["warning"] = security.SCIM_SYNC_STOPS_WARNING
    return JSONResponse(body)


@router.post("/api/users/{uid}/reset-password")
async def api_user_reset_password(request: Request, uid: int):
    """Admin password reset (mirrors `user reset-password`), org-ringed. is_admin,
    ADMIN scope (#270 r2-apiauth-10: a password reset is an account-security action,
    so a docs-scoped (MCP) key is refused here). Body: password (the NEW password;
    policy-enforced). SETS the password and returns ONLY ok+email — it NEVER returns
    or logs a plaintext password (the safe mirror of the CLI, which sets a supplied
    password and reveals nothing)."""
    from app.kernel import audit, security

    ident, org_id, blocked = _gate_admin(request)
    if blocked:
        return blocked
    b = _admin_block(ident)
    if b:
        return b
    body = await _json(request)
    new_password = body.get("password")
    if not isinstance(new_password, str) or not new_password:
        return JSONResponse({"ok": False, "error": "password is required."}, status_code=400)
    with Session(engine) as s:
        u = _user_in_org(s, _int_id(uid), org_id)
        if u is None:
            return JSONResponse(_NOT_FOUND, status_code=404)
        ok, msg = security.admin_set_password(s, u.id, new_password, actor=ident.get("actor", ""))
        email = u.email
    if not ok:
        return JSONResponse({"ok": False, "error": msg}, status_code=400)
    audit.log(
        actor=ident.get("actor", ""),
        action="user.password_reset",
        target=email,
        detail="must change at next sign-in",
    )
    # Password policy: an admin-set password is temporary; the person must choose their
    # own at the next sign-in (web). The response stays exactly {ok, email}.
    return JSONResponse({"ok": True, "email": email})


@router.get("/api/users/{uid}/mfa")
async def api_user_mfa_status(request: Request, uid: int):
    """Two-step sign-in status (mirrors `user mfa-status` and the Users page column),
    org-ringed. is_admin, docs scope. Never a secret or a code: enrolled, when it was
    turned on and last used, recovery codes left, whether it applies to the account
    (password sign-in) and whether the `mfa_required` policy makes it mandatory."""
    from app.kernel import mfa

    ident, org_id, blocked = _gate(request)
    if blocked:
        return blocked
    b = _admin_block(ident)
    if b:
        return b
    with Session(engine) as s:
        u = _user_in_org(s, _int_id(uid), org_id)
        if u is None:
            return JSONResponse(_NOT_FOUND, status_code=404)
        s.expunge(u)
    return JSONResponse({"ok": True, "id": u.id, "email": u.email, **mfa.status(u)})


@router.post("/api/users/{uid}/unlock")
def api_user_unlock(request: Request, uid: int):
    """Org-ringed admin unlock of the account password budget."""
    from app.kernel import login_throttle

    ident, org_id, blocked = _gate(request)
    if blocked:
        return blocked
    blocked = _admin_block(ident)
    if blocked:
        return blocked
    with Session(engine) as s:
        user = _user_in_org(s, _int_id(uid), org_id)
        if user is None:
            return JSONResponse(_NOT_FOUND, status_code=404)
        target = user.id
    return JSONResponse(login_throttle.unlock(target, actor=ident.get("actor") or "system"))


@router.delete("/api/users/{uid}/mfa")
async def api_user_mfa_reset(request: Request, uid: int):
    """Admin reset of a person's two-step sign-in (mirrors `user mfa-reset` and the Users
    page Reset button), org-ringed. is_admin, ADMIN scope (#270 r2-apiauth-10: an MFA
    reset is an account-security action, so a docs-scoped (MCP) key is refused here).
    Deletes the authenticator setup and every recovery code and bumps the person's
    session epoch (every web session ends). Their API keys are NOT touched (revoke
    them separately if a key was exposed)."""
    from app.kernel import audit, mfa

    ident, org_id, blocked = _gate_admin(request)
    if blocked:
        return blocked
    b = _admin_block(ident)
    if b:
        return b
    with Session(engine) as s:
        u = _user_in_org(s, _int_id(uid), org_id)
        if u is None:
            return JSONResponse(_NOT_FOUND, status_code=404)
        target = u.id
    res = mfa.reset(target)
    if not res["ok"]:
        return JSONResponse(_NOT_FOUND, status_code=404)
    audit.log(
        actor=ident.get("actor", ""),
        action="mfa.reset",
        target=res["email"],
        detail="was on" if res["was_enrolled"] else "was not set up",
    )
    return JSONResponse({"ok": True, "email": res["email"], "was_enrolled": res["was_enrolled"]})


@router.post("/api/users/invite")
async def api_user_invite(request: Request):
    """Create an invite (mirrors `user invite`). is_admin, ADMIN scope (#270
    r2-apiauth-10: an invite CREATES a user with the chosen role, admin included,
    so a docs-scoped (MCP) key is refused here, same as user create). Body:
    email, role. Returns the accept token ONCE (an invite credential, shown once
    like the CLI) + the accept path. The invited account lands in the caller's
    org."""
    from app.kernel import audit, security
    from app.models import Role

    ident, _org_id, blocked = _gate_admin(request)
    if blocked:
        return blocked
    b = _admin_block(ident)
    if b:
        return b
    body = await _json(request)
    email = (body.get("email") or "").strip()
    if not email:
        return JSONResponse({"ok": False, "error": "email is required."}, status_code=400)
    role = (body.get("role") or "").strip()
    role = role if role in Role.ALL else "translator"
    # BUG-A3-1: a malformed email is refused at the shared seam -> clean 400, not a 500.
    try:
        inv = security.create_invite(
            email,
            role,
            ident.get("actor", ""),
            org_id=getattr(ident.get("user"), "org_id", ident.get("org_id")),
        )
    except ValueError as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
    audit.log(
        actor=ident.get("actor", ""), action="user.invite", target=inv.email, detail="link-shown"
    )
    return JSONResponse(
        {
            "ok": True,
            "id": inv.id,
            "email": inv.email,
            "role": inv.role,
            "token": inv.token,
            "accept_path": f"/auth/accept/{inv.token}",
        }
    )


# --- GDPR (admin scope) ----------------------------------------------------
@router.get("/api/users/{uid}/export")
def api_user_export(request: Request, uid: int):
    """GDPR subject-access export (mirrors `user export` / `admin gdpr export`).
    `admin` scope + is_admin role, org-ringed. Streams NDJSON, one JSON object per
    line, each tagged with its `section`. Secrets are excluded by gdpr.export_user; the
    caller's org confines the stream so it never spills another org's rows."""
    from app.kernel import audit, gdpr

    ident, org_id, blocked = _gate_admin(request)
    if blocked:
        return blocked
    b = _admin_block(ident)
    if b:
        return b
    import json as _json

    from app.web.streaming import StreamingResponse

    with Session(engine) as s:
        u = _user_in_org(s, _int_id(uid), org_id)
        if u is None:
            return JSONResponse(_NOT_FOUND, status_code=404)
        target_uid, email = u.id, u.email
    audit.log(actor=ident.get("actor", ""), action="gdpr.export", target=email)

    def _stream():
        for section, rec in gdpr.export_user(target_uid, caller_org_id=org_id):
            yield _json.dumps({"section": section, **rec}, ensure_ascii=False, default=str) + "\n"

    return StreamingResponse(_stream(), media_type="application/x-ndjson")


@router.post("/api/users/{uid}/erase")
async def api_user_erase(request: Request, uid: int):
    """GDPR erasure (mirrors `user erase` / `admin gdpr erase`). DESTRUCTIVE +
    IRREVERSIBLE: `admin` scope + is_admin role + `remote_admin_enabled` + an ECHOED
    email confirm. Body MUST carry `email` equal to the target user's exact email; a
    mismatch is a clean 400 and erases NOTHING. Anonymizes the user (gdpr.erase_user);
    the last-active-admin recheck lives inside that service, so the last admin can
    never be erased."""
    from app.kernel import gdpr

    ident, org_id, blocked = _gate_admin(request)
    if blocked:
        return blocked
    b = _admin_block(ident)
    if b:
        return b
    rb = _remote_admin_block(ident)
    if rb:
        return rb
    body = await _json(request)
    with Session(engine) as s:
        u = _user_in_org(s, _int_id(uid), org_id)
        if u is None:
            return JSONResponse(_NOT_FOUND, status_code=404)
        target_uid, email = u.id, u.email
    # Echoed-confirm: the body's email must match EXACTLY, or nothing is erased.
    if (body.get("email") or "") != email:
        return JSONResponse(
            {
                "ok": False,
                "error": "confirm the erasure by echoing the user's exact "
                "email in `email`; nothing was erased.",
            },
            status_code=400,
        )
    ok, msg = gdpr.erase_user(target_uid, ident.get("actor", ""), caller_org_id=org_id)
    if not ok:
        return JSONResponse({"ok": False, "error": msg}, status_code=400)
    return JSONResponse({"ok": True, "uid": target_uid, "message": msg})
