"""Image filter — SVG text-node swap + raster region detection/overlay build.

SPEC-image-localization Phase 1. One filter registers all four image extensions
(svg / png / jpg / jpeg) and dispatches on the actual BYTES (magic sniff, never
the extension):

SVG (the trivial full-pipeline slice)
    parse(): tokenize the raw XML exactly like html_filter (split on tags, text
    between tags), but only text INSIDE a <text>/<tspan>/<textPath> container is
    translatable — everything else (paths, styles, defs, attributes, comments)
    is left byte-for-byte identical, so build() achieves FULL round-trip
    fidelity for non-text content by construction. Entities are unescaped into
    the unit text and re-escaped on build. build_localized() into a right-to-left
    target sets direction="rtl" on every translated <text> and mirrors its
    text-anchor (start <-> end) so labels keep their side of the anchor point
    (_svg_rtl; B-rtl decision 20, a deliberate layout change).
    Accessibility text (decision 8, 2026-10-11): the plain text of every <title>
    and <desc> element (the name and description a screen reader announces) is a
    unit too, styled a11y:svg-title / a11y:svg-desc and APPENDED after all the
    <text> units (the a11y convention), so an SVG imported before keeps its unit
    indices. A <title>/<desc> with child elements, or inside <style>, <script> or
    <metadata>, is left alone.

RASTER (png/jpg/jpeg; tif/tiff/bmp/gif/webp since file-format Batch 16, single frame only)
    parse(): region detection through the pluggable detector seam
    (services/image_localization.detect_regions — mock / winocr / rapidocr,
    geometry contract per research 02) + optional VLM recognition escalation
    for low-confidence regions (text only, never geometry). One TextUnit per
    region; the full geometry contract is stashed BOTH in the skeleton
    ("regions") and in each TextUnit.inline_map (filter-private channel).
    build(): OVERLAY mode (D4) — border-ring flat-fill erase + fit-to-box
    typesetting via image_localization.compose_overlay. build() re-detects on
    the SAME original bytes (the html/docx re-derive pattern); the content-hash
    detection cache makes parse/build see identical regions.

Nothing downstream inspects Document.fmt, so every region becomes an ordinary
Segment: TM / glossary / refusal / LQA / workflows apply unchanged (research 04).
"""

import re
from xml.sax.saxutils import escape as _xml_escape

from app.filters.base import (
    Filter,
    InputLimitError,
    ParseResult,
    TextUnit,
    check_segment_cap,
    decode_text_lossless,
    encode_text_lossless,
    note_source_encoding,
    read_text_upload,
    register,
)

# The historical tag pattern. It is kept as the definition of what a tag is, but it is
# never run over a whole document: `_split_tags` below gives the same result in one pass.
_TOKEN_RE = re.compile(r"(<[^>]+>)", re.S)


def _split_tags(text: str) -> list[str]:
    """Exactly `_TOKEN_RE.split(text)`, in linear time (security review 2026-09, #271).

    The regex retried from every `<` and scanned to the end of the text when no `>`
    followed, so text with many `<` and no `>` was quadratic (80 KB took 2.95 s, a 1 MB
    SVG minutes). A tag is still `<` up to the FIRST `>` after it with at least one
    character between; `<>` is not a tag; once no `>` remains, nothing after is a tag."""
    out: list[str] = []
    pos = i = 0
    while True:
        lt = text.find("<", i)
        if lt == -1:
            break
        gt = text.find(">", lt + 1)
        if gt == -1:
            break
        if gt == lt + 1:  # "<>" is not a tag; the scan resumes at the ">"
            i = gt
            continue
        out.append(text[pos:lt])
        out.append(text[lt : gt + 1])
        pos = i = gt + 1
    out.append(text[pos:])
    return out


_TAG_NAME_RE = re.compile(r"[a-zA-Z0-9:_-]+")

# SVG containers whose character data is translatable text. Everything else
# (path data, style sheets, scripts, metadata) is opaque and untouched.
_TEXT_CONTAINERS = frozenset({"text", "tspan", "textpath"})
# Containers whose character data must NEVER be treated as text even when
# nested oddly.
_SKIP_CONTAINERS = frozenset({"style", "script", "defs", "metadata", "title", "desc"})


def _parse_tag(tok: str):
    if not tok.startswith("<"):
        return False, "", False
    head = tok[1:].lstrip()
    closing = head.startswith("/")
    if closing:
        head = head[1:].lstrip()
    m = _TAG_NAME_RE.match(head)
    if not m:
        return False, "", False
    name = m.group(0).lower()
    self_closing = tok.rstrip(">").rstrip().endswith("/")
    return closing, name, self_closing


_XML_PREDEFINED = {"amp": "&", "lt": "<", "gt": ">", "quot": '"', "apos": "'"}
_ENTITY_RE = re.compile(r"&(?:#([0-9]{1,7})|#[xX]([0-9a-fA-F]{1,6})|(amp|lt|gt|quot|apos));")


def _xml_unescape(text: str) -> str:
    """Decode the five predefined XML entities AND numeric character references
    (#509: saxutils.unescape only knew &amp; &lt; &gt;, so `&#169;` and `&quot;` were
    re-escaped to `&amp;#169;` on build). Any other `&name;` is left as typed; it is
    re-escaped, never expanded (no DOCTYPE entity is ever resolved here)."""

    def _sub(m: re.Match) -> str:
        if m.group(3):
            return _XML_PREDEFINED[m.group(3)]
        cp = int(m.group(1), 10) if m.group(1) else int(m.group(2), 16)
        if (
            cp in (0x9, 0xA, 0xD)
            or 0x20 <= cp <= 0xD7FF
            or 0xE000 <= cp <= 0xFFFD
            or 0x10000 <= cp <= 0x10FFFF
        ):
            return chr(cp)
        return m.group(0)

    return _ENTITY_RE.sub(_sub, text)


def _svg_units(tokens: list[str], capped: bool = False) -> list[dict]:
    """One dict per translatable text node, in document order:
    {"ti": token index, "lead": ws, "core": unescaped text, "trail": ws}.
    parse() and build() both call this on the SAME bytes so indices align
    (the html_filter contract). `capped` (parse only, #571 gate): refuse as soon as the
    unit count passes MAX_DOCUMENT_SEGMENTS; build never passes it."""
    units: list[dict] = []
    text_depth = 0
    skip_depth = 0
    for ti, tok in enumerate(tokens):
        if tok.startswith("<") and tok.endswith(">"):
            closing, name, self_closing = _parse_tag(tok)
            if name in _SKIP_CONTAINERS:
                if closing:
                    skip_depth = max(0, skip_depth - 1)
                elif not self_closing:
                    skip_depth += 1
                continue
            if name in _TEXT_CONTAINERS and not self_closing:
                text_depth = max(0, text_depth - 1) if closing else text_depth + 1
            continue
        if text_depth <= 0 or skip_depth > 0 or not tok:
            continue
        core = tok.strip()
        if not core or not any(ch.isalnum() for ch in core):
            continue
        lead = tok[: len(tok) - len(tok.lstrip())]
        trail = tok[len(tok.rstrip()) :]
        units.append({"ti": ti, "lead": lead, "core": _xml_unescape(core), "trail": trail})
        if capped:
            check_segment_cap(len(units))
    return units


_A11Y_CONTAINERS = {"title": "a11y:svg-title", "desc": "a11y:svg-desc"}
_A11Y_OPAQUE = frozenset({"style", "script", "metadata"})


def _svg_a11y_units(tokens: list[str], capped: bool = False, already: int = 0) -> list[dict]:
    """One dict per translatable <title> / <desc> (decision 8), in document order:
    {"ti", "lead", "core", "trail", "style"}. Only an element whose whole content is ONE
    text token counts (`<title>Chart</title>`); parse() and build() call this on the SAME
    tokens, so the appended indices align. `already` = the <text> units counted before, for
    the parse-side segment cap."""
    units: list[dict] = []
    opaque = 0
    n = len(tokens)
    for ti, tok in enumerate(tokens):
        if not (tok.startswith("<") and tok.endswith(">")):
            continue
        closing, name, self_closing = _parse_tag(tok)
        if name in _A11Y_OPAQUE and not self_closing:
            opaque = max(0, opaque - 1) if closing else opaque + 1
            continue
        if opaque or closing or self_closing or name not in _A11Y_CONTAINERS:
            continue
        if ti + 2 >= n:
            continue
        text, end = tokens[ti + 1], tokens[ti + 2]
        if text.startswith("<") and text.endswith(">"):
            continue
        end_closing, end_name, _sc = _parse_tag(end)
        if not (end_closing and end_name == name):
            continue  # nested markup inside the title / desc: left alone
        core = text.strip()
        if not core or not any(ch.isalnum() for ch in core):
            continue
        lead = text[: len(text) - len(text.lstrip())]
        trail = text[len(text.rstrip()) :]
        units.append(
            {
                "ti": ti + 1,
                "lead": lead,
                "core": _xml_unescape(core),
                "trail": trail,
                "style": _A11Y_CONTAINERS[name],
            }
        )
        if capped:
            check_segment_cap(already + len(units))
    return units


# One attribute of a start tag, matched in order from the element name, so text inside
# another attribute's quoted value (aria-label="set lang=en") is never read as an attribute.
_ATTR_RE = re.compile(r"""\s+([^\s=/>"']+)(?:\s*=\s*("[^"]*"|'[^']*'|[^\s>"']+))?""")


def _svg_attr_spans(tok: str) -> dict[str, tuple[int, int]]:
    """{attribute name: (start, end) of its value} for a start tag (first occurrence)."""
    m = _TAG_NAME_RE.search(tok)
    spans: dict[str, tuple[int, int]] = {}
    pos = m.end() if m else len(tok)
    while (a := _ATTR_RE.match(tok, pos)) is not None:
        if a.group(2) is not None:
            spans.setdefault(a.group(1), a.span(2))
        pos = a.end()
    return spans


def _set_svg_attr(tok: str, attr: str, value: str, add_if_missing: bool) -> str:
    """Set `attr` (any quoting) on a start tag, or insert it after the element name."""
    span = _svg_attr_spans(tok).get(attr)
    if span is not None:
        return tok[: span[0]] + f'"{value}"' + tok[span[1] :]
    if not add_if_missing:
        return tok
    m = _TAG_NAME_RE.search(tok)
    if m is None:
        return tok
    return tok[: m.end()] + f' {attr}="{value}"' + tok[m.end() :]


def _declare_svg_language(tokens: list[str], tag: str) -> None:
    """#691: the ROOT svg element declares the target: its `xml:lang` and a plain `lang`
    when present; a root with neither gets `xml:lang`. `tag` is a validated BCP 47 tag
    (a11y.target_tag). Nested xml:lang (a deliberately foreign label) is left alone."""
    for ti, tok in enumerate(tokens):
        closing, name, _self_closing = _parse_tag(tok)
        if not name or closing:
            continue  # text, <?xml ?>, <!DOCTYPE>, comments
        if name != "svg":
            return  # the first element is not an svg root: nothing honest to stamp
        attrs = _svg_attr_spans(tok)
        has_xml = "xml:lang" in attrs
        has_plain = "lang" in attrs
        if has_xml or not has_plain:
            tok = _set_svg_attr(tok, "xml:lang", tag, add_if_missing=True)
        if has_plain:
            tok = _set_svg_attr(tok, "lang", tag, add_if_missing=False)
        tokens[ti] = tok
        return


# Right-to-left targets (B-rtl, decision 20). SVG 1.1 10.7.4 / SVG 2 11.10.1: `direction`
# sets the base direction of a text chunk, and text-anchor "start" / "end" are measured in
# that direction, so a left-anchored label (start, the initial value) with direction="rtl"
# would grow LEFTWARD from its x and leave the drawing. The anchor is therefore mirrored
# (start <-> end, middle kept): the label keeps its side of the anchor point and reads right
# to left. Values set in a <style> sheet or by a CSS class are not seen (only attributes and
# inline style on the element and its ancestors).
_ANCHOR_SWAP = {"start": "end", "end": "start"}
_STYLE_PROP_RE = r"(?:^|(?<=[;\s])){prop}\s*:\s*([^;!]*?)\s*(?=[;!]|$)"


def _style_prop_span(tok: str, prop: str):
    """(start, end) of `prop`'s value inside the start tag's inline style, or None."""
    span = _svg_attr_spans(tok).get("style")
    if span is None:
        return None
    start, end = span
    if tok[start : start + 1] in ("'", '"'):
        start, end = start + 1, end - 1
    m = re.compile(_STYLE_PROP_RE.format(prop=re.escape(prop)), re.I).search(tok[start:end])
    if m is None or not m.group(1):
        return None
    return (start + m.start(1), start + m.end(1))


def _own_prop(tok: str, prop: str) -> tuple[str | None, str]:
    """(value, where) of a presentation property on this start tag: inline style wins over
    the attribute (CSS cascade); where is "style", "attr" or "" (not set)."""
    span = _style_prop_span(tok, prop)
    if span is not None:
        return tok[span[0] : span[1]].strip().lower(), "style"
    aspan = _svg_attr_spans(tok).get(prop)
    if aspan is not None:
        return tok[aspan[0] : aspan[1]].strip("\"'").strip().lower(), "attr"
    return None, ""


def _set_prop(tok: str, prop: str, value: str) -> str:
    """Set a presentation property where the element already sets it (inline style or
    attribute); otherwise add it as an attribute."""
    span = _style_prop_span(tok, prop)
    if span is not None:
        return tok[: span[0]] + value + tok[span[1] :]
    return _set_svg_attr(tok, prop, value, add_if_missing=True)


def _svg_rtl(tokens: list[str], unit_tis: set) -> None:
    """direction="rtl" and a mirrored text-anchor on every <text> element holding a
    translated unit, and a mirrored anchor on any <tspan> / <textPath> inside it that sets
    its own. Token indexes do not change (start tags are rewritten in place)."""
    stack: list[tuple[str, str]] = []  # (element name, effective text-anchor)
    text_ti: int | None = None
    text_anchor = "start"
    inner: list[tuple[int, str]] = []  # (token index, own anchor) of tspan / textPath
    hit = False
    edits: list[tuple[int, str, str]] = []  # (token index, property, value)
    for ti, tok in enumerate(tokens):
        if not (tok.startswith("<") and tok.endswith(">")):
            if text_ti is not None and ti in unit_tis:
                hit = True
            continue
        closing, name, self_closing = _parse_tag(tok)
        if not name:
            continue  # comment, <?xml ?>, <!DOCTYPE>
        if closing:
            while stack:
                if stack.pop()[0] == name:
                    break
            if name == "text" and text_ti is not None:
                if hit:
                    edits.append((text_ti, "direction", "rtl"))
                    if text_anchor in _ANCHOR_SWAP:
                        edits.append((text_ti, "text-anchor", _ANCHOR_SWAP[text_anchor]))
                    for k, own in inner:
                        if own in _ANCHOR_SWAP:
                            edits.append((k, "text-anchor", _ANCHOR_SWAP[own]))
                text_ti, inner, hit = None, [], False
            continue
        own, _where = _own_prop(tok, "text-anchor")
        effective = own or (stack[-1][1] if stack else "start")
        if name == "text" and text_ti is None and not self_closing:
            # An empty <text/> holds no unit and has no end tag: tracking it would hand
            # the next <text>'s direction and anchor edits to it (gate fix).
            text_ti, text_anchor, inner, hit = ti, effective, [], False
        elif text_ti is not None and name in ("tspan", "textpath") and own:
            inner.append((ti, own))
        if not self_closing:
            stack.append((name, effective))
    for ti, prop, value in edits:
        tokens[ti] = _set_prop(tokens[ti], prop, value)


def _is_svg(data: bytes) -> bool:
    from app.services.image_localization import sniff_kind

    return sniff_kind(data) == "svg"


@register
class ImageFilter(Filter):
    fmt = "image"
    exts = ("svg", "png", "jpg", "jpeg", "tif", "tiff", "bmp", "gif", "webp")

    # --- parse ---------------------------------------------------------------
    def parse(self, data: bytes, provider: str | None = None) -> ParseResult:
        # Kill switch: parse (NEW ingestion, from any surface incl. a connector
        # attachment run) is refused while the module is off. build() stays
        # ungated so documents ingested while it was on still export.
        from app.services import image_localization as il

        if not il.enabled():
            raise il.ImageLocalizationError(
                "image localization is disabled (image_localization_enabled=0)"
            )
        if _is_svg(data):
            return self._parse_svg(data)
        return self._parse_raster(data, provider)

    def _parse_svg(self, data: bytes) -> ParseResult:
        # #509: declared charset or Windows-1252 converted, else refused
        text, enc = read_text_upload(data, declared=True)
        # #571 gate: splitting tags walks every `<` in Python and keeps every token, so a
        # 100 MB SVG of empty tags held the interpreter for minutes before a unit existed.
        # Twenty markup characters per allowed segment (1000000 at the default) covers a
        # detailed drawing and costs a few seconds at most.
        from app import config

        if text.count("<") > 20 * config.MAX_DOCUMENT_SEGMENTS:
            raise InputLimitError(
                "This SVG file has far more tags than MAX_DOCUMENT_SEGMENTS allows "
                f"({config.MAX_DOCUMENT_SEGMENTS} segments). Split it into smaller files."
            )
        tokens = _split_tags(text)
        units = [
            TextUnit(p_index=i, text=u["core"])
            for i, u in enumerate(_svg_units(tokens, capped=True))
        ]
        base = len(units)
        for k, u in enumerate(_svg_a11y_units(tokens, capped=True, already=base)):
            units.append(TextUnit(p_index=base + k, text=u["core"], style=u["style"]))
        skeleton = {
            "format": "image",
            "kind": "svg",
            "units": [{"p_index": u.p_index, "text": u.text, "style": u.style} for u in units],
        }
        return ParseResult(skeleton=note_source_encoding(skeleton, enc), units=units)

    def _parse_raster(self, data: bytes, provider: str | None) -> ParseResult:
        from app.services import image_localization as il

        # Decode guard first on every door (pixel caps, single frame, Batch 16): the mock
        # detector never opens a non-PNG, so without this an animated GIF uploaded as a
        # plain document would ingest and only fail at export.
        il.open_raster(data)
        regions = il.detect_regions(data)
        regions = il.escalate_recognition(data, regions, provider)
        units = [
            TextUnit(p_index=i, text=r["text"], inline_map={"image_region": r})
            for i, r in enumerate(regions)
        ]
        skeleton = {
            "format": "image",
            "kind": "raster",
            # The geometry contract, persisted with the document: the review
            # page, Segment.meta stamping, and render all read THIS list.
            "regions": regions,
            "detector": (regions[0]["source"] if regions else "none"),
            "units": [{"p_index": u.p_index, "text": u.text, "style": ""} for u in units],
        }
        return ParseResult(skeleton=skeleton, units=units)

    # --- build ---------------------------------------------------------------
    def build(self, original: bytes, paragraph_texts: dict) -> bytes:
        if _is_svg(original):
            return self._build_svg(original, paragraph_texts)
        return self._build_raster(original, paragraph_texts)

    def build_localized(
        self, original: bytes, paragraph_texts: dict, target_locale: str | None = None
    ) -> bytes:
        """SVG: also point the root element's language at the target (#691), and for a
        right-to-left target set direction="rtl" with a mirrored text-anchor on every
        translated <text> (_svg_rtl). Raster images and an unusable locale build exactly
        as `build`."""
        from app.filters import a11y

        tag = a11y.target_tag(target_locale)
        if not tag or not _is_svg(original):
            return self.build(original, paragraph_texts)
        rtl = a11y.target_direction(tag) == "rtl"
        return self._build_svg(original, paragraph_texts, lang=tag, rtl=rtl)

    def _build_svg(
        self, original: bytes, paragraph_texts: dict, lang: str = "", rtl: bool = False
    ) -> bytes:
        tokens = _split_tags(decode_text_lossless(original, declared=True))
        if lang:
            # Attribute values only change, so unit token indexes are unaffected.
            _declare_svg_language(tokens, lang)
        text_units = _svg_units(tokens)
        base = len(text_units)
        a11y_units = _svg_a11y_units(tokens)
        translated: set = set()
        for k, u in enumerate(text_units):
            new = paragraph_texts.get(str(k))
            if new is None:
                continue
            tokens[u["ti"]] = u["lead"] + _xml_escape(new) + u["trail"]
            translated.add(u["ti"])
        for k, u in enumerate(a11y_units):
            new = paragraph_texts.get(str(base + k))
            if new is None:
                continue
            tokens[u["ti"]] = u["lead"] + _xml_escape(new) + u["trail"]
        if rtl and translated:
            _svg_rtl(tokens, translated)
        return encode_text_lossless("".join(tokens))

    def _build_raster(self, original: bytes, paragraph_texts: dict) -> bytes:
        from app.services import image_localization as il

        regions = il.detect_regions(original)
        out, _flags = il.compose_overlay(original, regions, paragraph_texts)
        return out
