# Workflow Engine: Architecture (L1)

## 1. Role

This subsystem is the **definition and management layer for workflows**: the ordered graph
of steps a document is run through, its variants and groups, and the fan-out that runs one
workflow across many target locales at once. A workflow is a named, org-scoped graph of
`WorkflowStep` rows (each a step type plus its config, an optional `run_condition` gate, an
optional `branch`, and a parallel-group tag); the engine owns creating that graph, editing
it under a set of validation seams, classifying its shape, reporting its usage and quality
stats, and dispatching batch runs.

The business logic lives in one service module (`app/services/workflows.py`) that both the
web routes (`app/web/workflows.py`) and the headless CLI (`app/cli/cmd_workflow.py`) call,
so there is a single code path for create, add-step, reorder, edit, clone, rename, archive,
and delete. Around that core sit four helpers: a pure shape classifier
(`app/services/workflow_shapes.py`), the Studio aids-rail composition
(`app/services/workflow_rail.py`), the per-workflow stats seam
(`app/services/workflow_stats.py`), and the fan-out batch dispatcher
(`app/services/batch_runs.py`).

It owns the graph and its edit-time validation, not the execution of a run. Starting a run,
streaming its steps, cancelling it, and the loop-safety caps at run time belong to the
orchestrator (translation-pipeline); this subsystem defines the graph the orchestrator
executes and, through the batch layer, decides which member runs to launch. It also does not
own the background-job engine the batch daemon rides (jobs-and-scheduler), the connectors a
connector-sync run drives (connectors), or the routing rules that pick a workflow for an
inbound submission (those are a separate routing surface).

## 2. Invariants

- **Project-bound workflow definitions follow project visibility.** Web detail and plan
  reads return the same 404 for missing workflows and workflows whose non-null
  `project_id` the caller cannot view, regardless of the `scope` label. Web lists,
  group variants/counts, building-block links, and workflow pickers exclude those rows.
  `app/web/workflows.py::_wf_or_404` uses `access.can_view_project`;
  `app/web/workflows_common.py::_visible_workflows` uses `access.visible_project_ids`.
  API detail already applies this boundary in `app/web/api_cli_common.py::_wf_in_org`;
  API group lists/details also filter through that resolver. Remote CLI group reads
  inherit the API filter; the in-process CLI remains the operator surface.
  Regression: `tests/verify_workflow_project_visibility.py` (#372).
- **A dead branch edge is dropped-with-warning only for a warnings-collecting caller on a
  branch-hidden step type; every other case hard-refuses.**
  `app/services/workflows_part3.py::validate_branch` is caller-agnostic: it has no warnings
  parameter and raises `WorkflowError` for any branch on a non-gate-producing step,
  identically for every caller. The drop-with-warning decision lives in
  `app/services/setup_author.py::apply_workflow_draft`, whose per-step predicate
  (`rs.get("branch") and st in _BRANCH_HIDDEN_TYPES and warnings is not None`) drops the dead
  edge only when the step type is in `_BRANCH_HIDDEN_TYPES` (`human_review` / `tm_match` /
  `external`, whose branch controls are hidden on the review page) and a warnings sink is
  supplied; a builder path with no sink, and an `llm_agent` or `lqa` step even with a sink,
  fall through to `validate_branch`'s hard refusal. Enforced by
  `verify_workflow_author_parity` (the no-sink hard-refusal, the human-review
  drop-with-warning, and the `llm_agent`-branch-still-hard-errors arms).
- **Deleting a workflow fails closed on any live reference.**
  `app/services/workflows_part3.py::delete_workflow` calls
  `app/services/workflows_common.py::workflow_reference_count` first and raises with the blocking
  references (routing rules, customer or group defaults, in-flight runs) rather than
  orphaning them; that reference gate is enforced by `verify_workflow_rename_delete`. It
  carries a second, independent fail-closed gate,
  `app/services/workflows_common.py::_empty_held_marker_orphans`, that refuses the delete when this
  workflow's runs hold the only `tm_match` provenance marker for an empty-held deliverable
  (erasing them would silently un-hold a partial document for source-filled delivery), and it
  NULLs the `run_id` of the affected `app/models.py::DeliveryEmptyApproval` rows so the
  human's durable approval decision, which keeps the approved target releasing, survives the
  delete. Enforced by `verify_tm_empty_delivery` (its `test_delete_workflow_gate` arm: the
  block names the un-hold hazard; an approved target's delete instead proceeds with its
  `run_id` nulled).
- **The graph cannot be edited while one of its runs is in flight.**
  `app/services/workflows_part5.py::add_step` refuses when the workflow has an active run and
  succeeds once that run reaches a terminal state. Enforced by `verify_run_cancel` (add-step
  blocked mid-flight, then allowed after cancel).
- **Shape classification is pure and never raises.**
  `app/services/workflow_shapes.py::detect_shapes` derives its tag set from the stored
  step fields only, treats a dangling branch key as no edge, and returns straight-through
  for empty or `None` input. Enforced by `verify_workflow_shapes`.
- **Reported spend is honest about untracked runs, never a fake zero.**
  `app/services/workflow_stats.py::stats_for` reports `spend_usd = None` when every run
  is untracked, a real total otherwise, and sets `spend_partial` when a real total is a floor
  because some run is untracked; `lqa_pass_rate` is `None` when the workflow produced no LQA
  gate. Enforced by `verify_workflow_stats`.
- **A batch never launches a second run for a locale already in flight.**
  `app/services/batch_runs_hub.py::_active_run_elsewhere` makes `start_batch` skip a locale whose
  `(workflow, document, locale)` already has any active run, surfacing it as an already-running
  note rather than reusing or mutating that run. It blocks on any active run for the triple,
  including a member of THIS batch created by a concurrent dispatcher (a double-clicked Re-run,
  two overlapping retry-failed calls), not only a cross-batch one; the `batch_id` argument is
  retained for the call-site signature and no longer narrows the query. Enforced by
  `verify_batch_runs` (the already-running skip and the `test_concurrent_reruns_no_double_run`
  arm).
- **A re-run or retry-failed starts fresh work, so it refuses a retired workflow or project.**
  `app/services/batch_runs.py::rerun_batch` applies the same archive gates as a normal launch, in the
  same order and with the same wording (archived workflow, then archived project), before any state
  change: no reopen and no run row (#619). Enforced by `verify_rerun_archived`.
- **A plain single run stays byte-identical to the pre-batch path.**
  A run created outside `app/services/batch_runs.py::start_batch` carries a NULL `batch_id`;
  the batch layer is where a `WorkflowRun` gets a non-NULL `batch_id` and a `WorkflowBatch`
  parent. Enforced by `verify_batch_runs` (a plain run's `batch_id` is NULL).

## 3. Where the work happens

| Concern | Pin | One line |
|---|---|---|
| Spend preview | `app/services/workflow_plan.py::plan` | Read-only per-locale variant, step engine, QE judge and auto-fix disclosure shared by web, CLI, API and MCP. |
| Graph CRUD | `app/services/workflows_part2.py` · `app/services/workflows_part4.py::create_workflow` · `app/services/workflows_part5.py::add_step` · `app/services/workflows.py::reorder_step` · `app/services/workflows_part6.py::update_step` · `app/services/workflows_common.py::log` · `app/services/workflows_part2_leaf.py::WorkflowForeignCustomerError` · `app/services/workflows_part3.py::_ref_resolves` · `app/services/workflows_common_leaf.py::log` | The one service path for building and editing a workflow's ordered steps, shared by web and CLI. |
| Edit-time validation | `app/services/workflows_part4.py::validate_run_condition` · `app/services/workflows_part3.py::validate_branch` · `app/services/workflows_part3.py::validate_key` · `app/services/workflows_part4.py::refuse_dead_fields` | The gate/branch/key/dead-field seams the author surfaces run before a step is written. |
| Delete + reference safety | `app/services/workflows_part3.py::delete_workflow` · `app/services/workflows_common.py::workflow_reference_count` | Fails closed on live references instead of orphaning routing rules, defaults, or runs. |
| Variants + groups | `app/services/workflows_part5.py::copy_to_pairs` · `app/services/workflows_part4.py::resolve_variant` · `app/services/workflows_part4.py::add_to_group` | The per-locale variant fan-out and the group a batch resolves each locale against. |
| Shape taxonomy | `app/services/workflow_shapes.py::detect_shapes` · `app/services/workflow_shapes.py::shape_headline` | The pure classifier that tags a graph straight-through / gated / correction-loop / triage / human-checkpoint. |
| System prompt catalogue | `app/services/system_prompts.py::list_system_prompts` · `app/web/system_prompts_api.py::api_system_prompts_list` | The read-only list of the built-in pipeline system prompts (step system message variants, content fence, single-unit retry, MT, tag repair, judges), computed from the real constants and shown on Building blocks, `GET /api/system-prompts` and `workflow prompt system`. Read-only apart from the two editable heads below (GitHub #356). |
| System prompt overrides | `app/services/system_prompt_overrides.py::save` · `app/services/system_prompt_overrides.py::resolve` · `app/services/system_prompts.py::editable_prompts` | Org-level override of the editable head of the AI-step system message (`step_head`) and the MT system message (`mt_head`), behind the default-off `system_prompt_overrides` flag. Append-only history in `SystemPromptOverride`, restore and reset, audit-logged, admin only. The locked floor (identity sentence, language pair, marker clauses, content fence, retry sentence) is composed in code around the override and never stored; `validate_body` refuses text that forges or repeats it. Resolved per run from the run's project org (never a caller argument); reads and writes compare orgs through `access.normalized_org_id`, so off the multi-org gate an editor write (org None) and a Default-org project meet before and after the boot backfill. The resolved head also feeds the output-guard echo n-grams. Each AI `StepRun` records key, version and the sha256 of the composed text (`StepRun.system_prompt_*`; the digest is of the plain-text marker variant for the run's language pair). Flag off: runs read nothing and stamp nothing, writes refuse, prompts are byte-identical; only the editor's history view reads the table (GitHub #356 phase 2a). |
| Per-workflow system prompt overrides | `app/services/system_prompt_overrides.py::wf_save` · `app/services/system_prompt_overrides.py::resolve` · `app/services/system_prompt_overrides.py::base_workflow_id` · `app/services/system_prompt_overrides.py::copy_workflow_overrides` · `app/services/system_prompts.py::workflow_editable_detail` · `app/web/system_prompts_api.py::api_workflow_system_prompt_save` · `app/web/workflows.py::save_workflow_system_prompt` · `app/cli/workflow_system_prompts.py::system_set` | The same two editable heads set on ONE workflow (GitHub #356 phase 2b), behind the same default-off flag. Resolution is pair-variant override > base workflow override > org override > built-in (#365); "inherit" is the absence of a live workflow override (never set, or the last action was a reset). The base of a language-pair variant is the default workflow of its `WorkflowGroup` (`system_prompt_overrides.base_workflow_id`: a base has no base, and the base is ignored when missing, archived, in another org, or scoped to a customer other than the variant's (a global base reaches a customer variant; a customer base never reaches a global variant or another customer's), so a base of another ring never applies; under the multi-org gate it must also be in the run's project org). Because the group default is the base, a change that moves or removes a live head (`system_prompt_overrides.group_default_change_needs_admin`: set-default, add with make_default, moving a group's default into another group, or (#386) moving an ordinary variant so the move changes either effective head, including gaining a base override over org or built-in fallback, on the web and /api routes) needs an admin or a manager (`system_prompts.can_change_group_default_heads`, 2026-10-08; a linguist is refused), and set-default itself is open to a manager (`system_prompts.can_set_group_default`); a manager may also change and restore ONE step's prompt and nothing else of the step (`system_prompts.can_edit_step_prompt`: the web `apply_step_prompt` and `restore_step_prompt` routes, and a `prompt_text`-only body on /api edit-step); `add_to_group` also clears a joining workflow's stale default flag so a group never ends up with two bases. History is append-only in the sibling table `WorkflowSystemPromptOverride` (a plain FK onto the workflow, so a delete reaps exactly its own rows and the org table's unique index and queries stay untouched; the org is the workflow's own, never stored on the row). Same validation, locked floor, audit by hash (`workflow.system_prompt_*`) as the org level. Who may edit: an admin or a manager (#630, 2026-10-08; `system_prompts.can_edit_workflow_heads`, `_manage_block` on /api; the org level stays admin only) whose org and customer ring contain the workflow (a foreign workflow is a 404); reads are admin, manager or linguist (`can_read_workflow_heads`). The run resolves once from its workflow id; under the multi-org gate a workflow override is ignored when the workflow's org differs from the run's project. The plain-MT path has no workflow of its own, so a pass takes the project's default workflow (the pair variant for the locale), and `/api/translate/text` takes an optional `workflow_id`. Each AI `StepRun` also records `system_prompt_scope` (workflow, base_workflow, org or builtin). Clone carries the live text (re-validated); copy-to-pairs from the base does not copy it (the new variant inherits it live), from a variant with its own text it does; `delete_workflow` and the project delete reap it. The machine-translation floor now always carries the locked sentence `prompts.MT_LOCKED_CONTENT_RULE` after an override body. Flag off: nothing read, nothing stamped, nothing copied, writes refuse. |
| Studio aids rail | `app/services/workflow_rail.py::customer_facts` | Composes the "what we already know" panel from the existing per-asset service seams, honest by empty list. |
| Per-workflow stats | `app/services/workflow_stats.py::stats_for` · `app/services/workflow_stats.py::collect` | The single stat dict (runs, spend with honest NULL, LQA pass rate) behind the dashboard, CLI, and /api, single and bulk. |
| Batch fan-out | `app/services/batch_runs.py::start_batch` · `app/services/batch_runs_hub.py::_dispatch` · `app/services/batch_runs_hub.py::recover_interrupted_batches` · `app/services/batch_runs_common.py::log` · `app/services/batch_runs_hub2.py::_finalize` | One `WorkflowBatch` dispatching a member run per locale under a bounded pool, with boot recovery of an interrupted batch. |
| Web surface | `app/web/workflows_part2.py::workflows_index` · `app/web/workflows.py::start_batch_run` · `app/web/workflows_common.py::_notice_url` · `app/web/workflows_part3.py::group_detail` · `app/web/workflows_part4.py::delete_prompt_template` | The `/workflows` management pages, the group pages, and the run/batch POST routes. |
| CLI surface | `app/cli/cmd_workflow_part3.py` · `app/cli/cmd_workflow_part2.py` · `app/cli/cmd_workflow_common.py` · `app/cli/cmd_workflow.py::register` · `app/cli/cmd_workflow_common_leaf.py::_MAX_WAIT_HOURS` · `app/cli/cmd_workflow_part2_leaf.py::_wf_show` · `app/cli/cmd_workflow_part3_leaf.py::_wf_rename` · `app/cli/cmd_workflow_part4.py::_wf_run` | The `workflow` command group that drives the same service seams headless. |
| Run-view overlay | `app/services/run_overlay.py::build_overlay` · `app/services/run_overlay.py::run_step_states` · `app/services/run_overlay.py::failure_shape` | The read-only projection of a run's `StepRun` rows onto the workflow graph for the run-detail page and headless run views. |
| Package / verify view | `app/services/package_view.py::workflow_package_view` · `app/services/package_view.py::step_facts` | The single source of truth for what each step assembles and verifies, shared by the builder, the CLI and `/api`. |

## 4. Data & settings

- **Models:** `app/models.py::Workflow` (the graph header, its scope, group, and pair),
  `app/models.py::WorkflowStep` (a step's type, config, `run_condition`, `branch`, and
  parallel group), `app/models.py::WorkflowGroup` (the variant family a batch resolves a
  locale against), `app/models.py::WorkflowBatch` and `app/models.py::WorkflowRun` (a batch
  and its member runs, correlated by `batch_id`), and `app/models.py::StepRun` (the per-step
  execution row whose stamped `cost_usd` the stats seam sums).
- **Settings:** `setting:batch_max_parallel` (default `"3"`, clamped 1..8 by
  `app/services/batch_runs_common.py::clamp_max_parallel`) bounds how many member runs a batch
  launches at once. `setting:workflow_max_visits` (default `"5"`) and
  `setting:workflow_total_step_cap` (default `"50"`) are the loop-safety caps a graph with
  backward branch edges inherits; they are consumed by the run engine at execution time
  (translation-pipeline), so they gate a running workflow rather than its definition.

## 5. Enforced by

- `verify_system_prompts`: the catalogue equals the real constants and builders, the Building blocks
  section is gated like the page, and the API and CLI return the same list.
- `verify_system_prompt_overrides`: the locked floor survives any override, flag off is byte-identical,
  org isolation (including background runs and the MT path), version history / restore / reset, the
  audit rows, admin-only edit on web, API and CLI, the run-record stamp, and the refusal and
  output-guard couplings with an overridden head.
- `verify_system_prompt_workflow`: workflow > org > built-in on every call site (sync, background, batch, concurrent fan-out, FULL_TEXT, MT pretranslate, the translate door), the winning-scope stamp, history / restore / inherit-again, clone / copy-to-pairs / delete, the locked MT sentence with the byte-identical default, who may edit, org and customer isolation, and the migration.
- `verify_workflow_shapes`: the pure shape classifier, including the empty/`None`/dangling
  cases that must never raise.
- `verify_workflow_stats`: the stat dict, the honest-NULL spend and `spend_partial` floor,
  and the org/customer scoping of the numbers.
- `verify_batch_runs`: the batch entity and NULL-`batch_id` byte-identity, the bounded pool,
  the spend and fail-fast stops, the never-adopt-a-foreign-run skip, the boot recovery of an
  interrupted batch (`test_recover_interrupted_batches`), and the FULL_TEXT-on-resolved-plan
  `tm_match` refusal.
- `verify_workflow_author_parity`: the branch/key validation seams and their web-and-CLI
  parity.
- `verify_workflow_rename_delete`: rename propagation and the fail-closed delete.
- `verify_run_cancel`: that a graph edit is refused while a run is in flight and allowed once
  it is cancelled.
- `verify_run_recovery_progress`: the plain-run startup healer
  (`app/services/orchestrator_part6.py::recover_interrupted_runs`, GitHub #44) is exact: it fails a
  planted plain RUNNING corpse (NULL `batch_id`) while leaving a PAUSED run and a live batch
  member untouched, because those belong to the other healers (the batch member is swept by
  `verify_batch_runs`'s `test_recover_interrupted_batches`, not by the plain-run healer).

## 6. Battle scars

- **A multilingual run used to be N uncorrelated single-locale submissions.** The batch layer
  added one `WorkflowBatch` parent and a `batch_id` on each member so a fan-out is one
  observable, spend-capped, concurrency-bounded object, while a NULL `batch_id` keeps a plain
  run identical to the pre-batch path.
- **A concurrent dispatcher could double-run the same locale.** The already-running check and
  the member insert had to be atomic across dispatchers so two batches racing the same
  `(workflow, document, locale)` skip rather than both launch, proven by
  `verify_batch_runs`'s concurrent-dispatch arm and noted in the module docstring.
- **A dead branch edge must fail loudly for a builder but drop quietly for a reviewer.** The
  split between a no-sink hard refusal and a warnings-collecting drop keeps the authored graph
  honest without breaking the Design-review screen on a renamed step key, and the drop stays
  scoped to the branch-hidden step types (`human_review` / `tm_match` / `external`) so an
  `llm_agent` or `lqa` step whose controls are visible keeps the one-click-fixable hard error,
  proven by `verify_workflow_author_parity`.

## 7. Limits & accepted risks

- **Fail-fast stops launching, it does not kill an in-flight member.** A batch's fail-fast and
  spend stops prevent launching further locales; members already running finish, because the
  run stream has no cancellation hook mid-step, stated in the `app/services/batch_runs.py`
  module docstring.
- **Loop-safety caps back-stop a graph, they do not validate it.** A workflow with backward
  branch edges relies on the per-step visit ceiling and the total-step cap at run time rather
  than a static proof that the graph terminates, stated in `app/services/settings.py` around
  the loop-cap defaults.
- **Stats are derived, not stored.** Spend and LQA rates are computed from `StepRun` and run
  rows on read, so a workflow whose runs predate spend tracking reports a NULL rather than a
  reconstructed total, stated in the `app/services/workflow_stats.py` module docstring.

FULL_TEXT workflows accept tm_match by default. Exact (100%) sentence matches from the step's selected readable TM pools are OFFERED, unverified, beside the glossary; {tm_offers} controls placement. The source body is unchanged. This is an offer, not a guarantee. If a string must be exact, FULL_TEXT is the wrong mode. The run summary, GET /api/runs/{id}, MCP work_status and workflow show-run expose tm_offers with sentences_examined, matches_offered and message after a FULL_TEXT tm_match step. These describe the latest offer list, not applied translations. SSE tm_match carries the counts for that step visit. A SEGMENTED workflow's tm_match step also leaves a tally on the run: the run page, GET /api/runs/{id} (tm_match), MCP work_status and workflow show-run report filled (genuine TM fills), relocked (already held the exact TM text), unmatched (not taken from the TM at the step's fill threshold, so sent on to the next step) and total segments. Fuzzy suggestions below the fill threshold are not counted.

Every later llm_agent step receives the TM offer block, except steps that redact PII. An empty tm_pool_selection offers nothing.
