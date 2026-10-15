---
paths:
  - "app/services/connectors/**"
  - "app/web/connectors*.py"
  - "app/services/egress.py"
  - "integrations/airtable-*/**"
  - "integrations/jira-forge-app/**"
  - "tests/verify_connector*"
---

# Connectors

Architecture: `docs/architecture/connectors.md`.

- NEVER trigger a connector run or enable a sync schedule on an install that holds real credentials: it spends real money and writes to real third-party systems. Test with a throwaway `LOC_TMS_DATA_DIR`.
- Jira search must use `/rest/api/3/search/jql` (the legacy endpoint answers 410). The Confluence gateway path has NO `/wiki` for v1 (`/ex/confluence/{cloudid}/rest/api`); v2 sits under `/wiki/api/v2` there and is unverified live (`_GW_V2`). Confluence ids and space keys are ASCII-only before they reach a URL (`\d` matches Arabic digits).
- Egress guard is default ON and resolve-and-pins hosts (SSRF); keep `reject_unwrappable_query()` on query builders.
- Loopback (`localhost`, `127.0.0.0/8`) is ALWAYS blocked and cannot be allowlisted: live-QA sandboxes run on a private address (for example a VPN address) added to `egress_private_allowlist`.
- A held-back (NEEDS_REVIEW) unit must never ship: no created variant with missing or source-language fields, no content hash, retry after approval (`flow.py` stage 9); a pair written incompletely also loses its OLDER stored hash (`FlowCtx.hash_drop`). Tests that assert otherwise are wrong.
- OAuth refresh is single-flight; never add a second refresh path.
- Connector isolation: a foreign id answers exactly like a missing one (404 before 403, identical body); `verify_connector_isolation_gaps` enforces it.
- Any copy of production data must boot with `sync_scheduler_enabled=0`.
- Any caller-supplied base, table, project or target must be scope-checked against the connection's configured values; only admins may retarget a connection that has a stored credential (a supplied token counts only on token-mode connections, never OAuth; see `store_common.py::_airtable_retarget_error`).
- Jira `jql` and Confluence `space_key` are admin only on a credentialed connection on a credentialed connection's scope. They compare case-sensitively (`SCOPE_CASE_SENSITIVE`); blanking one is a change. Confluence `cql` is in the set too: a cql-only scope is the whole scope.
- Notion: Notion-Version 2026-03-11 (`in_trash`, not `archived`); ids go through `notion.canon_id` (ASCII hex or a notion.so URL, never a free string in a path); the root is a page (its child pages, `source_depth`) or a database (rows through `/data_sources/{id}/query`); a page outside the root answers like a missing one. A target is always a CHILD PAGE (never a data-source parent). Notion has NO draft state: writes are live; `fill_target` stores a read-back fingerprint (`notion_target_prints` in config) and refuses to overwrite a translation page edited in Notion (reported `edited-in-notion`, then PARKED per source hash in `notion_edited_src`: no MT until the source changes or the page is trashed). The seal is dropped BEFORE the first write (write-ahead) so a killed run's doubled page converges, and restored when a failed write left the page as sealed. A body update appends the new blocks first and trashes the old ones after (never empty); a failed first fill trashes the page it just created. The fingerprint keeps every reader-visible field (colours, links, settings, order) but not ids, timestamps, signed file URLs or a mention's live text. Notes that a success message cannot carry (blocks not copied, mentions kept as text) go through `connector.write_notes`.
- Scope-defining and live-edit config fields are admin only via `store_common.scope_admin_error` (`SCOPE_ADMIN_FIELDS`, `LIVE_EDIT_ADMIN_FIELDS`; ServiceNow knowledge_base/kb_category/update_published, Notion parent_page_id). Add a new kind's scope keys there (Jira jql, Confluence space_key and cql).
- Project automation (`app/services/project_automation.py`, docs/architecture/project-automation.md): one connection feeds many projects through a private lane (a Connection whose config has `lane_of`, hidden by `list_connections`; hashes, watermarks and the id map stay per lane, so never share them). `flow.py` asks `run_ctx.claim()` before touching a record and `run_ctx.full_pull()` before trusting a stored hash: a new flow-engine kind inherits both, a loop outside the engine (airtable, jira, git) must not take claims. Deleting a connection deletes its lanes, bindings and claims (`store_common_leaf.delete`); the project delete cascade drops the binding too.
- Connected projects (ADR 0088): a bound project's TargetLocale list drives runs; dedupe locales by `languages_catalog.normalize` but keep each connection's vendor spelling in its config (Akeneo rejects `fr-FR` for `fr_FR`). `store_part2` update/patch_config hashes are pinned by verify_connector_config_writers: re-pin only after reviewing the credential-host, secret-clearing and SSRF invariants.
