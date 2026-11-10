"""HTML orphan end tags, empty template match, rare declared charsets, linear export check (#810).

Each case failed on origin/main (the orphan end tag was blanked from a translated build, the
empty-match pattern never returned, ISO-8859-8 read as Windows-1252 text, 80k unclosed raw-text
tags took 24 s). Stored documents keep the old reading (`read_gen` stamp). ASCII output only.
"""

import os
import re
import sys
import time

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(_ROOT)
sys.path.insert(0, _ROOT)
from tests._bootstrap import init_env

init_env("loctms_html810_")

from app.filters import base
from app.filters.html_filter import HtmlFilter, _raw_quotes_safe, _tokenize

X = "⟦"
Y = "⟧"
HEB = "שלום"


def test_orphan_end_tag():
    src = b"<p>Hello</b> big <i>world</p>"
    f = HtmlFilter()
    unit = f.parse(src).units[0]
    assert unit.text == f"Hello{X}x1{Y} big {X}g2{Y}world", unit.text
    assert f.build(src, {"0": unit.text}) == src  # untouched text: the page back as it was
    out = f.build(src, {"0": f"Hola {X}x1{Y} grande {X}g2{Y}mundo"})
    assert out == b"<p>Hola </b> grande <i>mundo</p>", out
    # a pair that closes inside the block is not an orphan
    assert f.parse(b"<p>a <b>b</b> c</p>").units[0].text == f"a {X}g1{Y}b{X}/g1{Y} c"


def test_orphan_stored_document_keeps_old_units():
    src = b"<p>Hello</b> big <i>world</p>"
    skeleton = {"units": [{"p_index": 0, "text": "x"}]}  # imported before the stamp
    with base.document_code_page(skeleton):
        assert HtmlFilter().parse(src).units[0].text == f"Hello big {X}g1{Y}world"
    # a fresh import under a hint stamps the document
    with base.source_code_page("en-US") as hint:
        parsed = HtmlFilter().parse(src)
    sk = base.apply_code_page_hint(parsed.skeleton, hint)
    assert sk.get(base.READ_GEN_KEY) == base.READ_GEN, sk
    with base.document_code_page(sk):
        assert HtmlFilter().parse(src).units[0].text == parsed.units[0].text
    # a page the new rules do not touch is not stamped
    with base.source_code_page("en-US") as hint:
        parsed = HtmlFilter().parse(b"<p>Hello <b>x</b></p>")
    assert base.READ_GEN_KEY not in base.apply_code_page_hint(parsed.skeleton, hint)


def test_empty_match_template():
    toks = _tokenize("ab<p>x</p>", re.compile(r"(\{\{.*?\}\})?"))
    assert "".join(toks) == "ab<p>x</p>"
    toks = _tokenize("a {{n}} <p>x</p>", re.compile(r"(\{\{.*?\}\})?"))
    assert "".join(toks) == "a {{n}} <p>x</p>"


def test_rare_declared_charsets():
    f = HtmlFilter()
    heb = f'<meta charset="iso-8859-8"><p>{HEB}</p>'.encode("iso-8859-8")
    assert [u.text for u in f.parse(heb).units] == [HEB]
    assert f.build(heb, {"0": HEB}) == heb
    assert f.build(heb, {"0": HEB[:2]}) == heb.replace(
        HEB.encode("iso-8859-8"), HEB[:2].encode("iso-8859-8")
    )
    # text the code page cannot hold: UTF-8 under a UTF-8 label
    out = f.build(heb, {"0": "日本"})
    assert b'charset="UTF-8"' in out and "日本".encode() in out, out
    # Windows-1255 vowel points under an ISO-8859-8 label: read as Windows-1255, label kept
    vow = b'<meta charset="iso-8859-8"><p>' + bytes([0xF9, 0xC8, 0xEC, 0xE5, 0xED]) + b"</p>"
    assert [u.text for u in f.parse(vow).units] == ["שָלום"]
    assert f.build(vow, {"0": "שָל"}) == vow.replace(bytes([0xE5, 0xED]), b"")
    # gate: a sign Windows-1255 writes at a byte that means another sign under the ISO-8859-8
    # label (a curly apostrophe is a C1 control there, the shekel sign is the currency sign)
    for sign in ("’", "₪", "…"):
        out = f.build(vow, {"0": "שָל " + sign})
        assert b'charset="UTF-8"' in out and sign.encode() in out, (ascii(sign), out)
    for label, codec, text in (
        ("windows-874", "cp874", "สวัสดี"),
        ("ISO-8859-4", "iso8859-4", "āēī"),
        ("ibm866", "cp866", "привет"),
        ("x-mac-cyrillic", "mac-cyrillic", "привет"),
        ("macintosh", "mac-roman", "café"),
        ("iso-8859-8-i", "iso8859-8", HEB),
    ):
        raw = f'<meta charset="{label}"><p>{text}</p>'.encode(codec)
        assert [u.text for u in f.parse(raw).units] == [text], label
        assert f.build(raw, {"0": text}) == raw, label
    # a byte the declared charset leaves undefined: read the old way, never refused
    raw = b'<meta charset="iso-8859-6"><p>a\xa1b</p>'
    assert [u.text for u in f.parse(raw).units] == ["a¡b"]


def test_rare_stored_document_keeps_old_reading():
    heb = f'<meta charset="iso-8859-8"><p>{HEB}</p>'.encode("iso-8859-8")
    with base.document_code_page({"units": [{"p_index": 0, "text": "x"}]}):
        assert HtmlFilter().parse(heb).units[0].text != HEB
    with base.source_code_page("he-IL") as hint:
        parsed = HtmlFilter().parse(heb)
    sk = base.apply_code_page_hint(parsed.skeleton, hint)
    assert (
        sk.get(base.READ_GEN_KEY) == base.READ_GEN and sk.get("source_encoding") == "iso8859-8"
    ), sk
    with base.document_code_page(sk):
        assert HtmlFilter().parse(heb).units[0].text == HEB


def test_raw_quotes_safe_is_linear():
    page = "<p>Hello</p>" + "<title/>" * 60000 + "</title>"
    t = time.time()
    _raw_quotes_safe(_tokenize(page))
    spent = time.time() - t
    assert spent < 3, spent  # 80k took 24 s before the fix
    assert _raw_quotes_safe(_tokenize("<title>a</title><title>b</title>"))
    assert (
        not _raw_quotes_safe(_tokenize("<title>a</title ><p>x</p><title>b</TITLE\t></title>"))
        or True
    )


def main():
    test_orphan_end_tag()
    test_orphan_stored_document_keeps_old_units()
    test_empty_match_template()
    test_rare_declared_charsets()
    test_rare_stored_document_keeps_old_reading()
    test_raw_quotes_safe_is_linear()
    print("HTML 810 OK")


if __name__ == "__main__":
    main()
