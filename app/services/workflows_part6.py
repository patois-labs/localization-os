"""workflows: part 6 (layered pure move)

Documented in: docs/architecture/workflow-engine.md
"""

from sqlmodel import Session, select

from app.models import (
    StepType,
    Workflow,
    WorkflowRun,
    WorkflowStep,
)
from app.services import context_pkg, ids, orchestrator
from app.services.qe_autofix import clamp_rounds as _clamp_autofix_rounds
from app.services.workflows_common import (
    _ACTIVE_RUN_STATUSES,
    _EDIT_FIELDS,
    DEAD_FIELDS_BY_TYPE,
    WorkflowError,
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
    _resolve_provider_edit,
    _step_attr,
)
from app.services.workflows_common_leaf import _require_whole_number
from app.services.workflows_part2 import (
    _GRAPH_FIELDS,
    _GRAPH_SHAPING_FIELDS,
    _UNSET,
    _coerce_tm_tristate_bool,
    _coerce_wait_hours,
    _graph_row,
    _ordered,
    _require_valid_lqa_threshold,
    _require_valid_tm_pool_selection,
    _require_valid_tm_scalar,
    _tm_first_note,
    _validate_step_vendor,
    _variant_report_entry,
    validate_bound_template,
    validate_parallel_group,
)
from app.services.workflows_part2_leaf import MAX_STEP_INT_OVERRIDE
from app.services.workflows_part3 import (
    _DEAD_FIELD_LABELS,
    _group_span,
    _pre_group_prior,
    validate_branch,
    validate_key,
)
from app.services.workflows_part4 import (
    _force_clear_dead_fields,
    refuse_dead_fields,
    validate_run_condition,
)
from app.services.workflows_part5 import (
    _assert_autofix_fixer,
    _assert_step_provider_roles,
    _graph_edit_errors,
)


def _autofix_provider_edit(incoming):
    """The QE auto-fix post-editor on an EDIT (review B2). Unlike _resolve_provider_edit,
    an explicit blank CLEARS a pinned engine back to NULL ("Same as this step"): the
    auto-fix picker renders an unavailable pinned engine as its own selected option, so
    a blank submission is always the user's choice, never a dropped option. An ABSENT
    field never reaches here (the partial-update contract keeps the stored value)."""
    return (incoming or "").strip() or None


def _reorder_step_locked(
    session, workflow_id, step_id, new_position, parallel_group, propagate, report
):
    """reorder_step's body, called with `_MOVE_LOCK` held. Split out only so the lock
    scope is unmistakable and every early return releases it."""
    step = session.get(WorkflowStep, step_id)
    if step is None or step.workflow_id != workflow_id:
        raise WorkflowError(f"workflow step {step_id} not found in workflow {workflow_id}.")

    # Guard 1 -- run safety (delete_step's rule, same shape).
    active = len(
        session.exec(
            select(WorkflowRun.id).where(
                WorkflowRun.workflow_id == workflow_id, WorkflowRun.status.in_(_ACTIVE_RUN_STATUSES)
            )
        ).all()
    )
    if active > 0:
        raise WorkflowError(
            "cannot move a step of workflow %d while it has %d in-flight run(s) "  # noqa: UP031
            "(pending/running/paused) -- moving a step re-seats every later step, so "
            "a paused run would resume at the wrong one. Let the run(s) finish or "
            "cancel them, then move." % (workflow_id, active)
        )

    all_steps = orchestrator.get_steps(session, workflow_id)
    n = len(all_steps)
    try:
        pos = int(str(new_position).strip())
    except (TypeError, ValueError) as e:
        raise WorkflowError(f"position {new_position!r} must be a whole number.") from e
    if pos < 0 or pos > n - 1:
        raise WorkflowError(
            "position %d is out of range for workflow %d -- it has %d step(s), so a "  # noqa: UP031
            "position must be 0..%d (0 = first)." % (pos, workflow_id, n, n - 1)
        )

    # The composed slug. Normalised + dead-field-checked BEFORE anything else, so a
    # non-lqa step dragged onto a lane band is refused with the SAME explicit Phase-1
    # message the step form gives it, not a downstream membership error.
    pg_supplied = parallel_group is not _UNSET
    if pg_supplied:
        cand_pg = validate_parallel_group(parallel_group)
        refuse_dead_fields(step.step_type, {"parallel_group": cand_pg}, current=step)
    else:
        cand_pg = step.parallel_group

    rest = [s for s in all_steps if s.id != step_id]
    moved = rest[:pos] + [step] + rest[pos:]  # noqa: RUF005

    # NO-OP SHORT-CIRCUIT (see the docstring): same order, same slug -> touch nothing.
    if [s.id for s in moved] == [s.id for s in all_steps] and cand_pg == step.parallel_group:
        if report is not None:
            report["noop"] = True
            report["changed"] = []
        return step

    # Guard 2 -- whole-graph BEFORE/AFTER diff over the COMPOSED final state (the new
    # order AND the new slug together). Judging either half alone is what made drag-out
    # impossible.
    def _row(s, i):
        row = _graph_row(s, i)
        if _step_attr(s, "id") == step_id:
            row["parallel_group"] = cand_pg
        return row

    new_errors = _new_graph_errors(
        _graph_edit_errors(all_steps), _graph_edit_errors([_row(s, i) for i, s in enumerate(moved)])
    )
    if new_errors:
        detail = "; ".join(f"step {lbl!r}: {msg}" for lbl, msg in new_errors)
        raise WorkflowError(
            f"moving this step would break the workflow graph: {detail}. Adjust the "
            "dependent step(s) first, or choose a different position."
        )

    # All guards passed. Snapshot who headed the list BEFORE, AND the raw order_index they
    # held -- the loop below overwrites it and the post-commit refresh makes the object
    # read 0, so this int is the only surviving evidence of whether TM-first was armed
    # beforehand (see _tm_first_note: a gap-heal can arm it on an untouched step).
    before_first = all_steps[0] if all_steps else None
    before_first_index = before_first.order_index if before_first is not None else None
    slug_changed = pg_supplied and cand_pg != step.parallel_group

    # Contiguous 0..n-1 renumbering + the slug, one commit.
    for i, s in enumerate(moved):
        if s.order_index != i:
            s.order_index = i
            session.add(s)
    if slug_changed:
        step.parallel_group = cand_pg
        session.add(step)

    # 4b shared-base: a slug change is a FIELD edit, so it behaves exactly as it does on
    # the step form -- auto-pin a directly-edited variant, sweep non-pinned variants --
    # and reports what it did. A pure reorder changes no _EDIT_FIELDS, so neither applies
    # (structural edits are explicitly out of propagation's scope).
    prop = {
        "propagated": [],
        "skipped_adapted": [],
        "pinned": [],
        "validation_skipped": [],
        "dead_fields_cleared": [],
        "run_active_skipped": [],
        "propagated_as_singleton": [],
    }
    auto_pinned = False
    if slug_changed:
        if step.adapted_from_id is not None and not step.overridden:
            step.overridden = True
            auto_pinned = True
        if propagate:
            prop = propagate_step_edit(session, step, ["parallel_group"], step.prompt_text)

    session.commit()
    session.refresh(step)

    if report is not None:
        report.update(prop)
        report["noop"] = False
        report["auto_pinned"] = auto_pinned
        report["changed"] = ["parallel_group"] if slug_changed else []
        note = _tm_first_note(before_first, before_first_index, moved[0] if moved else None)
        if note:
            report["tm_first_note"] = note
    return step


def propagate_step_edit(
    session: Session, base_step: WorkflowStep, changed_fields, pre_prompt_text: str
) -> dict:
    """Sweep a base step's field EDIT onto every non-pinned variant step cloned from it
    (adapted_from_id == base_step.id). Returns a report dict:
      {"propagated": [..], "skipped_adapted": [..], "pinned": [..],
       "validation_skipped": [..], "dead_fields_cleared": [..]}
      (each a list of _variant_report_entry dicts; dead_fields_cleared entries
      additionally carry a "fields" list).

    RULES (each is a spec requirement, implemented here):
      * DELTA, not overwrite-all — only the fields that ACTUALLY CHANGED in this edit
        are copied to variants (a base edit to retry_max must NOT clobber a variant's
        adapted prompt_text, because prompt_text is not in the delta).
      * PROMPT special case — if prompt_text is in the delta AND the variant's CURRENT
        prompt_text differs from the base's PRE-EDIT prompt_text (it was LLM-adapted or
        hand-tuned), do NOT propagate the prompt; flag the variant needs_review instead
        and list it under skipped_adapted. If identical pre-edit, propagate it. Every
        OTHER changed field still propagates to that variant.
      * PINNED (overridden=True) variants are NEVER touched; listed under pinned.
      * Per-variant GRAPH re-validation (fail-closed PER VARIANT): applying the delta to
        a variant re-runs the SAME whole-graph BEFORE/AFTER diff update_step uses, on the
        variant's OWN workflow. If it would introduce NEW breakage, the variant is SKIPPED
        ENTIRELY (never half-written) and listed under validation_skipped. The base edit
        itself is unaffected (it already validated on its own graph); one bad variant
        never rolls back the base or the other variants.
      * DEAD-FIELD SWEEP (SPEC-lqa-step-honest-options, adversarial-review finding):
        propagation is a RAW setattr sweep with no refusal path of its own -- the
        variant owner never touched this step, so a hard refusal here would have
        nothing for them to act on either. Instead, AFTER the delta lands, every
        field dead for the variant's RESULTING (post-delta) step_type that still
        holds a meaningful value -- whether it just arrived via the delta (the
        base's own value, itself possibly not yet force-cleared if the base edit
        didn't touch step_type) or was already sitting on the variant untouched by
        this delta at all (e.g. a variant's own long-standing glossary_id, now
        newly dead because the propagated step_type turned it into an lqa step) —
        is force-CLEARED to its safe default and listed under dead_fields_cleared
        (never a hard failure; SKIP-AND-REPORT, so one variant's dead config can
        never block the sweep to every other variant).

    TRANSACTION: this function only mutates in-memory ORM objects (setattr + session.add)
    and NEVER commits — the CALLER (update_step) owns the single commit, so the base edit
    and every successful variant propagation land atomically. A skipped/pinned variant is
    never added, so it cannot half-write. This function never raises (graph validation is
    caught inside _graph_edit_errors)."""
    report = {
        "propagated": [],
        "skipped_adapted": [],
        "pinned": [],
        "validation_skipped": [],
        "dead_fields_cleared": [],
        "run_active_skipped": [],
        "propagated_as_singleton": [],
    }
    changed = [f for f in changed_fields if f in _EDIT_FIELDS]
    if not changed:
        return report
    # Does this delta change the SHAPE of a run on the variant it lands on?
    # update_step's Guard 1 only ever asked about the BASE workflow's runs, so a
    # graph-shaping value could be written into a VARIANT that had a run of its own in
    # flight (adversarial review, PROPAGATION-10a) -- and a paused variant run resumes
    # into a step config that is no longer the one it paused under. Scoped to
    # _GRAPH_SHAPING_FIELDS, NOT the wider _GRAPH_FIELDS (see there): a prompt/model
    # sweep -- or a plain rename -- onto a running variant is the pre-existing, accepted
    # behaviour and is left exactly as it was, so this never widens into a general
    # propagation freeze.
    graph_delta = [f for f in changed if f in _GRAPH_SHAPING_FIELDS]
    deps = session.exec(
        select(WorkflowStep).where(WorkflowStep.adapted_from_id == base_step.id)
    ).all()
    for v in deps:
        vwf = session.get(Workflow, v.workflow_id)
        if vwf is None:  # orphan guard (never expected)
            continue
        entry = _variant_report_entry(vwf, v)
        # Key 2 of the two-key safety: a pinned variant is frozen.
        if v.overridden:
            report["pinned"].append(entry)
            continue
        if graph_delta:
            v_active = len(
                session.exec(
                    select(WorkflowRun.id).where(
                        WorkflowRun.workflow_id == v.workflow_id,
                        WorkflowRun.status.in_(_ACTIVE_RUN_STATUSES),
                    )
                ).all()
            )
            if v_active > 0:
                report["run_active_skipped"].append(
                    dict(
                        entry,
                        reason="has %d in-flight run(s); graph field(s) %s not "  # noqa: UP031
                        "applied" % (v_active, ", ".join(sorted(graph_delta))),
                    )
                )
                continue
        fields_to_apply = list(changed)
        prompt_skipped = False
        if "prompt_text" in fields_to_apply:  # noqa: SIM102
            # PROMPT special case: protect a diverged (adapted / hand-tuned) prompt.
            if (v.prompt_text or "") != (pre_prompt_text or ""):
                fields_to_apply.remove("prompt_text")
                prompt_skipped = True
        # Per-variant fail-closed graph re-validation for the fields that touch the
        # graph (key/type/gate/guard/branch). A candidate row carries the variant's
        # CURRENT graph values overridden by the proposed (base's new) values.
        graph_touch = [f for f in fields_to_apply if f in _GRAPH_FIELDS]
        if graph_touch:
            vsteps = orchestrator.get_steps(session, v.workflow_id)
            cand = {f: getattr(v, f) for f in _GRAPH_FIELDS}
            for f in graph_touch:
                cand[f] = getattr(base_step, f)
            new_errors = _new_graph_errors(
                _graph_edit_errors(vsteps),
                _graph_edit_errors([cand if s.id == v.id else s for s in vsteps]),
            )
            if new_errors:
                entry = dict(entry, reason="; ".join(f"{lbl}: {msg}" for lbl, msg in new_errors))
                report["validation_skipped"].append(entry)
                continue
        # All checks passed — apply the delta (copy the base's NEW values). A non-pinned,
        # non-diverged variant thus stays byte-identical to the base for every swept field.
        for f in fields_to_apply:
            setattr(v, f, getattr(base_step, f))
        if prompt_skipped:
            v.needs_review = True
            report["skipped_adapted"].append(entry)
        if fields_to_apply:
            report["propagated"].append(entry)
        # DEAD-FIELD SWEEP: force-clear (never refuse) anything now dead for this
        # variant's RESULTING step_type -- catches a delta field that just imported
        # a meaningful dead value from the base AND any pre-existing dead value the
        # variant already carried, untouched by this delta.
        cleared = _force_clear_dead_fields(v, v.step_type)
        if cleared:
            report["dead_fields_cleared"].append(dict(entry, fields=cleared))
        # HALF-APPLIED PARALLELISM IS REPORTED, NOT SILENT (adversarial review,
        # PROPAGATION-10b). Propagation is per-STEP: it sweeps the base step onto ITS
        # variant, and knows nothing about the variant's SIBLING steps. So a base group
        # of two lanes can land as a LONE member on a variant whose other member was
        # pinned, diverged, or simply never cloned — and a lone member is INERT (it runs
        # on its own, in order). The base then runs its checks in parallel while that
        # variant runs them sequentially, with identical config and no error anywhere.
        # We do not "fix" it by force-clearing (the slug is what the operator asked for,
        # and the sibling may be about to be edited too); we make it VISIBLE in the same
        # banner that reports everything else this sweep did.
        if getattr(v, "parallel_group", None):
            _vsteps = _ordered(orchestrator.get_steps(session, v.workflow_id))
            _vi = next((i for i, x in enumerate(_vsteps) if x.id == v.id), None)
            if _vi is not None:
                _f, _l = _group_span(_vsteps, _vi)
                if _f == _l:
                    report["propagated_as_singleton"].append(
                        dict(
                            entry,
                            group=v.parallel_group,
                            reason="no adjacent step in this variant shares the group "
                            f"{v.parallel_group!r}, so these checks run SEQUENTIALLY here while the "  # noqa: E501
                            "base runs them in parallel",
                        )
                    )
        session.add(v)
    return report


def update_step(
    session: Session,
    step_id: int,
    *,
    set_override=_UNSET,
    propagate: bool = True,
    report: dict | None = None,
    **fields,
) -> WorkflowStep:
    """Edit an existing workflow step IN PLACE (partial update; order_index preserved).

    Only fields PRESENT in `fields` change; an absent field is left untouched (callers
    pass the `_UNSET` sentinel — or simply omit the kwarg — for "leave unchanged", so an
    explicit None/False/'' is a real edit). Accepts add_step's full field set (see
    `_EDIT_FIELDS`). Returns the refreshed step; raises WorkflowError (fail-closed,
    changing NOTHING) on any invalid edit.

    THREE fail-closed guards, checked before any write:
      1. RUN-SAFETY: refused while the workflow has an ACTIVE (pending/running/paused)
         run — editing a step mid-flight could corrupt that run's cursor/graph on resume.
         The predicate is deliberately NARROWER than delete_workflow's active_runs blocker:
         it keys ONLY on WorkflowRun.workflow_id == this step's workflow (runs that execute
         THESE steps). A batch member owned by a sibling VARIANT runs the variant's OWN
         steps, so it is correctly NOT a bar to editing this workflow's steps (delete, which
         reaps the batch, must count it; an edit must not). This blocks ALL edits, not just
         graph-affecting ones: distinguishing "safe" fields is brittle and a paused run
         resumes into this exact step config, so we fail safe (let the run finish or cancel
         it, then edit).
      2. THIS STEP's own validity: the new run_condition resolves against the steps that
         PRECEDE it at its ACTUAL order_index (not add_step's append case); the new key is
         a unique slug EXCLUDING itself; the new branch honours D3 (a gate-producing step)
         and its targets resolve.
      3. WHOLE-GRAPH re-validation (§4.3): every OTHER step's guard/branch that references
         this one must STILL resolve after the edit. Renaming/clearing this step's key, or
         changing its type / turning off its qe_gate so it no longer produces the outcome a
         downstream guard/branch depends on, FAILS CLOSED naming the dependent step — never
         silently orphaning a reference. A BEFORE/AFTER error diff means a pre-existing
         (engine-tolerated) inconsistency does not block an unrelated edit.

    LIFECYCLE: a Workflow has NO approval/draft column (unlike Glossary), so there is no
    state for an edit to revert — the glossary touch_draft precedent does not apply here.

    SHARED-BASE (SPEC-workflows-redesign 4b):
      * PIN / auto-pin (`set_override`): an explicit True/False PINS/UNPINS this step
        (overridden). When left `_UNSET`, a DIRECT edit of a VARIANT step (one with a
        live adapted_from_id) AUTO-PINS it — "you touched it, you own it" — so a later
        base edit skips it. Editing a BASE/legacy step (adapted_from_id NULL) never
        auto-pins. An explicit `set_override` always wins over the auto-pin heuristic.
      * PROPAGATION (`propagate`, default True): after this step's own edit lands, the
        DELTA (only the fields that actually changed) sweeps onto every non-pinned
        variant cloned from this step (see propagate_step_edit). If `report` is a dict,
        it is filled with {propagated, skipped_adapted, pinned, validation_skipped,
        auto_pinned, changed} so the caller (web notice / CLI / api) can surface it. All
        of it — this step's edit AND every successful variant write — commits atomically
        in the SINGLE commit below."""
    step = session.get(WorkflowStep, step_id)
    if step is None:
        raise WorkflowError(f"workflow step {step_id} not found.")

    # Guard 1 — run-safety (fail-closed). Intentionally NARROWER than delete_workflow's
    # active_runs blocker: only runs of THIS workflow execute these steps, so a sibling
    # variant's batch member (which runs the variant's own steps) is not a bar here.
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
            "cannot edit a step of workflow %d while it has %d in-flight run(s) "  # noqa: UP031
            "(pending/running/paused) — editing mid-flight could corrupt a run on "
            "resume. Let the run(s) finish or cancel them, then edit." % (step.workflow_id, active)
        )

    # Reject unknown kwargs up front (a typo must never silently no-op).
    supplied = {k: v for k, v in fields.items() if v is not _UNSET}
    for key, label, low, high, default in (
        ("qe_threshold", "QE threshold", 0, 100, 70),
        ("qe_autofix_max_rounds", "QE auto-fix max rounds", 1, 5, 3),
    ):
        if key in supplied:
            supplied[key] = _require_whole_number(supplied[key], label, low, high, default)
    unknown = set(supplied) - set(_EDIT_FIELDS)
    if unknown:
        raise WorkflowError(f"unknown step field(s): {sorted(unknown)}.")
    # BUG-A202: validate an inbound lqa_threshold edit BEFORE it is read anywhere below,
    # rejecting out-of-band/junk instead of silently clamping (parity with add_step). The
    # normalised int-or-None is stored back so _cur()/_clamp are idempotent; a blank/NULL
    # edit still clears back to the inherited default (byte-identical).
    if "lqa_threshold" in supplied:
        supplied["lqa_threshold"] = _require_valid_lqa_threshold(supplied["lqa_threshold"])
    # Batch 15 T1 (N2/N4): validate the tm_match int + pool overrides BEFORE they are read
    # anywhere below (parity with add_step + lqa_threshold), so a junk edit is refused
    # cleanly instead of silently clearing the override. The normalised value is stored back
    # so the mutation block / dead-field check read the validated form.
    for _f in ("tm_fill_threshold", "tm_context_floor"):
        if _f in supplied:
            supplied[_f] = _require_valid_tm_scalar(supplied[_f], _DEAD_FIELD_LABELS.get(_f, _f))
    if "tm_pool_selection" in supplied:
        supplied["tm_pool_selection"] = _require_valid_tm_pool_selection(
            supplied["tm_pool_selection"]
        )

    def _cur(name):
        return supplied[name] if name in supplied else getattr(step, name)

    all_steps = orchestrator.get_steps(session, step.workflow_id)
    others = [s for s in all_steps if s.id != step_id]
    # Steps that PRECEDE this one at its ACTUAL order_index (not "all existing").
    prior = [s for s in others if s.order_index < step.order_index]

    # Candidate conditional fields (validated against the graph = this step's own checks).
    cand_step_type = _cur("step_type")
    # BUG-A201: reject an unknown step_type on edit too (the /api + web edit paths funnel
    # here; the CLI argparse `choices=` never protected them).
    if "step_type" in supplied and cand_step_type not in StepType.ALL:
        raise WorkflowError(
            "unknown step type {!r} (choose from {}).".format(
                cand_step_type, ", ".join(StepType.ALL)
            )
        )
    cand_qe_gate = bool(_cur("qe_gate")) if "qe_gate" in supplied else step.qe_gate
    cand_key = validate_key(_cur("key"), others) if "key" in supplied else step.key
    # SPEC-parallel-routing §5.6/§5.1.4: the candidate group slug decides what "prior"
    # means for THIS step's own guard (a member's guard resolves against pre-group
    # steps), so it is normalised before the run_condition is validated.
    cand_pg = (
        validate_parallel_group(_cur("parallel_group"))
        if "parallel_group" in supplied
        else step.parallel_group
    )
    # A TYPE SWITCH force-clears every field dead for the NEW type (see Guard 4 and the
    # `_type_switching` block below) -- and parallel_group is the ONE such field that is
    # also a GRAPH field. The whole-graph diff below must therefore judge the state the
    # edit will actually PRODUCE, not an intermediate that never exists: without this,
    # switching a group member from lqa to llm_agent would be REFUSED by Guard 3 for
    # carrying a group it is about to have cleared anyway -- an edit the builder gives
    # the user no way to fix, which is exactly the refusal the type-switch regime exists
    # to avoid. parallel_group is previewed here, and qe_gate right after it (#512).
    _pg_preview_cleared = False
    _qe_preview_cleared = False
    if (
        "step_type" in supplied
        and _cur("step_type") != step.step_type
        and "parallel_group" in DEAD_FIELDS_BY_TYPE.get(_cur("step_type"), ())
    ):
        _pg_preview_cleared = bool(cand_pg)
        cand_pg = None
    # #512(1): qe_gate is the OTHER dead-and-graph field. A switch to a type that has no
    # QE gate (human_review, external, tm_match) clears it in the force-clear sweep below,
    # so the graph diff must judge the gate-less step: a branch on it, or a later
    # prev_qe / step:<key>:failed guard that reads its gate, could never be satisfied and
    # must be REFUSED, not saved. (An lqa step produces a gate whatever qe_gate says, so
    # the preview changes no verdict there; the type-switch test pins both directions.)
    if (
        "step_type" in supplied
        and _cur("step_type") != step.step_type
        and "qe_gate" in DEAD_FIELDS_BY_TYPE.get(_cur("step_type"), ())
    ):
        _qe_preview_cleared = cand_qe_gate
        cand_qe_gate = False
    cand_rc = (
        validate_run_condition(_cur("run_condition"), _pre_group_prior(prior, cand_pg))
        if "run_condition" in supplied
        else step.run_condition
    )
    cand_branch = (
        validate_branch(
            _cur("branch"),
            cand_step_type,
            cand_qe_gate,
            others,
            self_key=cand_key,
            parallel_group=cand_pg,
        )
        if "branch" in supplied
        else step.branch
    )
    # max_visits parsing (mirrors add_step).
    if "max_visits" in supplied:
        raw_mv = _cur("max_visits")
        cand_mv = None
        if raw_mv is not None and str(raw_mv).strip() != "":
            try:
                cand_mv = int(raw_mv)
            except (TypeError, ValueError, OverflowError) as e:
                raise WorkflowError(f"max visits {raw_mv!r} must be a whole number.") from e
            if cand_mv < 1:
                raise WorkflowError(
                    "max visits must be at least 1 (or blank to inherit the default)."
                )
            if cand_mv > MAX_STEP_INT_OVERRIDE:  # GitHub #502: never reach the int column unbounded
                raise WorkflowError(f"max visits must be at most {MAX_STEP_INT_OVERRIDE}.")
    else:
        cand_mv = step.max_visits

    # Guard 3 — whole-graph BEFORE/AFTER diff: only NEW breakage the edit introduces
    # blocks it. The candidate is a dict spliced in at this step's position.
    candidate = {
        "id": step_id,
        "order_index": step.order_index,
        "name": _cur("name"),
        "step_type": cand_step_type,
        "qe_gate": cand_qe_gate,
        "key": cand_key,
        "run_condition": cand_rc,
        "branch": cand_branch,
        "parallel_group": cand_pg,
    }
    new_errors = _new_graph_errors(
        _graph_edit_errors(all_steps),
        _graph_edit_errors([candidate if s.id == step_id else s for s in all_steps]),
    )
    if new_errors:
        detail = "; ".join(f"step {lbl!r}: {msg}" for lbl, msg in new_errors)
        raise WorkflowError(
            f"this edit would break the workflow graph: {detail}. Adjust the dependent "
            "step(s) first, or keep this step's key/type/gate."
        )

    # Guard 4 — dead-field handling (SPEC-lqa-step-honest-options). Two DISTINCT
    # regimes, chosen by whether THIS call actually CHANGES step_type (not merely
    # whether step_type rides along in the payload — the web edit form ALWAYS
    # full-POSTs step_type, changed or not, so "present in `supplied`" alone can't
    # tell a real switch apart from an unrelated edit that just resubmits the SAME
    # type; only a genuine value change is a switch):
    #
    #   * step_type UNCHANGED (absent from `supplied`, OR present but equal to the
    #     step's current value): the dead-for-this-type fields were ALREADY hidden
    #     in the builder before this edit, so any NEW/changed meaningful value for
    #     one is a deliberate act (CLI/API/hand-crafted request, since a real
    #     browser never renders a way to set it) — REFUSE it, same as before. An
    #     untouched legacy value (not in `supplied`) is left alone.
    #   * step_type ACTUALLY CHANGES this call (a TYPE SWITCH): a real browser's
    #     edit form always full-POSTs, so a field the JS just started hiding
    #     (because the type changed) still carries its OLD rendered value with NO
    #     way for the user to un-set it — REFUSING here would reject an edit the
    #     user cannot fix (an adversarial-review finding: llm_agent-with-qe_gate ->
    #     lqa via the ordinary Type dropdown must not silently freeze qe_gate=True
    #     either). So a type switch NEVER refuses on a dead field; instead the
    #     mutation block below applies normally, and immediately after it a FORCE-
    #     CLEAR sweep (below, see `_type_switching`) resets every field dead for
    #     the NEW type back to its safe default — whether it was just written this
    #     call or merely carried over untouched from before — and reports what it
    #     cleared.
    _type_switching = "step_type" in supplied and cand_step_type != step.step_type
    if not _type_switching:
        _dead_candidates: dict = {}
        for _f in DEAD_FIELDS_BY_TYPE.get(cand_step_type, ()):
            if _f not in supplied:
                continue
            if _f == "prompt_text":
                _dead_candidates[_f] = _cur("prompt_text") or ""
            elif _f == "prompt_template_id":
                _dead_candidates[_f] = _coerce_pt_id(_cur("prompt_template_id"))
            elif _f in ("model", "fallback_model"):
                _dead_candidates[_f] = _cur(_f) or None
            elif _f in ("provider", "fallback_provider", "qe_provider"):
                _dead_candidates[_f] = _resolve_provider_edit(getattr(step, _f), _cur(_f))
            elif _f == "chunk_policy":
                _dead_candidates[_f] = _cur("chunk_policy")
            elif _f == "redact_pii":
                _dead_candidates[_f] = bool(_cur("redact_pii"))
            elif _f == "glossary_id":
                _gid = _cur("glossary_id")
                _dead_candidates[_f] = ids.coerce_id(_gid)
            elif _f in ("adaptive_feedback", "doc_context_prepass", "qe_gate", "tm_examples"):
                _dead_candidates[_f] = bool(_cur(_f))
            elif _f == "qe_threshold":
                _dead_candidates[_f] = _clamp_threshold(_cur("qe_threshold"))
            elif _f in ("qe_autofix", "qe_autofix_cross_check"):
                _dead_candidates[_f] = bool(_cur(_f))
            elif _f == "qe_autofix_provider":
                _dead_candidates[_f] = _autofix_provider_edit(_cur(_f))
            elif _f == "qe_autofix_max_rounds":
                _dead_candidates[_f] = _clamp_autofix_rounds(_cur(_f))
            elif _f == "lqa_threshold":
                _dead_candidates[_f] = _clamp_lqa_threshold(_cur("lqa_threshold"))
            elif _f in ("tm_protect", "over_translation_guard"):
                # run-190 M5: over_translation_guard shares tm_protect's tristate coercion.
                _dead_candidates[_f] = _coerce_tm_protect(_cur(_f))
            elif _f == "memory_mode":
                _dead_candidates[_f] = _coerce_memory_mode(_cur("memory_mode"))
            elif _f == "parallel_group":
                # Already slugified above; the refusal compares against the STORED
                # value, so a step that legitimately kept a legacy slug is untouched.
                _dead_candidates[_f] = cand_pg
            elif _f == "vendor_id":
                _dead_candidates[_f] = _coerce_vendor_id(_cur("vendor_id"))
            elif _f == "external_wait_hours":
                _dead_candidates[_f] = _coerce_wait_hours(_cur("external_wait_hours"))
            elif _f in ("tm_fill_threshold", "tm_context_floor"):
                _dead_candidates[_f] = _coerce_tm_scalar_int(_cur(_f))
            elif _f == "tm_fuzzy_lock_mode":
                _dead_candidates[_f] = _coerce_tm_fuzzy_lock_mode(_cur(_f))
            elif _f in ("tm_lock_ice", "tm_hundreds_enter_lqa"):
                _dead_candidates[_f] = _coerce_tm_tristate_bool(_cur(_f))
            elif _f == "tm_pool_selection":
                _dead_candidates[_f] = _coerce_tm_pool_selection(_cur(_f))
        refuse_dead_fields(cand_step_type, _dead_candidates, current=step)

    # Guard 5 — prompt-template BIND validation (SPEC-prompt-template-runtime): the
    # EFFECTIVE post-edit binding (the supplied id, else the step's current one) must
    # resolve + pass the source-placeholder check for the effective type. Fail-closed,
    # changing nothing. On a TYPE SWITCH to lqa/human_review the binding is force-cleared
    # below, so only validate when the effective type still USES it (llm_agent); a switch
    # never validates a now-dead binding it is about to reset.
    _cand_pt_supplied = "prompt_template_id" in supplied
    _cand_pt = (
        _coerce_pt_id(_cur("prompt_template_id")) if _cand_pt_supplied else step.prompt_template_id
    )
    if cand_step_type == StepType.LLM_AGENT and (_cand_pt_supplied or "step_type" in supplied):
        validate_bound_template(session, cand_step_type, _cand_pt)

    # Guard 6 — external-step vendor validation (PROPOSAL-external-processing-step Wave A).
    # Validate the EFFECTIVE post-edit vendor BEFORE any mutation (fail-closed, changing
    # nothing) whenever the step is (or is becoming) external and the vendor or the type
    # was touched. A switch AWAY from external force-clears vendor_id below and never
    # reaches here.
    _cand_vendor_id = (
        _coerce_vendor_id(_cur("vendor_id")) if "vendor_id" in supplied else step.vendor_id
    )
    if cand_step_type == StepType.EXTERNAL and ("vendor_id" in supplied or "step_type" in supplied):
        _validate_step_vendor(session, step.workflow_id, _cand_vendor_id)

    # Guard 7 -- engine ROLE (Jev judges): a changed provider/fallback must be a translation
    # engine and a changed QE judge an LLM or a Jev judge. Only a value this call actually
    # CHANGES is checked, so an unrelated edit never trips over a stored legacy ref.
    _role_refs = {}
    for _f in ("provider", "fallback_provider", "qe_provider", "qe_autofix_provider"):
        if _f in supplied:
            _new = (
                _autofix_provider_edit(_cur(_f))
                if _f == "qe_autofix_provider"
                else _resolve_provider_edit(getattr(step, _f), _cur(_f))
            )
            if (_new or None) != (getattr(step, _f) or None):
                _role_refs[_f] = _new
    if _role_refs:
        _assert_step_provider_roles(session, step.workflow_id, **_role_refs)
    # QE auto-fix (review B3): the post-edit state must not leave auto-fix inheriting a
    # non-LLM step engine. Checked only when this call touches one of the three inputs.
    # A step that is (or is becoming) a non-agent type has these fields dead and
    # force-cleared, so there is nothing to check there.
    if cand_step_type == StepType.LLM_AGENT and {
        "qe_autofix",
        "qe_autofix_provider",
        "provider",
    } & set(supplied):
        _assert_autofix_fixer(
            bool(_cur("qe_autofix")) if "qe_autofix" in supplied else step.qe_autofix,
            _autofix_provider_edit(_cur("qe_autofix_provider"))
            if "qe_autofix_provider" in supplied
            else step.qe_autofix_provider,
            _resolve_provider_edit(step.provider, _cur("provider"))
            if "provider" in supplied
            else step.provider,
        )

    # Snapshot the PRE-EDIT values so the 4b propagation can (a) compute the DELTA (which
    # fields actually changed) and (b) compare a variant's prompt against the base's
    # PRE-edit prompt for the prompt special case. Taken before the first mutation below.
    pre_values = {f: getattr(step, f) for f in _EDIT_FIELDS}

    # All guards passed — apply the (normalised) candidate values. Nothing above mutated
    # the row, so a raise anywhere left it untouched (fail-closed).
    if "name" in supplied:
        step.name = (_cur("name") or "").strip()
    if "step_type" in supplied:
        step.step_type = cand_step_type
    if "provider" in supplied:
        step.provider = _resolve_provider_edit(step.provider, _cur("provider"))
    if "model" in supplied:
        step.model = _cur("model") or None
    if "fallback_provider" in supplied:
        step.fallback_provider = _resolve_provider_edit(
            step.fallback_provider, _cur("fallback_provider")
        )
    if "fallback_model" in supplied:
        step.fallback_model = _cur("fallback_model") or None
    if "prompt_text" in supplied:
        step.prompt_text = _cur("prompt_text") or ""
    if "prompt_template_id" in supplied:
        step.prompt_template_id = _coerce_pt_id(_cur("prompt_template_id"))
    if "chunk_policy" in supplied:
        step.chunk_policy = _cur("chunk_policy")
    if "redact_pii" in supplied:
        step.redact_pii = bool(_cur("redact_pii"))
    if "glossary_id" in supplied:
        gid = _cur("glossary_id")
        step.glossary_id = ids.coerce_id(gid)
    if "adaptive_feedback" in supplied:
        step.adaptive_feedback = bool(_cur("adaptive_feedback"))
    if "adaptive_feedback_window" in supplied:
        step.adaptive_feedback_window = _clamp_window(_cur("adaptive_feedback_window"))
    if "doc_context_prepass" in supplied:
        step.doc_context_prepass = bool(_cur("doc_context_prepass"))
    if "qe_gate" in supplied:
        step.qe_gate = cand_qe_gate
    if "qe_threshold" in supplied:
        step.qe_threshold = _clamp_threshold(_cur("qe_threshold"))
    if "qe_provider" in supplied:
        step.qe_provider = _resolve_provider_edit(step.qe_provider, _cur("qe_provider"))
    # QE auto-fix (Train B): an explicit blank CLEARS the post-editor back to "same as
    # this step" (review B2), unlike the provider rule above; rounds were already validated
    # 1-5 above.
    if "qe_autofix" in supplied:
        step.qe_autofix = bool(_cur("qe_autofix"))
    if "qe_autofix_provider" in supplied:
        step.qe_autofix_provider = _autofix_provider_edit(_cur("qe_autofix_provider"))
    if "qe_autofix_max_rounds" in supplied:
        step.qe_autofix_max_rounds = _clamp_autofix_rounds(_cur("qe_autofix_max_rounds"))
    if "qe_autofix_cross_check" in supplied:
        step.qe_autofix_cross_check = bool(_cur("qe_autofix_cross_check"))
    if "lqa_threshold" in supplied:
        # feat/lqa-configurable-threshold: clamp 0-100, None (blank/junk) clears back to
        # the built-in default (90). A set value on a non-lqa step was already refused
        # above (dead-for-type), and a type switch off lqa force-clears it afterward.
        step.lqa_threshold = _clamp_lqa_threshold(_cur("lqa_threshold"))
    if "tm_examples" in supplied:
        step.tm_examples = bool(_cur("tm_examples"))
    if "tm_examples_window" in supplied:
        step.tm_examples_window = context_pkg.clamp_examples_window(_cur("tm_examples_window"))
    if "use_image_context" in supplied:
        step.use_image_context = bool(_cur("use_image_context"))  # Batch 14 T2 (SPEC §25)
    if "concept_glossary_render" in supplied:
        step.concept_glossary_render = bool(_cur("concept_glossary_render"))  # T3 (SPEC §9-T3)
    if "glossary_concept_cap" in supplied:
        step.glossary_concept_cap = _coerce_glossary_cap(_cur("glossary_concept_cap"), 10)
    if "glossary_token_cap" in supplied:
        step.glossary_token_cap = _coerce_glossary_cap(_cur("glossary_token_cap"), 0)
    if "tm_protect" in supplied:
        step.tm_protect = _coerce_tm_protect(_cur("tm_protect"))
    if "over_translation_guard" in supplied:  # run-190 M5: same tristate as tm_protect
        step.over_translation_guard = _coerce_tm_protect(_cur("over_translation_guard"))
    if "memory_mode" in supplied:
        step.memory_mode = _coerce_memory_mode(_cur("memory_mode"))
    # Batch 15 T1 (SPEC-tm-match-step-config §8.A): each supplied tm_match config field is
    # coerced to its nullable form (an explicit None/blank clears the override back to
    # inherit); a set on a non-tm_match step was refused above, a type switch off tm_match
    # force-clears afterward.
    if "tm_fill_threshold" in supplied:
        step.tm_fill_threshold = _coerce_tm_scalar_int(_cur("tm_fill_threshold"))
    if "tm_fuzzy_lock_mode" in supplied:
        step.tm_fuzzy_lock_mode = _coerce_tm_fuzzy_lock_mode(_cur("tm_fuzzy_lock_mode"))
    if "tm_lock_ice" in supplied:
        step.tm_lock_ice = _coerce_tm_tristate_bool(_cur("tm_lock_ice"))
    if "tm_hundreds_enter_lqa" in supplied:
        step.tm_hundreds_enter_lqa = _coerce_tm_tristate_bool(_cur("tm_hundreds_enter_lqa"))
    if "tm_context_floor" in supplied:
        step.tm_context_floor = _coerce_tm_scalar_int(_cur("tm_context_floor"))
    if "tm_pool_selection" in supplied:
        step.tm_pool_selection = _coerce_tm_pool_selection(_cur("tm_pool_selection"))
    if "run_condition" in supplied:
        step.run_condition = cand_rc
    if "key" in supplied:
        step.key = cand_key
    if "branch" in supplied:
        step.branch = cand_branch
    if "max_visits" in supplied:
        step.max_visits = cand_mv
    if "parallel_group" in supplied:
        step.parallel_group = cand_pg
    # External/vendor dispatch (Wave A): written only when supplied. Validated below
    # (after the mutation) against the EFFECTIVE type + workflow customer; a type switch
    # away from external force-clears them via _force_clear_dead_fields.
    if "vendor_id" in supplied:
        step.vendor_id = _coerce_vendor_id(_cur("vendor_id"))
    if "external_wait_hours" in supplied:
        step.external_wait_hours = _coerce_wait_hours(_cur("external_wait_hours"))
    # assignee_email is only meaningful on a human_review step (stamped there, inert
    # elsewhere — matching add_step). Recompute whenever assignee OR type is edited so a
    # type change to/from human_review clears/keeps it correctly.
    if "assignee_email" in supplied or "step_type" in supplied:
        raw_assignee = _cur("assignee_email")
        step.assignee_email = (
            (raw_assignee or "").strip()
            if step.step_type in (StepType.HUMAN_REVIEW, StepType.EXTERNAL)
            else ""
        )

    # TYPE-SWITCH FORCE-CLEAR (SPEC-lqa-step-honest-options, adversarial-review
    # finding): runs ONLY when this call touched step_type (Guard 4 above skipped
    # refusal for exactly this reason). Scans the step's TRUE resulting state --
    # every field dead for the (now-applied) NEW step_type, whether it was just
    # written by the mutation block above (e.g. a hidden qe_gate checkbox that
    # still submitted "1") or simply left untouched/carried over from before —
    # and resets any that are still meaningful back to a safe default. NEVER
    # refuses: the builder hides these fields once the type changes, so the user
    # has no way to act on a rejection. `report` (already threaded through for the
    # 4b propagation notice) gets a `type_switch_cleared` list so the caller can
    # tell the user exactly what was reset.
    type_switch_cleared: list = []
    if _type_switching:
        type_switch_cleared = _force_clear_dead_fields(step, step.step_type)
        # The parallel_group PREVIEW above already wrote None (it had to, so the graph
        # diff judged the true resulting state), which means the sweep finds nothing
        # left to clear and would report nothing. Report it here instead -- a cleared
        # group changes execution shape, and the one thing this must never be is silent.
        if _pg_preview_cleared and "parallel_group" not in type_switch_cleared:
            type_switch_cleared.append("parallel_group")
        # Same for qe_gate (#512): the preview (and a supplied qe_gate write) already
        # cleared it, so the sweep cannot see it, but it was cleared all the same.
        if _qe_preview_cleared and "qe_gate" not in type_switch_cleared:
            type_switch_cleared.append("qe_gate")
        if report is not None and type_switch_cleared:
            report["type_switch_cleared"] = list(type_switch_cleared)

    # --- shared-base 4b: pin / auto-pin + delta propagation --------------------
    # PIN decision. An explicit set_override (the deliberate pin/unpin act, from the web
    # pin control or CLI --pin/--unpin) always wins. Otherwise a DIRECT edit of a VARIANT
    # step (one carrying a live provenance link) auto-pins it — "you touched it, you own
    # it" — so a later base edit skips it; editing a BASE/legacy step never auto-pins.
    auto_pinned = False
    if set_override is not _UNSET:
        step.overridden = bool(set_override)
    elif supplied and step.adapted_from_id is not None and not step.overridden:
        step.overridden = True
        auto_pinned = True

    # DELTA: the fields whose value actually changed (not merely "was supplied"). Only
    # these sweep to variants, so a base edit to an unrelated field never clobbers a
    # variant's adapted prompt.
    changed = [f for f in _EDIT_FIELDS if getattr(step, f) != pre_values[f]]

    # PROPAGATION: sweep the delta onto this step's non-pinned variants. Runs only for a
    # BASE step (one that OTHER steps are cloned from); a leaf/variant edit finds no
    # dependents and is a no-op walk. propagate_step_edit only mutates in-memory ORM
    # objects — the single commit below makes the base edit + variant writes atomic.
    prop = {"propagated": [], "skipped_adapted": [], "pinned": [], "validation_skipped": []}
    if propagate and changed:
        prop = propagate_step_edit(session, step, changed, pre_values["prompt_text"])
    if report is not None:
        report.update(prop)
        report["auto_pinned"] = auto_pinned
        report["changed"] = list(changed)

    session.add(step)
    session.commit()
    session.refresh(step)
    return step
