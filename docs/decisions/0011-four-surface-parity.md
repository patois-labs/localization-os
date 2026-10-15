# 0011. Every feature ships on web, CLI, API, MCP, help and the LLM descriptor together

- Status: Accepted
- Date: 2026-07-03 (web/CLI parity); 2026-08-03 (MCP first-class plus UTCP); 2026-08-27 (four surfaces)

## Context

Customers drive Localization OS from the browser, from scripts, and from AI agents. A feature that exists
on one surface only is invisible to the others and drifts.

## Decision

- Logic lives in `app/services/*`; every capability ships in the same change on the web UI AND the
  headless CLI (`tests/verify_cli_parity.py`), a working `/api` endpoint, an RBAC-mapped MCP tool, in-app
  help/docs, and the LLM descriptor (`llms.txt`, UTCP `/utcp`, read-only and additive to MCP).
- Exceptions: cosmetic UI; identity- or graph-bearing surfaces (export routes) are `NEVER_COVER` in MCP;
  host-only operations (DB, keyfiles, Alembic) are CLI in-process only.

## Consequences

Each new route bumps several ratchets (kernel facade, `features.CORE_API`, branding contract snapshot,
CLI parity, MCP exemptions, `docs/CLI.md`). See `.claude/rules/api-routes.md` and `cli.md`.

## Links

- docs/architecture/surfaces-and-parity.md
