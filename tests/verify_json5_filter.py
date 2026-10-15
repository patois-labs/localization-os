"""JSON5 / JSONC filter (.json5, .jsonc).

  identity   build with no changes (and with every unit unchanged) is byte-identical for both
             fixtures, a UTF-8 BOM file, a CRLF file and a file with \\u escapes
  walk       string VALUES only, json_filter's _is_translatable rule; keys, numbers (hex,
             .5, +Infinity, NaN), true/false/null and no-letter strings skipped; a plain JSON
             file gives exactly the JsonFilter units
  translate  translated round trip re-parses to the translations; comments, trailing commas,
             unquoted keys and quote style kept; quotes, backslashes, newlines, control chars
             and U+2028 re-escaped for the original quote style
  tokens     {name}, %1$s, {{user}} and <b> survive as protected inline tokens
  records    dotted keys, comment above a key (same-line comment excluded), unit_index
  encodings  BOM kept; UTF-16 converted (source_encoding recorded); binary refused
  hostile    depth cap, unterminated string/comment, bad escapes, lone surrogate, double comma,
             trailing junk, budgets and segment/char caps refuse; a large file parses fast;
             an unterminated string after 1 MB of text does not backtrack
  surfaces   registry, upload accept list, msgraph map, MIME, API labels, help, llms.txt,
             CLI.md; API upload + CLI translate + API/CLI export of a .jsonc end to end

Hermetic + offline. Final line: "JSON5 FILTER OK".
"""

import os
import sys
import time

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(_ROOT)
sys.path.insert(0, _ROOT)
from tests._bootstrap import init_env

init_env("loctms_json5filter_")

from app.filters import get_filter, supported_exts
from app.filters.base import InputLimitError, UnsupportedEncodingError

FIX = os.path.join(_ROOT, "tests", "fixtures", "formats", "json5")
F = get_filter("json5")
_TEST_PW = "json5-pw-1234567"  # pregate: allow-secret throwaway test user


def _fixture(name: str) -> bytes:
    with open(os.path.join(FIX, name), "rb") as fh:
        return fh.read()


def _texts(data: bytes) -> list[str]:
    return [u.text for u in F.parse(data).units]


def _refuses(data: bytes, exc=ValueError, word: str = "JSON5") -> None:
    try:
        F.parse(data)
    except exc as e:
        assert word in str(e), e
    else:
        raise AssertionError(f"must refuse {data[:40]!r}")


def test_identity():
    for name in ("app.json5", "settings.jsonc"):
        src = _fixture(name)
        assert F.build(src, {}) == src, name
        same = {str(u.p_index): u.text for u in F.parse(src).units}
        assert F.build(src, same) == src, name
    bom = b'\xef\xbb\xbf{"a": "Hello"}\n'
    assert F.build(bom, {}) == bom
    crlf = b"{\r\n  // c\r\n  a: 'Hello',\r\n  b: \"World\",\r\n}\r\n"
    assert F.build(crlf, {"0": "Hallo"}) == crlf.replace(b"'Hello'", b"'Hallo'")
    esc = b'{"a": "caf\\u00e9 \\"q\\"", "b": "x"}'
    assert _texts(esc) == ['caf\u00e9 "q"', "x"]
    out = F.build(esc, {"0": 'caf\u00e9 "q"', "1": "y"})
    assert out == b'{"a": "caf\\u00e9 \\"q\\"", "b": "y"}', out
    print("  [identity] fixtures, BOM, CRLF and original escapes byte-identical OK")


def test_walk():
    src = _fixture("app.json5")
    assert _texts(src) == [
        "Hello, {name}!",
        "Goodbye",
        'It\'s "fine"',
        "first half second half",
        "Tab\there \u00e9 A \v",
        "Dollar",
        "#00ff00",
        "%1$s has %d items",
        "One",
        "Two",
        "<b>Bold</b> text",
    ]
    assert _texts(_fixture("settings.jsonc")) == [
        "My Workspace",
        "Welcome back, {{user}}",
        "Saved",
        "Not saved",
    ]
    # a plain JSON document: exactly the JsonFilter units, in the same order
    plain = (
        b'{"a": "One", "b": {"c": ["Two", 3, "---", {"d": "Three"}], "e": null},'
        b' "f": "{count}", "g": "", "h": "Four 4", "i": true}'
    )
    assert _texts(plain) == [u.text for u in get_filter("json").parse(plain).units]
    assert _texts(b"'Top level string'") == ["Top level string"]
    assert _texts(b"[1, 0xFF, -Infinity, +NaN, .5, 5., 1e3, 'x1']") == ["x1"]
    print("  [walk] values only, JSON rule, plain JSON matches JsonFilter OK")


def test_translated_round_trip():
    src = _fixture("app.json5")
    units = F.parse(src).units
    tr = {str(u.p_index): f"DE {u.text}" for u in units}
    out = F.build(src, tr)
    assert _texts(out) == [f"DE {u.text}" for u in units]
    text = out.decode("utf-8").replace("\r\n", "\n")
    for kept in (
        "// App strings",
        "/* Greeting shown on the home page */",
        "// same-line note belongs to farewell",
        "$dollar_key: 'DE Dollar'",
        "count: 0x1F,",
        "ratio: .5,",
        "big: +Infinity,",
        "nothing: NaN,",
        "      '---',\n    ],",
        "greeting: 'DE Hello, {name}!'",
        '"farewell": "DE Goodbye"',
        "'quoted': 'DE It\\'s \"fine\"'",
    ):
        assert kept in text, kept
    # the line continuation became a plain value (same decoded text)
    assert "multi: 'DE first half second half'" in text
    # escaping per quote style
    tricky = "a'b\"c\\d\ne\rf\tg\x01h\u2028i\u00e9"
    s = b"{x: 'Hello', y: \"World\"}"
    out = F.build(s, {"0": tricky, "1": tricky})
    assert out == (
        b"{x: 'a\\'b\"c\\\\d\\ne\\rf\\tg\\u0001h\\u2028i\xc3\xa9', "
        b'y: "a\'b\\"c\\\\d\\ne\\rf\\tg\\u0001h\\u2028i\xc3\xa9"}'
    ), out
    assert _texts(out) == [tricky, tricky]
    jsonc = _fixture("settings.jsonc")
    out = F.build(jsonc, {"1": 'Willkommen "zur\u00fcck", {{user}}', "3": "Nicht gespeichert"})
    assert b'"window.welcome": "Willkommen \\"zur\xc3\xbcck\\", {{user}}",' in out
    assert b'"messages": ["Saved", "Nicht gespeichert", ""],' in out
    assert b'"**/.git": true,\n    },' in out.replace(b"\r\n", b"\n")
    print("  [translate] round trip, comments/trailing commas/quote style kept, escaping OK")


def test_tokens():
    from app.services.segment import extract_inline_tags

    units = _texts(_fixture("app.json5")) + _texts(_fixture("settings.jsonc"))
    joined = " ".join(units)
    tags = extract_inline_tags(joined)
    for tok in ("{name}", "%1$s", "%d", "<b>", "</b>", "{user}"):
        assert any(tok in t for t in tags), (tok, tags)
    print("  [tokens] {name}, printf, {{user}} and <b> are protected tokens OK")


def test_records():
    recs = F.records(_fixture("app.json5"))
    from app.filters.kv_resource import KeyValueRecord  # gate: b1's shared type, not a copy

    assert recs and all(type(r) is KeyValueRecord for r in recs)
    # records() contract: unit_index equals the parse p_index of the same text
    units = {u.p_index: u.text for u in F.parse(_fixture("app.json5")).units}
    assert all(units[r.unit_index] == r.value for r in recs if r.unit_index is not None)
    by = {r.key: r for r in recs}
    assert by["greeting"].comment == "Greeting shown on the home page"
    assert by["greeting"].unit_index == 0
    assert by["farewell"].comment == ""  # the same-line comment follows the value
    assert by["nested.list[1]"].value == "Two" and by["nested.list[1]"].unit_index == 9
    assert by["nested.list[3]"].unit_index is None  # '---' counted, not a unit
    recs = F.records(_fixture("settings.jsonc"))
    assert recs[0].key == "editor.title" and "Editor settings" in recs[0].comment
    assert [r.key for r in recs][-1] == "messages[2]"
    dup = F.records(b'{a: "One", a: "Two"}')
    assert [r.duplicate for r in dup] == [False, True]
    assert _texts(b'{a: "One", a: "Two"}') == ["One", "Two"]  # every occurrence is a span
    print("  [records] dotted keys, comments above, unit_index, duplicates OK")


def test_encodings():
    u16 = '{"greeting": "Gr\u00fc\u00dfe"}'.encode("utf-16")
    parsed = F.parse(u16)
    assert parsed.skeleton.get("source_encoding") == "utf-16" and _texts(u16) == ["Gr\u00fc\u00dfe"]
    assert F.build(u16, {"0": "Hi"}) == b'{"greeting": "Hi"}'
    try:
        F.parse(b"\xff\xd8\xff\x00\x81binary\x00" * 10)
    except UnsupportedEncodingError:
        pass
    else:
        raise AssertionError("binary input must be refused")
    print("  [encodings] BOM kept, UTF-16 converted, binary refused OK")


def test_hostile():
    from app import config

    _refuses(b"[" * 300 + b"]" * 300, InputLimitError)
    for bad in (
        b"",
        b"// only a comment\n",
        b"{a: 'unterminated}",
        b"{a: 'x'} /* unterminated",
        b"{a: 'x\\1'}",
        b"{a: 'x\\x4'}",
        b"{a: 'x\\u12'}",
        b"{a: 'x\\01'}",
        b'{a: "\\ud800 lone"}',
        b"[1,,2]",
        b"{,}",
        b"{a: 1} extra",
        b"{a 1}",
        b"{1a: 'x'}",
        b"[truex]",
        b"[0x]",
        b"{a: 'line\nbreak'}",
        b"{a: 'x',",
    ):
        _refuses(bad)
    assert _texts(b'{a: "\\ud83d\\ude00 ok"}') == ["\U0001f600 ok"]
    # an unterminated string after a long run: linear scan, no catastrophic backtracking
    t0 = time.time()
    _refuses(b"['" + b"a\\'" * 300000 + b"]")
    _refuses(b"[" + b" " * 300000 + b"/*" + b"x " * 300000)
    assert time.time() - t0 < 5, time.time() - t0
    # a large real file parses quickly
    big = (
        "{\n"
        + "".join(f"  // note {i}\n  key{i}: 'Value number {i}',\n" for i in range(20000))
        + "}\n"
    ).encode()
    t0 = time.time()
    assert len(F.parse(big).units) == 20000
    assert F.build(big, {"19999": "Last"}).endswith(b"key19999: 'Last',\n}\n")
    assert time.time() - t0 < 15, time.time() - t0
    old = (config.MAX_DOCUMENT_SEGMENTS, config.MAX_SEGMENT_CHARS)
    try:
        config.MAX_DOCUMENT_SEGMENTS = 5
        many = ("{" + ",".join(f"k{i}: 'Value {i}'" for i in range(10)) + "}").encode()
        quotes = ("[" + ",".join("''" for _ in range(200)) + "]").encode()
        commas = ("[" + ",".join("1" for _ in range(200)) + "]").encode()
        for bad in (many, quotes, commas):
            _refuses(bad, InputLimitError, "")
        config.MAX_DOCUMENT_SEGMENTS, config.MAX_SEGMENT_CHARS = old[0], 50
        _refuses(b"{a: '" + b"word " * 40 + b"'}", InputLimitError, "")
    finally:
        config.MAX_DOCUMENT_SEGMENTS, config.MAX_SEGMENT_CHARS = old
    print("  [hostile] depth, syntax errors, backtracking, budgets and caps refused safely OK")


def test_surfaces():
    def read(path: str) -> str:
        with open(os.path.join(_ROOT, path), encoding="utf-8") as fh:
            return fh.read()

    assert "json5" in supported_exts() and "jsonc" in supported_exts()
    assert get_filter("jsonc") is F and get_filter("json").fmt == "json"
    assert ".json,.json5,.jsonc," in read("app/templates/project.html")
    from app.services.connectors.msgraph import FORMATS
    from app.services.mime import mime_for_fmt
    from app.web.api_part2 import _FMT_LABELS

    assert FORMATS["json5"] == "json5" and FORMATS["jsonc"] == "jsonc" and FORMATS["json"] == "json"
    assert mime_for_fmt("json5").startswith("application/json5")
    assert mime_for_fmt("json") == "application/json; charset=utf-8"
    assert "JSON5" in _FMT_LABELS["json5"] and "JSONC" in _FMT_LABELS["jsonc"]
    for doc in ("app/capabilities/documents.md", "llms.txt", "docs/CLI.md"):
        body = read(doc)
        assert ".json5" in body and ".jsonc" in body, doc
    print("  [surfaces] registry, upload accept, msgraph, MIME, labels, help, llms, CLI OK")


def test_end_to_end_jsonc():
    from fastapi.testclient import TestClient
    from sqlmodel import Session

    from app.db import engine, init_db
    from app.main import app
    from app.models import Project, Role, TargetLocale
    from app.services import apikeys, security
    from tests._cli_runner import run_cli

    init_db()
    with Session(engine) as s:
        p = Project(name="json5", org_id=1, source_locale="en-US", default_provider="mock")
        s.add(p)
        s.commit()
        s.refresh(p)
        s.add(TargetLocale(project_id=p.id, locale="de-DE"))
        s.commit()
        pid = p.id
    user = security.create_user("json5@x.test", "json5", Role.ADMIN, _TEST_PW)
    _row, raw = apikeys.create_key(user.id, "json5", scopes="api docs admin")
    client = TestClient(app, raise_server_exceptions=False, follow_redirects=False)
    hdr = {"X-Loc-Token": raw}  # pregate: allow-secret throwaway test key
    src = _fixture("settings.jsonc")
    r = client.post(
        "/api/documents",
        headers=hdr,
        data={"project_id": str(pid)},
        files={"file": ("settings.jsonc", src, "application/jsonc")},
    )
    assert r.status_code in (200, 201), (r.status_code, r.text)
    doc_id = r.json()["document_id"]
    code, out, err = run_cli(["doc", "translate", str(doc_id), "--locale", "de-DE"])
    assert code == 0, (code, out, err)
    r = client.get(f"/api/documents/{doc_id}/download", headers=hdr, params={"locale": "de-DE"})
    assert r.status_code == 200, (r.status_code, r.text)
    assert _texts(r.content) == [
        "[de-DE] My Workspace",
        "[de-DE] Welcome back, {{user}}",
        "[de-DE] Saved",
        "[de-DE] Not saved",
    ], r.content
    assert b"// Editor settings (VS Code style JSON with Comments)" in r.content
    assert b'"tabSize": 4,' in r.content
    import tempfile

    target = os.path.join(tempfile.mkdtemp(prefix="loctms_json5_"), "settings.de.jsonc")
    code, out, err = run_cli(["doc", "export", str(doc_id), "--locale", "de-DE", "-o", target])
    assert code == 0, (code, out, err)
    with open(target, "rb") as fh:
        assert fh.read() == r.content
    print("  [e2e] .jsonc: API upload, CLI translate, API + CLI export agree OK")


def main():
    test_identity()
    test_walk()
    test_translated_round_trip()
    test_tokens()
    test_records()
    test_encodings()
    test_hostile()
    test_surfaces()
    test_end_to_end_jsonc()
    print("JSON5 FILTER OK")


if __name__ == "__main__":
    main()
