"""quality: shared helpers, second pass (layered pure move)

Documented in: docs/architecture/quality-lqa-evaluations.md
"""

from __future__ import annotations

from app import config
from app.services import refusal, scaffold
from app.services.quality_common import (
    LENGTH_RUNAWAY_REASON,
    META_COMMENTARY_REASON,
)


def is_refusal_check(text: str | None) -> bool:
    """The kill-switch-gated refusal predicate the per-step orchestrator guards use.

    `config.REFUSAL_GUARD_ENABLED and refusal.is_refusal(text)` — centralised so the
    per-step C1 checks (which decide whether to FREEZE a step's output before it can
    feed forward / seed the feedback buffer) share ONE definition with the write-back
    commit below. Does NOT touch the segment; the caller acts on the boolean."""
    return bool(config.REFUSAL_GUARD_ENABLED) and refusal.is_refusal(text)


def meta_commentary_flag(reason: str, source: str | None = None) -> dict:
    """One qa_flags entry for a target carrying model meta-commentary.

    DELIBERATELY the EXISTING `refusal.NONTRANSLATION_FLAG_TYPE` family (see the section
    header): identical {type,severity,message} shape and family to the language-mismatch
    quarantine, so every surface that already renders that chip — the editor grid's
    `_SURFACED` set, `GET /api/documents/{id}/segments?detail=segments`, `doc show
    --detail` and its `review_summary_line` breakdown — renders this one with no change.
    The `reason` field is what distinguishes the two verdicts."""
    entry = {
        "type": refusal.NONTRANSLATION_FLAG_TYPE,
        "severity": "major",
        "message": "Routed to review: " + reason,
        "reason": reason,
    }
    if source:
        entry["source"] = source
    return entry


def clear_meta_commentary_flag(existing: list | None) -> list | None:
    """qa_flags with the META-COMMENTARY verdict removed, and NOTHING else.

    The commentary finding rides the shared `nontranslation` family, so a blanket clear
    of that type would also erase the LANGUAGE-mismatch verdict — a different check, run
    under a different opt-in (`lang_guard`), which this call has no standing to overturn.
    Only an entry whose `reason` is the commentary one is dropped. Returns None when
    nothing is left (byte-identity with an untouched column)."""
    out = [
        f
        for f in (existing or [])
        if not (
            isinstance(f, dict)
            and f.get("type") == refusal.NONTRANSLATION_FLAG_TYPE
            and str(f.get("reason") or "").startswith(META_COMMENTARY_REASON)
        )
    ]
    return out or None


def length_runaway_flag(reason: str, source: str | None = None) -> dict:
    """One qa_flags entry for a target that ran away in length.

    DELIBERATELY the EXISTING `refusal.NONTRANSLATION_FLAG_TYPE` family (see the section
    header) with its own `reason` — identical {type,severity,message} shape to the
    language-mismatch and meta-commentary verdicts, so every surface that already renders
    that chip renders this one with no change, and the orchestrator's existing
    same-family NEEDS_REVIEW re-assertion covers it."""
    entry = {
        "type": refusal.NONTRANSLATION_FLAG_TYPE,
        "severity": "major",
        "message": "Routed to review: " + reason,
        "reason": reason,
    }
    if source:
        entry["source"] = source
    return entry


def clear_length_runaway_flag(existing: list | None) -> list | None:
    """qa_flags with the LENGTH-RUNAWAY verdict removed, and NOTHING else.

    Same scoping rule as `clear_meta_commentary_flag`: the finding rides the shared
    `nontranslation` family, so a blanket clear of that type would also erase the
    LANGUAGE-mismatch and META-COMMENTARY verdicts, which are other checks' and which
    this call has no standing to overturn. Only an entry whose `reason` is the length one
    is dropped. Returns None when nothing is left (byte-identity with an untouched
    column)."""
    out = [
        f
        for f in (existing or [])
        if not (
            isinstance(f, dict)
            and f.get("type") == refusal.NONTRANSLATION_FLAG_TYPE
            and str(f.get("reason") or "").startswith(LENGTH_RUNAWAY_REASON)
        )
    ]
    return out or None


SCAFFOLD_ECHO_REASON = scaffold.SCAFFOLD_ECHO_REASON


# --- deterministic SCAFFOLD-ECHO gate (core-QA campaign Q10, K4-class) --------
# Live prod evidence (doc09.docx = doc 83,
# es-ES): stored segment TARGETS contained our OWN prompt scaffolding, echoed back by a weak
# local model and persisted as content. Segment 0/12 targets were the fenced STYLE-GUIDE
# block (`<style_guide source="Core QA es" trust="data-only">Guía de estilo …`); segment 11
# stored `… MQM LQA encuentra para resolver:` — the auto-injected LQA-findings preamble
# echoed and half-translated — as CLEAN `mt`. The length-runaway gate abstained (the echo was
# not disproportionately long). So a NEW deterministic perimeter is needed at this seam.
#
# THE MARKERS ARE SINGLE-SOURCED. `app.services.scaffold` defines the fence tokens
# (`trust="data-only"`, the `<style_guide>` / `<translation_context>` / `<lqa_findings>` tag
# pairs) and the LQA-findings label ("MQM LQA findings to resolve"); the prompt BUILDERS
# (style_guides, context_pkg, lqa, orchestrator) construct their fences FROM those tokens and
# this gate detects the SAME tokens, so a marker can never drift from what a prompt injects.
# `scaffold.scaffold_echo` abstains per marker when the SOURCE also carries it (so a source
# that genuinely discusses `</style_guide>` or "MQM LQA" is not mistaken for a leak — and the
# offline MockProvider's echo-of-SOURCE cannot fire; only echo-of-PROMPT does).
#
# SEVERITY — treated like the language-mismatch / meta-commentary / length verdicts: SOFT and
# the SAME `refusal.NONTRANSLATION_FLAG_TYPE` family with its own reason. The text is
# COMMITTED (a reviewer must see the leaked structure that was produced), routed to
# NEEDS_REVIEW, and forced TM-INELIGIBLE + connector-withheld — a scaffold echo that seeds
# shared TM would return as a 100% match that BYPASSES this guard forever. It is the STRONGEST
# member of the family (the most specific verdict: we know exactly what the output is), so it
# is checked FIRST and the language/meta/length verdicts are subordinate to it.
#
# WHY SOFT AND NOT WITHHELD (the option weighed). A refusal WITHHOLDS the write (never stores
# target_text); a scaffold echo is arguably worse (leaked internal structure, nothing for a
# reviewer to salvage), which argues for withholding too. It is kept SOFT because the marker
# set is NOT uniformly zero-false-positive: the fence tokens are, but the "MQM LQA" invariant
# (required to catch segment 11's TRANSLATED preamble echo, whose only surviving scaffold
# token is that bigram) has a narrow real false-positive class — a document about MQM/LQA
# tooling. Withholding on a false positive would SILENTLY DROP a real translation, a strictly
# worse failure than committing-and-quarantining a leak that TM + connector egress already
# refuse. SOFT makes a false positive cost one review flag, never lost content — the same risk
# posture the length-runaway gate documents. The abstain-when-in-source rule spares the
# obvious legitimate case. KEY REQUIREMENT met either way: a scaffold echo can NEVER be a
# clean stored `mt` (segment 11's exact defect).
def scaffold_echo_flag(reason: str, source: str | None = None) -> dict:
    """One qa_flags entry for a target that echoed our own prompt scaffolding.

    DELIBERATELY the EXISTING `refusal.NONTRANSLATION_FLAG_TYPE` family (see the section
    header) with its own `reason` — identical {type,severity,message} shape to the
    language-mismatch / meta-commentary / length verdicts, so every surface that renders that
    chip renders this one with no change, and the orchestrator's existing same-family
    NEEDS_REVIEW re-assertion covers it with no write-back edit."""
    entry = {
        "type": refusal.NONTRANSLATION_FLAG_TYPE,
        "severity": "major",
        "message": "Routed to review: " + reason,
        "reason": reason,
    }
    if source:
        entry["source"] = source
    return entry


def clear_scaffold_echo_flag(existing: list | None) -> list | None:
    """qa_flags with the SCAFFOLD-ECHO verdict removed, and NOTHING else.

    Same scoping rule as `clear_length_runaway_flag`: the finding rides the shared
    `nontranslation` family, so a blanket clear of that type would also erase the LANGUAGE,
    META and LENGTH verdicts, which are other checks' and which this call has no standing to
    overturn. Only an entry whose `reason` is the scaffold one is dropped. Returns None when
    nothing is left (byte-identity with an untouched column)."""
    out = [
        f
        for f in (existing or [])
        if not (
            isinstance(f, dict)
            and f.get("type") == refusal.NONTRANSLATION_FLAG_TYPE
            and str(f.get("reason") or "").startswith(SCAFFOLD_ECHO_REASON)
        )
    ]
    return out or None


def clear_over_translation_flag(existing: list | None) -> list | None:
    """qa_flags with the OVER-TRANSLATION withhold verdict removed, and NOTHING else.

    run-190 LOW-5 (gate round 3). The over-translation entry rides the shared
    `nontranslation` family, and the family had no clean-commit clear for it (only the
    scaffold sub-family did), so a segment first WITHHELD as a page dump kept its
    "over-translation: ... the target was withheld" reason even after a LATER clean run
    committed a good target -- a stale review reason the delivered text no longer earns,
    surfaced in the editor grid, `doc show --detail`, the segment API and the review
    summary's `nontranslation` bucket. Same scoping rule as `clear_scaffold_echo_flag`:
    only an entry whose `reason` starts with the over-translation prefix is dropped, so the
    LANGUAGE / META / LENGTH verdicts (other checks, same family) are never touched. Returns
    None when nothing is left (byte-identity with an untouched column)."""
    out = [
        f
        for f in (existing or [])
        if not (
            isinstance(f, dict)
            and f.get("type") == refusal.NONTRANSLATION_FLAG_TYPE
            and str(f.get("reason") or "").startswith(
                # #683: the engine-output-limit withhold rides the same channel and is just
                # as stale once a later run commits a translation.
                (refusal.OVER_TRANSLATION_REASON_PREFIX, refusal.ENGINE_LIMIT_REASON_PREFIX)
            )
        )
    ]
    return out or None
