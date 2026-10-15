"""MCP Phase C2 (#753): the streamable-http listener as an OAuth 2.1 RESOURCE SERVER. Opt-in.

OFF unless LOC_STUDIO_MCP_OAUTH_RESOURCE is set; then LOC_STUDIO_MCP_OAUTH_ISSUER and
LOC_STUDIO_MCP_RS_SECRET are required (startup fails without them). The authorization server is
the Localization OS app itself (app/services/mcp_as.py, switched on with mcp_oauth_enabled).

What this ASGI gate does in front of the SDK app (outermost, so nothing below it runs and no
request body is buffered for a caller that proved nothing):

  * serves RFC 9728 Protected Resource Metadata at
      /.well-known/oauth-protected-resource<mcp path>   and   /.well-known/oauth-protected-resource
    { resource, authorization_servers: [issuer], scopes_supported: [api, docs, keys],
      bearer_methods_supported: [header] }
  * answers an MCP request with NO credential: 401 +
      WWW-Authenticate: Bearer resource_metadata="<PRM URL>", scope="api docs keys"
  * an invalid, expired, revoked or wrong-audience token: 401 + error="invalid_token"
  * a valid token whose grant lacks the scope a `tools/call` needs: 403 +
      error="insufficient_scope", scope="<every scope the call needs>" (one challenge, all of
      them), resource_metadata
  * a valid token: the token is EXCHANGED at the app (RFC 8693, authenticated with the
    resource-server secret) for the /api token of the same grant, and the request continues
    with ONLY that /api token in X-Loc-Token. The client's token never reaches /api (MCP: no
    token passthrough), and an /api token sent here is refused (it is not an MCP-audience
    token). Exchanges are cached by token digest for at most 5 minutes; failures for 60 s.
  * a per-user lsu_ key (Authorization: Bearer lsu_... or X-Loc-Token) passes through unchanged:
    the documented static-key alternative for Claude Code / Codex keeps working.

Documented in: docs/architecture/mcp-oauth.md
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import time
from urllib.parse import quote_plus, urlencode, urlsplit, urlunsplit

import httpx
from starlette.datastructures import Headers
from starlette.responses import JSONResponse, PlainTextResponse

SCOPES = ("api", "docs", "keys")
RS_CLIENT_ID = "loc-os-mcp-server"
TOKEN_EXCHANGE = "urn:ietf:params:oauth:grant-type:token-exchange"  # noqa: S105 (a URN)
TT_ACCESS = "urn:ietf:params:oauth:token-type:access_token"
ACCESS_PREFIX = "lsm_"
STATIC_PREFIX = "lsu_"
_POS_TTL = 300.0
_NEG_TTL = 60.0
_CACHE_MAX = 10_000


class OAuthConfig:
    def __init__(self, resource: str, issuer: str, rs_secret: str, token_url: str):
        self.resource = resource
        self.issuer = issuer
        self.rs_secret = rs_secret
        self.token_url = token_url

    @property
    def prm_url(self) -> str:
        p = urlsplit(self.resource)
        path = p.path.rstrip("/")
        return urlunsplit(
            (p.scheme, p.netloc, "/.well-known/oauth-protected-resource" + path, "", "")
        )

    def prm(self) -> dict:
        return {
            "resource": self.resource,
            "authorization_servers": [self.issuer],
            "scopes_supported": list(SCOPES),
            "bearer_methods_supported": ["header"],
            "resource_name": "Localization OS MCP server",
        }


def _https_or_loopback(url: str) -> bool:
    try:
        p = urlsplit(url)
        _ = p.port
    except ValueError:
        return False
    host = (p.hostname or "").lower()
    if not host or p.username or p.password or p.query or p.fragment:
        return False
    return p.scheme == "https" or (
        p.scheme == "http" and (host in ("localhost", "127.0.0.1", "::1"))
    )


def config_from_env(base_url: str, environ=None) -> OAuthConfig | None:
    """None when OAuth is off (no LOC_STUDIO_MCP_OAUTH_RESOURCE). Raises ValueError naming the
    variable when it is on but incomplete or malformed, so a half-configured listener never
    starts (and never silently serves anonymous tools/list)."""
    env = os.environ if environ is None else environ
    resource = (env.get("LOC_STUDIO_MCP_OAUTH_RESOURCE") or "").strip()
    if not resource:
        return None
    issuer = (env.get("LOC_STUDIO_MCP_OAUTH_ISSUER") or "").strip()
    secret = (env.get("LOC_STUDIO_MCP_RS_SECRET") or "").strip()
    if not _https_or_loopback(resource):
        raise ValueError(
            "LOC_STUDIO_MCP_OAUTH_RESOURCE must be the https URL clients connect to "
            "(http only on localhost), with no query or fragment"
        )
    if not _https_or_loopback(issuer):
        raise ValueError(
            "LOC_STUDIO_MCP_OAUTH_ISSUER must be the Localization OS app_base_url (https; "
            "http only on localhost)"
        )
    if not secret or any(ord(c) < 0x21 or ord(c) > 0x7E for c in secret):
        raise ValueError(
            "LOC_STUDIO_MCP_RS_SECRET must be the resource-server secret generated on the "
            "MCP OAuth page or with `mcp-oauth rs-secret`"
        )
    return OAuthConfig(resource, issuer, secret, base_url.rstrip("/") + "/oauth/token")


def _quoted(v: str) -> str:
    return '"' + v.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _challenge(cfg: OAuthConfig, *, error: str = "", scope: str = "", desc: str = "") -> str:
    parts = []
    if error:
        parts.append(f"error={_quoted(error)}")
    if desc:
        parts.append(f"error_description={_quoted(desc)}")
    parts.append(f"resource_metadata={_quoted(cfg.prm_url)}")
    parts.append(f"scope={_quoted(scope or ' '.join(SCOPES))}")
    return "Bearer " + ", ".join(parts)


class _Cache:
    def __init__(self):
        self._d: dict = {}

    def get(self, key):
        hit = self._d.get(key)
        if hit is None:
            return None
        if hit[0] <= time.monotonic():
            self._d.pop(key, None)
            return None
        return hit[1]

    def put(self, key, value, ttl: float):
        if len(self._d) >= _CACHE_MAX:
            now = time.monotonic()
            for k in [k for k, v in self._d.items() if v[0] <= now]:
                self._d.pop(k, None)
            if len(self._d) >= _CACHE_MAX:
                self._d.clear()
        self._d[key] = (time.monotonic() + ttl, value)


class OAuthResourceGate:
    """ASGI middleware; see the module docstring. `scope_for_tool(name)` returns the grantable
    scope a tool needs, or None when the tool needs none of them (an admin tool is left to the
    app's own 403: no OAuth grant can ever carry admin)."""

    def __init__(
        self, app, *, cfg: OAuthConfig, mcp_path: str, scope_for_tool, max_body: int, transport=None
    ):
        self.app = app
        self.cfg = cfg
        self.mcp_path = mcp_path
        self.scope_for_tool = scope_for_tool
        self.max_body = max_body
        self.transport = transport
        self.cache = _Cache()
        rpath = urlsplit(cfg.resource).path.rstrip("/")
        self.prm_paths = {
            "/.well-known/oauth-protected-resource",
            "/.well-known/oauth-protected-resource" + mcp_path.rstrip("/"),
            "/.well-known/oauth-protected-resource" + rpath,
        }

    def _path(self, scope) -> str:
        path = scope.get("path", "")
        root = scope.get("root_path", "")
        if root and path.startswith(root):
            path = path[len(root) :]
        return path

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        path = self._path(scope)
        method = scope.get("method", "GET")
        if path in self.prm_paths:
            if method not in ("GET", "HEAD"):
                return await PlainTextResponse("Method not allowed", 405)(scope, receive, send)
            return await JSONResponse(
                self.cfg.prm(), headers={"Cache-Control": "public, max-age=300"}
            )(scope, receive, send)
        if path != self.mcp_path:
            return await self.app(scope, receive, send)
        headers = Headers(scope=scope)
        auth = headers.get("authorization") or ""
        bearer = auth[7:].strip() if auth[:7].lower() == "bearer " else ""
        alias = (headers.get("x-loc-token") or "").strip()
        token = bearer or alias
        if token.startswith(STATIC_PREFIX):
            # The static per-user key path, byte-identical to before (the app verifies it).
            return await self.app(scope, receive, send)
        bearer = token
        if not bearer:
            return await self._deny(
                scope, receive, send, 401, _challenge(self.cfg), "Authorization required."
            )
        grant = await self._exchange(bearer)
        if grant == "unavailable":
            return await PlainTextResponse(
                "The authorization server is unavailable; retry shortly.",
                503,
                headers={"Retry-After": "5"},
            )(scope, receive, send)
        if grant is None:
            return await self._deny(
                scope,
                receive,
                send,
                401,
                _challenge(
                    self.cfg,
                    error="invalid_token",
                    desc="The access token is invalid, expired or not for this server.",
                ),
                "Invalid token.",
            )
        api_token, granted = grant
        body = b""
        if method == "POST":
            body, too_big = await self._read_body(receive)
            if too_big:
                return await PlainTextResponse("Request body too large", 413)(scope, receive, send)
            needed = self._needed_scopes(body)
            if needed and not needed <= granted:
                want = sorted(needed | {"api"}, key=SCOPES.index)
                return await self._deny(
                    scope,
                    receive,
                    send,
                    403,
                    _challenge(
                        self.cfg,
                        error="insufficient_scope",
                        scope=" ".join(want),
                        desc="This tool needs more access than the app was granted.",
                    ),
                    "Insufficient scope.",
                )
        new_headers = [
            (k, v)
            for k, v in scope["headers"]
            if k.lower() not in (b"authorization", b"x-loc-token")
        ]
        new_headers.append((b"x-loc-token", api_token.encode("ascii")))
        scope = dict(scope)
        scope["headers"] = new_headers
        if method == "POST":
            sent = False

            async def replay():
                nonlocal sent
                if not sent:
                    sent = True
                    return {"type": "http.request", "body": body, "more_body": False}
                return await receive()

            return await self.app(scope, replay, send)
        return await self.app(scope, receive, send)

    async def _deny(self, scope, receive, send, status, challenge, text):
        await JSONResponse(
            {"error": text},
            status_code=status,
            headers={"WWW-Authenticate": challenge, "Cache-Control": "no-store"},
        )(scope, receive, send)

    async def _read_body(self, receive):
        buf = bytearray()
        while True:
            msg = await receive()
            if msg["type"] == "http.disconnect":
                break
            buf += msg.get("body", b"")
            if len(buf) > self.max_body:
                return b"", True
            if not msg.get("more_body"):
                break
        return bytes(buf), False

    def _needed_scopes(self, body: bytes) -> set:
        try:
            msg = json.loads(body.decode("utf-8")) if body else None
        except (ValueError, UnicodeDecodeError):
            return set()  # the SDK answers the parse error itself
        msgs = msg if isinstance(msg, list) else [msg]
        need = set()
        for m in msgs:
            if not isinstance(m, dict) or m.get("method") != "tools/call":
                continue
            params = m.get("params") if isinstance(m.get("params"), dict) else {}
            name = params.get("name")
            sc = self.scope_for_tool(name) if isinstance(name, str) else None
            if sc in SCOPES:
                need.add(sc)
        return need

    async def _exchange(self, token: str):
        """(api_token, granted scopes) / None (invalid) / "unavailable" (AS unreachable)."""
        if not token.startswith(ACCESS_PREFIX) or any(
            ord(c) < 0x21 or ord(c) > 0x7E for c in token
        ):
            return None
        key = hashlib.sha256(token.encode("ascii")).hexdigest()
        hit = self.cache.get(key)
        if hit is not None:
            return None if hit == "invalid" else hit
        pair = f"{quote_plus(RS_CLIENT_ID)}:{quote_plus(self.cfg.rs_secret)}"
        basic = base64.b64encode(pair.encode("ascii")).decode("ascii")
        form = {
            "grant_type": TOKEN_EXCHANGE,
            "subject_token": token,
            "subject_token_type": TT_ACCESS,
        }
        try:
            async with httpx.AsyncClient(
                timeout=httpx.Timeout(10.0, connect=5.0),
                follow_redirects=False,
                transport=self.transport,
            ) as client:
                r = await client.post(
                    self.cfg.token_url,
                    content=urlencode(form).encode("ascii"),
                    headers={
                        "Authorization": f"Basic {basic}",
                        "Content-Type": "application/x-www-form-urlencoded",
                        "Accept": "application/json",
                    },
                )
        except httpx.HTTPError:
            return "unavailable"
        if r.status_code >= 500:
            return "unavailable"
        if r.status_code != 200:
            self.cache.put(key, "invalid", _NEG_TTL)
            return None
        try:
            doc = r.json()
            api_token = doc["access_token"]
            expires_in = float(doc.get("expires_in") or 0)
            granted = {s for s in str(doc.get("scope") or "api").split() if s in SCOPES}
        except (ValueError, KeyError, TypeError):
            return "unavailable"
        if not (isinstance(api_token, str) and api_token.startswith("lso_")):
            return "unavailable"
        granted.add("api")
        value = (api_token, frozenset(granted))
        ttl = max(1.0, min(_POS_TTL, expires_in - 5.0))
        self.cache.put(key, value, ttl)
        return value
