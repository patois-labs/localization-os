"""Hand-written Transitional workbooks for verify_xlsx_inplace_export (#685).

Every part is literal XML (or literal bytes) below; nothing comes from openpyxl, so the
fixture carries parts openpyxl does not model: a text box (in mc:AlternateContent, with a
Fallback copy), an EMF picture, printer settings, data validation, a comment with its VML
drawing, a chart, the calc chain, and a VBA project for the .xlsm variant. stdlib zip only.
"""

import io
import zipfile

DECL = '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
PKG_RELS = "http://schemas.openxmlformats.org/package/2006/relationships"
T = "http://schemas.openxmlformats.org/"
RT = T + "officeDocument/2006/relationships/"
SML = T + "spreadsheetml/2006/main"
XL = "application/vnd.openxmlformats-officedocument.spreadsheetml."
XLSX_MAIN = XL + "sheet.main+xml"
XLSM_MAIN = "application/vnd.ms-excel.sheet.macroEnabled.main+xml"
MC = "http://schemas.openxmlformats.org/markup-compatibility/2006"

# opaque binary parts: the test only checks they come back byte for byte
EMF = b"\x01\x00\x00\x00" + bytes(range(256)) * 4
PRINTER = b"PRINTER-SETTINGS\x00" + bytes(range(200))
VBA = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + bytes(300)

TEXTS = [
    # sheet "Report" (cells in openpyxl order)
    "Quarterly report",  # A1 shared 0 (also B4)
    "Bold title",  # B1 shared 1 (uniform rich run)
    "Mixed run text",  # A2 shared 2 (mixed runs + phonetic)
    "Inline note",  # B2 inline string
    "Plain str cell",  # A3 t="str" without a formula
    "Quarterly report",  # B4 shared 0 again
    "Implicit row cell",  # row 5 has no r, cell has no r -> A5
    # sheet "Notes"
    "Second sheet text",
    # text box (walk 2), after every cell
    "Box line one",
    "Box line two",
]
CELL_COUNT = 8


def _rels(*rels):
    body = "".join(f'<Relationship Id="{i}" Type="{t}" Target="{g}"/>' for i, t, g in rels)
    return f'{DECL}<Relationships xmlns="{PKG_RELS}">{body}</Relationships>'


def _zip(parts: dict) -> bytes:
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, data in parts.items():
            zf.writestr(name, data.encode("utf-8") if isinstance(data, str) else data)
    return out.getvalue()


def _types(main_type: str, vba: bool) -> str:
    over = [
        ("/xl/workbook.xml", main_type),
        ("/xl/worksheets/sheet1.xml", XL + "worksheet+xml"),
        ("/xl/worksheets/sheet2.xml", XL + "worksheet+xml"),
        ("/xl/sharedStrings.xml", XL + "sharedStrings+xml"),
        ("/xl/styles.xml", XL + "styles+xml"),
        ("/xl/calcChain.xml", XL + "calcChain+xml"),
        ("/xl/comments1.xml", XL + "comments+xml"),
        ("/xl/drawings/drawing1.xml", "application/vnd.openxmlformats-officedocument.drawing+xml"),
        (
            "/xl/charts/chart1.xml",
            "application/vnd.openxmlformats-officedocument.drawingml.chart+xml",
        ),
        ("/docProps/core.xml", "application/vnd.openxmlformats-package.core-properties+xml"),
    ]
    body = "".join(f'<Override PartName="{p}" ContentType="{c}"/>' for p, c in over)
    vba_default = (
        '<Default Extension="bin" ContentType="application/vnd.ms-office.vbaProject"/>'
        if vba
        else '<Default Extension="bin" ContentType='
        '"application/vnd.openxmlformats-officedocument.spreadsheetml.printerSettings"/>'
    )
    return (
        f'{DECL}<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
        '<Default Extension="rels" '
        'ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
        '<Default Extension="xml" ContentType="application/xml"/>'
        '<Default Extension="emf" ContentType="image/x-emf"/>'
        '<Default Extension="vml" '
        'ContentType="application/vnd.openxmlformats-officedocument.vmlDrawing"/>'
        f"{vba_default}{body}</Types>"
    )


WORKBOOK = (
    f'{DECL}<workbook xmlns="{SML}" xmlns:r="{RT[:-1]}"><workbookPr/><bookViews>'
    '<workbookView/></bookViews><sheets><sheet name="Report" sheetId="1" r:id="rId1"/>'
    '<sheet name="Notes" sheetId="2" r:id="rId2"/></sheets></workbook>'
)

SHEET1 = (
    f'{DECL}<worksheet xmlns="{SML}" xmlns:r="{RT[:-1]}"><dimension ref="A1:C5"/>'
    "<sheetData>"
    '<row r="1" spans="1:3"><c r="A1" t="s"><v>0</v></c><c r="B1" t="s"><v>1</v></c>'
    '<c r="C1"><v>42</v></c></row>'
    '<row r="2"><c r="A2" t="s"><v>2</v></c>'
    '<c r="B2" t="inlineStr"><is><t>Inline note</t></is></c>'
    '<c r="C2"><f>C1*2</f><v>84</v></c></row>'
    '<row r="3"><c r="A3" t="str"><v>Plain str cell</v></c><c r="B3" t="s"><v>3</v></c></row>'
    '<row r="4"><c r="B4" t="s"><v>0</v></c></row>'
    '<row><c t="inlineStr"><is><t>Implicit row cell</t></is></c></row>'
    "</sheetData>"
    '<dataValidations count="1"><dataValidation type="list" allowBlank="1" sqref="C1">'
    '<formula1>"Yes,No"</formula1></dataValidation></dataValidations>'
    '<pageMargins left="0.7" right="0.7" top="0.75" bottom="0.75" header="0.3" footer="0.3"/>'
    '<pageSetup orientation="landscape" r:id="rId2"/>'
    '<drawing r:id="rId1"/><legacyDrawing r:id="rId3"/></worksheet>'
)
SHEET2 = (
    f'{DECL}<worksheet xmlns="{SML}"><sheetData>'
    '<row r="1"><c r="A1" t="s"><v>4</v></c></row></sheetData></worksheet>'
)
SST = (
    f'{DECL}<sst xmlns="{SML}" count="6" uniqueCount="5">'
    "<si><t>Quarterly report</t></si>"
    '<si><r><rPr><b/><sz val="14"/></rPr><t>Bold </t></r>'
    '<r><rPr><b/><sz val="14"/></rPr><t>title</t></r></si>'
    '<si><r><rPr><i/></rPr><t xml:space="preserve">Mixed </t></r><r><t>run text</t></r>'
    '<rPh sb="0" eb="5"><t>mikusu</t></rPh><phoneticPr fontId="1"/></si>'
    "<si><t>---</t></si>"
    "<si><t>Second sheet text</t></si></sst>"
)
STYLES = (
    f'{DECL}<styleSheet xmlns="{SML}"><fonts count="1"><font><sz val="11"/><name val="Calibri"/>'
    '</font></fonts><fills count="1"><fill><patternFill patternType="none"/></fill></fills>'
    '<borders count="1"><border><left/><right/><top/><bottom/><diagonal/></border></borders>'
    '<cellStyleXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/></cellStyleXfs>'
    '<cellXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0"/></cellXfs>'
    "</styleSheet>"
)
CALC = f'{DECL}<calcChain xmlns="{SML}"><c r="C2" i="1"/></calcChain>'
COMMENTS = (
    f'{DECL}<comments xmlns="{SML}"><authors><author>Ann</author></authors><commentList>'
    '<comment ref="C1" authorId="0"><text><t>Pick one</t></text></comment>'
    "</commentList></comments>"
)
VML = (
    '<xml xmlns:v="urn:schemas-microsoft-com:vml"'
    ' xmlns:o="urn:schemas-microsoft-com:office:office"'
    ' xmlns:x="urn:schemas-microsoft-com:office:excel">'
    '<v:shape id="_x0000_s1025" type="#_x0000_t202">'
    '<x:ClientData ObjectType="Note"><x:Row>0</x:Row><x:Column>2</x:Column></x:ClientData>'
    "</v:shape></xml>"
)
_XDR = T + "drawingml/2006/spreadsheetDrawing"
_A = T + "drawingml/2006/main"


def _textbox(lines):
    paras = "".join(
        f'<a:p><a:r><a:rPr lang="en-US" sz="1100"/><a:t>{x}</a:t></a:r></a:p>' for x in lines
    )
    return (
        '<xdr:sp macro="" textlink=""><xdr:nvSpPr><xdr:cNvPr id="2" name="TextBox 1"/>'
        '<xdr:cNvSpPr txBox="1"/></xdr:nvSpPr><xdr:spPr/><xdr:txBody><a:bodyPr/><a:lstStyle/>'
        f"{paras}</xdr:txBody></xdr:sp>"
    )


DRAWING = (
    f'{DECL}<xdr:wsDr xmlns:xdr="{_XDR}" xmlns:a="{_A}" xmlns:r="{RT[:-1]}" xmlns:mc="{MC}" '
    'xmlns:a14="http://schemas.microsoft.com/office/drawing/2010/main">'
    "<mc:AlternateContent>"
    '<mc:Choice Requires="a14"><xdr:twoCellAnchor><xdr:from><xdr:col>4</xdr:col><xdr:colOff>0'
    "</xdr:colOff><xdr:row>1</xdr:row><xdr:rowOff>0</xdr:rowOff></xdr:from><xdr:to><xdr:col>8"
    "</xdr:col><xdr:colOff>0</xdr:colOff><xdr:row>5</xdr:row><xdr:rowOff>0</xdr:rowOff></xdr:to>"
    f"{_textbox(['Box line one', 'Box line two', '  '])}<xdr:clientData/></xdr:twoCellAnchor>"
    "</mc:Choice>"
    "<mc:Fallback><xdr:twoCellAnchor><xdr:from><xdr:col>4</xdr:col><xdr:colOff>0</xdr:colOff>"
    "<xdr:row>1</xdr:row><xdr:rowOff>0</xdr:rowOff></xdr:from><xdr:to><xdr:col>8</xdr:col>"
    "<xdr:colOff>0</xdr:colOff><xdr:row>5</xdr:row><xdr:rowOff>0</xdr:rowOff></xdr:to>"
    f"{_textbox(['Fallback copy'])}<xdr:clientData/></xdr:twoCellAnchor></mc:Fallback>"
    "</mc:AlternateContent>"
    "<xdr:oneCellAnchor><xdr:from><xdr:col>0</xdr:col><xdr:colOff>0</xdr:colOff><xdr:row>7"
    '</xdr:row><xdr:rowOff>0</xdr:rowOff></xdr:from><xdr:ext cx="914400" cy="914400"/>'
    '<xdr:pic><xdr:nvPicPr><xdr:cNvPr id="3" name="Picture 2"/><xdr:cNvPicPr/></xdr:nvPicPr>'
    '<xdr:blipFill><a:blip r:embed="rId1"/><a:stretch><a:fillRect/></a:stretch></xdr:blipFill>'
    '<xdr:spPr><a:prstGeom prst="rect"><a:avLst/></a:prstGeom></xdr:spPr></xdr:pic>'
    "<xdr:clientData/></xdr:oneCellAnchor>"
    "<xdr:oneCellAnchor><xdr:from><xdr:col>0</xdr:col><xdr:colOff>0</xdr:colOff><xdr:row>12"
    '</xdr:row><xdr:rowOff>0</xdr:rowOff></xdr:from><xdr:ext cx="914400" cy="914400"/>'
    '<xdr:graphicFrame macro=""><xdr:nvGraphicFramePr><xdr:cNvPr id="4" name="Chart 3"/>'
    "<xdr:cNvGraphicFramePr/></xdr:nvGraphicFramePr><xdr:xfrm/><a:graphic>"
    '<a:graphicData uri="http://schemas.openxmlformats.org/drawingml/2006/chart">'
    f'<c:chart xmlns:c="{T}drawingml/2006/chart" r:id="rId2"/></a:graphicData></a:graphic>'
    "</xdr:graphicFrame><xdr:clientData/></xdr:oneCellAnchor>"
    "</xdr:wsDr>"
)
CHART = (
    f'{DECL}<c:chartSpace xmlns:c="{T}drawingml/2006/chart" xmlns:a="{_A}"><c:chart>'
    '<c:plotArea><c:layout/><c:barChart><c:barDir val="col"/><c:grouping val="clustered"/>'
    '<c:ser><c:idx val="0"/><c:order val="0"/><c:val><c:numRef><c:f>Report!$C$1:$C$2</c:f>'
    '</c:numRef></c:val></c:ser><c:axId val="1"/><c:axId val="2"/></c:barChart>'
    '<c:catAx><c:axId val="1"/><c:scaling><c:orientation val="minMax"/></c:scaling>'
    '<c:axPos val="b"/><c:crossAx val="2"/></c:catAx>'
    '<c:valAx><c:axId val="2"/><c:scaling><c:orientation val="minMax"/></c:scaling>'
    '<c:axPos val="l"/><c:crossAx val="1"/></c:valAx>'
    "</c:plotArea></c:chart></c:chartSpace>"
)
CORE = (
    f"{DECL}<cp:coreProperties "
    'xmlns:cp="http://schemas.openxmlformats.org/package/2006/metadata/core-properties" '
    'xmlns:dc="http://purl.org/dc/elements/1.1/"><dc:creator>Fixture</dc:creator>'
    "</cp:coreProperties>"
)


def make_workbook(*, macro: bool = False, core: bool = True) -> bytes:
    root_rels = [("rId1", RT + "officeDocument", "xl/workbook.xml")]
    if core:
        root_rels.append(("rId2", PKG_RELS + "/metadata/core-properties", "docProps/core.xml"))
    wb_rels = [
        ("rId1", RT + "worksheet", "worksheets/sheet1.xml"),
        ("rId2", RT + "worksheet", "worksheets/sheet2.xml"),
        ("rId3", RT + "sharedStrings", "sharedStrings.xml"),
        ("rId4", RT + "styles", "styles.xml"),
        ("rId5", RT + "calcChain", "calcChain.xml"),
    ]
    if macro:
        wb_rels.append(
            (
                "rId6",
                "http://schemas.microsoft.com/office/2006/relationships/vbaProject",
                "vbaProject.bin",
            )
        )
    parts = {
        "[Content_Types].xml": _types(XLSM_MAIN if macro else XLSX_MAIN, macro),
        "_rels/.rels": _rels(*root_rels),
        "xl/workbook.xml": WORKBOOK,
        "xl/_rels/workbook.xml.rels": _rels(*wb_rels),
        "xl/worksheets/sheet1.xml": SHEET1,
        "xl/worksheets/_rels/sheet1.xml.rels": _rels(
            ("rId1", RT + "drawing", "../drawings/drawing1.xml"),
            ("rId2", RT + "printerSettings", "../printerSettings/printerSettings1.bin"),
            ("rId3", RT + "vmlDrawing", "../drawings/vmlDrawing1.vml"),
            ("rId4", RT + "comments", "../comments1.xml"),
        ),
        "xl/worksheets/sheet2.xml": SHEET2,
        "xl/sharedStrings.xml": SST,
        "xl/styles.xml": STYLES,
        "xl/calcChain.xml": CALC,
        "xl/comments1.xml": COMMENTS,
        "xl/drawings/vmlDrawing1.vml": VML,
        "xl/drawings/drawing1.xml": DRAWING,
        "xl/drawings/_rels/drawing1.xml.rels": _rels(
            ("rId1", RT + "image", "../media/image1.emf"),
            ("rId2", RT + "chart", "../charts/chart1.xml"),
        ),
        "xl/media/image1.emf": EMF,
        "xl/charts/chart1.xml": CHART,
        "xl/printerSettings/printerSettings1.bin": PRINTER,
    }
    if core:
        parts["docProps/core.xml"] = CORE
    if macro:
        parts["xl/vbaProject.bin"] = VBA
    return _zip(parts)
