"""Lightweight inline QA checks for the CAT editor.

These are the fast deterministic checks a translator wants live while editing:
missing placeholders/tags, number mismatches, untranslated, empty. The full
LLM-based MQM/DQF scoring is a separate engine (Chunk 7, services/lqa.py); these
two complement each other (cheap-always vs deep-on-demand).

`check_segment` is ALSO the deterministic structural layer of the LQA judge
(`lqa._structural_errors` wraps it and remaps each flag onto an MQM category), so
every improvement here lands on both surfaces at once.

Documented in: docs/architecture/quality-lqa-evaluations.md
"""

import re

from app.services import quality_part4 as link_locale
from app.services.segment import (
    extract_inline_tags,
    find_placeholders,
    order_significant,
    placeholders_well_formed,
    same_token_order,
    strip_placeholders,
)

# =============================================================================
# NUMBER INTEGRITY (QA campaign 2026-07-28, BUG-06)
# =============================================================================
# THE BUG THIS CLOSES (live, prod run 27, es-ES): the source "25 items" shipped as
# "25.000 articulos" — a value corrupted 1000x — with status `mt` and `qa_flags: []`.
#
# ROOT CAUSE, in two parts:
#   1. The old check compared SORTED LISTS OF NUMERAL STRINGS
#      (`sorted(_NUM.findall(src)) != sorted(_NUM.findall(tgt))`). That is a check on
#      RENDERING, not on VALUE, so it is wrong in BOTH directions:
#        * FALSE POSITIVE on every correct locale re-rendering — the es/fr target of
#          "1,234.56" is "1.234,56", an identical VALUE, and the old check flagged it;
#        * and it produced only a MINOR flag, which `lqa._QA_TO_MQM` scores at 1 penalty
#          point (99/100) — a PASS — so a genuine 1000x corruption never failed a gate.
#      A check that cries wolf on the correct case and shrugs at the corrupt one is
#      worse than no check: reviewers learn to ignore it.
#   2. It had no notion of the TARGET LOCALE, so "25.000" was unreadable: in en-US it
#      is 25 rendered with three decimals; in es-ES it is twenty-five THOUSAND. Without
#      the locale the two readings are indistinguishable and the only honest verdict is
#      "ambiguous".
#
# THE FIX — VALUE-AWARE comparison with LOCALE-AWARE parsing of the target side:
#   * numbers are extracted as whole GROUPED tokens ("1.234,56", "1 234,56", "25.000"),
#     not as bare digit runs;
#   * each token is resolved to the SET of numeric values it can plausibly denote. A
#     token is UNAMBIGUOUS when its own shape settles it (two different separators ->
#     the last one is the decimal; a repeated separator -> it groups; a non-3-digit tail
#     -> it decimalises; a leading zero -> it decimalises). It is AMBIGUOUS only in the
#     one genuinely undecidable shape — a single separator followed by EXACTLY three
#     digits ("25.000", "1,000") — and even that is settled when the side's locale
#     convention is known;
#   * matching is by VALUE, not by string, so a correct locale re-rendering matches and
#     is NOT flagged;
#   * a token pair that matched only because one side was ambiguous yields a MINOR
#     "ambiguous rendering" flag (this is what preserves the pre-fix recall on the
#     LQA/editor path, where no target locale is available);
#   * a source number with NO value-compatible target number, alongside a target number
#     with no source counterpart, is a VALUE CHANGE -> MAJOR.
#
# CONSERVATISM (masking): dates, times, version strings, URLs, emails, placeholders
# ({0}/{name}/%s/$VAR), markup and HTML entities are BLANKED before extraction, so a
# reformatted date ("March 3, 2026" -> "3 de marzo de 2026") or a version string never
# reaches the comparator. This mirrors the masking `glossary.mask_protected_spans`
# gained for the same campaign; it is re-implemented locally (a handful of patterns)
# rather than imported, because `glossary` pulls in models/providers and this module is
# imported by `lqa`, the editor and the write seam.

# Separator characters that can only ever GROUP digits (never a decimal point):
# ASCII space, NBSP, narrow NBSP, thin space, figure space, and the Swiss apostrophes.
# (fr renders "1 234,56" with a PLAIN space in real output — see the QA campaign's
# thousands-separator finding — so the plain space must be accepted here.)

# One number token: either a GROUPED rendering (1-3 digits, then one or more
# separator+exactly-3-digit groups, then an optional decimal tail) or a plain run with
# an optional single decimal separator. The lookbehind/lookahead keep a token from
# starting or ending in the middle of a longer numeric run.
_NUM_TOKEN_RE = re.compile(
    r"(?<![\d.,])(?:"
    r"\d{1,3}(?:[.,      '’]\d{3})+(?:[.,]\d+)?"
    r"|\d+(?:[.,]\d+)?"
    r")(?!\d)"
)

# Spans blanked before number extraction — numbers inside them are NOT prose numbers.
_TIME_RE = re.compile(
    r"(?<!\d)(\d{1,2})(?::|\s*h\s*)(\d{2})(?::(\d{2}))?"
    # The meridiem must end at a word boundary: "12:15 a mi casa" is not 12:15 AM.
    # Group 4 captures the whole meridiem so callers can tell bare "am" from "a.m.".
    r"(?!\d)(?:\s*(([ap])\.?\s*m)(?![^\W\d_])\.?)?",
    re.I,
)
# The non-prose masking alternation, in priority order. It is NOT one regex any more
# (#588): the email alternative and the markup-tag alternative were quadratic (cubic for
# an `@@@@` run) on a long hostile token, and a segment may be 100,000 characters. They
# are matched by the linear scanners below and merged with the regex alternatives by
# leftmost start (ties by the original alternation order), which yields exactly the
# matches the single alternation produced.
_MASK_URL_PATTERN = "|".join(
    (
        r"https?://\S+",  # URLs
        r"www\.[^\s<>]+",
        # Scheme-less URL carriers the scheme/www rows above miss: a protocol-relative
        # URL (//host/path), a bare host with an optional path (help.test/2026/02), and
        # a root-relative path (/p/73036). Each is gated by a non-word lookbehind so a
        # slash never splits a plain number, ratio or date (3/4, 12/31/2026 keep their
        # digits; only a URL/path that STARTS after a space or punctuation is blanked). Every
        # non-TLD host label must START with a letter, so list numbering ("1.Intro",
        # "5.ok") and a unit-suffixed number before a missing space ("10kg.The") are
        # never read as a host and a changed number there is still caught.
        r"(?<![\w:./-])//[^\s<>]+",  # protocol-relative URL
        r"(?<![\w./-])(?:[A-Za-z][A-Za-z0-9-]{0,62}\.)+[A-Za-z]{2,}(?::\d{2,5})?(?:/\S*)?",  # host + path  # noqa: E501
        r"(?<![\w./-])(?:/[A-Za-z0-9._~%+-]+){2,}",  # root-relative path
    )
)
_MASK_REST_PATTERN = "|".join(
    (
        r"⟦/?[a-z]?\d+⟧",  # inline-fidelity tokens
        r"\$\{[^{}]*\}",  # ${VAR}
        r"\{[^{}]*\}",  # {0}, {name}, ICU
        r"%(?:\d+\$)?[0-9.\-]*[a-zA-Z]",  # printf %s %d %1$s %.2f
        r"\$[A-Za-z_][A-Za-z0-9_]*",  # $VAR
        r"&#?[A-Za-z0-9]{1,8};",  # &nbsp; &#8212;
        r"(?<!\d)\d{4}[-/]\d{1,2}[-/]\d{1,2}(?!\d)",  # ISO date
        r"(?<!\d)\d{1,2}[/.\-]\d{1,2}[/.\-]\d{2,4}(?!\d)",  # 03/07/2026, 3.7.2026
        r"(?<!\d)\d{1,2}[:h]\d{2}(?::\d{2})?(?!\d)",  # 18:30, 18h30, 18:30:05
        r"(?<![\w.])[vV]\.?\s?\d+(?:\.\d+)+(?!\d)",  # v1.2.3, V 2.0
    )
)
_MASK_URL_RE = re.compile(_MASK_URL_PATTERN, re.UNICODE)
_MASK_REST_RE = re.compile(_MASK_REST_PATTERN, re.UNICODE)
# An email's local and domain parts are runs of this class.
_EMAIL_RUN_RE = re.compile(r"[^\s<>()\[\]]+")
_ASCII_LETTERS_RE = re.compile(r"[A-Za-z]+")
_ASCII_LETTERS = frozenset("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ")


def _next_email(text: str, pos: int):
    """(start, end) of the leftmost email at or after `pos`, or None, in linear time.

    Equivalent to the old email alternative (a run of non-space, non-bracket characters,
    an at sign, another such run, a dot, two or more ASCII letters). Inside one maximal
    run of that class, a match from the run start `a` exists iff some "@" lies after `a`
    and at least two characters before a "." that has two ASCII letters right after it;
    the greedy match then ends after the letters that follow the LAST such ".". When no
    match starts at `a`, none starts later in the run either (the set of usable "@" only
    shrinks), so each run is scanned once."""
    while True:
        run = _EMAIL_RUN_RE.search(text, pos)
        if run is None:
            return None
        a, b = run.span()
        at = text.find("@", a + 1, b)
        if at != -1:
            hi = b - 2
            while True:
                dot = text.rfind(".", at + 2, hi)
                if dot == -1:
                    break
                if text[dot + 1] in _ASCII_LETTERS and text[dot + 2] in _ASCII_LETTERS:
                    return a, _ASCII_LETTERS_RE.match(text, dot + 1).end()
                hi = dot
        pos = b


def _next_tag(text: str, pos: int):
    """(start, end) of the leftmost `</?[a-zA-Z][^>]*>` at or after `pos`, or None, in
    linear time: the match ends at the first ">" after the tag-name letter, so when no ">"
    follows there, no later "<" can match either."""
    lt = text.find("<", pos)
    while lt != -1:
        i = lt + 1
        if text[i : i + 1] == "/":
            i += 1
        if text[i : i + 1] in _ASCII_LETTERS:
            gt = text.find(">", i + 1)
            return None if gt == -1 else (lt, gt + 1)
        lt = text.find("<", lt + 1)
    return None


class _MaskSpan:
    """The slice of re.Match the masking callers use: m[0], group(), start(), end(), span()."""

    __slots__ = ("_e", "_s", "_text")

    def __init__(self, text: str, start: int, end: int) -> None:
        self._text, self._s, self._e = text, start, end

    def group(self, _n: int = 0) -> str:
        return self._text[self._s : self._e]

    __getitem__ = group

    def start(self) -> int:
        return self._s

    def end(self) -> int:
        return self._e

    def span(self) -> tuple[int, int]:
        return self._s, self._e


def _regex_finder(rx):
    def find(text: str, pos: int):
        m = rx.search(text, pos)
        return None if m is None else m.span()

    return find


class _LinearMask:
    """Drop-in for the old `_MASK_RE`: `.sub()` and `.finditer()` with linear-time scanning.

    Sources are tried in the old alternation's priority order (URL, www, the scheme-less
    URL and path carriers, email, the token
    and number carriers, markup tag); the leftmost start wins and a tie goes to the earlier
    source, exactly as one regex alternation resolves it. A found match stays valid until
    the scan passes its start, so no source is rescanned from the same text twice."""

    _FINDERS = (
        _regex_finder(_MASK_URL_RE),
        _next_email,
        _regex_finder(_MASK_REST_RE),
        _next_tag,
    )

    def finditer(self, text: str):
        text = text or ""
        pos, n = 0, len(text)
        unset = object()
        cache: list = [unset] * len(self._FINDERS)
        while pos <= n:
            best = None
            for k, find in enumerate(self._FINDERS):
                hit = cache[k]
                if hit is unset or (hit is not None and hit[0] < pos):
                    hit = cache[k] = find(text, pos)
                if hit is not None and (best is None or hit[0] < best[0]):
                    best = hit
            if best is None:
                return
            yield _MaskSpan(text, best[0], best[1])
            pos = best[1]

    def sub(self, repl, text: str) -> str:
        text = text or ""
        out: list[str] = []
        last = 0
        for m in self.finditer(text):
            out.append(text[last : m.start()])
            out.append(repl(m) if callable(repl) else repl)
            last = m.end()
        out.append(text[last:])
        return "".join(out)


_MASK_RE = _LinearMask()

# A run of digits carrying TWO OR MORE `.`/`,` separators. Either it is a legitimate
# grouped number ("1.234.567", "1.234.567,89") or it is a dotted VERSION / build id
# ("10.0.26200", "1.2.3"). `_mask_multi_sep_runs` keeps the former and blanks the latter.
_MULTI_SEP_RE = re.compile(r"(?<![\d.,])\d+(?:[.,]\d+){2,}(?!\d)")


def _is_grouped_number(tok: str) -> bool:
    """True when `tok` is a valid thousands-grouped rendering: a 1-3 digit head, every
    intermediate group EXACTLY three digits, and at most one trailing DIFFERENT
    separator introducing a decimal tail. False for "10.0.26200" / "1.2.3" (versions)."""
    seps = [c for c in tok if c in ".,"]
    parts = re.split(r"[^\d]", tok)
    if len(parts) < 3:
        return False
    if len(set(seps)) == 1:
        groups, tail_ok = parts[1:], True
    elif len(set(seps[:-1])) == 1 and seps[-1] != seps[0]:
        groups, tail_ok = parts[1:-1], bool(parts[-1])
    else:
        return False
    return tail_ok and 1 <= len(parts[0]) <= 3 and bool(groups) and all(len(g) == 3 for g in groups)


def _mask_multi_sep_runs(text: str) -> str:
    return _MULTI_SEP_RE.sub(
        lambda m: m.group(0) if _is_grouped_number(m.group(0)) else " " * len(m.group(0)), text
    )


def mask_non_prose(text: str | None) -> str:
    """`text` with every non-prose numeric carrier blanked to same-length spaces:
    URLs, emails, placeholders, markup, entities, dates, times and version strings.
    Blanking (not deleting) preserves offsets and word boundaries, so a masked span can
    never fuse its neighbours into a spurious number."""
    return _mask_multi_sep_runs(_MASK_RE.sub(lambda m: " " * len(m.group(0)), text or ""))


# Decimal-separator convention by base language. `point` = 1,234.56; `comma` = 1.234,56.
# A language absent from BOTH sets makes the parser ABSTAIN (every ambiguous token keeps
# both readings) — the conservative direction, since a wrong guess manufactures a MAJOR.
_DECIMAL_POINT_LANGS = frozenset(
    {
        "en",
        "ja",
        "zh",
        "ko",
        "th",
        "he",
        "iw",
        "ms",
        "hi",
        "bn",
        "ta",
        "te",
        "ur",
        "si",
        "km",
        "my",
        "sw",
        "fil",
        "tl",
        "ga",
        "cy",
        "mt",
        "zu",
        "xh",
    }
)
_DECIMAL_COMMA_LANGS = frozenset(
    {
        "es",
        "fr",
        "de",
        "it",
        "pt",
        "nl",
        "ru",
        "uk",
        "be",
        "pl",
        "cs",
        "sk",
        "sl",
        "hr",
        "sr",
        "bs",
        "mk",
        "sq",
        "bg",
        "ro",
        "hu",
        "lv",
        "lt",
        "et",
        "fi",
        "sv",
        "da",
        "nb",
        "nn",
        "no",
        "is",
        "tr",
        "el",
        "ca",
        "gl",
        "eu",
        "vi",
        "id",
        "af",
        "az",
        "kk",
        "hy",
        "ka",
    }
)
# Regions whose language is comma-decimal in Europe but POINT-decimal locally — the
# Latin-American Spanish split (MX/US/CA use 1,234.56) and the SWISS split (de-CH /
# fr-CH / it-CH render "1'234.56": apostrophe groups, POINT decimal, unlike de-DE /
# fr-FR / it-IT). Adversarial review: without the Swiss rows a correct de-CH target
# "25.000" (= 25, three decimals) was read as twenty-five THOUSAND and manufactured a
# MAJOR value-change flag on delivered content — the exact false-positive class this
# gate exists to avoid.
_POINT_REGIONS = frozenset(
    {
        "es-mx",
        "es-us",
        "es-pa",
        "es-pr",
        "es-do",
        "es-ni",
        "es-gt",
        "es-hn",
        "es-sv",
        "de-ch",
        "de-li",
        "fr-ch",
        "it-ch",
    }
)
# Regions where usage is genuinely mixed / contested: ABSTAIN rather than guess.
# `es-419` (generic Latin American Spanish, a very common CLDR tag) belongs here and not
# in the comma default: the region it names spans BOTH conventions (AR/CL/CO comma,
# MX/PE point), so any single verdict is wrong for half of it — and guessing "comma"
# manufactured a MAJOR on a correct point-decimal rendering (adversarial review).
_UNCERTAIN_REGIONS = frozenset({"es-pe", "es-ec", "es-cr", "es-cu", "es-bo", "es-419"})


def decimal_style(locale: str | None) -> str | None:
    """`"point"`, `"comma"`, or None (abstain) for a BCP-47-ish locale tag."""
    if not locale:
        return None
    tag = locale.replace("_", "-").strip().lower()
    base = tag.split("-", 1)[0]
    if base in _DECIMAL_POINT_LANGS:
        return "point"
    if base in _DECIMAL_COMMA_LANGS:
        if tag in _POINT_REGIONS:
            return "point"
        if tag in _UNCERTAIN_REGIONS:
            return None
        return "comma"
    return None


def _number_values(tok: str, style: str | None) -> tuple[frozenset, bool]:
    """(the numeric values `tok` can denote, whether that set required AMBIGUITY).

    Unambiguous by shape: no separator; a space-class group separator; two DIFFERENT
    `.`/`,` separators (the LAST is the decimal); a REPEATED separator (it groups); a
    decimal tail whose length is not 3; a head longer than 3 digits or starting with 0.
    The single genuinely undecidable shape — one separator + exactly three digits — is
    resolved by `style` when known, and otherwise keeps BOTH readings."""
    parts = re.split(r"[^\d]", tok)
    seps = [c for c in tok if not c.isdigit()]
    if not seps:
        return frozenset({float(tok)}), False
    dec_seps = [c for c in seps if c in ".,"]
    if len(dec_seps) < len(seps):  # a space-class separator is present
        if not dec_seps:
            return frozenset({float("".join(parts))}), False
        if len(dec_seps) == 1 and seps[-1] in ".,":
            return frozenset({float("".join(parts[:-1]) + "." + parts[-1])}), False
        return frozenset(), False  # unparseable mixture -> ignore token
    if len(set(dec_seps)) == 2:  # both `.` and `,` -> last is decimal
        return frozenset({float("".join(parts[:-1]) + "." + parts[-1])}), False
    if len(dec_seps) >= 2:  # repeated separator -> it groups
        return frozenset({float("".join(parts))}), False
    sep, head, tail = dec_seps[0], parts[0], parts[1]
    dec_val, grp_val = float(head + "." + tail), float(head + tail)
    if len(tail) != 3 or len(head) > 3 or head[0] == "0":
        return frozenset({dec_val}), False
    if style == "point":
        return frozenset({dec_val if sep == "." else grp_val}), False
    if style == "comma":
        return frozenset({grp_val if sep == "." else dec_val}), False
    return frozenset({dec_val, grp_val}), True


def _shape(tok: str) -> tuple:
    """A token's rendering SHAPE: its digit string plus its separator POSITIONS, with the
    separator CHARACTERS discarded. Two tokens with the same shape differ only in which
    separator glyph each locale uses ("1,234.56" vs "1.234,56" vs "1 234,56"), which is
    exactly a correct locale re-rendering — never a value change."""
    return (
        "".join(c for c in tok if c.isdigit()),
        tuple(i for i, c in enumerate(tok) if not c.isdigit()),
    )


def _is_regrouping(lost: list[str], gained: list[str]) -> bool:
    """True when the unmatched tokens on the two sides are the SAME DIGITS carved into a
    DIFFERENT NUMBER of tokens — the unambiguous signature of one side grouping (or
    splitting) digit runs the other side did not, never of a changed value.

    Found by adversarial review as the gate's one reachable MAJOR-false-positive class on
    correct delivered content. A plain space is a legitimate thousands separator (fr
    renders "1 234,56" that way, which is why the tokenizer must accept it), so ANY run
    of space-separated 3-digit groups fuses into one token — and a target that re-punctuates
    it splits it back apart:
        "Sizes: 128 256 512 GB" -> "Tamanos: 128, 256, 512 GB"   (1 token vs 3)
        "Call +1 555 123 4567"  -> "Llame al +1 (555) 123-4567"  (2 tokens vs 4)
    Both are correct translations and both were judged MAJOR "number value changed" ->
    NEEDS_REVIEW + TM-ineligible. They are demoted to the minor formatting class here.

    DELIBERATELY NARROW — the token COUNTS must differ. An equal-count pair whose digits
    coincide ("1,000" -> "10,00", a real separator-position corruption) keeps its MAJOR
    verdict, because nothing about it looks like re-grouping."""
    if len(lost) == len(gained):
        return False

    def digits(toks: list[str]) -> str:
        return "".join(c for t in toks for c in t if c.isdigit())

    return digits(lost) == digits(gained)


def _number_tokens(text: str | None) -> list[str]:
    """The prose number tokens of `text`, non-prose carriers masked out and tokens whose
    shape cannot be resolved to any value dropped."""
    masked = mask_non_prose(text)
    return [t for t in _NUM_TOKEN_RE.findall(masked) if _number_values(t, None)[0]]


def number_findings(
    source_text: str | None,
    target_text: str | None,
    target_locale: str | None = None,
    source_locale: str | None = None,
    *,
    check_times: bool = False,
) -> list[dict]:
    """Value-aware number-integrity findings for one (source, target) pair.

    Returns at most ONE flag, shaped like every other `check_segment` flag
    ({type,severity,message}) plus a `kind` discriminator:
      * kind "value"  / severity MAJOR — a source number has no value-compatible target
        number AND the target carries an unmatched number of its own: the value changed
        ("25 items" -> "25.000 articulos" for a comma-decimal target). This is the class
        the write seam gates on (quality.number_violation).
      * kind "count"  / severity minor — numbers were dropped or invented, but nothing
        contradicts a surviving value.
      * kind "format" / severity minor — the RENDERING differs but no value is
        contradicted: either a match depended on an AMBIGUOUS reading (one separator +
        exactly three digits on a side whose locale convention is unknown — the honest
        verdict on the LQA/editor path, which has no target locale, and what preserves
        the pre-fix recall there), or the two sides carved the SAME DIGITS into a
        different number of tokens (`_is_regrouping`).
    Empty (no flag) when every number matched unambiguously — so a correct locale
    re-rendering ("1,234.56" -> "1.234,56") is silent, which the old string compare was
    not.

    `target_locale` / `source_locale` are OPTIONAL; both default to abstain. Callers that
    know the locale (the write seam) get the decisive verdict, callers that do not (the
    editor grid, `lqa._structural_errors`) get the conservative one.
    `check_times` opts Jev into time parity, normalizing 12/24-hour and colon/h forms;
    other callers keep the existing time abstention."""
    if check_times:
        from collections import Counter

        def times(text, locale):
            lang = (locale or "").replace("_", "-").split("-", 1)[0].casefold()
            text = _MASK_RE.sub(
                lambda m: m[0] if _TIME_RE.match(m[0]) else " " * len(m[0]), text or ""
            )

            def hour_value(h, meridiem, ap):
                # A meridiem qualifies a 12-hour clock time only, so ignore it
                # when the hour is 0 or 13+; and a bare lowercase "am" after a
                # German/Dutch time is "on the", not AM.
                if not meridiem or not 1 <= int(h) <= 12:
                    return int(h)
                if lang in ("de", "nl") and meridiem == "am":
                    return int(h)
                return int(h) % 12 + (12 if ap.lower() == "p" else 0)

            return Counter(
                (hour_value(h, meridiem, ap), int(m), int(s or 0))
                for h, m, s, meridiem, ap in _TIME_RE.findall(text)
            )

        before, after = times(source_text, source_locale), times(target_text, target_locale)
        if before and after and before != after:
            return [
                {
                    "type": "numbers",
                    "severity": "major",
                    "kind": "value",
                    "message": f"Time changed: missing {list((before - after).elements())!r}; "
                    f"added {list((after - before).elements())!r}",
                }
            ]
        # Remove recognized times before number parity as well (17 h 30 is
        # one time, not two extra prose numbers). Unrecognized scripts abstain
        # from time parity; ordinary number parity still runs.
        source_text = _TIME_RE.sub(" ", source_text or "")
        target_text = _TIME_RE.sub(" ", target_text or "")
        # Japanese time carriers are recognizable but not semantically parsed.
        # Do not reinterpret their hour/minute digits as added prose quantities.
        time_carrier = r"(?:午[前後])?\d{1,2}時(?:\d{1,2}分|半)?(?:\d{1,2}秒)?"
        source_text = re.sub(time_carrier, " ", source_text)
        target_text = re.sub(time_carrier, " ", target_text)
    src = [
        (t, *_number_values(t, decimal_style(source_locale))) for t in _number_tokens(source_text)
    ]
    tgt = [
        (t, *_number_values(t, decimal_style(target_locale))) for t in _number_tokens(target_text)
    ]
    if not src and not tgt:
        return []
    used_s, used_t = [False] * len(src), [False] * len(tgt)
    ambiguous: list[tuple[str, str]] = []
    # Pass 1 — identical rendering SHAPE (a pure separator-glyph swap). Run first so an
    # exact counterpart is never stolen by a looser value match elsewhere in the segment.
    for i, (st, _sv, _sa) in enumerate(src):
        for j, (tt, _tv, _ta) in enumerate(tgt):
            if not used_t[j] and _shape(st) == _shape(tt):
                used_s[i] = used_t[j] = True
                break
    # Pass 2 — VALUE compatibility (the value sets intersect).
    for i, (st, sv, sa) in enumerate(src):
        if used_s[i]:
            continue
        for j, (tt, tv, ta) in enumerate(tgt):
            if used_t[j] or not (sv & tv):
                continue
            used_s[i] = used_t[j] = True
            if sa or ta:
                ambiguous.append((st, tt))
            break
    lost = [src[i][0] for i in range(len(src)) if not used_s[i]]
    gained = [tgt[j][0] for j in range(len(tgt)) if not used_t[j]]
    if lost and gained and _is_regrouping(lost, gained):
        return [
            {
                "type": "numbers",
                "severity": "minor",
                "kind": "format",
                "message": "Number grouping differs (the same digits are grouped into a "
                "different number of tokens: " + ", ".join(lost) + " vs " + ", ".join(gained) + ")",
            }
        ]
    if lost and gained:
        pairs = "; ".join(f"{a} -> {b}" for a, b in zip(lost, gained, strict=False))
        extra = [t for t in (lost[len(gained) :] + gained[len(lost) :])]  # noqa: C416
        msg = "Number value changed (" + pairs + ")"
        if extra:
            msg += " and unmatched " + ", ".join(extra)
        return [{"type": "numbers", "severity": "major", "kind": "value", "message": msg}]
    if lost or gained:
        parts = []
        if lost:
            parts.append("missing " + ", ".join(lost))
        if gained:
            parts.append("added " + ", ".join(gained))
        return [
            {
                "type": "numbers",
                "severity": "minor",
                "kind": "count",
                "message": "Number count mismatch (" + "; ".join(parts) + ")",
            }
        ]
    if ambiguous:
        pairs = "; ".join(f"{a} vs {b}" for a, b in ambiguous)
        return [
            {
                "type": "numbers",
                "severity": "minor",
                "kind": "format",
                "message": "Number rendering differs and is ambiguous without a "
                "locale convention (" + pairs + ")",
            }
        ]
    return []


def _tags_only_source(src: str) -> bool:
    from app.services.tags_only import is_tags_only  # leaf module, no cycle

    return is_tags_only(src)


def check_segment(
    source_text: str, target_text: str, target_locale: str | None = None
) -> list[dict]:
    """The cheap deterministic per-segment checks (empty / untranslated / tags /
    inline-fidelity placeholders / numbers).

    `target_locale` is OPTIONAL and additive — every pre-existing caller passes two
    positional arguments and is byte-identical. When supplied it lets the number check
    resolve the target's decimal convention and return the DECISIVE verdict instead of
    the ambiguous one (see `number_findings`)."""
    flags: list[dict] = []
    src = source_text or ""
    tgt = target_text or ""

    if not tgt.strip():
        return [{"type": "empty", "severity": "minor", "message": "No translation yet"}]

    # #705 (gate): a source with no translatable text (only inline tags / one placeholder)
    # is DELIVERED as its own copy on every path, so identical-to-source
    # is the correct target there, not a defect: no "untranslated" flag (it maps to an MQM
    # Accuracy major in LQA and a lens may opt in to block on it).
    if tgt.strip() == src.strip() and not _tags_only_source(src):
        flags.append(
            {"type": "untranslated", "severity": "major", "message": "Target identical to source"}
        )

    # Real inline tags ({0}, %s, <b>, URLs) — inline-fidelity placeholder tokens
    # are stripped here and checked separately below (avoids double-flagging).
    src_tags = sorted(extract_inline_tags(strip_placeholders(src)))
    tgt_tags = sorted(extract_inline_tags(strip_placeholders(tgt)))
    missing: list = []
    extra: list = []
    if src_tags != tgt_tags:
        from collections import Counter

        # Count-aware: a dropped or added DUPLICATE ("%s and %s" -> "%s") is a divergence too.
        src_ct, tgt_ct = Counter(src_tags), Counter(tgt_tags)
        missing = sorted((src_ct - tgt_ct).elements())
        extra = sorted((tgt_ct - src_ct).elements())
        if missing and extra and target_locale:
            # A link whose only change is its language part (/en/help -> /fr/help) is the
            # localized address of the same page, not a changed link.
            missing, extra = link_locale.pair_off(
                missing,
                extra,
                lambda a, b: link_locale.tag_text_locale_only(a, b, target_locale),
            )
    if missing or extra:
        msg = []
        if missing:
            msg.append("missing " + ", ".join(missing))
        if extra:
            msg.append("extra " + ", ".join(extra))
        flags.append(
            {"type": "tags", "severity": "major", "message": "Tag/placeholder " + "; ".join(msg)}
        )
    elif "<" in tgt and "<" not in src:
        flags.append(
            {
                "type": "tags",
                "severity": "major",
                "message": "Target contains a literal <. Use &lt; for HTML text.",
            }
        )

    # Inline-fidelity placeholder integrity: the DOCX/XLIFF tokens (⟦r1⟧, ⟦g1⟧…)
    # must survive translation with the same multiset AND order, or build() will
    # reflow formatting wrong. Flags dropped, added, or reordered tokens.
    src_ph, tgt_ph = find_placeholders(src), find_placeholders(tgt)
    # #693: free-order point tokens (`⟦pN⟧`) may move; see segment.same_token_order.
    if not same_token_order(src_ph, tgt_ph):
        from collections import Counter

        src_ct, tgt_ct = Counter(src_ph), Counter(tgt_ph)
        # Count-aware diff. Split target surplus into "added" (token absent from
        # source) vs "duplicated" (token in source but emitted extra times) so a
        # duplicated token yields a specific, non-empty message.
        dropped = sorted((src_ct - tgt_ct).elements())
        surplus = tgt_ct - src_ct  # tokens the target has more of than the source
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
            # same multiset, different order
            parts.append("reordered " + ", ".join(sorted(set(order_significant(src_ph)))))
        flags.append(
            {
                "type": "placeholder",
                "severity": "major",
                "message": "Inline placeholder " + "; ".join(parts),
            }
        )
    elif not placeholders_well_formed(tgt):
        # AI Tagging 1.8 — nesting validity: balanced open/close, no crossed pairs.
        # DEFENSE-IN-DEPTH, not an independent catch: a target that CROSSES a source's
        # pairs (⟦g1⟧⟦g2⟧⟦/g1⟧⟦/g2⟧ vs ⟦g1⟧⟦g2⟧⟦/g2⟧⟦/g1⟧) reorders the token SEQUENCE,
        # so it already trips the multiset+order compare above (the `if`). This `elif`
        # therefore fires only in the residual case the order compare cannot see: the
        # target carries the SAME token sequence as the source yet is itself malformed
        # (a source with no tokens + a hand-typed unbalanced pair, or a genuinely
        # malformed source echoed verbatim). Same "placeholder" type so 1.3 maps it to
        # Design/Markup/major and the fix loop / export hard-block treat it as a drop.
        flags.append(
            {
                "type": "placeholder",
                "severity": "major",
                "message": "Inline placeholder malformed nesting (crossed or unbalanced pairs)",
            }
        )

    # BUG-06: VALUE-aware, locale-aware number integrity (see the module section above).
    # The flag TYPE stays "numbers" so `lqa._QA_TO_MQM` keeps mapping it to
    # Accuracy/Number with no change to lqa.py; the per-flag `severity`/`kind` carry the
    # new value-vs-formatting distinction for the editor and the write seam.
    flags.extend(number_findings(src, tgt, target_locale))

    # QA-1 (text mechanics): whitespace and terminal-punctuation hygiene. Locale-
    # independent whitespace checks always run; the terminal-punctuation check is
    # SCRIPT-guarded and abstains wherever the rule is not defensible (see below).
    flags.extend(whitespace_findings(src, tgt))
    flags.extend(punctuation_findings(src, tgt, target_locale))

    return flags


# =============================================================================
# WHITESPACE & TERMINAL-PUNCTUATION HYGIENE (QA-1)
# =============================================================================
# Two cheap deterministic text-mechanics checks, added beside the number check and in
# the SAME conservative spirit: a linguist-credibility check must not cry wolf, or
# reviewers learn to ignore the badge. Both are LOCALE-INDEPENDENT for detection except
# the terminal-punctuation one, which is guarded by the TARGET's own script so it never
# fires where the rule cannot be defended.
#
# UNLIKE the other check_segment findings, these two are EXCLUDED from LQA scoring
# (lqa._structural_errors / _LQA_STRUCTURAL_SKIP, reviewer M4): they are editor badges +
# (whitespace only) a soft write-seam gate, never a contributor to the MQM score, so they
# cannot flip a card at the pass bar and fire a correction loop.
#
# The two whitespace findings are ALSO gated at the write seam (quality.whitespace_-
# violation) behind the real-MT `lang_guard`, NOT always-on: the offline MockProvider
# echoes the source under a "[locale] " prefix, and a source with a LEADING space would
# make that echo carry an interior double space ("[es]  Hello") that the source lacks —
# a systemic false positive on a demo/offline install. Gating the SEAM behind lang_guard
# (the same signal the language-mismatch guard uses, for the same reason) keeps the mock
# clean while real provider output is checked. The EDITOR-live path here is display-only,
# so it runs unconditionally.

# A run of two or more ASCII spaces. Only the plain space is considered: tabs / NBSP /
# ideographic space are legitimately load-bearing in enough contexts that flagging them
# would be guesswork, and the runaway-space artefact real MT emits is plain spaces.
_DOUBLE_SPACE_RE = re.compile(r"  +")


def whitespace_findings(source_text: str | None, target_text: str | None) -> list[dict]:
    """Whitespace-hygiene findings for one (source, target) pair — type "whitespace",
    always MINOR (a spacing slip is cosmetic, never a value error). At most two:

      * kind "double_space" — the target carries an interior run of 2+ spaces that the
        SOURCE does not. Source-suppressed: a source that itself uses double spaces
        (monospace alignment, ASCII art, code) licenses the same in its target and is
        NOT flagged — the direction that avoids a false positive on intentional spacing.
      * kind "leading_trailing" — the target has leading and/or trailing whitespace that
        the source lacks (each side judged independently). MT routinely adds a stray edge
        space that breaks string concatenation in key projects; the reverse (source has
        it, target dropped it) is deliberately NOT flagged here (the task's item (b) is
        target-ADDED edge whitespace) to keep the check one-directional and quiet.

    Byte-quiet when the target's whitespace matches or is explained by the source, so a
    faithful translation is silent."""
    src = source_text or ""
    tgt = target_text or ""
    if not tgt.strip():
        return []
    out: list[dict] = []
    if _DOUBLE_SPACE_RE.search(tgt) and not _DOUBLE_SPACE_RE.search(src):
        out.append(
            {
                "type": "whitespace",
                "severity": "minor",
                "kind": "double_space",
                "message": "Double space in target (the source has none)",
            }
        )
    lead = tgt[:1].isspace() and not src[:1].isspace()
    trail = tgt[-1:].isspace() and not src[-1:].isspace()
    if lead or trail:
        where = " and ".join([w for w, on in (("leading", lead), ("trailing", trail)) if on])
        out.append(
            {
                "type": "whitespace",
                "severity": "minor",
                "kind": "leading_trailing",
                "message": f"Target has {where} whitespace the source does not",
            }
        )
    return out


# Strong sentence-ending marks, across the scripts where the rule is defensible: Latin/
# Cyrillic/Greek `.!?`, the ellipsis, the CJK full-stop/bang/question, the Arabic
# question mark, and the Devanagari danda. Colon and semicolon are DELIBERATELY excluded
# — they end sentences too rarely and appear mid-string in UI labels ("Steps:", "Note;")
# often enough that a terminal-position rule on them would be guesswork.
_TERMINAL_ENDERS = frozenset(".!?…。！？؟।")
# Scripts whose orthography does NOT use the marks above at sentence end. When the TARGET
# contains any letter from one of these, the terminal-punctuation rule cannot be defended
# for it, so the check ABSTAINS. CJK is intentionally ABSENT: it uses 。！？, which ARE in
# the ender set, so a CJK target is judged normally. Ranges: Thai, Lao, Khmer, Myanmar,
# Tibetan (blocks that write without a Latin-style terminal mark).
_NO_TERMINAL_SCRIPT_RE = re.compile(r"[฀-๿຀-໿ក-៿က-႟ༀ-࿿]")
# Base languages whose script does not use Latin-style terminal marks — the locale-tag
# twin of `_NO_TERMINAL_SCRIPT_RE` (used only when a caller passes `target_locale`).
_NO_TERMINAL_PUNCT_LANGS = frozenset({"th", "lo", "km", "my", "bo", "dz"})


def _last_significant(text: str) -> str:
    """The last non-whitespace character of `text` ("" when blank). Trailing closing
    quotes/brackets are NOT peeled — a source ending `Go!"` reads as ending in `"`, so
    the check simply ABSTAINS on it (a miss, never a false positive)."""
    t = (text or "").rstrip()
    return t[-1:] if t else ""


# Closing quotes / brackets across scripts. When EITHER side's last significant character
# is one of these, the real sentence-ender is hidden behind it, so the check ABSTAINS
# (reviewer M3): `He said "Go!"` ends in `"`, and a source `"hello."` (period inside the
# quote) vs a target `"hola".` (period outside) is a legitimate quotation-style difference,
# never a dropped ender.
_QUOTE_BRACKET = frozenset("\"'”’‘“»«)]}›‹」』】）］｝")


def punctuation_findings(
    source_text: str | None, target_text: str | None, target_locale: str | None = None
) -> list[dict]:
    """Terminal-punctuation parity for one (source, target) pair — type "punctuation",
    kind "terminal", MINOR. ONE-DIRECTIONAL (reviewer M3): fires ONLY when the source's
    last significant character is a strong sentence-ender and the target's is NOT — a
    DROPPED ender. A target that ADDS terminal punctuation the source lacked ('Ready' ->
    'Listo.', '(optional)' -> '(opcional).') is legitimate and NEVER flags.

    DELIBERATELY EDITOR-ONLY (no write-seam gate, and excluded from LQA scoring — see
    lqa._structural_errors): terminal-punctuation convention is the most locale-variable of
    the QA-1 checks, so gating or scoring on it would misfire enough to erode trust. Here it
    is an informational badge.

    Tests only ENDER-CLASS MEMBERSHIP (does the last significant char belong to the strong-
    ender set), NOT which specific mark: a `?` -> `.` or `!` -> `.` swap is DELIBERATELY out
    of scope (both are strong enders, so it never flags) -- picking the "right" terminal mark
    is a locale/style judgement, not a mechanical drop this check can defend.

    FOUR conservatism locks: (1) only STRONG enders count (no colon/semicolon); (2) ABSTAIN
    when the target is written in a script that does not use these marks
    (`_NO_TERMINAL_SCRIPT_RE`), or when `target_locale` names such a script; (3) ABSTAIN
    when EITHER side's last significant character is a closing quote/bracket
    (`_QUOTE_BRACKET`); (4) one-directional (drop only). `target_locale` is optional; when
    the editor threads it, the locale script-guard runs alongside the text one."""
    src = source_text or ""
    tgt = target_text or ""
    if not tgt.strip():
        return []
    if _NO_TERMINAL_SCRIPT_RE.search(tgt):
        return []
    if target_locale:
        base = target_locale.replace("_", "-").strip().lower().split("-", 1)[0]
        if base in _NO_TERMINAL_PUNCT_LANGS:
            return []
    src_end, tgt_end = _last_significant(src), _last_significant(tgt)
    if not src_end or not tgt_end:
        return []
    if src_end in _QUOTE_BRACKET or tgt_end in _QUOTE_BRACKET:
        return []
    if src_end in _TERMINAL_ENDERS and tgt_end not in _TERMINAL_ENDERS:
        return [
            {
                "type": "punctuation",
                "severity": "minor",
                "kind": "terminal",
                "message": f"Source ends with terminal punctuation ({src_end}); the target does not",  # noqa: E501
            }
        ]
    return []


# =============================================================================
# CONSISTENCY-LITE (QA-1, document scope)
# =============================================================================
def _consistency_norm(text: str | None) -> str:
    """Normalized form for consistency grouping/comparison: whitespace collapsed and
    case-folded, so trivial spacing/casing differences neither split a source group nor
    manufacture a target disagreement."""
    return " ".join((text or "").split()).casefold()


def inconsistent_segment_ids(rows) -> set:
    """Given `rows` = an iterable of `(seg_id, source_text, target_text)`, return the set
    of `seg_id`s that are INCONSISTENT within this document: they share a normalized
    source with at least one other TRANSLATED row but carry a DIFFERENT normalized target.

    Informational only (no gating, no status change). One pass over already-loaded rows —
    the caller must not issue a new query for this. Untranslated rows (blank target) never
    participate: an untranslated segment is not an inconsistency, just unfinished."""
    from collections import defaultdict

    groups: dict = defaultdict(list)  # norm(source) -> [(seg_id, norm(target)), ...]
    for seg_id, source_text, target_text in rows:
        if not (target_text or "").strip():
            continue
        groups[_consistency_norm(source_text)].append((seg_id, _consistency_norm(target_text)))
    out: set = set()
    for members in groups.values():
        if len({t for _sid, t in members}) > 1:
            out.update(sid for sid, _t in members)
    return out


def repeated_segment_ids(rows) -> set:
    """Given `rows` = an iterable of `(seg_id, source_text)`, the set of `seg_id`s whose
    normalized source (the SAME `_consistency_norm` the INCONSISTENT check groups by, and
    the one repeat propagation matches on) occurs in at least one other row. Source-only:
    a repeat is a repeat whether or not it is translated yet."""
    from collections import defaultdict

    groups: dict = defaultdict(list)
    for seg_id, source_text in rows:
        if (source_text or "").strip():
            groups[_consistency_norm(source_text)].append(seg_id)
    out: set = set()
    for members in groups.values():
        if len(members) > 1:
            out.update(members)
    return out
