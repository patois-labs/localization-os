"""EPUB 2/3 filter (Batch 15, app/filters/epub_filter.py).

Hand-written fixtures, zipped at run time. Covers: registration on every surface, the
unit walk order (OPF title/description, spine XHTML in spine order, nav, NCX labels),
byte-identical round trip with no changes, a translated round trip (mimetype first and
stored with no extra field, CSS/font/image bytes identical, inline tags rematerialized),
build_localized language stamping (dc:language, package xml:lang, XHTML lang/xml:lang,
RTL page-progression-direction, NCX xml:lang), UTF-16 XHTML, percent-encoded hrefs,
font obfuscation accepted, DRM refused, hostile inputs (no mimetype, missing OPF,
entity declarations, zip bomb ratio, segment cap), and an ingest -> translate -> export
pass through the real pipeline with the mock engine.

Hermetic + offline. Final line: "EPUB FILTER OK".
"""

import io
import os
import sys
import zipfile

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(_ROOT)
sys.path.insert(0, _ROOT)
from tests._bootstrap import init_env

init_env("loctms_epub_")

from app.filters import get_filter, supported_exts
from app.filters.base import InputLimitError

LOC = "fr-FR"

CONTAINER = b"""<?xml version="1.0" encoding="UTF-8"?>
<container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container">
  <rootfiles>
    <rootfile full-path="OEBPS/content.opf" media-type="application/oebps-package+xml"/>
  </rootfiles>
</container>
"""

OPF = """<?xml version="1.0" encoding="UTF-8"?>
<package xmlns="http://www.idpf.org/2007/opf" version="3.0" unique-identifier="uid" xml:lang="en">
  <metadata xmlns:dc="http://purl.org/dc/elements/1.1/">
    <dc:identifier id="uid">urn:uuid:12345678-1234-1234-1234-123456789abc</dc:identifier>
    <dc:title id="title">The Little Guide</dc:title>
    <dc:creator>Ada Writer</dc:creator>
    <dc:language>en</dc:language>
    <dc:description>A short book about &amp; for testing.</dc:description>
    <meta property="dcterms:modified">2026-10-10T00:00:00Z</meta>
  </metadata>
  <manifest>
    <item id="nav" href="nav.xhtml" media-type="application/xhtml+xml" properties="nav"/>
    <item id="ncx" href="toc.ncx" media-type="application/x-dtbncx+xml"/>
    <item id="c2" href="text/chapter%202.xhtml" media-type="application/xhtml+xml"/>
    <item id="c1" href="text/ch1.xhtml" media-type="application/xhtml+xml"/>
    <item id="css" href="style.css" media-type="text/css"/>
    <item id="font" href="fonts/f.otf" media-type="font/otf"/>
    <item id="img" href="img/cover.png" media-type="image/png"/>
  </manifest>
  <spine toc="ncx">
    <itemref idref="c1"/>
    <itemref idref="c2"/>
  </spine>
</package>
"""

CH1 = """<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE html>
<html xmlns="http://www.w3.org/1999/xhtml"
  xmlns:epub="http://www.idpf.org/2007/ops" lang="en" xml:lang="en">
<head><title>Chapter One</title><link rel="stylesheet" href="../style.css"/></head>
<body>
<h1>Chapter One</h1>
<p>Hello <b>bold</b> world, you have {count} items and %1$s left.</p>
<p><img src="../img/cover.png" alt="A red cover"/></p>
</body>
</html>
"""

CH2 = """<?xml version="1.0" encoding="UTF-8"?>
<html xmlns="http://www.w3.org/1999/xhtml" lang="en">
<head><title>Chapter Two</title></head>
<body><p>Second chapter text.</p></body>
</html>
"""

NAV = """<?xml version="1.0" encoding="UTF-8"?>
<html xmlns="http://www.w3.org/1999/xhtml"
  xmlns:epub="http://www.idpf.org/2007/ops" lang="en" xml:lang="en">
<head><title>Contents</title></head>
<body><nav epub:type="toc"><ol>
<li><a href="text/ch1.xhtml">Start here</a></li>
<li><a href="text/chapter%202.xhtml">Next steps</a></li>
</ol></nav></body>
</html>
"""

NCX = """<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE ncx PUBLIC "-//NISO//DTD ncx 2005-1//EN" "http://www.daisy.org/z3986/2005/ncx-2005-1.dtd">
<ncx xmlns="http://www.daisy.org/z3986/2005/ncx/" version="2005-1" xml:lang="en">
  <head><meta name="dtb:uid" content="urn:uuid:12345678-1234-1234-1234-123456789abc"/></head>
  <docTitle><text>The Little Guide</text></docTitle>
  <navMap>
    <navPoint id="p1" playOrder="1">
      <navLabel><text>Start here</text></navLabel>
      <content src="text/ch1.xhtml"/>
    </navPoint>
    <navPoint id="p2" playOrder="2">
      <navLabel><text>Next steps</text></navLabel>
      <content src="text/chapter%202.xhtml"/>
    </navPoint>
  </navMap>
</ncx>
"""

CSS = b"body { font-family: 'F'; }\n"
FONT = bytes(range(256)) * 4
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64


def _epub(members=None, *, mimetype=b"application/epub+zip", drop=(), first_mimetype=True):
    base = {
        "META-INF/container.xml": CONTAINER,
        "OEBPS/content.opf": OPF.encode(),
        "OEBPS/nav.xhtml": NAV.encode(),
        "OEBPS/toc.ncx": NCX.encode(),
        "OEBPS/text/ch1.xhtml": CH1.encode(),
        "OEBPS/text/chapter 2.xhtml": CH2.encode(),
        "OEBPS/style.css": CSS,
        "OEBPS/fonts/f.otf": FONT,
        "OEBPS/img/cover.png": PNG,
    }
    base.update(members or {})
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        if mimetype is not None and first_mimetype:
            z.writestr(zipfile.ZipInfo("mimetype"), mimetype)
        for name, data in base.items():
            if name in drop or data is None:
                continue
            z.writestr(name, data, compress_type=zipfile.ZIP_DEFLATED)
        if mimetype is not None and not first_mimetype:
            z.writestr(zipfile.ZipInfo("mimetype"), mimetype)
    return buf.getvalue()


def _text(path):
    with open(path, encoding="utf-8") as fh:
        return fh.read()


def _members(data):
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        return {i.filename: z.read(i) for i in z.infolist()}, z.infolist()


def _well_formed(members):
    from lxml import etree

    for name, raw in members.items():
        if name.endswith((".xhtml", ".opf", ".ncx", ".xml")):
            etree.fromstring(raw, etree.XMLParser(resolve_entities=False, no_network=True))


def _refused(data, needle):
    try:
        get_filter("epub").parse(data)
    except InputLimitError as exc:
        assert needle.lower() in str(exc).lower(), (needle, str(exc))
        return
    raise AssertionError(f"expected a refusal mentioning {needle!r}")


def test_registration():
    assert "epub" in supported_exts()
    assert get_filter("epub").fmt == "epub"
    from app.services.connectors.msgraph import FORMATS
    from app.services.mime import mime_for_fmt
    from app.web.api_part2 import _FMT_LABELS

    assert FORMATS["epub"] == "epub"
    assert mime_for_fmt("epub") == "application/epub+zip"
    assert "epub" in _FMT_LABELS
    html = _text(os.path.join(_ROOT, "app", "templates", "project.html"))
    assert ".epub," in html
    for path in ("llms.txt", "docs/CLI.md", "app/capabilities/documents.md"):
        assert "epub" in _text(os.path.join(_ROOT, path)).lower(), path
    print("  registration + four surfaces OK")


def test_unit_order():
    res = get_filter("epub").parse(_epub())
    texts = [u.text for u in res.units]
    assert texts == [
        "The Little Guide",
        "A short book about & for testing.",
        # spine order: ch1 then chapter 2 (manifest order is the reverse)
        "Chapter One",
        "Chapter One",
        "Hello ⟦g1⟧bold⟦/g1⟧ world, you have {count} items and %1$s left.",
        "A red cover",
        "Chapter Two",
        "Second chapter text.",
        # nav (not in the spine)
        "Contents",
        "⟦g1⟧Start here⟦/g1⟧",
        "⟦g1⟧Next steps⟦/g1⟧",
        # NCX
        "The Little Guide",
        "Start here",
        "Next steps",
    ], texts
    styles = [u.style for u in res.units]
    assert styles[0] == "epub:title" and styles[1] == "epub:description", styles
    assert styles[5] == "a11y:img-alt" and styles[-1] == "epub:toc-label", styles
    assert [u.p_index for u in res.units] == list(range(len(texts)))
    sk = res.skeleton
    assert sk["format"] == "epub" and sk["package"] == "OEBPS/content.opf", sk
    assert [p["kind"] for p in sk["parts"]] == ["opf", "xhtml", "xhtml", "nav", "ncx"]
    assert sum(p["count"] for p in sk["parts"]) == len(texts)
    print("  unit walk order (OPF, spine XHTML, nav, NCX) OK")


def test_identity_round_trip():
    src = _epub()
    f = get_filter("epub")
    assert f.build(src, {}) == src
    same = {str(u.p_index): u.text for u in f.parse(src).units}
    assert f.build(src, same) == src, "re-submitting the source text must change nothing"
    print("  no-change round trip is byte-identical OK")


def test_translated_round_trip():
    src = _epub()
    f = get_filter("epub")
    units = f.parse(src).units
    tr = {str(u.p_index): "FR " + u.text for u in units}
    tr["4"] = "Bonjour ⟦g1⟧gras⟦/g1⟧ monde, vous avez {count} articles et %1$s restants."
    out = f.build(src, tr)
    members, infos = _members(out)
    assert infos[0].filename == "mimetype" and infos[0].compress_type == zipfile.ZIP_STORED
    assert infos[0].extra == b"" and members["mimetype"] == b"application/epub+zip"
    orig, orig_infos = _members(src)
    assert [i.filename for i in infos] == [i.filename for i in orig_infos]
    for name in (
        "OEBPS/style.css",
        "OEBPS/fonts/f.otf",
        "OEBPS/img/cover.png",
        "META-INF/container.xml",
    ):
        assert members[name] == orig[name], name
    ch1 = members["OEBPS/text/ch1.xhtml"].decode()
    assert "<p>Bonjour <b>gras</b> monde, vous avez {count} articles et %1$s restants.</p>" in ch1
    assert 'alt="FR A red cover"' in ch1 and "<!DOCTYPE html>" in ch1
    assert "<title>FR Chapter One</title>" in ch1
    opf = members["OEBPS/content.opf"].decode()
    assert '<dc:title id="title">FR The Little Guide</dc:title>' in opf, opf
    assert "FR A short book about &amp; for testing." in opf
    assert "<dc:creator>Ada Writer</dc:creator>" in opf
    assert opf.startswith('<?xml version="1.0" encoding="UTF-8"?>')
    ncx = members["OEBPS/toc.ncx"].decode()
    assert "<text>FR Start here</text>" in ncx and "<!DOCTYPE ncx" in ncx
    assert 'FR <a href="text/ch1.xhtml">Start here</a>' in members["OEBPS/nav.xhtml"].decode()
    _well_formed(members)
    # the output parses again with the same unit count
    assert len(f.parse(out).units) == len(units)
    # placeholder tokens a model invents never leak into the OPF/NCX
    out2 = f.build(src, {"0": "Titre ⟦g9⟧X⟦/g9⟧"})
    assert "Titre X" in _members(out2)[0]["OEBPS/content.opf"].decode()
    print("  translated round trip (mimetype first+stored, assets identical, tags) OK")


def test_build_localized():
    src = _epub()
    f = get_filter("epub")
    out = f.build_localized(src, {"2": "Chapitre un"}, LOC)
    m, _ = _members(out)
    opf = m["OEBPS/content.opf"].decode()
    assert "<dc:language>fr-FR</dc:language>" in opf and 'xml:lang="fr-FR"' in opf, opf
    assert "page-progression-direction" not in opf
    ch1 = m["OEBPS/text/ch1.xhtml"].decode()
    assert 'lang="fr-FR"' in ch1 and 'xml:lang="fr-FR"' in ch1, ch1[:300]
    ch2 = m["OEBPS/text/chapter 2.xhtml"].decode()
    assert 'lang="fr-FR"' in ch2 and 'xml:lang="fr-FR"' in ch2, ch2[:200]
    assert 'xml:lang="fr-FR"' in m["OEBPS/toc.ncx"].decode()
    assert 'xml:lang="fr-FR"' in m["OEBPS/nav.xhtml"].decode()
    out = f.build_localized(src, {}, "ar")
    m, _ = _members(out)
    opf = m["OEBPS/content.opf"].decode()
    assert 'page-progression-direction="rtl"' in opf, opf
    assert 'dir="rtl"' in m["OEBPS/text/ch1.xhtml"].decode()
    # an unusable locale declares nothing
    assert f.build_localized(src, {}, "-") == src
    print("  build_localized language stamping (OPF, XHTML, NCX, RTL) OK")


def test_encodings_and_epub2():
    ch2_16 = CH2.replace('encoding="UTF-8"', 'encoding="UTF-16"').encode("utf-16")
    src = _epub(
        {
            "OEBPS/text/chapter 2.xhtml": ch2_16,
            "OEBPS/content.opf": b"\xef\xbb\xbf"
            + OPF.replace('version="3.0"', 'version="2.0"')
            .replace(' properties="nav"', "")
            .encode(),
        }
    )
    f = get_filter("epub")
    res = f.parse(src)
    texts = [u.text for u in res.units]
    assert "Second chapter text." in texts and res.skeleton["epub_version"] == "2.0"
    # without the nav property the nav.xhtml is not translated
    assert "Contents" not in texts, texts
    k = texts.index("Second chapter text.")
    out = f.build(src, {str(k): "Texte du deuxième chapitre."})
    m, _ = _members(out)
    # B-bytes: the UTF-16 chapter stays UTF-16 (BOM and its own declaration)
    ch2 = m["OEBPS/text/chapter 2.xhtml"]
    assert ch2[:2] in (b"\xff\xfe", b"\xfe\xff"), ch2[:40]
    assert "Texte du deuxième chapitre." in ch2.decode("utf-16"), ch2
    assert 'encoding="UTF-8"' not in ch2.decode("utf-16"), ch2
    assert f.build(src, {}) == src
    print("  UTF-16 XHTML, BOM OPF, EPUB 2 without nav OK")


def test_font_obfuscation_ok_and_drm_refused():
    enc = """<?xml version="1.0" encoding="UTF-8"?>
<encryption xmlns="urn:oasis:names:tc:opendocument:xmlns:container"
  xmlns:enc="http://www.w3.org/2001/04/xmlenc#">
 <enc:EncryptedData>
  <enc:EncryptionMethod Algorithm="{alg}"/>
  <enc:CipherData><enc:CipherReference URI="{uri}"/></enc:CipherData>
 </enc:EncryptedData>
</encryption>"""
    ok = enc.format(alg="http://www.idpf.org/2008/embedding", uri="OEBPS/fonts/f.otf")
    src = _epub({"META-INF/encryption.xml": ok.encode()})
    assert len(get_filter("epub").parse(src).units) == 14
    out = get_filter("epub").build(src, {"0": "Titre"})
    m, _ = _members(out)
    assert m["OEBPS/fonts/f.otf"] == FONT and m["META-INF/encryption.xml"] == ok.encode()
    drm = enc.format(alg="http://www.w3.org/2001/04/xmlenc#aes128-cbc", uri="OEBPS/text/ch1.xhtml")
    _refused(_epub({"META-INF/encryption.xml": drm.encode()}), "encrypted")
    drm2 = enc.format(alg="http://www.w3.org/2001/04/xmlenc#aes128-cbc", uri="OEBPS/img/cover.png")
    _refused(_epub({"META-INF/encryption.xml": drm2.encode()}), "encrypted")
    obf_text = enc.format(alg="http://www.idpf.org/2008/embedding", uri="OEBPS/text/ch1.xhtml")
    _refused(_epub({"META-INF/encryption.xml": obf_text.encode()}), "encrypted")
    print("  font obfuscation accepted, DRM refused OK")


def test_mimetype_rules():
    _refused(_epub(mimetype=None), "mimetype")
    _refused(_epub(mimetype=b"application/zip"), "mimetype")
    # a container with mimetype NOT first is accepted and repaired on build
    src = _epub(first_mimetype=False)
    out = get_filter("epub").build(src, {"0": "Titre"})
    assert _members(out)[1][0].filename == "mimetype"
    print("  mimetype rules (missing/wrong refused, misplaced repaired) OK")


def test_hostile_inputs():
    _refused(b"not a zip at all", "zip")
    _refused(_epub(drop=("META-INF/container.xml",)), "container.xml")
    _refused(_epub(drop=("OEBPS/content.opf",)), "content.opf")
    bomb = OPF.replace(
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<?xml version="1.0"?><!DOCTYPE package [<!ENTITY a "aaaaaaaaaa">'
        '<!ENTITY b "&a;&a;&a;&a;&a;&a;&a;&a;&a;&a;">]>',
    ).replace("The Little Guide", "&b;")
    _refused(_epub({"OEBPS/content.opf": bomb.encode()}), "entities")
    xxe = OPF.replace(
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<?xml version="1.0"?><!DOCTYPE package [<!ENTITY x SYSTEM "file:///etc/passwd">]>',
    )
    _refused(_epub({"OEBPS/content.opf": xxe.encode()}), "entities")
    _refused(_epub({"OEBPS/content.opf": b"<package"}), "damaged")
    # a container.xml rootfile escaping the root is ignored -> no package document
    esc = CONTAINER.replace(b"OEBPS/content.opf", b"../../etc/passwd")
    _refused(_epub({"META-INF/container.xml": esc}), "package document")
    # a spine href escaping the container is skipped, not read
    opf = OPF.replace('href="text/ch1.xhtml"', 'href="../../../secret.xhtml"')
    texts = [
        u.text for u in get_filter("epub").parse(_epub({"OEBPS/content.opf": opf.encode()})).units
    ]
    assert "Hello" not in " ".join(texts)
    # zip bomb: one highly compressible big member
    big = _epub({"OEBPS/img/zeros.bin": b"\x00" * (3 * 1024 * 1024)})
    _refused(big, "expands")
    print("  hostile inputs (not zip, missing parts, entities, XXE, escapes, bomb) OK")


def test_segment_cap():
    from app import config

    old = config.MAX_DOCUMENT_SEGMENTS
    config.MAX_DOCUMENT_SEGMENTS = 5
    try:
        _refused(_epub(), "MAX_DOCUMENT_SEGMENTS")
    finally:
        config.MAX_DOCUMENT_SEGMENTS = old
    print("  segment cap OK")


def test_pipeline_ingest_translate_export():
    from sqlmodel import Session

    from app.db import engine, init_db
    from app.models import Project, TargetLocale
    from app.services.ingest import ingest_document
    from tests._cli_runner import run_cli

    init_db()
    with Session(engine) as s:
        p = Project(name="epub", org_id=1, source_locale="en-US", default_provider="mock")
        s.add(p)
        s.commit()
        s.refresh(p)
        s.add(TargetLocale(project_id=p.id, locale=LOC))
        s.commit()
        doc = ingest_document(s, p, "book.epub", _epub(), "segmented")
        doc_id = doc.id
        assert doc.fmt == "epub", doc.fmt
    code, out, err = run_cli(["doc", "translate", str(doc_id), "--locale", LOC])
    assert code == 0, (code, out, err)
    import tempfile

    path = os.path.join(tempfile.mkdtemp(prefix="loctms_epub_out_"), "book.fr.epub")
    code, out, err = run_cli(["doc", "export", str(doc_id), "--locale", LOC, "-o", path])
    assert code == 0, (code, out, err)
    with open(path, "rb") as fh:
        data = fh.read()
    m, infos = _members(data)
    assert infos[0].filename == "mimetype"
    assert "<dc:language>fr-FR</dc:language>" in m["OEBPS/content.opf"].decode()
    assert m["OEBPS/fonts/f.otf"] == FONT
    assert "⟦" not in m["OEBPS/text/ch1.xhtml"].decode()
    _well_formed(m)
    assert len(get_filter("epub").parse(data).units) == 14
    print("  ingest -> mock translate -> export through the pipeline OK")


def test_xhtml_safe_targets():
    """Gate fix: a translation with an HTML-only named entity (&nbsp;, &egrave;), an
    unknown entity or a control character must still give well-formed XHTML/OPF. Pre-fix:
    &nbsp; survived verbatim in the chapter and lxml refused it as undefined."""
    src = _epub()
    f = get_filter("epub")
    units = f.parse(src).units
    assert units[7].text == "Second chapter text." and units[5].style == "a11y:img-alt"
    tr = {
        "0": "Titre\x03 du livre",
        "5": "Une couverture\x02 &nbsp;rouge",
        "7": "Deuxi&egrave;me&nbsp;texte &bogus; A&amp;B\x01.",
    }
    members, _infos = _members(f.build(src, tr))
    _well_formed(members)
    ch2 = members["OEBPS/text/chapter 2.xhtml"].decode()
    assert "Deuxi&#232;me&#160;texte &amp;bogus; A&amp;B." in ch2, ch2
    ch1 = members["OEBPS/text/ch1.xhtml"].decode()
    assert 'alt="Une couverture &amp;nbsp;rouge"' in ch1, ch1
    assert "<dc:title" in members["OEBPS/content.opf"].decode()
    assert "Titre du livre" in members["OEBPS/content.opf"].decode()
    print("  XHTML-safe targets (named entities numeric, control chars dropped) OK")


def test_translate_no_734():
    """#734: an XHTML chapter honours translate="no" through the shared html walk."""
    ch = CH1.replace(
        "<p>Hello <b>bold</b>",
        '<p translate="no">Colophon text stays.</p>\n<p>Hello <b translate="no">bold</b>',
    )
    src = _epub({"OEBPS/text/ch1.xhtml": ch.encode()})
    f = get_filter("epub")
    units = f.parse(src).units
    texts = [u.text for u in units]
    assert not any("Colophon" in t or "bold" in t for t in texts), texts
    assert f.build(src, {}) == src
    out = f.build(src, {str(u.p_index): "FR " + u.text for u in units})
    chapter = _members(out)[0]["OEBPS/text/ch1.xhtml"].decode()
    assert '<p translate="no">Colophon text stays.</p>' in chapter, chapter
    assert '<b translate="no">bold</b>' in chapter, chapter
    print("  translate=no chapter content stays verbatim OK")


def main():
    test_registration()
    test_unit_order()
    test_identity_round_trip()
    test_translated_round_trip()
    test_xhtml_safe_targets()
    test_build_localized()
    test_encodings_and_epub2()
    test_font_obfuscation_ok_and_drm_refused()
    test_mimetype_rules()
    test_hostile_inputs()
    test_segment_cap()
    test_pipeline_ingest_translate_export()
    test_translate_no_734()
    print("EPUB FILTER OK")


if __name__ == "__main__":
    main()
