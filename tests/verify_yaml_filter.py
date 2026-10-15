"""YAML filter (file-format Batch 3): generic YAML, Rails i18n YAML, Symfony YAML.

  identity   build with no changes (and with every unit unchanged) is byte-identical,
             for the three fixtures, a UTF-8 BOM file and a CRLF file
  walk       string values only: keys, booleans (yes/off), numbers, dates, null, tagged
             non-strings, aliases and complex keys are skipped; an anchored value is ONE unit
  translate  a translated round trip re-parses to exactly the translations, keeps every
             comment/key/anchor, and re-quotes values that need it (leading [, @, ': ',
             ' #', yes, 123, line breaks into plain/single, folded -> literal, control chars)
  rails      the root locale key is renamed on build_localized (quoted when needed, `no`);
             plural maps fold into one record; %{count} survives
  symfony    no rename; flat dotted keys and ICU/pipe plurals are plain strings
  records    dotted keys, comments above a key, plural forms, unit_index alignment
  encodings  UTF-8 BOM kept; CRLF kept in a rebuilt literal block; UTF-16 and Windows-1252
             converted (source_encoding recorded), binary refused
  hostile    alias bomb parses without expansion, python/object tags never constructed,
             deep nesting and segment/char/value caps refuse, invalid YAML is a ValueError
  surfaces   registry, upload accept list, msgraph map, MIME, API labels, help, llms.txt,
             CLI.md; API upload + CLI translate + API/CLI export of a Rails file end to end

Hermetic + offline. Final line: "YAML FILTER OK".
"""

import os
import sys
import time

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(_ROOT)
sys.path.insert(0, _ROOT)
from tests._bootstrap import init_env

init_env("loctms_yamlfilter_")

import yaml

from app.filters import get_filter, supported_exts
from app.filters.base import InputLimitError, UnsupportedEncodingError

FIX = os.path.join(_ROOT, "tests", "fixtures", "formats", "yaml")
F = get_filter("yaml")
_TEST_PW = "yaml-pw-1234567"  # pregate: allow-secret throwaway test user


def _fixture(name: str) -> bytes:
    with open(os.path.join(FIX, name), "rb") as fh:
        return fh.read()


def _texts(data: bytes) -> list[str]:
    return [u.text for u in F.parse(data).units]


def _all(data: bytes, fn) -> dict:
    return {str(u.p_index): fn(u.text) for u in F.parse(data).units}


def test_identity():
    for name in ("generic.yaml", "rails.en.yml", "messages.en.yaml"):
        src = _fixture(name)
        assert F.build(src, {}) == src, name
        assert F.build(src, _all(src, lambda t: t)) == src, name
        assert F.build_localized(src, {}, None) == src, name
    bom = b"\xef\xbb\xbfgreeting: Hello\nother: 'World'\n"
    assert F.build(bom, {}) == bom
    crlf = b'a: Hello\r\nb: |\r\n  Line one\r\n  Line two\r\n\r\nc: "x"\r\n'
    assert F.build(crlf, {}) == crlf
    no_eol = b"a: Hello"
    assert F.build(no_eol, {}) == no_eol
    assert get_filter("yml") is F and "yml" in supported_exts() and "yaml" in supported_exts()
    print("  [identity] untouched and unchanged builds are byte-identical OK")


def test_walk_generic():
    got = _texts(_fixture("generic.yaml"))
    want = [
        "Welcome to the store",
        "It's open: come in",
        'Line one\nLine two "quoted" é',
        "Hello there",
        "Goodbye",
        "#ff0000",  # quoted strings with a letter or digit count, like json_filter
        "1.0",
        "12",  # !!str 12 is a string
        "First item",
        "Second item",
        "Red",
        "Green",
        "Blue sky",
        "Keep this line.\nAnd this one.",
        "A folded paragraph that spans lines.",
        "Complex value",
        "Hello %{name}, you have {count} items and %1$s",
        "<b>Bold</b> text",
        "https://example.com/path",
    ]
    assert got == want, got
    for skipped in ("yes", "off", "42", "0.5", "2026-10-10", "Not translated either", "complex"):
        assert skipped not in got, skipped
    print("  [walk] string values only; aliases, keys, bools, numbers, tags skipped OK")


def test_translated_round_trip():
    src = _fixture("generic.yaml")
    tr = _all(src, lambda t: "[de-DE] " + t)
    out = F.build(src, tr)
    assert _texts(out) == list(tr.values()), _texts(out)
    text = out.decode("utf-8")
    for keep in (
        "# Generic YAML fixture",
        "# trailing comment stays",
        "defaults: &defaults",
        "<<: *defaults",
        "extra: *hello",
        "enabled: yes",
        "count: 42",
        'explicit_int: !!int "7"',
        "ruby: !ruby/object:Foo",
        "? [complex, key]",
        "# trailing comment at end of file",
    ):
        assert keep in text, keep
    # safe_load cannot build a list key or a Ruby object: neutralize those two for the check
    plain = text.replace("!ruby/object:Foo", "").replace("? [complex, key]\n  : ", "complex: ")
    data = yaml.safe_load(plain)
    assert data["app"]["copy"]["greeting"] == "[de-DE] Hello there"  # merge key follows
    assert data["app"]["copy"]["extra"] == "[de-DE] Hello there"  # alias follows anchor
    assert data["app"]["enabled"] is True and data["app"]["count"] == 42
    assert data["app"]["literal"] == "[de-DE] Keep this line.\nAnd this one.\n"
    assert data["app"]["folded"] == "[de-DE] A folded paragraph that spans lines."
    assert data["app"]["inline"] == ["[de-DE] Red", "[de-DE] Green", {"label": "[de-DE] Blue sky"}]
    print("  [translate] translated build re-parses to the translations; structure kept OK")


def test_requoting():
    cases = {
        b"k: plain\n": [
            "@user",
            "a: b",
            "x #y",
            "yes",
            "123",
            "- item",
            "two\nlines",
            "  padded",
            "tab\there",
            "bell\x07",
            "sep x",
            "emoji \U0001f600 ok",
            "ends with colon:",
            "{brace}",
            "%{count} items",
            "'quote",
            "null",
        ],
        b"k: 'single'\n": ["It's", "two\nlines", "bell\x07"],
        b'k: "double"\n': ['say "hi"\\', "tab\t", "éè"],
        b"k: |\n  literal\n  block\nnext: x\n": ["one\ntwo", " leading space", "single"],
        b"k: >\n  folded\n  block\nnext: x\n": ["one\ntwo", "one line"],
        b"k: [plain]\n": ["a, b", "c]"],
    }
    for src, news in cases.items():
        for new in news:
            out = F.build(src, {"0": new})
            data = yaml.safe_load(out.decode("utf-8"))
            if src.startswith(b"k: ["):
                assert data["k"] == [new], (src, new, out)
            elif src.startswith((b"k: |", b"k: >")):
                # block scalars keep their clip chomping: one trailing newline
                assert data["k"] == new + "\n", (src, new, out)
                assert data["next"] == "x", out
            else:
                assert data["k"] == new, (src, new, out)
            assert _texts(out)[0] == new.strip(), (src, new, _texts(out))
    # a plain value that stays plain keeps its plain style
    assert F.build(b"k: plain\n", {"0": "Klar"}) == b"k: Klar\n"
    assert F.build(b"k: 'x'\n", {"0": "Ja"}) == b"k: 'Ja'\n"
    # folded with a translated line break becomes literal (the break survives)
    out = F.build(b"k: >-\n  a\n  b\n", {"0": "x\ny"})
    assert out == b"k: |-\n  x\n  y\n", out
    # leading/trailing whitespace of the source value is kept outside the unit
    src = b'k: "  spaced  "\n'
    assert _texts(src) == ["spaced"]
    assert yaml.safe_load(F.build(src, {"0": "Raum"}))["k"] == "  Raum  "
    print("  [requote] translations re-quoted only when YAML needs it OK")


def test_rails_plural_fill_729():
    """#729 (decision 13): a Rails plural map (`one:` / `other:`) exported into Polish
    lacked `few:` / `many:`, so Rails reported a missing translation for 2..4 and 5+. A
    localized build adds every CLDR category the target uses, each a line before
    `other:` with the translated `other` (the shared rule, app/filters/plurals.py).
    Flow maps, untranslated `other`, generic YAML and build() are untouched."""
    src = (
        b"en:\n"
        b"  files:\n"
        b"    deleted:\n"
        b'      one: "%{count} file was deleted"\n'
        b'      other: "%{count} files were deleted"\n'
        b'    flow: {one: "a %{count}", other: "b %{count}"}\n'
        b"  title: Home\n"
    )
    tr = {"0": "S-pl", "1": "%{count} P-pl: x", "2": "a", "3": "b", "4": "Dom"}
    out = F.build_localized(src, tr, "pl-PL")
    text = out.decode()
    assert (
        '      one: "S-pl"\n'
        '      few: "%{count} P-pl: x"\n'
        '      many: "%{count} P-pl: x"\n'
        '      other: "%{count} P-pl: x"\n'
    ) in text, text
    data = yaml.safe_load(out)["pl-PL"]["files"]
    assert set(data["deleted"]) == {"one", "few", "many", "other"}, data
    assert set(data["flow"]) == {"one", "other"}, data
    ar = yaml.safe_load(F.build_localized(src, tr, "ar"))["ar"]["files"]["deleted"]
    assert set(ar) == {"zero", "one", "two", "few", "many", "other"}, ar
    assert F.build_localized(src, {"0": "S"}, "pl-PL").count(b"few:") == 0  # other untranslated
    assert b"few:" not in F.build(src, tr)
    generic = src.replace(b"en:\n", b"app:\n")
    assert b"few:" not in F.build_localized(generic, tr, "pl-PL")
    print("  [#729] Rails plural maps get the target's missing categories OK")


def test_rails():
    src = _fixture("rails.en.yml")
    parsed = F.parse(src)
    assert parsed.skeleton.get("rails_locale") == "en", parsed.skeleton
    texts = [u.text for u in parsed.units]
    assert "is too long (maximum is %{count} character)" in texts and "%{count} messages" in texts
    assert "%Y-%m-%d" in texts  # strftime has letters: translatable like any string
    out = F.build_localized(src, _all(src, lambda t: "[de-DE] " + t), "de-DE")
    data = yaml.safe_load(out)
    assert list(data) == ["de-DE"], list(data)
    assert data["de-DE"]["inbox"]["messages"]["other"] == "[de-DE] %{count} messages"
    assert out.decode().count("%{count}") == src.decode().count("%{count}")
    assert b"# Shown under a required field" in out
    # plain build() never renames; underscore source keys keep underscores
    assert yaml.safe_load(F.build(src, {})) == yaml.safe_load(src)
    assert list(yaml.safe_load(F.build_localized(src, {}, "pt_BR"))) == ["pt-BR"]
    us = b"en_US:\n  hi: Hello\n"
    assert F.build_localized(us, {}, "de-DE") == b"de_DE:\n  hi: Hello\n"
    # Norwegian `no` is a YAML 1.1 boolean: the renamed key is quoted
    out = F.build_localized(b"en:\n  hi: Hello\n", {}, "no")
    assert out == b'"no":\n  hi: Hello\n', out
    assert list(yaml.safe_load(out)) == ["no"]
    # quoted source key keeps its quote style
    assert F.build_localized(b"'en':\n  hi: Hello\n", {}, "fr") == b"'fr':\n  hi: Hello\n"
    # not Rails: two roots, a non-locale root, a scalar root value, multi-document
    for not_rails in (
        b"en:\n  a: A\nde:\n  a: B\n",
        b"app:\n  a: A\n",
        b"en: Hello\n",
        b"en:\n  a: A\n---\nen:\n  a: B\n",
    ):
        assert "rails_locale" not in F.parse(not_rails).skeleton, not_rails
        assert F.build_localized(not_rails, {}, "de") == not_rails, not_rails
    print("  [rails] root locale renamed on localized build; plurals and %{count} kept OK")


def test_records():
    recs = {r.key: r for r in F.records(_fixture("rails.en.yml"))}
    blank = recs["activerecord.errors.messages.blank"]
    assert blank.value == "can't be blank" and blank.comment == "Shown under a required field"
    plural = recs["inbox.messages"]
    assert plural.plural_forms == {
        "zero": "No messages",
        "one": "One message",
        "other": "%{count} messages",
    }, plural
    assert plural.value == "%{count} messages"
    assert "inbox.messages.one" not in recs
    assert recs["date.abbr_day_names[1]"].value == "Mon"
    units = F.parse(_fixture("rails.en.yml")).units
    for r in recs.values():
        if r.unit_index is not None and not r.plural_forms:
            assert units[r.unit_index].text == r.value.strip(), r
    assert units[plural.unit_index].text == "%{count} messages"
    sym = {r.key: r for r in F.records(_fixture("messages.en.yaml"))}
    assert sym["symfony.great"].value == "Symfony is great"
    assert sym["symfony.quoted"].value == "Quoted key, quoted value"
    assert sym["user.login.error"].value == "Invalid credentials for {username}."
    gen = {r.key: r for r in F.records(_fixture("generic.yaml"))}
    assert gen["app.enabled"].unit_index is None and gen["app.enabled"].value == "yes"
    assert gen["app.items[0]"].value == "First item"
    dup = F.records(b"a: One\na: Two\n")
    assert [r.duplicate for r in dup] == [False, True]
    print("  [records] dotted keys, comments, plural forms, unit alignment OK")


def test_symfony():
    src = _fixture("messages.en.yaml")
    texts = _texts(src)
    assert (
        texts[0] == "Symfony is great"
        and "{0} No users|{1} One user|]1,Inf[ %count% users" in texts
    )
    assert "{apples, plural, one {# apple} other {# apples}}" in texts
    assert "rails_locale" not in F.parse(src).skeleton
    out = F.build_localized(src, _all(src, lambda t: "[fr] " + t), "fr")
    data = yaml.safe_load(out)
    assert data["symfony.great"] == "[fr] Symfony is great"
    assert data["user"]["login"]["title"] == "[fr] Sign in"
    print("  [symfony] flat dotted keys, no in-file locale, ICU/pipe plurals kept OK")


def test_encodings():
    bom = b"\xef\xbb\xbfgreeting: Hello\n"
    out = F.build(bom, {"0": "Hallo"})
    assert out == b"\xef\xbb\xbfgreeting: Hallo\n", out
    crlf = b'a: Hello\r\nb: |\r\n  Line one\r\n  Line two\r\n\r\nc: "x"\r\n'
    out = F.build(crlf, {"1": "Eins\nZwei\nDrei"})
    assert out == b'a: Hello\r\nb: |\r\n  Eins\r\n  Zwei\r\n  Drei\r\n\r\nc: "x"\r\n', out
    u16 = "greeting: Grüße\n".encode("utf-16")
    parsed = F.parse(u16)
    assert parsed.skeleton.get("source_encoding") == "utf-16" and _texts(u16) == ["Grüße"]
    assert F.build(u16, {"0": "Hi"}) == b"greeting: Hi\n"
    cp = "greeting: café\n".encode("cp1252")
    assert F.parse(cp).skeleton.get("source_encoding") == "cp1252"
    assert F.build(cp, {}) == cp  # B-bytes: an untouched file comes back as uploaded
    assert F.build(cp, {"0": "Grüße"}).decode("utf-8") == "greeting: Grüße\n"
    try:
        F.parse(b"\xff\xd8\xff\x00\x81binary\x00" * 10)
    except UnsupportedEncodingError:
        pass
    else:
        raise AssertionError("binary input must be refused")
    try:  # valid UTF-8 carrying control characters YAML forbids: a plain refusal
        F.parse(b"k: \x00\x01\x02 text\n")
    except ValueError as exc:
        assert "YAML" in str(exc), exc
    else:
        raise AssertionError("control characters must be refused")
    print("  [encodings] BOM and CRLF kept; UTF-16 and cp1252 converted; binary refused OK")


def test_hostile():
    from app import config

    bomb = (
        b'a: &a ["lol","lol","lol","lol","lol","lol","lol","lol","lol"]\n'
        b"b: &b [*a,*a,*a,*a,*a,*a,*a,*a,*a]\n"
        b"c: &c [*b,*b,*b,*b,*b,*b,*b,*b,*b]\n"
        b"d: &d [*c,*c,*c,*c,*c,*c,*c,*c,*c]\n"
        b"e: &e [*d,*d,*d,*d,*d,*d,*d,*d,*d]\n"
        b"f: &f [*e,*e,*e,*e,*e,*e,*e,*e,*e]\n"
        b"g: &g [*f,*f,*f,*f,*f,*f,*f,*f,*f]\n"
    )
    t0 = time.time()
    assert len(F.parse(bomb).units) == 9  # aliases are never expanded
    assert F.build(bomb, {}) == bomb
    assert time.time() - t0 < 5
    evil = b"x: !!python/object/apply:os.system ['echo PWNED']\ny: Safe text\n"
    assert _texts(evil) == ["Safe text"]
    assert F.build(evil, {"0": "Sicher"}).startswith(b"x: !!python/object/apply:os.system")
    deep = b"k: " + b"[" * 500 + b"x" + b"]" * 500 + b"\n"
    for bad, exc in (
        (deep, InputLimitError),
        (b"a: [unclosed\n", ValueError),
        (b"a: b: c\n", ValueError),
        (b"key: 'unterminated\n", ValueError),
    ):
        try:
            F.parse(bad)
        except exc as e:
            assert "YAML" in str(e), e
        else:
            raise AssertionError(f"must refuse {bad[:30]!r}")
    old = (config.MAX_DOCUMENT_SEGMENTS, config.MAX_SEGMENT_CHARS)
    try:
        config.MAX_DOCUMENT_SEGMENTS = 5
        many = "".join(f"k{i}: Value {i}\n" for i in range(10)).encode()
        flow = b"k: [" + b",".join(b"1" for _ in range(200)) + b"]\n"
        for bad in (many, flow):
            try:
                F.parse(bad)
            except InputLimitError:
                pass
            else:
                raise AssertionError("segment cap must refuse")
        config.MAX_DOCUMENT_SEGMENTS, config.MAX_SEGMENT_CHARS = old[0], 50
        try:
            F.parse(b"k: " + b"word " * 40 + b"\n")
        except InputLimitError:
            pass
        else:
            raise AssertionError("MAX_SEGMENT_CHARS must refuse")
    finally:
        config.MAX_DOCUMENT_SEGMENTS, config.MAX_SEGMENT_CHARS = old
    print("  [hostile] alias bomb, object tags, depth, caps and bad YAML refused safely OK")


def test_pure_python_parser_matches():
    """The libyaml fast path and the pure-Python fallback must find the same spans."""
    from app.filters import yaml_filter

    srcs = [_fixture(n) for n in ("generic.yaml", "rails.en.yml", "messages.en.yaml")]
    srcs.append('k: &a 日本語 \U0001d518\nl: *a\nm: "é"\n'.encode())
    fast = [F.build_localized(s, _all(s, lambda t: "[x] " + t), "de") for s in srcs]
    saved = getattr(yaml, "__with_libyaml__", False)
    try:
        yaml.__with_libyaml__ = False
        slow = [F.build_localized(s, _all(s, lambda t: "[x] " + t), "de") for s in srcs]
    finally:
        yaml.__with_libyaml__ = saved
    assert fast == slow
    assert yaml_filter._walk  # the shared walk
    print("  [parsers] libyaml and pure-Python walks agree OK")


def test_surfaces():
    def read(path: str) -> str:
        with open(os.path.join(_ROOT, path), encoding="utf-8") as fh:
            return fh.read()

    assert ".yaml,.yml" in read("app/templates/project.html")
    from app.services.connectors.msgraph import FORMATS
    from app.services.mime import mime_for_fmt
    from app.web.api_part2 import _FMT_LABELS

    assert FORMATS["yml"] == "yaml" and FORMATS["yaml"] == "yaml"
    assert mime_for_fmt("yml").startswith("application/yaml")
    assert "YAML" in _FMT_LABELS["yml"]
    for doc in ("app/capabilities/documents.md", "llms.txt", "docs/CLI.md"):
        body = read(doc)
        assert ".yaml" in body and "Rails" in body, doc
    print("  [surfaces] registry, upload accept, msgraph, MIME, labels, help, llms, CLI OK")


def test_end_to_end_rails():
    from fastapi.testclient import TestClient
    from sqlmodel import Session

    from app.db import engine, init_db
    from app.main import app
    from app.models import Project, Role, TargetLocale
    from app.services import apikeys, security
    from tests._cli_runner import run_cli

    init_db()
    with Session(engine) as s:
        p = Project(name="yaml", org_id=1, source_locale="en-US", default_provider="mock")
        s.add(p)
        s.commit()
        s.refresh(p)
        s.add(TargetLocale(project_id=p.id, locale="de-DE"))
        s.commit()
        pid = p.id
    user = security.create_user("yaml@x.test", "yaml", Role.ADMIN, _TEST_PW)
    _row, raw = apikeys.create_key(user.id, "yaml", scopes="api docs admin")
    client = TestClient(app, raise_server_exceptions=False, follow_redirects=False)
    hdr = {"X-Loc-Token": raw}  # pregate: allow-secret throwaway test key
    src = _fixture("rails.en.yml")
    r = client.post(
        "/api/documents",
        headers=hdr,
        data={"project_id": str(pid)},
        files={"file": ("en.yml", src, "application/yaml")},
    )
    assert r.status_code in (200, 201), (r.status_code, r.text)
    doc_id = r.json()["document_id"]
    code, out, err = run_cli(["doc", "translate", str(doc_id), "--locale", "de-DE"])
    assert code == 0, (code, out, err)
    r = client.get(f"/api/documents/{doc_id}/download", headers=hdr, params={"locale": "de-DE"})
    assert r.status_code == 200, (r.status_code, r.text)
    data = yaml.safe_load(r.content)
    assert list(data) == ["de-DE"], r.content
    assert data["de-DE"]["helpers"]["submit"]["create"] == "[de-DE] Create %{model}"
    assert b"# Shown under a required field" in r.content
    import tempfile

    target = os.path.join(tempfile.mkdtemp(prefix="loctms_yaml_"), "de.yml")
    code, out, err = run_cli(["doc", "export", str(doc_id), "--locale", "de-DE", "-o", target])
    assert code == 0, (code, out, err)
    with open(target, "rb") as fh:
        assert fh.read() == r.content
    print("  [e2e] Rails .yml: API upload, CLI translate, API + CLI export agree OK")


def test_gate_fixes():
    """Review gate: Rails %{name} is ONE protected token (a dropped `%` is caught by the tag
    badge and the LC-7 placeholder gate), and a 3-letter root key babel happens to know
    (`nav:`) is not a Rails locale (its key must never be renamed on export)."""
    from app.filters.yaml_filter import _looks_locale
    from app.services.quality import placeholder_violation
    from app.services.segment import extract_inline_tags

    assert extract_inline_tags("Hello %{name}, {count} left") == ["%{name}", "{count}"]
    assert placeholder_violation("%{count} items", "{count} Artikel"), "dropped % not caught"
    assert not placeholder_violation("%{count} items", "%{count} Artikel")
    assert not placeholder_violation("{count} items", "{count} Artikel")
    # a HubL/Jinja `{% ... %}` after a literal % stays the old brace token
    assert extract_inline_tags("20%{% endif %}") == ["{% endif %}"]

    for key in ("en", "pt-BR", "zh_CN", "fil", "fil-PH", "ast"):
        assert _looks_locale(key), key
    for key in ("nav", "spa", "app", "dev"):
        assert not _looks_locale(key), key
    src = b"nav:\n  home: Home page\n  about: About us\n"
    out = F.build_localized(src, {"0": "Startseite"}, "de")
    assert out == b"nav:\n  home: Startseite\n  about: About us\n", out
    assert "rails_locale" not in F.parse(src).skeleton
    print("  [gate] %{name} is one token; 3-letter non-Rails root keys are not locales OK")


def main():
    test_gate_fixes()
    test_identity()
    test_walk_generic()
    test_translated_round_trip()
    test_requoting()
    test_rails()
    test_rails_plural_fill_729()
    test_records()
    test_symfony()
    test_encodings()
    test_hostile()
    test_pure_python_parser_matches()
    test_surfaces()
    test_end_to_end_rails()
    print("YAML FILTER OK")


if __name__ == "__main__":
    main()
