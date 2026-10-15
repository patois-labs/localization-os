"""OpenDocument filter (format Batch 12): .odt/.ott text, .ods/.ots spreadsheets,
.odp/.otp presentations and .odg/.otg drawings.

An OpenDocument file is a zip package (OASIS ODF 1.3 Part 2) whose text lives in
``content.xml`` (plus header/footer text in ``styles.xml`` master pages). One walker
serves every family because all of them carry their text in the same paragraph
elements, ``text:p`` and ``text:h`` (ODF 1.3 Part 3, 5.1.3 / 5.1.2):

  * Writer: body paragraphs and headings, tables, lists, footnotes/endnotes
    (``text:note-body``), text frames, and header/footer paragraphs in styles.xml.
  * Calc: the paragraphs of STRING cells only. A cell with a ``table:formula`` or a
    non-string ``office:value-type`` (float, date, percentage, currency, time,
    boolean) is skipped: its paragraph is only a rendering of the value. A repeated
    cell (``table:number-columns-repeated``) is one element, so one unit.
  * Impress/Draw: text boxes and shapes on every page, and speaker notes
    (``presentation:notes``). Master-page placeholders are not translated.

Skipped everywhere: tracked-change regions (``text:tracked-changes`` holds DELETED
text) and index source templates. Comments (``office:annotation``, ODF 1.3 Part 3
14.1) are units only when the project's "translate comments" option is on (#690,
parse(comments=True)): each paragraph of each annotation, content.xml then styles.xml in
document order (a comment on a number cell in Calc too), styled "comment" at
office_comments.COMMENT_BASE + j. The annotation's ``dc:creator``, ``dc:date`` and
``meta:creator-initials`` are never units. The annotation element itself rides in its
host paragraph as a point token, so the host's rebuild keeps it whole and in place.

Inline content (one TextUnit per paragraph, docx/xliff token scheme from
services/segment): ``text:span``, ``text:a`` and ``text:meta`` become paired tokens
⟦gN⟧…⟦/gN⟧; every other child element (``text:tab``, ``text:line-break``, bookmarks,
fields, note anchors, frames, change marks, soft page breaks, unknown elements)
becomes a point token ⟦xN⟧ and is kept WHOLE. A paragraph nested inside a point
element (a footnote body, a text box in a frame) is its own unit. build() moves the
ORIGINAL elements back into place (never copies), so nested paragraphs, styles and
every attribute survive, and parse and build enumerate paragraphs in the same order.

White space follows ODF 1.3 Part 3 6.1.2: in paragraph text, tab/CR/LF are spaces,
runs collapse to one space, leading spaces are dropped, and ``text:s`` (6.1.3,
``text:c`` repeats) carries the second and following spaces. parse() decodes that to
plain spaces; build() encodes the translated text back (``text:s`` for extra spaces,
``text:tab`` / ``text:line-break`` for a tab or newline the translation adds).

Packaging (Part 2 3.3): the ``mimetype`` member stays first, STORED, with no extra
field; every other member keeps its name, order, compression and bytes, and only the
XML parts that really changed are re-serialised. A build with no translation returns
the original bytes. Password-protected packages (``manifest:encryption-data``) are
refused with a plain message.

Security: zip_guard (bombs, member count, forged sizes) before the package is read; a
DOCTYPE is refused and lxml parses with entities, DTD loading and network off and
huge_tree off; a markup budget bounds the tree before it is built; the segment caps
apply while walking.

build_localized() writes the target language into the default styles (``fo:language``
/ ``fo:country``, or the ``-asian`` / ``-complex`` slot for East Asian and right-to-left
targets, ODF 20.223 / 20.341 / 20.342), into every style that already declares one,
into meta.xml ``dc:language``, and sets ``style:writing-mode="rl-tb"`` on the default
paragraph AND graphic styles for a right-to-left target, and turns an explicit horizontal
left-to-right mode on paragraph / graphic / presentation styles right to left (frame
text: ODP / ODG, ODT text boxes, #724). A spreadsheet (ODS / OTS) into a right-to-left
target also has every sheet turned right to left (table style ``style:writing-mode="rl-tb"``,
column A on the right; B-rtl decision 17).

Documented in: docs/architecture/translation-pipeline.md
"""

from __future__ import annotations

import io
import re
import zipfile

from lxml import etree

from app import config
from app.filters import zip_guard
from app.filters.base import (
    Filter,
    InputLimitError,
    ParseResult,
    TextUnit,
    check_segment_cap,
    check_unit_texts,
    register,
)
from app.services.segment import _PLACEHOLDER_RE, ph_close, ph_open, ph_point

NS = {
    "office": "urn:oasis:names:tc:opendocument:xmlns:office:1.0",
    "style": "urn:oasis:names:tc:opendocument:xmlns:style:1.0",
    "text": "urn:oasis:names:tc:opendocument:xmlns:text:1.0",
    "table": "urn:oasis:names:tc:opendocument:xmlns:table:1.0",
    "draw": "urn:oasis:names:tc:opendocument:xmlns:drawing:1.0",
    "presentation": "urn:oasis:names:tc:opendocument:xmlns:presentation:1.0",
    "fo": "urn:oasis:names:tc:opendocument:xmlns:xsl-fo-compatible:1.0",
    "meta": "urn:oasis:names:tc:opendocument:xmlns:meta:1.0",
    "dc": "http://purl.org/dc/elements/1.1/",
    "manifest": "urn:oasis:names:tc:opendocument:xmlns:manifest:1.0",
}
_T = f"{{{NS['text']}}}"
_TABLE = f"{{{NS['table']}}}"
_OFFICE = f"{{{NS['office']}}}"
_STYLE = f"{{{NS['style']}}}"
_FO = f"{{{NS['fo']}}}"

_PAIRED = {_T + "span", _T + "a", _T + "meta"}
_SPACE = _T + "s"
# Ancestors whose paragraphs are never translated (deleted text, comments, templates).
_SKIP_ANCESTORS = {
    _T + "tracked-changes",
    _OFFICE + "annotation",
    _T + "index-title-template",
    _T + "table-of-content-entry-template",
    _T + "illustration-index-entry-template",
    _T + "table-index-entry-template",
    _T + "object-index-entry-template",
    _T + "user-index-entry-template",
    _T + "alphabetical-index-entry-template",
    _T + "bibliography-entry-template",
}
# styles.xml: only header/footer paragraphs of master pages are text a reader sees.
_HEADER_FOOTER = {
    _STYLE + n
    for n in (
        "header",
        "footer",
        "header-left",
        "footer-left",
        "header-first",
        "footer-first",
    )
}
_STRING_TYPES = {None, "string"}
_MAX_SPACES = 1000  # a hostile text:c="999999999" is not expanded past this
_DOCTYPE_RE = re.compile(rb"<!DOCTYPE", re.IGNORECASE)
_WS_RE = re.compile(r"[ \t\r\n]+")
_ENCODE_RE = re.compile(r" {2,}|^ +|\t|\n")
_PARTS = ("content.xml", "styles.xml")


# --------------------------------------------------------------------------- package


def _refuse(msg: str) -> InputLimitError:
    return InputLimitError(msg)


def _open_package(data: bytes) -> zipfile.ZipFile:
    data = bytes(data)
    zip_guard.check(data, "OpenDocument file")
    try:
        zf = zipfile.ZipFile(io.BytesIO(data))
    except (zipfile.BadZipFile, ValueError, OSError) as exc:
        raise _refuse(
            "This file is not a valid OpenDocument package (it is not a zip file). "
            "Save it again in LibreOffice."
        ) from exc
    names = set(zf.namelist())
    if "content.xml" not in names:
        zf.close()
        raise _refuse("This OpenDocument file has no content.xml. Save it again in LibreOffice.")
    if "mimetype" in names:
        mt = zf.read("mimetype")[:200].decode("ascii", "replace").strip()
        if not mt.startswith("application/vnd.oasis.opendocument."):
            zf.close()
            raise _refuse(
                "This file is not an OpenDocument document (its mimetype is not an "
                "OpenDocument type)."
            )
    manifest = zf.read("META-INF/manifest.xml") if "META-INF/manifest.xml" in names else b""
    if b"encryption-data" in manifest:
        zf.close()
        raise _refuse(
            "This OpenDocument file is password-protected. Save a copy without a password "
            "and upload that."
        )
    return zf


def check_odf_budget(raw: bytes) -> None:
    """Pre-parse budget for one XML part: forty tags per allowed segment (the same
    bound as xliff_filter.check_markup_budget), counted at C speed before lxml builds
    a tree roughly ten times the part's size."""
    if raw.count(b"<") > 40 * config.MAX_DOCUMENT_SEGMENTS:
        raise _refuse(
            "This OpenDocument file has far more markup than MAX_DOCUMENT_SEGMENTS allows "
            f"({config.MAX_DOCUMENT_SEGMENTS}). Split it into smaller files."
        )


def _parser() -> etree.XMLParser:
    return etree.XMLParser(
        resolve_entities=False,
        no_network=True,
        load_dtd=False,
        dtd_validation=False,
        huge_tree=False,
        remove_blank_text=False,
        strip_cdata=False,
    )


def _parse_xml(raw: bytes, budget: bool = True):
    if _DOCTYPE_RE.search(raw[:4096]):
        raise _refuse("This OpenDocument file declares a DOCTYPE; it was not processed.")
    if budget:
        check_odf_budget(raw)
    try:
        root = etree.fromstring(raw, parser=_parser())
    except etree.XMLSyntaxError as exc:
        raise _refuse(
            "This OpenDocument file has invalid XML. Save it again in LibreOffice."
        ) from exc
    info = root.getroottree().docinfo
    if info.doctype or info.internalDTD is not None or info.externalDTD is not None:
        raise _refuse("This OpenDocument file declares a DOCTYPE; it was not processed.")
    return root


def _serialize(root) -> bytes:
    return etree.tostring(root, xml_declaration=True, encoding="UTF-8")


def _write_package(zf: zipfile.ZipFile, replaced: dict) -> bytes:
    """Rewrite the package: mimetype first and STORED with no extra field (Part 2 3.3),
    every other member in its original order with its original compression."""
    out = io.BytesIO()
    infos = zf.infolist()
    infos = [i for i in infos if i.filename == "mimetype"] + [
        i for i in infos if i.filename != "mimetype"
    ]
    with zipfile.ZipFile(out, "w") as zout:
        for info in infos:
            data = replaced.get(info.filename)
            if data is None:
                data = zf.read(info.filename)
            new = zipfile.ZipInfo(info.filename, date_time=info.date_time)
            new.external_attr = info.external_attr
            new.create_system = info.create_system
            if info.filename == "mimetype":
                new.compress_type = zipfile.ZIP_STORED
            else:
                new.compress_type = info.compress_type
            zout.writestr(new, data)
    return out.getvalue()


# --------------------------------------------------------------------------- walk


def _cell_is_text(cell) -> bool:
    if cell.get(_TABLE + "formula") is not None:
        return False
    return cell.get(_OFFICE + "value-type") in _STRING_TYPES


def _paragraphs(root, part: str):
    """Every translatable paragraph element of one part, in document order. A paragraph
    nested in another paragraph (footnote, text box) is listed after its host."""
    out = []
    for el in root.iter(_T + "p", _T + "h"):
        skip = False
        in_hf = part != "styles.xml"
        anc = el.getparent()
        while anc is not None:
            tag = anc.tag
            if tag in _SKIP_ANCESTORS or (
                isinstance(tag, str) and tag.startswith(_T) and tag.endswith("-source")
            ):
                skip = True
                break
            if tag == _TABLE + "table-cell" and not _cell_is_text(anc):
                skip = True
                break
            if tag in _HEADER_FOOTER:
                in_hf = True
            anc = anc.getparent()
        if not skip and in_hf:
            out.append(el)
    return out


def _space_count(el) -> int:
    try:
        n = int(el.get(_T + "c") or 1)
    except ValueError:
        n = 1
    return max(1, min(n, _MAX_SPACES))


def _tokenize(p):
    """(text, inline_map) for one paragraph: inline_map[str(n)] = the ORIGINAL element.
    White space is decoded per ODF 6.1.2; a token resets the space-collapse state."""
    inline_map: dict = {}
    parts: list[str] = []
    state = {"n": 0, "started": False, "space": False, "len": 0}

    def grow(k):
        # #571: a paragraph of many text:s elements each asking for _MAX_SPACES spaces
        # must stop at the per-segment cap, not build a multi-gigabyte string first.
        state["len"] += k
        if state["len"] > config.MAX_SEGMENT_CHARS:
            raise _refuse(
                f"This file has a paragraph longer than {config.MAX_SEGMENT_CHARS} "
                "characters (MAX_SEGMENT_CHARS). Break it into shorter paragraphs."
            )

    def chars(s):
        if not s:
            return
        s = _WS_RE.sub(" ", s)
        if s.startswith(" ") and (state["space"] or not state["started"]):
            s = s[1:]
        if not s:
            return
        grow(len(s))
        parts.append(s)
        state["space"] = s.endswith(" ")
        state["started"] = True

    def token(t):
        parts.append(t)
        state["space"] = False

    def walk(el):
        for child in el:
            tag = child.tag
            if tag == _SPACE:
                k = _space_count(child)
                grow(k)
                parts.append(" " * k)
                state["space"] = False
                state["started"] = True
            elif not isinstance(tag, str):
                # comment / processing instruction: kept as a point so build restores it
                state["n"] += 1
                inline_map[str(state["n"])] = child
                token(ph_point("x", state["n"]))
            elif tag in _PAIRED:
                state["n"] += 1
                n = state["n"]
                inline_map[str(n)] = child
                token(ph_open("g", n))
                chars(child.text)
                walk(child)
                token(ph_close("g", n))
            else:
                state["n"] += 1
                inline_map[str(state["n"])] = child
                token(ph_point("x", state["n"]))
            chars(child.tail)

    chars(p.text)
    walk(p)
    return "".join(parts), inline_map


def _is_translatable(text: str) -> bool:
    return any(c.isalpha() for c in _PLACEHOLDER_RE.sub("", text))


def _annotation_paragraphs(parts) -> list:
    """#690: [(part, paragraph)] of every comment (office:annotation) paragraph, in
    document order, numbered from office_comments.COMMENT_BASE. A comment inside a
    tracked deletion is skipped (deleted text)."""
    out = []
    for part, root, _paras in parts:
        for ann in root.iter(_OFFICE + "annotation"):
            if any(a.tag == _T + "tracked-changes" for a in ann.iterancestors()):
                continue
            for p in ann.iter(_T + "p", _T + "h"):
                out.append((part, p))
    return out


def _walk_package(zf: zipfile.ZipFile, budget: bool = True):
    """[(part, root, [paragraph elements])] for content.xml then styles.xml."""
    out = []
    for part in _PARTS:
        if part not in zf.namelist():
            continue
        root = _parse_xml(zf.read(part), budget=budget)
        out.append((part, root, _paragraphs(root, part)))
    return out


# --------------------------------------------------------------------------- rebuild


def _new(tag: str, **attrs):
    el = etree.Element(tag)
    for k, v in attrs.items():
        el.set(k, v)
    return el


class _Writer:
    """Fills one cleared paragraph from translated, tokenized text."""

    def __init__(self, p):
        self.p = p
        self.stack: list[tuple] = []  # (element_or_None, key, phantom)
        self.started = False

    def container(self):
        for el, _key, phantom in reversed(self.stack):
            if not phantom:
                return el
        return self.p

    def _append_raw_text(self, s: str):
        c = self.container()
        if len(c):
            c[-1].tail = (c[-1].tail or "") + s
        else:
            c.text = (c.text or "") + s

    def append_el(self, el):
        el.tail = None
        self.container().append(el)

    def text(self, s: str):
        if not s:
            return
        pos = 0
        for m in _ENCODE_RE.finditer(s):
            single_lead = m.start() == 0 and m.group(0) == " "
            if single_lead and not self.started:
                # a single leading space of the paragraph text: only text:s keeps it
                self.append_el(_new(_SPACE))
                pos = m.end()
                continue
            self._plain(s[pos : m.start()])
            g = m.group(0)
            if g == "\t":
                self.append_el(_new(_T + "tab"))
            elif g == "\n":
                self.append_el(_new(_T + "line-break"))
            else:
                n = len(g)
                lead = 0 if (m.start() == 0 and not self.started) else 1
                if lead:
                    self._plain(" ")
                if n - lead > 0:
                    sp = _new(_SPACE)
                    if n - lead > 1:
                        sp.set(_T + "c", str(n - lead))
                    self.append_el(sp)
            self.started = True
            pos = m.end()
        self._plain(s[pos:])

    def _plain(self, s: str):
        if s:
            self._append_raw_text(s)
            self.started = True


def _clear(el):
    el.text = None
    for c in list(el):
        el.remove(c)


def _rebuild_paragraph(p, text: str) -> None:
    from app.filters.xml_text import xml_target_text

    text = xml_target_text(text)
    _, inline_map = _tokenize(p)
    for el in inline_map.values():
        parent = el.getparent()
        if parent is not None:
            parent.remove(el)
        el.tail = None
    _clear(p)
    w = _Writer(p)
    used: set[str] = set()
    pos = 0
    for m in _PLACEHOLDER_RE.finditer(text):
        w.text(text[pos : m.start()])
        pos = m.end()
        inner = m.group(0)[1:-1]
        if inner.startswith("/"):
            key = re.sub(r"^/[a-z]?", "", inner)
            if w.stack and w.stack[-1][1] == key:
                w.stack.pop()
            continue
        is_point = inner.startswith("x")
        key = re.sub(r"^[a-z]?", "", inner, count=1)
        el = inline_map.get(key)
        paired = el is not None and isinstance(el.tag, str) and el.tag in _PAIRED
        if el is None or key in used or paired == is_point:
            if not is_point:
                w.stack.append((None, key, True))
            continue
        used.add(key)
        if is_point:
            w.append_el(el)
        else:
            _clear(el)
            w.append_el(el)
            w.stack.append((el, key, False))
    w.text(text[pos:])
    # A point element the translation dropped (a footnote, a frame, a bookmark) is kept
    # at the end of the paragraph rather than lost with everything nested in it.
    for key, el in inline_map.items():
        if key in used:
            continue
        if isinstance(el.tag, str) and el.tag in _PAIRED:
            continue  # formatting only; its own point children were detached above
        el.tail = None
        p.append(el)


# --------------------------------------------------------------------------- filter


class _OdfFilter(Filter):
    fmt = "odf"
    label = "OpenDocument file"

    takes_comments = True  # #690: the per-project "translate comments" option

    def parse(
        self, data: bytes, provider: str | None = None, comments: bool = False
    ) -> ParseResult:
        with _open_package(data) as zf:
            parts = _walk_package(zf)
        units: list[TextUnit] = []
        meta: list[dict] = []
        idx = 0
        for part, _root, paras in parts:
            for p in paras:
                text, _map = _tokenize(p)
                if _is_translatable(text):
                    style = "heading" if p.tag == _T + "h" else ""
                    units.append(TextUnit(p_index=idx, text=text, style=style))
                    meta.append({"p_index": idx, "text": text, "style": style, "part": part})
                    check_segment_cap(len(units))
                idx += 1
        skeleton: dict = {"format": self.fmt, "units": meta}
        if comments:
            from app.filters.office_comments import COMMENT_BASE, COMMENT_STYLE

            for j, (part, p) in enumerate(_annotation_paragraphs(parts)):
                text, _map = _tokenize(p)
                if not _is_translatable(text):
                    continue
                units.append(TextUnit(p_index=COMMENT_BASE + j, text=text, style=COMMENT_STYLE))
                meta.append(
                    {
                        "p_index": COMMENT_BASE + j,
                        "text": text,
                        "style": COMMENT_STYLE,
                        "part": part,
                    }
                )
                check_segment_cap(len(units))
            skeleton["comments"] = True
        check_unit_texts(u.text for u in units)
        return ParseResult(skeleton=skeleton, units=units)

    def _rebuild(self, original: bytes, paragraph_texts: dict, target_locale=None) -> bytes:
        with _open_package(original) as zf:
            parts = _walk_package(zf, budget=False)
            replaced: dict = {}
            from app.filters.office_comments import COMMENT_BASE, has_comment_keys

            # #690: the comment paragraphs, listed BEFORE any host paragraph is rebuilt
            notes = _annotation_paragraphs(parts) if has_comment_keys(paragraph_texts) else []
            idx = 0
            for part, root, paras in parts:
                changed = False
                for p in paras:
                    new = paragraph_texts.get(str(idx))
                    idx += 1
                    if new is None:
                        continue
                    _rebuild_paragraph(p, new)
                    changed = True
                if changed:
                    replaced[part] = root
            for j, (part, p) in enumerate(notes):
                new = paragraph_texts.get(str(COMMENT_BASE + j))
                if new is None:
                    continue
                _rebuild_paragraph(p, new)
                replaced[part] = next(r for name, r, _ps in parts if name == part)
            if target_locale is not None:
                _stamp_language(zf, parts, replaced, target_locale)
            if not replaced:
                return bytes(original)
            blobs = {
                k: (_serialize(v) if not isinstance(v, bytes) else v) for k, v in replaced.items()
            }
            return _write_package(zf, blobs)

    def build(self, original: bytes, paragraph_texts: dict) -> bytes:
        return self._rebuild(original, paragraph_texts)

    def build_localized(
        self, original: bytes, paragraph_texts: dict, target_locale: str | None = None
    ) -> bytes:
        return self._rebuild(original, paragraph_texts, target_locale or "")


# --------------------------------------------------------------------------- language


def _lang_attrs(tag: str):
    """(language, country, script) attribute names + values for the target's slot."""
    from app.filters import a11y

    subtags = tag.split("-")
    lang = subtags[0].lower()
    script = next((s for s in subtags[1:] if len(s) == 4 and s.isalpha()), None)
    country = next(
        (s for s in subtags[1:] if (len(s) == 2 and s.isalpha()) or (len(s) == 3 and s.isdigit())),
        None,
    )
    slot = a11y.script_class(tag)
    if slot == "eastAsia":
        names = (_STYLE + "language-asian", _STYLE + "country-asian", _STYLE + "script-asian")
    elif slot == "bidi":
        names = (_STYLE + "language-complex", _STYLE + "country-complex", _STYLE + "script-complex")
    else:
        names = (_FO + "language", _FO + "country", _FO + "script")
    return names, (lang, country.upper() if country else None, script.title() if script else None)


def _set_lang(props, names, values):
    for name, value in zip(names, values, strict=True):
        if value:
            props.set(name, value)
        elif name in props.attrib:
            del props.attrib[name]


# #724 (gate): modern LibreOffice writes an explicit `style:writing-mode="lr-tb"` on the
# paragraph-properties of EVERY shape's graphic / presentation style and of the automatic
# paragraph styles inside them (a pptx saved as ODP by LibreOffice 25: 140 in one deck). An
# explicit mode overrides the default style, so flipping the defaults alone left every such
# slide left to right. For a right-to-left target the horizontal left-to-right modes turn
# right to left in paragraph / graphic / presentation styles of every part (the pptx filter
# likewise sets rtl="1" on every translated paragraph). Vertical modes (tb-rl, tb-lr, tb) and
# "page" are kept; alignment (fo:text-align) is not mirrored, as in docx / pptx.
_FRAME_FAMILIES = ("paragraph", "graphic", "presentation")
_RTL_FLIP = {"lr-tb": "rl-tb", "lr": "rl"}


def _flip_frame_styles_rtl(root) -> bool:
    flipped = False
    for st in root.iter(_STYLE + "style"):
        if st.get(_STYLE + "family") not in _FRAME_FAMILIES:
            continue
        for pp in st.iterfind(_STYLE + "paragraph-properties"):
            new = _RTL_FLIP.get(pp.get(_STYLE + "writing-mode"))
            if new:
                pp.set(_STYLE + "writing-mode", new)
                flipped = True
    return flipped


def _flip_sheets_rtl(root) -> bool:
    """Right-to-left target, spreadsheet (B-rtl, decision 17): every sheet of
    office:spreadsheet turns right to left, so column A sits on the right. A sheet's direction
    is `style:writing-mode` on the `style:table-properties` of its table style (ODF 1.3
    Part 3; LibreOffice writes rl-tb there for Sheet > Right-To-Left). A
    sheet with no automatic table style gets a new one (parent: its common style, if any).
    Text documents' tables are not touched (only office:spreadsheet sheets)."""
    body = root.find(_OFFICE + "body")
    sheet_body = body.find(_OFFICE + "spreadsheet") if body is not None else None
    if sheet_body is None:
        return False
    tables = sheet_body.findall(_TABLE + "table")
    if not tables:
        return False
    autos = root.find(_OFFICE + "automatic-styles")
    if autos is None:
        autos = etree.Element(_OFFICE + "automatic-styles")
        body.addprevious(autos)
    by_name = {
        st.get(_STYLE + "name"): st
        for st in autos.iterfind(_STYLE + "style")
        if st.get(_STYLE + "family") == "table"
    }
    taken = {st.get(_STYLE + "name") for st in autos.iter(_STYLE + "style")}
    made: dict = {}
    for table in tables:
        name = table.get(_TABLE + "style-name")
        st = by_name.get(name)
        if st is None:
            if name not in made:
                n = 1
                while f"taRtl{n}" in taken:
                    n += 1
                new_name = f"taRtl{n}"
                taken.add(new_name)
                st = etree.SubElement(autos, _STYLE + "style")
                st.set(_STYLE + "name", new_name)
                st.set(_STYLE + "family", "table")
                if name:
                    st.set(_STYLE + "parent-style-name", name)
                made[name] = st
            st = made[name]
            table.set(_TABLE + "style-name", st.get(_STYLE + "name"))
        tp = st.find(_STYLE + "table-properties")
        if tp is None:
            tp = etree.Element(_STYLE + "table-properties")
            st.insert(0, tp)  # table-properties is the only child a table style may hold
        tp.set(_STYLE + "writing-mode", "rl-tb")
    return True


def _stamp_language(zf, parts, replaced: dict, locale: str) -> None:
    from app.filters import a11y

    tag = a11y.target_tag(locale)
    if not tag:
        return
    names, values = _lang_attrs(tag)
    rtl = a11y.target_direction(tag) == "rtl"
    for part, root, _paras in parts:
        touched = False
        for props in root.iter(_STYLE + "text-properties"):
            # "zxx"/"none" is LibreOffice's "[None]" language (code, URLs): it switches
            # proofing off on purpose, so a localized build leaves it alone.
            if props.get(names[0]) not in (None, "zxx", "none"):
                _set_lang(props, names, values)
                touched = True
        if part == "styles.xml":
            for ds in root.iter(_STYLE + "default-style"):
                if ds.get(_STYLE + "family") not in ("paragraph", "graphic"):
                    continue
                props = ds.find(_STYLE + "text-properties")
                if props is None:
                    props = etree.SubElement(ds, _STYLE + "text-properties")
                _set_lang(props, names, values)
                # #724: text in drawing frames (every ODP / ODG text, ODT text boxes) takes its
                # paragraph direction from the GRAPHIC default style, so both defaults turn
                # right to left (an ODP usually has no paragraph default style at all, and its
                # graphic default says lr-tb: Hebrew slides stayed left to right).
                if rtl:
                    pp = ds.find(_STYLE + "paragraph-properties")
                    if pp is None:
                        pp = etree.Element(_STYLE + "paragraph-properties")
                        # schema order: graphic-properties?, paragraph-properties?, text-properties?
                        gp = ds.find(_STYLE + "graphic-properties")
                        ds.insert(ds.index(gp) + 1 if gp is not None else 0, pp)
                    pp.set(_STYLE + "writing-mode", "rl-tb")
                touched = True
        if rtl and _flip_frame_styles_rtl(root):
            touched = True
        if rtl and part == "content.xml" and _flip_sheets_rtl(root):
            touched = True
        if touched:
            replaced[part] = root
    if "meta.xml" in zf.namelist():
        meta = _parse_xml(zf.read("meta.xml"))  # never budget-checked by parse()
        office_meta = meta.find(_OFFICE + "meta")
        if office_meta is not None:
            lang_el = office_meta.find(f"{{{NS['dc']}}}language")
            if lang_el is None:
                lang_el = etree.SubElement(office_meta, f"{{{NS['dc']}}}language")
            lang_el.text = tag
            replaced["meta.xml"] = _serialize(meta)


@register
class OdtFilter(_OdfFilter):
    fmt = "odt"
    exts = ("ott",)


@register
class OdsFilter(_OdfFilter):
    fmt = "ods"
    exts = ("ots",)


@register
class OdpFilter(_OdfFilter):
    fmt = "odp"
    exts = ("otp",)


@register
class OdgFilter(_OdfFilter):
    fmt = "odg"
    exts = ("otg",)
