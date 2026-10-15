"""Hand-written Strict Open XML packages (ISO/IEC 29500 Strict) for verify_ooxml_strict (#669).

Every part is literal XML below, in the purl.oclc.org namespaces Office writes when a file
is saved as "Strict Open XML". Nothing is generated from the Transitional fixtures and
nothing is copied from another project's test data, so the test does not lean on the
namespace table it checks. stdlib zip only.
"""

import io
import zipfile

DECL = '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
PKG_RELS = "http://schemas.openxmlformats.org/package/2006/relationships"
CT_NS = "http://schemas.openxmlformats.org/package/2006/content-types"
S = "http://purl.oclc.org/ooxml/"
SRT = S + "officeDocument/relationships/"  # Strict relationship types
CORE_RT = "http://schemas.openxmlformats.org/package/2006/relationships/metadata/core-properties"

CORE = (
    f"{DECL}<cp:coreProperties "
    'xmlns:cp="http://schemas.openxmlformats.org/package/2006/metadata/core-properties" '
    'xmlns:dc="http://purl.org/dc/elements/1.1/" xmlns:dcterms="http://purl.org/dc/terms/">'
    "<dc:creator>Fixture</dc:creator></cp:coreProperties>"
)


def _app(name):
    return (
        f'{DECL}<Properties xmlns="{S}officeDocument/extendedProperties" '
        f'xmlns:vt="{S}officeDocument/docPropsVTypes"><Application>{name}</Application>'
        "</Properties>"
    )


def _rels(*rels):
    body = "".join(f'<Relationship Id="{i}" Type="{t}" Target="{g}"/>' for i, t, g in rels)
    return f'{DECL}<Relationships xmlns="{PKG_RELS}">{body}</Relationships>'


def _types(overrides):
    body = "".join(f'<Override PartName="{p}" ContentType="{c}"/>' for p, c in overrides)
    return (
        f'{DECL}<Types xmlns="{CT_NS}">'
        '<Default Extension="rels" '
        'ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
        '<Default Extension="xml" ContentType="application/xml"/>'
        f"{body}"
        '<Override PartName="/docProps/core.xml" '
        'ContentType="application/vnd.openxmlformats-package.core-properties+xml"/>'
        '<Override PartName="/docProps/app.xml" '
        'ContentType="application/vnd.openxmlformats-officedocument.extended-properties+xml"/>'
        "</Types>"
    )


def _zip(parts: dict) -> bytes:
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, text in parts.items():
            zf.writestr(name, text.encode("utf-8") if isinstance(text, str) else text)
    return out.getvalue()


def _root_rels(main):
    return _rels(
        ("rId1", SRT + "officeDocument", main),
        ("rId2", CORE_RT, "docProps/core.xml"),
        ("rId3", SRT + "extendedProperties", "docProps/app.xml"),
    )


# --- SpreadsheetML ---------------------------------------------------------------------

XL = "application/vnd.openxmlformats-officedocument.spreadsheetml."
SML = S + "spreadsheetml/main"
WORKBOOK = (
    f'{DECL}<workbook xmlns="{SML}" xmlns:r="{S}officeDocument/relationships" '
    'conformance="strict"><workbookPr dateCompatibility="0"/><bookViews><workbookView/>'
    '</bookViews><sheets><sheet name="Prices" sheetId="1" r:id="rId1"/>'
    '<sheet name="Notes" sheetId="2" r:id="rId2"/></sheets></workbook>'
)
SHEET1 = (
    f'{DECL}<worksheet xmlns="{SML}" xmlns:r="{S}officeDocument/relationships">'
    "<sheetData>"
    '<row r="1"><c r="A1" t="s"><v>0</v></c><c r="B1" t="s"><v>1</v></c></row>'
    '<row r="2"><c r="A2" t="s"><v>2</v></c><c r="B2"><v>4.5</v></c></row>'
    '<row r="3"><c r="A3" t="inlineStr"><is><t>Inline note</t></is></c>'
    '<c r="B3"><f>B2*2</f><v>9</v></c></row>'
    "</sheetData></worksheet>"
)
SHEET2 = (
    f'{DECL}<worksheet xmlns="{SML}"><sheetData>'
    '<row r="1"><c r="A1" t="s"><v>3</v></c></row></sheetData></worksheet>'
)
SST = (
    f'{DECL}<sst xmlns="{SML}" count="4" uniqueCount="4">'
    "<si><t>Product</t></si><si><t>Price</t></si>"
    '<si><r><rPr><b/></rPr><t>Green</t></r><r><t xml:space="preserve"> tea</t></r></si>'
    "<si><t>Ship within two days</t></si></sst>"
)
STYLES = (
    f'{DECL}<styleSheet xmlns="{SML}"><fonts count="1"><font><sz val="11"/><name val="Calibri"/>'
    '</font></fonts><fills count="1"><fill><patternFill patternType="none"/></fill></fills>'
    '<borders count="1"><border><start/><end/><top/><bottom/><diagonal/></border></borders>'
    '<cellStyleXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/></cellStyleXfs>'
    '<cellXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0"/></cellXfs>'
    "</styleSheet>"
)


def make_xlsx(*, unknown_ns: bool = False) -> bytes:
    """Two sheets, shared strings (one rich), an inline string, a number and a formula.
    unknown_ns: the workbook also declares a purl.oclc.org namespace no edition defines."""
    wb = WORKBOOK
    if unknown_ns:
        wb = wb.replace("<workbookPr", f'<workbookPr xmlns:zz="{S}spreadsheetml/future"', 1)
    return _zip(
        {
            "[Content_Types].xml": _types(
                [
                    ("/xl/workbook.xml", XL + "sheet.main+xml"),
                    ("/xl/worksheets/sheet1.xml", XL + "worksheet+xml"),
                    ("/xl/worksheets/sheet2.xml", XL + "worksheet+xml"),
                    ("/xl/sharedStrings.xml", XL + "sharedStrings+xml"),
                    ("/xl/styles.xml", XL + "styles+xml"),
                ]
            ),
            "_rels/.rels": _root_rels("xl/workbook.xml"),
            "docProps/core.xml": CORE,
            "docProps/app.xml": _app("Microsoft Excel"),
            "xl/workbook.xml": wb,
            "xl/_rels/workbook.xml.rels": _rels(
                ("rId1", SRT + "worksheet", "worksheets/sheet1.xml"),
                ("rId2", SRT + "worksheet", "worksheets/sheet2.xml"),
                ("rId3", SRT + "sharedStrings", "sharedStrings.xml"),
                ("rId4", SRT + "styles", "styles.xml"),
            ),
            "xl/worksheets/sheet1.xml": SHEET1,
            "xl/worksheets/sheet2.xml": SHEET2,
            "xl/sharedStrings.xml": SST,
            "xl/styles.xml": STYLES,
        }
    )


# --- WordprocessingML ------------------------------------------------------------------

WML = S + "wordprocessingml/main"
DOCUMENT = (
    f'{DECL}<w:document xmlns:w="{WML}" xmlns:r="{S}officeDocument/relationships" '
    'w:conformance="strict"><w:body>'
    '<w:p><w:pPr><w:pStyle w:val="Heading1"/></w:pPr><w:r><w:t>Welcome aboard</w:t></w:r></w:p>'
    '<w:p><w:r><w:t xml:space="preserve">Read the </w:t></w:r>'
    "<w:r><w:rPr><w:b/></w:rPr><w:t>safety card</w:t></w:r>"
    '<w:r><w:t xml:space="preserve"> before takeoff.</w:t></w:r></w:p>'
    '<w:tbl><w:tblPr/><w:tblGrid><w:gridCol w:w="2000"/></w:tblGrid>'
    "<w:tr><w:tc><w:p><w:r><w:t>Exit row</w:t></w:r></w:p></w:tc></w:tr></w:tbl>"
    '<w:sectPr><w:pgSz w:w="12240" w:h="15840"/></w:sectPr>'
    "</w:body></w:document>"
)
WSTYLES = (
    f'{DECL}<w:styles xmlns:w="{WML}">'
    '<w:style w:type="paragraph" w:default="1" w:styleId="Normal">'
    '<w:name w:val="Normal"/></w:style>'
    '<w:style w:type="paragraph" w:styleId="Heading1"><w:name w:val="heading 1"/>'
    '<w:basedOn w:val="Normal"/></w:style></w:styles>'
)
WD = "application/vnd.openxmlformats-officedocument.wordprocessingml."


def make_docx() -> bytes:
    return _zip(
        {
            "[Content_Types].xml": _types(
                [
                    ("/word/document.xml", WD + "document.main+xml"),
                    ("/word/styles.xml", WD + "styles+xml"),
                ]
            ),
            "_rels/.rels": _root_rels("word/document.xml"),
            "docProps/core.xml": CORE,
            "docProps/app.xml": _app("Microsoft Office Word"),
            "word/document.xml": DOCUMENT,
            "word/_rels/document.xml.rels": _rels(("rId1", SRT + "styles", "styles.xml")),
            "word/styles.xml": WSTYLES,
        }
    )


# --- PresentationML --------------------------------------------------------------------

PML = S + "presentationml/main"
DML = S + "drawingml/main"
PNS = f'xmlns:a="{DML}" xmlns:r="{S}officeDocument/relationships" xmlns:p="{PML}"'
PRESENTATION = (
    f'{DECL}<p:presentation {PNS} conformance="strict">'
    '<p:sldIdLst><p:sldId id="256" r:id="rId2"/></p:sldIdLst>'
    '<p:sldSz cx="9144000" cy="6858000"/><p:notesSz cx="6858000" cy="9144000"/>'
    "</p:presentation>"
)


def _sp(sid, name, text, descr=""):
    d = f' descr="{descr}"' if descr else ""
    return (
        f'<p:sp><p:nvSpPr><p:cNvPr id="{sid}" name="{name}"{d}/><p:cNvSpPr/><p:nvPr/></p:nvSpPr>'
        "<p:spPr/><p:txBody><a:bodyPr/><a:lstStyle/>"
        f'<a:p><a:r><a:rPr lang="en-US"/><a:t>{text}</a:t></a:r></a:p></p:txBody></p:sp>'
    )


SLIDE = (
    f"{DECL}<p:sld {PNS}><p:cSld><p:spTree>"
    '<p:nvGrpSpPr><p:cNvPr id="1" name=""/><p:cNvGrpSpPr/><p:nvPr/></p:nvGrpSpPr><p:grpSpPr/>'
    + _sp(2, "Title 1", "Spring launch")
    + _sp(3, "Body 2", "Three new routes", descr="Route map")
    + "</p:spTree></p:cSld></p:sld>"
)
PT = "application/vnd.openxmlformats-officedocument.presentationml."


def make_pptx() -> bytes:
    return _zip(
        {
            "[Content_Types].xml": _types(
                [
                    ("/ppt/presentation.xml", PT + "presentation.main+xml"),
                    ("/ppt/slides/slide1.xml", PT + "slide+xml"),
                ]
            ),
            "_rels/.rels": _root_rels("ppt/presentation.xml"),
            "docProps/core.xml": CORE,
            "docProps/app.xml": _app("Microsoft Office PowerPoint"),
            "ppt/presentation.xml": PRESENTATION,
            "ppt/_rels/presentation.xml.rels": _rels(("rId2", SRT + "slide", "slides/slide1.xml")),
            "ppt/slides/slide1.xml": SLIDE,
        }
    )
