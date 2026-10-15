"""Trados TagEditor TTX filter (Batch 13, app/filters/ttx_filter.py).

  identity     a build with nothing to write returns the original bytes (UTF-8 and the
               usual UTF-16 LE + BOM + CRLF), fallback keys included.
  units        one unit per <Tu>; <ut> start/end pairs are paired tokens, other <ut> point
               tokens, <df> wraps; a <Tu> whose target already has text is never parsed.
  translated   only the written <Tu> elements change: target <Tuv> filled (empty, self-
               closing or missing), MatchPercent/Origin set, every other byte identical;
               a stale mapping naming a protected <Tu> is ignored.
  unsegmented  a TTX with no <Tu> is refused with a plain message; all-translated refused.
  encodings    UTF-16 LE/BE with BOM, UTF-16 without BOM, UTF-8 with BOM written back in
               their own encoding; a declared legacy encoding is refused.
  inline       a translation that drops, repeats or invents tokens still builds well-formed.
  hostile      DOCTYPE, wrong root, malformed XML, the pre-parse budget and segment cap.
  localized    build_localized fills an empty TargetLanguage and names new target Tuvs.
  export       ingest -> partial translation -> export through services/export.

Hermetic + offline. Final line: "TTX FILTER OK".
"""

import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(_ROOT)
sys.path.insert(0, _ROOT)
from tests._bootstrap import init_env

init_env("loctms_ttx_")

from lxml import etree

from app.filters import get_filter, supported_exts

# Fixtures: tests/fixtures/formats/ttx/sample.ttx, tests/fixtures/formats/ttx/unsegmented.ttx
FIX = os.path.join(_ROOT, "tests", "fixtures", "formats", "ttx")


def _fx(name: str) -> bytes:
    with open(os.path.join(FIX, name), "rb") as fh:
        return fh.read().replace(b"\r\n", b"\n")  # autocrlf checkouts: test LF bytes


TTX = _fx("sample.ttx")
UNSEG = _fx("unsegmented.ttx")
F = get_filter("ttx")


def _utf16(raw: bytes, codec: str = "utf-16-le", bom: bytes = b"\xff\xfe") -> bytes:
    """The usual on-disk TTX: UTF-16 with a BOM, CRLF, declaration says utf-16."""
    text = raw.decode("utf-8").replace("encoding='utf-8'", "encoding='utf-16'")
    return bom + text.replace("\n", "\r\n").encode(codec)


def _tus(out: bytes) -> list[bytes]:
    """The bytes of every <Tu ...>...</Tu> in order (UTF-8 output)."""
    res, pos = [], 0
    while True:
        s = out.find(b"<Tu ", pos)
        if s < 0:
            return res
        e = out.index(b"</Tu>", s) + len(b"</Tu>")
        res.append(out[s:e])
        pos = e


def _outside(out: bytes) -> bytes:
    for tu in _tus(out):
        out = out.replace(tu, b"<TU/>", 1)
    return out


MAPPING = {
    "0": "Klicken Sie auf ⟦g1⟧Speichern⟦/g1⟧, um F&E-Notizen zu behalten.",
    "1": "Zeile eins⟦x1⟧⟦g2⟧Zeile zwei⟦/g2⟧ hier.",
    "2": "HACK protected",  # existing target: must never be written
    "3": "Leeres Ziel hier.",
    "4": "⟦x1⟧ 42",
}


def test_registration():
    assert "ttx" in set(supported_exts())
    assert get_filter("ttx").fmt == "ttx"
    print("  [registry] ttx registered OK")


def test_identity():
    keys = {str(u.p_index) for u in F.parse(TTX).units}
    for raw in (TTX, _utf16(TTX)):
        assert F.build(raw, {}) == raw
        assert F.build_localized(raw, {}, "de-DE") == raw  # TargetLanguage already set
        assert F.build(raw, dict.fromkeys(keys, "x"), fallback_keys=keys) == raw
    print("  [identity] no-change build returns the original bytes (UTF-8, UTF-16) OK")


def test_units():
    r = F.parse(TTX)
    got = {u.p_index: u.text for u in r.units}
    assert got == {
        0: "Click ⟦g1⟧Save⟦/g1⟧ to keep R&D notes.",
        1: "Line one⟦x1⟧⟦g2⟧line two⟦/g2⟧ here.",
        3: "Empty target here.",
        4: "⟦x1⟧ 42",
    }, got
    assert r.skeleton["protected_units"] == {"existing-target": 1}, r.skeleton
    assert r.skeleton["unsegmented_text"] == 1, r.skeleton
    first = next(u for u in r.units if u.p_index == 0)
    assert first.inline_map["1"][0].startswith('<ut Type="start"'), first.inline_map
    assert first.inline_map["1"][1].startswith('<ut Type="end"'), first.inline_map
    # the same file as UTF-16 parses to the same units
    assert {u.p_index: u.text for u in F.parse(_utf16(TTX)).units} == got
    print("  [units] one unit per Tu, ut pairs/points and df tokens, protected Tu skipped OK")


def test_translated_round_trip():
    out = F.build(TTX, MAPPING)
    etree.fromstring(out)
    assert b"HACK" not in out
    tus = _tus(out)
    assert tus[0] == (
        b'<Tu MatchPercent="0" Origin="mt"><Tuv Lang="EN-US">Click <ut Type="start" '
        b'RightEdge="angle" DisplayText="b">&lt;b&gt;</ut>Save<ut Type="end" LeftEdge="angle" '
        b'DisplayText="b">&lt;/b&gt;</ut> to keep R&amp;D notes.</Tuv><Tuv Lang="DE-DE">Klicken '
        b'Sie auf <ut Type="start" RightEdge="angle" DisplayText="b">&lt;b&gt;</ut>Speichern'
        b'<ut Type="end" LeftEdge="angle" DisplayText="b">&lt;/b&gt;</ut>, um F&amp;E-Notizen '
        b"zu behalten.</Tuv></Tu>"
    ), tus[0]
    # missing target Tuv created after the source; source trailing space carried
    assert tus[1].endswith(
        b' here. </Tuv><Tuv Lang="DE-DE">Zeile eins<ut DisplayText="br">&lt;br&gt;</ut>'
        b'<df Bold="on">Zeile zwei</df> hier. </Tuv></Tu>'
    ), tus[1]
    # the protected Tu is byte-identical
    assert tus[2] == _tus(TTX)[2], tus[2]
    # self-closing target expanded; single-quoted MatchPercent keeps its quoting
    assert tus[3] == (
        b'<Tu MatchPercent=\'0\' Origin="mt"><Tuv Lang="EN-US">Empty target here.</Tuv>'
        b'<Tuv Lang="DE-DE">Leeres Ziel hier.</Tuv></Tu>'
    ), tus[3]
    assert _outside(out) == _outside(TTX), "bytes outside the written Tu elements changed"
    # partial: only key 3 written, everything else identical
    part = F.build(TTX, {"3": "Leeres Ziel hier."})
    assert _tus(part)[0] == _tus(TTX)[0] and _tus(part)[1] == _tus(TTX)[1]
    assert part.count(b'Origin="mt"') == 1
    print("  [translated] targets written, MatchPercent/Origin set, other bytes identical OK")


def test_unsegmented_refused():
    from app.filters.base import EmptyDocumentError
    from app.filters.ttx_filter import UnsegmentedTtxError

    try:
        F.parse(UNSEG)
    except UnsegmentedTtxError as exc:
        msg = str(exc)
        assert "not segmented" in msg and "Pre-translate" in msg, msg
        assert isinstance(exc, EmptyDocumentError)
    else:
        raise AssertionError("an unsegmented TTX must be refused")
    done = TTX
    for empty in (b'<Tuv Lang="DE-DE"></Tuv>', b'<Tuv Lang="DE-DE"/>'):
        done = done.replace(empty, b'<Tuv Lang="DE-DE">x</Tuv>')
    done = done.replace(b"here. </Tuv></Tu>", b'here. </Tuv><Tuv Lang="DE-DE">y</Tuv></Tu>')
    done = done.replace(b" 42</Tuv></Tu>", b' 42</Tuv><Tuv Lang="DE-DE">42</Tuv></Tu>')
    try:
        F.parse(done)
    except EmptyDocumentError as exc:
        assert "already has a translation" in str(exc), exc
    else:
        raise AssertionError("a fully translated TTX must be refused as nothing to do")
    print("  [unsegmented] no-Tu TTX and all-translated TTX refused with plain messages OK")


def test_encodings():
    from app.filters.base import InputLimitError

    for codec, bom in (("utf-16-le", b"\xff\xfe"), ("utf-16-be", b"\xfe\xff"), ("utf-16-le", b"")):
        raw = _utf16(TTX, codec, bom)
        out = F.build(raw, MAPPING)
        assert out.startswith(bom + "<?xml".encode(codec)), (codec, out[:12])
        text = out[len(bom) :].decode(codec)
        assert "\r\n" in text and 'Origin="mt"' in text and "Leeres Ziel hier." in text
        assert text.count("\r\n") == raw[len(bom) :].decode(codec).count("\r\n")
    bom8 = b"\xef\xbb\xbf" + TTX
    out = F.build(bom8, MAPPING)
    assert out.startswith(b"\xef\xbb\xbf<?xml") and b"Leeres Ziel" in out
    legacy = TTX.replace(b"encoding='utf-8'", b"encoding='windows-1252'")
    try:
        F.parse(legacy)
    except InputLimitError as exc:
        assert "windows-1252" in str(exc), exc
    else:
        raise AssertionError("a declared legacy encoding must be refused")
    print("  [encodings] UTF-16 LE/BE (BOM or not), UTF-8 BOM kept; legacy refused OK")


def test_inline_damage_is_safe():
    bad = {
        "0": "Klicken ⟦g1⟧⟦g1⟧Speichern ⟦x9⟧ ⟦/g7⟧ und",  # repeated, unknown, stray close
        "1": "⟦/g2⟧Zeile⟦g2⟧ offen",  # close before open, open never closed
        "3": "Text mit ⟦x1⟧ unbekanntem Token & <b>",
    }
    out = F.build(TTX, bad)
    etree.fromstring(out)
    assert b"\xe2\x9f\xa6" not in out, "a token leaked into the file"
    assert b"&amp; &lt;b>" in out
    print("  [inline] damaged tokens dropped or closed, output well-formed OK")


def test_hostile_inputs():
    from app import config
    from app.filters.base import InputLimitError

    cases = {
        "doctype": TTX.replace(b"<TRADOStag ", b'<!DOCTYPE x [<!ENTITY a "b">]><TRADOStag ', 1),
        "not ttx": b"<?xml version='1.0'?><xliff version='1.2'><file/></xliff>",
        "malformed": b"<TRADOStag><Body><Raw><Tu>",
    }
    for name, raw in cases.items():
        try:
            F.parse(raw)
        except ValueError:
            pass
        else:
            raise AssertionError(f"{name} must be refused")
    old = config.MAX_DOCUMENT_SEGMENTS
    try:
        config.MAX_DOCUMENT_SEGMENTS = 2  # 4 units; the sample stays under the budget
        for raw in (TTX, _utf16(TTX)):
            try:
                F.parse(raw)
            except InputLimitError:
                pass
            else:
                raise AssertionError("over the segment cap must be refused")
        flood = TTX.replace(b"</Raw>", b"<ut/>" * 50 + b"</Raw>")
        try:
            F.parse(flood)
        except InputLimitError as exc:
            assert "MAX_DOCUMENT_SEGMENTS" in str(exc)
        else:
            raise AssertionError("the pre-parse markup budget must refuse")
    finally:
        config.MAX_DOCUMENT_SEGMENTS = old
    print("  [hostile] DOCTYPE, wrong root, malformed, budget and segment cap refused OK")


def test_localized_language():
    raw = TTX.replace(b'TargetLanguage="DE-DE"', b'TargetLanguage=""')
    raw = raw.replace(b'<Tuv Lang="DE-DE"></Tuv>', b"").replace(b'<Tuv Lang="DE-DE"/>', b"")
    out = F.build_localized(raw, {"0": "Bonjour ⟦g1⟧x⟦/g1⟧.", "3": "Cible."}, "fr-FR")
    assert b'TargetLanguage="FR-FR"' in out, out[:400]
    assert b'<Tuv Lang="FR-FR">Cible.</Tuv>' in out, out
    try:
        F.build(raw, {"3": "Cible."})  # no target language anywhere
    except ValueError as exc:
        assert "target language" in str(exc)
    else:
        raise AssertionError("a target Tuv with no language must be refused")
    print("  [localized] empty TargetLanguage filled, new Tuv named OK")


def test_export_end_to_end():
    from sqlmodel import Session, select

    from app.db import engine, init_db
    from app.models import Document, Project, Segment, TargetLocale
    from app.services.export import build_translated_file
    from app.services.ingest import ingest_document

    raw = _utf16(TTX)
    init_db()
    with Session(engine) as s:
        p = Project(name="ttx", org_id=1, source_locale="en-US", default_provider="mock")
        s.add(p)
        s.commit()
        s.refresh(p)
        s.add(TargetLocale(project_id=p.id, locale="de-DE"))
        s.commit()
        doc = ingest_document(s, p, "guide.ttx", raw, "segmented")
        segs = s.exec(
            select(Segment).where(Segment.document_id == doc.id).order_by(Segment.order_index)
        ).all()
        src = [g.source_text for g in segs]
        assert src[0] == "Click ⟦g1⟧Save⟦/g1⟧ to keep R&D notes.", src
        assert "Already done." not in src, src
        target = next(g for g in segs if g.source_text == "Empty target here.")
        target.target_text = "Leeres Ziel hier."
        target.status = "translated"
        s.add(target)
        s.commit()
        doc = s.get(Document, doc.id)
        out = build_translated_file(s, doc, "de-DE")
    assert out.startswith(b"\xff\xfe"), out[:4]
    text = out[2:].decode("utf-16-le")
    assert text.count('Origin="mt"') == 1, text
    assert '<Tuv Lang="DE-DE">Leeres Ziel hier.</Tuv>' in text
    # untranslated units stay without a target (never source-filled)
    assert '<Tuv Lang="DE-DE"></Tuv>' in text
    assert "here. </Tuv></Tu>" in text
    print("  [export] ingest -> partial translation -> export keeps the TTX OK")


def main():
    test_registration()
    test_identity()
    test_units()
    test_translated_round_trip()
    test_unsegmented_refused()
    test_encodings()
    test_inline_damage_is_safe()
    test_hostile_inputs()
    test_localized_language()
    test_export_end_to_end()
    print("TTX FILTER OK")


if __name__ == "__main__":
    main()
