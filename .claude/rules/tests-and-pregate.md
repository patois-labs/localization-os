---
paths:
  - "tests/**"
  - "scripts/pregate*"
  - "scripts/suite_owners.py"
  - "scripts/srp_*"
---

# Tests, suite selection and pregate

See also `CONTRIBUTING.md` and `AGENTS.md`.

- Run suites ONLY through `tests/test_verify_suites.py -k ...`; a `verify_*.py` handed to pytest directly gives fake failures. Each suite sets its own throwaway data dir and clears provider keys, which the wrapper does for you.
- Never run the wrapper with an EMPTY `-k`: it runs every suite (well over an hour). To split a full run across machines set `LOC_TMS_SUITE_SHARD=i/n` (for example `3/8`); `-k` still applies on top.
- Build `-k` from `scripts/suite_owners.py` (`--files ...` or `BASE..HEAD`), then add every ratchet suite that reads a touched file; check what the filter collected (`-k prompt_authoring` misses `verify_qa_7_8_10_authoring`).
- suite_owners also maps goldens, fixtures, Dockerfile, docker-entrypoint.sh, alembic/, tools/, requirements* and pyproject.toml to the suites that name them (any edit counts). A changed one that no suite names makes pregate FAIL (`UNOWNED <path>`); excuse it in the brief with `owners-unmapped: <path> reason`.
- `scripts/pregate.py <clone> <base-ref> [brief]` is the offline pre-merge gate. It runs with the Python that launches it (activate your virtualenv first) and writes its records to the gitignored `.pregate/` folder. The brief is optional: it reads the FIRST `-k "..."` after a `## Suites` heading, and without one runs a default trio. "Suite owners" MISSING lines go into that line.
- Pass pregate a base SHA, not a branch: it resolves the ref twice and a mid-run commit fails it.
- Suites set `LOC_TMS_DATA_DIR` and clear provider keys; never set `LOC_TMS_SERVER` (only `verify_docker_auth` does). Test `print()` output is ASCII (cp1252 console).
- `tests/_piece_patch.py` filters on `hasattr`: patching a renamed or deleted name silently patches nothing. Re-check patch targets after any module split.
- Suites that read source as BYTES (e.g. `verify_lqa_adjudication` reads `orchestrator.py`) go red if a file was rewritten with CRLF; never `pathlib.write_text` a repo file on Windows.
- Never `shutil.which("bash")` in a suite: on Windows it can hit the WSL stub in System32 (skip System32/WindowsApps like `verify_docker_saml`).
- Timing suites lie on a loaded machine: `verify_stress_liveness_pool`, `verify_feature_off_matrix` (180 s budget), `verify_key_paging_scale`. Judge them ALONE on an idle machine; waive in the brief with `owners-waive: <suite> <reason>`.
- Ratchets: allowlists shrink only. Adding a settings key updates `tests/branding_contract_snapshot.json` via `tests/gen_branding_contract_snapshot.py`; a new table is classified in `app/services/data_export_part2.py` and bumps the `verify_data_export` count.
- Secret scan: a credential-like word followed by `:` or `=` fails even in strings, comments and Markdown; `# pregate: allow-secret <reason>` must be on the SAME line; any `lsu_` literal of 8+ chars fails. Keep fixture passwords in named constants. Build vendor key prefixes (`glpat-`, `sk_live_`) at runtime so external scanners (gitleaks) stay quiet.
- Write the new test against the PRE-FIX tree first and see it fail; a test that passes either way is not a gate.
- Work in your own checkout or worktree and confirm the path before every edit, never in a checkout that serves a running install.
- Suites not owned by the files they test: `verify_api_r2_admin_identity`, `verify_api_r3_config_credentials` and `verify_apikey_expiry_admin_scope` act as an org admin under multi-org or fake an empty install. Run all three whenever host-only settings (`ORG_ADMIN_REFUSED_EXTRA`, `security_setting_keys`) or first-run setup change.
- `verify_glossary_write_parity` is owned by nothing: it pins the glossary compat WRITE output (`app/services/glossary_hub.py`, reached through `glossary.import_bytes`) against `tests/glossary_write_parity_golden.json` without ever naming that module, so suite_owners cannot route a `glossary_hub` edit to it. Run it on any glossary import/write change. If a reviewed change INTENTIONALLY moves that write output, re-capture the golden from the tree that already carries it (`tests/capture_glossary_write_parity.py`) in the same change, per the suite's GOLDEN PROVENANCE note.
- Known slow under load (can pass the 600 s wrapper limit on a busy machine; rerun alone before calling red): `verify_docker_image`, `verify_kernel_facade`, `verify_session_timeout`, `verify_api_rbac`, `verify_totp_mfa`.
- Agent scratch inside a clone (`scratchpad/`) is scanned by `verify_branding`: move it out before pregate.
- Page tests must anchor on element ids, not on visible text: section navs repeat headings, so `split("<text>")` hits the nav.
- suite_owners misses suites that quote non-.py files (help.html, templates) and new JS files: after template or JS changes also run verify_a11y_contact, verify_a11y_browser, verify_connector_oauth, verify_tap_viewer and every verify_ux_* suite.
- Template macro tests must include Undefined, None and number labels: a fix once crashed Prompt Tap on an undefined label because its fixtures never had one.
- Suite speed: a suite's fixed cost is the app import (a few seconds), not its database (test mode builds the schema with create_all in under 0.1 s). `verify_lazy_imports` keeps openpyxl, python-docx, pypdfium2, PyJWT and numpy out of `import app.main` and `python -m app.cli --help`: import such a library inside the function that uses it. A drain or wait loop must never wait on rows the test planted itself (they never move).
- Global checks on every retest and in every gate: verify_kernel_facade, verify_cli_scope_classification, verify_ruff, verify_docs_architecture, verify_cli_parity, verify_mcp_parity. They catch cross-change reds that every change's own owner list misses; CI runs them on each pull request.
- Tests that pin a count (MCP tool count in verify_release_manifest / verify_feature_flag_admin, route and CLI ratchets, data-export table counts) are updated in the same change as the feature.
- Suites that need Docker (`verify_db_verify`, `verify_deploy_security`, `verify_docker_image`, `verify_docker_saml`, `verify_image_scanning`, `verify_nightly_backup_526`, `verify_release_lock`), a Windows host (`verify_prompt_tap`, `verify_test_instance`, `verify_request_limits_fix2`, `verify_review_editor_layout`, `verify_workflow_step_tabs`) are deselected from the nightly shards; run them where the tool exists when you touch what they cover. The browser suites need a local Chrome.
- The harness turns an inherited `PYTHONDONTWRITEBYTECODE=1` (agent shells, pregate) into an out-of-tree bytecode cache (`PYTHONPYCACHEPREFIX=<temp>/locos_pycache`) for suite children, so a clone stays free of `__pycache__` without recompiling the app in every suite.
