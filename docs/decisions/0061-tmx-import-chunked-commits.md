# 0061. TMX imports commit in bounded chunks

Status: Accepted
Date: 2026-10-08

## Context

ADR-less decision of #584: a TMX import is all-or-nothing AND takes the SQLite write lock only briefly.
That held for small files. At scale the write phase costs about 40 us per row, so one transaction held the lock
4.5 s at 100k units and about 12 s at 300k (pressure test T4, #615): every other writer (approve, audit,
orchestrator steps) stalled that long. Merging or dropping one of the two segmenttm hash indexes would need a
migration and only shaves the constant.

## Decision

The TMX write phase goes out in chunks of at most `TMX_WRITE_CHUNK_ROWS` (2,500) rows. Each chunk is its own
short transaction that takes the write lock, writes, commits and releases it; `TMX_CHUNK_PAUSE_S` (50 ms) passes
between chunks so a writer polling SQLite's busy handler gets in. Parse and validation still finish before the
first write, so a bad file stores nothing.

Atomicity, stated honestly: an import of 2,500 rows or fewer is exactly as atomic as before. A bigger import that
fails after a chunk committed keeps those chunks and raises `TmxImportIncomplete` (API: 500 with `partial`,
`rows_written`, `rows_total`). It is safe to retry: rows are keyed by (project, locales, source hash), so the same
file stores only the missing rows.

Concurrent writers (review finding): #584's single transaction re-read the TM under the lock and re-ran the import, so
the file's target won over any change committed before the write and the counts were the re-run's
(`verify_import_lock_584` pins that). Each chunk now does the same for its own rows under its own lock: it never
writes from stale phase-1 state, an edit made meanwhile gets the file's target (counted `updated`), a source
another writer stored first is updated, not duplicated, and a row deleted meanwhile is not restored and is counted
in `concurrent_deleted` instead of as written. Target updates are compare-and-set on the value just re-read, so
Postgres (row locks only) holds the same rule.

Glossary CSV and TBX imports were unchanged when this was accepted; see "Extended to glossary imports (10-08)" below.

## Consequences

- Longest concurrent write wait during a 100k import: 4.5 s to 0.2 s; 300k: 0.2 s. Total import time grows about 12%.
- Revert by setting `tm.TMX_WRITE_CHUNK_ROWS` high (one transaction again).
- The all-or-nothing guarantee for files over 2,500 rows is gone; the retry contract replaces it.

## Extended to glossary imports (10-08)

The same design now covers glossary CSV and TBX imports, project and module glossaries (the last open
part of #615). Longest concurrent write wait at 100k rows, before to after: project CSV 3.5 s to 0.23 s, project TBX 3.7 s to 0.25 s, module CSV 84 s to 0.5 s, module TBX 80 s to 1.1 s (p95 about 0.15 s). The import itself takes longer (project CSV 4.6 s to 6.8 s, project TBX 14 s to 17 s, module CSV 86 s to 146 s, module TBX 90 s to 156 s). Module imports at 100k run at about 1.7x of a one-transaction import; accepted at the gate (10-08): most of the difference is the 50 ms lock-free pause per chunk (about 30%) plus a fixed reconcile and binding cost per chunk, and a longer hold or shorter pause would trade back the lock-wait win.

- Chunks: at most `GLOSSARY_WRITE_CHUNK_ROWS` (2,500) entries for a project glossary and
  `GLOSSARY_MODULE_CHUNK_ROWS` (250) for a module glossary, whose chunk also reconciles the concept/term
  store and binds the touched rows (about 0.8 ms per entry). Each chunk is a short transaction followed by a
  `GLOSSARY_CHUNK_PAUSE_S` (50 ms) pause with the lock free. After the first chunk, chunk size follows how long
  the previous ones held the lock (`GLOSSARY_CHUNK_TARGET_S`, 0.15 s), because the cost per entry varies tenfold
  (retiring a moved target costs about 8 ms). The module reconcile and binding pass run per chunk on that
  chunk's entries only (`entry_ids`); a one-chunk import still reconciles the whole glossary as before.
- Atomicity, unchanged in kind: a bad file fails in phase 1 and stores nothing; a file whose work fits one
  chunk is all-or-nothing; a bigger one that fails after a chunk committed keeps those chunks and raises
  `GlossaryImportIncomplete` (API 500 with `partial`, `rows_written`, `rows_total`; the message and every
  surface carry the error class only, never SQL or row text). An approved module glossary is reverted to draft
  by a partial import, as a finished one would. Retrying the same file finishes the job: entries are keyed by
  (scope, locales, source term), so stored ones count as unchanged.
- Concurrent writers: each chunk re-reads its entries under its own lock (`FOR UPDATE` on Postgres) and replays
  what the file says on them. The file wins where it speaks (target, dnt, meta keys), a field the file leaves
  blank keeps the current value, an entry another writer created first is updated, not duplicated (found by
  id range above the last chunk, since the table has no index on `source_term`), and an entry deleted
  meanwhile is not restored and is counted in `concurrent_deleted`. This replaces #584's `ImportScopeChanged`
  re-run for glossaries. On Postgres, sequence ids can commit out of order, so an entry created by another
  writer in the same instant can slip past that range check (there is no unique key to catch it either).
- Module concept identity map: a multi-chunk module import loads the glossary's source-surface map
  (`(source_locale, casefold source_term) -> concept_id`) ONCE, before the first chunk takes the lock
  (`prime_reconcile_cache`), and tops it up each chunk from entry ids above the last one seen. Each chunk
  checks every hit against the store under its lock: the concept must still exist in the glossary and still
  carry that source surface, else the surface is looked up again from the entries (so a surface deleted and
  created again meanwhile links to the live concept, never a second one). Known limit, the same blind spot as
  the entry replay above: an OLDER entry renamed by another writer onto a surface this import has not reached
  yet is not seen, so the import adds its own entry and concept for it; the boot-time
  `consolidate_split_concepts` merges such a split.
- Also: the module CSV term-column pass (`_apply_csv_term_columns`) looks entries up per 500 surfaces instead
  of scanning the glossary per row, and commits per batch.
- Revert: set `glossary_hub.GLOSSARY_WRITE_CHUNK_ROWS` and `GLOSSARY_MODULE_CHUNK_ROWS` high (one transaction).

## Links

- Issues #584, #615; `app/services/tm.py` (PHASE 2), `tests/verify_import_chunks_615.py`.
