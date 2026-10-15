"""workflows: part 3 (layered pure move)

Documented in: docs/architecture/workflow-engine.md
"""

import logging

from sqlmodel import Session, select

from app.models import (
    EvalCandidate,
    PromptTemplate,
    Scope,
    StepRun,
    StepType,
    Workflow,
    WorkflowBatch,
    WorkflowGroup,
    WorkflowRun,
    WorkflowStep,
)
from app.services import orchestrator
from app.services.workflows_common import (
    _BRANCH_SLOTS,
    WorkflowError,
    _clamp_lqa_threshold,
    _clamp_threshold,
    _coerce_glossary_cap,
    _coerce_pt_id,
    _coerce_tm_fuzzy_lock_mode,
    _coerce_tm_pool_selection,
    _coerce_tm_scalar_int,
    _empty_held_marker_orphans,
    _FieldLabels,
    _is_step_ref,
    _step_attr,
    _step_produces_gate,
    slugify_key,
    workflow_reference_count,
)
from app.services.workflows_part2 import (
    _ADAPT_PLACEHOLDERS,
    VariantMatch,
    _adaptation_prompt,
    _coerce_tm_tristate_bool,
    _existing_keys,
    _is_pair_atom,
    _ordered,
    _pair,
    _pgroup,
    _rc_ref_qualifies,
    _same_pair,
    _step_ref_key,
    _validate_branch_target,
    group_reference_count,
    group_variants,
    parse_branch_str,
    prompt_template_reference_count,
    variant_visible,
)

log = logging.getLogger("loc_tms.workflows")


def _ref_resolves(ref: str, prior_steps) -> bool:
    """A run_condition ref RESOLVES iff an earlier step (lower order_index) of the
    producing type exists: an LQA step for prev_lqa, a qe_gate=True step for prev_qe.
    For the `step:<key>` form (SPEC-parallel-routing §5.5) an earlier step must carry
    that exact key AND be gate-producing -- a ref to a step that never writes a
    gate/score could only ever fail open at run time, so it is refused at save time."""
    if _is_step_ref(ref):
        key = _step_ref_key(ref)
        if not key:
            return False
        return any(
            _step_attr(s, "key") == key
            and _step_produces_gate(_step_attr(s, "step_type"), _step_attr(s, "qe_gate", False))
            for s in prior_steps
        )
    if ref in ("prev_lqa", "prev_qe"):
        return any(_rc_ref_qualifies(s, ref) for s in prior_steps)
    return False


def _group_span(ordered, idx):
    """(first, last) INCLUSIVE indices of the maximal run of CONSECUTIVE steps around
    `idx` sharing its parallel_group slug. (idx, idx) for a step in no group.

    Consecutiveness is the whole design (SPEC-parallel-routing §4): it is what keeps
    every prefix-shaped predicate in this module correct -- "prior" for a group member
    is `ordered[:first]`, still a prefix."""
    g = _pgroup(ordered[idx])
    if not g:
        return idx, idx
    first = idx
    while first > 0 and _pgroup(ordered[first - 1]) == g:
        first -= 1
    last = idx
    while last + 1 < len(ordered) and _pgroup(ordered[last + 1]) == g:
        last += 1
    return first, last


def _pre_group_prior(prior_steps, parallel_group):
    """The steps a GROUP MEMBER's run_condition may reference: everything strictly
    BEFORE its group (SPEC-parallel-routing §5.1.4).

    `prior_steps` is the ordered prefix that precedes the step being validated, so the
    step's own earlier group members are exactly the TRAILING run sharing its slug --
    trimming them is what makes "a member referencing another member" a refusal rather
    than a silently-racy read (at fork time no member has produced anything yet). A
    step in no group keeps the full prefix -> byte-identical to pre-parallel."""
    if not parallel_group:
        return prior_steps
    out = _ordered(prior_steps)
    while out and _pgroup(out[-1]) == parallel_group:
        out.pop()
    return out


def validate_key(key, prior_steps):
    """Slugify + uniqueness-check a step key (D1) against the workflow's existing steps.
    Returns the clean slug, or None for a blank key (an unnamed step — byte-identical to
    today). Raises WorkflowError on a duplicate or a non-blank input that slugifies to
    empty (so a typo'd key is surfaced, never silently dropped)."""
    if key is None:
        return None
    raw = str(key).strip()
    if not raw:
        return None
    slug = slugify_key(raw)
    if not slug:
        raise WorkflowError(
            f"step key {key!r} is not a valid slug (use letters, digits, '-' or '_')."
        )
    if slug in _existing_keys(prior_steps):
        raise WorkflowError(
            f"step key {slug!r} is already used in this workflow (keys must be unique)."
        )
    return slug


def _group_runs(ordered):
    """Every maximal run of CONSECUTIVE same-slug steps, as (slug, first, last) triples
    in spine order. Two runs sharing a slug = a SPLIT group (rule §5.1.1)."""
    runs = []
    i = 0
    while i < len(ordered):
        g = _pgroup(ordered[i])
        if not g:
            i += 1
            continue
        j = i
        while j + 1 < len(ordered) and _pgroup(ordered[j + 1]) == g:
            j += 1
        runs.append((g, i, j))
        i = j + 1
    return runs


def parse_branch_flags(items) -> dict | None:
    """Turn the repeatable `on_pass=<t>`/`on_fail=<t>` branch flag surface (the CLI
    `--branch` list AND the `/api/.../steps` `branch` body array) into the branch dict
    {on_pass, on_fail} `add_step`/`update_step` validate. Shared by the CLI and the
    /api routes (house parity law) so the flag surface parses IDENTICALLY on both.

    A target of 'next'/'end' is the sentinel string; any other value is a step KEY ->
    {'step': key}. Raises WorkflowError (fail-closed) on a malformed 'slot=value' shape
    or a target that still carries a 'step:' prefix. Returns None when no flag was given
    (no branch)."""
    out: dict = {}
    for raw in items or []:
        if "=" not in str(raw):
            raise WorkflowError(f"branch {raw!r} must be 'on_pass=<target>' or 'on_fail=<target>'.")
        slot, _, target = str(raw).partition("=")
        slot = slot.strip()
        if slot not in ("on_pass", "on_fail"):
            raise WorkflowError(f"branch slot {slot!r} must be on_pass or on_fail.")
        target = target.strip()
        # A target is a BARE step key (e.g. 'fix'), 'next', or 'end'. Writing the stored
        # '{step: key}' form as 'step:fix' would otherwise have the colon slugified away
        # and the downstream error would name a 'stepfix' nobody typed -- echo the exact
        # input and point at the right syntax instead.
        if ":" in target:
            raise WorkflowError(
                f"branch {slot}={target!r}: a branch target is a bare step key "
                f"(e.g. 'fix'), 'next', or 'end' -- drop the 'step:' prefix."
            )
        out[slot] = parse_branch_str(target)
    return out or None


def validate_branch(branch, step_type, qe_gate, prior_steps, self_key=None, parallel_group=None):
    """Validate + sanitize a step's post-step branch (SPEC §5.4, D3). Returns a clean
    {'on_pass', 'on_fail'} dict (only the slots actually set), or None for no branch.
    Raises WorkflowError, fail-closed, on:
      - a branch on a NON-gate step (D3: needs an LQA step or QE gate ON);
      - keys other than on_pass/on_fail;
      - a target that is not 'next'/'end'/an existing step key (loop-back targets an
        EARLIER key; `self_key` is a legal self-loop target except for LQA steps);
      - an LQA self-loop (a check scores text but cannot change it) -- UNLESS the step is a
        parallel-group member, where its own key is a GROUP ENTRY rather than a self-loop
        (#161; see the `parallel_group` note below).

    `branch` may be a dict, or None/blank. `prior_steps` are the steps that precede this
    one (so a loop-back target must already exist — the builder only offers existing
    keys, and forward jumps to not-yet-created steps are unsupported by design)."""
    if branch is None:
        return None
    if not isinstance(branch, dict):
        raise WorkflowError("branch must be an object {on_pass, on_fail}.")
    # Drop blank-string slot values (the web form submits "" for an unset slot).
    branch = {k: v for k, v in branch.items() if not (isinstance(v, str) and not v.strip())}
    if not branch:
        return None
    extra = set(branch) - set(_BRANCH_SLOTS)
    if extra:
        raise WorkflowError(
            f"branch has unrecognized keys: {sorted(extra)} (only on_pass/on_fail)."
        )
    if not _step_produces_gate(step_type, qe_gate):
        raise WorkflowError(
            "a branch needs a gate-producing step: an LQA step, or an llm_agent step "
            "with the QE gate ON (D3). Enable the QE gate, use an LQA step, or drop the "
            "branch."
        )
    keys = set(_existing_keys(prior_steps))
    if self_key:
        keys.add(self_key)
    clean = {}
    for slot in _BRANCH_SLOTS:
        if slot in branch:
            clean[slot] = _validate_branch_target(branch[slot], keys)
            # SLUGIFY both sides: _validate_branch_target returns the target's key in
            # SLUG form, so comparing it against a raw self_key would miss (that shape
            # already fails closed on the unknown-key arm, but with a message about the
            # wrong thing). One normalization, one meaning.
            # #161: a PARALLEL-GROUP member's own key is not a self-loop. `_group_errors`
            # resolves a branch target inside a group to that group's span and permits
            # exactly one shape -- a target that is the group's FIRST step, which re-runs
            # the WHOLE group (siblings included, and siblings DO change text). That rule
            # was adversarially reviewed (part4 MEDIUM-3) and it already refuses the unsafe
            # shape, a target that is a NON-FIRST member. Firing this guard on a group
            # member too gave two guards contradictory opinions about the same graph, and
            # the stricter one won at authoring time: `verify_parallel_routing` could not
            # build its fixture at all. Membership defers to the rule that owns it.
            if (
                step_type == StepType.LQA
                and not parallel_group
                and clean[slot] == {"step": slugify_key(self_key)}
            ):
                raise WorkflowError(
                    f"branch {slot} cannot target this LQA step's own key {self_key!r}: "
                    "a check scores text, it does not change it, so a check that loops "
                    "back into itself re-judges text nothing has altered and can only "
                    "repeat its verdict. Loop back to the step that fixes the text instead."
                )
    return clean or None


_DEAD_FIELD_LABELS = _FieldLabels(
    {
        "provider": "provider",
        "model": "model",
        "fallback_provider": "fallback provider",
        "fallback_model": "fallback model",
        "prompt_text": "prompt",
        "prompt_template_id": "prompt template",
        "chunk_policy": "chunking",
        "redact_pii": "PII redaction",
        "glossary_id": "glossary pin",
        "adaptive_feedback": "adaptive feedback",
        "doc_context_prepass": "document-context pre-pass",
        "tm_examples": "TM few-shot examples",
        "use_image_context": "image translation context",
        "concept_glossary_render": "concept-glossary renderer",
        "glossary_concept_cap": "glossary concept cap",
        "glossary_token_cap": "glossary token cap",
        "qe_gate": "QE routing gate",
        "qe_threshold": "QE threshold",
        "qe_provider": "QE judge engine",
        "qe_autofix": "QE auto-fix",
        "qe_autofix_provider": "QE auto-fix post-editor",
        "qe_autofix_max_rounds": "QE auto-fix max rounds",
        "qe_autofix_cross_check": "QE auto-fix cross-check",
        "lqa_threshold": "LQA pass threshold",
        "tm_protect": "TM protection",
        "over_translation_guard": "over-translation guard",
        "memory_mode": "LLMemory recall opt-out",
        "parallel_group": "parallel group",
        "vendor_id": "vendor",
        "external_wait_hours": "external wait window",
        "tm_fill_threshold": "TM fill threshold",
        "tm_fuzzy_lock_mode": "TM fuzzy lock mode",
        "tm_lock_ice": "TM lock in-context matches",
        "tm_hundreds_enter_lqa": "TM send 100% matches to LQA",
        "tm_context_floor": "TM context-attach floor",
        "tm_pool_selection": "TM pool selection",
    }
)


_DEAD_FIELD_HINT = {
    StepType.LLM_AGENT: (
        "Phase 1 of parallel steps supports parallel CHECKS only -- the members of a "
        "parallel group must be lqa steps, because a check only READS the working text "
        "and needs no merge rule. Put the group on your lqa steps instead."
    ),
    StepType.LQA: (
        "an LQA step scores with a fixed internal MQM judge prompt + the pair "
        "rulebook (and the resolved style guide, when one applies) -- only "
        "provider, model, fallback_provider, fallback_model, redact_pii, "
        "run_condition, branch, max_visits, and key apply."
    ),
    StepType.HUMAN_REVIEW: (
        "a human_review step makes no provider call at all -- only "
        "assignee_email, run_condition, max_visits, and key apply."
    ),
    StepType.TM_MATCH: (
        "a tm_match step makes no provider call -- it fills + locks TM matches per its "
        "own config. Only its TM policy (fill threshold, fuzzy lock mode, lock_ice, "
        "hundreds_enter_lqa, context_floor, pool selection), run_condition, max_visits, "
        "and key apply."
    ),
    StepType.EXTERNAL: (
        "an external step makes no provider call -- it packages the current translation "
        "state and dispatches it to a vendor. Only vendor, external_wait_hours, "
        "run_condition, max_visits, and key apply."
    ),
}


# Batch 14 T2 (SPEC §25.5-A): llm_agent-only per-step image-context toggle.
# Terminology T3 (SPEC §9-T3): llm_agent-only concept-glossary renderer + caps.
# feat/lqa-configurable-threshold: live only on an lqa step (the reviewer pass bar).
# LQA safety floor D1: the TM provenance guard is an llm_agent-only lever (an LQA
# step scores, it never rewrites a segment; a human_review step makes no call).
# run-190 Item D / M5: the over-translation (page-dump) guard opt-out, llm_agent-only.
# LLMemory step-level opt-out (PROPOSAL-llmemory §6): llm_agent-only for the same
# reason -- an LQA step's judge prompt and a human_review step's no-call pause
# never resolve a recall block to inject.
# Stored as the exact registry DEFAULT; _FieldLabels.get rebrands it to the live
# name on access (defaults-only substitution always applies) — L8/MED-3.
# SPEC-parallel-routing §5.6: live on lqa, dead on human_review AND llm_agent.
# External/vendor dispatch (Wave A): live ONLY on an external step.
# Batch 15 T1 (SPEC-tm-match-step-config §8.A): LIVE only on a tm_match step.
def _dead_field_is_meaningful(field: str, value) -> bool:
    """True iff `value` is a REAL (non-default/no-op) setting for `field` -- shared
    by add_step (checked against the function's own defaults) and update_step
    (checked against the step's stored value) to tell "a field left at its inert
    default" apart from "a field someone actually set".

    `tm_examples` is deliberately NEVER flagged here (always "not meaningful"): its
    class default is True (ON), so an HTML checkbox is pre-CHECKED -- but an
    unchecked checkbox submits NOTHING, indistinguishable at this layer from a
    caller (a raw API/test client, not a browser) that simply never sent the field.
    Every OTHER boolean dead field defaults OFF, so "absent" already reads as its
    true default with no such ambiguity; `tm_examples` is the one outlier where
    treating an unreliable signal as a deliberate choice would refuse perfectly
    innocent requests. The builder still hides it for lqa/human_review (dead
    either way); this only affects whether the SERVER treats a False here as
    proof of intent."""
    if field in ("tm_examples", "use_image_context"):
        # Batch 14 T2 (SPEC §25.5-A): identical posture to tm_examples — class default
        # TRUE, so an HTML checkbox is pre-CHECKED and an UNCHECKED box submits NOTHING,
        # indistinguishable at this layer from an API/test caller that never sent the
        # field; treating a False here as proof of intent would refuse innocent requests
        # AND (via _force_clear_dead_fields, unguarded by this on a type switch /
        # propagation) both spuriously report "cleared" and write NULL into the NOT NULL
        # column. So an explicit use_image_context=False on a non-agent step is silently
        # INERT (accepted, ignored by the predicate), the tm_examples posture.
        return False
    if field in ("prompt_text",):
        return bool((value or "").strip())
    if field in ("model", "fallback_provider", "fallback_model", "qe_provider", "provider"):
        return bool((value or "").strip())
    if field == "chunk_policy":
        return (value or "auto") != "auto"
    if field in ("qe_autofix", "qe_autofix_cross_check"):
        return bool(value)
    if field == "qe_autofix_provider":
        return bool((value or "").strip())
    if field == "qe_autofix_max_rounds":
        from app.services import qe_autofix

        return qe_autofix.clamp_rounds(value) != qe_autofix.DEFAULT_ROUNDS
    if field in ("redact_pii", "adaptive_feedback", "doc_context_prepass", "qe_gate"):
        return bool(value)
    if field in ("glossary_id", "prompt_template_id"):
        return bool(value)
    if field in ("tm_protect", "over_translation_guard"):
        # TRISTATE: None is the inert "inherit the default"; BOTH True and False are
        # deliberate overrides, so `bool(value)` would wrongly read an explicit "off" as an
        # unset field. (over_translation_guard, run-190 M5: same tristate as tm_protect.)
        return value is not None
    if field == "memory_mode":
        # TRISTATE-BY-STRING: None/"auto" is the inert "participate normally" state;
        # only "off" is a deliberate override worth refusing on a dead-for-it step type.
        return value not in (None, "auto")
    if field == "qe_threshold":
        return _clamp_threshold(value) != 70
    if field == "lqa_threshold":
        # NULLABLE, no numeric default: any value that resolves to a real int (0-100)
        # is a deliberate choice worth refusing on a step type it is dead for. None/
        # blank/junk -> None (inherit the built-in 90) is the inert default, so a
        # non-lqa step that never set it — or a form that posts blank — is not refused.
        return _clamp_lqa_threshold(value) is not None
    if field in ("tm_lock_ice", "tm_hundreds_enter_lqa"):
        # Batch 15 T1: NULLABLE TRISTATE (like tm_protect). None (inherit the global) is
        # inert; BOTH True and False are deliberate overrides, so a coerced non-None value
        # is meaningful (worth refusing on a non-tm_match step).
        return _coerce_tm_tristate_bool(value) is not None
    if field == "tm_fuzzy_lock_mode":
        # NULLABLE: None/blank/"auto"/junk -> None (inherit); a recognised mode is a
        # deliberate override worth refusing on a non-tm_match step.
        return _coerce_tm_fuzzy_lock_mode(value) is not None
    if field in ("tm_fill_threshold", "tm_context_floor"):
        # NULLABLE int override: None/blank/junk -> None (inherit); a real int is a choice.
        return _coerce_tm_scalar_int(value) is not None
    if field == "tm_pool_selection":
        # NULLABLE list: None/blank -> None (all readable, inherit); a list (INCLUDING the
        # deliberate empty [] "select nothing") is a choice worth refusing off tm_match.
        return _coerce_tm_pool_selection(value) is not None
    if field == "glossary_concept_cap":
        # Terminology T3: NOT-NULL default 10. A value that resolves to something other than
        # the default is a deliberate choice worth refusing on a dead-for-it step type; the
        # default (or blank/junk -> default) is inert (so a full-POST edit form does not
        # refuse an innocent lqa/human_review step).
        return _coerce_glossary_cap(value, 10) != 10
    if field == "glossary_token_cap":
        # Terminology T3: NOT-NULL default 0 (= inherit the install token cap). Non-zero is
        # a deliberate choice; 0/blank/junk -> 0 is inert.
        return _coerce_glossary_cap(value, 0) != 0
    return bool(value)


def workflow_delete_block_reason(
    session: Session, workflow_id: int, wf=None, refs: dict | None = None
) -> str | None:
    """The SINGLE read-only eligibility check for deleting a workflow (LOW-2).

    Returns the human message for the FIRST guard that would block the delete, evaluated
    in the EXACT SAME ORDER ``delete_workflow`` fails closed in (permanence-first):
      1. a LAW-2 MemorySuggestion tombstone (archive-only, never clearable);
      2. a VendorDispatch leg anchored to one of this workflow's runs (durable, #137);
      3. an empty-held tm_match provenance orphan (clears only by a human decision);
      4. the transient reference blockers (routing / customer default / in-flight / live
         batch / group default).
    Returns ``None`` when the delete would proceed. Never mutates.

    Both ``delete_workflow`` (which raises the returned reason as a ``WorkflowError``) and
    the ``scripts/showcase_seed`` ``--dry-run`` forecast consult THIS one function, so the
    preview can never drift from the outcome -- the dry-run refuses exactly where the live
    delete does, including on the empty-held guard the earlier forecast silently omitted."""
    if wf is None:
        wf = session.get(Workflow, workflow_id)
    if wf is None:
        return f"workflow {workflow_id} not found."
    if refs is None:
        refs = workflow_reference_count(session, workflow_id)
    # (1) LAW-2 MemorySuggestion tombstone -- never reapable/un-scopable, archive instead.
    if refs["memory_suggestions"] > 0:
        from app import branding

        return (
            f"cannot delete workflow {workflow_id} ({wf.name!r}): "
            f"{refs['memory_suggestions']} {branding.llmemory_name()} improvement "
            "suggestion(s) reference it as their apply target, and those are permanent audit "
            "records that are never deleted. Archive the workflow instead of deleting it "
            "(archive keeps its history, removes it from pickers and dispatch, and is "
            "reversible)."
        )
    # (2) #137: a VendorDispatch leg (NOT NULL FK) is a durable commercial record -- archive.
    if refs["vendor_dispatches"] > 0:
        return (
            f"cannot delete workflow {workflow_id} ({wf.name!r}): "
            f"{refs['vendor_dispatches']} vendor dispatch leg(s) are anchored to its run "
            "history, and those are durable commercial records (the claim token that admits "
            "a vendor return and the estimate its actual cost reconciles against) that are "
            "never deleted with a workflow. Archive the workflow instead of deleting it "
            "(archive keeps its history, removes it from pickers and dispatch, and is "
            "reversible)."
        )
    # (3) empty-held tm_match provenance orphan -- deleting would silently un-hold it.
    _held_orphans = _empty_held_marker_orphans(session, workflow_id)
    if _held_orphans:
        _pairs = ", ".join(f"doc {d} [{loc}]" for d, loc in _held_orphans[:20])
        return (
            f"cannot delete workflow {workflow_id} ({wf.name!r}): it holds the only "
            f"tm_match provenance marker for {len(_held_orphans)} empty-held deliverable(s) "
            f"({_pairs}) -- deleting it would silently un-hold them for source-filled "
            "delivery. Approve the empty delivery (deliver-approve) or translate/clear the "
            "empties first."
        )
    # (4) transient/reassignable reference blockers, surfaced LAST.
    if refs["blockers"] > 0:
        return (
            "cannot delete workflow %d (%r): still referenced by %d routing rule(s), "  # noqa: UP031
            "%d customer default(s), %d in-flight run(s) or fan-out batch member(s), and "
            "%d live fan-out batch(es); and it is the fallback default of a group with %d "
            "other variant(s). Reassign/deactivate the routing rules, clear the customer "
            "default(s), let the run(s) and fan-out batch(es) finish, and reassign the "
            "group default first. (In-flight runs here include per-locale batch members "
            "owned by sibling variant workflows, which do not appear on this workflow's "
            "own run list.)"
            % (
                workflow_id,
                wf.name,
                refs["routing_rules"],
                refs["customer_defaults"],
                refs["active_runs"],
                refs["live_batches"],
                refs["group_default"],
            )
        )
    return None


def delete_workflow(session: Session, workflow_id: int) -> dict:
    """Delete a workflow, cascading its OWNED children (WorkflowStep + WorkflowRun +
    StepRun + the WorkflowBatch fan-out rows it dispatched, plus an un-scope of any
    Lab EvalCandidate.workflow_id and any MemoryEvent.workflow_run_id) hand-ordered
    child-before-parent (no ondelete=; FKs enforced globally).
    FAILS CLOSED (WorkflowError with the itemized count) while any BLOCKER holds it
    (routing rule / customer default / in-flight run or fan-out batch member / live
    fan-out batch / group-default-with-siblings), or while any LAW-2 MemorySuggestion
    tombstone targets it, or while any VendorDispatch leg is anchored to one of its runs
    (each a separate guard: archive the workflow instead of deleting it).

    #137 INVARIANT: no IntegrityError ever escapes this function for an FK onto
    workflowrun.id or workflow.id. Every such FK in app/models.py is handled explicitly --
    StepRun (reaped), DeliveryEmptyApproval.run_id (un-scoped), MemoryEvent.workflow_run_id
    (un-scoped), VendorDispatch.workflow_run_id (fail-closed guard). A NEW FK onto either
    parent MUST be added here and to workflow_reference_count.

    Returns the reference dict describing what was destroyed / un-scoped (for the audit
    log): adds workflow_batches (reaped), batch_members_unscoped, eval_candidates_unscoped
    and memory_events_unscoped to the workflow_reference_count keys."""
    wf = session.get(Workflow, workflow_id)
    if wf is None:
        raise WorkflowError(f"workflow {workflow_id} not found.")
    refs = workflow_reference_count(session, workflow_id)
    # GUARD ORDER = PERMANENCE, least-resolvable FIRST (LOW-3): a caller must never be told to
    # WAIT for a transient blocker while a more-fundamental obstruction it can never clear
    # stands behind it. (1) a LAW-2 MemorySuggestion tombstone is never clearable (archive-
    # only); (1b) a #137 VendorDispatch leg is a durable commercial record (archive-only);
    # (2) an empty-held marker orphan clears only by an explicit human decision or a data
    # change, never by waiting; (3) the generic blockers include the "just let it finish"
    # cases (active runs / live batches), so they surface LAST. All four live in the ONE
    # read-only eligibility check `workflow_delete_block_reason` (LOW-2), so this raise and
    # the showcase-seed --dry-run forecast can never disagree about what would refuse.
    _reason = workflow_delete_block_reason(session, workflow_id, wf, refs)
    if _reason is not None:
        raise WorkflowError(_reason, refs)
    run_ids = list(
        session.exec(select(WorkflowRun.id).where(WorkflowRun.workflow_id == workflow_id)).all()
    )
    # B-H2: a DeliveryEmptyApproval whose run_id points at one of THIS workflow's runs would
    # FK-block the WorkflowRun delete (and the approval must SURVIVE -- it is the human's
    # durable decision that keeps an APPROVED target releasing). NULL the run_id first,
    # mirroring retention.unscope_memory_event_runs (and mirrored back by
    # retention._reap_document_children's own run-anchored approval un-scope). The approval
    # row itself is preserved.
    if run_ids:
        from app.models import DeliveryEmptyApproval

        for _dea in session.exec(
            select(DeliveryEmptyApproval).where(DeliveryEmptyApproval.run_id.in_(run_ids))
        ).all():
            _dea.run_id = None
            session.add(_dea)
        session.flush()
    # 2b. #137: MemoryEvent.workflow_run_id is a NULLABLE FK onto workflowrun.id, and a nullable
    #     FK does NOT self-null when its parent is deleted -- an event captured against one of
    #     these runs made the step-3 delete raise IntegrityError and roll everything back (the
    #     crash `scripts/showcase_seed.py --cleanup` hit). The EPISODE belongs to the customer,
    #     not to the run that happened to produce it, so un-scope rather than reap: the same
    #     treatment delete_document / content.delete_project already apply through the SHARED
    #     helper. Imported lazily (retention imports the heavier service graph at call time).
    _memory_events_unscoped = 0
    if run_ids:
        from app.services import retention as _retention

        _memory_events_unscoped = _retention.unscope_memory_event_runs(session, run_ids)
    step_ids = list(
        session.exec(select(WorkflowStep.id).where(WorkflowStep.workflow_id == workflow_id)).all()
    )
    # 1. StepRun traces — children of BOTH WorkflowRun (workflow_run_id) and
    #    WorkflowStep (step_id). Deleting by run id covers this workflow's traces;
    #    the step-id sweep is a belt-and-suspenders for any stray trace.
    if run_ids:
        for sr in session.exec(select(StepRun).where(StepRun.workflow_run_id.in_(run_ids))).all():
            session.delete(sr)
    if step_ids:
        for sr in session.exec(select(StepRun).where(StepRun.step_id.in_(step_ids))).all():
            session.delete(sr)
        # 2. Null adapted_from_id on any cross-language COPY step (possibly in another
        #    workflow) whose provenance points at one of ours — mirrors delete_step so
        #    the derived step survives (just loses its link) instead of tripping the FK.
        for copy in session.exec(
            select(WorkflowStep).where(WorkflowStep.adapted_from_id.in_(step_ids))
        ).all():
            copy.adapted_from_id = None
            session.add(copy)
    session.flush()
    # 3. WorkflowRun history, then 4. WorkflowStep rows (parents of the above).
    for run in session.exec(
        select(WorkflowRun).where(WorkflowRun.workflow_id == workflow_id)
    ).all():
        session.delete(run)
    for st in session.exec(
        select(WorkflowStep).where(WorkflowStep.workflow_id == workflow_id)
    ).all():
        session.delete(st)
    session.flush()
    # 4b. WorkflowBatch fan-out rows OF this workflow: WorkflowBatch.workflow_id is a
    #     NOT-NULL FK to workflow.id, so any batch this workflow ever dispatched (batch_runs
    #     stamps one per fan-out) blocks the step-5 workflow delete exactly as a run does.
    #     A batch's MEMBER runs resolve per-locale VARIANTS off this workflow (resolve_variant),
    #     so a member's own workflow_id may be a sibling -- those runs are NOT in the step-3
    #     sweep (keyed on WorkflowRun.workflow_id == this) and MUST survive. Un-scope the
    #     surviving members' batch_id (nullable) before dropping the batch rows, mirroring
    #     retention._reap_document_children step (8); this workflow's own members are already
    #     gone at step 3, so the survivors here are exactly the variant siblings.
    batch_ids = list(
        session.exec(select(WorkflowBatch.id).where(WorkflowBatch.workflow_id == workflow_id)).all()
    )
    _members_unscoped = 0
    if batch_ids:
        for member in session.exec(
            select(WorkflowRun).where(WorkflowRun.batch_id.in_(batch_ids))
        ).all():
            member.batch_id = None
            session.add(member)
            _members_unscoped += 1
        session.flush()
        for batch in session.exec(
            select(WorkflowBatch).where(WorkflowBatch.id.in_(batch_ids))
        ).all():
            session.delete(batch)
        session.flush()
    # 4c. EvalCandidate.workflow_id (Lab config-under-test) is a NULLABLE FK -- a nullable FK
    #     does NOT self-null when its parent is deleted, so a candidate pinned to this workflow
    #     would trip the same IntegrityError. UN-SCOPE it (the row survives: a candidate can be
    #     config-only, and its `config` snapshot is a copy, so nulling the pointer preserves a
    #     reproducible record), mirroring the B-H2 approval un-scope rather than a reap.
    _evals_unscoped = 0
    for cand in session.exec(
        select(EvalCandidate).where(EvalCandidate.workflow_id == workflow_id)
    ).all():
        cand.workflow_id = None
        session.add(cand)
        _evals_unscoped += 1
    if _evals_unscoped:
        session.flush()
    # Audit truth (M3): record what step 4b/4c actually reaped / un-scoped alongside the
    # workflow_reference_count keys, so the delete audit log is not silent about the batch
    # rows destroyed and the sibling members + eval candidates un-scoped.
    refs["workflow_batches"] = len(batch_ids)
    refs["batch_members_unscoped"] = _members_unscoped
    refs["eval_candidates_unscoped"] = _evals_unscoped
    refs["memory_events_unscoped"] = _memory_events_unscoped
    # 4d. #356 phase 2b: the workflow's own system-prompt override history (a plain FK onto
    #     workflow.id): reaped with it, so nothing orphaned and the FK never refuses the delete.
    #     The audit chain keeps the by-hash line of every write.
    from app.services import system_prompt_overrides as _spo

    _spo.delete_workflow_overrides(session, workflow_id)
    # 5. The workflow itself (leaves any group it belonged to; group_id is on the row).
    session.delete(wf)
    session.commit()
    return refs


# --- prompt templates --------------------------------------------------------
# (_coerce_pt_id lives up top now -- shared with the step-binding seams.)
def create_prompt_template(
    session: Session,
    name: str,
    body: str = "",
    org_id: int | None = None,
    customer_id=None,
    bound_customer_id=None,
) -> PromptTemplate | None:
    """Save a reusable prompt template. Returns the new PromptTemplate, or None when
    the name is blank (route no-ops).

    Multi-org P8: `org_id` stamps the creator's org so the template list can scope
    to it (closing the flagged cross-org leak). None with the gate off leaves org_id
    NULL — templates stay app-global single-tenant, byte-identical to today.

    CUSTOMER ATTACHMENT (SPEC-pt-attach): `customer_id` optionally confines the template
    to one customer (scope=Scope.CUSTOMER); blank/None leaves it GLOBAL (scope=
    Scope.GLOBAL, customer_id NULL — byte-identical to today, the invariant for installs
    that never attach a PT). An attached customer is VALIDATED (customers.selectable): it
    must exist, be same-org, and NOT be archived — else WorkflowError (never a silently
    mis-owned / dangling template).

    `bound_customer_id` (the caller's OWN customer binding when the caller is a
    customer-bound account; None = unbound / internal staff / trusted CLI operator —
    unchanged): a BOUND caller's template is FORCED to their own customer at this SERVICE
    seam (never global, never another customer). An explicit customer_id for a DIFFERENT
    customer is refused; a blank one is force-stamped to the caller's own — the same
    shape content.project_create's bound guard uses, so the form can never route around
    it. Mirrors access.customer_ring_blocks' isolation bar on the write path."""
    name = (name or "").strip()
    if not name:
        return None
    cust_id = _coerce_pt_id(customer_id)
    bound_cid = _coerce_pt_id(bound_customer_id)
    # Bound-caller confinement (checked BEFORE validation so a bound user can never
    # attach to another customer even by forging the customer_id field).
    if bound_cid is not None:
        if cust_id is not None and cust_id != bound_cid:
            raise WorkflowError(
                f"customer {cust_id} is not your own customer — a prompt template you "
                "create is confined to your own customer."
            )
        cust_id = bound_cid
    if cust_id is not None:
        from app.services import customers as customer_service

        if customer_service.selectable(session, cust_id, org_id=org_id) is None:
            raise WorkflowError(
                f"customer {cust_id} is not available (missing, in another org, or "
                "archived) — pick an active customer, or leave the template global."
            )
    scope = Scope.CUSTOMER if cust_id is not None else Scope.GLOBAL
    tmpl = PromptTemplate(name=name, scope=scope, body=body, org_id=org_id, customer_id=cust_id)
    session.add(tmpl)
    session.commit()
    session.refresh(tmpl)
    return tmpl


def list_prompt_templates(session: Session, org_id=None, customer_id=None) -> list[PromptTemplate]:
    """Prompt templates offered in a context, newest first. `org_id` (an int) scopes to
    one org for the SaaS/multi-org path; None returns every org's (single-tenant,
    byte-identical).

    CUSTOMER VISIBILITY (SPEC-pt-attach): `customer_id` is the CALLER's own customer
    binding when the caller is a customer-bound account (None = unbound / internal staff
    / trusted operator — sees all, byte-identical). When given, a bound caller sees only
    GLOBAL templates (customer_id NULL) + its OWN customer's — a foreign customer's
    template is filtered out. Reuses access.customer_ring_blocks (the SAME predicate the
    by-id ring uses), so the list + by-id surfaces cannot drift."""
    from app.services import access

    stmt = select(PromptTemplate).order_by(PromptTemplate.created_at.desc())
    if org_id is not None:
        stmt = (
            select(PromptTemplate)
            .where(PromptTemplate.org_id == org_id)
            .order_by(PromptTemplate.created_at.desc())
        )
    rows = list(session.exec(stmt).all())
    cid = _coerce_pt_id(customer_id)
    return [
        t for t in rows if not access.customer_ring_blocks(cid, getattr(t, "customer_id", None))
    ]


def rename_prompt_template(session: Session, template_id: int, name: str) -> PromptTemplate:
    """Rename a prompt template. Names are legally non-unique (no new uniqueness
    rule), so no dup-check. Steps reference a template by id (WorkflowStep.
    prompt_template_id), so a rename propagates automatically. Raises WorkflowError
    on an unknown id or a blank name."""
    tmpl = session.get(PromptTemplate, template_id)
    if tmpl is None:
        raise WorkflowError(f"prompt template {template_id} not found.")
    name = (name or "").strip()
    if not name:
        raise WorkflowError("prompt template name must not be blank.")
    tmpl.name = name
    session.add(tmpl)
    session.commit()
    session.refresh(tmpl)
    return tmpl


def delete_prompt_template(session: Session, template_id: int) -> dict:
    """Delete a prompt template. FAILS CLOSED (WorkflowError with the live count)
    while any WorkflowStep still pins it — a prompt template is otherwise a leaf, so
    an unreferenced one is a plain hard delete. Returns the reference dict."""
    tmpl = session.get(PromptTemplate, template_id)
    if tmpl is None:
        raise WorkflowError(f"prompt template {template_id} not found.")
    refs = prompt_template_reference_count(session, template_id)
    if refs["steps"] > 0:
        raise WorkflowError(
            "cannot delete prompt template %d (%r): still referenced by %d workflow "  # noqa: UP031
            "step(s). Detach it from those steps first." % (template_id, tmpl.name, refs["steps"]),
            refs,
        )
    session.delete(tmpl)
    session.commit()
    return refs


def pair_key(src: str | None, tgt: str | None) -> str:
    """PUBLIC alias of `_pair`: the display key for a variant's language pair. The web
    + CLI both need to NAME a pair in a notice (R4: "replaced en-US->fr-FR with
    en-US->de-DE"), and both must spell it the way this module does — one definition."""
    return _pair(src, tgt)


def _is_pair_suffix_body(body: str) -> bool:
    """True when the text inside a trailing `[...]` is a pair key this module WROTE:
    the literal `universal`, or `<locale>-><locale>`."""
    body = (body or "").strip()
    if body.lower() == "universal":
        return True
    left, sep, right = body.partition("->")
    if not sep:
        return False
    return _is_pair_atom(left) and _is_pair_atom(right)


_MAX_ECHOED_CODE = 32


# How much of a rejected locale code an error message may echo. A copy POST can carry a
# 5000-char "locale"; the message names what was wrong without becoming the payload.
def _reject_unknown_locales(norm_pairs) -> None:
    """R8: raise WorkflowError naming every locale code in `norm_pairs` the language
    catalog does not know. Blank/None sides are legal (a universal side) and skipped.
    Called by copy_to_pairs AND add_to_group (N3) BEFORE any write, so every locale
    entry point on this module -- both web copy surfaces, the group add route and
    both CLI arms -- inherits one refusal from one seam (dual-surface parity)."""
    from app.services import languages_catalog

    bad, seen = [], set()
    for source_locale, target_locale in norm_pairs:
        for code in (source_locale, target_locale):
            if not code or languages_catalog.is_valid(code):
                continue
            # ASCII "..." not a typographic ellipsis: this message reaches the CLI as a
            # CliError, and program CHROME is pure ASCII by house rule (issue #15).
            shown = code if len(code) <= _MAX_ECHOED_CODE else code[:_MAX_ECHOED_CODE] + "..."
            if shown not in seen:
                seen.add(shown)
                bad.append(shown)
    if bad:
        raise WorkflowError(
            "unknown language code(s): {}. Use a code from the language catalog "
            "(e.g. en-US, fr-FR, es-419). Nothing was created.".format(
                ", ".join(repr(b) for b in bad)
            )
        )


def visible_group_variants(session: Session, group_id: int, user=None) -> list[Workflow]:
    """`group_variants` confined to what `user` may see (see `variant_visible`).
    `user=None` -> the full list, unchanged."""
    return [v for v in group_variants(session, group_id) if variant_visible(user, v)]


def group_covered_targets(
    session: Session, group_id: int, source_locale: str | None, locales, variants=None
) -> set:
    """Of `locales`, the target codes this group ALREADY has a variant for at
    `source_locale` — i.e. exactly the pairs `copy_to_pairs` would now refuse as
    duplicates. Returned in the CALLER's spelling so a picker can tick/disable its own
    option values. Locale-normalized, so a legacy `es_ES` member still covers `es-ES`.

    N1: `variants` overrides the member list — the web page passes the REQUESTER-VISIBLE
    members. A pair covered ONLY by a variant the requester cannot see must NOT render
    ticked-and-disabled: dispatch would never run that variant for them, so the pair is
    genuinely uncovered FOR THEM and they must be able to add it. Omitted -> the whole
    group, unchanged."""
    rows = group_variants(session, group_id) if variants is None else variants
    existing = [(v.source_locale, v.target_locale) for v in rows]
    return {
        loc
        for loc in locales
        if any(_same_pair(source_locale, loc, esrc, etgt) for esrc, etgt in existing)
    }


def rename_group(session: Session, group_id: int, name: str) -> WorkflowGroup:
    """Rename a workflow group. Group names are legally non-unique (no new uniqueness
    rule), so no dup-check. Variants reference the group by id (Workflow.group_id) so a
    rename propagates. Raises WorkflowError on an unknown id or a blank name."""
    grp = session.get(WorkflowGroup, group_id)
    if grp is None:
        raise WorkflowError(f"workflow group {group_id} not found.")
    name = (name or "").strip()
    if not name:
        raise WorkflowError("workflow group name must not be blank.")
    grp.name = name
    session.add(grp)
    session.commit()
    session.refresh(grp)
    return grp


def delete_group(session: Session, group_id: int) -> dict:
    """Delete a workflow group. FAILS CLOSED (WorkflowError with the member count)
    while it still has member workflows — the members are INDEPENDENT entities (each
    a real Workflow), never owned child data, so they are refused rather than
    cascaded. An empty group is a plain hard delete. Returns the reference dict."""
    grp = session.get(WorkflowGroup, group_id)
    if grp is None:
        raise WorkflowError(f"workflow group {group_id} not found.")
    refs = group_reference_count(session, group_id)
    if refs["members"] > 0:
        raise WorkflowError(
            "cannot delete workflow group %d (%r): it still has %d member workflow(s). "  # noqa: UP031
            "Ungroup or delete them first." % (group_id, grp.name, refs["members"]),
            refs,
        )
    session.delete(grp)
    session.commit()
    return refs


def adapt_instruction(step: WorkflowStep, src_pair: str, tgt_pair: str) -> tuple[str, bool]:
    """Rewrite one step's prompt_text for a new language pair via the provider seam.
    Returns (adapted_text, ok). On ANY failure — a provider error, an empty result,
    or a result that dropped a placeholder the original had — returns the ORIGINAL
    prompt_text with ok=False so the copy is created with the source instructions and
    the caller flags a warning (no data loss, per the plan). A blank source prompt is
    a no-op (nothing to adapt) and returns (\"\", True)."""
    original = step.prompt_text or ""
    if not original.strip():
        return original, True
    spec = orchestrator._spec_for_step(step)
    prompt = _adaptation_prompt(original, src_pair, tgt_pair)
    # _system_for, not the raw _SYSTEM constant: a step instruction almost never
    # carries ⟦⟧ tokens, and the raw clause TEACHES the notation to a model whose
    # output is saved as a prompt and re-rendered into every future request for the
    # new pair (review F5 of the #43 fix train). No locales on purpose — this call
    # rewrites an instruction; a "write in <target>" directive would be wrong here.
    res = orchestrator.run_completion(
        spec,
        prompt,
        system=orchestrator._system_for({}, original),
        task="adapt_prompt",
        payload={"text": original, "src_pair": src_pair, "tgt_pair": tgt_pair},
    )
    out = (res.get("text") or "").strip()
    if res.get("error") or not out:
        return original, False
    # Placeholder-integrity guard: the rewrite must not drop a placeholder the
    # original relied on (dropping {text} would break the render). If it did, keep
    # the original — an adapted-but-broken prompt is worse than an un-adapted one.
    for ph in _ADAPT_PLACEHOLDERS:
        if ph in original and ph not in out:
            return original, False
    return out, True


def _variant_match(
    session: Session,
    wf: Workflow,
    target_locale: str,
    source_locale: str | None = None,
    candidates=None,
    doc_customer_id: int | None = None,
) -> VariantMatch:
    """Rungs 2-4 of the dispatch ladder for a GROUPED workflow `wf` (the caller
    guarantees `wf.group_id is not None`; rung 1 — ungrouped — is handled by
    resolve_variant and never reaches here). Factored out so the group-coverage view
    derives from the exact same rung logic instead of a divergent reimplementation.

    `candidates` narrows the member pool BEFORE the `_compatible` dispatch filter (N1:
    the coverage readout passes the requester-visible members). Omitted -> the whole
    group, so real dispatch is untouched.

    `doc_customer_id` (#542) is the customer that owns the run's document. A variant
    stamped with THAT customer is compatible even when the requested workflow (the
    shared group's global default) has no customer: a bound customer's added pair
    (#468 stamps their copy) must dispatch for their own documents. It never admits a
    variant of any other customer, and None (the coverage readout, tests) is the
    old behavior unchanged."""
    from app.services import languages_catalog

    def _compatible(v: Workflow) -> bool:
        # Same org as the requested (already-scoped) workflow — a foreign-org variant
        # folded into this group (buildable via the CLI add path) is never dispatched.
        # Customer-compatible: global (customer_id None) or the SAME customer as the
        # requested workflow — a variant privately scoped to another customer must not
        # be substituted in (P4 "never mix customers").
        if getattr(v, "org_id", None) != getattr(wf, "org_id", None):
            return False
        if (
            v.customer_id is not None
            and v.customer_id != wf.customer_id
            and (doc_customer_id is None or v.customer_id != doc_customer_id)
        ):
            return False
        # Archive (residual sweep): an ARCHIVED sibling variant is never dispatched INTO
        # (nor counted as covering a pair) — dispatch must not swap in a retired workflow.
        # The REQUESTED workflow (rung 1 ungrouped / rung 4 fallback) is gated separately
        # at the launch seam (refuse_if_workflow_archived), so this only trims the sibling
        # substitution pool. Inert until a variant is archived (archived_at defaults NULL).
        if getattr(v, "archived_at", None) is not None:  # noqa: SIM103
            return False
        return True

    pool = group_variants(session, wf.group_id) if candidates is None else candidates
    variants = [v for v in pool if _compatible(v)]
    # Normalized locale keys (P2): es_ES / ES-es / es-ES all compare equal, so an
    # exact-pair variant is not missed on a legacy/underscore spelling.
    tgt = languages_catalog.normalize(target_locale)
    src = languages_catalog.normalize(source_locale)
    # L9 (loud signal): if an ARCHIVED sibling variant WOULD have been the exact-pair match
    # for this run, say so out loud -- dispatch is silently falling back to a default / the
    # requested workflow because the intended variant is retired.
    for v in pool:
        if getattr(v, "archived_at", None) is None:
            continue
        if not (tgt and languages_catalog.normalize(v.target_locale) == tgt):
            continue
        vsrc = languages_catalog.normalize(v.source_locale)
        if (
            (not src or not vsrc or vsrc == src)
            and getattr(v, "org_id", None) == getattr(wf, "org_id", None)
            and (
                v.customer_id is None
                or v.customer_id == wf.customer_id
                or (doc_customer_id is not None and v.customer_id == doc_customer_id)
            )
        ):
            log.warning(
                "dispatch: archived workflow variant #%s (%s->%s) skipped for run of "
                "workflow #%s target %s -- falling back to default/requested",
                v.id,
                v.source_locale or "*",
                v.target_locale or "*",
                wf.id,
                target_locale,
            )
            break

    # rung 2: exact pair variant (target must match; source matches or is universal)
    for v in variants:
        if tgt and languages_catalog.normalize(v.target_locale) == tgt:
            vsrc = languages_catalog.normalize(v.source_locale)
            if not src or not vsrc or vsrc == src:
                return VariantMatch(2, "exact", v.id, v)

    # rung 3: group default (is_default), then a universal member (no pair set)
    for v in variants:
        if v.is_default:
            return VariantMatch(3, "default", v.id, v)
    for v in variants:
        if not v.source_locale and not v.target_locale:
            return VariantMatch(3, "universal", v.id, v)

    # rung 4: nothing better — run the requested workflow unchanged
    return VariantMatch(4, "requested", wf.id, None)
