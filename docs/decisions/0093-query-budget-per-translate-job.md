# 0093. Query budget per translate job: read once per run, three statements per segment

Status: Accepted
Date: 2026-10-23

## Context

Engine redesign stage S4 ("Query diet"). A load profile issued about 35,700 SQL statements for 120 translate
jobs, and the S5 profile of a 1,902-segment job showed SQLAlchemy dominating CPU. Measured
(`POST /api/documents/{id}/translate`, mock engine, the load harness's `make_html` / `make_md` documents, three
targets, counted with a `before_cursor_execute` hook): an HTML job (12 segments) cost about 308 statements, a Markdown
job (7 segments) about 216; per segment about 15. Most of it was the same rows read again for every segment: the AI
opt-out guard read document, project and customer twice per engine call (run_completion's guard and the provider's own
complete() guard, three reads each); the glossary entries of the locale pair were read twice per segment (translate and
the Prompt Tap enrich replay); the optional memory add-on read the customer and the active memories per segment; the job row was read and
written per segment for progress; and each segment was re-read twice around its write. Above that, helpers re-read the
run's document, project and customer because SQLAlchemy's identity map only keeps rows that something still
references.

## Decision

- **Per segment: three statements, all deliberate.** The AI opt-out guard resolves fresh at every engine call (one
  joined document -> project -> customer read; `ai_optout._opt_out_lookup`); the human-work freshness read in
  `quality.commit_mt_output` (now the reload of the expired row itself, no second targeted read when the row was just
  re-loaded in the same call); the segment UPDATE, committed per segment for durability and resume.
- **One-shot guard hand-off.** `providers.run_completion` calls `ai_optout.guard(hand_off=True)`; the provider
  `complete()` wrapper (`wrap_callable(accept_hand_off=True)`) takes that answer once for the same scopes object within
  1 second instead of resolving again. Retries and failover attempts resolve afresh; a refusal is never handed off.
  The marker ends with the call: run_completion is wrapped so an unused marker is dropped however it exits (a spend
  block or a dead engine before any complete()), so no later engine call can take it (`verify_ai_opt_out`
  test L).
- **Per-run read caches.** `_pretranslate_stream_impl` owns a dict passed as `entry_cache` to
  `glossary_module.match_terms_merged` (glossary entries and senses) and as `recall_cache` to
  the memory add-on's recall (opt-out answer and active memories, only for id-stamping callers). Cached rows are
  detached snapshots (`app/services/run_cache.py`) so a commit never makes them re-load. The trade: an edit to a
  glossary or memory made while a run is translating reaches the next run, not the next segment; the run already took
  its glossary plan, style guide, context package and TM examples once at the start. The memory add-on's opt-out answer is
  cached with the memories, so a mid-run opt-out stops recall at the next run; the engine guard still refuses the
  very next engine call, so nothing is sent.
- **Pinned run rows.** `translate.pretranslate_stream` holds the run's document, project and customer for the stream,
  so later helpers on the same session use them (same transaction; a commit still expires and re-loads them).
- **Progress.** `jobs._run_translate_document` writes progress at most every `_PROGRESS_EVERY_S` (0.5 s), always for
  the last segment; each write still carries the exact count.
- **Small merges.** Context profile layers, style guide rungs and the project auto-complete check read in one
  statement each; the engine-slot look-ahead joins document and project; the job row is read once after execution.
- **Guard.** `tests/verify_query_budget.py` fails when an HTML or Markdown job exceeds its budget (150 / 130 per job,
  measured 134 / 116 in the suite, whose project has a glossary term on every bulk segment and a term with a sense)
  or when one more segment costs more than 3.6 statements (measured 3.32; one more read per segment gives about 4.3).

## Result

HTML job 308 -> 138, Markdown job 216 -> 112 (load-shaped measurement; byte-identical segments, TM rows and project
export, fingerprinted before and after). The target of under 60 per job is not met: the remaining fixed cost (about
100 per job) is one or two reads each across the job lifecycle (queue insert, claim, start, progress, finish, budget
and notification hooks: about 20), the Prompt Tap envelope and enrich observer (begin, finalize, manifest and detail
writes: about 25), the launch request (authz, locale seeding, audit: about 12) and translate setup (TM lookups, glossary
plan, memory note, style guide, context: about 20). Each is per job, not per segment, and is listed in the stage
report. Not done: orjson for JSON columns (not installed; a new dependency), and project create bumping the global
settings version (every process then drops its whole settings cache; a cross-process semantics change left for a later decision).

## Consequences

- A new per-segment read shows up in `verify_query_budget` at once (the marginal-cost check), whatever the document.
- A caller that wants the per-segment re-read behaviour simply does not pass a cache.
- `tests/_stmt_counter.py` is the diagnostic tool for the next diet: `StatementCounter(engine, stacks=True)` groups
  statements by call site.
