# TM & Glossary: Architecture (L1)

## 1. Role

This subsystem is the **reuse and terminology layer**: translation memory (prior
translations reused on a match) and the glossary/termbase (approved term translations and
do-not-translate rules a run must honor). Both are linguistic assets a workflow step
consults; both are org-owned and customer-scoped.

Translation memory has two stores (`app/services/tm.py`): a segment TM that imports and
exports TMX and does exact-plus-fuzzy lookup with rapidfuzz, and a full-text TM that keys a
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
- **Fuzzy lookup is exact-plus-fuzzy and batch-consistent.**
  `app/services/tm_hub.py::lookup_batch` returns a 100 exact match and below-100 fuzzy matches,
  and agrees with N individual lookups (including a miss). Enforced by `verify_tm_module`.
- **Fuzzy scoring is bounded (#599).** `app/services/tm_hub.py::_fuzzy_ratio` passes the
  caller's floor as the rapidfuzz score cutoff (exact at or above it) and never fuzzy-scores a
  source or TM row over `TM_FUZZY_MAX_CHARS` (10,000 normalized characters; such text still gets
  its exact 100 match), because rapidfuzz holds the GIL and a long source against long rows froze
  every thread. Enforced by `verify_pathological_text_599`.

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

## 3. Where the work happens

| Concern | Pin | One line |
|---|---|---|
| Segment + full-text TM | `app/services/tm_hub.py::lookup_batch` · `app/services/tm.py::import_tmx` · `app/services/tm.py::export_tmx` · `app/services/tm_common.py::_content_type_name` · `app/services/tm_part2.py::_export_entries` · `app/services/import_lock.py::begin_write_phase` | Exact-plus-fuzzy segment lookup, the TMX round-trip, and the whole-document full-text store. TMX, TBX and glossary CSV imports are two-phase (Issue #584): parse and read first with no writes, then bulk-write under the write lock (`import_lock`). The glossary CSV and TBX write (project and module glossaries) goes out in chunks too, see the glossary engine row below. The TMX write (Issue #615) goes out in chunks of at most `TMX_WRITE_CHUNK_ROWS` rows (2,500), each its own short transaction with a `TMX_CHUNK_PAUSE_S` pause between them so other writers get in (longest concurrent write wait at 100k units: 4.5 s to 0.2 s). A TMX of that size or smaller is still all-or-nothing; a bigger one that fails after a chunk committed raises `TmxImportIncomplete` (rows kept, the same file re-imports to completion without duplicates). Each chunk re-reads its rows under its lock and applies #584's re-run semantics to them (file target wins, counts as written); a row deleted meanwhile is not restored and is counted in `concurrent_deleted`. `GET /api/projects/{pid}/tm/export` streams its JSON body too. TMX import and the full-text writer apply the approve-time integrity gate (`tm_common.integrity_skip`, Issue #567). A Trados Studio `.sdltm` (SQLite) is recognized by its header inside `import_tmx` and read by `app/services/tm_sdltm.py::import_sdltm` (temp copy opened `mode=ro&immutable=1`, authorizer limited to `translation_units`/`translation_memories`, lxml with entities off, Trados tags to g/x tokens); its units become a TMX that takes this same path, and unreadable units are reported as `skipped_unreadable`. |
| TM management | `app/services/tm_module.py::list_entries` · `app/services/tm_module.py::bump_usage` · `app/services/tm_module.py::stats` · `app/services/tm_module.py::edit_entry` | Scoped browse/filter/paginate, the single usage-write seam, usage stats, and entry edit/delete. An edit runs the same placeholder/inline-tag gate as approve (`TMEditIntegrityRefused`, Issue #567). |
| tm_match decision + lock stamp | `app/services/tm_lock.py::fill_or_relock` · `app/services/tm_lock.py::resolve_policy` · `app/services/tm_lock.py::plan_hit` · `app/services/tm_lock.py::_stamp_lock` | The pure match/lock decision functions the orchestrator's `tm_match` branch acts on, plus the lock-band stamp it calls. |
| Glossary engine | `app/services/glossary.py::match_terms` · `app/services/glossary_hub.py::import_csv` · `app/services/glossary_hub.py::_write_chunks` · `app/services/glossary_common.py::_GLOSSARY_META_COLS` · `app/services/glossary_part2.py::detect_format` · `app/services/glossary_part3.py::export_bytes` | Static term-to-term, do-not-translate, and context sense selection at run time, plus CSV and TBX import. The import write (Issue #615, ADR 0061) goes out in chunks of at most `GLOSSARY_WRITE_CHUNK_ROWS` entries (2,500; module glossaries `GLOSSARY_MODULE_CHUNK_ROWS`, 250, because each chunk also reconciles the concept/term store and binds the touched rows), each its own short transaction with a `GLOSSARY_CHUNK_PAUSE_S` pause between them. Chunks after the first are sized from how long the previous one held the lock (`GLOSSARY_CHUNK_TARGET_S`), because the cost per entry varies tenfold (a module re-import that moves targets retires the old target terms). Longest concurrent write wait at 100k rows: project CSV 3.5 s to 0.23 s, module CSV 84 s to 1.0 s (the module import itself takes three to six times as long, each chunk paying a fixed reconcile and binding cost). A file that fits one chunk is one all-or-nothing transaction (a module one still reconciles the whole glossary); a bigger one that fails after a chunk committed keeps those chunks and raises `GlossaryImportIncomplete` (counts and the error class only; an approved module glossary is reverted to draft), and the same file re-imports to completion with no duplicates. Each chunk re-reads its entries under its own lock and replays the file's values on them (`_replay`): the file wins over an edit made meanwhile, a field it leaves blank keeps the current value, an entry another writer created first is updated, not duplicated, and an entry deleted meanwhile is not restored and is counted in `concurrent_deleted`. The module reconcile takes `entry_ids`, so a chunk costs its own rows. |
| Spreadsheet glossaries and TMs | `app/services/tabular_import.py::glossary_payload` · `app/services/tabular_import.py::tm_tmx` | Batch 6: a glossary XLSX/TSV becomes the glossary CSV `glossary_hub.import_bytes` reads, and a two-column TM XLSX/CSV/TSV becomes a TMX body inside `tm.import_tmx`, so dedupe, chunked writes, the integrity gate and locale reconciliation are the existing importers' own. Rows come from the document filters' readers (`xlsx_filter._load_checked`, `csv_filter._records`). |
| Glossary module | `app/services/glossary_module_locale_canonicalization_.py::approve` · `app/services/glossary_module_locale_canonicalization_.py::touch_draft` · `app/services/glossary_module.py::import_csv` | The approved container, its lifecycle, pair association, and CSV import. |
| TM web surface | `app/web/tm.py::tm_index` | The `/tm` management tab. |
| Glossary web surface | `app/web/glossaries.py` · `app/web/glossaries_approval_can_tune_ai.py::glossaries_index` | The `/glossaries` customer-first management tab. |

## 4. Data & settings

- **Models:** `app/models.py::SegmentTM` (a source/target reuse row, scoped through its
  project) and `app/models.py::FullTextTM` (a whole-document translation keyed by source
  hash); `app/models.py::Glossary` (the approved container), `app/models.py::GlossaryPair`
  (a language-pair association), `app/models.py::GlossaryEntry` (a term with its DNT flag),
  and `app/models.py::GlossarySense` (a context-tagged translation of a term).
- **Settings:** `setting:tm_fill_threshold` (default `"100"`) is the minimum match percent a
  `tm_match` step fills; `setting:tm_hundreds_enter_lqa` (default `"1"`) chooses soft vs hard
  lock for a 100 fill; `setting:tm_fuzzy_lock_mode` (default `"soft"`) is how a below-100
  fuzzy fill locks. `setting:tm_empty_delivery_gate` (default `"1"`) holds a TM-only
  empty delivery for human approval.

## 5. Enforced by

- `verify_tm_match_step`: the fill-and-lock, the soft-lock remainder, the hard-lock refusal,
  and the poisoned-hit withheld from locking.
- `verify_tm_lock`: the lock band and mode resolution and the human-edit unlock.
- `verify_tm_module`: the scoped browse, the usage-write seam, the stats, and delete.
- `verify_glossary_module`: the approval lifecycle, the edit-reverts-to-draft rule, and the
  idempotent-import-does-not-revert rule.
- `verify_glossary_sense_selection`: the context-sense pick and the zero-cue base fallback.
- `verify_big_imports_615`: the streamed TMX export, the sorted bulk write phase and the
  responsiveness of other requests during a large import.
- `verify_import_chunks_615`: the chunk bound on every TMX write transaction, the free write lock
  between chunks, `TmxImportIncomplete` and its duplicate-free retry, `concurrent_deleted`, the
  streamed JSON TM export and the refused unknown glossary CSV header.

## 6. Battle scars

- **A `tm_match` fill must be caught by the same net as MT output.** Filling through
  `app/services/quality.py::commit_mt_output` rather than writing the segment directly means a
  poisoned or refusal-shaped TM entry is withheld and routed to review exactly as an MT
  hedge would be, proven by `verify_tm_match_step`.
- **A changed approved glossary must lose its sign-off.** Editing terms or pairs reverts to
  draft so a published glossary that changes is re-approved before it drives a run again,
  while a rename or a byte-identical re-import deliberately does not revert, recorded in the
  `app/services/glossary_module.py` module docstring and proven by `verify_glossary_module`.
- **The context (ICE / 101) band is deliberately deferred.** The `tm_match` engine fills the
  100 band only in this wave because a context band needs a TM-entry context column captured
  at every write seam plus a backfill, recorded in the `app/services/tm_lock.py` module docstring.

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
