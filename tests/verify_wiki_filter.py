"""Wiki markup filters: MediaWiki (.wiki, .mediawiki, .wikitext) and DokuWiki (.dokuwiki)
(format Batch 15, fmt/wiki).

  registry    wiki + mediawiki + wikitext + dokuwiki resolve; .txt stays plain text
  identity    an untouched build is byte-identical (LF, CRLF, BOM, no final newline)
  units       headings, paragraphs, list items, dl term/definition, table cells, link
              labels translated; comments, nowiki, pre, code, syntaxhighlight, templates,
              magic words, signatures, categories, redirects, preformatted lines skipped
  tokens      link targets, templates, tags, URLs as point tokens; link labels, file and
              media captions, footnotes and bold/italic marks as paired tokens
  translated  translation written in place; table separators escaped; tokens restored
  templates   parameters translated only from the WIKI_TEMPLATE_PARAMS allowlist
  encodings   cp1252 converted, binary refused (#509)
  caps        segment cap and segment length cap (#571); hostile inputs stay fast
  surfaces    API upload -> CLI translate -> API download; /api/formats lists wiki

Hermetic + offline. Fixtures are hand-written below. Final line: "WIKI FILTER OK".
"""

import os
import sys
import time

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(_ROOT)
sys.path.insert(0, _ROOT)
from tests._bootstrap import init_env

init_env("loctms_wiki_")

from app.filters import get_filter, supported_exts
from app.filters.base import InputLimitError, UnsupportedEncodingError

LOC = "fr-FR"

MW = b"""{{Infobox city
| name = Paris
| caption = The Eiffel Tower at night
| population = {{formatnum:2100000}}
}}
'''Paris''' is the [[capital city|capital]] of [[France]].<ref name="a">Source text here.</ref>
It has many ''museums'' and [https://example.com an official site].
<!-- editor comment that must stay -->

== History ==
=== Early days <!-- note --> ===
* First item with {{lang|fr|Bonjour}} inside
** Nested [[File:Tower.jpg|thumb|200px|A tall tower]] item
# Numbered item
; Term : The definition
: Indented reply

{| class="wikitable"
|+ Table caption
|-
! Header one !! Header two
|-
| style="color:red" | Red cell || Plain cell
| [[Link|label]] cell
|}

 preformatted code line
<syntaxhighlight lang="python">
print("hi")  # code stays
</syntaxhighlight>
<pre>
Pre block stays.
</pre>
Use <code>foo()</code> and <nowiki>[[not a link]]</nowiki> here.

{{{1|Default param}}} is a parameter.
__NOTOC__
Signed ~~~~
----
[[Category:Cities]]
[[fr:Paris]]
"""

DW = b"""====== Main title ======
This is **bold** and //italic// text with [[ns:page|a link]] and [[other]].
Second line ((a footnote)) with {{img.png?200|An image caption}}.

  * List item one
  - Ordered item

^ Head A ^ Head B ^
| Cell [[x|y]] one | Cell two |
| ::: | more |

  indented code stays
<code python>
print("x")
</code>
<file php example.php>
<?php echo 1; ?>
</file>
%%**not bold**%% stays, ''mono'' too, see https://example.com/a//b and <a@b.example>.
> quoted line
~~NOTOC~~
----
"""

MW_KEPT = [
    "{{Infobox city\n| name = Paris\n| caption = The Eiffel Tower at night\n"
    "| population = {{formatnum:2100000}}\n}}",
    "<!-- editor comment that must stay -->",
    " preformatted code line",
    'print("hi")  # code stays',
    "<pre>\nPre block stays.\n</pre>",
    "__NOTOC__",
    "[[Category:Cities]]",
    "[[fr:Paris]]",
    '{| class="wikitable"',
]


def _wf():
    return get_filter("wiki")


def _df():
    return get_filter("dokuwiki")


def _texts(f, data):
    return [u.text for u in f.parse(data).units]


def test_registry():
    exts = supported_exts()
    for e in ("wiki", "mediawiki", "wikitext", "dokuwiki"):
        assert e in exts, e
    assert get_filter("mediawiki") is _wf() and get_filter("wikitext") is _wf()
    assert get_filter("dokuwiki").fmt == "dokuwiki"
    assert get_filter("txt").fmt == "txt"  # .txt is never guessed to be DokuWiki
    print("  [registry] wiki, mediawiki, wikitext, dokuwiki registered; txt unchanged OK")


def test_identity():
    for f, data in ((_wf(), MW), (_df(), DW)):
        for variant in (
            data,
            data.replace(b"\n", b"\r\n"),
            b"\xef\xbb\xbf" + data,
            data.rstrip(b"\n"),
        ):
            assert f.build(variant, {}) == variant
            same = {str(u.p_index): u.text for u in f.parse(variant).units}
            assert f.build(variant, same) == variant
    print("  [identity] untouched and echo builds byte-identical (LF, CRLF, BOM) OK")


def test_units_mediawiki():
    t = _texts(_wf(), MW)
    assert t == [
        "⟦g1⟧Paris⟦/g1⟧ is the ⟦g2⟧capital⟦/g2⟧ of ⟦x3⟧.⟦x4⟧Source text here.⟦x5⟧ "
        "It has many ⟦g6⟧museums⟦/g6⟧ and ⟦g7⟧an official site⟦/g7⟧.",
        "History",
        "Early days ⟦x1⟧",
        "First item with ⟦x1⟧ inside",
        "Nested ⟦g1⟧A tall tower⟦/g1⟧ item",
        "Numbered item",
        "Term",
        "The definition",
        "Indented reply",
        "Table caption",
        "Header one",
        "Header two",
        "Red cell",
        "Plain cell",
        "⟦g1⟧label⟦/g1⟧ cell",
        "Use ⟦x1⟧ and ⟦x2⟧ here.",
        "⟦x1⟧ is a parameter.",
        "Signed ⟦x1⟧",
    ], t
    u = _wf().parse(MW).units
    assert u[0].inline_map == {
        "1": ["'''", "'''"],
        "2": ["[[capital city|", "]]"],
        "3": "[[France]]",
        "4": '<ref name="a">',
        "5": "</ref>",
        "6": ["''", "''"],
        "7": ["[https://example.com ", "]"],
    }, u[0].inline_map
    assert u[3].inline_map == {"1": "{{lang|fr|Bonjour}}"}
    assert u[4].inline_map["1"] == ["[[File:Tower.jpg|thumb|200px|", "]]"]
    assert u[15].inline_map == {"1": "<code>foo()</code>", "2": "<nowiki>[[not a link]]</nowiki>"}
    assert u[16].inline_map == {"1": "{{{1|Default param}}}"}
    assert u[17].inline_map == {"1": "~~~~"}
    # redirects, file links without captions, options-only captions, pipe trick
    assert _texts(_wf(), b"#REDIRECT [[Target page]]\n") == []
    assert _texts(_wf(), b"A [[File:x.png|thumb|left|200px]] b\n") == ["A ⟦x1⟧ b"]
    assert _texts(_wf(), b"A [[Help:Contents|]] b\n") == ["A ⟦x1⟧ b"]
    assert _texts(_wf(), b"See [[:Category:Cities|city list]].\n") == ["See ⟦g1⟧city list⟦/g1⟧."]
    # nested link inside a file caption
    nested = _wf().parse(b"[[File:a.png|thumb|A [[b|nested]] caption]]\n").units[0]
    assert nested.text == "⟦g1⟧A ⟦g2⟧nested⟦/g2⟧ caption⟦/g1⟧", nested.text
    # resource-style placeholders in prose stay text; parser functions stay whole
    assert _texts(_wf(), b"Use %s or {0} with {{#if: x | yes | no }} now.\n") == [
        "Use %s or {0} with ⟦x1⟧ now."
    ]
    # an unclosed comment protects the rest; an unclosed <nowiki> only itself
    assert _texts(_wf(), b"Keep this.\n<!-- open\nhidden text\n") == ["Keep this."]
    assert _texts(_wf(), b"A <nowiki> b\n") == ["A ⟦x1⟧ b"]
    print("  [units] MediaWiki structure, skips and inline tokens OK")


def test_units_dokuwiki():
    f = _df()
    t = _texts(f, DW)
    assert t == [
        "Main title",
        "This is ⟦g1⟧bold⟦/g1⟧ and ⟦g2⟧italic⟦/g2⟧ text with ⟦g3⟧a link⟦/g3⟧ and ⟦x4⟧. "
        "Second line ⟦g5⟧a footnote⟦/g5⟧ with ⟦g6⟧An image caption⟦/g6⟧.",
        "List item one",
        "Ordered item",
        "Head A",
        "Head B",
        "Cell ⟦g1⟧y⟦/g1⟧ one",
        "Cell two",
        "more",
        "⟦x1⟧ stays, ⟦x2⟧ too, see ⟦x3⟧ and ⟦x4⟧.",
        "quoted line",
    ], t
    u = f.parse(DW).units
    assert u[1].inline_map["3"] == ["[[ns:page|", "]]"]
    assert u[1].inline_map["6"] == ["{{img.png?200|", "}}"]
    assert u[9].inline_map["3"] == "https://example.com/a//b"
    assert _texts(f, b"{{rss>http://x.example/feed 5|Feed}} text\n") == ["⟦x1⟧ text"]
    assert _texts(f, b"Line one\\\\ line two\n") == ["Line one⟦x1⟧ line two"]
    assert _texts(f, b"<code>\nnever closed\n") == ["never closed"]
    assert _texts(f, b"See <code> here\n") == ["See ⟦x1⟧ here"]
    print("  [units] DokuWiki structure, skips and inline tokens OK")


def test_dokuwiki_eol_linebreak_678():
    """#678: `\\` at the END of a line inside a multi-line paragraph is a DokuWiki forced
    line break; it must be a token, and a translated export must keep it working (not
    `<nowiki>\\</nowiki>`, which renders two literal backslashes)."""
    f = _df()
    for nl in (b"\n", b"\r\n"):
        src = b"First line\\\\" + nl + b"second line\\\\ and more" + nl + b"last line\\\\" + nl
        units = f.parse(src).units
        assert [u.text for u in units] == [
            "First line⟦x1⟧ second line⟦x2⟧ and more last line⟦x3⟧"
        ], [u.text for u in units]
        assert units[0].inline_map["1"] == "\\\\", units[0].inline_map
        assert f.build(src, {}) == src
        out = f.build(src, {"0": "Erste Zeile⟦x1⟧ zweite⟦x2⟧ und mehr letzte⟦x3⟧"})
        assert b"nowiki" not in out, out
        assert out.startswith(b"Erste Zeile\\\\ zweite\\\\ und mehr letzte\\\\"), out
        assert out.endswith(nl), out
    # `\\` glued to a word stays text, and a translator's own `\\` is still written inert
    assert _texts(f, b"a \\\\b\nnext\n") == ["a \\\\b next"]
    out = f.build(b"one\ntwo\n", {"0": "eins\\\\ zwei"})
    assert b"<nowiki>\\\\</nowiki>" in out, out
    print("  [units] DokuWiki end-of-line forced line break (#678) OK")


def test_dokuwiki_break_glued_701():
    """#701: a DokuWiki forced line break needs whitespace or the line end after it. The
    space after it is text in the unit, and a Japanese translation dropped it
    ("...です。\\\\祝日は..."), which DokuWiki prints as two backslashes. The build adds the
    space back; a break already followed by a space or ending the unit is unchanged."""
    f = _df()
    src = b"Open Monday to Friday.\\\\ Closed on holidays.\n"
    unit = f.parse(src).units[0].text
    assert unit == "Open Monday to Friday.⟦x1⟧ Closed on holidays.", unit
    ja = "月曜日から。⟦x1⟧祝日は休業。"
    out = f.build(src, {"0": ja})
    assert out == ("月曜日から。\\\\ 祝日は休業。\n").encode(), out
    assert len(f.parse(out).units) == 1 and "⟦x1⟧" in f.parse(out).units[0].text
    fr = "Ouvert du lundi au vendredi.⟦x1⟧ Fermé les jours fériés."
    assert (
        f.build(src, {"0": fr})
        == ("Ouvert du lundi au vendredi.\\\\ Fermé les jours fériés.\n").encode()
    )
    assert f.build(src, {"0": "Fin⟦x1⟧"}) == b"Fin\\\\\n"
    print("  [cjk] DokuWiki line break glued to the next word gets its space (#701) OK")


def test_translated():
    f = _wf()
    units = f.parse(MW).units
    out = f.build(MW, {str(u.p_index): "FR " + u.text for u in units}).decode()
    assert (
        "FR '''Paris''' is the [[capital city|capital]] of [[France]]."
        '<ref name="a">Source text here.</ref> It has many' in out
    ), out
    assert "== FR History ==\n=== FR Early days <!-- note --> ===" in out
    assert "* FR First item with {{lang|fr|Bonjour}} inside" in out
    assert "; FR Term : FR The definition" in out
    assert "! FR Header one !! FR Header two" in out
    assert '| style="color:red" | FR Red cell || FR Plain cell' in out
    for kept in MW_KEPT:
        assert kept in out, kept
    assert "⟦" not in out
    # translated words reorder around the tokens; a cell's pipe is escaped
    p0 = units[0]
    out2 = f.build(
        MW,
        {
            "0": "⟦g2⟧La capitale⟦/g2⟧ de ⟦x3⟧ est ⟦g1⟧Paris⟦/g1⟧.\nFin",
            "12": "Rouge | vif",
        },
    ).decode()
    assert "[[capital city|La capitale]] de [[France]] est '''Paris'''. Fin\n<!--" in out2, out2
    assert '| style="color:red" | Rouge {{!}} vif || Plain cell' in out2
    assert p0.text.startswith("⟦g1⟧")
    out3 = f.build(MW, {"1": "x ⟦x9⟧ y"}).decode()  # unknown token dropped
    assert "== x  y ==" in out3 and "⟦" not in out3
    d = _df()
    dunits = d.parse(DW).units
    dout = d.build(DW, {str(u.p_index): "FR " + u.text for u in dunits}).decode()
    assert "====== FR Main title ======" in dout
    assert "  * FR List item one\n  - FR Ordered item" in dout
    assert (
        "^ FR Head A ^ FR Head B ^\n| FR Cell [[x|y]] one | FR Cell two |\n| ::: | FR more |"
        in dout
    )
    assert '  indented code stays\n<code python>\nprint("x")\n</code>' in dout
    assert "<?php echo 1; ?>" in dout and "~~NOTOC~~" in dout
    dout2 = d.build(DW, {"7": "A | B ^ C"}).decode()
    assert "| A %%|%% B %%^%% C |" in dout2, dout2
    crlf = MW.replace(b"\n", b"\r\n")
    outc = f.build(crlf, {str(u.p_index): "FR " + u.text for u in f.parse(crlf).units})
    assert b"\r\n" in outc and b"\n" not in outc.replace(b"\r\n", b"")
    print("  [translated] in-place rebuild, cell escaping, token restore, CRLF OK")


def test_templates():
    from app import config

    old = config.WIKI_TEMPLATE_PARAMS
    try:
        config.WIKI_TEMPLATE_PARAMS = "Infobox_city:caption, Template:quote:text, Note:1"
        f = _wf()
        t = _texts(f, MW)
        assert t[0] == "The Eiffel Tower at night", t[:2]
        src = (
            b"Before {{note|Remember this}} after.\n\n"
            b"{{Quote|author=Someone|text=To be or not}}\n\n"
            b"{{Note|{{lang|fr|Bonjour}} friend}}\n"
            b"{{#if:{{{x|}}}|Nope}} {{Other|text=Not listed}} plain.\n"
        )
        units = f.parse(src).units
        assert [u.text for u in units] == [
            "Before ⟦x1⟧Remember this⟦x2⟧ after.",
            "To be or not",
            "⟦x1⟧ friend",
            "⟦x1⟧ ⟦x2⟧ plain.",
        ], [u.text for u in units]
        out = f.build(
            src,
            {
                "0": "Avant ⟦x1⟧Souviens-toi⟦x2⟧ apres.",
                "1": "Etre ou ne pas etre",
                "2": "⟦x1⟧ ami",
            },
        ).decode()
        assert out.startswith(
            "Avant {{note|Souviens-toi}} apres.\n\n"
            "{{Quote|author=Someone|text=Etre ou ne pas etre}}\n\n"
            "{{Note|{{lang|fr|Bonjour}} ami}}\n"
        ), out
        assert f.build(src, {}) == src
        mw_out = f.build(MW, {"0": "La tour Eiffel la nuit"}).decode()
        assert "| caption = La tour Eiffel la nuit\n" in mw_out
    finally:
        config.WIKI_TEMPLATE_PARAMS = old
    assert _texts(_wf(), b"{{Quote|text=To be}}\n") == []  # nothing listed: protected
    print("  [templates] parameters only from WIKI_TEMPLATE_PARAMS, block and inline OK")


def test_gate_inert_markup():
    """Gate fix: wiki syntax that appears only in the TRANSLATED words (MT or a translator)
    is written inside <nowiki>, so it cannot add templates, categories, links, raw HTML,
    lists or a redirect; a block template parameter escapes | and =."""
    from app import config

    f, d = _wf(), _df()
    src = b"== Intro ==\nThis is a '''bold''' [[Page|link]] text.\n\n* item one\n"
    out = f.build(
        src,
        {
            "1": "* Da ⟦g1⟧fett⟦/g1⟧ {{Delete}} [[Category:Spam]] <span>x</span> ''y'' ~~~~",
            "2": "#REDIRECT [[Evil]]",
        },
    ).decode()
    assert out == (
        "== Intro ==\n<nowiki>*</nowiki> Da '''fett''' "
        "<nowiki>{{</nowiki>Delete<nowiki>}}</nowiki> "
        "<nowiki>[[</nowiki>Category:Spam<nowiki>]]</nowiki> <nowiki><</nowiki>span>x"
        "<nowiki><</nowiki>/span> <nowiki>''</nowiki>y<nowiki>''</nowiki> <nowiki>~~~~</nowiki>\n\n"
        "* <nowiki>#</nowiki>REDIRECT <nowiki>[[</nowiki>Evil<nowiki>]]</nowiki>\n"
    ), out
    dsrc = b"====== Head ======\nSome **bold** text.\n"
    dout = d.build(dsrc, {"1": "> **x** {{a.png}} <html>h</html> ~~NOTOC~~ https://x.y"}).decode()
    assert dout == (
        "====== Head ======\n<nowiki>></nowiki> <nowiki>**</nowiki>x<nowiki>**</nowiki> "
        "<nowiki>{{</nowiki>a.png<nowiki>}}</nowiki> <nowiki><</nowiki>html>h"
        "<nowiki><</nowiki>/html> <nowiki>~~</nowiki>NOTOC<nowiki>~~</nowiki> https://x.y\n"
    ), dout
    old = config.WIKI_TEMPLATE_PARAMS
    try:
        config.WIKI_TEMPLATE_PARAMS = "Quote:text"
        tsrc = b"{{Quote|author=A|text=To be}}\n"
        tout = f.build(tsrc, {"0": "x=y|z"}).decode()
        assert tout == "{{Quote|author=A|text=x{{=}}y{{!}}z}}\n", tout
    finally:
        config.WIKI_TEMPLATE_PARAMS = old
    print("  [gate] translated words cannot create live wiki markup OK")


def test_cjk_bare_url_699():
    """#699: MediaWiki keeps adding characters to a bare URL until a space or [ ] < > ";
    a Chinese/Japanese translation writes the URL token straight before the next words,
    which became part of the link (live side run: 3 of 3 paragraphs). The build ends the
    URL with <nowiki/>; a URL followed by a space, a bracket or final ASCII punctuation is
    unchanged, and DokuWiki (its URLs stop at a non-ASCII letter) is untouched."""
    f, d = _wf(), _df()
    src = b"Read https://example.org/manual first and see https://x.org.\n"
    unit = f.parse(src).units[0].text
    assert unit == "Read ⟦x1⟧ first and see ⟦x2⟧", unit
    zh = "请阅读⟦x1⟧手册，参见⟦x2⟧。"
    out = f.build(src, {"0": zh}).decode()
    assert out == (
        "请阅读<nowiki/>https://example.org/manual<nowiki/>手册，"
        "参见<nowiki/>https://x.org.<nowiki/>。\n"
    ), out
    # after CJK punctuation no boundary marker is needed (gate: any word character needs one)
    out = f.build(src, {"0": "「⟦x1⟧」と⟦x2⟧"}).decode()
    assert out == "「https://example.org/manual<nowiki/>」と<nowiki/>https://x.org.\n", out
    # an ASCII letter right before the scheme: MediaWiki needs a word boundary there
    out = f.build(src, {"0": "Lisez⟦x1⟧ puis ⟦x2⟧"}).decode()
    assert out == "Lisez<nowiki/>https://example.org/manual puis https://x.org.\n", out
    # spaces, brackets and trailing ASCII punctuation already end the URL
    fr = "Lisez ⟦x1⟧ d'abord, voir (⟦x2⟧)"
    assert f.build(src, {"0": fr}).decode() == (
        "Lisez https://example.org/manual d'abord, voir (https://x.org.)\n"
    )
    dsrc = b"Read https://example.org/manual first.\n"
    dunit = d.parse(dsrc).units[0].text
    dzh = dunit.replace("Read ", "请阅读").replace(" first.", "手册。")
    assert b"<nowiki/>" not in d.build(dsrc, {"0": dzh})
    print("  [cjk url] a bare URL before CJK text ends with <nowiki/> (#699) OK")


def test_encodings():
    data = "Caf\xe9 cr\xe8me paragraph.\n".encode("cp1252")
    p = _wf().parse(data)
    assert p.units[0].text == "Caf\xe9 cr\xe8me paragraph."
    assert p.skeleton.get("source_encoding") == "cp1252"
    assert _wf().build(data, {"0": "Caf\xe9 traduit."}) == "Caf\xe9 traduit.\n".encode()
    try:
        _df().parse(b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x81\x8d\x00" * 20)
    except UnsupportedEncodingError:
        pass
    else:
        raise AssertionError("binary must be refused")
    print("  [encodings] cp1252 converted, binary refused OK")


def test_caps():
    from app import config

    old_n, old_c = config.MAX_DOCUMENT_SEGMENTS, config.MAX_SEGMENT_CHARS
    try:
        config.MAX_DOCUMENT_SEGMENTS = 5
        for f, many in (
            (_wf(), b"".join(b"* item %d\n" % i for i in range(20))),
            (_df(), b"".join(b"  * item %d\n" % i for i in range(20))),
            (_wf(), b"{{" * 300 + b"\n"),
        ):
            try:
                f.parse(many)
            except InputLimitError:
                pass
            else:
                raise AssertionError("segment or markup cap not enforced")
        config.MAX_DOCUMENT_SEGMENTS = old_n
        config.MAX_SEGMENT_CHARS = 50
        try:
            _wf().parse(b"word " * 30 + b"\n")
        except InputLimitError:
            pass
        else:
            raise AssertionError("segment length cap not enforced")
    finally:
        config.MAX_DOCUMENT_SEGMENTS, config.MAX_SEGMENT_CHARS = old_n, old_c
    print("  [caps] segment count, length and markup budget enforced OK")


def test_hostile():
    cases = [
        b"{{" * 10000 + b"\n",
        b"}}" * 10000 + b"\n",
        b"{{a|" * 5000 + b"x" + b"}}" * 5000 + b"\n",
        b"[[" * 10000 + b"\n",
        b"[[a|" * 5000 + b"\n",
        b"''" * 10000 + b"x\n",
        b"'''a " * 8000 + b"\n",
        b"<!--" * 10000 + b"\n",
        b"<nowiki>" * 5000 + b"\n",
        b"<pre " + b"a" * 20000 + b"\n",
        b"{|\n" + b"|" * 20000 + b"\n|}\n",
        b"; " + b"[" * 10000 + b":\n",
        b"=" * 20000 + b"\n",
        b"[http://" + b"a" * 20000 + b"\n",
        b"text " + b"<a " * 8000 + b"\n",
    ]
    dw_cases = [
        b"%%" * 10000 + b"x\n",
        b"**" * 10000 + b"\n",
        b"//" * 10000 + b"\n",
        b"((" * 10000 + b"\n",
        b"^" * 20000 + b"\n",
        b"| [[" * 5000 + b"\n",
        b"<code" * 5000 + b"\n",
        b"~~NOTOC" * 5000 + b"\n",
    ]
    for f, group in ((_wf(), cases), (_df(), dw_cases), (_df(), cases[:6])):
        for data in group:
            t0 = time.monotonic()
            units = f.parse(data).units
            assert f.build(data, {}) == data
            assert f.build(data, {str(u.p_index): u.text for u in units}) == data
            took = time.monotonic() - t0
            assert took < 5, (f.fmt, data[:40], took)
    print("  [hostile] unbalanced braces, brackets, quotes, tags, tables stay fast OK")


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
        p = Project(name="wiki", org_id=1, source_locale="en-US", default_provider="mock")
        s.add(p)
        s.commit()
        s.refresh(p)
        s.add(TargetLocale(project_id=p.id, locale=LOC))
        s.commit()
        pid = p.id
    user = security.create_user("wiki@x.test", "wiki", Role.ADMIN, "wiki-pw-123456")
    _row, raw = apikeys.create_key(user.id, "wiki", scopes="api docs admin")
    client = TestClient(app, raise_server_exceptions=False, follow_redirects=False)
    hdr = {"X-Loc-Token": raw}  # pregate: allow-secret throwaway test key

    r = client.get("/api/formats", headers=hdr)
    assert r.status_code == 200, r.text
    exts = {f["ext"]: f["name"] for f in r.json()["formats"]}
    assert exts.get("wiki") == "MediaWiki wikitext" and exts.get("dokuwiki") == "DokuWiki markup"

    for name, data, kept in (
        ("page.wiki", MW, b'print("hi")  # code stays'),
        ("page.dokuwiki", DW, b"<?php echo 1; ?>"),
    ):
        r = client.post(
            "/api/documents",
            headers=hdr,
            data={"project_id": str(pid)},
            files={"file": (name, data, "text/plain")},
        )
        assert r.status_code in (200, 201), (r.status_code, r.text)
        doc = r.json()["document_id"]
        code, out, err = run_cli(["doc", "translate", str(doc), "--locale", LOC])
        assert code == 0, (code, out, err)
        r = client.get(f"/api/documents/{doc}/download", headers=hdr, params={"locale": LOC})
        assert r.status_code == 200, (r.status_code, r.text)
        body = r.content
        assert b"\xe2\x9f\xa6" not in body, body  # no leaked token brackets
        assert kept in body, body
    print("  [surfaces] API upload, CLI translate, download for both dialects OK")


def main():
    test_registry()
    test_identity()
    test_units_mediawiki()
    test_units_dokuwiki()
    test_dokuwiki_eol_linebreak_678()
    test_dokuwiki_break_glued_701()
    test_translated()
    test_templates()
    test_gate_inert_markup()
    test_cjk_bare_url_699()
    test_encodings()
    test_caps()
    test_hostile()
    test_surfaces()
    print("WIKI FILTER OK")


if __name__ == "__main__":
    main()
