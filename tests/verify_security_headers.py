"""Browser-surface hardening verification (Batch 10, gaps G-01 / G-02) — offline.

Covers the application-wide security-header middleware and the boot-resolved
Secure-cookie posture:

- baseline headers (X-Content-Type-Options / Referrer-Policy / Permissions-Policy /
  X-Frame-Options) present on customer web, /api, 404, UNHANDLED-500, and
  back-office responses;
- HSTS present ONLY on https responses, absent on http, and carrying neither
  `preload` nor `includeSubDomains`;
- the header middleware is the TRUE outermost layer (F-5), so it stamps the 500
  page ServerErrorMiddleware renders (F-1);
- route-set headers (images._serve_image's SVG CSP + nosniff) are NOT clobbered,
  and a configured frame-ancestors directive is APPENDED to a route CSP that lacks
  one (F-4);
- HTML receives an enforced nonce-based script CSP by default;
- framing default = DENY, and the configurable `frame-ancestors` relaxation an
  embedding deployment would use (the embedded/headless profile serves NO web
  surface, so this relaxation is the only iframe path there ever is);
- an ASGI response with no `headers` key on http.response.start is handled (F-7);
- session cookie Secure flag: absent under the local-http-dev default, present
  when the boot resolution turns it on; the resolution rules; the loud failure on
  an unrecognized LOC_TMS_SECURE_COOKIES (F-3) or an injection-shaped
  LOC_TMS_FRAME_ANCESTORS (F-6).
"""

import asyncio
import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(_ROOT)
sys.path.insert(0, _ROOT)
from tests._bootstrap import init_env

init_env("loctms_sechdr_")

from fastapi import FastAPI, Request
from fastapi.responses import PlainTextResponse, Response
from fastapi.testclient import TestClient
from starlette.middleware.sessions import SessionMiddleware

from app import config
from app.config import (
    BrowserSurfaceConfigError,
    _resolve_secure_cookies,
    _validate_frame_ancestors,
)
from app.db import init_db
from app.kernel import access
from app.main import app
from app.services import settings
from app.web import images
from app.web.security_headers import SecurityHeadersMiddleware, apply_security_headers


def _assert_baseline(h, *, where: str) -> None:
    assert h.get("X-Content-Type-Options") == "nosniff", (where, dict(h))
    assert h.get("Referrer-Policy") == "strict-origin-when-cross-origin", (where, dict(h))
    pp = h.get("Permissions-Policy") or ""
    assert "camera=()" in pp and "microphone=()" in pp and "geolocation=()" in pp, (where, pp)


def _raises(fn, *args, exc=BrowserSurfaceConfigError, **kwargs) -> bool:
    try:
        fn(*args, **kwargs)
    except exc:
        return True
    return False


def _check_resolution() -> None:
    # auto (default): off with no https signal, on under Fly or an https public base.
    assert _resolve_secure_cookies("auto", fly_app_name="", public_base="") is False
    assert _resolve_secure_cookies("auto", fly_app_name="loc-tms", public_base="") is True
    assert _resolve_secure_cookies("auto", fly_app_name="", public_base="https://t.example") is True
    assert _resolve_secure_cookies("auto", fly_app_name="", public_base="http://t.example") is False
    # explicit force overrides every signal.
    assert _resolve_secure_cookies("1", fly_app_name="", public_base="") is True
    assert _resolve_secure_cookies("on", fly_app_name="", public_base="") is True
    assert _resolve_secure_cookies("0", fly_app_name="loc-tms", public_base="https://x") is False
    assert _resolve_secure_cookies("off", fly_app_name="loc-tms", public_base="") is False
    # blank falls back to auto semantics.
    assert _resolve_secure_cookies("", fly_app_name="", public_base="") is False
    # F-3: an unrecognized value must FAIL LOUD, never silently fall back to auto.
    for bad in ("banana", "ture", "enabled", "secure", "yes-please"):
        assert _raises(_resolve_secure_cookies, bad, fly_app_name="", public_base=""), bad
    # The hermetic suite has no Fly/https signal, so the app booted secure-OFF.
    assert config.SECURE_COOKIES is False, config.SECURE_COOKIES
    print("  resolution: auto/Fly/https/force correct; unrecognized value fails loud (F-3)")


def _check_frame_ancestors_validation() -> None:
    # F-6: valid values pass (trimmed); injection-shaped values fail LOUD at boot.
    assert _validate_frame_ancestors("") == ""
    assert _validate_frame_ancestors("  https://a https://b ") == "https://a https://b"
    for bad in (
        "a\r\nSet-Cookie: x=y",  # CRLF header injection
        "a\nb",  # bare LF
        "a\x00b",  # NUL / control
        "self; script-src *",  # ';' opens a second directive
        "https://日本.example",  # non-ASCII (multi-byte)
        "https://é.example",  # non-ASCII in the latin-1 0x80-0xFF range (R-1)
    ):
        assert _raises(_validate_frame_ancestors, bad), bad
    print("  frame-ancestors validation: CRLF/control/';'/non-ASCII all fail loud (F-6/R-1)")


def _check_stack_outermost() -> None:
    # The customer app's SessionMiddleware must carry the boot-resolved flag.
    # 2026-09-20: was _ScopedSessionMiddleware, a subclass whose only job was to go inert
    # on /back-office so the operator's separate bo_session cookie could not collide with
    # the customer session. The console is gone, so there is one session and one class.
    session_mw = [mw for mw in app.user_middleware if getattr(mw, "cls", None) is SessionMiddleware]
    assert session_mw, "customer app has no SessionMiddleware"
    assert session_mw[0].kwargs.get("https_only") == config.SECURE_COOKIES, session_mw[0].kwargs
    # F-5/F-1: the header middleware is the TRUE outermost layer — building the full
    # stack yields a SecurityHeadersMiddleware wrapping everything (incl. Starlette's
    # ServerErrorMiddleware and the observability layer added later at import time).
    from app.main import HostBoundaryMiddleware

    stack = app.build_middleware_stack()
    assert isinstance(stack, HostBoundaryMiddleware)
    assert isinstance(stack.app, SecurityHeadersMiddleware)
    assert isinstance(stack.rejection_app, SecurityHeadersMiddleware)
    print("  stack: SessionMiddleware.https_only bound; header mw outermost (F-5)")


def _mini_app(mw_kwargs: dict, *, route_headers: dict | None = None, raises: bool = False):
    m = FastAPI()

    @m.get("/x")
    def _x():
        if raises:
            raise RuntimeError("boom")
        return PlainTextResponse("ok", headers=route_headers or {})

    apply_security_headers(m, **mw_kwargs)
    return TestClient(m, raise_server_exceptions=not raises)


def _check_framing_behavior() -> None:
    # Default standalone: X-Frame-Options DENY, and NO frame-ancestors CSP.
    r = _mini_app({}).get("/x")
    assert r.headers.get("X-Frame-Options") == "DENY", dict(r.headers)
    assert "Content-Security-Policy" not in r.headers, dict(r.headers)
    _assert_baseline(r.headers, where="mini-default")
    # Embedding deployment (the only iframe path — embedded profile has no web
    # surface otherwise): frame-ancestors CSP replaces X-Frame-Options.
    r2 = _mini_app({"frame_ancestors": "https://host.example"}).get("/x")
    assert "X-Frame-Options" not in r2.headers, dict(r2.headers)
    assert r2.headers.get("Content-Security-Policy") == "frame-ancestors https://host.example"
    print("  framing behavior: default DENY (no CSP); configured frame-ancestors relaxation")


def _check_no_clobber_and_append() -> None:
    # A route that sets its OWN nosniff + CSP (exactly images._serve_image's SVG
    # contract) must keep them; the middleware only fills what is absent.
    route_headers = {
        "X-Content-Type-Options": "nosniff",
        "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'; sandbox",
    }
    r = _mini_app({}, route_headers=route_headers).get("/x")
    assert r.headers["X-Content-Type-Options"] == "nosniff"
    # The route's CSP survives — NOT overwritten (default framing uses X-Frame-Options).
    assert r.headers["Content-Security-Policy"] == route_headers["Content-Security-Policy"]
    assert r.headers.get("Referrer-Policy") == "strict-origin-when-cross-origin"
    assert r.headers.get("X-Frame-Options") == "DENY"
    # F-4: with frame-ancestors CONFIGURED, a route CSP that lacks frame-ancestors
    # gets our directive APPENDED (framing protection is never dropped), and
    # X-Frame-Options is NOT added in CSP mode.
    r2 = _mini_app(
        {"frame_ancestors": "https://host.example"},
        route_headers={"Content-Security-Policy": "default-src 'none'"},
    ).get("/x")
    assert r2.headers["Content-Security-Policy"] == (
        "default-src 'none'; frame-ancestors https://host.example"
    ), r2.headers["Content-Security-Policy"]
    assert "X-Frame-Options" not in r2.headers
    # A route CSP that ALREADY constrains framing is left untouched.
    r3 = _mini_app(
        {"frame_ancestors": "https://host.example"},
        route_headers={"Content-Security-Policy": "frame-ancestors 'self'"},
    ).get("/x")
    assert r3.headers["Content-Security-Policy"] == "frame-ancestors 'self'"
    # And the real images._serve_image still emits its SVG CSP contract intact.
    svg = images._serve_image(b"<svg xmlns='http://www.w3.org/2000/svg'/>", "svg")
    assert svg.headers.get("X-Content-Type-Options") == "nosniff"
    assert svg.headers.get("Content-Security-Policy")
    print("  no-clobber: route CSP preserved; frame-ancestors appended when configured (F-4)")


def _check_missing_headers_key() -> None:
    # F-7: the ASGI spec allows http.response.start with NO `headers` key. The
    # middleware must add it, not KeyError. Drive a raw inner ASGI app directly.
    async def _bare(scope, receive, send):
        await send({"type": "http.response.start", "status": 200})  # no "headers"
        await send({"type": "http.response.body", "body": b"ok"})

    mw = SecurityHeadersMiddleware(_bare, frame_ancestors="")
    captured: list = []

    async def _receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def _send(m):
        captured.append(m)

    scope = {"type": "http", "scheme": "http", "method": "GET", "path": "/", "headers": []}
    asyncio.run(mw(scope, _receive, _send))
    start = next(m for m in captured if m["type"] == "http.response.start")
    got = {k.decode().lower(): v.decode() for k, v in start["headers"]}
    assert got.get("x-content-type-options") == "nosniff", got
    assert got.get("x-frame-options") == "DENY", got
    print("  missing-headers-key http.response.start handled without KeyError (F-7)")


def _check_cookie_secure_flag() -> None:
    # Behavioral: the exact middleware class the app uses, with the flag forced ON,
    # marks the session cookie Secure; forced OFF, it does not.
    def _cookie_app(https_only: bool) -> str:
        m = FastAPI()

        @m.get("/set")
        def _set(request: Request):
            request.session["k"] = "v"
            return Response("ok")

        m.add_middleware(
            SessionMiddleware,
            secret_key="test-secret",
            same_site="lax",
            https_only=https_only,
            max_age=3600,
        )
        return TestClient(m).get("/set").headers.get("set-cookie", "")

    assert "secure" in _cookie_app(True).lower()
    assert "secure" not in _cookie_app(False).lower()
    print("  cookie Secure: https_only=True -> Secure attribute; False -> absent")


def _check_error_500() -> None:
    # F-1: an UNHANDLED exception is rendered as a 500 by Starlette's
    # ServerErrorMiddleware (via @app.exception_handler(Exception)), which sits
    # OUTSIDE add_middleware. Because apply_security_headers wraps
    # build_middleware_stack, the header middleware is outside THAT too, so the 500
    # still carries the headers (incl. HSTS on https). Auth is off here (default),
    # so nothing short-circuits before the raising route.
    async def _boom(request: Request):
        raise RuntimeError("boom for verify_security_headers")

    app.add_route("/__sechdr_boom", _boom)
    http = TestClient(app, raise_server_exceptions=False)
    r = http.get("/__sechdr_boom")
    assert r.status_code == 500, r.status_code
    _assert_baseline(r.headers, where="unhandled 500")
    assert r.headers.get("X-Frame-Options") == "DENY", dict(r.headers)
    https = TestClient(app, base_url="https://testserver", raise_server_exceptions=False)
    r = https.get("/__sechdr_boom")
    assert r.status_code == 500, r.status_code
    _assert_baseline(r.headers, where="unhandled 500 https")
    assert r.headers.get("Strict-Transport-Security") == "max-age=15552000", dict(r.headers)
    print("  unhandled 500 carries baseline + HSTS(https) — outermost wrap works (F-1)")


def _check_live_responses() -> None:
    http = TestClient(app)  # base_url http://testserver
    https = TestClient(app, base_url="https://testserver")

    # Customer web (unauthenticated is fine — headers are status-independent).
    for path, client, scheme in (
        ("/auth/login", http, "http"),
        ("/auth/login", https, "https"),
    ):
        r = client.get(path)
        _assert_baseline(r.headers, where=f"web {scheme} {path}")
        assert r.headers.get("X-Frame-Options") == "DENY", (scheme, dict(r.headers))
        assert "script-src 'self' 'nonce-" in r.headers["Content-Security-Policy"]
        if scheme == "https":
            assert r.headers.get("Strict-Transport-Security") == "max-age=15552000", dict(r.headers)
            hsts = r.headers["Strict-Transport-Security"]
            assert "includesubdomains" not in hsts.lower() and "preload" not in hsts.lower(), hsts
        else:
            assert "Strict-Transport-Security" not in r.headers, dict(r.headers)

    # Default local-http session cookie must NOT be Secure (a GET mints session csrf).
    setc = http.get("/auth/login").headers.get("set-cookie", "")
    if setc:  # a session cookie is set on first render
        assert "secure" not in setc.lower(), setc

    # /api tokenless -> 401, still carries the headers.
    r = http.get("/api/projects")
    _assert_baseline(r.headers, where="api 401")
    assert r.headers.get("X-Frame-Options") == "DENY"

    # A 404 carries them too (the outermost middleware wraps every response).
    r = http.get("/definitely-not-a-real-route-xyz")
    assert r.status_code == 404
    _assert_baseline(r.headers, where="404")

    # Back-office mount: covered whether the sacred gate 404s it (multi-org off) or
    # serves the login page (multi-org on).
    r = http.get("/back-office/login")
    _assert_baseline(r.headers, where="back-office (gate)")
    assert r.headers.get("X-Frame-Options") == "DENY"

    # multi_org_active() = auth on AND multi_org_enabled; turn both on so the mount
    # actually renders (the open login page), then restore. Done LAST so enabling the
    # login wall cannot disturb the earlier unauthenticated customer-app checks.
    settings.set_many({"security_auth_required": "1", "multi_org_enabled": "1"})
    assert access.multi_org_active(), "auth + multi_org should activate the back-office"
    r = http.get("/back-office/login")
    assert r.status_code == 200, r.status_code
    _assert_baseline(r.headers, where="back-office login 200")
    assert r.headers.get("X-Frame-Options") == "DENY"
    # HSTS reaches the sub-app on https too.
    r = https.get("/back-office/login")
    assert r.headers.get("Strict-Transport-Security") == "max-age=15552000", dict(r.headers)
    settings.set_many({"security_auth_required": "0", "multi_org_enabled": "0"})
    print("  live: web/api/404/back-office baseline; HSTS https-only; HTML script CSP")


def main() -> None:
    init_db()
    _check_resolution()
    _check_frame_ancestors_validation()
    _check_stack_outermost()
    _check_framing_behavior()
    _check_no_clobber_and_append()
    _check_missing_headers_key()
    _check_cookie_secure_flag()
    _check_error_500()
    _check_live_responses()
    print("verify_security_headers OK")


if __name__ == "__main__":
    main()
