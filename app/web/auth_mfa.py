"""Two-step sign-in (TOTP) on the web: the step after the password, forced enrollment, and
the self-service controls on the Account page (HECVAT AAAI-17 / DCTR-14).

THE LANDMINE this module is built around: every /auth/* path is exempt from the sign-in wall
in app/main.py. So the half-way state after a correct password must NOT be a logged-in
session with a flag on it (the `pwc` pattern): anything under /auth would then be reachable,
and a flag is one forgotten check away from a bypass. Instead, a correct password stores a
short-lived PENDING state in the signed session cookie (user id, the user's session epoch,
the time it was issued, where to go next) and NO `uid`. `security.session_user` therefore
answers None and every walled page still redirects to the sign-in page. Only a correct
second factor calls `security.login`, which clears the session (the pending state with it)
and mints the real one. The pending state lives at most mfa.PENDING_MAX_SECONDS (5 minutes)
and dies with an admin MFA reset or logout elsewhere (the session epoch no longer matches).

Every password door comes through `finish_password_step`: the sign-in form (local and
LDAP), and the first-admin setup wizard. Invite acceptance does not sign anyone in (it
sends the person to the sign-in form), so it is covered by the form. OIDC and SAML sign-ins
never reach it: the identity provider's own MFA applies.

Wrong codes count toward the SAME lockout as wrong passwords (security.reserve_login_attempt
then settle_login_failure, keyed on the sign-in name and the client IP), so the second
factor cannot be guessed faster than a password.

Documented in: docs/architecture/identity-and-access.md
"""

import logging
import time
from datetime import datetime, timezone
from urllib.parse import quote, urlencode

from fastapi import APIRouter, Form, Request
from fastapi.responses import JSONResponse, RedirectResponse
from markupsafe import Markup
from sqlmodel import Session

from app.db import engine
from app.kernel import access, audit, login_throttle, mfa, oidc, security
from app.models import User
from app.web.deps import _error_page, _ip, templates

router = APIRouter()

_logger = logging.getLogger("loc_tms")

#: The session key of the pending (password proven, second factor not yet) state.
PENDING_KEY = "mfa_p"
_MODES = ("verify", "enroll")
#: How far in the FUTURE an issued-at stamp may lie (clock step) before it is refused.
_FUTURE_SKEW_S = 30
#: The session key saying "this session proved a second factor" (gate round 1, LOW-3).
#: Written by complete_sign_in after a code (and by a self-service set-up); the sign-in
#: wall in app/main.py ends a session that lacks it once mfa_required applies to the user.
VERIFIED_KEY = "mf"
#: Every page this module renders shows a code form, the setup key and QR code, or the
#: one-time recovery codes: never cached, never restored by Back (gate round 1, LOW-4).
NO_STORE = {"Cache-Control": "no-store", "Pragma": "no-cache"}
#: The session key holding the self-service set-up flow (identity-02): the tag of the
#: secret this session was shown. Another session's set-up replaces the secret, and this
#: session can then neither see nor confirm it.
SETUP_FLOW_KEY = "mfa_sf"


def _sanitize_next(value) -> str:
    return oidc._sanitize_next(value or "/")


def _start_pending(request: Request, user, nxt: str, mode: str, via: str, login_key: str) -> None:
    """Replace the session with the pending state. The session is CLEARED first (fixation
    hygiene, and so no earlier `uid` can ride along), and no `uid` is written."""
    request.session.clear()
    request.session[PENDING_KEY] = {
        "u": int(user.id),
        "t": int(mfa._now()),
        "se": int(getattr(user, "session_epoch", 0) or 0),
        "n": _sanitize_next(nxt),
        "m": mode,
        "v": via,
        # The name the lockout counts on: what the person typed at the password step.
        "k": str(login_key or user.email)[:320],
        # The set-up flow (identity-02): the tag of the secret THIS pending state was shown.
        "f": "",
    }


def _read_pending(request: Request):
    """(pending, user, reason). pending is None when there is no usable pending state;
    reason is "expired" when it timed out (the page says so), else ""."""
    raw = request.session.get(PENDING_KEY)
    if raw is None:
        return None, None, ""
    try:
        uid, issued, epoch = int(raw["u"]), int(raw["t"]), int(raw["se"])
        mode, via = str(raw["m"]), str(raw.get("v") or "password")
        nxt, key = _sanitize_next(raw.get("n")), str(raw.get("k") or "")
        flow = str(raw.get("f") or "")
    except Exception:
        request.session.pop(PENDING_KEY, None)
        return None, None, ""
    age = mfa._now() - issued
    if age > mfa.PENDING_MAX_SECONDS or age < -_FUTURE_SKEW_S:
        request.session.pop(PENDING_KEY, None)
        return None, None, "expired"
    with Session(engine) as s:
        user = s.get(User, uid)
    if (
        user is None
        or not user.active
        or int(user.session_epoch or 0) != epoch
        or mode not in _MODES
        or not mfa.applies_to(user)
        or not access.user_org_active(user)
    ):
        request.session.pop(PENDING_KEY, None)
        return None, None, ""
    pending = {
        "uid": uid,
        "issued": issued,
        "mode": mode,
        "via": via,
        "next": nxt,
        "key": key or user.email,
        "flow": flow,
    }
    return pending, user, ""


def _set_mode(request: Request, mode: str) -> None:
    """Switch the pending state between the code page and the set-up page (reassigned, not
    mutated in place, so the session is always re-signed)."""
    state = dict(request.session.get(PENDING_KEY) or {})
    if state:
        state["m"] = mode
        request.session[PENDING_KEY] = state


def _set_flow(request: Request, pending: dict, tag: str) -> None:
    """Bind the set-up secret this pending state was shown to it (identity-02)."""
    state = dict(request.session.get(PENDING_KEY) or {})
    if state:
        state["f"] = tag
        request.session[PENDING_KEY] = state
    pending["flow"] = tag


def end_unverified_session(request: Request, user, path: str):
    """Gate round 1, LOW-3 (called by the sign-in wall in app/main.py): `mfa_required` now
    applies to a signed-in user whose session never proved a second factor (the policy was
    raised, or they were promoted to admin under "admins"). End the session, audit it, and
    send them to sign in again, which asks for the code or for set-up first. A fetch call
    (X-CSRF-Token) gets 401 JSON instead of a redirect."""
    audit.log(
        actor=user.email,
        action="mfa.session_ended",
        detail="two-step sign-in is now required for this account; sign in again",
        ip=_ip(request),
    )
    request.session.clear()
    if request.headers.get("x-csrf-token"):
        return JSONResponse({"error": "mfa_required"}, status_code=401, headers=NO_STORE)
    nxt = quote(path if request.method == "GET" else "/", safe="/")
    return RedirectResponse("/auth/login?err=mfa_signin_again&next=" + nxt, status_code=303)


def _to_login(reason: str):
    """Back to the sign-in page. A timed-out step says so (the error summary announces it,
    and signing in again is the recovery)."""
    if reason == "expired":
        return RedirectResponse("/auth/login?err=mfa_expired", status_code=303)
    return RedirectResponse("/auth/login", status_code=303)


def _deadline(pending: dict) -> str:
    """When the pending step ends, as HH:MM UTC, for the page to announce."""
    end = datetime.fromtimestamp(pending["issued"] + mfa.PENDING_MAX_SECONDS, tz=timezone.utc)
    return end.strftime("%H:%M UTC")


def finish_password_step(request: Request, user, nxt: str, *, via: str, login_key: str = ""):
    """The ONE continuation after a correct password (sign-in form, LDAP, setup wizard).

    Two-step sign-in ON for the account -> the pending state and the code page. Required by
    `mfa_required` but not set up -> the pending state and the set-up page (still not signed
    in). Otherwise -> signed in exactly as before."""
    mode = None
    if mfa.applies_to(user):
        if mfa.is_enrolled(user.id):
            mode = "verify"
        elif mfa.required_for(user):
            mode = "enroll"
    if mode is None:
        if login_key:
            # The password step already released its reservation before coming here.
            security.clear_login_failures(login_key, _ip(request), reserved=False)
        dest = complete_sign_in(request, user, nxt, via=via)
        if login_key:
            login_throttle.clear(login_key)
        return login_throttle.remember_device(
            RedirectResponse(dest, status_code=303), request, user
        )
    _start_pending(request, user, nxt, mode, via, login_key)
    audit.log(
        actor=user.email, action="mfa.challenge", detail=f"{mode} after {via}", ip=_ip(request)
    )
    return RedirectResponse(
        "/auth/mfa" if mode == "verify" else "/auth/mfa/enroll", status_code=303
    )


def complete_sign_in(request: Request, user, nxt: str, *, via: str, factor: str = "") -> str:
    """Mint the real session and write the sign-in audit row; returns where to go next.

    Called only once every factor the account needs is proven. The forced password change
    (password policy) comes AFTER the second factor, so a person with an admin-reset
    password still proves both factors before choosing a new one."""
    pending = request.session.get(PENDING_KEY) or {}
    if pending.get("k"):
        login_throttle.clear(pending["k"])
    login_throttle.clear(user.email)
    security.login(request, user)
    if factor:
        request.session[VERIFIED_KEY] = 1
    ip = _ip(request)
    detail = f"mfa={factor}" if factor else ""
    if via == "ldap":
        audit.log(
            actor=user.email, action="auth.ldap_login", target=user.email, detail=detail, ip=ip
        )
    elif via != "setup" or factor:
        audit.log(actor=user.email, action="login.success", detail=detail, ip=ip)
    nxt = _sanitize_next(nxt)
    reason = None if via == "setup" else security.password_change_required(user)
    if reason:
        request.session["pwc"] = reason
        audit.log(actor=user.email, action="login.password_change_required", detail=reason, ip=ip)
        return "/auth/password-change?" + urlencode({"next": nxt})
    return nxt


def _ip_keys(pending: dict) -> list:
    """The per-address lockout names a code attempt counts under: the name typed at the
    password step AND the account's email, so LDAP name variants share one count (LOW-2)."""
    keys = [pending["key"]]
    if pending["email"].lower().strip() != pending["key"].lower().strip():
        keys.append(pending["email"])
    return keys


def _reserve_ip(keys: list, ip: str) -> bool:
    """Reserve one attempt under EVERY key before any code is checked (gate round 2,
    MEDIUM-2): concurrent requests cannot all pass a "not locked yet" check. All or none."""
    got = []
    for k in keys:
        if not security.reserve_login_attempt(k, ip):
            for g in got:
                security.release_login_attempt(g, ip)
            return False
        got.append(k)
    return True


def _release_ip(keys: list, ip: str) -> None:
    """Give back the reservation _reserve_ip took under every key, for an attempt that
    checked no code: the service refused it unchecked, or the check raised (gate round 3,
    LOW-7). Called at most once per reservation, and never after _settle_ip."""
    for k in keys:
        security.release_login_attempt(k, ip)


def _checked_code(keys: list, addr: str, check, /, *args, **kwargs):
    """Run the service's code check for a per-address reservation: on ANY exception the
    reservation is given back (exactly once, here) and the exception re-raised. Settling
    happens only after this returns, so the two paths never both touch the count. (The
    first three are positional-only, so the check's own `ip=` keyword passes through.)"""
    try:
        return check(*args, **kwargs)
    except BaseException:
        _release_ip(keys, addr)
        raise


def _settle_ip(keys: list, ip: str, ok: bool) -> bool:
    """A reserved attempt finished: a right code clears every key, a wrong one stays
    counted (and may lock). Returns True when a key locked."""
    if ok:
        for k in keys:
            security.clear_login_failures(k, ip)
        return False
    tripped = False
    for k in keys:
        tripped = security.settle_login_failure(k, ip) or tripped
    return tripped


def _locked(pending: dict, request: Request, *, recovery: bool = False) -> bool:
    """Is the code step closed right now? Per address under every key, plus the
    per-ACCOUNT budget (MEDIUM-1), except for a recovery code, which bypasses the account
    lock so the real owner can still get in (LOW-5)."""
    ip = _ip(request)
    if any(security.login_locked_seconds(k, ip) for k in _ip_keys(pending)):
        return True
    return not recovery and bool(mfa.account_locked_seconds(pending["uid"]))


def _refuse_locked(request: Request, user):
    request.session.pop(PENDING_KEY, None)
    audit.log(actor=user.email, action="login.locked", detail="two-step sign-in", ip=_ip(request))
    return RedirectResponse("/auth/login?err=mfa_locked", status_code=303)


def _fail(pending: dict, user, request: Request, what: str, *, recovery: bool = False) -> bool:
    """Settle a wrong code (already reserved under every key) and report whether the code
    step is now closed: a per-address lock, or the per-ACCOUNT lock the service manages
    (not for a recovery code, which bypasses it). Returns True when closed."""
    ip = _ip(request)
    tripped = _settle_ip(_ip_keys(pending), ip, ok=False)
    if not recovery:
        tripped = tripped or bool(mfa.account_locked_seconds(user.id))
    audit.log(
        actor=user.email,
        action="mfa.failed",
        detail=what + ("; locked" if tripped else ""),
        ip=ip,
    )
    if tripped:
        audit.log(actor=user.email, action="login.lockout", detail="two-step sign-in", ip=ip)
        request.session.pop(PENDING_KEY, None)
    return tripped


def _qr(secret: str, account: str) -> dict:
    uri = mfa.otpauth_uri(secret, account)
    # segno builds this SVG from the encoded modules only; nothing typed by a person is
    # written into the markup, so it is safe to mark it as markup.
    return {
        "qr_svg": Markup(mfa.qr_svg(uri)),  # noqa: S704 -- generated SVG, no user text
        "secret_text": mfa.format_secret(secret),
        "account": account,
    }


# --- the sign-in step --------------------------------------------------------------------
def _verify_page(
    request: Request, pending: dict, user, error: str = "", field: str = "", status=200
):
    return templates.TemplateResponse(
        request,
        "mfa_verify.html",
        {
            "request": request,
            "email": user.email,
            "deadline": _deadline(pending),
            "minutes": mfa.PENDING_MAX_SECONDS // 60,
            "error": error or None,
            "error_field": field,
            "recovery_open": field == "mfa-recovery",
        },
        status_code=status,
        headers=NO_STORE,
    )


@router.get("/auth/mfa")
def mfa_verify_page(request: Request):
    pending, user, reason = _read_pending(request)
    if pending is None:
        return _to_login(reason)
    if not mfa.is_enrolled(user.id):
        if mfa.required_for(user):
            _set_mode(request, "enroll")
            return RedirectResponse("/auth/mfa/enroll", status_code=303)
        # Turned off in another session while this one waited: the password was the
        # whole sign-in the policy asks for.
        dest = complete_sign_in(request, user, pending["next"], via=pending["via"])
        return login_throttle.remember_device(
            RedirectResponse(dest, status_code=303), request, user
        )
    if pending["mode"] != "verify":
        _set_mode(request, "verify")
    return _verify_page(request, pending, user)


@router.post("/auth/mfa")
def mfa_verify_post(request: Request, code: str = Form(""), recovery: str = Form("")):
    pending, user, reason = _read_pending(request)
    if pending is None:
        return _to_login(reason)
    pending["email"] = user.email
    use_recovery = bool((recovery or "").strip())
    if _locked(pending, request, recovery=use_recovery):
        return _refuse_locked(request, user)
    keys, ip = _ip_keys(pending), _ip(request)
    if not _reserve_ip(keys, ip):
        return _refuse_locked(request, user)
    kind = _checked_code(keys, ip, mfa.verify, user.id, recovery if use_recovery else code, ip=ip)
    if kind is mfa.REFUSED:
        # Refused unchecked (the account budget is held or locked): no code was looked at,
        # so no per-address failure is counted, and the person is told code entry is
        # paused, not that a possibly right code was wrong (LOW-7).
        _release_ip(keys, ip)
        return _refuse_locked(request, user)
    if not kind:
        what = "recovery code" if use_recovery else "authenticator code"
        if _fail(pending, user, request, what, recovery=use_recovery):
            return RedirectResponse("/auth/login?err=mfa_locked", status_code=303)
        if use_recovery:
            msg = (
                "That recovery code is not correct or was already used. Check it and try "
                "again, or use the code from your authenticator app."
            )
        else:
            msg = (
                "That code is not correct or has already been used. Wait for the next code "
                "in your authenticator app, then type it again."
            )
        return _verify_page(
            request,
            pending,
            user,
            msg,
            "mfa-recovery" if use_recovery else "mfa-code",
            401,
        )
    ip = _ip(request)
    _settle_ip(_ip_keys(pending), ip, ok=True)
    audit.log(actor=user.email, action="mfa.success", detail=kind, ip=ip)
    if kind == "recovery":
        left = mfa.recovery_codes_left(user.id)
        audit.log(
            actor=user.email, action="mfa.recovery_used", detail=f"{left} code(s) left", ip=ip
        )
    dest = complete_sign_in(request, user, pending["next"], via=pending["via"], factor=kind)
    return login_throttle.remember_device(RedirectResponse(dest, status_code=303), request, user)


@router.post("/auth/mfa/cancel")
def mfa_cancel(request: Request):
    """Leave the half-way state (the page's "Start over" button). Clears only that state.
    POST (CSRF-checked), not GET: a cross-site link must not be able to abort a sign-in (#546)."""
    request.session.pop(PENDING_KEY, None)
    return RedirectResponse("/auth/login", status_code=303)


# --- forced enrollment (mfa_required applies, not yet set up) ----------------------------
def _enroll_page(request: Request, pending: dict, user, error: str = "", status=200):
    # _read_pending has already validated the recent password sign-in and its epoch.
    # identity-02: only THIS pending state's own flow resumes its secret; a new password
    # step (another person who knows the password, or the owner after them) gets a fresh
    # one, so an abandoned set-up never hands its secret to the next person.
    secret = mfa._begin_enrollment(user.id, pending.get("flow", ""))
    _set_flow(request, pending, mfa.flow_tag(secret))
    return templates.TemplateResponse(
        request,
        "mfa_enroll.html",
        {
            "request": request,
            "email": user.email,
            "deadline": _deadline(pending),
            "minutes": mfa.PENDING_MAX_SECONDS // 60,
            "error": error or None,
            "error_field": "mfa-enroll-code" if error else "",
            "form_action": "/auth/mfa/enroll",
            **_qr(secret, user.email),
        },
        status_code=status,
        headers=NO_STORE,
    )


@router.get("/auth/mfa/enroll")
def mfa_enroll_page(request: Request):
    pending, user, reason = _read_pending(request)
    if pending is None:
        return _to_login(reason)
    if mfa.is_enrolled(user.id):
        _set_mode(request, "verify")
        return RedirectResponse("/auth/mfa", status_code=303)
    if not mfa.required_for(user):
        # The requirement was lifted while this step waited: the password was enough.
        dest = complete_sign_in(request, user, pending["next"], via=pending["via"])
        return login_throttle.remember_device(
            RedirectResponse(dest, status_code=303), request, user
        )
    return _enroll_page(request, pending, user)


@router.post("/auth/mfa/enroll")
def mfa_enroll_post(request: Request, code: str = Form("")):
    pending, user, reason = _read_pending(request)
    if pending is None:
        return _to_login(reason)
    if mfa.is_enrolled(user.id):
        _set_mode(request, "verify")
        return RedirectResponse("/auth/mfa", status_code=303)
    pending["email"] = user.email
    if _locked(pending, request):
        return _refuse_locked(request, user)
    keys, ip = _ip_keys(pending), _ip(request)
    if not _reserve_ip(keys, ip):
        return _refuse_locked(request, user)
    codes = _checked_code(
        keys, ip, mfa.confirm_enrollment, user.id, code, ip=ip, flow=pending.get("flow", "")
    )
    if codes is mfa.REFUSED:
        _release_ip(keys, ip)  # nothing was checked (LOW-7)
        return _refuse_locked(request, user)
    if not codes:
        if _fail(pending, user, request, "enrollment code"):
            return RedirectResponse("/auth/login?err=mfa_locked", status_code=303)
        return _enroll_page(
            request,
            pending,
            user,
            "That code is not correct. Check that the app shows this account, wait for a new "
            "code, then type it again.",
            401,
        )
    ip = _ip(request)
    _settle_ip(_ip_keys(pending), ip, ok=True)
    audit.log(actor=user.email, action="mfa.enroll", detail="required at sign-in", ip=ip)
    audit.log(actor=user.email, action="mfa.success", detail="totp", ip=ip)
    user.session_epoch = codes.session_epoch
    dest = complete_sign_in(request, user, pending["next"], via=pending["via"], factor="totp")
    # The one time these codes are shown. Rendered straight into this response (never a
    # redirect, never stored): the person is signed in now and continues from the page.
    response = templates.TemplateResponse(
        request,
        "mfa_codes_standalone.html",
        {"request": request, "codes": codes, "continue_href": dest, "email": user.email},
        headers=NO_STORE,
    )
    return login_throttle.remember_device(response, request, user)


# --- self-service (Account page) ---------------------------------------------------------
def _me(request: Request):
    me = security.session_user(request)
    if not me or not getattr(me, "id", 0):
        return None
    return me


def _account_page(request: Request, me, status: int = 200, **ctx):
    return templates.TemplateResponse(
        request,
        "account_mfa.html",
        {"request": request, "me": me, **ctx},
        status_code=status,
        headers=NO_STORE,
    )


def _account_begin(me, request: Request) -> bool:
    """Open one code check on an Account page: refused while this address is locked out,
    else one attempt is RESERVED up front (MEDIUM-2). The per-ACCOUNT budget is enforced
    by the service itself (a recovery code bypasses it, LOW-5)."""
    ip = _ip(request)
    if security.login_locked_seconds(me.email, ip):
        return False
    return security.reserve_login_attempt(me.email, ip)


def _account_check(me, request: Request, check, code):
    """Run the service's code check for the reservation _account_begin took; an exception
    gives it back and re-raises (LOW-7)."""
    return _checked_code([me.email], _ip(request), check, me.id, code, ip=_ip(request))


def _account_fail(me, request: Request, what: str, result=None) -> bool:
    """Settle a wrong Account-page code. True when the address or the account is now locked
    (the page then says to wait). A code the service REFUSED unchecked (the account budget
    is held or locked) is not counted for the address: the reservation is given back and
    the page says to wait (LOW-7)."""
    ip = _ip(request)
    if result is mfa.REFUSED:
        _release_ip([me.email], ip)
        audit.log(actor=me.email, action="mfa.failed", detail=what + "; locked", ip=ip)
        return True
    tripped = security.settle_login_failure(me.email, ip)
    tripped = tripped or bool(mfa.account_locked_seconds(me.id))
    audit.log(
        actor=me.email, action="mfa.failed", detail=what + ("; locked" if tripped else ""), ip=ip
    )
    return tripped


_NOT_FOR_SSO = (
    "Two-step sign-in here is for accounts that sign in with a password on this site. Your "
    "account signs in through single sign-on, so your identity provider's own multi-factor "
    "sign-in applies."
)


@router.get("/account/mfa/setup")
def account_mfa_setup(request: Request):
    me = _me(request)
    if me is None:
        return RedirectResponse("/account", status_code=303)
    if not mfa.applies_to(me):
        return _error_page(request, 400, _NOT_FOR_SSO)
    if mfa.is_enrolled(me.id):
        return RedirectResponse("/account#mfa", status_code=303)
    if not me.password_hash and security.auth_source_of(me) != "ldap":
        # #423: no password here means nothing to prove: refuse set-up (it used to be
        # shown with no re-authentication, so any session holder could plant a factor).
        return _error_page(request, 400, mfa.NO_LOCAL_SIGNIN_REFUSAL)
    return _account_page(request, me, mode="reauth", form_action="/account/mfa/setup")


@router.post("/account/mfa/setup")
def account_mfa_setup_post(request: Request, code: str = Form(""), current: str = Form("")):
    me = _me(request)
    if me is None:
        return RedirectResponse("/account", status_code=303)
    if not mfa.applies_to(me):
        return _error_page(request, 400, _NOT_FOR_SSO)
    if mfa.is_enrolled(me.id):
        return RedirectResponse("/account#mfa", status_code=303)
    if not me.password_hash and security.auth_source_of(me) != "ldap":
        # #423: refused, also for a grant or code posted from an older page.
        request.session.pop("mfa_setup_reauth", None)
        request.session.pop(SETUP_FLOW_KEY, None)
        return _error_page(request, 400, mfa.NO_LOCAL_SIGNIN_REFUSAL)
    grant = request.session.get("mfa_setup_reauth", 0)
    if not grant or not 0 <= time.time() - grant < 300:
        if not current:
            request.session.pop("mfa_setup_reauth", None)
            return _account_page(
                request,
                me,
                400,
                mode="reauth",
                error="Enter your current password",
                error_field="mfa-current",
                form_action="/account/mfa/setup",
            )
        try:
            # identity-02: a new grant is a new flow, so it always gets a fresh secret.
            secret = mfa.begin_enrollment(me.id, current, ip=_ip(request))
        except mfa.MfaError as exc:
            return _account_page(
                request, me, 400, mode="reauth", error=str(exc), error_field="mfa-current"
            )
        request.session["mfa_setup_reauth"] = time.time()
        request.session[SETUP_FLOW_KEY] = mfa.flow_tag(secret)
        return _account_page(
            request, me, mode="setup", form_action="/account/mfa/setup", **_qr(secret, me.email)
        )
    flow = str(request.session.get(SETUP_FLOW_KEY) or "")
    secret = mfa.pending_secret(me.id, flow=flow)
    if not secret:
        # No set-up started, or a newer set-up (another session) replaced this one's.
        request.session.pop("mfa_setup_reauth", None)
        request.session.pop(SETUP_FLOW_KEY, None)
        return RedirectResponse("/account/mfa/setup", status_code=303)

    def _again(msg: str):
        return _account_page(
            request,
            me,
            400,
            mode="setup",
            form_action="/account/mfa/setup",
            error=msg,
            error_field="mfa-enroll-code",
            **_qr(secret, me.email),
        )

    if not _account_begin(me, request):
        return _again("Too many wrong codes. Wait 15 minutes, then try again.")
    ip = _ip(request)
    codes = _checked_code([me.email], ip, mfa.confirm_enrollment, me.id, code, ip=ip, flow=flow)
    if not codes:
        if _account_fail(me, request, "enrollment code", codes):
            return _again("Too many wrong codes. Wait 15 minutes, then try again.")
        return _again(
            "That code is not correct. Check that the app shows this account, wait for a new "
            "code, then type it again."
        )
    security.clear_login_failures(me.email, _ip(request))
    # This session has now proved the new factor (LOW-3's wall flag).
    request.session[VERIFIED_KEY] = 1
    request.session["se"] = codes.session_epoch
    request.session.pop("mfa_setup_reauth", None)
    request.session.pop(SETUP_FLOW_KEY, None)
    audit.log(actor=me.email, action="mfa.enroll", detail="self-service", ip=_ip(request))
    return _account_page(request, me, mode="codes", codes=codes)


@router.post("/account/mfa/recovery-codes")
def account_mfa_recovery_codes(request: Request, code: str = Form("")):
    """A new set of recovery codes, shown once. Needs a current code (authenticator or an
    unused recovery code), which counts toward the lockout when wrong."""
    me = _me(request)
    if me is None:
        return RedirectResponse("/account", status_code=303)
    if not mfa.is_enrolled(me.id):
        return RedirectResponse("/account?err=mfa_not_on", status_code=303)
    if not _account_begin(me, request):
        return RedirectResponse("/account?err=mfa_wait", status_code=303)
    kind = _account_check(me, request, mfa.verify, code)
    if not kind:
        if _account_fail(me, request, "code to replace recovery codes", kind):
            return RedirectResponse("/account?err=mfa_wait", status_code=303)
        return RedirectResponse("/account?err=mfa_regen_code", status_code=303)
    security.clear_login_failures(me.email, _ip(request))
    codes = mfa.regenerate_recovery_codes(me.id)
    audit.log(
        actor=me.email,
        action="mfa.recovery_codes",
        detail=f"new set of {len(codes)}",
        ip=_ip(request),
    )
    return _account_page(request, me, mode="codes", codes=codes)


@router.post("/account/mfa/disable")
def account_mfa_disable(request: Request, code: str = Form(""), current: str = Form("")):
    """Turn two-step sign-in off. Needs a current code, and is refused while
    `mfa_required` applies to this account."""
    me = _me(request)
    if me is None:
        return RedirectResponse("/account", status_code=303)
    if not mfa.is_enrolled(me.id):
        return RedirectResponse("/account?err=mfa_not_on", status_code=303)
    if mfa.required_for(me):
        return RedirectResponse("/account?err=mfa_disable_required", status_code=303)
    try:
        # #546: a right password must not wipe the (email, ip) failed-code count below.
        mfa.require_current_password(me.id, current, ip=_ip(request), clear_on_success=False)
    except mfa.MfaError:
        return RedirectResponse("/account?err=mfa_current", status_code=303)
    if not _account_begin(me, request):
        return RedirectResponse("/account?err=mfa_wait", status_code=303)
    kind = _account_check(me, request, mfa.verify, code)
    if not kind:
        if _account_fail(me, request, "code to turn off", kind):
            return RedirectResponse("/account?err=mfa_wait", status_code=303)
        return RedirectResponse("/account?err=mfa_disable_code", status_code=303)
    security.clear_login_failures(me.email, _ip(request))
    try:
        request.session["se"] = mfa._disable(me.id)
    except mfa.MfaError as exc:
        return _error_page(request, 400, str(exc))
    audit.log(actor=me.email, action="mfa.disable", detail="self-service", ip=_ip(request))
    return RedirectResponse("/account?saved=mfa_off#mfa", status_code=303)
