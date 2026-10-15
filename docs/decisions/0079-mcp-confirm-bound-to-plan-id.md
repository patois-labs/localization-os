# 0079. MCP spend confirms are bound to a plan_id (BREAKING)

Status: Accepted
Date: 2026-10-11

## Context

#757: a model steered by injected text could skip the plan step and call a spend tool with `confirm=true`
directly.

## Decision

- An MCP spend tool's confirm must carry the `plan_id` its plan returned; the plan is bound to the caller (token
  fingerprint) and redeemed once. No deprecation window: accepting a bare `confirm=true` with a warning keeps the
  hole open.
- With an idempotency key (ADR 0076): a used plan re-presented with the SAME key passes, so a retry after a lost
  answer gets the deduplicated result; another key or none is refused.
- Optional human confirm through MRTR (`LOC_STUDIO_MCP_HUMAN_CONFIRM=ask`).
- MCP only: /api, web and CLI spend paths are unchanged.

## Consequences

- BREAKING for MCP clients that confirm directly: they must now confirm with the `plan_id` from the plan. The skill files in the repo were updated; scripts and skills kept outside the repo must be updated too.
- An OAuth token refresh between plan and confirm refuses the confirm; the agent plans again.
