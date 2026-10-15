"""TTML / DFXP subtitle filter (.ttml, .dfxp; IMSC1 text profile documents are TTML).

A TTML document is XML rooted at ``<tt xmlns="http://www.w3.org/ns/ttml" xml:lang="en">``.
Each caption is a ``<p>`` (inside ``<body>``/``<div>``) carrying its own ``begin``/``end``/
``dur`` timing. Only the CONTENT of each ``<p>`` is translated:

- ``<br/>`` becomes a point token ``⟦xN⟧`` (a line break the translator places);
- ``<span ...>`` (styled or timed text) becomes a paired token ``⟦gN⟧...⟦/gN⟧``;
- any other child element (``<set>``, ``<metadata>``, animation, a foreign element),
  a comment or a processing instruction is carried whole as a point token ``⟦xN⟧``.

Timing, styling, layout, ``<head>`` and every byte outside a translated ``<p>`` content
are copied verbatim: the filter SPLICES the original text instead of re-serializing a
tree, so an untouched file round-trips byte-identical (including a UTF-8 BOM).

Safety: the whole document is first parsed with the hardened lxml parser used for XLIFF
(no DTD, no entities, no network, no huge tree; a DOCTYPE is refused), with a pre-parse
markup budget and the shared segment caps. The light tokenizer that finds byte offsets
only runs on a document lxml has already accepted as well-formed, and the two must agree
element for element or the file is refused. Only a ``<p>`` under ``<body>``/``<div>`` is a
caption (one inside ``<metadata>``, a foreign element or another ``<p>`` is not).

build_localized() also switches ``xml:lang`` to the target language on ``<tt>`` and on
any element that repeats the source language (an element tagged with a different
language is left alone).

Right-to-left targets (#741): TTML takes a paragraph's bidi level from ``tts:direction``
(initial ``ltr``, applies to p and span, inherited), not from the text: on a <p> it
"explicitly establishes the paragraph level" (UAX9 HL1; TTML1 8.2.4, TTML2 10.2.12; the
IMSC1 text profile permits ``#direction``). So for a target that a11y.target_direction calls
``rtl``, build_localized() writes ``tts:direction="rtl"`` on the start tag of every caption
<p> whose content it rebuilt (an explicit ``ltr`` there is flipped), using the prefix the
document already binds to its TT Style namespace (``http://www.w3.org/ns/ttml#styling``,
or the DFXP drafts' ``.../ttaf1#style`` or ``#styling``); with none in scope it binds an
unused prefix on <tt>. Deliberately per <p>, not on <body> / <div> / a region: a caption
left in the source language (untranslated, unchanged or blank) keeps its left-to-right
level. An inline attribute outranks a referenced style and the region's (TTML1 8.4.4.2), so
region
``tts:writingMode`` (shared with untouched captions) and ``tts:textAlign`` (start / end
follow direction) are left alone, as is ``tts:direction`` on a <span>: with
``tts:unicodeBidi`` that is an explicit embedding / override (HL3), usually deliberate (a
URL, code). Left-to-right targets and build() are unchanged.

Documented in: docs/architecture/translation-pipeline.md
"""

import bisect
import re

from lxml import etree

from app import config
from app.filters.base import (
    Filter,
    InputLimitError,
    ParseResult,
    TextUnit,
    check_segment_cap,
    check_unit_texts,
    decode_text_lossless,
    encode_text_lossless,
    note_source_encoding,
    read_text_upload,
    register,
)
from app.services.segment import _PLACEHOLDER_RE, ph_close, ph_open, ph_point

#: TTML 1/2 (and IMSC1) namespace, plus the two DFXP candidate-recommendation drafts
#: that older .dfxp files still carry.
TTML_NAMESPACES = (
    "http://www.w3.org/ns/ttml",
    "http://www.w3.org/2006/10/ttaf1",
    "http://www.w3.org/2006/04/ttaf1",
)
_XML_NS = "http://www.w3.org/XML/1998/namespace"

_BOM = b"\xef\xbb\xbf"
# Markup tokens in a document lxml already accepted as well-formed (no DOCTYPE).
_TOKEN_RE = re.compile(
    r"<!--.*?-->|<!\[CDATA\[.*?\]\]>|<\?.*?\?>|</[^>]*>|<(?:\"[^\"]*\"|'[^']*'|[^'\">])*>",
    re.S,
)
_NAME_RE = re.compile(r"</?\s*([^\s/>]+)")
_LANG_ATTR_RE = re.compile(r"""(\sxml:lang\s*=\s*)(["'])(.*?)\2""", re.S)
_ATTR_RE = re.compile(r"""\s([^\s=/>]+)\s*=\s*(["'])(.*?)\2""", re.S)
_WS_NL_RE = re.compile(r"[ \t\r\n]*[\r\n][ \t\r\n]*")
_DOCTYPE_RE = re.compile(rb"<!DOCTYPE", re.I)
_ENTITY_RE = re.compile(r"&(#x[0-9A-Fa-f]+|#[0-9]+|amp|lt|gt|quot|apos);")
_NAMED = {"amp": "&", "lt": "<", "gt": ">", "quot": '"', "apos": "'"}


class TtmlParseError(ValueError):
    """A .ttml/.dfxp upload that is not a usable TTML document. The message is plain."""


def check_ttml_budget(raw: bytes) -> None:
    """Pre-parse budget (#571 pattern): bound the tree lxml would build before it builds
    it. Forty markup characters per allowed segment is far above any real caption file."""
    if raw.count(b"<") > 40 * config.MAX_DOCUMENT_SEGMENTS:
        raise InputLimitError(
            "This TTML file has far more markup than MAX_DOCUMENT_SEGMENTS allows "
            f"({config.MAX_DOCUMENT_SEGMENTS}). Split it into smaller files."
        )


def _parse_tree(raw: bytes):
    """Hardened lxml parse (same parser as XLIFF uploads). Refuses a DOCTYPE/DTD."""
    from app.filters.xml_guard import make_hardened_parser

    if _DOCTYPE_RE.search(raw):
        raise TtmlParseError("TTML file declares a DOCTYPE / DTD; refused (entity/XXE vector).")
    try:
        root = etree.fromstring(raw, parser=make_hardened_parser())
    except etree.XMLSyntaxError as exc:
        raise TtmlParseError(f"TTML file is not well-formed XML: {exc}") from exc
    info = root.getroottree().docinfo
    if info.doctype or info.internalDTD is not None or info.externalDTD is not None:
        raise TtmlParseError("TTML file declares a DOCTYPE / DTD; refused (entity/XXE vector).")
    q = etree.QName(root)
    if q.localname != "tt" or q.namespace not in TTML_NAMESPACES:
        raise TtmlParseError(
            "not a TTML/DFXP document: the root element must be <tt> in the TTML namespace "
            "(http://www.w3.org/ns/ttml)."
        )
    return root


def _local(name: str) -> str:
    return name.rsplit(":", 1)[-1]


def _unescape(s: str) -> str:
    def rep(m):
        g = m.group(1)
        if g in _NAMED:
            return _NAMED[g]
        return chr(int(g[2:], 16) if g[1] in "xX" else int(g[1:]))

    return _ENTITY_RE.sub(rep, s)


def _escape(s: str) -> str:
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


class _Doc:
    """Decoded text plus the TTML <p> spans: (start_tag_end, end_tag_start, preserve,
    prefix) offsets into ``text`` for every TTML-namespace <p>, in document order."""

    def __init__(self, data: bytes, *, upload: bool):
        raw = bytes(data)
        self.bom = raw.startswith(_BOM)
        if upload:
            self.text, self.enc = read_text_upload(raw, sig=True, declared=True)
            check_ttml_budget(raw)
        else:
            self.text, self.enc = decode_text_lossless(raw, sig=True, declared=True), "utf-8"
        # lxml sees the decoded text (declaration already rewritten to UTF-8 when converted)
        self.root = _parse_tree(encode_text_lossless(self.text))
        # Every element's start tag (start, end, raw name) and content end, in document
        # order. lxml's element order must match one for one, so offsets map to elements.
        tags: list[list] = []  # [tag_start, tag_end, name, content_end]
        stack: list[int] = []
        self.lang_ranges: list[tuple] = []  # (value_start, value_end, value) of xml:lang
        for m in _TOKEN_RE.finditer(self.text):
            tok = m.group(0)
            if tok.startswith(("<!", "<?")):
                continue
            if tok.startswith("</"):
                if not stack:
                    raise TtmlParseError("TTML file structure could not be read; refused.")
                tags[stack.pop()][3] = m.start()
                continue
            nm = _NAME_RE.match(tok)
            for lm in _LANG_ATTR_RE.finditer(tok):
                self.lang_ranges.append(
                    (m.start() + lm.start(3), m.start() + lm.end(3), lm.group(3))
                )
            tags.append([m.start(), m.end(), nm.group(1) if nm else "", m.end()])
            if not tok.endswith("/>"):
                stack.append(len(tags) - 1)
        elements = [el for el in self.root.iter() if isinstance(el.tag, str)]
        if stack or len(tags) != len(elements) or not tags:
            raise TtmlParseError("TTML file structure could not be read consistently; refused.")
        self.root_tag = (tags[0][0], tags[0][1])  # the <tt> start tag
        self.root_lang = self.root.get(f"{{{_XML_NS}}}lang")  # None when absent
        self.body_start = None
        self.spans: list[tuple] = []  # (content_start, content_end, preserve, raw p name)
        self.p_tags: list[tuple] = []  # (tag_start, tag_end, element), aligned with spans
        self.prefixes: set = set()  # every namespace prefix declared anywhere (#741)
        for el, (t_start, t_end, name, c_end) in zip(elements, tags, strict=True):
            self.prefixes.update(k for k in el.nsmap if k)
            q = etree.QName(el)
            if q.namespace not in TTML_NAMESPACES:
                continue
            if q.localname == "body" and self.body_start is None:
                self.body_start = t_start
            if q.localname == "p" and _is_caption_p(el):
                self.spans.append((t_end, c_end, _preserve(el), name))
                self.p_tags.append((t_start, t_end, el))


def _is_caption_p(el) -> bool:
    """A caption <p> sits in <body>, under TTML <div> elements only (TTML1 7.1.3/7.1.4).
    A <p> inside <metadata>, a foreign element or another <p> is not a caption: taking it
    would also overlap the enclosing caption's splice."""
    parent = el.getparent()
    while parent is not None:
        q = etree.QName(parent)
        if q.namespace not in TTML_NAMESPACES:
            return False
        if q.localname == "body":
            return True
        if q.localname != "div":
            return False
        parent = parent.getparent()
    return False


def _preserve(el) -> bool:
    """Effective xml:space for a <p>: its own attribute or the nearest ancestor's (XML 1.0
    2.10, inherited; TTML1 7.2.3)."""
    while el is not None:
        v = el.get(f"{{{_XML_NS}}}space")
        if v is not None:
            return v.strip() == "preserve"
        el = el.getparent()
    return False


def _tokenize(fragment: str, preserve: bool):
    """(unit_text, inline_map) for one <p> content. inline_map[str(n)] = (kind, open_raw,
    close_raw): kind "br" / "span" / "raw" (a whole element, comment or PI kept verbatim)."""
    inline: dict = {}
    parts: list[str] = []
    n = 0
    pos = 0
    toks = list(_TOKEN_RE.finditer(fragment))
    i = 0
    span_stack: list[int] = []
    while i < len(toks):
        m = toks[i]
        parts.append(("t", _unescape(fragment[pos : m.start()])))
        tok = m.group(0)
        pos = m.end()
        i += 1
        if tok.startswith("<![CDATA["):
            parts.append(("t", tok[9:-3]))
            continue
        if tok.startswith(("<!--", "<?")):
            n += 1
            inline[str(n)] = ("raw", tok, "")
            parts.append(("x", ph_point("x", n)))
            continue
        name = _NAME_RE.match(tok).group(1)
        if tok.startswith("</"):
            if span_stack:
                k = span_stack.pop()
                inline[str(k)] = (inline[str(k)][0], inline[str(k)][1], tok)
                parts.append(("c", ph_close("g", k)))
            continue
        local = _local(name)
        n += 1
        if local == "br":
            if not tok.endswith("/>"):  # <br></br>: consume the matching end tag
                while i < len(toks) and not toks[i].group(0).startswith("</"):
                    i += 1
                close = toks[i].group(0) if i < len(toks) else ""
                pos = toks[i].end() if i < len(toks) else pos
                i += 1
                inline[str(n)] = ("br", tok + fragment[m.end() : pos - len(close)] + close, "")
            else:
                inline[str(n)] = ("br", tok, "")
            parts.append(("br", ph_point("x", n)))
        elif local == "span" and not tok.endswith("/>"):
            inline[str(n)] = ("span", tok, "")
            span_stack.append(n)
            parts.append(("o", ph_open("g", n)))
        else:
            # Any other element: keep it whole (with its subtree) as one point token.
            start = m.start()
            if not tok.endswith("/>"):
                depth = 1
                while i < len(toks) and depth:
                    t2 = toks[i].group(0)
                    if not t2.startswith(("<!", "<?")):
                        if t2.startswith("</"):
                            depth -= 1
                        elif not t2.endswith("/>"):
                            depth += 1
                    pos = toks[i].end()
                    i += 1
            inline[str(n)] = ("raw", fragment[start:pos], "")
            parts.append(("x", ph_point("x", n)))
    parts.append(("t", _unescape(fragment[pos:])))
    if not preserve:
        # xml:space="default": a run of whitespace containing a line break is layout, not
        # content. It vanishes next to a <br/> and collapses to one space elsewhere.
        out = []
        for idx, (kind, val) in enumerate(parts):
            if kind == "t":
                prev_br = idx > 0 and parts[idx - 1][0] == "br"
                next_br = idx + 1 < len(parts) and parts[idx + 1][0] == "br"
                val = _WS_NL_RE.sub(" ", val)
                if prev_br:
                    val = val.lstrip(" ")
                if next_br:
                    val = val.rstrip(" ")
            out.append(val)
        text = "".join(out).strip()
    else:
        text = "".join(v for _k, v in parts).strip()
    return text, inline


def _render(text: str, inline: dict, br_name: str) -> str:
    """Translated unit text -> <p> content. Tolerant like the XLIFF rebuild: an unknown
    or repeated token and a stray close are dropped (never written as literal ⟦⟧), an
    unclosed span is closed at the end, and a typed newline becomes a <br/>."""
    from app.filters.xml_text import xml_target_text

    text = xml_target_text(text)
    out: list[str] = []
    used: set[str] = set()
    stack: list[tuple] = []  # (key, close_raw or None for a phantom)

    def put_text(chunk: str):
        if chunk:
            lines = _escape(chunk).split("\n")
            out.append(f"<{br_name}/>".join(lines))

    pos = 0
    for m in _PLACEHOLDER_RE.finditer(text):
        put_text(text[pos : m.start()])
        pos = m.end()
        inner = m.group(0)[1:-1]
        if inner.startswith("/"):
            key = re.sub(r"^/[a-z]?", "", inner)
            if stack and stack[-1][0] == key:
                _k, close = stack.pop()
                if close is not None:
                    out.append(close)
            continue
        is_point = inner.startswith("x")
        key = re.sub(r"^[a-z]?", "", inner, count=1)
        entry = inline.get(key)
        if entry is None or key in used or (entry[0] == "span") == is_point:
            if not is_point:
                stack.append((key, None))
            continue
        used.add(key)
        kind, open_raw, close_raw = entry
        out.append(open_raw)
        if kind == "span":
            stack.append((key, close_raw))
    put_text(text[pos:])
    for _k, close in reversed(stack):
        if close is not None:
            out.append(close)
    return "".join(out)


def _relang_tags(fragment: str, source_lang: str | None, lang: str) -> str:
    """Switch xml:lang values equal to the source language inside the TAGS of a rebuilt
    <p> content (never inside text, which is escaped and may quote anything)."""
    src = (source_lang or "").strip().lower()
    if not src:
        return fragment

    def fix_attr(lm):
        if lm.group(3).strip().lower() != src:
            return lm.group(0)
        return lm.group(1) + lm.group(2) + lang + lm.group(2)

    def fix_tag(m):
        tok = m.group(0)
        if tok.startswith(("<!", "<?", "</")):
            return tok
        return _LANG_ATTR_RE.sub(fix_attr, tok)

    return _TOKEN_RE.sub(fix_tag, fragment)


#: TT Style namespace(s) for each TTML namespace, preferred first. TTML1 / TTML2 / IMSC1 use
#: ``ns/ttml#styling``; the DFXP drafts (2006/04 WD, 2006/10 CR) define ``ttaf1#style`` (Table 1
#: of each), and some tools (Flash-era players) wrote ``ttaf1#styling``: both are recognised.
_STYLING_NS = {
    ns: (ns + "#styling",) if ns.endswith("/ns/ttml") else (ns + "#style", ns + "#styling")
    for ns in TTML_NAMESPACES
}
_STYLING_NAMESPACES = frozenset(v for vs in _STYLING_NS.values() for v in vs)


def _rtl_edits(doc: _Doc, p_index: int, new_prefix: str) -> tuple[list, bool]:
    """(edits, used_new_prefix) giving one rebuilt caption <p> a right-to-left paragraph
    level (#741): TTML's ``tts:direction`` (initial ``ltr``, applies to p and span,
    inherited) "explicitly establishes the paragraph level" of a <p> (UAX9 HL1), so an
    Arabic / Hebrew cue without it is laid out left to right.

    An existing ``direction`` attribute in a TT Style namespace on the <p> is set to ``rtl``
    (an explicit ``ltr`` is flipped, #724 rule); otherwise ``tts:direction="rtl"`` is added
    after the tag name with whatever prefix is in scope for the styling namespace there, or
    ``new_prefix`` (which the caller then binds on <tt>)."""
    t_start, t_end, el = doc.p_tags[p_index]
    tag = doc.text[t_start:t_end]
    names = {f"{k}:direction" for k, v in el.nsmap.items() if k and v in _STYLING_NAMESPACES}
    found, edits = False, []
    for m in _ATTR_RE.finditer(tag):  # attribute by attribute: never inside a quoted value
        if m.group(1) in names:
            found = True
            if m.group(3) != "rtl":  # an explicit ltr (or junk) becomes rtl
                edits.append((t_start + m.start(3), t_start + m.end(3), "rtl"))
    if found:
        return edits, False
    wants = _STYLING_NS.get(etree.QName(el).namespace, ())
    prefix = next((k for w in wants for k, v in el.nsmap.items() if k and v == w), None)
    at = t_start + _NAME_RE.match(tag).end()
    return [(at, at, f' {prefix or new_prefix}:direction="rtl"')], prefix is None


def _units(doc: _Doc):
    """(p_index, text, inline, (start, end, br_name)) for each TTML <p>, document order.
    p_index is the <p>'s position among TTML <p> elements, so a blank <p> is skipped
    without renumbering the rest (build re-derives the same positions)."""
    for idx, (start, end, preserve, pname) in enumerate(doc.spans):
        text, inline = _tokenize(doc.text[start:end], preserve)
        prefix = pname.rsplit(":", 1)[0] + ":" if ":" in pname else ""
        yield idx, text, inline, (start, end, prefix + "br")


@register
class TtmlFilter(Filter):
    fmt = "ttml"
    exts = ("dfxp",)

    def parse(self, data: bytes, provider: str | None = None) -> ParseResult:
        doc = _Doc(data, upload=True)
        units: list[TextUnit] = []
        for idx, text, inline, _span in _units(doc):
            if not _PLACEHOLDER_RE.sub("", text).strip():
                continue
            units.append(TextUnit(p_index=idx, text=text, inline_map=inline))
            check_segment_cap(len(units))  # #571
        check_unit_texts(u.text for u in units)
        skeleton = {
            "format": "ttml",
            "units": [{"p_index": u.p_index, "text": u.text, "style": ""} for u in units],
        }
        return ParseResult(skeleton=note_source_encoding(skeleton, doc.enc), units=units)

    def _splice(
        self, doc: _Doc, paragraph_texts: dict, lang: str | None = None, rtl: bool = False
    ) -> bytes:
        edits: list[tuple] = []  # (start, end, replacement)
        rebuilt: list[int] = []  # p indexes whose content was rewritten
        for idx, text, inline, (start, end, br_name) in _units(doc):
            new = paragraph_texts.get(str(idx))
            if new is None or new == text:
                # Unchanged text keeps the original bytes (layout whitespace, entity
                # spelling, attribute quoting); the language switch below still applies.
                continue
            original = doc.text[start:end]
            lead = original[: len(original) - len(original.lstrip())]
            trail = original[len(original.rstrip()) :]
            body = _render(new, inline, br_name)
            if lang:
                body = _relang_tags(body, doc.root_lang, lang)
            edits.append((start, end, lead + body + trail))
            rebuilt.append(idx)
        if lang:
            src = (doc.root_lang or "").strip().lower()
            rt_start, rt_end = doc.root_tag
            done = sorted((s, e) for s, e, _r in edits)
            starts = [s for s, _e in done]
            for vs, ve, val in doc.lang_ranges:
                j = bisect.bisect_right(starts, vs) - 1
                if j >= 0 and vs < done[j][1]:
                    continue  # inside a rebuilt <p>: already switched by _relang_tags
                # <head> metadata is never translated: an explicit source tag there stays.
                in_body = doc.body_start is not None and vs >= doc.body_start
                if rt_start <= vs < rt_end or (in_body and src and val.strip().lower() == src):
                    edits.append((vs, ve, lang))
            if doc.root_lang is None:  # no xml:lang on <tt>: add one after the tag name
                head = doc.text[rt_start:rt_end]
                at = rt_start + _NAME_RE.match(head).end()
                edits.append((at, at, f' xml:lang="{lang}"'))
        if rtl and rebuilt:
            # Start tags of rebuilt <p> only (never the content splices above, never an
            # untouched caption), so these edits cannot overlap the others.
            new_prefix = next(
                p for p in ("tts", *(f"tts{n}" for n in range(1, 1000))) if p not in doc.prefixes
            )
            bind = False
            for idx in rebuilt:
                more, used = _rtl_edits(doc, idx, new_prefix)
                edits.extend(more)
                bind = bind or used
            if bind:  # no styling prefix in scope: bind an unused one on <tt>
                rt_start, rt_end = doc.root_tag
                at = rt_start + _NAME_RE.match(doc.text[rt_start:rt_end]).end()
                ns = _STYLING_NS[etree.QName(doc.root).namespace][0]
                edits.append((at, at, f' xmlns:{new_prefix}="{ns}"'))
        pieces: list[str] = []
        pos = 0
        for start, end, rep in sorted(edits, key=lambda e: e[0]):
            pieces.append(doc.text[pos:start])
            pieces.append(rep)
            pos = end
        pieces.append(doc.text[pos:])
        out = encode_text_lossless("".join(pieces))
        return _BOM + out if doc.bom else out

    def build(self, original: bytes, paragraph_texts: dict) -> bytes:
        return self._splice(_Doc(original, upload=False), paragraph_texts)

    def build_localized(
        self, original: bytes, paragraph_texts: dict, target_locale: str | None = None
    ) -> bytes:
        """build() plus the TARGET language in ``xml:lang`` (TTML requires it on <tt>) and,
        for a right-to-left target, ``tts:direction="rtl"`` on every rebuilt caption (#741)."""
        from app.filters import a11y

        tag = a11y.target_tag(target_locale)
        rtl = bool(tag) and a11y.target_direction(tag) == "rtl"
        return self._splice(_Doc(original, upload=False), paragraph_texts, tag or None, rtl)
