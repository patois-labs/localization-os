---
paths:
  - "integrations/mcp-server/**"
  - "app/cli/cmd_mcp.py"
  - "tests/verify_mcp*"
  - "tests/verify_utcp*"
---

# MCP server (mcp 2.x)

Why: ADR 0028 (mcp 2.2.0 port).

- The pin is EXACT `mcp==2.3.0` with a window assert `>=2.2,<3`. Any bump re-runs every `verify_mcp*` suite plus `verify_utcp_manual`, `verify_badinput_folded`, `verify_sec_preenable`, `verify_stress_liveness_pool` (on an idle machine).
- The request middleware reads `ctx.request` DIRECTLY (not `getattr`): SDK drift must fail loudly, never fall open to the env `LOC_STUDIO_TOKEN`. It sets and resets the `_MCP_REQUEST` contextvar inside each request's own task.
- HTTP transports (legacy and the sessionless 2026-07-28 path) never use the env token; only stdio forwards it. Bearer beats `X-Loc-Token`; non-ASCII tokens are refused.
- mcp 2 hides non-`ToolError` text: every EXPECTED refusal must raise `ToolError`. Body pre-checks use httpx's exact JSON encoding (`ensure_ascii=False`, compact separators, `allow_nan=False`, utf-8).
- Every client-visible mention of the base URL goes through `_public_base_url()` (masks `user:pass@`).
- HTTP sessions default to `max_sessions=2000` and `session_idle_timeout=14400` seconds; `LOC_STUDIO_MCP_MAX_SESSIONS` accepts 1..10000 and `LOC_STUDIO_MCP_SESSION_IDLE_TIMEOUT` accepts 60..86400 (invalid values fail startup). Keep the 8 MiB body cap and pre-allocation admission limiter: shared burst of 10, refill one/minute, metered on the MCP endpoint path only (other paths never spend it), excess gets 429 with Retry-After. `LOC_STUDIO_MCP_CLIENT_LIMITS` gives only operator-listed API-key fingerprints independent bounded budgets; unlisted keys/IP headers cannot create budgets. Existing sessions and the sessionless 2026-07-28 wire remain usable.
- DNS-rebinding allowlist is explicit on loopback; a non-localhost bind turns protection off (same as 1.x). Only `stdio` and `streamable-http` transports.
- Settings keys and tool names are a pinned contract (`tests/branding_contract_snapshot.json`, MCP `EXEMPT`); the `locstudio://` URI token in `tests/verify_mcp_transport.py` is a real wire assertion: do not change it.
- Every feature ships on API + MCP + help + `llms.txt` (ADR 0011).
- A new MCP tool changes the pinned tool count in `verify_release_manifest` and `verify_feature_flag_admin`: update both in the same change.
- `tools/list` is filtered in `_LocMCPServer.list_tools` / `call_tool` only: toolsets (`LOC_STUDIO_MCP_TOOLSETS`, `X-Loc-Toolsets`, `?toolsets=`; registry `toolsets()`) and the key's scopes from `GET /api/account` `scopes` (cached 60 s per key; `LOC_STUDIO_MCP_SCOPE_FILTER=off` lists all). A new tool joins its family toolset automatically; a new connector or admin tool must be added to `_CONNECTOR_TOOLS` / `_ADMIN_TOOLS`, and a new toolset name to `app/cli/cmd_mcp.py` `TOOLSET_NAMES` (`verify_mcp_tool_filter` pins both).
