"""Chunk 9 verification — additional file-format filters, offline.

For each new format we assert a faithful round-trip:
  parse() -> N translatable units -> build() with a translation for each ->
  re-parse the rebuilt bytes -> translations are present AND the non-translatable
  structure (tags, keys, code blocks, timecodes, msgids, xliff sources) survives.

Then an end-to-end check that the app actually accepts an HTML upload and still
rejects a genuinely unsupported extension.
"""

import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(_ROOT)
sys.path.insert(0, _ROOT)
from tests._bootstrap import init_env

init_env("loctms_test_")

from fastapi.testclient import TestClient

from app.db import init_db
from app.filters import get_filter, supported_exts
from app.main import app


def roundtrip(fmt: str, data: bytes):
    """parse -> translate every unit ('[FR] ' + src) -> build -> re-parse."""
    filt = get_filter(fmt)
    assert filt is not None, f"no filter for {fmt}"
    parsed = filt.parse(data)
    assert parsed.units, f"{fmt}: no units parsed"
    mapping = {str(u.p_index): "[FR] " + u.text for u in parsed.units}
    rebuilt = filt.build(data, mapping)
    reparsed = filt.parse(rebuilt)
    return parsed, rebuilt, reparsed


def assert_translated(fmt, parsed, reparsed):
    """Re-parsed source units should equal the injected translations (for formats
    whose translatable unit is what we re-extract)."""
    got = [u.text for u in reparsed.units]
    want = ["[FR] " + u.text for u in parsed.units]
    assert got == want, f"{fmt}: round-trip mismatch\n got={got}\n want={want}"


def main() -> None:
    init_db()

    # --- HTML ---
    html = (
        b"<html><head><title>Hi</title><style>.x{color:red}</style></head>"
        b"<body><h1>Hello</h1><p>World <b>bold</b> text.</p>"
        b'<script>var x="keep";</script></body></html>'
    )
    p, rebuilt, rp = roundtrip("html", html)
    assert_translated("html", p, rp)
    assert b"<style>.x{color:red}</style>" in rebuilt, "html: style content altered"
    assert b'var x="keep"' in rebuilt, "html: script content altered"
    assert b"<b>" in rebuilt and b"<h1>" in rebuilt, "html: tags lost"
    print(f"  html: {len(p.units)} units, tags/script/style preserved")

    # extension alias resolves
    assert get_filter("htm") is get_filter("html"), "htm alias not resolved"

    # --- JSON ---
    js = (
        b'{"greeting": "Hello world", "nav": {"home": "Home", "about": "About us"},'
        b' "version": 2, "enabled": true}'
    )
    p, rebuilt, rp = roundtrip("json", js)
    assert_translated("json", p, rp)
    import json as _json

    obj = _json.loads(rebuilt)
    assert obj["greeting"] == "[FR] Hello world"
    assert obj["nav"]["about"] == "[FR] About us"
    assert obj["version"] == 2 and obj["enabled"] is True, "json: non-strings altered"
    assert set(obj["nav"].keys()) == {"home", "about"}, "json: keys altered"
    print(f"  json: {len(p.units)} string values, keys/numbers/bools preserved")

    # --- Markdown ---
    md = (
        b"# Title Here\n\nSome intro paragraph.\n\n- First item\n- Second item\n\n"
        b'```python\ncode = "do not translate"\n```\n\nDone.\n'
    )
    p, rebuilt, rp = roundtrip("md", md)
    assert_translated("md", p, rp)
    assert b'code = "do not translate"' in rebuilt, "md: code fence translated"
    assert b"# [FR] Title Here" in rebuilt, "md: heading prefix lost"
    assert b"- [FR] First item" in rebuilt, "md: list prefix lost"
    print(f"  md: {len(p.units)} prose lines, code fence + markup prefixes preserved")

    # --- plain text ---
    txt = b"First line.\n\n  Indented line.\nThird line.\n"
    p, rebuilt, rp = roundtrip("txt", txt)
    assert_translated("txt", p, rp)
    assert b"  [FR] Indented line." in rebuilt, "txt: indentation lost"
    print(f"  txt: {len(p.units)} lines, blank lines + indentation preserved")

    # --- gettext PO ---
    po = (
        b'# Translator comment\nmsgid ""\nmsgstr "Content-Type: text/plain; charset=UTF-8\\n"\n\n'
        b'#: app.py:10\nmsgid "Hello"\nmsgstr ""\n\nmsgid "Goodbye"\nmsgstr ""\n'
    )
    filt = get_filter("po")
    parsed = filt.parse(po)
    assert [u.text for u in parsed.units] == ["Hello", "Goodbye"], "po: header not skipped"
    rebuilt = filt.build(po, {"0": "[FR] Hello", "1": "[FR] Goodbye"})
    assert b'msgstr "[FR] Hello"' in rebuilt, "po: translation not written to msgstr"
    assert b'msgstr "[FR] Goodbye"' in rebuilt
    assert b"Content-Type: text/plain" in rebuilt, "po: header metadata lost"
    assert b"#: app.py:10" in rebuilt, "po: source-reference comment lost"
    assert b'msgid "Hello"' in rebuilt, "po: msgid altered"
    print("  po: 2 entries, header + comments + msgids preserved")

    # --- SRT subtitles ---
    srt = (
        b"1\n00:00:01,000 --> 00:00:04,000\nHello world.\n\n"
        b"2\n00:00:05,000 --> 00:00:07,000\nSecond line.\nContinued.\n"
    )
    p, rebuilt, rp = roundtrip("srt", srt)
    assert_translated("srt", p, rp)
    assert b"00:00:01,000 --> 00:00:04,000" in rebuilt, "srt: timecode altered"
    assert b"[FR] Second line.\nContinued." in rebuilt, "srt: multi-line cue not preserved"
    print(f"  srt: {len(p.units)} cues, indices + timecodes preserved")

    # --- XLIFF ---
    xlf = (
        b'<?xml version="1.0"?>\n'
        b'<xliff version="1.2" xmlns="urn:oasis:names:tc:xliff:document:1.2">\n'
        b' <file source-language="en" target-language="fr" datatype="plaintext" original="x">\n'
        b"  <body>\n"
        b'   <trans-unit id="1"><source>Hello</source></trans-unit>\n'
        b'   <trans-unit id="2"><source>Goodbye</source><target>old</target></trans-unit>\n'
        b"  </body>\n </file>\n</xliff>\n"
    )
    filt = get_filter("xliff")
    parsed = filt.parse(xlf)
    assert [u.text for u in parsed.units] == ["Hello", "Goodbye"], "xliff: sources not parsed"
    rebuilt = filt.build(xlf, {"0": "[FR] Hello", "1": "[FR] Goodbye"})
    assert b"[FR] Hello" in rebuilt and b"[FR] Goodbye" in rebuilt, "xliff: target not written"
    assert b"<source>Hello</source>" in rebuilt, "xliff: source altered"
    # re-parse to confirm it's still valid XLIFF and a target now carries the translation
    from lxml import etree

    root = etree.fromstring(rebuilt)
    targets = [t.text for t in root.iter() if etree.QName(t).localname == "target"]
    assert "[FR] Hello" in targets and "[FR] Goodbye" in targets, targets
    assert get_filter("xlf") is filt, "xlf alias not resolved"
    print("  xliff: 2 trans-units, sources preserved, targets written/created")

    # --- XLIFF inline-element fidelity (Phase 5 chunk 6) ---
    # <g> (paired, wraps content) + <x> (standalone) must survive the MT pipeline.
    xlf2 = (
        b'<?xml version="1.0"?>\n'
        b'<xliff version="1.2" xmlns="urn:oasis:names:tc:xliff:document:1.2">\n'
        b' <file source-language="en" target-language="fr">\n  <body>\n'
        b'   <trans-unit id="1"><source>Click <g id="1" ctype="bold">Submit</g>'
        b' now<x id="2"/>.</source></trans-unit>\n'
        b'   <trans-unit id="2"><source>Plain sentence.</source></trans-unit>\n'
        b"  </body>\n </file>\n</xliff>\n"
    )
    from app.services.segment import find_placeholders, strip_placeholders

    p2 = filt.parse(xlf2)
    # parse yields TOKENIZED text (inline elements replaced by placeholder tokens)
    inline_u = p2.units[0]
    assert strip_placeholders(inline_u.text) == "Click Submit now.", repr(inline_u.text)
    assert len(find_placeholders(inline_u.text)) == 3, "expected ⟦g1⟧⟦/g1⟧ + ⟦x1⟧"
    assert set(inline_u.inline_map.keys()) == {"1", "2"}, inline_u.inline_map.keys()
    # plain trans-unit still yields plain text with no tokens
    assert p2.units[1].text == "Plain sentence." and not p2.units[1].inline_map

    # build reconstructs the inline elements in <target> with original attrs verbatim
    rebuilt2 = filt.build(xlf2, {str(u.p_index): "[FR] " + u.text for u in p2.units})
    root2 = etree.fromstring(rebuilt2)
    tgt = next(
        t
        for t in root2.iter()
        if etree.QName(t).localname == "target" and t.getparent().get("id") == "1"
    )
    # itertext (flattened) equals the translated readable text
    assert "".join(tgt.itertext()) == "[FR] Click Submit now.", repr("".join(tgt.itertext()))
    # actual child elements exist with their original attributes preserved
    kids = [(etree.QName(c).localname, dict(c.attrib)) for c in tgt]
    assert ("g", {"id": "1", "ctype": "bold"}) in kids, kids
    assert ("x", {"id": "2"}) in kids, kids
    g_el = next(c for c in tgt if etree.QName(c).localname == "g")
    assert g_el.text == "Submit", f"g content not preserved: {g_el.text!r}"
    print(f"  xliff inline: <g>/<x> tokenized on parse, rebuilt in <target> ({len(kids)} elems)")

    # --- XLIFF reconstruction robustness (Phase 5 chunk 6 fix) ---
    # The <target> is not trusted to carry the same tokens as the source. build()
    # must NEVER emit a literal ⟦…⟧ into <target> and must NEVER duplicate an id
    # attribute (schema-invalid). Surrounding translated text always survives.
    def target_for(rebuilt, unit_id):
        root = etree.fromstring(rebuilt)
        return next(
            t
            for t in root.iter()
            if etree.QName(t).localname == "target" and t.getparent().get("id") == unit_id
        )

    # (a) tagless target: translator dropped all inline tokens. Text survives;
    #     the inline elements are simply absent; no literal token leaks.
    rb_tagless = filt.build(xlf2, {"0": "Cliquez Envoyer maintenant.", "1": "[FR] Plain sentence."})
    tt = target_for(rb_tagless, "1")  # id="1" is the inline unit
    flat = "".join(tt.itertext())
    assert flat == "Cliquez Envoyer maintenant.", repr(flat)
    assert "⟦" not in flat and "⟧" not in flat, "literal token leaked into tagless target"

    # (b) unknown token id in target -> dropped, surrounding text kept, no literal
    rb_unknown = filt.build(xlf2, {"0": "A ⟦g9⟧X⟦/g9⟧ B ⟦x5⟧."})
    tu2 = target_for(rb_unknown, "1")
    ser_u = etree.tostring(tu2, encoding="unicode")
    assert "⟦" not in ser_u, f"unknown token leaked literally: {ser_u}"
    assert "".join(tu2.itertext()) == "A X B .", repr("".join(tu2.itertext()))

    # (c) duplicate id emitted twice -> materialized at most once (no dup id attr)
    rb_dup = filt.build(xlf2, {"0": "⟦g1⟧A⟦/g1⟧ ⟦g1⟧B⟦/g1⟧ ⟦x2⟧⟦x2⟧"})
    td = target_for(rb_dup, "1")
    ser_d = etree.tostring(td, encoding="unicode")
    assert "⟦" not in ser_d, "literal token leaked in duplicate case"
    g_count = sum(1 for c in td if etree.QName(c).localname == "g")
    x_count = sum(1 for c in td if etree.QName(c).localname == "x")
    assert g_count == 1, f"duplicate <g id> materialized {g_count}x (schema-invalid)"
    assert x_count == 1, f"duplicate <x id> materialized {x_count}x (schema-invalid)"
    print(
        "  xliff robustness: tagless/unknown/duplicate tokens degrade safely (no leak, no dup id)"
    )

    # --- segmentation must not split a paired token span across segments ---
    # A sentence boundary INSIDE a ⟦gN⟧…⟦/gN⟧ pair must be suppressed, or the open
    # lands in one segment and its close in another (orphaned pair, wrong reflow).
    from app.services.segment import find_placeholders, split_sentences

    paired = "⟦g1⟧First sentence. Second half⟦/g1⟧ Third one is here."
    segs = split_sentences(paired)
    for seg in segs:
        phs = find_placeholders(seg)
        # every paired segment carries balanced open/close tokens (no orphan). A
        # close token is ⟦/gN⟧ (char after ⟦ is '/'); everything else is an open or
        # point token. This span uses only paired ⟦g1⟧…⟦/g1⟧ so counts must match.
        closes = [t for t in phs if t[1] == "/"]
        opens = [t for t in phs if t[1] != "/"]
        assert len(opens) == len(closes), f"orphaned token in segment: {seg!r} -> {phs}"
    joined = "".join(segs)
    assert joined.count("⟦g1⟧") == 1 and joined.count("⟦/g1⟧") == 1
    # the internal ". " inside the pair did NOT create a split there
    assert any("First sentence. Second half" in s for s in segs), segs
    print(f"  segmentation: paired-token span kept intact across {len(segs)} segment(s)")

    # --- XLSX (Excel) ---
    import io as _io

    from openpyxl import Workbook, load_workbook

    wb = Workbook()
    ws = wb.active
    ws.title = "Sheet1"
    ws["A1"] = "Hello world"  # translatable string
    ws["A2"] = 42  # number -> untouched
    ws["A3"] = True  # boolean -> untouched
    ws["A4"] = "=SUM(A2:A2)"  # formula -> untouched, not even counted
    ws["B1"] = "###"  # no alnum -> not a unit, preserved unchanged
    ws2 = wb.create_sheet("Sheet2")
    ws2["A1"] = "Second sheet text"  # translatable, on another sheet
    xbuf = _io.BytesIO()
    wb.save(xbuf)
    xlsx_bytes = xbuf.getvalue()

    p, rebuilt, rp = roundtrip("xlsx", xlsx_bytes)
    assert [u.text for u in p.units] == ["Hello world", "Second sheet text"], (
        f"xlsx: wrong units extracted: {[u.text for u in p.units]}"
    )
    assert_translated("xlsx", p, rp)

    wb_out = load_workbook(_io.BytesIO(rebuilt))
    s1, s2 = wb_out["Sheet1"], wb_out["Sheet2"]
    assert s1["A1"].value == "[FR] Hello world", s1["A1"].value
    assert s2["A1"].value == "[FR] Second sheet text", s2["A1"].value
    assert s1["A2"].value == 42 and isinstance(s1["A2"].value, int), "xlsx: number cell altered"
    assert s1["A3"].value is True, "xlsx: boolean cell altered"
    assert s1["A4"].value == "=SUM(A2:A2)", "xlsx: formula cell altered"
    assert s1["B1"].value == "###", "xlsx: symbol-only cell altered"
    assert [ws.title for ws in wb_out.worksheets] == ["Sheet1", "Sheet2"], (
        "xlsx: sheet order/names lost"
    )
    print(
        f"  xlsx: {len(p.units)} string cells translated; numbers/bools/formulas/symbols/sheets preserved"  # noqa: E501
    )

    # --- end-to-end: app accepts HTML upload, rejects unknown ext ---
    client = TestClient(app)
    r = client.post(
        "/projects",
        data={
            "name": "Fmt Test",
            "source_locale": "en-US",
            "target_locales": "fr-FR",
            "default_provider": "claude",
        },
        follow_redirects=False,
    )
    pid = int(r.headers["location"].rsplit("/", 1)[-1])

    r = client.post(
        f"/projects/{pid}/documents",
        data={"mode": "segmented"},
        files={"file": ("page.html", html, "text/html")},
        follow_redirects=False,
    )
    assert r.status_code == 303 and "/documents/" in r.headers["location"], r.text
    doc_id = int(r.headers["location"].rsplit("/", 1)[-1])
    page = client.get(f"/documents/{doc_id}?locale=fr-FR").text
    assert "Hello" in page, "html document segments not shown"
    print("  e2e: HTML uploaded + segmented through the app")

    # export round-trip: translate the segments, then download the rebuilt HTML
    from sqlmodel import Session, select

    from app.db import engine
    from app.models import Segment

    with Session(engine) as s:
        segs = sorted(
            s.exec(
                select(Segment).where(
                    Segment.document_id == doc_id, Segment.target_locale == "fr-FR"
                )
            ).all(),
            key=lambda x: x.order_index,
        )
        ids_texts = [(x.id, x.source_text) for x in segs]
    for sid, src in ids_texts:
        client.post(f"/segments/{sid}/save", data={"target_text": "[FR] " + src, "tte_ms": "0"})
    resp = client.get(f"/documents/{doc_id}/export?locale=fr-FR")
    assert resp.status_code == 200, resp.text
    out = resp.content
    assert b"[FR] Hello" in out, "html export: translation not injected"
    assert b"<h1>" in out and b'var x="keep"' in out, "html export: structure/script lost"
    print("  e2e: HTML exported — translations injected, tags/script preserved")

    r = client.post(
        f"/projects/{pid}/documents",
        data={"mode": "segmented"},
        files={"file": ("archive.zip", b"PK\x03\x04junk", "application/zip")},
        follow_redirects=False,
    )
    assert r.status_code == 303 and "err=format" in r.headers["location"], "zip should be rejected"
    print("  e2e: unsupported .zip rejected")

    # --- e2e: upload an XLSX, translate (mock), export, verify the rebuilt workbook ---
    r = client.post(
        f"/projects/{pid}/documents",
        data={"mode": "segmented"},
        files={
            "file": (
                "book.xlsx",
                xlsx_bytes,
                "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            )
        },
        follow_redirects=False,
    )
    assert r.status_code == 303 and "/documents/" in r.headers["location"], r.text
    xdoc_id = int(r.headers["location"].rsplit("/", 1)[-1])
    page = client.get(f"/documents/{xdoc_id}?locale=fr-FR").text
    assert "Hello world" in page, "xlsx document segments not shown"
    print("  e2e: XLSX uploaded + segmented through the app")

    with Session(engine) as s:
        xsegs = sorted(
            s.exec(
                select(Segment).where(
                    Segment.document_id == xdoc_id, Segment.target_locale == "fr-FR"
                )
            ).all(),
            key=lambda x: x.order_index,
        )
        xids_texts = [(x.id, x.source_text) for x in xsegs]
    for sid, src in xids_texts:
        client.post(f"/segments/{sid}/save", data={"target_text": "[FR] " + src, "tte_ms": "0"})
    xresp = client.get(f"/documents/{xdoc_id}/export?locale=fr-FR")
    assert xresp.status_code == 200, xresp.text
    xout = xresp.content
    wb_e2e = load_workbook(_io.BytesIO(xout))
    assert wb_e2e["Sheet1"]["A1"].value == "[FR] Hello world", wb_e2e["Sheet1"]["A1"].value
    assert wb_e2e["Sheet2"]["A1"].value == "[FR] Second sheet text", wb_e2e["Sheet2"]["A1"].value
    assert wb_e2e["Sheet1"]["A2"].value == 42, "xlsx export: number cell altered"
    assert wb_e2e["Sheet1"]["A4"].value == "=SUM(A2:A2)", "xlsx export: formula cell altered"
    print("  e2e: XLSX exported — translations injected, non-string cells + structure preserved")

    print(f"  supported extensions: {', '.join(supported_exts())}")
    print("CHUNK 9 OK")


if __name__ == "__main__":
    main()
