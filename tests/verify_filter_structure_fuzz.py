"""Round-trip structure fuzz for the injection-prone text filters (g1-inject gate, 2026-11-07).

For MDX, AsciiDoc, PHP arrays, JS/TS modules, SVG and JSP: a small benign source, then
random translations drawn from that format's special characters plus letters (fixed seed,
the unit's own tokens kept in order). Each export is re-read with the same filter:
  S1 same unit count and styles, in order
  S2 the skeleton is unchanged: the export rebuilt with the SOURCE texts equals the source
     after one plain translation and back (whitespace-insensitive for markup formats, whose
     units are trimmed)
  S3 each unit reads back as the translation that was set, once the escape the filter adds is
     undone on both sides
  S4 an independent parser sees the same structure: lxml (SVG element tree, JSP tags, EL and
     scriptlet counts), markdown-it block tokens for MDX (when installed)
A build that raises InputLimitError counts as a clean refusal. Every translation holds one
plain word (a symbol- or placeholder-only string is not a unit by design).

The three "+" cases port Bilingua's structure fuzz (the Bilingua repo,
tests/structure_fuzz.rs: the same sources and atoms) so the two engines agree: MDX
with inline codes (`<Badge>`, `{count}`, a code span), Markdown, and AsciiDoc with a
constrained mark, a table and a description list. Their translations are built from
multi-character atoms (```` ``` ````, `import `, `::`, `== `), so the block starts the
trackers named come up, and heading text may start with a marker (#790: escaped now). The
original seven cases keep their alphabets and draws.

Hermetic + offline. Final line: "FILTER STRUCTURE FUZZ OK".
"""

import html
import os
import random
import re
import sys
import time

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(_ROOT)
sys.path.insert(0, _ROOT)
from tests._bootstrap import init_env

init_env("loctms_fzstruct_")

from lxml import etree
from lxml import html as lhtml

from app.filters import get_filter

try:
    from markdown_it import MarkdownIt
except ImportError:  # S4 for MDX is skipped
    MarkdownIt = None

N = int(os.environ.get("LOC_TMS_FUZZ_CASES", "1500"))
FMT = {"mdx+": "mdx", "md+": "md", "adoc+": "adoc"}  # "+" case -> filter format
SEED = 790807

SRCS = {
    "mdx": (
        "import { Note } from './note'\n\n# Getting started\n\nHello world paragraph.\n\n"
        "<Note>\n\nInside a component.\n\n</Note>\n\n| Col A | Col B |\n|---|---|\n"
        "| cell one | cell two |\n\n- item one\n- item two\n\n> quoted text\n"
    ),
    "adoc": (
        "= Document Title\n:attr: value\n\nIntro paragraph text.\n\n== Section heading\n\n"
        '[cols="1,1"]\n|===\n|Cell one |Cell two\n|Cell three\na|\n!===\n'
        "!Nested one !Nested two\n!===\n|===\n\n* List item one\n* List item two\n\n"
        "NOTE: Admonition text.\n"
    ),
    "php": (
        "<?php\n// comment\nreturn [\n    'a' => 'Hello world',\n"
        "    'b' => \"Double quoted text\",\n"
        "    'nested' => ['c' => 'Nested one', 'list' => ['One', 'Two']],\n"
        "    'tail' => <<<EOT\nHeredoc body text\nEOT,\n"
        "    'now' => <<<'EOT'\nNowdoc body text\nEOT,\n];\n"
    ),
    "js": (
        "// header\nexport default {\n  a: 'Hello world',\n  b: \"Double text\",\n"
        "  c: `Template text`,\n  nested: { d: 'Nested one' },\n  list: ['One', 'Two'],\n}\n"
    ),
    "ts": (
        "import type { M } from './t'\ninterface Extra {\n  note?: string\n}\n"
        "export type Msg = { a: string }\nexport const en: M = {\n  a: 'Hello world',\n"
        "  b: `Multi line`,\n} as const\nexport default en\n"
    ),
    "svg": (
        '<svg xmlns="http://www.w3.org/2000/svg" xmlns:x="http://www.w3.org/2000/svg" '
        'width="100" height="50">\n<title>Chart title</title>\n<desc>A description</desc>\n'
        '<text x="1" y="2">Hello world</text>\n<text><tspan>Span text</tspan> tail text</text>\n'
        "<x:script>var a = 1;</x:script>\n<x:style>a { b: c }</x:style>\n"
        "<g><text>Group &amp; text</text></g>\n</svg>\n"
    ),
    "jsp": (
        '<%@ page contentType="text/html; charset=UTF-8" %>\n<html><body>\n<h1>Welcome</h1>\n'
        "<p>Hello ${user.name} and welcome</p>\n<p>Total: ${cart.total}</p>\n"
        "<p>#{greeting} friend</p>\n"
        "<table><tr><td>Cell one</td><td>Cell two</td></tr></table>\n"
        '<% out.println("x"); %>\n<p>Plain &amp; paragraph</p>\n'
        '<a title="Tip text" href="x">Link text</a>\n</body></html>\n'
    ),
    # Bilingua structure_fuzz.rs MDX_SRC, MD_SRC and ADOC_SRC
    "mdx+": (
        "import { Badge } from './badge'\n\n# Getting started\n\n"
        "Hello <Badge>new</Badge> world and {count} items.\n\n<Note>\n\n"
        "Inside a component with `code`.\n\n</Note>\n\n| Col A | Col B |\n|---|---|\n"
        "| cell one | cell two |\n\n- item one\n- item two\n\n> quoted text here\n\n"
        "Last paragraph.\n"
    ),
    "md+": (
        "# Getting started\n\nHello *world* and `code` here.\n\n"
        "Second paragraph with a [link](https://example.org/a) in it.\n\n"
        "| Col A | Col B |\n|---|---|\n| cell one | cell two |\n\n"
        "- item one\n- item **two**\n\n> quoted text here\n\nLast paragraph.\n"
    ),
    "adoc+": (
        "= Document Title\n:attr: value\n\nIntro paragraph text.\n\n== Section heading\n\n"
        "A paragraph with *strong* words.\n\n"
        '[cols="1,1"]\n|===\n|Cell one |Cell two\n|Cell three |Cell four\n|===\n\n'
        "* List item one\n* List item two\n\nTerm one:: Its description.\n\n"
        "NOTE: Admonition text.\n"
    ),
}
# Bilingua's atoms (MDX_ATOMS, MD_LOCOS_ATOMS, ADOC_ATOMS). Like Bilingua, no lone inline mark
# for AsciiDoc: one further from a code's mark can still pair with it (open there too).
ATOMS = {
    "mdx+": [
        "{", "}", "<", ">", "\\", "`", "``", "```", "~~~", "\n", "|", "import ", "export ",
        "&", "#", "- ", "1. ",
    ],
    "md+": [
        "`", "``", "```", "~~~", "*", "_", "#", "# ", "- ", "+ ", "1. ", "> ", "[", "]",
        "\\", "&", "\n",
    ],
    "adoc+": [
        "|", "!", "{", "}", ":", "::", ":::", ";;", "/", "//", "\n", "* ", "[", "]", "=",
        "== ", ". ", "- ",
    ],
}  # fmt: skip
ATOM_LETTERS = ["a", "b", "x", "z", " ", "A", "w"]
ALPHA = {
    "mdx": "{}<>\\`\n",
    "adoc": "|!{}:/;\n",
    "php": "${}\\'\"\r\n",
    "js": "/*`${}'\"\\\n",
    "ts": "/*`${}'\"\\\n",
    "svg": "<>&;:'\"",
    "jsp": "$#{}\\'\"<>&",
}
LETTERS = "abcxyz ABC"
TOK = re.compile(r"⟦(/?)([a-z]?)(\d+)⟧")
_REF = re.compile(r"&(?:#[0-9]{1,7}|#[xX][0-9a-fA-F]{1,6}|[A-Za-z][A-Za-z0-9]{1,31});")


class Fail(Exception):
    pass


def _rand(rng, alpha):
    n = rng.randint(1, 12)
    return "".join(
        rng.choice(alpha) if rng.random() < 0.55 else rng.choice(LETTERS) for _ in range(n)
    )


def _atoms(rng, atoms):
    pool = atoms + ATOM_LETTERS
    return "".join(rng.choice(pool) for _ in range(rng.randint(0, 7)))


def _translation(rng, unit, alpha, heading):
    if isinstance(alpha, list):  # a "+" case: Bilingua's atoms, one piece per text run
        if unit.text.startswith("|") and unit.text.endswith("|"):  # Bilingua: no `|` added
            cells = unit.text.strip("|").split("|")
            pieces = (_atoms(rng, alpha).replace("|", "") for _ in cells)
            return "|" + "|".join(" w" + p + " " for p in pieces) + "|"
        out = [_atoms(rng, alpha)]
        for m in TOK.finditer(unit.text):
            out += [m.group(0), _atoms(rng, alpha)]
        # a pair keeps some content (an empty `*` `*` pair is not formatting: out of scope)
        s = re.sub(r"(⟦[a-z]?(\d+)⟧)(\s*)(⟦/[a-z]?\2⟧)", r"\1\3p\4", "".join(out))
        return "w " + s if rng.random() < 0.5 else s + " w"
    if unit.text.startswith("|") and unit.text.endswith("|"):  # a Markdown row keeps its cells
        cells = unit.text.strip("|").split("|")
        return "|" + "|".join(" w" + _rand(rng, alpha) + " " for _ in cells) + "|"
    out = [_rand(rng, alpha) if rng.random() < 0.8 else ""]
    for m in TOK.finditer(unit.text):
        out += [m.group(0), _rand(rng, alpha) if rng.random() < 0.7 else ""]
    s = "".join(out)
    return "w " + s if heading or rng.random() < 0.5 else s + " w"


def _expand(text, imap):
    def rep(m):
        info = imap.get(m.group(3)) or {}
        if isinstance(info, list):  # AsciiDoc: [open, close]
            return str(info[1] if m.group(1) else info[0])
        if not isinstance(info, dict):
            return str(info)
        if m.group(1):
            return str(info.get("close", m.group(0)))
        return str(info.get("point") or info.get("open") or m.group(0))

    return TOK.sub(rep, text)


def _ws(s):
    return re.sub(r"\s+", " ", s).strip()


def _unref(s):
    return _REF.sub(lambda m: html.unescape(m.group(0)), s)


def _norm(fmt, s):
    if fmt in ("mdx", "md"):
        return _ws(_unref(re.sub(r"\\([!-/:-@\[-`{-~])", r"\1", s)))
    if fmt == "adoc":
        s = s.replace("{empty}", "").replace("{two-colons}", "::")
        s = s.replace("{two-semicolons}", ";;")
        return _ws(_unref(re.sub(r"\\([|!])", r"\1", s)))  # &#42; / &#93; (#788 fuzz)
    if fmt == "jsp":
        s = re.sub(r"\\([$#])\{", r"\1{", s.replace("&#92;", "\\"))
        return _ws(_unref(s))
    if fmt == "svg":
        return _ws(_unref(s))
    return s


def _s3(fmt, text, imap, own=None):
    """S3 text of a unit. Markdown, MDX and AsciiDoc (as Bilingua's fuzz): the escapes are
    undone in each text run on its own and the codes written as their markup (`own`: the
    map the text's tokens come from), so an escape the writer adds next to a code
    (`\\\\<Badge>`) is not read as escaping the code; a doubled AsciiDoc mark (`**`, the
    writer's unconstrained form) counts as one; then blanks removed (AsciiDoc: its
    attribute guards undone over the whole text too)."""
    if fmt not in ("mdx", "md", "adoc"):
        return _norm(fmt, _expand(text, imap))
    out, pos = [], 0
    for m in TOK.finditer(text):
        out.append(_norm(fmt, text[pos : m.start()]))
        piece = _expand(m.group(0), own or imap)
        if fmt == "adoc" and len(piece) == 2 and piece[0] == piece[1]:
            piece = piece[0]
        out.append(piece)
        pos = m.end()
    out.append(_norm(fmt, text[pos:]))
    s = "".join(out)
    if fmt == "adoc":
        s = _norm(fmt, s)
    return re.sub(r"\s+", "", s)


def _shape(fmt, data):
    if fmt == "svg":
        root = etree.fromstring(data, etree.XMLParser(resolve_entities=False, no_network=True))
        return [(el.tag, tuple(sorted(el.attrib.items()))) for el in root.iter()]
    if fmt == "jsp":
        t = data.decode("utf-8")
        doc = lhtml.document_fromstring(t)
        tags = [(e.tag, tuple(sorted(e.attrib))) for e in doc.iter() if isinstance(e.tag, str)]
        return len(re.findall(r"(?<!\\)[$#]\{", t)), t.count("<%"), tags
    if fmt in ("mdx", "md") and MarkdownIt is not None:
        md = MarkdownIt("commonmark").enable("table")
        toks = md.parse(data.decode("utf-8"))
        return [(t.type, t.tag, t.nesting) for t in toks if t.type != "inline"]
    return None


def _adoc_skeleton(data):
    """The AsciiDoc text outside the translatable units, blanks removed (Bilingua raw_s2)."""
    from app.filters.asciidoc_filter import _translatable, _units

    text = data.decode("utf-8")
    out, pos = [], 0
    for slot, unit, _imap in _units(text):
        if _translatable(unit):
            out += [text[pos : slot["start"]], "\x00"]
            pos = slot["end"]
    out.append(text[pos:])
    return re.sub(r"\s+", "", "".join(out))


def _check(fmt, F, src, units, canon, trans, raw_s2=False):
    try:
        out = F.build(src, trans)
    except ValueError:  # InputLimitError: a clean refusal
        return "refused"
    try:
        again = F.parse(out).units
    except Exception as e:
        raise Fail(f"export not readable: {type(e).__name__}: {e}") from None
    if len(again) != len(units) or [u.style for u in again] != [u.style for u in units]:
        raise Fail(f"S1 units {len(units)} -> {len(again)}: {[u.text for u in again]}")
    back = F.build(out, {str(b.p_index): a.text for a, b in zip(units, again, strict=True)})
    if raw_s2:
        # adoc+: an {empty} guard read back as a token renumbers a pair, so the source text
        # cannot be set back by token number; the skeleton between the units is compared
        same = _adoc_skeleton(out) == _adoc_skeleton(src)
    elif fmt in ("svg", "jsp", "mdx", "md", "adoc"):
        same = re.sub(rb"\s+", b"", back) == re.sub(rb"\s+", b"", canon)
    else:
        same = back == canon
    if not same:
        raise Fail("S2 skeleton changed:\n" + back.decode("utf-8", "replace"))
    for a, b in zip(units, again, strict=True):
        # the set text's tokens are the source unit's (a re-read can renumber them)
        want = _s3(fmt, trans[str(a.p_index)], b.inline_map, a.inline_map)
        got = _s3(fmt, b.text, b.inline_map)
        if want != got:
            raise Fail(f"S3 unit {a.p_index}: set {want!r} read {got!r}")
    if _shape(fmt, out) != _shape(fmt, src):
        raise Fail("S4 independent parser sees a different structure")
    return "ok"


def run(key, n=N):
    fmt = FMT.get(key, key)
    F = get_filter(fmt)
    src = SRCS[key].encode("utf-8")
    units = F.parse(src).units
    glue = "p" if key in FMT else ""  # "+" cases: a pair keeps a letter inside (`*p*`)
    toks = {str(u.p_index): glue.join(m.group(0) for m in TOK.finditer(u.text)) for u in units}
    plain = F.build(src, {k: "Plain " + v + " words" for k, v in toks.items()})
    p = F.parse(plain).units
    canon = F.build(plain, {str(b.p_index): a.text for a, b in zip(units, p, strict=True)})
    headings = {i for i, u in enumerate(units) if key == "mdx" and i == 0}
    rng = random.Random(SEED)
    stats = {"ok": 0, "refused": 0}
    for _ in range(n):
        trans = {
            str(u.p_index): _translation(rng, u, ATOMS.get(key) or ALPHA[key], i in headings)
            for i, u in enumerate(units)
        }
        try:
            stats[_check(fmt, F, src, units, canon, trans, key == "adoc+")] += 1
        except Fail as e:
            raise AssertionError(f"{key}: {e}\ntranslations: {trans!r}") from None
    return stats


def main():
    t0 = time.time()
    for fmt in SRCS:
        t = time.time()
        stats = run(fmt)
        took = time.time() - t
        print(f"  [{fmt}] {N} cases: {stats['ok']} ok, {stats['refused']} refused ({took:.1f} s)")
    print(f"  all formats in {time.time() - t0:.1f} s, seed {SEED}")
    print("FILTER STRUCTURE FUZZ OK")


if __name__ == "__main__":
    main()
