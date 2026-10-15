"""Markdown / MDX block structure is not prose (GitHub #735).

  refdef     link reference definitions (CommonMark 4.7): label and destination never
             offered, only the title (each delimiter `"`, `'`, `( )`, a title on the next
             line, an <angle> destination); a definition glued to a paragraph, or a line
             that only looks like one, stays prose
  title      translated titles with `"`, `'`, `( )`, a backslash, guillemets, Japanese
             brackets or a line break are written back escaped inside the source delimiter;
             a CommonMark reader (markdown-it-py, when installed) reads exactly the
             translation as the title and the links that use the definition still resolve
  footnote   `[^x]: ` stays byte-identical (markup prefix), the text after it is the unit
  code       indented code after a blank line / at the start is skipped; a list item's
             4-space continuation, a lazy paragraph line and a footnote continuation are
             still translated; code inside a list item (6+ spaces) is skipped; MDX has no
             indented code, so its 4-space lines stay text
  comment    a block of only `<!-- ... -->` comments (one or several lines) is skipped; a
             comment followed by text, an unclosed one and an inline one are as before
  cm         every line the Markdown filter offers is paragraph / heading content for
             markdown-it-py, and every such line with letters is offered
  identity   no-op and same-text builds are byte-identical (LF, CRLF, BOM, no final newline)
  walk       new skeletons carry md_walk = 3; a document imported under walk 2 (or 1)
             exports under its own numbering
  e2e        API upload, CLI translate (mock), API download of the soak readme: definitions,
             code and comment byte-identical, titles translated

Hermetic + offline. Final line: "MD BLOCKS 735 OK".
"""

import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(_ROOT)
sys.path.insert(0, _ROOT)
from tests._bootstrap import init_env

init_env("loctms_md735_")

from app.filters import get_filter

MD = get_filter("md")
MDX = get_filter("mdx")
_TEST_PW = "md735-pw-1234567"  # pregate: allow-secret throwaway test user

try:
    from markdown_it import MarkdownIt
except ImportError:  # structural assertions only
    MarkdownIt = None

README = (
    b"# Quick start\n"
    b"\n"
    b"Install the package and run the setup command.\n"
    b"\n"
    b"    pip install northwind-tools\n"
    b"    northwind init --region us-east\n"
    b"\n"
    b"<!-- Keep this section short. Translators: do not change the anchor names. -->\n"
    b"\n"
    b"Read the [installation guide][install] and the [FAQ][faq] before you start.\n"
    b"\n"
    b'[install]: https://example.com/docs/install "Installation guide"\n'
    b'[faq]: https://example.com/faq "Frequently asked questions"\n'
    b"\n"
    b"The footnote explains the licence[^lic].\n"
    b"\n"
    b"[^lic]: The tools are free for personal use."
)

REFS = (
    b"See [one][a], [two][b], [three][c], [four][d], [five][e] and [six][f].\n"
    b"\n"
    b'[a]: https://example.com/a "Double quoted title"\n'
    b"[b]: https://example.com/b 'Single quoted title'\n"
    b"[c]: https://example.com/c (Parenthesised title)\n"
    b'[d]: <https://example.com/d d> "Angle destination title"\n'
    b"[e]: https://example.com/e\n"
    b'   "Title on the next line"\n'
    b"[f]: https://example.com/f\n"
    b"\n"
    b"A paragraph line\n"
    b'[g]: https://example.com/g "glued to the paragraph"\n'
    b"\n"
    b"[Note]: Read this before you install anything.\n"
)


def _texts(f, data: bytes) -> list[str]:
    return [u.text for u in f.parse(data).units]


def _translate_all(f, data, fn):
    return f.build(data, {str(u.p_index): fn(u.text) for u in f.parse(data).units})


def _refs(data: bytes) -> dict:
    env: dict = {}
    MarkdownIt("commonmark").parse(data.decode("utf-8"), env)
    return env.get("references", {})


def test_readme():
    want = [
        "Quick start",
        "Install the package and run the setup command.",
        "Read the [installation guide][install] and the [FAQ][faq] before you start.",
        "Installation guide",
        "Frequently asked questions",
        "The footnote explains the licence[^lic].",
        "The tools are free for personal use.",
    ]
    assert _texts(MD, README) == want, _texts(MD, README)
    # MDX: the same, except that MDX has no indented code (the two lines are paragraph text)
    mdx = _texts(MDX, README)
    code = ["pip install northwind-tools", "northwind init --region us-east"]
    assert mdx == [*want[:2], *code, *want[2:]], mdx
    for f in (MD, MDX):
        joined = "\n".join(_texts(f, README))
        for gone in ("<!--", "-->", "anchor names", "https://", "[install]:", "[^lic]:"):
            assert gone not in joined, (f.fmt, gone)
    print("  [readme] soak readme: definitions, code, comment, footnote label not offered OK")


def test_refdefs():
    for f in (MD, MDX):
        texts = _texts(f, REFS)
        assert texts == [
            "See [one][a], [two][b], [three][c], [four][d], [five][e] and [six][f].",
            "Double quoted title",
            "Single quoted title",
            "Parenthesised title",
            "Angle destination title",
            "Title on the next line",
            "A paragraph line",
            '[g]: https://example.com/g "glued to the paragraph"',
            "[Note]: Read this before you install anything.",
        ], (f.fmt, texts)
    # escapes in a source title are decoded for the unit and kept when unchanged
    src = b'[x]: /u "Say \\"hi\\" \\\\ now"\n\n[x]\n'
    assert _texts(MD, src)[0] == 'Say "hi" \\ now', _texts(MD, src)
    assert MD.build(src, {"0": 'Say "hi" \\ now'}) == src
    print("  [refdef] label / destination never offered, every title delimiter OK")


HARD_TITLES = [
    'Guide d\'installation "officiel"',
    "« Questions fréquemment posées »",
    "「よくある質問」",
    'Back\\slash and \\" quote',
    "Paren (inside) and ) close",
    "It's 'single'",
    "Two\nlines",
    "Ends with a backslash \\",
]


def test_titles():
    for f in (MD, MDX):
        for t in HARD_TITLES:
            out = _translate_all(
                f, REFS, lambda s, t=t: t if s[0].isupper() and "title" in s.lower() else s
            )
            text = out.decode("utf-8")
            lines = text.splitlines()
            # label + destination + delimiters byte-identical
            assert lines[2].startswith('[a]: https://example.com/a "'), lines[2]
            assert lines[2].endswith('"') and lines[3].endswith("'") and lines[4].endswith(")")
            assert lines[3].startswith("[b]: https://example.com/b '"), lines[3]
            assert lines[4].startswith("[c]: https://example.com/c ("), lines[4]
            assert lines[6] == "[e]: https://example.com/e", lines[6]
            assert lines[7].startswith('   "') and lines[7].endswith('"'), lines[7]
            assert len(lines) == len(REFS.decode().splitlines()), lines
            # the units read back as the translation (line break folded)
            want = t.replace("\n", " ")
            again = _texts(f, out)
            assert again[1:6] == [want] * 5, (f.fmt, t, again)
            if MarkdownIt is None:
                continue
            refs = _refs(out)
            for label, href in (("A", "https://example.com/a"), ("B", "https://example.com/b")):
                got = (refs[label]["href"], refs[label]["title"])
                assert got == (href, want), (f.fmt, t, refs[label])
            assert refs["C"]["title"] == want and refs["D"]["title"] == want, refs
            assert refs["E"]["title"] == want and refs["F"]["title"] == "", refs
            assert refs["F"]["href"] == "https://example.com/f", refs
            html = MarkdownIt("commonmark").render(text)
            assert '<a href="https://example.com/a"' in html and "[one][a]" not in html, html
    where = "markdown-it-py" if MarkdownIt else "structural only (markdown-it-py not installed)"
    print(f"  [title] hard translated titles re-read as the translation ({where}) OK")


def test_footnote():
    src = b"Text[^n].\n\n[^n]: Note text here.\n    Continued note paragraph.\n"
    for f in (MD, MDX):
        assert _texts(f, src) == [
            "Text[^n].",
            "Note text here.",
            "Continued note paragraph.",
        ], (f.fmt, _texts(f, src))
        out = _translate_all(f, src, lambda s: "1. " + s)
        lines = out.decode().splitlines()
        assert lines[2].startswith("[^n]: 1"), lines[2]
        assert lines[2] == "[^n]: 1\\. Note text here.", lines[2]  # #704: no list opened
    # a footnote definition after a blank line with 4-space continuation is not code
    src2 = b"[^n]: Note.\n\n    Second note paragraph.\n"
    assert _texts(MD, src2) == ["Note.", "Second note paragraph."], _texts(MD, src2)
    print("  [footnote] `[^x]: ` kept as markup, its text and continuation translated OK")


CODE = (
    b"    indented code at the start\n"
    b"\n"
    b"Paragraph one.\n"
    b"    lazy continuation, not code\n"
    b"\n"
    b"    real indented code\n"
    b"\n"
    b"\tcode after a tab\n"
    b"\n"
    b"- List item.\n"
    b"\n"
    b"    List continuation paragraph.\n"
    b"\n"
    b"        list item code block\n"
    b"\n"
    b"1. Ordered step.\n"
    b"\n"
    b"    More for the step.\n"
    b"\n"
    b"Back at the top level.\n"
    b"\n"
    b"    top level code again\n"
    b"    ```\n"
    b"    not a fence\n"
    b"\n"
    b"Done.\n"
)


def test_indented_code():
    want = [
        "Paragraph one.",
        "lazy continuation, not code",
        "List item.",
        "List continuation paragraph.",
        "Ordered step.",
        "More for the step.",
        "Back at the top level.",
        "Done.",
    ]
    assert _texts(MD, CODE) == want, _texts(MD, CODE)
    mdx = _texts(MDX, CODE)
    for line in ("indented code at the start", "real indented code", "code after a tab"):
        assert line in mdx, (line, mdx)  # MDX: no indented code
    print("  [code] indented code skipped, list / lazy / footnote continuations kept OK")


COMMENTS = (
    b"<!-- one line comment -->\n"
    b"\n"
    b"Text before.\n"
    b"<!--\n"
    b"  A multi-line comment\n"
    b"  that says things.\n"
    b"-->\n"
    b"\n"
    b"  <!-- a --> <!-- b -->\n"
    b"\n"
    b"<!-- a comment --> then visible text\n"
    b"\n"
    b"Inline <!-- note --> comment stays in the line.\n"
)


def test_comments():
    for f in (MD, MDX):
        texts = _texts(f, COMMENTS)
        assert texts[:1] == ["Text before."], (f.fmt, texts)
        joined = "\n".join(texts)
        for gone in ("one line comment", "multi-line comment", "says things", "<!-- a -->"):
            assert gone not in joined, (f.fmt, gone, texts)
        assert "Inline <!-- note --> comment stays in the line." in joined, texts
    assert "<!-- a comment --> then visible text" in _texts(MD, COMMENTS)
    # unclosed: left as text, as before
    assert _texts(MD, b"<!-- never closed\nSome words.\n") == ["<!-- never closed", "Some words."]
    # gate #735: many openers closed by one `--> text` line stay linear (was quadratic:
    # every opener rescanned every line up to the close, ~3.4 s for 20k lines)
    import time

    from app.filters.markdown_filter import CommentBlocks

    lines = ["<!--"] * 60000 + ["--> tail text"]
    cb = CommentBlocks(lines)
    t0 = time.perf_counter()
    assert all(cb.end(i) is None for i in range(len(lines)))
    assert time.perf_counter() - t0 < 2.0, time.perf_counter() - t0
    ok = CommentBlocks(["<!--", "x", "<!--", "y --> <!-- z -->", "Text."])
    assert (ok.end(0), ok.end(2), ok.end(4)) == (3, 3, None)
    bad = CommentBlocks(["<!--", "--> <!-- open", "Text."])
    assert bad.end(0) is None
    one = CommentBlocks(["<!-- a --> <!-- b -->", "<!-- a --> b", "<!-->"])
    assert (one.end(0), one.end(1), one.end(2)) == (0, None, 2)
    print("  [comment] comment-only blocks skipped, mixed / inline / unclosed as before OK")


def _cm_lines(data: bytes) -> set:
    """Line indices markdown-it-py reads as paragraph or heading content."""
    out = set()
    for tok in MarkdownIt("commonmark").parse(data.decode("utf-8")):
        if tok.type == "inline" and tok.map:
            out.update(range(*tok.map))
    return out


def test_commonmark_agreement():
    if MarkdownIt is None:
        print("  [cm] markdown-it-py not installed: skipped")
        return
    from app.filters.markdown_filter import _iter_md

    for name, data in (("readme", README), ("refs", REFS), ("code", CODE), ("cmt", COMMENTS)):
        lines = data.decode().splitlines()
        # other HTML blocks (here a comment followed by text) are offered as before #735
        offered = {
            li for li, _s, _e, _t, _p, d in _iter_md(lines) if not d and lines[li][:1] != "<"
        }
        cm = {li for li in _cm_lines(data) if any(c.isalnum() for c in lines[li])}
        # GFM footnote definitions are paragraph text for plain CommonMark: same set
        assert offered == cm, (name, sorted(offered ^ cm), [lines[i] for i in offered ^ cm])
    print("  [cm] offered lines == markdown-it paragraph / heading lines OK")


def test_identity():
    for src in (README, REFS, CODE, COMMENTS):
        for v in (
            src,
            src.replace(b"\n", b"\r\n"),
            b"\xef\xbb\xbf" + src,
            src.rstrip(b"\n"),
        ):
            for f in (MD, MDX):
                assert f.build(v, {}) == v, f.fmt
                if f is MDX and src is COMMENTS:
                    continue  # MDX escapes a raw `<` in a text line (an MDX syntax error)
                same = {str(u.p_index): u.text for u in f.parse(v).units}
                assert f.build(v, same) == v, (f.fmt, v[:40])
    out = MD.build(REFS.replace(b"\n", b"\r\n"), {"1": 'Neu "x"'})
    assert b'[a]: https://example.com/a "Neu \\"x\\""\r\n' in out, out
    print("  [identity] no-op builds byte-identical (LF, CRLF, BOM, no final newline) OK")


def test_walk():
    from app.filters.markdown_filter import MD_WALK_VERSION, md_walk_version

    assert MD.parse(README).skeleton["md_walk"] == MD_WALK_VERSION == 3
    assert MDX.parse(README).skeleton["md_walk"] == 3
    assert md_walk_version({"md_walk": 3}) == 3 and md_walk_version({"md_walk": 2}) == 2
    # walk 2 (imported before #735): unit 2 is the first indented code line, unit 3 the
    # second, unit 6 the whole `[install]:` line
    out = MD.build_localized(README, {"2": "X", "6": "Y"}, "fr-FR", md_walk=2)
    assert out == README.replace(b"pip install northwind-tools", b"X").replace(
        b'[install]: https://example.com/docs/install "Installation guide"', b"Y"
    ), out
    # MDX walk 2: unit 4 is the whole comment line
    comment = b"<!-- Keep this section short. Translators: do not change the anchor names. -->"
    out = MDX.build_localized(README, {"4": "Z"}, "fr-FR", md_walk=2)
    assert out == README.replace(comment, b"Z"), out
    print("  [walk] md_walk stamp 3, walk 2 imports keep their numbering OK")


def test_end_to_end():
    from fastapi.testclient import TestClient
    from sqlmodel import Session

    from app.db import engine, init_db
    from app.main import app
    from app.models import Project, Role, TargetLocale
    from app.services import apikeys, security
    from tests._cli_runner import run_cli

    init_db()
    with Session(engine) as s:
        p = Project(name="md735", org_id=1, source_locale="en-US", default_provider="mock")
        s.add(p)
        s.commit()
        s.refresh(p)
        s.add(TargetLocale(project_id=p.id, locale="fr-FR"))
        s.commit()
        pid = p.id
    user = security.create_user("md735@x.test", "md735", Role.ADMIN, _TEST_PW)
    _row, raw = apikeys.create_key(user.id, "md735", scopes="api docs admin")
    client = TestClient(app, raise_server_exceptions=False, follow_redirects=False)
    hdr = {"X-Loc-Token": raw}  # pregate: allow-secret throwaway test key
    r = client.post(
        "/api/documents",
        headers=hdr,
        data={"project_id": str(pid)},
        files={"file": ("readme.md", README, "text/markdown")},
    )
    assert r.status_code in (200, 201), (r.status_code, r.text)
    doc_id = r.json()["document_id"]
    code, out, err = run_cli(["doc", "translate", str(doc_id), "--locale", "fr-FR"])
    assert code == 0, (code, out, err)
    r = client.get(f"/api/documents/{doc_id}/download", headers=hdr, params={"locale": "fr-FR"})
    assert r.status_code == 200, (r.status_code, r.text)
    got = r.content.decode("utf-8").splitlines()
    src = README.decode("utf-8").splitlines()
    for i in (4, 5, 7):  # indented code and the comment
        assert got[i] == src[i], (i, got[i])
    assert got[11].startswith('[install]: https://example.com/docs/install "'), got[11]
    assert got[12].startswith('[faq]: https://example.com/faq "'), got[12]
    assert got[11] != src[11] and got[16].startswith("[^lic]: ") and got[16] != src[16], got
    if MarkdownIt is not None:
        refs = _refs(r.content)
        assert set(refs) == {"INSTALL", "FAQ"}, refs
        assert refs["FAQ"]["href"] == "https://example.com/faq", refs
    print("  [e2e] .md: API upload, CLI translate, API export keeps definitions OK")


def main():
    test_readme()
    test_refdefs()
    test_titles()
    test_footnote()
    test_indented_code()
    test_comments()
    test_commonmark_agreement()
    test_identity()
    test_walk()
    test_end_to_end()
    print("MD BLOCKS 735 OK")


if __name__ == "__main__":
    main()
