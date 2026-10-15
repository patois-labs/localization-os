"""Application-wide HTTP security-header middleware (Batch 10, gap G-01).

A single pure-ASGI middleware that stamps baseline browser-hardening headers onto
EVERY response from the customer app (app.main), which since 2026-09-20 is the only
mount (the back-office sub-app was removed). It NEVER clobbers a header a route already set — e.g.
images._serve_image's per-download `X-Content-Type-Options: nosniff` and the SVG
`Content-Security-Policy`, or the keys/api_cli download-path nosniff. HSTS is
emitted ONLY on https responses (max-age 15552000 = 180 days; deliberately no
`preload` and no `includeSubDomains` — tunnel subdomains like the interop
cloudflared host exist and must not be pinned to https by our header).

Pure ASGI (not BaseHTTPMiddleware) so it wraps the `http.response.start` message
without buffering the body — correct for header injection and cheap on the hot
path.

INSTALLATION — why it wraps `build_middleware_stack` instead of `add_middleware`
(F-1): Starlette's `build_middleware_stack` places `ServerErrorMiddleware`
(which renders unhandled 500s, incl. the app's `@app.exception_handler(Exception)`
handler) OUTSIDE every `add_middleware` layer. A plain `add_middleware` therefore
never sees a 500 response, so those pages shipped with ZERO security headers.
`apply_security_headers` instead wraps `build_middleware_stack` so this middleware
becomes the TRUE outermost layer — outside `ServerErrorMiddleware` AND outside any
later-added middleware (e.g. the observability metrics/OTel layer), which also
pins the "headers are always outermost" invariant (F-5).

Framing (see config.FRAME_ANCESTORS): standalone default is `X-Frame-Options:
DENY`. A deployment that must embed the web UI sets LOC_TMS_FRAME_ANCESTORS to an
origin list, and we emit a `frame-ancestors` CSP directive instead (the only way
to express an origin allowlist — X-Frame-Options cannot). When a route set its OWN
Content-Security-Policy that lacks a frame-ancestors directive (e.g. the SVG CSP),
we APPEND our frame-ancestors directive so framing protection is never dropped
(F-4). HTML also receives a nonce-based script CSP, enforced by default. The
operator can select report-only or off with LOC_TMS_CSP_MODE. Styles are outside
this policy's scope.

Documented in: docs/architecture/browser-surface.md
"""

import re
import secrets
from urllib.parse import urlsplit

from starlette.datastructures import MutableHeaders

from app import config

# (canonical name, value) — set on every response unless the route already set it.
_BASELINE: tuple[tuple[str, str], ...] = (
    ("X-Content-Type-Options", "nosniff"),
    ("Referrer-Policy", "strict-origin-when-cross-origin"),
    ("Permissions-Policy", "camera=(), microphone=(), geolocation=()"),
)
_HSTS_NAME = "Strict-Transport-Security"
# Baseline for an install WITHOUT a TLS-terminating edge. In a hosted deployment the edge
# (Cloudflare / Caddy / nginx) owns HSTS with a stricter, whole-domain value; the app never
# sends includeSubDomains because it cannot know what else lives under the domain (#614,
# docs/architecture/browser-surface.md).
_HSTS_VALUE = "max-age=15552000"
_CSP_NAME = "Content-Security-Policy"


def allow_form_action(request, url: str) -> None:
    """Allow one configured HTTP(S) origin on this response, never a URL from a form."""
    parts = urlsplit(url)
    if (
        parts.scheme not in {"http", "https"}
        or not parts.hostname
        or not re.fullmatch(r"[A-Za-z0-9.\-:\[\]]+", parts.netloc)
        or any(ord(char) <= 32 or ord(char) >= 127 for char in url)
    ):
        raise ValueError("Invalid configured form destination origin")
    # Accessing port also rejects malformed and out-of-range ports.
    _ = parts.port
    origin = f"{parts.scheme}://{parts.netloc}"
    origins = request.scope.setdefault("state", {}).setdefault("csp_form_actions", [])
    if origin not in origins:
        origins.append(origin)


class SecurityHeadersMiddleware:
    def __init__(self, app, *, frame_ancestors: str = "", csp_mode: str | None = None) -> None:
        self.app = app
        self._frame_ancestors = (frame_ancestors or "").strip()
        self._csp_mode = (
            config.CSP_MODE if csp_mode is None else config._validate_csp_mode(csp_mode)
        )

    def _apply_framing(self, headers: MutableHeaders) -> None:
        """DENY via X-Frame-Options by default; a `frame-ancestors` CSP when an
        origin allowlist is configured (X-Frame-Options can't express one). When a
        route already set a CSP that lacks frame-ancestors, APPEND the directive so
        framing protection survives (F-4)."""
        if not self._frame_ancestors:
            if "X-Frame-Options" not in headers:
                headers["X-Frame-Options"] = "DENY"
            return
        directive = f"frame-ancestors {self._frame_ancestors}"
        existing = headers.get(_CSP_NAME)
        if not existing:
            headers[_CSP_NAME] = directive
            return
        if "frame-ancestors" in existing.lower():
            return  # the route already constrained framing — do not fight it
        sep = "" if existing.rstrip().endswith(";") else "; "
        headers[_CSP_NAME] = f"{existing.rstrip()}{sep}{directive}"

    async def __call__(self, scope, receive, send) -> None:
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return
        is_https = scope.get("scheme") == "https"
        nonce = secrets.token_urlsafe(16)
        scope.setdefault("state", {})["csp_nonce"] = nonce

        async def _send(message) -> None:
            if message["type"] == "http.response.start":
                # F-7: the ASGI spec allows `headers` to be absent on the start
                # message; MutableHeaders(scope=...) would KeyError without this.
                message.setdefault("headers", [])
                headers = MutableHeaders(scope=message)
                for name, value in _BASELINE:
                    if name not in headers:  # case-insensitive; never clobber a route's own
                        headers[name] = value
                if (
                    self._csp_mode != "off"
                    and headers.get("content-type", "").split(";")[0].strip().lower() == "text/html"
                    and headers.get("content-disposition", "").split(";")[0].strip().lower()
                    != "attachment"
                    and _CSP_NAME not in headers
                ):
                    name = _CSP_NAME
                    if self._csp_mode == "report-only":
                        name += "-Report-Only"
                    origins = scope["state"].get("csp_form_actions", [])
                    form_actions = " ".join(["'self'", *origins])
                    headers[name] = (
                        f"script-src 'self' 'nonce-{nonce}'; object-src 'none'; "
                        f"base-uri 'none'; form-action {form_actions};"
                    )
                self._apply_framing(headers)
                if is_https and _HSTS_NAME not in headers:
                    headers[_HSTS_NAME] = _HSTS_VALUE
            await send(message)

        await self.app(scope, receive, _send)


def apply_security_headers(app, *, frame_ancestors: str = "") -> None:
    """Install the middleware as the TRUE outermost layer by wrapping
    `build_middleware_stack`, so it also stamps responses generated by Starlette's
    `ServerErrorMiddleware` (unhandled 500s) — which `build_middleware_stack`
    otherwise places OUTSIDE any `add_middleware` layer (F-1). The stack is built
    lazily on first request, so this reassignment is safe at import time and picks
    up every middleware added before then (including the observability layer)."""
    _base_build = app.build_middleware_stack

    def _build():
        return SecurityHeadersMiddleware(_base_build(), frame_ancestors=frame_ancestors)

    app.build_middleware_stack = _build
