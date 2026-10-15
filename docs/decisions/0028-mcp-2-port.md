# 0028. MCP server on mcp 2.2.0, failing loud on SDK drift

- Status: Accepted
- Date: 2026-10-02 (#369; Dependabot PR #326 deferred into its own train on 10-01)

## Context

mcp 2.x renamed FastMCP to MCPServer, moved request context, added a sessionless transport
(protocol 2026-07-28) and hides the text of non-ToolError exceptions. Our middleware decides which token
a request uses; a silent API change could fall open to the server's env token.

## Decision

- Pin EXACT `mcp==2.2.0` (window assert `>=2.2,<3`).
- The middleware reads `ctx.request` directly so drift raises instead of degrading; HTTP paths never use
  the env token (stdio only).
- Expected refusals are raised as `ToolError` so callers keep the message; body pre-checks use httpx's
  exact JSON encoding; base URLs shown to clients go through `_public_base_url()` (credentials masked).
- Session posture stays as in 1.x (no idle reaping, no session cap, 8 MiB body cap).

## Consequences

- Any mcp bump re-runs every MCP suite locally.
- Open owner question: turn on session reaping and a cap on the hosted listener (suggested: idle
  timeout of a few hours, cap in the low thousands).

## Links

- .claude/rules/mcp-server.md
