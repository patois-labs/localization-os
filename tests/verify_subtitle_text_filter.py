"""YouTube SBV, SubViewer 2.0 and MicroDVD (.sub) filters (Batch 10, fmt/subs-text).

  identity    an untouched build is byte-identical (CRLF, BOM, headers, blank lines kept)
  translated  cue text replaced in place; line breaks written as newline / [br] / |
  tokens      MicroDVD {y:i}-style codes are protected point tokens; {0} %s stay literal
  sniff       .sub told apart by content; ambiguous, SubViewer 1.0, VobSub, junk refused
  encodings   cp1252 / UTF-16 converted to UTF-8; unreadable refused (#509)
  caps        segment cap and segment length cap (#571)
  surfaces    API upload -> CLI translate -> API download; ambiguous .sub -> 422; /api/formats

Hermetic + offline. Fixtures are hand-written below. Final line: "SUBTITLE TEXT FILTER OK".
"""

import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(_ROOT)
sys.path.insert(0, _ROOT)
from tests._bootstrap import init_env

init_env("loctms_subs_text_")

from app.filters import get_filter, supported_exts
from app.filters.base import EmptyDocumentError, InputLimitError, UnsupportedEncodingError
from app.filters.subtitle_text_filter import UnrecognizedSubtitleError, sniff_sub

LOC = "fr-FR"

SBV = (
    b"0:00:00.599,0:00:04.160\n"
    b">> ALICE: Hi, my name is Alice.\n"
    b"\n"
    b"0:00:04.160,0:00:06.770\n"
    b"Line one {0} and %s\n"
    b"line two  \n"
    b"\n"
    b"0:00:06.770,0:00:08.000\n"
    b"\n"
    b"0:00:08.000,0:00:09.500\n"
    b"Caf\xc3\xa9 time.\n"
)

SV2 = (
    b"[INFORMATION]\r\n"
    b"[TITLE]Demo Title\r\n"
    b"[AUTHOR]Someone\r\n"
    b"[END INFORMATION]\r\n"
    b"[SUBTITLE]\r\n"
    b"[COLF]&HFFFFFF,[STYLE]bd,[SIZE]18,[FONT]Arial\r\n"
    b"00:00:01.00,00:00:04.00\r\n"
    b"Hello there.[br]How are you?\r\n"
    b"\r\n"
    b"00:00:05.50,00:00:07.25\r\n"
    b"Fine, thanks.\r\n"
    b"\r\n"
)

MDVD = (
    b"{1}{1}23.976\n"
    b"{DEFAULT}{C:$FF0000}{F:Arial}\n"
    b"{25}{50}{y:i}Hello!|How are you?\n"
    b"{60}{90}Plain {0} text\n"
    b"{100}{}{Y:b}\n"
    b"{120}{150}{c:$0000FF}Red|{y:b}Bold line\n"
)


def _round(fmt: str, data: bytes, mapping=None) -> bytes:
    return get_filter(fmt).build(data, mapping or {})


def _texts(fmt: str, data: bytes) -> list[str]:
    return [u.text for u in get_filter(fmt).parse(data).units]


def test_registry():
    exts = supported_exts()
    assert "sbv" in exts and "sub" in exts, exts
    assert get_filter("sbv").fmt == "sbv" and get_filter("sub").fmt == "sub"
    from app.services.mime import mime_for_fmt

    assert mime_for_fmt("sbv").startswith("text/plain") and mime_for_fmt("sub").startswith("text/")
    print("  [registry] sbv and sub registered with MIME types OK")


def test_sbv():
    assert _texts("sbv", SBV) == [
        ">> ALICE: Hi, my name is Alice.",
        "Line one {0} and %s\nline two",
        "Café time.",
    ], _texts("sbv", SBV)
    assert _round("sbv", SBV) == SBV
    same = {str(i): t for i, t in enumerate(_texts("sbv", SBV))}
    assert _round("sbv", SBV, same) == SBV, "identity mapping must be byte-identical"
    out = _round("sbv", SBV, {"1": "Ligne un {0} et %s\n\n  ligne deux", "2": "Pause café."})
    exp = SBV.replace(b"Line one {0} and %s\nline two", b"Ligne un {0} et %s\nligne deux")
    exp = exp.replace(b"Caf\xc3\xa9 time.", "Pause café.".encode())
    assert out == exp, out
    # CRLF and a UTF-8 BOM survive; a new line break uses the file's own newline.
    crlf = b"\xef\xbb\xbf" + SBV.replace(b"\n", b"\r\n")
    assert _round("sbv", crlf) == crlf
    out = _round("sbv", crlf, {"0": "Salut\nmoi"})
    assert out.startswith(b"\xef\xbb\xbf0:00:00.599,0:00:04.160\r\nSalut\r\nmoi\r\n\r\n"), out
    assert out.endswith(b"Caf\xc3\xa9 time.\r\n"), out
    print("  [sbv] parse, identity, translated, CRLF+BOM round trip OK")


def test_subviewer():
    assert sniff_sub(SV2) == "subviewer"
    assert _texts("sub", SV2) == ["Hello there.\nHow are you?", "Fine, thanks."]
    assert _round("sub", SV2) == SV2
    same = {str(i): t for i, t in enumerate(_texts("sub", SV2))}
    assert _round("sub", SV2, same) == SV2
    out = _round("sub", SV2, {"0": "Bonjour.\nCa va ?", "1": "Bien, merci.\nEt toi ?"})
    assert b"00:00:01.00,00:00:04.00\r\nBonjour.[br]Ca va ?\r\n" in out, out
    assert b"Bien, merci.[br]Et toi ?\r\n" in out, out
    assert out.startswith(SV2[: SV2.index(b"00:00:01.00")]), "header must be untouched"
    print("  [subviewer] header kept, [br] joins, translated round trip OK")


def test_microdvd():
    assert sniff_sub(MDVD) == "microdvd"
    units = get_filter("sub").parse(MDVD).units
    # A line-scoped code is a pair around its line's words (#680), at the start of a
    # several-line cue too; uppercase (whole-cue) leading codes stay outside the unit.
    assert [u.text for u in units] == [
        "⟦g1⟧Hello!⟦/g1⟧\nHow are you?",
        "Plain {0} text",
        "⟦g1⟧Red⟦/g1⟧\n⟦g2⟧Bold line⟦/g2⟧",
    ], [u.text for u in units]
    assert units[0].inline_map == {"g1": "{y:i}", "/g1": ""}, units[0].inline_map
    assert units[2].inline_map["g2"] == "{y:b}", units[2].inline_map
    assert _round("sub", MDVD) == MDVD
    same = {str(i): u.text for i, u in enumerate(units)}
    assert _round("sub", MDVD, same) == MDVD
    out = _round(
        "sub",
        MDVD,
        {
            "0": "⟦g1⟧Salut !⟦/g1⟧\nCa va ?",
            "1": "Texte {0} simple",
            # the token brings back its code; an invented token is dropped
            "2": "⟦g1⟧Rouge⟦/g1⟧\n⟦g2⟧Ligne grasse⟦/g2⟧ ⟦x9⟧",
        },
    )
    lines = out.decode().split("\n")
    assert lines[0] == "{1}{1}23.976" and lines[1].startswith("{DEFAULT}"), lines
    assert lines[2] == "{25}{50}{y:i}Salut !|Ca va ?", lines[2]
    assert lines[3] == "{60}{90}Texte {0} simple", lines[3]
    assert lines[4] == "{100}{}{Y:b}", lines[4]
    assert lines[5] == "{120}{150}{c:$0000FF}Rouge|{y:b}Ligne grasse", lines[5]
    assert "⟦" not in out.decode()
    # Gate: spaces around | are layout; the unit is trimmed per line and identity is exact.
    spaced = b"{10}{20}{0} items | %s\r\n"
    assert [u.text for u in get_filter("sub").parse(spaced).units] == ["{0} items\n%s"]
    assert _round("sub", spaced, {"0": "{0} items\n%s"}) == spaced
    assert _round("sub", spaced, {"0": "{0} Artikel\n%s"}) == b"{10}{20}{0} Artikel|%s\r\n"
    print("  [microdvd] fps/DEFAULT skipped, codes as tokens, | joins, round trip OK")


def test_microdvd_style_pairs_680():
    """#680: a line-scoped style code travels with its words when MT reorders lines."""
    f = get_filter("sub")
    src = (
        b"{1}{1}25\n"
        b"{10}{20}{y:i}Hello!|How are you?\n"
        b"{30}{40}Plain|{y:i}{c:$FF}styled words {Y:u}x\n"
        b"{50}{60}{Y:b}{y:i}Whole cue|second\n"
        b"{70}{80}{y:i}One line only\n"
        b"{90}{99}End{y:b}|next\n"
    )
    units = [u.text for u in f.parse(src).units]
    assert units == [
        "⟦g1⟧Hello!⟦/g1⟧\nHow are you?",
        "Plain\n⟦g1⟧styled words ⟦x2⟧x⟦/g1⟧",
        "⟦g1⟧Whole cue⟦/g1⟧\nsecond",  # {Y:b} (whole cue) stays outside
        "One line only",  # one line: the code styles the whole unit, outside
        "End⟦x1⟧\nnext",  # nothing after the code on its line: a point
    ], units
    assert f.build(src, {str(i): t for i, t in enumerate(units)}) == src
    # the reorder from the soak: the italic goes with "Salut", not onto the first line
    out = f.build(src, {"0": "Ca va ?\n⟦g1⟧Salut !⟦/g1⟧"}).decode().split("\n")
    assert out[1] == "{10}{20}Ca va ?|{y:i}Salut !", out[1]
    # a code that opened its line is written at the start of the line MT put it on
    out = f.build(src, {"0": "Oui ⟦g1⟧Salut !⟦/g1⟧\nCa va ?"}).decode().split("\n")
    assert out[1] == "{10}{20}{y:i}Oui Salut !|Ca va ?", out[1]
    # a line break inside a pair repeats the code (a code styles one line)
    out = f.build(src, {"1": "⟦g1⟧mots\nstyles ⟦x2⟧x⟦/g1⟧\nSimple"}).decode().split("\n")
    assert out[2] == "{30}{40}{y:i}{c:$FF}mots|{y:i}{c:$FF}styles {Y:u}x|Simple", out[2]
    # uppercase leading code kept, emptied pair dropped
    out = f.build(src, {"2": "Zweite\n⟦g1⟧⟦/g1⟧ganz"}).decode().split("\n")
    assert out[3] == "{50}{60}{Y:b}Zweite|ganz", out[3]
    # gate: a pair the translation breaks (missing, close first, re-kinded) loses its
    # code and keeps its words, never a refusal; a repeat is dropped
    for bad, want in (
        ("Ca va ?\nSalut !", "{10}{20}Ca va ?|Salut !"),
        ("⟦/g1⟧Salut⟦g1⟧ !", "{10}{20}Salut !"),
        ("⟦g1⟧a⟦/g1⟧ ⟦g1⟧b⟦/g1⟧", "{10}{20}{y:i}a b"),
        ("⟦x1⟧Salut !\nCa va ?", "{10}{20}Salut !|Ca va ?"),
        ("Salut⟦g1⟧ !", "{10}{20}Salut !"),
    ):
        out = f.build(src, {"0": bad}).decode().split("\n")
        assert out[1] == want, (bad, out[1])
    out = f.build(src, {"1": "⟦g1⟧a ⟦x2⟧b\nc⟦/g1⟧"}).decode().split("\n")
    assert out[2] == "{30}{40}{y:i}{c:$FF}a {Y:u}b|{y:i}{c:$FF}c", out[2]
    print("  [microdvd #680] line-scoped codes are pairs that travel with their words OK")


def test_sniff_refusals():
    cases = {
        "ambiguous": MDVD + b"\n00:00:01.00,00:00:04.00\nHi\n",
        "subviewer 1": b"[TITLE]\nx\n[SUBTITLE]\n[00:00:01]\nHello\n[00:00:04]\n",
        "vobsub": b"\x00\x00\x01\xba" + b"\x44" * 64,
        "junk": b"just some words\nnothing timed\n",
    }
    for name, data in cases.items():
        try:
            get_filter("sub").parse(data)
        except UnrecognizedSubtitleError as exc:
            assert isinstance(exc, EmptyDocumentError), name
            assert str(exc) and "—" not in str(exc), (name, str(exc))
        else:
            raise AssertionError(f"{name} .sub must be refused")
    print("  [sniff] ambiguous, SubViewer 1.0, VobSub and junk .sub refused OK")


def test_encodings():
    cp = "0:00:01.000,0:00:02.000\ncafé crème\n".encode("cp1252")
    p = get_filter("sbv").parse(cp)
    assert p.units[0].text == "café crème" and p.skeleton["source_encoding"] == "cp1252"
    out = get_filter("sbv").build(cp, {"0": "coffee"})
    assert out == b"0:00:01.000,0:00:02.000\ncoffee\n", out
    u16 = "{1}{2}naïve|ligne\n".encode("utf-16")
    assert _texts("sub", u16) == ["naïve\nligne"], _texts("sub", u16)
    assert _round("sub", u16) == u16  # B-bytes: untouched = as uploaded
    assert _round("sub", u16, {"0": "naïf\nligne"}) == "{1}{2}naïf|ligne\n".encode()
    try:
        get_filter("sbv").parse(b"0:00:01.000,0:00:02.000\ncaf\x81\n")
    except UnsupportedEncodingError:
        pass
    else:
        raise AssertionError("undecodable SBV must be refused")
    print("  [encodings] cp1252/UTF-16 converted, unreadable refused OK")


def test_caps():
    from app import config

    old_n, old_c = config.MAX_DOCUMENT_SEGMENTS, config.MAX_SEGMENT_CHARS
    try:
        config.MAX_DOCUMENT_SEGMENTS = 5
        many = b"".join(b"{%d}{%d}line %d\n" % (i, i + 1, i) for i in range(10, 30))
        for fmt, data in (
            ("sub", many),
            ("sbv", b"".join(b"0:00:01.000,0:00:02.000\nx%d\n\n" % i for i in range(20))),
        ):
            try:
                get_filter(fmt).parse(data)
            except InputLimitError:
                pass
            else:
                raise AssertionError(f"{fmt}: segment cap not enforced")
        config.MAX_DOCUMENT_SEGMENTS = old_n
        config.MAX_SEGMENT_CHARS = 50
        try:
            get_filter("sub").parse(b"{1}{2}" + b"a" * 80 + b"\n")
        except InputLimitError:
            pass
        else:
            raise AssertionError("segment length cap not enforced")
    finally:
        config.MAX_DOCUMENT_SEGMENTS, config.MAX_SEGMENT_CHARS = old_n, old_c
    print("  [caps] segment count and length caps enforced OK")


def test_hostile():
    # Unbalanced braces, huge frame numbers, stray control codes: no crash, no eval.
    data = b"{9999999999}{1}{{{y:i}x}}|\n{5}{6}{zz}\n{7}{8}" + b"{" * 5000 + b"\n"
    p = get_filter("sub").parse(data)
    assert _round("sub", data) == data
    assert len(p.units) >= 1
    print("  [hostile] malformed MicroDVD lines parse and round-trip OK")


def test_surfaces():
    from fastapi.testclient import TestClient
    from sqlmodel import Session

    from app.db import engine, init_db
    from app.main import app
    from app.models import Project, Role, TargetLocale
    from app.services import apikeys, security
    from tests._cli_runner import run_cli

    init_db()
    with Session(engine) as s:
        p = Project(name="subs", org_id=1, source_locale="en-US", default_provider="mock")
        s.add(p)
        s.commit()
        s.refresh(p)
        s.add(TargetLocale(project_id=p.id, locale=LOC))
        s.commit()
        pid = p.id
    user = security.create_user("subs@x.test", "subs", Role.ADMIN, "subs-pw-123456")
    _row, raw = apikeys.create_key(user.id, "subs", scopes="api docs admin")
    client = TestClient(app, raise_server_exceptions=False, follow_redirects=False)
    hdr = {"X-Loc-Token": raw}  # pregate: allow-secret throwaway test key

    r = client.get("/api/formats", headers=hdr)
    assert r.status_code == 200, r.text
    exts = {f["ext"]: f["name"] for f in r.json()["formats"]}
    assert "sbv" in exts and "sub" in exts, exts

    for name, data, keep in (
        ("a.sbv", SBV, b"0:00:04.160,0:00:06.770\n"),
        ("b.sub", MDVD, b"{1}{1}23.976\n{DEFAULT}"),
        ("c.sub", SV2, b"[INFORMATION]\r\n[TITLE]Demo Title\r\n"),
    ):
        r = client.post(
            "/api/documents",
            headers=hdr,
            data={"project_id": str(pid)},
            files={"file": (name, data, "text/plain")},
        )
        assert r.status_code in (200, 201), (name, r.status_code, r.text)
        doc = r.json()["document_id"]
        code, out, err = run_cli(["doc", "translate", str(doc), "--locale", LOC])
        assert code == 0, (name, code, out, err)
        r = client.get(f"/api/documents/{doc}/download", headers=hdr, params={"locale": LOC})
        assert r.status_code == 200, (name, r.status_code, r.text)
        body = r.content
        assert keep in body, (name, body)
        assert b"\xe2\x9f\xa6" not in body, (name, body)  # no leaked token brackets
        assert body != data, (name, "export should carry translations")
        if name == "b.sub":
            assert b"{25}{50}{y:i}" in body, body
    r = client.post(
        "/api/documents",
        headers=hdr,
        data={"project_id": str(pid)},
        files={"file": ("bad.sub", MDVD + b"00:00:01.00,00:00:04.00\nHi\n", "text/plain")},
    )
    assert r.status_code == 422, (r.status_code, r.text)
    assert "ambiguous" in r.json()["error"], r.text
    print("  [surfaces] API upload, CLI translate, download; ambiguous .sub -> 422 OK")


def main():
    test_registry()
    test_sbv()
    test_subviewer()
    test_microdvd()
    test_microdvd_style_pairs_680()
    test_sniff_refusals()
    test_encodings()
    test_caps()
    test_hostile()
    test_surfaces()
    print("SUBTITLE TEXT FILTER OK")


if __name__ == "__main__":
    main()
