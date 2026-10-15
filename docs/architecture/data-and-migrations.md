# Data & Migrations: Architecture (L1)

## 1. Role

This subsystem is the **relational core**: the one place the whole data model is declared,
the engine and session helpers every service reaches the database through, the boot-time
schema bring-up, and the write-contention retry law. SQLite is the zero-config default and
the test engine; setting a Postgres URL opts into the other supported backend, with the
SQLite-specific behavior (WAL, at-rest encryption, PRAGMA-based backfill) guarded behind
the dialect so the Postgres path stays clean and Alembic-owned.

`app/models.py` declares the entire schema once, up front, in SQLModel: customers,
projects, documents, segments, string keys, translation memory, glossary and concept
tables, workflows and runs, LQA, jobs, settings, and the audit log. Later chunks use these
tables rather than migrate them.

`app/db.py` owns schema bring-up and the session lifecycle. **Alembic is the schema
authority for every real install**: `app/db.py::init_db` runs `alembic upgrade head`
in-process at boot so every migration (including the non-additive ones a create-all shim
cannot apply) reaches the DB, and it hard-refuses to boot a Postgres install whose schema
is not stamped. The hermetic test engine keeps a fast create-all path, and a nullability
parity guard keeps that path from drifting from the Alembic authority. A legacy pre-Alembic
SQLite DB falls back to the create-all self-heal shim with a loud rebuild warning rather
than crashing.

`app/services/ids.py` is the small, cross-cutting id and name coercion law surfaces route
request-supplied ids and names through, so a malformed id becomes a clean not-found rather
than a 500 deep in the driver (a few surfaces reimplement the same bounds locally instead of
calling it, section 2).

It does **not** own what any table means (those are the product-domain subsystems) nor the
at-rest cipher itself (that is secrets-and-crypto); this layer owns the schema's shape, its
migration authority, and the transaction mechanics.

## 2. Invariants

- **Upload erasure keeps a durable retry identity (#399).** New document uploads
  persist a UUID-based `upload_blob_key` before writing bytes; legacy documents
  retain their `id.format` key. Failed blob deletion records that exact key in
  `BlobDeleteOutbox` in the transaction removing its document or key-image row.
  Every retention scheduler tick retries this ledger, including with retention
  disabled or a zero-day window. These settings only control new age-based
  erasures. Document/project counts and audit records include `file_pending`;
  web and CLI messages report pending upload erasure until storage recovers.
  API responses preserve the pending count. Document/project deletion remains
  deliberately excluded from MCP's tool registry.

- **Alembic is the schema authority on a real install.** `app/db.py::init_db` runs
  `app/db.py::_alembic_upgrade_head` on a fresh or already-stamped SQLite DB and refuses to
  serve an un-migrated Postgres DB. Enforced by `verify_alembic_authority`.
- **Fresh-create and upgrade agree.** The Alembic-head schema has zero column-set and zero
  nullability diffs against the model's create-all, so the test path and the migration path
  cannot drift. Enforced by `verify_migrations`.
- **The test path stays create-all, without an alembic_version table.** `app/db.py::init_db`
  under `config.IS_TEST` uses `app/db.py::_init_sqlite_create_all` so the fast hermetic
  suites keep their path. Enforced by `verify_alembic_authority`.
- **A legacy pre-Alembic DB self-heals rather than crashes.** The create-all path widens a
  column a shim-only DB still holds NOT NULL that the model declares nullable via
  `app/db.py::_ensure_nullability`. Enforced by `verify_nullability_selfheal`.
- **A write under contention retries, then sheds honestly.** `app/db.py::commit_with_retry`
  bounded-retries a locked-database error and `app/db.py::commit_launch` surfaces a
  retryable `app/db.py::WriteContentionError` rather than a 500. Enforced by
  `verify_write_contention`.
- **The org-confinement invariant is boot-asserted on every backend (partly untested).**
  `app/db.py::_assert_org_confinement_invariant` runs at boot against the install's root
  org on both SQLite and Postgres. It has two halves. The reserved-id check (no
  `Organization` may carry `access.BLOCKED_ORG_ID`) runs UNCONDITIONALLY, before the gate
  check, so `init_db` in `verify_migrations` and `verify_alembic_authority` does execute it
  (trivially green on a clean DB). The NULL-org-user half returns early with the multi-org
  gate off, so no verify suite exercises that half. It is deliberately not pinned to a
  dedicated suite.
- **A scoped subject-access export carries only the caller's org.** When
  `app/services/gdpr.py::export_user` gets a `caller_org_id` (the self export and the org
  admin's export under the multi-org gate), every section is confined to that org: through
  the row's own `org_id` where it has one, else through the project, document, connection,
  glossary or query thread it hangs off (`app/services/gdpr.py::_ExportScope`), and a job
  through the rows its payload names (a job naming none is left out). So a user moved
  between orgs never takes the former org's content along. The account row, API keys and
  page layouts are per-user and stay. With no caller org (gate off, or the operator) the
  export is unchanged. Enforced by `verify_multiorg_inv`.
- **Every column that can hold a user identity is erased or allow-listed with a reason.**
  A string or JSON column named email, actor, author, owner, initiator, assignee, labeler
  (and the like) or ending in `_by` must be in `app/services/gdpr_common.py::_ERASE_SWEEP`,
  in `_IDENTITY_SWEPT_ELSEWHERE` (a dedicated block of `erase_user`), or in
  `_IDENTITY_NOT_SWEPT` with the reason it is kept. Every JSON column, whatever its name,
  is likewise swept by content (`_JSON_CONTENT_SWEEP`: job payload and result, the job-queue
  ledger copy, dataset item meta), handled by a dedicated block (customer settings), or
  listed in `_JSON_NO_IDENTITY` with a reason. A new `*_by` or JSON column fails the suite
  until someone decides. The subject-access export matches a person by the exact column
  value, or as a whole address inside text and JSON (case-insensitive, never inside a
  longer address). For a job the subject is only named in, `export_user` emits only
  `id`, `subject`, `relationship: "mentioned"`, and `fields` (the matching column names,
  `payload` and/or `result`). It releases no job content, even from a matching JSON leaf
  or key, regardless of project/customer visibility. Org confinement is checked against
  the original payload before projection. Jobs created by the subject retain their
  existing export shape. Web self/admin exports, CLI and API share this service.
  Customer settings and dataset-label metadata disclose only paths to subject
  occurrences: non-subject keys are anonymized, unrelated branches are dropped,
  and non-email values are redacted. Customer mention records omit customer names.
  Enforced by `verify_gdpr` and `verify_multiorg_inv`.
- **A malformed id resolves to None, never a crash.** `app/services/ids.py::coerce_id`
  rejects booleans, Unicode digits, and out-of-range values without raising, enforced by
  `verify_id_coercion_hardening`. Several module-local `_coerce_id` delegates route to it
  (for example `app/services/connectors/store_common_leaf.py` (re-exported by the connector store),
  `app/services/segmentation_profiles.py`,
  `app/services/project_sources.py` and `app/services/templates.py` each
  `return ids.coerce_id(v)`), so the coverage transits to those surfaces;
  `app/services/customers.py::_coerce_id`, `app/services/context_pkg.py::_coerce_id` and
  `app/services/vendors.py::_coerce_id` are independent reimplementations that never call
  `ids.coerce_id` (context_pkg deliberately preserves its own negative/zero domain). The
  transitive coverage does not reach those reimplementations, but the suite closes two of
  them directly: `verify_id_coercion_hardening` probes `customers._coerce_id` by name
  (including its positive lower bound) and `context_pkg._coerce_id` on both range edges plus
  its deliberate negative/zero domain; `vendors._coerce_id` has no such direct arm.
  `app/services/ids.py::clean_name` refuses
  a non-string or over-long name with `app/services/ids.py::InvalidNameError`, enforced by
  `verify_test_campaign_fixes`.

## 3. Where the work happens

| Concern | Pin | One line |
|---|---|---|
| The whole data model | `app/models.py::Customer` · `app/models.py::Project` · `app/models.py::Document` · `app/models.py::Segment` · `app/models.py::Setting` | The SQLModel tables declared once up front; the settings key/value table lives here too. |
| Schema bring-up | `app/db.py::init_db` | The three SQLite sub-paths plus the Postgres refuse-if-unmigrated authority. |
| Alembic authority | `app/db.py::_alembic_upgrade_head` | Runs `alembic upgrade head` in-process against the install's own DB. |
| The create-all shim | `app/db.py::_init_sqlite_create_all` · `app/db.py::_ensure_columns` · `app/db.py::_ensure_nullability` | The test path and the legacy self-heal fallback: create-all, additive columns, then nullability widening. |
| Session lifecycle | `app/db.py::get_session` · `app/db.py::engine` | The per-request session dependency over the dialect-guarded, explicitly-sized engine (pool dials documented in configuration). |
| Capacity failure | `app/db.py::is_pool_timeout_error` · `app/db.py::POOL_EXHAUSTED_NOTE` · `app/db.py::emergency_session` | Connection-pool exhaustion as its own recognised class, its one shared sentence, and the pool-less session that lets a failure be recorded when the pool has nothing left. |
| Bookkeeping capacity | `app/services/bookkeeping.py::session` and `app/services/bookkeeping.py::scope` | Pooled sessions with headroom, otherwise the unbounded blocking emergency engine; bookkeeping never takes operator request relief permits. |
| Pool-wait budget | `app/db.py::_BudgetedQueuePool` · `app/db.py::begin_pool_budget` · `app/db.py::pool_budget_spent` · `app/db.py::pool_has_no_capacity` | One checkout timeout per request however many queries it runs, the per-request scope the middleware opens, the signal an error renderer reads to skip a database-backed page, and the non-blocking "the pool has nothing left" read the doorman sheds on. |
| Write-contention law | `app/db.py::commit_with_retry` · `app/db.py::commit_launch` · `app/db.py::commit_or_shed` · `app/db.py::WriteContentionError` | Bounded retry, the launch-path retryable error, and the shed-a-row posture. |
| Org-confinement assertion | `app/db.py::_assert_org_confinement_invariant` | The boot-time data-law check on the root org, all backends. |
| Id + name coercion | `app/services/ids.py::coerce_id` · `app/services/ids.py::clean_name` · `app/services/ids.py::InvalidNameError` | The id/name coercion law surfaces route request ids and names through: some call it directly, others through a module-local `_coerce_id`. |
| Data retention | `app/services/retention.py::purge` · `app/services/retention.py::delete_document` · `app/services/retention.py::tick` | Ages uploads, terminal jobs, TM and memory; preserves live jobs, catalogue content and TM matching surviving document sources. |
| Document-derived copies | `app/services/document_erasure.py::scrub_term_examples` · `app/services/document_erasure.py::erase_lab_snapshots` | Clears glossary context and privacy-erases Lab snapshots and derived artifacts in the document deletion transaction. |
| GDPR subject rights | `app/services/gdpr.py::export_user` · `app/services/gdpr_common.py::erase_user` | Art.15/20 export and Art.17 erasure that anonymizes to a per-user tombstone rather than hard-deleting. |

## 4. Data & settings

- TM retention uses `coalesce(activity_at, last_used_at, created_at)` on SegmentTM.
  FullTextTM has no usage timestamp and uses `coalesce(activity_at, created_at)`.
  Creation, import, re-approval, editing and accepted matches refresh `activity_at`;
  `last_used_at` remains translation usage only, confined to the applying project's
  own rows. Legacy NULL activity needs no backfill. Both TM and memory aging commit
  bounded batches and report committed partial counts if a later batch fails.
  Fresh evidence preserves supported beliefs; erased evidence also erases derived
  unsupported beliefs and citing suggestion content through the shared erasure path.
- Document deletion clears extracted candidate context before severing its document
  FK. Accepted examples are matched using the retained candidate's glossary and sanitized
  snippet, including renamed terms, and scrubbed from canonical terms, projected metadata
  and per-pair overrides. Accept/drop decisions and terminology remain; nonmatching
  examples remain. This uses existing provenance and also covers pre-fix accepted candidates
  whose document link still exists (`document_erasure.scrub_term_examples`). Previously
  severed provenance cannot be reconstructed automatically. Value matching can also erase
  an independently authored example with identical text in the same glossary.
- `retention.delete_document` defaults to privacy erasure with `cascade_tm=True`.
  The admin document delete (`content.delete_document`) passes `erase_document_tm=privacy_erasure`
  (#539): an explicit erasure also removes only the TM rows that came from that document
  (matched by its segment source text and locale pair; the rest of the project TM stays), an
  ordinary delete keeps all of it. Erasing a user purges the exports that user requested
  (`purge_artifacts_covering(actors=...)`) plus every workspace and self export of the org.
  A project privacy erasure also purges LLMemory learned from the project
  (`retention.erase_project_memory`, #441); a plain delete keeps it, un-scoped.
  Privacy erasure removes Lab datasets copied from the document, their items, runs,
  outputs, scores and gold labels in the same database transaction, even with Lab disabled.
  `privacy_erasure=True` can be requested independently of shared project TM removal;
  the scheduled retention sweep does this. Operators can select the document page's
  Privacy erasure checkbox, send boolean `privacy_erasure: true` to the delete API, or
  use `doc delete --privacy-erasure`. Ordinary deletion preserves Lab snapshots and
  warns with `lab_datasets_preserved`; the erasure count is `lab_datasets_erased`.
  After ordinary deletion, enabled retention (also `admin retention purge-now --yes`)
  erases orphan snapshots once their creation date is older than the retention cutoff.
  This includes datasets, runs, scores and gold/human labels. With retention disabled,
  operators must delete surviving datasets in Lab. MCP excludes destructive deletion.
  Project deletion (`content_projects.delete_project`, #429) offers the same choice for
  every document in the project: the delete page's Privacy erasure checkbox, boolean
  `privacy_erasure: true` on `POST /api/projects/{pid}/delete`, or
  `project delete --force --privacy-erasure`. Default is off; the result sums `lab_datasets_erased` and
  `lab_datasets_preserved` across the project's documents and adds a `warning` (shown on
  the projects page, API response and CLI result line) when snapshots were preserved.
  When a source ID is reused, snapshots predating the current document are excluded
  from that document's cascade but remain eligible for orphan aging. Moving a document
  between projects does not exclude its snapshots. Uploaded datasets have
  no source provenance and are outside this cascade. Independently exported files and
  backups still require their own disposal policy. Implementation:
  `app/services/retention.py::delete_document`, `app/services/document_erasure.py`;
  regression coverage: `verify_document_erasure`.
- **Models:** `app/models.py` is the single declaration of every table, including
  `app/models.py::Setting` (the key/value store every setting reads and writes),
  `app/models.py::Job` (the background-job row, documented in jobs-and-scheduler), and
  `app/models.py::Organization` (the tenancy root, documented in tenancy-confinement).
- **No settings key selects the backend or the at-rest cipher.** The backend is selected by
  the database URL and the SQLite at-rest cipher by an environment flag
  (`config.DB_ENCRYPTION_ON`, env `LOC_TMS_DB_ENCRYPTION`, documented in secrets-and-crypto
  and configuration), not by a `settings.get` key. Registry keys are still read at boot by
  direct SQL rather than the settings service: `app/db.py::_multi_org_gate_on` reads
  `security_auth_required` and `multi_org_enabled` (their AND gates the NULL-org half of the
  confinement assertion, section 2), and `app/db.py::_fixup_retired_org_name` reads and
  clears a retired `org_name`. The migration corpus lives under `alembic/`.

## 5. Enforced by

- `verify_alembic_authority`: the fresh-vs-create-all diff, the real-install alembic path,
  the legacy fallback, and the untouched test path.
- `verify_migrations`: the nullability parity between the model create-all and the Alembic
  head, and the `_ADDED_COLUMNS` registry guard that keeps the additive-column shim covered
  by a revision.
- `verify_write_contention`: the bounded commit retry and the retryable `WriteContentionError`
  the launch path surfaces instead of a 500.
- `verify_nullability_selfheal`: the create-all self-heal that widens a legacy NOT NULL
  column the model declares nullable.
- `verify_id_coercion_hardening`: the `coerce_id` bounds, plus the module-local `_coerce_id`
  coercers it probes directly (`customers`, `context_pkg`, `store`, `segmentation_profiles`,
  `templates`).
- `verify_test_campaign_fixes`: the `clean_name` refusal of a non-string or over-long name.

## 6. Battle scars

- **Migrations beyond ADD COLUMN silently skipped any pre-rebuild install.** The real DB
  never ran under Alembic, which broke a glossary term-add; the fix made `init_db` run
  `alembic upgrade head` at boot and added the nullability-parity guard so the two paths
  can never drift again.
- **The org-confinement invariant is asserted at boot on both backends.** Postgres never
  ran the SQLite default-org seed, so the invariant is checked directly against the root
  org there. That backend difference lives only in the code, at the `app/db.py::init_db`
  Postgres branch that resolves and asserts against the root org (the enforcement itself is
  `app/db.py::_assert_org_confinement_invariant`, called from that branch); there is no ADR
  for it. The #73 boot invariant it enforces backstops the fail-closed confinement.
- **`int()` is not validation.** A Unicode digit passes `isdigit()` then raises inside
  `int()`, and an over-wide digit string overflows the SQLite binder; `coerce_id` promoted
  the one-bound-both-surfaces rule to the whole app.

## 7. Limits & accepted risks

- **Single-node by design.** The default SQLite install is one process with WAL readers;
  the multi-instance story is Postgres plus the durable job queue (jobs-and-scheduler), and
  cross-node coordination beyond that is out of scope.
- **A legacy pre-Alembic SQLite DB cannot receive non-additive migrations.** The self-heal
  fallback applies additive columns and nullability widening only; the operator is told to
  back up, delete, and rebuild under Alembic to make it authoritative, stated in
  `app/db.py::init_db`.
- **The at-rest cipher is env-gated, not a setting.** Turning SQLCipher on or off is an
  environment decision made before boot, not a runtime toggle, documented in
  secrets-and-crypto.
