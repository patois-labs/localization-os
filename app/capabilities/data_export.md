---
module: data_export
title: Data Export
summary: A RBAC-confined, self-describing, checksummed export of a scope's whole object graph - one JSONL file per entity plus a manifest, in a ZIP. It dumps only what you can already see (a self-serve export of your visible projects, or a whole-workspace dump for an admin), never includes a secret, and runs as an async job you start, poll and download.
nouns:
  Scope: "What the export covers, resolved server-side under YOUR access. Four kinds: self (everything you can see), customer (one customer you can see), project (one project you can see), and workspace (the whole caller-org - admins only). A named target you cannot see is a 404, never a foreign dump."
  Export job: "The async unit of work. Starting one returns a run_uuid immediately; the walker streams the graph into a ZIP on the job worker, not inline. Poll the run to a terminal state (ok, partial or failed), then download the artifact."
  Artifact: "The staged ZIP. It carries one <entity>.jsonl per table in scope plus a manifest.json (the schema version, the scope, per-entity row counts, and a sha256 of every member) - self-describing and integrity-checkable offline."
  History depth: "Per-export: current dumps the current state of every entity; full adds the append-only / bi-temporal record (segment edit log, run/step history, key changelog, memory supersession chains, and - for a workspace dump - the audit slice)."
  Manifest: "manifest.json inside the ZIP: manifest_schema (data-export/1), the scope, counts, dropped counts (rows scrubbed or skipped, so the arithmetic is honest), and a per-member + whole-artifact checksum. A consumer verifies integrity from this alone."
  Audit slice: "A verifiable slice of the tamper-evident audit chain (each row with its prev_hash/row_hash), included ONLY in a workspace/admin dump when you ask for it. A self-serve export never contains audit rows or any identity-group table."
verbs:
  Start an export: "Web: the New export form on the Data export page (pick a scope, history depth, and options). CLI: `export run` (defaults to scope self, current history). Narrow it with `export run --scope customer --ref 7` or `export run --scope project --ref 12`. /api: POST /api/exports (docs scope) returns 202 with a run_uuid + a status_url."
  Take a whole-workspace dump: "Admins only. CLI: `export run --scope workspace --history full --audit` (the audit slice + the identity group ride along). Web: the workspace option + the audit toggle appear only for an admin. /api: POST /api/exports with an admin-scoped key; a non-admin asking for workspace is a 404 (non-enumerable), never a 403."
  Watch it finish: "Web: the run page refreshes its status automatically. CLI: `export status 3` (a run id) or `export status` with a run_uuid; over `--remote`, `export run` polls to a terminal state for you. /api: GET /api/exports/{run_uuid} for {status, counts, artifact_bytes}."
  List your exports: "Web: the Your exports table. CLI: `export list`. /api: GET /api/exports - filtered to YOUR OWN runs (actor-first), never another caller's."
  Download the ZIP: "Web: the Download link on a completed run. CLI: `export download <run_uuid> -o dump.zip` (Range-resumable). /api: GET /api/exports/{run_uuid}/artifact - a completed artifact only, owned by you, with a workspace dump additionally requiring the admin scope AT FETCH."
needs:
  - "The data_export_enabled switch on (default ON). It is a writes_off kill switch: with it off, starting a new export is refused at the service layer (naming data_export_enabled), while reads and downloads of already-staged artifacts stay up. It is host-only (REMOTE_IMMUTABLE), so no /api call can flip it."
  - "For the /api POST: a personal API key with the docs scope. A plain api-scoped key is a 403. A whole-workspace dump additionally needs the admin scope (mintable only locally) or, on the web, an admin role - the same control the GDPR export uses; remote_admin_enabled is NOT required for a self-serve export."
  - "Only what the caller can already see. The scope is resolved under your real RBAC (visible projects, the org ring, the customer clamp); an empty allow-list produces a shaped-empty artifact, never an unscoped dump. A customer-bound key can dump only its own customer."
  - "On an install that encrypts data at rest (encrypt_uploads or encrypt_db): either data_export_allow_plaintext_staging=1 to acknowledge that artifacts stage UNENCRYPTED on local disk, or S3 staging configured. Otherwise a new export is refused with that exact message - staged artifacts are plaintext-at-rest by default."
limits:
  - "Confined, always. No row belonging to a customer, project or org you cannot see is ever emitted or named - the walker uses positive-membership predicates, so global/shared (NULL-tenancy) rows are excluded by construction from a self/customer/project export. Any leak would be a HIGH finding; the export is red-teamed for exactly that."
  - "Secret-free, always. Every secret column (engine/connector/vendor secrets, password and key hashes, claim/invite tokens) is dropped, every free-text column is scrubbed before emission, and any user:password@ credentials embedded in a stored URL (a legacy engine base_url, a vendor endpoint, a URL inside engine or connection config) are stripped, keeping the host, port and path. The one exception is the admin-only audit slice, emitted raw so its hash chain stays re-verifiable - and it is reachable only under the workspace admin gate."
  - "The audit slice and the identity group (users, api keys, connections, settings) appear ONLY in a workspace/admin dump. A self-serve export excludes them entirely; the member set is a fixed whitelist, so a new identity file cannot slip into a self-serve artifact unnoticed."
  - "Async and staged, never a synchronous stream. Generation is atomic - a failed job leaves no half-artifact you can download, and only a completed (ok or partial) run is served. A per-entity failure yields a partial run (the gap recorded in dropped counts), never an aborted dump."
  - "Read-only portability, not a backup and not a warehouse feed. It excludes install secrets (unlike the host-only DR backup) and is a point-in-time dump (pair it with the keys change feed for 'what changed since')."
  - "Deletes and erasures reach staged artifacts. Deleting or privacy-erasing a document, deleting a project or a customer, or erasing a user (the exports that user requested) marks every staged export that may hold that content as purged (status purged) and removes its ZIP; a workspace or self export in that org is always included. Every download door re-checks this at download time, so a purged run is a 404 and its ZIP is gone. Start a fresh export afterwards."
  - "No MCP tool and no confirmation-free download tool: a full-graph dump is not an agent task, so every /api/exports route is off-limits to MCP, and the /utcp manual exposes only the run list + one run's status (metadata), never the artifact bytes."
connects:
  - module: documents
    how: "The document pipeline is most of what a dump contains - projects, documents, segments, their TM/glossary/workflow context and (in full history) their edit and run record - walked as one object graph."
  - module: reports
    how: "Reports aggregates the numbers on your runs as grouped tables/CSV; this export dumps the underlying rows themselves, self-describing and checksummed, for portability rather than a summary."
  - module: keys
    how: "The keys Delta API is the incremental complement: bootstrap a consumer with a full-dump export at a point in time, then keep it current with the change feed's since/modified_after cursors."
---

## What this is for

You need a complete, portable, self-describing copy of a scope's data - to hand to a
customer, to load into your own tools, or to archive. This export walks the **whole
object graph** for a scope you can already see and writes it as **one JSONL file per
entity** in a ZIP, alongside a `manifest.json` that names the schema version, the scope,
the per-entity row counts and a checksum of every file. It is the one door for
first-class run history, segment edit history and LQA findings that the per-domain
exporters never gave you.

Two properties make it safe to hand out. It is **confined** - it contains only rows the
caller could already see, resolved under your real access, and it never crosses a
customer, project or org boundary. And it is **secret-free** - every credential column is
dropped and every free-text field is scrubbed before it is written.

## The scopes

- **self** - everything you can see (your visible projects and their customers).
- **customer** - one customer you can see (`--ref <customer_id>`).
- **project** - one project you can see (`--ref <project_id>`).
- **workspace** - the whole caller-org. **Admins only**, and the only scope that includes
  the identity group (users, API keys, connections) and, on request, the tamper-evident
  **audit slice**. A non-admin asking for it gets a 404, not a 403.

A named target you cannot see resolves to **404** - the export never reveals whether an
out-of-scope id exists.

## History depth

Every export chooses `current` or `full`. `current` is the current state of every entity.
`full` adds the append-only and bi-temporal record: the segment post-edit log, run and
step history, the key changelog, memory supersession chains, and - for a workspace dump -
the audit slice with its verifiable hash chain.

## The normal path

1. **Start** one: `export run` (self, current), or narrow it -
   `export run --scope customer --ref 7 --history full`.
2. **Poll** it to a terminal state: `export status 3` (over `--remote` the `export run`
   command polls to completion for you).
3. **Download** the artifact when it is ready: `export download <run_uuid> -o dump.zip`.

Every ZIP carries `manifest.json`, so a third party can verify the artifact's integrity
offline - recompute each member's sha256 and compare.

## When the switch is off

`data_export_enabled = 0` is a **writes_off** kill switch: starting a new export is
refused (the CLI names the switch), but already-staged artifacts stay listable and
downloadable, and no data is touched. It is a host-only setting - no API call can change
it. Flip it back on and new exports resume.
