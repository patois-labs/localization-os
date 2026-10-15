# 0077. MCP tools/list follows the key's scopes; toolsets

Status: Accepted
Date: 2026-10-11

## Context

#751: every client saw all 84 tools whatever its key could do, and paired with vendor MCP servers (Contentful alone
has 87) the tool budget runs out.

## Decision

- tools/list shows only tools the calling key's scopes allow (`/api/account` returns the key's own `scopes`).
  Filtering is a convenience; the API still refuses a hidden tool called anyway.
- `LOC_STUDIO_MCP_TOOLSETS` limits the server to named toolsets; a header or query can only narrow it further.
  A call outside the toolsets gets a ToolError naming how to add it.
- A failed scope lookup fails NARROW (api-scope tools plus `whoami`, retried after 5 s), never wide.
  `SCOPE_FILTER=off` lists everything. CLI: `mcp connect --toolsets`, `account show --remote`.

## Consequences

- Claude Desktop started before the install is up sees the narrow list until it re-lists tools.
