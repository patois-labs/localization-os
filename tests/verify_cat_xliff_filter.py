"""Bilingual CAT XLIFF filters (Batch 5): Trados SDLXLIFF, memoQ MQXLIFF, Phrase MXLIFF,
and the XLIFF 1.2 hardening the plain .xlf filter shares (app/filters/cat_xliff.py).

  identity     a build with nothing to write returns the original bytes (BOM, CRLF,
               single-quoted attributes and character references included).
  segments     SDLXLIFF seg-source mrks are units; the target mirrors them by mid, a missing
               target is created from seg-source (inline wrappers kept), conf becomes
               Translated (an sdl:seg is added when missing).
  protection   locked / approved / translate=no / confirmed-with-target units are never
               parsed and never overwritten, even when a stored mapping names them.
  untranslated a unit export marks as source fallback stays EMPTY in the vendor file.
  inline       <g>/<x>/<bpt>/<ph> come back with the source's own bytes; MXLIFF {1>..<1}
               and {2} marks are protected tokens.
  encodings    UTF-8 with BOM, CRLF, UTF-16 LE with BOM round-trip in their own encoding;
               a declared legacy encoding is refused.
  hostile      DOCTYPE, wrong namespace, XLIFF 2.0 for a vendor fmt, the markup budget and
               the segment cap all refuse before any unit is stored.
  export       ingest -> partial translation -> export through services/export.

Hermetic + offline. Final line: "CAT XLIFF FILTER OK".
"""

import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(_ROOT)
sys.path.insert(0, _ROOT)
from tests._bootstrap import init_env

init_env("loctms_catxliff_")

from lxml import etree

from app.filters import get_filter, supported_exts
from app.filters.cat_xliff import SDL_NS, SEG_BASE

FIX = os.path.join(_ROOT, "tests", "fixtures", "formats", "catxliff")


def _fx(name: str) -> bytes:
    with open(os.path.join(FIX, name), "rb") as fh:
        return fh.read().replace(b"\r\n", b"\n")  # autocrlf checkouts: test LF bytes


SDL = _fx("sample.sdlxliff")
MQ = _fx("sample.mqxliff")
MX = _fx("sample.mxliff")
XLF = _fx("protected.xlf")


def _units(fmt, raw):
    return {u.p_index: u.text for u in get_filter(fmt).parse(raw).units}


def _tu(out: bytes, tu_id: str) -> bytes:
    """The bytes of one trans-unit element (id quoted either way)."""
    for q in (b'"', b"'"):
        start = out.find(b"<trans-unit id=" + q + tu_id.encode() + q)
        if start >= 0:
            return out[start : out.index(b"</trans-unit>", start) + len(b"</trans-unit>")]
    raise AssertionError(f"trans-unit {tu_id} not found")


def _changed_regions_only(before: bytes, after: bytes, allowed: list[bytes]) -> None:
    """Every byte outside the trans-units we wrote is identical."""
    a, b = before, after
    for tu in allowed:
        a = a.replace(_tu(before, tu.decode()), b"<TU/>")
        b = b.replace(_tu(after, tu.decode()), b"<TU/>")
    assert a == b, "bytes outside the written trans-units changed"


def test_registration():
    exts = set(supported_exts())
    for ext in ("sdlxliff", "sdlxlf", "mqxliff", "mxliff", "xlf", "xliff"):
        assert ext in exts, ext
    assert get_filter("sdlxlf").fmt == "sdlxliff"
    print("  [registry] sdlxliff/sdlxlf/mqxliff/mxliff registered OK")


def test_identity_round_trip():
    for fmt, raw in (("sdlxliff", SDL), ("mqxliff", MQ), ("mxliff", MX)):
        f = get_filter(fmt)
        assert f.build(raw, {}) == raw, fmt
        assert f.build_localized(raw, {}, "de-DE") == raw, fmt  # target-language already set
        # every key present but each flagged as source fallback: still byte-identical
        keys = {str(k) for k in _units(fmt, raw)}
        assert f.build(raw, dict.fromkeys(keys, "x"), fallback_keys=keys) == raw, fmt
    print("  [identity] no-change build returns the original bytes OK")


def test_sdlxliff_segments_and_status():
    units = _units("sdlxliff", SDL)
    assert units == {
        SEG_BASE + 0: "Open the ⟦g1⟧Settings⟦/g1⟧ menu.",
        SEG_BASE + 1: "Then click Save.",
        SEG_BASE + 2: "Line one⟦p1⟧line two & more text.",
        SEG_BASE + 6: "Wrapped segment.",
    }, units
    mapping = {
        str(SEG_BASE + 0): "⟦g1⟧Einstellungen⟦/g1⟧-Menue oeffnen.",
        str(SEG_BASE + 1): "Dann auf Speichern klicken.",
        str(SEG_BASE + 2): "Zeile eins⟦x1⟧Zeile zwei & mehr.",
        str(SEG_BASE + 6): "Umschlossenes Segment.",
        # protected slots named by a stale mapping must still never be written
        str(SEG_BASE + 3): "HACK locked",
        str(SEG_BASE + 4): "HACK approved",
        str(SEG_BASE + 5): "HACK confirmed",
    }
    out = get_filter("sdlxliff").build(SDL, mapping)
    etree.fromstring(out)
    assert b"HACK" not in out
    tu1 = _tu(out, "a1b2c3d4-0001")
    assert (
        b'<target><mrk mtype="seg" mid="1"><g id="pt1">Einstellungen</g>-Menue oeffnen.</mrk> '
        b'<mrk mtype="seg" mid="2">Dann auf Speichern klicken.</mrk></target>'
    ) in tu1, tu1
    assert b'<sdl:seg id="1" conf="Translated"/><sdl:seg id="2" conf="Translated"/>' in tu1, tu1
    # missing <target> created after seg-source, mirroring it; text escaped once
    tu2 = _tu(out, "a1b2c3d4-0002")
    assert tu2.startswith(b"<trans-unit id='a1b2c3d4-0002'>"), tu2  # quoting kept
    assert b'<source>Line one<x id="ph2"/>line two &amp; more&#xA0;text.</source>' in tu2
    assert (
        b'</seg-source><target><mrk mtype="seg" mid="3">Zeile eins<x id="ph2"/>Zeile zwei '
        b"&amp; mehr.</mrk></target><sdl:seg-defs>"
    ) in tu2, tu2
    # a target-less unit whose seg sits inside <g>: wrapper mirrored, seg-defs added
    tu6 = _tu(out, "a1b2c3d4-0006")
    assert (
        b'</seg-source><target><g id="pt1"><mrk mtype="seg" mid="7">Umschlossenes Segment.'
        b'</mrk></g></target><sdl:seg-defs><sdl:seg id="7" conf="Translated"/></sdl:seg-defs>'
        b"</trans-unit>"
    ) in tu6, tu6
    # locked / approved / confirmed / translate=no units are byte-identical
    for tid in ("a1b2c3d4-0003", "a1b2c3d4-0004", "a1b2c3d4-0005"):
        assert _tu(out, tid) == _tu(SDL, tid), tid
    _changed_regions_only(SDL, out, [b"a1b2c3d4-0001", b"a1b2c3d4-0002", b"a1b2c3d4-0006"])
    print("  [sdlxliff] mirrored seg mrks, conf=Translated, protected segments untouched OK")


def test_sdlxliff_partial_and_mismatch():
    # only segment 2 translated: segment 1 stays empty (never source text), conf unchanged
    keys = {str(SEG_BASE + 0)}
    out = get_filter("sdlxliff").build(
        SDL, {str(SEG_BASE + 0): "Open the menu.", str(SEG_BASE + 1): "Dann."}, fallback_keys=keys
    )
    tu1 = _tu(out, "a1b2c3d4-0001")
    assert b'<mrk mtype="seg" mid="1"/> <mrk mtype="seg" mid="2">Dann.</mrk>' in tu1, tu1
    assert b'<sdl:seg id="1" conf="Draft"/><sdl:seg id="2" conf="Translated"/>' in tu1, tu1
    # a target whose segmentation does not match seg-source is refused, not corrupted
    bad = SDL.replace(b'<target><mrk mtype="seg" mid="1"/>', b'<target><mrk mtype="seg" mid="9"/>')
    try:
        get_filter("sdlxliff").build(bad, {str(SEG_BASE + 0): "x"})
    except ValueError as exc:
        assert "segmentation" in str(exc), exc
    else:
        raise AssertionError("a mismatched target segmentation must be refused")
    print("  [sdlxliff] untranslated segment stays empty; mismatched target refused OK")


def test_sdlxliff_inline_damage_is_safe():
    # unknown / duplicated tokens are dropped, an unclosed pair is closed: still well-formed
    out = get_filter("sdlxliff").build(SDL, {str(SEG_BASE + 0): "⟦g1⟧A ⟦g7⟧B⟦/g7⟧ ⟦g1⟧C"})
    tu1 = _tu(out, "a1b2c3d4-0001")
    assert b'<mrk mtype="seg" mid="1"><g id="pt1">A B C</g></mrk>' in tu1, tu1
    assert "⟦".encode() not in out
    print("  [sdlxliff] damaged inline tokens never leak or break the XML OK")


def test_encodings():
    f = get_filter("sdlxliff")
    m = {str(SEG_BASE + 1): "Dann übernehmen."}
    # UTF-8 BOM + CRLF line endings
    crlf = b"\xef\xbb\xbf" + SDL.replace(b"\n", b"\r\n")
    assert f.build(crlf, {}) == crlf
    out = f.build(crlf, m)
    assert out.startswith(b'\xef\xbb\xbf<?xml version="1.0" encoding="utf-8"?>'), out[:60]
    assert out.count(b"\r\n") == crlf.count(b"\r\n")
    assert "Dann übernehmen.".encode() in out
    # UTF-16 LE with BOM: written back as UTF-16 LE
    u16 = b"\xff\xfe" + SDL.decode("utf-8").replace('encoding="utf-8"', 'encoding="utf-16"').encode(
        "utf-16-le"
    )
    assert _units("sdlxliff", u16)[SEG_BASE + 1] == "Then click Save."
    out16 = f.build(u16, m)
    assert out16.startswith(b"\xff\xfe")
    assert "Dann übernehmen." in out16[2:].decode("utf-16-le")
    # declared legacy encoding refused
    latin = SDL.replace(b'encoding="utf-8"', b'encoding="iso-8859-1"')
    try:
        f.parse(latin)
    except ValueError as exc:
        assert "UTF-8" in str(exc), exc
    else:
        raise AssertionError("a declared legacy encoding must be refused")
    print("  [encodings] BOM, CRLF and UTF-16 kept; legacy encoding refused OK")


def test_mqxliff():
    units = _units("mqxliff", MQ)
    assert units == {
        0: "Press ⟦x1⟧Start⟦x2⟧ to begin.",
        4: "Use {0} files & ⟦p1⟧more.",  # #693: <ph id> moves freely; bpt/ept keep x
    }, units
    out = get_filter("mqxliff").build(
        MQ,
        {
            "0": "⟦x1⟧Start⟦x2⟧ druecken.",
            "4": "{0} Dateien & ⟦x1⟧mehr.",
            "1": "HACK",
            "2": "HACK",
            "3": "HACK",
        },
    )
    assert b"HACK" not in out
    tu1 = _tu(out, "1")
    assert b'mq:status="ManuallyConfirmed"' in tu1
    assert (
        b'<target xml:space="preserve"><bpt id="1" ctype="bold">{}</bpt>Start<ept id="1">{}</ept>'
        b" druecken.</target>"
    ) in tu1, tu1
    tu5 = _tu(out, "5")
    assert b'<ph id="2">&lt;br/&gt;</ph>mehr.</target>' in tu5, tu5
    for tid in ("2", "3", "4"):  # confirmed, locked, reviewer-confirmed: untouched
        assert _tu(out, tid) == _tu(MQ, tid), tid
    assert b"<mq:historical-unit><source>Old source.</source><target>Alte Zeile.</target>" in out
    _changed_regions_only(MQ, out, [b"1", b"5"])
    print("  [mqxliff] status ManuallyConfirmed, locked/reviewed/confirmed untouched OK")


def test_mxliff():
    units = _units("mxliff", MX)
    assert units == {
        0: "Click ⟦g1⟧Save⟦/g1⟧ to keep ⟦x2⟧ changes.",
        3: "Draft from MT.",
    }, units
    out = get_filter("mxliff").build(
        MX,
        {
            "0": "⟦x2⟧ Aenderungen mit ⟦g1⟧Speichern⟦/g1⟧ behalten.",
            "1": "HACK",
            "2": "HACK",
            "3": "Entwurf aus MT.",
        },
    )
    assert b"HACK" not in out
    tu0 = _tu(out, "seg0")
    assert b"<target>{2} Aenderungen mit {1>Speichern&lt;1} behalten.</target>" in tu0, tu0
    assert b'm:confirmed="0"' in tu0  # the Phrase workflow level is never guessed
    for tid in ("seg1", "seg2"):
        assert _tu(out, tid) == _tu(MX, tid), tid
    tu3 = _tu(out, "seg3")
    assert b"<target>Entwurf aus MT.</target>" in tu3
    assert (
        b'<alt-trans origin="machine-trans" match-quality="62"><target>Entwurf aus MT.</target>'
        in tu3
    )
    _changed_regions_only(MX, out, [b"seg0", b"seg3"])
    print("  [mxliff] text marks protected, locked/confirmed untouched OK")


def test_xliff12_hardening():
    f = get_filter("xlf")
    units = _units("xlf", XLF)
    assert units == {0: "Translate me.", 5: "Needs work."}, units
    # a mapping stored before the hardening named the protected units: never written
    out = f.build(XLF, {str(i): f"T{i}" for i in range(6)})
    root = etree.fromstring(out)
    ns = {"x": "urn:oasis:names:tc:xliff:document:1.2"}

    def tgt(tid):
        t = root.find(f".//x:trans-unit[@id='{tid}']/x:target", ns)
        return None if t is None else (t.text, t.get("state"))

    assert tgt("1") == ("T0", None)
    assert tgt("2") is None and tgt("5") is None
    assert tgt("3") == ("Ligne approuvee.", None)
    assert tgt("4") == ("Ligne finale.", "final")
    assert tgt("6") == ("T5", "translated")
    alt = root.find(".//x:alt-trans/x:target", ns)
    assert alt.text == "Ancien."
    print("  [xliff 1.2] translate=no / approved / final skipped and never overwritten OK")


def test_xliff12_seg_source():
    xlf = SDL.replace(b' xmlns:sdl="http://sdl.com/FileTypes/SdlXliff/1.0"', b"").replace(
        b' sdl:version="1.0"', b""
    )
    # strip SDL-only markup so it is a plain 1.2 file with seg-source
    import re

    xlf = re.sub(rb"<sdl:seg-defs>.*?</sdl:seg-defs>", b"", xlf)
    xlf = re.sub(rb' sdl:cid="c1"', b"", xlf)
    xlf = re.sub(rb"<file-info .*?</file-info>", b"", xlf)
    units = _units("xlf", xlf)
    assert SEG_BASE + 0 in units and SEG_BASE + 1 in units, units
    out = get_filter("xlf").build(xlf, {str(SEG_BASE + 1): "Dann speichern."})
    assert b'<mrk mtype="seg" mid="2">Dann speichern.</mrk></target>' in out, out
    # a document stored before seg-source support keyed the trans-unit by its <source>:
    # the translation is still written (whole target), never dropped
    legacy = get_filter("xlf").build(xlf, {"0": "Alles neu."})
    assert b"<target>Alles neu.</target>" in _tu(legacy, "a1b2c3d4-0001"), legacy
    print("  [xliff 1.2] seg-source units mirrored; legacy whole-unit keys still written OK")


def test_hostile_inputs():
    from app import config
    from app.filters.base import InputLimitError

    f = get_filter("sdlxliff")
    cases = {
        "doctype": SDL.replace(b"<xliff ", b'<!DOCTYPE x [<!ENTITY a "b">]><xliff ', 1),
        "no sdl ns": MQ,
        "xliff 2.0": b'<?xml version="1.0"?><xliff xmlns="urn:oasis:names:tc:xliff:document:2.0" '
        b'xmlns:sdl="http://sdl.com/FileTypes/SdlXliff/1.0" version="2.0" srcLang="en">'
        b'<file id="f"><unit id="u"><segment><source>a</source></segment></unit></file></xliff>',
        "not xml": b"<xliff><trans-unit>",
    }
    for name, raw in cases.items():
        try:
            f.parse(raw)
        except ValueError:
            pass
        else:
            raise AssertionError(f"{name} must be refused")
    old = config.MAX_DOCUMENT_SEGMENTS
    try:
        config.MAX_DOCUMENT_SEGMENTS = 1
        for fmt, raw in (("sdlxliff", SDL), ("mqxliff", MQ)):
            try:
                get_filter(fmt).parse(raw)
            except InputLimitError:
                pass
            else:
                raise AssertionError(f"{fmt} over the segment cap must be refused")
    finally:
        config.MAX_DOCUMENT_SEGMENTS = old
    assert SDL_NS
    print("  [hostile] DOCTYPE, wrong namespace, 2.0, malformed and caps refused OK")


def test_export_end_to_end():
    from sqlmodel import Session, select

    from app.db import engine, init_db
    from app.models import Document, Project, Segment, TargetLocale
    from app.services.export import build_translated_file
    from app.services.ingest import ingest_document

    init_db()
    with Session(engine) as s:
        p = Project(name="catxliff", org_id=1, source_locale="en-US", default_provider="mock")
        s.add(p)
        s.commit()
        s.refresh(p)
        s.add(TargetLocale(project_id=p.id, locale="de-DE"))
        s.commit()
        doc = ingest_document(s, p, "guide.sdlxliff", SDL, "segmented")
        segs = s.exec(
            select(Segment).where(Segment.document_id == doc.id).order_by(Segment.order_index)
        ).all()
        assert [g.source_text for g in segs][:2] == [
            "Open the ⟦g1⟧Settings⟦/g1⟧ menu.",
            "Then click Save.",
        ], [g.source_text for g in segs]
        # translate every segment except the first
        for g in segs[1:]:
            g.target_text = "DE " + g.source_text
            g.status = "translated"
            s.add(g)
        s.commit()
        doc = s.get(Document, doc.id)
        out = build_translated_file(s, doc, "de-DE")
    tu1 = _tu(out, "a1b2c3d4-0001")
    assert (
        b'<mrk mtype="seg" mid="1"/> <mrk mtype="seg" mid="2">DE Then click Save.</mrk>' in tu1
    ), tu1
    assert b'<sdl:seg id="1" conf="Draft"/><sdl:seg id="2" conf="Translated"/>' in tu1, tu1
    for tid in ("a1b2c3d4-0003", "a1b2c3d4-0004", "a1b2c3d4-0005"):
        assert _tu(out, tid) == _tu(SDL, tid), tid
    print("  [export] ingest -> partial translation -> export keeps the vendor file OK")


def main():
    test_registration()
    test_identity_round_trip()
    test_sdlxliff_segments_and_status()
    test_sdlxliff_partial_and_mismatch()
    test_sdlxliff_inline_damage_is_safe()
    test_encodings()
    test_mqxliff()
    test_mxliff()
    test_xliff12_hardening()
    test_xliff12_seg_source()
    test_hostile_inputs()
    test_export_end_to_end()
    print("CAT XLIFF FILTER OK")


if __name__ == "__main__":
    main()
