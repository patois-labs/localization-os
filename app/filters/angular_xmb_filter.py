"""Angular i18n XMB / XTB filter (.xmb source bundles, .xtb translation bundles).

``ng extract-i18n --format=xmb`` writes an XMB *source* file and Angular reads its
translations from XTB files (angular.dev/guide/i18n/translation-files):

- XMB: ``<messagebundle>`` (after a ``<!DOCTYPE messagebundle [...]>`` internal subset)
  holding ``<msg id="..." desc="..." meaning="...">`` elements. A message carries
  ``<source>file:line</source>`` locations (never translated) and ``<ph name="X"/>``
  placeholders; the compiler-era serializer writes ``<ph name="X"><ex>example</ex>original
  </ph>`` instead. Either way the whole ``<ph>`` is ONE protected token.
- XTB: ``<translationbundle lang="fr">`` holding ``<translation id="...">`` elements whose
  content is text plus empty ``<ph name="X"/>`` placeholders.

Upload an XMB and every export is an XTB for the target language (``lang`` set, one
``<translation>`` per message id, ids copied byte for byte: Angular computes them from the
text and meaning, so they are never recomputed or normalised here). Upload an XTB and the
export is that XTB with each translation spliced in and ``lang`` switched, every other
byte unchanged.

Placeholders: a ``START_<X>`` ph and its matching ``CLOSE_<X>`` ph (properly nested)
become a paired token ``⟦gN⟧...⟦/gN⟧``; every other ph is a point token ``⟦xN⟧``. The
``desc`` and ``meaning`` attributes and the placeholder examples become the segment note.

ICU expressions (``{VAR_PLURAL, plural, =0 {...} other {...}}``) are message TEXT in both
formats and stay text: the translator edits the case bodies and keeps the keywords.

Safety: hardened lxml (no entity resolution, no DTD load, no network, no huge tree). The
XMB DOCTYPE and its internal subset are kept in the file but never resolved: an
``<!ENTITY`` declaration (internal or parameter) or an external DTD (SYSTEM/PUBLIC) is
refused, as is any root other than messagebundle/translationbundle. Pre-parse markup
budget and the shared segment caps apply.

Documented in: docs/architecture/translation-pipeline.md
"""

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

_BOM = b"\xef\xbb\xbf"
_XMB_ROOT, _XMB_MSG = "messagebundle", "msg"
_XTB_ROOT, _XTB_MSG = "translationbundle", "translation"
_ENTITY_DECL_RE = re.compile(rb"<!ENTITY")
# Markup tokens in a document lxml already accepted (copied from the TTML splice walker).
_TOKEN_RE = re.compile(
    r"<!--.*?-->|<!\[CDATA\[.*?\]\]>|<\?.*?\?>|</[^>]*>|<(?:\"[^\"]*\"|'[^']*'|[^'\">])*>",
    re.S,
)
_LANG_ATTR_RE = re.compile(r"""(\slang\s*=\s*)(["'])(.*?)\2""", re.S)
_NAME_RE = re.compile(r"<\s*([^\s/>]+)")
_NOTE_MAX = 2000


class AngularXmbError(ValueError):
    """An .xmb/.xtb upload that is not a usable Angular message bundle. Plain message."""


def check_xmb_budget(raw: bytes) -> None:
    """Pre-parse budget (#571 pattern): bound the tree lxml would build before it builds
    it. Forty markup characters per allowed segment is far above any real bundle."""
    if raw.count(b"<") > 40 * config.MAX_DOCUMENT_SEGMENTS:
        raise InputLimitError(
            "This XMB/XTB file has far more markup than MAX_DOCUMENT_SEGMENTS allows "
            f"({config.MAX_DOCUMENT_SEGMENTS}). Split it into smaller files."
        )


def _parse_tree(raw: bytes):
    """Hardened parse. The DOCTYPE stays in the file but nothing in it is resolved."""
    # Shared core parser (gate): never vendor_xliff, which is feature-gated (verify_features).
    from app.filters.xml_guard import make_hardened_parser as _make_parser

    if _ENTITY_DECL_RE.search(raw):
        raise AngularXmbError(
            "XMB/XTB file declares an ENTITY; refused (entity expansion / XXE vector)."
        )
    try:
        root = etree.fromstring(raw, parser=_make_parser())
    except etree.XMLSyntaxError as exc:
        raise AngularXmbError(f"XMB/XTB file is not well-formed XML: {exc}") from exc
    info = root.getroottree().docinfo
    if info.public_id or info.system_url or info.externalDTD is not None:
        raise AngularXmbError("XMB/XTB file points at an external DTD; refused.")
    if not isinstance(root.tag, str) or root.tag not in (_XMB_ROOT, _XTB_ROOT):
        raise AngularXmbError(
            "not an Angular message bundle: the root element must be <messagebundle> "
            "(XMB) or <translationbundle> (XTB)."
        )
    return root


def _pair_names(names: list[str]) -> dict[int, int]:
    """{start_index: close_index} for START_X ... CLOSE_X phs that nest properly."""
    pairs: dict[int, int] = {}
    stack: list[tuple[str, int]] = []
    for i, nm in enumerate(names):
        if nm.startswith("START_"):
            stack.append((nm[6:], i))
        elif nm.startswith("CLOSE_") and stack and stack[-1][0] == nm[6:]:
            pairs[stack.pop()[1]] = i
    return pairs


def _ph_example(ph) -> str:
    ex = ph.find("ex")
    if ex is not None:
        return "".join(ex.itertext())
    return ""


class _Msg:
    """One <msg>/<translation>: id, unit text (edge whitespace stripped), the edges, the
    inline map and the note."""

    __slots__ = ("id", "inline", "lead", "note", "span", "text", "trail")

    def __init__(self, el, raw_of):
        self.id = el.get("id")
        pieces: list[tuple[str, object]] = [("t", el.text or "")]
        for child in el:
            if not isinstance(child.tag, str):  # comment / PI: keep verbatim (XTB splice)
                pieces.append(("raw", raw_of(child)))
            elif child.tag == "source":
                pass  # an XMB location, never translated
            elif child.tag == "ph":
                pieces.append(("ph", child))
            else:
                pieces.append(("raw", raw_of(child)))
            pieces.append(("t", child.tail or ""))
        names = [p[1].get("name") or "" for p in pieces if p[0] == "ph"]
        pairs = _pair_names(names)
        closes = {c: s for s, c in pairs.items()}
        inline: dict = {}
        out: list[str] = []
        examples: list[str] = []
        n = 0
        ph_i = -1
        token_of: dict[int, int] = {}
        for kind, val in pieces:
            if kind == "t":
                out.append(val)
                continue
            if kind == "raw":
                n += 1
                inline[str(n)] = {"kind": "raw", "raw": val}
                out.append(ph_point("x", n))
                continue
            ph_i += 1
            name = val.get("name") or ""
            ex = _ph_example(val)
            if ph_i in closes:
                k = token_of[closes[ph_i]]
                inline[str(k)]["close"] = name
                inline[str(k)]["close_raw"] = raw_of(val)
                out.append(ph_close("g", k))
                continue
            n += 1
            if ph_i in pairs:
                token_of[ph_i] = n
                inline[str(n)] = {"kind": "pair", "open": name, "open_raw": raw_of(val)}
                out.append(ph_open("g", n))
            else:
                inline[str(n)] = {"kind": "ph", "name": name, "raw": raw_of(val)}
                out.append(ph_point("x", n))
            if ex:
                examples.append(f"{name} = {ex}")
        joined = "".join(out)
        self.text = joined.strip()
        self.lead = joined[: len(joined) - len(joined.lstrip())]
        self.trail = joined[len(joined.rstrip()) :] if self.text else ""
        self.inline = inline
        notes = []
        if el.get("desc"):
            notes.append(f"Description: {el.get('desc')}")
        if el.get("meaning"):
            notes.append(f"Meaning: {el.get('meaning')}")
        if examples:
            notes.append("Placeholders: " + "; ".join(examples))
        self.note = " | ".join(notes)[:_NOTE_MAX]
        self.span = None  # (content_start, content_end) in the decoded text (XTB splice)


def _escape_text(s: str) -> str:
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _escape_attr(s: str) -> str:
    return _escape_text(s).replace('"', "&quot;")


def _render(text: str, inline: dict, *, original_raw: bool) -> str:
    """Translated unit text -> message content. XMB->XTB writes each ph as an empty
    ``<ph name="X"/>`` (XTB carries no examples); an XTB splice reuses the original ph
    markup. Tolerant like the XLIFF/TTML rebuild: an unknown or repeated token and a stray
    close are dropped (never written as literal brackets) and an unclosed pair is closed."""
    from app.filters.xml_text import xml_target_text

    text = xml_target_text(text)

    def ph(name: str, raw: str | None) -> str:
        if original_raw and raw:
            return raw
        return f'<ph name="{_escape_attr(name)}"/>'

    out: list[str] = []
    used: set[str] = set()
    stack: list[tuple[str, str | None]] = []
    pos = 0
    for m in _PLACEHOLDER_RE.finditer(text):
        out.append(_escape_text(text[pos : m.start()]))
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
        if entry is None or key in used or (entry["kind"] == "pair") == is_point:
            if not is_point:
                stack.append((key, None))
            continue
        used.add(key)
        if entry["kind"] == "raw":
            out.append(entry["raw"] if original_raw else "")
        elif entry["kind"] == "ph":
            out.append(ph(entry["name"], entry.get("raw")))
        else:
            out.append(ph(entry["open"], entry.get("open_raw")))
            close = ph(entry.get("close", ""), entry.get("close_raw"))
            stack.append((key, close))
    out.append(_escape_text(text[pos:]))
    for _k, close in reversed(stack):
        if close is not None:
            out.append(close)
    return "".join(out)


class _Doc:
    """A parsed bundle: decoded text, root, its messages in document order, and (for the
    splice) the byte-free character offsets of every element."""

    def __init__(self, data: bytes, *, upload: bool):
        raw = bytes(data)
        self.bom = raw.startswith(_BOM)
        if upload:
            check_xmb_budget(raw)
            self.text, self.enc = read_text_upload(raw, sig=True, declared=True)
        else:
            self.text, self.enc = decode_text_lossless(raw, sig=True, declared=True), "utf-8"
        self.root = _parse_tree(encode_text_lossless(self.text))
        self.kind = "xmb" if self.root.tag == _XMB_ROOT else "xtb"
        self._index_elements()
        msg_tag = _XMB_MSG if self.kind == "xmb" else _XTB_MSG
        self.msgs: list[_Msg] = []
        for el in self.root:
            if isinstance(el.tag, str) and el.tag == msg_tag:
                m = _Msg(el, self._raw_of)
                m.span = (self._pos[el][1], self._pos[el][2])
                self.msgs.append(m)

    def _index_elements(self) -> None:
        """Offsets of every element: [tag_start, tag_end, content_end, element_end]. The
        light tokenizer only runs on text lxml accepted, and it must agree with lxml
        element for element, or the file is refused."""
        tags: list[list[int]] = []
        stack: list[int] = []
        for m in _TOKEN_RE.finditer(self.text):
            tok = m.group(0)
            if tok.startswith(("<!", "<?")):
                continue
            if tok.startswith("</"):
                if not stack:
                    raise AngularXmbError("XMB/XTB structure could not be read; refused.")
                i = stack.pop()
                tags[i][2], tags[i][3] = m.start(), m.end()
                continue
            tags.append([m.start(), m.end(), m.end(), m.end()])
            if not tok.endswith("/>"):
                stack.append(len(tags) - 1)
        elements = [el for el in self.root.iter() if isinstance(el.tag, str)]
        if stack or len(tags) != len(elements):
            raise AngularXmbError("XMB/XTB structure could not be read consistently; refused.")
        self._pos = dict(zip(elements, tags, strict=True))
        self.root_tag = (tags[0][0], tags[0][1])

    def _raw_of(self, node) -> str:
        if isinstance(node.tag, str):
            t = self._pos[node]
            return self.text[t[0] : t[3]]
        return etree.tostring(node, with_tail=False, encoding="unicode")


def _parse(data: bytes) -> ParseResult:
    doc = _Doc(data, upload=True)
    units: list[TextUnit] = []
    notes: list[str] = []
    seen: set[str] = set()
    for idx, m in enumerate(doc.msgs):
        if m.id is None or m.id in seen:
            continue  # no key / a duplicate id: Angular keeps only the first
        seen.add(m.id)
        if not _PLACEHOLDER_RE.sub("", m.text).strip():
            continue  # placeholder-only message: copied as is on export
        units.append(TextUnit(p_index=idx, text=m.text, inline_map=m.inline))
        notes.append(m.note)
        check_segment_cap(len(units))  # #571
    check_unit_texts(u.text for u in units)
    skeleton = {
        "format": doc.kind,
        "units": [
            {"p_index": u.p_index, "text": u.text, "style": "", "note": note}
            for u, note in zip(units, notes, strict=True)
        ],
    }
    return ParseResult(skeleton=note_source_encoding(skeleton, doc.enc), units=units)


def _xtb_from_xmb(doc: _Doc, paragraph_texts: dict, lang: str | None) -> bytes:
    """A fresh XTB: one <translation> per distinct message id, in XMB order."""
    out = ['<?xml version="1.0" encoding="UTF-8"?>\n<!DOCTYPE translationbundle>\n']
    out.append(
        f'<translationbundle lang="{_escape_attr(lang)}">\n' if lang else "<translationbundle>\n"
    )
    seen: set[str] = set()
    for idx, m in enumerate(doc.msgs):
        if m.id is None or m.id in seen:
            continue
        seen.add(m.id)
        new = paragraph_texts.get(str(idx))
        body = _render(m.text if new is None else new, m.inline, original_raw=False)
        out.append(
            f'  <translation id="{_escape_attr(m.id)}">{m.lead}{body}{m.trail}</translation>\n'
        )
    out.append("</translationbundle>\n")
    return "".join(out).encode("utf-8")


def _splice_xtb(doc: _Doc, paragraph_texts: dict, lang: str | None) -> bytes:
    """The uploaded XTB with changed translations spliced in; everything else verbatim."""
    edits: list[tuple[int, int, str]] = []
    seen: set[str] = set()
    for idx, m in enumerate(doc.msgs):
        if m.id is None or m.id in seen:
            continue
        seen.add(m.id)
        new = paragraph_texts.get(str(idx))
        if new is None or new == m.text:
            continue
        start, end = m.span
        if start == end and doc.text[start - 2 : start] == "/>":
            # <translation id="x"/>: open it up so content can go in.
            tag_start = doc.text.rfind("<", 0, start)
            head = doc.text[tag_start : start - 2].rstrip()
            body = _render(new, m.inline, original_raw=True)
            edits.append((tag_start, start, f"{head}>{body}</{_XTB_MSG}>"))
            continue
        body = _render(new, m.inline, original_raw=True)
        edits.append((start, end, m.lead + body + m.trail))
    if lang:
        rs, re_ = doc.root_tag
        head = doc.text[rs:re_]
        lm = _LANG_ATTR_RE.search(head)
        if lm:
            edits.append((rs + lm.start(3), rs + lm.end(3), _escape_attr(lang)))
        else:
            at = rs + _NAME_RE.match(head).end()
            edits.append((at, at, f' lang="{_escape_attr(lang)}"'))
    pieces: list[str] = []
    pos = 0
    for start, end, rep in sorted(edits, key=lambda e: e[0]):
        pieces.append(doc.text[pos:start])
        pieces.append(rep)
        pos = end
    pieces.append(doc.text[pos:])
    out = encode_text_lossless("".join(pieces))
    return _BOM + out if doc.bom else out


def _build(original: bytes, paragraph_texts: dict, lang: str | None) -> bytes:
    doc = _Doc(original, upload=False)
    if doc.kind == "xmb":
        return _xtb_from_xmb(doc, paragraph_texts, lang)
    return _splice_xtb(doc, paragraph_texts, lang)


class _BundleFilter(Filter):
    def parse(self, data: bytes, provider: str | None = None) -> ParseResult:
        return _parse(data)

    def build(self, original: bytes, paragraph_texts: dict) -> bytes:
        return _build(original, paragraph_texts, None)

    def build_localized(
        self, original: bytes, paragraph_texts: dict, target_locale: str | None = None
    ) -> bytes:
        """build() plus the TARGET language in ``lang`` on <translationbundle>."""
        from app.filters import a11y

        return _build(original, paragraph_texts, a11y.target_tag(target_locale) or None)


@register
class XmbFilter(_BundleFilter):
    """Angular XMB source bundle; exports XTB (see EXPORT_EXT_BY_FMT in services/mime)."""

    fmt = "xmb"
    passthrough_untouched = False  # B-bytes: the deliverable is a different file


@register
class XtbFilter(_BundleFilter):
    """Angular / Google XTB translation bundle; exports XTB."""

    fmt = "xtb"
    writes_targets = True  # B-bytes: a target equal to its source is a real write
