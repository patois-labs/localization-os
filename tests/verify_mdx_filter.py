"""MDX filter (file-format Batch 15): Markdown + JSX + ESM, built on the Markdown filter.

  identity   build with no changes, and with every unit unchanged, is byte-identical
             (fixture, CRLF, BOM, no trailing newline)
  walk       import/export blocks, flow JSX lines (multi-line tags too), flow
             {expressions} and {/* comments */}, fenced code are skipped; Markdown between
             a component's open and close tags is translated; front matter is
             handled exactly like the Markdown filter (#731: allowlisted values only)
  tokens     inline JSX pairs -> g tokens, self-closing / unpaired / expressions -> x tokens;
             code spans and backslash escapes stay text; a multi-line inline expression
             is one token; braces inside JS strings and comments do not unbalance
  translate  tokens are rematerialised (reordered too), invented tokens dropped, a brace or
             < the translation adds is escaped, code spans are not escaped
  attrs      only allowlisted string attributes are units, appended after the line units;
             LOC_TMS_MDX_ATTRS changes the list; quotes in a translation are entity-encoded;
             an attribute inside a translated line and inside an untranslated line both build
  markdown   the same text through the md filter and the mdx filter gives the same units
             for a plain-Markdown file (MDX adds nothing to plain prose)
  a11y       empty Markdown alt and <img> without alt are reported
  hostile    unclosed braces/tags read as text, a flood of them is refused, segment cap,
             a huge line, binary input refused
  surfaces   registry, upload accept list, msgraph map, MIME, API labels, help, llms.txt,
             CLI.md, architecture doc; API upload + CLI translate + API export end to end

Hermetic + offline. Final line: "MDX FILTER OK".
"""

import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(_ROOT)
sys.path.insert(0, _ROOT)
from tests._bootstrap import init_env

init_env("loctms_mdxfilter_")

from app.filters import get_filter, supported_exts
from app.filters.base import InputLimitError, UnsupportedEncodingError

FIX = os.path.join(_ROOT, "tests", "fixtures", "formats", "mdx")
F = get_filter("mdx")
MD = get_filter("md")
_TEST_PW = "mdx-pw-1234567"  # pregate: allow-secret throwaway test user


def _fixture(name: str) -> bytes:
    with open(os.path.join(FIX, name), "rb") as fh:
        return fh.read().replace(b"\r\n", b"\n")  # autocrlf checkouts


def _texts(data: bytes) -> list[str]:
    return [u.text for u in F.parse(data).units]


def _build(data: bytes, fn) -> bytes:
    return F.build(data, {str(u.p_index): fn(u.text) for u in F.parse(data).units})


def test_identity():
    src = _fixture("guide.mdx")
    variants = [
        src,
        src.replace(b"\n", b"\r\n"),
        b"\xef\xbb\xbf" + src,
        src.rstrip(b"\n"),
        b"",
        b"   \n\n",
    ]
    for v in variants:
        assert F.build(v, {}) == v, v[:40]
        assert _build(v, lambda t: t) == v, v[:40]
    # BOM does not hide a first-line import
    assert _texts(b"\xef\xbb\xbfimport X from 'x'\n\nHello\n") == ["Hello"]
    print("  [identity] no-change and same-text builds are byte-identical OK")


def test_walk():
    texts = _texts(_fixture("guide.mdx"))
    assert texts[0] == "Getting started", texts  # front matter like md (#731: the value)
    assert "Make sure the server is **running** first." in texts
    joined = "\n".join(texts)
    for gone in (
        "import",
        "export",
        "author",
        "Ada",
        "props.items",
        "comment the translator",
        "Not translated",
        "Hidden",
        "Chart",
        "data=",
    ):
        assert gone not in joined, (gone, texts)
    # ESM block runs to the blank line, even with a non-keyword line inside
    src = b"export const a = {\n  b: 'Hello world'\n}\nstill esm text\n\nReal prose.\n"
    assert _texts(src) == ["Real prose."]
    # a word that merely starts with "import" is prose
    assert _texts(b"Importing data is easy.\n") == ["Importing data is easy."]
    assert _texts(b"import\n") == ["import"]  # no space: not ESM (mdxjs-esm)
    # gate: ESM cannot interrupt a paragraph (mdxjs-esm checks self.interrupt), so a
    # wrapped prose line starting "export " and the rest of its paragraph stay prose
    src = b"When the run ends you can\nexport the file as PDF\nor keep it.\n\nexport const x = 1\n"
    assert _texts(src) == ["When the run ends you can", "export the file as PDF", "or keep it."]
    # ... but after a heading, a rule or a flow tag it is ESM again
    assert _texts(b"# Title\nimport A from 'a'\n\nBody.\n") == ["Title", "Body."]
    assert _texts(b"<Note />\nexport const y = 2\n\nBody.\n") == ["Body."]
    # multi-line flow tag and flow expression
    src = b"<Tabs\n  items={['a', 'b']}\n>\n  <Tab>\n  Tab one text.\n  </Tab>\n</Tabs>\n"
    assert _texts(src) == ["Tab one text."]
    src = b"{\n  // a } in a comment\n  '}'\n}\n\nAfter.\n"
    assert _texts(src) == ["After."]
    # two tags on one line, nothing else: flow
    assert _texts(b"<A></A>\n<B /> <C />\n") == []
    print("  [walk] ESM, flow JSX, flow expressions, fences, front matter OK")


def test_tokens():
    u = F.parse(b'Press <Kbd>Ctrl</Kbd> and {count} more <Icon name="x" /> now.\n').units
    assert [x.text for x in u] == ["Press ⟦g1⟧Ctrl⟦/g1⟧ and ⟦x2⟧ more ⟦x3⟧ now."]
    assert u[0].inline_map["1"] == {"open": "<Kbd>", "close": "</Kbd>"}
    assert u[0].inline_map["2"] == {"point": "{count}"}
    # unpaired open and orphan close -> points; nested pairs
    t = _texts(b"a <B>b <I>c</I> d\n")[0]
    assert t == "a ⟦x1⟧b ⟦g2⟧c⟦/g2⟧ d", t
    t = _texts(b"x </B> y\n")[0]
    assert t == "x ⟦x1⟧ y", t
    # a line starting with inline JSX followed by text is a text line
    assert _texts(b"<Kbd>Ctrl</Kbd> copies.\n") == ["⟦g1⟧Ctrl⟦/g1⟧ copies."]
    # code spans and escapes stay text
    assert _texts(b"Use `{x}` and \\{y\\} here.\n") == ["Use `{x}` and \\{y\\} here."]
    # multi-line inline expression: one token, the line unit extends over it
    src = b"Total: {items\n  .length} items.\nNext line.\n"
    assert _texts(src) == ["Total: ⟦x1⟧ items.", "Next line."]
    assert _build(src, lambda t: t.upper().replace("⟦X1⟧", "⟦x1⟧")) == (
        b"TOTAL: {items\n  .length} ITEMS.\nNEXT LINE.\n"
    )
    # braces in strings / template literals inside an expression
    assert _texts(b"Say {'}' + `{`} hi.\n") == ["Say ⟦x1⟧ hi."]
    # a line of only tokens has no text: not a unit
    assert _texts(b"Hi {a} <B/>\n{a} <B/> x\n") == ["Hi ⟦x1⟧ ⟦x2⟧", "⟦x1⟧ ⟦x2⟧ x"]
    print("  [tokens] inline JSX and expressions are protected tokens OK")


def test_translate():
    src = b"# Hello {name}\n\nPress <Kbd>Ctrl</Kbd> + <Kbd>C</Kbd>.\n"
    units = F.parse(src).units
    out = F.build(
        src,
        {
            "0": "Bonjour ⟦x1⟧",
            "1": "Appuyez ⟦g2⟧C⟦/g2⟧ puis ⟦g1⟧Ctrl⟦/g1⟧ ⟦x9⟧.",
        },
    )
    assert len(units) == 2
    assert out == b"# Bonjour {name}\n\nAppuyez <Kbd>C</Kbd> puis <Kbd>Ctrl</Kbd> .\n", out
    # escaping: a brace / < the translation adds; code spans untouched; existing escapes kept
    out = F.build(b"Plain text.\n", {"0": "a {b} <c> `{d}` \\{e"})
    assert out == b"a \\{b\\} \\<c> `{d}` \\{e\n", out
    # the md prefix is kept
    out = F.build(b"> - **Bold** item\n", {"0": "**Gras** element"})
    assert out == b"> - **Gras** element\n", out
    # CRLF and BOM kept around a translated line
    out = F.build(b"\xef\xbb\xbfOne\r\nTwo\r\n", {"1": "Deux"})
    assert out == b"\xef\xbb\xbfOne\r\nDeux\r\n", out
    print("  [translate] tokens rematerialised, escapes, prefixes, line endings OK")


def test_attrs():
    src = (
        b'<Note title="Before you begin" type="info" label={x}>\n'
        b"Body text.\n"
        b"</Note>\n"
        b'<Image alt="A cat" src="/cat.png" caption=\'Our cat\' />\n'
        b'The <Abbr title="Application interface">API</Abbr> is stable.\n'
        b'<Thing description="Not on the list" />\n'
    )
    pr = F.parse(src)
    texts = [u.text for u in pr.units]
    styles = [u.style for u in pr.units]
    assert texts == [
        "Body text.",
        "The ⟦g1⟧API⟦/g1⟧ is stable.",
        "Before you begin",
        "A cat",
        "Our cat",
        "Application interface",
    ], texts
    assert styles[:2] == ["", ""] and set(styles[2:]) == {"a11y:mdx-attr"}
    from app.filters.a11y import unit_note

    assert unit_note("a11y:mdx-attr")
    out = F.build(src, {"2": 'Avant "tout"', "3": "Un chat", "5": "Interface d'application"})
    assert b'title="Avant &quot;tout&quot;"' in out, out
    assert b'alt="Un chat"' in out and b"caption='Our cat'" in out
    assert b'<Abbr title="Interface d\'application">API</Abbr>' in out, out
    # attribute inside a TRANSLATED line
    out = F.build(src, {"1": "L'⟦g1⟧API⟦/g1⟧ est stable.", "5": "Interface"})
    assert b'L\'<Abbr title="Interface">API</Abbr> est stable.\n' in out, out
    # the translation dropped the token holding a translated attribute: no overlap damage
    out = F.build(src, {"1": "Plus de balise.", "5": "Interface"})
    assert b"\nPlus de balise.\n<Thing" in out and b"Interface" not in out, out
    # a duplicated token carries the translated attribute both times
    out = F.build(src, {"1": "⟦g1⟧API⟦/g1⟧ ⟦g1⟧API⟦/g1⟧", "5": "I"})
    assert out.count(b'<Abbr title="I">API</Abbr>') == 2, out
    # single-quoted value with a quote in the translation
    out = F.build(src, {"4": "Notre 'chat'"})
    assert b"caption='Notre &#39;chat&#39;'" in out, out
    # allowlist from the environment
    os.environ["LOC_TMS_MDX_ATTRS"] = "description"
    try:
        assert _texts(src) == ["Body text.", "The ⟦g1⟧API⟦/g1⟧ is stable.", "Not on the list"]
    finally:
        del os.environ["LOC_TMS_MDX_ATTRS"]
    print("  [attrs] allowlisted string attributes are appended units OK")


def test_markdown_parity():
    """Plain Markdown (no JSX, no braces) gives the same units through both filters."""
    src = (
        b"# Title\n\nSome *prose* here.\n\n- item one\n- item two\n\n---\n\n"
        b"| a | b |\n|---|---|\n| c | d |\n\n```\ncode\n```\n\n> quoted line\n"
        b"1. first\n![alt](x.png)\n"
    )
    assert [u.text for u in MD.parse(src).units] == _texts(src)
    tr = {str(u.p_index): "X " + u.text for u in MD.parse(src).units}
    assert MD.build(src, tr) == F.build(src, tr)  # #677: md keeps the final newline too
    print("  [markdown] plain Markdown parses identically to the md filter OK")


def test_cjk_emphasis():
    """#695: CommonMark ignores `_x_` / `__x__` between letters and `**` whose inner text
    starts or ends with punctuation next to a letter; a Japanese/Chinese translation hits
    both (live ja-JP run: 5 of 6 emphasised lines printed literal marks). Both builds now
    switch `_` to `*` and move edge punctuation outside the marks, only for a pair the
    source had and only when CommonMark would not read it as written."""
    src = (
        b'Choose __Advanced__ options.\n\nClick **"Save"** now.\n\nThe _quick_ fix.\n\n'
        b"Press **(Ctrl+S)** to save.\n\nUse my_var and *it* here.\n"
    )
    tr = {
        "0": "__詳細__オプションを選択。",
        "1": "**「保存」**をクリック。",
        "2": "_素早い_解決策。",
        "3": "**（Ctrl+S）**を押す。",
        "4": "my_varと*斜体*を使う。",
    }
    want = (
        "**詳細**オプションを選択。\n\n"
        "「**保存**」をクリック。\n\n"
        "*素早い*解決策。\n\n"
        "（**Ctrl+S**）を押す。\n\n"
        "my_varと*斜体*を使う。\n"
    )
    assert MD.build(src, tr).decode() == want, MD.build(src, tr).decode()
    assert F.build(src, tr).decode() == want, F.build(src, tr).decode()
    from app.filters.md_emphasis import fix_cjk_emphasis

    # Left alone: a source without emphasis, spaced pairs, code spans, link targets, an
    # identifier underscore, and an untouched (source) line.
    assert fix_cjk_emphasis("Plain.", "平文_です_") == "平文_です_"
    assert fix_cjk_emphasis("a _b_ c", "あ _b_ い") == "あ _b_ い"
    kept = "`a_b_`と[_文_](http://x/a_b_c)。"
    assert fix_cjk_emphasis("`a_b_` and [_d_](http://x/a_b_c).", kept) == kept
    cjk_src = "日本__語__です\n".encode()
    assert MD.build(cjk_src, {"0": cjk_src.decode().strip()}) == cjk_src
    print("  [cjk] emphasis glued to CJK text rewritten so CommonMark reads it (#695) OK")


def test_cjk_autolinks():
    """#700: GFM links a bare URL only after whitespace or * _ ~ ( and runs it over every
    following non-space character; a Chinese/Japanese translation put CJK punctuation next
    to it (live: "project，并镜像在" joined the link, "、https://..." was not linked). The
    builds write an explicit link there: <url> in Markdown, [url](url) in MDX."""
    src = (
        b"Hosted at https://github.com/example/project and mirrored on https://x.org/p.\n\n"
        b"Read [the manual](https://example.org/manual) and `https://a.b` now.\n"
    )
    tr = {
        "0": "托管在 https://github.com/example/project，镜像在、https://x.org/p。",
        "1": "阅读[手册](https://example.org/manual)和`https://a.b`。",
    }
    md = MD.build(src, tr).decode()
    assert md == (
        "托管在 <https://github.com/example/project>，镜像在"
        "、<https://x.org/p>。\n\n"
        "阅读[手册](https://example.org/manual)和`https://a.b`。\n"
    ), md
    mdx = F.build(src, tr).decode()
    assert "在 [https://github.com/example/project](https://github.com/example/project)，" in mdx
    assert "、[https://x.org/p](https://x.org/p)。" in mdx, mdx
    # spaces around the URL, or only trailing ASCII punctuation: unchanged
    fr = {"0": "Hébergé sur https://github.com/example/project, miroir https://x.org/p."}
    assert MD.build(src, fr).decode().startswith(fr["0"] + "\n"), MD.build(src, fr)
    # gate: <www.x> is not an autolink (no scheme): a www. URL becomes [www.x](http://www.x)
    wsrc = b"See www.example.org now.\n"
    wtr = {"0": "参见www.example.org。"}
    want = "参见[www.example.org](http://www.example.org)。\n"
    assert MD.build(wsrc, wtr).decode() == want, MD.build(wsrc, wtr)
    assert F.build(wsrc, wtr).decode() == want, F.build(wsrc, wtr)
    # a URL used as inline link text stays as written (never a link inside a link)
    lsrc = b"Open [https://x.org](https://x.org) now.\n"
    ltr = {"0": "打开[https://x.org](https://x.org)。"}
    assert MD.build(lsrc, ltr).decode() == ltr["0"] + "\n", MD.build(lsrc, ltr)
    print("  [cjk] bare URLs glued to CJK punctuation written as explicit links (#700) OK")


def test_block_start():
    """#704: the engine drops the backslash of an escaped line start (live de-DE/fr-FR:
    "1984\\. That was" -> "1984. Das war", which CommonMark renders as <ol start="1984">).
    A translated line never opens a block (ordered list, ATX heading, bullet, quote) its
    source line did not: the builds escape the marker. Heading text is left alone."""
    src = (
        b"1984\\. That was the year.\n\n\\# Not a heading\n\n- Item one\n\n"
        b"## Heading two\n\nPlain line.\n\n> Quoted line.\n\nKeep 1984\\. here.\n"
    )
    tr = {
        "0": "1984. Das war das Jahr.",
        "1": "# Keine Überschrift",
        "2": "1. Punkt eins",
        "3": "1. Überschrift zwei",
        "4": "- Einfache Zeile.",
        "5": "> Zitierte Zeile.",
        "6": "Behalte 1984\\. hier.",
    }
    want = (
        "1984\\. Das war das Jahr.\n\n\\# Keine Überschrift\n\n- 1\\. Punkt eins\n\n"
        "## 1. Überschrift zwei\n\n\\- Einfache Zeile.\n\n> \\> Zitierte Zeile.\n\n"
        "Behalte 1984\\. hier.\n"
    )
    for name, flt in (("md", MD), ("mdx", F)):
        texts = [u.text for u in flt.parse(src).units]
        assert len(texts) == 7, (name, texts)
        got = flt.build(src, tr).decode()
        assert got == want, (name, got)
    # an escape the engine kept, a number with no delimiter, a source that already starts
    # with a marker-looking run: unchanged
    keep = {
        "0": "1984\\. Das war das Jahr.",
        "1": "2024 war ein gutes Jahr",
        "4": "-5 Grad draußen.",
    }
    got = MD.build(src, keep).decode()
    assert got.startswith("1984\\. Das war das Jahr.\n\n2024 war ein gutes Jahr\n"), got
    assert "\n-5 Grad draußen.\n" in got, got
    # gate: only ASCII digits open an ordered list in CommonMark, so an Arabic-Indic or
    # full-width year at the start of a translated line is left as written
    for year in ("١٩٨٤. كانت تلك السنة.", "１９８４。それは年でした。", "１９８４. x"):
        got = MD.build(src, {"0": year}).decode()
        assert got.startswith(year + "\n"), (year, got)
    print("  [block] a translated line opens no list/heading/quote its source did not (#704) OK")


def test_table_pipe_escape():
    """#710: the engine dropped the backslash of an escaped pipe in a table cell (live fr-FR:
    `| Yes \\| No | Answer ... |` -> `| Oui | Non | Répondez ... |`); the row gained a cell and
    GFM hid the last one, so the translated text disappeared. Both builds put it back."""
    src = (
        b"| Operator | Meaning |\n|---|---|\n"
        b"| Yes \\| No | Answer with yes or no only |\n"
        b"| a \\|\\| b | Run b only when a fails |\n"
        b"| Plain | A cell with no pipe |\n"
    )
    tr = {
        "0": "| Opérateur | Signification |",
        "1": "| Oui | Non | Répondez par oui ou par non uniquement |",
        "2": "| a || b | Exécuter b uniquement si a échoue |",
        "3": "| Simple | Une cellule sans tuyau |",
    }
    want = (
        "| Opérateur | Signification |\n|---|---|\n"
        "| Oui \\| Non | Répondez par oui ou par non uniquement |\n"
        "| a \\|\\| b | Exécuter b uniquement si a échoue |\n"
        "| Simple | Une cellule sans tuyau |\n"
    )
    for name, flt in (("md", MD), ("mdx", F)):
        assert len(flt.parse(src).units) == 4, name
        got = flt.build(src, tr).decode()
        assert got == want, (name, got)
    # live fr-FR on the fix branch: the engine kept the backslash and dropped the pipe
    lone = {"1": "| Oui \\ Non | Répondez par oui ou non |", "2": "| a \\ \\ b | Exécuter b |"}
    for name, flt in (("md", MD), ("mdx", F)):
        got = flt.build(src, lone).decode()
        assert "| Oui \\| Non | Répondez par oui ou non |\n" in got, (name, got)
        assert "| a \\| \\| b | Exécuter b |\n" in got, (name, got)
    # a lone backslash the source also has is not touched
    bsrc = b"| A | B |\n|---|---|\n| C:\\ dir \\| x | y |\n"
    got = MD.build(bsrc, {"1": "| C:\\ dossier \\| x | y |"}).decode()
    assert got.endswith("| C:\\ dossier \\| x | y |\n"), got
    # kept escapes, an engine that moved the escaped pipe, the same cell count: unchanged
    keep = {
        "1": "| Oui \\| Non | Répondez par oui ou par non |",
        "2": "| Répondez | a \\|\\| b |",
    }
    got = MD.build(src, keep).decode()
    assert "| Oui \\| Non | Répondez par oui ou par non |\n" in got, got
    assert "| Répondez | a \\|\\| b |\n" in got, got
    # gate: a source with no escaped pipe is never touched, even when the row gains a cell
    plain = b"| A | B |\n|---|---|\n| x | y |\n"
    got = MD.build(plain, {"1": "| x | y | z |"}).decode()
    assert got.endswith("| x | y | z |\n"), got
    print("  [table] a lost `\\|` escape in a table cell is restored, no extra cell (#710) OK")


def test_line_breaks_714():
    """#714: a line break in the translation of a one-line unit cut a heading in two, split a
    table row, or opened a list on the next line. Both builds write it as a space."""
    src = (
        b"# Getting started\n\n| A | B |\n|---|---|\n| Yes | Answer here |\n\n"
        b"- Item one\n\nPlain para.\n"
    )
    tr = {
        "0": "Erste\nSchritte",
        "1": "| A | B |",
        "2": "| Ja | Antwort \r\n hier |",
        "3": "Punkt\n- eins",
        "4": "Absatz\nzwei.\n",
    }
    want = (
        "# Erste Schritte\n\n| A | B |\n|---|---|\n| Ja | Antwort hier |\n\n"
        "- Punkt - eins\n\nAbsatz zwei.\n"
    )
    for name, flt in (("md", MD), ("mdx", F)):
        got = flt.build(src, tr).decode()
        assert got == want, (name, got)
        assert len(flt.parse(got.encode()).units) == 5, name
    # untouched units and a no-op build stay byte-identical
    assert MD.build(src, {}) == src and F.build(src, {}) == src
    print("  [lines] a line break in a one-line unit is written as a space (#714) OK")


def test_a11y():
    src = (
        b'Intro.\n\n![](a/empty.png)\n\n<img src="/x/logo.png" />\n'
        b'<Image alt="" src="y.png"/>\n<img alt={t} src="z"/>\n'
    )
    sk = F.parse(src).skeleton
    imgs = sk["a11y_images"]
    assert imgs["images"] == 4, imgs
    kinds = sorted((m["kind"], m["where"]) for m in imgs["missing_alt"])
    assert kinds == [("empty", "empty.png"), ("empty", "y.png"), ("missing", "logo.png")], kinds
    print("  [a11y] empty and missing alt reported OK")


def test_hostile():
    from app import config

    # unclosed brace / tag: read as text, round trip intact
    src = b"Open { never closed.\nA <Tag never closed\n"
    assert _texts(src) == ["Open { never closed.", "A <Tag never closed"]
    assert F.build(src, {}) == src
    # a flood of unclosed braces is refused, not rescanned forever
    flood = b"x {\n" * 400_000
    try:
        F.parse(flood)
        raise AssertionError("brace flood was not refused")
    except InputLimitError:
        pass
    # segment cap
    old = config.MAX_DOCUMENT_SEGMENTS
    config.MAX_DOCUMENT_SEGMENTS = 5
    try:
        F.parse(b"".join(b"Line %d.\n\n" % i for i in range(10)))
        raise AssertionError("segment cap not enforced")
    except InputLimitError:
        pass
    finally:
        config.MAX_DOCUMENT_SEGMENTS = old
    # one huge line
    try:
        F.parse(b"a" * (config.MAX_SEGMENT_CHARS + 10))
        raise AssertionError("char cap not enforced")
    except InputLimitError:
        pass
    # binary refused; Windows-1252 converted
    try:
        F.parse(b"\x00\x01\x02\xff" * 50)
        raise AssertionError("binary accepted")
    except UnsupportedEncodingError:
        pass
    pr = F.parse("Café <B>crème</B>.\n".encode("cp1252"))
    assert pr.skeleton.get("source_encoding") == "cp1252"
    assert pr.units[0].text == "Café ⟦g1⟧crème⟦/g1⟧."
    print("  [hostile] unclosed markup, floods, caps, encodings OK")


def test_surfaces():
    assert "mdx" in supported_exts() and type(get_filter("mdx")).__name__ == "MdxFilter"
    assert get_filter("md") is MD and type(MD).__name__ == "MarkdownFilter"

    def read(p):
        with open(os.path.join(_ROOT, p), encoding="utf-8") as fh:
            return fh.read()

    assert ".markdown,.mdx," in read("app/templates/project.html")
    from app.services.connectors import msgraph
    from app.services.mime import mime_for_fmt

    assert msgraph.FORMATS["mdx"] == "mdx"
    assert mime_for_fmt("mdx").startswith("text/markdown")
    assert "MDX" in read("app/web/api_part2.py")
    assert "MDX (.mdx" in read("app/capabilities/documents.md")
    assert "MDX (.mdx)" in read("llms.txt")
    assert "**MDX documents**" in read("docs/CLI.md")
    assert "mdx_filter.py::MdxFilter" in read("docs/architecture/translation-pipeline.md")
    print("  [surfaces] registry, upload, msgraph, MIME, labels, docs OK")


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
        p = Project(name="mdx", org_id=1, source_locale="en-US", default_provider="mock")
        s.add(p)
        s.commit()
        s.refresh(p)
        s.add(TargetLocale(project_id=p.id, locale="de-DE"))
        s.commit()
        pid = p.id
    user = security.create_user("mdx@x.test", "mdx", Role.ADMIN, _TEST_PW)
    _row, raw = apikeys.create_key(user.id, "mdx", scopes="api docs admin")
    client = TestClient(app, raise_server_exceptions=False, follow_redirects=False)
    hdr = {"X-Loc-Token": raw}  # pregate: allow-secret throwaway test key
    src = _fixture("guide.mdx")
    r = client.post(
        "/api/documents",
        headers=hdr,
        data={"project_id": str(pid)},
        files={"file": ("guide.mdx", src, "text/markdown")},
    )
    assert r.status_code in (200, 201), (r.status_code, r.text)
    doc_id = r.json()["document_id"]
    code, out, err = run_cli(["doc", "translate", str(doc_id), "--locale", "de-DE"])
    assert code == 0, (code, out, err)
    r = client.get(f"/api/documents/{doc_id}/download", headers=hdr, params={"locale": "de-DE"})
    assert r.status_code == 200, (r.status_code, r.text)
    body = r.content.decode("utf-8")
    for kept in (
        "import Note from './note.mdx'",
        '<Note type="info"',
        "{/* A comment",
        "<Kbd>Ctrl</Kbd>",
        "{props.count}",
        "const x = <Hidden>Not translated</Hidden>",
    ):
        assert kept in body, (kept, body)
    assert "[de-DE]" in body and 'title="[de-DE]' in body, body
    print("  [e2e] .mdx: API upload, CLI translate, API export OK")


def main():
    test_identity()
    test_walk()
    test_tokens()
    test_translate()
    test_attrs()
    test_markdown_parity()
    test_cjk_emphasis()
    test_cjk_autolinks()
    test_block_start()
    test_table_pipe_escape()
    test_line_breaks_714()
    test_a11y()
    test_hostile()
    test_surfaces()
    test_end_to_end()
    print("MDX FILTER OK")


if __name__ == "__main__":
    main()
