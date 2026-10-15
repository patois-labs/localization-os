# 0074. MCP tools carry truthful annotations, titles and an output schema

Status: Accepted
Date: 2026-10-11

## Context

An MCP interop review found no tool metadata that agent hosts
use for permissions. #749, #750, #752, #756.

## Decision

- Every tool has a title and annotations. A write tool is destructive unless it is reviewed into
  `tool_meta.ADDITIVE_WRITE_TOOLS`; no write tool claims idempotent; readOnlyHint only on the read tier.
  `verify_mcp_parity` check 11 enforces it.
- Every tool declares an outputSchema; `structuredContent` must conform, otherwise it is sent as `{"value": ...}`.
  The text block is byte-identical to before (older clients).
- serverInfo carries name, title, website, MAJOR.MINOR version; server instructions carry rules only.
- Errors end with a JSON tail from a fixed code vocabulary (with `retry_after_s`, `required_scope`); no paths,
  tracebacks or secrets.

## Consequences

- A new write tool is destructive until reviewed. A new typed output field goes in
  `tool_meta.TOOL_OUTPUT_PROPERTIES`; keep typed fields few.
- Not done: real per-tool output schemas from API response models; resource_link for big inline exports.
