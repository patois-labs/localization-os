"""Provider-agnostic OAuth 2.0 authorization-code lifecycle for connectors.

Mirrors app/services/oidc.py (the OIDC relying party) deliberately: all HTTP goes
through an injectable httpx transport, so the whole flow is testable offline with
httpx.MockTransport (tests call set_default_transport()). PKCE S256, one-shot
session state, and constant-time state comparison are carried over verbatim in
spirit — the security properties are identical, only the token *use* differs
(OAuth here yields access/refresh tokens for a connector, not an id_token).

This chunk is PROVIDER-AGNOSTIC: the registry is empty; real Airtable/Atlassian
descriptors land in chunk 3. Everything here is exercised by a synthetic test
provider (tests/verify_connector_oauth.py) registered into the registry.

Design notes:
  - `state` is the CSRF defense for the callback (exactly as OIDC's state is),
    and it additionally BINDS the connection id (cid) so a callback can only ever
    finish the connection that started it.
  - client_id/client_secret are PER-KIND admin settings (Setting table), one
    provider app per kind per install — never stored in a connection's secret_enc.
  - refresh() persists a ROTATED refresh_token when the provider returns one
    (Airtable rotates its refresh token on every use) and falls back to the old
    one when the response omits it (Atlassian keeps the same refresh token).
  - the store owns persistence + single-flight locking; this module is pure
    token protocol (no DB writes).
"""

from __future__ import annotations

import base64
import hashlib
import logging
import re
import secrets
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from urllib.parse import urlencode, urlsplit

import httpx

from app.services import access, security, settings

_logger = logging.getLogger("loc_tms.connectors")

# module-level transport injection point (tests: set_default_transport(mock))
_default_transport: httpx.BaseTransport | None = None

# session keys used during the round-trip (single-use, cleared in complete_authorize)
_SESSION_KEYS = (
    "conn_oauth_state",
    "conn_oauth_verifier",
    "conn_oauth_cid",
    "conn_oauth_kind",
    "conn_oauth_host",
    "conn_oauth_scope",
    "conn_oauth_mcp",
    "conn_oauth_return",
)

# refresh a token when it has <= this many seconds of life left, so a request
# that starts fresh doesn't expire mid-flight (clock skew + round-trip budget).
SKEW_SECONDS = 120


def set_default_transport(transport: httpx.BaseTransport | None) -> None:
    """Inject an httpx transport for ALL connector-oauth HTTP (tests: MockTransport)."""
    global _default_transport
    _default_transport = transport


def _client(transport: httpx.BaseTransport | None = None) -> httpx.Client:
    # OAuth endpoints come only from the fixed provider registry, never user configuration.
    return httpx.Client(transport=transport or _default_transport, timeout=15.0)


def _b64url(data: bytes) -> str:
    # Local copy (2 lines) so we don't import oidc's private helper.
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


# --- provider registry ---------------------------------------------------------
@dataclass(frozen=True)
class OAuthProvider:
    """Everything the generic flow needs to talk to one provider's OAuth server.

    kind                the connector kind this provider serves ("airtable", ...)
    authorize_url       the provider's authorization endpoint
    token_url           the provider's token endpoint (code exchange + refresh)
    scopes              space-joined at request time
    uses_pkce           send PKCE S256 (required by Airtable; harmless elsewhere)
    extra_authorize_params  provider-specific authorize query params (audience, ...)
    post_token_hook     optional (oauth_dict, transport) -> oauth_dict, run after a
                        successful CODE exchange to enrich the token (e.g. resolve
                        Atlassian's cloudid). NOT run on refresh.
    """

    kind: str
    authorize_url: str
    token_url: str
    scopes: str = ""
    uses_pkce: bool = True
    extra_authorize_params: dict = field(default_factory=dict)
    post_token_hook: Callable | None = None


# The registry is keyed by connector kind; register_provider() is how a provider
# (or a test) installs a descriptor. The real Airtable/Atlassian descriptors are
# registered at import time by _register_builtin_providers() below.
_REGISTRY: dict[str, OAuthProvider] = {}


def register_provider(provider: OAuthProvider) -> None:
    _REGISTRY[provider.kind] = provider


def get_provider(kind: str) -> OAuthProvider | None:
    return _REGISTRY.get(kind)


def supports_oauth(kind: str) -> bool:
    """True when an OAuth provider descriptor is registered for this connector."""
    return kind in _REGISTRY


# --- Atlassian: constant gateway host + cloudid format check (SSRF) -------------
# Once a 3LO token is minted, EVERY Jira/Confluence call goes to this fixed public
# host (never the user's *.atlassian.net site), with the cloudid as a PATH segment.
# The host is a hard-coded constant (below) so a user can't retarget it; the only
# user/provider-influenced piece is the cloudid, which we constrain to the exact
# UUID-ish shape Atlassian issues so it can never inject a path traversal or an
# alternate host into the gateway URL.
ATLASSIAN_API_HOST = "https://api.atlassian.com"
ACCESSIBLE_RESOURCES = ATLASSIAN_API_HOST + "/oauth/token/accessible-resources"
# cloudids are lowercase-hex UUIDs with dashes; accept alnum+dash defensively but
# forbid slashes/dots/anything that could escape the {cloudid} path segment.
_CLOUDID_RE = re.compile(r"^[A-Za-z0-9-]{1,64}$")


def valid_cloudid(cloudid: str) -> bool:
    return bool(_CLOUDID_RE.match((cloudid or "").strip()))


def _atlassian_cloudid_hook(base_url_host: str = ""):
    """Build a post_token_hook that resolves the Atlassian cloudid via the
    accessible-resources endpoint and stores it on the oauth dict.

    MULTI-SITE: an app authorized for several Atlassian sites gets a LIST back; we
    match the resource whose `url` host equals the connection's configured
    base_url host, and fall back to the FIRST resource otherwise. `base_url_host`
    is captured per-connection at authorize time.
    FLAG: if the maintainer authorizes an app across multiple Atlassian sites and the
    connection's base_url host doesn't exactly match one of them, the FIRST
    accessible site is used — verify the connection's Site URL matches the site
    you intend to localize."""

    def hook(oauth_dict: dict, transport) -> dict:
        headers = {
            "Authorization": f"Bearer {oauth_dict.get('access_token', '')}",
            "Accept": "application/json",
        }
        with _client(transport) as c:
            r = c.get(ACCESSIBLE_RESOURCES, headers=headers)
            r.raise_for_status()
            resources = r.json() or []
        if not isinstance(resources, list) or not resources:
            raise ValueError("Atlassian returned no accessible resources.")
        chosen = None
        want = (base_url_host or "").strip().lower()
        if want:
            for res in resources:
                host = urlsplit(str(res.get("url") or "")).hostname or ""
                if host.lower() == want:
                    chosen = res
                    break
        chosen = chosen or resources[0]  # first accessible site if no host match
        cloudid = str(chosen.get("id") or "").strip()
        if not valid_cloudid(cloudid):
            raise ValueError(f"Atlassian returned an unexpected cloudid: {cloudid!r}")
        out = dict(oauth_dict)
        out["cloudid"] = cloudid
        out["site_url"] = str(chosen.get("url") or "")  # for the UI / diagnostics
        return out

    return hook


# --- built-in provider descriptors (VERIFIED against live provider docs) --------
# Airtable OAuth 2.0 (PKCE required). Verified 2026-07-03:
#   authorize/token: https://airtable.com/oauth2/v1/{authorize,token}
#   PKCE mandatory; code_challenge_method=S256; code_verifier 43-128 chars.
#   Refresh ROTATES: "the previous access and refresh tokens are invalidated and
#   new tokens are returned" — refresh() persists the rotated refresh_token.
#   Localhost redirect allowed (HTTP exception for localhost).
#   Scopes: data.records:read, data.records:write, schema.bases:read, plus
#   webhook:manage for the Phase 2 official-webhook lifecycle (live QA 2026-07-06:
#   registration 403s without it — same gap the PAT setup_steps had). Existing
#   grants keep working for data access but must RE-CONSENT to pick up the new
#   scope before webhook registration succeeds.
#   Docs: https://airtable.com/developers/web/api/oauth-reference
#         https://airtable.com/developers/web/api/scopes
_AIRTABLE = OAuthProvider(
    kind="airtable",
    authorize_url="https://airtable.com/oauth2/v1/authorize",
    token_url="https://airtable.com/oauth2/v1/token",  # noqa: S106 -- public OAuth token endpoint URL  # pregate: allow-secret not a credential
    scopes="data.records:read data.records:write schema.bases:read webhook:manage",
    uses_pkce=True,
)

# Atlassian 3LO (shared app for Jira + Confluence). Verified 2026-07-03:
#   authorize: https://auth.atlassian.com/authorize
#   token:     https://auth.atlassian.com/oauth/token
#   Required authorize params: audience=api.atlassian.com & prompt=consent.
#   offline_access -> refresh token; Atlassian uses ROTATING refresh tokens too
#   (a new one each use), so refresh() persisting the response's refresh_token is
#   correct for both providers.
#   accessible-resources: GET api.atlassian.com/oauth/token/accessible-resources
#     -> [{id(cloudid), name, url, scopes, avatarUrl}]  (post_token_hook uses id).
#   Classic scopes: read:jira-work + write:jira-work (issue read/search + comment/
#   edit), read:jira-user (G2 — /rest/api/3/myself, the comment/attachment authorship
#   loop-guard; read:jira-work alone does NOT grant it, so without this an OAuth-mode
#   connection silently degrades to the spoofable marker fallback), read:confluence-
#   content.all + write:confluence-content (+ search:confluence for CQL), offline_access.
#   Gateway base paths (token mode UNCHANGED; oauth mode via gateway):
#     Jira:       api.atlassian.com/ex/jira/{cloudid}/rest/api/3/...
#     Confluence: api.atlassian.com/ex/confluence/{cloudid}/rest/api/...
#     NOTE (contradicts the plan's flagged guess): through the gateway Confluence
#     is /ex/confluence/{cloudid}/rest/api — NOT /wiki/rest/api. The /wiki prefix
#     is ONLY on the direct *.atlassian.net site (token mode); the gateway strips
#     it. Verified from the gateway example ".../rest/api/space".
#   Localhost redirect: allowed for dev, but must be HTTP (not HTTPS) for localhost.
#   Docs: https://developer.atlassian.com/cloud/jira/platform/oauth-2-3lo-apps/
#         https://developer.atlassian.com/cloud/jira/platform/scopes-for-oauth-2-3LO-and-forge-apps/
#         https://developer.atlassian.com/cloud/confluence/scopes-for-oauth-2-3LO-and-forge-apps/
_ATLASSIAN_SCOPES = (
    "read:jira-work write:jira-work read:jira-user "
    "read:confluence-content.all write:confluence-content "
    "search:confluence offline_access"
)
# Connector parity: the Confluence v2 API (pages, spaces, children, ancestors, labels) accepts only
# granular scopes (per the v2 OpenAPI: pages read:page / write:page, spaces read:space,
# direct-children read:hierarchical-content, ancestors read:content.metadata, labels
# read:label). They are requested for the CONFLUENCE kind only: the Jira string above is
# byte-identical to what shipped, so a Jira connect / re-consent never asks for a scope the
# Atlassian app may not have enabled yet (Atlassian: a new grant REPLACES the old grant's
# scopes, and an unconfigured scope is not allowed on the authorize request).
_CONFLUENCE_GRANULAR = (
    "read:page:confluence write:page:confluence read:space:confluence "
    "read:label:confluence read:hierarchical-content:confluence "
    "read:content.metadata:confluence"
)
_CONFLUENCE_SCOPES = _ATLASSIAN_SCOPES.replace(
    " offline_access", f" {_CONFLUENCE_GRANULAR} offline_access"
)
_ATLASSIAN_EXTRA = {"audience": "api.atlassian.com", "prompt": "consent"}


def _atlassian_provider(kind: str, base_url_host: str = "") -> OAuthProvider:
    """A per-connection Atlassian descriptor: same shared app (client_id/secret
    are the kind-agnostic 'atlassian' settings — see _client_id override below),
    but the cloudid-resolution hook is bound to THIS connection's base_url host."""
    return OAuthProvider(
        kind=kind,
        authorize_url="https://auth.atlassian.com/authorize",
        token_url="https://auth.atlassian.com/oauth/token",  # noqa: S106 -- public OAuth token endpoint URL  # pregate: allow-secret not a credential
        scopes=_CONFLUENCE_SCOPES if kind == "confluence" else _ATLASSIAN_SCOPES,
        uses_pkce=False,  # Atlassian 3LO uses client_secret, not PKCE
        extra_authorize_params=_ATLASSIAN_EXTRA,
        post_token_hook=_atlassian_cloudid_hook(base_url_host),
    )


def _register_builtin_providers() -> None:
    register_provider(_AIRTABLE)
    # Jira + Confluence share one Atlassian app; each gets a descriptor with a
    # default (host-agnostic) cloudid hook. begin_authorize() re-registers the
    # Atlassian descriptors per-connection with the connection's base_url host
    # bound into the hook (see _rebind_atlassian below).
    register_provider(_atlassian_provider("jira"))
    register_provider(_atlassian_provider("confluence"))


_register_builtin_providers()

# Jira + Confluence authenticate against ONE Atlassian app, so their client creds
# live under a single kind-agnostic settings key ("atlassian"), not per-connector.
_ATLASSIAN_KINDS = ("jira", "confluence")


def _creds_kind(kind: str) -> str:
    """The settings key namespace for a kind's OAuth app creds. Jira + Confluence
    both read oauth_atlassian_client_id/secret (one shared 3LO app); everyone else
    reads oauth_{kind}_client_id/secret."""
    return "atlassian" if kind in _ATLASSIAN_KINDS else kind


def _client_id(kind: str) -> str:
    return (settings.get(f"oauth_{_creds_kind(kind)}_client_id") or "").strip()


def _client_secret(kind: str) -> str:
    # Fernet-at-rest (R3 hardening ride-along); read_secret_setting is legacy-plaintext
    # safe so an install whose oauth_*_client_secret predates the migration still works.
    return settings.read_secret_setting(f"oauth_{_creds_kind(kind)}_client_secret").strip()


# `mcp_cms` is the one kind with NO fixed provider: each connection names its own MCP server, and
# the authorization server, endpoints and (via dynamic registration) the client are DISCOVERED per
# connection (app/services/connectors/mcp_oauth.py). It still shares this module's state/PKCE
# session keys, callback route, token shape and single-flight refresh; only the protocol
# parameters come from the connection instead of the registry.
MCP_KIND = "mcp_cms"


def provider_app_configured(kind: str) -> bool:
    """True when this kind's OAuth provider app creds are set (client_id present).
    Airtable is PKCE-only so a client_secret is optional; Atlassian needs both, but
    a missing secret just fails the token exchange with a clear message — the gate
    for showing the OAuth option is simply 'is there a client_id to send'.
    mcp_cms has no install-wide app: its client is per connection (registered dynamically or
    typed in on the connection), so the gate is always open and discovery reports problems."""
    if kind == MCP_KIND:
        return True
    return bool(_client_id(kind))


def redirect_uri(request) -> str:
    """The callback URL this instance registers with the provider (one per install,
    kind-agnostic — the `state`'s bound cid+kind route the completion)."""
    return settings.external_url(request, "/connectors/oauth/callback")


# --- token shape ---------------------------------------------------------------
def _oauth_from_token_response(resp: dict, *, prior: dict | None = None) -> dict:
    """Build the stored oauth sub-dict from a token endpoint response.

    `prior` (a connection's existing oauth dict) supplies the fallback
    refresh_token when the provider omits one on refresh — Atlassian keeps the
    same refresh token across refreshes and simply doesn't resend it, whereas
    Airtable rotates it and DOES resend, so we always prefer the response's value
    and only fall back to prior when it's absent."""
    prior = prior or {}
    now = int(time.time())
    expires_in = resp.get("expires_in")
    try:
        expires_at = now + int(expires_in) if expires_in is not None else 0
    except (TypeError, ValueError):
        expires_at = 0
    refresh = resp.get("refresh_token") or prior.get("refresh_token") or ""
    return {
        "access_token": resp.get("access_token") or "",
        "refresh_token": refresh,
        "expires_at": expires_at,
        "scope": resp.get("scope") or prior.get("scope") or "",
        "token_type": resp.get("token_type") or prior.get("token_type") or "Bearer",
        "obtained_at": now,
    }


# --- authorization-code flow ---------------------------------------------------
def connection_scope(conn) -> dict:
    """Bind consent to the connection's identity and scope, including NULL scopes."""
    return {
        "kind": conn.kind,
        "org_id": conn.org_id,
        "customer_id": conn.customer_id,
        "created_at": conn.created_at.isoformat(),
    }


def connection_authorized(conn, user, expected_scope) -> bool:
    """Current role, org and customer ring, plus the original consent scope."""
    if not conn or expected_scope != connection_scope(conn):
        return False
    customer = access.current_customer_id(user)
    return (
        security.can_tune_ai(user)
        and user.active
        and access.same_org(user, conn.org_id)
        and not access.customer_ring_blocks(customer, conn.customer_id)
    )


def begin_authorize(
    request,
    kind: str,
    cid: int,
    transport: httpx.BaseTransport | None = None,
    base_url_host: str = "",
    connection=None,
) -> str:
    """Start the authorization-code flow for connection `cid` of `kind`.

    Stashes state (+ the bound cid/kind + PKCE verifier when the provider uses
    PKCE) in the session and returns the provider's authorize URL. `state` is the
    callback's CSRF defense AND the binding that ties the eventual callback to
    this exact connection — a callback whose state doesn't match the session, or
    whose bound cid differs, cannot finish.

    `base_url_host` (Atlassian only): the connection's configured Site URL host,
    bound into the cloudid-resolution hook so a multi-site Atlassian app resolves
    the RIGHT cloudid at code-exchange time. It's stashed in the session and
    re-applied in complete_authorize (the descriptor is process-global, so the
    per-connection host must travel with the round-trip, not on the registry)."""
    if kind == MCP_KIND:
        return _begin_mcp(request, cid, transport, connection)
    provider = get_provider(kind)
    if not provider:
        raise ValueError(f"No OAuth provider registered for kind: {kind}")

    state = secrets.token_urlsafe(24)
    request.session.pop("conn_oauth_scope", None)
    if connection is not None:
        request.session["conn_oauth_scope"] = connection_scope(connection)
    if base_url_host:
        request.session["conn_oauth_host"] = base_url_host
    request.session["conn_oauth_state"] = state
    request.session["conn_oauth_cid"] = int(cid)
    request.session["conn_oauth_kind"] = kind

    params = {
        "response_type": "code",
        "client_id": _client_id(kind),
        "redirect_uri": redirect_uri(request),
        "state": state,
    }
    if provider.scopes:
        params["scope"] = provider.scopes
    if provider.uses_pkce:
        verifier = _b64url(secrets.token_bytes(48))  # 64 chars, PKCE-safe
        challenge = _b64url(hashlib.sha256(verifier.encode("ascii")).digest())
        request.session["conn_oauth_verifier"] = verifier
        params["code_challenge"] = challenge
        params["code_challenge_method"] = "S256"
    else:
        request.session.pop("conn_oauth_verifier", None)
    params.update(provider.extra_authorize_params or {})

    auth_ep = provider.authorize_url
    return auth_ep + ("&" if "?" in auth_ep else "?") + urlencode(params)


def _begin_mcp(request, cid: int, transport, connection) -> str:
    """mcp_cms: discover the MCP server's authorization setup (SSRF-guarded), register or reuse
    a client, stash the one-shot session facts exactly like the fixed providers do, return the
    authorize URL. Raises ConnectorError (a message safe to show) when discovery fails."""
    from app.branding import brand_name
    from app.services.connectors import mcp_oauth
    from app.services.connectors.mcp_cms import effective_mcp_url

    if connection is None or getattr(connection, "kind", "") != MCP_KIND:
        raise ValueError("An mcp_cms connection is required to start MCP authorization.")
    cfg = connection.config or {}
    state = secrets.token_urlsafe(24)
    url, facts, verifier = mcp_oauth.begin(
        # The SAVED connection's MCP URL (or its preset's), the same one the connector calls
        # with the token. Nothing from the request can change where discovery goes.
        mcp_url=effective_mcp_url(cfg),
        redirect_uri=redirect_uri(request),
        state=state,
        client_name=brand_name(),
        client_id=str(cfg.get("oauth_client_id") or ""),
        scope=str(cfg.get("oauth_scope") or ""),
        transport=transport,
        # Client ID Metadata Document (#758): used only when the AS advertises support and
        # app_base_url is a public https URL; otherwise dynamic registration as before.
        client_metadata_url=mcp_client_metadata_url(),
    )
    for k in _SESSION_KEYS:
        request.session.pop(k, None)
    request.session["conn_oauth_scope"] = connection_scope(connection)
    request.session["conn_oauth_state"] = state
    request.session["conn_oauth_cid"] = int(cid)
    request.session["conn_oauth_kind"] = MCP_KIND
    request.session["conn_oauth_verifier"] = verifier
    request.session["conn_oauth_mcp"] = facts
    return url


def mcp_iss_refusal(facts, iss) -> str:
    """The RFC 9207 refusal message for an mcp_cms authorization response (success OR error)
    whose `iss` does not match the recorded issuer, or "" when it is acceptable (#758)."""
    from app.services.connectors import mcp_oauth
    from app.services.connectors.base import ConnectorError

    try:
        mcp_oauth.check_iss(facts if isinstance(facts, dict) else {}, iss)
    except ConnectorError as e:
        return str(e)
    return ""


#: Where the app publishes its OAuth Client ID Metadata Document for mcp_cms (#758).
MCP_CLIENT_METADATA_PATH = "/connectors/oauth/client-metadata.json"


def mcp_client_metadata_url() -> str:
    """The Client ID Metadata Document URL, built ONLY from the configured app_base_url (never
    the request Host, which a caller controls), or "" when none is configured or it is not a
    usable public https URL. The authorization server fetches this URL, so the document and the
    client id must agree byte for byte."""
    from app.services.connectors import mcp_oauth

    base = settings.configured_external_base_url()
    if not base:
        return ""
    url = base + MCP_CLIENT_METADATA_PATH
    return url if mcp_oauth.cimd_usable(url, base + "/connectors/oauth/callback") else ""


def mcp_client_metadata_document() -> dict | None:
    """The document served at MCP_CLIENT_METADATA_PATH, or None (404) when CIMD is unusable."""
    from app.branding import brand_name
    from app.services.connectors import mcp_oauth

    url = mcp_client_metadata_url()
    if not url:
        return None
    callback = settings.configured_external_base_url() + "/connectors/oauth/callback"
    return mcp_oauth.client_metadata_document(url, callback, brand_name())


def _mcp_client_secret(cid: int, client_id: str) -> str:
    """The operator-supplied client secret for this connection, or "". Read from the
    connection's own encrypted secret: the typed-in `oauth_client_secret` before the first
    connect, or the one carried in a previous OAuth grant of the SAME client id on a reconnect.
    A dynamically registered client is public and has none."""
    from app.services.connectors import store as _store

    conn = _store.get(int(cid))
    if not conn:
        return ""
    cfg = conn.config or {}
    if not str(cfg.get("oauth_client_id") or "").strip():
        return ""
    sec = _store.dec_secret(conn.secret_enc) or {}
    typed = str(sec.get("oauth_client_secret") or "").strip()
    if typed:
        return typed
    prior = ((sec.get("oauth") or {}).get("mcp")) or {}
    if isinstance(prior, dict) and prior.get("client_id") == client_id:
        return str(prior.get("client_secret") or "")
    return ""


def _complete_mcp(request, cid: int, code: str, verifier: str, facts, transport):
    from app.services.connectors import mcp_oauth
    from app.services.connectors.base import ConnectorError

    if not isinstance(facts, dict) or not facts.get("token_endpoint") or not verifier:
        return None, "OAuth session is incomplete - please start the connection again."
    try:
        # RFC 9207 mix-up defence, BEFORE the code (and any client secret) goes anywhere.
        mcp_oauth.check_iss(facts, request.query_params.get("iss"))
        secret = _mcp_client_secret(cid, str(facts.get("client_id") or ""))
        resp = mcp_oauth.exchange_code(
            facts, code, redirect_uri(request), verifier, secret, transport
        )
    except ConnectorError as e:
        _logger.info("MCP OAuth code exchange refused (%s)", type(e).__name__)
        return None, f"Token exchange with the MCP server's authorization server failed: {e}"
    oauth = _oauth_from_token_response(resp)
    if not oauth["scope"]:
        oauth["scope"] = str(facts.get("scope") or "")
    oauth["mcp"] = mcp_oauth.stored_facts(facts, secret)
    return (int(cid), oauth), None


def complete_authorize(
    request, code: str, state: str, transport: httpx.BaseTransport | None = None
):
    """Finish the flow: validate state (constant-time, one-shot), exchange the
    code for tokens, compute expires_at, run the provider's post-token hook.

    Returns ((cid, oauth_dict), None) on success or (None, error_message) on any
    failure. The session keys are single-use and cleared up front regardless of
    outcome, so a callback can never be replayed."""
    sess_state = request.session.get("conn_oauth_state") or ""
    verifier = request.session.get("conn_oauth_verifier") or ""
    cid = request.session.get("conn_oauth_cid")
    kind = request.session.get("conn_oauth_kind") or ""
    host = request.session.get("conn_oauth_host") or ""
    mcp_facts = request.session.get("conn_oauth_mcp")
    for k in _SESSION_KEYS:
        request.session.pop(k, None)

    if not code:
        return None, "The provider did not return an authorization code."
    # constant_time_eq (not raw compare_digest): `state` is a callback QUERY param that can be
    # non-ASCII, which would make compare_digest(str, str) raise TypeError -> an unauthenticated
    # 500 on the connector OAuth callback (task_3cc0e09a class). A non-ASCII state can never
    # match the session's ASCII state -> a clean mismatch.
    if not sess_state or not security.constant_time_eq(state or "", sess_state):
        return None, "OAuth state mismatch - please start the connection again."
    provider = get_provider(kind)
    if (not provider and kind != MCP_KIND) or cid is None:
        return None, "OAuth session is incomplete - please start the connection again."
    # ffadmin Wave 5 (HIGH -- the Wave-3 service-seam lesson): refuse a DISABLED kind HERE,
    # BEFORE the code->token exchange, so an OAuth flow started while the kind was enabled
    # and whose callback lands AFTER it was disabled egresses NOTHING to the provider's
    # token_url and writes no secret/status. The session keys were already cleared up top,
    # so this is one-shot; the callback surfaces the message as a clean refusal (not a 500).
    from app.services import connectors as _reg

    if not _reg.kind_enabled(kind):
        return None, (
            "This connector is disabled "
            "(settings connector_disabled_kinds) - re-enable it to reconnect."
        )

    if kind == MCP_KIND:
        return _complete_mcp(request, cid, code, verifier, mcp_facts, transport)

    data = {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": redirect_uri(request),
        "client_id": _client_id(kind),
    }
    if verifier:
        data["code_verifier"] = verifier  # PKCE
    secret = _client_secret(kind)
    if secret:
        data["client_secret"] = secret
    try:
        with _client(transport) as c:
            r = c.post(provider.token_url, data=data)
            r.raise_for_status()
            token_resp = r.json()
    except Exception:
        _logger.exception("Connector OAuth code exchange failed (kind=%s)", kind)
        return None, "Token exchange with the provider failed."

    if not token_resp.get("access_token"):
        return None, "The provider did not return an access token."

    oauth = _oauth_from_token_response(token_resp)
    # The registry's Atlassian hook is host-agnostic (host is per-connection, not
    # per-descriptor); rebind it to THIS round-trip's connection host so a
    # multi-site app resolves the right cloudid. Non-Atlassian kinds use the
    # descriptor's own hook unchanged.
    hook = provider.post_token_hook
    if kind in _ATLASSIAN_KINDS and host:
        hook = _atlassian_cloudid_hook(host)
    if hook:
        try:
            oauth = hook(oauth, transport or _default_transport)
        except Exception:
            _logger.exception("Connector OAuth post-token hook failed (kind=%s)", kind)
            return None, "The provider connected, but a follow-up call failed."
    return (int(cid), oauth), None


def refresh(kind: str, oauth: dict, transport: httpx.BaseTransport | None = None):
    """Exchange a refresh_token for a fresh access_token.

    Returns (new_oauth_dict, None) on success or (None, error_message) on failure.
    CRITICAL: persists the ROTATED refresh_token from the response and falls back
    to the incoming one only when the response omits it (see
    _oauth_from_token_response). Callers must write the returned dict back under a
    lock (the store does the single-flight); this function is pure protocol."""
    if kind == MCP_KIND:
        # Same single-flight caller (store_part2.ensure_fresh_token), same token shape and the
        # same carry-forward below; only the endpoint and client come from the stored grant.
        from app.services.connectors import mcp_oauth

        token_resp, err = mcp_oauth.refresh(oauth, transport)
        if err:
            return None, err
        return _finish_refresh(token_resp, oauth)
    provider = get_provider(kind)
    if not provider:
        return None, f"No OAuth provider registered for kind: {kind}"
    refresh_token = (oauth or {}).get("refresh_token") or ""
    if not refresh_token:
        return None, "No refresh token stored for this connection."

    data = {
        "grant_type": "refresh_token",
        "refresh_token": refresh_token,
        "client_id": _client_id(kind),
    }
    secret = _client_secret(kind)
    if secret:
        data["client_secret"] = secret
    try:
        with _client(transport) as c:
            r = c.post(provider.token_url, data=data)
            r.raise_for_status()
            token_resp = r.json()
    except Exception:
        _logger.exception("Connector OAuth refresh failed (kind=%s)", kind)
        return None, "Token refresh with the provider failed."

    return _finish_refresh(token_resp, oauth)


def _finish_refresh(token_resp: dict, oauth: dict):
    if not token_resp.get("access_token"):
        return None, "The provider did not return a refreshed access token."
    # prior=oauth so a response without refresh_token keeps the old one; cloudid /
    # other provider-added keys the hook stored are preserved below.
    new_oauth = _oauth_from_token_response(token_resp, prior=oauth or {})
    # Carry forward any provider-specific keys (e.g. Atlassian cloudid) that the
    # code exchange's post_token_hook added — refresh responses don't repeat them.
    for k, v in (oauth or {}).items():
        if k not in new_oauth:
            new_oauth[k] = v
    return new_oauth, None
