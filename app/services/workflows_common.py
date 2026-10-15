"""workflows: shared leaf helpers (layered pure move)

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
_MAX_WAIT_HOURS, _coerce_vendor_id from app.services.workflows_common_leaf.

Documented in: docs/architecture/workflow-engine.md
"""

import logging
import re as _re

from sqlmodel import Session, select

from app.models import (
    Customer,
    EvalCandidate,
    MemoryEvent,
    MemorySuggestion,
    Project,
    ProjectTemplate,
    PromptTemplate,
    RoutingRule,
    RunStatus,
    Scope,
    StepRun,
    VendorDispatch,
    Workflow,
    WorkflowBatch,
    WorkflowRun,
    WorkflowStep,
)
from app.services import ids, orchestrator
from app.services.workflows_common_leaf import (
    _BRANCH_SENTINELS,
    _BRANCH_SLOTS,
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
    _FieldLabels,
    _is_step_ref,
    _new_graph_errors,
    _rc_ref_of,
    _step_attr,
    _step_produces_gate,
    slugify_key,
)

__all__ = [
    "DEAD_FIELDS_BY_TYPE",
    "_BRANCH_SENTINELS",
    "_BRANCH_SLOTS",
    "_EXTERNAL_DEAD_FIELDS",
    "_HUMAN_REVIEW_DEAD_FIELDS",
    "_LLM_AGENT_DEAD_FIELDS",
    "_LQA_DEAD_FIELDS",
    "_MAX_WAIT_HOURS",
    "_MEMORY_MODE_OFF",
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
    "_TM_MATCH_CONFIG_FIELDS",
    "_TM_MATCH_DEAD_FIELDS",
    "_TM_PROTECT_FALSE",
    "_TM_PROTECT_TRUE",
    "_VENDOR_DEAD_FIELDS",
    "WorkflowError",
    "_FieldLabels",
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
    "_coerce_vendor_id",
    "_is_step_ref",
    "_new_graph_errors",
    "_rc_ref_of",
    "_re",
    "_step_attr",
    "_step_produces_gate",
    "ids",
    "slugify_key",
]

log = logging.getLogger("loc_tms.workflows")


_EDIT_FIELDS = (
    "name",
    "step_type",
    "provider",
    "model",
    "fallback_provider",
    "fallback_model",
    "prompt_text",
    "prompt_template_id",
    "chunk_policy",
    "redact_pii",
    "assignee_email",
    "glossary_id",
    "adaptive_feedback",
    "adaptive_feedback_window",
    "doc_context_prepass",
    "qe_gate",
    "qe_threshold",
    "qe_provider",
    "qe_autofix",
    "qe_autofix_provider",
    "qe_autofix_max_rounds",
    "qe_autofix_cross_check",
    "lqa_threshold",
    "tm_examples",
    "tm_examples_window",
    "use_image_context",
    "concept_glossary_render",
    "glossary_concept_cap",
    "glossary_token_cap",
    "tm_protect",
    "over_translation_guard",
    "memory_mode",
    "tm_fill_threshold",
    "tm_fuzzy_lock_mode",
    "tm_lock_ice",
    "tm_hundreds_enter_lqa",
    "tm_context_floor",
    "tm_pool_selection",
    "run_condition",
    "key",
    "branch",
    "max_visits",
    "parallel_group",
    "vendor_id",
    "external_wait_hours",
)


# --- edit an existing step (SPEC-conditional-workflows §4.3 graph re-validation) --
# A partial-update seam mirroring add_step's full field set. `_UNSET` (defined above
# reorder_step, which needs it as a default too) is the sentinel that distinguishes
# "field not supplied -> leave unchanged" from an explicit value (including None / False
# / "" which are all meaningful edits). Editing a step can break OTHER steps'
# guards/branches, so an edit re-validates the WHOLE graph, fail-closed.
# The full ordered field set update_step accepts (mirrors add_step). Bool fields are
# coerced; string provider/model/fallback fields normalise ""->None; the conditional
# fields (run_condition/key/branch/max_visits) are re-validated against the graph.
# Batch 14 T2 (SPEC §25): the per-step image-context toggle. Adding it here also makes
# base-edit propagation carry it (propagate_step_edit sweeps _EDIT_FIELDS generically).
# Terminology T3 (SPEC §9-T3): the per-step concept-glossary renderer toggle + caps
# edit in place and propagate to non-pinned variants (propagate_step_edit sweeps
# _EDIT_FIELDS generically).
# run-190 Item D / M5: the per-step over-translation guard opt-out edits in place and
# propagates to non-pinned variants (propagate_step_edit sweeps _EDIT_FIELDS generically).
# Batch 15 T1 (SPEC-tm-match-step-config §8.A): the six tm_match per-step config
# columns edit in place like any other step config (and propagate to non-pinned
# variants -- propagate_step_edit sweeps _EDIT_FIELDS generically).
# SPEC-parallel-routing §5.6: edited in place like any other step config (and
# propagated to non-pinned variants, since a flattened variant would silently run
# its checks sequentially).
# External/vendor dispatch (PROPOSAL-external-processing-step Wave A): the vendor
# selector + per-step wait override, edited in place like any other step config.
def _resolve_provider_edit(current, incoming):
    """Resolve a provider / fallback_provider / qe_provider field on a step EDIT.

    Defense-in-depth for an adversarial-review finding: the web builder's per-step edit
    form populates these <select>s from config.PROVIDERS + engine_options(request), and
    engine_options() only offers ENABLED engines (app/web/deps.py). A step pinned to an
    engine:<id> that's since been disabled (or fell out of the org-scoped enabled list)
    has NO matching <option>, so the browser silently submits "" for that field — and
    because the edit form always posts the FULL field set, a bare prompt-text tweak would
    otherwise clear the engine pin back to the default provider. The template
    (workflow_builder.html) now splices the stored value back in as an extra selected
    <option> so a real browser submission carries it through unchanged; THIS function is
    the belt-and-suspenders for any other caller that still posts blank (a hand-crafted
    request, a future UI, an /api client) — update_step alone cannot tell "user chose
    blank" apart from "the browser dropped an unrepresentable option".

    Rule: an incoming empty value clears the field EXCEPT when the CURRENT value is an
    engine:-prefixed ref — that case is treated as UNCHANGED (leave the pin) because it is
    the one value a picker can silently fail to represent. A plain-provider pin
    (claude/gemini/openai/local/...) is always offered by the picker, so a real user-clear
    of a non-engine provider stays representable and is honoured normally. A non-empty
    incoming value always wins outright (an explicit choice, never ambiguous)."""
    incoming = (incoming or "").strip()
    if incoming:
        return incoming
    if (current or "").startswith("engine:"):
        return current
    return None


def clone_workflow(session: Session, workflow_id: int, bound_customer_id=None) -> Workflow | None:
    """Deep-copy a workflow and all its steps into a new "(copy)" workflow.
    Returns the new Workflow, or None when the source id does not exist.

    The clone inherits the source workflow's org_id (leak vector 12) so a cloned
    workflow stays in the same org as its source — the route already 404s a
    cross-org source before reaching here, so the org carried over is always the
    caller's own. No-op single-tenant (org_id is NULL on both).

    `bound_customer_id` (#468): the caller's own customer when it is a customer-bound
    account (None = staff / trusted CLI, unchanged). Cloning a GLOBAL workflow is how a
    customer account gets its own editable copy, so the clone is stamped with the
    caller's customer (scope customer) instead of inheriting the shared global scope."""
    src = session.get(Workflow, workflow_id)
    if not src:
        return None
    new_customer_id, new_scope = src.customer_id, src.scope
    bound = ids.coerce_id(bound_customer_id)
    if bound is not None and new_customer_id != bound:
        new_customer_id = bound
        if new_scope == Scope.GLOBAL:
            new_scope = Scope.CUSTOMER
    new = Workflow(
        name=f"{src.name} (copy)",
        scope=new_scope,
        customer_id=new_customer_id,
        project_id=src.project_id,
        org_id=getattr(src, "org_id", None),
        # Origin stamp (Phase 3): a clone INHERITS its source's origin — a
        # copy of a drafted workflow is still "draft" in origin, a copy of a
        # manual one "manual", a copy of a legacy one NULL.
        created_via=getattr(src, "created_via", None),
    )
    session.add(new)
    session.commit()
    session.refresh(new)
    for st in orchestrator.get_steps(session, workflow_id):
        session.add(
            WorkflowStep(
                workflow_id=new.id,
                order_index=st.order_index,
                name=st.name,
                step_type=st.step_type,
                provider=st.provider,
                model=st.model,
                fallback_provider=st.fallback_provider,
                fallback_model=st.fallback_model,
                # SPEC-prompt-template-runtime: carry the bound PromptTemplate onto the clone
                # or the copy silently loses its saved-template binding (copy_to_pairs already
                # copies it; clone_workflow used to drop it -- now the FK survives a deep copy).
                prompt_template_id=st.prompt_template_id,
                prompt_text=st.prompt_text,
                chunk_policy=st.chunk_policy,
                retry_max=st.retry_max,
                backoff_base=st.backoff_base,
                redact_pii=st.redact_pii,
                assignee_email=getattr(st, "assignee_email", ""),
                adaptive_feedback=getattr(st, "adaptive_feedback", False),
                adaptive_feedback_window=getattr(st, "adaptive_feedback_window", 5),
                doc_context_prepass=getattr(st, "doc_context_prepass", False),
                qe_gate=getattr(st, "qe_gate", False),
                qe_threshold=getattr(st, "qe_threshold", 70),
                qe_provider=getattr(st, "qe_provider", None),
                # QE auto-fix (Train B): step config like any other; survives a deep copy.
                qe_autofix=getattr(st, "qe_autofix", False),
                qe_autofix_provider=getattr(st, "qe_autofix_provider", None),
                qe_autofix_max_rounds=getattr(st, "qe_autofix_max_rounds", 3),
                qe_autofix_cross_check=getattr(st, "qe_autofix_cross_check", False),
                # feat/lqa-configurable-threshold: the lqa pass bar is step config like any
                # other and must survive a deep copy (None on a non-lqa source is inert).
                lqa_threshold=getattr(st, "lqa_threshold", None),
                tm_examples=getattr(st, "tm_examples", True),
                tm_examples_window=getattr(st, "tm_examples_window", 3),
                use_image_context=getattr(st, "use_image_context", True),  # Batch 14 T2 §25.6
                # Terminology T3 (SPEC §9-T3): clone the concept-glossary renderer toggle +
                # caps so a copied step keeps its rendering config (clone-parity pinned).
                concept_glossary_render=getattr(st, "concept_glossary_render", False),
                glossary_concept_cap=getattr(st, "glossary_concept_cap", 10),
                glossary_token_cap=getattr(st, "glossary_token_cap", 0),
                # LQA safety floor D1: the TM-protection tristate is step config like any
                # other and must survive a deep copy (the known copy-site footgun).
                tm_protect=getattr(st, "tm_protect", None),
                # run-190 Item D / M5: the over-translation guard opt-out is step config like
                # tm_protect and must survive a deep copy (same copy-site footgun).
                over_translation_guard=getattr(st, "over_translation_guard", None),
                # LLMemory step-level opt-out (PROPOSAL-llmemory §6): the memory_mode
                # tristate is step config like tm_protect and must survive a deep copy too
                # -- without this a cloned "off" step would silently flip back to "auto".
                memory_mode=getattr(st, "memory_mode", None),
                # Batch 15 T1 (SPEC-tm-match-step-config §7.3): the six tm_match config
                # overrides are step config like tm_protect and must survive a deep copy
                # (a cloned tm_match step keeps its threshold/lock modes/pool selection).
                # The pool selection copies VERBATIM; a cross-project clone's stale refs
                # fail closed at run time (§7.3), so no clone-time re-validation is needed.
                tm_fill_threshold=getattr(st, "tm_fill_threshold", None),
                tm_fuzzy_lock_mode=getattr(st, "tm_fuzzy_lock_mode", None),
                tm_lock_ice=getattr(st, "tm_lock_ice", None),
                tm_hundreds_enter_lqa=getattr(st, "tm_hundreds_enter_lqa", None),
                tm_context_floor=getattr(st, "tm_context_floor", None),
                tm_pool_selection=getattr(st, "tm_pool_selection", None),
                # SPEC §5 pre-step guard (Phase 2): carry the guard onto the clone or it
                # silently drops (the known copy-site footgun). Already-validated data.
                run_condition=getattr(st, "run_condition", None),
                # SPEC §5 Phase 3: the branch graph must clone faithfully. Keys are UNIQUE
                # PER WORKFLOW, so they copy VERBATIM into the new (distinct) workflow with
                # zero collision risk — and every branch target names a key WITHIN this same
                # cloned workflow, so the graph stays internally consistent with no remap.
                key=getattr(st, "key", None),
                branch=getattr(st, "branch", None),
                max_visits=getattr(st, "max_visits", None),
                # SPEC-parallel-routing §5.6: the parallel-group slug must clone with the
                # step or the graph silently FLATTENS -- a cloned workflow would run its
                # lanes sequentially with no error and no warning, the exact class of
                # copy-site drift glossary_id and needs_review already demonstrated. Group
                # slugs are scoped to ONE workflow (membership is "consecutive steps of
                # THIS workflow sharing the slug"), so they copy verbatim with zero
                # collision risk, exactly like `key`.
                parallel_group=getattr(st, "parallel_group", None),
                # PROPOSAL-external-processing-step Wave A: the external step's vendor
                # selection + per-step wait override are step config like glossary_id/
                # parallel_group and must survive a deep copy -- without this a cloned external
                # step silently loses its vendor (and fails fail-closed at run time). vendor_id
                # is UNIQUE per vendor.id (a global row, not workflow-scoped), so it copies
                # verbatim with zero collision risk.
                vendor_id=getattr(st, "vendor_id", None),
                external_wait_hours=getattr(st, "external_wait_hours", None),
                # Glossary pin (Phase 5-B): copy_to_pairs already carries this (the known
                # copy-site footgun -- see its comment) but clone_workflow used to drop it,
                # so a whole-workflow clone silently lost a pinned glossary. Full step-config
                # fidelity for ALL callers.
                glossary_id=getattr(st, "glossary_id", None),
                # A human-set "flag for linguist review" (workflow_author_review.html
                # checkbox -> setup_author.py) is step config like any other and must
                # survive a deep copy -- copy_to_pairs' adapt=False branch already carries
                # it VERBATIM from the source step; clone_workflow used to silently reset
                # it to False on every whole-workflow clone.
                needs_review=bool(getattr(st, "needs_review", False)),
            )
        )
    session.commit()
    # #356 phase 2b: a copy keeps its source's live system-prompt overrides (re-validated).
    # Nothing is read or written while the feature flag is off.
    from app.services import system_prompt_overrides

    system_prompt_overrides.copy_workflow_overrides(session, workflow_id, new.id)
    return new


def workflow_active_only(stmt):
    """Append the ACTIVE filter (`archived_at IS NULL`) to a `select(Workflow)` — the
    single seam a default LISTING / PICKER / dispatch resolution routes through so no
    surface drifts on the archive rule. A management/name-resolution read deliberately
    does NOT call this (it shows archived rows so history still resolves the name)."""
    return stmt.where(Workflow.archived_at.is_(None))


def workflow_is_active(wf: "Workflow | None") -> bool:
    """True when the workflow exists and is not archived."""
    return wf is not None and getattr(wf, "archived_at", None) is None


_ACTIVE_RUN_STATUSES = (
    RunStatus.PENDING,
    RunStatus.QUEUED,
    RunStatus.RUNNING,
    RunStatus.PAUSED,
)


# In-flight run statuses that BLOCK a workflow delete: a pending/running run is
# executing, a QUEUED one is a fan-out launch waiting for a slot (gate A round 3 — it
# will execute this workflow, so deleting it out from under the queue is the same defect
# as deleting it under a running run), and a paused run is mid-workflow waiting on a human
# step (resume would break if the workflow vanished). Terminal runs (completed/failed) are
# OWNED history and cascade with the workflow.
def workflow_reference_count(session: Session, workflow_id: int) -> dict:
    """Itemize what references a workflow, split into BLOCKERS (fail the delete),
    WARNINGS (dangling FK-less refs — re-validated at apply time, not blockers), and
    OWNED children (cascade with the workflow). Never mutates.

    BLOCKERS (independent live referrers, general rule "fail closed"):
      * routing_rules   — RoutingRule.workflow_id (a rule would route to nothing)
      * customer_defaults — Customer.default_workflow_id (a customer's scoped default)
      * active_runs     — a pending/running/paused WorkflowRun on this workflow, OR an
        in-flight MEMBER run of a fan-out batch OF this workflow whose own workflow_id is
        a sibling VARIANT (resolve_variant picks a per-locale variant, so a batch this
        workflow dispatched can carry member runs owned by another workflow). delete_workflow
        reaps those batches and un-scopes their surviving members, so a running one must fail
        the delete closed too; each run is counted once (a same-workflow member is both).
      * live_batches    — a WorkflowBatch OF this workflow with finished_at IS NULL (the
        repo's batch-liveness authority is WorkflowBatch.finished_at, owned end-to-end by
        batch_runs: finalize stamps it (batch_runs._finalize), batch_runs.batch_status
        reads it as the terminal DISPLAY authority, batch_runs.recover_interrupted_batches
        selects finished_at IS NULL as the interrupted set to heal at startup, and
        batch_runs.rerun_batch REOPENs by clearing it before re-dispatch; retention never
        reads it). Between dispatch_batch's COMMIT of the batch row and its dispatcher
        spawning the first member (and between members when a locale is skipped) the batch
        exists with ZERO member runs, so the active-member count alone reads 0 -- reaping it
        there would make the dispatcher's next _create_member hit a vanished-parent
        IntegrityError and silently drop the user's whole fan-out. A finished_at-NULL batch
        is in-flight; block until it stamps.
      * group_default   — this workflow is a group's is_default fallback AND the group
        still has OTHER variants that would lose their dispatch fallback. (The schema
        has no WorkflowGroup.default_* column; the group default is Workflow.is_default,
        so "the group's default pointer" maps to this condition.)

    WARNINGS (deliberately FK-less, dangle by design, re-validated at apply/run time):
      * template_defaults — ProjectTemplate.default_workflow_id (spec: warning only)
      * project_defaults  — Project.default_workflow_id (FK-less; UI pre-select only)

    SEPARATE FAIL-CLOSED GUARDS (NOT summed into `blockers`, each its own delete_workflow
    raise with an actionable message, mirroring the empty-held-marker guard):
      * memory_suggestions — MemorySuggestion.workflow_id (NOT NULL) rows targeting this
        workflow. LAW 2 keeps them as permanent audit tombstones (never hard-deleted, even
        by GDPR purge, which only blanks content columns), so they can be neither reaped nor
        un-scoped; the workflow must be ARCHIVED instead of deleted while any exist.
      * vendor_dispatches — VendorDispatch.workflow_run_id (NOT NULL) legs anchored to one
        of this workflow's runs (#137). A leg is the BUYER's durable commercial record (the
        claim token that admits a vendor return, plus the estimate the vendor's actual cost
        reconciles against), so it can be neither reaped (it outlives the run by design) nor
        un-scoped (NOT NULL). Same resolution as a suggestion tombstone: ARCHIVE the workflow.

    OWNED (hand-ordered cascade): steps, runs, step_runs, workflow_batches.

    PREVIEW FORECASTS (un-scope outcomes, not blockers — the same keys delete_workflow
    step 4b/4c re-stamps with the ACTUAL counts, for preview/outcome symmetry):
      * batch_members_unscoped — sibling-variant member runs of this workflow's batches
        that survive step 4b with batch_id nulled.
      * eval_candidates_unscoped — EvalCandidate.workflow_id rows un-scoped at step 4c.
      * memory_events_unscoped — MemoryEvent.workflow_run_id rows on this workflow's runs,
        un-scoped (nullable FK, row preserved) at the new step 2b (#137)."""
    wf = session.get(Workflow, workflow_id)
    routing_rules = len(
        session.exec(select(RoutingRule.id).where(RoutingRule.workflow_id == workflow_id)).all()
    )
    customer_defaults = len(
        session.exec(select(Customer.id).where(Customer.default_workflow_id == workflow_id)).all()
    )
    # This workflow's OWN in-flight runs, PLUS in-flight member runs of a batch OF this
    # workflow whose own workflow_id is a sibling variant (see the docstring). Collected as
    # a set of ids so a same-workflow member (both a direct run and a batch member) counts
    # once. delete_workflow reaps those batches + un-scopes surviving members, so a running
    # variant member would be silently un-scoped mid-flight -- it must block the delete.
    _active_ids = set(
        session.exec(
            select(WorkflowRun.id).where(
                WorkflowRun.workflow_id == workflow_id, WorkflowRun.status.in_(_ACTIVE_RUN_STATUSES)
            )
        ).all()
    )
    _batch_ids = list(
        session.exec(select(WorkflowBatch.id).where(WorkflowBatch.workflow_id == workflow_id)).all()
    )
    if _batch_ids:
        _active_ids |= set(
            session.exec(
                select(WorkflowRun.id).where(
                    WorkflowRun.batch_id.in_(_batch_ids),
                    WorkflowRun.status.in_(_ACTIVE_RUN_STATUSES),
                )
            ).all()
        )
    active_runs = len(_active_ids)
    # live_batches: an in-flight (finished_at IS NULL) batch of this workflow blocks even
    # when it has ZERO member runs yet -- the dispatch-commit-before-first-member window
    # (see the docstring). workflow_batches is the OWNED total reaped at delete step 4b.
    workflow_batches = len(_batch_ids)
    # LOW-5: key liveness on workflow_id directly (equivalent to `id IN _batch_ids AND
    # finished_at IS NULL`, since _batch_ids IS every batch of this workflow) so a workflow with
    # a large batch history binds ONE parameter, not one per batch (SQLITE_MAX_VARIABLE_NUMBER /
    # Postgres 65535). No empty-list guard needed -- a direct equality is safe on zero rows.
    live_batches = len(
        session.exec(
            select(WorkflowBatch.id).where(
                WorkflowBatch.workflow_id == workflow_id,
                WorkflowBatch.finished_at.is_(None),
            )
        ).all()
    )
    # memory_suggestions: LAW 2 tombstones (models.MemorySuggestion) targeting this workflow.
    # Not reapable / un-scopable (see the docstring) -> a SEPARATE fail-closed guard, so it is
    # counted here for the preview but kept OUT of the `blockers` sum (its own raise wording).
    memory_suggestions = len(
        session.exec(
            select(MemorySuggestion.id).where(MemorySuggestion.workflow_id == workflow_id)
        ).all()
    )
    # LOW-4: preview/outcome SYMMETRY. delete_workflow step 4b/4c stamps the ACTUAL counts it
    # un-scoped under these same two keys (batch_members_unscoped / eval_candidates_unscoped);
    # forecast them here (nothing mutated) so a --force-less preview reports what the delete
    # WILL un-scope, not just what it will cascade. batch_members_unscoped = members of THIS
    # workflow's batches owned by a SIBLING variant -- this workflow's own members die at step
    # 3, so exactly the siblings survive step 4b to have batch_id nulled (so the forecast
    # equals the actual). eval_candidates_unscoped = EvalCandidate rows pinned to this workflow
    # (nullable FK, un-scoped at step 4c, row preserved).
    batch_members_unscoped = (
        len(
            session.exec(
                select(WorkflowRun.id).where(
                    WorkflowRun.batch_id.in_(_batch_ids),
                    WorkflowRun.workflow_id != workflow_id,
                )
            ).all()
        )
        if _batch_ids
        else 0
    )
    eval_candidates_unscoped = len(
        session.exec(select(EvalCandidate.id).where(EvalCandidate.workflow_id == workflow_id)).all()
    )
    group_default = 0
    if wf is not None and wf.group_id is not None and wf.is_default:
        group_default = len(
            session.exec(
                select(Workflow.id).where(
                    Workflow.group_id == wf.group_id, Workflow.id != workflow_id
                )
            ).all()
        )
    template_defaults = len(
        session.exec(
            select(ProjectTemplate.id).where(ProjectTemplate.default_workflow_id == workflow_id)
        ).all()
    )
    project_defaults = len(
        session.exec(select(Project.id).where(Project.default_workflow_id == workflow_id)).all()
    )
    run_ids = list(
        session.exec(select(WorkflowRun.id).where(WorkflowRun.workflow_id == workflow_id)).all()
    )
    steps = len(
        session.exec(select(WorkflowStep.id).where(WorkflowStep.workflow_id == workflow_id)).all()
    )
    step_runs = (
        len(session.exec(select(StepRun.id).where(StepRun.workflow_run_id.in_(run_ids))).all())
        if run_ids
        else 0
    )
    # #137 — the two remaining FKs onto workflowrun.id (StepRun is OWNED, DeliveryEmptyApproval
    # is un-scoped at delete step 2a). Both are keyed on THIS workflow's run ids:
    #   * memory_events_unscoped: NULLABLE, so the event survives its run un-scoped (a preview
    #     forecast whose actual count delete_workflow step 2b re-stamps under the same key);
    #   * vendor_dispatches: NOT NULL and durable -> a SEPARATE fail-closed guard, kept OUT of
    #     the `blockers` sum (like memory_suggestions) so its raise can word its own way out.
    memory_events_unscoped = (
        len(
            session.exec(
                select(MemoryEvent.id).where(MemoryEvent.workflow_run_id.in_(run_ids))
            ).all()
        )
        if run_ids
        else 0
    )
    vendor_dispatches = (
        len(
            session.exec(
                select(VendorDispatch.id).where(VendorDispatch.workflow_run_id.in_(run_ids))
            ).all()
        )
        if run_ids
        else 0
    )
    blockers = routing_rules + customer_defaults + active_runs + group_default + live_batches
    return {
        "routing_rules": routing_rules,
        "customer_defaults": customer_defaults,
        "active_runs": active_runs,
        "live_batches": live_batches,
        "group_default": group_default,
        "template_defaults": template_defaults,
        "project_defaults": project_defaults,
        "memory_suggestions": memory_suggestions,
        "vendor_dispatches": vendor_dispatches,
        "memory_events_unscoped": memory_events_unscoped,
        "steps": steps,
        "runs": len(run_ids),
        "step_runs": step_runs,
        "workflow_batches": workflow_batches,
        "batch_members_unscoped": batch_members_unscoped,
        "eval_candidates_unscoped": eval_candidates_unscoped,
        "blockers": blockers,
        "warnings": template_defaults + project_defaults,
    }


def _empty_held_marker_orphans(session: Session, workflow_id: int) -> list:
    """Batch 15 T2 (B-H1): the (document_id, locale) pairs that would be UN-HELD by deleting
    this workflow -- currently empty-HELD AND provenanced ONLY by this workflow's runs.

    For each (doc, locale) this workflow's runs stamped `used_tm_match`, the delete is unsafe
    iff the target is currently HOLD and NO OTHER workflow's run carries a surviving marker
    for it. An approved target is RELEASE (not HOLD), so it is never returned -- its delete
    proceeds with the approval preserved (B-H2). Returns [] on any pure-document /
    no-tm_match workflow, so this check is inert for the overwhelming majority of deletes."""
    from sqlalchemy import or_ as _or_

    from app.models import Document, Project, Segment, WorkflowRun
    from app.services import tm_delivery

    marked = session.exec(
        select(WorkflowRun.document_id, WorkflowRun.target_locale).where(
            WorkflowRun.workflow_id == workflow_id,
            WorkflowRun.used_tm_match == True,  # noqa: E712
        )
    ).all()
    # B2-M2: a WILDCARD ("" target_locale) marked run holds EVERY locale the doc carries
    # segments for (_doc_provenanced honors it for any locale via has_wild), so expand it to
    # those concrete locales -- dropping "" runs (the old `if l_`) let a wildcard-marked held
    # doc be silently un-held by the delete.
    pairs: set = set()
    _doc_locs: dict = {}
    for doc_id, l_ in marked:
        if l_:
            pairs.add((doc_id, l_))
            continue
        if doc_id not in _doc_locs:
            _doc_locs[doc_id] = {
                x
                for x in session.exec(
                    select(Segment.target_locale).where(Segment.document_id == doc_id).distinct()
                ).all()
                if x
            }
        for dl in _doc_locs[doc_id]:
            pairs.add((doc_id, dl))
    out: list = []
    for doc_id, loc in pairs:
        doc = session.get(Document, doc_id)
        if doc is None:
            continue
        # A key-INTERNAL document (fmt=="keys") is gated in KEY mode (the whole-project export),
        # NOT doc mode -- evaluating it as a document would spuriously HOLD an APPROVED key
        # project (its approval is document_id-NULL). Use the right decision + survivor scope.
        if getattr(doc, "fmt", None) == "keys":
            proj = session.get(Project, doc.project_id)
            # R3-MED-1: evaluate at the WIDEST universe (include_archived/hidden=True) -- the
            # export/approve/review surfaces all moved there, so a DEFAULT (non-archived) check
            # here would miss an archived-only-empty key project and let the workflow delete
            # erase its marker, then ship "Archived English" un-approved at ?archived=1.
            if (
                proj is None
                or tm_delivery.empty_gate_decision(
                    session, project=proj, locale=loc, include_archived=True, include_hidden=True
                )
                != tm_delivery.HOLD
            ):
                continue
            key_doc_ids = list(
                session.exec(
                    select(Document.id).where(
                        Document.project_id == proj.id, Document.fmt == "keys"
                    )
                ).all()
            )
            other = session.exec(
                select(WorkflowRun.id).where(
                    WorkflowRun.document_id.in_(key_doc_ids),
                    WorkflowRun.used_tm_match == True,  # noqa: E712
                    WorkflowRun.workflow_id != workflow_id,
                    _or_(WorkflowRun.target_locale == loc, WorkflowRun.target_locale == ""),
                )
            ).first()
        else:
            if tm_delivery.empty_gate_decision(session, doc=doc, locale=loc) != tm_delivery.HOLD:
                continue  # not held (delivered / approved / no empties) -> deleting is safe
            # Would ANY marker survive the delete? (another workflow's run for this doc+locale)
            other = session.exec(
                select(WorkflowRun.id).where(
                    WorkflowRun.document_id == doc_id,
                    WorkflowRun.used_tm_match == True,  # noqa: E712
                    WorkflowRun.workflow_id != workflow_id,
                    _or_(WorkflowRun.target_locale == loc, WorkflowRun.target_locale == ""),
                )
            ).first()
        if other is None:
            out.append((doc_id, loc))
    return out


def get_prompt_template(session: Session, template_id: int) -> PromptTemplate | None:
    return session.get(PromptTemplate, template_id)
