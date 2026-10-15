"""Promptable setup (SPEC-promptable-setup, Branch A) -- turn a plain-language
description into a VALIDATED project-creation draft.

Transitional compatibility shim for one release (added 2026-09-14, remove the re-export block after
the next tagged release): `__all__` re-exports _today_anchor, SetupAuthorError, _ParseError,
_JSON_FENCE_RE, _extract_json_dict, _base_catalogs, _project_catalogs, _project_prompt,
_validate_project_draft, _WF_STEP_TYPES, _ASSISTANT_UNSUPPORTED, _ASSISTANT_SUBSTITUTION_NOTE,
_BRANCH_HIDDEN_TYPES, _STRIP_CANDIDATE_FIELDS, _WF_SCHEMA, _WF_BRANCH_RULES, _EXECUTABLE_STEP_TYPES,
WorkflowApplyError, _workflow_catalogs, _workflow_prompt, _resolve_step_provider, _WF_LEVEL_DIFF,
_STEP_DIFF_FIELDS, _coerce_int, _norm_struct, _diff_drafts, _STEP_MERGE_KEYS, _meaningful,
_error_step_indices, _removed_step_notes, _serialize_workflow_draft, _cleanup_partial_workflow,
_as_bool, _as_str, _match_by_name, _match_provider, _author_json, _validate_workflow_draft,
_validate_step_draft, _bind_drafted_step_providers, _merge_back_current, _repair_prompt from
app.services.setup_author_helpers, app.services.setup_author_part2.

The house pattern, mirrored from orchestrator.author_prompt: resolve + spend-guard
the provider, then run the completion through providers.run_completion with a
generous STARTING output budget (so a large-output draft is not truncated by the
input-sized heuristic) -- this gains escalate-on-truncation, Prompt-Tap capture, and
an accurate one-time spend-ledger stamp that the old direct prov.complete() call
lacked. No refusal guard (this is setup metadata, not translation output). Retry
story: the framework escalates the OUTPUT BUDGET on truncation, and design
decision #2's ONE repair retry handles a hard JSON PARSE failure -- two orthogonal
retries, neither a loop.

Contract (design decisions, do not re-derive):
  * STATELESS: the authoring call persists NOTHING. It returns a validated draft
    the review surface (dialog / CLI) holds; the human submits through the
    EXISTING, already-guarded POST /projects (or content.project_create) route.
  * The LLM PROPOSES; the server VALIDATES every reference against the caller's
    REAL, visibility-scoped catalogs (same seams as the pages -- access org/
    bound-customer confinement). An unresolvable reference the model DID propose
    becomes a WARNING (skip-and-warn, apply_template's philosophy), never a silent
    guess and never a hard failure. A field the description never mentioned stays
    at the form default and is NOT flagged.
  * Single-shot JSON + one repair retry: parse defensively; a hard parse failure
    (not an object / no JSON found) triggers exactly ONE retry that appends the
    exact parse error; a second parse failure fails cleanly (web 502 / CLI
    non-zero). Never a loop, never a half-applied form.

The fence-tolerant JSON extraction is the JSON-tolerant sibling of
orchestrator._clean_draft (a weak local model wraps its answer in a ```json
fence or a sentence of prose) -- factored here rather than duplicated inline.

Documented in: docs/architecture/project-setup-and-templates.md
"""

import json
import re
from datetime import date

from app import config
from app.services import ids, qe_autofix
from app.services.setup_author_helpers import (
    _ASSISTANT_SUBSTITUTION_NOTE,
    _ASSISTANT_UNSUPPORTED,
    _BRANCH_HIDDEN_TYPES,
    _EXECUTABLE_STEP_TYPES,
    _JSON_FENCE_RE,
    _STEP_DIFF_FIELDS,
    _STEP_MERGE_KEYS,
    _STRIP_CANDIDATE_FIELDS,
    _WF_BRANCH_RULES,
    _WF_LEVEL_DIFF,
    _WF_SCHEMA,
    _WF_STEP_TYPES,
    SetupAuthorError,
    WorkflowApplyError,
    _as_bool,
    _as_str,
    _base_catalogs,
    _cleanup_partial_workflow,
    _coerce_int,
    _diff_drafts,
    _error_step_indices,
    _extract_json_dict,
    _match_by_name,
    _match_provider,
    _meaningful,
    _norm_struct,
    _ParseError,
    _project_catalogs,
    _project_prompt,
    _removed_step_notes,
    _resolve_step_provider,
    _serialize_workflow_draft,
    _today_anchor,
    _validate_project_draft,
    _workflow_catalogs,
    _workflow_prompt,
)
from app.services.setup_author_part2 import (
    _author_json,
    _bind_drafted_step_providers,
    _merge_back_current,
    _repair_prompt,
    _validate_step_draft,
    _validate_workflow_draft,
    _warn_self_judging_lqa,
)

__all__ = [
    "_ASSISTANT_SUBSTITUTION_NOTE",
    "_ASSISTANT_UNSUPPORTED",
    "_BRANCH_HIDDEN_TYPES",
    "_EXECUTABLE_STEP_TYPES",
    "_JSON_FENCE_RE",
    "_STEP_DIFF_FIELDS",
    "_STEP_MERGE_KEYS",
    "_STRIP_CANDIDATE_FIELDS",
    "_WF_BRANCH_RULES",
    "_WF_LEVEL_DIFF",
    "_WF_SCHEMA",
    "_WF_STEP_TYPES",
    "SetupAuthorError",
    "WorkflowApplyError",
    "_ParseError",
    "_as_bool",
    "_as_str",
    "_author_json",
    "_base_catalogs",
    "_bind_drafted_step_providers",
    "_cleanup_partial_workflow",
    "_coerce_int",
    "_diff_drafts",
    "_error_step_indices",
    "_extract_json_dict",
    "_match_by_name",
    "_match_provider",
    "_meaningful",
    "_merge_back_current",
    "_norm_struct",
    "_project_catalogs",
    "_project_prompt",
    "_removed_step_notes",
    "_repair_prompt",
    "_resolve_step_provider",
    "_serialize_workflow_draft",
    "_today_anchor",
    "_validate_project_draft",
    "_validate_step_draft",
    "_validate_workflow_draft",
    "_warn_self_judging_lqa",
    "_workflow_catalogs",
    "_workflow_prompt",
    "date",
    "re",
]


def _author_json_with_repair(
    prov_ref, system, base_prompt, session, *, org_id, model, tap_origin: str = "setup_author"
):
    """Single-shot authoring JSON call + the ONE repair retry (design decision #2):
    on a hard parse failure, re-issue the SAME prompt with the exact parse error
    appended; a second parse failure raises a clean 502. Shared verbatim by
    author_project_setup and author_workflow_setup.

    `tap_origin` is threaded to the underlying _author_json so the draft-repair seam's
    calls are tagged "draft_repair" in the Prompt-Tap capture."""
    try:
        return _author_json(
            prov_ref,
            system,
            base_prompt,
            session,
            org_id=org_id,
            model=model,
            tap_origin=tap_origin,
        )
    except _ParseError as exc:
        retry_prompt = (
            base_prompt
            + "\n\nYour previous response could not be parsed as JSON ("
            + exc.detail
            + "). Return ONLY the JSON object -- no prose, no code "
            "fences, no trailing text."
        )
        try:
            return _author_json(
                prov_ref,
                system,
                retry_prompt,
                session,
                org_id=org_id,
                model=model,
                tap_origin=tap_origin,
            )
        except _ParseError as exc2:
            raise SetupAuthorError(
                "The authoring model did not return valid JSON after a repair "
                "retry. Try rephrasing the description.",
                status=502,
            ) from exc2


# --- project authoring -------------------------------------------------------
def author_project_setup(
    session, *, description, provider, org_id, actor, bound_customer_id=None, model=None
) -> dict:
    """Draft a new-project setup from `description`. Returns
    {"fields": {...validated...}, "warnings": [str], "model_notes": str|None}.

    `fields` mirrors the new-project dialog contract EXACTLY (name, customer_id,
    content_type_id, source_locale, target_locales, due_date, default_provider,
    provider_mode, pair_providers, notes_for_reviewer) so the review surface can
    prefill each control and submit the untouched POST /projects form.

    Catalogs are built through the SAME visibility seams as the dialog (org filter
    + bound-customer confinement), so the model can only ever see -- and the
    validator can only ever resolve -- values the caller could pick in the UI. A
    bound caller naming a foreign customer therefore fails the name match naturally
    (it isn't in the visible set) and is warned + cleared, never resolved across
    the ring."""
    from app.services.providers import resolve_default_model

    prov_ref = provider or config.DEFAULT_PROVIDER
    # Thread a MODEL: a UI/CLI pick wins; blank resolves a sensible cloud default so a
    # keyed built-in with an empty DEFAULT_<X>_MODEL no longer trips the empty-model
    # guard. An engine:<id>/local ref keeps blank (its profile model applies).
    auth_model = resolve_default_model(prov_ref, model, org_id)
    catalogs = _project_catalogs(session, org_id=org_id, bound_customer_id=bound_customer_id)
    system = "You are a localization setup assistant. Output only JSON."
    base_prompt = _project_prompt(description or "", catalogs)
    raw = _author_json_with_repair(
        prov_ref, system, base_prompt, session, org_id=org_id, model=auth_model
    )
    return _validate_project_draft(session, raw, catalogs)


def author_workflow_setup(
    session, *, description, provider, org_id, actor, bound_customer_id=None, model=None
) -> dict:
    """Draft a whole workflow from `description`. Returns
    {"fields": {...validated...}, "warnings": [str], "model_notes": str|None}.

    `fields` holds name, customer_id/customer_name, a list of validated `steps`, and
    notes_for_reviewer. A designed workflow carries no language pair of its own -- the
    project it runs on decides the languages -- so `fields` never holds source_locale /
    target_locales and a draft is never fanned out into a copy-to-pairs group.
    Catalogs (customers, providers incl. engine:<id>, locale codes, approved
    glossaries, step-type vocabulary) are built through the SAME visibility seams as
    the builder page, so the model can only ever pick — and the validator only ever
    resolve — values the caller could choose in the UI. Same single-shot + ONE repair
    retry story as author_project_setup."""
    from app.services.providers import resolve_default_model

    prov_ref = provider or config.DEFAULT_PROVIDER
    # Same model threading as author_project_setup (see there): a UI/CLI pick wins,
    # blank resolves a cloud default, an engine ref keeps its own model.
    auth_model = resolve_default_model(prov_ref, model, org_id)
    catalogs = _workflow_catalogs(session, org_id=org_id, bound_customer_id=bound_customer_id)
    system = "You are a localization setup assistant. Output only JSON."
    base_prompt = _workflow_prompt(description or "", catalogs)
    raw = _author_json_with_repair(
        prov_ref, system, base_prompt, session, org_id=org_id, model=auth_model
    )
    draft = _validate_workflow_draft(session, raw, catalogs)
    # QA-7 (#37): bind drafted EXECUTABLE step providers to the author's SELECTED engine.
    # The `provider` pick drove only the authoring call above; without this, the drafted
    # steps' provider values (the model's free text) leave "use the local model" + a
    # registered engine running on a keyless built-in / an unreachable 'local', with the
    # selected engine referenced by nothing. Keyed on the RAW `provider` arg (not prov_ref)
    # so "no selection" is byte-identical. This mutates the draft the review surface holds;
    # the human confirms it (web) / sees the warning (CLI) — nothing rewritten past review,
    # and apply_workflow_draft re-validates the result unchanged.
    _bind_drafted_step_providers(draft, provider, catalogs["providers"])
    # #148: AFTER binding, so it judges the providers the operator will actually see.
    _warn_self_judging_lqa(draft)
    # A designed workflow has no language pair -- it takes its languages from the project
    # it runs on. If the description named languages ("English to Spanish"), say so in one
    # honest line rather than storing them: the review screen shows it with the warnings.
    from app.services import prompts

    if description and prompts.named_language(description):
        draft["warnings"].append(
            "Languages come from the project you run this workflow on, so the languages "
            "in your description were not stored on the workflow."
        )
    return draft


def apply_workflow_draft(
    session,
    payload: dict,
    *,
    org_id,
    actor,
    bound_customer_id=None,
    adapt: bool = False,
    warnings: list | None = None,
    validate_only: bool = False,
):
    """Create a workflow (+ steps) from an UNTRUSTED review-form payload, ATOMICALLY.

    Re-validates EVERYTHING as untrusted input the same way the manual builder does:
    name required; a submitted customer must be visible/active/same-org (and, for a
    bound caller, their OWN customer); each step's provider/
    fallback/qe_provider in the offered set; each pinned glossary in the approved
    visible set (a BLANK pair: a designed workflow has no pair of its own); each
    llm_agent prompt_text passes prompts.validate_step_prompt. ANY of THOSE
    failures raises WorkflowApplyError with the full error list and creates NOTHING
    (the write phase runs only after every check passes, so a poisoned step yields
    zero rows).

    `warnings` (SPEC-lqa-step-honest-options, adversarial-review finding), when a
    list is passed, is APPENDED to (never replaced) with one message per step that
    had a dead-for-its-type field STRIPPED (reset to its safe default) rather than
    rejected -- e.g. a drafted lqa step with qe_gate=true (QE never runs on lqa; the
    exact shape of a real prod misconfiguration), or a resubmitted review form whose
    now-hidden checkbox still posted its old value (the review page cannot offer the
    human a way to un-set a field it hides). This is STRIP-AND-WARN, not a rejection:
    unlike the errors above, a stripped dead field never blocks the apply -- the
    workflow is created (with those fields safely defaulted) and the caller surfaces
    the warnings alongside the success (review page / apply result / CLI output).
    Direct human authoring (add_step/update_step, and the manual builder that calls
    them) keeps the HARD refusal — only the AI-proposed draft path is this lenient,
    because only there does the human have no rendered control to fix the field
    before submitting.

    On success: creates exactly ONE ungrouped Workflow (wf_service.create_workflow) with
    NO language pair (source_locale / target_locale stay NULL; group_id IS NULL) and adds
    every step (wf_service.add_step -- the SAME seam POST /workflows/{id}/steps uses,
    which validates QE thresholds/rounds, bounds windows and coerces flags). A draft
    never stamps a pair and never fans out into a multilingual group; the workflow takes
    its languages from the project it runs on. Returns the new Workflow.

    `adapt` is retained in the signature so existing callers do not break, but it is a
    NO-OP on this path: a designed workflow has no language variants to adapt."""
    from app.models import Scope
    from app.services import (
        customers as customer_service,
    )
    from app.services import (
        glossary_module as gm,
    )
    from app.services import (
        prompts,
    )
    from app.services import (
        workflows as wf_service,
    )

    errors: list[str] = []

    name = _as_str(payload.get("name"))
    if not name:
        errors.append("A workflow name is required.")

    # customer -- optional; a submitted id must be selectable (exists, same-org, not
    # archived) AND, for a bound caller, their own customer (never routed elsewhere).
    raw_cid = payload.get("customer_id")
    # coerce_id never raises on the untrusted payload: a Unicode digit / >2**63 value
    # resolves to None (no customer) exactly like a blank, never a 500 at int().
    cust_id = ids.coerce_id(raw_cid)
    if cust_id is None and bound_customer_id is not None:
        # #468: a customer-bound caller never creates a GLOBAL (shared) workflow; a
        # blank customer becomes its own (same rule as create_workflow / clone).
        cust_id = ids.coerce_id(bound_customer_id)
    if cust_id is not None:
        if customer_service.selectable(session, cust_id, org_id=org_id) is None:
            errors.append(
                f"Customer {cust_id} is not available (unknown, archived, or in another org)."
            )
        elif bound_customer_id is not None and cust_id != bound_customer_id:
            errors.append("You can only create a workflow for your own customer.")

    # A designed workflow carries NO language pair: it takes its languages from the
    # project it runs on (source = project.source_locale, target = the run's locale).
    # Any source_locale / target_locales a legacy review form or old client still posts
    # are ignored -- nothing is stamped and there is no fan-out into a group. Glossary
    # pins are validated against a BLANK pair (options_for_step returns every approved,
    # visible glossary); a pin that does not cover a given run's languages is skipped at
    # run time (orchestrator), exactly as a blank-paired workflow already behaves.
    catalogs = _workflow_catalogs(session, org_id=org_id, bound_customer_id=bound_customer_id)
    providers = catalogs["providers"]
    allowed_gloss = {
        g.id
        for g in gm.options_for_step(session, "", "", org_id=org_id, customer_id=bound_customer_id)
    }

    raw_steps = payload.get("steps") or []
    if not raw_steps:
        errors.append("A workflow needs at least one step.")
    clean_steps: list[dict] = []
    for i, rs in enumerate(raw_steps):
        st = _as_str(rs.get("step_type")).lower().replace("-", "_")
        # HONEST exclusion vs unknown junk (atomic reject either way -- st is set to a
        # placeholder only so the loop can keep collecting the rest of the errors; the
        # write phase runs only if `errors` is empty). A canonical-but-not-draftable-here
        # type (external) gets the message naming the builder path, NOT "unknown step
        # type" -- that was the reported wrong-message half of the maintainer's #1 bug. tm_match is
        # now in _WF_STEP_TYPES, so it passes here and is created as a real tm_match step.
        if st in _ASSISTANT_UNSUPPORTED:
            errors.append(
                f"Step {i + 1}: {_ASSISTANT_UNSUPPORTED[st]} -- {_ASSISTANT_SUBSTITUTION_NOTE}."
            )
            st = "llm_agent"
        elif st not in _WF_STEP_TYPES:
            errors.append(f"Step {i + 1}: unknown step type {st or '(blank)'!r}.")
            st = "llm_agent"
        prompt_text = _as_str(rs.get("prompt_text"))
        try:
            # GitHub #43 (adversarial review P6): the RETURN value is the SOFT language
            # warning. It goes to `warnings`, NEVER to `errors` -- errors here are atomic
            # and create nothing, and a prompt that names its language in a way the
            # catalog heuristic cannot see is a legitimate shape the product allows. Same
            # channel the dead-field strip-and-warn below uses, so the confirm path says
            # the same thing the review screen already said.
            lang_warning = prompts.validate_step_prompt(st, prompt_text)
        except prompts.PromptValidationError as exc:
            errors.append(f"Step {i + 1}: {exc}")
        else:
            if lang_warning and warnings is not None:
                warnings.append(f"Step {i + 1}: {lang_warning}")
        prov = _as_str(rs.get("provider"))
        prov_c = _match_provider(prov, providers) if prov else ""
        if prov and prov_c is None:
            errors.append(f"Step {i + 1}: provider {prov!r} is not an available provider.")
        model_val = _as_str(rs.get("model"))
        fb = _as_str(rs.get("fallback_provider"))
        fb_c = _match_provider(fb, providers) if fb else ""
        if fb and fb_c is None:
            errors.append(f"Step {i + 1}: fallback provider {fb!r} is not an available provider.")
        fallback_model_val = _as_str(rs.get("fallback_model"))
        qp = _as_str(rs.get("qe_provider"))
        # The QE judge validates against the JUDGE set (LLMs + Jev judge engines + "jev").
        qp_c = _match_provider(qp, catalogs.get("judge_providers") or providers) if qp else ""
        if st == "llm_agent" and qp and qp_c is None:
            errors.append(f"Step {i + 1}: QE judge provider {qp!r} is not an available provider.")
        # QE auto-fix (Train B): the post-editor validates against the FIXER set (LLMs
        # only), so an NMT or judge engine is a hard per-step error here (add_step's role
        # check is the final backstop).
        afp = _as_str(rs.get("qe_autofix_provider"))
        afp_c = _match_provider(afp, catalogs.get("fixer_providers") or providers) if afp else ""
        if st == "llm_agent" and afp and afp_c is None:
            errors.append(
                f"Step {i + 1}: QE auto-fix post-editor {afp!r} is not an available LLM "
                "engine (NMT and quality-judge engines cannot post-edit)."
            )
        # Review B3 (no silent engine switch): auto-fix inheriting a non-LLM step engine
        # is a per-step error here, the same refusal add_step makes.
        af_on = _as_bool(rs.get("qe_autofix"))
        af_rounds = rs.get("qe_autofix_max_rounds", 3)
        af_cross = _as_bool(rs.get("qe_autofix_cross_check"))
        if (
            st == "llm_agent"
            and af_on
            and not afp
            and (prov_c is not None)
            and not qe_autofix.fixer_is_llm(qe_autofix.effective_fixer_ref("", prov_c or ""))
        ):
            errors.append(
                f"Step {i + 1}: auto-fix needs an LLM post-editor and this step's engine is "
                "not an LLM (machine translation cannot post-edit); pick an LLM post-editor "
                "or turn auto-fix off."
            )
        raw_gid = rs.get("glossary_id")
        gid = ids.coerce_id(raw_gid)
        if gid is not None and gid not in allowed_gloss:
            errors.append(f"Step {i + 1}: glossary {gid} is not an approved glossary you can use.")
        # SPEC §5 pre-step guard (Phase 2): re-validate the (untrusted) run_condition
        # against the EARLIER clean steps — fail closed here (a per-step error), so a
        # malformed/unresolvable guard rejects the whole draft atomically. add_step
        # re-validates it once more at write time (defense-in-depth).
        rc_clean = None
        try:
            rc_clean = wf_service.validate_run_condition(rs.get("run_condition"), clean_steps)
        except wf_service.WorkflowError as exc:
            errors.append(f"Step {i + 1}: {exc}")
        qe_gate = _as_bool(rs.get("qe_gate"))
        qe_threshold_val = rs.get("qe_threshold")
        from app.services.workflows_common_leaf import _require_whole_number

        # Hidden QE fields are stripped below, including junk values. Only validate
        # fields the operator can actually edit on this step type's review form.
        if st == "llm_agent":
            try:
                qe_threshold_val = _require_whole_number(
                    qe_threshold_val, "QE threshold", 0, 100, 70
                )
            except wf_service.WorkflowError as exc:
                errors.append(f"Step {i + 1}: {exc}")
            try:
                af_rounds = _require_whole_number(af_rounds, "QE auto-fix max rounds", 1, 5, 3)
            except wf_service.WorkflowError as exc:
                errors.append(f"Step {i + 1}: {exc}")
        # chip task_8face0e6: re-validate the (untrusted) lqa_threshold with the SAME
        # [0,100] reject rule the add/update seam applies -- a hard per-step error here
        # (atomic reject, nothing saved), mirroring add_step's BUG-A202 refusal, so the
        # confirm path never silently clamps a typo'd bar. None/blank -> inherit 90.
        lqa_threshold_val = None
        try:
            lqa_threshold_val = wf_service._require_valid_lqa_threshold(rs.get("lqa_threshold"))
        except wf_service.WorkflowError as exc:
            errors.append(f"Step {i + 1}: {exc}")
        # SPEC-lqa-step-honest-options: a drafted lqa/human_review step whose fields
        # include one that type ignores (e.g. an LLM-authored draft that turns
        # qe_gate=true on for an "lqa" step -- the exact shape of a real prod
        # misconfiguration, or a resubmitted review form whose now-hidden checkbox
        # still posted its old value) is STRIPPED here, never REFUSED: the review
        # page hides these fields once the step type is set (workflow_author_review.
        # html's data-types sync), so the human has no way to un-set one before
        # confirming -- rejecting the WHOLE draft over a field they were never shown
        # would dead-end a previously-working common flow. Reset each dead-and-
        # meaningful field to its safe default and record a visible per-step warning
        # instead; every OTHER step-level check above still hard-rejects atomically
        # (a real error like an unknown provider or a bad prompt).
        dead_here = wf_service.DEAD_FIELDS_BY_TYPE.get(st, ())
        # LOW-C: _candidates is BUILT from _STRIP_CANDIDATE_FIELDS (the single source of truth
        # the parity suite pins) so a field can never be dropped from the strip without CI
        # noticing. chip task_8face0e6: lqa_threshold rides here too (dead on non-lqa steps),
        # so a drafted value on an llm_agent/human_review step is dropped-and-warned rather
        # than a hard reject over a field the review page hides.
        _candidate_values = {
            "provider": prov_c or "",
            "model": model_val,
            "fallback_provider": fb_c or "",
            "fallback_model": fallback_model_val,
            "prompt_text": prompt_text,
            "glossary_id": gid,
            "qe_gate": qe_gate,
            "qe_threshold": qe_threshold_val,
            "qe_provider": qp_c or qp,
            "qe_autofix": af_on,
            "qe_autofix_provider": afp_c or afp,
            "qe_autofix_max_rounds": af_rounds,
            "qe_autofix_cross_check": af_cross,
            "lqa_threshold": lqa_threshold_val,
        }
        _candidates = {f: _candidate_values[f] for f in _STRIP_CANDIDATE_FIELDS}
        stripped = [
            f
            for f in dead_here
            if (
                _candidates.get(f) not in (None, "", 70 if f == "qe_threshold" else 3)
                if f in ("qe_threshold", "qe_autofix_max_rounds")
                else wf_service._dead_field_is_meaningful(f, _candidates.get(f))
            )
        ]
        if stripped:
            if "provider" in stripped:
                prov_c = ""
            if "model" in stripped:
                model_val = ""
            if "fallback_provider" in stripped:
                fb_c = ""
            if "fallback_model" in stripped:
                fallback_model_val = ""
            if "prompt_text" in stripped:
                prompt_text = ""
            if "glossary_id" in stripped:
                gid = None
            if "qe_gate" in stripped:
                qe_gate = False
            if "qe_threshold" in stripped:
                qe_threshold_val = 70
            if "qe_provider" in stripped:
                qp_c = ""
            if "lqa_threshold" in stripped:
                lqa_threshold_val = None
            if "qe_autofix" in stripped:
                af_on = False
            if "qe_autofix_provider" in stripped:
                afp_c = ""
            if "qe_autofix_max_rounds" in stripped:
                af_rounds = 3
            if "qe_autofix_cross_check" in stripped:
                af_cross = False
            labels = [wf_service._DEAD_FIELD_LABELS.get(f, f) for f in stripped]
            plural = len(labels) > 1
            # L3: name the ACTUAL step type. lqa_threshold is dead on llm_agent too, so
            # the old "an lqa / a human_review" binary mislabelled an llm_agent step's
            # stripped field as a human_review one.
            msg = (
                f"Step {i + 1}: dropped {', '.join(labels)} -- "
                f"{'they have' if plural else 'it has'} no effect on a step of type "
                f"'{st}' and would never be honoured."
            )
            if warnings is not None:
                warnings.append(msg)
        if st != "llm_agent":
            # Even values equal to the defaults (e.g. 70.0) are dead here. Drop
            # their raw representation before add_step validates numeric inputs.
            qe_threshold_val = 70
            af_rounds = 3
        # SPEC §5 Phase 3/4 (D1/D3/D4): re-validate the (untrusted) stable key + branch +
        # loop ceiling against the EARLIER clean steps — fail closed here (per-step errors)
        # so an invalid graph rejects the whole draft atomically. add_step re-validates all
        # three once more at write time (defense-in-depth), against the real DB rows.
        key_clean = None
        try:
            key_clean = wf_service.validate_key(rs.get("key"), clean_steps)
        except wf_service.WorkflowError as exc:
            errors.append(f"Step {i + 1}: {exc}")
        # MED-2 (scoped by MED-B): a leftover branch is DROPPED via strip-and-warn ONLY on a
        # type whose branch controls are HIDDEN on the review page (_BRANCH_HIDDEN_TYPES:
        # human_review / tm_match / external) -- there the human has no rendered On pass/On
        # fail control to satisfy validate_branch's refusal, so rejecting the whole draft
        # would dead-end a common flow (a drafted tm_match-first shape that also carried a
        # stray branch). An llm_agent/lqa step keeps validate_branch's HARD refusal: its
        # branch controls AND (for llm_agent) the QE-gate checkbox are VISIBLE, so the error
        # is one-click-fixable and stays in the repair loop (repair is driven by exc.errors)
        # -- MED-B: the round-1 `not _step_produces_gate` predicate wrongly swallowed a branch
        # on a plain llm_agent step (qe_gate absent) too. The MANUAL builder always keeps the
        # hard refusal. LOW-B1: the drop needs a warnings sink to be honest -- a warnings=None
        # caller (no place to surface the drop) keeps the OLD hard refusal rather than a
        # silent shape change, so the else-branch (validate_branch) runs for it.
        branch_clean = None
        if rs.get("branch") and st in _BRANCH_HIDDEN_TYPES and warnings is not None:
            warnings.append(
                f"Step {i + 1}: dropped the branch -- a step of type '{st}' produces no "
                "pass/fail gate for a branch to route on."
            )
        else:
            try:
                branch_clean = wf_service.validate_branch(
                    rs.get("branch"), st, qe_gate, clean_steps, self_key=key_clean
                )
            except wf_service.WorkflowError as exc:
                errors.append(f"Step {i + 1}: {exc}")
        mv_clean = None
        raw_mv = rs.get("max_visits")
        if raw_mv not in (None, ""):
            try:
                mv_clean = int(raw_mv)
                if mv_clean < 1:
                    raise ValueError
            except (TypeError, ValueError):
                errors.append(
                    f"Step {i + 1}: max visits {raw_mv!r} must be a whole number >= 1 "
                    "(or blank to inherit the default)."
                )
        clean_steps.append(
            {
                "name": _as_str(rs.get("name")),
                "step_type": st,
                "provider": prov_c or "",
                "model": model_val,
                "fallback_provider": fb_c or "",
                "fallback_model": fallback_model_val,
                "prompt_text": prompt_text,
                "needs_review": _as_bool(rs.get("needs_review")),
                "assignee_email": _as_str(rs.get("assignee_email")),
                "glossary_id": gid,
                "qe_gate": qe_gate,
                "qe_threshold": qe_threshold_val,
                "qe_provider": qp_c or "",
                "qe_autofix": af_on,
                "qe_autofix_provider": afp_c or "",
                "qe_autofix_max_rounds": af_rounds,
                "qe_autofix_cross_check": af_cross,
                # chip task_8face0e6: the validated lqa pass bar (None = inherit 90). add_step
                # re-refuses it dead-for-type as a final fail-closed backstop.
                "lqa_threshold": lqa_threshold_val,
                "tm_examples": _as_bool(rs.get("tm_examples")),
                "tm_examples_window": rs.get("tm_examples_window"),
                "run_condition": rc_clean,
                "key": key_clean,
                "branch": branch_clean,
                "max_visits": mv_clean,
            }
        )

    if warnings is not None:
        from app.services.qe_autofix import configuration_warnings

        for index, clean_step in enumerate(clean_steps):
            warnings.extend(f"Step {index + 1}: {w}" for w in configuration_warnings(clean_step))
    if errors:
        # Atomic: reject BEFORE any write, so nothing is created.
        raise WorkflowApplyError(errors)

    # Draft-repair (validate_only): the caller wants the SAME confirm-path validation
    # (identical error rejection above + identical dead-field strip-and-warn collected
    # into `warnings` during the loop) but with NOTHING saved -- it re-renders the
    # repaired draft for the human to confirm. Return before the write phase; no
    # Workflow/step rows are created. (SPEC-lqa-step-honest-options still applies: a
    # repaired step that reintroduces a dead-for-type field is stripped-and-warned here,
    # not errored, exactly as on a real create.)
    if validate_only:
        return None

    # --- all valid: create atomically (validation done; no partial state) --------
    scope = Scope.CUSTOMER if cust_id is not None else Scope.GLOBAL
    # A draft creates exactly ONE ungrouped workflow (group_id IS NULL) with NO language
    # pair (source_locale / target_locale stay NULL) -- it takes its languages from the
    # project it runs on. The `adapt` keyword is retained for caller compatibility but is
    # a no-op here: a designed workflow has no language variants to adapt.
    wf = wf_service.create_workflow(
        session,
        name,
        scope=scope,
        customer_id=str(cust_id) if cust_id is not None else "",
        org_id=org_id,
        # Origin stamp (SPEC-workflows-redesign Phase 3): this is the Design-a-workflow
        # promptable path -> "draft".
        created_via="draft",
    )
    session.add(wf)
    session.commit()
    session.refresh(wf)

    # The base workflow row is already committed above. Its steps are a SEPARATE write
    # phase; if any of it faults mid-way we would be left with a partial workflow.
    # Compensate: delete the created workflow + its steps and re-raise cleanly.
    try:
        for cs in clean_steps:
            needs_review = cs.pop("needs_review")
            step = wf_service.add_step(session, wf.id, **cs)
            if needs_review:
                # add_step does not carry needs_review (it is set by the LLM-adapt copy
                # path); stamp it here so a drafted "review this" step is flagged.
                step.needs_review = True
                session.add(step)
                session.commit()
    except WorkflowApplyError:
        # A downstream validation-style rejection already carries a clean message and
        # cleaned up after itself; roll back the partial workflow and propagate as-is.
        _cleanup_partial_workflow(session, wf.id)
        raise
    except Exception as exc:
        _cleanup_partial_workflow(session, wf.id)
        raise WorkflowApplyError(
            [
                "The workflow could not be created because of an internal error while "
                "writing its steps. Nothing was saved -- please try again."
            ]
        ) from exc

    return wf


def repair_workflow_draft(
    session, *, payload, errors, provider, org_id, actor, bound_customer_id=None, model=None
) -> dict:
    """One LLM repair pass over the CURRENT review-form state (`payload`, possibly hand-
    edited) that failed confirm-path validation with `errors`. Returns:
        {"fields": <repaired draft in the template/fields shape, each step carrying a
                    "repaired_fields" list of the field names the repair changed>,
         "errors": <REMAINING confirm-path errors after re-validation; [] if now valid>,
         "warnings": <validation + dead-field strip-and-warn messages>,
         "workflow_repaired_fields": <workflow-level field names the repair changed>,
         "removed_notes": <honest notices if the repair returned FEWER steps (F2)>}

    Contract: STATELESS (nothing saved). Up to 2 provider calls per pass (one JSON-parse
    retry via the shared author seam). The corrected draft is MERGE-BACKED onto the current
    state (_merge_back_current, F1) so a field the model omits restores the current value
    instead of blanking -- edit preservation is STRUCTURAL, not model-dependent -- then re-
    validated with the SAME apply_workflow_draft validation the confirm path uses
    (validate_only=True), so a still-invalid repair returns honest remaining errors
    (requirement 4) and a repair that reintroduces a dead-for-type field is stripped-and-
    warned, not errored (requirement 7). The diff is computed against the CURRENT state so a
    hand-edit the repair left alone is never badged. Provider/parse/spend/truncation
    failures raise SetupAuthorError with the author path's clean 400/502 mapping."""
    from app.services import glossary_module as gm
    from app.services.providers import resolve_default_model

    prov_ref = provider or config.DEFAULT_PROVIDER
    auth_model = resolve_default_model(prov_ref, model, org_id)
    catalogs = _workflow_catalogs(session, org_id=org_id, bound_customer_id=bound_customer_id)
    cust_name_by_id = {c.id: c.name for c in catalogs["customers"]}
    # Glossary options resolve against a BLANK pair (a designed workflow carries no pair
    # of its own): every approved, visible glossary -- the same universe
    # _validate_workflow_draft / apply_workflow_draft use.
    gloss_name_by_id = {
        g.id: g.name
        for g in gm.options_for_step(session, "", "", org_id=org_id, customer_id=bound_customer_id)
    }

    current = _serialize_workflow_draft(payload, cust_name_by_id, gloss_name_by_id)
    current_json = json.dumps(current, indent=2, ensure_ascii=False)
    system = "You are a localization setup assistant. Output only JSON."
    base_prompt = _repair_prompt(current_json, list(errors), catalogs)
    raw = _author_json_with_repair(
        prov_ref,
        system,
        base_prompt,
        session,
        org_id=org_id,
        model=auth_model,
        tap_origin="draft_repair",
    )
    repaired = _validate_workflow_draft(session, raw, catalogs)
    new_fields = repaired["fields"]
    # F1 MERGE-BACK: restore any current-state field the model omitted BEFORE re-validation
    # + diff, so an omitted hand-edit is preserved (not blanked) and never spuriously
    # counted as a fix. F2: surface any steps the repair dropped.
    raw_steps = raw.get("steps") if isinstance(raw.get("steps"), list) else []
    _merge_back_current(payload, new_fields, raw_steps, _error_step_indices(errors))
    removed_notes = _removed_step_notes(payload, new_fields)

    # Re-validate the repaired draft with the SAME confirm-path validation (atomic,
    # nothing saved) to compute the REMAINING errors + surface any dead-field strip.
    strip_warnings: list = []
    remaining_errors: list = []
    try:
        apply_workflow_draft(
            session,
            new_fields,
            org_id=org_id,
            actor=actor,
            bound_customer_id=bound_customer_id,
            warnings=strip_warnings,
            validate_only=True,
        )
    except WorkflowApplyError as exc:
        remaining_errors = list(exc.errors)

    wf_changed, per_step_changed = _diff_drafts(payload, new_fields)
    for i, st in enumerate(new_fields.get("steps") or []):
        st["repaired_fields"] = sorted(per_step_changed[i]) if i < len(per_step_changed) else []
    return {
        "fields": new_fields,
        "errors": remaining_errors,
        "warnings": list(repaired["warnings"]) + strip_warnings,
        "workflow_repaired_fields": sorted(wf_changed),
        "removed_notes": removed_notes,
    }
