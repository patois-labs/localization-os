"""EBU STL subtitle filter (Tech 3264-E): app/filters/stl_filter.py.

  identity   parse + build with no changes is byte-identical (GSI, user data, comments).
  decode     table 00 floating diacritics, teletext rows, extension blocks, tokens.
  translate  only the TF of changed subtitles changes; timecodes, VP, JC, CF kept.
  reflow     long text re-wrapped within MNC; extension blocks added and TNB updated.
  table      a Cyrillic translation of a Latin file switches CCT 00 -> 01.
  refuse     text that does not fit, or characters no table holds, refuse the export.
  lang       build_localized writes the target language into LC (Appendix 3).
  hostile    not-STL (3D model), truncated blocks, bad CCT, segment cap.

Fixtures are built by hand byte by byte below. Final line: "STL FILTER OK".
"""

import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(_ROOT)
sys.path.insert(0, _ROOT)
from tests._bootstrap import init_env

init_env("loctms_stl_")

from app import config
from app.filters import get_filter, supported_exts
from app.filters.base import InputLimitError
from app.filters.scc_filter import CaptionTargetError
from app.filters.stl_filter import StlParseError, language_code


def gsi(*, cct=b"00", lc=b"09", dsc=b"1", mnc=b"40", tnb=0):
    g = bytearray(b" " * 1024)
    g[0:3] = b"850"
    g[3:11] = b"STL25.01"
    g[11:12] = dsc
    g[12:14] = cct
    g[14:16] = lc
    g[16:48] = b"Demo programme".ljust(32)
    g[224:230] = b"261010"
    g[230:236] = b"261010"
    g[236:238] = b"01"
    g[238:243] = b"%05d" % tnb
    g[243:248] = b"%05d" % tnb
    g[248:251] = b"001"
    g[251:253] = mnc
    g[253:255] = b"23"
    g[255:256] = b"1"
    g[256:264] = b"10000000"
    g[264:272] = b"10000100"
    g[272:274] = b"11"
    g[274:277] = b"GBR"
    g[448:460] = b"user area ok"
    return bytes(g)


def tti(sn, tf, *, ebn=0xFF, cf=0, vp=20, jc=2, cs=0, tci=(10, 0, 1, 0), tco=(10, 0, 3, 0)):
    assert len(tf) <= 112
    head = bytes([0]) + sn.to_bytes(2, "little") + bytes([ebn, cs, *tci, *tco, vp, jc, cf])
    return head + tf + b"\x8f" * (112 - len(tf))


DH = b"\x0d\x0b\x0b"  # double height, start box (twice)
END = b"\x0a\x0a"  # end box (twice)
SUBS = [
    tti(1, DH + b"Hello there," + END + b"\x8a\x8a" + DH + b"how are you?" + END),
    tti(2, DH + b"Caf\xc2e cr\xc8eme" + END, vp=22, tci=(10, 0, 4, 0), tco=(10, 0, 6, 0)),
    tti(3, DH + b"Red \x01alert\x07 now" + END, tci=(10, 0, 7, 0), tco=(10, 0, 9, 0)),
    tti(4, b"Translator note: check the name", cf=1),
    tti(4, b"user data block", ebn=0xFE),
    tti(5, DH + b"A long subtitle in two " + END, ebn=0x00),
    tti(5, b"\x8a\x8a" + DH + b"extension blocks." + END),
    tti(6, b"\x80Italic\x81 open text $5 #1"),
]
STL = gsi(tnb=len(SUBS)) + b"".join(SUBS)


def _f():
    return get_filter("stl")


def _texts(data):
    return [(u.p_index, u.text) for u in _f().parse(data).units]


def _blocks(data):
    return [data[1024 + i * 128 : 1024 + (i + 1) * 128] for i in range((len(data) - 1024) // 128)]


def test_registered():
    assert "stl" in supported_exts()
    print("  [reg] .stl registered OK")


def test_identity():
    f = _f()
    pr = f.parse(STL)
    assert f.build(STL, {}) == STL
    assert f.build(STL, {str(u.p_index): u.text for u in pr.units}) == STL
    print("  [identity] untouched and same-text builds byte-identical OK")


def test_decode():
    got = _texts(STL)
    assert got == [
        (0, "Hello there,\nhow are you?"),
        (1, "Café crëme"),
        (2, "Red ⟦x1⟧alert ⟦x2⟧ now"),
        (3, "A long subtitle in two\nextension blocks."),
        (4, "Italic⟦x1⟧ open text $5 #1"),
    ], got
    print("  [decode] diacritics, rows, tokens, extension blocks, comments skipped OK")


def test_translate():
    f = _f()
    out = f.build(STL, {"0": "Bonjour,\ncomment ça va ?", "1": "Café crème brûlée"})
    ob, sb = _blocks(out), _blocks(STL)
    assert len(ob) == len(sb) and out[:1024] == STL[:1024]
    assert ob[0][:16] == sb[0][:16] and ob[1][:16] == sb[1][:16]  # timecodes, VP, JC, CF
    assert ob[0][16:].startswith(DH + b"Bonjour," + END + b"\x8a\x8a" + DH + b"comment \xcbca va ?")
    assert b"cr\xc1eme br\xc3ul\xc2ee" in ob[1]
    assert ob[2:] == sb[2:]
    got = _texts(out)
    assert got[0] == (0, "Bonjour,\ncomment ça va ?") and got[1] == (1, "Café crème brûlée"), got
    # inline teletext colour codes survive as tokens; the code takes the space's cell
    out = f.build(STL, {"2": "Alerte ⟦x1⟧rouge ⟦x2⟧maintenant"})
    assert DH + b"Alerte\x01rouge\x07maintenant" + END in _blocks(out)[2], _blocks(out)[2]
    # open-subtitle italics (0x80/0x81) are non-spacing tokens
    out = f.build(STL, {"4": "Kursiv⟦x1⟧ offener Text 5 $"})
    assert _blocks(out)[7][16:].startswith(b"\x80Kursiv\x81 offener Text 5 $\x8f")
    print("  [translate] only changed TF bytes, headers kept, tokens restored OK")


def test_reflow_and_extension():
    f = _f()
    long = "This translation is much longer than the original line and wraps"
    out = f.build(STL, {"1": long})
    b = _blocks(out)[1]
    assert b[13] == 20, b[13]  # one row became two: VP moves up one double-height row
    rows = _texts(out)[1][1].split("\n")
    assert len(rows) == 2 and all(len(r) <= 40 for r in rows), rows
    # a subtitle that needs more TF bytes gets an extension block; TNB follows
    big = "Ça été très été ça été très été ça été\nça été très été ça été très été ça été"
    out = f.build(STL, {"0": big})
    ob = _blocks(out)
    assert len(ob) == len(SUBS) + 1 and out[238:243] == b"%05d" % (len(SUBS) + 1)
    assert ob[0][3] == 0x00 and ob[1][3] == 0xFF and ob[0][1:3] == ob[1][1:3]
    assert _texts(out)[0] == (0, big)
    # the five-block group shrinks back to one block when the text gets short
    out = f.build(STL, {"3": "Short."})
    assert len(_blocks(out)) == len(SUBS) - 1 and out[238:243] == b"%05d" % (len(SUBS) - 1)
    assert _texts(out)[3] == (3, "Short.")
    print("  [reflow] MNC wrap, VP shift, extension blocks + TNB OK")


def test_table_switch():
    f = _f()
    plain = gsi(tnb=2) + tti(1, DH + b"Hello," + END + b"\x8a\x8a" + DH + b"friend" + END)
    plain += tti(2, b"See you $5")
    out = f.build(plain, {"0": "Привет,\nдруг"})
    assert out[12:14] == b"01", out[12:14]
    assert _texts(out) == [(0, "Привет,\nдруг"), (1, "See you $5")], _texts(out)
    assert _blocks(out)[1] == _blocks(plain)[1]  # ASCII bytes are the same in 8859-5
    print("  [table] Cyrillic translation switches CCT 00 -> 01 OK")


def test_cyrillic_quotes_679():
    """#679: Russian MT output with « » (not in table 01) exported nothing, and the error blamed
    every Cyrillic letter. The quotes fold to '"' when the table lacks them; a translated
    subtitle's OLD text no longer blocks a table switch; a real blocker is named precisely."""
    f = _f()
    plain = gsi(tnb=2) + tti(1, DH + b"Hello, caf\xc2e" + END) + tti(2, b"Closed today")
    out = f.build(plain, {"0": "Привет — «кафе»", "1": "Сегодня „закрыто“"})
    assert out[12:14] == b"01", out[12:14]
    assert _texts(out) == [(0, 'Привет - "кафе"'), (1, 'Сегодня "закрыто"')], _texts(out)
    # a Latin file keeps its typographic quotes (table 00 has them, no switch); table 00 has
    # no em dash, so it folds (before #679 an em dash refused every Latin export)
    lat = f.build(plain, {"0": "«Bonjour» — “ami”"})
    assert lat[12:14] == b"00" and _texts(lat)[0] == (0, "«Bonjour» - “ami”"), _texts(lat)
    # a real blocker: Cyrillic + CJK in one subtitle names the CJK, not the Cyrillic letters
    try:
        f.build(plain, {"0": "Привет 你好", "1": "Сегодня"})
        raise AssertionError("CJK must be refused")
    except CaptionTargetError as exc:
        msg = str(exc)
        assert "subtitle 1" in msg and "'你'" in msg and "'П'" not in msg, msg
        assert "subtitle 2" not in msg, msg
    print("  [table] Russian quotes fold, old text no blocker, precise blocker (#679) OK")


def test_refusals():
    f = _f()
    try:
        f.build(STL, {"0": "x" * 150})
        raise AssertionError("an over-long subtitle must be refused")
    except CaptionTargetError as exc:
        assert "subtitle 1" in str(exc) and "does not fit" in str(exc), exc
    try:
        f.build(STL, {"0": "你好世界"})
        raise AssertionError("CJK must be refused")
    except CaptionTargetError as exc:
        assert "subtitle 1" in str(exc), exc
    # Greek into a file whose other subtitles hold Latin accents no Greek table has
    try:
        f.build(STL, {"0": "Γειά σου"})
        raise AssertionError("no table holds both é and Greek")
    except CaptionTargetError as exc:
        assert "code table" in str(exc), exc
    print("  [refuse] overflow and unencodable characters refuse with a list OK")


def test_language():
    f = _f()
    out = f.build_localized(STL, {}, "fr-FR")
    assert out[14:16] == b"0F" and out[16:] == STL[16:]
    assert f.build_localized(STL, {}, "nl-BE")[14:16] == b"2A"
    assert f.build_localized(STL, {}, "x-klingon")[14:16] == b"00"
    assert language_code("ru") == 0x56 and language_code("ar-EG") == 0x7E
    print("  [lang] LC follows the target language OK")


def test_hostile():
    f = _f()
    for bad in (b"solid cube\nfacet normal 0 0 1\n", b"\x00" * 84, STL[:1100], gsi(cct=b"09")):
        try:
            f.parse(bad)
            raise AssertionError("must refuse")
        except (StlParseError, InputLimitError):
            pass
    old = config.MAX_DOCUMENT_SEGMENTS
    config.MAX_DOCUMENT_SEGMENTS = 3
    try:
        f.parse(STL)
        raise AssertionError("segment cap must refuse")
    except InputLimitError:
        pass
    finally:
        config.MAX_DOCUMENT_SEGMENTS = old
    # a dangling diacritic and odd control bytes decode without crashing
    odd = gsi(tnb=1) + tti(1, b"ab\xc2\x8b\x9f\x7fend\xc8")
    assert f.build(odd, {}) == odd and len(f.parse(odd).units) == 1
    # gate (train-9): a huge translation is refused at once, not re-wrapped per width
    # (20,000 words took 13 s per subtitle at MNC 99 for STL, 3 s for SCC)
    import time

    from app.filters import scc_filter, stl_filter

    huge = ("word " * 20000).strip()
    t0 = time.monotonic()
    assert stl_filter._layout(huge, 2, 2, 99, {}) is None
    assert scc_filter._layout(huge, 2, 4, {}) is None
    assert time.monotonic() - t0 < 2
    assert stl_filter._layout("two short rows", 1, 2, 40, {}) == ["two short rows"]
    print("  [hostile] 3D-model STL, truncated, bad CCT, segment cap, odd bytes, huge text OK")


def test_pipeline():
    from tests import _caption_pipeline as cp

    client, doc_id = cp.upload("show.stl", STL, "application/octet-stream")
    assert cp.fill(doc_id, "Bon ") >= 5
    r = cp.export(client, doc_id)
    assert r.status_code == 200, (r.status_code, r.text[:300])
    out = r.content
    assert out[14:16] == b"0F", out[14:16]  # LC = French
    assert all(t.startswith("Bon") for _i, t in _texts(out)), _texts(out)
    assert _blocks(out)[3] == _blocks(STL)[3] and _blocks(out)[4] == _blocks(STL)[4]
    cp.fill(doc_id, "你好 ")
    r = cp.export(client, doc_id)
    assert r.status_code == 409 and "subtitle 1" in r.json()["error"], r.text[:400]
    print("  [pipeline] web upload -> export with LC; CJK refused with its subtitle OK")


def main():
    test_registered()
    test_identity()
    test_decode()
    test_translate()
    test_reflow_and_extension()
    test_table_switch()
    test_cyrillic_quotes_679()
    test_refusals()
    test_language()
    test_hostile()
    test_pipeline()
    print("STL FILTER OK")


if __name__ == "__main__":
    main()
