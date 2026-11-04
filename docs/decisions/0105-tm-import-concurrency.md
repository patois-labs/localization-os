# 0105. TM imports into one project: conflict-safe chunk writes and one running import per project

Status: Accepted
Date: 2026-10-24

## Context

S7c (a 16-core test server, Postgres, 4 web + 8 worker processes, the durable job queue of ADR 0092) uploaded a
2,000,000-unit TMX into ONE project as seven chunk files. The seven uploads became seven `tm_import` jobs
(ADR 0094) that ran at once. Four failed (three with "a database error stopped the import; nothing was stored",
one stopped at 35,000 of 285,626 rows) and only 734,494 of 1,726,003 units were stored. The same run on SQLite in
one process was fine.

The cause is in `tm._import_tmx_write`. Each write chunk re-reads which source hashes are already stored, then
plain-INSERTs the ones it found absent. On SQLite the write lock (taken first, `import_lock.begin_write_phase`)
makes the re-read and the INSERT one step. On Postgres the lock is per row: two jobs both saw a unit as absent,
both inserted it, and `ux_segmenttm_upsert_key` (project, source hash, source locale, target locale) refused the
second with a `UniqueViolation`. The chunks share repeated sentences, as real TMX sets do. Nothing in the queue
limited a project to one import at a time: every worker thread of every worker process took a `tm_import` job.

## Decision

**(a) The chunk write cannot lose to a concurrent writer.**

- New units are written with `INSERT ... ON CONFLICT (project_id, source_hash, source_locale, target_locale) DO
  NOTHING RETURNING source_hash` (the dialect insert of SQLAlchemy on Postgres and SQLite; the index is the plain
  unique index, not partial, and NULL project ids never conflict). The statement stores what it can and names what
  it stored.
- A unit that lost to a concurrent writer is re-read and joins the chunk's existing "late" path (#584 semantics):
  the file's target is applied to the stored row through the compare-and-set `UPDATE`, with the file unit's
  context key (ADR 0102), and the unit moves from `imported` to `updated` or `skipped`. A unit stored and deleted
  again meanwhile is retried (`TMX_INSERT_ROUNDS`) and then counted `concurrent_deleted`. No import fails or
  drops a row silently because of a concurrent writer.
- The compare-and-set is now verified: the targets are read back, a miss (the row changed again between the re-read
  and the update) is retried on the fresh target (`TMX_CAS_ROUNDS`, the file's target still wins over any change
  committed before the write), and a row that is deleted meanwhile or never settles is not counted written.
  This covers `_existing_chunk` too.
- A legacy SQLite file whose unique index was skipped (pre-existing duplicates) falls back to the plain INSERT.

**(b) A project's TM imports run one at a time, in submission order.**

- `jobqueue.serial_key` (nullable, alembic `e5a1c9d3b7f2`): `tm_import:<project id>` for a TM import, NULL for every
  other kind. `jobqueue.serial_key_for` is the one definition; `enqueue` writes the key in the same `INSERT ...
  SELECT` that creates the row, so a worker can never claim a keyed row before its key exists.
- A partial UNIQUE index `ux_jobqueue_serial_running ON jobqueue (serial_key) WHERE state = 'running'` is the hard
  guarantee. A second partial index `ix_jobqueue_serial_active` serves the claim's look-up.
- The claim's candidate SELECT skips a pending keyed row when another row of its key is running, or is pending and
  older (the older-pending half keeps submission order when two Postgres claimers skip each other's locked row).
  A waiting import is simply not picked: it holds no worker slot. The pool ordering is unchanged.
- Two Postgres claimers can still flip two rows of one key in the same instant: the unique index refuses the
  second flip, `_try_claim` catches the `IntegrityError`, rolls back and claims nothing that round (the row stays
  pending, no lease). Lease expiry and `hand_back` return a running row to pending and so free the key.
  `release` of a keyed row wakes the next claimer at once, here and in the other processes.
- The in-memory adapter enforces the same rule in-process (a busy-key set; `claim` skips a blocked job).
- The migration backfills the key of pending and running `tm_import` queue rows from the payload (the oldest
  running row of a project keeps it, an older pre-fix twin stays NULL so the index can be built).
- A waiting import's job row says "Waiting for another import into this project to finish" until it starts (web
  page, `GET /api/jobs/{id}`, `job status`, MCP `job_status` all read the row's message).

## Consequences

- Inline imports (the web form, the CLI, the API's JSON path) are not serialized; part (a) keeps them correct.
  Against a running job on Postgres they can need chunk retries and, in the worst case, stop with
  `TmxImportIncomplete` (rows kept, a re-run finishes).
- A drain hand-back returns a running import to pending while its handler thread keeps writing until the
  process exits, so another process may run the same import alongside it; part (a) keeps that safe
  (pre-existing, not changed here).
- Queue rows follow their Job: the project-delete cascade deletes the `jobqueue` rows of the Job rows it deletes
  (retention keeps live queue rows of finished jobs on purpose, #438), and the claim's blocking test counts a pending or running row of a key only while its Job exists
  and is queued or running, so an orphaned row can never hold a key (SQLite reuses project ids). Per-key order is
  the Job id, not the queue row id (a requeue makes a higher row id). `serial_key_for` and the SQL key written by
  `enqueue` use one rule (a JSON number truncated to an integer, or a digit string; anything else, or an id beyond
  9e18, has no key), pinned by the suite.
- Real TMX sets uploaded as parallel chunks import completely, on Postgres with any number of web and worker
  processes. The last submitted chunk's target wins every overlapping unit (submission order).
- Throughput of ONE project's imports is serial by design; different projects still import in parallel. Direct
  callers of `tm.import_tmx` (the inline web form, the CLI, the API's JSON path) are not queued, and are now
  merely safe against each other and against the job path, not ordered.
- A unit changed by another writer every round for `TMX_CAS_ROUNDS` rounds keeps the other writer's target and is
  counted `skipped`; a unit deleted meanwhile is counted `concurrent_deleted`.
- Imports with a NULL project (global TM) cannot conflict on the unique key (SQL keeps NULLs distinct) and are not
  serialized (no project id); they were never affected by the failure above.
- Other TM writers audited in this change (write-back, an add-on's per-row hook, edits, full-text TM, the ADR 0102 backfill) are
  recorded in `docs/architecture/tm-and-glossary.md`.
- Guard: `tests/verify_tm_import_concurrency.py` (SQLite always; Postgres with `LOC_TMS_TEST_PG_URL`).
