"""Shared machinery for XML software-resource filters (Android strings.xml, .NET RESX/RESW,
Java XML properties, WiX .wxl, Qt Linguist .ts).

Parse uses lxml with the hardened posture of xliff_filter (no entity resolution, no DTD load,
no network, no huge tree). A DOCTYPE is allowed ONLY without an internal subset, because two of
these formats ship one by design (Qt `<!DOCTYPE TS>`, Java `<!DOCTYPE properties SYSTEM ...>`);
an internal subset is where entities are declared, so refusing `[` refuses every XXE /
billion-laughs payload, and any entity reference left in the tree is refused too.

Build never re-serializes the whole document. lxml finds the units; a small lexer over the SAME
decoded text gives every element's character span, and build splices only the ranges it
changes. Everything else (resheader and schema blocks, comments, attribute quoting, empty
`<a></a>` tags, CRLF line endings, the BOM) stays byte-identical.

Each filter walks its tree into an ordered list of Slots. Parse and build call the same walk,
so unit indices always line up; a slot that is not translatable is skipped-but-counted.

Documented in: docs/architecture/translation-pipeline.md
"""

import re
from copy import deepcopy
from dataclasses import dataclass, field, replace

from lxml import etree

from app import config
from app.filters.base import (
    Filter,
    InputLimitError,
    ParseResult,
    TextUnit,
    check_segment_cap,
    note_source_encoding,
    read_text_upload,
)
from app.services.segment import ph_close, ph_open, ph_point


class XmlResourceError(InputLimitError):
    """An upload that is not a well-formed, safe resource file (not XML, a DTD internal
    subset, an entity, an unsupported .xml root). A subclass of InputLimitError (itself a
    ValueError) so every surface that shows a refusal's own message (web page, CLI error line,
    MCP tool error) shows this plain one; the API answers it 422 like #509's encoding refusal."""


@dataclass
class ResourceRecord:
    """The key-based view of one resource string (Batch 9 wires it into keys import/export).

    key: resource id (Android name, RESX data name, Java entry key, WiX Id, Qt context +
    source); plural: the CLDR quantity (Android `<item quantity>`) or "" ; index: position in
    a string-array; translatable: False for skipped-but-counted entries."""

    key: str
    value: str
    comment: str = ""
    plural: str = ""
    index: int | None = None
    max_length: int | None = None
    context: str = ""
    translatable: bool = True


@dataclass
class Slot:
    """One translatable place. `src` holds the source text (element content or attribute
    `src_attr`); a translation is written into `dst` (content or `dst_attr`)."""

    src: object
    dst: object
    record: ResourceRecord
    src_attr: str | None = None
    dst_attr: str | None = None
    kind: str = "content"  # content | attr | ts_numerus
    skip: bool = False
    extra: dict = field(default_factory=dict)


# --- decode + hardened parse --------------------------------------------------------------

# Quote-aware: a system id may contain ">" ("a>b"), which must not hide a following "[".
_DOCTYPE_RE = re.compile(r"""<!DOCTYPE\b(?:[^>\["']|"[^"]*"|'[^']*')*(\[)?""", re.IGNORECASE)
_XML_DECL_ENC = re.compile(r"""(<\?xml[^>]{0,200}?\bencoding\s*=\s*["'])([^"']{1,40})(["'])""")


def check_xml_budget(text: str, what: str) -> None:
    """Pre-parse budget (#571 pattern, xliff_filter.check_markup_budget): bound the element
    count at C speed before lxml builds a tree (about ten times the file)."""
    if text.count("<") > 40 * config.MAX_DOCUMENT_SEGMENTS:
        raise InputLimitError(
            f"{what} has far more entries than MAX_DOCUMENT_SEGMENTS allows "
            f"({config.MAX_DOCUMENT_SEGMENTS}). Split it into smaller files."
        )


def decode(data: bytes) -> tuple[str, bool, str]:
    """(text, had_utf8_bom, source_encoding). UTF-8 (BOM kept aside), UTF-16 with a BOM, a
    declared legacy charset, or Windows-1252, exactly as every text filter reads (#509). The
    XML declaration is made truthful (UTF-8), since build always writes UTF-8."""
    raw = bytes(data)
    bom = raw.startswith(b"\xef\xbb\xbf")
    text, enc = read_text_upload(raw, sig=True, declared=True)
    m = _XML_DECL_ENC.search(text[:300])
    if m and m.group(2).strip().lower().replace("_", "-") not in ("utf-8", "utf8"):
        text = text[: m.start(2)] + "UTF-8" + text[m.end(2) :]
    return text, bom, enc


def parse_tree(text: str, what: str, *, capped: bool = True):
    """Parse decoded XML text safely. Raises XmlResourceError / InputLimitError."""
    if capped:
        check_xml_budget(text, what)
    m = _DOCTYPE_RE.search(text)
    if m and m.group(1):
        raise XmlResourceError(
            f"{what} declares a DTD internal subset; refused (resource files never need one, "
            "and it is an entity-expansion / XXE vector)."
        )
    parser = etree.XMLParser(
        resolve_entities=False,
        no_network=True,
        load_dtd=False,
        dtd_validation=False,
        huge_tree=False,
        strip_cdata=False,
        remove_blank_text=False,
        remove_comments=False,
    )
    try:
        root = etree.fromstring(text.encode("utf-8"), parser=parser)
    except etree.XMLSyntaxError as exc:
        raise XmlResourceError(f"{what} is not well-formed XML: {exc}") from exc
    for node in root.iter():
        if isinstance(node, etree._Entity):
            raise XmlResourceError(f"{what} uses an undefined entity (&{node.name};); refused.")
    return root


def local(el) -> str:
    return etree.QName(el).localname if isinstance(el.tag, str) else ""


def elements(root):
    return [e for e in root.iter() if isinstance(e.tag, str)]


# --- element spans in the original text ----------------------------------------------------

_LEX = re.compile(
    r"<!--.*?-->"
    r"|<!\[CDATA\[.*?\]\]>"
    r"|<\?.*?\?>"
    r"|<!DOCTYPE\b[^>]*>"
    r"|</[^\s>]+\s*>"
    r"|<([^\s/>!?]+)((?:[^>\"']|\"[^\"]*\"|'[^']*')*?)(/?)>",
    re.DOTALL,
)


@dataclass
class Span:
    start: int  # start of the start tag
    stag_end: int  # end of the start tag
    etag_start: int  # start of the end tag (== stag_end for a self-closing element)
    end: int  # end of the end tag
    qname: str
    empty: bool  # written as <a/>


def element_spans(text: str) -> list[Span]:
    """Character spans of every element of an already well-formed document, in document
    (start-tag) order: the same order as lxml's root.iter() over elements."""
    spans: list[Span] = []
    stack: list[Span] = []
    for m in _LEX.finditer(text):
        tok = m.group(0)
        if tok.startswith("</"):
            if not stack:
                raise ValueError("could not map the XML elements (unbalanced tags)")
            sp = stack.pop()
            sp.etag_start, sp.end = m.start(), m.end()
        elif m.group(1):
            sp = Span(m.start(), m.end(), m.end(), m.end(), m.group(1), bool(m.group(3)))
            spans.append(sp)
            if not sp.empty:
                stack.append(sp)
    if stack:
        raise ValueError("could not map the XML elements (unbalanced tags)")
    return spans


# One attribute of a start tag: groups (leading space, name, "=", quote, value).
_ATTR_TOKEN = re.compile(r"""(\s+)([^\s=/>]+)(\s*=\s*)(["'])(.*?)\4""", re.DOTALL)


class Splicer:
    """Collects non-overlapping text-range edits on the original and applies them."""

    def __init__(self, text: str, root):
        self.text = text
        els = elements(root)
        spans = element_spans(text)
        if len(spans) != len(els):
            raise ValueError("could not map the XML elements (element count differs)")
        # lxml element proxies are only stable (same id) while a Python reference is alive:
        # keep the list so every later lookup by id() resolves to the same element.
        self._els = els
        self.span = {id(e): s for e, s in zip(els, spans, strict=True)}
        self.edits: list[tuple[int, int, str]] = []

    def of(self, el) -> Span:
        return self.span[id(el)]

    def replace_inner(self, el, inner: str) -> None:
        sp = self.of(el)
        if sp.empty:
            stag = self.text[sp.start : sp.stag_end]
            stag = re.sub(r"\s*/>$", ">", stag)
            self.edits.append((sp.start, sp.end, f"{stag}{inner}</{sp.qname}>"))
        else:
            self.edits.append((sp.stag_end, sp.etag_start, inner))

    def set_attr(self, el, name: str, value: str | None) -> None:
        """Set (or with None remove) one attribute in the ORIGINAL start tag text, keeping
        the quote style and every other byte of the tag."""
        sp = self.of(el)
        stag = self.text[sp.start : sp.stag_end]
        # Walk the attributes in order so a look-alike inside another attribute's quoted
        # value (Id="a Value='x'") is never taken for the attribute itself.
        m = next((a for a in _ATTR_TOKEN.finditer(stag) if a.group(2) == name), None)
        if value is None:
            if not m:
                return
            new = stag[: m.start()] + stag[m.end() :]
        elif m:
            q = m.group(4)
            new = stag[: m.start(5)] + attr_escape(value, q) + stag[m.end(5) :]
        else:
            close = re.search(r"\s*/?>$", stag)
            new = (
                stag[: close.start()]
                + f' {name}="{attr_escape(value, chr(34))}"'
                + stag[close.start() :]
            )
        self.edits.append((sp.start, sp.stag_end, new))

    def insert(self, pos: int, s: str) -> None:
        self.edits.append((pos, pos, s))

    def delete(self, start: int, end: int) -> None:
        self.edits.append((start, end, ""))

    def result(self) -> str:
        out = self.text
        last = None
        for start, end, s in sorted(self.edits, key=lambda e: (e[0], e[1]), reverse=True):
            if last is not None and end > last:
                raise ValueError("overlapping edits")  # a filter bug, never user data
            out = out[:start] + s + out[end:]
            last = start
        return out


def text_escape(s: str) -> str:
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def attr_escape(s: str, quote: str = '"') -> str:
    s = text_escape(s).replace("\n", "&#10;").replace("\r", "&#13;").replace("\t", "&#9;")
    return s.replace(quote, "&quot;" if quote == '"' else "&apos;")


# --- inline content: tokens out, elements back -----------------------------------------------


def tokenize(el, is_point) -> tuple[str, dict]:
    """(text, inline_map) for an element's mixed content, using the shared ⟦g1⟧/⟦x1⟧ scheme
    (services/segment). `is_point(child)` children keep their content untouched (a
    placeholder such as Android <xliff:g> or Qt <byte>); others are paired and recursed."""
    inline_map: dict = {}
    counter = [0]

    def walk(node) -> str:
        parts = []
        for c in node:
            if not isinstance(c.tag, str):  # a comment or PI inside the value
                counter[0] += 1
                n = counter[0]
                inline_map[str(n)] = deepcopy(c)
                parts.append(ph_point("x", n) + (c.tail or ""))
                continue
            counter[0] += 1
            n = counter[0]
            inline_map[str(n)] = deepcopy(c)
            if is_point(c):
                parts.append(ph_point("x", n) + (c.tail or ""))
            else:
                parts.append(
                    ph_open("g", n) + (c.text or "") + walk(c) + ph_close("g", n) + (c.tail or "")
                )
        return "".join(parts)

    return (el.text or "") + walk(el), inline_map


def has_cdata(splicer_text: str, sp: Span) -> bool:
    return "<![CDATA[" in splicer_text[sp.stag_end : sp.etag_start]


_POINT_MARK = "data-locos-point"


def render_inner(dst, text: str, inline_map: dict, escape, is_point, cdata=False) -> str:
    """Serialize translated tokenized `text` as the inner XML of `dst`. Unknown or duplicated
    tokens are dropped (xliff_filter._rebuild_target rules); `escape(piece, first)` applies the
    format's own escaping to every translated text piece, never inside a point element."""
    from app.filters.xliff_filter import _rebuild_target
    from app.filters.xml_text import xml_target_text

    text = xml_target_text(text)
    if not inline_map:
        body = escape(text, True)
        if cdata and "]]>" not in body:
            return f"<![CDATA[{body}]]>"
        return text_escape(body)
    for v in inline_map.values():
        if isinstance(v.tag, str) and is_point(v):
            v.set(_POINT_MARK, "1")
    tmp = etree.Element("tmp", nsmap={k: v for k, v in dst.nsmap.items() if k})
    _rebuild_target(tmp, text, inline_map)
    if tmp.text:
        tmp.text = escape(tmp.text, True)

    def walk(node):
        for c in node:
            if c.tail:
                c.tail = escape(c.tail, False)
            if isinstance(c.tag, str) and c.get(_POINT_MARK) is None:
                if c.text:
                    c.text = escape(c.text, False)
                walk(c)

    walk(tmp)
    for c in tmp.iter():
        if isinstance(c.tag, str) and c.get(_POINT_MARK) is not None:
            del c.attrib[_POINT_MARK]
    parts = [text_escape(tmp.text or "")]
    for c in tmp:
        frag = etree.tostring(c, encoding="unicode", with_tail=True)
        for p, u in dst.nsmap.items():
            frag = frag.replace(f' xmlns:{p}="{u}"' if p else f' xmlns="{u}"', "")
        parts.append(frag)
    return "".join(parts)


def split_ws(s: str) -> tuple[str, str, str]:
    """(leading whitespace, core, trailing whitespace)."""
    core = s.strip()
    if not core:
        return s, "", ""
    lead = s[: len(s) - len(s.lstrip())]
    trail = s[len(s.rstrip()) :]
    return lead, core, trail


def is_translatable(s: str) -> bool:
    return any(c.isalnum() for c in s)


# --- the base filter ----------------------------------------------------------------------


class XmlResourceFilter(Filter):
    """Subclasses set `fmt`, `what` and implement `slots(root)`; optional hooks: `display`
    (unescape for the translator), `escape` (re-escape on build), `write`, `localize`."""

    what = "This resource file"
    style = ""

    def is_point(self, el) -> bool:
        return True

    def slots(self, root) -> list[Slot]:  # pragma: no cover - abstract
        raise NotImplementedError

    # parse ---------------------------------------------------------------------------------
    def _load(self, data: bytes, capped: bool = True):
        text, bom, enc = decode(data)
        root = parse_tree(text, self.what, capped=capped)
        return text, bom, enc, root

    def source_text(self, slot: Slot) -> tuple[str, dict]:
        if slot.src_attr:
            return slot.src.get(slot.src_attr) or "", {}
        return tokenize(slot.src, self.is_point)

    def display(self, slot: Slot, raw: str) -> str:
        """Format-specific unescape for the translator (Android \\' and so on)."""
        return raw

    def parse(self, data: bytes, provider: str | None = None) -> ParseResult:
        text, _bom, enc, root = self._load(data)
        Splicer(text, root)  # the build must be able to map every element: check at upload
        units: list[TextUnit] = []
        for idx, slot in enumerate(self.slots(root)):
            if slot.skip:
                continue
            raw, inline_map = self.source_text(slot)
            _lead, core, _trail = split_ws(self.display(slot, raw))
            if not is_translatable(core):
                continue
            units.append(TextUnit(p_index=idx, text=core, style=self.style, inline_map=inline_map))
            check_segment_cap(len(units))
        skeleton = {
            "format": self.fmt,
            "units": [{"p_index": u.p_index, "text": u.text, "style": u.style} for u in units],
        }
        note_source_encoding(skeleton, enc)
        return ParseResult(skeleton=skeleton, units=units)

    def records(self, data: bytes) -> list[ResourceRecord]:
        """Every resource entry in file order, translatable or not (Batch 9 keys bridge)."""
        _text, _bom, _enc, root = self._load(data)
        out = []
        for slot in self.slots(root):
            raw, _ = self.source_text(slot)
            shown = self.display(slot, raw)
            ok = not slot.skip and is_translatable(shown)
            out.append(replace(slot.record, value=shown, translatable=ok))
        return out

    # build ---------------------------------------------------------------------------------
    def escape(self, slot: Slot):
        return lambda s, first: s

    def inner_for(self, slot: Slot, new: str, sp: Splicer) -> str:
        """The new inner XML for a content slot carrying translation `new`."""
        raw, inline_map = self.source_text(slot)
        lead, _core, trail = split_ws(self.display(slot, raw))
        cdata = has_cdata(sp.text, sp.of(slot.src))
        return render_inner(
            slot.dst, lead + new + trail, inline_map, self.escape(slot), self.is_point, cdata
        )

    def write(self, slot: Slot, new: str, sp: Splicer) -> None:
        if slot.dst_attr:
            raw, _ = self.source_text(slot)
            lead, _core, trail = split_ws(self.display(slot, raw))
            sp.set_attr(slot.dst, slot.dst_attr, lead + new + trail)
            return
        sp.replace_inner(slot.dst, self.inner_for(slot, new, sp))

    def _build(self, original: bytes, paragraph_texts: dict, locale: str | None = None) -> bytes:
        text, bom, _enc, root = self._load(original, capped=False)
        sp = Splicer(text, root)
        slots = self.slots(root)
        # localize first: it may drop elements (plural forms a language does not use), whose
        # own translation must then not be written (the edits would overlap).
        dropped = self.localize(root, slots, paragraph_texts, sp, locale) or set()
        for idx, slot in enumerate(slots):
            new = paragraph_texts.get(str(idx))
            if new is None or slot.skip or id(slot.dst) in dropped:
                continue
            self.write(slot, new, sp)
        out = sp.result().encode("utf-8")
        return (b"\xef\xbb\xbf" + out) if bom else out

    def localize(self, root, slots, paragraph_texts, sp: Splicer, locale) -> set | None:
        """Hook for build_localized (target language stamp, plural forms). Returns the ids of
        elements it removed. No-op by default."""
        return None

    def build(self, original: bytes, paragraph_texts: dict) -> bytes:
        return self._build(original, paragraph_texts, None)

    def build_localized(
        self, original: bytes, paragraph_texts: dict, target_locale: str | None = None
    ) -> bytes:
        return self._build(original, paragraph_texts, target_locale)


def plural_categories(locale: str | None, *, with_other: bool = True) -> list[str]:
    """CLDR plural categories a language uses for whole numbers, in CLDR order (babel), plus
    "other" (always required by Android) unless with_other=False. Falls back to
    ["one", "other"] for an unknown locale."""
    order = ["zero", "one", "two", "few", "many", "other"]
    try:
        from babel import Locale

        rule = Locale.parse((locale or "").replace("-", "_")).plural_form
        cats = {rule(n) for n in range(0, 1001)} | ({"other"} if with_other else set())
        return [c for c in order if c in cats]
    except Exception:
        return ["one", "other"]
