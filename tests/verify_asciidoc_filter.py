"""AsciiDoc filter (.adoc, .asciidoc, .asc; format Batch 15, fmt/asciidoc).

  registry    adoc + asciidoc + asc resolve to the filter; listed in supported_exts
  identity    an untouched build is byte-identical (LF, CRLF, BOM, no final newline)
  units       paragraphs, titles, list items, dlists, admonitions, table cells translated;
              listing / literal / pass / comment blocks, attribute entries, header lines,
              [source] paragraphs, indented literals, include:: and comments skipped
  tokens      {attr}, kbd:, image:, monospace, passthrough as point tokens; formatting,
              link/xref/footnote text as paired tokens; hard line break kept
  translated  translation written in place; a table separator in a translation escaped
  lang        build_localized sets or adds :lang:
  encodings   cp1252 converted, binary refused (#509); OpenPGP .asc refused
  caps        segment cap and segment length cap (#571); hostile inputs stay fast
  surfaces    API upload -> CLI translate -> API download; /api/formats lists adoc

Hermetic + offline. Fixtures are hand-written below. Final line: "ASCIIDOC FILTER OK".
"""

import os
import sys
import time

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(_ROOT)
sys.path.insert(0, _ROOT)
from tests._bootstrap import init_env

init_env("loctms_asciidoc_")

from app.filters import get_filter, supported_exts
from app.filters.base import EmptyDocumentError, InputLimitError, UnsupportedEncodingError

LOC = "fr-FR"

DOC = rb"""= User Guide for {product-name}
Jane Doe <jane@example.com>
v1.2, 2026-10-01
:product-name: Acme Widget
:toc:
:description: This is a long description \
  that wraps onto a second line.

// A line comment that must stay untouched.

This is the *first* paragraph with a {product-name} reference.
It continues on a second line with _italic_ text and `inline code`.

== Getting Started ==

NOTE: Read the link:install.html[installation guide] before you begin.

.Supported platforms
* Windows with kbd:[Ctrl+C] support
* macOS, see <<mac-setup,the macOS section>>
** Nested item with https://example.com[our website^]
* [x] A checked item
. First step
. Second step +
continued after a hard break
<1> A callout explanation

CPU:: The brain of the computer.
Hard drive::
Permanent storage for data.

[source,python]
----
print("Hello, world")  # code stays
----

....
Literal block stays.
....

++++
<p>Raw passthrough stays</p>
++++

////
Comment block stays.
////

```js
fenced code stays
```

[source]
A source paragraph stays.

[NOTE]
====
An admonition block body is translated.
====

include::partials/legal.adoc[]

 An indented literal paragraph stays.

[cols="1,2"]
|===
|Name |Description

|Widget
|A small device with a \| pipe inside.

2+^.^|Spanning cell text
a|An AsciiDoc cell with *bold*
l|literal cell stays
|===

[format=csv]
|===
csv,cell stays
|===

image::diagram.png[Architecture diagram]

Footnote here.footnote:[A translatable footnote.] Water is H~2~O.

Two Line Title
--------------

Final paragraph with [.role]#highlighted text# and pass:[<u>raw</u>] and +{literal}+.
"""

TRANSLATED = {
    "User Guide for",
    "This is the",
    "Getting Started",
    "Read the",
    "Supported platforms",
    "Windows with",
    "macOS, see",
    "Nested item with",
    "A checked item",
    "First step",
    "Second step",
    "A callout explanation",
    "CPU",
    "The brain of the computer.",
    "Hard drive",
    "Permanent storage for data.",
    "An admonition block body is translated.",
    "Name",
    "Description",
    "Widget",
    "A small device with a",
    "Spanning cell text",
    "An AsciiDoc cell with",
    "Footnote here.",
    "Two Line Title",
    "Final paragraph with",
}
KEPT = [
    "Jane Doe",
    "v1.2",
    "Acme Widget",
    "line comment",
    "Hello, world",
    "Literal block stays",
    "Raw passthrough stays",
    "Comment block stays",
    "fenced code stays",
    "source paragraph stays",
    "partials/legal",
    "indented literal",
    "literal cell stays",
    "csv,cell stays",
    "Architecture diagram",
]


def _f():
    return get_filter("adoc")


def _round(data, mapping=None):
    return _f().build(data, mapping or {})


def _units(data):
    return _f().parse(data).units


def test_registry():
    for ext in ("adoc", "asciidoc", "asc"):
        f = get_filter(ext)
        assert f is not None and f.fmt == "adoc", ext
        assert ext in supported_exts(), ext
    print("  [registry] .adoc .asciidoc .asc registered OK")


def test_identity():
    for data in (
        DOC,
        DOC.replace(b"\n", b"\r\n"),
        b"\xef\xbb\xbf" + DOC,
        DOC.rstrip(b"\n"),
        b"",
        b"\n\n",
    ):
        if not data.strip(b"\xef\xbb\xbf\r\n"):
            assert _units(data) == [], data
            continue
        units = _units(data)
        assert _round(data) == data
        same = {str(u.p_index): u.text for u in units}
        assert _round(data, same) == data, "no-change mapping must be byte-identical"
    crlf = DOC.replace(b"\n", b"\r\n")
    assert [u.text for u in _units(crlf)] == [u.text for u in _units(DOC)]
    print("  [identity] LF, CRLF, BOM, no final newline round-trip byte-identical OK")


def test_units():
    units = _units(DOC)
    texts = [u.text for u in units]
    joined = "\n".join(texts)
    for want in TRANSLATED:
        assert any(want in t for t in texts), (want, texts)
    for kept in KEPT:
        assert kept not in joined, (kept, texts)
    assert texts[0].startswith("User Guide for"), texts[0]
    assert "Getting Started" in texts and not any(t.endswith("==") for t in texts)
    assert "Supported platforms" in texts  # block title without its dot
    assert "A checked item" in texts and "A callout explanation" in texts
    assert "CPU" in texts and "Hard drive" in texts  # dlist terms are their own units
    assert [u.p_index for u in units] == list(range(len(units)))
    # #733: the header's wrapped :description: is the LAST unit (appended), joined
    meta = [(u.text, u.style) for u in units if u.style]
    assert meta == [
        ("This is a long description that wraps onto a second line.", "meta:description")
    ], meta
    assert units[-1].style == "meta:description"
    print(f"  [units] {len(units)} units; verbatim, comments, attributes, include skipped OK")


def test_tokens():
    units = {u.text.split("⟦")[0].strip(): u for u in _units(DOC)}
    first = units["This is the"]
    assert first.text == (
        "This is the ⟦g1⟧first⟦/g1⟧ paragraph with a ⟦x2⟧ "
        "reference. It continues on a second line with ⟦g3⟧italic⟦/g3⟧ "
        "text and ⟦x4⟧."
    ), first.text
    assert first.inline_map == {
        "1": ["*", "*"],
        "2": "{product-name}",
        "3": ["_", "_"],
        "4": "`inline code`",
    }, first.inline_map
    link = units["Read the"]
    assert "⟦g1⟧installation guide⟦/g1⟧" in link.text, link.text
    assert link.inline_map["1"] == ["link:install.html[", "]"]
    assert units["Windows with"].inline_map["1"] == "kbd:[Ctrl+C]"
    assert units["macOS, see"].inline_map["1"] == ["<<mac-setup,", ">>"]
    assert units["Nested item with"].inline_map["1"] == ["https://example.com[", "^]"]
    brk = units["Second step"]
    assert brk.inline_map["1"] == " +\n", brk.inline_map
    foot = units["Footnote here."]
    assert foot.inline_map["1"] == ["footnote:[", "]"] and foot.inline_map["2"] == ["~", "~"]
    final = units["Final paragraph with"]
    assert final.inline_map["1"] == ["[.role]#", "#"]
    assert final.inline_map["2"] == "pass:[<u>raw</u>]" and final.inline_map["3"] == "+{literal}+"
    pipe = units["A small device with a"]
    assert pipe.inline_map["1"] == "\\|"
    # resource-style placeholders in prose are left as text (AsciiDoc has none of its own)
    p = _units(b"Use %s or {0} here.\n")
    assert p[0].text == "Use %s or ⟦x1⟧ here.", p[0].text  # {0} is an attribute ref
    print("  [tokens] attributes, macros, marks, xrefs, footnotes, hard breaks OK")


def test_translated():
    units = _units(DOC)
    mapping = {}
    for u in units:
        mapping[str(u.p_index)] = "FR " + u.text
    out = _round(DOC, mapping).decode("utf-8")
    assert "= FR User Guide for {product-name}\nJane Doe <jane@example.com>" in out
    assert "== FR Getting Started ==" in out
    assert "NOTE: FR Read the link:install.html[installation guide] before" in out
    assert ".FR Supported platforms\n* FR Windows with kbd:[Ctrl+C] support" in out
    assert "* [x] FR A checked item" in out and "<1> FR A callout explanation" in out
    assert ". FR Second step +\ncontinued after a hard break" in out
    assert "FR CPU:: FR The brain of the computer.\nFR Hard drive::\nFR Permanent" in out
    assert "FR This is the *first* paragraph with a {product-name} reference. It continues" in out
    assert "2+^.^|FR Spanning cell text\na|FR An AsciiDoc cell with *bold*\nl|literal" in out
    assert "FR Two Line Title\n--------------" in out
    # #733: the wrapped header description is written back on one line
    assert (
        ":toc:\n:description: FR This is a long description that wraps onto a second line.\n\n"
        "// A line" in out
    ), out[:400]
    for kept in KEPT:
        assert kept in out, kept
    assert "⟦" not in out
    # a table separator inside a translated cell is escaped; markup tokens re-inserted
    cell = next(u for u in units if u.text == "Widget")
    out2 = _round(DOC, {str(cell.p_index): "Truc | machin"}).decode("utf-8")
    assert "|Truc \\| machin\n" in out2, out2
    para = next(u for u in units if u.text.startswith("This is the"))
    out3 = _round(
        DOC,
        {str(para.p_index): "C'est le ⟦g1⟧premier⟦/g1⟧ (⟦x2⟧)\n\nfin"},
    ).decode("utf-8")
    assert "C'est le *premier* ({product-name}) fin\n" in out3, out3
    # unknown tokens are dropped, never leaked
    out4 = _round(DOC, {str(para.p_index): "x ⟦x9⟧ y"}).decode("utf-8")
    assert "x  y" in out4 and "⟦" not in out4
    # CRLF file: untouched line endings stay CRLF
    crlf = DOC.replace(b"\n", b"\r\n")
    outc = _round(crlf, mapping)
    assert b"\r\n" in outc and b"\n" not in outc.replace(b"\r\n", b"")
    print("  [translated] in-place rebuild, cell escaping, token restore, CRLF OK")


META_DOC = b"""// leading comment
= Getting started with the dashboard
Jane Doe <jane@example.com>
:description: Learn how to set up {product} in five minutes.
:keywords: setup, onboarding
:page-layout: post
:toc: left
:robots: noindex
:description-extra: not a meta
:keywords-x: nope
:imagesdir: images

Open the dashboard and click *New project*.

:description: A body attribute entry, not the header's.
"""


def test_header_meta_733():
    """#733: the header's :description: / :keywords: values are units appended after
    every body unit; other entries (and a body entry) stay; written back on one line, a
    trailing backslash can never become a line continuation; no-op builds identical."""
    f = _f()
    units = _units(META_DOC)
    body = [u for u in units if not u.style]
    meta = [(u.p_index, u.text, u.style) for u in units if u.style]
    assert [u.text for u in body] == [
        "Getting started with the dashboard",
        "Open the dashboard and click ⟦g1⟧New project⟦/g1⟧.",
    ], body
    n = len(body)
    assert meta == [
        (n, "Learn how to set up ⟦x1⟧ in five minutes.", "meta:description"),
        (n + 1, "setup, onboarding", "meta:keywords"),
    ], meta
    # body indexes are what a document without the metadata gives
    plain = META_DOC.replace(b":description: Learn", b":page-x: Learn").replace(
        b":keywords: setup", b":page-y: setup"
    )
    assert [(u.p_index, u.text) for u in _units(plain)] == [(u.p_index, u.text) for u in body]
    assert f.build(META_DOC, {}) == META_DOC
    assert f.build(META_DOC, {str(u.p_index): u.text for u in units}) == META_DOC
    mapping = {
        str(n): "Apprenez ⟦x1⟧\nen cinq minutes \\",
        str(n + 1): "installation, prise en main",
        "0": "Bien demarrer",
    }
    out = f.build(META_DOC, mapping).decode()
    assert "= Bien demarrer\n" in out, out
    assert ":description: Apprenez {product} en cinq minutes {backslash}\n:keywords: " in out
    assert ":keywords: installation, prise en main\n:page-layout: post\n" in out, out
    for kept in (":robots: noindex", ":description-extra: not a meta", ":keywords-x: nope"):
        assert kept in out, kept
    assert ":description: A body attribute entry, not the header's.\n" in out
    assert len(_units(out.encode())) == len(units)  # still one entry each, still aligned
    # a wrapped value: joined into one unit, written back on one line
    wrapped = b"= T\n:keywords: one, \\\n  two, \\\n  three\n:toc:\n\nBody.\n"
    wu = _units(wrapped)
    assert [(u.text, u.style) for u in wu] == [
        ("T", ""),
        ("Body.", ""),
        ("one, two, three", "meta:keywords"),
    ], wu
    assert f.build(wrapped, {}) == wrapped
    assert f.build(wrapped, {"2": "un, deux"}) == b"= T\n:keywords: un, deux\n:toc:\n\nBody.\n"
    # a value with a hard line break, a letterless value and a body-only file: not offered
    assert not [u for u in _units(b"= T\n:description: a + \\\nb\n\nx\n") if u.style]
    assert not [u for u in _units(b"= T\n:keywords: 123, {x}\n\nx\n") if u.style]
    assert not [u for u in _units(b"Para.\n\n:description: later\n") if u.style]
    # build_localized still stamps :lang: at the end of the header
    loc = f.build_localized(META_DOC, mapping, "fr-FR").decode()
    assert ":imagesdir: images\n:lang: fr-FR\n\nOpen" in loc, loc
    print("  [#733] header description / keywords appended, one line, others kept OK")


def test_lang():
    f = _f()
    out = f.build_localized(DOC, {}, "fr-FR").decode("utf-8")
    assert ":description: This is a long description \\\n  that wraps onto a second line.\n" in out
    assert "that wraps onto a second line.\n:lang: fr-FR\n\n// A line" in out, out[:400]
    has = b"= Title\n:lang: en\n:toc:\n\nBody text.\n"
    assert f.build_localized(has, {}, "de").decode() == "= Title\n:lang: de\n:toc:\n\nBody text.\n"
    bare = b"Just a paragraph.\n"
    assert f.build_localized(bare, {}, "ja").decode() == ":lang: ja\n\nJust a paragraph.\n"
    assert f.build_localized(bare, {}, "bad value\n:x:") == bare  # malformed locale ignored
    assert f.build(has, {}) == has  # plain build never stamps
    print("  [lang] :lang: rewritten, added at header end, or added as a header OK")


def test_encodings():
    data = "Café crème paragraph.\n".encode("cp1252")
    p = _f().parse(data)
    assert p.units[0].text == "Café crème paragraph."
    assert p.skeleton.get("source_encoding") == "cp1252"
    out = _round(data, {"0": "Café traduit."})
    assert out == "Café traduit.\n".encode()
    try:
        _f().parse(b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x81\x8d\x00" * 20)
    except UnsupportedEncodingError:
        pass
    else:
        raise AssertionError("binary must be refused")
    try:
        _f().parse(b"-----BEGIN PGP SIGNATURE-----\n\niQEz\n-----END PGP SIGNATURE-----\n")
    except EmptyDocumentError as exc:
        assert "OpenPGP" in str(exc)
    else:
        raise AssertionError("OpenPGP .asc must be refused")
    print("  [encodings] cp1252 converted, binary and OpenPGP refused OK")


def test_caps():
    from app import config

    old_n, old_c = config.MAX_DOCUMENT_SEGMENTS, config.MAX_SEGMENT_CHARS
    try:
        config.MAX_DOCUMENT_SEGMENTS = 5
        many = b"".join(b"* item %d\n" % i for i in range(20))
        try:
            _f().parse(many)
        except InputLimitError:
            pass
        else:
            raise AssertionError("segment cap not enforced")
        config.MAX_DOCUMENT_SEGMENTS = old_n
        config.MAX_SEGMENT_CHARS = 50
        try:
            _f().parse(b"word " * 30 + b"\n")
        except InputLimitError:
            pass
        else:
            raise AssertionError("segment length cap not enforced")
    finally:
        config.MAX_DOCUMENT_SEGMENTS, config.MAX_SEGMENT_CHARS = old_n, old_c
    print("  [caps] segment count and length caps enforced OK")


def test_hostile():
    cases = [
        b"*" * 20000 + b"\n",
        b"* " * 10000 + b"x\n",
        b"_a " * 8000 + b"\n",
        b"`x " * 8000 + b"\n",
        b"link:" * 5000 + b"\n",
        b"{" * 20000 + b"\n",
        b"<<" * 10000 + b"\n",
        b"|===\n" + b"|" * 20000 + b"\n|===\n",
        b"----\n" + b"never closed\n" * 2000,
        b"[[" * 10000 + b"\n",
        b"text " + b"+" * 20000 + b"\n",
        b"a\\" * 10000 + b"\n",
        b"include::/etc/passwd[]\ninclude::https://evil.example/x.adoc[]\nok text\n",
    ]
    for data in cases:
        t0 = time.monotonic()
        units = _units(data)
        assert _round(data) == data
        assert _round(data, {str(u.p_index): u.text for u in units}) == data
        took = time.monotonic() - t0
        assert took < 5, (data[:40], took)
    inc = _units(cases[-1])
    assert [u.text for u in inc] == ["ok text"], [u.text for u in inc]
    print("  [hostile] pathological marks, tables, unclosed blocks, include targets OK")


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
        p = Project(name="adoc", org_id=1, source_locale="en-US", default_provider="mock")
        s.add(p)
        s.commit()
        s.refresh(p)
        s.add(TargetLocale(project_id=p.id, locale=LOC))
        s.commit()
        pid = p.id
    user = security.create_user("adoc@x.test", "adoc", Role.ADMIN, "adoc-pw-123456")
    _row, raw = apikeys.create_key(user.id, "adoc", scopes="api docs admin")
    client = TestClient(app, raise_server_exceptions=False, follow_redirects=False)
    hdr = {"X-Loc-Token": raw}  # pregate: allow-secret throwaway test key

    r = client.get("/api/formats", headers=hdr)
    assert r.status_code == 200, r.text
    exts = {f["ext"]: f["name"] for f in r.json()["formats"]}
    assert "adoc" in exts and "asc" in exts, exts

    r = client.post(
        "/api/documents",
        headers=hdr,
        data={"project_id": str(pid)},
        files={"file": ("guide.adoc", DOC, "text/plain")},
    )
    assert r.status_code in (200, 201), (r.status_code, r.text)
    doc = r.json()["document_id"]
    code, out, err = run_cli(["doc", "translate", str(doc), "--locale", LOC])
    assert code == 0, (code, out, err)
    r = client.get(f"/api/documents/{doc}/download", headers=hdr, params={"locale": LOC})
    assert r.status_code == 200, (r.status_code, r.text)
    body = r.content
    assert b"\xe2\x9f\xa6" not in body, body  # no leaked token brackets
    assert b'print("Hello, world")  # code stays' in body
    assert b"include::partials/legal.adoc[]" in body
    assert b":lang: fr-FR" in body, body[:400]
    r = client.post(
        "/api/documents",
        headers=hdr,
        data={"project_id": str(pid)},
        files={"file": ("key.asc", b"-----BEGIN PGP PUBLIC KEY BLOCK-----\n\nx\n", "text/plain")},
    )
    assert r.status_code == 422, (r.status_code, r.text)
    print("  [surfaces] API upload, CLI translate, download with :lang:; PGP .asc -> 422 OK")


def test_block_syntax_guard():
    """Gate fix: a paragraph translation that opens with block syntax ("== ", "* ",
    ".Title", "NOTE: ", "[x]", "//") must stay a paragraph, so the re-parse keeps the
    same unit count and no new section/list/comment appears. Pre-fix: "== x" became a
    section title and "// x" a comment (the unit vanished on re-parse)."""
    src = b"= T\n\nFirst para.\n\nSecond para.\n\nThird para.\n\n* item\n"
    units = _units(src)
    assert [u.text for u in units] == ["T", "First para.", "Second para.", "Third para.", "item"]
    tr = {"1": "== Pas un titre", "2": "// pas un commentaire", "3": "* pas une liste"}
    out = _round(src, tr)
    text = out.decode()
    assert "\n{empty}== Pas un titre\n" in text, text
    assert "\n{empty}// pas un commentaire\n" in text, text
    assert "\n{empty}* pas une liste\n" in text, text
    assert "* item" in text  # list item content is not guarded (it follows its marker)
    again = _units(out)
    assert len(again) == len(units), [u.text for u in again]
    # A plain translation is never prefixed.
    assert b"{empty}" not in _round(src, {"1": "Premier paragraphe."})
    print("  block-syntax guard: translated paragraphs stay paragraphs")


def test_cjk_constrained_marks():
    """#695: a constrained mark (`` `code` ``, ``*b*``, ``_i_``, ``#m#``, ``+p+``) glued to a
    word character in the translation (Japanese/Chinese have no spaces) is written in the
    doubled, unconstrained form, so Asciidoctor still formats it. Pre-fix the export kept
    the single marks and the reader saw literal backticks/asterisks (real CONTRIBUTING.adoc
    into ja-JP: 17 of 71 units). Marks next to a space or punctuation stay single, and an
    untouched or Latin translation keeps the source bytes."""
    import re

    src = b"Run `make` then *bold* and _it_ and #hi# and +pass+ and [.path]_x_ and **two** end.\n"
    unit = _units(src)[0].text
    toks = re.findall(r"⟦/?[a-z]?\d+⟧", unit)
    assert len(toks) == 12, unit
    ja = "".join("あ" + t for t in toks) + "。"  # every token glued to a kana
    out = _round(src, {"0": ja}).decode()
    want = "あ``make``あ**あ**あ__あ__あ##あ##あ++pass++あ[.path]__あ__あ**あ**。\n"
    assert out == want, out
    # The export re-parses with every token (nothing became literal text).
    again = _units(out.encode())[0].text
    assert len(re.findall(r"⟦/?[a-z]?\d+⟧", again)) == 12, again
    # Punctuation and spaces are boundaries: the single form stays.
    ja2 = "（" + toks[0] + "、" + toks[1] + "x" + toks[2] + "。"
    out2 = _round(src, {"0": ja2}).decode()
    assert out2.startswith("（`make`、*x*。"), out2
    # Connector punctuation is a word character too (full-width low line, gate).
    ja4 = "（" + toks[0] + "＿。"
    out4 = _round(src, {"0": ja4}).decode()
    assert out4.startswith("（``make``＿。"), out4
    # A pair whose translated text starts with a space cannot stay constrained.
    out3 = _round(src, {"0": "Lancez " + toks[0] + " puis " + toks[1] + " gras" + toks[2]})
    assert out3.decode() == "Lancez `make` puis ** gras**\n", out3
    # A Latin translation with spaces keeps the source form byte for byte.
    fr = unit.replace("Run", "Lancez").replace(" end", " fin")
    assert _round(src, {"0": fr}) == src.replace(b"Run", b"Lancez").replace(b" end", b" fin")
    print("  [cjk] constrained marks glued to CJK text written unconstrained (#695)")


def test_cjk_links():
    """#698: Asciidoctor links a bare URL, ``https://x[text]`` and ``{uri-attr}[text]`` only
    after a space, the line start or one of < > ( ) [ ] ; " ', and a bare URL runs on into a
    following letter. A Chinese translation glues them to letters (real README-fr.adoc into
    zh-CN: 7 units printed raw URLs). The build writes the ``link:`` macro form there; links
    after a space, and the link:/mailto: macros, are unchanged."""
    import re

    src = (
        b":uri-js: https://js.example.org\n\n"
        b"See https://brew.sh/[brew.sh] and {uri-js}[JS] or https://example.org now, "
        b"mail mailto:team@example.invalid[us] and link:https://x.org[X] and {uri-js}.\n"
    )
    unit = _units(src)[0].text
    toks = re.findall(r"⟦/?[a-z]?\d+⟧", unit)
    assert len(toks) == 9, unit
    zh = (
        "请"
        + toks[0]
        + "brew.sh"
        + toks[1]
        + "和"
        + toks[2]
        + "[JS]或"
        + toks[3]
        + "现在，"
        + toks[4]
        + "我们"
        + toks[5]
        + "和"
        + toks[6]
        + "X"
        + toks[7]
        + "和"
        + toks[8]
        + "。"
    )
    body = _round(src, {"0": zh}).decode().split("\n\n", 1)[1]
    want = (
        "请link:https://brew.sh/[brew.sh]和link:{uri-js}[JS]或"
        "link:https://example.org[]现在，mailto:team@example.invalid[我们]"
        "和link:https://x.org[X]和link:{uri-js}[]。\n"
    )
    assert body == want, body
    # Gate: a bare URL runs on over ASCII ",.)" too when a letter follows; an attribute the
    # document defines as a non-URL is never made a link, even before "[".
    src2 = b":ver: 2.0\n\nGet https://example.org, then {ver}[beta] and https://a.org.\n"
    t2 = re.findall(r"⟦/?[a-z]?\d+⟧", _units(src2)[0].text)
    out2 = _round(src2, {"0": "获取" + t2[0] + ",然后" + t2[1] + "[beta]和" + t2[2] + "。"})
    assert out2.decode().endswith(
        "获取link:https://example.org[],然后{ver}[beta]和link:https://a.org[]。\n"
    ), out2
    # "," or "." then a space or the end: the URL stops there, nothing changes.
    out3 = _round(src2, {"0": "Hol " + t2[0] + ", dann " + t2[1] + "[beta] und " + t2[2] + "."})
    assert out3.decode().endswith(
        "Hol https://example.org, dann {ver}[beta] und https://a.org.\n"
    ), out3
    # After a space or an allowed character nothing changes; a Latin translation keeps bytes.
    spaced = "请 " + toks[0] + "brew.sh" + toks[1] + " (" + toks[3] + ")"
    assert (
        _round(src, {"0": spaced})
        .decode()
        .endswith("请 https://brew.sh/[brew.sh] (https://example.org)\n")
    )
    fr = unit.replace("See", "Voir").replace(" now", " maintenant")
    assert _round(src, {"0": fr}) == src.replace(b"See", b"Voir").replace(b" now", b" maintenant")
    print("  [cjk] URL links glued to CJK text written as link: macros (#698)")


def main():
    test_registry()
    test_identity()
    test_units()
    test_tokens()
    test_translated()
    test_header_meta_733()
    test_block_syntax_guard()
    test_cjk_constrained_marks()
    test_cjk_links()
    test_lang()
    test_encodings()
    test_caps()
    test_hostile()
    test_surfaces()
    print("ASCIIDOC FILTER OK")


if __name__ == "__main__":
    main()
