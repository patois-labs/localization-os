"""The single sanctioned CHOKE POINT for landing machine-produced / imported
translation text into a Segment's target (arch-review Finding 2).

Transitional compatibility shim for one release (added 2026-09-15, remove the re-export block after
the next tagged release): `__all__` re-exports is_refusal_check, meta_commentary_flag,
clear_meta_commentary_flag, length_runaway_flag, clear_length_runaway_flag, SCAFFOLD_ECHO_REASON,
scaffold_echo_flag, clear_scaffold_echo_flag, clear_over_translation_flag from
app.services.quality_hub.

Transitional compatibility shim for one release (added 2026-09-14, remove the re-export block after
the next tagged release): `__all__` re-exports DNT_FLAG_TYPE, dnt_flag, merge_dnt_flags,
_term_present, dnt_violations, GLOSSARY_FLAG_TYPE, glossary_flag, merge_glossary_flags, _WORD_RE,
_MIN_STEM, _MIN_APPEND_STEM, _AGREEMENT_SUFFIXES, _STEM_SUFFIXES, _lang_family, _fold_accents,
_word_stem, TAG_FLAG_TYPE, tag_damage, _RESIDUE_EXCERPT_CAP, _residue_excerpt, STEP_ERROR_FLAG_TYPE,
step_error_flag, merge_step_error_flags, clear_step_error_flags, GATE_HOLD_FLAG_TYPE,
GATE_HOLD_CAUSE_NO_PROGRESS, GATE_HOLD_CAUSE_LOOP_EXHAUSTED, GATE_HOLD_CAUSE_VISIT_CAP,
GATE_HOLD_CAUSE_UNREPAIRED, GATE_HOLD_CAUSE_JUDGE_UNAVAILABLE, GATE_HOLD_FAMILY_LQA,
GATE_HOLD_FAMILY_QE, gate_hold_families, gate_hold_flag, ascii_reason, NUMBER_FLAG_TYPE,
number_flag, number_violation, CHAR_LIMIT_FLAG_TYPE, coerce_char_limit, char_count,
segment_char_limit, PLACEHOLDER_FLAG_TYPE, _SEAM_PLACEHOLDER_RE, _PRINTF_POSITION_RE,
_placeholder_multiset, WHITESPACE_FLAG_TYPE, whitespace_violation, META_COMMENTARY_REASON,
_META_PAREN_RE, _META_BRACKET_RE, _META_NOTE_MARKERS, _META_FIRST_PERSON, _META_VOCAB,
_META_FIRST_PERSON_GAPPED, _META_QUOTE_FOLD, _meta_fold, LENGTH_MIN_SURPLUS, LENGTH_MIN_RATIO,
LENGTH_RUNAWAY_REASON, length_runaway, SOURCE_FALLBACK_FLAG_TYPE, source_fallback_flag,
is_hard_locked, CommitOutcome, _inflection_match_word, _term_present_tolerant,
glossary_term_violations, tag_flag, merge_tag_flags, clear_tag_flags, merge_gate_hold_flags,
clear_gate_hold_flags, has_gate_hold_flag, tag_damage_detail, merge_number_flags,
clear_number_flags, char_limit_violation, char_limit_flag, merge_char_limit_flags,
clear_char_limit_flags, placeholder_violation, placeholder_flag, merge_placeholder_flags,
clear_placeholder_flags, nonplaceholder_tag_divergence, whitespace_flag, merge_whitespace_flags,
clear_whitespace_flags, _meta_candidate_spans, _is_meta_span, meta_commentary,
merge_source_fallback_flag, source_fallback_indices from app.services.quality_common,
app.services.quality_part2.

WHY THIS EXISTS
---------------
The refusal guard used to be a CHECKLIST: `refusal.is_refusal()` was called at each
enumerated fresh-MT write-back site, and every NEW writer had to REMEMBER to join
the perimeter. Cluster C (orchestrator laundering, approve-poison, trace leak) and
its residuals (tmx-import ungated, the flow.py title read-gap) are all the same
failure class — checklist perimeters leak. This module inverts the checklist into a
SEAM: `commit_mt_output()` is the ONLY sanctioned way any code path writes
machine-produced or imported text into `Segment.target_text` (and, transitively,
makes it TM-eligible). A dumb grep tripwire (tests/verify_refusal_choke_point.py)
FAILS the build if a `.target_text =` assignment appears outside the small
sanctioned allowlist, so a future connector author cannot silently bypass the seam.

WHAT IS *NOT* ROUTED THROUGH HERE (by design)
--------------------------------------------
  * HUMAN text — the CAT editor save path and `segment_approve`'s human-edited text
    are never run through `is_refusal` (a human deliberately typing "I cannot
    translate this" is CONTENT, not a hedge). Those stay their own writers. Those same
    direct human writers are the ONE way a hard-locked segment may be overwritten -- they
    CLEAR the lock at the save site (tm_lock.clear_lock_on_human_edit); the machine seam
    below always REFUSES a hard-locked write.
  * (HISTORICAL NOTE) TM 100%-match APPLIES used to write target_text DIRECTLY, on the
    theory that a stored target was already vetted. That was a filed defect: a TM entry
    stored BEFORE the write gate existed (a hedge, a length-runaway) reintroduced itself
    on every later document as clean MT, and — once TM locks shipped — a cached apply
    could overwrite a hard lock. ALL THREE TM-apply sites now route THROUGH this seam
    (translate.py's segment + full-text short-circuits, content.apply_tm_to_document),
    with `source="tm", tm_eligible=False`. The TM-WRITE-time gates (import_tmx + the
    historical scrub + approve's C3 guard) remain the FIRST line; this is the second.
  * The QE gate (app/services/qe.py) — it ALSO routes to NEEDS_REVIEW, but on a
    DIFFERENT axis (quality score) and with its own qa_flag namespace ("qe"). The
    seam must NOT double-flag or clobber a QE rationale; it only ever adds/updates
    the "refusal"-type flag (refusal.merge_flag keeps every non-refusal flag).

The seam NEVER rewrites or drops good text; on a suspected refusal it only ever
WITHHOLDS the write and DOWNGRADES the segment to review (mirroring the pre-seam
per-site behavior byte-for-byte for the non-refusal path).

Documented in: docs/architecture/quality-lqa-evaluations.md
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

from sqlmodel import select

from app import config
from app.models import Segment, SegStatus, StringKey
from app.services import ai_tagging, output_guard, qa, qe, refusal, scaffold, tag_repair
from app.services.quality_common import (
    _AGREEMENT_SUFFIXES,
    _META_BRACKET_RE,
    _META_FIRST_PERSON,
    _META_FIRST_PERSON_GAPPED,
    _META_NOTE_MARKERS,
    _META_PAREN_RE,
    _META_QUOTE_FOLD,
    _META_VOCAB,
    _MIN_APPEND_STEM,
    _MIN_STEM,
    _PRINTF_POSITION_RE,
    _RESIDUE_EXCERPT_CAP,
    _SEAM_PLACEHOLDER_RE,
    _STEM_SUFFIXES,
    _WORD_RE,
    CHAR_LIMIT_FLAG_TYPE,
    DNT_FLAG_TYPE,
    GATE_HOLD_CAUSE_JUDGE_UNAVAILABLE,
    GATE_HOLD_CAUSE_LOOP_EXHAUSTED,
    GATE_HOLD_CAUSE_NO_PROGRESS,
    GATE_HOLD_CAUSE_UNREPAIRED,
    GATE_HOLD_CAUSE_VISIT_CAP,
    GATE_HOLD_FAMILY_LQA,
    GATE_HOLD_FAMILY_QE,
    GATE_HOLD_FLAG_TYPE,
    GLOSSARY_FLAG_TYPE,
    LENGTH_MIN_RATIO,
    LENGTH_MIN_SURPLUS,
    LENGTH_RUNAWAY_REASON,
    META_COMMENTARY_REASON,
    NUMBER_FLAG_TYPE,
    PLACEHOLDER_FLAG_TYPE,
    SOURCE_FALLBACK_FLAG_TYPE,
    STEP_ERROR_FLAG_TYPE,
    TAG_FLAG_TYPE,
    WHITESPACE_FLAG_TYPE,
    _fold_accents,
    _lang_family,
    _meta_fold,
    _placeholder_multiset,
    _residue_excerpt,
    _term_present,
    _word_stem,
    ascii_reason,
    char_count,
    clear_step_error_flags,
    coerce_char_limit,
    dnt_flag,
    dnt_violations,
    gate_hold_families,
    gate_hold_flag,
    glossary_flag,
    is_hard_locked,
    length_runaway,
    merge_dnt_flags,
    merge_glossary_flags,
    merge_step_error_flags,
    number_flag,
    number_violation,
    segment_char_limit,
    source_fallback_flag,
    step_error_flag,
    tag_damage,
    whitespace_violation,
)
from app.services.quality_hub import (
    SCAFFOLD_ECHO_REASON,
    clear_length_runaway_flag,
    clear_meta_commentary_flag,
    clear_over_translation_flag,
    clear_scaffold_echo_flag,
    is_refusal_check,
    length_runaway_flag,
    meta_commentary_flag,
    scaffold_echo_flag,
)
from app.services.quality_part2 import (
    CommitOutcome,
    _inflection_match_word,
    _is_meta_span,
    _meta_candidate_spans,
    _term_present_tolerant,
    char_limit_flag,
    char_limit_violation,
    clear_char_limit_flags,
    clear_gate_hold_flags,
    clear_label_echo_flag,
    clear_number_flags,
    clear_placeholder_flags,
    clear_tag_flags,
    clear_whitespace_flags,
    glossary_term_violations,
    has_gate_hold_flag,
    label_echo_stripped_flag,
    merge_char_limit_flags,
    merge_gate_hold_flags,
    merge_label_echo_flag,
    merge_number_flags,
    merge_placeholder_flags,
    merge_source_fallback_flag,
    merge_tag_flags,
    merge_whitespace_flags,
    meta_commentary,
    nonplaceholder_tag_divergence,
    placeholder_flag,
    placeholder_violation,
    run_split_spacing,
    source_fallback_indices,
    tag_damage_detail,
    tag_flag,
    whitespace_flag,
)
from app.services.quality_part3 import (
    REVIEW_HOLD_FLAG_TYPE,
    clear_review_hold_flags,
    merge_review_hold_flags,
    review_hold_flag,
    review_hold_violation,
    segment_fmt,
    segment_source_locale,
)

# #664: the minor note left when an echoed prompt label was stripped (defined in scaffold, the
# leaf that single-sources the labels; re-exported so the surfaces name it through `quality`).
LABEL_ECHO_FLAG_TYPE = scaffold.LABEL_ECHO_FLAG_TYPE

__all__ = [
    "CHAR_LIMIT_FLAG_TYPE",
    "DNT_FLAG_TYPE",
    "GATE_HOLD_CAUSE_JUDGE_UNAVAILABLE",
    "GATE_HOLD_CAUSE_LOOP_EXHAUSTED",
    "GATE_HOLD_CAUSE_NO_PROGRESS",
    "GATE_HOLD_CAUSE_UNREPAIRED",
    "GATE_HOLD_CAUSE_VISIT_CAP",
    "GATE_HOLD_FAMILY_LQA",
    "GATE_HOLD_FAMILY_QE",
    "GATE_HOLD_FLAG_TYPE",
    "GLOSSARY_FLAG_TYPE",
    "LABEL_ECHO_FLAG_TYPE",
    "LENGTH_MIN_RATIO",
    "LENGTH_MIN_SURPLUS",
    "LENGTH_RUNAWAY_REASON",
    "META_COMMENTARY_REASON",
    "NUMBER_FLAG_TYPE",
    "PLACEHOLDER_FLAG_TYPE",
    "REVIEW_HOLD_FLAG_TYPE",
    "SCAFFOLD_ECHO_REASON",
    "SOURCE_FALLBACK_FLAG_TYPE",
    "STEP_ERROR_FLAG_TYPE",
    "TAG_FLAG_TYPE",
    "WHITESPACE_FLAG_TYPE",
    "_AGREEMENT_SUFFIXES",
    "_META_BRACKET_RE",
    "_META_FIRST_PERSON",
    "_META_FIRST_PERSON_GAPPED",
    "_META_NOTE_MARKERS",
    "_META_PAREN_RE",
    "_META_QUOTE_FOLD",
    "_META_VOCAB",
    "_MIN_APPEND_STEM",
    "_MIN_STEM",
    "_PRINTF_POSITION_RE",
    "_RESIDUE_EXCERPT_CAP",
    "_SEAM_PLACEHOLDER_RE",
    "_STEM_SUFFIXES",
    "_WORD_RE",
    "CommitOutcome",
    "StringKey",
    "_fold_accents",
    "_inflection_match_word",
    "_is_meta_span",
    "_lang_family",
    "_meta_candidate_spans",
    "_meta_fold",
    "_placeholder_multiset",
    "_residue_excerpt",
    "_term_present",
    "_term_present_tolerant",
    "_word_stem",
    "ai_tagging",
    "ascii_reason",
    "char_count",
    "char_limit_flag",
    "char_limit_violation",
    "clear_char_limit_flags",
    "clear_gate_hold_flags",
    "clear_label_echo_flag",
    "clear_length_runaway_flag",
    "clear_meta_commentary_flag",
    "clear_number_flags",
    "clear_over_translation_flag",
    "clear_placeholder_flags",
    "clear_review_hold_flags",
    "clear_scaffold_echo_flag",
    "clear_step_error_flags",
    "clear_tag_flags",
    "clear_whitespace_flags",
    "coerce_char_limit",
    "dataclass",
    "dnt_flag",
    "dnt_violations",
    "gate_hold_families",
    "gate_hold_flag",
    "glossary_flag",
    "glossary_term_violations",
    "has_gate_hold_flag",
    "is_hard_locked",
    "is_refusal_check",
    "label_echo_stripped_flag",
    "length_runaway",
    "length_runaway_flag",
    "merge_char_limit_flags",
    "merge_dnt_flags",
    "merge_gate_hold_flags",
    "merge_glossary_flags",
    "merge_label_echo_flag",
    "merge_number_flags",
    "merge_placeholder_flags",
    "merge_review_hold_flags",
    "merge_source_fallback_flag",
    "merge_step_error_flags",
    "merge_tag_flags",
    "merge_whitespace_flags",
    "meta_commentary",
    "meta_commentary_flag",
    "nonplaceholder_tag_divergence",
    "number_flag",
    "number_violation",
    "placeholder_flag",
    "placeholder_violation",
    "qa",
    "re",
    "review_hold_flag",
    "review_hold_violation",
    "scaffold_echo_flag",
    "segment_char_limit",
    "source_fallback_flag",
    "source_fallback_indices",
    "step_error_flag",
    "tag_damage",
    "tag_damage_detail",
    "tag_flag",
    "unicodedata",
    "whitespace_flag",
    "whitespace_violation",
]


def commit_mt_output(
    session,
    segment,
    text,
    *,
    source: str,
    tm_eligible: bool = True,
    pair: tuple | None = None,
    already_refused: bool = False,
    over_translation_flag: dict | None = None,
    output_guard_flag: dict | None = None,
    set_status: bool = True,
    glossary_terms=None,
    lang_guard: bool = False,
    full_text: bool = False,
    llm_tag_repair=None,
    human_edit: bool = False,
) -> CommitOutcome:
    """Land machine-produced / imported translation `text` into `segment.target_text`
    — the ONE sanctioned write path (arch-review Finding 2).

    `source` is a short provenance tag ("mt", "workflow", "tmx-import", ...) recorded
    on the refusal qa_flag when the guard trips, so a reviewer sees WHICH path
    produced the hedge. `pair` (optional source/target locale tuple) is accepted for
    callers that want to record it; it is not persisted here (the segment already
    carries its locale). `already_refused` lets a caller that has ALREADY established
    a refusal by a stronger, position-authoritative signal (the orchestrator per-step
    C1 guard's `refused_seg`/`refused`) force the quarantine WITHOUT re-running the
    detector — preserving those paths' exact quarantine semantics.

    On a refusal (guard ON and `is_refusal(text)`, or `already_refused`): does NOT
    write `target_text`, sets `SegStatus.NEEDS_REVIEW`, merges a refusal qa_flag
    (keeping any QE/other flags), and returns a flagged, TM-ineligible outcome. The
    segment is `session.add`ed but NOT committed (the caller owns the transaction).

    `over_translation_flag` (run-190 Item D / M-1) lets a caller quarantining an
    OVER-TRANSLATION (page-dump) unit hand in an honestly-named `nontranslation`-family
    qa_flag (built by `refusal.over_translation_flag`, naming the shape rule + char counts)
    instead of the default refusal flag. It is used ONLY on the withhold branch above and
    ONLY when `already_refused` is set for a dump; every other withhold (a genuine refusal
    hedge, the C1 per-step guard) passes None and keeps the refusal disposition. The
    quarantine mechanism is identical either way — only the flag family/reason differs.

    `output_guard_flag` (#343) is the same channel for an llm_agent OUTPUT-GUARD withhold
    (`output_guard.check_output`: instruction echo / short-source blow-up / number mismatch):
    the caller sets `already_refused` and hands the entry in, and it is merged by its own
    type. Every other commit clears a stale output-guard entry.

    On a pass (or the kill switch OFF): writes `target_text`, sets `SegStatus.MT`
    (unless `set_status=False` — the orchestrator FULL_TEXT commit left status
    untouched), and returns a clean outcome carrying the caller's `tm_eligible`.
    Provider-specific provenance the caller still wants (mt_confidence=0.9,
    tm_match_pct=100) is applied by the caller AFTER this returns — byte-identical to
    the pre-seam sites.

    `llm_tag_repair` (AI Tagging 1.5c) is the OPTIONAL LLM tag-repair rung: a callable
    `(source_text, target_text) -> tag_llm_repair.RepairResult | None`, injected by
    callers that hold a resolved ProviderSpec + spend/tap context (the orchestrator and
    translate paths). It runs ONLY after the deterministic rung has failed and the
    segment is otherwise headed for quarantine. It is INJECTED rather than imported
    because this function is the sole synchronous write seam and must stay LLM-free,
    zero-spend and hermetic by default: every existing caller passes nothing and is
    byte-identical. A returned candidate has already passed the rung's own structural +
    body-preservation locks, and is re-verified HERE by `tag_damage` before it may be
    written — this seam never trusts an injected repairer's word.

    `lang_guard` (QA-3 layer b) opts THIS call into the INDEPENDENT language-mismatch
    signal (refusal.looks_untranslated): after a clean commit, if the produced target
    reads as decisively English for a non-English target locale, the segment is
    DOWNGRADED to review under a DISTINCT `nontranslation` qa_flag and made TM-ineligible
    (a soft gate, exactly like the DNT/glossary checks — the text is NOT withheld or
    rewritten, only routed to a human and kept out of TM + connector write-back). Default
    False so every existing caller is byte-identical; only the REAL fresh-MT path opts in
    (never the offline MockProvider, whose deterministic echo returns the English source
    and would be a systemic false positive). Governed by the same `REFUSAL_GUARD_ENABLED`
    kill switch as the refusal check, so flipping the guard off restores pre-guard
    behavior here too. This is the second, non-shared detector the QA-3 finding asked for:
    the choke point no longer hinges solely on the curated-phrase `is_refusal`.

    `human_edit` (data-integrity fix, human-work protection) marks this call as a PERSON's
    own edit of the target rather than a machine/import/vendor write, so the status-based
    human-work protection gate below is skipped for it (a human may re-edit their own
    approved/edited content, exactly as the CAT editor allows). It bypasses ONLY that gate,
    never the hard-lock gate. The sole caller is the full-text paragraph editor's
    `_commit_body` (its save/revert routes) -- no machine path passes it. Default False, so
    every machine caller keeps full protection and is byte-identical."""
    # TM-matching HARD-LOCK enforcement (PROPOSAL-tm-matching-workflow-step §5), the
    # FIRST gate: this seam is the sole writer of MACHINE / imported text, so one check
    # here makes an LLM/MT/TM-apply/import overwrite of a hard-locked segment impossible
    # on every surface (web workflows, CLI, /api, MCP). It is REFUSED: the segment's good
    # TM fill + its status/qa_flags are left exactly as they are, the outcome is withheld
    # (committed=False, lock_refused=True). A HUMAN edit does NOT reach this seam -- the
    # CAT editor / key-grid saves are direct writers (human text is content, never gated)
    # and they CLEAR the lock at their own save site (tm_lock.clear_lock_on_human_edit),
    # which is the reviewer+ override, RBAC-gated by that route. Inert when TM_LOCKS_ENABLED
    # is off or the segment is not hard-locked (getattr-guarded) -> byte-identical.
    if is_hard_locked(segment):
        return CommitOutcome(committed=False, flagged=True, tm_eligible=False, lock_refused=True)
    # STATUS-BASED HUMAN-WORK PROTECTION, the SECOND structural gate at this sole write seam
    # and a sibling of the hard-lock refusal above (data-integrity fix). This seam only ever
    # writes MACHINE / imported / vendor text, so a single check here makes it impossible on
    # EVERY surface (workflows, plain pretranslate, CLI, /api, MCP, key import, vendor return)
    # for a later machine pass to silently overwrite a segment a HUMAN has APPROVED or EDITED.
    # It is REFUSED: target_text, status, qa_flags AND the lock columns are left EXACTLY as
    # they are (the human's target already stands -- the seam simply declines to overwrite it),
    # the outcome is withheld (committed=False, flagged=True, status_protected=True). This is
    # CORE, DEFAULT-ON behavior: unlike the hard-lock gate above it is NOT gated by
    # TM_LOCKS_ENABLED, and unlike the refusal / DNT / glossary gates below it is NOT gated by
    # REFUSAL_GUARD_ENABLED -- there is no config flag that turns off protection of human work.
    # NEEDS_REVIEW is NOT protected here: it is machine routing, and a re-run is SUPPOSED to be
    # able to re-translate a flagged segment (the byte-exact probe-5 protection of a
    # NEEDS_REVIEW TM sentinel lives in the orchestrator's candidate selection, not here). The
    # escape hatch for a human who WANTS a re-translation is the CAT editor: clearing the
    # target resets status to NEW (translatable again), and an ordinary edit keeps status
    # EDITED and protects the NEW text. MOST human edits never reach this seam -- the CAT
    # editor and key-grid ORIGIN_HUMAN saves are direct writers (human text is content, never
    # gated). The human writers that DO route through here are (1) the full-text paragraph
    # editor (fulltext_paragraphs._commit_body -- the save/revert routes) and (2) the
    # orchestrator's resume commit for a slot an operator EDITED during a human_review pause.
    # Both borrow the seam for its integrity gates and pass `human_edit=True` so a person can
    # post-edit their OWN approved/edited content (exactly as the CAT editor lets a person
    # re-edit an approved segment). `human_edit` bypasses ONLY this status gate, never the
    # hard-lock gate above (a hard TM lock still refuses, unchanged). No MT / import / vendor
    # MACHINE pass ever passes it. CAVEAT (issue #50, PRE-EXISTING): the paragraph save/revert
    # routes gate on VIEW access, not write access (the whole document-write family, incl.
    # segment_save, does -- an RBAC policy call tracked in #50), so a viewer-scoped docs caller
    # who reaches those routes CAN overwrite an approved body through this carve-out, the SAME
    # exposure #50 already grants for non-approved content. This is a route-RBAC gap, not a
    # machine-egress one; closing it is #50's job, not this seam's.
    #
    # FRESHNESS: `segment` may have been loaded at run start and be STALE, so a human approval
    # that lands mid-run (e.g. during a human_review pause, in another session) would be missed
    # if we trusted the in-memory status alone. We read the CURRENT `status` column with a
    # TARGETED scalar SELECT under `no_autoflush` -- NOT session.refresh(): refresh expires the
    # object then re-loads it, so a load that raises leaves it EXPIRED (the next attribute read
    # re-raises), and it triggers a full session AUTOFLUSH that can write a SIBLING object's
    # unflushed changes as a side effect of a commit on THIS segment. The scalar select touches
    # only this row's status, expires nothing, flushes nothing, and stays a READ (no early
    # write-lock promotion). We then take the MAX-PROTECTION of the in-memory status and the DB
    # status: the DB read catches a mid-run approval another session committed, while the
    # in-memory status catches a caller that STAGED `seg.status = APPROVED` without flushing yet
    # (the DB would still read the old value) -- either one being APPROVED/EDITED protects.
    # BEST-EFFORT: this is a read-then-refuse, not a conditional UPDATE, so it has no atomicity
    # guarantee -- an approval that commits in the narrow window between this read and the
    # write-back flush can still lose. That window is small and the failure mode is a machine
    # overwrite of a just-approved segment (rare, and the human can re-approve); closing it fully
    # would need row-versioned compare-and-set at the write, out of scope for this fix.
    # Guarded: an unpersisted / detached stub (no id, or a closed session) cannot be read, so we
    # fall back to the in-memory status, which is all such a caller has to offer.
    if not human_edit:
        _mem_status = getattr(segment, "status", None)
        _db_status = None
        _seg_id = getattr(segment, "id", None)
        if _seg_id is not None:
            try:
                with session.no_autoflush:
                    _db_status = session.exec(
                        select(Segment.status).where(Segment.id == _seg_id)
                    ).first()
            except Exception:
                _db_status = None
        if _mem_status in (SegStatus.APPROVED, SegStatus.EDITED) or _db_status in (
            SegStatus.APPROVED,
            SegStatus.EDITED,
        ):
            return CommitOutcome(
                committed=False, flagged=True, tm_eligible=False, status_protected=True
            )
    # item 7a: the target BEFORE this commit, so the lock-clear at the end fires ONLY on
    # an actual change (a round-2 fix / human edit that supersedes the TM fill) and never
    # on a no-op re-commit of the SAME text (a later run passing a soft-locked segment's
    # own stored target straight back through the seam must not clear its lock).
    _prev_target = getattr(segment, "target_text", "") or ""
    guard = bool(config.REFUSAL_GUARD_ENABLED)
    # #664 — a model that echoes the prompt's own section label ("Source:") before the
    # translation. Judged FIRST so every later gate sees the clean text: only a label (plus
    # whitespace) before the translation is stripped deterministically and flagged minor; more
    # than that (the source echoed back, a second label) is left as written and routed to review
    # below, riding the scaffold-echo verdict. Abstains when the source starts with the label.
    # Same kill switch as the rest of the perimeter; byte-identical when the output is clean.
    label_stripped = False
    label_review = ""
    if guard and not already_refused:
        _lbl_text, _lbl_verdict = scaffold.label_echo(
            text, segment.source_text, getattr(segment, "target_locale", None)
        )
        if _lbl_verdict == "stripped":
            text, label_stripped = _lbl_text, True
        elif _lbl_verdict == "review":
            label_review = scaffold.LABEL_ECHO_REVIEW_REASON
    if already_refused or (guard and refusal.is_refusal(text)):
        segment.status = SegStatus.NEEDS_REVIEW
        # run-190 Item D / M-1: an over-translation (page-dump) withhold is NOT a model
        # refusal — the caller passes the honestly-named `over_translation_flag` (a
        # `nontranslation`-family entry naming the shape rule + char counts). Every other
        # withhold (a genuine refusal hedge, or the C1 per-step guard's `refused_seg`) keeps
        # the refusal disposition. Only the flag/marker change; the quarantine mechanism is
        # unchanged (target_text not written, NEEDS_REVIEW, TM-ineligible).
        if output_guard_flag is not None and output_guard.withholds(output_guard_flag):
            segment.qa_flags = output_guard.merge_output_guard_flag(
                segment.qa_flags, output_guard_flag
            )
        elif over_translation_flag is not None:
            segment.qa_flags = refusal.merge_nontranslation_flag(
                segment.qa_flags, over_translation_flag
            )
        else:
            segment.qa_flags = refusal.merge_flag(segment.qa_flags, refusal.refusal_flag(source))
        session.add(segment)
        return CommitOutcome(committed=False, flagged=True, tm_eligible=False)
    # #721: a space the source has at a formatting-run split (`before⟦/r1⟧⟦r2⟧ closing`) that
    # the machine text dropped is put back, or the export glues two words ("antes decerrar").
    # Deterministic, machine writes only (a person's own edit is content); unchanged otherwise.
    if not human_edit:
        text = run_split_spacing(segment.source_text, text, getattr(segment, "target_locale", None))
    segment.target_text = text
    if set_status:
        segment.status = SegStatus.MT
    # Deterministic DNT verification (2B): a POST-WRITE gate — the text is already
    # committed above; a DNT miss only DOWNGRADES the segment to review and appends a
    # dnt_violation qa_flag (composing with any refusal/qe/structural flag via
    # merge_dnt_flags — it removes only prior dnt entries). Empty glossary_terms / no
    # violation -> clear the previous deterministic verdict only when terms were supplied.
    # None means no re-check: preserve both terminology families. A caller
    # that re-sets status after this (the orchestrator write-back) honours
    # `outcome.dnt_violations` to re-apply NEEDS_REVIEW, exactly as it honours a QE fail.
    dnt = dnt_violations(segment.source_text, text, glossary_terms)
    if glossary_terms is not None and (
        dnt
        or any(
            f.get("type") == DNT_FLAG_TYPE for f in (segment.qa_flags or []) if isinstance(f, dict)
        )
    ):
        segment.qa_flags = merge_dnt_flags(segment.qa_flags, dnt)
    if dnt:
        segment.status = SegStatus.NEEDS_REVIEW
    # Glossary adherence (Track B / B1): the POSITIVE sibling of the DNT gate above —
    # a POST-WRITE check that every required (non-DNT) target_term whose source_term
    # appeared actually landed. Identical soft semantics: the text is already
    # committed; a miss only DOWNGRADES to review + appends a glossary_violation
    # qa_flag (merge_glossary_flags composes with the dnt/qe/refusal/structural flags,
    # removing only prior glossary entries). tm_eligible is left UNTOUCHED (mirrors
    # DNT: a required-term miss flags for a human, it does not make the text
    # TM-ineligible). No miss clears the prior glossary verdict only.
    # The orchestrator's post-write status reset honours
    # `outcome.glossary_violations` to re-apply NEEDS_REVIEW, exactly as for DNT.
    gloss = glossary_term_violations(
        segment.source_text,
        text,
        glossary_terms,
        target_locale=getattr(segment, "target_locale", None),
    )
    if glossary_terms is not None and (
        gloss
        or any(
            f.get("type") == GLOSSARY_FLAG_TYPE
            for f in (segment.qa_flags or [])
            if isinstance(f, dict)
        )
    ):
        segment.qa_flags = merge_glossary_flags(segment.qa_flags, gloss)
    if gloss:
        segment.status = SegStatus.NEEDS_REVIEW
    # Q10 — the SCAFFOLD-ECHO gate (core-QA campaign; see the section header above). The
    # produced target reproduces our OWN injected prompt scaffolding — a fenced
    # style-guide / context / LQA-findings block, or the "MQM LQA findings to resolve"
    # preamble echoed (even half-translated) by a weak reviewer/post-edit model. Markers are
    # single-sourced in `app.services.scaffold`, matched against the OUTPUT and abstained
    # per-marker when the SOURCE carries the same token (so MockProvider's echo-of-source and
    # a source that legitimately discusses a marker never fire). Like the meta/length guards
    # and UNLIKE `lang_guard` it is NOT opt-in per caller (safe on every path incl. the
    # offline MockProvider) and gated by the same `REFUSAL_GUARD_ENABLED` kill switch. SOFT
    # (text committed, NEEDS_REVIEW, TM-ineligible) but the STRONGEST, most-specific member of
    # the `nontranslation` family — checked FIRST, and the language/meta/length verdicts are
    # SUBORDINATE to it (their `not scaffold_reason` guards below) because "this is our leaked
    # style guide" is a more actionable verdict for a reviewer than "this looks long/English".
    scaffold_reason = scaffold.scaffold_echo(text, segment.source_text) if guard else ""
    # #664: a label followed by more than the translation joins the scaffold verdict (it is a
    # scaffold-echo reason), unless a more specific marker already fired.
    scaffold_reason = scaffold_reason or label_review
    if scaffold_reason:
        segment.qa_flags = refusal.merge_nontranslation_flag(
            segment.qa_flags, scaffold_echo_flag(scaffold_reason, source)
        )
        segment.status = SegStatus.NEEDS_REVIEW
    elif guard:
        # STALE-BADGE CLEARING (see `clear_scaffold_echo_flag`). Scaffold is the top member of
        # the family, so a clean re-commit clears a prior scaffold verdict with no higher gate
        # to defer to. Scoped to the scaffold reason only — the language/meta/length verdicts
        # in the same family belong to the checks below and are never touched here.
        segment.qa_flags = clear_scaffold_echo_flag(segment.qa_flags)
    # LOW-5 (gate round 3): a prior OVER-TRANSLATION withhold verdict is stale once THIS
    # commit produced committable text (the unit was NOT withheld this visit -- we are past
    # the withhold branch). Clear it reason-scoped, beside the scaffold clear, so the editor
    # grid / doc show / segment API / review summary stop showing a withhold reason the
    # delivered text no longer earns. Reason-scoped (a segment with no over-translation entry is
    # unchanged), so it composes with the family and is safe on every path.
    # LOW-1 (gate round 4): UNGATED, unlike the scaffold clear above. The over-translation
    # WITHHOLD is governed by OVER_TRANSLATION_MAX_RATIO ALONE, never REFUSAL_GUARD_ENABLED
    # (round-1 H2), so its CLEAR must be ungated too -- under `if guard:` a stale
    # "over-translation: ... withheld" reason survived FOREVER with REFUSAL_GUARD_ENABLED=0
    # after a clean re-run, the very coupling H2 removed on the withhold side.
    segment.qa_flags = clear_over_translation_flag(segment.qa_flags)
    # #664: the minor label-echo note describes ONE commit's text, so drop a stale one and
    # re-add it only when THIS commit stripped a label (UNGATED clear, like the line above:
    # the guard off must not leave a stale note behind).
    segment.qa_flags = clear_label_echo_flag(segment.qa_flags)
    if label_stripped:
        segment.qa_flags = merge_label_echo_flag(segment.qa_flags, source)
    segment.qa_flags = output_guard.clear_output_guard_flags(segment.qa_flags)
    # #343 SOFT output-guard hit (a dropped / added number, NUMBER_MISMATCH_WITHHOLDS False):
    # the text stays committed; the flag is re-merged AFTER the stale clear above, the
    # segment routes to review and is kept out of TM (same disposition as number_violation).
    og_soft = output_guard_flag is not None
    if og_soft:
        segment.qa_flags = output_guard.merge_output_guard_flag(segment.qa_flags, output_guard_flag)
        segment.status = SegStatus.NEEDS_REVIEW
    # QA-3 layer (b): the INDEPENDENT language-mismatch signal — a second, non-shared
    # detector so the choke point does not hinge solely on the curated-phrase is_refusal.
    # SOFT, like the DNT/glossary gates above (the text is already committed): a mismatch
    # DOWNGRADES to review + appends a DISTINCT `nontranslation` qa_flag AND forces
    # TM-ineligibility (a suspected non-translation must never seed TM), but never
    # withholds or rewrites the produced text. Gated by the same kill switch (`guard`)
    # AND by `lang_guard` — only the REAL fresh-MT path opts in; the offline MockProvider
    # echoes the English source and must not trip. SUBORDINATE to the scaffold verdict
    # (`not scaffold_reason`) so the shared family holds the more-specific finding when both
    # fire. Empty/clean -> zero change.
    tgt_locale = getattr(segment, "target_locale", None)
    nontranslation = bool(guard and lang_guard and refusal.looks_untranslated(text, tgt_locale))
    if nontranslation and not scaffold_reason:
        segment.qa_flags = refusal.merge_nontranslation_flag(
            segment.qa_flags, refusal.nontranslation_flag(source)
        )
        segment.status = SegStatus.NEEDS_REVIEW
    # Issue #58 — the OUTPUT-SCRIPT SANITY gate, sibling of `nontranslation`. The
    # language-echo signal above is function-word based and only catches an ENGLISH echo;
    # the world tour's catastrophic class was a non-Latin target coming back in a DIFFERENT
    # wrong language/script (Urdu as Spanish, Punjabi as Devanagari), which carries no
    # English function words and slipped through as a clean success. SCRIPT-based and
    # language-agnostic: a non-Latin target with ZERO expected-script letters is flagged.
    # SOFT and rides the SAME `nontranslation` qa_flag family (so the workflow write-back's
    # flag-family re-assertion covers it for free), gated by the same `guard AND lang_guard`
    # as the echo signal (MockProvider's English echo would otherwise trip every non-Latin
    # mock target). SUBORDINATE to the echo and scaffold verdicts so the family holds the
    # already-set finding when they fire.
    wrong_script = ""
    if guard and lang_guard and not nontranslation and not scaffold_reason:
        wrong_script = refusal.wrong_script(text, tgt_locale, source=segment.source_text)
        if wrong_script:
            segment.qa_flags = refusal.merge_nontranslation_flag(
                segment.qa_flags, refusal.wrong_script_flag(wrong_script, source)
            )
            segment.status = SegStatus.NEEDS_REVIEW
    # BUG-06 (b) — the META-COMMENTARY guard. UNLIKE `lang_guard` this is NOT opt-in per
    # caller: it is safe on EVERY path including the offline MockProvider (whose echo can
    # only carry a note the SOURCE already carried, which the source-side keep-guard
    # abstains on), and the live miss happened on the WORKFLOW fix path, which does not
    # opt into lang_guard. Gated by the same `REFUSAL_GUARD_ENABLED` kill switch as the
    # rest of the refusal perimeter. Rides the SAME `nontranslation` flag family (see
    # `meta_commentary_flag`) with its own reason, so a segment that trips BOTH signals
    # carries one flag naming the stronger, already-set language verdict.
    meta_reason = meta_commentary(segment.source_text, text) if guard else ""
    # F3 (gate review): `merge_nontranslation_flag` REPLACES the shared-family entry by
    # type, so a meta verdict firing here would OVERWRITE a wrong-script badge already set
    # above with "translator note left in target" — mislabeling a Spanish-for-Urdu target.
    # `not wrong_script` makes the stronger, more-specific LANGUAGE verdict win, exactly as
    # `not nontranslation`/`not scaffold_reason` do (the :2166 subordinate-to-language claim
    # then holds for wrong_script too). The stale-clear elif is reason-scoped, so it never
    # touches the wrong-script entry.
    if meta_reason and not nontranslation and not scaffold_reason and not wrong_script:
        segment.qa_flags = refusal.merge_nontranslation_flag(
            segment.qa_flags, meta_commentary_flag(meta_reason, source)
        )
        segment.status = SegStatus.NEEDS_REVIEW
    elif guard and not meta_reason and not nontranslation and not scaffold_reason:
        # STALE-BADGE CLEARING (see `clear_meta_commentary_flag`). We just RAN the guard
        # on this text and it is clean, so a commentary verdict left by an earlier commit
        # describes text that no longer exists. Scoped to the commentary reason only —
        # the language-mismatch verdict in the same family is another check's and is
        # never touched here (and `nontranslation` above would have re-set it anyway).
        segment.qa_flags = clear_meta_commentary_flag(segment.qa_flags)
    # K4 — the LENGTH-RUNAWAY gate (live QA campaign; see the section header above). Like
    # the commentary guard and UNLIKE `lang_guard` it is NOT opt-in per caller: it is safe
    # on EVERY path including the offline MockProvider (whose echo is the source plus a
    # short locale tag and cannot clear either lock), and the live miss happened on a path
    # that does not opt into lang_guard. Gated by the same `REFUSAL_GUARD_ENABLED` kill
    # switch as the rest of the refusal perimeter, so flipping the guard off is
    # byte-identical to pre-fix. Rides the SAME `nontranslation` family with its own
    # reason; SUBORDINATE to both the language and the commentary verdicts (that family
    # holds one entry, and those two are the stronger, more specific findings).
    length_reason = length_runaway(segment.source_text, text) if guard else ""
    # F3 (gate review): same shared-family REPLACE hazard as the meta gate above — a length
    # verdict must not overwrite a wrong-script badge, so it is subordinate to the language
    # verdict too (`not wrong_script`).
    if (
        length_reason
        and not nontranslation
        and not meta_reason
        and not scaffold_reason
        and not wrong_script
    ):
        segment.qa_flags = refusal.merge_nontranslation_flag(
            segment.qa_flags, length_runaway_flag(length_reason, source)
        )
        segment.status = SegStatus.NEEDS_REVIEW
    elif (
        guard
        and not length_reason
        and not nontranslation
        and not meta_reason
        and not scaffold_reason
    ):
        # STALE-BADGE CLEARING, same rule as the commentary guard directly above: we just
        # RAN this gate on the committed text and it is clean, so a length verdict left by
        # an earlier commit describes text that no longer exists. Scoped to the length
        # reason only — the language and commentary verdicts in the same family belong to
        # other checks (and either of them being set skips this branch anyway).
        segment.qa_flags = clear_length_runaway_flag(segment.qa_flags)
    # BUG-06 (a) — the NUMBER integrity gate. ALWAYS ON (like the tag gate, and for the
    # same reason: a pure deterministic value comparison that cannot false-positive on
    # MockProvider's verbatim echo). Judged with the segment's OWN target locale, which is
    # what makes "25.000" readable as twenty-five thousand for es-ES instead of an
    # undecidable rendering. SOFT: the text stays committed, the segment routes to review
    # and is kept out of TM.
    num_reason = number_violation(segment.source_text, text, tgt_locale)
    if num_reason:
        segment.qa_flags = merge_number_flags(segment.qa_flags, num_reason)
        segment.status = SegStatus.NEEDS_REVIEW
    else:
        # STALE-BADGE CLEARING, same rule as the tag gate below: this gate is ALWAYS ON,
        # so a clean verdict here is a full re-evaluation of the committed text and a
        # number_integrity flag from an earlier commit is now describing text that no
        # longer exists. Every other family is preserved.
        segment.qa_flags = clear_number_flags(segment.qa_flags)
    # LC-7 — the REAL-PLACEHOLDER integrity gate. ALWAYS ON, exactly like the number and
    # tag gates and for the same reason: a pure deterministic multiset compare that cannot
    # false-positive on MockProvider's echo (its "[locale] " prefix carries no placeholder).
    # SOFT: the text stays committed, the segment routes to NEEDS_REVIEW and is forced
    # TM-ineligible (a dropped `%s`/`{name}` that seeds TM returns as a 100% match that
    # bypasses this guard on every later document). NO export hard-block of its own; while the
    # segment stays held, the export's tag-damage policy (`export.segment_tag_damage` rung 3,
    # #711) ships its SOURCE instead (default `source`), so a broken target never reaches the file.
    ph_reason = placeholder_violation(segment.source_text, text, segment=segment)
    if ph_reason:
        segment.qa_flags = merge_placeholder_flags(segment.qa_flags, ph_reason)
        segment.status = SegStatus.NEEDS_REVIEW
    else:
        # STALE-BADGE CLEARING, same rule as the number/tag gates: this gate is ALWAYS ON,
        # so a clean verdict here is a full re-evaluation and a placeholder_integrity flag
        # from an earlier commit now describes text that no longer exists.
        segment.qa_flags = clear_placeholder_flags(segment.qa_flags)
    # AI Tagging 1.2 — the inline-tag integrity gate (ALWAYS ON; not gated by `guard`
    # or `lang_guard` — a pure deterministic honesty check that never false-positives
    # offline, since MockProvider's echo preserves the tokens verbatim). Runs on the
    # RESTORED committed target vs the RESTORED source. On damage the cheap
    # deterministic repair rung (1.5b) runs FIRST — order-restore + dedup, re-verified
    # — so a recoverable case is fixed and committed clean instead of swamping review
    # or bouncing back into the fix loop. Only genuinely unrecoverable damage
    # quarantines: NEEDS_REVIEW + tag_integrity qa_flag + TM-ineligible +
    # connector-withheld (the NONTRANSLATION pattern, NOT the soft DNT one).
    tag_reason = tag_damage(segment.source_text, text)
    tag_repaired = False
    tag_llm_repaired = tag_llm_verified = False
    tag_cost, tag_tin, tag_tout = 0.0, 0, 0
    if tag_reason:
        rep = tag_repair.repair(segment.source_text, text)
        if rep is not None:
            cand, clean = rep
            if not tag_damage(segment.source_text, cand):  # structurally verified
                segment.target_text = text = cand
                tag_repaired = True
                # De-dup is certified clean -> clear the flag. A point-order restore is
                # a BEST GUESS (a legitimate target-language point-token reorder must
                # not be silently undone), so keep the repaired text but STILL flag +
                # NEEDS_REVIEW for a human to confirm placement (MEDIUM, reviewer #2).
                tag_reason = (
                    "" if clean else "point-token order restored (best-guess placement — verify)"
                )
    # AI Tagging 1.5c — the LLM rung, LAST before quarantine. Reached ONLY when the
    # deterministic rung could not certify a fix (a dropped/misplaced paired span has no
    # zero-dependency repair) AND the caller injected a repairer. Skipped entirely when
    # the deterministic rung already produced a best-guess point-order restore
    # (`tag_repaired`): that text is structurally whole and only its placement is in
    # doubt, so spending an LLM call to re-derive it would be redundant — it stays
    # flagged for the human confirmation it already asks for.
    if tag_reason and not tag_repaired and llm_tag_repair is not None:
        try:
            res = llm_tag_repair(segment.source_text, text)
        except Exception:
            res = None  # a repair rung must NEVER break the write path
        # RE-VERIFY HERE (never trust the injected repairer): the candidate must clear
        # the SAME gate everything else clears. A candidate that doesn't is discarded
        # and the segment quarantines exactly as it would have.
        if res is not None and not tag_damage(segment.source_text, res.text):
            segment.target_text = text = res.text
            tag_llm_repaired = True
            tag_llm_verified = bool(res.verified)
            tag_cost = res.cost_usd or 0.0
            tag_tin, tag_tout = res.tokens_in or 0, res.tokens_out or 0
            # VERIFIED -> the tokens are all back (lock 1), no word changed (lock 2) and
            # an independent reader confirmed placement (lock 3): commit CLEAN, no flag,
            # TM-eligible. UNVERIFIED -> commit the repaired text (tags-back beats
            # tags-missing) but keep tag_integrity + NEEDS_REVIEW so a human confirms.
            tag_reason = (
                "" if res.verified else (res.note or "AI tag repair applied, placement unconfirmed")
            )
    if tag_reason:
        segment.qa_flags = merge_tag_flags(segment.qa_flags, tag_reason)
        segment.status = SegStatus.NEEDS_REVIEW
    else:
        # STALE-BADGE CLEARING (export-guard reviewer, 2026-07-28). This seam MERGED a
        # tag_integrity flag on damage but never removed one, so a segment whose tags were
        # restored by a later commit — a re-translate, a fix-step rewrite, a certified
        # deterministic/LLM repair in this very call — kept a MAJOR "tags broken" badge
        # forever. The editor grid and `doc show --detail` rendered it, and `export`
        # had to build an admissibility floor to work around exactly this staleness.
        # The gate is ALWAYS ON, so reaching here IS a full re-verification of the text
        # that was just written: `clear_tag_flags` drops only the tag_integrity entries
        # (the hand-repair path in `fulltext_paragraphs.save_paragraph` clears the same
        # way) and leaves every other family — refusal, qe, dnt, glossary,
        # nontranslation, number_integrity — exactly as the checks above left it.
        # Status is deliberately NOT touched: NEEDS_REVIEW may be owed to any of those.
        segment.qa_flags = clear_tag_flags(segment.qa_flags)
    # K8 — the CHARACTER-LIMIT gate. DELIBERATELY LAST, AFTER the tag-repair rungs above
    # (reviewer H1): the deterministic dedup (1.5b) and the LLM rung (1.5c) both REASSIGN
    # `text`/`segment.target_text` to a repaired string of a DIFFERENT LENGTH, and this is
    # the ONE gate that changes verdict when length changes. Judged before them, it
    # false-POSITIVED on a dedup that made the text SHORTER (flag naming a length that no
    # longer exists — the stale-badge defect) and false-NEGATIVED on an LLM repair that
    # made it LONGER (gate bypassed). The number/meta/length gates survive being judged
    # early because the deterministic dedup rung (1.5b) preserves the token multiset, and
    # the inline LLM rung (1.5c) here re-verifies TAGS ONLY (`not tag_damage`) — its
    # body-preservation is NOT asserted at this seam but in `repair_stored_segments` (the
    # `body_preserved` re-check at that write site). char_limit is preserved by neither
    # rung, so it alone must read the FINAL committed `text`. SELF-RESOLVING from the
    # segment's OWN key_id (a real FK) — no caller passes it and none can forget it (the
    # K10 class); a document segment (key_id None) issues ZERO queries and is byte-
    # identical. ALWAYS ON like the number/tag gates (a pure len() comparison that cannot
    # false-positive on MockProvider's echo). SOFT and its OWN `char_limit` family: routes
    # to NEEDS_REVIEW but leaves tm_eligible UNTOUCHED (an over-limit value is
    # linguistically correct — see the section header). Never truncates or withholds. The
    # workflow write-back re-asserts this off `outcome.char_limit_violation`.
    cl_limit = segment_char_limit(session, segment)
    cl_reason = char_limit_violation(text, cl_limit)
    if cl_reason:
        segment.qa_flags = merge_char_limit_flags(
            segment.qa_flags, cl_reason, char_count(text), coerce_char_limit(cl_limit)
        )
        segment.status = SegStatus.NEEDS_REVIEW
    else:
        # STALE-BADGE CLEARING, same rule as the number gate: ALWAYS ON, so reaching here
        # is a full re-evaluation of the FINAL text and a char_limit flag from an earlier
        # commit (of longer text, or before the limit was raised) now describes a value
        # that fits. Every other family is preserved.
        segment.qa_flags = clear_char_limit_flags(segment.qa_flags)
    # QA-1 — the WHITESPACE hygiene gate. Gated behind `guard AND lang_guard AND NOT
    # full_text` (real-MT, non-full-text only):
    #   * lang_guard (real-MT): the offline MockProvider echoes the source under a
    #     "[locale] " prefix, so a leading-space source produces an echo carrying an
    #     interior double space the source lacks — a systemic false positive offline.
    #   * NOT full_text (reviewer H1): a full-text body is committed VERBATIM from the
    #     provider, whose raw output keeps its trailing "\n" (and paragraphs join on
    #     "\n\n"), so a trailing-whitespace check would route EVERY real full-text
    #     delivery to review. Full-text bodies are excluded from THIS gate only; the
    #     editor-live whitespace badge (qa.check_segment) still shows the slip.
    # SOFT and its OWN `whitespace` family (MINOR): routes to NEEDS_REVIEW but leaves
    # tm_eligible UNTOUCHED (the char_limit posture — a spacing slip is linguistically
    # correct).
    ws_reason = (
        whitespace_violation(segment.source_text, text)
        if (guard and lang_guard and not full_text)
        else ""
    )
    if ws_reason:
        segment.qa_flags = merge_whitespace_flags(segment.qa_flags, ws_reason)
        segment.status = SegStatus.NEEDS_REVIEW
    else:
        # STALE-BADGE CLEARING, UNCONDITIONAL (reviewer L6): clear any prior whitespace
        # badge whenever this commit produced no violation — whether the gate ran clean OR
        # was SKIPPED (mock / import / TM apply / full-text). A skipped gate means the text
        # changed under an unknown verdict, and a stale "whitespace" badge from an earlier
        # real-MT commit then describes text that no longer exists — strictly worse than
        # absent. Byte-safe: clear_whitespace_flags returns None on an already-clean column.
        segment.qa_flags = clear_whitespace_flags(segment.qa_flags)
    # Decisions 11 / 14 / 16 (2026-10-11): the REVIEW-HOLD family (quality_part3): an
    # ambiguous numeric date the target reader can take with day and month swapped, a line
    # break the engine added or removed, words glued at a formatting change. Same gate as the
    # whitespace check (real-MT, not full-text: the MockProvider echo keeps a US date verbatim,
    # which would read swapped in a day-first target). Routes to NEEDS_REVIEW and keeps the
    # target out of TM (a swapped date or an invented line break that seeds TM returns as a
    # 100% match through the TM path, which runs no lang_guard checks). The export still writes
    # the target. Stale badge cleared whenever this commit produced no hold.
    hold_reason = (
        review_hold_violation(
            segment.source_text,
            text,
            tgt_locale,
            source_locale=lambda: segment_source_locale(session, segment),
            fmt=lambda: segment_fmt(session, segment),
        )
        if (guard and lang_guard and not full_text)
        else ""
    )
    if hold_reason:
        segment.qa_flags = merge_review_hold_flags(segment.qa_flags, hold_reason)
        segment.status = SegStatus.NEEDS_REVIEW
    else:
        segment.qa_flags = clear_review_hold_flags(segment.qa_flags)
    # QE verdicts describe one exact text. Compare AFTER tag repair, and only after
    # a successful write: refused/protected writes and unchanged targets keep them.
    # The orchestrator attaches this run's fresh verdict after this seam returns.
    if text != _prev_target and any(
        isinstance(f, dict) and f.get("type") in ("qe", "qe_autofix")
        for f in (segment.qa_flags or [])
    ):
        segment.qa_flags = qe.clear_qe_flag(segment.qa_flags)
    # TM-matching lock CLEARING (PROPOSAL-tm-matching §5, item 7a). Reaching this write
    # means the segment is NOT hard-locked (a hard lock was refused far above) -- so a
    # lock still on it is a SOFT lock being SUPERSEDED by this commit (a round-2 fix
    # editing an LQA-flagged soft-locked segment, per D3). Clear the band + provenance in
    # the SAME commit so the segment never
    # carries a "locked, band=100, TM entry N" claim over text that no longer came from
    # that TM entry. Gated on an ACTUAL change (text != _prev_target) so a later run that
    # re-commits a soft-locked segment's OWN stored target straight back through the seam
    # keeps the lock. Inline (no tm_lock import -- it imports quality); a no-op on the
    # common unlocked segment.
    if getattr(segment, "lock_band", None) and text != _prev_target:
        segment.lock_band = None
        segment.lock_hard = False
        if segment.meta and "tm_lock" in segment.meta:
            _m = dict(segment.meta)
            _m.pop("tm_lock", None)
            segment.meta = _m
    session.add(segment)
    return CommitOutcome(
        committed=True,
        flagged=False,
        # BUG-06: a corrupted figure and a surviving translator note both
        # force TM-ineligibility, exactly like the nontranslation/tag
        # verdicts — a wrong number or a model note that seeds shared TM
        # returns as a 100% match that BYPASSES this guard forever.
        # K4: a length runaway joins them — a 27x-expanded target that
        # seeds shared TM comes back as a 100% match that BYPASSES this
        # guard on every later document (the C3 poison path).
        # Q10: a scaffold echo joins them too — a leaked style-guide/findings
        # block that seeds shared TM returns as a 100% match that bypasses
        # this guard forever, and it is never a valid TM entry.
        # LC-7: a placeholder-dropped target joins the TM-ineligible
        # classes for the same C3-poison reason as number/tag/length —
        # seeding TM would let it return as a 100% match that bypasses
        # this guard forever. (whitespace does NOT subtract: it is soft,
        # tm_eligible-neutral, like char_limit.)
        tm_eligible=(
            tm_eligible
            and not nontranslation
            and not tag_reason
            and not num_reason
            and not og_soft
            and not meta_reason
            and not length_reason
            and not scaffold_reason
            and not ph_reason
            and not wrong_script
            and not hold_reason
        ),
        dnt_violations=tuple(dnt),
        glossary_violations=tuple(gloss),
        tag_violation=tag_reason,
        tag_repaired=tag_repaired,
        tag_llm_repaired=tag_llm_repaired,
        tag_llm_verified=tag_llm_verified,
        tag_repair_cost_usd=tag_cost,
        tag_repair_tokens_in=tag_tin,
        tag_repair_tokens_out=tag_tout,
        number_violation=num_reason,
        meta_commentary=meta_reason,
        length_runaway=length_reason,
        scaffold_echo=scaffold_reason,
        wrong_script=wrong_script,
        # K8: SOFT and its OWN family — routes to review but does NOT
        # subtract from tm_eligible above (an over-limit value is
        # linguistically correct; the same text in a limit-free document
        # is a fine TM entry). The workflow write-back consumes this to
        # re-assert NEEDS_REVIEW after its status=MT reset.
        char_limit_violation=cl_reason,
        placeholder_violation=ph_reason,
        whitespace_violation=ws_reason,
        review_hold=hold_reason,
        label_echo_stripped=label_stripped,
    )


# =============================================================================
# REPEAT PROPAGATION WRITERS (Issue #617 item 6)
# =============================================================================
# Propagation copies a target a PERSON just approved onto the document's open repeats of the
# same source (segment_review.propagate_repeats). It is not machine output, so it does not go
# through commit_mt_output (whose machine-only gates would misjudge human text and whose status
# rules would land it MT). The two writes live HERE, in the choke-point module, so the
# target_text tripwire (tests/verify_refusal_choke_point.py) keeps its full strength over
# segment_review.py and the integrity gate is inseparable from the write: a caller cannot write
# a propagated copy without the per-copy tag and placeholder check running first.


def write_propagated_copy(segment, text, *, acknowledge_placeholder_loss=False) -> tuple:
    """Write `text` (an approved target) onto the repeat `segment` as EDITED, after checking it
    against the copy's OWN source. Returns ("", "") when written, or (kind, reason) with kind
    "tag" or "placeholder" when the text would break this copy; then NOTHING is changed. An
    acknowledged placeholder loss on the approve carries over (the approver accepted it for
    this text); a tag break never does. Status, marker and flag bookkeeping are the caller's."""
    reason = tag_damage(segment.source_text, text)
    if reason:
        return ("tag", str(reason))
    if not acknowledge_placeholder_loss:
        reason = placeholder_violation(segment.source_text, text, segment=segment)
        if reason:
            return ("placeholder", str(reason))
    segment.target_text = text
    segment.status = SegStatus.EDITED
    return ("", "")


def restore_propagated_copy(segment, previous_text, *, propagated_text) -> bool:
    """Undo of one propagated copy: put back `previous_text` ONLY while the copy still holds
    exactly `propagated_text` (compare-and-set; a copy someone changed since is never
    reverted). Returns True when restored. Status, marker and flags are the caller's."""
    if (segment.target_text or "") != (propagated_text or ""):
        return False
    segment.target_text = previous_text
    return True
