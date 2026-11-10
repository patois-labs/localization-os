"""A token that names no code of a unit is kept as text on export, in every filter (#814 item 1).

G7 made the rule (app/filters/token_lit.py) and applied it to HTML, VTT, SRT, MDX, the XLIFF family
and the Office text boxes. The LocOS-vs-Bilingua differential harness (11-07, 3,765 real files)
showed the other filters still deleted such a token (``⟦x77⟧`` typed by a translator or an MT
engine, or token-shaped text in a source) when they wrote the translation. Each case here, per
filter build path:

* a translation with an unknown token keeps it as text in the export (re-read: the unit text is
  the translation exactly, so the token is literal text and every REAL code still maps back to
  its own markup; where the filter's reader does not give the text back, the output bytes are
  checked instead);
* quality.tag_damage still reports the token (``added``), so the QA gate is unchanged;
* an export whose translations hold no unknown token is the same as before the fix.

Plus: the EBU STL writer refuses a kept token (no EBU code table has the brackets), as Bilingua
does, and an old project (stored rows from before the fix) exports through the real services.

Hand-written fixtures (and tests/fixtures/formats). Hermetic + offline. ASCII output only.
Final line: "UNKNOWN TOKEN KEPT OK".
"""

import hashlib
import io
import os
import sys
import zipfile

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(_ROOT)
sys.path.insert(0, _ROOT)
from tests._bootstrap import init_env

init_env("loctms_unknowntok_")

from app.filters import get_filter
from app.services import quality

X, Y = "\u27e6", "\u27e7"
TOK = X + "x77" + Y


def tok(s: str) -> str:
    return X + s + Y


def _fixture(rel: str) -> bytes:
    with open(os.path.join(_ROOT, "tests", "fixtures", "formats", rel), "rb") as fh:
        return fh.read()


# --------------------------------------------------------------------------- sources

ADOC = b"= Title\n\nPress *Save* to keep `config.yml` now.\n\nPlain paragraph here.\n"
RST = b"Title\n=====\n\nPress **Save** to keep ``config.yml`` now.\n\nPlain paragraph here.\n"
MW = b"== Heading ==\n\nPress '''Save''' to read [[Main Page|the page]] now.\n\nPlain text.\n"
DW = b"====== Heading ======\n\nPress **Save** to read [[wiki:page|the page]] now.\n\nPlain text.\n"
TEX = (
    b"\\documentclass{article}\n\\begin{document}\n\\section{Intro}\n"
    b"Press \\textbf{Save} to keep the file.\n\nPlain paragraph here.\n\\end{document}\n"
)
JS = b"export default {\n  hello: 'Hello {name}, welcome',\n  plain: 'Plain text',\n};\n"
PHP = b"<?php\nreturn [\n    'hello' => 'Hello :name, welcome',\n    'plain' => 'Plain text',\n];\n"
RC = (
    b"#pragma code_page(65001)\nSTRINGTABLE\nBEGIN\n"
    b'    1 "Hello %s, welcome"\n    2 "Plain text"\nEND\n'
)
STF = (
    b"Language code: en_US\r\nType: Source\r\nTranslation type: Metadata\r\n\r\n"
    b"# KEY\tLABEL\r\n\r\nButton.Call\tCall {0} now\r\nButton.Plain\tPlain text\r\n"
)
TOML = b'[hello]\nother = "Hello {{.Name}}, welcome"\n\n[plain]\nother = "Plain text"\n'
TTML = (
    b'<?xml version="1.0" encoding="UTF-8"?>\n'
    b'<tt xmlns="http://www.w3.org/ns/ttml" xmlns:tts="http://www.w3.org/ns/ttml#styling" '
    b'xml:lang="en"><body><div>'
    b'<p begin="00:00:01.000" end="00:00:02.000">Hello '
    b'<span tts:fontStyle="italic">big</span> world</p>'
    b'<p begin="00:00:03.000" end="00:00:04.000">Plain line</p>'
    b"</div></body></tt>\n"
)
DITA = (
    b'<?xml version="1.0" encoding="UTF-8"?>\n<topic id="t"><title>Title</title><body>'
    b"<p>Hello <b>bold</b> world</p><p>Plain text</p></body></topic>\n"
)
XSLT = (
    b'<?xml version="1.0" encoding="UTF-8"?>\n'
    b'<xsl:stylesheet version="1.0" xmlns:xsl="http://www.w3.org/1999/XSL/Transform">'
    b'<xsl:template match="/"><html><body><p>Hello <b>bold</b> world</p>'
    b"<p>Plain text</p></body></html></xsl:template></xsl:stylesheet>\n"
)
MICRODVD = (
    b"{10}{20}{y:i}Hello there|second line\r\n{30}{40}Plain|{y:i}styled words {Y:u}x\r\n"
    b"{50}{60}End{y:b}|next\r\n"
)
XTG = b"<v19.00><e9>\r\n@Body:Hello <B>bold<B> world\r\n@Body:Plain text\r\n"
PLIST = (
    b'<?xml version="1.0" encoding="UTF-8"?>\n<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" '
    b'"http://www.apple.com/DTDs/PropertyList-1.0.dtd">\n<plist version="1.0"><dict>'
    b"<key>Title</key><string>Hello %@, welcome</string>"
    b"<key>Plain</key><string>Plain text</string></dict></plist>\n"
)
XCSTRINGS = (
    b'{\n  "sourceLanguage" : "en",\n  "strings" : {\n'
    b'    "Hello %@" : {\n      "localizations" : {\n        "en" : {\n'
    b'          "stringUnit" : {\n            "state" : "translated",\n'
    b'            "value" : "Hello %@, welcome"\n          }\n        }\n      }\n    },\n'
    b'    "Plain" : {\n      "localizations" : {\n        "en" : {\n'
    b'          "stringUnit" : {\n            "state" : "translated",\n'
    b'            "value" : "Plain text"\n          }\n        }\n      }\n    }\n'
    b'  },\n  "version" : "1.0"\n}\n'
)
ARB = (
    b'{\n  "@@locale": "en",\n  "items": "You have {count, plural, one{one item} '
    b'other{{count} items}} now",\n  "plain": "Plain text"\n}\n'
)
CHROME = (
    b'{\n  "hello": {"message": "Hello $USER$, welcome", '
    b'"placeholders": {"user": {"content": "$1"}}},\n'
    b'  "plain": {"message": "Plain text"}\n}\n'
)
MIF = (
    b"<MIFFile 2019>\n<TextFlow\n <TFTag `A'>\n <Para\n  <PgfTag `Body'>\n  <ParaLine\n"
    b"   <String `Hello '>\n   <Font\n    <FTag `'>\n    <FWeight `Bold'>\n   > # end of Font\n"
    b"   <String `bold'>\n   <Font\n    <FTag `'>\n    <FWeight `Regular'>\n   > # end of Font\n"
    b"   <String ` world'>\n  > # end of ParaLine\n > # end of Para\n"
    b" <Para\n  <PgfTag `Body'>\n  <ParaLine\n   <String `Plain text'>\n  > # end of ParaLine\n"
    b" > # end of Para\n> # end of TextFlow\n"
)


def _dxf() -> bytes:
    pairs = [
        (0, "SECTION"), (2, "HEADER"), (9, "$ACADVER"), (1, "AC1032"),
        (9, "$DWGCODEPAGE"), (3, "ANSI_1252"), (0, "ENDSEC"),
        (0, "SECTION"), (2, "ENTITIES"),
        (0, "MTEXT"), (5, "30"), (8, "0"), (1, r"Hello \Pnext {\fArial|b1;bold} line"),
        (0, "TEXT"), (5, "31"), (8, "0"), (1, "Plain text"),
        (0, "ENDSEC"), (0, "EOF"),
    ]  # fmt: skip
    return "".join(f"{c:>3}\r\n{v}\r\n" for c, v in pairs).encode()


_ICML_HEAD = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
    '<?aid style="50" type="snippet" readerVersion="6.0" featureSet="513" product="20.0(73)" ?>\n'
    '<?aid SnippetType="InCopyInterchange"?>\n'
    '<Document DOMVersion="20.0" Self="d">\n'
    '\t<RootCharacterStyleGroup Self="rcsg"><CharacterStyle Self="CharacterStyle/Bold" '
    'Name="Bold" FontStyle="Bold"/></RootCharacterStyleGroup>\n'
)
_NORMAL = "CharacterStyle/$ID/[No character style]"


def _story_body() -> str:
    def csr(inner, style=_NORMAL):
        return f'<CharacterStyleRange AppliedCharacterStyle="{style}">{inner}</CharacterStyleRange>'

    p = "ParagraphStyle/$ID/NormalParagraphStyle"
    return (
        f'<ParagraphStyleRange AppliedParagraphStyle="{p}">'
        + csr("<Content>Hello </Content>")
        + csr("<Content>bold</Content>", "CharacterStyle/Bold")
        + csr("<Content> world</Content><Br/>")
        + "</ParagraphStyleRange>"
        + f'<ParagraphStyleRange AppliedParagraphStyle="{p}">'
        + csr("<Content>Plain text</Content>")
        + "</ParagraphStyleRange>"
    )


def _icml() -> bytes:
    story = (
        '\t<Story Self="u1" AppliedTOCStyle="n" TrackChanges="false" StoryTitle="$ID/">\n'
        f"{_story_body()}\n\t</Story>\n"
    )
    return (_ICML_HEAD + story + "</Document>\n").encode()


def _zip(members: list, first_stored: str | None = None) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for name, data in members:
            info = zipfile.ZipInfo(name, date_time=(2026, 11, 7, 9, 0, 0))
            info.compress_type = (
                zipfile.ZIP_STORED if name == first_stored else zipfile.ZIP_DEFLATED
            )
            z.writestr(info, data)
    return buf.getvalue()


def _idml() -> bytes:
    ns = "http://ns.adobe.com/AdobeInDesign/idml/1.0/packaging"
    story = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
        f'<idPkg:Story xmlns:idPkg="{ns}" DOMVersion="20.0">'
        f'<Story Self="u1" AppliedTOCStyle="n" TrackChanges="false">{_story_body()}</Story>'
        "</idPkg:Story>\n"
    ).encode()
    design = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
        f'<Document xmlns:idPkg="{ns}" DOMVersion="20.0" Self="d">'
        '<idPkg:Spread src="Spreads/Spread_s1.xml"/><idPkg:Story src="Stories/Story_u1.xml"/>'
        "</Document>\n"
    ).encode()
    spread = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
        f'<idPkg:Spread xmlns:idPkg="{ns}" '
        'DOMVersion="20.0"><Spread Self="s1"/></idPkg:Spread>\n'
    ).encode()
    return _zip(
        [
            ("mimetype", b"application/vnd.adobe.indesign-idml-package"),
            ("designmap.xml", design),
            ("META-INF/container.xml", b'<?xml version="1.0"?><container/>'),
            ("Spreads/Spread_s1.xml", spread),
            ("Stories/Story_u1.xml", story),
        ],
        "mimetype",
    )


_ODF_NS = (
    'xmlns:office="urn:oasis:names:tc:opendocument:xmlns:office:1.0" '
    'xmlns:style="urn:oasis:names:tc:opendocument:xmlns:style:1.0" '
    'xmlns:text="urn:oasis:names:tc:opendocument:xmlns:text:1.0" '
    'xmlns:table="urn:oasis:names:tc:opendocument:xmlns:table:1.0" office:version="1.3"'
)


def _odf(mimetype: str, body: str) -> bytes:
    content = (
        f'<?xml version="1.0" encoding="UTF-8"?>\n<office:document-content {_ODF_NS}>'
        f"<office:body>{body}</office:body></office:document-content>"
    ).encode()
    manifest = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<manifest:manifest xmlns:manifest="urn:oasis:names:tc:opendocument:xmlns:manifest:1.0" '
        f'manifest:version="1.3"><manifest:file-entry manifest:full-path="/" '
        f'manifest:media-type="{mimetype}"/><manifest:file-entry manifest:full-path="content.xml" '
        'manifest:media-type="text/xml"/></manifest:manifest>'
    ).encode()
    return _zip(
        [
            ("mimetype", mimetype.encode()),
            ("content.xml", content),
            ("META-INF/manifest.xml", manifest),
        ],
        "mimetype",
    )


def _odt() -> bytes:
    return _odf(
        "application/vnd.oasis.opendocument.text",
        '<office:text><text:p>Hello <text:span text:style-name="T1">bold</text:span> world</text:p>'
        "<text:p>Plain text</text:p></office:text>",
    )


def _ods() -> bytes:
    return _odf(
        "application/vnd.oasis.opendocument.spreadsheet",
        '<office:spreadsheet><table:table table:name="S"><table:table-row>'
        '<table:table-cell office:value-type="string"><text:p>Hello <text:span '
        'text:style-name="T1">bold</text:span> cell</text:p></table:table-cell>'
        '<table:table-cell office:value-type="string"><text:p>Plain text</text:p>'
        "</table:table-cell>"
        "</table:table-row></table:table></office:spreadsheet>",
    )


def _epub() -> bytes:
    container = (
        b'<?xml version="1.0" encoding="UTF-8"?>\n<container version="1.0" '
        b'xmlns="urn:oasis:names:tc:opendocument:xmlns:container"><rootfiles><rootfile '
        b'full-path="OEBPS/content.opf" media-type="application/oebps-package+xml"/>'
        b"</rootfiles></container>\n"
    )
    opf = (
        b'<?xml version="1.0" encoding="UTF-8"?>\n<package xmlns="http://www.idpf.org/2007/opf" '
        b'version="3.0" unique-identifier="uid" xml:lang="en"><metadata '
        b'xmlns:dc="http://purl.org/dc/elements/1.1/">'
        b'<dc:identifier id="uid">urn:uuid:1</dc:identifier>'
        b"<dc:title>The Little Guide</dc:title><dc:language>en</dc:language>"
        b'<meta property="dcterms:modified">2026-11-07T00:00:00Z</meta></metadata><manifest>'
        b'<item id="c1" href="ch1.xhtml" media-type="application/xhtml+xml"/></manifest>'
        b'<spine><itemref idref="c1"/></spine></package>\n'
    )
    ch1 = (
        b'<?xml version="1.0" encoding="UTF-8"?>\n<html xmlns="http://www.w3.org/1999/xhtml" '
        b'lang="en"><head><title>One</title></head><body><p>Hello <b>bold</b> world</p>'
        b"</body></html>\n"
    )
    return _zip(
        [
            ("mimetype", b"application/epub+zip"),
            ("META-INF/container.xml", container),
            ("OEBPS/content.opf", opf),
            ("OEBPS/ch1.xhtml", ch1),
        ],
        "mimetype",
    )


# (name, extension, source bytes, check): check "reread" = re-read units equal the translation;
# "bytes" = the output text holds the translation's literal token (the reader does not give
# the translation back as unit text, e.g. a bilingual file or a placeholder-normalising reader)
CASES = [
    ("asciidoc", "adoc", ADOC, "reread"),
    ("rst", "rst", RST, "reread"),
    ("mediawiki", "wiki", MW, "reread"),
    ("dokuwiki", "dokuwiki", DW, "reread"),
    ("latex", "tex", TEX, "reread"),
    ("dxf", "dxf", _dxf(), "reread"),
    ("js module", "js", JS, "bytes"),
    ("php array", "php", PHP, "bytes"),
    ("rc", "rc", RC, "reread"),
    ("stf", "stf", STF, "bytes"),
    ("toml", "toml", TOML, "reread"),
    ("ttml", "ttml", TTML, "reread"),
    ("dita inline", "dita", DITA, "reread"),
    ("dita", "dita", _fixture("xmlits/task.dita"), "reread"),
    ("docbook", "dbk", _fixture("xmlits/book5.dbk"), "reread"),
    ("xslt", "xslt", XSLT, "reread"),
    ("xmb", "xmb", _fixture("angular/messages.xmb"), "reread"),
    ("xtb", "xtb", _fixture("angular/messages.fr.xtb"), "bytes"),
    # a MicroDVD style code styles its whole line, so text after a pair on that line is read
    # back inside it: checked on the bytes
    ("microdvd", "sub", MICRODVD, "bytes"),
    ("xpress tags", "xtg", XTG, "bytes"),
    ("mif", "mif", MIF, "bytes"),
    ("plist", "plist", PLIST, "reread"),
    ("xcstrings", "xcstrings", XCSTRINGS, "reread"),
    ("arb icu", "arb", ARB, "reread"),
    ("chrome json", "json", CHROME, "reread"),
    ("icml", "icml", _icml(), "reread"),
    ("idml", "idml", _idml(), "reread"),
    ("odt", "odt", _odt(), "reread"),
    ("ods", "ods", _ods(), "reread"),
    ("epub", "epub", _epub(), "reread"),
]


def _texts_of(out: bytes) -> str:
    """Every text member of the output, decoded (zip members joined)."""
    blobs = [out]
    if out[:2] == b"PK":
        with zipfile.ZipFile(io.BytesIO(out)) as z:
            blobs = [z.read(n) for n in z.namelist()]
    parts = []
    for b in blobs:
        for enc in ("utf-8-sig", "utf-16"):
            try:
                parts.append(b.decode(enc))
                break
            except UnicodeDecodeError:
                continue
    return "\n".join(parts)


def _translations(units) -> dict:
    """Every live unit translated with the unknown token appended: its own text (so every real
    code is still in it) plus the literal token."""
    return {str(u.p_index): u.text + " " + TOK for u in units if u.text.strip()}


def check_case(name: str, ext: str, src: bytes, mode: str) -> None:
    f = get_filter(ext)
    assert f is not None, f"{name}: no filter for .{ext}"
    units = f.parse(src).units
    texts = _translations(units)
    assert texts, f"{name}: the fixture has no unit"
    assert any(X in u.text for u in units), f"{name}: the fixture has no real code"
    out = bytes(f.build(src, texts))
    body = _texts_of(out)
    assert TOK in body, f"{name}: the unknown token was deleted from the export"
    # every real code went back to its markup: the only literal tokens are the typed ones
    assert body.count(X) == body.count(TOK), f"{name}: a real code was written as text"
    if mode == "reread":
        again = {str(u.p_index): u.text for u in f.parse(out).units}
        for k, want in texts.items():
            assert again.get(k) == want, f"{name} unit {k}: {again.get(k)!r} != {want!r}"
    # the QA gate still sees it: tag_damage reports the added token
    for u in units:
        if u.text.strip():
            why = quality.tag_damage(u.text, u.text + " " + TOK)
            assert "added" in why and TOK in why, f"{name}: tag_damage said {why!r}"
    # without the unknown token the export is what it was: the build of the plain texts holds
    # no literal token at all
    plain = bytes(f.build(src, {k: v[: -len(" " + TOK)] for k, v in texts.items()}))
    assert "x77" not in _texts_of(plain), name


def test_filters():
    for name, ext, src, mode in CASES:
        check_case(name, ext, src, mode)
    print(f"  {len(CASES)} filter build paths keep an unknown token as text")


def test_real_code_still_maps():
    """An unknown token beside a real one: the real code is written as its markup."""
    f = get_filter("rst")
    u = f.parse(RST).units[1]
    assert u.text.count(X) >= 2, u.text
    out = f.build(RST, {"1": u.text.replace("Press", f"Press {TOK}")}).decode()
    assert f"Press {TOK} **Save**" in out, out
    f = get_filter("wiki")
    out = f.build(MW, {"1": f"{tok('x9')} Press {tok('g1')}Save{tok('/g1')} {TOK}"}).decode()
    assert "'''Save'''" in out and TOK in out, out
    f = get_filter("dita")
    u = next(u for u in f.parse(DITA).units if X in u.text)
    out = f.build(DITA, {str(u.p_index): u.text + " " + TOK}).decode()
    assert f"<p>Hello <b>bold</b> world {TOK}</p>" in out, out
    # a repeated real code is still dropped (only a token naming NO code is text)
    out = f.build(DITA, {str(u.p_index): u.text + " " + tok("g1")}).decode()
    assert X not in out and out.count("<b>") == 1, out
    f = get_filter("ttml")
    u = f.parse(TTML).units[0]
    out = f.build(TTML, {"0": u.text + " " + TOK}).decode()
    assert 'tts:fontStyle="italic">big</span> world ' + TOK + "</p>" in out, out
    print("  real codes still map; a repeated real code is still dropped")


def test_json_translation_not_thrown_away():
    """ICU (ARB) and Chrome JSON used to throw the whole translation away when it held a token
    number outside the message: the source came back untranslated."""
    f = get_filter("arb")
    out = f.build(ARB, {"0": f"Sie haben {tok('x1')} jetzt {TOK}"}).decode()
    want = "Sie haben {count, plural, one{one item} other{{count} items}} jetzt " + TOK
    assert f'"items": "{want}"' in out, out
    f = get_filter("json")
    u = f.parse(CHROME).units[0]
    out = f.build(CHROME, {"0": u.text.replace("Hello", "Hallo") + " " + TOK}).decode()
    assert "Hallo $USER$, welcome " + TOK in out, out
    print("  ARB / Chrome JSON keep the translation and the token")


def test_stl_refuses():
    """EBU STL: the brackets are in no EBU code table, so a kept token refuses the export (as
    Bilingua does) instead of shipping a subtitle with the token silently deleted."""
    from app.filters.scc_filter import CaptionTargetError

    g = bytearray(b" " * 1024)
    g[0:3], g[3:11], g[11:12], g[12:14], g[14:16] = b"850", b"STL25.01", b"1", b"00", b"09"
    g[238:243] = g[243:248] = b"00001"
    g[248:251], g[251:253], g[253:255], g[255:256] = b"001", b"40", b"23", b"1"
    head = (
        bytes([0])
        + (1).to_bytes(2, "little")
        + bytes([0xFF, 0, 10, 0, 1, 0, 10, 0, 3, 0, 20, 2, 0])
    )
    tf = b"\x80Italic\x81 text"
    stl = bytes(g) + head + tf + b"\x8f" * (112 - len(tf))
    f = get_filter("stl")
    u = f.parse(stl).units[0]
    assert X in u.text, u.text
    try:
        f.build(stl, {"0": u.text + " " + TOK})
    except CaptionTargetError as exc:
        assert "code table" in str(exc) and X in str(exc), exc
    else:
        raise AssertionError("STL wrote a subtitle without the typed token")
    out = f.build(stl, {"0": u.text.replace("text", "Text")})
    assert b"\x80Italic\x81 Text" in out
    print("  STL refuses a kept token like Bilingua; real codes still write")


RST_LOOK = f"Title\n=====\n\nUse {tok('x9')} as **the** marker.\n".encode()
OLD_FILES = (
    ("a.rst", RST),
    ("a.wiki", MW),
    ("a.ttml", TTML),
    ("a.dxf", _dxf()),
    ("look.rst", RST_LOOK),
)


def old_project_exports() -> dict:
    """Each file imported as a project document through the real services, then exported twice:
    every target "FR <source>", and every target "FR <source> <typed token>".
    {name: (stored source texts, sha256[:16] of the first export, the second export)}."""
    from sqlmodel import Session, select

    from app.db import engine, init_db
    from app.models import Document, Project, Segment, SegStatus, TargetLocale, TransMode
    from app.services import export
    from app.services.ingest import ingest_document

    init_db()
    with Session(engine) as s:
        p = Project(name="old-tokens", org_id=1, source_locale="en-US", default_provider="mock")
        s.add(p)
        s.commit()
        s.refresh(p)
        s.add(TargetLocale(project_id=p.id, locale="fr-FR"))
        s.commit()
        docs = {
            name: ingest_document(s, p, name, data, TransMode.SEGMENTED).id
            for name, data in OLD_FILES
        }
        s.commit()

    def export_with(doc_id, fn):
        with Session(engine) as s:
            for seg in s.exec(select(Segment).where(Segment.document_id == doc_id)).all():
                seg.target_text = fn(seg.source_text)
                seg.status = SegStatus.MT
                s.add(seg)
            s.commit()
            return export.build_translated_file(s, s.get(Document, doc_id), "fr-FR")

    out = {}
    for name, doc_id in docs.items():
        with Session(engine) as s:
            q = select(Segment).where(Segment.document_id == doc_id).order_by(Segment.id)
            stored = [g.source_text for g in s.exec(q).all()]
        plain = export_with(doc_id, lambda t: "FR " + t)
        kept = export_with(doc_id, lambda t: "FR " + t + " " + TOK)
        out[name] = (stored, hashlib.sha256(plain).hexdigest()[:16], kept)
    return out


def test_old_project_export():
    """An old project through the real services (the fix is build side only, so stored rows are
    what an import before the fix stored): a project whose targets hold no token that names no
    code exports the bytes the code before the fix wrote (sha256 pinned from build/issues-1107 at
    16147e1c0); token-shaped text in a SOURCE (look.rst), which the export used to delete from
    the translation, is now kept. A target with a TYPED token is still caught by the export's
    tag-damage gate (default policy "source": that unit falls back to its source), unchanged."""
    got = old_project_exports()
    for name, (_stored, digest, kept) in got.items():
        if name in OLD_EXPORT:
            assert digest == OLD_EXPORT[name], f"{name}: export changed ({digest})"
        assert "x77" not in kept.decode("utf-8"), f"{name}: the tag-damage gate let it through"
    stored, digest, _kept = got["look.rst"]
    assert stored == ["Title", f"Use {tok('x9')} as {tok('g1')}the{tok('/g1')} marker."], stored
    assert digest == "a934dec64ac960df", digest  # pre-fix a9ac55033c7da615: the x9 text deleted
    print("  old projects: plain exports unchanged; a source look-alike is kept on export")


def test_old_project_lookalike_bytes():
    """The look-alike export itself (what a934dec64ac960df pins)."""
    f = get_filter("rst")
    u = f.parse(RST_LOOK).units[1]
    out = f.build(RST_LOOK, {"1": "FR " + u.text}).decode()
    assert f"FR Use {tok('x9')} as **the** marker." in out, out
    print("  rst source look-alike written as text")


# sha256[:16] of the "FR <source>" export, written by build/issues-1107 at 16147e1c0 (pre-fix)
OLD_EXPORT = {
    "a.rst": "0e3a83c60a91dc3e",
    "a.wiki": "50ac369c99549b7c",
    "a.ttml": "91810de11c1191c0",
    "a.dxf": "be597c681ace4faf",
}


def main():
    test_filters()
    test_real_code_still_maps()
    test_json_translation_not_thrown_away()
    test_stl_refuses()
    test_old_project_export()
    test_old_project_lookalike_bytes()
    print("UNKNOWN TOKEN KEPT OK")


if __name__ == "__main__":
    main()
