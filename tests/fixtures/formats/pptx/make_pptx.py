"""Hand-written minimal PresentationML packages for verify_pptx_filter (stdlib only).

Every part is written out below as literal XML (ECMA-376 Part 1, PresentationML and
DrawingML), small enough to read. Nothing is copied from another project's test data.
`make_pptx()` returns the bytes of one package; keyword switches add the macro project,
change the main content type (template / show / macro variants) or swap in hostile parts.
"""

import io
import zipfile

NS = (
    'xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main" '
    'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships" '
    'xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main"'
)
DECL = '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
RELS_NS = "http://schemas.openxmlformats.org/package/2006/relationships"
RT = "http://schemas.openxmlformats.org/officeDocument/2006/relationships/"

MAIN_TYPES = {
    "pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation.main+xml",
    "ppsx": "application/vnd.openxmlformats-officedocument.presentationml.slideshow.main+xml",
    "potx": "application/vnd.openxmlformats-officedocument.presentationml.template.main+xml",
    "pptm": "application/vnd.ms-powerpoint.presentation.macroEnabled.main+xml",
    "ppsm": "application/vnd.ms-powerpoint.slideshow.macroEnabled.main+xml",
    "potm": "application/vnd.ms-powerpoint.template.macroEnabled.main+xml",
}

VBA_BYTES = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + bytes(range(256)) * 4  # an OLE-looking blob


def _rels(*rels):
    body = "".join(
        f'<Relationship Id="{rid}" Type="{typ}" Target="{tgt}"{extra}/>'
        for rid, typ, tgt, extra in rels
    )
    return f'{DECL}<Relationships xmlns="{RELS_NS}">{body}</Relationships>'


def _sp(sid, name, paras_xml, descr=""):
    d = f' descr="{descr}"' if descr else ""
    return (
        f'<p:sp><p:nvSpPr><p:cNvPr id="{sid}" name="{name}"{d}/><p:cNvSpPr/><p:nvPr/></p:nvSpPr>'
        f"<p:spPr/><p:txBody><a:bodyPr/><a:lstStyle/>{paras_xml}</p:txBody></p:sp>"
    )


def _p(text, lang="en-US"):
    return f'<a:p><a:r><a:rPr lang="{lang}"/><a:t>{text}</a:t></a:r></a:p>'


SLIDE1 = (
    f"{DECL}<p:sld {NS} "
    'xmlns:mc="http://schemas.openxmlformats.org/markup-compatibility/2006">'
    "<p:cSld><p:spTree>"
    '<p:nvGrpSpPr><p:cNvPr id="1" name=""/><p:cNvGrpSpPr/><p:nvPr/></p:nvGrpSpPr><p:grpSpPr/>'
    + _sp(2, "Title 1", _p("Quarterly results"))
    + _sp(
        3,
        "Body 2",
        # mixed runs: plain, bold, a hyperlink run, a line break, then the slide-number field
        '<a:p><a:r><a:rPr lang="en-US"/><a:t>Sales grew </a:t></a:r>'
        '<a:r><a:rPr lang="en-US" b="1"/><a:t>twelve percent</a:t></a:r>'
        '<a:r><a:rPr lang="en-US"/><a:t> see </a:t></a:r>'
        '<a:r><a:rPr lang="en-US"><a:hlinkClick r:id="rId9"/></a:rPr><a:t>the report</a:t></a:r>'
        '<a:br><a:rPr lang="en-US"/></a:br>'
        '<a:r><a:rPr lang="en-US"/><a:t>Page </a:t></a:r>'
        '<a:fld id="{B6F15528-21DE-4FAA-801E-634DDDAF4B2B}" type="slidenum">'
        '<a:rPr lang="en-US"/><a:t>1</a:t></a:fld>'
        '<a:endParaRPr lang="en-US"/></a:p>'
        # a paragraph with only a field: not a unit
        '<a:p><a:fld id="{11111111-2222-3333-4444-555555555555}" type="datetime1">'
        '<a:rPr lang="en-US"/><a:t>10/10/2026</a:t></a:fld></a:p>'
        "<a:p><a:endParaRPr/></a:p>",  # empty paragraph: counted, never a unit
    )
    # a group shape with one text shape inside
    + '<p:grpSp><p:nvGrpSpPr><p:cNvPr id="4" name="Group 3"/><p:cNvGrpSpPr/><p:nvPr/>'
    "</p:nvGrpSpPr><p:grpSpPr/>"
    + _sp(5, "Inner 4", _p("Grouped caption"))
    + "</p:grpSp>"
    # a picture with alt text and a title
    + '<p:pic><p:nvPicPr><p:cNvPr id="6" name="Picture 5" descr="A rising bar chart" '
    'title="Growth"/><p:cNvPicPr/><p:nvPr/></p:nvPicPr><p:blipFill/><p:spPr/></p:pic>'
    # a table: the second cell of row 1 is merged away (hMerge) and is skipped
     + '<p:graphicFrame><p:nvGraphicFramePr><p:cNvPr id="7" name="Table 6"/>'
    "<p:cNvGraphicFramePr/><p:nvPr/></p:nvGraphicFramePr><p:xfrm/>"
    '<a:graphic><a:graphicData uri="http://schemas.openxmlformats.org/drawingml/2006/table">'
    '<a:tbl><a:tblGrid><a:gridCol w="100"/><a:gridCol w="100"/></a:tblGrid>'
    '<a:tr h="10"><a:tc gridSpan="2"><a:txBody><a:bodyPr/>'
    + _p("Region")
    + '</a:txBody></a:tc><a:tc hMerge="1"><a:txBody><a:bodyPr/>'
    + _p("hidden merged")
    + '</a:txBody></a:tc></a:tr><a:tr h="10"><a:tc><a:txBody><a:bodyPr/>'
    + _p("North")
    + "</a:txBody></a:tc><a:tc><a:txBody><a:bodyPr/>"
    + _p("42")
    + "</a:txBody></a:tc></a:tr></a:tbl></a:graphicData></a:graphic></p:graphicFrame>"
    # SmartArt
    + '<p:graphicFrame><p:nvGraphicFramePr><p:cNvPr id="8" name="Diagram 7"/>'
    "<p:cNvGraphicFramePr/><p:nvPr/></p:nvGraphicFramePr><p:xfrm/>"
    '<a:graphic><a:graphicData uri="http://schemas.openxmlformats.org/drawingml/2006/diagram">'
    '<dgm:relIds xmlns:dgm="http://schemas.openxmlformats.org/drawingml/2006/diagram" '
    'r:dm="rId3" r:lo="rId4" r:qs="rId5" r:cs="rId6"/></a:graphicData></a:graphic>'
    "</p:graphicFrame>"
    # chart
     + '<p:graphicFrame><p:nvGraphicFramePr><p:cNvPr id="9" name="Chart 8"/>'
    "<p:cNvGraphicFramePr/><p:nvPr/></p:nvGraphicFramePr><p:xfrm/>"
    '<a:graphic><a:graphicData uri="http://schemas.openxmlformats.org/drawingml/2006/chart">'
    '<c:chart xmlns:c="http://schemas.openxmlformats.org/drawingml/2006/chart" r:id="rId7"/>'
    "</a:graphicData></a:graphic></p:graphicFrame>"
    # markup compatibility: the Choice copy is translated, the Fallback copy is not
    + '<mc:AlternateContent><mc:Choice Requires="p14">'
    + _sp(10, "Choice 9", _p("Choice text"))
    + "</mc:Choice><mc:Fallback>"
    + _sp(11, "Fallback 10", _p("Fallback text"), descr="Fallback alt")
    + "</mc:Fallback></mc:AlternateContent>"
    "</p:spTree></p:cSld><p:clrMapOvr><a:masterClrMapping/></p:clrMapOvr></p:sld>"
)

SLIDE2_HIDDEN = (
    f'{DECL}<p:sld {NS} show="0"><p:cSld><p:spTree>'
    '<p:nvGrpSpPr><p:cNvPr id="1" name=""/><p:cNvGrpSpPr/><p:nvPr/></p:nvGrpSpPr><p:grpSpPr/>'
    + _sp(2, "Title 1", _p("Backup slide"))
    + "</p:spTree></p:cSld></p:sld>"
)

NOTES1 = (
    f"{DECL}<p:notes {NS}><p:cSld><p:spTree>"
    '<p:nvGrpSpPr><p:cNvPr id="1" name=""/><p:cNvGrpSpPr/><p:nvPr/></p:nvGrpSpPr><p:grpSpPr/>'
    + _sp(2, "Notes Placeholder 1", _p("Mention the new office."))
    + _sp(
        3,
        "Slide Number 2",
        '<a:p><a:fld id="{B6F15528-21DE-4FAA-801E-634DDDAF4B2B}" type="slidenum">'
        '<a:rPr lang="en-US"/><a:t>1</a:t></a:fld></a:p>',
    )
    + "</p:spTree></p:cSld></p:notes>"
)

DGM_NS = (
    'xmlns:dgm="http://schemas.openxmlformats.org/drawingml/2006/diagram" '
    'xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main" '
    'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"'
)
DSP = "http://schemas.microsoft.com/office/drawing/2008/diagram"

DATA1 = (
    f"{DECL}<dgm:dataModel {DGM_NS}><dgm:ptLst>"
    '<dgm:pt modelId="{00000000-0000-0000-0000-000000000000}" type="doc"><dgm:prSet/>'
    "<dgm:spPr/><dgm:t><a:bodyPr/><a:lstStyle/><a:p><a:endParaRPr/></a:p></dgm:t></dgm:pt>"
    '<dgm:pt modelId="{AAAAAAAA-0000-0000-0000-000000000001}"><dgm:prSet/><dgm:spPr/>'
    "<dgm:t><a:bodyPr/><a:lstStyle/>" + _p("Plan") + "</dgm:t></dgm:pt>"
    '<dgm:pt modelId="{AAAAAAAA-0000-0000-0000-000000000002}"><dgm:prSet/><dgm:spPr/>'
    "<dgm:t><a:bodyPr/><a:lstStyle/>" + _p("Build") + "</dgm:t></dgm:pt>"
    "</dgm:ptLst><dgm:cxnLst/><dgm:bg/><dgm:whole/>"
    '<dgm:extLst><a:ext uri="http://schemas.microsoft.com/office/drawing/2008/diagram">'
    f'<dsp:dataModelExt xmlns:dsp="{DSP}" relId="rId8" minVer="http://schemas.openxmlformats.org/drawingml/2006/diagram"/>'
    "</a:ext></dgm:extLst></dgm:dataModel>"
)

DRAWING1 = (
    f'{DECL}<dsp:drawing xmlns:dsp="{DSP}" '
    'xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main" '
    'xmlns:dgm="http://schemas.openxmlformats.org/drawingml/2006/diagram"><dsp:spTree>'
    '<dsp:nvGrpSpPr><dsp:cNvPr id="0" name=""/><dsp:cNvGrpSpPr/></dsp:nvGrpSpPr><dsp:grpSpPr/>'
    '<dsp:sp modelId="{AAAAAAAA-0000-0000-0000-000000000001}"><dsp:nvSpPr>'
    '<dsp:cNvPr id="0" name=""/><dsp:cNvSpPr/></dsp:nvSpPr><dsp:spPr/>'
    "<dsp:txBody><a:bodyPr/><a:lstStyle/>" + _p("Plan") + "</dsp:txBody></dsp:sp>"
    '<dsp:sp modelId="{AAAAAAAA-0000-0000-0000-000000000002}"><dsp:nvSpPr>'
    '<dsp:cNvPr id="0" name=""/><dsp:cNvSpPr/></dsp:nvSpPr><dsp:spPr/>'
    "<dsp:txBody><a:bodyPr/><a:lstStyle/>"
    + _p("Build")
    + "</dsp:txBody></dsp:sp></dsp:spTree></dsp:drawing>"
)

CHART1 = (
    f"{DECL}<c:chartSpace "
    'xmlns:c="http://schemas.openxmlformats.org/drawingml/2006/chart" '
    'xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main" '
    'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
    "<c:chart><c:title><c:tx><c:rich><a:bodyPr/><a:lstStyle/>"
    + _p("Revenue by region")
    + '</c:rich></c:tx><c:overlay val="0"/></c:title><c:plotArea><c:layout/>'
    '<c:barChart><c:barDir val="col"/><c:ser><c:idx val="0"/><c:order val="0"/>'
    '<c:tx><c:strRef><c:f>Sheet1!$B$1</c:f><c:strCache><c:ptCount val="1"/>'
    '<c:pt idx="0"><c:v>Series cache</c:v></c:pt></c:strCache></c:strRef></c:tx>'
    "</c:ser></c:barChart></c:plotArea></c:chart></c:chartSpace>"
)

LAYOUT = (
    f"{DECL}<p:sldLayout {NS}><p:cSld><p:spTree>"
    '<p:nvGrpSpPr><p:cNvPr id="1" name=""/><p:cNvGrpSpPr/><p:nvPr/></p:nvGrpSpPr><p:grpSpPr/>'
    + _sp(2, "Title 1", _p("Click to edit Master title style"))
    + "</p:spTree></p:cSld></p:sldLayout>"
)
MASTER = (
    f"{DECL}<p:sldMaster {NS}><p:cSld><p:spTree>"
    '<p:nvGrpSpPr><p:cNvPr id="1" name=""/><p:cNvGrpSpPr/><p:nvPr/></p:nvGrpSpPr><p:grpSpPr/>'
    + _sp(2, "Title 1", _p("Click to edit Master title style"))
    + '</p:spTree></p:cSld><p:sldLayoutIdLst><p:sldLayoutId id="2147483649" r:id="rId1"/>'
    "</p:sldLayoutIdLst></p:sldMaster>"
)


def make_pptx(kind="pptx", *, vba=False, slide1=SLIDE1, extra_parts=None, extra_slide_rels=()):
    """Bytes of one hand-written package. `kind` picks the main content type."""
    ct_main = MAIN_TYPES[kind]
    overrides = [
        ("/ppt/presentation.xml", ct_main),
        (
            "/ppt/slides/slide1.xml",
            "application/vnd.openxmlformats-officedocument.presentationml.slide+xml",
        ),
        (
            "/ppt/slides/slide2.xml",
            "application/vnd.openxmlformats-officedocument.presentationml.slide+xml",
        ),
        (
            "/ppt/notesSlides/notesSlide1.xml",
            "application/vnd.openxmlformats-officedocument.presentationml.notesSlide+xml",
        ),
        (
            "/ppt/slideLayouts/slideLayout1.xml",
            "application/vnd.openxmlformats-officedocument.presentationml.slideLayout+xml",
        ),
        (
            "/ppt/slideMasters/slideMaster1.xml",
            "application/vnd.openxmlformats-officedocument.presentationml.slideMaster+xml",
        ),
        (
            "/ppt/diagrams/data1.xml",
            "application/vnd.openxmlformats-officedocument.drawingml.diagramData+xml",
        ),
        ("/ppt/diagrams/drawing1.xml", "application/vnd.ms-office.drawingml.diagramDrawing+xml"),
        (
            "/ppt/charts/chart1.xml",
            "application/vnd.openxmlformats-officedocument.drawingml.chart+xml",
        ),
        (
            "/ppt/diagrams/layout1.xml",
            "application/vnd.openxmlformats-officedocument.drawingml.diagramLayout+xml",
        ),
        (
            "/ppt/diagrams/quickStyle1.xml",
            "application/vnd.openxmlformats-officedocument.drawingml.diagramStyle+xml",
        ),
        (
            "/ppt/diagrams/colors1.xml",
            "application/vnd.openxmlformats-officedocument.drawingml.diagramColors+xml",
        ),
    ]
    defaults = [
        ("rels", "application/vnd.openxmlformats-package.relationships+xml"),
        ("xml", "application/xml"),
    ]
    if vba:
        defaults.append(("bin", "application/vnd.ms-office.vbaProject"))
    ct = (
        f'{DECL}<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
        + "".join(f'<Default Extension="{e}" ContentType="{c}"/>' for e, c in defaults)
        + "".join(f'<Override PartName="{n}" ContentType="{c}"/>' for n, c in overrides)
        + "</Types>"
    )
    pres_rels = [
        ("rId1", RT + "slideMaster", "slideMasters/slideMaster1.xml", ""),
        ("rId2", RT + "slide", "slides/slide1.xml", ""),
        ("rId3", RT + "slide", "slides/slide2.xml", ""),
    ]
    if vba:
        pres_rels.append(
            (
                "rId9",
                "http://schemas.microsoft.com/office/2006/relationships/vbaProject",
                "vbaProject.bin",
                "",
            )
        )
    parts = {
        "[Content_Types].xml": ct,
        "_rels/.rels": _rels(("rId1", RT + "officeDocument", "ppt/presentation.xml", "")),
        "ppt/presentation.xml": (
            f"{DECL}<p:presentation {NS}>"
            '<p:sldMasterIdLst><p:sldMasterId id="2147483648" r:id="rId1"/></p:sldMasterIdLst>'
            '<p:sldIdLst><p:sldId id="256" r:id="rId2"/>'
            '<p:sldId id="257" r:id="rId3"/></p:sldIdLst>'
            '<p:sldSz cx="9144000" cy="6858000"/><p:notesSz cx="6858000" cy="9144000"/>'
            "</p:presentation>"
        ),
        "ppt/_rels/presentation.xml.rels": _rels(*pres_rels),
        "ppt/slides/slide1.xml": slide1,
        "ppt/slides/_rels/slide1.xml.rels": _rels(
            ("rId1", RT + "slideLayout", "../slideLayouts/slideLayout1.xml", ""),
            ("rId2", RT + "notesSlide", "../notesSlides/notesSlide1.xml", ""),
            ("rId3", RT + "diagramData", "../diagrams/data1.xml", ""),
            ("rId4", RT + "diagramLayout", "../diagrams/layout1.xml", ""),
            ("rId5", RT + "diagramQuickStyle", "../diagrams/quickStyle1.xml", ""),
            ("rId6", RT + "diagramColors", "../diagrams/colors1.xml", ""),
            ("rId7", RT + "chart", "../charts/chart1.xml", ""),
            (
                "rId8",
                "http://schemas.microsoft.com/office/2007/relationships/diagramDrawing",
                "../diagrams/drawing1.xml",
                "",
            ),
            ("rId9", RT + "hyperlink", "https://example.com/report", ' TargetMode="External"'),
            *extra_slide_rels,
        ),
        "ppt/slides/slide2.xml": SLIDE2_HIDDEN,
        "ppt/slides/_rels/slide2.xml.rels": _rels(
            ("rId1", RT + "slideLayout", "../slideLayouts/slideLayout1.xml", "")
        ),
        "ppt/notesSlides/notesSlide1.xml": NOTES1,
        "ppt/notesSlides/_rels/notesSlide1.xml.rels": _rels(
            ("rId1", RT + "slide", "../slides/slide1.xml", "")
        ),
        "ppt/slideLayouts/slideLayout1.xml": LAYOUT,
        "ppt/slideLayouts/_rels/slideLayout1.xml.rels": _rels(
            ("rId1", RT + "slideMaster", "../slideMasters/slideMaster1.xml", "")
        ),
        "ppt/slideMasters/slideMaster1.xml": MASTER,
        "ppt/slideMasters/_rels/slideMaster1.xml.rels": _rels(
            ("rId1", RT + "slideLayout", "../slideLayouts/slideLayout1.xml", "")
        ),
        "ppt/diagrams/data1.xml": DATA1,
        "ppt/diagrams/drawing1.xml": DRAWING1,
        "ppt/charts/chart1.xml": CHART1,
        # the other three SmartArt parts carry no text; minimal roots so every relationship
        # of the slide resolves to a real member
        "ppt/diagrams/layout1.xml": f"{DECL}<dgm:layoutDef {DGM_NS}/>",
        "ppt/diagrams/quickStyle1.xml": f"{DECL}<dgm:styleDef {DGM_NS}/>",
        "ppt/diagrams/colors1.xml": f"{DECL}<dgm:colorsDef {DGM_NS}/>",
    }
    if vba:
        parts["ppt/vbaProject.bin"] = VBA_BYTES
    parts.update(extra_parts or {})
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, body in parts.items():
            zf.writestr(name, body.encode("utf-8") if isinstance(body, str) else body)
    return out.getvalue()
