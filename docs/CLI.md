# Localization OS TMS - Headless CLI

A small, dependency-free command-line interface for operating an install without
the web UI: schema/migrations, backups, DB encryption, settings, and first-admin
bootstrap. Built for headless/container/CI provisioning.

Remote role requirements: upload and MT writes require translator or higher;
Ledger reads require admin. Customer preferences, defaults, context cards and language
pairs require manager, linguist or admin. Document context overrides and context A/B
require linguist or admin. Customer 360 summaries include only visible projects.
The API key issuance endpoint mints only for its caller and refuses admin/SCIM grants;
use the trusted local operator path to provision other accounts.
Airtable record deletion requires an admin per-user key over the API and a configured
base on every surface. Shared connector credentials cannot delete records.

Inviting an active account is refused on CLI, API and web. Use `user reset-password`
or the Users page's Reset password action instead. Self-service password changes and MFA
enrollment are on the Account page: local and LDAP accounts re-enter their current password,
with a shared five-attempt limit and a 15-minute lockout. MFA setup does not show its key
until that check passes. Turning MFA off also requires the current password and a current
code. API/CLI offer MFA status and privileged reset only; they cannot enroll a factor.
LDAP proof uses a directory bind within the same attempt budget. Pure SSO/SAML/OIDC
accounts use provider MFA and cannot enroll or disable a factor here.
MCP deliberately excludes user-administration routes, including invitations and MFA.

Entry point: **`python -m app.cli`** (always available, no install step). Repo-root
wrappers forward to it, preferring the project virtualenv's Python:

```powershell
# Windows PowerShell
.\loc-tms.ps1 <command> [args...]
```
```cmd
:: Windows cmd.exe
loc-tms.cmd <command> [args...]
```

**Optional - a real `loc-tms` command.** `pip install -e .` (from the repo root)
registers a `loc-tms` console script (via `[project.scripts]` in `pyproject.toml`)
so you can drop the `python -m` / wrapper prefix:

```bash
pip install -e .        # once, in the project venv
loc-tms health          # -> the same in-process CLI
```

This is purely a convenience: the packaging table is **dependency-less** (runtime
deps stay in `requirements.txt` / `requirements.lock`, which is what CI and Docker
install), so `python -m app.cli` and the wrappers work identically with or without
the editable install.

Every command and subcommand has `--help`.

---

## Local engine base URL policy

`config set local_base_url URL` and `config apply` validate the same fixed deny set
as Setup AI, `/api/admin/config` and MCP `manage_install` (`set_config`). Empty
means not configured. HTTP/HTTPS loopback, LAN and tailnet URLs are allowed;
metadata, link-local, unspecified, multicast and reserved IPv4 targets are refused
and audited. Calls and probes validate again before selecting direct or tailnet
SOCKS routing. Unresolvable LAN names remain allowed for offline installs.
Alibaba metadata `100.100.100.200` overlaps allowed tailnet CGNAT; the sidecar wins
as an accepted risk. See [the exact deny set](architecture/secrets-and-crypto.md#local-engine-egress-door).

## The MCP server - a sibling surface for agents

The CLI is one of Localization OS's surfaces; the **MCP server** is another, aimed at
an autonomous agent (Claude Desktop, Claude Code, Codex, your own runtime) rather
than a shell script. It is a thin HTTP client of the same `/api` and packages the
product as **72 task-shaped tools** in 7 families, with a two-phase spend contract
and zero destructive tools. It runs over stdio by default, or streamable-HTTP with
`--transport streamable-http` for a runtime you control behind your own proxy.

Anything you can do here you can do there and vice versa where the route exists -
they are two front doors on one `/api`. Full setup (Claude Desktop / Claude Code /
remote), the scope-minimalism guidance, and the tool catalogue live in
**`integrations/mcp-server/README.md`**; the design is in
`docs/specs/mcp-server.md`.

---

## Security model - read this

The CLI runs **in-process by default**: it imports the app's services and opens
the database directly (exactly like Alembic), so it works **with no server
running**. This is a **local-root** operation at the same trust level as editing
the SQLite file with the `sqlite3` shell - the operator already holds the DB and
the `ENCRYPTION_KEY`, so there is **no login and no RBAC**. Audit rows are
attributed to the actor `cli` (override with `--actor <email>`). Destructive
commands prompt for confirmation; pass `--yes` to skip (required in
non-interactive / `--json` / `--quiet` contexts).

**Stored settings are applied at bootstrap.** Before any command runs, the CLI
pushes the stored Settings (the Setup AI page / `config set` values - default
provider, per-provider model, local base URL/key/model, in-app cloud keys) into
the runtime config, exactly as the web server does at startup. So a headless
local-engine translate resolves the same model + base URL and engages the same
per-family prompt adapter as the web path - it does **not** fall back to bare env
defaults. A fresh install with no `setting` table yet (`init` / `db init-schema`)
skips the push cleanly. **Consequence:** this also activates any DB-stored in-app
cloud key (Setup AI / `providers key`) on the CLI/headless path - so blanking the
`ANTHROPIC_API_KEY` / `GEMINI_API_KEY` / `OPENAI_API_KEY` **env** vars no longer
prevents cloud spend from a CLI run when a key is stored in the database (a stored
key overrides the env, exactly as it does for the web server). To run a CLI batch
with no cloud spend, use a local/mock provider or clear the stored key.

### Remote mode - `--remote URL --token lsu_…`

`--remote URL --token lsu_…` turns a **remote-capable** command into a thin HTTP
client of a running server's `/api`. It sends the same intent over HTTP that the
in-process arm runs locally and prints the **identical** output shape (human table
/ `--json`), so behavior is transport-independent.

- **Org scoping is done entirely server-side by the token.** A per-user `lsu_` key
  resolves to its owner's **HOME org**, with no account type exempt (there was a
  super-admin until 2026-09-20; it never widened `/api` scope either, and it no
  longer exists). Remote mode therefore reaches exactly one org and can
  only ever see/touch what the token can: whatever the token's org holds.
- An unreachable server → exit **3**; a `/api` 4xx/5xx (e.g. a cross-org 404) → exit
  **1** with the server's message.
- **Not-found errors differ by transport (contract).** In-process, a missing id is a
  specific, local message at exit **2** (e.g. `no such TM entry: 5`). Remote, the same
  miss is the server's generic pass-through (e.g. `server: not found`) at exit **1** -
  the client does not re-specialize `/api` errors. Branch on the exit code, not the
  message text, for transport-independent scripts.
- **Some `/api` errors carry a stable `code`.** A structured, machine-readable `code`
  field rides beside `error` on certain refusals (today the keys-export image-cap
  refusal, `code: "key_image_export_max"`). Over `--remote --json` it is merged into the
  error object, and the remote arm re-classes that one refusal to **exit 2** (matching the
  in-process arm, which now also emits the same `code`) off the structured code rather than
  a substring of the message. Other refusals omit it; branch on the exit code first.
- **Remote `--json` may carry extra envelope keys.** A remote response can include
  server-echo fields the in-process arm does not emit - e.g. `tm list --remote --json`
  adds a top-level `customer_id` echoing the scope the server resolved from the token.
  These are additive envelope metadata; the entry/row payloads themselves match.
- **The key comes from three places, in priority order (issue #264):** `--token`,
  then `--token-file PATH` (the file's first line, stripped), then the
  `LOC_TMS_TOKEN` environment variable. `--token` puts the key on argv, where it
  is visible in process listings and shell history, so the CLI prints a one-line
  stderr warning naming the two argv-free alternatives; in scripts and on shared
  hosts, prefer `--token-file` or `LOC_TMS_TOKEN`.
- **Plain `http://` is refused for open-internet hosts (issue #264).** The key must
  not cross an unencrypted path: an `http://` URL is allowed only for loopback
  (`127.0.0.0/8`, `::1`, `localhost`) and tailnet hosts (`100.64.0.0/10`,
  `fd7a:115c:a1e0::/48`, `*.ts.net`); anything else is refused with a clean error
  that names the fix, before any request is made, unless `--insecure` is passed.
  `https://` is always allowed. The client never forwards the key through a
  redirect: every 3xx is refused with a clean error, so a 3xx cannot move the key
  to a different scheme or host. The one exception (issue #329, following #317):
  `export download --remote` accepts the artifact route's single 302 to the
  export's storage URL and fetches it once, anonymously over `https://` to a
  different host: no key, no cookies on that request. Storage the CLI will not
  fetch that way (plain `http://` or a same-host S3-compatible store such as
  MinIO) is refused with a message pointing at the web UI download.
- `--remote` **requires** a key: `--token`, `--token-file PATH`, or
  `LOC_TMS_TOKEN`.

**In-process vs remote - the matrix.** Remote capability is decided **per leaf**, not
per group: a group is remote-capable exactly where an `/api` route exists for that verb.
**This table summarizes remote capability.** The per-command tables below carry the
details. `tests/verify_cli_documentation.py` checks every registered leaf and its
flags against those command sections and their explicitly linked shared helpers.

| Command group | Remote-capable (`--remote`)? |
|---|---|
| `tm`, `langset`, `content-type`, `account` | **yes**, every leaf |
| `keys` | **yes**, every leaf **except** `empties`, `deliver-approve`, `deliver-revoke` (the delivery empty-gate verbs - in-process only, `require_local`) |
| `style-guide` | **yes**, except `style-guide ab` (in-process only) |
| `formality` | **yes**, every leaf |
| `doc` | **yes**, except `doc rename` and `doc delete` (in-process only). `doc translate` ENQUEUES - poll `job status --remote` |
| `glossary` | **yes**, every leaf - including `glossary export` (the legacy per-project one, GET `/api/projects/{id}/glossary/export`) and `glossary module extract` (POST `/api/glossaries/{id}/extract`), both wired to their `/api` routes as of R-MED-11 |
| `project` | **mixed.** Remote: `create`, `list`, `show`, `leverage` (GET `/api/projects/{id}/leverage`), `add-locale`, `remove-locale`, `apply-langset`, `set-due`, `set-content-type`, `set-customer`, `set-providers`, `rename`, `delete` (R1a: POST `/api/projects/{id}/rename` and `/delete`; delete is admin-only + typed confirm, echoed server-side after a dry-run preview). Project automation is remote-capable too: `bind`, `unbind`, `automation show|set|pause|resume|handover`, `run`, `runs` (the `/api/projects/{id}/automation`, `/source` and `/runs` routes). In-process only: `status`, `author`, `export`, `archive`, `unarchive`, `set-ai` (the `/api/projects/{id}/archive` and `/unarchive` routes serve the web + remote-API surfaces, but the CLI verbs are not `--remote`-wired), and the whole `tm-sources` and `glossaries` families (these have `/api` routes as of R1a, but the CLI verbs are not `--remote`-wired either) |
| `customer` | **mostly in-process.** Remote: `create`, `list` and `pairs import` only. In-process only: `show`, `update`, `rename`, `delete`, `archive`, `unarchive`, `set-ai`, `set-default`, `pairs list`, `pairs add`, `pairs remove` |
| `workflow` | **mixed.** Remote: `list`, `show`, `run`, `resume`, `show-run`, the `batch` family (`show`, `rerun`, `retry-failed`), the **authoring** verbs `create`, `add-step`, `edit-step`, `delete-step`, `move-step` (SPEC-workflow-authoring-api - `can_tune_ai`; a server-side refusal exits **1**, not the in-process **2**), and (R1b) `clone`, `copy`, `author` (a spend path - refuses a keyless-mock authoring engine 409; the remote arm does a single apply, no `--auto-fix` loop), the `group` family (`create`, `list`, `show`, `add`, `set-default`) and the `prompt` family (`create`, `list`). In-process only: `rename`, `delete`, `author-prompt`, `group rename`/`group delete`, `prompt rename`/`prompt delete` |
| `template` | **yes** (R1b) - `create`, `list`, `show`, `apply`, `rename`, `delete`, the whole `/api/templates` family. `create`/`apply` are `can_write`; `rename` is `can_manage` (matching the web twin `POST /templates/{id}/rename`, which requires a manager role); `delete` is `is_admin`. The remote `list` mirrors `template list` (GLOBAL + your customer's; there is no `--all` arm over `--remote`) |
| `segprofile` | **yes** (R1b) - all 8 (`list`, `show`, `create`, `clone`, `rename`, `delete`, `set-default`, `assign`) over the `/api/segprofiles` family. `is_admin` (mirrors the web `/settings` gate); the SRX blob is write-only |
| `context` | **mixed** (R1b). Remote: `show`, `set-profile`, `set-document`, `ab` (`ab` is a spend path - mock-409, writes nothing). In-process only: `delete-profile`, `show-document` (no `/api` route) |
| `image` | **mixed** (R1b). Remote: `upload` (multipart) and `translate` (a spend path - refuses a keyless mock 409). In-process only: `list`, `show`, `render`, `export` (the `/api` reads exist but the CLI verbs are not `--remote`-wired) |
| `connector` | **mostly in-process.** Remote: `list`, `pull`, `push`, `sync` only. Everything else (`create`/`update`/`delete`/`test`/`browse`/`rotate`/`schedule`/`token-regen`/`retry`/`oauth`/the per-platform `*-translate` verbs) touches live credentials or host state |
| `job` run / status / list | **yes** (`run` is synchronous) |
| `report` run / kinds | **yes** (GET `/api/reports/run`, org-bound by the token) |
| `apikey list` | **yes**; `apikey issue` / `revoke` / `revoke-all` are in-process only |
| `routing` | **yes**, every leaf - full remote parity (`add`, `list`, `show`, `toggle`, `delete` each dispatch to `/api/routing/rules`, admin-only, org-scoped by the token). The feature-gated top-level twin of `admin routing rule …` below (see *Content routing*) |
| `admin` | **mixed.** Remote: `admin routing rule show`, `admin tick`, `admin audit export`, `admin audit verify`. Every other verb is in-process only. |
| `engine`, `providers`, `lang`, `user`, `org`, `member` | **no** - in-process operator only |
| `tagging` repair / verify | **no** - they sweep the host DB directly. Note the refusal message is their own (`"tagging repair is in-process only (no --remote)."`), not the generic one below |
| `prompt-tap` on / off / status / show / actions / show-action / export / clear | **mostly no** - the tap is a host-local observer of this process's own provider calls. Exception: `actions --summary` and `show-action --summary` (bodies-withheld metadata only) work over `--remote` via `GET /api/prompt-tap/actions[/{id}/summary]`; the raw call bodies (`show`, `export`, `--calls`) stay host-only |
| `config` | **mostly in-process.** Remote: `features`, `feature enable`, `feature disable` only (GET/POST `/api/admin/features`, admin scope + `is_admin` + `remote_admin_enabled`; an install-locked feature key is still refused over the wire). Everything else (`get`, `set`, `list`, `apply`, `export-env`) touches the host's own store and stays local |
| `db …`, `health`, `audit verify`, `init` | **no** - in-process operator on the host |
| `ask` | **no** - Ask has no `/api` surface by spec (SPEC-ask-draft non-goals), so it refuses `--remote` loudly (exit 2) rather than answering silently from the local host |

An in-process-only command run with `--remote` **refuses cleanly at exit 2** rather
than silently running against the local host - which is the guarantee that matters.
Most of them print `require_local`'s standard "not available over `--remote`"; a few
carry their own wording (`tagging` says "tagging repair is in-process only (no
--remote)."), so branch on the **exit code**, never on the message text.

### SaaS in-process operator gate - `--org` / `--operator`

Only relevant when **multi-org mode is ON** (`multi_org_enabled=1`) **and** you run
**in-process** (no `--remote`). Then a command that would sweep across orgs must be
scoped - mirroring the `retention purge-now --yes` precedent - so a customer or cron
on a shared SaaS host can never silently touch every org:

- `--org <id>` scopes the whole command to one org (and **stamps new rows** with it);
- `--operator` authorizes an **unscoped cross-org** run, backed by the
  `LOC_TMS_OPERATOR_KEY` environment variable verified against the stored
  `operator_key_hash` setting (SHA-256 hex; set it with
  `config set operator_key_hash <sha256hex>`).

Without either, an in-process command in multi-org mode is **refused (exit 2)**. With
the gate **OFF** (single-tenant, the default) this is a strict no-op: the in-process
CLI works exactly as today with **no `--org`** needed. In `--remote` mode the token
does the scoping, so `--org`/`--operator` never apply.

The gate is applied **centrally** before any verb body runs (`app/cli/__main__.py`,
`_central_scope_gate`), from one table that classifies every verb (`app/cli/_scope.py`):
**org-scoped** verbs (they touch tenant rows) need `--org` or `--operator`; **operator-only**
verbs need `--operator`, and `--org` does not unlock them; **install-wide** verbs (`db`,
`config`, `enterprise`, `lang`, `providers`, `ledger`, status readers, static catalogs) touch
no tenant rows and run as before. `verify_cli_scope_classification` fails when the parser has
a verb the table does not name, so a new verb must be classified in the change that adds it.

**Operator-only verbs (`--operator`, never `--org`).** The `org` group, `user org`,
`admin tick` and the `prompt-tap` readers cross every org and cannot be narrowed to one. A
verb that writes something every org shares cannot be scoped to one org either, so with
multi-org ON `--org` is refused (exit 2) and only `--operator` runs it: `admin retention
config` and `purge-now`, `admin srx enable|disable|upload`,
`admin ldap config`, `admin oidc config`, `admin lqa rulebook get|set` and
`connector token-regen`. Every other tenant-data verb honours `--org`: `query`,
`workflow group show`, `context show`, `lqa log|findings|edit|withdraw`, `account`,
`job run|status` (`job run` refuses `data_export` and the webhook-only connector payload
keys, and a connection, project or document of another org reads as "no such ..."),
and `connector prune-config --all`.

---

## Global flags

| Flag | Meaning |
|------|---------|
| `--json` | Machine-readable JSON on stdout (one object per command). |
| `-q`, `--quiet` | Suppress normal human output. |
| `-v`, `--verbose` | Print extra detail. |
| `--actor EMAIL` | Audit attribution (default `cli`). |
| `--remote URL` | Route a remote-capable command to a live server's `/api` (org-scoped by the key; no account type reaches past that scope). Plain `http://` is allowed only for loopback and tailnet hosts (`.ts.net`); any other host needs `https://` or `--insecure`. |
| `--token TOKEN` | An `lsu_` per-user API key (required with `--remote`; visible in process listings and shell history, so prefer the argv-free alternatives below). |
| `--token-file PATH` | Read the `lsu_` key from `PATH`'s first line, stripped (beats the `LOC_TMS_TOKEN` env var). |
| `--insecure` | Allow `--remote` over plain `http://` to a host that is not loopback or the tailnet (the key is then sent unencrypted; prefer `https://`). |
| `--idempotency-key KEY` | (with `--remote`) Send `KEY` (1 to 255 visible ASCII characters) as the `Idempotency-Key` of this command's paid call (and of `connector pull`, `push` and `sync`). Re-running the same command with the same `KEY` within 24 hours returns the first run's job or answer instead of spending again; the same `KEY` on a different request is refused. |
| `--org ID` | (SaaS in-process) scope the command to one org id + stamp new rows. No-op single-tenant. |
| `--operator` | (SaaS in-process) authorize an UNSCOPED cross-org run (env `LOC_TMS_OPERATOR_KEY` vs `operator_key_hash`). |

Global flags must precede the subcommand: `loc-tms --json config get org_name`.

**Exit codes:** `0` ok · `1` operation failure · `2` usage error · `3` remote
unreachable. A full-archive Postgres restore is PARTIAL and exits `1` even after
uploads and keyfiles have been replaced: the database has not been restored.
Automation must branch on the exit code and inspect `status: "partial"`,
`ok: false`, and `db_restore_required: true`; run the emitted `pg_restore` recipe
before starting the server. Complete restores exit `0` with `status: "complete"`,
`ok: true`, and `db_restore_required: false`.

---

## Commands

### Ops - `db …`, `health`, `audit verify`

| Command | What it does |
|---------|--------------|
| `db migrate` | Bring the schema up to date. SQLite → `init_db()`; Postgres → shells `alembic upgrade head`. |
| `db init-schema` | Create/upgrade the SQLite schema via `init_db()` (create-if-missing + additive migrations). |
| `db backup <path> [--force]` | Consistent DB backup. SQLite: WAL-checkpoint + atomic `.backup` copy. Postgres: runs `pg_dump` if on PATH, else prints the recipe. Refuses to overwrite an existing `path` unless `--force` (same guard as `db encrypt`/`decrypt`). Also back up `DATA_DIR`. |
| `db backup --full <archive.tar.gz> [--force]` | **Whole-install** backup (gap G-06): ONE timestamped `.tar.gz` covering the DB **+** `uploads/` **+** the encryption keyfiles, with an embedded manifest (schema, app version, per-member sha256). Restorable on a blank machine. **The archive CONTAINS live secret key material** when secrets are keyfile-based - a loud warning fires; store it **encrypted** at rest (see docs/BACKUP.md). Local-only. |
| `db verify-backup <archive.tar.gz>` | **Restore-verification** control for a `--full` archive: manifest completeness, per-member sha256, `PRAGMA integrity_check` on a temp copy of the contained DB, and keyfile presence/non-emptiness. Exit nonzero on any failure. Read-only; local-only. |
| `db restore <path> [--yes] [--skip-verify]` | Replace the current install from a backup (**destructive**; confirm / `--yes`). Auto-detects: a `--full` `.tar.gz` archive restores the SQLite DB + uploads + keyfiles (double-confirmed; **verified before any write**; refused while the server appears running; extracted to a staging dir and swapped in with rollback attempted on failure; incomplete rollback reports affected paths; existing .pre-restore copies are preserved and block replacement until recovered or moved to secure storage; uploads replaced wholesale); a plain file restores just the SQLite DB (standalone source without sidecars; staged integrity check, CLI pool disposal, rollback-protected database/sidecar moves, and final integrity and SHA-256 verification; only if it passes the `SQLite format 3` header check). `--skip-verify` forces a desperate partial recovery from a known-damaged archive (dangerous). **Postgres: PARTIAL, exit 1**; uploads and keyfiles are replaced and the dump is extracted, but the database is NOT restored. Run the emitted `pg_restore` recipe before starting the server. JSON: `ok: false`, `status: "partial"`, `db_restore_required: true`. Complete SQLite restores keep exit 0 (`ok: true`, `status: "complete"`, `db_restore_required: false`). Local CLI only; no web/API/MCP restore action. |
| `db encrypt <src> <dst> [--force]` | Write a SQLCipher-encrypted copy of a plaintext SQLite DB (never in place; reuses `scripts/convert_db_encryption`). `--force` permits overwriting the destination. |
| `db decrypt <src> <dst> [--force]` | Write a plaintext copy of an encrypted DB. `--force` permits overwriting the destination. |
| `db recover-jobs` | With `job_queue_backend=db`, only ONE app process per database is supported; a second process with fresh in-flight leases is detected at boot and surfaced on `/health`, but not prevented. Queue recovery remains lease-only; heartbeat renewal is the F04 follow-up. An install-wide sweep: re-queue interrupted jobs through `jobs.recover_interrupted`. Heal plain interrupted runs through `orchestrator.recover_interrupted_runs` and its thread/activity guards. For orphaned batches, the liveness check requires a confirmed-dead local dispatcher; `batch_runs.recover_interrupted_batches` fails RUNNING/PENDING members, preserves PAUSED members, finalizes the batches and releases their delivery holds; live or unknown owners remain untouched. Batches with durable QUEUED members instead remain unfinished for admission; queued members are preserved. |
| `db protect-data` | Mark the data directory PROTECTED: the server refuses to serve it without `LOC_TMS_ALLOW_REAL_DATA=1`. Host-only. |
| `db verify` | Read-only DB **integrity/health** check: SQLite `PRAGMA integrity_check` + `foreign_key_check`, alembic head vs the DB's stamped revision, and portable orphan-FK counts (SQLAlchemy Core, works on Postgres too). Exit nonzero on any failure. Distinct from `audit verify` below, which checks the tamper-evident audit **hash chain**, not schema/data integrity. Local-only (no `--remote` arm - see the command's docstring for why). |
| `db scrub-scaffold [--apply] [--project ID]` | One-off remediation (core-QA Part 1 / Q10 cleanup) for content that echoed our **own prompt scaffolding** and was persisted as a translation **before** the scaffold-echo gate shipped. Scans `Segment` targets **and** stored TM (`SegmentTM` + `FullTextTM`) using the same single-sourced markers as the live gate (`app/services/scaffold.py`, with the abstain-when-the-source-carries-it rule). **Dry-run by default** - reports what it would touch and changes nothing. `--apply`: segments route to **needs_review + a scaffold `qa_flag`** (target text kept so a reviewer sees the leak; idempotent - an already-flagged segment is re-reported, not re-written); poisoned TM rows are **deleted** (+ semantic embeddings invalidated), like `tm scrub-hedges`. Scans the whole DB unless `--project` narrows it; `--apply` is audit-logged. Local-only. |
| `health` | Ping the DB (`SELECT 1`) and report readiness - the CLI mirror of `/ready`. |
| `audit verify` | Verify the tamper-evident audit hash chain end to end; reports where it broke. Shares one service function with the scheduled monitor (`audit_chain_verify_hours`, default 24h). When audit logging is switched off it also prints an `AUDIT OFF:` line (an intact chain says nothing about rows never written; the hosted alert timer emails on it); `--json` carries `audit_enabled`. The exit code is unchanged. |
| `audit reset-anchor --yes --i-understand-this-erases-truncation-evidence` | See below. |
| `audit rotate-key --yes` | Host-only rotation; stop other writers first. Keeps every earlier key. |

`audit reset-anchor` is host-only recovery after a legitimate restore. It re-baselines
the database and filesystem witnesses, requires both confirmations, and records the reset.
Stop the server and all other writers, reset, then start the server. Reset refuses a
detected listener using the same check as `db restore` and `audit rotate-key`.
`audit rotate-key` records both signatures and keeps every earlier signing epoch for verification.
It refuses a detected server listener, using the same check as `db restore`.
Full backups include `.audit_key`, `.audit_epochs`, `.audit_head`, and `.audit_format` and restore
them with the database. Plain DB-file restores print a reset-anchor recovery hint;
they require the matching signing material. Reset can replace a corrupt witness and
records `witness_replaced=1`; missing keys must first be recovered from backup.
Legacy full archives without audit keys clear the target's audit trust files so the
key is re-derived from the restored session secret on startup; no reset is needed.

> **After a code update, run `loc-tms db migrate` before other commands on an
> existing DB.** A new build may add columns/tables; the web app backfills them
> automatically at boot (via `init_db()`), but the CLI's read commands
> **deliberately do not auto-migrate**. If you run the CLI against a DB last
> touched by an older build (i.e. you haven't restarted `run.py` since pulling),
> a read command stops with a friendly hint - *"your database schema is older
> than this code … Run: `loc-tms db migrate`"* (exit 1; `-v` shows the raw
> traceback). Run `db migrate` once and retry; it is idempotent and safe to run on
> every boot.

Successful full restores warn if recovery-copy cleanup fails. JSON includes
`leftover_recovery_copies` (empty when cleanup succeeds). After confirming the restored
install is healthy, delete the listed secret-bearing copies or move them to secure
storage before another restore. Conflicts are checked before the first swap.

### Config - `config …`

Set `app_base_url` to this install's external URL before registering IdP or connector
OAuth applications. It is the authority for every emitted external URL; the request
Host is the fallback when unset. Register `<app_base_url>/auth/oidc/callback` for OIDC
and `<app_base_url>/connectors/oauth/callback` for connector OAuth; Security and Help
render the resolved OIDC callback, and Security also renders the connector callback.
Explicit SAML SP EntityID/ACS settings win over app_base_url. Otherwise SAML metadata
and ACS use `/auth/saml/metadata` and `/auth/saml/acs` under that base. An HTTPS base
supplies SAML's external scheme/host/port behind a TLS-terminating proxy.

Recover a legacy invalid value with `config set app_base_url https://tms.example.com`
(or correct it on `/security`). Invalid stored values warn once and use the request
Host until repaired; background emails omit links. Writes normalize whitespace,
trailing slashes, and scheme/host case. If served under `/tms`, include `/tms` in
app_base_url: its path is the external prefix, replacing ASGI root_path.
Proxy trust is still required for forwarded client identity and audit attribution.

Thin, cache-coherent wrappers over `app.services.settings` - the **same store the
Settings UI writes**, so there is one source of truth. It is also the only way to reach
the **several dozen** keys that have a store but no Settings form today - among them
`mt_concurrency`, `api_rate_limit_per_minute`, `api_read_rate_limit_per_minute`,
`session_idle_minutes` and
`session_max_hours` (0..1440 minutes and 0..24 hours, default 240 and 24; both also on
the web Security page's Sessions card), the password policy for local accounts
(`password_min_length`, `password_require_classes`, `password_block_common`,
`password_history`, `password_max_age_days`; also on the Security page's Passwords card),
who must use two-step sign-in (`mfa_required` = `off` / `admins` / `all`, a sensitive key
that asks to confirm; also on the Security page's Two-step sign-in card),
`project_scoping`, `batch_max_parallel`, `workflow_max_visits`,
`workflow_total_step_cap`, `export_tag_damage_policy`, `key_autounverify`,
`key_changelog_retention_days`, `assist_provider` / `assist_model`,
`ai_tagging_enabled`,
`tag_llm_repair_enabled` and the module kill switches (`assist_enabled`,
`key_projects_enabled`, `image_localization_enabled`,
`vendors_enabled`, `reports_enabled`, `routing_enabled`) and the vendor rework cap
(`vendor_rework_max_rounds`, a whole number of reject-and-rework rounds per dispatch leg from
1 to 10, default 1; past it a reject escalates to a human).
`config list` is the authoritative inventory - do not treat any written list, including
this one, as complete.

Both API rate limits accept 0..100000 requests/minute. 0 disables a limit; corrupt stored
values use its default. Counters reset on restart and are separate per worker.

`accessibility_contact` (group `general`) is where people report an accessibility problem,
shown in the Accessibility section of `/help`: a plain email address (rendered as a mailto
link) or an https:// link, at most 254 characters, or blank for "Contact your
administrator". Anything else (a `javascript:` or `http://` link, markup, spaces around the
value) is refused on every write door. It is also on the web Security page's Accessibility
card and in /api config, with the same validator, and every change is audit-logged.

The **feature kill switches** have their own pair of verbs (`config features` to read,
`config feature enable|disable <slug>` to write) and are the one slice of `config` that
works over `--remote`, because the alternative doors are a browser (the Settings →
Features card) or a shell on the host. They are **deliberately not on MCP**: a feature
flag decides what the install is allowed to do, so it stays in the same excluded class
as config, credentials, identity and retention.

They are also the **only** flag door on the wire. `POST /api/admin/config` (and the MCP
`manage_install(set_config)` action that forwards to it) refuses every feature `setting_key`
with a 400 naming the feature route, so a flag cannot be flipped over the wire by
spelling its raw key - which is what makes `feature.toggle` the complete record of
remote flag changes. The **host-local** `config set` / `config apply` are unchanged and
still write a flag directly, recording a `config.set` row; the lock is on the wire, not
on the operator sitting at the machine.

| Command | What it does |
|---------|--------------|
| `config get <key>` | Print one setting. Secret keys show `(set)`/`(unset)`, **never** plaintext. |
| `config set <key> <value> [--yes]` | Set one setting (validated against known keys; unknown -> exit 2). Secret keys are stored masked; security-sensitive keys confirm first. `--yes` skips confirmation for security-sensitive settings. Changing `audit_enabled` or `audit_fail_open` is always written to the audit log, even when it turns logging off. |
| `config show` | Read configured private egress policy with source and validity. Supports `--remote`. |
| `config list [--group G]` | List settings, grouped, secrets masked. Groups: capacity, general, providers, spend, security, identity, connectors, smtp, segmentation, semantic, tm, retention, vendor_studio, features. |
| `config apply <file.toml> [--yes]` | Apply many settings from TOML atomically. All keys validated first (unknown -> reject the whole file). `--dry-run` shows the diff without writing. `--yes` skips confirmation for security-sensitive settings. |
| `config export-env` | Print a `.env` scaffold of the install-time env vars (secrets as `<secret>` placeholders). |
| `config features` | List the toggleable features + their on/off state (the CLI twin of the Settings → Features card). **Remote-capable** (GET `/api/admin/features`). The `notes` column flags a feature you cannot flip here: `moved` (its switch lives on `/settings/enterprise`) or `host-only` (its key is install-locked, so only the host may set it). |
| `config feature enable <slug>` | Turn one feature on by slug: the headless twin of the Settings -> Features card. Works in-process **and** over `--remote` (POST `/api/admin/features/{slug}`). A successful flip prints the same success line and the same `--json` object either way; **refusals follow each arm's own convention** (a remote 4xx is always exit 1 with the server's message, where the local arm may exit 2 - an unknown slug is exit 2 locally, exit 1 remotely). A same-state flip is an honest no-op (`no change`, no audit row). Refuses an unknown slug, a feature whose switch moved (SCIM/SAML -> `/settings/enterprise`), and, over `--remote` only, an install-locked key such as `data_export_enabled`, which stays a host write. |
| `config feature disable <slug>` | Turn one feature off. Same scope, access checks and local/remote refusal behavior as `config feature enable`. |

Example TOML for `config apply` (a flat table, or a single `[settings]` table):

```toml
mt_concurrency = "8"
api_rate_limit_per_minute = "240"
project_scoping = "1"
```

`login_trusted_networks` (a comma list of IPs/CIDRs, IPv4 /8 or narrower, IPv6 /32 or
narrower; blank trusts none) exempts shared-NAT sign-in sources such as a campus egress from
the 200-failure per-source budget only; per-account and per-name lockouts still apply (#411).
It is a sensitive key: `config set login_trusted_networks 203.0.113.0/24` asks for confirmation.
The same list is on the web Security page (Access control card). IPv4-mapped IPv6 entries
are refused (write them as IPv4); a stored list the validator would refuse trusts nothing and
is named in the startup log.

Ingress trust settings are local-only: `security_allowed_hosts` pins valid hostnames
without ports (blank accepts any valid Host); `trusted_proxy_ips` lists the proxy
Socket IPs/CIDRs trusted for forwarded scheme and client identity (blank trusts none).
List only verified proxy or edge peers; this enables X-Forwarded-For (or Fly-Client-IP
when `FLY_APP_NAME` is set). Do not include tailnet forwarders or an entire private network.
Set them on the host with `config set` or `LOC_TMS_SECURITY_ALLOWED_HOSTS` /
`LOC_TMS_TRUSTED_PROXY_IPS`. Local proxy trust changes apply live to identity and
scheme; environment changes require a restart. Local CLI writes still require
sensitive-key confirmation; `/api/admin/config` and MCP `manage_install(set_config)`
refuse these keys even with confirmation. Generic CLI config verbs refuse --remote.
There is no loopback exemption from the Host pin. The exact `/health` and `/ready`
probes may use any syntactically valid Host; they bypass only the Host allowlist.
Recovery is a corrected or empty
host override / config apply file, then restart. Invalid ingress overrides log
ERROR, resolve to empty without stored fallback, and appear in /health detail as
`ingress_config: invalid <key>`. Non-empty all-separator Host lists are invalid.
When `app_base_url` is configured and the effective pin is empty, `/health` detail
includes `"host_pin":"unset"`; otherwise that key is absent. Boot logs one WARNING
in the same condition. The default pin remains empty and is never auto-populated.
Non-empty Host list writes must include the configured `app_base_url` host,
case-insensitively and independently of port: add that host, or clear/repoint
`app_base_url`. Empty is always accepted by the local write door. For recovery,
run `config set security_allowed_hosts ""` from the host shell (outside the ASGI
boundary); clear any environment override too. API/MCP writes remain local-only refusals.
Every uvicorn launch uses --no-proxy-headers; the application boundary alone
handles forwarding with trusted_proxy_ips as its only proxy setting.
IPv4 prefixes shorter than /8 and IPv6 prefixes shorter than /32 are refused:
broad networks would restore caller-controlled audit identity.

Settings reads mask `local_api_key` as (set)/(unset). Setup AI leaves the password
field empty: blank retains the saved key, and Clear key removes it. A blank-key
probe reuses the saved key only for the saved destination (scheme and host ignore case).

### Enterprise features - `enterprise …`

The ONE door that turns the six flag-OFF, `REMOTE_IMMUTABLE` enterprise features on and
off - OpenTelemetry tracing, Prometheus `/metrics`, SAML 2.0 SSO, SCIM 2.0 provisioning,
PII named-entity redaction, and the durable DB job queue - plus the S3-compatible blob
store (chain/IRSA creds). The CLI twin of the `/settings/enterprise` web panel; both are
thin callers of `app/services/enterprise_enablement.py`. In-process = **local-root** (these
enable Settings are `REMOTE_IMMUTABLE`, settable only on the host), so every verb refuses
`--remote`. Each feature's prereqs are checked by a **preflight** that runs against the
proposed values *before* the write, so a missing library / path-less endpoint / absent
table is caught here instead of at the next boot (where the fail-loud `configure_at_boot`
check would refuse to start). **All seven take effect at boot - restart the server after
enabling.**

| Command | What it does |
| --- | --- |
| `enterprise status` | Each feature's on/off state + its **OFFLINE** preflight (green/red + reason). No network probes - safe and fast. |
| `enterprise preflight <feature>` | Run one feature's **full** preflight, including the bounded live-network probes (OTel empty-batch POST, S3 botocore chain-resolve). Exits 0 when GREEN and **1 when RED**, so CI can gate on it. |
| `enterprise enable <feature> [--force]` | Enable a feature: the boot-brick preflight runs first and **refuses on red** unless `--force`, then the switch flips. The gate is the offline checks plus the probes boot itself performs and refuses to start on (for `s3` in `chain` mode, the botocore credential resolve); a purely informational probe never blocks it. Set the feature's config fields first with `config set` (e.g. `config set otel_endpoint http://collector:4318/v1/traces`, then `enterprise enable otel`). |
| `enterprise disable <feature>` | Disable a feature. **Never** runs a preflight - turning a feature off must never be blocked. Config fields are preserved for re-enabling. |

Feature keys: `otel`, `metrics`, `saml`, `scim`, `pii_ner`, `jobqueue`, `s3`.

```bash
loc-tms config set otel_endpoint http://collector:4318/v1/traces
loc-tms enterprise preflight otel      # full check incl. a reachability probe
loc-tms enterprise enable otel         # offline preflight gates the write
loc-tms enterprise status              # feature x on/off x preflight
```

The web panel additionally requires an **operator-key step-up** per enable/disable (the
existing `operator_key_hash`, entered per action); the in-process CLI is already host-root,
so it needs no operator key - exactly like `config set`.

### LLM Ledger - `prompt-tap …`

Control + read the **LLM Ledger** request capture - the parity surface for the
`/prompt-tap` web page (both call `app.services.prompt_tap`). The tap is an opt-in,
read-only OBSERVER that records the **exact** request payload `run_completion` sends
on each completion call (system prompt, message array, `max_tokens`, task channel),
so a prompt tuner can see what actually went out to a local model whose own logs
truncate. **Off by default** - zero writes until turned on. API keys / secrets are
never recorded. **Local arm only for the raw bodies** (like `config` / `providers
key`): the capture file lives in the server's `DATA_DIR`, so a remote operator reads
the payloads via the web viewer. The ONE exception is the **bodies-withheld summary**:
`actions --summary` and `show-action --summary` work over `--remote` (they return
counts, statuses, error classes, finish reasons, spend and timings only - never a
prompt or response body), backed by `GET /api/prompt-tap/actions` and
`/api/prompt-tap/actions/{id}/summary` and the `ledger_summary` MCP tool.

| Command | What it does |
|---------|--------------|
| `prompt-tap on` | Turn request capturing **on** (audited). |
| `prompt-tap off` | Stop NEW raw capture; envelopes continue; existing raw files retained and age-out FROZEN while off. |
| `prompt-tap status` | Report on/off + how many records are stored, split into **legacy/clearable** vs **run-capture** (what `clear` drops vs what it leaves). |
| `prompt-tap show [--tail N]` | Print captured records newest-first (default 20). Filters: `--provider`, `--workflow`, `--run`, `--document`. `--json` emits the full request/response payloads. |
| `prompt-tap actions` | List translation **actions** newest-first - the run-envelope view: one entry per translate click, with its language-pair runs (the `/prompt-tap` **Runs** section). Filters: `--project`, `--document`, `--batch`, `--status`, `--failures`, `--limit N` (default 50). The table splits spend into **`ai$`** (model) and **`human$`** (vendor post-edit). `--json` emits the full action→run→call tree (totals carry `human_spend_usd` + `human_spend_source`, and each action carries its `vendor_legs`). Add **`--summary`** for the bodies-withheld view (counts / statuses / error classes / finish reasons / spend / timings) - the one form that works over `--remote`. |
| `prompt-tap show-action <ID>` | Show ONE action's envelope + its language-pair runs (locale / status / calls / spend). The totals line reports **AI spend + Human spend** (with the estimate/actual label), and a **vendor legs** table lists any dispatch legs on the action's runs (host only, never the claim token). `--calls` (or `--json`) also prints each run's nested call payloads (**host only** - refused over `--remote`). Add **`--summary`** for the bodies-withheld per-run view (counts / statuses / error classes / finish reasons / spend / timings + the per-call failure shape) - works over `--remote`; mutually exclusive with `--calls`. Parity for expanding a run on the `/prompt-tap` page. |
| `prompt-tap runs [--cursor ID]` | List translation **runs** newest-first - the run-first view: one row per run, grouped from each call's own run id (the `/prompt-tap/runs` page). Filters: `--since`, `--until`, `--failures`, `--limit N` (default 50). Bodies are **withheld** (call/failure counts, engines, spend, tokens), so it works over `--remote` (GET `/api/prompt-tap/runs`). `--json` emits the run summaries. Pass the previous JSON page's `next_cursor` to `--cursor` to continue. |
| `prompt-tap run <ID>` | Show ONE run's calls grouped by **step then unit** (step → segment). Bodies **withheld** by default (per-call lengths + sha256 + a 24-char head only for a body over 48 chars); works over `--remote` (GET `/api/prompt-tap/runs/{id}`). Add **`--bodies`** for the full prompt/response payloads - **host only** (implies `--json`; refused over `--remote`). A foreign/unknown run id is a non-enumerable 404. |
| `prompt-tap export [--out FILE]` | Write captures out for analysis **outside** the tool. Same filters as `show`, plus `--format jsonl\|json`, `--tail N` (must be positive; default: all stored), and `--record KEY` for one record. Without `--out` the data goes to stdout (redirect it); truncation is reported on stderr. Filters: `--provider P`, `--workflow ID`, `--run ID`, `--document ID`. |
| `prompt-tap clear` | Clear the **legacy** global capture file only (audited). Per-action **run-capture** files and their envelopes are NOT cleared here - run capture has no one-click clear (proposal §8); it falls only to the guarded Wave C purge. The audit entry records the legacy count actually dropped. |

`export` and the web page's download control call the same
`prompt_tap.export_bundle` seam, so the same selection yields identical bytes on
either surface. `jsonl` is the capture file's own format - one record per line, so
external tooling can stream it; `json` wraps the same records in a manifest head
(filters, `count`, `total_matching`, `truncated`) for when that context has to travel
inside the file. `--record KEY` takes the key from the viewer row's **Download** link
(a content digest, so it survives new captures and rotation) and emits that one
record pretty-printed. A download contains no FIELD the viewer page does not render
(it can contain more ROWS: the page lists the newest 200 and has no pagination, and it
says so when the download would be larger). Keys are kept out of a capture by the
request-channel contract plus a best-effort scrub of known key shapes at the write -
see `redact()` in `app/services/prompt_tap.py`.

Other behaviour worth knowing:

* **`--json` with no `--out`.** The exported DATA is stdout's machine output, so the
  `--json` summary object is written to **stderr** rather than corrupting the stream it
  describes. With `--out` the summary is on stdout as usual.
* **Multi-org installs.** `show`, `export`, `clear`, `on` and `off` all run under the
  operator gate (`local_op`) - gating `export` alone would be decorative, since
  `show --json` prints the same full records and `clear` destroys every tenant's
  legacy captures (it does not touch per-action run capture - §8). A capture carries no
  organization, so `--org` cannot scope any of them and is
  refused; use `--operator` for an explicit unscoped run. `status` stays ungated (a count,
  not content). The web download is disabled outright while multi-org is on. All strict
  no-ops on a single-tenant install.
* **No per-request limit on the web arm** (deliberate, not an oversight): the page's
  filters are its selection mechanism, and `--tail` is the CLI-only way to ask for just
  the newest N.
* **Retention.** Per-action run-capture files are append-only and are NOT rotated, so on a
  long-lived install they grow without bound (the 2026-08-14 soak measured ~11 MB/h under
  load). A background sweep on the scheduler tick ages them out: it deletes any capture file
  not written in `prompt_tap_ttl_days` days (**default 30**; `0` disables; raise it for
  long-retention installs, e.g. `config set prompt_tap_ttl_days 180` on a production host - watch the disk
  arithmetic of your data volume). `0` is the *only* disable value - a negative or garbage
  value is treated as invalid (falls back to the default with a warning), never as "delete
  everything". A file still being appended keeps a fresh timestamp and is never swept; a run
  whose **envelope is still running or paused is never swept regardless of age**; and the
  sweep only runs while capturing is **on** (turning the tap off freezes retention with it,
  preserving evidence - re-enable to resume both). Envelope rows outlive their call files
  (they hold no prompt text and fall only to the guarded purge; a swept file is marked
  `capture_swept` in the envelope so its totals can never masquerade as an exact zero). The
  legacy global capture file stays size-bounded by rotation and is dropped whole once it too
  goes a full window unwritten. The sweep logs an INFO summary (window in effect + bytes
  reclaimed) rather than an audit row - it drops observability data, not the PM-history
  envelopes `purge` removes. See `sweep_expired_captures` in `app/services/prompt_tap.py`.
  **Size cap (#616):** the same sweep also enforces the byte budget `prompt_tap_actions_max_bytes`
  (default 512 MB): when `prompt_tap/actions/` is over it, the oldest non-running capture files are
  deleted until it is under 80 percent of the budget (never a symlink, never a file written in the
  last 10 minutes, and a budget under 1 MB is ignored as a misconfiguration). It only ever deletes
  files named `<ULID>.jsonl` inside `prompt_tap/actions/`. `prompt-tap status`, `ledger status`, the Ledger page and the API/MCP
  `ledger_status` show the raw capture size, the age and size caps, and when the last prune ran
  and what it removed (`last_prune`, kept in `prompt_tap/prune_state.json`).

```bash
# every capture for one workflow run, as newline-delimited JSON
python -m app.cli prompt-tap export --run 12 --out run12.jsonl

# the newest 50 captures from the local engine, to stdout
python -m app.cli prompt-tap export --provider local --tail 50 > local.jsonl
```

### Bootstrap - `init`

| Command | What it does |
|---------|--------------|
| `init [--admin-email E] [--admin-password P] [--admin-name N] [--from-env] [--issue-key] [--key-scopes S]` | Seed the first administrator on an empty install. `--admin-name` defaults to `Administrator`; `--from-env` reads the email/password from the environment. `--issue-key` prints a new key once, with `--key-scopes` (default `docs`; `api` is implied). See the bootstrap rules below. |

```bash
# explicit credentials
python -m app.cli init --admin-email admin@example.com --admin-password 'a-strong-passphrase' --issue-key

# or from the environment (containers / CI)
LOC_TMS_ADMIN_EMAIL=admin@example.com LOC_TMS_ADMIN_PASSWORD='a-strong-passphrase' \
  python -m app.cli init --from-env
```

`init` runs `init_db()`, then - **only if no user exists yet** - seeds the first
admin and turns on the login wall via `security.seed_admin()` (the same code path
the `/auth/setup` wizard converges on). It is **idempotent**: on an already-seeded
install it no-ops and says so, so it is safe to run on every boot.

The "no user yet" check and the insert are one locked transaction (#414), so `init`
and a `/auth/setup` form submitted at the same moment can never both create an
admin. `init` needs no setup token (it runs on the host) and removes the generated
`DATA_DIR/.setup_token` once the admin exists. The web wizard on a server install
asks for that token: it is printed in the server log at startup, or set it yourself
with `LOC_TMS_SETUP_TOKEN` (16+ characters).

- `--from-env` reads `LOC_TMS_ADMIN_EMAIL` / `LOC_TMS_ADMIN_PASSWORD`.
- `--issue-key` mints a per-user API key and prints it **once** with a store-it-now
  banner (default scope `docs` for the document/MCP front door; `api` is implied).
- The 12-character password policy is enforced.
- **Default-off:** with the env vars unset, nothing here runs at startup - the app's
  normal boot (dev-seed / wizard) is completely unchanged.

### Ask - `ask …`

**Ask** answers plain-language questions about a module, grounded EXCLUSIVELY in the
repo-tracked **capability cards** (`app/capabilities/<module>.md`) - the same text the
in-app `/help/<module>` pages render, so a help page and an answer can never disagree.
The model is told to answer only from the cards and to say so plainly when they do not
cover the question; it is never given database content, so an Ask call cannot leak one
customer's data to another and cannot invent a feature.

It is governed by the `assist_enabled` kill switch (`config set assist_enabled 0`
turns every surface off, web and CLI) and runs on the engine named by
`assist_provider` / `assist_model` (default `local`, blank = the provider's own
model). A local engine costs $0; a cloud one goes through the normal spend ledger.
**In-process only** - SPEC-ask-draft's non-goals rule out an `/api` surface, so
`--remote` is refused loudly (exit 2) rather than silently answering from the host.

| Command | What it does |
|---------|--------------|
| `ask [<module>] "<question>"` | Answer a question about a module from its shipped guide. `<module>` is one of `connectors \| documents \| engines \| glossary \| images \| keys \| llm \| reports \| routing \| tm \| vendors \| workflows` (feature-gated modules resolve only while their feature is on); naming one grounds the answer on that card **plus every card it connects to**; omitting it grounds on the OVERVIEW set - every guide except the reference documents flagged `overview_exclude` in their frontmatter (today just `llm`, which is far too long to carry in every general answer and is reachable by naming it: `ask llm "..."`). Every answer prints its grounding footer ("From the Memory guide") and, when the assist engine is not live, a warning that the reply came from the offline mock. Web parity: the **Ask about this page** panel (`POST /assist/ask`) and `GET /help/<module>`. |

```bash
python -m app.cli ask glossary "how do I stop it translating our product name?"
python -m app.cli ask "which module decides what a step's engine is?"
```

### Content - `customer …`, `project …`, `doc …`, `tm …`, `glossary …`, `langset …`

Create and drive translation content without the web UI. The business logic is
shared with the web routes (`app/services/content.py` + `tm`/`glossary`/`languages`
services), so the CLI and the UI produce identical rows. In-process (local-root, no
RBAC) **or** `--remote` (org-scoped by an `lsu_` token, via `/api`) - see the
in-process-vs-remote matrix above. Most content commands here are remote-capable; the
exceptions are called out in their own rows (notably most of `customer`, and
`project status` / `author` / `export` / `tm-sources` / `glossaries` - the CLI verbs
stay in-process even though several now have `/api` routes; `project rename` / `delete`
became remote-capable in R1a).

Office upload/export safety checks verify actual ZIP expansion. The 200-times ratio cap
applies to parts larger than 1 MiB. XLSX also defaults to 250000 cells across worksheet
bounding rectangles and cumulative cell/merged-range work, 100000 rows per sheet and 32767 characters
per cell. Configure these with `XLSX_MAX_CELLS`, `XLSX_MAX_ROWS` and `XLSX_MAX_CELL_CHARS`.
`XLSX_MAX_TOTAL_TEXT_CHARS` (8388608) caps loaded/expanded cell text, counting repeated
shared strings and formulas, so a tiny worksheet cannot multiply one value into gigabytes.
These limits prevent sparse or merged worksheets from creating huge empty grids. Rules
imports share the checks. Split refused files into smaller ones. Every format also stops at
`MAX_DOCUMENT_SEGMENTS` (50000 segments per document; for XLSX, 50000 text cells),
`MAX_SEGMENT_CHARS` (100000 characters in one line or paragraph) and `MAX_DOCUMENT_ROWS`
(500000 segment rows, segments x target languages), checked before anything is stored. One connector run
(`connector sync` or `connector pull`) may fetch at most `CONNECTOR_RUN_CHAR_BUDGET` (50000000) characters
of source records and is refused whole past it, with the reason on the run. Remote uploads report
HTTP 413 for safety limits and retryable HTTP 503 with Retry-After when workers are busy;
MCP uses the same API responses. PDF OCR defaults to at most 4000 pixels on either edge.

Subtitles: `doc upload` takes `.srt`, `.vtt`, YouTube `.sbv`, and `.sub` as SubViewer 2.0 or
MicroDVD (told apart by content; a `.sub` that looks like both, SubViewer 1.0 or binary VobSub is
refused with HTTP 422 and a plain reason). Only cue text is translated; timecodes, frame numbers,
headers and MicroDVD `{y:i}` style codes (kept as protected tags) come back unchanged.
AsciiDoc: `doc upload` takes `.adoc`, `.asciidoc` and `.asc`. Prose, section and block titles,
list items, admonitions and table cells are translated; listing, literal, passthrough and
comment blocks, attribute entries and `include::` lines (never followed) come back unchanged;
`{attribute}` references, inline macros and formatting marks are protected tags. The export
sets `:lang:` to the target language.
Wiki markup: `doc upload` takes MediaWiki `.wiki`, `.mediawiki` and `.wikitext`, and
DokuWiki `.dokuwiki` (a DokuWiki page file is `.txt`, which uploads as plain text, so rename
it first). Headings, paragraphs, list items, table cells and link labels are translated;
link targets, templates, `<nowiki>`, `<pre>`, `<code>`, `<syntaxhighlight>`, comments, magic
words and categories come back unchanged. Template parameters are translated only when the
install lists them in `WIKI_TEMPLATE_PARAMS` (for example `Quote:text,Note:1`).

**Customers**

| Command | What it does |
|---------|--------------|
| `customer create <name>` | Create a customer. A name already used by another customer in your org (case and surrounding space ignored; archived customers count) is exit 2 naming the existing id. |
| `customer list [--archived \| --all] [--limit N] [--offset N]` | List active customers by default (id, name). `--archived` (archived-only) / `--all` (both) are **server-side** filters (`/api/customers?archived=only\|all`), so `--remote` returns the same result set as local at any size. `--limit`/`--offset` page the list (capped at 1000 with a truncation note, never a silent cutoff); a non-positive `--limit` or negative `--offset` is refused (exit 2). Advance `--offset` by the number of rows returned to fetch the next page. |
| `customer show <id>` | Customer detail: projects, connectors, workflows, language pairs and TM stats (`--json` for the full payload). In-process only. |
| `customer update <id> --set K=V` | Set preferences (repeat `--set` for multiple keys); `K=` removes a key. In-process only. |
| `customer set-default <id> [--provider P] [--workflow ID] [--segmentation ID] [--content-type ID]` | Set scoped defaults; pass a blank value to clear an override. In-process only. |
| `customer rename <id> <name>` | Rename a customer. In-process only. |
| `customer delete <id> [--yes]` | Delete an unreferenced customer (confirms unless `--yes`). Fails closed while anything references it; no cascade. In-process only. |
| `customer archive <id>` | Retire a customer: hide it from the default list and pickers while preserving its data. In-process only. |
| `customer unarchive <id>` | Reactivate an archived customer so it is offered in pickers again. In-process only. |
| `customer set-ai <id> off\|on` | AI opt-out for every project of the customer, current and future (a project cannot undo it). Audited. In-process only. |
| `customer pairs list <id>` | List a customer's explicit and derived active language pairs. In-process only. |
| `customer pairs add <id> <pair>` | Add an explicit active pair, e.g. `en-US->fr-FR`. In-process only. |
| `customer pairs remove <id> <pair>` | Remove an explicit active pair. In-process only. |
| `customer pairs import <id> <csv>` | Bulk-import language pairs from a CSV (`source_locale,target_locale` columns), atomic (a bad row rejects the whole file, no partial write) + idempotent (already-present pairs skipped). Unlike its `list`/`add`/`remove` siblings above, this verb IS `--remote`-capable (`POST /api/customers/{id}/pairs/import`, admin, manager or linguist (can_configure_customer)); a deliberate asymmetry: the CLI-parity backlog explicitly asked for `/api` parity on this one, so it got a dual local/remote arm the others were never given. |

**Projects**

| Command | What it does |
|---------|--------------|
| `project create <name> [--source LOC] [--targets "fr-FR,de-DE"] [--customer-id N] [--provider P] [--per-pair] [--pair-provider SRC:TGT=P] [--seg-profile ID] [--content-type ID] [--due YYYY-MM-DD] [--all-targets] [--type document\|key_based] [--from-sdl-package FILE] [--field KEY=VALUE] [--connected]` | Create a project and its target locales (comma/space separated, de-duplicated). `--from-sdl-package` takes the source and target languages from a Trados project package (.sdlppx) and adds one document per target SDLXLIFF (local only; a refused import removes the project again). `--seg-profile` pins a segmentation profile (omit to inherit the chain); `--content-type` sets the content type (omit = General / the customer's default); `--due` sets the optional due date. `--per-pair` switches the project to PER_PAIR provider mode; `--pair-provider en-US:fr-FR=engine:2` is repeatable and implies `--per-pair`. `--type key_based` makes a **key-based** project (see the Keys section below); it needs at least one `--targets` locale and is refused while the `key_projects_enabled` switch is off. Omitting `--type` creates a document project, exactly as before. `--field KEY=VALUE` (repeatable) sets custom project fields at creation; a required field must be given, and one bad value creates no project (exit 2). A target outside the customer's configured active pairs is created anyway and reported as a warning on both in-process and `--remote` (the server returns `targets_outside_pairs`); `--all-targets` silences it. `--connected` creates a **connected project**: its content comes from a connected system instead of uploads. It starts empty as a Documents project (not combinable with `--type key_based` or `--from-sdl-package`), uploads to it are refused, and the reply carries `intake` and `setup_url`; bind an existing connection with `project bind` (or on the project page). On `--remote` it sends `intake: connected` to `POST /api/projects`. |
| `project list [--status S] [--archived active\|all\|only] [--limit N] [--offset N] [--field KEY=EXPR] [--columns a,b] [--sort-field KEY[:desc]] [--connected]` | List projects (id, name, source, customer, status). **Custom fields:** `--field KEY=EXPR` (repeatable) filters by a custom project field (`KEY=value`; `KEY='~text'` contains; `KEY='>=2026-01-01'` and `<=` for number and date fields), `--columns a,b` adds custom field columns to the table, `--sort-field KEY:desc` orders by a field (projects without a value last); every `--json` row carries `custom_fields`. Omitted/blank `--status` (or `default`) hides completed/closed/cancelled projects; `--status all` includes every status, or name one status to filter it. `--archived active` (default) hides archived projects, `all` includes both, `only` selects archived projects. `--status` and `--archived` are **server-side** filters (`/api/projects` honours `?status=`/`?archived=`), so `--remote` returns the same result set as local at any size. `--limit`/`--offset` page the list (capped at 1000 with a truncation note, never a silent cutoff); a non-positive `--limit` or negative `--offset` is refused (exit 2). Advance `--offset` by the number of rows returned to fetch the next page. When the default filters hid projects the caller can open by id, a stderr note (and `hidden: {archived, finished}` in `--json`) says how many (#607). `--connected` lists only connected projects and adds a `connected source` column (the connector kind, or `Not connected yet` until a source is bound); every project in `--json` carries `intake` and, when connected, `source`. Remote-capable (`GET /api/projects?intake=connected`). |
| `project show <id>` | Full project detail: locales, documents, TM + glossary counts. |
| `project leverage <id>` | 7-band **word-based** TM-leverage breakdown (repetitions / context / exact / fuzzy 95-99 / 85-94 / 75-84 / new) - the real vendor-cost model surfaced project-wide from the segments' persisted TM-match data, not the coarse 4-band segment-count bar. Human table or `--json` (`bands[]` + `total_words` / `covered_words` / `billable_words`). `--remote` (`GET /api/projects/{id}/leverage`). Computed on demand (O(n) over the project's segments; no live TM lookup). |
| `project author "<description>" [--provider P] [--model M] [--create] [--save-template NAME]` | Draft a whole project from a plain-language description - name, locales, provider, content type - the CLI twin of the web **Design a project** dialog. It **prints the draft and stops** by default: `--create` then creates it through the same `content.project_create` seam `project create` uses, or `--save-template` stores the draft as a reusable project template instead of creating anything. `--provider` / `--model` pick the engine for the authoring call itself (blank = the configured default; list model ids with `providers models --provider <ref>`). **In-process only** (`--remote` refused, exit 2). |
| `project add-locale <id> <locale> [--all-targets]` | Add one target locale (additive, idempotent). Over `--remote` (and from MCP) a project with a bound content source needs a manager or above, because the next run translates the new language; the in-process CLI runs as the trusted operator on the box and has no roles. |
| `project remove-locale <id> <locale> [--yes]` | Remove an untranslated locale and its seeded segments; keep TM and glossary data. Refuses the last locale or a locale with committed translation. Confirms unless `--yes`. Remote-capable. On a CONNECTED project (one with a content source) it only STOPS the locale: nothing is deleted, the next run no longer pulls, translates or writes it, and `project add-locale` resumes it; `project show` lists `stopped_locales`. |
| `project set-providers <id> [--per-pair] [--pair-provider SRC:TGT=P]` | Set per-pair engine overrides (`--pair-provider` is repeatable and implies `--per-pair`). With neither flag, switch to UNIFORM mode and clear existing pair overrides. Remote-capable. |
| `field list [--archived]` | List the custom project fields (typed fields such as a Jira ticket link). Remote-capable. |
| `field create <key> <label> [--type text\|long_text\|number\|date\|select\|url\|external_link] [--options a,b] [--customer ID] [--required] [--list-column] [--no-filter] [--help-text T] [--order N] [--key-pattern RE] [--system jira]` | Define a custom project field. The key (lowercase letters, digits, underscores) and the type cannot change later. `--customer` scopes it to one customer (default: every customer); `--list-column` makes it a default projects-list column; `--key-pattern` is a regular expression a link key (or a text value) must match; `--system jira` names the external system of a link and builds in a Jira key pattern. Remote-capable (POST `/api/custom-fields`, manager or admin; a manager must give `--customer` and later changes only fields they created, org-wide fields are admin only). |
| `field update <id> [--label L] [--options a,b] [--required on\|off] [--list-column on\|off] [--filterable on\|off] [--help-text T] [--order N] [--key-pattern RE] [--system S]` | Change a field. A select option still used by a project cannot be removed. Remote-capable (PATCH `/api/custom-fields/{id}`; a manager changes only a customer field it created, an admin any). |
| `field archive <id>` | Archive a field: its values are kept and stay readable, but it is no longer offered for editing. Remote-capable. Same rule as `field update`. |
| `field restore <id>` | Restore an archived field. Remote-capable. Same rule as `field update`. |
| `project set-content-type <id> [ID] [--clear]` | Set the content type; omit the id or pass `--clear` to return to General. Remote-capable. |
| `project set-field <id> <key> [value]` | Set a **custom project field** value (omit the value or pass `''` to clear it). A link field takes a URL, a key such as `LOC-123`, or a JSON object with `url`, `key` and `label`. A bad value is refused (exit 2) and nothing is written. Remote-capable (PATCH `/api/projects/{id}/fields`). |
| `project set-customer <id> <customer_id>` | Assign a customer to a customer-less project. Refuses reassignment to a different customer. Remote-capable. |
| `project rename <id> <name>` | Rename a project. Remote-capable. |
| `project delete <id> [--force] [--privacy-erasure]` | Show the deletion preview (exit 2 without `--force`); `--force` deletes the project and its owned data (documents, segments, TM and runs). `--privacy-erasure` also deletes Lab snapshots of the project's documents with their runs, scores and labels; otherwise the result line warns when Lab snapshots retain text. Fails closed on connection/routing-rule references and workflow-memory suggestions targeting the project. Remote-capable. |
| `project archive <id>` | Archive (retire) a project - it drops out of the default `project list` + every picker and is **blocked from launching new runs**; its documents/segments/TM/run history are fully **preserved** and it is **reversible**. Not a delete and not a kill switch (an in-flight run finishes untouched). In-process only (the `/api/projects/{id}/archive` route serves the web + remote-API surfaces). Idempotent-safe (archiving an already-archived project is a clean usage error). |
| `project unarchive <id>` | Unarchive (reactivate) a project - list it, offer it in pickers, and make it runnable again. In-process only. |
| `project set-ai <id> off\|on` | Switch this instance's AI off/on. See AI opt-out below. |
| `project set-comments <id> on\|off` | Translate comments and annotations in Office files (#690): Word comments, Excel notes and threaded comments, PowerPoint comments (classic and modern, with replies), OpenDocument annotations (.odt, .ods, .odp, .odg) and Visio comments. `off` keeps them in the source language. Authors, initials and dates never change. Applies to files uploaded or re-imported afterwards; documents already imported keep their segments. New projects start with it `off`; a project created before the option keeps Word comments on and the other formats off until it is switched. Audited. In-process only (the `/api/projects/{id}/translate-comments` route serves the web and remote API). `project show` reports `translate_comments`, `translate_comments_word`, `translate_comments_source` (`project` or `legacy`). |
| `project apply-langset <id> <langset_id>` | Add every locale in a Language Set to the project (additive merge, idempotent; audited). Same manager rule as `project add-locale` on a project with a bound content source. |
| `project export <id> [--locales "fr-FR,de-DE" \| --all-locales] [--zip] [--batch BATCH_ID] [-o PATH] [--force] [--allow-held] [--sdl-return-package [GUID]]` | **`--batch BATCH_ID`** (ZIP batch upload) exports only the documents created from that uploaded `.zip`, always as a ZIP laid out `<locale>/<original path in the archive>` (one folder per language, the upload's own folders and file names inside); the id is printed by `doc upload` and listed by `doc list`. `--sdl-return-package` builds one Trados return package (.sdlrpx) from the documents imported from a Trados project package instead (name the package GUID when the project holds several); held documents are left out and listed unless `--allow-held`. Otherwise: build the project's **delivery bundle** - a ZIP with per-locale subfolders and `<stem>_<locale>.<ext>` filenames (one explicitly-named locale streams that single file unless `--zip`). Two documents whose names match (ignoring case, or after unsafe characters become `_`) never overwrite each other: the later one gets a `_<doc_id>` suffix before its extension and is listed in the in-archive `_RENAMED.txt`. Local-only; parity with `GET /projects/{id}/export`. **Two honesty channels, because a bundle can degrade in two different ways.** *Quarantine* (loud): a document whose inline tags could not be verified is **excluded** from the ZIP, listed in the in-archive `_QUARANTINED.txt`, warned about on stderr and returned as `skipped` in `--json` (web twin: the `X-Export-Quarantined` header). *Substitution* (silent, BUG-04): a document that **is** delivered but had tag-damaged units replaced by their **SOURCE text** under `export_tag_damage_policy` - the file is well-formed and those units are simply still in English, which nothing in the archive would otherwise reveal. It is now reported per document in `_SUBSTITUTED.txt`, on stderr, and as `substituted` in `--json` (`{doc_id, filename, locale, count, detail}`, `detail` being the same `substituted=N kind=... unit=... index=...` rendering `doc export` uses); the web twin adds `X-Export-Substituted` + `X-Export-Substituted-Detail`. **`--allow-held`** affects only the **single-file** path: a single-file export of a locale with no committed translation is refused by default (the file would be pure source), and this flag ships it explicitly. The **ZIP** path is unaffected - it always **skip-and-reports** held docs (naming the override), so `--allow-held` changes nothing about a ZIP. `--out PATH` is the long form of `-o PATH`. |
| `project status <id> <status>` | Move a project through its lifecycle. `<status>` is one of `draft`, `pre_translating`, `in_translation`, `in_review`, `completed`, `closed`, `cancelled` (anything else is exit 2, listing the valid ones). `completed` and `closed` are both still editable and reopenable - adding a locale or new content flips a project back to `in_translation`; `cancelled` is manual and can only be reopened to `in_translation`. Only these moves are allowed (anything else, including repeating the current status, is exit 2 listing the allowed next states): draft, pre_translating, in_translation and in_review move to any status except `closed` or themselves; `completed` to in_translation, in_review, closed or cancelled; `closed` or `cancelled` to in_translation. The app's own automatic changes (auto-complete, reopen, auto-close) are not restricted. Audited. Parity with `POST /projects/{id}/status`. **In-process only** (`--remote` refused, exit 2). |
| `project set-due <id> [DATE] [--clear]` | Set (`YYYY-MM-DD`) or clear (`--clear`) a project's optional due date; audited (`project.due_set` / `project.due_cleared`). **Remote-capable** (`POST /api/projects/{id}/due`, mirroring the web form). |
| `project tm-sources list <id>` | List the projects whose translation memory this project **also** matches, with the number of entries **reachable for this project's content type** (not the source's own row count - see `add` below). Only *currently effective* sources are listed: a link whose source has since moved to another customer stops being shown, because it has stopped contributing. **In-process only** (no `/api` route; `--remote` is refused, exit 2). |
| `project tm-sources add <id> <source_project_id>` | Attach an **already-established** TM: this project's **matching** paths (100% exact, fuzzy, batch, TM few-shot examples, full-text and semantic) will also match the source project's entries. **A reference, not a copy** - no row is duplicated, and write-back / `tm stats` / TMX export / the `/tm` browse / the editor's **concordance** stay own-project only (attachment widens what you *match*, never what you can *read out* or *own*). Ties go to **your own row**: at an equal match your project's entry beats a borrowed one, so a reviewer's correction takes effect immediately. Usage counters on the source project are never touched by your runs. A content-type mismatch is **not** an error but *is* a no-op - the content-type scope still applies, so the command reports how many of the source's entries are actually reachable. Refused (exit 2) for a self-link or a source outside the project's customer/organization; idempotent. One level only: the source's *own* attached sources do **not** chain. **In-process only** (`--remote` refused, exit 2). Web parity: the *Translation memory sources* card on the project page. |
| `project tm-sources remove <id> <source_project_id>` | Detach a TM source. Because nothing was copied, matching returns to exactly what it was before the attach. Reports honestly when there was no such link. **In-process only** (`--remote` refused, exit 2). |
| `project glossaries list <id>` | List the module glossaries pinned to this project, with whether each is actually **applied** (a draft one is listed but marked `no (needs approval)`). **In-process only** (`--remote` refused, exit 2). |
| `project glossaries add <id> <glossary_id>` | Pin an **existing** module glossary to a project, so every run matches its terms without a workflow step naming it. Works on typed **and** untyped projects (a pin is an explicit choice, unlike content-type auto-attach), and an explicit pin **outranks** the project's own auto-attached glossaries on a term conflict - including a pin of a *different* content type, which the web picker labels for that reason. A **draft** glossary may be pinned but contributes nothing until approved; a step-level glossary pin still overrides. Refused (exit 2) for a glossary outside the project's customer/organization **or outside the caller's own glossary-module visibility**; idempotent. **In-process only** (`--remote` refused, exit 2). Web parity: the *Attached glossaries* card on the project page. |
| `project glossaries remove <id> <glossary_id>` | Unpin a glossary from a project. The glossary itself is untouched. Reports honestly when there was no such pin. **In-process only** (`--remote` refused, exit 2). |
| `project set-intake <id> upload\|connected` | Switch a project between uploads and a connected source. Allowed only while the project has no documents, no keys and no bound source; otherwise refused (exit 2) with the reason. Unbinding a source never changes it: an unbound connected project stays connected and shows the setup menu. Audited (`project.intake_set`). Remote-capable (`POST /api/projects/{id}/intake`, manager or admin). |
| `project make-connected <id> [--connection N]` | Make an existing project that a connection already feeds a connected project bound to that same connection: no lane, no schedule, no run, nothing written to the connection. `--connection` is needed only when more than one connection feeds the project. Refused (exit 2, with the reason) for uploaded documents, inbox and archived projects and projects already connected or bound. Audited (`project.made_connected`). An upgrade never does this by itself. Remote-capable (`POST /api/projects/{id}/make-connected`, manager or admin). |
| `project bind <id> <connection_id> [--clone] [--scope KEY=VALUE]...` | Set the project's **content source**: the connection it pulls from and writes translations back to (project automation). One connection can feed many projects: the first project adopts a connection that feeds nothing yet, and any further project (or `--clone`) gets its own private copy of it, with the same credential and its own tracking, so projects never overwrite each other's items (the first project to reach an item owns it). `--scope` narrows what this project reads (repeatable; the in-process CLI is the operator, so any declared setting; over `--remote` a manager may only narrow). Refused for an inbox project and a connection the project may not use. Audited (`automation.bind`, `automation.clone`). Remote-capable (`POST /api/projects/{id}/source`). |
| `project unbind <id>` | Remove the project's content source. Stops its schedule and releases the items it owned; the connection and its tracking are kept, so the same project can bind it again. Remote-capable. |
| `project automation show <id>` | The content source, schedule (UTC), mode, pause state, next and last run, with `--json`. Remote-capable (`GET /api/projects/{id}/automation`). |
| `project automation set <id> [--times 09:00,14:30] [--days mon,wed] [--interval N] [--mode delta\|full] [--enable\|--disable] [--auto-pause-after N] [--run-cap USD\|none] [--move-schedule]` | Change the schedule and run settings; only what you pass changes. **Times are UTC**, 24-hour; `--days` empty means every day; `--interval` (15 to 10080 minutes) replaces times of day (not both). `--mode full` translates and writes everything again (TM matches are free) and is refused for airtable, jira and the git connectors. `--enable` never starts a run: the first run is the next slot. `--auto-pause-after` pauses the schedule after N failed runs in a row (default 3). `--run-cap` stops paid calls in one run at that many USD (the project's monthly budget always applies; no default cap). `--move-schedule` replaces the connection's own interval schedule (one schedule per connection). Audited (`automation.set`). Remote-capable (`PUT /api/projects/{id}/automation`). |
| `project automation pause <id> [--reason TEXT]` | Pause the schedule. A paused automation fires nothing; Run now still works. Audited. Remote-capable. |
| `project automation resume <id>` | Resume a paused schedule (clears the failure counter; the next run is the next slot). Audited. Remote-capable. |
| `project automation handover <id> (--to PROJECT_ID \| --from PROJECT_ID)` | Hand over ownership of the items another project was blocked on by this project's claims (`--to`: this project owns them and gives them to PROJECT_ID), or take the items this project was blocked on from their owner (`--from`). The receiving project owns those pairs afterwards. Refused while a run is in progress for either project. Audited (`automation.claim_handed_over`) and shown in both run histories. A blocked run also notifies both projects (`automation.blocked`). Remote-capable (`POST /api/projects/{id}/automation/handover`, manager or admin). |
| `project run <id> [--mode delta\|full]` | Run the project's content source **now**: pull, translate what is new or changed (or everything with `--mode full`), write back. **Spends money** and writes to the live external system. The project's monthly budget is checked first; a run already in flight is handed back (never stacked). In-process it waits for the run to finish; `--remote` returns the job id (`POST /api/projects/{id}/runs`, `job status <id>` to follow it). |
| `project runs <id> [--limit N]` | The project's recent automation runs (when, why, mode, status, written, held for review, failed). Remote-capable (`GET /api/projects/{id}/runs`); cost appears only for managers and up. |

AI opt-out, `project set-ai <id> off`:
This instance's AI engines never process this project's content. Content you download, export,
or connect to your own AI tools (for example the MCP agent connection) is outside this switch.
MT, AI workflow steps (llm_agent, lqa), the LLM judge, auto-fix, Editor AI helpers
and OCR/vision are refused or skipped, while human-only workflows and TM/glossary
lookups keep working. `on` allows AI again (a customer-level opt-out still applies).
Audited. In-process only (the `/api/projects/{id}/ai-opt-out` route serves the web and remote API).
`project show` reports `ai_opt_out`, `ai_opt_out_project`, `ai_opt_out_customer`.
`customer set-ai <id> off` applies this same boundary to every current and future project
of that customer.

**Which projects the web picker offers.** The project page's *Translation memory
sources* dropdown lists only projects that already hold **at least one TM entry** - the
card attaches an *already-established* memory, and offering every project in the install
would bury the ones that carry any. The CLI is deliberately not filtered that way:
`project tm-sources add <id> <source_project_id>` accepts a project with an **empty** TM,
so you can wire up the link before importing into the source. An existing link to a
project whose TM is later emptied also keeps working (it simply matches nothing) -
detaching it is the operator's call.

**Deleting either end.** `project delete` removes the project's attach links in **both**
directions (its own sources *and* the links other projects made to it); the itemized
pre-count names all three (`tm_sources_attached`, `tm_source_uses`, `glossary_pins`) so
you can see how many other projects lose coverage before confirming. `glossary module
delete` unpins the glossary from every project first, so a pinned glossary stays
deletable.

**Active-pairs confinement.** A customer's *active language pairs* (`customer pairs …`)
reduce the Target-languages picker across the web UI to just that customer's pairs
(the *Show all languages* toggle stays as the escape hatch). `project create --targets`
and `project add-locale` mirror this **softly**: a target outside the customer's active
pairs is **still accepted**, but prints a `warning:` to stderr naming the confinement.
Pass `--all-targets` to silence the warning, or `customer pairs add <id> <src>-><tgt>`
to widen the pairs. A customer with no pairs configured has no confinement (no warning) -
the mandatory fresh-install fallback.

**Project templates**

| Command | What it does |
|---------|--------------|
| `template create --from-project <id> --name N [--scope global\|customer] [--description D]` | Snapshot a project's setup (locales, provider, workflow, segmentation) as a reusable template. `--scope customer` binds it to the source project's customer. |
| `template list [--customer-id N] [--all]` | List templates (global + a customer's own). |
| `template show <id>` | Show one template's captured setup. |
| `template apply <id> --name N [--customer-id N] [--connected]` | Create a new project from a template (`--connected` makes an empty connected project; a template never binds or schedules a source).  Mirrors the web "from template" policy: a **global** template requires `--customer-id` whenever the org has a selectable customer (a customer-less project is refused); blank `--customer-id` on a **customer-scoped** template uses the template's own customer, and a conflicting one is refused. Archived customers are refused. |
| `template rename <id> <name>` | Rename a template. |
| `template delete <id> [--yes]` | Delete a template (the snapshot only - projects made from it are untouched). Writes a `template.delete` audit row. With `--operator` (host-only, `LOC_TMS_OPERATOR_KEY`) it can delete a GLOBAL template, which no org-scoped caller can see; it REFUSES an org-owned template outright, which is a narrowing of what `--operator` reached before 2026-09-20. That operator row is audited even when `audit_enabled` is off. |

**Documents**

Salesforce Translation Workbench exports upload like any document: `doc upload <project_id> de.stf`. The LABEL column becomes segments (keys are never changed) and `doc export` writes the file Translation Workbench Import expects: Bilingual TRANSLATED rows get the TRANSLATION column; Source, Outdated and untranslated rows and the Bilingual UNTRANSLATED section get the translation in place of LABEL; `Language code:` is set to the target (de, pt_BR, zh_CN, ...). The same file uploads on the web project page, over `/api` and through MCP.
Server-page templates upload like HTML: `doc upload <project_id> index.jsp` (also `.php`, `.phtml`, `.jspf`, `.aspx`, `.ascx`, `.erb`, `.shtml`, `.shtm`, `.stm`). Code blocks (`<?php ?>`, `<%= %>`, JSP `${...}`, SSI `<!--#echo -->` and the rest) are never translated and come back byte for byte; a file whose code cannot be separated from the HTML safely is refused with the line number. The same formats upload on the web project page, over `/api` and through MCP.
PHP localization arrays upload like any document: `doc upload <project_id> lang/en/auth.php` (Laravel `return [...]` or CodeIgniter `$lang[...]`). String values become segments; the file is never executed and is refused when a value is not a static literal; `doc export` rewrites only the translated strings. The same file uploads on the web project page, over `/api` and through MCP.
Key-value software resources upload like any document: `doc upload <project_id> messages.properties` (also `.strings`, `.ini`, `.resjson`). Only values become segments; keys, comments and layout are kept, and `doc export` writes the file back in its source encoding (ISO-8859-1, or ASCII with `\uXXXX` escapes, for .properties; UTF-16 for a UTF-16 .strings or .ini). The same formats upload on the web project page, over `/api` and through MCP.
AutoCAD drawings upload like any document: `doc upload <project_id> plan.dxf` (ASCII DXF; binary DXF is refused). TEXT, MTEXT, attribute, dimension override and multileader text become segments; `doc export` changes only those values, keeps the drawing's encoding and leaves every other byte identical. The same file uploads on the web project page, over `/api` and through MCP.
Windows resource scripts upload like any document: `doc upload <project_id> app.rc`. STRINGTABLE, DIALOG/DIALOGEX, MENU/MENUEX and VERSIONINFO text become segments; `#include` is never opened; `doc export` keeps the source encoding (UTF-16, UTF-8 or the ANSI code page) and writes the target LANGUAGE. The same file uploads on the web project page, over `/api` and through MCP.
TOML files (Hugo i18n, go-i18n) upload like any document: `doc upload <project_id> en.toml`. Only string values become segments (each plural category on its own; `description`, `id`, `hash` stay); `{{...}}` template actions are protected; `doc export` keeps keys, comments and layout byte for byte. The same file uploads on the web project page, over `/api` and through MCP.
Visio drawings upload like any document: `doc upload <project_id> flow.vsdx` (also `.vsdm`). Page names, shape text and shape data labels become segments; `doc export` writes the drawing back with masters, macros and every other part untouched. The same formats upload on the web project page, over `/api` and through MCP.
FrameMaker MIF files (MIF 8 and later) upload like any document: `doc upload <project_id> guide.mif`. Each paragraph is one segment; variables, cross-references, markers and font changes are protected tokens; `doc export` keeps everything else byte for byte. MIF 7 and older is refused (re-save from FrameMaker 8 or later). The same file uploads on the web project page, over `/api` and through MCP.
JavaScript/TypeScript locale modules upload like any document: `doc upload <project_id> locales/en.ts` (`.js .mjs .cjs .ts .mts .cts` whose content is only `export default {...}`, `module.exports = {...}` or `const x = {...}` with string values). The file is never executed: any other code (a function call, spread, computed key, interpolated template) refuses it with a line number, so ordinary application code is never claimed. `doc export` rewrites only the translated strings, in their original quotes. The same file uploads on the web project page, over `/api` and through MCP.

Broadcast captions upload the same way: `doc upload <project_id> show.scc` (Scenarist SCC, CEA-608) or `doc upload <project_id> show.stl` (EBU STL). Each caption is a segment; `doc export` keeps every timecode and code, re-encodes only changed captions, and refuses the export with a per-caption list (timecode and reason) when a translation has characters the format cannot hold (SCC: CEA-608 Latin set) or does not fit (SCC 32 characters per row; STL the file's MNC). The web project page, `/api` and MCP behave the same.
Apple Xcode localization catalogs upload like any document: zip the exported `.xcloc` folder, name the archive `fr.xcloc` and run `doc upload <project_id> fr.xcloc`. The strings in `Localized Contents/*.xliff` become segments; `doc export` writes the archive back with every other file unchanged and the target language set in `contents.json` (`targetLocale`) and the XLIFF (`target-language`). The same file uploads on the web project page, over `/api` and through MCP.

QuarkXPress Tags (XPress Tags tagged text) upload the same way: `doc upload <project_id> chapter.xtg` (also `.tag`; rename a tagged `.txt` export to `.xtg`, since `.txt` is read as plain text). Each paragraph is one segment; attribute tags, special characters and index markers are protected tokens; `doc export` keeps the header, style sheet definitions and untouched paragraphs byte for byte and writes the file in the encoding its `<e>` tag names. The same file uploads on the web project page, over `/api` and through MCP.

reStructuredText and LaTeX upload the same way: `doc upload <project_id> guide.rst` (also `.rest`, `.tex`, `.latex`, `.ltx`). Code, math, comments and directives are kept; `doc export` refuses an rST file whose translated table cell cannot fit its column and names the table line (shorten the segment or widen the column in the source). Set `LATEX_TEXT_COMMANDS=cmd1,cmd2` to translate the text of more inline LaTeX commands.

| Command | What it does |
|---------|--------------|
| `doc upload <project_id> <file> [--mode segmented\|full_text] [--context-json JSON] [--columns MAPPING]` | Ingest a file (docx, epub, pptx, odt/ods/odp/odg, txt, html, md, csv, tsv, json, arb, ttml/dfxp subtitles, Angular xmb/xtb (an .xmb exports as .xtb), a MadCap Flare project as .flprjzip, resx/resw, wxl, Qt .ts, Android strings.xml or Java XML properties .xml, xml (multilingual XML too), dita/ditamap/bookmap, DocBook .dbk, …; Office macro and template files such as .docm, .xlsm, .pptm and .potx keep their macros and file type on export; Apple .stringsdict, .xcstrings and .plist too, where a String Catalog export adds or replaces only the target language; bilingual CAT files .sdlxliff/.sdlxlf, .mqxliff, .mxliff, pre-segmented Trados .ttx, Wordfast .txlf/.txml and zipped packages .mqxlz, .xlz and .wsxz too: locked, approved and confirmed segments are never overwritten and the export keeps the vendor's segmentation and every other byte; a Trados project package .sdlppx becomes one document per target SDLXLIFF in the project's languages, and the skipped files are listed) into a project as a document. A **`.zip`** is a batch: one document per supported file inside, named by its folder path (`intro.md` in folder `guide` -> `guide__intro.md`); every member that is not ingested (unsupported, nested `.zip`, symlink, system file, duplicate path, failed to read) is printed with its reason, and the batch id is printed for `project export <id> --batch <batch_id>`. An archive with an unsafe path (absolute, drive, `..`), a compression bomb or more than `ZIP_BATCH_MAX_DOCUMENTS` (500) files is refused whole. Flutter `.arb` and resource-dialect `.json` (Chrome messages.json, FormatJS, Structured JSON, go-i18n) translate only their messages; `doc export` sets ARB `@@locale` and gives ICU/go-i18n plurals the target language's CLDR categories. `--columns` maps spreadsheet columns for CSV/TSV/XLSX: `source=English,target.de=German,target.fr=fr,key=id,context=notes,maxlen=limit` (header names or 1-based numbers; `header=yes\|no\|auto`; CSV only: `delimiter=comma\|semicolon\|tab\|pipe`). Each target language writes its own column; unnamed columns and untouched rows keep their exact bytes; written cells starting with `= + - @` get an apostrophe. A CSV/TSV without `--columns` is read from its header row (source/target or language-code columns), else translated cell by cell in place; an XLSX without it is translated in place as before. A mapping that does not fit the file exits 1 with a plain message (`--remote`: the server's 422). Web: the upload form's Spreadsheet columns field; API: form field `columns`. `--context-json` attaches a per-document translation-context payload (size-capped; validated as JSON before ingest). A file over a safety limit is refused with a plain message and nothing is stored: a DOCX/XLSX whose parts expand past `ZIP_MAX_UNCOMPRESSED_BYTES` (256 MB) or by more than `ZIP_MAX_RATIO` (200 times), a PDF with more than `PDF_MAX_PAGES` (1000) pages, or any format with more than `MAX_DOCUMENT_SEGMENTS` (50000) segments, a line or paragraph over `MAX_SEGMENT_CHARS` (100000 characters), or more than `MAX_DOCUMENT_ROWS` (500000) segment rows across the project's target languages. `--remote` gets the same refusal as a 413. A connected project refuses uploads (exit 2): its content comes from its source. |
| `doc list <project_id>` | List a project's documents (id, filename, fmt, mode, status), then one line per ZIP batch upload (batch id, archive, document count; `zip_batches` in `--json`). `status` is derived from the segments of every language: `new`, `in_translation`, `in_review` or `done`. |
| `doc show <doc_id> [--locale LOC] [--detail]` | Per-locale segment counts + how many are translated. The document `status` is derived from its segments (`new`, `in_translation`, `in_review`, `done`). **`--detail`** additionally surfaces the per-segment review state the web editor shows - one row per segment (`id`, `order`, `status`, `target_present`, and any surfaced `qa_flags` with `type`, `severity`, `message` and QE `passed` and `skipped` fields or the auto-fix `outcome` field for QE / DNT / glossary / refusal / language-mismatch / number / placeholder / whitespace / review-hold) plus a calm human line per flagged locale that names each surfaced family carrying a flag, e.g. `N flagged for review (dnt X, glossary Y, refusal Z, nontranslation W, qe V)` (zero-count families are dropped, so the itemized reasons always account for the header count). Passing or skipped QE and fixed or skipped auto-fix records remain visible but are informational and excluded from the review count. Without `--detail` the light counts shape is unchanged; with it, `--json` carries a `segment_details[]` array under each locale. This is the headless view of a doc whose segments were quarantined into `needs_review` - the counts alone read "2/2 translated" even when both are held for a linguist. Mirrors `GET /api/documents/{id}/segments?detail=segments` (the flag follows the batch `?detail=steps` precedent - opt-in so a plain status check stays light). The `nontranslation` family carries **two** distinct verdicts, told apart by the flag's `reason`: the output-language mismatch, and (QA campaign BUG-06) a **translator note / model meta-commentary** span that survived into the delivered target - e.g. a trailing `(Note: I corrected all issues identified in the feedback ...)`. A segment whose numeric **values** did not survive translation (BUG-06's other half - `"25 items"` shipped as `"25.000 artículos"` for `es-ES`) is routed to `needs_review` by the same write seam under a `number_integrity` qa_flag and is held out of TM; the `status` column reports it here, and the editor's live QA cell shows the number finding on the segment. The always-on write-seam badges - `number_integrity`, `placeholder_integrity`, `char_limit`, `tag_integrity`, `whitespace` and the meta-commentary verdict - are **cleared automatically** when a later commit to the same segment re-runs the check clean (a corrected figure, restored placeholders, a shortened value, restored tags, tidied spacing, a note-free retranslation), so a badge always describes the text that is actually stored; the language-mismatch verdict that shares the commentary's flag family is scoped out of that clear and is never touched. DNT and glossary flags are recomputed only when glossary terms are supplied for that write; no-terms writes preserve them. Replacing the stored target clears old `qe` and `qe_autofix` verdicts; unchanged text preserves them. **`placeholder_integrity`** (a dropped/added `{0}`/`{name}`/`%s`, compared as a multiset so reordering is silent) is a **hard** downgrade like `tag_integrity`/`number_integrity` - TM-ineligible and withheld from connector write-back. **`whitespace`** (an interior double space or target-added edge whitespace) is checked only on real-provider output - never on the offline echo, and never on a full-text body (its `\n\n` paragraph joins are structural) - and is **soft** and TM-neutral, like `char_limit`. `review_hold` (minor, real-provider output only, TM-ineligible until a person saves or approves the text, which clears it; the export still writes the target) names one or more of: an ambiguous numeric date the target reader can take with day and month swapped (en-US `10/11/2026` written as de-DE `10.11.2026`, or kept as `10/11/2026` for Hebrew or Thai readers; a correct conversion such as `11.10.2026`, a dotted day-first date and an unambiguous date such as `25/12/2026` pass), line breaks that differ between source and target (not judged for HTML / XML / Markdown and other markup where a newline is a space; a subtitle cue merged into one line passes), and, for German, Dutch, Finnish, Korean, Turkish, Arabic and Hebrew, two words joined at a formatting change where the source has a space and the result is not a plausible compound (`bevor Siedas`; `Benutzername` passes). The flag's `reason` says which. The summary line counts it as `review-hold`. |
| `doc rename <id> <filename>` | Rename a document. In-process only. |
| `doc delete <id> [--yes] [--privacy-erasure]` | Delete a document and its owned data (confirms unless `--yes`). `--privacy-erasure` also deletes Lab snapshots, runs, scores and labels and removes the translation memory rows from this document (the rest of the project TM stays). Otherwise warns when Lab snapshots retain text and keeps shared project TM. Failed upload erasure is reported as pending and retried automatically. In-process only. |
| `doc translate <doc_id> --locale LOC` | MT pre-translate **synchronously in-process** (see note below). The **global** `-v`/`--verbose` (placed *before* the subcommand, e.g. `-v doc translate …`) prints a per-segment line - it is not a `doc translate` flag. When every segment fails (a dead engine), the command records a FAILED job with the first engine error and exits non-zero; a partial failure (some segments failed) also records a FAILED job and exits non-zero; the translated segments stay saved and running the command again retries only the failed ones, and `doc export` holds that locale until they are retried (`--allow-held` ships it source-filled). |
| `doc export <doc_id> --locale LOC [-o PATH] [--force] [--allow-source-fallback] [--allow-held] [--as-fmt po]` | Rebuild the format-preserving translated file. `--as-fmt po` delivers a gettext `.mo` document as its `.po` source instead (API `as_fmt=po`; any other format answers a usage error). Download API: 401 without a token; 400 when locale is missing or blank. **`--allow-held`:** a locale with no committed translation is refused by default (the file would be entirely source text that reads as delivered); pass `--allow-held` to ship the source-filled file explicitly (the default refusal names it). Defaults to `<stem>.<locale>.<fmt>`; audited. **The inline-tag guard runs on EVERY format (BUG-04).** It used to be skipped entirely unless the document's *source* already carried an inline-formatting token - true only for DOCX/XLIFF/HTML - so on Markdown/text/PO/SRT/JSON/XLSX the `export_tag_damage_policy` setting was never consulted, and a model that **invented** mask notation on a tagless source shipped it verbatim into the customer's file (measured in production, escaped here: `Cliquez sur \u27e6g1\u27e7Enregistrer\u27e6/g1\u27e7`, flagged `tag_integrity` MAJOR and `needs_review`, delivered anyway). A segment is now substituted when its tags diverge from its source **or** - narrowly - when it sits in `needs_review` carrying a `tag_integrity` flag that names an **unconfirmed placement** (a best-guess point-order restore or an unverified AI repair: the text is structurally clean, so re-deriving the verdict from it cannot see the doubt). A `tag_integrity` flag naming a *structural* break on text that now checks clean is **stale** - it describes a previous version of the segment - and is deliberately ignored, because `commit_mt_output` never clears the flag on a later clean write, so honouring it would replace a correct translation with English. A genuinely tagless, undamaged document is byte-identical to before. **Substitution reporting (#41):** when the inline-tag guard replaces a damaged segment's translation with its SOURCE text, the file is still well-formed and leaks no tokens - so the fact is reported instead of being silent: a `WARNING:` line naming what was substituted, and a `substituted` array in `--json`. Each entry is a self-describing record `{"unit": "segment"|"paragraph", "index": N, "kind": "automatic"|"explicit_override"}` - `unit` because the same channel carries segment `order_index`es (SEGMENTED) and paragraph indices (a FULL_TEXT `--allow-source-fallback` export), and `kind` because an audit row must distinguish an operator's explicit force-export from a routine automatic substitution. `--remote` reports the same fact as `substituted_count` plus `substituted_detail` (read from the `X-Export-Substituted` / `X-Export-Substituted-Detail` response headers, since the body is the file itself) and names the same **unit** as the local arm. `--remote` also forwards `--allow-source-fallback` to the `/api` download route. Over `--remote` (and `/api`), `--allow-held` and `--allow-source-fallback` need a write role (translator or above): a viewer key gets a 403, never the source-filled file (#593). A clean export reports nothing. **`--allow-source-fallback` (#42, FULL_TEXT only):** an *explicit, per-export* opt-in - when a full-text paragraph's inline tags no longer match its source, export that paragraph's **SOURCE text** instead of quarantining the whole document. It is deliberately not a setting: a blanket full-text fallback would ship the entire document untranslated. Bounded by the `export_source_fallback_max_share` setting (default `0.35`) - above that share the override is **refused**, not warned, because "export anyway" would ship an essentially untranslated file that looks finished. It never applies to a paragraph-**count** desync, which still quarantines globally - and it applies ONLY to the tagged inline-tag case: a **tagless** full-text body with fewer real paragraphs than the source (or more, or a blank stored body) is refused loud (`kind:untranslated`/`count`, honest-failure 2026-08-13), never silently source-filled, because a count desync leaves no trustworthy positional map to substitute against; re-run or edit the body is the way out. The substituted paragraphs ride the same `substituted` array / `X-Export-Substituted` header as #41. `--out PATH` is the long form of `-o PATH`. |
| `doc empties <doc_id> --locale LOC` | **The delivery empty gate (Batch 15).** List a document's untranslated (**empty**) targets that the gate is holding back from delivery - the actual empty strings (source shown), not just a count. A deliverable produced by a `tm_match` step (the durable `WorkflowRun.used_tm_match` marker) that still carries empty targets is **held** from every export surface until a person approves shipping the empties. The hold keys on that durable RUN MARKER, not on a workflow shape, so a `tm_match -> llm_agent` workflow is held exactly like a TM-only one. **While a run is in flight over the `(document, locale)` the hold applies even WITH a standing approval** (issue #105): the approval is *suspended*, never revoked, and resumes on its own once nothing is running, covering the targets that are still empty then. Every export surface answers the same refusal (`409` on `/api` + web, non-zero exit on `doc export`, skip-and-report in the project ZIP), and a deliverable under back-to-back runs stays held for as long as runs keep starting - empties cannot ship while a run may still fill them. **A run is a run in either tier: a classic workflow run (a fan-out's member runs included, and the unfinished fan-out ITSELF only while that locale's current-attempt member is not yet created or is non-terminal; a re-run holds its selected locales from acceptance until their fresh members are terminal, and finished locales not selected still release) or an Advanced-Workflow run over the same project/document and locale.** An Advanced mission is committed RUNNING before its loop starts and leaves every target empty until its terminal delivery write; it matches on the mission's own bound segments, so a mission over one document does not hold its siblings, and the refusal names the tier (`Advanced-Workflow run #12`). A fan-out (**batch**) follows that same per-locale rule: members are created lazily, one per launch-loop turn. The refusal names it (`batch #3`). Wait for the batch to finish; `loc-tms db recover-jobs` finalizes batches with a confirmed-dead dispatcher on this host (install-wide). Manual recovery finalizes only batches with confirmed-dead local dispatcher processes. Live or unknown owners remain untouched. For unknown owners, only if the server died, after confirming no worker is live, restart the server to recover interrupted batches. Web/api parity: `GET /documents/{id}/empties`, `GET /api/documents/{id}/empties`. |
| `doc deliver-approve <doc_id> --locale LOC` | **The delivery empty gate (Batch 15).** Approve delivering a held `(doc, locale)`'s empty targets as **empty strings** (not source text), audited (`delivery.empty_approved`). Covers the empty set the approver saw - a later run that adds NEW empties re-holds until re-approved. This hold is **not** bypassable with `--allow-held` (only this audited approval releases it). **Refused (`400`, `/api`; non-zero exit on the CLI) while a run is still in flight over the `(document, locale)`** (issue #105): mid-run those targets are empty because the run has not reached them, not because they are final, so an approval would cover segments no human has seen and ship a blank file. Wait for the run to finish, or cancel it. **A run is a run in either tier: a classic workflow run (a fan-out's member runs included, and the unfinished fan-out ITSELF only while that locale's current-attempt member is not yet created or is non-terminal; a re-run holds its selected locales from acceptance until their fresh members are terminal, and finished locales not selected still release) or an Advanced-Workflow run over the same project/document and locale.** Web/api parity: `POST /documents/{id}/deliver-approve`, `POST /api/documents/{id}/deliver-approve`. |
| `doc deliver-revoke <doc_id> --locale LOC` | **The delivery empty gate (Batch 15).** Revoke a prior empty-delivery approval for a `(doc, locale)` → re-held (audited `delivery.empty_revoked`). A revoke with no standing approval is a no-op (already held by the live default). Web/api parity: `POST /documents/{id}/deliver-revoke`, `POST /api/documents/{id}/deliver-revoke`. |
| `doc paragraphs --doc N --locale LOC` | **FULL_TEXT only (#42).** Split the one giant full-text unit back into its real paragraphs and show, per paragraph, the source, the target, the tag verdict (`reason` - `quality.tag_damage`'s own string, the SAME predicate the commit gate and the export guard use) and the token delta (`missing` / `extra`). Read `reason`: the damage classes that keep the token multiset intact - an emptied paired span, broken nesting - have an EMPTY delta on both sides, and `reason` names what fired. **Conservative quarantine semantics:** ANY tag divergence (including a shape-preserving reorder) counts as damage - shown red, export-blocking, and offered a revert; the recovery surface is the mitigation. Human output names the tokens ASCII-transliterated (`r1, /r1`); `--json` carries the real tokens plus `rows[]` (each with `reason`, `missing`, `extra`, `damaged`), `damaged_count`, `damaged_indices`, `damaged_share`, `reverted_indices`, `fallback_share`. `damaged_*` counts only paragraphs the export blocks on, so the view always agrees with the export. Web/api twins: `GET /documents/{id}/paragraphs` and `GET /api/documents/{id}/paragraphs` - all three call `services.fulltext_paragraphs.split_view`. **`--remote`-capable** (it reads the same `/api` route and renders the identical block through `_common.render_paragraph_view`). A paragraph **count** desync (guard A) is refused with a clear error: the positional map is scrambled for every later paragraph, so no per-paragraph view is trustworthy and no revert is offered. |
| `doc revert-paragraph --doc N --locale LOC [--index I] [--all-damaged]` | **FULL_TEXT only (#42).** Replace one damaged paragraph's translation with its **SOURCE text** so a quarantined document can be delivered at all: the source tokens are correct by construction, so the formatting survives and that paragraph is *visibly* untranslated rather than invisibly broken. Not a quiet fix - it forces the segment to `needs_review`, records a durable `source_fallback` QA flag naming every reverted paragraph, makes the segment **TM-ineligible** (`tm.write_back` / `tm.ft_store` refuse it, so approving cannot launder it into shared TM), and writes a `document.paragraph_revert` audit row carrying the untranslated share. Refused for an undamaged paragraph, and **refused above the `export_source_fallback_max_share` ceiling** (`share_ceiling`, HTTP 409) - the same limit `doc export --allow-source-fallback` enforces. `--all-damaged` reverts every damaged paragraph **atomically**: it pre-checks the whole batch against the ceiling and writes NOTHING on a refusal (never a partial revert). Reverted paragraphs are reported by **every later export** through the `substituted` channel (`kind=explicit_override`, `unit=paragraph`), so the `X-Export-Substituted` header, the audit `detail` and the ZIP's `_SUBSTITUTED.txt` all name them. Web/api twins: `POST /documents/{id}/paragraphs/{i}/revert`, `POST /api/documents/{id}/paragraphs/{i}/revert` (and `POST /api/documents/{id}/paragraphs/revert-all-damaged` for the atomic batch). **`--remote`-capable.** |
| `doc set-paragraph --doc N --locale LOC --index I (--text T \| --from-file P) [--confirm]` | **FULL_TEXT only (#42).** The hand repair: replace one paragraph's translation, inline `⟦rN⟧` tokens included (`--from-file` reads UTF-8, so the tokens need not survive a shell). A save that would break whole-body inline-tag integrity is **refused** unless `--confirm`, exactly like the editor's save guard. A clean save clears the stale `tag_integrity` badge and drops that paragraph from the `source_fallback` flag **only when the saved text actually differs from the source** - re-stating the source text is the fallback again by hand, not a repair (the editor has a "Copy source" button one click above the textarea). Web/api twins: `POST /documents/{id}/paragraphs/{i}/save`, `POST /api/documents/{id}/paragraphs/{i}/save`. **`--remote`-capable.** |
| `doc apply-tm <doc_id> <locale>` | Fill in target text for every segment with a **100% TM match** - CLI parity with the web `POST /documents/{id}/apply-tm` button; both call the same `content.apply_tm_to_document()`. Reports how many segments were filled. **`--remote`-capable** (BUG-08, fixed 2026-07-29): previously `--remote` was refused outright (no `/api` route existed). New `POST /api/documents/{id}/apply-tm` (can_write-gated, org+customer scoped like its doc-route neighbours) + a `--remote` CLI arm. `prompt-tap show`/`export` stay host-only by deliberate admin posture (a policy call, not a bug - see HANDOFF). |
| `doc unlock-segment <segment_id>` | Clear the **TM lock** on one segment (audited `tm.unlock`). The reviewer-facing half of the `tm_match` lock lifecycle (PROPOSAL-tm-matching §5): a hard lock keeps LLM/MT steps off a matched segment, and this is the explicit way a human removes it. Web/api parity: `POST /segments/{id}/unlock` and `POST /api/segments/{sid}/unlock` are gated **reviewer+** (`security.can_review` - reviewer/linguist/manager/admin; a translator or viewer is refused 403), because clearing a hard TM lock is a reviewer+ action. Available in-process and over `--remote` (R-MED-11, driving the API route): the in-process operator is always allowed, while the `--remote` arm hits the reviewer+-gated API route and needs a reviewer+ key. A plain human CAT-editor / key-grid **edit** of a locked segment ALSO clears the lock automatically (human edit wins). |
| `doc approve-segment <segment_id> [--acknowledge-placeholder-loss] [--no-propagate]` | Approve a reviewed/edited segment and **save its source→target pair to TM** (audited `segment.approve`). The operator/CLI half of the CAT "approve" action - the same shared seam the web editor and `POST /api/segments/{id}/approve` use, so the **refusal/language guards** run identically: a suspected hedge or non-translation is WITHHELD from shared TM (`tm_written:false` with a `tm_skipped` reason) while the segment keeps its approved target. The same withhold applies when the target drops or adds a real placeholder such as `{count}` (reason `placeholder integrity broken`, `tm_skipped_code:"placeholder_integrity"`); `--acknowledge-placeholder-loss` saves it to TM anyway. Inline tags are stricter: a tagged source or a target whose inline tags differ from the source is never saved to TM (`tm_skipped_code` `tagged_source` or `tag_integrity`) and the flag does not override it. Every withhold carries a `tm_skipped_code` (`placeholder_integrity`, `tag_integrity`, `tagged_source`, `source_fallback`, `empty_target`, `suspected_refusal`, `language_mismatch`, `tm_write_refused`). Web parity: `POST /segments/{id}/approve` (visibility-scoped + **can_write** - a viewer is refused; the web route is gated the same way by the login-wall middleware, and `POST /api/segments/{id}/approve` self-applies `_write_block`). Works **in-process and `--remote`** (the remote arm drives `POST /api/segments/{id}/approve`). **Repeat propagation (Issue #617):** approving also applies the target to the document's other repeats of the same source and target language (the same normalization the INCONSISTENT check uses) that are not approved, not edited by a person, not TM-locked and not unlinked; they become *edited* with a propagated marker, never approved. The output says `Applied to N repeats (segments ...)`; a repeat whose placeholders or inline tags the text would break is left unchanged and listed (`propagation_refused`). `--no-propagate` approves only this segment. One audited `segment.propagate` entry lists the ids. |
| `doc segment-propagation <segment_id> (--unlink \| --link \| --undo)` | Repeat-propagation controls of one segment (audited; web `POST /segments/{id}/propagation`, `POST /api/segments/{id}/propagation`). `--unlink` keeps a context-specific rendering safe: the segment neither receives nor sends propagated text (persisted). `--link` undoes that. `--undo` restores the repeats the segment's last approval propagated, but only those still exactly as propagated (a repeat someone edited since is kept). Works in-process and `--remote`. |

> **`doc translate` runs synchronously.** A one-shot CLI process has no daemon
> job workers (they die with the process), so `translate` does **not**
> `jobs.submit()` and exit. It consumes `translate.pretranslate_stream()` directly
> in the calling thread, so when the command returns the translation is really
> done. On the mock engine (no provider key) each segment is filled with a
> deterministic `[<locale>] …` placeholder.

> **JSON5 / JSONC documents** (`.json5`, `.jsonc`): `doc upload` translates string values by the JSON
> rule; comments, trailing commas, unquoted keys, quote style and untouched bytes stay identical on
> `doc export`. A `.json` with comments is refused as JSON: rename it `.jsonc`. Same on web, API and MCP.
> **MDX documents** (`.mdx`): `doc upload` translates the Markdown prose; `import`/`export` blocks,
> block JSX and block `{expressions}` stay as they are, inline components and `{expressions}` are
> protected tags, and only the allowlisted component attributes (`alt`, `aria-label`, `caption`,
> `label`, `placeholder`, `title`; host setting `LOC_TMS_MDX_ATTRS`) are segments. Same on web, API and MCP.
> **Markdown / MDX front matter** (YAML `---` or Hugo TOML `+++` at the start of the file): keys, dates,
> booleans, ids, slugs, permalinks and layouts stay as they are; only the one-line text values of
> `title`, `description`, `summary`, `excerpt`, `subtitle`, `sidebar_label`, `pagination_label` and `linkTitle`
> (host setting `LOC_TMS_FRONT_MATTER_KEYS`) are segments, quoted on export when needed (#731).
> **Markdown / MDX block structure** (#735): link reference definitions (`[label]: url "title"`) keep
> their label and URL, only the title is a segment (written back escaped inside its own quotes); a
> footnote definition keeps its `[^label]: ` and its text is the segment; indented code blocks (Markdown
> only) and comment-only `<!-- ... -->` blocks are skipped.

> **YAML documents** (`.yaml`/`.yml`: generic, Rails i18n, Symfony): `doc upload` translates string
> values only (keys, comments, anchors and untouched quoting stay byte-identical; yes/no/on, numbers and
> dates are skipped). `doc export` renames a Rails file's root locale key (`en:`) to `--locale`; Symfony
> keeps the locale in the file name. Same behaviour on web, API and MCP.
> **Subtitles: SSA / ASS (`.ssa`, `.ass`).** `doc upload` translates only the Text field of each `Dialogue:` line (the last Format field, commas allowed) and the `[Script Info]` `Title:`; override tags `{\i1}`, brace comments and `\N` `\n` `\h` are protected inline tokens, and `Comment:` lines, styles, timings and every other line export byte for byte.

**Translation memory**

| Command | What it does |
|---------|--------------|
| `tm import <project_id> <file.tmx> --source LOC --target LOC` | Import a TMX (or a Trados Studio `.sdltm`, recognized by its SQLite header and read read-only through the same import; `--json` adds `format`, `skipped_unreadable`; unreadable units are listed with `sdltm_*` codes) into the project's segment TM. A two-column spreadsheet TM (`.xlsx`, `.csv`, `.tsv`: source then target, or a header naming source/target or the two language codes) is accepted too and converted to TMX first, so every rule below applies to it. Streamed (`iterparse`) and capped at `MAX_UPLOAD_BYTES`; a file declaring XML entities is refused (amplification guard, same rule as `.tbx`); a malformed file exits 1 with a plain message. **Effective-locale reporting (BUG-03, landed `a95a402`):** the success line and `--json` (`source`/`target`) now state the locales the rows were actually **stored** under, not the ones asked for - a base-language pair (`en`/`es`) is coerced to the project's real pair (e.g. `en-US`/`es-ES`) at the service seam and echoed as `requested_source`/`requested_target` + `locales_normalized: true`, so a public TMX corpus's bare `en`/`es` no longer looks like it landed in a bucket the project can actually match. An **unrelated or ambiguous** pair (neither exact nor coercible) is refused **before any write** - a non-zero exit (1) with a plain message, never a silent "0 imported" or a never-matching bucket. **Integrity gate (Issue #567, same rules as `doc approve-segment`):** a unit whose target drops or adds a placeholder (`{0}`, `%s`, `{{name}}`) is withheld (`placeholder_integrity`) unless you pass `--acknowledge-placeholder-loss`; a unit with inline tags (`tagged_source`) or damaged tags (`tag_integrity`) is always withheld. The import still completes: the summary says `withheld N`, `--json` carries `skipped_integrity`, `skipped_by_code` and `skipped_rows` (first 20 with row number, code, reason), and the human output lists them. **Chunked writes (Issue #615):** a file with more than 2,500 rows to write is stored in short transactions (`write_chunks` in `--json`); one that fails part way keeps the stored rows, exits 1 saying how many, and the same file re-imports to completion with no duplicates. Each chunk re-reads its rows when it writes, so the file's target is stored over an edit made while the import runs (as a re-run would). A TM entry someone deletes while the import runs is not restored: the summary says `Did not restore N TM entries someone deleted`, `--json` carries `concurrent_deleted`. |
| `tm export <project_id> <file.tmx> --source LOC --target LOC [--force]` | Export the project's segment TM to TMX (refuses to overwrite without `--force`). |
| `tm list [--order recent\|oldest\|most_used\|least_used\|recently_used] [--page N] [--per-page N]` | Browse TM entries with usage data; default order `recent`, page 1, 50 entries per page (max 500). `--query TEXT` matches a substring in source or target. See [Translation memory filters](#translation-memory-filters). Remote-capable. |
| `tm search <text> [--page N] [--per-page N]` | Search source/target text by substring; page 1 and 50 entries per page by default (max 500). See [Translation memory filters](#translation-memory-filters). Remote-capable. |
| `tm show <entry_id>` | Show one entry's source, target and usage stats. Remote-capable. |
| `tm edit <entry_id> --target TEXT [--acknowledge-placeholder-loss]` | Replace the target text in place. The source is the match key and cannot be edited. Remote-capable. The edit is refused (exit 1, code `placeholder_integrity`) when the new target drops or adds a placeholder, unless `--acknowledge-placeholder-loss`; inline-tag breakage (`tag_integrity`, `tagged_source`) is always refused (Issue #567, same gate as approve). |
| `tm stats` | Summarize totals, language pairs and usage. Remote-capable. |
| `tm delete [entry_id] [--customer ID] [--query TEXT] [--dry-run] [--yes]` | Delete one entry, or omit the id to delete by filter. A bulk delete only ever touches one customer's TM: `--customer` is required when you can see more than one customer, unless `--project` names a single project (otherwise it is refused and nothing is deleted, dry-run included). `--query` matches source/target substrings; `--dry-run` reports the count without deleting; `--yes` skips confirmation. See [Translation memory filters](#translation-memory-filters). Remote-capable. |
| `tm scrub-hedges [--customer ID] [--project ID] [--apply]` | Scan one customer's stored segment + full-text TM for refusal hedges. `--customer` is required when you can see more than one customer, unless `--project` is given. Report only unless `--apply` removes the poisoned entries. Remote-capable. |

#### Translation memory filters

These filters are shared by `tm list`, `tm search` and `tm delete`. They narrow
the same browse set; `tm search` takes the search text positionally, while
`tm list` and `tm delete` take `--query TEXT`.

| Flag | What it does |
|------|--------------|
| `--customer ID` | Scope to one customer's TM. |
| `--project ID` | Filter by project. |
| `--source LOC` / `--target LOC` | Filter by source / target locale. |
| `--content-type ID` | Filter by content type; `general` / `none` selects the untyped bucket. |
| `--created-before YYYY-MM-DD` / `--created-after YYYY-MM-DD` | Entries created before / on or after the given date. |
| `--min-use N` / `--max-use N` | Entries used at least / at most N times. |
| `--unused` | Only never-used entries (`use_count` 0). |

**Glossary**

| Command | What it does |
|---------|--------------|
| `glossary import <project_id> <file.csv\|file.tbx\|file.xlsx\|file.tsv> --source LOC --target LOC` | Import a glossary **CSV or TBX** (an **XLSX or TSV** is converted to the glossary CSV first: header `source_term,target_term,...`, or `source`/`target`, or the two language codes; no header reads as source, target). CSV columns: `source_term,target_term,dnt,sense_label,context_cue` (+ the optional TBX-ish meta columns). A **`.tbx`** path is parsed as a native termbase - both dialects in the wild (TBX 2.0/`martif` `termEntry`/`langSet`, as Microsoft Terminology and IATE emit, and TBX v3 `conceptEntry`/`langSec`), namespaced or not. `--source`/`--target` select the `xml:lang` pair (exact match first, then a base-language fallback: a `es` langSet satisfies `es-ES`; when several sections match a pair only by base language, e.g. both `es` and `es-419` for an `es-ES` pair, the generic base tag `es` wins, else the lexicographically-first tag - deterministic, not document order, and matching the module `.glossary.json` concept arm); entries missing either side are skipped and the count is reported. `termNote type="partOfSpeech"` → `part_of_speech`, `descrip type="definition"` → `definition`, `descrip type="subjectField"` → `subject_field`, `termNote type="doNotTranslate"` and the house `los:dnt` element (value `true`/`yes`/`1`, case-insensitive; anything else, including `false`/`no`/garbage, reads as false) → `dnt=true` - a **private** extension our own export emits, read from the SOURCE side only. It is not a TBX-Core data category, so a tolerant third-party processor ignores it but a strictly validating one may reject the file; a foreign TBX without it still imports `dnt=false` as before. Other fields are ignored, and TBX still has no sense model (imported terms carry no senses). **Re-importing is idempotent**: a term is matched by source term and a sense by its `sense_label`, so the same file twice updates in place and never duplicates a row (fixed 2026-07-29 - sense rows used to be inserted blindly, so every re-import added another copy of every sense). Streamed (`iterparse`) and capped at `MAX_UPLOAD_BYTES`; a malformed file exits 1 with a plain message. `--remote` sends the bytes unchanged and the server picks the format. **Chunked writes (Issue #615, ADR 0061):** a file with more than 2,500 entries is stored in short transactions (`write_chunks` in `--json`, `Written in N short transactions` in the text), so other writers are never held up for the whole import; one that fails part way keeps the stored entries, exits 1 saying how many (`--json`: `partial`, `rows_written`, `rows_total`; the message names the error class, never the database text), and the same file re-imports to completion with no duplicates. Each chunk re-reads its entries when it writes, so the file's values are stored over an edit made while the import runs and a field the file leaves blank keeps its current value. An entry someone deletes while the import runs is not restored: the summary says `Did not restore N glossary entries someone deleted`, `--json` carries `concurrent_deleted`. **Termbase dialects (2026-10-10):** the same import also reads **MultiTerm XML** (`<mtf><conceptGrp>`, a `.xml`/`.mtf` file, UTF-16 with a BOM is fine; `<language lang="EN-US">` codes are normalized to BCP 47 casing and a name-only `<language type="German">` is mapped) and the **TBX 2002 default dialect** MultiTerm exports (`martif type="TBX"`, field labels as `descrip` types such as `Definition`, `Part of Speech`, `Subject`, `Context`, `Note`), plus TBX-Basic and TBX v3. Mapped: definition, subject field, part of speech, context (to `usage_example`) and notes. A term whose usage status is forbidden or deprecated (`administrativeStatus`, `normativeAuthorization`, or a MultiTerm `Status` field) is never used as the target and never becomes a source row: the preferred (else first usable) term is the target. A module glossary keeps the do-not-use terms as forbidden/deprecated terms on the concept (one an existing binding still enforces is left for the concept editor); a per-project glossary lists them in the entry's notes, on a new term or after the file's own note, never over a stored note the file does not replace. Usage status words are matched with negation in mind (`Disallowed` and `Not permitted` are forbidden, `Not recommended` deprecated, `Not approved` unranked). A context or note the file carries replaces the stored one (the file wins, as for CSV); a field the file leaves blank keeps its value. The result carries a per-entry **`report`** (`imported`, `merged` into existing terms, `skipped` with each reason, `entries_detail` for the first 500 entries); the CLI prints it, the web page shows the totals. |
| `glossary add <project_id> <source_term> [--target-term T] [--source LOC] [--target LOC] [--dnt]` | Add/update one term (audited). |
| `glossary list <project_id> [--limit N] [--offset N] [--cursor ID]` | List a project's entries (id, source, target, locales, DNT, sense count). `--limit`/`--offset` page the list (capped at 1000 with a truncation note, never a silent cutoff); a non-positive `--limit` or negative `--offset` is refused (exit 2). Follow `next_cursor` with `--cursor ID`, resetting `--offset` to 0; malformed or non-positive cursors exit 2 (cursor must be a positive integer). |
| `glossary export <project_id> <out.csv\|out.tbx> [--force]` | Export a project's glossary (round-trips `import`; includes the TBX-ish meta columns). **The output path picks the format**: `.tbx` writes a TBX v3 / TBX-Core termbase, anything else CSV. CSV is the **lossless** one - TBX-Core has no home for our context-dependent senses, `usage_example` or `notes`, so a `.tbx` export carries terms plus `part_of_speech`/`definition`/`subject_field` and (via a private `termNote type="doNotTranslate"`) the DNT flag; senses/`usage_example`/`notes` stay CSV-only. A DNT term with **no target term** still exports source-only (a valid single-language TBX concept) and re-imports as a skipped entry either way - the DNT flag round-trips only when the term also has a target. An empty project glossary exports a valid empty TBX file (or the header-only CSV), never an error (#604); a module glossary's TBX/JSON export of an empty glossary is still refused (422). Refuses to overwrite without `--force`. Web parity: `GET /projects/{id}/glossary/export[?format=tbx]`. **Remote-capable** (R-MED-11): `--remote` is wired to `GET /api/projects/{id}/glossary/export` (the route was added after the CLI arm; the old "no `/api` route / `require_local`" note was stale). |

**Glossary module** - `glossary module …` - the dedicated, first-class glossaries (customer-owned, approval lifecycle, language-pair association, workflow-step dropdown; `app.services.glossary_module`). Distinct from the per-project `glossary` verbs above. **`--remote` (DATA-SAFETY chip, 2026-07-29; `extract` added R-MED-11)**: every verb in this table, including `extract`, is `--remote`-capable - each dispatches to the matching `/api/glossaries…` route instead of the local DB (`extract` to `POST /api/glossaries/{id}/extract`, spend-capped and can_tune_ai-gated same as local). Before the 2026-07-29 fix these verbs silently ignored `--remote` and ran against the local host while reporting success; a parser-tree guard test (`tests/verify_cli_remote_dispatch_guard.py`) now asserts every CLI leaf either dispatches on `--remote` or refuses it loudly, so this bug class cannot silently return.

TBX import reads the house field `los:dnt` (`true`/`1`/`yes`, case-insensitive) as DNT, alongside legacy `doNotTranslate` notes. Any recognized truthy marker at any source metadata level sets DNT; no marker ever clears it. Other fields retain narrower-source precedence.

| Command | What it does |
|---------|--------------|
| `glossary module create <name> --customer ID [--content-type ID]` | Create a **draft** glossary owned by a customer (customer is **required** - every module glossary belongs to one). Optional content-type scope (SPEC-content-types). |
| `glossary module list [--customer ID] [--unassigned]` | List glossaries in scope; `--customer` filters to one; `--unassigned` shows only legacy glossaries with no customer. Both filters are applied **server-side** over `--remote` too (`GET /api/glossaries?customer=ID` / `?unassigned=1`). |
| `glossary module show <glossary_id> [--limit N] [--offset N] [--cursor ID]` | Show one glossary (status, language pairs, terms). `--json`'s `terms` array (full term rows incl. senses/meta) is now also carried by `GET /api/glossaries/{id}` - previously that route returned only the summary, so `--remote glossary module show` had no term rows to render. Entries are paged (default/hard cap 1000): advance `--offset`, or follow `next_cursor` with `--cursor ID` and reset `--offset` to 0. A non-positive limit/cursor or negative offset is refused (exit 2). |
| `glossary module set-customer <glossary_id> <customer_id>` | One-time assignment of a legacy unassigned glossary to a customer (never a reassignment). |
| `glossary module set-content-type <glossary_id> [content_type_id] [--clear]` | Set or clear the glossary's content type. |
| `glossary module set-pairs <glossary_id> --pair SRC:TGT …` | Replace the glossary's language pairs (`--pair` repeatable). **Widening** the set (adding a pair) auto-creates the missing per-pair bindings for every concept that has a usable source + target for the new pair; `--json` (and the `/api` / MCP response) report `bindings_created` and `dnt_bindings_extended` (a DNT-only source binding), and the web redirect carries the counts in its `?msg=` banner. Narrowing (dropping a pair) leaves the existing bindings in place; an idempotent resubmit creates nothing. |
| `glossary module approve <glossary_id>` | Approve a glossary so it can be pinned on a workflow step. Remote-capable. |
| `glossary module unapprove <glossary_id>` | Revert an approved glossary to draft. Remote-capable. |
| `glossary module add-term <glossary_id> <source_term> [--target-term T] [--source LOC] [--target LOC] [--dnt] [meta…]` | Add a term. TBX meta flags: `--part-of-speech`, `--definition`, `--usage-example`, `--subject-field`, `--notes` (metadata is organizational - **never reaches a prompt**). |
| `glossary module edit-term <glossary_id> <entry_id> [--target-term T] [--dnt\|--no-dnt] [--case-sensitive\|--no-case-sensitive] [meta...]` | **PARTIAL** edit of a term: `--target-term` and the meta flags leave a field unchanged when omitted, and **clear** it when passed `''`; `--dnt`/`--no-dnt` and `--case-sensitive`/`--no-case-sensitive` are tri-state toggles (omitted = unchanged). `--case-sensitive` sets the per-pair case-sensitivity fact on the binding (A.3); it is carried by all four surfaces (row editor, CLI, `/api` `POST /api/glossaries/{gid}/terms/{eid}/edit`, MCP `edit_glossary`). Metadata flags: `--part-of-speech P`, `--definition TEXT`, `--usage-example TEXT`, `--subject-field TEXT`, `--notes TEXT`. |
| `glossary module import <glossary_id> <file.csv\|file.tbx\|file.glossary.json> --source LOC --target LOC [--strict] [--concepts]` | Import a **CSV, TBX-Basic, or `.glossary.json`** into an existing module glossary. **CSV and (by default) TBX take the per-pair entry path** (entries/senses, idempotent - the same file byte-identical is a no-op that leaves an approved glossary approved; `--json` reports `changed`/`unchanged`/`updated`/`skipped_rows`). A base-language TBX (`xml:lang="es"`) projects onto a regional pair (`es-ES`) by base language; when several sections match a pair only by base language (e.g. both `es` and `es-419` for an `es-ES` pair) the generic base tag (`es`) wins, else the lexicographically-first tag, and the flat entry and `.glossary.json` concept arms agree (the tie-break is deterministic, not document-order - a change from earlier releases, which took the first matching section in document order). **`--concepts`** (TBX only) is **RESERVED**: the concept-PRESERVING TBX import (every language, all synonyms, senses, every §6.1 data category, LAW-TERM-9) is **deferred to the terminology authority train**, so the flag is **refused before any parse or write** ("Concept-preserving TBX import arrives with the terminology authority train; use the standard import.") on every surface - use the default flat entry path, or a `.glossary.json` round-trip to carry the concept store losslessly meanwhile. The **`.glossary.json`** import writes the concept store directly (result `concepts`/`terms`/`projected_entries`/`dropped_refs`/`truncated_fields`/`id_conflicts`, no `entries`/`senses`); `projected_entries` reports how many entries **this import's terms** enforce for the glossary's pair(s) (a re-import reports the subset its own terms cover, not a whole-glossary total). An import **reverts an approved glossary to draft** only when it CHANGED content; a byte-identical re-import (any format) leaves it approved. **`--strict`** (TBX only) validates the file against the vendored TBX core RELAX NG and an in-repo evaluator of the vendored TBX-Basic DCA Schematron and **refuses the first finding, writing nothing** (Decision D); without it, findings are warnings. Streamed + size-capped; a malformed/invalid file exits 1 with a plain message. After the T1b authority inversion a `.glossary.json` import is **allowed on any glossary regardless of the `terminology_concept_editor` flag** (it upserts only the named concepts and re-imports idempotently, reconciling id-less concepts by content identity); the flag now gates only the concept **editor** (create/edit/delete concept and term, set audience). **Chunked writes (Issue #615, ADR 0061):** a file with more than 250 entries is stored in short transactions (`write_chunks` in `--json`, `Written in N short transactions` in the text), so other writers are never held up for the whole import; one that fails part way keeps the stored entries, exits 1 saying how many (`--json`: `partial`, `rows_written`, `rows_total`; the message names the error class, never the database text), and the same file re-imports to completion with no duplicates. Each chunk re-reads its entries when it writes, so the file's values are stored over an edit made while the import runs and a field the file leaves blank keeps its current value. An entry someone deletes while the import runs is not restored: the summary says `Did not restore N glossary entries someone deleted`, `--json` carries `concurrent_deleted`. A partial import of an approved glossary still reverts it to draft. **Termbase dialects (2026-10-10):** the same import also reads **MultiTerm XML** (`<mtf><conceptGrp>`, a `.xml`/`.mtf` file, UTF-16 with a BOM is fine; `<language lang="EN-US">` codes are normalized to BCP 47 casing and a name-only `<language type="German">` is mapped) and the **TBX 2002 default dialect** MultiTerm exports (`martif type="TBX"`, field labels as `descrip` types such as `Definition`, `Part of Speech`, `Subject`, `Context`, `Note`), plus TBX-Basic and TBX v3. Mapped: definition, subject field, part of speech, context (to `usage_example`) and notes. A term whose usage status is forbidden or deprecated (`administrativeStatus`, `normativeAuthorization`, or a MultiTerm `Status` field) is never used as the target and never becomes a source row: the preferred (else first usable) term is the target. A module glossary keeps the do-not-use terms as forbidden/deprecated terms on the concept (one an existing binding still enforces is left for the concept editor); a per-project glossary lists them in the entry's notes, on a new term or after the file's own note, never over a stored note the file does not replace. Usage status words are matched with negation in mind (`Disallowed` and `Not permitted` are forbidden, `Not recommended` deprecated, `Not approved` unranked). A context or note the file carries replaces the stored one (the file wins, as for CSV); a field the file leaves blank keeps its value. The result carries a per-entry **`report`** (`imported`, `merged` into existing terms, `skipped` with each reason, `entries_detail` for the first 500 entries); the CLI prints it, the web page shows the totals. |
| `glossary module export <glossary_id> <file.csv\|file.tbx\|file.glossary.json> [--dialect tbx-basic\|tbx-core] [--strict-portable] [--force]` | Export a module glossary (round-trips with `glossary module import`). **The output path picks the format**: `.tbx` writes **TBX-Basic v3 (DCA style)** by default - the required `<tbxHeader>` (product/version/glossary/timestamp) plus every §6.1 data category, with house fields (DNT, audience, forbidden/superseded status, UI/label term types, subdomain, out-of-picklist part-of-speech/gender/number, usage note, case-sensitivity, exact-match) on a documented **`los:` private namespace**; `--dialect tbx-core` and `--strict-portable` drop `los:` (the latter also drops the `xmlns:los` declaration and emits a clean pure-TBX-Basic instance that validates with zero findings, naming its dropped fields). `.glossary.json` (or `.json`) writes the concept-aware `glossary-concept/v1` document, the **concept-lossless format**: every concept, term, status, audience, term-type, **homonym group (sense) and same-locale synonym** round-trips, and re-importing the product's own export upserts by the persisted `external_id` (so `export → import → export` is a fixed point). After T1b the export also carries a per-concept **`bindings`** block (the per-pair enforced target, DNT, case-sensitivity and `meta_overrides`); the concept-lossless / fixed-point guarantee applies to documents **with a `bindings` block** (the product's own export always emits one). A pre-T1b `.glossary.json` that has no `bindings` restores through the A.4-2 fallback, so a both-directions pair (LT3-1) is not lossless for such an older document. A **concept-format (TBX/JSON) export of an EMPTY glossary refuses** ("nothing to export"); CSV of an empty glossary is a valid header-only file. `audience` is enforced **source-side**. The module CSV header is **16 columns** (SPEC-terminology-backend §7.1, Decision G): the 5 core columns `source_term,target_term,dnt,sense_label,context_cue`, the 5 TBX-ish meta columns `part_of_speech,definition,usage_example,subject_field,notes`, and 6 concept-model term columns `status,term_type,audience,grammatical_gender,grammatical_number,geographical_usage` (filled from the entry's linked source term; `status` defaults to `preferred` and `audience` to `human,mt,llm` on any linked base row, the other four blank when unset; a sense row leaves the 6 blank). Refuses to overwrite without `--force`. Web parity: `GET /glossaries/{id}/export[?format=tbx][&dialect=][&strict_portable=1]`; `/api` `GET /api/glossaries/{id}/export?format=tbx`; `--remote` dispatches to the same `/api` route. |
| `glossary module rename <glossary_id> <name>` | Rename (does not revert approval). |
| `glossary module delete <glossary_id> [--yes]` | Delete a glossary (**destructive**; confirm / `--yes`). |
| `glossary module extract <glossary_id> <document_id> [--locale LOC] [--source LOC]` | **Extract candidate terms** from a document into the glossary's review queue (research Option C). Two-stage: the LLM extractor over-proposes, then a precision-prune pass + deterministic filters (min frequency, dedupe against existing terms + this glossary's dropped/accepted history) cut the noise; proposed translations fill the `--locale` target. **Nothing is auto-added** - candidates land **pending** for review. Spend-capped + mock-exempt (offline the deterministic mock proposes candidates, so the whole loop is hermetic); a blocked run refuses cleanly with no partial candidates. **Remote-capable** (R-MED-11): `--remote` is wired to **POST /api/glossaries/{id}/extract** (can_tune_ai). Web parity: **POST /glossaries/{id}/extract** (the glossary detail "Extract terms from a document" card). `--target LOC` is an alias for `--locale LOC`. With no API key on the default engine it exits 1 with a message that says to add the key (Setup AI, or `engine update <id> --secret api_key=-`); nothing is written. |
| `glossary module candidates list <glossary_id> [--status pending\|accepted\|dropped]` | List term candidates (default **pending** - the review queue), highest-frequency first. Columns: `id`, `source`, `proposed`, `locales`, `freq`, `status`. `--json` carries the full candidate array incl. the in-context snippet. Mirrors the glossary detail review grid. |
| `glossary module candidates accept <glossary_id> <candidate_id> [--target-term T]` | Accept a candidate into **this** glossary as a real term (customer-bound; reverts an approved glossary to draft). `--target-term` overrides the proposed translation; the in-context snippet is stored as the term's `usage_example` (metadata - **never reaches a prompt**). Web parity: **POST /glossaries/{id}/candidates/{cid}/accept**. |
| `glossary module candidates drop <glossary_id> <candidate_id>` | Drop a candidate. It **persists** as dropped so re-extraction **never re-proposes** it (the per-glossary noise-control backbone). Web parity: **POST /glossaries/{id}/candidates/{cid}/drop**. |

**Concept-level terminology** - `glossary module concepts …` / `terms …` - the concept/term rich store beneath the compat termbase (SPEC-terminology-backend D1/D2; `app.services.glossary_concepts`). A **concept** groups the terms that mean one thing across locales; a **term** is one locale-and-surface carrying `status` (preferred/admitted/deprecated/forbidden), `audience` (any subset of `human,mt,llm`), `term_type`, part of speech, gender/number, usage note, context example, DNT, and provenance. Every leaf delegates to the shared service (web/CLI parity) and is confined to the operator's org/customer scope (LAW-TERM-12). These verbs run **in-process only** - they refuse `--remote` loudly (exit 2); remote/programmatic access is the `/api/glossaries/{gid}/concepts…` routes. Concept and term **numeric ids** are shown by `concepts list` / `concepts show` (and in their `--json`); the `.glossary.json` export hides them behind stable document ids.

> **Concept EDITING is flag-gated (default off this release).** The write leaves below - `concepts add/edit/delete`, `terms add/edit/delete/set-audience` - ship behind the DEFAULT-OFF feature `terminology_concept_editor`, because the dual-write editor seams are not yet gate-clean; concept editing returns fully in **T1b** (authority inversion, per SPEC-terminology-backend). With the feature off each write leaf refuses (exit 2) naming the switch - as do the web/`/api`/MCP twins - while the **read** leaves (`concepts list/show`, `glossary module export … .glossary.json`) stay available; a `.glossary.json` **import** is allowed only as a **seed into an empty glossary** (no entries, no concepts) that has a language pair. Turn it on with `config feature enable terminology_concept_editor` (or Settings > Features / `POST /api/admin/features/{slug}`).

| Command | What it does |
|---------|--------------|
| `glossary module concepts list <glossary_id>` | List the glossary's concepts (`id`, `external_id`, `domain`, `definition`, term count). `--json` carries every concept and its full term rows (with numeric ids). |
| `glossary module concepts show <glossary_id> <concept_id>` | Show one concept and its terms (`term_id`, `locale`, `term`, `status`, `audience`, `type`, `dnt`). |
| `glossary module concepts add <glossary_id> [--definition D] [--domain D] [--subdomain S] [--notes N] [--external-id X] [--term LOCALE:SURFACE …]` | Create a concept, optionally seeding terms with the repeatable `--term LOCALE:SURFACE` (a value with no colon is a wildcard-locale surface). Prints the new concept id. |
| `glossary module concepts edit <glossary_id> <concept_id> [--definition D] [--domain D] [--subdomain S] [--notes N] [--external-id X] [--source S]` | Edit a concept's fields; an omitted flag is left unchanged. Reverts an approved glossary to draft. |
| `glossary module concepts delete <glossary_id> <concept_id> [--yes]` | Delete a concept and its terms (**destructive**; confirm / `--yes`). Runs the full delete graph (clears inbound deprecated-replacement refs, drops orphaned source/target terms and homonym siblings). |
| `glossary module terms add <glossary_id> <concept_id> --term T [--locale LOC] [--status ...] [--audience human,mt,llm] [--term-type ...] [--part-of-speech ...] [--usage-note ...] [--context-example ...] [--geographical-usage ...] [--grammatical-gender ...] [--grammatical-number ...] [--source ...] [--case-sensitive] [--exact] [--dnt]` | Add a term to a concept. `--term` (the surface) is required; every metadata flag is optional and takes the service default when omitted. Booleans are tri-state (`--dnt`/`--no-dnt`, etc.). `--no-case-sensitive` clears case-sensitive matching; `--no-exact` clears exact-surface matching. |
| `glossary module terms edit <glossary_id> <concept_id> <term_id> [--surface T] [--locale LOC] [--status ...] [--audience ...] [meta...]` | Edit a term; an omitted flag is left unchanged, and `--no-<flag>` clears a boolean. `--surface` changes the term's matchable surface. Metadata flags: `--term-type T`, `--part-of-speech P`, `--usage-note TEXT`, `--context-example TEXT`, `--geographical-usage T`, `--grammatical-gender T`, `--grammatical-number T`, `--source TEXT`. Boolean pairs: `--case-sensitive` / `--no-case-sensitive`, `--exact` / `--no-exact`, `--dnt` / `--no-dnt`. |
| `glossary module terms delete <glossary_id> <concept_id> <term_id> [--yes]` | Delete one term (**destructive**; confirm / `--yes`). The concept is removed too if it becomes termless. |
| `glossary module terms set-audience <glossary_id> --term TERM_ID [--term TERM_ID …] --audience human,mt,llm` | Bulk-set the `audience` of one or more of the glossary's terms (`--term` repeatable). Ids outside the glossary are skipped; prints the count updated. |

**Language Sets** (reusable named target-locale profiles)

| Command | What it does |
|---------|--------------|
| `langset create <name> [--source LOC] [--targets "es-ES,it-IT"]` | Create a Language Set. |
| `langset list` | List sets (id, name, source, targets). |
| `langset update <id> [--name N] [--source LOC] [--targets "…"]` | Update a set; `--targets` **replaces** the locale list (blank leaves it unchanged). |
| `langset delete <id>` | Delete a set (**destructive**; confirm / `--yes`). |

**Language catalog**

| Command | What it does |
|---------|--------------|
| `lang list [--all \| --enabled] [--search TEXT]` | List the enabled languages (default), or the whole BCP-47 catalog with enabled markers (`--all`). `--search` matches name, code or endonym. The last column, `dir`, is the writing direction (`rtl` or `ltr`); `--json` rows carry it as `direction`. |
| `lang enable <codes...>` | Enable catalog codes, e.g. `ja-JP hi-IN`. |
| `lang disable <codes...>` | Disable catalog codes. |

**Style guides** - `style-guide …` - the first-class, approvable, customer-scoped
long-form style guide packaged into translation prompts (SPEC-workflows-redesign Track B,
B2; `app.services.style_guides`). Resolves at the customer × content-type grain (optionally
per language pair); **only approved guides are injected**, as a fenced, sanitized,
budget-capped `<style_guide>` block. Body/scope edits revert an approved guide to draft.

| Command | What it does |
|---------|--------------|
| `style-guide create <name> [--customer ID] [--content-type ID] [--source LOC] [--target LOC] [--body TEXT \| --body-file PATH]` | Create a **draft** guide. Omit `--customer` for an **org-global** guide (applies to everyone). Leave both locales blank for a **universal** (pair-agnostic) guide; a per-pair guide needs both. |
| `style-guide list` | List guides in scope (id, name, status, customer, resolved scope). `--json` for machine output. |
| `style-guide show <id>` | Show one guide - scope, status, and the full body. |
| `style-guide update <id> [--name N] [--body TEXT \| --body-file PATH] [--content-type ID \| --clear-content-type] [--source LOC --target LOC \| --clear-pair]` | Update a guide. **Body and scope edits revert approval to draft** (it must be re-approved before it is injected again); rename does not. |
| `style-guide set-status <id> <draft\|approved>` | Approve a guide (records the approver; makes it eligible for injection) or revert it to draft. |
| `style-guide delete <id> [--yes]` | Delete a guide (**destructive**; confirm / `--yes`). Nothing references a guide by id at run time (resolution is by scope), so there is no in-use guard. |
| `style-guide ab --document ID [--locale LOC] [--provider P] [--max-segments N] [--yes]` | **A/B measure** whether the resolved style guide improves quality (Track B, B3). Runs the same segments with the guide **ON vs OFF**, holds context / glossary / TM constant, scores both with the QE judge, and prints the QE + style-error delta + a verdict. Read-only (never writes a target / TM). It makes paid provider calls (translate twice plus a judge per segment), so it first prints an estimate and runs only with `--yes` (without it nothing is sent and the exit code is 0, `--json` carries `confirmed: false`); a project over its budget cap is refused. A keyless install resolves to the offline mock (a structural test only); pin `--provider` at a live engine for a real number. Web parity: document page **"A/B test style guide"** → `POST /documents/{id}/style-ab`. |

**MT formality** - `formality …` - how formal the machine translation addresses the reader
(German "Sie" or "du", French "vous" or "tu", Japanese and Korean speech levels), set **per target
language** at three levels: a **customer** default, a **project** setting and a **document**
override. The most specific level wins (document, then project, then customer, then none); `auto`
means no instruction and, set at a level, stops the chain. The resolved register is one sentence in
the style-guide block of every MT prompt and is recorded on the run (`GET /api/runs/{id}` shows
`formality`). Web parity: the **MT formality** section on the customer, project and document pages.
`--remote` capable (`/api/formality`).

| Command | What it does |
|---------|--------------|
| `formality registers [--target LOC]` | List the registers: the whole catalog, or the values valid for one language (the generic `formal`, `informal`, `auto` plus that language's own, e.g. `sie` / `du` for `de`, `casual` / `polite` / `honorific` for `ja`). |
| `formality show (--customer ID \| --project ID \| --document ID)` | A scope's entries and, per target language, the register in force and the level it comes from. |
| `formality set REGISTER --target LOC (--customer ID \| --project ID \| --document ID)` | Set the register for one target language at one level. A customer default is normally keyed by language (`de`); a project or document entry must be one of the project's target locales. An unknown register for that language is a usage error (exit 2). With `--remote`, a customer default needs an admin or manager key; a project or document entry admin, manager or linguist. |
| `formality clear --target LOC (--customer ID \| --project ID \| --document ID)` | Remove the entry so the next level applies. |

Example - German is formal for the customer, but one project and one document are informal:

```bash
python -m app.cli formality set sie --target de --customer 1
python -m app.cli formality set du --target de-DE --project 4
python -m app.cli formality set du --target de-DE --document 17
python -m app.cli formality show --document 17
```

Example - provision a whole project from scratch and translate a document:

```bash
python -m app.cli customer create "Acme Corp"
python -m app.cli project create "Handbook" --source en-US --targets "fr-FR,de-DE" --customer-id 1
python -m app.cli doc upload 1 ./handbook.docx
python -m app.cli -v doc translate 1 --locale fr-FR   # synchronous; -v = per-segment lines
python -m app.cli doc export 1 --locale fr-FR -o handbook.fr-FR.docx
```

Mozilla localization files upload the same way: `doc upload 1 ./browser.dtd` or
`doc upload 1 ./main.ftl` (UTF-8). Fluent select-expression variants are separate segments;
Fluent terms stay untranslated unless the server sets `FTL_TRANSLATE_TERMS=1`.
Email messages upload the same way (`doc upload 1 ./welcome.eml`): the Subject and the
text/plain and text/html body parts are translated, and `doc export` writes the message
back with attachments, inline images and every other header unchanged. Signed or
encrypted mail (S/MIME, PGP) is refused; Outlook .msg files must be saved as .eml first.
Freedesktop launchers and Haiku catalogs work the same way (`doc upload 1 ./org.example.App.desktop`,
`doc upload 1 ./en.catkeys`): `doc export --locale fr-FR` writes the `.desktop` file with
`Name[fr_FR]=` lines added after each source key, and the `.catkeys` file with the translation
column filled and the header language set (rename it `fr.catkeys`).
HTML Help files upload the same way (`doc upload 1 ./toc.hhc`, also `.hhk` and `.hhp`):
`doc export` writes the file in the target language's Windows ANSI code page (for example
Windows-1251 for `--locale ru-RU`) and refuses with a message naming the character when a
translation does not fit it; the install can set `HTMLHELP_UTF8_FALLBACK=1` to write UTF-8
instead (with a warning).

XAML resource files upload the same way (`doc upload 1 ./Strings.xaml`): the `sys:String` /
`x:String` resources are translated and `doc export` writes the file back byte for byte
outside the changed values. View attributes (Content, Text, ToolTip, Header, Title) are
translated only when the install sets `XAML_VIEW_ATTRIBUTES=1` (environment, restart).

XSLT stylesheets (`.xsl`, `.xslt`) upload the same way (`doc upload 1 ./invoice.xsl`): the
literal output text and the alt/title/placeholder/aria-label attributes of literal result
elements are translated; `xsl:*` instructions, XPath and `{...}` value templates are kept
exactly, and `doc export` writes every other byte back unchanged.

The same content commands work **against a running server** with `--remote`
(org-scoped by the `lsu_` token, with no account type exempt). `doc translate` enqueues one
job per locale; poll it with `job status --remote`:

```bash
python -m app.cli --remote https://tms.example.com --token lsu_xxx project list
python -m app.cli --remote https://tms.example.com --token lsu_xxx doc upload 1 ./handbook.docx
python -m app.cli --remote https://tms.example.com --token lsu_xxx doc translate 5 --locale fr-FR
python -m app.cli --remote https://tms.example.com --token lsu_xxx job status 9
```

### Connectors - `connector …`

Manage every connector integration without the web UI. The logic is
`app.services.connectors.store` (already a clean service) + `app.services.scheduler`;
the CLI wraps them directly. In-process (local-root, no RBAC); only `list` and the
operate verbs `pull` / `push` / `sync` are **remote-capable** (via `/api`). CRUD,
`test`, `browse`, `rotate`, `schedule`, `token-regen`, `retry`, the per-platform
`*-translate` verbs and the OAuth adjuncts touch live credentials/secrets on the host
and stay in-process (exit 2 under `--remote`).

**Per-connector-kind visibility (ffadmin Wave 5).** An admin can disable individual
connector KINDS on an install via the `connector_disabled_kinds` setting (a CSV of kind
slugs; toggled from the `/connectors/manage` page or the catalog, or headlessly with
`config set connector_disabled_kinds "jira,shopify"`). A disabled kind is
**non-enumerable** (the kill-switch law, per kind): `connector create --kind <k>` refuses,
and **every** verb targeting an existing connection of a disabled kind (`show` / `update` /
`delete` / `test` / `browse` / `rotate` / `schedule` / `pull` / `push` / `sync` / `retry` /
the per-platform `*-translate` verbs / the OAuth adjuncts) refuses with
`connector kind '<k>' is disabled (settings connector_disabled_kinds).` - because they all
resolve the connection through the one guard. Under `--remote` the **server** answers (a
disabled kind's `/api/connectors/{cid}/*` routes 404), so a laptop's local flag state never
vetoes a healthy server. Existing connections keep their data and every verb returns when
the kind is re-enabled. The section itself stays always-on (only kinds toggle). There is no
dedicated CLI verb for the toggle - `config set connector_disabled_kinds …` is the twin.

**CRUD + credentials**

| Command | What it does |
|---------|--------------|
| `connector create --kind <airtable\|jira\|confluence\|zendesk\|document360\|freshdesk\|zoho\|intercom\|salesforce\|notion\|helpscout\|strapi\|contentful\|shopify\|hubspot\|wordpress\|akeneo\|mcp_cms\|github\|sanity\|sitecore\|drupal\|plytix\|msgraph\|servicenow\|gitlab\|bitbucket\|gdocs> --name N [--config K=V …] [--secret K=V …] [--customer ID] [--content-type ID]` | Create a connection. `--config` is repeatable non-secret settings (base_id, table, jql, …); `--secret` is repeatable credential fields (api_key, or email + api_token). Secrets are Fernet-encrypted at rest and **never echoed**; prefer `--secret api_key` (prompts via getpass) or `--secret api_key=-` (reads one line from stdin) to keep the value off the command line. `--customer` scopes the connection (blank = global); `--content-type` is the default content type copied onto the auto-created sync project. **Archived connectors** (none right now: every retired kind has been rebuilt) are **refused** here with a clear message; any existing connection of an archived kind keeps working and stays fully drivable via every other `connector` verb. | <!-- pregate: allow-secret flag syntax and field names, no credential value -->
| `connector list [--kind K]` | List connections (id, kind, name, status, auth mode). JSON rows also carry `languages_project_id`: the connected project whose language list the connection follows (null for a plain connection). |
| `connector show <id>` | Full detail: config + credential/auth **state** (token / oauth / none) - never the secret values. |
| `connector update <id> [--name N] [--config K=V …] [--secret K=V …] [--customer ID] [--content-type ID]` | Merge config (never a wholesale replace); update credentials only if `--secret` is given. **Changing the credential destination origin (scheme, host or port) clears the stored credential** unless the same call supplies a new `--secret`, so a config-only edit can never repoint a live token at another host; the connection goes to `error` until credentials are re-entered. The destination is read from a different config key per connector: `base_url` (strapi, akeneo, wordpress, servicenow, sitecore, jira, confluence), `api_domain` (zoho), `login_url` (salesforce), `domain` (freshdesk), `subdomain` (zendesk) and `store_name` (shopify). Every other connector has a fixed vendor host that no config value can move. Refuses a `--secret` on an OAuth connection (use `set-oauth-secret`). `--customer` re-scopes the connection and `--content-type` sets its default content type; pass `''` to clear either, omit to leave it unchanged. On the web form and `/api`, only an admin may change a ServiceNow connection's `knowledge_base` or `kb_category`, a Notion `parent_page_id`, a Jira `jql` or a Confluence `space_key` or `cql` while it holds a credential, or turn on `update_published` (the CLI runs as the local operator). On the connection a connected project runs on, `--config target_locales=...` changes the PROJECT's languages (new ones added, removed ones stopped, never deleted) and the connection then follows the project; over `--remote` adding a language needs a manager or above. On a connection that only feeds other projects (their own copies) a language change is refused, naming them. |
| `connector delete <id>` | Delete (**destructive**; confirm / `--yes`). |
| `connector prune-config <id> \| --all [--apply]` | Prune stale content-hash entries a dropped target locale left in a connection's config (append-only content-hash maps are never pruned at write time). **DRY-RUN by default** - lists exactly what would be removed, per map - pass `--apply` to execute (audited). Restricted to an allow-list of pure content-hash maps (`zendesk_hashes`, `jira_hashes`, …); never touches child-id / `*_written` / field-keyed maps. `--all` sweeps every connection (aggregated). Local-only. |
| `connector rotate <id> --secret K=V …` | Replace credentials wholesale - but only **after proving the NEW ones work** (a throwaway `test()`); a bad rotation never disturbs the working secret. **Refuses OAuth connections** (their tokens are minted by the provider). Secret is password-like, never echoed. |

**Operate**

| Command | What it does |
|---------|--------------|
| `connector test <id> [--offline]` | Test credentials against the external system (`store.test_connection`). `--offline` (CI/agent smoke flag): use a deterministic canned mock transport instead of the network - no credentials or live system needed. Explicit only; core kinds only (`airtable`, `jira`) - any other kind refuses cleanly. **`--offline` never persists**: it bypasses `store.test_connection`'s status write entirely, so the connection's `status`/`last_message`/`last_test` are left byte-untouched - a canned result can never make a genuinely-broken production connection read false-green. |
| `connector browse <id> [--base B --table T] [--offline]` | List containers (Airtable bases / Jira projects / Confluence spaces); with `--base --table`, list an Airtable table's field names (Airtable only; the base and table must be the connection's configured ones, anything else is refused). Same `--offline` smoke flag as `test`. |
| `connector schedule <id> --every-minutes N \| --off` | Set or clear the automated-sync schedule (`scheduler.set_schedule`). A nonzero interval is **floored at the service minimum**; enabling **never fires immediately** (first run is one interval out). Over `--remote`, turning on a schedule for a Jira connection needs an admin account (it starts recurring saved-JQL batches); `--off` does not. |
| `connector pull <id>` | Pull source content into a Localization OS document (`store.pull_connection`). Over `--remote` it runs as a background call (`Prefer: respond-async`) and the CLI polls the job to its end, so a long pull is never cut off by a client timeout (#728). |
| `connector push <id>` | Push translated segments back (`store.push_connection`). Over `--remote`, a background call polled to its end, like `pull`. |
| `connector sync <id>` | Automated round-trip pull → MT-translate → push, **synchronously in-process** (`store.translate_now`, dispatches to the right flow per kind). |
| `connector retry <id> --from-json FILE` | Re-run **only** the items that failed in a prior run (`store.retry_failed`). `FILE` is a `summary['failures']` list, or the whole summary object. Over `--remote`, a Jira list naming more than 10 issues needs an admin account. |
| `connector jira-translate <id> [--issue-keys …] [--jql …] [--locales …]` | Translate Jira issues into the connection's locales as labelled comments/fields. Over `--remote`, a saved-JQL run (no `--issue-keys`) or more than 10 issues needs an admin account; locally the host operator is trusted. |
| `connector jira-translate-pending <id>` | Translate the Jira connection's queued issue changes. Over `--remote` this needs an admin account. |
| `connector jira-count <id> [--issue-keys …] [--jql …]` | Issue-count **preview** (`store.jira_count` → `approximate-count`): how many issues a JQL, ANDed onto the connection's scope, would touch - translating none, spending nothing. The same scope guard as `jira-translate` applies (a caller `--jql` with parentheses is refused when a scope is configured). Prints `~N issue(s) match this JQL`. |
| `connector confluence-translate <id> [--page-ids …] [--space-key …] [--cql …] [--locales …]` | Translate Confluence pages into localized child pages. |
| `connector airtable-translate <id> [--base …] [--table …] [--source-field …] [--record-ids …]` | Convention-driven Airtable translation (auto-detects target-language columns). `--base`/`--table` must equal the connection's configured base and table (a connection with none configured refuses every base). Before writing, each target column is validated by field **type**: a non-text column (number/date/checkbox, or a select without `allow_select_typecast=1`) is **skipped and reported** with a field-specific reason rather than failing opaquely. With `allow_select_typecast=1` on the connection, select targets are written via typecast and any **new option minted** is reported loudly. Field IDs are resolved from the schema and cached in config, so a **renamed source column** keeps resolving. |
| `connector airtable-webhook register <id> [--base ID] [--table T] [--source-field F]` | Register an Airtable webhook; omitted overrides use the connection's configuration, and a `--base`/`--table` that differs from it is refused. |
| `connector airtable-webhook unregister <id>` | Remove the connection's Airtable webhook. |
| `connector airtable-webhook status <id>` | Show the stored webhook status. |
| `connector airtable-webhook translate-pending <id>` | Translate queued Airtable record changes. |
| `connector airtable-webhook health <id>` | Probe the official Airtable webhook's health via list-webhooks and surface a **watched-field-deleted / paused error state** (Airtable disables such a hook without 404ing it, so the ping path goes silent). Persists the state so the connection page + `airtable-webhook status` show it. Parity: `POST /api/connectors/{id}/airtable/webhook/health`. |
| `connector airtable-record create <id> [--base …] [--table …] --fields-json '[{"Name":"x"}]' [--typecast]` | Create new Airtable record(s) from a JSON array of field maps (a single object is also accepted). Manual/API-triggered only - **not** part of the auto-translate sync; runs under the per-run write ceiling. Parity: `POST /api/connectors/{id}/airtable/records`. |
| `connector airtable-record delete <id> [--base …] [--table …] --record-ids a,b,c` | Delete Airtable record(s) by id. Manual/API-triggered only; runs under the per-run write ceiling. Parity: `POST /api/connectors/{id}/airtable/records/delete`. |
| `connector document360-translate <id> [--article-ids …] [--locales …]` | Translate Document360 articles into the connection's langCodes as per-language article versions (T2; Markdown & WYSIWYG handled). |
| `connector zendesk-translate <id> [--article-ids IDS] [--locales LOCS]` | Translate Zendesk articles (default: the connection's label/section scope). Ids and locale overrides are comma/space separated. |
| `connector freshdesk-translate <id> [--article-ids IDS] [--locales LOCS]` | Translate Freshdesk articles (default: the connection's category/folder scope). Ids and locale overrides are comma/space separated. |
| `connector zoho-translate <id> [--article-ids IDS] [--locales LOCS]` | Translate Zoho Desk articles into the connection's locales as per-locale translations (T2). `--article-ids` are digits only and must sit inside the connection's category or department. |
| `connector intercom-translate <id> [--article-ids IDS] [--locales LOCS]` | Translate Intercom articles (default: the connection's article list). Ids and locale overrides are comma/space separated. |
| `connector salesforce-translate <id> [--article-ids IDS] [--locales LOCS]` | Translate Salesforce Knowledge articles (default: the connection's record-type scope). `--article-ids` takes KnowledgeArticleIds; ids and locale overrides are comma/space separated. |
| `connector salesforce-rediscover-fields <id>` | Re-discover the Salesforce connection's Knowledge fields. |
| `connector notion-translate <id> [--article-ids IDS] [--locales LOCS]` | Translate Notion pages (default: the root page's child pages, or the root database's rows). Writes are live (Notion has no draft state); a translation page edited in Notion is reported and not overwritten. `--article-ids` takes source page ids (32 hex characters or dashed UUIDs) that sit under the connection's root; ids and locale overrides are comma/space separated. |
| `connector notion-resolve-link <id> <source_id> <target_locale> <adopt\|ignore>` | Resolve a flagged Notion page link: `adopt` accepts the existing target for updates on the next run; `ignore` drops the row (it re-flags next run). |
| `connector helpscout-translate <id> [--article-ids IDS] [--locales LOCS]` | Translate Help Scout Docs articles into the mapped target collections (default: every mapped source collection). `--article-ids` must sit inside a mapped source collection; ids and locale overrides are comma/space separated. |
| `connector helpscout-resolve-link <id> <source_id> <target_locale> <adopt\|ignore>` | Resolve a flagged Help Scout article link: `adopt` accepts the existing target for updates on the next run; `ignore` drops the row (it re-flags next run). |
| `connector strapi-translate <id> [--article-ids …] [--locales …]` | Translate Strapi collection-type entries into the connection's locales as per-locale entry variants (T2; v5 documentId / v4 localization). `--article-ids` are `<contentType>:<id>` (or a bare id for a single-content-type connection). |
| `connector contentful-translate <id> [--article-ids …] [--locales …]` | Translate Contentful entries into the connection's locales as new per-locale field keys on each entry (T1; fields.{id}.{locale}, version-locked read-merge-write, never auto-published). `--article-ids` are entry ids (each entry's content type must be configured on the connection). |
| `connector shopify-translate <id> [--article-ids …] [--locales …]` | Translate Shopify resources (products/collections/articles/blogs/pages) into the connection's locales as registered per-locale translations (T2-flavored; Translations API `translationsRegister`, keyed by resource+key+locale with a freshly-read content digest, never overwriting the source). `--article-ids` are resource GIDs (`gid://shopify/Product/123`) whose type must be configured on the connection. |
| `connector hubspot-translate <id> [--article-ids …] [--locales …]` | Translate HubSpot site pages / landing pages / blog posts into the connection's languages as real HubSpot **language variations** (T3 with native linkage; HubSpot's own `translations` dict + `translatedFromId` are the correlation, so no id map is persisted). Written to each variation's **draft** by default and **never published**. `--article-ids` are `<content-type>:<id>` (or a bare id for a single-content-type connection) whose type must be configured on the connection. **Marketing emails and marketing forms are not supported in this version.** The connection's credential is a HubSpot Service Key (Settings -> Integrations -> Service Keys, `content` scope), sent as a Bearer token; an older Private App token works the same way. |
| `connector wordpress-translate <id> [--article-ids …] [--locales …]` | Translate WordPress posts/pages into the connection's locales as separate per-locale posts in the target language (T3, doc-per-locale via Polylang; raw block content read+written via context=edit so Gutenberg blocks and shortcodes survive; explicit draft/publish status on both posts and pages). `--article-ids` are source refs `<type>:<id>` (e.g. `posts:12`; a bare id for a single-type connection). With Polylang Pro the translation is linked to the source's translation group; without Pro the connector tracks it in its own id map alone. |
| `connector wordpress-resolve-link <id> <source_ref> <target_locale> <adopt\|ignore>` | Resolve a WordPress needs_link id-map row (SPEC-t3 rule 6): a pre-existing Polylang-linked target post the id map never created. `adopt` accepts it (the next run updates it in place); `ignore` drops the row (it re-flags next run). Never overwrites content - only edits the id map. |
| `connector akeneo-translate <id> [--article-ids ...] [--locales ...]` | Translate Akeneo product and product-model attribute values into the connection's locales as per-locale values on the same record (T1; partial-PATCH merge per attribute+locale+scope). `--article-ids` are `<contentType>:<uuidOrCode>` where contentType is `product` or `product_model`. |
| `connector mcp_cms-translate <id> [--article-ids ...] [--locales ...]` | Translate CMS items through the CMS's own MCP server into the connection's locales as per-locale item variants (T2, prototype). `--article-ids` are the item ids the mapped list tool returns. The connection's `--config mcp_protocol=auto\|legacy\|2026-07-28` (default auto: `server/discover`, else the `initialize` handshake) and `--config call_timeout_s=5..300` (default 30) apply to every MCP call. |
| `connector gdocs-translate <id> [--article-ids ...] [--locales ...]` | Translate Google Docs from the connection's source Drive folder into per-locale Google Docs under the target folder (T3 document per locale; the source is never written). `--article-ids` are Drive file ids of Docs inside the source folder. |
| `connector servicenow-translate <id> [--article-ids ...] [--locales ...]` | Translate ServiceNow Knowledge articles into the connection's languages as one Draft kb_knowledge record per language, linked to the source by the Parent field (T3). `--article-ids` are 32-character article sys_ids inside the connection's knowledge base. |
| `connector servicenow-resolve-link <id> <source_id> <target_locale> <adopt\|ignore>` | Resolve a ServiceNow needs_link id-map row (SPEC-t3 rule 6): a translation that already exists under the source's Parent but that the id map never created. `adopt` accepts it (the next run updates it while it is Draft); `ignore` drops the row (it re-flags next run). Never overwrites content, only edits the id map. |
| `connector msgraph-translate <id> [--article-ids ...] [--locales ...]` | Translate SharePoint library or OneDrive files from the connection's source folder into per-locale copies under the target root (T3 document per locale; the source is never written). `--article-ids` are drive item ids of files inside the source folder. |
| `connector plytix-translate <id> [--article-ids ...] [--locales ...]` | Translate Plytix product text attributes into the per-language target attributes mapped on the connection (T1; partial PATCH of the target attributes only). `--article-ids` are Plytix product ids. |
| `connector drupal-translate <id> [--article-ids ...] [--locales ...]` | EXPERIMENTAL (needs the alpha JSON:API Multilingual module, 1.0.0-alpha5). Translate Drupal entities (nodes, terms, blocks) into the connection's languages as the entity's own translations (T2; created unpublished; a write that lands in the wrong language halts the run, restore from the Revisions tab). `--article-ids` are `<jsonapiType>:<uuid>`, for example `node--article:<uuid>`. |
| `connector github-translate <id> [--article-ids ...] [--locales ...]` | Localize GitHub resource files into the connection's locales: the source files are imported into the connection's key project, translated, and the target-language files are committed together on a working branch with one pull request against the base branch (T3). `--article-ids` are source file paths matching the configured patterns. |
| `connector sanity-translate <id> [--article-ids ...] [--locales ...]` | Translate Sanity documents into the connection's languages, either as language items inside internationalized-array fields (field-level) or as linked per-language documents (document-level); writes drafts only. `--article-ids` are Sanity document ids of the configured types. |
| `connector sitecore-translate <id> [--article-ids ...] [--locales ...]` | Translate Sitecore XM Cloud content items into the connection's languages as language versions of the same item (T2; addItemVersion then updateItem, never published). `--article-ids` are item GUIDs that must sit under the connection's root path. |
| `connector bitbucket-translate <id> [--article-ids ...] [--locales ...]` | Localize Bitbucket resource files into the connection's locales: the source files are imported into the connection's key project, translated, and the target-language files are committed together on a working branch with one pull request against the base branch (T3). `--article-ids` are source file paths matching the configured patterns. |
| `connector gitlab-translate <id> [--article-ids ...] [--locales ...]` | Localize GitLab resource files into the connection's locales: the source files are imported into the connection's key project, translated, and the target-language files are committed together on a working branch with one merge request against the base branch (T3). `--article-ids` are source file paths matching the configured patterns. |
<!-- factory:anchor docs.cli_md  (scripts/factory inserts new connector rows above this line) -->

> All operate commands run **synchronously in-process** (like `doc translate`): a
> one-shot CLI has no daemon job worker, so the work is really done when the
> command returns. Every command requires an **explicit connection id** and, in
> normal use, makes **no network call until you drive a live connection**.

**Legacy shared token + OAuth adjuncts**

Shared connector tokens no longer authorize connector operations, text translation, connection
lists or job results. Use a per-user key from Account for companion apps and remote API clients.
MCP HTTP requests must carry their own key; environment authentication is stdio-only.
Suspended organizations cannot run scheduled or queued connector jobs or accept inbound webhooks.

| Command | What it does |
|---------|--------------|
| `connector token-regen` | Regenerate the legacy shared connector token (`lsk_…`). The `/api` routes now refuse that token (companion apps use per-user keys), so this only rotates the stored value. Printed **once**; the old token stops working. Confirm / `--yes`. |
| `connector oauth-status [<id>] [--kind K]` | Report whether a kind's OAuth **provider app** is configured (client_id set) and, for a connection id, its `auth_mode` (token/oauth) + status. |
| `connector set-oauth-secret <id> --from-json FILE` | **Advanced / CI:** inject provider-minted OAuth tokens (`store.set_oauth_secret`). `FILE` is the oauth sub-dict (`access_token`, `refresh_token`, `expires_at`, …) or a `{"oauth": {…}}` wrapper. |

> **OAuth authorize stays interactive - a CLI cannot do the browser consent leg.**
> Airtable and Atlassian (Jira/Confluence) provide **no device flow** for these
> apps, so the authorization-code round-trip (redirect the operator's browser to
> the provider, collect consent, exchange the code) is inherently **web-UI only**.
> The CLI therefore covers *everything except* that one leg: token-auth connectors
> are fully CLI-managed (create/test/rotate/sync); OAuth **setup** happens in the
> Settings UI + the connection's "Connect" button; and for headless/CI pipelines
> that already hold provider-minted tokens, `set-oauth-secret --from-json` injects
> them and `oauth-status` reports readiness. Once a connection is in OAuth mode,
> the CLI drives it exactly like a token one (the store's proactive refresh mints
> fresh access tokens automatically).

Example - provision an Airtable connector, test it, and run one sync:

```bash
python -m app.cli connector create --kind airtable --name "Marketing" \
  --config base_id=appX --config table=Content --config source_field=Notes \
  --secret api_key=pat_XXXXXXXX
python -m app.cli connector test 1
python -m app.cli connector sync 1        # pull -> MT -> push, synchronous
python -m app.cli connector schedule 1 --every-minutes 60   # then hands off to the scheduler
```

### Users / Access - `user …`, `apikey …`, `member …`

Manage accounts, per-user API keys, and project membership without the web UI.
The logic lives in `app.services.security` / `apikeys` / `access` / `gdpr` - the
same functions the admin Users page calls (the role/department/active state
transitions were extracted into `security.set_role` / `set_department` /
`set_active` so the page and the CLI share one code path). In-process operator
commands (local-root, no RBAC); **not** remote-capable - `--remote` refuses (exit 2).
The one exception is `apikey list`, which has an `/api` route (`GET /api/apikeys`,
admin-only) and works over `--remote`.

Every `<user>` argument accepts an **email or a numeric id**. Destructive ops
(`disable`, `erase`, `apikey revoke-all`, `member remove`) confirm unless `--yes`
(and in `--json` / `--quiet` / non-interactive mode a destructive op **refuses**
without `--yes` rather than blocking on stdin).

**Users - `user …`**

| Command | What it does |
|---------|--------------|
| `user create --email E --role R [--name N --department D --customer ID] [--password P \| --password-stdin]` | Create a user. `--customer ID` binds the account to one customer (a "customer account"; a bound user may only be translator/linguist/viewer). Blank/omitted = internal staff. `--role` must be one of admin/manager/reviewer/linguist/translator/viewer. Omit `--password` on a terminal to be **prompted securely** (getpass, confirmed, never echoed); a blank answer means no local login (SSO/LDAP only). With no terminal and no `--password` the account is created without a local login and a warning says so. For scripts, `--password-stdin` reads the password from the first line of stdin (never echoed, never logged, kept out of argv and shell history); an empty line, or both `--password` and `--password-stdin`, is a usage error (exit 2). Reserved identities + duplicate emails are rejected. |
| `user list` | List users (id, email, role, dept, active, active-key count). |
| `user show <user>` | One user's detail + their API keys (never the key values). |
| `user customer <user> <--customer ID \| --clear-customer>` | Bind a translator/linguist/viewer account to one customer, or clear the binding to return to internal staff. In-process only. |
| `user role <user> <role>` | Set a user's role (unknown role → exit 2). Refuses to demote the last active administrator of the account's own organization. |
| `user department <user> <dept>` | Set a user's department (stored lower-cased - the one canonical form; `''` clears). |
| `user org <user> <org_id>` | **(multi-org)** Move a user to another organization - changes `User.org_id` **only**. Cross-org operator action: refuses `--remote`; with the gate ON it requires `--operator` (see [Organizations](#organizations--org-)). **Footgun:** the user's already-created projects/connections **stay in the old org** (they are not re-parented). Refuses to move the last active administrator out of their organization (counted install-wide with the gate off); make another account an administrator there first. Refuses to move a customer-bound account whose customer belongs to another organization; unbind it from that customer first. |
| `user enable <user>` | Re-activate a user. Old sessions and API keys stay dead (disable ended them): the person signs in again and creates new keys. |
| `user disable <user>` | Deactivate a user (**destructive**; confirm / `--yes`). Signs the account out on every device and revokes every API key it holds, for good (#422); the output says how many keys were revoked. If the account owns a SCIM token, the prompt and the output warn that identity provider sync stops (the token is revoked too, audited `scim.token_revoked`). The Users page, `/api/users/{uid}/disable` and SCIM deactivation do the same. **Refuses to disable the last active administrator** of the account's own organization (the rule lives in the user service, so the Users page and `/api` refuse it too). |
| `user reset-password <user> [--password P]` | **Headless admin reset** - sets a new password **without** knowing the current one (`security.admin_set_password`). Enforces the 12-char policy. Omit `--password` to be prompted securely. The password is **never logged, never echoed, never in `--json`**. An SSO / SAML / SCIM / LDAP account becomes a local-password account on purpose (a local password never signs in an identity-provider account), audited `user.auth_source_changed`. A trust (host service identity) account is refused. |
| `user mfa-status <user>` | Two-step sign-in (TOTP) state: on/off, when it was turned on and last used, recovery codes left, and whether `mfa_required` makes it mandatory for this account (`not applicable` for SSO/SAML accounts, and for a SCIM-linked account with no password here, #423). Never a secret or a code. API: GET /api/users/{uid}/mfa. |
| `user mfa-reset <user> [--yes]` | Reset a person's two-step sign-in (a lost phone): deletes their authenticator setup and recovery codes and **signs them out everywhere** (bumps the session epoch). Confirm / `--yes`. Audited as `mfa.reset`. This is also the **break-glass for a locked-out sole admin**: run it on the host (for example `docker exec <container> python -m app.cli user mfa-reset admin@example.edu --yes`). API: DELETE /api/users/{uid}/mfa; web: Users page, Reset. |
| `user unlock <email>` | Create or reset the account password-lock row (host break-glass; audited), even when unknown-name creation quotas are exhausted. Success requires a committed clear. Address and MFA limits remain separate. |
| `user invite --email E --role R` | Create an invite; prints the accept token / path (the web flow emails it). |
| `user invites [--expired]` | List pending invitations, or expired invitations with `--expired` (IDs, emails, roles and dates; never tokens). Local operator only, like `user invite`; API: GET /api/users/invites?expired=1 for expired. Under multi-org, invites carry their org; NULL belongs only to NULL/operator scope. |
| `user invite-revoke --id N` | Revoke a live pending invitation, or delete an expired one (a soft delete: the row is kept for audit, the audit detail says `expired`); old links stop working. No prompt. Local operator only; API: DELETE /api/users/invites/{invite_id}. |
| `user export <user> [-o FILE] [--force]` | GDPR subject-access export - NDJSON to stdout (or `-o FILE`, which refuses to overwrite an existing file unless `--force`). Secrets (password_hash, key hashes, connector secrets) never leave. `--out PATH` is the long form of `-o PATH`. |
| `user erase <user>` | GDPR erasure: **anonymize** the user (tombstone email, scrub the row, revoke keys, rewrite denormalized pointers). **Destructive, irreversible**; confirm / `--yes`. Refuses erasing the last active admin. The audit log is retained (append-only). |

The `user ...` commands above run in-process on the host (they refuse `--remote`), so they are unchanged by #270. Their `/api` counterparts (the routes API clients and the MCP `manage_users` tool call) treat account-security changes as needing the **admin** key scope: `POST /api/users` (create), `/api/users/{uid}/role`, `/api/users/{uid}/enable` (an account-security action; since #422 disabling ended the account's sessions and revoked its keys, so enabling revives none), `/api/users/{uid}/reset-password`, `DELETE /api/users/{uid}/mfa` and `POST /api/users/invite` refuse a docs-scoped (MCP) key with a `403` ("This endpoint requires an admin-scoped user API key."). Disable, unlock, department and customer keep the docs scope, and web sessions for admins are unchanged.

**API keys - `apikey …`**

| Command | What it does |
|---------|--------------|
| `apikey issue --user U [--name N --scopes docs --expires-in-days N]` | Mint a per-user key. The `lsu_…` key is **printed ONCE** with a store-now banner and is unrecoverable afterwards (only its hash is stored). The four scopes: `api` (always implied, the base inbound grant); `docs` (the default - the document/MCP endpoints + the remote-CLI surface); `keys` (the key-project + Delta endpoints); and `admin` (the sharpest identity/admin verbs, **mintable only locally** - an `/api` mint refuses it). `--expires-in-days N` sets an optional expiry N days from now (omitted = no expiry); the output shows it, and above the install's maximum (setting `apikey_max_age_days`, unset by default) the mint is refused with a clear error. |
| `apikey list [--user U]` | Enumerate issued keys system-wide (or one owner's) as **metadata only**: id, owner, name, prefix, scopes, active, `last_used`, `expires` (the expiry date, `never` for a no-expiry key, and the `(expired)` state once it has passed). **Never** the key value or hash. In-process this is the trusted operator (org-confined only under `--org`; unscoped with `--operator`/gate off). Remote-capable (`--remote`, `GET /api/apikeys`): the `/api` arm is **admin-only** - an org admin sees only their own org's keys, a non-admin is refused (their own keys are on `/account`). |
| `apikey revoke <key-id>` | Revoke one key by its id (from `apikey list`). A revoked key stops authenticating immediately. |
| `apikey revoke-all <user>` | Admin kill-switch: revoke **every** key a user holds (**destructive**; confirm / `--yes`). |

**Project membership - `member …`**

| Command | What it does |
|---------|--------------|
| `member add <user> <project_id>` | Grant a user visibility into a project (idempotent). |
| `member remove <user> <project_id>` | Revoke a membership (**destructive**; confirm / `--yes`). |
| `member list <project_id>` | List a project's members (user id, email, role, added-by). |

Example - provision an account, issue it a key, and scope it to a project:

```bash
python -m app.cli user create --email jo@acme.com --role translator --password 's3cret-passphrase'
python -m app.cli apikey issue --user jo@acme.com --scopes docs    # lsu_… shown ONCE
python -m app.cli member add jo@acme.com 7
python -m app.cli user reset-password jo@acme.com                  # prompts securely
```

### Account - `account …`

Self-service account view and, since SPEC-onboarding-tour Phase 3, headless **state
parity for the first-run onboarding flag** (the "Get started" checklist). Web + `/api` +
CLI parity: the web `/account` page (its Get started card posts `POST /account/get-started`),
`GET /api/account`, `POST /api/account/theme` and `POST /api/account/onboarding-reset`, and
these commands. Appearance (Light, Dark, Match my computer) is stored per account (ADR 0063):
the web account menu and Account settings, `POST /api/account/theme` and `account set-theme`
all write the same value.

The onboarding **checklist panel itself is web-only UX** - there is no CLI tour.
What's surfaced headlessly is the `onboarding_dismissed_at` **flag**: `account
show` reports it (plus eligibility/variant, from `app.services.onboarding`, the
same authority the dashboard route reads) and `account reset-onboarding` clears
it - a support/scripting escape hatch equivalent to the web's `?tour=1`
re-summon, except a reset actually **clears** the flag (re-summon only overrides
display for one view; see `app/services/onboarding.py:dashboard_panel`).

**Remote is self-service:** over `--remote URL --token lsu_…` the per-user token
**is** the account, so the command acts on the **caller's own** account (mirroring
the web page) - a non-empty `--user` is **refused** (exit 2; it could otherwise
silently act on the caller). **In-process** there is no session, so `--user`
(email or numeric id) names the target account and is **required**.

| Command | What it does |
|---------|--------------|
| `account show [--user E]` | Show an account's profile + onboarding flag (email, name, role, ui locale, `ui_theme`, `onboarding: {dismissed_at, eligible, variant}`). Over `--remote` it also prints `key scopes`, the calling key's own grants (`scopes`, #751; never another key's; the web `/account` page lists each of your keys with its scopes). `GET /api/account` parity. |
| `account set-theme <light\|dark\|system> [--user E]` | Set the account's appearance (`system` = Match my computer). `POST /api/account/theme` parity; any other value is refused on every surface. The next page load in any browser uses it. |
| `account reset-onboarding [--user E]` | Clear `onboarding_dismissed_at` (NULL → tour-eligible again). `POST /api/account/onboarding-reset` parity. Self-service remotely (own account only); no-op for an ineligible role (still clears the flag, just nothing auto-shows). |

```bash
# remote, self-service (the token owner's own account):
python -m app.cli --remote https://tms.example.com --token lsu_… account show --json
python -m app.cli --remote https://tms.example.com --token lsu_… account reset-onboarding

# in-process (trusted root), naming the target account:
python -m app.cli account reset-onboarding --user jo@acme.com
```

### Page layouts: `layout ...`

Your own saved page layouts (trains L1 and L2): the order of the cards on a page, and
which cards sit side by side in rows of 1, 2 or 3. One layout per page TYPE per person,
so a saved `project` layout applies to every project page for that account. The page
keys (`layout list` prints them all, from the one registry): `project`, `dashboard`,
`connectors`, `connectors_catalog`, `connector_type`, `connection`, `setup_ai`, `users`,
`settings`, `security`, `account`, `routing`, `document`, `reports`, `my_work`,
`customer`, `glossary`, `style_guide`, `vendor`, `workflow_group`, `building_blocks`
and `queries`. Web + `/api` + CLI parity over `app/services/page_layouts.py`,
which validates for every surface: an unknown page or card, a row of more than 3 cards,
a repeated card or an oversized layout is refused and nothing is stored. Cards a layout
does not mention are added at the end in their own rows; no saved layout = the default
order.

**Remote is self-service:** over `--remote URL --token lsu_...` the token **is** the
account (`/api/account/layouts`); a non-empty `--user` is refused (exit 2).
**In-process** `--user` (email or numeric id) names the account and is required while
sign-in is on. With sign-in **off** there is one shared layout per page for the install
(the same one the web page shows), so `--user` is refused there.

| Command | What it does |
|---------|--------------|
| `layout list [--user E]` | Every layout page with its rows and whether it is saved or the default. `GET /api/account/layouts` parity. |
| `layout show <page> [--user E]` | One page's rows with the card names (saved or default). `GET /api/account/layouts/{page_key}` parity. An unknown page: exit 2 (local) / server 404 (remote). |
| `layout set <page> --rows JSON [--user E]` | Save a layout, e.g. `--rows '[["overview"],["tm","glossary"]]'`. `PUT /account/layouts/{page_key}` (web) + `PUT /api/account/layouts/{page_key}` (remote) parity. An invalid layout: exit 2 (local) / server 400 (remote). `--rows`, not `--json`, because `--json` is the global output flag. |
| `layout reset <page> [--user E]` | Delete the saved layout (back to the default). `DELETE /account/layouts/{page_key}` + `DELETE /api/account/layouts/{page_key}` parity. |

```bash
python -m app.cli --remote https://tms.example.com --token lsu_... layout show project
python -m app.cli layout set project --rows '[["tm","glossary","langs"],["overview"]]' --user jo@acme.com
python -m app.cli layout reset project --user jo@acme.com
```

### Global search: `search ...`

Searches **project and customer names, document file names, translation memory text,
glossary terms, workflow names and connection names**. It does not search inside documents. Web (`/search`, plus live suggestions in
the Ctrl+K dialog), `GET /api/search`, the MCP `search_workspace` tool and this verb share
`app/services/global_search.py`, so the groups, the access rules and the bounds are the same
everywhere: 2 to 100 characters, at most 25 hits per group per call, counts stop at 1000
(shown as `1000+`), the TM scan covers the newest 100,000 entries you can open, and paging stops
at offset 1000.

**Remote** (`--remote URL --token lsu_...`) is scoped server-side by the token to what that
person can open (project membership, customer binding, org). **In-process** has no per-user
rules, like `tm list`; with multi-org on it needs `--org` or `--operator`.

| Command | What it does |
|---------|--------------|
| `search <text> [--group projects\|documents\|tm\|glossary\|workflows\|connections ...] [--limit N] [--offset N]` | Grouped hits with a count per group. `--group` repeats; `--limit` is hits per group (1 to 25, default 5); `--offset` pages each group (0 to 1000). Text under 2 or over 100 characters: exit 2 (local) / server 400 (remote). `GET /api/search` parity. |

```bash
python -m app.cli search atlas
python -m app.cli --json search "welcome" --group tm --limit 25
python -m app.cli --remote https://tms.example.com --token-file key.txt search atlas
```

### Notifications: `notify ...`

Project notification channels (Slack, Microsoft Teams, a signed webhook, email) and which scopes post
to them. Web parity: **Settings > Notifications** (`/settings/notifications`) and the Notifications
panel on a project page; `/api/notify/*`; the MCP `project_notifications` tool (list and attach
existing channels only). Messages carry the project name, counts and a sign-in link, never source or
target text. A channel's URL is a bearer credential: it is stored encrypted and never printed back,
so prefer `--url-file` or `--url-env` over `--url` (argv shows in process listings). A webhook
channel's signing secret is printed **once**, when the channel is created or the secret rotated.
Managers and admins may create channels and routes. Kill switch: `config set notify_outbound_enabled 0`.

**Remote** (`--remote URL --token lsu_...`) is scoped server-side by the token. **In-process** has no
per-user rules; with multi-org on it needs `--org` or `--operator`.

| Command | What it does |
|---------|--------------|
| `notify channel list` | The channels: id, name, kind, state, a masked destination, last test, failed deliveries. `GET /api/notify/channels`. |
| `notify channel add KIND NAME [--url-file PATH \| --url-env VAR \| --url URL] [--recipients A,B] [--customer ID]` | Create a channel. `KIND` is `slack`, `teams`, `webhook` or `email`. Slack needs a hooks.slack.com incoming-webhook URL, Teams a Workflows (Power Automate) URL, webhook any https URL (private hosts are refused by the egress guard), email needs `--recipients`. `--customer` pins the channel to one customer. `POST /api/notify/channels`. |
| `notify channel update CHANNEL_ID [--name N] [--url-file PATH \| --url-env VAR \| --url URL] [--recipients A,B] [--enable \| --disable] [--rotate-secret]` | Rename, re-point, turn on or off, or mint a new webhook signing secret (printed once). `POST /api/notify/channels/{cid}`. |
| `notify channel delete CHANNEL_ID` | Delete a channel with its routes and delivery log. `DELETE /api/notify/channels/{cid}`. |
| `notify channel test CHANNEL_ID` | Send a test message now; exit 1 when it was not delivered. `POST /api/notify/channels/{cid}/test`. |
| `notify channel deliveries CHANNEL_ID [--limit N]` | The delivery log, newest first (status, tries, error class; no content). `GET /api/notify/channels/{cid}/deliveries`. |
| `notify channel retry CHANNEL_ID` | Re-queue the channel's failed deliveries for one more round of retries. `POST /api/notify/channels/{cid}/retry`. |
| `notify route list [--scope S]` | The routes, optionally of one scope. `GET /api/notify/routes`. |
| `notify route set CHANNEL_ID --scope S [--ref ID] [--events E,E]` | Attach a channel to a scope (`project`, `content_type`, `project_type`, `customer` or `org`; `--ref` is the id, the project type name, or empty for `org`). Empty `--events` means the default events: `run.finished`, `run.failed`, `run.needs_review`, `run.limit_hit`, `automation.paused`, `automation.blocked`. Also available: `run.started`, `project.created`, `project.completed`. Idempotent. `POST /api/notify/routes`. |
| `notify route clear ROUTE_ID` | Remove a route. `DELETE /api/notify/routes/{rid}`. |
| `notify project show PROJECT_ID` | The channel each event reaches and the level it came from (project, content type, project type, customer, org). `GET /api/notify/projects/{pid}`. |
| `notify project mute PROJECT_ID` | Make the project post to no channel. `POST /api/notify/projects/{pid}/mute`. |
| `notify project unmute PROJECT_ID` | Let the project post to its channels again. |

A project posts to the most specific route that covers an event: its own, then its content type,
project type, customer and finally the org default, one per channel kind.

```bash
python -m app.cli notify channel add slack "Loc updates" --url-env SLACK_WEBHOOK_URL
python -m app.cli notify channel test 1
python -m app.cli notify route set 1 --scope customer --ref 4
python -m app.cli --remote https://tms.example.com --token-file key.txt notify project show 12
```

### MCP fourth surface - `mcp …`

Connection help for the **MCP server** (the fourth surface, packaged in
`integrations/mcp-server/`). In remote (streamable-HTTP) mode each request carries
its own per-user `lsu_` key in an `Authorization: Bearer` header (wave 4), so one
listener serves several users. `mcp connect` is a **pure formatter** - no `/api`
route, no DB, no credential handling - that prints the exact `claude mcp add` and
generic-client snippet for a base URL. It mirrors the web `/help` **"Connect an AI
agent"** section (`app/templates/help.html`), so a browserless operator gets the
same config a browser user would copy. The full auth story and scope guidance live
in `integrations/mcp-server/README.md`.

| Command | What it does |
|---------|--------------|
| `mcp connect --base-url URL [--path /mcp] [--key lsu_…] [--toolsets NAMES]` | Print the remote MCP connection snippet for `URL`: the endpoint (`URL/mcp`), the `claude mcp add --transport http` line, and the generic `Authorization: Bearer` / `X-Loc-Token` header. `--key` defaults to a copy-paste placeholder (no real credential required). `--toolsets` (#751) loads only the named toolsets on that connection, appended as `?toolsets=` (names: the tool families `orientation`, `projects`, `documents`, `keys`, `workflows`, `assets`, `memory`, `evaluations`, `ops`, plus `connectors` and `admin`; `all` or empty = every tool; an unknown name exits 2). The tool list always shows only the tools the key's scopes reach. Mirrors the `/help` "Connect an AI agent" section. |
| `mcp client-limit (--key-env VAR \| --fingerprint SHA256) --burst N --refill-per-minute N` | Print the `LOC_STUDIO_MCP_CLIENT_LIMITS` entry (#410) that gives one API key its own session-admission budget (1..10000 each). `--key-env` names an environment variable holding the key, which is hashed locally and never printed; unlisted keys keep the shared default (burst 10, one per minute). Pure formatter: the listener is a standalone process, so the map is listener environment, not an app setting. Restart the listener to apply. |

```bash
# print the connection snippet for a running listener's public base URL:
python -m app.cli mcp connect --base-url https://tms.example.com
python -m app.cli --json mcp connect --base-url https://tms.example.com
# per-client admission budget for one API key (key read from an env var, never printed):
python -m app.cli mcp client-limit --key-env CLIENT_KEY --burst 20 --refill-per-minute 2
```

### MCP OAuth authorization server - `mcp-oauth ...`

Host-only operator verbs for the **opt-in** MCP OAuth authorization server (#753, off by
default; see `docs/architecture/mcp-oauth.md`). When it is on, remote MCP clients (claude.ai
or ChatGPT connectors) connect with a browser sign-in and a consent page instead of a pasted
`lsu_` key; per-user keys keep working either way. The switches are ordinary settings, all
confirm-to-set and host-only over `/api`: `config set mcp_oauth_enabled 1`,
`mcp_oauth_resource https://mcp.example.com/mcp`, `mcp_oauth_dcr_enabled 0|1`,
`mcp_oauth_cimd_hosts "claude.ai, *.example.com"`. Web parity: **Security > MCP OAuth**
(`/security/mcp-oauth`) and **Account > Connected apps** (`/account/connected-apps`). Every
verb is in-process only (`--remote` refuses, exit 2); a person lists or revokes their OWN
grants remotely through `GET /api/mcp-oauth/grants` and
`POST /api/mcp-oauth/grants/{id}/revoke`.

| Command | What it does |
|---------|--------------|
| `mcp-oauth status` | Whether the server is off, on, or switched on but not ready, and every reason it is not ready (sign-in off, `app_base_url` missing or not https or with a path, `mcp_oauth_resource` missing, no resource-server secret). |
| `mcp-oauth rs-secret [--yes]` | Mint a new resource-server secret and print it ONCE (only its SHA-256 is stored in `mcp_oauth_rs_secret_hash`). Give it to the MCP server as `LOC_STUDIO_MCP_RS_SECRET`. `--yes` replaces an existing secret (the MCP server stops exchanging tokens until it has the new one). |
| `mcp-oauth grants [--user EMAIL\|ID] [--all]` | List consented grants: id, user, app name, the host answers go to, scopes, last use. Never a token or digest. `--all` includes revoked grants. |
| `mcp-oauth revoke GRANT_ID` | Revoke one grant: its access, API and refresh tokens stop working at once. Disabling an account revokes all of its grants too. |

```bash
python -m app.cli mcp-oauth status
python -m app.cli config set mcp_oauth_resource https://mcp.example.com/mcp
python -m app.cli config set mcp_oauth_enabled 1
python -m app.cli mcp-oauth rs-secret
python -m app.cli --json mcp-oauth grants --user ana@example.com
python -m app.cli mcp-oauth revoke 7
```

### Organizations - `org …`

Manage organizations (the SaaS multi-org tenant boundary). **Cross-org operator
commands**: org management is inherently cross-org, so these are **in-process only**
(`--remote` refuses, exit 2). There is **no web parity**: the back-office console
and the super-admin were removed on 2026-09-20, so the CLI is the only door.
(Historical: the console mirrored these verbs - org management is cross-org
operator work, not a customer-app page).

The **operator gate** (same matrix as the rest of the CLI, but stricter - you
cannot scope org management to a single org):

* **Gate OFF** (single-tenant, `multi_org_enabled=0`): the `org` group runs
  **freely** with no `--operator` - byte-identical to today. Creating a 2nd org
  on-prem is allowed but **inert** (nothing enforces org isolation until the gate
  is on).
* **Gate ON** (SaaS): the `org` group **requires `--operator`** (env
  `LOC_TMS_OPERATOR_KEY` verified against the stored `operator_key_hash`). `--org`
  is **not** accepted here - you can't scope org management to one org. Without
  `--operator` the command is **refused** (exit 2) so a customer/cron on a shared
  host can never manage tenants.

| Command | What it does |
|---------|--------------|
| `org create --name N` | Create an organization (name must be unique). |
| `org list` | List organizations (id, name, status). |
| `org rename <org_id> --name N` | Rename an organization. |
| `org show <org_id>` | Show one org: name, status, user + project counts. |
| `org suspend <org_id>` | **Suspend** an org (**destructive**; confirm / `--yes`). While the gate is ON, a suspended org's users **cannot log in** and its API tokens are **rejected**. |
| `org unsuspend <org_id>` | Re-activate a suspended org. |
| `org delete <org_id> [--yes]` | Delete an **EMPTY** organization (**destructive**; confirm / `--yes`). Fails closed (exit 2, itemized) on any **non-empty** org (users/customers/projects/connections + every other org-owned entity), on the **default/root** org, and on the caller's **own** org - there is **no** cascading force-delete; move or delete the contents first. Operator-gated + in-process only (refuses `--remote`); audited. No web parity: this is the only door since the operator console was removed. |

Suspension is enforced by `access.org_active()` - a **strict no-op with the gate
off** (single-tenant installs never read `Organization.active`, so it is
byte-identical to today).

Example - provision a tenant on a SaaS host (gate on):

```bash
# operator_key_hash must be set on the host (config set operator_key_hash …)
export LOC_TMS_OPERATOR_KEY='the-operator-secret'
python -m app.cli --operator org create --name 'Acme Inc.'
python -m app.cli --operator org list
python -m app.cli --operator user org jo@acme.com 3   # move Jo into org #3
python -m app.cli --operator org suspend 3 --yes      # lock the tenant out
```

### Jobs / Workflows - `job …`, `workflow …`

Run the background-job handlers and the multi-agent workflow orchestrator from
the CLI. The workflow CRUD logic lives in `app.services.workflows` (extracted
from the route handlers so the builder page and the CLI share one code path);
runs go through `app.services.orchestrator`. `job` run/status/list and `workflow`
run/resume/list/show are **remote-capable** (via `/api`; `job run` and `workflow
run/resume` execute synchronously server-side). Workflow create/add-step/edit-step/delete-step/move-step/clone also support `--remote`.
`workflow author-prompt` stays in-process.

**Synchronous by design (the key correctness rule).** A one-shot `python -m
app.cli` process has **no daemon worker threads** - they die with the process. So
the CLI never `jobs.submit()`-and-exits (that would leave the Job forever
`QUEUED`). Instead:

- `job run` calls `jobs.run_sync(kind, payload)`,
  which executes the handler **inline in the calling thread** and returns the
  finished Job (`COMPLETED` / `FAILED`, never `QUEUED`).
- `workflow run` / `workflow resume` call `orchestrator.run_workflow` /
  `resume_workflow` (the **synchronous** paths), **not** `start_run` /
  `resume_run_async` (which spawn a background thread that would die with the
  process). When the command returns, the run's DB state is real.
- `workflow cancel-run <run_id>` aborts an in-flight run (pending/running/paused
  &rarr; `cancelled`) via `orchestrator.cancel_run` (mirrors POST
  `/api/runs/{run_id}/cancel`). It is the escape hatch for a run stuck paused at a
  `human_review` / external-vendor step, and - because a `cancelled` run stops
  counting as in-flight - it is the action that unblocks mid-flight re-authoring
  (the "cancel them, then move/edit/delete" the authoring guards point at). Terminal
  and **not** resumable (launch a new run instead); refunds nothing and closes any
  open vendor leg. An already-terminal run exits **1** with a clean message.

A `FAILED` job/run still prints its row/error but exits **1** (operation
failure).

**Jobs - `job …`**

| Command | What it does |
|---------|--------------|
| `job run <kind> [--payload K=V …] [--payload-json FILE]` | Run a job kind **synchronously** and block until it finishes. Kinds: `translate_document`, `connector_translate`. `--payload K=V` is repeatable (a pure-digit value becomes an int - `document_id`, `connection_id`, `project_id` are integer keys); `--payload-json FILE` supplies the whole payload with types preserved. Prints progress + the final result; exit 1 if the job failed. |
| `job status <id>` | Show one job (status, progress, message, error, result). |
| `job list [--kind K] [--status S] [--actor SUBSTR] [--document ID] [--workflow ID] [--include-runs] [--runs-only] [--limit N] [--offset N]` | List recent jobs, optionally filtered by kind/status/actor and by the **document** or **workflow** targeted (`--workflow` implies runs/batches only - a job has no workflow). `--actor` matches `created_by` as a case-insensitive substring. By default only background JOBS are listed: `--include-runs` adds workflow runs to the unified history, `--runs-only` shows runs alone. `--offset` pages alongside `--limit` (default 50). Scoped-existence: an unknown or out-of-scope id yields an empty list, indistinguishable from a nonexistent one (no cross-org existence oracle). Remote-capable (`GET /api/jobs?document=&workflow=`). |

Over `--remote`, `connector_translate` requires `can_tune_ai`, and `data_export` is not remotely launchable through `job run`. Backfill batches are bounded to 1 through 1000 rows.

**Workflows - `workflow …`**

| Command | What it does |
|---------|--------------|
| `workflow create --name N [--scope global\|customer\|project --customer-id ID]` | Create a workflow. |
| `workflow list [--archived active\|all\|only]` | List workflows (id, name, scope, step count, shape). The human table adds lean **runs** + **last run** columns; `--json` (and `/api/workflows`) additionally carry the full single-sourced usage + quality stats per workflow - `runs_total`, `runs_30d`, `last_run_at`, `projects_served`, `customers_served`, `spend_usd` (honest `null` when every run predates spend tracking - never a fake `0`) with `spend_partial` (`true` when the total is real but at least one run stamped no cost, so it is a floor, not the full spend), `active`, `lqa_pass_rate` + `lqa_gate_count` (share of the workflow's LQA gate outcomes that passed, `null` when it produced no LQA gate), and `needs_review_rate` + `needs_review_count` (share of runs left paused/unresolved at a human checkpoint) - the SAME `app/services/workflow_stats` figures the web dashboard shows, run-scoped to the caller's visible projects. Archive filter: `active` (default) hides archived workflows, `all` includes both, `only` lists archived workflows. |
| `workflow show <wf>` | Show a workflow and its ordered steps. `--json` includes, per step, `needs_review` (the linguist-review flag set on LLM-adapted cross-language variants) and `adapted_from_id` (the source step a variant was cloned from, `null` on a plain add-step) - the same fields the web builder / group page surface, so a headless consumer can see which variant steps are flagged and their cross-language provenance. The `/api/workflows/{id}` step rows carry the identical shape. |
| `workflow archive <wf>` | Archive (retire) a workflow - it drops out of the default `workflow list` + every picker and is **excluded from dispatch/variant resolution** and **blocked from launching new runs**; its steps + run history are **preserved** and it is **reversible**. Not a delete and not a kill switch (an in-flight run finishes untouched). In-process only (the `/api/workflows/{id}/archive` route serves the web + remote-API surfaces). Idempotent-safe. |
| `workflow unarchive <wf>` | Unarchive (reactivate) a workflow - list it, offer it in pickers/dispatch, and make it runnable again. In-process only. |
| `workflow add-step <wf> [--type llm_agent\|lqa\|human_review\|tm_match\|external --vendor ID --external-wait-hours N --name N --provider P --model M --fallback-provider P --fallback-model M --prompt TEXT --prompt-template ID --chunk-policy auto\|none --redact-pii --adaptive-feedback --adaptive-window N --doc-context-prepass]` | Append a step (order = current count). `--redact-pii` scrubs PII before this step's engine call, restores after. `--adaptive-feedback` (F2.1) feeds earlier confirmed source->target pairs of the same document into later segments' prompts (`--adaptive-window` sets the rolling window, default 5). `--doc-context-prepass` (F2.2) runs one cheap pre-pass call to extract a doc-level brief prepended to every segment prompt (counts against the spend cap). Both default OFF. **TM provenance guard** (LQA safety floor, Phase 1 D1): `--tm-protect auto|on|off` (default `auto`) decides whether segments filled by a **100% TM match** are excluded from this step. `auto` protects only a **correction-loop** step (one gated on `prev_lqa:failed`, or the loop-back target of an LQA `on_fail` branch) - so a fix step that runs because the *document-level* LQA failed can no longer silently rewrite a byte-exact approved match and have the run report it as fresh MT; `on` always protects; `off` never does. Global kill switch: `config set tm_fix_protection_enabled 0`. Web parity: the step editor's **TM protection** select; `/api` `GET /api/workflows/{id}` echoes `tm_protect`. **Over-translation (page-dump) guard** (run-190 Item D): `--over-translation-guard auto|on|off` (default `auto`, `llm_agent`-only) controls the classic segmented guard that rejects a candidate looking like a WHOLE-PAGE dump (a code fence / cross-language table / multi-line-from-single-line, or text far longer than the source - the ceiling is `max(160, len(src)*OVER_TRANSLATION_MAX_RATIO + 60)`), retries once with a strengthened single-unit instruction, and if it still dumps WITHHOLDS the segment to NEEDS_REVIEW (never committing the dump). `auto`/`on` inherit the install guard; `off` opts this step out - use it for a legitimately EXPANDING rewrite / post-edit / summarize step whose target is much longer than its source. Install-wide kill switch: `config set over_translation_max_ratio 0`. A non-default value is **refused** on `lqa`/`human_review`/`tm_match`/`external`. Web parity: the step editor's **Over-translation guard** select; `show --json` and `GET /api/workflows/{id}` echo `over_translation_guard`. **LLMemory step-level opt-out** (PROPOSAL-llmemory section 6): `--memory-mode auto|off` (default `auto`) decides whether this step ever gets a recall block injected into its prompt. `auto` (== unset/NULL) participates in memory recall normally; `off` means NO memory block is ever injected here, whatever the global switches say. **Opt-out only** - it can never turn recall ON for a step the orchestrator's own step-role predicate already excludes (an LQA/judge step, or the correction-loop fixer never gets recall regardless of this flag). Web parity: the step editor's **LLMemory recall** select; `show --json` echoes `memory_mode`. **Concept-glossary renderer** (SPEC-terminology-backend T3): `--concept-glossary` (default OFF, `llm_agent`-only) renders the run's matched glossary **concepts** - audience-gated (a `human`-only term never reaches the model), ordered (DNT/forbidden first), and capped - into this step's prompt instead of the plain term list; `--glossary-concept-cap N` (default 10, 0 = no cap) and `--glossary-token-cap N` (default 0 = inherit the install `glossary_prompt_token_cap`, default 1500) bound it server-side. With it OFF (the default) the prompt and the enforced glossary set are byte-identical to before; with it ON the enforced set IS the shown set (no step flags a term the model was never shown). A non-default value is **refused** on `lqa`/`human_review`/`tm_match`/`external`. Web parity: the step editor's **concept-glossary** controls; `show --json` and `GET /api/workflows/{id}` echo `concept_glossary_render`/`glossary_concept_cap`/`glossary_token_cap`. **Saved prompt templates** (SPEC-prompt-template-runtime): `--prompt-template <id>` binds a saved prompt template (see `workflow prompt list`). At run time the effective prompt is resolved **inline `--prompt` wins -> else the bound template's body -> else the default** for the step type; the template must exist, be visible to your org, and (for `llm_agent`) its body must contain `{text}`/`{prior}` - a dangling/invalid binding is refused at bind time (and, defensively, falls back to the default with an honest run-trace note). `--prompt-template` is one of the dead fields **refused** on `lqa`/`human_review` (same as `--prompt`). **Honest step-type options** (SPEC-lqa-step-honest-options): an `lqa` step scores with a fixed internal MQM judge prompt + the pair rulebook - `--prompt`/`--chunk-policy`/`--glossary`/`--adaptive-feedback`/`--doc-context-prepass`/`--no-tm-examples`/`--qe-gate`/`--qe-threshold`/`--qe-provider`/`--tm-protect`/`--memory-mode` have NO effect on it and a non-default value is **refused** (`--provider`, `--model`, `--fallback-provider`, `--fallback-model`, `--redact-pii`, `--run-condition`, `--branch`, `--max-visits`, `--key`, `--parallel-group` all apply - the LQA judge honors the engine/model/fallback chain like an llm_agent step, per feat/prompt-package-engine). The one config an `lqa` step **adds** is **`--lqa-threshold <0-100>`** (feat/lqa-configurable-threshold): its numeric **pass bar** - the step FAILS unless the card scores at or above it (the no-major-error rule is unchanged). Unset keeps the built-in gate of **90**, so existing workflows are byte-identical; set it (e.g. `--lqa-threshold 94`) to require a higher score, which then drives the step's `on_fail`/`on_pass` branch. `--lqa-threshold` is **lqa-only** - a non-default value is **refused** on `llm_agent`/`human_review`/`tm_match`/`external` - and is DISTINCT from `--qe-threshold` (the QE routing cutoff on an llm_agent step, dead on lqa). Web parity: the step editor's **LQA pass threshold** field; `show --json` and `GET /api/workflows/{id}` echo `lqa_threshold`. `human_review` makes no provider call at all - every one of the lqa-dead fields PLUS `--provider`/`--model`/`--fallback-provider`/`--fallback-model` themselves are refused (only `--run-condition`, `--max-visits`, `--key` apply at add-step; the reviewer **assignee is set afterward** with `workflow edit-step --assignee-email` - that flag is NOT on `add-step`). **`tm_match`** (PROPOSAL-tm-matching section 7) is likewise **orchestration-only** - it makes no provider call and carries no prompt (it fills 100% TM matches and *locks* them; its policy lives on the TM, not the step), so the same dead-field set as `human_review` is refused, plus `--branch` (validated as a non-gate step) - only `--run-condition`, `--max-visits`, `--key` apply. **`external`** (PROPOSAL-external-processing-step) is **orchestration-only** as well -- it makes no provider call and carries no prompt; it PACKAGES the current text and DISPATCHES it to the selected `--vendor` (fail-closed: the vendor must be linked to the document's customer), then the run PARKS awaiting the vendor's return (Wave A is dispatch-only; the guarded re-ingest lands in a later wave). Its only live config at add-step is `--vendor`, `--external-wait-hours`, `--run-condition`, `--max-visits`, `--key` (the `--assignee-email` - emailed on dispatch + expiry escalation - is set afterward via `workflow edit-step`, not `add-step`); every model/prompt/glossary/QE field is refused, and `--vendor`/`--external-wait-hours` are in turn refused on every non-external step. A tm_match step placed at the front of a workflow fills + locks qualifying segments so every later step (MT, LQA, fix rounds) skips them; a hard lock is additionally refused at the write seam. Human reviewer edits win over a lock (the edit clears it, audited); the explicit unlock is `doc unlock-segment <segment_id>` / `POST /segments/{id}/unlock`. **Parallel steps** (SPEC-parallel-routing Phase 1): `--parallel-group <slug>` makes this step run **at the same time** as the CONSECUTIVE neighbouring steps carrying the same slug. Every member scores the SAME frozen snapshot of the working text; the step after the group starts only when all of them have finished (the join), and their results are written in `order_index` order in one commit. **Phase 1 is CHECKS ONLY**: members must be `lqa` steps - `--parallel-group` is a dead field **refused** on `llm_agent` and `human_review` (a check only READS the text, so lanes need no merge rule; a text-producing lane is Phase 2). Members must be consecutive; at most ONE member may carry a `--branch` (it routes at the join, off its own gate); a member's `--run-condition` resolves against the steps BEFORE the group, never a sibling lane. Lanes share the existing `mt_concurrency` pool - no new setting, no extra ceiling. A judge outage (`judge unavailable`) on ANY lane of a group, not only the routing lane, ends the run held at the group exactly like the sequential path. A lone member is inert (it just runs on its own) and is shown as such by `workflow show`'s `par` column. Web parity: the step editor's **Parallel steps** field and the `∥ group:` chip; `show --json` and `GET /api/workflows/{id}` echo `parallel_group`. **Run-condition refs**: `--run-condition <ref>:<op>[:<value>]` where `ref` is `prev_lqa`, `prev_qe`, or `step:<key>` and `op` is `passed|failed|score_gte|score_lt` (e.g. `prev_lqa:failed`, `prev_lqa:score_lt:90`, `step:lqa_main:failed`). `step:<key>` names ONE earlier gate-producing step by its `--key` - it is **required** when the nearest matching steps are members of a parallel group, because a bare `prev_lqa` would then be ambiguous and is **refused at save time** (the refusal names the group and suggests the exact `step:` form). **Loop safety** (`--max-visits N`): a per-step per-run **execution** ceiling that binds the step in EVERY role it plays in the graph - as the TARGET a loop-back jumps into and as the step that OWNS the branch (checked before the step runs, so the canonical `lqa --branch on_fail=mt --max-visits 1` gate really does execute once). What happens when a step reaches its ceiling depends on what the step IS. A capped **check** (`lqa`, or a QE-gated step) ENDS the run: stepping over it would run the steps after it on text no gate passed. A capped **producer/fixer** is SKIPPED and the run carries on to the check, so the check always judges the last text that was produced - ending at a producer would leave that text unjudged *and* still written to the document. A step whose `--run-condition` is false is skipped as usual, ceiling or no ceiling. Neither case writes a step row for a step that did not execute, so `visits` counts executions only. Blank inherits `config workflow_max_visits` (5); only the run-level `workflow_total_step_cap` FAILS a run. **LQA correction loops make progress or stop** (issue #95): when a loop-back returns an `lqa` step to text **byte-identical** to text that same step has ALREADY judged in this run - the previous visit or any earlier one, so an oscillating repair counts too - the judge is NOT called again; the verdict it gave those exact bytes is carried forward with the reason on the step row, and the loop ends. **A run that ENDS with an `lqa` check's standing verdict still FAILING is HELD, and held means the text does not ship.** The rule keys on the VERDICT, not on the loop's shape, so it covers every ending: the no-progress carry, an exhausted loop (the step it loops back into is at its ceiling), a capped check, `on_fail: end`, `on_fail: next`, and a check with no branch at all. A check that could NOT be EVALUATED - the judge was configured but delivered no verdict - is held the same way and says so (`not evaluated: judge unavailable`): a configured-but-unevaluated check does not deliver. A check whose own `--run-condition` SKIPPED it is not held - you asked for it to be skipped. The **QE routing gate** (`--qe-gate` on an `llm_agent` step) is not a delivery hold at all: it scores every segment it produces and routes the below-threshold ones to review while the document still ships, which is its documented per-segment contract. ROUTING IS UNCHANGED - `on_fail: next` still runs the next step, so the ordinary `run_condition: prev_lqa:failed` fixer still runs; what is withheld is the DELIVERY. The run reports `completed`; every segment it produced lands **needs-review** with a critical gate-hold badge naming the check; `doc export` (and the web / `/api` / bundle / key exports) **refuse with a 409** naming the check and the reason, and `--allow-held` - which ships a source-filled file - cannot bypass it; and the run carries the reason at run level, printed by `workflow show-run` and returned by `GET /api/runs/{id}` in their DEFAULT views. The hold RELEASES two ways and only two: a reviewer **approves or edits every held unit**, or a later run whose **own check PASSES over the unit's new text** - the check must have judged the text that run actually saves, so a post-edit / fix step that REWRITES a unit AFTER the check leaves that unit held - a step that runs after the check and rewrites NOTHING withholds nothing, so where it sits in the graph never decides the answer. A later run with no check, or with a check that fails, releases nothing - re-running the workflow is not a way around a quality hold, and it is not a way to re-impose one either: a unit a reviewer APPROVED or EDITED is never sent back to needs-review by a later run, including a body the operator edited during a pause in the very run whose check then failed (it keeps `edited`, its protection and the badge), and approving a unit clears the badge that hold left on it. One case WIDENS the release: when the hold came from a QE-gated step's OWN correction loop ending (a loop ending, not the per-segment routing above), a later run whose QE gate PASSES over the unit's new text releases it too, so the workflow that created the hold can clear it. A carried **pass** is not a hold: the loop ends and the run goes forward on text the check passed. A non-deterministic judge asked twice about the same bytes is a re-roll, not a re-check; a repair that genuinely changed the text is re-judged normally. A failed check counts as repaired only when a text-rewriting step ran AFTER that check's latest failing verdict and BEFORE a later check passed (graph order alone is not enough: a repair from an earlier pass of a loop does not clear a later failure). The `done` text a finished run replays (SSE reconnect, `/api` `wait=true`, `--remote`, MCP) is the full-width text of every segment, including segments the TM-LQA exclusion kept out of the last LQA step. The `lqa` step's judge call is attributed like any other engine call - its resolved provider/model, `attempts`, `elapsed_ms`, `finish_reason` and `error_class` are stamped on the StepRun, so `failure_shape` diagnoses a FAILING judge and `report run tokens --group-by engine` files judge tokens under the engine that ran them (issue #96). See [Workflow step configuration flags](#workflow-step-configuration-flags) for `tm_match` settings. `--no-image-context` disables attached images for this step (normally on; only effective for `llm_agent` on a vision-capable engine). `--tm-examples-window N` selects the top fuzzy examples, clamped to 0-5 (default 3; 0 disables). |
| `workflow edit-step <wf> <step> [flags]` | **PARTIAL** in-place edit of an existing step - only the flags you pass change; the rest are untouched. Non-boolean fields (`--name`, `--type`, `--provider`, `--model`, `--fallback-provider/-model`, `--prompt`, `--prompt-template`, `--chunk-policy`, `--assignee-email`, `--adaptive-window`, `--qe-threshold`, `--qe-provider`, `--lqa-threshold` (an `lqa` step's `0-100` pass bar; refused on non-lqa steps), `--tm-examples-window`, `--tm-protect` (`auto|on|off`), `--over-translation-guard` (`auto|on|off`; `llm_agent`-only page-dump guard opt-out, run-190 Item D), `--memory-mode` (`auto|off`), `--glossary-concept-cap`, `--glossary-token-cap` (Terminology T3; `0`/blank = inherit the install cap), `--glossary`, `--run-condition`, `--key`, `--max-visits`) default to "unchanged"; an explicit value (incl. `''` for the clearable text fields - `--prompt-template ''` clears a saved-template binding; `--prompt ''` is **Restore default prompt**: the step uses its saved template or the default prompt for its type, #630) is a real edit. `--prompt-template <id>` binds a saved template (inline `--prompt` still wins at run time; see add-step). Boolean toggles are **tri-state** `--flag` / `--no-flag` / neither: `--redact-pii`, `--adaptive-feedback`, `--doc-context-prepass`, `--qe-gate`, `--tm-examples`, `--concept-glossary` (Terminology T3, `llm_agent`-only). Branching: `--branch on_pass=<target>` / `on_fail=<target>` (repeatable; target = `next`/`end`/a step key) or `--clear-branch`. Parallel steps: `--parallel-group <slug>` joins a group, `--no-parallel-group` leaves one (mutually exclusive; neither = unchanged - two flags rather than the `''` convention, because argparse cannot tell `--parallel-group ''` apart from "not passed"). Editing **re-validates the whole graph** (fail-closed on a new break); refused while a run is pending/running/paused. A step pinned to an `engine:<id>` keeps its pin when `--provider ''` (re-pin explicitly to change it). **`--remote`-capable** (POST `/api/workflows/{id}/steps/{sid}/edit`; matches add-step). **Honest step-type options**: for an edit that does NOT change the type, the same dead-field refusal as add-step applies to the step's type, but only for a flag you actually pass this call with a real (non-default) value that DIFFERS from the step's current stored value - an untouched legacy value on an already-dead-for-its-type step (e.g. a pre-existing `lqa` step with a stale `qe_gate`) is left alone, so it keeps loading/editing fine; only a freshly-set/changed dead field is refused. An edit that DOES change `--type` never refuses: every field the new type ignores (whether carried over or set in this same call) is force-cleared to its safe default and reported in the result under `type_switch_cleared`. See [Workflow step configuration flags](#workflow-step-configuration-flags) for `tm_match` settings. `--image-context` / `--no-image-context` enables / disables attached images (`llm_agent` on vision-capable engines); omit both to leave it unchanged. The same tri-state rule applies to `--redact-pii` / `--no-redact-pii`, `--adaptive-feedback` / `--no-adaptive-feedback`, `--doc-context-prepass` / `--no-doc-context-prepass`, `--qe-gate` / `--no-qe-gate`, `--tm-examples` / `--no-tm-examples` and `--concept-glossary` / `--no-concept-glossary`. `--fallback-model M` sets the failover model (blank clears it). `--pin` freezes a variant step against base-edit propagation; `--unpin` rejoins future propagation without retro-sync. Over `--remote`, a manager may pass `--prompt` alone (an empty `--prompt ''` restores the step's default prompt); any other flag needs `can_tune_ai` (2026-10-08). |
| `workflow delete-step <wf> <step>` | Delete a step by id. **Fail-closed, with the same two guards as `edit-step`** (deleting a step reaches further than editing one - it removes a node other steps depend on): (1) **refused while the workflow has an in-flight run** (pending/running/paused). `order_index` is never compacted on delete, so removing an earlier step re-seats every later one and a paused run would resume at the wrong step - with a parallel group in the workflow it could resume *inside* the group, where the engine does not fork and a lane silently never runs. Let the run finish or cancel it, then delete. (2) **refused when the delete would introduce a NEW graph error** - an orphaned `--run-condition` (its last upstream LQA/QE step is going away), a `--branch` whose target key is going away, or an ambiguous condition ref (deleting the lone gate between a parallel group and a downstream bare `prev_lqa` leaves two candidate producers). Clear the referring branch/condition first, then delete. A pre-existing inconsistency never blocks a delete - only breakage *this* delete introduces does. **This is a deliberate tightening:** deleting a branch target used to succeed and leave a dangling branch that fell through at run time; it is now refused at save time, consistent with how renaming or clearing that step's `--key` is already refused. Web parity: the builder's per-step Delete surfaces the same refusal as a 400 page. |
| `workflow move-step <wf> <step> (--to N \| --before KEY\|ID \| --after KEY\|ID) [--parallel-group SLUG \| --clear-parallel-group]` | **Reorder** a step (SPEC-workflow-canvas Phase 1). `--to N` is the 0-based position the step ends up at (`0` = first); `--before`/`--after` name another step by its `--key` or id and resolve to that position. This is the **only** operation that renumbers `order_index`, and it renumbers ALL steps contiguously `0..n-1` (add-step appends at max+1 and delete-step compacts nothing, precisely so no unrelated step ever shifts under an edit; a move owns the whole order by definition, so it also heals gaps a past delete left). **Fail-closed, with the same two guards as `delete-step`**: (1) **refused while the workflow has an in-flight run** (pending/running/paused) - a move re-seats steps around a paused run's stored cursor and there is no correct index to fix up to; (2) **refused when the move would introduce a NEW graph error** - a gate moved BELOW the step whose `--run-condition`/`--branch` depends on it, a move that SPLITS a parallel group (dropping an outsider between members, or dragging a member out of contiguity without clearing its slug), or a branch left re-entering a group at a non-first member. A pre-existing inconsistency never blocks an unrelated move. **TM-first follows the move, honestly:** the TM-first short-circuit fires on `order_index == 0`, so moving a translate step to position 0 makes it TM-first-eligible (a 100% TM match answers without calling the model) and moving it away removes that - reordering *means* this, it is not special-cased. `--json` returns the step's new `position` plus the whole resulting `order`. **`--remote`-capable** (POST `/api/workflows/{id}/steps/{sid}/move`; matches add-step/edit-step). Web parity: the builder graph's drag-to-reorder and the per-step **move up / move down** buttons both POST `/workflows/{id}/steps/{sid}/move`. **Lane membership composes into the same call:** `--parallel-group <slug>` joins or switches groups and `--clear-parallel-group` LEAVES one, applied together with the move and validated as one final state in one commit - leaving a group is only expressible this way, since clearing the slug without moving out of the members' run (or moving out without clearing it) is refused by the consecutiveness rule either way. A slug change is a real field edit, so it sweeps to non-pinned variants and auto-pins a directly-edited variant step exactly like `edit-step --parallel-group`; the result reports both. **Two honest consequences of reordering:** (1) a move can change which gate a bare `prev_lqa` / `prev_qe` reads, because those refs resolve to the NEAREST matching step upstream - pin the one you mean with `step:<key>` if the workflow must not drift; (2) reordering a base workflow's steps can make HISTORICAL batch matrices show a step-divergence flag for runs that executed under the OLD order - the runs themselves are unaffected, only the matrix's column alignment (known limit, tracked separately). A move that changes nothing (same position, same slug) is a no-op: it does not renumber, does not commit, and says so. |
| `workflow clone <wf>` | Deep-copy a workflow and all its steps into a new "(copy)". |
| `workflow rename <wf> --name N` | Rename a workflow. In-process only. |
| `workflow delete <wf> [--force]` | Preview the cascade and blockers; `--force` actually deletes. In-process only. |
| `workflow author-prompt <wf> <description> [--provider P] [--model M]` | AI-draft a step prompt from a plain-language description (prints the draft; `orchestrator.author_prompt`). Omit `--model` to use the provider default. |
| `workflow author <description> [--provider P] [--model M] [--create] [--auto-fix [N]]` | AI-draft a whole workflow (name + ordered steps) from a plain-language description and print the validated draft + warnings. A workflow takes its languages from the project it runs on, so the draft names no source/target and `--create` makes exactly one ungrouped workflow. `--create` applies it through the same `apply_workflow_draft` seam as the web confirm route (refuses on a remaining step-level validation error). `--adapt` is accepted for backward compatibility but is ignored (it prints that a designed workflow has no language variants; use a workflow group to keep per-language variants). `--auto-fix [N]` (with `--create`) attempts automatic repair up to **N** times (default **N=2** when the flag is given) via the same repair seam as the web **"Fix this for me"** button when a validation error remains: each round prints its errors + what the repair changed (up to 2 provider calls per round - one JSON-parse retry), and it still refuses cleanly (non-zero) if unresolved after N rounds. Mirrors `POST /workflows/author` + `/workflows/author/confirm` + `/workflows/author/repair` and the review screen's Fix-this-for-me button. |
| `workflow run <doc-id> <wf-id> [locale] [--all-locales \| --locales L1,L2] [--max-parallel N] [--fail-fast]` | Run a workflow over a document/locale **synchronously**. A positional `locale` is a single run (pauses + prints the resume command on a `human_review` step). **`--all-locales`** (or **`--locales`** for a subset) instead fans out across the document's target locales as one **spend-capped, concurrency-bounded batch** (SPEC Phase 5) - still synchronous in-process, joining every member before returning. `--max-parallel` (clamped 1..8) is the run-level concurrency; `--fail-fast` stops launching further locales once a member fails (in-flight members finish). Prints a per-locale rollup + the batch id; monitor/act on it with `workflow batch …`. FULL_TEXT workflows accept tm_match by default. Exact (100%) sentence matches from the step's selected readable TM pools are OFFERED, unverified, beside the glossary; {tm_offers} controls placement. The source body is unchanged. This is an offer, not a guarantee. If a string must be exact, FULL_TEXT is the wrong mode. Offers are WITHHELD from each step that redacts PII, because an offer reproduces the document's own sentence verbatim; the run message says so. Every later llm_agent step receives the block except steps that redact PII. A workflow that belongs to a DIFFERENT customer than the document (its project's customer) is refused on every surface (web, /api including wait, batch start and batch re-run or retry-failed, CLI local and --remote, MCP, key translate; a run plan shows it as a 'Run would be refused' warning) with HTTP 400 / exit 2 / a tool error saying the workflow belongs to a different customer than the document, and no run is created; only a global workflow or one of the document's own customer runs (#542). |
| `workflow show-run <run-id> [--visits] [--findings]` | Show a run's status and, for a **paused** human-review run, the review text split into explicit **segment boundaries** (`GET /api/runs/{id}` parity; mirrors the `/runs/{id}` run-view page's newest-pause read). Use it to read the text safely before editing - a script edits the `segments[]` individually and never hand-touches the delimiter. Non-paused runs print `paused:false`. The default `step_states` block is **latest-per-step** (one row per step, the LAST visit wins). `--visits` additionally attaches `visit_history`: one entry **per StepRun row** (a loop step that failed then re-ran shows BOTH visits, each with its own status/score/gate/provider/model/attempts/error/latency, plus its recorded `cost_usd`/`tokens_in`/`tokens_out`) - the row-level trace `step_states` folds away. Both `step_states` (summed across a step's visits) and `visit_history` (per row) carry the recorded per-step spend: `cost_usd`/`tokens_in`/`tokens_out`, `null` on runs finished before spend tracking. Additive/opt-in (`?visits=1` on `/api/runs/{id}`); omitted by default so existing output is byte-identical. `--findings` (BUG-07, fixed 2026-07-29) prints the LQA MQM findings block (`dimension`/`severity`/`note` per issue) for this run - previously only score+gate were visible headlessly. Read-only from the run's ALREADY-PERSISTED scorecard (`WorkflowRun.context["lqa_findings_block"]`, the same text the orchestrator's LQA step renders for a downstream fixer step): **no LQA re-run, no spend**. `""`/omitted when the run never reached an LQA step. Opt-in (`?findings=1` on `/api/runs/{id}`), mirroring `--visits`. The envelope also now always carries `workflow_id`/`document_id`/`target_locale`/`created_by`/`started_at`/`finished_at` (BUG-09) plus `warnings` (a run-level warnings list - `[]` when none) so a run is attributable without a second lookup - local and `--remote` are key-identical. **`progress`** (GitHub #101) is the LIVE position of a long step - `{done, total, unit, step, step_index, at}`, e.g. `47/102 segments` on step 2 "Translate" with the timestamp of the last tick - published by the orchestrator from INSIDE the step (per segment as the fan-out lands, per chunk on the FULL_TEXT path) and printed as a `progress:` line. Before it, a step-run row only appeared when the step ENDED, so a 10-25 minute `llm_agent` step reported `step_states: []` here, `translated: 0` on the document and silence on the SSE stream: an operator could not tell a working run from a wedged one without turning on the default-OFF LLM Ledger. It counts the units that actually LANDED - a segment whose provider call raised is not counted, so a step that died on segment 2 of 6 reports `5/6`, never a cosmetic `6/6` beside `failed`. The counter is PER STEP and restarts at `0/M` on each one (and `unit` changes, segments → chunks), which is why `step`/`step_index` ride beside it: read a smaller number against a different step as a transition, not a regression. `null` when the run published none (a single-unit step, a legacy run) - honest "no progress recorded", never a false 0%. **Under load the counter can go quiet:** the persist is skipped outright whenever the connection pool has no free slot (telemetry never queues ahead of a translation), so a `show-run` in a DIFFERENT process than the executor can report the step's last persisted position rather than its live one until the pool frees up - read `at` to tell "not written" from "not moving". **`reason`** (GitHub #106) is WHY a **failed or cancelled** run ended that way when a RECONCILER put it there rather than a step: `{code, text}` with a stable code (`interrupted_by_restart`, `superseded_stale`, `cancelled_by_operator`, `write_contention`, `superseded_by_recovery`, `run_crashed`, `pool_exhausted`) and the reconciler's own sentence as text, printed as a `reason:` line and carried on the SSE `closed` event. Such a run has no StepRun to blame, so `failure_shape`/`step_states` are empty for it by construction and every surface used to show a bare `failed`/`cancelled` plus a timestamp. `null` on every ordinary run - and also on a failed run whose `notes` carry only an informational sentence (a `tm_match` pool that was not readable, a vendor dead-letter escalation): `reason` reports ONLY what a terminal reconciler wrote, never an unrelated note under a "Reason:" heading. Both fields belong to the CLASSIC workflow engine; Advanced-tier missions publish neither. **`failure_shape`** (run-190 Item C) rides the envelope BY DEFAULT: one compact metadata row per StepRun (attempts, provider, model, `finish_reason`, `error_class`, sanitized `error`, `output_chars`, `elapsed_ms`) - no prompt/response text - so the shape of a failure (e.g. 3 attempts, all `finish_reason=length`, 0 output) is visible without an SSH into the host Ledger. For a segmented step the row carries the WORST-CASE segment shape: `finish_reason`/`error_class` describe the first failing segment in order-index order (any burned, errored, or gracefully-degraded segment wins), so a mixed step never reports a clean `stop` beside a failure. `output_chars` counts only the text that step visit actually PRODUCED: a segment freshly translated this visit, or one filled by a 100% TM match at step 0 (this visit's delivery), is counted; carry-over kept from a PRIOR visit (withheld / degraded / fix-reverted / status-protected / TM-locked) counts 0. |
| `workflow step-diff <run-id> [--from STEP_RUN_ID] [--to STEP_RUN_ID]` | GitHub #340: what a step **CHANGED**, segment by segment, word-level - the headless twin of the run view's **Highlight differences** page (`/runs/{id}/step-diff`) of `GET /api/runs/{id}/step-diff` and of the MCP `work_status(run_id=ID, step_diff=true)` (optional `from_step_run_id` / `to_step_run_id`). Read-only over text the run **already recorded** (each step's `StepRun` output is the run's per-segment text joined by the segment delimiter; this splits it - **no provider call, no spend**). Without flags the pair defaults to the run's newest **translating** output vs its previous translating step (translate -> post-edit round 1 -> post-edit round 2; a pass-through step such as a trailing LQA never wins the pair, so the default view is never a false '0 changed'); `--from`/`--to` name any two StepRun ids of this run instead (the output lists every step run id with its own count). Human output: `changed: N of M segments` plus one line per segment with insertions in `[+ ... +]` and deletions in `[- ... -]` (the non-colour markers on this surface). `--json` prints the full payload: per-segment `before`/`after`/`changed` + word-level `diff` ops (`equal`/`insert`/`delete`), `changed`/`total` counts, a `steps` overview (each step run id with its own N-of-M count vs the previous translating step), and honest `not_recorded` when a side stored no output. Local and `--remote` are key-identical (`GET /api/runs/{id}/step-diff?from=&to=` parity). |
| `workflow resume <run-id> [--edited-text TEXT \| --edited-segments JSON_FILE] [--locale L]` | Resume a **paused** run **synchronously**. `--edited-text` (blank = accept the produced MT as-is) OR `--edited-segments` (a JSON file holding an array of strings, one per segment in order - the safe structured path; the tool assembles the delimiters). The segment count is validated **before** any state change: a mismatch is **refused cleanly** (`ok:false`, exit 1) and the run **stays paused and re-resumable** - it is NOT terminally failed (issue #23). `--locale` is inferred from the document if omitted. Resuming a non-paused run is a clean usage error (exit 2). Passing both `--edited-text` and `--edited-segments` is a usage error. The run resumes only in **its own target locale**; `--locale` is optional and a different one is a usage error (exit 2). The resume claims the run under the run-create lock and flips it to **running** before the remaining steps execute (the same as the web and `/api` resume), so two overlapping resumes can no longer both run the segment set: the second is refused (`rejected`, `run is not paused`) and the run is advanced exactly once. |

#### Workflow step configuration flags

`workflow add-step` and `workflow edit-step` share these six **`tm_match`-only**
flags; passing them on another step type is refused. On add, omitted values inherit
the global settings and seed a concrete snapshot. On edit, omitted flags leave the
stored value unchanged; explicit `auto` (or `inherit` for the two scalar flags and
the lock mode) clears the override back to inheritance.

The **QE routing gate** (`--qe-gate`, `--qe-threshold N` default 70, `--qe-provider P`;
`llm_agent` only, segmented documents only) scores every segment the step produces and
routes one below the threshold to review while the document still ships. `--qe-provider`
picks the judge: blank reuses the step's provider, a built-in or LLM `engine:<id>` is an
MQM-aligned LLM judge, and a **quality judge** is an `engine:<id>` of kind `jev` (hosted,
priced, spend-capped) or `jev_local` (a server you run such as SemIf, free), or the legacy
`jev` (the `TYPESAFE_API_KEY` setup). A Jev verdict is pass, fail (below the threshold,
even with an unsure dimension: routed to review), undecided (at or above the threshold with
an unsure dimension: never a pass, but the segment carries on unchanged, its `qe` flag
reads "QE undecided", it is not routed to review, and it counts as QE skipped) or not scored (the
segment carries on, counted as QE skipped). `engine inspect <id>` prints a judge's
exact rule.

They also share the four **QE auto-fix** flags (an `llm_agent` step with `--qe-gate` and a
Jev QE judge: `--qe-provider jev` or an `engine:<id>` of kind jev / jev_local). A segment
that fails the gate is rewritten by an LLM post-editor and re-scored by the same judge; a
passing version replaces it, anything still failing goes to review with every round's
score and text (the best rewrite is kept for the reviewer only when the judge was sure of
it and it scores strictly higher than the original, allowing baseline unsure dimensions
to remain flagged for review). Confident major dimensions are fixed even alongside unsure
dimensions; those are left unchanged for review. PII-redacting steps and output withheld
as a refusal are never auto-fixed (an undecided segment is not a fail, so it never enters the loop); the segment's `qe_autofix` flag
then says `auto-fix skipped` with the reason. A loop that can cost money (hosted Jev or a
paid post-editor) does not start, and stops between rounds, once the spend cap is
reached; a fully local loop keeps running. On add the defaults are off / the step's own
provider / 3 rounds / off; on edit an omitted flag leaves the stored value unchanged.

| Flag | What it does |
|------|--------------|
| `--fill-threshold PCT` | Minimum match score that fills a segment (clamped to 50-100; 100 = exact matches only). `auto`, `inherit` or a blank value inherits `tm_fill_threshold`. |
| `--fuzzy-lock-mode auto\|inherit\|none\|soft\|policy` | Lock for a below-100 fuzzy fill: `none` leaves a suggestion a later translate step can overwrite; `soft` enters LQA; `policy` uses the same lock as the 100 band. `auto` / `inherit` inherits `tm_fuzzy_lock_mode`. |
| `--lock-ice auto\|on\|off` | Lock in-context exact (ICE) matches. `auto` inherits `tm_lock_ice`; `on` / `off` overrides it. |
| `--hundreds-enter-lqa auto\|on\|off` | Send 100% matches to LQA with a soft lock instead of hard-locking them. `auto` inherits `tm_hundreds_enter_lqa`. |
| `--context-floor PCT` | Near-match context score floor (clamped to 50-99); forward-compatible configuration for the ICE/context wave. `auto`, `inherit` or a blank value inherits `tm_context_floor`. |
| `--tm-pools IDS` | Comma-separated readable project ids whose TM this step reads. Blank / `auto` selects all readable pools; `(none)` reads no pool. An id outside the run's readable set is dropped with a note at run time; a value naming no valid id is refused. |
| `--qe-autofix` / `--no-qe-autofix` | Turn the QE auto-fix on (edit: `--no-qe-autofix` turns it off). |
| `--qe-autofix-provider P` | The post-editor LLM: a provider name or an `engine:<id>` of an LLM kind. Blank = the step's own provider; an NMT or judge engine is refused, and so is auto-fix on a machine-translation step with no LLM post-editor named. On edit, `--qe-autofix-provider ""` clears a pinned post-editor back to the step's own provider. |
| `--qe-autofix-max-rounds N` | Post-editor rounds per failed segment, whole numbers 1-5 (default 3); out-of-range and junk refused. |
| `--qe-autofix-cross-check` / `--no-qe-autofix-cross-check` | Score each accepted rewrite with the post-editor LLM's own second opinion, shown to it. No round-0 or failed-rewrite cross-check. |

QE workflow save validation: `qe_threshold` accepts whole numbers 0-100 (default 70); `qe_autofix_max_rounds` accepts whole numbers 1-5 (blank or absent = 3). Decimal forms, including 70.0, junk and out-of-range values are refused on web, CLI, API, MCP and setup-author saves. Explicit auto-fix post-editors must name an existing LLM engine in scope. Existing stored values are unchanged.

Example - build a workflow, run it, clear a human-review pause:

```bash
python -m app.cli workflow create --name "EN→FR review" --scope global
python -m app.cli workflow add-step 1 --type llm_agent --name Translator --prompt 'Translate: {text}'
python -m app.cli workflow add-step 1 --type human_review --name "In-country review"
python -m app.cli workflow add-step 1 --type llm_agent --name Finalizer --prompt 'Finalize: {text}'

python -m app.cli workflow run 42 1 fr-FR          # → "Run #5 PAUSED at human-review step 1 …"
python -m app.cli workflow show-run 5              # → lists the segment(s) to review with boundaries
python -m app.cli workflow resume 5 --edited-text "reviewed text" --locale fr-FR   # → completes
# safe structured edit (never hand-touch the delimiter): one JSON array element per segment
python -m app.cli workflow resume 5 --edited-segments edited.json --locale fr-FR   # → completes

# abort a run stuck paused (or still running): the escape hatch, and it unblocks re-authoring
python -m app.cli workflow cancel-run 5           # → "Run #5 CANCELLED (0 open vendor leg(s) closed)…"

# a document-translation job, run to completion in-process:
python -m app.cli job run translate_document --payload document_id=42 --payload locale=fr-FR
python -m app.cli job list --kind translate_document
```

**Fan-out batch monitor + actions - `workflow batch …`** (SPEC Phase 5b)

A batch (from `workflow run --all-locales`) is one observable unit: rows = its requested
locales, columns = the pipeline steps. `batch show` prints that matrix (each cell a member
step's status + LQA/QE score + loop-visit count); `batch rerun` / `retry-failed` re-dispatch
fresh member runs INTO the same batch (the prior runs stay in history; the matrix shows the
latest attempt per locale). All three are **remote-capable** (`--remote`, over the `/api/batches`
endpoints); the write actions run synchronously server-side and re-open a finished batch for the
duration, then re-finalize its rollup from the latest attempt per locale.

| Command | Notes |
| --- | --- |
| `workflow batch show <batch-id>` | Show the locale × steps matrix as an ASCII table (`--json` emits the same detail shape the web monitor polls - per-member `step_runs` + the requested-step baseline). Codes: `ok`=passed/completed, `run`=running, `rev`=in-review, `FAIL`=failed, `.`=pending, `-`=step absent in this variant. Mirrors `GET /api/batches/{id}?detail=steps` and the `/batches/{id}` page. |
| `workflow batch rerun <batch-id> --locale L` | Re-run ONE locale as a fresh member run into the same finished batch. An unfinished batch refuses a competing re-run. The locale must be one of the batch's requested targets and its **latest run must be terminal** (an in-flight member is refused). An un-launched locale (e.g. spend-stopped) is launched. Mirrors web `POST /batches/{id}/rerun` + `/api/batches/{id}/rerun`. |
| `workflow batch retry-failed <batch-id>` | Re-run, retry-failed and run-now require a finished batch (finished_at is set). Re-run EVERY failed locale of a finished batch as one action, bounded by the batch's `max_parallel` (not N client calls). A no-op (nothing failed) prints so cleanly. Mirrors web `POST /batches/{id}/retry-failed` + `/api/batches/{id}/retry-failed`. |

```bash
python -m app.cli workflow run 42 1 --all-locales --max-parallel 3   # → "Batch #7 completed: …"
python -m app.cli workflow batch show 7                              # → the locale × steps matrix
python -m app.cli workflow batch rerun 7 --locale de-DE             # → re-runs one locale into batch #7
python -m app.cli workflow batch retry-failed 7                     # → re-runs every failed locale
```

**Multilingual workflow groups - `workflow group …`**

A group ties one base workflow to per-language-pair variants; a run's target locale is
resolved to the exact variant, else the group's default/universal member, else the
requested workflow (`resolve_variant`). Group create/list/show/add/set-default are **remote-capable**; only
`workflow group rename` and `workflow group delete` are **in-process only**.

**Advanced (per-language variants):** a workflow works for every language as it is, so
groups and `workflow copy --to-pairs` are an Advanced feature for the rare case that one
language needs a different prompt. They stay available on every surface; the web moves
them behind an Advanced disclosure.

| Command | What it does |
|---|---|
| `workflow group create --name N [--customer-id ID] [--description D]` | Create a group (blank customer = global). |
| `workflow group list` | List groups + their variant counts. |
| `workflow group show <group>` | Show a group and its per-language-pair variants (which is default). |
| `workflow group add <workflow> <group> [--pair SRC:TGT] [--default] [--force]` | Add a workflow as a variant. **A blank `--pair` on a FRESH add = universal (group-default candidate); re-adding an already-grouped member without `--pair` PRESERVES its existing pair** (so `--default` never silently universalizes it). An explicit `--pair` sets the pair on a FRESH add; RE-STATING the member's current pair (any spelling) changes nothing - the stored spelling is left exactly as it was, and only `--default` applies. Giving a pair to a member that has NONE (a universal variant) is always allowed - nothing is replaced. An explicit `--pair` that would REPLACE an already-grouped member's existing pair is **REFUSED** (that was the one path that silently dropped a language from a family - the error names the pair the member currently holds); add a separate variant with `workflow copy --to-pairs` instead, or pass **`--force`** to replace it deliberately (the output NAMES the `old -> new` pair and warns the old one is now uncovered; `--json` carries `replaced_pair`). `--default` flags it the group fallback. Web equivalent: `POST /workflow-groups/{id}/add` (same `force` field). |
| `workflow group set-default <workflow> <group>` | Make an existing group member the group's fallback default variant (clears the flag on its siblings; exactly one default). Mirrors web `POST /workflow-groups/{id}/default`; validates membership first. With the System prompt overrides feature on, the web and `--remote` (API) forms need an admin or a manager when the old or the new default has its own system prompt text, since the default is the base its pair variants inherit from (#365); `group add --default`, and moving a group's default into another group, follow the same rule. Over `--remote`, `set-default` is open to an admin, a linguist or a manager (2026-10-08). |
| `workflow group rename <group> --name N` | Rename a group. |
| `workflow group delete <group> [--force]` | Delete an EMPTY group (preview without `--force`; fails closed while it has members). |
| `workflow copy <wf> --to-pairs SRC:TGT[,…] [--adapt]` | **Advanced (per-language variants).** Copy a workflow onto other language pairs inside a group; `--adapt` LLM-rewrites each variant's step prompts for its language (post-edit required). Each new variant is named from the source's STEM plus exactly one `[src->tgt]` suffix (copying FROM a variant never compounds suffixes), and a pair the group ALREADY covers is reported `skipped` rather than duplicated - the other requested pairs are still created (the duplicate guard re-checks inside the write transaction under a SAVEPOINT - **on SQLite**, whose single-writer lock serializes the rival, two concurrent copies of the same pair yield exactly one variant; on Postgres under READ COMMITTED both inserts can pass the re-check, so a rare concurrent duplicate is possible and is harmless to dispatch - the lower id wins deterministically. A UNIQUE index is the real cure and needs a migration). Every locale is validated against the language catalog BEFORE anything is written: an unknown code fails the command and creates nothing. Web equivalent: the builder's *Copy to language pair(s)* and the group page's *Add a language pair*. |

**Reusable prompt templates**

Saved templates can be bound with `workflow add-step --prompt-template ID` or
`workflow edit-step --prompt-template ID`. The inline prompt takes precedence over
the saved body. `workflow prompt create`, `workflow prompt list` and the `workflow prompt system*` verbs are remote-capable;
`workflow prompt rename` and `workflow prompt delete` are in-process only.

| Command | What it does |
|---------|--------------|
| `workflow prompt create --name N [--body TEXT] [--customer ID]` | Create a reusable prompt template. Omit `--customer` for global scope; a customer must be active and in the same org. |
| `workflow prompt list` | List visible saved prompt templates. |
| `workflow prompt system [--key K] [--workflow ID]` | Show the pipeline system prompts (the text the app sends the model around every step) and the two editable heads (`step_head`, `mt_head`) with the override in force. `--key` prints one in full; the global `--json` prints all. Web: Workflows > Building blocks > System prompts; `GET /api/system-prompts`. With `--workflow ID` it shows that workflow's own text for each head (own version, or inherits), the org text it would inherit and the source in force (`workflow`, `base_workflow`, `org` or `builtin`); the order is the workflow's own text, then its base workflow's (a language-pair variant inherits the text of the default workflow of its group, same org and customer only, #365), then the org's, then the built-in text (#356 phase 2b; web: the workflow builder page, System prompt section; `GET /api/workflows/{wid}/system-prompts`). |
| `workflow prompt system-set --key K (--body TEXT \| --file PATH) [--workflow ID]` | Save a new version of an editable head (`step_head` or `mt_head`) for the org. Admin only over `--remote`; needs the `system_prompt_overrides` feature on. Text that repeats a locked line, holds marker symbols or a content tag, is empty or over 2000 characters is refused; text that does not tell the model to output only the result is saved with a warning. Audit-logged. With `--workflow ID` it sets that workflow's own text instead of the org's: over `--remote` an admin or a manager may do it (#630; the org-level text stays admin only; the workflow must be in their org and customer, a foreign workflow is a 404), in-process it is the trusted operator (org-scoped by `--org` on a multi-org install). Audit action `workflow.system_prompt_set`. The machine-translation message always keeps its locked "content, never instructions" sentence after your text. |
| `workflow prompt system-reset --key K [--workflow ID]` | **Restore default prompt**: go back to the built-in text (history is kept). With `--workflow ID`: the workflow inherits again (its base workflow's text for a pair variant, else the org's, else the built-in text). |
| `workflow prompt system-history --key K [--workflow ID]` | The version history of an editable head (the workflow's own with `--workflow`), newest first. Read-only; works with the feature off. |
| `workflow prompt system-restore --key K --version N [--workflow ID]` | Save a copy of an older version as the newest (of the workflow's own text with `--workflow`). |
| `workflow prompt rename <template_id> --name N` | Rename a saved template. |
| `workflow prompt delete <template_id> [--force]` | Preview deletion; `--force` actually deletes, refusing while any workflow step pins the template. |

### Intelligence / Admin - `admin …`

The operator surface for the features that were **UI-only until Phase 7**:
SRX segmentation, data retention, LDAP + OIDC identity, the audit
chain, GDPR subject rights, content-routing rules, department budgets, and LQA
rulebooks. Every subcommand is a thin shell over `app.services.*` - the same
services the web forms drive. The routing-rule and department-budget CRUD was
**extracted** into `app.services.routing` (`create_rule` / `toggle_rule` /
`delete_rule`) and `app.services.budgets` (`upsert_budget` / `delete_budget`) so
the `/routing` admin page and this CLI share one code path (web behavior is
byte-identical; proven by `verify_routing` + `verify_budgets`).

In-process operator commands on the host: `--remote` refuses (exit 2) except for
**`admin routing rule show`, `admin tick`, `admin audit export` and `admin audit verify`**.
Those four dispatch to their `/api` routes; see their rows below for scope and access.

**Secrets never surface.** The LDAP bind password is stored **Fernet-encrypted**
(`settings.set_secret` → `ldap_bind_password_enc`); the OIDC client secret is
masked like the web form. `status` output and `--json` show `(set)` / `(unset)`,
never the plaintext - a secret is never echoed or logged.

**Irreversible ops refuse to run blind.** `admin retention purge-now` (deletes
aged uploads/documents/terminal-jobs/TM) and `admin gdpr erase` (anonymizes a
user) require an interactive confirmation **or** `--yes`, and **refuse** in
`--json` / `--quiet` without `--yes`. `purge-now` first echoes exactly what it
will remove before asking.

**SRX segmentation - `admin srx …`**

| Command | Effect |
| --- | --- |
| `admin srx enable` | Enable `srx_enabled`. |
| `admin srx disable` | Disable `srx_enabled` (use the legacy regex splitter). |
| `admin srx status` | Show the segmentation policy + active ruleset. |
| `admin srx validate <file>` | Parse-validate an `.srx` file (writes nothing; an invalid file is a clean failure, exit 1). |
| `admin srx upload <file>` | Validate **then** install a custom ruleset at `DATA_DIR/segmentation.srx` and point `srx_file` at it. An invalid file is rejected and nothing is persisted. |

**Data retention - `admin retention …`**

| Command | Effect |
| --- | --- |
| `admin retention config [--enabled on\|off] [--days N] [--yes]` | Set the policy. `--days 0` = no window = nothing is ever deleted, even when enabled. Config **never** triggers a purge. `retention_enabled` and `retention_days` are security-sensitive settings, so the change asks for confirmation like `config set`; `--yes` skips the prompt (without a terminal the command refuses and writes nothing). |
| `admin retention status` | Show the policy (and warn when enabled-but-days-0). |
| `admin retention purge-now [--yes]` | Run one sweep **now** - DESTRUCTIVE. Echoes the blast radius (docs/segments/jobs/files) first, then confirms. Refuses if retention is disabled or `days=0`. |

**Scheduler tick - `admin tick`**

| Command | Effect |
| --- | --- |
| `admin tick` | Run **one scheduler pass now**, synchronously: the piggybacked auto-close / vendor-expiry / prompt-tap sweeps, then - behind `sync_scheduler_enabled` - the vendor network sweeps and any due connector schedules. This is the **external-cron entry for the embedded / headless profile** (`LOC_TMS_EMBEDDED=1`), where the in-process scheduler daemon is not started; pair it with `admin retention purge-now` (or `POST /api/admin/retention/purge`) as the external-cron duo. Harmless in a normal install (the daemon already ticks every 30 s; `tick_now` serializes the two so they never double-run). **`--remote`-capable** (`POST /api/admin/tick`, admin scope + admin role - **not** behind `remote_admin_enabled`, since a tick is the routine passage of time, not an irreversible admin verb). Reports the count of connector-sync jobs it enqueued. A concurrent tick returns *busy* (exit 1 locally / HTTP 409 remote). |

Retention also ages TM and learned memory rows when their project still has documents.
SegmentTM uses `coalesce(activity_at, last_used_at, created_at)`; FullTextTM uses
`coalesce(activity_at, created_at)`. Creation, import, re-approval, edits and accepted
matches refresh `activity_at`, independently of translation usage statistics. Matching
surviving document sources and key catalogues protect TM. Memory events are erased;
unsupported beliefs and citing suggestions are blanked into tombstones. Both TM and
memory aging commit bounded batches; a failure reports committed partial counts.

**LDAP - `admin ldap …`**

| Command | Effect |
| --- | --- |
| `admin ldap config [--enabled on\|off --host URI --base-dn DN --bind-dn DN --bind-password PW --user-filter F --default-role R --ca-cert PATH --yes]` | Set LDAP settings. Every `ldap_*` key is security-sensitive: the change is validated first, then confirmed like `config set` (`--yes` skips the prompt; no terminal and no `--yes` means nothing is written), and the bind password is stored only after the rest of the batch was accepted. `--bind-password` is stored **encrypted** and never echoed. `--host` must be `ldaps://`, `ldap://` (StartTLS is then mandatory) or a bare host (normalised to `ldaps://`); the server certificate is always validated. `--ca-cert` points at a PEM bundle for an internal CA; left blank the system trust store is used. |
| `admin ldap status` | Show LDAP settings (bind password shown only as `(set)`/`(unset)`). |
| `admin ldap test-login <user> [--password PW]` | Attempt a real LDAP login against the configured directory (`ldap_auth.try_login`). Success prints the resolved identity; a wrong password / no match / disabled directory is a clean failure (exit 1). |

LDAP host DNS labels must contain 1 to 63 ASCII letters, digits, hyphens or underscores,
with no empty labels. Wildcards, IP literals, control characters, percent escapes, trailing dots,
raw internationalised names and scoped IPv6 addresses are refused. Use a concrete DNS name that the
directory certificate carries as a subject-alternative name; an IP literal cannot validate, because
only `DNS` SANs are checked. DNS names supply TLS SNI.
See [the LDAP upgrade notes](SECRETS.md#upgrading-an-existing-ldap-install-read-before-you-upgrade)
for previously stored host forms that must be corrected.

**OIDC - `admin oidc …`**

| Command | Effect |
| --- | --- |
| `admin oidc config [--enabled on\|off --issuer URL --client-id ID --client-secret SECRET --default-role R --trust-unverified-email on\|off --yes]` | Set OIDC settings. Validated and confirmed like `config set` (`--yes` skips the prompt; no terminal and no `--yes` means nothing is written); the client secret is stored only after the rest of the batch was accepted. `--client-secret` is masked; never echoed. `--trust-unverified-email on` lets a first OIDC sign-in link an account by email without `email_verified` (only for an IdP whose addresses are admin-assigned; default off). Linked accounts are matched by issuer and `sub`. |
| `admin oidc status` | Show OIDC settings (client secret shown only as `(set)`/`(unset)`). |

**Audit - `admin audit …`**

| Command | Effect |
| --- | --- |
| `admin audit export [--since ISO --actor EMAIL --action SUBSTR --customer ID] [-o FILE] [--force]` | Stream the chain as NDJSON (oldest first). `--customer` narrows to one customer's rows, matching the customer-scoped `/audit` view. Each row ships `prev_hash` + `row_hash` so a downstream store can re-verify the chain. `-o FILE` refuses to overwrite an existing file unless `--force`. The export **self-logs** an `audit.export` row before streaming, on every surface (web, `/api`, CLI). Works over `--remote` (GET `/api/audit/export`); there a customer-bound key exports only its own customer, and a `--customer` id that is missing or another customer's answers `no such customer.` (#586). `--out PATH` is the long form of `-o PATH`. |
| `admin audit verify` | Verify the keyed hash chain end to end (exit 1 if broken). Works over `--remote` (GET `/api/audit/verify`). |

**GDPR - `admin gdpr …`**

| Command | Effect |
| --- | --- |
| `admin gdpr export <user> [-o FILE] [--force]` | Subject-access export as NDJSON (no secrets). `-o FILE` refuses to overwrite an existing file unless `--force`. `--out PATH` is the long form of `-o PATH`. |
| `admin gdpr erase <user> [--yes]` | Anonymize a user - DESTRUCTIVE, irreversible. Confirms unless `--yes`. |

**Routing rules - `admin routing rule …`**

| Command | Effect |
| --- | --- |
| `admin routing rule add [--ext E --department D --priority N --project ID --provider P]` | Create a rule (`routing.create_rule`). A rule matches on **ext + department** and targets a **project and/or provider**. `--subject`, `--workflow` and `--language-set` still parse but are **refused** (reserved for v2, not consulted at run time yet - exit 2). The referenced project must **exist within the caller's org** - a cross-org referent is refused as nonexistent (#26); under `--org` the new rule is **stamped** to that org. |
| `admin routing rule list` | List rules in priority order. Confined to the `--org` scope (in-process) - a scoped operator never sees another org's rules. A trailing note flags any rule carrying inert reserved fields (subject/workflow/language set). |
| `admin routing rule show <rule-id>` | Read back one rule's full config (human + `--json`): priority, match ext, department, project, provider, active, created_by/at (the reserved subject/workflow/language-set fields are shown too, flagged `reserved v2, not consulted` when set). Org-scoped (a cross-org id is refused as nonexistent). Remote-capable (`GET /api/routing/rules/{id}`, admin-only). |
| `admin routing rule toggle <rule-id>` | Flip a rule active/inactive. Org-scoped under `--org` (a cross-org id is refused identically to a missing one). |
| `admin routing rule delete <rule-id> [--yes]` | Delete a rule (confirms unless `--yes`). Org-scoped under `--org` (a cross-org id is refused identically to a missing one). |

**Department budgets + per-project money budgets - `admin budget …`**

| Command | Effect |
| --- | --- |
| `admin budget set <department> <limit>` | Create-or-update a department's monthly token cap (`budgets.upsert_budget`; case-insensitive upsert; `limit <= 0` = unlimited). |
| `admin budget list` | List budgets + this month's spend. |
| `admin budget delete <budget-id> [--yes]` | Delete a budget (confirms unless `--yes`). |
| `admin budget project set <project-id> <limit>` | Create-or-update a project's monthly USD cap (`project_budgets.upsert_project_budget`; one row per project; `limit <= 0` = unlimited). Counts AI model spend + settled vendor human spend. |
| `admin budget project list` | List project budgets + this month's spend (enforced) and committed-but-unpaid. |
| `admin budget project delete <budget-id> [--yes]` | Delete a project budget (confirms unless `--yes`). |

**LQA rulebooks - `admin lqa rulebook …`**

| Command | Effect |
| --- | --- |
| `admin lqa rulebook get <src> <tgt>` | Print the language-pair rulebook (empty if none). |
| `admin lqa rulebook set <src> <tgt> <file>` | Install a language-pair rulebook from a file. |

Examples:

```bash
# LDAP requires validated TLS (bind password stored encrypted). Bare hosts use LDAPS;
# ldap:// requires StartTLS before binding. --ca-cert selects a PEM CA bundle;
# omit/clear it to use system roots (also: config set ldap_ca_cert /path/to/ca.pem).
# Configure LDAP and smoke-test a login:
python -m app.cli admin ldap config --enabled on --host ldaps://dc.corp \
    --base-dn "dc=corp,dc=test" --bind-dn "cn=svc,dc=corp,dc=test" --bind-password '••••' --yes
python -m app.cli admin ldap test-login alice@corp.test --password '••••'

# retention: set a 90-day window, preview + purge (irreversible):
python -m app.cli admin retention config --enabled on --days 90 --yes
python -m app.cli admin retention purge-now --yes

# export the audit chain for a SIEM and verify it:
python -m app.cli admin audit export --since 2026-01-01 -o audit.ndjson
python -m app.cli admin audit verify

# routing + budgets:
python -m app.cli admin routing rule add --ext srt,vtt --priority 10 --project 3
python -m app.cli admin budget set marketing 500000
```

### Reports - `report …`

CSV cost / token / volume / qa / postedit reporting over finished jobs, LQA findings and
post-edits (`app.services.reports`, the same seam the `/reports` page uses).
**Remote-capable** (`GET /api/reports/run`, org-bound by the token). `qa` is the MQM LQA
dimension×severity cross-tab (from `LQAResult`); `postedit` is edit distance and
post-editing effort (from `SegmentPostEdit`).

**Access (CV-4 + Wave 2):** the `cost` kind is **manager+** - it exposes AI (model) spend,
so a `--remote` cost report requires a key whose owner clears `can_manage`; a viewer/
translator/reviewer/linguist key is refused with a 403 (`cost reports require the manager
role (or higher).`) and writes no audit row. The **`actor` axis** (on `qa`/`postedit`) is
likewise **manager+** - it ranks named people by output - and is refused with a 403 (`the
actor axis requires the manager role (or higher).`) on a `--remote`/`/api` call - and because a postedit bucket key is an editor's EMAIL (PII), this gate is **always-on**: an under-ranked docs-scoped key is refused even when the login wall is OFF (unlike the `cost` gate, which no-ops under auth-off single-tenant). The `tokens`,
`volume`, `qa` and `postedit` kinds and every other axis stay readable to any in-scope
viewer. The **local** `report` verbs run as the trusted operator, so `report run cost` and
`--group-by actor` work locally regardless of role (like every other local read). This
default is a **source edit**, not a setting: to make cost reports read-open change
`reports.can_see_cost` to `return True`, and to open the actor axis change
`reports.can_see_actor_axis` (one place each). **Asymmetry CLOSED (2026-08-19):**
a **linguist** is refused the cost report here, and now also no longer reads install-wide AI spend on
`/setup-ai` - that page stays linguist-accessible but its spend meter is now **manager+** (`can_manage`), so a non-manager sees a neutral stub, not the figures. The two surfaces now agree under the login wall: whole-install AI spend is manager+ on both. (Auth-off nuance: the /api cost gate still no-ops without the login wall; the actor axis, unlike cost, is gated always.)

Gated by the `reports_enabled` kill switch (default on, ffadmin Wave 4): with it off the
LOCAL `report` verbs refuse cleanly (`reports are disabled (setting reports_enabled=0).`),
the `/reports` page + `GET /api/reports/run` 404, and the `run_report` MCP tool answers
404 on call. A `--remote` `report` command is answered by the server, whose flag state is
authoritative. Re-enable with `config set reports_enabled 1`.

| Command | What it does |
|---------|--------------|
| `report run <kind> [--from YYYY-MM-DD] [--to YYYY-MM-DD] [--customer ID] [--project ID] [--provider NAME] [--group-by GB] [-o CSV]` | Run a report of `kind` (`cost` \| `tokens` \| `volume` \| `qa` \| `postedit`) and print a table, `--json`, or write CSV with `-o`. An unknown kind is rejected at the CLI door (clean usage error - never a silent 'cost' fallback, issue #25). `--group-by` in `project` \| `customer` \| `provider` \| `engine` \| `date` \| `workflow` \| `locale` \| `vendor` \| `dimension` \| `actor` \| `none` (default `none`); the group-by key column is prepended and a TOTALS row appended. An axis a kind does not support (e.g. `--kind cost --group-by dimension`) degrades to `none` **with a caveat** naming the degradation - never a silent cap. `workflow` reports **EXACT** per-workflow spend: each workflow row's cost/tokens is the sum of that workflow's runs' recorded per-step spend (`StepRun` costs, the same figures the spend ledger accrued), bucketed by run finished date. Runs feed the workflow buckets; plain machine-translation and connector **jobs** (which carry no workflow - a `workflow run` writes no Job row) roll up as `(no workflow)`, so the two never double-count. On this axis the count column counts **runs** for workflow rows. Runs finished **before** per-step spend tracking existed have no recorded cost and surface as a separate `(untracked)` row (counted, cost/tokens blank) rather than being silently counted as zero. On **`provider`/`engine`** a step that made **no provider call** (a human review, an orchestration-only `tm_match`/`external` step, a skipped or cap-halted visit) has no engine to attribute to and buckets as **`(none)`** - a row the report now LABELS in its caveats (web caption, CSV footer, `/api` + `--json`) the way it already labels `(untracked)`; the two are different things (`(none)` is about which engine ran, `(untracked)` about spend tracking). An `lqa` judge's tokens are NOT in `(none)` - they are attributed to the judge's own engine (issue #96). Date filters match the run's finished date. **`locale`** groups by **target locale** - jobs by `payload.locale`, runs by `WorkflowRun.target_locale`, and segment volume by `Segment.target_locale` (each honestly attributed; a job/segment with no locale -> `(untracked locale)`, a run with no recorded cost -> `(untracked)`). **`vendor`** is a **scorecard** sourced from vendor dispatch legs (external/human work), with its OWN columns - `legs`, `word_count`, `returned_on_time` (returned <= expiry), the LQA quality columns `lqa_findings` / `lqa_major_critical` / `avg_lqa_score` (machine-judged quality attributed to the vendor, windowed on each finding's own date), and the **human (MSA) spend money group** - `estimate_cost_usd` vs `actual_cost_usd` over **returned** legs, `open_estimate_usd` (sum of estimate over **in-flight** legs: dispatched/quoted/retry_wait/escalated - so an in-flight-only vendor no longer reads as free), and `quoted_cost_usd` (sum of the operator-**approved** quote, a separate figure from estimate/actual, excluding proposed-only and cancelled/dead-letter/rejected-superseded legs) - all four cost columns admin/operator-only (`_can_see_human_spend`), the rest viewer-readable; a `--provider` filter and the kind's cost/token/volume columns do not apply to it. **`dimension`** (`qa` only) is the MQM dimension×severity cross-tab. **`actor`** (`qa`/`postedit`) buckets by the machine source/judge (`qa`) or the human editor / vendor (`postedit`) and is **manager+** on a remote call (enforced even when the login wall is off). On `qa`/`postedit` the `vendor` axis reads `LQAResult.vendor_id` / `SegmentPostEdit.vendor_id` (a different report from the leg-denominated scorecard). `--out PATH` is the long form of `-o PATH`. |
| `report kinds` | List the report kinds and the columns each renders. |

**Report kinds → columns** (the key column from `--group-by` is prepended; `words_derived` is labelled "derived" and is never an authoritative count; `human_cost_usd` is additive but appears **only** on the `--group-by workflow` axis AND **only for an admin/operator** - on every other axis and for a non-admin / customer-bound caller the column is **dropped from the output entirely**, not zeroed. On `postedit`'s `actor`/`vendor` axes the per-segment columns `words_derived` / `tte_ms` / `avg_tte_ms` are omitted and `mt_chars` / `final_chars` become episode-scoped so a segment touched by two actors is not double-counted. Every `avg_*` column renders **blank, not 0**, when nothing was measured, and is never summed into the TOTALS row):

| Kind | Metric columns |
|------|----------------|
| `cost` | jobs, segments, words_derived, chars, tokens_in, tokens_out, tokens_cache_read, tokens_cache_write, cost_usd, human_cost_usd |
| `tokens` | jobs, segments, tokens_in, tokens_out, tokens_cache_read, tokens_cache_write |
| `volume` | jobs, segments, words_derived, chars |
| `qa` | findings, critical, major, minor, neutral, other, human, cards, avg_card_score |
| `postedit` | segments_edited, episodes, edits, words_derived, mt_chars, final_chars, distance_char, distance_word, avg_distance_pct, tte_ms, avg_tte_ms |

```bash
# monthly cost grouped by provider, written to CSV:
python -m app.cli report run cost --from 2026-07-01 --to 2026-07-31 --group-by provider -o july-cost.csv
# MQM LQA findings as a dimension x severity cross-tab:
python -m app.cli report run qa --group-by dimension
# post-editing effort per editor (actor axis; local operator is trusted):
python -m app.cli report run postedit --group-by actor
python -m app.cli report kinds
```

### Queries - `query …`

Threaded queries and comments on a segment / key / document / project (SPEC-wave3 §4.4).
Parity with the web `/queries` surface and `GET|POST /api/queries*`: all three call
`app.services.query_threads`. **In-process the `query` group is the trusted operator** - no
login, no RBAC - so the author is `--actor` (or `system`) and every verb passes
`trusted_operator=True`; under the multi-org gate it threads `--org` so a shared-host operator
cannot sweep every org. `--remote --token` hits the `/api`, which is org-bound and
RBAC-enforced (§4.5) server-side by the token. A query is one conversation, visible whole to
everyone who can see the project. Disabled by `queries_enabled=0` (the `query` group then
refuses: `queries are disabled (setting queries_enabled=0).`, exit 1). **Notifications are
email only** - no in-app centre, no digest, no webhook; an unassigned query notifies nobody,
and without SMTP nothing is sent and nothing is logged.

| Command | What it does |
|---|---|
| `query open --segment N --body B [--subject S] [--assignee E]` | Open a thread scoped to a segment (or `--key`/`--document`/`--project` - exactly one anchor) with a first post. `--subject` ≤ 200 chars, `--body` ≤ 4000 (a longer value is **refused**, never truncated). `--assignee` must be an existing active user in scope (the mail-relay guard). In-process the author is `--actor` or `system`. Parity: `POST /api/queries`. |
| `query list [--status open\|answered\|closed] [--project ID] [--document ID] [--segment ID] [--key ID] [--assignee E] [--mine] [--limit N] [--offset N]` | List threads over your visible projects (max 50/page, `total` always returned; scope applied before pagination). `--mine` filters to `--actor`'s own threads. Parity: `GET /api/queries`. |
| `query show <id>` | Show one thread + its posts (oldest first). An out-of-scope id is not found. Parity: `GET /api/queries/{id}`. |
| `query reply <id> --body B` | Post a reply. A plain reply **never** marks the thread answered. Parity: `POST /api/queries/{id}/posts`. |
| `query answer <id> --body B` | Post a reply that marks the thread **answered**. Parity: `POST /api/queries/{id}/posts` with `answer=1`. |
| `query close <id> [--reopen]` | Close a thread (author / assignee / reviewer+), or `--reopen` a closed one (author / reviewer+). Parity: `POST /api/queries/{id}/status`. |
| `query assign <id> [--assignee E]` | Set the assignee (author / reviewer+; the target must be an in-scope active user), or clear it with an empty `--assignee`. Parity: `POST /api/queries/{id}/assign`. |

```
python -m app.cli --actor me@example.com query open --segment 42 --subject "tone?" --body "Is this too formal?"
python -m app.cli --actor me@example.com query list --status open --mine
python -m app.cli query answer 7 --body "Yes, use the informal register."
python -m app.cli query close 7
```

### My work - `work …`

The cross-project reviewer worklist ("My work", SPEC-wave3 §8.4 / LC-5): paused review
steps that name you, queries assigned to / answered for you, and documents needing quality
attention. Parity with the web `/my-work` page and `GET /api/worklist`; all three call
`app.services.worklist`. **In-process the `work` group is the trusted operator** - it has no
session user, so it MUST be told whose work to show: `work list` requires `--actor <email>`
(or `--remote`), and refuses otherwise rather than returning an empty list. **Assignment lives
on the workflow DEFINITION** (`WorkflowStep.assignee_email`), so a review step naming you
surfaces **every paused run of that workflow** - per-run assignment does not exist yet. The
quality section is `major`/`critical` LQA findings from a declared window (`--days`, default
90); `--quality needs_review` swaps it for the off-by-default NEEDS_REVIEW segment list (a
machine flag, not an assignment). Disabled by `worklist_enabled=0` (the `work` group then
refuses: `the reviewer worklist is disabled (setting worklist_enabled=0).`, exit 1).

| Command | What it does |
|---|---|
| `work list [--actor E] [--days N] [--quality needs_review] [--limit N] [--offset N]` | Your cross-project worklist (review + query + quality sections), newest first, max 50/page with the true `total`. Needs `--actor <email>` in-process (or `--remote`). The query section disappears when `queries_enabled=0`; the quality section is reviewer+ only. Parity: `GET /api/worklist`. |

```
python -m app.cli --actor me@example.com work list
python -m app.cli --actor me@example.com work list --quality needs_review
python -m app.cli --remote https://host --token lsu_... work list
```


### Reviewer LQA findings - `lqa …`

Reviewer-authored MQM findings on a segment (SPEC-wave3-collaboration §6.5). Parity with
the web LQA page's reviewer surface and `POST|GET /api/documents/{id}/lqa/findings` +
`POST /api/lqa/findings/{fid}[/withdraw]`: all call `app.services.lqa`. This is a **core**
surface (no feature flag) and it manages **findings only** - **running a scoring is
web-only** (`POST /documents/{id}/lqa/run` has no CLI/`/api` twin), which is the concrete
reason QA-2 stays PARTIAL. In-process the `lqa` group is the trusted operator (author =
`--actor`, default `cli`); `--remote --token` hits the `/api`, which is **reviewer+**
RBAC-enforced server-side (edit/withdraw additionally need own-row or manager+). A finding
persists and feeds the `qa` report but **never**
changes the machine LQA score or a workflow gate.

| Command | What it does |
|---|---|
| `lqa log --document N --segment M --dimension D --severity S --note T` | Log one reviewer MQM finding on a segment (which must belong to the document). `--dimension` must be a real MQM dimension (a free sub-category after `/` is allowed); `--severity` is critical/major/minor/neutral; an empty note is **refused**. Parity: `POST /api/documents/{id}/lqa/findings` (reviewer+). |
| `lqa findings --document N [--locale L] [--machine]` | List non-withdrawn reviewer findings for a document, optionally one locale. Parity: `GET /api/documents/{id}/lqa/findings`. `--machine` reads stored machine findings (workflow-run + ad-hoc) with the saved score/gate instead of reviewer findings. That mode is in-process only. |
| `lqa edit <fid> --dimension D --severity S --note T` | Edit a finding's dimension/severity/note (own-row or manager+; a machine row is refused). Parity: `POST /api/lqa/findings/{fid}`. |
| `lqa withdraw <fid>` | Soft-withdraw a finding (stamps `withdrawn_at`, never deletes; own-row or manager+). A withdrawn finding drops from the LQA page and every report bucket but is **retained** in the append-only lakehouse. Parity: `POST /api/lqa/findings/{fid}/withdraw`. |

```
python -m app.cli --actor rev@example.com lqa log --document 5 --segment 42 --dimension Accuracy --severity major --note "mistranslation"
python -m app.cli lqa findings --document 5 --locale es-ES
python -m app.cli lqa withdraw 9
```

### Content routing - `routing …`

Inbound **content-routing rules** - the feature-gated top-level twin of the `/routing`
admin page and the `/api/routing/rules` endpoints (`app.services.routing`, the same
service the web form and `admin routing rule …` call). A rule matches an inbound
submission by **file extension and department** and picks its **project and/or provider**
override. (Subject matching and workflow / language-set targeting are **reserved for v2** and
not consulted at run time yet, so they are refused at authoring time.) A rule takes effect on
**two API document paths only**: its **project** places an upload on `POST /api/documents`
(only when the caller names no project - an explicit project short-circuits the rules), and its
**provider** override applies on `POST /api/documents/{id}/translate` (reachable from /api, MCP and
`--remote` CLI doc translate) - the ONLY site where a rule's provider target has any effect. **Web-UI
uploads and in-process CLI document uploads consult no routing rules** (a known v1 asymmetry).
**Full remote parity** (`--remote --token`): each verb
hits its `/api/routing/rules` endpoint (admin-only, org-scoped by the token).

> **Two twins, on purpose.** `routing rule …` (here) is the **feature** surface, gated by
> the `routing_enabled` kill switch (below). `admin routing rule …` is the **admin-console**
> twin under the always-on `admin` spine (see *Intelligence / Admin*). They share one
> service seam, so a rule created through either is identical.

Gated by the `routing_enabled` kill switch (default on, ffadmin Wave 4): with it off the
LOCAL `routing` verbs refuse cleanly (`content routing is disabled (setting
routing_enabled=0).`), the `/routing` page + every `/api/routing/rules` route 404, and the
routing-rule actions of the `manage_admin_ops` MCP tool answer 404 on call. **Department
budgets and per-project money budgets are NOT gated** - `admin budget …` (incl. `admin
budget project …`), `/routing/budgets`, `/routing/project-budgets`, `/api/budgets` and
`/api/projects/{id}/budget` stay reachable (spend control is independent of content routing). The **runtime** routing
decision that drives inbound connector submissions is core pipeline behaviour and keeps
working (existing rules still apply). A `--remote` command is answered by the server, whose
flag state is authoritative. Re-enable with `config set routing_enabled 1`.

| Command | What it does |
|---------|--------------|
| `routing rule add [--ext E] [--department D] [--priority N] [--project ID] [--provider P]` | Create a rule (`routing.create_rule`, `priority` lower wins, default 100). A rule matches on **ext + department** and targets a **project and/or provider**. `--subject`, `--workflow` and `--language-set` still parse but are **refused** (reserved for v2, not consulted at run time yet - exit 2, byte-identical local vs `--remote`). A referenced project must **exist within the caller's org** - a cross-org referent is refused as nonexistent (#26); under `--org` the new rule is **stamped** to that org. |
| `routing rule list` | List rules in priority order, `--json`-capable. Confined to the `--org` scope - a scoped operator never sees another org's rules. A trailing note flags any rule carrying inert reserved fields (subject/workflow/language set). |
| `routing rule show <rule-id>` | Read back one rule's full config (human + `--json`): priority, match ext, department, project, provider, active, created_by/at (the reserved subject/workflow/language-set fields are shown too, flagged `reserved v2, not consulted` when set). Org-scoped (a cross-org id is refused as nonexistent). |
| `routing rule toggle <rule-id>` | Flip a rule active/inactive. Org-scoped (a cross-org id is refused identically to a missing one). |
| `routing rule delete <rule-id> [--yes]` | Delete a rule (confirms unless `--yes`). Org-scoped (a cross-org id is refused identically to a missing one). |

```bash
python -m app.cli routing rule add --ext srt,vtt --priority 10 --project 3
python -m app.cli routing rule list
python -m app.cli routing rule delete 7 --yes
```

### Data export - `export …`

A **RBAC-confined, self-describing dump of a scope's whole object graph** (Batch 13) - one
`.zip` of **JSONL-per-entity + `manifest.json`** (per-member + whole-artifact sha256), staged
to `EXPORTS_DIR` through the `ExportStore` port (`app.services.data_export`, the walker all
surfaces call). This is the CLI (host-only) slice; the `/api/exports` + `/exports` web surfaces
and the `--remote` arms are a later train. Every read is confined to the caller's existing
visibility (a customer-bound key can dump only its own customer; `--scope workspace` needs the
admin scope); a global/shared row never crosses a scope, and no secret column ever leaves.

Gated by the **host-only, default-on** `data_export_enabled` kill switch (a `writes_off` module -
reads stay up, the write path refuses when off). On an install that encrypts data at rest
(`encrypt_uploads`/`encrypt_db`), an export is **refused** unless
`data_export_allow_plaintext_staging=1` acknowledges plaintext-on-disk staging, or S3 staging is
configured (`export stages artifacts UNENCRYPTED on disk; …`).

| Command | What it does |
|---------|--------------|
| `export run [--scope self\|customer\|project\|workspace] [--ref ID] [--history current\|full] [--audit] [--include-eval] [--include-embeddings] [--emitters jsonl,csv,xlsx]` | Run one export. `--scope self` (default) = everything the caller can see; `--scope customer --ref <cid>` / `--scope project --ref <pid>` narrow it (an out-of-scope id is refused as nonexistent); `--scope workspace` = the caller-org whole-workspace dump (admin only). `--history full` adds the append-only/bi-temporal history (edit log, run/step history, key changelog, supersession chains). `--include-eval` / `--include-embeddings` opt in the Evaluations Lab tables / opaque vectors (both default off). Org-scoped under `--org`. Stages a ZIP to `EXPORTS_DIR`; a failed extractor lands the run `partial` (never aborts the whole dump). `--emitters` is a comma-separated set: `jsonl` is always included (and is the default); `csv` / `xlsx` add human-readable views. |
| `export list` | List past data-export runs (newest first), `--json`-capable. Org-scoped under `--org`. |
| `export status <run>` | Show one run's scope/history/status + per-entity row counts (+ any dropped-entity notes + expiry), by a local run id or a `run_uuid`. Org-scoped (a cross-org id is refused as nonexistent). |
| `export download <run_uuid> [-o FILE] [--force]` | Download a completed export artifact ZIP to a path (default `dataexport_<uuid>.zip`). Range-resumable over `--remote`. Only an `ok`/`partial` run with a staged artifact is served. `--out PATH` is the long form of `-o PATH`. |

All four verbs also take `--remote URL --token lsu_KEY` to drive the `/api/exports` routes instead of the local DB (`export run` then polls the job to completion). Example:

```bash
python -m app.cli export run                                 # dump everything you can see
python -m app.cli export run --scope customer --ref 7 --history full
python -m app.cli export run --scope workspace --audit       # admin: whole-org dump
python -m app.cli export list
python -m app.cli export status 3
python -m app.cli export download <run_uuid> -o dump.zip
```

### SCIM provisioning - `scim …`

The **read-only** operator view of **SCIM 2.0 directory provisioning** (Train I) - the CLI
twin of what your IdP (Okta / Entra) pushes to the `/scim/v2` endpoint. Provisioning itself
is **IdP-push by design**: the IdP creates, updates, deactivates and groups accounts over
`/scim/v2/Users` + `/scim/v2/Groups` (Bearer or `X-Loc-Token`, a key with the **`scim`
scope** owned by an **active admin**). There is no create/update verb here - only visibility.
Backed by `app.services.scim`.

Gated by the **host-only** `scim_enabled` opt-in switch (default **off**; `REMOTE_IMMUTABLE`,
never flippable over `/api`). With it off every `/scim/v2/*` path **404**s (in SCIM error
shape, non-enumerable). These verbs are pure reads, so they are **not** flag-gated -
`scim status` reports the switch either way. `--org <id>` confines the listings under the
multi-org gate. **Groups are directory metadata only: membership confers no authorization**
(no role, no visibility) in v1.

| Command | What it does |
|---------|--------------|
| `scim status` | Show the `scim_enabled` flag, counts of SCIM-managed users + groups, the default provisioning role, and the last SCIM sync activity from the audit trail. `--json`-capable. |
| `scim users` | List SCIM-managed accounts (`auth_source=scim` or an adopted `external_id`): email, name, active state, external id, auth source. Org-scoped under `--org`. |
| `scim groups` | List SCIM directory groups + their member emails. Org-scoped under `--org`. |

```bash
python -m app.cli scim status
python -m app.cli scim users --json
python -m app.cli scim groups
```

### Engine profiles - `engine …`

Registered **engine profiles** - saved cloud/local provider presets that appear in every
engine picker (project default, per-pair, workflow step, QE judge). The two **quality
judge** kinds, `jev` (hosted TypeSafe Jev) and `jev_local` (a Jev-compatible server you run,
such as SemIf or Kev), are the exception: they only score, so they appear only in a
workflow step's QE judge picker (`--qe-provider engine:<id>`), and saving one in any
translate slot or as an auto-fix post-editor is refused. Backed by
`app.services.engines`, the same seam the Setup AI page writes, so a profile made here is
byte-identical to one made in the UI. Secrets are Fernet-encrypted at rest and **never
echoed**: `engine list` shows a `key` column reading `set` (or blank when there is none),
and `engine show` prints `key: (set)` or `key: (none)`. A paired secret appears in the
config table as a masked `<name>_set` flag, never as ciphertext. Unlike `providers key
show`, these print no fingerprint. In-process operator commands on the host; `--remote`
refuses (exit 2).

Reference a profile anywhere a provider is expected as `engine:<id>`.

| Command | What it does |
| --- | --- |
| `engine list` | List profiles (id, name, kind, model, enabled, credential state). |
| `engine show <engine_id>` | One profile's full config: kind, base URL, model, non-secret params, enabled state, whether a credential is stored. |
| `engine create --name N --kind K [--base-url U] [--model M] [--secret K=V ...] [--config K=V ...] [--instruction TEXT ...] [--disabled]` | Register a profile. `--kind` is one of `local`, `openai_compatible`, `deepl`, `google`, `microsoft`, `cohere`, `lara`, `amazon` (**`lara` is EXPERIMENTAL**: its request signing needs live verification before production use), or a quality judge: `jev` (hosted Jev; needs `--secret api_key=...`, and its URL is pinned to `https://api.typesafe.ai`) or `jev_local` (needs `--base-url`, e.g. `http://127.0.0.1:8077` or a Tailscale address; the key is optional, with a warning when the server is off loopback and has none). A judge's `--model` defaults to `jev-latest`, and it takes `--config confidence_floor=0.6` (0.5 to less than 1.0), `--config meaning_floor=0.8` (0-1) and `--config timeout_s=30` (default 10 hosted, 30 local); unset = the install defaults. `--secret` and `--config` are repeatable `key=value` pairs: credentials go in `--secret` (`api_key=...`; `api_key=-` reads the value from one stdin line so it stays off the command line, plus `access_key_secret=...` for the key-PAIR kinds Lara and Amazon, whose access-key id goes in `api_key`), everything else in `--config` (`formality=more`, `model_type=quality_optimized`, `context='a help centre'` for DeepL; `region=eastus` for Microsoft, `region=us-east-1` for Amazon). `--instruction` is a repeatable DeepL custom instruction (up to 10, 300 chars each) and sets `config.custom_instructions`. `--disabled` registers the profile without offering it in the pickers. |
| `engine update <engine_id> [--name N] [--base-url U] [--model M] [--secret K=V ...] [--clear-key] [--clear-access-key-secret] [--config K=V ...] [--clear-config KEY ...] [--instruction TEXT ...] [--enable\|--disable]` | Change a profile. `--secret api_key=-` reads the new key from one stdin line (never echoed). Omitted fields and blank secret inputs keep their values, except that moving `--base-url` to another server (a different scheme, host or port) clears the stored key and paired secret unless `--secret` re-supplies them; the command warns and the move is audited. `--clear-key` removes the API key and disables hosted Jev; local Jev becomes keyless and warns off loopback. `--clear-access-key-secret` removes the Lara/Amazon paired secret. Setting and removing the same key together is refused. Config merges; `--clear-config` removes a config param, and blank judge floor/timeout values clear overrides. `--instruction` replaces custom instructions. |
| `engine models <engine_id>` | List the models the engine's server offers, using its stored key (the parity twin of Setup AI *Fetch models* on an existing engine). Prints one id per line; exit 1 with `FAILED: the server refused the key.` when the server answers 401/403. Never prints the key. |
| `engine test <engine_id>` | Live round-trip against the profile's endpoint using its stored credential (the parity twin of the Setup AI *Test connection* button). For a quality judge it lists the server's models, says whether the configured model is among them, and asks one question about a fixed test sentence (never your content), with each call's latency; on hosted Jev that costs a few tokens and is not sent once the spend cap is reached. |
| `engine delete <engine_id> [--yes]` | Delete a profile (confirms unless `--yes`). |
| `engine inspect <engine_id> [--json]` | For a quality judge (`jev` / `jev_local`) only, the parity twin of the Setup AI *Inspect* page: the questions Jev is asked with every level's meaning, the exact request body template (the key only ever rides in an Authorization header and is never printed), the decision rule rendered from the code's real constants and this engine's floors, and the Ledger link. Sends nothing and costs nothing. `--json` (same as the root flag) prints the whole inspection. A non-judge engine is refused (exit 2). |
| `engine jev-try <engine_id> --source TEXT --target TEXT --source-locale L --target-locale L [--threshold N] [--json]` | Score ONE segment through a quality-judge engine exactly as a QE-gated workflow step would, the twin of the Inspect page's *Try it*. Prints the verdict (`pass`, `fail`, `undecided` or `not scored`, the real gate decision), each dimension's level, cost and latency, the request that was sent, the raw response, and the exact round-1 prompt the auto-fix post-editor would get. Source and target must be text; locales must be codes from the language catalog, such as en, zh-Hans or pt-BR. `--threshold` is the step's QE pass threshold (0 to 100, default 70). Hosted Jev bills a fraction of a cent per try and honours the spend cap: once the cap is reached nothing is sent and the command exits 1. Tries are limited to 20 a minute per caller, one at a time per engine and one hosted try at a time; over that the command exits 1 with a retry time and sends nothing. These limits are kept per process: each CLI invocation and each server process has its own counters and locks, so separate `engine jev-try` runs (or a CLI run beside the web page) are not counted or serialized together. For hosted Jev the spend cap, checked before every call, remains the money backstop. An upstream answer that echoes the engine's key is shown with the key replaced by `[redacted]`. Every try is recorded in the Ledger (when it is on) as action `jev-try`, under its own org-owned Ledger action. `--json` prints the full result. |
| `engine import-jev-env` | Create a hosted Jev engine from this install's `TYPESAFE_API_KEY` (the Setup AI *Create a Jev engine from TYPESAFE_API_KEY* button). Idempotent: when a hosted Jev engine already exists nothing is created. The key is stored encrypted and never printed. |

Engine base URLs refuse userinfo (`user:password@`) on create or when changing the URL across
web, API, CLI and MCP; put credentials in the API key field. Legacy URL userinfo is hidden in
list/show/JSON and web/API displays without changing stored connections. An omitted URL or
one matching the displayed URL keeps the stored connection. Test, Inspect and Edit warn about it.
Web removal: engine Edit -> Remove key -> confirm checkbox -> Save. API update accepts
`clear_api_key: true` and `clear_access_key_secret: true`; blank strings still keep keys.
MCP never carries credentials.

Translation engines take `--config max_parallel_jobs=N` (1 to 64; blank on update clears it): at most N translate jobs run on that engine at once, never more than `LOC_TMS_JOB_WORKERS` (default 2). To use all 4 slots of a 4-slot local server, set `LOC_TMS_JOB_WORKERS=4` and `max_parallel_jobs=4` on it; paid engines stay unlimited-by-engine (so they too can use all workers) unless you give them a `max_parallel_jobs`. Each running job adds load: the 2026-10-07 production test peaked at 177% of 2 vCPU with two jobs.

Judge updates: blank `confidence_floor`, `meaning_floor` or `timeout_s` clears that override to the install default on CLI and API too. Blank `--model ""` resets a judge to `jev-latest`. Inspect warns about legacy profiles with confidence floor 1.0; those stored values are not rewritten.

```bash
python -m app.cli engine create --name "DeepL prod" --kind deepl \
  --secret api_key=... --config formality=more --config model_type=quality_optimized
python -m app.cli engine test 4
python -m app.cli project set-providers 7 --per-pair --pair-provider en-US:de-DE=engine:4
```

### Cloud provider keys - `providers …`

The three **built-in** cloud providers (`claude`, `gemini`, `openai`), whose keys live in
the install's own secret store rather than in an engine profile - the parity surface for
the Setup AI key fields. In-process only (`--remote` refuses, exit 2).

**A provider's key has a SOURCE, and there are two of them.** An **app** key is the one
stored (encrypted) by `key set`; an **env** key is one in `.env`. An app key overrides
`.env` immediately, and every command below reports which source is currently in effect,
which is the field that actually answers "is this provider live?".

**What is printed is a 4-character fingerprint, never the key.** `....AbCd` is the
key's last four characters, shown so you can tell two keys apart without revealing
either. The key value itself is never echoed by any of these commands.

| Command | What it does |
| --- | --- |
| `providers key show` | A row per provider: `provider`, `source` (`app` / `env` / `none`) and `fingerprint` (`....AbCd`, or `(none)` when no key is set from either source). `--json` adds `stored` and `has_env` so a script can tell the two sources apart directly. |
| `providers key set <claude\|gemini\|openai> [--key K]` | Store an app key, which takes effect immediately and overrides any `.env` key. Omit `--key` to read one line from **stdin** - the preferred form, since it keeps the secret out of your shell history. Surrounding whitespace and quotes are trimmed. It confirms with the fingerprint only - `claude: key set (source: app, ....AbCd).` |
| `providers key clear <claude\|gemini\|openai>` | Remove the **app** key only. **This does not necessarily take the provider offline:** if a key for it is present in `.env`, that one takes over and the provider stays live - the command says so (`now source: env`). Only when there is no env key either does it fall back to the offline mock (`now source: none`). To take an env-keyed provider off, remove it from `.env` and restart. |
| `providers models --provider REF` | Fetch the model pick-list from the provider itself, so you pin a model id that really exists. `REF` is `claude`, `gemini`, `openai`, `local`, or `engine:<id>` for a registered profile. This is what the Setup AI *Fetch models* button calls. |
| `providers search-url set [--url URL] [--provider brave\|searxng]` | Set the web-search backend and/or the self-hosted SearXNG base URL. The URL is validated through the SSRF egress guard on write - a loopback/private/metadata host is refused. Both values are validated before either is saved, so a refused URL never leaves the provider half-switched. |
| `providers search-url show` | Show the configured backend (`brave`/`searxng`) and the SearXNG base URL. |

### Segmentation profiles - `segprofile …`

A **segmentation profile** packages an SRX ruleset so different content can be split
differently. Which one a project uses is a chain: the project's own override, else its
customer's, else the system default, else the built-in rules. All eight verbs are
**remote-capable** (`--remote`, via `/api/segprofiles` and its per-profile routes).

| Command | What it does |
| --- | --- |
| `segprofile list` | List profiles (id, name, scope, whether it is the system default). |
| `segprofile show <profile_id>` | One profile's detail and its ruleset summary. |
| `segprofile create <name> [--file PATH] [--customer-id N] [--description D] [--system-default]` | Create a profile. `--file` is a `.srx` ruleset (max 1 MB, parse-validated before it is accepted); omit it to start from the built-in default rules. `--customer-id` scopes it to one customer (omit = global). `--system-default` installs it as rung 3 of the resolution chain. |
| `segprofile clone <profile_id> <new_name> [--customer-id N]` | Copy a profile under a new name. `--customer-id` re-scopes the clone; omit to keep the source's scope, or pass `''` to make it global. |
| `segprofile rename <profile_id> <name>` | Rename. |
| `segprofile assign <project_id> [--profile ID]` | Set a project's profile override. **Omit `--profile` (or pass a blank value) to CLEAR it** and fall back to the chain. |
| `segprofile set-default <profile_id> [--clear]` | Make this the system default, or `--clear` to leave the install on the built-in rules. |
| `segprofile delete <profile_id> [--yes]` | Delete a profile (confirms unless `--yes`). |

### Translation context - `context …`

The **context package**: the standing brief - domain, content kind, audience, formality,
tone, notes, character limit - rendered into every prompt as one fenced, data-only block.
Cards layer most-specific-first, and a document's own context sits on top of them. Same
seam as the web context surfaces (`app.services.context_pkg`). `context show`,
`context set-profile`, `context set-document` and `context ab` are **remote-capable**.
Remote `context set-profile` roles: org-default card: admin only; customer-scoped: admin, manager, linguist; project or content-type scoped: admin, linguist.
A bound caller is forced to its own customer. `context set-document` and `context ab`
require admin or linguist. All retain org and customer scoping.
These context operations have no MCP tool (the registry records their API/CLI exemption).
Only `context show-document` and `context delete-profile` are **in-process only**
(`--remote` refuses, exit 2).

| Command | What it does |
| --- | --- |
| `context show [--project ID] [--customer ID] [--content-type ID] [--document ID]` | Show the **resolved** package for a scope - what a run would actually get, after layering. Adding `--document` layers that document's stored context on top. |
| `context show-document --document ID` | Show one document's own stored context package, unlayered. |
| `context set-profile [--customer ID] [--content-type ID] [--project ID] [--name N] [--json JSON] [--domain D] [--content-kind K] [--audience A] [--formality formal\|informal\|neutral] [--tone "friendly,concise"] [--notes T] [--char-limit N]` | Create or update a context **card**. Scope it by customer, by content type (with `--customer`, that is the workhorse card), or by project. `--json` sets the whole package at once; the individual flags set single fields. `--tone` is a comma-separated list drawn from a curated vocabulary (friendly, concise, formal, casual, professional, playful, empathetic, authoritative, neutral, enthusiastic, ...) - a word outside it is **ignored with a warning**, never silently accepted. |
| `context delete-profile <profile_id> [--yes]` | Delete a context card (confirms unless `--yes`). |
| `context set-document --document ID [--json JSON] [--domain D] [--content-kind K] [--audience A] [--formality F] [--tone T] [--notes N] [--char-limit N] [--source-title T] [--source-section S] [--labels "faq,accounts"]` | Set one document's own context. `--source-title` / `--source-section` / `--labels` are the harvested-metadata fields a connector fills automatically; setting them here is the manual equivalent. |
| `context ab --document ID [--locale LOC] [--provider P] [--max-segments N] [--yes]` | Measure whether the context is actually helping: translate the same segments with and without the package and print the comparison. It makes paid provider calls (four per segment), so it first prints an estimate and runs only with `--yes` (without it nothing is sent, exit 0, `--json` carries `confirmed: false`); a project over its budget cap is refused. Over `--remote` the dollar figure is priced on the server, but `--yes` is still required. `--provider` pins a real engine - without one a keyless install resolves to the offline mock, which makes the run a structural test only, not a measurement. `--max-segments` defaults to 20 (ceiling 200). |

### Content types - `content-type …`

**Content types** label the kind of content (Marketing, Legal, UI, ...) and are the scope
axis glossaries, style guides, context cards, TM browse and projects all filter on. Every
leaf is **remote-capable**.

| Command | What it does |
| --- | --- |
| `content-type list` | List content types in scope. |
| `content-type create <name> [--description D] [--customer ID]` | Create one. `--customer` scopes it to a customer; omit for a global type offered to everyone. A customer-bound caller is forced to its own customer. |
| `content-type rename <content_type_id> <name>` | Rename. |
| `content-type delete <content_type_id> [--yes]` | Delete (confirms unless `--yes`). **Fails closed** while anything still references the type. |

### Vendors - `vendor …`

**Vendors** are first-class external translation/review vendors (PROPOSAL-external-processing-step
§9 decision 6). A vendor owns a name, a connection (endpoint URL + a Fernet-encrypted
signing secret / auth token, stored exactly like connector credentials and **never
echoed**), its MSA rate card (cost per word by TM-match band), and its customer links -
the fail-closed scope a workflow's `external` step dispatches through (a step may only
select a vendor **linked to the document's customer**). **LOCAL-only** (it manages
credentials + rate cards on the host): every leaf refuses `--remote`.

Gated by the `vendors_enabled` kill switch (default on): with it off every `vendor`
verb refuses cleanly (`vendors are disabled (setting vendors_enabled=0).`), the `/vendors`
pages, `/api/vendors` endpoints and inbound vendor webhooks 404, new dispatch egress is
refused at the send seam, and the scheduler's vendor return-poll / dispatch-retry sweeps
stop - existing vendors and dispatch history are preserved, just unreachable until it is
switched back on.

| Command | What it does |
| --- | --- |
| `vendor add <name> [--description D] [--endpoint-url URL] [--protocol native]` | Create a vendor. Only the name is required; fill the connection + rate card in later. `--protocol` names the transport (`native \| manual`, default `native`). `native` = our signed JSON contract POSTed to the vendor's HTTPS endpoint; `manual` = file-exchange with no wire - delivery is the operator downloading the leg's XLIFF and handing it over, the return comes back via the guarded upload (per-LSP adapters are a later wave). An unknown protocol is refused at write time. |
| `vendor list [--include-archived]` | List vendors (active by default; `--include-archived` also shows retired ones). |
| `vendor show <vendor_id>` | Show a vendor's connection state, transport protocol, rate card, and linked customers. Reports only whether a secret is **set**, never the value. |
| `vendor update <vendor_id> [--name N] [--description D] [--endpoint-url URL] [--protocol native] [--config-json JSON]` | Partial update - only the flags you pass change (an explicit `''` clears the description / endpoint). Moving `--endpoint-url` to another origin (scheme, host or port) clears the stored signing secret and auth token; re-enter them with `vendor set-secret`. `--protocol` sets the transport (`native \| manual`; unknown values refused). Flipping the protocol only affects FUTURE dispatches - every already-dispatched leg keeps the protocol it was sent under (snapshotted on the leg). `--config-json` sets non-secret transport configuration; a blank value clears it. Headers are sanitized at dispatch: Host, authentication and reserved `x-locstudio-*` headers cannot be overridden. |
| `vendor set-secret <vendor_id> --secret K[=V] …` | Set/merge a credential (`signing_secret` / `auth_token`). Preferred forms keep the value off argv: `--secret signing_secret` prompts (getpass), `--secret signing_secret=-` reads stdin; `--secret signing_secret=` clears that field. Never echoed. |
| `vendor link-customer <vendor_id> <customer_id>` | Link a vendor to a customer (idempotent). **Unrestricted across orgs** - any active vendor install-wide is a valid link target, full many-to-many; the linking authority is the **customer's** org (buyer consent). Single-org installs are fully unrestricted. |
| `vendor unlink-customer <vendor_id> <customer_id>` | Remove a vendor↔customer link. Either party may sever it: an admin of the **customer's** org (buyer) or of the **vendor's** org. |
| `vendor rate-card set <vendor_id> --band BAND --rate FLOAT` | Set one band's cost per word, **merged** onto the existing card (the other bands are untouched). Bands: `repetitions`, `context`, `exact`, `fuzzy_high`, `fuzzy_mid`, `fuzzy_low`, `new`. |
| `vendor rate-card show <vendor_id>` | Show the vendor's MSA rate card (per-band rates + currency). |
| `vendor archive <vendor_id>` | Archive (retire) a vendor - drops it from every picker without breaking dispatch history. |
| `vendor unarchive <vendor_id>` | Unarchive (reactivate) a vendor. |
| `vendor delete <vendor_id> [--yes]` | Delete a vendor (confirms unless `--yes`). **Fails closed** while a workflow step selects it or a dispatch references it; never cascades. |
| `vendor dispatches [--vendor-id ID] [--status open\|all\|dispatched\|retry_wait\|returned\|escalated\|dead_letter\|cancelled]` | List dispatch legs - what is out with a vendor, how many ids are still outstanding, the endpoint **host** (never the URL or the claim token), and estimate vs. actual cost. `retry_wait` = a transient outbound failure parked for a backoff retry; `dead_letter` = the retry window exhausted and the leg escalated to a human. |
| `vendor dispatch-show <dispatch_id> [--xliff-out FILE]` | Show ONE dispatch leg: status (incl. `retry_wait`/`dead_letter` detail), segment/word counts, returned/outstanding progress, estimate vs. actual cost, sanitized last error, and the **TM-leverage band breakdown** (band / segments / words / cost / covered-vs-billable). Host only, never the claim token. `--xliff-out FILE` also writes the leg's **XLIFF 1.2** rendition (`-` = stdout) to hand to a CAT tool (OmegaT/Weblate/Trados). `--remote`-refused (LOCAL-only group); mirrors `GET /api/vendors/dispatches/{id}`. |
| `vendor return --file PATH\|- [--claim TOKEN] [--dispatch-id ID]` | **Manual re-upload** of a vendor's return contract - **JSON or an XLIFF 1.2 file** (the always-available fallback). Matched by `claim_token` (from a JSON file or `--claim`; an XLIFF file has no token slot, so pass `--claim`); `--dispatch-id` pins which leg the file may belong to. An XLIFF body is transport-decoded into the same `{id, target}` contract; same validation as the signed callback: id accounting first, then every string through the guarded write seam. An **XLIFF return is whole-file** (every trans-unit present; empty targets are blank-lane no-ops) so the one file COMPLETES the leg - unlike a JSON return, which may be a partial tranche. Placeholder tokens must be literal text; inline XLIFF tags in a `<target>` and any `<!DOCTYPE>`/DTD are refused. Resumes the run synchronously when the return completes the leg. |
| `vendor poll [--vendor-id ID] [--dispatch-id ID] [--force]` | **Poll** vendors for ready returns. Only vendors carrying a `config.poll_url` are polled (an explicit per-vendor opt-in); outbound through the same SSRF guard + https allowlist as dispatch. `--force` ignores the per-vendor throttle window. |
| `vendor redrive --dispatch-id ID` | **Recovery**: re-drive a leg that RETURNED but whose run never resumed (the return's own resume threw and was swallowed, so the run is wedged PAUSED). The content is already committed; this just continues the parked run past the external step, re-committing nothing. |
| `vendor quote --dispatch-id ID --amount N [--currency USD] [--note ...]` | **Wave E R3 quote gate**: record the vendor/operator **quote** (`quoted_cost_usd`) on a QUOTED leg. Moves no money and does not advance the leg - approval is the separate spend decision. The estimate → **quote** → actual reconciliation chain sits alongside the scoping estimate (never overwriting it). Mirrors `POST /api/vendors/dispatches/{id}/quote`. |
| `vendor approve-quote --dispatch-id ID [--amount N] [--currency USD] [--note ...]` | **Wave E R3**: **APPROVE** the quote (a spend decision). Flips the leg `QUOTED → DISPATCHED` so work proceeds and a return may be admitted; restarts the wait clock; best-effort notifies the vendor. `--amount` records + approves in one step. Mirrors `POST /api/vendors/dispatches/{id}/approve-quote` (MCP: two-phase confirm). |
| `vendor decline-quote --dispatch-id ID [--reason ...]` | **Wave E R3**: **DECLINE** the quote - escalates the parked run to a human_review pause (decision 3), reusing the existing escalate-to-human transition. Mirrors `POST /api/vendors/dispatches/{id}/decline-quote`. |
| `vendor reject --dispatch-id ID [--note ...]` | **Wave E R4 reject/rework**: reject a delivered (RETURNED) leg and **re-dispatch a rework** to the SAME vendor with structured feedback (which segment ids failed which guard lanes + your note), a NEW claim token, and the same package rendition. Caps rework rounds (`vendor_rework_max_rounds`, default 1) then escalates to a human. The rework return funnels through the ONE guarded admission seam - this injects no content. Mirrors `POST /api/vendors/dispatches/{id}/reject-rework` (MCP: two-phase confirm). |

An `external` **workflow step** dispatches its packaged text to a linked vendor. Attach one
with `workflow add-step <wf> --type external --vendor <vendor_id> [--external-wait-hours N]`
(and edit it in place with `workflow edit-step`); `--vendor` / `--external-wait-hours` are
the step's only live config and are refused on non-external step types.

**The guarded return.** A dispatched leg parks the run and waits. A return may arrive three
ways - the **signed webhook callback** (`POST /webhooks/vendor-returns`, HMAC-SHA256 over the
raw body with the vendor's `signing_secret`, the same shared secret the outbound dispatch is
signed with, plus the claim token in `X-LocStudio-Claim`), an outbound **poll**, or the
**manual re-upload** above. The transport decides only how bytes arrive; it never decides how
content is judged. Every return is held to the same contract: each dispatched segment id must
come back **exactly once**, in the right locale, with no extras and no unknown ids
(cumulative across partial returns - one bad id fails the *whole* return, never a partial
admit). Admitted text goes through `quality.commit_mt_output` and nothing else, with every
guard lane on (refusal, language, inline-tag/placeholder integrity, glossary/DNT, number,
length). A conversational **refusal** or a **hard TM lock** WITHHOLDS the text (nothing is
stored; the segment is routed to `needs_review`); the softer lanes
(glossary/DNT/language/number/length/tag) STORE the text and then flag it `needs_review`. A returned
target that just **echoes the source** for a non-English locale is treated as *not
translated* and never overwrites the existing translation. A **partial** return admits what
it covers and leaves the leg parked; the completing return closes the leg, records the
vendor's `actual_cost_usd` against the estimate, and resumes the run. A leg that was
cancelled, escalated on expiry, or whose run has finished **drops** a late return loudly.

**Transport protocol + dead-letter/backoff (Wave C).** Every vendor has a `protocol` (the
transport a dispatch/return travels over; `native` is our own signed JSON contract and the
only one today - per-LSP adapters are a later wave, but the seam is in place and an inbound
return of *any* protocol still funnels through the one guarded write seam above). On the
**outbound** side there are two failure classes. A **policy refusal** - no resolvable/valid
vendor, an SSRF-guard or non-https refusal, a mock-egress block - is deterministic and
**fails the run** immediately, exactly as before (a retry cannot help). A **transient**
transport failure - a network error, timeout, or non-2xx from the vendor - instead parks the
leg in **`retry_wait`**: the run pauses exactly like a delivered dispatch, and the scheduler
re-sends the exact package on an exponential backoff (1m → 5m → 15m → 1h → 6h). If the window
exhausts, the leg becomes **`dead_letter`** and is escalated to a human review pause - vendor
unreachability is **never** hard-failed; a person decides. A sanitized `last_error` (no URLs
or secrets) is kept on the leg for the surfaces. `vendor dispatches --status retry_wait` /
`dead_letter` list each class.

**AI vs Human spend.** Model spend (AI) and vendor post-edit spend (Human) are tracked as
separate ledgers and surfaced together: the run page and the prompt-tap envelope show
**AI + Human = total** (Human labelled `estimate` until the vendor reconciles an `actual`),
and the `report run cost --group-by workflow` output gains an additive **`human_cost_usd`**
column. The TM-leverage **band breakdown** behind the Human estimate is on the vendor detail
page, the run-view leg panel, and `GET /api/vendors/dispatches/{id}`.

### Images - `image …`

Image localization (Beta) from the CLI: upload an image, review its OCR-detected
text regions, translate them (each region becomes an ordinary `Segment`, so TM /
glossary / refusal / workflows apply unchanged), re-render the localized image, and
export it. Gated by the `image_localization_enabled` kill switch (default on). `image upload` and `image translate`
are **remote-capable**. `image list`, `image show`, `image render` and `image export`
are **in-process only** (read/write the DB + upload dir on the host; `--remote`
refuses, exit 2).

| Command | What it does |
|---------|--------------|
| `image upload <project_id> <file>` | Upload an image (`svg` / `png` / `jpg` / `jpeg` / `tif` / `tiff` / `bmp` / `gif` / `webp`) into a project as an image document; detects text regions. An animated GIF or WebP, or a multi-page TIFF, is refused (the overlay build draws on one frame). A connected project refuses uploads (exit 2). |
| `image list <project_id>` | List a project's image documents. |
| `image show <doc_id> [--locale LOC]` | Show an image's regions, translations, per-region confidence, and overflow flags (defaults to the first locale). |
| `image translate <doc_id> --locale LOC` | Translate an image's regions into a target locale (synchronous). If any region fails the run ends FAILED and the command exits non-zero; regions that did translate stay saved, so run it again to fill only the missing ones. |
| `image render <doc_id> --locale LOC [-o PATH] [--force]` | Re-render the localized image (overlay-mode typesetting) and report per-region fit flags; writes nothing unless `-o` is given. `--out PATH` is the long form of `-o PATH`. |
| `image export <doc_id> --locale LOC [-o PATH] [--force]` | Export the localized image file (default path `<stem>.<locale>.<fmt>` in the CWD). `--out PATH` is the long form of `-o PATH`. |

```bash
python -m app.cli image upload 3 ./banner.png
python -m app.cli image show 12 --locale es-ES
python -m app.cli image translate 12 --locale es-ES
python -m app.cli image render 12 --locale es-ES
python -m app.cli image export 12 --locale es-ES -o banner.es-ES.png
```

### Keys - `keys …`

Key-based projects (Beta): the continuous-localization project type, where the unit
of work is a **string key** rather than an uploaded file and the deliverable is one
key/value file per locale. Values are ordinary `Segment` rows under system-managed
internal documents, so TM, glossary, LQA, the export tag guard and spend
safety all apply unchanged. Gated by the `key_projects_enabled` kill switch (default
on).

`--remote` capable, **except the three TM-only delivery empty-gate verbs** -
`empties`, `deliver-approve`, `deliver-revoke` - which are in-process only
(`require_local`; no `--remote` arm exists for them despite their `/api` route
twins). A remote token needs the **`keys` scope** (`apikey issue --user
you@example.com --scopes keys`), which is separate from `docs` on purpose: a
sync consumer has no business uploading documents, and vice versa.

| Command | What it does |
|---------|--------------|
| `keys add --project ID --name NAME [--namespace NS] [--context C] [--description D] [--base TEXT] [--plural] [--tag T] [--char-limit N]` | Create a string key, optionally with its source-locale value. `::` separates nested JSON path segments. On a `--plural` key `--base` fills the **`other` form only** - the command prints (and `--json` carries, as `hint`) which CLDR forms still need their own source text, set with `keys set --form`. |
| `keys list --project ID [--namespace NS] [--tag T] [--search Q] [--status S] [--untranslated-in LOC] [--archived] [--include-deleted] [--limit N] [--cursor N]` | List keys with their per-locale values, using the web grid's filters. `--include-deleted` matches the grid's "include deleted" box - the way to find a key to `keys restore`. `--json` carries `next_cursor` for paging. A value past its key's `--char-limit` is marked with a trailing `!` on its cell, with a footer naming the affected keys; `--json` carries the same fact per value as `over_limit: {length, limit}`. |
| `keys set --project ID --name NAME --locale LOC --text TEXT [--namespace NS] [--context C] [--form CAT]` | Set one locale's value. Passing the project's **source** locale sets the BASE value (which triggers auto-unverify on the other locales). `--form` picks a CLDR plural form. On a plural key a base edit writes **only the form you named** - the others keep whatever source text they have, including none. |
| `keys delete --project ID --name NAME [--namespace NS] [--context C] [--yes]` | Soft-delete a key. It leaves the grid and every export, sync consumers receive a tombstone, and future translate runs exclude it - but its translations are kept and it can be restored. |
| `keys restore --project ID --name NAME [--namespace NS] [--context C]` | Undo a soft delete. The key and every value it kept are re-announced on the change feed, so a consumer that had already dropped it gets it back in full. Find deletable candidates with `keys list --include-deleted`. |
| `keys edit --project ID --name NAME [--namespace NS] [--context C] [--rename NEW] [--move-namespace NS] [--set-context C] [--description D] [--tags "a,b"] [--char-limit N] [--archive\|--unarchive] [--hide\|--unhide] [--plural\|--single] [--plural-arg NAME] [--force]` | Edit a key's metadata, its identity (name / namespace / context) and its plural setting. **All-or-nothing for every deterministic refusal**: the whole edit is validated before the first write, so a colliding rename never lands a half-applied change. A demotion that would discard translated plural forms is refused unless you pass `--force`. |
| `keys import --project ID --file PATH [--locale LOC] [--format FMT] [--namespace NS] [--cleanup] [--yes]` | Import a JSON (flat or nested) or gettext `.po` key file, or a resource file through its filter's key view (Batch 9 keys bridge): `properties`, `strings`, `ini`, `resjson`, `yaml`, and every resource format that adds a key view later (discovered from the filter registry; an unknown `--format` is refused with the full list, locally and over `--remote`). A resource file's comments become key descriptions, its plural forms a plural key, a context (INI section) the key's context; a placeholder-only entry is not imported and stays as it is on export. A base-locale import keeps the file as the export template for that namespace and format. Two entries with the same key, or a key a file of ANOTHER format already defined in the namespace, are refused (`KeyCollisionError`), never merged. Reports per-key errors rather than failing the whole file. `--cleanup` also soft-deletes keys the file does not mention (base-locale imports only, archived keys never swept). `--namespace @from-key` re-imports a whole-project export. A value that merely restates the key's own source text is never written (not over a translation, and not into an empty slot) - every skip is named in `notes[]` and printed. "Restates" means after Unicode normalisation and stripping only: a case-only difference (`Email` for `email`) is a real translation and IS written. |
| `keys export --project ID [--locale LOC] [--format FMT] [--namespace NS] [--empty-as base\|empty\|skip] [--status S] [--archived] [--include-string-images] [-o PATH] [--force]` | Export one locale. A resource `--format` (`properties`, `strings`, `yaml`, ...) needs `--namespace` and writes that namespace's source file with the locale's values filled in, in the file's own encoding (use `-o` for a non-UTF-8 file); a source-locale export with nothing changed is the imported file byte for byte, an entry with no exported value keeps its source text, and keys the file has no place for are reported as `not_in_template` / `not_in_template_keys`. **With `--namespace` you get the BUILD file** (bare key names). **Without it you get the whole project as a TRANSPORT file**: every name qualified `namespace::name` plus a marker, which is what `--namespace @from-key` reads back. The shape follows the flag, never the data. A build file never ships a value whose inline tags are damaged (its source text goes out instead); a transport file carries what is stored, so re-importing it changes nothing. Either way the affected keys are named - as a stderr warning in human mode, and as `substituted` / `substituted_keys` / `kept_damaged` / `kept_damaged_keys` under `--json`. `--empty-as base` fills untranslated slots with the source text; where the value was deliberately WITHHELD (flagged for review with nothing in it) that is reported as `withheld_filled` / `withheld_filled_keys` and marked `#, fuzzy` in `.po`. A value that SHIPS but exceeds its key's `--char-limit` is reported (report-only - it is not withheld or filtered) as `over_char_limit` / `over_char_limit_keys` under `--json`, as a stderr line in human mode, and in the `X-Export-Over-Char-Limit` download header - a signal distinct from `--status`, so a release manager can find "too long to fit" without conflating it with "not reviewed yet". `--status` (`new`\|`mt`\|`edited`\|`needs_review`\|`approved`) filters the export by segment status, **KEY-LEVEL**: a key ships WHOLE if any of its per-locale values carries that status, else it is omitted whole (never a partial key). Omit for the default, unfiltered export. It has no effect on a source-locale (base-value) export, which has no per-locale status. Composes with `--empty-as` and the tag-damage substitution - filtering a value OUT is not the same as substituting it. Every key the filter drops is REPORTED (so a smaller-or-empty file is legible, not mistaken for an empty project) as `filtered_out` / `filtered_out_keys` under `--json`, as a stderr line in human mode, and in the `X-Export-Filtered-Out` download header. `--include-string-images` (Batch 14 Phase 1.5) bundles each exported key's in-context images: the output becomes a **ZIP** (the key file, an `images/` subtree of the ready image blobs, and a `manifest.json` linking key to image path) and so it **requires `-o`** (the bundle is binary). Off by default (byte-identical to today), and ignored (plain file) when the image-context feature is off. Only images on keys that actually ship in this export are bundled, confined to the project, and refused past the `key_image_export_max_files` / `key_image_export_max_bytes` caps. `--out PATH` is the long form of `-o PATH`. |
| `keys changes --project ID [--since N \| --modified-after ISO [--after-id N]] [--cursor-file [PATH]] [--locale LOC] [--limit N]` | The **Delta API**, two mutually-exclusive modes. **Changelog** (`--since N`): everything that changed after a cursor, with **deletions as first-class entries** and metadata-only key edits; `--cursor-file` (default `.locstudio-sync.json`) stores `next_cursor` so a CI job can ask "what changed since last time?", and a cursor older than the retention floor answers `resync_required` instead of a confident, wrong "up to date". `--since` is a whole number (argparse rejects a non-integer); a negative value clamps to 0. **Modified-after** (`--modified-after ISO`): the current-state value rows of keys whose **content or status changed** (source edits count too) after an ISO-8601 **UTC** timestamp (a naive value is assumed UTC), **keyset-paged**. To page, feed the returned `next_modified_after` straight back as `--modified-after` while more remain - while paging it is a self-contained `ISO~id` **cursor token**, so you need not also thread `next_after_id` (the `--after-id` param still works if you prefer it). `--locale` narrows to one target (validated - an unknown locale is refused). This mode **never reports deletions** (a removed key just stops appearing - use `--since` for tombstones) and is an incremental filter, not a bootstrap (`--since 0` bootstraps). A **cleared** value emits with `text:""` (so a consumer retires the string); a never-translated **placeholder** locale is excluded. `--locale`/`--after-id` outside modified-after mode are refused (exit 2). In both modes each value entry carries `text` + `content_hash` (lowercase hex SHA-256 of the UTF-8 value) so a consumer need not fetch each string back. |
| `keys empties --project ID [--locale LOC]` | **The delivery empty gate (Batch 15).** List a key project's untranslated (**empty**) keys for a locale that the gate is holding back from delivery - the actual key names (source shown), not just a count. The three empty shapes are all counted (no value row, an empty target, an `--empty-as skip` omission). A whole-project key export is held until a person approves shipping the empties. **In-process only** (no `--remote` arm). Web/api parity: `GET /projects/{id}/keys/empties`, `GET /api/projects/{id}/keys/empties`. |
| `keys deliver-approve --project ID [--locale LOC]` | **The delivery empty gate (Batch 15).** Approve delivering a held key project's empty values for a locale, audited (`delivery.empty_approved`). The gate is **permission**; the export still renders the empties per `--empty-as` (default `empty`). Covers the empty set the approver saw. **Refused (`400`) while a run is still in flight over the project's key documents** (issue #105), for the same reason as the document twin. **A run is a run in either tier: a classic workflow run (a fan-out's member runs included, and the unfinished fan-out ITSELF only while that locale's current-attempt member is not yet created or is non-terminal; a re-run holds its selected locales from acceptance until their fresh members are terminal, and finished locales not selected still release) or an Advanced-Workflow run over the same project/document and locale.** **In-process only** (no `--remote` arm). Web/api parity: `POST /projects/{id}/keys/deliver-approve`, `POST /api/projects/{id}/keys/deliver-approve`. |
| `keys deliver-revoke --project ID [--locale LOC]` | **The delivery empty gate (Batch 15).** Revoke a prior empty-delivery approval for a key project + locale → re-held (audited `delivery.empty_revoked`). A revoke with no standing approval is a no-op. **In-process only** (no `--remote` arm). Web/api parity: `POST /projects/{id}/keys/deliver-revoke`, `POST /api/projects/{id}/keys/deliver-revoke`. |
| `keys translate --project ID --workflow ID --locale LOC` | Launch translation over a key project's string namespace for one target locale, or `--locale all` to fan out across every target. The headless twin of the key grid's Translate control (`doc upload` / `workflow run` refuse a key project's internal documents by design). Returns launched run ids admitted immediately and queued rows with run ids created in QUEUED status, plus busy/paused/skipped lists. queue_group identifies the durable fan-out; queue_incomplete names members that could not be queued. Mirrors `POST /api/projects/{id}/keys/translate`. The local CLI preserves this admission snapshot while waiting; `waiting` lists any rows still queued after its wait. FULL_TEXT documents can launch on tm_match workflows; skipped_incompatible is reserved and empty. Refused on a non-key (document) project. |
| `keys image add KEY_ID PATH [--caption C]` | Attach a PNG or JPEG in-context image to a string key (max 5 per key), so a vision-capable engine sees it as context when translating that string. The image is downscaled and re-encoded to a send-ready PNG at upload; the first image on a key becomes its primary. **Remote-capable**: `--remote` dispatches to `POST /api/keys/{KEY_ID}/images`. |
| `keys image list KEY_ID` | List a string key's in-context images (id, caption, dimensions, primary flag) - metadata only, never the bytes. Mirrors `GET /api/keys/{KEY_ID}/images`. |
| `keys image rm KEY_ID IMAGE_ID` | Soft-delete one of a key's images; the primary is promoted to the next image if this was it. Mirrors `DELETE /api/keys/{KEY_ID}/images/{IMAGE_ID}`. |
| `keys image set-primary KEY_ID IMAGE_ID` | Make one image the key's primary - the one sent by default at translation time. Mirrors `POST /api/keys/{KEY_ID}/images/{IMAGE_ID}/primary`. |

An import files every key under the NAMESPACE, which defaults to the file stem - so
`./en/common.json` lands in namespace `common`, and every later command naming one of
its keys has to say `--namespace common`. (`--json` is a top-level flag: it goes
BEFORE the command group, not after it.)

**How `--char-limit` is counted.** When a key has a character limit, a translation past
it is committed (never truncated) and flagged for review; the same check runs in the Eval
Lab's `qa_char_limit`. The count is **Unicode code points after NFC normalisation**
(a combining accent and its precomposed form count the same), **characters not bytes**
(ten CJK characters are ten, not thirty), **inclusive** (length equal to the limit is
fine), and **placeholders counted literally** (`{count} items` is 13, not its rendered
width). Note this can differ from a JavaScript `maxLength`, which counts UTF-16 code
units - an astral emoji is **1** here and **2** there. A limit that is zero, negative or
non-integer is treated as no limit.

```bash
# seed a catalogue, translate it, ship it
python -m app.cli keys import --project 7 --file ./en/common.json
# keys list --namespace "" selects only the root namespace; omit --namespace for all.
python -m app.cli keys list --project 7 --untranslated-in de-DE
python -m app.cli keys set --project 7 --namespace common --name error::generic --locale de-DE --text "Etwas ist schiefgelaufen."
python -m app.cli keys export --project 7 --locale de-DE --namespace common -o de/common.json

# the CI sync loop: only what changed, tombstones included
python -m app.cli --json keys changes --project 7 --cursor-file
```

### Inline-tag integrity - `tagging …`

Batch re-verification of inline-formatting **tag integrity** (AI Tagging Phase 1).
The DOCX/XLIFF/HTML filters replace mid-paragraph run formatting / inline elements
with opaque `⟦rN⟧ ⟦gN⟧ ⟦xN⟧` tokens; a translation that drops, reorders, duplicates,
or crosses those tokens silently collapses formatting at export. Fresh translations
are protected at the write seam (`quality.commit_mt_output`), but segments translated
**before** this protection existed are not re-checked until re-translated or re-saved.
`tagging verify` is the one-shot sweep that runs the SAME deterministic verifier over
existing stored targets and **flags** the damaged ones for review - it never
auto-repairs or rewrites text (the reviewer decides). The web surface for the results
is the ordinary needs-review queue / editor `tag_integrity` chips. In-process only.

| Command | What it does |
|---------|--------------|
| `tagging verify [--project ID] [--doc ID] [--locale LOC] [--apply]` | Scan translated segments for broken inline-formatting tokens (drop / add / duplicate / reorder / crossed nesting / residual masking sentinel). **Dry-run by default** - reports the damaged segments and changes nothing (safe on the real DB). With `--apply`, persists a `tag_integrity` qa_flag and routes each damaged segment to `NEEDS_REVIEW`, mirroring the live write gate. Optional filters narrow the sweep to one project / document / target locale. `--json` emits `{scanned, damaged, applied, segments[]}`. |
| `tagging repair [--project ID] [--doc ID] [--segment ID] [--locale LOC] [--limit N] [--apply] [--yes]` | **AI repair** of damaged segments (AI Tagging 1.5c): each one goes to the LLM with an explicit per-token brief (*"`⟦r1⟧` is missing; in the source it wraps 'the quarterly report'"*), and the returned candidate must clear three locks - same token multiset/order/nesting, **body preservation** (words unchanged; a candidate that retranslates anything is discarded), and an **independent placement-verification call**. **Dry-run by default**: nothing is written. Without `--yes` the command only **previews**: it lists the damaged segments and the cost estimate and calls no provider (exit 0, `--json` carries `confirmed: false`). With `--yes` the provider calls run (that is how a repair is known to be possible), still without writing unless `--apply` is given; a project over its budget cap is refused. With `--apply`, a *placement-verified* repair is written and its `tag_integrity` flag cleared (returning the segment to `MT` only if no other qa_flag remains); an *unverified* repair is written but stays `NEEDS_REVIEW` for a human to confirm placement. `--limit` (default 200) bounds provider spend per sweep and the report says when it truncated. In-process only. `--json` emits `{scanned, attempted, repaired, verified, cost_usd, applied, truncated, limit, segments[]}` (`--limit` bounds *attempts*, not successes, so an unfixable backlog cannot spend without bound). Web twin: the editor's repair action (`POST /segments/{id}/tag-repair`). |

```bash
# preview which existing targets have broken inline tags (writes nothing):
python -m app.cli tagging verify --project 3
# then flag the damaged ones for review across one document:
python -m app.cli tagging verify --doc 12 --apply
# see what an AI repair WOULD do to those segments (no writes):
python -m app.cli tagging repair --doc 12
# then persist the repairs it can certify:
python -m app.cli tagging repair --doc 12 --apply
```

The mask/restore layer that keeps the model from mangling tokens mid-generation is
governed by the `ai_tagging_enabled` setting (default on - `config set ai_tagging_enabled 0`
turns off masking; the verification gate above is **always on** regardless).

**The AI repair rung ships DISABLED (`tag_llm_repair_enabled=0`).** Two independent
reviews on 2026-07-25 returned FIX-FIRST with three CRITICAL findings that would ship
silent corruption into TM and to live connectors. `tagging repair` and the editor's
"fix tags" button both refuse while it is off, and the underlying seam refuses too. **Do
not enable it until those findings are closed and re-reviewed.** The inline-tag
*verification gate* and the prompt-hygiene fixes shipped alongside it are unaffected and
remain always-on.

The rung's switches are independent of `ai_tagging_enabled` (masking off makes damage *more* likely, so repair must stay
available): `tag_llm_repair_enabled` turns the whole rung off - inline and on-demand -
restoring the pre-1.5c behavior (deterministic repair, then quarantine) and spending
nothing; `tag_llm_repair_verify_enabled` turns off only the second, placement-verifying
call, which halves the spend but means **no** repair can ever commit clean (every one
lands flagged for human confirmation). Neither switch can disable the verification gate.

`tag_llm_repair_verify_votes` (1–5, default 2) is how many independent verdicts must
**all** agree before a repair commits clean; a rejection short-circuits the rest.
**Verification quality is engine-bound, and the gap is large** - measured 2026-07-25 on
identical probe cases, `claude-haiku-4-5` judged 4/4 correctly and repeatably, while
`meta-llama-3-8b-instruct` approved *deliberately misplaced* tags on 2 of 3 tries. Inside
a workflow the verifier therefore runs on the **LQA step's engine** (your quality engine),
not the translate engine. If your only engine is a small local model, prefer
`tag_llm_repair_verify_enabled=0` so repairs still happen but always land in review -
a weak verifier's "correct" is not worth auto-committing to TM and connectors.

### LQA safety floor (the correction loop's guardrails)

Two deterministic, zero-spend, LLM-free guards run inside the LQA -> fixer correction
loop (`PROPOSAL-lqa-glossary-adjudication` Phase 1). Both default ON and are exact no-ops
on an install with no glossary and no TM hits.

* **Fixer post-check with revert** (`glossary_fix_guard_enabled`, default on). Before a
  step's output replaces a segment's working text, the glossary/DNT violations of the
  **pre-fix** and **post-fix** text are compared. If the edit *introduced* one that was
  not there before, the edit is **discarded**, the pre-fix text kept, and the segment
  carries a `fix_reverted` flag + `NEEDS_REVIEW`. It is a **delta**, never an absolute: a
  pre-existing violation the edit leaves alone is untouched (the write-back gate already
  flags that), and an edit that *fixes* a violation is kept. Whole-document (FULL_TEXT)
  runs **flag but never revert** - one introduced violation must not discard an otherwise
  good pass - so they carry a `fix_term_regression` flag naming the step instead.
  `config set glossary_fix_guard_enabled 0` restores the previous behaviour exactly.
* **TM provenance guard** (`tm_fix_protection_enabled`, default on). Segments filled by a
  100% TM match are dropped from a **correction-loop** step's pending set, so a fix step
  can no longer rewrite an approved byte-exact match and have the run report it as fresh
  MT. Per-step override: `workflow add-step/edit-step --tm-protect auto|on|off`.

Both are visible where the work happened: the reverted / protected counts land on the
run trace (`workflow show-run --visits`, the web run timeline, and the live `step` event's
`lqa_safety` block), and the per-segment flags show as editor chips and in
`doc show --detail`'s review summary (`fix-reverted`, `fix-regression`).

### Glossary-aware LQA judge + adjudication

Two more layers of the same proposal (Phases 2 and 3) make the LQA judge and the fixer
agree about terminology. **The glossary is always the terminology authority; a style
guide carries mechanics only.** Both default ON, and both are exact no-ops on an install
with no glossary - the judge prompt is byte-identical and no finding is touched.

* **Glossary-aware judge** (`lqa_glossary_context_enabled`, default on). The terms the
  run **already matched** for its translate/fix steps are injected into the LQA judge
  prompt as *approved terminology that takes precedence over the style guide*. Only
  matched terms are sent, never the whole termbase. The judge keeps explicit licence to
  disagree: an approved term used in the **wrong sense, wrong part of speech, or
  grammatically mis-integrated** is reported under the dedicated dimension
  `Terminology/Approved-term-misuse`, which is gate-failing and is **never** adjudicated
  away. `config set lqa_glossary_context_enabled 0` restores the previous judge prompt
  byte-for-byte.
* **Deterministic adjudication** (`lqa_adjudication_enabled`, default on). Prompts
  persuade, gates enforce: before the score is computed, a finding whose reported span
  targets a **legitimately applied** approved term is **downgraded to `neutral`** -
  penalty 0, so it cannot fail the gate and cannot trigger a fix loop over correct
  terminology. It is never deleted. Fail-safes point at keeping the human: no span, a
  span the target does not contain, a term the model failed to apply, a different sense,
  or an `Accuracy` finding whose span is wider than the term itself all leave the finding
  exactly as the judge reported it. The span-to-term match is **whole-token**, the same
  matcher the write-back glossary gate uses and honouring the entry's case rule, so a
  span that merely *starts with* the term (an inflected form, or an unrelated longer
  word) is not adjudicated either - losing a suppression is always preferred to losing a
  finding. The span must also be **about** the term, not merely contain it: on a
  `Terminology`/`Style` finding the approved term has to carry **at least half the span's
  words** (or be the whole span), so a finding about an entire clause - *"Envie el correo
  AHORA POR FAVOR!!!"* flagged for shouting capitals - is never silenced by one approved
  term buried inside it. A span with **no whitespace at all** - every Chinese/Japanese/Thai
  span, and any hyphen-glued one - is measured in **characters** at the same one-half
  fraction, so the rule is not a no-op for CJK glossaries. `config set
  lqa_adjudication_enabled 0` restores the previous score, gate and findings block exactly.

Nothing vanishes. An adjudicated finding stays in the scorecard, in the persisted LQA
rows (at `Neutral`, with an `[adjudicated: ...]` note naming the rule, the term and the
original severity) and in the reports rollup; the LQA step's run summary and the live
`step` event carry the count (`workflow show-run`, `workflow run --json`). It is withheld
from exactly one place - the **fixer's** findings block - and even there the block states
`(N finding(s) adjudicated as approved-glossary usage and withheld)`, so a fix step is
never quietly handed a partial picture.

## Private egress exceptions

`egress_private_allowlist` is host-only and read-only on remote surfaces.
Use `loc-tms config show` to read the configured value, source and validity.
The `--remote` variant requires all existing remote-admin locks.
Entries are exact hostnames or IPv4/IPv6 CIDRs. The cloud metadata endpoint, loopback,
link-local and unspecified/multicast/reserved ranges are always blocked and can never be
allowlisted. Every approved private egress writes one `egress.private_target` audit row
(hostname, resolved_address, matched_entry, subsystem, attempt_count) and honors
`audit_enabled`; a corporate `HTTPS_PROXY` still routes it.
See the [canonical private egress policy](architecture/secrets-and-crypto.md#private-egress-exceptions)
for the complete rules.

## Release identity and recovery checkpoints

| Command | Behavior |
|---|---|
| `version [--json]` | Baked RELEASE identity, with pyproject version fallback; also over `--remote`. `--json` prints the release identity as a JSON object. |
| `release manifest --json` | Tag/commit, image digest if known, enabled extras, locked dependency inventory with installed versions, and Alembic head. Same payload as docs-scoped `GET /api/release` and read MCP `release_manifest`. |
| `db backup-full <path> [--force]` | Alias for `db backup --full <path>`, host-only. Verify with `db verify-backup <path>` before relying on the checkpoint. `--force` permits overwriting the destination. |

`/health` shows the coarse major.minor version, or `unknown` when the release
string does not parse; the full release is on `/api/release` (docs scope),
`loc-tms version` and `release manifest`. A build records git describe and the full
commit via RELEASE and RELEASE_COMMIT build arguments. No runtime git or network
lookup occurs. Optional profile detection means matching installed lock pins;
it does not mean the corresponding feature toggle is on. Deployment may supply
LOC_TMS_IMAGE_DIGEST; unknown digest is null.

Postgres verification uses pg_restore --list and requires a nonempty schema/data
TOC. A recorded dump size that disagrees with the archived member fails; a recorded
pg_dump version that is absent, malformed or disagrees with the dump's own TOC is
advisory (a dump_metadata warn, verdict UNVERIFIED) and does not block a restore.
Missing pg_restore returns UNVERIFIED (warn, nonzero exit) in both human and JSON
summaries. A TOC check is
not a recovery drill. Restore extracts the dump and restores ancillary state;
complete the credential-free pg_restore hint using PGPASSWORD or ~/.pgpass.
See [DEPLOY](DEPLOY.md#upgrade) for the pre-upgrade checkpoint and schema recovery.

### Guarded envelope controls

The full semantics (the two capture tiers, honest raw-off / FROZEN age-out, the audit
posture, purge staging and its hard-kill reconciliation) live in the owning doc,
`docs/architecture/connectors.md`. The CLI surface:

| Command | Behavior |
|---|---|
| `ledger status` | Read envelope state and retained raw-file counts/date. Host-only. |
| `ledger envelopes off --confirm "disable envelope recording"` | Exact typed phrase; optional --reason; audited disable. |
| `ledger envelopes on [--reason TEXT] [--confirm TEXT]` | One-step audited enable. `--reason` is recorded in the audit; `--confirm` is accepted but no phrase is required when enabling. |
| `ledger purge --before 2020-01-01 --confirm "purge envelopes before 2020-01-01"` | Dry-run counts; date must match exactly and be a PAST UTC date. |
| `ledger purge --before 2020-01-01 --confirm "purge envelopes before 2020-01-01" --apply` | Delete terminal envelopes finished strictly before UTC cutoff and their per-action raw files, transaction per batch. |

Prefix commands with `loc-tms`. Dry-run is the default; add `--apply` to delete. The web
twin uses the same typed phrases and dated cutoff. MCP exposes read-only `ledger_summary`
status only (no disable or purge tool).

The purge's staged-file reconciler (hard-kill residue settled at boot and on the scheduler
tick) leaves alone any file staged inside a grace window, measured from the staging moment.
That window is `config set ledger_purge_reconcile_grace_seconds <seconds>` (default `600`,
bounded `0`–`86400`). It governs a data-deletion safety guard, so it is **host-only** - settable
only with `config set` on the host, refused over `/api` and absent from the web Settings page -
exactly like the envelope ceremony's own `prompt_tap_envelopes_enabled`.

### Host memory settings

`loc-tms config list --group capacity` shows effective restart-only environment
settings locally. They cannot be changed with `config set` or `--remote`.

| Environment variable | Default | Purpose |
|---|---:|---|
| `LOC_TMS_ANYIO_WORKERS` | 16 | Anyio sync-route worker limiter tokens; streams and long waits are off it. |
| `LOC_TMS_STREAM_WORKERS` | 32 | Dedicated stream and long-wait workers; one token per open SSE tab or waiting caller. Admission is bounded by LOC_TMS_STREAM_WAIT_S. The same pool runs long blocking request work off the event loop (glossary and TM imports, glossary extraction, workflow drafting, image translation, POST /api/jobs/run, SCIM writes, SAML sign-in); when no worker comes free in time, that request gets a retryable 503 with Retry-After and nothing is changed. Also caps an independent process-wide pool of provider stream pumps (32 by default); each pump queues at most LOC_TMS_RUN_WORKERS chunks (default 4) and fails with ProviderStalled after PROVIDER_TIMEOUT_S of consumer backpressure. |
| `LOC_TMS_STREAM_WAIT_S` | 5 | LOC_TMS_STREAM_WAIT_S defaults to 5 seconds and bounds stream-token admission. Saturated wait=true returns the non-waiting response with current status, waited: false and retry_after; poll the run or batch status. Saturated SSE returns HTTP 503, code stream_capacity and Retry-After, before response headers start. The shared refusal is: Stream capacity is busy. Retry shortly; existing work continues. CLI and MCP relay that message verbatim. Size LOC_TMS_STREAM_WORKERS above the expected watcher count: one token per open SSE tab or waiting caller, held up to 900 seconds per silent SSE next() or 3600 seconds per run wait, including disconnected non-abandonable workers. |
| `LOC_TMS_RUN_WORKERS` | 4 | Maximum MT child workers per executor. |
| `LOC_TMS_JOB_WORKERS` | 2 | Shared background job consumers; the global cap that an engine's `max_parallel_jobs` can only lower. |
| `LOC_TMS_MEMORY_BUDGET_MB` | unset | Optional MiB sizing refusal at boot; 0 disables. |

See [memory sizing](architecture/configuration.md#memory-sizing-109-round-1) for the
explicit arithmetic, measured mock workload, install profiles and remaining exclusions.

### Glossary entry paging

`glossary list <project_id> --limit 100 --cursor ID` pages the legacy project
termbase in entry-id order (default and cap 1000). The existing `--offset` and
`truncated` contract is unchanged; offset skips rows after cursor in SQL.
`glossary module show <glossary_id> --limit 100 --cursor ID` pages module terms;
omitting all paging flags returns at most 1000 terms. Both commands also accept
`--offset N`, work locally and with `--remote`, and return `next_cursor` in paged
JSON output (`null` at the end). Follow it with `--cursor`, resetting offset to 0.
Human output prints continuation advice when there are more terms.

The equivalent API queries are `GET /api/projects/{pid}/glossary` and
`GET /api/glossaries/{gid}` with `limit`, `offset`, and `cursor`. The module endpoint
defaults to 1000 terms; both glossary routes reject limit above 1000 with a typed
400. Malformed or non-positive cursor values return 400 (cursor must be a positive
integer); an empty cursor starts the first page. Clients needing every term must
follow `next_cursor` or use an export. MCP `list_assets(view="glossary")`
forwards these same optional parameters. The web entry tables show 200 terms and a
Next glossary terms link via `glossary_cursor`. Exports always include all terms.

### Jev QE backend (POC) comparison

`lqa qe-compare` runs both the existing LLM QE judge and the independent hosted
Jev judge for every JSONL pair. This local operator report is the quick adjunct
to the `judge_qe` / `judge_qe_jev` comparison.
Options: `--pairs` reads the JSONL file, `--llm-provider` selects the LLM judge,
`--threshold` sets the gate score, and `--json <out>` writes the comparison report. Every row is judged by an LLM and by hosted Jev, so the command prints an estimate first and runs only with `--yes` (without it nothing is sent, exit 0). `--json <out>` may not be the `--pairs` file, and `--remote` is refused; both exit 2.

```text
loc-tms lqa qe-compare --pairs tests/fixtures/qe_pairs_sample.jsonl --llm-provider mock --threshold 70 --json comparison.json --yes
```

`--pairs` is required. Each line contains string `source`, `target`, `source_locale`
and `target_locale`, with optional boolean `gold_pass`. `--llm-provider` defaults
to the installation provider. `--threshold` defaults to 70 (clamped to 0-100).
`--json <out>` writes rows and summary; the root `--json` flag still controls
stdout JSON. The table shows row, both scores, pass decisions, agreement,
latencies and skip reasons. Summary includes scored counts, agreement and mean
absolute score difference over jointly scored rows, p50/p95 latency (all attempts,
nearest-rank p95), cost totals, and gold accuracy over each judge's scored gold rows.
Empty denominators are null, never fabricated zeros. Reporting exits 0, including
provider skips and malformed rows; inspect `errors` in the report. An unreadable or
missing `--pairs` file exits 2 with a one-line message. In-process only.

An unsure Jev card at or above threshold has a null pass decision; below threshold
it still fails. Undecided rows are excluded from gate agreement and gold accuracy.
The JSON retains `unsure`; the table adds dimension initials when any row is unsure.
Both `qe-compare` and `jev-refine` attach CLI tap metadata to judge and fixer calls:
`source=cli`, the command as `tool` and `step_key`, the pair id (or one-based row)
as `pair`, and the row as `segment_index`. Workflow/run/document ids are null.
`round` is null for comparison and zero-based for refinement, including baseline 0.
Enable raw capture with `prompt-tap on`; `prompt-tap show --provider jev` lists
the Jev calls (a Jev engine's calls are under `--provider engine:<id>`) and root `--json`
includes their metadata.

Set `TYPESAFE_API_KEY` to enable Jev; `TYPESAFE_MODEL` defaults to `jev-latest` and
`TYPESAFE_BASE_URL` to `https://api.typesafe.ai`. The kill switch is
`qe_jev_enabled=0`; the confidence floor is `qe_jev_confidence_floor=0.6`.
No key means a visible not_configured skip. Jev is text-only, hosted, English-primary,
with no error spans or generated prose. Input costs USD 0.042 per million tokens.
The fixture has six clean pairs and six planted errors; gold labels are illustrative,
so a minor style error can disagree with the numeric gate. Use representative gold
before choosing a backend. Workflow selection uses `--qe-gate --qe-provider jev` for this
environment setup, or `--qe-gate --qe-provider engine:<id>` for a Jev engine (hosted `jev`
or `jev_local`, see `engine create`). `qe-compare` and the Lab's `judge_qe_jev` always use
the environment setup; `engine jev-try <id>` scores one segment through a Jev engine.

### Jev-guided refine loop (POC)

`lqa jev-refine` runs a bounded post-edit experiment with a full score transcript.
Options: `--source` and `--target` with `--source-locale` and `--target-locale`,
or `--pairs`; `--provider` chooses the fixer, `--target-score` accepts whole numbers 0-100 and defaults to 70,
`--max-tries` accepts whole numbers 1-25 and defaults to 10 (invalid ranges exit 2), `--cross-check` adds the post-editor LLM's own second opinion on accepted rewrites (not an independent judge),
`--show-prompts` prints prompts, `--yes` confirms the printed cost estimate (up to `--max-tries` fixer and Jev calls per pair; without it nothing is sent and the exit code is 0), and `--json <out>` writes the report (it may not be the `--pairs` file; `--remote` is refused, exit 2). `--judge`
picks the scorer: `engine:<id>` of a `jev` or `jev_local` engine (for example a
SemIf server on this PC) scores every version through that engine, and `jev`, or
leaving it out, keeps the `TYPESAFE_API_KEY` setup. A dead or non-judge `--judge`
exits 2.

```text
loc-tms lqa jev-refine --source "Pay ten euros." --target "Zahlen Sie neun Euro." --source-locale en --target-locale de --provider claude --target-score 70 --max-tries 10 --show-prompts --json refine.json --yes
loc-tms lqa jev-refine --pairs tests/fixtures/qe_pairs_sample.jsonl --cross-check --json refine-pairs.json --yes
```

Use either a single source/target with both locales or `--pairs` in the same JSONL
shape as qe-compare above. `--provider` selects the fixer (installation default
when omitted). Default target is 70; default max-tries is 10 total versions,
including baseline version 0. The CLI accepts max-tries 1-25 and target-score 0-100,
whole numbers only. Invalid ranges and mixing --pairs with --source/--target exit 2.
Direct service calls clamp larger budgets to 25 and record `max_tries_clamped: true`.
Best is the highest Jev score, with a fully confident round preferred on ties,
then the earliest round. An unsure round can win only if no fully confident round
has an equal or higher score. Best and final include their `unsure` lists;
final is the last attempt. Neither is written back to production content tables.

The prompt explains Jev, all four dimensions and level signals, pseudo-counts and
penalties, then includes source, every version and score oldest first, signed
changes and the next-version instruction. A dimension unsure in either round has
a null delta and no sign, such as `accuracy: unsure -> major` or
`accuracy: none -> unsure`. `--show-prompts` prints the exact rendered prompts.
`JEV_REFINE_PROMPT_CAP_CHARS` defaults to 60000; oldest history
translations are omitted first but score lines remain. If these cannot fit,
the attempt fails without sending an oversized prompt.

The table shows rounds, scores, levels, meaning probability, deltas, time and cost,
then best/final translations, stop reason and totals. Pair summaries show pairs,
target reaches, mean versions, mean baseline-to-best gain and total cost.
`--json <out>` saves full rounds (including prompts), summary and errors; root
`--json` produces JSON stdout. Protect reports as you would the source text.

Stops: target_reached (score at least the target, no unsure dimensions,
and no category at major or critical),
max_tries, no_progress,
judge_skipped_twice, fixer_failed, spend_cap, judge_unavailable_at_baseline.
no_progress wins over judge_skipped_twice when both hold.
Without TYPESAFE_API_KEY the baseline says not_configured, no fixer is called,
and the loop stops immediately. Unavailable scores and deltas are null.
Argparse value errors exit 2. Runtime input and report errors, including
missing files and malformed rows print messages and exit 0; inspect report errors.
In-process only; --remote is not supported.

`--cross-check` adds the post-editor LLM's own second opinion on each accepted rewrite,
using the fixer provider. It skips the baseline and any failed rewrite. This roughly doubles
LLM calls, with one extra baseline call; actual cost depends on token usage.
Goodhart caveat: improving Jev's score can exploit its weaknesses. Agreement with
a second judge is evidence for inspection, not a guarantee of better translation.
The experiment installs no workflow step. Only existing spend and optional Prompt
Tap accounting persist; production translations, TM and LQA tables are untouched.

QE judge saves refuse unknown names, missing engines, and non-LLM translation providers. Legacy `jev` and existing judge or LLM engine refs remain valid. Disabled judges save with a warning. Auto-fix saved without a QE gate or with a non-Jev judge warns that it cannot run. A gate that scores no segments adds a run warning naming the skip reason. Auto-fix skips name unsure dimensions; failures name the post-editor error or safety check. Cross-check is the post-editor LLM's own second opinion on accepted rewrites only. Auto-fix rounds: 1-5, other values are refused.

### Reading QE and auto-fix history

Before launch, `workflow run DOC_ID WORKFLOW_ID LOCALE --plan` previews the resolved
workflow, translation/fallback engines, QE judges, auto-fix post-editors and pricing.
It is read-only and makes no provider calls.

`doc show ID --detail` prints a per-segment QE card and each auto-fix round; `--json`
includes the full sanitized records under `locales[locale].segment_details[].qa_flags`.
Both local and `--remote` show scores, categories, unsure dimensions, rationale, stored
capped texts, cross-check scores, stop reason, kept version, fixer engine/model, cost
and skip reason. The API twin is `/api/documents/{id}/segments?detail=segments`; MCP
uses `work_status(document_id=ID, detail=true)` after polling with `job_status`.

`workflow show-run ID` prints per-step-visit `qe_steps`
counters; `--json`, `/api/runs/{id}` and MCP `work_status(run_id=ID)` carry the same
records. QE has scored/passed/failed/undecided/skipped and `skipped_by_reason`;
auto-fix has attempted/fixed/still_failing/skipped, `skipped_by_reason`, rounds_used
and cost_usd. These describe completed visits and survive later segment edits.
Undecided is separate from skipped here; the older SSE tallies are unchanged.
Null means the run predates tracking or has no recorded QE visit.
The web Ledger, CLI `prompt-tap run ID`, `/api/prompt-tap/runs/{id}` and MCP `ledger_runs`
carry call `round`, `purpose`
and step labels for new captures. Legacy captures without this metadata may have null
round/step. Prompt and response bodies remain withheld on the API and MCP.

Refine's best version prefers structurally clean versions over blocked versions,
then compares judge scores and confidence. A blocked baseline cannot win a score tie
against its repaired version.

Jev runs deterministic structural checks BEFORE the judge: identical source/target text
(after collapsing whitespace and folding case; same base languages are exempt), inline tags/markup, placeholders (including double braces,
ICU arguments and printf), and number/time parity. Numbers-only text, URLs, codes and
applicable glossary DNT terms are exempt from the untranslated check. Identical single tokens (including acronyms and brands) are advisory MINOR findings,
with or without a glossary, and do not block a QE pass. Identical multi-word text
and other structural failures are MAJOR and block a pass regardless of score or threshold. The judge still runs;
its findings are retained. Cards expose `structural_findings` (dimension, severity, reason)
and `structural_blocked`; the shared rationale names the failure in web and CLI views.
The existing empty-target fast path still skips the judge and fails as empty.
The `qe_jev_enabled=0` kill switch disables judge calls, not structural checks.
A disabled, unavailable or unconfident judge has no score; a structural block
still routes to review and says 'judge did not score'. Lab records structural
blocks as 0 with the reason, preserving the judge score separately; an unscored
card without a block stays not applicable. Judge provenance is judge-wrappers/2.
Number and time parity keeps the number checker's own grade. A real mismatch (a changed
value such as 5 -> 50, a changed time such as 6:45 -> 7:45, an AM/PM swap) is MAJOR and
blocks. Two forms are advisory: a dot-format time that equals the source time (Finnish
klo 18.45, German 18.45 Uhr, Danish and Norwegian dot times) and a number from 0 to 10
written as a word (2 -> deux) in English, French, German, Spanish, Italian, Portuguese,
Dutch, Swedish, Danish, Norwegian, Finnish, Polish, Russian, Chinese or Japanese. They
are listed in `structural_findings` for review but do not block a QE pass. Every other
number finding is MAJOR and blocks, including a dropped or added number, a wrong dot
time, digits regrouped into different numbers (1-10 -> 110) and a rendering that is
ambiguous without a locale convention. Jev also reads what a number means, and each of
these is MAJOR. A clock time dropped from the target blocks (hour words such as cinq
heures et demie, an hour-only form such as 20 heures, and Chinese or Japanese clock
times are read as the time). A date whose day, month or year changed blocks: the order is
judged for the target locale, so 3/14/2026 -> 14.03.2026 is correct and
3/14/2026 -> 15.03.2026 is not;
a dot date is day first, ISO dates and Chinese, Japanese and Korean written dates are
read the same way, a date written out in words passes when its day and year match, and a
bare en source locale with both numbers 12 or below leaves both orders open. A lost minus sign blocks (a
hyphen-minus, U+2212, an en dash or a parenthesised currency amount is negative, and a
word such as minus or moins stands in for the sign). A swapped or dropped currency
blocks ($, USD, US$ and dollars are one currency; $ -> EUR is not; placement and spacing
per locale do not matter). A minus the target adds is not judged. An AM/PM marker
counts only on hours 1 to 12, and a bare lowercase am after a German or Dutch time is
the word for on the, not a meridiem. In scripts written without word spaces (Chinese,
Japanese, Thai, Lao, Khmer, Myanmar) a run is one token, so identical text is advisory,
only while it is at most 8 characters after trailing punctuation (ASCII or full-width) is
removed and holds no sentence punctuation inside; a longer or internally punctuated run is
multi-word text and a copy of it is MAJOR. So the short word with a closing mark and the
same word without it are treated alike. HTML void elements (br, hr, img, input and the
like) are one tag kind whether written <br>, <br/> or <br />.
Time parity accepts 17 h 30 and 5:30 p. m.; it abstains when either side has no
parseable time. ICU nested argument names are sets across branches. Markup parity
compares element names and opening/closing/self-closing kind, not attribute values.
Meaning promotion runs BEFORE the low-confidence skip: a meaning signal at its floor
lifts accuracy none, minor or unsure to major. Confident higher severity is retained.
Auto-fix targets confident major dimensions even alongside unsure dimensions, which are
left unchanged and flagged for review. A better rewrite can be kept for review with the
baseline unsure dimensions still flagged; it cannot pass. New unsure dimensions prevent
keeping an attempt. API and MCP return the same cards and flags as web and CLI.

### FULL_TEXT TM offers

FULL_TEXT workflows accept tm_match by default. Exact (100%) sentence matches from the step's selected readable TM pools are OFFERED, unverified, beside the glossary; {tm_offers} controls placement. The source body is unchanged. This is an offer, not a guarantee. If a string must be exact, FULL_TEXT is the wrong mode. Offers are WITHHELD from each step that redacts PII, because an offer reproduces the document's own sentence verbatim; the run message says so. The run summary, GET /api/runs/{id}, MCP work_status and workflow show-run expose tm_offers with sentences_examined, matches_offered and message after a FULL_TEXT tm_match step. These describe the latest offer list, not applied translations. SSE tm_match carries the counts for that step visit. A SEGMENTED workflow's tm_match step also leaves a tally on the run: the run page, GET /api/runs/{id} (tm_match), MCP work_status and workflow show-run report filled (genuine TM fills), relocked (already held the exact TM text), unmatched (not taken from the TM at the step's fill threshold, so sent on to the next step) and total segments. Fuzzy suggestions below the fill threshold are not counted.

Every later llm_agent step receives the TM offer block, except steps that redact PII. The tm_match step honors tm_pool_selection within the readable pools; an empty selection offers nothing.
