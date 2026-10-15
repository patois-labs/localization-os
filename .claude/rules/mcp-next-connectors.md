---
paths:
  - "integrations/mcp-server/**"
  - "app/web/background_calls.py"
  - "app/services/connectors/mcp_*.py"
  - "tests/verify_mcp*"
  - "tests/_mcp_plan.py"
---

# MCP server: plan ids, external content and the dispatcher

Why: ADRs 0074 to 0080.

- Spend confirms need the plan's `plan_id` (ADR 0079, BREAKING). A test that confirms a spend through the dispatcher must plan first (`tests/_mcp_plan.with_plan`); a new spend tool needs `PLAN_ID_PARAM: PLAN_ID_SCHEMA` (verify_mcp_plan_files P1). Scripts outside the repo that confirm without a plan break on the same rule.
- `server.py` `_dispatch` is touched by many changes; keep this order in it: pop plan_id, plan, validate the idempotency key BEFORE redeeming the plan, mark results with `external_content.mark`, but return the MRTR InputRequiredResult unmarked and outside `_structured`.
- A new write tool is destructive until reviewed into `tool_meta.ADDITIVE_WRITE_TOOLS` (verify_mcp_parity check 11). A new typed output field goes in `tool_meta.TOOL_OUTPUT_PROPERTIES`.
- External text is marked by default; only `OWN_TEXT_TOOLS` opt out. Vendor errors go through `vendor_text` (redact BEFORE the cap).
- Slow launches that may hit an install WITHOUT background calls (a production install with background calls off) keep the 210 s read timeout.
