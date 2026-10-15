"""DOCX filter — round-trips Word documents via python-docx.

parse(): walks every paragraph (body + table cells, in a stable order) and emits
a TextUnit for each non-empty one. The skeleton records each unit's position,
text, and style.

build(): re-opens the ORIGINAL file and replaces only the paragraphs we
translated, so images, tables, headers/footers, and untouched runs are preserved.

Inline-tag fidelity (Phase 5 chunk 6): a paragraph with more than one run carries
mid-sentence formatting (a bold word, a hyperlink span, a colour change). parse()
wraps each such run's text in paired placeholder tokens (⟦r1⟧…⟦/r1⟧, see
services/segment) and stashes that run's rPr XML in the TextUnit side-channel;
build() splits the translated text on those tokens and rebuilds one Run per span,
deep-copying the matching rPr so the formatting survives translation. Single-run
paragraphs stay plain text (no tokens, identical to the pre-fidelity behavior).

A paragraph that holds a NON-TEXT object next to its text (a picture, floating
image, text box, chart, OLE object, field, footnote reference, symbol, page break,
tracked change, smartTag) or any text inside a w:hyperlink is handled in "object
mode" instead (see the "non-text objects" section): each group of objects becomes a
point token (⟦xN⟧) the translator keeps, a hyperlink is re-created around its
translated runs, and build() puts the original elements back, so nothing is deleted
on export. Paragraphs with neither keep exactly the behavior described above.

A paragraph whose text is NOT fully carried by its direct runs and that object mode
does not claim falls back to the pre-fidelity whole-paragraph behavior (no tokens,
first-run set). With the walker below that is a defensive branch only.
"""

from io import BytesIO

from app.filters import zip_guard
from app.filters.base import Filter, ParseResult, TextUnit, register
from app.services.segment import ph_close, ph_open, ph_point

# python-docx is imported lazily (test speed, 2026-10-08): importing the package costs
# about 0.15 s, and every app and CLI process imports this filter through the registry.
# qn() is python-docx's docx.oxml.ns.qn for the three prefixes this module uses; the URIs
# are the fixed OOXML namespaces, and verify_lazy_imports pins them to docx's own nsmap.
_NSMAP = {
    "w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main",
    "wp": "http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing",
    "pic": "http://schemas.openxmlformats.org/drawingml/2006/picture",
}


def qn(tag: str) -> str:
    """Clark-notation name for a prefixed tag ("w:p" -> "{...main}p"), as docx.oxml.ns.qn."""
    prefix, tagroot = tag.split(":")
    return f"{{{_NSMAP[prefix]}}}{tagroot}"


# Word main-part content types besides the plain document (ECMA-376 Part 1 11.3.10 for
# the template; [MS-OFFMACRO2] for the macro-enabled document and template). The .docm,
# .dotx and .dotm aliases of this filter open through the same python-docx DocumentPart;
# python-docx keeps each part's content type on save, so [Content_Types].xml still names
# the template / macro type and word/vbaProject.bin is written back as the same bytes.
WORD_DOCUMENT_TYPE = (
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"
)
WORD_ALIAS_TYPES = (
    "application/vnd.openxmlformats-officedocument.wordprocessingml.template.main+xml",
    "application/vnd.ms-word.document.macroEnabled.main+xml",
    "application/vnd.ms-word.template.macroEnabledTemplate.main+xml",
)


def DocxDocument(docx=None):
    """python-docx's Document() (the old `Document as DocxDocument` name), imported on first use.

    Also opens a Word template or macro-enabled package (.dotx, .docm, .dotm): python-docx's
    Document() refuses any main part that is not the plain document type, so those are
    opened through the package and the content type is left exactly as it was."""
    from docx import Document
    from docx.opc.part import PartFactory
    from docx.package import Package
    from docx.parts.document import DocumentPart

    for ct in WORD_ALIAS_TYPES:
        PartFactory.part_type_for.setdefault(ct, DocumentPart)
    if docx is None:
        return Document(docx)
    part = Package.open(docx).main_document_part
    if part.content_type in WORD_ALIAS_TYPES:
        return part.document
    # the plain document type (or anything else): python-docx's own check and message
    if part.content_type != WORD_DOCUMENT_TYPE:
        raise ValueError(f"file '{docx}' is not a Word file, content type is '{part.content_type}'")
    return part.document


_RPR = qn("w:rPr")
_VMERGE = qn("w:vMerge")
_W_VAL = qn("w:val")
_W_T = qn("w:t")


def _is_vmerge_continuation(tc) -> bool:
    """True for a w:tc that continues a vertical merge from the cell above it.

    w:vMerge with no w:val (or val="continue") is a continuation; val="restart"
    starts a new merged region and is the cell that owns the text."""
    tc_pr = tc.tcPr
    if tc_pr is None:
        return False
    vm = tc_pr.find(_VMERGE)
    return vm is not None and vm.get(_W_VAL, "continue") != "restart"


# Walker version stamped into every new skeleton as skeleton["docx_walk"] (#349, #355).
# A stored skeleton addresses paragraphs by p_index and export re-walks the ORIGINAL
# file, so the numbering a project was imported under must be the numbering it is
# exported under. Walks: 1 = no stamp (pre-#349 row.cells walk), 2 = #349 (one visit per
# physical w:tc, no nested tables or w:sdt / w:customXml wrappers), 3 = #355 (reading
# order: body paragraphs and tables interleaved as in the body, plus nested tables and the
# wrappers), 4 = #270 r1-files-06 (stamp 3 order for the main body, then the other text
# parts: headers, footers, footnotes, endnotes, comments; text boxes inline after their
# anchor paragraph; tracked insertions read as text), 5 = #368 (stamp 4 numbering; text in
# a w:smartTag is read as text like a hyperlink's, and the alt text of drawings in headers,
# footers, notes and comments is extracted, appended after the body's alt units), 6 = #686
# (stamp 5 numbering; inline wrappers NEST: text in a smart tag inside a smart tag, hyperlink
# or tracked insertion, and in an inline w:customXml, w:dir, w:bdo or text content control
# (w:sdt), at any depth up to MAX_INLINE_DEPTH, is read as text and each wrapper is re-created
# around the translation, see `_walk_paragraph_v6`). Each stamp keeps its own walk at export,
# never a neighbour's.
DOCX_WALK_LEGACY = 1
DOCX_WALK_VERSION = 6
_KNOWN_WALKS = (2, 3, 4, 5, 6)  # stamps this build understands; a missing stamp is walk 1
# Table nesting deeper than this is not descended into (Word itself stops far earlier);
# the same bound applies in parse and export so the numbering stays stable.
MAX_TABLE_DEPTH = 32


def walk_version(skeleton) -> int:
    """The walk a stored skeleton was numbered by (1 to 6).

    Only a MISSING stamp means the legacy walk 1; only a stamp this build knows selects
    its walk. Any other value (a future stamp after a rollback, a corrupted value) is
    refused: guessing a walk would put translations into the wrong cells (the #349
    round-1 gate bug)."""
    skel = skeleton or {}
    if "docx_walk" not in skel:
        return DOCX_WALK_LEGACY
    stamp = skel["docx_walk"]
    # bool is an int subclass: True == 1 must not pass for a stamp
    if type(stamp) is int and stamp in _KNOWN_WALKS:
        return stamp
    raise ValueError(
        f"This DOCX was imported with table walk {stamp!r}, which this version "
        f"does not know (it knows {', '.join(map(str, _KNOWN_WALKS))}). "
        "Re-import the document to export it."
    )


def walk_is_legacy(skeleton) -> bool:
    """True when a stored skeleton was numbered by the pre-#349 row.cells walk."""
    return walk_version(skeleton) == DOCX_WALK_LEGACY


def _legacy_paragraphs(doc) -> list:
    """The pre-#349 walk, byte-identical numbering, kept for stored projects.

    python-docx's Row.cells repeats a merged cell once per spanned grid column and
    once per vertically merged row, so those paragraphs appear several times. That
    is exactly the numbering such projects were imported under; do not change it.
    (It is quadratic in a vMerge column and hits RecursionError past ~1,100 rows,
    so no such project can exist; the numbering cannot be kept by any other walk.)"""
    paras = list(doc.paragraphs)
    for table in doc.tables:
        for row in table.rows:
            for cell in row.cells:
                paras.extend(cell.paragraphs)
    return paras


def _v2_paragraphs(doc) -> list:
    """The #349 walk (stamp 2), kept unchanged for stored projects: body paragraphs, then
    each top-level table's physical w:tc once; nested tables and wrappers are not walked."""
    from docx.table import _Cell

    paras = list(doc.paragraphs)
    for table in doc.tables:
        for tr in table._tbl.tr_lst:
            for tc in tr.tc_lst:
                cell = _Cell(tc, table)
                cell_paras = cell.paragraphs
                if _is_vmerge_continuation(tc) and not any(_p_has_text(p._p) for p in cell_paras):
                    continue
                paras.extend(cell_paras)
    return paras


_W_P = qn("w:p")
_W_TBL = qn("w:tbl")
_W_TR = qn("w:tr")
_W_TC = qn("w:tc")
_W_SDT = qn("w:sdt")
_W_SDT_CONTENT = qn("w:sdtContent")
_W_CUSTOM_XML = qn("w:customXml")


def _through_wrappers(parent, *tags):
    """The children of `parent` with one of `tags`, in document order, looking through
    content controls (w:sdt / w:sdtContent) and w:customXml wrappers, however deeply
    stacked. Iterative, so a hostile chain of wrappers cannot recurse."""
    stack = [iter(parent)]
    while stack:
        for child in stack[-1]:
            t = child.tag
            if t in tags:
                yield child
            elif t == _W_SDT:
                content = child.find(_W_SDT_CONTENT)
                if content is not None:
                    stack.append(iter(content))
                    break
            elif t == _W_CUSTOM_XML:
                stack.append(iter(child))
                break
        else:
            stack.pop()


def _blocks(tops, parent):
    """Yield the Paragraph of every text-carrying block in `tops` (body-level elements,
    already looked through wrappers) in reading order; the shared core of walks 3 and 4.

    Each table: its cells in row order (rows and cells may sit inside w:sdt /
    w:customXml), each cell's content in document order. A table nested in a cell is
    walked in place, depth-first, so its text sits right after the cell text before it
    and before the cell text after it, which is the order a translator reads it in (#355
    gate). An explicit stack, no recursion. A vMerge continuation cell's own paragraphs
    are skipped unless malformed XML put text in them (#349); a table nested in one is
    still walked. `parent` is the python-docx story the blocks belong to (the body, or a
    header / footer / notes container) and only supplies `.part` to the proxies."""
    from docx.table import Table, _Cell
    from docx.text.paragraph import Paragraph

    def cells_of(tbl_el):
        for tr in _through_wrappers(tbl_el, _W_TR):
            yield from _through_wrappers(tr, _W_TC)

    for top in tops:
        if top.tag == _W_P:
            yield Paragraph(top, parent)
            continue
        # frames: (True, tc iterator, Table, depth) or (False, item iterator, (_Cell, keep), depth)
        stack = [(True, cells_of(top), Table(top, parent), 1)]
        while stack:
            is_table, items, owner, depth = stack[-1]
            el = next(items, None)
            if el is None:
                stack.pop()
            elif is_table:
                content = list(_through_wrappers(el, _W_P, _W_TBL))
                keep = not _is_vmerge_continuation(el) or any(
                    _p_has_text(x) for x in content if x.tag == _W_P
                )
                stack.append((False, iter(content), (_Cell(el, owner), keep), depth))
            elif el.tag == _W_P:
                if owner[1]:
                    yield Paragraph(el, owner[0])
            elif depth < MAX_TABLE_DEPTH:
                stack.append((True, cells_of(el), Table(el, owner[0]), depth + 1))


def _v3_paragraphs(doc) -> list:
    """The #355 walk (stamp 3): true reading order of the main body.

    Body paragraphs and tables interleaved exactly as they sit in the body (also inside
    body-level w:sdt / w:customXml wrappers, #355, the maintainer's reading-order decision), so a
    table's text lands between the paragraphs around it (unlike v2: body paragraphs, then
    tables). See `_blocks` for the table rules."""
    return list(_blocks(_through_wrappers(doc.element.body, _W_P, _W_TBL), doc._body))


# --- walk 4 (#270 r1-files-06): the other text parts, text boxes, tracked insertions -----
_W_INS = qn("w:ins")
_W_DEL = qn("w:del")
_W_MOVE_FROM = qn("w:moveFrom")
_W_MOVE_TO = qn("w:moveTo")
# Tracked changes walk 4 reads as text: an insertion and the destination of a move (both
# are text as Word shows it). Deletions and move sources (w:del, w:moveFrom) are never read.
_REVISION_CONTAINERS = (_W_INS, _W_MOVE_TO)
_W_SMARTTAG = qn("w:smartTag")
_W_SMARTTAGPR = qn("w:smartTagPr")


def _containers(level) -> tuple:
    """The paragraph-level wrappers whose runs a walk reads as text and re-creates around
    the translation. `level` is 0 / False for stamps 3 and older, the stamp (or True) from
    4: revisions (w:ins, w:moveTo); from 5 also w:smartTag."""
    if not level:
        return ()
    if level >= 5:
        return (*_REVISION_CONTAINERS, _W_SMARTTAG)
    return _REVISION_CONTAINERS


# --- walk 6 (#686): nested inline wrappers --------------------------------------------
# Walk 5 reads a wrapper's runs only when the wrapper sits DIRECTLY in the paragraph, so a
# smart tag inside a smart tag (Word nests them: place > City), a smart tag or hyperlink in
# a tracked insertion, or an inline content control was one opaque object and its words
# were never translated. Walk 6 descends through these wrappers at any depth (bounded,
# iterative) and re-creates the whole chain around the translation on export.
_W_CUSTOM_XML_PR = qn("w:customXmlPr")
_W_SDT_PR = qn("w:sdtPr")
_W_SDT_END_PR = qn("w:sdtEndPr")
_W_DIR = qn("w:dir")
_W_BDO = qn("w:bdo")
# Run-level wrappers whose content is runs (plus markers and objects), re-created around
# their translated runs with their attributes and properties element.
_W6_WRAPPERS = frozenset(
    (qn("w:hyperlink"), _W_INS, _W_MOVE_TO, _W_SMARTTAG, _W_CUSTOM_XML, _W_DIR, _W_BDO)
)
# A wrapper's properties element: copied onto each re-created piece, never read as content.
_W6_PROPS = frozenset((_W_SMARTTAGPR, _W_CUSTOM_XML_PR))
# Content controls whose displayed text is not free text (a list choice, a date, a check
# box, a picture, a gallery or citation): they stay one object, as in walk 5.
_SDT_FIXED_KINDS = frozenset(
    (
        "checkbox",
        "dropDownList",
        "comboBox",
        "date",
        "picture",
        "docPartObj",
        "docPartList",
        "citation",
        "bibliography",
        "equation",
        "group",
    )
)
# Inline wrappers nested deeper than this stay one object (parse, export and the warning
# agree); Word itself never nests anywhere near this deep.
MAX_INLINE_DEPTH = 32


def _w6_content(el):
    """The element whose children walk 6 reads for a run-level wrapper `el`, or None when
    `el` is not one: the wrapper itself, or the w:sdtContent of a text content control."""
    tag = el.tag
    if tag in _W6_WRAPPERS:
        return el
    if tag != _W_SDT:
        return None
    pr = el.find(_W_SDT_PR)
    if pr is not None and any(
        isinstance(c.tag, str) and c.tag.rsplit("}", 1)[-1] in _SDT_FIXED_KINDS for c in pr
    ):
        return None
    return el.find(_W_SDT_CONTENT)


def _w6_paragraph_of_run(r):
    """The w:p whose walk-6 text includes run `r` (climbing only through walk-6 wrappers,
    at most MAX_INLINE_DEPTH of them), or None when something else holds it."""
    a = r.getparent()
    depth = 0
    while a is not None and a.tag != _W_P:
        if a.tag == _W_SDT_CONTENT:
            sdt = a.getparent()
            if sdt is None or _w6_content(sdt) is not a:
                return None
            a = sdt
        elif a.tag not in _W6_WRAPPERS:
            return None
        depth += 1
        if depth > MAX_INLINE_DEPTH:
            return None
        a = a.getparent()
    return a


def walk6_reads(el) -> bool:
    """True when walk 6 translates every text of `el`, an element inside a paragraph: each
    w:t in it sits in a run that reaches el's own paragraph through walk-6 wrappers only.
    (The untranslated-regions warning names what this leaves out.)"""
    p = next(el.iterancestors(_W_P), None)
    if p is None:
        return False
    for t in el.iter(_W_T):
        if not t.text:
            continue
        r = t.getparent()
        if r is None or r.tag != _W_R or _w6_paragraph_of_run(r) is not p:
            return False
    return True


def _walk_paragraph_v6(p) -> list:
    """Walk 6's atom list for one w:p: as walk 5, but every walk-6 wrapper is a container
    at any depth (see `_w6_content`), and each atom records the chain of wrappers that
    holds it (`path`, outermost first; `ctx` is the innermost). Iterative."""
    out: list = []
    stack = [(iter(p), None, ())]
    while stack:
        it, ctx, path = stack[-1]
        el = next(it, None)
        if el is None:
            stack.pop()
            continue
        if not isinstance(el.tag, str) or el.tag in _W6_PROPS:
            continue  # (a wrapper's properties are copied onto its re-created pieces)
        if ctx is None and el.tag == _PPR:
            continue
        start = len(out)
        if el.tag == _W_R:
            _walk_runs(el, ctx, out)
        elif el.tag in _MARKERS:
            out.append(_Atom("marker", ctx=ctx, el=el))
        else:
            box = _w6_content(el) if len(path) < MAX_INLINE_DEPTH else None
            if box is not None:
                stack.append((iter(box), el, (*path, el)))
                continue
            out.append(_Atom("obj", ctx=ctx, el=el))
        for a in out[start:]:
            a.path = path
    return [a for a in out if a.kind != "text" or a.text]


def _w6_runs(p) -> list:
    """Every run walk 6 reads in paragraph `p` (direct or inside walk-6 wrappers)."""
    out = []
    stack = [(iter(p), 0)]
    while stack:
        it, depth = stack[-1]
        el = next(it, None)
        if el is None:
            stack.pop()
            continue
        if not isinstance(el.tag, str):
            continue
        if el.tag == _W_R:
            out.append(el)
            continue
        box = _w6_content(el) if depth < MAX_INLINE_DEPTH else None
        if box is not None:
            stack.append((iter(box), depth + 1))
    return out


def _strong_direction(text: str) -> str:
    """'ltr' / 'rtl' from the first strong bidi character of `text`, '' when it has none."""
    import unicodedata

    for ch in text:
        kind = unicodedata.bidirectional(ch)
        if kind == "L":
            return "ltr"
        if kind in ("R", "AL"):
            return "rtl"
    return ""


def _fit_direction(clones: list) -> None:
    """#686 gate: re-created w:bdo (direction OVERRIDE) / w:dir (embedding) pieces whose
    source direction matched their source text take the direction of the text now inside
    them. An English source's `w:bdo w:val="ltr"` around words translated into Arabic would
    otherwise lay the Arabic letters out left to right (reversed); a Latin name kept in its
    override keeps 'ltr'. A source wrapper that disagreed with its own text (deliberate)
    is re-created as it was."""
    for el in clones:
        want = _strong_direction("".join(t.text or "" for t in el.iter(_W_T)))
        if want and want != el.get(_W_VAL):
            el.set(_W_VAL, want)


def _clone_container(ctx, again: bool, new_id):
    """(element to append, element that receives the content) re-creating wrapper `ctx`:
    its attributes and properties element; a tracked change re-created again (`again`)
    takes a fresh w:id, a content control re-created again drops its own w:id (optional,
    and must be unique)."""
    from copy import deepcopy

    from docx.oxml.parser import OxmlElement

    clone = OxmlElement("w:" + ctx.tag.rsplit("}", 1)[-1])
    for k, v in ctx.attrib.items():
        clone.set(k, v)
    if ctx.tag == _W_SDT:
        for pr_tag in (_W_SDT_PR, _W_SDT_END_PR):
            pr = ctx.find(pr_tag)
            if pr is not None:
                pr = deepcopy(pr)
                if again and pr_tag == _W_SDT_PR:
                    for old in pr.findall(qn("w:id")):
                        pr.remove(old)
                clone.append(pr)
        content = OxmlElement("w:sdtContent")
        clone.append(content)
        return clone, content
    pr_tag = _W_SMARTTAGPR if ctx.tag == _W_SMARTTAG else _W_CUSTOM_XML_PR
    pr = ctx.find(pr_tag) if ctx.tag in (_W_SMARTTAG, _W_CUSTOM_XML) else None
    if pr is not None:  # the tag's properties lead every piece of it
        clone.append(deepcopy(pr))
    if ctx.tag in _REVISION_CONTAINERS and again and new_id is not None:
        clone.set(qn("w:id"), new_id())
    return clone, clone


_TXBX = qn("w:txbxContent")
_MC_NS = "{http://schemas.openxmlformats.org/markup-compatibility/2006}"
_MC_AC = _MC_NS + "AlternateContent"
_MC_CHOICE = _MC_NS + "Choice"
_MC_FALLBACK = _MC_NS + "Fallback"
_W_TYPE = qn("w:type")
# Text boxes nested inside text boxes are followed this deep (parse and export alike).
MAX_BOX_DEPTH = 8
# Other text parts, in the order walk 4 numbers them after the main body: the relationship
# type's last segment -> rank. Within one kind: natural order of the part name.
_EXTRA_PART_KINDS = {"header": 0, "footer": 1, "footnotes": 2, "endnotes": 3, "comments": 4}
# Notes that only draw the separator line (or the "continued" notice) carry no text.
_NOTE_SKIP_TYPES = frozenset({"separator", "continuationSeparator", "continuationNotice"})


def _outer_boxes(root) -> list:
    """The w:txbxContent elements under `root` that are not inside another one."""
    boxes = list(root.iter(_TXBX))
    inner = {d for b in boxes for d in b.iterdescendants(_TXBX)}
    return [b for b in boxes if b not in inner]


def _ac_boxes(ac) -> list[tuple]:
    """[(box, mirror or None)] for one mc:AlternateContent. Word writes the SAME text box
    twice, as the mc:Choice DrawingML shape and as the mc:Fallback VML copy. The text is
    extracted once, from the first Choice that holds a box (the Fallback when no Choice
    does), and the Fallback copy is its mirror when it has the same number of boxes."""
    cboxes: list = []
    for choice in ac:
        if choice.tag == _MC_CHOICE:
            cboxes = _outer_boxes(choice)
            if cboxes:
                break
    fb = ac.find(_MC_FALLBACK)
    fboxes = _outer_boxes(fb) if fb is not None else []
    if not cboxes:
        return [(b, None) for b in fboxes]
    if len(fboxes) == len(cboxes):
        return list(zip(cboxes, fboxes, strict=True))
    return [(b, None) for b in cboxes]


def _text_boxes(p) -> list[tuple]:
    """[(w:txbxContent, mirror or None)] anchored in paragraph `p`, in document order.
    Iterative. A box nested in another box is found when that box's own paragraphs are
    walked; deleted content (w:del, w:moveFrom) is not searched."""
    out: list = []
    stack = [iter(p)]
    while stack:
        for el in stack[-1]:
            tag = el.tag
            if not isinstance(tag, str):
                continue
            if tag == _TXBX:
                out.append((el, None))
            elif tag == _MC_AC:
                out.extend(_ac_boxes(el))
            elif tag in (_W_DEL, _W_MOVE_FROM):
                continue
            elif len(el):
                stack.append(iter(el))
                break
        else:
            stack.pop()
    return out


def _stream(tops, parent):
    """Yield (Paragraph, [mirror Paragraphs]) for `tops` in reading order, with the
    paragraphs of each text box right after the paragraph the box is anchored in (nested
    boxes after theirs, down to MAX_BOX_DEPTH). A mirror is the same paragraph in the
    Fallback copy of the text box it sits in (also, for a box nested in a Fallback copy,
    the copies of that inner box). Iterative."""
    stack = [((p, []) for p in _blocks(tops, parent))]

    def paras_of(box) -> list:
        return list(_blocks(_through_wrappers(box, _W_P, _W_TBL), parent))

    while stack:
        item = next(stack[-1], None)
        if item is None:
            stack.pop()
            continue
        yield item
        if len(stack) > MAX_BOX_DEPTH:
            continue
        para, mirrors = item
        boxes = _text_boxes(para._p)
        if not boxes:
            continue
        in_mirrors = [_text_boxes(m._p) for m in mirrors]
        frames = []
        for k, (box, fbox) in enumerate(boxes):
            prim = paras_of(box)
            copies = [] if fbox is None else [fbox]
            for found in in_mirrors:
                if len(found) == len(boxes):  # the mirror paragraph holds the same boxes
                    copies.extend(c for c in found[k] if c is not None)
            # a copy with another paragraph count is another shape: left alone
            lists = [lst for lst in map(paras_of, copies) if len(lst) == len(prim)]
            frames.append(iter([(q, [lst[j] for lst in lists]) for j, q in enumerate(prim)]))
        stack.extend(reversed(frames))


def _natural(name: str) -> list:
    import re

    return [int(t) if t.isdigit() else t for t in re.split(r"(\d+)", name)]


class _Walk:
    """What walk 4 numbers: the paragraphs (the p_index space), the part each one came
    from, the mirror paragraphs of a text box's Fallback copy, and the parts it opened."""

    __slots__ = ("mirrors", "paras", "parts", "roots")

    def __init__(self, paras: list, parts: list | None = None):
        self.paras = paras
        self.parts = parts
        self.mirrors: dict = {}  # p_index -> [Paragraph] (the Fallback copies of that box text)
        self.roots: dict = {}  # part name -> (part, root, kept as a blob)


def _extra_parts(doc) -> list[tuple]:
    """[(kind, part name, part)] for every header, footer, footnotes, endnotes and comments
    part the main document relates to, once each, in the documented order (kind, then the
    natural order of the part name). Orphan parts (no relationship) are not reachable."""
    found: dict = {}
    for rel in doc.part.rels.values():
        if rel.is_external:
            continue
        kind = rel.reltype.rsplit("/", 1)[-1]
        if kind not in _EXTRA_PART_KINDS:
            continue
        part = rel.target_part
        name = str(part.partname).lstrip("/")
        found.setdefault(name, (kind, name, part))
    return sorted(found.values(), key=lambda t: (_EXTRA_PART_KINDS[t[0]], _natural(t[1])))


def _part_root(part, walk: "_Walk", name: str):
    """The root element of an extra part. python-docx keeps headers, footers and comments
    as XML parts; footnotes and endnotes it keeps as a blob, which is parsed once here
    (and written back by `_flush_blob_parts` only when a paragraph in it changed).
    None for a part that is not parseable XML: it is left exactly as it was."""
    from docx.oxml.parser import parse_xml
    from lxml import etree

    root = getattr(part, "_element", None)
    if root is not None:
        walk.roots[name] = (part, root, False)
        return root
    try:
        root = parse_xml(part.blob)
    except (etree.LxmlError, ValueError, TypeError):
        return None
    walk.roots[name] = (part, root, True)
    return root


def _story_tops(kind: str, root) -> list:
    """The block-element iterators of one extra part, in order: header / footer = the root;
    footnotes / endnotes = each note that carries text; comments = each w:comment."""
    if kind in ("header", "footer"):
        return [_through_wrappers(root, _W_P, _W_TBL)]
    stems = {"footnotes": "footnote", "endnotes": "endnote", "comments": "comment"}
    item = qn("w:" + stems[kind])
    return [
        _through_wrappers(el, _W_P, _W_TBL)
        for el in root
        if el.tag == item and el.get(_W_TYPE) not in _NOTE_SKIP_TYPES
    ]


def _has_text(para, ins) -> bool:
    """True when a paragraph carries text to translate. Walk 4 (`ins`) also counts text
    that sits only inside a tracked insertion, which `Paragraph.text` does not see."""
    if para.text.strip():
        return True
    return bool(ins) and any(
        a.kind == "text" and a.text.strip() for a in _walk_paragraph(para._p, ins)
    )


def _v4_walk(doc, budget: int | None = None) -> "_Walk":
    """The #270 walk (stamp 4): the main body exactly as walk 3 numbers it, then each
    other text part (headers, footers, footnotes, endnotes, comments, see `_extra_parts`).
    A text box's paragraphs follow the paragraph it is anchored in, in whatever part that
    is; the Fallback copy of a text box is not numbered (it mirrors the Choice copy).
    `budget` is how many w:p the other parts may hold in all (parse passes what the
    paragraph cap leaves after the main part); more is refused before they are walked."""
    from docx.blkcntnr import BlockItemContainer

    from app.filters.base import InputLimitError

    walk = _Walk([], [])
    main_name = str(doc.part.partname).lstrip("/")

    def add(tops, parent, name):
        for para, mirrors in _stream(tops, parent):
            if mirrors:
                walk.mirrors[len(walk.paras)] = mirrors
            walk.paras.append(para)
            walk.parts.append(name)

    add(_through_wrappers(doc.element.body, _W_P, _W_TBL), doc._body, main_name)
    for kind, name, part in _extra_parts(doc):
        root = _part_root(part, walk, name)
        if root is None:
            continue
        if budget is not None:
            for _ in root.iter(_W_P):
                budget -= 1
                if budget < 0:
                    from app import config

                    raise InputLimitError(
                        f"This DOCX exceeds the paragraph limit ({config.DOCX_MAX_PARAGRAPHS}). "
                        "Split it into smaller files."
                    )
        parent = BlockItemContainer(root, part)
        for tops in _story_tops(kind, root):
            add(tops, parent, name)
    return walk


def _walk(doc, walk: int = DOCX_WALK_VERSION) -> "_Walk":
    """The numbering for stamp `walk` as a _Walk (parts and mirrors only exist for 4)."""
    if walk >= 4:
        return _v4_walk(doc)
    return _Walk(_all_paragraphs(doc, walk))


def _revision_ids(doc, walk: "_Walk"):
    """A callable handing out w:id values no annotation in the document uses yet (above the
    largest numeric w:id of the main part and every part walk 4 opened), for the extra
    pieces of a tracked change a translation split. Computed on first use only."""
    state: dict = {}

    def new_id() -> str:
        if "next" not in state:
            top = -1
            for root in (doc.element, *(r[1] for r in walk.roots.values())):
                for el in root.iter():
                    v = el.get(_W_ID) if isinstance(el.tag, str) else None
                    if v is not None and v.lstrip("-").isdigit():
                        top = max(top, int(v))
            state["next"] = top + 1
        state["next"] += 1
        return str(state["next"] - 1)

    return new_id


def _flush_blob_parts(walk: "_Walk", touched: set) -> None:
    """Write back the blob-kept parts (footnotes, endnotes) a translation landed in."""
    from lxml import etree

    for name in touched:
        part, root, from_blob = walk.roots.get(name, (None, None, False))
        if from_blob:
            part._blob = etree.tostring(
                root, xml_declaration=True, encoding="UTF-8", standalone=True
            )


def _all_paragraphs(doc, walk: int = DOCX_WALK_VERSION) -> list:
    """Deterministic flat list of paragraphs. Walks 1 and 2: body paragraphs first, then
    table cells; walk 3: reading order (body paragraphs and tables interleaved); walk 4:
    walk 3 for the main body, then the other text parts and text boxes (`_v4_walk`).

    Must iterate identically in parse() and build() so p_index stays stable, which is why
    `walk` names the numbering a stored skeleton was imported under (see walk_version):
    1 = pre-#349, 2 = #349, 3 = #355, 4 = #270 (the current one, used for every new
    import)."""
    if walk == DOCX_WALK_LEGACY:
        return _legacy_paragraphs(doc)
    if walk == 2:
        return _v2_paragraphs(doc)
    if walk == 3:
        return _v3_paragraphs(doc)
    return _v4_walk(doc).paras


def _p_has_text(p_el) -> bool:
    return any(t.text for t in p_el.iter(_W_T))


def _content_runs(para) -> list:
    """The runs that actually carry text (skip empty runs so tokens track real
    formatting boundaries only)."""
    return [r for r in para.runs if r.text]


def _runs_cover_paragraph(para) -> bool:
    """True iff the paragraph's direct runs carry ALL of its text.

    When a paragraph contains a hyperlink, a tracked-change <w:ins>, a smartTag, or
    a field, that run lives inside a child element, NOT in para.runs — so para.text
    (which walks the whole XML) is longer than the concatenation of the direct runs.
    Tokenizing such a paragraph would drop the out-of-run text and reorder it on
    export, so we detect the mismatch and fall back to whole-paragraph handling."""
    return "".join(r.text for r in para.runs) == para.text


def _tokenize_paragraph(para, ins: int = 0):
    """Return (tokenized_text, inline_map) for a paragraph.

    For a single-content-run paragraph we return its plain text with an empty map
    — preserving the historical behavior and keeping tokens out of the common case.
    For a multi-run paragraph each run's text is wrapped in paired ⟦rN⟧…⟦/rN⟧
    tokens and the map carries that run's rPr XML snapshot (or None).

    A paragraph whose direct runs do NOT cover its whole text (hyperlink/<w:ins>/
    smartTag/field) is returned as plain, untokenized text with an empty map — the
    pre-fidelity fallback that guarantees zero data loss.

    A paragraph holding a non-text object (picture, field, note reference, ...) or
    hyperlink text is tokenized by `_tokenize_objects` instead (see "non-text
    objects" below)."""
    layout = _object_layout(para._p, ins)
    if layout is not None:
        return _tokenize_objects(layout)
    if not _runs_cover_paragraph(para):
        return para.text, {}
    runs = _content_runs(para)
    if len(runs) <= 1:
        return para.text, {}
    parts: list[str] = []
    inline_map: dict = {}
    for i, run in enumerate(runs, start=1):
        rpr = run._r.find(_RPR)
        inline_map[str(i)] = None if rpr is None else _serialize(rpr)
        parts.append(ph_open("r", i) + run.text + ph_close("r", i))
    return "".join(parts), inline_map


def _serialize(el) -> str:
    from lxml import etree

    return etree.tostring(el).decode("utf-8")


def _parse_rpr(xml: str):
    from lxml import etree

    return etree.fromstring(xml.encode("utf-8"))


def _split_on_run_tokens(text: str):
    """Split translated text into (rpr_key_or_None, span_text) pairs.

    Text inside a ⟦rN⟧…⟦/rN⟧ pair carries key "N"; text outside any pair carries
    None (fallback formatting). Tolerant of the (mock/LLM) translation dropping,
    duplicating, or reordering tokens: unmatched close tokens are ignored and any
    trailing open run simply extends to the end.

    IT IS FLAT, DELIBERATELY, AND THAT IS A PRECONDITION SOMEONE ELSE ENFORCES. A close
    token returns to key `None`; it does NOT pop back to the ENCLOSING open. So for a
    NESTED target (`⟦r1⟧⟦r2⟧b⟦/r2⟧a⟦/r1⟧`) the text after `⟦/r2⟧` is emitted keyless and
    delivered UNFORMATTED, even though it sits inside r1. That is not fixable here in any
    honest way: a `<w:r>` cannot nest, so representing "inside r1 AND r2" would mean
    COMPOSING two rPr snapshots, and popping to the enclosing open alone would still put
    r2's text in r2's formatting only. Under conservative quarantine semantics (GitHub
    #42, advisory dropped) ANY tag divergence — a reorder that changed the nesting shape
    included — is EXPORT-BLOCKING via `quality.tag_damage`, so the default paths never
    hand this function a shape it cannot represent: FULL_TEXT quarantines it, SEGMENTED
    substitutes the source. The one
    remaining route in is the explicit `export_tag_damage_policy="ship"` opt-out, whose
    whole meaning is "deliver it anyway"; the loss it then produces is measured and pinned
    (verify_ai_tagging [12b]). If that rung is ever removed, every path becomes a silent
    formatting-loss path again."""
    import re

    from app.services.segment import _PLACEHOLDER_RE

    spans: list[tuple[str | None, str]] = []
    cur_key: str | None = None
    pos = 0
    buf: list[str] = []

    def flush():
        if buf:
            spans.append((cur_key, "".join(buf)))
            buf.clear()

    for m in _PLACEHOLDER_RE.finditer(text):
        buf.append(text[pos : m.start()])
        pos = m.end()
        tok = m.group(0)
        inner = tok[1:-1]  # strip ⟦ ⟧
        if inner.startswith("/"):
            # close token — end whatever run we're in
            flush()
            cur_key = None
        else:
            # open token — start a new run span
            flush()
            key = re.sub(r"^r", "", inner)
            cur_key = key if key.isdigit() else None
    buf.append(text[pos:])
    flush()
    return spans


# Paragraph children (besides direct <w:r> runs) that carry visible text: a
# hyperlink span, a tracked-change insert, a smartTag, a field's result. When we
# fall back to whole-paragraph replacement we must drop these so their original
# text doesn't survive alongside the translation (double text on export).
_TEXT_CHILDREN = tuple(qn(t) for t in ("w:hyperlink", "w:ins", "w:smartTag", "w:fldSimple"))


def _remove_out_of_run_text(para) -> None:
    """Strip paragraph children that hold text outside the direct <w:r> runs."""
    p = para._p
    for child in list(p):
        if child.tag in _TEXT_CHILDREN:
            p.remove(child)


def _set_paragraph_text(para, text: str, inline_map: dict) -> None:
    """Replace a paragraph's text, rebuilding per-run formatting from tokens.

    When the translated text contains no run tokens (single-run paragraph, or a
    translation that dropped them all) we keep the historical behavior: write
    everything into the first CONTENT run (see `_content_runs` — an empty `<w:r/>`
    is not a formatting boundary and must not receive the write) and blank the rest.

    A paragraph whose direct runs don't cover its text (hyperlink/<w:ins>/smartTag/
    field — the parse() fallback left it untokenized) is rewritten wholesale: the
    out-of-run text children are removed first so the translation doesn't collide
    with leftover original text."""
    spans = _split_on_run_tokens(text)
    has_tokens = any(k is not None for k, _ in spans)

    if not has_tokens or not inline_map:
        plain = "".join(t for _, t in spans)
        if not _runs_cover_paragraph(para):
            _remove_out_of_run_text(para)
        # Write into the first CONTENT run, not `para.runs[0]`. `_tokenize_paragraph`
        # decides whether to tokenize by counting CONTENT runs (`_content_runs` drops
        # empties, "so tokens track real formatting boundaries only") — but this branch
        # then read `para.runs`, empties included. Real Word emits `[<w:r/>, <w:r>bold]`
        # constantly (rsid splits, revision merges, bookmark boundaries), so a paragraph
        # like `[empty, bold "Section 3."]` got the translation in the UNFORMATTED run
        # and its bold run blanked: formatted 1 -> 0 with the target BYTE-IDENTICAL to
        # the source. Pure round-trip loss, no model involved (4 of 16 formatted
        # paragraphs lost with a perfect translation).
        content = _content_runs(para)
        keep = content[0] if content else (para.runs[0] if para.runs else None)
        if keep is not None:
            keep.text = plain
            # python-docx mints a FRESH Run proxy on every `para.runs` access, so
            # `r is not keep` compares two different proxies wrapping the SAME element
            # and would silently blank the run we just wrote. Compare ELEMENT identity.
            for r in para.runs:
                if r._r is not keep._r:
                    r.text = ""
        else:
            para.add_run(plain)
        return

    # Rebuild: drop the original runs, add one Run per span with its snapped rPr.
    # Zero-width range markers (bookmark, comment range, proofErr, ...) sit BETWEEN the
    # runs; add_run appends at the paragraph's end, so they are detached first and put
    # back at the matching run boundary afterwards (#509: ranges collapsed to empty).
    content_els = {r._r for r in _content_runs(para)}
    n_content = len(content_els)
    markers: list[tuple[int, object]] = []
    seen_content = 0
    for child in list(para._p):
        if child.tag == _W_R:
            if child in content_els:
                seen_content += 1
        elif child.tag in _MARKERS:
            markers.append((seen_content, child))
            para._p.remove(child)
    for r in list(para.runs):
        r._r.getparent().remove(r._r)
    new_runs = []
    for key, span_text in spans:
        if not span_text:
            continue
        run = para.add_run(span_text)
        new_runs.append(run._r)
        rpr_xml = inline_map.get(key) if key is not None else None
        if rpr_xml:
            existing = run._r.find(_RPR)
            if existing is not None:
                run._r.remove(existing)
            run._r.insert(0, _parse_rpr(rpr_xml))
    _restore_range_markers(para._p, new_runs, markers, n_content)


def _restore_range_markers(p, new_runs: list, markers: list, n_old: int) -> None:
    """Re-insert detached markers at the run boundary they originally had, scaled to the
    new run count when a translation merged or split runs (order is always preserved)."""
    if not markers:
        return
    if not new_runs:
        for _, m in markers:
            p.append(m)
        return
    n_new = len(new_runs)
    anchor_after: dict[int, object] = {}
    for before, m in markers:
        b = before if n_old <= 0 else round(before * n_new / n_old)
        b = max(0, min(n_new, b))
        if b == 0:
            new_runs[0].addprevious(m)
        else:
            anchor = anchor_after.get(b, new_runs[b - 1])
            anchor.addnext(m)
            anchor_after[b] = m


# --- non-text objects: pictures, fields, notes, breaks (object-aware paragraphs) ----
# The two paths above only know TEXT: `Run.text = ...` clears every child of a run, so
# a picture, chart, text box, OLE object, field character, footnote reference, symbol
# or page break sharing a paragraph with translated text was DELETED on export.
#
# A paragraph that holds any such object is handled here instead ("object mode"). The
# paragraph is walked in document order into ATOMS:
#   * text   - the text of one run (w:t / w:tab / text-wrapping w:br / w:cr / ...);
#   * object - any other run child (w:drawing, w:pict, mc:AlternateContent, w:object,
#              w:fldChar, w:instrText, w:footnoteReference, w:sym, a page w:br, ...)
#              or a paragraph-level element the filter never translated (w:fldSimple,
#              w:ins, w:del, w:smartTag, w:sdt, m:oMath, ...);
#   * marker - a zero-width range marker (bookmarks, comment ranges, proofErr, ...).
# A w:hyperlink is a CONTAINER: its runs are walked (their text is translated) and the
# hyperlink is re-created around them on export, keeping its attributes (r:id, anchor,
# history) and each run's rPr. A paragraph with hyperlink text is in object mode even
# without any object: the text-only fallback has no way to put a link back, and used
# to delete the w:hyperlink wholesale (a link-only paragraph came back unlinked).
#
# Consecutive objects form one GROUP carried by one point token `⟦xN⟧` that the
# translator keeps, like the existing run tokens. Text is wrapped in `⟦rN⟧` pairs per
# ORIGINAL RUN exactly as the multi-run path does (so a translation stored before object
# mode existed still maps its formatting), and only when there is more than one run; a
# run with text on both sides of an object keeps ONE pair with the x token inside it
# (`⟦r1⟧X⟦x3⟧Y⟦/r1⟧`). x ids start after the last r id so a kind-agnostic pair matcher
# never confuses them. Markers carry no token: each travels with the atom that followed
# it. Between 2026-09-25 and this numbering, r was counted per text ATOM; build() still
# reads a translation stored that way when its ids only fit that scheme.
#
# On export the paragraph is rebuilt from the translated text: each x token puts its
# ORIGINAL elements back (moved, not copied, so drawing ids, relationships and the alt
# write-back keep pointing at the same XML). A group whose token the translation lost
# (the QA placeholder flag already reports a dropped token) is never deleted: it goes
# back right after the text it originally followed, at the start when it led the
# paragraph, else at the end. A duplicated token is placed once.
_W_R = qn("w:r")
_W_HYPERLINK = qn("w:hyperlink")
_W_BR = qn("w:br")
_W_ID = qn("w:id")
_TEXT_RUN_CHILDREN = frozenset(qn(t) for t in ("w:t", "w:tab", "w:cr", "w:noBreakHyphen", "w:ptab"))
# Run children python-docx's own `Run.text` setter drops and Word regenerates.
_DROPPABLE_RUN_CHILDREN = frozenset(
    qn(t) for t in ("w:rPr", "w:lastRenderedPageBreak", "w:softHyphen")
)
_MARKERS = frozenset(
    qn("w:" + t)
    for t in (
        "bookmarkStart",
        "bookmarkEnd",
        "proofErr",
        "commentRangeStart",
        "commentRangeEnd",
        "permStart",
        "permEnd",
        "moveFromRangeStart",
        "moveFromRangeEnd",
        "moveToRangeStart",
        "moveToRangeEnd",
        "customXmlInsRangeStart",
        "customXmlInsRangeEnd",
        "customXmlDelRangeStart",
        "customXmlDelRangeEnd",
        "customXmlMoveFromRangeStart",
        "customXmlMoveFromRangeEnd",
        "customXmlMoveToRangeStart",
        "customXmlMoveToRangeEnd",
    )
)
_PPR = qn("w:pPr")


def _is_text_child(el) -> bool:
    if el.tag in _TEXT_RUN_CHILDREN:
        return True
    return el.tag == _W_BR and (el.get(qn("w:type")) or "textWrapping") == "textWrapping"


class _Atom:
    __slots__ = ("ctx", "el", "group", "kind", "markers", "path", "rkey", "run", "text")

    def __init__(self, kind, ctx=None, run=None, el=None, text=""):
        self.kind = kind  # "text" | "obj" | "marker"
        self.ctx = ctx  # the ORIGINAL w:hyperlink holding it, or None
        self.run = run  # the ORIGINAL w:r (text atoms and run-child objects)
        self.el = el  # the element itself (objects and markers)
        self.text = text
        self.markers: list = []  # markers that preceded this atom
        self.group = None  # object group id (objects only)
        self.rkey = None  # index of its original run among the text runs (text only)
        self.path = None  # walk 6: the wrappers holding it, outermost first (ctx = last)


def _walk_runs(r, ctx, out: list) -> None:
    buf: list[str] = []

    def flush():
        if buf:
            out.append(_Atom("text", ctx=ctx, run=r, text="".join(buf)))
            buf.clear()

    for child in r:
        if not isinstance(child.tag, str) or child.tag in _DROPPABLE_RUN_CHILDREN:
            continue
        if _is_text_child(child):
            buf.append(str(child))
        else:
            flush()
            out.append(_Atom("obj", ctx=ctx, run=r, el=child))
    flush()


def _walk_paragraph(p, ins: int = 0) -> list:
    """Flat atom list for one w:p, in document order. Never descends into an object
    (a text box's own paragraphs live INSIDE its drawing and travel with it).

    With `ins` (walk 4; 5 adds w:smartTag, see `_containers`) a tracked insertion (w:ins) and a
    move destination (w:moveTo) are
    CONTAINERS like a hyperlink: their runs are text as Word shows it and the wrapper is
    re-created around their translation. A tracked deletion (w:del) and a move source
    (w:moveFrom) stay untouched objects either way. Walk 6 nests (`_walk_paragraph_v6`)."""
    if ins >= 6:
        return _walk_paragraph_v6(p)
    out: list = []

    containers = _containers(ins)

    def visit(el, ctx):
        if not isinstance(el.tag, str) or el.tag == _W_SMARTTAGPR:
            return  # (a smart tag's properties are copied onto its re-created wrapper)
        if el.tag == _W_R:
            _walk_runs(el, ctx, out)
        elif el.tag in _MARKERS:
            out.append(_Atom("marker", ctx=ctx, el=el))
        else:
            out.append(_Atom("obj", ctx=ctx, el=el))

    for child in p:
        if not isinstance(child.tag, str) or child.tag == _PPR:
            continue
        if child.tag == _W_HYPERLINK or child.tag in containers:
            for sub in child:
                visit(sub, child)
        else:
            visit(child, None)
    # Empty text atoms carry nothing; drop them so they are not formatting boundaries.
    return [a for a in out if a.kind != "text" or a.text]


def _object_layout(p, ins: int = 0):
    """(texts, groups, tail_markers, atoms, runs) for an object-mode paragraph, or None
    when the paragraph holds neither an object nor hyperlink text (it then keeps the
    historical text-only paths).

    texts:  the text atoms, in order;
    groups: list of object-atom lists, in order (x id = x_base + position);
    tail_markers: markers after the last atom;
    runs:   the text atoms grouped by their ORIGINAL run, in order (r id = position + 1;
            a run split by an object has more than one atom)."""
    atoms = _walk_paragraph(p, ins)
    if not any(a.kind == "obj" or (a.kind == "text" and a.ctx is not None) for a in atoms):
        return None
    texts: list = []
    groups: list = []
    runs: list = []
    pending: list = []
    prev = None
    for a in atoms:
        if a.kind == "marker":
            pending.append((a.el, a.ctx))
            continue
        a.markers = pending
        pending = []
        if a.kind == "text":
            texts.append(a)
            if runs and runs[-1][0].run is a.run:
                runs[-1].append(a)
            else:
                runs.append([a])
            a.rkey = len(runs) - 1
        else:
            if prev is not None and prev.kind == "obj":
                groups[-1].append(a)
            else:
                groups.append([a])
            a.group = len(groups) - 1
        prev = a
    return texts, groups, pending, atoms, runs


def _x_base(n_runs: int) -> int:
    return n_runs + 1 if n_runs > 1 else 1


def _tokenize_objects(layout) -> tuple[str, dict]:
    _texts, _groups, _tail, atoms, runs = layout
    wrap = len(runs) > 1
    base = _x_base(len(runs))
    parts: list[str] = []
    inline_map: dict = {}
    seen_groups: set = set()
    for a in atoms:
        if a.kind == "text":
            members = runs[a.rkey]
            i = a.rkey + 1
            if wrap and a is members[0]:
                rpr = a.run.find(_RPR)
                inline_map[str(i)] = None if rpr is None else _serialize(rpr)
                parts.append(ph_open("r", i))
            parts.append(a.text)
            if wrap and a is members[-1]:
                parts.append(ph_close("r", i))
        elif a.kind == "obj" and a.group not in seen_groups:
            seen_groups.add(a.group)
            xid = base + a.group
            inline_map["x" + str(xid)] = "object"
            parts.append(ph_point("x", xid))
    return "".join(parts), inline_map


def _split_object_tokens(text: str, layout) -> list[tuple]:
    """Translated text -> [("text", atom_or_None, s) | ("x", group_index)].

    Mirrors `_split_on_run_tokens` for r pairs (a close returns to no key; an unknown
    token resets). A point x token does NOT end the r span it sits in, so the pieces of
    one r pair land on that run's atoms in turn (clamped to its last). With no r token
    at all, every piece goes to the first DIRECT run, where the text-only path put it.

    Ids are read per original run. A translation stored while r was counted per text
    atom (only differs when a run has text on both sides of an object) is recognized
    by ids that fit that scheme and not this one, and read that way. With DAMAGED tokens
    the pick can be wrong (formatting or an object lands one run off, nothing is lost);
    tag_damage flags every such target, so only export_tag_damage_policy=ship gets here."""
    import re

    from app.services.segment import _PLACEHOLDER_RE

    texts, groups, _tail, _atoms, runs = layout
    raw: list[tuple] = []
    pos = 0
    for m in _PLACEHOLDER_RE.finditer(text):
        if m.start() > pos:
            raw.append(("t", text[pos : m.start()]))
        pos = m.end()
        inner = m.group(0)[1:-1]
        mx = re.fullmatch(r"x(\d+)", inner)
        mr = re.fullmatch(r"r(\d+)", inner)
        if mx:
            raw.append(("x", int(mx.group(1))))
        elif mr:
            raw.append(("r", int(mr.group(1))))
        else:
            raw.append(("reset", None))
    if pos < len(text):
        raw.append(("t", text[pos:]))

    def fits(n: int) -> bool:
        base = _x_base(n)
        return all(
            (k != "r" or 1 <= v <= n) and (k != "x" or 0 <= v - base < len(groups)) for k, v in raw
        )

    # Each r id names a list of atoms: per run (current), or one atom (per-atom legacy).
    owners = runs
    if len(texts) != len(runs) and not fits(len(runs)) and fits(len(texts)):
        owners = [[t] for t in texts]
    base = _x_base(len(owners))
    has_r = any(k == "r" and 1 <= v <= len(owners) for k, v in raw)
    default = None
    if not has_r and texts:
        first = next((t for t in texts if t.ctx is None), texts[0])
        default = next(i for i, o in enumerate(owners) if first in o)
    used: dict = {}
    items: list[tuple] = []
    cur = default
    for k, v in raw:
        if k == "t":
            if cur is None:
                items.append(("text", None, v))
            else:
                n = used.get(cur, 0)
                used[cur] = n + 1
                items.append(("text", owners[cur][min(n, len(owners[cur]) - 1)], v))
        elif k == "x" and 0 <= v - base < len(groups):
            items.append(("x", v - base))
        elif k == "r" and 1 <= v <= len(owners):
            cur = v - 1
        elif has_r:
            cur = None
    return items


def _fix_marker_order(seq: list) -> list:
    """A range end marker placed before its start (a reordered translation) is moved
    to sit right after the start, so no bookmark or comment range is inverted."""

    def name(el) -> str:
        return el.tag.rsplit("}", 1)[-1]

    result = list(seq)
    for item in list(result):
        if item[0] != "marker" or not name(item[1]).endswith("End"):
            continue
        stem, mid = name(item[1])[:-3], item[1].get(_W_ID)
        s_pos = next(
            (
                i
                for i, it in enumerate(result)
                if it[0] == "marker" and name(it[1]) == stem + "Start" and it[1].get(_W_ID) == mid
            ),
            None,
        )
        e_pos = next(i for i, it in enumerate(result) if it is item)
        if s_pos is not None and e_pos < s_pos:
            result.pop(e_pos)
            result.insert(s_pos, item)
    return result


_W_FLDCHAR = qn("w:fldChar")
_FLD_TYPE = qn("w:fldCharType")


def _fld_types(group: list) -> list:
    return [a.el.get(_FLD_TYPE) for a in group if a.el is not None and a.el.tag == _W_FLDCHAR]


def _fld_residue(types: list) -> list:
    """What is left of a fldChar begin/separate/end sequence once every well-formed
    field is cancelled out. A paragraph can legitimately hold an unbalanced piece of
    a field that spans paragraphs (a TOC), so the residue is compared, not required to
    be empty."""
    stack: list = []
    for t in types:
        if t == "begin":
            stack.append("b")
        elif t == "separate" and stack and stack[-1] == "b":
            stack[-1] = "bs"
        elif t == "end" and stack and stack[-1] in ("b", "bs"):
            stack.pop()
        elif t in ("separate", "end"):
            stack.append(t[0])
    return stack


def _fix_field_order(seq: list, groups: list) -> list:
    """A complex field whose groups the translation reordered (its end token before its
    begin token) makes Word dissolve the field and show its instruction text (for
    example `PAGE`) in the body. When the emitted fldChar sequence no longer reduces the
    way the original did, the groups holding fldChars are put back in their ORIGINAL
    order in the slots the translation gave them; text and other groups stay put."""
    slots = [i for i, s in enumerate(seq) if s[0] == "group" and _fld_types(groups[s[1]])]
    if len(slots) < 2:
        return seq
    emitted = [t for i in slots for t in _fld_types(groups[seq[i][1]])]
    ordered = sorted(seq[i][1] for i in slots)
    original = [t for g in ordered for t in _fld_types(groups[g])]
    if _fld_residue(emitted) == _fld_residue(original):
        return seq
    out = list(seq)
    for i, g in zip(slots, ordered, strict=True):
        out[i] = ("group", g)
    return out


def _fields_in_links(atoms: list) -> dict:
    """{id(begin fldChar atom): (id(its end fldChar atom), n)} for every complex field whose
    begin and end sit in the SAME w:hyperlink in the source (a TOC entry's PAGEREF). Walk 6
    (#686 gate): begin and end inside the same OUTER wrapper count too (the end in a smart
    tag inside the hyperlink that holds the begin); `n` is how many wrappers, outermost
    first, they share (older walks: 1, the same container)."""

    def chain(a) -> tuple:
        if a.path is not None:
            return a.path
        return () if a.ctx is None else (a.ctx,)

    out: dict = {}
    stack: list = []
    for a in atoms:
        if a.kind != "obj" or a.el is None or a.el.tag != _W_FLDCHAR:
            continue
        kind = a.el.get(_FLD_TYPE)
        if kind == "begin":
            stack.append(a)
        elif kind == "end" and stack:
            begin = stack.pop()
            x, y = chain(begin), chain(a)
            n = 0
            while n < len(x) and n < len(y) and x[n] is y[n]:
                n += 1
            if n:
                out[id(begin)] = (id(a), n)
    return out


def _rehome_loose_text(seq: list, groups: list, paths=None) -> list:
    """Give a text piece the translation put OUTSIDE every r pair (a stray space between
    two tokens is enough) the container of its neighbours when both neighbours sit in
    the same hyperlink. Emitted bare, it would close that hyperlink and open a second
    one: one link turned into several, and a field inside the link split across two
    hyperlinks (Word refuses the file). Its formatting comes from the neighbouring text
    run in that hyperlink, when there is one. With nested wrappers (walk 6, `paths`: the
    wrapper chain of each container) it goes into the deepest wrapper both neighbours
    share."""

    def chain(ctx) -> tuple:
        if ctx is None:
            return ()
        return (paths or {}).get(id(ctx), (ctx,))

    def edge(s, last: bool):
        """(ctx, text atom or None) of a sequence item as seen from one side."""
        if s[0] == "group":
            members = groups[s[1]]
            return (members[-1] if last else members[0]).ctx, None
        if s[1] is not None:
            return s[1].ctx, s[1]
        return (s[3], s[4]) if len(s) > 3 else (None, None)

    out = list(seq)
    for i, s in enumerate(out):
        if s[0] != "text" or s[1] is not None:
            continue
        prev_ctx, prev_fmt = edge(out[i - 1], last=True) if i > 0 else (None, None)
        j = i + 1
        while j < len(out) and out[j][0] == "text" and out[j][1] is None:
            j += 1
        next_ctx, next_fmt = edge(out[j], last=False) if j < len(out) else (None, None)
        a, b = chain(prev_ctx), chain(next_ctx)
        k = 0
        while k < len(a) and k < len(b) and a[k] is b[k]:
            k += 1
        if k:
            out[i] = ("text", None, s[2], a[k - 1], prev_fmt or next_fmt)
    return out


def _marker_items(markers: list, ctx, path=None) -> list:
    """Emission items for markers placed next to an atom in container `ctx`. A marker
    keeps its hyperlink only when the atom shares it (never an empty hyperlink). Walk 6
    (`path`, the atom's wrapper chain): a marker in any wrapper of that chain goes into
    the atom's own wrapper, so it never splits the chain."""
    if path is None:
        return [("marker", el, mctx if mctx is ctx else None) for el, mctx in markers]
    return [
        ("marker", el, ctx if mctx is not None and any(mctx is c for c in path) else None)
        for el, mctx in markers
    ]


def _set_paragraph_objects(para, text: str, layout, new_id=None) -> None:
    """Rebuild an object-mode paragraph from its translation (see the section above).

    A tracked change (walk 4) the translation split into several pieces is re-created once
    per piece; every piece after the first gets a fresh revision id from `new_id` (a
    revision's w:id must be unique in the document), author and date kept."""
    from copy import deepcopy

    from docx.oxml.parser import OxmlElement
    from docx.text.run import Run

    texts, groups, tail, _atoms, _runs = layout
    items = _split_object_tokens(text, layout)

    # 1. The emission sequence: ("text", atom_or_None, s) | ("group", g).
    seq: list = []
    placed: set = set()
    for it in items:
        if it[0] == "x":
            if it[1] not in placed:
                placed.add(it[1])
                seq.append(("group", it[1]))
        elif it[2]:
            seq.append(("text", it[1], it[2]))
    # 2. Groups the translation lost: back after the text they followed.
    atom_pos = {id(a): i for i, a in enumerate(_atoms)}
    for g, members in enumerate(groups):
        if g in placed:
            continue
        first = members[0]
        before = [t for t in texts if atom_pos[id(t)] < atom_pos[id(first)]]
        anchor = before[-1] if before else None
        if anchor is None:
            at = 0
            while (
                at < len(seq) and seq[at][0] == "group" and seq[at][1] in placed and seq[at][1] < g
            ):
                at += 1
        else:
            hits = [i for i, s in enumerate(seq) if s[0] == "text" and s[1] is anchor]
            if not hits:  # its run's text came back in one piece (a legacy translation)
                hits = [
                    i
                    for i, s in enumerate(seq)
                    if s[0] == "text" and s[1] is not None and s[1].run is anchor.run
                ]
            at = hits[-1] + 1 if hits else len(seq)
            while at < len(seq) and seq[at][0] == "group" and seq[at][1] < g:
                at += 1
        seq.insert(at, ("group", g))
        placed.add(g)
    seq = _fix_field_order(seq, groups)
    # each container's wrapper chain, outermost first (walk 6 nests; older walks: itself)
    paths = {id(a.ctx): a.path for a in _atoms if a.ctx is not None and a.path is not None}
    seq = _rehome_loose_text(seq, groups, paths)
    # 3. Markers travel before the first emission of the atom they preceded.
    final: list = []
    marked: set = set()
    for s in seq:
        owner = s[1] if s[0] == "text" else groups[s[1]][0]
        if owner is not None and id(owner) not in marked:
            marked.add(id(owner))
            atoms_here = [owner] if s[0] == "text" else groups[s[1]]
            for a in atoms_here:
                final.extend(_marker_items(a.markers, a.ctx, a.path))
                if s[0] == "group":
                    final.append(("obj", a))
            if s[0] == "text":
                final.append(s)
        elif s[0] == "group":
            final.extend(("obj", a) for a in groups[s[1]])
        else:
            final.append(s)
    for t in texts:  # markers of a text atom the translation never used
        if id(t) not in marked:
            final.extend(_marker_items(t.markers, None))
    final.extend(_marker_items(tail, None))
    final = _fix_marker_order(final)

    # Walk 6: a w:bdo / w:dir whose direction matched its source text follows the
    # translation's direction (`_fit_direction`); read before the source is taken apart.
    fits_source = {
        id(w)
        for chain in paths.values()
        for w in chain
        if w.tag in (_W_BDO, _W_DIR)
        and w.get(_W_VAL) in ("ltr", "rtl")
        and _strong_direction("".join(t.text or "" for t in w.iter(_W_T))) == w.get(_W_VAL)
    }
    fit: list = []
    # 4. Materialize.
    p = para._p
    for child in list(p):
        if child.tag != _PPR:
            p.remove(child)
    # "open": the re-created wrappers the next item lands in, as (original, content box)
    state = {"ctx": None, "box": p, "wrap_for": None, "wrap": None, "open": []}
    # Complex fields that begin and end inside ONE hyperlink in the source: while one is
    # open in a hyperlink clone, nothing may close that clone. A field whose fldChars
    # land in two hyperlinks is a file Word refuses to open.
    field_ends = _fields_in_links(_atoms)
    open_ends: list = []
    floors: dict = {}  # end atom id -> how many open wrappers its field must stay inside
    cloned: set = set()  # ids of the containers already re-created once

    def box_for(ctx):
        if ctx is not state["ctx"]:
            want = () if ctx is None else paths.get(id(ctx), (ctx,))
            opened = state["open"]
            if open_ends:
                # A field open in a wrapper chain: only a container inside the wrappers
                # the field shares may open (walk 6: a smart tag inside the hyperlink);
                # anything else lands where the field is, nothing closes those wrappers.
                floor = max(floors[e] for e in open_ends)
                if len(want) < floor or len(opened) < floor:
                    return state["box"]
                if any(opened[i][0] is not want[i] for i in range(floor)):
                    return state["box"]
            state["wrap_for"] = state["wrap"] = None
            # Keep the wrappers the new container shares with the open chain (walk 6: text
            # after an inner smart tag stays in the outer one), re-create the rest.
            k = 0
            while k < len(opened) and k < len(want) and opened[k][0] is want[k]:
                k += 1
            del opened[k:]
            box = opened[-1][1] if opened else p
            for orig in want[k:]:
                clone, inner = _clone_container(orig, id(orig) in cloned, new_id)
                cloned.add(id(orig))
                if id(orig) in fits_source:
                    fit.append(clone)
                box.append(clone)
                opened.append((orig, inner))
                box = inner
            state["box"] = box
            state["ctx"] = ctx
        return state["box"]

    for item in final:
        kind = item[0]
        if kind == "marker":
            box_for(item[2]).append(item[1])
            state["wrap_for"] = state["wrap"] = None
        elif kind == "obj":
            a = item[1]
            box = box_for(a.ctx)
            if a.run is None:
                box.append(a.el)
                state["wrap_for"] = state["wrap"] = None
                continue
            if state["wrap_for"] is not a.run:
                r = OxmlElement("w:r")
                rpr = a.run.find(_RPR)
                if rpr is not None:
                    r.append(deepcopy(rpr))
                box.append(r)
                state["wrap_for"], state["wrap"] = a.run, r
            state["wrap"].append(a.el)
            if id(a) in field_ends and state["ctx"] is not None:
                end_id, floor = field_ends[id(a)]
                open_ends.append(end_id)
                floors[end_id] = floor
            elif id(a) in open_ends:
                del open_ends[open_ends.index(id(a)) :]
        else:
            a, s = item[1], item[2]
            # A piece with no run of its own (see `_rehome_loose_text`) may carry the
            # container and the formatting source it was given.
            ctx = item[3] if len(item) > 3 else (a.ctx if a is not None else None)
            fmt = item[4] if len(item) > 4 else a
            box = box_for(ctx)
            r = OxmlElement("w:r")
            if fmt is not None:
                rpr = fmt.run.find(_RPR)
                if rpr is not None:
                    r.append(deepcopy(rpr))
            box.append(r)
            Run(r, para).text = s
            state["wrap_for"] = state["wrap"] = None
    _fit_direction(fit)


# --- accessibility: alt text units, target language, missing-alt findings ---------
# A drawing's alternative text lives on its <wp:docPr> (descr = the alt text Word's
# "Alt Text" pane edits; title = the older "title" field). Those values are extracted
# as `a11y:*` units APPENDED after every paragraph index (p_index >= len(paragraphs)),
# so body units keep their indices for documents ingested before this existed.
# Stamps 3 and 4: body drawings only. Stamp 5 (#368) adds the drawings of the headers,
# footers, footnotes, endnotes and comments the walk opens, after the body's (`_alt_units`).
_DOCPR = qn("wp:docPr")
_PIC_CNVPR = qn("pic:cNvPr")
_W_P = qn("w:p")
_W_LANG = qn("w:lang")
_ALT_ATTRS = (("descr", "a11y:img-alt"), ("title", "a11y:img-title"))
# CT_PPrBase elements that must FOLLOW <w:bidi> (schema order).
_BIDI_SUCCESSORS = (
    "w:adjustRightInd",
    "w:snapToGrid",
    "w:spacing",
    "w:ind",
    "w:contextualSpacing",
    "w:mirrorIndents",
    "w:suppressOverlap",
    "w:jc",
    "w:textDirection",
    "w:textAlignment",
    "w:textboxTightWrap",
    "w:outlineLvl",
    "w:divId",
    "w:cnfStyle",
    "w:rPr",
    "w:sectPr",
    "w:pPrChange",
)
_LANG_SLOT = {"latin": qn("w:val"), "eastAsia": qn("w:eastAsia"), "bidi": qn("w:bidi")}


def _alt_units(doc, numbering: "_Walk | None" = None) -> list[tuple]:
    """(docPr element, attribute, text, style, part name) for every translatable alt/title,
    in body document order. Identical in parse() and build(). With `numbering` (walk 5) the
    drawings of the other parts walk 4 opened follow the body's, in the order the walk
    numbers the parts (headers, footers, footnotes, endnotes, comments)."""
    out = []
    main_name = str(doc.part.partname).lstrip("/")
    roots = [(main_name, doc.element.body)]
    if numbering is not None:
        roots.extend((name, root) for name, (_part, root, _blob) in numbering.roots.items())
    for name, root in roots:
        for dp in root.iter(_DOCPR):
            for attr, style in _ALT_ATTRS:
                value = (dp.get(attr) or "").strip()
                if value and any(c.isalnum() for c in value):
                    out.append((dp, attr, value, style, name))
    return out


def _set_alt(dp, attr: str, new: str) -> None:
    """Write translated alt text onto the docPr and onto the picture's own cNvPr when
    that carried the same source text (some producers duplicate it there)."""
    old = dp.get(attr)
    dp.set(attr, new)
    container = dp.getparent()
    if container is None:
        return
    for cnv in container.iter(_PIC_CNVPR):
        if old is not None and cnv.get(attr) == old:
            cnv.set(attr, new)


def _is_decorative(dp) -> bool:
    """Word 365 marks a decorative image with an adec:decorative val="1" extension."""
    for el in dp.iter():
        tag = el.tag if isinstance(el.tag, str) else ""
        if tag.endswith("}decorative") and (el.get("val") or "").lower() in ("1", "true"):
            return True
    return False


def _missing_alt_findings(doc, paras: list, unit_indices: list[int]) -> tuple[int, list]:
    """(image count, findings) for body drawings with no or blank alt text. A drawing
    Word marks decorative is counted but never flagged."""
    from app.filters import a11y

    index_of = {p._p: i for i, p in enumerate(paras)}
    spans = [(i, i, i) for i in unit_indices]
    count = 0
    found = []
    for dp in doc.element.body.iter(_DOCPR):
        count += 1
        if _is_decorative(dp):
            continue
        descr = dp.get("descr")
        if descr is not None and descr.strip():
            continue
        p = dp
        while p is not None and p.tag != _W_P:
            p = p.getparent()
        pos = index_of.get(p, -1) if p is not None else -1
        anchor = (
            a11y.anchor_for(pos, spans) if pos >= 0 else (unit_indices[0] if unit_indices else None)
        )
        kind = "missing" if descr is None else "empty"
        found.append(a11y.missing_alt(kind, anchor, dp.get("name") or ""))
    return count, found


def _has_part(rels, reltype: str) -> bool:
    """True when `rels` already relates a part of `reltype`. Checked BEFORE any access
    that would make python-docx invent the part (doc.styles / doc.core_properties create
    a default styles.xml / a core.xml titled "Word Document" when it is missing)."""
    return any(r.reltype == reltype and not r.is_external for r in rels.values())


def _set_run_lang(run_el, slot: str, tag: str) -> None:
    """Give one <w:r> an explicit w:lang in `slot` (creating rPr / w:lang in schema order)."""
    from docx.oxml.parser import OxmlElement

    rpr = run_el.get_or_add_rPr()
    lang = rpr.find(_W_LANG)
    if lang is None:
        lang = OxmlElement("w:lang")
        rpr.insert_element_before(lang, "w:eastAsianLayout", "w:specVanish", "w:oMath")
    lang.set(slot, tag)


# #722: Word keeps separate run properties for complex-script text (Arabic, Hebrew, Persian,
# Urdu, Thai, Lao, Khmer, Burmese, Hindi and the other Indic scripts): `w:b` / `w:i` / `w:sz`
# apply only to the NON-complex-script characters of a run, `w:bCs` / `w:iCs` / `w:szCs` to the
# complex-script ones (ECMA-376 Part 1, 17.3.2.1-2, 17.3.2.16-17, 17.3.2.38-39). An English run
# with only `w:b` therefore shows its Arabic translation regular weight (LibreOffice and Word
# both). For such a target each translated run gets the missing twin, same value.
_CS_TWINS = (("w:b", "w:bCs"), ("w:i", "w:iCs"), ("w:sz", "w:szCs"))


def _complex_script_target(tag: str, direction: str) -> bool:
    from app.filters.a11y import COMPLEX_SCRIPT_LANGS  # one list for #722 and #730

    return direction == "rtl" or (tag or "").split("-")[0].lower() in COMPLEX_SCRIPT_LANGS


def _mirror_cs_props(run_el) -> None:
    """Give a translated run the complex-script twin of its bold / italic / size (#722)."""
    from copy import deepcopy

    rpr = run_el.find(_RPR)
    if rpr is None:
        return
    for name, twin in _CS_TWINS:
        el = rpr.find(qn(name))
        if el is None or rpr.find(qn(twin)) is not None:
            continue
        new = deepcopy(el)
        new.tag = qn(twin)
        el.addnext(new)  # the schema puts each twin right after its base property


def _translated_runs(p, ins: int = 0) -> list:
    """The runs of `p` whose text this filter writes: its direct w:r children and the
    runs of its direct w:hyperlink children. Everything else in the paragraph travels
    verbatim in the SOURCE language and must not be declared as the target: runs inside
    a picture, VML shape, mc:AlternateContent or text box (w:drawing / w:pict /
    w:txbxContent, their own paragraphs are not translated), tracked deletions (w:del),
    and w:ins (walk 3 and older), w:sdt, w:smartTag, w:fldSimple, w:moveFrom / w:moveTo,
    which the walker carries as x objects without translating their text. Walk 4 (`ins`)
    translates the runs of a w:ins and of a w:moveTo too, walk 5 those of a w:smartTag, walk
    6 the runs inside nested wrappers and text content controls (`_w6_runs`)."""
    if ins >= 6:
        return _w6_runs(p)
    out = []
    containers = _containers(ins)
    for child in p:
        if child.tag == _W_R:
            out.append(child)
        elif child.tag == _W_HYPERLINK or child.tag in containers:
            out.extend(sub for sub in child if sub.tag == _W_R)
    return out


def _style_langs(styles, slot: str) -> tuple[dict, str | None]:
    """({styleId: (w:lang value in `slot` or None, basedOn id or None)}, default
    paragraph style id), read from the styles part as the SOURCE had it."""
    info: dict = {}
    default_para = None
    w_style, w_id, w_val = qn("w:style"), qn("w:styleId"), qn("w:val")
    for st in styles.iter(w_style):
        sid = st.get(w_id)
        rpr = st.find(_RPR)
        lang = rpr.find(_W_LANG) if rpr is not None else None
        based = st.find(qn("w:basedOn"))
        info[sid] = (
            lang.get(slot) if lang is not None else None,
            based.get(w_val) if based is not None else None,
        )
        if st.get(qn("w:type")) == "paragraph" and st.get(qn("w:default")) in ("1", "true"):
            default_para = sid
    return info, default_para


def _keep_source_language(
    doc, slot: str, source: str, style_langs, translated: list, ins: int = 0, roots=None
) -> None:
    """Retargeting the document default declares EVERY run without its own w:lang as the
    target language, including content this filter never translates: text boxes,
    tracked insertions, content controls, untranslated paragraphs, headers, footers,
    footnotes and endnotes. Such a run whose language came from the old default gets
    that SOURCE language written on it explicitly (a run whose style names another
    language keeps inheriting it)."""
    info, default_para = style_langs
    w_val = qn("w:val")

    def chain(sid) -> str | None:
        seen = set()
        while sid and sid not in seen and sid in info:
            seen.add(sid)
            lang, sid_next = info[sid]
            if lang:
                return lang
            sid = sid_next
        return None

    def inherited(r) -> str:
        rpr = r.find(_RPR)
        rs = rpr.find(qn("w:rStyle")) if rpr is not None else None
        lang = chain(rs.get(w_val)) if rs is not None else None
        if lang:
            return lang
        p = r.getparent()
        while p is not None and p.tag != _W_P:
            p = p.getparent()
        ps = p.find(qn("w:pPr") + "/" + qn("w:pStyle")) if p is not None else None
        return chain(ps.get(w_val) if ps is not None else default_para) or source

    # The translated paragraphs' w:p proxies stay alive in `translated`, so lxml hands
    # back these same objects from getparent() and id() identifies them.
    written = {id(para._p) for para in translated}

    containers = _containers(ins)

    def is_translated(r) -> bool:
        if ins >= 6:
            para = _w6_paragraph_of_run(r)
            return para is not None and id(para) in written
        parent = r.getparent()
        if parent is not None and (parent.tag == _W_HYPERLINK or parent.tag in containers):
            parent = parent.getparent()
        return parent is not None and id(parent) in written

    def stamp(root, skip_translated: bool) -> bool:
        changed = False
        for r in root.iter(_W_R):
            if skip_translated and is_translated(r):
                continue
            rpr = r.find(_RPR)
            lang = rpr.find(_W_LANG) if rpr is not None else None
            if lang is not None and lang.get(slot) is not None:
                continue
            if inherited(r) == source:
                _set_run_lang(r, slot, source)
                changed = True
        return changed

    from docx.opc.constants import RELATIONSHIP_TYPE as RT
    from docx.oxml.parser import parse_xml
    from lxml import etree

    # Walk 4 translates paragraphs in these parts too (and in the comments part), so a
    # translated paragraph there must not get the source language written back on it.
    kinds = (RT.HEADER, RT.FOOTER, RT.FOOTNOTES, RT.ENDNOTES, *((RT.COMMENTS,) if ins else ()))
    stamp(doc.element.body, True)
    for rel in doc.part.rels.values():
        if rel.is_external or rel.reltype not in kinds:
            continue
        part = rel.target_part
        if hasattr(part, "element"):
            stamp(part.element, True)
        else:  # python-docx keeps footnotes / endnotes as a plain blob part
            known = (roots or {}).get(str(part.partname).lstrip("/"))
            # the root walk 4 already parsed (it carries the translations), else parse here
            try:
                root = known[1] if known else parse_xml(part.blob)  # python-docx's parser
            except (etree.LxmlError, ValueError):
                continue  # not XML (a damaged part): left exactly as it was, never a 500
            if stamp(root, True):
                part._blob = etree.tostring(
                    root, xml_declaration=True, encoding="UTF-8", standalone=True
                )


def _declare_language(
    doc, tag: str, direction: str, translated: list, ins: int = 0, roots=None
) -> None:
    """Declare the TARGET language, touching only parts the package ALREADY has:

    * styles part present: the document default (<w:docDefaults> rPr w:lang) and every
      style that carried the old default, in the slot the target's script uses (val /
      eastAsia / bidi). Absent: no styles part is created; every translated run of a
      translated paragraph gets an explicit w:lang instead.
    * every existing w:lang on a translated run (`_translated_runs`) or on the
      paragraph mark is set to the target; objects and untranslated tracked-change or
      content-control runs keep theirs;
    * core properties present: dc:language. Absent: no core.xml is created (python-docx
      would invent one titled "Word Document", which a screen reader then announces);
    * paragraph direction (<w:bidi/>) on translated paragraphs for an RTL target
      (removed for an LTR target whose source paragraph had it);
    * #722: for a complex-script target (RTL, Thai, Indic, ...) every translated run's
      w:b / w:i / w:sz gets its w:bCs / w:iCs / w:szCs twin when it has none."""
    from docx.opc.constants import RELATIONSHIP_TYPE as RT
    from docx.oxml.parser import OxmlElement

    from app.filters import a11y

    slot = _LANG_SLOT[a11y.script_class(tag)]
    cs_target = _complex_script_target(tag, direction)  # #722
    has_styles = _has_part(doc.part.rels, RT.STYLES)
    has_core = _has_part(doc.part.package.rels, RT.CORE_PROPERTIES)
    old_default = None
    if has_styles:
        styles = doc.styles.element
        style_langs = _style_langs(styles, slot)  # BEFORE any style is retargeted
        dd = styles.find(qn("w:docDefaults"))
        if dd is None:
            dd = OxmlElement("w:docDefaults")
            styles.insert(0, dd)
        rpd = dd.find(qn("w:rPrDefault"))
        if rpd is None:
            rpd = OxmlElement("w:rPrDefault")
            dd.insert(0, rpd)
        rpr = rpd.find(_RPR)
        if rpr is None:
            rpr = OxmlElement("w:rPr")
            rpd.append(rpr)
        lang = rpr.find(_W_LANG)
        if lang is None:
            lang = OxmlElement("w:lang")
            rpr.insert_element_before(lang, "w:eastAsianLayout", "w:specVanish", "w:oMath")
        old_default = lang.get(slot)
        lang.set(slot, tag)
        if old_default:
            for el in styles.iter(_W_LANG):
                if el.get(slot) == old_default:
                    el.set(slot, tag)
    for para in translated:
        runs = _translated_runs(para._p, ins)
        if has_styles:
            ppr = para._p.pPr
            owners = [*runs, *([] if ppr is None else [ppr])]  # pPr: the paragraph mark
            for owner in owners:
                rpr = owner.find(_RPR)
                el = rpr.find(_W_LANG) if rpr is not None else None
                if el is not None and el.get(slot) is not None:
                    el.set(slot, tag)
        else:
            for r in runs:
                _set_run_lang(r, slot, tag)
        if cs_target:
            for r in runs:
                _mirror_cs_props(r)
        ppr = para._p.pPr
        bidi = ppr.find(qn("w:bidi")) if ppr is not None else None
        if direction == "rtl" and bidi is None:
            para._p.get_or_add_pPr().insert_element_before(OxmlElement("w:bidi"), *_BIDI_SUCCESSORS)
        elif direction == "ltr" and bidi is not None:
            ppr.remove(bidi)
    if has_styles and old_default:
        _keep_source_language(doc, slot, old_default, style_langs, translated, ins, roots)
    if has_core:
        doc.core_properties.language = tag


def _paragraph_style_names(doc) -> tuple[dict[str, str | None], str | None]:
    """({styleId: name} of the PARAGRAPH styles, document-default paragraph style
    name or None), in one pass over the styles part.

    Replaces per-id `DocumentPart.get_style()`: python-docx builds that id lookup as
    an XPath with the id inlined, so a `w:styleId` or `w:pStyle` containing a double
    quote raised XPathEvalError and failed the whole import with a 500 (#348). An
    absent styles part is invented the same way `doc.part.get_style()` did."""
    from docx.enum.style import WD_STYLE_TYPE
    from docx.exceptions import InvalidXmlError
    from docx.oxml.exceptions import InvalidXmlError as OxmlInvalidXmlError
    from docx.styles.styles import StyleFactory

    # python-docx raises two UNRELATED InvalidXmlError classes (docx.exceptions for ST_OnOff
    # values, docx.oxml.exceptions for a required attribute that is missing, e.g. <w:name/>).
    _bad = (ValueError, InvalidXmlError, OxmlInvalidXmlError)
    names: dict[str, str | None] = {}
    seen: set[str] = set()
    default_name: str | None = None
    # Direct w:style children only, as python-docx's get_by_id / default_for look (a w:style
    # nested in mc:AlternateContent is not a style of the document).
    for el in doc.styles.element.iterchildren(qn("w:style")):
        # Each attribute is read on its own: a value python-docx cannot convert
        # (w:type="bogus" -> ValueError, w:default="2" -> InvalidXmlError) must not 500
        # the import (#348), and must not hide the style's name from the id lookup.
        try:
            is_para = el.type == WD_STYLE_TYPE.PARAGRAPH
        except _bad:
            is_para = False
        try:
            is_default = el.default
        except _bad:
            is_default = False
        if not is_para:
            sid = el.styleId
            if sid:
                seen.add(sid)  # still owns its id: get_by_id returned the FIRST match
            continue
        try:
            name = StyleFactory(el).name or ""
        except _bad:  # e.g. <w:name/> with no w:val: an unused style must not 500 the import
            name = ""
        sid = el.styleId
        # First element with an id wins, whatever its type (python-docx get_by_id);
        # a paragraph reference to a non-paragraph style falls back to the default.
        if sid and sid not in seen:
            seen.add(sid)
            names[sid] = name
        if is_default:
            default_name = name  # last one wins, as in CT_Styles.default_for
    return names, default_name


def _check_markup_budget(data: bytes) -> None:
    """Refuse a DOCX whose text parts carry far more markup than MAX_DOCUMENT_SEGMENTS
    allows, before python-docx builds its tree (#571 gate).

    The paragraph cap alone let ONE paragraph hold a million runs: a 3.5 MB upload
    (well under the ZIP ratio limit) took three minutes, because every run costs
    several python-docx XPath calls (about 0.2 ms each run). Counted on the raw XML of
    the word/ parts, which zip_guard has already bounded: two runs per allowed segment
    (100000 at the default; real documents carry one or two per paragraph, and the
    paragraph cap's own worst case costs about as much) and forty elements per allowed
    segment (2000000; real documents use about twenty per paragraph plus a fixed 25000
    for styles and settings) for the tree as a whole."""
    import zipfile

    from app import config
    from app.filters.base import InputLimitError

    runs = elements = 0
    with zipfile.ZipFile(BytesIO(data)) as zf:
        for info in zf.infolist():
            name = info.filename
            if not (name.startswith("word/") and name.endswith(".xml")):
                continue
            xml = zf.read(info)
            runs += xml.count(b"<w:r>") + xml.count(b"<w:r ")
            elements += xml.count(b"<")
    if runs > 2 * config.MAX_DOCUMENT_SEGMENTS or elements > 40 * config.MAX_DOCUMENT_SEGMENTS:
        raise InputLimitError(
            "This DOCX has far more formatting runs or markup than MAX_DOCUMENT_SEGMENTS "
            f"allows ({config.MAX_DOCUMENT_SEGMENTS} segments). Split it into smaller files."
        )


@register
class DocxFilter(Filter):
    fmt = "docx"
    # Word template and macro-enabled packages: same walk and stamp (ADR 0027), content
    # type and vbaProject.bin kept as they are (see WORD_ALIAS_TYPES).
    exts = ("docm", "dotx", "dotm")

    takes_comments = True  # #690: the per-project "translate comments" option

    def parse(self, data: bytes, provider: str | None = None, comments: bool = True) -> ParseResult:
        """`comments` (#690): False leaves the comments part's paragraphs (and the alt
        text of drawings inside comments) out of the units. The walk and its numbering are
        unchanged (stamp 5), so the remaining units keep their indices and export leaves
        the comments in the source language. True (the default, and every caller that
        does not pass it) is the pre-#690 behaviour."""
        zip_guard.check(data, "DOCX file")  # #224: refuse a zip bomb before inflating
        from app.filters import ooxml_strict
        from app.filters.docx_resources import external_resource_warnings

        # #669: a Strict Open XML document is read through its Transitional names
        data, _strict = ooxml_strict.to_transitional(data, "DOCX file")

        _check_markup_budget(data)  # #571 gate: before python-docx builds the tree
        doc = DocxDocument(BytesIO(data))
        from app import config
        from app.filters.base import InputLimitError

        # Include empty and nested/table paragraphs before _all_paragraphs expands
        # table wrappers or any paragraph style/tokenization work begins.
        n_main = 0
        for n_main, _ in enumerate(doc.element.iter(qn("w:p")), 1):
            if n_main > config.DOCX_MAX_PARAGRAPHS:
                raise InputLimitError(
                    f"This DOCX exceeds the paragraph limit ({config.DOCX_MAX_PARAGRAPHS}). "
                    "Split it into smaller files."
                )
        units: list[TextUnit] = []
        # Walk 4 also counts the paragraphs of the other text parts against the same cap.
        walk = _v4_walk(doc, config.DOCX_MAX_PARAGRAPHS - n_main)
        paras = walk.paras
        main_name = str(doc.part.partname).lstrip("/")
        # Resolve each style id ONCE per document: python-docx re-derives the
        # default style by walking every w:style element on EACH lookup
        # (~1.7 ms), which made an ordinary 12k-paragraph file take ~20 s to
        # import (#332). A missing w:pStyle or a dangling id falls back to the
        # document default, exactly like get_style() / the per-paragraph
        # para.style used to.
        style_names, default_name = _paragraph_style_names(doc)
        for i, para in enumerate(paras):
            if _has_text(para, DOCX_WALK_VERSION):
                sid = para._p.style
                if sid not in style_names:
                    # "" when the styles part defines no default for this type;
                    # the pre-fix para.style read mapped that to "".
                    style_names[sid] = default_name if default_name is not None else ""
                text, inline_map = _tokenize_paragraph(para, DOCX_WALK_VERSION)
                units.append(
                    TextUnit(p_index=i, text=text, style=style_names[sid], inline_map=inline_map)
                )
        # Storyline "Export to Word" (and any table with a source column and a column
        # titled "Translation"): only the Translation cells are units, everything else
        # (IDs, source text, headings, alt text) stays as exported. Same walk, fewer units.
        from app.filters import docx_bilingual

        bilingual = docx_bilingual.translation_paragraphs(doc.element)
        if bilingual is not None:
            keep = bilingual[0]
            units = [u for u in units if paras[u.p_index]._p in keep]
        part_of = {u.p_index: walk.parts[u.p_index] for u in units}
        body_indices = [i for i, name in part_of.items() if name == main_name]
        # Alt text: appended AFTER every paragraph index (see the section above).
        alts = _alt_units(doc, walk) if bilingual is None else []
        part_of.update({len(paras) + j: a[4] for j, a in enumerate(alts)})
        for j, (_dp, _attr, value, style, _part) in enumerate(alts):
            units.append(TextUnit(p_index=len(paras) + j, text=value, style=style))
        if not comments:
            comment_parts = {name for kind, name, _p in _extra_parts(doc) if kind == "comments"}
            units = [u for u in units if part_of.get(u.p_index, main_name) not in comment_parts]
        skeleton = {
            "format": "docx",
            "docx_walk": DOCX_WALK_VERSION,
            **({} if comments else {"comments": False}),
            "units": [
                {
                    "p_index": u.p_index,
                    "text": u.text,
                    "style": u.style,
                    # the part export writes the unit back to (walk 4; alt text of a drawing
                    # in another part from walk 5)
                    "part": part_of.get(u.p_index, main_name),
                }
                for u in units
            ],
        }
        n_images, missing = _missing_alt_findings(doc, paras, body_indices)
        if n_images:
            skeleton["a11y_images"] = {"images": n_images, "missing_alt": missing}
        warnings = external_resource_warnings(data)
        if bilingual is not None:
            warnings.append(docx_bilingual.notice(bilingual[1]))
        if warnings:
            skeleton["file_warnings"] = warnings
        return ParseResult(skeleton=skeleton, units=units)

    def _rebuild(self, original: bytes, paragraph_texts: dict, walk: int = DOCX_WALK_VERSION):
        zip_guard.check(original, "DOCX file")  # #224: the export re-opens the original
        from app.filters import ooxml_strict
        from app.filters.xml_text import XmlTargetError, xml_target_text

        # #669: Strict in, Strict out (_save maps the rebuilt parts back)
        normalized, strict = ooxml_strict.to_transitional(original, "DOCX file")
        doc = DocxDocument(BytesIO(normalized))
        ins = walk if walk >= 4 else 0  # walk 4: tracked insertions are text; 5: smart tags too
        numbering = _walk(doc, walk)
        paras = numbering.paras
        # from the ORIGINAL, before any paragraph is rewritten; stamp 5 also reads the drawings
        # in the other parts, older stamps the body's only (their numbering never moves)
        alts = _alt_units(doc, numbering if walk >= 5 else None)
        translated = []
        touched: set = set()
        new_id = _revision_ids(doc, numbering) if ins else None
        for key, new_text in paragraph_texts.items():
            i = int(key)
            if new_text is None:
                continue
            new_text = xml_target_text(str(new_text))
            if 0 <= i < len(paras):
                # a Fallback copy of a text box takes the text only when it has the same
                # structure (same tokenized source), read BEFORE either copy is written
                shape = _tokenize_paragraph(paras[i], ins)[0] if i in numbering.mirrors else None
                twins = [
                    m
                    for m in numbering.mirrors.get(i, ())
                    if _tokenize_paragraph(m, ins)[0] == shape
                ]
                # Re-derive the token->rPr map from the ORIGINAL paragraph so the
                # snapshots stay in sync without persisting them through the DB.
                try:
                    self._write(paras[i], new_text, ins, new_id)
                except ValueError as exc:
                    raise XmlTargetError(
                        f"DOCX paragraph {i + 1} could not be written. Review its target text."
                    ) from exc
                translated.append(paras[i])
                if numbering.parts is not None:
                    touched.add(numbering.parts[i])
                for twin in twins:
                    try:
                        self._write(twin, new_text, ins, new_id)
                        translated.append(twin)
                    except ValueError:
                        pass  # the copy keeps its source text; the Choice copy is written
            elif len(paras) <= i < len(paras) + len(alts) and new_text is not None:
                dp, attr, _value, _style, part_name = alts[i - len(paras)]
                try:
                    _set_alt(dp, attr, new_text)
                    touched.add(part_name)
                except ValueError as exc:
                    raise XmlTargetError(
                        f"DOCX alt text unit {i + 1} could not be written. Review its target text."
                    ) from exc
        _flush_blob_parts(numbering, touched)
        return doc, translated, numbering.roots, ((original, normalized) if strict else None)

    @staticmethod
    def _write(para, new_text: str, ins: int, new_id=None) -> None:
        layout = _object_layout(para._p, ins)
        if layout is not None:
            _set_paragraph_objects(para, new_text, layout, new_id)
        else:
            _, inline_map = _tokenize_paragraph(para, ins)
            _set_paragraph_text(para, new_text, inline_map)

    @staticmethod
    def _save(doc, strict) -> bytes:
        """The rebuilt package; a Strict original (#669) gets its Strict names back."""
        out = BytesIO()
        doc.save(out)
        if strict is None:
            return out.getvalue()
        from app.filters import ooxml_strict

        return ooxml_strict.to_strict(strict[0], strict[1], out.getvalue())

    def build(self, original: bytes, paragraph_texts: dict, walk: int = DOCX_WALK_VERSION) -> bytes:
        doc, _, _, strict = self._rebuild(original, paragraph_texts, walk)
        return self._save(doc, strict)

    def build_localized(
        self,
        original: bytes,
        paragraph_texts: dict,
        target_locale: str | None = None,
        walk: int = DOCX_WALK_VERSION,
    ) -> bytes:
        from app.filters import a11y

        doc, translated, roots, strict = self._rebuild(original, paragraph_texts, walk)
        tag = a11y.target_tag(target_locale)
        if tag:
            _declare_language(
                doc,
                tag,
                a11y.target_direction(target_locale),
                translated,
                walk if walk >= 4 else 0,
                roots,
            )
        return self._save(doc, strict)
