"""Session timeout verification (a11y train T9, WCAG 2.2.1). Offline, hermetic.

Proves the SECURITY INVARIANTS of the session timeout warning with real requests through
the app, on a controllable clock (the session service reads ONE time source,
`security_client_ip_one_authority_._now`, which this suite replaces; no sleeps):

  I1  GET /auth/session is read-only: no renewing Set-Cookie, polling it past the idle
      limit still ends in expiry, and neither it nor session_status can write the session
      (AST pin: no touch/enforce/login call, no store into the session).
  I2  POST /auth/keepalive needs the CSRF protection other /auth POSTs get: without a
      token, with a wrong token, cross-origin (Origin or Referer), and by GET / HEAD it
      does NOT extend the session; with the token it extends idle but never absolute.
  I3  `iat` is set at every sign-in path (password, LDAP, OIDC, SAML, setup) and never
      rewritten (keepalive, Get started / locale / name saves, page loads). Activity every
      minute for longer than session_max_hours still expires. A session without `iat`
      is expired while the absolute limit is on, and so is one whose `iat` is more than
      300 s in the future. AST pin: `uid` / `iat` are stored only in security.login. The
      session cookie window is a fixed 24 h (flags unchanged), and both limits validate
      inside it (idle 0..1440 min, max 0..24 h, default 24 h since T11).
  I4  remaining_seconds = min(idle, absolute) from both endpoints; keepalive after the
      absolute limit answers 401 and clears the session.
  I5  an expired fetch (X-CSRF-Token) gets 401 {"error": "session_expired"}; a navigation
      keeps the 303 with expired=1 (reason=max for the absolute limit); an expired form
      POST returns to the sanitized same-origin Referer page, never an external URL and
      never a POST-only route.
  I6  sign-in still clears the session; a client cannot set `iat` or `la`.

Security review 2026-09: #247 the limits also hold on /auth/password-change (GET and
POST, idle and absolute); #251 GET /auth/logout only shows a confirmation and signs no
session out, the POST needs same-origin + the CSRF token; #260 signed-in HTML is
Cache-Control: no-store and the signed-out dialog refuses Escape and hides the page.

Plus: settings validation on every door (validator registry, CLI config set, the web
Security page's Sessions card with its audit row), and base.html loading the warning only
for a signed-in user with the wall on and a limit set.

Prints ASCII only; ends with SESSION TIMEOUT OK.
"""

import ast
import json
import os
import sys
from base64 import b64encode
from types import SimpleNamespace
from unittest.mock import patch

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(_ROOT)
sys.path.insert(0, _ROOT)
from tests._bootstrap import init_env

init_env("loctms_session_timeout_")

import itsdangerous
from fastapi.testclient import TestClient
from sqlmodel import Session, select

from app import config
from app import main as main_mod
from app.db import engine, init_db
from app.main import app
from app.models import AuditLog, Role
from app.services import security, settings
from app.services import security_client_ip_one_authority_ as sec_mod
from app.web import auth as auth_mod
from tests._cli_runner import run_cli
from tests._csrf import session_data, sign_out

# --- the controllable clock ----------------------------------------------------------------
CLOCK = [1_900_000_000]
sec_mod._now = lambda: CLOCK[0]


def tick(seconds: int) -> None:
    CLOCK[0] += seconds


ADMIN = "admin@x.com"
ADMIN_PW = "session-suite-pass-2026"  # pregate: allow-secret throwaway fixture login
SECOND = "second@x.com"
SECOND_PW = "second-suite-pass-2026"  # pregate: allow-secret throwaway fixture login
IDLE_MIN = 10
MAX_H = 2

_signer = itsdangerous.TimestampSigner(str(config.SESSION_SECRET))


def sign_session(payload: dict) -> str:
    return _signer.sign(b64encode(json.dumps(payload).encode("utf-8"))).decode("utf-8")


def renews_cookie(resp) -> bool:
    """True when a response carries a Set-Cookie for the session that is not a deletion."""
    for raw in resp.headers.get_list("set-cookie"):
        deletion = raw.startswith("session=null") or "expires=Thu, 01 Jan 1970" in raw
        if raw.startswith("session=") and not deletion:
            return True
    return False


def token_for(c) -> str:
    """The session CSRF token, minted by rendering a signed-in page when absent."""
    tok = session_data(c).get("csrf")
    if not tok:
        c.get("/account")
        tok = session_data(c).get("csrf") or ""
    assert tok, "no CSRF token could be minted"
    return tok


def login(email=ADMIN, pw=ADMIN_PW) -> TestClient:
    c = TestClient(app)
    c.get("/auth/login")  # mints the pre-login CSRF token
    pre = session_data(c).get("csrf")
    r = c.post(
        "/auth/login",
        data={"email": email, "password": pw, "next": "/", "csrf_token": pre},
        follow_redirects=False,
    )
    assert r.status_code == 303, (r.status_code, r.text[:300])
    assert session_data(c).get("uid"), "login did not sign in"
    return c


def keepalive(c, token=None, **kw):
    headers = kw.pop("headers", {})
    if token is not None:
        headers["X-CSRF-Token"] = token
    return c.post("/auth/keepalive", headers=headers, follow_redirects=False, **kw)


def limits(idle=IDLE_MIN, max_h=MAX_H) -> None:
    settings.set_many({"session_idle_minutes": str(idle), "session_max_hours": str(max_h)})


# --- sections ----------------------------------------------------------------------------------
def setup_path_sets_iat() -> None:
    """Setup is a sign-in path: the first admin is signed in with iat = now."""
    assert security.user_count() == 0
    c = TestClient(app)
    r = c.post(
        "/auth/setup",
        data={"email": ADMIN, "name": "Admin", "password": ADMIN_PW},
        follow_redirects=False,
    )
    assert r.status_code == 303, (r.status_code, r.text[:300])
    s = session_data(c)
    assert s.get("uid") and s.get("iat") == CLOCK[0] and s.get("la") == CLOCK[0], s
    security.create_user(SECOND, "Second", Role.TRANSLATOR, SECOND_PW)
    settings.set_many({"security_auth_required": "1"})
    limits()
    print("  I3: /auth/setup signs in with iat = now")


def i1_session_is_read_only() -> None:
    c = login()
    t0 = CLOCK[0]
    before = c.cookies.get("session")
    r = c.get("/auth/session")
    assert r.status_code == 200 and r.headers.get("cache-control") == "no-store", r.headers
    body = r.json()
    assert body["signed_in"] is True and body["remaining_seconds"] == IDLE_MIN * 60, body
    assert body["ending"] == "idle" and body["absolute_remaining"] == MAX_H * 3600, body
    assert not renews_cookie(r), r.headers.get_list("set-cookie")
    assert c.cookies.get("session") == before
    # Poll once a minute, well past the idle limit: never renews, and ends in expiry.
    for minute in range(1, IDLE_MIN + 2):
        tick(60)
        r = c.get("/auth/session")
        assert not renews_cookie(r), f"minute {minute}: /auth/session renewed the cookie"
        assert session_data(c).get("la") == t0, "polling moved the last-active stamp"
        if minute <= IDLE_MIN:
            assert r.json()["signed_in"] is True, (minute, r.json())
            assert r.json()["remaining_seconds"] == IDLE_MIN * 60 - minute * 60
    assert r.json() == {"signed_in": False, "expired": "idle"}, r.json()
    nav = c.get("/projects", follow_redirects=False)
    assert nav.status_code == 303
    assert nav.headers["location"] == "/auth/login?next=/projects&expired=1", nav.headers
    assert not session_data(c).get("uid"), "the wall must clear the expired session"
    print("  I1: polling /auth/session never renews; past the idle limit it expires")

    # A revoked cookie: session_user's clear-on-mismatch lands on a copy, not the cookie.
    c = login()
    security.bump_session_epoch(session_data(c)["uid"])
    r = c.get("/auth/session")
    assert r.json() == {"signed_in": False} and not r.headers.get_list("set-cookie"), (
        r.json(),
        r.headers.get_list("set-cookie"),
    )
    # auth off / anonymous
    assert TestClient(app).get("/auth/session").json() == {"signed_in": False}
    print("  I1: a revoked or anonymous session reads signed_in=false with no Set-Cookie")

    # AST pin (research 5.5): a later refactor must not make the read path write.
    def fn(path, name):
        with open(path, encoding="utf-8") as fh:
            tree = ast.parse(fh.read())
        return next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == name)

    for path, name in (
        ("app/web/auth.py", "auth_session"),
        ("app/services/security.py", "session_status"),
        ("app/services/security_client_ip_one_authority_.py", "session_clock"),
    ):
        node = fn(path, name)
        for sub in ast.walk(node):
            if isinstance(sub, ast.Call):
                callee = ast.unparse(sub.func)
                assert not any(
                    w in callee for w in ("touch_session", "enforce_session", "login", "logout")
                ), f"{name} calls {callee}"
                assert not (
                    callee.endswith(("session.clear", "session.pop", "session.update"))
                    or callee.endswith("session.setdefault")
                ), f"{name} mutates the session via {callee}"
            if isinstance(sub, (ast.Assign, ast.AugAssign, ast.AnnAssign, ast.Delete)):
                targets = sub.targets if hasattr(sub, "targets") else [sub.target]
                for tgt in targets:
                    assert not (
                        isinstance(tgt, ast.Subscript) and "session" in ast.unparse(tgt.value)
                    ), f"{name} stores into the session: {ast.unparse(tgt)}"
    print("  I1: AST pin - the read path never touches, stores into or clears the session")


def i2_keepalive_needs_csrf() -> None:
    c = login()
    t0 = CLOCK[0]
    tok = token_for(c)
    tick(300)
    ref_la = session_data(c)["la"]

    def unchanged(label):
        assert session_data(c).get("la") == ref_la, f"{label} moved la"
        assert session_data(c).get("iat") == t0, f"{label} moved iat"

    r = keepalive(c)  # no token at all: the plain-form CSRF redirect
    assert r.status_code == 303 and "expired=1" in r.headers["location"], r.status_code
    unchanged("no token")
    r = keepalive(c, "not-the-real-token")  # pregate: allow-secret wrong on purpose
    assert r.status_code == 403, r.status_code
    unchanged("wrong token")
    r = keepalive(c, tok, headers={"Origin": "http://evil.example"})
    assert r.status_code == 403, r.status_code
    unchanged("cross-origin Origin")
    r = keepalive(c, tok, headers={"Referer": "http://evil.example/page"})
    assert r.status_code == 403, r.status_code
    unchanged("cross-origin Referer")
    for method in ("GET", "HEAD"):
        r = c.request(method, "/auth/keepalive", headers={"X-CSRF-Token": tok})
        assert r.status_code == 405, (method, r.status_code)
        unchanged(method)
    # the form-field token (no header) still passes the middleware's CSRF check
    r = keepalive(c, None, data={"csrf_token": tok})
    assert r.status_code == 200 and r.json()["ok"] is True, r.status_code
    tick(60)
    r = keepalive(c, tok, headers={"Origin": "http://testserver"})
    assert r.status_code == 200 and r.headers.get("cache-control") == "no-store"
    body = r.json()
    assert body["ok"] is True and body["remaining_seconds"] == IDLE_MIN * 60, body
    assert session_data(c)["la"] == CLOCK[0] and session_data(c)["iat"] == t0
    print("  I2: keepalive refuses no/wrong token, cross-origin, GET and HEAD; token extends idle")


def i3_i4_absolute_limit() -> None:
    c = login()
    t0 = CLOCK[0]
    tok = token_for(c)
    # Activity every minute (keepalive, page loads, account saves) for the whole limit.
    posts = [
        ("/account/get-started", {}),
        ("/account/locale", {"ui_locale": "en"}),
        ("/account/name", {"name": "Admin"}),
    ]
    for minute in range(1, MAX_H * 60):
        tick(60)
        if minute % 3 == 0:
            path, data = posts[(minute // 3) % len(posts)]
            r = c.post(path, data=data, headers={"X-CSRF-Token": tok}, follow_redirects=False)
            assert r.status_code == 303, (path, r.status_code)
        elif minute % 3 == 1:
            assert c.get("/projects", follow_redirects=False).status_code == 200
        else:
            r = keepalive(c, tok)
            assert r.status_code == 200, (minute, r.status_code)
            left = r.json()["remaining_seconds"]
            assert left == min(IDLE_MIN * 60, MAX_H * 3600 - minute * 60), (minute, left)
        assert session_data(c).get("iat") == t0, f"iat rewritten at minute {minute}"
    # 60 s before the absolute limit: remaining is the ABSOLUTE remainder, ending "max".
    status = c.get("/auth/session").json()
    assert status["remaining_seconds"] == 60 and status["ending"] == "max", status
    r = keepalive(c, tok)
    assert r.json()["remaining_seconds"] == 60 and r.json()["idle_remaining"] == IDLE_MIN * 60
    tick(60)  # exactly session_max_hours after sign-in
    r = keepalive(c, tok)
    assert r.status_code == 401 and r.json() == {
        "ok": False,
        "signed_in": False,
        "expired": "max",
    }, (r.status_code, r.text)
    assert not session_data(c).get("uid"), "keepalive past the absolute limit must clear it"
    print("  I3/I4: a minute of activity each minute for 2h still expires at 2h; iat never moves")

    # Navigation past the absolute limit: 303 with reason=max; fetch: 401 JSON.
    c2 = login()
    tok2 = token_for(c2)
    tick(MAX_H * 3600)
    r = c2.get(
        "/account",
        headers={"X-CSRF-Token": tok2},  # pregate: allow-secret session CSRF from the fixture
        follow_redirects=False,
    )
    assert r.status_code == 401 and r.json()["error"] == "session_expired", r.status_code
    assert r.json()["reason"] == "max"
    c3 = login()
    tick(MAX_H * 3600 + 5)
    r = c3.get("/projects?page=2", follow_redirects=False)
    assert r.headers["location"] == "/auth/login?next=/projects&expired=1&reason=max", r.headers
    page = c3.get(r.headers["location"]).text
    assert 'role="status"' in page and "maximum session length" in page
    print("  I3: absolute expiry: 401 JSON for fetch, 303 reason=max, login page says why")

    # A session minted before this deploy (no iat) is expired while the limit is on.
    uid = security.get_by_email(ADMIN).id
    epoch = int(security.get_by_email(ADMIN).session_epoch or 0)
    legacy = sign_session({"uid": uid, "se": epoch, "la": CLOCK[0]})
    lc = TestClient(app)
    lc.cookies.set("session", legacy)
    assert lc.get("/auth/session").json() == {"signed_in": False, "expired": "max"}
    r = lc.get("/projects", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"].endswith("expired=1&reason=max")
    limits(max_h=0)
    lc = TestClient(app)
    lc.cookies.set("session", legacy)
    assert lc.get("/projects", follow_redirects=False).status_code == 200, (
        "with the absolute limit off, a no-iat session is judged on idle only"
    )
    status = lc.get("/auth/session").json()
    assert status["absolute_remaining"] is None and status["remaining_seconds"] == 600
    limits()
    print("  I3: a session with no iat is expired while session_max_hours > 0")


def i5_expired_requests() -> None:
    c = login()
    tok = token_for(c)
    tick(IDLE_MIN * 60 + 1)
    r = c.post("/projects", data={"name": "x"}, headers={"X-CSRF-Token": tok})
    assert r.status_code == 401 and r.json() == {"error": "session_expired", "reason": "idle"}
    assert r.headers.get("cache-control") == "no-store"
    print("  I5: an expired fetch POST gets 401 session_expired JSON")

    def expired_post(path, referer=None, origin=None):
        cc = login()
        tk = token_for(cc)
        tick(IDLE_MIN * 60 + 1)
        headers = {}
        if referer:
            headers["Referer"] = referer
        if origin:
            headers["Origin"] = origin
        r = cc.post(path, data={"csrf_token": tk}, headers=headers, follow_redirects=False)
        assert r.status_code == 303, (path, r.status_code)
        return r.headers["location"]

    base = "/auth/login?next="
    loc = expired_post("/account/name", referer="http://testserver/projects/5?tab=a&x=1")
    assert loc == base + "/projects/5%3Ftab%3Da%26x%3D1&expired=1", loc
    # an external Referer is ignored (Origin makes the POST same-origin, so it reaches the
    # wall); the POST-only route then falls back to the dashboard, a GET route to itself
    loc = expired_post(
        "/account/name", referer="http://evil.example/phish", origin="http://testserver"
    )
    assert loc == base + "/&expired=1", loc
    loc = expired_post("/projects", referer="http://evil.example/p", origin="http://testserver")
    assert loc == base + "/projects&expired=1", loc
    for trick in (
        "http://testserver//evil.example/x",
        "http://testserver/a\\b",
        "javascript:alert(1)",
        "http://testserver@evil.example/projects",
        "http://testserver/auth/logout",
        # gate MEDIUM 1: same-origin POST-only pages (some render AT their POST URL)
        "http://testserver/batches/1/rerun",
        "http://testserver/projects/from-template",
        "http://testserver/batches/1/rerun?x=1",
    ):
        loc = expired_post("/account/name", referer=trick, origin="http://testserver")
        assert loc == base + "/&expired=1", (trick, loc)
    # the GET-route oracle: a POST-only path, and a literal segment that only a typed
    # parameter route would "match" (GET /projects/from-template reaches /projects/{int} -> 422)
    for path, has_get in (
        ("/projects", True),
        ("/projects/5", True),
        ("/batches/1/rerun", False),
        ("/projects/from-template", False),
        ("/account/name", False),
    ):
        assert main_mod._has_get_route(path) is has_get, (path, has_get)
    # a same-origin Referer that DOES answer GET still wins over the POST route
    loc = expired_post("/account/name", referer="http://testserver/projects/5")
    assert loc == base + "/projects/5&expired=1", loc
    loc = expired_post("/account/name")  # no Referer, POST-only route
    assert loc == base + "/&expired=1", loc
    # the CSRF branch (session already gone, stale form token) also returns to the page
    cc = login()
    tk = token_for(cc)
    sign_out(cc)
    r = cc.post(
        "/account/name",
        data={"csrf_token": tk},
        headers={"Referer": "http://testserver/account"},
        follow_redirects=False,
    )
    assert r.headers["location"] == base + "/account&expired=1", r.headers["location"]
    # the login page sanitizes whatever next it is given
    page = TestClient(app).get("/auth/login", params={"next": "//evil.example"}).text
    assert 'name="next" value="/"' in page
    print("  I5: expired form POST returns to the same-origin Referer page, never off-site")


def cookie_window_and_future_iat() -> None:
    """The cookie signature window is a fixed 24 h (it capped a 20 h limit at 12 h before);
    only max_age moved: HttpOnly, SameSite=lax and the secret are unchanged. A future iat
    minted by a skewed server is expired, never extra time."""
    c = TestClient(app)
    c.get("/auth/login")
    pre = session_data(c)["csrf"]
    r = c.post(
        "/auth/login",
        data={"email": ADMIN, "password": ADMIN_PW, "next": "/", "csrf_token": pre},
        follow_redirects=False,
    )
    cookie = next(v for v in r.headers.get_list("set-cookie") if v.startswith("session="))
    low = cookie.lower()
    assert "max-age=86400" in low and "httponly" in low and "samesite=lax" in low, cookie
    assert main_mod.SESSION_COOKIE_MAX_AGE == 24 * 3600
    uid = security.get_by_email(ADMIN).id
    epoch = int(security.get_by_email(ADMIN).session_epoch or 0)
    for skew, expired in ((300, False), (301, True), (24 * 3600, True)):
        fc = TestClient(app)
        fc.cookies.set(
            "session",
            sign_session({"uid": uid, "se": epoch, "la": CLOCK[0], "iat": CLOCK[0] + skew}),
        )
        body = fc.get("/auth/session").json()
        if expired:
            assert body == {"signed_in": False, "expired": "max"}, (skew, body)
            assert fc.get("/projects", follow_redirects=False).status_code == 303
        else:
            assert body["signed_in"] is True and body["absolute_remaining"] == MAX_H * 3600, body
    print("  cookie window 24 h (flags unchanged); an iat > 300 s in the future is expired")


def i6_fixation_and_client_stamps() -> None:
    # A pre-login session carrying stale stamps and junk (the server's own signature: this
    # models left-over state, not a client forgery, which the signature already rules out).
    tok = "pre-login-token-for-fixation"  # pregate: allow-secret fixture CSRF value
    forged = sign_session({"csrf": tok, "iat": 1, "la": 1, "junk": "left-over"})
    pre = TestClient(app)
    pre.get("/auth/login")  # the server's own cookie first, to learn the jar's domain
    domain = next(ck.domain for ck in pre.cookies.jar if ck.name == "session")
    pre.cookies.set("session", forged, domain=domain)
    r = pre.post(
        "/auth/login",
        data={"email": ADMIN, "password": ADMIN_PW, "next": "/", "csrf_token": tok, "iat": "1"},
        params={"la": "1", "iat": "1"},
        follow_redirects=False,
    )
    assert r.status_code == 303
    s = session_data(pre)
    assert s["iat"] == CLOCK[0] and s["la"] == CLOCK[0] and "junk" not in s, s
    assert "csrf" not in s or s["csrf"] != tok, "the pre-login CSRF token survived sign-in"
    t = token_for(pre)
    tick(30)
    r = keepalive(pre, t, json={"iat": 0, "la": 0})
    assert r.status_code == 200 and session_data(pre)["iat"] == s["iat"]
    # AST pin: uid / iat are stored in exactly one function, security.login.
    stores = []
    for dirpath, _dirs, files in os.walk("app"):
        for fname in files:
            if not fname.endswith(".py"):
                continue
            path = os.path.join(dirpath, fname)
            with open(path, encoding="utf-8") as fh:
                tree = ast.parse(fh.read())
            for func in ast.walk(tree):
                if not isinstance(func, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    continue
                for sub in ast.walk(func):
                    if isinstance(sub, ast.Assign):
                        for tgt in sub.targets:
                            if (
                                isinstance(tgt, ast.Subscript)
                                and isinstance(tgt.slice, ast.Constant)
                                and tgt.slice.value in ("uid", "iat")
                                and "session" in ast.unparse(tgt.value)
                            ):
                                stores.append((path.replace("\\", "/"), func.name, tgt.slice.value))
    assert sorted(set(stores)) == [
        ("app/services/security_client_ip_one_authority_.py", "login", "iat"),
        ("app/services/security_client_ip_one_authority_.py", "login", "uid"),
    ], stores
    print("  I6: sign-in regenerates the session; a client cannot set iat or la")


def every_sign_in_path_sets_iat() -> None:
    user = security.get_by_email(SECOND)

    def fresh():
        tick(7)
        c = TestClient(app)
        c.get("/auth/login")
        return c, session_data(c)["csrf"]

    # password: covered by login(); assert it explicitly
    c = login(SECOND, SECOND_PW)
    assert session_data(c)["iat"] == CLOCK[0]
    # LDAP
    c, tok = fresh()
    with (
        patch.object(auth_mod.ldap_auth, "enabled", lambda: True),
        patch.object(auth_mod.ldap_auth, "try_login", lambda e, p: (user, None)),
    ):
        r = c.post(
            "/auth/login",
            data={
                "email": SECOND,
                "password": "dir-pass",  # pregate: allow-secret fake directory password
                "csrf_token": tok,
            },  # pregate: allow-secret fake directory password
            follow_redirects=False,
        )
    assert r.status_code == 303 and session_data(c)["iat"] == CLOCK[0], r.status_code
    # OIDC
    c, _tok = fresh()
    with (
        patch.object(auth_mod.oidc, "enabled", lambda: True),
        patch.object(auth_mod.oidc, "stored_next", lambda request: "/"),
        patch.object(
            auth_mod.oidc, "complete_login", lambda request, code, state: ({"email": SECOND}, None)
        ),
        patch.object(
            auth_mod.security, "get_or_create_sso_user", lambda claims, **_kw: (user, None)
        ),
    ):
        r = c.get("/auth/oidc/callback?code=a&state=b", follow_redirects=False)
    assert r.status_code == 303 and session_data(c)["iat"] == CLOCK[0], r.status_code
    # SAML
    c, _tok = fresh()
    attrs = {"email": SECOND, "attributes": {}, "next": "/"}
    with (
        patch.object(auth_mod.saml, "enabled", lambda: True),
        patch.object(auth_mod.saml, "process_acs", lambda *a, **k: (attrs, None)),
        patch.object(
            auth_mod.security, "get_or_create_saml_user", lambda e, n, **_kw: (user, None)
        ),
    ):
        r = c.post("/auth/saml/acs", data={"SAMLResponse": "x"}, follow_redirects=False)
    assert r.status_code == 303 and session_data(c)["iat"] == CLOCK[0], r.status_code
    # Invite accept does NOT sign in (it sends the person to the login page).
    inv = security.create_invite("invitee@x.com", Role.TRANSLATOR, ADMIN)
    c, tok = fresh()
    r = c.post(
        f"/auth/accept/{inv.token}",
        data={
            "name": "Inv",
            "password": "invitee-pass-2026",  # pregate: allow-secret throwaway fixture
            "confirm": "invitee-pass-2026",  # pregate: allow-secret throwaway fixture
            "csrf_token": tok,
        },
        follow_redirects=False,
    )
    assert r.status_code == 303 and "accepted=1" in r.headers["location"], r.status_code
    assert not session_data(c).get("uid"), "invite accept must not sign in"
    print(
        "  I3: iat is set by password, LDAP, OIDC, SAML and setup sign-in; invite does not sign in"
    )


def settings_doors() -> None:
    for bad in ("-1", "abc", "1.5", "25", "8760", "", " ", "²", "1e3"):
        try:
            settings.validate_value("session_max_hours", bad)
        except settings.InvalidSettingValue:
            continue
        raise AssertionError(f"session_max_hours accepted {bad!r}")
    for bad in ("-5", "1441", "43200", "x"):
        try:
            settings.validate_value("session_idle_minutes", bad)
        except settings.InvalidSettingValue:
            continue
        raise AssertionError(f"session_idle_minutes accepted {bad!r}")
    for good in ("0", "20", "24"):
        settings.validate_value("session_max_hours", good)
    for good in ("0", "240", "1440"):
        settings.validate_value("session_idle_minutes", good)
    # 09-24 (a11y T11): default 24 h, longer than 20 h so the WCAG 2.2.1 20-hour
    # exception applies, and equal to (never past) the 24 h cookie window.
    assert settings.DEFAULTS["session_max_hours"] == "24"
    assert sec_mod._SESSION_MAX_HOURS_FALLBACK == 24, "read-side fallback must match"
    assert int(settings.DEFAULTS["session_max_hours"]) * 3600 <= main_mod.SESSION_COOKIE_MAX_AGE
    assert main_mod.SESSION_COOKIE_MAX_AGE == 1440 * 60, "idle ceiling must equal the window"
    # CLI door
    code, _out, _err = run_cli(["config", "set", "session_max_hours", "abc"])
    assert code != 0 and settings.get("session_max_hours") == str(MAX_H), code
    code, _out, _err = run_cli(["config", "set", "session_max_hours", "25"])
    assert code != 0 and settings.get("session_max_hours") == str(MAX_H), code
    code, _out, _err = run_cli(["config", "set", "session_idle_minutes", "1441"])
    assert code != 0 and settings.get("session_idle_minutes") == str(IDLE_MIN), code
    # #280: the session limits are security-sensitive, so a valid write confirms.
    code, _out, _err = run_cli(["config", "set", "session_max_hours", "3", "--yes"])
    assert code == 0 and settings.get("session_max_hours") == "3", (code, _err)
    limits()
    print("  settings: validator on every door (registry + CLI config set)")

    # Web Security page: the Sessions card
    c = login()
    tok = token_for(c)
    page = c.get("/security").text
    assert 'name="session_idle_minutes"' in page and 'name="session_max_hours"' in page
    assert "2 minutes" in page
    base_form = {"security_auth_required": "1", "audit_enabled": "1", "csrf_token": tok}
    r = c.post(
        "/security",
        data={**base_form, "session_idle_minutes": "15", "session_max_hours": "-3"},
        follow_redirects=False,
    )
    assert r.status_code == 303 and "err=" in r.headers["location"], r.headers["location"]
    assert settings.get("session_idle_minutes") == str(IDLE_MIN), "a refused save wrote"
    r = c.post(
        "/security",
        data={**base_form, "session_idle_minutes": "15", "session_max_hours": "3"},
        follow_redirects=False,
    )
    assert r.headers["location"] == "/security?saved=1", r.headers["location"]
    assert settings.get("session_idle_minutes") == "15" and settings.get("session_max_hours") == "3"
    with Session(engine) as s:
        row = s.exec(
            select(AuditLog)
            .where(AuditLog.action == "security.update")
            .order_by(AuditLog.id.desc())
        ).first()
    assert "session_max_hours old=2 new=3" in row.detail, row.detail
    assert "session_idle_minutes old=10 new=15" in row.detail, row.detail
    r = c.post("/security", data=base_form, follow_redirects=False)  # fields omitted
    assert settings.get("session_max_hours") == "3", "an omitted field must not reset the limit"
    limits()
    print("  web: Sessions card saves, validates, audit-logs; omitted fields stay unchanged")


def base_html_loading() -> None:
    c = login()
    html = c.get("/projects").text
    assert "/static/session_timeout.js" in html and 'name="loc-session-max" content="7200"' in html
    # drafts are stamped with the signed-in user's id (never the email)
    uid = session_data(c)["uid"]
    assert f'<meta name="loc-session-uid" content="{uid}" />' in html, "draft owner meta missing"
    assert 'name="loc-session-uid" content="admin@' not in html
    assert 'role="alertdialog"' in html and 'id="session-announce"' in html
    # T11 (4.1.3): a sign-out while the warning is open is spoken once through
    # #session-announce (the served script; verify_a11y_static C25 pins the branch).
    js = c.get("/static/session_timeout.js").text.replace("\r\n", "\n")
    body = js.split("function signedOut() {", 1)[1].split("\n  }\n", 1)[0]
    assert body.count("announce(") == 1, "sign-out must be announced exactly once"
    assert 'announce(tr("session_out_title", "You have been signed out"));' in body
    assert body.index("} else {") < body.index("announce("), "announce only when already open"
    limits(idle=0, max_h=0)
    html = c.get("/projects").text
    assert "session_timeout.js" not in html and "session-dialog" not in html
    limits(idle=0, max_h=5)
    assert "session_timeout.js" in c.get("/projects").text, "absolute-only must still warn"
    limits()
    assert "session_timeout.js" not in TestClient(app).get("/auth/login").text
    settings.set_many({"security_auth_required": "0"})
    try:
        assert "session_timeout.js" not in TestClient(app).get("/projects").text
    finally:
        settings.set_many({"security_auth_required": "1"})
    print("  base.html: the warning loads only for a signed-in user with the wall on and a limit")


def review_247_auth_doors_honor_limits() -> None:
    """Security review 2026-09, #247: the idle and absolute limits hold on /auth doors."""
    c = login(SECOND, SECOND_PW)
    tok = token_for(c)
    tick(IDLE_MIN * 60 + 1)
    r = c.get("/auth/password-change", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/auth/login", (
        "#247: an idle-expired session opened the password change page",
        r.status_code,
        r.text[:200],
    )
    assert not session_data(c).get("uid"), "#247: the expired session must be cleared"
    new_pw = "changed-by-an-expired-cookie-1"  # pregate: allow-secret throwaway, must not land
    c2 = login(SECOND, SECOND_PW)
    tok_second = token_for(c2)
    tick(IDLE_MIN * 60 + 1)
    r = c2.post(
        "/auth/password-change",
        data={"current": SECOND_PW, "new": new_pw, "confirm": new_pw, "csrf_token": tok_second},
        follow_redirects=False,
    )
    assert r.status_code == 303 and r.headers["location"] == "/auth/login", r.status_code
    assert security.authenticate(SECOND, SECOND_PW), "#247: an expired session changed it"
    # ... and after the ABSOLUTE limit, with activity all along (keepalive keeps idle fresh)
    c3 = login(SECOND, SECOND_PW)
    tok_third = token_for(c3)
    for _ in range(MAX_H * 6):
        tick(600 - 1)
        keepalive(c3, tok_third)
    tick(20)  # 12 x 599 s + 20 s: just past session_max_hours, 20 s after the last keepalive
    assert c3.get("/auth/password-change", follow_redirects=False).status_code == 303
    assert security.session_user(SimpleNamespace(session={"uid": 1})) is None
    del tok
    print("  #247: /auth/password-change refuses a session past the idle or absolute limit")


def review_251_sign_out_is_a_csrf_post() -> None:
    """Security review 2026-09, #251: GET /auth/logout signs nobody out."""
    a = login()
    b = login()
    r = a.get("/auth/logout", headers={"Referer": "https://evil.example/x"})
    assert r.status_code == 200 and 'action="/auth/logout"' in r.text, r.status_code
    assert 'method="post"' in r.text and 'name="csrf_token"' in r.text
    assert b.get("/account", follow_redirects=False).status_code == 200, (
        "#251: a GET of /auth/logout ended another session of the account"
    )
    assert a.get("/account", follow_redirects=False).status_code == 200
    r = a.post("/auth/logout", follow_redirects=False)  # no token
    assert r.status_code == 303 and "expired=1" in r.headers["location"], r.status_code
    tok_a = token_for(a)
    r = a.post(
        "/auth/logout",
        headers={"X-CSRF-Token": tok_a, "Origin": "https://evil.example"},
        follow_redirects=False,
    )
    assert r.status_code == 403, r.status_code
    assert b.get("/account", follow_redirects=False).status_code == 200, "refused POST acted"
    r = a.post("/auth/logout", data={"csrf_token": tok_a}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/auth/login", r.status_code
    assert b.get("/account", follow_redirects=False).status_code == 303, (
        "the explicit sign-out ends every session of the account (#138)"
    )
    anon = TestClient(app).get("/auth/logout", follow_redirects=False)
    assert anon.status_code == 303 and anon.headers["location"] == "/auth/login"
    page = login().get("/projects").text
    assert 'href="/auth/logout"' not in page, "a sign-out LINK is back (it must be a form)"
    assert page.count('<form method="post" action="/auth/logout"') == 2, "menu + dialog forms"
    print("  #251: GET /auth/logout only confirms; POST needs same-origin + CSRF token")


def review_260_signed_out_page_hidden() -> None:
    """Security review 2026-09, #260: no-store on signed-in HTML; the signed-out dialog
    cannot be dismissed and hides the page."""
    c = login()
    for path in ("/", "/projects", "/account", "/auth/logout"):
        r = c.get(path)
        assert r.status_code == 200 and r.headers.get("cache-control") == "no-store", (
            path,
            r.headers.get("cache-control"),
        )
    assert c.get("/static/style.css").headers.get("cache-control") != "no-store"
    js = c.get("/static/session_timeout.js").text.replace("\r\n", "\n")
    cancel = js.split('dlg.addEventListener("cancel"', 1)[1].split("});", 1)[0]
    assert 'mode === "out"' in cancel and "preventDefault" in cancel, cancel
    close = js.split('dlg.addEventListener("close"', 1)[1].split("\n  });", 1)[0]
    assert 'if (mode === "out")' in close and "showModal()" in close, close
    signed_out = js.split("function signedOut() {", 1)[1].split("\n  }\n", 1)[0]
    assert 'document.body.classList.add("session-ended")' in signed_out
    css = c.get("/static/style.css").text
    assert "body.session-ended > :not(#session-dialog) { visibility: hidden" in css
    # #408: the forced-enrollment recovery page is a standalone shell (does not extend
    # base.html) that signs the person in and shows all ten recovery codes, so #260's
    # hide-on-timeout must be wired into it too. verify_totp_mfa proves the served page
    # carries the warning; here the template is pinned to load it under the same guard.
    shell_path = os.path.join(_ROOT, "app", "templates", "mfa_codes_standalone.html")
    with open(shell_path, encoding="utf-8") as fh:
        shell = fh.read()
    assert "{% set _st = session_timeout_config(request) %}" in shell
    assert '{% if _st %}{% include "_session_timeout.html" %}' in shell
    assert "/static/session_timeout.js" in shell and 'name="loc-session-idle"' in shell
    print("  #260: signed-in HTML is no-store; signed out stays modal and hides the page")
    print("  #408: the recovery-code standalone shell wires in the same timeout dialog + script")


def pure_clock() -> None:
    limits()
    now = 10_000
    st = security.session_clock({"la": now, "iat": now}, now)
    assert st["remaining_seconds"] == 600 and st["ending"] == "idle" and not st["expired"]
    st = security.session_clock({"la": now, "iat": now - 7200 + 30}, now)
    assert st["remaining_seconds"] == 30 and st["ending"] == "max"
    assert security.session_clock({"la": now - 601, "iat": now}, now)["expired"] == "idle"
    assert security.session_clock({"la": now - 600, "iat": now}, now)["expired"] == ""
    assert security.session_clock({"la": now, "iat": now - 7200}, now)["expired"] == "max"
    for bad in (None, "123", 1.5, True):
        assert security.session_clock({"la": now, "iat": bad}, now)["expired"] == "max", bad
    # gate LOW 3: an iat in the future beyond 300 s of skew is not extra time, it is expired
    assert security.session_clock({"la": now, "iat": now + 300}, now)["expired"] == ""
    assert security.session_clock({"la": now, "iat": now + 301}, now)["expired"] == "max"
    assert security.session_clock({"la": now, "iat": now + 86400}, now)["expired"] == "max"
    settings.set_many({"session_max_hours": "garbage"})  # a hand-edited row
    try:
        assert security.session_limits()[1] == 24 * 3600, "garbage must keep the default"
    finally:
        limits()
    # the in-process request shim: enforce_session never rewrites iat
    req = SimpleNamespace(session={"uid": 1, "la": now, "iat": now - 60})
    CLOCK[0], saved = now, CLOCK[0]
    try:
        assert security.enforce_session(req) == "" and req.session["iat"] == now - 60
    finally:
        CLOCK[0] = saved
    print("  clock: min(idle, absolute), strict idle, no-iat expired, garbage keeps default")


def main() -> None:
    init_db()
    setup_path_sets_iat()
    pure_clock()
    i1_session_is_read_only()
    i2_keepalive_needs_csrf()
    i3_i4_absolute_limit()
    i5_expired_requests()
    cookie_window_and_future_iat()
    i6_fixation_and_client_stamps()
    every_sign_in_path_sets_iat()
    settings_doors()
    base_html_loading()
    review_247_auth_doors_honor_limits()
    review_251_sign_out_is_a_csrf_post()
    review_260_signed_out_page_hidden()
    print("SESSION TIMEOUT OK")


if __name__ == "__main__":
    main()
