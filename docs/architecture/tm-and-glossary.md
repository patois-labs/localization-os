# TM & Glossary: Architecture (L1)

## 1. Role

This subsystem is the **reuse and terminology layer**: translation memory (prior
translations reused on a match) and the glossary/termbase (approved term translations and
do-not-translate rules a run must honor). Both are linguistic assets a workflow step
consults; both are org-owned and customer-scoped.

Translation memory has two stores (`app/services/tm.py`): a segment TM that imports and
exports TMX and does exact-only lookup (a 100 % hash match, or a 101 % in-context match; ADR 0102), and a full-text TM that keys a
whole-document translation by a hash of its normalized source. The `tm` management surface
(`app/services/tm_module.py`) browses, filters, counts usage, and deletes segment TM entries
scoped to the caller's visible projects. The `tm_match` workflow step is orchestration-only
(`app/services/tm_lock.py`): it fills qualifying matches through the shared write seam and
locks the filled segments so later steps skip them.

The glossary has a run-time engine (`app/services/glossary.py`: static term-to-term plus
do-not-translate, context-dependent sense selection, an LLM builder, CSV import) wrapped by a
module (`app/services/glossary_module.py`) that adds a first-class approved container and
language-pair association. The two web surfaces are `/tm` (`app/web/tm.py`) and `/glossaries`
(`app/web/glossaries.py`).

It does not own the run engine that invokes a match or applies a term (translation-pipeline),
the concept/term rich store beneath the compat termbase or TBX conformance (a later
terminology-backend doc claims those), or the empty-delivery approval gate itself (that gate
holds a TM-only delivery for review, in `app/services/tm_delivery.py`, reserved for a later
delivery / export doc).

## 2. Invariants

- **`tm_match` fills only through the shared write seam and never locks a poisoned hit.**
  `app/services/tm_lock.py::fill_or_relock` is a pure decision: given a stored target and a
  TM hit it returns `(action, band, hard, lock_mode)` or `None`, and it neither writes nor
  locks. The orchestrator's `tm_match` branch
  (`app/services/orchestrator.py::_run_workflow_stream_segmented_impl`) acts on that decision:
  it fills a qualifying match through `app/services/quality.py::commit_mt_output` (so a
  poisoned or refusal-shaped entry is withheld at the same seam as MT output) and stamps the
  lock with `app/services/tm_lock.py::_stamp_lock`; a withheld hit is left unlocked and routed
  to review. Enforced by `verify_tm_match_step` (the poisoned-hit-not-locked arm, which drives
  a whole orchestrator run).
- **A soft lock excludes a segment from re-translation; a hard lock refuses a later write.**
  A default 100-band fill soft-locks so downstream MT sees only the remainder, and with
  `tm_hundreds_enter_lqa` off the hard lock makes a second run's direct MT write refuse at
  the seam. Enforced by `verify_tm_match_step` (the soft-then-remainder and hard-lock-survives
  arms).
- **Segment TM is scoped through project visibility.**
  `app/services/tm_module.py::list_entries` filters on the caller's `visible_pids` set, so
  list, get, and delete are confined to the caller's visible projects and a cross-scope delete
  is a no-op. Enforced by `verify_tm_module` (the scoped-browse and cross-scope-delete arms).
  The further property that a NULL-project global row is invisible to a scoped caller is a
  design consequence of that filter (`SegmentTM.project_id.in_(pids)` never matching NULL),
  stated in section 7 rather than separately asserted.
- **TM usage is recorded through one write seam at every apply site.**
  `app/services/tm_module.py::bump_usage` is the single writer of `use_count` / `last_used_at`,
  called from the segmented translate, the workflow step-0 fill, and apply-tm-to-document.
  Enforced by `verify_tm_module`.
- **Retention activity is separate from translation usage.** SegmentTM and FullTextTM
  carry nullable `activity_at`, refreshed by creation, imports, re-approval, edits and
  accepted matches, including borrowed matches. Retention uses
  `coalesce(activity_at, last_used_at, created_at)` (without `last_used_at` for FullTextTM,
  which has no usage column). Borrowers change only lifecycle activity; the owner's
  `use_count` and `last_used_at` retain their existing meaning. Standalone activity
  indexes are unnecessary because retention scans bounded primary-key ranges.
- **Editing an approved glossary's terms or pairs reverts it to draft; metadata and no-op
  imports do not.** `app/services/glossary_module_locale_canonicalization_.py::touch_draft` reverts an approved
  glossary on a term/pair change so it must be re-approved before it re-appears in the
  workflow dropdown, while a rename and a byte-identical re-import leave it approved. Enforced
  by `verify_glossary_module`.
- **Context-sense selection picks the fitting sense and falls back to the base term.**
  `app/services/glossary.py::match_terms` runs sense disambiguation through the provider and,
  with zero context cues, falls back to the base translation rather than guessing. Enforced by
  `verify_glossary_sense_selection`.
- **TM fill is exact-only: 100 % on a normalized-source hash, 101 % when the context also matches.**
  `app/services/tm_hub.py::lookup_batch` (and the positional `app/services/tm_hub.py::lookup_segments`, used
  wherever the same source can occur twice in a document with different neighbours) is one chunked
  `source_hash IN (...)` query per batch through the `_tm_read_scope` seam, with the hash probe as its own
  subquery so the hash index drives it (200,000 units: p95 0.14 ms per segment, limit 2 ms). Among the
  rows of one hash the project's own row is never shadowed by a borrowed (attached-source) one: if an own
  row exists it wins, at 101 when its stored `SegmentTM.context_key` equals the query's or when an
  in-context row from another source holds the same target, else 100; with no own row an in-context row
  wins and reports `match_pct` 101, otherwise lowest id, 100. A near-identical source (one
  word changed) gets no hit and nothing fills from it. `min_score` survives only for old callers: None or
  100 passes, anything lower raises `ValueError` naming ADR 0102. The rapidfuzz scoring, the candidate
  caps and the #599 fuzzy length ceiling were deleted with the fuzzy engine (rapidfuzz stays a dependency
  for post-edit distance); a long source now costs one hash probe, so #599 cannot
  recur on this path. Decided in [ADR 0102](../decisions/0102-exact-only-tm-and-leverage.md). Enforced by
  `verify_tm_exact_only` (recall, 101, latency, fuzzy removed) and
  `verify_pathological_text_599` (80k-character sources stay bounded).
- **101 % needs a context captured at every write.** `app/services/tm_common.py::context_key` hashes the
  normalized previous and next source segments (`ctx:`), `key_context_key` a string key's full name
  (`key:`); `app/services/tm_context.py::segment_context_keys` turns an ordered list of a document's
  segments into those keys. `app/services/tm.py::write_back` stores the key on insert and update (the last
  writer wins, one context per row), the TMX import takes the `x-locos-context-key` prop, else Trados
  `x-context-pre` / `x-context-post`, else (third-party files with at least two units of the pair) the
  adjacent units in file order; a file exported by this product never uses the file-order fallback, so a
  unit without a key stays context-free. A TMX import that changes an existing row's target also takes the
  unit's own key (NULL when it has none; an unchanged target keeps the stored key), and a human edit of a TM
  entry's target (`tm_module.edit_entry`) keeps the key (a correction in the same context). The TMX export
  writes `x-locos-context-key`, so a round trip keeps 101 %. A one-time backfill in the
  `d6b3f8a2c4e1` alembic revision sets keys on existing rows from approved segments still in the
  database; rows without one stay 100 % only. Enforced by `verify_tm_exact_only`.

- **A big TMX export streams in pages; a cut-off one is never well-formed.**
  `app/services/tm.py::export_tmx_stream` reads `SegmentTM` rows in id-ordered pages of
  `app/services/tm.py::TMX_EXPORT_PAGE` through its own session, never as ORM objects, so a 100k-unit
  export no longer builds one tree in memory (about 1.1 GB at that size) and the response starts at
  once (#615). The web download and the `/api` export both return it through
  `app/web/streaming.py::StreamingResponse`. A failure after the first bytes cannot change the 200
  already sent, so the body simply ends without the closing `</body></tmx>`; that closing tag is the
  end marker, and no TMX reader, this app's included, takes a truncated file for a complete one.
  Callers that need a count check read `X-TM-Count`. Enforced by `verify_big_imports_615`.
- **A big TMX import never holds the single SQLite write lock for more than one chunk.**
  `app/services/tm.py::import_tmx` parses and validates the whole file first with no writes, so a
  bad file stores nothing. It then writes in chunks of at most
  `app/services/tm.py::TMX_WRITE_CHUNK_ROWS` rows (2,500), each its own short transaction that takes
  the write lock in `app/services/import_lock.py::begin_write_phase`, commits and releases it, with a
  `TMX_CHUNK_PAUSE_S` pause between chunks so a waiting writer gets in. New rows are sorted by source
  hash so inserts append to each index instead of splitting pages (#584, #615). An import that fits
  one chunk is as atomic as before; a bigger one that fails after a chunk committed keeps those
  chunks and raises `app/services/tm.py::TmxImportIncomplete`, and re-importing the same file
  completes it without duplicates. Each chunk re-reads its rows under its own lock (the file's
  target wins; a row deleted meanwhile is not restored and is counted in `concurrent_deleted`).
  Decided in [ADR 0061](../decisions/0061-tmx-import-chunked-commits.md). Enforced by
  `verify_import_chunks_615` and `verify_big_imports_615`.
- **A TMX import's memory does not grow with the file, and a large TMX imports in the background.**
  The write plan (new rows, the scope's existing rows, the rows the file changes) lives in a throwaway
  SQLite file, `app/services/tm_import_stream.py::ImportSpool`, resolved in batches of
  `TMX_IMPORT_FLUSH_BATCH` units and streamed out in the same chunks as before; the counting and dedupe
  rules are unchanged (`ImportSpool.resolve`). `app/services/tm.py::import_tmx_file` imports a TMX
  straight from disk (streamed pre-scan for entities and the unit count, `iterparse` over the file)
  under `TMX_IMPORT_MAX_BYTES` (2 GiB) and `TMX_IMPORT_MAX_UNITS` (5M). A web form TMX over
  `TMX_IMPORT_INLINE_MAX_BYTES` (8 MiB), and every multipart `POST /api/projects/{pid}/tm/import`,
  is copied to `<data dir>/tm_imports/` and imported by a `tm_import` job
  (`app/services/tm_import_job.py::run`): 202 with the job id at once, progress on the job row, the
  upload removed when the job ends, leftovers swept after a day. Only multipart bodies to the two TM
  import routes get the larger body cap (`app/web/body_limit.py`). Decided in
  [ADR 0094](../decisions/0094-streamed-background-tm-import.md). Enforced by
  `verify_tm_import_background`.

## 3. Where the work happens

| Concern | Pin | One line |
|---|---|---|
| Segment + full-text TM | `app/services/tm_hub.py::lookup_batch` · `app/services/tm.py::import_tmx` · `app/services/tm.py::export_tmx` · `app/services/tm_common.py::_content_type_name` · `app/services/tm_part2.py::_export_entries` · `app/services/import_lock.py::begin_write_phase` | Exact-only segment lookup (100 % / 101 %), the TMX round-trip, and the whole-document full-text store. TMX, TBX and glossary CSV imports are two-phase (Issue #584): parse and read first with no writes, then bulk-write under the write lock (`import_lock`). The glossary CSV and TBX write (project and module glossaries) goes out in chunks too, see the glossary engine row below. The TMX write (Issue #615) goes out in chunks of at most `TMX_WRITE_CHUNK_ROWS` rows (2,500), each its own short transaction with a `TMX_CHUNK_PAUSE_S` pause between them so other writers get in (longest concurrent write wait at 100k units: 4.5 s to 0.2 s). A TMX of that size or smaller is still all-or-nothing; a bigger one that fails after a chunk committed raises `TmxImportIncomplete` (rows kept, the same file re-imports to completion without duplicates). Each chunk re-reads its rows under its lock and applies #584's re-run semantics to them (file target wins, counts as written); a row deleted meanwhile is not restored and is counted in `concurrent_deleted`. `GET /api/projects/{pid}/tm/export` streams its JSON body too. TMX import and the full-text writer apply the approve-time integrity gate (`tm_common.integrity_skip`, Issue #567). A Trados Studio `.sdltm` (SQLite) is recognized by its header inside `import_tmx` and read by `app/services/tm_sdltm.py::import_sdltm` (temp copy opened `mode=ro&immutable=1`, authorizer limited to `translation_units`/`translation_memories`, lxml with entities off, Trados tags to g/x tokens); its units become a TMX that takes this same path, and unreadable units are reported as `skipped_unreadable`. |
| 101 % in-context keys | `app/services/tm_context.py::segment_context_keys` · `app/services/tm_context.py::context_key_for_segment` · `app/services/tm_context.py::key_full_name` · `app/services/tm_common.py::context_key` · `app/services/tm_common.py::key_context_key` | Turns a document's ordered segments (neighbour sources) or a key segment (its string key name) into the context key stored on `SegmentTM.context_key` at write time and compared at lookup time. The alembic revision `d6b3f8a2c4e1` holds a copy of the recipes for its one-time backfill; `verify_tm_exact_only` pins the copy against these. |
| Leverage report | `app/services/leverage.py::analyze` · `app/services/leverage.py::filled_band` · `app/services/leverage.py::to_csv` · `app/cli/_leverage_out.py::deliver_csv` | The 101 % / repetitions / 100 % / new report (see Leverage below). |
| TM management | `app/services/tm_module.py::list_entries` · `app/services/tm_module.py::bump_usage` · `app/services/tm_module.py::stats` · `app/services/tm_module.py::edit_entry` | Scoped browse/filter/paginate, the single usage-write seam, usage stats, and entry edit/delete. An edit runs the same placeholder/inline-tag gate as approve (`TMEditIntegrityRefused`, Issue #567). |
| tm_match decision + lock stamp | `app/services/tm_lock.py::fill_or_relock` · `app/services/tm_lock.py::resolve_policy` · `app/services/tm_lock.py::plan_hit` · `app/services/tm_lock.py::_stamp_lock` | The pure match/lock decision functions the orchestrator's `tm_match` branch acts on, plus the lock-band stamp it calls. |
| Glossary engine | `app/services/glossary.py::match_terms` · `app/services/glossary_hub.py::import_csv` · `app/services/glossary_hub.py::_write_chunks` · `app/services/glossary_common.py::_GLOSSARY_META_COLS` · `app/services/glossary_part2.py::detect_format` · `app/services/glossary_part3.py::export_bytes` | Static term-to-term, do-not-translate, and context sense selection at run time, plus CSV and TBX import. The import write (Issue #615, ADR 0061) goes out in chunks of at most `GLOSSARY_WRITE_CHUNK_ROWS` entries (2,500; module glossaries `GLOSSARY_MODULE_CHUNK_ROWS`, 250, because each chunk also reconciles the concept/term store and binds the touched rows), each its own short transaction with a `GLOSSARY_CHUNK_PAUSE_S` pause between them. Chunks after the first are sized from how long the previous one held the lock (`GLOSSARY_CHUNK_TARGET_S`), because the cost per entry varies tenfold (a module re-import that moves targets retires the old target terms). Longest concurrent write wait at 100k rows: project CSV 3.5 s to 0.23 s, module CSV 84 s to 1.0 s (the module import itself takes three to six times as long, each chunk paying a fixed reconcile and binding cost). A file that fits one chunk is one all-or-nothing transaction (a module one still reconciles the whole glossary); a bigger one that fails after a chunk committed keeps those chunks and raises `GlossaryImportIncomplete` (counts and the error class only; an approved module glossary is reverted to draft), and the same file re-imports to completion with no duplicates. Each chunk re-reads its entries under its own lock and replays the file's values on them (`_replay`): the file wins over an edit made meanwhile, a field it leaves blank keeps the current value, an entry another writer created first is updated, not duplicated, and an entry deleted meanwhile is not restored and is counted in `concurrent_deleted`. The module reconcile takes `entry_ids`, so a chunk costs its own rows. |
| Spreadsheet glossaries and TMs | `app/services/tabular_import.py::glossary_payload` · `app/services/tabular_import.py::tm_tmx` | Batch 6: a glossary XLSX/TSV becomes the glossary CSV `glossary_hub.import_bytes` reads, and a two-column TM XLSX/CSV/TSV becomes a TMX body inside `tm.import_tmx`, so dedupe, chunked writes, the integrity gate and locale reconciliation are the existing importers' own. Rows come from the document filters' readers (`xlsx_filter._load_checked`, `csv_filter._records`). |
| Glossary module | `app/services/glossary_module_locale_canonicalization_.py::approve` · `app/services/glossary_module_locale_canonicalization_.py::touch_draft` · `app/services/glossary_module.py::import_csv` | The approved container, its lifecycle, pair association, and CSV import. |
| TM row counts (cached) | `app/services/tm_counts.py::count` · `app/services/tm_counts.py::ft_count` · `app/services/tm_counts.py::invalidate` · `app/services/tm.py::cached_count` | the project GET, the project page and the customer rollup read `tm_count` / `ft_tm_count` through a process-local cache of the exact `tm_common.count` / `ft_count` COUNT, keyed by (kind, project id), instead of counting every TM row on every hit (7.7 s p95 at 1.7M units). Stale-while-revalidate: a value older than `TTL_SECONDS` (30 s) is still served and ONE background thread refreshes it; the first miss runs one COUNT under a per-key lock while concurrent first callers wait on it; LRU capped at `MAX_KEYS` (4096). `tm_counts.invalidate` runs after TMX import (also on a partial failure), single and bulk TM delete and both scrubs; other writes (approve write-back, full-text store) show up within the TTL. Counts are per worker process. Enforced by `verify_tm_count_cache`. |
| Streamed and background TM import | `app/services/tm_import_stream.py::ImportSpool` · `app/services/tm_import_stream.py::scan_tmx_file` · `app/services/tm.py::import_tmx_file` · `app/services/tm_import_job.py::run` · `app/services/tm_import_job.py::stage_upload` | ADR 0094: the TMX import's write plan in a throwaway SQLite file (`ImportSpool.resolve` applies the per-unit dedupe and count rules in batches, `iter_work` streams the write chunks), the streamed entity and unit pre-scan of a file on disk, and the `tm_import` job: a large upload is copied to `<data dir>/tm_imports/upload-<token>.tmx` (`stage_upload`, under `TMX_IMPORT_MAX_BYTES`), the job imports it with progress on the job row and removes it, and `sweep_stale` clears leftovers older than a day. Enforced by `verify_tm_import_background`. |
| TM web surface | `app/web/tm.py::tm_index` | The `/tm` management tab. |
| Glossary web surface | `app/web/glossaries.py` · `app/web/glossaries_approval_can_tune_ai.py::glossaries_index` | The `/glossaries` customer-first management tab. |

### Leverage

`app/services/leverage.py::analyze` is the one seam behind the project page, `GET /api/projects/{id}/leverage`,
`GET /api/jobs/{id}/leverage`, `project leverage`, `job leverage`, the MCP `list_projects` and `job_status`
`leverage` views, quotes and the vendor estimate (ADR 0102). It counts source words (the vendor-cost word counter)
per target language in four bands, in source order (document order, then `order_index`), key-based content included:

| Band | Rule |
|---|---|
| `context` (101 %) | an in-context TM match, or a segment already filled or locked as 101 / ICE (`tm_match_pct >= 101`, lock band `ice`, `101` or `context`) |
| `repetitions` | not 101 %, and the same whitespace-normalized source occurred earlier in the scope for that language |
| `exact` (100 %) | an exact TM match, or a segment already filled or locked from TM at 100 (`tm_match_pct == 100` or a non-fuzzy lock band) |
| `new` | everything else: MT, human, old 75-99 fills, a `fuzzy` lock |

Precedence is 101 % > repetitions > 100 % > new. A filled segment (non-empty target) keeps the band it was filled
with, read from its persisted fields; an unfilled segment is looked up live with `tm_hub.lookup_segments` and its
context key, one positional lookup per language across all documents, inside one `source_ids_memo`. The scope is a whole project, or the documents and target
languages one `translate_document` job covered (`leverage.job_scope`); any other job kind has no documents and is a
400. Languages never share repetitions. `vendor_cost.classify_band` applies the same rule from persisted fields, with
no live lookup, to price a vendor dispatch. Enforced by `verify_leverage_report`.

## 4. Data & settings

- **Models:** `app/models.py::SegmentTM` (a source/target reuse row, scoped through its
  project, with a nullable `context_key` for the 101 % match) and `app/models.py::FullTextTM` (a whole-document translation keyed by source
  hash); `app/models.py::Glossary` (the approved container), `app/models.py::GlossaryPair`
  (a language-pair association), `app/models.py::GlossaryEntry` (a term with its DNT flag),
  and `app/models.py::GlossarySense` (a context-tagged translation of a term).
- **Settings:** `setting:tm_hundreds_enter_lqa` (default `"1"`) chooses soft vs hard
  lock for a 100 fill; `setting:tm_lock_ice` (default `"1"`) hard-locks a 101 (in-context) fill.
  The fill threshold, fuzzy lock mode and context floor settings were removed with fuzzy matching
  (ADR 0102). `setting:tm_empty_delivery_gate` (default `"1"`) holds a TM-only
  empty delivery for human approval.

## 5. Enforced by

- `verify_tm_match_step`: the fill-and-lock, the soft-lock remainder, the hard-lock refusal,
  and the poisoned-hit withheld from locking.
- `verify_tm_lock`: the lock band and mode resolution and the human-edit unlock.
- `verify_tm_module`: the scoped browse, the usage-write seam, the stats, and delete.
- `verify_tm_exact_only`: exact recall at 200,000 units, the 101 % in-context match, the 2 ms latency
  bound, fuzzy removal, write-back context, TMX round trip, the backfill and string-key projects.
- `verify_glossary_module`: the approval lifecycle, the edit-reverts-to-draft rule, and the
  idempotent-import-does-not-revert rule.
- `verify_glossary_sense_selection`: the context-sense pick and the zero-cue base fallback.
- `verify_big_imports_615`: the streamed TMX export, the sorted bulk write phase and the
  responsiveness of other requests during a large import.
- `verify_import_chunks_615`: the chunk bound on every TMX write transaction, the free write lock
  between chunks, `TmxImportIncomplete` and its duplicate-free retry, `concurrent_deleted`, the
  streamed JSON TM export and the refused unknown glossary CSV header.
- `verify_tm_import_background`: the file path import against the bytes import (replies and rows),
  the streamed pre-scan, the TMX caps, the flat memory, and the web, API and CLI background import.

## 6. Battle scars

- **A `tm_match` fill must be caught by the same net as MT output.** Filling through
  `app/services/quality.py::commit_mt_output` rather than writing the segment directly means a
  poisoned or refusal-shaped TM entry is withheld and routed to review exactly as an MT
  hedge would be, proven by `verify_tm_match_step`.
- **A changed approved glossary must lose its sign-off.** Editing terms or pairs reverts to
  draft so a published glossary that changes is re-approved before it drives a run again,
  while a rename or a byte-identical re-import deliberately does not revert, recorded in the
  `app/services/glossary_module.py` module docstring and proven by `verify_glossary_module`.
- **The 101 % context is one key per row.** A source used in several contexts is 101 % only in the
  context it was last approved in and 100 % elsewhere; storing several contexts per source would need the
  context in the unique key (ADR 0102 consequences). Old rows with a stored 75-99 `tm_match_pct` or
  `lock_band` "fuzzy" are read as not leveraged.

- **Concurrent TM writers (ADR 0105, audited 2026-10-24).** Concurrent imports into one project lost data on
  Postgres (991,509 of 1,726,003 units). The chunk write is now an `ON CONFLICT DO NOTHING` insert with conflicted units taking the #584 late path and a verified compare-and-set
  (`app/services/tm.py::_import_tmx_write`), and a project's `tm_import` jobs run one at a time
  (`jobqueue.serial_key`, see jobs-and-scheduler). Inline imports (web form, CLI, JSON API) are not serialized: (a) keeps them correct, but against a running job on Postgres they can need chunk retries and, worst case, stop with `TmxImportIncomplete` (rows kept, a re-run finishes). The other writers: `tm.write_back` was already safe (read,
  INSERT, `IntegrityError` then re-read and update, P-C2); `tm_module.edit_entry` and `delete_entry` are single-row last-writer-wins updates
  and deletes with no unique key to hit; `tm_part2.ft_store` (full-text TM) has no unique index, so a race
  leaves a duplicate row that `ft_lookup` resolves deterministically (own project first, lowest id), never an
  error, and the TMX path does not write it; the ADR 0102 `context_key` backfill in alembic `d6b3f8a2c4e1`
  runs once, in the one upgrade process, in chunks, and only fills NULLs. Enforced by
  `verify_tm_import_concurrency`.
## 7. Limits & accepted risks

- **Global (NULL-project) TM rows are invisible to a scoped caller.** A customer-bound or
  multi-org caller never sees org-less global rows, an intentional isolation trade-off that
  can hide a legitimately shared entry from a scoped user, stated in the
  `app/services/tm_module.py` module docstring.
- **The full-text TM keys on a normalized-source hash.** A whole-document memory hit requires
  the normalized source to match, so a small edit misses the memory and re-translates, stated
  in the `app/services/tm.py` module docstring.

### D5 paging scope (#122)

D5 bounded-page acceptance covers keys, glossary entries (including their attached
senses), and TM entries. A page loads only its SQL window and uses a constant number
of SELECTs independent of collection size. Glossary pages use two SELECTs: at most
N+1 entry rows and one sense fetch for the N returned entries. Attached sense count
can vary per entry; this is not a bound on sense fan-out, database scan work, bytes,
or latency. TM pages retain their existing count plus OFFSET/LIMIT query and 500 cap.
Legacy glossary limit/offset windows execute in SQL; deep offsets may still scan
skipped rows. Full-list compatibility reads and complete exports intentionally walk
all entries and are outside the bounded-page guarantee.

Every other collection is outside D5 acceptance: projects, customers, documents,
segments and key values, full-text TM, glossary module catalogs, concepts and concept
terms, term-extraction candidates, workflow steps/runs, jobs/events/audit logs,
users/members/invitations, vendors, connectors, language sets/locales, content types,
style guides, reports and all other administrative
or provider catalogs. Other panels on the project and glossary pages (in particular
the concept editor and extraction queue) keep their existing complete-read behavior;
D5 is a service-page guarantee, not a whole-page rendering-cost claim.

`glossary.page_entries` is the bounded seam; `iter_entries` walks every page.
`list_entries` and `list_entries_for_glossary` deliberately materialize all entries.
The entry id is already a primary key. The sense foreign key has no declared index
in the model, SQLite index shim, or migrations; this change adds no index or migration.
