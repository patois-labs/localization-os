"""Markdown / MDX front matter is structure, not prose (GitHub #731).

  keys       YAML (`---`) and Hugo TOML (`+++`) front matter at the start of the file:
             keys, dates, booleans, numbers, ids, slugs, permalinks, layouts and lists are
             never units; only the one-line string values of allowlisted keys are, FIRST
  writeback  translated values land in the right key and the block re-parses to the
             source mapping with only those values changed (md and mdx, unit offsets with
             MDX attributes)
  quoting    a translation with `: `, ` #`, a leading `-` / `#` / `[` / quote / `@`, a
             YAML 1.1 boolean or number, quotes, a line break, re-parses (yaml.safe_load /
             tomllib) to exactly the translation, from plain, single and double quoted
             sources; TOML always a valid string
  skip       block scalars, lists, nested maps, multi-line plain values, malformed front
             matter (left untouched, no units), LOC_TMS_FRONT_MATTER_KEYS allowlist
  not fm     a `---` rule later in the file, an unclosed opening `---`, a prose block
             between two rules are body text as before
  identity   no-op and same-text builds are byte-identical (LF, CRLF, BOM, no final newline)
  walk       new skeletons carry md_walk = 2; no stamp = the pre-#731 walk (front matter
             lines are line units); an unknown stamp is refused at export
  e2e        API upload, CLI translate, API download of a Jekyll post: valid front matter,
             structural keys unchanged, title translated

Hermetic + offline. Final line: "FRONT MATTER 731 OK".
"""

import os
import sys

import tomllib

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(_ROOT)
sys.path.insert(0, _ROOT)
from tests._bootstrap import init_env

init_env("loctms_fm731_")

import yaml

from app.filters import get_filter

MD = get_filter("md")
MDX = get_filter("mdx")
_TEST_PW = "fm731-pw-1234567"  # pregate: allow-secret throwaway test user

POST = (
    b"---\n"
    b"title: Getting started with the dashboard\n"
    b"layout: post\n"
    b"permalink: /docs/getting-started/\n"
    b"date: 2026-10-11\n"
    b"tags: [setup, onboarding]\n"
    b"draft: false\n"
    b"description: Learn how to set up your first project in five minutes.\n"
    b"---\n"
    b"\n"
    b"# Getting started\n"
    b"\n"
    b"Open the dashboard and click **New project** to create your first project.\n"
)
DOCU = (
    b"---\n"
    b"id: architecture\n"
    b"slug: /advanced/architecture\n"
    b"sidebar_label: Architecture overview\n"
    b"sidebar_position: 2\n"
    b"description: How the build pipeline turns your files into a website.\n"
    b"---\n"
    b"\n"
    b"# Architecture\n"
    b"\n"
    b'<Figure title="Pipeline diagram" src="/img/p.png" />\n'
    b"\n"
    b"The build pipeline reads every page and writes static HTML files.\n"
)
HUGO = (
    b"+++\n"
    b'title = "Getting started with the dashboard"\n'
    b"draft = false\n"
    b'slug = "getting-started"\n'
    b"summary = 'A literal summary string'\n"
    b'description = "Learn how to set up your first project in five minutes."\n'
    b"weight = 10\n"
    b"\n"
    b"[params]\n"
    b'title = "Nested table title"\n'
    b"+++\n"
    b"\n"
    b"Open the dashboard and click **New project** to create your first project.\n"
)


def _texts(f, data: bytes) -> list[str]:
    return [u.text for u in f.parse(data).units]


def _fm(data: bytes, kind: str = "yaml"):
    text = data.decode("utf-8").lstrip("﻿")
    fence = "---" if kind == "yaml" else "+++"
    lines = text.splitlines()
    assert lines[0] == fence, lines[:2]
    close = next(i for i in range(1, len(lines)) if lines[i] in (fence, "..."))
    block = "\n".join(lines[1:close])
    return yaml.safe_load(block) if kind == "yaml" else tomllib.loads(block)


def test_keys():
    for f in (MD, MDX):
        texts = _texts(f, POST)
        assert texts == [
            "Getting started with the dashboard",
            "Learn how to set up your first project in five minutes.",
            "Getting started",
            "Open the dashboard and click **New project** to create your first project.",
        ], (f.fmt, texts)
        joined = "\n".join(texts)
        for gone in ("layout", "post\n", "permalink", "2026", "draft", "false", "setup"):
            assert gone not in joined.lower(), (gone, texts)
        assert not any(t.startswith(("title", "layout", "date", "draft")) for t in texts), texts
    texts = _texts(MDX, DOCU)
    assert texts == [
        "Architecture overview",
        "How the build pipeline turns your files into a website.",
        "Architecture",
        "The build pipeline reads every page and writes static HTML files.",
        "Pipeline diagram",
    ], texts
    texts = _texts(MD, HUGO)
    assert texts == [
        "Getting started with the dashboard",
        "A literal summary string",
        "Learn how to set up your first project in five minutes.",
        "Open the dashboard and click **New project** to create your first project.",
    ], texts  # [params].title is in a table: not a top-level key
    print("  [keys] keys / dates / booleans / ids / slugs / tables never offered OK")


def _translate_all(f, data, fn):
    return f.build(data, {str(u.p_index): fn(u.text) for u in f.parse(data).units})


def test_writeback():
    src = _fm(POST)
    for f in (MD, MDX):
        out = _translate_all(f, POST, lambda t: "FR " + t)
        got = _fm(out)
        want = dict(src, title="FR " + src["title"], description="FR " + src["description"])
        assert got == want, (f.fmt, got)
        body = out.decode().split("---\n", 2)[2]
        assert "# FR Getting started" in body and "FR Open the dashboard" in body, body
    out = _translate_all(MDX, DOCU, lambda t: "DE " + t)
    got = _fm(out)
    assert got == dict(
        _fm(DOCU),
        sidebar_label="DE Architecture overview",
        description="DE How the build pipeline turns your files into a website.",
    ), got
    s = out.decode()
    assert "id: architecture\n" in s and "slug: /advanced/architecture\n" in s, s
    assert 'title="DE Pipeline diagram"' in s and "# DE Architecture" in s, s
    assert "DE The build pipeline" in s, s
    # only the attribute translated: the offset of the attribute unit is right
    units = MDX.parse(DOCU).units
    out = MDX.build(DOCU, {str(units[-1].p_index): "Diagramm"})
    assert b'title="Diagramm"' in out and out.replace(b"Diagramm", b"Pipeline diagram") == DOCU
    out = _translate_all(MD, HUGO, lambda t: "FR " + t)
    got = _fm(out, "toml")
    src = _fm(HUGO, "toml")
    for k in ("title", "summary", "description"):
        src[k] = "FR " + src[k]
    assert got == src, got
    print("  [writeback] values written to their keys, md + mdx + toml OK")


_HARD = [
    "Bonjour : le monde",
    "- tiret en tete",
    "# diese en tete",
    "Valeur #1 et plus",
    "fin avec #",
    "l'ete arrive",
    'il a dit "oui"',
    "'cite'",
    '"double"',
    "true",
    "no",
    "123",
    "1e3",
    "2026-10-11",
    "[x] crochet",
    "{y} accolade",
    "@arobase",
    "`backtick",
    "%pourcent",
    "!bang",
    "&ancre",
    "*etoile",
    "|pipe",
    ">plus grand",
    "? question",
    "  espaces  ",
    "Unicode 日本語 et émojis",
    "barre \\ oblique",
    "tab\there",
    "null",
    "~",
    "a: b: c",
    "---",
]


def test_quoting():
    for line, unit in (
        (b"title: Plain source value", "Plain source value"),
        (b"title: 'Single ''quoted'' value'", "Single 'quoted' value"),
        (b'title: "Double \\"quoted\\" value"', 'Double "quoted" value'),
        (b"title: Value with comment # keep me", "Value with comment"),
    ):
        doc = b"---\n" + line + b"\nlayout: post\n---\n\nBody text.\n"
        assert _texts(MD, doc)[0] == unit == _texts(MDX, doc)[0], (_texts(MD, doc), unit)
        for new in [*_HARD, "ligne un\nligne deux", "crlf\r\nbreak"]:
            for f in (MD, MDX):
                out = f.build(doc, {"0": new})
                want = new.replace("\r\n", " ").replace("\n", " ")
                got = _fm(out)
                assert got == {"title": want, "layout": "post"}, (line, new, out)
                if b"# keep me" in line:
                    assert b"# keep me\n" in out, out
                assert out.endswith(b"\n---\n\nBody text.\n"), out
    for line in (b'title = "Basic source"', b"title = 'Literal source'"):
        doc = b"+++\n" + line + b"\ndraft = true\n+++\n\nBody text.\n"
        for new in [*_HARD, "ligne un\nligne deux", "it's", "'''", '"""']:
            out = MD.build(doc, {"0": new})
            got = _fm(out, "toml")
            assert got == {"title": new.replace("\n", " "), "draft": True}, (line, new, out)
    print("  [quoting] every translation re-parses to itself (yaml + toml) OK")


def test_skip():
    doc = (
        b"---\n"
        b"title: >\n"
        b"  A folded title\n"
        b"  over two lines\n"
        b"description: |\n"
        b"  A literal block\n"
        b"summary:\n"
        b"  - a list item\n"
        b"subtitle: A plain value that\n"
        b"  continues here\n"
        b"sidebar:\n"
        b"  title: Nested title\n"
        b"excerpt: 42\n"
        b"linkTitle: 2026-10-11\n"
        b"pagination_label: true\n"
        b"---\n"
        b"Body.\n"
    )
    assert _texts(MD, doc) == ["Body."], _texts(MD, doc)
    assert _texts(MDX, doc) == ["Body."], _texts(MDX, doc)
    malformed = b"---\ntitle: [unclosed\nlayout: post\n---\nBody text.\n"
    for f in (MD, MDX):
        assert _texts(f, malformed) == ["Body text."], (f.fmt, _texts(f, malformed))
        assert _translate_all(f, malformed, lambda t: "X") == malformed.replace(b"Body text.", b"X")
    bad_toml = b'+++\ntitle = "open\ndraft = false\n+++\nBody text.\n'
    assert _texts(MD, bad_toml) == ["Body text."], _texts(MD, bad_toml)
    os.environ["LOC_TMS_FRONT_MATTER_KEYS"] = "Title, keywords"
    try:
        doc = b"---\ntitle: T one\ndescription: D one\nkeywords: k one, k two\n---\nBody.\n"
        assert _texts(MD, doc) == ["T one", "k one, k two", "Body."], _texts(MD, doc)
    finally:
        del os.environ["LOC_TMS_FRONT_MATTER_KEYS"]
    print("  [skip] block scalars, lists, nested, non-strings, malformed, allowlist OK")


def test_not_front_matter():
    doc = b"Intro paragraph.\n\n---\ntitle: Not front matter\n---\n\nMore text.\n"
    assert _texts(MD, doc) == ["Intro paragraph.", "title: Not front matter", "More text."]
    assert _texts(MDX, doc) == ["Intro paragraph.", "title: Not front matter", "More text."]
    unclosed = b"---\ntitle: open rule\n\nBody text.\n"
    assert _texts(MD, unclosed) == ["title: open rule", "Body text."], _texts(MD, unclosed)
    prose = b"---\nA heading under a rule\n---\n\nBody text.\n"
    assert _texts(MD, prose) == ["A heading under a rule", "Body text."], _texts(MD, prose)
    assert _texts(MD, b"+++ not toml\n") == ["+++ not toml"]
    print("  [not fm] later rules, unclosed fence, prose block stay body text OK")


def test_identity():
    for f, src in ((MD, POST), (MDX, POST), (MDX, DOCU), (MD, HUGO)):
        for v in (
            src,
            src.replace(b"\n", b"\r\n"),
            b"\xef\xbb\xbf" + src,
            src.rstrip(b"\n"),
        ):
            assert f.build(v, {}) == v, (f.fmt, v[:30])
            assert _translate_all(f, v, lambda t: t) == v, (f.fmt, v[:30])
    # BOM and CRLF: front matter is still found and the value written in place
    v = b"\xef\xbb\xbf" + POST.replace(b"\n", b"\r\n")
    out = MD.build(v, {"0": "Titre : un"})
    assert out.startswith(b"\xef\xbb\xbf---\r\ntitle: 'Titre : un'\r\nlayout: post\r\n"), out
    print("  [identity] no-op builds byte-identical (LF, CRLF, BOM, no final newline) OK")


def test_walk():
    from app.filters.markdown_filter import MD_WALK_VERSION, md_walk_version

    assert MD.parse(POST).skeleton["md_walk"] == MD_WALK_VERSION >= 2  # #735: 3
    assert MDX.parse(DOCU).skeleton["md_walk"] == MD_WALK_VERSION
    assert md_walk_version({}) == 1 and md_walk_version(None) == 1
    assert md_walk_version({"md_walk": 2}) == 2
    for bad in (99, True, "2", None):
        try:
            md_walk_version({"md_walk": bad})
        except ValueError:
            pass
        else:
            raise AssertionError(bad)
    # walk 1 (a document imported before #731): unit 0 is the whole `title:` line
    out = MD.build_localized(POST, {"0": "X"}, "fr-FR", md_walk=1)
    assert out == POST.replace(b"title: Getting started with the dashboard", b"X"), out
    out = MDX.build_localized(DOCU, {"0": "X"}, "de-DE", md_walk=1)
    assert out == DOCU.replace(b"id: architecture", b"X"), out
    # walk 2 is the default
    out = MD.build_localized(POST, {"0": "X"}, "fr-FR")
    assert _fm(out)["title"] == "X"
    print("  [walk] md_walk stamp, legacy walk 1 kept for old imports OK")


def test_gate_allowlist_and_anchors():
    from app.filters.markdown_filter import md_fm_keys

    # the allowlist used at import is stored and reused at build (indices stay put)
    doc = b"---\ntitle: T one\nkeywords: k one\n---\nBody.\n"
    for f in (MD, MDX):
        skel = f.parse(doc).skeleton
        keys = md_fm_keys(skel)
        assert "title" in keys and "keywords" not in keys, skel
        os.environ["LOC_TMS_FRONT_MATTER_KEYS"] = "keywords,title"
        try:
            out = f.build_localized(doc, {"0": "T deux", "1": "Corps."}, "fr-FR", fm_keys=keys)
        finally:
            del os.environ["LOC_TMS_FRONT_MATTER_KEYS"]
        assert out == b"---\ntitle: T deux\nkeywords: k one\n---\nCorps.\n", (f.fmt, out)
    assert md_fm_keys({}) is None and md_fm_keys({"md_fm_keys": "title"}) is None
    # an anchored / tagged value is not offered: rewriting it would drop the anchor
    anch = b"---\ntitle: &t Hello world\nog_title: *t\ndescription: !!str Some text\n---\nBody.\n"
    for f in (MD, MDX):
        assert _texts(f, anch) == ["Body."], (f.fmt, _texts(f, anch))
        assert _fm(_translate_all(f, anch, lambda t: "X")) == _fm(anch)
    print("  [gate] stored allowlist keeps indices; anchors / tags left alone OK")


def test_end_to_end():
    from fastapi.testclient import TestClient
    from sqlmodel import Session

    from app.db import engine, init_db
    from app.main import app
    from app.models import Document, Project, Role, TargetLocale
    from app.services import apikeys, security
    from tests._cli_runner import run_cli

    init_db()
    with Session(engine) as s:
        p = Project(name="fm731", org_id=1, source_locale="en-US", default_provider="mock")
        s.add(p)
        s.commit()
        s.refresh(p)
        s.add(TargetLocale(project_id=p.id, locale="fr-FR"))
        s.commit()
        pid = p.id
    user = security.create_user("fm731@x.test", "fm731", Role.ADMIN, _TEST_PW)
    _row, raw = apikeys.create_key(user.id, "fm731", scopes="api docs admin")
    client = TestClient(app, raise_server_exceptions=False, follow_redirects=False)
    hdr = {"X-Loc-Token": raw}  # pregate: allow-secret throwaway test key
    r = client.post(
        "/api/documents",
        headers=hdr,
        data={"project_id": str(pid)},
        files={"file": ("post.md", POST, "text/markdown")},
    )
    assert r.status_code in (200, 201), (r.status_code, r.text)
    doc_id = r.json()["document_id"]
    code, out, err = run_cli(["doc", "translate", str(doc_id), "--locale", "fr-FR"])
    assert code == 0, (code, out, err)
    r = client.get(f"/api/documents/{doc_id}/download", headers=hdr, params={"locale": "fr-FR"})
    assert r.status_code == 200, (r.status_code, r.text)
    got = _fm(r.content)
    src = _fm(POST)
    assert set(got) == set(src), got
    for k in ("layout", "permalink", "date", "tags", "draft"):
        assert got[k] == src[k], (k, got)
    assert got["title"] != src["title"] and src["title"] in got["title"], got
    assert got["description"] != src["description"], got
    # gate #731: the allowlist is stored with the import; a host that changes
    # LOC_TMS_FRONT_MATTER_KEYS afterwards must not shift the stored indices
    first = r.content
    os.environ["LOC_TMS_FRONT_MATTER_KEYS"] = "keywords"
    try:
        r = client.get(f"/api/documents/{doc_id}/download", headers=hdr, params={"locale": "fr-FR"})
        assert r.status_code == 200 and r.content == first, r.content
    finally:
        del os.environ["LOC_TMS_FRONT_MATTER_KEYS"]
    # an unknown walk stamp is refused at export, never guessed
    with Session(engine) as s:
        d = s.get(Document, doc_id)
        d.skeleton = dict(d.skeleton, md_walk=99)
        s.add(d)
        s.commit()
    r = client.get(f"/api/documents/{doc_id}/download", headers=hdr, params={"locale": "fr-FR"})
    assert 400 <= r.status_code < 500, (r.status_code, r.text)
    print("  [e2e] .md: API upload, CLI translate, API export keeps valid front matter OK")


def main():
    test_keys()
    test_writeback()
    test_quoting()
    test_skip()
    test_not_front_matter()
    test_identity()
    test_walk()
    test_gate_allowlist_and_anchors()
    test_end_to_end()
    print("FRONT MATTER 731 OK")


if __name__ == "__main__":
    main()
