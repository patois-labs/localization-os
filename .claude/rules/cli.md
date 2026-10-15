---
paths:
  - "app/cli/**"
  - "docs/CLI.md"
  - "loc-tms.cmd"
  - "loc-tms.ps1"
  - "tests/verify_cli_*"
---

# Headless CLI

Why: every feature ships on web and CLI in the same change (ADR 0011). Reference: `docs/CLI.md`.

- Every CLI leaf and flag must appear in `docs/CLI.md` (`verify_cli_documentation`, no exemptions); `DOC_EXEMPT` in `verify_cli_parity` stays empty.
- A new host-only verb (DB, keyfile, Alembic, host connector ops) needs an entry in `tests/verify_cli_parity.py`; remote mode exits 2 for it.
- Exit codes: 0 ok, 1 operation failed, 2 usage, 3 unreachable. Output printed to a terminal is ASCII only (`verify_cli_ascii`).
- Remote mode (`--remote URL --token lsu_...`) is org-scoped server-side; plain http is allowed only for loopback and the tailnet (`100.64.0.0/10`, `*.ts.net`), else `--insecure`. Never put the key on argv in examples.
- In-process CLI has no RBAC: it is the operator surface (org lifecycle is CLI-only behind `require_operator` / `LOC_TMS_OPERATOR_KEY`).
- `cli user role` refuses to demote the only admin; keep that guard in any role-changing path.
- Legacy `db restore` stages and verifies the backup, disposes pooled handles, and preserves the old DB/sidecars for rollback (`app/cli/legacy_restore.py`). Stop all database users first; never suppress sidecar move errors.
- `--secret name=-` reads ONE line from stdin (engine create/update); an empty line is a usage error (exit 2). It used to store a literal "-".
