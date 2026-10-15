---
paths:
  - "app/web/api*.py"
  - "app/web/system_prompts_api.py"
  - "app/web/workflows*.py"
  - "app/web/deps.py"
  - "app/web/__init__.py"
  - "app/web/_routes.py"
  - "tests/verify_project_scoping.py"
  - "tests/verify_connector_isolation_gaps.py"
  - "tests/verify_router_slice_guard.py"
  - "tests/verify_security_external_origin.py"
---

# Web and /api routes

Why: ADRs 0010 (isolation 404 before 403), 0011 (four surfaces).

- `app/web/api_cli.py` splices source routers by fixed `routes[a:b]` slices: inserting a route mid-slice silently drops a neighbour. Append new routes at the END of their router. A new `api_cli_*.py` exposing `router` must be registered in `_SRP_SOURCES`; the import-time guard fails loudly otherwise (and after FastAPI internals change).
- Isolation order: the org/customer/project ring check runs BEFORE the role/admin gate. A foreign id must answer byte-identically to a missing id (404, same body, same timing); only in-ring callers see 403. Only `_ai_block` (id-independent) may precede the ring check.
- Workflow project visibility: web `_wf_or_404` and API `_wf_in_org` require project visibility whenever `project_id` is set, regardless of `scope`. Keep lists, group variants/counts, and pickers aligned through the shared access authority.
- Every new route goes in `verify_project_scoping` (guarded, or exempt with a reason); `_WF_SWEEP_ALLOWLIST` matches exact paths only.
- Adding a GET to `/utcp` changes `verify_security_external_origin`'s pinned URL list; adding any route bumps the kernel facade / `features.CORE_API` / branding snapshot / `verify_cli_parity._CORE_EXPECTED` / MCP `EXEMPT` ratchets by exactly one.
- Literal routes register before parameterised siblings (`/workflows/building-blocks/system-prompts/...` before `/workflows/{workflow_id}/...`).
- `/api` has no session and no CSRF: never read `request.session` there. Doors that build identity via `deps.api_identity` (not `_gate`) must set the customer ring explicitly (`deps._ident_customer_id`).
- AI spend (cost fields) is manager+ data (`reports.can_see_cost`): never put cost on a payload a translator can reach (SSE streams, image stats, CLI JSON).
- Ship each feature on web UI + /api + CLI + MCP + help + `llms.txt` in the same change.
