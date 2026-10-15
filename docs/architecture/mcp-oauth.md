# MCP OAuth (Phase C2) - Architecture (L1)

## 1. Role

Localization OS can act as the **OAuth 2.1 authorization server** for its own remote MCP server,
so directory-style MCP clients (claude.ai custom connectors, ChatGPT connectors) connect with a
browser sign-in and a consent page instead of a pasted per-user `lsu_` key (#753, MCP spec
revision 2026-07-28). It is **off by default** and changes nothing until an operator switches it
on (`setting:mcp_oauth_enabled`) and configures the MCP server listener as a **resource server**
(`integrations/mcp-server/oauth_rs.py`, environment `LOC_STUDIO_MCP_OAUTH_RESOURCE`,
`LOC_STUDIO_MCP_OAUTH_ISSUER`, `LOC_STUDIO_MCP_RS_SECRET`).

The pieces: RFC 8414 metadata at `/.well-known/oauth-authorization-server`; the authorization
code grant with mandatory PKCE S256, the RFC 8707 `resource` parameter and the RFC 9207 `iss`
response parameter; a signed-in consent page; **Client ID Metadata Documents** as the first client
registration mechanism (an https URL is the client id, fetched through the SSRF egress guard); and
deprecated RFC 7591 dynamic registration only when `setting:mcp_oauth_dcr_enabled` is on. The MCP
server serves RFC 9728 protected resource metadata, answers 401 with `resource_metadata` and
`scope` in `WWW-Authenticate`, and 403 `insufficient_scope` naming every scope a tools/call
request needs.

Scopes are the per-user API key grants of the same names: `api` (always), `docs`, `keys`. Admin
and SCIM are never grantable. Per-user `lsu_` keys are untouched and stay the documented
alternative for Claude Code and Codex. This subsystem also owns the **client** fixes for the
`mcp_cms` connector's MCP authorization (#758: exact issuer comparison, `application_type` on
dynamic registration, our own Client ID Metadata Document); the client itself is documented in
[connectors](connectors.md).

## 2. Invariants

- **No token passthrough.** The MCP server never forwards the client's token to `/api`; it
  exchanges it (RFC 8693, authenticated with the resource-server secret) for an `lso_` token that
  only `/api` accepts - `app/services/mcp_as.py::api_identity` and the gate in
  `integrations/mcp-server/oauth_rs.py`, enforced by `verify_mcp_oauth_as`.
- **Audience binding.** Every token is bound to the configured `setting:mcp_oauth_resource`;
  changing it invalidates every token at once - `app/services/mcp_as.py::_resource_ok`.
- **Hash-only storage.** Codes and tokens are stored as SHA-256 digests on
  `app/models.py::McpOAuthGrant`; the resource-server secret only as `setting:mcp_oauth_rs_secret_hash`.
- **No open redirect.** An unknown client or an unregistered redirect URI is shown on our page and
  never redirected to - `app/services/mcp_as.py::validate_authorize`.
- **One-time codes, rotating refresh tokens.** A reused code or refresh token revokes the grant -
  `app/services/mcp_as.py::token`.
- **Re-enabling revives nothing.** Every account disable, erasure and revoke-all goes through
  `app/services/apikeys.py::revoke_all_for_user`, which revokes the grants in the same transaction.
- **An OAuth identity cannot mint API keys** (`POST /api/apikeys/issue` answers 403).
- **Exact issuer comparison on the client side** (#758) - `app/services/connectors/mcp_oauth.py::check_iss`,
  enforced by `verify_mcp_oauth`.

## 3. Where the work happens

| Concern | Pin | One line |
|---|---|---|
| Authorization server | `app/services/mcp_as.py` | metadata, client resolution (CIMD/DCR), authorize, consent grant, token endpoint, exchange, `/api` identity, grants |
| Pure helpers + validators | `app/services/mcp_as_common.py` | grantable scopes, resource URL rules, CIMD host trust list, setting validators |
| Routes | `app/web/mcp_oauth_as.py` | well-known metadata, `/oauth/*`, connected apps, the admin page, `/api/mcp-oauth/grants` |
| CLI | `app/cli/cmd_mcp_oauth.py` | `mcp-oauth status / rs-secret / grants / revoke` |
| Resource-server gate | `integrations/mcp-server/oauth_rs.py` | PRM, 401/403 challenges, token exchange cache, header swap |

## 4. Data & settings

- **Models:** `app/models.py::McpOAuthGrant` - one consented grant: user, client id and name,
  redirect URI, scopes, bound resource, the current generation of code/token digests, the previous
  refresh digest (reuse detection), `revoked_at`. Excluded from the compliance export
  (credential material); the account's own grants are in its access export without digests.
- **Settings:** `setting:mcp_oauth_enabled` (default `0`), `setting:mcp_oauth_resource` (blank),
  `setting:mcp_oauth_dcr_enabled` (default `0`), `setting:mcp_oauth_cimd_hosts` (blank = any
  public host), `setting:mcp_oauth_rs_secret_hash` (blank). All five are host-only over `/api`
  (`REMOTE_IMMUTABLE_KEYS`) and confirm-to-set (`SENSITIVE_KEYS`); web page `/security/mcp-oauth`,
  CLI `config set` plus `mcp-oauth`. The issuer is `setting:app_base_url`, which must be https at
  the host root.
- **Tokens:** `lsm_` MCP access token (1 hour), `lsr_` refresh token (30 days, rotated), `lso_`
  `/api` token (an HMAC of the `lsm_` token under the session secret, so every MCP server process
  gets the same one, valid exactly as long), `lsrs_` resource-server secret.

## 5. Enforced by

- `verify_mcp_oauth_as` - the whole authorization server and the MCP server gate, offline.
- `verify_mcp_oauth` - the `mcp_cms` client, including section 10 (#758).
- `verify_data_export` - the table is classified.
- `verify_mcp_parity`, `verify_cli_parity`, `verify_cli_scope_classification` - the two `/api`
  routes are MCP-exempt (credential lifecycle) and the CLI verbs are documented and classified.

## 6. Battle scars

- **Phase C2 was deferred in August** because the SDK offered no 401 hook that still let a client
  `initialize` anonymously; the 2026-07-28 revision removed `initialize`, so the listener can now
  challenge every request.
- **The 2026-10-11 interop review** (findings M5 and L5) asked for this surface and for the client fixes
  listed here.

## 7. Limits & accepted risks

- **Static `lsu_` keys still pass the MCP gate unchanged** while OAuth is on: a documented
  divergence from "MCP servers MUST NOT accept any other tokens", kept because Claude Code and Codex
  rely on it; those keys are issued by this same install.
- **Public clients only.** `private_key_jwt` and client secrets are refused; PKCE is the only
  client proof. A Client ID Metadata Document cannot stop a localhost redirect impersonation; the
  consent page warns on loopback redirects and shows the redirect host.
- **Dynamic registration is stateless** (the client id is a signed record), so a single registered
  client cannot be revoked; switching DCR off disables all of them, and grants are revocable.
- **The exchange cache** in the MCP server keeps a result for at most 5 minutes; a revoked grant's
  `lso_` token is refused by `/api` at once, so the cache only delays the 401 at the MCP layer.
- **Single issuer at the host root.** An `app_base_url` with a path prefix is reported as not ready
  rather than served with path-inserted metadata.
