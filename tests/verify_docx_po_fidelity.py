"""DOCX non-text objects and PO header fidelity (output-a11y gate finding, 2026-09-25).

Data loss in customer deliverables, pre-existing on main:

  A. DOCX: translating a paragraph that held text AND a non-text object (a picture, a
     floating image, a text box, a chart, a field, a footnote reference, a hyperlink
     around a picture) deleted the object on export, because `Run.text = ...` clears
     every child of a run. Object paragraphs now carry point tokens (x kind) that put
     the ORIGINAL elements back where the translator left them.
  B. PO: a header entry (msgid "") with no comment lines above it was dropped on
     rebuild (charset, Plural-Forms and Language lost), and a trailing obsolete `#~`
     block gained a duplicate empty header.

Pinned here:
  1. one fixture per object kind (picture before / after / between words, floating
     anchor, text box with its own paragraphs, hyperlink with a picture, complex PAGE
     field, simple field, footnote reference, chart, page break, symbol, bookmark):
     every original object survives (counted by element type), text is translated, the
     object sits where it was relative to the text, the package opens (python-docx),
     [Content_Types] + rels + media parts are intact;
  2. a lost token keeps the object (next to the text it followed) and raises the QA
     placeholder flag; a duplicated token places the object once;
  3. re-export of a translation stored BEFORE this fix (no x tokens) keeps every object;
  4. alt text write-back now reaches a picture that shares a paragraph with text, and
     the target-language declaration still lands;
  5. paragraphs without objects keep their historical tokenization (byte-identical);
  6. end to end through upload -> save -> export;
  7. PO header with and without comments, obsolete blocks, Language update.

Added by docx-fidelity2 (2026-09-26):
  8. a paragraph whose text sits in a w:hyperlink keeps the link (it was deleted on
     export): external URL, internal anchor, text + link, two links, table cell,
     header/footer; r:id resolves, run style kept, python-docx sees the address;
  9. x tokens reordered around a complex field never invert begin/separate/end;
 9b. a field inside a hyperlink (TOC entry) stays inside ONE hyperlink, and a stray
     space between tokens never splits a link (gate round 1);
 10. the target language is declared on translated runs only; untranslated content
     that inherited the old default gets the source language written on it (10b);
 11. r tokens are numbered per original run (legacy formatting maps);
 12. malformed PO blocks never produce a second msgid "" header;
 13. #332 ratchet: parsing an ordinary 12,000-paragraph manual makes at most one
     style lookup per distinct style id (deterministic; pre-fix it made one or two
     per paragraph), with a loose 15 s wall-clock backstop;
 14. a DOCX whose styles part defines no default paragraph style (and a dangling
     w:pStyle, which falls through to it) parses with style "" instead of crashing;
 15. #332 fix round 1: exact per-paragraph style names (Heading 1, a Title inside a
     table cell, a custom style, a dangling id, a repeated id) survive the style cache;
 16. #348: a w:pStyle (defined or dangling) whose id contains a double quote used to
     make python-docx build an invalid XPath and crash the import (XPathEvalError,
     500); it now parses, and every other style id still resolves to its own name;
 17. #348 self-review: a styles part whose w:type or w:default carries a value
     python-docx cannot convert (ValueError / docx.exceptions.InvalidXmlError),
     plus a default style with no w:name, parses; a dangling w:pStyle falling
     back to the nameless default gets style "", not None, and every other
     style id still resolves to its own name.

 16b. #349: a 2,000-row vertically merged column parses with work proportional to
     the table (cell and paragraph visits counted; a loose wall-clock backstop well
     under the 30 s watchdog); pre-fix it took minutes then raised RecursionError;
 17b. the merged cell's text is yielded exactly once (not once per spanned row);
 18. export (build and build_localized) writes the translation into the merged cell
     and keeps the vMerge structure and a valid package;
 19. gridSpan cells are visited once; plain tables, nested tables, hyperlinks and
     multi-run cells keep the exact paragraph order of the pre-#349 walker;
 20-23. #349 walker version stamp: legacy skeletons export with the old numbering,
     new imports are stamped, re-ingest restamps, an unknown stamp refuses;
 24. #348: hostile style ids (quote, apostrophe, backslash, bracket, 50k chars,
     unicode, injection) parse in body and table cells under both walks; the first
     w:style owning an id wins; a pStyle on a non-paragraph style, a dangling id, a
     style nested in mc:AlternateContent all fall back to the default; an unused
     style whose w:name has no value does not 500 the import.
 25-29. #355: nested tables and w:sdt / w:customXml wrappers (rows, cells, tables, body
     paragraphs, stacked, inside nested tables, with vMerge) are extracted and written
     back; walk stamp 3; a skeleton stored under stamp 2 exports on the stamp-2 walk
     unchanged; hostile / malformed wrappers never raise; nesting bound and linear walk.
     Gate: nested tables are walked depth-first in reading order; 30 an unknown stamp
     exports as a 409 that says to re-import.
 32-38. #270 r1-files-06 (walk stamp 4): headers, footers, footnotes, endnotes and comments
     are units after the main body in a documented order, each recording its part;
     text boxes inline after their anchor (the Choice and Fallback copies extracted once,
     both written); tracked insertions are text and deletions untouched; stamp 3 and 2
     export unchanged; hostile parts never raise; the untranslated-regions warning names
     only what stamp 4 still skips; end to end through upload and export.
 39-42. #368 (walk stamp 5): alt text in headers, footers and notes; smartTag text; fldSimple
     results and nested smart tags named by the warning; data-bound content controls warned;
     the repair re-ingest recomputes file_warnings and a11y_images.
 43. #686 (walk stamp 6): text in nested smart tags and other nested inline wrappers
     (tracked insertion, hyperlink, custom XML, w:dir, text content control) is translated
     inside the same wrapper chain; a stamp-5 project exports byte-identically to before.
 44. #686 gate: a field from a hyperlink into a smart tag inside it stays in one hyperlink;
     a re-created w:bdo / w:dir follows the direction of its translated text.

ASCII output only.
"""

import io
import os
import re
import sys
import time
import zipfile

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(_ROOT)
sys.path.insert(0, _ROOT)
from tests._bootstrap import init_env

init_env("loctms_docxpo_")

from fastapi.testclient import TestClient
from lxml import etree
from sqlmodel import Session, select

from app.db import engine, init_db
from app.filters import get_filter
from app.main import app
from app.models import Segment
from app.services import segment as seg
from app.services.quality_common import tag_damage

NS = {
    "w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main",
    "wp": "http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing",
    "a": "http://schemas.openxmlformats.org/drawingml/2006/main",
    "pic": "http://schemas.openxmlformats.org/drawingml/2006/picture",
    "c": "http://schemas.openxmlformats.org/drawingml/2006/chart",
    "r": "http://schemas.openxmlformats.org/officeDocument/2006/relationships",
    "mc": "http://schemas.openxmlformats.org/markup-compatibility/2006",
    "wps": "http://schemas.microsoft.com/office/word/2010/wordprocessingShape",
    "v": "urn:schemas-microsoft-com:vml",
}


def W(prefix: str, local: str) -> str:
    return "{" + NS[prefix] + "}" + local


_NSDECL = " ".join(f'xmlns:{k}="{v}"' for k, v in NS.items())

# Element kinds counted in word/document.xml before and after export.
OBJECT_XPATHS = {
    "drawing": "//w:drawing",
    "inline": "//wp:inline",
    "anchor": "//wp:anchor",
    "pic": "//pic:pic",
    "blip": "//a:blip",
    "chart": "//c:chart",
    "alternateContent": "//mc:AlternateContent",
    "pict": "//w:pict",
    "txbxContent": "//w:txbxContent",
    "txbx_paragraphs": "//w:txbxContent/w:p",
    "hyperlink": "//w:hyperlink",
    "hyperlink_pic": "//w:hyperlink//w:drawing",
    "fldChar": "//w:fldChar",
    "instrText": "//w:instrText",
    "fldSimple": "//w:fldSimple",
    "footnoteReference": "//w:footnoteReference",
    "page_break": "//w:br[@w:type='page']",
    "sym": "//w:sym",
    "bookmarkStart": "//w:bookmarkStart",
    "bookmarkEnd": "//w:bookmarkEnd",
    "docPr": "//wp:docPr",
}


def _png() -> bytes:
    from PIL import Image

    b = io.BytesIO()
    Image.new("RGB", (4, 4), "blue").save(b, "PNG")
    return b.getvalue()


def _el(xml: str):
    from docx.oxml import parse_xml

    return parse_xml(xml)


def _anchor_from_inline(inline_xml: str) -> str:
    """wp:inline -> wp:anchor (floating, square wrap) keeping extent/docPr/graphic."""
    inner = inline_xml.split(">", 1)[1].rsplit("</wp:inline>", 1)[0]
    extent = re.search(r"<wp:extent[^>]*/>", inner).group(0)
    rest = inner.replace(extent, "", 1)
    return (
        f'<wp:anchor {_NSDECL} distT="0" distB="0" distL="114300" distR="114300" '
        'simplePos="0" relativeHeight="251659264" behindDoc="0" locked="0" '
        'layoutInCell="1" allowOverlap="1">'
        '<wp:simplePos x="0" y="0"/>'
        '<wp:positionH relativeFrom="column"><wp:posOffset>0</wp:posOffset></wp:positionH>'
        '<wp:positionV relativeFrom="paragraph"><wp:posOffset>0</wp:posOffset></wp:positionV>'
        f'{extent}<wp:wrapSquare wrapText="bothSides"/>{rest}</wp:anchor>'
    )


def _textbox_run():
    paras = (
        "<w:p><w:r><w:t>Text box line one.</w:t></w:r></w:p>"
        "<w:p><w:r><w:t>Text box line two.</w:t></w:r></w:p>"
    )
    return _el(
        f"<w:r {_NSDECL}><mc:AlternateContent>"
        '<mc:Choice Requires="wps"><w:drawing>'
        '<wp:anchor distT="0" distB="0" distL="114300" distR="114300" simplePos="0" '
        'relativeHeight="251660288" behindDoc="0" locked="0" layoutInCell="1" allowOverlap="1">'
        '<wp:simplePos x="0" y="0"/>'
        '<wp:positionH relativeFrom="column"><wp:posOffset>0</wp:posOffset></wp:positionH>'
        '<wp:positionV relativeFrom="paragraph"><wp:posOffset>0</wp:posOffset></wp:positionV>'
        '<wp:extent cx="1828800" cy="457200"/><wp:wrapSquare wrapText="bothSides"/>'
        '<wp:docPr id="900" name="Text Box 900"/>'
        "<a:graphic><a:graphicData "
        'uri="http://schemas.microsoft.com/office/word/2010/wordprocessingShape">'
        '<wps:wsp><wps:cNvSpPr txBox="1"/><wps:spPr><a:xfrm><a:off x="0" y="0"/>'
        '<a:ext cx="1828800" cy="457200"/></a:xfrm><a:prstGeom prst="rect"><a:avLst/></a:prstGeom>'
        f"</wps:spPr><wps:txbx><w:txbxContent>{paras}"
        "</w:txbxContent></wps:txbx><wps:bodyPr/></wps:wsp>"
        "</a:graphicData></a:graphic></wp:anchor></w:drawing></mc:Choice>"
        '<mc:Fallback><w:pict><v:shape id="tb900" style="width:144pt;height:36pt">'
        f"<v:textbox><w:txbxContent>{paras}</w:txbxContent></v:textbox>"
        "</v:shape></w:pict></mc:Fallback></mc:AlternateContent></w:r>"
    )


def _chart_run(rid: str):
    return _el(
        f'<w:r {_NSDECL}><w:drawing><wp:inline distT="0" distB="0" distL="0" distR="0">'
        '<wp:extent cx="3200400" cy="1828800"/><wp:docPr id="901" name="Chart 901" '
        'descr="Quarterly sales chart"/>'
        '<a:graphic><a:graphicData uri="http://schemas.openxmlformats.org/drawingml/2006/chart">'
        f'<c:chart r:id="{rid}"/></a:graphicData></a:graphic></wp:inline></w:drawing></w:r>'
    )


CHART_XML = (
    b'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
    b'<c:chartSpace xmlns:c="http://schemas.openxmlformats.org/drawingml/2006/chart" '
    b'xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main">'
    b'<c:chart><c:autoTitleDeleted val="1"/><c:plotArea><c:layout/><c:barChart>'
    b'<c:barDir val="col"/><c:grouping val="clustered"/><c:varyColors val="0"/>'
    b'<c:ser><c:idx val="0"/><c:order val="0"/><c:val><c:numLit><c:formatCode>General'
    b'</c:formatCode><c:ptCount val="2"/><c:pt idx="0"><c:v>1</c:v></c:pt><c:pt idx="1">'
    b'<c:v>2</c:v></c:pt></c:numLit></c:val></c:ser><c:axId val="1"/><c:axId val="2"/>'
    b'</c:barChart><c:catAx><c:axId val="1"/><c:scaling><c:orientation val="minMax"/>'
    b'</c:scaling><c:delete val="0"/><c:axPos val="b"/><c:crossAx val="2"/></c:catAx>'
    b'<c:valAx><c:axId val="2"/><c:scaling><c:orientation val="minMax"/></c:scaling>'
    b'<c:delete val="0"/><c:axPos val="l"/><c:crossAx val="1"/></c:valAx></c:plotArea>'
    b'<c:plotVisOnly val="1"/></c:chart></c:chartSpace>'
)
FOOTNOTES_XML = (
    b'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
    b'<w:footnotes xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
    b'<w:footnote w:type="separator" w:id="-1"><w:p><w:r><w:separator/></w:r></w:p></w:footnote>'
    b'<w:footnote w:type="continuationSeparator" w:id="0"><w:p><w:r><w:continuationSeparator/>'
    b"</w:r></w:p></w:footnote>"
    b'<w:footnote w:id="1"><w:p><w:r><w:t>The footnote text.</w:t></w:r></w:p></w:footnote>'
    b"</w:footnotes>"
)

# (key, fixture paragraph builder) - built in this order; KEYS index the paragraphs.
KEYS = [
    "before",
    "after",
    "between",
    "anchor",
    "hyperlink_pic",
    "field_page",
    "field_simple",
    "footnote",
    "chart",
    "page_break",
    "symbol",
    "bookmark_pic",
    "plain",
    "multirun",
    # LAST: walk 4 numbers a text box's own paragraphs right after its anchor, so the
    # anchor sits at the end and the other fixtures keep p_index == KEYS index.
    "textbox",
]


def build_fixture() -> bytes:
    from docx import Document as Docx
    from docx.opc.constants import RELATIONSHIP_TYPE as RT
    from docx.opc.packuri import PackURI
    from docx.opc.part import Part

    png = _png()
    d = Docx()
    body_rid = {}

    # before: [picture][text]
    p = d.add_paragraph()
    p.add_run().add_picture(io.BytesIO(png))
    p.add_run("After the picture comes this sentence.")
    # after: [text][picture]; alt text on this one (write-back must reach it)
    p = d.add_paragraph("The picture follows this sentence.")
    p.add_run().add_picture(io.BytesIO(png))
    # between: [text][picture][bold text]
    p = d.add_paragraph("Left words ")
    p.add_run().add_picture(io.BytesIO(png))
    p.add_run(" right words.").bold = True
    # anchor: a floating picture ahead of the text
    p = d.add_paragraph()
    r = p.add_run()
    r.add_picture(io.BytesIO(png))
    inline = r._r.find(".//" + W("wp", "inline"))
    drawing = inline.getparent()
    drawing.replace(inline, _el(_anchor_from_inline(etree.tostring(inline).decode())))
    p.add_run("A floating picture sits beside this text.")
    # hyperlink with a picture: "See " + <hyperlink>[our site][picture]</hyperlink> + " now."
    rid = d.part.relate_to("https://example.com/", RT.HYPERLINK, is_external=True)
    body_rid["hyperlink"] = rid
    p = d.add_paragraph("See ")
    tmp = d.add_paragraph()
    tmp.add_run("our site")
    tmp.add_run().add_picture(io.BytesIO(png))
    hl = _el(f'<w:hyperlink {_NSDECL} r:id="{rid}" w:history="1"/>')
    for run_el in list(tmp._p.iterchildren(W("w", "r"))):
        hl.append(run_el)
    tmp._p.getparent().remove(tmp._p)
    p._p.append(hl)
    p.add_run(" for details.")
    # complex PAGE field: "Page " [begin][instr][separate]["1"][end] " of the report."
    p = d.add_paragraph("Page ")
    for frag in (
        '<w:r><w:fldChar w:fldCharType="begin"/></w:r>',
        '<w:r><w:instrText xml:space="preserve"> PAGE </w:instrText></w:r>',
        '<w:r><w:fldChar w:fldCharType="separate"/></w:r>',
        "<w:r><w:t>1</w:t></w:r>",
        '<w:r><w:fldChar w:fldCharType="end"/></w:r>',
    ):
        p._p.append(_el(frag.replace("<w:r>", f"<w:r {_NSDECL}>", 1)))
    p.add_run(" of the report.")
    # simple field
    p = d.add_paragraph("Printed on ")
    p._p.append(
        _el(
            f'<w:fldSimple {_NSDECL} w:instr=" DATE "><w:r><w:t>9/25/2026</w:t></w:r></w:fldSimple>'
        )
    )
    p.add_run(" by the team.")
    # footnote reference
    p = d.add_paragraph("A sentence with a footnote.")
    p._p.append(
        _el(
            f'<w:r {_NSDECL}><w:rPr><w:vertAlign w:val="superscript"/></w:rPr>'
            '<w:footnoteReference w:id="1"/></w:r>'
        )
    )
    p.add_run(" And more text.")
    # chart
    chart = Part(
        PackURI("/word/charts/chart1.xml"),
        "application/vnd.openxmlformats-officedocument.drawingml.chart+xml",
        CHART_XML,
        d.part.package,
    )
    crid = d.part.relate_to(chart, RT.CHART)
    p = d.add_paragraph("The chart shows sales.")
    p._p.append(_chart_run(crid))
    # page break inside a text run
    p = d.add_paragraph("Before the break.")
    p.runs[0]._r.append(_el(f'<w:br {_NSDECL} w:type="page"/>'))
    p.add_run("After the break.")
    # symbol
    p = d.add_paragraph("Checked ")
    p._p.append(_el(f'<w:r {_NSDECL}><w:sym w:font="Wingdings" w:char="F0FC"/></w:r>'))
    p.add_run(" item.")
    # bookmark around text + a picture
    p = d.add_paragraph()
    p._p.append(_el(f'<w:bookmarkStart {_NSDECL} w:id="7" w:name="intro"/>'))
    p.add_run("Bookmarked intro text.")
    p._p.append(_el(f'<w:bookmarkEnd {_NSDECL} w:id="7"/>'))
    p.add_run().add_picture(io.BytesIO(png))
    # no objects: must keep the historical tokenization
    d.add_paragraph("A plain paragraph with no objects.")
    p = d.add_paragraph("Normal then ")
    p.add_run("bold").bold = True
    p.add_run(" then normal.")
    # textbox (last, see KEYS)
    p = d.add_paragraph("A text box is anchored here.")
    p._p.append(_textbox_run())

    fn = Part(
        PackURI("/word/footnotes.xml"),
        "application/vnd.openxmlformats-officedocument.wordprocessingml.footnotes+xml",
        FOOTNOTES_XML,
        d.part.package,
    )
    d.part.relate_to(fn, RT.FOOTNOTES)
    dps = list(d.element.body.iter(W("wp", "docPr")))
    dps[1].set("descr", "A blue square after text")
    buf = io.BytesIO()
    d.save(buf)
    return buf.getvalue()


def _doc_xml(data: bytes):
    return etree.fromstring(zipfile.ZipFile(io.BytesIO(data)).read("word/document.xml"))


def _body_paras(root) -> list:
    return root.findall("w:body/w:p", NS)


def _counts(el) -> dict:
    return {
        k: len(el.xpath("." + x if x.startswith("//") else x, namespaces=NS))
        for k, x in OBJECT_XPATHS.items()
    }


def _para_text(p) -> str:
    """python-docx's own text of a (plain lxml) w:p or w:hyperlink."""
    from docx.oxml import parse_xml
    from docx.text.paragraph import Paragraph

    el = parse_xml(etree.tostring(p))
    return Paragraph(el, None).text if etree.QName(el).localname == "p" else el.text


def _translate(text: str) -> str:
    """Deterministic 'translation': upper-case every text piece, keep every token."""
    out = []
    pos = 0
    for m in seg._PLACEHOLDER_RE.finditer(text):
        out.append(text[pos : m.start()].upper())
        out.append(m.group(0))
        pos = m.end()
    out.append(text[pos:].upper())
    return "".join(out)


def _package_intact(src: bytes, out: bytes) -> None:
    from docx import Document as Docx

    zs, zo = zipfile.ZipFile(io.BytesIO(src)), zipfile.ZipFile(io.BytesIO(out))
    assert sorted(zs.namelist()) == sorted(zo.namelist()), (zs.namelist(), zo.namelist())
    assert zs.read("[Content_Types].xml") == zo.read("[Content_Types].xml")
    for n in zs.namelist():
        if n.endswith(".rels") or n.startswith("word/media/") or n.startswith("word/charts/"):
            assert zs.read(n) == zo.read(n), n
        if n.endswith(".xml") or n.endswith(".rels"):
            etree.fromstring(zo.read(n))
    Docx(io.BytesIO(out))  # opens


def _child_kinds(p) -> list[str]:
    """Paragraph content as a compact kind sequence: T (text run), O (object), H( ... )."""
    out = []
    for ch in p:
        tag = etree.QName(ch).localname
        if tag == "pPr":
            continue
        if tag == "hyperlink":
            out.append("H(" + "".join(_child_kinds(ch)) + ")")
        elif tag == "r":
            for rc in ch:
                t = etree.QName(rc).localname
                if t in ("t", "tab"):
                    if not out or out[-1] != "T":
                        out.append("T")
                elif t != "rPr":
                    out.append(t)
        else:
            out.append(tag)
    return out


def test_fixtures():
    f = get_filter("docx")
    src = build_fixture()
    units = f.parse(src).units
    # walk 4 also extracts the text box's paragraphs and the footnote part (after the
    # anchor paragraphs); the fidelity checks below look at the paragraphs KEYS names
    body = {
        u.p_index: u for u in units if not u.style.startswith("a11y:") and u.p_index < len(KEYS)
    }
    assert sorted(body) == list(range(len(KEYS))), sorted(body)
    by_key = {k: body[i] for i, k in enumerate(KEYS)}

    # 5. no-object paragraphs: historical tokenization, byte for byte
    assert by_key["plain"].text == "A plain paragraph with no objects."
    assert by_key["multirun"].text == "⟦r1⟧Normal then ⟦/r1⟧⟦r2⟧bold⟦/r2⟧⟦r3⟧ then normal.⟦/r3⟧"
    # object paragraphs carry point tokens; r numbering matches the old content-run order
    assert by_key["before"].text == "⟦x1⟧After the picture comes this sentence.", by_key[
        "before"
    ].text
    assert by_key["after"].text == "The picture follows this sentence.⟦x1⟧"
    assert by_key["between"].text == "⟦r1⟧Left words ⟦/r1⟧⟦x3⟧⟦r2⟧ right words.⟦/r2⟧"
    assert by_key["field_page"].text == (
        "⟦r1⟧Page ⟦/r1⟧⟦x4⟧⟦r2⟧1⟦/r2⟧⟦x5⟧⟦r3⟧ of the report.⟦/r3⟧"
    ), by_key["field_page"].text
    assert by_key["hyperlink_pic"].text == (
        "⟦r1⟧See ⟦/r1⟧⟦r2⟧our site⟦/r2⟧⟦x4⟧⟦r3⟧ for details.⟦/r3⟧"
    ), by_key["hyperlink_pic"].text
    assert by_key["bookmark_pic"].text == "Bookmarked intro text.⟦x1⟧"
    for u in body.values():
        assert seg.placeholders_well_formed(u.text), u.text

    src_root = _doc_xml(src)
    src_paras = _body_paras(src_root)
    mapping = {str(i): _translate(u.text) for i, u in body.items()}
    alt_units = [u for u in units if u.style == "a11y:img-alt"]
    assert [u.text for u in alt_units] == ["A blue square after text", "Quarterly sales chart"]
    for u in alt_units:
        mapping[str(u.p_index)] = "ES " + u.text
    out = f.build_localized(src, mapping, "es-ES")
    _package_intact(src, out)
    out_root = _doc_xml(out)
    out_paras = _body_paras(out_root)
    assert len(out_paras) == len(src_paras)

    print("  1)  per-fixture object counts (before -> after), text translated, position kept")
    for i, key in enumerate(KEYS):
        before, after = _counts(src_paras[i]), _counts(out_paras[i])
        nz = {k: (v, after[k]) for k, v in before.items() if v or after[k]}
        assert before == after, (key, nz)
        want = _translate(seg.strip_placeholders(body[i].text))
        assert _para_text(out_paras[i]) == want, (key, _para_text(out_paras[i]), want)
        print(f"      {key:14s} " + ", ".join(f"{k} {a}->{b}" for k, (a, b) in nz.items()))
    assert _counts(src_root) == _counts(out_root)

    kinds = {k: _child_kinds(out_paras[i]) for i, k in enumerate(KEYS)}
    assert kinds["before"] == ["drawing", "T"], kinds["before"]
    assert kinds["after"] == ["T", "drawing"], kinds["after"]
    assert kinds["between"] == ["T", "drawing", "T"], kinds["between"]
    assert kinds["anchor"] == ["drawing", "T"], kinds["anchor"]
    assert kinds["textbox"] == ["T", "AlternateContent"], kinds["textbox"]
    assert kinds["hyperlink_pic"] == ["T", "H(Tdrawing)", "T"], kinds["hyperlink_pic"]
    assert kinds["field_page"] == ["T", "fldChar", "instrText", "fldChar", "T", "fldChar", "T"]
    assert kinds["field_simple"] == ["T", "fldSimple", "T"], kinds["field_simple"]
    assert kinds["footnote"] == ["T", "footnoteReference", "T"], kinds["footnote"]
    assert kinds["chart"] == ["T", "drawing"], kinds["chart"]
    assert kinds["page_break"] == ["T", "br", "T"], kinds["page_break"]
    assert kinds["symbol"] == ["T", "sym", "T"], kinds["symbol"]
    assert kinds["bookmark_pic"] == ["bookmarkStart", "T", "bookmarkEnd", "drawing"]
    # formatting carried: the bold run is still bold, the footnote mark still superscript
    between = out_paras[KEYS.index("between")]
    assert between.xpath(".//w:r[w:rPr/w:b]/w:t/text()", namespaces=NS) == [" RIGHT WORDS."]
    fn = out_paras[KEYS.index("footnote")]
    assert fn.xpath(".//w:r[w:footnoteReference]/w:rPr/w:vertAlign/@w:val", namespaces=NS) == [
        "superscript"
    ]
    hl = out_paras[KEYS.index("hyperlink_pic")].find("w:hyperlink", NS)
    assert hl.get(W("r", "id")) and hl.get(W("w", "history")) == "1"
    assert _para_text(hl) == "OUR SITE"
    # the text box's own paragraphs are inside the object and untouched
    assert out_root.xpath("//w:txbxContent/w:p//w:t/text()", namespaces=NS)[:2] == [
        "Text box line one.",
        "Text box line two.",
    ]
    # 4. alt write-back reaches a picture in a TEXT paragraph; the language still lands
    descrs = out_root.xpath("//wp:docPr/@descr", namespaces=NS)
    assert "ES A blue square after text" in descrs and "ES Quarterly sales chart" in descrs
    styles = zipfile.ZipFile(io.BytesIO(out)).read("word/styles.xml").decode()
    assert re.search(r'<w:lang [^>]*w:val="es-ES"', styles), "docDefaults lang"
    # reparse: the exported file tokenizes to the same token shapes
    again = {u.p_index: u.text for u in f.parse(out).units if not u.style.startswith("a11y:")}
    for i, u in body.items():
        assert seg.find_placeholders(again[i]) == seg.find_placeholders(u.text), (i, again[i])
    print("  4)  alt write-back in a text paragraph + target language declared")
    return src, body


def test_lost_and_duplicated_tokens(src: bytes, body: dict):
    f = get_filter("docx")
    src_paras = _body_paras(_doc_xml(src))
    # every x token dropped: objects kept, next to the text they followed
    mapping = {str(i): _translate(re.sub(r"⟦x\d+⟧", "", u.text)) for i, u in body.items()}
    out = f.build(src, mapping)
    _package_intact(src, out)
    out_paras = _body_paras(_doc_xml(out))
    for i, key in enumerate(KEYS):
        assert _counts(src_paras[i]) == _counts(out_paras[i]), key
    kinds = {k: _child_kinds(out_paras[i]) for i, k in enumerate(KEYS)}
    assert kinds["before"] == ["drawing", "T"], kinds["before"]
    assert kinds["between"] == ["T", "drawing", "T"], kinds["between"]
    assert kinds["field_page"] == ["T", "fldChar", "instrText", "fldChar", "T", "fldChar", "T"]
    assert kinds["footnote"] == ["T", "footnoteReference", "T"], kinds["footnote"]
    # the QA placeholder flag names the dropped token
    u = body[KEYS.index("between")]
    reason = tag_damage(u.text, re.sub(r"⟦x\d+⟧", "", u.text))
    assert "dropped" in reason and "⟦x3⟧" in reason, reason
    # a token moved to the end: object moves with it; duplicated: placed once
    i = KEYS.index("before")
    moved = f.build(src, {str(i): "TEXT FIRST.⟦x1⟧"})
    assert _child_kinds(_body_paras(_doc_xml(moved))[i]) == ["T", "drawing"]
    dup = f.build(src, {str(i): "⟦x1⟧A ⟦x1⟧B ⟦x1⟧"})
    dp = _body_paras(_doc_xml(dup))[i]
    assert _counts(dp) == _counts(src_paras[i]) and _child_kinds(dp) == ["drawing", "T"]
    assert _para_text(dp) == "A B "
    # a reorder that would put a bookmark's end before its start is repaired
    b = KEYS.index("bookmark_pic")
    bm = _body_paras(_doc_xml(f.build(src, {str(b): "⟦x1⟧MOVED TEXT."})))[b]
    assert _child_kinds(bm) == ["drawing", "bookmarkStart", "bookmarkEnd", "T"], _child_kinds(bm)
    # garbage / out-of-range tokens never delete anything
    junk = f.build(src, {str(i): "⟦x9⟧⟦r7⟧junk⟦/r7⟧"})
    jp = _body_paras(_doc_xml(junk))[i]
    assert _counts(jp) == _counts(src_paras[i]) and _para_text(jp) == "junk"
    print("  2)  lost token keeps the object in place + QA flag; duplicate placed once")


def test_legacy_translation(src: bytes, body: dict):
    """A document ingested before this fix stored units WITHOUT x tokens (and, for a
    hyperlink paragraph, without any token). Re-export must keep every object."""
    f = get_filter("docx")
    src_paras = _body_paras(_doc_xml(src))
    legacy = {}
    for i, u in body.items():
        t = re.sub(r"⟦x\d+⟧", "", u.text)
        if KEYS[i] in ("hyperlink_pic", "field_simple", "textbox", "chart", "footnote", "symbol"):
            t = seg.strip_placeholders(t)  # the old parse left these untokenized / single-run
        legacy[str(i)] = _translate(t)
    out = f.build(src, legacy)
    _package_intact(src, out)
    out_paras = _body_paras(_doc_xml(out))
    for i, key in enumerate(KEYS):
        assert _counts(src_paras[i]) == _counts(out_paras[i]), key
        assert _para_text(out_paras[i]).replace("OUR SITE", "") == _translate(
            seg.strip_placeholders(body[i].text)
        ).replace("OUR SITE", ""), (key, _para_text(out_paras[i]))
    print("  3)  legacy (pre-fix) translations re-export with every object kept")


def test_end_to_end():
    client = TestClient(app)
    r = client.post(
        "/projects",
        data={
            "name": "DOCX objects",
            "source_locale": "en-US",
            "target_locales": "es-ES",
            "default_provider": "claude",
        },
        follow_redirects=False,
    )
    assert r.status_code == 303, r.text
    pid = int(r.headers["location"].rsplit("/", 1)[-1])
    src = build_fixture()
    r = client.post(
        f"/projects/{pid}/documents",
        data={"mode": "segmented"},
        files={
            "file": (
                "objects.docx",
                src,
                "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            )
        },
        follow_redirects=False,
    )
    assert r.status_code == 303, r.text
    doc_id = int(r.headers["location"].rsplit("/", 1)[-1])
    with Session(engine) as s:
        segs = s.exec(
            select(Segment).where(Segment.document_id == doc_id, Segment.target_locale == "es-ES")
        ).all()
        rows = [(x.id, x.source_text) for x in segs]
    assert any("⟦x" in t for _i, t in rows), rows
    for sid, text in rows:
        r = client.post(
            f"/segments/{sid}/save", data={"target_text": _translate(text) + "\x01", "tte_ms": "0"}
        )
        assert r.status_code == 200, r.text
    resp = client.get(f"/documents/{doc_id}/export?locale=es-ES")
    assert resp.status_code == 200, resp.text[:300]
    _package_intact(src, resp.content)
    assert _counts(_doc_xml(src)) == _counts(_doc_xml(resp.content))
    texts = [_para_text(p) for p in _body_paras(_doc_xml(resp.content))]
    assert "AFTER THE PICTURE COMES THIS SENTENCE." in texts, texts
    from unittest.mock import patch

    with patch("app.filters.docx_filter._set_paragraph_objects", side_effect=ValueError("bad XML")):
        refused = client.get(f"/documents/{doc_id}/export?locale=es-ES")
    assert 400 <= refused.status_code < 500, refused.text
    assert "DOCX paragraph" in refused.text, refused.text
    print("  6)  end to end: upload -> save -> export keeps every object")


# ----------------------------------------------------- docx-fidelity2 (2026-09-26)
_HL_STYLE = '<w:rPr><w:rStyle w:val="Hyperlink"/></w:rPr>'


def _hl(target: str, text: str) -> str:
    """A w:hyperlink: `target` is `r:id="..."` or `w:anchor="..."`."""
    return f'<w:hyperlink {_NSDECL} {target}><w:r>{_HL_STYLE}<w:t xml:space="preserve">{text}</w:t></w:r></w:hyperlink>'  # noqa: E501


def _rels(data: bytes, part: str) -> dict:
    """{rId: (target, target mode)} of one part's relationships."""
    root = etree.fromstring(zipfile.ZipFile(io.BytesIO(data)).read(part))
    return {r.get("Id"): (r.get("Target"), r.get("TargetMode")) for r in root}


def build_hyperlink_fixture() -> bytes:
    """Hyperlink paragraphs: link only (external URL), link only (internal bookmark
    anchor), text + link + text, two links, a link-only paragraph in a table cell, and
    links in the header and footer (not translated by this filter; they must survive)."""
    from docx import Document as Docx
    from docx.opc.constants import RELATIONSHIP_TYPE as RT

    d = Docx()
    ext = d.part.relate_to("https://example.com/", RT.HYPERLINK, is_external=True)
    ext2 = d.part.relate_to("https://example.org/docs", RT.HYPERLINK, is_external=True)
    p = d.add_paragraph()  # 0: link only, external URL
    p._p.append(_el(_hl(f'r:id="{ext}" w:history="1"', "Visit our site")))
    p = d.add_paragraph()  # 1: link only, internal bookmark anchor
    p._p.append(_el(_hl('w:anchor="intro" w:history="1"', "Back to the introduction")))
    p = d.add_paragraph("Read the ")  # 2: text + link + text
    p._p.append(_el(_hl(f'r:id="{ext}"', "full guide")))
    p.add_run(" before you start.")
    p = d.add_paragraph("See ")  # 3: two links
    p._p.append(_el(_hl(f'r:id="{ext}"', "our site")))
    p.add_run(" and ")
    p._p.append(_el(_hl(f'r:id="{ext2}"', "the docs")))
    p.add_run(".")
    p = d.add_paragraph()  # 4: the bookmark the anchor points at
    p._p.append(_el(f'<w:bookmarkStart {_NSDECL} w:id="3" w:name="intro"/>'))
    p.add_run("Introduction.")
    p._p.append(_el(f'<w:bookmarkEnd {_NSDECL} w:id="3"/>'))
    cell = d.add_table(rows=1, cols=1).rows[0].cells[0]  # 5: link-only table cell
    cell.paragraphs[0]._p.append(_el(_hl(f'r:id="{ext}"', "Cell link")))
    sec = d.sections[0]
    hp = sec.header.paragraphs[0]
    hrid = sec.header.part.relate_to("https://example.com/h", RT.HYPERLINK, is_external=True)
    hp._p.append(_el(_hl(f'r:id="{hrid}"', "Header link")))
    fp = sec.footer.paragraphs[0]
    frid = sec.footer.part.relate_to("https://example.com/f", RT.HYPERLINK, is_external=True)
    fp._p.append(_el(_hl(f'r:id="{frid}"', "Footer link")))
    buf = io.BytesIO()
    d.save(buf)
    return buf.getvalue()


def _links(p) -> list[tuple]:
    """(r:id, anchor, rStyle of each run, text) per w:hyperlink child of `p`."""
    out = []
    for h in p.findall("w:hyperlink", NS):
        styles = h.xpath("w:r/w:rPr/w:rStyle/@w:val", namespaces=NS)
        out.append((h.get(W("r", "id")), h.get(W("w", "anchor")), styles, "".join(h.itertext())))
    return out


def _no_dangling_rids(data: bytes) -> None:
    z = zipfile.ZipFile(io.BytesIO(data))
    for part in ("word/document.xml", "word/header1.xml", "word/footer1.xml"):
        if part not in z.namelist():
            continue
        rels = _rels(data, part.replace("word/", "word/_rels/") + ".rels")
        for rid in etree.fromstring(z.read(part)).xpath("//@r:id", namespaces=NS):
            assert rid in rels, (part, rid, rels)


def test_hyperlinks():
    from docx import Document as Docx

    f = get_filter("docx")
    src = build_hyperlink_fixture()
    parsed = f.parse(src)
    units = {u.p_index: u.text for u in parsed.units}
    # walk 4 also extracts the header and footer; this test translates the body only
    body_only = {
        str(d["p_index"]) for d in parsed.skeleton["units"] if d["part"] == "word/document.xml"
    }
    assert len(body_only) == len(units) - 2, (len(body_only), len(units))
    # a link-only paragraph is a single run: plain text, no tokens (as before)
    assert units[0] == "Visit our site" and units[1] == "Back to the introduction", units
    assert units[2] == "⟦r1⟧Read the ⟦/r1⟧⟦r2⟧full guide⟦/r2⟧⟦r3⟧ before you start.⟦/r3⟧"
    assert units[3] == (
        "⟦r1⟧See ⟦/r1⟧⟦r2⟧our site⟦/r2⟧⟦r3⟧ and ⟦/r3⟧⟦r4⟧the docs⟦/r4⟧⟦r5⟧.⟦/r5⟧"
    ), units[3]
    cell_i = next(i for i, t in units.items() if t == "Cell link")
    out = f.build_localized(
        src, {str(i): _translate(t) for i, t in units.items() if str(i) in body_only}, "es-ES"
    )
    _package_intact(src, out)
    _no_dangling_rids(out)
    src_paras, out_paras = _body_paras(_doc_xml(src)), _body_paras(_doc_xml(out))
    src_cell = _doc_xml(src).find(".//w:tc/w:p", NS)
    out_cell = _doc_xml(out).find(".//w:tc/w:p", NS)
    pairs = [*zip(src_paras[:4], out_paras[:4], strict=True), (src_cell, out_cell)]
    for n, (sp, op) in enumerate(pairs):
        want = [(r, a, s, t.upper()) for r, a, s, t in _links(sp)]
        assert _links(op) == want, (n, _links(op), want)
    assert _child_kinds(out_paras[0]) == ["H(T)"]
    assert _child_kinds(out_paras[2]) == ["T", "H(T)", "T"], _child_kinds(out_paras[2])
    assert _child_kinds(out_paras[3]) == ["T", "H(T)", "T", "H(T)", "T"]
    assert _child_kinds(out_cell) == ["H(T)"]
    # the relationships still carry the targets the r:ids name
    rels = _rels(out, "word/_rels/document.xml.rels")
    ext = _links(out_paras[0])[0][0]
    assert rels[ext] == ("https://example.com/", "External"), rels[ext]
    assert rels[_links(out_paras[3])[1][0]] == ("https://example.org/docs", "External")
    assert _links(out_paras[1])[0][1] == "intro"
    # python-docx sees the link and its address
    doc = Docx(io.BytesIO(out))
    first = doc.paragraphs[0]
    assert first.text == "VISIT OUR SITE", first.text
    assert [h.address for h in first.hyperlinks] == ["https://example.com/"]
    assert [h.text for h in doc.paragraphs[3].hyperlinks] == ["OUR SITE", "THE DOCS"]
    assert doc.tables[0].rows[0].cells[0].paragraphs[0].hyperlinks[0].text == "CELL LINK"
    # header / footer links are untouched here (their units were left out of the mapping)
    for part, want in (("word/header1.xml", "Header link"), ("word/footer1.xml", "Footer link")):
        xml = etree.fromstring(zipfile.ZipFile(io.BytesIO(out)).read(part))
        hl = xml.find(".//w:hyperlink", NS)
        assert "".join(hl.itertext()) == want, part
        assert hl.get(W("r", "id")) in _rels(out, part.replace("word/", "word/_rels/") + ".rels")
    assert doc.sections[0].header.paragraphs[0].hyperlinks[0].address == "https://example.com/h"
    # a translation stored before this fix (untokenized) still keeps a link-only link
    legacy = f.build(src, {"0": "VISITEZ", str(cell_i): "LIEN"})
    lp = _body_paras(_doc_xml(legacy))[0]
    assert _links(lp) == [(ext, None, ["Hyperlink"], "VISITEZ")], _links(lp)
    print("  8)  hyperlinks: link-only / anchor / mixed / two links / cell keep r:id + style")


def test_field_reorder(src: bytes, body: dict):
    """Gate LOW-1: x tokens reordered around a complex field (reachable with
    export_tag_damage_policy=ship) must not invert its fldChar begin/separate/end."""
    f = get_filter("docx")
    i = KEYS.index("field_page")
    assert body[i].text == "⟦r1⟧Page ⟦/r1⟧⟦x4⟧⟦r2⟧1⟦/r2⟧⟦x5⟧⟦r3⟧ of the report.⟦/r3⟧"
    swapped = "⟦r1⟧PAGE ⟦/r1⟧⟦x5⟧⟦r2⟧1⟦/r2⟧⟦x4⟧⟦r3⟧ OF THE REPORT.⟦/r3⟧"
    assert "reordered" in tag_damage(body[i].text, swapped)
    op = _body_paras(_doc_xml(f.build(src, {str(i): swapped})))[i]
    kinds = [k for k in _child_kinds(op) if k in ("fldChar", "instrText")]
    types = op.xpath(".//w:fldChar/@w:fldCharType", namespaces=NS)
    assert types == ["begin", "separate", "end"], types
    assert kinds == ["fldChar", "instrText", "fldChar", "fldChar"], kinds
    assert _counts(op) == _counts(_body_paras(_doc_xml(src))[i])

    # two whole fields swapped are each still well-formed: the translator's order stands
    from docx import Document as Docx

    d = Docx()
    p = d.add_paragraph("A ")
    for n, instr in ((1, "PAGE"), (2, "NUMPAGES")):
        for frag in (
            '<w:r><w:fldChar w:fldCharType="begin"/></w:r>',
            f'<w:r><w:instrText xml:space="preserve"> {instr} </w:instrText></w:r>',
            '<w:r><w:fldChar w:fldCharType="separate"/></w:r>',
            f"<w:r><w:t>{n}</w:t></w:r>",
            '<w:r><w:fldChar w:fldCharType="end"/></w:r>',
        ):
            p._p.append(_el(frag.replace("<w:r>", f"<w:r {_NSDECL}>", 1)))
        p.add_run(" B " if n == 1 else " C")
    buf = io.BytesIO()
    d.save(buf)
    two = buf.getvalue()
    text = f.parse(two).units[0].text
    toks = re.findall(r"⟦x\d+⟧", text)
    assert len(toks) == 4, text
    # NUMPAGES field first, then PAGE: groups g2 g3 before g0 g1
    moved = f"A {toks[2]}2{toks[3]} B {toks[0]}1{toks[1]} C"
    tp = _body_paras(_doc_xml(f.build(two, {"0": moved})))[0]
    instrs = [t.strip() for t in tp.xpath(".//w:instrText/text()", namespaces=NS)]
    assert instrs == ["NUMPAGES", "PAGE"], instrs
    types = tp.xpath(".//w:fldChar/@w:fldCharType", namespaces=NS)
    assert types == ["begin", "separate", "end"] * 2, types
    print("  9)  reordered field markers -> fldChar order repaired; swapped whole fields kept")


def _one_para_doc(inner: str) -> bytes:
    from docx import Document as Docx

    d = Docx()
    body = d.element.body
    body.insert(len(body) - 1, _el(f"<w:p {_NSDECL}>{inner}</w:p>"))
    buf = io.BytesIO()
    d.save(buf)
    return buf.getvalue()


def _spaced_variants(text: str) -> dict:
    """One variant per adjacent token pair with a stray space between the two tokens."""
    toks = re.findall(r"⟦[^⟧]*⟧", text)
    out = {}
    for i in range(len(toks) - 1):
        pair = toks[i] + toks[i + 1]
        if pair in text:
            out[f"space_{i}"] = text.replace(pair, toks[i] + " " + toks[i + 1], 1)
    return out


def test_field_in_link():
    """Gate round 1 HIGH: a complex field inside a w:hyperlink (a Word TOC entry) must
    stay inside ONE hyperlink whatever text the translation puts between tokens; a
    field split across two hyperlink clones is a file Word refuses to open. Also LOW-3:
    a stray space between tokens never splits one link into several."""
    from docx import Document as Docx

    fld = (
        '<w:r><w:fldChar w:fldCharType="begin"/></w:r>'
        '<w:r><w:instrText xml:space="preserve"> PAGEREF _Toc1 \\h </w:instrText></w:r>'
        '<w:r><w:fldChar w:fldCharType="separate"/></w:r>'
        "<w:r><w:t>3</w:t></w:r>"
        '<w:r><w:fldChar w:fldCharType="end"/></w:r>'
    )
    entry = (
        '<w:hyperlink w:anchor="_Toc1" w:history="1">'
        f"<w:r>{_HL_STYLE}<w:t>Introduction</w:t></w:r><w:r><w:tab/></w:r>{fld}</w:hyperlink>"
    )
    f = get_filter("docx")

    def check(src: bytes, text: str, label: str, n_links: int) -> None:
        out = f.build(src, {"0": text})
        Docx(io.BytesIO(out))
        _no_dangling_rids(out)
        op = _body_paras(_doc_xml(out))[0]
        links = op.findall("w:hyperlink", NS)
        assert len(links) == n_links, (label, _child_kinds(op))
        chars = op.xpath(".//w:fldChar", namespaces=NS)
        assert [c.get(W("w", "fldCharType")) for c in chars] == ["begin", "separate", "end"]
        homes = {id(c.getparent().getparent()) for c in chars}
        assert len(homes) == 1 and chars[0].getparent().getparent().tag == W("w", "hyperlink"), (
            label,
            _child_kinds(op),
        )
        assert links[-1].get(W("w", "anchor")) == "_Toc1", label

    src = _one_para_doc(entry)
    text = f.parse(src).units[0].text
    assert text == "⟦r1⟧Introduction⟦/r1⟧⟦r2⟧\t⟦/r2⟧⟦x4⟧⟦r3⟧3⟦/r3⟧⟦x5⟧", text
    variants = _spaced_variants(text)
    assert len(variants) == 4, variants
    for name, v in variants.items():
        assert tag_damage(text, v) == "", name  # no token damage: the default policy ships it
        check(src, v, name, 1)
    # ship policy: field tokens reversed (the fldChar order is repaired, container kept)
    check(src, text.replace("⟦x4⟧", "⟦X⟧").replace("⟦x5⟧", "⟦x4⟧").replace("⟦X⟧", "⟦x5⟧"), "rev", 1)

    # text before the link; the translation moves that direct text INTO the field
    src2 = _one_para_doc(f'<w:r><w:t xml:space="preserve">See </w:t></w:r>{entry}')
    text2 = f.parse(src2).units[0].text
    assert text2 == "⟦r1⟧See ⟦/r1⟧⟦r2⟧Introduction⟦/r2⟧⟦r3⟧\t⟦/r3⟧⟦x5⟧⟦r4⟧3⟦/r4⟧⟦x6⟧", text2
    for name, v in _spaced_variants(text2).items():
        check(src2, v, name, 1)
    moved = "⟦r2⟧Introduction⟦/r2⟧⟦r3⟧\t⟦/r3⟧⟦x5⟧⟦r1⟧See ⟦/r1⟧⟦r4⟧3⟦/r4⟧⟦x6⟧"
    assert "reordered" in tag_damage(text2, moved)
    check(src2, moved, "moved", 1)

    # LOW-3: a link-only paragraph with three runs stays ONE link with spaces added
    link = (
        '<w:hyperlink w:anchor="a">'
        f'<w:r>{_HL_STYLE}<w:t xml:space="preserve">Read </w:t></w:r>'
        f'<w:r><w:rPr><w:rStyle w:val="Hyperlink"/><w:b/></w:rPr><w:t>this</w:t></w:r>'
        f'<w:r>{_HL_STYLE}<w:t xml:space="preserve"> now</w:t></w:r></w:hyperlink>'
    )
    src3 = _one_para_doc(link)
    text3 = f.parse(src3).units[0].text
    spaced = _spaced_variants(text3)
    assert len(spaced) == 2, text3
    both = text3.replace("⟦/r1⟧⟦r2⟧", "⟦/r1⟧ ⟦r2⟧").replace("⟦/r2⟧⟦r3⟧", "⟦/r2⟧ ⟦r3⟧")
    for name, v in {**spaced, "both": both}.items():
        op = _body_paras(_doc_xml(f.build(src3, {"0": v})))[0]
        assert _child_kinds(op) == ["H(T)"], (name, _child_kinds(op))
        styles = op.xpath("w:hyperlink/w:r/w:rPr/w:rStyle/@w:val", namespaces=NS)
        assert styles and set(styles) == {"Hyperlink"}, (name, styles)
    print("  9b) field in a link stays in one hyperlink; stray spaces never split a link")


def _strip_styles(data: bytes) -> bytes:
    """The same package without word/styles.xml (its rel and content type removed)."""
    zin = zipfile.ZipFile(io.BytesIO(data))
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zout:
        for n in zin.namelist():
            if n == "word/styles.xml":
                continue
            raw = zin.read(n)
            if n in ("word/_rels/document.xml.rels", "[Content_Types].xml"):
                root = etree.fromstring(raw)
                for el in list(root):
                    if (el.get("Target") or el.get("PartName") or "").endswith("styles.xml"):
                        root.remove(el)
                raw = etree.tostring(root, xml_declaration=True, encoding="UTF-8", standalone=True)
            zout.writestr(n, raw)
    return buf.getvalue()


def test_language_scope():
    """Gate LOW-2: the target language lands on translated runs only; text-box runs,
    tracked deletions and list content controls keep their source language (walk 6 #686
    translates a text content control inside the paragraph, so it takes the target)."""
    from docx import Document as Docx

    en = '<w:rPr><w:lang w:val="en-US"/></w:rPr>'
    d = Docx()
    p = d.add_paragraph()
    p._p.append(_el(f'<w:r {_NSDECL}>{en}<w:t xml:space="preserve">Translated words </w:t></w:r>'))
    p._p.append(_textbox_run())
    p._p.append(
        _el(
            f'<w:del {_NSDECL} w:id="11" w:author="a"><w:r>{en}'
            "<w:delText>deleted</w:delText></w:r></w:del>"
        )
    )
    p._p.append(
        _el(f'<w:ins {_NSDECL} w:id="12" w:author="a"><w:r>{en}<w:t>inserted</w:t></w:r></w:ins>')
    )
    p._p.append(
        _el(
            f"<w:sdt {_NSDECL}><w:sdtPr><w:dropDownList/></w:sdtPr><w:sdtContent><w:r>{en}"
            "<w:t>control</w:t></w:r></w:sdtContent></w:sdt>"
        )
    )
    p._p.append(
        _el(
            f"<w:sdt {_NSDECL}><w:sdtPr/><w:sdtContent><w:r>{en}<w:t> typed</w:t></w:r>"
            "</w:sdtContent></w:sdt>"
        )
    )
    p._p.append(_el(_hl('w:anchor="x"', "link")))
    p._p.append(_el(f'<w:r {_NSDECL}>{en}<w:t xml:space="preserve"> more.</w:t></w:r>'))
    # give the text box runs a source w:lang too
    for r in p._p.xpath(".//w:txbxContent//w:r"):
        r.insert(0, _el(f'<w:rPr {_NSDECL}><w:lang w:val="en-US"/></w:rPr>'))
    buf = io.BytesIO()
    d.save(buf)
    src = buf.getvalue()
    f = get_filter("docx")
    for label, data in (("styles part", src), ("no styles part", _strip_styles(src))):
        units = f.parse(data).units
        out = f.build_localized(data, {str(units[0].p_index): _translate(units[0].text)}, "fr-FR")
        op = _body_paras(_doc_xml(out))[0]

        def langs(xpath, op=op):
            return op.xpath(xpath + "/w:rPr/w:lang/@w:val", namespaces=NS)

        direct = langs("w:r[w:t]") + langs("w:hyperlink/w:r")
        assert direct and set(direct) == {"fr-FR"}, (label, direct)
        for xp in (".//w:txbxContent//w:r", "w:del/w:r", "w:sdt[w:sdtPr/w:dropDownList]//w:r"):
            got = langs(xp)
            assert got and set(got) == {"en-US"}, (label, xp, got)
        got = langs("w:sdt[not(w:sdtPr/w:dropDownList)]//w:r")
        assert got and set(got) == {"fr-FR"}, (label, "text control", got)
        # walk 4 translates a tracked insertion with the paragraph, so it takes the target
        got = langs("w:ins/w:r")
        assert got and set(got) == {"fr-FR"}, (label, "w:ins/w:r", got)
        Docx(io.BytesIO(out))
    print("  10) target language on translated runs only (text box / del / list sdt keep en-US)")
    _test_language_inherited()


def _test_language_inherited():
    """Gate round 1 LOW-4: runs WITHOUT their own w:lang inherit the document default,
    which build_localized retargets. Untranslated content (text box, tracked insertion,
    content control, an untranslated paragraph, header, footer, footnote) gets the
    SOURCE language written on it; a run whose character style names another language
    keeps inheriting that; translated runs stay bare and inherit the target."""
    from docx import Document as Docx
    from docx.enum.style import WD_STYLE_TYPE
    from docx.opc.constants import RELATIONSHIP_TYPE as RT
    from docx.opc.packuri import PackURI
    from docx.opc.part import Part

    d = Docx()
    de = d.styles.add_style("GermanChar", WD_STYLE_TYPE.CHARACTER)
    de.element.get_or_add_rPr().append(_el(f'<w:lang {_NSDECL} w:val="de-DE"/>'))
    p = d.add_paragraph("Translated words ")
    p._p.append(_textbox_run())
    p._p.append(
        _el(f'<w:ins {_NSDECL} w:id="12" w:author="a"><w:r><w:t>inserted</w:t></w:r></w:ins>')
    )
    p._p.append(
        _el(
            f"<w:sdt {_NSDECL}><w:sdtPr><w:dropDownList/></w:sdtPr><w:sdtContent><w:r>"
            "<w:t>control</w:t></w:r></w:sdtContent></w:sdt>"
        )
    )
    left = d.add_paragraph("Left untranslated.")
    left._p.append(
        _el(f'<w:r {_NSDECL}><w:rPr><w:rStyle w:val="GermanChar"/></w:rPr><w:t>Wort</w:t></w:r>')
    )
    sec = d.sections[0]
    sec.header.paragraphs[0].text = "Header stays English"
    sec.footer.paragraphs[0].text = "Footer stays English"
    fn = Part(
        PackURI("/word/footnotes.xml"),
        "application/vnd.openxmlformats-officedocument.wordprocessingml.footnotes+xml",
        FOOTNOTES_XML,
        d.part.package,
    )
    d.part.relate_to(fn, RT.FOOTNOTES)
    buf = io.BytesIO()
    d.save(buf)
    src = buf.getvalue()
    f = get_filter("docx")
    units = f.parse(src).units
    out = f.build_localized(src, {str(units[0].p_index): _translate(units[0].text)}, "fr-FR")
    Docx(io.BytesIO(out))
    z = zipfile.ZipFile(io.BytesIO(out))
    styles = etree.fromstring(z.read("word/styles.xml"))
    assert styles.xpath("//w:docDefaults//w:lang/@w:val", namespaces=NS) == ["fr-FR"]
    root = etree.fromstring(z.read("word/document.xml"))
    op, left = _body_paras(root)[:2]

    def langs(el, xpath):
        return [
            (r.xpath("string(w:rPr/w:lang/@w:val)", namespaces=NS), "".join(r.itertext()))
            for r in el.xpath(xpath, namespaces=NS)
        ]

    translated = langs(op, "w:r[w:t]")
    assert translated == [("", "TRANSLATED WORDS ")], translated
    for xp in (".//w:txbxContent//w:r", "w:sdt//w:r"):
        got = langs(op, xp)
        assert got and all(lang == "en-US" for lang, _t in got), (xp, got)
    # the tracked insertion is translated with its paragraph (walk 4): bare, inherits target
    assert langs(op, "w:ins/w:r") == [("", "INSERTED")], langs(op, "w:ins/w:r")
    got = langs(left, "w:r")
    assert got == [("en-US", "Left untranslated."), ("", "Wort")], got
    for part in ("word/header1.xml", "word/footer1.xml", "word/footnotes.xml"):
        runs = langs(etree.fromstring(z.read(part)), "//w:r[w:t]")
        assert runs and all(lang == "en-US" for lang, _t in runs), (part, runs)
    print("  10b) inherited language: untranslated content stamped en-US, styled run kept")


def test_legacy_r_numbering():
    """Gate LOW-3: r tokens are numbered per ORIGINAL run, so a legacy translation (one
    pair per run) keeps each run's formatting when one run has text on both sides of an
    object; a translation stored under the 2026-09-25 per-atom numbering still reads."""
    from docx import Document as Docx

    d = Docx()
    p = d.add_paragraph()
    r1 = p.add_run("Bold left ")
    r1.bold = True
    pic = p.add_run()
    pic.add_picture(io.BytesIO(_png()))
    r1._r.append(pic._r.find(W("w", "drawing")))
    p._p.remove(pic._r)
    r1._r.append(_el(f'<w:t {_NSDECL} xml:space="preserve">bold right</w:t>'))
    p.add_run(" italic tail").italic = True
    buf = io.BytesIO()
    d.save(buf)
    src = buf.getvalue()
    f = get_filter("docx")
    text = f.parse(src).units[0].text
    assert text == "⟦r1⟧Bold left ⟦x3⟧bold right⟦/r1⟧⟦r2⟧ italic tail⟦/r2⟧", text
    assert seg.placeholders_well_formed(text)

    def shape(data: bytes):
        op = _body_paras(_doc_xml(data))[0]
        runs = []
        for r in op.findall("w:r", NS):
            fmt = "b" if r.find("w:rPr/w:b", NS) is not None else ""
            fmt += "i" if r.find("w:rPr/w:i", NS) is not None else ""
            body = "D" if r.find("w:drawing", NS) is not None else "".join(r.itertext())
            runs.append((fmt, body))
        return runs

    current = shape(f.build(src, {"0": _translate(text)}))
    assert current == [
        ("b", "BOLD LEFT "),
        ("b", "D"),
        ("b", "BOLD RIGHT"),
        ("i", " ITALIC TAIL"),
    ], current
    # the old content-run path stored one pair per run with the run's whole text
    legacy = shape(f.build(src, {"0": "⟦r1⟧BOLD LEFT BOLD RIGHT⟦/r1⟧⟦r2⟧ ITALIC TAIL⟦/r2⟧"}))
    assert legacy == [("b", "BOLD LEFT BOLD RIGHT"), ("b", "D"), ("i", " ITALIC TAIL")], legacy
    # stored between 2026-09-25 and this fix: one pair per text atom, x after them
    atom = "⟦r1⟧BOLD LEFT ⟦/r1⟧⟦x4⟧⟦r2⟧BOLD RIGHT⟦/r2⟧⟦r3⟧ ITALIC TAIL⟦/r3⟧"
    assert shape(f.build(src, {"0": atom})) == current
    # no token survived: all text in the first direct run, the picture still kept
    plain = shape(f.build(src, {"0": "TOUT"}))
    assert plain == [("b", "TOUT"), ("b", "D")], plain
    print("  11) r tokens per original run: legacy and per-atom stored translations map")


# ------------------------------------------------------------------------------- PO
PO_NO_COMMENT = (
    b'msgid ""\nmsgstr ""\n"Content-Type: text/plain; charset=UTF-8\\n"\n"Language: en\\n"\n'
    b'"Plural-Forms: nplurals=2; plural=(n != 1);\\n"\n\n'
    b'#: a.c:1\nmsgid "Hello"\nmsgstr ""\n\n'
    b'msgid "One"\nmsgid_plural "Many"\nmsgstr[0] ""\nmsgstr[1] ""\n\n'
    b'#~ msgid "Obsolete"\n#~ msgstr "Obs"\n'
)
PO_WITH_COMMENT = b"# Title\n# Copyright\n" + PO_NO_COMMENT


def _po_header(text: str) -> str:
    """The one header entry's unescaped msgstr (build() emits it on a single line)."""
    from app.filters.po_filter import _is_header, _parse_entries

    heads = [e for e in _parse_entries(text.encode()) if _is_header(e)]
    assert len(heads) == 1, heads
    return heads[0]["msgstr"]


def test_po():
    f = get_filter("po")
    mapping = {"0": "Hola", "1": "Uno", "2": "Muchos"}
    for name, src in (("no comments", PO_NO_COMMENT), ("with comments", PO_WITH_COMMENT)):
        units = [u.text for u in f.parse(src).units]
        assert units == ["Hello", "One", "Many"], units
        out = f.build(src, mapping).decode()
        assert out.count('msgid ""') == 1, (name, out)
        hdr = _po_header(out)
        assert "Content-Type: text/plain; charset=UTF-8\n" in hdr, (name, out)
        assert "Plural-Forms: nplurals=2; plural=(n != 1);\n" in hdr, (name, out)
        assert "Language: en\n" in hdr, (name, out)
        assert out.index('msgid ""') < out.index('msgid "Hello"')
        assert out.rstrip("\n").endswith('#~ msgid "Obsolete"\n#~ msgstr "Obs"'), (name, out)
        assert 'msgstr "Hola"' in out and 'msgstr[1] "Muchos"' in out
        loc = f.build_localized(src, mapping, "es-ES").decode()
        assert "Language: es_ES\n" in _po_header(loc), (name, loc)
        assert "Language: en" not in loc, (name, loc)
        assert loc.count('msgid ""') == 1, (name, loc)
        # the rebuilt file parses back to the same units and still has its header
        assert [u.text for u in f.parse(out.encode()).units] == units
        print(f"  7)  PO {name}: header kept (charset, Plural-Forms, Language), one header")
    # obsolete blocks in the middle and at the end, no header at all: no header invented
    src = (
        b'msgid "A"\nmsgstr ""\n\n#~ msgid "Old"\n#~ msgstr "Viejo"\n\n'
        b'msgid "B"\nmsgstr ""\n\n#~ msgid "Z"\n'
    )
    out = f.build(src, {"0": "a", "1": "b"}).decode()
    assert 'msgid ""' not in out, out
    assert out == (
        'msgid "A"\nmsgstr "a"\n\n#~ msgid "Old"\n#~ msgstr "Viejo"\n\n'
        'msgid "B"\nmsgstr "b"\n\n#~ msgid "Z"\n'
    ), out
    # a header-only file keeps its header
    only = b'msgid ""\nmsgstr ""\n"Language: fr\\n"\n'
    assert _po_header(f.build(only, {}).decode()) == "Language: fr\n", f.build(only, {})
    print("  7)  PO obsolete blocks never gain a header; header-only file unchanged")
    # Gate LOW-4: malformed blocks (an orphan msgstr with no msgid, an unterminated
    # msgid) parse as msgid "" too; only the real header may come out as msgid "".
    hdr = b'msgid ""\nmsgstr ""\n"Content-Type: text/plain; charset=UTF-8\\n"\n"Language: en\\n"\n'
    body = b'\n#: a.c:1\nmsgid "Hello"\nmsgstr ""\n'
    orphan = b'\n# orphan\nmsgstr "stray"\n'
    unterminated = b'\nmsgid "hello\nmsgstr "x"\n'
    for name, src in (
        ("orphan msgstr after header", hdr + orphan + body),
        ("unterminated msgid after header", hdr + unterminated + body),
        ("orphan msgstr before header", orphan.lstrip(b"\n") + b"\n" + hdr + body),
        # gate round 1 MEDIUM: a malformed block before the header must never win
        ("unterminated msgid before header", unterminated.lstrip(b"\n") + b"\n" + hdr + body),
    ):
        assert [u.text for u in f.parse(src).units] == ["Hello"], name
        for out in (
            f.build(src, {"0": "Hola"}).decode(),
            f.build_localized(src, {"0": "Hola"}, "es-ES").decode(),
        ):
            assert out.count('msgid ""') == 1, (name, out)
            assert "Content-Type: text/plain; charset=UTF-8\n" in _po_header(out), (name, out)
            assert "stray" not in out and 'msgstr "Hola"' in out, (name, out)
            assert "\n\n\n" not in out, (name, out)
        assert "Language: es_ES\n" in _po_header(out), (name, out)
        assert "# orphan" in out or b"orphan" not in src, (name, out)
    print('  12) PO malformed blocks never produce a second msgid "" header')


def test_xml_target_characters():
    """#243: plain, tokenized, object and alt targets all reach XML-safe sinks."""
    from docx import Document as Docx

    f = get_filter("docx")
    src = build_fixture()
    bad = "A\x0bB\x0cC\x00\x08\x1f\ufffe\uffff\ud800\udfff\tD\nE\rF\U0001f642"
    for build in (f.build, lambda s, m: f.build_localized(s, m, "es-ES")):
        mapping = {str(u.p_index): bad for u in f.parse(src).units}
        out = build(src, mapping)
        Docx(io.BytesIO(out))
        xml = _doc_xml(out)
        assert xml.xpath("//w:br", namespaces=NS)
        assert "\U0001f642" in etree.tostring(xml, encoding="unicode")
        assert "A\nB\nC\tD\nE\rF\U0001f642" in xml.xpath("//wp:docPr/@descr", namespaces=NS)
    d = Docx()
    d.add_paragraph("Plain")
    p = d.add_paragraph("One ")
    p.add_run("two").bold = True
    buf = io.BytesIO()
    d.save(buf)
    src = buf.getvalue()
    units = f.parse(src).units
    out = f.build(src, {"0": bad, "1": units[1].text.replace("two", bad)})
    assert len(_doc_xml(out).xpath("//w:br", namespaces=NS)) >= 6
    from openpyxl import Workbook, load_workbook

    wb = Workbook()
    wb.active["A1"] = "Text"
    buf = io.BytesIO()
    wb.save(buf)
    out = get_filter("xlsx").build_localized(buf.getvalue(), {"0": bad}, "es-ES")
    assert load_workbook(io.BytesIO(out)).active["A1"].value == "A\nB\nC\tD\nE\rF\U0001f642"
    xliff = b'<xliff version="1.2"><file><body><trans-unit id="1">'
    xliff += b'<source>Hello <g id="g">world</g></source></trans-unit></body></file></xliff>'
    f = get_filter("xliff")
    target = f.parse(xliff).units[0].text.replace("world", bad)
    out = f.build_localized(xliff, {"0": target}, "es-ES")
    root = etree.fromstring(out)
    assert root.find(".//target/g").text == "A\nB\nC\tD\nE\rF\U0001f642"


def test_po_fuzzy_delivery():
    from babel.messages.mofile import read_mo, write_mo
    from babel.messages.pofile import read_po

    from app.filters.po_filter import _parse_entries

    src = (
        b'#, fuzzy, python-format\n#| msgid "Old"\n#| " continuation"\n'
        b'#: source.py:1\n#. Keep me\nmsgid "Save"\nmsgstr "Old target"\n\n'
        b'#, fuzzy\n#| msgid "Before"\nmsgid "Untouched"\nmsgstr "Existing"\n'
    )
    out = get_filter("po").build(src, {"0": "Guardar"})
    entries = _parse_entries(out)
    assert entries[0]["comments"] == ["#, python-format", "#: source.py:1", "#. Keep me"]
    assert entries[1]["comments"] == ["#, fuzzy", '#| msgid "Before"']
    assert entries[1]["msgstr"] == "Existing"
    compiled = io.BytesIO()
    write_mo(compiled, read_po(io.BytesIO(out)))
    compiled.seek(0)
    assert read_mo(compiled).get("Save").string == "Guardar"
    out = get_filter("po").build(src, {"1": ""})
    assert _parse_entries(out)[1]["comments"] == []


def test_po_plural_delivery():
    import gettext

    from babel.messages.plurals import get_plural

    from app.filters.po_filter import _parse_entries

    f = get_filter("po")
    for locale in ("ru-RU", "pl-PL", "cs-CZ", "ar", "ja", "fr-FR", "en-US"):
        out = f.build_localized(PO_NO_COMMENT, {"0": "Hi", "1": "One", "2": "Many"}, locale)
        rule = get_plural(locale.replace("-", "_"))
        assert f"Plural-Forms: {rule}\n" in _po_header(out.decode()), locale
        slots = re.findall(rb"^msgstr\[(\d+)\]", out, re.M)
        assert slots == [str(i).encode() for i in range(rule.num_plurals)], (locale, slots)
        # #729 (decision 18, was: slots past msgstr[1] left empty): every slot holds a
        # translation, the singular where the rule puts n = 1, the plural elsewhere
        pick = gettext.c2py(rule.plural_expr)
        for i in range(rule.num_plurals):
            want = "One" if i == pick(1) and rule.num_plurals > 1 else "Many"
            assert f'msgstr[{i}] "{want}"'.encode() in out, (locale, i, out)
    src = (
        b'msgid "One"\nmsgid_plural "Many"\nmsgstr[0] "a"\nmsgstr[1] "b"\n'
        b'msgstr[2] "c"\n"d"\nmsgstr[3] "e"\n'
    )
    out = f.build(src, {"0": "a2"})  # B-bytes: build(src, {}) is src itself now
    assert b'msgstr[2] "cd"' in out and b'msgstr[3] "e"' in out
    assert len(_parse_entries(out)) == 1
    single = b'msgid "One"\nmsgid_plural "Many"\nmsgstr[0] "Only"\n'
    assert b"msgstr[1]" not in f.build(single, {})
    assert b"msgstr[1]" not in f.build(single, {"0": "Updated"})


def test_po_no_blank_line_between_entries():
    """#507: PO entries need no blank line between them (valid for GNU gettext).

    A msgid, msgctxt or comment that follows a completed msgstr starts a new entry. The
    old parser flushed only on blank lines, so a second msgid in the same block overwrote
    the first (dropping the entry) and a following `#:` comment glued onto the previous one.
    """
    from app.filters.po_filter import _parse_entries

    f = get_filter("po")

    # 1) the issue's input: a header, then apple and banana with no blank line between them.
    src = (
        b'msgid ""\n'
        b'msgstr "Content-Type: text/plain; charset=UTF-8\\n"\n'
        b"\n"
        b'msgid "apple"\nmsgstr ""\n'
        b'msgid "banana"\nmsgstr ""\n'
    )
    assert [u.text for u in f.parse(src).units] == ["apple", "banana"]
    out = f.build(src, {"0": "pomme", "1": "banane"}).decode()
    assert 'msgid "apple"\nmsgstr "pomme"' in out, out
    assert 'msgid "banana"\nmsgstr "banane"' in out, out
    assert out.count('msgid ""') == 1, out

    # 2) a `#:` reference right after a msgstr with no blank line belongs to the NEXT entry.
    src = b'msgid "apple"\nmsgstr ""\n#: fruits.py:3\nmsgid "banana"\nmsgstr ""\n'
    entries = _parse_entries(src)
    assert [e["msgid"] for e in entries] == ["apple", "banana"], entries
    assert entries[0]["comments"] == [], entries[0]
    assert entries[1]["comments"] == ["#: fruits.py:3"], entries[1]
    out = f.build(src, {"0": "pomme", "1": "banane"})
    assert b'#: fruits.py:3\nmsgid "banana"' in out, out
    assert b'#: fruits.py:3\nmsgid "apple"' not in out, out

    # 3) msgctxt and plural entries with no blank line still parse as separate entries.
    ctx = b'msgctxt "menu"\nmsgid "File"\nmsgstr ""\nmsgctxt "toolbar"\nmsgid "Save"\nmsgstr ""\n'
    entries = _parse_entries(ctx)
    assert [(e["msgctxt"], e["msgid"]) for e in entries] == [("menu", "File"), ("toolbar", "Save")]
    assert [u.text for u in f.parse(ctx).units] == ["File", "Save"]
    plural = (
        b'msgid "One"\nmsgid_plural "Many"\nmsgstr[0] ""\nmsgstr[1] ""\n'
        b'msgid "Two"\nmsgid_plural "Twos"\nmsgstr[0] ""\nmsgstr[1] ""\n'
    )
    entries = _parse_entries(plural)
    assert [e["msgid"] for e in entries] == ["One", "Two"], entries
    assert [e["msgid_plural"] for e in entries] == ["Many", "Twos"], entries
    assert [u.text for u in f.parse(plural).units] == ["One", "Many", "Two", "Twos"]

    # 4) a well-formed file (a blank line between every entry) round-trips byte for byte.
    canonical = (
        b'msgid ""\nmsgstr "Content-Type: text/plain; charset=UTF-8\\n"\n'
        b'\n#: fruit.py:1\nmsgid "apple"\nmsgstr ""\n'
        b'\nmsgid "One"\nmsgid_plural "Many"\nmsgstr[0] ""\nmsgstr[1] ""\n'
    )
    assert f.build(canonical, {}) == canonical, f.build(canonical, {})
    assert [u.text for u in f.parse(canonical).units] == ["apple", "One", "Many"]

    # 5) documented boundaries of the rule (review findings, no code change). The boundary is
    #    "follows a completed msgstr", so two msgid lines with no msgstr between them still
    #    merge and keep the last (GNU msgfmt rejects such a file anyway), and a flag comment
    #    left dangling at EOF belongs to no entry, so it survives an update untouched.
    merged = _parse_entries(b'msgid "a"\nmsgid "b"\nmsgstr ""\n')
    assert [(e["msgid"], e["msgstr"]) for e in merged] == [("b", "")], merged
    assert [u.text for u in f.parse(b'msgid "a"\nmsgid "b"\nmsgstr ""\n').units] == ["b"]
    dangling = f.build(b'msgid "a"\nmsgstr ""\n#, fuzzy\n', {"0": "A"})
    # B-bytes: the file's own layout is kept (no blank line is inserted before it)
    assert dangling == b'msgid "a"\nmsgstr "A"\n#, fuzzy\n', dangling

    print("  #507) PO entries with no blank line parse/rebuild; blank-line files byte-identical")


def external_fixture():
    from docx import Document as Docx
    from docx.opc.constants import RELATIONSHIP_TYPE as RT

    d = Docx()
    d.add_paragraph("External resources.")
    d.part.part_related_by(RT.SETTINGS).relate_to(
        "https://example.invalid/t.dotm", NS["r"] + "/attachedTemplate", True
    )
    d.part.relate_to(r"\\example.invalid\share\image.png", RT.IMAGE, True)
    d.part.relate_to("https://example.invalid/object", RT.OLE_OBJECT, True)
    d.part.relate_to("https://example.invalid/site", RT.HYPERLINK, True)
    for command in ("INCLUDEPICTURE", "INCLUDETEXT", "LINK", "DDE", "DDEAUTO"):
        p = d.add_paragraph("Field ")
        field = _el(f"<w:fldSimple {_NSDECL}/>")
        field.set(W("w", "instr"), f' {command} "https://example.invalid/file" ')
        p._p.append(field)
    p = d.sections[0].header.paragraphs[0]
    for xml in (
        '<w:fldChar w:fldCharType="begin"/>',
        "<w:instrText> INCLU</w:instrText>",
        '<w:instrText>DETEXT "\\\\example.invalid\\share\\file"</w:instrText>',
        '<w:fldChar w:fldCharType="end"/>',
    ):
        p.add_run()._r.append(_el(f"<w:r {_NSDECL}>{xml}</w:r>")[0])
    buf = io.BytesIO()
    d.save(buf)
    return buf.getvalue()


def test_external_resource_warnings():
    f = get_filter("docx")
    src = external_fixture()
    all_warnings = f.parse(src).skeleton.get("file_warnings", [])
    warnings = [w for w in all_warnings if w["code"] == "docx_external_resource"]
    assert len(warnings) == 9, warnings  # 3 relationships, 5 simple + 1 split field
    assert all(w["code"] == "docx_external_resource" for w in warnings)
    assert any(w["part"] == "word/header1.xml" for w in warnings)
    assert not any(
        w["code"] == "docx_external_resource"
        for w in f.parse(build_hyperlink_fixture()).skeleton.get("file_warnings", [])
    )
    out = f.build_localized(src, {"0": "Ressources externes."}, "fr-FR")
    assert f.parse(out).skeleton["file_warnings"] == all_warnings


def test_resource_warning_surfaces():
    import json
    import subprocess

    from app.services import apikeys, security

    client = TestClient(app)
    created = client.post(
        "/projects",
        data={"name": "Resource warnings", "source_locale": "en-US", "target_locales": "fr-FR"},
        follow_redirects=False,
    )
    assert created.status_code == 303, created.text
    pid = int(created.headers["location"].rsplit("/", 1)[-1])
    user = security.create_user(
        "fidelity@test.invalid", "Fidelity admin", "admin", "fidelity-test-passphrase"
    )
    _, raw = apikeys.create_key(user.id, "fidelity", scopes="api docs admin")
    headers = {"X-Loc-Token": raw}
    uploaded = client.post(
        "/api/documents",
        headers=headers,
        data={"project_id": str(pid)},
        files={
            "file": (
                "external.docx",
                _gate_package(
                    {"customXml/broken.xml": b"not XML"},
                    body=b'<w:p><w:fldSimple w:instr="INCLUDETEXT local.docx"/></w:p>',
                ),
            )
        },
    )
    assert uploaded.status_code == 200, uploaded.text
    warnings = uploaded.json()["file_warnings"]
    assert {w["code"] for w in warnings} == {"docx_external_resource", "docx_unscanned_part"}
    did = uploaded.json()["document_id"]
    page = client.get(f"/documents/{did}")
    assert page.status_code == 200 and warnings[0]["message"] in page.text
    inspected = client.get(f"/api/documents/{did}/segments", headers=headers)
    assert inspected.status_code == 200, inspected.text
    assert inspected.json()["file_warnings"] == warnings
    for flags in ([], ["--json"]):
        proc = subprocess.run(
            [sys.executable, "-m", "app.cli", *flags, "doc", "show", str(did)],
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=90,
            check=False,
        )
        assert proc.returncode == 0, proc.stderr
        if flags:
            assert json.loads(proc.stdout)["file_warnings"] == warnings
        else:
            assert warnings[0]["message"] in proc.stdout
    # Remote CLI is an offline API adapter here; no HTTP socket is opened.
    from contextlib import redirect_stdout
    from types import SimpleNamespace
    from unittest.mock import Mock, patch

    from app.cli import _remote_impl_part3
    from app.cli._common import Emitter

    adapter = Mock()
    adapter.get.return_value = inspected.json()
    adapter.upload.return_value = uploaded.json()
    args = SimpleNamespace(doc_id=did, locale=None, detail=False)
    for as_json in (False, True):
        capture = io.StringIO()
        with (
            patch.object(_remote_impl_part3, "_client", return_value=adapter),
            redirect_stdout(capture),
        ):
            assert _remote_impl_part3.doc_show(args, Emitter(as_json=as_json)) == 0
        if as_json:
            assert json.loads(capture.getvalue())["file_warnings"] == warnings
        else:
            assert warnings[0]["message"] in capture.getvalue()
    args = SimpleNamespace(file="external.docx", project_id=pid, mode="segmented", context_json="")
    capture = io.StringIO()
    with (
        patch.object(_remote_impl_part3, "_client", return_value=adapter),
        patch.object(_remote_impl_part3, "_read_bytes", return_value=external_fixture()),
        redirect_stdout(capture),
    ):
        assert _remote_impl_part3.doc_upload(args, Emitter(as_json=True)) == 0
    assert json.loads(capture.getvalue())["file_warnings"] == warnings


def test_gate_po_export():
    from app.filters.po_filter import _parse_entries

    client = TestClient(app)
    src = (
        b'msgid ""\nmsgstr "Language: en\\n"\n\n'
        b'#, fuzzy\n#| msgid "Old save"\nmsgid "Save"\nmsgstr "old"\n\n'
        b'#, fuzzy\n#| msgid "Old close"\nmsgid "Close"\nmsgstr "existing"\n\n'
        b'#, fuzzy\n#| msgid "Old file"\nmsgid "file"\nmsgid_plural "files"\n'
        b'msgstr[0] "f0"\nmsgstr[1] "f1"\n'
    )
    failures = []
    for locale in ("es-ES", "ru-RU"):
        created = client.post(
            "/projects",
            data={"name": "PO gate " + locale, "source_locale": "en-US", "target_locales": locale},
            follow_redirects=False,
        )
        pid = int(created.headers["location"].rsplit("/", 1)[-1])
        uploaded = client.post(
            f"/projects/{pid}/documents",
            data={"mode": "segmented"},
            files={"file": ("gate.po", src)},
            follow_redirects=False,
        )
        did = int(uploaded.headers["location"].rsplit("/", 1)[-1])
        with Session(engine) as session:
            rows = session.exec(select(Segment).where(Segment.document_id == did)).all()
            sid = next(row.id for row in rows if row.source_text == "Save")
        target = "Save" if locale == "ru-RU" else "Saved target"
        saved = client.post(f"/segments/{sid}/save", data={"target_text": target})
        assert saved.status_code == 200, saved.text
        exported = client.get(f"/documents/{did}/export?locale={locale}")
        assert exported.status_code == 200, exported.text
        entries = {e["msgid"]: e for e in _parse_entries(exported.content)}
        assert entries["Save"]["comments"] == []
        for key, slot, value in (("Close", "msgstr", "existing"), ("file", "msgstr0", "f0")):
            if (
                "#, fuzzy" not in entries[key]["comments"]
                or not any(c.startswith("#|") for c in entries[key]["comments"])
                or entries[key][slot] != value
            ):
                failures.append((locale, key, entries[key]))
    assert not failures, failures


def _gate_package(extra=None, rename=False, body=b""):
    from docx import Document as Docx

    doc = Docx()
    doc.add_paragraph("Hello.")
    buf = io.BytesIO()
    doc.save(buf)
    out = io.BytesIO()
    with zipfile.ZipFile(buf) as src, zipfile.ZipFile(out, "w") as dest:
        for name in src.namelist():
            data = src.read(name)
            if name == "word/document.xml":
                data = data.replace(b"</w:body>", body + b"</w:body>")
            if rename:
                data = data.replace(b"word/document.xml", b"word/document.dat")
                name = name.replace("document.xml", "document.dat")
            dest.writestr(name, data)
        for name, data in (extra or {}).items():
            dest.writestr(name, data)
    return out.getvalue()


def test_gate_resource_bypasses():
    from app.filters.docx_resources import external_resource_warnings

    failures = []
    for command in ("INCLUDEPICTURE", "INCLUDETEXT", "LINK", "DDE", "DDEAUTO"):
        for nested in ("QUOTE", "DOCPROPERTY"):
            body = (
                '<w:p><w:r><w:fldChar w:fldCharType="begin"/></w:r>'
                f"<w:r><w:instrText> {command} </w:instrText></w:r>"
                '<w:r><w:fldChar w:fldCharType="begin"/></w:r>'
                f"<w:r><w:instrText> {nested} Target </w:instrText></w:r>"
                '<w:r><w:fldChar w:fldCharType="end"/></w:r>'
                '<w:r><w:fldChar w:fldCharType="end"/></w:r></w:p>'
            ).encode()
            if not external_resource_warnings(_gate_package(body=body)):
                failures.append((command, nested))
    body = b'<w:p><w:fldSimple w:instr="INCLUDEPICTURE //host/share"/></w:p>'
    for rename in (False, True):
        pkg = _gate_package(body=body, rename=rename)
        if not get_filter("docx").parse(pkg).skeleton.get("file_warnings"):
            failures.append(("main part", rename))
    rel = (
        b'<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        b'<Relationship Id="x" Type="image" Target="//host/share" TargetMode="External"/>'
        b"</Relationships>"
    )
    if not external_resource_warnings(_gate_package({"word/_rels/settings.XML.RELS": rel})):
        failures.append("uppercase rels")
    hyperlink = b'<w:p><w:fldSimple w:instr="HYPERLINK https://example.invalid"/></w:p>'
    assert not external_resource_warnings(_gate_package(body=hyperlink))
    assert not failures, failures


def test_gate_unscanned_parts():
    from unittest.mock import patch

    from app.filters import docx_resources

    amplification = (
        b'<!DOCTYPE r [<!ENTITY a "aaaaaaaaaa">'
        + b"".join(
            b'<!ENTITY %s "%s">' % (bytes([98 + i]), (b"&" + bytes([97 + i]) + b";") * 10)
            for i in range(9)
        )
        + b"]><r>&j;</r>"
    )
    for payload in (b"not XML", b"\x00\xff", b"<r>" * 300 + b"</r>" * 300, amplification):
        name = "customXml/broken.xml"
        result = get_filter("docx").parse(_gate_package({name: payload}))
        assert any(
            w["code"] == "docx_unscanned_part" and w["part"] == name
            for w in result.skeleton["file_warnings"]
        )
    for cap in ("_MAX_PART_BYTES", "_MAX_TOTAL_BYTES"):
        with patch.object(docx_resources, cap, 100):
            warnings = docx_resources.external_resource_warnings(_gate_package())
        assert any(w["code"] == "docx_unscanned_part" for w in warnings), cap
    # An external entity containing a fetching field must never be expanded.
    xxe = b'<!DOCTYPE r [<!ENTITY x SYSTEM "https://example.invalid/field.xml">]><r>&x;</r>'
    with patch.object(
        docx_resources.etree, "XMLParser", wraps=docx_resources.etree.XMLParser
    ) as parser:
        assert not docx_resources.external_resource_warnings(
            _gate_package({"customXml/entity.xml": xxe})
        )
    assert all(
        c.kwargs == {"resolve_entities": False, "no_network": True} for c in parser.call_args_list
    )


def test_gate_failed_read_budget():
    from unittest.mock import MagicMock, patch

    from app.filters import docx_resources

    broken_stream = MagicMock()
    broken_stream.__enter__.return_value.read.side_effect = zipfile.BadZipFile("bad CRC")
    pkg = _gate_package({f"customXml/{i}.xml": b"x" * 400 for i in range(3)})
    with (
        patch.object(docx_resources, "_MAX_PART_BYTES", 500),
        patch.object(docx_resources, "_MAX_TOTAL_BYTES", 1000),
        patch.object(zipfile.ZipFile, "open", return_value=broken_stream) as opened,
    ):
        warnings = docx_resources.external_resource_warnings(pkg)
    assert opened.call_count == 2, opened.call_count
    assert all(w["code"] == "docx_unscanned_part" for w in warnings)


def test_gate_plural_unknown():
    f = get_filter("po")
    # #739: am / fil are CLDR locales now (they get a rule); a tag babel cannot parse,
    # or ht (its CLDR rule tests the exponent operand `e`, no gettext form), keeps both
    for locale in ("xx", "tlh", "ht"):
        assert f.build_localized(PO_NO_COMMENT, {}, locale) == f.build(PO_NO_COMMENT, {}), locale


def test_gate_plural_region():
    src = (
        b'msgid ""\nmsgstr "Language: ru\\n"\n\n'
        b'msgid "one"\nmsgid_plural "many"\n'
        b'msgstr[0] "a"\nmsgstr[1] "b"\nmsgstr[2] "c"\n'
    )
    assert b'msgstr[2] "c"' in get_filter("po").build_localized(src, {}, "ru-RU")


def test_po_header_without_language():
    """#688: a header with no Language field gets the target and its Plural-Forms."""
    src = (
        b'msgid ""\nmsgstr ""\n"Content-Type: text/plain; charset=UTF-8\\n"\n'
        b'"Plural-Forms: nplurals=2; plural=(n != 1);\\n"\n\n'
        b'msgid "Open"\nmsgstr ""\n\n'
        b'msgid "%d file"\nmsgid_plural "%d files"\nmsgstr[0] ""\nmsgstr[1] ""\n'
    )
    f = get_filter("po")
    texts = {"0": "Ouvrir", "1": "%d one", "2": "%d many"}
    ru = _po_header(f.build_localized(src, texts, "ru-RU").decode())
    assert "Language: ru_RU\n" in ru, ru
    assert "nplurals=3;" in ru and "nplurals=2;" not in ru, ru
    assert "Content-Type: text/plain; charset=UTF-8\n" in ru, ru
    ja = f.build_localized(src, texts, "ja-JP").decode()
    assert "Language: ja_JP\n" in _po_header(ja) and "nplurals=1;" in ja, ja
    # the #682 one-form mapping now runs: the plural text, one slot
    assert 'msgstr[0] "%d many"' in ja and "msgstr[1]" not in ja, ja
    # unknown locale / no target: declarations unchanged (no Language invented)
    for locale in ("", "tlh"):
        assert f.build_localized(src, texts, locale) == f.build(src, texts), locale
    # an empty header gets both fields
    bare = b'msgid ""\nmsgstr ""\n\nmsgid "Open"\nmsgstr ""\n'
    out = _po_header(f.build_localized(bare, {"0": "Abrir"}, "es-ES").decode())
    assert "Language: es_ES\n" in out and "Plural-Forms: nplurals=2;" in out, out
    # gate: a catalog that already declares the target's rule keeps its human
    # msgstr[2] (a Russian file without Language re-exported into Russian)
    ru_rule = (
        "nplurals=3;plural=n%10==1 && n%100!=11 ? 0 : n%10>=2 && n%10<=4 && "
        "(n%100<10 || n%100>=20) ? 1 : 2;"
    )
    ru_src = (
        'msgid ""\nmsgstr ""\n"Plural-Forms: ' + ru_rule + '\\n"\n\n'
        'msgid "%d file"\nmsgid_plural "%d files"\n'
        'msgstr[0] "a"\nmsgstr[1] "b"\nmsgstr[2] "c"\n'
    ).encode()
    kept = f.build_localized(ru_src, {}, "ru-RU").decode()
    assert "Language: ru_RU\n" in _po_header(kept) and 'msgstr[2] "c"' in kept, kept
    # a two-form (English-rule) file has no slot 2 of the target's: cleared, not invented
    en_src = ru_src.replace(ru_rule.encode(), b"nplurals=2; plural=(n != 1);")
    assert 'msgstr[2] ""' in f.build_localized(en_src, {}, "ru-RU").decode()


def test_po_cldr_only_targets_739():
    """#739: 25 targets missing from babel's legacy gettext table (id, ms, ta, ur, sw, fil,
    mk ...) came out with an empty Language and the SOURCE Plural-Forms, so a one-form
    language (id, ms, my) kept the English two-form rule and the #682 mapping never ran.
    Every locale babel parses gets Language and a rule from its CLDR plural rule, kept
    compact and msgfmt-valid (C operators only); tags babel cannot parse keep both."""
    import gettext

    src = (
        b'msgid ""\nmsgstr ""\n"Language: \\n"\n"Content-Type: text/plain; charset=UTF-8\\n"\n'
        b'"Plural-Forms: nplurals=2; plural=(n != 1);\\n"\n\n'
        b'msgid "Open"\nmsgstr ""\n\n'
        b'msgid "One file"\nmsgid_plural "%d files"\nmsgstr[0] ""\nmsgstr[1] ""\n'
    )
    f = get_filter("po")
    texts = {"0": "Buka", "1": "Satu berkas", "2": "%d berkas"}
    out = f.build_localized(src, texts, "id-ID").decode()
    head = _po_header(out)
    assert "Language: id_ID\n" in head, head
    assert "Plural-Forms: nplurals=1; plural=0;\n" in head and "n != 1" not in head, head
    # the #682 one-form mapping now runs: the plural text, one slot
    assert 'msgstr[0] "%d berkas"' in out and "msgstr[1]" not in out, out
    # the same through the empty-Language path (#688 header without the field)
    no_lang = src.replace(b'"Language: \\n"\n', b"")
    assert "Language: ms_MY\n" in _po_header(f.build_localized(no_lang, texts, "ms-MY").decode())
    want = {
        "id": "nplurals=1; plural=0;",
        "ms": "nplurals=1; plural=0;",
        "my": "nplurals=1; plural=0;",
        "ta": "nplurals=2; plural=(n != 1);",
        "ur": "nplurals=2; plural=(n != 1);",
        "sw": "nplurals=2; plural=(n != 1);",
        "am": "nplurals=2; plural=(n > 1);",
        "zu": "nplurals=2; plural=(n > 1);",
        "mk": "nplurals=2; plural=(n % 10 == 1 && n % 100 != 11 ? 0 : 1);",
    }
    langs = "am az fil gu id kk kn ky mk ml mn mr ms my ne ps si sq sw ta te tl ur uz zu"
    from babel.core import Locale

    for lang in langs.split():
        head = _po_header(f.build_localized(src, texts, lang).decode())
        assert f"Language: {lang}\n" in head, (lang, head)
        rule = re.search(r"(?m)^Plural-Forms: (.*)$", head)[1]
        if lang in want:
            assert rule == want[lang], (lang, rule)
        nplurals, expr = re.fullmatch(r"nplurals=(\d); plural=(.+);", rule).groups()
        assert re.fullmatch(r"[n0-9 ()%!=<>&|?:]+", expr), (lang, expr)  # C operators only
        # same slot as the CLDR rule for every integer count
        cldr, slot = Locale.parse(lang).plural_form, gettext.c2py(expr)
        # gettext slot order is babel's to_gettext order (zero one two few many other);
        # a set has no order, so walk that tuple
        order = ("zero", "one", "two", "few", "many", "other")
        tags = [t for t in order if t in cldr.tags | {"other"}]
        for n in range(0, 1200):
            assert tags[slot(n)] == cldr(n), (lang, n, expr)
        assert int(nplurals) == len(tags), (lang, rule)
    # a tag babel does not know: both declarations kept, nothing invented
    assert f.build_localized(src, texts, "tlh") == f.build(src, texts)


def test_po_plural_slots_filled_729():
    """#729 (decision 18): a plural entry exported into a language with more plural forms
    left every slot past msgstr[1] EMPTY, so the program showed the ENGLISH msgid_plural
    for most counts (Polish 0, 5, 100; Russian 5; Arabic 2 and up), and Arabic got the
    singular in its "zero" slot 0 and the plural in its "one" slot 1. Now every slot of the
    target's rule is filled by the shared plural-fill rule (app.filters.plurals): the
    singular in the slot gettext picks for n = 1, the plural translation everywhere else.
    Checked the way a program reads it: compiled (babel's MO writer and ours) and asked
    through Python gettext for every count 0..100. The Plural-Forms header is the
    target's (#739)."""
    import gettext

    from babel.messages.mofile import write_mo as babel_write_mo
    from babel.messages.pofile import read_po

    from app.filters.mo_filter import write_mo
    from app.filters.po_filter import _parse_entries

    src = (
        b'msgid ""\nmsgstr ""\n"Content-Type: text/plain; charset=UTF-8\\n"\n'
        b'"Language: en\\n"\n"Plural-Forms: nplurals=2; plural=(n != 1);\\n"\n\n'
        b'msgid "%d file was deleted."\nmsgid_plural "%d files were deleted."\n'
        b'msgstr[0] ""\nmsgstr[1] ""\n\n'
        b'msgid "Open"\nmsgstr ""\n'
    )
    one, many = "%d file was deleted.", "%d files were deleted."
    f = get_filter("po")
    cases = {
        "pl-PL": (3, "pl_PL"),
        "ru-RU": (3, "ru_RU"),
        "ar-SA": (6, "ar_SA"),
        "cs-CZ": (3, "cs_CZ"),
        "lv-LV": (3, "lv_LV"),
        "uk-UA": (3, "uk_UA"),
    }
    for locale, (nplurals, lang) in cases.items():
        sing, plur = f"SING-{lang} %d", f"PLUR-{lang} %d"
        out = f.build_localized(src, {"0": sing, "1": plur, "2": "OPEN"}, locale)
        head = _po_header(out.decode())
        assert f"Language: {lang}\n" in head, (locale, head)
        assert f"Plural-Forms: nplurals={nplurals};" in head, (locale, head)
        slots = re.findall(r'msgstr\[(\d)\] "([^"]*)"', out.decode())
        assert len(slots) == nplurals and all(t for _i, t in slots), (locale, slots)
        compiled = [write_mo(_parse_entries(out))]
        bio = io.BytesIO()
        babel_write_mo(bio, read_po(io.BytesIO(out)))
        compiled.append(bio.getvalue())
        for mo in compiled:
            tr = gettext.GNUTranslations(io.BytesIO(mo))
            got = {n: tr.ngettext(one, many, n) for n in range(0, 101)}
            english = [n for n, v in got.items() if "deleted" in v]
            assert not english, (locale, english[:10])
            assert got[1] == sing, (locale, got[1])
            for n in (0, 2, 5, 11, 22, 100):
                want = plur
                if locale in ("ru-RU", "uk-UA", "lv-LV") and n % 10 == 1 and n % 100 != 11:
                    want = sing
                assert got[n] == want, (locale, n, got[n])
    # Arabic slot order: msgstr[0] is "zero" (plural text), msgstr[1] is "one" (singular)
    ar = f.build_localized(src, {"0": "S", "1": "P"}, "ar-SA").decode()
    assert 'msgstr[0] "P"\nmsgstr[1] "S"\nmsgstr[2] "P"' in ar, ar
    # the MO filter rides on the same build
    # (an MO holds translated entries only: an English catalog that maps to itself)
    mo_src = write_mo(
        _parse_entries(src.replace(b'msgstr[0] ""\nmsgstr[1] ""', b'msgstr[0] "a"\nmsgstr[1] "b"'))
    )
    mo_out = get_filter("mo").build_localized(mo_src, {"0": "S %d", "1": "P %d"}, "pl-PL")
    tr = gettext.GNUTranslations(io.BytesIO(mo_out))
    assert [tr.ngettext(one, many, n) for n in (0, 1, 2, 5, 100)] == [
        "P %d",
        "S %d",
        "P %d",
        "P %d",
        "P %d",
    ]
    # a held plural (source fallback) is never written as a translation: its slots stay
    # empty (the program shows the source), the singular still lands in the n = 1 slot
    held = f.build_localized(src, {"0": "S", "1": many}, "pl-PL", fallback_keys={"1"}).decode()
    assert 'msgstr[0] "S"\nmsgstr[1] ""\nmsgstr[2] ""' in held, held
    # the shared rule's slot map (French: 0 and 1 share slot 0; one-form: the plural)
    from app.filters import plurals

    assert plurals.gettext_slot_forms(2, "(n > 1)") == ["one", "other"]
    assert plurals.gettext_slot_forms(1, "0") == ["other"]
    # gate: a catalog already written for the target (Language: ar) keeps the forms its
    # translator entered (Arabic zero in slot 0, two in slot 2); only empty slots are
    # filled. The singular goes to slot 1 (n = 1), the plural to slot 4 (the catch-all).
    own_ar = (
        b'msgid ""\nmsgstr ""\n"Language: ar\\n"\n'
        b'"Plural-Forms: nplurals=6; plural=(n==0 ? 0 : n==1 ? 1 : n==2 ? 2 : '
        b'n%100>=3 && n%100<=10 ? 3 : n%100>=11 ? 4 : 5);\\n"\n\n'
        b'msgid "%d file"\nmsgid_plural "%d files"\nmsgstr[0] "ZERO-OWN"\nmsgstr[1] ""\n'
        b'msgstr[2] "TWO-OWN"\nmsgstr[3] ""\nmsgstr[4] ""\nmsgstr[5] ""\n'
    )
    kept = f.build_localized(own_ar, {"0": "S %d", "1": "P %d"}, "ar").decode()
    got = dict(re.findall(r'msgstr\[(\d)\] "([^"]*)"', kept))
    assert got == {
        "0": "ZERO-OWN",
        "1": "S %d",
        "2": "TWO-OWN",
        "3": "P %d",
        "4": "P %d",
        "5": "P %d",
    }, kept
    # gate: a header entry the export inserts (decision 9, feat/b-intake1) must not
    # break the slot alignment: same slots with or without a header in the source
    bare = b'msgid "%d file"\nmsgid_plural "%d files"\nmsgstr[0] ""\nmsgstr[1] ""\n'
    from babel.core import Locale

    from app.filters.po_filter import _fill_plural_slots, _target_plural

    rule = _target_plural(Locale.parse("pl_PL"))
    with_header = _parse_entries(b'msgid ""\nmsgstr "Language: pl\\n"\n\n' + bare)
    _fill_plural_slots(with_header, _parse_entries(bare), {"0": "S", "1": "P"}, rule, True)
    msgs = [e for e in with_header if e["msgid"]]
    assert [msgs[0][f"msgstr{i}"] for i in range(3)] == ["S", "P", "P"], msgs
    assert plurals.gettext_plural_slot(6, "(n==0 ? 0 : n==1 ? 1 : n==2 ? 2 : 5)") == 5
    assert plurals.gettext_plural_slot(3, rule.plural_expr) == 1
    assert plurals.gettext_plural_slot(1, "0") == 0


def test_po_edge_newlines_712():
    """#712: the engine ended a one-line translation with a newline (soak, real Django po/3
    nl-NL: `Frisian` -> `Fries` + newline); `msgfmt -c` refused the whole catalog. A written
    msgstr begins / ends with a newline exactly when its msgid does, plural slots included."""
    src = (
        b'msgid ""\nmsgstr "Language: en\\n"\n\n'
        b'msgid "Frisian"\nmsgstr ""\n\n'
        b'msgid "\\nUsage: run\\n"\nmsgstr ""\n\n'
        b'msgid "%d file\\n"\nmsgid_plural "%d files\\n"\nmsgstr[0] ""\nmsgstr[1] ""\n\n'
        b'msgid "Line one\\nLine two"\nmsgstr ""\n'
    )
    f = get_filter("po")
    texts = {
        "0": "Fries\n",
        "1": "Gebruik: run",
        "2": "\n%d bestand",
        "3": "%d bestanden",
        "4": "Regel een\nRegel twee",
    }
    for out in (f.build(src, texts).decode(), f.build_localized(src, texts, "nl-NL").decode()):
        assert 'msgid "Frisian"\nmsgstr "Fries"\n' in out, out
        assert 'msgstr "\\nGebruik: run\\n"' in out, out
        assert 'msgstr[0] "%d bestand\\n"' in out and 'msgstr[1] "%d bestanden\\n"' in out, out
        assert 'msgstr "Regel een\\nRegel twee"' in out, out  # inner newline kept
    # the one-form (ja) mapping writes the plural text into msgstr[0] under the same rule
    ja = f.build_localized(src, texts, "ja-JP").decode()
    assert 'msgstr[0] "%d bestanden\\n"' in ja, ja
    # an empty translation stays empty; untouched entries keep their bytes
    assert f.build(src, {"0": "\n"}).decode().count('msgstr ""') == 3
    assert f.build(src, {}) == src


def test_gate_export_errors():
    from types import SimpleNamespace
    from unittest.mock import Mock, patch

    from app.services import export, tm_delivery

    for fmt in ("docx", "xlsx", "xliff", "po", "json"):
        doc = SimpleNamespace(id=123, fmt=fmt)
        filt = Mock(fmt=fmt)
        filt.build_localized.side_effect = ValueError("test rebuild failure")
        with (
            patch.object(tm_delivery, "covering_approval_releases", return_value=False),
            patch.object(export, "paragraph_texts_for", return_value={}),
            patch("app.ports.blobstore.get") as blob,
            patch.object(export, "get_filter", return_value=filt),
            patch.object(export._LOG, "warning") as log,
        ):
            blob.return_value.get.return_value = b"source"
            try:
                export.build_translated_file.__wrapped__(None, doc, "fr-FR")
            except export.ExportUnreadableBlob as exc:
                assert fmt in ("docx", "xlsx", "xliff"), fmt
                assert "target text" not in str(exc)
                assert log.call_args.kwargs["exc_info"] is True
            except ValueError:
                assert fmt in ("po", "json"), fmt
            else:
                raise AssertionError(fmt)


def test_security_regressions():
    failures = []
    for test in (
        test_xml_target_characters,
        test_po_fuzzy_delivery,
        test_po_plural_delivery,
        test_po_no_blank_line_between_entries,
        test_external_resource_warnings,
        test_gate_po_export,
        test_gate_resource_bypasses,
        test_gate_unscanned_parts,
        test_gate_failed_read_budget,
        test_gate_plural_unknown,
        test_gate_plural_region,
        test_po_header_without_language,
        test_po_cldr_only_targets_739,
        test_po_plural_slots_filled_729,
        test_po_edge_newlines_712,
        test_gate_export_errors,
    ):
        try:
            test()
        except Exception as exc:
            failures.append(f"{test.__name__}: {type(exc).__name__}: {exc}")
    assert not failures, "\n".join(failures)


def test_parse_speed_budget():
    """#332: style resolution in parse() was linear per paragraph (python-docx
    re-derived the default style from every w:style element on each lookup), so an
    ordinary 12,000-paragraph manual took ~13 s here and 20-28 s on a loaded box.
    The primary check is deterministic: count DocumentPart.get_style calls (the seam
    behind Paragraph.style). Wall-clock is only a loose backstop, because timing
    flakes when the box is busy."""
    from docx import Document as Docx
    from docx.parts.document import DocumentPart

    from app.filters.docx_filter import DocxFilter

    doc = Docx()
    for i in range(12000):
        doc.add_paragraph(f"Manual paragraph {i}." if i % 5 else "")
    out = io.BytesIO()
    doc.save(out)
    data = out.getvalue()

    # #348: ids are resolved from a dict built ONCE from the styles part, never through
    # DocumentPart.get_style (an XPath with the id inlined). The ratchet therefore counts
    # both seams: get_style must stay at zero calls, the styles part is read once.
    calls = []
    orig = DocumentPart.get_style

    def spy(self, style_id, style_type):
        calls.append(style_id)
        return orig(self, style_id, style_type)

    import app.filters.docx_filter as docx_filter

    passes = []
    orig_names = docx_filter._paragraph_style_names

    def spy_names(d):
        passes.append(1)
        return orig_names(d)

    DocumentPart.get_style = spy
    docx_filter._paragraph_style_names = spy_names
    try:
        start = time.perf_counter()
        units = DocxFilter().parse(data).units
        elapsed = time.perf_counter() - start
    finally:
        DocumentPart.get_style = orig
        docx_filter._paragraph_style_names = orig_names
    assert len(units) == 9600
    assert not calls, f"{len(calls)} per-id get_style lookups (XPath-with-id seam, #348)"
    assert len(passes) == 1, f"styles part walked {len(passes)} times (want once per document)"
    assert elapsed < 15.0, (
        f"ordinary 12,000-paragraph DOCX parsed in {elapsed:.1f} s (backstop 15 s)"
    )
    print(f"  13)  12k parse: {len(calls)} get_style, {len(passes)} styles pass, {elapsed:.2f} s")


def test_no_default_style_parses():
    """#332 self-review: python-docx's get_style() returns None when the styles part
    defines no default paragraph style (no w:style with w:default="1"), and a dangling
    w:pStyle falls through to the same default. The pre-fix code mapped that to style
    ""; the style cache must keep that instead of crashing on .name (a 500 on upload)."""
    from docx import Document as Docx
    from docx.oxml.ns import qn

    from app.filters.docx_filter import DocxFilter

    doc = Docx()
    doc.add_paragraph("No default style paragraph")
    p = doc.add_paragraph("Dangling style id")
    p._p.get_or_add_pPr().append(p._p.makeelement(qn("w:pStyle"), {qn("w:val"): "DoesNotExist1"}))
    for st in doc.styles.element.iter(qn("w:style")):
        st.attrib.pop(qn("w:default"), None)
    out = io.BytesIO()
    doc.save(out)
    units = DocxFilter().parse(out.getvalue()).units
    assert [u.style for u in units] == ["", ""], [u.style for u in units]
    print('  14)  no default paragraph style (and dangling w:pStyle) parse with style ""')


def test_style_names_exact():
    """#332 fix round 1: pin the resolved style name of every paragraph, so a wrong
    cache key (e.g. everything getting the default style) cannot ship green."""
    from docx import Document as Docx
    from docx.enum.style import WD_STYLE_TYPE
    from docx.oxml.ns import qn

    from app.filters.docx_filter import DocxFilter

    doc = Docx()
    doc.styles.add_style("Fancy Custom", WD_STYLE_TYPE.PARAGRAPH)

    def dangling(par):
        par._p.get_or_add_pPr().append(
            par._p.makeelement(qn("w:pStyle"), {qn("w:val"): "DoesNotExist1"})
        )
        return par

    doc.add_paragraph("plain one")
    doc.add_paragraph("head one", style="Heading 1")
    table = doc.add_table(rows=1, cols=1)
    table.cell(0, 0).paragraphs[0].text = "cell title"
    table.cell(0, 0).paragraphs[0].style = doc.styles["Title"]
    doc.add_paragraph("custom", style="Fancy Custom")
    dangling(doc.add_paragraph("dangling"))
    doc.add_paragraph("head two", style="Heading 1")
    doc.add_paragraph("plain two")
    out = io.BytesIO()
    doc.save(out)
    units = DocxFilter().parse(out.getvalue()).units
    got = [(u.text, u.style) for u in units]
    want = [
        ("plain one", "Normal"),
        ("head one", "Heading 1"),
        ("cell title", "Title"),
        ("custom", "Fancy Custom"),
        ("dangling", "Normal"),
        ("head two", "Heading 1"),
        ("plain two", "Normal"),
    ]
    # Walk 3 numbers in reading order (#355), so the table cell sits between the
    # paragraphs around it: compare the exact order.
    assert got == want, got
    print("  15)  exact per-paragraph style names survive the style cache")


def test_quote_in_style_id_parses():
    """#348: python-docx resolves a w:pStyle by building an XPath with the id inlined,
    so a style id containing a double quote raised XPathEvalError and failed the whole
    import (500). The fix looks the id up in a dict built from the styles part; a
    dangling quoted id falls back to the document default, and the other ids are
    unaffected."""
    from docx import Document as Docx
    from docx.oxml.ns import qn

    from app.filters.docx_filter import DocxFilter

    doc = Docx()
    quoted = doc.styles.element.makeelement(
        qn("w:style"), {qn("w:type"): "paragraph", qn("w:styleId"): 'Bad"Id'}
    )
    quoted.append(doc.styles.element.makeelement(qn("w:name"), {qn("w:val"): "Quoted Style"}))
    doc.styles.element.append(quoted)

    def set_pstyle(par, val):
        par._p.get_or_add_pPr().append(par._p.makeelement(qn("w:pStyle"), {qn("w:val"): val}))
        return par

    doc.add_paragraph("plain one")
    doc.add_paragraph("head one", style="Heading 1")
    set_pstyle(doc.add_paragraph("quoted id"), 'Bad"Id')
    set_pstyle(doc.add_paragraph("dangling quote"), 'Dang"ling')
    doc.add_paragraph("head two", style="Heading 1")
    out = io.BytesIO()
    doc.save(out)
    units = DocxFilter().parse(out.getvalue()).units
    got = {(u.text, u.style) for u in units}
    assert got == {
        ("plain one", "Normal"),
        ("head one", "Heading 1"),
        ("quoted id", "Quoted Style"),
        ("dangling quote", "Normal"),
        ("head two", "Heading 1"),
    }, got
    print("  16)  a double quote in a style id parses; other ids unchanged")


def test_malformed_style_entry_parses():
    """#348 self-review: _paragraph_style_names reads every w:style element up front,
    so a value python-docx cannot convert (w:type="bogus" -> ValueError,
    w:default="2" -> docx.exceptions.InvalidXmlError) raised out of parse and 500'd
    the import; a nameless w:style could also leak style=None into the units. Each
    attribute is now read on its own (an unconvertible value means "not paragraph" or
    "not default", never a crash), a nameless style maps to "" like the pre-#332
    behaviour, and the other ids are unaffected."""
    from docx import Document as Docx
    from docx.oxml.ns import qn

    from app.filters.docx_filter import DocxFilter

    doc = Docx()

    def set_pstyle(par, val):
        par._p.get_or_add_pPr().append(par._p.makeelement(qn("w:pStyle"), {qn("w:val"): val}))
        return par

    # The paragraphs must be added BEFORE the hostile entries land in the styles
    # part: python-docx walks every w:type itself while resolving a style by name.
    doc.add_paragraph("head one", style="Heading 1")
    set_pstyle(doc.add_paragraph("dangling"), "DoesNotExist9")
    doc.add_paragraph("head two", style="Heading 1")
    styles_el = doc.styles.element
    for type_val, style_id, attrs in (
        ("bogus", "Bogus", {}),
        ("paragraph", "BadDefault", {qn("w:default"): "2"}),
    ):
        el = styles_el.makeelement(
            qn("w:style"), {qn("w:type"): type_val, qn("w:styleId"): style_id, **attrs}
        )
        el.append(styles_el.makeelement(qn("w:name"), {qn("w:val"): style_id}))
        styles_el.append(el)
    # A default paragraph style with no w:name, appended last so it wins the
    # last-default tie-break: a dangling w:pStyle must fall back to it as "" (not
    # None).
    nameless = styles_el.makeelement(
        qn("w:style"),
        {qn("w:type"): "paragraph", qn("w:styleId"): "Nameless", qn("w:default"): "1"},
    )
    styles_el.append(nameless)
    out = io.BytesIO()
    doc.save(out)
    units = DocxFilter().parse(out.getvalue()).units
    got = [(u.text, u.style) for u in units]
    want = [
        ("head one", "Heading 1"),
        ("dangling", ""),
        ("head two", "Heading 1"),
    ]
    assert got == want, got
    assert all(isinstance(u.style, str) for u in units)

    # A style that exists but has no w:name maps to "" for its own paragraphs too.
    doc2 = Docx()
    unnamed = doc2.styles.element.makeelement(
        qn("w:style"), {qn("w:type"): "paragraph", qn("w:styleId"): "Unnamed"}
    )
    doc2.styles.element.append(unnamed)
    doc2.add_paragraph("named heading", style="Heading 1")
    set_pstyle(doc2.add_paragraph("unnamed style"), "Unnamed")
    out2 = io.BytesIO()
    doc2.save(out2)
    units2 = DocxFilter().parse(out2.getvalue()).units
    got2 = [(u.text, u.style) for u in units2]
    want2 = [("named heading", "Heading 1"), ("unnamed style", "")]
    assert got2 == want2, got2
    print("  17)  unconvertible w:type/w:default and a nameless style parse; no None styles")


def _vmerge_docx(rows: int, merged_text: str = "Merged label") -> bytes:
    """A two-column table whose first column is ONE vertical merge down every row
    (w:vMerge restart on row 0, bare w:vMerge continuation below); column two holds
    a distinct text per row."""
    from docx import Document as Docx
    from docx.oxml.ns import qn

    doc = Docx()
    table = doc.add_table(rows=rows, cols=2)
    for i, tr in enumerate(table._tbl.tr_lst):
        left = tr.tc_lst[0]
        tc_pr = left.get_or_add_tcPr()
        vm = tc_pr.makeelement(qn("w:vMerge"), {qn("w:val"): "restart"} if i == 0 else {})
        tc_pr.append(vm)
        if i == 0:
            run = left.makeelement(qn("w:r"), {})
            t = run.makeelement(qn("w:t"), {})
            t.text = merged_text
            run.append(t)
            left.p_lst[0].append(run)
        tr.tc_lst[1].p_lst[0].append(_text_run(f"Row {i} value"))
    out = io.BytesIO()
    doc.save(out)
    return out.getvalue()


def _text_run(text: str):
    from docx.oxml.ns import qn
    from lxml import etree as _et

    run = _et.SubElement(_et.Element("holder"), qn("w:r"))
    t = _et.SubElement(run, qn("w:t"))
    t.text = text
    return run


VMERGE_ROWS = 2000


def test_vmerge_parse_bound():
    """#349: python-docx Row.cells expands a vertical merge by walking up the column
    for each spanned row, so 500 rows took ~16 s and ~1,100 rows overflowed the stack.
    The walk is now XML-level. Deterministic check: count _Cell constructions (one per
    physical w:tc visited) and the paragraphs handed on; wall-clock is a loose
    backstop only."""
    from docx import Document as Docx
    from docx.table import _Cell

    from app.filters import docx_filter
    from app.filters.docx_filter import DocxFilter

    data = _vmerge_docx(VMERGE_ROWS)
    cells = []
    orig = _Cell.__init__

    def spy(self, tc, parent):
        cells.append(tc)
        return orig(self, tc, parent)

    _Cell.__init__ = spy
    try:
        start = time.perf_counter()
        paras = docx_filter._all_paragraphs(Docx(io.BytesIO(data)))
        walk = time.perf_counter() - start
        walk_cells = len(cells)
        start = time.perf_counter()
        units = DocxFilter().parse(data).units
        elapsed = time.perf_counter() - start
    finally:
        _Cell.__init__ = orig
    physical = VMERGE_ROWS * 2
    assert walk_cells <= physical, f"{walk_cells} cell visits for {physical} physical cells"
    assert len(cells) <= 2 * physical, f"{len(cells)} cell visits across two walks"
    assert len(paras) <= physical, f"{len(paras)} paragraphs for {physical} physical cells"
    assert len(units) == VMERGE_ROWS + 1, len(units)
    assert elapsed < 10.0, f"{VMERGE_ROWS}-row vMerge parse took {elapsed:.1f} s (backstop 10 s)"
    print(
        f"  16)  {VMERGE_ROWS}-row vMerge: {walk_cells} cell visits for {physical} cells, "
        f"{len(paras)} paragraphs, walk {walk:.2f} s, parse {elapsed:.2f} s"
    )


def test_vmerge_text_once():
    from docx import Document as Docx

    from app.filters.docx_filter import DocxFilter

    rows = 60
    units = DocxFilter().parse(_vmerge_docx(rows)).units
    texts = [u.text for u in units]
    assert texts.count("Merged label") == 1, texts.count("Merged label")
    assert len(texts) == rows + 1, len(texts)
    assert texts[0] == "Merged label", texts[:3]
    assert texts[1:] == [f"Row {i} value" for i in range(rows)], texts[1:4]
    # Smaller merge: three rows merged, one row below it, in the middle of a table.
    doc = Docx()
    t = doc.add_table(rows=4, cols=2)
    for r in range(4):
        t.cell(r, 1).text = f"R{r}"
    t.cell(0, 0).merge(t.cell(2, 0)).text = "Top three"
    t.cell(3, 0).text = "Bottom"
    out = io.BytesIO()
    doc.save(out)
    got = [u.text for u in DocxFilter().parse(out.getvalue()).units]
    assert got == ["Top three", "R0", "R1", "R2", "Bottom", "R3"], got
    print("  17b) merged cell text yielded once (large column and a mid-table merge)")


def test_vmerge_export_round_trip():
    from docx import Document as Docx

    from app.filters.docx_filter import DocxFilter

    rows = 80
    data = _vmerge_docx(rows)
    f = DocxFilter()
    units = f.parse(data).units
    # One translatable unit for the merged cell (not one per spanned row).
    assert [u.text for u in units].count("Merged label") == 1, len(units)
    assert len(units) == rows + 1, len(units)
    texts = {str(u.p_index): "TRANSLATED " + u.text for u in units}
    for label, out in (
        ("build", f.build(data, texts)),
        ("build_localized", f.build_localized(data, texts, "de-DE")),
    ):
        doc = Docx(io.BytesIO(out))
        tbl = doc.tables[0]
        marks = []
        for tr in tbl._tbl.tr_lst:
            vm = tr.tc_lst[0].tcPr.find(W("w", "vMerge"))
            marks.append(None if vm is None else vm.get(W("w", "val"), "continue"))
        assert marks[0] == "restart" and set(marks[1:]) == {"continue"}, marks[:3]
        assert tbl.cell(0, 0).text == "TRANSLATED Merged label", tbl.cell(0, 0).text
        assert tbl.cell(rows - 1, 0).text == "TRANSLATED Merged label"  # python-docx follows it
        assert tbl.cell(5, 1).text == "TRANSLATED Row 5 value", tbl.cell(5, 1).text
        xml = zipfile.ZipFile(io.BytesIO(out)).read("word/document.xml").decode("utf-8")
        assert xml.count("TRANSLATED Merged label") == 1, xml.count("TRANSLATED Merged label")
        assert "Merged label" not in xml.replace("TRANSLATED Merged label", ""), label
        again = f.parse(out).units
        assert [u.text for u in again] == ["TRANSLATED " + u.text for u in units], label
    print("  18)  export keeps the translation in the merged cell and the vMerge structure")


def _old_walk(doc) -> list:
    """The pre-#349 walker, the reference for documents without merged cells."""
    paras = list(doc.paragraphs)
    for table in doc.tables:
        for row in table.rows:
            for cell in row.cells:
                paras.extend(cell.paragraphs)
    return paras


def test_table_walk_unchanged():
    from docx import Document as Docx
    from docx.opc.constants import RELATIONSHIP_TYPE as RT
    from docx.oxml.ns import qn

    from app.filters import docx_filter
    from app.filters.docx_filter import DocxFilter

    doc = Docx()
    doc.add_paragraph("Body A")
    t = doc.add_table(rows=3, cols=3)
    for r in range(3):
        for c in range(3):
            t.cell(r, c).text = f"plain {r}{c}"
    t.cell(1, 1).paragraphs[0].add_run(" bold").bold = True
    t2 = doc.add_table(rows=2, cols=2)
    t2.cell(0, 0).text = "outer00"
    inner = t2.cell(0, 1).add_table(rows=2, cols=2)
    inner.cell(0, 0).text = "inner00"
    t2.cell(1, 0).text = "outer10"
    t2.cell(1, 1).text = "outer11"
    out = io.BytesIO()
    doc.save(out)
    reloaded = Docx(io.BytesIO(out.getvalue()))
    new = [p._p for p in docx_filter._all_paragraphs(reloaded, 2)]
    old = [p._p for p in _old_walk(reloaded)]
    assert new == old, "walk order changed for a document without merged cells"
    texts = [u.text for u in DocxFilter().parse(out.getvalue()).units]
    assert "outer11" in texts and texts.count("inner00") == 1, texts  # nested text: walk 3 (#355)
    # Hyperlink inside a cell: still found, once.
    doc = Docx()
    t = doc.add_table(rows=1, cols=2)
    par = t.cell(0, 0).paragraphs[0]
    rid = doc.part.relate_to("https://example.com", RT.HYPERLINK, is_external=True)
    h = par._p.makeelement(qn("w:hyperlink"), {qn("r:id"): rid})
    h.append(_text_run("link text"))
    par._p.append(h)
    t.cell(0, 1).text = "other"
    buf = io.BytesIO()
    doc.save(buf)
    got = [u.text for u in DocxFilter().parse(buf.getvalue()).units]
    assert got == ["link text", "other"], got
    # gridSpan: one physical cell spanning three grid columns is visited once.
    doc = Docx()
    t = doc.add_table(rows=2, cols=3)
    t.cell(0, 0).merge(t.cell(0, 2)).text = "wide header"
    for c, v in enumerate("abc"):
        t.cell(1, c).text = v
    buf = io.BytesIO()
    doc.save(buf)
    got = [u.text for u in DocxFilter().parse(buf.getvalue()).units]
    assert got == ["wide header", "a", "b", "c"], got
    f = DocxFilter()
    rebuilt = f.build(buf.getvalue(), {"0": "HEADER", "2": "B"})
    re_texts = [u.text for u in f.parse(rebuilt).units]
    assert re_texts == ["HEADER", "a", "B", "c"], re_texts
    print("  19)  gridSpan visited once; plain/nested/hyperlink/multi-run walk unchanged")


def _tiny_png() -> bytes:
    import struct
    import zlib

    def chunk(tag: bytes, body: bytes) -> bytes:
        crc = zlib.crc32(tag + body) & 0xFFFFFFFF
        return struct.pack(">I", len(body)) + tag + body + struct.pack(">I", crc)

    raw = b"\x00\xff\x00\x00"
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(raw))
        + chunk(b"IEND", b"")
    )


_WP_DOCPR = "{http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing}docPr"
_DOCX_CTYPE = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
_LEGACY_WANT = [
    ["DE Wide header"],
    ["DE Group A", "DE R1C1", "DE R1C2"],
    ["", "DE R2C1", "DE R2C2"],
    ["DE R3C0", "DE R3C1", "DE R3C2"],
]


def _legacy_merge_docx() -> bytes:
    """Intro; a 4x3 table (row 0 = one gridSpan=3 header, column 0 rows 1-2 = one
    vMerge, row 3 plain); Outro; a picture with alt text."""
    from docx import Document as Docx
    from docx.shared import Inches

    doc = Docx()
    doc.add_paragraph("Intro paragraph")
    t = doc.add_table(rows=4, cols=3)
    for r in range(4):
        for c in range(3):
            t.cell(r, c).text = f"R{r}C{c}"
    t.cell(0, 0).merge(t.cell(0, 2)).text = "Wide header"
    t.cell(1, 0).merge(t.cell(2, 0)).text = "Group A"
    doc.add_paragraph("Outro paragraph")
    doc.add_picture(io.BytesIO(_tiny_png()), width=Inches(1))
    for dp in doc.element.iter(_WP_DOCPR):
        dp.set("descr", "Company logo")
    out = io.BytesIO()
    doc.save(out)
    return out.getvalue()


def _tc_texts(data: bytes) -> list[list[str]]:
    from docx import Document as Docx

    tbl = Docx(io.BytesIO(data)).tables[0]
    rows = []
    for tr in tbl._tbl.tr_lst:
        rows.append(["".join(t.text or "" for t in tc.iter(W("w", "t"))) for tc in tr.tc_lst])
    return rows


def _alt_descrs(data: bytes) -> list[str]:
    from docx import Document as Docx

    return [dp.get("descr") for dp in Docx(io.BytesIO(data)).element.iter(_WP_DOCPR)]


def _legacy_skeleton(data: bytes) -> dict:
    """The skeleton main stored (pre-#349): units numbered by the row.cells walk, no
    walker stamp. Built here independently of the filter's own legacy helper."""
    from docx import Document as Docx

    from app.filters import docx_filter

    doc = Docx(io.BytesIO(data))
    paras = _old_walk(doc)
    units = [
        {"p_index": i, "text": p.text, "style": "Normal"} for i, p in enumerate(paras) if p.text
    ]
    for j, (_dp, _attr, value, style, _part) in enumerate(docx_filter._alt_units(doc)):
        units.append({"p_index": len(paras) + j, "text": value, "style": style})
    return {"format": "docx", "units": units}


def test_legacy_skeleton_export():
    """#349 gate finding 1: a project stored under the old row.cells numbering must
    export with that numbering, not the new XML walk's."""
    from docx import Document as Docx

    from app.filters.docx_filter import DocxFilter, walk_is_legacy, walk_version

    data = _legacy_merge_docx()
    skel = _legacy_skeleton(data)
    assert walk_is_legacy(skel), skel
    texts = [u["text"] for u in skel["units"]]
    assert texts.count("Wide header") == 3 and texts.count("Group A") == 2, texts
    mapping = {str(u["p_index"]): "DE " + u["text"] for u in skel["units"]}
    f = DocxFilter()
    walk = walk_version(skel)
    for label, out in (
        ("build", f.build(data, mapping, walk=walk)),
        ("build_localized", f.build_localized(data, mapping, "de-DE", walk=walk)),
    ):
        assert _tc_texts(out) == _LEGACY_WANT, (label, _tc_texts(out))
        assert _alt_descrs(out) == ["DE Company logo"], (label, _alt_descrs(out))
        body = [p.text for p in Docx(io.BytesIO(out)).paragraphs]
        assert "DE Intro paragraph" in body and "DE Outro paragraph" in body, body
    # What the NEW walk does with the same stored skeleton (the gate's bug): wrong.
    wrong = f.build(data, mapping)
    assert _tc_texts(wrong) != _LEGACY_WANT, "new walk must differ here, or this proves nothing"
    print("  20)  legacy-numbered skeleton exports into the right cells, alt text intact")


def _new_docx_doc(client, name: str, data: bytes, comments: bool = False) -> int:
    """Upload `data` into a new project. A project created now starts with Word comments
    OFF (#690); `comments=True` switches the project's option on before the upload."""
    r = client.post(
        "/projects",
        data={
            "name": name,
            "source_locale": "en-US",
            "target_locales": "de-DE",
            "default_provider": "claude",
        },
        follow_redirects=False,
    )
    pid = int(r.headers["location"].rsplit("/", 1)[-1])
    if comments:
        from app.services import comment_option

        comment_option.set_for(pid, True)
    r = client.post(
        f"/projects/{pid}/documents",
        data={"mode": "segmented"},
        files={"file": (name + ".docx", data, _DOCX_CTYPE)},
        follow_redirects=False,
    )
    assert r.status_code == 303, r.text
    return int(r.headers["location"].rsplit("/", 1)[-1])


def test_walk_stamp_and_selection():
    """A new import is stamped 2 and round-trips; a stamped skeleton never takes the
    legacy walk; an unstamped one always does (export)."""
    from unittest.mock import patch

    from app.filters import docx_filter
    from app.filters.docx_filter import DocxFilter, walk_is_legacy
    from app.models import Document

    data = _legacy_merge_docx()
    res = DocxFilter().parse(data)
    assert res.skeleton["docx_walk"] == 6, res.skeleton
    assert not walk_is_legacy(res.skeleton)
    assert [u.text for u in res.units].count("Wide header") == 1
    mapping = {str(u.p_index): "DE " + u.text for u in res.units}
    out = DocxFilter().build(data, mapping)
    assert _tc_texts(out) == _LEGACY_WANT, _tc_texts(out)
    assert _alt_descrs(out) == ["DE Company logo"]

    client = TestClient(app)
    doc_id = _new_docx_doc(client, "walkstamp", data)
    with Session(engine) as s:
        assert s.get(Document, doc_id).skeleton["docx_walk"] == 6
        segs = s.exec(
            select(Segment).where(Segment.document_id == doc_id, Segment.target_locale == "de-DE")
        ).all()
        rows = [(x.id, x.source_text) for x in segs]
    for sid, text in rows:
        r = client.post(f"/segments/{sid}/save", data={"target_text": "DE " + text, "tte_ms": "0"})
        assert r.status_code == 200, r.text
    with patch.object(docx_filter, "_legacy_paragraphs", side_effect=AssertionError("legacy")):
        resp = client.get(f"/documents/{doc_id}/export?locale=de-DE")
    assert resp.status_code == 200, resp.text[:300]
    assert _tc_texts(resp.content) == _LEGACY_WANT, _tc_texts(resp.content)
    assert _alt_descrs(resp.content) == ["DE Company logo"]
    # Drop the stamp (a project stored before #349): export must take the legacy walk.
    with Session(engine) as s:
        d = s.get(Document, doc_id)
        sk = dict(d.skeleton)
        sk.pop("docx_walk")
        d.skeleton = sk
        s.add(d)
        s.commit()
    calls = []
    real = docx_filter._legacy_paragraphs
    with patch.object(
        docx_filter, "_legacy_paragraphs", side_effect=lambda doc: calls.append(1) or real(doc)
    ):
        resp = client.get(f"/documents/{doc_id}/export?locale=de-DE")
    assert resp.status_code == 200 and calls, (resp.status_code, calls)
    print("  21)  new import stamped 2 and round-trips; the stamp selects the walker on export")


def test_reingest_restamps():
    """The repair re-ingest re-walks with the current walk, so it must move the stored
    unit numbering and the stamp with it (never new segments over old numbering)."""
    from app.filters.docx_filter import walk_is_legacy
    from app.models import Document
    from app.services import ingest

    data = _legacy_merge_docx()
    doc_id = _new_docx_doc(TestClient(app), "reingest", data)
    with Session(engine) as s:
        d = s.get(Document, doc_id)
        d.skeleton = _legacy_skeleton(data)
        s.add(d)
        s.commit()
    assert ingest.reingest_document(doc_id) is True
    with Session(engine) as s:
        d = s.get(Document, doc_id)
        assert not walk_is_legacy(d.skeleton), d.skeleton.get("docx_walk")
        assert [u["text"] for u in d.skeleton["units"]].count("Wide header") == 1
    print("  22)  re-ingest moves a legacy project to the current numbering and stamp")


def test_unknown_walk_stamp_refuses():
    """#349 gate r2 note 1: only a missing stamp is legacy and only the current stamp is the
    current walk; any other value refuses instead of guessing a walk (a guess exports into
    the wrong cells)."""
    from app.filters.docx_filter import DOCX_WALK_VERSION, walk_is_legacy

    assert walk_is_legacy({}) is True
    assert walk_is_legacy(None) is True
    assert walk_is_legacy({"docx_walk": DOCX_WALK_VERSION}) is False
    for bad in (7, 1, 2.0, True, "3", None, 0):
        try:
            walk_is_legacy({"docx_walk": bad})
        except ValueError as exc:
            assert "Re-import" in str(exc), exc
        else:
            raise AssertionError(f"stamp {bad!r} must refuse, not guess a walk")
    print("  23)  an unknown table-walk stamp refuses instead of guessing a walk")


def test_hostile_style_ids_and_first_wins():
    """#348 code review: every hostile styleId/pStyle shape parses (quote, apostrophe,
    both, backslash, bracket, 50k chars, unicode), in body paragraphs AND table cells,
    exports in both walks, and the id lookup keeps python-docx's semantics: the FIRST
    w:style owning an id wins whatever its type, a pStyle on a non-paragraph style falls
    back to the default, and an unconvertible w:default does not hide a style's name."""
    from docx import Document as Docx
    from docx.oxml.ns import qn

    from app.filters.docx_filter import DocxFilter

    bs = chr(92)
    ids = {
        "dq": 'a"b',
        "sq": "a'b",
        "both": "a\"b'c",
        "bs": "a" + bs + "b",
        "br": "a]b[c",
        "long": "x" * 50000,
        "uni": "é日本",
        "xp": 'x" or "1"="1',
    }
    doc = Docx()
    el = doc.styles.element

    def add_style(sid, typ, name, default=None):
        attrs = {qn("w:styleId"): sid}
        if typ is not None:
            attrs[qn("w:type")] = typ
        if default is not None:
            attrs[qn("w:default")] = default
        st = el.makeelement(qn("w:style"), attrs)
        if name is not None:
            st.append(el.makeelement(qn("w:name"), {qn("w:val"): name}))
        el.append(st)

    def set_pstyle(par, val):
        par._p.get_or_add_pPr().append(par._p.makeelement(qn("w:pStyle"), {qn("w:val"): val}))
        return par

    for key, sid in ids.items():
        add_style(sid, "paragraph", "Name " + key)
    add_style("Dup", "paragraph", "First wins")
    add_style("Dup", "paragraph", "Second loses")
    add_style("CharFirst", "character", "Char owns it")
    add_style("CharFirst", "paragraph", "Para later")
    add_style("BadDef", "paragraph", "Kept name", default="2")
    # gate finding 1: an UNUSED style whose <w:name/> has no w:val (docx.oxml.exceptions
    # InvalidXmlError, a different class from docx.exceptions') must not 500 the import
    noval = el.makeelement(qn("w:style"), {qn("w:type"): "paragraph", qn("w:styleId"): "NoVal"})
    noval.append(el.makeelement(qn("w:name"), {}))
    el.append(noval)
    # gate finding 2: only DIRECT w:style children are document styles (python-docx looks
    # no deeper); one nested in mc:AlternateContent must not resolve
    alt = el.makeelement(
        "{http://schemas.openxmlformats.org/markup-compatibility/2006}AlternateContent", {}
    )
    nested = el.makeelement(qn("w:style"), {qn("w:type"): "paragraph", qn("w:styleId"): "Nested"})
    nested.append(el.makeelement(qn("w:name"), {qn("w:val"): "Nested name"}))
    alt.append(nested)
    el.append(alt)
    for key, sid in ids.items():
        set_pstyle(doc.add_paragraph("body " + key), sid)
    tbl = doc.add_table(rows=1, cols=1)
    set_pstyle(tbl.cell(0, 0).paragraphs[0], ids["dq"])
    tbl.cell(0, 0).paragraphs[0].add_run("cell dq")
    set_pstyle(doc.add_paragraph("dup"), "Dup")
    set_pstyle(doc.add_paragraph("charfirst"), "CharFirst")
    set_pstyle(doc.add_paragraph("baddef"), "BadDef")
    set_pstyle(doc.add_paragraph("dangling"), 'Nope"Nope' + bs)
    set_pstyle(doc.add_paragraph("empty"), "")
    set_pstyle(doc.add_paragraph("nested"), "Nested")
    out = io.BytesIO()
    doc.save(out)
    data = out.getvalue()
    f = DocxFilter()
    units = f.parse(data).units
    got = {u.text: u.style for u in units}
    for key in ids:
        assert got["body " + key] == "Name " + key, (key, got["body " + key])
    assert got["cell dq"] == "Name dq", got
    assert got["dup"] == "First wins", got["dup"]
    assert got["charfirst"] == "Normal", got["charfirst"]
    assert got["baddef"] == "Kept name", got["baddef"]
    assert got["dangling"] == "Normal" and got["empty"] == "Normal", got
    assert got["nested"] == "Normal", got["nested"]
    assert all(isinstance(u.style, str) for u in units)
    texts = {str(u.p_index): "T " + u.text for u in units}
    for walk in (1, 2, 3, 4):
        built = f.build(data, texts, walk=walk)
        assert next(u.text for u in f.parse(built).units).startswith("T "), walk
    print("  24)  hostile style ids (quote, backslash, long, unicode) parse; first id wins")


# --- #355: nested tables and w:sdt / w:customXml wrappers -------------------------------------

_WNS = 'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"'


def _wp(text: str) -> str:
    return f"<w:p><w:r><w:t>{text}</w:t></w:r></w:p>"


def _wtc(inner: str, props: str = "") -> str:
    return f"<w:tc>{props}{inner}</w:tc>"


def _wtr(*cells: str) -> str:
    return "<w:tr>" + "".join(cells) + "</w:tr>"


def _wtbl(*rows: str) -> str:
    return "<w:tbl><w:tblPr/><w:tblGrid/>" + "".join(rows) + "</w:tbl>"


def _wsdt(inner: str) -> str:
    return f"<w:sdt><w:sdtPr/><w:sdtContent>{inner}</w:sdtContent></w:sdt>"


def _wcx(inner: str) -> str:
    return f'<w:customXml w:uri="urn:x" w:element="x">{inner}</w:customXml>'


def _docx_with_body(*blocks: str) -> bytes:
    """A .docx whose body is exactly `blocks` (raw WordprocessingML) plus the sectPr."""
    from docx import Document as Docx
    from lxml import etree

    doc = Docx()
    body = doc.element.body
    sect = body.find(W("w", "sectPr"))
    for child in list(body):
        if child is not sect:
            body.remove(child)
    for blk in blocks:
        el = etree.fromstring(f"<root {_NSDECL}>{blk}</root>")
        for child in list(el):
            if sect is not None:
                sect.addprevious(child)
            else:
                body.append(child)
    out = io.BytesIO()
    doc.save(out)
    return out.getvalue()


def _all_w_texts(data: bytes) -> list[str]:
    """Every non-empty paragraph's text anywhere in the body, in document order."""
    from docx import Document as Docx

    root = Docx(io.BytesIO(data)).element.body
    res = []
    for p in root.iter(W("w", "p")):
        t = "".join(x.text or "" for x in p.iter(W("w", "t")))
        if t:
            res.append(t)
    return res


def _census(data: bytes) -> list[int]:
    from docx import Document as Docx

    r = Docx(io.BytesIO(data)).element.body
    return [len(list(r.iter(W("w", t)))) for t in ("sdt", "customXml", "tbl", "tr", "tc")]


def _translate_all(data: bytes, prefix: str = "DE "):
    from app.filters.docx_filter import DocxFilter

    f = DocxFilter()
    res = f.parse(data)
    mapping = {str(u.p_index): prefix + u.text for u in res.units}
    return res, f.build(data, mapping), f.build_localized(data, mapping, "de-DE")


def test_nested_table_extracted():
    from app.filters.docx_filter import DocxFilter

    nested = _wtbl(
        _wtr(_wtc(_wp("in A1")), _wtc(_wp("in B1"))),
        _wtr(_wtc(_wp("in A2")), _wtc(_wp("in B2"))),
    )
    data = _docx_with_body(
        _wp("Body"),
        _wtbl(_wtr(_wtc(_wp("outer A") + nested + _wp("outer tail")), _wtc(_wp("outer B")))),
    )
    res, built, built_loc = _translate_all(data)
    assert res.skeleton["docx_walk"] == 6, res.skeleton
    texts = [u.text for u in res.units]
    # body, then the outer table in reading order: the nested table sits inside cell A,
    # between "outer A" and "outer tail", never after the sibling cells (#355 gate)
    want = ["Body", "outer A", "in A1", "in B1", "in A2", "in B2", "outer tail", "outer B"]
    assert texts == want, texts
    for out in (built, built_loc):
        got = _all_w_texts(out)
        assert got == [
            "DE Body",
            "DE outer A",
            "DE in A1",
            "DE in B1",
            "DE in A2",
            "DE in B2",
            "DE outer tail",
            "DE outer B",
        ], got
        assert [u.text for u in DocxFilter().parse(out).units] == ["DE " + t for t in texts]
    # a table in a table in a table, five levels: every level extracted exactly once
    inner = _wp("L5")
    for lvl in (4, 3, 2, 1):
        inner = _wtbl(_wtr(_wtc(_wp(f"L{lvl}") + inner)))
    data = _docx_with_body(inner)
    res, built, _ = _translate_all(data)
    got = sorted(u.text for u in res.units)
    assert got == ["L1", "L2", "L3", "L4", "L5"], got
    assert sorted(_all_w_texts(built)) == sorted(f"DE L{i}" for i in range(1, 6))
    print("  25)  nested table text (two shapes, five levels deep) extracted and written back once")


def test_sdt_customxml_wrappers():
    from app.filters.docx_filter import DocxFilter

    nested_cell = _wtbl(
        _wtr(_wsdt(_wtc(_wp("nested sdt cell")))), _wsdt(_wtr(_wtc(_wp("nested sdt row"))))
    )
    stacked = _wsdt(_wcx(_wsdt(_wtbl(_wtr(_wsdt(_wcx(_wtc(_wp("deep wrapped")))))))))
    cases = {
        "sdt row": _wtbl(
            _wtr(_wtc(_wp("r0"))), _wsdt(_wtr(_wtc(_wp("sdt row c1")), _wtc(_wp("sdt row c2"))))
        ),
        "sdt cell": _wtbl(_wtr(_wsdt(_wtc(_wp("cell A"))), _wtc(_wp("plain B")))),
        "sdt table": _wsdt(_wtbl(_wtr(_wtc(_wp("t in sdt"))))),
        "customXml table": _wcx(_wtbl(_wtr(_wtc(_wp("t in cx"))))),
        "customXml row": _wtbl(_wcx(_wtr(_wtc(_wp("row in cx"))))),
        "customXml cell": _wtbl(_wtr(_wcx(_wtc(_wp("cell in cx"))))),
        "sdt in cell": _wtbl(_wtr(_wtc(_wsdt(_wp("para in sdt in cell"))))),
        "stacked wrappers": stacked,
        "sdt in nested table": _wtbl(_wtr(_wtc(_wp("outer") + nested_cell))),
        "nested table in sdt cell": _wtbl(
            _wtr(_wtc(_wsdt(_wtbl(_wtr(_wtc(_wp("nested in sdt cell")))))))
        ),
        "body para in wrappers": _wsdt(_wp("body para in sdt")) + _wcx(_wp("body para in cx")),
    }
    for name, xml in cases.items():
        data = _docx_with_body(_wp("Lead"), xml)
        want = _all_w_texts(data)
        assert len(want) >= 2, name
        res, built, built_loc = _translate_all(data)
        # one block after "Lead": the unit order is the reading order (depth-first, #355 gate)
        assert [u.text for u in res.units] == want, (name, [u.text for u in res.units])
        for out in (built, built_loc):
            got = _all_w_texts(out)
            assert sorted(got) == sorted("DE " + t for t in want), (name, got)
            assert _census(out) == _census(data), (name, _census(out), _census(data))
            again = [u.text for u in DocxFilter().parse(out).units]
            assert again == ["DE " + u.text for u in res.units], name
    print("  26)  sdt / customXml wrapped row, cell, table, paragraph (stacked, nested) round-trip")


def test_nested_with_vmerge():
    vm_restart = '<w:tcPr><w:vMerge w:val="restart"/></w:tcPr>'
    vm_cont = "<w:tcPr><w:vMerge/></w:tcPr>"
    nested = _wtbl(_wtr(_wtc(_wp("nested in merged"))))
    data = _docx_with_body(
        _wtbl(
            _wtr(_wtc(_wp("merged") + nested, vm_restart), _wtc(_wp("r0c1"))),
            _wtr(_wtc("<w:p/>", vm_cont), _wtc(_wp("r1c1"))),
            _wtr(_wtc("<w:p/>", vm_cont), _wtc(_wp("r2c1"))),
        )
    )
    res, built, _ = _translate_all(data)
    texts = [u.text for u in res.units]
    assert texts == ["merged", "nested in merged", "r0c1", "r1c1", "r2c1"], texts
    got = _all_w_texts(built)
    assert got == ["DE merged", "DE nested in merged", "DE r0c1", "DE r1c1", "DE r2c1"], got
    # a nested table inside a CONTINUATION cell is malformed, but its text is kept, not dropped
    data = _docx_with_body(
        _wtbl(
            _wtr(_wtc(_wp("top"), vm_restart)),
            _wtr(_wtc("<w:p/>" + _wtbl(_wtr(_wtc(_wp("hidden nested")))), vm_cont)),
        )
    )
    res, built, _ = _translate_all(data)
    assert sorted(u.text for u in res.units) == ["hidden nested", "top"], [
        u.text for u in res.units
    ]
    assert sorted(_all_w_texts(built)) == ["DE hidden nested", "DE top"]
    print("  27)  vMerge x nested tables: merged text once, a table nested in the merged cell kept")


def test_previous_stamp_exports_unchanged():
    """#355: a skeleton stored under stamp 2 must export exactly as it did before this change:
    nested tables and wrapped rows stay untranslated and nothing renumbers."""
    from unittest.mock import patch

    from docx import Document as Docx
    from docx.table import _Cell

    from app.filters import docx_filter
    from app.filters.docx_filter import DocxFilter, walk_version
    from app.models import Document

    nested = _wtbl(_wtr(_wtc(_wp("nested X"))))
    data = _docx_with_body(
        _wp("Lead"),
        _wtbl(
            _wtr(_wtc(_wp("A") + nested), _wtc(_wp("B"))),
            _wsdt(_wtr(_wtc(_wp("wrapped row")))),
            _wtr(_wtc(_wp("C")), _wtc(_wp("D"))),
        ),
        _wsdt(_wtbl(_wtr(_wtc(_wp("wrapped table"))))),
    )
    f = DocxFilter()
    # the stamp-2 numbering, derived here independently of the filter's own helper
    doc = Docx(io.BytesIO(data))
    tbl = doc.tables[0]
    old = list(doc.paragraphs)
    for tr in tbl._tbl.tr_lst:
        for tc in tr.tc_lst:
            old.extend(_Cell(tc, tbl).paragraphs)
    units = [{"p_index": i, "text": p.text, "style": "Normal"} for i, p in enumerate(old) if p.text]
    assert [u["text"] for u in units] == ["Lead", "A", "B", "C", "D"], units
    skel2 = {"format": "docx", "docx_walk": 2, "units": units}
    assert walk_version(skel2) == 2
    mapping = {str(u["p_index"]): "DE " + u["text"] for u in units}
    # ZIP headers record wall-clock time. Pin it so byte comparisons test the walk,
    # not whether consecutive exports happened across a ZIP timestamp boundary.
    with patch("zipfile.time.localtime", return_value=(2026, 1, 1, 0, 0, 0, 3, 1, -1)):
        out2 = f.build(data, mapping, walk=walk_version(skel2))
        same2 = f.build(data, mapping, walk=2)
        current = f.build(data, mapping)
    assert _all_w_texts(out2) == [
        "DE Lead",
        "DE A",
        "nested X",
        "DE B",
        "wrapped row",
        "DE C",
        "DE D",
        "wrapped table",
    ], _all_w_texts(out2)
    assert out2 == same2
    assert current != out2, "the current walk numbers this file differently"

    # end to end through the real export
    client = TestClient(app)
    doc_id = _new_docx_doc(client, "stamp2nested", data)
    with Session(engine) as s:
        d = s.get(Document, doc_id)
        assert d.skeleton["docx_walk"] == 6
        assert len(d.skeleton["units"]) == 8, d.skeleton["units"]  # nested + wrapped included
        segs = s.exec(
            select(Segment).where(Segment.document_id == doc_id, Segment.target_locale == "de-DE")
        ).all()
        rows = [(x.id, x.source_text) for x in segs]
    for sid, text in rows:
        r = client.post(f"/segments/{sid}/save", data={"target_text": "DE " + text, "tte_ms": "0"})
        assert r.status_code == 200, r.text
    resp = client.get(f"/documents/{doc_id}/export?locale=de-DE")
    assert resp.status_code == 200, resp.text[:300]
    got = _all_w_texts(resp.content)
    every = ["Lead", "A", "nested X", "B", "wrapped row", "C", "D", "wrapped table"]
    assert sorted(got) == sorted("DE " + t for t in every), got
    # the same project stored under stamp 2 (5 units, old numbering): the export takes the
    # stamp-2 walk (never the current one) and leaves nested / wrapped text as the file had it
    with Session(engine) as s:
        d = s.get(Document, doc_id)
        d.skeleton = {**d.skeleton, "docx_walk": 2, "units": units}
        s.add(d)
        s.commit()
    calls = []
    real = docx_filter._v3_paragraphs
    with patch.object(
        docx_filter, "_v3_paragraphs", side_effect=lambda d_: calls.append(1) or real(d_)
    ):
        resp2 = client.get(f"/documents/{doc_id}/export?locale=de-DE")
    assert resp2.status_code == 200 and not calls, (resp2.status_code, calls)
    got2 = _all_w_texts(resp2.content)
    for kept in ("nested X", "wrapped row", "wrapped table"):
        assert kept in got2, (kept, got2)
    assert walk_version({}) == 1 and walk_version(None) == 1
    print("  28)  a skeleton stored under stamp 2 exports on the stamp-2 walk, nothing renumbers")


def test_walk_hostile_and_bounded():
    import time

    from docx import Document as Docx

    from app.filters import docx_filter
    from app.filters.docx_filter import MAX_TABLE_DEPTH, DocxFilter

    f = DocxFilter()
    hostile = {
        "sdt with no content": "<w:sdt><w:sdtPr/></w:sdt>" + _wp("tail"),
        "empty sdtContent": _wsdt("") + _wp("tail"),
        "empty customXml": _wcx("") + _wp("tail"),
        "table without rows": "<w:tbl/>" + _wp("tail"),
        "row without cells": _wtbl("<w:tr/>") + _wp("tail"),
        "cell without paragraph": _wtbl(_wtr("<w:tc/>")) + _wp("tail"),
        "sdtContent holding junk": _wsdt('<w:bookmarkStart w:id="1" w:name="b"/><w:foo/>')
        + _wp("tail"),
        "vmerge only": _wtbl(_wtr("<w:tc><w:tcPr><w:vMerge/></w:tcPr></w:tc>")) + _wp("tail"),
        "stray cell in sdt": _wsdt(_wtc(_wp("stray"))) + _wp("tail"),
    }
    for name, xml in hostile.items():
        data = _docx_with_body(xml)
        res = f.parse(data)  # must not raise
        assert "tail" in [u.text for u in res.units], name
        f.build(data, {str(u.p_index): "X " + u.text for u in res.units})
    # wrappers stacked about as deep as the XML parser allows: iterative walk, no recursion
    deep = _wp("deep wrapped para")
    for i in range(80):
        deep = (_wsdt if i % 2 else _wcx)(deep)
    res = f.parse(_docx_with_body(deep))
    assert [u.text for u in res.units] == ["deep wrapped para"]
    # nesting beyond the bound: no error, the same cut-off in parse and export, text above the
    # bound is still extracted
    n = MAX_TABLE_DEPTH + 8
    inner = _wp("bottom")
    for lvl in range(n, 0, -1):
        inner = _wtbl(_wtr(_wtc(_wp(f"lvl{lvl}") + inner)))
    data = _docx_with_body(inner)
    res = f.parse(data)
    texts = {u.text for u in res.units}
    assert "lvl1" in texts and f"lvl{MAX_TABLE_DEPTH}" in texts, sorted(texts)[:5]
    assert "bottom" not in texts and f"lvl{n}" not in texts
    # the text past the bound is not silent: the untranslated-regions warning names it
    regions = [w for w in res.skeleton.get("file_warnings", []) if w["kind"] == "untranslated"]
    assert any("nested tables" in w["message"] for w in regions), regions
    shallow = f.parse(_docx_with_body(_wtbl(_wtr(_wtc(_wtbl(_wtr(_wtc(_wp("in")))))))))
    assert not shallow.skeleton.get("file_warnings"), shallow.skeleton.get("file_warnings")
    out = f.build(data, {str(u.p_index): "X " + u.text for u in res.units})
    assert {u.text for u in f.parse(out).units} == {"X " + t for t in texts}
    # width: 3,000 sibling nested tables parse in time linear in the file, each paragraph once
    many = "".join(_wtbl(_wtr(_wtc(_wp(f"n{i}")))) for i in range(3000))
    data = _docx_with_body(_wtbl(_wtr(_wtc(_wp("host") + many))))
    t0 = time.perf_counter()
    res = f.parse(data)
    dt = time.perf_counter() - t0
    assert len(res.units) == 3001 and dt < 20, (len(res.units), dt)
    ps = docx_filter._all_paragraphs(Docx(io.BytesIO(data)))
    assert len({id(p._p) for p in ps}) == len(ps)
    print("  29)  hostile wrappers/tables never raise; nesting bound in parse and export; linear")


def test_unknown_stamp_export_route():
    """#355 gate: a stored skeleton with a walk stamp this build does not know is refused
    through the REAL export route as an honest 409 that says what to do (re-import), not
    the generic "contact support" rebuild failure and not a 500."""
    from app.models import Document

    data = _docx_with_body(_wp("Lead"), _wtbl(_wtr(_wtc(_wp("A")), _wtc(_wp("B")))))
    client = TestClient(app)
    doc_id = _new_docx_doc(client, "stamp4", data)
    with Session(engine) as s:
        segs = s.exec(
            select(Segment).where(Segment.document_id == doc_id, Segment.target_locale == "de-DE")
        ).all()
        rows = [(x.id, x.source_text) for x in segs]
    for sid, text in rows:
        r = client.post(f"/segments/{sid}/save", data={"target_text": "DE " + text, "tte_ms": "0"})
        assert r.status_code == 200, r.text
    assert client.get(f"/documents/{doc_id}/export?locale=de-DE").status_code == 200
    for bad in (7, "3"):
        with Session(engine) as s:
            d = s.get(Document, doc_id)
            d.skeleton = {**d.skeleton, "docx_walk": bad}
            s.add(d)
            s.commit()
        resp = client.get(f"/documents/{doc_id}/export?locale=de-DE")
        assert resp.status_code == 409, (bad, resp.status_code, resp.text[:300])
        assert "Re-import" in resp.text and "Contact support" not in resp.text, resp.text[:300]
    print("  30)  an unknown walk stamp exports as a 409 that says to re-import (real route)")


def test_reading_order():
    """#355 maintainer decision: walk 3 numbers body paragraphs and tables in the order they
    sit in the body, wrappers included, nested tables inline; export puts every
    translation back at its own position (distinct marker per position)."""
    from app.filters import docx_filter
    from app.filters.docx_filter import DocxFilter

    nested = _wtbl(_wtr(_wtc(_wp("N1")), _wtc(_wp("N2"))))
    cases = {
        "p t p t": (
            _wp("P1"),
            _wtbl(_wtr(_wtc(_wp("T1a")), _wtc(_wp("T1b")))),
            _wp("P2"),
            _wtbl(_wtr(_wtc(_wp("T2a")))),
            _wp("P3"),
        ),
        "t first": (_wtbl(_wtr(_wtc(_wp("T1")))), _wp("P1"), _wtbl(_wtr(_wtc(_wp("T2"))))),
        "sdt p t": (
            _wp("P0"),
            _wsdt(_wp("P1") + _wtbl(_wtr(_wtc(_wp("T1")))) + _wp("P2")),
            _wtbl(_wtr(_wtc(_wp("T2")))),
            _wcx(_wp("P3") + _wtbl(_wtr(_wtc(_wp("T3"))))),
        ),
        "nested in interleaved": (
            _wp("P1"),
            _wtbl(_wtr(_wtc(_wp("A") + nested + _wp("A tail")), _wtc(_wp("B")))),
            _wp("P2"),
            _wtbl(_wtr(_wtc(nested + _wp("C")))),
            _wp("P3"),
        ),
    }
    want_nested = ["P1", "A", "N1", "N2", "A tail", "B", "P2", "N1", "N2", "C", "P3"]
    for name, blocks in cases.items():
        data = _docx_with_body(*blocks)
        want = _all_w_texts(data)  # document order
        if name == "nested in interleaved":
            assert want == want_nested, want
        res, _built, _built_loc = _translate_all(data)
        assert res.skeleton["docx_walk"] == 6
        assert [u.text for u in res.units] == want, (name, [u.text for u in res.units])
        # distinct marker per POSITION (two paragraphs may share text): translate by index
        f = DocxFilter()
        mapping = {str(u.p_index): f"X{i}" for i, u in enumerate(res.units)}
        for out in (f.build(data, mapping), f.build_localized(data, mapping, "de-DE")):
            got = _all_w_texts(out)
            assert got == [f"X{i}" for i in range(len(want))], (name, got)
            assert _census(out) == _census(data), name
        # parse and export share one walk on the same bytes
        from docx import Document as Docx

        paras = docx_filter._all_paragraphs(Docx(io.BytesIO(data)), 3)
        assert [p.text for p in paras if p.text] == want, name
    # stamp 2 keeps its own (body first, then tables) order
    data = _docx_with_body(_wp("P1"), _wtbl(_wtr(_wtc(_wp("T1")))), _wp("P2"))
    from docx import Document as Docx

    got2 = [p.text for p in docx_filter._all_paragraphs(Docx(io.BytesIO(data)), 2) if p.text]
    assert got2 == ["P1", "P2", "T1"], got2
    print("  31)  walk 3 reading order: paragraphs/tables interleaved, wrappers, nested")


# --- #270 r1-files-06: headers, footers, notes, comments, text boxes, tracked insertions -------
_CT_NS = "{http://schemas.openxmlformats.org/package/2006/content-types}"
_PKG_REL_NS = "{http://schemas.openxmlformats.org/package/2006/relationships}"
_REL_BASE = "http://schemas.openxmlformats.org/officeDocument/2006/relationships/"
_WML_CT = "application/vnd.openxmlformats-officedocument.wordprocessingml."
_PART_CT = {
    "header": _WML_CT + "header+xml",
    "footer": _WML_CT + "footer+xml",
    "footnotes": _WML_CT + "footnotes+xml",
    "endnotes": _WML_CT + "endnotes+xml",
    "comments": _WML_CT + "comments+xml",
    "glossaryDocument": _WML_CT + "document.glossary+xml",
}
_MAIN = "word/document.xml"
_EXTRA_PARTS = (
    "word/header1.xml",
    "word/header2.xml",
    "word/header10.xml",
    "word/footer1.xml",
    "word/footnotes.xml",
    "word/endnotes.xml",
    "word/comments.xml",
)


def _part_xml(root_tag: str, inner: str) -> str:
    return f"<w:{root_tag} {_NSDECL}>{inner}</w:{root_tag}>"


def _docx_with_parts(blocks, parts, dup_rels=(), normalize=True) -> bytes:
    """A .docx whose body is `blocks` plus extra parts: [(part name, kind, xml)] where kind
    is the relationship type's last segment (header, footer, footnotes, endnotes, comments).
    `dup_rels` relates a part a second time. Saved through python-docx once so a later
    comparison sees python-docx's own serialization of every part."""
    base = _docx_with_body(*blocks)
    zin = zipfile.ZipFile(io.BytesIO(base))
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as zout:
        for n in zin.namelist():
            raw = zin.read(n)
            if n == "[Content_Types].xml":
                root = etree.fromstring(raw)
                for name, kind, _xml in parts:
                    el = etree.SubElement(root, _CT_NS + "Override")
                    el.set("PartName", "/" + name)
                    el.set("ContentType", _PART_CT[kind])
                raw = etree.tostring(root, xml_declaration=True, encoding="UTF-8", standalone=True)
            elif n == "word/_rels/document.xml.rels":
                root = etree.fromstring(raw)
                for i, (name, kind, _xml) in enumerate([*parts, *dup_rels]):
                    if isinstance(_xml, str) and _xml == "orphan":
                        continue
                    el = etree.SubElement(root, _PKG_REL_NS + "Relationship")
                    el.set("Id", f"rIdP{i}")
                    el.set("Type", _REL_BASE + kind)
                    el.set("Target", name[len("word/") :])
                raw = etree.tostring(root, xml_declaration=True, encoding="UTF-8", standalone=True)
            zout.writestr(n, raw)
        for name, _kind, xml in parts:
            if xml == "orphan":
                continue
            zout.writestr(name, xml if isinstance(xml, bytes) else xml.encode("utf-8"))
    data = out.getvalue()
    if not normalize:
        return data
    from docx import Document as Docx

    buf = io.BytesIO()
    Docx(io.BytesIO(data)).save(buf)
    return buf.getvalue()


def _own_text(p) -> str:
    """The text of w:p `p` itself, not of paragraphs nested in its text boxes."""
    out = []
    for t in p.iter(W("w", "t")):
        a = t.getparent()
        while a is not None and a.tag != W("w", "p"):
            a = a.getparent()
        if a is p:
            out.append(t.text or "")
    return "".join(out)


def _part_root(data: bytes, part: str = _MAIN):
    return etree.fromstring(zipfile.ZipFile(io.BytesIO(data)).read(part))


def _paras_of(data: bytes, part: str = _MAIN) -> list[str]:
    """Own text of every non-empty paragraph of a part, in document (pre-)order."""
    return [t for t in (_own_text(p) for p in _part_root(data, part).iter(W("w", "p"))) if t]


def _c14n(el) -> bytes:
    return etree.tostring(el, method="c14n")


def _src_texts(res) -> list[str]:
    return [seg.strip_placeholders(u.text).strip() for u in res.units]


def _marker_mapping(res) -> tuple[dict, list[str]]:
    marks = [f"X{n}" for n in range(len(res.units))]
    return {str(u.p_index): m for u, m in zip(res.units, marks, strict=True)}, marks


def _fnote(kind: str, i: int, text: str) -> str:
    ref = "footnoteRef" if kind == "footnote" else "endnoteRef"
    style = "FootnoteReference" if kind == "footnote" else "EndnoteReference"
    return (
        f'<w:{kind} w:id="{i}"><w:p><w:r><w:rPr><w:rStyle w:val="{style}"/></w:rPr><w:{ref}/></w:r>'
        f'<w:r><w:t xml:space="preserve"> {text}</w:t></w:r></w:p></w:{kind}>'
    )


def _sep_notes(kind: str) -> str:
    return (
        f'<w:{kind} w:type="separator" w:id="-1"><w:p><w:r><w:t>SEP</w:t></w:r></w:p></w:{kind}>'
        f'<w:{kind} w:type="continuationSeparator" w:id="0">'
        f"<w:p><w:r><w:t>CSEP</w:t></w:r></w:p></w:{kind}>"
    )


def _comment(i: int, *texts: str) -> str:
    paras = "".join(
        "<w:p>"
        + ("<w:r><w:annotationRef/></w:r>" if n == 0 else "")
        + f"<w:r><w:t>{t}</w:t></w:r></w:p>"
        for n, t in enumerate(texts)
    )
    return f'<w:comment w:id="{i}" w:author="A" w:initials="A">{paras}</w:comment>'


def _all_parts_fixture() -> bytes:
    parts = [
        ("word/header10.xml", "header", _part_xml("hdr", _wp("Header even"))),
        ("word/header2.xml", "header", _part_xml("hdr", _wp("Header first"))),
        ("word/header1.xml", "header", _part_xml("hdr", _wp("Header default"))),
        (
            "word/footer1.xml",
            "footer",
            _part_xml(
                "ftr",
                _wp("Footer head") + _wtbl(_wtr(_wtc(_wp("Footer cell")))) + _wp("Footer tail"),
            ),
        ),
        (
            "word/footnotes.xml",
            "footnotes",
            _part_xml(
                "footnotes",
                _sep_notes("footnote")
                + _fnote("footnote", 1, "Footnote one.")
                + _fnote("footnote", 2, "Footnote two."),
            ),
        ),
        (
            "word/endnotes.xml",
            "endnotes",
            _part_xml("endnotes", _sep_notes("endnote") + _fnote("endnote", 1, "Endnote one.")),
        ),
        (
            "word/comments.xml",
            "comments",
            _part_xml(
                "comments",
                _comment(0, "Comment one.") + _comment(1, "Comment two a.", "Comment two b."),
            ),
        ),
    ]
    return _docx_with_parts(
        [_wp("Body one"), _wtbl(_wtr(_wtc(_wp("Cell")))), _wp("Body two")], parts
    )


_ALL_PARTS_ORDER = [
    "Body one",
    "Cell",
    "Body two",
    "Header default",
    "Header first",
    "Header even",
    "Footer head",
    "Footer cell",
    "Footer tail",
    "Footnote one.",
    "Footnote two.",
    "Endnote one.",
    "Comment one.",
    "Comment two a.",
    "Comment two b.",
]


def test_extra_parts_roundtrip():
    """#270: headers, footers, footnotes, endnotes and comments are units (walk 4), after the
    main body in the documented order; each unit records its part; a translation lands in
    its own part and position; separator notes and every other part are left alone."""
    from app.filters.docx_filter import DocxFilter

    data = _all_parts_fixture()
    f = DocxFilter()
    res = f.parse(data)
    assert res.skeleton["docx_walk"] == 6
    assert _src_texts(res) == _ALL_PARTS_ORDER, _src_texts(res)
    assert [d["part"] for d in res.skeleton["units"]] == (
        [_MAIN] * 3
        + ["word/header1.xml", "word/header2.xml", "word/header10.xml"]  # natural order
        + ["word/footer1.xml"] * 3
        + ["word/footnotes.xml"] * 2
        + ["word/endnotes.xml"]
        + ["word/comments.xml"] * 3
    ), [d["part"] for d in res.skeleton["units"]]
    assert [u.p_index for u in res.units] == list(range(15))
    mapping, marks = _marker_mapping(res)
    body3 = {"0": "X0", "1": "X1", "2": "X2"}
    out3 = f.build(data, body3, walk=3)
    out3_loc = f.build_localized(data, body3, "de-DE", walk=3)
    n_ref = {
        part: len(
            _part_root(data, part).xpath(
                "//w:footnoteRef|//w:endnoteRef|//w:annotationRef", namespaces=NS
            )
        )
        for part in _EXTRA_PARTS
    }
    for label, out in (
        ("build", f.build(data, mapping)),
        ("build_localized", f.build_localized(data, mapping, "de-DE")),
    ):
        assert _paras_of(out) == marks[:3], (label, _paras_of(out))
        assert _paras_of(out, "word/header1.xml") == ["X3"], label
        assert _paras_of(out, "word/header2.xml") == ["X4"], label
        assert _paras_of(out, "word/header10.xml") == ["X5"], label
        assert _paras_of(out, "word/footer1.xml") == ["X6", "X7", "X8"], label
        # the separator notes carry text here on purpose: they are not units
        assert _paras_of(out, "word/footnotes.xml") == ["SEP", "CSEP", "X9", "X10"], label
        assert _paras_of(out, "word/endnotes.xml") == ["SEP", "CSEP", "X11"], label
        assert _paras_of(out, "word/comments.xml") == ["X12", "X13", "X14"], label
        # the note / comment reference marks the translation dropped are put back
        for part, n in n_ref.items():
            root = _part_root(out, part)
            got = len(root.xpath("//w:footnoteRef|//w:endnoteRef|//w:annotationRef", namespaces=NS))
            assert got == n, (label, part, got, n)
        same3 = out3 if label == "build" else out3_loc
        z, z3 = zipfile.ZipFile(io.BytesIO(out)), zipfile.ZipFile(io.BytesIO(same3))
        assert z.namelist() == z3.namelist(), (z.namelist(), z3.namelist())
        touched = {_MAIN, *_EXTRA_PARTS}
        for n in z.namelist():
            if n not in touched:
                assert z.read(n) == z3.read(n), (label, n)
        _package_intact(data, out) if label == "build" else None
    # a translated paragraph in a part takes the TARGET language, an untranslated one keeps
    # the source (blob parts included): separator "SEP" stays en-US, the note text does not
    loc = f.build_localized(data, mapping, "de-DE")
    fn = _part_root(loc, "word/footnotes.xml")
    sep = fn.xpath("//w:r[w:t='SEP']/w:rPr/w:lang/@w:val", namespaces=NS)
    note = fn.xpath("//w:r[w:t='X9']/w:rPr/w:lang/@w:val", namespaces=NS)
    assert sep == ["en-US"] and "en-US" not in note, (sep, note)
    hdr = _part_root(loc, "word/header1.xml").xpath("//w:r/w:rPr/w:lang/@w:val", namespaces=NS)
    assert "en-US" not in hdr, hdr
    # stamp 3 numbers the body only: the extra parts stay exactly as they were
    for part in _EXTRA_PARTS:
        assert _c14n(_part_root(out3, part)) == _c14n(_part_root(data, part)), part
    print("  32)  walk 4: headers, footers, notes, comments: order, part recorded, round trip")


def _tb(lines, fallback=None, ac=True, choice_extra="") -> str:
    """A run holding a text box: mc:AlternateContent (Choice DrawingML + Fallback VML copy
    of the same shape) or, with ac=False, VML only. `lines` are the box's paragraphs (a
    string is raw block XML)."""
    body = lines if isinstance(lines, str) else "".join(_wp(t) for t in lines)
    fb = (
        body
        if fallback is None
        else (fallback if isinstance(fallback, str) else "".join(_wp(t) for t in fallback))
    )
    vml = (
        '<w:pict><v:shape id="tb" style="width:1pt"><v:textbox>'
        "<w:txbxContent>{}</w:txbxContent></v:textbox></v:shape></w:pict>"
    )
    if not ac:
        return "<w:r>" + vml.format(body) + "</w:r>"
    choice = (
        '<mc:Choice Requires="wps"><w:drawing><wp:anchor><wp:docPr id="901" name="tb"/>'
        "<a:graphic><a:graphicData "
        'uri="http://schemas.microsoft.com/office/word/2010/wordprocessingShape">'
        f"<wps:wsp><wps:txbx><w:txbxContent>{body}</w:txbxContent></wps:txbx></wps:wsp>"
        f"</a:graphicData></a:graphic></wp:anchor></w:drawing></mc:Choice>{choice_extra}"
    )
    return (
        f"<w:r><mc:AlternateContent>{choice}<mc:Fallback>{vml.format(fb)}</mc:Fallback>"
        "</mc:AlternateContent></w:r>"
    )


def _anchor_p(text: str, *runs: str) -> str:
    lead = f"<w:r><w:t>{text}</w:t></w:r>" if text else ""
    return f"<w:p>{lead}{''.join(runs)}</w:p>"


def test_text_boxes():
    """#270: a text box's paragraphs are units right after the paragraph it is anchored in
    (nested boxes after their own anchor); the Choice and Fallback copies of one box are
    extracted once and both written; a copy that is not the same shape is left alone."""
    from app.filters.docx_filter import DocxFilter

    f = DocxFilter()

    def run(blocks, want_order, want_paras, parts=None, data=None):
        data = data or _docx_with_parts(blocks, parts or [])
        res = f.parse(data)
        assert _src_texts(res) == want_order, (_src_texts(res), want_order)
        mapping, marks = _marker_mapping(res)
        for out in (f.build(data, mapping), f.build_localized(data, mapping, "de-DE")):
            got = _paras_of(out)
            want = want_paras(marks)
            assert got == want, (got, want)
            # the shapes themselves are intact: every drawing, pict and box is still there
            for tag in ("drawing", "pict", "txbxContent"):
                assert len(list(_part_root(out).iter(W("w", tag)))) == len(
                    list(_part_root(data).iter(W("w", tag)))
                ), tag
        return res

    # A. standard Choice + Fallback box between two paragraphs
    run(
        [_wp("Before"), _anchor_p("Anchor", _tb(["Box one", "Box two"])), _wp("After")],
        ["Before", "Anchor", "Box one", "Box two", "After"],
        lambda m: [m[0], m[1], m[2], m[3], m[2], m[3], m[4]],  # both copies carry X2, X3
    )
    # B. Fallback with another paragraph count is not the same shape: only Choice written
    run(
        [_anchor_p("Anchor", _tb(["Box one", "Box two"], fallback=["Only one"]))],
        ["Anchor", "Box one", "Box two"],
        lambda m: [m[0], m[1], m[2], "Only one"],
    )
    # C. Fallback with the same count but other text (a stale copy) is left alone
    run(
        [_anchor_p("Anchor", _tb(["Box one", "Box two"], fallback=["Old one", "Old two"]))],
        ["Anchor", "Box one", "Box two"],
        lambda m: [m[0], m[1], m[2], "Old one", "Old two"],
    )
    # D. VML only (no AlternateContent): one copy, translated
    run(
        [_anchor_p("Anchor", _tb(["Vml box"], ac=False)), _wp("After")],
        ["Anchor", "Vml box", "After"],
        lambda m: [m[0], m[1], m[2]],
    )
    # E. an anchor paragraph with no text of its own: only the box's paragraphs are units
    run(
        [_anchor_p("", _tb(["Box one"])), _wp("After")],
        ["Box one", "After"],
        lambda m: [m[0], m[0], m[1]],
    )
    # F. nested: a box paragraph that anchors another box; inner text follows its anchor
    inner = _anchor_p("Outer one", _tb(["Inner a"]))
    run(
        [_anchor_p("Anchor", _tb(inner + _wp("Outer two")))],
        ["Anchor", "Outer one", "Inner a", "Outer two"],
        lambda m: [m[0], m[1], m[2], m[2], m[3], m[1], m[2], m[2], m[3]],
    )
    # G. a table inside a text box, a content control around a box paragraph
    run(
        [_anchor_p("Anchor", _tb(_wtbl(_wtr(_wtc(_wp("Boxed cell")))) + _wsdt(_wp("Boxed sdt"))))],
        ["Anchor", "Boxed cell", "Boxed sdt"],
        lambda m: [m[0], m[1], m[2], m[1], m[2]],
    )
    # H. a text box in a header: after the header paragraph it hangs on
    hdr = _part_xml("hdr", _anchor_p("Header text", _tb(["Header box"])))
    data = _docx_with_parts([_wp("Body")], [("word/header1.xml", "header", hdr)])
    res = f.parse(data)
    assert _src_texts(res) == ["Body", "Header text", "Header box"], _src_texts(res)
    assert [d["part"] for d in res.skeleton["units"]] == [
        _MAIN,
        "word/header1.xml",
        "word/header1.xml",
    ]
    mapping, _marks = _marker_mapping(res)
    out = f.build(data, mapping)
    assert _paras_of(out, "word/header1.xml") == ["X1", "X2", "X2"], _paras_of(
        out, "word/header1.xml"
    )
    # I. the anchor paragraph's own text and its box keep their own formatting runs
    p = (
        '<w:p><w:r><w:rPr><w:b/></w:rPr><w:t xml:space="preserve">Bold </w:t></w:r>'
        "<w:r><w:t>plain</w:t></w:r>" + _tb(["Boxed"]) + "</w:p>"
    )
    data = _docx_with_parts([p], [])
    res = f.parse(data)
    first_text = res.units[0].text
    assert first_text.startswith("⟦r1⟧Bold ⟦/r1⟧⟦r2⟧plain⟦/r2⟧⟦x3⟧"), first_text
    mapping = {"0": _translate(res.units[0].text), "1": "BOXED"}
    out = f.build(data, mapping)
    root = _part_root(out)
    assert root.xpath("//w:body/w:p/w:r[w:rPr/w:b]/w:t/text()", namespaces=NS) == ["BOLD "]
    assert _paras_of(out) == ["BOLD PLAIN", "BOXED", "BOXED"], _paras_of(out)
    print("  33)  walk 4: text boxes inline after their anchor, Choice+Fallback once, both written")


def test_tracked_changes():
    """#270: a tracked insertion is text as Word shows it (translated inside its w:ins,
    author / date / id kept); a tracked deletion is not extracted and stays untouched."""
    from app.filters.docx_filter import DocxFilter

    f = DocxFilter()
    meta = 'w:author="Ann" w:date="2026-09-01T10:00:00Z"'
    mixed = (
        '<w:p><w:r><w:t xml:space="preserve">Start </w:t></w:r>'
        f'<w:ins w:id="1" {meta}><w:r><w:rPr><w:b/></w:rPr><w:t>inserted words</w:t></w:r></w:ins>'
        f'<w:del w:id="2" {meta}><w:r><w:delText>deleted words</w:delText></w:r></w:del>'
        '<w:r><w:t xml:space="preserve"> End.</w:t></w:r></w:p>'
    )
    ins_only = f'<w:p><w:ins w:id="3" {meta}><w:r><w:t>inserted only</w:t></w:r></w:ins></w:p>'
    del_only = (
        f'<w:p><w:del w:id="4" {meta}><w:r><w:delText>deleted only</w:delText></w:r></w:del></w:p>'
    )
    hdr = _part_xml("hdr", ins_only.replace('w:id="3"', 'w:id="5"').replace("only", "in header"))
    data = _docx_with_parts([mixed, ins_only, del_only], [("word/header1.xml", "header", hdr)])
    res = f.parse(data)
    assert [u.text for u in res.units] == [
        "⟦r1⟧Start ⟦/r1⟧⟦r2⟧inserted words⟦/r2⟧⟦x4⟧⟦r3⟧ End.⟦/r3⟧",
        "inserted only",
        "inserted in header",
    ], [u.text for u in res.units]
    src_root = _part_root(data)
    src_del = [_c14n(d) for d in src_root.iter(W("w", "del"))]
    mapping = {str(u.p_index): _translate(u.text) for u in res.units}
    for label, out in (
        ("build", f.build(data, mapping)),
        ("build_localized", f.build_localized(data, mapping, "de-DE")),
    ):
        root = _part_root(out)
        p0 = root.find(".//w:body/w:p", NS)
        assert [etree.QName(c).localname for c in p0 if etree.QName(c).localname != "pPr"] == [
            "r",
            "ins",
            "del",
            "r",
        ], label
        ins = p0.find("w:ins", NS)
        assert ins.get(W("w", "id")) == "1" and ins.get(W("w", "author")) == "Ann", label
        assert ins.get(W("w", "date")) == "2026-09-01T10:00:00Z", label
        assert "".join(ins.itertext()) == "INSERTED WORDS", label
        assert ins.xpath("count(w:r/w:rPr/w:b)", namespaces=NS) == 1, label  # formatting kept
        assert _own_text(p0) == "START INSERTED WORDS END.", label
        # the deletions (also the paragraph that holds only one) are byte-for-byte as before
        if label == "build":
            assert [_c14n(d) for d in root.iter(W("w", "del"))] == src_del, label
        else:  # build_localized only adds the source language to a run that had none
            dels = list(root.iter(W("w", "del")))
            assert [d.attrib for d in dels] == [d.attrib for d in src_root.iter(W("w", "del"))]
        assert (
            "".join(root.xpath("//w:del//w:delText/text()", namespaces=NS))
            == "deleted wordsdeleted only"
        )
        body = _paras_of(out)
        assert body == ["START INSERTED WORDS END.", "INSERTED ONLY"], (label, body)
        assert _paras_of(out, "word/header1.xml") == ["INSERTED IN HEADER"], label
        from docx import Document as Docx

        Docx(io.BytesIO(out))
    # stamp 3 never touched a tracked insertion: the same mapping leaves it as it was
    out3 = f.build(data, {"0": "⟦r1⟧S ⟦/r1⟧⟦r2⟧I⟦/r2⟧⟦x4⟧⟦r3⟧ E.⟦/r3⟧"}, walk=3)
    assert "".join(_part_root(out3).find(".//w:ins", NS).itertext()) == "inserted words"
    print("  34)  walk 4: tracked insertions are text (kept in w:ins); deletions untouched")


def test_previous_stamps_skip_new_parts():
    """#270: a skeleton stored under stamp 3 or 2 exports exactly as before: its body is
    translated and every extra part, text box and tracked change is left as it was."""
    from docx import Document as Docx

    from app.filters import docx_filter
    from app.filters.docx_filter import DocxFilter

    f = DocxFilter()
    meta = 'w:author="Ann" w:date="2026-09-01T10:00:00Z"'
    body_p = _anchor_p("Anchor", _tb(["Box one"]))
    ins_p = (
        f'<w:p><w:r><w:t>Lead </w:t></w:r><w:ins w:id="1" {meta}>'
        "<w:r><w:t>added</w:t></w:r></w:ins></w:p>"
    )
    data = _docx_with_parts(
        [_wp("Body"), body_p, ins_p, _wtbl(_wtr(_wtc(_wp("Cell"))))],
        [
            ("word/header1.xml", "header", _part_xml("hdr", _wp("Header"))),
            (
                "word/footnotes.xml",
                "footnotes",
                _part_xml("footnotes", _fnote("footnote", 1, "Note")),
            ),
        ],
    )
    for walk in (3, 2):
        paras = docx_filter._all_paragraphs(Docx(io.BytesIO(data)), walk)
        mapping = {str(i): "T " + p.text for i, p in enumerate(paras) if p.text.strip()}
        assert mapping, walk
        for label, out in (
            ("build", f.build(data, mapping, walk=walk)),
            ("build_localized", f.build_localized(data, mapping, "de-DE", walk=walk)),
        ):
            body = _paras_of(out)
            assert "T Body" in body and "T Cell" in body and "T Anchor" in body, (walk, body)
            assert body.count("Box one") == 2, (walk, body)  # both copies, still the source
            assert "added" in "".join(_part_root(out).find(".//w:ins", NS).itertext()), walk
            # build_localized only writes the source language onto runs that had none, so
            # the byte-level comparison is for build() and the text comparison for both
            for part in ("word/header1.xml", "word/footnotes.xml"):
                if label == "build":
                    assert _c14n(_part_root(out, part)) == _c14n(_part_root(data, part)), part
                assert _paras_of(out, part) == _paras_of(data, part), (walk, label, part)
            tb_src = list(_part_root(data).iter(W("w", "txbxContent")))
            tb_out = list(_part_root(out).iter(W("w", "txbxContent")))
            if label == "build":
                assert [_c14n(b) for b in tb_out] == [_c14n(b) for b in tb_src], walk
            assert [list(b.itertext()) for b in tb_out] == [list(b.itertext()) for b in tb_src]
    # and through the real export route with a stored stamp-3 skeleton
    from unittest.mock import patch

    from app.models import Document

    data = _docx_with_parts(
        [_wp("Body one."), _wp("Body two.")],
        [("word/header1.xml", "header", _part_xml("hdr", _wp("Header text.")))],
    )
    client = TestClient(app)
    doc_id = _new_docx_doc(client, "stamp3parts", data)
    with Session(engine) as s:
        d = s.get(Document, doc_id)
        assert d.skeleton["docx_walk"] == 6 and len(d.skeleton["units"]) == 3
        d.skeleton = {
            **d.skeleton,
            "docx_walk": 3,
            "units": [{k: v for k, v in u.items() if k != "part"} for u in d.skeleton["units"][:2]],
        }
        s.add(d)
        segs = s.exec(
            select(Segment).where(Segment.document_id == doc_id, Segment.target_locale == "de-DE")
        ).all()
        segs = sorted(segs, key=lambda x: x.order_index)
        assert len(segs) == 3
        s.delete(segs[2])  # the header segment: a stamp-3 project never had one
        s.commit()
        rows = [(x.id, x.source_text) for x in segs[:2]]
    for sid, text in rows:
        r = client.post(f"/segments/{sid}/save", data={"target_text": "DE " + text, "tte_ms": "0"})
        assert r.status_code == 200, r.text
    with patch.object(docx_filter, "_v4_walk", side_effect=AssertionError("walk 4")):
        resp = client.get(f"/documents/{doc_id}/export?locale=de-DE")
    assert resp.status_code == 200, resp.text[:300]
    assert _paras_of(resp.content) == ["DE Body one.", "DE Body two."], _paras_of(resp.content)
    assert _paras_of(resp.content, "word/header1.xml") == ["Header text."]
    print("  35)  stamp 3 and 2 skeletons never touch headers, notes, text boxes, insertions")


def test_parts_hostile():
    """#270: malformed or hostile extra parts never raise or 500; parts are walked once and
    only when related; the paragraph cap counts them; the walk is linear."""
    from unittest.mock import patch

    from app import config
    from app.filters.base import InputLimitError
    from app.filters.docx_filter import MAX_BOX_DEPTH, DocxFilter

    f = DocxFilter()
    # a blob part that is not XML, a notes root with junk, a comment with no paragraphs
    parts = [
        ("word/footnotes.xml", "footnotes", b"<w:footnotes <<< not xml"),
        (
            "word/endnotes.xml",
            "endnotes",
            _part_xml(
                "endnotes",
                "<w:junk/><w:endnote><w:p/></w:endnote><w:endnote/>" + _fnote("endnote", 3, "Good"),
            ),
        ),
        (
            "word/comments.xml",
            "comments",
            _part_xml("comments", '<w:comment w:id="0"/><w:comment/>'),
        ),
        (
            "word/header1.xml",
            "header",
            _part_xml(
                "hdr",
                _wsdt(_wp("Header sdt"))
                + _wtbl(_wtr(_wtc(_wtbl(_wtr(_wtc(_wp("Header nested"))))))),
            ),
        ),
    ]
    data = _docx_with_parts([_wp("Body")], parts)
    res = f.parse(data)
    assert _src_texts(res) == ["Body", "Header sdt", "Header nested", "Good"], _src_texts(res)
    mapping, marks = _marker_mapping(res)
    out = f.build_localized(data, mapping, "de-DE")
    assert _paras_of(out, "word/header1.xml") == ["X1", "X2"]
    assert _paras_of(out, "word/endnotes.xml") == ["X3"]
    assert zipfile.ZipFile(io.BytesIO(out)).read("word/footnotes.xml") == zipfile.ZipFile(
        io.BytesIO(data)
    ).read("word/footnotes.xml")
    # a part related twice is walked once; a part with no relationship is not walked
    parts = [
        ("word/header1.xml", "header", _part_xml("hdr", _wp("Related twice"))),
        ("word/header2.xml", "header", "orphan"),
    ]
    data = _docx_with_parts([_wp("Body")], parts, dup_rels=[("word/header1.xml", "header", "")])
    assert _src_texts(f.parse(data)) == ["Body", "Related twice"]
    # the paragraph cap counts the extra parts too (parse refuses, with the limit message)
    big = _docx_with_parts(
        [_wp("Body")],
        [
            (
                "word/header1.xml",
                "header",
                _part_xml("hdr", "".join(_wp(f"H{i}") for i in range(30))),
            )
        ],
    )
    with patch.object(config, "DOCX_MAX_PARAGRAPHS", 10):
        try:
            f.parse(big)
        except InputLimitError as exc:
            assert "paragraph limit" in str(exc), exc
        else:
            raise AssertionError("an oversized header must hit the paragraph cap")
    assert len(f.parse(big).units) == 31
    # text boxes nested past the depth bound: no error, the same units on parse and export
    inner = _wp("Deepest")
    for depth in range(MAX_BOX_DEPTH + 4):
        inner = _anchor_p(f"Level {depth}", _tb(inner))
    deep = _docx_with_parts([inner], [])
    res = f.parse(deep)
    assert 2 <= len(res.units) <= MAX_BOX_DEPTH + 3, len(res.units)
    mapping, marks = _marker_mapping(res)
    out = f.build(deep, mapping)
    assert marks[0] in _paras_of(out) and marks[-1] in _paras_of(out)
    f.parse(out)
    # linear: 3,000 footnotes
    notes = "".join(_fnote("footnote", i, f"Note {i}.") for i in range(1, 3001))
    many = _docx_with_parts(
        [_wp("Body")],
        [
            (
                "word/footnotes.xml",
                "footnotes",
                _part_xml("footnotes", _sep_notes("footnote") + notes),
            )
        ],
    )
    t0 = time.time()
    res = f.parse(many)
    mapping, marks = _marker_mapping(res)
    out = f.build(many, mapping)
    dt = time.time() - t0
    assert len(res.units) == 3001 and dt < 30, (len(res.units), dt)
    assert _paras_of(out, "word/footnotes.xml")[2:] == marks[1:], (
        "every note landed in its own place"
    )
    print("  36)  hostile parts never raise; related once; cap counts them; linear")


def test_untranslated_regions_warning():
    """#270: the untranslated-regions warning names only what walk 4 still skips: a content
    control inside a paragraph (any walked part) and the text of parts it does not walk."""
    from app.filters.docx_filter import DocxFilter

    f = DocxFilter()
    meta = 'w:author="Ann" w:date="2026-09-01T10:00:00Z"'

    def regions(data):
        return {
            w["part"]: w["message"]
            for w in f.parse(data).skeleton.get("file_warnings", [])
            if w["code"] == "docx_untranslated_regions"
        }

    translated = _docx_with_parts(
        [
            _anchor_p("Anchor", _tb(["Box"])),
            f'<w:p><w:ins w:id="1" {meta}><w:r><w:t>Added</w:t></w:r></w:ins></w:p>',
            _wsdt(_wp("Block control")),
        ],
        [
            ("word/header1.xml", "header", _part_xml("hdr", _wsdt(_wp("Header control")))),
            ("word/footer1.xml", "footer", _part_xml("ftr", _wp("Footer"))),
            (
                "word/footnotes.xml",
                "footnotes",
                _part_xml("footnotes", _fnote("footnote", 1, "Note")),
            ),
            ("word/endnotes.xml", "endnotes", _part_xml("endnotes", _fnote("endnote", 1, "End"))),
            ("word/comments.xml", "comments", _part_xml("comments", _comment(0, "Remark"))),
        ],
    )
    assert regions(translated) == {}, regions(translated)
    # walk 6 (#686) reads a text content control inside a paragraph; a list control stays
    inline_text = _docx_with_body(
        f"<w:p><w:r><w:t>Lead </w:t></w:r>{_wsdt('<w:r><w:t>inline control</w:t></w:r>')}</w:p>"
    )
    assert regions(inline_text) == {}, regions(inline_text)
    run_level = (
        "<w:p><w:r><w:t>Lead </w:t></w:r><w:sdt><w:sdtPr><w:comboBox/></w:sdtPr><w:sdtContent>"
        "<w:r><w:t>inline control</w:t></w:r></w:sdtContent></w:sdt></w:p>"
    )
    glossary = _part_xml("glossaryDocument", _anchor_p("Block text", _tb(["Glossary box"])))
    skipped = _docx_with_parts(
        [_wp("Body")],
        [
            ("word/header1.xml", "header", _part_xml("hdr", run_level)),
            ("word/glossary/document.xml", "glossaryDocument", glossary),
        ],
    )
    got = regions(skipped)
    assert got == {
        "word/header1.xml": "Untranslated DOCX regions: content controls (including TOC). "
        "Review these in Word before delivery.",
        "word/glossary/document.xml": "Untranslated DOCX regions: text boxes. "
        "Review these in Word before delivery.",
    }, got
    print("  37)  untranslated-regions warning names only what stamp 4 still skips")


def test_moves_split_revisions_nested_insertions():
    """#270 gate: a move destination (w:moveTo) is text like an insertion and is rebuilt
    inside its w:moveTo, the move source (w:moveFrom) and its range markers stay untouched;
    a tracked change a reordered translation splits gets a fresh, document-unique w:id for
    each extra piece (author and date kept); an insertion nested in a hyperlink is carried
    as it was under stamp 5 and read as text from stamp 6 (#686); one in a field is named."""
    from app.filters.docx_filter import DocxFilter

    f = DocxFilter()
    meta = 'w:author="Ann" w:date="2026-09-01T10:00:00Z"'
    move = (
        '<w:p><w:r><w:t xml:space="preserve">Keep </w:t></w:r>'
        f'<w:moveFromRangeStart w:id="10" w:name="m1" {meta}/>'
        f'<w:moveFrom w:id="11" {meta}><w:r><w:delText>moved away</w:delText></w:r></w:moveFrom>'
        '<w:moveFromRangeEnd w:id="10"/></w:p>'
        f'<w:p><w:moveToRangeStart w:id="12" w:name="m1" {meta}/>'
        f'<w:moveTo w:id="13" {meta}><w:r><w:t>moved here</w:t></w:r></w:moveTo>'
        '<w:moveToRangeEnd w:id="12"/></w:p>'
    )
    split = (
        '<w:p><w:r><w:t xml:space="preserve">One </w:t></w:r>'
        f'<w:ins w:id="20" {meta}><w:r><w:t xml:space="preserve">two </w:t></w:r>'
        "<w:r><w:rPr><w:b/></w:rPr><w:t>three</w:t></w:r></w:ins></w:p>"
    )
    nested = (
        '<w:p><w:hyperlink w:anchor="x"><w:r><w:t xml:space="preserve">Link </w:t></w:r>'
        f'<w:ins w:id="30" {meta}><w:r><w:t>added</w:t></w:r></w:ins></w:hyperlink></w:p>'
    )
    hdr = _part_xml("hdr", f'<w:p><w:ins w:id="77" {meta}><w:r><w:t>H</w:t></w:r></w:ins></w:p>')
    data = _docx_with_parts([move, split], [("word/header1.xml", "header", hdr)])
    res = f.parse(data)
    texts = [u.text for u in res.units]
    assert texts == ["Keep ⟦x1⟧", "moved here", "⟦r1⟧One ⟦/r1⟧⟦r2⟧two ⟦/r2⟧⟦r3⟧three⟦/r3⟧", "H"]
    # the translation puts the plain run between the two inserted runs: two w:ins pieces
    mapping = {
        str(res.units[0].p_index): "BEHALTEN ⟦x1⟧",
        str(res.units[1].p_index): "HIERHER",
        str(res.units[2].p_index): "⟦r2⟧ZWEI ⟦/r2⟧⟦r1⟧EINS ⟦/r1⟧⟦r3⟧DREI⟦/r3⟧",
    }
    src = _part_root(data)
    for label, out in (
        ("build", f.build(data, mapping)),
        ("build_localized", f.build_localized(data, mapping, "de-DE")),
    ):
        root = _part_root(out)
        mt = root.find(".//w:moveTo", NS)
        assert "".join(mt.itertext()) == "HIERHER" and mt.get(W("w", "id")) == "13", label
        mf = root.find(".//w:moveFrom", NS)
        assert (
            "".join(mf.itertext()) == "moved away"
            and mf.attrib == src.find(".//w:moveFrom", NS).attrib
        ), label
        for tag in ("moveFromRangeStart", "moveFromRangeEnd", "moveToRangeStart", "moveToRangeEnd"):
            assert len(root.findall(".//w:" + tag, NS)) == 1, (label, tag)
        pieces = root.xpath("//w:ins", namespaces=NS)
        assert ["".join(p.itertext()) for p in pieces] == ["ZWEI ", "DREI"], label
        assert [p.get(W("w", "author")) for p in pieces] == ["Ann", "Ann"], label
        assert pieces[0].get(W("w", "id")) == "20", label
        ids = [
            e.get(W("w", "id"))
            for r in (root, _part_root(out, "word/header1.xml"))
            for e in r.iter()
            if isinstance(e.tag, str) and e.get(W("w", "id")) is not None
        ]
        dup = {i for i in ids if ids.count(i) > 1 and i not in ("10", "12")}  # range pairs
        assert not dup and int(pieces[1].get(W("w", "id"))) > 77, (label, dup)
        from docx import Document as Docx

        Docx(io.BytesIO(out))
    # an insertion nested in a hyperlink: one object under stamp 5, text from stamp 6 (#686)
    nested_doc = _docx_with_parts([nested], [])
    res = DocxFilter().parse(nested_doc)
    assert [u.text for u in res.units] == ["⟦r1⟧Link ⟦/r1⟧⟦r2⟧added⟦/r2⟧"], res.units
    assert not res.skeleton.get("file_warnings"), res.skeleton.get("file_warnings")
    out = f.build(nested_doc, {str(res.units[0].p_index): "⟦r1⟧VERWEIS ⟦/r1⟧⟦r2⟧NEU⟦/r2⟧"})
    link = _part_root(out).find(".//w:hyperlink", NS)
    assert "".join(link.itertext()) == "VERWEIS NEU", "".join(link.itertext())
    assert "".join(link.find("w:ins", NS).itertext()) == "NEU"
    assert link.find("w:ins", NS).get(W("w", "id")) == "30"
    from docx import Document as Docx

    from app.filters import docx_filter

    p5 = docx_filter._all_paragraphs(Docx(io.BytesIO(nested_doc)), 5)
    assert docx_filter._tokenize_paragraph(p5[0], 5)[0] == "Link ⟦x1⟧"
    # an insertion inside a simple field is still carried: the warning names it
    in_field = (
        '<w:p><w:r><w:t xml:space="preserve">Lead </w:t></w:r><w:fldSimple w:instr=" PAGE ">'
        f'<w:ins w:id="31" {meta}><w:r><w:t>added</w:t></w:r></w:ins></w:fldSimple></w:p>'
    )
    warn = DocxFilter().parse(_docx_with_parts([in_field], [])).skeleton.get("file_warnings", [])
    msgs = [w["message"] for w in warn if w["code"] == "docx_untranslated_regions"]
    assert msgs == [
        "Untranslated DOCX regions: tracked insertions. Review these in Word before delivery."
    ], msgs
    print("  39)  gate: moves read like insertions, split revisions get fresh ids, nested read")


def test_parts_end_to_end():
    """#270 through upload -> segments -> save -> export: the header, a footnote, a comment
    and both copies of a text box come back translated, in the right parts."""
    from app.models import Document

    data = _docx_with_parts(
        [_wp("Body text."), _anchor_p("Anchor text.", _tb(["Box text."]))],
        [
            ("word/header1.xml", "header", _part_xml("hdr", _wp("Header text."))),
            (
                "word/footnotes.xml",
                "footnotes",
                _part_xml("footnotes", _fnote("footnote", 1, "Note text.")),
            ),
            ("word/comments.xml", "comments", _part_xml("comments", _comment(0, "Comment text."))),
        ],
    )
    client = TestClient(app)
    # #690: a new project leaves Word comments out until its option is switched on
    off_id = _new_docx_doc(client, "partse2e-off", data)
    with Session(engine) as s:
        off_parts = [u["part"] for u in s.get(Document, off_id).skeleton["units"]]
    assert "word/comments.xml" not in off_parts and "word/footnotes.xml" in off_parts, off_parts
    doc_id = _new_docx_doc(client, "partse2e", data, comments=True)
    with Session(engine) as s:
        d = s.get(Document, doc_id)
        assert d.skeleton["docx_walk"] == 6
        parts = [u["part"] for u in d.skeleton["units"]]
        assert parts == [_MAIN] * 3 + [
            "word/header1.xml",
            "word/footnotes.xml",
            "word/comments.xml",
        ], parts
        assert not [
            w
            for w in d.skeleton.get("file_warnings", [])
            if w["code"] == "docx_untranslated_regions"
        ]
        segs = s.exec(
            select(Segment).where(Segment.document_id == doc_id, Segment.target_locale == "de-DE")
        ).all()
        rows = [(x.id, x.source_text) for x in segs]
    assert len(rows) == 6, rows
    for sid, text in rows:
        r = client.post(f"/segments/{sid}/save", data={"target_text": "DE " + text, "tte_ms": "0"})
        assert r.status_code == 200, r.text
    resp = client.get(f"/documents/{doc_id}/export?locale=de-DE")
    assert resp.status_code == 200, resp.text[:300]
    out = resp.content
    assert _paras_of(out) == [
        "DE Body text.",
        "DE Anchor text.",
        "DE Box text.",
        "DE Box text.",
    ], _paras_of(out)
    assert _paras_of(out, "word/header1.xml") == ["DE Header text."]
    assert [" ".join(t.split()) for t in _paras_of(out, "word/footnotes.xml")] == ["DE Note text."]
    assert _paras_of(out, "word/comments.xml") == ["DE Comment text."]
    print(
        "  38)  end to end: header, footnote, comment and both text-box copies come back translated"
    )


# --- #368: leftovers after walker stamps 3 and 4 ------------------------------------------


def _pic(alt: str, title: str = "", pid: int = 7) -> str:
    t = f' title="{title}"' if title else ""
    return (
        f'<w:r><w:drawing><wp:inline><wp:extent cx="1" cy="1"/><wp:docPr id="{pid}" '
        f'name="Pic {pid}" descr="{alt}"{t}/></wp:inline></w:drawing></w:r>'
    )


def _alt_fixture() -> bytes:
    note = (
        '<w:footnote w:id="1"><w:p><w:r><w:footnoteRef/></w:r><w:r><w:t> Note</w:t></w:r>'
        f"{_pic('Note picture', pid=30)}</w:p></w:footnote>"
    )

    def hf(tag, alt, pid):
        return _part_xml(tag, f"<w:p>{_pic(alt, pid=pid)}</w:p>")

    return _docx_with_parts(
        [_wp("Body"), f"<w:p>{_pic('Body picture', 'Body title', 10)}</w:p>"],
        [
            ("word/header2.xml", "header", hf("hdr", "Header two", 21)),
            ("word/header1.xml", "header", hf("hdr", "Header logo", 20)),
            ("word/footer1.xml", "footer", hf("ftr", "Footer logo", 25)),
            ("word/footnotes.xml", "footnotes", _part_xml("footnotes", note)),
        ],
    )


def _descrs(data: bytes, part: str) -> list[str]:
    return list(_part_root(data, part).xpath("//wp:docPr/@descr", namespaces=NS))


def test_alt_text_other_parts():
    """#368 item 3: from stamp 5 the alt text (descr / title) of drawings in headers, footers,
    footnotes, endnotes and comments is extracted, after the body's alt units, in part order,
    recording its part; stamps 4 and 3 keep exporting the body's alt text only."""
    from docx import Document as Docx

    from app.filters import docx_filter
    from app.filters.docx_filter import DocxFilter

    f = DocxFilter()
    data = _alt_fixture()
    res = f.parse(data)
    assert res.skeleton["docx_walk"] == 6
    alt_units = [u for u in res.units if u.style.startswith("a11y:")]
    parts = {u["p_index"]: u["part"] for u in res.skeleton["units"]}
    got = [(u.text, parts[u.p_index]) for u in alt_units]
    assert got == [
        ("Body picture", _MAIN),
        ("Body title", _MAIN),
        ("Header logo", "word/header1.xml"),
        ("Header two", "word/header2.xml"),
        ("Footer logo", "word/footer1.xml"),
        ("Note picture", "word/footnotes.xml"),
    ], got
    n_par = max(u.p_index for u in res.units if not u.style.startswith("a11y:")) + 1
    assert [u.p_index for u in alt_units] == list(range(n_par, n_par + 6)) or (
        min(u.p_index for u in alt_units) >= n_par
    )
    mapping = {str(u.p_index): "DE " + u.text for u in res.units}
    for label, out in (
        ("build", f.build(data, mapping)),
        ("build_localized", f.build_localized(data, mapping, "de-DE")),
    ):
        assert _descrs(out, _MAIN) == ["DE Body picture"], (label, _descrs(out, _MAIN))
        assert _part_root(out).xpath("//wp:docPr/@title", namespaces=NS) == ["DE Body title"]
        assert _descrs(out, "word/header1.xml") == ["DE Header logo"], label
        assert _descrs(out, "word/header2.xml") == ["DE Header two"], label
        assert _descrs(out, "word/footer1.xml") == ["DE Footer logo"], label
        assert _descrs(out, "word/footnotes.xml") == ["DE Note picture"], label
    # stamps 4 and 3: only the body's alt text is a unit; the other parts are never touched
    for walk in (4, 3):
        doc = Docx(io.BytesIO(data))
        n_p = len(docx_filter._all_paragraphs(doc, walk))
        old_alts = docx_filter._alt_units(doc)
        assert [a[2] for a in old_alts] == ["Body picture", "Body title"], walk
        mp = {str(n_p + j): "DE " + a[2] for j, a in enumerate(old_alts)}
        out = f.build(data, mp, walk=walk)
        assert _descrs(out, _MAIN) == ["DE Body picture"], walk
        for part, want in (
            ("word/header1.xml", ["Header logo"]),
            ("word/footer1.xml", ["Footer logo"]),
            ("word/footnotes.xml", ["Note picture"]),
        ):
            assert _descrs(out, part) == want, (walk, part)
    print("  39)  stamp 5: alt text in headers, footers and notes extracted and written; 4, 3 not")


def _smart(inner: str, attrs: str = 'w:uri="urn:x" w:element="place"', pr: str = "") -> str:
    return f"<w:smartTag {attrs}>{pr}{inner}</w:smartTag>"


_OUTER_TAG = 'w:uri="urn:o" w:element="o"'


def test_smart_tags():
    """#368 item 4: from stamp 5 the text of a w:smartTag that sits in a paragraph is text,
    written back inside a re-created smartTag (attributes and smartTagPr kept); under stamp 5
    a nested smartTag stays source, from stamp 6 (#686) it is text too; stamps 4 and 3 leave
    smartTag text alone."""
    from docx import Document as Docx

    from app.filters import docx_filter
    from app.filters.docx_filter import DocxFilter

    f = DocxFilter()
    pr = '<w:smartTagPr><w:attr w:name="k" w:val="v"/></w:smartTagPr>'
    tagged = (
        '<w:p><w:r><w:t xml:space="preserve">Visit </w:t></w:r>'
        f"{_smart('<w:r><w:t>Paris</w:t></w:r>', pr=pr)}<w:r><w:t> now</w:t></w:r></w:p>"
    )
    only = f"<w:p>{_smart('<w:r><w:t>Only tagged</w:t></w:r>')}</w:p>"
    nested = f"<w:p>{_smart(_smart('<w:r><w:t>Inner</w:t></w:r>'), _OUTER_TAG)}</w:p>"
    data = _docx_with_body(tagged, only, nested)
    res = f.parse(data)
    texts = [u.text for u in res.units]
    assert any("Paris" in t and "Visit" in t for t in texts), texts
    assert "Only tagged" in texts, texts
    assert "Inner" in texts, texts  # nested: text from stamp 6 (#686)
    swaps = (
        ("Paris", "Paris-DE"),
        ("Visit", "Besuche"),
        ("Only tagged", "Nur markiert"),
        ("Inner", "Innen"),
    )
    mapping = {}
    for u in res.units:
        t = u.text
        for a, b in swaps:
            t = t.replace(a, b)
        mapping[str(u.p_index)] = t.replace(" now", " jetzt")
    for label, out in (
        ("build", f.build(data, mapping)),
        ("build_localized", f.build_localized(data, mapping, "de-DE")),
    ):
        tags = _part_root(out).findall(".//w:smartTag", NS)
        assert len(tags) == 4, (label, len(tags))  # incl. the nested one
        first = tags[0]
        assert first.get(W("w", "uri")) == "urn:x" and first.get(W("w", "element")) == "place"
        assert "".join(first.itertext()) == "Paris-DE", (label, "".join(first.itertext()))
        assert first[0].tag == W("w", "smartTagPr"), label  # properties lead the tag
        assert first[0].find("w:attr", NS).get(W("w", "val")) == "v"
        assert "".join(tags[1].itertext()) == "Nur markiert", label
        assert "".join(tags[2].itertext()) == "Innen", label  # outer, re-created
        assert "".join(tags[3].itertext()) == "Innen", label  # nested, inside it
        assert tags[3].getparent() is tags[2], label
        assert tags[2].get(W("w", "uri")) == "urn:o" and tags[3].get(W("w", "uri")) == "urn:x"
        assert _paras_of(out)[0] == "Besuche Paris-DE jetzt", (label, _paras_of(out))
    # stamp 5: the nested tag is still one object and keeps its source text
    paras = docx_filter._all_paragraphs(Docx(io.BytesIO(data)), 5)
    mp = {}
    for i, p in enumerate(paras):
        if docx_filter._has_text(p, 5):
            t = docx_filter._tokenize_paragraph(p, 5)[0]
            for a, b in swaps:
                t = t.replace(a, b)
            mp[str(i)] = t
    assert not any("Inn" in t for t in mp.values()), mp
    out = f.build(data, mp, walk=5)
    tags = _part_root(out).findall(".//w:smartTag", NS)
    assert ["".join(t.itertext()) for t in tags] == ["Paris-DE", "Nur markiert", "Inner", "Inner"]
    # stamps 4 and 3: smartTag text is carried, as before
    for walk in (4, 3):
        paras = docx_filter._all_paragraphs(Docx(io.BytesIO(data)), walk)
        mp = {str(i): "T " + p.text for i, p in enumerate(paras) if p.text.strip()}
        out = f.build(data, mp, walk=walk)
        tags = _part_root(out).findall(".//w:smartTag", NS)
        assert ["".join(t.itertext()) for t in tags] == [
            "Paris",
            "Only tagged",
            "Inner",
            "Inner",
        ], walk
    print("  40)  stamp 5: smartTag text translated inside its tag; nested from 6; 4, 3 unchanged")


_ST = 'w:uri="urn:schemas-microsoft-com:office:smarttags" w:element="{}"'
_W6_TAGS = ("smartTag", "hyperlink", "ins", "customXml", "sdt", "dir")


def _r(text: str, bold: bool = False) -> str:
    rpr = "<w:rPr><w:b/></w:rPr>" if bold else ""
    return f'<w:r>{rpr}<w:t xml:space="preserve">{text}</w:t></w:r>'


def _nested_fixture() -> bytes:
    """#686: the shapes the soak's real file (Apache POI smarttag-snippet.docx) has, plus
    the other inline wrappers walk 6 nests through. One sentence per paragraph."""

    def st(el: str, inner: str) -> str:
        return _smart(inner, _ST.format(el))

    meta = 'w:author="Ann" w:date="2026-09-01T10:00:00Z"'
    text_sdt = (
        '<w:sdt><w:sdtPr><w:alias w:val="Name"/><w:id w:val="42"/></w:sdtPr>'
        f"<w:sdtContent>{_r('control ')}{_r('text', True)}</w:sdtContent></w:sdt>"
    )
    list_sdt = (
        '<w:sdt><w:sdtPr><w:id w:val="43"/><w:dropDownList><w:listItem w:displayText="Choice A" '
        'w:value="A"/></w:dropDownList></w:sdtPr>'
        f"<w:sdtContent>{_r('Choice A')}</w:sdtContent></w:sdt>"
    )
    deep = _r("Deep")
    for _ in range(40):
        deep = st("place", deep)
    return _docx_with_body(
        # the real file's first paragraph: smart tags three deep inside a sentence
        "<w:p>"
        + _r("Copied from the ")
        + st(
            "PlaceName",
            st(
                "place",
                st("PlaceName", _r("Carnegie")) + _r(" ") + st("PlaceType", _r("University")),
            ),
        )
        + _r(" site today.")
        + "</w:p>",
        # its heading: City > place
        "<w:p>"
        + st("City", st("place", _r("Alice")))
        + _r("'s Adventures in Wonderland")
        + "</w:p>",
        # a smart tag and an internal hyperlink inside a tracked insertion
        f'<w:p><w:ins w:id="7" {meta}>'
        + _r("Added ")
        + st("City", _r("Boston"))
        + '<w:hyperlink w:anchor="bm">'
        + _r(" link")
        + "</w:hyperlink></w:ins>"
        + _r(" here.")
        + "</w:p>",
        # inline custom XML and a text content control
        "<w:p>" + _wcx(_r("Custom words")) + _r(" and ") + text_sdt + _r(".") + "</w:p>",
        # a drop-down control: its text is a list choice, it stays one object
        "<w:p>" + list_sdt + _r(" stays put.") + "</w:p>",
        '<w:p><w:dir w:val="rtl">' + _r("Directed text") + "</w:dir>" + _r(" end.") + "</w:p>",
        # deeper than MAX_INLINE_DEPTH: the innermost levels stay one object, nothing raises
        "<w:p>" + deep + _r(" tail.") + "</w:p>",
    )


def _upper_text(text: str) -> str:
    """A 'translation' that uppercases the words and keeps every token where it is."""
    out, pos = [], 0
    for m in seg._PLACEHOLDER_RE.finditer(text):
        out.append(text[pos : m.start()].upper())
        out.append(m.group(0))
        pos = m.end()
    out.append(text[pos:].upper())
    return "".join(out)


def _reordered_pairs(text: str, order=None) -> str:
    """The r pairs of a unit in another order (`order`: 0-based pair positions; default
    reversed), a translation that moves runs across wrapper boundaries."""
    spans = [m.group(0) for m in re.finditer(r"⟦r(\d+)⟧.*?⟦/r\1⟧", text)]
    if len(spans) < 2:
        return text
    order = order if order is not None else list(reversed(range(len(spans))))
    return "".join(spans[i] for i in order)


def _wrapper_shape(data: bytes) -> list:
    """Every inline wrapper of the body in document order: its name, attributes, the
    wrappers around it, and its properties element (c14n)."""
    shape = []
    for el in _part_root(data).iter(*(W("w", t) for t in _W6_TAGS)):
        chain = tuple(
            a.tag.rsplit("}", 1)[-1]
            for a in el.iterancestors()
            if a.tag in {W("w", t) for t in _W6_TAGS}
        )
        pr = el.find(W("w", el.tag.rsplit("}", 1)[-1] + "Pr"))
        shape.append(
            (
                el.tag.rsplit("}", 1)[-1],
                sorted(el.attrib.items()),
                chain,
                _c14n(pr) if pr is not None else None,
            )
        )
    return shape


# word/document.xml of the stamp-5 export of _nested_fixture() (build_localized, de-DE, the
# _upper_text translation of the stamp-5 units), produced by next/connectors c07fc408f
# BEFORE walk 6 existed. Stamp 5 must keep producing exactly these bytes.
_W5_NESTED_SHA = "5bc38bd49d8237d2f5c1774e6a79d544d83544629073a2d30034449106d72203"


def test_walk6_gate_686():
    """#686 gate. (a) A complex field whose begin sits in a hyperlink and whose end sits in
    a smart tag INSIDE that hyperlink stays in one hyperlink when the translation moves
    outside text between them (two hyperlinks around one field: Word refuses the file);
    the smart tag is kept. (b) A w:bdo / w:dir around translated text takes the direction
    of that text: an English `bdo ltr` translated into Arabic becomes rtl (Arabic laid out
    left to right reads reversed), a Latin name kept in its override stays ltr, and a
    Hebrew `bdo rtl` translated into English becomes ltr. Stamp 5 never touches them."""
    from app.filters.docx_filter import DocxFilter

    f = DocxFilter()

    def st(el: str, inner: str) -> str:
        return _smart(inner, _ST.format(el))

    fld = (
        '<w:r><w:fldChar w:fldCharType="begin"/></w:r>'
        "<w:r><w:instrText> PAGEREF bm </w:instrText></w:r>"
        '<w:r><w:fldChar w:fldCharType="separate"/></w:r>'
    )
    data = _docx_with_body(
        "<w:p>"
        + _r("Intro ")
        + '<w:hyperlink w:anchor="bm">'
        + fld
        + _r("Chapter one")
        + st("place", _r("Paris") + '<w:r><w:fldChar w:fldCharType="end"/></w:r>')
        + "</w:hyperlink>"
        + _r(" outro")
        + "</w:p>",
        '<w:p><w:bookmarkStart w:id="0" w:name="bm"/>'
        + _r("Target")
        + '<w:bookmarkEnd w:id="0"/></w:p>',
    )
    unit = f.parse(data).units[0].text
    pairs = dict(re.findall(r"⟦r(\d+)⟧(.*?)⟦/r\1⟧", unit))
    xs = re.findall(r"⟦x\d+⟧", unit)
    assert len(pairs) == 4 and len(xs) == 2, unit
    moved = (
        f"{xs[0]}⟦r2⟧{pairs['2']}⟦/r2⟧⟦r1⟧{pairs['1']}⟦/r1⟧⟦r3⟧{pairs['3']}⟦/r3⟧{xs[1]}"
        f"⟦r4⟧{pairs['4']}⟦/r4⟧"
    )
    body = _part_root(f.build(data, {"0": moved}))
    p0 = next(body.iter(W("w", "p")))
    links = p0.findall(W("w", "hyperlink"))
    assert len(links) == 1, [_c14n(x) for x in links]
    kinds = [c.get(W("w", "fldCharType")) for c in links[0].iter(W("w", "fldChar"))]
    assert kinds == ["begin", "separate", "end"], kinds
    assert links[0].find(W("w", "smartTag")) is not None
    assert "".join(t.text for t in p0.iter(W("w", "t"))) == "Chapter oneIntro Paris outro"

    data = _docx_with_body(
        '<w:p><w:bdo w:val="ltr">'
        + _r("Override words")
        + "</w:bdo>"
        + _r(" and ")
        + '<w:dir w:val="ltr">'
        + _r("embedded words")
        + "</w:dir>"
        + _r(".")
        + "</w:p>",
        '<w:p><w:bdo w:val="ltr">' + _r("ACME-7") + "</w:bdo>" + _r(" is the name.") + "</w:p>",
        '<w:p><w:bdo w:val="rtl">' + _r("שלום עולם") + "</w:bdo>" + _r(" סוף.") + "</w:p>",
    )
    units = f.parse(data).units
    ar = {
        "0": "⟦r1⟧كلمات التجاوز⟦/r1⟧⟦r2⟧ و ⟦/r2⟧⟦r3⟧كلمات مضمنة⟦/r3⟧⟦r4⟧.⟦/r4⟧",
        "1": "⟦r1⟧ACME-7⟦/r1⟧⟦r2⟧ هو الاسم.⟦/r2⟧",
        "2": "⟦r1⟧Hello world⟦/r1⟧⟦r2⟧ End.⟦/r2⟧",
    }
    assert [u.text.count("⟦r") for u in units] == [4, 2, 2], [u.text for u in units]

    def dirs(out: bytes) -> list:
        return [
            (el.tag.rsplit("}", 1)[-1], el.get(W("w", "val")))
            for el in _part_root(out).iter(W("w", "bdo"), W("w", "dir"))
        ]

    want = [("bdo", "rtl"), ("dir", "rtl"), ("bdo", "ltr"), ("bdo", "ltr")]
    assert dirs(f.build(data, ar)) == want, dirs(f.build(data, ar))
    assert dirs(f.build_localized(data, ar, "ar-EG")) == want
    # stamp 5 carries the wrappers as objects: their direction is never rewritten
    kept = [("bdo", "ltr"), ("dir", "ltr"), ("bdo", "ltr"), ("bdo", "rtl")]
    assert dirs(f.build_localized(data, {"1": "⟦x1⟧ هو الاسم."}, "ar-EG", walk=5)) == kept
    print("  44)  walk 6: field kept in one hyperlink across a nested tag; bdo/dir follow text")


def test_nested_wrappers_686():
    """#686 (walk stamp 6): text in smart tags nested in smart tags (the real file's
    place > City), in a tracked insertion, hyperlink, inline custom XML, w:dir or a text
    content control is offered for translation and written back inside the SAME chain of
    wrappers; a list control and wrappers deeper than the bound stay objects; the warning
    names only those. A document stored under stamp 5 exports byte-identically to before
    (pinned bytes, and through the real export route without touching the walk-6 code)."""
    import hashlib
    from unittest.mock import patch

    from docx import Document as Docx

    from app.filters import docx_filter
    from app.filters.docx_filter import DocxFilter
    from app.models import Document

    f = DocxFilter()
    data = _nested_fixture()
    res = f.parse(data)
    assert res.skeleton["docx_walk"] == 6, res.skeleton
    texts = [u.text for u in res.units]
    plain = [re.sub(r"⟦[^⟧]*⟧", "", t) for t in texts]
    assert plain[:6] == [
        "Copied from the Carnegie University site today.",
        "Alice's Adventures in Wonderland",
        "Added Boston link here.",
        "Custom words and control text.",
        " stays put.",
        "Directed text end.",
    ], plain
    assert plain[6] == " tail.", plain  # past the bound: one object, not read
    for t in texts[:4] + texts[5:6]:
        assert "⟦x" not in t, t  # no wrapper left as an object
    assert "⟦x" in texts[4] and "⟦x" in texts[6], texts
    # the warning names what walk 6 still carries: the list control and the too-deep tags
    warn = [
        w for w in res.skeleton.get("file_warnings", []) if w["code"] == "docx_untranslated_regions"
    ]
    assert len(warn) == 1 and "content controls" in warn[0]["message"], warn
    assert "smart tags" in warn[0]["message"], warn

    mapping = {str(u.p_index): _upper_text(u.text) for u in res.units}
    for label, out in (
        ("build", f.build(data, mapping)),
        ("build_localized", f.build_localized(data, mapping, "de-DE")),
    ):
        left = [
            t.text
            for t in _part_root(out).iter(W("w", "t"))
            if t.text and re.search("[a-z]", t.text)
        ]
        assert left == ["Choice A", "Deep"], (label, left)
        # every wrapper comes back where it was, attributes and properties kept
        assert _wrapper_shape(out) == _wrapper_shape(data), label
        sdt_pr = _part_root(out).findall(".//w:sdtPr", NS)
        assert [p.find("w:alias", NS) is not None for p in sdt_pr] == [True, False], label
        assert f.parse(out).skeleton["docx_walk"] == 6
    # stamped as the target: the translated runs inside nested tags carry no source lang
    out = f.build_localized(data, mapping, "de-DE")
    for r in _part_root(out).iter(W("w", "r")):
        if "CARNEGIE" in "".join(r.itertext()):
            lang = r.find("w:rPr/w:lang", NS)
            assert lang is None or lang.get(W("w", "val")) != "en-US", _c14n(r)

    # translations that move runs across wrappers: nothing lost, ids stay unique, it opens
    rev = {k: _reordered_pairs(v) for k, v in mapping.items()}
    p_of = [str(u.p_index) for u in res.units]
    # the insertion and the content control each come back in two pieces
    rev[p_of[2]] = _reordered_pairs(mapping[p_of[2]], [0, 3, 1, 2])
    rev[p_of[3]] = _reordered_pairs(mapping[p_of[3]], [0, 2, 1, 3, 4])
    out = f.build_localized(data, rev, "de-DE")
    Docx(io.BytesIO(out))
    root = _part_root(out)
    ins_ids = [e.get(W("w", "id")) for e in root.iter(W("w", "ins"))]
    assert len(ins_ids) >= 2 and len(set(ins_ids)) == len(ins_ids), ins_ids
    assert ins_ids[0] == "7", ins_ids  # the first piece keeps the revision id
    sdt_ids = [e.get(W("w", "val")) for e in root.iterfind(".//w:sdtPr/w:id", NS)]
    assert sdt_ids.count("42") == 1 and sdt_ids.count("43") == 1, sdt_ids
    aliases = root.findall(".//w:sdtPr/w:alias", NS)
    assert len(aliases) == 2, len(aliases)  # the text control in two pieces, one w:id
    # the reversed real-file paragraph keeps every smart tag around its own word
    for word in ("CARNEGIE", "UNIVERSITY", "ALICE"):
        holder = next(t for t in root.iter(W("w", "t")) if t.text == word).getparent()
        assert holder.getparent().tag == W("w", "smartTag"), word
    got = " ".join(_paras_of(out))
    for word in ("CARNEGIE", "UNIVERSITY", "ALICE", "BOSTON", "LINK", "CUSTOM WORDS", "TEXT"):
        assert word in got, (word, got)
    f.parse(out)

    # stamp 5: same numbering, nested text stays source, bytes exactly as before #686
    doc5 = Docx(io.BytesIO(data))
    paras = docx_filter._all_paragraphs(doc5, 5)
    units5 = {
        i: docx_filter._tokenize_paragraph(p, 5)[0]
        for i, p in enumerate(paras)
        if docx_filter._has_text(p, 5)
    }
    assert sorted(units5) == sorted(u.p_index for u in res.units), (units5, res.units)
    assert "Carnegie" not in units5[0] and "⟦x" in units5[0], units5[0]
    map5 = {str(i): _upper_text(t) for i, t in units5.items()}
    out5 = f.build_localized(data, map5, "de-DE", walk=5)
    doc_xml5 = zipfile.ZipFile(io.BytesIO(out5)).read(_MAIN)
    assert hashlib.sha256(doc_xml5).hexdigest() == _W5_NESTED_SHA, hashlib.sha256(
        doc_xml5
    ).hexdigest()
    assert "Carnegie" in "".join(_paras_of(out5)), _paras_of(out5)

    # end to end: the same project stored under stamp 5 exports on the stamp-5 walk
    client = TestClient(app)
    doc_id = _new_docx_doc(client, "stamp5nested", data)
    with Session(engine) as s:
        d = s.get(Document, doc_id)
        assert d.skeleton["docx_walk"] == 6
        d.skeleton = {
            **d.skeleton,
            "docx_walk": 5,
            "units": [{**u, "text": units5[u["p_index"]]} for u in d.skeleton["units"]],
        }
        s.add(d)
        segs = sorted(
            s.exec(
                select(Segment).where(
                    Segment.document_id == doc_id, Segment.target_locale == "de-DE"
                )
            ).all(),
            key=lambda x: x.order_index,
        )
        assert len(segs) == len(units5), [x.source_text for x in segs]
        for x, i in zip(segs, sorted(units5), strict=True):
            x.source_text = units5[i]
            s.add(x)
        s.commit()
        rows = [(x.id, map5[str(i)]) for x, i in zip(segs, sorted(units5), strict=True)]
    for sid, target in rows:
        r = client.post(f"/segments/{sid}/save", data={"target_text": target, "tte_ms": "0"})
        assert r.status_code == 200, r.text
    with patch.object(docx_filter, "_walk_paragraph_v6", side_effect=AssertionError("walk 6 used")):
        resp = client.get(f"/documents/{doc_id}/export?locale=de-DE")
    assert resp.status_code == 200, resp.text[:300]
    got5 = zipfile.ZipFile(io.BytesIO(resp.content)).read(_MAIN)
    assert hashlib.sha256(got5).hexdigest() == _W5_NESTED_SHA, "stamp-5 export changed"
    print(
        "  43)  stamp 6: nested smart tags and inline wrappers translated; stamp 5 byte-identical"
    )


def _fld_simple(instr: str, result: str) -> str:
    return f'<w:fldSimple w:instr="{instr}"><w:r><w:t>{result}</w:t></w:r></w:fldSimple>'


def test_field_and_binding_warnings():
    """#368 items 2 and 4: a simple field whose cached result has words (and is not a field
    Word recomputes itself) is carried as source and named; page numbers are not; a
    data-bound content control with text gets its own warning. fldSimple results are NOT
    translated (Word regenerates them on update)."""
    from app.filters.docx_filter import DocxFilter

    f = DocxFilter()

    def warns(data):
        return {
            (w["code"], w["part"]): w["message"]
            for w in f.parse(data).skeleton.get("file_warnings", [])
            if w["code"] in ("docx_untranslated_regions", "docx_data_bound_controls")
        }

    plain = _docx_with_body(
        _wp("Lead"),
        f"<w:p><w:r><w:t>Page </w:t></w:r>{_fld_simple(' PAGE ', '3')}"
        f"<w:r><w:t> of </w:t></w:r>{_fld_simple(' NUMPAGES ', '9')}"
        f"{_fld_simple(' DATE ', 'October 2')}</w:p>",
    )
    assert warns(plain) == {}, warns(plain)
    named = _docx_with_body(
        "<w:p><w:r><w:t>Hello </w:t></w:r>"
        f"{_fld_simple(' MERGEFIELD Name ', 'Customer name')}</w:p>"
    )
    got = warns(named)
    assert got == {
        ("docx_untranslated_regions", _MAIN): "Untranslated DOCX regions: simple field results. "
        "Review these in Word before delivery."
    }, got
    # carried as source on export: the result is not text the walk writes
    res = f.parse(named)
    assert [u.text.startswith("Hello") for u in res.units] == [True], [u.text for u in res.units]
    out = f.build(named, {str(res.units[0].p_index): res.units[0].text.replace("Hello", "Hallo")})
    fld = _part_root(out).find(".//w:fldSimple", NS)
    assert "".join(fld.itertext()) == "Customer name" and "MERGEFIELD" in fld.get(W("w", "instr"))
    # the same field in a header is named for that part
    hdr = _docx_with_parts(
        [_wp("Body")],
        [
            (
                "word/header1.xml",
                "header",
                _part_xml("hdr", f"<w:p>{_fld_simple(' TITLE ', 'Doc title')}</w:p>"),
            )
        ],
    )
    assert ("docx_untranslated_regions", "word/header1.xml") in warns(hdr), warns(hdr)

    # a smart tag is named only where walk 6 still carries it (#686: nested ones are read)
    plain_tag = _docx_with_body(f"<w:p>{_smart('<w:r><w:t>Paris</w:t></w:r>')}</w:p>")
    assert warns(plain_tag) == {}, warns(plain_tag)
    nested_tag = _docx_with_body(
        f"<w:p>{_smart(_smart('<w:r><w:t>Inner</w:t></w:r>'), _OUTER_TAG)}</w:p>"
    )
    assert warns(nested_tag) == {}, warns(nested_tag)
    in_field = _docx_with_body(
        '<w:p><w:fldSimple w:instr=" PAGE ">'
        f"{_smart('<w:r><w:t>Inner</w:t></w:r>')}</w:fldSimple></w:p>"
    )
    assert "smart tags" in warns(in_field).get(("docx_untranslated_regions", _MAIN), ""), warns(
        in_field
    )

    # data binding
    bound = (
        '<w:sdt><w:sdtPr><w:dataBinding w:prefixMappings="" w:xpath="/a/b" '
        'w:storeItemID="STORE0"/></w:sdtPr>'
        "<w:sdtContent>{}</w:sdtContent></w:sdt>"
    )
    data = _docx_with_body(_wp("Lead"), bound.format(_wp("Bound text")))
    got = warns(data)
    assert list(got) == [("docx_data_bound_controls", _MAIN)], got
    assert "refill" in got[("docx_data_bound_controls", _MAIN)]
    # block control: still translated (warning only, extraction unchanged)
    assert any(u.text == "Bound text" for u in f.parse(data).units)
    # empty bound control, and an unbound one with text: no warning
    assert warns(_docx_with_body(_wp("Lead"), bound.format("<w:p/>"))) == {}
    assert warns(_docx_with_body(_wp("Lead"), _wsdt(_wp("Unbound text")))) == {}
    # a bound control in a footer names that part
    ftr_bound = _docx_with_parts(
        [_wp("Body")],
        [("word/footer1.xml", "footer", _part_xml("ftr", bound.format(_wp("Footer bound"))))],
    )
    assert ("docx_data_bound_controls", "word/footer1.xml") in warns(ftr_bound)
    # gate: a bound control in the glossary (building blocks nobody translates) is not
    # reported as one Word would refill over a translation; the part itself is named
    gl_bound = _docx_with_parts(
        [_wp("Body")],
        [
            (
                "word/glossary/document.xml",
                "glossaryDocument",
                _part_xml("glossaryDocument", bound.format(_wp("Cover title"))),
            )
        ],
    )
    got = warns(gl_bound)
    assert ("docx_data_bound_controls", "word/glossary/document.xml") not in got, got
    assert ("docx_untranslated_regions", "word/glossary/document.xml") in got, got
    print("  41)  warnings: carried fldSimple results and smart tags named; data-bound controls")


def test_reingest_recomputes_file_info():
    """#368 item 1: the repair re-ingest recomputes file_warnings and a11y_images, so a
    repaired document shows what a fresh upload of the same bytes shows (stale ones go)."""
    from app.models import Document
    from app.services import ingest

    stale = [
        {
            "code": "docx_untranslated_regions",
            "part": _MAIN,
            "kind": "untranslated",
            "message": "STALE",
        }
    ]
    # 1) the file has no warning and no images: stale values are removed
    clean = _docx_with_body(_wp("Clean text"))
    doc_id = _new_docx_doc(TestClient(app), "repair-clean", clean)
    with Session(engine) as s:
        d = s.get(Document, doc_id)
        d.skeleton = {
            **d.skeleton,
            "file_warnings": stale,
            "a11y_images": {"images": 3, "missing_alt": [{"unit": 0, "kind": "missing"}]},
        }
        s.add(d)
        s.commit()
    assert ingest.reingest_document(doc_id) is True
    with Session(engine) as s:
        sk = s.get(Document, doc_id).skeleton
        assert "file_warnings" not in sk and "a11y_images" not in sk, sk
    # 2) the file has a warning and an image without alt text the stale skeleton lacks
    named = _docx_with_body(
        "<w:p><w:r><w:t>Hello </w:t></w:r>"
        f"{_fld_simple(' MERGEFIELD N ', 'Customer name')}"
        '<w:r><w:drawing><wp:inline><wp:extent cx="1" cy="1"/><wp:docPr id="5" name="P"/>'
        "</wp:inline></w:drawing></w:r></w:p>"
    )
    doc_id = _new_docx_doc(TestClient(app), "repair-named", named)
    with Session(engine) as s:
        d = s.get(Document, doc_id)
        sk = {
            k: v
            for k, v in d.skeleton.items()
            if k not in ("file_warnings", "a11y_images", "a11y_alt_flags")
        }
        d.skeleton = {**sk, "docx_walk": 4}
        s.add(d)
        s.commit()
    assert ingest.reingest_document(doc_id) is True
    with Session(engine) as s:
        sk = s.get(Document, doc_id).skeleton
        assert sk["docx_walk"] == 6, sk
        assert [w["code"] for w in sk["file_warnings"]] == ["docx_untranslated_regions"], sk
        assert sk["a11y_images"]["images"] == 1 and sk["a11y_images"]["missing_alt"], sk
        # gate: the Editor/API flags follow the fresh findings (not left absent or stale)
        from app.filters import a11y as _a11y

        assert _a11y.a11y_summary(sk)["missing_alt"], sk
    # 3) same walk: the stale message is replaced by the fresh one
    with Session(engine) as s:
        d = s.get(Document, doc_id)
        d.skeleton = {**d.skeleton, "file_warnings": stale}
        s.add(d)
        s.commit()
    assert ingest.reingest_document(doc_id) is True
    with Session(engine) as s:
        sk = s.get(Document, doc_id).skeleton
        assert all(w["message"] != "STALE" for w in sk["file_warnings"]), sk
    # 4) gate: a repair builds what an upload builds, in every format. A full-text body
    # leaves the alt text out (else export quarantines a paragraph-count desync), and a
    # segmented alt unit keeps its Editor note.
    client = TestClient(app)
    alt_md = b"Body text here.\n\n![Company logo](logo.png)\n\nMore body.\n"
    alt_docx = _docx_with_body(_wp("Body text here.") + f"<w:p>{_pic('Company logo')}</w:p>")
    for fmt, data, ctype in (("md", alt_md, "text/markdown"), ("docx", alt_docx, _DOCX_CTYPE)):
        for mode in ("full_text", "segmented"):
            r = client.post(
                "/projects",
                data={
                    "name": f"repair-{fmt}-{mode}",
                    "source_locale": "en-US",
                    "target_locales": "de-DE",
                    "default_provider": "claude",
                },
                follow_redirects=False,
            )
            pid = int(r.headers["location"].rsplit("/", 1)[-1])
            r = client.post(
                f"/projects/{pid}/documents",
                data={"mode": mode},
                files={"file": ("a." + fmt, data, ctype)},
                follow_redirects=False,
            )
            doc_id = int(r.headers["location"].rsplit("/", 1)[-1])

            def rows(doc_id=doc_id):
                with Session(engine) as s:
                    segs = s.exec(
                        select(Segment)
                        .where(Segment.document_id == doc_id)
                        .order_by(Segment.order_index)
                    ).all()
                    return [(x.source_text, x.meta) for x in segs]

            fresh = rows()
            if fmt == "docx":  # also across a restamp
                with Session(engine) as s:
                    d = s.get(Document, doc_id)
                    d.skeleton = {**d.skeleton, "docx_walk": 3}
                    s.add(d)
                    s.commit()
            assert ingest.reingest_document(doc_id) is True
            assert rows() == fresh, (fmt, mode, rows(), fresh)
            with Session(engine) as s:
                for x in s.exec(select(Segment).where(Segment.document_id == doc_id)).all():
                    x.target_text = "DE " + x.source_text
                    s.add(x)
                s.commit()
            resp = client.get(f"/documents/{doc_id}/export?locale=de-DE")
            assert resp.status_code == 200, (fmt, mode, resp.text[:300])
    print("  42)  repair re-ingest recomputes file_warnings and a11y_images")


def main() -> None:
    init_db()
    test_security_regressions()
    src, body = test_fixtures()
    test_lost_and_duplicated_tokens(src, body)
    test_legacy_translation(src, body)
    test_hyperlinks()
    test_field_reorder(src, body)
    test_field_in_link()
    test_language_scope()
    test_legacy_r_numbering()
    test_end_to_end()
    test_resource_warning_surfaces()
    test_po()
    test_parse_speed_budget()
    test_no_default_style_parses()
    test_style_names_exact()
    test_quote_in_style_id_parses()
    test_malformed_style_entry_parses()
    test_vmerge_parse_bound()
    test_vmerge_text_once()
    test_vmerge_export_round_trip()
    test_table_walk_unchanged()
    test_legacy_skeleton_export()
    test_walk_stamp_and_selection()
    test_reingest_restamps()
    test_unknown_walk_stamp_refuses()
    test_hostile_style_ids_and_first_wins()
    test_nested_table_extracted()
    test_sdt_customxml_wrappers()
    test_nested_with_vmerge()
    test_previous_stamp_exports_unchanged()
    test_walk_hostile_and_bounded()
    test_unknown_stamp_export_route()
    test_reading_order()
    test_extra_parts_roundtrip()
    test_text_boxes()
    test_tracked_changes()
    test_previous_stamps_skip_new_parts()
    test_parts_hostile()
    test_untranslated_regions_warning()
    test_parts_end_to_end()
    test_moves_split_revisions_nested_insertions()
    test_alt_text_other_parts()
    test_smart_tags()
    test_field_and_binding_warnings()
    test_reingest_recomputes_file_info()
    test_nested_wrappers_686()
    test_walk6_gate_686()
    print("DOCX PO FIDELITY OK")


if __name__ == "__main__":
    main()
