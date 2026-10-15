# Quality & LQA: Architecture (L1)

## 1. Role

This subsystem is the **quality-measurement layer**: the MQM-based LQA scorer and its
correction-loop safety guards, the per-segment quality-estimation (QE) judge and its routing
gate. The Evaluations Lab that measures candidate configurations against a dataset is a paid
add-on built on the shared metric library kept here.

LQA (`app/services/lqa.py`) follows the MQM model: errors are categorized by dimension and
severity, each severity carries a penalty, and the score is 100 minus the total penalty. It
produces a real deterministic scorecard offline (structural checks plus a per-pair rulebook
DSL) so the score responds to a rulebook edit with zero provider keys, and a live provider
adds fluency/accuracy/style findings on top. Around the LQA-to-fixer loop sit the safety
guards (`app/services/lqa_safety.py`): zero-spend deterministic checks that revert a fix
which introduced a violation, and a backstop that downgrades an LLM finding to neutral when
its span targets a legitimately-applied approved glossary term. QE (`app/services/qe.py`) is
a thin per-segment LLM judge that scores every produced segment so only the low-confidence
ones are routed to a human.

Post-edit capture (`app/services/postedit.py`) records edit-distance rows for reporting, reusing
the shared metric library (`app/services/eval_metrics.py`) rather than forking it. The surfaces are
the `lqa` CLI (`app/cli/cmd_lqa.py`) and the LQA pages.

It does not own the run engine that invokes LQA/QE inside a workflow (translation-pipeline),
the Evaluations Lab and the Advanced-tier meta-evaluation harness (paid add-ons), or the
reporting surfaces that read the post-edit rows.

## 2. Invariants

- **LQA scores offline and deterministically, 100 minus penalties.**
  `app/services/lqa.py::score_text` returns a scorecard whose score is exactly 100 minus the
  rulebook and structural penalties with the mock provider, and a set numeric threshold makes
  the step pass only at score at-or-above it. Enforced by `verify_lqa_threshold`.
- **A rulebook is one install-wide file per language pair, host-only under multi-org.**
  `app/services/lqa.py::rulebook_path` carries no org or customer key and its text goes into every
  org's judge prompt and gate, so with `setting:multi_org_enabled` on an org admin may neither read
  another org's rules nor change them for every org. The `/api` doors
  (`app/web/api_cli_misc_6.py`) and the web `POST /documents/{id}/rulebook` answer 403
  (`app/services/lqa.py::rulebook_host_only`), the LQA page offers no editor and does not show the text, the write is atomic
  (`app/services/lqa.py::save_rulebook`), and the host CLI keeps the door. Enforced by
  `verify_multiorg_install_wide_files`.
- **A fix that introduces a violation is reverted, but only as a delta.**
  `app/services/lqa_safety.py::introduced_violations` is a pure predicate: it returns the
  `(new_dnt, new_glossary)` delta of glossary/DNT violations the post-edit text has that the
  pre-edit text did not, and it is empty for an edit that repairs a violation or leaves a
  pre-existing one untouched. The orchestrator acts on a non-empty delta at its two call
  sites, keeping the pre-edit text byte-for-byte, flagging it, and routing it to review.
  Enforced by `verify_lqa_safety_floor`.
- **The adjudicator downgrades a glossary-term finding to neutral, never deletes it.**
  `app/services/lqa_safety.py::adjudicate` sets an LLM finding whose span targets a
  legitimately-applied approved term to neutral (penalty 0, cannot fail the gate) and marks
  it, leaving it visible rather than removing it. Enforced by `verify_lqa_adjudication`.
- **A numeral or punctuation-only segment that equals its source is correct, so an
  "identical to source" finding on it is advisory (#345).**
  `app/services/lqa_downstream_findings_bloc.py::downgrade_numeral_identical` runs in
  `score_text` just after adjudication and before scoring. The workflow LQA step scores a
  whole-document blob whose segments are joined with `SEG_JOINER`, so the rule splits
  source and target on it and works per segment: a finding becomes `neutral` (penalty 0,
  cannot fail the gate) only when its span matches a segment that has no letters and is
  unchanged from its own source segment (whitespace ignored) and its note or dimension
  says untranslated / identical to source (the span only picks the segment). This applies
  to segmented LQA; a FULL_TEXT document is scored as raw text and keeps the finding. A
  finding with no span on a
  multi-segment blob cannot be attributed and stays as the judge wrote it. Never
  downgraded: a finding whose dimension is Locale convention or Accuracy/Number, and
  anything on a segment whose number changed (`3.` to `4.`). The downgraded finding keeps
  its severity history in `advisory_numeral.original_severity` and its note gains an
  advisory marker. Enforced by `verify_findings_segment_scope`.
- **An HTML character reference the engine unescapes or loses goes to review (#703).**
  `app/services/quality_part2.py::entity_violation`, part of the write seam's placeholder
  gate (`placeholder_violation`, soft, TM-ineligible): a source `&lt;` / `&gt;` / `&amp;`
  the target writes as the raw character, or a currency or symbol reference (Unicode Sc /
  So, e.g. `&#162;`, `&copy;`) whose character is in neither form in the target. Typographic
  references (`&nbsp;`, `&quot;`, `&hellip;`, `&laquo;`) may be localised or dropped. On a
  document whose filter escapes raw `<`, `>`, `&` on export (class attribute
  `escapes_raw_text`: the HTML family, EPUB, email, Flare, MDX) the raw-character rule is off;
  callers that hold the segment pass `segment=` so the seam, approve and human save agree. Known
  trade-off: a currency spelled out in words (`&euro;9` -> `9ユーロ`) is held for review.
  Enforced by `verify_entity_gate_703`.
- **An ambiguous date, a changed line count or words glued at a formatting change go to review (decisions 11, 14, 16 of 2026-10-11).**
  `app/services/quality_part3.py::review_hold_violation`, called by the write seam on
  real-provider output (the whitespace gate's `lang_guard`, never full text): a numeric
  date whose day and month can be swapped is compared with the source date written with
  the same numbers (or an English written-out one) under each locale's reading order
  (month first for US English and Filipino, year first for `2026/11/10`, dotted dates day
  first, unknown for year-first languages reading `10/11/2026`) and held when the reader
  gets another day or cannot be proven to get the same one; the line-break count inside
  the text must match (not judged for formats whose newlines are spaces, the HTML / XML /
  markup family; a merged subtitle cue passes); for de, nl, fi, ko, tr, ar, he a run of
  inline tokens with a space beside it in the source and letters on both sides in the
  target is held when the join is implausible (a lowercase-to-capital join, a function
  word that never starts or ends a compound, a Korean word already ending in a case
  particle). One `review_hold` flag (minor), needs_review, TM-ineligible; a human save or
  approve clears it; the export still writes the target. Each scan is one bounded pass.
  Enforced by `verify_review_holds_qa`.
- **The QE gate is off by default and, when off, makes no judge call.**
  `app/services/qe.py::score_segment` runs only when a step's QE gate is on; with it off no
  judge call is made and no QE flag is stored, and a real judge call bills on the normal
  spend seam. Enforced by `verify_qe_gate`.
- **The Jev decision rule an operator reads is rendered from the code, not written beside it.**
  `app/services/qe_jev.py::describe_rule` reads the level names, the meaning-promotion level,
  the category penalties, the default threshold and this judge's resolved floors
  (`app/services/qe_jev.py::resolve_floors`, the one rule the scorer applies) at call time, and
  `app/services/qe_jev_loop.py::first_fix_prompt` builds the round-1 post-editor prompt exactly
  as `app/services/qe_jev_loop.py::refine` does. The judge Inspect page, its /api, CLI and MCP
  twins show both (engines-and-providers). Running SemIf or Kev as a local judge is the operator
  guide [jev-compatible-server](../guides/jev-compatible-server.md). Enforced by
  `verify_jev_inspector`.

### D1: TM provenance and correction-loop LQA (#159)

`lqa_safety.step_refused_indices` owns the shared refusal union used by provider
steps and segmented correction-loop LQA. D1 provenance remains gated by
`tm_fix_protection_enabled` and the repairing step's `tm_protect` tristate.
Persisted/in-run hard locks, content-protected TM fills, human APPROVED/EDITED
segments, and prior refusals are unconditional exclusions. Soft locks protect
only non-correction steps; editable prior-run soft locks are still judged.
The helper preserves the fixer's existing D1-only counter and safety-floor note.

`lqa_tm_lock_exclusion_enabled` is registered in `settings.DEFAULTS`, default `"1"`.
Setting it to `"0"` restores whole-document judging. Each check resolves its own
`on_fail` repairing target; a failure fallthrough resolves `prev_lqa:failed`
agents before the next LQA. Parallel lanes share their join's route. If several
agents can repair a check, exclusion requires all of them to refuse the unit.
A standalone check retains all units. A repairer's `tm_protect=False` disables
only D1; the other refusal terms still apply.

When all units are excluded, the check records SKIPPED with its reason, makes no
judge call, and stores no score or verdict. Routing uses **on_pass**, because
nothing failed, including `on_pass: "end"`. This routing choice does not fabricate
a passing verdict: both `prev_lqa:failed` and `prev_lqa:passed` (and their keyed
step guards) evaluate false. A publish step gated on `prev_lqa:passed` therefore
does not run after this skip; delivery routing is not a passing quality verdict.
Existing holds remain intact. In particular, an excluded unit's stale hold cannot
be cleared by another excluded run; it needs explicit review or a judging run
with exclusion disabled. The completion event's `lqa_safety.lqa_tm_excluded`
counts distinct excluded units across check visits (including parallel lanes and skips).
Scope events and run context store only excluded indices, not the judged index list.

Enforced by `verify_lqa_tm_exclusion` and `verify_lqa_tm_exclusion_shapes`.

- **Post-edit distance reuses the shared metric library and never poisons the caller's transaction.**
  `app/services/postedit.py::distances` equals the `app/services/eval_metrics.py` primitive
  (never forked), and `app/services/postedit.py::capture_guarded` wraps the write in a
  savepoint so a capture failure rolls back only the capture and never raises. Enforced by
  `verify_postedit`. Past `PE_EXACT_CELLS` (20k x 20k characters) the distance comes from a
  banded search that is exact for a small edit and a lower bound only for a near-total rewrite
  of a huge segment (#599: rapidfuzz holds the GIL, so a full pass on a 100k-character save
  froze every thread). Enforced by `verify_pathological_text_599`.

## 3. Where the work happens

| Concern | Pin | One line |
|---|---|---|
| MQM LQA scoring | `app/services/lqa.py::score_text` · `app/services/lqa.py::score_document` · `app/services/lqa.py::load_rulebook` · `app/services/lqa_downstream_findings_bloc.py::MQM_DIMENSIONS` | The deterministic-offline plus live MQM scorer and the per-pair rulebook DSL. |
| LQA safety guards | `app/services/lqa_safety.py::introduced_violations` · `app/services/lqa_safety.py::adjudicate` | The zero-spend introduced-violation delta predicate and the glossary-aware neutral-downgrade backstop. |
| QE judge + gate | `app/services/qe.py::score_segment` | The per-segment MQM-aligned judge behind the routing gate. |
| Jev QE backend (POC) | `app/services/qe_jev.py::score_segment` · `app/services/qe_jev.py::build_request` · `app/services/qe_jev.py::resolve_floors` · `app/services/qe_jev.py::describe_rule` | Independent text-only judge (hosted Jev or a Jev-compatible server) for the same routing gate; the request builder, the floor rule and the decision-rule text the judge Inspect page renders from the same constants. |
| Jev structural preflight | `app/services/qe_jev_structural.py::findings` · `app/services/qe_jev_structural.py::attach` | Deterministic checks run BEFORE the judge on every scored segment (untranslated multi-word text, tag/element parity, placeholder parity, number and time parity) using the write seam's shared recognizers; a MAJOR hit blocks the QE pass whatever the judge scores, identical single tokens are advisory (#200). |
| Jev number semantics (#367) | `app/services/qe_jev_numbers.py::check` | The meaning checks the shared number checker cannot make, run by the structural preflight only: a clock time dropped from the target (or changed between Western and CJK forms), a numeric, ISO or CJK date whose day, month or year changed (order judged per locale) or that was dropped, a lost minus sign, and a swapped or dropped currency. Each is MAJOR; a time or date written out in words is accepted. Compared dates are blanked before the value check so a correct reorder stays silent. |
| Jev-guided refine loop (POC) | `app/services/qe_jev_loop.py::refine`, `app/services/qe_jev_loop.py::build_prompt`, `app/services/qe_jev_loop.py::first_fix_prompt` | Bounded loop with baseline, full history, an optional cross-check (the post-editor LLM's own second opinion, not an independent judge) and spend totals; the CLI experiment and the workflow auto-fix both drive it, and the Inspect page shows its exact round-1 prompt. |
| QE auto-fix in workflows | `app/services/qe_autofix.py::run_loop` · `app/services/qe_autofix.py::make_accept` · `app/services/qe_autofix.py::gate_decision` · `app/services/qe_autofix.py::record` | The opt-in per-step repair of Jev gate failures: skip rules, fixer resolution, the safety guard chain and the reviewer's `qe_autofix` record. |
| Post-edit capture | `app/services/postedit.py::distances` · `app/services/postedit.py::capture_guarded` | The reused edit-distance metric and the savepoint-guarded capture seam. |
| Shared metric library | `app/services/eval_metrics.py::register` · `app/services/eval_metrics.py::applicable_scorers` · `app/services/eval_metrics_the_result_type.py::ScoreResult` · `app/services/eval_metrics_judge_qe_qe_score_segmen.py::_judge_identity` | The pure-computation scorer registry and the Tier-0 scorers the post-edit capture and the paid Evaluations add-on share. |
| Quality lenses + checks | `app/services/quality_lens.py` · `app/services/quality_lens_part2.py` · `app/services/quality_lens_seed_constants_copies_of.py` · `app/services/checks.py` | The Quality Lens registry and the deterministic checks the lenses reference (shared with the paid Advanced add-on). |
| LQA CLI | `app/cli/cmd_lqa.py::register` | The `lqa` command group (rulebooks, scoring). |
| Write choke point | `app/services/quality.py::commit_mt_output` · `app/services/quality_common.py::tag_damage` · `app/services/quality_part2.py::glossary_term_violations` · `app/services/quality_hub.py::is_refusal_check` | The single sanctioned target-text writer and the deterministic DNT/glossary/tag/number flags. |
| Review holds | `app/services/quality_part3.py::review_hold_violation` · `app/services/quality_part3.py::date_hold` · `app/services/quality_part3.py::line_break_hold` · `app/services/quality_part3.py::glued_words_hold` | Ambiguous dates, line breaks that differ and words glued at a formatting change on machine output (one `review_hold` flag). |
| Inline CAT QA | `app/services/qa.py::check_segment` · `app/services/qa.py::number_findings` · `app/services/qa.py::punctuation_findings` | The lightweight number/whitespace/punctuation checks for the editor. |
| Link locale | `app/services/quality_part4.py::url_locale_only` · `app/services/quality_part4.py::pair_off` | A link whose only change is its language or locale part (`/en/help` to `/fr/help`, `?lang=en` to `?lang=fr`) is not a changed link for the placeholder gate, the live tags badge and Jev; any other link change is still held. |
| Scaffold-echo guard | `app/services/scaffold.py::scaffold_echo` · `app/services/scaffold.py::markers` | Detects our own prompt scaffolding echoed back by a weak model and stored as a translation. |
| LQA vendor attribution | `app/services/lqa_attribution.py::vendor_for_scored_content` | Which vendor dispatch leg produced the content a scored LQA card covers. |

### Jev QE backend (POC)

`app/services/qe_jev.py::score_segment` is the alternative per-segment judge.
The single dispatch in `app/services/orchestrator.py` selects it when a gated
step's `qe_provider` is a judge ref (`app/services/qe_jev.py::is_judge_ref`): the legacy
`"jev"` (the env target) or an `engine:<id>` of kind `jev` / `jev_local` (a per-profile
target, see engines-and-providers). The target is resolved once per step visit by
`app/services/qe_jev.py::resolve_target`; a dead judge engine skips every segment with
provider_error. Other providers retain the existing LLM call.
Web, /api, CLI and MCP step authoring accept that existing field. The caller
passes already-redacted source and translation. Four severity questions and
one actionable-meaning question share one request. Levels none/minor/major/critical
map to counts 0/1/2/3, using the existing QE category penalties. An actionable
meaning probability at the configured floor promotes accuracy none, minor or unsure to major before the low-confidence skip.
The live Jev answer carries its level probabilities keyed by index (with an accompanying
legend describing each level), not by level name; both shapes are accepted.

Confidence distinguishes error from none per dimension: `p_none = probs[0]` and
`p_error = sum(probs[1:])`. If `p_none >= qe_jev_confidence_floor`, choose none with
confidence `p_none`; otherwise, if `p_error >= qe_jev_confidence_floor`, choose the
highest-probability error level (indices 1..3, ties toward higher severity) with
confidence `p_error`. A split among error severities only selects the level.
If both probabilities are below the floor, that dimension is unsure. A confident
error in any dimension keeps the card scored; unsure dimensions contribute count 0
and cannot add a penalty or veto that error. All confident none answers score 100
before actionable-meaning promotion. With no confident error and at least one unsure
dimension, skip as `low_confidence` and include per-dimension `p_none` and `p_error`
in `error`. Apply actionable-meaning promotion BEFORE this decision: accuracy none,
minor or unsure becomes major when the meaning signal reaches its floor. Confident
major/critical severity is preserved, and promoted accuracy is no longer unsure.
A 2026-09-22 live readout showed that the old winning-probability rule discarded planted errors when Jev
was split on severity, which motivated this error-versus-none rule.
The rationale shows three numbers per probability-backed dimension: `p_none`,
`p_err` (the error probability), and `api_conf` (the API's confidence, retained for
inspection but not used for the floor). For example,
`terminology=major(p_none=0.19,p_err=0.81,api_conf=0.19)`.
Score-only answers without probabilities retain rounded severity and use API
confidence for the floor, showing `api_conf` in the rationale.
Below-floor score-only answers are also unsure. Scored Jev cards add `unsure`, a
list of full dimension names (empty when none, including the empty-translation
card), alongside `score`, `rationale`, `categories`, `skipped`, `reason`, `error`,
`cost_usd`, `tokens_in` and `tokens_out`. Treat an absent `unsure` as an empty list;
the LLM backend does not set it. The rationale keeps confident entries as above
and labels unsure entries, for example `terminology=unsure(p_none=0.44,p_err=0.56)`
or `accuracy=unsure(api_conf=0.40)` for a score-only answer.

The QE gate (both backends) runs only in the SEGMENTED workflow path; a full-text step with qe_gate on makes no judge call.

The `_score_seg` dispatch guard in `app/services/orchestrator.py` records a non-skipped card
with unsure dimensions at or above threshold as `undecided=True`, `passed=None`.
It counts under `skipped` in both step and run stats, returns no gate decision,
and lets the segment proceed without seeding adaptive feedback. The writeback
preserves that undecided card in the segment's QE flag. Below threshold, an unsure
card still fails and routes to review. Unsure dimensions cannot rescue a failure.

`judge_qe_jev` sits beside `judge_qe` in the Evaluations Lab scorer registry,
with the same applicability and paired comparison surfaces. A missing key
produces NOT_APPLICABLE. `lqa qe-compare` provides a local JSONL comparison report.
Both `lqa qe-compare` and `lqa jev-refine` print a cost estimate first and make provider
calls only with `--yes` (#531); `--remote` and a `--json` path equal to `--pairs` exit 2.
Its JSON retains the card's `unsure` list. When any row has unsure dimensions, the
ASCII table adds an `unsure` column with initials such as `T` or `T,S`.
The comparison's pass field is also null for unsure cards at or above threshold,
and false below threshold; undecided rows are excluded from gate agreement and gold accuracy.
Spend uses `spend.record_external_usd` with the Jev input-token price, including
already-spent skips. The existing telemetry port records Prompt Tap requests
and responses when enabled. No MQM LQA or correction-loop behavior changes.

Both CLI commands attach caller-built tap metadata to every judge and fixer call:
`source=cli`, `tool=qe-compare` or `tool=jev-refine`, `pair` (the JSONL id or
one-based row index), and `round` (null for comparison, zero-based for refinement).
They use the workflow correlation keys: null `workflow_id`, `workflow_run_id`,
and `document_id`, the tool as `step_key`, and row index as `segment_index`.
With raw capture enabled by `prompt-tap on`, `prompt-tap show --provider jev`
lists the Jev calls. Refinement's LLM second-opinion scorer (the same post-editor LLM that writes the fixes) and fixer carry the same
pair and current round metadata.

#### Jev-guided refine loop (POC)

`lqa jev-refine` scores the input as version 0, then asks a fixer LLM for revised
translations and scores each with Jev. The default budget is ten versions including
the baseline (at most nine edits). The service clamps budgets to a ceiling of 25
versions and records max_tries_clamped when reduced; the CLI refuses larger values
with exit 2. Best means highest non-skipped Jev score; a fully confident round wins a score tie,
then the earliest version wins. An unsure round is eligible only when no fully
confident round has an equal or higher score. If all scored rounds are unsure,
best still selects the highest score. Both best and final retain an `unsure` list;
final is the last attempted version. A failed
fixer attempt retains the previous translation with a null score and an error.

The pure prompt renderer includes six fenced sections in order: role, dimensions
and level signals, penalties and target, source, oldest-first version history,
and the next-version instruction. History carries Jev levels, confidence,
meaning_changed probability and signed deltas. Lower severity is a positive delta.
For a dimension unsure in either round, `_delta` returns null and `_delta_line`
prints the actual labels without a sign, such as `accuracy: unsure -> major`
or `accuracy: none -> unsure`. It never subtracts an unsure pseudo-zero.
The prompt and runtime target stop both require the numeric target and no major
or critical dimension on that round, with no unsure dimensions.
The loop passes the Jev card through unchanged. `qe_jev_loop.refine` requires an empty `unsure` list
for `target_reached`, as well as every count < 2 and the numeric target. An unsure
count of 0 cannot create an all-clear pass because this stop guard and the
orchestrator's undecided guard explicitly reject it. The loop continues until
a fully confident target round or another stop reason. A fully confident minor
error can still meet a lower numeric target by design; a confident major or
critical error prevents the stop. In `qe_jev.score_segment`, meaning promotion runs
before unsure counts are reset and before the low-confidence skip.
`config.JEV_REFINE_PROMPT_CAP_CHARS` defaults to 60000. Oldest translations are
omitted first while retaining score lines, with history_truncated recorded on the
attempt. If even the source and score lines exceed the cap, fixer_failed records
the size error without calling the fixer. Source and history are data, not commands.

Stops are target_reached, max_tries, no_progress (identical translation),
judge_skipped_twice, fixer_failed, spend_cap, or judge_unavailable_at_baseline.
no_progress wins over judge_skipped_twice when both hold.
A missing key produces a not_configured baseline and the last of these stops,
with no fixer calls. Skips never count as scores or zero deltas. Round costs,
tokens and elapsed times sum to totals, including paid failures and skips.

Optional --cross-check asks the post-editor LLM for its own second opinion on each
accepted rewrite and shows its card alongside Jev. No cross-check runs on the baseline
or a failed rewrite. It roughly doubles LLM calls; spend depends on prompt sizes.
Goodhart caveat: optimizing Jev's score can exploit the judge without improving
translation quality. The second judge is a comparison signal, not proof of quality,
and uses the fixer provider without its revision history. The CLI command writes
nothing to production content tables. Existing spend-ledger and optional Prompt Tap
accounting still apply. The workflow auto-fix below reuses the same loop with three
injected seams (`scorer`, `baseline_card`, `accept`); their defaults keep the CLI
behaviour unchanged.

#### QE auto-fix in workflows

A `app/models.py::WorkflowStep` opts in with `qe_autofix` (default off), plus
`qe_autofix_provider` (the post-editor; NULL means the step's own provider),
`qe_autofix_max_rounds` (fixer rounds, refused outside 1 to 5, default 3) and
`qe_autofix_cross_check`. The loop runs only when the QE gate is on and the judge is a
Jev judge (`app/services/qe_jev.py::is_judge_ref`). Every save path refuses an NMT or
judge engine as the post-editor through `app/services/engines.py::assert_ref_role` with
the `fixer` role.

In the segmented run engine (`app/services/orchestrator.py`), right after a segment
FAILS the gate, the auto-fix closure checks the skip rules in
`app/services/qe_autofix.py::skip_reason`: unsure dimensions without a confident major failure, a
PII-redacting step (a v1 limit: the restore map would have to leave the worker), a
withheld refusal, a non-LLM post-editor, or the spend cap (see below). Each skip is
recorded. Otherwise `app/services/qe_autofix.py::run_loop` drives the refine loop with
the step's resolved judge target as the scorer (one resolution per step visit), the
gate's own card as round 0 (no duplicate judge call), and
`app/services/qe_autofix.py::make_accept` as the guard: refusal, inline-tag damage
against the source, the over-translation shape check (when the step's guard is on) and
the glossary/DNT delta against the text being fixed. A rejected version is a
fixer_failed round that is never scored and never best.

The versions are re-judged with `app/services/qe_autofix.py::gate_decision`, the same
pure rule the gate itself now uses. The earliest highest-scoring version that passes
replaces the segment's text and its card becomes the QE verdict (the step tally moves
from failed to passed). With no passing version the segment goes to review as before,
keeping the best attempt only when its Jev score is strictly higher than the
original's. Either way a `qe_autofix` qa_flags entry (built by
`app/services/qe_autofix.py::record`) carries every round's score, categories, unsure
list, rationale and text (capped at 2000 characters), the fixer ref and model, the stop
reason and the loop cost; the editor badge reads "auto-fixed in N rounds" or "auto-fix
tried N rounds" with the rounds openable. Loop spend (fixer and judge, minus the gate's
own baseline call) folds into the step's QE spend, so the StepRun cost stays true, and
every fixer and judge call carries its round number in the tap meta for the LLM Ledger.
The post-editor must be an LLM and is never switched silently (owner decision,
local-first): a save that would leave auto-fix inheriting a non-LLM step engine is
refused, and at run time such a step skips with `no_llm_fixer`. A re-scored version
with an unsure dimension and no remaining confident major stops the loop (`stop_reason` "unsure") and the segment goes
to review, never "fixed". The spend cap is checked for real, not only through a
per-visit latch: a loop with any priced component (hosted Jev, or a non-exempt
post-editor, see `app/services/qe_autofix.py::loop_is_priced`) is skipped before it
starts and stopped between rounds once the cap is reached, while a fully local loop
costs nothing and keeps running. A better version can be kept when it has no new
unsure dimensions: baseline unsure dimensions stay flagged for review and are never
rewrite targets. A new unsure dimension prevents keeping that attempt. A post-editor reply identical to the previous version is recorded
"unchanged" without another judge call and stops the loop (no_progress). The four
fields are dead on every non-llm_agent step (refused on save, cleared on a type switch),
and an explicit post-editor must name a known provider or an engine ref. The loop runs
serially on the database thread.

## 4. Data & settings

- **Models:** `app/models.py::LQAResult` (a stored scorecard), `app/models.py::SegmentPostEdit`
  (an episode-scoped edit-distance row). The Lab's own tables (`app/models.py::EvalDataset`,
  `app/models.py::EvalRun`, `app/models.py::EvalScore` and siblings) stay in the schema, empty,
  for the paid Evaluations add-on.
- **Rulebooks:** `app/services/lqa.py::rulebook_path` is one markdown/DSL file per language pair
  for the whole install (no per-org rulebook exists). Install-wide: host-only under multi-org (#425).

### Jev QE backend (POC) configuration

- `TYPESAFE_API_KEY`: optional hosted key; absent means not_configured, zero HTTP.
- `TYPESAFE_MODEL`: default `jev-latest`.
- `TYPESAFE_BASE_URL`: default `https://api.typesafe.ai`.
- `TYPESAFE_QE_MEANING_FLOOR`: default `0.8`.
- `setting:qe_jev_enabled`: default `"1"`; trimmed, case-insensitive `"0"`, `"off"`,
  `"false"` and `"no"` return jev_disabled with zero HTTP. It gates the legacy env target
  (`qe_provider="jev"`) only; a Jev engine profile is gated by its own enabled flag.
- A Jev engine profile carries its own key, model and optional `confidence_floor`,
  `meaning_floor` and `timeout_s` (10 s hosted, 30 s local); an unset floor falls back to the
  install value above. Only hosted `jev` consults the spend cap and books cost; `jev_local`
  is $0.
- `setting:qe_jev_confidence_floor`: at least 0.5 and less than 1.0, default 0.6; a dimension with both `p_none`
  and `p_error` below it is unsure, as is a score-only answer with API confidence below it.
  Skip only when at least one dimension is unsure and no dimension is a confident error.
  Finite stored values below 0.5 clamp to 0.5 before HTTP with one warning.
  Other invalid stored values fall back to 0.6 with one warning; settings write doors require
  a finite number at least 0.5 and less than 1.0. Stored 1.0 still loads, and Inspect warns
  that it cannot score segments. Exact error-level ties prefer higher severity.

HTTP uses a ten-second timeout and one retry after one second on 429, 529 or
transport failure. Other errors skip. Input costs USD 0.042 per million tokens;
output is free. Spend caps are checked before each attempt.

## 5. Enforced by

- `verify_lqa_threshold`: the deterministic-offline MQM score and the numeric pass gate.
- `verify_lqa_safety_floor`: the delta-only revert of an introduced violation.
- `verify_lqa_adjudication`: the neutral downgrade of a legitimately-applied glossary term.
- `verify_qe_jev`: hermetic mappings, spend, retry, dispatch, authoring and comparison contracts.
- `verify_qe_jev_loop`: bounded rounds, transcript, stop reasons, accounting and CLI contracts.
- `verify_qe_autofix`: the workflow auto-fix (pass in one round, max rounds with history,
  unsure and redaction skips, guard rejection, the spend-cap latch, flag-off byte identity,
  fixer roles, spend folding and round-tagged Ledger meta).
- `verify_qe_gate`: the gate-off no-call path and the real-judge spend accrual.
- `verify_postedit`: the reused metric and the savepoint-guarded, never-raising capture.
- `verify_multiorg_install_wide_files`: the per-language-pair LQA rulebook is host-only under
  multi-org (`/api` and web refuse 403; the host CLI keeps the door).

## 6. Battle scars

- **An LLM LQA finding must not punish a correctly-applied glossary term.** The adjudicator
  downgrades such a finding to neutral rather than deleting it, so the score is not dragged
  down by a term the run was told to use while the finding stays visible for a human.

## 7. Limits & accepted risks

- **Offline LQA is structural, not fluent.** With no provider key the scorecard is the
  deterministic structural-plus-rulebook layer only, so it catches rule and structure
  violations but not the fluency/accuracy findings a live judge adds, stated in the
  `app/services/lqa.py` module docstring.
- **QE is a cheap judge, sampled by gate, not a full LQA.** The QE score is one compact judge
  call per gated segment, so it is a routing signal rather than a full MQM scorecard, stated
  in the `app/services/qe.py` module docstring.

### QE auto-fix limits

Auto-fix requires a Jev judge and does not run on a PII-redacting step (v1).
An unsure dimension permits a loop only when a confident major can be fixed;
the unsure dimension stays flagged for review. It optimizes the Jev score, so the Goodhart caveat above applies:
a passing fix is a routing decision, not proof of quality. Rounds run serially, one
segment at a time, so a step with many failures takes proportionally longer.

### Jev QE backend (POC) limits

The default Jev judge is hosted (US cloud accepted for this POC); a `jev_local` engine profile points the
same protocol at a Jev-compatible server the operator runs. Either way it is text only and English-primary;
other languages can have lower accuracy. Images are ignored. It provides no error
spans and no generated prose rationale: the badge is a deterministic one-line
summary of severity and confidence. The 64k-token provider limit applies; rejected
requests skip. This is a routing experiment, not a replacement for MQM LQA or its
correction loop. Gold gate labels in the sample fixture are illustrative human
expectations; a minor style slip can still pass the default numeric gate.

Refine's best version prefers structurally clean versions over blocked versions,
then compares judge scores and confidence. A blocked baseline cannot win a score tie
against its repaired version.

Jev runs deterministic structural checks BEFORE the judge: identical source/target text
(after collapsing whitespace and folding case; same base languages are exempt), inline tags/markup, placeholders (including double braces,
ICU arguments and printf), and number/time parity. Numbers-only text, URLs, codes and
applicable glossary DNT terms are exempt from the untranslated check. Identical single tokens (including acronyms and brands) are advisory MINOR findings,
with or without a glossary, and do not block a QE pass. Identical multi-word text
and other structural failures are MAJOR and block a pass regardless of score or threshold. The judge still runs;
its findings are retained. Cards expose `structural_findings` (dimension, severity, reason)
and `structural_blocked`; the shared rationale names the failure in web and CLI views.
The existing empty-target fast path still skips the judge and fails as empty.
The `qe_jev_enabled=0` kill switch disables judge calls, not structural checks.
A disabled, unavailable or unconfident judge has no score; a structural block
still routes to review and says 'judge did not score'. Lab records structural
blocks as 0 with the reason, preserving the judge score separately; an unscored
card without a block stays not applicable. Judge provenance is judge-wrappers/2.
Number and time parity keeps the number checker's own grade. A real mismatch (a changed
value such as 5 -> 50, a changed time such as 6:45 -> 7:45, an AM/PM swap) is MAJOR and
blocks. Two forms are advisory: a dot-format time that equals the source time (Finnish
klo 18.45, German 18.45 Uhr, Danish and Norwegian dot times) and a number from 0 to 10
written as a word (2 -> deux) in English, French, German, Spanish, Italian, Portuguese,
Dutch, Swedish, Danish, Norwegian, Finnish, Polish, Russian, Chinese or Japanese. They
are listed in `structural_findings` for review but do not block a QE pass. Every other
number finding is MAJOR and blocks, including a dropped or added number, a wrong dot
time, digits regrouped into different numbers (1-10 -> 110) and a rendering that is
ambiguous without a locale convention. Jev also reads what a number means, and each of
these is MAJOR. A clock time dropped from the target blocks (hour words such as cinq
heures et demie, an hour-only form such as 20 heures, and Chinese or Japanese clock
times are read as the time). A date whose day, month or year changed blocks: the order is
judged for the target locale, so 3/14/2026 -> 14.03.2026 is correct and
3/14/2026 -> 15.03.2026 is not;
a dot date is day first, ISO dates and Chinese, Japanese and Korean written dates are
read the same way, a date written out in words passes when its day and year match, and a
bare en source locale with both numbers 12 or below leaves both orders open. A lost minus sign blocks (a
hyphen-minus, U+2212, an en dash or a parenthesised currency amount is negative, and a
word such as minus or moins stands in for the sign). A swapped or dropped currency
blocks ($, USD, US$ and dollars are one currency; $ -> EUR is not; placement and spacing
per locale do not matter). A minus the target adds is not judged. An AM/PM marker
counts only on hours 1 to 12, and a bare lowercase am after a German or Dutch time is
the word for on the, not a meridiem. In scripts written without word spaces (Chinese,
Japanese, Thai, Lao, Khmer, Myanmar) a run is one token, so identical text is advisory,
only while it is at most 8 characters after trailing punctuation (ASCII or full-width) is
removed and holds no sentence punctuation inside; a longer or internally punctuated run is
multi-word text and a copy of it is MAJOR. So the short word with a closing mark and the
same word without it are treated alike. HTML void elements (br, hr, img, input and the
like) are one tag kind whether written <br>, <br/> or <br />.
Time parity accepts 17 h 30 and 5:30 p. m.; it abstains when either side has no
parseable time. ICU nested argument names are sets across branches. Markup parity
compares element names and opening/closing/self-closing kind, not attribute values.
Meaning promotion runs BEFORE the low-confidence skip: a meaning signal at its floor
lifts accuracy none, minor or unsure to major. Confident higher severity is retained.
Auto-fix targets confident major dimensions even alongside unsure dimensions, which are
left unchanged and flagged for review. A better rewrite can be kept for review with the
baseline unsure dimensions still flagged; it cannot pass. New unsure dimensions prevent
keeping an attempt. API and MCP return the same cards and flags as web and CLI.
