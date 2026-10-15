"""Markdown filter.

Translates the prose while leaving the structure alone:
  - fenced code blocks (``` or ~~~) are skipped entirely
  - each translatable line is split into a leading MARKUP PREFIX (heading #'s,
    list bullets, blockquote >, ordered-list numbers) and a translatable CORE;
    only the core is sent to MT, the prefix is re-attached on build
  - horizontal rules, table separators, and pure-symbol lines are left untouched

parse() and build() walk the lines through the SAME generator, so the unit index
stays stable across the round-trip.

Accessibility: an image's alt text (``![alt](src)``) is part of its line's text, so it
is already translated with the line; such a line is marked with the ``a11y:md-image``
style so the Editor tells the linguist to translate the alt text and keep the ``(...)``
target. An image with an EMPTY alt is reported in ``skeleton["a11y_images"]`` (the
Editor's missing-alt prompt). Markdown has no document language declaration.

Emphasis marks stay text the engine writes; a translated line whose emphasis CommonMark
would print literally (``_x_`` between CJK letters, ``**「x」**`` glued to text) is repaired by
``md_emphasis.fix_cjk_emphasis`` (#695, shared with the MDX filter). A bare URL of the
source that the translation glues to CJK text (GitHub would not link it, or would add the
next words to it) is written as an explicit ``<url>`` autolink (``fix_cjk_autolinks``, #700).
A translated line never opens a block its source line did not: an ordered-list number,
heading ``#``, bullet or ``>`` the engine wrote at its start (typically by dropping the
source's ``1984\\.`` escape) is backslash-escaped (``escape_block_start``, #704).
A table row whose translation lost the backslash of an escaped ``\\|`` (the row would gain a
cell and GFM would hide the last one) gets it back (``restore_cell_escapes``, #710).
A line break inside the translation of a one-line unit is written as a space
(``fold_line_breaks``, #714): it would split a heading or a table row.

Front matter (#731): a YAML block (``---`` ... ``---`` or ``...``) or a Hugo TOML block
(``+++`` ... ``+++``) at the very start of the file is structure, not prose. Its keys,
dates, booleans, numbers, ids, slugs, permalinks, layouts and lists are never offered.
Only the one-line string VALUES of top-level human-readable keys are units, listed by
``LOC_TMS_FRONT_MATTER_KEYS`` (comma separated, case-insensitive; default ``title,
description, summary, excerpt, subtitle, sidebar_label, pagination_label, linkTitle``).
A value is offered only when its own line parses (``yaml.safe_load`` / ``tomllib``) to the
same string the whole block gives that key, so block scalars, multi-line values, nested
maps and lists are left alone. These units come FIRST (reading order), before the body's
line units. On build the translation is written as a valid scalar: the source quoting
style when the translation survives it, else single then double quotes (YAML), always a
basic string (TOML); every candidate is re-parsed before it is written. A block that does
not parse as a mapping but whose first line looks like a key is left untouched (no units);
an opening fence with no closing one, or a block of plain prose, is not front matter.
The walk changed with #731, so new skeletons carry ``md_walk`` = 2 and export rebuilds a
document under the walk it was imported with (``md_walk_version``; no stamp = walk 1, the
pre-#731 walk where front matter lines were ordinary lines). The MDX filter shares all this.

Block structure (#735, walk 3; CommonMark 0.31.2):
  - link reference definitions (4.7, ``[label]: destination "title"``, also over up to three
    lines) are structure: the label and destination are never offered. Only the title, when
    there is one on a single line, is a unit (its backslash escapes decoded); a changed title
    is written back inside the source delimiters (``"``, ``'`` or ``( )``) with ``\\`` and
    that delimiter backslash-escaped, so ``« »`` or ``「」`` from the engine stay title text.
    A definition cannot interrupt a paragraph, so a ``[x]: y`` line glued to prose stays prose.
  - footnote definitions (GFM ``[^label]: text``): the ``[^label]: `` part joins the markup
    prefix (protected like a list bullet); the text is the unit.
  - indented code blocks (4.4, a line indented 4+ columns past its list item's / footnote's
    content column that does not continue a paragraph) are skipped like fenced code. A list
    item's continuation indented 4 spaces is list content and is still translated. MDX has no
    indented code (mdx-md disables it), so the MDX filter keeps those lines as text.
  - HTML comment blocks (4.6 type 2): a run of lines that holds only ``<!-- ... -->``
    comments is skipped entirely. A comment followed by text on its closing line, or one
    that never closes, is left as before.
These change unit numbering, so new skeletons carry ``md_walk`` = 3; a document imported
under walk 1 or 2 still exports under its own walk.
"""

import os
import re
from bisect import bisect_right
from dataclasses import dataclass, field

import tomllib

from app.filters import a11y
from app.filters.base import (
    Filter,
    ParseResult,
    TextUnit,
    check_segment_cap,
    decode_text_lossless,
    encode_text_lossless,
    note_source_encoding,
    read_text_upload,
    register,
)
from app.filters.md_emphasis import (
    escape_block_start,
    fix_cjk_autolinks,
    fix_cjk_emphasis,
    fold_line_breaks,
    restore_cell_escapes,
)

_FENCE = re.compile(r"^\s*(```|~~~)")
# leading markup that should stay verbatim (one or more, e.g. "> - ")
_PREFIX = re.compile(r"^(\s*(?:#{1,6}\s+|[-*+]\s+|\d+[.)]\s+|>\s+)*)")
# horizontal rule / table separator / pure underline. Matched with fullmatch on the
# STRIPPED line (#271): the old `^\s*(...|\|?[\s:|.-]+\|?)\s*$` had three overlapping
# whitespace runs and backtracked cubically (800 spaces and a letter took 1.35 s, 4000 did
# not finish in two minutes). The `|` pieces were already inside the class, so this is the
# same set of lines, matched in linear time.
_SKIP = re.compile(r"[-*_=]{3,}|[\s:|.-]+")
# An inline image: ![alt](target). Reference-style ![alt][ref] counts too. The alt text
# excludes `[` as well as `]` (#271): with `[^\]]*` a line of repeated `![` rescanned the
# rest of the line from every one (quadratic, minutes on a 1 MB line). An alt text with an
# unbalanced `[` was never a real image reference.
_IMAGE = re.compile(r"!\[([^\[\]]*)\]\s*[(\[]([^)\]]*)[)\]]")


def _is_skip_line(line: str) -> bool:
    """A horizontal rule, a table separator row, or a setext underline."""
    return _SKIP.fullmatch(line.strip()) is not None


# ---- front matter (#731) -------------------------------------------------------------

# Walk stamp stored as skeleton["md_walk"] by the md and mdx filters. 1 = no stamp (the
# pre-#731 walk: front matter lines are ordinary lines), 2 = #731 (front matter values of
# allowlisted keys first, then the body). Export keeps the walk a document was imported
# under, so stored paragraph indices never address the other walk's units. 3 = #735 (link
# reference definitions, footnote labels, indented code and HTML comment blocks).
MD_WALK_LEGACY = 1
MD_WALK_VERSION = 3
_KNOWN_MD_WALKS = (1, 2, 3)

DEFAULT_FRONT_MATTER_KEYS = (
    "title",
    "description",
    "summary",
    "excerpt",
    "subtitle",
    "sidebar_label",
    "pagination_label",
    "linkTitle",
)
# A front matter block larger than this is never parsed (left untouched, no units).
_FM_MAX_CHARS = 256 * 1024
_YAML_KEY = re.compile(r"([A-Za-z_][A-Za-z0-9_-]*)[ \t]*:[ \t]+(?=\S)")
_TOML_KEY = re.compile(r"([A-Za-z0-9_-]+)[ \t]*=[ \t]*(?=[\"'])")
_YAML_KEYISH = re.compile(r"[\"']?[\w-]+[\"']?[ \t]*:(?:[ \t]|$)")
_TOML_KEYISH = re.compile(r"(?:[\w.\"'-]+[ \t]*=|\[)")
_TRAILING_COMMENT = re.compile(r"[ \t]+#.*")
_LINE_BREAKS = re.compile(r"[ \t]*(?:\r\n|[\r\n\x0b\x0c\x1c-\x1e\x85  ])[ \t]*")


def front_matter_keys() -> frozenset:
    """The front matter keys (lower-cased) whose string values are translated."""
    raw = os.environ.get("LOC_TMS_FRONT_MATTER_KEYS")
    names = DEFAULT_FRONT_MATTER_KEYS if raw is None else raw.split(",")
    return frozenset(n.strip().lower() for n in names if n.strip())


def md_walk_version(skeleton) -> int:
    """The walk a stored md / mdx skeleton was numbered by (1 or 2).

    Only a MISSING stamp means the legacy walk 1; an unknown stamp (a rollback, a
    corrupted value) is refused rather than guessed."""
    skel = skeleton or {}
    if "md_walk" not in skel:
        return MD_WALK_LEGACY
    stamp = skel["md_walk"]
    if type(stamp) is int and stamp in _KNOWN_MD_WALKS:
        return stamp
    raise ValueError(
        f"This Markdown document was imported with walk {stamp!r}, which this version "
        f"does not know (it knows {', '.join(map(str, _KNOWN_MD_WALKS))}). "
        "Re-import the document to export it."
    )


@dataclass
class FmField:
    line: int  # line index
    start: int  # value span inside the line (quotes included)
    end: int
    style: str  # "plain" / "single" / "double" (YAML), "basic" / "literal" (TOML)
    text: str  # the decoded value: the unit text
    key: str


@dataclass
class FrontMatter:
    kind: str  # "yaml" or "toml"
    end: int  # first line index AFTER the closing fence
    fields: list = field(default_factory=list)


def _yaml_span(line: str, pos: int):
    """(end, style) of the one-line scalar starting at pos, or None."""
    c = line[pos]
    if c == '"':
        j = pos + 1
        while j < len(line):
            if line[j] == "\\":
                j += 2
                continue
            if line[j] == '"':
                break
            j += 1
        else:
            return None
        end, style = j + 1, "double"
    elif c == "'":
        j = pos + 1
        while True:
            k = line.find("'", j)
            if k < 0:
                return None
            if line[k + 1 : k + 2] == "'":
                j = k + 2
                continue
            break
        end, style = k + 1, "single"
    else:
        m = re.search(r"[ \t]#", line[pos:])
        stop = pos + m.start() if m else len(line)
        end, style = pos + len(line[pos:stop].rstrip()), "plain"
    rest = line[end:]
    if rest.strip() and not _TRAILING_COMMENT.fullmatch(rest):
        return None
    return end, style


def _toml_span(line: str, pos: int):
    q = line[pos]
    if line[pos : pos + 3] == q * 3:
        return None  # multi-line string
    if q == '"':
        j = pos + 1
        while j < len(line):
            if line[j] == "\\":
                j += 2
                continue
            if line[j] == '"':
                break
            j += 1
        else:
            return None
        end, style = j + 1, "basic"
    else:
        k = line.find("'", pos + 1)
        if k < 0:
            return None
        end, style = k + 1, "literal"
    rest = line[end:]
    if rest.strip() and not _TRAILING_COMMENT.fullmatch(rest):
        return None
    return end, style


def _offerable(v) -> bool:
    return isinstance(v, str) and any(ch.isalnum() for ch in v) and _LINE_BREAKS.search(v) is None


def find_front_matter(lines: list, keys: frozenset | None = None) -> FrontMatter | None:
    """The front matter at the start of `lines` (no line endings), or None.

    Returns a FrontMatter with no fields for a block that is front matter but cannot be
    read safely (left untouched)."""
    if not lines:
        return None
    first = lines[0].lstrip("﻿").rstrip(" \t")
    if first == "---":
        kind, closers = "yaml", ("---", "...")
    elif first == "+++":
        kind, closers = "toml", ("+++",)
    else:
        return None
    close = None
    for i in range(1, len(lines)):
        if lines[i].rstrip(" \t") in closers:
            close = i
            break
    if close is None:
        return None  # an opening rule with no closing fence: not front matter
    body = lines[1:close]
    content = [ln for ln in body if ln.strip() and not ln.lstrip().startswith("#")]
    keyish = _YAML_KEYISH if kind == "yaml" else _TOML_KEYISH
    looks_like = not content or bool(keyish.match(content[0]))
    block = "\n".join(body)
    fm = FrontMatter(kind, close + 1)
    import yaml  # lazy: only a file that opens with a fence pays for it

    if len(block) > _FM_MAX_CHARS:
        return fm if looks_like else None
    try:
        data = yaml.safe_load(block) if kind == "yaml" else tomllib.loads(block)
    except Exception:  # malformed, too deep (RecursionError), bad encoding
        data = None
    if not isinstance(data, dict):
        if data is None and not content:
            return fm  # an empty block
        return fm if looks_like else None
    allow = front_matter_keys() if keys is None else keys
    for i in range(1, close):
        line = lines[i]
        m = (_YAML_KEY if kind == "yaml" else _TOML_KEY).match(line)
        if not m or m.group(1).lower() not in allow:
            continue
        key = m.group(1)
        span = (_yaml_span if kind == "yaml" else _toml_span)(line, m.end())
        if span is None:
            continue
        end, style = span
        if style == "plain" and line[m.end()] in "&!*|>":
            continue  # gate #731: anchor, tag, alias or block indicator: not plain text
        try:
            one = yaml.safe_load(line[:end]) if kind == "yaml" else tomllib.loads(line[:end])
        except Exception:
            one = None
        if not isinstance(one, dict) or list(one) != [key]:
            continue
        val = one[key]
        if not _offerable(val) or data.get(key) != val:
            continue
        fm.fields.append(FmField(i, m.end(), end, style, val, key))
    return fm


def _dq_escape(s: str, toml: bool) -> str:
    out = ['"']
    for ch in s:
        o = ord(ch)
        if ch == "\\":
            out.append("\\\\")
        elif ch == '"':
            out.append('\\"')
        elif (
            o < 0x20
            or o == 0x7F
            or (not toml and (o in (0x85, 0xFEFF, 0x2028, 0x2029) or 0x80 <= o < 0xA0))
            or 0xD800 <= o <= 0xDFFF
        ):
            out.append(f"\\u{o:04x}" if o <= 0xFFFF else f"\\U{o:08x}")
        else:
            out.append(ch)
    out.append('"')
    return "".join(out)


def _yaml_reads(scalar: str, value: str) -> bool:
    import yaml

    try:
        return yaml.safe_load("k: " + scalar) == {"k": value}
    except Exception:
        return False


def _toml_reads(scalar: str, value: str) -> bool:
    try:
        return tomllib.loads("k = " + scalar) == {"k": value}
    except Exception:
        return False


def encode_fm_value(kind: str, style: str, new: str) -> str | None:
    """`new` written as a scalar that parses back to exactly `new` (after folding line
    breaks to a space: the value stays on its line), or None when no form does."""
    new = _LINE_BREAKS.sub(" ", new)
    if kind == "toml":
        cands = ["'" + new + "'"] if style == "literal" else []
        cands.append(_dq_escape(new, toml=True))
        return next((c for c in cands if _toml_reads(c, new)), None)
    cands = []
    if style == "plain":
        cands.append(new)
    if style in ("plain", "single"):
        cands.append("'" + new.replace("'", "''") + "'")
    cands.append(_dq_escape(new, toml=False))
    return next((c for c in cands if _yaml_reads(c, new)), None)


def md_fm_keys(skeleton) -> frozenset | None:
    """The front matter allowlist a stored skeleton was numbered with (gate #731).

    Front matter units come FIRST, so a host that changes LOC_TMS_FRONT_MATTER_KEYS
    after an import would shift every stored index; export reuses the stored list.
    None (no list stored) means the current allowlist."""
    keys = (skeleton or {}).get("md_fm_keys")
    if isinstance(keys, list) and all(isinstance(k, str) for k in keys):
        return frozenset(k.lower() for k in keys)
    return None


def front_matter_for(
    lines: list, md_walk: int, keys: frozenset | None = None
) -> FrontMatter | None:
    """The front matter the given walk reads (none for the legacy walk)."""
    return find_front_matter(lines, keys) if md_walk >= 2 else None


# ---- block structure (#735) ----------------------------------------------------------

# A GFM footnote definition's label: protected as markup prefix, the text after it is prose.
FOOTNOTE_DEF = re.compile(r"\[\^[^\]\s]+\]:[ \t]*")
_LIST_ITEM = re.compile(r"([-*+]|\d{1,9}[.)])([ \t]+|$)")
# CommonMark 4.5 fences (walk 3): a backtick fence's info string has no backtick
_FENCE_OPEN = re.compile(r"(`{3,})[^`]*$|(~{3,})")
_FENCE_CLOSE = re.compile(r"(`{3,}|~{3,})[ \t]*$")
_ESC_PUNCT = re.compile(r"\\([!-/:-@\[-`{-~])")
_TITLE_CLOSE = {'"': '"', "'": "'", "(": ")"}
# A link reference definition is read over at most this many lines (label, destination,
# title each on its own line); a longer multi-line one stays prose, as before #735.
_REFDEF_LINES = 3


def _indent(line: str) -> tuple[int, int]:
    """(column, offset) of the first character after the leading blanks (tabs stop every
    4 columns, CommonMark 2.2); a leading BOM takes no column."""
    i = 1 if line.startswith("\ufeff") else 0
    col = 0
    while i < len(line) and line[i] in " \t":
        col = col + 4 - col % 4 if line[i] == "\t" else col + 1
        i += 1
    return col, i


@dataclass
class RefDef:
    end: int  # last line index of the definition
    title: tuple | None  # (line, start, end, delim): the title's inner span on one line


def _ws_once(s: str, k: int) -> int:
    """Skip spaces / tabs and at most one line ending."""
    n = len(s)
    while k < n and s[k] in " \t":
        k += 1
    if k < n and s[k] == "\n":
        k += 1
        while k < n and s[k] in " \t":
            k += 1
    return k


def _title_end(s: str, k: int) -> int | None:
    """End (exclusive, after the closing delimiter) of a link title starting at k."""
    close = _TITLE_CLOSE.get(s[k : k + 1])
    if close is None:
        return None
    e = k + 1
    while e < len(s):
        c = s[e]
        if c == "\\":
            e += 2
            continue
        if c == close:
            return e + 1
        if close == ")" and c == "(":
            return None
        e += 1
    return None


def link_ref_def(lines: list, i: int) -> RefDef | None:
    """The link reference definition starting at line i (CommonMark 4.7), or None.

    The caller checks that line i does not continue a paragraph. A footnote definition
    (`[^x]:`) is not a link reference definition (GFM)."""
    _col, p = _indent(lines[i])
    if lines[i][p : p + 1] != "[":
        return None
    chunk = []
    for j in range(i, min(len(lines), i + _REFDEF_LINES)):
        if j > i and not lines[j].strip():
            break
        chunk.append(lines[j])
    s = "\n".join(chunk)
    starts = [0]
    for ln in chunk[:-1]:
        starts.append(starts[-1] + len(ln) + 1)
    n = len(s)
    k = p + 1
    while k < n:  # label
        c = s[k]
        if c == "\\":
            k += 2
            continue
        if c == "[":
            return None
        if c == "]":
            break
        k += 1
    if k >= n:
        return None
    label = s[p + 1 : k]
    if not label.strip() or len(label) > 999 or label.startswith("^"):
        return None
    if s[k + 1 : k + 2] != ":":
        return None
    k = _ws_once(s, k + 2)
    if k >= n or s[k] == "\n":
        return None
    if s[k] == "<":  # destination
        e = k + 1
        while e < n and s[e] not in "<>\n":
            e += 2 if s[e] == "\\" else 1
        if e >= n or s[e] != ">":
            return None
        e += 1
    else:
        e, depth = k, 0
        while e < n:
            c = s[e]
            if c == "\\" and e + 1 < n and s[e + 1] not in " \t\n":
                e += 2
                continue
            if c in " \t\n" or ord(c) < 0x20 or c == "\x7f":
                break
            if c == "(":
                depth += 1
            elif c == ")":
                if depth == 0:
                    return None
                depth -= 1
            e += 1
        if e == k or depth:
            return None

    def line_of(off: int) -> int:
        return bisect_right(starts, off) - 1

    def done(t0: int | None, t1: int | None) -> RefDef:
        last = line_of(max(e, t1 or 0) - 1)
        title = None
        if t0 is not None and line_of(t0) == line_of(t1 - 1):
            li = line_of(t0)
            title = (i + li, t0 + 1 - starts[li], t1 - 1 - starts[li], s[t0])
        return RefDef(i + last, title)

    eol = s.find("\n", e)
    eol = n if eol < 0 else eol
    after = s[e:eol]
    if not after.strip(" \t"):
        if eol < n:  # a title on the next line, else the definition ends here
            t0 = eol + 1
            while t0 < n and s[t0] in " \t":
                t0 += 1
            t1 = _title_end(s, t0)
            if t1 is not None:
                rest_end = s.find("\n", t1)
                if not s[t1 : n if rest_end < 0 else rest_end].strip(" \t"):
                    return done(t0, t1)
        return done(None, None)
    if after[0] not in " \t":
        return None
    t0 = e + len(after) - len(after.lstrip(" \t"))
    t1 = _title_end(s, t0)
    if t1 is None:
        return None
    rest_end = s.find("\n", t1)
    if s[t1 : n if rest_end < 0 else rest_end].strip(" \t"):
        return None
    return done(t0, t1)


def decode_link_title(raw: str) -> str:
    """A link title's text: backslash escapes of ASCII punctuation removed."""
    return _ESC_PUNCT.sub(r"\1", raw)


def encode_link_title(new: str, delim: str) -> str:
    """`new` as the inside of a link title between `delim` (`"`, `'` or `(`): one line,
    backslash and the delimiter(s) backslash-escaped (CommonMark 4.7)."""
    new = _LINE_BREAKS.sub(" ", new)
    special = "\\" + ("()" if delim == "(" else delim)
    return "".join("\\" + c if c in special else c for c in new)


class CommentBlocks:
    """HTML comment blocks (CommonMark 4.6, type 2) that hold nothing but comments."""

    def __init__(self, lines: list):
        self.lines = lines
        self._next = None
        self._tail_ok: dict[int, bool] = {}  # closing line -> its tail holds only comments

    def _next_close(self, j: int) -> int | None:
        if self._next is None:  # next line index at or after j holding `-->` (O(n), once)
            nxt, acc = [None] * (len(self.lines) + 1), None
            for k in range(len(self.lines) - 1, -1, -1):
                if "-->" in self.lines[k]:
                    acc = k
                nxt[k] = acc
            self._next = nxt
        return self._next[j]

    def end(self, i: int) -> int | None:
        """Last line of the comment block starting at line i, or None."""
        line = self.lines[i]
        col, p = _indent(line)
        if col > 3 or not line.startswith("<!--", p):
            return None
        if "-->" in line[p + 2 :]:
            return i if self._only_comments(line, p) else None
        j = self._next_close(i + 1)
        if j is None:
            return None  # never closes: left as text, as before
        # gate #735: the comment opened on line i closes at the first `-->` of line j (no
        # line between holds one), so only line j's tail decides. It is the same for every
        # opener before j: computed once per j, or a file of `<!--` lines closed by one
        # `--> text` line rescans O(n) lines per opener (quadratic).
        if j not in self._tail_ok:
            tail = self.lines[j]
            self._tail_ok[j] = self._only_comments(tail, tail.find("-->") + 3)
        return j if self._tail_ok[j] else None

    @staticmethod
    def _only_comments(s: str, pos: int) -> bool:
        """True when s[pos:] holds nothing but complete `<!-- ... -->` comments and blanks."""
        while True:  # linear (no regex backtracking)
            while pos < len(s) and s[pos] in " \t\n":
                pos += 1
            if pos == len(s):
                return True
            if not s.startswith("<!--", pos):
                return False
            close = s.find("-->", pos + 2)
            if close < 0:
                return False
            pos = close + 3


def list_content_col(line: str, col: int, p: int) -> int | None:
    """The content column of a list item opening at offset p (column col), or None."""
    m = _LIST_ITEM.match(line, p)
    if m is None or _is_skip_line(line):
        return None
    marker = len(m.group(1))
    gap = len(m.group(2).expandtabs(4))
    return col + marker + (gap if 1 <= gap <= 4 else 1)


def _iter_md(lines, start: int = 0, walk: int = MD_WALK_VERSION):
    """Yield (line_index, start, end, text, prefix, title_delim) for each unit from
    `start` on: a line core (`title_delim` "") or, walk 3, a link reference definition's
    title (`text` decoded, `start:end` the span inside the delimiter `title_delim`)."""
    if walk < 3:
        in_fence = False
        for i, line in enumerate(lines):
            if i < start:
                continue
            if _FENCE.match(line):
                in_fence = not in_fence
                continue
            if in_fence or not line.strip() or _is_skip_line(line):
                continue
            prefix = _PREFIX.match(line).group(1)
            core = line[len(prefix) :].rstrip()
            if core.strip() and any(c.isalnum() for c in core):
                yield i, len(prefix), len(prefix) + len(core), core, prefix, ""
        return
    comments = CommentBlocks(lines)
    in_code = para = False
    fence = None  # (char, length, container column) of the open fenced code block
    stack: list[int] = []  # content columns of the open list items / footnote definitions
    i, n = start, len(lines)
    while i < n:
        line = lines[i]
        i += 1
        col, p = _indent(line)
        if fence is not None:
            # CommonMark 4.5: closed by the same character, at least as long, indented
            # less than 4 past its container; a line left of the container ends it too
            fch, flen, fbase = fence
            if line.strip() and col < fbase:
                fence = None  # the list item ended: this line is read below
            else:
                m = _FENCE_CLOSE.match(line, p)
                if m and m.group(1)[0] == fch and len(m.group(1)) >= flen and col - fbase < 4:
                    fence = None
                continue
        if not line.strip():
            para = False  # a blank line does not end indented code or a list
            continue
        if in_code and col - (stack[-1] if stack else 0) >= 4:
            continue
        in_code = False
        item = list_content_col(line, col, p)
        opens = _FENCE_OPEN.match(line, p)
        if not para or item is not None or opens:  # a lazy paragraph line keeps containers
            while stack and col < stack[-1]:
                stack.pop()
        base = stack[-1] if stack else 0
        if col - base >= 4:
            if not para:
                in_code = True  # CommonMark 4.4: cannot interrupt a paragraph
                continue
        elif opens:
            g = opens.group(1) or opens.group(2)
            fence, para = (g[0], len(g), base), False
            continue
        if _is_skip_line(line):
            para = False
            continue
        end = comments.end(i - 1)
        if end is not None:
            para, i = False, end + 1
            continue
        if not para:
            rd = link_ref_def(lines, i - 1)
            if rd is not None:
                if rd.title is not None:
                    li, ts, te, delim = rd.title
                    text = decode_link_title(lines[li][ts:te])
                    if any(c.isalnum() for c in text):
                        yield li, ts, te, text, lines[li][:ts], delim
                i = rd.end + 1
                continue
        prefix = _PREFIX.match(line).group(1)
        heading = "#" in prefix
        if item is not None:
            stack.append(item)
        fn = FOOTNOTE_DEF.match(line, len(prefix))
        if fn is not None:
            prefix = line[: fn.end()]
            stack.append(col + 4)  # GFM: the footnote's continuation lines are indented 4
        core = line[len(prefix) :].rstrip()
        para = not heading and bool(core.strip())
        if core.strip() and any(c.isalnum() for c in core):
            yield i - 1, len(prefix), len(prefix) + len(core), core, prefix, ""


@register
class MarkdownFilter(Filter):
    fmt = "md"
    exts = ("markdown", "mdown", "mkd")

    def parse(self, data: bytes, provider: str | None = None) -> ParseResult:
        text, enc = read_text_upload(data)  # #509: Windows-1252 converted, else refused
        lines = text.splitlines()
        units: list[TextUnit] = []
        n_images = 0
        missing: list[dict] = []
        spans = []
        images: list[tuple] = []  # (line index, alt, target) outside code fences
        fm_keys = front_matter_keys()
        fm = front_matter_for(lines, MD_WALK_VERSION, fm_keys)
        start = fm.end if fm else 0
        in_fence = False
        for li, line in enumerate(lines):
            if li < start:
                continue  # front matter is not rendered: no images there
            if _FENCE.match(line):
                in_fence = not in_fence
                continue
            if not in_fence:
                images.extend((li, m.group(1), m.group(2)) for m in _IMAGE.finditer(line))
        for f in fm.fields if fm else ():
            idx = len(units)
            units.append(TextUnit(p_index=idx, text=f.text))
            check_segment_cap(idx + 1)
            spans.append((f.line, f.line, idx))
        for li, _s, _e, core, _prefix, delim in _iter_md(lines, start):
            idx = len(units)
            style = "a11y:md-image" if not delim and _IMAGE.search(core) else ""
            units.append(TextUnit(p_index=idx, text=core, style=style))
            check_segment_cap(idx + 1)  # #571: stop before building the rest
            spans.append((li, li, idx))
        for li, alt, target in images:
            n_images += 1
            if not alt.strip():
                where = target.strip().split(" ")[0].rsplit("/", 1)[-1]
                missing.append(a11y.missing_alt("empty", a11y.anchor_for(li, spans), where))
        skeleton = {
            "format": "md",
            "md_walk": MD_WALK_VERSION,  # #731
            "md_fm_keys": sorted(fm_keys),  # gate #731: export reuses this allowlist
            "units": [{"p_index": u.p_index, "text": u.text, "style": u.style} for u in units],
        }
        if n_images:
            skeleton["a11y_images"] = {"images": n_images, "missing_alt": missing}
        return ParseResult(skeleton=note_source_encoding(skeleton, enc), units=units)

    def build_localized(
        self,
        original: bytes,
        paragraph_texts: dict,
        target_locale: str | None = None,
        md_walk: int = MD_WALK_VERSION,
        fm_keys: frozenset | None = None,
    ) -> bytes:
        """Markdown has no language declaration: build() under the stored walk (#731)."""
        return self.build(original, paragraph_texts, md_walk=md_walk, fm_keys=fm_keys)

    def build(
        self,
        original: bytes,
        paragraph_texts: dict,
        md_walk: int = MD_WALK_VERSION,
        fm_keys: frozenset | None = None,
    ) -> bytes:
        text = decode_text_lossless(original)
        lines = text.splitlines()
        # #677: keep every line's own ending (CRLF, the final newline or its absence) and the
        # whitespace after the translated text (two trailing spaces are a Markdown hard line
        # break). `splitlines(keepends=True)` splits at exactly the same places, so the
        # indices `_iter_md` yields line up with `out`; only the `core` span is replaced.
        out = text.splitlines(keepends=True)
        fm = front_matter_for(lines, md_walk, fm_keys)
        fields = fm.fields if fm else []
        for k, f in enumerate(fields):  # #731: front matter values come first
            new = paragraph_texts.get(str(k))
            if new is not None and new != f.text:
                val = encode_fm_value(fm.kind, f.style, new)
                if val is not None:
                    bare = lines[f.line]
                    out[f.line] = bare[: f.start] + val + bare[f.end :] + out[f.line][len(bare) :]
        off = len(fields)
        units = _iter_md(lines, fm.end if fm else 0, md_walk)
        for k, (li, cs, ce, core, prefix, delim) in enumerate(units):
            new = paragraph_texts.get(str(k + off))
            if new is not None and delim:  # #735: a link title, inside its delimiters
                if new != core:
                    bare = lines[li]
                    val = encode_link_title(new, delim)
                    out[li] = bare[:cs] + val + bare[ce:] + out[li][len(bare) :]
            elif new is not None:
                bare = lines[li]
                tail = bare[ce:]
                if new != core:  # #695: emphasis marks glued to CJK text
                    new = fold_line_breaks(core, new)  # #714: one line stays one line
                    new = fix_cjk_emphasis(core, new)
                    new = fix_cjk_autolinks(core, new)  # #700
                    new = restore_cell_escapes(core, new)  # #710: table cell `\|`
                    if "#" not in prefix:  # #704 (heading text is inline: no block start)
                        new = escape_block_start(core, new)
                out[li] = prefix + new + tail + out[li][len(bare) :]
        return encode_text_lossless("".join(out))
