"""LQA safety floor + adjudication — Phases 1 and 3 of
PROPOSAL-lqa-glossary-adjudication.

Phase 1 (below): two deterministic, zero-spend, LLM-free guards that sit INSIDE the
LQA -> fixer correction loop. Phase 3 (bottom of this file): `adjudicate`, the
deterministic BACKSTOP that downgrades an LLM finding to `neutral` when its span
targets a legitimately-applied approved glossary term — reusing Phase 1's
`_survives_softly` survival test so the revert gate and the adjudicator can never
disagree about what "the term is still there" means. Phase 2 (the glossary-aware judge
prompt) lives in `lqa._glossary_clause`; its kill-switch predicate is here so all four
switches read the same way.

The two Phase 1 guards. Both are pure functions of text the orchestrator already holds, so
they cost microseconds per segment and never call a provider.

**(C) Fixer post-check with revert.** `introduced_violations(source, pre, post, terms)`
returns only the glossary / DNT violations an edit *introduced* — a DELTA, never an
absolute. A pre-existing violation the edit leaves alone is not this guard's business
(`quality.commit_mt_output`'s write-back gate already flags it), and an edit that FIXES
a violation is obviously kept. The contract is copied verbatim from the tag-repair
seam (`quality.commit_mt_output`: *"this seam never trusts an injected repairer's
word"*) — generalized from inline tags to terminology.

**(D1) TM provenance guard.** `tm_protect_for(step, is_correction_loop)` resolves the
per-step tristate `WorkflowStep.tm_protect` (None = the correction-loop-shaped default,
True = always protect, False = never) so `tm_origin` segments can be dropped from a
correction-loop step's pending set. That is Phrase's documented lock model
(pre-translate at 100% -> lock -> exclude from QA/auto-fix) expressed in our data
model; the provenance is preserved by *not doing the thing that destroyed it*.

MATCHER REUSE IS DELIBERATE: every check here delegates to
`quality.dnt_violations` / `quality.glossary_term_violations` — the SAME functions the
write-back gate runs. This guard and the write-back gate must never disagree about what
"the term is present" means, so there is exactly one whole-token, CJK-safe, case-rule-
honouring matcher (`quality._term_present`) in the codebase.

BYTE-IDENTICAL WHEN THE ASSETS ARE ABSENT: no glossary terms -> both violation sets are
empty -> no revert, no flag; no TM hits -> `tm_origin` is all False -> D1 excludes
nothing. Every install without a glossary behaves exactly as before.

Kill switches (settings.DEFAULTS, the `ai_tagging_enabled` convention), all default
"1": `glossary_fix_guard_enabled`, `tm_fix_protection_enabled` (Phase 1),
`lqa_glossary_context_enabled` (Phase 2) and `lqa_adjudication_enabled` (Phase 3).

Documented in: docs/architecture/quality-lqa-evaluations.md
"""

from __future__ import annotations

import re
import unicodedata

from app.services import quality, settings

# --- the revert SUPPRESSOR (adversarial review, 2026-07-28) ------------------
# HISTORICAL NOTE (Q5, core-QA fix train 1, 2026-08-10; corrected in the fix round):
# `quality.glossary_term_violations` is now itself MORPHOLOGY-TOLERANT (plural/gender
# agreement, verb conjugations, per-word multiword inflection) — but ONLY when it is given
# the target locale. This module now THREADS `target_locale` on both call sites
# (`introduced_violations`, `applied_terms`); without it the matcher falls back to the
# generic append-only family and a conjugation like "Descargue" for required "descarga"
# would STILL read as a miss and this suppressor would carry it. With the locale threaded,
# most cases this suppressor was built for no longer even register as a detected
# violation. It is KEPT as a further, stronger backstop: it still covers what the core
# matcher deliberately does not (DE compounding, typographic-apostrophe/hyphen/space
# normalisation, and any locale outside the es/fr/en stem tables), and it can only ever
# SUPPRESS a revert, never add one — so it stays strictly safe. The original rationale
# (kept for the record):
# `quality.glossary_term_violations` documented its own accepted limitation in as many
# words: the match was on the SURFACE target_term, it had no lemmatizer, and an inflected
# or agreement-shifted form "will still be reported as a miss". That limitation was
# accepted BECAUSE "the consequence is a SOFT review flag ... never a block and NEVER A
# REWRITE: a false positive costs one glance by a reviewer".
#
# (C) is the first consumer for which that sentence is FALSE. Here a false positive
# DISCARDS the model's edit. Reproduced against the shipped matcher: `aparato` ->
# `aparatos` (ES/FR plural), `Datenbank` -> `Datenbankverbindung` (DE compounding),
# `d'aujourd'hui` -> `d’aujourd’hui` (typographic-apostrophe normalisation, a
# class the Acme [E2E] corpus already measured) and a renamed `{placeholder}` whose
# interior spuriously satisfied the pre-fix match ALL read as "the edit introduced a
# violation" and would revert a good edit.
#
# So detection stays exactly where it was — `quality` alone decides what a violation IS,
# and the write-back gate still flags every one of these — but DISCARDING TEXT now needs
# stronger evidence than a surface-form mismatch: the required term must have genuinely
# DISAPPEARED, not merely inflected. `_survives_softly` is that evidence test. It is
# LEFT-ANCHORED on purpose (the term must still start at a token boundary), so a
# suffix/compound survives while a short term hiding inside an unrelated word does not:
# a required "AI" is NOT considered to survive in "email".
#
# The asymmetry is deliberate and matches the proposal's own invariant ("every fail-safe
# points at 'leave the finding, keep the human'"): suppressing a revert costs a flagged
# segment a human already has to look at, while a wrong revert throws away real work.
# DNT is NOT relaxed — a do-not-translate term is defined as surviving VERBATIM, so any
# alteration of it is a real breach.
_APOSTROPHES = "’ʼ‘´`"
_HYPHENS = "‐‑‒–—−"
_SPACES = "         "


def _fold(text: str, casefold: bool = True) -> str:
    """Typography-insensitive folding: NFKC, casefold, and the apostrophe / hyphen /
    space variants an LLM post-editor normalises between the pre-fix and post-fix text.
    Used ONLY to decide whether a detected violation is strong enough to revert.

    `casefold=False` keeps case (adversarial review: Phase 3's span match has to honour
    a `case_sensitive` glossary entry, which a blanket casefold silently threw away —
    an entry pinned to "IT" adjudicated a finding about the pronoun "it")."""
    out = unicodedata.normalize("NFKC", text or "")
    if casefold:
        out = out.casefold()
    out = "".join(
        "'" if ch in _APOSTROPHES else "-" if ch in _HYPHENS else " " if ch in _SPACES else ch
        for ch in out
    )
    return re.sub(r"\s+", " ", out)


def _survives_softly(term: str, text: str) -> bool:
    """True when `term` still starts at a token boundary in `text` after folding, with
    NO right boundary required — so `aparato` survives in `aparatos`, `Datenbank` in
    `Datenbankverbindung`, and `d'aujourd'hui` in `d’aujourd’hui`, while `AI`
    does NOT survive in `email` (no left boundary) and a term that was genuinely
    replaced does not survive at all."""
    folded = _fold(term)
    if not folded:
        return False
    return re.search(r"(?<![a-z0-9])" + re.escape(folded), _fold(text)) is not None


# --- qa_flag types ----------------------------------------------------------
# The SEGMENTED revert: the fixer's edit introduced a term violation, so the PRE-fix
# text was retained and the edit discarded. Named exactly as the proposal names it.
FIX_REVERTED_FLAG_TYPE = "fix_reverted"
# The FULL_TEXT counterpart. The proposal (§4.C, "FULL_TEXT asymmetry") says the
# whole-document path FLAGS and does NOT revert — reverting a whole document because of
# one introduced violation is far too blunt. A DISTINCT type keeps the names honest: a
# `fix_reverted` flag always means text was discarded, and this one always means it was
# not. Both are surfaced by the editor grid / headless review summary.
FIX_REGRESSION_FLAG_TYPE = "fix_term_regression"


def fix_guard_enabled() -> bool:
    """The (C) post-check kill switch (`glossary_fix_guard_enabled`, default ON).
    OFF restores the pre-Phase-1 behaviour exactly: a provider result always replaces
    the working text, no delta is computed, no flag is written."""
    return str(settings.get("glossary_fix_guard_enabled", "1")) != "0"


def tm_protection_enabled() -> bool:
    """The (D1) TM provenance-guard kill switch (`tm_fix_protection_enabled`, default
    ON). OFF restores the pre-Phase-1 behaviour exactly: every later step re-runs the
    TM-filled segments (Gap 4)."""
    return str(settings.get("tm_fix_protection_enabled", "1")) != "0"


def introduced_violations(source_text, pre_text, post_text, glossary_terms, *, target_locale=None):
    """The glossary / DNT violations `post_text` has that `pre_text` did NOT.

    Returns `(new_dnt, new_glossary)` where `new_dnt` is a list of DNT source terms
    (as `quality.dnt_violations` returns) and `new_glossary` a list of
    `{source_term, target_term}` dicts (as `quality.glossary_term_violations` returns),
    each restricted to the entries ABSENT from the pre-edit text's violation set.

    A DELTA, not an absolute (the proposal's §4.C predicate):
      * an edit that leaves a pre-existing violation untouched -> empty (the write-back
        gate already flags it; that is not this guard's business),
      * an edit that FIXES a violation -> empty (obviously kept),
      * an edit that BREAKS a term that survived the previous step -> reported.

    `target_locale` (F2) is threaded into `quality.glossary_term_violations` on BOTH legs
    so the delta uses the caller's per-language morphology tables, not the generic append-
    only family. This is what makes the guard stop DISCARDING a good conjugation: the pre-
    edit target's inflected required term ("Descargue" for "descarga") is now recognised as
    PRESENT on the pre leg, so the edit is not accused of introducing a violation. NOTE the
    delta semantics: a form that no longer counts as a pre-text violation can now surface a
    genuinely-introduced post violation the append-only comparison used to mask — the
    correct direction (the guard reports a real break instead of hiding it).

    Empty (a no-op) whenever `glossary_terms` is falsy or either text is empty, so an
    install with no glossary can never revert anything."""
    if not glossary_terms or not source_text or not post_text:
        return [], []
    pre_dnt = set(quality.dnt_violations(source_text, pre_text, glossary_terms))
    post_dnt = quality.dnt_violations(source_text, post_text, glossary_terms)
    new_dnt = [t for t in post_dnt if t not in pre_dnt]
    pre_gloss = {
        (m["source_term"], m["target_term"])
        for m in quality.glossary_term_violations(
            source_text, pre_text, glossary_terms, target_locale=target_locale
        )
    }
    post_gloss = quality.glossary_term_violations(
        source_text, post_text, glossary_terms, target_locale=target_locale
    )
    # ...and, for the GLOSSARY arm only, require real DISAPPEARANCE rather than a
    # surface-form mismatch before the caller is allowed to discard text (see
    # `_survives_softly`). DNT stays strict: verbatim survival is its definition.
    new_gloss = [
        m
        for m in post_gloss
        if (m["source_term"], m["target_term"]) not in pre_gloss
        and not _survives_softly(m["target_term"], post_text)
    ]
    return new_dnt, new_gloss


def _terms_label(new_dnt, new_gloss) -> str:
    """ASCII-safe, comma-joined list of the terms an edit broke, for a flag message
    (Windows-console CLI parity — no em-dashes, no smart quotes)."""
    bits = [str(t) for t in new_dnt]
    bits += ["{} -> {}".format(m["source_term"], m["target_term"]) for m in new_gloss]
    return ", ".join(bits)


def _flag(flag_type: str, message: str, step_name: str, new_dnt, new_gloss) -> dict:
    return {
        "type": flag_type,
        "severity": "major",
        "message": message,
        "step": step_name or "",
        "dnt_terms": [str(t) for t in new_dnt],
        "glossary_terms": [
            {"source_term": m["source_term"], "target_term": m["target_term"]} for m in new_gloss
        ],
        "reason": "step edit introduced a terminology violation",
    }


def revert_flag(step_name: str, new_dnt, new_gloss) -> dict:
    """One qa_flags entry for a SEGMENTED edit that was rejected and reverted — shaped
    like `quality.dnt_flag` / `glossary_flag` ({type, severity, message}) so the CAT
    editor renders the badge through the existing generic code path. 'major': a human
    should look, because an automated improvement was deliberately discarded."""
    return _flag(
        FIX_REVERTED_FLAG_TYPE,
        "Step {!r} introduced a terminology violation ({}); its edit was REVERTED "
        "(discarded) and the text it was editing was kept.".format(
            step_name or "(unnamed)", _terms_label(new_dnt, new_gloss)
        ),
        step_name,
        new_dnt,
        new_gloss,
    )


def regression_flag(step_name: str, new_dnt, new_gloss) -> dict:
    """One qa_flags entry for a FULL_TEXT (whole-document) step that introduced a
    terminology violation. The text is NOT reverted (§4.C: whole-document revert
    granularity is far too blunt) — the flag ATTRIBUTES the regression to the step that
    caused it, on top of the absolute glossary/DNT flags the write-back gate already
    raises."""
    return _flag(
        FIX_REGRESSION_FLAG_TYPE,
        "Step {!r} introduced a terminology violation ({}); whole-document output is not "
        "reverted -- review required.".format(
            step_name or "(unnamed)", _terms_label(new_dnt, new_gloss)
        ),
        step_name,
        new_dnt,
        new_gloss,
    )


def merge_flags(existing: list | None, flags) -> list:
    """qa_flags with any prior fix-guard entries replaced by `flags`; every OTHER flag
    family (refusal, qe, dnt, glossary, structural, tag) is preserved. A fresh list each
    call so SQLAlchemy sees the JSON column as changed (the `merge_dnt_flags` /
    `refusal.merge_flag` / `qe.merge_qe_flag` pattern)."""
    kinds = {FIX_REVERTED_FLAG_TYPE, FIX_REGRESSION_FLAG_TYPE}
    out = [f for f in (existing or []) if not (isinstance(f, dict) and f.get("type") in kinds)]
    out.extend(flags)
    return out


def tm_protected_indices(pending, tm_origin, step, is_correction_loop: bool) -> list[int]:
    """D1 component of step_refused_indices; preserves its separate telemetry."""
    if not tm_protection_enabled() or not tm_protect_for(step, is_correction_loop):
        return []
    return [j for j in pending if tm_origin[j]]


LQA_EMPTY_SCOPE_PREFIX = "LQA skipped: no eligible segments;"


def step_refused_indices(
    pending,
    tm_origin,
    step,
    is_correction_loop,
    *,
    tm_hard_idx,
    tm_soft_idx,
    content_protected,
    status_protected,
    refused_seg,
    tm_protected_idx=None,
) -> set[int]:
    """Existing dispatch refusal union, restricted to this step's pending slots.

    Only D1 uses the switch/tristate. Soft locks protect non-correction steps.
    The optional output list preserves the D1-only safety counter and trace.
    """
    d1 = tm_protected_indices(pending, tm_origin, step, is_correction_loop)
    if tm_protected_idx is not None:
        tm_protected_idx.extend(d1)
    refused = (
        set(d1)
        | set(tm_hard_idx)
        | set(content_protected)
        | set(status_protected)
        | set(refused_seg)
    )
    if not is_correction_loop:
        refused.update(tm_soft_idx)
    return set(pending) & refused


def tm_lqa_exclusion_enabled() -> bool:
    """Correction-loop refusal exclusion switch; registered default ON."""
    return str(settings.get("lqa_tm_lock_exclusion_enabled")) != "0"


def tm_protect_for(step, is_correction_loop: bool) -> bool:
    """Whether THIS step visit protects `tm_origin` segments (D1).

    `WorkflowStep.tm_protect` is a TRISTATE:
      * None (the default, and every legacy row) -> protect only the CORRECTION-LOOP
        shape (a step gated on `prev_lqa:failed`, or an LQA `on_fail` loop-back
        target). Deliberately narrow: protecting TM segments from EVERY later step
        would change the semantics of every existing "Translator -> In-country
        reviewer" workflow, which some users deliberately use to polish TM output.
      * True  -> always protect (e.g. pin a polish step off the TM segments).
      * False -> never protect (unprotect a fixer that is SUPPOSED to rewrite TM).

    A workflow with no LQA step has no correction-loop-shaped step, so the default
    resolves False everywhere and the run is byte-identical to pre-Phase-1."""
    flag = getattr(step, "tm_protect", None)
    if flag is None:
        return bool(is_correction_loop)
    return bool(flag)


# =============================================================================
# PHASE 3 (B) — DETERMINISTIC FINDINGS ADJUDICATION
# =============================================================================
# PROPOSAL-lqa-glossary-adjudication §4.B. Phase 2 gave the judge the approved terms as
# DATA-WITH-CAVEAT; this is the layer that makes the guarantee, because "prompts
# persuade, gates enforce". A finding whose span targets a LEGITIMATELY APPLIED approved
# glossary term is DOWNGRADED to `neutral` before `lqa._score` runs.
#
# WHY `neutral` AND NOT DELETION. `lqa.PENALTY` already maps `"neutral": 0` and
# `lqa._score` fails the gate only on `major`/`critical`, so a downgraded finding
# contributes ZERO penalty and CANNOT fail the gate while remaining a first-class member
# of `card["errors"]`. No `_score` change is needed and none was made. There is
# deliberately NO suppress/delete tier: deleting is unauditable, and the `neutral`
# primitive already gives everything a suppress tier would. NOTHING VANISHES.
#
# WHY THIS LIVES HERE AND NOT IN `lqa`. The predicate needs `_survives_softly` — the
# folded, left-anchored survival test Phase 1 built so a REVERT needs stronger evidence
# than a surface-form mismatch. Adjudication needs exactly the same relaxation for
# exactly the same reason (`aparato` applied as `aparatos`, `d'aujourd'hui` typed with a
# typographic apostrophe): a term the model DID apply, in an inflected form, must not
# read as "not applied". Reusing the one implementation is what keeps the revert gate
# and the adjudicator from ever disagreeing about what "the term is still there" means.
#
# WHERE THE PROPOSAL SAID `app/services/lqa_adjudicate.py`: this is the same entry
# point (`adjudicate(errors, source, target, glossary_terms)`) in the module that
# already owns the shared survival primitive, rather than a new module reaching across
# a private name. Documented deviation.
ADJUDICATION_RULE = "glossary-approved-term"

# §4.B rule 6 — the ONE finding class adjudication must never touch: the judge saying
# "the approved term is WRONG here". Adjudicating it would make Phase 2 and Phase 3
# cancel each other out. Compared case-folded so a judge's casing cannot dodge the rule.
_MISUSE_DIM = "terminology/approved-term-misuse"
# ...and compared with EVERY non-alphanumeric stripped (adversarial review). An exact /
# "starts with the dimension + '/'" comparison let three shapes real judges actually emit
# walk straight past the rule and adjudicate away the one finding class that must never
# be touched: "Terminology/Approved-term-misuse (wrong sense)" (a trailing
# parenthetical), "Terminology/Approved term misuse" (spaces for hyphens) and
# "Terminology/Approved-Term-Misuse - plural mismatch" (a trailing dash clause). The
# stripped key matches all of them, and nothing that is NOT this category can start with
# it. Reproduced against the shipped predicate before this was added.
_MISUSE_KEY = "terminologyapprovedtermmisuse"

# §4.B rule 5 — the dimension allow-list. `Terminology*` and `Style*` are where an
# approved-term false positive lands, so they adjudicate on a span that CONTAINS the
# term. `Accuracy*` adjudicates ONLY when the span IS the term (never a superstring), so
# "the whole clause is mistranslated" is never downgraded merely because the clause
# happens to contain an approved term. `Fluency*`, `Design*` (the inline-tag axis) and
# `Locale convention*` are NEVER adjudicated.
_CONTAINS_DIMS = ("terminology", "style")
_EXACT_DIMS = ("accuracy",)

# --- rule 5 PROPORTIONALITY BOUND (approved spec amendment, 2026-07-29) ------
# The residual left by the rule-2 tightening. `_span_targets_term` fixed WHICH strings
# count as an occurrence of the term (strict whole-token, so `aparatoso` no longer
# matches an approved `aparato`) — but it says nothing about HOW MUCH OF THE SPAN the
# term accounts for. So a Style*/Terminology* finding about a whole CLAUSE adjudicated
# merely because that clause happened to contain an approved term somewhere inside it.
#
# REPRODUCED: a `Style/Register` finding about shouting capitals on
# "Envie el correo AHORA POR FAVOR!!!" was silenced by an approved `email -> correo`,
# because `correo` is a whole token of the span. The finding is not about `correo` at
# all; the term is 1 of 6 tokens. That is a REAL, human-visible finding withheld from
# the fixer and removed from the gate.
#
# THE BOUND: the term must be a SUBSTANTIAL FRACTION of the span — at least half its
# tokens — or the span must BE the term. Tokens are whitespace-split on the folded
# strings, so the count uses exactly the normalisation the match itself used.
#
# ...EXCEPT WHERE WHITESPACE TOKENS CARRY NO INFORMATION (adversarial review,
# 2026-07-29). A whitespace token count is a PROXY for "how much of the span", and the
# proxy DEGENERATES to 1-of-1 whenever the span has no whitespace — which is EVERY span
# in zh/ja/th, the scripts this codebase is otherwise careful about (`quality._term_present`
# is explicitly CJK-safe). REPRODUCED against the first implementation of this bound: an
# approved `email -> 电子邮件` silenced a `Style/Register` finding whose span was the whole
# clause "请立即发送电子邮件！！！" (1 token vs 1 token -> 2 >= 1 -> adjudicated), i.e. the
# bound was a complete NO-OP for CJK glossaries and the residual it exists to close was
# still fully open there. The same degeneracy is reachable in Latin script by gluing:
# a span the judge reports hyphen-joined ("Envie-el-correo-AHORA-POR-FAVOR") folds to one
# token and adjudicated too.
# So when the folded span is a SINGLE token the bound falls back to CHARACTERS with the
# same one-half fraction. That fallback is exact on the spec's pinned table (every row of
# it is multi-token or a span == term early return, so the table is untouched) and it is
# the same direction as the token rule: failing it costs LOST SUPPRESSION only.
#
# The direction is the one §8 risk 3 calls safe. Failing the bound costs LOST
# SUPPRESSION: the finding keeps its severity and stays visible to the fixer and the
# gate, which is the state the whole feature is a refinement of. Rule 5's `Accuracy*`
# exact-match arm and rule 6 (misuse is never adjudicated) are untouched — Accuracy
# already required span == term, which satisfies the bound trivially.
_PROPORTION_DENOMINATOR = 2  # term_tokens >= span_tokens / 2


def _span_is_mostly_term(surface: str, span: str, case_sensitive: bool) -> bool:
    """The rule-5 proportionality bound: is the approved `surface` a substantial
    fraction of `span`?

    True when the folded span IS the folded term, or when the term carries at least half
    the span's whitespace tokens. Boundary cases, pinned by the suite: a single-token
    span equal to the term adjudicates (1 of 1); a term that is 1 of 2 tokens adjudicates
    (the boundary — `>=` half, not `>`); a term that is 2 of 5 does NOT; a two-token term
    inside a three-token span does (`prime d'encouragement` in
    `la prime d'encouragement`).

    A span with NO whitespace (every CJK span; a hyphen-glued Latin one) is measured in
    CHARACTERS at the same one-half fraction instead — see the block comment above for
    why the token count is uninformative there. `电子邮件` in the whole clause
    `请立即发送电子邮件！！！` is 4 of 12 characters and does NOT adjudicate; in
    `电子邮件地址` it is 4 of 6 and does."""
    cs = bool(case_sensitive)
    folded_term = _fold(surface, casefold=not cs).strip()
    folded_span = _fold(span, casefold=not cs).strip()
    if folded_span and folded_span == folded_term:
        return True  # the span IS the term
    term_tokens = folded_term.split()
    span_tokens = folded_span.split()
    if not term_tokens or not span_tokens:
        return False
    if len(span_tokens) == 1:
        # Whitespace tokens carry no information here (the token rule would pass
        # unconditionally): measure the same fraction in characters.
        return len(folded_term) * _PROPORTION_DENOMINATOR >= len(folded_span)
    return len(term_tokens) * _PROPORTION_DENOMINATOR >= len(span_tokens)


def _switch_on(key: str) -> bool:
    """Read a "1"/"0" kill switch defensively.

    Phases 2 and 3 hook `lqa.score_text` — a pure, DB-free scoring function that
    non-workflow callers (the A/B harnesses, the judge-parser suites) invoke with no
    Setting table in scope at all. A settings read must therefore never be able to raise
    out of a scoring call; on any failure we fall back to `settings.DEFAULTS`, which is
    exactly the value `settings.get` returns when the row is simply absent. It is the
    same soft-degrade posture the rest of the LQA path already takes (a dead engine
    yields a clean scorecard rather than an exception)."""
    try:
        value = settings.get(key, settings.DEFAULTS.get(key, "1"))
    except Exception:
        value = settings.DEFAULTS.get(key, "1")
    return str(value) != "0"


def glossary_context_enabled() -> bool:
    """The Phase 2 (A) kill switch (`lqa_glossary_context_enabled`, default ON).
    OFF -> `lqa._glossary_clause` receives None and renders zero bytes, so the judge
    prompt is byte-identical to the pre-Phase-2 prompt. Adjudication is INDEPENDENT of
    this switch: a judge that still returns spans still gets adjudicated (and one that
    does not simply yields nothing to adjudicate — the safe direction)."""
    return _switch_on("lqa_glossary_context_enabled")


def adjudication_enabled() -> bool:
    """The Phase 3 (B) kill switch (`lqa_adjudication_enabled`, default ON). OFF ->
    `adjudicate` is an exact identity function: no finding is downgraded, no marker is
    written, no `adjudications` key is attached to the card, and the score/gate are the
    pre-Phase-3 values."""
    return _switch_on("lqa_adjudication_enabled")


def _approved_surface(term: dict) -> str:
    """The string the TARGET is expected to carry for this entry: the `target_term` for
    an ordinary entry, and the `source_term` for a DNT entry (whose approved rendering
    IS the source string, kept verbatim)."""
    if term.get("dnt"):
        return term.get("source_term") or ""
    return term.get("target_term") or ""


def applied_terms(source_text, target_text, glossary_terms, *, target_locale=None) -> list[dict]:
    """The entries from `glossary_terms` that are LEGITIMATELY APPLIED in this pair —
    §4.B rule 3: the entry's `source_term` appears in the source AND its approved
    surface appears in the target, i.e. exactly the entries
    `quality.glossary_term_violations` / `quality.dnt_violations` would NOT report.

    `target_locale` (F2) is threaded into `quality.glossary_term_violations` so the
    applied/missed decision uses the caller's per-language morphology tables, not the
    generic append-only family (the same reason `introduced_violations` threads it).

    A term the model FAILED to apply is never adjudicable: the deterministic write-back
    check should fire on it, and a finding about it is probably RIGHT.

    Detection is delegated wholesale to `quality` (one matcher in the codebase), then
    relaxed by `_survives_softly` on the GLOSSARY arm only — the same asymmetry Phase 1
    established: `quality` alone decides what a violation IS, but an inflected form of a
    required term ("aparato" applied as "aparatos") must not read as "absent" when the
    consequence is a decision about a HUMAN-VISIBLE finding. DNT stays STRICT: a
    do-not-translate term is defined as surviving verbatim, so any alteration of it is a
    real breach and the finding stands.

    RULE 4 (sense/context) is satisfied STRUCTURALLY, not by a check: `glossary_terms` is
    the caller's ALREADY-RESOLVED `match_terms` output, which carries exactly ONE entry
    per source term at the sense `_choose_sense` picked for THIS text. A different
    sense's target string is simply not in this list, so it can never adjudicate — and
    this function never re-queries the glossary (§8 risk 7)."""
    if not glossary_terms or not source_text or not target_text:
        return []
    missed_gloss = {
        (m["source_term"], m["target_term"])
        for m in quality.glossary_term_violations(
            source_text, target_text, glossary_terms, target_locale=target_locale
        )
    }
    missed_dnt = set(quality.dnt_violations(source_text, target_text, glossary_terms))
    out: list[dict] = []
    for t in glossary_terms:
        if not isinstance(t, dict):
            continue
        st = t.get("source_term") or ""
        surface = _approved_surface(t)
        if not st or not surface:
            continue
        if not quality._term_present(st, source_text, bool(t.get("case_sensitive"))):
            continue  # the entry does not even apply to this source
        if t.get("dnt"):
            if st in missed_dnt:
                continue
        elif (st, t.get("target_term")) in missed_gloss and not _survives_softly(
            surface, target_text
        ):
            continue
        out.append(t)
    return out


def _span_targets_term(surface: str, span: str, case_sensitive: bool) -> bool:
    """§4.B rule 2, verbatim: does `span` carry an occurrence of the approved `surface`?

    The proposal names ONE matcher for this — `quality._term_present`, the whole-token,
    CJK-safe, `case_sensitive`-honouring matcher the write-back gate uses — "so
    adjudication and the write-back gate never disagree about what 'the term is
    present' means". It is applied to the TYPOGRAPHY-FOLDED strings so the apostrophe /
    hyphen / NBSP variants a model normalises (`d'aujourd'hui` -> `d’aujourd’hui`) still
    match, and the fold keeps case whenever the entry is case-sensitive.

    ADVERSARIAL REVIEW — WHY NOT `_survives_softly` HERE. The first implementation reused
    Phase 1's LEFT-ANCHORED survival test for this rule too. That test exists to make a
    REVERT need strong evidence, where over-matching is the SAFE direction (it suppresses
    a revert and leaves the finding for a human). Here the asymmetry is EXACTLY INVERTED:
    over-matching SUPPRESSES a real, human-visible finding, withholds it from the fixer
    and can flip the gate to pass. With no right boundary, an approved `aparato`
    adjudicated a genuine Terminology finding about the unrelated `aparatoso`, and
    (because the segmented branch adjudicates the whole SEG_JOINER blob against the
    document-level union of terms) a term applied correctly in one segment silenced a
    finding in another. Both reproduced. The cost of tightening is LOST SUPPRESSION on an
    inflected span (`aparatos`) — the direction §8 risk 3 explicitly calls the safe one,
    and one Phase 1 already covers downstream: if the fixer then breaks the term, (C)
    reverts the edit. Rule 3 (`applied_terms`) keeps the soft test, because there
    over-matching only decides that a term WAS applied, which never suppresses anything.
    """
    if not surface or not span:
        return False
    cs = bool(case_sensitive)
    return quality._term_present(_fold(surface, casefold=not cs), _fold(span, casefold=not cs), cs)


def _dim_matches(dimension, span: str, term: dict) -> bool:
    """§4.B rules 5 + 6: is a finding on `dimension` with this `span` adjudicable
    against this approved `term`?"""
    dim = " ".join(str(dimension or "").split()).casefold()
    if re.sub(r"[^a-z0-9]+", "", dim).startswith(_MISUSE_KEY):
        return False  # rule 6 — never
    surface = _approved_surface(term)
    cs = bool(term.get("case_sensitive"))
    if dim.startswith(_CONTAINS_DIMS):
        # span CONTAINS the term (as a whole token) AND the term is a substantial
        # fraction of the span (the 2026-07-29 proportionality bound).
        return _span_targets_term(surface, span, cs) and _span_is_mostly_term(surface, span, cs)
    if dim.startswith(_EXACT_DIMS):
        # span IS the term
        return bool(surface) and (_fold(span, casefold=not cs) == _fold(surface, casefold=not cs))
    return False


def adjudicate(errors, source_text, target_text, glossary_terms, *, target_locale=None):
    """`(errors_out, adjudications)` — findings whose span targets a legitimately-applied
    approved glossary term, DOWNGRADED to `neutral` and MARKED. Never deleted.

    Called from `lqa.score_text` between the error assembly and `_score`, so both the
    numeric score and the gate reflect adjudication.

    An adjudicable finding must satisfy ALL of §4.B's predicate:
      1. it carries a non-empty `span` that really OCCURS in the target (folded). No
         span -> untouched; a paraphrased/hallucinated span -> untouched. Brittleness
         therefore costs LOST SUPPRESSION, never lost detection — the safe direction,
         and it is why a structural/rulebook finding (which never carries a span) is
         structurally untouchable here;
      2. the span targets an approved term (`_survives_softly`, or exact for Accuracy);
      3. that term is LEGITIMATELY APPLIED in this pair (`applied_terms`);
      4. the sense is the one resolved for this text (structural — see `applied_terms`);
      5. the dimension is on the allow-list, and — for the `Terminology*`/`Style*`
         CONTAINS arm — the term is a SUBSTANTIAL FRACTION of the span
         (`_span_is_mostly_term`, the 2026-07-29 proportionality bound), so a finding
         about a whole clause is not silenced by one approved term buried in it;
      6. it is not a `Terminology/Approved-term-misuse` finding.

    IDEMPOTENT across a correction-loop's repeat LQA visits: an already-adjudicated
    finding is passed through untouched rather than re-marked, so a multi-visit loop
    cannot compound markers or double-count the audit tally.

    IDENTITY when the kill switch is off, when there are no errors, or when
    `glossary_terms` is falsy -> every install without a glossary is unchanged."""
    src_errors = list(errors or [])
    # The cheap, DB-free guards run FIRST so an install with no glossary never pays a
    # Setting read per LQA call (`score_text` is otherwise DB-free). Identical outcome
    # either way — all three arms return the input unchanged.
    if not src_errors or not glossary_terms or not adjudication_enabled():
        return src_errors, []
    applied = applied_terms(source_text, target_text, glossary_terms, target_locale=target_locale)
    if not applied:
        return src_errors, []
    folded_target = _fold(target_text or "")
    out: list[dict] = []
    adjudications: list[dict] = []
    for e in src_errors:
        if not isinstance(e, dict) or e.get("adjudicated"):
            out.append(e)
            continue
        raw_span = e.get("span")
        span = raw_span.strip() if isinstance(raw_span, str) else ""
        if not span or _fold(span) not in folded_target:
            out.append(e)
            continue
        hit = next((t for t in applied if _dim_matches(e.get("dimension"), span, t)), None)
        if hit is None:
            out.append(e)
            continue
        record = {
            "rule": ADJUDICATION_RULE,
            "term": "{} -> {}".format(hit.get("source_term") or "", _approved_surface(hit)),
            "original_severity": str(e.get("severity") or ""),
        }
        marked = dict(e)
        marked["severity"] = "neutral"
        marked["adjudicated"] = record
        out.append(marked)
        adjudications.append({**record, "dimension": str(e.get("dimension") or ""), "span": span})
    return out, adjudications


def adjudication_summary(adjudications) -> dict:
    """`{"count": N, "by_rule": {rule: n}}` — the card counter §4.B asks for (count +
    per-rule breakdown). One rule exists today; the shape is per-rule so a second
    adjudication rule needs no consumer change."""
    by_rule: dict = {}
    for a in adjudications or []:
        rule = (a or {}).get("rule") or ADJUDICATION_RULE
        by_rule[rule] = by_rule.get(rule, 0) + 1
    return {"count": len(adjudications or []), "by_rule": by_rule}


def tm_protect_label(value) -> str:
    """Human/ASCII label for the tristate, shared by the CLI table and the web badge so
    the two surfaces cannot drift."""
    if value is None:
        return "auto"
    return "on" if value else "off"
