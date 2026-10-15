"""MCP authorization (OAuth 2.1) for the `mcp_cms` connector.

Implements the client side of the MCP authorization spec (2025-06-18 / 2025-11-25 / 2026-07-28):

  1. PROTECTED RESOURCE METADATA (RFC 9728). An unauthenticated probe of the MCP URL answers 401
     with `WWW-Authenticate: Bearer resource_metadata="..."`; failing that, the well-known paths
     `/.well-known/oauth-protected-resource[/path]` are tried. The document names the
     authorization server(s).
  2. AUTHORIZATION SERVER METADATA (RFC 8414, then OpenID discovery). The issuer in the document
     must be IDENTICAL to the issuer we asked for (simple string comparison, no trailing-slash
     normalization; mix-up defence) and S256 PKCE must be advertised.
  3. CLIENT: an operator-supplied client id (+ optional secret); else our CLIENT ID METADATA
     DOCUMENT URL as the client id when the server advertises
     `client_id_metadata_document_supported` (the document is served by the app at
     /connectors/oauth/client-metadata.json); else DYNAMIC CLIENT REGISTRATION (RFC 7591,
     deprecated by MCP 2026-07-28) as a public client with an `application_type`.
  4. AUTHORIZE (PKCE S256 + the RFC 8707 `resource` parameter) -> browser consent -> the EXISTING
     connector callback route -> code exchange. The token, refresh token and the client facts a
     refresh needs are stored in the connection's OAuth secret (encrypted like every connector
     secret).
  5. REFRESH goes through the existing single-flight path (store_part2.ensure_fresh_token ->
     oauth.refresh); this module only supplies the protocol request. It is NOT a second refresh
     path (.claude/rules/connectors.md).

SSRF. Every URL that comes out of a metadata document (resource_metadata, authorization server
issuer, authorize/token/registration endpoints) is attacker-influenced, because the MCP server
chooses them. `check_url` runs on each one BEFORE it is used or stored: absolute http(s) only, no
credentials in the URL, no fragment, no scheme downgrade relative to the configured MCP URL
(https never falls to http), and the connector egress validator (private, loopback, link-local and
metadata addresses refused unless on `egress_private_allowlist`; loopback is never allowed). All
requests also run through `egress.GuardedTransport`, which re-checks the resolved address on every
hop. Redirects are never followed. Metadata bodies are capped at 256 KiB.

This module is pure protocol: no database writes, no secrets in logs or messages.

Documented in: docs/architecture/connectors.md
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import re
import secrets
from urllib.parse import quote_plus, urlencode, urlsplit, urlunsplit

import httpx

from app.services import egress
from app.services.connectors.base import ConnectorError, _validate_target

log = logging.getLogger("loc_tms.connectors")

KIND = "mcp_cms"
MAX_DOC = 256 * 1024
_MAX_URL = 2048
_MAX_AS = 5
_MAX_SCOPE = 1000
_TIMEOUT = 15.0
_CLIENT_ID_RE = re.compile(r"[\x21-\x7e]{1,256}")
_WWW_AUTH_RE = re.compile(r'([A-Za-z_]+)="([^"]*)"')


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


# --------------------------------------------------------------------------- SSRF
def _scheme_of(url: str) -> str:
    return (urlsplit(str(url or "")).scheme or "").lower()


def check_url(url, *, base_scheme: str, what: str) -> str:
    """Validate ONE discovered URL and return it (the validator's canonical form).

    Refuses: non-strings, control characters, over-long values, non-http(s) schemes, a missing
    host, embedded credentials, fragments, any scheme DOWNGRADE (an http URL while the MCP URL is
    https), and every address the connector egress validator refuses."""
    u = url.strip() if isinstance(url, str) else ""
    if not u or len(u) > _MAX_URL or any(ord(ch) < 0x21 or ord(ch) == 0x7F for ch in u):
        raise ConnectorError(f"The server's {what} is not a usable URL.")
    try:
        p = urlsplit(u)
        _ = p.port  # malformed port raises
    except ValueError as e:
        raise ConnectorError(f"The server's {what} is not a usable URL.") from e
    if p.scheme.lower() not in ("https", "http") or not p.hostname:
        raise ConnectorError(f"The server's {what} must be an absolute http(s) URL.")
    if p.username or p.password or p.fragment:
        raise ConnectorError(f"The server's {what} may not carry credentials or a fragment.")
    if p.scheme.lower() == "http" and base_scheme != "http":
        raise ConnectorError(f"The server's {what} would downgrade from https to http; refused.")
    return _validate_target(u)


def _well_known(origin_url: str, suffix: str, path: str = "") -> str:
    p = urlsplit(origin_url)
    path = path.strip("/")
    wk = f"/.well-known/{suffix}" + (f"/{path}" if path else "")
    return urlunsplit((p.scheme, p.netloc, wk, "", ""))


def _norm_resource(url: str) -> tuple:
    p = urlsplit(str(url or "").strip())
    return (p.scheme.lower(), (p.hostname or "").lower(), p.port, p.path.rstrip("/"))


def _resource_matches(prm_resource: str, mcp_url: str) -> bool:
    """The metadata's `resource` must identify THIS server: the same URL, or an origin / path
    prefix of it on a segment boundary (servers commonly publish the origin)."""
    a, b = _norm_resource(prm_resource), _norm_resource(mcp_url)
    if a[:3] != b[:3]:
        return False
    return a[3] == b[3] or b[3].startswith(a[3] + "/") or a[3] == ""


# --------------------------------------------------------------------------- HTTP
def _client(transport=None) -> httpx.Client:
    from app.services.connectors import oauth

    return httpx.Client(
        timeout=_TIMEOUT,
        follow_redirects=False,
        **egress.guarded_transport_kwargs(
            "connectors",
            transport=transport or oauth._default_transport,
            error_type=ConnectorError,
        ),
    )


def _send(client: httpx.Client, method: str, url: str, **kw):
    """One bounded request. Returns (status, headers, body bytes). Never follows redirects."""
    try:
        with client.stream(method, url, **kw) as r:
            buf = bytearray()
            for chunk in r.iter_bytes():
                buf += chunk
                if len(buf) > MAX_DOC:
                    raise ConnectorError("An authorization document was larger than 256 KiB.")
            return r.status_code, r.headers, bytes(buf)
    except ConnectorError:
        raise
    except httpx.HTTPError as e:
        raise ConnectorError(
            f"Could not reach the authorization server ({type(e).__name__})."
        ) from e


def _json_doc(status: int, body: bytes, what: str) -> dict:
    if status != 200:
        raise ConnectorError(f"The {what} answered HTTP {status}.")
    try:
        doc = json.loads(body.decode("utf-8"))
    except (ValueError, UnicodeDecodeError) as e:
        raise ConnectorError(f"The {what} is not valid JSON.") from e
    if not isinstance(doc, dict):
        raise ConnectorError(f"The {what} is not a JSON object.")
    return doc


# --------------------------------------------------------------------------- discovery
def _probe_challenge(client, mcp_url: str) -> tuple[dict, int]:
    """POST an unauthenticated initialize and read the 401 challenge.
    Returns (challenge params, HTTP status); the params are {} when there is no challenge."""
    payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": "2025-06-18",
            "capabilities": {},
            "clientInfo": {"name": "probe", "version": "1"},
        },
    }
    status, headers, _body = _send(
        client,
        "POST",
        mcp_url,
        content=json.dumps(payload).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        },
    )
    if status != 401:
        return {}, status
    out = {}
    for k, v in _WWW_AUTH_RE.findall(headers.get("www-authenticate") or ""):
        out[k.lower()] = v
    return out, status


def fetch_resource_metadata(client, mcp_url: str, base_scheme: str) -> tuple[dict, str]:
    """(protected resource metadata, scope hint from the challenge)."""
    challenge, probe_status = _probe_challenge(client, mcp_url)
    candidates = []
    if challenge.get("resource_metadata"):
        candidates.append(
            check_url(
                challenge["resource_metadata"],
                base_scheme=base_scheme,
                what="resource metadata URL",
            )
        )
    sp = urlsplit(mcp_url)
    for cand in (
        _well_known(mcp_url, "oauth-protected-resource", sp.path),
        _well_known(mcp_url, "oauth-protected-resource"),
    ):
        if cand not in candidates:
            candidates.append(
                check_url(cand, base_scheme=base_scheme, what="resource metadata URL")
            )
    last = "no metadata document found"
    for cand in candidates:
        status, _h, body = _send(client, "GET", cand, headers={"Accept": "application/json"})
        try:
            doc = _json_doc(status, body, "protected resource metadata")
        except ConnectorError as e:
            last = str(e)
            continue
        res = doc.get("resource")
        if not (isinstance(res, str) and res.strip()):
            raise ConnectorError(
                "The protected resource metadata names no resource (RFC 9728 requires one)."
            )
        if not _resource_matches(res, mcp_url):
            raise ConnectorError(
                "The protected resource metadata names a different resource than this MCP URL."
            )
        return doc, str(challenge.get("scope") or "")
    if probe_status == 200:
        # An open initialize is not proof that tools/call is open too (a server may guard only
        # some methods), so the well-known metadata was still tried above. With neither a
        # challenge nor metadata there is nothing to authorize.
        raise ConnectorError(
            "This MCP server accepted a request without authorization and publishes no OAuth "
            "metadata, so there is nothing to authorize. Leave OAuth off (a bearer token is "
            "optional for it)."
        )
    raise ConnectorError(
        f"This MCP server does not publish OAuth protected resource metadata ({last})."
    )


def _as_candidates(issuer: str) -> list[str]:
    p = urlsplit(issuer)
    path = p.path.strip("/")
    if not path:
        return [
            _well_known(issuer, "oauth-authorization-server"),
            _well_known(issuer, "openid-configuration"),
        ]
    return [
        _well_known(issuer, "oauth-authorization-server", path),
        _well_known(issuer, "openid-configuration", path),
        issuer.rstrip("/") + "/.well-known/openid-configuration",
    ]


def fetch_as_metadata(client, issuer: str, base_scheme: str) -> dict:
    last = "no metadata document found"
    for cand in _as_candidates(issuer):
        cand = check_url(cand, base_scheme=base_scheme, what="authorization server metadata URL")
        status, _h, body = _send(client, "GET", cand, headers={"Accept": "application/json"})
        try:
            doc = _json_doc(status, body, "authorization server metadata")
        except ConnectorError as e:
            last = str(e)
            continue
        # RFC 8414 3.3 / MCP 2026-07-28: the document's issuer MUST be IDENTICAL to the issuer
        # the well-known URL was built from. Simple string comparison, no trailing-slash or
        # case normalization (#758): `https://as.example/` is not `https://as.example`.
        if not isinstance(doc.get("issuer"), str) or doc["issuer"] != issuer:
            raise ConnectorError(
                "The authorization server metadata names a different issuer than the one it was "
                "fetched for; refused."
            )
        # RFC 9207: the exact issuer string the server publishes, and whether it promises to
        # return it as `iss` on the authorization response (checked on the callback).
        out = {
            "issuer": issuer,
            "iss_value": doc["issuer"],
            "iss_required": doc.get("authorization_response_iss_parameter_supported") is True,
            # Client ID Metadata Documents (MCP 2026-07-28 client registration, priority 2).
            "cimd": doc.get("client_id_metadata_document_supported") is True,
        }
        for key, what in (
            ("authorization_endpoint", "authorization endpoint"),
            ("token_endpoint", "token endpoint"),
        ):
            out[key] = check_url(doc.get(key), base_scheme=base_scheme, what=what)
        if doc.get("registration_endpoint"):
            out["registration_endpoint"] = check_url(
                doc["registration_endpoint"], base_scheme=base_scheme, what="registration endpoint"
            )
        methods = doc.get("code_challenge_methods_supported")
        if not (isinstance(methods, list) and "S256" in methods):
            raise ConnectorError(
                "The authorization server does not advertise PKCE S256, which MCP requires."
            )
        grants = doc.get("grant_types_supported")
        if isinstance(grants, list) and "authorization_code" not in grants:
            raise ConnectorError(
                "The authorization server does not offer the authorization code grant."
            )
        am = doc.get("token_endpoint_auth_methods_supported")
        out["auth_methods"] = (
            [str(x) for x in am if isinstance(x, str)][:8] if isinstance(am, list) else []
        )
        sc = doc.get("scopes_supported")
        out["scopes_supported"] = (
            [str(x) for x in sc if isinstance(x, str)][:50] if isinstance(sc, list) else []
        )
        return out
    raise ConnectorError(f"The authorization server publishes no usable metadata ({last}).")


def discover(mcp_url: str, transport=None) -> dict:
    """Run steps 1 and 2. Returns {resource, issuer, authorization_endpoint, token_endpoint,
    registration_endpoint?, auth_methods, scopes_supported, scope_hint}."""
    raw = str(mcp_url or "").strip()
    base_scheme = _scheme_of(raw)
    mcp_url = check_url(raw, base_scheme=base_scheme, what="MCP server URL")
    with _client(transport) as client:
        prm, hint = fetch_resource_metadata(client, mcp_url, base_scheme)
        servers = prm.get("authorization_servers")
        if not (isinstance(servers, list) and servers):
            raise ConnectorError("The protected resource metadata lists no authorization server.")
        last = None
        meta = None
        for issuer in [s for s in servers[:_MAX_AS] if isinstance(s, str)]:
            try:
                issuer = check_url(issuer, base_scheme=base_scheme, what="authorization server URL")
                meta = fetch_as_metadata(client, issuer, base_scheme)
                break
            except ConnectorError as e:
                last = e
        if meta is None:
            raise last or ConnectorError("No usable authorization server.")
    res = prm.get("resource")
    meta["resource"] = res.strip() if isinstance(res, str) and res.strip() else mcp_url
    meta["scope_hint"] = hint
    # Least privilege: only the RESOURCE's own advertised scopes are requested by default. The
    # authorization server's scopes_supported covers every resource it fronts (admin scopes
    # included), so it is never used as a fallback; with neither hint the scope is omitted.
    psc = prm.get("scopes_supported")
    meta["scopes_supported"] = (
        [str(x) for x in psc if isinstance(x, str)][:50] if isinstance(psc, list) else []
    )
    return meta


# --------------------------------------------------------------------------- client
def register_client(meta: dict, redirect_uri: str, client_name: str, transport=None) -> dict:
    """Dynamic client registration (RFC 7591) as a PUBLIC client. {client_id}."""
    ep = meta.get("registration_endpoint")
    if not ep:
        raise ConnectorError(
            "This server has no dynamic client registration. Enter an OAuth client id (and "
            "secret) from the vendor's developer console, with this callback URL registered: "
            + redirect_uri
        )
    body = {
        "client_name": client_name[:100],
        "redirect_uris": [redirect_uri],
        "grant_types": ["authorization_code", "refresh_token"],
        "response_types": ["code"],
        "token_endpoint_auth_method": "none",
        # MCP 2026-07-28: clients MUST send an appropriate application_type on DCR (#758).
        "application_type": application_type(redirect_uri),
    }
    with _client(transport) as client:
        status, _h, raw = _send(
            client,
            "POST",
            ep,
            content=json.dumps(body).encode("utf-8"),
            headers={"Content-Type": "application/json", "Accept": "application/json"},
        )
    if status not in (200, 201):
        # Never echo the body (it may name the redirect URI rules or a credential); the HTTP
        # status and the application_type we sent are what an operator needs to act.
        raise ConnectorError(
            f"Dynamic client registration was refused (HTTP {status}; application_type "
            f"{body['application_type']}). Enter an OAuth client id from the vendor's "
            "developer console instead, with this callback URL registered: " + redirect_uri
        )
    doc = _json_doc(200, raw, "client registration response")
    cid = doc.get("client_id")
    if not (isinstance(cid, str) and _CLIENT_ID_RE.fullmatch(cid)):
        raise ConnectorError("Dynamic client registration returned no usable client id.")
    out = {"client_id": cid}
    # A secret in the response is deliberately dropped: we registered a PUBLIC client (PKCE),
    # and a secret must never travel in the signed-but-readable session cookie.
    return out


_LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})


def application_type(redirect_uri: str) -> str:
    """OIDC Dynamic Client Registration `application_type` for our redirect URI (MCP
    2026-07-28 client registration): "native" when the callback is a loopback address (a
    locally hosted web app reached via localhost), else "web" (a remote browser-based app)."""
    host = (urlsplit(str(redirect_uri or "")).hostname or "").lower()
    return "native" if host in _LOOPBACK_HOSTS or host.endswith(".localhost") else "web"


def cimd_usable(client_metadata_url: str, redirect_uri: str) -> bool:
    """True when our Client ID Metadata Document URL can be used as a client_id: an https URL
    with a path (MCP 2026-07-28 client requirements), on a public host the authorization server
    can fetch, and the redirect URI it lists is ours and https too. Anything else falls back to
    dynamic registration."""
    u = str(client_metadata_url or "").strip()
    if not u or len(u) > _MAX_URL:
        return False
    try:
        p = urlsplit(u)
        r = urlsplit(str(redirect_uri or ""))
    except ValueError:
        return False
    host = (p.hostname or "").lower()
    return (
        p.scheme == "https"
        and bool(host)
        and host not in _LOOPBACK_HOSTS
        and not host.endswith(".localhost")
        and p.path not in ("", "/")
        and not (p.username or p.password or p.query or p.fragment)
        and r.scheme == "https"
        and (r.hostname or "").lower() == host
    )


def client_metadata_document(client_metadata_url: str, redirect_uri: str, client_name: str):
    """The Client ID Metadata Document we publish at `client_metadata_url` (draft-ietf-oauth-
    client-id-metadata-document-00, MCP 2026-07-28). `client_id` equals the document URL
    EXACTLY; a public client (PKCE, no secret)."""
    return {
        "client_id": client_metadata_url,
        "client_name": str(client_name or "")[:100],
        "redirect_uris": [redirect_uri],
        "grant_types": ["authorization_code", "refresh_token"],
        "response_types": ["code"],
        "token_endpoint_auth_method": "none",
        "application_type": application_type(redirect_uri),
    }


def _auth_method(meta: dict, has_secret: bool) -> str:
    """RFC 8414: an omitted token_endpoint_auth_methods_supported means client_secret_basic.
    client_secret_post is used only when the server lists it and does not list basic."""
    if not has_secret:
        return "none"
    methods = meta.get("auth_methods") or []
    if "client_secret_post" in methods and "client_secret_basic" not in methods:
        return "client_secret_post"
    return "client_secret_basic"


def begin(
    *,
    mcp_url: str,
    redirect_uri: str,
    state: str,
    client_name: str,
    client_id: str = "",
    scope: str = "",
    transport=None,
    client_metadata_url: str = "",
) -> tuple[str, dict, str]:
    """Steps 1 to 4 up to the browser redirect.

    Client registration follows the MCP 2026-07-28 priority order: (1) an operator-supplied
    (pre-registered) client id; (2) our Client ID Metadata Document URL as the client id, when
    the authorization server advertises `client_id_metadata_document_supported` and the URL is
    usable (`cimd_usable`); (3) dynamic client registration (deprecated, kept for older
    servers); (4) an actionable message asking for a client id.

    Returns (authorize_url, session_facts, code_verifier). `session_facts` is what the callback
    needs (all non-secret and re-validated there): token endpoint, resource, client id, auth
    method, issuer, scope."""
    meta = discover(mcp_url, transport)
    client_id = (client_id or "").strip()
    if client_id:
        if not _CLIENT_ID_RE.fullmatch(client_id):
            raise ConnectorError("The OAuth client id has characters it cannot carry.")
    elif meta.get("cimd") and cimd_usable(client_metadata_url, redirect_uri):
        client_id = client_metadata_url.strip()
    else:
        reg = register_client(meta, redirect_uri, client_name, transport)
        client_id = reg["client_id"]
    scope = (
        (scope or "").strip()
        or meta.get("scope_hint")
        or " ".join(meta.get("scopes_supported") or [])
    )
    scope = scope[:_MAX_SCOPE]
    verifier = _b64url(secrets.token_bytes(48))
    params = {
        "response_type": "code",
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "state": state,
        "code_challenge": _b64url(hashlib.sha256(verifier.encode("ascii")).digest()),
        "code_challenge_method": "S256",
        "resource": meta["resource"],
    }
    if scope:
        params["scope"] = scope
    ep = meta["authorization_endpoint"]
    url = ep + ("&" if "?" in ep else "?") + urlencode(params)
    facts = {
        "token_endpoint": meta["token_endpoint"],
        "resource": meta["resource"],
        "issuer": meta["issuer"],
        "client_id": client_id,
        "auth_methods": meta.get("auth_methods") or [],
        "scope": scope,
        "iss_value": meta.get("iss_value") or meta["issuer"],
        "iss_required": bool(meta.get("iss_required")),
    }
    return url, facts, verifier


def check_iss(facts: dict, iss) -> None:
    """RFC 9207 authorization server mix-up defence on the callback. When the response carries
    `iss` it must name the issuer this flow was started against; when the server advertised
    `authorization_response_iss_parameter_supported` it must be present. Raises
    ConnectorError BEFORE any code exchange."""
    expected = str((facts or {}).get("iss_value") or (facts or {}).get("issuer") or "")
    if iss is None or iss == "":
        if (facts or {}).get("iss_required"):
            raise ConnectorError(
                "The authorization response did not name its issuer (iss), which this "
                "authorization server promised to send; refused."
            )
        return
    # Simple string comparison (RFC 3986 6.2.1). MCP 2026-07-28: clients MUST NOT apply case
    # folding, default-port elision, trailing-slash or percent-encoding normalization (#758).
    if not expected or not isinstance(iss, str) or iss != expected:
        raise ConnectorError(
            "The authorization response came from a different issuer than the one this "
            "connection started with; refused."
        )


# --------------------------------------------------------------------------- token calls
def _token_request(facts: dict, data: dict, client_secret: str, transport=None) -> dict:
    """POST to the (re-validated) token endpoint. Returns the parsed token response."""
    resource = str(facts.get("resource") or "")
    base_scheme = _scheme_of(resource)
    endpoint = check_url(
        facts.get("token_endpoint"), base_scheme=base_scheme, what="token endpoint"
    )
    method = _auth_method({"auth_methods": facts.get("auth_methods") or []}, bool(client_secret))
    form = dict(data)
    form["client_id"] = facts["client_id"]
    form["resource"] = resource
    headers = {"Accept": "application/json", "Content-Type": "application/x-www-form-urlencoded"}
    if client_secret:
        if method == "client_secret_basic":
            # RFC 6749 2.3.1: id and secret are form-urlencoded before the Basic encoding.
            pair = f"{quote_plus(facts['client_id'])}:{quote_plus(client_secret)}"
            raw = base64.b64encode(pair.encode()).decode("ascii")
            headers["Authorization"] = f"Basic {raw}"
        else:
            form["client_secret"] = client_secret
    with _client(transport) as client:
        status, _h, body = _send(
            client, "POST", endpoint, content=urlencode(form).encode(), headers=headers
        )
    if status != 200:
        # Never echo the body: it may carry an error_description naming a credential.
        code = ""
        try:
            doc = json.loads(body.decode("utf-8"))
            e = doc.get("error") if isinstance(doc, dict) else ""
            code = e if isinstance(e, str) and re.fullmatch(r"[a-z_]{1,40}", e) else ""
        except (ValueError, UnicodeDecodeError):
            pass
        raise ConnectorError(
            f"The token endpoint refused the request (HTTP {status}{', ' + code if code else ''})."
        )
    doc = _json_doc(200, body, "token response")
    tt = str(doc.get("token_type") or "Bearer")
    if tt.lower() != "bearer":
        raise ConnectorError("The token endpoint issued a token type other than Bearer; refused.")
    tok = doc.get("access_token")
    if not (isinstance(tok, str) and tok and all(0x21 <= ord(c) <= 0x7E for c in tok)):
        raise ConnectorError("The token endpoint returned no usable access token.")
    return doc


def exchange_code(
    facts: dict, code: str, redirect_uri: str, verifier: str, client_secret: str, transport=None
) -> dict:
    data = {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": redirect_uri,
        "code_verifier": verifier,
    }
    return _token_request(facts, data, client_secret, transport)


def stored_facts(facts: dict, client_secret: str) -> dict:
    """The `mcp` sub-dict kept inside the encrypted OAuth secret: everything a refresh needs."""
    out = {
        "token_endpoint": facts["token_endpoint"],
        "resource": facts["resource"],
        "issuer": facts.get("issuer", ""),
        "client_id": facts["client_id"],
        "auth_methods": list(facts.get("auth_methods") or []),
    }
    if client_secret:
        out["client_secret"] = client_secret
    return out


def refresh(oauth_dict: dict, transport=None):
    """Protocol half of the single-flight refresh (oauth.refresh dispatches here for mcp_cms).
    Returns (token response dict, None) or (None, message). No persistence, no second path."""
    mcp = (oauth_dict or {}).get("mcp")
    rt = (oauth_dict or {}).get("refresh_token") or ""
    if not isinstance(mcp, dict) or not mcp.get("token_endpoint") or not mcp.get("client_id"):
        return None, "This connection has no stored MCP authorization; reconnect with OAuth."
    if not rt:
        return None, "No refresh token stored for this connection."
    try:
        doc = _token_request(
            mcp,
            {"grant_type": "refresh_token", "refresh_token": rt},
            str(mcp.get("client_secret") or ""),
            transport,
        )
    except ConnectorError as e:
        log.info("mcp_cms: token refresh failed (%s)", type(e).__name__)
        return None, str(e)
    return doc, None
