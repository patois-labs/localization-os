"""cmd_workflow_part2: shared leaf helpers (layered pure move)

Documented in: docs/architecture/workflow-engine.md
"""

from app.cli import _remote_impl
from app.cli._common import (
    EXIT_USAGE,
    CliError,
    _branch_label,
    _opts_label,
    _prompt_snippet,
    _session,
    op_org_id,
    op_org_scope,
    remote_mode,
    render_package_view,
    require_org_scope,
)
from app.cli.cmd_workflow_common import (
    _get_wf,
    _parallel_brackets,
    _parse_branch_flags,
    _resolve_step_ref,
)
from app.kernel import qe_autofix


# --- workflow show -----------------------------------------------------------
def _wf_show(args, emit) -> int:
    if remote_mode(args):
        return _remote_impl.workflow_show(args, emit)
    require_org_scope(args, emit)
    from app.kernel import orchestrator
    from app.kernel import workflow_shapes as wshapes

    with _session() as session:
        wf = _get_wf(session, args.workflow_id, op_org_id(args))
        steps = orchestrator.get_steps(session, wf.id)
        shapes = wshapes.detect_shapes(steps)
        shape = shapes.primary
        created_via = getattr(wf, "created_via", None)
        # Issue #19 (read-back completeness): resolve pinned glossary names in one
        # batch query so the human table + --json can show 'Legal Terms' rather than
        # just a bare id a caller has to look up separately.
        gnames = {}
        gids = {st.glossary_id for st in steps if st.glossary_id is not None}
        if gids:
            from sqlmodel import select

            from app.models import Glossary

            gnames = {
                g.id: g.name
                for g in session.exec(select(Glossary).where(Glossary.id.in_(gids))).all()
            }
        step_rows = [
            {
                "index": st.order_index,
                "id": st.id,
                "name": st.name,
                "type": st.step_type,
                "provider": st.provider,
                "model": st.model,
                "fallback_provider": st.fallback_provider,
                "fallback_model": st.fallback_model,
                "chunk_policy": st.chunk_policy,
                "redact_pii": st.redact_pii,
                # Issue #19: the pinned glossary (id + resolved name, None = project-scoped
                # matching), the step's own prompt/instruction text (or the saved template
                # it uses), and the engine-layer quality toggles a `workflow add-step` flag
                # can set but `show` never echoed back.
                "glossary_id": st.glossary_id,
                "glossary_name": gnames.get(st.glossary_id) if st.glossary_id else None,
                "prompt_template_id": st.prompt_template_id,
                "prompt_text": st.prompt_text or "",
                "adaptive_feedback": st.adaptive_feedback,
                "adaptive_feedback_window": st.adaptive_feedback_window,
                "doc_context_prepass": st.doc_context_prepass,
                "qe_gate": st.qe_gate,
                "qe_threshold": st.qe_threshold,
                "qe_provider": st.qe_provider,
                # QE auto-fix (Train B): mirrored on the /api show row (key-set parity).
                "qe_autofix": getattr(st, "qe_autofix", False),
                "qe_autofix_status": qe_autofix.display_status(st),
                "qe_autofix_provider": getattr(st, "qe_autofix_provider", None),
                "qe_autofix_max_rounds": getattr(st, "qe_autofix_max_rounds", 3),
                "qe_autofix_cross_check": getattr(st, "qe_autofix_cross_check", False),
                # feat/lqa-configurable-threshold: the lqa step's pass bar (None = the
                # built-in default 90) so `show --json` echoes back what add/edit-step can
                # set -- the Issue #19 read-surface rule, mirrored in /api (row-shape parity).
                "lqa_threshold": getattr(st, "lqa_threshold", None),
                "tm_examples": st.tm_examples,
                "tm_examples_window": st.tm_examples_window,
                # Batch 14 T2 (SPEC §25.5-D): the per-step image-context toggle so
                # `show --json` echoes what add/edit-step can set (Issue #19); MUST land on
                # BOTH this CLI row AND the /api show row (25.5-C) or the key-set parity
                # guards (verify_package_view / verify_headless_qa6_qa9 check 7) go RED.
                "use_image_context": getattr(st, "use_image_context", True),
                # Terminology T3 (SPEC §9-T3): the concept-glossary renderer toggle + caps
                # so `show --json` echoes what add/edit-step can set; MUST match the /api
                # show row (25.5-C-style key-set parity guard).
                "concept_glossary_render": getattr(st, "concept_glossary_render", False),
                "glossary_concept_cap": getattr(st, "glossary_concept_cap", 10),
                "glossary_token_cap": getattr(st, "glossary_token_cap", 0),
                # LQA safety floor D1: the TM-protection tristate (None = inherit the
                # correction-loop default) so `show --json` echoes back what add/edit-step
                # can set -- the Issue #19 read-surface rule.
                "tm_protect": getattr(st, "tm_protect", None),
                # run-190 Item D / M5: the over-translation guard opt-out tristate (None =
                # inherit the global) so `show --json` echoes what add/edit-step can set;
                # MUST match the /api show row (row-shape parity guard).
                "over_translation_guard": getattr(st, "over_translation_guard", None),
                # LLMemory step-level opt-out (PROPOSAL-llmemory §6): read surface
                # mirroring tm_protect -- `show --json` echoes back what add/edit-step
                # can set (the Issue #19 read-surface rule).
                "memory_mode": getattr(st, "memory_mode", None),
                "assignee_email": st.assignee_email,
                # QA-9 (#39): the shared-base provenance link + the linguist-review flag,
                # both set in the DB and surfaced in the web builder / group page but never
                # echoed to --json. adapted_from_id points at the SOURCE step a cross-language
                # variant was cloned from (NULL on a plain add-step); needs_review flags an
                # LLM-adapted step a linguist should post-edit before activating. A headless
                # consumer needs both to see which variant steps are flagged and their
                # cross-language provenance. Mirrored in the /api workflow-show payload so the
                # P6 row-shape guard (verify_package_view) stays green.
                "needs_review": st.needs_review,
                "adapted_from_id": st.adapted_from_id,
                # Conditional-workflow (Phase 2/3): the guard + stable key + branch graph +
                # loop ceiling so the CLI read surface mirrors the web builder's badges.
                "run_condition": st.run_condition,
                "key": st.key,
                "branch": st.branch,
                "max_visits": st.max_visits,
                # SPEC-parallel-routing §6: the group slug on the CLI read surface, so a
                # headless operator can see that two checks run together (Issue #19 rule:
                # anything add/edit-step can set, `show --json` echoes back).
                "parallel_group": getattr(st, "parallel_group", None),
                # Batch 15 T1 (SPEC-tm-match-step-config §8.D): the six tm_match config
                # overrides on the CLI read surface. MUST land on BOTH this CLI row AND the
                # /api show row (§8.C) in one diff or the key-set parity guards
                # (verify_package_view / verify_headless_qa6_qa9 check 7) go RED. None on a
                # non-tm_match step; a concrete snapshot on a tm_match step (NULL = inherit).
                "tm_fill_threshold": getattr(st, "tm_fill_threshold", None),
                "tm_fuzzy_lock_mode": getattr(st, "tm_fuzzy_lock_mode", None),
                "tm_lock_ice": getattr(st, "tm_lock_ice", None),
                "tm_hundreds_enter_lqa": getattr(st, "tm_hundreds_enter_lqa", None),
                "tm_context_floor": getattr(st, "tm_context_floor", None),
                "tm_pool_selection": getattr(st, "tm_pool_selection", None),
            }
            for st in steps
        ]
        # B0 (SPEC-workflows-redesign Track B): attach the per-step PACKAGE/VERIFY view
        # (single-sourced from package_view, mirroring the builder panel + /api) onto each
        # row. gnames is reused so no duplicate glossary-name query fires. zip aligns
        # because step_rows is built in `steps` order.
        from app.kernel import package_view as pv

        for row, pview in zip(
            step_rows,
            pv.workflow_package_view(session, wf, steps, glossary_names=gnames),
            strict=False,
        ):
            row["package"] = pview["package"]
            row["verify"] = pview["verify"]
        if emit.as_json:
            emit.json(
                {
                    "ok": True,
                    "id": wf.id,
                    "name": wf.name,
                    "scope": wf.scope,
                    "shape": shape,
                    "shape_tags": shapes.sorted_tags(),
                    "created_via": created_via,
                    "archived": wf.archived_at is not None,  # L11 (parity with /api)
                    "steps": step_rows,
                }
            )
        else:
            origin = wshapes.origin_label(created_via) or "unknown"
            extra = ", ".join(
                wshapes.SHAPE_LABELS[tag] for tag in shapes.sorted_tags() if tag != shape
            )
            shape_label = wshapes.SHAPE_LABELS[shape] + (f" (+{extra})" if extra else "")
            emit.say(
                f"Workflow #{wf.id}: {wf.name} (scope={wf.scope}, "
                f"{len(steps)} step(s), shape={shape_label}, "
                f"origin={origin})"
            )
            # SPEC-parallel-routing §6: ASCII group brackets in the step list. The
            # column is EMPTY on every ungrouped workflow, so existing output is
            # unchanged in substance; a group renders as a /,|,\ bracket run naming
            # the slug, which is what makes a MISTYPED slug (two intended lanes that
            # silently became two lone steps) visible on the surface an author reads.
            par = _parallel_brackets(step_rows)
            emit.table(
                [
                    (
                        s["index"],
                        s["id"],
                        par[i],
                        s["key"] or "",
                        s["name"] or "",
                        s["type"],
                        s["provider"] or "",
                        s["glossary_name"] or (str(s["glossary_id"]) if s["glossary_id"] else "-"),
                        (
                            f"on({s['qe_threshold']})"
                            + (
                                "+fix"
                                if s["qe_autofix_status"] == "auto-fix on"
                                else " " + s["qe_autofix_status"]
                                if s.get("qe_autofix")
                                else ""
                            )
                            if s["qe_gate"]
                            else s["qe_autofix_status"]
                            if s.get("qe_autofix")
                            else "-"
                        ),
                        _opts_label(s),
                        _branch_label(s["branch"]),
                        _prompt_snippet(s["prompt_text"], s["prompt_template_id"]),
                    )
                    for i, s in enumerate(step_rows)
                ],
                headers=(
                    "idx",
                    "id",
                    "par",
                    "key",
                    "name",
                    "type",
                    "provider",
                    "glossary",
                    "qe",
                    "opts",
                    "branch",
                    "prompt",
                ),
            )
            if any(par):
                emit.say(
                    "par: steps bracketed together (/ | \\) run in PARALLEL "
                    "against the same text; the next step waits for all of them. "
                    "A '.' marks a LONE member -- no adjacent step shares its "
                    "group, so it just runs on its own, in order."
                )
            render_package_view(emit, step_rows)
    return 0


# --- workflow add-step -------------------------------------------------------
def _wf_add_step(args, emit) -> int:
    if remote_mode(args):
        return _remote_impl.workflow_add_step(args, emit)
    require_org_scope(args, emit)
    from app.kernel import workflows

    with _session() as session:
        _get_wf(session, args.workflow_id, op_org_id(args))  # 404 guard
        # T1-01 (web+CLI parity): reject an llm_agent step whose non-empty prompt omits
        # {text}/{prior} (it would send the model no source). Shared validator.
        from app.kernel import prompts

        try:
            # GitHub #43: the return value is the SOFT language warning (a prompt that
            # names no target language). Surfaced on the result line below, never fatal.
            prompt_warning = prompts.validate_step_prompt(args.step_type, args.prompt_text)
        except prompts.PromptValidationError as exc:
            raise CliError(str(exc), EXIT_USAGE) from exc
        # F3 (web+CLI parity): --glossary pins an approved module glossary for this
        # step, re-validated exactly as the web POST does -- it must be an APPROVED
        # glossary VISIBLE to this operator's org (a workflow is language-agnostic, so
        # every approved glossary is offerable; a pin that does not cover a run's
        # languages is skipped at run time with a note). Unlike the web (which silently
        # drops a tampered id -> project-scoped), a CLI author gets a CLEAR ERROR so an
        # invalid/unapproved/foreign id is never silently ignored.
        glossary_id = getattr(args, "glossary_id", None)
        if glossary_id is not None:
            from app.kernel import glossary_module as gm

            allowed = {
                g.id
                for g in gm.options_for_step(
                    # #73 COUPLING: op_org_SCOPE -- options_for_step now fails closed on a
                    # bare None under an active gate, so the --operator door hands it
                    # access.UNSCOPED to keep the cross-org offered set; op_org_id would
                    # return a bare None and the operator would see zero glossaries.
                    session,
                    "",
                    "",
                    org_id=op_org_scope(args),
                )
            }
            if glossary_id not in allowed:
                raise CliError(
                    f"glossary {glossary_id} is not an approved glossary "
                    f"(or is not visible to this org).",
                    EXIT_USAGE,
                )
        # SPEC-prompt-template-runtime: --prompt-template must name a template VISIBLE to
        # this operator's org (foreign-org isolation), mirroring the glossary check. The
        # service then enforces existence + the source-placeholder rule and refuses it as
        # a DEAD field on an lqa/human_review step.
        prompt_template_id = getattr(args, "prompt_template_id", None)
        if prompt_template_id is not None:
            visible = {
                t.id for t in workflows.list_prompt_templates(session, org_id=op_org_id(args))
            }
            if prompt_template_id not in visible:
                raise CliError(
                    f"prompt template {prompt_template_id} is not visible to this org "
                    "(or does not exist).",
                    EXIT_USAGE,
                )
        # SPEC §5 Phase 3: parse the repeatable --branch on_pass=<t>/on_fail=<t> flags
        # into the {on_pass, on_fail} dict add_step validates (D3 gate rule + target
        # resolution, fail-closed). A malformed 'slot=value' shape is a clean usage error.
        branch = _parse_branch_flags(getattr(args, "branch", []) or [])
        # SPEC §5 pre-step guard (Phase 2): --run-condition is parsed + validated
        # inside add_step against the workflow's earlier steps (fail-closed). A
        # malformed/unresolvable guard raises WorkflowError -> a clean CLI usage error.
        try:
            step = workflows.add_step(
                session,
                args.workflow_id,
                name=args.name,
                step_type=args.step_type,
                provider=args.provider,
                model=args.model,
                fallback_provider=args.fallback_provider,
                fallback_model=args.fallback_model,
                prompt_text=args.prompt_text,
                prompt_template_id=prompt_template_id,
                chunk_policy=args.chunk_policy,
                redact_pii=getattr(args, "redact_pii", False),
                adaptive_feedback=getattr(args, "adaptive_feedback", False),
                adaptive_feedback_window=getattr(args, "adaptive_feedback_window", 5),
                doc_context_prepass=getattr(args, "doc_context_prepass", False),
                qe_gate=getattr(args, "qe_gate", False),
                qe_threshold=getattr(args, "qe_threshold", 70),
                # feat/lqa-configurable-threshold: the lqa step's pass bar (None -> the
                # built-in default 90; refused on a non-lqa step in add_step).
                lqa_threshold=getattr(args, "lqa_threshold", None),
                tm_examples=getattr(args, "tm_examples", True),
                tm_examples_window=getattr(args, "tm_examples_window", 3),
                use_image_context=getattr(args, "use_image_context", True),  # Batch 14 T2 §25.5-D
                # Terminology T3 (SPEC §9-T3): the renderer toggle + caps (add_step coerces
                # the caps; default OFF/10/0 = byte-identical).
                concept_glossary_render=getattr(args, "concept_glossary_render", False),
                glossary_concept_cap=getattr(args, "glossary_concept_cap", 10),
                glossary_token_cap=getattr(args, "glossary_token_cap", 0),
                tm_protect=getattr(args, "tm_protect", "auto"),
                # run-190 Item D / M5: the over-translation guard opt-out ("auto"->inherit).
                over_translation_guard=getattr(args, "over_translation_guard", "auto"),
                memory_mode=getattr(args, "memory_mode", "auto"),
                # Batch 15 T1 (SPEC-tm-match-step-config §8.D): the six tm_match config
                # overrides. Defaults (auto/None/"") mean inherit, which SEEDS a concrete
                # snapshot on a NEW tm_match step; add_step coerces + refuses a value forged
                # onto a non-tm_match step.
                tm_fill_threshold=getattr(args, "tm_fill_threshold", None),
                tm_fuzzy_lock_mode=getattr(args, "tm_fuzzy_lock_mode", None),
                tm_lock_ice=getattr(args, "tm_lock_ice", "auto"),
                tm_hundreds_enter_lqa=getattr(args, "tm_hundreds_enter_lqa", "auto"),
                tm_context_floor=getattr(args, "tm_context_floor", None),
                tm_pool_selection=getattr(args, "tm_pool_selection", "") or None,
                qe_provider=getattr(args, "qe_provider", ""),
                # QE auto-fix (Train B): role-checked; invalid rounds refused in add_step.
                qe_autofix=getattr(args, "qe_autofix", False),
                qe_autofix_provider=getattr(args, "qe_autofix_provider", ""),
                qe_autofix_max_rounds=getattr(args, "qe_autofix_max_rounds", 3),
                qe_autofix_cross_check=getattr(args, "qe_autofix_cross_check", False),
                glossary_id=glossary_id,
                run_condition=getattr(args, "run_condition", "") or None,
                key=getattr(args, "key", "") or None,
                branch=branch,
                max_visits=getattr(args, "max_visits", None),
                # SPEC-parallel-routing §6: slugified + membership-validated in
                # add_step (fail-closed) -> a clean CLI usage error, never a traceback.
                parallel_group=getattr(args, "parallel_group", "") or None,
                # PROPOSAL-external-processing-step §9: the external step's live config.
                # add_step validates fail-closed (a stray vendor on a non-external step,
                # an unlinked/archived/foreign vendor) -> a clean CLI usage error.
                vendor_id=getattr(args, "vendor_id", None),
                external_wait_hours=getattr(args, "external_wait_hours", None),
            )
        except workflows.WorkflowError as exc:
            raise CliError(str(exc), EXIT_USAGE) from exc
        # ONE warning shape across add-step and edit-step (adversarial review P12c): the
        # same `warning: ` prefix, on its OWN line after the result sentence, and the
        # same `warnings` key in the same position of the --json payload. The two used to
        # differ in prefix, joiner and key order, which is exactly the drift the parity
        # rule exists to stop.
        text = (
            f"Added step #{step.id} ({step.step_type}, index {step.order_index}) "
            f"to workflow #{args.workflow_id}."
        )
        from app.kernel.qe_autofix import configuration_warnings

        save_warnings = ([prompt_warning] if prompt_warning else []) + configuration_warnings(step)
        for warning in save_warnings:
            text += "\nwarning: " + warning
        emit.result(
            text,
            {
                "ok": True,
                "id": step.id,
                "workflow_id": args.workflow_id,
                "order_index": step.order_index,
                "step_type": step.step_type,
                "name": step.name,
                "glossary_id": step.glossary_id,
                "run_condition": step.run_condition,
                "key": step.key,
                "branch": step.branch,
                "max_visits": step.max_visits,
                "parallel_group": step.parallel_group,
                # Present ONLY when something warned, so an unwarned --json payload is
                # byte-identical to before (the CLI/web parity rule for optional facts).
                **({"warnings": save_warnings} if save_warnings else {}),
            },
        )
    return 0


# --- workflow edit-step (PARTIAL update) -------------------------------------
def _wf_edit_step(args, emit) -> int:
    """Edit an existing step in place. PARTIAL: only the flags actually passed are sent
    to update_step (the rest stay as-is). The service re-validates the whole graph and
    fails closed (a clean usage error) if the edit would break a dependent step's guard/
    branch, or if the workflow has an in-flight run. --remote-capable (matches add-step)."""
    if remote_mode(args):
        return _remote_impl.workflow_edit_step(args, emit)
    require_org_scope(args, emit)
    from app.kernel import prompts, workflows
    from app.models import WorkflowStep

    with _session() as session:
        _get_wf(session, args.workflow_id, op_org_id(args))  # org 404 guard
        step = session.get(WorkflowStep, args.step_id)
        if step is None or step.workflow_id != args.workflow_id:
            raise CliError(f"no such step: {args.step_id}", EXIT_USAGE)
        if getattr(args, "branch", None) and getattr(args, "clear_branch", False):
            raise CliError("pass either --branch or --clear-branch, not both.", EXIT_USAGE)

        # Assemble ONLY the fields the operator passed (None sentinel = leave unchanged),
        # so update_step's partial contract is honoured.
        kw = {}
        for f in (
            "name",
            "step_type",
            "provider",
            "model",
            "fallback_provider",
            "fallback_model",
            "prompt_text",
            "chunk_policy",
            "assignee_email",
            "adaptive_feedback_window",
            "qe_threshold",
            "qe_provider",
            # QE auto-fix (Train B): present sets / absent leaves unchanged.
            "qe_autofix_provider",
            "qe_autofix_max_rounds",
            "lqa_threshold",
            "tm_examples_window",
            "max_visits",
            "redact_pii",
            "adaptive_feedback",
            "doc_context_prepass",
            "qe_gate",
            "qe_autofix",
            "qe_autofix_cross_check",
            "tm_examples",
            # Batch 14 T2 (SPEC §25.5-D): a field enters kw only when set (tri-state
            # --image-context / --no-image-context; absent leaves it unchanged).
            "use_image_context",
            # Terminology T3 (SPEC §9-T3): the renderer toggle (tri-state) + the two caps
            # (present sets / absent leaves unchanged; update_step clamps to >=0).
            "concept_glossary_render",
            "glossary_concept_cap",
            "glossary_token_cap",
            "tm_protect",
            # run-190 Item D / M5: enters kw only when --over-translation-guard is passed
            # (absent leaves it unchanged; "auto"->inherit, "off"->opt out).
            "over_translation_guard",
            "memory_mode",
            # Batch 15 T1 (SPEC-tm-match-step-config §8.D): the six tm_match config
            # overrides enter kw only when supplied (partial contract). An "auto"/"" value
            # clears the override back to inherit; a concrete value overrides. update_step
            # refuses a value on a non-tm_match step and force-clears on a type switch.
            "tm_fill_threshold",
            "tm_fuzzy_lock_mode",
            "tm_lock_ice",
            "tm_hundreds_enter_lqa",
            "tm_context_floor",
            "tm_pool_selection",
            # PROPOSAL-external-processing-step §9: external-step live config;
            # update_step re-validates fail-closed on either.
            "vendor_id",
            "external_wait_hours",
        ):
            v = getattr(args, f, None)
            if v is not None:
                kw[f] = v
        # Clearable conditional text fields: '' clears (validated in the service).
        if getattr(args, "run_condition", None) is not None:
            kw["run_condition"] = args.run_condition or None
        if getattr(args, "key", None) is not None:
            kw["key"] = args.key or None
        if getattr(args, "clear_branch", False):
            kw["branch"] = None
        elif getattr(args, "branch", None):
            kw["branch"] = _parse_branch_flags(args.branch)
        # SPEC-parallel-routing §6: --parallel-group joins, --no-parallel-group leaves
        # (mutually exclusive at the parser); neither = unchanged.
        if getattr(args, "no_parallel_group", False):
            kw["parallel_group"] = None
        elif getattr(args, "parallel_group", None) is not None:
            kw["parallel_group"] = args.parallel_group or None
        # Glossary pin: re-validate exactly like add-step (approved, visible).
        glossary_id = getattr(args, "glossary_id", None)
        if glossary_id is not None:
            from app.kernel import glossary_module as gm

            allowed = {
                g.id
                for g in gm.options_for_step(
                    # #73 COUPLING: op_org_SCOPE -- options_for_step now fails closed on a
                    # bare None under an active gate, so the --operator door hands it
                    # access.UNSCOPED to keep the cross-org offered set; op_org_id would
                    # return a bare None and the operator would see zero glossaries.
                    session,
                    "",
                    "",
                    org_id=op_org_scope(args),
                )
            }
            if glossary_id not in allowed:
                raise CliError(
                    f"glossary {glossary_id} is not an approved glossary "
                    f"(or is not visible to this org).",
                    EXIT_USAGE,
                )
            kw["glossary_id"] = glossary_id

        # SPEC-prompt-template-runtime: --prompt-template <id> binds a saved template; ''
        # clears the binding. A non-empty id must be a digit VISIBLE to this org (foreign-
        # org isolation); the service then enforces existence + the source-placeholder
        # rule (and refuses a dead binding on a type switch to lqa/human_review).
        if getattr(args, "prompt_template_id", None) is not None:
            raw_pt = str(args.prompt_template_id).strip()
            if raw_pt:
                from app.kernel import ids

                pt = ids.coerce_id(raw_pt)
                if pt is None:
                    raise CliError(
                        "--prompt-template must be a template id, or '' to clear.", EXIT_USAGE
                    )
                visible = {
                    t.id for t in workflows.list_prompt_templates(session, org_id=op_org_id(args))
                }
                if pt not in visible:
                    raise CliError(
                        f"prompt template {raw_pt} is not visible to this org (or does not exist).",
                        EXIT_USAGE,
                    )
            kw["prompt_template_id"] = raw_pt or None

        # SPEC-workflows-redesign 4b: the deliberate pin/unpin intent (None = leave to the
        # auto-pin heuristic in update_step). Mutually exclusive at the parser.
        override_intent = (
            True
            if getattr(args, "pin", False)
            else False
            if getattr(args, "unpin", False)
            else None
        )

        # PURE pin/unpin (no field flags): a lightweight, run-safe metadata flip.
        if not kw:
            if override_intent is None:
                raise CliError(
                    "no fields to edit; pass at least one field flag (or --pin / --unpin).",
                    EXIT_USAGE,
                )
            updated = workflows.set_step_override(session, args.step_id, override_intent)
            verb = "Pinned" if override_intent else "Unpinned"
            tail = (
                ""
                if override_intent
                else " (rejoins base-edit propagation from the next base edit; no retro-sync)"
            )
            emit.result(
                f"{verb} step #{updated.id} in workflow #{args.workflow_id}{tail}.",
                {
                    "ok": True,
                    "id": updated.id,
                    "workflow_id": args.workflow_id,
                    "overridden": updated.overridden,
                },
            )
            return 0

        # T1-01 parity: validate the EFFECTIVE (post-edit) prompt against the effective
        # type so a prompt edit that drops {text}/{prior} on an llm_agent is refused.
        eff_type = kw.get("step_type", step.step_type)
        eff_prompt = kw.get("prompt_text", step.prompt_text)
        try:
            # GitHub #43: the return value is the SOFT language warning; surfaced on
            # the result line below (never fatal), exactly as add-step does.
            prompt_warning = prompts.validate_step_prompt(eff_type, eff_prompt)
        except prompts.PromptValidationError as exc:
            raise CliError(str(exc), EXIT_USAGE) from exc
        # ...but ONLY when this edit actually touched the effective prompt (adversarial
        # review P12e). The HARD rule is re-checked against the effective values on every
        # edit by design — a type switch alone can make a saved prompt illegal. The SOFT
        # language warning is an AUTHORING nudge about the prompt text, so re-emitting it
        # on an edit that only renamed the step (or changed its provider, its threshold,
        # its glossary...) is noise about something the operator did not do, and noise is
        # how a warning gets trained out of being read. Warn when the prompt itself
        # changed, or when a type switch newly brought the prompt under the rule.
        if not ("prompt_text" in kw or "step_type" in kw):
            prompt_warning = ""

        # Pass set_override ONLY when explicitly requested; otherwise update_step's auto-pin
        # heuristic applies (a direct variant edit pins itself). report collects the 4b
        # propagation buckets so the operator sees exactly what a base edit swept.
        report: dict = {}
        extra = {} if override_intent is None else {"set_override": override_intent}
        try:
            updated = workflows.update_step(session, args.step_id, report=report, **extra, **kw)
        except workflows.WorkflowError as exc:
            raise CliError(str(exc), EXIT_USAGE) from exc

        def _labels(key):
            return [e.get("label", "?") for e in report.get(key, [])]

        prop = {
            k: _labels(k)
            for k in (
                "propagated",
                "skipped_adapted",
                "pinned",
                "validation_skipped",
                # SPEC-parallel-routing (adversarial review, PROPAGATION-10): a variant
                # left untouched because a run is in flight there, and a variant where
                # the swept group landed as a LONE (inert) member.
                "run_active_skipped",
                "propagated_as_singleton",
            )
        }
        lines = [
            f"Edited step #{updated.id} ({updated.step_type}, index "
            f"{updated.order_index}) in workflow #{args.workflow_id}. Changed: "
            f"{', '.join(sorted(kw))}."
        ]
        if report.get("auto_pinned"):
            lines.append(
                "This step is now pinned to this variant (a direct edit) -- base "
                "edits will skip it until you unpin."
            )
        # SPEC-lqa-step-honest-options (adversarial-review finding): a TYPE SWITCH
        # never refuses a now-dead field (nothing to act on -- the builder/CLI can
        # only hide it) -- it force-clears it instead, reported here.
        tsc = [
            workflows._DEAD_FIELD_LABELS.get(f, f) for f in report.get("type_switch_cleared", [])
        ]
        if tsc:
            lines.append(f"Cleared {', '.join(tsc)} (no effect on the new step type).")
        if prop["propagated"]:
            lines.append(f"Propagated to: {', '.join(prop['propagated'])}.")
        if prop["skipped_adapted"]:
            lines.append(
                f"Kept the adapted prompt (flagged for review) on: "
                f"{', '.join(prop['skipped_adapted'])}."
            )
        if prop["pinned"]:
            lines.append(f"Skipped pinned variant(s): {', '.join(prop['pinned'])}.")
        if prop["validation_skipped"]:
            lines.append(
                f"Skipped (edit would break the variant graph): "
                f"{', '.join(prop['validation_skipped'])}."
            )
        if prop["run_active_skipped"]:
            lines.append(
                f"Skipped (a run is in flight there; graph changes not "
                f"applied): {', '.join(prop['run_active_skipped'])}. "
                "Re-apply after the run finishes."
            )
        if prop["propagated_as_singleton"]:
            lines.append(
                "Heads up -- no adjacent step shares that parallel group on: "
                f"{', '.join(prop['propagated_as_singleton'])}; those checks run "
                "SEQUENTIALLY there while this workflow runs them in parallel."
            )
        # Propagation's OWN dead-field sweep (a variant's resulting type made one of
        # its fields dead, whether via the delta or a pre-existing value) -- SKIP-
        # AND-REPORT, never a hard failure.
        dfc = report.get("dead_fields_cleared", [])
        if dfc:
            bits = [
                f"{e.get('label', '?')} "
                f"({', '.join(workflows._DEAD_FIELD_LABELS.get(f, f) for f in e.get('fields', []))})"  # noqa: E501
                for e in dfc
            ]
            lines.append(f"Cleared now-dead field(s) on: {'; '.join(bits)}.")
        # Same warning shape as add-step (P12c): own line, `warning: ` prefix, and the
        # `warnings` key LAST in the --json payload.
        text = " ".join(lines)
        from app.kernel.qe_autofix import configuration_warnings

        save_warnings = ([prompt_warning] if prompt_warning else []) + configuration_warnings(
            updated
        )
        for warning in save_warnings:
            text += "\nwarning: " + warning
        emit.result(
            text,
            {
                "ok": True,
                "id": updated.id,
                "workflow_id": args.workflow_id,
                "order_index": updated.order_index,
                "step_type": updated.step_type,
                "name": updated.name,
                "changed": sorted(kw),
                "glossary_id": updated.glossary_id,
                "run_condition": updated.run_condition,
                "key": updated.key,
                "branch": updated.branch,
                "max_visits": updated.max_visits,
                "parallel_group": updated.parallel_group,
                "overridden": updated.overridden,
                "auto_pinned": bool(report.get("auto_pinned")),
                "type_switch_cleared": report.get("type_switch_cleared", []),
                "propagation": prop,
                "dead_fields_cleared": dfc,
                # Present ONLY when something warned, so an unwarned --json payload is
                # byte-identical to before (the CLI/web parity rule for optional facts).
                **({"warnings": save_warnings} if save_warnings else {}),
            },
        )
    return 0


# --- workflow delete-step ----------------------------------------------------
def _wf_delete_step(args, emit) -> int:
    if remote_mode(args):
        return _remote_impl.workflow_delete_step(args, emit)
    require_org_scope(args, emit)
    from app.kernel import workflows

    with _session() as session:
        _get_wf(session, args.workflow_id, op_org_id(args))  # 404 guard
        # delete_step is now fail-closed (an in-flight run, or a delete that would
        # orphan a dependent guard/branch) -- surface it as a clean usage error, the
        # same shape add-step/edit-step already use, never a traceback.
        try:
            deleted = workflows.delete_step(session, args.step_id, workflow_id=args.workflow_id)
        except workflows.WorkflowError as exc:
            raise CliError(str(exc), EXIT_USAGE) from exc
        if not deleted:
            raise CliError(f"no such step: {args.step_id}", EXIT_USAGE)
        emit.result(
            f"Deleted step #{args.step_id} from workflow #{args.workflow_id}.",
            {"ok": True, "deleted": True, "step_id": args.step_id, "workflow_id": args.workflow_id},
        )
    return 0


def _wf_move_step(args, emit) -> int:
    """Reorder a step (web parity: POST /workflows/{id}/steps/{sid}/move).

    --to takes the 0-based position the step ends up at. --before/--after name another
    step (by key or id) and resolve to that position, which is friendlier for a keyed
    graph. All three land on the SAME seam (workflows.reorder_step) with the same
    fail-closed refusals: an in-flight run, or a move that would introduce a NEW graph
    error (a gate pushed below the step that depends on it, a parallel group split).

    --parallel-group / --clear-parallel-group compose the LANE MEMBERSHIP change into the
    same seam call, so the headless surface has the drag's full capability: the move and
    the slug change are validated together as one final state and land in one commit.
    Leaving a group is only expressible this way -- clearing the slug without also moving
    out of the members' run, or moving out without clearing it, is refused by the
    consecutiveness rule either way."""
    if remote_mode(args):
        return _remote_impl.workflow_move_step(args, emit)
    require_org_scope(args, emit)
    from app.kernel import orchestrator, workflows

    with _session() as session:
        _get_wf(session, args.workflow_id, op_org_id(args))  # org 404 guard
        steps = orchestrator.get_steps(session, args.workflow_id)
        if not any(s.id == args.step_id for s in steps):
            raise CliError(
                f"no such step in workflow {args.workflow_id}: {args.step_id}", EXIT_USAGE
            )
        rest = [s for s in steps if s.id != args.step_id]
        if args.to_position is not None:
            pos = args.to_position
        else:
            ref = args.before_ref if args.before_ref is not None else args.after_ref
            at = _resolve_step_ref(rest, ref)
            if at is None:
                raise CliError(
                    "no step matches %r in workflow %d (give another step's key or id; "  # noqa: UP031
                    "the step being moved is not a valid anchor)" % (ref, args.workflow_id),
                    EXIT_USAGE,
                )
            pos = at if args.before_ref is not None else at + 1
        kw = {}
        if args.clear_parallel_group:
            kw["parallel_group"] = None
        elif args.parallel_group is not None:
            kw["parallel_group"] = args.parallel_group
        report: dict = {}
        try:
            moved = workflows.reorder_step(
                session, args.workflow_id, args.step_id, pos, report=report, **kw
            )
        except workflows.WorkflowError as exc:
            raise CliError(str(exc), EXIT_USAGE) from exc
        order = [
            {
                "id": s.id,
                "position": s.order_index,
                "key": s.key,
                "name": s.name,
                "step_type": s.step_type,
            }
            for s in orchestrator.get_steps(session, args.workflow_id)
        ]
        listing = ", ".join(
            "%d:%s" % (r["position"], r["key"] or r["name"] or ("#%d" % r["id"]))  # noqa: UP031
            for r in order  # noqa: RUF100, UP031
        )
        lines = [
            "Moved step #%d to position %d in workflow #%d. Order is now: %s"  # noqa: UP031
            % (moved.id, moved.order_index, args.workflow_id, listing)
        ]
        if report.get("noop"):
            lines = [
                "Step #%d is already at position %d in workflow #%d; nothing "  # noqa: UP031
                "changed. Order is: %s" % (moved.id, moved.order_index, args.workflow_id, listing)
            ]
        if kw:
            lines.append(
                "Parallel group is now %s."
                % (repr(moved.parallel_group) if moved.parallel_group else "(none)")
            )
        # Web parity: the builder shows this same sentence as a banner. Reordering really
        # does change which step can answer from TM alone, so both surfaces say so.
        if report.get("tm_first_note"):
            lines.append(report["tm_first_note"])
        prop = {
            k: report.get(k, [])
            for k in (
                "propagated",
                "pinned",
                "skipped_adapted",
                "validation_skipped",
                "run_active_skipped",
                "propagated_as_singleton",
            )
        }
        if any(prop.values()):
            lines.append(
                "Variants: {}.".format(
                    ", ".join("%s=%d" % (k, len(v)) for k, v in sorted(prop.items()) if v)  # noqa: UP031
                )
            )
        if report.get("auto_pinned"):
            lines.append("This variant step is now pinned (you edited it directly).")
        emit.result(
            " ".join(lines),
            {
                "ok": True,
                "step_id": moved.id,
                "workflow_id": args.workflow_id,
                "position": moved.order_index,
                "order": order,
                "parallel_group": moved.parallel_group,
                "noop": bool(report.get("noop")),
                "tm_first_note": report.get("tm_first_note", ""),
                "auto_pinned": bool(report.get("auto_pinned")),
                "propagation": prop,
            },
        )
    return 0


# --- workflow clone ----------------------------------------------------------
def _wf_clone(args, emit) -> int:
    if remote_mode(args):
        return _remote_impl.workflow_clone(args, emit)
    require_org_scope(args, emit)
    from app.kernel import orchestrator, workflows

    with _session() as session:
        _get_wf(session, args.workflow_id, op_org_id(args))  # org 404 guard
        new = workflows.clone_workflow(session, args.workflow_id)
        if new is None:
            raise CliError(f"no such workflow: {args.workflow_id}", EXIT_USAGE)
        n = len(orchestrator.get_steps(session, new.id))
        emit.result(
            f"Cloned workflow #{args.workflow_id} -> #{new.id}: {new.name} ({n} step(s)).",
            {"ok": True, "id": new.id, "name": new.name, "steps": n, "source_id": args.workflow_id},
        )
    return 0
