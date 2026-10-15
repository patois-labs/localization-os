"""Deterministic Jev preflight, using the write seam's shared recognizers.

Documented in: docs/architecture/quality-lqa-evaluations.md
"""

import re
from collections import Counter

from app.services import qa, qe_jev_numbers, quality, segment

# Scripts written without word spaces (CJK, Thai, Lao, Khmer, Myanmar): a run of
# them is one token only while it stays word-sized and unpunctuated, otherwise it
# is multi-word text and a copy of it is a full untranslated sentence.
_SPACELESS_SCRIPT_RE = re.compile(
    "[\u3040-\u30ff\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff\U00020000-\U0002a6df"
    "\u0e00-\u0e7f\u0e80-\u0eff\u1780-\u17ff\u1000-\u109f]"
)
_SENTENCE_PUNCT_RE = re.compile(
    "[\u3002\uff0e\uff01\uff1f\u0e4f\u0e5a\u0e5b]"  # CJK/Thai full stop, !, ?
)

# Trailing punctuation (ASCII and full-width) is stripped before the single-token
# decision, so a short word with a closing mark is judged like the same word without
# it, exactly as 'Save!' is judged like 'Save'. Only interior sentence punctuation
# marks a multi-clause run.
_TRAILING_PUNCT_RE = re.compile("[.!?,;:…、。！，．：；？๏๚๛]+$")

# HTML void elements: one tag kind whether written <x>, <x/> or <x />.
_VOID_ELEMENTS = frozenset(
    {
        "br",
        "hr",
        "img",
        "input",
        "meta",
        "link",
        "area",
        "base",
        "col",
        "embed",
        "source",
        "track",
        "wbr",
    }
)


def _single_token(text):
    parts = text.split()
    if len(parts) != 1:
        return False
    word = parts[0]
    if _SPACELESS_SCRIPT_RE.search(word):
        word = _TRAILING_PUNCT_RE.sub("", word)
        return len(word) <= 8 and not _SENTENCE_PUNCT_RE.search(word)
    return True


# #335 N4: the number checker grades a dropped or added number MINOR ("count"), which
# covers both the legitimate cases the owner made advisory (a dot-format time, a digit
# written as a word) and real mismatches (3 items -> no number, an invented 5, 6:45 PM
# -> 19.45 Uhr). A MINOR finding is advisory only when rewriting exactly those two
# legitimate forms back to digits clears it; anything left over stays MAJOR.
_NUMBER_WORDS = {
    "en": "zero|one|two|three|four|five|six|seven|eight|nine|ten",
    "fr": "zéro|un,une|deux|trois|quatre|cinq|six|sept|huit|neuf|dix",
    "de": "null|ein,eine,eins,einen,einem,einer|zwei|drei|vier|fünf|sechs|sieben|acht|neun|zehn",
    "es": "cero|un,uno,una|dos|tres|cuatro|cinco|seis|siete|ocho|nueve|diez",
    "it": "zero|un,uno,una|due|tre|quattro|cinque|sei|sette|otto|nove|dieci",
    "pt": "zero|um,uma|dois,duas|três|quatro|cinco|seis|sete|oito|nove|dez",
    "nl": "nul|een,één|twee|drie|vier|vijf|zes|zeven|acht|negen|tien",
    "sv": "noll|en,ett|två|tre|fyra|fem|sex|sju|åtta|nio|tio",
    "da": "nul|en,et|to|tre|fire|fem|seks|syv|otte|ni|ti",
    "nb": "null|en,ei,ett|to|tre|fire|fem|seks|sju,syv|åtte|ni|ti",
    "fi": "nolla|yksi|kaksi|kolme|neljä|viisi|kuusi|seitsemän|kahdeksan|yhdeksän|kymmenen",
    "pl": "zero|jeden,jedna,jedno|dwa,dwie|trzy|cztery|pięć|sześć|siedem|osiem|dziewięć|dziesięć",
    "ru": "ноль|один,одна,одно|два,две|три|четыре|пять|шесть|семь|восемь|девять|десять",
    "zh": "零|一|二,两|三|四|五|六|七|八|九|十",
    "ja": "零,ゼロ|一|二|三|四|五|六|七|八|九|十",
}
_NUMBER_WORDS["nn"] = _NUMBER_WORDS["no"] = _NUMBER_WORDS["nb"]


def _unexplained_numbers(source, target, source_locale, target_locale):
    """The number findings left after a dot-format time that the source states as a
    clock time is read as that time, and a number 0 to 10 written as a word (in a
    language listed above) is read as its digit. Empty means every MINOR finding was
    one of the owner's advisory forms."""

    def language(locale):
        return (locale or "").replace("_", "-").split("-", 1)[0].casefold()

    # A dot token copied from the source is a decimal there, so not a time here.
    target = qe_jev_numbers.rewrite_dot_times(source, target)

    def small(text):
        return Counter(
            int(t)
            for t in qa._number_tokens(qa._TIME_RE.sub(" ", text))
            if t.isdigit() and int(t) <= 10
        )

    def spell(text, other, lang):
        words = _NUMBER_WORDS.get(lang)
        if not words:
            return text
        forms = words.split("|")
        for value, count in (small(other) - small(text)).items():
            alts = "|".join(
                re.escape(w) for w in sorted(forms[value].split(","), key=len, reverse=True)
            )
            pattern = alts if lang in ("zh", "ja") else rf"(?<![^\W\d_])(?:{alts})(?![^\W\d_])"
            text = re.sub(pattern, f" {value} ", text, count=count, flags=re.IGNORECASE)
        return text

    target = spell(target, source, language(target_locale))
    source = spell(source, target, language(source_locale))
    return qa.number_findings(source, target, target_locale, source_locale, check_times=True)


def findings(source, target, source_locale, target_locale, terms=()):
    """Structural failures plus single-token advisories; never suppress the judge call."""
    source, target = source or "", target or ""
    out = []

    def add(dimension, reason, severity="major"):
        if reason:
            out.append({"dimension": dimension, "severity": severity, "reason": reason})

    # Strip protected carriers and only the applicable DNT terms, not all glossary
    # terms: an ordinary glossary entry is a translation constraint, not an exemption.
    prose = qa.mask_non_prose(source)
    single_token = _single_token(source)
    for term in terms or ():
        if single_token or not isinstance(term, dict) or not term.get("dnt"):
            continue
        word = term.get("source_term") or ""
        cs = bool(term.get("case_sensitive"))
        if word and quality._term_present(word, prose, cs):
            prose = re.sub(
                r"(?<![A-Za-z0-9])" + re.escape(word) + r"(?![A-Za-z0-9])",
                " ",
                prose,
                flags=0 if cs else re.IGNORECASE,
            )
    # Identifiers containing digits or separators are codes, not ordinary prose.
    prose = " ".join(
        word
        for word in prose.split()
        if not (any(c.isdigit() for c in word) and any(c.isalpha() for c in word))
        and not re.fullmatch(r"[A-Z][A-Z0-9]*(?:[_-][A-Z0-9]+)+", word)
    )

    def normalize(text):
        return " ".join(text.split()).casefold()

    def language(locale):
        return (locale or "").replace("_", "-").split("-", 1)[0].casefold()

    src, tgt = normalize(source), normalize(target)
    if (
        language(source_locale) != language(target_locale)
        and any(c.isalpha() for c in prose)
        and src
        and src == tgt
    ):
        add(
            "untranslated",
            "Target equals the source text (normalized whitespace and case)",
            "minor" if single_token else "major",
        )

    add("tags", quality.tag_damage(source, target))
    src_tags, tgt_tags = (
        Counter(
            (
                m[1].casefold(),
                "close"
                if t.startswith("</")
                else "void"
                if m[1].casefold() in _VOID_ELEMENTS
                else "self"
                if t.rstrip().endswith("/>")
                else "open",
            )
            for t in segment.extract_inline_tags(text)
            if (m := re.match(r"</?([A-Za-z][\w:-]*)\b", t))
        )
        for text in (source, target)
    )
    if src_tags != tgt_tags:
        add(
            "tags",
            f"Markup missing {list((src_tags - tgt_tags).elements())!r}; "
            f"added {list((tgt_tags - src_tags).elements())!r}",
        )
    add(
        "placeholders",
        quality.placeholder_violation(
            source,
            target,
            extended=True,
            source_locale=source_locale,
            target_locale=target_locale,
        ),
    )
    # #367: dropped times and dates, changed dates, lost minus signs and swapped
    # currencies keep every digit, so the value check cannot see them. Dates compared
    # here are blanked before it runs, so a correct reorder (3/14 -> 14.03.) is silent.
    semantic, value_source, value_target = qe_jev_numbers.check(
        source, target, source_locale, target_locale
    )
    numbers = qa.number_findings(
        value_source, value_target, target_locale, source_locale, check_times=True
    )
    if numbers:
        # Advisory only when a dot-format time or a digit written as a word explains
        # every finding (also a MAJOR the checker built by pairing a spelled-out 5
        # with a dot time). Otherwise a MAJOR (changed value, changed time, AM/PM
        # swap) keeps blocking, and a MINOR one (a dropped or added number, regrouped
        # digits, an ambiguous rendering) is re-checked and blocks as MAJOR.
        rest = _unexplained_numbers(value_source, value_target, source_locale, target_locale)
        if not rest:
            numbers = [
                {
                    "severity": "minor",
                    "message": "; ".join(f["message"] for f in numbers)
                    + " (a dot-format time or a number written as a word; review)",
                }
            ]
        elif all(f.get("severity") == "minor" for f in numbers):
            numbers = [{**f, "severity": "major"} for f in rest]
    for finding in numbers:
        add("numbers", finding["message"], finding.get("severity") or "major")
    for severity, message in semantic:
        add("numbers", message, severity)
    return out


def attach(card, structural):
    """Keep the judge card intact, adding an explicit hard gate and review rationale."""
    if not structural:
        return card
    blocked = any(f["severity"] == "major" for f in structural)
    reasons = "; ".join(
        f"{f['dimension']} {f['severity'].upper()}: {f['reason']}" for f in structural
    )
    judge_reason = card.get("rationale") or card.get("reason") or ""
    if card.get("skipped"):
        judge_reason = "judge did not score: " + judge_reason
    return {
        **card,
        "judge_skipped": card.get("skipped", False),
        "skipped": False if blocked else card.get("skipped", False),
        "score": card.get("score"),
        "categories": card.get("categories") or {},
        "structural_findings": structural,
        "structural_blocked": blocked,
        "judge_rationale": card.get("rationale", ""),
        "rationale": reasons + "; " + judge_reason,
    }
