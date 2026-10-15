"""MadCap Flare project filter (wave 4, route C): app/filters/flare_filter.py.

Hermetic: the fixture project is built in memory below (no network, no Flare install).
It covers topics and snippets with MadCap:variable (self-closing and with cached content),
snippetText, snippetBlock (nested snippets), conditionalText, dropDown/head/hotspot/body,
xref, keyword (keywords, sub-keywords, an escaped colon), glossaryTerm, MadCap:conditions
attributes, a variable set (text, DateTime and numeric variables), a TOC (nested entries,
[%=System.LinkedTitle%] and a mixed title), a glossary, a condition tag set, a stylesheet
and a binary image, plus RTL/CJK content.

Proves: the right units (and only those) are extracted; build() of the original with no or
identical translations returns the ORIGINAL BYTES; a translated build keeps every member's
name, order and (for untouched members) bytes, keeps every MadCap element and attribute,
stays well-formed XML; hostile zips are refused; and the four surfaces (web upload/export,
CLI upload/export, /api/formats, help + llms.txt + capability descriptor) carry the format.
ASCII-only output.
"""

# ruff: noqa: E501
import io
import json
import os
import sys
import tempfile
import zipfile

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(_ROOT)
sys.path.insert(0, _ROOT)
from tests._bootstrap import init_env

init_env("loctms_flare_")

from fastapi.testclient import TestClient
from lxml import etree

from app.db import engine, init_db
from app.filters import get_filter
from app.filters.base import InputLimitError
from app.main import app
from app.models import Segment
from app.services.segment import find_placeholders, ph_point
from tests._cli_runner import run_cli

JA = "\u65e5\u672c\u8a9e\u306e\u30c6\u30ad\u30b9\u30c8"  # Japanese sample
AR = "\u0646\u0635 \u0639\u0631\u0628\u064a"  # Arabic sample

TOPIC = (
    '<?xml version="1.0" encoding="utf-8"?>\n'
    '<html xmlns:MadCap="http://www.madcapsoftware.com/Schemas/MadCap.xsd" lang="en-us" xml:lang="en-us">\n'
    "    <head><title>Printing guide</title>\n"
    '    <link href="../Resources/Stylesheets/Styles.css" rel="stylesheet" type="text/css" />\n'
    "    </head>\n"
    "    <body>\n"
    '        <h1>Print with <MadCap:variable name="General.ProductName" /></h1>\n'
    '        <p MadCap:conditions="Default.PrintOnly">Install <MadCap:variable name="General.ProductName">Acme Writer</MadCap:variable> before you print, don\'t skip it.</p>\n'
    '        <p>Read the <MadCap:glossaryTerm glossTerm="Glossary.Term0">spooler</MadCap:glossaryTerm> notes and see <MadCap:xref href="Other.htm">Other topic</MadCap:xref>.<MadCap:keyword term="Printing;Printing:Color;\\:Colon start" /></p>\n'
    '        <p>Text for <MadCap:conditionalText MadCap:conditions="Default.Online">online readers only</MadCap:conditionalText> and everyone.</p>\n'
    '        <p>Brand line: <MadCap:snippetText src="../Resources/Snippets/Brand.flsnp" /> ends here.</p>\n'
    '        <MadCap:snippetBlock src="../Resources/Snippets/Outer.flsnp" />\n'
    "        <MadCap:dropDown>\n"
    "            <MadCap:dropDownHead>\n"
    "                <MadCap:dropDownHotspot>More details</MadCap:dropDownHotspot>\n"
    "            </MadCap:dropDownHead>\n"
    "            <MadCap:dropDownBody>\n"
    "                <p>Hidden body text.</p>\n"
    "            </MadCap:dropDownBody>\n"
    "        </MadCap:dropDown>\n"
    '        <p><img src="../Resources/Images/logo.png" alt="Company logo" /> Spacing&#160;kept.</p>\n'
    "    </body>\n"
    "</html>\n"
)
INTL = (
    '<?xml version="1.0" encoding="utf-8"?>\n'
    '<html xmlns:MadCap="http://www.madcapsoftware.com/Schemas/MadCap.xsd">\n'
    "<body>\r\n"
    f"<p>{JA}</p>\r\n"
    f'<p dir="rtl">{AR} <MadCap:variable name="General.ProductName" /></p>\r\n'
    "</body>\r\n</html>\r\n"
)
OUTER = (
    '<?xml version="1.0" encoding="utf-8"?>\n'
    '<html xmlns:MadCap="http://www.madcapsoftware.com/Schemas/MadCap.xsd">\n'
    "<body>\n<p>Outer snippet text.</p>\n"
    '<MadCap:snippetBlock src="Inner.flsnp" />\n'
    "</body>\n</html>\n"
)
INNER = (
    '<?xml version="1.0" encoding="utf-8"?>\n'
    '<html xmlns:MadCap="http://www.madcapsoftware.com/Schemas/MadCap.xsd">\n'
    '<body>\n<p>Inner snippet with <MadCap:conditionalText MadCap:conditions="Default.Print">print words</MadCap:conditionalText>.</p>\n'
    "</body>\n</html>\n"
)
BRAND = (
    '<?xml version="1.0" encoding="utf-8"?>\n'
    '<html xmlns:MadCap="http://www.madcapsoftware.com/Schemas/MadCap.xsd">\n'
    "<body><p>Acme makes writing tools</p></body>\n</html>\n"
)
FLVAR = (
    '<?xml version="1.0" encoding="utf-8"?>\n'
    "<CatapultVariableSet>\n"
    '  <Variable Name="ProductName" Comment="Do not abbreviate" EvaluatedDefinition="Acme Writer">Acme Writer</Variable>\n'
    '  <Variable Name="Tagline" EvaluatedDefinition="Write &amp; ship">Write &amp; ship</Variable>\n'
    '  <Variable Name="Year" EvaluatedDefinition="2026">2026</Variable>\n'
    '  <Variable Name="Updated" Type="DateTime" Format="MMMM dd, yyyy" EvaluatedDefinition="October 10, 2026">October 10, 2026</Variable>\n'
    "</CatapultVariableSet>\n"
)
FLTOC = (
    '<?xml version="1.0" encoding="utf-8"?>\n'
    '<CatapultToc Version="1">\n'
    '  <TocEntry Title="Getting started" Link="/Content/Topic.htm">\n'
    '    <TocEntry Title="[%=System.LinkedTitle%]" Link="/Content/Intl.htm" />\n'
    '    <TocEntry Title="Guide to [%=General.ProductName%] printing" Link="/Content/Topic.htm" MadCap:conditions="Default.PrintOnly" xmlns:MadCap="http://www.madcapsoftware.com/Schemas/MadCap.xsd" />\n'
    "  </TocEntry>\n"
    "</CatapultToc>\n"
)
FLGLO = (
    '<?xml version="1.0" encoding="utf-8"?>\n'
    "<CatapultGlossary>\n"
    '  <GlossaryEntry glossTerm="Glossary.Term0">\n'
    "    <Terms>\n      <Term>spooler</Term>\n    </Terms>\n"
    '    <Definition Link="">A queue that holds print jobs.</Definition>\n'
    "  </GlossaryEntry>\n"
    "</CatapultGlossary>\n"
)
FLCTS = (
    '<?xml version="1.0" encoding="utf-8"?>\n'
    "<CatapultConditionTagSet>\n"
    '  <ConditionTag Name="PrintOnly" BackgroundColor="#ff0000" />\n'
    '  <ConditionTag Name="Online" BackgroundColor="#00ff00" />\n'
    "</CatapultConditionTagSet>\n"
)
FLPRJ = '<?xml version="1.0" encoding="utf-8"?>\n<CatapultProject Version="1" xml:lang="en-us" />\n'
PNG = b"\x89PNG\r\n\x1a\n" + bytes(range(256)) * 4

MEMBERS = [
    ("Content/", b""),
    ("Content/Topic.htm", TOPIC.encode("utf-8")),
    ("Content/Intl.htm", b"\xef\xbb\xbf" + INTL.encode("utf-8")),
    ("Content/Resources/Snippets/Outer.flsnp", OUTER.encode("utf-8")),
    ("Content/Resources/Snippets/Inner.flsnp", INNER.encode("utf-8")),
    ("Content/Resources/Snippets/Brand.flsnp", BRAND.encode("utf-8")),
    ("Content/Resources/Stylesheets/Styles.css", b"p { color: #333; }\n"),
    ("Content/Resources/Images/logo.png", PNG),
    ("Project/VariableSets/General.flvar", FLVAR.encode("utf-8")),
    ("Project/TOCs/Master.fltoc", FLTOC.encode("utf-8")),
    ("Project/Glossaries/Glossary.flglo", FLGLO.encode("utf-8")),
    ("Project/ConditionTagSets/Default.flcts", FLCTS.encode("utf-8")),
    ("Help.flprj", FLPRJ.encode("utf-8")),
]


def make_zip(members=MEMBERS, comment=b"flare fixture") -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.comment = comment
        for name, data in members:
            zi = zipfile.ZipInfo(name, date_time=(2026, 10, 1, 9, 30, 0))
            zi.compress_type = zipfile.ZIP_STORED if name.endswith(".png") else zipfile.ZIP_DEFLATED
            zf.writestr(zi, data)
    return buf.getvalue()


def members_of(data: bytes) -> list[tuple[str, bytes, int]]:
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        return [(i.filename, zf.read(i), i.compress_type) for i in zf.infolist()]


FLT = get_filter("flprjzip")
ORIG = make_zip()


def _texts(parsed):
    return [u.text for u in parsed.units]


# --------------------------------------------------------------------------- 1 extraction
def test_extraction():
    assert get_filter("flprjzip") is FLT and get_filter("zip") is None
    parsed = FLT.parse(ORIG)
    texts = _texts(parsed)
    joined = "\n".join(texts)
    x1 = ph_point("x", 1)
    # Variables are point tokens, never text: name and cached content never offered.
    assert "General.ProductName" not in joined, texts
    assert "Acme Writer before" not in joined and f"Install {x1} before you print" in joined, texts
    assert f"Print with {x1}" in texts, texts
    # Inline MadCap elements stay inside their sentence.
    assert any("spooler" in t and "Other topic" in t and "notes and see" in t for t in texts), texts
    assert any("online readers only" in t and "and everyone." in t for t in texts), texts
    assert any(t.startswith("Brand line: ") and "ends here." in t for t in texts), texts
    # Drop-down hotspot and body are their own units.
    assert "More details" in texts and "Hidden body text." in texts, texts
    # Head title, nested snippets, image alt, RTL/CJK.
    for want in (
        "Printing guide",
        "Outer snippet text.",
        "Acme makes writing tools",
        "Company logo",
        JA,
    ):
        assert want in joined, (want, texts)
    assert any("print words" in t and t.startswith("Inner snippet with") for t in texts), texts
    assert any(t.startswith(AR) for t in texts), texts
    # Index keywords: one unit per keyword / level, escaped colon unescaped.
    kw = [u.text for u in parsed.units if u.style == "flare:index-keyword"]
    assert kw == ["Printing", "Printing", "Color", ":Colon start"], kw
    # Variable set: text definitions only (not DateTime, not numeric-only).
    var = [u.text for u in parsed.units if u.style == "flare:variable"]
    assert var == ["Acme Writer", "Write & ship"], var
    toc = [u.text for u in parsed.units if u.style == "flare:toc-title"]
    assert toc == ["Getting started", f"Guide to {x1} printing"], toc
    glo = [(u.style, u.text) for u in parsed.units if u.style.startswith("flare:glossary")]
    assert glo == [
        ("flare:glossary-term", "spooler"),
        ("flare:glossary-definition", "A queue that holds print jobs."),
    ], glo
    # Nothing from the condition tag set, the project file or the stylesheet.
    for never in ("PrintOnly", "Default.Online", "CatapultProject", "color"):
        assert never not in joined, (never, texts)
    files = {f["name"]: f["kind"] for f in parsed.skeleton["files"]}
    assert files["Project/TOCs/Master.fltoc"] == "toc" and files["Content/Intl.htm"] == "topic", (
        files
    )
    assert [u.p_index for u in parsed.units] == list(range(len(parsed.units)))
    print(
        f"  1) extraction: {len(texts)} units; variables protected, MadCap inline kept in sentences"
    )


# --------------------------------------------------------------------------- 2 byte identity
def test_byte_identity():
    assert FLT.build(ORIG, {}) == ORIG
    parsed = FLT.parse(ORIG)
    same = {str(u.p_index): u.text for u in parsed.units}
    assert FLT.build(ORIG, same) == ORIG, "identity mapping must return the original bytes"
    print("  2) byte identity: empty and identity mappings return the original zip bytes")


def _translate(text: str) -> str:
    return "T(" + text + ")"


# --------------------------------------------------------------------------- 3 translated build
def test_translated_build():
    parsed = FLT.parse(ORIG)
    mapping = {}
    for u in parsed.units:
        if u.text == JA:
            mapping[str(u.p_index)] = "\u4e2d\u6587\u7ffb\u8bd1"  # CJK target
        elif u.text.startswith(AR):
            mapping[str(u.p_index)] = u.text.replace(AR, "\u062a\u0631\u062c\u0645\u0629")
        else:
            mapping[str(u.p_index)] = _translate(u.text)
    out = FLT.build(ORIG, mapping)
    before, after = members_of(ORIG), members_of(out)
    assert [m[0] for m in before] == [m[0] for m in after], "member names/order changed"
    assert [m[2] for m in before] == [m[2] for m in after], "compression types changed"
    with zipfile.ZipFile(io.BytesIO(out)) as zf:
        assert zf.comment == b"flare fixture"
        assert all(i.date_time == (2026, 10, 1, 9, 30, 0) for i in zf.infolist())
    a = {n: d for n, d, _c in after}
    b = {n: d for n, d, _c in before}
    for untouched in (
        "Project/ConditionTagSets/Default.flcts",
        "Help.flprj",
        "Content/Resources/Images/logo.png",
        "Content/Resources/Stylesheets/Styles.css",
        "Content/",
    ):
        assert a[untouched] == b[untouched], untouched
    topic = a["Content/Topic.htm"].decode("utf-8")
    for keep in (
        '<MadCap:variable name="General.ProductName" />',
        '<MadCap:variable name="General.ProductName">Acme Writer</MadCap:variable>',
        'MadCap:conditions="Default.PrintOnly"',
        '<MadCap:conditionalText MadCap:conditions="Default.Online">online readers only</MadCap:conditionalText>',
        '<MadCap:glossaryTerm glossTerm="Glossary.Term0">',
        '<MadCap:xref href="Other.htm">',
        '<MadCap:snippetText src="../Resources/Snippets/Brand.flsnp" />',
        '<MadCap:snippetBlock src="../Resources/Snippets/Outer.flsnp" />',
        "<MadCap:dropDownHotspot>T(More details)</MadCap:dropDownHotspot>",
        'alt="T(Company logo)"',
        "<title>T(Printing guide)</title>",
        'term="T(Printing);T(Printing):T(Color);T(\\:Colon start)"',
    ):
        assert keep in topic, (keep, topic)
    assert find_placeholders(topic) == []
    assert a["Content/Intl.htm"].startswith(b"\xef\xbb\xbf"), "BOM lost"
    intl = a["Content/Intl.htm"].decode("utf-8-sig")
    assert "\u4e2d\u6587\u7ffb\u8bd1" in intl and "\u062a\u0631\u062c\u0645\u0629" in intl
    assert "\r\n" in intl and '<MadCap:variable name="General.ProductName" />' in intl
    inner = a["Content/Resources/Snippets/Inner.flsnp"].decode("utf-8")
    assert 'MadCap:conditions="Default.Print">' in inner and "T(Inner snippet with" in inner, inner
    outer = a["Content/Resources/Snippets/Outer.flsnp"].decode("utf-8")
    assert '<MadCap:snippetBlock src="Inner.flsnp" />' in outer, outer
    flvar = a["Project/VariableSets/General.flvar"].decode("utf-8")
    assert 'EvaluatedDefinition="T(Acme Writer)">T(Acme Writer)</Variable>' in flvar, flvar
    assert 'EvaluatedDefinition="T(Write &amp; ship)">T(Write &amp; ship)</Variable>' in flvar, (
        flvar
    )
    assert ">2026</Variable>" in flvar and ">October 10, 2026</Variable>" in flvar, flvar
    assert 'Comment="Do not abbreviate"' in flvar
    toc = a["Project/TOCs/Master.fltoc"].decode("utf-8")
    assert 'Title="[%=System.LinkedTitle%]"' in toc, toc
    assert 'Title="T(Guide to [%=General.ProductName%] printing)"' in toc, toc
    assert 'Title="T(Getting started)"' in toc
    glo = a["Project/Glossaries/Glossary.flglo"].decode("utf-8")
    assert "<Term>T(spooler)</Term>" in glo and "T(A queue that holds print jobs.)" in glo
    # Every rewritten XML part is still well-formed.
    parser = etree.XMLParser(resolve_entities=False, no_network=True, load_dtd=False)
    for name in a:
        if name.endswith((".htm", ".flsnp", ".flvar", ".fltoc", ".flglo")):
            etree.fromstring(a[name].lstrip(b"\xef\xbb\xbf"), parser)
    # Re-parse of the translated project has the same unit count.
    assert len(FLT.parse(out).units) == len(parsed.units)
    # build_localized declares the target language (RTL direction for Arabic).
    loc = members_of(FLT.build_localized(ORIG, mapping, "ar-SA"))
    loc_topic = {n: d for n, d, _c in loc}["Content/Topic.htm"].decode("utf-8")
    assert (
        'lang="ar-SA"' in loc_topic and 'xml:lang="ar-SA"' in loc_topic and 'dir="rtl"' in loc_topic
    )
    print("  3) translated build: layout kept, MadCap elements/attributes kept, XML well-formed")


# ------------------------------------------------------------- 3b XHTML-safe targets (gate)
def test_xhtml_safe_targets():
    """A translation with an HTML-only named entity (&nbsp;, &eacute;), an unknown entity
    or a control character must still give well-formed XHTML topics (Flare parses them as
    XML with no DTD). Pre-fix: &nbsp; survived verbatim and lxml refused the topic."""
    parsed = FLT.parse(ORIG)
    mapping = {}
    for u in parsed.units:
        if u.text == "More details":
            mapping[str(u.p_index)] = "Plus&nbsp;de d&eacute;tails &bogus; A&amp;B\x01"
        elif u.text == "Company logo":
            mapping[str(u.p_index)] = "Logo\x02 de l'entreprise"
    out = FLT.build(ORIG, mapping)
    topic = {n: d for n, d, _c in members_of(out)}["Content/Topic.htm"]
    parser = etree.XMLParser(resolve_entities=False, no_network=True, load_dtd=False)
    etree.fromstring(topic, parser)
    text = topic.decode("utf-8")
    assert "Plus&#160;de d&#233;tails &amp;bogus; A&amp;B</MadCap:dropDownHotspot>" in text, text
    assert "&nbsp;" not in text and "\x01" not in text and "\x02" not in text
    print("  3b) XHTML-safe targets: named entities become numeric, control chars dropped")


# --------------------------------------------------------------------------- 4 hostile zips
def _refused(data: bytes, needle: str):
    try:
        FLT.parse(data)
    except InputLimitError as exc:
        assert needle.lower() in str(exc).lower(), (needle, str(exc))
        return
    raise AssertionError(f"not refused: {needle}")


def test_hostile():
    from app import config

    _refused(b"not a zip at all", "not a zip")
    _refused(
        make_zip([("readme.txt", b"hello"), ("a.htm", b"<p>Hi</p>")]), "not a MadCap Flare project"
    )
    _refused(make_zip([*MEMBERS, ("../evil.htm", b"<p>x</p>")]), "unsafe path")
    _refused(make_zip([*MEMBERS, ("/abs/evil.htm", b"<p>x</p>")]), "unsafe path")
    _refused(make_zip([*MEMBERS, ("C:/evil.htm", b"<p>x</p>")]), "unsafe path")
    _refused(make_zip([*MEMBERS, ("content/topic.htm", b"<p>x</p>")]), "same path twice")
    # symlink member
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, data in MEMBERS:
            zf.writestr(name, data)
        zi = zipfile.ZipInfo("Content/link.htm")
        zi.create_system = 3
        zi.external_attr = (0o120777) << 16
        zf.writestr(zi, "/etc/passwd")
    _refused(buf.getvalue(), "symbolic link")
    # billion-laughs style entity declaration in a variable set
    lol = (
        '<?xml version="1.0"?>\n<!DOCTYPE v [<!ENTITY a "aaaa"><!ENTITY b "&a;&a;&a;&a;">]>\n'
        '<CatapultVariableSet><Variable Name="x">&b;</Variable></CatapultVariableSet>\n'
    )
    bad = [(n, lol.encode() if n.endswith(".flvar") else d) for n, d in MEMBERS]
    _refused(make_zip(bad), "DTD or entities")
    bad = [
        (n, b"<CatapultToc><TocEntry Title='x'></CatapultToc>" if n.endswith(".fltoc") else d)
        for n, d in MEMBERS
    ]
    _refused(make_zip(bad), "not well-formed")
    bad = [(n, b"<p>caf\xe9</p>" if n == "Content/Topic.htm" else d) for n, d in MEMBERS]
    _refused(make_zip(bad), "not UTF-8")
    # zip bomb: one member inflating far past the ratio limit
    bomb = [*MEMBERS, ("Content/Resources/big.bin", b"\x00" * (config.ZIP_RATIO_MIN_BYTES * 4))]
    _refused(make_zip(bomb), "expands")
    # too many members
    old = config.ZIP_MAX_MEMBERS
    config.ZIP_MAX_MEMBERS = 5
    try:
        _refused(ORIG, "internal parts")
    finally:
        config.ZIP_MAX_MEMBERS = old
    # segment cap
    old = config.MAX_DOCUMENT_SEGMENTS
    config.MAX_DOCUMENT_SEGMENTS = 5
    try:
        _refused(ORIG, "segments")
    finally:
        config.MAX_DOCUMENT_SEGMENTS = old
    # __MACOSX resource forks are copied, never decoded
    mac = [*MEMBERS, ("__MACOSX/Content/._Topic.htm", b"\x00\x05\x16\x07\xff\xfe")]
    data = make_zip(mac)
    assert len(FLT.parse(data).units) == len(FLT.parse(ORIG).units)
    print(
        "  4) hostile zips refused: non-zip, non-Flare, traversal, abs, drive, dup, symlink, DTD, bomb, caps"
    )


# --------------------------------------------------------------------------- 5 surfaces
def test_web_and_api():
    client = TestClient(app)
    r = client.post(
        "/projects",
        data={
            "name": "Flare Web",
            "source_locale": "en-US",
            "target_locales": "de-DE",
            "default_provider": "claude",
        },
        follow_redirects=False,
    )
    assert r.status_code == 303, r.text
    pid = int(r.headers["location"].rsplit("/", 1)[-1])
    r = client.post(
        f"/projects/{pid}/documents",
        data={"mode": "segmented"},
        files={"file": ("Help.flprjzip", ORIG, "application/zip")},
        follow_redirects=False,
    )
    assert r.status_code == 303 and "/documents/" in r.headers["location"], r.text
    doc_id = int(r.headers["location"].rsplit("/", 1)[-1])
    from sqlmodel import Session, select

    with Session(engine) as s:
        segs = s.exec(
            select(Segment).where(Segment.document_id == doc_id, Segment.target_locale == "de-DE")
        ).all()
        rows = [(x.id, x.source_text) for x in segs]
    assert rows and not any("General.ProductName" in t for _i, t in rows), rows
    for sid, src in rows:
        client.post(f"/segments/{sid}/save", data={"target_text": "[de] " + src, "tte_ms": "0"})
    resp = client.get(f"/documents/{doc_id}/export?locale=de-DE")
    assert resp.status_code == 200, resp.text[:300]
    out = {n: d for n, d, _c in members_of(resp.content)}
    assert [m[0] for m in members_of(resp.content)] == [m[0] for m in members_of(ORIG)]
    topic = out["Content/Topic.htm"].decode("utf-8")
    assert "[de] Printing guide" in topic and 'lang="de-DE"' in topic, topic[:400]
    assert '<MadCap:variable name="General.ProductName" />' in topic
    assert out["Project/ConditionTagSets/Default.flcts"] == FLCTS.encode("utf-8")
    assert "[de] Acme Writer" in out["Project/VariableSets/General.flvar"].decode("utf-8")

    # /api/formats discovery lists both extensions with a name.
    from app.web.api_part2 import _FMT_LABELS

    assert "MadCap Flare" in _FMT_LABELS["flprjzip"] and "zip" not in _FMT_LABELS
    from app.services.mime import mime_for_fmt

    assert mime_for_fmt("flprjzip") == "application/zip"
    print("  5) web upload -> segments -> export: zip layout kept, target language declared")


def test_cli():
    init_db()
    code, out, err = run_cli(["--json", "customer", "create", "Flare Cust"])
    assert code == 0, err
    cust = json.loads(out.strip().splitlines()[-1])["id"]
    code, out, err = run_cli(
        [
            "--json",
            "project",
            "create",
            "Flare CLI",
            "--targets",
            "fr-FR",
            "--customer-id",
            str(cust),
        ]
    )
    assert code == 0, err
    pid = json.loads(out.strip().splitlines()[-1])["id"]
    tmp = tempfile.mkdtemp(prefix="loctms_flare_cli_")
    path = os.path.join(tmp, "Help.flprjzip")
    with open(path, "wb") as fh:
        fh.write(ORIG)
    code, out, err = run_cli(["--json", "doc", "upload", str(pid), path])
    assert code == 0, (out, err)
    payload = json.loads(out.strip().splitlines()[-1])
    assert payload["fmt"] == "flprjzip", payload
    doc_id = payload["id"]
    code, out, err = run_cli(["--json", "doc", "translate", str(doc_id), "--locale", "fr-FR"])
    assert code == 0, (out, err)
    dest = os.path.join(tmp, "Help.fr.zip")
    code, out, err = run_cli(
        ["--json", "doc", "export", str(doc_id), "--locale", "fr-FR", "-o", dest]
    )
    assert code == 0, (out, err)
    with open(dest, "rb") as fh:
        got = members_of(fh.read())
    assert [m[0] for m in got] == [m[0] for m in members_of(ORIG)]
    topic = {n: d for n, d, _c in got}["Content/Topic.htm"].decode("utf-8")
    assert (
        "[fr-FR]" in topic
        and '<MadCap:snippetBlock src="../Resources/Snippets/Outer.flsnp" />' in topic
    )
    # A non-Flare zip is refused with the plain reason.
    bad = os.path.join(tmp, "plain.flprjzip")
    with open(bad, "wb") as fh:
        fh.write(make_zip([("readme.txt", b"hello")]))
    code, out, err = run_cli(["doc", "upload", str(pid), bad])
    assert code != 0 and "not a madcap flare project" in (out + err).lower(), (code, out, err)
    print("  6) CLI: doc upload (.flprjzip) -> translate (mock) -> export; non-Flare zip refused")


def test_docs_surfaces():
    for path, needle in (
        ("app/templates/help.html", "MadCap Flare"),
        ("app/templates/project.html", ".flprjzip"),
        ("llms.txt", "MadCap Flare"),
        ("app/capabilities/documents.md", "MadCap Flare"),
        ("docs/CLI.md", "flprjzip"),
    ):
        with open(path, encoding="utf-8") as fh:
            assert needle in fh.read(), (path, needle)
    print("  7) help, upload accept list, llms.txt, capability card and CLI docs name the format")


def test_lazy_registration():
    import subprocess

    code = (
        "import sys, app.filters as f; "
        "assert 'app.filters.flare_filter' not in sys.modules; "
        "assert 'flprjzip' in f.supported_exts() and 'zip' not in f.supported_exts(); "
        "assert f.get_filter('FLPRJZIP').fmt == 'flprjzip'; print('lazy ok')"
    )
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, cwd=_ROOT)
    assert out.returncode == 0 and "lazy ok" in out.stdout, out.stderr[-800:]
    print("  0) lazy registration: app.filters does not import the Flare filter until used")


def main() -> None:
    init_db()
    test_lazy_registration()
    test_extraction()
    test_byte_identity()
    test_translated_build()
    test_xhtml_safe_targets()
    test_hostile()
    test_web_and_api()
    test_cli()
    test_docs_surfaces()
    print("FLARE FILTER OK")


if __name__ == "__main__":
    main()
