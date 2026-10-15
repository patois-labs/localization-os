"""Scenarist SCC caption filter (CEA-608): app/filters/scc_filter.py.

  identity   parse + build with no changes is byte-identical (CRLF, LF, BOM, blank lines).
  decode     pop-on, roll-up and paint-on text; basic, special and extended characters;
             mid-row codes as tokens; the redundancy rule for doubled codes.
  translate  only changed caption lines are re-encoded; timecodes and codes are kept.
  layout     the 32-cell rows: own breaks kept, balanced re-wrap, extra rows move up,
             columns move left, roll-up adds carriage returns.
  refuse     characters 608 cannot show and text that cannot fit refuse the export.
  skip       channel 2, backspace and multi-caption lines are kept verbatim.
  hostile    no header, bad words, huge lines, segment cap.

Fixtures are hand-written hex below (parity computed by hand). Final line: "SCC FILTER OK".
"""

import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(_ROOT)
sys.path.insert(0, _ROOT)
from tests._bootstrap import init_env

init_env("loctms_scc_")

from app import config
from app.filters import get_filter, supported_exts
from app.filters.base import InputLimitError
from app.filters.scc_filter import CaptionTargetError, SccParseError, _word

LINES = [
    "Scenarist_SCC V1.0",
    "",
    # pop-on: ENM, RCL, PAC row 14 col 0 "HELLO", PAC row 15 "WORLD", EOC
    "00:00:01:00\t94ae 94ae 9420 9420 9440 9440 c845 4c4c 4f80 94e0 94e0 574f 524c c480 942f 942f",
    "",
    "00:00:03:00\t942c 942c",
    "",
    # roll-up 2: RU2, CR, PAC row 15, "I say " + italics mid-row + "hi! "
    "00:00:04:00\t9425 9425 94ad 94ad 9470 9470 4920 7361 7920 91ae 91ae 68e9 a120",
    # roll-up continues: CR, PAC, "U" + extended U-umlaut, "nd", special music note
    "00:00:05:00\t94ad 94ad 9470 9470 d580 92a4 92a4 6e64 9137 9137",
    # paint-on (drop-frame timecode): RDC, PAC row 1 indent 8 + tab offset 2, "caf" + e-acute
    "00:00:06;02\t9429 9429 9154 9154 97a2 97a2 e361 e6dc",
    # channel 2 (CC2) text: kept verbatim, not a unit
    "00:00:07:00\t1c20 1c20 1c70 1c70 c1c2",
    # backspace inside the text: kept verbatim
    "00:00:08:00\t9420 9420 9470 9470 c1c2 94a1 94a1 43c4 942f 942f",
]
SCC = "\r\n".join(LINES) + "\r\n"


def _f():
    return get_filter("scc")


def _texts(data):
    return [(u.p_index, u.text) for u in _f().parse(data).units]


def _line(data, tc):
    return next(ln for ln in data.decode("utf-8-sig").splitlines() if ln.startswith(tc))


def test_registered():
    assert "scc" in supported_exts()
    print("  [reg] .scc registered OK")


def test_identity():
    f = _f()
    for data in (SCC.encode(), SCC.replace("\r\n", "\n").encode(), b"\xef\xbb\xbf" + SCC.encode()):
        pr = f.parse(data)
        assert f.build(data, {}) == data
        assert f.build(data, {str(u.p_index): u.text for u in pr.units}) == data
    print("  [identity] untouched and same-text builds byte-identical (CRLF, LF, BOM) OK")


def test_decode():
    got = _texts(SCC.encode())
    assert got == [
        (0, "HELLO\nWORLD"),
        (1, "I say ⟦x1⟧hi!"),
        (2, "Ünd♪"),
        (3, "café"),
    ], got
    sk = _f().parse(SCC.encode()).skeleton
    assert sk["scc_lines_kept_verbatim"] == 2, sk
    print("  [decode] pop-on, roll-up, paint-on, special/extended chars, mid-row OK")


def test_translate():
    f = _f()
    data = SCC.encode()
    out = f.build(data, {"0": "BONJOUR\nMONDE", "3": "thé"})
    o = out.decode().splitlines()
    s = SCC.splitlines()
    for a, b in zip(o, s, strict=True):
        if not (a.startswith("00:00:01:00") or a.startswith("00:00:06;02")):
            assert a == b, (a, b)
    # same rows, same PACs; text re-encoded with parity; EOC kept at the end
    assert _line(out, "00:00:01:00") == (
        "00:00:01:00\t94ae 94ae 9420 9420 9440 9440 c24f ce4a 4fd5 5280"
        " 94e0 94e0 cd4f cec4 4580 942f 942f"
    ), _line(out, "00:00:01:00")
    assert _texts(out)[0] == (0, "BONJOUR\nMONDE") and _texts(out)[3] == (3, "thé")
    # extended and special characters: fallback + extended code, doubled specials
    out = f.build(data, {"2": "Über ♪ «oui»"})
    assert _texts(out)[2] == (2, "Über ♪ «oui»"), _texts(out)
    assert "d580 92a4 92a4" in _line(out, "00:00:05:00")
    assert "9137 9137" in _line(out, "00:00:05:00")
    # the mid-row token takes the place of the space before it
    out = f.build(data, {"1": "Je dis ⟦x1⟧salut !"})
    assert _texts(out)[1] == (1, "Je dis ⟦x1⟧salut !")
    assert "4ae5 2064 e973 91ae 91ae" in _line(out, "00:00:04:00"), _line(out, "00:00:04:00")
    # typographic apostrophe and dashes map to 608 equivalents
    out = f.build(data, {"3": "l’été – oui…"})
    assert _texts(out)[3] == (3, "l'été - oui..."), _texts(out)
    print("  [translate] only changed lines, parity, tokens, extended/special OK")


def test_layout():
    f = _f()
    data = SCC.encode()
    # one long line re-wrapped into the caption's two rows, balanced
    out = f.build(data, {"0": "BONJOUR TOUT LE MONDE, COMMENT ALLEZ-VOUS CE SOIR ?"})
    rows = _texts(out)[0][1].split("\n")
    assert len(rows) == 2 and all(len(r) <= 32 for r in rows), rows
    # four rows needed: rows 12-15, bottom row kept at 15
    long = " ".join(["Voici une phrase vraiment longue"] * 4)
    out = f.build(data, {"0": long})
    ln = _line(out, "00:00:01:00")
    for pac in ("1340", "13e0", "9440", "94e0"):
        assert f"{pac} {pac}" in ln, (pac, ln)
    assert len(_texts(out)[0][1].split("\n")) == 4
    # paint-on row at column 10 (indent 8 + tab 2): a 30-cell row moves left to column 2
    out = f.build(data, {"3": "x" * 30})
    ln = _line(out, "00:00:06;02")
    assert ln.startswith("00:00:06;02\t9429 9429 91d0 91d0 97a2 97a2 "), ln  # indent 0 + TO2
    # roll-up: a second row is added with a carriage return on the same base row
    out = f.build(data, {"1": "Une phrase assez longue pour deux lignes de roll-up"})
    ln = _line(out, "00:00:04:00")
    assert ln.count("94ad 94ad") == 2 and ln.count("9470 9470") == 2, ln
    assert len(_texts(out)[1][1].split("\n")) == 2
    print("  [layout] 32-cell rows, balanced wrap, rows move up, columns move left OK")


def test_refusals():
    f = _f()
    data = SCC.encode()
    try:
        f.build(data, {"0": "Привет мир", "3": "日本語"})
        raise AssertionError("Cyrillic and CJK must be refused")
    except CaptionTargetError as exc:
        msg = str(exc)
        assert "caption 1 at 00:00:01:00" in msg and "caption 4 at 00:00:06;02" in msg, msg
    try:
        f.build(data, {"1": "word " * 40})  # roll-up depth 2: at most 2 rows
        raise AssertionError("overflow must be refused")
    except CaptionTargetError as exc:
        assert "does not fit in 2 rows of 32" in str(exc), exc
    print("  [refuse] non-608 characters and overflow refuse with a list OK")


def test_hostile():
    f = _f()
    bad = [
        b"00:00:01:00\t9420 9420\n",  # no header
        b"Scenarist_SCC V1.0\n\n00:00:01:00\t9420 94zz\n",
        b"Scenarist_SCC V1.0\n\nnot a caption line\n",
        b"Scenarist_SCC V1.0\n\n00:00:01:00\t" + b" ".join([b"9420"] * 5000) + b"\n",
    ]
    for data in bad:
        try:
            f.parse(data)
            raise AssertionError(f"must refuse {data[:40]!r}")
        except (SccParseError, InputLimitError):
            pass
    old = config.MAX_DOCUMENT_SEGMENTS
    config.MAX_DOCUMENT_SEGMENTS = 2
    try:
        f.parse(SCC.encode())
        raise AssertionError("segment cap must refuse")
    except InputLimitError:
        pass
    finally:
        config.MAX_DOCUMENT_SEGMENTS = old
    assert (
        _word(0x14, 0x20) == "9420" and _word(0x14, 0x2F) == "942f" and _word(0x11, 0x37) == "9137"
    )
    print("  [hostile] no header, bad words, huge line, segment cap; parity table OK")


def test_pipeline():
    from tests import _caption_pipeline as cp

    client, doc_id = cp.upload("show.scc", SCC.encode(), "text/plain")
    assert cp.fill(doc_id, "SALUT ") >= 4
    r = cp.export(client, doc_id)
    assert r.status_code == 200, (r.status_code, r.text[:300])
    texts = [t for _i, t in _texts(r.content)]
    assert len(texts) == 4 and all(t.startswith("SALUT") for t in texts), texts
    assert _line(r.content, "00:00:07:00") == _line(SCC.encode(), "00:00:07:00")
    cp.fill(doc_id, "Привет ")
    r = cp.export(client, doc_id)
    assert r.status_code == 409 and "caption 1 at 00:00:01:00" in r.json()["error"], r.text[:400]
    print("  [pipeline] web upload -> export; a non-608 translation is refused with its caption OK")


def main():
    test_registered()
    test_identity()
    test_decode()
    test_translate()
    test_layout()
    test_refusals()
    test_hostile()
    test_pipeline()
    print("SCC FILTER OK")


if __name__ == "__main__":
    main()
