"""Shared CSRF helper for the auth-on verify suites.

With the login wall ON, the security middleware (app/main.py::_security_audit)
enforces a session-bound synchronizer token on every mutating, non-/api request:
the browser sends it as the X-CSRF-Token header or a csrf_token form field, and
the middleware compares it against request.session["csrf"].

The token lives in the SIGNED session cookie (Starlette SessionMiddleware). This
module decodes that cookie straight from the TestClient's cookie jar, mirroring
the EXACT codec used by the installed starlette (1.7.0)
middleware/sessions.py::SessionMiddleware:

    signer = itsdangerous.TimestampSigner(str(secret_key))     # all defaults
    scope["session"] = json.loads(b64decode(signer.unsign(cookie)))

and signs with the same SESSION_SECRET the app runs with (app.config).

Usage in an auth-on suite: after establishing a session (any GET renders a
template, which lazily mints session["csrf"] and Set-Cookie's it), call

    csrf_for(client)

once. It returns the token AND installs it as the client's default
X-CSRF-Token header, so every subsequent POST on that client carries it. Call
it again after any login/logout (security.login()/logout() clear the session,
which rotates the token).
"""

import json
from base64 import b64decode, b64encode

import itsdangerous

from app import config

_signer = itsdangerous.TimestampSigner(str(config.SESSION_SECRET))


def session_data(client) -> dict:
    """Decode the current 'session' cookie from the TestClient's jar to a dict.

    Returns {} when there is no session cookie yet or it fails to verify."""
    raw = client.cookies.get("session")
    if not raw:
        return {}
    try:
        unsigned = _signer.unsign(
            raw.encode("utf-8"), max_age=60 * 60 * 24
        )  # app.main.SESSION_COOKIE_MAX_AGE
        return json.loads(b64decode(unsigned))
    except Exception:
        return {}


def csrf_value(client) -> str:
    """Return the session CSRF token WITHOUT installing it as a default header.

    Same minting fallback as csrf_for() (a GET renders a page whose _csrf.html
    include creates session["csrf"]), but leaves the client's default headers
    untouched. Use this to exercise the form-field CSRF path end-to-end: pass the
    returned token as a csrf_token form field with NO X-CSRF-Token header."""
    tok = session_data(client).get("csrf")
    if not tok:
        client.get("/auth/login")
        tok = session_data(client).get("csrf") or ""
    return tok


def record_message(client, value: str, name: str = "err") -> str:
    """Record free-text ``value`` for ``name`` in the client's session, exactly as
    app/web/page_guard.py does after a server redirect, so a suite can render a free-text
    message on a page directly (security review 2026-09, #248: unrecorded free text in
    ?err= and the other message parameters is removed before a page reads it). The cookie
    is re-signed with the app's SESSION_SECRET on the jar's own domain/path. Returns value."""
    from app.web import page_guard

    data = session_data(client)
    digest = page_guard._digest(name, value)
    data[page_guard.QM_KEY] = [*data.get(page_guard.QM_KEY, []), digest]
    jar = [ck for ck in client.cookies.jar if ck.name == "session"]
    where = {"domain": jar[0].domain, "path": jar[0].path} if jar else {}
    signed = _signer.sign(b64encode(json.dumps(data).encode("utf-8"))).decode("utf-8")
    client.cookies.delete("session")
    client.cookies.set("session", signed, **where)
    return value


def sign_out(client, **kwargs):
    """Sign the client out the way the product does since #251: POST /auth/logout with the
    session's CSRF token (GET /auth/logout only shows a confirmation page now). Extra
    kwargs (follow_redirects=...) go to the POST. Leaves no X-CSRF-Token default behind
    unless one was already installed (call csrf_for again after the next sign-in)."""
    tok = csrf_value(client)
    return client.post("/auth/logout", headers={"X-CSRF-Token": tok}, **kwargs)


def csrf_for(client) -> str:
    """Return the session CSRF token, minting it via a GET if absent, and set it
    as the client's default X-CSRF-Token header so all later POSTs carry it.

    Idempotent and safe to call repeatedly (e.g. after each login/logout, which
    rotates the token). The GET fallback hits /auth/login, which is always
    reachable (pre-session or authed) and renders a template that lazily creates
    session["csrf"]."""
    tok = session_data(client).get("csrf")
    if not tok:
        # No session yet: a GET renders a page whose _csrf.html include mints the
        # token and Set-Cookie's the session. /auth/login is always reachable.
        client.get("/auth/login")
        tok = session_data(client).get("csrf") or ""
    client.headers["X-CSRF-Token"] = tok
    return tok
