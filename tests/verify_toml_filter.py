"""TOML document filter (format Batch 11): Hugo i18n, go-i18n and generic TOML files.

  identity    an untouched file (comments, layout, CRLF, BOM) rebuilds byte for byte
  units       string values only, in file order; keys/numbers/dates never; plural
              categories styled; description/id/hash/leftdelim/rightdelim skipped
  tokens      {{...}} template actions (and a table's own delimiters) are point tokens
  quoting     basic / literal / multi-line kinds kept; a literal that cannot hold the
              translation becomes the matching basic string; every rebuild re-parses
  hostile     invalid TOML, non-UTF-8, deep nesting, budgets and caps are refused
  surfaces    API upload -> translate (mock) -> download, upload label and supported_exts

Fixtures are hand-written here. Hermetic + offline. Final line: "TOML FILTER OK".
"""

import os
import sys

import tomllib

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(_ROOT)
sys.path.insert(0, _ROOT)
from tests._bootstrap import init_env

init_env("loctms_toml_")

from app.filters import get_filter
from app.filters.base import InputLimitError, UnsupportedEncodingError

LOC = "de-DE"

HUGO = """# Hugo i18n (en)
[home]
other = "Home"   # nav label

[readingTime]
one = "One minute to read"
other = "{{ .Count }} minutes to read"

[PersonCats]
description = "The number of cats a person has"
id = "PersonCats"
hash = "sha1-abc"
one = '{{.Name}} has one cat.'
other = '''
{{.Name}} has {{.Count}} cats.'''

[Custom]
leftdelim = "<<"
rightdelim = ">>"
other = "Hello <<.Name>>, {{not a token}}"

[[i18n]]
id = "wordCount"
translation = \"\"\"Words: \\
   many\"\"\"

[site]
title.main = "My \\"site\\" \\u00e9"   # trailing comment
tags = ["alpha", 'beta', 3, { label = "Gamma" }, "{{ .Only }}", "---"]
when = 1979-05-27 07:32:00Z
ratio = 0.5
"quoted key" = "Quoted"
"""
HUGO_B = HUGO.encode("utf-8")

EXPECTED = [
    ("toml:other", "Home"),
    ("toml:one", "One minute to read"),
    ("toml:other", "⟦x1⟧ minutes to read"),
    ("toml:one", "⟦x1⟧ has one cat."),
    ("toml:other", "⟦x1⟧ has ⟦x2⟧ cats."),
    ("toml:other", "Hello ⟦x1⟧, {{not a token}}"),
    ("toml", "Words: many"),
    ("toml", 'My "site" é'),
    ("toml", "alpha"),
    ("toml", "beta"),
    ("toml", "Gamma"),
    ("toml", "Quoted"),
]


def _f():
    return get_filter("toml")


def _values(data: bytes) -> dict:
    return tomllib.loads(data.decode("utf-8-sig"))


def test_registered():
    from app.filters import supported_exts

    assert _f() is not None and _f().fmt == "toml"
    assert "toml" in supported_exts()
    print("  [registry] toml registered OK")


def test_units_and_identity():
    r = _f().parse(HUGO_B)
    got = [(u.style, u.text) for u in r.units]
    assert got == EXPECTED, got
    assert [u.p_index for u in r.units] == list(range(len(EXPECTED)))
    assert r.units[2].inline_map == {"1": {"point": "{{ .Count }}"}}, r.units[2].inline_map
    assert r.units[5].inline_map == {"1": {"point": "<<.Name>>"}}, r.units[5].inline_map
    assert r.skeleton["format"] == "toml"
    assert _f().build(HUGO_B, {}) == HUGO_B
    same = {str(u.p_index): u.text for u in r.units}
    assert _f().build(HUGO_B, same) == HUGO_B
    # CRLF + BOM survive untouched
    crlf = b"\xef\xbb\xbf" + HUGO.replace("\n", "\r\n").encode("utf-8")
    rc = _f().parse(crlf)
    assert [(u.style, u.text) for u in rc.units] == EXPECTED
    assert _f().build(crlf, {}) == crlf
    print("  [units] string values in order, plurals styled, metadata skipped, identity OK")


def test_translated_round_trip():
    r = _f().parse(HUGO_B)
    tr = {str(u.p_index): "DE " + u.text for u in r.units}
    out = _f().build(HUGO_B, tr)
    v = _values(out)
    assert v["home"]["other"] == "DE Home"
    assert v["readingTime"]["other"] == "DE {{ .Count }} minutes to read"
    assert v["PersonCats"]["other"] == "DE {{.Name}} has {{.Count}} cats."
    assert v["PersonCats"]["description"] == "The number of cats a person has"
    assert v["PersonCats"]["id"] == "PersonCats" and v["PersonCats"]["hash"] == "sha1-abc"
    assert v["Custom"]["other"] == "DE Hello <<.Name>>, {{not a token}}"
    assert v["i18n"][0]["translation"] == "DE Words: many"
    assert v["site"]["title"]["main"] == 'DE My "site" é'
    tags = ["DE alpha", "DE beta", 3, {"label": "DE Gamma"}, "{{ .Only }}", "---"]
    assert v["site"]["tags"] == tags, v["site"]["tags"]
    assert v["site"]["quoted key"] == "DE Quoted"
    text = out.decode("utf-8")
    # everything outside the string tokens is byte-identical
    for keep in (
        "# Hugo i18n (en)",
        "   # nav label",
        "   # trailing comment",
        "when = 1979-05-27 07:32:00Z",
        "ratio = 0.5",
        "[[i18n]]",
        "'''\nDE {{.Name}} has {{.Count}} cats.'''",
        "one = 'DE {{.Name}} has one cat.'",
    ):
        assert keep in text, (keep, text)
    # a dropped or invented token never leaks; a missing action is dropped, not invented
    out2 = _f().build(HUGO_B, {"2": "Lesezeit ⟦x9⟧ ⟦g1⟧Min⟦/g1⟧ ⟦x1⟧"})
    assert _values(out2)["readingTime"]["other"] == "Lesezeit  Min {{ .Count }}"
    print("  [round trip] translations spliced, layout and tokens preserved OK")


def test_quoting_choices():
    src = (
        b"a = 'plain'\n"
        b'b = "basic"\n'
        b"c = '''\nmulti\nline'''\n"
        b'd = """no lead"""\n'
        b"e = 'path'\n"
    )
    out = _f().build(
        src,
        {
            "0": "l'apostrophe",  # literal cannot hold ' -> basic
            "1": 'tab\there "q" back\\slash\x01',
            "2": "zwei\nZeilen ''' drei",  # ml literal cannot hold ''' -> ml basic
            "3": '\nstarts with newline and ends with "',
            "4": "zeile1\nzeile2",  # literal cannot hold a newline -> basic with \n
        },
    )
    text = out.decode("utf-8")
    assert 'a = "l\'apostrophe"' in text, text
    assert 'b = "tab\\there \\"q\\" back\\\\slash\\u0001"' in text, text
    assert text.count('"""') == 4, text  # c switched to ml basic, d stayed
    v = _values(out)
    assert v == {
        "a": "l'apostrophe",
        "b": 'tab\there "q" back\\slash\x01',
        "c": "zwei\nZeilen ''' drei",
        "d": '\nstarts with newline and ends with "',
        "e": "zeile1\nzeile2",
    }, v
    assert 'e = "zeile1\\nzeile2"' in text, text
    # literal kept when it fits; ml literal kept when it fits
    out = _f().build(src, {"0": "schlicht", "2": "mehr\nZeilen"})
    assert b"a = 'schlicht'" in out and b"c = '''\nmehr\nZeilen'''" in out, out
    # CRLF file: new line breaks inside a multi-line value follow the file
    crlf = b'x = """\r\nalt\r\nzwei"""\r\ny = 1\r\n'
    out = _f().build(crlf, {"0": "neu\ndrei"})
    assert out == b'x = """\r\nneu\r\ndrei"""\r\ny = 1\r\n', out
    print("  [quoting] basic/literal/multi-line choices and escapes OK")


def test_paths_and_tables():
    src = b"""
[[fruits]]
name = "apple"
[fruits.physical]
color = "red"
[[fruits.varieties]]
name = "red delicious"
[[fruits]]
name = "banana"
[[fruits.varieties]]
name = "plantain"
[dog."tater.man"]
type.name = "pug"
inline = { a = { b = "deep" } }
nested = [["x1", ["y1"]]]
"""
    recs = _f().records(src)
    keys = [r["key"] for r in recs]
    assert keys == [
        "fruits[0].name",
        "fruits[0].physical.color",
        "fruits[0].varieties[0].name",
        "fruits[1].name",
        "fruits[1].varieties[0].name",
        'dog."tater.man".type.name',
        'dog."tater.man".inline.a.b',
        'dog."tater.man".nested[0][0]',
        'dog."tater.man".nested[0][1][0]',
    ], keys
    tr = {str(i): f"T{i}" for i in range(len(recs))}
    v = _values(_f().build(src, tr))
    assert v["fruits"][1]["varieties"][0]["name"] == "T4"
    assert v["dog"]["tater.man"]["nested"] == [["T7", ["T8"]]]
    # records: message tables fold plurals and carry the description
    rec = next(r for r in _f().records(HUGO_B) if r["key"] == "PersonCats.other")
    assert rec["comment"] == "The number of cats a person has"
    assert set(rec["plural_forms"]) == {"one", "other"} and rec["unit_index"] == 4
    print("  [paths] array tables, dotted/quoted keys, inline tables, records OK")


def test_message_detection():
    """go-i18n isMessage: a table whose keys are ALL reserved message fields is a
    message (its description is a note); a mixed table is ordinary data."""
    src = (
        b'[a]\ndescription = "note A"\ntranslation = "Text A"\n'
        b'[b]\ndescription = "Shown B"\ntitle = "Title B"\n'
        b'[c]\nDescription = "note C"\nOne = "one C"\nOther = "other C"\n'
    )
    got = [(u.style, u.text) for u in _f().parse(src).units]
    assert got == [
        ("toml", "Text A"),
        ("toml", "Shown B"),
        ("toml", "Title B"),
        ("toml:one", "one C"),
        ("toml:other", "other C"),
    ], got
    print("  [messages] go-i18n message tables vs ordinary tables OK")


def _refused(data, exc=(ValueError,), needle=""):
    try:
        _f().parse(data)
    except exc as e:
        assert needle in str(e), (needle, str(e))
        return
    raise AssertionError(f"expected a refusal for {data[:60]!r}")


def test_hostile_inputs():
    from app import config

    _refused(b'a = "unterminated\n', needle="could not be read")
    _refused(b"a = 1\na = 2\n", needle="could not be read")
    _refused("a = 'café'".encode("latin-1"), (UnsupportedEncodingError,), "UTF-8")
    _refused("a = 'x'".encode("utf-16"), (UnsupportedEncodingError,), "UTF-8")
    _refused(b"a = '\x00\x00\x00'", (UnsupportedEncodingError,))
    _refused(b"a = " + b"[" * 5000 + b"]" * 5000, (InputLimitError,))
    _refused(b"a = " + b"[" * 70 + b'"x"' + b"]" * 70 + b"\n", (InputLimitError,), "nested")
    old = config.MAX_DOCUMENT_SEGMENTS
    config.MAX_DOCUMENT_SEGMENTS = 3
    try:
        _refused(b"\n".join(b'k%d = "v"' % i for i in range(5)), (InputLimitError,), "segments")
        _refused(b'a = "' + b"'" * 200 + b'"', (InputLimitError,), "strings")
        _refused(b"a = [" + b"1," * 100 + b"]", (InputLimitError,), "values")
    finally:
        config.MAX_DOCUMENT_SEGMENTS = old
    _refused(b'a = "' + b"x" * (config.MAX_SEGMENT_CHARS + 1) + b'"', (InputLimitError,))
    # pathological template text stays linear (bounded action regex)
    import time

    big = b'a = "' + b"x{{" * 30000 + b'"\n'
    t0 = time.monotonic()
    r = _f().parse(big)
    assert len(r.units) == 1 and not r.units[0].inline_map
    assert time.monotonic() - t0 < 5, time.monotonic() - t0
    print("  [hostile] invalid TOML, encodings, nesting, budgets and caps refused OK")


def test_api_surface():
    from fastapi.testclient import TestClient
    from sqlmodel import Session

    from app.db import engine, init_db
    from app.main import app
    from app.models import Project, Role, TargetLocale
    from app.services import apikeys, security
    from tests._cli_runner import run_cli

    init_db()
    with Session(engine) as s:
        p = Project(name="toml", org_id=1, source_locale="en-US", default_provider="mock")
        s.add(p)
        s.commit()
        s.refresh(p)
        s.add(TargetLocale(project_id=p.id, locale=LOC))
        s.commit()
        pid = p.id
    user = security.create_user("toml@x.test", "toml", Role.ADMIN, "toml-pw-123456")
    _row, raw = apikeys.create_key(user.id, "toml", scopes="api docs admin")
    client = TestClient(app, raise_server_exceptions=False, follow_redirects=False)
    hdr = {"X-Loc-Token": raw}  # pregate: allow-secret throwaway test key
    r = client.post(
        "/api/documents",
        headers=hdr,
        data={"project_id": str(pid)},
        files={"file": ("en.toml", HUGO_B, "application/toml")},
    )
    assert r.status_code in (200, 201), (r.status_code, r.text)
    doc_id = r.json()["document_id"]
    code, out, err = run_cli(["doc", "translate", str(doc_id), "--locale", LOC])
    assert code == 0, (code, out, err)
    r = client.get(f"/api/documents/{doc_id}/download", headers=hdr, params={"locale": LOC})
    assert r.status_code == 200, (r.status_code, r.text)
    assert r.headers["content-type"].startswith("application/toml"), r.headers
    v = _values(r.content)
    assert v["home"]["other"] != "Home" and "Home" in v["home"]["other"], v["home"]
    assert "{{ .Count }}" in v["readingTime"]["other"], v["readingTime"]
    assert v["PersonCats"]["description"] == "The number of cats a person has"
    assert b"# Hugo i18n (en)" in r.content and b"when = 1979-05-27 07:32:00Z" in r.content
    r = client.post(
        "/api/documents",
        headers=hdr,
        data={"project_id": str(pid)},
        files={"file": ("bad.toml", b'a = "open\n', "application/toml")},
    )
    assert 400 <= r.status_code < 500, (r.status_code, r.text)
    print("  [api] upload, translate, download TOML; invalid file refused OK")


def test_many_edits_linear_splice():
    # Review gate: the build splices all edits in one pass (re-slicing the whole text per
    # edit was quadratic in file size x edit count).
    from app.filters.toml_filter import _splice

    assert _splice("abcdef", [(4, 5, "E"), (0, 1, "A"), (2, 3, "")]) == "AbdEf"
    n = 5000
    src = "".join(f'k{i} = "Value number {i}"\n' for i in range(n)).encode("utf-8")
    texts = {str(i): f"Wert Nummer {i}" for i in range(n)}
    out = _f().build(src, texts)
    vals = tomllib.loads(out.decode("utf-8"))
    assert all(vals[f"k{i}"] == f"Wert Nummer {i}" for i in range(n))
    print("  [build] 5000 edits spliced in one pass, every value correct OK")


def test_upload_accept_lists():
    with open(os.path.join(_ROOT, "app", "templates", "project.html"), encoding="utf-8") as fh:
        html = fh.read()
    assert ",.toml" in html and "· TOML" in html
    from app.services.connectors.msgraph import FORMATS

    assert FORMATS.get("toml") == "toml"
    print("  [surfaces] upload accept list and connector map list TOML OK")


def main():
    test_registered()
    test_units_and_identity()
    test_translated_round_trip()
    test_quoting_choices()
    test_paths_and_tables()
    test_message_detection()
    test_hostile_inputs()
    test_many_edits_linear_splice()
    test_upload_accept_lists()
    test_api_surface()
    print("TOML FILTER OK")


if __name__ == "__main__":
    main()
