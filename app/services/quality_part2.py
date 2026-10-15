"""quality: part 2 (layered pure move)

Documented in: docs/architecture/quality-lqa-evaluations.md
"""

from __future__ import annotations

import bisect
import functools
import html
import re
import unicodedata
from collections import Counter
from dataclasses import dataclass

from app.services import quality_part4 as link_locale
from app.services import scaffold, segment
from app.services.quality_common import (
    _AGREEMENT_SUFFIXES,
    _META_BRACKET_RE,
    _META_FIRST_PERSON,
    _META_FIRST_PERSON_GAPPED,
    _META_NOTE_MARKERS,
    _META_PAREN_RE,
    _META_VOCAB,
    _MIN_APPEND_STEM,
    _MIN_STEM,
    _SEAM_PLACEHOLDER_RE,
    _WORD_RE,
    CHAR_LIMIT_FLAG_TYPE,
    GATE_HOLD_FLAG_TYPE,
    META_COMMENTARY_REASON,
    NUMBER_FLAG_TYPE,
    PLACEHOLDER_FLAG_TYPE,
    SOURCE_FALLBACK_FLAG_TYPE,
    TAG_FLAG_TYPE,
    WHITESPACE_FLAG_TYPE,
    _fold_accents,
    _lang_family,
    _meta_fold,
    _placeholder_multiset,
    _term_present,
    _word_stem,
    char_count,
    coerce_char_limit,
    number_flag,
    source_fallback_flag,
    standalone_source_terms,
    tag_damage,
)


@dataclass(frozen=True)
class CommitOutcome:
    """The result of a `commit_mt_output` call.

    * committed    — True iff `target_text` was written (a clean, non-refusal
                     commit). False when the guard withheld the write.
    * flagged      — True iff the write was WITHHELD. Usually a suspected refusal:
                     the segment was routed to NEEDS_REVIEW with a refusal qa_flag
                     and no target_text was written. It is ALSO True (with no review
                     routing and no qa_flag -- the row is byte-untouched) on the two
                     protection refusals, `lock_refused` and `status_protected`, so
                     every caller's existing withheld-write guard skips its
                     success-path mutations for those too. Consult those two fields
                     (or `committed`) to tell a protection refusal from a hedge.
    * tm_eligible  — whether the committed text may be stored into TM. False on a
                     flagged outcome (a hedge must never reach TM); otherwise the
                     caller's `tm_eligible` argument. The TM store call stays at the
                     existing site but MUST consult this (or `committed`).
    * dnt_violations — the DNT source terms that were present in the source but NOT
                     preserved verbatim in the COMMITTED target (SPEC phase 2B). A
                     non-empty tuple means the choke point appended a dnt_violation
                     qa_flag and routed the segment to NEEDS_REVIEW — but the text
                     WAS still committed (a DNT miss flags for a human, it does not
                     block). Empty on the refusal path (nothing committed) and
                     whenever no glossary_terms were passed / no term was dropped.
    * glossary_violations — the NON-DNT glossary entries whose required target_term
                     was MISSING from the committed target though their source_term
                     appeared in the source (SPEC Track B / B1). Exact-same soft
                     semantics as dnt_violations: a non-empty tuple means the choke
                     point appended a glossary_violation qa_flag and routed the
                     segment to NEEDS_REVIEW, but the text WAS still committed and
                     tm_eligible is UNCHANGED (a required-term miss flags for a human,
                     it never blocks the write nor forces TM-ineligibility). Empty on
                     the refusal path and whenever every required term was present."""

    committed: bool
    flagged: bool
    tm_eligible: bool
    dnt_violations: tuple = ()
    glossary_violations: tuple = ()
    # AI Tagging 1.2: non-empty reason string when the committed target's inline-
    # fidelity `⟦⟧` tokens diverged from the source and could NOT be deterministically
    # repaired (tag_repair). Non-empty means the choke point appended a tag_integrity
    # qa_flag, routed the segment to NEEDS_REVIEW, AND forced tm_eligible=False +
    # connector-withheld (mirrors the nontranslation gate — HARD, unlike DNT/glossary).
    # Empty on the refusal path (nothing committed) and whenever tags were intact or a
    # repair certified clean. `tag_repaired` is True when a deterministic repair fixed
    # the tokens (the committed text was rewritten, no flag) — surfaced so a caller/
    # test can tell a clean pass from a repaired one.
    tag_violation: str = ""
    tag_repaired: bool = False
    # AI Tagging 1.5c: True when the LLM repair rung (tag_llm_repair) produced the
    # committed text — i.e. deterministic repair had already given up and the segment
    # was headed for quarantine. `tag_llm_verified` is True only when the INDEPENDENT
    # placement-verification call also confirmed the tags landed in the right places
    # (the one condition under which an LLM repair commits with NO flag). A repair that
    # is llm_repaired-but-not-verified commits the improved text AND keeps
    # tag_violation + NEEDS_REVIEW. `tag_repair_cost_usd`/`tokens_*` are the exact
    # ledger spend of the rung's call(s) (0.0/0 when it never ran), returned so a
    # workflow step can stamp its real per-visit cost the same way the LQA judge does.
    tag_llm_repaired: bool = False
    tag_llm_verified: bool = False
    tag_repair_cost_usd: float = 0.0
    tag_repair_tokens_in: int = 0
    tag_repair_tokens_out: int = 0
    # QA campaign BUG-06 (both halves). Non-empty reason strings when the choke point's
    # two new deterministic gates fired on the COMMITTED target:
    #   * number_violation — a source number's VALUE is not preserved by any target
    #     number, read with the TARGET LOCALE's decimal convention ("25 items" ->
    #     "25.000 articulos" for es-ES). SOFT like the nontranslation gate: the text is
    #     committed, the segment is routed to NEEDS_REVIEW under a `number_integrity`
    #     qa_flag and forced TM-ineligible (a corrupted figure must never seed TM).
    #   * meta_commentary — a translator-note / model self-commentary parenthetical
    #     survived into the delivered target ("(Note: I corrected all issues identified
    #     in the feedback ...)"). Same SOFT semantics, and it rides the EXISTING
    #     `nontranslation` qa_flag family (see the section below for why).
    # Empty on the refusal path (nothing committed) and whenever the target is clean.
    number_violation: str = ""
    meta_commentary: str = ""
    # TM-matching locks (PROPOSAL-tm-matching-workflow-step §5). True iff this call was
    # REFUSED because the segment carries a HARD TM lock. This seam only ever writes
    # MACHINE / imported text, so it always refuses a hard lock; a human edit does not
    # reach here (the CAT editor / key-grid saves are direct writers that clear the lock
    # at their own site). target_text/status/qa_flags are left UNTOUCHED (the locked
    # segment already holds its good TM fill — the seam simply declined to overwrite it)
    # and the outcome is committed=False, flagged=True. Distinct from a content refusal so
    # a caller/test can tell "blocked by lock" from "hedge withheld".
    lock_refused: bool = False
    # HUMAN-WORK PROTECTION (data-integrity fix). True iff this call was REFUSED because the
    # segment carries a HUMAN decision the seam must never overwrite -- status APPROVED or
    # EDITED. Like lock_refused it leaves target_text / status / qa_flags / lock columns
    # EXACTLY as they are (the row is byte-untouched) and returns committed=False, flagged=True
    # (so every caller's existing withheld-write guard skips its success-path mutations). It is
    # a DISTINCT marker, never overloaded onto lock_refused, so a caller/test can tell "blocked
    # by a hard TM lock" from "blocked by a human sign-off" and count / message each correctly.
    # Unlike lock_refused it is NOT gated by TM_LOCKS_ENABLED, and unlike the content refusal it
    # is NOT gated by REFUSAL_GUARD_ENABLED: protecting human work is core, default-on behavior
    # with no kill switch. NEEDS_REVIEW is deliberately NOT protected (it is machine routing --
    # a re-run is meant to re-translate a flagged segment).
    status_protected: bool = False
    # Live QA campaign K4 — the LENGTH-RUNAWAY gate. Non-empty reason string when the
    # committed target is BOTH absolutely and proportionally far longer than its source
    # (a 9-char UI label that came back as a 245-char conversational preamble). Same SOFT
    # semantics as the two above — the text is committed and never rewritten, the segment
    # routes to NEEDS_REVIEW and is forced TM-ineligible — and it rides the SAME
    # `nontranslation` qa_flag family with its own reason. Empty on the refusal path and
    # whenever either of the two locks fails to hold.
    length_runaway: str = ""
    # Core-QA campaign Q10 — the SCAFFOLD-ECHO gate. Non-empty reason string when the
    # committed target reproduced our OWN injected prompt scaffolding (a fenced
    # style-guide / context / LQA-findings block, or the "MQM LQA findings to resolve"
    # preamble echoed by a weak reviewer/post-edit model). Same SOFT semantics as the three
    # above and rides the SAME `nontranslation` qa_flag family, but it is the STRONGEST member
    # (the language/meta/length verdicts subordinate to it). Empty on the refusal path and
    # whenever the target carries no scaffold marker (or the source carried the same one).
    scaffold_echo: str = ""
    # Issue #58 — the OUTPUT-SCRIPT SANITY gate. Non-empty reason string when a non-Latin
    # target came back with ZERO characters of its expected script (Urdu produced as
    # Spanish, Punjabi as Devanagari). Same SOFT semantics as `nontranslation` — text
    # committed, routed to NEEDS_REVIEW, forced TM-ineligible — and it rides the SAME
    # `nontranslation` qa_flag family, so the orchestrator's flag-family re-assertion covers
    # the workflow path without a dedicated line. Empty unless `guard AND lang_guard` and a
    # real wrong-script delivery. Surfaced as a field so the translate path and pins can
    # read the verdict directly (the fill/summary counts a soft-flagged review).
    wrong_script: str = ""
    # Live QA campaign K8 — the CHARACTER-LIMIT gate. Non-empty reason string when the
    # committed target exceeds the character limit set on its StringKey (a `<=40` UI slot
    # that came back 63 characters long). Its OWN `char_limit` qa_flag family, NOT the
    # nontranslation one: an over-limit value is LINGUISTICALLY CORRECT, so it routes to
    # NEEDS_REVIEW but leaves tm_eligible UNTOUCHED (the same sentence in a document with
    # no limit is a fine TM entry). Empty on the refusal path, for a document segment
    # (key_id None), and whenever the value fits or the key has no valid limit.
    #
    # THE ORCHESTRATOR RE-ASSERTION (BUG-06 class). The per-segment workflow write-back
    # resets `seg.status = MT` AFTER this seam and re-asserts NEEDS_REVIEW off the outcome
    # fields (number_violation, meta_commentary, ...). A workflow that produces an
    # over-limit KEY value therefore needs `if outcome.char_limit_violation: seg.status =
    # NEEDS_REVIEW` beside those lines, exactly as number_violation has it — otherwise the
    # STATUS routing (not the stored flag, which survives the reset) is wiped on that one
    # path. This field exists so that line is a one-liner when it lands.
    char_limit_violation: str = ""
    # LC-7 — the REAL-PLACEHOLDER integrity gate. Non-empty reason string when the
    # committed target's `{0}`/`{name}`/`%s`/`%1$d` placeholder multiset diverges from the
    # source's (reordering is silent; a dropped/added token is named). SOFT like the number
    # gate: text committed, routed to NEEDS_REVIEW, forced tm_eligible=False (a placeholder-
    # dropped target that seeds TM returns as a 100% match that bypasses this guard). Empty
    # on the refusal path and whenever the placeholders match. The orchestrator's segmented
    # write-back re-asserts NEEDS_REVIEW off this field after its status=MT reset.
    placeholder_violation: str = ""
    # QA-1 — the WHITESPACE hygiene gate. Non-empty reason string when the committed target
    # has an interior double space, or leading/trailing whitespace, that the source lacks.
    # SOFT and its OWN `whitespace` family (MINOR): routes to NEEDS_REVIEW but leaves
    # tm_eligible UNTOUCHED (like char_limit — a spacing slip is linguistically correct).
    # Only set when the caller opts in via `lang_guard` (real-MT paths); empty for the
    # offline MockProvider echo. Re-asserted by the orchestrator like char_limit.
    whitespace_violation: str = ""
    # Decisions 11 / 14 / 16 (2026-10-11): the review-hold family (app.services.quality_part3:
    # ambiguous numeric date, line breaks differ, words glued at a formatting change). Routes to
    # NEEDS_REVIEW and makes the commit TM-ineligible; real-MT only like whitespace. Re-asserted
    # by the orchestrator after its status reset.
    review_hold: str = ""
    # #664 — a leading prompt section label ("Source:") the model echoed was STRIPPED from the
    # committed text and a minor `label_echo_stripped` qa_flag added. The text is clean, so
    # status stays mt and tm_eligible is untouched; callers that keep their own copy of the
    # output (the workflow's emitted/working text) re-read the target when this is True. The
    # review case (more than the label) is reported as `scaffold_echo` instead.
    label_echo_stripped: bool = False


def _inflection_match_word(term_word: str, tok: str, lang: str, case_sensitive: bool) -> bool:
    """True when target token `tok` is `term_word` or a recognised inflection of it (the
    three rungs in the section header). Deterministic, LLM-free."""
    if case_sensitive:
        a, b = term_word, tok
    else:
        a, b = term_word.lower(), tok.lower()
    if a == b:  # (1) exact (accent-sensitive)
        return True
    # F8: rungs (2) and (3) fold accents so botón/botones, sesión/sesiones and -ó
    # preterites match; the exact rung above stays accent-sensitive on purpose.
    fa, fb = _fold_accents(a), _fold_accents(b)
    for short, lng in ((fa, fb), (fb, fa)):  # (2) append agreement
        if (
            len(short) >= _MIN_APPEND_STEM
            and lng.startswith(short)
            and lng[len(short) :] in _AGREEMENT_SUFFIXES.get(lang, _AGREEMENT_SUFFIXES["*"])
        ):
            return True
    sa, sb = _word_stem(fa, lang), _word_stem(fb, lang)  # (3) floored stem equality
    return sa == sb and len(sa) >= _MIN_STEM


def _term_present_tolerant(
    term: str,
    text: str,
    case_sensitive: bool,
    lang: str,
    exact: bool,
    *,
    text_nfc: str | None = None,
    text_toks: list | None = None,
) -> bool:
    """Whole-token, morphology-tolerant presence of `term` in `text`. A strict SUPERSET
    of `_term_present`: True whenever the exact whole-token check is (preserving its
    CJK-safe brand semantics) and then — unless the entry opts out via `exact`, or the term
    carries punctuation (the F1 well-formedness guard) — True when every word of `term`
    inflection-matches a run of adjacent target tokens in order (so a multiword term
    matches with per-word inflection: "iniciar sesión" -> "Inicie sesión").

    `text_nfc` / `text_toks` are an optional caller-supplied HOIST (F14): the NFC-
    normalized target and its `_WORD_RE` tokens, computed ONCE per target text instead of
    re-derived for every glossary term. Absent, both are computed here (standalone use)."""
    # F13: NFC-normalize both surfaces so precomposed/decomposed accents compare equal and
    # the accent fold on the tolerant rungs sees one canonical form.
    term = unicodedata.normalize("NFC", term or "")
    if text_nfc is None:
        text_nfc = unicodedata.normalize("NFC", text or "")
    if _term_present(term, text_nfc, case_sensitive):
        return True
    if exact:
        return False
    term_words = _WORD_RE.findall(term)
    if not term_words:
        return False
    # F1 well-formedness guard: a term carrying any non-word/non-space character (its
    # word-tokens don't reconstitute the whitespace-stripped term) is matched STRICTLY —
    # the strict check above already ran (and failed), so stop here rather than let the
    # punctuation-stripping tolerant rungs satisfy "C++" with a bare "c".
    if "".join(term_words) != "".join(term.split()):
        return False
    toks = text_toks if text_toks is not None else _WORD_RE.findall(text_nfc)
    n = len(term_words)
    for i in range(len(toks) - n + 1):
        if all(
            _inflection_match_word(term_words[k], toks[i + k], lang, case_sensitive)
            for k in range(n)
        ):
            return True
    return False


def glossary_term_violations(
    source_text: str | None,
    target_text: str | None,
    glossary_terms,
    *,
    target_locale: str | None = None,
) -> list:
    """The NON-DNT glossary entries whose required `target_term` is MISSING from
    `target_text` even though their `source_term` appears in `source_text`.

    Sibling of `dnt_violations`, consuming the SAME term dicts match_terms produces
    (source_term/target_term/sense_label/dnt/case_sensitive[/exact] — metadata is never
    in this list, per the glossary invariant). For each entry that (a) is NOT dnt, (b)
    carries a non-empty target_term, and (c) whose source_term appears as a whole token
    in the source (EXACT — the applicability gate stays strict), this checks the
    target_term is present in the target under a MORPHOLOGY-TOLERANT whole-token match
    (`_term_present_tolerant`, see the section header), honouring each entry's
    `case_sensitive` and `exact`. Returns the missed entries as {source_term,
    target_term} dicts, in glossary order, de-duplicated by the pair.

    MORPHOLOGY (Q5, core-QA fix train 1) — the accepted-limitation rationale, POST-FIX:
    the target check is no longer surface-only. It accepts inflected/agreement forms
    deterministically (plural + gender agreement, verb conjugations of a shared lemma,
    and per-word inflection inside a multiword term), because a live prod campaign proved
    the old surface match flagged ~half its hits on legitimate inflections of KNOWN-
    CORRECT text (and, via Q6, cost those TM-filled segments their lock band). It stays
    PRECISE where precision matters: the whole-token discipline is preserved (a required
    "AI" is not satisfied by "email"), a stem-length FLOOR keeps distinct short lemmas
    apart (a required "carga" is not satisfied by "cargo"), and a punctuation-bearing term
    (C++, Wi-Fi, %s, Node.js) is matched STRICTLY (F1). What REMAINS accepted (still a SOFT
    flag, never a block or a rewrite): because the stem rung strips a per-language
    inflectional suffix from both surfaces, ANY two same-root lemmas whose >=5-char stems
    coincide satisfy each other — INCLUDING noun<->verb pairs ("archivo"/"archivar",
    "registro"/"registrar"), not merely trailing-vowel swaps; and a term outside the
    es/fr/en families (or matched with no `target_locale`) gets plural/append tolerance
    only, not full conjugation tolerance. `target_locale` selects the per-language suffix
    tables; DNT (`dnt_violations`) stays EXACT and is untouched; a per-entry truthy `exact`
    opts back into strict surface matching (a forward hook — no surface sets it yet).

    Empty (no flag) whenever glossary_terms is falsy, either text is empty, or every
    applicable required term is present — so a clean translation's qa_flags/status are
    untouched."""
    if not glossary_terms or not source_text or not target_text:
        return []
    lang = _lang_family(target_locale)
    # F14: tokenize the target ONCE for the whole glossary, not once per term. The
    # NFC-normalized target and its word-tokens are term-independent, so the per-term
    # tolerant match reuses them.
    tgt_nfc = unicodedata.normalize("NFC", target_text)
    tgt_toks = _WORD_RE.findall(tgt_nfc)
    viol: list = []
    seen: set = set()
    active = standalone_source_terms(source_text, glossary_terms)
    for idx, t in enumerate(glossary_terms):
        if not isinstance(t, dict) or t.get("dnt"):
            continue
        if idx not in active:  # longest match wins (#663)
            continue
        src = t.get("source_term") or ""
        tgt = t.get("target_term") or ""
        if not src or not tgt or (src, tgt) in seen:
            continue
        cs = bool(t.get("case_sensitive"))
        exact = bool(t.get("exact"))
        if _term_present(src, source_text, cs) and not _term_present_tolerant(
            tgt, target_text, cs, lang, exact, text_nfc=tgt_nfc, text_toks=tgt_toks
        ):
            seen.add((src, tgt))
            viol.append({"source_term": src, "target_term": tgt})
    return viol


def tag_flag(reason: str) -> dict:
    """One qa_flags entry for a tag-integrity violation — shaped like the
    dnt/glossary/refusal flags ({type,severity,message}) so the CAT editor renders the
    badge through the SAME generic code path. 'major' (a reviewer must act — export
    would otherwise collapse formatting)."""
    return {
        "type": TAG_FLAG_TYPE,
        "severity": "major",
        "message": "Inline formatting tag integrity broken: " + reason,
        "reason": reason,
    }


def merge_tag_flags(existing: list | None, reason: str) -> list:
    """qa_flags with any prior tag_integrity entry replaced by one for `reason` (every
    other flag — refusal, qe, dnt, glossary, nontranslation — kept). Fresh list each
    call so SQLAlchemy sees the JSON column as changed (same pattern as merge_flag)."""
    out = [
        f for f in (existing or []) if not (isinstance(f, dict) and f.get("type") == TAG_FLAG_TYPE)
    ]
    out.append(tag_flag(reason))
    return out


def clear_tag_flags(existing: list | None) -> list | None:
    """qa_flags with every tag_integrity entry REMOVED (every other flag kept).

    Mirrors `qe.clear_qe_flag`. Nothing cleared this flag before GitHub #42, so a
    paragraph a human successfully hand-repaired kept its "tags broken" badge forever
    — the flag recorded a verdict about text that no longer existed. Returns None when
    nothing is left, so the column reads exactly as it did before any flag was set
    (byte-identity), and a FRESH list otherwise so SQLAlchemy sees the JSON column as
    changed."""
    out = [
        f for f in (existing or []) if not (isinstance(f, dict) and f.get("type") == TAG_FLAG_TYPE)
    ]
    return out or None


def merge_gate_hold_flags(existing: list | None, flags) -> list:
    """qa_flags with any prior gate-hold entries replaced by `flags`; every OTHER family
    (refusal, qe, dnt, glossary, tag, nontranslation, step_error) is preserved. A fresh
    list each call so SQLAlchemy sees the JSON column as changed."""
    out = [
        f
        for f in (existing or [])
        if not (isinstance(f, dict) and f.get("type") == GATE_HOLD_FLAG_TYPE)
    ]
    out.extend(flags)
    return out


def clear_gate_hold_flags(existing: list | None) -> list | None:
    """qa_flags with every gate-hold entry REMOVED (every other flag kept).

    The STALE-BADGE half of the rule, exactly as `clear_step_error_flags` is for its own
    family: a gate hold describes text a LATER clean run has since replaced. Without this
    the segment would wear a CRITICAL badge -- and the document would stay export-held --
    forever, no matter how good the new translation is. Returns None when nothing is left
    so the column reads as it did before any flag was set."""
    out = [
        f
        for f in (existing or [])
        if not (isinstance(f, dict) and f.get("type") == GATE_HOLD_FLAG_TYPE)
    ]
    return out or None


def has_gate_hold_flag(qa_flags: list | None) -> bool:
    """True iff `qa_flags` carries an LQA gate-hold entry (the export-refusal predicate's
    per-segment test, shared so no surface re-implements the type string)."""
    return any(
        isinstance(f, dict) and f.get("type") == GATE_HOLD_FLAG_TYPE for f in (qa_flags or [])
    )


def tag_damage_detail(source_text: str | None, target_text: str | None) -> dict | None:
    """`tag_damage`'s verdict PLUS the token delta, or None when clean.

    `{"reason": <the tag_damage string>, "missing": [...], "extra": [...]}`.

    THE ONE PREDICATE THE FULL_TEXT SURFACES CALL (GitHub #42). Both of them —
    `export.paragraph_texts_for`'s guard B and `fulltext_paragraphs._rows` — used to
    judge a paragraph with `Counter(find_placeholders(...))` alone. That multiset
    comparison is a STRICT SUBSET of `tag_damage`: it cannot see reordering, broken
    nesting, a residual `«TAG_N»` sentinel, or an EMPTIED PAIRED SPAN (every token
    present and correctly nested, the words moved out, the run and its rPr dropped at
    build time). Measured end to end on a real DOCX: the commit gate said "inline tag
    emptied span r2", the export shipped anyway, and paragraph 0 lost a formatted run
    while the split view reported `damaged_count=0`.

    `reason` is the AUTHORITATIVE field — it is `tag_damage`'s own string and is
    non-empty iff ANYTHING diverged (conservative quarantine semantics: any tag
    divergence is damage). `missing`/`extra` are DERIVED for the diagnosis rows so the
    UI can show WHICH tokens diverged, but they NEVER decide the verdict: a damage class
    whose delta is empty on both sides (emptied span, reorder, malformed nesting) is
    still damage, and `reason` is what names it."""
    reason = tag_damage(source_text, target_text)
    if not reason:
        return None
    from collections import Counter

    src_ct = Counter(segment.find_placeholders(source_text or ""))
    tgt_ct = Counter(segment.find_placeholders(target_text or ""))
    return {
        "reason": reason,
        "missing": sorted((src_ct - tgt_ct).elements()),
        "extra": sorted((tgt_ct - src_ct).elements()),
    }


def merge_number_flags(existing: list | None, reason: str) -> list:
    """qa_flags with any prior number_integrity entry replaced by one for `reason` (every
    other flag — refusal, qe, dnt, glossary, tag, nontranslation — kept). Fresh list each
    call so SQLAlchemy sees the JSON column as changed (same pattern as merge_tag_flags)."""
    out = [
        f
        for f in (existing or [])
        if not (isinstance(f, dict) and f.get("type") == NUMBER_FLAG_TYPE)
    ]
    out.append(number_flag(reason))
    return out


def clear_number_flags(existing: list | None) -> list | None:
    """qa_flags with every number_integrity entry REMOVED (every other flag kept).

    Mirrors `clear_tag_flags` / `qe.clear_qe_flag`, and exists for the same reason the
    export reviewer found for tags: a flag that is only ever ADDED records a verdict about
    text that no longer exists. A segment whose figure is corrected by a later commit
    would otherwise wear a MAJOR "number integrity broken" badge forever. Returns None
    when nothing is left, so the column reads exactly as it did before any flag was set."""
    out = [
        f
        for f in (existing or [])
        if not (isinstance(f, dict) and f.get("type") == NUMBER_FLAG_TYPE)
    ]
    return out or None


def char_limit_violation(text, limit) -> str:
    """The reason string when `text` exceeds `limit` characters, or "" when it fits or
    the limit is unset. "" is the clean/unset answer (mirrors `number_violation`).

    `limit` runs through `coerce_char_limit` FIRST, so None / non-integer / <= 0 return
    "" and this can never raise inside the write seam. Counting is NFC code points (see
    the section header). The reason carries both numbers so every surface renders the
    same fact: `"63 characters, limit 40"`."""
    lim = coerce_char_limit(limit)
    if lim is None:
        return ""
    n = char_count(text)
    if n <= lim:
        return ""
    return "%d characters, limit %d" % (n, lim)  # noqa: UP031


def char_limit_flag(reason: str, length: int, limit: int) -> dict:
    """One qa_flags entry for a value over its key's character limit — same
    {type,severity,message} shape as the number/dnt/glossary/tag flags, so every generic
    qa-cell loop (the CAT editor, the key grid) renders the badge with no template branch.
    'major': a clipped button label is customer-visible, not a style nit (spec section 8).
    `length`/`limit` ride the flag so a surface can show the numbers without re-counting."""
    return {
        "type": CHAR_LIMIT_FLAG_TYPE,
        "severity": "major",
        "message": "Over character limit: " + reason,
        "reason": reason,
        "length": int(length),
        "limit": int(limit),
    }


def merge_char_limit_flags(existing: list | None, reason: str, length: int, limit: int) -> list:
    """qa_flags with any prior char_limit entry replaced by one for `reason` (every other
    family — refusal, qe, dnt, glossary, tag, number, nontranslation — preserved). Fresh
    list each call so SQLAlchemy sees the JSON column as changed (the merge_number_flags
    pattern)."""
    out = [
        f
        for f in (existing or [])
        if not (isinstance(f, dict) and f.get("type") == CHAR_LIMIT_FLAG_TYPE)
    ]
    out.append(char_limit_flag(reason, length, limit))
    return out


def clear_char_limit_flags(existing: list | None) -> list | None:
    """qa_flags with every char_limit entry REMOVED (every other family kept).

    Mirrors `clear_number_flags`: this gate is ALWAYS ON, so a clean verdict is a full
    re-evaluation of the committed text — a char_limit flag from an earlier commit (of
    longer text, or before the key's limit was raised) now describes a value that no
    longer violates. Returns None when nothing is left, so the column reads exactly as it
    did before any flag was set; a fresh list otherwise."""
    out = [
        f
        for f in (existing or [])
        if not (isinstance(f, dict) and f.get("type") == CHAR_LIMIT_FLAG_TYPE)
    ]
    return out or None


def _segment_source_locale_lazy(segment):
    """A zero-argument lookup of `segment`'s project source locale (None when unknown), run
    only if a link actually differs. Memoised on the session like `_segment_rebuilds_escapes`."""

    def lookup():
        from sqlalchemy.orm import object_session

        from app.services.quality_part3 import segment_source_locale

        s = object_session(segment)
        if s is not None:
            with s.no_autoflush:
                return segment_source_locale(s, segment)
        from sqlmodel import Session

        from app.db import engine

        with Session(engine) as s2:
            return segment_source_locale(s2, segment)

    return lookup


def placeholder_violation(
    source_text,
    target_text,
    *,
    extended=False,
    segment=None,
    count_wording=True,
    source_locale=None,
    target_locale=None,
) -> str:
    """The reason string when the target's real-placeholder multiset diverges from the
    source's, or "" when they match. Reordering is silent (multiset); a dropped or added
    `{name}`/`%s` is named.

    ABSTAINS on an empty / whitespace-only target (reviewer H2), exactly like its siblings
    `number_violation` and `tag_damage`: an empty target is UNTRANSLATED -- the `empty`
    check owns it -- not a placeholder drop, and flagging it would stamp a MAJOR badge on
    every cleared segment (and no clear-set could reach it via a re-run over empty text).

    `segment` (optional, #703 gate): the Segment the pair belongs to. When its document's
    filter escapes raw markup characters on export (HTML family, MDX), an engine that
    writes `&lt;`/`&gt;`/`&amp;` as the raw character is harmless and is not flagged. Its
    `target_locale` also lets the ICU check judge a plural `one` branch (#716).

    `count_wording=False` (#716 gate) leaves that `one`-branch rule out: the message still
    compiles and shows the right text for most counts, so it is a review finding, not the
    runtime-breaking damage the export's rung 3 swaps for the SOURCE
    (`export.segment_tag_damage`).

    `source_locale` / `target_locale` (optional; the target defaults to the segment's, the
    source to its project's): a link whose only change is its language or locale part
    (`/en/help` -> `/fr/help`, `?lang=en` -> `?lang=fr`) passes (`link_locale`)."""
    if not (target_text or "").strip():
        return ""
    link_target = target_locale or getattr(segment, "target_locale", None)
    link_source = source_locale or (
        _segment_source_locale_lazy(segment) if segment is not None else None
    )
    locale = (
        getattr(segment, "target_locale", None) if segment is not None and count_wording else None
    )
    src_ct = _placeholder_multiset(source_text, extended=extended)
    tgt_ct = _placeholder_multiset(target_text, extended=extended)
    ent = entity_violation(
        source_text,
        target_text,
        escapes_rebuilt=segment is not None and _segment_rebuilds_escapes(segment),
    )
    icu = "; ".join(
        p
        for p in (
            icu_violation(source_text, target_text, locale),
            icu_format_violation(source_text, target_text),
            brace_violation(source_text, target_text),
            choice_violation(source_text, target_text),
            html_tag_violation(source_text, target_text, link_target, link_source),
            token_stray_brackets(source_text, target_text),
            md_link_violation(source_text, target_text, link_target, link_source),
        )
        if p
    )
    if src_ct == tgt_ct:
        return "; ".join(p for p in (ent, icu) if p)
    dropped = sorted((src_ct - tgt_ct).elements())
    added = sorted((tgt_ct - src_ct).elements())
    parts = []
    if dropped:
        parts.append("dropped " + ", ".join(dropped))
    if added:
        parts.append("added " + ", ".join(added))
    out = ("placeholder " + "; ".join(parts)) if parts else ""
    return "; ".join(p for p in (out, ent, icu) if p)


# #717: Java MessageFormat `choice` arguments and plain brace balance. Live ru-RU wrote one `}`
# too many after a choice argument (`MessageFormat` throws "Unmatched braces" when the message is
# loaded), fr-FR emptied the `0#` branch and moved its text in front of the argument; both were
# stored as clean MT. Brace balance is judged only when the source's braces balance (a quoted
# MessageFormat `'{'` or a lone brace in the source is the author's business).
_CHOICE_HEAD = re.compile(r"\{\s*([A-Za-z_\d][\w.]*)\s*,\s*choice\s*,")
_CHOICE_LIMIT = re.compile(r"\s*(-?(?:\d+(?:\.\d+)?|\u221e))\s*([#<\u2264])")
_NON_SPACE = re.compile(r"\S")


# #719: raw HTML in a resource string (JSON / YAML / .properties / .strings / PO values; document
# filters mask markup as ⟦⟧ tokens instead, so for them this never fires). Live ar-SA dropped the
# `href` of both links (`<a href="/terms">` -> `<a>`), stored as clean MT. Tags may move and their
# translatable attributes may change; a tag that disappears or appears, or a non-translatable
# attribute (`href`, `src`, `class`, ...) that changes, is damage.
_HTML_TAG = re.compile(r"<(/?)([A-Za-z][A-Za-z0-9-]{0,30})((?:\s[^<>]{0,2000})?)/?>")
_HTML_ATTR = re.compile(r"([A-Za-z_:][-\w:.]{0,60})(?:\s*=\s*(\"[^\"]*\"|'[^']*'|[^\s\"'>]+))?")
_HTML_ELEMENTS = frozenset(
    [
        "a",
        "abbr",
        "b",
        "bdi",
        "bdo",
        "big",
        "blockquote",
        "br",
        "button",
        "caption",
        "cite",
        "code",
        "dd",
        "del",
        "dfn",
        "div",
        "dl",
        "dt",
        "em",
        "font",
        "h1",
        "h2",
        "h3",
        "h4",
        "h5",
        "h6",
        "hr",
        "i",
        "img",
        "input",
        "ins",
        "kbd",
        "label",
        "li",
        "mark",
        "ol",
        "p",
        "pre",
        "q",
        "s",
        "samp",
        "small",
        "span",
        "strike",
        "strong",
        "sub",
        "sup",
        "table",
        "tbody",
        "td",
        "th",
        "thead",
        "time",
        "tr",
        "tt",
        "u",
        "ul",
        "var",
        "wbr",
    ]
)
# #719 gate: a line break the translation adds or drops is a layout choice (a translator
# rebalances lines for a longer or CJK text), not damage; a held target ships its SOURCE under
# #711, which is far worse than one line break more or less. Not counted at all.
_HTML_LINE_BREAKS = frozenset(("br", "wbr"))
_HTML_TEXT_ATTRS = frozenset(
    ("alt", "title", "placeholder", "label", "value", "summary", "content", "abbr")
)


def _html_tags(text: str) -> Counter:
    """Multiset of (closing?, tag name, non-translatable attributes) for every HTML tag
    (standard element names only: `<file>` in a CLI usage line is a translatable metavariable)."""
    out = Counter()
    if "<" not in text or len(text) > _ICU_MAX:
        return out
    for m in _HTML_TAG.finditer(text):
        if m.group(2).lower() not in _HTML_ELEMENTS or m.group(2).lower() in _HTML_LINE_BREAKS:
            continue
        attrs = []
        for a in _HTML_ATTR.finditer(m.group(3).rstrip("/ \t\r\n")):
            name = a.group(1).lower()
            if name in _HTML_TEXT_ATTRS or name.startswith("aria-"):
                continue
            attrs.append((name, (a.group(2) or "").strip("\"'")))
        out[(m.group(1), m.group(2).lower(), tuple(sorted(attrs)))] += 1
    return out


_HTML_VOID = frozenset(("br", "hr", "img", "input", "wbr"))


def _html_balanced(tags: Counter) -> bool:
    """Every non-void element of the source opens as often as it closes."""
    opens, closes = Counter(), Counter()
    for (closing, name, _attrs), n in tags.items():
        if name not in _HTML_VOID:
            (closes if closing else opens)[name] += n
    return opens == closes


def _tag_label(tag) -> str:
    closing, name, attrs = tag
    bits = "".join(f" {k}" + (f'="{v}"' if v else "") for k, v in attrs)
    return f"<{closing}{name}{bits}>"


_HTML_LT = re.compile(r"<|&lt;|&#0*60;|&#x0*3c;", re.IGNORECASE)
_HTML_GT = re.compile(r">|&gt;|&#0*62;|&#x0*3e;", re.IGNORECASE)


def _html_stray_brackets(source_text: str, target_text: str) -> str:
    """#725: with every tag intact, the engine sometimes writes one `>` too many after a closing
    tag (th `<b>...</b>>` in SRT / WebVTT cues, ja `<i>Starter</i>>` in a JSON string); the file
    then shows a literal `>`. More literal `<` / `>` outside the tags than the source has is
    flagged; fewer is not (`File > Save` may become `File → Save`).
    Gate: the escaped forms count as the same character (`&gt;` -> `>` renders identically and
    was a false hold; `</b>&gt;` is the same visible stray)."""

    def stray(text: str) -> tuple[int, int]:
        rest = _HTML_TAG.sub(" ", text)
        return len(_HTML_LT.findall(rest)), len(_HTML_GT.findall(rest))

    (s_lt, s_gt), (t_lt, t_gt) = stray(source_text), stray(target_text)
    added = [c for c, s, t in (("<", s_lt, t_lt), (">", s_gt, t_gt)) if t > s]
    return f"HTML stray {' '.join(repr(c) for c in added)} outside the tags" if added else ""


def _resolve_locale(value):
    """A locale given directly or as a zero-argument callable (a lazy source-locale lookup)."""
    return value() if callable(value) else value


def _html_tag_locale_only(src_tag, tgt_tag, target_locale, source_locale) -> bool:
    """Two tag keys that differ only in `href`, and the hrefs differ only in the language part."""
    (sc, sn, sa), (tc, tn, ta) = src_tag, tgt_tag
    if (sc, sn) != (tc, tn) or [k for k, _ in sa] != [k for k, _ in ta]:
        return False
    moved = False
    for (k, sv), (_k, tv) in zip(sa, ta, strict=True):
        if sv == tv:
            continue
        if k != "href" or not link_locale.url_locale_only(
            sv, tv, target_locale, _resolve_locale(source_locale)
        ):
            return False
        moved = True
    return moved


def html_tag_violation(source_text, target_text, target_locale=None, source_locale=None) -> str:
    """The reason when the target drops or adds an HTML tag of the source, or changes one of its
    non-translatable attributes (#719), else "". A link whose only change is its language or
    locale part (`/en/help` -> `/fr/help`) is not a change when the locale pair is given."""
    source_text, target_text = source_text or "", target_text or ""
    if "<" not in source_text or not target_text.strip():
        return ""
    src = _html_tags(source_text)
    if not src or not _html_balanced(src):
        return ""  # no markup, or the source's own markup is malformed (`<b>x</>`): not judged
    tgt = _html_tags(target_text)
    if src == tgt:
        return _html_stray_brackets(source_text, target_text)
    lost_keys, added_keys = list((src - tgt).elements()), list((tgt - src).elements())
    if target_locale and lost_keys and added_keys:
        lost_keys, added_keys = link_locale.pair_off(
            lost_keys,
            added_keys,
            lambda a, b: _html_tag_locale_only(a, b, target_locale, source_locale),
        )
        if not lost_keys and not added_keys:
            return _html_stray_brackets(source_text, target_text)
    lost = sorted(_tag_label(t) for t in lost_keys)
    added = sorted(_tag_label(t) for t in added_keys)
    parts = [f"dropped {', '.join(lost[:4])}"] if lost else []
    parts += [f"added {', '.join(added[:4])}"] if added else []
    return "HTML tag " + "; ".join(parts)


_TOKEN_LT = re.compile(r"(?:<|&lt;|&#0*60;|&#x0*3c;)⟦", re.IGNORECASE)
_TOKEN_GT = re.compile(r"⟧(?:>|&gt;|&#0*62;|&#x0*3e;)", re.IGNORECASE)


def token_stray_brackets(source_text, target_text) -> str:
    """#737 gate: #725's stray bracket (`<b>...</b>>`, live th-TH WebVTT) for a unit whose
    tags are `⟦⟧` tokens (WebVTT cues since #737, every document filter): a literal `<` / `>`
    (or its escaped form) written right against a token marker, more often than the source
    has one there. Only brackets touching a marker count, so a translation's own `a > b` or
    `File > Save` arrow is never judged."""
    source_text, target_text = source_text or "", target_text or ""
    if "⟦" not in source_text or "⟦" not in target_text:
        return ""
    added = [
        c
        for c, rx in (("<", _TOKEN_LT), (">", _TOKEN_GT))
        if len(rx.findall(target_text)) > len(rx.findall(source_text))
    ]
    return f"stray {' '.join(repr(c) for c in added)} next to a tag" if added else ""


# #736: Markdown link / image syntax is unit TEXT (md / mdx documents, and resource strings that
# carry Markdown: JSON / YAML docs strings), so the engine can damage it. Live de-DE wrote
# `[Einrichtungsschritte](#install-tools].` for `[setup steps](#install-tools)` (not a link any
# more), stored as clean MT and exported. Judged only when the SOURCE has at least one well-formed
# construct (no document-format test: a resource string carries Markdown as often as an .md
# file, and a pair with no link in its source is never judged). Link text, alt text and titles
# may change; a construct that disappears or appears, a destination or reference label that
# changes, or a `](` that no longer closes is damage.
_MD_TICKS = re.compile(r"`+")
_MD_ESCAPE = re.compile(r"\\[\\`\[\]()<>!]")
# `(` destination [title] `)`: a `<...>` destination or a run without spaces / brackets (one
# level of balanced parentheses, as in Wikipedia URLs); every part bounded (linear).
_MD_INLINE_TAIL = re.compile(
    r"\(\s{0,40}(<[^<>\n]{0,2000}>|[^\s()<>\[\]]{0,2000}(?:\([^\s()<>\[\]]{0,500}\)"
    r"[^\s()<>\[\]]{0,2000}){0,3})"
    r"(?:\s{1,40}(?:\"[^\"\n]{0,500}\"|'[^'\n]{0,500}'|\([^()\n]{0,500}\)))?\s{0,40}\)"
)
_MD_REF_TAIL = re.compile(r"\[([^\[\]\n]{0,999})\]")
_MD_BROKEN_DEST = re.compile(r"\(\s{0,40}([^\s()\[\]]{0,200})")
_MD_AUTOLINK = re.compile(r"<([A-Za-z][A-Za-z0-9+.-]{1,31}:[^\s<>]{1,2000})>")
_MD_SPACE = re.compile(r"\s+")
_MD_MAX_BRACKET = 1000  # link text longer than this is not a link (bounds the walk)


def _md_blank_code(text: str) -> str:
    """Code spans (`` `[x](y)` ``) and backslash escapes (`\\[`) blanked to spaces, same length.
    A run of N backticks opens a span that the next run of exactly N backticks closes
    (CommonMark 6.1); runs are paired through a per-length index, so a hostile string of
    backticks stays linear (a regex with a backreference was quadratic)."""
    text = _MD_ESCAPE.sub("  ", text)
    if "`" not in text:
        return text
    runs = [(m.start(), m.end()) for m in _MD_TICKS.finditer(text)]
    by_len: dict = {}
    for k, (a, b) in enumerate(runs):
        by_len.setdefault(b - a, []).append(k)
    out, k, last = [], 0, 0
    while k < len(runs):
        a, b = runs[k]
        same = by_len[b - a]
        j = bisect.bisect_right(same, k)
        if j == len(same):
            k += 1
            continue
        close = runs[same[j]][1]
        out.append(text[last:a])
        out.append(" " * (close - a))
        last = close
        k = same[j] + 1
    out.append(text[last:])
    return "".join(out)


def _md_links(text: str) -> tuple[Counter, Counter]:
    """(constructs, broken): a multiset of (kind, destination or `[label]`) for every inline link /
    image, full or collapsed reference link / image and URI autolink, and a multiset of the
    destinations of `[text](` openings that do not close (`](#install-tools].`)."""
    found, broken = Counter(), Counter()
    if len(text) > _ICU_MAX or ("[" not in text and "<" not in text):
        return found, broken
    clean = _md_blank_code(text)
    pairs, stack = {}, []
    for i, ch in enumerate(clean):  # one pass: every `[` paired with its own `]`
        if ch == "[":
            stack.append(i)
        elif ch == "]" and stack:
            o = stack.pop()
            if i - o <= _MD_MAX_BRACKET:
                pairs[o] = i
    for o, c in pairs.items():
        image = o > 0 and clean[o - 1] == "!"
        kind = "image" if image else "link"
        nxt = clean[c + 1 : c + 2]
        if nxt == "(":
            m = _MD_INLINE_TAIL.match(clean, c + 1)
            if m:
                found[(kind, m.group(1).strip("<>").strip())] += 1
            else:
                b = _MD_BROKEN_DEST.match(clean, c + 1)
                broken[b.group(1) if b else ""] += 1
        elif nxt == "[":
            # `arr[i][j]` in a code-ish string is not a reference link (a link opens on its own)
            start = o - 1 if image else o
            if start > 0 and (clean[start - 1].isascii() and clean[start - 1].isalnum()):
                continue
            m = _MD_REF_TAIL.match(clean, c + 1)
            if m:
                label = m.group(1) or clean[o + 1 : c]  # `[text][]` is labelled by its text
                found[(kind, "[" + _MD_SPACE.sub(" ", label).strip().casefold() + "]")] += 1
    for m in _MD_AUTOLINK.finditer(clean):
        found[("autolink", m.group(1))] += 1
    return found, broken


def _md_label(key) -> str:
    kind, dest = key
    if kind == "autolink":
        return f"<{dest}>"
    return ("!" if kind == "image" else "") + (
        f"[...]{dest}" if dest[:1] == "[" else f"[...]({dest})"
    )


_MD_EXCUSE_MAX = 64


def _md_unexcused(diff: Counter, other_text: str) -> list:
    """The constructs of `diff` that are real damage: an autolink whose URL appears in the other
    text (bare) is excused. Gate: one substring search per DISTINCT autolink, at most
    `_MD_EXCUSE_MAX` of them (a search per element was quadratic on the commit seam: 1.7 s for
    12k changed autolinks); past the cap an autolink is not excused (held for review)."""
    excused: dict = {}
    out = []
    for k in diff.elements():
        if k[0] == "autolink":
            if k[1] not in excused:
                excused[k[1]] = len(excused) < _MD_EXCUSE_MAX and k[1] in other_text
            if excused[k[1]]:
                continue
        out.append(k)
    return out


def _md_locale_only(a, b, target_locale, source_locale) -> bool:
    """Two Markdown constructs of one kind whose URL destinations differ only in the language
    part (a reference label `[x]` is not a URL and never qualifies)."""
    (ak, ad), (bk, bd) = a, b
    return (
        ak == bk
        and ak != "image"
        and ad[:1] != "["
        and link_locale.url_locale_only(ad, bd, target_locale, _resolve_locale(source_locale))
    )


def md_link_violation(source_text, target_text, target_locale=None, source_locale=None) -> str:
    """The reason when the target drops, adds or breaks a Markdown link / image of the source, or
    changes its destination or reference label (#736), else "". A destination whose only change
    is its language or locale part passes when the locale pair is given."""
    source_text, target_text = source_text or "", target_text or ""
    if ("](" not in source_text and "][" not in source_text and "<" not in source_text) or not (
        target_text.strip()
    ):
        return ""
    src, src_broken = _md_links(source_text)
    if not src:
        return ""  # no link in the source: not judged
    tgt, tgt_broken = _md_links(target_text)
    parts = []
    new_broken = sorted((tgt_broken - src_broken).elements())
    if new_broken:
        parts.append("broken " + ", ".join(f"]({d}" for d in new_broken[:4]))
    # `<url>` and a bare `url` are the same link in GitHub Markdown (and #700 writes the bracketed
    # form itself on export when the URL is glued to CJK text): either direction is not damage
    lost_keys = _md_unexcused(src - tgt, target_text)
    added_keys = _md_unexcused(tgt - src, source_text)
    if target_locale and lost_keys and added_keys:
        lost_keys, added_keys = link_locale.pair_off(
            lost_keys,
            added_keys,
            lambda a, b: _md_locale_only(a, b, target_locale, source_locale),
        )
    lost = sorted(_md_label(k) for k in lost_keys)
    added = sorted(_md_label(k) for k in added_keys)
    if lost:
        parts.append(f"dropped or changed {', '.join(lost[:4])}")
        if len(lost) > 4:
            parts.append(f"{len(lost) - 4} more")
    if added:
        parts.append(f"added {', '.join(added[:4])}")
        if len(added) > 4:
            parts.append(f"{len(added) - 4} more added")
    return ("Markdown link " + "; ".join(parts)) if parts else ""


def brace_violation(source_text, target_text) -> str:
    """The reason when the source's `{` / `}` balance and the target's do not (#717), else ""."""
    source_text, target_text = source_text or "", target_text or ""
    n_src = source_text.count("{")
    if not n_src or n_src != source_text.count("}") or not target_text.strip():
        return ""
    o, c = target_text.count("{"), target_text.count("}")
    return "" if o == c else f"braces unbalanced ({o} {{ vs {c} }})"


def _choices(text: str) -> dict:
    """{argument: [[(limit + op, branch has text), ...] per occurrence]} for every MessageFormat
    `{arg,choice,...}` argument (branches split on top-level `|`). One pass over the text."""
    out: dict = {}
    if "choice" not in text or len(text) > _ICU_MAX:
        return out
    close: dict = {}
    stack = []
    for i, c in enumerate(text):
        if c == "{":
            stack.append(i)
        elif c == "}" and stack:
            close[stack.pop()] = i
    for m in _CHOICE_HEAD.finditer(text):
        end = close.get(m.start())
        if end is None:
            continue
        # Linear (gate): walk only THIS argument's top-level text and JUMP over every nested
        # `{...}` via the matched-brace map, so a nested choice's text is walked once, by its
        # own head, never again by each enclosing one (nested heads were quadratic).
        branches, start, j = [], m.end(), m.end()
        while j <= end:
            ch = text[j] if j < end else "|"
            if ch == "{" and j < end:
                k = close.get(j)
                if k is None or k >= end:
                    break  # malformed nesting: stop here
                j = k + 1
                continue
            if ch == "|":
                # no slicing: a branch can hold a nested argument, and copying it per level
                # is quadratic again; the text test stops at the first non-space character.
                lm = _CHOICE_LIMIT.match(text, start, j)
                if lm:
                    has_text = _NON_SPACE.search(text, lm.end(), j) is not None
                    branches.append((lm.group(1) + lm.group(2), has_text))
                start = j + 1
            j += 1
        out.setdefault(m.group(1), []).append(branches)
    return out


def choice_violation(source_text, target_text) -> str:
    """The reason when a MessageFormat `choice` argument of the source changed its limits or
    lost the text of a branch in the target (#717), else ""."""
    source_text, target_text = source_text or "", target_text or ""
    if "choice" not in source_text or not target_text.strip():
        return ""
    src = _choices(source_text)
    if not src:
        return ""
    tgt = _choices(target_text)
    parts = []
    for name, occ in src.items():
        t_occ = tgt.get(name, [])
        if len(t_occ) < len(occ):
            parts.append(f"{{{name},choice}} missing")
            continue
        for s_br, t_br in zip(occ, t_occ, strict=False):
            if [k for k, _t in s_br] != [k for k, _t in t_br]:
                parts.append(f"{name} choice limits changed")
                continue
            emptied = [
                k for (k, s_t), (_k, t_t) in zip(s_br, t_br, strict=False) if s_t and not t_t
            ]
            if emptied:
                parts.append(f"{name} choice branch {', '.join(emptied)} emptied")
    return ("MessageFormat " + "; ".join(parts)) if parts else ""


# #709: ICU message syntax is plain text to the engine (Angular XLIFF, ARB, FormatJS JSON...):
# de-DE wrote `{VAR_SELECT, wählen, admin {..} editor {..} andere {..}}` for `select` / `other`,
# which every ICU runtime rejects. Each `{name, plural|select|selectordinal, ...}` header must
# survive; a select keeps its keys; a plural keeps `other` and its explicit `=N` branches (CLDR
# categories such as one / few / many may be added or dropped per language).
_ICU_HEAD = re.compile(r"\{\s*([A-Za-z_][\w.]*)\s*,\s*(plural|selectordinal|select)\s*,")
_ICU_KEY_NUM = re.compile(r"=\d+")
_ICU_MAX = 200_000  # a pathological input is not walked (the seam is synchronous)


def _icu_key(text: str, seg: int, i: int) -> str | None:
    """The selector key just before the branch brace at `i`: the last whitespace-separated
    token of text[seg:i] (`=N`, or the token without leading `=`/`:`). Scans back over that
    span only, so all keys of all headers cost one pass over the text."""
    j = i
    while j > seg and text[j - 1].isspace():
        j -= 1
    k = j
    while k > seg and not text[k - 1].isspace() and text[k - 1] not in "{}":
        k -= 1
    tok = text[k:j]
    p = 0
    while p < len(tok) and tok[p] in "=:":
        if _ICU_KEY_NUM.fullmatch(tok, p):
            return tok[p:]
        p += 1
    return tok[p:] or None


def _icu_args(text: str) -> dict:
    """{(name, type): [selector keys, ...] per occurrence} for every ICU argument header."""
    return {
        head: [[key for key, _start, _end in occ] for occ in occs]
        for head, occs in _icu_branches(text).items()
    }


def _icu_branches(text: str) -> dict:
    """{(name, type): [[(selector key, body start, body end), ...] per occurrence]} for every
    ICU argument header (``text[start:end]`` is the text between the branch's braces).

    Linear (gate fix): the matching brace of every `{` is found in one stack pass, and each
    header walks only its own top-level text, jumping over branch bodies, so nested or
    unclosed headers never rescan the same characters (the first version took 7 s on 24,000
    characters of repeated `{a, select, ` and grew quadratically)."""
    out: dict = {}
    if len(text) > _ICU_MAX:
        return out
    close: dict = {}
    stack = []
    for i, c in enumerate(text):
        if c == "{":
            stack.append(i)
        elif c == "}" and stack:
            close[stack.pop()] = i
    for m in _ICU_HEAD.finditer(text):
        keys, i, seg, n = [], m.end(), m.end(), len(text)
        while i < n:
            c = text[i]
            if c == "{":
                key = _icu_key(text, seg, i)
                j = close.get(i)
                if key:
                    keys.append((key, i + 1, j if j is not None else i + 1))
                if j is None:
                    break  # an unclosed branch: malformed, stop here
                i = seg = j + 1
                continue
            if c == "}":
                break
            i += 1
        out.setdefault((m.group(1), m.group(2)), []).append(keys)
    return out


@functools.lru_cache(maxsize=256)
def _one_counts(locale: str) -> tuple:
    """The integers 0..1000 other than 1 that the CLDR plural category `one` of `locale`
    covers (ru 21, 31, ...; fr and pt-BR 0; hi 0), empty for de, pt-PT, ja, zh or an unknown
    locale."""
    try:
        from babel import Locale

        rule = Locale.parse(locale.replace("-", "_")).plural_form
    except Exception:
        return ()
    return tuple(n for n in range(1001) if n != 1 and rule(n) == "one")


def _icu_one_fixed_count(name, src_text, s_br, tgt_text, t_br, locale) -> str:
    """#716: the source's `one` branch shows the count (`#`) and the target's `one` branch
    writes neither `#` nor `{name}` (the engine wrote a fixed `1`): wrong whenever the target
    language's `one` also covers another number (Russian 21, French 0) that no explicit `=N`
    branch of the target takes first."""
    s_one = next(((s, e) for k, s, e in s_br if k == "one"), None)
    t_one = next(((s, e) for k, s, e in t_br if k == "one"), None)
    if s_one is None or t_one is None or "#" not in src_text[s_one[0] : s_one[1]]:
        return ""
    body = tgt_text[t_one[0] : t_one[1]]
    if "#" in body or re.search(r"\{\s*" + re.escape(name) + r"\s*\}", body):
        return ""
    explicit = {int(k[1:]) for k, _s, _e in t_br if _ICU_KEY_NUM.fullmatch(k)}
    n = next((c for c in _one_counts(locale) if c not in explicit), None)
    if n is None:
        return ""
    return f"{name} one branch has no # ({locale} 'one' also covers {n})"


# #727: ICU / Java MessageFormat simple arguments format numbers, dates and times:
# `{amount, number, currency}`, `{d, date, short}`, `{0,number,integer}`. The TYPE and a
# predefined STYLE keyword are syntax; the engine translated them (live fr-FR
# `{progress, nombre, pourcentage}` in a Flutter ARB file) and the catalog no longer compiles.
# #709's check reads only plural / select / selectordinal headers. A custom pattern or skeleton
# style (`dd.MM.yyyy`, `::currency/EUR`) may be localised and is not judged. A plural
# `offset:N` keeps its value. `[^{}]` bodies keep every scan linear (one attempt per `{`).
# The style group is GREEDY up to the next brace (stripped afterwards): a lazy group followed
# by `\s*\}` backtracked quadratically over a long run of spaces.
_ICU_SIMPLE = re.compile(r"\{\s*([A-Za-z0-9_][\w.]*)\s*,\s*([^\s,{}]+)\s*(?:,([^{}]*))?\}")
_ICU_FORMAT_TYPES = frozenset(("number", "date", "time", "spellout", "ordinal", "duration"))
_ICU_STYLE_KEYWORDS = frozenset(
    ("integer", "currency", "percent", "short", "medium", "long", "full")
)
_ICU_OFFSET = re.compile(r"\{\s*([A-Za-z0-9_][\w.]*)\s*,\s*plural\s*,\s*offset\s*:\s*(\d+)")


def _icu_simple_args(text: str) -> Counter:
    """Multiset of (name, type, style) for every simple ICU argument; a style that is not a
    predefined keyword (a pattern or skeleton) reads as "*"."""
    out = Counter()
    for m in _ICU_SIMPLE.finditer(text):
        style = (m.group(3) or "").strip()
        key = style.lower() if style.lower() in _ICU_STYLE_KEYWORDS else ("*" if style else "")
        out[(m.group(1), m.group(2).lower(), key)] += 1
    return out


def icu_format_violation(source_text, target_text) -> str:
    """The reason when the target changes the type or predefined style of an ICU number / date
    / time argument, or a plural `offset:N`, of the source (#727), else ""."""
    source_text, target_text = source_text or "", target_text or ""
    if "," not in source_text or "{" not in source_text or not target_text.strip():
        return ""
    if len(source_text) > _ICU_MAX or len(target_text) > _ICU_MAX:
        return ""
    src = Counter(
        {k: n for k, n in _icu_simple_args(source_text).items() if k[1] in _ICU_FORMAT_TYPES}
    )
    parts = []
    if src:
        tgt = _icu_simple_args(target_text)
        # gate: one name index, not a scan of the target per changed argument (that was
        # quadratic on the commit seam: 3 s for 9k changed arguments), and at most 4 listed
        # like the sibling checks (the reason was unbounded: 440k characters).
        by_name: dict = {}
        for n, t, s in sorted(tgt):
            by_name.setdefault(n, f"{t}" + (f", {s}" if s and s != "*" else ""))
        changed = sorted((src - tgt).elements())
        for name, typ, style in changed[:4]:
            label = f"{{{name}, {typ}" + (f", {style}" if style and style != "*" else "") + "}"
            seen = by_name.get(name)
            parts.append(
                f"{label} changed" + (f" to {{{name}, {seen}}}" if seen else " or missing")
            )
        if len(changed) > 4:
            parts.append(f"{len(changed) - 4} more")
    src_off = {m.group(1): m.group(2) for m in _ICU_OFFSET.finditer(source_text)}
    if src_off:
        tgt_off = {m.group(1): m.group(2) for m in _ICU_OFFSET.finditer(target_text)}
        lost = [(n, o) for n, o in sorted(src_off.items()) if tgt_off.get(n) != o]
        parts += [f"{{{name}, plural}} offset:{off} lost" for name, off in lost[:4]]
        if len(lost) > 4:
            parts.append(f"{len(lost) - 4} more offsets lost")
    return ("ICU " + "; ".join(parts)) if parts else ""


def icu_violation(source_text, target_text, locale=None) -> str:
    """The reason when the target loses or changes an ICU argument header or its selector keys
    (#709), or (with the target `locale`) writes a fixed number for `#` in a plural `one`
    branch that language also uses for other counts (#716), else ""."""
    source_text, target_text = source_text or "", target_text or ""
    if "," not in source_text or not target_text.strip():
        return ""
    src_br = _icu_branches(source_text)
    if not src_br:
        return ""
    tgt_br = _icu_branches(target_text)
    src = {h: [[k for k, _s, _e in o] for o in occs] for h, occs in src_br.items()}
    tgt = {h: [[k for k, _s, _e in o] for o in occs] for h, occs in tgt_br.items()}
    parts = []
    for (name, kind), occ in src.items():
        t_occ = tgt.get((name, kind), [])
        if len(t_occ) < len(occ):
            parts.append(f"{{{name}, {kind}}} missing")
            continue
        if kind == "plural" and locale:
            for s_b, t_b in zip(src_br[(name, kind)], tgt_br[(name, kind)], strict=False):
                fixed = _icu_one_fixed_count(name, source_text, s_b, target_text, t_b, locale)
                if fixed:
                    parts.append(fixed)
        for s_keys, t_keys in zip(occ, t_occ, strict=False):
            if kind == "select":
                need = set(s_keys)
            else:
                need = {k for k in s_keys if k == "other" or k.startswith("=")}
            lost = sorted(need - set(t_keys))
            extra = sorted(set(t_keys) - set(s_keys)) if kind == "select" else []
            if lost or extra:
                bits = [f"dropped {', '.join(lost)}"] if lost else []
                bits += [f"added {', '.join(extra)}"] if extra else []
                parts.append(f"{name} keys {'; '.join(bits)}")
    return ("ICU " + "; ".join(parts)) if parts else ""


# #703: HTML character references written as literal text (a JSON / YAML / properties / PO
# value meant for innerHTML, an HTML mapping table). The engine unescaped `&lt;Enter&gt;` to
# `<Enter>` (an unknown tag: the word vanishes from the page) and turned `2&#162;` (cents)
# into `2 $` in 5 of 5 languages, all saved as clean MT. Two rules, both narrow:
#   * ESCAPE: a source `&lt;` / `&gt;` / `&amp;` (any spelling) the target writes as the RAW
#     character instead (fewer escapes AND more raw characters than the source).
#   * SYMBOL: a reference to a currency or other symbol character (Unicode Sc / So: ¢ € ©
#     ® ™ °) whose character the target carries in neither form.
# Typographic references (&nbsp; &quot; &hellip; &laquo; dashes) may be localised or dropped
# and are never flagged; a reference written as its own character (&euro;9 -> 9 €) passes.
_CHAR_REF_RE = re.compile(r"&(?:#[0-9]{1,7}|#[xX][0-9A-Fa-f]{1,6}|[A-Za-z][A-Za-z0-9]{1,31});")
_ESCAPE_CHARS = ("<", ">", "&")


def _char_refs(text: str) -> list[tuple[str, str]]:
    """(reference, decoded character) for every character reference that decodes to ONE
    character (an unknown name stays as written and is skipped)."""
    out = []
    for m in _CHAR_REF_RE.finditer(text or ""):
        ch = html.unescape(m.group())
        if len(ch) == 1 and ch != m.group() and ch != "�":
            out.append((m.group(), ch))
    return out


def _fmt_rebuilds_escapes(fmt: str | None) -> bool:
    """True when the filter for `fmt` escapes raw `<`, `>` and `&` in a target on export
    (its class sets `escapes_raw_text`): the HTML family re-escapes them as references and
    MDX backslash-escapes `<`, so a raw character in the target is not markup damage."""
    if not fmt:
        return False
    try:
        from app.filters.base import get_filter

        return bool(getattr(get_filter(fmt), "escapes_raw_text", False))
    except Exception:
        return False


def _segment_rebuilds_escapes(segment) -> bool:
    """`_fmt_rebuilds_escapes` for the document `segment` belongs to (a key segment, with
    no document, is written verbatim: False). Looked up without autoflushing the caller's
    pending writes and memoised on the session (`session.info`), so an approve that
    propagates to N repeats issues one query, not N (verify_repeat_propagation_617). A
    detached segment gets a short session of its own."""
    doc_id = getattr(segment, "document_id", None)
    if not doc_id:
        return False
    from sqlalchemy.orm import object_session
    from sqlmodel import select

    from app.models import Document

    stmt = select(Document.fmt).where(Document.id == doc_id)
    s = object_session(segment)
    if s is not None:
        memo = s.info.setdefault("quality_doc_fmt_703", {})
        if doc_id not in memo:
            with s.no_autoflush:
                memo[doc_id] = s.exec(stmt).first()
        fmt = memo[doc_id]
    else:
        from sqlmodel import Session

        from app.db import engine

        with Session(engine) as s2:
            fmt = s2.exec(stmt).first()
    return _fmt_rebuilds_escapes(fmt)


def entity_violation(source_text, target_text, *, escapes_rebuilt=False) -> str:
    """The reason when the target unescapes a markup escape or loses a symbol reference the
    source wrote as an HTML character reference (#703), else "". `escapes_rebuilt` (the
    document's filter escapes raw markup characters on export) turns the ESCAPE rule off;
    the SYMBOL rule still applies (a lost `&#162;` is a wrong price in any format)."""
    source_text, target_text = source_text or "", target_text or ""
    if "&" not in source_text or not target_text.strip():
        return ""
    src_refs, tgt_refs = _char_refs(source_text), _char_refs(target_text)
    if not src_refs:
        return ""
    src_raw, tgt_raw = _CHAR_REF_RE.sub("", source_text), _CHAR_REF_RE.sub("", target_text)
    parts = []
    for ch in () if escapes_rebuilt else _ESCAPE_CHARS:
        n_src = sum(1 for _r, c in src_refs if c == ch)
        if not n_src:
            continue
        n_tgt = sum(1 for _r, c in tgt_refs if c == ch)
        if n_tgt < n_src and tgt_raw.count(ch) > src_raw.count(ch):
            ref = next(r for r, c in src_refs if c == ch)
            parts.append(f"unescaped {ref} as a raw {ch}")
    seen = set()
    for ref, ch in src_refs:
        if ch in seen or unicodedata.category(ch) not in ("Sc", "So"):
            continue
        seen.add(ch)
        n_src = sum(1 for _r, c in src_refs if c == ch) + src_raw.count(ch)
        n_tgt = sum(1 for _r, c in tgt_refs if c == ch) + tgt_raw.count(ch)
        if n_tgt < n_src:
            parts.append(f"dropped {ref} (U+{ord(ch):04X})")
    return ("character reference " + "; ".join(parts)) if parts else ""


def placeholder_flag(reason: str) -> dict:
    """One qa_flags entry for a target whose real placeholders diverge from the source —
    same {type,severity,message} shape as the number/tag/dnt/glossary flags, so the CAT
    editor renders the badge through the SAME generic loop. 'major' (a dropped placeholder
    is a customer-visible / runtime-crashing defect, not a style nit)."""
    return {
        "type": PLACEHOLDER_FLAG_TYPE,
        "severity": "major",
        "message": "Placeholder integrity broken: " + reason,
        "reason": reason,
    }


def merge_placeholder_flags(existing: list | None, reason: str) -> list:
    """qa_flags with any prior placeholder_integrity entry replaced by one for `reason`
    (every other family preserved). Fresh list each call so SQLAlchemy sees the JSON
    column as changed (the merge_number_flags pattern)."""
    out = [
        f
        for f in (existing or [])
        if not (isinstance(f, dict) and f.get("type") == PLACEHOLDER_FLAG_TYPE)
    ]
    out.append(placeholder_flag(reason))
    return out


def clear_placeholder_flags(existing: list | None) -> list | None:
    """qa_flags with every placeholder_integrity entry REMOVED (every other family kept).

    Mirrors clear_number_flags: this gate is ALWAYS ON, so a clean verdict is a full
    re-evaluation of the committed text and a placeholder_integrity flag from an earlier
    commit now describes text that no longer exists. Returns None when nothing is left, so
    the column reads exactly as it did before any flag was set; a fresh list otherwise."""
    out = [
        f
        for f in (existing or [])
        if not (isinstance(f, dict) and f.get("type") == PLACEHOLDER_FLAG_TYPE)
    ]
    return out or None


def nonplaceholder_tag_divergence(source_text, target_text) -> list[str]:
    """The inline-tag tokens that diverge between source and target but are NOT governed by
    `placeholder_violation` -- i.e. HTML tags (`<b>`, `</a>`), URLs, and complex/ICU brace
    forms the placeholder gate deliberately excludes.

    The editor grid (reviewer L1) uses this to de-dupe the dropped-placeholder DOUBLE badge
    WITHOUT hiding a real HTML/URL loss: the live broad "tags" finding matches real
    placeholders too, so a segment with a persisted placeholder_integrity flag would show
    BOTH a "tags" MAJOR and a "placeholder_integrity" MAJOR naming the same drop. Suppress
    the "tags" badge ONLY when THIS returns empty (the placeholder verdict already says it
    all); when a `<b>`/URL also diverged this is non-empty and both badges must show. Uses
    the SAME extractor check_segment's "tags" finding uses (`_TAG_RE` via segment), minus
    the real-placeholder tokens, so it can never disagree with what that badge is about."""
    from collections import Counter

    src = Counter(segment.extract_inline_tags(segment.strip_placeholders(source_text or "")))
    tgt = Counter(segment.extract_inline_tags(segment.strip_placeholders(target_text or "")))
    diff = list((src - tgt).elements()) + list((tgt - src).elements())
    return [t for t in diff if not _SEAM_PLACEHOLDER_RE.fullmatch(t)]


def whitespace_flag(reason: str) -> dict:
    """One qa_flags entry for a whitespace-hygiene divergence — same {type,severity,
    message} shape as its siblings. 'minor': a spacing slip is cosmetic, not a value
    defect (so it never fails an LQA gate the way a major would)."""
    return {
        "type": WHITESPACE_FLAG_TYPE,
        "severity": "minor",
        "message": "Whitespace: " + reason,
        "reason": reason,
    }


def label_echo_stripped_flag(source: str | None = None) -> dict:
    """One qa_flags entry for a stripped prompt-label echo (#664): MINOR, informational. The
    text was repaired deterministically, so this only tells a reviewer it happened."""
    entry = {
        "type": scaffold.LABEL_ECHO_FLAG_TYPE,
        "severity": "minor",
        "message": "Label echo removed: " + scaffold.LABEL_ECHO_STRIPPED_REASON,
        "reason": scaffold.LABEL_ECHO_STRIPPED_REASON,
    }
    if source:
        entry["source"] = source
    return entry


def merge_label_echo_flag(existing: list | None, source: str | None = None) -> list:
    """qa_flags with any prior label_echo_stripped entry replaced by a fresh one."""
    out = [
        f
        for f in (existing or [])
        if not (isinstance(f, dict) and f.get("type") == scaffold.LABEL_ECHO_FLAG_TYPE)
    ]
    out.append(label_echo_stripped_flag(source))
    return out


def clear_label_echo_flag(existing: list | None) -> list | None:
    """qa_flags with every label_echo_stripped entry removed (None when empty)."""
    out = [
        f
        for f in (existing or [])
        if not (isinstance(f, dict) and f.get("type") == scaffold.LABEL_ECHO_FLAG_TYPE)
    ]
    return out or None


def merge_whitespace_flags(existing: list | None, reason: str) -> list:
    """qa_flags with any prior whitespace entry replaced by one for `reason` (every other
    family preserved). Fresh list each call (the merge_number_flags pattern)."""
    out = [
        f
        for f in (existing or [])
        if not (isinstance(f, dict) and f.get("type") == WHITESPACE_FLAG_TYPE)
    ]
    out.append(whitespace_flag(reason))
    return out


def clear_whitespace_flags(existing: list | None) -> list | None:
    """qa_flags with every whitespace entry REMOVED (every other family kept). Mirrors
    clear_number_flags: a clean re-evaluation drops a stale badge. None when empty."""
    out = [
        f
        for f in (existing or [])
        if not (isinstance(f, dict) and f.get("type") == WHITESPACE_FLAG_TYPE)
    ]
    return out or None


def _meta_candidate_spans(text: str) -> list[tuple[str, str]]:
    """The (KIND, span) pairs of `text` a translator note can occupy: parenthetical
    contents, bracketed contents, and the tail of a LINE from a note marker onward.
    Span-bounding is lock 1 — it is what stops an ordinary sentence that merely quotes
    first-person translation talk from being judged at all.

    The marker rule matches a marker ANYWHERE in the line, not only at its head. Two
    review findings needed that: a note whose parenthetical contains NESTED parens is
    invisible to `_META_PAREN_RE` (`[^()]` cannot span them) unless the line rule catches
    it, and a note wrapped in a comment delimiter ("<!-- Note: ... -->") never starts its
    line. Locks 2 and 3 still have to hold on the span, and the count-symmetry guard in
    `meta_commentary` bounds what the broader selection can cost."""
    if not text:
        return []
    spans = [("paren", m.group(1)) for m in _META_PAREN_RE.finditer(text)]
    spans += [("bracket", m.group(1)) for m in _META_BRACKET_RE.finditer(text)]
    for line in text.split("\n"):
        low = _meta_fold(line)
        at = min((low.find(m) for m in _META_NOTE_MARKERS if m in low), default=-1)
        if at >= 0:
            # `str.lower()` is not always length-preserving (U+0130 lowercases to TWO
            # code points), so an offset from the folded text can only be trusted back
            # onto the original when the lengths agree; otherwise take the whole line.
            spans.append(("note", line[at:] if len(low) == len(line) else line))
    return spans


def _is_meta_span(span: str) -> bool:
    """True when a candidate span is BOTH first-person about correcting/translating
    (lock 2) AND about the translation task itself (lock 3). Both are required — either
    alone is ordinary translatable content."""
    low = _meta_fold(span)
    first_person = any(p in low for p in _META_FIRST_PERSON) or bool(
        _META_FIRST_PERSON_GAPPED.search(low)
    )
    return first_person and any(v in low for v in _META_VOCAB)


def meta_commentary(source_text: str | None, target_text: str | None) -> str:
    """A short reason string when `target_text` carries a translator-note / model
    meta-commentary span, or "" when clean.

    Deterministic, LLM-free, and SAFE OFFLINE: MockProvider echoes the English source, so
    a target can only trip this when the SOURCE tripped it too — and the source-side
    keep-guard then abstains. That is also the correctness argument for real content: a
    document genuinely ABOUT translator notes translates its notes, and those are content,
    not commentary."""
    src_spans = _meta_candidate_spans(source_text or "")
    hits = [(kind, s) for kind, s in _meta_candidate_spans(target_text or "") if _is_meta_span(s)]
    if not hits:
        return ""
    if any(_is_meta_span(s) for _kind, s in src_spans):
        return ""  # the SOURCE is about translator notes -> real content
    # COUNT-SYMMETRY GUARD (adversarial review). The keep-guard above is a PHRASE test,
    # so it only fires for source languages the phrase lists cover — a genuine ja/zh/ru/pl
    # source parenthetical ("(I have translated this document)" in the original) sails
    # past it while its English translation trips locks 1-3, and correct content is
    # quarantined. This guard is language-agnostic instead: an APPENDED model note ADDS a
    # span of its kind, so the target carries MORE parentheticals / brackets / note lines
    # than the source. When the source already has at least as many spans of that kind,
    # the target's span is a TRANSLATION of one of them, not an addition — abstain.
    src_counts: dict[str, int] = {}
    tgt_counts: dict[str, int] = {}
    for kind, _s in src_spans:
        src_counts[kind] = src_counts.get(kind, 0) + 1
    for kind, _s in _meta_candidate_spans(target_text or ""):
        tgt_counts[kind] = tgt_counts.get(kind, 0) + 1
    hits = [(k, s) for k, s in hits if src_counts.get(k, 0) < tgt_counts.get(k, 0)]
    if not hits:
        return ""
    src_low = _meta_fold(source_text)
    hits = [
        (k, h) for k, h in hits if " ".join(_meta_fold(h).split()) not in " ".join(src_low.split())
    ]
    if not hits:
        return ""
    snippet = " ".join(hits[0][1].split())
    if len(snippet) > 120:
        snippet = snippet[:117] + "..."
    return META_COMMENTARY_REASON + ": " + snippet


def merge_source_fallback_flag(existing: list | None, indices) -> list | None:
    """qa_flags with any prior source_fallback entry replaced by one naming `indices`
    (every other flag preserved). An EMPTY `indices` removes the flag entirely — that is
    how a hand-repaired paragraph stops claiming to be untranslated. Fresh list each
    call so SQLAlchemy sees the JSON column as changed; None when nothing is left."""
    out = [
        f
        for f in (existing or [])
        if not (isinstance(f, dict) and f.get("type") == SOURCE_FALLBACK_FLAG_TYPE)
    ]
    idx = sorted({int(i) for i in (indices or [])})
    if idx:
        out.append(source_fallback_flag(idx))
    return out or None


def source_fallback_indices(existing: list | None) -> list[int]:
    """The paragraph indices a segment's source_fallback flag names ([] when clean).
    One reader so the editor, /api, the CLI and the TM refusal cannot drift."""
    for f in existing or []:
        if isinstance(f, dict) and f.get("type") == SOURCE_FALLBACK_FLAG_TYPE:
            try:
                return sorted({int(i) for i in (f.get("indices") or [])})
            except (TypeError, ValueError):
                return []
    return []


# --- #721: the space at a formatting-run split follows the source ----------------------------
# Word, PowerPoint, InDesign and InCopy paragraphs whose formatting changes between two words
# keep the space at the EDGE of a run (`Save changes before` + ` closing the window?`), carried
# as `before⟦/r1⟧⟦r2⟧ closing`. The engine reproduces the tags but often drops that space, and
# the export glues the words ("antes decerrar la ventana?", es/fr/de 3 of 3 in the soak).
# Deterministic repair at the write seam: for a run split (a token sequence with a closing AND
# an opening run token) that has whitespace right before or after it in the source, a target
# that writes letters / digits on both sides of the same split gets one space back on the
# source's side. Never inside a source word (a split with no whitespace in the source gives no
# hint).
# #721b: ONLY for target languages that write separate words with a space between them and do
# not glue words into compounds, attach particles or prefix clitics (_SPACED_LANGS). Elsewhere
# a glued split is often correct: German / Dutch / Finnish compounds (`Benutzer⟦/r2⟧⟦r3⟧name`,
# `gebruiker⟦/r2⟧⟦r3⟧naam`, `verkko⟦/r2⟧⟦r3⟧yhteys`), Korean particles (`파일⟦/r2⟧⟦r3⟧을`),
# Turkish / Hungarian suffixes, Arabic / Hebrew prefix clitics; the first version split all of
# those (side probe soak triage/compound-r9). Scripts written without spaces (Chinese, Japanese,
# Thai, Lao, Khmer, Burmese, Tibetan) are never in the list.
_RUN_SPLIT_RE = re.compile(r"(?:⟦/?r\d+⟧)+")
_RUN_OPEN_RE = re.compile(r"⟦r\d+⟧")
_SPACED_LANGS = frozenset(
    {
        # Romance
        "ca",
        "es",
        "fr",
        "gl",
        "it",
        "oc",
        "pt",
        "ro",
        # English, Irish, Welsh, Albanian, Greek
        "en",
        "ga",
        "cy",
        "sq",
        "el",
        # Slavic
        "be",
        "bg",
        "bs",
        "cs",
        "hr",
        "mk",
        "pl",
        "ru",
        "sk",
        "sl",
        "sr",
        "uk",
        # Baltic
        "lt",
        "lv",
        # Vietnamese, Hindi. NOT Indonesian / Malay (`-nya`, `-ku`, `-lah` attach to the word:
        # `akun⟦/r2⟧⟦r3⟧nya`) nor Tagalog / Filipino (the linker `-ng` attaches: `bago⟦/r⟧⟦r⟧ng`)
        # (#721b gate).
        "vi",
        "hi",
    }
)


def _no_space_script(ch: str) -> bool:
    """A character of a script written without spaces between words."""
    o = ord(ch)
    if 0xAC00 <= o <= 0xD7AF or 0x1100 <= o <= 0x11FF or 0x3130 <= o <= 0x318F:
        return False  # Hangul is spaced (Korean itself is not in _SPACED_LANGS: particles attach)
    if 0x0E00 <= o <= 0x0EFF or 0x1000 <= o <= 0x109F or 0x1780 <= o <= 0x17FF:
        return True  # Thai, Lao, Burmese, Khmer
    if 0x0F00 <= o <= 0x0FFF:
        return True  # Tibetan
    return unicodedata.east_asian_width(ch) in ("W", "F")


def _split_side(text: str, m) -> str:
    """'after' / 'before' when whitespace touches this run split in the source, else ''."""
    seq = m.group()
    if "⟦/" not in seq or not _RUN_OPEN_RE.search(seq):
        return ""
    after = text[m.end()] if m.end() < len(text) else ""
    before = text[m.start() - 1] if m.start() > 0 else ""
    if after.isspace():
        return "after"
    if before.isspace():
        return "before"
    return ""


def run_split_spacing(source_text, target_text, target_locale=None) -> str:
    """`target_text` with the space at each formatting-run split restored where the source
    has one and the target glued two words across it (#721). Unchanged otherwise."""
    if not source_text or not target_text or "⟦/r" not in target_text:
        return target_text
    lang = str(target_locale or "").replace("_", "-").split("-")[0].lower()
    if lang not in _SPACED_LANGS:
        return target_text
    sides = {}
    for m in _RUN_SPLIT_RE.finditer(source_text):
        side = _split_side(source_text, m)
        if side:
            sides[m.group()] = side
    if not sides:
        return target_text
    out: list[str] = []
    last = 0
    for m in _RUN_SPLIT_RE.finditer(target_text):
        side = sides.get(m.group())
        if not side:
            continue
        b = target_text[m.start() - 1] if m.start() > 0 else ""
        a = target_text[m.end()] if m.end() < len(target_text) else ""
        if not (b.isalnum() and a.isalnum()) or _no_space_script(b) or _no_space_script(a):
            continue
        cut = m.end() if side == "after" else m.start()
        out.append(target_text[last:cut])
        out.append(" ")
        last = cut
    if not out:
        return target_text
    out.append(target_text[last:])
    return "".join(out)
