"""refusal: part 2 (layered pure move)

Documented in: docs/architecture/translation-pipeline.md
"""

from __future__ import annotations

import re

from app.services.refusal_common import (
    _BLANK_RUN_RE,
    _FENCE_BLOCK_RE,
    _FENCE_TAG_RE,
    _FENCE_TRUNC_BLOCK_RE,
    _SRC_FENCE_RE,
    _strip_trunc_tail,
)


def _strip_injected_fences(text: str, source: str | None = None):
    """Remove every echoed injected fence block / orphaned or TRUNCATED fence tag from
    `text`, collapse the leftover blank lines, and return (cleaned_text, note) — or
    (text, None) when there was nothing to remove.

    SOURCE-SIDE KEEP-GUARD (mirrors LAYER 2's philosophy): if the SOURCE document itself
    mentions one of our fence tags as prose (e.g. our own docs describing the
    prompt-injection defense), that document is genuinely ABOUT the tag, so LAYER 1 is
    skipped ENTIRELY for that tag name — a translated fence-mention is real content, not an
    echo. The probe is word-boundary bounded (`<style_guideline>` does NOT protect
    `style_guide`).

    Non-empty-body guard: if removal would empty the output (it was ONLY a fence block),
    the ORIGINAL is returned untouched."""
    if "<" not in text:  # fast path: no tag can be present
        return text, None
    protected = {m.group(1).lower() for m in _SRC_FENCE_RE.finditer(source or "")}
    names: set[str] = set()

    def _named_sub(m):
        n = m.group(1).lower()
        if n in protected:
            return m.group(0)  # source is about this tag -> keep verbatim
        names.add(n)
        return ""

    new = _FENCE_BLOCK_RE.sub(_named_sub, text)
    # Truncated-closer block BEFORE the orphan/lone-truncated passes, so the opener + body
    # + half-closer go as one unit (not opener-line-then-residual-body).
    new = _FENCE_TRUNC_BLOCK_RE.sub(_named_sub, new)
    new = _FENCE_TAG_RE.sub(_named_sub, new)
    new = _strip_trunc_tail(new, protected, names)  # ReDoS-safe lone-truncated-tag pass
    if new == text:
        return text, None  # nothing of ours removed (or all protected)
    cleaned = _BLANK_RUN_RE.sub("\n\n", new).strip()
    if not cleaned:
        return text, None  # was ONLY a fence block -> never empty output
    return cleaned, "fence-echo (" + ", ".join(sorted(names)) + ")"


_LANG_MIN_CHARS = 40


_LANG_MIN_TOKENS = 8


_LANG_MIN_DISTINCT_EN = 4


_LANG_MIN_EN_RATIO = 0.20


_EN_FUNCTION_WORDS: frozenset[str] = frozenset(
    {
        "the",
        "and",
        "of",
        "to",
        "that",
        "this",
        "with",
        "for",
        "you",
        "your",
        "are",
        "have",
        "has",
        "had",
        "not",
        "be",
        "will",
        "would",
        "can",
        "could",
        "should",
        "from",
        "they",
        "them",
        "these",
        "those",
        "there",
        "their",
        "which",
        "what",
        "when",
        "where",
        "who",
        "how",
        "we",
        "our",
        "us",
        "he",
        "she",
        "his",
        "her",
        "at",
        "by",
        "an",
        "if",
        "but",
        "all",
        "one",
        "do",
        "does",
        "did",
        "my",
        "into",
        "about",
        "over",
        "under",
        "after",
        "before",
        "between",
        "is",
        "it",
        "was",
        "were",
        "been",
        "being",
        "also",
        "than",
        "then",
        "only",
        "some",
        "any",
        "more",
        "most",
        "such",
        "each",
        "other",
        "its",
        "out",
        "up",
        "down",
        "off",
        "because",
        "while",
        "during",
        "however",
        "here",
    }
)


_LATIN_TARGET_STOPWORDS: dict[str, frozenset[str]] = {
    "es": frozenset(
        {
            "el",
            "la",
            "los",
            "las",
            "de",
            "que",
            "en",
            "y",
            "un",
            "una",
            "con",
            "por",
            "para",
            "se",
            "su",
            "lo",
            "del",
            "al",
            "es",
            "como",
            "más",
            "mas",
            "pero",
            "este",
            "esta",
            "hay",
            "nuestra",
            "nuestro",
            "mensaje",
            "usuario",
            "sistema",
            "página",
            "pagina",
            "política",
            "politica",
            "indica",
            "muestra",
            "mostrado",
            "debe",
            "puede",
            "contraseña",
            "según",
            "segun",
        }
    ),
    "fr": frozenset(
        {
            "le",
            "la",
            "les",
            "de",
            "des",
            "un",
            "une",
            "et",
            "que",
            "en",
            "dans",
            "pour",
            "par",
            "avec",
            "ne",
            "pas",
            "est",
            "ce",
            "cette",
            "sur",
            "au",
            "aux",
            "du",
            "qui",
            "sont",
            "il",
            "utilisateur",
            "erreur",
            "système",
            "systeme",
            "veuillez",
            "affiché",
            "affiche",
            "doit",
            "peut",
        }
    ),
    "de": frozenset(
        {
            "der",
            "die",
            "das",
            "und",
            "den",
            "dem",
            "ein",
            "eine",
            "mit",
            "für",
            "fur",
            "ist",
            "nicht",
            "sich",
            "auf",
            "im",
            "zu",
            "von",
            "dass",
            "oder",
            "auch",
            "sind",
            "werden",
            "wird",
            "kein",
            "fehler",
            "meldung",
            "nachricht",
            "benutzer",
            "seite",
            "muss",
            "kann",
            "angezeigt",
            "einstellungen",
        }
    ),
    "it": frozenset(
        {
            "il",
            "la",
            "le",
            "lo",
            "gli",
            "di",
            "che",
            "un",
            "una",
            "con",
            "per",
            "non",
            "si",
            "del",
            "della",
            "è",
            "questo",
            "più",
            "piu",
            "sono",
            "nel",
            "utente",
            "errore",
            "messaggio",
            "sistema",
            "deve",
            "può",
            "puo",
            "mostra",
            "pagina",
            "impostazioni",
        }
    ),
    "pt": frozenset(
        {
            "os",
            "de",
            "que",
            "em",
            "um",
            "uma",
            "com",
            "por",
            "para",
            "não",
            "nao",
            "se",
            "da",
            "é",
            "este",
            "esta",
            "como",
            "mais",
            "são",
            "sao",
            "usuário",
            "usuario",
            "mensagem",
            "erro",
            "sistema",
            "deve",
            "pode",
            "página",
            "pagina",
            "mostra",
        }
    ),
    "nl": frozenset(
        {
            "de",
            "het",
            "een",
            "van",
            "en",
            "op",
            "met",
            "voor",
            "niet",
            "dat",
            "te",
            "zich",
            "aan",
            "door",
            "zijn",
            "worden",
            "geen",
            "gebruiker",
            "fout",
            "bericht",
            "systeem",
            "moet",
            "kan",
            "pagina",
            "toont",
            "instellingen",
        }
    ),
}


_NON_LATIN_SCRIPTS: dict[str, tuple[tuple[int, int], ...]] = {
    "ja": ((0x3040, 0x30FF), (0x3400, 0x4DBF), (0x4E00, 0x9FFF)),
    "zh": ((0x3400, 0x4DBF), (0x4E00, 0x9FFF)),
    "ko": ((0x1100, 0x11FF), (0x3130, 0x318F), (0xAC00, 0xD7A3)),
    "ru": ((0x0400, 0x04FF),),
    "uk": ((0x0400, 0x04FF),),
    "bg": ((0x0400, 0x04FF),),
    "sr": ((0x0400, 0x04FF),),
    "el": ((0x0370, 0x03FF),),
    "ar": ((0x0600, 0x06FF),),
    "fa": ((0x0600, 0x06FF),),
    "he": ((0x0590, 0x05FF),),
    "hi": ((0x0900, 0x097F),),
    "th": ((0x0E00, 0x0E7F),),
}


_WORD_RE = re.compile(r"[^\W\d_]+", re.UNICODE)


# =============================================================================
# QA-3 layer (b) — the INDEPENDENT second signal: output-language != target-locale.
#
# `is_refusal` (the curated-phrase detector above) is ONE mechanism, and BOTH refusal
# call sites (quality.commit_mt_output and the segment_approve TM backstop) share it —
# so a single recall gap in the phrase set defeats defense-in-depth (the QA-3 finding).
# `looks_untranslated` is a DELIBERATELY DIFFERENT mechanism (a language-profile
# heuristic, not phrase matching), so the two sites no longer hinge on one detector:
# a non-translation that slips the phrase set — a model that simply answers in English
# for a non-English target instead of translating — is still caught.
#
# PRECISION IS THE WHOLE GAME (the refusal guard's precision is currently perfect and
# must stay so). A naive "is this English?" test false-positives on short labels, brand
# names, numbers, code, and legit English loanwords in target copy. This predicate is
# therefore gated behind a stack of AND conditions, every one of which a genuine
# translation of real content satisfies trivially (so it is NOT flagged):
#   1. the target locale is a KNOWN non-English language (English target -> never fires;
#      an unknown/unsupported target -> abstain, we cannot judge it);
#   2. the output clears a LENGTH FLOOR (chars + word tokens) — short strings (labels,
#      brands, ids, numbers, one-word loanwords, most code) carry too little signal;
#   3. the output is DECISIVELY English — a minimum count of DISTINCT English function
#      words AND a minimum English-function-word ratio (a stray English brand/loanword
#      or a quoted UI string never reaches it);
#   4. the target language's OWN profile is DECISIVELY ABSENT — for a non-Latin script
#      target, ZERO characters of that script (a real, even partial, translation always
#      has some); for a Latin-script target, ZERO of that language's function words
#      (real es/fr/de/it/pt/nl prose is saturated with them).
# Only when ALL hold does the output read as "English produced for a non-English target"
# — the report's unused-but-strong signal. The English function-word set is curated to
# UNIQUELY-English tokens (cross-language collisions like a/no/as/in/o/e/me/i/am/or/so
# are excluded) so genuine target text scores ~0 on it.
#
# WIRING (see quality.commit_mt_output `lang_guard` + web segment_approve): this fires
# only for output from a REAL provider — never the offline MockProvider, which
# deliberately ECHOES the (English) source with a "[locale]" tag and would otherwise be
# a systemic false positive. It routes to NEEDS_REVIEW under a DISTINCT flag
# (`nontranslation`, not `refusal`) and marks the unit TM-ineligible; it never rewrites
# or drops text (a caught unit is committed for the reviewer to see, but withheld from
# TM and from connector write-back, exactly like every other soft review gate).
# =============================================================================
# Minimum stripped length / word-token count for the language heuristic to even run.
# The output must carry at least this many DISTINCT English function words AND at least
# this fraction of its word tokens must be English function words to read as "English".
# UNIQUELY-English function words. Deliberately EXCLUDES tokens that are ordinary words
# in the target languages this TMS serves (a/no/as/in/on-collisions, o/e in it/pt/es,
# me in es/fr/pt, i in it, am in de, or/so) so a genuine target-language translation
# scores ~0 here and can never trip the guard.
# High-frequency function words per Latin-script target language. ANY one present in the
# output means the output IS in that language (real prose is saturated with them), so the
# guard abstains. A target NOT in this map (an unsupported Latin-script language) makes
# the guard ABSTAIN entirely — we cannot reliably tell its content from English cognates.
# Every token here is DISTINCTIVELY the target language — tokens that are ALSO ordinary
# English words (a/o/e/as/do/no/in/is/on/son/come/...) are EXCLUDED, because such a
# collision would make an English non-translation read as "target present" and wrongly
# abstain. Excluding them only costs RECALL (a missed catch), never precision: a genuine
# target sentence is still saturated with the distinctive words that remain, and the
# strong English-signal gate above is the primary protector against a false positive.
#
# FRAMING-WORD COVERAGE (adversarial-review precision hardening): the second row of each
# set adds common DISTINCTIVE content/framing words (error/message/user/system/page/
# shows/must/can + a few very-common possessives) a genuine target segment routinely uses
# to FRAME a quoted or do-not-translate ENGLISH span ("Fehler: <English msg>", "Nuestra
# politica indica: '<English quote>'"). Without them such a segment has ZERO recognized
# target tokens, so a long embedded-English quote tips the English-signal gate and the
# genuine target prose is repeatedly routed to review. Each addition is strictly
# DISTINCTIVE (no English collision), so it can only ADD an abstain on framed-quote
# content — it never lets an all-English non-translation read as "target present" (that
# threat is a pure-English body, which contains none of these), so layer-(b) recall on
# the real "answered in English" case is unchanged.
# Non-Latin target scripts: a genuine translation carries characters in that script; an
# all-Latin English output carries none. lang -> list of (lo, hi) codepoint ranges.
def _base_lang(locale: str | None) -> str:
    """The base language subtag of a locale, lowercased ("es-MX" -> "es", "pt_BR" ->
    "pt"). Empty for a falsy/degenerate locale."""
    if not locale:
        return ""
    return locale.replace("_", "-").split("-", 1)[0].strip().lower()


def looks_untranslated(text: str | None, target_locale: str | None) -> bool:
    """QA-3 layer (b): True when a PRODUCED target reads as decisively ENGLISH even
    though `target_locale` is a KNOWN non-English language whose own script / function-
    word profile is entirely absent — i.e. the model answered in English instead of
    translating. Conservative by construction (see the module section header): every
    gate below is one a genuine translation of real content clears trivially, so it is
    NOT flagged. Returns False whenever in doubt — the guard only ever DOWNGRADES to
    review, never rewrites or drops text."""
    if not text:
        return False
    lang = _base_lang(target_locale)
    # English target (or unknown target) -> English output is not a mismatch.
    if not lang or lang == "en":
        return False
    # Only judge targets whose profile we can actually detect; abstain otherwise.
    if lang not in _NON_LATIN_SCRIPTS and lang not in _LATIN_TARGET_STOPWORDS:
        return False
    stripped = text.strip()
    if len(stripped) < _LANG_MIN_CHARS:
        return False
    low = stripped.lower()
    tokens = _WORD_RE.findall(low)
    if len(tokens) < _LANG_MIN_TOKENS:
        return False
    en_hits = [t for t in tokens if t in _EN_FUNCTION_WORDS]
    if len(set(en_hits)) < _LANG_MIN_DISTINCT_EN:
        return False
    if (len(en_hits) / len(tokens)) < _LANG_MIN_EN_RATIO:
        return False
    # The target's own profile must be DECISIVELY ABSENT.
    if lang in _NON_LATIN_SCRIPTS:
        ranges = _NON_LATIN_SCRIPTS[lang]
        for ch in stripped:
            cp = ord(ch)
            if any(lo <= cp <= hi for lo, hi in ranges):
                return False  # any target-script char -> a real (even partial) translation
        return True
    stop = _LATIN_TARGET_STOPWORDS[lang]
    if any(t in stop for t in tokens):  # noqa: SIM103
        return False  # any target function word present -> the output IS in the target language
    return True


NONTRANSLATION_FLAG_TYPE = "nontranslation"


NONTRANSLATION_REASON = (
    "suspected non-translation: output language does not match the target locale"
)


# --- persistence: the DISTINCT nontranslation qa_flag ------------------------
# A separate flag type from "refusal": the report asks for "a distinct flag reason
# rather than reusing 'refusal' blindly", and the two are genuinely different verdicts
# (a curated hedge vs. a language mismatch). Same {type,severity,message} shape so the
# CAT editor renders the badge through the SAME generic code path (add it to the
# editor's _SURFACED set alongside refusal/dnt/glossary).
def nontranslation_flag(source: str | None = None) -> dict:
    """One qa_flags entry describing a segment routed to review by the language-mismatch
    signal (`looks_untranslated`). 'major' so the editor shows it as a warning; carries
    an optional `source` provenance tag (mirrors refusal_flag)."""
    entry = {
        "type": NONTRANSLATION_FLAG_TYPE,
        "severity": "major",
        "message": f"Routed to review: {NONTRANSLATION_REASON}",
        "reason": NONTRANSLATION_REASON,
    }
    if source:
        entry["source"] = source
    return entry


def merge_nontranslation_flag(existing: list | None, entry: dict) -> list:
    """qa_flags with any prior nontranslation entry replaced by `entry` (every other
    flag — refusal, qe, dnt, glossary, structural — kept). Fresh list each call so
    SQLAlchemy sees the JSON column as changed (same pattern as merge_flag)."""
    out = [
        f
        for f in (existing or [])
        if not (isinstance(f, dict) and f.get("type") == NONTRANSLATION_FLAG_TYPE)
    ]
    out.append(entry)
    return out


_OVER_TRANSLATION_SHAPES = {
    "code_fence": "a code fence",
    "markdown_table": "a cross-language markdown table",
    "multi_line": "a multi-line block from a single-line source",
    "too_long": "text far longer than the source",
}


OVER_TRANSLATION_REASON_PREFIX = "over-translation:"

# #683: a segment too long for the engine's output limit that no safe cut could fit
# (mt_chunking.plan). It rides the same withhold-to-review channel as an over-translation
# dump (nothing is committed, the run carries on), under its own honest reason prefix.
ENGINE_LIMIT_REASON_PREFIX = "too long for machine translation:"
ENGINE_LIMIT_SHAPE = "engine_output_limit"  # its over_translation shape token


# run-190 Item D / M-1: the OVER-TRANSLATION (page-dump) disposition. It rides the
# `nontranslation` flag family (NOT the refusal family) because the model did not refuse —
# it returned a page-shaped dump instead of a single-unit translation. The reason names
# WHICH shape rule fired and the source/target lengths (the shape `length_runaway` already
# uses) so an operator meeting the NEEDS_REVIEW segment can tell Item D fired and reach the
# lever (`over_translation_guard off` per step, or `OVER_TRANSLATION_MAX_RATIO 0` install-wide).
# Human-readable names for the shape reasons orchestrator._looks_like_unit_translation returns.
# run-190 round-3 (H-1 / LOW-5): the STABLE leading token of every over_translation_reason()
# string, single-sourced so the withhold marker, the qa_flag reason, the replay-parity blank
# in orchestrator._replay_done_text and quality's clean-commit clear all key off ONE prefix
# (mirrors quality.SCAFFOLD_ECHO_REASON's role). Keying replay/clear on this prefix — not on
# the `nontranslation` family alone — is what keeps a COMMITTED language-mismatch / length-
# runaway segment (same family, real target) from ever being blanked or cleared.
def over_translation_reason(
    shape: str | None = None, src_len: int | None = None, tgt_len: int | None = None
) -> str:
    """The reason string for an over-translation withhold, in the shape length_runaway
    uses: names the page-dump shape rule that fired and (when known) the char counts, then
    the lever. `shape` is orchestrator._looks_like_unit_translation's reason token; a retry
    that ERRORED carries no shape token, so the generic phrasing is used."""
    shape_txt = _OVER_TRANSLATION_SHAPES.get(shape or "", "a page-shaped dump")
    if src_len is not None and tgt_len is not None:
        counts = f" (a {src_len}-character source produced a {tgt_len}-character target)"
    else:
        counts = ""
    return (
        f"{OVER_TRANSLATION_REASON_PREFIX} the model returned {shape_txt} instead of a "
        f"single-unit translation{counts}; the target was withheld. Opt out with "
        f"over_translation_guard off, or raise/disable OVER_TRANSLATION_MAX_RATIO."
    )


def over_translation_flag(
    source: str | None = None,
    *,
    shape: str | None = None,
    src_len: int | None = None,
    tgt_len: int | None = None,
    reason: str | None = None,
) -> dict:
    """One qa_flags entry for a segment routed to review by the over-translation guard.

    `reason` (#683) replaces the built reason verbatim: the engine-output-limit withhold
    (mt_chunking) rides this channel with its own `ENGINE_LIMIT_REASON_PREFIX` sentence.
    'major' so the editor shows it as a warning; rides the `nontranslation` family (merge
    with `merge_nontranslation_flag`) and carries an optional `source` provenance tag
    (mirrors refusal_flag / nontranslation_flag). Its `type` is NEVER "refusal": the
    disposition is honestly named so an operator does not chase a model refusal that never
    happened."""
    reason = reason or over_translation_reason(shape, src_len, tgt_len)
    entry = {
        "type": NONTRANSLATION_FLAG_TYPE,
        "severity": "major",
        "message": f"Routed to review: {reason}",
        "reason": reason,
    }
    if source:
        entry["source"] = source
    return entry


_ARABIC = ((0x0600, 0x06FF), (0x0750, 0x077F), (0x08A0, 0x08FF), (0xFB50, 0xFDFF), (0xFE70, 0xFEFF))


_DEVANAGARI = ((0x0900, 0x097F), (0xA8E0, 0xA8FF))


_BENGALI = ((0x0980, 0x09FF),)


_GURMUKHI = ((0x0A00, 0x0A7F),)


_GUJARATI = ((0x0A80, 0x0AFF),)


_ORIYA = ((0x0B00, 0x0B7F),)


_TAMIL = ((0x0B80, 0x0BFF),)


_TELUGU = ((0x0C00, 0x0C7F),)


_KANNADA = ((0x0C80, 0x0CFF),)


_MALAYALAM = ((0x0D00, 0x0D7F),)


_SINHALA = ((0x0D80, 0x0DFF),)


_THAI = ((0x0E00, 0x0E7F),)


_LAO = ((0x0E80, 0x0EFF),)


_KHMER = ((0x1780, 0x17FF),)


_MYANMAR = ((0x1000, 0x109F), (0xAA60, 0xAA7F))


_TIBETAN = ((0x0F00, 0x0FFF),)


_GEORGIAN = ((0x10A0, 0x10FF), (0x1C90, 0x1CBF))


_ARMENIAN = ((0x0530, 0x058F),)


_GREEK = ((0x0370, 0x03FF), (0x1F00, 0x1FFF))


_HEBREW = ((0x0590, 0x05FF), (0xFB1D, 0xFB4F))


_ETHIOPIC = ((0x1200, 0x137F), (0x1380, 0x139F))


_THAANA = ((0x0780, 0x07BF),)


_CYRILLIC = ((0x0400, 0x04FF), (0x0500, 0x052F), (0x2DE0, 0x2DFF), (0xA640, 0xA69F))


_HAN = ((0x3400, 0x4DBF), (0x4E00, 0x9FFF), (0xF900, 0xFAFF), (0x20000, 0x2A6DF))


_KANA = ((0x3040, 0x30FF), (0x31F0, 0x31FF))


_HANGUL = ((0x1100, 0x11FF), (0x3130, 0x318F), (0xA960, 0xA97F), (0xAC00, 0xD7A3))


_EXPECTED_SCRIPTS: dict[str, tuple[tuple[int, int], ...]] = {
    "ar": _ARABIC,
    "fa": _ARABIC,
    "ur": _ARABIC,
    "ps": _ARABIC,
    "sd": _ARABIC,
    "ckb": _ARABIC,
    "ug": _ARABIC,
    "hi": _DEVANAGARI,
    "mr": _DEVANAGARI,
    "ne": _DEVANAGARI,
    "sa": _DEVANAGARI,
    "bn": _BENGALI,
    "as": _BENGALI,
    "pa": _GURMUKHI,
    "gu": _GUJARATI,
    "or": _ORIYA,
    "ta": _TAMIL,
    "te": _TELUGU,
    "kn": _KANNADA,
    "ml": _MALAYALAM,
    "si": _SINHALA,
    "th": _THAI,
    "lo": _LAO,
    "km": _KHMER,
    "my": _MYANMAR,
    "bo": _TIBETAN,
    "ka": _GEORGIAN,
    "hy": _ARMENIAN,
    "el": _GREEK,
    "he": _HEBREW,
    "yi": _HEBREW,
    "am": _ETHIOPIC,
    "ti": _ETHIOPIC,
    "dv": _THAANA,
    "ru": _CYRILLIC,
    "uk": _CYRILLIC,
    "bg": _CYRILLIC,
    "be": _CYRILLIC,
    "mk": _CYRILLIC,
    "kk": _CYRILLIC,
    "ky": _CYRILLIC,
    "mn": _CYRILLIC,
    "tg": _CYRILLIC,
    "tt": _CYRILLIC,
    "kaa": _CYRILLIC,
    "zh": _HAN,
    "yue": _HAN,
    "ja": _HAN + _KANA,
    "ko": _HANGUL,
}


_WRONG_SCRIPT_MIN_LETTERS = 16


_DENSE_SCRIPTS = _HAN + _KANA + _HANGUL + _THAI + _LAO + _KHMER + _MYANMAR + _TIBETAN


# --- OUTPUT-SCRIPT SANITY (issue #58) ----------------------------------------
# `looks_untranslated` above is FUNCTION-WORD based and only ever catches an ENGLISH
# echo (it needs `_EN_FUNCTION_WORDS` hits). The 156-locale world tour surfaced the
# OTHER catastrophic class it is structurally blind to: a non-Latin target that came
# back in the WRONG language but NOT English — `ur` (Urdu, Arabic script) delivered as
# SPANISH, `pa` (Punjabi, Gurmukhi) as Devanagari, `my` (Burmese) as romanized Latin.
# Every one was a bare short code the model mis-read, stored as a clean success.
#
# This gate is SCRIPT-based and language-agnostic: a genuine translation into a
# non-Latin-script language MUST carry characters of that script; a target with ZERO of
# them and a substantial run of some OTHER script is a wrong-language delivery. It is
# deliberately conservative — the SAME posture as `looks_untranslated`:
#   * only NON-LATIN majors are judged. Latin-script targets (es/fr/de/…) and bi-script
#     locales whose Latin output is valid (Serbian `sr`) are ABSENT from the table, so
#     the gate abstains — we cannot cheaply tell es from fr, and that class is not the
#     catastrophic one (internal notation / a whole document in the wrong alphabet is);
#   * WELL-FORMED PLACEHOLDER TOKENS are stripped before counting — ⟦⟧ tokens, `{count}`/
#     `{0}`, `%s`/`%1$d`, `<b>`/`</b>`, and URLs (their Latin letters, "count"/"b"/"https",
#     would otherwise masquerade as wrong-script content). So a mostly-placeholder segment
#     cannot trip it, and a numeric segment carries no letters at all;
#   * a letter FLOOR (`_WRONG_SCRIPT_MIN_LETTERS`) means a short brand token ("Acme")
#     kept verbatim for a non-Latin target passes — only a real sentence's worth of
#     wrong-script letters, with none of the expected script, is flagged;
#   * ANY expected-script character (even one) -> pass (a real, even partial, translation).
#
# KNOWN, ACCEPTED SOFT FALSE POSITIVE (gate review F7). DNT / glossary "keep verbatim"
# terms are NOT stripped — they are ordinary letters to this gate — so a LONG all-DNT
# target for a non-Latin locale ("API SDK HTTP JSON OAuth webhook endpoint parameters",
# a tech string legitimately kept in Latin) clears the floor with zero expected-script
# letters and is flagged wrong-script + routed to review + TM-ineligible. This is a
# DELIBERATE trade, not an oversight: stripping DNT here would need the segment's glossary/
# DNT plan (which `wrong_script` has no access to at this seam), and the gate is SOFT (it
# only DOWNGRADES to review — a human clears it), so the cost is one review flag on a rare
# all-DNT segment versus catching the catastrophic wrong-language class. Pinned as an
# accepted-noise case so the behavior is a decision on record.
#
# Returns "" whenever in doubt. Like `looks_untranslated`, it only ever DOWNGRADES a
# segment to review; it never rewrites or drops text, and it is opted into ONLY by the
# real-MT path (`lang_guard`) so MockProvider's English echo is not a systemic false
# positive.
# Base-lang subtag -> the letter ranges a genuine translation MUST contain. NON-LATIN
# majors only; Serbian (`sr`, bi-script, Latin valid) and every Latin-script locale are
# deliberately absent so the gate abstains on them.
# The WEIGHTED letter floor (a named tunable). A wrong-language delivery of real content
# clears it easily (a sentence); a brand token, an identifier, a URL or a mostly-numeric/
# placeholder segment does not. Tuned high enough that "Acme Industries"-class DNT segments
# pass (boundary: 15 alphabetic letters pass, 16 flag — pinned).
# F9 (gate review): the floor is WEIGHTED, not a raw character count, because DENSE scripts
# pack a whole sentence into far fewer characters than an alphabet does — a raw floor let an
# ENTIRE Chinese sentence (13 chars) sail past unflagged for a Hebrew-script `yi` target
# (world-tour yi seg3). Each character in a dense script (CJK Han, Japanese kana, Korean
# Hangul, Thai, Lao, Khmer, Myanmar, Tibetan) counts DOUBLE toward the floor, so ~8 of them
# clear the same bar a ~16-letter alphabetic run does. Alphabetic (Latin/Cyrillic/Arabic/
# Indic/…) letters count 1, so the 15/16 alphabetic boundary is unchanged.
def _is_dense_script(cp: int) -> bool:
    return any(lo <= cp <= hi for lo, hi in _DENSE_SCRIPTS)


# #723: an e-mail address (optionally `mailto:`) is non-content like a URL. The engine copies
# `support@example.com` verbatim (correct), and its 17 Latin letters used to clear the floor
# for every non-Latin target: 13 of 15 e-mail-only strings held for review in the format soak.
# The leading lookbehind lets a match start only at the beginning of an address-like run, so a
# long run of letters with no `@` costs one pass, not one attempt per position (#599: linear).
_NONCONTENT_RE = re.compile(
    r"https?://\S+"
    r"|(?<![A-Za-z0-9._%+-])(?:mailto:)?[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+"
    r"|\{[^{}]*\}"
    # 7a: Python `%(name)s` / `%(count)d` is a placeholder like `%s`; its
    # name's letters are not wrong-script content. Flag and width classes are disjoint (#599).
    r"|%\([A-Za-z_][A-Za-z0-9_]*\)[-#+]*[0-9.]*[hlL]?[diouxXeEfFgGcrsa]"
    r"|%\d*\$?[sdfx@]"
    r"|</?[A-Za-z][^<>]*>",
    re.IGNORECASE,
)


WRONG_SCRIPT_FLAG_TYPE = NONTRANSLATION_FLAG_TYPE


WRONG_SCRIPT_REASON = (
    "suspected wrong-script output: the target contains no characters "
    "of the target locale's expected script"
)


# Strip real inline tags / placeholders / URLs BEFORE counting, so their Latin letters
# ("count", "b", "https") never masquerade as wrong-script content for a non-Latin
# target. (⟦⟧ tokens are stripped separately via segment.strip_placeholders.)
# URLs
# {count}, {0}, {name}
# %s, %d, %1$s
# <b>, </b>, <br/>
# rides the nontranslation family
# 7a: a target for a non-Latin locale that keeps the source's product
# names and code ("TruffleRuby (GraalVM)", `<%= request.getParameter("id") %>`) is correct,
# yet its Latin letters cleared the floor (format soak, ru / zh). A Latin token is DISCOUNTED
# from the weight only when BOTH hold:
#   * it appears verbatim (same case) as a token of the SOURCE, so the engine kept it; and
#   * it looks like a name or code, not a word: CamelCase or a lower-then-upper join
#     (`TruffleRuby`, `GraalVM`, `iPhone`), an acronym (`JSON`, `HTTP`), or code characters
#     inside it (digits, `_ . / \ : = # + @ $ < > { } ( ) [ ] * & | ^ ~`).
# Plain words ("Save", "file", "the") always count, so a copied English sentence, or a
# Spanish one for a Russian target, still clears the floor and is held even when it also
# carries names. A name the source does not contain counts as before.
# The rest must be nearly nothing: fewer than `_KEPT_NAMES_MAX_OTHER_LETTERS` (8) weighted
# letters, room for a code keyword (`page`, `import`) but not for a sentence around the
# names ("Install TruffleRuby on GraalVM today" keeps 14 plain letters and is held).
# Known limit (deliberate): a product name made of plain capitalised words ("Visual Studio
# Code", 16 letters) still counts; it cannot be told from an untranslated title.
_KEPT_NAMES_MAX_OTHER_LETTERS = 8
_NAMEISH_RE = re.compile(r"[a-z][A-Z]|^[A-Z]{2}|[0-9_./\\:=#+@$<>{}()\[\]*&|^~]")
_CODEISH_RE = re.compile(r"[a-z][A-Z]|[0-9_./\\:=#+@$<>{}()\[\]*&|^~]")
_ABBREVIATION_RE = re.compile(r"[A-Za-z]{1,2}(?:\.[A-Za-z]{1,2})+")
_TOKEN_EDGE = "\"'“”‘’«»,;!?"  # noqa: S105 (punctuation, not a secret)


def _bare_token(tok: str) -> str:
    """`tok` without surrounding quotes / sentence punctuation and one wrapping pair of
    parentheses, so "(GraalVM)," compares as GraalVM while `foo()` keeps its parens."""
    tok = tok.strip(_TOKEN_EDGE)
    if tok.startswith("(") and tok.endswith(")"):
        tok = tok[1:-1]
    elif tok.startswith("(") and ")" not in tok:
        tok = tok[1:]
    elif tok.endswith(")") and "(" not in tok:
        tok = tok[:-1]
    return tok.rstrip(".:,;").strip(_TOKEN_EDGE)


def _kept_name_tokens(source: str | None) -> set[str]:
    """The source's name / code tokens (see the 7a note): the only Latin tokens a target
    may keep without them counting toward the wrong-script weight."""
    from app.services import segment

    src = _NONCONTENT_RE.sub(" ", segment.strip_placeholders(source or ""))
    # Gate (#693 / 7a): an all-caps source ("SAVE YOUR CHANGES BEFORE EXIT") has no
    # acronyms to tell apart, so the 2+ capitals rule would excuse a copied shouted
    # sentence; there only the CamelCase / code-character signals count.
    caps_ok = any("a" <= ch <= "z" for ch in src)
    out = set()
    for raw in src.split():
        tok = _bare_token(raw)
        if not tok or _ABBREVIATION_RE.fullmatch(tok):
            continue  # "e.g." / "i.e." are words, not code
        if _NAMEISH_RE.search(tok) if caps_ok else _CODEISH_RE.search(tok):
            out.add(tok)
    return out


def wrong_script(text: str | None, target_locale: str | None, source: str | None = None) -> str:
    """WRONG_SCRIPT_REASON when `text` is a NON-LATIN-script target carrying ZERO
    characters of its expected script AND a WEIGHTED wrong-script letter total of at least
    `_WRONG_SCRIPT_MIN_LETTERS` (dense-script characters count double) — a wrong-language
    delivery (Urdu produced as Spanish, Punjabi as Devanagari, Burmese as romanized Latin,
    Yiddish as a short Chinese sentence). "" otherwise (see the section header for the
    conservatism guarantees). Never raises."""
    if not text:
        return ""
    lang = _base_lang(target_locale)
    ranges = _EXPECTED_SCRIPTS.get(lang)
    if not ranges:
        return ""  # Latin / bi-script / unknown target -> abstain
    from app.services import segment

    stripped = _NONCONTENT_RE.sub(" ", segment.strip_placeholders(text))
    letters = [ord(ch) for ch in stripped if ch.isalpha()]
    # ANY expected-script letter -> a real (even partial) translation; judged first so the
    # floor never suppresses a genuine target.
    for cp in letters:
        if any(lo <= cp <= hi for lo, hi in ranges):
            return ""
    # Zero expected-script letters: weigh the wrong-script run (dense scripts x2, see F9)
    # so a short dense-script sentence cannot duck a raw character floor.
    weight = sum(2 if _is_dense_script(cp) else 1 for cp in letters)
    if weight < _WRONG_SCRIPT_MIN_LETTERS:
        return ""  # too little real text to judge (brand / code / numeric)
    if source:
        # 7a: names and code kept verbatim from the source do not count (see the note).
        kept = _kept_name_tokens(source)
        if kept:
            weight = 0
            for raw in stripped.split():
                if _bare_token(raw) in kept:
                    continue
                weight += sum(2 if _is_dense_script(ord(ch)) else 1 for ch in raw if ch.isalpha())
            if weight < _KEPT_NAMES_MAX_OTHER_LETTERS:
                return ""
    return WRONG_SCRIPT_REASON


def wrong_script_flag(reason: str | None = None, source: str | None = None) -> dict:
    """One qa_flags entry for a wrong-script delivery, in the SHARED `nontranslation`
    family (merge via `merge_nontranslation_flag`) so it composes with the language-echo
    verdict and the CAT editor renders the same badge. 'major'; carries `source`
    provenance like the other members."""
    reason = reason or WRONG_SCRIPT_REASON
    entry = {
        "type": WRONG_SCRIPT_FLAG_TYPE,
        "severity": "major",
        "message": f"Routed to review: {reason}",
        "reason": reason,
    }
    if source:
        entry["source"] = source
    return entry
