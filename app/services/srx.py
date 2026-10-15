r"""SRX 2.0 (Segmentation Rules eXchange) rule engine.

This is the real, standards-based segmentation path. It is OPT-IN: the legacy
regex splitter in ``segment.py`` stays the byte-unchanged default (gated by the
``srx_enabled`` setting, default "0"). When enabled, ``segment.split_sentences``
delegates to :func:`segment_text` here with the source locale.

--------------------------------------------------------------------------------
DESIGN
--------------------------------------------------------------------------------
Document model (mirrors the SRX 2.0 schema we support):

  <srx>
    <header segmentsubflows="yes|no" cascade="yes|no"/>
    <body>
      <languagerules>
        <languagerule languagerulename="Default">
          <rule break="yes|no">
            <beforebreak>REGEX</beforebreak>
            <afterbreak>REGEX</afterbreak>
          </rule>
          ...
        </languagerule>
      </languagerules>
      <maprules>
        <languagemap languagepattern="ICU-regex" languagerulename="Default"/>
        ...
      </maprules>
    </body>
  </srx>

Apply semantics (SRX 2.0 §2.4):
  * Per source locale, ``maprules`` are scanned IN DOCUMENT ORDER; each
    ``languagemap`` whose ``languagepattern`` matches the locale contributes its
    named languagerule.
  * ``cascade="yes"`` (header): ALL matching languagerules apply, their rules
    concatenated in document order. ``cascade="no"``: only the FIRST matching
    languagemap's languagerule applies.
  * At each candidate position (every inter-character boundary), the rules are
    evaluated in order. The FIRST rule whose ``beforebreak`` matches the text
    ending at the position AND whose ``afterbreak`` matches the text starting at
    the position DECIDES that position: a ``break="no"`` rule SUPPRESSES a break;
    a ``break="yes"`` rule forces one. First match wins — later rules are not
    consulted for that position. (So exception/no-break rules are placed BEFORE
    the general break rules, the standard SRX idiom.)
  * A position with no matching rule is not a break.

``beforebreak`` is anchored to END at the position (we match it as ``(?:...)$``
against the left slice); ``afterbreak`` is anchored to START at the position
(``^(?:...)`` against the right slice). An empty/absent ``beforebreak`` or
``afterbreak`` matches anything (the SRX default).

--------------------------------------------------------------------------------
ICU -> Python ``re`` translation (:func:`icu_to_python`)
--------------------------------------------------------------------------------
SRX regexes are ICU-flavored. We do a documented best-effort translation of the
supported ICU escapes to plain-``re`` form. The translated patterns are COMPILED
with the ``regex`` module (a superset of ``re`` semantics; pinned in
requirements) because it is the only engine here that enforces a per-match
timeout — see the ReDoS guards below.

  SUPPORTED (translated to explicit unicode character-class alternations built
  once from ``unicodedata`` categories):
    \p{L}  \p{Lu}  \p{Ll}  \p{N}   and their negations \P{L} \P{Lu} \P{Ll} \P{N}
    (also the long forms \p{Letter}, \p{Uppercase_Letter}, \p{Lowercase_Letter},
     \p{Number} and single-letter category families L/N).
    Inside a character class ([...]) the \p{...} is spliced in as a range list;
    outside it is wrapped in its own group.

  NOT SUPPORTED (a translated doc using these will raise SrxError at parse time
  with a clear message, so an invalid upload is rejected rather than silently
  mis-segmenting):
    - Other \p{...} categories (\p{P}, \p{Zs}, scripts \p{Han}, blocks, etc.)
    - POSIX [[:alpha:]] classes and other constructs the translated pattern
      still makes ``regex.compile`` reject.
  NOTE: ICU's possessive quantifiers (a++, a*+) and atomic groups (?>...) pass
  through untranslated and COMPILE under ``regex`` (which implements them with
  ICU-faithful semantics), so documents using those constructs now parse instead
  of being rejected — no backtracking to blow up there either way.

  The translation is intentionally conservative: it only rewrites the \p/\P
  escapes and otherwise passes the regex through to ``regex`` unchanged. Common
  European/CJK break rulesets (the shipped ``default.srx`` included) use only the
  supported subset.

Documented in: docs/architecture/segmentation.md
"""

from __future__ import annotations

import contextlib
import contextvars
import hashlib
import logging
import re
import sys
import threading
import time
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path

import regex
from lxml import etree

_logger = logging.getLogger("loc_tms")

# --------------------------------------------------------------------------- #
# ReDoS / O(n^2) guards (finding F2)
# --------------------------------------------------------------------------- #
# beforebreak/afterbreak are matched against a BOUNDED window around the
# candidate boundary rather than the full growing prefix/suffix. This kills the
# old O(n^2) prefix rescans (text[:pos] + finditer per rule per position) and a
# malicious admin's catastrophic-backtracking rule (e.g. (a+)+): the regex never
# sees more than _CONTEXT_WINDOW characters at once, so its work is bounded.
#
# The shipped default.srx rules use single-character before/after context, so a
# 200-char window is orders of magnitude larger than they need — the corpus
# output stays byte-identical (verified by verify_srx). A custom rule whose
# before-context legitimately needs more than 200 chars of look-behind is
# extraordinarily unusual; if one existed it would only ever UNDER-match at the
# window edge (never mis-break), which is the safe direction.
_CONTEXT_WINDOW = 200

# Above this paragraph length, SRX segmentation is skipped entirely and the
# caller falls back to the legacy splitter (finding F2 part b). Even bounded
# per-position work is O(n) positions * rules; a multi-megabyte "paragraph" from
# a hostile upload could still be slow, so we cap it. Real paragraphs are far
# shorter; documents are already split into paragraphs before reaching here.
_MAX_SRX_PARAGRAPH = 100_000

# Pattern bounds for the bounded window above (red-team audit 2026-10-03 finding
# 02). The window caps how MUCH text a rule regex sees per position, but a
# catastrophic pattern ((a|aa)+b style) is exponential INSIDE a 200-char window
# too, so rule patterns additionally run under the `regex` module's hard
# per-match wall-clock timeout, and a raw rule pattern longer than
# _SRX_PATTERN_MAX_LEN is refused at parse/upload time, before anything is
# compiled. Unlike the Contentful fail-open, a match timeout here is LOUD: it
# raises SrxError naming the rule, and the caller falls back to the built-in
# default rules so the admin learns which rule to fix.
_SRX_PATTERN_MAX_LEN = 1000
_SRX_MATCH_TIMEOUT_S = 0.2

# TOTAL wall-clock budget for segmenting ONE paragraph with a custom ruleset (#511). The
# per-match timeout above does not bound the run: a rule that backtracks for 150 ms
# never times out, yet at every position of a 50,000-character paragraph it adds up to
# hours inside the ingest worker. Past this budget segment_spans raises SrxError and
# spans_with_default_fallback re-segments the paragraph with the built-in default rules
# (which run without a budget, so the fallback itself can never fail on a busy box).
_SRX_PARAGRAPH_BUDGET_S = 10.0


class SrxError(Exception):
    """Raised when an SRX document cannot be parsed / translated / compiled.

    Carries a human-readable message suitable for surfacing to an admin who
    uploaded an invalid .srx file."""


# --------------------------------------------------------------------------- #
# ICU \p{...} -> Python re unicode character-class translation
# --------------------------------------------------------------------------- #
# Built lazily once and cached: the set of codepoint ranges for each supported
# unicode general-category family, rendered as a character-class body string
# (e.g. "A-Za-zÀ-ÿ...") usable inside [] or, wrapped, standalone.
_CLASS_CACHE: dict[str, str] = {}
_CLASS_LOCK = threading.Lock()

# Map the ICU property token (case-insensitive, normalized) to the set of
# unicodedata category codes it covers.
_PROP_CATEGORIES: dict[str, tuple[str, ...]] = {
    "L": ("Lu", "Ll", "Lt", "Lm", "Lo"),
    "LETTER": ("Lu", "Ll", "Lt", "Lm", "Lo"),
    "LU": ("Lu",),
    "UPPERCASE_LETTER": ("Lu",),
    "LL": ("Ll",),
    "LOWERCASE_LETTER": ("Ll",),
    "N": ("Nd", "Nl", "No"),
    "NUMBER": ("Nd", "Nl", "No"),
}


def _ranges_for_categories(cats: tuple[str, ...]) -> str:
    """Build a character-class body (contents that go inside [...]) covering
    every codepoint whose unicode general category is in ``cats``.

    Codepoints are collapsed into ``a-b`` ranges and escaped for use in a class.
    Scans the whole BMP + astral plane once; cached by the caller."""
    catset = set(cats)
    ranges: list[tuple[int, int]] = []
    start = None
    prev = None
    # Scanning the full unicode space is ~1.1M codepoints; done at most once per
    # property token per process (cached), well under a second.
    for cp in range(sys.maxunicode + 1):
        try:
            in_cat = unicodedata.category(chr(cp)) in catset
        except ValueError:  # pragma: no cover - surrogates
            in_cat = False
        if in_cat:
            if start is None:
                start = cp
            prev = cp
        else:
            if start is not None:
                ranges.append((start, prev))
                start = None
    if start is not None:
        ranges.append((start, prev))

    def esc(cp: int) -> str:
        # Escape the characters that are special inside a character class.
        if cp in (0x5C, 0x5D, 0x5E, 0x2D):  # \ ] ^ -
            return "\\" + chr(cp)
        if cp < 0x20 or cp == 0x7F:
            return f"\\x{cp:02x}"
        return chr(cp)

    parts: list[str] = []
    for a, b in ranges:
        if a == b:
            parts.append(esc(a))
        elif b == a + 1:
            parts.append(esc(a) + esc(b))
        else:
            parts.append(esc(a) + "-" + esc(b))
    return "".join(parts)


def _class_body(token: str) -> str:
    """Return the cached character-class body for an ICU property token."""
    key = token.upper()
    cats = _PROP_CATEGORIES.get(key)
    if cats is None:
        raise SrxError(
            f"Unsupported ICU unicode property \\p{{{token}}}: only L, Lu, Ll, N "
            "(and their long names / negations) are translated."
        )
    with _CLASS_LOCK:
        body = _CLASS_CACHE.get(key)
        if body is None:
            body = _ranges_for_categories(cats)
            _CLASS_CACHE[key] = body
    return body


# Matches \p{...}, \P{...}, \pL, \PL (single-letter category shorthand).
_PROP_RE = re.compile(r"\\([pP])(?:\{([^}]*)\}|([A-Za-z]))")


def icu_to_python(pattern: str) -> str:
    """Translate the supported ICU \\p{...} / \\P{...} escapes in ``pattern`` to
    stdlib-``re`` unicode character classes. Everything else passes through.

    Raises :class:`SrxError` for an unsupported property token so the caller can
    reject the whole SRX document rather than mis-segment silently."""
    if not pattern:
        return pattern

    # Track whether each match sits INSIDE a character class ([...]) so we splice
    # the ranges directly rather than wrapping them in their own []. A simple
    # left-to-right scan honoring escapes is enough for the rule regexes we see.
    out: list[str] = []
    i = 0
    n = len(pattern)
    in_class = False
    while i < n:
        ch = pattern[i]
        if ch == "\\" and i + 1 < n:
            nxt = pattern[i + 1]
            if nxt in "pP":
                m = _PROP_RE.match(pattern, i)
                if not m:
                    raise SrxError(f"Malformed \\p unicode property near: {pattern[i : i + 8]!r}")
                negate = m.group(1) == "P"
                token = m.group(2) if m.group(2) is not None else m.group(3)
                body = _class_body(token)
                if negate:
                    # Negation is only sound as a standalone class. Inside an
                    # existing [...] a negated property can't be expressed as a
                    # simple splice, so reject rather than silently invert wrong.
                    if in_class:
                        raise SrxError(
                            f"Negated \\P{{{token}}} inside a character class is not supported."
                        )
                    out.append("[^" + body + "]")
                else:
                    out.append(body if in_class else "[" + body + "]")
                i = m.end()
                continue
            # Ordinary escape: copy the pair verbatim.
            out.append(pattern[i : i + 2])
            i += 2
            continue
        if ch == "[":
            in_class = True
        elif ch == "]":
            in_class = False
        out.append(ch)
        i += 1
    return "".join(out)


def _compile(pattern: str, *, what: str) -> regex.Pattern | None:
    """Translate + compile an SRX rule regex. Empty/None -> None (matches all).

    ``what`` names the field for a clear SrxError. Uploaded rule patterns are
    third-party input, so two guards apply (red-team audit 2026-10-03 finding
    02): an over-long pattern is refused here (parse/upload time), and the engine
    is ``regex`` so every match carries _SRX_MATCH_TIMEOUT_S (see Rule)."""
    if pattern is None or pattern == "":
        return None
    if len(pattern) > _SRX_PATTERN_MAX_LEN:
        raise SrxError(
            f"{what} regex is {len(pattern)} chars, above the "
            f"{_SRX_PATTERN_MAX_LEN}-character limit for an SRX rule pattern; "
            "simplify the rule or split it into several rules."
        )
    translated = icu_to_python(pattern)
    try:
        return regex.compile(translated, regex.UNICODE | regex.DOTALL)
    except regex.error as e:
        raise SrxError(f"Invalid {what} regex {pattern!r} (translated: {translated!r}): {e}") from e


# --------------------------------------------------------------------------- #
# Document model
# --------------------------------------------------------------------------- #
@dataclass
class Rule:
    break_: bool
    before: regex.Pattern | None
    after: regex.Pattern | None
    # Raw (pre-translation) pattern text, kept ONLY so a match-time SrxError can
    # name the rule for the admin who uploaded it (red-team finding 02).
    before_src: str | None = None
    after_src: str | None = None
    # `before` anchored to the END of the window, built once (see _before_at_end).
    before_end: regex.Pattern | None = field(default=None, init=False, repr=False)

    def __post_init__(self) -> None:
        if self.before is not None:
            self.before_end = regex.compile(
                "(?:" + self.before.pattern + ")" + chr(92) + "Z", self.before.flags
            )

    def decides_at(self, left_window: str, right_window: str) -> bool | None:
        """Does this rule fire at the boundary between the two windows?

        ``left_window`` is the (bounded) tail of the text ending AT the boundary;
        ``right_window`` is the (bounded) head of the text starting at it. Both
        are capped by the caller to ``_CONTEXT_WINDOW`` chars so a pathological
        beforebreak/afterbreak can never scan an unbounded, growing slice
        (finding F2). Each match additionally runs under
        _SRX_MATCH_TIMEOUT_S; exceeding it raises SrxError naming the rule.
        Returns the break decision (True/False) if the rule
        matches, else None."""
        if self.before is not None:  # noqa: SIM102
            # beforebreak must END exactly at the boundary (the tail of
            # left_window). Scan matches within the bounded window and accept one
            # ending at the window's end. The window is short, so this is O(1) in
            # the paragraph length regardless of how far back the boundary sits.
            if not self._before_at_end(left_window):
                return None
        if self.after is not None:  # noqa: SIM102
            if not self._after_at_start(right_window):
                return None
        return self.break_

    def _timeout_error(self, what: str, src: str | None) -> SrxError:
        return SrxError(
            f"SRX rule {what} regex {src!r} exceeded the "
            f"{_SRX_MATCH_TIMEOUT_S}-second match limit (possible catastrophic "
            "backtracking); fix or remove the rule"
        )

    def _after_at_start(self, right_window: str) -> bool:
        try:
            return self.after.match(right_window, timeout=_SRX_MATCH_TIMEOUT_S) is not None
        except TimeoutError:
            raise self._timeout_error("afterbreak", self.after_src) from None

    def _before_at_end(self, left_window: str) -> bool:
        # Find any match of `before` that ends exactly at len(left_window) (i.e.
        # at the boundary). `left_window` is a bounded suffix, so finditer here is
        # cheap and can't blow up on a growing prefix.
        #
        # `finditer` yields only NON-overlapping leftmost matches, so for `[A-Z]\.[A-Z]\.`
        # over "See U.S.A." it saw `U.S.` and never `S.A.`, which is the one that ends at
        # the boundary (#511). A search for the pattern anchored at the end finds any
        # match that ends there, overlapping or not.
        try:
            return self.before_end.search(left_window, timeout=_SRX_MATCH_TIMEOUT_S) is not None
        except TimeoutError:
            raise self._timeout_error("beforebreak", self.before_src) from None


@dataclass
class LanguageRule:
    name: str
    rules: list[Rule] = field(default_factory=list)


@dataclass
class LanguageMap:
    pattern: re.Pattern
    rulename: str


@dataclass
class SrxDocument:
    cascade: bool
    languagerules: dict[str, LanguageRule]
    maprules: list[LanguageMap]

    def resolve_rules(self, locale: str) -> list[Rule]:
        """The ordered rule list for a source locale, per cascade semantics."""
        chosen: list[Rule] = []
        seen_names: set[str] = set()
        for lm in self.maprules:
            if lm.pattern.match(locale or ""):
                lr = self.languagerules.get(lm.rulename)
                if lr is None:
                    continue
                if not self.cascade:
                    return list(lr.rules)
                # cascade: concatenate, but never the same languagerule twice.
                if lr.name not in seen_names:
                    seen_names.add(lr.name)
                    chosen.extend(lr.rules)
        return chosen

    def segment_spans(
        self, text: str, locale: str, budget_s: float | None = None
    ) -> list[tuple[int, int]]:
        """Split ``text`` into (start, end) offset spans for ``locale``.

        This is the offset-exact primitive (finding F3): callers that need to map
        segments back onto the original text (e.g. inline-token-span merging in
        segment.py) use these contiguous offsets instead of ``str.find`` on
        stripped pieces, which a repeated substring could mis-locate.

        Boundaries land BEFORE the ``afterbreak`` (SRX convention). Spans are
        contiguous and cover ``text`` exactly: ``spans[0][0] == 0``,
        ``spans[-1][1] == len(text)``, and each span's end is the next span's
        start. Returns ``[(0, len(text))]`` when no rule applies.

        ``budget_s`` (default: ``_SRX_PARAGRAPH_BUDGET_S``) is the total wall-clock
        allowance for this paragraph; running past it raises SrxError so the caller falls
        back to the built-in default rules. ``0`` or a negative value disables the budget
        (only the built-in fallback passes it)."""
        if not text:
            return []
        n = len(text)
        if budget_s is None:
            budget_s = _SRX_PARAGRAPH_BUDGET_S
        deadline = time.monotonic() + budget_s if budget_s > 0 else None
        # Paragraph-length guard (finding F2 part b). This method keeps a total
        # API (returns a single whole-text span above the cap). The real legacy
        # fallback lives in the callers that own it — segment_text() and
        # segment_spans_for_engine() short-circuit to legacy above the cap — so
        # this branch only defends against an accidental direct call on a huge
        # string and never does the O(n) per-position work on it.
        if n > _MAX_SRX_PARAGRAPH:
            _logger.warning(
                "SRX segmentation skipped: paragraph of %d chars exceeds the "
                "%d-char cap; using a single span (legacy fallback expected).",
                n,
                _MAX_SRX_PARAGRAPH,
            )
            return [(0, n)]
        rules = self.resolve_rules(locale)
        if not rules:
            return [(0, n)]
        cuts = [0]
        w = _CONTEXT_WINDOW
        # Candidate positions: every inter-character boundary 1..n-1. At each we
        # test the rules against BOUNDED windows around the boundary, not the full
        # growing prefix/suffix (finding F2).
        for pos in range(1, n):
            if deadline is not None and time.monotonic() > deadline:
                raise SrxError(
                    f"SRX segmentation of a {n}-character paragraph exceeded the "
                    f"{budget_s:g}-second budget (a custom rule is too slow); fix or "
                    "remove the slow rule"
                )
            left_window = text[max(0, pos - w) : pos]
            right_window = text[pos : pos + w]
            for r in rules:
                decision = r.decides_at(left_window, right_window)
                if decision is None:
                    continue
                if decision:
                    cuts.append(pos)
                break  # first matching rule wins for this position
        cuts.append(n)
        return [(a, b) for a, b in zip(cuts, cuts[1:], strict=False) if b > a]  # noqa: RUF007

    def segment(self, text: str, locale: str) -> list[str]:
        """Split ``text`` into segments for ``locale`` (raw, UNSTRIPPED pieces).

        Preserves the original contract used across callers: the concatenation of
        the returned pieces equals ``text``. Implemented on top of
        :meth:`segment_spans` so the offset-exact path and the piece path can
        never diverge. Boundaries land BEFORE the ``afterbreak``."""
        if not text:
            return []
        spans = self.segment_spans(text, locale)
        out = [text[a:b] for a, b in spans if b > a]
        return out or [text]


# --------------------------------------------------------------------------- #
# Parsing
# --------------------------------------------------------------------------- #
def _localname(tag) -> str:
    """Element local name, namespace-insensitive (SRX docs may or may not carry
    the http://www.lisa.org/srx20 namespace)."""
    if not isinstance(tag, str):
        return ""
    return tag.rsplit("}", 1)[-1]


def _find_all(parent, name: str):
    return [el for el in parent.iter() if _localname(el.tag) == name]


def parse(xml: bytes | str) -> SrxDocument:
    """Parse SRX 2.0 XML into an :class:`SrxDocument`.

    Raises :class:`SrxError` on malformed XML, an unsupported ICU construct, or
    an uncompilable rule regex."""
    if isinstance(xml, str):
        xml = xml.encode("utf-8")
    parser = etree.XMLParser(
        resolve_entities=False, no_network=True, dtd_validation=False, load_dtd=False
    )
    try:
        root = etree.fromstring(xml, parser=parser)
    except etree.XMLSyntaxError as e:
        raise SrxError(f"Malformed SRX XML: {e}") from e

    if _localname(root.tag) != "srx":
        raise SrxError(f"Root element is <{_localname(root.tag)}>, expected <srx>.")

    # header/@cascade (default "no" per the spec's absence)
    cascade = False
    headers = _find_all(root, "header")
    if headers:
        cval = (headers[0].get("cascade") or "").strip().lower()
        cascade = cval == "yes"

    languagerules: dict[str, LanguageRule] = {}
    for lr_el in _find_all(root, "languagerule"):
        name = lr_el.get("languagerulename") or ""
        lr = LanguageRule(name=name)
        for rule_el in _find_all(lr_el, "rule"):
            brk = (rule_el.get("break") or "yes").strip().lower() != "no"
            before_txt = None
            after_txt = None
            for child in rule_el:
                ln = _localname(child.tag)
                if ln == "beforebreak":
                    before_txt = child.text or ""
                elif ln == "afterbreak":
                    after_txt = child.text or ""
            lr.rules.append(
                Rule(
                    break_=brk,
                    before=_compile(before_txt, what="beforebreak"),
                    after=_compile(after_txt, what="afterbreak"),
                    before_src=before_txt,
                    after_src=after_txt,
                )
            )
        if not name:
            raise SrxError("<languagerule> is missing languagerulename.")
        languagerules[name] = lr

    maprules: list[LanguageMap] = []
    for lm_el in _find_all(root, "languagemap"):
        pat = lm_el.get("languagepattern") or ""
        rulename = lm_el.get("languagerulename") or ""
        if not rulename:
            raise SrxError("<languagemap> is missing languagerulename.")
        if rulename not in languagerules:
            raise SrxError(f"<languagemap> references unknown languagerule {rulename!r}.")
        try:
            compiled = re.compile(icu_to_python(pat) if pat else ".*", re.UNICODE)
        except re.error as e:
            raise SrxError(f"Invalid languagepattern {pat!r}: {e}") from e
        maprules.append(LanguageMap(pattern=compiled, rulename=rulename))

    if not languagerules:
        raise SrxError("SRX document defines no <languagerule>.")

    return SrxDocument(cascade=cascade, languagerules=languagerules, maprules=maprules)


def validate(xml: bytes | str) -> tuple[bool, str]:
    """Parse-validate an SRX document without keeping it. Returns (ok, message).

    Used by the admin upload path to reject an invalid file with a clear reason
    before it is written into the data dir."""
    try:
        doc = parse(xml)
    except SrxError as e:
        return False, str(e)
    except Exception as e:  # defensive: never let a parser quirk 500 the upload
        return False, f"Could not parse SRX: {e}"
    n_rules = sum(len(lr.rules) for lr in doc.languagerules.values())
    return True, "OK: %d language rule set(s), %d rule(s), %d map(s)." % (  # noqa: UP031
        len(doc.languagerules),
        n_rules,
        len(doc.maprules),
    )


# --------------------------------------------------------------------------- #
# Active-document resolution (setting-gated, cached by file mtime)
# --------------------------------------------------------------------------- #
# The shipped default ruleset lives next to the package as a resource.
_DEFAULT_SRX_PATH = Path(__file__).resolve().parent.parent / "resources" / "default.srx"
# The version 1 shipped ruleset (no abbreviation rules; decision 15 moved default.srx to
# version 2 on 2026-10-11). Kept byte-identical so a document stamped "srx:<v1 hash>"
# still re-splits exactly as imported, and it stays the built-in FALLBACK for a custom
# rule that times out: a recorded fallback is replayed with the same rules at export
# whatever the document's age (see spans_with_default_fallback).
_DEFAULT_V1_SRX_PATH = _DEFAULT_SRX_PATH.with_name("default_v1.srx")

# Cache the parsed SrxDocument keyed by (path, mtime) so repeated segmentation
# does not re-parse + re-translate the XML on every paragraph. A custom upload
# replacing the file bumps mtime and invalidates the entry.
_DOC_CACHE: dict[tuple[str, float], SrxDocument] = {}
_DOC_CACHE_LOCK = threading.Lock()


def _load_path(path: Path) -> SrxDocument:
    try:
        mtime = path.stat().st_mtime
    except OSError as e:
        raise SrxError(f"SRX file not readable ({path}): {e}") from e
    key = (str(path), mtime)
    with _DOC_CACHE_LOCK:
        doc = _DOC_CACHE.get(key)
    if doc is None:
        doc = parse(path.read_bytes())
        with _DOC_CACHE_LOCK:
            _DOC_CACHE[key] = doc
    return doc


def default_document() -> SrxDocument:
    """The shipped default.srx, parsed + cached."""
    return _load_path(_DEFAULT_SRX_PATH)


def fallback_document() -> SrxDocument:
    """The built-in rules a timed-out custom rule falls back to: the version 1
    default ruleset (unchanged since the fallback was introduced, so a fallback
    recorded at ingest replays identically at export for old and new documents)."""
    return _load_path(_DEFAULT_V1_SRX_PATH)


def _eol_variants(data: bytes) -> list[bytes]:
    """``data`` as stored plus its LF and CRLF forms: a shipped .srx is hashed from the
    working tree, which git checks out CRLF on Windows and LF on Linux, so a stamp
    written on one platform must still resolve on the other."""
    lf = data.replace(b"\r\n", b"\n")
    return list(dict.fromkeys([data, lf, lf.replace(b"\n", b"\r\n")]))


def default_ruleset_bytes() -> bytes:
    """The raw bytes of the shipped default.srx (the built-in/legacy-mirror rung of
    the P6 resolution chain; a NULL-blob SegmentationProfile resolves to these)."""
    return _DEFAULT_SRX_PATH.read_bytes()


# Parsed-document cache keyed by ruleset-sha (P6): a profile's custom blob is parsed
# once and reused for every paragraph of a document ingested under it, mirroring the
# file-mtime cache above for on-disk rulesets.
_BLOB_DOC_CACHE: dict[str, SrxDocument] = {}
_BLOB_DOC_CACHE_LOCK = threading.Lock()


def document_for_bytes(ruleset: bytes) -> SrxDocument:
    """Parse (and cache by content hash) an in-memory SRX ruleset into an
    :class:`SrxDocument` (P6). Used when a resolved SegmentationProfile's blob —
    rather than the globally-active ruleset — drives segmentation at ingest.
    Raises :class:`SrxError` on an invalid ruleset (the caller falls back)."""
    key = _ruleset_hash(ruleset)
    with _BLOB_DOC_CACHE_LOCK:
        doc = _BLOB_DOC_CACHE.get(key)
    if doc is None:
        doc = parse(ruleset)
        with _BLOB_DOC_CACHE_LOCK:
            _BLOB_DOC_CACHE[key] = doc
    return doc


def custom_srx_path() -> Path:
    """Where an admin-uploaded custom .srx is stored in the data dir."""
    from app import config

    return config.DATA_DIR / "segmentation.srx"


def write_custom_ruleset(raw: bytes) -> Path:
    """Install `raw` (already parse-validated by the caller) as the custom ruleset,
    atomically: a temp file in the same directory then os.replace, so a concurrent
    ingest never reads a half-written file. The one writer for web, /api and CLI.
    Who may call it is the door's decision (#424: host-only under multi-org)."""
    import contextlib
    import os
    import tempfile

    path = custom_srx_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".segmentation.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(raw)
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise
    return path


def active_document() -> SrxDocument:
    """Resolve the SRX document per the ``srx_file`` setting.

    Blank setting -> the shipped default.srx. A non-blank value names a file in
    the data dir (the custom upload). Falls back to the default if the named
    file is missing or fails to parse (segmentation must never crash ingest)."""
    from app.services import settings

    name = (settings.get("srx_file") or "").strip()
    if name:
        path = custom_srx_path()
        if path.exists():
            try:
                return _load_path(path)
            except SrxError:
                # Corrupt custom file: fall back to the default rather than break
                # ingestion. (Upload is parse-validated, so this is belt-and-braces.)
                pass
    return default_document()


# --------------------------------------------------------------------------- #
# Segmentation-identity stamping (finding F1)
# --------------------------------------------------------------------------- #
# Export must re-split a document with the EXACT segmentation that produced its
# stored segments at ingest — otherwise toggling srx_enabled or swapping the
# active .srx between ingest and export re-splits differently and scrambles the
# target alignment. To make ingest reproducible we stamp a "segmentation engine"
# identity in the skeleton at ingest and thread it back through export.
#
# Engine string grammar:
#   "legacy"                -> the legacy regex splitter, version 1 (srx disabled at
#                              ingest, before decision 15: no abbreviation rules)
#   "legacy:2"              -> the legacy regex splitter, version 2 (abbreviation
#                              rules; what ingest stamps now with srx disabled)
#   "srx:<12-hex>"          -> the SRX engine with the ruleset whose bytes hash to
#                              that 12-char sha1 prefix
# The ruleset hash lets export detect a swapped/removed custom ruleset (hash
# mismatch) and fall back to legacy WITH A WARNING rather than silently
# mis-segmenting.

_ENGINE_LEGACY = "legacy"
_ENGINE_LEGACY_V2 = "legacy:2"  # == segment.LEGACY_ENGINE_V2 (srx.py stays a leaf)
_ENGINE_SRX_PREFIX = "srx:"


def active_ruleset_bytes() -> bytes:
    """The raw bytes of the currently active SRX ruleset (custom upload if the
    ``srx_file`` setting names one and it exists, else the shipped default). Used
    to compute the ingest-time segmentation-identity hash."""
    from app.services import settings

    name = (settings.get("srx_file") or "").strip()
    if name:
        path = custom_srx_path()
        if path.exists():
            try:
                return path.read_bytes()
            except OSError:
                pass
    return _DEFAULT_SRX_PATH.read_bytes()


def _ruleset_hash(data: bytes) -> str:
    return hashlib.sha1(data, usedforsecurity=False).hexdigest()[:12]  # a cache/display key


def engine_identity(srx_enabled: bool) -> str:
    """Compute the segmentation-engine identity to stamp at ingest.

    ``srx_enabled`` is the live gate at ingest time. Returns ``"legacy:2"`` (the
    version 2 built-in splitter) when the SRX path is off, else ``"srx:<hash>"``
    binding the exact active ruleset bytes so export can reproduce (or detect the
    loss of) this segmentation."""
    if not srx_enabled:
        return _ENGINE_LEGACY_V2
    return _ENGINE_SRX_PREFIX + _ruleset_hash(active_ruleset_bytes())


def engine_identity_for_bytes(srx_enabled: bool, ruleset: bytes) -> str:
    """The ingest-time segmentation identity for an EXPLICIT ruleset (P6).

    Like :func:`engine_identity` but binds the given ``ruleset`` bytes rather than
    the globally-active ruleset — used when a resolved SegmentationProfile drives
    ingest so the stamp reflects the profile's actual ruleset (which is what export
    reproduces). ``srx_enabled`` off still returns ``"legacy:2"`` (the master gate)."""
    if not srx_enabled:
        return _ENGINE_LEGACY_V2
    return _ENGINE_SRX_PREFIX + _ruleset_hash(ruleset)


# A callback the profile layer registers so export's hash resolution can find a
# SegmentationProfile's ruleset bytes by their sha1 prefix without srx.py importing
# the (higher-level) profile service — avoids an import cycle and keeps srx.py a
# leaf. Returns the raw ruleset bytes whose hash matches ``want_hash``, or None.
_PROFILE_BLOB_RESOLVER = None


def register_profile_blob_resolver(fn) -> None:
    """Install (or clear with None) the callback that resolves a stamped ruleset
    hash to a SegmentationProfile's ruleset bytes (P6). Called once at import of
    app.services.segmentation_profiles; tests may override it."""
    global _PROFILE_BLOB_RESOLVER
    _PROFILE_BLOB_RESOLVER = fn


def _document_for_hash(want_hash: str) -> SrxDocument | None:
    """Resolve the parsed SrxDocument whose ruleset bytes hash to ``want_hash``.

    Checks the active ruleset first (fast path: nothing changed since ingest),
    then the shipped default, then any custom file on disk. Returns None when no
    available ruleset matches the stamped hash (the custom .srx was swapped or
    removed) — the caller then falls back to legacy with a warning."""
    candidates: list[bytes] = []
    try:  # noqa: SIM105
        candidates.append(active_ruleset_bytes())
    except OSError:
        pass
    try:  # noqa: SIM105
        candidates.append(_DEFAULT_SRX_PATH.read_bytes())
    except OSError:
        pass
    # Decision 15: documents stamped with the version 1 shipped ruleset (in either
    # line-ending form) keep resolving after default.srx moved to version 2.
    try:  # noqa: SIM105
        candidates.extend(_eol_variants(_DEFAULT_V1_SRX_PATH.read_bytes()))
    except OSError:
        pass
    path = custom_srx_path()
    if path.exists():
        try:  # noqa: SIM105
            candidates.append(path.read_bytes())
        except OSError:
            pass
    for data in candidates:
        if _ruleset_hash(data) == want_hash:
            try:
                return parse(data)
            except SrxError:
                return None
    # P6: the stamped ruleset may belong to a SegmentationProfile whose custom blob
    # is neither the active ruleset, the default, nor the on-disk custom file (e.g. a
    # per-project/per-customer profile). Ask the registered profile resolver for the
    # bytes whose hash matches so export reproduces that profile's ingest-time split.
    global _PROFILE_BLOB_RESOLVER
    if _PROFILE_BLOB_RESOLVER is None:
        # Self-heal: a process that never imported the profile service (e.g. a
        # one-shot `doc export` CLI run) must still reproduce a profile-stamped
        # split — a legacy fallback here would silently misalign the export.
        try:
            from app.services.segmentation_profiles import _blob_resolver

            _PROFILE_BLOB_RESOLVER = _blob_resolver
        except Exception:
            # Without the resolver a profile-stamped split falls back to legacy and misaligns.
            _logger.warning("srx: segmentation profile resolver unavailable", exc_info=True)
    if _PROFILE_BLOB_RESOLVER is not None:
        try:
            data = _PROFILE_BLOB_RESOLVER(want_hash)
        except Exception:
            data = None
        if data and _ruleset_hash(data) == want_hash:
            try:
                return parse(data)
            except SrxError:
                return None
    return None


def segment_spans_for_engine(
    text: str,
    locale: str,
    engine: str,
) -> list[tuple[int, int]] | None:
    """Offset-exact segmentation reproducing a STAMPED engine identity (finding
    F1 + F3). ``engine`` is the value stamped at ingest (see
    :func:`engine_identity`).

    Returns contiguous (start, end) spans covering ``text`` exactly, or None to
    signal 'use the legacy splitter' — either because ``engine`` is the legacy
    identity, the paragraph exceeds the SRX cap (finding F2), or the stamped
    ruleset is no longer available (hash mismatch; a clear warning is logged)."""
    if not text:
        return []
    if not engine or engine in (_ENGINE_LEGACY, _ENGINE_LEGACY_V2):
        return None
    if not engine.startswith(_ENGINE_SRX_PREFIX):
        # Unknown stamp: be conservative and use legacy.
        _logger.warning("Unknown segmentation engine stamp %r; using legacy splitter.", engine)
        return None
    want_hash = engine[len(_ENGINE_SRX_PREFIX) :]
    doc = _document_for_hash(want_hash)
    if doc is None:
        _logger.warning(
            "Segmentation identity lost: stamped SRX ruleset %r is no longer "
            "available (custom .srx swapped or removed). Falling back to the "
            "legacy splitter for re-segmentation; export may misalign.",
            engine,
        )
        return None
    if len(text) > _MAX_SRX_PARAGRAPH:
        _logger.warning(
            "SRX re-segmentation skipped: paragraph of %d chars exceeds the "
            "%d-char cap; using the legacy splitter.",
            len(text),
            _MAX_SRX_PARAGRAPH,
        )
        return None
    try:
        # A stamped-ruleset rule that blows the match timeout falls back to the
        # built-in default rules, the SAME fallback ingest took for that paragraph
        # (spans_with_default_fallback), so export re-splits the way ingest did.
        return spans_with_default_fallback(doc, text, locale or "")
    except SrxError as e:  # pragma: no cover - the shipped default must not time out
        _logger.warning(
            "SRX re-segmentation failed for engine %s (%s); using the legacy splitter.",
            engine,
            e,
        )
        return None


# Deterministic fallback (#511 gate). The paragraph budget is wall-clock, so the SAME
# paragraph could fall back at ingest and not at export on a slower or busier box (or
# the reverse), changing the sentence count export aligns targets by. Ingest therefore
# RECORDS which paragraphs fell back (skeleton["srx_fallback"], keys from
# fallback_key), and export REPLAYS that decision: a recorded paragraph goes straight
# to the default rules, any other runs the stamped rules with no budget (it finished
# inside the budget at ingest). Documents without the key replay "no fallback", which
# is exactly the pre-#511 behaviour they were ingested under.
_FALLBACK_RECORD: contextvars.ContextVar[set | None] = contextvars.ContextVar(
    "srx_fallback_record", default=None
)
_FALLBACK_REPLAY: contextvars.ContextVar[frozenset | None] = contextvars.ContextVar(
    "srx_fallback_replay", default=None
)


def fallback_key(text: str, locale: str) -> str:
    """A short stable key for one paragraph's fallback decision (locale + stripped text)."""
    raw = f"{locale}\x00{(text or '').strip()}".encode("utf-8", "surrogatepass")
    return hashlib.sha256(raw).hexdigest()[:20]


@contextlib.contextmanager
def record_fallbacks():
    """Ingest: collect the fallback_key of every paragraph that fell back to the
    default rules inside this block (the caller stores them on the skeleton)."""
    seen: set = set()
    tok = _FALLBACK_RECORD.set(seen)
    try:
        yield seen
    finally:
        _FALLBACK_RECORD.reset(tok)


@contextlib.contextmanager
def replay_fallbacks(keys):
    """Export: re-segment with the ingest-time fallback decisions in ``keys``."""
    tok = _FALLBACK_REPLAY.set(frozenset(keys or ()))
    try:
        yield
    finally:
        _FALLBACK_REPLAY.reset(tok)


def spans_with_default_fallback(doc: SrxDocument, text: str, locale: str) -> list[tuple[int, int]]:
    """``doc.segment_spans`` with the red-team finding 02 fallback: when a rule in
    ``doc`` exceeds the per-match timeout or the paragraph budget (SrxError naming the
    cause), log a loud warning and segment this text with the built-in default rules
    instead. Shared by every SRX path (live ingest, profile ingest, stamped export,
    segment_text); under replay_fallbacks the ingest-time decision is reused, never
    re-made against this box's clock. An SrxError from the default ruleset itself
    propagates (callers then use the legacy splitter)."""
    replay = _FALLBACK_REPLAY.get()
    budget_s = None
    if replay is not None:
        if fallback_key(text, locale) in replay:
            return fallback_document().segment_spans(text, locale, budget_s=0)
        budget_s = 0
    try:
        return doc.segment_spans(text, locale, budget_s=budget_s)
    except SrxError as e:
        _logger.warning(
            "SRX segmentation of this document fell back to the built-in default rules: %s",
            e,
        )
        record = _FALLBACK_RECORD.get()
        if record is not None:
            record.add(fallback_key(text, locale))
        return fallback_document().segment_spans(text, locale, budget_s=0)


def segment_text(text: str, locale: str) -> list[str]:
    """SRX segmentation entry point used by ``segment.split_sentences`` when the
    ``srx_enabled`` gate is on. Returns stripped, non-empty segments — matching
    the legacy splitter's post-processing so callers see the same shape.

    Enforces the paragraph-length cap (finding F2): a paragraph longer than
    ``_MAX_SRX_PARAGRAPH`` returns a single piece so the caller (which strips and
    would otherwise re-split) degrades gracefully rather than doing bounded but
    still-large per-position work on a hostile multi-megabyte 'paragraph'."""
    text = (text or "").strip()
    if not text:
        return []
    if len(text) > _MAX_SRX_PARAGRAPH:
        _logger.warning(
            "SRX segmentation skipped: paragraph of %d chars exceeds the "
            "%d-char cap; returning it unsplit (legacy fallback).",
            len(text),
            _MAX_SRX_PARAGRAPH,
        )
        return [text]
    try:
        doc = active_document()
    except SrxError:
        # Absolute last resort: never raise into ingest/export.
        return [text]
    try:
        # A rule regex in the active (custom) ruleset that times out mid-segmentation
        # (red-team finding 02) does NOT fail silently and does NOT keep the bad
        # ruleset's partial answer: the helper re-segments with the built-in default
        # rules and logs the offending rule loudly so the admin can fix the upload.
        spans = spans_with_default_fallback(doc, text, locale or "")
    except SrxError:  # pragma: no cover - the shipped default must not time out
        return [text]
    pieces = [text[a:b] for a, b in spans if b > a] or [text]
    out = [p.strip() for p in pieces if p and p.strip()]
    return out or [text]
