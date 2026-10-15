"""OIDC Relying Party — real single sign-on via authorization code + PKCE.

Hand-rolled against any spec-compliant IdP (Okta, Entra ID, Auth0, Keycloak,
Google, ...) using httpx + PyJWT. Design mirrors the connector framework: all
HTTP goes through an injectable httpx transport, so the whole flow is testable
offline with httpx.MockTransport (tests call set_default_transport()).

Security properties:
  - PKCE S256 on every request (client secret optional, sent when configured)
  - state validated constant-time against the session (CSRF on the callback)
  - id_token signature verified against the issuer's JWKS (RS256 only),
    plus iss / aud / exp / nonce — unverified claims are NEVER trusted
  - one-shot session keys: state/nonce/verifier are cleared on first use
  - https enforced on the issuer AND every discovery-derived endpoint
    (authorize/token/JWKS); plain http is allowed only for a loopback IdP
  - error messages shown to the browser are generic; details go to the log
  - every outbound call (discovery, JWKS, token) rides the SSRF egress guard (#354): the
    issuer host is resolved once, every answer validated, only those addresses dialed and
    the peer re-checked; a deliberately private IdP is admitted through the host-only
    egress_private_allowlist. The one documented exception is the plain-http loopback dev
    IdP (localhost / 127.0.0.1 / ::1), which the guard could never admit

Documented in: docs/architecture/sso-provisioning.md
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import secrets
from urllib.parse import unquote_plus, urlencode, urlparse

import httpx

from app.services import egress, security, settings

_logger = logging.getLogger("loc_tms")

_WELL_KNOWN = "/.well-known/openid-configuration"

# module-level transport injection point (tests: set_default_transport(mock))
_default_transport: httpx.BaseTransport | None = None

# discovery metadata cache, keyed by issuer URL
_discovery_cache: dict[str, dict] = {}

# session keys used during the round-trip (single-use, cleared in complete_login)
_SESSION_KEYS = ("oidc_state", "oidc_nonce", "oidc_verifier", "oidc_next")


def set_default_transport(transport: httpx.BaseTransport | None) -> None:
    """Inject an httpx transport for ALL oidc HTTP (tests use MockTransport)."""
    global _default_transport
    _default_transport = transport


def clear_cache() -> None:
    """Drop cached discovery documents (tests; or after changing the issuer)."""
    _discovery_cache.clear()


_EGRESS_MSG = (
    "SSO is misconfigured: the identity provider's address is not a permitted outbound "
    "target. If it is a private host, an administrator must add it to the host-only "
    "egress_private_allowlist."
)


def _client(
    transport: httpx.BaseTransport | None = None, url: str = "", iss: str | None = None
) -> httpx.Client:
    """The one OIDC HTTP client, built per target URL.

    #354: the issuer, discovery-derived endpoints and JWKS are operator-entered / IdP-served
    URLs, so they ride the egress guard (resolve once, pin, peer check; allowlist for a
    private IdP). The ONLY exception is the documented loopback dev IdP: a plain-http
    localhost / 127.0.0.1 / ::1 target (the same literal set ``_url_insecure`` exempts),
    which the guard refuses by design and which only a local process can answer. The
    exception holds ONLY when the configured issuer (`iss`, default the URL itself) is that
    loopback IdP too: a public IdP whose discovery document names a loopback token or JWKS
    endpoint stays on the guarded client, so the client_secret is never posted there."""
    inner = transport or _default_transport
    if _loopback_http(url) and _loopback_http(url if iss is None else iss):
        return httpx.Client(transport=inner, timeout=15.0)
    return httpx.Client(timeout=15.0, **egress.guarded_transport_kwargs("oidc", transport=inner))


def _loopback_http(url: str) -> bool:
    u = urlparse(url or "")
    return u.scheme == "http" and (u.hostname or "").lower() in ("localhost", "127.0.0.1", "::1")


def issuer() -> str:
    """The configured issuer, normalized (no trailing slash; a pasted
    .well-known discovery URL is accepted and trimmed back to the issuer)."""
    iss = (settings.get("sso_issuer") or "").strip().rstrip("/")
    if iss.endswith(_WELL_KNOWN):
        iss = iss[: -len(_WELL_KNOWN)].rstrip("/")
    return iss


def enabled() -> bool:
    return settings.get("sso_enabled") == "1" and bool(issuer())


def _url_insecure(url: str) -> bool:
    """True when a URL is plain http to a non-loopback host. A cleartext issuer
    (or cleartext discovery endpoints) would let a network MITM serve forged
    JWKS/token responses and mint arbitrary identities. Loopback is allowed so
    a local development IdP (Keycloak on localhost) still works."""
    u = urlparse(url or "")
    if u.scheme == "https":
        return False
    host = (u.hostname or "").lower()
    return not (u.scheme == "http" and host in ("localhost", "127.0.0.1", "::1"))


_INSECURE_MSG = (
    "SSO is misconfigured: the issuer and its endpoints must use "
    "https (plain http is only allowed for a localhost IdP)."
)


def _require_https(iss: str, meta: dict | None = None) -> None:
    """Raise ValueError when the issuer or any discovery-derived endpoint we
    are about to trust is served over cleartext http."""
    urls = [iss]
    if meta:
        urls += [
            meta.get("authorization_endpoint") or "",
            meta.get("token_endpoint") or "",
            meta.get("jwks_uri") or "",
        ]
    for url in urls:
        if url and _url_insecure(url):
            raise ValueError(_INSECURE_MSG)
    # The loopback http allowance belongs to a loopback dev IdP only: a non-loopback issuer
    # whose discovery points an endpoint at loopback is refused (it would carry the
    # client_secret and the code to a local port).
    if meta and not _loopback_http(iss):
        for url in urls[1:]:
            if url and _loopback_http(url):
                raise ValueError(_INSECURE_MSG)


def discover(issuer_url: str, transport: httpx.BaseTransport | None = None) -> dict:
    """Fetch (and cache) the OIDC discovery document for an issuer."""
    key = (issuer_url or "").rstrip("/")
    if key in _discovery_cache:
        return _discovery_cache[key]
    try:
        with _client(transport, key) as c:
            r = c.get(key + _WELL_KNOWN)
            r.raise_for_status()
            meta = r.json()
    except egress.EgressBlocked as exc:
        _logger.warning("OIDC discovery refused by the egress guard: %s", exc)
        raise ValueError(_EGRESS_MSG) from exc
    _discovery_cache[key] = meta
    return meta


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


#: Query parameters that carry a message a page shows (?err=, ?msg=, ?notice=, key_error,
#: srx_msg, engine_warning, ...). ONE definition: app/web/page_guard.py filters and records
#: exactly these, and _sanitize_next strips them from every post-sign-in target.
MESSAGE_PARAMS = frozenset(
    {"err", "error", "msg", "notice", "locale_removed", "jpv", "gl_degraded"}
)
MESSAGE_PARAM_SUFFIXES = ("_error", "_msg", "_notice", "_warning", "_warn")


def is_message_param(name: str) -> bool:
    """True for a query parameter whose value a page renders as a message."""
    return name in MESSAGE_PARAMS or name.endswith(MESSAGE_PARAM_SUFFIXES)


def is_unsafe_local_target(nxt: str) -> bool:
    """True unless ``nxt`` (already stripped) is a same-site absolute path a browser can only
    read as one: exactly one leading '/', no scheme or ':' at all, no backslash, and no
    control character. Security review 2026-10 web-01: browsers DROP tab, CR and LF inside
    a URL (WHATWG), so '/<TAB>/evil.example' passed the '//' test yet resolved as the
    protocol-relative '//evil.example' once rendered into a link. Every post-sign-in
    ``next`` and every form-supplied redirect target goes through this one test."""
    return (
        not nxt.startswith("/")
        or nxt.startswith("//")
        or "\\" in nxt
        or ":" in nxt
        or any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in nxt)
    )


def _sanitize_next(nxt: str) -> str:
    """Only same-site absolute paths survive; anything else becomes '/'.

    Security review 2026-10 web-01: a control character anywhere (TAB, CR, LF, DEL)
    also gives '/' (see is_unsafe_local_target).

    Security review 2026-09 (gate fix-auth-easy round 1, MEDIUM-1): every message
    parameter (is_message_param) is removed from the target's query. A sign-in, the
    two-step code, SAML RelayState, the password change and the expired-session return all
    redirect to this value after a real, CSRF-checked POST, so a message in it would read
    as the product's own words (a link to the real sign-in page carrying
    next=/projects/1?err=<phishing text>). Every other parameter is kept byte-for-byte."""
    nxt = (nxt or "").strip()
    if is_unsafe_local_target(nxt):
        return "/"
    path, sep, rest = nxt.partition("?")
    if not sep:
        return nxt
    query, hsep, frag = rest.partition("#")
    kept = [
        seg
        for seg in query.split("&")
        if seg and not is_message_param(unquote_plus(seg.split("=", 1)[0]))
    ]
    return path + ("?" + "&".join(kept) if kept else "") + ("#" + frag if hsep else "")


def redirect_uri(request) -> str:
    """The callback URL this instance registers with the IdP."""
    return settings.external_url(request, "/auth/oidc/callback")


def begin_login(request, transport: httpx.BaseTransport | None = None) -> str:
    """Start the authorization-code flow: stash state/nonce/PKCE verifier (and a
    sanitized post-login 'next') in the session, return the IdP authorize URL."""
    iss = issuer()
    _require_https(iss)
    meta = discover(iss, transport)
    _require_https(iss, meta)
    state = secrets.token_urlsafe(24)
    nonce = secrets.token_urlsafe(24)
    verifier = _b64url(secrets.token_bytes(48))  # 64 chars, PKCE-safe
    challenge = _b64url(hashlib.sha256(verifier.encode("ascii")).digest())
    request.session["oidc_state"] = state
    request.session["oidc_nonce"] = nonce
    request.session["oidc_verifier"] = verifier
    request.session["oidc_next"] = _sanitize_next(request.query_params.get("next", "/"))
    params = {
        "response_type": "code",
        "client_id": settings.get("sso_client_id") or "",
        "redirect_uri": redirect_uri(request),
        "scope": "openid email profile",
        "state": state,
        "nonce": nonce,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
    }
    auth_ep = meta.get("authorization_endpoint") or iss + "/authorize"
    return auth_ep + ("&" if "?" in auth_ep else "?") + urlencode(params)


def stored_next(request) -> str:
    """The sanitized post-login destination stored by begin_login (default '/').
    Read this BEFORE complete_login, which clears all oidc session keys."""
    return _sanitize_next(request.session.get("oidc_next") or "/")


def _signing_key(
    id_token: str,
    meta: dict,
    transport: httpx.BaseTransport | None = None,
    iss: str | None = None,
):
    """Resolve the id_token's signing key from the issuer's JWKS."""
    # PyJWT is imported on first use (test speed, 2026-10-08): only an OIDC sign-in needs it.
    import jwt
    from jwt.algorithms import RSAAlgorithm

    kid = jwt.get_unverified_header(id_token).get("kid")
    with _client(transport, meta["jwks_uri"], iss if iss is not None else issuer()) as c:
        r = c.get(meta["jwks_uri"])
        r.raise_for_status()
        jwks = r.json()
    for k in jwks.get("keys", []):
        if k.get("kty") != "RSA":
            continue
        if k.get("use") and k.get("use") != "sig":
            continue  # encryption keys are never trusted
        if k.get("alg") and k.get("alg") != "RS256":
            continue  # we verify RS256 only
        if kid is not None and k.get("kid") != kid:
            continue  # kid in the header must match exactly
        return RSAAlgorithm.from_jwk(json.dumps(k))
    raise ValueError("no matching RSA signing key in the issuer JWKS")


def complete_login(request, code: str, state: str, transport: httpx.BaseTransport | None = None):
    """Finish the flow: validate state, exchange the code, verify the id_token.

    Returns (claims, None) on success or (None, error_message) on any failure.
    The oidc session keys are single-use and cleared up front regardless of
    outcome, so a callback can never be replayed."""
    sess_state = request.session.get("oidc_state") or ""
    sess_nonce = request.session.get("oidc_nonce") or ""
    verifier = request.session.get("oidc_verifier") or ""
    for k in _SESSION_KEYS:
        request.session.pop(k, None)

    if not code:
        return None, "The identity provider did not return an authorization code."
    # constant_time_eq (not raw compare_digest): `state` is a callback QUERY param that can be
    # non-ASCII (Starlette utf-8-decodes it), which would make compare_digest(str, str) raise
    # TypeError -> an unauthenticated 500 on /auth/oidc/callback (task_3cc0e09a class). A
    # non-ASCII state can never match the session's ASCII state -> a clean mismatch 401.
    if not sess_state or not security.constant_time_eq(state or "", sess_state):
        return None, "Sign-in state mismatch - please try again."

    iss = issuer()
    try:
        _require_https(iss)
        meta = discover(iss, transport)
        _require_https(iss, meta)
    except ValueError as e:
        return None, str(e)  # our own https-config message
    except Exception:
        # never reflect raw exception text (internal URLs, library internals)
        _logger.exception("OIDC discovery failed for issuer %r", iss)
        return None, (
            "OIDC discovery failed - the identity provider's configuration could not be retrieved."
        )

    client_id = settings.get("sso_client_id") or ""
    data = {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": redirect_uri(request),
        "client_id": client_id,
        "code_verifier": verifier,  # PKCE always
    }
    client_secret = settings.read_secret_setting("sso_client_secret")  # Fernet-at-rest (R3)
    if client_secret:
        data["client_secret"] = client_secret
    try:
        with _client(transport, meta["token_endpoint"], iss) as c:
            r = c.post(meta["token_endpoint"], data=data)
            r.raise_for_status()
            token_resp = r.json()
    except egress.EgressBlocked as exc:
        _logger.warning("OIDC token exchange refused by the egress guard: %s", exc)
        return None, _EGRESS_MSG
    except Exception:
        _logger.exception("OIDC token exchange failed")
        return None, "Token exchange with the identity provider failed."

    id_token = token_resp.get("id_token") or ""
    if not id_token:
        return None, "The identity provider did not return an id_token."

    # Verify BEFORE reading any claim: signature (issuer JWKS, RS256 only),
    # iss, aud = our client_id, exp. jwt.decode raises on any failure.
    import jwt

    try:
        key = _signing_key(id_token, meta, transport, iss)
        claims = jwt.decode(
            id_token,
            key=key,
            algorithms=["RS256"],
            audience=client_id,
            issuer=meta.get("issuer") or iss,
            # identity-01: `sub` is required too; the account is bound to (iss, sub).
            options={"require": ["exp", "iss", "aud", "sub"]},
        )
    except Exception:
        _logger.exception("OIDC id_token validation failed")
        return None, "id_token validation failed - the token could not be trusted."

    token_nonce = str(claims.get("nonce") or "")
    # constant_time_eq: a non-ASCII id_token nonce would likewise 500 a raw compare_digest.
    if not sess_nonce or not security.constant_time_eq(token_nonce, sess_nonce):
        return None, "Sign-in nonce mismatch - the response could not be trusted."
    return claims, None
