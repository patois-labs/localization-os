"""setup_author: part2

Documented in: docs/architecture/project-setup-and-templates.md
"""

from app import config
from app.services.setup_author_helpers import (
    _ASSISTANT_SUBSTITUTION_NOTE,
    _ASSISTANT_UNSUPPORTED,
    _EXECUTABLE_STEP_TYPES,
    _STEP_MERGE_KEYS,
    _WF_BRANCH_RULES,
    _WF_SCHEMA,
    _WF_STEP_TYPES,
    SetupAuthorError,
    _as_bool,
    _as_str,
    _extract_json_dict,
    _match_by_name,
    _match_provider,
    _meaningful,
    _resolve_step_provider,
)


def _author_json(
    prov_ref,
    system: str,
    prompt: str,
    session=None,
    org_id=None,
    model=None,
    tap_origin: str = "setup_author",
) -> dict:
    """Shared low-level authoring call: resolve the provider, guard the spend cap,
    run the completion through run_completion (escalate-on-truncation + Prompt-Tap +
    ledger), then fence-tolerant JSON extraction. Raises SetupAuthorError with a clean
    message + status hint for every failure mode (dead/unknown ref -> 400; spend-blocked
    / provider-call failure / at-ceiling truncation -> 502) and _ParseError (502) for a
    hard JSON parse failure. `session` is accepted for signature symmetry with the
    workflow variant (Branch B) -- unused here.

    `tap_origin` labels this call in the Prompt-Tap capture (task stays the
    "setup_author" family so the spend/budget machinery is byte-identical); the draft-
    repair seam passes "draft_repair" so its calls are distinguishable in the tap
    viewer from a fresh author draft.

    `org_id` scopes the authoring provider ref (MED-2): a foreign-org engine:<id>
    is refused as not-live BEFORE resolution, never enumerable.

    `model` is the resolved authoring model (a UI/CLI pick or a server-resolved
    default) passed as the run_completion spec model; blank/None lets the provider apply its
    own default (an engine:<id>'s profile model, or a built-in's DEFAULT_MODELS).
    The callers resolve it via providers.resolve_default_model so a keyed cloud
    provider whose DEFAULT_<X>_MODEL is empty still gets a real model here instead
    of tripping the empty-model guard with no way for the operator to pick one."""
    from app.services import spend
    from app.services.providers import (
        DeadEngineError,
        ProviderSpec,
        UnknownProviderError,
        assert_provider_visible,
        get_provider,
        run_completion,
    )

    try:
        # Cross-org engine guard: a foreign-org engine:<id> must look dead, not
        # resolve. Raised as DeadEngineError -> mapped to a clean 400 just below.
        assert_provider_visible(prov_ref, org_id)
        prov = get_provider(prov_ref)
    except (DeadEngineError, UnknownProviderError) as exc:
        # A project/step provider dropdown includes engine:<id> refs; a dead or
        # unknown one is a clean 400, never a raw 500.
        raise SetupAuthorError(str(exc), status=400) from exc

    # Spend cap: over cap on a REAL engine -> refuse BEFORE calling, so a blocked
    # request makes ZERO provider calls (Mock is exempt). This pre-check mirrors
    # run_completion's own per-target spend brake below; it fires first so the user
    # gets the clean 502 without run_completion collapsing it into an error string.
    block_msg = spend.blocked_for(prov)
    if block_msg:
        raise SetupAuthorError(block_msg, status=502)

    # Route through run_completion (NOT a direct prov.complete): setup authoring
    # OUTPUT (a full project/workflow JSON with per-step prompt_text) is much LARGER
    # than its short INPUT description, so the input-sized heuristic truncated the
    # draft on a direct call (BUG 5 / LIVE 502). run_completion gives three things a
    # direct call could not: (1) escalate-on-truncation -- it GROWS the output budget
    # toward the model ceiling instead of failing at a fixed cap; (2) Prompt-Tap
    # capture of this call (model/budget/truncation flag) for the debugging tool,
    # which was blind on the old direct path; (3) an accurate spend-ledger stamp
    # (the resolved model priced, once) via record_usage. We seed a generous STARTING
    # budget so the FIRST attempt is not truncated; escalation covers anything larger.
    spec = ProviderSpec(provider=prov_ref, model=(model or None))
    start_budget = min(config.model_output_ceiling(model or None), 16384)
    try:
        res = run_completion(
            spec,
            prompt,
            system=system,
            task="setup_author",
            payload={"description": prompt},
            tap_meta={"task_origin": tap_origin},
            max_tokens=start_budget,
        )
    except DeadEngineError as exc:  # defensive: a ref that only dies mid-call
        raise SetupAuthorError(str(exc), status=400) from exc
    if res["error"] is not None:
        if res.get("truncated"):
            # Escalation already grew the budget to this model's output CEILING and the
            # draft STILL overflowed -- so it genuinely exceeds what this model can emit.
            raise SetupAuthorError(
                "The model ran out of output space before finishing the setup draft, "
                "even after automatically expanding the output budget to this model's "
                "ceiling. Pick a provider/model with a larger output token limit.",
                status=502,
            )
        # Any other provider failure (bad key / unreachable local server / malformed
        # body / NMT engine with no prompt channel) must reach the user CLEAN.
        raise SetupAuthorError(
            f"The authoring provider could not complete the request: {res['error']}", status=502
        )
    # run_completion already recorded spend once (record_usage on the successful
    # attempt); do NOT stamp again here -- that would double-count the ledger.
    return _extract_json_dict(res["text"])  # may raise _ParseError -> caller retries


def _validate_workflow_draft(session, raw: dict, catalogs: dict) -> dict:
    """Validate the model's proposal against the caller's real catalogs. Every
    reference the model PROVIDED is resolved or warned-and-cleared (skip-and-warn); a
    field it left empty stays at the form default and is NOT flagged. A step's
    prompt_text failing validate_step_prompt is a WARNING here (the review screen
    shows it inline) -- it becomes a REJECTION at confirm time."""
    from app.services import glossary_module as gm

    warnings: list[str] = []
    fields: dict = {}

    name = _as_str(raw.get("name"))
    if not name:
        warnings.append("No workflow name was proposed -- enter one before creating.")
    fields["name"] = name

    # customer -- exact-then-case-insensitive match against the VISIBLE set.
    cust_id, cust_name = None, ""
    raw_cust = _as_str(raw.get("customer"))
    if raw_cust:
        match = _match_by_name(raw_cust, catalogs["customers"])
        if match is None:
            warnings.append(
                f"Customer {raw_cust!r} did not match a customer you can see -- "
                "left unset (this workflow will be global)."
            )
        else:
            cust_id, cust_name = match.id, match.name
    fields["customer_id"] = cust_id
    fields["customer_name"] = cust_name

    # No language pair on a workflow draft: a designed workflow takes its languages from
    # the project it runs on. Any source_locale / target_locale / pairs a model still
    # returns are dropped silently (never a warning -- the contract no longer asks for
    # them). Glossary options resolve against a BLANK pair: every approved, visible
    # glossary (the same universe apply_workflow_draft validates pins against).
    gloss_opts = gm.options_for_step(
        session,
        "",
        "",
        org_id=catalogs["org_id"],
        customer_id=catalogs["bound_customer_id"],
    )

    steps = []
    raw_steps = raw.get("steps")
    if not isinstance(raw_steps, list):
        raw_steps = []
    for i, rs in enumerate(raw_steps):
        if not isinstance(rs, dict):
            continue
        step, step_warnings = _validate_step_draft(
            rs,
            i,
            catalogs["providers"],
            gloss_opts,
            steps,
            judge_providers=catalogs.get("judge_providers"),
            fixer_providers=catalogs.get("fixer_providers"),
        )
        warnings.extend(step_warnings)
        steps.append(step)
    if not steps:
        warnings.append("No steps were proposed -- add at least one before creating.")
    fields["steps"] = steps

    notes = _as_str(raw.get("notes_for_reviewer"))
    fields["notes_for_reviewer"] = notes

    return {"fields": fields, "warnings": warnings, "model_notes": notes or None}


def _validate_step_draft(
    rs: dict,
    idx: int,
    providers,
    gloss_opts,
    prior_steps=(),
    *,
    judge_providers=None,
    fixer_providers=None,
) -> tuple[dict, list]:
    """Validate ONE proposed step. Returns (step_fields, warnings). Unresolvable
    references warn + clear; a prompt_text failing validate_step_prompt warns inline
    (via the returned step's `prompt_warning`, for the review screen). `prior_steps`
    are the already-validated earlier steps, used to resolve a run_condition guard's
    ref (SPEC §5) — a guard the model proposed that references no earlier LQA/QE step
    (or is otherwise malformed) is WARNED + DROPPED here, editable on the review page,
    and hard-re-validated at confirm."""
    from app.services import context_pkg, prompts
    from app.services import workflows as wf_service

    warnings: list[str] = []
    label = _as_str(rs.get("name")) or f"step {idx + 1}"
    step: dict = {}
    step["name"] = _as_str(rs.get("name"))

    st = _as_str(rs.get("step_type")).lower().replace("-", "_")
    # A canonical-but-not-draftable type (external) is distinguished from unknown junk:
    # name the surface where it CAN be added, then fall back to llm_agent so the review
    # page still holds an editable step (the schema never offers external, so this is a
    # belt-and-braces path). A canonical DRAFTABLE type (now incl. tm_match) survives as
    # ITSELF -- it is never rewritten to llm_agent (BACKLOG §2026-08-31: the silent
    # default was the bug). Only genuine gibberish keeps the warn-and-default behavior.
    if st in _ASSISTANT_UNSUPPORTED:
        warnings.append(
            f"Step {idx + 1} ({label}): {_ASSISTANT_UNSUPPORTED[st]} "
            f"-- {_ASSISTANT_SUBSTITUTION_NOTE}."
        )
        st = "llm_agent"
    elif st not in _WF_STEP_TYPES:
        if st:
            warnings.append(
                f"Step {idx + 1} ({label}): step type {st!r} is not recognized -- "
                "defaulting to llm_agent."
            )
        st = "llm_agent"
    step["step_type"] = st

    step["provider"] = _resolve_step_provider(
        rs.get("provider"), providers, warnings, idx, label, "provider"
    )
    step["model"] = _as_str(rs.get("model"))
    step["fallback_provider"] = _resolve_step_provider(
        rs.get("fallback_provider"), providers, warnings, idx, label, "fallback provider"
    )
    step["fallback_model"] = _as_str(rs.get("fallback_model"))

    prompt_text = _as_str(rs.get("prompt_text"))
    step["prompt_text"] = prompt_text
    step["prompt_warning"] = None
    try:
        # GitHub #43: the RETURN value is the SOFT language warning -- an llm_agent
        # prompt that names no target language. It rides the SAME two channels the hard
        # rule already uses (the per-step `prompt_warning` the review screen renders
        # inline, and the run-level `warnings` list), because the DRAFTER is where this
        # defect was actually born: The maintainer's failing prod prompt ("...into the target
        # locale...") was authored right here, and the review screen -- the last place a
        # human sees it before it becomes a saved workflow -- said nothing.
        lang_warning = prompts.validate_step_prompt(st, prompt_text)
    except prompts.PromptValidationError as exc:
        step["prompt_warning"] = str(exc)
        warnings.append(f"Step {idx + 1} ({label}): {exc}")
    else:
        if lang_warning:
            step["prompt_warning"] = lang_warning
            warnings.append(f"Step {idx + 1} ({label}): {lang_warning}")

    step["needs_review"] = _as_bool(rs.get("needs_review"))
    step["assignee_email"] = _as_str(rs.get("assignee_email"))

    gid, gname = None, ""
    raw_g = _as_str(rs.get("glossary"))
    if raw_g:
        match = _match_by_name(raw_g, gloss_opts)
        if match is None:
            warnings.append(
                f"Step {idx + 1} ({label}): glossary {raw_g!r} is not an approved "
                "glossary you can use -- left unset."
            )
        else:
            gid, gname = match.id, match.name
    step["glossary_id"] = gid
    step["glossary_name"] = gname

    step["qe_gate"] = _as_bool(rs.get("qe_gate"))
    # Preserve invalid draft values for correction; confirm refuses them atomically.
    from app.services.workflows_common_leaf import _require_whole_number

    step["qe_threshold"] = rs.get("qe_threshold")
    try:
        step["qe_threshold"] = _require_whole_number(
            rs.get("qe_threshold"), "QE threshold", 0, 100, 70
        )
    except wf_service.WorkflowError as exc:
        warnings.append(f"Step {idx + 1} ({label}): {exc}")
    # The QE judge resolves against the JUDGE set (LLMs + Jev judge engines + the legacy
    # "jev"), not the translate set, so a drafted Jev judge is kept rather than cleared.
    step["qe_provider"] = _resolve_step_provider(
        rs.get("qe_provider"),
        judge_providers if judge_providers is not None else providers,
        warnings,
        idx,
        label,
        "QE judge provider",
    )
    # QE auto-fix (Train B): the post-editor resolves against the FIXER set (LLMs only);
    # an NMT or judge engine warns + clears here and is hard-refused at confirm.
    step["qe_autofix"] = _as_bool(rs.get("qe_autofix"))
    step["qe_autofix_provider"] = _resolve_step_provider(
        rs.get("qe_autofix_provider"),
        fixer_providers if fixer_providers is not None else providers,
        warnings,
        idx,
        label,
        "QE auto-fix post-editor",
    )
    step["qe_autofix_max_rounds"] = rs.get("qe_autofix_max_rounds")
    try:
        step["qe_autofix_max_rounds"] = _require_whole_number(
            rs.get("qe_autofix_max_rounds"), "QE auto-fix max rounds", 1, 5, 3
        )
    except wf_service.WorkflowError as exc:
        warnings.append(f"Step {idx + 1} ({label}): {exc}")
    step["qe_autofix_cross_check"] = _as_bool(rs.get("qe_autofix_cross_check"))

    # chip task_8face0e6: the lqa step's numeric pass bar ("pass with a 94"). Validated
    # through the SAME [0,100] reject rule the add/update seam uses (BUG-A202: an
    # out-of-band value is refused, never silently clamped to a gate-disabling 0/100) --
    # but WARN+DROP here (draft time, like every other unresolvable field), so the review
    # screen re-offers it editable and the hard reject lands at confirm. None/blank =
    # inherit the default 90. It rides on any step type here; the confirm path strips it
    # (dead-for-type) on a non-lqa step, mirroring qe_gate/glossary.
    step["lqa_threshold"] = None
    raw_lqa = rs.get("lqa_threshold")
    if raw_lqa not in (None, ""):
        try:
            step["lqa_threshold"] = wf_service._require_valid_lqa_threshold(raw_lqa)
        except wf_service.WorkflowError as exc:
            warnings.append(f"Step {idx + 1} ({label}): LQA pass threshold dropped -- {exc}")

    tmx = rs.get("tm_examples")
    step["tm_examples"] = True if tmx is None else _as_bool(tmx)
    step["tm_examples_window"] = context_pkg.clamp_examples_window(
        rs.get("tm_examples_window") if rs.get("tm_examples_window") is not None else 3
    )

    # SPEC §5 pre-step guard (Phase 2): validate the proposed run_condition against the
    # EARLIER drafted steps (warn + DROP on anything malformed/unresolvable — the review
    # screen re-offers it editable, and confirm hard-re-validates). None = always run.
    step["run_condition"] = None
    raw_rc = rs.get("run_condition")
    if raw_rc:
        try:
            step["run_condition"] = wf_service.validate_run_condition(raw_rc, prior_steps)
        except wf_service.WorkflowError as exc:
            warnings.append(f"Step {idx + 1} ({label}): run condition dropped -- {exc}")

    # SPEC §5 Phase 3/4 (D1/D3/D4): the stable key + post-step branch + loop ceiling the
    # LLM proposed. SAME skip-and-warn discipline as run_condition — anything malformed or
    # unresolvable is WARNED + DROPPED here (editable on the review screen, hard-re-
    # validated at confirm), NEVER silently guessed and NEVER a saved-invalid graph.
    #   * key: slugified + uniqueness-checked against the EARLIER drafted keys.
    #   * branch: validated against the earlier drafted keys + this step's own (clean) key
    #     and the D3 gate rule (needs an lqa step or qe_gate=true). A branch that names a
    #     not-yet-defined (later) step, or sits on a non-gate step, is dropped whole.
    #   * max_visits: a positive int, else dropped.
    step["key"] = None
    raw_key = rs.get("key")
    if raw_key not in (None, ""):
        try:
            step["key"] = wf_service.validate_key(raw_key, prior_steps)
        except wf_service.WorkflowError as exc:
            warnings.append(f"Step {idx + 1} ({label}): step key dropped -- {exc}")

    step["max_visits"] = None
    raw_mv = rs.get("max_visits")
    if raw_mv not in (None, ""):
        try:
            mv = int(raw_mv)
            if mv < 1:
                raise ValueError
            step["max_visits"] = mv
        except (TypeError, ValueError):
            warnings.append(
                f"Step {idx + 1} ({label}): max visits {raw_mv!r} is not a whole number "
                ">= 1 -- dropped (the run-level default cap applies)."
            )

    step["branch"] = None
    raw_branch = rs.get("branch")
    if raw_branch:
        try:
            step["branch"] = wf_service.validate_branch(
                raw_branch, st, step["qe_gate"], prior_steps, self_key=step["key"]
            )
        except wf_service.WorkflowError as exc:
            warnings.append(f"Step {idx + 1} ({label}): branch dropped -- {exc}")
    return step, warnings


def _bind_drafted_step_providers(draft: dict, selection, offered_providers) -> None:
    """QA-7 (#37): bind drafted EXECUTABLE step providers to the author's SELECTED engine.

    The "Use provider" pick drives only the authoring call; a drafted step's `provider`
    comes from the model's free-text JSON, so "use the local model" + a registered
    engine:<id> yields translate/fix steps on a base-URL-less 'local' (unreachable) and
    lqa steps on a keyless 'claude' (-> MockProvider), while the selected engine is
    referenced by NOTHING. Fix: when the author picked a CONCRETE, RUNNABLE provider,
    replace every executable step's provider that resolves to NOTHING RUNNABLE with that
    selection, and surface ONE reviewer-visible warning naming the bound steps. Mutates
    `draft` in place (its "fields"["steps"] providers + "warnings"); the human confirms on
    the review screen (web) or sees the warning printed (CLI), so nothing is rewritten
    silently past review, and apply_workflow_draft re-validates the result unchanged.

    "Resolves to NOTHING RUNNABLE" is BOTH shapes the model produces (mirrors the report's
    parenthetical "if it resolves to nothing runnable -- substitute the selection"):
      * A step naming a CONCRETE provider that is not live (a 'local' without a base URL, a
        keyless 'claude' -> MockProvider) -- a drafted MISTAKE the model invented -> BOUND.
      * A BLANK/absent step provider ("inherit the run default", config.DEFAULT_PROVIDER via
        orchestrator._spec_for_step's `step.provider or config.DEFAULT_PROVIDER`) WHEN that
        run default is itself NON-live. On the keyless install QA-7 targets the default
        ('claude') falls to MockProvider, so a blank step would silently run on the offline
        mock, NOT the selected engine -- the same footgun spelled as a blank -> BOUND, and
        named in the SAME binding warning (QA-7 review follow-up, MED: the review dropdown
        showed 'default' with no hint it != the selection; the human can still change it).
    LEFT UNTOUCHED (semantics preserved, byte-identical to today):
      * A step naming a DIFFERENT provider that IS live is RESPECTED (the description named
        a real, runnable provider for that step).
      * A BLANK/absent provider WHEN the run default IS live -- it inherits a runnable
        default, a legitimate default-step authored state, so it is not rewritten/flagged.
      * No concrete selection, a selection that is not an offered provider, OR a selection
        that is not itself live -> a whole-draft NO-OP (today's behavior, byte-identical):
        binding to a dead selection cannot make a step runnable (and there is no live engine
        to bind a blank to), so the draft is left exactly as-is.
    On a bind the step's drafted `model` is CLEARED (set "") so the bound engine uses its
    OWN default model, not a free-text model name the LLM attached to the now-replaced
    provider (e.g. a cloud model id pinned onto a local engine); a blank model resolves to
    the provider's own default at run time (_spec_for_step -> the engine profile's model).
    QA-7 review follow-up (LOW): avoids a provider/model family mismatch on rebind.
    Only the primary `provider` (the model call) is bound; a blank qe_provider already
    "reuses the step provider", and fallback_provider blank means "no fallback" (adding one
    would change semantics), so neither is touched."""
    from app import config
    from app.services.providers import provider_is_live

    sel = _match_provider(_as_str(selection), offered_providers)
    if not sel or not provider_is_live(sel):
        return
    # A blank/absent executable step inherits config.DEFAULT_PROVIDER at run time; treat it as
    # "nothing runnable" (bind it) exactly when that default is itself non-live -- otherwise a
    # blank silently runs on the offline mock while the selected engine is referenced by
    # nothing (the QA-7 footgun spelled as a blank).
    default_is_live = provider_is_live(_as_str(config.DEFAULT_PROVIDER))
    bound: list[str] = []
    for i, step in enumerate(draft.get("fields", {}).get("steps") or []):
        if step.get("step_type") not in _EXECUTABLE_STEP_TYPES:
            continue
        cur = _as_str(step.get("provider"))
        if cur and provider_is_live(cur):
            continue  # the draft named a runnable provider for this step -- respect it
        if not cur and default_is_live:
            continue  # blank inherits a LIVE run default -- a legitimate default-step state
        # else: a concrete non-live provider, OR a blank inheriting a non-live default -- both
        # resolve to nothing runnable while a live engine was selected. Bind it + warn.
        step["provider"] = sel
        step["model"] = ""  # use the bound engine's own default model (LOW: no family mismatch)
        label = _as_str(step.get("name")) or f"step {i + 1}"
        bound.append(f"step {i + 1} ({label})")
    if bound:
        draft.setdefault("warnings", []).append(
            f"Step providers bound to {sel}: {', '.join(bound)} had no runnable provider "
            "set, so they were bound to your selected engine. Change any to a different "
            "provider on the review screen before creating if you prefer."
        )


def _warn_self_judging_lqa(draft: dict) -> None:
    """#148: warn when a drafted LQA step would be judged by the engine that WROTE the text.

    An LQA step resolves its judge as `step.provider or config.DEFAULT_PROVIDER`
    (`orchestrator_part7`: `"provider": m.provider or config.DEFAULT_PROVIDER`), and the
    drafter routinely emits BLANK providers on every step -- a legitimate default-step
    state that `_bind_drafted_step_providers` deliberately leaves alone when the run
    default is live. The consequence on a single-engine install is that the translate step
    and the check that grades it resolve to the SAME engine, so the model marks its own
    homework.

    Measured on prod (campaign qa-0916w, run #196 vs #197): gemma scored its own
    translation 100 and PASSED the 90 gate; claude scored the same text 85 and FAILED it.
    A correction loop whose gate cannot fail never reaches its fix step, so the loop the
    operator asked for is decorative.

    THIS WARNS, IT DOES NOT REBIND. Picking a different judge is a SPEND decision -- on
    this prod the alternative to the free local box is a paid Claude call -- and the
    drafter has no basis for making it on the operator's behalf. `_bind_drafted_step_providers`
    substitutes only where the current value resolves to NOTHING RUNNABLE; here it is
    perfectly runnable and merely unwise, which is a judgement for the human on the review
    screen. So the warning names the collision and the alternatives that actually exist on
    this install, which is what turns it into one dropdown change instead of a research task.

    Family resolution delegates to `quality_lens.engine_family`, the ONE resolver the
    committee/lens judge-bias warnings already use (stress-0906 #92 recorded what happens
    when that logic gets a second copy). Identity is checked FIRST and independently:
    two `engine:<id>` refs whose family nobody can resolve are still the same engine
    grading its own homework (`tools_common._same_engine_ref`).
    """
    from app import config
    from app.services import engines, quality_lens

    steps = draft.get("fields", {}).get("steps") or []
    default_ref = _as_str(config.DEFAULT_PROVIDER)

    def _effective(step) -> str:
        return _as_str(step.get("provider")) or default_ref

    def _ident(step) -> str:
        ref = engines.canonicalize_ref(_effective(step)).lower()
        model = _as_str(step.get("model")).lower()
        return f"{ref}|{model}" if (ref and model) else ref

    judges = [(i, st) for i, st in enumerate(steps) if st.get("step_type") == "lqa"]
    authors = [(i, st) for i, st in enumerate(steps) if st.get("step_type") == "llm_agent"]
    if not judges or not authors:
        return

    collisions: list[str] = []
    for j_idx, judge in judges:
        j_ident, j_ref = _ident(judge), _effective(judge)
        j_fam = quality_lens.engine_family(j_ref, _as_str(judge.get("model")))
        for a_idx, author in authors:
            same = _ident(author) == j_ident
            if not same and j_fam:
                same = (
                    quality_lens.engine_family(_effective(author), _as_str(author.get("model")))
                    == j_fam
                )
            if same:
                j_label = _as_str(judge.get("name")) or f"step {j_idx + 1}"
                a_label = _as_str(author.get("name")) or f"step {a_idx + 1}"
                collisions.append(
                    f"step {j_idx + 1} ({j_label}) judging step {a_idx + 1} ({a_label})"
                )
                break

    if not collisions:
        return
    where = (
        "the install default"
        if not _as_str(judges[0][1].get("provider"))
        else "the provider set on it"
    )
    draft.setdefault("warnings", []).append(
        f"A quality check is judging the engine that produced the text: {'; '.join(collisions)}. "
        f"The check inherits its engine from {where}, which here is the same one doing the "
        "translating, so the model grades its own output and the check tends to pass -- which "
        "means a correction/fix step gated on a FAILED check may never run. Set a DIFFERENT "
        "engine on the check step(s) on this screen before creating."
    )


def _merge_back_current(current: dict, new_fields: dict, raw_steps, error_step_idxs: set) -> None:
    """MERGE-BACK (F1): the model returns the whole corrected draft, so a field it OMITS
    would validate to a blank/default and silently DROP the user's current value. Restore
    the current-state value for any field the model did NOT genuinely provide, so a
    non-compliant model (one that omits fields) cannot erase a hand-edit -- the edit-
    preservation guarantee is STRUCTURAL, not model-dependent. A field the model
    EXPLICITLY changed stays changed (that is the repair).

    Per step (matched by index -- see the reorder caveat in _diff_drafts): for each field,
      * ABSENT from the model's returned step  -> always restore the current value
        (an omission is never an intent to clear -- it means "I did not touch this").
      * returned EMPTY/null when the current value was meaningful -> restore ONLY when the
        step had no validation error (on a problem step an explicit clear may be the fix,
        so the model's empty is kept). A step BEYOND the current length is newly added -- it
        has nothing to merge back. Mutates new_fields in place."""
    cur_steps = current.get("steps") or []
    new_steps = new_fields.get("steps") or []
    for i, ns in enumerate(new_steps):
        if i >= len(cur_steps):
            continue  # a newly-added step (e.g. a missing branch-target step) -- nothing
        cur = cur_steps[i]
        raw_step = (
            raw_steps[i]
            if (
                isinstance(raw_steps, list)
                and i < len(raw_steps)
                and isinstance(raw_steps[i], dict)
            )
            else {}
        )
        step_had_error = i in error_step_idxs
        for f, rk in _STEP_MERGE_KEYS:
            if _meaningful(raw_step.get(rk)):
                continue  # the model gave a real value -> keep the validated new value
            if not _meaningful(cur.get(f)):
                continue  # nothing to preserve
            if rk not in raw_step:
                ns[f] = cur.get(f)  # ABSENT -> always restore
            elif not step_had_error:
                ns[f] = cur.get(f)  # explicit empty on a clean step -> restore


def _repair_prompt(current_json: str, errors, catalogs: dict) -> str:
    """The grounded REPAIR prompt: the current draft (in the author JSON contract) + the
    verbatim validation errors + the same caller-visible catalogs the author prompt
    grounds on, with a tight "fix ONLY the errors, preserve everything else, return the
    COMPLETE corrected draft" instruction. The branching/key/guard rules (_WF_BRANCH_RULES)
    are included because the most common failures are graph errors (a branch target that
    names no step, a forward jump, a branch on a non-gate step)."""
    cust_names = [c.name for c in catalogs["customers"]]
    gloss_names = [g.name for g in catalogs["glossaries"]]
    provs = catalogs["providers"]
    err_lines = "\n".join(f"  {i + 1}. {e}" for i, e in enumerate(errors)) or "  (none)"
    lines = [
        "A drafted localization WORKFLOW failed validation. Return the MINIMAL corrected "
        "draft as a SINGLE JSON object.",
        "",
        "Fix ONLY what the errors below require. PRESERVE every other field EXACTLY as "
        "given -- do not rename, reword, re-order, or restructure anything the errors do "
        "not mention. Return the COMPLETE corrected draft (all steps + fields), not just "
        "the changed parts.",
        "",
        "Validation errors to fix:",
        err_lines,
        "",
        "Current draft (JSON):",
        current_json,
        "",
        "Pick any replacement values ONLY from these lists (do NOT invent customers, "
        "providers, or glossaries that are not listed):",
        "- Customers: " + (", ".join(cust_names) or "(none available)"),
        "- Providers: " + ", ".join(provs),
        "- Approved glossaries: " + (", ".join(gloss_names) or "(none available)"),
        "",
        _WF_BRANCH_RULES,
        "",
        "Return ONLY JSON: " + _WF_SCHEMA,
    ]
    return "\n".join(lines)
