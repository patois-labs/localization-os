"""Visio .vsdx / .vsdm document filter (file-format Batch 16).

Spec checked: [MS-VSDX] Text_Type / cp / pp / tp / fld and Page_Type Name / NameU
(learn.microsoft.com/en-us/openspecs/sharepoint_protocols/ms-vsdx/). Fixtures are
hand-written packages built in this file.

  walk       page names (Name, never NameU) in pages.xml order, then each page's shape text
             and shape data labels in document order; pages resolved through pages.xml.rels
             (deliberately out of part-number order); group sub-shapes; masters ignored
  tokens     cp / pp / tp / fld become ⟦xN⟧; edge markers and the closing "\n" stay outside
  identity   a build with no or unchanged translations returns the original bytes
  round trip translated build: markers verbatim at the token positions, field content kept,
             untouched parts byte-identical, re-parse yields the targets
  vsdm       vbaProject.bin and [Content_Types].xml copied byte for byte
  tokens     a dropped marker is appended, a duplicate or foreign token dropped
  rtl        #740: a right-to-left target sets Paragraph Flags=1 on translated shapes
             (literal cells, sparse rows / section added); LTR targets and build() unchanged
  hostile    DOCTYPE / entity, zip bomb ratio, not a zip, no pages, segment cap, markup budget

Hermetic + offline. Final line: "VSDX FILTER OK".
"""

import io
import os
import sys
import zipfile

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(_ROOT)
sys.path.insert(0, _ROOT)
from tests._bootstrap import init_env

init_env("loctms_vsdx_")

from app.filters import get_filter
from app.filters.base import InputLimitError

NS = "http://schemas.microsoft.com/office/visio/2012/main"
R = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
PAGE_REL = "http://schemas.microsoft.com/visio/2010/relationships/page"

CT_VSDX = (
    "<?xml version='1.0' encoding='utf-8' standalone='yes'?>"
    "<Types xmlns='http://schemas.openxmlformats.org/package/2006/content-types'>"
    "<Default Extension='rels' "
    "ContentType='application/vnd.openxmlformats-package.relationships+xml'/>"
    "<Default Extension='xml' ContentType='application/xml'/>"
    "<Override PartName='/visio/document.xml' ContentType='{main}'/>"
    "<Override PartName='/visio/pages/pages.xml' ContentType='application/vnd.ms-visio.pages+xml'/>"
    "<Override PartName='/visio/pages/page1.xml' ContentType='application/vnd.ms-visio.page+xml'/>"
    "<Override PartName='/visio/pages/page2.xml' ContentType='application/vnd.ms-visio.page+xml'/>"
    "</Types>"
)

PAGES = (
    "<?xml version='1.0' encoding='utf-8' ?>\r\n"
    f"<Pages xmlns='{NS}' xmlns:r='{R}' xml:space='preserve'>"
    "<Page ID='4' NameU='Overview' Name='Overview' ViewScale='1'>"
    "<PageSheet/><Rel r:id='rId2'/></Page>"
    "<Page ID='0' NameU='Page-1' Name='Process details'><PageSheet/><Rel r:id='rId1'/></Page>"
    "</Pages>"
)

PAGES_RELS = (
    "<?xml version='1.0' encoding='utf-8' ?>"
    "<Relationships xmlns='http://schemas.openxmlformats.org/package/2006/relationships'>"
    f"<Relationship Id='rId1' Type='{PAGE_REL}' Target='page1.xml'/>"
    f"<Relationship Id='rId2' Type='{PAGE_REL}' Target='page2.xml'/>"
    "</Relationships>"
)

PAGE1 = (
    "<?xml version='1.0' encoding='utf-8' ?>\r\n"
    f"<PageContents xmlns='{NS}' xmlns:r='{R}' xml:space='preserve'><Shapes>"
    "<Shape ID='1' Type='Shape' Master='2'><Cell N='PinX' V='1'/>"
    "<Section N='Property'><Row N='Owner'><Cell N='Value' V='Ops' U='STR'/>"
    "<Cell N='Label' V='Process owner'/><Cell N='Prompt' V=''/></Row>"
    "<Row N='Calc'><Cell N='Label' V='Computed' F='Sheet.5!Prop.X'/></Row></Section>"
    "<Text><cp IX='0'/><pp IX='0'/>Review the <cp IX='1'/>request<cp IX='0'/>"
    " by %1$s\n</Text></Shape>"
    "<Shape ID='2' Type='Group'><Shapes>"
    "<Shape ID='3' Type='Shape'><Text>Printed on <fld IX='0'>10/10/2026</fld>\n</Text></Shape>"
    "<Shape ID='4' Type='Shape'><Text><fld IX='0'>10/10/2026</fld></Text></Shape>"
    "<Shape ID='5' Type='Shape'><Text>  42  </Text></Shape>"
    "</Shapes></Shape>"
    "</Shapes></PageContents>"
)

PAGE2 = (
    "<?xml version='1.0' encoding='utf-8' ?>\r\n"
    f"<PageContents xmlns='{NS}' xmlns:r='{R}' xml:space='preserve'><Shapes>"
    "<Shape ID='7' Type='Shape'><Text>Start</Text></Shape>"
    "</Shapes></PageContents>"
)

MASTER = (
    "<?xml version='1.0' encoding='utf-8' ?>"
    f"<MasterContents xmlns='{NS}'><Shapes><Shape ID='5'><Text>Master default</Text></Shape>"
    "</Shapes></MasterContents>"
)


def package(pages=PAGES, page1=PAGE1, page2=PAGE2, vba=False, extra=None) -> bytes:
    main = (
        "application/vnd.ms-visio.drawing.macroEnabled.main+xml"
        if vba
        else "application/vnd.ms-visio.drawing.main+xml"
    )
    parts = [
        ("[Content_Types].xml", CT_VSDX.replace("{main}", main)),
        (
            "_rels/.rels",
            "<Relationships xmlns='http://schemas.openxmlformats.org/package/2006/relationships'/>",
        ),
        ("visio/document.xml", f"<VisioDocument xmlns='{NS}'/>"),
        ("visio/pages/pages.xml", pages),
        ("visio/pages/_rels/pages.xml.rels", PAGES_RELS),
        ("visio/pages/page1.xml", page1),
        ("visio/pages/page2.xml", page2),
        ("visio/masters/master1.xml", MASTER),
    ]
    if vba:
        parts.append(("visio/vbaProject.bin", b"\xd0\xcf\x11\xe0VBA-PROJECT-BYTES" * 4))
    parts += list(extra or [])
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, data in parts:
            zf.writestr(name, data if isinstance(data, bytes) else data.encode("utf-8"))
    return out.getvalue()


F = get_filter("vsdx")


def members(data: bytes) -> dict:
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        return {i.filename: zf.read(i) for i in zf.infolist()}


def refused(data: bytes, needle: str) -> None:
    try:
        F.parse(data)
    except InputLimitError as exc:
        assert needle.lower() in str(exc).lower(), exc
        return
    raise AssertionError(f"not refused: {needle}")


def test_registration():
    assert get_filter("vsdm") is F and F.fmt == "vsdx"
    from app.filters import supported_exts

    assert {"vsdx", "vsdm"} <= set(supported_exts())
    print("  [register] vsdx + vsdm alias OK")


def test_walk_and_tokens():
    res = F.parse(package())
    got = [(u.p_index, u.text, u.style) for u in res.units]
    assert got == [
        (0, "Overview", "Visio page name"),
        (1, "Process details", "Visio page name"),
        (2, "Start", "Visio shape text"),  # page2.xml first: pages.xml order via rels
        (3, "Process owner", "Visio shape data label"),
        (4, "Review the ⟦x1⟧request⟦x2⟧ by %1$s", "Visio shape text"),
        (5, "Printed on ⟦x1⟧", "Visio shape text"),
        # 6: field-only text is walked but not a unit
        (7, "42", "Visio shape text"),  # edge whitespace outside the unit (json_filter rule)
    ], got
    assert all("Master default" not in u.text for u in res.units)
    assert res.skeleton["units"][2]["part"] == "visio/pages/page2.xml"
    print("  [walk] page names, rels order, labels, groups, cp/fld tokens, skips OK")


def test_identity():
    src = package()
    assert F.build(src, {}) == src
    same = {str(u.p_index): u.text for u in F.parse(src).units}
    assert F.build(src, same) == src
    assert F.build(src, {"99": "x", "nope": "y", "0": None}) == src
    print("  [identity] empty / unchanged / out-of-range builds return original bytes OK")


def test_gate_nameu_and_langid():
    # A page with no NameU keeps its language-independent name when Name is translated;
    # a localized export sets the translated shape's literal Character LangID cells.
    pages = PAGES.replace("NameU='Page-1' ", "")
    page2 = PAGE2.replace(
        "<Shape ID='7' Type='Shape'><Text>",
        "<Shape ID='7' Type='Shape'><Section N='Character'><Row IX='0'>"
        "<Cell N='LangID' V='1033'/></Row><Row IX='1'><Cell N='LangID' V='1033' F='Inh'/>"
        "</Row></Section><Text>",
    )
    src = package(pages=pages, page2=page2)
    out = F.build_localized(src, {"1": "Prozessdetails", "2": "Anfang"}, "de-DE")
    m = members(out)
    assert b'NameU="Process details"' in m["visio/pages/pages.xml"], m["visio/pages/pages.xml"]
    assert b'Name="Prozessdetails"' in m["visio/pages/pages.xml"]
    p2 = m["visio/pages/page2.xml"]
    assert b'<Cell N="LangID" V="1031"/>' in p2, p2
    assert b'V="1033" F="Inh"' in p2, "a formula cell is left alone"
    # plain build() never stamps; an untranslated localized build is the original
    assert b'V="1033"/>' in members(F.build(src, {"2": "Anfang"}))["visio/pages/page2.xml"]
    assert F.build_localized(src, {}, "de-DE") == src
    print("  [gate] NameU kept on rename, Character LangID stamped on export OK")


RTL_PAGE1 = PAGE1.replace(
    "<Shape ID='1' Type='Shape' Master='2'><Cell N='PinX' V='1'/>",
    "<Shape ID='1' Type='Shape' Master='2'><Cell N='PinX' V='1'/>"
    "<Section N='Character'><Row IX='0'><Cell N='LangID' V='en-US'/></Row></Section>"
    "<Section N='Paragraph'><Row IX='0'><Cell N='HorzAlign' V='0'/><Cell N='Flags' V='0'/>"
    "</Row><Row IX='1'><Cell N='Flags' V='0' F='GUARD(0)'/></Row>"
    "<Row IX='3'><Cell N='SpLine' V='-1.2'/></Row></Section>",
).replace("request<cp IX='0'/>", "request<cp IX='0'/><pp IX='1'/><pp IX='2'/>")
# gate: rows written out of IX order, and an F="Inh" Flags cell (inherited value, not a formula)
RTL_PAGE1_GATE = RTL_PAGE1.replace(
    "<Section N='Paragraph'><Row IX='0'><Cell N='HorzAlign' V='0'/><Cell N='Flags' V='0'/>"
    "</Row><Row IX='1'><Cell N='Flags' V='0' F='GUARD(0)'/></Row>"
    "<Row IX='3'><Cell N='SpLine' V='-1.2'/></Row></Section>",
    "<Section N='Paragraph'><Row IX='3'><Cell N='SpLine' V='-1.2'/></Row>"
    "<Row IX='0'><Cell N='Flags' V='0' F='Inh'/></Row>"
    "<Row IX='1'><Cell N='HorzAlign' V='1'/></Row></Section>",
)


def _para(xml: str, shape_id: str) -> str:
    start = xml.index(f'<Shape ID="{shape_id}"')
    body = xml[start : xml.index("</Shape>", start)]
    if '<Section N="Paragraph">' not in body:
        return ""
    return body[
        body.index('<Section N="Paragraph">') : body.index(
            "</Section>", body.index('<Section N="Paragraph">')
        )
    ]


def test_rtl_paragraph_flags_740():
    # #740: a right-to-left target sets the Paragraph section Flags cell (1 = right to left,
    # learn.microsoft.com/en-us/office/client-developer/visio/flags-cell-paragraph-section) on
    # every translated shape: literal cells only, a sparse row added for each paragraph row
    # the shape's text uses (row 0 and every <pp IX>) that the shape does not have locally,
    # a Paragraph section added when the shape has none. RTLText is not written (VisioSchema15
    # does not list it; the ShapeSheet reference calls it "not implemented").
    src = package(page1=RTL_PAGE1)
    targets = {"2": "התחלה", "4": "סקור את ⟦x1⟧הבקשה⟦x2⟧ על ידי %1$s"}
    for loc in ("he-IL", "ar-SA", "fa"):
        out = F.build_localized(src, targets, loc)
        m = members(out)
        p1 = m["visio/pages/page1.xml"].decode("utf-8")
        para = _para(p1, "1")
        assert '<Row IX="0"><Cell N="HorzAlign" V="0"/><Cell N="Flags" V="1"/></Row>' in para, (
            loc,
            para,
        )
        assert '<Row IX="1"><Cell N="Flags" V="0" F="GUARD(0)"/></Row>' in para, (loc, para)
        assert '<Row IX="2"><Cell N="Flags" V="1"/></Row><Row IX="3">' in para, (loc, para)
        assert '<Row IX="3"><Cell N="SpLine" V="-1.2"/></Row>' in para, "row 3 unused: kept"
        assert "RTLText" not in p1
        assert _para(p1, "5") == "", "an untranslated shape gets nothing"
        p2 = m["visio/pages/page2.xml"].decode("utf-8")
        assert (
            '<Shape ID="7" Type="Shape"><Section N="Paragraph"><Row IX="0">'
            '<Cell N="Flags" V="1"/></Row></Section><Text>התחלה</Text>'
        ) in p2, (loc, p2)
        for name in members(src):
            if name not in ("visio/pages/page1.xml", "visio/pages/page2.xml"):
                assert members(src)[name] == m[name], name
    # left-to-right targets and plain build() write no direction
    for out in (F.build_localized(src, targets, "de-DE"), F.build(src, targets)):
        m = members(out)
        assert b'<Cell N="Flags" V="1"/>' not in m["visio/pages/page1.xml"]
        assert b'N="Paragraph"' not in m["visio/pages/page2.xml"]
    assert F.build_localized(src, {}, "he-IL") == src
    # gate: F="Inh" means inherited (the master / style says left to right), so it becomes a
    # local 1 like an absent cell; a missing row goes after the highest lower IX even when
    # the file lists rows out of order
    out = F.build_localized(package(page1=RTL_PAGE1_GATE), targets, "he-IL")
    para = _para(members(out)["visio/pages/page1.xml"].decode("utf-8"), "1")
    assert '<Row IX="0"><Cell N="Flags" V="1"/></Row>' in para, para
    assert "Inh" not in para, para
    assert (
        '<Row IX="1"><Cell N="HorzAlign" V="1"/><Cell N="Flags" V="1"/></Row>'
        '<Row IX="2"><Cell N="Flags" V="1"/></Row>'
    ) in para, para
    assert para.index('<Row IX="3">') < para.index('<Row IX="0">'), "existing order kept"
    print("  [#740] RTL: Paragraph Flags=1 on translated shapes, formulas kept, LTR unchanged OK")


def test_round_trip():
    src = package(vba=True)
    targets = {
        "1": "Prozessdetails",
        "3": "Prozessverantwortlicher",
        "4": "Prüfen Sie die ⟦x1⟧Anfrage⟦x2⟧ bis %1$s",
        "5": "Gedruckt am ⟦x1⟧ <ok> & fertig",
    }
    out = F.build(src, targets)
    a, b = members(src), members(out)
    assert list(a) == list(b), "member order changed"
    for name in a:
        if name not in ("visio/pages/pages.xml", "visio/pages/page1.xml"):
            assert a[name] == b[name], name  # page2, masters, vba, content types untouched
    p1 = b["visio/pages/page1.xml"].decode("utf-8")
    assert (
        '<Text><cp IX="0"/><pp IX="0"/>Prüfen Sie die <cp IX="1"/>Anfrage<cp IX="0"/>'
        " bis %1$s\n</Text>"
    ) in p1, p1
    assert 'Gedruckt am <fld IX="0">10/10/2026</fld> &lt;ok&gt; &amp; fertig\n</Text>' in p1, p1
    assert 'V="Prozessverantwortlicher"' in p1 and 'F="Sheet.5!Prop.X"' in p1
    pg = b["visio/pages/pages.xml"].decode("utf-8")
    assert 'NameU="Page-1" Name="Prozessdetails"' in pg and 'NameU="Overview"' in pg
    assert pg.startswith("<?xml version='1.0' encoding='UTF-8'?>"), pg[:60]
    again = {u.p_index: u.text for u in F.parse(out).units}
    for k, v in targets.items():
        assert again[int(k)] == v, (k, again[int(k)])
    assert again[0] == "Overview" and again[2] == "Start"
    print("  [round trip] markers, field, label, page name; vba + other parts byte-identical OK")


def test_token_damage():
    src = package()
    out = F.build(src, {"4": "⟦x2⟧Nur⟦x2⟧ Text ⟦g9⟧⟦x7⟧"})
    p1 = members(out)["visio/pages/page1.xml"].decode("utf-8")
    # x2 first use only, foreign tokens dropped, missing x1 appended before the suffix "\n"
    assert '<pp IX="0"/><cp IX="0"/>Nur Text <cp IX="1"/>\n</Text>' in p1, p1
    out = F.build(src, {"5": "Gedruckt"})
    p1 = members(out)["visio/pages/page1.xml"].decode("utf-8")
    assert '<Text>Gedruckt<fld IX="0">10/10/2026</fld>\n</Text>' in p1, p1
    out = F.build(src, {"2": "Bad \x01 char\x0bline"})
    assert "<Text>Bad  char\nline</Text>" in members(out)["visio/pages/page2.xml"].decode()
    print(
        "  [tokens] duplicate/foreign dropped, missing marker and field appended,"
        " XML-illegal cleaned OK"
    )


def test_no_pages_xml_fallback():
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as zf:
        zf.writestr("visio/pages/page10.xml", PAGE2.replace("Start", "Ten"))
        zf.writestr("visio/pages/page2.xml", PAGE2.replace("Start", "Two"))
    texts = [u.text for u in F.parse(out.getvalue()).units]
    assert texts == ["Two", "Ten"], texts
    print("  [fallback] no pages.xml: page<N>.xml in numeric order OK")


def test_hostile():
    from app import config

    bomb = PAGE2.replace(
        "<?xml version='1.0' encoding='utf-8' ?>\r\n",
        "<?xml version='1.0'?><!DOCTYPE l [<!ENTITY a 'aaaa'>]>",
    ).replace("Start", "&a;")
    refused(package(page2=bomb), "DOCTYPE")
    refused(package(page2="<PageContents"), "not valid XML")
    refused(b"not a zip at all", "not a valid")
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as zf:
        zf.writestr("word/document.xml", "<x/>")
    refused(out.getvalue(), "no drawing pages")
    zeros = package(
        extra=[("visio/media/huge.bin", b"\x00" * (config.ZIP_RATIO_MIN_BYTES * 2 + 10))]
    )
    try:
        F.parse(zeros)
        raise AssertionError("zip bomb not refused")
    except InputLimitError as exc:
        assert "expands" in str(exc), exc
    old_cap = config.MAX_DOCUMENT_SEGMENTS
    try:
        config.MAX_DOCUMENT_SEGMENTS = 3
        refused(package(), "more than 3 segments")  # running cap inside the walk
        config.MAX_DOCUMENT_SEGMENTS = 1
        refused(package(), "far more shapes or markup")  # markup budget before lxml
    finally:
        config.MAX_DOCUMENT_SEGMENTS = old_cap
    long_page = PAGE2.replace("Start", "x" * (config.MAX_SEGMENT_CHARS + 1))
    refused(package(page2=long_page), "MAX_SEGMENT_CHARS")
    print("  [hostile] DOCTYPE, bad XML, not zip, no pages, zip bomb, caps OK")


def test_end_to_end_web_upload_and_export():
    from fastapi.testclient import TestClient
    from sqlmodel import Session, select

    from app.db import engine, init_db
    from app.main import app
    from app.models import Document, Segment
    from app.services import export

    init_db()
    client = TestClient(app)
    r = client.post(
        "/projects",
        data={
            "name": "vsdx-e2e",
            "source_locale": "en-US",
            "target_locales": "de-DE",
            "default_provider": "mock",
        },
        follow_redirects=False,
    )
    pid = int(r.headers["location"].rsplit("/", 1)[-1])
    src = package(vba=True)
    r = client.post(
        f"/projects/{pid}/documents",
        data={"mode": "segmented"},
        files={"file": ("flow.vsdm", src, "application/octet-stream")},
        follow_redirects=False,
    )
    assert r.status_code in (302, 303), (r.status_code, r.text[:300])
    doc_id = int(r.headers["location"].rsplit("/", 1)[-1].split("?")[0])
    with Session(engine) as s:
        segs = s.exec(
            select(Segment)
            .where(Segment.document_id == doc_id, Segment.target_locale == "de-DE")
            .order_by(Segment.order_index)
        ).all()
        sources = [g.source_text for g in segs]
        assert "Start" in sources and "Overview" in sources, sources
        for g in segs:
            if g.source_text == "Start":
                g.target_text = "Beginn"
                g.status = "mt"
                s.add(g)
        s.commit()
        doc = s.get(Document, doc_id)
        out = export.build_translated_file(s, doc, "de-DE")
    a, b = members(src), members(out)
    assert b"<Text>Beginn</Text>" in b["visio/pages/page2.xml"]
    assert a["visio/vbaProject.bin"] == b["visio/vbaProject.bin"]
    assert a["[Content_Types].xml"] == b["[Content_Types].xml"]
    print("  [e2e] web upload of a .vsdm exports the translation, macros untouched OK")


def main():
    test_registration()
    test_walk_and_tokens()
    test_identity()
    test_gate_nameu_and_langid()
    test_rtl_paragraph_flags_740()
    test_round_trip()
    test_token_damage()
    test_no_pages_xml_fallback()
    test_hostile()
    test_end_to_end_web_upload_and_export()
    print("VSDX FILTER OK")


if __name__ == "__main__":
    main()
