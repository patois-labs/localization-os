"""AutoCAD DXF (ASCII) filter: parse, byte-identical round trip, MTEXT codes and re-chunking,
$DWGCODEPAGE / UTF-8 encodings, \\U+XXXX, hostile input (formats parity sprint, fmt/dxf).

Hand-written fixtures only. Hermetic + offline. Final line: "DXF FILTER OK".
"""

import os
import re
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(_ROOT)
sys.path.insert(0, _ROOT)
from tests._bootstrap import init_env

init_env("loctms_dxffilter_")

from app.filters import get_filter, supported_exts
from app.filters.base import InputLimitError, UnsupportedEncodingError

F = get_filter("dxf")


def dxf(pairs, nl="\r\n", enc="utf-8") -> bytes:
    out = []
    for code, val in pairs:
        out.append(f"{code:>3}{nl}{val}{nl}")
    return "".join(out).encode(enc)


def header(ver="AC1032", cp="ANSI_1252"):
    h = [(0, "SECTION"), (2, "HEADER"), (9, "$ACADVER"), (1, ver)]
    if cp:
        h += [(9, "$DWGCODEPAGE"), (3, cp)]
    return [*h, (9, "$INSUNITS"), (70, "4"), (0, "ENDSEC")]


LONG = ("The contractor shall verify all dimensions on site before fabrication. " * 5).strip()
MT = r"{\fArial|b1|i0|c0|p34;Bold note} and \H2.5x;tall\Pnext line with \S1/2; inch and \U+00E9t\~e"
CHUNKS = [LONG[:250], LONG[250:]]


def sample_pairs(ver="AC1032", cp="ANSI_1252"):
    return [
        *header(ver, cp),
        (0, "SECTION"),
        (2, "TABLES"),
        (0, "TABLE"),
        (2, "LAYER"),
        (5, "2"),
        (0, "LAYER"),
        (5, "10"),
        (2, "Walls Text"),
        (70, "0"),
        (62, "7"),
        (6, "CONTINUOUS"),
        (0, "ENDTAB"),
        (0, "ENDSEC"),
        (0, "SECTION"),
        (2, "BLOCKS"),
        (0, "BLOCK"),
        (5, "20"),
        (8, "0"),
        (2, "TITLEBLOCK"),
        (70, "2"),
        (1, "xref path text"),
        (0, "ATTDEF"),
        (5, "21"),
        (8, "0"),
        (1, "Untitled"),
        (3, "Enter drawing title"),
        (2, "TITLE"),
        (0, "TEXT"),
        (5, "22"),
        (8, "0"),
        (1, "Patois Labs Engineering"),
        (0, "ENDBLK"),
        (5, "23"),
        (0, "ENDSEC"),
        (0, "SECTION"),
        (2, "ENTITIES"),
        (0, "TEXT"),
        (5, "30"),
        (8, "Walls Text"),
        (10, "1.0"),
        (1, "Living room"),
        (7, "Standard"),
        (0, "TEXT"),
        (5, "31"),
        (8, "Walls Text"),
        (1, "1200"),
        (0, "TEXT"),
        (5, "32"),
        (8, "Walls Text"),
        (1, "%%c50 pipe, 45%%d bend"),
        (0, "MTEXT"),
        (5, "33"),
        (8, "Notes"),
        (3, CHUNKS[0]),
        (1, CHUNKS[1]),
        (7, "Standard"),
        (1001, "ACAD"),
        (1000, "do not translate me"),
        (0, "MTEXT"),
        (5, "34"),
        (8, "Notes"),
        (1, MT),
        (0, "INSERT"),
        (5, "35"),
        (2, "TITLEBLOCK"),
        (66, "1"),
        (0, "ATTRIB"),
        (5, "36"),
        (1, "Ground floor plan"),
        (2, "TITLE"),
        (0, "SEQEND"),
        (5, "37"),
        (0, "DIMENSION"),
        (5, "38"),
        (2, "*D1"),
        (1, "<> typical"),
        (3, "Standard"),
        (0, "DIMENSION"),
        (5, "39"),
        (2, "*D2"),
        (1, "<>"),
        (3, "Standard"),
        (0, "MULTILEADER"),
        (5, "3A"),
        (300, "CONTEXT_DATA{"),
        (304, r"Main\Pentrance"),
        (301, "}"),
        (0, "ENDSEC"),
        (0, "SECTION"),
        (2, "OBJECTS"),
        (0, "DICTIONARY"),
        (5, "C"),
        (3, "ACAD_GROUP"),
        (350, "D"),
        (0, "ENDSEC"),
        (0, "EOF"),
    ]


SAMPLE = dxf(sample_pairs())


def pairs_of(b: bytes, enc="utf-8"):
    lines = re.split(r"\r\n|\n|\r", b.decode(enc))
    if lines and lines[-1] == "":
        lines.pop()
    return [(int(lines[i]), lines[i + 1]) for i in range(0, len(lines), 2)]


def texts(b):
    return [u.text for u in F.parse(b).units]


def test_registered():
    assert F is not None and F.fmt == "dxf"
    assert "dxf" in supported_exts()
    print("  [dxf] registered OK")


def test_parse_units():
    units = F.parse(SAMPLE).units
    got = [(u.style, u.text) for u in units]
    assert got[0] == ("dxf:attdef", "Untitled"), got
    assert got[1] == ("dxf:prompt", "Enter drawing title"), got
    assert got[2] == ("dxf:text", "Patois Labs Engineering"), got
    assert got[3] == ("dxf:text", "Living room"), got
    assert got[4] == ("dxf:text", "⟦x1⟧50 pipe, 45⟦x2⟧ bend"), got
    assert got[5] == ("dxf:mtext", LONG), got
    assert got[6][0] == "dxf:mtext"
    assert got[6][1] == (
        "⟦g1⟧⟦x2⟧Bold note⟦/g1⟧ and ⟦x3⟧tall⟦x4⟧next line with ⟦x5⟧ inch and \u00e9t\u00a0e"
    ), got[6]
    assert got[7] == ("dxf:attrib", "Ground floor plan"), got
    assert got[8] == ("dxf:dimension", "⟦x1⟧ typical"), got
    assert got[9] == ("dxf:mleader", "Main⟦x1⟧entrance"), got
    assert len(got) == 10, got  # "1200", bare "<>", layer/block names, xdata, objects: never units
    all_text = " ".join(t for _s, t in got)
    for never in (
        "Walls Text",
        "TITLEBLOCK",
        "xref path",
        "do not translate",
        "ACAD_GROUP",
        "Standard",
    ):
        assert never not in all_text, never
    print("  [dxf] parse: TEXT, MTEXT chunks, ATTDEF/ATTRIB, DIMENSION, MULTILEADER OK")


def test_identity_round_trip():
    for nl in ("\r\n", "\n"):
        for ver, cp, enc in (("AC1032", "ANSI_1252", "utf-8"), ("AC1015", "ANSI_1252", "cp1252")):
            data = dxf(sample_pairs(ver, cp), nl, enc)
            assert F.build(data, {}) == data
            same = {str(u.p_index): u.text for u in F.parse(data).units}
            assert F.build(data, same) == data
    bom = b"\xef\xbb\xbf" + SAMPLE
    assert F.build(bom, {str(u.p_index): u.text for u in F.parse(bom).units}) == bom
    print("  [dxf] identity round trip (CRLF/LF, UTF-8/cp1252, BOM) byte-identical OK")


def test_translated_round_trip():
    units = F.parse(SAMPLE).units
    tr = {
        "0": "Sans titre",
        "1": "Saisir le titre du dessin",
        "3": "Séjour",
        "4": "⟦x1⟧50 tuyau, coude 45⟦x2⟧",
        "5": "L'entrepreneur doit vérifier toutes les cotes sur place avant la fabrication. " * 7,
        "6": "⟦g1⟧⟦x2⟧Note en gras⟦/g1⟧ et ⟦x3⟧grand⟦x4⟧ligne suivante ⟦x5⟧ pouce",
        "7": "Plan du rez-de-chaussée",
        "8": "⟦x1⟧ typique",
        "9": "Entrée⟦x1⟧principale",
    }
    out = F.build(SAMPLE, tr)
    back = F.parse(out).units
    assert len(back) == len(units)
    for k, v in tr.items():
        assert back[int(k)].text == v, (k, back[int(k)].text)
    # every non-text group is identical and in the same order
    a = list(pairs_of(SAMPLE))
    b = list(pairs_of(out))
    strip = lambda ps: [p for p in ps if p[0] not in (1, 3, 304)]  # noqa: E731
    assert strip(a) == strip(b)
    # untranslated unit (index 2) and the never-text groups kept verbatim
    assert (1, "Patois Labs Engineering") in b and (1, "1200") in b and (3, "Standard") in b
    assert (
        (1, "xref path text") in b and (1000, "do not translate me") in b and (3, "ACAD_GROUP") in b
    )
    assert (
        1,
        r"{\fArial|b1|i0|c0|p34;Note en gras} et \H2.5x;grand\Pligne suivante \S1/2; pouce",
    ) in b
    assert (304, r"Entrée\Pprincipale") in b and (1, "<> typique") in b
    # MTEXT re-chunked: group 3 pieces of <= 250 chars then a final group 1 under 250
    i = b.index((5, "33"))
    chunk = []
    for code, val in b[i + 1 :]:
        if code in (1, 3):
            chunk.append((code, val))
        elif chunk:
            break
    assert [c for c, _ in chunk[:-1]] == [3] * (len(chunk) - 1) and chunk[-1][0] == 1, chunk
    assert all(len(v) <= 250 for _c, v in chunk) and len(chunk[-1][1]) < 250
    assert "".join(v for _c, v in chunk) == tr["5"]
    print("  [dxf] translated round trip: values spliced, MTEXT re-chunked, rest identical OK")


def test_short_mtext_collapses_chunks():
    out = F.build(SAMPLE, {"5": "Verify dimensions."})
    b = pairs_of(out)
    i = b.index((5, "33"))
    assert b[i + 1 : i + 4] == [(8, "Notes"), (1, "Verify dimensions."), (7, "Standard")], b[
        i : i + 5
    ]
    # only the MTEXT lines changed: everything before it is a byte prefix
    pos = SAMPLE.index(CHUNKS[0].encode())
    assert out[: pos - 5] == SAMPLE[: pos - 5]
    assert out.endswith(SAMPLE[SAMPLE.index(b"\r\n  7\r\nStandard\r\n1001") :])
    print("  [dxf] short MTEXT: chunks collapse to one group 1 in place OK")


def test_inline_tokens_and_escaping():
    # a dropped closing brace token drops its opener too; literal braces/backslash escaped
    out = F.build(SAMPLE, {"6": "⟦g1⟧⟦x2⟧Gras { a\\b } x^y\nnext"})
    b = pairs_of(out)
    v = b[b.index((5, "34")) + 2][1]
    assert v == r"\fArial|b1|i0|c0|p34;Gras \{ a\\b \} x^ y\Pnext", v
    # TEXT: newline becomes a space, %% is tripled per percent, caret escaped
    out = F.build(SAMPLE, {"3": "Salon\n100%% ^ok"})
    assert (1, "Salon 100%%%%%% ^ ok") in pairs_of(out)
    # unknown tokens dropped, known ones kept
    out = F.build(SAMPLE, {"4": "⟦x9⟧tuyau ⟦x1⟧50"})
    assert (1, "tuyau %%c50") in pairs_of(out)
    print("  [dxf] inline tokens: balanced braces, escapes, %% and ^ OK")


def test_legacy_code_page_and_unicode_escape():
    pairs = [
        *header("AC1015", "ANSI_1251"),
        (0, "SECTION"),
        (2, "ENTITIES"),
        (0, "TEXT"),
        (5, "1"),
        (1, "Гостиная"),
        (0, "TEXT"),
        (5, "2"),
        (1, r"Caf\U+00E9 \U+4E2D"),
        (0, "MTEXT"),
        (5, "3"),
        (1, r"Plan\M+18A8Aend"),
        (0, "ENDSEC"),
        (0, "EOF"),
    ]
    data = dxf(pairs, enc="cp1251")
    res = F.parse(data)
    assert res.skeleton.get("source_encoding") == "cp1251", res.skeleton
    assert [u.text for u in res.units] == [
        "Гостиная",
        "Café 中",
        "Plan" + bytes.fromhex("8A8A").decode("cp932") + "end",
    ], [u.text for u in res.units]
    assert F.build(data, {"0": "Гостиная"}) == data
    out = F.build(data, {"0": "リビング Комната", "1": "Кафе"})
    assert "Комната".encode("cp1251") in out and b"\\U+30EA\\U+30D3\\U+30F3\\U+30B0" in out, out
    assert [u.text for u in F.parse(out).units][:2] == ["リビング Комната", "Кафе"]
    # long Japanese MTEXT in a legacy page: re-chunked without splitting a \U+ escape
    out = F.build(data, {"2": "日本語のテキスト" * 30})
    vals = [v for c, v in pairs_of(out, "cp1251") if c in (1, 3)]
    assert all(len(v) <= 250 and not re.search(r"\\(U\+?[0-9A-F]{0,3})?$", v) for v in vals), vals
    assert F.parse(out).units[2].text == "日本語のテキスト" * 30
    # non-BMP: surrogate pair escapes, decoded back
    out = F.build(data, {"1": "Cafe \U0001f600"})
    assert b"\\U+D83D\\U+DE00" in out and F.parse(out).units[1].text == "Cafe \U0001f600"
    print("  [dxf] $DWGCODEPAGE decode/encode, \\U+ and \\M+ escapes, overflow as \\U+ OK")


def test_encoding_selection():
    body = [(0, "SECTION"), (2, "ENTITIES"), (0, "TEXT"), (1, "Größe"), (0, "ENDSEC"), (0, "EOF")]
    # R12 with no header: cp1252 unless valid UTF-8
    r12 = dxf(body, enc="cp1252")
    assert texts(r12) == ["Größe"] and F.parse(r12).skeleton["source_encoding"] == "cp1252"
    u8 = dxf(body)
    assert texts(u8) == ["Größe"] and "source_encoding" not in F.parse(u8).skeleton
    # AC1021+ is UTF-8 whatever $DWGCODEPAGE says
    data = dxf(header("AC1021", "ANSI_1251") + body)
    assert texts(data) == ["Größe"]
    # invalid UTF-8 in an AC1021 value: refused
    bad = dxf(header("AC1027") + body, enc="cp1252")
    try:
        F.parse(bad)
        raise AssertionError("invalid UTF-8 accepted")
    except UnsupportedEncodingError as exc:
        assert "utf-8" in str(exc)
    # unknown code page refused
    try:
        F.parse(dxf(header("AC1015", "ANSI_9999") + body))
        raise AssertionError("unknown code page accepted")
    except UnsupportedEncodingError as exc:
        assert "ANSI_9999" in str(exc)
    print("  [dxf] encoding selection: AC1021+ UTF-8, R12 default, refusals OK")


def test_hostile_inputs():
    from app import config

    binary = b"AutoCAD Binary DXF\r\n\x1a\x00" + b"\x00\x00SECTION\x00"
    for bad, why in (
        (binary, "binary"),
        (b"hello world\nthis is not dxf\n", "not a valid"),
        (b"  0\r\nSECTION\r\n  2\r\nENTITIES\r\nnot-a-code\r\nx\r\n", "line 5"),
        (b"%PDF-1.7\n", "not a valid"),
    ):
        try:
            F.parse(bad)
            raise AssertionError(f"accepted: {why}")
        except InputLimitError as exc:
            assert why.lower() in str(exc).lower(), (why, str(exc))
    old = config.MAX_DOCUMENT_SEGMENTS
    try:
        config.MAX_DOCUMENT_SEGMENTS = 3
        many = [(0, "SECTION"), (2, "ENTITIES")] + [(0, "TEXT"), (1, "word")] * 5 + [(0, "EOF")]
        try:
            F.parse(dxf(many))
            raise AssertionError("segment cap not enforced")
        except InputLimitError as exc:
            assert "MAX_DOCUMENT_SEGMENTS" in str(exc)
        try:
            F.parse(b"  0\n" + b"999\nx\n" * 1300)
            raise AssertionError("line budget not enforced")
        except InputLimitError as exc:
            assert "group codes" in str(exc)
    finally:
        config.MAX_DOCUMENT_SEGMENTS = old
    # unbalanced braces and a dangling backslash never crash, and round-trip identically
    odd = dxf(
        [
            (0, "SECTION"),
            (2, "ENTITIES"),
            (0, "MTEXT"),
            (1, "{\\fArial;open } } { \\"),
            (0, "ENDSEC"),
            (0, "EOF"),
        ]
    )
    t = texts(odd)
    assert len(t) == 1 and F.build(odd, {"0": t[0]}) == odd
    rebuilt = F.build(odd, {"0": t[0].replace("open", "ouvert")})
    assert texts(rebuilt) == [t[0].replace("open", "ouvert")]
    # an empty / text-free drawing has no units
    assert (
        texts(
            dxf(
                [(0, "SECTION"), (2, "ENTITIES"), (0, "TEXT"), (1, "  "), (0, "ENDSEC"), (0, "EOF")]
            )
        )
        == []
    )
    print("  [dxf] hostile: binary refused, malformed refused, caps, odd braces OK")


def test_end_to_end_upload_export():
    from fastapi.testclient import TestClient
    from sqlmodel import Session, select

    from app.db import engine, init_db
    from app.main import app
    from app.models import Segment

    init_db()
    client = TestClient(app)
    created = client.post(
        "/projects",
        data={"name": "DXF e2e", "source_locale": "en-US", "target_locales": "de-DE"},
        follow_redirects=False,
    )
    pid = int(created.headers["location"].rsplit("/", 1)[-1])
    uploaded = client.post(
        f"/projects/{pid}/documents",
        data={"mode": "segmented"},
        files={"file": ("plan.dxf", SAMPLE)},
        follow_redirects=False,
    )
    assert uploaded.status_code in (302, 303), uploaded.text[:400]
    did = int(uploaded.headers["location"].rsplit("/", 1)[-1])
    with Session(engine) as session:
        rows = session.exec(select(Segment).where(Segment.document_id == did)).all()
        by_src = {r.source_text: r.id for r in rows}
    assert "Living room" in by_src, sorted(by_src)
    saved = client.post(
        f"/segments/{by_src['Living room']}/save", data={"target_text": "Wohnzimmer"}
    )
    assert saved.status_code == 200, saved.text[:300]
    exported = client.get(f"/documents/{did}/export?locale=de-DE")
    assert exported.status_code == 200, exported.text[:300]
    assert exported.content == SAMPLE.replace(b"\r\nLiving room\r\n", b"\r\nWohnzimmer\r\n")
    print("  [dxf] end to end: upload -> save -> export splices one value OK")


if __name__ == "__main__":
    test_registered()
    test_parse_units()
    test_identity_round_trip()
    test_translated_round_trip()
    test_short_mtext_collapses_chunks()
    test_inline_tokens_and_escaping()
    test_legacy_code_page_and_unicode_escape()
    test_encoding_selection()
    test_hostile_inputs()
    test_end_to_end_upload_export()
    print("DXF FILTER OK")
