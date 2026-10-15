"""Workflow CRUD shared by the web routes and the headless CLI.

Transitional compatibility shim for one release (added 2026-09-14, remove the re-export block after
the next tagged release): `__all__` re-exports log, WorkflowError, _clamp_window, _clamp_threshold,
_coerce_glossary_cap, _clamp_lqa_threshold, _TM_PROTECT_TRUE, _TM_PROTECT_FALSE, _coerce_tm_protect,
_MEMORY_MODE_OFF, _coerce_memory_mode, _coerce_tm_scalar_int, _coerce_tm_fuzzy_lock_mode,
_coerce_tm_pool_selection, _RC_REFS, _RC_OPS, _RC_SCORE_OPS, _RC_STEP_PREFIX, _is_step_ref,
_step_attr, _BRANCH_SLOTS, _BRANCH_SENTINELS, slugify_key, _step_produces_gate, _new_graph_errors,
_RULE_GROUP_SPLIT, _RULE_GROUP_MEMBER_TYPE, _RULE_GROUP_MULTI_BRANCH, _RULE_GROUP_REENTRY,
_RULE_DUP_KEY, _RULE_GUARD, _RULE_BRANCH, _rc_ref_of, _branch_ref_of, _LQA_DEAD_FIELDS,
_HUMAN_REVIEW_DEAD_FIELDS, _LLM_AGENT_DEAD_FIELDS, _EXTERNAL_DEAD_FIELDS, _VENDOR_DEAD_FIELDS,
_TM_MATCH_CONFIG_FIELDS, _TM_MATCH_DEAD_FIELDS, DEAD_FIELDS_BY_TYPE, _FieldLabels, _coerce_pt_id,
_MAX_WAIT_HOURS, _coerce_vendor_id, _EDIT_FIELDS, _resolve_provider_edit, clone_workflow,
workflow_active_only, workflow_is_active, _ACTIVE_RUN_STATUSES, workflow_reference_count,
_empty_held_marker_orphans, get_prompt_template,
WorkflowForeignCustomerError, _require_valid_lqa_threshold, _coerce_tm_tristate_bool,
_require_valid_tm_scalar, _require_valid_tm_pool_selection, _step_ref_key, parse_run_condition_str,
_rc_ref_qualifies, _ordered, _pgroup, _existing_keys, PARALLEL_PHASE1_TYPES,
validate_parallel_group, _step_label, _stable_ref, _SINGLETON_GROUP_IS_INERT, parse_branch_str,
_validate_branch_target, _coerce_wait_hours, _validate_step_vendor, validate_bound_template,
resolve_step_ref, _graph_row, _UNSET, _MOVE_LOCK, _tm_first_note, _GRAPH_FIELDS,
_GRAPH_SHAPING_FIELDS, _variant_report_entry, set_step_override, rename_workflow,
WorkflowArchiveError, WorkflowArchivedError, WorkflowCustomerMismatchError,
prompt_template_reference_count, prompt_template_bound_workflow_ids, _pair, _PAIR_SUFFIX_ONE_RE,
_LOC_ATOM_RE, _is_pair_atom, _same_pair, variant_visible, create_group, group_reference_count,
list_groups, group_variants, set_default_variant, _ADAPT_PLACEHOLDERS, _adaptation_prompt,
_RacedDuplicatePair, VariantMatch, group_scope_pairs, _ref_resolves, _group_span, _pre_group_prior,
validate_key, _group_runs, parse_branch_flags, validate_branch, _DEAD_FIELD_LABELS,
_DEAD_FIELD_HINT, _dead_field_is_meaningful, delete_workflow, create_prompt_template,
list_prompt_templates, rename_prompt_template, delete_prompt_template, pair_key,
_is_pair_suffix_body, _MAX_ECHOED_CODE, _reject_unknown_locales, visible_group_variants,
group_covered_targets, rename_group, delete_group, adapt_instruction, _variant_match,
_ambiguous_short_ref, validate_run_condition, _group_errors, refuse_dead_fields,
_DEAD_FIELD_DEFAULT, _force_clear_dead_fields, create_workflow, archive_workflow,
strip_pair_suffix, add_to_group, resolve_variant, group_locale_coverage, add_step, delete_step,
_graph_edit_errors, copy_to_pairs, _reorder_step_locked, propagate_step_edit, update_step from
app.services.workflows_common, app.services.workflows_part2, app.services.workflows_part3,
app.services.workflows_part4, app.services.workflows_part5, app.services.workflows_part6.

These functions hold the business logic that used to live INSIDE the route
handlers in app/web/workflows.py. The web routes now call them; the CLI
(app/cli/cmd_workflow.py) calls them too, so there is ONE code path for creating
a workflow, adding/deleting a step, cloning a workflow, and saving a prompt
template.

Each function is a pure service call: it takes a live Session (the caller owns
the transaction boundary — these helpers commit inside, matching the route
behaviour they replaced) and returns the created/affected object (or None when
the route silently no-ops on blank input). No HTTP concerns (redirects, form
parsing, AI-block gates) leak in — those stay in the routes.

Documented in: docs/architecture/workflow-engine.md
"""

import logging
import threading
from typing import NamedTuple

from sqlmodel import Session, select

from app.models import (
    Customer,
    EvalCandidate,
    MemorySuggestion,
    Project,
    ProjectTemplate,
    PromptTemplate,
    RoutingRule,
    RunStatus,
    Scope,
    StepRun,
    StepType,
    TargetLocale,
    TransMode,
    Workflow,
    WorkflowBatch,
    WorkflowGroup,
    WorkflowRun,
    WorkflowStep,
)
from app.services import context_pkg, ids, orchestrator, prompts
from app.services.workflows_common import (
    _ACTIVE_RUN_STATUSES,
    _BRANCH_SENTINELS,
    _BRANCH_SLOTS,
    _EDIT_FIELDS,
    _EXTERNAL_DEAD_FIELDS,
    _HUMAN_REVIEW_DEAD_FIELDS,
    _LLM_AGENT_DEAD_FIELDS,
    _LQA_DEAD_FIELDS,
    _MAX_WAIT_HOURS,
    _MEMORY_MODE_OFF,
    _RC_OPS,
    _RC_REFS,
    _RC_SCORE_OPS,
    _RC_STEP_PREFIX,
    _RULE_BRANCH,
    _RULE_DUP_KEY,
    _RULE_GROUP_MEMBER_TYPE,
    _RULE_GROUP_MULTI_BRANCH,
    _RULE_GROUP_REENTRY,
    _RULE_GROUP_SPLIT,
    _RULE_GUARD,
    _TM_MATCH_CONFIG_FIELDS,
    _TM_MATCH_DEAD_FIELDS,
    _TM_PROTECT_FALSE,
    _TM_PROTECT_TRUE,
    _VENDOR_DEAD_FIELDS,
    DEAD_FIELDS_BY_TYPE,
    WorkflowError,
    _branch_ref_of,
    _clamp_lqa_threshold,
    _clamp_threshold,
    _clamp_window,
    _coerce_glossary_cap,
    _coerce_memory_mode,
    _coerce_pt_id,
    _coerce_tm_fuzzy_lock_mode,
    _coerce_tm_pool_selection,
    _coerce_tm_protect,
    _coerce_tm_scalar_int,
    _coerce_vendor_id,
    _empty_held_marker_orphans,
    _FieldLabels,
    _is_step_ref,
    _new_graph_errors,
    _rc_ref_of,
    _resolve_provider_edit,
    _step_attr,
    _step_produces_gate,
    clone_workflow,
    get_prompt_template,
    slugify_key,
    workflow_active_only,
    workflow_is_active,
    workflow_reference_count,
)
from app.services.workflows_part2 import (
    _ADAPT_PLACEHOLDERS,
    _GRAPH_FIELDS,
    _GRAPH_SHAPING_FIELDS,
    _LOC_ATOM_RE,
    _MOVE_LOCK,
    _PAIR_SUFFIX_ONE_RE,
    _SINGLETON_GROUP_IS_INERT,
    _UNSET,
    PARALLEL_PHASE1_TYPES,
    VariantMatch,
    WorkflowArchivedError,
    WorkflowArchiveError,
    WorkflowCustomerMismatchError,
    WorkflowForeignCustomerError,
    _adaptation_prompt,
    _coerce_tm_tristate_bool,
    _coerce_wait_hours,
    _existing_keys,
    _graph_row,
    _is_pair_atom,
    _ordered,
    _pair,
    _pgroup,
    _RacedDuplicatePair,
    _rc_ref_qualifies,
    _require_valid_lqa_threshold,
    _require_valid_tm_pool_selection,
    _require_valid_tm_scalar,
    _same_pair,
    _stable_ref,
    _step_label,
    _step_ref_key,
    _tm_first_note,
    _validate_branch_target,
    _validate_step_vendor,
    _variant_report_entry,
    create_group,
    group_reference_count,
    group_scope_pairs,
    group_variants,
    list_groups,
    parse_branch_str,
    parse_run_condition_str,
    prompt_template_bound_workflow_ids,
    prompt_template_reference_count,
    rename_workflow,
    resolve_step_ref,
    set_default_variant,
    set_step_override,
    validate_bound_template,
    validate_parallel_group,
    variant_visible,
)
from app.services.workflows_part3 import (
    _DEAD_FIELD_HINT,
    _DEAD_FIELD_LABELS,
    _MAX_ECHOED_CODE,
    _dead_field_is_meaningful,
    _group_runs,
    _group_span,
    _is_pair_suffix_body,
    _pre_group_prior,
    _ref_resolves,
    _reject_unknown_locales,
    _variant_match,
    adapt_instruction,
    create_prompt_template,
    delete_group,
    delete_prompt_template,
    delete_workflow,
    group_covered_targets,
    list_prompt_templates,
    pair_key,
    parse_branch_flags,
    rename_group,
    rename_prompt_template,
    validate_branch,
    validate_key,
    visible_group_variants,
    workflow_delete_block_reason,
)
from app.services.workflows_part4 import (
    _DEAD_FIELD_DEFAULT,
    _ambiguous_short_ref,
    _force_clear_dead_fields,
    _group_errors,
    add_to_group,
    archive_workflow,
    create_workflow,
    group_locale_coverage,
    pair_name,
    refuse_dead_fields,
    refuse_if_workflow_archived,
    refuse_if_workflow_customer_mismatch,
    resolve_variant,
    strip_pair_suffix,
    unarchive_workflow,
    validate_run_condition,
)
from app.services.workflows_part5 import _graph_edit_errors, add_step, copy_to_pairs, delete_step
from app.services.workflows_part6 import _reorder_step_locked, propagate_step_edit, update_step

__all__ = [
    "DEAD_FIELDS_BY_TYPE",
    "PARALLEL_PHASE1_TYPES",
    "_ACTIVE_RUN_STATUSES",
    "_ADAPT_PLACEHOLDERS",
    "_BRANCH_SENTINELS",
    "_BRANCH_SLOTS",
    "_DEAD_FIELD_DEFAULT",
    "_DEAD_FIELD_HINT",
    "_DEAD_FIELD_LABELS",
    "_EDIT_FIELDS",
    "_EXTERNAL_DEAD_FIELDS",
    "_GRAPH_FIELDS",
    "_GRAPH_SHAPING_FIELDS",
    "_HUMAN_REVIEW_DEAD_FIELDS",
    "_LLM_AGENT_DEAD_FIELDS",
    "_LOC_ATOM_RE",
    "_LQA_DEAD_FIELDS",
    "_MAX_ECHOED_CODE",
    "_MAX_WAIT_HOURS",
    "_MEMORY_MODE_OFF",
    "_MOVE_LOCK",
    "_PAIR_SUFFIX_ONE_RE",
    "_RC_OPS",
    "_RC_REFS",
    "_RC_SCORE_OPS",
    "_RC_STEP_PREFIX",
    "_RULE_BRANCH",
    "_RULE_DUP_KEY",
    "_RULE_GROUP_MEMBER_TYPE",
    "_RULE_GROUP_MULTI_BRANCH",
    "_RULE_GROUP_REENTRY",
    "_RULE_GROUP_SPLIT",
    "_RULE_GUARD",
    "_SINGLETON_GROUP_IS_INERT",
    "_TM_MATCH_CONFIG_FIELDS",
    "_TM_MATCH_DEAD_FIELDS",
    "_TM_PROTECT_FALSE",
    "_TM_PROTECT_TRUE",
    "_UNSET",
    "_VENDOR_DEAD_FIELDS",
    "Customer",
    "EvalCandidate",
    "MemorySuggestion",
    "NamedTuple",
    "Project",
    "ProjectTemplate",
    "PromptTemplate",
    "RoutingRule",
    "RunStatus",
    "Scope",
    "StepRun",
    "StepType",
    "TargetLocale",
    "TransMode",
    "VariantMatch",
    "Workflow",
    "WorkflowArchiveError",
    "WorkflowArchivedError",
    "WorkflowBatch",
    "WorkflowCustomerMismatchError",
    "WorkflowError",
    "WorkflowForeignCustomerError",
    "WorkflowGroup",
    "WorkflowRun",
    "_FieldLabels",
    "_RacedDuplicatePair",
    "_adaptation_prompt",
    "_ambiguous_short_ref",
    "_branch_ref_of",
    "_clamp_lqa_threshold",
    "_clamp_threshold",
    "_clamp_window",
    "_coerce_glossary_cap",
    "_coerce_memory_mode",
    "_coerce_pt_id",
    "_coerce_tm_fuzzy_lock_mode",
    "_coerce_tm_pool_selection",
    "_coerce_tm_protect",
    "_coerce_tm_scalar_int",
    "_coerce_tm_tristate_bool",
    "_coerce_vendor_id",
    "_coerce_wait_hours",
    "_dead_field_is_meaningful",
    "_empty_held_marker_orphans",
    "_existing_keys",
    "_force_clear_dead_fields",
    "_graph_edit_errors",
    "_graph_row",
    "_group_errors",
    "_group_runs",
    "_group_span",
    "_is_pair_atom",
    "_is_pair_suffix_body",
    "_is_step_ref",
    "_new_graph_errors",
    "_ordered",
    "_pair",
    "_pgroup",
    "_pre_group_prior",
    "_rc_ref_of",
    "_rc_ref_qualifies",
    "_re",
    "_ref_resolves",
    "_reject_unknown_locales",
    "_reorder_step_locked",
    "_require_valid_lqa_threshold",
    "_require_valid_tm_pool_selection",
    "_require_valid_tm_scalar",
    "_resolve_provider_edit",
    "_same_pair",
    "_stable_ref",
    "_step_attr",
    "_step_label",
    "_step_produces_gate",
    "_step_ref_key",
    "_tm_first_note",
    "_validate_branch_target",
    "_validate_step_vendor",
    "_variant_match",
    "_variant_report_entry",
    "adapt_instruction",
    "add_step",
    "add_to_group",
    "archive_workflow",
    "clone_workflow",
    "context_pkg",
    "copy_to_pairs",
    "create_group",
    "create_prompt_template",
    "create_workflow",
    "delete_group",
    "delete_prompt_template",
    "delete_step",
    "delete_workflow",
    "get_prompt_template",
    "group_covered_targets",
    "group_locale_coverage",
    "group_reference_count",
    "group_scope_pairs",
    "group_variants",
    "ids",
    "list_groups",
    "list_prompt_templates",
    "orchestrator",
    "pair_key",
    "pair_name",
    "parse_branch_flags",
    "parse_branch_str",
    "parse_run_condition_str",
    "prompt_template_bound_workflow_ids",
    "prompt_template_reference_count",
    "prompts",
    "propagate_step_edit",
    "refuse_dead_fields",
    "refuse_if_workflow_archived",
    "refuse_if_workflow_customer_mismatch",
    "rename_group",
    "rename_prompt_template",
    "rename_workflow",
    "resolve_step_ref",
    "resolve_variant",
    "select",
    "set_default_variant",
    "set_step_override",
    "slugify_key",
    "strip_pair_suffix",
    "threading",
    "unarchive_workflow",
    "update_step",
    "validate_bound_template",
    "validate_branch",
    "validate_key",
    "validate_parallel_group",
    "validate_run_condition",
    "variant_visible",
    "visible_group_variants",
    "workflow_active_only",
    "workflow_delete_block_reason",
    "workflow_is_active",
    "workflow_reference_count",
]

log = logging.getLogger("loc_tms.workflows")


import re as _re


def reorder_step(
    session: Session,
    workflow_id: int,
    step_id: int,
    new_position,
    *,
    parallel_group=_UNSET,
    propagate: bool = True,
    report: dict | None = None,
) -> WorkflowStep:
    """Move a step to `new_position` — a 0-BASED POSITION IN THE ORDERED STEP LIST
    (not a raw order_index), i.e. the index this step will occupy once the move lands.
    Renumbers `order_index` CONTIGUOUSLY 0..n-1 across every step of the workflow and
    commits. Returns the refreshed step; raises WorkflowError (fail-closed, changing
    NOTHING) on any refusal.

    THIS IS THE ONE OPERATION ENTITLED TO RENUMBER (SPEC-workflow-canvas §4.1). Every
    other mutation path leaves order_index alone on purpose: `add_step` appends at
    max+1 and `delete_step` compacts nothing, precisely so no OTHER step's position
    silently shifts under an unrelated edit. A move owns the whole order by definition,
    so it is the one seam that may (and must) hand out a clean 0..n-1 — which also
    heals the gaps a prior delete left behind.

    THE TWO GUARDS ARE delete_step's / update_step's, and that symmetry is the point:

      GUARD 1, RUN SAFETY. Refused while the workflow has an ACTIVE
      (pending/running/paused) run. A paused run resumes at a stored
      `current_step_index`; a move re-seats steps around that cursor, so resuming
      would enter the wrong step (and, with a parallel group in the workflow, could
      resume INSIDE a group where `_group_at` declines to fork and a lane silently
      never runs). There is no correct index to fix up to, so the honest answer is to
      refuse until the run finishes or is cancelled.

      GUARD 2, WHOLE-GRAPH BEFORE/AFTER DIFF. The same `_graph_edit_errors` diff
      add/edit/delete apply, over the REORDERED list. Position is a graph property:
      moving a gate BELOW the step whose run_condition/branch depends on it orphans
      that reference; dropping an outsider between two members SPLITS a parallel
      group; dragging a member out of contiguity without clearing its slug splits it
      the same way. All of these are already errors `_group_errors` /
      `validate_run_condition` / `validate_branch` know how to name — this seam simply
      submits the post-move graph to them. A pre-existing (engine-tolerated)
      inconsistency never blocks an unrelated move; only NEW breakage does.

    LANE MEMBERSHIP COMPOSES INTO THE SAME CALL (`parallel_group`). `_UNSET` (the
    default) leaves the slug alone; `None`/`""` CLEARS it; a slug sets/changes it. The
    move and the slug change are applied together and the COMPOSED FINAL STATE is what
    Guard 2 judges, in ONE diff and ONE commit.

    THAT COMPOSITION IS NOT A CONVENIENCE, IT IS THE ONLY CORRECT SHAPE (review R1+R2,
    convergent HIGH). The surfaces previously did move-then-slug as two seam calls, which
    made dragging a step OUT of a group IMPOSSIBLE: the move was validated while the step
    still carried the group's slug, so pulling it away from its siblings always tripped
    the consecutiveness rule and refused before the clearing edit could run. Every
    drag-out was a guaranteed refusal, and every cross-group drag with it. Composing also
    deletes the compensating-write machinery the two-call design needed (snapshot the
    order, roll it back if the second call refused) -- which was itself unsound under
    concurrency: the review reproduced a rollback reverting a DIFFERENT request's
    committed move from a stale snapshot. One call, one diff, one commit: nothing to
    compensate.

    NO-OP SHORT-CIRCUIT. If the resulting order equals the current order AND the slug is
    unchanged, this returns immediately WITHOUT renumbering or committing. Contiguous
    renumbering is a real side effect -- it heals the gaps `delete_step` leaves -- and
    healing a gap can move a step INTO index 0, silently arming TM-first. A move that
    changes nothing must change nothing.

    HONEST SIDE EFFECT — TM-FIRST. The TM-first short-circuit fires on
    `order_index == 0` (orchestrator: a 100%-TM segment skips the model entirely).
    So moving a translate step to the FRONT makes it TM-first-eligible and moving it
    away removes that. That is what reordering MEANS here; it is not special-cased away
    -- instead, when a move CHANGES which step sits at index 0, `report['tm_first_note']`
    carries the plain sentence both surfaces show the user.

    `report`, when a dict is supplied, is filled exactly as update_step fills it:
    the 4b propagation outcome for a slug change (propagated / pinned / skipped_adapted /
    validation_skipped / run_active_skipped / propagated_as_singleton /
    dead_fields_cleared / auto_pinned / changed) plus `tm_first_note` and `noop`. A slug
    change is a real field edit, so it propagates to non-pinned variants and auto-pins a
    directly-edited variant step, identically to editing that field on the step form -- a
    band drop must not be a quieter way to do the same thing."""
    with _MOVE_LOCK:
        return _reorder_step_locked(
            session, workflow_id, step_id, new_position, parallel_group, propagate, report
        )
