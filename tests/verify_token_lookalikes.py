"""Token look-alike text in a source is kept on export (#814 item 1).

Source text that merely looks like one of our inline tokens (an x2 token in brackets) used to be
read as a code by every writer that re-reads a translation string, and deleted or moved. Now:
the unit's real codes skip the numbers look-alikes use, and a token in a translation that names
no code of the unit is kept as text. Covers HTML, WebVTT, SubRip, MDX (also its CJK emphasis
repair), XLIFF, DOCX and PPTX. ASCII output only.
"""

import io
import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(_ROOT)
sys.path.insert(0, _ROOT)
from tests._bootstrap import init_env

init_env("loctms_lookalike_")

from app.filters import base, get_filter

X = "\u27e6"
Y = "\u27e7"


def tok(s: str) -> str:
    return X + s + Y


def test_html():
    f = get_filter("html")
    src = f"<p>Use {tok('x2')} as the marker</p>".encode()
    assert f.parse(src).units[0].text == f"Use {tok('x2')} as the marker"
    out = f.build(src, {"0": f"Nutze {tok('x2')} als Marke"}).decode()
    assert out == f"<p>Nutze {tok('x2')} als Marke</p>", out
    # a real code collides with the look-alike number: the code skips it
    src = f"<p>Use {tok('g1')} and <b>bold</b> here</p>".encode()
    u = f.parse(src).units[0]
    assert u.text == f"Use {tok('g1')} and {tok('g2')}bold{tok('/g2')} here", u.text
    out = f.build(src, {"0": f"A {tok('g1')} y {tok('g2')}negrita{tok('/g2')} aqui"}).decode()
    assert out == f"<p>A {tok('g1')} y <b>negrita</b> aqui</p>", out
    # an unchanged unit is the upload
    assert f.build(src, {"0": u.text}) == src
    # a stored document imported before the rule keeps its numbering
    old = {"units": [{"p_index": 0, "text": "x"}]}
    with base.document_code_page(old):
        # the filter export picks for that stored (Python) document: Python, switch on or off
        old_f = get_filter("html", skeleton=old)
        got = old_f.parse(src).units[0].text
        assert got == f"Use {tok('g1')} and {tok('g1')}bold{tok('/g1')} here"
    with base.source_code_page("en-US") as hint:
        parsed = f.parse(src)
    assert base.apply_code_page_hint(parsed.skeleton, hint).get(base.READ_GEN_KEY) == base.READ_GEN


def test_vtt_srt():
    f = get_filter("vtt")
    vtt = f"WEBVTT\n\n00:00:01.000 --> 00:00:02.000\nUse {tok('x1')} and <b>bold</b> now\n".encode()
    u = f.parse(vtt).units[0]
    assert u.text == f"Use {tok('x1')} and {tok('g2')}bold{tok('/g2')} now", u.text
    out = f.build(vtt, {"0": f"Usa {tok('x1')} y {tok('g2')}negrita{tok('/g2')} ya"}).decode()
    assert out.endswith(f"Usa {tok('x1')} y <b>negrita</b> ya\n"), out
    s = get_filter("srt")
    srt = f"1\n00:00:01,000 --> 00:00:02,000\nUse {tok('x1')} {{\\i1}}now\n".encode()
    u = s.parse(srt).units[0]
    assert u.text == f"Use {tok('x1')} {tok('x2')}now", u.text
    out = s.build(srt, {"0": f"Usa {tok('x1')} {tok('x2')}ya"}).decode()
    assert f"Usa {tok('x1')} {{\\i1}}ya" in out, out


def test_mdx():
    f = get_filter("mdx")
    src = f"Use {tok('x1')} and <Kbd>Ctrl</Kbd> here\n".encode()
    u = f.parse(src).units[0]
    assert u.text == f"Use {tok('x1')} and {tok('g2')}Ctrl{tok('/g2')} here", u.text
    out = f.build(src, {"0": f"Usa {tok('x1')} y {tok('g2')}Ctrl{tok('/g2')} aqui"}).decode()
    assert out == f"Usa {tok('x1')} y <Kbd>Ctrl</Kbd> aqui\n", out
    # the CJK emphasis repair would move a mark into a token: skipped for the line
    src = b"*bold* and <Kbd>k</Kbd>\n"
    u = f.parse(src).units[0]
    assert u.text == f"*bold* and {tok('g1')}k{tok('/g1')}", u.text
    new = f"*\u65e5\u672c{tok('g1')}k{tok('/g1')}*\u3068"
    out = f.build(src, {"0": new}).decode()
    assert out == "*\u65e5\u672c<Kbd>k</Kbd>*\u3068\n", out


def test_xliff():
    f = get_filter("xliff")
    x = (
        '<xliff version="1.2" xmlns="urn:oasis:names:tc:xliff:document:1.2">'
        '<file source-language="en" datatype="plaintext" original="a">'
        '<body><trans-unit id="1"><source>Use '
        + tok("x1")
        + ' and <g id="1">bold</g> now</source></trans-unit></body></file></xliff>'
    ).encode()
    u = f.parse(x).units[0]
    assert u.text == f"Use {tok('x1')} and {tok('g2')}bold{tok('/g2')} now", u.text
    out = f.build(
        x, {"0": f"Usa {tok('x1')} y {tok('g2')}negrita{tok('/g2')} ya {tok('x9')}"}
    ).decode()
    assert f'<target>Usa {tok("x1")} y <g id="1">negrita</g> ya {tok("x9")}</target>' in out, out


def test_office():
    import docx

    f = get_filter("docx")
    d = docx.Document()
    p = d.add_paragraph()
    p.add_run("Use ")
    p.add_run("bold").bold = True
    d.add_paragraph(f"Plain {tok('x2')} text")
    buf = io.BytesIO()
    d.save(buf)
    out = f.build(
        buf.getvalue(),
        {
            "0": f"Usa {tok('x7')} {tok('r1')}uno{tok('/r1')}{tok('r2')}negrita{tok('/r2')}",
            "1": f"Llano {tok('x2')} texto",
        },
    )
    paras = [q.text for q in docx.Document(io.BytesIO(out)).paragraphs]
    assert paras == [f"Usa {tok('x7')} unonegrita", f"Llano {tok('x2')} texto"], paras
    # PPTX: a point token that names no break or field of the paragraph is text
    from app.filters import pptx_filter

    spans = pptx_filter._spans(f"a {tok('x9')} b", 1, 0)
    assert "".join(s[2] for s in spans if s[0] == "t") == f"a {tok('x9')} b", spans


def main():
    test_html()
    test_vtt_srt()
    test_mdx()
    test_xliff()
    test_office()
    print("TOKEN LOOKALIKES OK")


if __name__ == "__main__":
    main()
