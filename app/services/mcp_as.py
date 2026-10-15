"""MCP OAuth 2.1 authorization server (#753, MCP Phase C2). OFF by default.

When an operator turns it on (`mcp_oauth_enabled`, plus the canonical MCP URL in
`mcp_oauth_resource` and a public https `app_base_url`), this install is the authorization
server its own remote MCP server (integrations/mcp-server, the RESOURCE server) points clients
at through RFC 9728 protected resource metadata. Directory-style clients (claude.ai custom
connectors, ChatGPT connectors) can then connect with a browser sign-in and a consent page
instead of a pasted `lsu_` key. Per-user `lsu_` keys are untouched and stay the documented
alternative.

Surface (all in app/web/mcp_oauth_as.py):
  GET  /.well-known/oauth-authorization-server   RFC 8414 metadata (404 while off)
  GET  /oauth/authorize                           validate, then send the browser to consent
  GET  /oauth/consent  POST /oauth/consent        signed-in consent page (CSRF, session wall)
  POST /oauth/token                               code, refresh_token, token-exchange grants
  POST /oauth/register                            RFC 7591 DCR, only with mcp_oauth_dcr_enabled

Clients: Client ID Metadata Documents first (an https URL with a path IS the client id; the
document is fetched through the SSRF egress guard, no redirects, 64 KiB cap, cached, optional
host trust list `mcp_oauth_cimd_hosts`); deprecated dynamic registration only when switched on
(stateless: the client id is a signed record of the registered metadata, so registration
writes nothing). Public clients only (token_endpoint_auth_method "none", PKCE S256 required).
A loopback http redirect URI matches on any port (RFC 8252 7.3).

Tokens (opaque, random, stored as SHA-256 digests on McpOAuthGrant, never in logs):
  lsm_  the MCP access token (1 hour), audience = the configured resource (RFC 8707);
  lsr_  the refresh token (30 days, rotated on every use; a reused one revokes the grant);
  lso_  the /api token. The MCP server NEVER forwards the client's token to /api (MCP: no
        token passthrough). It authenticates itself as the resource server (`mcp_oauth_rs_
        secret_hash`) and exchanges the lsm_ token (RFC 8693) for the lso_ token, which is an
        HMAC of the lsm_ token under the install's session secret: the same answer for every
        MCP server process, valid exactly as long as the lsm_ token, and accepted ONLY by /api
        (deps._resolve_api_identity). An lsm_ token sent to /api, or an lso_ token sent to the
        MCP server, is refused. Without the resource-server secret a client cannot exchange
        its own token, so the audience binding holds.

Revocation: the user (web /account, `mcp-oauth grants`, /api), and every account disable or
erasure (apikeys.revoke_all_for_user calls revoke_all_for_user here). A revoked grant kills its
lsm_, lso_ and lsr_ tokens at once.

Documented in: docs/architecture/mcp-oauth.md
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import re
import secrets
import threading
import time
from datetime import datetime, timedelta
from urllib.parse import urlencode, urlsplit

import httpx
import itsdangerous
from sqlmodel import Session, select

from app import config
from app.db import engine
from app.models import McpOAuthGrant, User
from app.services import access, egress, settings
from app.services.mcp_as_common import (
    GRANTABLE_SCOPES,
    host_trusted,
    is_loopback_host,
    normalize_resource,
    parse_hosts,
    resource_problem,
)

log = logging.getLogger("loc_tms.mcp_oauth")

ACCESS_PREFIX = "lsm_"
REFRESH_PREFIX = "lsr_"
API_PREFIX = "lso_"
DCR_PREFIX = "dcr_"
RS_CLIENT_ID = "loc-os-mcp-server"
TOKEN_EXCHANGE = "urn:ietf:params:oauth:grant-type:token-exchange"  # noqa: S105 (a URN)
TT_ACCESS = "urn:ietf:params:oauth:token-type:access_token"

CODE_TTL = 60
ACCESS_TTL = 3600
REFRESH_TTL = 30 * 24 * 3600
REQUEST_TTL = 600  # a signed authorization request must be approved within 10 minutes
MAX_GRANTS_PER_USER = 50
MAX_DOC = 64 * 1024
MAX_REDIRECTS = 5
_CIMD_TIMEOUT = 5.0
_CIMD_CACHE_MAX = 256
_CIMD_DEFAULT_TTL = 300
_CIMD_MAX_TTL = 3600

_VERIFIER_RE = re.compile(r"[A-Za-z0-9\-._~]{43,128}")
_CHALLENGE_RE = re.compile(r"[A-Za-z0-9\-_]{43}")
_STATE_MAX = 1024
#: The netloc shape app/web/security_headers.allow_form_action accepts (host[:port]).
_NETLOC_RE = re.compile(r"[A-Za-z0-9.\-:\[\]]+")
MAX_CLIENT_ID = 2048

#: Per client-IP budgets (requests per minute) for the unauthenticated endpoints (gate). The
#: resource server's token exchange is NOT charged here: every exchange comes from the MCP
#: server's one address and is authenticated with the resource-server secret.
AUTHORIZE_PER_MINUTE = 60
TOKEN_PER_MINUTE = 120
REGISTER_PER_MINUTE = 10


def rate_limited(kind: str, ip: str, per_minute: int) -> int:
    """0 when allowed, else the Retry-After seconds (the shared app/services/ratelimit window,
    namespaced so it never shares a budget with /api)."""
    from app.services import ratelimit

    allowed, retry_after = ratelimit.check(f"mcp-oauth:{kind}:{ip or '-'}", limit=per_minute)
    return 0 if allowed else max(1, retry_after)


_NAME_RE = re.compile(r"[^\x00-\x1f\x7f]{1,100}")


class OAuthError(Exception):
    """An OAuth error: `error` is the RFC 6749 code, `description` safe to show. `redirect`
    False means the client or redirect URI could not be trusted, so the error is shown on our
    own page and the browser is NEVER sent to the presented redirect URI (open redirector)."""

    def __init__(
        self, error: str, description: str, *, redirect: bool = True, status=400, target=None
    ):
        super().__init__(description)
        self.error = error
        self.description = description
        self.redirect = redirect and bool(target)
        self.status = status
        #: {redirect_uri, state} of a VALIDATED client redirect, or None.
        self.target = target


# --------------------------------------------------------------------------- configuration
def issuer() -> str:
    """This authorization server's issuer: the configured app_base_url, exactly (never the
    request Host, which a caller controls)."""
    return settings.configured_external_base_url()


def resource() -> str:
    return (settings.get("mcp_oauth_resource") or "").strip()


def dcr_enabled() -> bool:
    return settings.get("mcp_oauth_dcr_enabled") == "1"


def problems() -> list:
    """Every reason the switched-on server cannot run (empty = ready). Shown on the web page,
    by `mcp-oauth status` and over /api so an operator sees why discovery answers 404."""
    out = []
    from app.services import security

    if not security.auth_required():
        out.append("sign-in is off (security_auth_required): OAuth needs real user accounts")
    iss = issuer()
    if not iss:
        out.append("app_base_url is not set: it is the OAuth issuer")
    else:
        p = urlsplit(iss)
        if p.scheme != "https" and not is_loopback_host(p.hostname or ""):
            out.append("app_base_url must be https (http only on localhost)")
        if p.path not in ("", "/"):
            out.append(
                "app_base_url has a path: the RFC 8414 metadata must be served at the host root"
            )
    res = resource()
    problem = resource_problem(res)
    if problem:
        out.append(f"mcp_oauth_resource {problem}")
    if not (settings.get("mcp_oauth_rs_secret_hash") or "").strip():
        out.append(
            "no resource-server secret: generate one (`mcp-oauth rs-secret`) and give it to the "
            "MCP server as LOC_STUDIO_MCP_RS_SECRET"
        )
    return out


def active() -> bool:
    return settings.get("mcp_oauth_enabled") == "1" and not problems()


def status() -> dict:
    """The operator view shared by the web page (Security > MCP OAuth) and `mcp-oauth status`.
    The switches are host-only, so /api exposes no status route (only a person's own grants)."""
    return {
        "enabled": settings.get("mcp_oauth_enabled") == "1",
        "active": active(),
        "problems": problems(),
        "issuer": issuer(),
        "resource": resource(),
        "dcr_enabled": dcr_enabled(),
        "cimd_hosts": parse_hosts(settings.get("mcp_oauth_cimd_hosts")),
        "rs_secret_set": bool((settings.get("mcp_oauth_rs_secret_hash") or "").strip()),
        "metadata_url": (issuer() + "/.well-known/oauth-authorization-server") if issuer() else "",
        "scopes": list(GRANTABLE_SCOPES),
    }


def metadata() -> dict:
    """RFC 8414 authorization server metadata (with the MCP 2026-07-28 extensions)."""
    iss = issuer()
    doc = {
        "issuer": iss,
        "authorization_endpoint": iss + "/oauth/authorize",
        "token_endpoint": iss + "/oauth/token",
        "response_types_supported": ["code"],
        "response_modes_supported": ["query"],
        "grant_types_supported": ["authorization_code", "refresh_token"],
        "code_challenge_methods_supported": ["S256"],
        "token_endpoint_auth_methods_supported": ["none"],
        "scopes_supported": list(GRANTABLE_SCOPES),
        "authorization_response_iss_parameter_supported": True,
        "client_id_metadata_document_supported": True,
    }
    if dcr_enabled():
        doc["registration_endpoint"] = iss + "/oauth/register"
    return doc


def _digest(raw: str) -> str:
    return hashlib.sha256((raw or "").encode("utf-8")).hexdigest()


def _now() -> datetime:
    return datetime.utcnow()


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def _serializer(salt: str):
    return itsdangerous.URLSafeTimedSerializer(config.SESSION_SECRET, salt=salt)


# --------------------------------------------------------------------------- redirect URIs
def redirect_uri_problem(uri) -> str:
    """ "" when `uri` is an acceptable redirect URI: https, or http on a loopback host (MCP:
    "All redirect URIs MUST be either localhost or use HTTPS"); no fragment or userinfo."""
    u = str(uri if uri is not None else "")
    # Gate: printable ASCII only (an IDN host is sent in its punycode form), so the consent
    # page's CSP form-action helper never meets a host it refuses (that was a 500).
    if not u or len(u) > 512 or any(ord(c) < 0x21 or ord(c) > 0x7E for c in u):
        return "is not a usable URL"
    try:
        p = urlsplit(u)
        _ = p.port
    except ValueError:
        return "is not a usable URL"
    if not p.hostname or p.username or p.password or p.fragment:
        return "must be absolute, with no credentials or fragment"
    if not _NETLOC_RE.fullmatch(p.netloc):
        return "is not a usable URL"
    if p.scheme == "https":
        return ""
    if p.scheme == "http" and is_loopback_host(p.hostname):
        return ""
    return "must use https (http only on localhost)"


def redirect_matches(presented: str, registered: list) -> bool:
    """Exact string match against the registered URIs (MCP: "Authorization servers MUST
    validate exact redirect URIs"), except that a loopback http URI matches on any port (RFC
    8252 7.3: native clients pick an ephemeral port)."""
    if presented in registered:
        return True
    try:
        p = urlsplit(presented)
        if p.scheme != "http" or not is_loopback_host(p.hostname or ""):
            return False
        for r in registered:
            q = urlsplit(r)
            if (
                q.scheme == "http"
                and (q.hostname or "").lower() == (p.hostname or "").lower()
                and q.path == p.path
                and q.query == p.query
            ):
                return True
    except ValueError:
        return False
    return False


# --------------------------------------------------------------------------- clients
_CIMD_CACHE: dict = {}
_CIMD_LOCK = threading.Lock()
_TEST_TRANSPORT = None


def set_test_transport(transport) -> None:
    """Offline seam for the suites: route Client ID Metadata Document fetches here."""
    global _TEST_TRANSPORT
    _TEST_TRANSPORT = transport
    with _CIMD_LOCK:
        _CIMD_CACHE.clear()


def _is_cimd_id(client_id: str) -> bool:
    return client_id.startswith("https://")


def _cache_ttl(headers) -> int:
    cc = (headers.get("cache-control") or "").lower()
    if "no-store" in cc or "no-cache" in cc:
        return 0
    m = re.search(r"max-age=(\d{1,7})", cc)
    if m:
        return min(int(m.group(1)), _CIMD_MAX_TTL)
    return _CIMD_DEFAULT_TTL


def _fetch_cimd(client_id: str) -> dict:
    """Fetch and validate a Client ID Metadata Document (draft-ietf-oauth-client-id-metadata-
    document-00 / MCP 2026-07-28). The URL is attacker-chosen, so: https with a path, no
    userinfo/fragment, trust-list host, SSRF egress guard on every hop (private, loopback,
    link-local, metadata addresses refused), no redirects, 5 s timeout, 64 KiB cap."""
    try:
        p = urlsplit(client_id)
        _ = p.port
    except ValueError:
        raise OAuthError(
            "invalid_client", "The client id is not a usable URL.", redirect=False
        ) from None
    if (
        p.scheme != "https"
        or not p.hostname
        or p.path in ("", "/")
        or p.username
        or p.password
        or p.fragment
        or len(client_id) > 512
    ):
        raise OAuthError(
            "invalid_client",
            "A URL client id must be an https URL with a path (Client ID Metadata Document).",
            redirect=False,
        )
    if not host_trusted(p.hostname, parse_hosts(settings.get("mcp_oauth_cimd_hosts"))):
        raise OAuthError(
            "invalid_client",
            "This install does not accept clients published on " + p.hostname + ".",
            redirect=False,
        )
    now = time.monotonic()
    with _CIMD_LOCK:
        hit = _CIMD_CACHE.get(client_id)
        if hit and hit[0] > now:
            return hit[1]
    try:
        egress.validate_target(client_id, subsystem="mcp_oauth")
        with (
            httpx.Client(
                timeout=_CIMD_TIMEOUT,
                follow_redirects=False,
                **egress.guarded_transport_kwargs("mcp_oauth", transport=_TEST_TRANSPORT),
            ) as client,
            client.stream("GET", client_id, headers={"Accept": "application/json"}) as r,
        ):
            buf = bytearray()
            for chunk in r.iter_bytes():
                buf += chunk
                if time.monotonic() - now > _CIMD_TIMEOUT:
                    # Gate: the httpx timeout is per read; a server dripping bytes would
                    # otherwise hold this unauthenticated request open far past 5 s in total.
                    raise OAuthError(
                        "invalid_client",
                        "The client metadata document took too long to arrive.",
                        redirect=False,
                    )
                if len(buf) > MAX_DOC:
                    raise OAuthError(
                        "invalid_client",
                        "The client metadata document is larger than 64 KiB.",
                        redirect=False,
                    )
            status, headers = r.status_code, r.headers
    except OAuthError:
        raise
    except (egress.EgressBlocked, httpx.HTTPError) as e:
        log.info("mcp_oauth: client metadata fetch failed (%s)", type(e).__name__)
        raise OAuthError(
            "invalid_client", "The client metadata document could not be fetched.", redirect=False
        ) from e
    if status != 200:
        raise OAuthError(
            "invalid_client",
            f"The client metadata document answered HTTP {status}.",
            redirect=False,
        )
    try:
        doc = json.loads(bytes(buf).decode("utf-8"))
    except (ValueError, UnicodeDecodeError) as e:
        raise OAuthError(
            "invalid_client", "The client metadata document is not JSON.", redirect=False
        ) from e
    client = _validate_client_doc(doc, client_id)
    ttl = _cache_ttl(headers)
    if ttl:
        with _CIMD_LOCK:
            if len(_CIMD_CACHE) >= _CIMD_CACHE_MAX:
                _CIMD_CACHE.clear()
            _CIMD_CACHE[client_id] = (now + ttl, client)
    return client


def _validate_client_doc(doc, client_id: str) -> dict:
    def bad(why):
        return OAuthError("invalid_client", "Client metadata " + why + ".", redirect=False)

    if not isinstance(doc, dict):
        raise bad("is not a JSON object")
    if doc.get("client_id") != client_id:
        raise bad("names a different client_id than its own URL")
    name = doc.get("client_name")
    if not (isinstance(name, str) and _NAME_RE.fullmatch(name)):
        raise bad("has no usable client_name")
    uris = doc.get("redirect_uris")
    if not (isinstance(uris, list) and 1 <= len(uris) <= MAX_REDIRECTS):
        raise bad(f"must list 1 to {MAX_REDIRECTS} redirect_uris")
    for u in uris:
        if not isinstance(u, str) or redirect_uri_problem(u):
            raise bad("lists a redirect URI that is not https or localhost")
    method = doc.get("token_endpoint_auth_method", "none")
    if method != "none":
        raise bad("asks for client authentication; only public clients (PKCE) are supported")
    return {
        "client_id": client_id,
        "client_name": name,
        "redirect_uris": list(uris),
        "kind": "cimd",
    }


def register(body) -> dict:
    """RFC 7591 dynamic client registration (deprecated by MCP 2026-07-28; only with
    mcp_oauth_dcr_enabled). Stateless: the returned client id is a signed record of the
    validated metadata, so registration stores nothing and cannot be used to fill the
    database. Raises OAuthError("invalid_redirect_uri" / "invalid_client_metadata")."""
    if not isinstance(body, dict):
        raise OAuthError("invalid_client_metadata", "The registration body must be a JSON object.")
    uris = body.get("redirect_uris")
    if not (isinstance(uris, list) and 1 <= len(uris) <= MAX_REDIRECTS):
        raise OAuthError(
            "invalid_redirect_uri", f"redirect_uris must list 1 to {MAX_REDIRECTS} URIs."
        )
    for u in uris:
        if not isinstance(u, str) or redirect_uri_problem(u):
            raise OAuthError(
                "invalid_redirect_uri", "Every redirect URI must be https (http only on localhost)."
            )
    method = body.get("token_endpoint_auth_method", "none")
    if method != "none":
        raise OAuthError(
            "invalid_client_metadata",
            "Only public clients are supported: token_endpoint_auth_method must be none.",
        )
    grants = body.get("grant_types", ["authorization_code"])
    if not (isinstance(grants, list) and set(grants) <= {"authorization_code", "refresh_token"}):
        raise OAuthError("invalid_client_metadata", "Unsupported grant_types.")
    rtypes = body.get("response_types", ["code"])
    if not (isinstance(rtypes, list) and set(rtypes) <= {"code"}):
        raise OAuthError("invalid_client_metadata", "Unsupported response_types.")
    app_type = body.get("application_type", "web")
    if app_type not in ("web", "native"):
        raise OAuthError("invalid_client_metadata", "application_type must be web or native.")
    name = body.get("client_name") or "Unnamed MCP client"
    if not (isinstance(name, str) and _NAME_RE.fullmatch(name)):
        raise OAuthError("invalid_client_metadata", "client_name must be 1 to 100 characters.")
    issued = int(time.time())
    record = {"n": name, "r": list(uris), "t": app_type, "i": issued}
    client_id = DCR_PREFIX + _serializer("mcp-oauth-dcr-v1").dumps(record)
    if len(client_id) > MAX_CLIENT_ID:
        # Gate: the grant row keeps at most MAX_CLIENT_ID characters of the client id, and a
        # longer one could then never redeem its code. Refuse it here, where the client can act.
        raise OAuthError(
            "invalid_client_metadata",
            "The registration is too large; list fewer or shorter redirect URIs.",
        )
    return {
        "client_id": client_id,
        "client_id_issued_at": issued,
        "client_name": name,
        "redirect_uris": list(uris),
        "grant_types": ["authorization_code", "refresh_token"],
        "response_types": ["code"],
        "token_endpoint_auth_method": "none",
        "application_type": app_type,
    }


def resolve_client(client_id) -> dict:
    """{client_id, client_name, redirect_uris, kind} for a CIMD URL or one of our signed DCR
    client ids. Anything else is an unknown client (never redirected to)."""
    cid = str(client_id or "")
    if _is_cimd_id(cid):
        return _fetch_cimd(cid)
    if cid.startswith(DCR_PREFIX) and dcr_enabled():
        try:
            rec = _serializer("mcp-oauth-dcr-v1").loads(cid[len(DCR_PREFIX) :])
        except itsdangerous.BadData:
            rec = None
        if isinstance(rec, dict) and isinstance(rec.get("r"), list):
            return {
                "client_id": cid,
                "client_name": str(rec.get("n") or "Unnamed MCP client"),
                "redirect_uris": [str(u) for u in rec["r"]],
                "kind": "dcr",
            }
    raise OAuthError("invalid_client", "Unknown client.", redirect=False)


def _resource_ok(value) -> bool:
    return bool(value) and normalize_resource(value) == normalize_resource(resource())


def _scope_set(raw) -> set:
    return {s for s in re.split(r"[\s,]+", str(raw or "").strip()) if s}


# --------------------------------------------------------------------------- authorize
def validate_authorize(params) -> dict:
    """Validate an authorization request (RFC 6749 4.1.1 + PKCE + RFC 8707). Returns the
    request record the consent page signs. Client and redirect URI problems raise with
    redirect=False (shown on our page); the rest redirect back to the client with an error."""
    if not active():
        raise OAuthError("temporarily_unavailable", "Not found.", redirect=False, status=404)
    q = {k: params.get(k) for k in params}
    client = resolve_client(q.get("client_id"))
    redirect_uri = str(q.get("redirect_uri") or "")
    if not redirect_uri or not redirect_matches(redirect_uri, client["redirect_uris"]):
        raise OAuthError(
            "invalid_request",
            "The redirect URI is not registered for this client.",
            redirect=False,
        )
    state = str(q.get("state") or "")
    base = {"redirect_uri": redirect_uri, "state": state[:_STATE_MAX]}
    if len(state) > _STATE_MAX:
        raise OAuthError("invalid_request", "state is too long.", **_redir(base))
    if q.get("response_type") != "code":
        raise OAuthError(
            "unsupported_response_type", "Only response_type=code is supported.", **_redir(base)
        )
    challenge = str(q.get("code_challenge") or "")
    if q.get("code_challenge_method") != "S256" or not _CHALLENGE_RE.fullmatch(challenge):
        raise OAuthError(
            "invalid_request", "PKCE with code_challenge_method=S256 is required.", **_redir(base)
        )
    res = str(q.get("resource") or "")
    if not _resource_ok(res):
        raise OAuthError(
            "invalid_target",
            "The resource parameter must name this install's MCP server.",
            **_redir(base),
        )
    requested = _scope_set(q.get("scope")) or set(GRANTABLE_SCOPES)
    unknown = requested - set(GRANTABLE_SCOPES)
    if unknown:
        raise OAuthError(
            "invalid_scope",
            "Unknown scope(s): " + ", ".join(sorted(unknown)) + ".",
            **_redir(base),
        )
    return {
        "client_id": client["client_id"],
        "client_name": client["client_name"],
        "kind": client["kind"],
        "redirect_uri": redirect_uri,
        "state": state,
        "code_challenge": challenge,
        "resource": resource(),
        "scopes": sorted(requested | {"api"}, key=GRANTABLE_SCOPES.index),
    }


def _redir(base: dict) -> dict:
    return {"redirect": True, "target": base}


def sign_request(req: dict) -> str:
    return _serializer("mcp-oauth-authz-v1").dumps(req)


def load_request(blob) -> dict | None:
    try:
        req = _serializer("mcp-oauth-authz-v1").loads(str(blob or ""), max_age=REQUEST_TTL)
    except itsdangerous.BadData:
        return None
    return req if isinstance(req, dict) else None


def response_url(redirect_uri: str, params: dict) -> str:
    """The client redirect with `iss` always added (RFC 9207, success and error alike)."""
    out = dict(params)
    out["iss"] = issuer()
    if out.get("state") in (None, ""):
        out.pop("state", None)
    return redirect_uri + ("&" if "?" in redirect_uri else "?") + urlencode(out)


def client_error_redirect(req: dict, error: str, description: str = "") -> str:
    params = {"error": error, "state": req.get("state") or ""}
    if description:
        params["error_description"] = description[:200]
    return response_url(req["redirect_uri"], params)


def consent_view(req: dict) -> dict:
    """What the consent page must show (MCP: clearly display the redirect URI hostname; warn on
    localhost-only redirect URIs)."""
    rp = urlsplit(req["redirect_uri"])
    cp = urlsplit(req["client_id"]) if req.get("kind") == "cimd" else None
    return {
        "client_name": req["client_name"],
        "client_host": cp.hostname if cp else "",
        "client_kind": req.get("kind"),
        "redirect_host": rp.hostname or "",
        "redirect_uri": req["redirect_uri"],
        "localhost_redirect": is_loopback_host(rp.hostname or ""),
        "resource": req["resource"],
        "scopes": req["scopes"],
    }


def approve(user, req: dict, chosen_scopes) -> str:
    """The user approved: store the grant with a one-time code (60 s) and return the redirect
    to the client. `chosen_scopes` may only narrow what the client asked for."""
    if not active():
        raise OAuthError("temporarily_unavailable", "Not found.", redirect=False, status=404)
    if not _resource_ok(req.get("resource")):
        return client_error_redirect(req, "invalid_target")
    scopes = (set(chosen_scopes or ()) & set(req["scopes"])) | {"api"}
    code = secrets.token_urlsafe(32)
    with Session(engine) as s:
        live = s.exec(
            select(McpOAuthGrant).where(
                McpOAuthGrant.user_id == user.id,
                McpOAuthGrant.revoked_at == None,  # noqa: E711
            )
        ).all()
        now = _now()
        for g in live:
            # Housekeeping: a consent whose code was never redeemed is dead after its TTL.
            if not g.code_redeemed and g.code_expires_at and g.code_expires_at < now:
                g.revoked_at = now
                s.add(g)
        alive = [g for g in live if g.revoked_at is None]
        if len(alive) >= MAX_GRANTS_PER_USER:
            s.commit()
            return client_error_redirect(
                req, "access_denied", "Too many connected apps; revoke one on your account page."
            )
        s.add(
            McpOAuthGrant(
                user_id=user.id,
                client_id=req["client_id"][:MAX_CLIENT_ID],
                client_name=req["client_name"][:100],
                redirect_uri=req["redirect_uri"],
                scopes=" ".join(sorted(scopes, key=GRANTABLE_SCOPES.index)),
                resource=req["resource"],
                code_hash=_digest(code),
                code_challenge=req["code_challenge"],
                code_expires_at=now + timedelta(seconds=CODE_TTL),
            )
        )
        s.commit()
    return response_url(req["redirect_uri"], {"code": code, "state": req.get("state") or ""})


# --------------------------------------------------------------------------- token endpoint
def _api_token_for(access_token: str) -> str:
    key = hashlib.sha256(("mcp-oauth-api-v1|" + str(config.SESSION_SECRET)).encode()).digest()
    mac = hmac.new(key, access_token.encode("utf-8"), hashlib.sha256).digest()
    return API_PREFIX + _b64url(mac)


def _issue(g: McpOAuthGrant, now: datetime) -> dict:
    access_token = ACCESS_PREFIX + secrets.token_urlsafe(32)
    refresh_token = REFRESH_PREFIX + secrets.token_urlsafe(32)
    g.prev_refresh_hash = g.refresh_hash
    g.access_hash = _digest(access_token)
    g.api_hash = _digest(_api_token_for(access_token))
    g.access_expires_at = now + timedelta(seconds=ACCESS_TTL)
    g.refresh_hash = _digest(refresh_token)
    g.refresh_expires_at = now + timedelta(seconds=REFRESH_TTL)
    g.last_used = now
    return {
        "access_token": access_token,
        "token_type": "Bearer",
        "expires_in": ACCESS_TTL,
        "refresh_token": refresh_token,
        "scope": g.scopes,
    }


def _user_ok(s: Session, g: McpOAuthGrant):
    user = s.get(User, g.user_id)
    if user is None or not user.active or not access.user_org_active(user):
        return None
    return user


def _pkce_ok(verifier: str, challenge: str) -> bool:
    if not _VERIFIER_RE.fullmatch(verifier or ""):
        return False
    calc = _b64url(hashlib.sha256(verifier.encode("ascii")).digest())
    return hmac.compare_digest(calc, challenge or "")


def _rs_authenticated(form: dict, authorization: str) -> bool:
    """The MCP server (resource server) authenticates with HTTP Basic (or form) as
    RS_CLIENT_ID and the secret whose SHA-256 is mcp_oauth_rs_secret_hash."""
    from app.services import security

    expected = (settings.get("mcp_oauth_rs_secret_hash") or "").strip()
    if not expected:
        return False
    cid, secret = form.get("client_id") or "", form.get("client_secret") or ""
    if authorization[:6].lower() == "basic ":
        try:
            raw = base64.b64decode(authorization[6:].strip(), validate=True).decode("utf-8")
        except (ValueError, UnicodeDecodeError):
            return False
        from urllib.parse import unquote_plus

        a, _, b = raw.partition(":")
        cid, secret = unquote_plus(a), unquote_plus(b)
    return cid == RS_CLIENT_ID and security.constant_time_eq(_digest(secret), expected)


def token(form: dict, authorization: str = "") -> tuple[int, dict]:
    """The token endpoint. Returns (HTTP status, JSON body). Error bodies carry only the RFC
    6749 error code and a fixed description, never a token."""
    if not active():
        return 404, {"error": "not_found"}
    gt = form.get("grant_type")
    try:
        if gt == "authorization_code":
            return 200, _grant_code(form)
        if gt == "refresh_token":
            return 200, _grant_refresh(form)
        if gt == TOKEN_EXCHANGE:
            if not _rs_authenticated(form, authorization):
                return 401, {"error": "invalid_client", "error_description": "Unknown client."}
            return 200, _grant_exchange(form)
    except OAuthError as e:
        return e.status, {"error": e.error, "error_description": e.description}
    return 400, {"error": "unsupported_grant_type"}


def _bad_grant(msg="The grant is invalid, expired or revoked."):
    return OAuthError("invalid_grant", msg)


def _check_resource_param(form: dict) -> None:
    res = form.get("resource")
    if res not in (None, "") and not _resource_ok(res):
        raise OAuthError("invalid_target", "The resource is not this install's MCP server.")


def _grant_code(form: dict) -> dict:
    code = form.get("code") or ""
    _check_resource_param(form)
    now = _now()
    with Session(engine) as s:
        g = s.exec(select(McpOAuthGrant).where(McpOAuthGrant.code_hash == _digest(code))).first()
        if g is None or not code:
            raise _bad_grant()
        if g.code_redeemed:
            # OAuth 2.1 4.1.3: a reused code revokes what it issued.
            if g.revoked_at is None:
                g.revoked_at = now
                s.add(g)
                s.commit()
                log.warning("mcp_oauth: authorization code reuse, grant %s revoked", g.id)
            raise _bad_grant()
        if g.revoked_at is not None or not g.code_expires_at or g.code_expires_at < now:
            raise _bad_grant()
        if (
            form.get("client_id") != g.client_id
            or form.get("redirect_uri") != g.redirect_uri
            or not _pkce_ok(form.get("code_verifier") or "", g.code_challenge)
            or not _resource_ok(g.resource)
        ):
            raise _bad_grant()
        if _user_ok(s, g) is None:
            raise _bad_grant()
        # Atomic one-time claim: of two racing redemptions exactly one flips the flag.
        if not _claim(
            s,
            g.id,
            McpOAuthGrant.code_redeemed == False,  # noqa: E712
            code_redeemed=True,
        ):
            raise _bad_grant()
        g.code_redeemed = True
        out = _issue(g, now)
        s.add(g)
        s.commit()
    return out


def _grant_refresh(form: dict) -> dict:
    raw = form.get("refresh_token") or ""
    _check_resource_param(form)
    now = _now()
    digest = _digest(raw)
    with Session(engine) as s:
        g = s.exec(select(McpOAuthGrant).where(McpOAuthGrant.refresh_hash == digest)).first()
        if g is None and raw:
            reused = s.exec(
                select(McpOAuthGrant).where(McpOAuthGrant.prev_refresh_hash == digest)
            ).first()
            if reused is not None and reused.revoked_at is None:
                reused.revoked_at = now
                s.add(reused)
                s.commit()
                log.warning("mcp_oauth: refresh token reuse, grant %s revoked", reused.id)
            raise _bad_grant()
        if g is None or not raw or g.revoked_at is not None:
            raise _bad_grant()
        if (
            not g.refresh_expires_at
            or g.refresh_expires_at < now
            or form.get("client_id") != g.client_id
            or not _resource_ok(g.resource)
            or _user_ok(s, g) is None
        ):
            raise _bad_grant()
        # Atomic rotation claim: of two racing refreshes with one token exactly one wins.
        if not _claim(
            s,
            g.id,
            McpOAuthGrant.refresh_hash == digest,
            refresh_hash=None,
            prev_refresh_hash=digest,
        ):
            raise _bad_grant()
        narrowed = _scope_set(form.get("scope"))
        if narrowed:
            if not narrowed <= _scope_set(g.scopes):
                raise OAuthError("invalid_scope", "A refresh cannot widen the granted scopes.")
            g.scopes = " ".join(sorted(narrowed | {"api"}, key=GRANTABLE_SCOPES.index))
        out = _issue(g, now)
        s.add(g)
        s.commit()
    return out


def _claim(s: Session, grant_id: int, condition, **values) -> bool:
    """Compare-and-set on one grant row: the UPDATE both tests `condition` and changes the
    column it tests, so of two racing transactions exactly one matches a row."""
    from sqlalchemy import update

    res = s.execute(
        update(McpOAuthGrant)
        .where(McpOAuthGrant.id == grant_id, condition)
        .values(**values)
        .execution_options(synchronize_session=False)
    )
    return res.rowcount == 1


def _live_grant_for_access(s: Session, access_token: str):
    if not access_token.startswith(ACCESS_PREFIX):
        return None, None
    g = s.exec(
        select(McpOAuthGrant).where(McpOAuthGrant.access_hash == _digest(access_token))
    ).first()
    if (
        g is None
        or g.revoked_at is not None
        or not g.access_expires_at
        or g.access_expires_at <= _now()
        or not _resource_ok(g.resource)
    ):
        return None, None
    user = _user_ok(s, g)
    return (g, user) if user is not None else (None, None)


def _grant_exchange(form: dict) -> dict:
    """RFC 8693, for the MCP server only: an MCP-audience lsm_ token in, the /api lso_ token
    out. Validates the audience (the grant's resource must still be the configured one)."""
    if form.get("subject_token_type") != TT_ACCESS:
        raise OAuthError("invalid_request", "subject_token_type must be an access token.")
    subject = form.get("subject_token") or ""
    with Session(engine) as s:
        g, _user = _live_grant_for_access(s, subject)
        if g is None:
            raise _bad_grant("The subject token is invalid, expired or revoked.")
        remaining = int((g.access_expires_at - _now()).total_seconds())
        scopes = g.scopes
    return {
        "access_token": _api_token_for(subject),
        "issued_token_type": TT_ACCESS,
        "token_type": "Bearer",
        "expires_in": max(1, remaining),
        "scope": scopes,
    }


def api_identity(raw: str) -> dict | None:
    """The /api identity an lso_ token resolves to, or None (-> 401). Shaped like the per-user
    key identity (deps._resolve_api_identity) plus `auth_kind: "mcp_oauth"`, so every scope,
    role and org guard applies unchanged. None whenever the server is off."""
    if not raw.startswith(API_PREFIX) or not active():
        return None
    with Session(engine) as s:
        g = s.exec(select(McpOAuthGrant).where(McpOAuthGrant.api_hash == _digest(raw))).first()
        if (
            g is None
            or g.revoked_at is not None
            or not g.access_expires_at
            or g.access_expires_at <= _now()
            or not _resource_ok(g.resource)
        ):
            return None
        user = _user_ok(s, g)
        if user is None:
            return None
        s.expunge(user)
        scopes = g.scopes or "api"
        grant_id = g.id
    return {
        "kind": "user",
        "actor": user.email,
        "user": user,
        "scopes": scopes,
        "org_id": access.current_org_id(user),
        "auth_kind": "mcp_oauth",
        "mcp_grant_id": grant_id,
    }


# --------------------------------------------------------------------------- grants
def grant_view(g: McpOAuthGrant) -> dict:
    """Metadata only, never a digest."""

    def iso(d):
        return d.strftime("%Y-%m-%d %H:%M UTC") if d else None

    return {
        "id": g.id,
        "user_id": g.user_id,
        "client_name": g.client_name,
        "client_id": g.client_id if not g.client_id.startswith(DCR_PREFIX) else "(registered)",
        "redirect_host": urlsplit(g.redirect_uri).hostname or "",
        "scopes": g.scopes,
        "resource": g.resource,
        "created_at": iso(g.created_at),
        "last_used": iso(g.last_used),
        "revoked": g.revoked_at is not None,
    }


def list_grants(user_id: int | None = None, *, include_revoked: bool = False) -> list:
    """A user's grants that reached a token (None = every user, the host operator's view)."""
    with Session(engine) as s:
        q = select(McpOAuthGrant).where(McpOAuthGrant.code_redeemed == True)  # noqa: E712
        if user_id is not None:
            q = q.where(McpOAuthGrant.user_id == user_id)
        if not include_revoked:
            q = q.where(McpOAuthGrant.revoked_at == None)  # noqa: E711
        return [grant_view(g) for g in s.exec(q.order_by(McpOAuthGrant.id)).all()]


def revoke_grant(grant_id, user_id: int | None = None) -> bool:
    """Revoke one grant. With user_id (self-service) it must be the caller's own; a foreign or
    missing id answers False either way."""
    try:
        gid = int(grant_id)
    except (TypeError, ValueError):
        return False
    with Session(engine) as s:
        g = s.get(McpOAuthGrant, gid)
        if g is None or (user_id is not None and g.user_id != user_id):
            return False
        if g.revoked_at is None:
            g.revoked_at = _now()
            s.add(g)
            s.commit()
        return True


def revoke_all_for_user(user_id: int, session: Session | None = None) -> int:
    """Revoke every live grant of a user (account disable / erasure / revoke-all). The caller's
    session, when given, owns the commit (apikeys.revoke_all_for_user's atomic contract)."""
    from contextlib import nullcontext

    with Session(engine) if session is None else nullcontext(session) as s:
        rows = s.exec(
            select(McpOAuthGrant).where(
                McpOAuthGrant.user_id == user_id,
                McpOAuthGrant.revoked_at == None,  # noqa: E711
            )
        ).all()
        now = _now()
        for g in rows:
            g.revoked_at = now
            s.add(g)
        if session is None:
            s.commit()
        return len(rows)


def generate_rs_secret(actor: str = "") -> str:
    """Mint a new resource-server secret: store only its SHA-256, return the secret ONCE (it
    goes into the MCP server's LOC_STUDIO_MCP_RS_SECRET). Replaces any previous one."""
    secret = "lsrs_" + secrets.token_urlsafe(32)
    settings.set_many({"mcp_oauth_rs_secret_hash": _digest(secret)}, actor=actor)
    return secret
