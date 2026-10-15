"""QuarkXPress Tags (XPress Tags tagged text, .xtg / .tag) document filter.

  identity    an untouched file (header, style sheet definitions, CRLF/CR/LF, BOM,
              UTF-16, legacy encodings) rebuilds byte for byte
  units       one unit per paragraph line; @Name: is the style and carries over to
              later paragraphs; @$: is Normal, @: No Style; definitions are not text
  tokens      attribute tags, character style sheets, special characters, index
              markers, hyperlinks and rubi are x point tokens (consecutive tags share
              one); leading and trailing tags stay outside the unit; <\\@> <\\<> <\\\\>
              <\\>> are literal text
  build       translated text is re-escaped, dropped tokens are appended, a newline
              becomes <\\n>; the file is written back in its <e> encoding, and switches
              to UTF-8 + <e9> when the translation does not fit
  hostile     unknown / Mac CJK encodings, undecodable bytes, NUL, unclosed tags,
              deep parentheses, the tag budget, segment and unit caps are refused
  surfaces    API upload -> translate (mock) -> download, upload accept list, connector

Fixtures are hand-written here. Hermetic + offline. Final line: "QUARK TAGS FILTER OK".
"""

import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(_ROOT)
sys.path.insert(0, _ROOT)
from tests._bootstrap import init_env

init_env("loctms_xtg_")

from app.filters import get_filter
from app.filters.base import InputLimitError, UnsupportedEncodingError

LOC = "de-DE"

XTG = (
    "<v19.00><e9>\r\n"
    '@Normal=[S"",""]<*L*h"Standard"f"Helvetica"z12>\r\n'
    '@Heading=[S"","Body"]<*C><B>\r\n'
    '@Font set 1=[F]<f"Arial">\r\n'
    '~Bookmark=<BookmarkRef"Intro">\r\n'
    "@Heading:<*C>Getting started\r\n"
    "@Body:Hello <B>bold<P> world<\\n>second line\r\n"
    "Mail us <\\@> once, 3 <\\<> 4 <\\>> 2, path C:<\\\\>x<\\i>.\r\n"
    '<f"Arial"><z10>Leading tags stay outside<f$><z$>\r\n'
    '@$:<XI,"Entry","",,,,,"">Indexed <A(3,"HYP\\"B",\\#002\\#000)[2]>'
    'link<A(3,"HYPB",)[0]> text.\r\n'
    "\r\n"
    "<\\#13><*R>\r\n"
    "@:Rubi <A(\\#68Base\\#36<@><z6>\\#9Rubi\\#132,50,C,A,0,2,B,T)> end.\r\n"
    '<*t(36,0,"1 ")>Name<\\t>Value <c"Red">\u00fcber\r\n'
)
XTG_B = XTG.encode("utf-8")

EXPECTED = [
    ("Getting started", "Heading"),
    ("Hello ⟦g1⟧bold⟦/g1⟧ world⟦x2⟧second line", "Body"),
    ("Mail us @ once, 3 < 4 > 2, path C:\\x⟦x1⟧.", "Body"),
    ("Leading tags stay outside", "Body"),
    ("Indexed ⟦x1⟧link⟦x2⟧ text.", "Normal"),
    ("Rubi ⟦x1⟧ end.", ""),
    ("Name⟦x1⟧Value ⟦x2⟧über", ""),
]


def _f():
    return get_filter("xtg")


def test_registered():
    assert get_filter("xtg") is get_filter("tag")
    assert get_filter("xtg").fmt == "xtg"
    assert get_filter("txt").fmt != "xtg", ".txt stays plain text"
    from app.filters import supported_exts

    assert "xtg" in supported_exts() and "tag" in supported_exts()
    print("  [registry] .xtg and .tag registered, .txt untouched OK")


def test_units_and_identity():
    r = _f().parse(XTG_B)
    got = [(u.text, u.style) for u in r.units]
    assert got == EXPECTED, got
    assert r.skeleton["format"] == "xtg" and r.skeleton["xtg_e"] == 9
    assert "source_encoding" not in r.skeleton
    u1 = r.units[1].inline_map
    assert u1 == {"g1": "<B>", "/g1": "<P>", "x2": "<\\n>"}, u1
    u4 = r.units[4].inline_map
    assert u4["x1"] == '<A(3,"HYP\\"B",\\#002\\#000)[2]>', u4
    assert r.units[5].inline_map["x1"].startswith("<A(\\#68Base")
    assert r.units[6].inline_map == {"x1": "<\\t>", "x2": '<c"Red">'}
    assert _f().build(XTG_B, {}) == XTG_B
    same = {str(i): t for i, (t, _s) in enumerate(EXPECTED)}
    assert _f().build(XTG_B, same) == XTG_B
    # LF and CR (classic Mac) line ends, and a UTF-8 BOM, are kept
    for nl in ("\n", "\r"):
        b = XTG.replace("\r\n", nl).encode("utf-8")
        assert [(u.text, u.style) for u in _f().parse(b).units] == EXPECTED
        assert _f().build(b, {}) == b
    bom = b"\xef\xbb\xbf" + XTG_B
    assert len(_f().parse(bom).units) == len(EXPECTED)
    out = _f().build(bom, {"0": "Erste Schritte"})
    assert out.startswith(b"\xef\xbb\xbf<v19.00><e9>") and b"@Heading:<*C>Erste Schritte\r\n" in out
    # a header on the same line as the first paragraph
    b = b"<v15.00><e9>@Normal:One line file<\\n>\n"
    r = _f().parse(b)
    assert [(u.text, u.style) for u in r.units] == [("One line file", "Normal")]
    # a paragraph with no letters or digits is not a unit
    assert _f().parse(b"<v1><e9>\n@Normal:Text\n<B>--- ...<P>\n").units[0].text == "Text"
    assert len(_f().parse(b"<v1><e9>\n@Normal:Text\n<B>--- ...<P>\n").units) == 1
    print("  [units] paragraphs, styles, tokens; byte identity (CRLF, LF, CR, BOM) OK")


def test_translated_round_trip():
    tr = {
        "0": "Erste Schritte",
        "1": "Hallo ⟦g1⟧fett⟦/g1⟧ Welt⟦x2⟧zweite Zeile",
        "2": "Mail an uns @ einmal, 3 < 4, Pfad C:\\y⟦x1⟧.",
        "4": "Indiziert ⟦x2⟧ohne Link⟦x9⟧ ⟦x2⟧Text.",
        "6": "Name\nWert",
    }
    out = _f().build(XTG_B, tr).decode("utf-8")
    assert "@Heading:<*C>Erste Schritte\r\n" in out
    assert "@Body:Hello" not in out and "@Body:Hallo <B>fett<P> Welt<\\n>zweite Zeile\r\n" in out
    assert "Mail an uns <\\@> einmal, 3 <\\<> 4, Pfad C:<\\\\>y<\\i>.\r\n" in out
    # untranslated paragraph and every definition stay byte for byte
    assert '<f"Arial"><z10>Leading tags stay outside<f$><z$>\r\n' in out
    assert out.startswith('<v19.00><e9>\r\n@Normal=[S"",""]<*L*h"Standard"f"Helvetica"z12>\r\n')
    # unknown (x9) and duplicate (second x2) tokens dropped; missing x1 appended
    assert (
        '@$:<XI,"Entry","",,,,,"">Indiziert <A(3,"HYPB",)[0]>ohne Link Text.'
        '<A(3,"HYP\\"B",\\#002\\#000)[2]>\r\n' in out
    ), out
    # a newline in a translation is the New line tag; dropped tokens at the end
    assert '<*t(36,0,"1 ")>Name<\\n>Wert<\\t><c"Red">\r\n' in out, out
    back = _f().parse(out.encode("utf-8"))
    assert back.units[0].text == "Erste Schritte"
    assert back.units[2].text == "Mail an uns @ einmal, 3 < 4, Pfad C:\\y⟦x1⟧."
    print("  [build] translated paragraphs re-escaped, tokens kept, rest byte-identical OK")


def test_encodings():
    # Mac Roman (<e0>), classic Mac CR line ends
    mac = "<v15.00><e0>\r@Normal:Caf\u00e9 cr\u00e8me \u2013 na\u00efve\r".encode("mac_roman")
    assert b"\x8e" in mac  # é in Mac Roman
    r = _f().parse(mac)
    assert r.units[0].text == "Café crème – naïve" and r.skeleton["source_encoding"] == "mac_roman"
    assert _f().build(mac, {}) == mac
    out = _f().build(mac, {"0": "Café crème – naïf"})
    assert out == "<v15.00><e0>\r@Normal:Café crème – naïf\r".encode("mac_roman")
    # a translation Mac Roman cannot hold: the whole file becomes UTF-8 with <e9>
    out = _f().build(mac, {"0": "カフェ"})
    assert out == "<v15.00><e9>\r@Normal:カフェ\r".encode(), out
    # Windows Latin (<e1>): the euro sign is 0x80
    w = "<v8.00><e1>\r\n@Normal:Price 5 \u20ac\r\n".encode("cp1252")
    assert _f().parse(w).units[0].text == "Price 5 €"
    assert _f().build(w, {"0": "Preis 5 €"}) == "<v8.00><e1>\r\n@Normal:Preis 5 €\r\n".encode(
        "cp1252"
    )
    # ISO-8859-1 (<e2>), Japanese Windows (<e3>), Korean (<e19>), Big5 (<e6>), GB2312 (<e7>)
    for num, codec, s in (
        (2, "latin-1", "Grüße"),
        (3, "cp932", "日本語のテキスト"),
        (19, "cp949", "한국어 텍스트"),
        (6, "big5", "中文文字"),
        (7, "gbk", "中文文本"),
    ):
        b = f"<v19.00><e{num}>\n@Normal:{s}\n".encode(codec)
        r = _f().parse(b)
        assert r.units[0].text == s and r.skeleton["source_encoding"] == codec, (num, r.units)
        assert _f().build(b, {}) == b
        assert _f().build(b, {"0": s + "!"}) == f"<v19.00><e{num}>\n@Normal:{s}!\n".encode(codec)
    # UTF-16 LE with BOM (<e8>, what QuarkXPress for Windows writes), and BE, and no BOM
    for bom, codec in ((b"\xff\xfe", "utf-16-le"), (b"\xfe\xff", "utf-16-be"), (b"", "utf-16-le")):
        b = bom + "<v19.00><e8>\r\n@Normal:Hello <B>world<P>\r\n".encode(codec)
        r = _f().parse(b)
        assert r.units[0].text == "Hello ⟦g1⟧world⟦/g1⟧" and r.skeleton["xtg_e"] == 8
        assert _f().build(b, {}) == b
        out = _f().build(b, {"0": "Hallo ⟦g1⟧Welt⟦/g1⟧"})
        assert out == bom + "<v19.00><e8>\r\n@Normal:Hallo <B>Welt<P>\r\n".encode(codec), out
    # <e8> declared on 8-bit bytes: the bytes win (read as UTF-8)
    assert _f().parse("<v1><e8>\n@N:Grüße\n".encode()).units[0].text == "Grüße"
    # no <e> tag: the shared #509 decode (UTF-8, else Windows-1252), written back the same way
    nohead = "@Normal:Na\u00efve text\n".encode("cp1252")
    r = _f().parse(nohead)
    assert r.units[0].text == "Naïve text" and r.skeleton["source_encoding"] == "cp1252"
    assert _f().build(nohead, {"0": "Naïf"}) == "@Normal:Naïf\n".encode("cp1252")
    out = _f().build(nohead, {"0": "ナイーブ"})
    assert out == "\ufeff@Normal:ナイーブ\n".encode("utf-8"), out
    print("  [encodings] Mac Roman, 1252, Latin-1, CJK, UTF-16 LE/BE, UTF-8, no header OK")


def _refused(data, exc_type, needle):
    try:
        _f().parse(data)
    except exc_type as exc:
        assert needle in str(exc), (needle, str(exc))
        return
    raise AssertionError(f"not refused: {data[:60]!r}")


def test_hostile_inputs():
    from app import config

    _refused(b"<v1><e21>\n@N:x\n", UnsupportedEncodingError, "Japanese Mac")
    _refused(b"<v1><e20>\n@N:x\n", UnsupportedEncodingError, "Korean Mac")
    _refused(b"<v1><e5>\n@N:x\n", UnsupportedEncodingError, "unknown encoding <e5>")
    _refused(b"<v1><e1>\n@N:x\x81y\n", UnsupportedEncodingError, "cp1252")
    _refused(b"<v1><e9>\n@N:caf\xe9\n", UnsupportedEncodingError, "utf-8")
    _refused(b"\xff\xfe<\x00v\x00>", UnsupportedEncodingError, "odd number of bytes")
    _refused(b"<v1><e9>\n@N:a\x00b\n", UnsupportedEncodingError, "NUL")
    _refused(b"<v1><e9>\n@N:open <B tag\nnext>\n", ValueError, "line 2")
    _refused(b"<v1><e9>\n@N:x" + b"<A" + b"(" * 80 + b">\n", InputLimitError, "parentheses")
    # a source paragraph that already holds the token marker characters is skipped
    r = _f().parse("<v1><e9>\n@N:keep ⟦x1⟧ out\n@N:Fine\n".encode())
    assert [u.text for u in r.units] == ["Fine"] and r.skeleton["skipped_marker_paragraphs"] == 1
    old = (config.MAX_DOCUMENT_SEGMENTS, config.MAX_SEGMENT_CHARS)
    try:
        config.MAX_DOCUMENT_SEGMENTS = 3
        _refused(b"<v1><e9>\n" + b"@N:a\n" * 4, InputLimitError, "MAX_DOCUMENT_SEGMENTS")
        _refused(b"<v1><e9>\n@N:a" + b"<B>" * 700 + b"\n", InputLimitError, "tags")
        config.MAX_DOCUMENT_SEGMENTS, config.MAX_SEGMENT_CHARS = old[0], 50
        _refused(b"<v1><e9>\n@N:" + b"a" * 60 + b"\n", InputLimitError, "MAX_SEGMENT_CHARS")
    finally:
        config.MAX_DOCUMENT_SEGMENTS, config.MAX_SEGMENT_CHARS = old
    print("  [hostile] encodings, NUL, unclosed tag, nesting, budgets, caps refused OK")


def test_api_surface():
    from fastapi.testclient import TestClient
    from sqlmodel import Session

    from app.db import engine, init_db
    from app.main import app
    from app.models import Project, Role, TargetLocale
    from app.services import apikeys, security
    from tests._cli_runner import run_cli

    init_db()
    with Session(engine) as s:
        p = Project(name="xtg", org_id=1, source_locale="en-US", default_provider="mock")
        s.add(p)
        s.commit()
        s.refresh(p)
        s.add(TargetLocale(project_id=p.id, locale=LOC))
        s.commit()
        pid = p.id
    user = security.create_user("xtg@x.test", "xtg", Role.ADMIN, "xtg-pw-123456")
    _row, raw = apikeys.create_key(user.id, "xtg", scopes="api docs admin")
    client = TestClient(app, raise_server_exceptions=False, follow_redirects=False)
    hdr = {"X-Loc-Token": raw}  # pregate: allow-secret throwaway test key
    mac = "<v15.00><e0>\r@Normal:Getting started\r@Body:Hello <B>world<P>\r".encode("mac_roman")
    r = client.post(
        "/api/documents",
        headers=hdr,
        data={"project_id": str(pid)},
        files={"file": ("chapter.tag", mac, "text/plain")},
    )
    assert r.status_code in (200, 201), (r.status_code, r.text)
    doc_id = r.json()["document_id"]
    code, out, err = run_cli(["doc", "translate", str(doc_id), "--locale", LOC])
    assert code == 0, (code, out, err)
    r = client.get(f"/api/documents/{doc_id}/download", headers=hdr, params={"locale": LOC})
    assert r.status_code == 200, (r.status_code, r.text)
    assert r.headers["content-type"].startswith("text/plain"), r.headers
    assert r.content.startswith(b"<v15.00><e"), r.content[:40]
    back = _f().parse(r.content)
    assert len(back.units) == 2
    assert back.units[0].text != "Getting started" and "Getting started" in back.units[0].text
    assert "⟦g1⟧" in back.units[1].text and "⟦/g1⟧" in back.units[1].text, back.units[1].text
    assert b"<B>" in r.content and b"<P>" in r.content
    r = client.post(
        "/api/documents",
        headers=hdr,
        data={"project_id": str(pid)},
        files={"file": ("mac.xtg", b"<v1><e21>\r@N:x\r", "text/plain")},
    )
    assert 400 <= r.status_code < 500 and "Japanese Mac" in r.text, (r.status_code, r.text)
    print("  [api] upload .tag, translate (mock), download in source encoding; refusal OK")


def test_upload_accept_lists():
    with open(os.path.join(_ROOT, "app", "templates", "project.html"), encoding="utf-8") as fh:
        html = fh.read()
    assert ",.xtg,.tag" in html and "· QuarkXPress Tags" in html
    from app.services.connectors.msgraph import FORMATS
    from app.services.mime import mime_for_fmt
    from app.web.api_part2 import _FMT_LABELS

    assert FORMATS.get("xtg") == "xtg" and FORMATS.get("tag") == "xtg"
    assert mime_for_fmt("xtg").startswith("text/plain") and "charset" not in mime_for_fmt("tag")
    assert _FMT_LABELS["xtg"] == _FMT_LABELS["tag"] == "QuarkXPress Tags"
    print("  [surfaces] upload accept list, connector map, MIME and label OK")


def test_translation_cannot_become_a_definition():
    """Gate train-11: a translation starting `~Name=<` must not turn its paragraph into
    a bookmark definition (the paragraph vanished on re-import before the fix); `@`
    and `<` in a translation are always escaped."""
    src = b'<v19.00><e9>\n@Body=[S"",""]<*L>\n@Body:First line\nSecond line\n'
    f = _f()
    assert len(f.parse(src).units) == 2
    for t in ("~X=<y", "~Mark=<B>", "@Head:evil", "a<B>b"):
        out = f.build(src, {"1": t})
        units = f.parse(out).units
        assert len(units) == 2, (t, out, [u.text for u in units])
    out = f.build(src, {"1": "~X=<y"})
    assert out.endswith(b"<\\#126>X=<\\<>y\n"), out
    out = f.build(src, {"1": "@Head:evil"})
    assert out.endswith(b"<\\@>Head:evil\n"), out
    print("  [build] translations never become style or bookmark definitions OK")


def test_attribute_pairs_travel_with_words_680():
    """#680: an attribute switched on and reset in the same paragraph is a paired token,
    so a reordered translation moves the formatting with its words; build re-emits it
    balanced, and a pair a translation breaks loses its formatting, never its words."""
    f = _f()
    # the soak repro: the reset after the last text is part of the unit
    src = b"<v13.00><e9>\n@Heading:Account <B>settings<B$>\n"
    r = f.parse(src)
    assert r.units[0].text == "Account ⟦g1⟧settings⟦/g1⟧", r.units[0].text
    assert r.units[0].inline_map == {"g1": "<B>", "/g1": "<B$>"}, r.units[0].inline_map
    assert f.build(src, {"0": r.units[0].text}) == src
    out = f.build(src, {"0": "⟦g1⟧Paramètres⟦/g1⟧ du compte"})
    assert out == "<v13.00><e9>\n@Heading:<B>Paramètres<B$> du compte\n".encode(), out
    assert f.parse(out).units[0].text == "⟦g1⟧Paramètres⟦/g1⟧ du compte"
    # MT moved every word out of the pair: no attribute switched on around nothing
    out = f.build(src, {"0": "Paramètres du compte ⟦g1⟧⟦/g1⟧"})
    assert b"<B>" not in out and b"<B$>" not in out, out
    # a pair opened before the first text and reset inside it pulls its opener in;
    # paragraph attributes before it stay outside
    src = b"<v1><e9>\n@B:<*C><B>Account<B$> settings\n"
    assert f.parse(src).units[0].text == "⟦g1⟧Account⟦/g1⟧ settings"
    out = f.build(src, {"0": "Paramètres du ⟦g1⟧compte⟦/g1⟧"})
    assert out.endswith("@B:<*C>Paramètres du <B>compte<B$>\n".encode()), out
    # a pair around the whole text stays in the skeleton
    src = b'<v1><e9>\n@B:<f"Arial"><z10>Whole<f$><z$>\n'
    assert f.parse(src).units[0].text == "Whole"
    assert f.build(src, {"0": "Ganz"}).endswith(b'<f"Arial"><z10>Ganz<f$><z$>\n')
    # nested pairs; closes that stay together keep the source's own reset tags, closes
    # MT separates each reset exactly their own attribute
    src = b"<v1><e9>\n@B:x <B>a <I>b<I$><B$> c\n"
    assert f.parse(src).units[0].text == "x ⟦g1⟧a ⟦g2⟧b⟦/g2⟧⟦/g1⟧ c"
    assert f.build(src, {"0": "X ⟦g1⟧A ⟦g2⟧B⟦/g2⟧⟦/g1⟧ C"}).endswith(b"@B:X <B>A <I>B<I$><B$> C\n")
    out = f.build(src, {"0": "X ⟦g2⟧B⟦/g2⟧ ⟦g1⟧A⟦/g1⟧ C"})
    assert out.endswith(b"@B:X <I>B<I$> <B>A<B$> C\n"), out
    src = b"<v1><e9>\n@B:<B>a <I>b<$>\n"
    assert f.parse(src).units[0].text == "⟦g1⟧a ⟦g2⟧b⟦/g2⟧⟦/g1⟧"
    # (the outermost close keeps the source's reset-all <$>)
    assert f.build(src, {"0": "⟦g2⟧B⟦/g2⟧ ⟦g1⟧A⟦/g1⟧"}).endswith(b"@B:<I>B<I$> <B>A<$>\n")
    # every attribute kind: style sheets, <P>, toggles switched back, fonts, colours
    src = b'<v1><e9>\n@B:x <@Emph>a<@$> <BI>c<P> d <B>e<B> <f"Arial">g<f$> <c"Red">h<c$>\n'
    want = "x ⟦g1⟧a⟦/g1⟧ ⟦g2⟧c⟦/g2⟧ d ⟦g3⟧e⟦/g3⟧ ⟦g4⟧g⟦/g4⟧ ⟦g5⟧h⟦/g5⟧"
    assert f.parse(src).units[0].text == want, f.parse(src).units[0].text
    out = f.build(src, {"0": "⟦g5⟧H⟦/g5⟧ ⟦g4⟧G⟦/g4⟧ ⟦g3⟧E⟦/g3⟧ D ⟦g2⟧C⟦/g2⟧ ⟦g1⟧A⟦/g1⟧ X"})
    assert out.endswith(b'@B:<c"Red">H<c$> <f"Arial">G<f$> <B>E<B> D <BI>C<P> <@Emph>A<@$> X\n'), (
        out
    )
    # point tokens inside a pair stay points; an attribute never reset stays a point
    src = b'<v1><e9>\n@B:<c"Red">Red<c$> and <B>bold<\\n>more<B$> <z12>big\n'
    assert f.parse(src).units[0].text == "⟦g1⟧Red⟦/g1⟧ and ⟦g2⟧bold⟦x3⟧more⟦/g2⟧ ⟦x4⟧big"
    # what does not nest cleanly keeps the pre-#680 point tokens
    assert f.parse(b"<v1><e9>\n@B:x <z12>a<z14>b<z$> d\n").units[0].text == ("x ⟦x1⟧a⟦x2⟧b⟦x3⟧ d")
    # gate: a pair the translation does not write whole loses its formatting and keeps
    # its words (never refused, never left switched on); crossed pairs each reset their
    # own attribute; a repeat or a pair id written as a point is dropped
    src = b"<v1><e9>\n@B:x <B>a <I>b<I$><B$> c\n"
    for got, want in (
        ("X A B C", b"@B:X A B C\n"),
        ("X ⟦g1⟧A ⟦g2⟧B⟦/g1⟧⟦/g2⟧ C", b"@B:X <B>A <I>B<B$><I$> C\n"),
        ("X ⟦g1⟧A ⟦g2⟧B⟦/g2⟧ C", b"@B:X A <I>B<I$> C\n"),
        ("X ⟦g1⟧A ⟦g2⟧B⟦/g2⟧⟦/g1⟧ C ⟦g2⟧", b"@B:X <B>A <I>B<I$><B$> C \n"),
        ("X ⟦/g1⟧A ⟦g2⟧B⟦/g2⟧⟦g1⟧ C", b"@B:X A <I>B<I$> C\n"),
        ("X ⟦x1⟧A ⟦g2⟧B⟦/g2⟧⟦/g1⟧ C", b"@B:X A <I>B<I$> C\n"),
        ("X ⟦g1⟧A ⟦g2⟧B⟦/g2⟧ C⟦/g1⟧ ⟦/g1⟧", b"@B:X <B>A <I>B<I$> C<B$> \n"),
    ):
        out = f.build(src, {"0": got})
        assert out.endswith(want), (got, out)
        f.parse(out)  # re-lexes
    # a source <$> that ends several pairs stays a reset-all when MT separates them, so
    # an attribute set by a point token inside a span (<z14>) does not run on
    src = b"<v1><e9>\n@B:x <B>a <I>b<z12>c<z14>d<$> e\n"
    assert f.parse(src).units[0].text == "x ⟦g1⟧a ⟦g2⟧b⟦x3⟧c⟦x4⟧d⟦/g2⟧⟦/g1⟧ e"
    out = f.build(src, {"0": "⟦g2⟧B⟦x3⟧C⟦x4⟧D⟦/g2⟧ X ⟦g1⟧A⟦/g1⟧ E"})
    assert out.endswith(b"@B:<I>B<z12>C<z14>D<I$> X <B>A<$> E\n"), out
    print("  [pairs] #680 attribute pairs travel with their words, balanced, tolerant OK")


def main():
    test_registered()
    test_units_and_identity()
    test_translated_round_trip()
    test_attribute_pairs_travel_with_words_680()
    test_translation_cannot_become_a_definition()
    test_encodings()
    test_hostile_inputs()
    test_upload_accept_lists()
    test_api_surface()
    print("QUARK TAGS FILTER OK")


if __name__ == "__main__":
    main()
