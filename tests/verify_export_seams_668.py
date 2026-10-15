"""Export rejoins a unit's segments with the unit's own whitespace (GitHub #668, #671).

A segmented export rebuilds each unit from its trimmed segments. It used to `" ".join` them,
so (#668) an ASCII space landed between Japanese / Chinese sentences (`。 次に`) and a held
Japanese source changed, and (#671) the unit's leading / trailing whitespace and the original
inter-sentence whitespace (a line break inside a subtitle cue, a double space) were lost. The
fix lives in the shared rejoin (`segment.unit_layout` / `segment.join_unit`, called from
`export.paragraph_texts_for`), so every segmented format gets it; this suite drives it through
the real upload door and `build_translated_file` for txt, csv, html, srt and docx.

  cjk       en -> ja / zh-CN: no space after 。 (txt, html, docx, srt)
  held      a ja source exported with its own text is byte-identical (no space inserted)
  ja->en    a no-space source seam between two English sentences gets one space
  edges     CSV cell leading / trailing whitespace kept, translated or not (#671)
  lines     a two-line SRT cue keeps its line break instead of becoming one line
  unit      seam_separator / join_unit rules (Thai, mixed script, approved-empty positions)

Hermetic + offline. Final line: "EXPORT SEAMS OK".
"""

import io
import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(_ROOT)
sys.path.insert(0, _ROOT)
from tests._bootstrap import init_env

init_env("loctms_seams668_")

from fastapi.testclient import TestClient
from sqlmodel import Session, select

from app.db import engine, init_db
from app.main import app
from app.models import Document, Segment
from app.services import export
from app.services.segment import join_unit, seam_separator, unit_layout

init_db()
_client = TestClient(app)
_N = [0]


def _project(src: str, tgt: str) -> int:
    _N[0] += 1
    r = _client.post(
        "/projects",
        data={
            "name": f"seams-{_N[0]}",
            "source_locale": src,
            "target_locales": tgt,
            "default_provider": "mock",
        },
        follow_redirects=False,
    )
    assert r.status_code in (302, 303), (r.status_code, r.text[:300])
    return int(r.headers["location"].rsplit("/", 1)[-1])


def _export(src: str, tgt: str, name: str, data: bytes, targets) -> bytes:
    """Upload `data`, set each segment's target (a list, or a callable source -> target),
    and return the exported bytes."""
    pid = _project(src, tgt)
    r = _client.post(
        f"/projects/{pid}/documents",
        data={"mode": "segmented"},
        files={"file": (name, data, "application/octet-stream")},
        follow_redirects=False,
    )
    assert r.status_code in (302, 303), (r.status_code, r.text[:300])
    doc_id = int(r.headers["location"].rsplit("/", 1)[-1].split("?")[0])
    with Session(engine) as s:
        segs = s.exec(
            select(Segment)
            .where(Segment.document_id == doc_id, Segment.target_locale == tgt)
            .order_by(Segment.order_index)
        ).all()
        want = [targets(g.source_text) for g in segs] if callable(targets) else list(targets)
        assert len(want) == len(segs), ([g.source_text for g in segs], want)
        for g, t in zip(segs, want, strict=True):
            g.target_text = t
            g.status = "mt"
            s.add(g)
        s.commit()
        return export.build_translated_file(s, s.get(Document, doc_id), tgt)


EN = "Open the settings page. Then choose a language."
JA = ["設定ページを開きます。", "次に言語を選択してください。"]
ZH = ["打开设置页面。", "然后选择一种语言。"]


def test_cjk_txt():
    out = _export("en-US", "ja-JP", "two.txt", (EN + "\n").encode(), JA)
    assert out.decode("utf-8") == "".join(JA) + "\n", out.decode("utf-8")
    out = _export("en-US", "zh-CN", "two.txt", (EN + "\n").encode(), ZH)
    assert out.decode("utf-8") == "".join(ZH) + "\n", out.decode("utf-8")
    print("  [cjk] txt en->ja / en->zh-CN: no space after the full-width stop OK")


def test_held_ja_and_ja_to_en():
    src = "これは一文目です。これは二文目です。\n".encode()
    out = _export("ja-JP", "en-US", "ja.txt", src, lambda t: t)
    assert out == src, out.decode("utf-8")
    out = _export(
        "ja-JP", "en-US", "ja.txt", src, ["This is sentence one.", "This is sentence two."]
    )
    assert out.decode("utf-8") == "This is sentence one. This is sentence two.\n", out
    print("  [held] ja source byte-identical; ja->en gets one space between sentences OK")


def test_html_cjk():
    src = b"<html><body><p>Open the settings page. Then choose a language.</p></body></html>"
    out = _export("en-US", "ja-JP", "page.html", src, JA).decode("utf-8")
    assert "<p>" + "".join(JA) + "</p>" in out, out
    print("  [cjk] html en->ja OK")


def test_docx_cjk():
    from docx import Document as Docx

    d = Docx()
    d.add_paragraph(EN)
    buf = io.BytesIO()
    d.save(buf)
    out = _export("en-US", "ja-JP", "two.docx", buf.getvalue(), JA)
    paras = [p.text for p in Docx(io.BytesIO(out)).paragraphs if p.text]
    assert paras == ["".join(JA)], paras
    print("  [cjk] docx en->ja OK")


def test_srt_lines_and_cjk():
    # (the SRT writer emits LF line ends whatever the upload had, so the fixture is LF)
    src = b"1\n00:00:01,000 --> 00:00:03,000\nHello there.\nHow are you?\n"
    # held: the two-line cue stays two lines (it used to collapse into "Hello there. How...")
    out = _export("en-US", "de-DE", "c.srt", src, lambda t: t)
    assert out == src, out
    out = _export("en-US", "ja-JP", "c.srt", src, ["こんにちは。", "お元気ですか？"])
    assert "こんにちは。\nお元気ですか？" in out.decode("utf-8"), out.decode("utf-8")
    src1 = b"1\n00:00:01,000 --> 00:00:03,000\nOpen the settings page. Then choose a language.\n"
    out = _export("en-US", "ja-JP", "c.srt", src1, JA)
    assert "".join(JA) in out.decode("utf-8"), out.decode("utf-8")
    print("  [lines] srt cue line break kept; en->ja no space OK")


def test_csv_edges():
    src = (
        b'key,text\r\nk1,"First sentence. Second sentence. "\r\n'
        b'k2,"Trailing space "\r\nk3,"  Lead"\r\n'
    )
    out = _export("en-US", "de-DE", "t.csv", src, lambda t: t)
    assert out == src, out
    out = _export(
        "en-US", "de-DE", "t.csv", src, ["Erster Satz.", "Zweiter Satz.", "Leerzeichen", "Vorne"]
    )
    assert out == (
        b'key,text\r\nk1,"Erster Satz. Zweiter Satz. "\r\nk2,"Leerzeichen "\r\nk3,"  Vorne"\r\n'
    ), out
    print("  [edges] csv cell leading/trailing whitespace kept (held + translated) OK")


def test_unit_rules():
    lay = unit_layout("  One.  Two.\nThree. ", "en-US", "legacy")
    assert lay["lead"] == "  " and lay["trail"] == " " and lay["seps"] == ["  ", "\n"], lay
    assert join_unit(lay["pieces"], lay) == "  One.  Two.\nThree. "
    # approved-empty delivery: the dropped middle member takes no separator of its own
    assert join_unit(["Eins.", "Drei."], lay, positions=[0, 2]) == "  Eins.\nDrei. "
    # all-empty stays exactly "" (no edge whitespace around nothing)
    assert join_unit([], lay) == ""
    # mixed-script source seam kept verbatim when untranslated
    assert seam_separator(" ", "さん！", "One file.", "さん！", "One file.") == " "
    # a translated seam ending in a full-width stop drops the space
    assert seam_separator(" ", "さん！", "次に", "Hi!", "Then") == ""
    # Thai with no source whitespace stays without; Thai with a space keeps it
    assert seam_separator("", "ประโยคหนึ่ง", "ประโยคสอง") == ""
    assert seam_separator(" ", "ประโยคหนึ่ง", "ประโยคสอง") == " "
    # Korean uses spaces between sentences
    assert seam_separator(" ", "설정 페이지를 엽니다.", "그런 다음") == " "
    print("  [unit] seam rules OK")


def test_whitespace_only_unit_675():
    """#675: an XSLT text node that is only &#160; parses to a unit with ZERO segments
    (split_sentences strips it away). Export used max(1, ...) and shifted every later unit
    one slot up -- also on a held export with no translation at all."""
    src = (
        b'<?xml version="1.0" encoding="UTF-8"?>\n'
        b'<xsl:stylesheet version="1.0" xmlns:xsl="http://www.w3.org/1999/XSL/Transform">\n'
        b'<xsl:template match="/">\n'
        b"<p><xsl:if test=\". = ''\">&#160;</xsl:if></p>\n"
        b"<p>First sentence.</p>\n<p>Second sentence.</p>\n<p>Third sentence.</p>\n"
        b"</xsl:template>\n</xsl:stylesheet>\n"
    )
    out = _export("en-US", "de-DE", "t.xsl", src, lambda t: t)
    assert out == src, out
    de = ["Erster Satz.", "Zweiter Satz.", "Dritter Satz."]
    out = _export("en-US", "de-DE", "t.xsl", src, de)
    assert out == src.replace(b"First sentence.", b"Erster Satz.").replace(
        b"Second sentence.", b"Zweiter Satz."
    ).replace(b"Third sentence.", b"Dritter Satz."), out
    print("  [675] whitespace-only unit keeps its place; later units not shifted OK")


def main():
    test_unit_rules()
    test_cjk_txt()
    test_held_ja_and_ja_to_en()
    test_html_cjk()
    test_docx_cjk()
    test_srt_lines_and_cjk()
    test_csv_edges()
    test_whitespace_only_unit_675()
    print("EXPORT SEAMS OK")


if __name__ == "__main__":
    main()
