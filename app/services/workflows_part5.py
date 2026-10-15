"""workflows: part 5 (layered pure move)

Documented in: docs/architecture/workflow-engine.md
"""

from sqlmodel import Session, select

from app.models import (
    Scope,
    StepRun,
    StepType,
    Workflow,
    WorkflowRun,
    WorkflowStep,
)
from app.services import context_pkg, ids, orchestrator
from app.services import qe_autofix as _qe_autofix
from app.services.qe_autofix import clamp_rounds as _clamp_autofix_rounds
from app.services.workflows_common import (
    _ACTIVE_RUN_STATUSES,
    _RULE_BRANCH,
    _RULE_DUP_KEY,
    _RULE_GUARD,
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
    _new_graph_errors,
    _rc_ref_of,
    _step_attr,
)
from app.services.workflows_common_leaf import _require_whole_number
from app.services.workflows_part2 import (
    _coerce_tm_tristate_bool,
    _coerce_wait_hours,
    _ordered,
    _pair,
    _RacedDuplicatePair,
    _require_valid_lqa_threshold,
    _require_valid_tm_pool_selection,
    _require_valid_tm_scalar,
    _same_pair,
    _step_label,
    _validate_step_vendor,
    create_group,
    group_variants,
    validate_bound_template,
    validate_parallel_group,
    variant_visible,
)
from app.services.workflows_part2_leaf import MAX_STEP_INT_OVERRIDE
from app.services.workflows_part3 import (
    _group_span,
    _pre_group_prior,
    _reject_unknown_locales,
    adapt_instruction,
    validate_branch,
    validate_key,
)
from app.services.workflows_part4 import (
    _group_errors,
    pair_name,
    refuse_dead_fields,
    strip_pair_suffix,
    validate_run_condition,
)


def _assert_step_provider_roles(
    session: Session,
    workflow_id: int,
    *,
    provider=None,
    fallback_provider=None,
    qe_provider=None,
    qe_autofix_provider=None,
) -> None:
    """Refuse a provider ref saved into a slot its engine kind cannot fill (engines.
    assert_ref_role): the step provider and fallback must be translation engines, the
    QE judge an LLM or a Jev judge (engine:<id> of kind jev / jev_local, or the legacy
    "jev"). The one seam every surface's add/edit reaches (web, /api, CLI, MCP, the
    workflow author confirm path), so a judge can never be pinned as a translator.
    The QE auto-fix post-editor (qe_autofix_provider) must be a prompt LLM: an NMT or
    judge engine is refused ("fixer" role).
    Under the multi-org gate the workflow's org also confines which engines resolve.
    Raises WorkflowError (every caller already surfaces it cleanly)."""
    from app.services import access, engines

    judge_ref = (qe_provider or "").strip()
    if judge_ref and judge_ref.lower() != "jev":
        if (
            judge_ref.lower().startswith("engine:") and engines._parse_engine_ref(judge_ref) is None
        ) or not _qe_autofix.fixer_name_known(judge_ref):
            pid = engines._parse_engine_ref(judge_ref)
            detail = (
                f"no engine #{pid} exists"
                if pid is not None
                else "not a built-in LLM provider (claude / gemini / openai / local / mock), "
                "jev, or an engine:<id> reference"
            )
            raise WorkflowError(f"unknown QE judge {judge_ref!r}: {detail}.")
        if not engines._parse_engine_ref(judge_ref) and not _qe_autofix.fixer_is_llm(judge_ref):
            raise WorkflowError(
                f"QE judge {judge_ref!r}: pick an LLM engine or a Jev judge engine."
            )

    org_id = engines.NO_ORG_CHECK
    if access.multi_org_active():
        wf = session.get(Workflow, workflow_id)
        wf_org = getattr(wf, "org_id", None) if wf is not None else None
        # An org-owned workflow confines its refs to engines that org may use. An
        # install-level (NULL-org) workflow is only reachable through the operator door
        # and keeps the pre-gate role-only check.
        if wf_org:
            org_id = wf_org
    try:
        for ref, role in (
            (provider, "translate"),
            (fallback_provider, "translate"),
            (qe_provider, "judge"),
            (qe_autofix_provider, "fixer"),
        ):
            engines.assert_ref_role(ref, role, org_id)
    except engines.EngineRoleError as exc:
        raise WorkflowError(str(exc)) from exc


def _assert_autofix_fixer(qe_autofix, qe_autofix_provider, provider) -> None:
    """Refuse QE auto-fix whose post-editor would INHERIT a non-LLM engine (review B3,
    owner decision: local-first, no silent engine switches). With qe_autofix on and no
    explicit post-editor, the fixer is the step's own engine (else the install default);
    when that is machine translation it cannot post-edit, and the run would never swap
    in another engine on its own, so the save is refused with a clear ask to pick an LLM.
    An explicit non-LLM post-editor is already refused by the "fixer" role check.

    Review gate L2: an explicit post-editor must also NAME something that exists (a
    built-in provider or an engine:<id> ref), so a typo such as "claud" is refused
    here instead of failing every segment at run time."""
    explicit = (qe_autofix_provider or "").strip()
    if explicit and not _qe_autofix.fixer_name_known(explicit):
        from app.services import engines

        engine_id = engines._parse_engine_ref(explicit)
        if engine_id is not None:
            raise WorkflowError(
                f"unknown auto-fix post-editor {explicit!r}: no engine #{engine_id} exists."
            )
        raise WorkflowError(
            f"unknown auto-fix post-editor {explicit!r}: not a built-in provider "
            "(claude / gemini / openai / local / mock) or an engine:<id> reference."
        )
    if not qe_autofix or explicit:
        return
    ref = _qe_autofix.effective_fixer_ref("", provider)
    if not _qe_autofix.fixer_is_llm(ref):
        raise WorkflowError(
            f"Auto-fix needs an LLM post-editor, and this step's engine ({ref}) is not an "
            "LLM: machine translation cannot post-edit. Pick an LLM as the auto-fix "
            "post-editor, or turn auto-fix off."
        )


def add_step(
    session: Session,
    workflow_id: int,
    name: str = "",
    step_type: str = StepType.LLM_AGENT,
    provider: str = "",
    model: str = "",
    fallback_provider: str = "",
    fallback_model: str = "",
    prompt_text: str = "",
    prompt_template_id: int | None = None,
    chunk_policy: str = "auto",
    redact_pii: bool = False,
    assignee_email: str = "",
    glossary_id: int | None = None,
    adaptive_feedback: bool = False,
    adaptive_feedback_window: int = 5,
    doc_context_prepass: bool = False,
    qe_gate: bool = False,
    qe_threshold: int = 70,
    qe_provider: str = "",
    qe_autofix: bool = False,
    qe_autofix_provider: str = "",
    qe_autofix_max_rounds: int = 3,
    qe_autofix_cross_check: bool = False,
    lqa_threshold=None,
    tm_examples: bool = True,
    tm_examples_window: int = 3,
    use_image_context: bool = True,
    concept_glossary_render: bool = False,
    glossary_concept_cap: int = 10,
    glossary_token_cap: int = 0,
    tm_protect=None,
    over_translation_guard=None,
    memory_mode=None,
    tm_fill_threshold=None,
    tm_fuzzy_lock_mode=None,
    tm_lock_ice=None,
    tm_hundreds_enter_lqa=None,
    tm_context_floor=None,
    tm_pool_selection=None,
    run_condition=None,
    key=None,
    branch=None,
    max_visits=None,
    parallel_group=None,
    vendor_id=None,
    external_wait_hours=None,
) -> WorkflowStep:
    """Append a step to a workflow (order_index = current step count). Empty
    string provider/model/fallback fields normalise to None, exactly as the
    route did. `redact_pii` scrubs PII before this step's engine call + restores
    it after (per-step choice). `assignee_email` (Feature B) is only meaningful on
    a human_review step — the reviewer emailed when the run pauses there; it is
    stored trimmed and ignored on non-human_review steps. `glossary_id` (Phase 5-B)
    pins an approved module Glossary for this step's run-time glossary matching
    (None = project-scoped matching, byte-identical to pre-module).

    `run_condition` (SPEC-conditional-workflows §5, Phase 2) is an optional PRE-STEP
    GUARD — a dict {ref, op, value} or the compact CLI string ('prev_lqa:failed').
    NULL/blank = always run (byte-identical to today). It is validated + sanitized here
    against the workflow's EARLIER steps (validate_run_condition, fail-closed): an
    unknown ref/op, a score op with no numeric value, extra keys, or a ref with no
    resolving prior step raises WorkflowError and creates nothing."""
    # BUG-A201: reject an unknown step_type at the SHARED seam. The whitelist lived only
    # in the CLI argparse `choices=`, so /api + web persisted a bogus type ("banana")
    # that the orchestrator then ran as an llm_agent — an extra billable MT pass under a
    # green run. Every surface funnels through here, so one guard closes all three.
    if step_type not in StepType.ALL:
        raise WorkflowError(
            "unknown step type {!r} (choose from {}).".format(step_type, ", ".join(StepType.ALL))
        )
    # BUG-A203 (run-cancel train): block APPEND while a run is in flight, for parity with
    # move/edit/delete (which already refuse). An append lands at max(order_index)+1 so it
    # does not re-seat earlier steps, but a resumed run would still execute a step its
    # launcher never authored — a surprising trajectory change under a green run. Same
    # in-flight set + message shape as the edit/move/delete guards; a CANCELLED / completed
    # / failed run is NOT counted (it is not in _ACTIVE_RUN_STATUSES), so cancelling a
    # stuck run unblocks add-step exactly as it unblocks the others.
    active = len(
        session.exec(
            select(WorkflowRun.id).where(
                WorkflowRun.workflow_id == workflow_id, WorkflowRun.status.in_(_ACTIVE_RUN_STATUSES)
            )
        ).all()
    )
    if active > 0:
        raise WorkflowError(
            "cannot add a step to workflow %d while it has %d in-flight run(s) "  # noqa: UP031
            "(pending/running/paused) — a resumed run would execute a step it never "
            "launched with. Let the run(s) finish or cancel them, then add." % (workflow_id, active)
        )
    # BUG-A202: reject an out-of-band / junk lqa_threshold up front instead of silently
    # clamping it (a typo'd -5 would have become a gate-disabling 0). NULL/blank stays
    # "inherit the default" (byte-identical NULL path).
    lqa_threshold = _require_valid_lqa_threshold(lqa_threshold)
    # Batch 15 T1 (N2/N4): validate the tm_match int + pool overrides at the seam so a junk
    # value ('85%', 'garbage') is REFUSED cleanly on every surface, never silently cleared
    # to inherit / collapsed to "read no pool". Inherit tokens (auto/inherit/blank) still
    # resolve to None; the '(none)' sentinel / an explicit [] to the empty selection.
    tm_fill_threshold = _require_valid_tm_scalar(tm_fill_threshold, "TM fill threshold")
    tm_context_floor = _require_valid_tm_scalar(tm_context_floor, "TM context floor")
    tm_pool_selection = _require_valid_tm_pool_selection(tm_pool_selection)
    # SPEC-lqa-step-honest-options: refuse a dead-for-this-type field the caller
    # actively set (a brand-new step has no prior state, so any meaningful value
    # here is a fresh explicit choice). Fail-closed, before any DB write.
    # SPEC-parallel-routing §5.6: normalise the slug BEFORE the dead-field check so an
    # llm_agent/human_review step that actually names a group is refused with the
    # explicit Phase-1 message (and a blank/whitespace value stays inert everywhere).
    qe_threshold = _require_whole_number(qe_threshold, "QE threshold", 0, 100, 70)
    qe_autofix_max_rounds = _require_whole_number(
        qe_autofix_max_rounds, "QE auto-fix max rounds", 1, 5, 3
    )
    parallel_group = validate_parallel_group(parallel_group)
    refuse_dead_fields(
        step_type,
        {
            "parallel_group": parallel_group,
            "provider": provider,
            "model": model,
            "fallback_provider": fallback_provider,
            "fallback_model": fallback_model,
            "prompt_text": prompt_text,
            "prompt_template_id": _coerce_pt_id(prompt_template_id),
            "chunk_policy": chunk_policy,
            "redact_pii": redact_pii,
            "glossary_id": glossary_id,
            "adaptive_feedback": adaptive_feedback,
            "doc_context_prepass": doc_context_prepass,
            "tm_examples": tm_examples,
            "qe_gate": qe_gate,
            "qe_threshold": qe_threshold,
            "qe_provider": qe_provider,
            # QE auto-fix (review gate L1): refused on every non-llm_agent step.
            "qe_autofix": qe_autofix,
            "qe_autofix_provider": qe_autofix_provider,
            "qe_autofix_max_rounds": qe_autofix_max_rounds,
            "qe_autofix_cross_check": qe_autofix_cross_check,
            "lqa_threshold": _clamp_lqa_threshold(lqa_threshold),
            "tm_protect": _coerce_tm_protect(tm_protect),
            # run-190 Item D / M5: refused on any non-llm_agent step (a forged
            # over_translation_guard on an lqa/human_review/tm_match/external step is a clean
            # refusal). Same tristate coercion as tm_protect.
            "over_translation_guard": _coerce_tm_protect(over_translation_guard),
            "memory_mode": _coerce_memory_mode(memory_mode),
            # External/vendor dispatch (Wave A): refused on any non-external step.
            "vendor_id": _coerce_vendor_id(vendor_id),
            "external_wait_hours": _coerce_wait_hours(external_wait_hours),
            # Batch 15 T1 (SPEC-tm-match-step-config §8.A): the six tm_match config fields
            # are refused on any NON-tm_match step (a forged tm_fill_threshold on an
            # llm_agent step is a clean refusal, LAW-TM-10).
            "tm_fill_threshold": _coerce_tm_scalar_int(tm_fill_threshold),
            "tm_fuzzy_lock_mode": _coerce_tm_fuzzy_lock_mode(tm_fuzzy_lock_mode),
            "tm_lock_ice": _coerce_tm_tristate_bool(tm_lock_ice),
            "tm_hundreds_enter_lqa": _coerce_tm_tristate_bool(tm_hundreds_enter_lqa),
            "tm_context_floor": _coerce_tm_scalar_int(tm_context_floor),
            "tm_pool_selection": _coerce_tm_pool_selection(tm_pool_selection),
        },
    )
    # Jev judges: a judge-kind engine may only be a QE judge; a translate slot refuses it.
    _assert_step_provider_roles(
        session,
        workflow_id,
        provider=provider,
        fallback_provider=fallback_provider,
        qe_provider=qe_provider,
        qe_autofix_provider=qe_autofix_provider,
    )
    _assert_autofix_fixer(qe_autofix, qe_autofix_provider, provider)
    # External step: the vendor is REQUIRED and validated here (selectable + linked to the
    # workflow's customer when it is customer-scoped) so a broken external step can never
    # be saved; the run-time branch is the ultimate fail-closed gate (vendor_for_customer).
    _v_id = _coerce_vendor_id(vendor_id) if step_type == StepType.EXTERNAL else None
    _wait_hours = (
        _coerce_wait_hours(external_wait_hours) if step_type == StepType.EXTERNAL else None
    )
    if step_type == StepType.EXTERNAL:
        _validate_step_vendor(session, workflow_id, _v_id)
    # SPEC-prompt-template-runtime: resolve + validate a bound prompt template at BIND
    # time (dangling id / an llm_agent template missing {text} is refused, fail-closed,
    # before any DB write). None when unset. Dead-for-type (lqa/human_review) is already
    # refused above, so this only runs for an llm_agent step.
    pt_id = validate_bound_template(session, step_type, prompt_template_id)
    existing = orchestrator.get_steps(session, workflow_id)
    # THE APPEND POSITION IS max(order_index)+1, NOT len(existing) (adversarial review,
    # MEDIUM-4). order_index is NOT compacted on delete -- delete_step removes a row and
    # renumbers nothing -- so after any delete the two disagree and `len` lands the new
    # step MID-SPINE. Pre-existing, but this branch is where it starts corrupting rather
    # than merely reordering: an append that materialises INSIDE a parallel group fuses
    # itself into that group (or splits it), which in the reviewer's repro made a
    # downstream bare `prev_lqa` ambiguous with no refusal, and in another left a
    # workflow PERMANENTLY unaddable -- every subsequent append refused with "group is
    # split", and the product has no reorder affordance to escape with.
    count = max((s.order_index for s in existing), default=-1) + 1
    # SPEC §5 pre-step guard: validate against the steps that will PRECEDE this one
    # (add_step appends, so every existing step). Fail-closed at save time.
    # SPEC-parallel-routing §5.1.4: a GROUP MEMBER's guard resolves against the steps
    # strictly BEFORE its group, never against a sibling lane (at fork time no lane has
    # produced anything). A step in no group keeps the full prefix -> byte-identical.
    run_condition = validate_run_condition(
        run_condition, _pre_group_prior(existing, parallel_group)
    )
    # SPEC §5 Phase 3: stable key (D1) + post-step branch (D3). The key is slugified +
    # uniqueness-checked against the existing steps; the branch is validated against the
    # existing keys (loop-back targets must already exist) + this step's own new key
    # (a self-loop) and requires a gate-producing step (D3). Fail-closed at save time.
    key = validate_key(key, existing)
    branch = validate_branch(
        branch, step_type, qe_gate, existing, self_key=key, parallel_group=parallel_group
    )
    # max_visits: NULL inherits the run-level default; a set value must be a positive int.
    mv = None
    if max_visits is not None and str(max_visits).strip() != "":
        try:
            mv = int(max_visits)
        except (TypeError, ValueError, OverflowError) as e:
            raise WorkflowError(f"max visits {max_visits!r} must be a whole number.") from e
        if mv < 1:
            raise WorkflowError("max visits must be at least 1 (or blank to inherit the default).")
        if mv > MAX_STEP_INT_OVERRIDE:  # GitHub #502: never reach the int column unbounded
            raise WorkflowError(f"max visits must be at most {MAX_STEP_INT_OVERRIDE}.")
    # WHOLE-GRAPH re-validation for the graph this add would PRODUCE (the existing steps
    # plus this one), as a BEFORE/AFTER diff -- the SAME guard update_step applies, and
    # deliberately the same function (adversarial review, HIGH-2). This used to run
    # `_group_errors` alone, which sees only membership: an append landing mid-spine
    # (see the order_index note above) could fuse an inert singleton into a real group
    # and thereby make a DOWNSTREAM step's bare `prev_lqa` ambiguous -- a guard error,
    # invisible to the membership checker, accepted at save. Running the full checker
    # closes that class outright: any error an add introduces ANYWHERE in the graph is
    # refused, while a pre-existing (engine-tolerated) inconsistency still never blocks
    # an unrelated add.
    #
    # DELIBERATE BEHAVIOUR CHANGE, PINNED BY TEST (SPEC-workflow-canvas review round 2).
    # Routing this diff through the shared identity keying relaxed exactly one case: on a
    # workflow whose slug X is ALREADY split, adding a NEW step carrying slug X now
    # SUCCEEDS. Under the old (step_id, message) keying it was refused, because the new
    # fragment produced a split message against a step id that had none before, and the
    # diff read that as fresh breakage. The symmetric identity ("slug X is split") sees
    # the truth: X was split before and is split after, so the add introduced nothing.
    #
    # Allowing it is the right call, not a tolerated regression:
    #   * the split is a STANDING graph error, already surfaced on every builder render
    #     and every `workflow show` -- it is not being hidden, only not double-charged;
    #   * at RUN time `_group_at` degrades gracefully: each consecutive run of >= 2
    #     members forks independently, so an extra fragment changes no other lane;
    #   * refusing traps LEGACY installs. A workflow that already carries a split cannot
    #     be repaired by ADDING (there is no insert-at-position), so the refusal blocked
    #     every future add to that workflow while fixing nothing -- the same
    #     "PERMANENTLY unaddable" trap the order_index note above exists to prevent.
    # A CLEAN group is still protected: an add that would CREATE a split is refused, as
    # before. Both halves are pinned in tests/verify_workflow_canvas.py.
    _cand_row = {
        "id": None,
        "order_index": count,
        "name": (name or "").strip(),
        "step_type": step_type,
        "qe_gate": bool(qe_gate),
        "key": key,
        "run_condition": run_condition,
        "branch": branch,
        "parallel_group": parallel_group,
    }
    _new = [
        f"step {lbl!r}: {msg}"
        for lbl, msg in _new_graph_errors(
            _graph_edit_errors(existing),
            _graph_edit_errors(list(existing) + [_cand_row]),  # noqa: RUF005
        )
    ]
    if _new:
        raise WorkflowError(
            "this step would break the workflow graph: {}. Adjust the dependent step(s) "
            "first, or change this step's key/type/gate/group.".format("; ".join(_new))
        )
    # Feature B: the assignee applies to a HUMAN_REVIEW step and (M3) to an EXTERNAL step
    # -- the person notified when the run parks on a vendor dispatch AND when an expired
    # leg escalates to a human_review pause (decision 3). Stamped only on those types so an
    # accidental value on an llm_agent/lqa/tm_match step is inert.
    assignee = (
        (assignee_email or "").strip()
        if step_type in (StepType.HUMAN_REVIEW, StepType.EXTERNAL)
        else ""
    )
    # Batch 15 T1 (SPEC-tm-match-step-config §8.A, D15-4/LAW-TM-8): SEED a NEW tm_match
    # step's five scalar overrides from the CURRENT globals (a concrete snapshot frozen at
    # creation -- a later global change never mutates it), when the caller left them NULL.
    # A caller-supplied value wins. The pool selection is NEVER snapshotted (S2 -- pool
    # membership is dynamic); it defaults NULL = all readable. On a non-tm_match step the
    # six are dead (refused above if actively set) and stored NULL/inert.
    _tm_fill = _coerce_tm_scalar_int(tm_fill_threshold)
    _tm_mode = _coerce_tm_fuzzy_lock_mode(tm_fuzzy_lock_mode)
    _tm_ice = _coerce_tm_tristate_bool(tm_lock_ice)
    _tm_hundreds = _coerce_tm_tristate_bool(tm_hundreds_enter_lqa)
    _tm_floor = _coerce_tm_scalar_int(tm_context_floor)
    _tm_pools = None
    if step_type == StepType.TM_MATCH:
        from app.services import tm_lock

        _seed = tm_lock.resolve_policy(session)  # the current GLOBAL policy (defaults)
        if _tm_fill is None:
            _tm_fill = _seed.fill_threshold
        if _tm_mode is None:
            _tm_mode = _seed.fuzzy_lock_mode
        if _tm_ice is None:
            _tm_ice = _seed.lock_ice
        if _tm_hundreds is None:
            _tm_hundreds = _seed.hundreds_enter_lqa
        if _tm_floor is None:
            _tm_floor = _seed.context_floor
        _tm_pools = _coerce_tm_pool_selection(tm_pool_selection)
    else:
        _tm_fill = _tm_mode = _tm_ice = _tm_hundreds = _tm_floor = None
    step = WorkflowStep(
        workflow_id=workflow_id,
        order_index=count,
        name=(name or "").strip(),
        step_type=step_type,
        provider=provider or None,
        model=model or None,
        fallback_provider=fallback_provider or None,
        fallback_model=fallback_model or None,
        prompt_text=prompt_text,
        prompt_template_id=pt_id,
        chunk_policy=chunk_policy,
        redact_pii=bool(redact_pii),
        assignee_email=assignee,
        glossary_id=ids.coerce_id(glossary_id),
        adaptive_feedback=bool(adaptive_feedback),
        adaptive_feedback_window=_clamp_window(adaptive_feedback_window),
        doc_context_prepass=bool(doc_context_prepass),
        # QE routing gate (roadmap gap #1/#6): default OFF. Threshold validated within
        # 0-100; qe_provider "" normalises to None (reuse the step's own provider).
        qe_gate=bool(qe_gate),
        qe_threshold=_clamp_threshold(qe_threshold),
        qe_provider=(qe_provider or "").strip() or None,
        # QE auto-fix (Train B): opt-in, default OFF. The fixer "" normalises to None
        # (the step's own provider); rounds outside 1-5 refused above.
        qe_autofix=bool(qe_autofix),
        qe_autofix_provider=(qe_autofix_provider or "").strip() or None,
        qe_autofix_max_rounds=_clamp_autofix_rounds(qe_autofix_max_rounds),
        qe_autofix_cross_check=bool(qe_autofix_cross_check),
        # feat/lqa-configurable-threshold: the lqa step's numeric pass bar. None (unset)
        # -> the built-in default (90) at scoring time. Clamped 0-100; refused above on
        # a non-lqa step. dead-for-type is caught by refuse_dead_fields.
        lqa_threshold=_clamp_lqa_threshold(lqa_threshold),
        # TM few-shot examples (SPEC-context-metadata phase 2B): default ON. Window
        # clamped 0-5 (0 disables, like the toggle off).
        tm_examples=bool(tm_examples),
        tm_examples_window=context_pkg.clamp_examples_window(tm_examples_window),
        # Batch 14 T2 (SPEC §25): per-step image-context toggle, default ON so a new
        # llm_agent step sees the string's images (the ratified all-agent-steps behavior);
        # inert on non-agent types (the orchestrator predicate excludes them by type).
        use_image_context=bool(use_image_context),
        # Terminology T3 (SPEC §9-T3): the per-step concept-glossary renderer toggle +
        # caps, default OFF/10/0 so a fresh step renders prompts.glossary_line verbatim
        # (byte-identical, LAW-TERM-14). Inert on non-llm_agent types (they render no
        # {glossary} block).
        concept_glossary_render=bool(concept_glossary_render),
        glossary_concept_cap=_coerce_glossary_cap(glossary_concept_cap, 10),
        glossary_token_cap=_coerce_glossary_cap(glossary_token_cap, 0),
        # LQA safety floor D1 (PROPOSAL §4.D1): the TRISTATE TM provenance override.
        # None = the correction-loop-shaped default (byte-identical to pre-Phase-1 on
        # any workflow without an LQA step).
        tm_protect=_coerce_tm_protect(tm_protect),
        # run-190 Item D / M5: the TRISTATE over-translation (page-dump) guard override.
        # None = inherit the global OVER_TRANSLATION_MAX_RATIO knob (ON); False = this
        # llm_agent step opts out; True = explicitly ON. Inert on non-llm_agent types.
        over_translation_guard=_coerce_tm_protect(over_translation_guard),
        # LLMemory step-level opt-out (PROPOSAL-llmemory §6): the TRISTATE-BY-STRING
        # recall override. None/"auto" = participate normally (byte-identical to
        # pre-LLMemory on any step); "off" = never inject a memory block here.
        memory_mode=_coerce_memory_mode(memory_mode),
        # Batch 15 T1 (SPEC-tm-match-step-config §5.1/§8.A): the six tm_match config
        # overrides. On a tm_match step the five scalars are seeded from the current
        # globals when NULL (a concrete snapshot); pool selection defaults NULL (all
        # readable, never snapshotted). Dead/None on every other type.
        tm_fill_threshold=_tm_fill,
        tm_fuzzy_lock_mode=_tm_mode,
        tm_lock_ice=_tm_ice,
        tm_hundreds_enter_lqa=_tm_hundreds,
        tm_context_floor=_tm_floor,
        tm_pool_selection=_tm_pools,
        # SPEC §5 pre-step guard (Phase 2): the validated {ref, op, value} or None.
        run_condition=run_condition,
        # SPEC §5 Phase 3 (D1/D3/D4): the validated stable key, post-step branch, and
        # per-step loop ceiling (all None = an unnamed, non-branching, default-capped
        # step -> byte-identical to today).
        key=key,
        branch=branch,
        max_visits=mv,
        # SPEC-parallel-routing §5.6: the validated group slug (None = not in a group
        # -> byte-identical to today; the orchestrator's `_has_parallel` gate stays off).
        parallel_group=parallel_group,
        # External/vendor dispatch (Wave A): the validated vendor + wait override, stamped
        # ONLY on an external step (inert None elsewhere, like assignee_email on non-hr).
        vendor_id=_v_id,
        external_wait_hours=_wait_hours,
    )
    session.add(step)
    session.commit()
    session.refresh(step)
    return step


def delete_step(session: Session, step_id: int, *, workflow_id: int) -> bool:
    """Delete a step bound to its workflow. Returns False for a missing or foreign
    step, before inspecting its runs or graph. Raises WorkflowError,
    changing NOTHING, when the delete is unsafe -- see the two guards below.

    THE TWO GUARDS ARE update_step's, AND THAT SYMMETRY IS THE POINT (adversarial
    review, HIGH-1). Deleting a step is a graph edit with strictly MORE reach than
    editing one -- it removes a node other steps' guards and branches may depend on --
    yet this seam used to apply neither of update_step's protections, so the whole
    fail-closed-at-save-time discipline had a door in it:

      GUARD 1, RUN SAFETY. Refused while the workflow has an ACTIVE
      (pending/running/paused) run, exactly as an edit is. A PAUSED run resumes at a
      stored `current_step_index`, and order_index is not compacted on delete, so
      removing an EARLIER step re-seats every later step one position down: the
      reviewer's repro resumed straight into the MIDDLE of a parallel group, where
      `_group_at` declines to fork and one lane silently never ran at all. There is no
      correct index to fix up to (the cursor's meaning changed), so the only honest
      answer is to refuse until the run is finished or cancelled.

      GUARD 2, WHOLE-GRAPH BEFORE/AFTER DIFF. The same `_graph_edit_errors` diff
      update_step uses, over the step list MINUS this step. Deleting the last LQA step
      upstream of a `prev_lqa` guard orphans it; deleting a step BETWEEN two same-slug
      singletons FUSES them into a real parallel group, which can make a downstream bare
      `prev_lqa` ambiguous -- the reviewer's repro, where the engine then answered with
      the highest-order lane only, silently. A pre-existing inconsistency still never
      blocks the delete; only NEW breakage does.

    Two FKs point at workflowstep.id: StepRun.step_id (the per-step run trace) and
    WorkflowStep.adapted_from_id (a P5 cross-language copy's link back to its
    source step). Both must be cleared BEFORE deleting the step, or the delete
    trips the FK — on Postgres (always FK-enforcing) this failed for any step that
    had ever run or been used as an adaptation source. The StepRun trace rows for a
    now-deleted step are dangling history, so they are removed; adapted_from_id is
    nulled (the copy survives, just loses its provenance link) rather than deleting
    the derived step. The child writes are flushed before the parent delete so the
    single commit orders the DELETEs child-before-parent (no SQLModel Relationship
    declares the dependency for the unit-of-work to infer)."""
    step = session.get(WorkflowStep, step_id)
    if step is None or step.workflow_id != workflow_id:
        return False
    if step:
        # Guard 1 -- run safety (update_step's rule, verbatim).
        active = len(
            session.exec(
                select(WorkflowRun.id).where(
                    WorkflowRun.workflow_id == step.workflow_id,
                    WorkflowRun.status.in_(_ACTIVE_RUN_STATUSES),
                )
            ).all()
        )
        if active > 0:
            raise WorkflowError(
                "cannot delete a step of workflow %d while it has %d in-flight run(s) "  # noqa: UP031
                "(pending/running/paused) -- deleting a step re-seats every later step, "
                "so a paused run would resume at the wrong one. Let the run(s) finish or "
                "cancel them, then delete." % (step.workflow_id, active)
            )
        # Guard 2 -- whole-graph BEFORE/AFTER diff over the list MINUS this step.
        all_steps = orchestrator.get_steps(session, step.workflow_id)
        new_errors = _new_graph_errors(
            _graph_edit_errors(all_steps),
            _graph_edit_errors([s for s in all_steps if s.id != step_id]),
        )
        if new_errors:
            detail = "; ".join(f"step {lbl!r}: {msg}" for lbl, msg in new_errors)
            raise WorkflowError(
                f"deleting this step would break the workflow graph: {detail}. Adjust "
                "the dependent step(s) first, then delete."
            )
        for sr in session.exec(select(StepRun).where(StepRun.step_id == step_id)).all():
            session.delete(sr)
        for copy in session.exec(
            select(WorkflowStep).where(WorkflowStep.adapted_from_id == step_id)
        ).all():
            copy.adapted_from_id = None
            session.add(copy)
        session.flush()
        session.delete(step)
        session.commit()
        return True
    return False


def _graph_edit_errors(steps) -> list:
    """Every structural graph error, as (step_id, step_label, message, IDENTITY)
    4-tuples, over a list of steps (each a WorkflowStep OR a plain dict — _step_attr
    handles both, so a CANDIDATE edit can be represented as a dict spliced into the real
    rows). Never raises.

    The first three elements are for DISPLAY; the fourth is the ONLY thing the
    before/after diff may key on — see the ERROR IDENTITY note above `_group_errors` for
    why the message and the step id are both unsafe keys once steps can move.

    Detects the three things a step edit can break for OTHER steps (and for the edited
    step itself): a run_condition that no longer resolves against its EARLIER steps
    (e.g. a downstream prev_lqa guard after the only prior LQA step changed type), a
    duplicated key, and a branch that violates D3 / targets a key that no longer exists
    (e.g. a downstream on_fail after this step's key was renamed/cleared). Used for a
    BEFORE/AFTER diff so a pre-existing (engine-tolerated) inconsistency never blocks an
    edit — only NEW breakage the edit introduces does."""
    ordered = _ordered(steps)
    errors = []
    seen_keys = set()
    # SPEC-parallel-routing §5.1: group MEMBERSHIP errors join the same triple list, so
    # the before/after diff protects them exactly like the key/guard/branch errors.
    errors.extend(_group_errors(ordered))
    for idx, s in enumerate(ordered):
        sid = _step_attr(s, "id")
        label = _step_label(s)
        # §5.1.4: a group member's "prior" stops at its GROUP's first member -- sibling
        # lanes are concurrent, not earlier. `_group_span` returns (idx, idx) for a step
        # in no group, so this is the unchanged `ordered[:idx]` for every plain step.
        prior = ordered[: _group_span(ordered, idx)[0]]
        others = ordered[:idx] + ordered[idx + 1 :]
        k = _step_attr(s, "key")
        if k:
            if k in seen_keys:
                # SYMMETRIC identity: the error is reported against whichever occurrence
                # is seen SECOND, so moving the later one in front hands it to the other
                # step id. A duplicate is a property of the KEY, not of one occurrence.
                errors.append((sid, label, f"duplicate step key {k!r}", (_RULE_DUP_KEY, k)))
            seen_keys.add(k)
        rc = _step_attr(s, "run_condition")
        if rc:
            try:
                validate_run_condition(rc, prior)
            except WorkflowError as exc:
                # The MESSAGE can carry a worked example naming a candidate lane, which a
                # legal lane reorder flips; the identity names only the ref.
                errors.append((sid, label, f"guard: {exc}", (sid, _RULE_GUARD, _rc_ref_of(rc))))
        br = _step_attr(s, "branch")
        if br:
            try:
                validate_branch(
                    br,
                    _step_attr(s, "step_type"),
                    _step_attr(s, "qe_gate", False),
                    others,
                    self_key=k,
                    parallel_group=_step_attr(s, "parallel_group"),
                )
            except WorkflowError as exc:
                errors.append(
                    (sid, label, f"branch: {exc}", (sid, _RULE_BRANCH, _branch_ref_of(br)))
                )
    return errors


def copy_to_pairs(
    session: Session,
    workflow_id: int,
    pairs,
    adapt: bool = False,
    group_id: int | None = None,
    created_by: str = "",
    viewer=None,
    bound_customer_id=None,
) -> dict:
    """Clone a workflow (+ its steps) onto one or more target language pairs, inside a
    group. `pairs` is an iterable of (source_locale, target_locale) tuples. Returns
    {\"group_id\": gid, \"created\": [ {workflow_id, source, target, adapted, warnings} ],
    \"skipped\": [ {source, target, pair, reason} ], \"any_adapt_failed\": bool}.
    `group_id` is None ONLY when the source workflow does not exist — an all-duplicate
    submit returns a real group id with an empty `created` and a populated `skipped`.

    Behavior:
      - The SOURCE workflow is used purely as a template — it is NOT modified. If it is
        already grouped, new variants join its group (unless `group_id` overrides);
        if ungrouped, a group is created named after it and the source is folded in as
        the group's default (so the family has a fallback).
      - Each clone copies EVERY step faithfully (provider/model/fallback/chunk_policy/
        redact_pii/order/retry/backoff preserved) — the fidelity the plan requires.
      - EVERY cloned step records provenance: adapted_from_id points at the source step
        it was copied from (SPEC-workflows-redesign 4b — the live link shared-base
        propagation walks). This holds on BOTH the plain and the adapt path.
      - When `adapt=True`, each cloned step's prompt_text is ALSO rewritten from the
        source pair to that clone's pair via adapt_instruction (ONE run_completion per
        step) and the step is marked needs_review. A per-step adaptation FAILURE keeps
        the ORIGINAL prompt (copy still created) and surfaces a warning — never a
        data-loss.

    Raises WorkflowError (R8, adversarial review) when ANY requested locale is not in
    the language catalog — BEFORE anything is written, so a junk/oversized/`<script>`
    code creates no group, no variant and no name. Locale codes ENTER the system through
    languages_catalog.validate_codes everywhere else (project create, add-locale, language
    sets); the two copy surfaces were the hole. Validation only — the caller's SPELLING is
    stored as given (canonicalizing here would rewrite pairs behind the operator's back,
    the same class of bug as R3).

    `viewer` (N1, re-review) is the requesting User (or None). When given, the DUPLICATE
    guard only counts variants that viewer can SEE. A pair covered solely by a variant
    outside their ring is not coverage FOR THEM — dispatch (`_variant_match._compatible`)
    would never substitute it into their run — so refusing it as a duplicate left them
    permanently unable to add a language they legitimately lack, with the refusal citing
    a workflow they cannot open. The two resulting same-pair variants are unambiguous:
    each side's dispatch filter picks its own. None (CLI, the draft fan-out, tests) keeps
    the whole-group comparison, unchanged.

    `bound_customer_id` (#468) is the requester's own customer when it is a
    customer-bound account (None = staff / trusted CLI, unchanged). Its copies are always
    stamped with that customer (scope customer), even when the template is a GLOBAL
    variant of a shared group, so a customer account can add its own language variants
    to a shared group but can never add a GLOBAL variant every customer would dispatch.

    CALLER CONTRACT: this COMMITS (per clone) — as it always has. The N2 duplicate
    re-check runs inside a SAVEPOINT (`session.begin_nested`), so a duplicate can only
    ever discard the clone it just flushed; unrelated rows pending in `session` survive
    it. They will, however, be committed along with the clone, exactly as before.
    """
    src = session.get(Workflow, workflow_id)
    if src is None:
        return {"group_id": None, "created": [], "skipped": [], "any_adapt_failed": False}
    # R8: validate EVERY locale first, before a single write. Normalize the tuples here
    # too so the loop below and the guard read the same values.
    norm_pairs = [((s or "").strip() or None, (t or "").strip() or None) for s, t in pairs]
    _reject_unknown_locales(norm_pairs)
    src_steps = orchestrator.get_steps(session, workflow_id)
    src_pair_str = _pair(src.source_locale, src.target_locale)
    # #468: the ring the copies (and a newly created group) belong to.
    bound = ids.coerce_id(bound_customer_id)
    new_customer_id = src.customer_id
    if bound is not None and new_customer_id != bound:
        new_customer_id = bound

    # Resolve / create the group the clones live in.
    gid = group_id if group_id is not None else src.group_id
    if gid is None:
        # Name the family off the STEM, so a group created from an already-suffixed
        # source (or a multi-target draft's now-suffixed base, issue #46) reads
        # "ACME Workflow (group)", not "ACME Workflow [en-US->fr-FR] (group)".
        # `or src.name`: a workflow named ENTIRELY as a pair suffix strips to "" (R7),
        # and a group must never be named " (group)".
        grp = create_group(
            session,
            f"{strip_pair_suffix(src.name) or src.name} (group)",
            customer_id=new_customer_id,
            org_id=getattr(src, "org_id", None),
            created_by=created_by,
        )
        gid = grp.id
        # Fold the SOURCE workflow into its new group as the default fallback so the
        # family always has a rung below the exact-pair variants.
        src.group_id = gid
        if not any(w.is_default for w in group_variants(session, gid)):
            src.is_default = True
        session.add(src)
        session.commit()

    created, any_adapt_failed, skipped = [], False, []
    # Issue #46(7): (group, source, target) uniqueness. Re-copying a pair used to mint a
    # SILENT duplicate variant (dispatch then picks the lower id, so the newer one looks
    # like it did nothing). Guarded per pair — a mixed submit still creates the pairs the
    # group does not have yet, and only the already-covered ones are reported skipped.
    # N1: compared against the VIEWER-VISIBLE members when a viewer is given -- a pair
    # only a foreign variant covers is not coverage for them (see the docstring).
    existing_pairs = [
        (v.source_locale, v.target_locale)
        for v in group_variants(session, gid)
        if variant_visible(viewer, v)
    ]
    # The source's own template fields, read ONCE: the N2 savepoint rollback below expires
    # the session's ORM objects, and re-reading `src` mid-loop would then fire a lazy
    # refresh at an awkward moment.
    src_name, src_scope = src.name, src.scope
    src_customer_id, src_project_id = new_customer_id, src.project_id
    if src_customer_id != src.customer_id and src_scope == Scope.GLOBAL:
        src_scope = Scope.CUSTOMER  # #468: a bound requester's copy of a global template
    src_org_id = getattr(src, "org_id", None)
    src_created_via = getattr(src, "created_via", None)
    for source_locale, target_locale in norm_pairs:
        tgt_pair_str = _pair(source_locale, target_locale)
        dup = {
            "source": source_locale,
            "target": target_locale,
            "pair": tgt_pair_str,
            "reason": f"this group already has a {tgt_pair_str} variant",
        }
        if any(
            _same_pair(source_locale, target_locale, esrc, etgt) for esrc, etgt in existing_pairs
        ):
            skipped.append(dup)
            continue
        clone = Workflow(
            # Issue #46(6): name from the source's STEM, so copying FROM a variant yields
            # `ACME [en-US->de-DE]`, never `ACME [en-US->fr-FR] [en-US->de-DE]`.
            name=pair_name(src_name, source_locale, target_locale),
            scope=src_scope,
            customer_id=src_customer_id,
            project_id=src_project_id,
            org_id=src_org_id,
            group_id=gid,
            source_locale=source_locale,
            target_locale=target_locale,
            is_default=False,
            # Origin stamp (Phase 3): a pair variant INHERITS the source's origin — the
            # variants a Design-a-workflow multi-pair draft fans out are all "draft"
            # (the base is stamped "draft" before this clone), and a manual
            # Copy-to-language-pairs inherits "manual". Legacy source -> NULL.
            created_via=src_created_via,
        )
        # R2 (adversarial review): the read-then-write duplicate guard above is a TOCTOU
        # window -- two interleaved copies both read "no de-DE" and both created one, and
        # dispatch then silently picked the lower id (exactly the #46 symptom, back again
        # by another door). No schema/UNIQUE index is available without a migration, so:
        # FLUSH the clone (this takes SQLite's write lock, which serializes the rival
        # writer behind us), then re-read the group's pairs INSIDE the same transaction.
        # A rival that committed first is now visible -> discard our clone and report it
        # as an ordinary skip (identical API shape). The reservation is committed before
        # the steps are cloned, so the lock is never held across the adapt LLM calls.
        #
        # N2 (re-review): the discard is a SAVEPOINT rollback, not a session rollback, so
        # it can only ever undo THIS clone -- a caller holding unrelated pending rows used
        # to lose them to a duplicate it did not cause. It also scopes what the writer
        # lock has to protect. NB the serialization itself is a SQLite property (single
        # writer); under Postgres READ COMMITTED both inserts can pass the re-check, which
        # is why docs/CLI.md states the guarantee per-engine. A rare Postgres duplicate is
        # harmless to dispatch (lower id wins, deterministically) -- a UNIQUE index is the
        # real cure and needs a migration, deliberately not taken on this branch.
        try:
            with session.begin_nested():
                session.add(clone)
                session.flush()
                rival = [
                    v
                    for v in session.exec(select(Workflow).where(Workflow.group_id == gid)).all()
                    if v.id != clone.id
                    and variant_visible(viewer, v)
                    and _same_pair(source_locale, target_locale, v.source_locale, v.target_locale)
                ]
                if rival:
                    raise _RacedDuplicatePair()
        except _RacedDuplicatePair:
            skipped.append(dup)
            existing_pairs.append((source_locale, target_locale))
            continue
        session.commit()
        session.refresh(clone)
        existing_pairs.append((source_locale, target_locale))

        warnings = []
        adapted_any = False
        for st in src_steps:
            prompt_text = st.prompt_text
            # A non-adapt clone carries the SOURCE step's review flag verbatim (a
            # drafted "review this" step must stay flagged on EVERY pair variant).
            # The adapt=True path below deliberately (re)sets needs_review=True
            # because an LLM-rewritten prompt always wants a linguist's eyes.
            needs_review = bool(getattr(st, "needs_review", False))
            # SPEC-workflows-redesign 4b: stamp adapted_from_id on EVERY clone (BOTH
            # branches), not just the LLM-rewrite path. This is the LIVE provenance link
            # shared-base propagation walks (WorkflowStep.adapted_from_id == base.id) —
            # without it a plain (adapt=False) variant would silently drift on a base
            # edit. Every cloned step is a variant of `st`, so `st.id` is its base. (A
            # legacy variant made before 4b has NULL here and is never a propagation
            # target — conservative by construction.)
            adapted_from = st.id
            if adapt and (st.prompt_text or "").strip():
                new_text, ok = adapt_instruction(st, src_pair_str, tgt_pair_str)
                prompt_text = new_text
                needs_review = True  # adapted OR attempted -> a linguist reviews
                adapted_any = True
                if not ok:
                    any_adapt_failed = True
                    warnings.append(
                        f"step '{st.name or st.step_type}' kept its ORIGINAL prompt "
                        f"(adaptation failed) — review before activating"
                    )
            session.add(
                WorkflowStep(
                    workflow_id=clone.id,
                    order_index=st.order_index,
                    name=st.name,
                    step_type=st.step_type,
                    provider=st.provider,
                    model=st.model,
                    fallback_provider=st.fallback_provider,
                    fallback_model=st.fallback_model,
                    prompt_template_id=st.prompt_template_id,
                    prompt_text=prompt_text,
                    retry_max=st.retry_max,
                    backoff_base=st.backoff_base,
                    chunk_policy=st.chunk_policy,
                    redact_pii=st.redact_pii,
                    adaptive_feedback=getattr(st, "adaptive_feedback", False),
                    adaptive_feedback_window=getattr(st, "adaptive_feedback_window", 5),
                    doc_context_prepass=getattr(st, "doc_context_prepass", False),
                    qe_gate=getattr(st, "qe_gate", False),
                    qe_threshold=getattr(st, "qe_threshold", 70),
                    qe_provider=getattr(st, "qe_provider", None),
                    qe_autofix=getattr(st, "qe_autofix", False),
                    qe_autofix_provider=getattr(st, "qe_autofix_provider", None),
                    qe_autofix_max_rounds=getattr(st, "qe_autofix_max_rounds", 3),
                    qe_autofix_cross_check=getattr(st, "qe_autofix_cross_check", False),
                    # feat/lqa-configurable-threshold: clone the lqa pass bar too (None on a
                    # non-lqa source is inert; a configured lqa step keeps its 94 across pairs).
                    lqa_threshold=getattr(st, "lqa_threshold", None),
                    # Full step-config fidelity for ALL callers: a pinned glossary,
                    # the human assignee, and the TM-examples toggle/window must clone
                    # onto every pair variant, not just the primary (promptable multi-
                    # pair confirm + manual copy-to-pairs both rely on this).
                    glossary_id=getattr(st, "glossary_id", None),
                    assignee_email=getattr(st, "assignee_email", ""),
                    tm_examples=getattr(st, "tm_examples", True),
                    tm_examples_window=getattr(st, "tm_examples_window", 3),
                    use_image_context=getattr(st, "use_image_context", True),  # Batch 14 T2 §25.6
                    # Terminology T3 (SPEC §9-T3): the concept-glossary renderer toggle +
                    # caps clone onto every pair variant too (full step-config fidelity).
                    concept_glossary_render=getattr(st, "concept_glossary_render", False),
                    glossary_concept_cap=getattr(st, "glossary_concept_cap", 10),
                    glossary_token_cap=getattr(st, "glossary_token_cap", 0),
                    # LQA safety floor D1: the TM-protection tristate clones onto every
                    # pair variant too (full step-config fidelity for ALL callers).
                    tm_protect=getattr(st, "tm_protect", None),
                    # run-190 Item D / M5: the over-translation guard opt-out clones onto
                    # every pair variant too (full step-config fidelity).
                    over_translation_guard=getattr(st, "over_translation_guard", None),
                    # LLMemory step-level opt-out (PROPOSAL-llmemory §6): full step-config
                    # fidelity for ALL callers (same copy-site footgun as tm_protect).
                    memory_mode=getattr(st, "memory_mode", None),
                    # Batch 15 T1 (SPEC-tm-match-step-config §7.3): the six tm_match config
                    # overrides clone onto every pair variant too (full fidelity). Pool refs
                    # copy verbatim; run-time validation fails stale/cross-project refs
                    # closed (§7.3), so no per-pair re-validation is needed.
                    tm_fill_threshold=getattr(st, "tm_fill_threshold", None),
                    tm_fuzzy_lock_mode=getattr(st, "tm_fuzzy_lock_mode", None),
                    tm_lock_ice=getattr(st, "tm_lock_ice", None),
                    tm_hundreds_enter_lqa=getattr(st, "tm_hundreds_enter_lqa", None),
                    tm_context_floor=getattr(st, "tm_context_floor", None),
                    tm_pool_selection=getattr(st, "tm_pool_selection", None),
                    # SPEC §5 pre-step guard (Phase 2): the branch graph must clone
                    # faithfully onto EVERY pair variant (spec §7 multi-pair note) — carry
                    # the guard here or it drops on multi-pair copy. Already-validated data.
                    run_condition=getattr(st, "run_condition", None),
                    # SPEC §5 Phase 3: carry the stable key + branch + loop ceiling onto
                    # every pair variant. Keys are unique PER WORKFLOW and each variant is a
                    # distinct workflow, so keys copy verbatim (no collision) and branch
                    # targets keep naming keys inside the same variant (no remap needed).
                    key=getattr(st, "key", None),
                    branch=getattr(st, "branch", None),
                    max_visits=getattr(st, "max_visits", None),
                    # SPEC-parallel-routing §5.6: full step-config fidelity for the pair
                    # fan-out too -- a 16-locale variant set must run its parallel checks
                    # in parallel, not silently flatten to a sequential chain on 15 of them.
                    parallel_group=getattr(st, "parallel_group", None),
                    # PROPOSAL-external-processing-step Wave A: carry the external step's
                    # vendor + wait override onto every pair variant too (same fidelity as
                    # glossary_id/parallel_group -- a per-pair variant of an external step
                    # must keep dispatching to the same vendor).
                    vendor_id=getattr(st, "vendor_id", None),
                    external_wait_hours=getattr(st, "external_wait_hours", None),
                    adapted_from_id=adapted_from,
                    needs_review=needs_review,
                )
            )
        session.commit()
        # #356 phase 2b: a pair variant copied from a NON-base source keeps that source's own
        # live system-prompt overrides (a head is not language-specific, so `adapt` does not
        # touch it). #365: a variant copied from the group's BASE does not copy it: it inherits
        # the base's text live (pair-variant > base > org > built-in), so a later edit of the
        # base reaches it. Flag off: no-op.
        from app.services import system_prompt_overrides

        if (
            system_prompt_overrides.enabled()
            and system_prompt_overrides.base_workflow_id(session, clone.id) != workflow_id
        ):
            system_prompt_overrides.copy_workflow_overrides(session, workflow_id, clone.id)
        created.append(
            {
                "workflow_id": clone.id,
                "source": source_locale,
                "target": target_locale,
                "pair": tgt_pair_str,
                "adapted": adapted_any,
                "warnings": warnings,
            }
        )

    return {
        "group_id": gid,
        "created": created,
        "skipped": skipped,
        "any_adapt_failed": any_adapt_failed,
    }
