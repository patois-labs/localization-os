# Translation Pipeline: Architecture (L1)

## 1. Role

This subsystem is the **path a source document travels from upload to delivered file**:
ingest into a `Document` plus `Segment` rows, machine translation through a provider, the
run engine that walks a workflow's steps and applies the quality gates, the downstream
refusal guard that catches a model talking instead of translating, and the delivery layer
that assembles the translated files per locale. It is the product's core value path.

Ingest (`app/services/ingest.py`) parses an uploaded file into segments (one per sentence
in segmented mode, or one segment holding the whole body in full-text mode) and records a
chunk plan and the original bytes for format-preserving export. Machine translation
(`app/services/translate.py`) is the single-engine pre-translate path; the run engine
(`app/services/orchestrator.py`) is the multi-step workflow executor that most runs go
through, streaming step results, applying the loop-safety guard, and holding the run's
spend accounting. The refusal guard (`app/services/refusal.py`) is the across-the-board net
that downgrades a suspected non-translation to review. Delivery (`app/services/delivery.py`)
assembles the per-locale files, forcing the locale into both the path and the filename so a
file can never overwrite.

It owns the run and its per-segment results, not the workflow graph it executes
(workflow-engine defines the steps and the batch fan-out), the TM and glossary lookups a
step consults (tm-and-glossary), the quality lenses and LQA scoring beyond the refusal net
(quality-lqa-evaluations), or the connector write-back that posts a delivered unit to an
external system (connectors). Its two direct surfaces are the document upload/export web
routes (`app/web/documents.py`) and the `doc` CLI group (`app/cli/cmd_doc.py`); the run and
batch triggers live on the workflow routes (workflow-engine).

## 2. Invariants

- **HTML export checks stored paragraph positions against the current extraction.**
  Older uploads containing a raw `>` inside a quoted attribute may have extracted part
  of that attribute as body text. Export refuses those stale positions with a re-import
  message on the web, API, and CLI delivery paths. Re-import the original HTML and review
  its translations before exporting. Unchanged accessibility attributes retain their
  original bytes; translated attributes and body text are escaped on write-back.
- **The refusal guard only ever downgrades, never rewrites or drops good text.**
  `app/services/refusal.py::is_refusal` matches a produced output (never the source) against
  a conservative, mostly leading-anchored marker set and returns False when in doubt. The
  verdict's write-back decision is made at exactly one place,
  `app/services/quality.py::commit_mt_output` (the single sanctioned `target_text` writer,
  reached from the pre-translate path through `app/services/translate_hub.py::_apply_result` /
  `app/services/translate_hub.py::_apply_result_ft` and from the orchestrator write-back): on a
  True verdict it does not write `target_text`, sets the segment to needs-review, and merges a
  refusal `qa_flag` rather than committing. The same verdict is separately consulted at four
  other seams, none of which writes `target_text`. Three are proven by the cited choke-point
  suite: the `app/services/tm.py::import_tmx` gate skips a hedged target rather than storing it,
  `app/services/tm_module.py::scrub_hedges` removes hedges already in the TM stores, and
  `app/services/connectors/flow.py`'s stage-9 title gap keeps a needs-review title off the
  transport by consulting `SegStatus.NEEDS_REVIEW` one step downstream of the verdict. The
  fourth seam is the orchestrator's own four per-step `refusal.is_refusal` guards, which act
  before `commit_mt_output` is ever reached and are pinned separately by `verify_refusal_guard`:
  three of them withhold the `StepRun` output and mark the unit refused (one of those also
  keeps the hedge out of the feedback buffer), while the first leaves the hedge as the step
  output for the downstream `step_refused` path to claim. The thin `app/services/translate.py::pretranslate_document`
  wrapper does no refusal handling of its own. Enforced by `verify_refusal_guard` (the live-Jira
  detection, the ten reviewer false-positives left through, the route-to-review write-back, and
  the orchestrator segmented and full-text write-back guards, whose segmented refusal arm asserts
  `rows[1].status == SegStatus.NEEDS_REVIEW` with no `target_text` and whose full-text arm asserts
  the status) and
  `verify_refusal_choke_point` (the write-back decision at the one seam, plus the import_tmx,
  scrub_hedges seams that consult the same verdict, and the flow-title seam that reads the
  resulting review status one step downstream).
- **A held (all-untranslated) locale is never delivered, only skipped and reported.**
  `app/services/delivery.py::export_project` skips such a locale from the bundle and surfaces
  it rather than shipping source text as if translated. Enforced by
  `verify_export_held_locale` (the service seam and the bundle skip-and-report arm).
- **A priced provider call accrues spend without deadlocking.** The orchestrator's spend
  write opens its own DB session so `app/services/orchestrator.py::run_workflow_stream`
  completes and records ledger spend rather than blocking on the writer lock. Enforced by
  `verify_orchestrator_spend_lock`.
- **A plain workflow runs byte-identically to the pre-graph engine.** The run guard is inert
  when no step carries a branch or `max_visits` (`app/services/orchestrator_part2.py::_has_graph`
  is False), so a straight-through run writes no run context and skips nothing. Enforced by
  `verify_condwf_phase3` (the byte-stable-when-unused arm).
- **A diverging loop fails cleanly, never spins forever.** A run that keeps looping back
  trips the run-level total-step cap and fails with reason `loop_cap_reached` rather than
  running unbounded. Enforced by `verify_condwf_phase3` (the total-step hard-cap arm).
- **Cancelling a run is safe and idempotent-ish.** `app/services/orchestrator_part6.py::cancel_run`
  transitions a paused or running run to cancelled with a note (closing an open external
  vendor leg), and raises `app/services/orchestrator_part4.py::RunCancelError` on a missing or
  already-terminal run rather than crashing. Enforced by `verify_run_cancel`.
- **A run this install cannot carry is refused at the door, not admitted and wedged.** Each
  in-flight execution pins one pooled database connection for its whole duration, so
  `app/services/orchestrator.py::start_run` reserves a slot from
  `app/services/orchestrator.py::RUN_SLOTS` BEFORE it writes anything and raises
  `app/services/orchestrator.py::RunCapacityError` past `app/config.py::MAX_CONCURRENT_RUNS`
  - a retryable 503 with a named reason on every surface, and no run row left behind.
  **Capacity decides WHEN work runs, never WHETHER it may.** A fan-out (the key project's
  "translate all", all surfaces) persists every planned run
  through `app/services/orchestrator.py::queue_runs` before execution. The single
  `app/services/orchestrator.py::_AdmissionScheduler` admits their durable queued rows
  as slots free, rotating across fan-outs and reserving a share for interactive launches.
  Batches and their re-runs use the same scheduler and retain their per-batch width,
  spend and fail-fast gates. Fan-outs queue even when no slot is free. A
  re-attach to a run already in flight is never refused and consumes no slot. Ownership of a
  reservation moves exactly once (`app/services/orchestrator.py::_SlotToken`, held for one
  launch iteration by `app/services/orchestrator.py::fanout_slot`), so neither a failed
  thread start, nor a crashed run, nor a `WriteContentionError` out of a fan-out's launch can
  double-release it or leak it. What is NOT bounded, and says so: an Advanced mission's
  parallel delegate dispatch runs several pooled sessions under one slot (GitHub #109).
  Enforced by `verify_stress_liveness_pool`.

- **One run per document + locale at a time, whatever the workflow.** The double-submit
  guard in `app/services/orchestrator.py::start_run` RE-ATTACHES a launch of the exact
  (workflow, document, locale) already in flight. A run of a DIFFERENT workflow on the
  same (document, locale) has nothing to re-attach to and would write back over the same
  segment set, so every launch path refuses it: the check sits in the same
  `app/services/orchestrator_part2.py::_RUN_CREATE_LOCK` transaction as the create -
  including the synchronous `app/services/orchestrator.py::run_workflow` arm, whose row
  is pre-created in that same critical section (the batch-member pattern) - and raises
  `app/services/orchestrator.py::RunConflictError` (GitHub #213 part 2) before any row is
  written - 409 `{"error": ..., "code": "run_conflict", "run_id": N}` on /api and MCP,
  the themed page on the web, a non-zero CLI exit with the same sentence (local and
  `--remote`). The lock is process-local: a separate local-CLI process beside the server
  is not serialized by it (the pre-existing limit, shared by every guard here). The batch
  dispatcher keeps its skip semantics: a busy locale is skipped and reported ("already
  running elsewhere"), never launched as a rival member; the key fan-out reports the
  conflicting pair on every surface (web flash, `conflicts` in the /api body, both CLI
  arms, MCP). Enforced by `verify_run_recovery_progress`,
  `verify_api_workflow_run_async` and `verify_batch_runs`.

- **A delivered file declares the target language, and alt text units never shift body
  units.** `app/services/export.py::build_translated_file` calls `build_localized`, which each
  filter overrides only where its format carries a language declaration (HTML `lang`/`dir`,
  DOCX `w:lang`/`w:bidi`/`dc:language`, PDF `/Lang` via typst, XLSX `dc:language`, XLIFF
  `target-language`/`trgLang`, PO `Language`); `build` itself is unchanged, so the round-trip
  ratchets hold. Alt text units (`a11y:*` style) are APPENDED after every body unit, so a
  document ingested before they existed exports with its alt text untouched instead of
  misaligned. Enforced by `verify_output_a11y`.

- **A segment too long for the engine's output limit is translated in pieces, never failed
  and never cut off (#683).** Both per-segment MT paths (the plain pre-translate stream and
  the workflow segmented step) ask `app/services/mt_chunking.py::Planner` before the engine
  call whether the segment's generously estimated output fits the engine's budget (its
  `app/config.py::model_output_ceiling` times `MT_CHUNK_OUTPUT_FILL`, the smaller of primary
  and fallback; input within `MAX_CHUNK_TOKENS`). If not, it is cut at sentence ends, then
  clause punctuation, then spaces, never inside a protected token or a PII value, translated
  piece by piece and rejoined with the #668 seam rules (`app/services/segment.py::join_unit`);
  the stored segment stays one segment and `Segment.meta["mt_chunks"]` records the piece
  count. Any piece error fails the whole segment with no partial text. A segment no safe cut
  can fit is not sent: it goes to review under `ENGINE_LIMIT_REASON_PREFIX` (the
  over-translation withhold channel on the workflow path), and the job or run carries on. An
  engine answer cut by its output limit (finish reason `length`, `max_tokens`,
  `max_output_tokens` and the other spellings in
  `app/services/providers_common.py::is_length_finish`) is a truncation, never a
  translation. FULL_TEXT keeps its own paragraph chunker. Enforced by
  `verify_mt_chunking_683`.

- **A segment with no translatable text is copied on every path, never sent to an engine
  (#702, #705).** Maintainer decision (2026-10-11): tags-only segments are copied to the
  target and delivered, across the board. The one predicate is
  `app/services/tags_only.py::is_tags_only` (inline `⟦..⟧` tags plus at most ONE placeholder
  such as `{name}`, `{{count}}`, `%s`, `%1$d`, `%{name}`, `%(name)s`, `${name}`, with only
  whitespace around them; any letter, digit, punctuation or raw markup keeps the engine path,
  and so do two placeholders, whose order can be the translation: `{first} {last}` is
  `{last} {first}` in Japanese) and the one
  action is `app/services/tags_only.py::commit_copy` (the source written through the
  `quality.commit_mt_output` seam, status MT, never TM-eligible, stale step-error and gate-hold
  badges cleared). Callers: the plain pre-translate stream (`tags_only` in the job summary;
  the job also reports `0/N` before the first segment); the workflow segmented run, where such
  a segment is never in any step's pending set (no engine, LLM, QE or fixer call, no spend, no
  feedback seed), counts as step 0's delivery in `output_chars` like a TM fill, is committed by
  the copy action at write-back, is never gate-held, and is counted as `tags_only` on the run
  context and the `done` event (keys translate, connector and image paths all reach the
  workflow kernel or the pre-translate stream); `orchestrator_part8._run_step_on_segment`
  (defense in depth, also the Evaluations Lab's direct caller); `eval_runner.produce`;
  `translate_hub.translate_text_once` (`/api/translate/text` and the MCP tool); the editor's
  alternates route; the Advanced Workflows conductor, which stages the copy without any
  delegate or lens judge, and its delivery gate, which admits that copy unjudged. The context
  and style A/B experiments never sample such a unit. QA (`qa.check_segment`: editor, LQA
  structural errors, the quality lens' `untranslated` check) does not call the copy
  untranslated, and an LQA judge's identical-to-source finding on it is advisory like a
  numeral-only one (#345). Not covered: a FULL_TEXT body (one unit for the whole document)
  and the workflow LQA judge's whole-document blob, which still carries the copies. Enforced
  by `verify_tags_only_mt_702` and `verify_tags_only_everywhere_705`.

## 3. Where the work happens

| Concern | Pin | One line |
|---|---|---|
| Ingest | `app/services/ingest.py::ingest_document` | Parses an upload into a `Document` + `Segment` rows (segmented or full-text) and records the chunk plan and original bytes. |
| Ingest caps for non-upload paths | `app/services/seed_caps.py::seed_error` · `app/services/seed_caps.py::add_in_chunks` | The #571 limits (`MAX_DOCUMENT_SEGMENTS`, `MAX_DOCUMENT_ROWS`, `MAX_SEGMENT_CHARS`) for connector pulls and seeders, key-file imports, TM/glossary imports and eval datasets, plus committed-chunk writes (#576). One connector run's fetched records also stop at `CONNECTOR_RUN_CHAR_BUDGET` characters in total (`connectors.base.run_char_budget`, #587). |
| Add-a-language seeding | `app/services/locale_seed.py::seed_locale` | Clones the source rows into a new target locale: row cap, committed chunks, and a resumable marker on `Document.skeleton` so a crash is finished by the next call (#576). |
| Machine translation | `app/services/translate.py::pretranslate_document` · `app/services/translate.py::pretranslate_stream` · `app/services/translate_hub.py::_apply_result` · `app/services/translate_common.py::_logger` · `app/services/translate_hub2.py::_pretranslate_stream_impl` | The single-engine pre-translate path; `_apply_result` lands each result through the `quality.commit_mt_output` seam that applies the refusal verdict. |
| No-text segments | `app/services/tags_only.py::is_tags_only` · `app/services/tags_only.py::commit_copy` | The one predicate and the one copy action every translate, workflow, Advanced and API path uses for a segment that is only tags or placeholders (#702, #705). |
| Overlong segments | `app/services/mt_chunking.py::Planner` · `app/services/mt_chunking.py::complete_pieces` · `app/services/mt_chunking.py::route_unfit` | Plans pieces for a segment whose translation would not fit the engine's output budget, translates and rejoins them, and routes an unsplittable one to review (#683). |
| FULL_TEXT TM offers | `app/services/fulltext_tm_offers.py::render_offers` | Renders approved sentence offers beside the source; unverified, not guaranteed. |
| Run engine | `app/services/orchestrator.py::start_run` · `app/services/orchestrator.py::run_workflow_stream` · `app/services/orchestrator_part6.py::cancel_run` · `app/services/orchestrator_terminology_backen.py::_mark_project_completed` · `app/services/orchestrator_part2.py::_notify_human_review_paused` · `app/services/orchestrator_part3.py::_is_correction_loop_step` · `app/services/orchestrator_part4.py::RunGuard` · `app/services/orchestrator_part5.py::_system_for` · `app/services/orchestrator_part7.py::_run_parallel_group` · `app/services/orchestrator_part8.py::_gate_release_mask_in_scope` · `app/services/orchestrator_part9.py::_execute_step` · `app/services/orchestrator_part10.py::_gate_release_mask` | Starts and streams a workflow run over the document's segments, applies the loop guard, and cancels a run. |
| Run admission | `app/services/orchestrator.py::RUN_SLOTS` · `app/services/orchestrator.py::_SlotToken` · `app/services/orchestrator.py::RunCapacityError` · `app/services/orchestrator_part2.py::_bookkeeping_session` | The in-flight execution count, the single-owner reservation handed to an execution, the refusal a launch beyond capacity raises, and the pool-less session that records why a run died when the pool has nothing left. |
| Refusal guard | `app/services/refusal.py::is_refusal` · `app/services/refusal.py::strip_meta_preamble` · `app/services/refusal.py::REASON` · `app/services/refusal_common.py::_FENCE_NAME_LIST` · `app/services/refusal_part2.py::_strip_injected_fences` | The conservative non-translation detector and the meta-preamble stripper, gated by a runtime kill switch. |
| Output guard | `app/services/output_guard.py::check_output` · `app/services/output_guard.py::instruction_echo` · `app/services/output_guard.py::short_source_blowup` · `app/services/output_guard.py::number_mismatch` | Deterministic per-step checks on SEGMENTED llm_agent output only (a FULL_TEXT document step is not guarded): an echoed step instruction (the content-fence sentence counts only as a run of 10 or more consecutive words, #353) or a blown-up short source is quarantined like a refusal; a dropped or added number (locale-aware, `qa.number_findings` count) keeps the text and flags the segment for review (`NUMBER_MISMATCH_WITHHOLDS`). Same kill switch as the refusal guard. |
| Delivery | `app/services/delivery.py::export_project` · `app/services/delivery.py::file_name_for` | Assembles the per-locale bundle, forcing the locale into both the path and the filename. |
| Bilingual file target language | `app/services/bilingual_target.py::declared_targets` · `app/services/bilingual_target.py::plan_ingest` · `app/services/export.py::file_target_refusal` | #687 (decision 6, 2026-10-11): an SDLXLIFF, memoQ/Phrase XLIFF, Wordfast TXLF/TXML, TTX, Trados package member or XLIFF member of a memoQ MQXLZ / WorldServer XLZ / WSXZ package (#759) keeps its own target language on export, so ingest seeds it only for the project target(s) it declares (stamped as `file_target_locales`, with a `file_warnings` entry), refuses it when none match (`FileTargetMismatch`, 422 on the API), and `locale_seed`, export and the project bundle never put another language under it (documents from before the rule are checked from the file). |
| Trados packages | `app/services/sdl_package.py::ingest_package` · `app/services/sdl_package.py::build_return_package` · `app/services/sdl_package.py::create_project_from_package` | Turns an uploaded .sdlppx into one document per target SDLXLIFF (called from the ingest chokepoint), creates a project from a package (web new-project form, POST /api/documents sdl_new_project=1) and merges a package's documents into one .sdlrpx return package (called from delivery). |
| Web surface | `app/web/documents.py` · `app/web/documents_tm_only_delivery_empty_g.py::upload_document` · `app/web/documents_tm_only_delivery_empty_g.py::export_document` · `app/web/documents_document_level_context_o.py::_logger` · `app/web/documents_part3.py::segment_save` | The document upload and per-document export/download routes. |
| CLI surface | `app/cli/cmd_doc.py::register`, `app/cli/doc_common.py::_get_doc`, `app/cli/doc_inspect.py::_doc_show` | The headless `doc` command group: lifecycle registration, shared document resolution, and segment inspection / paragraph repair. |
| File export round-trip | `app/services/export.py::build_translated_file` · `app/services/export_gate.py::held_locale_reason` · `app/services/export_gate.py::assert_deliverable` | `app/services/export.py` rebuilds the source file with translations injected and checks tag damage; `app/services/export_gate.py` owns delivery gating and quarantine exceptions. |
| Output accessibility | `app/filters/a11y.py::target_tag` · `app/filters/base.py::Filter` · `app/filters/a11y.py::alt_prompt_flags` · `app/services/ingest.py::_a11y_alt_flags` | `build_localized` (the export path) is `build` plus the TARGET language declaration per format; image alt text is extracted as appended `a11y:*` units (and, after those, page metadata as `meta:*` units: HTML `<meta>` description, keywords, og:/twitter: title and description, AsciiDoc header `:description:` / `:keywords:`, rST meta directive description / keywords, #733; a full-text document leaves both out); a missing or empty source alt becomes an Editor warning on the nearest segment. |
| FULL_TEXT recovery | `app/services/fulltext_paragraphs.py::split_view` · `app/services/fulltext_paragraphs.py::save_paragraph` · `app/services/fulltext_paragraphs.py::revert_all_damaged` | The per-paragraph view and damaged-unit recovery for a FULL_TEXT document. |
| TM-only delivery gate | `app/services/tm_delivery.py::empty_gate_decision` · `app/services/tm_delivery.py::approve_doc` · `app/services/tm_delivery_the_three_valued_gate_de.py::revoke_doc` | The live companion predicate that holds delivery of a deliverable still carrying empty (untranslated) targets until an audited human approval. |

## 4. Data & settings

- **Models:** `app/models.py::Document` (the ingested file, its type and mode) and
  `app/models.py::Segment` (a unit of source and target text, its `status`, and its
  `qa_flags`); the run rows (`WorkflowRun` / `StepRun`) are shared with workflow-engine.
- **Settings:** `app/config.py::REFUSAL_GUARD_ENABLED` (env-only, default on) is the runtime
  kill switch that restores byte-identical pre-guard behavior. `setting:mt_concurrency`
  (default `"4"`) bounds the pre-translate worker pool; `setting:spend_cap_usd` (default
  `"0"`, off) is the ledger spend cap the provider seam checks; `setting:workflow_max_visits`
  (default `"5"`) and `setting:workflow_total_step_cap` (default `"50"`) are the loop-safety
  caps the run guard enforces at execution time. `app/config.py::MT_CHUNK_OUTPUT_FILL`
  (env-only, default 0.5, 0 turns it off) is the share of the engine's output ceiling one
  request may be planned to use (#683).

## 5. Enforced by

- `verify_refusal_guard`: the conservative refusal detection, the reviewer false-positive
  set, the kill switch, and the route-to-review write-back.
- `verify_refusal_choke_point`: that the refusal verdict sits at the single write-back choke
  point rather than being re-implemented per caller.
- `verify_orchestrator_spend_lock`: that a priced provider run completes and accrues spend
  without deadlocking on the writer lock.
- `verify_stress_liveness_pool`: that a launch beyond the admission ceiling is refused before
  any row is written, that a re-attach is never refused and consumes no slot, that the slot
  comes back when the run ends, and that a run whose execution dies on a connection-pool
  timeout lands FAILED carrying `app/db.py::POOL_EXHAUSTED_NOTE` rather than sitting RUNNING
  at step 0 with no reason.
- `verify_condwf_phase3`: the run guard's byte-stable inert path, the soft per-step visit
  cap, and the hard total-step cap.
- `verify_run_cancel`: run cancellation from paused and running states, the terminal refusal,
  and the closed vendor leg.
- `verify_export_held_locale`: that a held locale is refused at export and skipped-and-reported
  in a bundle.
- `verify_fulltext_recovery`: full-text ingest and the damaged-unit recovery seam.
- `verify_output_a11y`: per-format target language on export, alt text extraction and
  write-back, the missing-alt Editor prompt, and byte-identical `build` for untouched files.
- `verify_mt_chunking_683`: an overlong segment is translated in pieces and rejoined on the
  job and workflow paths, an unsplittable one goes to review, and an output-limit truncation
  is never saved.
- `verify_tags_only_mt_702`: a tags-only segment is copied without an engine call and the
  translate job reports its total before the first segment.
- `verify_tags_only_everywhere_705`: a tags-only or placeholder-only segment is copied without
  an engine call on the workflow (two steps, gate hold), keys, step-engine, Evaluations Lab,
  text front door, alternates and Advanced Workflows paths, and the LQA identical-to-source
  finding on the copy is advisory.

## 6. Battle scars

- **A cheap model once posted a hedge to real Jira instead of a translation.** In live
  testing a haiku-tier model handed an ambiguous label-like field answered conversationally
  and the pipeline wrote that verbatim to the target and posted it; the refusal guard is the
  across-the-board net added so any path that still emits a non-translation is downgraded to
  review rather than delivered, recorded in the `app/services/refusal.py` module docstring.
- **The loop-safety caps came from conditional workflows, off by default for plain graphs.**
  The run guard stays inert unless a step carries a branch or `max_visits`, so the soft
  per-step and hard total-step caps back-stop only a graph that can loop.
- **A refused web export is a page, not raw JSON.** A browser navigation (`Accept: text/html`) to
  the document export that a delivery check refuses (quality-gate hold, tag quarantine, empty-target
  hold, held locale, missing original) gets a 409 page naming the problem and the first ten affected
  items, with Review and View links and Export anyway only where the API offers it; the
  non-bypassable gate hold never does. Every other client keeps the exact JSON body
  (`app/web/documents_tm_only_delivery_empty_g.py::_export_refused_page`, #593). Enforced by
  `verify_export_refusal_page`.
- **A held locale must never masquerade as delivered.** Delivery, the web export, the /api
  download, and the CLI export all funnel through one held-locale seam
  (`app/services/export_gate.py::held_locale_reason`) so an all-untranslated locale is refused
  everywhere rather than shipping source text, proven across all four surfaces by
  `verify_export_held_locale`.

## 7. Limits & accepted risks

- XML text exports normalize vertical tabs/form feeds to newlines and drop XML 1.0
  illegal characters at the DOCX, XLSX and XLIFF filter boundary. Stored text is unchanged.
  DOCX body and alt write failures identify the unit through the shared export error path.
  Other ValueErrors in DOCX, XLSX and XLIFF rebuilds receive a neutral failure message
  and a logged traceback. Non-XML filter ValueErrors propagate normally.
- PO rebuild preserves indexed plural slots and clears fuzzy/previous-string comments
  only when a mapping supplies a target. Export passes source-fallback unit keys to PO;
  those units retain their original msgstr and comments, including fuzzy and previous msgid.
  Localized output updates an existing Language
  header together with Babel's gettext Plural-Forms rule and slot count. Every slot of the
  target's rule is filled (#729): the singular translation in the slot the rule picks for
  n = 1 (Arabic slot 1), the plural translation in every other slot; same-language region
  changes retain existing extra forms. The same plural-fill rule (app/filters/plurals.py)
  adds the target's missing CLDR categories, filled with the translated plural, to Android,
  Apple, ARB/ICU, go-i18n, i18next JSON keys, Qt .ts numerus forms, Rails YAML plural maps
  and Fluent plural selects on localized export.
  A missing Language header or locale absent from Babel's gettext rule table leaves
  declarations unchanged; the two source units per plural entry retain their stable indices.
- DOCX ingest stores an offline external-resource inventory in skeleton.file_warnings.
  The document page, upload/inspection API and local/remote CLI surface it; MCP inspection
  passes through the API result. Non-hyperlink external relationships and external-loading
  fields (INCLUDEPICTURE, INCLUDETEXT, LINK, DDE, DDEAUTO) are flagged unconditionally,
  including local paths, //host paths and nested field targets. XML parts are selected
  from content-type defaults/overrides, with case-insensitive .rels matching.
  Per-part read/parse failures produce docx_unscanned_part warnings naming the part
  and do not block ingest. The scanner caps each inflated part at 4 MiB and total
  inflated scan bytes at 16 MiB; skipped parts receive the same warning. XML entity
  resolution and network access remain disabled. No URL is fetched or
  rendered as a link. Source and translated packages retain the resources for fidelity;
  stripping is not offered. Older documents require re-upload for an inventory.
- DOCX table walk version (#349). Units are addressed by p_index and export re-walks the
  original file, so the walk a project was imported under must be the walk it is exported
  under. New parses stamp `skeleton.docx_walk = 5`. Stamp 3 is the XML-level walk: each
  physical cell once, linear, merged text yielded once; #355 adds tables nested in cells and rows, cells, tables
  and paragraphs inside `w:sdt` content controls or `w:customXml`; a nested table is walked
  depth-first in reading order inside its cell; nesting capped at 32 levels. Stamp 3 is
  true reading order: body paragraphs and tables are numbered interleaved exactly as they sit
  in the body (also inside body-level `w:sdt` / `w:customXml`), where stamp 2 and the legacy
  walk number all body paragraphs first and then all tables.
  #690: `parse(comments=False)` (the project's "translate comments" option off) leaves the
  comments part's units out without changing the walk or any index; True is the default and
  the pre-#690 behaviour. The other Office filters (xlsx, pptx, odt/ods/odp/odg, vsdx) append
  comment units at fixed indices `app/filters/office_comments.py::COMMENT_BASE` + j only when
  `comments=True`; their build() writes any comment key present, so no stamp is needed. The
  option itself is `app/services/comment_option.py` (default off for new projects, Word on
  for projects that predate it); ingest passes it to filters that set `takes_comments`.
  Stamp 4 (#270 r1-files-06) numbers the main body exactly as stamp 3 does, then the other
  text parts the document relates to: headers, footers, footnotes, endnotes and comments, in
  that order and each kind by the natural order of its part name (header2 before header10;
  the separator and continuation notes carry no text and are skipped). A text box's
  paragraphs (`w:txbxContent`, in a drawing or a VML shape, in the body or in any of those
  parts) are units right after the paragraph the box is anchored in, nested boxes after
  theirs (capped at 8 levels). The Choice and Fallback copies Word writes in
  `mc:AlternateContent` are one text box: it is extracted once and the translation is written
  to both copies when they have the same structure; a stale or differently shaped copy is
  left as it was. A tracked insertion (`w:ins`) and the destination of a tracked move
  (`w:moveTo`) are text as Word shows it and are rebuilt inside their wrapper (author, date
  and id kept; when a reordered translation splits one into several pieces, each extra piece
  gets a fresh document-unique id); a tracked deletion and a move source (`w:moveFrom`) are
  not extracted and are never touched. An insertion nested in a hyperlink, field, smart tag
  or inline content control is carried as it was and the warning names it. Each skeleton unit records the `part` it was read from, and export writes
  it back there. The stamp-3 skeleton and the stamp-2 skeleton keep their own walks
  (`_v3_paragraphs`, `_v2_paragraphs`) at export, so no extra part, text box or tracked
  insertion is touched for them; an unknown stamp is refused with a re-import message (409).
  Stamp 5 (#368) keeps stamp 4's paragraph numbering and changes two things. The text of a
  `w:smartTag` that sits directly in a paragraph is text like a hyperlink's: it is part of the
  unit and the translation is written back inside a re-created tag (attributes and
  `w:smartTagPr` kept); a smart tag nested in another smart tag, hyperlink or field is still
  carried as it was. The alt text (`descr`, `title`) of drawings in the headers, footers,
  footnotes, endnotes and comments is extracted as `a11y:*` units after the body's alt units, in
  the part order above, and the unit records its `part`. Stamps 4 and 3 keep exporting the
  body's alt text only and leave smart tag text alone. The result of a `w:fldSimple` is NOT
  translated: Word regenerates it whenever the field updates, and the field is its own wrapper,
  so a translation that splits the result would re-create the field once per piece (each copy
  shows the whole result after an update), which a split smart tag or insertion never does. A
  complex field (`w:fldChar`) keeps its one begin/separate/end, so its result runs stay text as
  before. The warning names a carried simple field result instead (not for fields Word
  recomputes itself: PAGE, NUMPAGES, DATE and similar, nor a result with no letters).
  A content control bound to custom XML data (`w:dataBinding`) that holds text gets its own
  `docx_data_bound_controls` warning per walked part: it is translated, but Word can refill it
  from the source data when the file opens. Extraction is unchanged.
  Stamp 6 (#686) keeps stamp 5's paragraph numbering and lets inline wrappers NEST. Word
  nests smart tags (`place` > `City`), and puts smart tags and hyperlinks inside tracked
  insertions; under stamp 5 such a wrapper was one object and its words stayed in the source.
  Stamp 6 reads runs through any chain of `w:hyperlink`, `w:ins`, `w:moveTo`, `w:smartTag`,
  inline `w:customXml`, `w:dir`, `w:bdo` and text content controls (`w:sdt` inside a
  paragraph whose `w:sdtPr` names no list, date, check box, picture, gallery, citation,
  equation or group kind), up to 32 levels (`MAX_INLINE_DEPTH`, iterative). Each atom records
  its wrapper chain; export re-creates the chain around the translation (attributes and the
  `w:smartTagPr` / `w:customXmlPr` / `w:sdtPr` / `w:sdtEndPr` kept), reusing the wrappers the
  next piece shares with the open chain, so a translation in source order comes back with the
  same wrapper tree. A split content control keeps its `w:id` on the first piece only; a split
  tracked change gets fresh ids as before. A list, date or check box control, a wrapper inside
  a field or a deletion, and anything deeper than the bound stay one object. Projects stamped
  5 and older export on their own walk, byte-identical.
  The untranslated-regions warning in file_warnings follows the walk: in the main part, the
  headers, footers, notes and comments it lists only content controls, tracked insertions and
  smart tags that stamp 6 does not read (`docx_filter.walk6_reads`), simple field results with words
  and tables nested past the cap; any other part with text (the glossary) keeps the whole-part
  rule. A skeleton with no stamp predates #349 and is rebuilt
  with the legacy `row.cells` walk (`docx_filter._legacy_paragraphs`, original numbering
  kept); `export.build_translated_file` picks the walker from the stored skeleton, and the
  repair re-ingest moves a legacy project to the new numbering and stamp together and
  recomputes `file_warnings`, `a11y_images`, the missing-alt flags and the accessibility
  notes from the fresh parse, as a new upload would (a full-text document leaves the alt
  units out, as at upload; this holds for every format). Any
  future change to the numbering must bump the stamp, never renumber silently.

- **A document in a key-based project is refused at ingest, by design.**
  `app/services/ingest.py::ingest_document` raises rather than accept a document into a
  key-based project because the two project types are disjoint and immutable; this is a
  design guard stated in the `NotADocumentProject` docstring, not separately pinned to a
  suite.
- **The refusal guard is conservative and can miss a novel hedge.** It matches a curated
  marker set and returns False when in doubt, so a non-translation phrased outside those
  markers passes through; the marker set is tuned against real content to avoid downgrading
  good text, stated in the `app/services/refusal.py` module docstring.
- **Output accessibility is partial by format, and the PDF is not PDF/UA.** HTML fragments
  get no declaration and inner `lang` attributes are left alone; DOCX headers, footers,
  tables and sections are not re-directioned; PPTX is not a supported format; the rebuilt PDF
  is tagged and declares its language but drops source headings and images, so it claims no
  PDF/UA conformance. Stated in `app/filters/a11y.py` and the help page's output
  accessibility section.
- **Fail-fast and cancel cannot kill work mid-step.** A cancel closes the run and its open
  legs but a provider call already in flight is not interrupted, stated in
  `app/services/orchestrator.py`.

## FULL_TEXT TM offers (Train 1)

FULL_TEXT workflows accept tm_match by default. Exact (100%) sentence matches from the step's selected readable TM pools are OFFERED, unverified, beside the glossary; {tm_offers} controls placement. The source body is unchanged. This is an offer, not a guarantee. If a string must be exact, FULL_TEXT is the wrong mode. The run summary, GET /api/runs/{id}, MCP work_status and workflow show-run expose tm_offers with sentences_examined, matches_offered and message after a FULL_TEXT tm_match step. These describe the latest offer list, not applied translations. SSE tm_match carries the counts for that step visit. A SEGMENTED workflow's tm_match step also leaves a tally on the run: the run page, GET /api/runs/{id} (tm_match), MCP work_status and workflow show-run report filled (genuine TM fills), relocked (already held the exact TM text), unmatched (not taken from the TM at the step's fill threshold, so sent on to the next step) and total segments. Fuzzy suggestions below the fill threshold are not counted. The helper app/services/fulltext_tm_offers.py renders the optional block. Required renderings and per-StepRun counts persist in WorkflowRun.context. No verification or context-budget bound is implemented in Train 1.

Every later llm_agent step receives the TM offer block, except steps that redact PII. The tm_match step honors tm_pool_selection within the readable pools; an empty selection offers nothing.

## llm_agent segment fence

The segment text is automatically wrapped in <content> markers unless the template already wraps the placeholder itself (for example <text>{text}</text> or a code fence), which is how an author opts out.

## Document filters and input limits

The format layer under ingest and export is `app/filters/`. A filter does two things: `parse` turns
uploaded bytes into paragraph-level `TextUnit`s plus a skeleton, and `build` re-injects translations
into that skeleton to rebuild the file. Segmentation into sentences stays above this layer, so filters
stay format-specific. Each filter module registers itself with `app/filters/base.py::register`, which
maps its format name and any extra extensions to one instance, and `app/filters/base.py::get_filter`
resolves a format or extension to it. The registered formats are Haiku catkeys, Freedesktop desktop, DOCX, HTML (`htm`, `xhtml`),
image (`svg`, `png`, `jpg`, `jpeg`, `tif`, `tiff`, `bmp`, `gif`, `webp`, sniffed from the bytes), JSON,
JSON5 (`jsonc`), Markdown, MDX, PDF, PO (`pot`), SRT (`vtt`), plain text, Xcode localization catalog (`xcloc`), XLIFF (`xlf`),
XLSX and YAML (`yml`).

| Concern | Pin | One line |
|---|---|---|
| Filter contract and limits | `app/filters/base.py::Filter` · `app/filters/base.py::TextUnit` · `app/filters/base.py::InputLimitError` · `app/filters/base.py::check_segment_cap` | The parse and build interface, the unit type, and the one refusal type every cap raises (mapped to 413 on every door). |
| Lossless text | `app/filters/base.py::decode_text_lossless` · `app/filters/base.py::encode_text_lossless` · `app/filters/base.py::read_text_upload` | UTF-8, then UTF-16 with a BOM, then the declared charset, then the Windows code page of the project's SOURCE language (Windows-1252 for Western languages or one without a code page), so a legacy-encoded upload converts instead of failing; only an unreadable file is refused. Translations export as UTF-8. |
| Source-language code page (decision 22) | `app/filters/base.py::source_code_page` · `app/filters/base.py::document_code_page` · `app/filters/base.py::apply_code_page_hint` · `app/filters/base.py::text_looks_garbled` · `app/filters/code_pages.py::target_code_page` · `app/filters/code_pages.py::native_letters` | Ingest parses inside `source_code_page(project.source_locale)`: the last step of the shared decode uses that language's code page (Greek 1253, Russian 1251, Czech 1250, Japanese 932, Chinese GBK/Big5 with GB18030 as the GBK fallback, Korean 949, ...; table shared with HTML Help). One parse uses one code page: once a part used one, later parts are read with it (a container re-reads every part with the single stamp). A short file valid as UTF-8 in a CJK-source project stays UTF-8 but warns when its few non-ASCII characters read cleanly as CJK in the source code page. When the letters look wrong (scripts mixed inside words, words of mostly accented letters, except, when the file was read with the source language's own code page, words made only of that language's own accented letters such as Polish "Zażółć" (#760), a single-byte decode of mostly UTF-8 bytes) a non-Western code page gives way to Windows-1252 when that reads cleanly, and either way a `text_encoding` warning rides `file_warnings` (upload JSON, document page, CLI, MCP). The code page used is stamped as `text_code_page`; export (`build_translated_file`) and repair re-read the original with exactly that code page, and a document without the stamp keeps the #509 rule. A Java `.properties` stays ISO-8859-1 but warns (`warn_if_garbled`). |
| DOCX | `app/filters/docx_filter.py::DocxFilter` · `app/filters/docx_resources.py::external_resource_warnings` · `app/filters/docx_bilingual.py::translation_paragraphs` | The versioned-walk filter described above, the offline inventory of external links recorded in the skeleton, and the bilingual-table mode (an Articulate Storyline 'Export to Word' document translates only its Translation column; same walk, fewer units). |
| PowerPoint and Office siblings | `app/filters/pptx_filter.py::PptxFilter` · `app/filters/pptx_filter.py::check_markup_budget` · `app/filters/docx_filter.py::DocxDocument` · `app/filters/xlsx_package.py::Package` | PPTX, PPTM, POTX, POTM, PPSX and PPSM through one lxml filter: slides in order with tables, groups, SmartArt (and its cached drawing), chart titles, speaker notes and hidden slides (both marked in the unit style), then shape alt text; only the text parts are rewritten, every other member keeps its bytes. DOCM, DOTX and DOTM use the DOCX walk and stamp; XLSM, XLTX and XLTM use the XLSX filter. Content types and the VBA project stay as uploaded, so a macro file never becomes its macro-free sibling. |
| Office comments | `app/filters/office_comments.py::COMMENT_BASE` · `app/filters/office_comments.py::xlsx_comment_slots` · `app/filters/office_comments.py::xlsx_apply` | #690: comment units appended at fixed indices for Excel (notes and threaded comments, patched into the finished package), PowerPoint, OpenDocument and Visio when the project option is on. |
| TTML / DFXP | `app/filters/ttml_filter.py::TtmlFilter` · `app/filters/ttml_filter.py::check_ttml_budget` | Caption `<p>` content only, spliced into the original text so untouched bytes stay identical; `<br/>` and `<span>` become inline tokens; hardened lxml parse first; export switches `xml:lang` to the target and, for a right-to-left target, writes `tts:direction="rtl"` on each rebuilt caption `<p>` (#741). |
| JS/TS locale modules | `app/filters/js_module_filter.py::JsModuleFilter` · `app/filters/js_module_filter.py::is_js_locale_module` | A tokenizer (never executed) that accepts only static object literals in `export default`, `module.exports` or `const` declarations; any other code refuses the file with a line number; translated strings are spliced back in their own quote style. |
| Angular XMB / XTB | `app/filters/angular_xmb_filter.py::XmbFilter` · `app/filters/angular_xmb_filter.py::XtbFilter` · `app/services/mime.py::export_ext_for` | One unit per `<msg>`/`<translation>`; `<ph>` becomes a protected token (START_/CLOSE_ pairs paired); an XMB exports as a fresh XTB with `lang` and the exact ids, an XTB is spliced so untouched bytes stay identical; the DOCTYPE is never resolved and ENTITY or external DTD is refused. |
| Broadcast captions | `app/filters/scc_filter.py::SccFilter` · `app/filters/stl_filter.py::StlFilter` · `app/filters/scc_filter.py::CaptionTargetError` | Scenarist SCC (CEA-608 byte pairs) and EBU STL (GSI + TTI blocks): caption text per cue, codes kept, untouched cues byte-identical; build re-encodes into the 608 set or the STL code table and refuses (CaptionTargetError, shown on export) characters or rows that do not fit. |
| HTML and XLIFF | `app/filters/html_filter.py::HtmlFilter` · `app/filters/xliff_filter.py::XliffFilter` · `app/filters/xml_text.py::xml_target_text` | Tag-preserving filters; XLIFF reads 1.2 and 2.0 and writes each translation into the sibling target; the shared helper normalizes text at XML boundaries (vertical tabs and form feeds become newlines, XML 1.0 illegal characters are dropped) while stored text stays unchanged. HTML text nodes keep `'` and `"` raw only when `app/filters/html_filter.py::_raw_quotes_safe` finds no construct a browser tokenizes differently (abrupt comments, raw-text elements, SVG, bogus comments); otherwise, and always for server-page and template builds, they are escaped, so a translated quote can never close a browser-side attribute (#672). Attribute values and the built page get the same guarantee from `app/filters/html_guard.py::check_build` (#676): a translated value whose surroundings a browser may read differently is written opaque to every HTML5 tag state (`app/filters/html_guard.py::attr_value`), and every html, xhtml, server-page, EPUB chapter, email HTML, Flare and HTML Help build is re-parsed with an HTML5 tokenizer (scripting on and off, both select parsers) and as XML when well-formed, then refused unless the browser finds only the source's elements, attributes and script/style bodies (translated alt, title, aria-label, language and allowlisted `<meta>` content values aside; `app/filters/html_guard.py::meta_text_style` is the one allowlist the filter and the check share, #733). |
| Bilingual CAT XLIFF | `app/filters/cat_xliff.py::SdlxliffFilter` · `app/filters/cat_xliff.py::MqxliffFilter` · `app/filters/cat_xliff.py::MxliffFilter` · `app/filters/cat_xliff.py::build_units` | Trados SDLXLIFF, memoQ MQXLIFF and Phrase MXLIFF (and plain XLIFF 1.2 with seg-source): one unit per vendor segment, locked/approved/confirmed segments never parsed or overwritten, and a byte-splice build that writes only target contents and status attributes (SDL conf, memoQ status) so every other byte of the vendor file is kept. |
| Server-page templates | `app/filters/server_page_filter.py::PhpTemplateFilter` · `app/filters/server_page_filter.py::JspFilter` · `app/filters/server_page_filter.py::AspxFilter` · `app/filters/server_page_filter.py::ErbFilter` · `app/filters/server_page_filter.py::ShtmlFilter` | The HTML filter over a masked copy: each dialect lexer replaces code islands with sentinels before tag scanning, output expressions in text become point tokens, statement blocks are unit boundaries, and build swaps the original islands back byte for byte. Unsafe islands (in tag or attribute names, unclosed, half a tag) refuse the file. A plain .html/.htm holding a closed PHP or `<% %>` island is parsed and built by the matching filter (`server_page_for_html`: PHP, JSP when the page says so, else ASP.NET; mixed PHP and `<% %>` is refused), so server code never becomes page text (#672). |
| Trados TagEditor TTX | `app/filters/ttx_filter.py::TtxFilter` · `app/filters/ttx_filter.py::build_ttx` | Pre-segmented TTX only (one unit per `<Tu>`; a file with no `<Tu>` is refused); `<ut>` codes and `<df>` formatting are protected tokens, a `<Tu>` whose target already has text is never parsed or overwritten, and the build splices only target `<Tuv>` contents and the written `<Tu>`'s MatchPercent/Origin, in the file's own encoding (usually UTF-16 LE with a BOM). |
| Wordfast | `app/filters/wordfast.py::TxlfFilter` · `app/filters/wordfast.py::TxmlFilter` · `app/filters/wordfast.py::build_txml` | Wordfast Pro TXLF runs on the CAT XLIFF engine (gs4tr locked and confirmed targets protected); TXML is one unit per `<segment>` in a translatable block, `<ut>` codes as protected tokens, locked/confirmed targets never overwritten, and a byte-splice build that changes only target contents. |
| Trados packages | `app/filters/sdl_package.py::SdlPackageFilter` | A Trados project package (.sdlppx) becomes one document per target SDLXLIFF (the ingest chokepoint routes it); each document stores only the .sdlproj and its SDLXLIFF, translates through the SDLXLIFF filter, and exports as a return package (.sdlrpx): the original .sdlproj with only PackageType switched to ReturnPackage plus the rebuilt SDLXLIFF under its original path. |
| Zipped XLIFF packages | `app/filters/xliff_package.py::MqxlzFilter` · `app/filters/xliff_package.py::XlzFilter` · `app/filters/xliff_package.py::build_package` | memoQ .mqxlz and WorldServer .xlz/.wsxz: opened through zip_guard, every XLIFF member routed through the cat_xliff engine (WorldServer iws:status lock_status/translation_status honoured), every other member carried with identical content in the same order; keys are member offset plus walk position. |
| Resource and subtitle formats | `app/filters/json_filter.py::JsonFilter` · `app/filters/po_filter.py::PoFilter` · `app/filters/srt_filter.py::SrtFilter` · `app/filters/markdown_filter.py::MarkdownFilter` · `app/filters/text_filter.py::TextFilter` | String values only for JSON, msgid and msgstr slots for PO, cue text for SRT and WebVTT (WebVTT cue tags are tokens, its `Language:` header takes the target, #737; SRT ASS override blocks such as `{\an8}` are point tokens, #738), prose without fenced code for Markdown (front matter: only the values of `LOC_TMS_FRONT_MATTER_KEYS` keys, see `markdown_filter.find_front_matter`; link reference definitions offer only their title, footnote labels stay markup, indented code and comment-only HTML blocks are skipped, #735; walk stamp `md_walk`), one unit per non-empty line for text. |
| SBV, SubViewer and MicroDVD subtitles | `app/filters/subtitle_text_filter.py::SbvFilter` · `app/filters/subtitle_text_filter.py::SubFilter` · `app/filters/subtitle_text_filter.py::sniff_sub` | Cue text only, span-replaced so every other byte round-trips; `.sub` is sniffed as SubViewer 2.0 or MicroDVD and refused when ambiguous; MicroDVD `{y:i}` codes are point tokens. |
| Salesforce STF | `app/filters/stf_filter.py::StfFilter` | Translation Workbench Source / Outdated and untranslated / Bilingual exports: the LABEL column is the unit, keys never change; build fills TRANSLATION under a column header that names it (Bilingual TRANSLATED) and replaces LABEL elsewhere; `build_localized` sets `Language code:`; escapes, `{0}` and `{!merge}` fields are point tokens. |
| OpenDocument | `app/filters/odf_filter.py::OdtFilter` · `app/filters/odf_filter.py::OdsFilter` · `app/filters/odf_filter.py::OdpFilter` · `app/filters/odf_filter.py::OdgFilter` | One paragraph walker over content.xml (plus header and footer paragraphs in styles.xml) for text, spreadsheet, presentation and drawing packages and their templates; spans and links are paired tokens, every other inline element a point token moved back whole; string cells only; mimetype stays the first stored member and untouched members keep their bytes. For a right-to-left target every spreadsheet sheet's table style gets `style:writing-mode="rl-tb"` (column A on the right; `_flip_sheets_rtl`, B-rtl). |
| AsciiDoc | `app/filters/asciidoc_filter.py::AsciiDocFilter` · `app/filters/asciidoc_filter.py::set_lang` | Line-grammar walk (no Asciidoctor): prose, titles, list items and table cells span-replaced; verbatim, comment and passthrough blocks, attribute entries and preprocessor lines (include:: never followed) kept, except the header's `:description:` / `:keywords:` values (appended `meta:*` units, written back on one line, a trailing backslash as `{backslash}`, #733); inline macros, {attribute} references and formatting marks become point or paired tokens; export sets `:lang:`. |
| MDX | `app/filters/mdx_filter.py::MdxFilter` · `app/filters/mdx_filter.py::translatable_attrs` | The Markdown line rules plus MDX: `import`/`export` blocks, block JSX lines and block `{expressions}` are skipped; inline JSX and expressions inside a line become protected tokens; JSX string attributes on the allowlist (`LOC_TMS_MDX_ATTRS`) are units appended after the line units; build splices by offset so untouched bytes stay identical. |
| Wiki markup | `app/filters/wiki_filter.py::MediaWikiFilter` · `app/filters/wiki_filter.py::DokuWikiFilter` · `app/filters/wiki_filter.py::template_params` | Line-grammar walk (no wiki engine): headings, paragraphs, list items and table cells span-replaced; comments, verbatim tags, templates (balanced), magic words, categories and preformatted lines masked; link targets, URLs and tags become point tokens and link labels and formatting marks paired tokens; template parameters only from the `WIKI_TEMPLATE_PARAMS` allowlist. |
| Key-value software resources | `app/filters/kv_resource.py::KeyValueFilter` · `app/filters/kv_resource.py::KeyValueRecord` · `app/filters/properties_filter.py::PropertiesFilter` · `app/filters/apple_strings_filter.py::AppleStringsFilter` · `app/filters/ini_filter.py::IniFilter` · `app/filters/resjson_filter.py::ResjsonFilter` | Java .properties, Apple .strings, INI (Joomla too) and RESJSON: values only, spliced back into the original text so keys, comments and layout stay byte-identical; each file keeps its source encoding; `records()` is the key-based view for keys import and export. |
| JSON5 / JSONC | `app/filters/json5_filter.py::Json5Filter` · `app/filters/json5_filter.py::check_json5_budget` | Iterative regex tokenizer (depth cap, no recursion) records each string value span; json_filter's `_is_translatable` picks units; build splices translations in the original quote style so comments, trailing commas and untouched bytes stay identical. |
| Docs markup | `app/filters/rst_filter.py::RstFilter` · `app/filters/rst_filter.py::TableFitError` · `app/filters/latex_filter.py::LatexFilter` | reStructuredText and LaTeX: a line/command scan splices translated prose into the original text; code, math, directives and commands become protected tags; rST meta directive description / keywords values are appended `meta:*` units (#733); rST titles are re-adorned to the new width and table cells re-wrapped inside fixed column widths (TableFitError when they cannot fit). |
| YAML | `app/filters/yaml_filter.py::YamlFilter` · `app/filters/yaml_filter.py::check_yaml_budget` | Generic, Rails i18n and Symfony YAML: PyYAML events (never a constructed tree, aliases never expanded) locate each string value and build splices translations into the original text, so keys, comments, anchors and untouched quoting stay byte-identical; a Rails root locale key is renamed on localized build; `records()` is the key-based view. |
| XML software resources | `app/filters/xml_resource_common.py::XmlResourceFilter` · `app/filters/xml_resource_common.py::Splicer` · `app/filters/xml_resource.py::XmlResourceDispatch` · `app/filters/xml_resource.py::AndroidStringsFilter` · `app/filters/xml_resource.py::ResxFilter` · `app/filters/xml_resource.py::JavaXmlPropertiesFilter` · `app/filters/xml_resource.py::WxlFilter` · `app/filters/xml_resource.py::QtTsFilter` | Android strings.xml and Java XML properties (both .xml, routed by root element), .resx/.resw, WiX .wxl and Qt .ts. lxml finds the units (no entities, no DTD internal subset); build splices only the changed values into the original text, so every other byte stays identical. Each filter also exposes `records()`, the key view for the keys bridge. Pinned by `verify_xml_resource_filters`. |
| Email | `app/filters/email_filter.py::EmailFilter` | .eml: a byte-offset MIME walker (stdlib `email` only reads header blocks) yields the Subject and the non-attachment text/plain and text/html parts; HTML parts reuse `HtmlFilter`, plain parts are split into paragraphs (RFC 3676 format=flowed unwrapped); build splices only changed header blocks and bodies, re-encoding each in its charset and transfer encoding with a recorded UTF-8 / quoted-printable fallback. S/MIME and PGP are refused. |
| Desktop entries and Haiku catalogs | `app/filters/desktop_filter.py::DesktopFilter` · `app/filters/catkeys_filter.py::CatkeysFilter` · `app/filters/catkeys_filter.py::catkeys_fingerprint` | `.desktop`: unlocalized Name/GenericName/Comment/Keywords (items) of Desktop Entry and Desktop Action groups; localized build inserts or replaces `Key[ll_CC]=` after the source key. `.catkeys`: the source column is the unit, build fills the translation column, localized build sets the header language and keeps the source-only fingerprint; both expose `records()`. |
| AutoCAD DXF | `app/filters/dxf_filter.py::DxfFilter` | ASCII DXF group-code walk over ENTITIES and BLOCKS (TEXT, MTEXT chunks, ATTRIB/ATTDEF, DIMENSION override, MULTILEADER 304); changed values are byte-spliced, MTEXT re-chunked to 250-character group 3 pieces; UTF-8 for AC1021+, $DWGCODEPAGE before, with \\U+XXXX overflow. |
| HTML Help | `app/filters/htmlhelp_filter.py::HhcFilter` · `app/filters/htmlhelp_filter.py::HhkFilter` · `app/filters/htmlhelp_filter.py::HhpFilter` | `.hhc`/`.hhk` sitemap Name/Keyword param values and `.hhp` Title/window captions; a lenient span lexer splices only changed values; See Also follows its keyword; ANSI code pages decoded explicitly and re-encoded in the target language's code page (`HtmlHelpEncodingError` refusal or `HTMLHELP_UTF8_FALLBACK`). A .hhc/.hhk that declares no code page is read in the code page of the .hhp project's `Language=` (`app/filters/htmlhelp_filter.py::hhp_code_page`; the .hhp from the same ZIP batch or already in the project, decision 21), stamped as `hhp_code_page` so export reads it the same way. With no .hhp a fresh upload uses the project's SOURCE language code page (decision 22). |
| QuarkXPress Tags | `app/filters/quark_tags_filter.py::QuarkTagsFilter` · `app/filters/quark_tags_filter.py::check_tags_budget` | One unit per paragraph line; `@Name:` is the style; every `<...>` tag inside the text is a point token; decoded and re-encoded in the `<eN>` encoding (UTF-8 and `<e9>` when a translation does not fit). |
| XAML | `app/filters/xaml_filter.py::XamlFilter` | `sys:String` / `x:String` resources (plus, with `XAML_VIEW_ATTRIBUTES`, the Content/Text/ToolTip/Header/Title attributes, appended after the resources); lxml checks the file, a span lexer splices only the changed values so every other byte survives; markup extensions are never units. |
| Multilingual XML | `app/filters/xml_multilingual.py::plan_multilingual` · `app/filters/xml_multilingual.py::build_multilingual` | An .xml file holding every language as sibling nodes (xml:lang / lang siblings, language-named elements, or a {lang} XPath rule from a `<?loc-multilingual?>` instruction or the `multilingual=` config) is handed over by the generic XML filter; segments are the source-language nodes, and the localized build fills or creates only the target-language sibling, splicing so every other byte stays. |
| SSA / ASS subtitles | `app/filters/ass_filter.py::AssFilter` | Dialogue Text field (last Format field) and Script Info Title only; `{...}` override blocks and `\N` `\n` `\h` become point tokens, drawing-mode lines are skipped, every other byte is copied; localized export sets the Script Info `Language:` header. |
| TOML | `app/filters/toml_filter.py::TomlFilter` | String values only, validated by stdlib `tomllib` and spliced into the original bytes; plural categories of a message table are separate units, Go template actions are point tokens, and a translation keeps its quoting unless a literal string cannot hold it. |
| Apple resources | `app/filters/apple_filter.py::PlistFilter` · `app/filters/apple_filter.py::StringsdictFilter` · `app/filters/apple_filter.py::read_plist` · `app/filters/apple_filter.py::XcstringsFilter` | XML plists are spliced in place (a strict span scanner cross-checked against plistlib) and binary plists rewritten by plistlib; Apple placeholders become point tokens; stringsdict and String Catalog plurals follow the target language's CLDR categories; a catalog export adds or replaces only the target language; `records()` is the key-based view. |
| FrameMaker MIF | `app/filters/mif_filter.py::MifFilter` | MIF 8+ statement lexer; every Para is one unit across its ParaLines, inline statements (variables, xrefs, markers, fonts, anchors) are point tokens, and a translated paragraph is spliced back as one ParaLine; MIF 7 and older is refused. |
| XSL / XSLT | `app/filters/xslt_filter.py::XsltFilter` · `app/filters/xslt_filter.py::check_xslt_budget` | Literal result text (literal result elements, `xsl:text`, output instructions) and alt/title/placeholder/aria-label attributes; inline HTML and `xsl:text` are paired tokens, `xsl:value-of` / AVT / TVT expressions protected tokens; lxml checks the file, a span lexer splices only changed units so every other byte survives. |
| Spreadsheets and PDF | `app/filters/xlsx_filter.py::XlsxFilter` · `app/filters/xlsx_filter.py::walk_version` · `app/filters/xlsx_package.py::Package` · `app/filters/xlsx_guard.py::check` · `app/filters/pdf_filter.py::PdfFilter` | String cells (formulas are never units), then under walk 2 (#685, `xlsx_walk` stamp; no stamp = cells only) the text-box and shape paragraphs of each worksheet drawing; the export writes into the original package (shared strings, inline strings, drawings, core.xml) and keeps every other part byte for byte, Strict in and Strict out; a right-to-left target turns every worksheet view right to left (`set_sheets_rtl`, B-rtl, on the package's sheetView elements); the worksheet-expansion bound; the PDF text-layer parse with vision OCR for scanned pages, serialized because PDFium is single-threaded. |
| InDesign IDML | `app/filters/idml_filter.py::IdmlFilter` | One unit per visible story paragraph (designmap story order, then table cells, footnotes and endnotes; hyperlinks and tracked insertions that span several ranges are walked through); a paragraph styled across several character ranges is one unit with run tokens, and tabs, special-character processing instructions, index markers, hyperlinks and inline XML tags are protected. Hidden conditions, notes, tracked deletions and non-printing stories are skipped. Build rewrites only the stories it translates; every other package member keeps its bytes, and mimetype stays first and stored. `build_localized` sets `AppliedLanguage` on the rebuilt ranges only when designmap.xml declares a matching `<Language>`, and for a right-to-left target sets `ParagraphDirection="RightToLeftDirection"` plus the World-Ready composer (`HL Composer Optyca`, or `HL Single Optyca` for a single-line paragraph) on each translated paragraph's range and turns an explicit left-to-right `CharacterDirection` into `DefaultDirection` (`_stamp_rtl`, shared with ICML; B-rtl). Suites `verify_idml_filter`, `verify_rtl_layout`. |
| Visio drawings | `app/filters/vsdx_filter.py::VsdxFilter` | .vsdx and .vsdm: page names, shape text and shape data labels from the page parts; cp/pp/tp run markers and fld fields as point tokens; masters, macros and every other part copied untouched, only changed page parts re-serialized; a localized export stamps the target LangID on translated shapes and, for a right-to-left target, Paragraph `Flags`=1 on the rows their text uses (literal cells only; #740). |
| MadCap Flare projects | `app/filters/flare_filter.py::FlareFilter` | One .flprjzip is one document (plain .zip stays unsupported): topic and snippet blocks through the HTML tokenizer with MadCap phrasing elements as inline tags (topic variables are point tokens, never text), then alt text, then index keywords; `.flvar` text definitions, `.fltoc` titles, `.flglo` terms and definitions. Untranslated members and attributes stay byte-identical, an unchanged build returns the original bytes, and zip_guard plus path, symlink and DTD checks run first. |
| InCopy ICML / WCML | `app/filters/icml_filter.py::IcmlFilter` | One XML file whose plain `<Story>` elements (one for ICML, several for a WoodWing .wcml, including the Smart Connection v3 layout whose `<ea:Stories>` root wraps one snippet `<Document>` per `<ea:Story>`, #670) are walked and rebuilt with the IDML story walker, so units, tokens, skips and the right-to-left stamp match IDML. Build returns the original bytes when nothing is translated and otherwise splices only the re-serialized stories into the original bytes (whole-document re-serialization under the original declaration when a story span cannot be located safely). UTF-8 only. Suite `verify_icml_filter`. |
| Images | `app/filters/image_filter.py::ImageFilter` | SVG text nodes swapped in place (a right-to-left target adds `direction="rtl"` and a mirrored `text-anchor` on each translated `<text>`, `_svg_rtl`, B-rtl), and raster region detection plus overlay build (see [image localization](image-localization.md)). |
| Xcode localization catalog | `app/filters/xcloc_filter.py::XclocFilter` | A zipped `.xcloc` bundle: each `Localized Contents/*.xliff` goes through the XLIFF filter, every other member keeps its bytes, and a localized build stamps `targetLocale` in contents.json and `target-language` in the XLIFF. |
| Zip-bomb guard | `app/filters/zip_guard.py::check` | Checks the OOXML central directory before python-docx or openpyxl opens the package. |
| ZIP batch upload | `app/filters/zip_batch.py::expand`, `app/services/ingest.py::ingest_zip_batch`, `app/services/delivery.py::export_project` (`batch=`) | A `.zip` upload (not a registered filter, so not in `supported_exts`) becomes one document per supported member through `ingest_document`, with `zip_batch = {id, archive, path}` on the skeleton; skipped and failed members are listed. `zip_guard` runs first; unsafe paths refuse the archive. The batch export writes `<locale>/<original path>`. |
| Output accessibility | `app/filters/a11y.py::target_tag` | The shared target-language declaration and alt-text handling, pinned above. |
| ADF | `app/filters/adf.py::extract` · `app/filters/adf.py::reinsert` | Not a file filter: the Jira rich-text walker the Jira connector composes. |
| Contentful Rich Text | `app/filters/contentful_rich_text.py::extract` · `app/filters/contentful_rich_text.py::rebuild` | Not a file filter: the Contentful Rich Text walker (one unit per text block, paired and point placeholders, all-or-nothing rebuild with a structure guard) the Contentful connector composes. |
| Sanity Portable Text | `app/filters/sanity_portable_text.py::extract` · `app/filters/sanity_portable_text.py::rebuild` | Not a file filter: the Sanity Portable Text walker (one unit per text block, marked spans as paired placeholders, inline objects and code spans as point placeholders, images and custom blocks copied verbatim, all-or-nothing rebuild) the Sanity connector composes. |

The input limits are settings of the process, not of a request: `app/config.py::MAX_UPLOAD_BYTES`
(100 MB by default) bounds the compressed file, `app/config.py::ZIP_MAX_UNCOMPRESSED_BYTES`,
`app/config.py::ZIP_MAX_MEMBERS` and `app/config.py::ZIP_MAX_RATIO` bound what a DOCX or XLSX package
may inflate to, `app/config.py::DOCX_MAX_PARAGRAPHS` counts every paragraph before the walk, and
`app/config.py::MAX_DOCUMENT_ROWS` caps segments per document. Every one is checked before any row is
written, and every refusal is an `InputLimitError`. The caps are enforced by `verify_request_limits`;
that entities, tag order, DOCX range markers and non-UTF-8 bytes survive a round trip is enforced by
`verify_filter_roundtrip_509` and `verify_doc_filter_encoding`.
