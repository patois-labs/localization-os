"""TTML / DFXP subtitle filter (Batch 10): app/filters/ttml_filter.py.

  identity   parse + build with no changes is byte-identical (BOM, CRLF, comments, head).
  translate  only <p> content changes; begin/end/dur, styles and <head> are untouched.
  inline     <br/> is a point token, <span> a paired token, <set>/<metadata> kept whole.
  tolerant   unknown / repeated / stray tokens are dropped, an unclosed span is closed.
  lang       build_localized switches xml:lang on <tt> and on source-language elements.
  rtl        a right-to-left target gets tts:direction="rtl" on rebuilt captions (#741).
  hostile    DOCTYPE / entities / non-TTML root / markup budget / segment cap refused.

Hermetic + offline. Final line: "TTML FILTER OK".
"""

import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(_ROOT)
sys.path.insert(0, _ROOT)
from tests._bootstrap import init_env

init_env("loctms_ttml_")

from lxml import etree

from app import config
from app.filters import get_filter, supported_exts
from app.filters.base import InputLimitError

TT = (
    '<?xml version="1.0" encoding="UTF-8"?>\n'
    "<!-- produced by hand -->\n"
    '<tt xmlns="http://www.w3.org/ns/ttml" xmlns:tts="http://www.w3.org/ns/ttml#styling"\n'
    '    xmlns:ttm="http://www.w3.org/ns/ttml#metadata" xml:lang="en">\n'
    "  <head>\n"
    '    <styling><style xml:id="s1" tts:color="white"/></styling>\n'
    "    <metadata><ttm:title>Demo &amp; test</ttm:title></metadata>\n"
    "  </head>\n"
    '  <body><div xml:lang="en">\n'
    '    <p begin="00:00:01.000" end="00:00:04.000" style="s1">Hello &amp; welcome<br/>'
    'to the <span tts:fontStyle="italic">show</span>.</p>\n'
    '    <p begin="5s" end="7s">\n'
    "      Second line\n"
    "      continues here\n"
    "    </p>\n"
    '    <p begin="8s" end="9s"></p>\n'
    '    <p begin="10s" end="12s">Press {0} for <span xml:lang="en" tts:fontWeight="bold">'
    '%1$s</span><set begin="1s" tts:color="red"/> now</p>\n'
    "  </div></body>\n"
    "</tt>\n"
)


def _f():
    return get_filter("ttml")


def test_registered():
    assert "ttml" in supported_exts() and "dfxp" in supported_exts()
    assert get_filter("dfxp") is _f()
    print("  [reg] .ttml and .dfxp registered OK")


def test_identity_round_trip():
    f = _f()
    for data in (
        TT.encode(),
        b"\xef\xbb\xbf" + TT.encode(),
        TT.replace("\n", "\r\n").encode(),
    ):
        pr = f.parse(data)
        assert f.build(data, {}) == data
        same = {str(u.p_index): u.text for u in pr.units}
        assert f.build(data, same) == data, "re-injecting the source must be byte-identical"
    print("  [identity] untouched and same-text builds byte-identical (BOM, CRLF) OK")


def test_units_and_tokens():
    pr = _f().parse(TT.encode())
    texts = [(u.p_index, u.text) for u in pr.units]
    assert texts == [
        (0, "Hello & welcome⟦x1⟧to the ⟦g2⟧show⟦/g2⟧."),
        (1, "Second line continues here"),
        (3, "Press {0} for ⟦g1⟧%1$s⟦/g1⟧⟦x2⟧ now"),
    ], texts
    print("  [parse] br point token, span paired, set kept whole, blank p skipped OK")


def test_translated_round_trip():
    f = _f()
    data = TT.encode()
    out = f.build(
        data,
        {
            "0": "Hola & bienvenidos⟦x1⟧al ⟦g2⟧programa⟦/g2⟧ <ya>.",
            "1": "Segunda línea\nsigue aquí",
            "3": "Pulse {0} para ⟦g1⟧%1$s⟦/g1⟧⟦x2⟧ ahora",
        },
    ).decode("utf-8")
    root = etree.fromstring(out.encode())
    p0 = '<p begin="00:00:01.000" end="00:00:04.000" style="s1">'
    assert p0 + "Hola &amp; bienvenidos<br/>" in out
    assert '<span tts:fontStyle="italic">programa</span> &lt;ya&gt;.</p>' in out
    assert "Segunda línea<br/>sigue aquí" in out, "typed newline becomes <br/>"
    assert '<set begin="1s" tts:color="red"/> ahora</p>' in out
    assert '<p begin="8s" end="9s"></p>' in out
    # Everything before <body> is byte-identical.
    assert out.split("<body>")[0] == TT.split("<body>")[0]
    assert root.get("{http://www.w3.org/XML/1998/namespace}lang") == "en"
    print("  [build] translated text spliced, timing and head untouched OK")


def test_tolerant_tokens():
    f = _f()
    data = TT.encode()
    out = f.build(data, {"0": "A ⟦x9⟧B ⟦g2⟧C ⟦x1⟧⟦x1⟧D ⟦/g7⟧E"}).decode("utf-8")
    etree.fromstring(out.encode())
    assert "⟦" not in out
    assert 'A B <span tts:fontStyle="italic">C <br/>D E</span></p>' in out, out
    print("  [tolerant] unknown/duplicate/stray dropped, unclosed span closed OK")


def test_localized_lang():
    f = _f()
    data = TT.encode()
    out = f.build_localized(data, {"3": "Pulse {0} ⟦g1⟧%1$s⟦/g1⟧⟦x2⟧"}, "es_ES").decode("utf-8")
    assert '"http://www.w3.org/ns/ttml#metadata" xml:lang="es-ES">' in out
    assert '<div xml:lang="es-ES">' in out
    assert '<span xml:lang="es-ES" tts:fontWeight="bold">' in out
    # An element in another language stays; a <tt> without xml:lang gets one.
    data2 = TT.replace(' xml:lang="en">\n  <head>', ">\n  <head>").replace(
        '<div xml:lang="en">', '<div xml:lang="fr">'
    )
    out2 = f.build_localized(data2.encode(), {}, "de").decode("utf-8")
    assert '<tt xml:lang="de" xmlns=' in out2 and '<div xml:lang="fr">' in out2
    assert f.build_localized(data, {}, None) == data
    print("  [lang] xml:lang switched on tt and source-language elements OK")


def test_prefixed_and_dfxp_namespace():
    f = _f()
    data = (
        b'<tt:tt xmlns:tt="http://www.w3.org/2006/10/ttaf1" xml:lang="en"><tt:body><tt:div>'
        b'<tt:p begin="1s" end="2s">One<tt:br/>Two</tt:p>'
        b'<x:p xmlns:x="urn:other">not a caption</x:p>'
        b"</tt:div></tt:body></tt:tt>"
    )
    pr = f.parse(data)
    assert [u.text for u in pr.units] == ["One⟦x1⟧Two"]
    out = f.build(data, {"0": "Uno\nDos"}).decode()
    assert '<tt:p begin="1s" end="2s">Uno<tt:br/>Dos</tt:p>' in out
    assert "not a caption" in out
    print("  [dfxp] legacy namespace, prefixes, foreign p ignored OK")


def test_encodings():
    f = _f()
    utf16 = TT.replace('encoding="UTF-8"', 'encoding="UTF-16"').encode("utf-16")
    pr = f.parse(utf16)
    assert pr.skeleton.get("source_encoding") == "utf-16"
    assert pr.units[1].text == "Second line continues here"
    out = f.build(utf16, {"1": "x"})
    assert out.startswith(b'<?xml version="1.0" encoding="UTF-8"?>')
    etree.fromstring(out)
    print("  [enc] UTF-16 with BOM read, exported as UTF-8 OK")


def _refused(data, exc=ValueError):
    try:
        _f().parse(data)
    except exc:
        return
    raise AssertionError(f"must be refused: {data[:80]!r}")


def test_hostile():
    _refused(
        b'<?xml version="1.0"?><!DOCTYPE tt [<!ENTITY x "boom">]>'
        b'<tt xmlns="http://www.w3.org/ns/ttml"><body><div><p>&x;</p></div></body></tt>'
    )
    _refused(
        b'<?xml version="1.0"?><!DOCTYPE tt SYSTEM "file:///etc/passwd">'
        b'<tt xmlns="http://www.w3.org/ns/ttml"><body/></tt>'
    )
    _refused(b'<tt xmlns="http://www.w3.org/ns/ttml"><body><div><p>&nbsp;</p></div></body></tt>')
    _refused(b"<html><body><p>hi</p></body></html>")
    _refused(b"<tt xmlns='http://www.w3.org/ns/ttml'><body><p>unclosed</body></tt>")
    _refused(b"\x00\x01\x02 not xml at all \x00")
    old = config.MAX_DOCUMENT_SEGMENTS
    config.MAX_DOCUMENT_SEGMENTS = 5
    try:
        many = "".join(f"<p>line {i}</p>" for i in range(8))
        _refused(
            f'<tt xmlns="http://www.w3.org/ns/ttml"><body><div>{many}</div></body></tt>'.encode(),
            InputLimitError,
        )
        _refused(
            (
                '<tt xmlns="http://www.w3.org/ns/ttml"><body><div><p>'
                + "<br/>" * 300
                + "</p></div></body></tt>"
            ).encode(),
            InputLimitError,
        )
    finally:
        config.MAX_DOCUMENT_SEGMENTS = old
    print("  [hostile] DOCTYPE/entities/non-TTML/malformed/binary/caps refused OK")


def test_comment_and_cdata_in_p():
    f = _f()
    data = (
        b'<tt xmlns="http://www.w3.org/ns/ttml" xml:lang="en"><body><div>'
        b"<p>Before<!-- note --> <![CDATA[a<b]]> after</p></div></body></tt>"
    )
    pr = f.parse(data)
    assert pr.units[0].text == "Before⟦x1⟧ a<b after", pr.units[0].text
    assert f.build(data, {}) == data
    out = f.build(data, {"0": "Antes⟦x1⟧ a<b después"}).decode()
    assert "<p>Antes<!-- note --> a&lt;b después</p>" in out
    print("  [misc] comment kept as token, CDATA text escaped on build OK")


def test_gate_caption_scope_space_and_head_lang():
    """Review gate: a <p> nested in <metadata> (inside a caption or in <head>) is not a
    caption and must not overlap the enclosing splice; xml:space is inherited; an explicit
    source-language tag in untranslated <head> metadata is not relabelled."""
    f = _f()
    data = (
        b'<tt xmlns="http://www.w3.org/ns/ttml" xmlns:ttm="http://www.w3.org/ns/ttml#metadata"'
        b' xml:lang="en"><head><metadata><p>head note</p>'
        b'<ttm:desc xml:lang="en">Demo</ttm:desc></metadata></head>'
        b'<body><div xml:space="preserve">'
        b"<p>Outer<metadata><p>inner</p></metadata>  two  spaces</p>"
        b"</div></body></tt>"
    )
    pr = f.parse(data)
    assert [(u.p_index, u.text) for u in pr.units] == [(0, "Outer⟦x1⟧  two  spaces")], [
        (u.p_index, u.text) for u in pr.units
    ]
    assert f.build(data, {}) == data
    out = f.build_localized(data, {"0": "Fuera⟦x1⟧ dos"}, "es").decode()
    root = etree.fromstring(out.encode())
    assert "<p>Fuera<metadata><p>inner</p></metadata> dos</p>" in out, out
    assert '<p>head note</p><ttm:desc xml:lang="en">Demo</ttm:desc>' in out, out
    assert root.get("{http://www.w3.org/XML/1998/namespace}lang") == "es"
    print("  [gate] nested/head <p> skipped, inherited xml:space, head lang kept OK")


_TTS2 = "http://www.w3.org/ns/ttml#styling"
_TTS_DFXP = "http://www.w3.org/2006/10/ttaf1#styling"
_TTS_DFXP_CR = "http://www.w3.org/2006/10/ttaf1#style"  # the DFXP CR's own TT Style URI


def _p_dirs(out: bytes, styling_ns: str) -> list:
    """Re-parse an export with the filter AND lxml: (computed-ish tts:direction on each
    caption <p>, text) in document order."""
    _f().parse(out)  # the export is still a TTML file our own filter accepts
    root = etree.fromstring(out.lstrip(b"\xef\xbb\xbf"))
    res = []
    for p in root.iter(f"{{{etree.QName(root).namespace}}}p"):
        res.append((p.get(f"{{{styling_ns}}}direction"), "".join(p.itertext()).strip()))
    return res


def test_rtl_direction_741():
    """#741: a right-to-left target gets tts:direction="rtl" on each rebuilt caption <p>,
    in the styling namespace the document binds (or a fresh, non-colliding prefix)."""
    f = _f()
    # (a) the soak repro shape: no styling namespace bound at all.
    base = (
        b'<?xml version="1.0" encoding="UTF-8"?>\n'
        b'<tt xmlns="http://www.w3.org/ns/ttml" xml:lang="en">\n'
        b"  <body>\n    <div>\n"
        b'      <p begin="1s" end="3s">Welcome back {0}!<br/>Line two</p>\n'
        b'      <p begin="4s" end="6s">Kept in English</p>\n'
        b'      <p begin="7s" end="9s">Third</p>\n'
        b"    </div>\n  </body>\n</tt>\n"
    )
    texts = {"0": "أهلاً {0}، مرحباً بك مجدداً!⟦x1⟧الصف الثاني", "2": "الثالث"}
    out = f.build_localized(base, texts, "ar-SA")
    s = out.decode()
    assert s.startswith(
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<tt xmlns:tts="http://www.w3.org/ns/ttml#styling" xmlns="http://www.w3.org/ns/ttml"'
        ' xml:lang="ar-SA">'
    ), s[:200]
    assert '<p tts:direction="rtl" begin="1s" end="3s">أهلاً' in s, s
    assert _p_dirs(out, _TTS2) == [
        ("rtl", "أهلاً {0}، مرحباً بك مجدداً!الصف الثاني"),
        (None, "Kept in English"),  # untranslated caption keeps its ltr paragraph level
        ("rtl", "الثالث"),
    ], _p_dirs(out, _TTS2)
    # Hebrew too; LTR target, build() and a no-op localized build stay as before.
    heb = f.build_localized(base, {"0": "שלום {0}!⟦x1⟧שורה"}, "he-IL")
    assert _p_dirs(heb, _TTS2)[0] == ("rtl", "שלום {0}!שורה"), _p_dirs(heb, _TTS2)
    fr = f.build_localized(base, {"0": "Bon retour {0} !⟦x1⟧Ligne deux"}, "fr-FR")
    assert b"direction" not in fr and b"#styling" not in fr, fr
    assert b"direction" not in f.build(base, texts)
    assert f.build(base, {}) == base
    noop = f.build_localized(base, {}, "ar-SA")
    assert noop == base.replace(b'xml:lang="en"', b'xml:lang="ar-SA"'), noop

    # (b) tts prefix already bound (TTML2): reused; explicit ltr on a translated <p>
    # flipped; ltr on an untouched <p>, span embeddings, region writingMode and textAlign kept.
    doc = (
        b'<tt xmlns="http://www.w3.org/ns/ttml" xmlns:s="http://www.w3.org/ns/ttml#styling"'
        b' xml:lang="en"><head><layout><region xml:id="r1" s:writingMode="lrtb"'
        b' s:textAlign="start"/></layout></head><body region="r1"><div>'
        b'<p s:direction = \'ltr\' begin="1s" end="2s">Open <span s:unicodeBidi="embed"'
        b' s:direction="ltr">a.txt</span> now</p>'
        b'<p s:direction="ltr" begin="3s" end="4s">Same</p>'
        b'<p begin="5s" end="6s" s:direction="rtl">Already</p>'
        b"</div></body></tt>"
    )
    out = f.build_localized(doc, {"0": "افتح ⟦g1⟧a.txt⟦/g1⟧ الآن", "2": "سابقاً"}, "ar")
    s = out.decode()
    assert "<p s:direction = 'rtl' begin=\"1s\"" in s, s
    assert '<span s:unicodeBidi="embed" s:direction="ltr">a.txt</span>' in s, s
    assert '<p s:direction="ltr" begin="3s" end="4s">Same</p>' in s, s
    assert '<p begin="5s" end="6s" s:direction="rtl">سابقاً</p>' in s, s
    assert 's:writingMode="lrtb" s:textAlign="start"' in s and "xmlns:tts" not in s, s
    assert [d for d, _t in _p_dirs(out, _TTS2)] == ["rtl", "ltr", "rtl"]

    # (c) legacy DFXP namespace with a prefix that is NOT tts, bound only on <body>.
    dfxp = (
        b'<tt xmlns="http://www.w3.org/2006/10/ttaf1" xml:lang="en"><body'
        b' xmlns:st="http://www.w3.org/2006/10/ttaf1#styling"><div>'
        b'<p begin="1s" end="2s">Hi</p></div></body></tt>'
    )
    out = f.build_localized(dfxp, {"0": "مرحبا"}, "ar")
    assert b'<p st:direction="rtl" begin="1s"' in out and b"xmlns:tts" not in out, out
    assert _p_dirs(out, _TTS_DFXP) == [("rtl", "مرحبا")]

    # (d) prefix "tts" already taken by another namespace: a fresh prefix, no collision.
    clash = (
        b'<tt xmlns="http://www.w3.org/2006/10/ttaf1" xmlns:tts="urn:not-styling"'
        b' xml:lang="en"><body><div><p tts:x="1" begin="1s" end="2s">Hi</p></div></body></tt>'
    )
    out = f.build_localized(clash, {"0": "שלום"}, "he")
    assert b'xmlns:tts1="http://www.w3.org/2006/10/ttaf1#style"' in out, out  # CR Table 1
    assert b'<p tts1:direction="rtl" tts:x="1"' in out, out
    assert _p_dirs(out, _TTS_DFXP_CR) == [("rtl", "שלום")]
    root = etree.fromstring(out)
    assert next(root.iter("{http://www.w3.org/2006/10/ttaf1}p")).get("{urn:not-styling}x") == "1"
    print("  [741] rtl target: tts:direction=rtl on rebuilt <p>, prefix reuse/bind, ltr flip OK")


def test_gate_rtl_dfxp_style_ns_741():
    """Gate #741: the DFXP CR (2006/10) and WD (2006/04) define the TT Style namespace as
    ``ttaf1#style`` (Table 1), which Netflix-era DFXP files carry. Its prefix is reused and an
    explicit ltr flipped, never shadowed by a second ``#styling`` binding. Also: prefixed
    TTML element names, xml:space="preserve", CRLF and UTF-16 survive the splice."""
    f = _f()
    doc = (
        b'<tt xmlns="http://www.w3.org/2006/10/ttaf1"'
        b' xmlns:tts="http://www.w3.org/2006/10/ttaf1#style" xml:lang="en"><body><div>'
        b'<p begin="1s" end="2s" tts:direction="ltr" tts:color="white">Hi!</p>'
        b'<p begin="3s" end="4s">Bye</p></div></body></tt>'
    )
    out = f.build_localized(doc, {"0": "مرحبا!", "1": "وداعا"}, "ar")
    s = out.decode()
    assert s.count("xmlns:") == 1 and "#styling" not in s, s
    assert '<p begin="1s" end="2s" tts:direction="rtl" tts:color="white">' in s, s
    assert '<p tts:direction="rtl" begin="3s" end="4s">' in s, s
    assert [d for d, _t in _p_dirs(out, _TTS_DFXP_CR)] == ["rtl", "rtl"]
    old = doc.replace(b"2006/10", b"2006/04")  # the 2006/04 WD: same rule
    out = f.build_localized(old, {"1": "וַדָּאִי"}, "he")
    assert b'<p tts:direction="rtl" begin="3s"' in out and out.count(b"xmlns:") == 1, out

    # Prefixed TTML elements (no default namespace), xml:space preserve, CRLF line ends.
    pref = (
        b'<tt:tt xmlns:tt="http://www.w3.org/ns/ttml" xml:lang="en">\r\n<tt:body><tt:div>\r\n'
        b'<tt:p xml:space="preserve" begin="1s" end="2s">  Two  spaces </tt:p>\r\n'
        b"</tt:div></tt:body></tt:tt>\r\n"
    )
    out = f.build_localized(pref, {"0": "שתי  רווחים"}, "he-IL")  # unit text is trimmed
    assert out.startswith(
        b'<tt:tt xmlns:tts="http://www.w3.org/ns/ttml#styling" xmlns:tt="http://www.w3.org/ns/ttml"'
    ), out
    want = '<tt:p tts:direction="rtl" xml:space="preserve" begin="1s" end="2s">'
    want += "  שתי  רווחים </tt:p>"
    assert (want + "\r\n").encode() in out, out
    assert out.count(b"\r\n") == pref.count(b"\r\n"), out
    assert _p_dirs(out, _TTS2) == [("rtl", "שתי  רווחים")]

    # UTF-16 with a BOM: exported as UTF-8 (declaration rewritten, #509), direction present.
    u16 = (
        '<?xml version="1.0" encoding="UTF-16"?>\n<tt xmlns="http://www.w3.org/ns/ttml"'
        ' xml:lang="en"><body><div><p begin="1s" end="2s">Hi</p></div></body></tt>'
    ).encode("utf-16")
    out = f.build_localized(u16, {"0": "سلام"}, "fa-IR")
    assert out.startswith(b'<?xml version="1.0" encoding="UTF-8"?>'), out[:60]
    dec = out.decode("utf-8")
    assert '<p tts:direction="rtl" begin="1s" end="2s">سلام</p>' in dec, dec
    root = etree.fromstring(out)
    assert next(root.iter("{http://www.w3.org/ns/ttml}p")).get(f"{{{_TTS2}}}direction") == "rtl"
    f.parse(out)
    print("  [741 gate] DFXP #style reused + ltr flipped, tt:p prefix, preserve, CRLF, UTF-16 OK")


def main():
    test_registered()
    test_identity_round_trip()
    test_units_and_tokens()
    test_translated_round_trip()
    test_tolerant_tokens()
    test_localized_lang()
    test_prefixed_and_dfxp_namespace()
    test_encodings()
    test_hostile()
    test_comment_and_cdata_in_p()
    test_gate_caption_scope_space_and_head_lang()
    test_rtl_direction_741()
    test_gate_rtl_dfxp_style_ns_741()
    print("TTML FILTER OK")


if __name__ == "__main__":
    main()
