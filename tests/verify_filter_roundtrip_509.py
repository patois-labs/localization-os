"""File-filter round-trip fidelity (issue #509): one round-trip test per format.

  SVG    numeric and quote entities are decoded once and escaped once (no `&amp;#169;`).
  XLIFF  text after a dropped (phantom) paired tag stays AFTER the emitted children.
  DOCX   bookmark / comment range markers stay between the runs they bracketed.
  text   non-UTF-8 input is refused at parse; CRLF and the trailing newline survive build;
         a stored non-UTF-8 original is rebuilt without U+FFFD in untranslated lines.
  srt/md/po/html  refuse non-UTF-8 at parse like the JSON filter.

Hermetic + offline. Final line: "FILTER ROUNDTRIP 509 OK".
"""

import io
import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(_ROOT)
sys.path.insert(0, _ROOT)
from tests._bootstrap import init_env

init_env("loctms_filter509_")

from app.filters import get_filter

LATIN1 = "café au lait".encode("latin-1")
# 0x81 is undefined in Windows-1252 and invalid UTF-8: no reading of it is meaningful.
UNDECODABLE = b"caf\x81 au lait"


def test_svg_entities_round_trip():
    filt = get_filter("image")
    svg = (
        b'<svg xmlns="http://www.w3.org/2000/svg"><text>'
        b"&#169; 2024 &quot;Acme&quot; &#x41; &amp; Co</text></svg>"
    )
    parsed = filt.parse(svg)
    text = parsed.units[0].text
    assert text == '© 2024 "Acme" A & Co', ("entities must decode once", text)
    assert filt.build(svg, {"0": text}) == svg  # B-bytes: an identity build is the original
    out = filt.build(svg, {"0": text + "!"}).decode("utf-8")
    assert "&amp;#169;" not in out and "&amp;quot;" not in out, out
    assert '© 2024 "Acme" A &amp; Co!' in out, out
    # a translation with markup characters is still escaped exactly once
    out2 = filt.build(svg, {"0": "a < b & c"}).decode("utf-8")
    assert "a &lt; b &amp; c" in out2, out2
    print("  [svg] numeric and quote entities round-trip without double escaping OK")


def test_xliff_text_after_phantom_stays_in_order():
    from app.services.segment import ph_close, ph_open

    filt = get_filter("xliff")
    xlf = (
        b'<?xml version="1.0" encoding="UTF-8"?>'
        b'<xliff version="1.2"><file source-language="en" target-language="de">'
        b'<body><trans-unit id="1"><source>Please <g id="1">Click</g> here</source>'
        b"</trans-unit></body></file></xliff>"
    )
    # MT duplicated the paired tag: the second pair is a phantom frame.
    new = (
        "bitte "
        + ph_open("g", 1)
        + "Klick"
        + ph_close("g", 1)
        + " hier "
        + ph_open("g", 1)
        + "doppelt"
        + ph_close("g", 1)
        + " jetzt"
    )
    out = filt.build(xlf, {"0": new}).decode("utf-8")
    assert '<target>bitte <g id="1">Klick</g> hier doppelt jetzt</target>' in out, out
    print("  [xliff] text after a phantom tag keeps its position OK")


def _docx_with_markers() -> bytes:
    from docx import Document
    from docx.oxml import OxmlElement
    from docx.oxml.ns import qn

    doc = Document()
    para = doc.add_paragraph()
    para.add_run("Hello ")
    bs = OxmlElement("w:bookmarkStart")
    bs.set(qn("w:id"), "7")
    bs.set(qn("w:name"), "mark")
    para._p.append(bs)
    bold = para.add_run("bold world")
    bold.bold = True
    be = OxmlElement("w:bookmarkEnd")
    be.set(qn("w:id"), "7")
    para._p.append(be)
    para.add_run(" end")
    buf = io.BytesIO()
    doc.save(buf)
    return buf.getvalue()


def test_docx_range_markers_stay_between_runs():
    from docx import Document
    from docx.oxml.ns import qn

    filt = get_filter("docx")
    data = _docx_with_markers()
    parsed = filt.parse(data)
    unit = next(u for u in parsed.units if "Hello" in u.text)
    out = filt.build(data, {str(unit.p_index): unit.text})
    doc = Document(io.BytesIO(out))
    target = next(p for p in doc.paragraphs if "Hello" in p.text)
    kinds = []
    for child in target._p:
        if child.tag == qn("w:bookmarkStart"):
            kinds.append("BS")
        elif child.tag == qn("w:bookmarkEnd"):
            kinds.append("BE")
        elif child.tag == qn("w:r"):
            kinds.append("R:" + "".join(t.text or "" for t in child.iter(qn("w:t"))))
    kinds = [k for k in kinds if k != "R:"]
    assert kinds == ["R:Hello ", "BS", "R:bold world", "BE", "R: end"], kinds
    print("  [docx] bookmark range markers stay around the run they bracketed OK")


def test_text_cp1252_converted_and_endings_preserved():
    filt = get_filter("txt")
    # 2026-10-06: a Windows-1252 file is CONVERTED, not refused.
    parsed = filt.parse(LATIN1)
    assert [u.text for u in parsed.units] == ["café au lait"], parsed.units
    assert parsed.skeleton.get("source_encoding") == "cp1252", parsed.skeleton
    # ...but bytes undefined in Windows-1252 cannot be read meaningfully: refused
    try:
        filt.parse(UNDECODABLE)
    except ValueError as exc:
        assert "re-save the file as UTF-8" in str(exc), exc
    else:
        raise AssertionError("a .txt that is neither UTF-8 nor Windows-1252 must be refused")
    # a UTF-8 file's skeleton is unchanged (no encoding key)
    assert "source_encoding" not in filt.parse("café".encode()).skeleton
    data = b"Hello\r\n\r\n  World\r\nlast\r\n"
    out = filt.build(data, {"0": "Hallo", "2": "Ende"})
    assert out == b"Hallo\r\n\r\n  World\r\nEnde\r\n", out
    # a stored Windows-1252 original exports in Windows-1252 when the text fits (B-bytes),
    # as UTF-8 with the right characters when it does not
    stored = b"Hello\ncaf\xe9\nbye\n"
    out2 = filt.build(stored, {"0": "Hallo"})
    assert out2 == b"Hallo\ncaf\xe9\nbye\n", out2
    out2 = filt.build(stored, {"0": "こんにちは"})
    assert out2 == "こんにちは\ncafé\nbye\n".encode(), out2
    # a stored pre-#509 original that is not even Windows-1252 keeps its bytes
    legacy = b"Hello\ncaf\x81\nbye\n"
    out3 = filt.build(legacy, {"0": "Hallo"})
    assert out3 == b"Hallo\ncaf\x81\nbye\n", out3
    print("  [txt] cp1252 converted, undecodable refused, CRLF and final newline kept OK")


def test_other_text_formats_convert_cp1252():
    def samples(raw: bytes) -> dict:
        return {
            "srt": b"1\n00:00:01,000 --> 00:00:02,000\n" + raw + b"\n",
            "md": b"# " + raw + b"\n",
            "po": b'msgid "' + raw + b'"\nmsgstr ""\n',
            "html": b"<p>" + raw + b"</p>",
            "svg": b'<svg xmlns="http://www.w3.org/2000/svg"><text>' + raw + b"</text></svg>",
        }

    for fmt, raw in samples(LATIN1).items():
        units = get_filter(fmt).parse(raw).units
        assert [u.text for u in units] == ["café au lait"], (fmt, units)
    for fmt, raw in samples(UNDECODABLE).items():
        try:
            get_filter(fmt).parse(raw)
        except ValueError as exc:
            assert "re-save the file as UTF-8" in str(exc), (fmt, exc)
            continue
        raise AssertionError(f"{fmt}: an unreadable upload must be refused at parse")
    # a stored Windows-1252 markdown original rebuilds as UTF-8
    md = b"# Title\n\ncaf\xe9\n"
    out = get_filter("md").build(md, {"0": "Titel"})
    assert out == "# Titel\n\ncafé\n".encode(), out  # #677: final newline kept
    print("  [srt/md/po/html/svg] cp1252 converted, undecodable refused, never U+FFFD OK")


def test_encoding_detection_rules():
    from app.filters.base import UnsupportedEncodingError, read_text_upload

    # binary content is refused, never decoded as Windows-1252
    for raw in (b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\xe9", bytes(range(1, 9)) * 4 + b"\xe9"):
        try:
            read_text_upload(raw)
        except UnsupportedEncodingError as exc:
            assert "binary" in str(exc), exc
        else:
            raise AssertionError(f"binary bytes were accepted as text: {raw!r}")
    # a UTF-16 file with a BOM is read as UTF-16
    assert read_text_upload("﻿café".encode("utf-16-le"), sig=True) == ("café", "utf-16")
    # an HTML file declaring windows-1251 is read as such and re-declared UTF-8 on export
    html = '<html><head><meta charset="windows-1251"></head><body><p>Привет</p></body></html>'
    raw = html.encode("cp1251")
    parsed = get_filter("html").parse(raw)
    assert [u.text for u in parsed.units] == ["Привет"], parsed.units
    assert parsed.skeleton.get("source_encoding") == "cp1251", parsed.skeleton
    # B-bytes: untouched = as uploaded; a translation that fits keeps windows-1251 (and
    # its declaration); one that does not is UTF-8 and re-declared UTF-8
    assert get_filter("html").build(raw, {}) == raw
    out = get_filter("html").build(raw, {"0": "Пока"})
    assert out == raw.replace("Привет".encode("cp1251"), "Пока".encode("cp1251")), out
    out = get_filter("html").build(raw, {"0": "こんにちは"}).decode("utf-8")
    assert '<meta charset="UTF-8">' in out and "<p>こんにちは</p>" in out, out
    # a PO file whose header declares CP1251: units read right, header says UTF-8 after
    po = (
        'msgid ""\nmsgstr ""\n"Content-Type: text/plain; charset=CP1251\\n"\n\n'
        'msgid "Привет"\nmsgstr ""\n'
    ).encode("cp1251")
    assert [u.text for u in get_filter("po").parse(po).units] == ["Привет"]
    # B-bytes: build() keeps CP1251 bytes and label; the localized export is UTF-8 and
    # says so (decision 9)
    out = get_filter("po").build(po, {"0": "Hallo"}).decode("cp1251")
    assert "charset=CP1251" in out and 'msgid "Привет"' in out, out
    out = get_filter("po").build_localized(po, {"0": "Hallo"}, "de-DE").decode("utf-8")
    assert "charset=UTF-8" in out and "CP1251" not in out and 'msgid "Привет"' in out, out
    # an SVG XML declaration is rewritten the same way
    svg = (
        b'<?xml version="1.0" encoding="windows-1252"?><svg xmlns="http://www.w3.org/2000/svg">'
        b"<text>caf\xe9</text></svg>"
    )
    assert get_filter("image").build(svg, {}) == svg  # B-bytes: untouched = as uploaded
    out = get_filter("image").build(svg, {"0": "café!"}).decode("utf-8")
    assert 'encoding="UTF-8"' in out and "café" in out, out
    # a UTF-8 file is never rewritten (its declaration and bytes stay as they were)
    utf8 = '<html><head><meta charset="utf-8"></head><body><p>café</p></body></html>'.encode()
    assert get_filter("html").build(utf8, {}) == utf8
    print("  [detect] binary refused, UTF-16 BOM read, declared charsets read + re-declared OK")


# A Windows-1252 subtitle file with accented letters, an en dash and a euro sign (all
# bytes 0x80-0xFF that UTF-8 cannot read), CRLF line ends as Notepad writes them.
SRC_LINES = ["Café crème – naïve, 5 € déjà vu", "Über die Straße"]
SRT_1252 = (
    "1\r\n00:00:01,000 --> 00:00:02,000\r\n"
    + SRC_LINES[0]
    + "\r\n\r\n2\r\n00:00:03,000 --> 00:00:04,000\r\n"
    + SRC_LINES[1]
    + "\r\n"
).encode("cp1252")
SRT_BAD = b"1\r\n00:00:01,000 --> 00:00:02,000\r\ncaf\x81\r\n"
LOC = "de-DE"


class _Env:
    pid = 0
    api_raw = ""
    tmp = ""


def _surface_env():
    if _Env.pid:
        return _Env
    import tempfile

    from sqlmodel import Session

    from app.db import engine, init_db
    from app.models import Project, Role, TargetLocale
    from app.services import apikeys, security

    init_db()
    with Session(engine) as s:
        p = Project(name="enc509", org_id=1, source_locale="en-US", default_provider="mock")
        s.add(p)
        s.commit()
        s.refresh(p)
        s.add(TargetLocale(project_id=p.id, locale=LOC))
        s.commit()
        _Env.pid = p.id
    user = security.create_user("enc509@x.test", "enc509", Role.ADMIN, "enc509-pw-123456")
    _row, _Env.api_raw = apikeys.create_key(user.id, "enc509", scopes="api docs admin")
    _Env.tmp = tempfile.mkdtemp(prefix="loctms_enc509_")
    return _Env


def _sources(doc_id: int) -> list[str]:
    from sqlmodel import Session, select

    from app.db import engine
    from app.models import Document, Segment

    with Session(engine) as s:
        doc = s.get(Document, doc_id)
        assert doc.skeleton.get("source_encoding") == "cp1252", doc.skeleton
        rows = s.exec(
            select(Segment)
            .where(Segment.document_id == doc_id, Segment.target_locale == LOC)
            .order_by(Segment.order_index)
        ).all()
        return [r.source_text for r in rows]


def _translate(doc_id: int) -> None:
    from tests._cli_runner import run_cli

    code, out, err = run_cli(["doc", "translate", str(doc_id), "--locale", LOC])
    assert code == 0, (code, out, err)


def _assert_export(surface: str, data: bytes) -> None:
    # B-bytes (2026-10-11, decision 1): the German mock translation fits Windows-1252,
    # so the export comes back in the upload's own code page (strict: raises otherwise)
    text = data.decode("cp1252")
    assert "�" not in text, (surface, text)
    for line in SRC_LINES:
        assert f"[{LOC}] {line}" in text, (surface, text)
    assert "00:00:03,000 --> 00:00:04,000" in text, (surface, text)


def test_cp1252_srt_translates_on_every_surface():
    """2026-10-06: a Windows-1252 .srt is converted on upload and translates with
    the right characters on web, API, CLI and MCP; the export keeps its code page. A file that is
    not readable at all is refused with the same clear message on every surface."""
    import re
    from urllib.parse import unquote

    from fastapi.testclient import TestClient

    from app.main import app
    from tests._cli_runner import run_cli
    from tests._csrf import csrf_for

    env = _surface_env()
    client = TestClient(app, raise_server_exceptions=False, follow_redirects=False)
    hdr = {"X-Loc-Token": env.api_raw}  # pregate: allow-secret throwaway test key

    # --- API ------------------------------------------------------------------
    r = client.post(
        "/api/documents",
        headers=hdr,
        data={"project_id": str(env.pid)},
        files={"file": ("api.srt", SRT_1252, "application/x-subrip")},
    )
    assert r.status_code in (200, 201), (r.status_code, r.text)
    api_doc = r.json()["document_id"]
    assert _sources(api_doc) == SRC_LINES, _sources(api_doc)
    _translate(api_doc)
    r = client.get(f"/api/documents/{api_doc}/download", headers=hdr, params={"locale": LOC})
    assert r.status_code == 200, (r.status_code, r.text)
    _assert_export("api", r.content)
    r = client.post(
        "/api/documents",
        headers=hdr,
        data={"project_id": str(env.pid)},
        files={"file": ("bad.srt", SRT_BAD, "application/x-subrip")},
    )
    assert r.status_code == 422, (r.status_code, r.text)
    assert "re-save the file as UTF-8" in r.json()["error"], r.text
    assert "corrupt" not in r.json()["error"], r.text
    print("  [api] cp1252 .srt converted, translated, exported as UTF-8; unreadable -> 422 OK")

    # --- web ------------------------------------------------------------------
    web = TestClient(app, raise_server_exceptions=False, follow_redirects=False)
    csrf_for(web)
    r = web.post(
        f"/projects/{env.pid}/documents",
        data={"mode": "segmented"},
        files={"file": ("web.srt", SRT_1252, "application/x-subrip")},
    )
    assert r.status_code == 303, (r.status_code, r.text[:300])
    m = re.match(r"^/documents/(\d+)$", r.headers["location"])
    assert m, r.headers["location"]
    web_doc = int(m.group(1))
    assert _sources(web_doc) == SRC_LINES, _sources(web_doc)
    _translate(web_doc)
    r = web.get(f"/documents/{web_doc}/export", params={"locale": LOC})
    assert r.status_code == 200, (r.status_code, r.text[:300])
    _assert_export("web", r.content)
    r = web.post(
        f"/projects/{env.pid}/documents",
        data={"mode": "segmented"},
        files={"file": ("bad.srt", SRT_BAD, "application/x-subrip")},
    )
    assert r.status_code == 303, r.status_code
    assert "re-save the file as UTF-8" in unquote(r.headers["location"]), r.headers["location"]
    print("  [web] cp1252 .srt converted, translated, exported as UTF-8; unreadable refused OK")

    # --- CLI ------------------------------------------------------------------
    src = os.path.join(env.tmp, "cli.srt")
    with open(src, "wb") as fh:
        fh.write(SRT_1252)
    code, out, err = run_cli(["doc", "upload", str(env.pid), src])
    assert code == 0, (code, out, err)
    cli_doc = int(re.search(r"Uploaded #(\d+)", out).group(1))
    assert _sources(cli_doc) == SRC_LINES, _sources(cli_doc)
    _translate(cli_doc)
    dest = os.path.join(env.tmp, "cli.de.srt")
    code, out, err = run_cli(["doc", "export", str(cli_doc), "--locale", LOC, "-o", dest])
    assert code == 0, (code, out, err)
    with open(dest, "rb") as fh:
        _assert_export("cli", fh.read())
    bad = os.path.join(env.tmp, "bad.srt")
    with open(bad, "wb") as fh:
        fh.write(SRT_BAD)
    code, out, err = run_cli(["doc", "upload", str(env.pid), bad])
    assert code != 0 and "re-save the file as UTF-8" in (out + err), (code, out, err)
    print("  [cli] cp1252 .srt converted, translated, exported as UTF-8; unreadable refused OK")

    # --- MCP (the stdio server, its HTTP calls answered in-process) -----------
    import asyncio
    import json
    import pathlib

    import httpx

    sys.path.insert(0, os.path.join(_ROOT, "integrations", "mcp-server"))
    import server as mcp_server

    from tests._mcp_plan import aconfirmed

    mcp_server._allow_local_file_reads(True)
    mcp_server.BASE_URL = "http://testserver"
    mcp_server.TOKEN = env.api_raw  # pregate: allow-secret throwaway test key
    mcp_server.OUTPUT_DIR = pathlib.Path(env.tmp)
    mcp_server._client = lambda: httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), timeout=60
    )
    mcp_src = os.path.join(env.tmp, "mcp.srt")
    with open(mcp_src, "wb") as fh:
        fh.write(SRT_1252)
    up = json.loads(
        asyncio.run(
            aconfirmed(  # #757: plan first, then confirm with its plan_id
                mcp_server,
                "submit_translation",
                {"file_path": mcp_src, "targets": [], "project_id": env.pid, "confirm": True},
            )
        )
    )
    mcp_doc = int(up["document_id"])
    assert _sources(mcp_doc) == SRC_LINES, _sources(mcp_doc)
    _translate(mcp_doc)
    got = json.loads(
        asyncio.run(
            mcp_server.call_tool_unvalidated("get_result", {"document_id": mcp_doc, "locale": LOC})
        )
    )
    with open(got["path"], "rb") as fh:
        _assert_export("mcp", fh.read())
    try:
        asyncio.run(
            aconfirmed(  # #757: plan first, then confirm with its plan_id
                mcp_server,
                "submit_translation",
                {"file_path": bad, "targets": [], "project_id": env.pid, "confirm": True},
            )
        )
    except mcp_server.ToolError as exc:
        assert "re-save the file as UTF-8" in str(exc), exc
    else:
        raise AssertionError("MCP accepted an unreadable file")
    print("  [mcp] cp1252 .srt converted, translated, exported as UTF-8; unreadable refused OK")


def test_stored_originals_stay_exportable():
    """Export re-reads a stored HTML original to check its unit list: a pre-#509 upload
    (UTF-8 `replace` units, no source_encoding) is re-read that way and never refused."""
    bad = b"<p>caf\x81 au lait</p>"
    assert get_filter("html").parse(bad, lenient=True).units
    try:
        get_filter("html").parse(bad)
    except ValueError:
        pass
    else:
        raise AssertionError("a fresh unreadable HTML upload must be refused")
    print("  [export] stored pre-#509 HTML originals stay readable OK")


def test_markdown_line_endings_677():
    """#677: Markdown build kept neither the final newline, CRLF, nor the two trailing spaces
    of a hard line break on a translated line."""
    md = get_filter("md")
    for src in (
        b"# Title\n\nHello world.\n",
        b"- a\n- b\n\n",
        b"Text\n\n```\ncode\n```\n",
        b"No final newline",
        b"Line one  \r\nLine two\r\n",
    ):
        assert md.build(src, {}) == src, src
    out = md.build(b"Line one  \r\nLine two\r\n", {"0": "Zeile eins", "1": "Zeile zwei"})
    assert out == b"Zeile eins  \r\nZeile zwei\r\n", out
    out = md.build(b"# Title\n\n> Quote here.\n", {"0": "Titel", "1": "Zitat."})
    assert out == b"# Titel\n\n> Zitat.\n", out
    print("  [677] markdown keeps final newline, CRLF and hard-break spaces OK")


def test_subtitle_blank_line_713():
    """#713: a blank line ends an SRT / WebVTT cue, so a translation with an empty line split
    the cue (players dropped the text after it, re-import read one cue more)."""
    f = get_filter("srt")
    srt = (
        b"1\n00:00:01,000 --> 00:00:02,000\nHello there.\n\n"
        b"2\n00:00:03,000 --> 00:00:04,000\nSecond line.\n"
    )
    vtt = (
        b"WEBVTT\n\n00:00:01.000 --> 00:00:02.000\nHello there.\n\n"
        b"00:00:03.000 --> 00:00:04.000\nSecond line.\n"
    )
    texts = {"0": "Bonjour.\n\nDeuxieme partie.\r\n \n", "1": "Ligne deux."}
    for src in (srt, vtt):
        out = f.build_localized(src, texts, "fr-FR")
        assert b"Bonjour.\nDeuxieme partie.\n\n" in out, out
        units = [u.text for u in f.parse(out).units]
        assert units == ["Bonjour.\nDeuxieme partie.", "Ligne deux."], units
        # a translation with no text left keeps the source cue; no-op build unchanged
        assert f.parse(f.build(src, {"0": "\n\n"})).units[0].text == "Hello there."
        assert f.build(src, {}) == src
    print("  [713] srt/vtt: a blank line in a translation never splits the cue OK")


def main():
    test_subtitle_blank_line_713()
    test_markdown_line_endings_677()
    test_svg_entities_round_trip()
    test_xliff_text_after_phantom_stays_in_order()
    test_docx_range_markers_stay_between_runs()
    test_text_cp1252_converted_and_endings_preserved()
    test_other_text_formats_convert_cp1252()
    test_encoding_detection_rules()
    test_cp1252_srt_translates_on_every_surface()
    test_stored_originals_stay_exportable()
    print("FILTER ROUNDTRIP 509 OK")


if __name__ == "__main__":
    main()
