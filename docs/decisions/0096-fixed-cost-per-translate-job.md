# 0096. Fixed cost per translate job: the run's reads serve its envelope, one statement per write

Status: Accepted
Date: 2026-10-23

## Context

S4 (ADR 0093) brought a plain translate job to three statements per segment, but about 100 fixed
statements per job remained (the goal was under 60 per job). Measured with
`tests/verify_query_budget.py` (mock engine, the load harness's documents, three targets, a glossary
with a sense-bearing term) and grouped by call site with `StatementCounter(engine, stacks=True)`:
an HTML job (12 segments) cost 134 statements, a Markdown job (7 segments) 116.

The largest block was the Prompt Tap tail (about 30 per job). After the last segment, finalize and
enrich re-read the document, project and customer, rebuilt the glossary plan and re-read its pins,
entries and senses, re-resolved the style guide, memory add-on entries and attached TM sources, read the
leg's segments twice and the action's legs twice, and wrote each row with a read first. The enrich
also replayed glossary matching, which re-runs sense selection: one engine call (`complete_direct`,
task `choose_sense`) per sense-bearing segment after the run had already made it. On a paid engine
that is spend and a second send of the segment text, for an observability record.

## Decision

- **The run's read cache serves its envelope.** `translate.pretranslate_stream` owns the run cache
  (it used to live in the impl) and passes it to `tap_envelope.finalize_pair` and
  `tap_enrich.enrich`. The direct-MT leg's detail and manifest report what the run resolved: the
  glossary plan and pins, the matches the run made per source text (no replayed sense choice), the
  style guide, the in-scope memories and the attached TM sources. A segment the run did not match
  (a TM fill) is replayed exactly as before. Workflow legs pass no cache and are unchanged. On a busy
  install the manifest now names the assets the run used, not the ones in place a moment later.
- **Joined reads for row chains.** `run_cache.load_document_chain` reads document, project and
  customer in one statement (job handler, stream pin, enrich); the rows sit in the session's identity
  map, so the AI opt-out and formality lookups on that session read nothing more. Run and action,
  action and its legs, project and its stopped locales are one read each.
- **One statement per write.** Run status, run detail, action manifest (`WHERE manifest IS NULL`,
  the column stores None as SQL NULL) and `jobs._update` are single guarded UPDATEs. `_update` keeps
  the drain guard in the statement (`WHERE status = 'running'` with `if_running`), runs through a
  Session and returns False when no row matched (agreed with the S2 owner).
- **Once per request or run, not per language or per call.** The translate door reads the project's
  target locales, checks for source segments and sums the per-language source text once per request;
  the batched TM lookup and the TM few-shot pull resolve attached sources once
  (`tm_common.source_ids_memo`, a session-scoped memo that ends with the block); the memory add-on reads every
  in-scope kind once per run and each caller filters its kinds (same rows, same order: the kind
  filter moves from SQL to Python over the same customer-indexed scan). The finish notification takes
  the row the job hook just read.
- **A fix found on the way.** The memory add-on's manifest compared raw locales ("en-US") with recall's
  normalized ones, so it never listed a pair-scoped memory. It now normalizes like recall.
- **Guard.** `verify_query_budget` budgets drop to 110 (HTML) and 95 (Markdown), measured 98 and 83;
  the per-segment guard stays at 3.6 (measured 3.25). `verify_tap_enrichment` test S4F fails on the
  old tree (five sense choices for three segments, the pair-scoped memory missing).

## Result

HTML 134 -> 98, Markdown 116 -> 83 statements per job (suite measurement). Segments, TM rows, project
export and every Tap row are byte-identical before and after (fingerprinted with and without a style
guide, memories, an attached TM source and TM fills), except the manifest fix above. What remains per
HTML job (98):

| Block | Per job | Statements |
|---|---|---|
| Per segment (12) | 36 | opt-out guard at the engine call, human-work freshness read, segment UPDATE |
| Sense choice (fixture) | 3 | opt-out guard of each sense-choice engine call |
| Launch request | 17 | API key auth and last-used stamp (5) and two audit-chain rows (16) per request, about 10 per job; per language: review gate, seed check (2), target locale row, opt-out refusal at submit, job INSERT and its read back |
| Job lifecycle | 16 | claim read, engine-cap read, RUNNING, handler cost read, document chain, segment count, review gate, two progress writes, auto-complete (2), finish, hook read, notification and budget document reads, project budget |
| Translate setup | 15 | stream pin, review gate, context profile, style guide, segments, glossary pins, entries, senses, TM sources, TM lookups (3), memories, document format, first status read |
| Prompt Tap | 12 | action and run INSERTs, run status, action legs, leg segments, action status and totals, run and action, document chain, legacy glossary hash, TM signature, detail, manifest |

Not done (each needs a decision or another owner): the three human-review gates (launch, job, impl)
are kept; the audit chain and API key stamp are fail-closed security paths; the job claim and the
INSERT read-back are S2 queue internals. With the memory add-on's recall hitting, each segment costs about three
more (usage stamp read and write, then the segment reload after that commit): moving the stamp
needs care because it must not take the write lock across the engine call. Project create still bumps
the global settings version (left for a later decision, ADR 0093).

## Consequences

- A new read in the Tap tail of the direct path shows up in `verify_query_budget` at once.
- A caller that wants the envelope to re-resolve everything simply passes no `run_cache`.
