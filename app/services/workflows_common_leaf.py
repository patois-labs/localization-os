"""workflows_common: shared leaf helpers (layered pure move)

Documented in: docs/architecture/workflow-engine.md
"""

import logging
import re as _re

from app.models import (
    StepType,
)
from app.services import ids

log = logging.getLogger("loc_tms.workflows")


class WorkflowError(ValueError):
    """A workflow-family rename/delete was invalid: a blank name, an unknown id, or a
    delete refused while the entity is still referenced / in-flight. Surfaced by the
    web route / CLI / API as a clean error, never a 500 (mirrors ContentTypeError).

    `refs` carries the live reference tally for a blocked delete (None for a plain
    validation error) so a caller can render the itemized counts."""

    def __init__(self, message: str, refs: dict | None = None):
        super().__init__(message)
        self.refs = refs


def _clamp_window(n) -> int:
    """Bound a step's adaptive_feedback_window to a sane int (shared with the
    engine so a stored/CLI value and its runtime interpretation agree)."""
    from app.services import adaptive

    return adaptive.clamp_window(n)


def _clamp_threshold(n) -> int:
    """Bound a step's qe_threshold to 0-100 (shared with the engine via qe.py)."""
    from app.services import qe

    return qe.clamp_threshold(n)


def _require_whole_number(value, label, low, high, default):
    """Validate new writes only. Decimal forms (including 70.0) are refused."""
    if value is None or (isinstance(value, str) and not value.strip()):
        return default
    if type(value) is int or (
        isinstance(value, str) and _re.fullmatch(r"[+-]?[0-9]+", value.strip())
    ):
        number = int(value)
        if low <= number <= high:
            return number
    raise WorkflowError(
        f"{label} must be a whole number from {low} to {high} (or blank for {default})."
    )


def _coerce_glossary_cap(v, default: int) -> int:
    """Terminology T3: coerce a per-step glossary cap (concept cap / token cap) to a
    non-negative int, falling back to `default` on None/blank/junk. NOT-NULL columns, so
    this never returns None (contrast the tm_match nullable caps)."""
    if v is None:
        return default
    try:
        n = int(str(v).strip())
    except (TypeError, ValueError):
        return default
    return n if n >= 0 else default


def _clamp_lqa_threshold(n):
    """Bound an lqa step's configurable pass threshold to 0-100, or None to keep the
    built-in default (shared with the engine via lqa.py). None/blank/junk -> None
    (the step inherits lqa.LQA_PASS_THRESHOLD = 90, byte-identical to the old gate);
    a real value is clamped to the sane band."""
    from app.services import lqa

    return lqa.clamp_threshold(n)


_TM_PROTECT_TRUE = ("1", "true", "yes", "on", "protect")


_TM_PROTECT_FALSE = ("0", "false", "no", "off", "unprotect")


# The tristate values a caller may supply for WorkflowStep.tm_protect (LQA safety floor
# D1). Every surface — the web <select>, the CLI --tm-protect flag, /api — funnels
# through _coerce_tm_protect, so "auto"/""/None all mean the SAME inherit-the-default
# state on all three and no surface can invent a fourth value.
def _coerce_tm_protect(v):
    """Normalise a caller-supplied tm_protect to the TRISTATE None/True/False.

    None, "" and "auto" -> None (inherit the correction-loop-shaped default: protect
    `tm_origin` segments only on a fix step). A real bool passes through. A string is
    matched against the small allowlist above; ANYTHING unrecognised falls back to None
    (inherit) rather than guessing — the conservative direction, since None reproduces
    pre-Phase-1 behaviour on any workflow without an LQA step."""
    if v is None or isinstance(v, bool):
        return v
    s = str(v).strip().lower()
    if s in _TM_PROTECT_TRUE:
        return True
    if s in _TM_PROTECT_FALSE:
        return False
    return None


_MEMORY_MODE_OFF = ("0", "false", "no", "off")


# LLMemory step-level opt-out (PROPOSAL-llmemory §6 "WorkflowStep.memory_mode tristate
# (auto | off)", models.py ~1010). Mirrors _coerce_tm_protect's shape exactly (every
# surface funnels through one normaliser so a fourth spelling can never sneak in), but
# the tristate here is NULL/"auto" (participate normally) vs "off" (never inject) --
# there is no third "always on" value because memory_mode can only ever say NO to an
# already-eligible step, never turn recall on for an ineligible one.
def _coerce_memory_mode(v):
    """Normalise a caller-supplied memory_mode to None (participate normally) or
    "off" (opt out). None, "", "auto" and anything unrecognised -> None (the
    conservative, byte-identical-to-pre-LLMemory direction); a recognised
    off-spelling -> "off".

    A real bool is answered EXPLICITLY (re-review F10) rather than via `str(v).lower()`:
    the string route happens to give the same two answers today (False -> "off",
    True -> None), but it gives them by accident of Python's repr, and the sibling
    `_coerce_tm_protect` short-circuits bools on purpose. Stating it here means a future
    edit to `_MEMORY_MODE_OFF` cannot silently flip what a bool means."""
    if v is None:
        return None
    if isinstance(v, bool):
        return None if v else "off"
    s = str(v).strip().lower()
    if s in _MEMORY_MODE_OFF:
        return "off"
    return None


# --- tm_match step per-step config coercers (Batch 15 T1, SPEC-tm-match-step-config
# §5.1 / §8.A) -----------------------------------------------------------------------
# The SIX nullable-inherit overrides funnel through these normalisers so every surface
# (web tri-state select, /api, CLI flag) agrees: None/""/"auto"/"inherit" -> None
# (inherit the live global), a concrete value -> the per-step override. NULL is the
# stored "inherit" state; a garbage value coerces to None (inherit) rather than guessing.
def _coerce_tm_scalar_int(v):
    """A nullable int override (tm_fill_threshold / tm_context_floor). None/blank/junk ->
    None (inherit); a parseable int passes through UNCLAMPED here (the engine's
    resolve_policy_for_step clamps at read -- [50,100] for the threshold, [50,99] for the
    floor -- so a stored value is always interpreted safely)."""
    if v is None:
        return None
    if isinstance(v, str) and not v.strip():
        return None
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def _coerce_tm_fuzzy_lock_mode(v):
    """A nullable fuzzy-lock-mode override. None/""/"auto"/"inherit"/junk -> None
    (inherit the global); a recognised mode ("none"|"soft"|"policy") passes through."""
    if v is None:
        return None
    s = str(v).strip().lower()
    if s in ("", "auto", "inherit"):
        return None
    from app.services import tm_lock

    return s if s in tm_lock.FUZZY_LOCK_MODES else None


def _coerce_tm_pool_selection(v):
    """A nullable JSON list of readable project ids (tm_pool_selection). None/""/"auto" ->
    None (all readable pools -- the dynamic default, NEVER snapshotted, S2). A list or a
    comma/space-separated string parses to a de-duplicated list of ints (order preserved).
    An EMPTY list stays [] (a deliberate "select nothing" -> matches nothing at run time,
    distinct from None) so the NULL-vs-[] distinction is preserved (LAW-TM-9, spec §7.2).

    M4: the web text field cannot type an empty list, so the sentinel string ``(none)``
    round-trips a deliberate empty selection through a web full-POST (the builder renders
    ``[]`` as ``(none)``); blank stays None (inherit/all-readable). So a web edit of an
    UNRELATED field never silently flips a []-selection back to all-readable.

    N3: each id is validated through ``ids.coerce_id`` (a POSITIVE, in-range row id, or
    None -- the project_sources._coerce_id precedent), so a zero/negative/out-of-range id
    is DROPPED at authoring time rather than stored. TOLERANT (never raises): the
    seam-level ``_require_valid_tm_pool_selection`` is what REFUSES an all-invalid input."""
    if v is None:
        return None
    if isinstance(v, str):
        s = v.strip()
        if s == "" or s.lower() in ("auto", "inherit"):
            return None
        if s.lower() in ("(none)", "none"):
            return []  # a deliberate empty selection (matches nothing), NOT inherit
        parts = [p.strip() for p in s.replace(",", " ").split()]
    elif isinstance(v, (list, tuple)):
        parts = list(v)
    else:
        return None
    out: list[int] = []
    seen: set[int] = set()
    for p in parts:
        n = ids.coerce_id(p)  # N3: positive, in-range row id, or None (drops 0/negative)
        if n is not None and n not in seen:
            seen.add(n)
            out.append(n)
    return out


_RC_REFS = ("prev_lqa", "prev_qe")


_RC_OPS = ("passed", "failed", "score_gte", "score_lt")


_RC_SCORE_OPS = ("score_gte", "score_lt")


_RC_STEP_PREFIX = "step:"


# --- conditional-workflow PRE-STEP GUARD validation (SPEC §5, Phase 2) --------
# The single save-time validate-and-sanitize seam for a WorkflowStep.run_condition,
# reused by add_step (web + CLI) and the promptable-setup apply path. Fail CLOSED at
# save time (never at run time): a malformed/unresolvable guard is REJECTED here so a
# run can never hit one. Structured allowlists — NEVER eval'd (context_pkg ethos).
# SPEC-parallel-routing §5.5: ONE new ref FORM (not a new expression language) --
# `step:<key>` = "the most recent outcome of the step carrying this key". It exists
# because two LQA members of one parallel group make a bare `prev_lqa` ambiguous TO A
# HUMAN even though _latest_outcome_steprun would resolve it deterministically (highest
# id = the highest-order member). The short forms stay legal wherever they are
# unambiguous -> zero migration. This reuses `key` addressing (condwf D1) rather than
# inventing a second naming scheme; it is exactly the reserved namespace
# SPEC-conditional-workflows §5.1 anticipated ("step:<ref>").
def _is_step_ref(ref) -> bool:
    """True for the `step:<key>` ref FORM (any key, valid or not -- shape only)."""
    return isinstance(ref, str) and ref.startswith(_RC_STEP_PREFIX)


def _step_attr(s, name, default=None):
    """Read a step attribute whether `s` is a WorkflowStep (add_step / clone) or a plain
    step dict (the promptable-setup apply path re-validates its clean_steps dicts)."""
    if isinstance(s, dict):
        return s.get(name, default)
    return getattr(s, name, default)


_BRANCH_SLOTS = ("on_pass", "on_fail")


_BRANCH_SENTINELS = ("next", "end")


# --- conditional-workflow BRANCHING + loop-back validation (SPEC §5, Phase 3) -
# Save-time validate-and-sanitize for a step's stable `key` (D1) + post-step `branch`
# (D3 gate rule). Fail CLOSED at save time (like Phase 2): a malformed/unresolvable
# graph is REJECTED here so a run can never hit one. Structured allowlists, NEVER
# eval'd. Keys are UNIQUE within a workflow; branch targets name a step by its key.
def slugify_key(s) -> str:
    """Normalize a human-typed step key into a slug: lowercase, whitespace collapsed to
    '_', only [a-z0-9_-] kept, trimmed of leading/trailing separators. Returns '' when
    the input is blank or slugifies to nothing (the caller decides if that is an error)."""
    s = (s or "").strip().lower()
    s = _re.sub(r"\s+", "_", s)
    s = _re.sub(r"[^a-z0-9_-]", "", s)
    return s.strip("_-")


def _step_produces_gate(step_type, qe_gate) -> bool:
    """A step produces a durable gate a branch can read iff it is an LQA step or a
    qe_gate=True step (D3). A branch on any other step type is a save-time error."""
    return step_type == StepType.LQA or bool(qe_gate)


# --- ERROR IDENTITY (the key the before/after diff compares on) ------------------
# Every graph error is emitted as a 4-tuple (step_id, label, message, IDENTITY). The
# first three are for DISPLAY; the fourth is what the before/after diff keys on, and it
# exists because THE MESSAGE IS NOT A STABLE KEY.
#
# The diff used to key on (step_id, message), which silently assumed both are invariant
# under an edit that did not cause the error. Reordering broke that assumption in two
# reproduced ways (review R1, HIGH):
#
#   * MESSAGE-UNSTABLE. The ambiguity refusal's message ends with a worked example --
#     "(e.g. step:lane_a:failed)" -- naming the FIRST keyed candidate. Reordering the
#     lanes inside a group (explicitly allowed, SPEC-workflow-canvas 4.1: lane order
#     only affects display and join persistence order) flips which candidate is first,
#     so the PRE-EXISTING error's text changed and read as NEW -- refusing a legal move
#     over a downstream guard the move did not touch.
#   * STEP-ID-UNSTABLE. A duplicate key is reported against whichever occurrence is seen
#     SECOND. Moving the later occurrence in front of the earlier one hands the error to
#     the other step id, so a pre-existing duplicate read as new breakage.
#
# The identity is therefore (step_id, rule_code, offending_ref) -- built from values a
# reorder cannot change -- and is SYMMETRIC (no step_id at all) for the two rules whose
# attribution is inherently order-dependent: a split group names the SLUG, a duplicate
# key names the KEY. Display messages are untouched; only the diff key changed.
def _new_graph_errors(before_rows, after_rows) -> list:
    """THE ONE PLACE the before/after diff is computed. Returns the (label, message)
    pairs present in `after_rows` whose IDENTITY was not already present in
    `before_rows` -- i.e. the breakage this edit introduces, ignoring whatever the graph
    was already tolerating.

    Every mutation seam (add/edit/delete/move, and the per-variant propagation check)
    routes through here, so the identity rule cannot drift between them -- which is
    exactly how the reorder-specific instability below stayed invisible in four of the
    five sites while breaking the fifth."""
    before = {ident for _sid, _lbl, _msg, ident in before_rows}
    return [(lbl, msg) for _sid, lbl, msg, ident in after_rows if ident not in before]


_RULE_GROUP_SPLIT = "group_split"


_RULE_GROUP_MEMBER_TYPE = "group_member_type"


_RULE_GROUP_MULTI_BRANCH = "group_multi_branch"


_RULE_GROUP_REENTRY = "group_reentry_non_first"


_RULE_DUP_KEY = "dup_key"


_RULE_GUARD = "guard"


_RULE_BRANCH = "branch"


def _rc_ref_of(rc) -> str:
    """The ref a run_condition names, as a plain string -- the identity's offending_ref
    for a guard error. Reordering never rewrites a step's own condition, so this is
    stable across a move while the REASON the guard fails to resolve (unresolved vs
    ambiguous) may legitimately change. Both are 'this guard does not resolve': a step
    already carrying a broken guard is not newly broken because it broke differently."""
    if isinstance(rc, dict):
        return str(rc.get("ref") or "")
    return str(rc or "")


def _branch_ref_of(branch) -> str:
    """A canonical, order-independent rendering of a step's branch -- the identity's
    offending_ref for a branch error. A reorder never rewrites a branch, so this pins the
    error to the branch that caused it; an EDIT that changes the branch changes the
    identity, so genuinely new breakage is still caught."""
    if isinstance(branch, dict):
        return repr(sorted((str(k), repr(v)) for k, v in branch.items()))
    return repr(branch)


_LQA_DEAD_FIELDS = (
    "prompt_text",
    "prompt_template_id",
    "chunk_policy",
    "glossary_id",
    "adaptive_feedback",
    "doc_context_prepass",
    "tm_examples",
    "use_image_context",
    "concept_glossary_render",
    "glossary_concept_cap",
    "glossary_token_cap",
    "qe_gate",
    "qe_threshold",
    "qe_provider",
    # QE auto-fix (Train B, review gate L1): live only behind an llm_agent QE gate, so
    # dead here; a type switch force-clears them so a hidden post-editor pin can never
    # block deleting its engine.
    "qe_autofix",
    "qe_autofix_provider",
    "qe_autofix_max_rounds",
    "qe_autofix_cross_check",
    "tm_protect",
    "over_translation_guard",
    "memory_mode",
)


_HUMAN_REVIEW_DEAD_FIELDS = (
    "provider",
    "model",
    "fallback_provider",
    "fallback_model",
    "prompt_text",
    "prompt_template_id",
    "chunk_policy",
    "redact_pii",
    "glossary_id",
    "adaptive_feedback",
    "doc_context_prepass",
    "tm_examples",
    "use_image_context",
    "concept_glossary_render",
    "glossary_concept_cap",
    "glossary_token_cap",
    "qe_gate",
    "qe_threshold",
    "qe_provider",
    "qe_autofix",
    "qe_autofix_provider",
    "qe_autofix_max_rounds",
    "qe_autofix_cross_check",
    "tm_protect",
    "over_translation_guard",
    "memory_mode",
    "lqa_threshold",
    "parallel_group",
)


_LLM_AGENT_DEAD_FIELDS = ("parallel_group", "lqa_threshold")


_EXTERNAL_DEAD_FIELDS = _HUMAN_REVIEW_DEAD_FIELDS


_VENDOR_DEAD_FIELDS = ("vendor_id", "external_wait_hours")


_TM_MATCH_CONFIG_FIELDS = (
    "tm_fill_threshold",
    "tm_fuzzy_lock_mode",
    "tm_lock_ice",
    "tm_hundreds_enter_lqa",
    "tm_context_floor",
    "tm_pool_selection",
)


_LQA_DEAD_FIELDS = _LQA_DEAD_FIELDS + _VENDOR_DEAD_FIELDS + _TM_MATCH_CONFIG_FIELDS


_TM_MATCH_DEAD_FIELDS = _HUMAN_REVIEW_DEAD_FIELDS + _VENDOR_DEAD_FIELDS


_HUMAN_REVIEW_DEAD_FIELDS = (
    _HUMAN_REVIEW_DEAD_FIELDS + _VENDOR_DEAD_FIELDS + _TM_MATCH_CONFIG_FIELDS
)


_LLM_AGENT_DEAD_FIELDS = _LLM_AGENT_DEAD_FIELDS + _VENDOR_DEAD_FIELDS + _TM_MATCH_CONFIG_FIELDS


_EXTERNAL_DEAD_FIELDS = _EXTERNAL_DEAD_FIELDS + _TM_MATCH_CONFIG_FIELDS


DEAD_FIELDS_BY_TYPE = {
    StepType.LQA: _LQA_DEAD_FIELDS,
    StepType.HUMAN_REVIEW: _HUMAN_REVIEW_DEAD_FIELDS,
    StepType.LLM_AGENT: _LLM_AGENT_DEAD_FIELDS,
    StepType.TM_MATCH: _TM_MATCH_DEAD_FIELDS,
    StepType.EXTERNAL: _EXTERNAL_DEAD_FIELDS,
}


# --- honest step-type options (SPEC-lqa-step-honest-options) -----------------
# Which WorkflowStep fields have NO effect for a given step_type -- verified by
# reading the orchestrator's execution branches (never assumed):
#
#   lqa: orchestrator.py's two LQA branches (~1027-1093 FULL_TEXT, ~1683-1750
#   segmented) call `lqa.score_text(lqa_src, lqa_tgt, src, locale, rulebook,
#   step.provider or config.DEFAULT_PROVIDER, style_guide=_sg_render)` DIRECTLY --
#   never `_spec_for_step` / `prompts.render` / the glossary-matching-per-segment
#   code the llm_agent path uses. Consequences:
#     * prompt_text is NEVER rendered -- lqa.py builds the ENTIRE judge prompt
#       internally from the fixed MQM template + the pair rulebook (+ the style
#       guide, when one resolves).
#     * chunk_policy is only consulted by the llm_agent chunking code AFTER the
#       LQA branch's `continue` -- never reached for an LQA step.
#     * glossary_id / tm_examples(_window) / adaptive_feedback(_window) /
#       doc_context_prepass / qe_gate / qe_threshold / qe_provider are read only
#       by the llm_agent per-segment path (QE only ever runs on an llm_agent
#       step) -- an LQA step's own scoring never consults them. (glossary_id is
#       scanned across ALL steps by `_resolved_glossary_id` to pick the run's
#       PINNED glossary for OTHER steps' matching -- a real but orthogonal
#       run-wide side effect, not a property of the LQA step's own behaviour, so
#       it stays off this step's config surface; pin it on an llm_agent step.)
#   provider / redact_pii / run_condition / branch / max_visits / key remain
#   LIVE for lqa (confirmed in the same branches).
#
#   model / fallback_provider / fallback_model were ALSO dead on lqa as of THIS
#   branch's original finding (lqa._llm_errors used to build its own ProviderSpec
#   with model=config.DEFAULT_MODELS.get(provider) and no fallback target,
#   ignoring step.model entirely). The maintainer's product decision (2026-07-22): FIX the
#   engine rather than hide the knobs -- feat/prompt-package-engine (merges BEFORE
#   this branch) makes the LQA judge honor step.model + the fallback chain via
#   _spec_for_step. So they are back on the KEEP list here, and package_view's
#   "Judge model" fact shows model again -- both honest once that branch lands.
#   Cross-branch dependency: if feat/prompt-package-engine does NOT land first,
#   the judge will (once again) silently ignore an authored model/fallback on an
#   lqa step even though the builder now offers them -- re-verify against
#   lqa.py/orchestrator.py before relying on this list post-merge.
#
#   human_review: makes NO provider call at all (orchestrator.py ~1011-1025 /
#   ~1664-1679 just flips the run to PAUSED) -- every engine/prompt/QE/glossary/
#   context field is dead. `branch` defined ON a human_review step is ALSO dead:
#   resume_workflow (~2264) unconditionally advances
#   `run.current_step_index + 1`, never consulting the step's own branch/gate
#   (and a human_review step never produces one). `key` / `max_visits` DO stay
#   live -- not for the step's own routing, but because ANOTHER step's branch
#   can target this step's key as a bounded loop-back, and RunGuard.at_visit_cap
#   enforces max_visits at THAT jump (_next_cursor) AND, since issue #94, before
#   the step executes at all (_visit_cap_halt), so the ceiling bounds this step's
#   own executions in every role it plays in the graph. assignee_email and
#   run_condition (the uniform pre-step guard, applied identically to every step
#   type) are live too.
# prompt_template_id (SPEC-prompt-template-runtime) is DEAD for lqa/human_review for
# the same reason prompt_text is: an lqa step's judge prompt is built internally (fixed
# MQM template + pair rulebook) and a human_review step makes no model call -- neither
# resolves a bound PromptTemplate. It is refused/hidden exactly like prompt_text there.
# Batch 14 T2 (SPEC §25.5-A): the per-step image-context toggle is llm_agent-only (an
# lqa step scores with a fixed MQM judge and never reaches _run_step_on_segment), so
# it is dead here — same posture + coupling as tm_examples (default-TRUE carve-out).
# Terminology T3 (SPEC §9-T3): the concept-glossary renderer toggle + its two caps are
# llm_agent-only (an lqa step renders no {glossary} block), so they are dead here — same
# posture as use_image_context.
# run-190 Item D / M5: the over-translation (page-dump) guard opt-out is llm_agent-only
# (an lqa step makes no fresh-MT llm_agent call), so it is dead here — same posture as
# tm_protect (a NULLABLE tristate default-inherit carve-out).
# Batch 14 T2 (SPEC §25.5-A): llm_agent-only, dead on human_review/tm_match/external.
# Terminology T3 (SPEC §9-T3): llm_agent-only concept-glossary renderer toggle + caps,
# dead on human_review/tm_match/external (they render no {glossary} block).
# run-190 Item D / M5: llm_agent-only over-translation guard opt-out — dead on
# human_review / tm_match / external (they make no fresh-MT llm_agent call).
# feat/lqa-configurable-threshold: the LQA pass bar is live ONLY on an lqa step
# (a human_review step makes no scoring call). This entry propagates to tm_match +
# external too, which alias this tuple below.
# SPEC-parallel-routing §5.6: DEAD on human_review. A Phase-1 group forks LQA
# scoring against a frozen snapshot and joins; a human_review step's whole
# behaviour is to PAUSE the run and hand the operator the assembled blob, which
# has no meaning inside a fork (resume_workflow's blind cursor+1 would also step
# out of the middle of a group). Lanes with a human in them are Phase 2 (§8).
# SPEC-parallel-routing §5.6, the third entry in this table -- and the FIRST field that
# is dead for llm_agent. Phase 1 lanes are CHECK-ONLY: a group member only READS the
# working text, which is what makes the "segments are written exactly once, post-loop,
# on success" choke point (quality.commit_mt_output) safe with no merge semantics at
# all. A text-PRODUCING member would need one -- two lanes both rewriting the working
# text has no defined answer -- so an llm_agent member is REFUSED here rather than
# accepted-and-ignored: silently clearing it would change the execution shape the
# author asked for, which is precisely the "silent truncation" this repo forbids.
# PROPOSAL-tm-matching-workflow-step (§7): a tm_match step is ORCHESTRATION-ONLY (D8) --
# it makes no provider call and carries no prompt, exactly like human_review -- so the
# same field set is dead. (Its only real config, "which attached TMs participate", is
# v1-fixed to all-in-precedence-order, so there is no live field to add yet.)
# EXTERNAL (PROPOSAL-external-processing-step Wave A): the dispatch-and-wait step makes
# NO provider call (like human_review / tm_match), so every engine/prompt/QE/glossary
# field is dead -- BUT `vendor_id` + `external_wait_hours` are its LIVE config. So its dead
# set is exactly the human_review LLM/prompt set, WITHOUT the two vendor fields. This alias
# is taken BEFORE the vendor fields are appended below (tuple + makes a fresh object, so the
# alias keeps the vendor-free tuple and external never inherits its own live fields).
# Vendor config is dead on every NON-external step (a forged `vendor_id` on an llm_agent /
# lqa / human_review / tm_match step is refused, exactly like a stray glossary pin).
# Batch 15 T1 (SPEC-tm-match-step-config §8.A, the load-bearing DE-ALIAS): the six
# per-step tm_match config columns are LIVE ONLY on a tm_match step (dead on every other
# type). The INVERSE of how use_image_context was ADDED to dead sets: here the fields must
# be REMOVED from tm_match's dead set (which used to alias human_review's) and ADDED to
# the other four types' dead sets. So capture the human_review-shaped dead set BEFORE the
# tm-config fields are appended, alias tm_match to THAT (its own tuple, WITHOUT the six),
# and append the six to LQA / HUMAN_REVIEW / LLM_AGENT / EXTERNAL.
# tm_match aliases the human_review dead set + vendor (engine/prompt/QE all dead, like
# human_review), but NOT the six tm-config fields (its OWN live config). Captured here,
# BEFORE the six are appended to _HUMAN_REVIEW_DEAD_FIELDS below.
# EXTERNAL keeps the vendor-FREE human_review set (vendor is its LIVE config) + the six
# tm-config fields as dead (a forged tm_fill_threshold on an external step is refused).
class _FieldLabels(dict):
    """A dead-field -> display-label map whose ``.get()`` rebrands registry DEFAULT coined
    names to the install's LIVE names (L8), so a workflow-diff label like the memory
    opt-out follows an admin rename. Every consumer reaches it through ``.get(f, f)`` (all
    7 checked), so one seam covers the CLI, web and setup-author surfaces. Defaults-only
    substitution; no-op on an unchanged install.

    NOTE: the seam is ONLY on ``.get()``. ``d[key]`` / ``.items()`` / ``.values()`` bypass
    it and would serve the raw default — no live consumer does that. Any coined label MUST
    be stored as its exact registry DEFAULT (not an import-time f-string, which would freeze
    to a non-default on an already-renamed boot and never be repaired) — MED-3."""

    def get(self, key, default=None):
        val = super().get(key, default)
        if not isinstance(val, str):
            return val
        from app import branding

        for k, d in branding.REGISTRY.items():
            live = branding.branded(k)
            if d != live and d in val:
                val = val.replace(d, live)
        return val


# --- prompt-template BIND-TIME validation (SPEC-prompt-template-runtime) -----
def _coerce_pt_id(v):
    """'' / None / malformed -> None; an int or digit-string in range -> int. Shared by
    the step-binding seams and the prompt-template CRUD. Routes through the app-wide id
    law (app/services/ids.coerce_id) so a request-supplied Unicode digit or >2**63 value
    is None, never a raw ValueError/OverflowError 500 at int()/the SQLite bind."""
    return ids.coerce_id(v)


_MAX_WAIT_HOURS = 100_000


# M6: the external-step wait override is a duration in HOURS, not an id. Bound it well
# inside SQLite's signed-32-bit INTEGER range (and any sane wait) so a hostile 10**30
# value can never reach the column and raise a raw OverflowError -> 500. ~11 years of
# hours is far past any real bounded wait.
def _coerce_vendor_id(v):
    """The external step's vendor selector, bounded + non-raising (M6): '' / None / a
    malformed / out-of-range / non-positive value -> None; a positive in-range id -> int.
    Delegates to the shared ids.coerce_id law (which caps at SQLite's id range) so a huge
    digit string can never overflow session.get()."""
    from app.services import ids

    return ids.coerce_id(v)
