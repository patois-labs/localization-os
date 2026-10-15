"""quality: shared leaf helpers (layered pure move)

Documented in: docs/architecture/quality-lqa-evaluations.md
"""

from __future__ import annotations

import re
import unicodedata

from app import config
from app.models import StringKey
from app.services import ai_tagging, qa, segment

DNT_FLAG_TYPE = "dnt_violation"


# --- deterministic DNT verification (SPEC-context-metadata phase 2B) ---------
# "Prompts persuade, gates enforce" (CONTEXT-METADATA-CROSS-INDUSTRY §1D): prompt-only
# DNT is probabilistic — a model can still translate a term the glossary said to keep.
# This is the deterministic GATE at the sole write seam: a cheap string check (no LLM
# call) that, for every applicable DNT term PRESENT in the source, verifies the term
# survived VERBATIM in the committed target. A miss flags the segment for a HUMAN
# (NEEDS_REVIEW + dnt_violation qa_flag) but does NOT block the write.
def dnt_flag(term: str) -> dict:
    """One qa_flags entry for a DNT term the model translated away — shaped like a
    qa.check_segment / refusal / qe flag ({type,severity,message}) so the CAT editor
    renders the badge with the same code path. 'major' (a reviewer should act)."""
    return {
        "type": DNT_FLAG_TYPE,
        "severity": "major",
        "message": f"Do-not-translate term not preserved verbatim in the target: {term!r}",
        "term": term,
        "reason": "dnt term translated away",
    }


def merge_dnt_flags(existing: list | None, terms) -> list:
    """qa_flags with any prior dnt_violation entries replaced by one entry per `term`
    (every NON-dnt flag — refusal, qe, structural — is preserved). A fresh list each
    call so SQLAlchemy sees the JSON column as changed (same pattern as
    refusal.merge_flag / qe.merge_qe_flag)."""
    out = [
        f for f in (existing or []) if not (isinstance(f, dict) and f.get("type") == DNT_FLAG_TYPE)
    ]
    for term in terms:
        out.append(dnt_flag(term))
    return out


def _term_present(term: str, text: str, case_sensitive: bool) -> bool:
    """True when `term` occurs in `text` as a WHOLE token (SPEC 2B / adversarial
    Finding 1).

    Boundary rule: only ASCII letters/digits count as "inside a word", so a match
    must not be flanked by an ASCII alnum on either side. This treats CJK, punctuation
    and whitespace as boundaries — the opposite failure of bare `\\b`, whose Unicode
    word class would FALSE-NEGATIVE an ASCII brand before a CJK particle
    (`\\biPhone\\b` fails in "iPhoneを" because を is a Unicode word char). So a short
    DNT token like "AI" is NOT matched inside "email"/"rail" (fixing the substring
    false-positive AND the masking false-negative), while "iPhone" still matches before
    a Japanese particle. Mirrors the `\\b`-style whole-word matching MockProvider's own
    glossary substitution already uses, generalized to be CJK-safe."""
    pattern = r"(?<![A-Za-z0-9])" + re.escape(term) + r"(?![A-Za-z0-9])"
    return re.search(pattern, text, 0 if case_sensitive else re.IGNORECASE) is not None


def _term_spans_strict(term: str, text: str, case_sensitive: bool) -> list:
    """Every (start, end) whole-token occurrence of `term` in `text`, using the SAME
    boundary rule as `_term_present` (the two cannot drift: presence == any span)."""
    pattern = r"(?<![A-Za-z0-9])" + re.escape(term) + r"(?![A-Za-z0-9])"
    flags = 0 if case_sensitive else re.IGNORECASE
    return [m.span() for m in re.finditer(pattern, text, flags)]


def standalone_source_terms(source_text: str, glossary_terms) -> set:
    """Indexes (into `glossary_terms`) of the dict entries that occur in `source_text`
    at least once OUTSIDE every strictly longer matched term (longest match wins, #663).

    "Colorado Trail" (DNT) and a generic `trail` -> `sentier`: the output keeps the proper
    name, and requiring `sentier` for the `trail` inside it is a false violation that
    holds a correct translation back from the CMS. A term whose every source occurrence
    sits inside a strictly longer matched term's occurrence is not an independent
    constraint on this segment; one with a single free occurrence still is. Equal-length
    terms never shadow each other. Covers terms merged across glossaries, which the
    per-glossary shadowing in `glossary_common._shadowed` cannot see."""
    spans: dict = {}
    shadowers: set = set()
    for i, t in enumerate(glossary_terms or ()):
        if not isinstance(t, dict):
            continue
        term = t.get("source_term") or ""
        if not term:
            continue
        sp = _term_spans_strict(term, source_text, bool(t.get("case_sensitive")))
        if sp:
            spans[i] = sp
            # Only an ENFORCEABLE term (DNT, or one with a target) may shadow another: an
            # informational entry with no target is never checked, so letting it hide a
            # shorter required term would silently drop a violation.
            if t.get("dnt") or t.get("target_term"):
                shadowers.add(i)
    active: set = set()
    for i, sp in spans.items():
        for s0, s1 in sp:
            inside = any(
                o0 <= s0 and s1 <= o1 and (o1 - o0) > (s1 - s0)
                for j, osp in spans.items()
                if j != i and j in shadowers
                for o0, o1 in osp
            )
            if not inside:
                active.add(i)
                break
    return active


def dnt_violations(source_text: str | None, target_text: str | None, glossary_terms) -> list:
    """The DNT source terms present in `source_text` but ABSENT from `target_text`
    (deterministic WHOLE-TOKEN check; honours each term's `case_sensitive`).

    Matching is whole-token on ASCII-alnum boundaries (see `_term_present`): CJK,
    punctuation and whitespace all count as boundaries, so a short DNT token ("AI",
    "EU", "SAP") is never matched inside a larger word ("email", "rail") on EITHER the
    source or target leg — that fixes both the substring false-positive (a clean
    translation flagged because the source's "email" contains "ai") and the masking
    false-negative (a genuinely dropped "AI" hidden because the target's "email"
    contains "ai"). Only entries with `dnt` truthy are considered; a term is checked
    only when it actually appears (as a whole token) in the source. Returns the
    offending source terms in glossary order, de-duplicated. Empty (no flag) whenever
    glossary_terms is falsy, either text is empty, or every applicable DNT term
    survived verbatim — so a clean translation's qa_flags/status are untouched."""
    if not glossary_terms or not source_text or not target_text:
        return []
    viol: list = []
    seen: set = set()
    active = standalone_source_terms(source_text, glossary_terms)
    for idx, t in enumerate(glossary_terms):
        if not (isinstance(t, dict) and t.get("dnt")):
            continue
        if idx not in active:
            continue
        term = t.get("source_term") or ""
        if not term or term in seen:
            continue
        cs = bool(t.get("case_sensitive"))
        if _term_present(term, source_text, cs) and not _term_present(term, target_text, cs):
            seen.add(term)
            viol.append(term)
    return viol


GLOSSARY_FLAG_TYPE = "glossary_violation"


# --- deterministic glossary-adherence verification (SPEC Track B / B1) --------
# The DNT gate above verifies the NEGATIVE glossary constraint ("keep this term
# verbatim"). This is its POSITIVE sibling: a normal glossary entry says "translate
# source_term AS target_term" — nothing checked, until now, that the required
# target_term actually LANDED in the output. Same philosophy ("prompts persuade,
# gates enforce"): a cheap, LLM-free string check at the sole write seam. A miss is
# a SOFT review flag (NEEDS_REVIEW + glossary_violation qa_flag), never a block —
# identical semantics to the DNT gate.
def glossary_flag(source_term: str, target_term: str) -> dict:
    """One qa_flags entry for a required glossary target term the model did not use —
    shaped like the dnt/qe/refusal flags ({type,severity,message}) so the CAT editor
    renders the badge through the same generic code path. 'major' (a reviewer should
    act), mirroring dnt_flag; the severity here is a display/triage cue only — the
    choke-point gate is soft (always NEEDS_REVIEW, never a block) regardless of it,
    and it is INDEPENDENT of lqa._score (which scores the IN-MEMORY error list inside
    score_text, never the LQAResult table or qa_flags — SPEC-wave3-collaboration §10)."""
    return {
        "type": GLOSSARY_FLAG_TYPE,
        "severity": "major",
        "message": f"Required glossary term {target_term!r} (for source term {source_term!r}) not present in "  # noqa: E501
        "the target",
        "source_term": source_term,
        "target_term": target_term,
        "reason": "required glossary target term missing",
    }


def merge_glossary_flags(existing: list | None, missed) -> list:
    """qa_flags with any prior glossary_violation entries replaced by one entry per
    `missed` (a {source_term,target_term} dict); every non-glossary flag (refusal, qe,
    dnt_violation, structural) is preserved. A fresh list each call so SQLAlchemy sees
    the JSON column as changed (same pattern as merge_dnt_flags / refusal.merge_flag)."""
    out = [
        f
        for f in (existing or [])
        if not (isinstance(f, dict) and f.get("type") == GLOSSARY_FLAG_TYPE)
    ]
    for m in missed:
        out.append(glossary_flag(m["source_term"], m["target_term"]))
    return out


_WORD_RE = re.compile(r"[^\W_]+", re.UNICODE)


_MIN_STEM = 5


_MIN_APPEND_STEM = 3


_AGREEMENT_SUFFIXES = {
    "es": ("es", "s"),
    "fr": ("s", "x"),
    "en": ("es", "s"),
    "*": ("es", "s"),
}


_STEM_SUFFIXES = {
    "es": (
        "iendo",
        "ando",
        "ar",
        "er",
        "ir",
        "es",
        "os",
        "as",
        "ue",
        "en",
        "an",
        "o",
        "a",
        "e",
        "s",
        "n",
    ),
    "fr": ("ent", "ons", "ez", "er", "ir", "re", "es", "e", "s", "x"),
    "en": ("ing", "es", "ed", "s"),
    "*": ("es", "s"),
}


# --- morphology-tolerant term presence (Q5, core-QA fix train 1) --------------
# The DNT gate (`dnt_violations`, above) stays EXACT — a do-not-translate term is defined
# as surviving VERBATIM, so any alteration is a real breach. This tolerance is for the
# POSITIVE adherence check ONLY, whose miss is a SOFT review flag. A live prod QA campaign
# FALSIFIED the "surface-only is fine, a miss is just a glance" rationale the old check
# carried: ~half of all glossary flags were legitimate INFLECTIONS of the required term
# on KNOWN-CORRECT text ("dispositivos" for required "dispositivo", the conjugation
# "Descargue" for "descarga", the conjugated multiword "Inicie sesión" for "iniciar
# sesión", fr "appareils" for "appareil"), flooding dense-glossary reviews AND (via Q6)
# stripping TM lock bands off the flagged segments.
#
# The matcher is a strict SUPERSET of `_term_present`: it matches whenever the exact
# whole-token check did (so nothing previously clean becomes flagged, and the CJK-safe
# brand behaviour is preserved VERBATIM) PLUS three deterministic, LLM-free, µs rungs
# applied per required WORD against each target token:
#   (1) EXACT (honouring case_sensitive; ACCENT-SENSITIVE) — the existing behaviour;
#   (2) APPEND agreement: one surface is the other plus a CLOSED plural/agreement suffix
#       (es/fr/en {s, es, x}) and the shorter is >= _MIN_APPEND_STEM chars. Left-anchored
#       and closed-suffix, so "dispositivo"->"dispositivos" and "appareil"->"appareils"
#       pass, while "sol"->"solución" (open, non-suffix tail) and "carga"/"cargo"
#       (neither is a prefix of the other) do NOT. Bare gender vowels are deliberately
#       NOT append suffixes ("o"/"a" would false-match "mes"->"mesa"); gender rides (3);
#   (3) STEM equality: strip a small ordered per-language inflectional suffix set from
#       both surfaces and compare, requiring the shared stem to be >= _MIN_STEM chars.
#       This accepts verb conjugations and vowel-final agreement swaps ("descarga"/
#       "descargue" share stem "descarg"=7; "iniciar"/"inicie" share "inici"=5;
#       "blanco"/"blanca" share "blanc"=5) while the length FLOOR rejects the required
#       precision case: "carga"/"cargo" share only "carg"=4 (< floor), so a required
#       "carga" is NOT satisfied by "cargo".
#
# ACCENT FOLDING (F8) — rungs (2) and (3) ONLY. Before the append/stem comparison both
# surfaces are accent-folded (NFD, drop combining marks), which closes the dominant
# remaining false-flag class: "botón"/"botones", "sesión"/"sesiones", "acción"/"acciones"
# and the "-ó" preterites, whose accented head would otherwise defeat the left-anchored
# startswith and the stem compare. Rung (1) stays ACCENT-SENSITIVE, so under the strict /
# punctuation / `exact` paths an accent-different form still requires its exact accents.
# Inputs are NFC-normalized first (F13) so a precomposed "é" (U+00E9) and a decomposed
# "e"+U+0301 can never get different verdicts.
#
# WELL-FORMEDNESS GUARD (F1) — a required term carrying ANY non-word/non-space character
# (C++, .NET, Wi-Fi, %s, AT&T, Node.js, e-mail, d'aujourd'hui) is matched STRICTLY: the
# `_WORD_RE` tokenizer STRIPS punctuation, so without this guard a required "C++" would be
# satisfied by a bare "c", "%s" by "s", "AT&T" by "at t" and "Node.js" by "node js". Such
# a term takes ONLY the exact `_term_present` check above; the tolerant rungs never see it.
# Detected by comparing the concatenated word-tokens to the whitespace-stripped term.
# R5 (consequence, documented): a HYPHEN- or APOSTROPHE-bearing required term (e-mail,
# Wi-Fi, d'aujourd'hui) is therefore matched at its EXACT surface with NO plural/inflection
# tolerance -- its own punctuation trips this guard, so "e-mails" does NOT satisfy required
# "e-mail". This is deliberately conservative (such terms are usually brands/technical
# tokens). OPTIONAL FUTURE CARVE-OUT: were a real surface to need it, a hyphenated term
# could be split on its separators and each component inflection-matched in order (like a
# multiword term) -- not built, because no authored glossary has needed it.
#
# PRECISION TRADEOFF (documented): _MIN_STEM is 5 — the exact value the fixtures force
# (the shortest form that MUST match, "inici", is 5; the shortest that MUST NOT, "carg",
# is 4). The accepted consequence is BROADER than "a trailing-vowel swap": stripping a
# per-language inflectional suffix from BOTH surfaces means ANY two same-root lemmas whose
# >=5-char stems coincide satisfy each other — INCLUDING noun<->verb pairs an es glossary
# routinely separates ("archivo"/"archivar" share "archiv"=6; "registro"/"registrar"
# share "registr"=7; "cuenta"/"cuento" share "cuent"=5). This is a deliberate looseness:
# the consequence is still a SOFT reviewer glance, never a block or a rewrite, and the
# campaign's dominant failure mode was OVER-flagging. A term may opt back into strict
# surface matching per-entry via a truthy `exact` — a forward hook for glossary metadata;
# NO surface sets it yet (the field is accepted and honoured, not yet authored anywhere).
# case_sensitive keeps its meaning: a case-sensitive entry is compared WITHOUT case
# folding, so the append/stem rungs (whose suffix tables are lowercase) collapse to
# near-exact matching — the conservative behaviour brand-like entries want.
# R8 (honesty note): case_sensitive suppresses ONLY case folding, NOT the F8 ACCENT fold —
# `_fold_accents` still runs on the append/stem rungs for a case-sensitive entry, so a
# case-sensitive "Botón" still matches "Botones". If a surface needs accent-strict matching
# too, that is the `exact` opt-out's job (it returns before every tolerant rung), not
# case_sensitive's.
# Closed plural/agreement suffixes for the APPEND rung (NEVER bare gender vowels).
# Ordered LONGEST-FIRST inflectional suffixes for the STEM rung (a single longest strip).
def _lang_family(locale: str | None) -> str:
    """The suffix-table key for a BCP-47-ish `locale` ('es-ES' -> 'es'); '*' (generic —
    append/plural tolerance only) when unknown or unsupported, so a caller that passes no
    locale still gets SAFE plural tolerance and never a wrong-language stem."""
    if not locale:
        return "*"
    lang = str(locale).replace("_", "-").split("-", 1)[0].lower()
    return lang if lang in _STEM_SUFFIXES else "*"


def _fold_accents(word: str) -> str:
    """`word` with combining accents removed (NFD, drop Unicode category Mn). Used by the
    APPEND and STEM rungs ONLY (F8) — the exact rung stays accent-sensitive. Latin accents
    are single combining marks, so length is preserved (é -> e), keeping the rungs' length
    floors meaningful."""
    return "".join(c for c in unicodedata.normalize("NFD", word) if unicodedata.category(c) != "Mn")


def _word_stem(word: str, lang: str) -> str:
    """`word` with its single LONGEST matching inflectional suffix stripped (per the
    ordered per-language table), leaving at least one char. No folding — the caller has
    already applied the case rule."""
    for suf in _STEM_SUFFIXES.get(lang, _STEM_SUFFIXES["*"]):
        if len(word) - len(suf) >= 1 and word.endswith(suf):
            return word[: len(word) - len(suf)]
    return word


TAG_FLAG_TYPE = "tag_integrity"


# --- deterministic inline-tag integrity verification (AI Tagging 1.2) ---------
# The DNT/glossary gates above verify TERMINOLOGY. This is the sibling that verifies
# INLINE-FORMATTING TOKEN integrity — the `⟦rN⟧ ⟦gN⟧ ⟦xN⟧` placeholders the filters
# use for DOCX runs / XLIFF+HTML inline elements. Detection has always existed
# (qa.check_segment), but display-only: it never persisted, never gated a write. This
# is the ENFORCEMENT the proposal's central gap asks for, run at the SOLE write seam,
# ALWAYS ON (the verification honesty backstop is never gated off — only the mask
# layer toggles, see ai_tagging.enabled).
#
# HARD DIVERGENCE FROM DNT/GLOSSARY (which are SOFT — flag but leave tm_eligible and
# never withhold): tag damage MIRRORS THE NONTRANSLATION GATE — it forces
# tm_eligible=False AND connector-withheld (via NEEDS_REVIEW), because a tag-broken
# target must never seed TM nor push to a live connector (a dropped run token
# collapses whole-paragraph formatting at export). The text is still committed (a
# reviewer must see it) — it is just downgraded, kept out of TM, and withheld from
# push, exactly like a suspected non-translation.
#
# RESTORED FORMS ONLY (HIGH-1): this compares `segment.find_placeholders(source)` vs
# the committed target — both RESTORED `⟦⟧` (the mask layer restores before the write
# reaches here), so the check sees real tokens, never `«TAG_N»` sentinels.
def tag_damage(source_text: str | None, target_text: str | None) -> str:
    """A human-readable reason string when `target_text`'s inline-fidelity `⟦⟧` tokens
    diverge from `source_text`'s — dropped / added / duplicated / reordered tokens, a
    residual `«TAG_N»` masking sentinel, or malformed (crossed / unbalanced) nesting.
    "" (clean) when the token multiset AND order match, no sentinel survives, and the
    nesting is well-formed.

    Deterministic, LLM-free, µs. BYTE-IDENTITY GUARANTEE: a source with no tokens and
    a target with no tokens returns "" (no flag, no state change), so a tagless
    translation is untouched. Runs on RESTORED forms (the mask layer restores before
    the write reaches the seam), so it always sees real `⟦⟧` tokens."""
    tgt = target_text or ""
    # A surviving masking sentinel is unconditional damage (never ship `«TAG_N»`).
    if ai_tagging.has_residual_sentinel(tgt, source_text or ""):
        return "residual masking sentinel in target"
    src_ph = segment.find_placeholders(source_text or "")
    tgt_ph = segment.find_placeholders(tgt)
    # #693: a free-order point token (`⟦pN⟧`, a numbered / named placeholder) may move;
    # only the remaining tokens' order is judged. Drops / adds / duplicates still count.
    if not segment.same_token_order(src_ph, tgt_ph):
        from collections import Counter

        src_ct, tgt_ct = Counter(src_ph), Counter(tgt_ph)
        dropped = sorted((src_ct - tgt_ct).elements())
        surplus = tgt_ct - src_ct
        added = sorted(t for t in surplus if src_ct.get(t, 0) == 0 for _ in range(surplus[t]))
        duplicated = sorted(t for t in surplus if src_ct.get(t, 0) > 0)
        parts = []
        if dropped:
            parts.append("dropped " + ", ".join(dropped))
        if added:
            parts.append("added " + ", ".join(added))
        if duplicated:
            parts.append("duplicated " + ", ".join(duplicated))
        if not parts and src_ct == tgt_ct:
            parts.append("reordered " + ", ".join(sorted(set(segment.order_significant(src_ph)))))
        return "inline tag " + "; ".join(parts)
    # Multiset+order match: the only remaining damage is broken nesting. Only judge
    # the target when the SOURCE is itself well-formed (filters always emit well-formed
    # nesting, but never flag a target for a malformed source we can't have caused).
    if segment.placeholders_well_formed(source_text or "") and not segment.placeholders_well_formed(
        tgt
    ):
        return "malformed tag nesting"
    # EMPTIED PAIRED SPAN — the damage class every check above is structurally blind to.
    # Token identity, multiset, ORDER and nesting are ALL preserved by a target that
    # simply moves the words OUT of a pair: `⟦r2⟧Acme Pro⟦/r2⟧` -> `⟦r2⟧⟦/r2⟧`, with the
    # words re-homed into a neighbouring span. At build time an empty span is DROPPED
    # (`docx_filter._set_paragraph_text` skips it), so the run and its rPr snapshot
    # evaporate and the neighbour absorbs the words wearing the NEIGHBOUR's formatting.
    # Measured live (llama-3-8b, fr-CA): the model moved "export pipeline" out of its
    # bold span leaving `⟦r4⟧⟦/r4⟧`, and the delivered DOCX lost the bold — while
    # `tag_damage` returned "" and the export guard, which keys on this verdict, waved
    # it through. One firing in 16 tagged segments and it was a TRUE positive.
    #
    # Judged on the SOURCE side too: a pair whose source body is ALREADY blank (or
    # whitespace-only) is legitimate and must not be flagged, so only a non-blank source
    # body that came back blank counts. The body comparison is deliberately the LAST
    # rung — the cheaper structural checks above give better messages when they apply.
    if segment.placeholders_well_formed(source_text or ""):
        src_bodies = segment.paired_span_bodies(source_text or "")
        tgt_bodies = segment.paired_span_bodies(tgt)
        emptied = sorted(
            name
            for name, body in src_bodies.items()
            if body.strip() and not (tgt_bodies.get(name) or "").strip()
        )
        if emptied:
            return "inline tag emptied span " + ", ".join(emptied)
    # MALFORMED MARKER RESIDUE (GitHub #43, root cause C2) — the damage class every
    # rung above is structurally blind to, and the LAST rung deliberately: the cheaper,
    # sharper checks give better messages wherever they apply, so placing this after
    # all of them keeps their verdicts (and their wording) intact.
    #
    # Each check above is expressed in TOKENS, and `segment.find_placeholders` only
    # ever matches a WELL-FORMED `⟦/?[a-z]?\d+⟧`. So a model that was shown the ⟦⟧
    # notation and imitated it BADLY produces output the whole gate reads as tagless:
    # measured live on prod v37 (project 12, local qwen3.5-9b), `Acme Workspace
    # ⟦r1{limit}⟧/r1`, `kind: "UI" ⟦x1...` and `⟦R1⟧` each reported ZERO tokens,
    # tag_damage "", and landed as clean `mt` with NO qa_flags — English source plus
    # invented debris, presented to the customer as a finished machine translation.
    #
    # THE COMPARISON IS SYMMETRIC (adversarial review P11). The first cut scoped this to
    # a MARKER-FREE SOURCE, which left a real blind spot one rung wide: a source that
    # DOES carry tokens, whose target reproduces those same tokens correctly AND adds
    # malformed debris beside them, passes every multiset/order/nesting check above and
    # then abstained here — clean verdict, debris delivered. So instead of abstaining we
    # measure BOTH sides with the same crude ruler: strip every well-formed token, count
    # the bracket characters that survive, and flag only when the TARGET carries more of
    # them than its source did. That keeps the conservatism that mattered (a source whose
    # own text is full of stray brackets can license the same number in its target — we
    # cannot have caused those) while closing the asymmetric case, which is the one that
    # leaks internal notation to a customer.
    #
    # NOT byte-identical, by design and narrowly: the ONLY newly-flagged shape is a
    # target with MORE stray marker characters than its source. A target with no
    # malformed debris has `tgt_res <= src_res` and its verdict is unchanged.
    src_residue = segment.marker_residue(source_text or "")
    tgt_residue = segment.marker_residue(tgt)
    if len(tgt_residue) > len(src_residue):
        if not segment.has_marker_chars(source_text or ""):
            return (
                "inline tag malformed marker residue in target (the source has "
                "none): " + _residue_excerpt(tgt)
            )
        return (
            "inline tag malformed marker residue in target (more stray marker "
            "characters than the source): " + _residue_excerpt(tgt)
        )
    return ""


_RESIDUE_EXCERPT_CAP = 80


# How much of the offending target a residue verdict quotes. The reviewer has the full
# target in front of them; the flag only has to point AT the debris, and an unbounded
# quote of a runaway generation would bloat qa_flags (a JSON column) without adding
# information.
def _residue_excerpt(text: str) -> str:
    """A bounded, whitespace-flattened quote of the target around its FIRST stray
    bracket character, for the residue verdict's message."""
    flat = " ".join((text or "").split())
    idx = min((flat.find(c) for c in segment.MARKER_CHARS if c in flat), default=0)
    start = max(0, idx - 20)
    frag = flat[start : start + _RESIDUE_EXCERPT_CAP]
    return (
        ("..." if start else "")
        + frag
        + ("..." if start + _RESIDUE_EXCERPT_CAP < len(flat) else "")
    )


STEP_ERROR_FLAG_TYPE = "step_error"


# --- per-segment STEP FAILURE (GitHub #45) -----------------------------------
# A workflow step that fails for ONE segment used to fail the WHOLE run, and the
# all-or-nothing write-back then discarded every value the run had already produced for
# that locale (prod run #74: one over-long fix prompt -> APIError -> a 100-key namespace
# delivered nothing). The orchestrator now keeps that segment's PRE-STEP text and routes
# it to a human instead; this is the flag that says so. Same {type,severity,message}
# shape as the dnt/glossary/tag/refusal flags, so the CAT editor renders the badge
# through the SAME generic code path.
#
# 'major': the segment holds a REAL translation (the step before the failure produced
# it), it simply never received the improvement the run promised — a reviewer must
# decide whether to accept it as-is or re-run.
def step_error_flag(step_name: str, error: str | None = "") -> dict:
    """One qa_flags entry for a segment whose step call FAILED while its siblings
    succeeded. Names the step (a run can have several) and carries the provider's own
    message, so the operator sees WHY without opening the run trace."""
    detail = " ".join(str(error or "").split())[:200]
    return {
        "type": STEP_ERROR_FLAG_TYPE,
        "severity": "major",
        "message": (
            "Step {!r} failed for this segment; the text it was editing was "
            "KEPT and the segment routed to review.{}".format(
                step_name or "(unnamed)", (" Engine error: " + detail) if detail else ""
            )
        ),
        "step": step_name or "",
        "reason": detail,
    }


def merge_step_error_flags(existing: list | None, flags) -> list:
    """qa_flags with any prior step_error entries replaced by `flags`; every OTHER
    family (refusal, qe, dnt, glossary, tag, nontranslation) is preserved. A fresh list
    each call so SQLAlchemy sees the JSON column as changed (the merge_dnt_flags /
    refusal.merge_flag / qe.merge_qe_flag pattern)."""
    out = [
        f
        for f in (existing or [])
        if not (isinstance(f, dict) and f.get("type") == STEP_ERROR_FLAG_TYPE)
    ]
    out.extend(flags)
    return out


def clear_step_error_flags(existing: list | None) -> list | None:
    """qa_flags with every step_error entry REMOVED (every other flag kept).

    Mirrors `clear_tag_flags` / `clear_number_flags` / `qe.clear_qe_flag`, and lands for
    the same reason (adversarial review P4, the stale-badge class): a family that is only
    ever ADDED records a verdict about text that no longer exists. A segment whose fix
    step errored once would otherwise wear a MAJOR "step failed for this segment" badge —
    and, because the write-back re-asserts NEEDS_REVIEW off it, stay permanently
    undeliverable — even after a later CLEAN run replaced the very text the flag is
    about. Returns None when nothing is left, so the column reads exactly as it did
    before any flag was set; a fresh list otherwise so SQLAlchemy sees the JSON column as
    changed."""
    out = [
        f
        for f in (existing or [])
        if not (isinstance(f, dict) and f.get("type") == STEP_ERROR_FLAG_TYPE)
    ]
    return out or None


GATE_HOLD_FLAG_TYPE = "lqa_gate_hold"


GATE_HOLD_CAUSE_NO_PROGRESS = "no_progress"


GATE_HOLD_CAUSE_LOOP_EXHAUSTED = "loop_exhausted"


GATE_HOLD_CAUSE_VISIT_CAP = "visit_cap"


GATE_HOLD_CAUSE_UNREPAIRED = "unrepaired"


GATE_HOLD_CAUSE_JUDGE_UNAVAILABLE = "judge_unavailable"


GATE_HOLD_FAMILY_LQA = "lqa"


GATE_HOLD_FAMILY_QE = "qe"


# ISSUE #95 (fix round 1, gate C H1/H2/H3) -- THE LQA GATE HOLD FLAG.
#
# A classic run whose correction loop ENDS with the gate's standing verdict still FAILED
# (the no-progress guard carried a FAIL forward, the loop-back was refused because its
# target sat at its max_visits ceiling, or the gate itself reached its own ceiling) must
# not deliver that text as finished work. In a classic workflow there is no "deliver step"
# to withhold -- delivery IS the post-loop segment write-back -- so the withholding has to
# happen on the SEGMENT: every unit this run produced carries this flag and lands
# NEEDS_REVIEW, and `export.gate_held_reason` refuses the document export (409) while any
# flagged unit is still unreviewed.
#
# 'critical': unlike `step_error` (the segment holds a real translation that simply missed
# an improvement), this text was JUDGED and FAILED, and the loop that was supposed to
# repair it gave up. A human must look before it ships.
#: THE GATE-HOLD CAUSES -- the canonical strings, here rather than in `orchestrator`
#: because THREE readers need them and only one of them is the orchestrator: the run
#: surface (which records the hold), this flag (which the reviewer sees on the unit) and
#: the EXPORT refusal (`export._gate_held_reason_for_docs`, which scans qa_flags and never
#: sees the run). Gate C round 3, M2: the refusal described the round-1 endings for every
#: cause, telling an operator a judge FAILED text that no judge had judged.
#: THE GATE-HOLD FAMILIES -- which KIND of check recorded the hold. Gate C round 4, M2: the
#: RELEASE was scoped to `lqa` rows while the HOLD was not, so a hold recorded by a QE-gated
#: step's own correction loop (ERRATUM E1) had no non-human way out: its own workflow's clean
#: re-run wrote a QE verdict row the release refused to read. The family rides the flag (and
#: the run-level record) so the release can match the family that held: `lqa` releases every
#: hold, and a `qe` hold is ALSO released by a passing QE verdict over the same text.
#: Absent/empty on a flag written before round 4 -> read as `lqa` (the historic scope).
def gate_hold_families(qa_flags: list | None) -> frozenset:
    """The gate-hold FAMILIES on a unit's badges (`{"lqa"}`, `{"qe"}`, ... ), empty when the
    unit carries no gate-hold badge. A badge with no recorded family (written before round 4)
    reads `lqa`, which is exactly the scope those badges were released under."""
    out = set()
    for f in qa_flags or []:
        if isinstance(f, dict) and f.get("type") == GATE_HOLD_FLAG_TYPE:
            out.add(str(f.get("family") or "") or GATE_HOLD_FAMILY_LQA)
    return frozenset(out)


def gate_hold_flag(
    step_name: str,
    reason: str | None = "",
    cause: str | None = "",
    family: str | None = "",
) -> dict:
    """One qa_flags entry for a segment produced by a run that ENDED on a FAILED gate.
    Names the gate step and why the loop stopped, so the reviewer sees the verdict and the
    reason on the segment itself rather than only in the run trace.

    `cause` is the orchestrator's machine-readable ending (`unrepaired`, `loop_exhausted`,
    `no_progress`, `visit_cap`, `judge_unavailable`) -- carried on the flag (gate C round 3,
    M2) so the EXPORT refusal, which scans qa_flags and never sees the run, can describe
    the ending it is actually refusing instead of quoting the round-1 one at every operator.
    Absent/empty on a flag written before round 3, which the export reads as "unknown" and
    answers with its neutral sentence.

    `family` is the KIND of check that held (gate C round 4, M2) -- `lqa` for a document
    check, `qe` for a QE-gated step whose own correction loop ended. The release matches it
    (`orchestrator._gate_release_mask`), so the QE hold E1 records is releasable by the very
    workflow that recorded it. Absent/empty reads as `lqa`."""
    detail = " ".join(str(reason or "").split())[:240]
    lead = (
        # ROUND 3: an OUTAGE never judged the text, so the badge must not say the gate
        # "did not pass" it -- the same two lies the export refusal carried (M2).
        "Quality gate {!r} could not be evaluated (judge unavailable), so this text was "
        "never judged; the run was HELD and the translation needs human review before "
        "delivery."
        if str(cause or "") == GATE_HOLD_CAUSE_JUDGE_UNAVAILABLE
        else "Quality gate {!r} did not pass this text and the run ENDED with that verdict "
        "standing; the run was HELD and the translation needs human review before delivery."
    ).format(step_name or "(unnamed)")
    return {
        "type": GATE_HOLD_FLAG_TYPE,
        "severity": "critical",
        "message": lead + ((" Reason: " + detail) if detail else ""),
        "step": step_name or "",
        "reason": detail,
        "cause": str(cause or ""),
        "family": str(family or "") or GATE_HOLD_FAMILY_LQA,
    }


def ascii_reason(reason: str | None) -> str:
    """A `tag_damage` reason rendered ASCII-ONLY: token brackets stripped, every other
    non-ASCII run folded to a single '?'.

    Reason strings embed REAL tokens (`inline tag dropped ⟦r1⟧, ⟦/r1⟧`), and they reach
    CLI stdout twice: inside `str(TagExportQuarantine)` (`doc export` / `project
    export`) and in `doc paragraphs`. Repo law is ASCII-safe CLI output —
    `_write_ascii_safe` stops the crash but renders `⟦r1⟧` as a backslash escape on a
    cp1252 console, unreadable exactly when the operator most needs to read it. ONE
    formatter here rather than one per surface; the --json payloads keep the real
    token text.

    THE FOLD (adversarial review P5). Stripping the two bracket characters was
    sufficient while every reason was ASCII apart from them — and it stopped being
    sufficient the moment the residue rung began QUOTING THE TARGET (`_residue_excerpt`),
    because a target is arbitrary text in an arbitrary script. A Cyrillic or CJK target
    put this function's own output right back into the escape-soup it exists to prevent,
    so the name stopped being true: `ascii_reason` did not return ASCII. It does now.
    A RUN of non-ASCII folds to ONE '?' (not one per character) so a whole Russian
    sentence reads as a single elision rather than forty question marks, and the
    surrounding ASCII — which is where the debris the operator is hunting actually is —
    stays legible."""
    stripped = (reason or "").replace("⟦", "").replace("⟧", "")
    return re.sub(r"[^\x20-\x7e]+", "?", stripped)


NUMBER_FLAG_TYPE = "number_integrity"


# --- deterministic NUMBER integrity (QA campaign BUG-06) ---------------------
# The DNT/glossary gates verify TERMINOLOGY, the tag gate verifies MARKUP. This is the
# third member of the family and the one BUG-06 found missing: nothing at the sole write
# seam verified that the FIGURES survived. Live evidence (prod run 27, es-ES): "25 items"
# committed as "25.000 articulos" — a value corrupted 1000x — with `status=mt` and
# `qa_flags: []`.
#
# ROOT CAUSE OF THE MISS, precisely: a number check DID exist in `qa.check_segment`, but
# (a) it was never called from this seam — it is a DISPLAY/LQA check, so it could not
# persist a flag or route a status, which is exactly what the campaign reported ("no
# structural number check exists at the segment write seam"); and (b) it compared sorted
# NUMERAL STRINGS with no notion of value or locale, so its verdict was a MINOR "number
# mismatch" that `lqa._QA_TO_MQM` scores at 1 point (99/100 — a PASS). See the qa.py
# section header for the full analysis and the value-aware replacement.
#
# THIS gate consumes `qa.number_findings` and acts ONLY on its MAJOR class (`kind ==
# "value"`): a source number with no value-compatible counterpart in the target, read
# with the target locale's own decimal convention. The MINOR classes (a dropped/invented
# number, an ambiguous re-rendering) deliberately stay display/LQA-only — routing every
# thousands-separator difference to review would drown the queue and teach reviewers to
# ignore the badge, the exact failure the old check had.
#
# SEVERITY — the SOFT pattern, matching the established `nontranslation` gate rather than
# the DNT/glossary one: the text IS committed (a reviewer must see the corrupted figure),
# the segment is routed to NEEDS_REVIEW, AND it is forced TM-ineligible + connector-
# withheld. TM-ineligibility is the part DNT/glossary do not have and this needs: a
# 1000x-wrong figure that seeds shared TM becomes a 100% match that BYPASSES this guard on
# every later document (the C3 poison path). Nothing is ever rewritten or withheld.
def number_flag(reason: str) -> dict:
    """One qa_flags entry for a target whose numeric VALUES diverge from the source —
    same {type,severity,message} shape as the dnt/glossary/tag/refusal flags, so the CAT
    editor renders the badge through the SAME generic loop. 'major' (a reviewer must
    act: a wrong figure is a customer-visible defect, not a style nit)."""
    return {
        "type": NUMBER_FLAG_TYPE,
        "severity": "major",
        "message": "Number integrity broken: " + reason,
        "reason": reason,
    }


def number_violation(
    source_text: str | None, target_text: str | None, target_locale: str | None = None
) -> str:
    """The reason string for a VALUE-CHANGING numeric divergence, or "" when clean.

    Thin, deliberate wrapper over `qa.number_findings` so the write seam and the
    editor/LQA structural layer share ONE comparator and cannot drift. Only the MAJOR
    (`kind == "value"`) class is a violation here; the minor formatting/count classes are
    returned to the display surfaces by `qa.check_segment` and are not gated on."""
    for f in qa.number_findings(source_text, target_text, target_locale):
        if f.get("kind") == "value":
            return f.get("message") or "number value changed"
    return ""


CHAR_LIMIT_FLAG_TYPE = "char_limit"


# --- deterministic CHARACTER-LIMIT verification (live-QA K8) ------------------
# `StringKey.char_limit` is a UI-slot ceiling ("this button fits 40 characters"). It was
# stored, displayed and enforced on NO production path — the only implementation lived in
# `eval_metrics._qa_char_limit`, inside the Eval Lab, where it can never reach a write.
# This is that predicate lifted to the sole write seam so the Lab and production share ONE
# definition and cannot disagree (spec section 6).
#
# COUNTING SEMANTICS (spec section 5), stated because each choice is deliberate:
#   * CHARACTERS, not bytes — `len` on a `str`, so 10 CJK chars pass a limit of 10 even
#     though they are 30 UTF-8 bytes; a UI slot counts glyphs, not storage.
#   * CODE POINTS after NFC normalization — stdlib has no grapheme segmentation and adding
#     a hard `regex` dep at the sole synchronous write seam is a real cost, so code points
#     it is (they OVER-count clusters, so this soft gate errs toward "a human looks"). NFC
#     FIRST so precomposed "é" U+00E9 and decomposed e+U+0301 cannot get DIFFERENT verdicts
#     — a small, tested improvement over `_qa_char_limit`'s bare `len()`.
#   * PLACEHOLDERS counted as their literal string ("{count}" is 7) — the rendered worst
#     case ("1" vs "1,234,567") is unknowable, and this matches what `maxLength` means.
#   * WHITESPACE verbatim, no strip — the exporter ships the value verbatim, so trimming
#     here would pass a value that then ships longer than its limit.
#   * limit <= 0 / non-integer / None -> UNSET. "must be empty" is a requirement nobody
#     has, and `create_key`/`update_key` let 0, negatives and (before this wave) a non-int
#     reach the column, so the predicate MUST survive them without raising inside the seam.
#
# SEVERITY / FLAG FAMILY — SOFT, and its OWN `char_limit` family (NOT the nontranslation
# one the length/meta gates share). Two consequences follow from "an over-limit value is
# LINGUISTICALLY CORRECT, it is just too long for one slot":
#   (1) tm_eligible is left UNTOUCHED — the one deliberate deviation from the newer HARD
#       gates. TM-ineligibility exists for text that is WRONG and would poison future
#       matches; the same sentence in a document with no limit is a fine TM entry.
#   (2) never truncated (truncation can split a placeholder `{count}` -> `{cou`, turning a
#       layout problem into a runtime crash, and it is unverifiable) and never withheld (a
#       withheld value exports as the English SOURCE under empty_as=base — K1's laundering
#       shape). The text is COMMITTED; a human is routed to shorten it.
def coerce_char_limit(raw) -> int | None:
    """The char_limit that actually GOVERNS a value, or None (unset). NEVER raises.

    None / non-integer / <= 0 all read as UNSET. This is the READ-side twin of
    `keys._coerce_char_limit` (which REJECTS those at INGESTION): a value that should
    never have been stored is treated as "no limit" here rather than crashing the sole
    synchronous write seam, because the dirty legacy rows are already reachable — before
    this wave `create_key` validated nothing and `update_key` was `int()`-only, so a 0, a
    -5 or even a non-integer can already sit in `StringKey.char_limit`. Matches
    `eval_metrics._qa_char_limit`'s own <=0 guard."""
    if raw is None or isinstance(raw, bool):
        # `bool` is an `int` subclass, so `True` would coerce to 1 and flag every value of
        # a key an /api upsert set `"char_limit": true` on (reviewer L1). It is never a
        # real limit; read it as unset.
        return None
    try:
        n = int(raw)
    except (TypeError, ValueError, OverflowError):
        # OverflowError (an ArithmeticError, NOT caught by the ValueError/TypeError pair)
        # is what `int(float("inf"))` raises, and `float("inf")` round-trips through JSON
        # as `Infinity` by default (reviewer M1). "never raises" has to mean never.
        return None
    return n if n > 0 else None


def char_count(text) -> int:
    """The code-point count under NFC — the ONE length definition the predicate, the
    flag, the export report and the read-time surfaces (`key_view`, the CAT editor) all
    share, so a value cannot be judged over-limit by one surface and within-limit by
    another."""
    return len(unicodedata.normalize("NFC", text or ""))


def segment_char_limit(session, segment) -> int | None:
    """The char_limit governing this segment, resolved from the segment's `key_id` (a real
    FK to `stringkey`) — self-resolution, so no caller passes a kwarg it can forget (spec
    section 2, the K10 class).

    A NON-KEY segment (`key_id` None) issues ZERO queries and returns None, so the document
    write path is UNCONDITIONALLY byte-identical — the killer golden in verify_char_limit.
    NOT coerced here: the caller runs the value through `char_limit_violation`, which
    coerces, so a dirty stored limit is handled in exactly one place.

    The `Segment.meta['char_limit']` per-unit OVERRIDE is DELIBERATELY DEFERRED to phase 2
    (reviewer M3): spec section 8 reserves that column, and nothing writes it — no UI, no
    validator, no ingestion coercion, no release note. Reading it now would create two
    competing definitions of "the governing limit" (`key_view`'s derived check reads
    `key.char_limit` only), and it was the sole reachability path for a dirty float limit.
    When phase 2 ships the override, resolve it HERE and update `key_view` in the same
    change so the derived read and the write seam still agree."""
    kid = getattr(segment, "key_id", None)
    if kid is not None:
        sk = session.get(StringKey, kid)
        if sk is not None and sk.char_limit is not None:
            return sk.char_limit
    return None


PLACEHOLDER_FLAG_TYPE = "placeholder_integrity"


_SEAM_PLACEHOLDER_RE = re.compile(
    # #692: `%%` is an escaped percent and its OWN token, consumed FIRST so the `%` after it
    # never starts a placeholder. An engine that writes `%%d` / `%%(name)s` turned the
    # placeholder into literal text (the old scan matched `%d` at the second `%`, so the
    # multisets were equal); one that writes `%` for `%%` broke the format string (Python
    # raises on `% d`). A literal `%%` kept on both sides compares equal and stays silent.
    r"%%"
    # Rails i18n `%{name}` is one token (YAML gate): `%{count}` -> `{count}` is a drop.
    r"|%\{[A-Za-z_][A-Za-z0-9_]*\}"
    # #689: Python named `%(name)s` / `%(count)d` / `%(total).2f` is one token: a dropped or
    # renamed name breaks `str % mapping` at run time (KeyError). Same flag/width split as
    # printf below (no class overlap, so no quadratic backtracking).
    r"|%\([A-Za-z_][A-Za-z0-9_]*\)[-#+]*[0-9.]*[hlL]?[diouxXeEfFgGcrsa](?![A-Za-z])"
    # #707: i18next / Mustache `{{name}}` is ONE placeholder (it used to read as `{name}`, so
    # `{{count}}` -> `{count}` passed). A doubled `{{` that does not open a simple argument
    # (`other{{count} files}` in ICU does) is a .NET / Python format ESCAPE: the engine wrote
    # `{"name": "{0}"}` for `{{"name": "{0}"}}` and string.Format throws on the single brace.
    r"|\{\{[A-Za-z0-9_][A-Za-z0-9_.]*\}\}"
    r"|\{\{(?![A-Za-z0-9_][A-Za-z0-9_.]*\})"
    r"|\{[A-Za-z0-9_][A-Za-z0-9_.]*\}"
    # #718: Qt / Windows positional `%1` .. `%99` (Qt's localized `%L1` too). A digit run with
    # no conversion letter after it; `%1$s`, `%5d` and `%10.2f` stay printf (lookahead).
    r"|%L?\d{1,2}(?![\d$A-Za-z.])"
    # #718: printf LENGTH MODIFIERS (`%ld` `%lu` `%lld` `%zu` `%hhd`, iOS / C / gettext): the
    # conversion letter used to be the modifier itself, so `%ld` was never a placeholder.
    r"|%(?:\d+\$)?[-#+]*[0-9.]*(?:hh|ll|[hlLqzjt])?[a-zA-Z@](?![A-Za-z])"
)


_PRINTF_POSITION_RE = re.compile(r"^%\d+\$")


# --- deterministic REAL-PLACEHOLDER integrity (LC-7) --------------------------
# The tag gate (above) enforces the inline-fidelity `⟦rN⟧/⟦gN⟧/⟦xN⟧` tokens the DOCX/
# XLIFF/HTML filters inject. This is its sibling for the REAL programmatic placeholders
# that live LITERALLY in the text — `{0}`, `{name}`, `%s`, `%1$d` — the ones a key value
# or a plain-text document carries. They were DETECTED (segment._TAG_PATTERNS ->
# qa.check_segment's live "tags" badge) but NEVER persisted or gated at the sole write
# seam, so a dropped `%s` shipped as clean `mt` (audit LC-7). This closes that: an exact
# MULTISET comparison of the extracted placeholder tokens, a MISMATCH routing the segment
# to NEEDS_REVIEW + TM-ineligible under its own qa_flag family — the number-gate pattern.
#
# FALSE-POSITIVE POSTURE (the whole game, exactly as the number gate reasons about
# locale rendering):
#   * multiset, not sequence — a target that REORDERS placeholders is silent (reordering
#     is legitimate; the ⟦⟧ tag gate handles order for real markup).
#   * printf POSITION is normalized away — `%1$s` compares equal to `%s`, because adding
#     explicit argument positions (`%s %s` -> `%2$s %1$s`) is a legitimate reordering
#     technique a translator uses; only a change to the set of conversion TYPES flags.
#   * SIMPLE brace args only (`{name}`, `{0}`, `{a.b}`) — a complex ICU form
#     (`{count, plural, one {# item} other {# items}}`) contains spaces/commas and is
#     NOT matched, so its legitimately-translated inner text ("# item" -> "# articulo")
#     never manufactures a mismatch (ICU protection is LC-11's job, not this gate's).
#   * a printf conversion glued to following letters is NOT a specifier — `(?![A-Za-z])`
#     keeps a literal "50%off" / "100%sure" from reading as `%o` / `%s`.
#   * URLs and HTML tags are DELIBERATELY OUT of this SOFT gate: a URL legitimately
#     localizes its path/query, an HTML tag carries translatable `alt`/`title` text, and
#     document filters already mask real inline markup to ⟦⟧ tokens the tag gate covers.
#     They remain visible on the editor's liberal live "tags" badge; they are just not a
#     quarantine here (a gate that cries wolf on a localized URL trains reviewers to
#     ignore it — the failure the number gate's own history warns about).
#
# SEVERITY / SEMANTICS — SOFT, mirroring the number gate: the text IS committed (a
# reviewer must see the broken value), the segment is routed to NEEDS_REVIEW, and it is
# forced TM-ineligible (a placeholder-dropped target that seeds shared TM returns as a
# 100% match that BYPASSES this guard forever). NO export hard-block (Fable default:
# soft-only for now) — unlike the tag gate, this never raises a quarantine at export.
# ALWAYS ON (like number/tag): a pure deterministic multiset compare that cannot
# false-positive on MockProvider's echo (the "[locale] " prefix carries no placeholder).
# The placeholder classes this gate enforces (see the posture note for what is excluded).
# REGEX SAFETY (reviewer M5): the flag class and the width class must NOT overlap, or the
# two adjacent stars split an all-`0` run O(n^2) ways -- `%` + 20k zeros measured at ~3s on
# this sole synchronous write seam with untrusted input. So `0` lives ONLY in the width run
# (`[0-9.]*`), never in the flag class, and the printf `0`-flag (`%05d`) is still matched
# because the width run happily starts with `0`. The SPACE flag is dropped too (reviewer
# M6): `%` followed by a space is never a specifier for our purposes, and keeping it made
# "20 % a year" -> "20 % al año" read as a `% a` specifier and MAJOR-quarantine a correct
# translation. Trade-off: a genuine `% d` space-flagged conversion (rare in loc strings) is
# no longer caught -- an acceptable miss to kill a real false-positive class.
# {0} {name} {user.name}
# %s %d %1$s %.2f %-5d %05d %@ %1$@ (Apple .strings object placeholders, file-format Batch 1)
def _placeholder_multiset(text, *, extended=False):
    """The multiset (Counter) of real placeholder tokens in `text`, printf positional
    index normalized away so `%1$s` and `%s` compare equal (see the posture note).
    `extended` also protects double braces and ICU argument names/types for Jev;
    localized ICU option text is not a placeholder."""
    from collections import Counter

    if extended:
        # Extend the existing recognizer for Jev. ICU branch text is translatable;
        # argument names/types and nested arguments are the protected structure.
        tokens = []
        text = text or ""
        header = re.compile(r"\s*([\w.]+)\s*,\s*(plural|selectordinal|select|number|date|time)\b")

        def scan(start, end, nesting=0):
            if nesting > 32:
                tokens.append("ICU:nesting-limit")
                return
            i = start
            while i < end:
                if text.startswith("{{", i):
                    close = text.find("}}", i + 2, end)
                    if close >= 0:
                        tokens.append(text[i : close + 2])
                        i = close + 2
                        continue
                if text[i] == "{":
                    match = header.match(text, i + 1, end)
                    if match:
                        depth, close = 1, i + 1
                        while close < end and depth:
                            depth += (text[close] == "{") - (text[close] == "}")
                            close += 1
                        tokens.append("ICU:" + match[1] + ":" + match[2])
                        if depth:
                            tokens.append("ICU:unclosed")
                        # Scan inside each option body without mistaking its braces
                        # for a simple argument (e.g. one {item}).
                        pos = match.end()
                        nested_start = len(tokens)
                        while pos < close - 1:
                            if text[pos] != "{":
                                pos += 1
                                continue
                            body, level = pos + 1, 1
                            pos += 1
                            while pos < close - 1 and level:
                                level += (text[pos] == "{") - (text[pos] == "}")
                                pos += 1
                            scan(body, pos - 1, nesting + 1)
                        # Branch counts vary by language. Each nested identity
                        # must survive somewhere in this argument, not per branch.
                        tokens[nested_start:] = sorted(set(tokens[nested_start:]))
                        i = close
                        continue
                match = _SEAM_PLACEHOLDER_RE.match(text, i, end)
                if match:
                    tokens.append(_PRINTF_POSITION_RE.sub("%", match[0]))
                    i = match.end()
                else:
                    i += 1

        scan(0, len(text))
        return Counter(tokens)
    text = text or ""
    bodies = _icu_branch_bodies(text)
    return Counter(
        _PRINTF_POSITION_RE.sub("%", m[0])
        for m in _SEAM_PLACEHOLDER_RE.finditer(text)
        if not (bodies.get(m.start()) == m.end() - 1 and not m[0].startswith("{{"))
    )


# #711 gate: an ICU branch whose whole body is one word (`other {Viewer}`) is message text,
# not a `{name}` placeholder. Counting it held every correct translation of a one-word select
# branch (fr `other {Lecteur}`), and since #711 a held placeholder mismatch exports the
# SOURCE, so the translation would never ship. ICU structure itself is checked by
# `quality_part2.icu_violation` (#709).
_ICU_BRANCH_HEAD = re.compile(r"\{\s*[A-Za-z_][\w.]*\s*,\s*(?:plural|selectordinal|select)\s*,")


def _icu_branch_bodies(text: str) -> dict:
    """{start: end} of every ICU branch body brace pair (the `{...}` right after a selector
    key at the top level of a plural / select / selectordinal argument). Linear: one stack
    pass for matching braces, then each header walks only its own top level."""
    if "," not in text or not _ICU_BRANCH_HEAD.search(text):
        return {}
    close: dict = {}
    stack = []
    for i, c in enumerate(text):
        if c == "{":
            stack.append(i)
        elif c == "}" and stack:
            close[stack.pop()] = i
    bodies: dict = {}
    n = len(text)
    for m in _ICU_BRANCH_HEAD.finditer(text):
        i = m.end()
        while i < n:
            c = text[i]
            if c == "{":
                j = close.get(i)
                if j is None:
                    break
                bodies[i] = j
                i = j + 1
                continue
            if c == "}":
                break
            i += 1
    return bodies


WHITESPACE_FLAG_TYPE = "whitespace"


# --- deterministic WHITESPACE hygiene (QA-1) ---------------------------------
# Two cheap text-mechanics checks (interior double space; target-added leading/trailing
# whitespace), computed by `qa.whitespace_findings` so the editor-live path and this seam
# share ONE comparator and cannot drift. SOFT and its OWN `whitespace` family, MINOR
# severity: a spacing slip routes to NEEDS_REVIEW so a human tidies it, but leaves
# tm_eligible UNTOUCHED (mirroring char_limit — the translation is linguistically correct;
# the same text minus the stray space is a fine TM entry, and TM-ineligibility is reserved
# for text that is WRONG).
#
# GATED behind the real-MT `lang_guard`, NOT always-on (see the qa.whitespace_findings
# section header): the offline MockProvider echoes the source under a "[locale] " prefix,
# so a source with a LEADING space produces an echo carrying an interior double space the
# source lacks — a systemic false positive on a demo/offline install. lang_guard exempts
# the mock exactly as it does for the language-mismatch guard.
def whitespace_violation(source_text, target_text) -> str:
    """The reason string for a whitespace-hygiene divergence, or "" when clean. Thin
    wrapper over `qa.whitespace_findings` (the shared comparator); joins the messages of
    all findings (at most two: double-space and leading/trailing)."""
    return "; ".join(f["message"] for f in qa.whitespace_findings(source_text, target_text))


META_COMMENTARY_REASON = (
    "suspected translator note / model meta-commentary left in the delivered target"
)


_META_PAREN_RE = re.compile(r"\(([^()]{0,800})\)")


_META_BRACKET_RE = re.compile(r"\[([^\[\]]{0,800})\]")


_META_NOTE_MARKERS: tuple[str, ...] = (
    "note:",
    "note :",
    "notes:",
    "n.b.",
    "nb:",
    "nota bene",
    "translator's note",
    "translators note",
    "translator note",
    "nota:",
    "nota del traductor",
    "n.d.t.",
    "ndt:",
    "remarque:",
    "remarque :",
    "note du traducteur",
    "hinweis:",
    "anmerkung:",
    "anmerkung des uebersetzers",
    "nota do tradutor",
    "nota del traduttore",
    "opmerking:",
    "vertalersnotitie",
)


_META_FIRST_PERSON: tuple[str, ...] = (
    "i corrected",
    "i have corrected",
    "i've corrected",
    "i translated",
    "i have translated",
    "i've translated",
    "i revised",
    "i have revised",
    "i've revised",
    "i fixed",
    "i have fixed",
    "i've fixed",
    "i adjusted",
    "i have adjusted",
    "i've adjusted",
    "i addressed",
    "i have addressed",
    "i've addressed",
    "i changed",
    "i have changed",
    "i've changed",
    "i ensured",
    "i have ensured",
    "i've ensured",
    "i applied",
    "i have applied",
    "i've applied",
    "i followed",
    "i have followed",
    "i've followed",
    "i kept",
    "i have kept",
    "i maintained",
    "i produced",
    "i rendered",
    "i used the",
    "i made sure",
    "he corregido",
    "he traducido",
    "he revisado",
    "he ajustado",
    "he cambiado",
    "he aplicado",
    "he mantenido",
    "he seguido",
    "he asegurado",
    "he producido",
    "j'ai corrige",
    "j'ai corrigé",
    "j'ai traduit",
    "j'ai revise",
    "j'ai révisé",
    "j'ai ajuste",
    "j'ai ajusté",
    "j'ai suivi",
    "j'ai applique",
    "j'ai appliqué",
    "j'ai conserve",
    "j'ai conservé",
    "j'ai veille",
    "j'ai veillé",
    "ich habe korrigiert",
    "ich habe uebersetzt",
    "ich habe übersetzt",
    "ich habe die uebersetzung",
    "ich habe die übersetzung",
    "ich habe alle",
    "ich habe angepasst",
    "ich habe sichergestellt",
    "ho corretto",
    "ho tradotto",
    "ho rivisto",
    "ho adattato",
    "eu corrigi",
    "eu traduzi",
    "eu revisei",
    "ik heb gecorrigeerd",
    "ik heb vertaald",
    "ik heb aangepast",
)


_META_VOCAB: tuple[str, ...] = (
    "translation",
    "translated",
    "translating",
    "retranslat",
    "traduccion",
    "traducción",
    "traducido",
    "traducida",
    "traduction",
    "traduit",
    "traduite",
    "uebersetzung",
    "übersetzung",
    "uebersetzt",
    "übersetzt",
    "traduzione",
    "tradotto",
    "traducao",
    "tradução",
    "traduzido",
    "vertaling",
    "vertaald",
    "feedback",
    "comentarios",
    "retroalimentacion",
    "retroalimentación",
    "commentaires",
    "rueckmeldung",
    "rückmeldung",
    "instructions",
    "instrucciones",
    "consignes",
    "anweisungen",
    "istruzioni",
    "guidelines",
    "directrices",
    "directives",
    "richtlinien",
    "linee guida",
    "conventions",
    "convenciones",
    "konventionen",
    "convenzioni",
    "convencoes",
    "style guide",
    "guia de estilo",
    "guía de estilo",
    "guide de style",
    "stilrichtlinie",
    "glossary",
    "glosario",
    "glossaire",
    "glossar",
    "glossario",
    "source text",
    "texto de origen",
    "texto fuente",
    "texte source",
    "quelltext",
    "issues identified",
    "problemas identificados",
    "problemes identifies",
)


_META_FIRST_PERSON_GAPPED = re.compile(
    r"\b(?:ich habe|ik heb)\b[^.!?\n]{0,90}?\b("
    r"uebersetzt|ubersetzt|übersetzt|korrigiert|angepasst|"
    r"ueberarbeitet|überarbeitet|befolgt|sichergestellt|beibehalten|"
    r"vertaald|gecorrigeerd|aangepast|herzien|gevolgd|behouden)\b"
)


_META_QUOTE_FOLD = str.maketrans({"’": "'", "ʼ": "'", "‘": "'", "´": "'", "`": "'"})


# --- deterministic META-COMMENTARY guard (QA campaign BUG-06, second half) -----
# Live evidence (same prod run): a delivered es-ES translation ended with
#   "*(Note: I corrected all issues identified in the feedback to produce a final,
#     polished translation that complies with es-ES conventions and guidelines.)*"
# and shipped with `status=mt`, `qa_flags: []`.
#
# ROOT CAUSE OF THE MISS: EVERY existing commentary detector is ANCHORED TO THE START of
# the output, and this note is at the END of an otherwise-correct translation.
#   * `refusal.is_refusal` — `_STEMS`/`_META_STEMS`/`_TARGET_*` only match within the
#     first `_ANCHOR_CHARS` (80) or at position 0; `_FULL_PHRASES` (the one position-free
#     tier) is deliberately tiny and lists none of this vocabulary.
#   * `refusal.looks_untranslated` — the segment is real Spanish, saturated with es
#     stopwords, so the language heuristic correctly ABSTAINS.
#   * `refusal.strip_meta_preamble` — LAYER 1 removes echoed `<translation_context>`
#     fences; LAYER 2 strips a leading meta-LABEL line that is followed by a blank line.
#     A TRAILING parenthetical is neither.
# So the gap is structural, not a missing phrase: nothing looked anywhere but the head.
#
# PRECISION IS THE WHOLE GAME (the same rule the refusal module lives by). A legitimate
# translated parenthetical must never be flagged, and translated content ABOUT
# translation is exactly what this product routinely handles. Three independent locks:
#   1. POSITION-FREE but SPAN-BOUNDED — only parentheticals, bracketed spans, and
#      note-marker-led LINES are candidates. A note lives in one of those; an ordinary
#      sentence that quotes a user saying "he traducido el documento" does not.
#   2. FIRST PERSON — the span must contain a first-person TRANSLATION/CORRECTION verb
#      phrase ("I corrected", "he corregido", "j'ai traduit", "ich habe ... uebersetzt").
#      A note marker alone is NOT enough: "(Nota: la traduccion se actualiza sola)" is
#      ordinary content and must pass.
#   3. META-LINGUISTIC VOCABULARY — the span must ALSO name the translation/feedback/
#      instruction/guideline/glossary domain. "(He corregido el error de la pagina)" is a
#      first-person sentence about the PRODUCT, not about the task, and must pass.
# Plus a SOURCE-SIDE KEEP-GUARD (mirroring `refusal._strip_injected_fences`): if the
# SOURCE itself carries a span that trips the same predicate, the document is genuinely
# ABOUT translator notes and the target's translated note is real content — abstain.
#
# SEVERITY / FLAG FAMILY — deliberately NOT a new blocking class. It reuses the
# ESTABLISHED `refusal.NONTRANSLATION_FLAG_TYPE` family with its own distinct `reason`
# string: soft (the text is committed, never rewritten or withheld), NEEDS_REVIEW,
# TM-ineligible, connector-withheld. Reusing the family is what makes the finding visible
# on EVERY surface with no edit to the editor's `_SURFACED` set or the CLI's
# `_SEGMENT_SURFACED`/`_SUMMARY_FAMILIES` allowlists — and it is semantically honest: a
# target carrying the model's commentary about the task is a partial non-translation.
# Candidate SPANS. Parentheticals and brackets are non-nesting by design (a note is flat);
# the length cap keeps a stray unmatched bracket from turning a whole document into one
# candidate.
# Note markers (en/es/fr/de/it/pt/nl). These only SELECT a candidate span (the tail of
# the line from the marker on); on their own they never flag — lock 2 + lock 3 still have
# to hold, and the count-symmetry guard still has to clear.
# LOCK 2 — first-person translation/correction verb phrases. Multi-word throughout (a
# pronoun or auxiliary plus the verb), which is what keeps them from matching inside
# ordinary words and what keeps bare preterites ("corrigi", which is also a fragment of
# "corrigir") out of the set.
# English
# Spanish
# French
# German
# Italian / Portuguese / Dutch
# LOCK 3 — meta-linguistic vocabulary: the span must be ABOUT the translation task.
# LOCK 2, second tier — languages whose PERFECT TENSE puts the participle at the END of
# the clause, so the auxiliary and the verb are never adjacent and no fixed multi-word
# phrase can match them: German ("Ich habe den Text ... uebersetzt") and Dutch ("Ik heb
# de tekst ... vertaald"). Adversarial review found this both ways round: the de/nl note
# shapes were MISSED, and — worse — a genuine de/nl SOURCE parenthetical was missed by
# the source-side keep-guard while its English translation WAS caught, manufacturing a
# false positive on correct content. The gap is bounded (`[^.!?\n]{0,90}` — one clause,
# never across a sentence boundary) so it stays a note-shaped predicate.
# Typographic look-alikes a model emits far more often than the ASCII forms: the curly
# apostrophe in "j’ai" / "I’ve" is what the real providers produce, and matching only the
# straight form missed every French and contracted-English note (and, again, let the
# source-side keep-guard fail asymmetrically). Folded before any phrase test.
def _meta_fold(text: str | None) -> str:
    return (text or "").translate(_META_QUOTE_FOLD).lower()


LENGTH_MIN_SURPLUS = 120


LENGTH_MIN_RATIO = 5.0


LENGTH_RUNAWAY_REASON = (
    "suspected non-translation: the delivered target is disproportionately longer than its source"
)


# --- deterministic LENGTH-RUNAWAY gate (live QA campaign K4) ------------------
# Live evidence (Batch 4 key-based live QA):
# the source UI label "Start now" (9 chars) came back from the local model as ~245 chars of
# Russian conversational preamble ("Privet! Ya gotov perevesti UI-stroku ...", markdown
# bold + backticks included). It stored `status=mt` with ZERO qa_flags, seeded TM, and
# exported verbatim into the ru build file where it would render as a button label.
#
# NOT A KEY BUG. The investigation reproduced the identical outcome for a DOCUMENT segment
# with the same input — byte-identical row, `status=mt`, `qa_flags=None`, TM-eligible. The
# gap is at the SHARED write seam, so the fix belongs here and the regression suite pins
# the document shape too, so nobody re-scopes this to the key surfaces.
#
# ROOT CAUSE OF THE MISS — every existing perimeter check abstained, correctly:
#   * `refusal.is_refusal` — `_TARGET_STEMS` carries this very phrase in es/fr/de/it/pt
#     ("estoy listo para traducir", "je suis pret a traduire", "ich bin bereit zu
#     uebersetzen", "sono pronto a tradurre", "estou pronto para traduzir") but had NO
#     Russian tier, and `_TARGET_PREAMBLES` had hola/bonjour/hallo/ciao/ola but no
#     "privet". Controls confirmed it: the same output in Spanish or English IS caught.
#     (That per-language asymmetry is a defect in its own right and is closed separately.)
#   * `meta_commentary` — span-bounded to parentheticals / brackets / note-marker tails;
#     this output is one unbroken conversational block with no note marker, and
#     `_META_FIRST_PERSON`/`_META_VOCAB` carry no Cyrillic.
#   * `refusal.looks_untranslated` — correctly abstains, the output really IS Russian.
#     (It is opt-in via `lang_guard`, which at the time of this analysis the workflow
#     path did not pass at all — GitHub #43 since wired BOTH workflow write-backs into
#     it, gated on a live provider. That closes a different hole, not this one: this
#     output is not an untranslated echo, so the guard would still, correctly, abstain.)
#   * `number_violation` — no numbers in the source. `tag_damage` — no tags.
# So NO length / ratio / expansion check existed anywhere on the write path. Phrase lists
# are inherently per-language and will always trail the next model and the next locale;
# this gate is STRUCTURAL and language-independent, which is why it is the primary fix.
#
# PRECISION IS THE WHOLE GAME (the rule the rest of this perimeter lives by). Legitimate
# expansion is enormous and routine: an acronym glossary entry ("DTaP" -> the full vaccine
# name), a short German/Finnish UI label, a terse en source rendered as formal prose. So
# TWO INDEPENDENT LOCKS must BOTH hold, and either alone is demonstrably unusable:
#   1. ABSOLUTE SURPLUS  len(tgt) - len(src) >= LENGTH_MIN_SURPLUS (120 chars)
#   2. PROPORTIONAL      len(tgt) >= len(src) * LENGTH_MIN_RATIO   (5.0x)
#
# CALIBRATION — stated exactly as it was measured, on a READ-ONLY COPY of this install's
# dev DB (the first framing of this block called all 8,695 rows "the corpus", which is
# 93.5% out of scope; fix-round finding F2):
#   * IN SCOPE — 562 pairs that can actually reach this seam: 500 `segmenttm` + 62
#     non-empty `segment` rows. (`commit_mt_output`'s only callers are translate,
#     orchestrator x2, keys.set_value and fulltext_paragraphs._commit_body — a
#     `glossaryentry` row NEVER passes through it.) Ratio lock alone -> 0 fires;
#     SURPLUS lock alone -> 4 fires (worst: a 422-char source expanding +273 at a
#     perfectly ordinary 1.65x); BOTH locks -> 0.
#   * OUT OF SCOPE, kept deliberately as a CONSERVATIVE PROXY — the 8,133 `glossaryentry`
#     rows. Glossary is the hardest false-positive class this install has (an acronym
#     entry expands into its full expansion: DTaP 17.25x, Hib 16.0x, iSNS 12.25x, DoS
#     11.0x), so holding the gate to zero fires there is a strictly HARDER bar than the
#     one it must clear. Ratio lock alone -> 95 fires; surplus lock alone -> 0; BOTH -> 0.
#   * 8,695 pairs in total (562 in scope + 8,133 proxy), en-US -> de/es/fr/ja/zh/es-419,
#     ZERO false positives with both locks. The K4 case clears both with ~2x and ~5.4x
#     of margin. That target list is EXACT and was re-verified in the final round against
#     a challenge that it omitted ru-RU: the install does hold one ru-RU `segment` row,
#     but its target is EMPTY, so it is not one of the 62 in-scope PAIRS (those are
#     es-419 x58 + es-ES x4) and contributes to no count here.
#
# NO CJK CARVE-OUT (fix-round finding F1), WITH A RECORDED RESIDUAL (final-round F8).
# The first cut of this gate gave a CJK-dominant source a higher ratio floor (15.0x) and
# cited "a real ja->en pair in the corpus measured +119 chars at exactly 5.00x". THAT
# MEASUREMENT DOES NOT EXIST — the corpus is en-US -> de/es/fr/ja/zh/es-419 throughout, so
# 0 of its 8,695 SOURCES are CJK-dominant, and both the floor and its 0.30 share threshold
# were invented rather than derived. What CAN be measured is the reverse: read the 244
# CJK-dominant TARGETS (200 segmenttm + 44 glossaryentry) as sources. Stated as TWO
# SEPARATE ROWS, because they ARE two different rows — the earlier "worst pair +39 at
# 6.25x" welded a surplus from one row onto a ratio from another and described a pair that
# does not exist (the same composite-claim habit F2 removed):
#   * worst SURPLUS  +39 at 4.00x — 启用双重认证以提高安全性。
#                                   -> "Enable two-factor authentication for extra security."
#   * worst RATIO   6.25x at +21  — 双重认证 -> "Two-factor authentication"
# ZERO of the 244 clear the +120 surplus lock; ZERO clear both locks.
#
# WHAT THAT EVIDENCE EARNS, AND WHAT IT DOES NOT. Every one of the 244 reversed rows is a
# CJK source against an ENGLISH target, so the claim it supports is narrow: the plain locks
# spare every faithful CJK->en pair, and the arithmetic says why — the SURPLUS lock does
# the work, and a 30-char ja source would have to produce a 150-char en target to fire at
# all. It does NOT generalise to CJK->de/ru, and reversing a target cannot reach that case
# (read backwards these rows are short en->zh sources, and the Latin side is en, not
# de/ru). So the residual below was found by AUTHORING pairs, not by measuring the corpus:
#   KNOWN LIMITATION, zh->de (and by the same arithmetic zh->ru). A standard PRC
#   data-subject-rights clause in standard DSGVO register FIRES, with every term mapping
#   1:1 and nothing added:
#     src (28 hanzi) 您有权随时要求访问、更正或删除我们所持有的您的个人信息。
#     tgt (153 de)   Sie haben jederzeit das Recht, Zugang zu Ihren bei uns gespeicherten
#                    personenbezogenen Daten zu verlangen sowie diese berichtigen oder
#                    löschen zu lassen.
#     +125 chars at 5.46x -> FIRES. The retired 15.0x floor spared it. (The re-review's
#     authored zh-legal set held four more pairs straddling the locks at 4.91x-5.42x;
#     their texts were not carried over, so only this pair is reproduced — verbatim, in
#     `verify_length_runaway` C2, so it stays checkable rather than anecdotal.)
#   WHY zh->de and not ja->en: zh carries no kana, so per-character density is higher than
#   ja, and de/ru run longer than en. For scale — the in-scope Latin corpus tops out at
#   2.50x, i.e. 2x of margin under the 5.0 lock; faithful zh->de/ru runs 4.2x-5.5x, i.e.
#   NO margin.
#
# BLAST RADIUS TODAY IS ZERO, and the gate is SOFT. 0 CJK-dominant sources exist in this
# install (re-confirmed independently in the final round), and a fire KEEPS the text,
# routes to needs_review and marks the segment TM-ineligible. So this is RECORDED rather
# than patched. PREREQUISITE: deriving a SOURCE-SCRIPT-AWARE floor on REAL CJK-source
# pairs must happen BEFORE any install enables a CJK source locale. Do NOT restore
# `LENGTH_MIN_RATIO_CJK` / `LENGTH_CJK_SHARE` to do it — those numbers were invented, and
# re-adding an invented floor to cover a measured residual is exactly how this got here.
#
# The carve-out itself bought no measurable precision and cost real recall (a CJK-source
# runaway at 6-14x sailed straight through), so it is GONE. Dropping it also deletes the
# O(n) per-character source scan the gate used to run before its O(1) ratio test (F5) and
# moots the gaps in the old `_CJK_RANGES` (halfwidth katakana, fullwidth forms, Ext G).
#
# COST — this gate is O(1) in TIME plus two `str.strip()` calls, and that matters at ONE
# caller: `fulltext_paragraphs._commit_body` reaches this seam on EVERY paragraph save with
# the WHOLE document body as source_text. With the old CJK share scan the review measured
# 0.019s / 0.312s / 0.887s at 20k / 200k / 1M chars, and a re-measurement here gave 3.1s
# uninstrumented (6.5s under tracemalloc) at 1M with an 8.4 MB allocation peak — the
# absolute number is machine- and instrumentation-dependent, the O(n) shape is not.
#
# NOW, stated as BOTH cases rather than the flattering one (final-round F10). `str.strip()`
# returns the SAME object only when there is nothing to strip, so at 1,000,000 chars:
#   * source needs no stripping        -> 56 bytes   (the best case, and the rarer one)
#   * source has ONE trailing newline  -> 1,000,097 bytes: one ~n-byte copy
# A document body arriving at `_commit_body` almost always HAS a trailing newline, so the
# copying case is the NORMAL one. Either way the TIME is O(1) (median ~0.4us at both 10k
# and 1,000,000 chars) and the work is ONE copy instead of a per-character scan.
# The regression that pins this (verify_length_runaway C2b) therefore asserts the SCALING —
# t(1M) against t(10k) — not an absolute allocation: an allocation bound only catches a
# scan that MATERIALISES a list (as the old `_cjk_share` did, 8.4 MB), and a
# generator-shaped scan of identical O(n) cost walks straight through it. The allocation
# assert is kept as a secondary, and both source shapes are measured.
#
# SEVERITY / FLAG FAMILY — deliberately the SOFT pattern and the EXISTING
# `refusal.NONTRANSLATION_FLAG_TYPE` family with its own distinct `reason`, exactly as
# `meta_commentary_flag` does, for three reasons:
#   (1) it renders on every surface with ZERO edits to `documents._SURFACED`,
#       `api_cli._SEGMENT_SURFACED` or `_SUMMARY_FAMILIES` (api_cli has an assert over
#       that allowlist that a brand-new flag type would break);
#   (2) `orchestrator` already re-asserts NEEDS_REVIEW for flags in THIS family that the
#       commit just wrote, so the status=MT reset on its write-back path cannot wipe the
#       verdict — a new flag type would be silently erased there (the BUG-06 class);
#   (3) it is semantically honest: a target 27x its source is a partial non-translation.
# The text is COMMITTED (a reviewer must see what shipped), never rewritten or withheld;
# the segment routes to NEEDS_REVIEW and is forced TM-ineligible + connector-withheld —
# a runaway that seeds shared TM returns as a 100% match that BYPASSES this guard forever.
# SUBORDINATION: language > meta > length. The family holds ONE entry
# (`merge_nontranslation_flag` replaces), so the strongest verdict already set wins.
def length_runaway(source_text: str | None, target_text: str | None) -> str:
    """A short reason string when `target_text` is a LENGTH RUNAWAY relative to
    `source_text`, or "" when the expansion is within the plausible band.

    BOTH locks must hold (see the section header): an absolute surplus of at least
    `LENGTH_MIN_SURPLUS` characters AND a ratio of at least `LENGTH_MIN_RATIO`. Either
    alone false-positives on real delivered content; together they measured ZERO false
    positives across 562 in-scope pairs plus 8,133 out-of-scope glossary rows carried as
    a deliberately conservative proxy (see the section header for the exact split).

    Deterministic, LLM-free, language-independent, and SAFE OFFLINE: MockProvider's echo
    is the source plus a short locale tag, which cannot clear either lock.

    O(1) in TIME after two `str.strip()` calls (which copy when there IS whitespace to
    strip, and return the same object when there is not) — NOTHING here walks the source.
    That is a requirement, not an accident: `fulltext_paragraphs._commit_body` reaches this
    seam on every paragraph save with the WHOLE document body as source_text, so a
    per-character scan here costs ~0.3s per save on a 200 KB document to reach a ratio of
    ~1.1 (F5). See the section header for both measured allocation cases.

    Empty/whitespace source OR target -> "" (an empty target is the empty-output guards'
    business, and a runaway is undefined against no source)."""
    src = (source_text or "").strip()
    tgt = (target_text or "").strip()
    if not src or not tgt:
        return ""
    surplus = len(tgt) - len(src)
    if surplus < LENGTH_MIN_SURPLUS:
        return ""
    ratio = len(tgt) / len(src)
    if ratio < LENGTH_MIN_RATIO:
        return ""
    return (
        LENGTH_RUNAWAY_REASON + ": a %d-character source produced a %d-character target "  # noqa: UP031
        "(+%d, %.1fx)" % (len(src), len(tgt), surplus, ratio)
    )


SOURCE_FALLBACK_FLAG_TYPE = "source_fallback"


# --- source-fallback provenance (GitHub #42) ---------------------------------
# A FULL_TEXT recovery replaces a tag-damaged PARAGRAPH with its SOURCE text so the
# document can be delivered at all. The spliced body is then perfectly well-formed —
# so without this flag the segment commits CLEAN, TM-eligible and connector-pushable
# while 21% of it is still English, with NOTHING recording why. That is a laundering
# regression, not a recovery. This flag is the durable record; `revert_paragraph` also
# forces NEEDS_REVIEW, passes tm_eligible=False, and tm.write_back/tm.ft_store REFUSE a
# segment carrying it (otherwise segment_approve's TM path launders it straight back
# in). Same {type,severity,message} shape as the dnt/glossary/qe/tag flags, so the
# generic qa-cell loop in editor.html renders it with no template branch.
def source_fallback_flag(indices) -> dict:
    """One qa_flags entry naming EVERY paragraph currently reverted to source."""
    idx = sorted({int(i) for i in (indices or [])})
    return {
        "type": SOURCE_FALLBACK_FLAG_TYPE,
        "severity": "major",
        "message": (
            "Paragraph(s) {} were reverted to the SOURCE text to recover "
            "from broken inline formatting tags - they are UNTRANSLATED".format(
                ", ".join(str(i) for i in idx)
            )
        ),
        "indices": idx,
        "reason": "paragraph reverted to source",
    }


def is_hard_locked(segment) -> bool:
    """Whether `segment` carries a HARD TM lock (PROPOSAL-tm-matching §5): a value the
    `tm_match` step filled + hard-locked so no LLM/MT/import write may ever overwrite
    it. `lock_band` set AND `lock_hard` True. getattr-guarded so a pre-lock-column
    ORM object (or a plain stub) never raises. Kill-switchable via TM_LOCKS_ENABLED."""
    return bool(
        config.TM_LOCKS_ENABLED
        and getattr(segment, "lock_band", None)
        and getattr(segment, "lock_hard", False)
    )
