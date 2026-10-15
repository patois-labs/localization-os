# AGENTS.md

Guidance for AI coding agents and human contributors working in this repository. It holds the rules that
cost real time when missed; everything else is found just in time from the map below. `CLAUDE.md` points here.

## What this is

Localization OS is a machine-translation-first translation management system: FastAPI, Jinja2 and vanilla
JavaScript over SQLModel, SQLite by default and Postgres supported. Logic lives in services; the web, CLI and
MCP server are thin shells over them. Licence: Apache 2.0.

| Path | What is there |
|---|---|
| `app/services/` | Business logic (translation, workflows, connectors, quality, spend) |
| `app/web/`, `app/cli/` | The web UI and API, and the headless CLI (`loc-tms`) |
| `app/templates/`, `app/static/` | Jinja2 pages and vanilla JS |
| `integrations/mcp-server/` | The MCP server (a separate package that talks to the HTTP API) |
| `integrations/` (others), `tools/vendor_studio/` | Companion apps and the vendor-side test bed |
| `alembic/` | Forward-only migrations |
| `tests/verify_*.py` | One standalone suite per script; `tests/test_verify_suites.py` is the only pytest entry |
| `docs/architecture/` | One doc per module; `docs/decisions/INDEX.md` lists the ADRs |
| `scripts/` | Test selection (`suite_owners.py`), pregate, lock tooling, `bootstrap.py` |

## Set up and run

```bash
python scripts/bootstrap.py          # creates .venv, installs the hash-pinned locks, copies .env.example to .env
python run.py                        # http://127.0.0.1:8000
```

The default engine in `.env.example` is `mock` (free, offline, deterministic), so the app works with no API
key. Switch `DEFAULT_PROVIDER` once you have added a real key. Python 3.10 or newer; CI and the container use
3.13. By hand: `pip install --require-hashes -r requirements.lock -r requirements-dev.lock` (dev), or
`pip install -r requirements.txt` for a runtime-only install. Restart `run.py` after multi-file edits.

## Testing rules

Full detail: `.claude/rules/tests-and-pregate.md`. (`.claude/rules/*.md` are path-scoped notes for
contributors, readable by people and AI tools alike; `.claude/hooks` holds an optional lint hook for Claude Code users.)

- Run suites only through the wrapper: `python -m pytest tests/test_verify_suites.py -k "<expr>" -p no:cacheprovider`.
  Never hand a `verify_*.py` to pytest directly (fake failures), and never run the wrapper with an empty `-k`
  (it runs every suite, over an hour).
- Pick suites with `python scripts/suite_owners.py --files <changed files>` (or `BASE..HEAD`), then add every
  ratchet suite that reads a file you touched. Check what your `-k` collected.
- Before a pull request also run the global checks: `verify_kernel_facade`, `verify_cli_scope_classification`,
  `verify_ruff`, `verify_docs_architecture`, `verify_cli_parity`, `verify_mcp_parity`, `verify_nullability_selfheal`. CI runs them on every
  pull request, and the full set nightly in 8 shards (`LOC_TMS_SUITE_SHARD=i/8`).
- Write the failing test first. A test that passes with and without the fix is not a gate.
- Test output is ASCII (a cp1252 console must not crash). Suites run offline against a throwaway data dir and
  the mock engine.
- Lint with `ruff check .` and `ruff format --check .` (a post-edit hook in `.claude/` does this for agents).
- Files are LF (`.gitattributes`). On Windows never use `pathlib.write_text` on a repo file (it writes CRLF and
  turns byte-reading suites red); use `write_bytes` or an editor.
- Never use `git stash` in a shared worktree setup; commit work in progress instead.

## Never touch a real install

Suites and tools set a throwaway `LOC_TMS_DATA_DIR`. Do not point the app, a script or a test at a data directory
that holds real work, and do not run connector syncs or machine translation against real third-party systems
from development. Never set `LOC_TMS_MOCK_FALLBACK` on a real install: it makes a keyless engine silently return
mock text. Use `scripts/live_smoke.py` for a deliberate, tiny real-provider check.

## Invariants that bite

- Four-surface parity (ADR 0011): a feature reaches the web UI, the API, the CLI and the MCP server together,
  with help text and the `llms.txt` descriptor. `verify_cli_parity` and `verify_mcp_parity` enforce it.
- Isolation: a foreign id answers exactly like a missing one, 404 before 403 (ADR 0010).
- Untrusted text (customer files, vendor replies, memory) is fenced in prompts and marked in tool results
  (ADR 0029, 0075); it is data, never instructions.
- All spend goes through one funnel (`run_completion`; ADR 0030, 0042). Spend tools plan first and confirm second.
- Alembic is forward-only and has exactly one head; new tables are classified in the data export.
- A feature switched off answers 404 (the kill-switch law), not 403.
- Product names, headers and URIs come from `app/branding.py`; `verify_branding` pins the exceptions.
- No em dashes in anything a person reads (ADR 0021); ASCII in anything printed to a terminal.
- Ratchet allowlists shrink only. Tests that pin a count change in the same commit as the feature.

## Path-scoped rules and decisions

- `.claude/rules/*.md` load when you read a matching file (routes, auth, CLI, MCP, connectors, formats,
  migrations, templates and accessibility, spend, prompts, tests, build and deploy). Read the one for the area
  you are changing.
- `docs/decisions/INDEX.md` lists the architecture decision records; `docs/architecture/` has one doc per module.
- Read a file before editing it, and prefer small changes that keep the owning suites green.

## Contributing

Sign off each commit (`git commit -s`, the Developer Certificate of Origin), keep ruff clean, run the owning
suites plus the global checks, and fill in the pull request template. See `CONTRIBUTING.md`.
