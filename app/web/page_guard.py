"""Page guard: query-string messages and signed-in page caching (security review 2026-09).

Two jobs for every browser page, done at ONE seam (a pure ASGI middleware mounted just
inside the session middleware, so ``scope["session"]`` is available both ways):

1. Query-string messages (#248, #270 ``?notice=`` / ``?msg=``). Routes report outcomes
   by redirecting to ``/page?err=<text>`` (and ``?error=``, ``?msg=``, ``?notice=``,
   ``?key_error=`` and the other ``*_error`` / ``*_msg`` / ``*_warning`` names). A crafted
   link could carry the same parameters with attacker-written text, and the product would
   show it as its own message. So a message parameter whose value is FREE TEXT is only
   believed when this browser session was sent there by the server:

   * outbound: a redirect answering a mutating request (POST/PUT/PATCH/DELETE, which the
     security middleware already holds to the same-origin + CSRF token checks; never an
     /auth request, whose target is a caller-supplied ``next``) has every
     free-text message parameter in its ``Location`` recorded in the session as a short
     hash (``QM_KEY``, the newest ``QM_MAX`` kept). A GET route that emits server-written
     text records it explicitly with ``attest`` / ``attest_url``;
   * inbound: on every page request, a message parameter whose value is free text and is
     NOT recorded for this session is removed from the query string before any route or
     template reads it. Codes (``form_errors.lookup`` hits and code-shaped tokens) pass
     unchanged: they only ever render the product's own sentences.
   * the sign-in and other ``/auth/*`` pages accept KNOWN codes only (the a11y T6
     guarantee: no pre-auth content spoofing), recorded or not.

   Nothing on the wire changes: every existing ``Location`` stays byte-identical, and a
   message survives a reload because the record lives in the (signed) session cookie.

2. ``Cache-Control: no-store`` on HTML answered to a signed-in session (#260), so the back
   button after a sign-out or a timeout cannot bring confidential pages back from the
   browser cache. A response that already sets Cache-Control keeps its own.

Documented in: docs/architecture/browser-surface.md
"""

import hashlib
import re
from urllib.parse import parse_qsl

from app.kernel import oidc
from app.web import form_errors
from app.web import route_path as routing

#: Session key holding the recorded message hashes (newest last).
QM_KEY = "qm"
#: How many recorded messages a session keeps (a handful of redirects in flight).
QM_MAX = 12

#: Parameters the page-error resolver turns into a sentence (form_errors.resolve). There a
#: KNOWN code (a form_errors.lookup hit) is the product's own sentence and needs no record.
#: Every other message value, code-shaped or not, must be recorded (gate round 1 LOW-2: an
#: unknown code used to show as the generic sentence's reference, and a short token such
#: as ?notice=call_555_0100 printed as written).
_RESOLVED = re.compile(r"err|error|[a-z]\w*_error")

#: Machine and asset surfaces: no page renders there, so no message is filtered.
_EXEMPT_PREFIXES = ("/api", "/static/", "/webhooks/", "/scim/")
_EXEMPT_EXACT = frozenset({"/static", "/health", "/ready", "/metrics", "/utcp", "/openapi.json"})
#: OAuth redirect targets: the provider's ?error= is INPUT to these routes (never rendered
#: from the query), and each one shows it only when it is an OAuth error code.
_PROVIDER_CALLBACKS = frozenset({"/auth/oidc/callback", "/connectors/oauth/callback"})

_MUTATING = frozenset({"POST", "PUT", "PATCH", "DELETE"})

#: Per-PAGE message parameters (security review 2026-10 web-02, the rest of #270). These
#: names are plain flags or filters on other pages (?saved=1, /projects/{id}/keys?deleted=1),
#: but on these pages the value itself is printed in a success banner, so free text there
#: must be server-recorded exactly like ?err= / ?msg=. Every one is written by a POST
#: redirect (recorded automatically), so nothing on the wire changes.
_PAGE_MESSAGE_PARAMS = {
    "/users": frozenset({"invited", "revoked", "deleted", "erased"}),
    "/settings/enterprise": frozenset({"saved"}),
}
#: The same, for pages whose path carries an id. Gate i430: /projects/{id}/keys prints its
#: ?ok= flash verbatim in a green banner (keys_common._back writes it after every key-grid
#: POST, so it is always recorded).
_PAGE_MESSAGE_PATTERNS = ((re.compile(r"/projects/[^/]+/keys"), frozenset({"ok"})),)


def is_message_param(name: str) -> bool:
    """True for a query parameter that carries a message a page shows (the one definition
    lives beside the post-sign-in `next` sanitizer, which strips the same names)."""
    return oidc.is_message_param(name)


def _is_page_message(path: str, name: str) -> bool:
    """is_message_param plus the names that carry a message on ``path`` only."""
    if is_message_param(name) or name in _PAGE_MESSAGE_PARAMS.get(path, ()):
        return True
    return any(name in names and rx.fullmatch(path) for rx, names in _PAGE_MESSAGE_PATTERNS)


def _is_code(name: str, value: str) -> bool:
    """A value that can only render the product's own words (never needs a record)."""
    v = value.strip()
    if not v:
        return True
    return bool(_RESOLVED.fullmatch(name) and form_errors.lookup(v))


def _digest(name: str, value: str) -> str:
    return hashlib.sha256(f"{name}\x00{value}".encode()).hexdigest()[:20]


def _record(session, name: str, value: str) -> None:
    if not isinstance(session, dict):
        return
    digest = _digest(name, value)
    seen = [d for d in (session.get(QM_KEY) or []) if isinstance(d, str) and d != digest]
    seen.append(digest)
    session[QM_KEY] = seen[-QM_MAX:]


def _pairs(query: str):
    """(name, value) pairs of a query string, decoded exactly as Starlette decodes them."""
    return parse_qsl(query, keep_blank_values=True)


def _record_url(session, url: str) -> None:
    url = str(url or "")
    if not url.startswith("/") or url.startswith("//") or "?" not in url:
        return
    path, query = url.split("#", 1)[0].split("?", 1)
    for name, value in _pairs(query):
        if _is_page_message(path, name) and not _is_code(name, value):
            _record(session, name, value)


def attest(request, name: str, value: str) -> None:
    """Record server-written message text for this session (a GET route that redirects
    with a free-text message it wrote itself; never text taken from the request)."""
    _record(getattr(request, "session", None), name, str(value))


def attest_url(request, url: str) -> str:
    """Record every free-text message parameter of a same-site ``url`` (for a route that
    hands the URL to a script instead of redirecting). Returns ``url`` unchanged."""
    _record_url(getattr(request, "session", None), url)
    return url


def _is_exempt(path: str) -> bool:
    return path in _EXEMPT_EXACT or path in _PROVIDER_CALLBACKS or path.startswith(_EXEMPT_PREFIXES)


def filter_query(raw: bytes, path: str, session) -> bytes:
    """``raw`` without the message parameters this session may not be shown. Segments are
    dropped from the RAW bytes, so everything kept stays byte-identical."""
    try:
        text = raw.decode("latin-1")
    except Exception:  # pragma: no cover - latin-1 decodes every byte
        return raw
    auth_page = path == "/auth" or path.startswith("/auth/")
    seen = session.get(QM_KEY) if isinstance(session, dict) else None
    seen = set(seen) if isinstance(seen, list) else set()
    kept, dropped = [], False
    for segment in text.split("&"):
        pair = _pairs(segment)
        if len(pair) == 1 and _is_page_message(path, pair[0][0]):
            name, value = pair[0]
            if auth_page:
                drop = bool(value.strip()) and not form_errors.lookup(value.strip())
            else:
                drop = not _is_code(name, value) and _digest(name, value) not in seen
            if drop:
                dropped = True
                continue
        kept.append(segment)
    if not dropped:
        return raw
    return "&".join(kept).encode("latin-1")


class PageGuardMiddleware:
    """The ASGI layer for both jobs (see the module docstring)."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return
        path = routing.route_path(scope)
        if _is_exempt(path):
            await self.app(scope, receive, send)
            return
        session = scope.get("session")
        raw = scope.get("query_string") or b""
        if raw and b"=" in raw:
            scope["query_string"] = filter_query(raw, path, session)
        signed_in = bool(isinstance(session, dict) and session.get("uid"))
        # Gate round 1 MEDIUM-1: an /auth request (sign-in, two-step code, SAML ACS,
        # password change, sign-out) redirects to a CALLER-SUPPLIED `next`, so its Location
        # is never recorded as server-written text (the sanitizer also strips messages).
        auth_request = path == "/auth" or path.startswith("/auth/")
        mutating = scope.get("method", "GET") in _MUTATING and not auth_request

        async def guarded_send(message):
            if message.get("type") == "http.response.start":
                status = int(message.get("status") or 0)
                headers = list(message.get("headers") or [])
                if mutating and 300 <= status < 400 and isinstance(session, dict):
                    for key, value in headers:
                        if key.lower() == b"location":
                            _record_url(session, value.decode("latin-1"))
                now_signed_in = isinstance(session, dict) and session.get("uid")
                if signed_in or now_signed_in:
                    names = {k.lower() for k, _v in headers}
                    ctype = next((v for k, v in headers if k.lower() == b"content-type"), b"")
                    if ctype.lower().startswith(b"text/html") and b"cache-control" not in names:
                        headers.append((b"cache-control", b"no-store"))
                        message = {**message, "headers": headers}
            await send(message)

        await self.app(scope, receive, guarded_send)
