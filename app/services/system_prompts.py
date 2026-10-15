"""system_prompts: catalogue of the built-in LLM SYSTEM prompts (GitHub #356 phase 1) and the
org-level (phase 2a) and per-workflow (phase 2b) edit seams for two of their heads.

The workflow pipeline wraps every LLM call in text that no user wrote: the step system
message, the machine-translation system message, the tag-repair prompts, the quality judges.
Until now that text was only visible in Python. This module lists it, READ-ONLY, for the
Building blocks page, `GET /api/system-prompts`, and `loc workflow prompt system`.

SINGLE SOURCE OF TRUTH. Nothing here is a copy. Every entry's `text` is computed by calling
the same code the pipeline calls (`_system_for`, `translation_prompt`, `_repair_prompt`, ...) or
reading the same constant (`qe._JUDGE_SYSTEM`, `lqa._JUDGE_SYSTEM`, ...). Edit the constant and
this catalogue follows. Entries whose real text depends on a per-call value (the language pair,
or a segment) are rendered with a stated EXAMPLE value, and say so in `note`.

PHASE 2a (editable heads). With the `system_prompt_overrides` feature flag ON, an org admin
may override the EDITABLE HEAD of two prompts (`step_head`, `mt_head`). `editable_prompts`
describes them (built-in default, the org's current override, the locked floor shown as
"always added"); `save_override` / `reset_override` / `restore_override` write through
`system_prompt_overrides`, which owns validation, versioning and the audit trail. The
catalogue entries stay the BUILT-IN text; `effective_text` shows what the org's runs send.
Everything else here stays read-only. Overrides are org data, so every function that touches
them takes the caller's `org_id` explicitly. PHASE 2b adds the same heads per WORKFLOW
(`workflow_*` functions, `save_workflow_override` ...): a workflow's own override beats the
org's, "inherit" falls through to the org then the built-in text.

SCOPE, DELIBERATE. Only the pipeline prompts that already go to an engine on every run. The
product assistant (`assist`, `setup_author`, `SYSTEM_CONTRACT`) stays internal and is NOT listed.
The catalogue carries no org data, no key, and no run content; it is identical for every caller.

Documented in: docs/architecture/workflow-engine.md
"""

from app.services import lqa, qe, qe_jev_loop, tag_llm_repair
from app.services import system_prompt_overrides as _ovr
from app.services.orchestrator import _SINGLE_UNIT_INSTRUCTION, _system_for
from app.services.orchestrator_terminology_backen import (
    _SYSTEM_HEAD,
    _SYSTEM_HEAD_BODY,
    _SYSTEM_IDENTITY,
)
from app.services.prompts import (
    CONTENT_FENCE_INSTRUCTION,
    MEMORY_FENCE_INSTRUCTION,
    MT_LOCKED_CONTENT_RULE,
    MT_SYSTEM_BODY_DEFAULT,
    MT_SYSTEM_FLOOR_SUFFIX,
    mt_system_prefix,
    mt_system_text,
    translation_prompt,
)

OverrideError = _ovr.OverrideError
EDITABLE_KEYS = _ovr.EDITABLE_KEYS
OFF_MESSAGE = _ovr.OFF_MESSAGE
WARN_NO_OUTPUT_ONLY = _ovr.WARN_NO_OUTPUT_ONLY
# #365 gate fix: changing a group's default can change the head its pair variants inherit.
group_default_change_needs_admin = _ovr.group_default_change_needs_admin
GROUP_DEFAULT_ADMIN_ERROR = _ovr.GROUP_DEFAULT_ADMIN_ERROR

# Example values used where the real text is composed per call. Fixed and boring on purpose.
_EXAMPLE_SRC = "en-US"
_EXAMPLE_TGT = "de-DE"
_EXAMPLE_NOTE = (
    "Shown for an en-US to de-DE call; the language names change with each call's language pair."
)

_RAW_MARKER_SAMPLE = "⟦r1⟧text⟦/r1⟧"


def _entry(key: str, name: str, used_in: str, text: str, *, note: str = "") -> dict:
    return {"key": key, "name": name, "used_in": used_in, "text": text, "note": note}


def list_system_prompts(session=None, org_id=None) -> list[dict]:
    """Every built-in pipeline system prompt, in display order. Each dict has `key`, `name`,
    `used_in` (one plain sentence), `text` (exactly what the code sends), `note` ("" or a
    caveat such as the example language pair), `effective_text`, `override_version` and
    `editable_key`. Called with no session it is pure and cheap: no DB, no network, no engine,
    and `effective_text` equals `text`.

    With a `session`, and the overrides flag ON, the four entries an override reaches (the
    three step variants and the MT message) carry in `effective_text` what THIS org's runs
    send: the override's body composed into the same locked floor (`override_version` names
    it, 0 = none in force). `text` always stays the built-in."""
    rows = _builtin_entries()
    step_ovr = mt_ovr = None
    if session is not None:
        step_ovr = _ovr.resolve_step_head(session, org_id)
        mt_ovr = _ovr.resolve_mt_head(session, org_id)
    for row in rows:
        row["effective_text"] = row["text"]
        row["override_version"] = 0
        row["editable_key"] = ""
        if row["key"].startswith("step_system_"):
            row["editable_key"] = _ovr.KEY_STEP_HEAD
            if step_ovr is not None:
                row["effective_text"] = _step_variant(row["key"], step_ovr.body)
                row["override_version"] = step_ovr.version
        elif row["key"] == "mt_translation_system":
            row["editable_key"] = _ovr.KEY_MT_HEAD
            if mt_ovr is not None:
                row["effective_text"] = mt_system_text(_EXAMPLE_SRC, _EXAMPLE_TGT, mt_ovr.body)
                row["override_version"] = mt_ovr.version
    return rows


def _step_variant(key: str, head_body: str | None) -> str:
    """The step system message variant `key` composed with an optional head override."""
    kw = {"src_locale": _EXAMPLE_SRC, "tgt_locale": _EXAMPLE_TGT, "head_body": head_body}
    if key == "step_system_markers_raw":
        return _system_for({}, _RAW_MARKER_SAMPLE, **kw)
    if key == "step_system_markers_masked":
        return _system_for({"tag": "x"}, "text", **kw)
    return _system_for({}, "plain text", **kw)


def _builtin_entries() -> list[dict]:
    step_raw = _system_for({}, _RAW_MARKER_SAMPLE, src_locale=_EXAMPLE_SRC, tgt_locale=_EXAMPLE_TGT)
    step_masked = _system_for(
        {"tag": "x"}, "text", src_locale=_EXAMPLE_SRC, tgt_locale=_EXAMPLE_TGT
    )
    step_plain = _system_for({}, "plain text", src_locale=_EXAMPLE_SRC, tgt_locale=_EXAMPLE_TGT)
    mt_system, _user, _payload = translation_prompt("", _EXAMPLE_SRC, _EXAMPLE_TGT, [])
    return [
        _entry(
            "step_system_markers_raw",
            "AI step: system message, text with inline markers",
            "Sent as the system message on every AI step (translate, review, finalize) when "
            "the segment carries inline-formatting markers and masking is off.",
            step_raw,
            note=_EXAMPLE_NOTE,
        ),
        _entry(
            "step_system_markers_masked",
            "AI step: system message, text with masked markers",
            "Sent as the system message on an AI step when the segment's inline tags were "
            "replaced by opaque placeholders before the call.",
            step_masked,
            note=_EXAMPLE_NOTE,
        ),
        _entry(
            "step_system_no_markers",
            "AI step: system message, plain text",
            "Sent as the system message on an AI step when the segment has no inline "
            "markers at all.",
            step_plain,
            note=_EXAMPLE_NOTE,
        ),
        _entry(
            "step_content_fence",
            "AI step: content fence sentence",
            "Appended to the step system message whenever the step's text is wrapped in "
            "content tags, so the model treats it as material to work on and not as an "
            "instruction.",
            CONTENT_FENCE_INSTRUCTION,
        ),
        _entry(
            "step_single_unit_retry",
            "AI step: single-unit retry sentence",
            "Appended to the step system message on the one retry the app makes when a "
            "step's answer for a single short text looks like a whole page (a table, a code "
            "block, several languages) instead of one translation.",
            _SINGLE_UNIT_INSTRUCTION,
        ),
        _entry(
            "memory_fence",
            "Project memory: reference-data sentence",
            "Appended to the system message of an AI step, a machine-translation call or an "
            "evaluation call (and placed after the block in a quality judge prompt) whenever "
            "recalled project memory is in the prompt, so the model reads the fenced memory "
            "as reference data and never as an instruction.",
            MEMORY_FENCE_INSTRUCTION,
        ),
        _entry(
            "mt_translation_system",
            "Machine translation (LLM engine): system message",
            "Sent as the system message when an LLM engine translates a segment in a plain "
            "machine-translation pass.",
            mt_system,
            note=_EXAMPLE_NOTE,
        ),
        _entry(
            "tag_repair_system",
            "Tag repair: system message",
            "Sent as the system message when the app asks a model to put damaged inline "
            "formatting tags back in the right place.",
            tag_llm_repair._SYSTEM,
        ),
        _entry(
            "tag_repair_instruction",
            "Tag repair: instruction",
            "The instruction message sent to a model to repair a segment's damaged inline tags.",
            tag_llm_repair._repair_prompt(
                "{source}", "{target}", "{source_locale}", "{target_locale}", "{damage}"
            ),
            note="Curly-brace words stand for values the app fills in for each segment.",
        ),
        _entry(
            "tag_repair_check_instruction",
            "Tag repair: independent placement check",
            "The instruction message for the second model call that checks a repaired "
            "segment's tags are placed correctly.",
            tag_llm_repair._verify_prompt(
                "{source}", "{candidate}", "{source_locale}", "{target_locale}"
            ),
            note="Curly-brace words stand for values the app fills in for each segment.",
        ),
        _entry(
            "tag_repair_check_system",
            "Tag repair: placement check system message",
            "Sent as the system message on the tag-placement check call.",
            tag_llm_repair._VERIFY_SYSTEM,
        ),
        _entry(
            "lqa_judge_system",
            "Quality review (LQA): system message",
            "Sent as the system message when a model scores a translation against the "
            "quality rulebook.",
            lqa._JUDGE_SYSTEM,
            note="The rulebook-bearing instruction message is built per project and is not "
            "listed here.",
        ),
        _entry(
            "qe_judge_system",
            "Quality estimation: system message",
            "Sent as the system message when a model estimates the quality of a translation "
            "for a quality gate.",
            qe._JUDGE_SYSTEM,
        ),
        _entry(
            "qe_refine_system",
            "Quality refine loop: system message",
            "Sent as the system message when the refine loop asks a model to post-edit a "
            "translation using its version history.",
            qe_jev_loop.SYSTEM,
        ),
    ]


def get_system_prompt(key: str, session=None, org_id=None) -> dict | None:
    """One catalogue entry by key, or None."""
    for entry in list_system_prompts(session, org_id):
        if entry["key"] == key:
            return entry
    return None


# --- phase 2a: the editable heads -------------------------------------------------------
_EDITABLE_META = {
    _ovr.KEY_STEP_HEAD: {
        "name": "AI step: system message head",
        "used_in": (
            "The instruction part of the system message on every AI step (translate, review, "
            "finalize). The app always adds the lines shown as locked, around your text."
        ),
        "also_added": [
            "step_system_markers_raw",
            "step_system_markers_masked",
            "step_system_no_markers",
            "step_content_fence",
            "step_single_unit_retry",
            "memory_fence",
        ],
    },
    _ovr.KEY_MT_HEAD: {
        "name": "Machine translation: system message body",
        "used_in": (
            "The behaviour part of the system message when an LLM engine translates a segment "
            "in a plain machine-translation pass. The language pair before it and the "
            "placeholder and marker rules after it are always added by the app."
        ),
        "also_added": ["mt_translation_system", "memory_fence"],
    },
}


def _default_body(key: str) -> str:
    return _SYSTEM_HEAD_BODY.strip() if key == _ovr.KEY_STEP_HEAD else MT_SYSTEM_BODY_DEFAULT


def _floor_parts(key: str) -> tuple[str, str]:
    """(locked text before the editable body, locked text after it), for display."""
    if key == _ovr.KEY_STEP_HEAD:
        plain = _system_for({}, "plain text", src_locale=_EXAMPLE_SRC, tgt_locale=_EXAMPLE_TGT)
        return _SYSTEM_IDENTITY.strip(), plain[len(_SYSTEM_HEAD) :].strip()
    return (
        mt_system_prefix(_EXAMPLE_SRC, _EXAMPLE_TGT).rstrip(),
        MT_LOCKED_CONTENT_RULE + " " + MT_SYSTEM_FLOOR_SUFFIX,
    )


def overrides_enabled() -> bool:
    return _ovr.enabled()


def can_edit_workflow_heads(user) -> bool:
    """Who may change ONE workflow's own system prompt heads (Apply, Restore default prompt,
    restore a version): an admin or a manager (#630, 2026-10-08; the org-level heads stay
    admin only). The ONE predicate the web routes, /api (and so the CLI over --remote and MCP)
    and the builder page's Unlock use. The org and customer ring is checked before it."""
    from app.services import security

    return security.can_manage(user)


def can_edit_step_prompt(user) -> bool:
    """Who may change ONE step's inline prompt (Unlock, Apply, Restore default prompt): an
    admin, a linguist (can_tune_ai) or a manager (2026-10-08, after #630). Narrow on
    purpose: a manager gets the step PROMPT only, never the rest of the step (type, models,
    TM, glossary, QE and the other tuning fields stay can_tune_ai), and can_tune_ai itself is
    NOT granted to managers (it also covers TM, glossary and AI setup). The ONE predicate the
    web prompt routes, /api edit-step (a prompt-only body) and the builder's Unlock use. The
    org and customer ring is checked as before."""
    from app.services import security

    return security.can_manage(user) or security.can_tune_ai(user)


def can_set_group_default(user) -> bool:
    """Who may make an existing member the default workflow of its group: admin, linguist
    (can_tune_ai) or manager. Adding, moving and removing group members
    stays can_tune_ai."""
    from app.services import security

    return security.can_manage(user) or security.can_tune_ai(user)


def can_change_group_default_heads(user) -> bool:
    """Who may make a group change that alters the system prompt head a workflow inherits from
    its group's default (#365, #386): an admin or a manager (2026-10-08; was admin
    only). A linguist is still refused when a head is involved."""
    from app.services import security

    return security.can_manage(user)


def can_read_workflow_heads(user) -> bool:
    """Who may read one workflow's heads: whoever may edit them, plus can_tune_ai (linguists)."""
    from app.services import security

    return security.can_manage(user) or security.can_tune_ai(user)


def mt_head_body(session, org_id, workflow_id=None) -> str | None:
    """The MT system-message body override in force (the workflow's own when `workflow_id` is
    given and has one, else the org's), or None for the built-in text (always None while the
    flag is off). For callers that build a plain-MT prompt outside a project (the
    /api/translate/text front door)."""
    ovr = _ovr.resolve_mt_head(session, org_id, workflow_id)
    return ovr.body if ovr is not None else None


def _editable_row(session, org_id, key: str, with_history: bool = False) -> dict:
    meta = _EDITABLE_META[key]
    before, after = _floor_parts(key)
    cur = _ovr.current(session, org_id, key)
    row = {
        "key": key,
        "name": meta["name"],
        "used_in": meta["used_in"],
        "default_body": _default_body(key),
        "floor_before": before,
        "floor_after": after,
        "floor_note": _EXAMPLE_NOTE
        + " The marker sentence is chosen per call; the three variants are listed above.",
        "also_added": list(meta["also_added"]),
        "max_chars": _ovr.MAX_BODY_CHARS,
        "override": _ovr.row_dict(cur) if cur is not None else None,
        "effective_body": cur.body if cur is not None and _ovr.enabled() else _default_body(key),
    }
    if with_history:
        row["history"] = _ovr.history(session, org_id, key)
    return row


def catalogue_payload(session, org_id) -> dict:
    """The body GET /api/system-prompts returns (minus the caller-specific `can_edit`) and the
    CLI prints: one function so the web page, the API and both CLI arms cannot drift."""
    return {
        "overrides_enabled": _ovr.enabled(),
        "system_prompts": list_system_prompts(session, org_id),
        "editable": editable_prompts(session, org_id),
    }


def editable_prompts(session, org_id) -> list[dict]:
    """The editable heads for the caller's org: built-in default, current override (or None),
    the locked floor, the length limit. Readable with the flag off (the API then reports
    `overrides_enabled` False and the editor stays hidden)."""
    return [_editable_row(session, org_id, k) for k in EDITABLE_KEYS]


def editable_detail(session, org_id, key: str) -> dict:
    """One editable head with its version history (newest first)."""
    return _editable_row(session, org_id, _ovr.check_key(key), with_history=True)


def save_override(session, org_id, key: str, body: str, actor: str) -> dict:
    row, warnings = _ovr.save(session, org_id, key, body, actor)
    return {"version": row.version, "warnings": warnings, **_saved_view(session, org_id, key)}


def restore_override(session, org_id, key: str, version, actor: str) -> dict:
    row, warnings = _ovr.restore(session, org_id, key, version, actor)
    return {"version": row.version, "warnings": warnings, **_saved_view(session, org_id, key)}


def reset_override(session, org_id, key: str, actor: str) -> dict:
    row = _ovr.reset(session, org_id, key, actor)
    return {
        "version": row.version if row is not None else None,
        "changed": row is not None,
        "warnings": [],
        **_saved_view(session, org_id, key),
    }


def _saved_view(session, org_id, key: str) -> dict:
    return {"prompt": _editable_row(session, org_id, key, with_history=True)}


# --- phase 2b: per-workflow overrides ---------------------------------------------------
# The same two editable heads, set on ONE workflow. Resolution at run time is workflow
# override > org override > built-in (see system_prompt_overrides.resolve). The callers have
# already checked that the caller may edit that workflow; `org_id` here only feeds the
# inherited (org-level) view. Readable with the flag off, like the org-level reads.
def _base_name(session, base_id) -> str | None:
    from app.models import Workflow

    wf = session.get(Workflow, base_id) if base_id is not None else None
    return wf.name if wf is not None else None


def _workflow_row(session, org_id, workflow_id: int, key: str, with_history: bool = False) -> dict:
    meta = _EDITABLE_META[key]
    before, after = _floor_parts(key)
    default = _default_body(key)
    wcur = _ovr.wf_current(session, workflow_id, key)
    ocur = _ovr.current(session, org_id, key)
    on = _ovr.enabled()
    # #365: a pair variant that has no text of its own inherits its BASE workflow's text (when
    # the base is usable: not archived, same org, same customer ring) before the org's.
    bcur = None
    base_id = None
    if on:
        base_id = _ovr.base_workflow_id(session, workflow_id)
        if base_id is not None:
            bcur = _ovr.wf_current(session, base_id, key)
            if bcur is not None and not _ovr._workflow_matches_org(session, base_id, org_id):
                bcur = None
    if wcur is not None and on:
        source, effective = _ovr.SCOPE_WORKFLOW, wcur.body
    elif bcur is not None:
        source, effective = _ovr.SCOPE_BASE_WORKFLOW, bcur.body
    elif ocur is not None and on:
        source, effective = _ovr.SCOPE_ORG, ocur.body
    else:
        source, effective = _ovr.SCOPE_BUILTIN, default
    if bcur is not None:
        inherited_source, inherited_body = _ovr.SCOPE_BASE_WORKFLOW, bcur.body
    elif ocur is not None and on:
        inherited_source, inherited_body = _ovr.SCOPE_ORG, ocur.body
    else:
        inherited_source, inherited_body = _ovr.SCOPE_BUILTIN, default
    row = {
        "key": key,
        "workflow_id": int(workflow_id),
        "name": meta["name"],
        "used_in": meta["used_in"],
        "default_body": default,
        "floor_before": before,
        "floor_after": after,
        "floor_note": _EXAMPLE_NOTE
        + " The marker sentence is chosen per call; the three variants are listed above.",
        "max_chars": _ovr.MAX_BODY_CHARS,
        "override": _ovr.row_dict(wcur) if wcur is not None else None,
        "org_override": _ovr.row_dict(ocur) if ocur is not None else None,
        "inherited_source": inherited_source,
        "inherited_body": inherited_body,
        "base_workflow_id": base_id if bcur is not None else None,
        "base_workflow_name": _base_name(session, base_id) if bcur is not None else None,
        "base_override": _ovr.row_dict(bcur) if bcur is not None else None,
        "effective_source": source,
        "effective_body": effective,
    }
    if with_history:
        row["history"] = _ovr.wf_history(session, workflow_id, key)
    return row


def workflow_editable_detail(session, org_id, workflow_id: int, key: str) -> dict:
    """One editable head as it applies to a workflow: its own override (or None = inherit),
    what it would inherit (the org's override or the built-in text), the locked floor, the
    source that wins now, and the workflow's own version history (newest first)."""
    return _workflow_row(session, org_id, workflow_id, _ovr.check_key(key), with_history=True)


def workflow_editable_prompts(session, org_id, workflow_id: int) -> list[dict]:
    """Both editable heads for a workflow (no history), in catalogue order."""
    return [_workflow_row(session, org_id, workflow_id, k) for k in EDITABLE_KEYS]


def workflow_payload(session, org_id, workflow_id: int) -> dict:
    """The body GET /api/workflows/{id}/system-prompts returns and the CLI prints: one function
    so the builder page, the API and both CLI arms cannot drift."""
    return {
        "overrides_enabled": _ovr.enabled(),
        "workflow_id": int(workflow_id),
        "editable": workflow_editable_prompts(session, org_id, workflow_id),
    }


def _workflow_saved_view(session, org_id, workflow_id: int, key: str) -> dict:
    return {"prompt": _workflow_row(session, org_id, workflow_id, key, with_history=True)}


def save_workflow_override(
    session, org_id, workflow_id: int, key: str, body: str, actor: str
) -> dict:
    row, warnings = _ovr.wf_save(session, workflow_id, key, body, actor)
    return {
        "version": row.version,
        "warnings": warnings,
        **_workflow_saved_view(session, org_id, workflow_id, key),
    }


def restore_workflow_override(
    session, org_id, workflow_id: int, key: str, version, actor: str
) -> dict:
    row, warnings = _ovr.wf_restore(session, workflow_id, key, version, actor)
    return {
        "version": row.version,
        "warnings": warnings,
        **_workflow_saved_view(session, org_id, workflow_id, key),
    }


def reset_workflow_override(session, org_id, workflow_id: int, key: str, actor: str) -> dict:
    row = _ovr.wf_reset(session, workflow_id, key, actor)
    return {
        "version": row.version if row is not None else None,
        "changed": row is not None,
        "warnings": [],
        **_workflow_saved_view(session, org_id, workflow_id, key),
    }
