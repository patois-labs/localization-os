"""Segmentation, inline-tag extraction, and size-aware chunk planning.

- Segmentation splits a paragraph into sentence-level segments (the atomic TM
  unit). Real tools use SRX rules; this is a pragmatic sentence splitter that
  also handles CJK sentence enders.
- Inline-tag extraction finds the things MT must NOT translate or must preserve
  verbatim: placeholders ({0}, {name}), printf specifiers (%s, %1$d), HTML tags,
  and URLs. Later chunks pass these to the engine as preservation constraints.
- Chunk planning groups consecutive paragraphs into request-sized chunks so the
  orchestrator can translate a huge document without overloading a provider.

Documented in: docs/architecture/segmentation.md
"""

import math
import re

# Sentence-ending punctuation: Latin + common CJK.
_SENT_SPLIT = re.compile(r"(?<=[.!?])\s+(?=[\"'(\[A-Z0-9])")
_CJK_SPLIT = re.compile(r"(?<=[。！？])")

# ---------------------------------------------------------------------------------------
# Abbreviation rules (decision 15, 2026-10-11): splitter version 2.
#
# Version 1 (the stamp "legacy") broke after every "." followed by a space and a capital
# letter or digit, so "Max. 5 files", "incl. VAT" and "Call us, ext. 42." reached the engine
# as fragments (Japanese came back "最大。5個..." with a full stop mid-sentence). Version 2
# (the stamp "legacy:2", every NEW document) keeps a break candidate only when the text
# before the "." is not one of the abbreviations below. ALREADY-IMPORTED documents carry the
# "legacy" stamp (or no stamp) and keep the version 1 split on export, so their stored
# segments still line up (see split_sentences, segments_per_unit).
#
# Each rule is (before, after, locales): `before` must END exactly at the "." (matched
# against at most _ABBREV_WINDOW chars of left context), `after` must match at the start of
# the text right after it, `locales` is a tuple of lower-case language prefixes or None for
# every source locale. This is the SRX no-break-rule shape, and app/resources/default.srx
# carries the same rules so the opt-in SRX path with the shipped ruleset splits identically
# (tests/verify_srx.py proves the two agree on its corpus).
#
# "etc." / "usw." / "ecc." are deliberately NOT here: they end real sentences ("..., cats,
# etc. Next we ...") and mid-sentence they are followed by a lowercase word, which never
# broke anyway. Words that also end sentences (Max., No., Nr., Jan., p.) only hold before a
# digit ("Max. 5", "No. 7", "p. 12"); "I met Max. He left." still splits.
# Segmentation-engine stamps for the built-in splitter (srx.py stamps the same strings).
LEGACY_ENGINE_V1 = "legacy"
LEGACY_ENGINE_V2 = "legacy:2"

# The longest "before" match is under 16 chars ("Messrs." / "d. w. z." plus the lookbehind
# char), so 64 chars of context on each side is plenty and bounds the per-candidate work.
_ABBREV_WINDOW = 64
_WB = r"(?<![\w.])"  # start of a word: not after a letter, digit or another dot
_ANY_AFTER = r"\s"
_DIGIT_AFTER = r"\s+[0-9]"
# Sentence openers that follow a capital letter + "." at a real sentence end ("Plan B. Then
# ...") but never follow an initial inside a name. English plus the common articles and
# pronouns of the per-language rule sets below.
_OPENERS = (
    r"(?:The|This|That|These|Those|Then|There|They|Their|We|Our|It|Its|In|If|Is|You|Your|"
    r"He|His|Her|She|My|And|But|So|Or|For|When|What|Why|How|Where|Who|Which|Please|Next|"
    r"Now|Also|However|After|Before|Once|To|On|At|By|With|As|An|All|Each|Some|Click|"
    r"Select|See|Note|Use|Do|Not|No|Yes|Let|Here|"
    r"Der|Die|Das|Den|Dem|Ein|Eine|Es|Er|Sie|Wir|Ich|Und|Aber|Dann|Bitte|"
    r"Le|Les|Il|Elle|Ils|Nous|Vous|Je|Ce|Cette|Puis|Ensuite|Et|Mais|"
    r"El|Los|Las|Una|Un|Ella|Luego|Pero|Os|Um|Uma|Ele|Gli|Lo|Poi|Ma|"
    r"Het|Een|Dit|Dat|Wij|Ik|Dan|Maar)\b"
    r"|[AI]\s"
)
_ABBREV_RULES: tuple[tuple[str, str, tuple[str, ...] | None], ...] = (
    # Every source locale: Latin abbreviations (any case; "ca." lower-case only, so "I live
    # in CA. Then" still splits) and English titles (capitalised only, so "500 ms. Then"
    # and "next-gen. The" still split; gate fix).
    (
        _WB + r"(?:(?i:e\.g|i\.e|incl|excl|ext|approx|appr|vs|cf|resp|esp|viz)|ca)\.",
        _ANY_AFTER,
        None,
    ),
    (
        _WB + r"(?:Mr|Mrs|Ms|Mx|Dr|Prof|Rev|Hon|Gen|Capt|Sgt|Lt|Messrs)\.",
        _ANY_AFTER,
        None,
    ),
    (
        _WB + r"(?i:no|nos|nr|max|min|fig|figs|vol|vols|pp|p|ch|sec|art|tel|ph|"
        r"jan|feb|mar|apr|jun|jul|aug|sep|sept|oct|nov|dec)\.",
        _DIGIT_AFTER,
        None,
    ),
    # Single-letter initials ("J. R. R. Tolkien", "John F. Kennedy", French "M. Dupont"):
    # an uppercase letter standing alone, then a capitalised word. "I" is left out ("So
    # did I. Then ..."). Gate fix: no hold when the next word is a common sentence opener
    # (article, pronoun, conjunction, "Then", "Please" ...), so "Plan B. Then we go." and
    # "Take vitamin C. The rest" still split; a name after an initial is not on the list.
    (_WB + r"[A-HJ-Z]\.", r"\s+(?!" + _OPENERS + r")[A-Z]", None),
    # German: z. B. / z.B., d. h., u. a., bzw., ggf., evtl., inkl., zzgl., vgl., bspw.,
    # sog., Hr./Fr.; Abs. / S. (Seite) before a number.
    (_WB + r"z\.", r"\s?B\.", ("de",)),
    (
        _WB + r"(?:(?i:z\.\s?B|d\.\s?h|u\.\s?a|bzw|ggf|evtl|inkl|zzgl|vgl|bspw|sog)|Hr|Fr)\.",
        _ANY_AFTER,
        ("de",),
    ),
    (_WB + r"(?i:abs|s)\.", _DIGIT_AFTER, ("de",)),
    # French: p. ex., c.-à-d., env. (environ), MM. (Messieurs, capitals only: "5 mm."
    # ends sentences), Mme/Mlle take no dot. Titles in every language are case-sensitive.
    (_WB + r"(?:(?i:p\.\s?ex|c\.-à-d|env)|MM)\.", _ANY_AFTER, ("fr",)),
    # Spanish / Portuguese: Sr., Sra., Srta., Dra., Ud., Uds., aprox., p. ej.; no break
    # inside "EE. UU." (but after it: it often ends a sentence); pág. / núm. before a number.
    (_WB + r"EE\.", r"\s?UU\.", ("es",)),
    (
        _WB + r"(?:Sr|Sra|Srta|Dra|Ud|Uds|(?i:aprox|p\.\s?ej))\.",
        _ANY_AFTER,
        ("es", "pt"),
    ),
    (_WB + r"(?i:pág|núm)\.", _DIGIT_AFTER, ("es", "pt")),
    # Italian: Sig., Dott., Ing., Avv.; pag. before a number.
    (_WB + r"(?:Sig|Dott|Ing|Avv)\.", _ANY_AFTER, ("it",)),
    (_WB + r"(?i:pag)\.", _DIGIT_AFTER, ("it",)),
    # Dutch: bijv., bv., o.a., d.w.z., i.p.v., m.b.t.; blz. before a number.
    (_WB + r"(?i:bijv|bv|o\.a|d\.w\.z|i\.p\.v|m\.b\.t)\.", _ANY_AFTER, ("nl",)),
    (_WB + r"(?i:blz)\.", _DIGIT_AFTER, ("nl",)),
)
_ABBREV_COMPILED = tuple(
    (re.compile("(?:" + before + r")\Z"), re.compile(after), locs)
    for before, after, locs in _ABBREV_RULES
)


def _abbrev_holds(text: str, pos: int, locale: str) -> bool:
    """Is the break candidate right after the "." at ``text[pos - 1]`` an abbreviation
    (version 2 rules)? ``pos`` is the boundary just after the punctuation."""
    lang = (locale or "").lower().replace("_", "-").split("-", 1)[0]
    left = text[max(0, pos - _ABBREV_WINDOW) : pos]
    right = text[pos : pos + _ABBREV_WINDOW]
    for before, after, locs in _ABBREV_COMPILED:
        if locs is not None and lang not in locs:
            continue
        if before.search(left) and after.match(right):
            return True
    return False


# Inline-formatting placeholder tokens (Phase 5 chunk 6 "inline-tag fidelity").
# The DOCX and XLIFF filters replace mid-paragraph run formatting / inline
# elements with these opaque tokens BEFORE the text reaches segmentation, MT, TM,
# and QA, then rebuild the real markup in build() from a filter-private side-channel
# (TextUnit.inline_map). The tokens must survive an LLM rewrite verbatim, so they
# use the mathematical white-bracket delimiters ⟦ ⟧ (U+27E6/U+27E7): a single
# codepoint each, essentially never present in real prose, containing no
# regex-special or sentence-ending characters. Grammar (numbered per segment,
# deterministic by source order, starting at 1):
#   ⟦x1⟧            self-closing / point token  (XLIFF <x>, <ph>)
#   ⟦g1⟧ … ⟦/g1⟧    paired open/close           (XLIFF <g>/<bpt..ept>, DOCX runs ⟦r1⟧…⟦/r1⟧)
_PLACEHOLDER_RE = re.compile(r"⟦/?[a-z]?\d+⟧")

# The two DELIMITER CHARACTERS themselves, independent of the token grammar above.
# `_PLACEHOLDER_RE` only ever matches a WELL-FORMED token, which is exactly right for
# counting/ordering real tokens — and exactly WRONG as a damage detector, because a
# model that INVENTS marker-ish output ("⟦r1{limit}⟧/r1", "⟦x1...", "⟦R1⟧") produces
# text the regex cannot see at all (GitHub #43, root cause C2: those three shapes were
# measured live, each landed as clean `mt` with zero qa_flags). These two constants let
# the write gate ask the separate, cruder question "did any bracket character survive
# that is not part of a real token?" — see `marker_residue`.
MARKER_OPEN, MARKER_CLOSE = "⟦", "⟧"
MARKER_CHARS = MARKER_OPEN + MARKER_CLOSE

_TAG_PATTERNS = [
    r"⟦/?[a-z]?\d+⟧",  # inline-fidelity placeholder tokens (see above)
    # Rails i18n %{name}: ONE token, so a dropped `%` is a tag divergence (YAML gate)
    r"%\{[A-Za-z_][A-Za-z0-9_]*\}",
    # Python named %(name)s / %(count)d / %(total).2f: ONE token (#689)
    r"%\([A-Za-z_][A-Za-z0-9_]*\)[-#+]*[0-9.]*[hlL]?[diouxXeEfFgGcrsa]",
    r"\{[^{}]+\}",  # {0}, {name}, ICU-ish {count, plural, ...}
    # printf: %s %d %1$s %.2f, Apple %@ %1$@. A conversion glued to following letters is
    # marketing copy ("20%OFF", "50%Rabatt"), not a placeholder (same rule as the write seam).
    r"%(?:\d+\$)?[-#+]*[0-9.]*(?:hh|ll|[hlLqzjt])?[a-zA-Z@](?![A-Za-z])",
    r"</?[a-zA-Z][^>]*>",  # <b>, </b>, <a href="...">
    r"https?://\S+",  # URLs
]
_TAG_RE = re.compile("|".join(_TAG_PATTERNS))


def est_tokens(text: str) -> int:
    """Rough token estimate (~4 chars/token). Good enough for chunk planning."""
    return max(1, math.ceil(len(text) / 4))


def _protected_spans(text: str) -> list[tuple[int, int]]:
    """Half-open [start, end) ranges covering paired inline-fidelity token spans
    (⟦gN⟧…⟦/gN⟧ / ⟦rN⟧…⟦/rN⟧). Segmentation must never break inside one of these:
    an open token in one segment and its close in another would orphan the pair,
    and export would rejoin the halves with a space around the wrong span.

    Matching is by the numeric id (kind-agnostic) so ⟦g1⟧…⟦/g1⟧ pairs correctly.
    Unmatched opens/closes are ignored (they protect nothing)."""
    opens: dict[str, int] = {}
    spans: list[tuple[int, int]] = []
    for m in _PLACEHOLDER_RE.finditer(text):
        inner = m.group(0)[1:-1]  # strip ⟦ ⟧
        if inner.startswith("/"):
            key = re.sub(r"^/[a-z]?", "", inner)
            start = opens.pop(key, None)
            if start is not None:
                spans.append((start, m.end()))
        else:
            key = re.sub(r"^[a-z]?", "", inner, count=1)
            opens[key] = m.start()
    return spans


def _in_protected_span(idx: int, spans: list[tuple[int, int]]) -> bool:
    return any(start < idx < end for start, end in spans)


def _srx_enabled() -> bool:
    """Is the opt-in SRX segmentation path turned on? Default OFF (the sacred
    gate). Import settings lazily so segment.py stays import-light for the many
    modules that only use its token/chunk helpers."""
    try:
        from app.services import settings

        return settings.get("srx_enabled") == "1"
    except Exception:
        return False


def _split_via_srx(text: str, locale: str, engine: str | None = None) -> list[str] | None:
    """Segment via the SRX engine, then re-merge any boundary that lands inside a
    paired inline-fidelity token span (⟦gN⟧…⟦/gN⟧) so a pair is never orphaned —
    the same invariant the legacy path enforces. Returns None on any failure so
    the caller can fall back to the legacy regex path.

    Offsets are EXACT (finding F3): the SRX engine returns contiguous (start,end)
    spans over the ORIGINAL text, so protected-span boundary tests and the
    re-merge use precise offsets instead of ``str.find`` on stripped pieces (a
    repeated substring could otherwise be located at the wrong offset and corrupt
    an inline-token-span merge).

    ``engine`` selects HOW to segment:
      * None  -> the live active ruleset (ingest-time path, gate already checked);
      * a stamped engine identity (e.g. "srx:ab12cd34ef56") -> reproduce that
        exact ingest-time segmentation (export path, finding F1). A stamped
        legacy/mismatched/oversized engine yields None so the caller uses legacy.
    """
    try:
        from app.services import srx

        if engine is None:
            # Live path: segment_text enforces the paragraph cap and active
            # ruleset. Recover exact spans via segment_spans on the active doc so
            # the offset-exact merge below has real offsets, not find()-ed ones.
            if len(text) > srx._MAX_SRX_PARAGRAPH:
                return None
            doc = srx.active_document()
            # A rule that exceeds the SRX match timeout falls back to the default
            # rules with a warning naming it (red-team finding 02), never silently.
            spans_pieces = srx.spans_with_default_fallback(doc, text, locale or "")
        else:
            # Export path: reproduce the stamped ingest-time segmentation exactly.
            spans_pieces = srx.segment_spans_for_engine(text, locale or "", engine)
            if spans_pieces is None:
                return None
    except Exception:
        return None
    if not spans_pieces:
        return None
    # Glue any boundary that lands inside a protected paired-token span (offsets are
    # exact and contiguous here) — the shared re-merge used by every SRX path.
    return _merge_protected(text, spans_pieces)


def _merge_protected(text: str, spans_pieces: list[tuple[int, int]]) -> list[str]:
    """Re-merge any SRX boundary that falls inside a paired inline-fidelity token
    span (⟦gN⟧…⟦/gN⟧) so a pair is never orphaned, then return stripped pieces.
    Shared by the live/stamped path (_split_via_srx) and the explicit-ruleset path
    (split_with_ruleset). All offsets are EXACT (finding F3)."""
    if not spans_pieces:
        return []
    prot = _protected_spans(text)
    merged_spans: list[tuple[int, int]] = [spans_pieces[0]]
    for s, e in spans_pieces[1:]:
        boundary = merged_spans[-1][1]
        if prot and _in_protected_span(boundary, prot):
            merged_spans[-1] = (merged_spans[-1][0], e)
        else:
            merged_spans.append((s, e))
    out = [text[a:b].strip() for a, b in merged_spans]
    out = [p for p in out if p]
    return out or [text]


def split_with_ruleset(text: str, locale: str, ruleset: bytes) -> list[str] | None:
    """Segment ``text`` with an EXPLICIT SRX ruleset (P6): a resolved
    SegmentationProfile's blob drives ingest instead of the globally-active
    ruleset. Honors the same protected-paired-token invariant as every other path.
    Returns None on any failure (invalid ruleset / oversized paragraph) so the
    caller falls back to the legacy splitter. The ingest stamp is computed
    separately by srx.engine_identity_for_bytes(ruleset)."""
    text = (text or "").strip()
    if not text:
        return []
    try:
        from app.services import srx

        if len(text) > srx._MAX_SRX_PARAGRAPH:
            return None
        doc = srx.document_for_bytes(ruleset)
        spans_pieces = srx.spans_with_default_fallback(doc, text, locale or "")
    except Exception:
        return None
    if not spans_pieces:
        return None
    return _merge_protected(text, spans_pieces)


def split_sentences(text: str, locale: str = "", engine: str | None = None) -> list[str]:
    """Split a single paragraph into sentence segments.

    A split candidate that falls inside a paired inline-fidelity token span
    (⟦gN⟧…⟦/gN⟧) is suppressed so the pair is never orphaned across segments.

    SRX path (opt-in): when the ``srx_enabled`` setting is "1", segmentation is
    delegated to the SRX rule engine (``app.services.srx``) using ``locale`` as
    the source locale for maprule selection. The DEFAULT ("0") keeps this legacy
    regex path byte-unchanged — the sacred gate. Protected paired-token spans are
    honored on BOTH paths (SRX cut points inside a ⟦gN⟧…⟦/gN⟧ span are dropped).

    ``engine`` pins the segmentation to a STAMPED ingest-time identity (finding
    F1). Export passes the ``seg_engine`` stamped in the skeleton so it reproduces
    the ingest-time split EXACTLY, independent of the live ``srx_enabled`` gate or
    a swapped ruleset:
      * None                 -> live behavior (gate + active ruleset). Ingest path.
      * "legacy"             -> force the legacy splitter, version 1 (no abbreviation
                                rules; documents imported before decision 15).
      * "legacy:2"           -> the legacy splitter, version 2 (abbreviation rules;
                                what the live path stamps with the SRX gate off).
      * "srx:<hash>"         -> reproduce that ruleset's SRX split, or fall back to
                                legacy (with a logged warning) if it is gone.
    Callers that did not stamp an engine (non-export callers) pass None and get the
    live behavior; export maps a pre-F1 document's missing stamp to version 1 itself
    (see ``_stamped_engine``)."""
    text = text.strip()
    if not text:
        return []
    # Which version of the built-in splitter runs below: 2 (abbreviation rules) for the
    # live path with the SRX gate off and for a "legacy:2" stamp; 1 for a "legacy" stamp
    # (documents imported before decision 15) and for every SRX fallback, which has
    # always been version 1 and must stay so for SRX-stamped documents to re-split alike.
    version = 1
    if engine is not None:
        # Pinned path: honor the stamp regardless of the live gate.
        if engine == LEGACY_ENGINE_V2:
            version = 2
        elif engine != LEGACY_ENGINE_V1:
            srx_out = _split_via_srx(text, locale, engine=engine)
            if srx_out is not None:
                return srx_out
        # "legacy" stamp, or SRX reproduction unavailable -> legacy split below.
    elif _srx_enabled():
        srx_out = _split_via_srx(text, locale)
        if srx_out is not None:
            return srx_out
    else:
        version = 2
    spans = _protected_spans(text)
    # Collect candidate cut points from both splitters, then keep only those that
    # do not fall inside a protected paired-token span.
    cuts = {0, len(text)}
    for rx in (_SENT_SPLIT, _CJK_SPLIT):
        for m in rx.finditer(text):
            # Version 2: no break after an abbreviation ("Max. 5", "incl. VAT").
            # _SENT_SPLIT's match is the whitespace run, so start() sits right after the
            # punctuation; _CJK_SPLIT is zero-width after a CJK stop (never an abbreviation).
            if version == 2 and rx is _SENT_SPLIT and _abbrev_holds(text, m.start(), locale):
                continue
            # cut AFTER the match (both patterns are zero-width or trailing-ws
            # boundaries; end() is where the next sentence begins)
            cuts.add(m.end())
    cuts = sorted(i for i in cuts if not _in_protected_span(i, spans))
    out: list[str] = []
    for a, b in zip(cuts, cuts[1:], strict=False):  # noqa: RUF007
        piece = text[a:b].strip()
        if piece:
            out.append(piece)
    return out or [text]


# ---------------------------------------------------------------------------------------
# Rejoining a unit's segments on export (GitHub #668, #671).
#
# Segments are stored TRIMMED, so the whitespace a unit had around and between its
# sentences lives only in the skeleton unit text. `unit_layout` recovers it with the same
# stamped split ingest ran; `join_unit` rebuilds the unit from (possibly translated)
# pieces: the unit's own leading/trailing whitespace is kept, and each seam takes the
# source separator unless the characters at the seam say otherwise (no space after a
# full-width sentence end or between two CJK characters; a space where a no-space source
# seam now joins two spaced-script sentences, e.g. ja -> en).

# Full-width / CJK punctuation after which no space belongs (sentence ends, closing
# brackets and quotes, the ideographic comma and the CJK ellipsis forms).
_NO_SPACE_AFTER = frozenset("。．！？、，；：」』）】〕〉》｝］〗〙〛…‥｡､")

_CJK_RANGES = (
    (0x2E80, 0x2FDF),  # CJK radicals, Kangxi radicals
    (0x3000, 0x303F),  # CJK symbols and punctuation
    (0x3040, 0x30FF),  # Hiragana, Katakana
    (0x3100, 0x312F),  # Bopomofo
    (0x31A0, 0x31FF),  # Bopomofo ext, CJK strokes, Katakana phonetic ext
    (0x3400, 0x4DBF),  # CJK ext A
    (0x4E00, 0x9FFF),  # CJK unified ideographs
    (0xF900, 0xFAFF),  # CJK compatibility ideographs
    (0xFF00, 0xFFEF),  # half/full-width forms
    (0x20000, 0x3134F),  # CJK ext B..G
)
# Scripts written without spaces between words or sentences, beyond CJK: Thai, Lao,
# Tibetan, Myanmar, Khmer. A seam the SOURCE wrote without whitespace stays without it
# when both sides are in one of these scripts (Thai prose that does put a space between
# sentences keeps that space: it is the source separator).
_NO_SPACE_SCRIPT_RANGES = (
    (0x0E00, 0x0EFF),  # Thai, Lao
    (0x0F00, 0x0FFF),  # Tibetan
    (0x1000, 0x109F),  # Myanmar
    (0x1780, 0x17FF),  # Khmer
)


def _in_ranges(ch: str, ranges) -> bool:
    if not ch:
        return False
    cp = ord(ch)
    return any(a <= cp <= b for a, b in ranges)


def _is_cjk(ch: str) -> bool:
    return _in_ranges(ch, _CJK_RANGES)


def seam_separator(
    src_sep: str, left: str, right: str, src_left: str | None = None, src_right: str | None = None
) -> str:
    """The whitespace to put between two rejoined segments.

    `src_sep` is what sat between the two SOURCE sentences (possibly ""); `left`/`right`
    are the (translated) texts on either side, `src_left`/`src_right` the source
    sentences when known. The source separator is kept verbatim when it holds a line
    break (line structure) or when the characters at the seam are the source's own (an
    untranslated or held export is byte-identical, whatever mix of scripts the author
    wrote). Otherwise: no space after a full-width sentence end or between two CJK
    characters (en -> ja/zh); a source seam with no whitespace keeps none between two
    no-space-script characters, and gets one space between anything else (ja -> en)."""
    if "\n" in src_sep or "\r" in src_sep:
        return src_sep
    lc = (left or "").rstrip()[-1:]
    rc = (right or "").lstrip()[:1]
    if not lc or not rc:
        return src_sep
    if src_left is not None and src_right is not None:  # noqa: SIM102
        if lc == src_left.rstrip()[-1:] and rc == src_right.lstrip()[:1]:
            return src_sep
    if lc in _NO_SPACE_AFTER or (_is_cjk(lc) and _is_cjk(rc)):
        return ""
    if src_sep:
        return src_sep
    no_space = _CJK_RANGES + _NO_SPACE_SCRIPT_RANGES
    if _in_ranges(lc, no_space) and _in_ranges(rc, no_space):
        return ""
    return " "


def unit_layout(text: str, locale: str = "", engine: str | None = None) -> dict:
    """The whitespace skeleton of one unit under the stamped split: `lead`/`trail` (the
    unit's own edge whitespace) and `seps` (what sat between consecutive sentences, one
    per seam). `pieces` is exactly `split_sentences(text, locale, engine)`."""
    text = text or ""
    pieces = split_sentences(text, locale, engine=engine)
    core = text.strip()
    if not core:
        return {"pieces": pieces, "lead": "", "seps": [], "trail": ""}
    lead = text[: len(text) - len(text.lstrip())]
    trail = text[len(text.rstrip()) :]
    seps: list[str] = []
    cursor = 0
    for n, piece in enumerate(pieces):
        at = core.find(piece, cursor)
        if n:
            gap = core[cursor:at] if at >= 0 else " "
            # Pieces are contiguous stripped slices, so the gap is whitespace; anything
            # else means the split did not come from this text - fall back to a space.
            seps.append(gap if not gap.strip() else " ")
        if at >= 0:
            cursor = at + len(piece)
    return {"pieces": pieces, "lead": lead, "seps": seps, "trail": trail}


def join_unit(parts: list[str], layout: dict | None, positions: list[int] | None = None) -> str:
    """Rebuild a unit from its segment texts `parts` with the unit's `layout`.

    `positions` names each part's sentence index in the source split when some members
    were dropped (an approved-empty delivery); the seam before a kept part is the source
    separator that preceded its sentence. The unit's leading/trailing whitespace wraps a
    non-empty result only, so an empty delivery stays exactly ""."""
    layout = layout or {}
    seps = layout.get("seps") or []
    src = layout.get("pieces") or []
    if positions is None:
        positions = list(range(len(parts)))
    out = ""
    for k, part in enumerate(parts):
        part = part or ""
        if k:
            pos = positions[k]
            src_sep = seps[pos - 1] if 0 < pos <= len(seps) else " "
            prev = positions[k - 1]
            src_left = src[prev] if 0 <= prev < len(src) else None
            src_right = src[pos] if 0 <= pos < len(src) else None
            out += seam_separator(src_sep, out, part, src_left, src_right) + part
        else:
            out = part
    if not out.strip():
        return out
    return (layout.get("lead") or "") + out + (layout.get("trail") or "")


def _stamped_engine(skel: dict) -> str | None:
    """The segmentation engine to re-split a stored document with. A document ingested
    before the F1 stamp has no ``seg_engine``: it was split by whatever was live then,
    which with the SRX gate off was the version 1 splitter, so it gets "legacy" (the live
    path is version 2 now, and would move its segment boundaries). With the gate on it
    keeps the historical live SRX behaviour (None)."""
    seg_engine = skel.get("seg_engine")
    if seg_engine is None and not _srx_enabled():
        return LEGACY_ENGINE_V1
    return seg_engine


def unit_layouts(doc) -> list[dict]:
    """`unit_layout` for every skeleton unit of `doc`, under the SAME stamped
    segmentation identity as `segments_per_unit` (seg_locale + seg_engine + replayed
    SRX fallbacks)."""
    skel = doc.skeleton or {}
    seg_locale = skel.get("seg_locale", "")
    seg_engine = _stamped_engine(skel)
    from app.services import srx

    with srx.replay_fallbacks(skel.get("srx_fallback") or ()):
        return [unit_layout(u["text"], seg_locale, seg_engine) for u in skel.get("units", [])]


def segments_per_unit(doc) -> list[int]:
    """How many Segments each skeleton unit produced: re-split every unit's source
    with the SAME stamped segmentation identity (seg_locale + seg_engine) that ran
    at ingest, so the count aligns stored target segments back to their paragraph
    regardless of a later srx_enabled toggle or a swapped ruleset (finding F1;
    seg_engine absent on pre-F1 docs -> None reproduces historical live behavior).
    Shared by export.paragraph_texts_for and image_localization._segments_per_unit."""
    skel = doc.skeleton or {}
    seg_locale = skel.get("seg_locale", "")
    seg_engine = _stamped_engine(skel)
    from app.services import srx

    # #511 gate: reuse ingest's per-paragraph default-rules fallback decisions so a
    # wall-clock budget can never split a paragraph differently here than at ingest.
    # #675: EXACTLY what ingest wrote -- 0 for a whitespace-only unit (split_sentences
    # strips and returns []); a `max(1, ...)` here shifted every later unit by one.
    with srx.replay_fallbacks(skel.get("srx_fallback") or ()):
        return [
            len(split_sentences(u["text"], seg_locale, engine=seg_engine))
            for u in skel.get("units", [])
        ]


def extract_inline_tags(text: str) -> list[str]:
    """Return the protected inline spans found in a segment, in order."""
    return _TAG_RE.findall(text)


# --- inline-fidelity placeholder tokens ---------------------------------------
def ph_open(kind: str, n: int) -> str:
    """Paired opening token, e.g. ph_open('g', 1) -> '⟦g1⟧'."""
    return f"⟦{kind}{n}⟧"


def ph_close(kind: str, n: int) -> str:
    """Paired closing token, e.g. ph_close('g', 1) -> '⟦/g1⟧'."""
    return f"⟦/{kind}{n}⟧"


def ph_point(kind: str, n: int) -> str:
    """Self-closing / point token, e.g. ph_point('x', 1) -> '⟦x1⟧'."""
    return f"⟦{kind}{n}⟧"


# #693 (2026-10-11, decision 10): a point token whose original NAMES its argument
# (Apple / Android `%1$@` `%2$s`, Windows `%1`, `{0}` / `{name}`, Python `%(name)s`, an
# XLIFF `<x id>` / `<ph id>`) may move freely in the translation: build maps the token back
# by its number, so the target's order is safe, and target languages need a different word
# order. Such tokens use the kind letter `p` (`⟦p1⟧`), every other point token keeps `x`.
# The tag gate (`quality_common.tag_damage`) and the editor badge (`qa.check_segment`)
# judge ORDER only over the other tokens; the multiset (drop / add / duplicate) is judged
# over all of them. Unnumbered `%@` / `%s` keep `x`: their order IS the argument order.
POINT_KINDS = frozenset("xp")
FREE_POINT_KIND = "p"


def ph_free(n: int) -> str:
    """A point token that may move freely in the target, e.g. ph_free(1) -> '⟦p1⟧'."""
    return f"⟦{FREE_POINT_KIND}{n}⟧"


def is_point_kind(kind: str) -> bool:
    """True for the kind letter of a self-closing point token (`x` or `p`)."""
    return kind in POINT_KINDS


def is_free_token(tok: str) -> bool:
    """True for a free-order point token (`⟦pN⟧`)."""
    return tok[1:2] == FREE_POINT_KIND


def order_significant(tokens: list[str]) -> list[str]:
    """`tokens` without the free-order point tokens: the sequence whose order counts."""
    return [t for t in tokens if not is_free_token(t)]


def same_token_order(source_tokens: list[str], target_tokens: list[str]) -> bool:
    """True when the target carries the source's tokens in an acceptable order: the same
    multiset, and the same sequence once free-order tokens (`⟦pN⟧`) are set aside."""
    if source_tokens == target_tokens:
        return True
    from collections import Counter

    return Counter(source_tokens) == Counter(target_tokens) and order_significant(
        source_tokens
    ) == order_significant(target_tokens)


def find_placeholders(text: str) -> list[str]:
    """All inline-fidelity placeholder tokens in a string, in document order."""
    return _PLACEHOLDER_RE.findall(text or "")


def strip_placeholders(text: str) -> str:
    """Remove inline-fidelity placeholder tokens (used for TM canonicalization so a
    tokenized segment still matches its tagless TM twin)."""
    return _PLACEHOLDER_RE.sub("", text or "")


def has_marker_chars(text: str | None) -> bool:
    """True when `text` contains EITHER bracket delimiter, well-formed or not.

    The cheap source-side abstention for the residue check: a source that already
    carries bracket characters can produce marker-ish target output for legitimate
    reasons we did not cause, so `marker_residue` is only ever consulted against a
    source with none (GitHub #43)."""
    return any(c in (text or "") for c in MARKER_CHARS)


def marker_residue(text: str | None) -> str:
    """The bracket characters left in `text` once every WELL-FORMED token is removed —
    "" when the text carries only real tokens (or none at all).

    This is deliberately CRUDER than the token grammar: it answers "is there marker-
    shaped debris here?", not "which tokens are these?". A model that was shown the
    ⟦⟧ notation and imitated it badly emits `⟦r1{limit}⟧/r1`, `⟦x1...` or `⟦R1⟧` —
    strings `find_placeholders` reports as ZERO tokens, so every multiset/order/nesting
    check upstream reads them as a perfectly clean target (GitHub #43)."""
    return "".join(c for c in _PLACEHOLDER_RE.sub("", text or "") if c in MARKER_CHARS)


def paired_span_bodies(text: str | None) -> dict[str, str]:
    """{token-name -> inner text} for every WELL-FORMED `⟦kN⟧…⟦/kN⟧` pair in `text`,
    with nested placeholder tokens stripped from the body. Point tokens (`⟦xN⟧`) and
    unmatched opens/closes are omitted. Called by `quality.tag_damage` ONLY — every other surface (the write gate, the
    export guard) reaches this rung THROUGH that one function, so DOCX runs, XLIFF
    `<g>` and HTML inline elements are judged identically with no second caller
    re-deriving the comparison.

    Exists because token identity, order and nesting are ALL preserved by a target that
    simply moves the words OUT of a pair (`⟦r2⟧Acme Pro⟦/r2⟧` -> `⟦r2⟧⟦/r2⟧`) — and an
    empty span is dropped at build time (`docx_filter._set_paragraph_text`), so the run
    and its rPr snapshot evaporate and the neighbour absorbs the words with the
    NEIGHBOUR's formatting. The body is what makes that visible.

    Keys are the OPEN token's inner text (`"r2"`, `"g1"`) so different kinds never
    collide, while matching mirrors `placeholders_well_formed` (numeric id, kind-
    agnostic). Ids repeat across paragraphs of a FULL_TEXT blob (each paragraph
    renumbers from 1), so a repeated name keeps only its LAST body — deliberately
    conservative: it can MISS an emptied duplicate, never invent one. That blind spot matters only for FULL_TEXT (whose ids collide across paragraphs);
    the FULL_TEXT export keeps its own 1.6 whole-body guard, which is unchanged here."""  # noqa: E501
    stack: list[tuple[str, str, int]] = []  # (numeric id, open-token name, body start)
    out: dict[str, str] = {}
    s = text or ""
    for m in _PLACEHOLDER_RE.finditer(s):
        inner = m.group(0)[1:-1]  # strip ⟦ ⟧
        if inner.startswith("/"):
            key = re.sub(r"^/[a-z]?", "", inner)
            if not stack or stack[-1][0] != key:
                continue  # close with no open, or crossed pair: omitted
            _, name, start = stack.pop()
            out[name] = strip_placeholders(s[start : m.start()])
        else:
            kind = re.match(r"^([a-z]?)", inner).group(1)
            if is_point_kind(kind):
                continue  # point token: self-closing, nests nothing
            stack.append((re.sub(r"^[a-z]?", "", inner, count=1), inner, m.end()))
    return out


def placeholders_well_formed(text: str | None) -> bool:
    """True when the paired inline-fidelity tokens in `text` are PROPERLY NESTED:
    every close matches the most-recently-opened still-open id, no close appears
    without a matching open, and no open is left unclosed (AI Tagging 1.8 — the one
    check `[COMMERCIAL]` flags as missing everywhere: balanced open/close, no
    crossed pairs). Point tokens (`⟦xN⟧`, self-closing) are ignored — they nest
    nothing. Matching is by numeric id, kind-agnostic (mirrors `_protected_spans`),
    so `⟦g1⟧…⟦/g1⟧` and `⟦r1⟧…⟦/r1⟧` both balance.

    A crossed pair (`⟦g1⟧⟦g2⟧⟦/g1⟧⟦/g2⟧`) returns False — when `⟦/g1⟧` is seen the
    top of the stack is `g2`, so the nesting is invalid. A well-formed sequence,
    sequential runs (`⟦r1⟧…⟦/r1⟧⟦r2⟧…⟦/r2⟧`), or no tokens at all return True. The
    filters only ever EMIT well-formed nesting, so a malformed target is model (or
    human) damage, never a legitimate source shape."""
    stack: list[str] = []
    for m in _PLACEHOLDER_RE.finditer(text or ""):
        inner = m.group(0)[1:-1]  # strip ⟦ ⟧
        if inner.startswith("/"):
            key = re.sub(r"^/[a-z]?", "", inner)
            if not stack or stack[-1] != key:
                return False  # close with no open, or crossed pairs
            stack.pop()
        else:
            # A point token ⟦xN⟧ has kind 'x' and never pairs; treat any token that
            # never gets a matching close as a point token by NOT requiring closure
            # for the 'x' kind. Distinguish by kind letter: 'x' = point, else paired.
            kind = re.match(r"^([a-z]?)", inner).group(1)
            if is_point_kind(kind):
                continue
            key = re.sub(r"^[a-z]?", "", inner, count=1)
            stack.append(key)
    return not stack


def plan_chunks(unit_texts: list[str], max_tokens: int) -> list[dict]:
    """Group consecutive paragraph texts into request-sized chunks.

    A single paragraph larger than the budget becomes its own oversized chunk
    (the orchestrator will sub-split it later). Returns a list of:
        {"index", "unit_count", "est_tokens", "oversized"}
    """
    chunks: list[dict] = []
    cur_count = 0
    cur_tokens = 0

    def flush():
        nonlocal cur_count, cur_tokens
        if cur_count:
            chunks.append(
                {
                    "index": len(chunks),
                    "unit_count": cur_count,
                    "est_tokens": cur_tokens,
                    "oversized": False,
                }
            )
            cur_count = 0
            cur_tokens = 0

    for t in unit_texts:
        tok = est_tokens(t)
        if tok > max_tokens:
            flush()
            chunks.append(
                {
                    "index": len(chunks),
                    "unit_count": 1,
                    "est_tokens": tok,
                    "oversized": True,
                }
            )
            continue
        if cur_count and cur_tokens + tok > max_tokens:
            flush()
        cur_count += 1
        cur_tokens += tok

    flush()
    return chunks
