"""setup_author: helpers

Documented in: docs/architecture/project-setup-and-templates.md
"""

import json
import re
from datetime import date

from app import config
from app.services import ids


def _today_anchor() -> str:
    """A current-date anchor line for the authoring prompts so the model can resolve
    relative dates like 'next Friday' to a real ISO date instead of dropping them
    (BUG 3). Uses the SERVER's current date -- the no-Date rule applies only to
    workflow SCRIPTS, not to app runtime code, so date.today() is fine here."""
    return (
        f"Today is {date.today().isoformat()}. Resolve any relative date in the "
        "description (e.g. 'next Friday', 'in two weeks') against it and emit a "
        "concrete YYYY-MM-DD value."
    )


class SetupAuthorError(Exception):
    """A setup-authoring call failed with a clean, user-facing message.

    `status` is the HTTP status the web route surfaces (matching the author.json
    mapping: 400 for a bad/dead provider ref, 502 for a provider-call/parse/spend
    failure); the CLI ignores it and just prints the message + exits non-zero."""

    def __init__(self, message: str, *, status: int = 502):
        super().__init__(message)
        self.status = status
        self.message = message


class _ParseError(SetupAuthorError):
    """A HARD JSON parse failure (the model's response was not a JSON object).
    Raised by _author_json so the one-repair-retry loop one level up can catch it
    specifically (a provider/spend/dead-engine SetupAuthorError is NOT retried --
    re-prompting can't fix a bad key or a tripped cap)."""

    def __init__(self, detail: str):
        super().__init__("The authoring model did not return valid JSON.", status=502)
        self.detail = detail


_JSON_FENCE_RE = re.compile(r"```[a-zA-Z0-9_-]*\n(.*?)\n?```", re.DOTALL)


# A single ```-fenced block (optional language tag). The JSON-tolerant analogue of
# orchestrator._DRAFT_FENCE_RE: we only need the fence BODY, then we still hunt for
# the outermost {...} inside it, so this is deliberately permissive.
def _extract_json_dict(text: str) -> dict:
    """Pull a JSON OBJECT out of a model response, tolerating a ```json fence and/
    or surrounding prose (a weaker model routinely adds "Here is the setup:" or
    wraps the JSON in a code fence). Returns the parsed dict; raises _ParseError on
    a hard failure so the caller's ONE repair retry can fire."""
    if not text or not text.strip():
        raise _ParseError("the model returned an empty response")
    s = text.strip()
    m = _JSON_FENCE_RE.search(s)
    if m and m.group(1).strip():
        s = m.group(1).strip()
    start = s.find("{")
    if start == -1:
        raise _ParseError("no JSON object found in the response")
    # Balanced-brace scan from the FIRST "{", tracking JSON string/escape state so a
    # brace inside a value (or fence-less trailing prose like "... close every
    # {brace}.") never moves the boundary: stop at the matching depth-0 close. The
    # old first-"{"..rfind("}") span OVERSHOT into such trailing braces and mangled
    # the parse (forcing a needless repair retry, or a hard 502 on the 2nd failure).
    # Falls back to the rfind heuristic only when the object never balances
    # (truncated/malformed), preserving that last-chance parse.
    end, depth, in_str, esc = -1, 0, False, False
    for i in range(start, len(s)):
        c = s[i]
        if in_str:
            if esc:
                esc = False
            elif c == "\\":
                esc = True
            elif c == '"':
                in_str = False
            continue
        if c == '"':
            in_str = True
        elif c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                end = i
                break
    if end == -1:
        end = s.rfind("}")
    if end == -1 or end < start:
        raise _ParseError("no JSON object found in the response")
    try:
        data = json.loads(s[start : end + 1])
    except (ValueError, TypeError) as exc:
        raise _ParseError(str(exc)) from exc
    if not isinstance(data, dict):
        raise _ParseError("the top-level JSON value was not an object")
    return data


def _base_catalogs(session, *, org_id, bound_customer_id) -> dict:
    """The catalogs common to project + workflow authoring — customers, content
    types, providers, default provider/source, enabled locales — all scoped through
    the SAME visibility seams (archived customers excluded, org filter, R2-3
    bound-customer confinement) so no name the caller couldn't see ever leaks. Each
    caller augments this with its own extras (workflow adds glossaries + step_types).
    Validation later re-narrows content types to the RESOLVED customer's options."""
    from sqlmodel import select

    from app.models import Customer
    from app.services import (
        content_types,
        engines,
        languages_catalog,
    )
    from app.services import (
        settings as _settings,
    )

    q = select(Customer).where(Customer.archived_at.is_(None)).order_by(Customer.name)
    if org_id is not None:
        q = q.where(Customer.org_id == org_id)
    if bound_customer_id is not None:
        q = q.where(Customer.id == bound_customer_id)
    customers = list(session.exec(q).all())

    content_type_rows = content_types.list_types(
        session, org_id=org_id, customer_id=bound_customer_id
    )

    # Provider option set the dialog offers: the built-ins + every ENABLED engine's
    # engine:<id> ref (org-scoped). The authoring provider may be one of these too.
    # Quality-judge engines (jev / jev_local) are offered ONLY as a step's QE judge
    # (`judge_providers`), never as a translate provider; the legacy "jev" string rides
    # there too, so a drafted or confirmed qe_provider="jev" is kept, not cleared.
    providers = list(config.PROVIDERS)
    judges = list(config.PROVIDERS)
    # The QE auto-fix post-editor (Train B) must be a prompt LLM: built-ins + LLM engines.
    fixers = list(config.PROVIDERS)
    for prof in engines.list_profiles(org_id=org_id):
        row = engines.engine_row(prof)
        if row.get("enabled"):
            if engines.role_allows(prof.kind, "translate"):
                providers.append(row["provider_name"])
            if engines.role_allows(prof.kind, "judge"):
                judges.append(row["provider_name"])
            if engines.role_allows(prof.kind, "fixer"):
                fixers.append(row["provider_name"])

    return {
        "judge_providers": [*judges, "jev"],
        "fixer_providers": fixers,
        "org_id": org_id,
        "bound_customer_id": bound_customer_id,
        "customers": customers,
        "content_types": content_type_rows,
        "providers": providers,
        "default_provider": config.DEFAULT_PROVIDER,
        "default_source": (_settings.get("default_source_locale") or "en-US"),
        "enabled_locales": languages_catalog.enabled_language_rows(),
    }


def _project_catalogs(session, *, org_id, bound_customer_id) -> dict:
    """The caller-visible catalogs for grounding + validation, scoped through the
    SAME seams as new_project_ctx (archived customers excluded, org filter, R2-3
    bound-customer confinement). Never leaks a name the caller couldn't see."""
    return _base_catalogs(session, org_id=org_id, bound_customer_id=bound_customer_id)


def _project_prompt(description: str, catalogs: dict) -> str:
    """The grounded authoring prompt: the description + the caller-visible catalogs
    as DATA the model must pick exact values from, ending with the strict
    "Return ONLY JSON: {schema}" house instruction."""
    cust_names = [c.name for c in catalogs["customers"]]
    ct_names = [t.name for t in catalogs["content_types"]]
    provs = catalogs["providers"]
    codes = [r["code"] for r in catalogs["enabled_locales"]][:40]
    schema = (
        '{"name": string, "customer": string|null, "content_type": string|null, '
        '"source_locale": "BCP-47 code", "target_locales": ["BCP-47 code", ...], '
        '"due_date": "YYYY-MM-DD"|null, "default_provider": string|null, '
        '"provider_mode": "uniform"|"per_pair", '
        '"pair_providers": {"<target locale>": "<provider>"}, '
        '"notes_for_reviewer": string|null}'
    )
    lines = [
        "Configure a new localization PROJECT from the description below. Extract "
        "the setup into a SINGLE JSON object.",
        "",
        _today_anchor(),
        "",
        "Description:",
        (description or "").strip() or "(none given)",
        "",
        "Pick values ONLY from these lists where the description names one. Leave a "
        "field null (or omit it) when the description does not mention it -- do NOT "
        "invent customers, content types, or providers that are not listed:",
        "- Customers: " + (", ".join(cust_names) or "(none available)"),
        "- Content types: " + (", ".join(ct_names) or "(none available)"),
        "- Providers: " + ", ".join(provs),
        "- Example locale codes (BCP-47; use codes shaped like these): "
        + (", ".join(codes) or "en-US, fr-FR, de-DE"),
        "",
        "Rules: source_locale and target_locales are BCP-47 codes (e.g. en-US, "
        "fr-FR). Set default_provider ONLY if the description NAMES a provider (from "
        "the list above); otherwise leave it null so the system default is kept -- "
        "do NOT pick an arbitrary provider. Use provider_mode 'per_pair' ONLY if the "
        "description asks for a different provider per language pair, and then map "
        "each such target in pair_providers; otherwise use 'uniform' with an empty "
        "pair_providers.",
        "",
        "Return ONLY JSON: " + schema,
    ]
    return "\n".join(lines)


def _validate_project_draft(session, raw: dict, catalogs: dict) -> dict:
    """Validate the model's proposal against the caller's real catalogs. Every
    reference the model PROVIDED is resolved or warned-and-cleared (skip-and-warn);
    a field it left empty stays at the form default and is NOT flagged."""
    from app.services import content, content_types, languages_catalog

    warnings: list[str] = []
    fields: dict = {}

    # name -- required; blank warns but the draft still returns (the human fills it).
    name = _as_str(raw.get("name"))
    if not name:
        warnings.append("No project name was proposed -- enter one before creating.")
    fields["name"] = name

    # customer -- exact-then-case-insensitive name match against the VISIBLE set.
    cust_id, cust_name = None, ""
    raw_cust = _as_str(raw.get("customer"))
    if raw_cust:
        match = _match_by_name(raw_cust, catalogs["customers"])
        if match is None:
            warnings.append(
                f"Customer {raw_cust!r} did not match a customer you can see -- left unset."
            )
        else:
            cust_id, cust_name = match.id, match.name
    fields["customer_id"] = cust_id
    fields["customer_name"] = cust_name
    # 2026-07-14: a project requires a customer. When the draft resolves to no
    # customer, WARN so the review screen prompts the human to pick one before the
    # confirm/create seam (POST /projects / `--create`) hard-rejects it. Skipped for a
    # bound caller — their customer is force-stamped at create, so it can't be missing —
    # and gated on a customer actually EXISTING to pick (same seam as project_create),
    # so a zero-customer install isn't nagged.
    if cust_id is None and catalogs.get("bound_customer_id") is None and catalogs.get("customers"):
        warnings.append("Pick a customer -- a project requires one before it can be created.")

    # content type -- narrowed to what the RESOLVED customer may attach (global +
    # own); a foreign/unknown type warns and clears.
    ct_id, ct_name = None, ""
    raw_ct = _as_str(raw.get("content_type"))
    if raw_ct:
        options = content_types.options_for_project(
            session, org_id=catalogs["org_id"], customer_id=cust_id
        )
        match = _match_by_name(raw_ct, options)
        if match is None:
            warnings.append(
                f"Content type {raw_ct!r} did not match an available type -- left unset."
            )
        else:
            ct_id, ct_name = match.id, match.name
    fields["content_type_id"] = ct_id
    fields["content_type_name"] = ct_name

    # source locale -- canonicalized; invalid warns + falls back to the default.
    raw_src = _as_str(raw.get("source_locale"))
    if raw_src:
        valid, _invalid = languages_catalog.validate_codes([raw_src])
        if valid:
            fields["source_locale"] = valid[0]
        else:
            warnings.append(
                f"Source locale {raw_src!r} is not a known language code -- using "
                f"{catalogs['default_source']}."
            )
            fields["source_locale"] = catalogs["default_source"]
    else:
        fields["source_locale"] = catalogs["default_source"]

    # target locales -- canonicalized; each invalid code warns + is dropped.
    raw_tgts = raw.get("target_locales") or []
    if isinstance(raw_tgts, str):
        raw_tgts = [raw_tgts]
    valid_t, invalid_t = languages_catalog.validate_codes(
        [str(x) for x in raw_tgts if str(x).strip()]
    )
    for code in invalid_t:
        warnings.append(f"Target locale {code!r} is not a known language code -- dropped.")
    fields["target_locales"] = valid_t

    # due date -- ISO (or a parseable phrase); a bad value warns + is dropped.
    raw_due = _as_str(raw.get("due_date"))
    due_iso = ""
    if raw_due:
        try:
            dt = content.parse_due_date(raw_due)
            due_iso = dt.strftime("%Y-%m-%d") if dt else ""
        except content.DueDateError:
            warnings.append(f"Due date {raw_due!r} is not a valid date -- dropped.")
    fields["due_date"] = due_iso

    # default provider -- BUG 2: only set it when the description NAMED one. A blank/
    # null value means "not mentioned" -> leave default_provider OUT of `fields`
    # entirely so the form keeps its config.DEFAULT_PROVIDER selection (the old code
    # forced catalogs['default_provider'] here, but the model, pressured by a non-
    # nullable schema, would emit an arbitrary provider like 'local' that then got
    # silently accepted). A NON-EMPTY value that does not match the offered set is
    # still a real mistake -> warn + fall back to the default.
    raw_prov = _as_str(raw.get("default_provider"))
    if raw_prov:
        prov = _match_provider(raw_prov, catalogs["providers"])
        if prov is None:
            warnings.append(
                f"Provider {raw_prov!r} is not an available provider -- using "
                f"{catalogs['default_provider']}."
            )
            fields["default_provider"] = catalogs["default_provider"]
        else:
            fields["default_provider"] = prov
    # else: unmentioned -> omit; the dialog/CLI keeps the form's default provider.

    # per-pair providers -- each target must be one of the validated targets AND
    # each provider must be offered; anything else warns + is dropped.
    pairs: dict[str, str] = {}
    raw_pairs = raw.get("pair_providers") or {}
    if isinstance(raw_pairs, dict):
        target_set = set(fields["target_locales"])
        for tgt, prov in raw_pairs.items():
            tgt_valid, _bad = languages_catalog.validate_codes([str(tgt)])
            tgt_c = tgt_valid[0] if tgt_valid else None
            prov_m = _match_provider(_as_str(prov), catalogs["providers"])
            if tgt_c is None or tgt_c not in target_set:
                warnings.append(
                    f"Per-pair provider for {str(tgt)!r} was dropped -- it is not a "
                    "target locale of this project."
                )
                continue
            if prov_m is None:
                warnings.append(
                    f"Per-pair provider {_as_str(prov)!r} for {tgt_c} is not an "
                    "available provider -- dropped."
                )
                continue
            pairs[tgt_c] = prov_m
    # provider_mode: per_pair only makes sense with at least one valid override.
    mode = _as_str(raw.get("provider_mode")).lower()
    if pairs:
        mode = "per_pair"
    elif mode != "per_pair":
        mode = "uniform"
    else:
        mode = "uniform"  # model asked for per_pair but nothing valid to differ on
    fields["provider_mode"] = mode
    fields["pair_providers"] = pairs

    notes = _as_str(raw.get("notes_for_reviewer"))
    fields["notes_for_reviewer"] = notes

    return {"fields": fields, "warnings": warnings, "model_notes": notes or None}


_WF_STEP_TYPES = ("llm_agent", "lqa", "human_review", "tm_match")


_ASSISTANT_UNSUPPORTED = {
    "external": (
        "external steps need a vendor binding -- add them in the workflow builder "
        "after creating the workflow"
    ),
}


_ASSISTANT_SUBSTITUTION_NOTE = (
    "this step was changed to llm_agent; edit or delete it before creating"
)


_BRANCH_HIDDEN_TYPES = ("human_review", "tm_match", "external")


_STRIP_CANDIDATE_FIELDS = (
    "provider",
    "model",
    "fallback_provider",
    "fallback_model",
    "prompt_text",
    "glossary_id",
    "qe_gate",
    "qe_threshold",
    "qe_provider",
    "qe_autofix",
    "qe_autofix_provider",
    "qe_autofix_max_rounds",
    "qe_autofix_cross_check",
    "lqa_threshold",
)


_WF_SCHEMA = (
    '{"name": string, "customer": string|null, '
    '"steps": [{"name": string, "step_type": "llm_agent"|"lqa"|"human_review"|"tm_match", '
    '"provider": string|null, "model": string|null, '
    '"fallback_provider": string|null, "fallback_model": string|null, '
    '"prompt_text": string|null, "needs_review": bool, '
    '"assignee_email": string|null, "glossary": string|null, '
    '"qe_gate": bool, "qe_threshold": integer 0-100 (invalid values refused), '
    '"qe_provider": string|null, '
    '"qe_autofix": bool, "qe_autofix_provider": string|null, '
    '"qe_autofix_max_rounds": integer 1-5 (invalid values refused), '
    '"qe_autofix_cross_check": bool, '
    '"lqa_threshold": 0-100|null, '
    '"tm_examples": bool, "tm_examples_window": 0-5, '
    '"run_condition": {"ref": "prev_lqa"|"prev_qe", '
    '"op": "passed"|"failed"|"score_gte"|"score_lt", "value": number|null}|null, '
    '"key": string|null, '
    '"branch": {"on_pass": "next"|"end"|{"step": "<key>"}, '
    '"on_fail": "next"|"end"|{"step": "<key>"}}|null, '
    '"max_visits": integer|null}], '
    '"notes_for_reviewer": string|null}'
)


_WF_BRANCH_RULES = (
    "BRANCHING & LOOPS: 'key' is a short stable slug naming a step (e.g. 'fix', 'lqa') "
    "so a branch can target it. 'branch' routes AFTER a GATE-PRODUCING step (an lqa "
    "step, or an llm_agent step with qe_gate=true) by its pass/fail outcome: on_pass/"
    "on_fail each = 'next' (fall through), 'end' (finish the run), or {'step':'<key>'} to "
    "jump to that keyed step. A branch target can only name a step that appears EARLIER "
    "in the list (no forward jumps) -- so to target a step you must give THAT step a "
    "'key' AND place it before the branch. A target that names an earlier step is a "
    "bounded LOOP-BACK; 'max_visits' caps how many times a step may run (loop safety). A "
    "branch on a non-gate step is invalid. run_condition is an OPTIONAL pre-step guard "
    "(ref 'prev_lqa'/'prev_qe' + op 'failed'/'passed'/'score_lt'/'score_gte') that NEEDS "
    "an earlier lqa/qe_gate step. CORRECTION-LOOP CONVENTION: 'end' is NOT a jump, so "
    "on_pass 'end' is legal on ANY gate step and is how a check FINISHES the run on "
    "success -- give a check that should be terminal on pass a branch of on_pass 'end' "
    "plus on_fail 'next'. Without it a PASSING check silently falls through to whatever "
    "step comes next, which is how a pass ends up running the re-check that was only "
    "meant for a failure. Sending a failure FORWARD to a later fix step is not a branch "
    "at all (no forward jumps): give that fix step run_condition prev_lqa:failed instead, "
    "and write {lqa_findings} into its prompt (beside {text} or {prior}) so it can act on "
    "what the check actually found. A prompt_text (llm_agent only) MUST contain {text} "
    "(source) or {prior} (previous step's output), and MUST use the {target_locale} "
    "placeholder (plus {source_locale} where the source language matters) for the "
    "languages -- NEVER name a language or a locale code outright ('into Canadian "
    "French' or 'into fr-CA'). A workflow is written once and the PROJECT decides the "
    "languages at run time, so a prompt that hard-codes one language breaks every other "
    "project; a prompt that names no language at all is how an English namespace gets "
    "delivered as a finished translation."
)


_EXECUTABLE_STEP_TYPES = ("llm_agent", "lqa")


# --- workflow authoring (Branch B, additive) ---------------------------------
# A sibling of author_project_setup: the LLM proposes a WHOLE workflow (name +
# customer + language pair(s) + ordered steps); the server validates every
# reference against the caller's real, visibility-scoped catalogs; the review
# surface holds it in editable form fields; confirm re-validates ALL of it as
# UNTRUSTED input and creates atomically through the SAME seams the manual
# builder uses (wf_service.create_workflow + add_step, prompts.validate_step_prompt,
# glossary_module.options_for_step). Nothing is persisted at draft time.
# The step types the Design-a-workflow ASSISTANT can DRAFT + CREATE: the assistant-surface
# slice of the canonical app.models.StepType.ALL. llm_agent/lqa/human_review were always
# draftable; tm_match (PROPOSAL-tm-matching-workflow-step §7) is ORCHESTRATION-ONLY (fills +
# hard-locks qualifying 100% TM matches, no provider/prompt/QE) and is ALSO draftable here --
# it was silently DEFAULTED to llm_agent before (BACKLOG §2026-08-31, the maintainer's #1: the web
# assistant rejected 'tm_match' as unknown while the CLI + manual builder accepted it). external
# is the ONE canonical type NOT draftable here (see _ASSISTANT_UNSUPPORTED). INVARIANT (pinned by
# tests/verify_workflow_author_parity.py): set(_WF_STEP_TYPES) | set(_ASSISTANT_UNSUPPORTED) ==
# set(StepType.ALL), so ANY future StepType fails CI until this surface makes a deliberate
# accept-or-exclude choice (never a silent default-to-llm_agent for a canonical type again).
# RECOGNIZED-but-not-draftable-here, mapped to an HONEST message that names the surface where the
# type CAN be added -- DISTINCT from an unknown junk step_type (gibberish), which stays "unknown
# step type". external (PROPOSAL-external-processing-step) needs a vendor binding (WorkflowStep.
# vendor_id, validated fail-closed against the document customer's linked vendors); no drafting/
# review UI exists for that yet, so it is added in the manual builder AFTER the workflow exists
# rather than rejected as gibberish. Keep this map + _WF_STEP_TYPES exhaustive over StepType.ALL.
# MED-1: the honest-exclusion path must DISCLOSE the substitution, mirroring the junk
# path's "defaulting to llm_agent". Without it, the confirm re-render shows the excluded
# type with no <option> selected and a blind re-confirm SILENTLY creates a stray llm_agent
# step -- an extra billable MT call the author never asked for (the BUG-A201 class). The
# review dropdown additionally surfaces the posted type as a selected+disabled option
# (_render_workflow_review) so the human must consciously fix or delete the step.
# MED-B: the step types whose branch (On pass / On fail) controls are HIDDEN on the review
# page -- a direct mirror of workflow_author_review.html's data-types="llm_agent lqa" on
# those controls. A leftover branch on one of THESE types is dropped via strip-and-warn at
# confirm (the human has no rendered control to satisfy validate_branch's refusal), whereas
# an llm_agent/lqa step keeps validate_branch's HARD refusal: its On pass/On fail controls
# AND (for llm_agent) the QE-gate checkbox are visible, so the error is one-click-fixable
# and stays in the repair loop (repair is driven by exc.errors). Keep this in sync with the
# template's data-types set. external never actually reaches the branch check (it is caught
# earlier and the draft rejected atomically), but it is listed for template parity.
# LOW-C: the SINGLE source of truth for which fields the confirm-path dead-field strip
# inspects. `_candidates` (in apply_workflow_draft) is BUILT from this tuple, and
# tests/verify_workflow_author_parity.py imports it to pin the invariant "every dead field
# clean_steps carries into add_step is covered here" -- so REMOVING a field from the strip
# (which would let a dead value reach add_step un-stripped and raise mid-write) fails CI,
# not just adding one. These are exactly the engine/QE/glossary fields the review FORM posts.
# The single JSON contract both the fresh-draft author prompt (_workflow_prompt) and the
# draft-repair prompt (_repair_prompt) instruct the model to return, so the two prompts
# can never drift on the shape _validate_workflow_draft parses. Factored to one constant.
# The branching/key/loop rules the model needs to FIX the most common validation
# failures (a branch target that names no step, a forward jump, a branch on a non-gate
# step). Shared by the fresh-draft and repair prompts so the repair path teaches the
# model the same graph discipline the author path does.
# The step types that actually DISPATCH a model call (QA-7): a translate/rewrite/fix
# (llm_agent) step and a judge (lqa) step both send a request through their provider; a
# human_review step makes none, so its provider is irrelevant to engine binding.
class WorkflowApplyError(SetupAuthorError):
    """The untrusted confirm payload failed re-validation. Carries the FULL list of
    per-field errors so the review page can re-render them inline and create NOTHING
    (atomic: a single bad step means zero rows). status 400 (a bad request the user
    can fix on the review screen), distinct from the 502 provider/parse failures."""

    def __init__(self, errors):
        self.errors = list(errors)
        super().__init__("; ".join(self.errors) or "The workflow draft was invalid.", status=400)


def _workflow_catalogs(session, *, org_id, bound_customer_id) -> dict:
    """The caller-visible catalogs for grounding + validation, scoped through the SAME
    seams as the workflow builder (archived customers excluded, org filter, R2-3
    bound-customer confinement; glossaries via glossary_module.options_for_step's
    approved-visible universe). Never leaks a name the caller couldn't see."""
    from app.services import glossary_module as gm

    catalogs = _base_catalogs(session, org_id=org_id, bound_customer_id=bound_customer_id)
    # Glossary universe: every APPROVED glossary visible to the caller (blank pair ->
    # options_for_step returns all approved). The per-pair narrowing happens at
    # validate time, once the draft's language pair is known.
    catalogs["glossaries"] = gm.options_for_step(
        session, "", "", org_id=org_id, customer_id=bound_customer_id
    )
    catalogs["step_types"] = _WF_STEP_TYPES
    return catalogs


def _workflow_prompt(description: str, catalogs: dict) -> str:
    """The grounded authoring prompt: description + caller-visible catalogs as DATA
    the model picks exact values from, ending with the strict schema hint. Terse
    QE/TM/glossary step-option semantics are named so the model fills them correctly."""
    cust_names = [c.name for c in catalogs["customers"]]
    ct_names = [t.name for t in catalogs["content_types"]]
    gloss_names = [g.name for g in catalogs["glossaries"]]
    provs = catalogs["providers"]
    schema = _WF_SCHEMA
    lines = [
        "Configure a multi-step localization WORKFLOW from the description below. "
        "Extract the setup into a SINGLE JSON object.",
        "",
        _today_anchor(),
        "",
        "Description:",
        (description or "").strip() or "(none given)",
        "",
        "Pick values ONLY from these lists where the description names one. Leave a "
        "field null (or omit it) when the description does not mention it -- do NOT "
        "invent customers, providers, or glossaries that are not listed:",
        "- Customers: " + (", ".join(cust_names) or "(none available)"),
        "- Content types (domain context only): " + (", ".join(ct_names) or "(none)"),
        "- Providers (for a step's provider/fallback_provider): " + ", ".join(provs),
        "- QE judge engines (for a step's qe_provider only): "
        + ", ".join(catalogs.get("judge_providers") or provs),
        "- QE auto-fix post-editors (for a step's qe_autofix_provider only): "
        + ", ".join(catalogs.get("fixer_providers") or provs),
        "- Approved glossaries (for a step's glossary): "
        + (", ".join(gloss_names) or "(none available)"),
        "",
        "Rules: a workflow has NO language pair of its own -- it takes its languages from "
        "the project it runs on, so DO NOT output source_locale, target_locale or pairs. "
        "In each step's prompt_text use the {target_locale} placeholder (and "
        "{source_locale} where the source language matters) for the languages; never name "
        "a language or a locale code. steps run top to bottom; each step's output feeds "
        "the next. Use "
        "step_type 'llm_agent' for a translate/rewrite/fix step, 'lqa' for an "
        "automated quality-check step, 'human_review' for a step a person must "
        "approve, 'tm_match' for a TM-first pre-fill step. A 'tm_match' step is "
        "ORCHESTRATION-ONLY: it fills and hard-locks qualifying 100% translation-memory "
        "matches so every later step (MT, LQA, fix rounds) skips them; it makes NO model "
        "call, so it takes no provider/model and no prompt_text. Put it FIRST when the "
        "description asks to reuse existing translations / TM before translating "
        "(a TM-first shape). A step's prompt_text (only for llm_agent) MUST contain {text} where "
        "the source content is inserted (or {prior} to reuse the previous step's "
        "output). For the languages use the {target_locale} placeholder (substituted with "
        "the real code at run time) and {source_locale} where relevant -- NEVER name a "
        "language or a locale code ('translate into Canadian French' or 'into fr-CA' are "
        "both wrong). A workflow runs on any project, so a prompt that hard-codes one "
        "language breaks every other project's languages. "
        "qe_gate=true with qe_threshold scores each segment 0-100 and routes "
        "those below the threshold to human review (put it on an lqa/quality step). "
        "qe_autofix=true (only with qe_gate and a Jev QE judge) has an LLM post-editor "
        "rewrite a failing segment up to qe_autofix_max_rounds times (1-5, default 3) "
        "before it goes to review; qe_autofix_provider names that LLM (null = the step's "
        "own provider). Set it only when the description asks to fix QE failures "
        "automatically. "
        "lqa_threshold is the numeric PASS BAR for an 'lqa' step ONLY (0-100): the step "
        "PASSES when its LQA score is >= it (default 90 when unset). When the description "
        "names a pass mark for the quality check -- 'pass with a 94', 'must score at "
        "least 90', 'LQA gate at 85' -- set lqa_threshold to that number on the lqa "
        "step; leave it null to accept the default 90. It is distinct from qe_threshold "
        "(the per-segment QE routing cutoff) and is ignored on any non-lqa step. "
        "needs_review=true flags a step for a linguist to check before it is used. "
        "tm_examples defaults true. "
        "run_condition is an OPTIONAL pre-step GUARD: set it to run a step ONLY when the "
        "immediately-prior quality outcome matches. ref 'prev_lqa' reads the most recent "
        "lqa step; ref 'prev_qe' reads the most recent qe_gate step. op 'failed'/'passed' "
        "check the pass/fail; 'score_lt'/'score_gte' compare the score to value (0-100). "
        "Example: a Fix step that should run only when the LQA did not pass gets "
        '{"ref":"prev_lqa","op":"failed","value":null}. The guard NEEDS an '
        "earlier lqa (or qe_gate) step before it, or it is dropped. Leave run_condition "
        "null for a step that always runs. "
        "BRANCHING & LOOPS (optional): 'key' is a short stable slug naming a step (e.g. "
        "'fix', 'lqa') so a branch can target it. 'branch' routes AFTER a GATE-PRODUCING "
        "step (an lqa step, or an llm_agent step with qe_gate=true) by its pass/fail "
        "outcome: on_pass/on_fail each = 'next' (fall through), 'end' (finish the run), or "
        "{'step':'<key>'} to jump to that keyed step. A target that names an EARLIER step "
        "is a bounded LOOP-BACK (redo that step, re-check, repeat). 'max_visits' caps how "
        "many times a step may run in one run (loop safety). A branch on a non-gate step is "
        "dropped. IMPORTANT: a branch target can only name a step that appears EARLIER in "
        "the list (no forward jumps). For a 'fix only if it fails, then re-check until it "
        "passes' loop, the FAITHFUL shape puts a quality-check BEFORE the fix so the fix is "
        "GUARDED and only runs on a failure -- ORDER the steps: (1) translate; (2) a first "
        "lqa (key 'lqa1', no branch) -- the gate the fix reads; (3) the rework/FIX step "
        "(key 'fix', max_visits e.g. 3) carrying run_condition "
        "{'ref':'prev_lqa','op':'failed','value':null} so it is SKIPPED when the "
        "translation already passed; (4) a second lqa (key 'lqa2') whose branch is "
        "{'on_pass':'end','on_fail':{'step':'fix'}} to re-check and loop back to the fix "
        "until it passes or fix hits its max_visits. This drafts the maintainer's canonical ask "
        "'translate, LQA at 90, if it fails run a fix step and re-check up to 3 times' "
        "correctly -- the fix runs ONLY on a failed check, never on an already-good "
        "translation. (A simpler 3-step form -- translate, fix(key 'fix', max_visits 3), "
        "lqa(branch on_fail->fix) -- also loops, but the fix then runs UNCONDITIONALLY on "
        "the first pass even when the translation was fine; prefer the guarded 4-step shape "
        "above unless the user explicitly wants every translation reworked.) Leave "
        "key/branch/max_visits null for a plain linear step.",
        "",
        "Return ONLY JSON: " + schema,
    ]
    return "\n".join(lines)


def _resolve_step_provider(raw, providers, warnings, idx, label, what) -> str:
    """Resolve a step's provider ref against the offered set (draft time): a non-blank
    ref outside the set warns + clears (falls back to the step/default provider). ''
    means 'inherit the default' and is never flagged."""
    s = _as_str(raw)
    if not s:
        return ""
    m = _match_provider(s, providers)
    if m is None:
        warnings.append(
            f"Step {idx + 1} ({label}): {what} {s!r} is not an available provider -- "
            "left as the default."
        )
        return ""
    return m


_WF_LEVEL_DIFF = ("name", "customer_id", "notes_for_reviewer")


_STEP_DIFF_FIELDS = (
    "name",
    "step_type",
    "provider",
    "model",
    "fallback_provider",
    "fallback_model",
    "prompt_text",
    "needs_review",
    "assignee_email",
    "glossary_id",
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
    "run_condition",
    "key",
    "branch",
    "max_visits",
)


# --- draft repair ("Fix this for me") ----------------------------------------
# One LLM pass that takes the CURRENT (possibly hand-edited) review-form state + the
# exact confirm-path validation errors and returns the MINIMAL corrected draft, re-
# validated server-side with the SAME validation the confirm path uses (atomically,
# nothing saved). The corrected draft is DIFFED against the current state so the review
# screen can badge exactly what changed. Same run_completion budget/spend/escalation
# seam as the author path (via _author_json_with_repair), tagged task_origin=
# "draft_repair" in the Prompt-Tap capture. Each pass makes UP TO 2 provider calls (one
# JSON-parse retry); the web does ONE pass per click (no auto-loop), the CLI --auto-fix
# bounds its own N-pass loop.
def _coerce_int(v, default):
    """A whole number from a form scalar / JSON number, else `default`. int() is guarded
    (not just isdigit-gated): str.isdigit() is True for a Unicode digit int() rejects
    (U+00B2 -> ValueError), so the untrusted draft payload must never 500 here."""
    s = str(v if v is not None else "").strip()
    try:
        return int(s)
    except (TypeError, ValueError):
        pass
    try:
        return int(float(s))
    except (TypeError, ValueError, OverflowError):  # OverflowError: "1e999" -> inf (GitHub #502)
        return default


def _norm_struct(v):
    """Canonicalize a draft field value for CHANGE detection so cosmetic differences
    never read as a repair edit: None and '' collapse to None; a numeric string / int /
    float all collapse to the same number ('70' == 70 == 70.0); nested dicts/lists
    (run_condition, branch) normalize recursively with sorted keys. Booleans stay
    booleans (a bool is never coerced to a number)."""
    if isinstance(v, bool):
        return v
    if isinstance(v, dict):
        return {k: _norm_struct(v[k]) for k in sorted(v)}
    if isinstance(v, list):
        return [_norm_struct(x) for x in v]
    if isinstance(v, (int, float)):
        f = float(v)
        return int(f) if f.is_integer() else f
    if v is None:
        return None
    s = str(v).strip()
    if s == "":
        return None
    try:
        f = float(s)
        return int(f) if f.is_integer() else f
    except ValueError:
        return s


# QE auto-fix (Train B) defaults for a step field ABSENT from the current state.
_STEP_DIFF_ABSENT_DEFAULTS = {
    "qe_autofix": False,
    "qe_autofix_max_rounds": 3,
    "qe_autofix_cross_check": False,
}


def _diff_drafts(old: dict, new: dict) -> tuple[set, list]:
    """Diff the CURRENT form state (`old`) against the repaired draft (`new`). Returns
    (workflow_changed, per_step_changed): a set of workflow-level field names that
    changed, and a list (indexed to `new`'s steps) of the set of changed field names per
    step. Steps match by INDEX; a step present in `new` beyond `old`'s length is a
    freshly-ADDED step (e.g. the model inserted a missing branch-target step) and every
    meaningful field it carries is marked so the whole step reads as repaired.

    Caveat (F3, accepted): matching by index over-badges if the model REORDERS steps (a
    shifted step reads as "changed"). This is cosmetic-only (no data effect), and the
    repair prompt explicitly forbids re-ordering, so it is left as-is."""
    wf_changed = set()
    for f in _WF_LEVEL_DIFF:
        if _norm_struct(old.get(f)) != _norm_struct(new.get(f)):
            wf_changed.add(f)
    old_steps = old.get("steps") or []
    new_steps = new.get("steps") or []
    per_step: list[set] = []
    for i, ns in enumerate(new_steps):
        changed: set = set()
        if i >= len(old_steps):
            # A newly-added step: mark step_type (always present) + every non-empty field.
            changed.add("step_type")
            for f in _STEP_DIFF_FIELDS:
                if _norm_struct(ns.get(f)) not in (None, False):
                    changed.add(f)
        else:
            os_ = old_steps[i]
            for f in _STEP_DIFF_FIELDS:
                # A field the current state never carried (a pre-auto-fix payload or an
                # older client) reads as its default, so it is never badged "repaired".
                cur = os_.get(f, _STEP_DIFF_ABSENT_DEFAULTS.get(f))
                if _norm_struct(cur) != _norm_struct(ns.get(f)):
                    changed.add(f)
        per_step.append(changed)
    return wf_changed, per_step


_STEP_MERGE_KEYS = (
    ("name", "name"),
    ("step_type", "step_type"),
    ("provider", "provider"),
    ("model", "model"),
    ("fallback_provider", "fallback_provider"),
    ("fallback_model", "fallback_model"),
    ("prompt_text", "prompt_text"),
    ("needs_review", "needs_review"),
    ("assignee_email", "assignee_email"),
    ("glossary_id", "glossary"),
    ("qe_gate", "qe_gate"),
    ("qe_threshold", "qe_threshold"),
    ("qe_provider", "qe_provider"),
    ("qe_autofix", "qe_autofix"),
    ("qe_autofix_provider", "qe_autofix_provider"),
    ("qe_autofix_max_rounds", "qe_autofix_max_rounds"),
    ("qe_autofix_cross_check", "qe_autofix_cross_check"),
    ("lqa_threshold", "lqa_threshold"),
    ("tm_examples", "tm_examples"),
    ("tm_examples_window", "tm_examples_window"),
    ("run_condition", "run_condition"),
    ("key", "key"),
    ("branch", "branch"),
    ("max_visits", "max_visits"),
)


# (field-in-fields-shape, key-in-the-JSON-contract) pairs for merge-back. Every pair is
# identical except glossary_id <- the contract's name-based "glossary" key.
def _meaningful(v) -> bool:
    """A field carries a real value worth preserving. '' and None collapse to nothing;
    a False bool / 0 count as meaningful (an explicit off/zero the user may have set)."""
    return _norm_struct(v) is not None


def _error_step_indices(errors) -> set:
    """The 0-based indices of steps named in "Step N:" validation errors (the current-
    state errors the model was asked to fix), so merge-back knows which steps the model is
    actively repairing vs merely echoing."""
    import re as _re

    idxs = set()
    for e in errors:
        m = _re.match(r"\s*Step (\d+)\b", str(e))
        if m:
            idxs.add(int(m.group(1)) - 1)
    return idxs


def _removed_step_notes(current: dict, new_fields: dict) -> list:
    """F2: honest surfacing when the repair returned FEWER steps than the current draft.
    Reports each dropped step (by name where the current step was named, else by position),
    falling back to a plain count when name-matching under-counts (unnamed/renamed steps).
    No new validation rule -- just a visible notice the web + CLI surface."""
    cur_steps = current.get("steps") or []
    new_steps = new_fields.get("steps") or []
    if len(new_steps) >= len(cur_steps):
        return []
    new_names = {_as_str(s.get("name")) for s in new_steps if _as_str(s.get("name"))}
    dropped = [
        f"step {i + 1}: {_as_str(s.get('name'))}"
        for i, s in enumerate(cur_steps)
        if _as_str(s.get("name")) and _as_str(s.get("name")) not in new_names
    ]
    delta = len(cur_steps) - len(new_steps)
    if len(dropped) != delta:
        # Name-matching couldn't pin every drop (unnamed/renamed) -> an honest count.
        return [
            f"Repair removed {delta} step(s) (returned {len(new_steps)} of "
            f"{len(cur_steps)}); review the steps below."
        ]
    return ["Repair removed " + "; ".join(dropped) + "."]


def _serialize_workflow_draft(fields: dict, cust_name_by_id: dict, gloss_name_by_id: dict) -> dict:
    """Serialize the CURRENT review-form state into the SAME JSON contract (_WF_SCHEMA)
    the author flow's model produces + _validate_workflow_draft parses, so the repair
    round-trip (serialize -> model -> re-validate) can never drift on shape. Customer +
    per-step glossary are emitted by NAME (the contract is name-based; validation re-
    resolves name -> id), the same round-trip the author->confirm flow already uses.
    Accepts either the untrusted parsed payload or a validated `fields` dict (both carry
    the read keys). The contract carries no language pair: a workflow's languages come
    from the project it runs on, never from the draft."""
    cust_id = ids.coerce_id(fields.get("customer_id"))
    steps_out = []
    for st in fields.get("steps") or []:
        stype = _as_str(st.get("step_type")).lower().replace("-", "_") or "llm_agent"
        gid = ids.coerce_id(st.get("glossary_id"))
        steps_out.append(
            {
                "name": _as_str(st.get("name")),
                "step_type": stype,
                "provider": _as_str(st.get("provider")) or None,
                "model": _as_str(st.get("model")) or None,
                "fallback_provider": _as_str(st.get("fallback_provider")) or None,
                "fallback_model": _as_str(st.get("fallback_model")) or None,
                "prompt_text": _as_str(st.get("prompt_text")) or None,
                "needs_review": _as_bool(st.get("needs_review")),
                "assignee_email": _as_str(st.get("assignee_email")) or None,
                "glossary": gloss_name_by_id.get(gid) if gid is not None else None,
                "qe_gate": _as_bool(st.get("qe_gate")),
                "qe_threshold": st.get("qe_threshold", 70),
                "qe_provider": _as_str(st.get("qe_provider")) or None,
                "qe_autofix": _as_bool(st.get("qe_autofix")),
                "qe_autofix_provider": _as_str(st.get("qe_autofix_provider")) or None,
                "qe_autofix_max_rounds": st.get("qe_autofix_max_rounds", 3),
                "qe_autofix_cross_check": _as_bool(st.get("qe_autofix_cross_check")),
                "lqa_threshold": st.get("lqa_threshold"),
                "tm_examples": _as_bool(st.get("tm_examples")),
                "tm_examples_window": _coerce_int(st.get("tm_examples_window"), 3),
                "run_condition": st.get("run_condition") or None,
                "key": _as_str(st.get("key")) or None,
                "branch": st.get("branch") or None,
                "max_visits": (
                    _coerce_int(st.get("max_visits"), None)
                    if str(st.get("max_visits") or "").strip()
                    else None
                ),
            }
        )
    return {
        "name": _as_str(fields.get("name")),
        "customer": cust_name_by_id.get(cust_id) if cust_id is not None else None,
        "steps": steps_out,
        "notes_for_reviewer": _as_str(fields.get("notes_for_reviewer")) or None,
    }


def _cleanup_partial_workflow(session, workflow_id: int) -> None:
    """Fail-closed compensation for apply_workflow_draft: after a mid-write fault,
    delete the base workflow, its steps, and any multi-pair family it had started
    (the group + every sibling variant + their steps), so a fault never leaves a
    partial workflow behind. Best-effort and swallow-safe -- the caller re-raises the
    ORIGINAL failure, so a cleanup hiccup must never mask it."""
    from sqlmodel import select

    from app.models import Workflow, WorkflowGroup, WorkflowStep

    try:  # noqa: SIM105
        session.rollback()  # clear the failed op's uncommitted state
    except Exception:  # noqa: S110 -- caller re-raises the ORIGINAL failure; must not mask it
        pass
    try:
        base = session.get(Workflow, workflow_id)
        gid = getattr(base, "group_id", None) if base is not None else None
        wf_ids = {workflow_id}
        if gid is not None:
            # A multi-pair confirm folds the base into a NEW group and clones the
            # variants into it; drop every workflow in that group.
            for w in session.exec(select(Workflow).where(Workflow.group_id == gid)).all():
                wf_ids.add(w.id)
        # Delete in strict child->parent order under no_autoflush so a half-issued
        # DELETE never trips the WorkflowStep->Workflow FK: ALL steps first (flush),
        # then all workflows (flush), then the group. Interleaving these lets
        # autoflush try to drop a workflow while its steps are still pending.
        with session.no_autoflush:
            fam_steps = []
            for wid in wf_ids:
                fam_steps.extend(
                    session.exec(select(WorkflowStep).where(WorkflowStep.workflow_id == wid)).all()
                )
            # SPEC-workflows-redesign 4b: every clone now carries a live adapted_from_id
            # (a SELF-referential WorkflowStep FK) — a variant step points at its base
            # step, and BOTH sit inside this family. NULL any provenance link pointing at
            # a step we're about to delete BEFORE deleting, or the bulk DELETE trips the
            # self-FK (mirrors delete_step's provenance handling). Covers an external
            # derived step too (it survives, losing only the dangling link).
            fam_ids = {st.id for st in fam_steps if st.id is not None}
            if fam_ids:
                for dep in session.exec(
                    select(WorkflowStep).where(WorkflowStep.adapted_from_id.in_(fam_ids))
                ).all():
                    dep.adapted_from_id = None
                    session.add(dep)
                session.flush()
            for st in fam_steps:
                session.delete(st)
            session.flush()
            for wid in wf_ids:
                w = session.get(Workflow, wid)
                if w is not None:
                    session.delete(w)
            session.flush()
            if gid is not None:
                grp = session.get(WorkflowGroup, gid)
                if grp is not None:
                    session.delete(grp)
        session.commit()
    except Exception:
        try:  # noqa: SIM105
            session.rollback()
        except Exception:  # noqa: S110 -- cleanup of a partial workflow; caller re-raises original
            pass


# --- helpers -----------------------------------------------------------------
def _as_bool(v) -> bool:
    """Truthiness for a JSON scalar OR an HTML form value ('1'/'true'/'on'/'yes')."""
    if isinstance(v, bool):
        return v
    if isinstance(v, (int, float)):
        return bool(v)
    if isinstance(v, str):
        return v.strip().lower() in ("1", "true", "yes", "on", "y", "checked")
    return False


def _as_str(v) -> str:
    """A trimmed string for any JSON scalar; '' for null/empty. Never raises."""
    if v is None:
        return ""
    if isinstance(v, str):
        return v.strip()
    return str(v).strip()


def _match_by_name(raw: str, rows):
    """Exact-then-case-insensitive name match against catalog rows (each with a
    .name), the same resolution the manual pickers imply. None when unresolved."""
    raw = (raw or "").strip()
    if not raw:
        return None
    for r in rows:
        if (getattr(r, "name", None) or "") == raw:
            return r
    low = raw.lower()
    for r in rows:
        if (getattr(r, "name", None) or "").lower() == low:
            return r
    return None


def _match_provider(raw: str, providers) -> str | None:
    """Exact-then-case-insensitive match of a provider ref against the offered set
    (built-ins + engine:<id> refs). Returns the canonical offered value, or None."""
    raw = (raw or "").strip()
    if not raw:
        return None
    if raw in providers:
        return raw
    low = raw.lower()
    for p in providers:
        if p.lower() == low:
            return p
    return None
