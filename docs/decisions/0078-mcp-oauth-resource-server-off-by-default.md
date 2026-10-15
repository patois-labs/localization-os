# 0078. MCP OAuth authorization and resource server, off by default

Status: Accepted
Date: 2026-10-11

## Context

#753: directory-style clients (claude.ai, ChatGPT) connect to MCP servers with OAuth 2.1, not pasted keys. #758:
our `mcp_oauth` client needed exact `iss` checks and a Client ID Metadata Document.

## Decision

- Opt-in OAuth 2.1 authorization server plus resource server for `/mcp`. OFF by default: every endpoint 404s until
  `mcp_oauth_enabled` is on AND sign-in, an https root issuer, the resource and the RS secret are configured; the RS
  middleware runs only when `LOC_STUDIO_MCP_OAUTH_RESOURCE` is set (a half config refuses to start).
- PKCE S256 only, exact redirect match (loopback any port), one-time 60 s codes, rotated refresh tokens, tokens
  bound to the resource; scopes api/docs/keys only, same role guards at /api; OAuth identities cannot mint keys.
- CIMD supported and advertised; dynamic client registration OFF by default. Switches are admin and host-only.
- Client side: exact `iss` comparison, `application_type`, our own CIMD document.

## Consequences

- Not verified against claude.ai or ChatGPT (needs a TLS instance). No RFC 7009 revocation endpoint.
- New alembic revision a7c3e9f1b2d4.
