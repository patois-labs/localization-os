"""OpenDocument filter (format Batch 12): app/filters/odf_filter.py.

  registry   .odt .ott .ods .ots .odp .otp .odg .otg resolve to the ODF filters.
  identity   build with no translations returns the original bytes; an identity
             translation keeps every unit, mimetype first + stored, members in order.
  odt        spans/links paired, tab/bookmark/note/frame point tokens, text:s spaces,
             footnote and text-box paragraphs their own units, tracked changes and
             comments skipped, table cells, headings, header/footer in styles.xml.
  translate  translated round trip keeps styles, the footnote, nested paragraphs;
             added spaces/tabs/newlines become text:s / text:tab / text:line-break.
  tolerant   dropped point elements are kept, unknown/duplicate tokens dropped.
  ods        string cells only (formulas, floats, dates skipped), repeated cells.
  odp/odg    slide text boxes and speaker notes; drawing shapes; masters skipped.
  lang       build_localized stamps fo:language / -asian / -complex, dc:language, RTL.
  hostile    not a zip, wrong mimetype, encrypted, DOCTYPE, entity, markup budget,
             segment cap, huge text:c, zip bomb.

Hermetic + offline. Final line: "ODF FILTER OK".
"""

import io
import os
import sys
import zipfile

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(_ROOT)
sys.path.insert(0, _ROOT)
from tests._bootstrap import init_env

init_env("loctms_odf_")

from lxml import etree

from app import config
from app.filters import get_filter, supported_exts
from app.filters.base import InputLimitError

NSDECL = (
    'xmlns:office="urn:oasis:names:tc:opendocument:xmlns:office:1.0" '
    'xmlns:style="urn:oasis:names:tc:opendocument:xmlns:style:1.0" '
    'xmlns:text="urn:oasis:names:tc:opendocument:xmlns:text:1.0" '
    'xmlns:table="urn:oasis:names:tc:opendocument:xmlns:table:1.0" '
    'xmlns:draw="urn:oasis:names:tc:opendocument:xmlns:drawing:1.0" '
    'xmlns:presentation="urn:oasis:names:tc:opendocument:xmlns:presentation:1.0" '
    'xmlns:fo="urn:oasis:names:tc:opendocument:xmlns:xsl-fo-compatible:1.0" '
    'xmlns:svg="urn:oasis:names:tc:opendocument:xmlns:svg-compatible:1.0" '
    'xmlns:xlink="http://www.w3.org/1999/xlink" '
    'xmlns:dc="http://purl.org/dc/elements/1.1/" '
    'xmlns:meta="urn:oasis:names:tc:opendocument:xmlns:meta:1.0" '
    'office:version="1.3"'
)
T = "{urn:oasis:names:tc:opendocument:xmlns:text:1.0}"
STYLE = "{urn:oasis:names:tc:opendocument:xmlns:style:1.0}"
FO = "{urn:oasis:names:tc:opendocument:xmlns:xsl-fo-compatible:1.0}"

ODT_CONTENT = (
    '<?xml version="1.0" encoding="UTF-8"?>\n'
    f"<office:document-content {NSDECL}><office:body><office:text>"
    '<text:tracked-changes><text:changed-region text:id="ct1"><text:deletion>'
    "<office:change-info><dc:creator>Ann</dc:creator></office:change-info>"
    "<text:p>Deleted words</text:p></text:deletion></text:changed-region>"
    '<text:changed-region text:id="ct2"><text:insertion><office:change-info>'
    "<dc:creator>Ann</dc:creator></office:change-info></text:insertion></text:changed-region>"
    "</text:tracked-changes>"
    '<text:h text:style-name="H1" text:outline-level="1">Getting started</text:h>'
    '<text:p text:style-name="P1">Press <text:span text:style-name="T1">Save</text:span> '
    'to keep {0} files.<text:note text:id="ftn1" text:note-class="footnote">'
    "<text:note-citation>1</text:note-citation><text:note-body>"
    "<text:p>A footnote about %1$s.</text:p></text:note-body></text:note></text:p>"
    '<text:p>Two<text:s text:c="3"/>gaps<text:tab/>and a <text:a xlink:href="https://x.example/">'
    'link</text:a><text:bookmark text:name="b1"/>.</text:p>'
    "<text:p>Before<office:annotation><dc:creator>Bo</dc:creator><text:p>Comment text</text:p>"
    '</office:annotation> after <text:change-start text:change-id="ct2"/>insert'
    '<text:change-end text:change-id="ct2"/>.<text:change text:change-id="ct1"/></text:p>'
    '<text:p>Caption<draw:frame draw:name="F1"><draw:text-box><text:p>Inside the box</text:p>'
    "</draw:text-box></draw:frame></text:p>"
    '<table:table><table:table-row><table:table-cell office:value-type="string">'
    "<text:p>Cell text</text:p></table:table-cell>"
    '<table:table-cell office:value-type="float" office:value="3"><text:p>3</text:p>'
    "</table:table-cell></table:table-row></table:table>"
    "<text:p>   </text:p><text:p>12345</text:p>"
    "<text:p>Unicode café 日本 \U0001f600</text:p>"
    "</office:text></office:body></office:document-content>"
)
ODT_STYLES = (
    '<?xml version="1.0" encoding="UTF-8"?>\n'
    f"<office:document-styles {NSDECL}><office:styles>"
    '<style:default-style style:family="paragraph"><style:text-properties fo:language="en" '
    'fo:country="US"/></style:default-style>'
    '<style:style style:name="Standard" style:family="paragraph"/></office:styles>'
    '<office:master-styles><style:master-page style:name="Standard">'
    "<style:header><text:p>Header line</text:p></style:header>"
    "<style:footer><text:p>Page <text:page-number>1</text:page-number></text:p></style:footer>"
    "</style:master-page></office:master-styles></office:document-styles>"
)
META = (
    '<?xml version="1.0" encoding="UTF-8"?>\n'
    f"<office:document-meta {NSDECL}><office:meta><dc:title>T</dc:title>"
    "<dc:language>en-US</dc:language></office:meta></office:document-meta>"
)


def _manifest(mimetype, names):
    entries = "".join(
        f'<manifest:file-entry manifest:full-path="{n}" manifest:media-type="{t}"/>'
        for n, t in [("/", mimetype)] + [(n, "text/xml") for n in names]
    )
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<manifest:manifest xmlns:manifest="urn:oasis:names:tc:opendocument:xmlns:manifest:1.0" '
        f'manifest:version="1.3">{entries}</manifest:manifest>'
    )


MANIFEST = _manifest("application/vnd.oasis.opendocument.text", ["content.xml"])


def pkg(
    content,
    styles=None,
    mimetype="application/vnd.oasis.opendocument.text",
    manifest=None,
    meta=META,
    extra=(),
):
    """A minimal valid package (the manifest lists every member, as LibreOffice requires)."""
    names = ["content.xml"] + (["styles.xml"] if styles else []) + (["meta.xml"] if meta else [])
    manifest = manifest or _manifest(mimetype or "application/zip", names).replace(
        "</manifest:manifest>",
        '<manifest:file-entry manifest:full-path="Pictures/a.png" manifest:media-type="image/png"/>'
        + "".join(
            f'<manifest:file-entry manifest:full-path="{n}" manifest:media-type=""/>'
            for n, _d in extra
        )
        + "</manifest:manifest>",
    )
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        if mimetype is not None:
            z.writestr(zipfile.ZipInfo("mimetype"), mimetype)
        z.writestr("content.xml", content, zipfile.ZIP_DEFLATED)
        if styles:
            z.writestr("styles.xml", styles, zipfile.ZIP_DEFLATED)
        if meta:
            z.writestr("meta.xml", meta, zipfile.ZIP_DEFLATED)
        z.writestr("Pictures/a.png", b"\x89PNG fake image bytes", zipfile.ZIP_STORED)
        z.writestr("META-INF/manifest.xml", manifest, zipfile.ZIP_DEFLATED)
        for name, data in extra:
            z.writestr(name, data, zipfile.ZIP_DEFLATED)
    return buf.getvalue()


ODT = pkg(ODT_CONTENT, ODT_STYLES)


def units(fmt, data):
    return [(u.p_index, u.text) for u in get_filter(fmt).parse(data).units]


def member(data, name):
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        return z.read(name)


def test_registered():
    for ext in ("odt", "ott", "ods", "ots", "odp", "otp", "odg", "otg"):
        assert ext in supported_exts(), ext
        assert get_filter(ext) is not None
    assert get_filter("ott") is get_filter("odt")
    assert get_filter("ots").fmt == "ods" and get_filter("otp").fmt == "odp"
    print("  [reg] eight ODF extensions registered OK")


EXPECTED_ODT = [
    (0, "Getting started"),
    (1, "Press ⟦g1⟧Save⟦/g1⟧ to keep {0} files.⟦x2⟧"),
    (2, "A footnote about %1$s."),
    (3, "Two   gaps⟦x1⟧and a ⟦g2⟧link⟦/g2⟧⟦x3⟧."),
    (4, "Before⟦x1⟧ after ⟦x2⟧insert⟦x3⟧.⟦x4⟧"),
    (5, "Caption⟦x1⟧"),
    (6, "Inside the box"),
    (7, "Cell text"),
    (10, "Unicode café 日本 \U0001f600"),
    (11, "Header line"),
    (12, "Page ⟦x1⟧"),
]


def test_odt_units():
    got = units("odt", ODT)
    # 5 "Caption⟦x1⟧" has letters; the deleted region, the comment, the float cell,
    # the blank and the digits-only paragraphs are not units (8, 9 are counted, skipped).
    assert got == EXPECTED_ODT, got
    sk = get_filter("odt").parse(ODT).skeleton
    assert sk["format"] == "odt" and sk["units"][0]["style"] == "heading"
    assert sk["units"][-1]["part"] == "styles.xml"
    print("  [odt] units, tokens, footnote/frame/table/header units OK")


def test_identity():
    f = get_filter("odt")
    assert f.build(ODT, {}) == ODT
    same = {str(i): t for i, t in EXPECTED_ODT}
    out = f.build(ODT, same)
    assert units("odt", out) == EXPECTED_ODT
    with zipfile.ZipFile(io.BytesIO(out)) as z:
        infos = z.infolist()
        names = [i.filename for i in infos]
        assert names[0] == "mimetype" and infos[0].compress_type == zipfile.ZIP_STORED
        assert infos[0].extra == b""
        with zipfile.ZipFile(io.BytesIO(ODT)) as zo:
            assert names == zo.namelist()
            for n in ("Pictures/a.png", "META-INF/manifest.xml", "meta.xml", "mimetype"):
                assert z.read(n) == zo.read(n), n
            assert z.getinfo("Pictures/a.png").compress_type == zipfile.ZIP_STORED
    # the raw bytes start with the mimetype member, readable without unzipping
    assert out[30:38] == b"mimetype" and out[38:77] == b"application/vnd.oasis.opendocument.text"
    print("  [identity] no-op build is byte-identical, identity build keeps units OK")


def test_translated():
    f = get_filter("odt")
    out = f.build(
        ODT,
        {
            "0": "Erste Schritte",
            "1": "Klicken Sie auf ⟦g1⟧Speichern⟦/g1⟧, um {0} Dateien zu behalten.⟦x2⟧",
            "2": "Eine Fußnote zu %1$s.",
            "3": "Zwei  Lücken⟦x1⟧und ein ⟦g2⟧Link⟦/g2⟧⟦x3⟧.\nNeu\tTab",
            "6": "Im Kasten",
            "7": "Zelltext",
            "11": "Kopfzeile",
        },
    )
    got = dict(units("odt", out))
    assert got[0] == "Erste Schritte"
    assert got[1] == "Klicken Sie auf ⟦g1⟧Speichern⟦/g1⟧, um {0} Dateien zu behalten.⟦x2⟧"
    assert got[2] == "Eine Fußnote zu %1$s."
    assert got[3] == "Zwei  Lücken⟦x1⟧und ein ⟦g2⟧Link⟦/g2⟧⟦x3⟧.⟦x4⟧Neu⟦x5⟧Tab", got[3]
    assert got[6] == "Im Kasten" and got[7] == "Zelltext" and got[11] == "Kopfzeile"
    assert got[5] == "Caption⟦x1⟧" and got[12] == "Page ⟦x1⟧"
    content = member(out, "content.xml").decode()
    assert '<text:span text:style-name="T1">Speichern</text:span>' in content
    assert 'text:note-class="footnote"><text:note-citation>1</text:note-citation>' in content
    assert "Zwei <text:s/>Lücken<text:tab/>und ein " in content, content
    assert (
        '<text:a xlink:href="https://x.example/">Link</text:a><text:bookmark text:name="b1"/>.'
        in content
    )
    assert ".<text:line-break/>Neu<text:tab/>Tab</text:p>" in content
    assert "<text:p>Deleted words</text:p>" in content  # tracked deletion untouched
    assert "<text:p>Comment text</text:p>" in content
    assert "Kopfzeile" in member(out, "styles.xml").decode()
    print("  [translate] styles, footnote, frame, spaces/tab/newline encoding OK")


def test_spaces():
    c = ODT_CONTENT.replace(
        "<text:p>   </text:p>",
        '<text:p>  lead\n  wrapped   text <text:s text:c="999999999"/>x</text:p>',
    )
    data = pkg(c, ODT_STYLES)
    got = dict(units("odt", data))
    text = got[8]
    assert text.startswith("lead wrapped text ") and text.endswith("x"), text[:40]
    assert text.count(" ") == 3 + 1000, text.count(" ")  # text:c is capped
    out = get_filter("odt").build(data, {"8": "  Anfang  zwei"})
    assert dict(units("odt", out))[8] == "  Anfang  zwei"
    content = member(out, "content.xml").decode()
    assert '<text:p><text:s text:c="2"/>Anfang <text:s/>zwei</text:p>' in content, content
    print("  [spaces] ODF 6.1.2 collapse, leading text:s, text:c cap OK")


def test_tolerant():
    f = get_filter("odt")
    out = f.build(ODT, {"1": "Nur Text ⟦g9⟧fremd⟦/g9⟧ ⟦x1⟧ ⟦x7⟧ ⟦/g4⟧ Ende"})
    content = member(out, "content.xml").decode()
    # the footnote (x2) was dropped by the translation but is kept at the end
    p = content.split('text:style-name="P1">')[1].split("</text:p>", 1)[0]
    assert p.startswith("Nur Text fremd  "), p
    assert "⟦" not in content
    assert 'text:id="ftn1"' in content and "A footnote about" in content
    assert "Speichern" not in content and "Save" not in p
    assert dict(units("odt", out))[2] == "A footnote about %1$s."
    print("  [tolerant] stray/unknown tokens dropped, footnote kept OK")


ODS_CONTENT = (
    '<?xml version="1.0" encoding="UTF-8"?>\n'
    f"<office:document-content {NSDECL}><office:body><office:spreadsheet>"
    '<table:table table:name="Sheet1"><table:table-row>'
    '<table:table-cell office:value-type="string"><text:p>Name</text:p></table:table-cell>'
    '<table:table-cell office:value-type="float" office:value="42"><text:p>42</text:p>'
    "</table:table-cell>"
    '<table:table-cell table:formula="of:=CONCAT(&quot;a&quot;;&quot;b&quot;)" '
    'office:value-type="string" office:string-value="ab"><text:p>ab</text:p></table:table-cell>'
    '<table:table-cell office:value-type="date" office:date-value="2026-10-10">'
    "<text:p>Oct 10, 2026</text:p></table:table-cell>"
    '<table:table-cell table:number-columns-repeated="3" office:value-type="string">'
    "<text:p>Repeated label</text:p></table:table-cell>"
    '<table:table-cell office:value-type="string"><text:p>Total <text:span>due</text:span></text:p>'
    "<office:annotation><text:p>cell comment</text:p></office:annotation></table:table-cell>"
    "</table:table-row></table:table></office:spreadsheet></office:body></office:document-content>"
)


def test_ods():
    data = pkg(ODS_CONTENT, mimetype="application/vnd.oasis.opendocument.spreadsheet")
    got = units("ods", data)
    assert got == [(0, "Name"), (1, "Repeated label"), (2, "Total ⟦g1⟧due⟦/g1⟧")], got
    out = get_filter("ods").build(data, {"0": "Nom", "1": "Étiquette", "2": "Total ⟦g1⟧dû⟦/g1⟧"})
    c = member(out, "content.xml").decode()
    rep = '<table:table-cell table:number-columns-repeated="3" office:value-type="string">'
    assert rep + "<text:p>Étiquette" in c
    assert "<text:p>42</text:p>" in c and "<text:p>Oct 10, 2026</text:p>" in c
    assert 'office:string-value="ab"><text:p>ab</text:p>' in c
    assert "<text:p>cell comment</text:p>" in c
    assert member(out, "mimetype") == b"application/vnd.oasis.opendocument.spreadsheet"
    print("  [ods] string cells only, formulas/numbers/dates skipped, repeats OK")


ODP_CONTENT = (
    '<?xml version="1.0" encoding="UTF-8"?>\n'
    f"<office:document-content {NSDECL}><office:body><office:presentation>"
    '<draw:page draw:name="Slide 1"><draw:frame presentation:class="title"><draw:text-box>'
    "<text:p>Quarterly results</text:p></draw:text-box></draw:frame>"
    '<draw:frame presentation:class="outline"><draw:text-box><text:list><text:list-item>'
    "<text:p>Revenue <text:span>up</text:span> 20%</text:p></text:list-item></text:list>"
    "</draw:text-box></draw:frame>"
    "<presentation:notes><draw:frame><draw:text-box><text:p>Speak slowly here</text:p>"
    "</draw:text-box></draw:frame></presentation:notes></draw:page>"
    "</office:presentation></office:body></office:document-content>"
)
ODP_STYLES = (
    '<?xml version="1.0" encoding="UTF-8"?>\n'
    f"<office:document-styles {NSDECL}><office:master-styles>"
    '<style:master-page style:name="Default"><draw:frame presentation:class="title">'
    "<draw:text-box><text:p>Click to edit the title</text:p></draw:text-box></draw:frame>"
    "</style:master-page></office:master-styles></office:document-styles>"
)
ODG_CONTENT = (
    '<?xml version="1.0" encoding="UTF-8"?>\n'
    f"<office:document-content {NSDECL}><office:body><office:drawing>"
    '<draw:page draw:name="page1"><draw:custom-shape><text:p>Start here</text:p>'
    "</draw:custom-shape><draw:rect><text:p>Decision</text:p></draw:rect></draw:page>"
    "</office:drawing></office:body></office:document-content>"
)


def test_odp_odg():
    data = pkg(ODP_CONTENT, ODP_STYLES, mimetype="application/vnd.oasis.opendocument.presentation")
    got = units("odp", data)
    assert got == [
        (0, "Quarterly results"),
        (1, "Revenue ⟦g1⟧up⟦/g1⟧ 20%"),
        (2, "Speak slowly here"),
    ], got
    out = get_filter("odp").build(data, {"2": "Langsam sprechen"})
    assert "Langsam sprechen" in member(out, "content.xml").decode()
    assert member(out, "styles.xml") == member(data, "styles.xml")
    g = pkg(ODG_CONTENT, mimetype="application/vnd.oasis.opendocument.graphics")
    assert units("odg", g) == [(0, "Start here"), (1, "Decision")]
    out = get_filter("odg").build(g, {"1": "Entscheidung"})
    assert units("odg", out) == [(0, "Start here"), (1, "Entscheidung")]
    print("  [odp/odg] slides, notes, shapes; master placeholders skipped OK")


def _default_props(data):
    root = etree.fromstring(member(data, "styles.xml"))
    ds = root.find(f".//{STYLE}default-style[@{STYLE}family='paragraph']")
    return ds


def test_localized():
    f = get_filter("odt")
    out = f.build_localized(ODT, {"0": "Erste Schritte"}, "de_DE")
    tp = _default_props(out).find(f"{STYLE}text-properties")
    assert tp.get(f"{FO}language") == "de" and tp.get(f"{FO}country") == "DE"
    assert b"<dc:language>de-DE</dc:language>" in member(out, "meta.xml")
    ja = f.build_localized(ODT, {}, "ja")
    tp = _default_props(ja).find(f"{STYLE}text-properties")
    assert tp.get(f"{STYLE}language-asian") == "ja" and tp.get(f"{FO}language") == "en"
    # #730: Thai / Hindi are complex-script (CTL) text: the -complex slot, not fo:language
    for loc, lang in (("th-TH", "th"), ("hi-IN", "hi")):
        tp = _default_props(f.build_localized(ODT, {}, loc)).find(f"{STYLE}text-properties")
        assert tp.get(f"{STYLE}language-complex") == lang, (loc, tp.attrib)
        assert tp.get(f"{FO}language") == "en", (loc, tp.attrib)
    ar = f.build_localized(ODT, {}, "ar")
    ds = _default_props(ar)
    assert ds.find(f"{STYLE}text-properties").get(f"{STYLE}language-complex") == "ar"
    assert ds.find(f"{STYLE}paragraph-properties").get(f"{STYLE}writing-mode") == "rl-tb"
    assert units("odt", ar) == EXPECTED_ODT
    assert f.build_localized(ODT, {}, None) == ODT and f.build_localized(ODT, {}, "-") == ODT
    # Gate: a "[None]" language style (zxx/none, code text) keeps proofing off; a style
    # that declares a real language is re-stamped.
    styles = ODT_STYLES.replace(
        '<style:style style:name="Standard" style:family="paragraph"/>',
        '<style:style style:name="Code" style:family="text"><style:text-properties '
        'fo:language="zxx" fo:country="none"/></style:style>'
        '<style:style style:name="Quote" style:family="text"><style:text-properties '
        'fo:language="en" fo:country="GB"/></style:style>',
    )
    out = f.build_localized(pkg(ODT_CONTENT, styles), {}, "de-DE")
    root = etree.fromstring(member(out, "styles.xml"))
    code = root.find(f".//{STYLE}style[@{STYLE}name='Code']/{STYLE}text-properties")
    quote = root.find(f".//{STYLE}style[@{STYLE}name='Quote']/{STYLE}text-properties")
    assert code.get(f"{FO}language") == "zxx" and code.get(f"{FO}country") == "none"
    assert quote.get(f"{FO}language") == "de" and quote.get(f"{FO}country") == "DE"
    print("  [lang] fo:language / -asian / -complex, dc:language, RTL OK")


ODP_STYLES_GRAPHIC = ODP_STYLES.replace(
    "<office:master-styles>",
    '<office:styles><style:default-style style:family="graphic">'
    '<style:graphic-properties svg:stroke-color="#3465a4"/>'
    '<style:paragraph-properties style:text-autospace="ideograph-alpha" '
    'style:writing-mode="lr-tb"/>'
    '<style:text-properties fo:language="en" fo:country="US"/></style:default-style>'
    '<style:default-style style:family="presentation"><style:text-properties '
    'fo:language="en" fo:country="US"/></style:default-style>'
    "</office:styles><office:master-styles>",
)


def _graphic_default(data):
    root = etree.fromstring(member(data, "styles.xml"))
    return root.find(f".//{STYLE}default-style[@{STYLE}family='graphic']")


def test_rtl_frames_724():
    """#724 (format soak): ODP / ODG text lives in drawing frames, which take their paragraph
    direction from the GRAPHIC default style. A Hebrew / Arabic export used to keep its
    `lr-tb` (real POI TestPresentation01.odp into he-IL: every slide left to right)."""
    data = pkg(
        ODP_CONTENT, ODP_STYLES_GRAPHIC, mimetype="application/vnd.oasis.opendocument.presentation"
    )
    f = get_filter("odp")
    for loc in ("he-IL", "ar-SA", "fa"):
        out = f.build_localized(data, {"0": "תוצאות רבעוניות"}, loc)
        ds = _graphic_default(out)
        kids = [etree.QName(c).localname for c in ds]
        assert kids == ["graphic-properties", "paragraph-properties", "text-properties"], kids
        pp = ds.find(f"{STYLE}paragraph-properties")
        assert pp.get(f"{STYLE}writing-mode") == "rl-tb", (loc, pp.attrib)
        assert pp.get(f"{STYLE}text-autospace") == "ideograph-alpha"
        assert units("odp", out)[1:] == units("odp", data)[1:]
    for loc in ("de-DE", "ja-JP"):
        out = f.build_localized(data, {}, loc)
        pp = _graphic_default(out).find(f"{STYLE}paragraph-properties")
        assert pp.get(f"{STYLE}writing-mode") == "lr-tb", (loc, pp.attrib)
    # a graphic default without paragraph-properties gets one, after graphic-properties
    bare = ODP_STYLES_GRAPHIC.replace(
        '<style:paragraph-properties style:text-autospace="ideograph-alpha" '
        'style:writing-mode="lr-tb"/>',
        "",
    )
    out = get_filter("odg").build_localized(
        pkg(ODG_CONTENT, bare, mimetype="application/vnd.oasis.opendocument.graphics"), {}, "ar"
    )
    ds = _graphic_default(out)
    assert [etree.QName(c).localname for c in ds] == [
        "graphic-properties",
        "paragraph-properties",
        "text-properties",
    ]
    assert ds.find(f"{STYLE}paragraph-properties").get(f"{STYLE}writing-mode") == "rl-tb"
    print("  [rtl] ODP / ODG frame text (graphic default) right to left for RTL targets OK")


# Modern LibreOffice shape (a pptx saved as ODP by LibreOffice 25): every shape's graphic /
# presentation style and the automatic paragraph styles inside it carry an EXPLICIT lr-tb,
# which overrides the default style.
_MODERN_AUTO = (
    "<office:automatic-styles>"
    '<style:style style:name="gr1" style:family="graphic" style:parent-style-name="standard">'
    '<style:graphic-properties draw:fill="none"/>'
    '<style:paragraph-properties style:writing-mode="lr-tb"/></style:style>'
    '<style:style style:name="pr1" style:family="presentation">'
    '<style:graphic-properties style:writing-mode="lr-tb"/>'
    '<style:paragraph-properties style:writing-mode="lr-tb"/></style:style>'
    '<style:style style:name="P1" style:family="paragraph">'
    '<style:paragraph-properties fo:text-align="left" style:writing-mode="lr-tb"/></style:style>'
    '<style:style style:name="P2" style:family="paragraph">'
    '<style:paragraph-properties style:writing-mode="tb-rl"/></style:style>'
    '<style:style style:name="P3" style:family="paragraph">'
    '<style:paragraph-properties style:writing-mode="page"/></style:style>'
    '<style:style style:name="T1" style:family="text">'
    '<style:text-properties fo:font-weight="bold"/></style:style>'
    "</office:automatic-styles>"
)


def _modes(data, part):
    root = etree.fromstring(member(data, part))
    out = {}
    for st in root.iter(f"{STYLE}style"):
        pp = st.find(f"{STYLE}paragraph-properties")
        if pp is not None:
            out[st.get(f"{STYLE}name")] = pp.get(f"{STYLE}writing-mode")
    return out


def test_rtl_explicit_ltr_styles_724():
    """#724 gate: a modern ODP pins lr-tb on every shape and paragraph style, so flipping
    the default alone changed nothing visible (LibreOffice render of a pptx-derived deck into
    he-IL: bullets left, period at the right end). For an RTL target the explicit horizontal
    left-to-right modes of paragraph / graphic / presentation styles turn rl-tb, in content.xml
    and styles.xml; vertical modes and "page" are kept, alignment and the frame-level
    graphic-properties mode are not touched. LTR targets: byte-identical styles."""
    content = ODP_CONTENT.replace("<office:body>", _MODERN_AUTO + "<office:body>")
    styles = ODP_STYLES_GRAPHIC.replace(
        "</office:styles>",
        '<style:style style:name="standard" style:family="graphic">'
        '<style:paragraph-properties style:writing-mode="lr-tb"/></style:style>'
        "</office:styles>",
    )
    data = pkg(content, styles, mimetype="application/vnd.oasis.opendocument.presentation")
    for loc in ("he-IL", "ar-SA"):
        out = get_filter("odp").build_localized(data, {"0": "תוצאות רבעוניות"}, loc)
        got = _modes(out, "content.xml")
        assert got == {
            "gr1": "rl-tb",
            "pr1": "rl-tb",
            "P1": "rl-tb",
            "P2": "tb-rl",
            "P3": "page",
        }, (
            loc,
            got,
        )
        root = etree.fromstring(member(out, "content.xml"))
        p1 = root.find(f".//{STYLE}style[@{STYLE}name='P1']/{STYLE}paragraph-properties")
        assert p1.get(f"{FO}text-align") == "left"
        pr1 = root.find(f".//{STYLE}style[@{STYLE}name='pr1']/{STYLE}graphic-properties")
        assert pr1.get(f"{STYLE}writing-mode") == "lr-tb"
        assert _modes(out, "styles.xml")["standard"] == "rl-tb"
        assert units("odp", out)[1:] == units("odp", data)[1:]
    out = get_filter("odp").build_localized(data, {}, "de-DE")
    assert _modes(out, "content.xml") == _modes(data, "content.xml")
    assert _modes(out, "styles.xml") == _modes(data, "styles.xml")
    print("  [rtl] explicit lr-tb in shape / paragraph styles turns rl-tb for RTL targets OK")


def _refused(fmt, data, needle):
    try:
        get_filter(fmt).parse(data)
    except InputLimitError as exc:
        assert needle in str(exc), str(exc)
        return
    raise AssertionError(f"not refused: {needle}")


def test_hostile():
    _refused("odt", b"not a zip at all", "not a valid OpenDocument")
    _refused("odt", pkg(ODT_CONTENT, mimetype="application/zip"), "not an OpenDocument")
    enc = MANIFEST.replace(
        "</manifest:manifest>", "<manifest:encryption-data/></manifest:manifest>"
    )
    _refused("odt", pkg(ODT_CONTENT, manifest=enc), "password-protected")
    doctype = ODT_CONTENT.replace(
        "<office:document-content",
        '<!DOCTYPE x [<!ENTITY a "aaaa"><!ENTITY b "&a;&a;&a;">]><office:document-content',
        1,
    ).replace("Getting started", "&b;")
    _refused("odt", pkg(doctype), "DOCTYPE")
    _refused("odt", pkg(ODT_CONTENT.replace("Getting started", "&ext;")), "invalid XML")
    _refused("odt", pkg("<office:document-content"), "invalid XML")
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("mimetype", "application/vnd.oasis.opendocument.text")
    _refused("odt", buf.getvalue(), "no content.xml")
    old = config.MAX_DOCUMENT_SEGMENTS
    try:
        config.MAX_DOCUMENT_SEGMENTS = 3
        _refused("odt", ODT, "segments")
        config.MAX_DOCUMENT_SEGMENTS = 2
        _refused("odt", ODT, "MAX_DOCUMENT_SEGMENTS")
    finally:
        config.MAX_DOCUMENT_SEGMENTS = old
    spaces = '<text:s text:c="1000"/>' * 200
    _refused("odt", pkg(ODT_CONTENT.replace("Getting started", spaces)), "MAX_SEGMENT_CHARS")
    bomb = pkg(ODT_CONTENT, extra=(("Pictures/zero.bin", b"\0" * (8 * 1024 * 1024)),))
    _refused("odt", bomb, "expands")
    print("  [hostile] zip/mimetype/encryption/DOCTYPE/entity/budget/cap/bomb refused OK")


def main():
    test_registered()
    test_odt_units()
    test_identity()
    test_translated()
    test_spaces()
    test_tolerant()
    test_ods()
    test_odp_odg()
    test_localized()
    test_rtl_frames_724()
    test_rtl_explicit_ltr_styles_724()
    test_hostile()
    print("ODF FILTER OK")


if __name__ == "__main__":
    main()
