# 0102. TM fill is exact-only (100 % and in-context 101 %); fuzzy matching removed; leverage is 101 / 100 / repetitions / new

Status: Accepted (S6b)
Date: 2026-10-24

## Context

Fuzzy TM matching (rapidfuzz, 75-99 %) scored at most 500 length-sorted candidates per source. At 30,000 units it
returned the best match for about 10 % of one-word-changed queries, and with a 2M-unit TM the pipeline fell from
about 261 to 16-29 jobs/min (target T5). The `tm_match`
step already filled at 100 only by default; the fuzzy engine was mostly dormant code plus two translate paths that
stamped a fuzzy score on MT output. There was no 101 % (in-context) match at all: `SegmentTM` had no context.
An inventory of every fuzzy site preceded the removal.

## Decision

**Exact-only fill.** A TM unit fills a segment only on an exact match: the normalized source hash
(`tm_common.hash_text`, unchanged, native-backed per ADR 0091) is equal. Every read path (pretranslate, the
`tm_match` workflow step, the translate door, apply-tm, API/CLI/MCP lookups, key projects, add-on tools,
editor grid) goes through the one exact lookup, `tm_hub.lookup_batch` / `tm_hub.lookup_segments`.

**101 % in-context.** A 100 % hit is promoted to 101 % when the stored unit's context key equals the query's:

- documents: the previous and next source segments match (same document, same target locale, by `order_index`;
  a missing neighbour at a document edge is the empty string). Key = `"ctx:" + sha256(normalize(prev) + US +
  normalize(next))` (`tm_common.context_key`). US is U+001F.
- key (software) projects: the string key matches. Key = `"key:" + sha256(<the key's full name>)`.

`SegmentTM.context_key` (nullable string, Alembic revision d6b3f8a2c4e1) holds the context of the last write for
that (project, source hash, locale pair) row; the unique key is unchanged (one row per source, last writer wins, as
before). So a source used in several contexts gives 101 % only in the context it was last approved in, and 100 %
elsewhere. Among rows for one hash (own project plus attached TM sources), the project's OWN row is never
shadowed by a borrowed one, because this path applies a target without review: if an own row exists it is
the winner, and it is 101 % when its own `context_key` matches, or when an in-context row from another
source holds the SAME target text (the borrowed row confirms it); an in-context borrowed row with a
different target leaves the own row at 100 %. With no own row, an in-context row wins as 101 % and
otherwise the first row by precedence (lowest id) at 100 %. A 101 % hit is stamped
`tm_match_pct = 101` and locks in the `ice` band (`tm_lock_ice`, already a setting). Every consumer that tested
`== 100` now tests `>= 100`.

Writes capture context: the approve write-back (from the segment's neighbours or its string key), TMX import
(Trados-style `x-context-pre`/`x-context-post` props, the app's own `x-locos-context-key` prop, else for a
third-party file the adjacent TUs in file order), and TMX export writes `x-locos-context-key` so a round trip keeps
101 %. The file-order fallback needs evidence of document order: a file with a single unit of the pair gets none (a
lone unit would otherwise become ctx of two empty strings and falsely lock any one-segment document as 101 %), and a
file exported by this product never uses it, so a unit without a key stays context-free. A TMX import that CHANGES
an existing row's target also replaces its `context_key` with the imported unit's own key (NULL when the unit has
none), because the old key described the old target's approval; an unchanged target keeps its key. A human edit of a
TM entry's target in the TM module keeps the key (a correction in the same context). Existing rows are
backfilled once from approved segments still in the database whose target equals the row's target; rows with no
such segment stay context-free (100 % only).

**Fuzzy removed entirely.** No fuzzy fill, no fuzzy lookup, no opt-in setting. Removed: the rapidfuzz scoring in
`tm_hub` (rapidfuzz stays a dependency for post-edit distance), `TM_FUZZY_*`/`TM_UNION_CHUNK`,
`example_matches_batch`, the settings `tm_fill_threshold`, `tm_fuzzy_lock_mode`, `tm_context_floor`, the
WorkflowStep fields of the same names (their columns are dropped by the same migration), the fuzzy lock band and parity check
in `tm_lock`, the fuzzy badges in the editor and document pages, the fuzzy leverage and vendor bands. Callers that
still send a removed parameter (`min_score` below 100, `tm_fill_threshold` below 100, `tm_fuzzy_lock_mode`,
`--fill-threshold`, `--fuzzy-lock-mode`, rate-card `fuzzy_*` keys) get a clear error naming this ADR, not a silent
ignore. A `tm_fill_threshold` of 100 (or blank) is accepted as a no-op; any other threshold, and any
`tm_fuzzy_lock_mode` or `tm_context_floor` value, is refused with the ADR 0102 message on every surface, so a
workflow re-created from a pre-upgrade dump must have those keys removed first. Stored segments keep their old `tm_match_pct` 75-99 and `lock_band =
'fuzzy'` values; reads treat them as "new" (not leveraged).

**TM few-shot examples** in prompts came from the fuzzy scorer and go with it. The core ships no example
retriever: an optional add-on may supply one; without it a step that asks for examples gets none.

**Leverage.** Word counts per target language in four bands, computed over the segments of a project or of a job
(the documents and target locales one translate job covered), in source order:

| Band | Id | Rule |
|---|---|---|
| 101 % | `context` | an in-context TM match (or a segment already filled or locked as 101 / ICE) |
| Repetitions | `repetitions` | not 101 %, and the same normalized source occurred earlier in the scope for that language |
| 100 % | `exact` | an exact TM match (or a segment already filled or locked from TM at 100) |
| New | `new` | everything else (MT, human, old fuzzy fills) |

Precedence is 101 % > repetitions > 100 % > new (standard CAT order: only the first occurrence of a repeated
source is priced as 100 % or new). Words use the vendor-cost word counter. A segment that has been filled keeps the
band it was filled with; a segment not yet filled is looked up live against the TM in scope, so an analysis before
translation is a quote and an analysis after translation reports what was actually leveraged. The vendor rate card
prices the same four band ids.

## Consequences

- Fill recall is exact by construction; lookup is one indexed `IN` query per batch (T5: at most 2 ms per segment
  at 200,000 units). T5 is promoted into `tests/verify_tm_exact_only.py`.
- Customer-visible: no more 75-99 % TM suggestions or fills; editor shows exact matches only; leverage and
  quotes show four bands instead of seven; API/CLI/MCP leverage responses change shape; vendor rate cards lose
  three fuzzy prices (stored cards load with them dropped; saving one is refused); prompts that used fuzzy
  examples get none (or an add-on's).
- One context per (project, source, locale pair) row. Storing several contexts per source would need the context
  in the unique key; revisit if 101 % rates look low on repetitive content.
