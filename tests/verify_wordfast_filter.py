"""Wordfast filters (app/filters/wordfast.py): Wordfast Pro TXML and TXLF.

  identity     a build with nothing to write returns the original bytes.
  txml         one unit per <segment> in a translatable block; <ut> start/end pairs are
               paired tokens, other inline elements point tokens, all written back with
               the source's own bytes; a missing target lands after the source and its
               trailing <ws>; <comments>, <revisions>, <ws> and segment attributes are kept.
  protection   TXML locked (segment or block), localizable blocks and confirmed targets
               (not raw MT, not a fuzzy under 100) are never parsed or overwritten; TXLF
               gs4tr:locked and a target past needs-* likewise, even when a stored mapping
               names them.
  untranslated a unit export marks as source fallback stays EMPTY (no source copy).
  encodings    UTF-8 BOM + CRLF and UTF-16 LE BOM round-trip in their own encoding; a
               declared legacy encoding is refused.
  hostile      DOCTYPE, a non-TXML root, malformed XML, the markup budget and the
               segment cap refuse before any unit is stored.
  export       ingest -> partial translation -> export through services/export.

Fixtures are hand-written from the Okapi TXML filter notes (Apache-2.0, behaviour spec
only) and the XLIFF 1.2 spec. Hermetic + offline. Final line: "WORDFAST FILTER OK".
"""

import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(_ROOT)
sys.path.insert(0, _ROOT)
from tests._bootstrap import init_env

init_env("loctms_wordfast_")

from app.filters import get_filter, supported_exts

FIX = os.path.join(_ROOT, "tests", "fixtures", "formats", "wordfast")


def _fx(name: str) -> bytes:
    with open(os.path.join(FIX, name), "rb") as fh:
        return fh.read().replace(b"\r\n", b"\n")  # autocrlf checkouts: test LF bytes


TXML = _fx("sample.txml")
TXLF = _fx("sample.txlf")


def _units(fmt, raw):
    return {u.p_index: u.text for u in get_filter(fmt).parse(raw).units}


def _seg(out: bytes, seg_id: str) -> bytes:
    start = out.find(b'<segment segmentId="' + seg_id.encode() + b'"')
    assert start >= 0, seg_id
    return out[start : out.index(b"</segment>", start) + len(b"</segment>")]


def _tu(out: bytes, tu_id: str) -> bytes:
    start = out.find(b'<trans-unit id="' + tu_id.encode() + b'"')
    assert start >= 0, tu_id
    return out[start : out.index(b"</trans-unit>", start) + len(b"</trans-unit>")]


def _only_changed(before: bytes, after: bytes, cut, ids: list[str]) -> None:
    a, b = before, after
    for i in ids:
        a = a.replace(cut(before, i), b"<CUT/>")
        b = b.replace(cut(after, i), b"<CUT/>")
    assert a == b, "bytes outside the written segments changed"


def test_registration():
    exts = set(supported_exts())
    assert {"txml", "txlf"} <= exts, exts
    assert get_filter("txml").fmt == "txml"
    assert get_filter("txlf").fmt == "txlf"
    print("  [registry] txml/txlf registered OK")


def test_identity():
    for fmt, raw in (("txml", TXML), ("txlf", TXLF)):
        f = get_filter(fmt)
        assert f.build(raw, {}) == raw, fmt
        assert f.build_localized(raw, {}, "de-DE") == raw, fmt  # targetlocale already set
    print("  [identity] build with nothing to write returns the original bytes OK")


def test_txml_units_and_protection():
    u = _units("txml", TXML)
    assert u == {
        0: "Open the ⟦g1⟧Settings⟦/g1⟧ menu.",
        1: "Press ⟦x1⟧Save & close.",
        5: "Machine line.",
        6: "Left untranslated.",
        7: "With a comment.",
    }, u
    sk = get_filter("txml").parse(TXML).skeleton
    assert sk["protected_units"] == {"locked": 2, "confirmed": 2, "localizable": 1}, sk
    # a stored mapping that names every key (protected ones included) writes only the
    # writable segments
    mapping = {str(k): f"T{k}" for k in range(12)}
    mapping["0"] = "Oeffnen Sie das ⟦g1⟧Einstellungen⟦/g1⟧-Menue."
    mapping["1"] = "Druecken Sie ⟦x1⟧Speichern & schliessen."
    out = get_filter("txml").build(TXML, mapping)
    s1 = _seg(out, "1")
    assert (
        b'<ws> </ws><target>Oeffnen Sie das <ut type="start" x="1">&lt;b class=\'ui\'&gt;</ut>'
        b'Einstellungen<ut type="end" x="1">&lt;/b&gt;</ut>-Menue.</target>' in s1
    ), s1
    s2 = _seg(out, "2")
    assert (
        b'<target score="85">Druecken Sie <ut x="2">&lt;br/&gt;</ut>Speichern &amp; schliessen.'
        b"</target>" in s2
    ), s2
    assert b"<target>T5</target>" in _seg(out, "6")
    assert b"<target>T6</target>" in _seg(out, "7")  # <target/> filled
    s8 = _seg(out, "8")
    assert b"<ws>  </ws><source>With a comment.</source><ws> </ws><target>T7</target>" in s8, s8
    assert b"Keep it short &amp; plain</comment>" in s8
    for sid in ("3", "4", "5", "9", "10"):
        assert _seg(out, sid) == _seg(TXML, sid), sid
    _only_changed(TXML, out, _seg, ["1", "2", "6", "7", "8"])
    print("  [txml] units, ut pairs, ws order, protection and byte fidelity OK")


def test_txml_tag_damage_is_safe():
    out = get_filter("txml").build(TXML, {"0": "Nur ⟦/g1⟧ Text ⟦x9⟧ ohne ⟦g1⟧Tags", "1": "x"})
    from lxml import etree

    etree.fromstring(out)  # always well-formed
    assert b"<target>x</target>" in _seg(out, "2") or b'score="85">x</target>' in _seg(out, "2")
    print("  [txml] damaged or dropped tokens still give well-formed XML OK")


def test_txlf():
    u = _units("txlf", TXLF)
    assert u == {
        0: "Open the ⟦x1⟧Settings⟦x2⟧ menu.",
        1: "Then click ⟦p1⟧.",  # #693: an <x id>/<ph id> placeholder moves freely
        4: "Left untranslated.",
    }, u
    sk = get_filter("txlf").parse(TXLF).skeleton
    assert sk["protected_units"] == {"locked": 1, "confirmed": 1, "final": 1}, sk
    mapping = {str(k): f"T{k}" for k in range(6)}
    mapping["0"] = "Oeffnen ⟦x1⟧Einstellungen⟦x2⟧."
    out = get_filter("txlf").build(TXLF, mapping)
    assert (
        b'<target state="translated">Oeffnen <bpt id="1">&lt;b&gt;</bpt>Einstellungen'
        b'<ept id="1">&lt;/b&gt;</ept>.</target>' in _tu(out, "1")
    ), _tu(out, "1")
    assert b'<target state="translated" gs4tr:score="75">T1</target>' in _tu(out, "2")
    assert b"<target>T4</target>" in _tu(out, "5")
    for tid in ("3", "4", "6"):
        assert _tu(out, tid) == _tu(TXLF, tid), tid
    _only_changed(TXLF, out, _tu, ["1", "2", "5"])
    # a gs4tr lock on the target element also protects
    raw = TXLF.replace(b'<target state="needs-translation">', b'<target gs4tr:locked="true">')
    assert 0 not in _units("txlf", raw)
    # the same file under .xlf gets the same protection (detection is by namespace)
    assert set(_units("xlf", TXLF)) == {0, 1, 4}
    print("  [txlf] gs4tr locked/confirmed protected, needs-* targets written OK")


def test_untranslated_stays_empty():
    out = get_filter("txml").build(TXML, {"0": "A", "6": "src copy"}, fallback_keys={"6"})
    assert _seg(out, "7") == _seg(TXML, "7")  # <target/> untouched, no source copy
    out = get_filter("txlf").build(TXLF, {"0": "A", "4": "src copy"}, fallback_keys={"4"})
    assert _tu(out, "5") == _tu(TXLF, "5")
    print("  [untranslated] fallback keys stay empty OK")


def test_encodings():
    f = get_filter("txml")
    crlf = b"\xef\xbb\xbf" + TXML.replace(b"\n", b"\r\n")
    assert f.build(crlf, {}) == crlf
    out = f.build(crlf, {"5": "Maschine."})
    assert out.startswith(b"\xef\xbb\xbf") and out.count(b"\r\n") == crlf.count(b"\r\n")
    assert (
        b'<segment segmentId="6" gtmt="true">\r\n<source>Machine line.</source>\r\n'
        b"<target>Maschine.</target>" in out
    )
    u16 = b"\xff\xfe" + TXML.decode("utf-8").replace(
        'encoding="UTF-8"', 'encoding="UTF-16"'
    ).encode("utf-16-le")
    assert _units("txml", u16)[5] == "Machine line."
    out16 = f.build(u16, {"5": "Maschine ä."})
    assert out16.startswith(b"\xff\xfe")
    assert "<target>Maschine ä.</target>" in out16[2:].decode("utf-16-le")
    legacy = TXML.replace(b'encoding="UTF-8"', b'encoding="ISO-8859-1"')
    try:
        f.parse(legacy)
    except ValueError as exc:
        assert "TXML" in str(exc), exc
    else:
        raise AssertionError("legacy encoding must be refused")
    print("  [encodings] BOM/CRLF, UTF-16 LE kept; legacy encoding refused OK")


def test_target_locale():
    raw = TXML.replace(b' targetlocale="de_DE"', b"")
    out = get_filter("txml").build_localized(raw, {"5": "M"}, "fr-FR")
    assert b'createdby="hand-written fixture" targetlocale="fr_FR">' in out, out[:300]
    print("  [locale] missing targetlocale stamped in the file's own style OK")


def test_hostile_inputs():
    from app import config
    from app.filters.base import InputLimitError

    f = get_filter("txml")
    cases = {
        "doctype": TXML.replace(b"<txml ", b'<!DOCTYPE x [<!ENTITY a "b">]><txml ', 1),
        "xxe": b'<?xml version="1.0"?><!DOCTYPE t [<!ENTITY e SYSTEM "file:///etc/passwd">]>'
        b"<txml><translatable><segment><source>&e;</source></segment></translatable></txml>",
        "not txml": TXLF,
        "malformed": b"<txml><translatable><segment><source>a</segment>",
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
        for fmt, raw in (("txml", TXML), ("txlf", TXLF)):
            try:
                get_filter(fmt).parse(raw)
            except InputLimitError:
                pass
            else:
                raise AssertionError(f"{fmt} over the segment cap must be refused")
        config.MAX_DOCUMENT_SEGMENTS = 3
        bomb = b"<txml><translatable>" + b"<segment><source>a</source></segment>" * 50
        bomb += b"</translatable></txml>"
        try:
            f.parse(bomb)
        except InputLimitError as exc:
            assert "TXML" in str(exc), exc
        else:
            raise AssertionError("markup budget must refuse")
    finally:
        config.MAX_DOCUMENT_SEGMENTS = old
    print("  [hostile] DOCTYPE/XXE, wrong root, malformed, budget and caps refused OK")


def test_export_end_to_end():
    from sqlmodel import Session, select

    from app.db import engine, init_db
    from app.models import Document, Project, Segment, TargetLocale
    from app.services.export import build_translated_file
    from app.services.ingest import ingest_document

    init_db()
    for fname, raw in (("guide.txml", TXML), ("guide.txlf", TXLF)):
        with Session(engine) as s:
            p = Project(name=fname, org_id=1, source_locale="en-US", default_provider="mock")
            s.add(p)
            s.commit()
            s.refresh(p)
            s.add(TargetLocale(project_id=p.id, locale="de-DE"))
            s.commit()
            doc = ingest_document(s, p, fname, raw, "segmented")
            segs = s.exec(
                select(Segment).where(Segment.document_id == doc.id).order_by(Segment.order_index)
            ).all()
            assert segs[0].source_text.startswith("Open the "), [g.source_text for g in segs]
            for g in segs[:-1]:  # leave the last one untranslated
                g.target_text = "DE " + g.source_text
                g.status = "translated"
                s.add(g)
            s.commit()
            doc = s.get(Document, doc.id)
            out = build_translated_file(s, doc, "de-DE")
        if fname.endswith("txml"):
            assert b"<target>DE Machine line.</target>" in _seg(out, "6"), out
            assert _seg(out, "8") == _seg(TXML, "8")  # untranslated: no target created
            assert _seg(out, "5") == _seg(TXML, "5")
        else:
            assert b"DE Then click" in _tu(out, "2"), out
            assert _tu(out, "5") == _tu(TXLF, "5")
            assert _tu(out, "3") == _tu(TXLF, "3")
    print("  [export] ingest -> partial translation -> export keeps both Wordfast files OK")


def main():
    test_registration()
    test_identity()
    test_txml_units_and_protection()
    test_txml_tag_damage_is_safe()
    test_txlf()
    test_untranslated_stays_empty()
    test_encodings()
    test_target_locale()
    test_hostile_inputs()
    test_export_end_to_end()
    print("WORDFAST FILTER OK")


if __name__ == "__main__":
    main()
