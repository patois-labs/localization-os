"""Authentication (setup/login/logout/SSO/invites) and self-service account.

Documented in: docs/architecture/identity-and-access.md
"""

import json
import logging
import re
from datetime import datetime
from urllib.parse import urlencode

from fastapi import APIRouter, Form, Request
from fastapi.responses import JSONResponse, RedirectResponse, Response

from app.i18n import loader as i18n
from app.kernel import (
    access,
    audit,
    gdpr,
    ldap_auth,
    login_throttle,
    mfa,
    oidc,
    onboarding,
    saml,
    security,
    settings,
)
from app.models import Role
from app.web import auth_mfa, form_errors, one_time_stash
from app.web import route_path as routing
from app.web.deps import WorkerBusy, _error_page, _ip, offload_parse, t, templates
from app.web.streaming import StreamingResponse

router = APIRouter()

_logger = logging.getLogger("loc_tms")


def _attempt_attribution(
    request: Request, email: str, *, directory_verified: bool = False
) -> tuple[str, str]:
    """(actor, detail) for a failed or locked sign-in attempt.

    The actor names an account ONLY when the typed name resolves to an existing one, exactly
    as before for a real address (org-scoped audit views match the actor against member
    emails, so an org admin keeps seeing failed sign-ins on its own people). Anything else
    (a typo, a probe, a reserved word, a zero-width or lookalike variant of "system" or
    "unauthenticated <address>") is attributed to the caller, `unauthenticated <ip>`, and
    the typed text goes into detail as evidence: sanitized and quoted (`typed-name="..."`)
    when it is an email address at a domain an account uses or a directory-verified
    username, otherwise masked
    (`typed-name-masked id=<hex>`), since it may be a password typed into the wrong
    box (gate audit-actor round 2, LOW-6; see security.typed_name_detail).
    Gate audit-actor round 1, MEDIUM-1: free text typed by a stranger never becomes an
    actor, so no rendering trick can make a row read as the app or as another caller."""
    try:
        user = security.get_by_email(email)
    except Exception:
        user = None
    if user is not None and getattr(user, "email", ""):
        return user.email, ""
    detail = security.typed_name_detail(email, directory_verified=directory_verified)
    return security.unauthenticated_actor(request), detail


_SETUP_REFUSED_MSG = (
    "That setup token is missing or wrong. The server prints it in its log at startup "
    "(it is also in the .setup_token file in the data folder, or LOC_TMS_SETUP_TOKEN)."
)


@router.get("/auth/setup")
def auth_setup(request: Request):
    if security.has_users():
        return RedirectResponse("/auth/login", status_code=303)
    return templates.TemplateResponse(
        request,
        "setup.html",
        {"request": request, "token_required": security.setup_token_required()},
    )


@router.post("/auth/setup")
def auth_setup_post(
    request: Request,
    email: str = Form(...),
    name: str = Form(""),
    password: str = Form(...),
    setup_token: str = Form(""),
):
    if security.has_users():
        return RedirectResponse("/auth/login", status_code=303)
    # a11y T6: a refusal re-renders with the name and email echoed back (never the
    # password) and names the field it is about, so the page can mark it aria-invalid.
    echo = {
        "request": request,
        "name": name[:200],
        "email": email[:320],
        "token_required": security.setup_token_required(),
    }
    # #414: a server install creates its first admin only for the operator, who can read
    # the one-time setup token from the server log, DATA_DIR/.setup_token or the env.
    if not security.setup_token_ok(setup_token):
        # Another request may have claimed the install and removed the token after
        # our first probe. Late submissions still land on sign-in.
        if security.has_users():
            return RedirectResponse("/auth/login", status_code=303)
        _logger.warning("First-run setup refused: missing or wrong setup token (%s)", _ip(request))
        return templates.TemplateResponse(
            request,
            "setup.html",
            {**echo, "error": _SETUP_REFUSED_MSG, "error_field": "setup-token"},
            status_code=403,
        )
    issue = security.password_issue(password, user=email)
    if issue:
        return templates.TemplateResponse(
            request,
            "setup.html",
            {**echo, "error": issue, "error_field": "setup-password"},
            status_code=400,
        )
    # BUG-A3-1: a malformed admin email is refused at the shared seam — re-render the
    # setup form with the message instead of a 500.
    # #181: the FIRST account on an empty install (user_count() == 0 above), before any
    # org context exists, so it opts out of the org-less refusal on purpose.
    # #414: first_account_only makes check-and-create one locked transaction, so of any
    # number of concurrent posts exactly one creates the admin; the rest go to sign-in.
    try:
        u = security.create_user(
            email, name, Role.ADMIN, password, allow_orgless_boot=True, first_account_only=True
        )
    except security.FirstAccountExists:
        return RedirectResponse("/auth/login", status_code=303)
    except ValueError as exc:
        return templates.TemplateResponse(
            request,
            "setup.html",
            {**echo, "error": str(exc), "error_field": "setup-email"},
            status_code=400,
        )
    security.clear_setup_token()
    audit.log(actor=u.email, action="setup.create_admin", target=u.email, ip=_ip(request))
    # TOTP train: the first admin is signed in straight away unless mfa_required already
    # applies to them (set on the host before setup), in which case they set up two-step
    # sign-in first, still not signed in (app/web/auth_mfa.py).
    return auth_mfa.finish_password_step(request, u, "/", via="setup", login_key=email)


@router.get("/auth/login")
def auth_login(request: Request, next: str = "/"):
    # same open-redirect sanitizer as the OIDC path: only same-site paths survive
    next = oidc._sanitize_next(next)
    return templates.TemplateResponse(request, "login.html", _login_context(request, next))


def _login_context(request: Request, next: str, **extra) -> dict:
    """The ONE login.html context. The GET page and every error re-render build from it,
    so the SSO / SAML / LDAP affordances cannot drift apart again (a11y T6: the SAML
    button used to vanish after one failed password because the error path omitted
    saml_enabled)."""
    ctx = {
        "request": request,
        "next": next,
        "sso_enabled": settings.get("sso_enabled") == "1",
        "sso_provider": settings.get("sso_provider"),
        "ldap_enabled": settings.get("ldap_enabled") == "1",
        "saml_enabled": settings.get("saml_enabled") == "1",
        # #252: an existence probe (this renders on every failed or locked sign-in).
        "needs_setup": not security.has_users(),
        "username": "",
    }
    ctx.update(extra)
    return ctx


def _login_error(
    request: Request,
    next: str,
    error: str,
    status: int,
    username: str = "",
    credential_error: bool = False,
):
    """Re-render the sign-in page with an error. ``username`` is echoed back into the
    field (never the password) so one typo does not cost the whole form;
    ``credential_error`` marks both fields aria-invalid (a wrong username or password)."""
    return templates.TemplateResponse(
        request,
        "login.html",
        _login_context(
            request,
            next,
            error=error,
            username=(username or "")[:320],
            credential_error=credential_error,
        ),
        status_code=status,
    )


def _password_checked(email: str, ip: str, check, *args):
    """Run one password check for the attempt reserved under (email, ip): on ANY exception
    (a database error, a directory fault) the reservation is given back, exactly once, and
    the exception re-raised, so a server-side fault never counts as a failed login (TOTP
    gate round 3, LOW-7). Settling or releasing after a normal answer happens outside it."""
    try:
        return check(*args)
    except BaseException:
        security.release_login_attempt(email, ip)
        raise


@router.post("/auth/login")
def auth_login_post(
    request: Request, email: str = Form(...), password: str = Form(...), next: str = Form("/")
):
    """Reserve the durable password budget before either verifier, including LDAP."""
    if len(email) > security.EMAIL_MAX_LEN:
        # #384: no account can ever own a name longer than EMAIL_MAX_LEN (validate_email
        # refuses it at every create door), so this is the ordinary invalid-credentials
        # answer, given BEFORE any lookup, reservation or verifier touches the name so
        # attacker-sized text never reaches the limiter state. The typed text is kept
        # only as masked audit evidence, and the field is not echoed back.
        audit.log(
            actor=security.unauthenticated_actor(request),
            action="login.failed",
            detail=security.typed_name_detail(email),
            ip=_ip(request),
        )
        return _login_error(
            request,
            oidc._sanitize_next(next),
            t(request, "errors.login_invalid"),
            401,
            credential_error=True,
        )
    user = security.get_by_email(login_throttle.account_name(email))
    trusted = login_throttle.known_device(request.cookies.get(login_throttle.DEVICE_COOKIE), user)
    held = None if trusted else login_throttle.reserve(email, _ip(request))
    if not trusted and held is None:
        return _login_error(
            request,
            oidc._sanitize_next(next),
            t(request, "errors.login_account_locked"),
            429,
            username=email,
        )
    reservations = [held]
    directory_context = login_throttle.start_directory_context(
        email, _ip(request), request.cookies.get(login_throttle.DEVICE_COOKIE), reservations
    )
    try:
        response = _password_login_post(request, email, password, next)
    except login_throttle.AccountLocked:
        for reservation in reservations:
            login_throttle.settle(reservation, failed=False)
        return _login_error(
            request,
            oidc._sanitize_next(next),
            t(request, "errors.login_account_locked"),
            429,
            username=email,
        )
    except BaseException:
        for reservation in reservations:
            login_throttle.settle(reservation, failed=False)
        raise
    finally:
        login_throttle.end_directory_context(directory_context)
    # The address layer can refuse before a check or on its fifth failed check.
    # Only completed password failures spend the separate account budget.
    failed = getattr(request.state, "password_failed", False)
    tripped = False
    for reservation in reservations:
        tripped = login_throttle.settle(reservation, failed=failed) or tripped
    if tripped:
        return _login_error(
            request,
            oidc._sanitize_next(next),
            t(request, "errors.login_account_locked"),
            429,
            username=email,
        )
    return response


def _password_login_post(request: Request, email: str, password: str, next: str):
    # never redirect off-site from the login form (open redirect / phishing)
    next = oidc._sanitize_next(next)
    ip = _ip(request)
    locked_s = security.login_locked_seconds(email, ip)
    if locked_s:
        who, typed = _attempt_attribution(request, email)
        audit.log(actor=who, action="login.locked", detail=typed, ip=ip)
        return _login_error(
            request,
            next,
            t(request, "errors.login_locked_wait", minutes=max(1, locked_s // 60)),
            status=429,
            username=email,
        )
    # TOTP gate round 2 (MEDIUM-2 pattern): RESERVE this attempt before any password is
    # checked, so concurrent wrong passwords can never check more than LOCKOUT_THRESHOLD
    # per (name, ip) window. A refusal here is the same fast 429 as the locked branch just
    # above (as before this change, a locked pair is answered without an Argon2 verify:
    # the lock is driven by the caller's own attempts on the typed name, known or not, so
    # it says nothing about whether the account exists). A correct password gives the
    # reservation back (nothing was guessed), a wrong one settles it as a failure.
    if not security.reserve_login_attempt(email, ip):
        who, typed = _attempt_attribution(request, email)
        audit.log(actor=who, action="login.locked", detail=typed, ip=ip)
        wait_s = security.login_locked_seconds(email, ip) or security.LOCKOUT_DURATION_S
        return _login_error(
            request,
            next,
            t(request, "errors.login_locked_wait", minutes=max(1, wait_s // 60)),
            status=429,
            username=email,
        )
    # LDAP first (when enabled): search-then-bind against the directory. A
    # success provisions/updates the local account and logs in. ANY LDAP
    # non-success — no match, wrong directory password, a local-admin takeover
    # refusal, or an LDAP outage — falls THROUGH to local password auth below,
    # so the seeded admin always works and an outage never blocks login. LDAP
    # is never even contacted when disabled (zero directory traffic).
    if ldap_auth.enabled():
        luser, lerr = _password_checked(email, ip, ldap_auth.try_login, email, password)
        if luser:
            # TOTP train: a correct directory password is the FIRST factor only. Two-step
            # sign-in (when on for the account, or required) comes before any session, and
            # the failure counter is cleared only once the whole sign-in succeeds.
            security.release_login_attempt(email, ip)
            return auth_mfa.finish_password_step(request, luser, next, via="ldap", login_key=email)
        if ldap_auth.is_not_provisioned(lerr):
            # GATE R3 MED-1: the ONE hard refusal with nothing to fall through to. The
            # directory proved the identity and the password; there is simply no local
            # account to provision it onto under an active multi-org gate. Falling through
            # ran security.authenticate against a password the person does not have, so a
            # correct directory password counted as a failed login and locked them out at
            # the fifth attempt, with the explanation never shown. Surface it, and do NOT
            # settle a failure: give the reservation back, nothing was guessed.
            security.release_login_attempt(email, ip)
            # The directory bind proved this name, so it stays readable (it is a real
            # directory username, not a stray password), but bounded and quoted like any
            # other caller text, in the target as well as the detail.
            who, typed = _attempt_attribution(request, email, directory_verified=True)
            audit.log(
                actor=who,
                action="auth.ldap_unprovisioned",
                target=security.typed_name_field(email),
                detail=typed,
                ip=ip,
            )
            return _login_error(request, next, lerr, 403, username=email)
        if lerr:
            # a hard refusal (e.g. LDAP identity shadowing a local password admin,
            # or a disabled account): do NOT surface it — fall through to local
            # auth so the legitimate local password still works. Just note it.
            _logger.info("LDAP login refused for %s (%s); trying local auth", email, lerr)

    u = _password_checked(email, ip, security.authenticate, email, password)
    if not u:
        request.state.password_failed = True
        # the reserved attempt stays counted; this may lock the pair
        tripped = security.settle_login_failure(email, ip)
        who, typed = _attempt_attribution(request, email)
        audit.log(
            actor=who,
            action="login.lockout" if tripped else "login.failed",
            detail=typed,
            ip=ip,
        )
        if tripped:
            return _login_error(
                request, next, t(request, "errors.login_locked"), status=429, username=email
            )
        return _login_error(
            request,
            next,
            t(request, "errors.login_invalid"),
            401,
            username=email,
            credential_error=True,
        )
    # TOTP train: the password is the FIRST factor. When two-step sign-in is on for the
    # account (or mfa_required makes it mandatory) NO session is created here: the person
    # gets a short-lived pending state and the code page (app/web/auth_mfa.py). Only then
    # auth_mfa.complete_sign_in signs them in and applies the password policy: an admin
    # reset or an expired password (password_max_age_days) sets the session flag `pwc`,
    # which the sign-in wall in app/main.py checks on every page until a successful change
    # (or signing out) clears it. LDAP / SSO / SAML accounts have no local password to change.
    # The lockout counter is cleared only when the WHOLE sign-in succeeds: clearing it on a
    # correct password would let someone who has the password guess codes without limit.
    # The reservation for this (correct) password is given back: nothing was guessed.
    security.release_login_attempt(email, ip)
    return auth_mfa.finish_password_step(request, u, next, via="password", login_key=email)


@router.get("/auth/logout")
def auth_logout_confirm(request: Request):
    """#251: sign-out is a POST behind the synchronizer CSRF token. A GET (a bookmarked
    "sign out" link, an old tab, a link on another site) changes nothing: a signed-in
    person gets a confirmation page whose button posts the form, anyone else goes to the
    sign-in page. Sign-out ends every session of the account (#138), so it must be an
    explicit action, never something another page can trigger by navigating here."""
    me = security.session_user(request)
    if not security.auth_required() or not me or not getattr(me, "id", 0):
        return RedirectResponse("/auth/login", status_code=303)
    return templates.TemplateResponse(
        request, "logout_confirm.html", {"request": request, "me": me}, headers=_NO_STORE
    )


@router.post("/auth/logout")
def auth_logout(request: Request):
    u = security.session_user(request)
    # No session (a stale tab, a bookmarked sign-out link, a probe): the row names the
    # caller, never "system" (2026-09-25: every row names who or what acted).
    actor = getattr(u, "email", "") or security.unauthenticated_actor(request)
    try:
        security.logout(request)
    except Exception:
        # #138: the revocation did not report success, so security.logout deliberately
        # left the client session INTACT. We cannot know whether it committed - a write
        # can land and then fail to be acknowledged - and of the two unknowns, leaving
        # the browser signed in is the honest one: never show a logout we cannot vouch
        # for. Say so cleanly instead of leaking a traceback, and leave a trail, because
        # a logout that may not have taken is a security event, not a page glitch.
        _logger.exception("#138: session revocation failed for %s; session left as-is", actor)
        try:
            audit.log(actor=actor, action="logout.failed", ip=_ip(request))
        except Exception:
            # The revocation most likely failed because storage is down, and the audit
            # write goes to that SAME storage - so this second failure is expected, not
            # exceptional. It must never replace the clean page with a raw 500: the
            # operator still gets told, and the exception above is already logged.
            _logger.exception("#138: could not write the logout.failed audit for %s", actor)
        return _error_page(request, 500, "Could not sign you out. Please try again in a moment.")
    audit.log(actor=actor, action="logout", ip=_ip(request))
    return RedirectResponse("/auth/login", status_code=303)


# --- session lifetime (a11y T9, WCAG 2.2.1): the warning dialog's two endpoints ---
# Both live under /auth, so the login wall never touches the session for them and never
# answers them with a redirect. `/auth/session` only READS (polling it can never extend a
# session); `/auth/keepalive` is the one that extends, and as a POST under /auth it goes
# through the middleware's same-origin + synchronizer-token CSRF check like every other
# /auth POST (only /auth/saml/acs is exempt), so another site cannot keep a victim's
# session alive. Neither can outlive the absolute limit: remaining_seconds is always the
# smaller of the idle and absolute remainders.
_NO_STORE = {"Cache-Control": "no-store"}


@router.get("/auth/session")
def auth_session(request: Request):
    """Read-only session lifetime for the timeout warning. Never writes the session."""
    return JSONResponse(security.session_status(request), headers=_NO_STORE)


@router.post("/auth/keepalive")
def auth_keepalive(request: Request):
    """Extend the idle timer ("Stay signed in", or throttled typing). Capped by the
    absolute limit: past it this clears the session and answers 401."""
    if not security.auth_required() or not request.session.get("uid"):
        return JSONResponse({"ok": False, "signed_in": False}, status_code=401, headers=_NO_STORE)
    expired = security.enforce_session(request)
    if expired:
        return JSONResponse(
            {"ok": False, "signed_in": False, "expired": expired},
            status_code=401,
            headers=_NO_STORE,
        )
    if security.session_user(request) is None:
        return JSONResponse({"ok": False, "signed_in": False}, status_code=401, headers=_NO_STORE)
    state = security.session_clock(request.session)
    return JSONResponse(
        {
            "ok": True,
            "signed_in": True,
            "idle_seconds": state["idle_seconds"],
            "max_seconds": state["max_seconds"],
            "idle_remaining": state["idle_remaining"],
            "absolute_remaining": state["absolute_remaining"],
            "remaining_seconds": state["remaining_seconds"],
            "ending": state["ending"],
        },
        headers=_NO_STORE,
    )


@router.post("/auth/sso")
def auth_sso(request: Request, next: str = Form("/")):
    if oidc.enabled():
        return RedirectResponse(
            "/auth/oidc/login?" + urlencode({"next": next or "/"}), status_code=303
        )
    return _login_error(
        request,
        "/",
        "SSO is not configured. An administrator must set the issuer and "
        "client ID on the Security page first.",
        status=501,
    )


# --- OIDC single sign-on (authorization code + PKCE; see app/services/oidc.py) ---
_OAUTH_ERROR_CODE = re.compile(r"[a-z][a-z_]{0,63}")


@router.get("/auth/oidc/login")
def oidc_login(request: Request):
    if not oidc.enabled():
        return _login_error(
            request,
            "/",
            "SSO is not configured. An administrator must set the issuer and "
            "client ID on the Security page first.",
            status=501,
        )
    try:
        url = oidc.begin_login(request)
    except ValueError as e:
        # our own configuration message (e.g. non-https issuer) — safe to show
        return _login_error(request, "/", str(e), status=502)
    except Exception:
        _logger.exception("OIDC begin_login failed")
        return _login_error(
            request,
            "/",
            "Could not reach the identity provider. "
            "An administrator should check the SSO settings.",
            status=502,
        )
    return RedirectResponse(url, status_code=303)


@router.get("/auth/oidc/callback")
def oidc_callback(request: Request, code: str = "", state: str = "", error: str = ""):
    if not oidc.enabled():
        return RedirectResponse("/auth/login", status_code=303)
    nxt = oidc.stored_next(request)  # read before the keys are cleared
    if error:  # IdP-reported failure (access_denied, ...)
        oidc.complete_login(request, "", state)  # burn the one-shot session keys
        # #248 door: anyone can send a link to this callback, so the reported value is
        # shown only when it is an OAuth error CODE (RFC 6749 section 4.1.2.1: access_denied,
        # invalid_request, ...), never as free text on the real sign-in page.
        shown = error if _OAUTH_ERROR_CODE.fullmatch(error) else "sign-in refused"
        return _login_error(request, "/", f"The identity provider reported: {shown}", status=401)
    claims, err = oidc.complete_login(request, code, state)
    if err or not claims:
        return _login_error(request, "/", err or "SSO sign-in failed.", status=401)
    user, uerr = security.get_or_create_sso_user(claims, ip=_ip(request))
    if not user:
        return _login_error(request, "/", uerr or "SSO sign-in refused.", status=401)
    security.login(request, user)
    audit.log(actor=user.email, action="login.sso", target=user.email, ip=_ip(request))
    return RedirectResponse(nxt, status_code=303)


# --- SAML 2.0 single sign-on (SP-initiated; see app/services/saml.py) ---
# The whole /auth/saml/* surface 404s when saml_enabled is off (opt_in, non-enumerable).
# The ACS (POST) is CSRF-exempt in app/main.py (an IdP POST carries no session/CSRF
# token) and validates the signed assertion ONLY.
@router.get("/auth/saml/login")
def saml_login(request: Request):
    if not saml.enabled():
        return _error_page(request, 404, "Not found.")
    try:
        url, request_id, next_path = saml.begin_login(
            request, routed_path=routing.route_path(request.scope)
        )
    except saml.SamlError as e:
        return _login_error(request, "/", str(e), status=502)
    except Exception:
        _logger.exception("SAML begin_login failed")
        return _login_error(
            request,
            "/",
            "Could not start SAML sign-in. An administrator should check the SAML settings.",
            status=502,
        )
    resp = RedirectResponse(url, status_code=303)
    # Mint the signed, one-shot cross-site correlation cookie (https only) so the IdP's
    # cross-site ACS POST can correlate even though the SameSite=Lax session cookie is
    # withheld there. On http this is a no-op and the session fallback carries the state.
    saml.set_txn_cookie(resp, request, request_id, next_path)
    return resp


_SAML_BUSY = "Sign-in is busy. Retry in a few seconds."


@router.post("/auth/saml/acs")
async def saml_acs(request: Request):
    if not saml.enabled():
        return _error_page(request, 404, "Not found.")
    form = await request.form()
    post_data = dict(form.items())

    # One-shot semantics: the correlation cookie is deleted on EVERY outcome below (success
    # and refusal), so a helper wraps each return to attach the delete-cookie header. The one
    # exception is the busy refusal: no worker ran, so nothing (cookie, session state, replay
    # cache) was consumed and the browser's resubmit must still correlate.
    def _done(resp):
        saml.clear_txn_cookie(resp)
        return resp

    def _acs_and_login():
        # XML signature work, the replay-cache write, user lookup/provisioning, the session
        # write and the audit row are all blocking: ONE bounded worker call (#495). One call
        # matters: process_acs consumes the one-time assertion (replay cache + session pop),
        # so a second admission that could come back busy would burn the assertion and
        # leave the user with a "retry" that can only be refused as a replay.
        try:
            attrs, err = saml.process_acs(
                request, post_data, routed_path=routing.route_path(request.scope)
            )
        except saml.SamlError as e:
            return None, None, (502, str(e))
        except Exception:
            _logger.exception("SAML ACS failed")
            return None, None, (401, "SAML sign-in failed.")
        if err or not attrs:
            return None, None, (401, err or "SAML sign-in failed.")
        # Past this point a failure propagates exactly as before the offload (e.g. the
        # app-wide write-contention 503), not as a generic 401.
        user, uerr = security.get_or_create_saml_user(
            attrs.get("email", ""),
            saml._display_name_from(attrs.get("attributes") or {}),
            ip=_ip(request),
        )
        if not user:
            return None, None, (401, uerr or "SAML sign-in refused.")
        security.login(request, user)
        audit.log(actor=user.email, action="login.saml", target=user.email, ip=_ip(request))
        return user, attrs, None

    try:
        user, attrs, refusal = await offload_parse(_acs_and_login)
    except WorkerBusy:
        # Nothing ran: the assertion is unspent and the correlation cookie is kept.
        return _login_error(request, "/", _SAML_BUSY, status=503)
    if refusal or not user:
        status, message = refusal or (401, "SAML sign-in failed.")
        return _done(_login_error(request, "/", message, status=status))
    return _done(RedirectResponse(saml.sanitize_next(attrs.get("next") or "/"), status_code=303))


@router.get("/auth/saml/metadata")
def saml_metadata(request: Request):
    if not saml.enabled():
        return _error_page(request, 404, "Not found.")
    try:
        xml = saml.metadata(request)
    except saml.SamlError as e:
        return _login_error(request, "/", str(e), status=502)
    return Response(content=xml, media_type="application/samlmetadata+xml")


@router.get("/auth/accept/{token}")
def accept_get(token: str, request: Request):
    inv = security.get_invite(token)
    if not inv:
        return templates.TemplateResponse(
            request, "accept.html", {"request": request, "invalid": True}
        )
    return templates.TemplateResponse(
        request, "accept.html", {"request": request, "inv": inv, "token": token}
    )


@router.post("/auth/accept/{token}")
def accept_post(
    token: str,
    request: Request,
    name: str = Form(""),
    password: str = Form(...),
    confirm: str = Form(""),
):
    inv = security.get_invite(token)
    if not inv:
        return templates.TemplateResponse(
            request, "accept.html", {"request": request, "invalid": True}
        )
    matched = password == confirm
    # Refuse an existing protected account before any password-policy comparison.
    # The invited email supplies the personal-term rule without exposing stored hashes.
    target = security.get_by_email(inv.email)
    if target and security.invite_refusal(target, inv.org_id):
        security.revoke_invite(
            inv.id, security.UNAUTHENTICATED_ACTOR, org_id=inv.org_id, ip=_ip(request)
        )
        audit.log(
            actor=security.UNAUTHENTICATED_ACTOR,
            action="user.invite.refused",
            target=target.email,
            detail="invitation cannot change this account",
            ip=_ip(request),
        )
        return templates.TemplateResponse(
            request, "accept.html", {"request": request, "invalid": True}, status_code=400
        )
    issue = (
        security.password_issue(password, user=inv.email, admin_reset=True)
        if matched
        else "Passwords must match."
    )
    if issue:
        return templates.TemplateResponse(
            request,
            "accept.html",
            {
                "request": request,
                "inv": inv,
                "token": token,
                "error": issue,
                "error_field": "accept-password" if matched else "accept-confirm",
                "name": name[:200],
            },
            status_code=400,
        )
    try:
        u = security.accept_invite(token, name, password, ip=_ip(request))
    except security.InviteRefused as exc:
        # Gate sso-localpw round 1: the invitation names an account it may not change
        # (SSO/LDAP/trust, or deactivated); the service revoked it and audited the refusal.
        return templates.TemplateResponse(
            request,
            "accept.html",
            {"request": request, "invalid": True, "refused": str(exc)},
            status_code=403,
        )
    if not u:
        return templates.TemplateResponse(
            request, "accept.html", {"request": request, "invalid": True}
        )
    audit.log(actor=u.email, action="account.accept_invite", target=u.email, ip=_ip(request))
    return RedirectResponse("/auth/login?accepted=1", status_code=303)


# --- self-service account (any signed-in user) ---
def _get_started_view(me) -> dict:
    """What the Account page's "Get started" card needs: whether this account gets the
    checklist at all (admin or linguist with auth on) and whether it is currently hidden."""
    eligible = onboarding.is_eligible(me)
    return {"eligible": eligible, "hidden": eligible and onboarding.is_dismissed(me)}


@router.get("/account")
def account_page(request: Request):
    me = security.session_user(request)
    # one-time API-key display: stashed in the session by /account/apikeys and
    # popped here so the secret never rides in a URL (access logs, history)
    # #476: the session carries only a reference; the key itself stays server-side.
    # A pre-#476 cookie may still hold a plaintext "new_key": drop it unread.
    request.session.pop("new_key", None)
    had_key, new_key = one_time_stash.pop(request, "new_key_ref")
    new_key_refusal = request.session.pop("new_key_refusal", None)
    # A reference whose value is gone (expired, or the server restarted in between).
    new_key_lost = had_key and new_key is None
    new_key_expires_at = request.session.pop("new_key_expires_at", None)
    return templates.TemplateResponse(
        request,
        "account.html",
        {
            "request": request,
            "me": me,
            "auth_on": security.auth_required(),
            "new_key": new_key,
            "new_key_refusal": new_key_refusal,
            "new_key_lost": new_key_lost,
            "new_key_expires_at": new_key_expires_at,
            "locales": i18n.available_locales(),
            "get_started": _get_started_view(me),
            "saved": request.query_params.get("saved"),
            # TOTP train: the Two-step sign-in card (status only; never a secret or a code).
            "mfa": mfa.status(me) if (me and getattr(me, "id", 0)) else None,
            # Train sso-localpw: why this account has no password form (None = it has one).
            "pw_refusal": (
                security.password_change_refusal(me) if (me and getattr(me, "id", 0)) else None
            ),
        },
    )


@router.post("/account/name")
def account_name(request: Request, name: str = Form("")):
    me = security.session_user(request)
    if me and getattr(me, "id", 0):
        security.update_name(me.id, name.strip())
        audit.log(actor=me.email, action="account.update_name", ip=_ip(request))
    return RedirectResponse("/account?saved=name", status_code=303)


@router.post("/account/locale")
def account_locale(request: Request, ui_locale: str = Form("")):
    """Pick a UI language. Persists to the session (works even when auth is off)
    and, when signed in, to User.ui_locale. Unknown codes fall back to the
    compiled-in default (en) rather than sticking a bad value on the session."""
    chosen = (ui_locale or "").strip()
    if chosen and chosen not in i18n.available_locales():
        chosen = ""  # unknown -> follow the install default
    request.session["ui_locale"] = chosen
    me = security.session_user(request)
    if me and getattr(me, "id", 0):
        security.update_locale(me.id, chosen)
        audit.log(
            actor=me.email,
            action="account.update_locale",
            detail=chosen or "default",
            ip=_ip(request),
        )
    return RedirectResponse("/account?saved=locale", status_code=303)


@router.post("/account/theme")
def account_theme(request: Request, theme: str = Form("")):
    """Light / Dark / Match my computer (ADR 0063). Stored PER ACCOUNT on User.ui_theme (the
    account menu and Account settings both post here), so the choice follows the person to
    every browser; with the login wall off it lives in the session. An unknown value is
    refused, never stored. The account menu posts with fetch (Accept: application/json) and
    gets JSON; the Account settings form gets a redirect."""
    chosen = (theme or "").strip().lower()
    is_fetch = "application/json" in (request.headers.get("accept") or "")
    if chosen not in security.UI_THEMES:
        if is_fetch:
            return JSONResponse({"ok": False, "error": "unknown theme"}, status_code=400)
        return RedirectResponse("/account", status_code=303)
    me = security.session_user(request)
    if me and getattr(me, "id", 0):
        security.update_theme(me.id, chosen)
        audit.log(actor=me.email, action="account.update_theme", detail=chosen, ip=_ip(request))
    else:
        request.session["ui_theme"] = chosen
    if is_fetch:
        return JSONResponse({"ok": True, "ui_theme": chosen})
    return RedirectResponse("/account?saved=theme", status_code=303)


@router.post("/account/get-started")
def account_get_started(request: Request):
    """Turn the "Get started" checklist back on for the signed-in account. The flag is the
    per-user onboarding_dismissed_at stamp, the same one the dashboard's "Don't show this
    again" writes and `account reset-onboarding` / POST /api/account/onboarding-reset clear,
    so web, CLI and API all read one value. A no-op for an ineligible or auth-off caller."""
    me = security.session_user(request)
    if onboarding.is_eligible(me):
        onboarding.reset(me.id)
        audit.log(actor=me.email, action="account.reset_onboarding", ip=_ip(request))
    return RedirectResponse("/account?saved=getstarted", status_code=303)


@router.post("/account/password")
def account_password(
    request: Request, current: str = Form(""), new: str = Form(""), confirm: str = Form("")
):
    me = security.session_user(request)
    if not me or not getattr(me, "id", 0):
        return RedirectResponse("/account", status_code=303)
    refusal = security.password_change_refusal(me)
    if refusal:
        _audit_password_change_refused(request, me)
        return form_errors.redirect_with_error("/account", refusal)
    if new != confirm:
        return RedirectResponse("/account?saved=mismatch", status_code=303)
    result = security.change_password(me.id, current, new, ip=_ip(request))
    ok, msg = result
    if ok:
        # #138: the change revoked every OTHER outstanding cookie for this account
        # (other devices, a stolen copy). Re-stamp THIS session so the person who just
        # did the right thing is not bounced to the login page for it - with the EXACT
        # epoch the change produced, never one re-read afterwards. A revocation that
        # commits in between (an admin reset, another device logging out) would
        # otherwise be read here and handed to the session it had just killed.
        request.session["se"] = int(result.session_epoch)
    audit.log(
        actor=me.email,
        action="account.change_password",
        detail="ok" if ok else "failed",
        ip=_ip(request),
    )
    if ok:
        request.session.pop("pwc", None)
        return RedirectResponse("/account?saved=pw", status_code=303)
    if msg == _WRONG_CURRENT:
        return RedirectResponse("/account?saved=pwfail", status_code=303)
    # A password-policy refusal: the ONE sentence naming what is wrong, as free text in
    # the page error summary (autoescaped), linked to the new-password field.
    return form_errors.redirect_with_error("/account", msg)


_WRONG_CURRENT = "Current password is incorrect"


def _audit_password_change_refused(request, me) -> None:
    """Train sso-localpw: a self-service password change by an account that does not sign
    in with a local password (SSO/SAML/SCIM/LDAP/trust, or no local password) is refused
    before anything is written. Same action as every other attempt, so the account's
    password history reads in one place; the detail names why. The source is read through
    security.auth_source_of, the one normalization, so a whitespace-only value reads as
    "local" rather than an empty name."""
    source = security.auth_source_of(me)
    audit.log(
        actor=me.email,
        action="account.change_password",
        detail=f"refused: signs in through {source}"
        if source != "local"
        else "refused: no local password",
        ip=_ip(request),
    )


def _safe_next(value) -> str:
    return oidc._sanitize_next(value or "/")


@router.get("/auth/password-change")
def password_change_page(request: Request, next: str = "/"):
    """The forced password change (password policy): an admin reset or an expired
    password. Reached right after sign-in and, via the wall in app/main.py, from every
    other page until the change succeeds. A signed-in person may also open it directly."""
    me = security.session_user(request)
    if not me or not getattr(me, "id", 0):
        return RedirectResponse("/auth/login", status_code=303)
    return templates.TemplateResponse(
        request,
        "password_change.html",
        {
            "request": request,
            "me": me,
            "next": _safe_next(next),
            "reason": request.session.get("pwc") or "",
            "rules": security.password_rules(),
            # Train sso-localpw: an account with no local password gets the reason, no form.
            "refusal": security.password_change_refusal(me),
        },
    )


@router.post("/auth/password-change")
def password_change_post(
    request: Request,
    current: str = Form(""),
    new: str = Form(""),
    confirm: str = Form(""),
    next: str = Form("/"),
):
    me = security.session_user(request)
    if not me or not getattr(me, "id", 0):
        return RedirectResponse("/auth/login", status_code=303)
    nxt = _safe_next(next)

    def _again(error: str, field: str):
        return templates.TemplateResponse(
            request,
            "password_change.html",
            {
                "request": request,
                "me": me,
                "next": nxt,
                "reason": request.session.get("pwc") or "",
                "rules": security.password_rules(),
                "error": error,
                "error_field": field,
            },
            status_code=400,
        )

    refusal = security.password_change_refusal(me)
    if refusal:
        _audit_password_change_refused(request, me)
        return templates.TemplateResponse(
            request,
            "password_change.html",
            {
                "request": request,
                "me": me,
                "next": nxt,
                "reason": "",
                "rules": security.password_rules(),
                "refusal": refusal,
            },
            status_code=403,
        )
    if new != confirm:
        return _again("The new passwords did not match. Type the same one twice.", "pwc-confirm")
    result = security.change_password(me.id, current, new, ip=_ip(request))
    ok, msg = result
    audit.log(
        actor=me.email,
        action="account.change_password",
        detail=("ok" if ok else "failed")
        + (" (required at sign-in)" if request.session.get("pwc") else ""),
        ip=_ip(request),
    )
    if not ok:
        return _again(msg, "pwc-current" if msg == _WRONG_CURRENT else "pwc-new")
    request.session["se"] = int(result.session_epoch)
    request.session.pop("pwc", None)
    return RedirectResponse(nxt, status_code=303)


@router.get("/account/export")
def account_export(request: Request):
    """The signed-in user downloads a copy of their own data (GDPR Art. 15/20)
    as an NDJSON stream. With auth OFF there is no real account (the SYSTEM
    sentinel has no DB row), so there is nothing to export."""
    me = security.session_user(request)
    if not me or not getattr(me, "id", 0):
        return _error_page(request, 400, "Sign in to export your data.")
    # audit BEFORE streaming — the response body is produced lazily by the
    # generator, so logging here guarantees the event is recorded even if the
    # client disconnects mid-download.
    audit.log(actor=me.email, action="gdpr.export", target=me.email, ip=_ip(request))

    # #178-5: scope the copy to the caller's CURRENT org, like the admin and /api export
    # twins. Unscoped, a user moved between orgs got the former org's org-owned rows (the
    # operator behaviour). current_org_id is None with the gate off (unscoped, as before).
    caller_org = access.current_org_id(me)

    def gen():
        for section, rec in gdpr.export_user(me.id, caller_org_id=caller_org):
            yield json.dumps({"section": section, **rec}, ensure_ascii=False) + "\n"

    fname = "gdpr-export-%d-%s.ndjson" % (me.id, datetime.utcnow().strftime("%Y%m%d-%H%M%S"))  # noqa: UP031
    return StreamingResponse(
        gen(),
        media_type="application/x-ndjson",
        headers={"Content-Disposition": f'attachment; filename="{fname}"'},
    )


# Editable page layouts (train L1): the self-service /account/layouts/{page_key} session
# routes live in app/web/page_layouts.py and ride this router (the /account family), so
# the pinned router assembly in app/modules.py ROUTER_GROUPS is unchanged.
from app.web.page_layouts import router as _page_layouts_router  # noqa: E402

router.include_router(_page_layouts_router)

# Two-step sign-in (TOTP train): /auth/mfa* (the step after the password and forced set-up)
# and /account/mfa/* (self-service) live in app/web/auth_mfa.py and ride this router, so the
# pinned router assembly in app/modules.py ROUTER_GROUPS is unchanged.
router.include_router(auth_mfa.router)
