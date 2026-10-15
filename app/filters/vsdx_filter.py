"""Visio drawing filter (.vsdx, macro-enabled .vsdm): file-format Batch 16.

A Visio 2013+ drawing is an OPC zip package [MS-VSDX]. The translatable, user-visible
text lives in three places, walked in this order (parse and build walk the same units in
the same order; the unit index is the position in that walk, translatable or not):

  1. page names: the `Name` attribute of each `<Page>` in visio/pages/pages.xml (the
     language-dependent name; `NameU`, the universal name formulas refer to, is never
     touched), in pages.xml order;
  2. for each page, in pages.xml order (part resolved through pages.xml.rels), every
     shape `<Text>` element and every shape data label (`<Section N="Property">` row,
     `<Cell N="Label" V="..."/>` with no formula), in document order of the page part.

Shape text: a `<Text>` element is mixed content, the characters interspersed with empty
`<cp IX>`/`<pp IX>`/`<tp IX>` run markers (character / paragraph / tab properties row of
the shape) and `<fld IX>` text fields whose content is the cached field value. Every one
of those child elements becomes a point token ⟦xN⟧ (the services/segment scheme) and is
written back verbatim (attributes, field content) wherever the translation puts its
token. Markers before the first and after the last character (and the edge whitespace,
usually the closing paragraph mark "\n") stay outside the unit. A token the translation
drops is appended at the end of the text so no run marker or field is ever lost (the
tag-damage gate reports the divergence before export).

Comments (#690): when the project's "translate comments" option is on
(parse(comments=True)), the text of each `<CommentEntry>` in the comments part
(visio/comments.xml, the document part's "comments" relationship) is a unit, styled
"comment", at office_comments.COMMENT_BASE + j in CommentList order. AuthorList, AuthorID,
dates, page and shape ids are never touched; an entry with child elements is skipped.

Masters (visio/masters/*) are left alone: master text only shows on a shape that does not
override it and edits there would change every instance. Macros (.vsdm vbaProject.bin),
[Content_Types].xml, relationships, themes, images and every other part are copied
untouched; only the page parts that actually received a changed translation are
re-serialized, and a build with no changes returns the original bytes.

Localized export (build_localized): a translated shape's literal Character LangID cells get
the target language; for a right-to-left target (a11y.target_direction) the Paragraph rows
its text uses get Flags=1, adding a sparse row or a Paragraph section where the shape
inherits its paragraph formatting (#740, see _stamp_rtl). Left-to-right targets and build()
write no direction.

Security: zip_guard before any read, a pre-parse markup budget over the parts walked,
lxml with no entities / no DTD / no network / no huge tree, DOCTYPE refused, and the
shared segment caps.

Spec: https://learn.microsoft.com/en-us/openspecs/sharepoint_protocols/ms-vsdx/
Documented in: docs/architecture/translation-pipeline.md
"""

from __future__ import annotations

import posixpath
import re
import zipfile
from copy import deepcopy
from io import BytesIO

from lxml import etree

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

_PAGES = "visio/pages/pages.xml"
_PAGES_RELS = "visio/pages/_rels/pages.xml.rels"
_PAGE_PART_RE = re.compile(r"^visio/pages/page(\d+)\.xml$")
_R_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
_DOCTYPE_RE = re.compile(rb"<!DOCTYPE", re.IGNORECASE)
_STANDALONE_RE = re.compile(rb"standalone\s*=\s*[\"'](yes|no)[\"']")
_MARKERS = {"cp", "pp", "tp", "fld"}
STYLE_PAGE = "Visio page name"
STYLE_TEXT = "Visio shape text"
STYLE_LABEL = "Visio shape data label"


def _local(el) -> str:
    return etree.QName(el).localname if isinstance(el.tag, str) else ""


def _is_translatable(s: str) -> bool:
    return any(c.isalnum() for c in s)


def _parser() -> etree.XMLParser:
    return etree.XMLParser(
        resolve_entities=False,
        no_network=True,
        load_dtd=False,
        dtd_validation=False,
        huge_tree=False,
        remove_blank_text=False,
    )


def _parse_part(raw: bytes, name: str):
    if _DOCTYPE_RE.search(raw):
        raise InputLimitError(
            f"This Visio file declares a DOCTYPE in {name}. It was not processed."
        )
    try:
        root = etree.fromstring(raw, parser=_parser())
    except etree.XMLSyntaxError as exc:
        raise InputLimitError(
            f"This Visio file has a part that is not valid XML ({name})."
        ) from exc
    info = root.getroottree().docinfo
    if info.doctype or info.internalDTD is not None or info.externalDTD is not None:
        raise InputLimitError(
            f"This Visio file declares a DOCTYPE in {name}. It was not processed."
        )
    return root


def _check_markup_budget(zf: zipfile.ZipFile, names: list[str]) -> None:
    """Forty markup characters per allowed segment over the walked parts (the docx and
    xliff budget), counted on the raw bytes zip_guard already bounded, before lxml."""
    from app import config

    elements = 0
    for name in names:
        elements += zf.read(name).count(b"<")
        if elements > 40 * config.MAX_DOCUMENT_SEGMENTS:
            raise InputLimitError(
                "This Visio file has far more shapes or markup than MAX_DOCUMENT_SEGMENTS "
                f"allows ({config.MAX_DOCUMENT_SEGMENTS} segments). Split it into smaller files."
            )


def _natural(name: str) -> int:
    m = _PAGE_PART_RE.match(name)
    return int(m.group(1)) if m else 0


def _page_parts(zf: zipfile.ZipFile, pages_root) -> list[str]:
    """Page part names in pages.xml order (via the Rel r:id), else page<N>.xml order."""
    names = set(zf.namelist())
    out: list[str] = []
    if pages_root is not None and _PAGES_RELS in names:
        rels = _parse_part(zf.read(_PAGES_RELS), _PAGES_RELS)
        targets = {}
        for rel in rels:
            if _local(rel) == "Relationship" and rel.get("TargetMode") != "External":
                target = rel.get("Target") or ""
                if target.startswith("/"):
                    path = target.lstrip("/")
                else:
                    path = posixpath.normpath(posixpath.join("visio/pages", target))
                targets[rel.get("Id")] = path
        for page in pages_root:
            if _local(page) != "Page":
                continue
            for child in page:
                if _local(child) == "Rel":
                    path = targets.get(child.get(f"{{{_R_NS}}}id"))
                    if path in names and path not in out and path.endswith(".xml"):
                        out.append(path)
    if not out:
        out = sorted((n for n in names if _PAGE_PART_RE.match(n)), key=_natural)
    return out


# ---------------------------------------------------------------- shape text tokens


def _text_items(text_el) -> list:
    """The mixed content of a <Text> as a flat list of str and marker elements."""
    items: list = []
    if text_el.text:
        items.append(text_el.text)
    for child in text_el:
        if isinstance(child.tag, str):
            items.append(child)
        if child.tail:
            items.append(child.tail)
    return items


def _split_layout(items: list) -> tuple[list, list, list]:
    """(prefix, core, suffix): markers and whitespace before the first and after the last
    visible character stay outside the unit; a field counts as visible content."""

    def visible(it) -> bool:
        return (isinstance(it, str) and it.strip() != "") or (
            not isinstance(it, str) and _local(it) == "fld"
        )

    idx = [i for i, it in enumerate(items) if visible(it)]
    if not idx:
        return list(items), [], []
    first, last = idx[0], idx[-1]
    prefix, core, suffix = (
        list(items[:first]),
        list(items[first : last + 1]),
        list(items[last + 1 :]),
    )
    if isinstance(core[0], str):
        s = core[0]
        lead = s[: len(s) - len(s.lstrip())]
        if lead:
            prefix.append(lead)
            core[0] = s[len(lead) :]
    if isinstance(core[-1], str):
        s = core[-1]
        trail = s[len(s.rstrip()) :]
        if trail:
            suffix.insert(0, trail)
            core[-1] = s[: len(s) - len(trail)]
    return prefix, core, suffix


def _core_text(core: list) -> str:
    from app.services.segment import ph_point

    out, n = [], 0
    for it in core:
        if isinstance(it, str):
            out.append(it)
        else:
            n += 1
            out.append(ph_point("x", n))
    return "".join(out)


def _visible_plain(core: list) -> str:
    return "".join(it if isinstance(it, str) else (it.xpath("string()") or "") for it in core)


def _new_core(text: str, core: list) -> list:
    """Map a translated unit back to items: ⟦xN⟧ -> a copy of core marker N (first use
    only), any other token dropped, missing markers appended at the end."""
    from app.services.segment import _PLACEHOLDER_RE

    markers = [it for it in core if not isinstance(it, str)]
    used: set[int] = set()
    out: list = []
    pos = 0
    for m in _PLACEHOLDER_RE.finditer(text):
        out.append(text[pos : m.start()])
        pos = m.end()
        inner = m.group(0)[1:-1]
        if inner.startswith("x") and inner[1:].isdigit():
            k = int(inner[1:])
            if 1 <= k <= len(markers) and k not in used:
                used.add(k)
                out.append(markers[k - 1])
    out.append(text[pos:])
    for k, mk in enumerate(markers, 1):
        if k not in used:
            out.append(mk)
    return [it for it in out if not (isinstance(it, str) and it == "")]


def _write_text(text_el, items: list) -> None:
    for child in list(text_el):
        text_el.remove(child)
    text_el.text = None
    last = None
    for it in items:
        if isinstance(it, str):
            if last is None:
                text_el.text = (text_el.text or "") + it
            else:
                last.tail = (last.tail or "") + it
        else:
            el = deepcopy(it)
            el.tail = None
            text_el.append(el)
            last = el


# ---------------------------------------------------------------- the walk


class _Slot:
    """One walked unit: a page Name attribute, a shape <Text>, or a data label cell."""

    __slots__ = ("core", "el", "kind", "part", "text")

    def __init__(self, kind, el, part, text, core=None):
        self.kind, self.el, self.part, self.text, self.core = kind, el, part, text, core


def _label_cell(el) -> bool:
    if _local(el) != "Cell" or el.get("N") != "Label" or el.get("F") is not None:
        return False
    row = el.getparent()
    sec = row.getparent() if row is not None else None
    return (
        row is not None
        and _local(row) == "Row"
        and sec is not None
        and _local(sec) == "Section"
        and sec.get("N") == "Property"
    )


def _walk(data: bytes):
    """(zip infolist order names, {part: root}, [slots]) for a package."""
    zip_guard.check(data, "Visio file")
    try:
        zf = zipfile.ZipFile(BytesIO(data))
    except zipfile.BadZipFile as exc:
        raise InputLimitError("This Visio file is not a valid .vsdx/.vsdm package.") from exc
    with zf:
        names = set(zf.namelist())
        if _PAGES not in names and not any(_PAGE_PART_RE.match(n) for n in names):
            raise InputLimitError("This Visio file has no drawing pages (visio/pages/).")
        _check_markup_budget(
            zf, [n for n in zf.namelist() if n.startswith("visio/pages/") and n.endswith(".xml")]
        )
        roots: dict = {}
        slots: list[_Slot] = []
        pages_root = None
        if _PAGES in names:
            pages_root = _parse_part(zf.read(_PAGES), _PAGES)
            roots[_PAGES] = pages_root
            for page in pages_root:
                if _local(page) == "Page" and page.get("Name") is not None:
                    slots.append(_Slot("page", page, _PAGES, page.get("Name")))
                    check_segment_cap(len(slots))
        for part in _page_parts(zf, pages_root):
            root = _parse_part(zf.read(part), part)
            roots[part] = root
            for el in root.iter(etree.Element):
                name = _local(el)
                if name == "Text":
                    parent = el.getparent()
                    if parent is None or _local(parent) != "Shape":
                        continue
                    _pre, core, _suf = _split_layout(_text_items(el))
                    slots.append(_Slot("text", el, part, _core_text(core), core))
                elif _label_cell(el):
                    slots.append(_Slot("label", el, part, el.get("V") or ""))
                else:
                    continue
                check_segment_cap(len(slots))
    return roots, slots


_COMMENTS = "visio/comments.xml"
_DOCUMENT = "visio/document.xml"


def _comments_part(zf: zipfile.ZipFile) -> str | None:
    """The comments part: the document part's "comments" relationship, else the
    conventional visio/comments.xml."""
    names = set(zf.namelist())
    rels = posixpath.join(posixpath.dirname(_DOCUMENT), "_rels", "document.xml.rels")
    if rels in names:
        root = _parse_part(zf.read(rels), rels)
        for rel in root:
            if _local(rel) != "Relationship" or (rel.get("TargetMode") or "") == "External":
                continue
            if (rel.get("Type") or "").rsplit("/", 1)[-1] != "comments":
                continue
            target = rel.get("Target") or ""
            path = posixpath.normpath(
                target.lstrip("/")
                if target.startswith("/")
                else posixpath.join(posixpath.dirname(_DOCUMENT), target)
            )
            if path in names and not path.startswith(".."):
                return path
    return _COMMENTS if _COMMENTS in names else None


def _comment_entries(data: bytes):
    """#690: (part, root, [CommentEntry elements in CommentList order]) or None."""
    with zipfile.ZipFile(BytesIO(data)) as zf:
        part = _comments_part(zf)
        if part is None:
            return None
        _check_markup_budget(zf, [part])
        root = _parse_part(zf.read(part), part)
    entries = [el for el in root.iter(etree.Element) if _local(el) == "CommentEntry"]
    return part, root, entries


def _translatable(slot: _Slot) -> bool:
    if slot.kind == "text":
        return _is_translatable(_visible_plain(slot.core)) and any(
            isinstance(it, str) and _is_translatable(it) for it in slot.core
        )
    return _is_translatable(slot.text)


def _serialize(root, original: bytes) -> bytes:
    """UTF-8, keeping whether the part had an XML declaration and its standalone flag
    (lxml's docinfo reports False for an absent flag, so read the source bytes)."""
    head = original.lstrip()[:200]
    if not head.startswith(b"<?xml"):
        return etree.tostring(root, encoding="UTF-8", xml_declaration=False)
    decl = head.split(b"?>", 1)[0]
    m = _STANDALONE_RE.search(decl)
    kw = {} if m is None else {"standalone": m.group(1) == b"yes"}
    return etree.tostring(root.getroottree(), encoding="UTF-8", xml_declaration=True, **kw)


@register
class VsdxFilter(Filter):
    fmt = "vsdx"
    exts = ("vsdm",)

    takes_comments = True  # #690: the per-project "translate comments" option

    def parse(
        self, data: bytes, provider: str | None = None, comments: bool = False
    ) -> ParseResult:
        _roots, slots = _walk(bytes(data))
        style = {"page": STYLE_PAGE, "text": STYLE_TEXT, "label": STYLE_LABEL}
        units = [
            TextUnit(p_index=i, text=s.text, style=style[s.kind])
            for i, s in enumerate(slots)
            if _translatable(s)
        ]
        parts = {u.p_index: slots[u.p_index].part for u in units}
        found = _comment_entries(bytes(data)) if comments else None
        if found is not None:
            from app.filters.office_comments import COMMENT_BASE, COMMENT_STYLE

            part, _root, entries = found
            for j, el in enumerate(entries):
                if len(el) or not _is_translatable(el.text or ""):
                    continue
                units.append(
                    TextUnit(
                        p_index=COMMENT_BASE + j, text=(el.text or "").strip(), style=COMMENT_STYLE
                    )
                )
                parts[COMMENT_BASE + j] = part
                check_segment_cap(len(units))
        check_unit_texts([u.text for u in units])
        skeleton = {
            "format": "vsdx",
            "units": [
                {
                    "p_index": u.p_index,
                    "text": u.text,
                    "style": u.style,
                    "part": parts[u.p_index],
                }
                for u in units
            ],
        }
        if comments:
            skeleton["comments"] = True
        return ParseResult(skeleton=skeleton, units=units)

    def build(self, original: bytes, paragraph_texts: dict) -> bytes:
        return self._rebuild(original, paragraph_texts)

    def build_localized(
        self, original: bytes, paragraph_texts: dict, target_locale: str | None = None
    ) -> bytes:
        from app.filters import a11y

        rtl = bool(target_locale) and a11y.target_direction(target_locale) == "rtl"
        return self._rebuild(original, paragraph_texts, _lcid(target_locale), rtl)

    def _rebuild(
        self,
        original: bytes,
        paragraph_texts: dict,
        lcid: int | None = None,
        rtl: bool = False,
    ) -> bytes:
        from app.filters.xml_text import XmlTargetError, xml_target_text

        original = bytes(original)
        roots, slots = _walk(original)
        touched: set[str] = set()
        from app.filters.office_comments import COMMENT_BASE, has_comment_keys

        found = _comment_entries(original) if has_comment_keys(paragraph_texts) else None
        if found is not None:
            roots[found[0]] = found[1]
            for key, new_text in paragraph_texts.items():
                try:
                    j = int(key) - COMMENT_BASE
                except (TypeError, ValueError):
                    continue
                if new_text is None or not 0 <= j < len(found[2]) or len(found[2][j]):
                    continue
                el = found[2][j]
                old = el.text or ""
                new_text = xml_target_text(str(new_text))
                if new_text == old.strip():
                    continue
                lead = old[: len(old) - len(old.lstrip())]
                trail = old[len(old.rstrip()) :]
                try:
                    el.text = lead + new_text + trail
                except ValueError as exc:
                    raise XmlTargetError(
                        f"Visio comment {j + 1} could not be written. Review its target text."
                    ) from exc
                touched.add(found[0])
        for key, new_text in (paragraph_texts or {}).items():
            if new_text is None:
                continue
            try:
                i = int(key)
            except (TypeError, ValueError):
                continue
            if not 0 <= i < len(slots):
                continue
            slot = slots[i]
            new_text = xml_target_text(str(new_text))
            if new_text == slot.text:
                continue
            try:
                if slot.kind == "page":
                    if slot.el.get("NameU") is None:
                        # keep the language-independent name formulas and links use
                        slot.el.set("NameU", slot.el.get("Name"))
                    slot.el.set("Name", new_text)
                elif slot.kind == "label":
                    slot.el.set("V", new_text)
                else:
                    pre, core, suf = _split_layout(_text_items(slot.el))
                    _write_text(slot.el, pre + _new_core(new_text, core) + suf)
                    if lcid is not None:
                        _stamp_langid(slot.el.getparent(), lcid)
                    if rtl:
                        _stamp_rtl(slot.el.getparent(), slot.el)
            except ValueError as exc:
                raise XmlTargetError(
                    f"Visio unit {i + 1} could not be written. Review its target text."
                ) from exc
            touched.add(slot.part)
        if not touched:
            return original
        return _repack(original, {p: roots[p] for p in touched})


def _lcid(target_locale) -> int | None:
    """Windows LCID of the target (Visio's Character LangID cell), or None."""
    import locale as _pylocale

    from app.filters import a11y

    tag = a11y.target_tag(target_locale) if target_locale else ""
    if not tag:
        return None
    parts = tag.split("-")
    lang = parts[0].lower()
    region = next((p.upper() for p in parts[1:] if len(p) == 2 and p.isalpha()), "")
    script = next((p.title() for p in parts[1:] if len(p) == 4 and p.isalpha()), "")
    if lang == "zh" and not region:
        region = {"Hant": "TW", "Hans": "CN"}.get(script, "CN")
    table = _pylocale.windows_locale
    if region:
        hits = sorted(k for k, v in table.items() if v == f"{lang}_{region}")
        if hits:
            return hits[0]
    same = sorted(k for k, v in table.items() if v.split("_")[0] == lang)
    default = [k for k in same if (k >> 10) == 1]
    return (default or same or [None])[0]


def _stamp_langid(shape, lcid: int) -> None:
    """Localized export: the translated shape's Character rows declare the target
    language (LangID cell, a Windows LCID). Only literal cells are set; a formula or an
    inherited (absent) cell is left alone."""
    if shape is None:
        return
    for sec in shape:
        if _local(sec) != "Section" or sec.get("N") != "Character":
            continue
        for row in sec:
            for cell in row:
                if _local(cell) == "Cell" and cell.get("N") == "LangID" and cell.get("F") is None:
                    cell.set("V", str(lcid))


def _stamp_rtl(shape, text_el) -> None:
    """#740: localized export into a right-to-left target. The Paragraph section Flags cell
    is the paragraph direction (0 = left to right, the default; 1 = right to left: the
    Direction setting of the Text dialog's Paragraph tab). In every Paragraph row the
    translated text uses (row 0 for the text before the first <pp>, and every <pp IX>) a
    literal Flags cell becomes 1 and a missing one is added; a cell with a real formula (F)
    is left alone, as for LangID, while F="Inh" (the value is only inherited from the
    master / style, like an absent cell) becomes a local 1. A used row the shape does not have
    locally inherits from its master shape or text style, which usually says left to
    right: a sparse local row holding only Flags is added (ShapeSheet inheritance is per
    cell, so every other paragraph cell still comes from the master / style; Visio's own
    files carry such sparse rows, e.g. a row holding only HorzAlign), and a Paragraph
    section is added after the Character section (else after the last Section / Cell)
    when the shape has none. Rows the text does not use, and a deleted (Del="1") section
    or row, are left alone.

    Not written: the Character RTLText cell (VisioSchema15 does not list it among the
    Character section cells and the VisCellIndices reference marks it "Cell not
    implemented"), and alignment is not mirrored (HorzAlign kept, as docx / pptx / ODF
    #724)."""
    if shape is None:
        return
    ns = etree.QName(shape).namespace
    q = (lambda n: f"{{{ns}}}{n}") if ns else (lambda n: n)
    used = {0}
    for m in text_el.iter(q("pp")):
        ix = m.get("IX")
        if ix is not None and ix.isdigit():
            used.add(int(ix))
    sec = None
    for child in shape:
        if _local(child) == "Section" and child.get("N") == "Paragraph":
            sec = child
            break
    if sec is not None and sec.get("Del") == "1":
        return  # the shape deliberately deleted its paragraph formatting: leave it
    if sec is None:
        sec = etree.Element(q("Section"), N="Paragraph")
        # Shape content order: Cell*, Trigger*, Section*, Text?, ...; Visio writes the
        # Paragraph section after the Character section.
        anchor = None
        for child in shape:
            if _local(child) == "Section" and child.get("N") == "Character":
                anchor = child
        if anchor is not None:
            anchor.addnext(sec)
        else:
            pos = 0
            for i, child in enumerate(shape):
                if _local(child) in ("Cell", "Trigger", "Section"):
                    pos = i + 1
            shape.insert(pos, sec)
    have: dict[int, object] = {}
    for i, row in enumerate(r for r in sec if _local(r) == "Row"):
        ix = row.get("IX")
        have[int(ix) if ix is not None and ix.isdigit() else i] = row
    have = dict(sorted(have.items()))
    for ix, row in have.items():
        if ix not in used or row.get("Del") == "1":
            continue  # a row no paragraph of this text uses changes nothing
        flags = next((c for c in row if _local(c) == "Cell" and c.get("N") == "Flags"), None)
        if flags is None:
            etree.SubElement(row, q("Cell"), N="Flags", V="1")
        elif flags.get("F") is None or flags.get("F") == "Inh":
            flags.attrib.pop("F", None)
            flags.set("V", "1")
    for ix in sorted(used - set(have)):
        row = etree.Element(q("Row"), IX=str(ix))
        etree.SubElement(row, q("Cell"), N="Flags", V="1")
        after = [r for k, r in have.items() if k < ix]
        if after:
            after[-1].addnext(row)
        else:
            sec.insert(0, row)
        have[ix] = row
        have = dict(sorted(have.items()))


def _repack(original: bytes, changed: dict) -> bytes:
    """Copy the package member by member in its original order with its original
    metadata; only the changed parts get new bytes."""
    src = zipfile.ZipFile(BytesIO(original))
    out = BytesIO()
    with src, zipfile.ZipFile(out, "w") as dst:
        for info in src.infolist():
            raw = src.read(info)
            if info.filename in changed:
                raw = _serialize(changed[info.filename], raw)
            new = zipfile.ZipInfo(info.filename, date_time=info.date_time)
            new.compress_type = info.compress_type
            new.external_attr = info.external_attr
            new.create_system = info.create_system
            new.comment = info.comment
            dst.writestr(new, raw)
        dst.comment = src.comment
    return out.getvalue()
