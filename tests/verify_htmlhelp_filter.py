"""HTML Help filter (app/filters/htmlhelp_filter.py): .hhc table of contents, .hhk index and
.hhp project files. Name / Keyword translated, every other param untouched, See Also follows
its keyword, Windows ANSI code pages decoded explicitly and re-encoded in the target language's
code page (clear refusal or the UTF-8 fallback), hostile input, caps, API + CLI round trip.

Hand-written fixtures only. Hermetic + offline. Final line: "HTMLHELP FILTER OK".
"""

import contextlib
import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(_ROOT)
sys.path.insert(0, _ROOT)
from tests._bootstrap import bilingua_switch_off, init_env

init_env("loctms_htmlhelp_")

from app import config
from app.filters import get_filter, supported_exts
from app.filters.base import EmptyDocumentError, InputLimitError, UnsupportedEncodingError
from app.filters.htmlhelp_filter import (
    HtmlHelpEncodingError,
    target_code_page,
    target_lcid,
)
from app.filters.xml_text import XmlTargetError

HHC_F = get_filter("hhc")
HHK_F = get_filter("hhk")
HHP_F = get_filter("hhp")

HHC = (
    '<!DOCTYPE HTML PUBLIC "-//IETF//DTD HTML//EN">\r\n'
    "<HTML>\r\n<HEAD>\r\n"
    '<meta name="GENERATOR" content="Microsoft&reg; HTML Help Workshop 4.1">\r\n'
    "<!-- Sitemap 1.0 -->\r\n"
    "</HEAD><BODY>\r\n"
    '<OBJECT type="text/site properties">\r\n'
    '\t<param name="ImageType" value="Folder">\r\n'
    '\t<param name="Font" value="Tahoma,8,0">\r\n'
    "</OBJECT>\r\n"
    "<UL>\r\n"
    '\t<LI> <OBJECT type="text/sitemap">\r\n'
    '\t\t<param name="Name" value="Getting started">\r\n'
    '\t\t<param name="Local" value="html\\start.htm">\r\n'
    '\t\t<param name="ImageNumber" value="1">\r\n'
    "\t\t</OBJECT>\r\n"
    "\t<UL>\r\n"
    '\t\t<LI> <OBJECT type="text/sitemap">\r\n'
    '\t\t\t<param name="Name" value="Caf\xe9 &amp; &quot;Bar&quot; &lt;tips&gt;">\r\n'
    '\t\t\t<param name="Local" value="html/caf\xe9.htm#top">\r\n'
    "\t\t\t</OBJECT>\r\n"
    '\t\t<LI> <OBJECT type="text/sitemap">\r\n'
    "\t\t\t<PARAM NAME=name VALUE='Use %1 and {0} here'>\r\n"
    "\t\t\t<param name=URL value=http://example.test/x.htm>\r\n"
    "\t\t\t</OBJECT>\r\n"
    '\t\t<LI> <OBJECT type="text/sitemap">\r\n'
    '\t\t\t<param name="Name" value="---">\r\n'
    '\t\t\t<param name="Comment" value="Not translated">\r\n'
    "\t\t\t</OBJECT>\r\n"
    "\t</UL>\r\n"
    '\t<LI> <OBJECT type="text/sitemap">\r\n'
    '\t\t<param name="Name" value="Unquoted">\r\n'
    '\t\t<param name="Merge" value="other.chm::/toc.hhc">\r\n'
    "\t\t</OBJECT>\r\n"
    "</UL>\r\n"
    '<param name="Name" value="Outside any object">\r\n'
    "</BODY></HTML>\r\n"
)
HHC_UNITS = [
    "Getting started",
    'Caf\xe9 & "Bar" <tips>',
    "Use %1 and {0} here",
    "Unquoted",
]

HHK = (
    '<!DOCTYPE HTML PUBLIC "-//IETF//DTD HTML//EN">\n'
    '<HTML><HEAD><meta http-equiv="Content-Type" content="text/html; charset=windows-1252">\n'
    "<!-- Sitemap 1.0 -->\n</HEAD><BODY>\n<UL>\n"
    '\t<LI> <OBJECT type="text/sitemap">\n'
    '\t\t<param name="Name" value="printing">\n'
    '\t\t<param name="Name" value="How to print">\n'
    '\t\t<param name="Local" value="print.htm">\n'
    '\t\t<param name="Name" value="Print settings">\n'
    '\t\t<param name="Local" value="settings.htm">\n'
    "\t\t</OBJECT>\n"
    "\t<UL>\n"
    '\t\t<LI> <OBJECT type="text/sitemap">\n'
    '\t\t\t<param name="Keyword" value="page setup">\n'
    '\t\t\t<param name="Local" value="setup.htm">\n'
    "\t\t\t</OBJECT>\n"
    "\t</UL>\n"
    '\t<LI> <OBJECT type="text/sitemap">\n'
    '\t\t<param name="Name" value="output">\n'
    '\t\t<param name="See Also" value="printing">\n'
    "\t\t</OBJECT>\n"
    '\t<LI> <OBJECT type="text/sitemap">\n'
    '\t\t<param name="Name" value="paper">\n'
    '\t\t<param name="See Also" value="no such keyword">\n'
    "\t\t</OBJECT>\n"
    "</UL>\n</BODY></HTML>\n"
)

HHP = (
    "[OPTIONS]\r\n"
    "Compatibility=1.1 or later\r\n"
    "Compiled file=manual.chm\r\n"
    "Contents file=toc.hhc\r\n"
    "Default topic=start.htm\r\n"
    "Display compile progress=No\r\n"
    "Index file=index.hhk\r\n"
    "Language=0x409 English (United States)\r\n"
    "Title=Product Manual\r\n"
    "\r\n"
    "[WINDOWS]\r\n"
    'main="Product Help","toc.hhc","index.hhk","start.htm","start.htm",,,,,0x23520,,'
    "0x387e,,,,,,,,0\r\n"
    "\r\n"
    "[FILES]\r\n"
    "start.htm\r\n"
    "; Title=not an option here\r\n"
    "\r\n"
    "[INFOTYPES]\r\n"
)


def _texts(filt, data: bytes):
    return [u.text for u in filt.parse(data).units]


def _refused(filt, data: bytes, exc=ValueError, needle: str = "") -> str:
    try:
        filt.parse(data)
    except exc as e:
        assert needle.lower() in str(e).lower(), (needle, str(e))
        return str(e)
    raise AssertionError(f"expected refusal for {data[:60]!r}")


def test_registration():
    for ext in ("hhc", "hhk", "hhp"):
        assert ext in supported_exts(), ext
        assert get_filter(ext).fmt == ext
    from app.services.connectors import msgraph

    assert msgraph.FORMATS.get("hhc") == "hhc" and msgraph.FORMATS.get("hhp") == "hhp"
    with open(os.path.join(_ROOT, "app", "templates", "project.html"), encoding="utf-8") as fh:
        html = fh.read()
    assert ".hhc,.hhk,.hhp," in html
    print("  [reg] hhc/hhk/hhp registered, upload accept list and msgraph map OK")


def test_hhc_parse_and_identity():
    data = HHC.encode("cp1252")
    parsed = HHC_F.parse(data)
    assert [u.text for u in parsed.units] == HHC_UNITS, [u.text for u in parsed.units]
    assert parsed.skeleton["source_encoding"] == "cp1252"
    assert HHC_F.build(data, {}) == data
    assert HHC_F.build(data, {str(i): t for i, t in enumerate(HHC_UNITS)}) == data
    print("  [hhc] Name values only (both quote styles, unquoted, entities); identity OK")


def test_hhc_translated_round_trip():
    data = HHC.encode("cp1252")
    out = HHC_F.build(
        data,
        {"0": "Erste Schritte", "1": 'Caf\xe9 & "Bar" <Tipps>', "2": "%1 und {0} hier", "3": "X"},
    )
    exp = (
        HHC.replace('value="Getting started"', 'value="Erste Schritte"')
        .replace("&lt;tips&gt;", "&lt;Tipps&gt;")
        .replace("VALUE='Use %1 and {0} here'", "VALUE='%1 und {0} hier'")
        .replace('value="Unquoted"', 'value="X"')
    ).encode("cp1252")
    assert out == exp, out.decode("cp1252")
    assert _texts(HHC_F, out) == [
        "Erste Schritte",
        'Caf\xe9 & "Bar" <Tipps>',
        "%1 und {0} hier",
        "X",
    ]
    # untouched params byte for byte
    for keep in (
        b'value="html\\start.htm"',
        b'value="html/caf\xe9.htm#top"',
        b"value=http://example.test/x.htm",
        b'value="Not translated"',
        b'value="other.chm::/toc.hhc"',
        b'value="Outside any object"',
        b'value="Tahoma,8,0"',
        b'value="1"',
    ):
        assert keep in out, keep
    # an unquoted value gets quotes when it is rewritten; a single-quoted one escapes '
    src = b'<OBJECT type="text/sitemap"><param name=Name value=Plain></OBJECT>'
    assert HHC_F.build(src, {"0": "Two words"}) == (
        b'<OBJECT type="text/sitemap"><param name=Name value="Two words"></OBJECT>'
    )
    out = HHC_F.build(data, {"2": 'it\'s "x"'})
    assert b"VALUE='it&#39;s &quot;x&quot;'" in out, out
    assert _texts(HHC_F, out)[2] == 'it\'s "x"'
    print("  [hhc] translated splice, placeholders, quoting and escaping OK")


def test_hhk_index_and_see_also():
    data = HHK.encode("cp1252")
    texts = _texts(HHK_F, data)
    assert texts == [
        "printing",
        "How to print",
        "Print settings",
        "page setup",
        "output",
        "paper",
    ], texts
    assert HHK_F.build(data, {}) == data
    out = HHK_F.build_localized(
        data, {"0": "impression", "1": "Comment imprimer", "3": "mise en page"}, "fr-FR"
    ).decode("cp1252")
    assert '<param name="See Also" value="impression">' in out, out
    assert '<param name="See Also" value="no such keyword">' in out
    assert '<param name="Keyword" value="mise en page">' in out
    assert "charset=windows-1252" in out  # already right for French: untouched
    out = HHK_F.build_localized(data, {"0": "печать"}, "ru").decode("cp1251")
    assert "charset=windows-1251" in out, out
    assert '<param name="See Also" value="печать">' in out
    print("  [hhk] keywords, extra topic titles, Keyword param, See Also follows keyword OK")


def test_code_pages_and_refusal():
    data = HHC.encode("cp1252")
    assert target_code_page("ru-RU") == 1251 and target_code_page("ja") == 932
    assert target_code_page("zh-Hant") == 950 and target_code_page("zh-CN") == 936
    assert target_code_page("sr-Latn-RS") == 1250 and target_code_page("sr") == 1251
    assert target_code_page("hi-IN") is None
    clean = HHC.replace("é", "e").encode("cp1252")
    out = HHC_F.build_localized(clean, {"0": "日本語"}, "ja-JP")
    assert out.decode("cp932").count("日本語") == 1
    assert b'value="Tahoma,8,128"' in out  # Font charset = SHIFTJIS_CHARSET
    # untranslated source text counts too: the e-acute in a Local path has no cp932 byte
    try:
        HHC_F.build_localized(data, {"0": "日本語"}, "ja-JP")
    except HtmlHelpEncodingError as e:
        assert "U+00E9" in str(e), str(e)
    else:
        raise AssertionError("e-acute cannot be written in cp932")
    try:
        HHC_F.build_localized(data, {"0": "Ж"}, "el-GR")
    except HtmlHelpEncodingError as e:
        assert isinstance(e, XmlTargetError)  # export maps this to a clean refusal
        assert (
            "Windows-1253" in str(e) and "U+0416" in str(e) and "HTMLHELP_UTF8_FALLBACK" in str(e)
        )
    else:
        raise AssertionError("Cyrillic in a Greek ANSI file must be refused")
    # a language with no ANSI code page tries the source code page
    out = HHC_F.build_localized(data, {"0": "Anza"}, "sw")
    assert out.startswith(b"<!DOCTYPE") and b'value="Anza"' in out
    try:
        HHC_F.build_localized(data, {"0": "शुरू"}, "hi-IN")
    except HtmlHelpEncodingError:
        pass
    else:
        raise AssertionError("Devanagari cannot be written in an ANSI HHC")
    # UTF-8 fallback: written as UTF-8, meta charset (when present) relabelled
    config.HTMLHELP_UTF8_FALLBACK = True
    try:
        out = HHC_F.build_localized(data, {"0": "शुरू"}, "hi-IN")
        assert "शुरू" in out.decode("utf-8")
        hhk = HHK.encode("cp1252")
        out = HHK_F.build_localized(hhk, {"0": "日本"}, "ru-RU").decode("utf-8")
        assert "charset=UTF-8" in out and 'value="日本"' in out, out
    finally:
        config.HTMLHELP_UTF8_FALLBACK = False
    print("  [cp] target ANSI code pages, Font charset, clear refusal, UTF-8 fallback OK")


def test_vietnamese_cp1258_681():
    """#681: Windows-1258 writes toned Vietnamese letters as base + combining tone mark;
    precomposed (NFC) text was refused outright. A character with no form there still is."""
    import unicodedata

    clean = HHC.replace("é", "e").encode("cp1252")
    vi = "Tổng quan về điện thoại Việt Nam"
    out = HHC_F.build_localized(clean, {"0": vi}, "vi-VN")
    text = out.decode("cp1258")
    assert unicodedata.normalize("NFC", text).count(vi) == 1, text
    assert "ổ" in text and "ệ" in text  # Windows' own 1258 forms
    assert b"T\xf4\xd2ng" in out, out
    try:
        HHC_F.build_localized(clean, {"0": "Việt 日本"}, "vi-VN")
    except HtmlHelpEncodingError as e:
        assert "U+65E5" in str(e), str(e)
    else:
        raise AssertionError("CJK cannot be written in cp1258")
    print("  [cp] Vietnamese Windows-1258 base + combining tone marks (#681) OK")


def test_typographic_fallback_715():
    """#715: a French narrow no-break space (U+202F) before ':' refused the whole export.
    Typographic spaces, hyphens and zero-width marks the code page lacks are written as the
    nearest code-page character; letters with no representation are still refused."""
    clean = HHC.replace("\u00e9", "e").encode("cp1252")
    fr = "Aide\u202f: index\u2009\u2011 a\u2212b\u200bc \u00abx\u202f\u00bb \u2026"
    out = HHC_F.build_localized(clean, {"0": fr}, "fr-FR")
    text = out.decode("cp1252")
    assert "Aide\u00a0: index -" in text, ascii(text)
    assert "a-bc \u00abx\u00a0\u00bb \u2026" in text, ascii(text)
    # Russian (cp1251) gets the same mapping
    ru = HHC_F.build_localized(
        clean, {"0": "\u0421\u043f\u0440\u0430\u0432\u043a\u0430\u202f\u2011"}, "ru-RU"
    )
    assert "\u0421\u043f\u0440\u0430\u0432\u043a\u0430\u00a0-" in ru.decode("cp1251"), ru
    try:
        HHC_F.build_localized(clean, {"0": "Aide\u202f: \u65e5"}, "fr-FR")
    except HtmlHelpEncodingError as e:
        assert "U+65E5" in str(e), str(e)
    else:
        raise AssertionError("CJK cannot be written in cp1252")
    print("  [cp] typographic spaces / hyphens / zero-width mapped, letters refused (#715) OK")


def test_encodings_and_bom():
    # pure ASCII: ANSI, no source_encoding noise; unchanged bytes on build
    ascii_hhc = HHC.replace("\xe9", "e").encode("ascii")
    p = HHC_F.parse(ascii_hhc)
    assert p.skeleton["source_encoding"] == "cp1252"
    assert HHC_F.build(ascii_hhc, {}) == ascii_hhc
    # declared charset (Russian source)
    ru = (
        HHK.replace("windows-1252", "windows-1251")
        .replace('value="printing"', 'value="печать"')
        .replace('"See Also" value="printing"', '"See Also" value="печать"')
    )
    rb = ru.encode("cp1251")
    assert _texts(HHK_F, rb)[0] == "печать"
    assert HHK_F.build(rb, {}) == rb
    out = HHK_F.build_localized(rb, {"0": "printing"}, "en-US").decode("cp1252")
    assert "charset=windows-1252" in out and '"See Also" value="printing"' in out, out
    # UTF-8 with BOM and non-ASCII UTF-8 stay UTF-8; UTF-16 LE/BE keep their BOM
    for raw in (
        b"\xef\xbb\xbf" + HHC.encode("utf-8"),
        HHC.encode("utf-8"),
        b"\xff\xfe" + HHC.encode("utf-16-le"),
        b"\xfe\xff" + HHC.encode("utf-16-be"),
    ):
        assert _texts(HHC_F, raw) == HHC_UNITS
        assert HHC_F.build(raw, {}) == raw
        out = HHC_F.build_localized(raw, {"0": "日本"}, "ru-RU")
        assert out[:2] == raw[:2]
        assert _texts(HHC_F, out)[0] == "日本"
    # HHP in cp1252, Language stamped on localized build
    hb = HHP.encode("cp1252")
    assert _texts(HHP_F, hb) == ["Product Manual", "Product Help"]
    assert HHP_F.build(hb, {}) == hb
    print("  [enc] ASCII, declared charset, UTF-8 (BOM), UTF-16 LE/BE round trips OK")


def test_hhp():
    hb = HHP.encode("cp1252")
    out = HHP_F.build_localized(
        hb, {"0": "Manuel du produit", "1": 'Aide "produit"'}, "fr-CA"
    ).decode("cp1252")
    exp = (
        HHP.replace("Title=Product Manual", "Title=Manuel du produit")
        .replace("Language=0x409 English (United States)", "Language=0xc0c French (Canada)")
        .replace('main="Product Help"', "main=\"Aide 'produit'\"")
    )
    assert out == exp, out
    assert (
        HHP_F.build_localized(hb, {}, "de")
        .decode("cp1252")
        .count("Language=0x407 German (Germany)")
        == 1
    )
    out = HHP_F.build_localized(hb, {"0": "製品"}, "zh-TW").decode("cp950")
    assert "Language=0x404 Chinese (Traditional, Taiwan)" in out and "Title=製品" in out, out
    assert target_lcid("ja")[0] == 0x411 and target_lcid("xx") is None
    print("  [hhp] Title, window caption, Language LCID stamp OK")


def test_hostile_inputs():
    _refused(HHC_F, b"\x00\x01\x02\x03" * 50, UnsupportedEncodingError, "binary")
    _refused(
        HHC_F,
        b'<OBJECT type="text/sitemap"><param name="Local" value="a.htm"></OBJECT>',
        EmptyDocumentError,
        "no entry names",
    )
    _refused(HHP_F, b"[FILES]\r\na.htm\r\n", EmptyDocumentError, "Title")
    # unterminated comment, stray '<', unclosed tags: lenient, never crash or hang
    for raw in (
        b"<!-- never closed <OBJECT type=text/sitemap><param name=Name value=X>",
        b"< <<< <OBJECT type='text/sitemap'><param name='Name' value='a < b'>",
        b'<OBJECT type=text/sitemap><param name=Name value="unterminated>',
    ):
        with contextlib.suppress(EmptyDocumentError):
            HHC_F.parse(raw)
    assert _texts(
        HHC_F, b"< <<< <OBJECT type='text/sitemap'><param name='Name' value='a < b'>"
    ) == ["a < b"]
    # caps: pre-parse budget, segment cap, one huge value
    old = config.MAX_DOCUMENT_SEGMENTS
    config.MAX_DOCUMENT_SEGMENTS = 5
    try:
        _refused(HHC_F, b"<" * 500, InputLimitError, "MAX_DOCUMENT_SEGMENTS")
        many = b"".join(
            b'<OBJECT type="text/sitemap"><param name="Name" value="Entry %d"></OBJECT>' % i
            for i in range(6)
        )
        _refused(HHC_F, many, InputLimitError, "segments")
    finally:
        config.MAX_DOCUMENT_SEGMENTS = old
    huge = (
        b'<OBJECT type="text/sitemap"><param name="Name" value="'
        + b"a" * (config.MAX_SEGMENT_CHARS + 5)
        + b'"></OBJECT>'
    )
    _refused(HHC_F, huge, InputLimitError, "MAX_SEGMENT_CHARS")
    # a translation cannot inject markup or break out of the attribute
    data = HHC.encode("cp1252")
    out = HHC_F.build(data, {"0": '"><script>alert(1)</script>'})
    assert b"<script>" not in out and b"&quot;&gt;&lt;script&gt;" in out
    # gate (train-9): tags that never close must not backtrack across the file
    # (before the fix 20 KB of "<a x " took about 17 s; now about 0.2 s for 1 MB)
    import time

    entry = b'<OBJECT type="text/sitemap"><param name="Name" value="Hi"></OBJECT>\n'
    for junk in (b"<a x ", b"<a x\n", b"<a b=c", b'<p a="'):
        t0 = time.monotonic()
        assert _texts(HHC_F, entry + junk * 40000) == ["Hi"]
        assert time.monotonic() - t0 < 5, junk
    # unquoted values still end at '<' only where HTML says so
    assert _texts(HHC_F, b"<OBJECT type=text/sitemap><param name=Name value=a/b=c></OBJECT>") == [
        "a/b=c"
    ]
    print("  [hostile] binary, empty, lenient junk, caps, attribute injection, no ReDoS OK")


def test_api_and_cli_round_trip():
    """Upload a .hhc through the API and the CLI, mock-translate to ru-RU, export: a
    Windows-1251 file with only the Name values changed and the Font charset set to 204."""
    import tempfile

    from fastapi.testclient import TestClient
    from sqlmodel import Session

    from app.db import engine, init_db
    from app.main import app
    from app.models import Project, Role, TargetLocale
    from app.services import apikeys, security
    from tests._cli_runner import run_cli

    loc = "ru-RU"
    init_db()
    with Session(engine) as s:
        p = Project(name="hhc", org_id=1, source_locale="en-US", default_provider="mock")
        s.add(p)
        s.commit()
        s.refresh(p)
        s.add(TargetLocale(project_id=p.id, locale=loc))
        s.commit()
        pid = p.id
    user = security.create_user("hhc@x.test", "hhc", Role.ADMIN, "hhc-pw-123456")
    _row, raw_key = apikeys.create_key(user.id, "hhc", scopes="api docs admin")
    client = TestClient(app, raise_server_exceptions=False, follow_redirects=False)
    hdr = {"X-Loc-Token": raw_key}  # pregate: allow-secret throwaway test key
    data = HHC.encode("cp1252")

    # the Local path and one Name hold an e-acute, which cp1251 cannot hold: the clean file
    # has neither, the raw one checks the refusal through the export door.
    raw_hhc = data
    data = data.replace(b"caf\xe9.htm", b"cafe.htm").replace(b"Caf\xe9", b"Cafe")

    def check2(out: bytes, surface: str):
        text = out.decode("cp1251")
        assert f'value="[{loc}] Getting started"' in text, (surface, text)
        assert 'value="Tahoma,8,204"' in text, (surface, text)
        assert 'value="html/cafe.htm#top"' in text, (surface, text)
        assert "\r\n" in text and 'name="Local" value="html\\start.htm"' in text, (surface, text)

    r = client.post(
        "/api/documents",
        headers=hdr,
        data={"project_id": str(pid)},
        files={"file": ("toc.hhc", data, "text/html")},
    )
    assert r.status_code in (200, 201), (r.status_code, r.text)
    doc = r.json()["document_id"]
    code, out, err = run_cli(["doc", "translate", str(doc), "--locale", loc])
    assert code == 0, (code, out, err)
    r = client.get(f"/api/documents/{doc}/download", headers=hdr, params={"locale": loc})
    assert r.status_code == 200, (r.status_code, r.text)
    check2(r.content, "api")
    r = client.post(
        "/api/documents",
        headers=hdr,
        data={"project_id": str(pid)},
        files={"file": ("raw.hhc", raw_hhc, "text/html")},
    )
    assert r.status_code in (200, 201), (r.status_code, r.text)
    bad = r.json()["document_id"]
    code, out, err = run_cli(["doc", "translate", str(bad), "--locale", loc])
    assert code == 0, (code, out, err)
    r = client.get(f"/api/documents/{bad}/download", headers=hdr, params={"locale": loc})
    assert 400 <= r.status_code < 500 and "Windows-1251" in r.text, (r.status_code, r.text)
    r = client.get("/api/formats", headers=hdr)
    assert r.status_code == 200, (r.status_code, r.text)
    assert {"ext": "hhc", "name": "HTML Help table of contents"} in r.json()["formats"], r.text

    tmp = tempfile.mkdtemp(prefix="loctms_hhc_cli_")
    src = os.path.join(tmp, "toc.hhc")
    with open(src, "wb") as fh:
        fh.write(data)
    code, out, err = run_cli(["doc", "upload", str(pid), src])
    assert code == 0, (code, out, err)
    import re

    m = re.search(r"Uploaded #(\d+): toc\.hhc \(hhc,", out)
    assert m, out
    cli_doc = int(m.group(1))
    code, out, err = run_cli(["doc", "translate", str(cli_doc), "--locale", loc])
    assert code == 0, (code, out, err)
    dest = os.path.join(tmp, "out.hhc")
    code, out, err = run_cli(["doc", "export", str(cli_doc), "--locale", loc, "-o", dest])
    assert code == 0, (code, out, err)
    with open(dest, "rb") as fh:
        check2(fh.read(), "cli")
    print("  [surfaces] API upload/download and CLI upload/export round trip (cp1251) OK")


def test_cjk_export_reads_back_796_797():
    from app.filters.htmlhelp_filter import HH_DECODE_KEY, hhp_code_page

    src = (
        b'<HTML><HEAD><meta http-equiv="Content-Type" content="text/html; charset=windows-1252">'
        b'</HEAD><BODY><OBJECT type="text/sitemap"><param name="Name" value="Start"></OBJECT>'
        b"</BODY></HTML>"
    )
    # #796: characters outside the strict gb2312 / shift_jis / euc_kr / big5 subsets
    for loc, text in (("ko-KR", "똠"), ("zh-CN", "喆"), ("ja-JP", "①設定"), ("zh-TW", "€元")):
        out = HHC_F.build_localized(src, {"0": text}, loc)
        parsed = HHC_F.parse(out)
        assert [u.text for u in parsed.units] == [text], (loc, [u.text for u in parsed.units])
        assert parsed.skeleton.get(HH_DECODE_KEY) == 2, parsed.skeleton
        # a document stored before the fix (no stamp) is read exactly as it always was
        try:
            old = HHC_F.parse(out, hh_decode=1)
            assert [u.text for u in old.units] != [text] and HH_DECODE_KEY not in old.skeleton, loc
        except (EmptyDocumentError, UnsupportedEncodingError):
            pass  # the old decode read the bytes as Windows-1252: no letters, or refused
        # an unchanged re-export of the new reading keeps the text
        again = HHC_F.build_localized(out, {"0": text}, loc, hh_decode=2)
        assert [u.text for u in HHC_F.parse(again).units] == [text], loc
    # #797: a localized project file reads back in the code page of its own Language=
    for loc, title, cp in (("ko-KR", "똠 도움말", 949), ("zh-CN", "喆 帮助", 936)):
        out = HHP_F.build_localized(HHP.encode("cp1252"), {"0": title}, loc)
        assert hhp_code_page(out) == cp, (loc, hhp_code_page(out))
        parsed = HHP_F.parse(out)
        assert parsed.units[0].text == title, (loc, parsed.units[0].text)
        assert parsed.skeleton.get(HH_DECODE_KEY) == 2
        if loc == "zh-CN":
            old = HHP_F.parse(out, hh_decode=1)
            assert old.units[0].text != title and HH_DECODE_KEY not in old.skeleton
        assert HHP_F.build_localized(out, {"0": title}, loc) == out
    # a project with a byte 1252 lacks is no longer refused for its code page hint
    assert hhp_code_page(b"[OPTIONS]\r\nLanguage=0x412 Korean\r\nTitle=\x81\x41\r\n") == 949
    # the stored stamp reaches the export (no stamp: version 1)
    from types import SimpleNamespace

    from app.services.export import _hh_decode

    assert _hh_decode(SimpleNamespace(skeleton={HH_DECODE_KEY: 2})) == 2
    assert _hh_decode(SimpleNamespace(skeleton={})) == 1
    print("  [hhp/hhc] 796/797: localized CJK export reads back, old decode kept for unstamped OK")


def test_gate_versioned_decode_service_paths():
    """#796/#797 gate: through ingest, repair and export. A document stored before the fix
    (no hh_decode stamp) exports and repairs exactly as before, a new upload is version 2,
    and a re-upload of a version 1 export reads back."""
    from sqlmodel import Session, select

    from app.db import engine, init_db
    from app.filters.htmlhelp_filter import HH_DECODE_KEY
    from app.models import Document, Project, Segment, TargetLocale, TransMode
    from app.ports import blobstore
    from app.services import ingest
    from app.services.export import build_translated_file

    def sitemap(charset: str, name: bytes, local: bytes = b"a.htm") -> bytes:
        meta = (
            b'<meta http-equiv="Content-Type" content="text/html; charset='
            + charset.encode()
            + b'">'
            if charset
            else b""
        )
        return (
            b"<HTML><HEAD>" + meta + b'</HEAD><BODY><OBJECT type="text/sitemap">'
            b'<param name="Name" value="' + name + b'"><param name="Local" value="' + local + b'">'
            b"</OBJECT></BODY></HTML>"
        )

    init_db()
    loc = "ja-JP"
    with Session(engine) as s:
        p = Project(name="hh-gate", org_id=1, source_locale="en-US", default_provider="mock")
        s.add(p)
        s.commit()
        s.refresh(p)
        s.add(TargetLocale(project_id=p.id, locale=loc))
        s.commit()
        pid = p.id

    def upload(name: str, data: bytes) -> int:
        # Python filter path (documents imported before Bilingua, or no wheel)
        with Session(engine) as s, bilingua_switch_off():
            doc = ingest.ingest_document(s, s.get(Project, pid), name, data, TransMode.SEGMENTED)
            return doc.id

    def units_of(doc_id: int) -> list:
        with Session(engine) as s:
            segs = s.exec(
                select(Segment)
                .where(Segment.document_id == doc_id, Segment.target_locale == loc)
                .order_by(Segment.order_index)
            ).all()
            return [g.source_text for g in segs]

    def unstamp(doc_id: int) -> None:
        with Session(engine) as s:
            d = s.get(Document, doc_id)
            sk = dict(d.skeleton or {})
            sk.pop(HH_DECODE_KEY, None)
            d.skeleton = sk
            s.add(d)
            s.commit()

    def export(doc_id: int, target: str) -> bytes:
        with Session(engine) as s:
            for g in s.exec(
                select(Segment).where(Segment.document_id == doc_id, Segment.target_locale == loc)
            ).all():
                g.target_text = target
                g.status = "translated"
                s.add(g)
            s.commit()
            out = build_translated_file(s, s.get(Document, doc_id), loc)
            return out

    # 1. a new upload is version 2: a declared shift_jis label reads as Windows 932
    # (an NEC-row character; the strict codec of the old decode falls back to Windows-1252)
    nec = sitemap("shift_jis", "① Start".encode("cp932"), "①.htm".encode("cp932"))
    new_id = upload("nec.hhc", nec)
    assert units_of(new_id) == ["① Start"], units_of(new_id)
    with Session(engine) as s:
        assert s.get(Document, new_id).skeleton.get(HH_DECODE_KEY) == 2

    # 2. the same document stored before the fix (no stamp) exports with the old decode
    blob = blobstore.get().get(blobstore.document_key(Session(engine).get(Document, new_id)))
    want_v1 = HHC_F.build_localized(blob, {"0": "Start"}, loc, hh_decode=1)
    want_v2 = HHC_F.build_localized(blob, {"0": "Start"}, loc, hh_decode=2)
    assert want_v1 != want_v2
    assert export(new_id, "Start") == want_v2
    unstamp(new_id)
    assert export(new_id, "Start") == want_v1

    # 3. repair of an unstamped document: the old reading and the old .hhp hint
    # (a later Japanese .hhp holding a byte Windows-1252 lacks gave no hint before the fix)
    kata = sitemap("", "テスト".encode("cp932"))  # valid Windows-1252 bytes too
    old_id = upload("kata.hhc", kata)
    assert units_of(old_id) == ["ƒeƒXƒg"], units_of(old_id)
    hhp = b"[OPTIONS]\r\nLanguage=0x411 Japanese\r\nTitle=" + "　ヘルプ".encode("cp932") + b"\r\n"
    upload("project.hhp", hhp)
    unstamp(old_id)
    assert ingest.reingest_document(old_id)
    assert units_of(old_id) == ["ƒeƒXƒg"], units_of(old_id)
    with Session(engine) as s:
        assert HH_DECODE_KEY not in (s.get(Document, old_id).skeleton or {})
    with Session(engine) as s:  # a repair only runs on an untranslated document
        for g in s.exec(select(Segment).where(Segment.document_id == new_id)).all():
            g.target_text = ""
            s.add(g)
        s.commit()
    assert ingest.reingest_document(new_id)
    with Session(engine) as s:
        assert HH_DECODE_KEY not in (s.get(Document, new_id).skeleton or {})
    assert units_of(new_id) != ["① Start"], units_of(new_id)  # the pre-fix reading
    # a version 2 repair takes the project's .hhp hint the fix now reads
    v2_id = upload("kata2.hhc", kata)
    assert units_of(v2_id) == ["テスト"], units_of(v2_id)  # the .hhp is there now
    assert ingest.reingest_document(v2_id)
    assert units_of(v2_id) == ["テスト"], units_of(v2_id)

    # 4. a version 1 export re-uploaded reads back as text (version 2)
    src = sitemap("windows-1252", b"Start")
    v1_id = upload("plain.hhc", src)
    unstamp(v1_id)
    out = export(v1_id, "①設定")
    assert b"shift_jis" in out.lower(), out
    back = upload("reup.hhc", out)
    assert units_of(back) == ["①設定"], units_of(back)
    print("  [gate] 796/797: unstamped export+repair unchanged, new upload v2, v1 export re-reads")


def main():
    test_cjk_export_reads_back_796_797()
    test_registration()
    test_hhc_parse_and_identity()
    test_hhc_translated_round_trip()
    test_hhk_index_and_see_also()
    test_code_pages_and_refusal()
    test_vietnamese_cp1258_681()
    test_typographic_fallback_715()
    test_encodings_and_bom()
    test_hhp()
    test_hostile_inputs()
    test_api_and_cli_round_trip()
    test_gate_versioned_decode_service_paths()
    print("HTMLHELP FILTER OK")


if __name__ == "__main__":
    main()
