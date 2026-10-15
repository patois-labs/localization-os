"""Zipped bilingual XLIFF packages (app/filters/xliff_package.py): memoQ .mqxlz and
WorldServer .xlz / .wsxz, plus the WorldServer iws:status rules in cat_xliff.

  registration  mqxlz / xlz / wsxz resolve, API labels and the upload accept list.
  identity      a build with nothing to write returns the original archive bytes.
  mqxlz         the inner .mqxliff goes through the cat_xliff engine (locked, reviewer
                and confirmed rows protected, written rows ManuallyConfirmed, an
                untranslated row stays empty); the skeleton member, member order, names,
                dates, compression and archive comment are kept.
  worldserver   iws:status lock_status="locked" and translation_status="finished" are
                never parsed or overwritten (even when a stale mapping names them);
                written units become finished; a unit with no target gets none when it
                was not translated; loose .xlf WorldServer files get the same protection.
  multi         a .wsxz with SDLXLIFF + XLIFF members and TMX/TBX: keys are member
                offset + walk position and each member is written independently.
  hostile       not a zip, no XLIFF inside, duplicate names, a compression bomb, an
                encrypted member, a DOCTYPE inside, a legacy encoding and the segment cap
                are refused; a traversal-style member name is never extracted.
  export        ingest -> partial translation -> export through services/export.

Hermetic + offline. Final line: "XLIFF PACKAGE FILTER OK".
"""

import io
import os
import sys
import zipfile

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(_ROOT)
sys.path.insert(0, _ROOT)
from tests._bootstrap import init_env

init_env("loctms_xlzpack_")

from app.filters import get_filter, supported_exts
from app.filters.base import InputLimitError

FIX = os.path.join(_ROOT, "tests", "fixtures", "formats")


def _fx(*parts) -> bytes:
    with open(os.path.join(FIX, *parts), "rb") as fh:
        return fh.read().replace(b"\r\n", b"\n")  # autocrlf checkouts: test LF bytes


MQ = _fx("catxliff", "sample.mqxliff")
SDL = _fx("catxliff", "sample.sdlxliff")
WS = _fx("xlzpack", "content.xlf")
SKELETON = bytes(range(256)) * 4  # binary skeleton data memoQ keeps for itself
TMX = b'<?xml version="1.0"?><tmx version="1.4"><header/><body/></tmx>'
TBX = b'<?xml version="1.0"?><martif type="TBX"><text><body/></text></martif>'
DATE = (2024, 5, 6, 7, 8, 10)


def _zip(members, comment=b"") -> bytes:
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as zf:
        for name, body, method in members:
            info = zipfile.ZipInfo(name, date_time=DATE)
            info.compress_type = method
            zf.writestr(info, body)
        if comment:
            zf.comment = comment
    return out.getvalue()


D, S = zipfile.ZIP_DEFLATED, zipfile.ZIP_STORED
MQXLZ = _zip(
    [("document.mqxliff", MQ, D), ("skeleton", SKELETON, S), ("preview.html", b"<p>x</p>", D)],
    b"memoQ export",
)
XLZ = _zip([("content.xlf", WS, D), ("content.skl", b"<skl>{1}</skl>", D)])
WSXZ = _zip(
    [
        ("tm/project.tmx", TMX, D),
        ("fr-FR/guide.docx.sdlxliff", SDL, D),
        ("tb/terms.tbx", TBX, D),
        ("fr-FR/help.html.xlf", WS, D),
    ]
)


def _members(pkg: bytes) -> list:
    with zipfile.ZipFile(io.BytesIO(pkg)) as zf:
        return [(i.filename, i.date_time, i.compress_type, zf.read(i)) for i in zf.infolist()]


def _member(pkg: bytes, name: str) -> bytes:
    with zipfile.ZipFile(io.BytesIO(pkg)) as zf:
        return zf.read(name)


def _tu(out: bytes, tu_id: str) -> bytes:
    start = out.find(b'<trans-unit id="' + tu_id.encode() + b'"')
    assert start >= 0, tu_id
    return out[start : out.find(b"</trans-unit>", start)]


def _units(fmt, raw):
    return {u.p_index: u.text for u in get_filter(fmt).parse(raw).units}


def test_registration():
    exts = supported_exts()
    for ext, fmt in (("mqxlz", "mqxlz"), ("xlz", "xlz"), ("wsxz", "xlz")):
        assert ext in exts, ext
        assert get_filter(ext).fmt == fmt, ext
    from app.web.api_part2 import _FMT_LABELS

    assert {"mqxlz", "xlz", "wsxz"} <= set(_FMT_LABELS)
    with open(os.path.join(_ROOT, "app", "templates", "project.html"), encoding="utf-8") as fh:
        page = fh.read()
    assert ".mqxlz," in page and ".xlz," in page and ".wsxz," in page
    print("  [registration] mqxlz, xlz, wsxz resolve and are listed OK")


def test_identity():
    for fmt, pkg in (("mqxlz", MQXLZ), ("xlz", XLZ), ("xlz", WSXZ)):
        f = get_filter(fmt)
        assert f.build(pkg, {}) == pkg, fmt
        # a mapping that only names protected units writes nothing either
        units = f.parse(pkg).units
        assert f.build(pkg, {"999999": "x"}) == pkg
        assert units
    print("  [identity] nothing to write returns the original archive bytes OK")


def test_mqxlz():
    f = get_filter("mqxlz")
    units = _units("mqxlz", MQXLZ)
    # rows 2 (confirmed with target), 3 (locked), 4 (reviewer 1) are protected
    assert sorted(units) == [0, 4], units
    assert units[0] == "Press ⟦x1⟧Start⟦x2⟧ to begin.", units[0]
    sk = f.parse(MQXLZ).skeleton
    assert sk["format"] == "mqxlz"
    assert sk["package"]["xliff_files"][0]["path"] == "document.mqxliff"
    assert sk["package"]["other_files"] == 2
    assert sk["protected_units"] == {"confirmed": 1, "locked": 1, "approved": 1}
    out = f.build(
        MQXLZ,
        {"0": "Druecke ⟦x1⟧Start⟦x2⟧ zum Beginnen.", "4": "SOURCE FALLBACK", "1": "NO", "2": "NO"},
        fallback_keys={"4"},
    )
    before, after = _members(MQXLZ), _members(out)
    assert [m[:3] for m in before] == [m[:3] for m in after], "names/dates/compression"
    assert before[1:] == after[1:], "skeleton and preview identical"
    with zipfile.ZipFile(io.BytesIO(out)) as zf:
        assert zf.comment == b"memoQ export"
    inner = after[0][3]
    tu1 = _tu(inner, "1")
    assert b'mq:status="ManuallyConfirmed"' in tu1, tu1
    assert (
        b'<target xml:space="preserve">Druecke <bpt id="1" ctype="bold">{}</bpt>Start'
        b'<ept id="1">{}</ept> zum Beginnen.</target>'
    ) in tu1, tu1
    for tid in ("2", "3", "4", "5"):
        assert _tu(inner, tid) == _tu(MQ, tid), tid  # protected or untranslated: unchanged
    print("  [mqxlz] inner mqxliff written, protected rows and skeleton unchanged OK")


def test_worldserver_rules():
    f = get_filter("xlz")
    units = _units("xlz", XLZ)
    # tu2 finished, tu3 locked: protected; tu1, tu4, tu5 open
    assert sorted(units) == [0, 3, 4], units
    assert units[3] == "Use {0} items & ⟦p1⟧more.", units[3]  # #693: <ph id> is free-order
    sk = f.parse(XLZ).skeleton
    assert sk["protected_units"] == {"final": 1, "locked": 1}, sk
    out = f.build(
        XLZ,
        {
            "0": "Ouvrez la page ⟦g1⟧Parametres⟦/g1⟧.",
            "1": "OVERWRITE",
            "2": "OVERWRITE",
            "3": "Utilisez {0} elements & ⟦x1⟧plus.",
            "4": "SOURCE FALLBACK",
        },
        fallback_keys={"4"},
    )
    inner = _member(out, "content.xlf")
    tu1 = _tu(inner, "1")
    assert b'<target>Ouvrez la page <g id="1">Parametres</g>.</target>' in tu1, tu1
    assert (
        b'<iws:status translation_status="finished" translation_type="manual_translation"/>' in tu1
    ), tu1
    tu4 = _tu(inner, "4")
    assert b'<target>Utilisez {0} elements &amp; <x id="2"/>plus.</target>' in tu4, tu4
    assert b"<iws:status translation_status='finished'/>" in tu4, tu4  # quoting kept
    for tid in ("2", "3", "5"):
        assert _tu(inner, tid) == _tu(WS, tid), tid
    assert b"SOURCE FALLBACK" not in inner and b"OVERWRITE" not in inner
    assert _member(out, "content.skl") == b"<skl>{1}</skl>"
    # the same rules hold for a loose WorldServer .xlf (shared _unit_skip)
    loose = sorted(u.p_index for u in get_filter("xlf").parse(WS).units)
    assert loose == [0, 3, 4], loose
    print("  [worldserver] locked/finished protected, written units finished OK")


def test_multi_member_wsxz():
    f = get_filter("wsxz")
    sk = f.parse(WSXZ).skeleton
    files = sk["package"]["xliff_files"]
    assert [x["path"] for x in files] == ["fr-FR/guide.docx.sdlxliff", "fr-FR/help.html.xlf"]
    assert sk["package"]["other_files"] == 2
    off = files[1]["offset"]
    assert off > 0
    units = _units("xlz", WSXZ)
    assert off in units and units[off] == "Open the ⟦g1⟧Settings⟦/g1⟧ page.", units
    sdl_keys = sorted(k for k in units if k < off)
    assert sdl_keys, units
    out = f.build(WSXZ, {str(off): "Ouvrez ⟦g1⟧Parametres⟦/g1⟧.", str(sdl_keys[0]): "DE eins"})
    before, after = _members(WSXZ), _members(out)
    assert [m[0] for m in before] == [m[0] for m in after]
    assert before[0] == after[0] and before[2] == after[2], "TMX and TBX identical"
    assert b"DE eins" in after[1][3] and b'conf="Translated"' in after[1][3]
    assert b'<target>Ouvrez <g id="1">Parametres</g>.</target>' in after[3][3]
    # writing only the second member leaves the first byte-identical
    out2 = f.build(WSXZ, {str(off): "Ouvrez."})
    assert _members(out2)[1] == before[1]
    print("  [multi] wsxz member offsets and independent members OK")


def _refused(fmt, raw, name):
    try:
        get_filter(fmt).parse(raw)
    except ValueError:
        return
    raise AssertionError(f"{name} must be refused")


def test_hostile_inputs():
    from app import config

    _refused("mqxlz", MQ, "a bare mqxliff named .mqxlz")
    _refused("mqxlz", b"PK\x03\x04garbage", "a broken zip")
    _refused("xlz", _zip([("readme.txt", b"hi", D)]), "a zip with no XLIFF")
    dup = io.BytesIO()
    with zipfile.ZipFile(dup, "w") as zf:
        zf.writestr("a.xlf", WS)
        import warnings

        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            zf.writestr("a.xlf", WS)
    _refused("xlz", dup.getvalue(), "duplicate member names")
    bomb = _zip([("content.xlf", WS, D), ("pad.bin", b"\0" * (8 * 1024 * 1024), D)])
    old_ratio = config.ZIP_MAX_RATIO
    try:
        config.ZIP_MAX_RATIO = 50
        _refused("xlz", bomb, "a compression bomb")
    finally:
        config.ZIP_MAX_RATIO = old_ratio
    enc = bytearray(XLZ)
    enc[6] |= 1  # set the encrypted flag on the first local header
    _refused("xlz", bytes(enc), "an encrypted member")
    doctype = WS.replace(b"<xliff ", b'<!DOCTYPE x [<!ENTITY a "b">]><xliff ', 1)
    _refused("xlz", _zip([("content.xlf", doctype, D)]), "a DOCTYPE inside")
    legacy = WS.replace(b'encoding="UTF-8"', b'encoding="ISO-8859-1"', 1)
    _refused("xlz", _zip([("content.xlf", legacy, D)]), "a legacy encoding")
    old = config.MAX_DOCUMENT_SEGMENTS
    try:
        config.MAX_DOCUMENT_SEGMENTS = 2
        try:
            get_filter("xlz").parse(XLZ)
        except InputLimitError:
            pass
        else:
            raise AssertionError("segment cap must be refused")
    finally:
        config.MAX_DOCUMENT_SEGMENTS = old
    # a traversal-style name is only ever a zip entry name: carried, never extracted
    trav = _zip([("../../evil.xlf", WS, D)])
    out = get_filter("xlz").build(trav, {"0": "x"})
    assert [m[0] for m in _members(out)] == ["../../evil.xlf"]
    assert not os.path.exists(os.path.join(_ROOT, "..", "..", "evil.xlf"))
    print("  [hostile] non-zip, empty, duplicate, bomb, encrypted, DOCTYPE, caps OK")


def test_export_end_to_end():
    from sqlmodel import Session, select

    from app.db import engine, init_db
    from app.models import Document, Project, Segment, TargetLocale
    from app.services.export import build_translated_file
    from app.services.ingest import ingest_document

    init_db()
    with Session(engine) as s:
        p = Project(name="xlzpack", org_id=1, source_locale="en-US", default_provider="mock")
        s.add(p)
        s.commit()
        s.refresh(p)
        s.add(TargetLocale(project_id=p.id, locale="fr-FR"))
        s.commit()
        doc = ingest_document(s, p, "help.xlz", XLZ, "segmented")
        assert doc.fmt == "xlz", doc.fmt
        segs = s.exec(
            select(Segment).where(Segment.document_id == doc.id).order_by(Segment.order_index)
        ).all()
        first = segs[0].source_text
        assert first == "Open the ⟦g1⟧Settings⟦/g1⟧ page.", first
        segs[0].target_text = "Ouvrez la page ⟦g1⟧Parametres⟦/g1⟧."
        segs[0].status = "translated"
        s.add(segs[0])
        s.commit()
        doc = s.get(Document, doc.id)
        out = build_translated_file(s, doc, "fr-FR")
    inner = _member(out, "content.xlf")
    assert b'<target>Ouvrez la page <g id="1">Parametres</g>.</target>' in _tu(inner, "1")
    for tid in ("2", "3", "4", "5"):
        assert _tu(inner, tid) == _tu(WS, tid), tid  # untranslated stay empty, never source
    assert _member(out, "content.skl") == b"<skl>{1}</skl>"
    print("  [export] ingest -> partial translation -> export keeps the package OK")


def main():
    test_registration()
    test_identity()
    test_mqxlz()
    test_worldserver_rules()
    test_multi_member_wsxz()
    test_hostile_inputs()
    test_export_end_to_end()
    print("XLIFF PACKAGE FILTER OK")


if __name__ == "__main__":
    main()
