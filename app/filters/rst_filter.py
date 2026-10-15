"""reStructuredText filter (.rst, .rest).

A line-grammar walk over the source following the docutils reStructuredText
specification (https://docutils.sourceforge.io/docs/ref/rst/restructuredtext.html).
No docutils dependency and nothing is ever executed or followed. Translatable units:

  - paragraphs (lines joined into one unit; a trailing ``::`` stays outside the unit and
    the literal block after it is skipped), block quotes, definition-list terms and
    definitions, line-block lines, footnote and citation bodies
  - section titles, underline-only and overline + underline. On build the adornment is
    resized to the translated title: its length changes by the same number of columns as
    the title, never shorter than the title, counting East Asian wide and full-width
    characters as 2 columns and combining marks as 0 (docutils ``column_width``)
  - bullet and enumerated list items (the marker stays outside the unit)
  - admonitions (``note``, ``warning``, ... ``admonition`` whose title is a unit,
    ``seealso``, ``versionadded``/``versionchanged``/``deprecated`` text after the version),
    titled and container directives (``topic``, ``sidebar``, ``rubric``, ``table``,
    ``list-table``, ``figure`` captions, ``container``, ``only``, ...), domain directive
    bodies (``py:function`` etc.; the signature is not translated)
  - grid table and simple table cells. A translated cell is re-wrapped inside its column;
    a grid row grows by a line when the row's last line is a plain content line, a simple
    table row grows by continuation lines. Column widths never change: a translation
    that cannot fit (a word wider than the column, a first simple-table column that needs
    a second line) refuses the export with a message naming the table line and cell
    (TableFitError). Tables the walk cannot map (malformed grids, text across simple
    table column gaps, cells holding lists or several paragraphs) are kept verbatim and
    listed in ``skeleton["rst_skipped_tables"]``.

Never translated, kept byte for byte: literal blocks (indented and quoted) after ``::``,
``code-block``/``code``/``sourcecode``/``literalinclude``/``raw``/``math``/``csv-table``
and every unknown directive, comments, hyperlink targets, substitution definitions,
directive options (``:name: value`` lines), field lists, option lists, doctest blocks,
transitions. ``.. include::`` is never followed (the line is kept as is). #733: the
``:description:`` and ``:keywords:`` values of a ``.. meta::`` directive (the page's HTML
``<meta>`` text) are units appended after every body unit (style ``meta:*``), written
back on one line; every other meta field stays as is.

Inline markup becomes protected tokens from the shared ``services/segment`` scheme:
inline literals, roles (``:ref:`...```), interpreted text, substitution references
``|x|``, footnote and citation references, named hyperlink references (``word_``,
```text`_``), inline targets, standalone URLs and backslash escapes are point tokens
``⟦xN⟧``; ``*emphasis*``, ``**strong**`` and embedded-URI references
```text <url>`_`` are pairs ``⟦gN⟧text⟦/gN⟧`` so the words are translated and the
markup is not. Markup a translation glues to a letter (Japanese, Chinese) gets an escaped
space (backslash, space) on that side, so docutils still recognises it (#695). parse()
and build() walk the same slots in the same order; build() re-derives each slot's token
map from the original, so untouched bytes are identical.

Encoding: UTF-8 (docutils' default input encoding; a BOM is kept), with the shared #509
decode. reStructuredText has no document language declaration (Sphinx keeps it in
conf.py), so build_localized is build.

Limits: tabs count as one column of indentation (docutils expands them to 8); field-list
bodies (often bibliographic or Sphinx metadata) and ``csv-table`` content are not
translated; a simple table's blank line ends a row.

Documented in: docs/architecture/translation-pipeline.md
"""

from __future__ import annotations

import re
import unicodedata

from app.filters.base import (
    Filter,
    InputLimitError,
    ParseResult,
    TextUnit,
    check_segment_cap,
    check_unit_texts,
    decode_text_lossless,
    encode_text_lossless,
    note_source_encoding,
    read_text_upload,
    register,
)
from app.services.segment import _PLACEHOLDER_RE, ph_close, ph_open, ph_point, strip_placeholders

_BOM = b"\xef\xbb\xbf"
_LINE_RE = re.compile(r"([^\r\n]*)(\r\n|\r|\n|$)")
_MAX_DEPTH = 40
_MAX_TOKENS = 1000

# --- block grammar ------------------------------------------------------------------
_PUNCT = "!\"#$%&'()*+,-./:;<=>?@[\\]^_`{|}~"
_BULLET = re.compile(r"([-*+•‣⁃])(?:[ \t]+|$)")
_ENUM = re.compile(r"(\(?)(\d{1,9}|#|[A-Za-z]|[ivxlcdmIVXLCDM]{1,10})([.)])(?:[ \t]+|$)")
_FIELD = re.compile(r":(?![: ])(?:[^:\\\r\n]|\\.){1,200}(?<! ):(?:[ \t]+|$)")
_OPTION_LIST = re.compile(
    r"(?:--?[A-Za-z0-9][\w-]*|/[A-Za-z0-9]\w*)(?:[ =]<?[\w-]+>?)?(  +\S|[ \t]*$)"
)
_LINE_BLOCK = re.compile(r"\|(?:[ \t]+|$)")
_GRID_TOP = re.compile(r"\+(?:[-=]+\+)+[ \t]*")
_SIMPLE_BORDER = re.compile(r"=+(?:[ \t]+=+)+[ \t]*")
_SIMPLE_SPAN = re.compile(r"-+(?:[ \t]+-+)*[ \t]*")
_DIRECTIVE = re.compile(r"([A-Za-z0-9](?:[\w:+.-]{0,100}[\w])?)::(?:[ \t]+|$)")
_FOOTNOTE = re.compile(r"\[(?:\d+|#[\w.-]*|\*|[\w.-]+)\](?:[ \t]+|$)")
_OPTION_LINE = re.compile(r":[\w][\w .-]{0,100}:(?:[ \t]|$)")
# #733: a `.. meta::` field whose value is page text (HTML <meta name="description" /
# "keywords">). Other meta fields (robots, http-equiv=..., lang-qualified names) stay as is.
_META_FIELD = re.compile(r":(description|keywords):(?:[ \t]+|$)", re.IGNORECASE)

_ADMONITIONS = frozenset(
    {
        "attention",
        "caution",
        "danger",
        "error",
        "hint",
        "important",
        "note",
        "tip",
        "warning",
        "seealso",
        "todo",
    }
)
_VERSIONED = frozenset({"versionadded", "versionchanged", "deprecated", "versionremoved"})
# The directive argument is translatable text (a title); the content is walked.
_TITLED = frozenset({"admonition", "topic", "sidebar", "table", "list-table", "rubric", "centered"})
# The argument is not text (a path, a condition, a class); the content is walked.
_CONTAINERS = frozenset(
    {
        "figure",
        "container",
        "only",
        "ifconfig",
        "compound",
        "epigraph",
        "highlights",
        "pull-quote",
        "class",
        "rst-class",
        "cssclass",
        "glossary",
        "hlist",
        "line-block",
        "parsed-literal-text",
    }
)


class TableFitError(InputLimitError):
    """A translated table cell does not fit its column (the column widths of a
    reStructuredText table are part of its markup and are never changed)."""


def _cw(ch: str) -> int:
    if unicodedata.combining(ch):
        return 0
    return 2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1


def column_width(s: str) -> int:
    """Display columns as docutils counts them (wide/full-width = 2, combining = 0)."""
    return sum(_cw(c) for c in s)


def _lines(text: str):
    for m in _LINE_RE.finditer(text):
        if m.start() == len(text) and not m.group(0):
            break
        yield m.start(1), m.group(1)
        if not m.group(2):
            break


def _blank(s: str) -> bool:
    return not s.strip()


def _indent(s: str) -> int:
    return len(s) - len(s.lstrip(" \t"))


def _is_adorn(s: str) -> bool:
    r = s.rstrip()
    return bool(r) and r[0] in _PUNCT and r == r[0] * len(r)


def _end_indented(vl, i: int) -> int:
    """First index after the indented (or blank) lines from i, trailing blanks excluded."""
    j = i
    while j < len(vl) and (_blank(vl[j][1]) or _indent(vl[j][1]) >= 1):
        j += 1
    while j > i and _blank(vl[j - 1][1]):
        j -= 1
    return j


def _dedent(vl) -> list:
    ind = [_indent(s) for _, s in vl if not _blank(s)]
    m = min(ind) if ind else 0
    return [(off + m, s[m:]) if not _blank(s) else (off + len(s), "") for off, s in vl]


# --------------------------------------------------------------------------- inline tokens

_INLINE_RE = re.compile(
    r"(?P<esc>\\.)"
    r"|(?P<lit>(?<![\w\\])``(?!\s)[\s\S]{1,2000}?(?<!\s)``(?!\w))"
    r"|(?P<role>(?<![\w\\`]):[A-Za-z][\w.+:-]{0,100}:`[^`]{1,2000}?`(?!\w))"
    r"|(?P<eref>(?<![\w\\`])`(?P<etext>[^`<]{0,2000}?)(?P<euri>\s*<[^`<>]{1,2000}>)`__?(?!\w))"
    r"|(?P<ref>(?<![\w\\`])`[^`]{1,2000}?`(?::[A-Za-z][\w.+:-]{0,100}:|__?)?(?!\w))"
    r"|(?P<subst>(?<![\w\\|])\|(?! )[^|\r\n]{1,200}?(?<! )\|(?:__?)?(?!\w))"
    r"|(?P<fnref>(?<![\w\\])\[(?:\d+|#[\w.-]*|\*|[\w.-]+)\]_(?!\w))"
    r"|(?P<target>(?<![\w\\])_`[^`]{1,2000}?`(?!\w))"
    r"|(?P<strong>(?<![\w\\*])\*\*(?![\s*])(?P<sin>[\s\S]{1,2000}?)(?<![\s\\])\*\*(?![\w*]))"
    r"|(?P<em>(?<![\w\\*])\*(?![\s*])(?P<ein>[^*]{1,2000}?)(?<![\s\\])\*(?![\w*]))"
    r"|(?P<url>(?<![\w/\\<])(?:https?|ftp|mailto):[^\s<>`]*[^\s<>`.,;:!?)'\"])"
    r"|(?P<wref>(?<![\w\\.-])[A-Za-z0-9][\w.+-]{0,200}(?<![._+-])__?(?![\w]))"
)
_SOFT = re.compile(r"[ \t]*(?:\r\n|\r|\n)[ \t]*")


class _Tok:
    def __init__(self):
        self.n = 0
        self.map: dict = {}

    def point(self, raw: str) -> str:
        self.n += 1
        self.map[str(self.n)] = raw
        return ph_point("x", self.n)

    def pair(self, open_: str, inner: str, close: str, depth: int) -> str:
        self.n += 1
        n = self.n
        self.map[str(n)] = [open_, close]
        return ph_open("g", n) + _tokenize(inner, self, depth + 1) + ph_close("g", n)


def _plain(s: str) -> str:
    return _SOFT.sub(" ", s)


def _tokenize(s: str, tok: _Tok, depth: int = 0) -> str:
    out: list[str] = []
    pos = 0
    for m in _INLINE_RE.finditer(s):
        if tok.n >= _MAX_TOKENS:
            break
        out.append(_plain(s[pos : m.start()]))
        pos = m.end()
        raw = m.group(0)
        if depth < 4 and m.group("strong") is not None:
            out.append(tok.pair("**", m.group("sin"), "**", depth))
        elif depth < 4 and m.group("em") is not None:
            out.append(tok.pair("*", m.group("ein"), "*", depth))
        elif m.group("eref") is not None and any(c.isalpha() for c in m.group("etext")):
            etext = m.group("etext")
            out.append(tok.pair("`", etext, raw[1 + len(etext) :], 4))
        else:
            out.append(tok.point(raw))
    out.append(_plain(s[pos:]))
    return "".join(out)


def _unit_of(raw: str) -> tuple[str, dict]:
    tok = _Tok()
    return _tokenize(raw, tok).strip(), tok.map


_ESC_TEXT = re.compile(r"[*`|\\]|(?<=\w)_(?!\w)")
_PARA_START = re.compile(
    r"(?:[-*+•‣⁃](?:\s|$)|\(?(?:\d+|#|[A-Za-z]|[ivxlcdmIVXLCDM]+)[.)](?:\s|$)"
    r"|\.\.(?:\s|$)|\|(?:\s|$)|:(?![: ])[^:]+:(?:\s|$)|>>>|__ |[-=~^\"'`#*+<>]{4,}\s*$)"
)


def _render(translation: str, imap: dict) -> str:
    """Write a translated unit back as reStructuredText on one line: tokens back to their
    markup (line breaks inside a token folded to a space), characters that would start
    inline markup in the translated words escaped."""
    t = re.sub(r"[ \t\r\n]+", " ", translation).strip(" ")  # never \s: keep no-break spaces
    out: list[str] = []
    pos = 0
    opened: list[str] = []  # pair ids whose opening markup was written, innermost last
    open_at: dict[str, int] = {}  # pair id -> index of its opening piece in out
    spans: list[tuple[int, int]] = []  # (first piece, last piece) of each written markup
    for m in _PLACEHOLDER_RE.finditer(t):
        out.append(_ESC_TEXT.sub(lambda x: "\\" + x.group(0), t[pos : m.start()]))
        pos = m.end()
        inner = m.group(0)[1:-1]
        closing = inner.startswith("/")
        key = inner.lstrip("/")[1:]
        val = imap.get(key)
        if val is None:
            continue  # unknown token: dropped
        if isinstance(val, list):
            # Gate: never leave a start-string without its end-string (docutils warns,
            # `sphinx -W` fails): a lone close is dropped, an unclosed open is closed.
            if not closing:
                if key not in opened:
                    opened.append(key)
                    open_at[key] = len(out)
                    out.append(_plain(val[0]))
            elif key in opened:
                while opened:
                    k = opened.pop()
                    spans.append((open_at[k], len(out)))
                    out.append(_plain(imap[k][1]))
                    if k == key:
                        break
        elif not closing:
            if not _ESCAPE_TOKEN.fullmatch(val):
                spans.append((len(out), len(out)))
            out.append(_plain(val))
    out.append(_ESC_TEXT.sub(lambda x: "\\" + x.group(0), t[pos:]))
    while opened:
        k = opened.pop()
        spans.append((open_at[k], len(out)))
        out.append(_plain(imap[k][1]))
    return _guard_boundaries(out, spans)


# #695: docutils recognises inline markup only at a boundary: the start-string must follow
# whitespace, an opening/delimiting punctuation character or the start of the text, the
# end-string must precede whitespace, closing/delimiting punctuation or the end (rST spec,
# "Inline markup recognition rules"). Japanese and Chinese have no spaces, so a translation
# glues the markup to a letter ("日本語**強調**です") and docutils prints the asterisks. An
# escaped space ("\ ", removed from the output) is the spec's way to put markup inside a word.
_ESCAPE_TOKEN = re.compile(r"\\.", re.DOTALL)
_RST_BEFORE_OK = frozenset("-:/'\"<([{")
_RST_AFTER_OK = frozenset("-.,:;!?\\/'\")]}>")
_RST_BEFORE_CATS = frozenset({"Ps", "Pi", "Pf", "Pd", "Po"})
_RST_AFTER_CATS = frozenset({"Pe", "Pi", "Pf", "Pd", "Po"})


def _boundary_ok(c: str, ok: frozenset, cats: frozenset) -> bool:
    return not c or c.isspace() or c in ok or c == "\x00" or unicodedata.category(c) in cats


def _guard_boundaries(out: list[str], spans: list[tuple[int, int]]) -> str:
    if not spans:
        return "".join(out)
    ends = []
    n = 0
    for piece in out:
        n += len(piece)
        ends.append(n)
    joined = "".join(out)
    before_esc: set[int] = set()
    after_esc: set[int] = set()
    for a, b in spans:
        start, end = ends[a] - len(out[a]), ends[b]
        if start == end:
            continue
        before = joined[start - 1] if start > 0 else ""
        after = joined[end] if end < len(joined) else ""
        if not _boundary_ok(before, _RST_BEFORE_OK, _RST_BEFORE_CATS):
            before_esc.add(a)
        if not _boundary_ok(after, _RST_AFTER_OK, _RST_AFTER_CATS):
            after_esc.add(b)
    for i in before_esc:
        out[i] = "\\ " + out[i]
    for i in after_esc:
        out[i] = out[i] + "\\ "
    return "".join(out)


# --------------------------------------------------------------------------- block walk


class _Walk:
    def __init__(self, text: str):
        self.text = text
        self.slots: list[dict] = []
        self.meta: list[dict] = []  # #733: `.. meta::` description / keywords, appended
        self.skipped: list[dict] = []
        self.count = 0

    def line_no(self, off: int) -> int:
        return self.text.count("\n", 0, off) + 1

    def span(self, start: int, end: int, kind: str = "span", **extra) -> None:
        t = self.text
        while start < end and t[start].isspace():
            start += 1
        while end > start and t[end - 1].isspace():
            end -= 1
        if start < end:
            self.slots.append({"kind": kind, "start": start, "end": end, **extra})
            self.count += 1
            check_segment_cap(self.count)

    # -- the walk --------------------------------------------------------------------
    def walk(self, vl, depth: int = 0) -> None:
        if depth > _MAX_DEPTH:
            return  # deeper nesting stays untranslated
        i, n = 0, len(vl)
        literal_next = False
        ends: list = []  # simple-table end borders, computed once per level
        while i < n:
            off, s = vl[i]
            if _blank(s):
                i += 1
                continue
            ind = _indent(s)
            if literal_next:
                literal_next = False
                if ind > 0:
                    i = _end_indented(vl, i)
                    continue
                if s[0] in _PUNCT and not _is_adorn(s):
                    ch = s[0]
                    while i < n and not _blank(vl[i][1]) and vl[i][1][:1] == ch:
                        i += 1
                    continue
            if ind > 0:  # block quote
                j = _end_indented(vl, i)
                self.walk(_dedent(vl[i:j]), depth + 1)
                i = j
                continue
            if s.startswith("..") and (len(s.rstrip()) == 2 or s[2] in " \t"):
                i = self.explicit(vl, i, depth)
                continue
            if s.startswith("__ ") or s.rstrip() == "__":
                i = _end_indented(vl, i + 1)
                continue
            if _GRID_TOP.fullmatch(s):
                j = i
                while j < n and not _blank(vl[j][1]):
                    j += 1
                self.grid_table(vl[i:j])
                i = j
                continue
            if _SIMPLE_BORDER.fullmatch(s):
                i = self.simple_table(vl, i, ends)
                continue
            # overline + title + underline
            if (
                _is_adorn(s)
                and i + 2 < n
                and not _blank(vl[i + 1][1])
                and not _is_adorn(vl[i + 1][1])
                and _is_adorn(vl[i + 2][1])
                and vl[i + 2][1][0] == s[0]
            ):
                self.title(vl[i + 1], [vl[i], vl[i + 2]])
                i += 3
                continue
            if _is_adorn(s) and len(s.rstrip()) >= 4:
                i += 1  # transition
                continue
            bm = _BULLET.match(s)
            em = None if bm else _ENUM.match(s)
            if em and em.group(1) and em.group(3) != ")":
                em = None
            if em and em.group(2).isalpha() and i + 1 < n:
                nxt = vl[i + 1][1]
                if not _blank(nxt) and _indent(nxt) == 0 and not _ENUM.match(nxt):
                    em = None  # "A. Einstein was ..." is a paragraph
            lm = bm or em
            if lm:
                j = _end_indented(vl, i + 1)
                k = lm.end()
                body = [(off + k, s[k:]), *_dedent(vl[i + 1 : j])]
                self.walk(body, depth + 1)
                i = j
                continue
            if _FIELD.match(s):
                i = _end_indented(vl, i + 1)
                continue
            if s.startswith(">>>"):
                while i < n and not _blank(vl[i][1]):
                    i += 1
                continue
            om = _OPTION_LIST.match(s)
            if om and (om.group(1).strip() or (i + 1 < n and _indent(vl[i + 1][1]) > 0)):
                while i < n and not _blank(vl[i][1]):
                    i += 1
                i = _end_indented(vl, i) if i < n and _indent(vl[i][1]) else i
                continue
            if _LINE_BLOCK.match(s):
                while i < n and not _blank(vl[i][1]):
                    o2, s2 = vl[i]
                    lb = _LINE_BLOCK.match(s2)
                    k = lb.end() if lb else 0
                    self.span(o2 + k, o2 + len(s2))
                    i += 1
                continue
            # a single line underlined: a section title
            if i + 1 < n and _is_adorn(vl[i + 1][1]) and not _is_adorn(s):
                ul = vl[i + 1][1].rstrip()
                if len(ul) >= 4 or len(ul) >= column_width(s.rstrip()):
                    self.title(vl[i], [vl[i + 1]])
                    i += 2
                    continue
            # paragraph
            j = i
            while j < n and not _blank(vl[j][1]) and _indent(vl[j][1]) == 0:
                j += 1
            if j < n and not _blank(vl[j][1]) and j == i + 1:  # definition list item
                self.span(off, off + len(s))
                k = _end_indented(vl, j)
                self.walk(_dedent(vl[j:k]), depth + 1)
                i = k
                continue
            last_off, last = vl[j - 1]
            end = last_off + len(last.rstrip())
            if last.rstrip().endswith("::"):
                literal_next = True
                end -= 2
            self.span(off, end, "para")
            i = j

    def title(self, tline, adorn_lines) -> None:
        off, s = tline
        before = len(self.slots)
        self.span(off, off + len(s), "title")
        if len(self.slots) > before:
            slot = self.slots[-1]
            slot["width"] = column_width(self.text[slot["start"] : slot["end"]])
            slot["adorn"] = [(o, o + len(a.rstrip()), a.rstrip()[0]) for o, a in adorn_lines]

    def explicit(self, vl, i: int, depth: int) -> int:
        off, s = vl[i]
        j = _end_indented(vl, i + 1)
        rest = s[3:] if len(s) > 2 else ""
        base = off + 3
        if _blank(rest) and (i + 1 >= len(vl) or _blank(vl[i + 1][1])):
            return i + 1  # empty comment: ends the construct before it, consumes nothing
        fm = _FOOTNOTE.match(rest)
        if fm:
            body = [(base + fm.end(), rest[fm.end() :]), *_dedent(vl[i + 1 : j])]
            self.walk(body, depth + 1)
            return j
        if rest.startswith("|") or rest.startswith("_"):
            return j  # substitution definition, hyperlink target
        dm = _DIRECTIVE.match(rest)
        if not dm:
            return j  # comment
        name = dm.group(1).lower()
        arg = (base + dm.end(), rest[dm.end() :])
        follow = _dedent(vl[i + 1 : j])
        hdr: list = []
        k = 0
        while k < len(follow) and not _blank(follow[k][1]):
            hdr.append(follow[k])
            k += 1
        after = follow[k:]
        non_opt = [ln for ln in hdr if not _OPTION_LINE.match(ln[1])]
        if name in _ADMONITIONS or name in _VERSIONED:
            first = arg
            if name in _VERSIONED:
                vm = re.match(r"\S+[ \t]*", arg[1])
                cut = vm.end() if vm else 0
                first = (arg[0] + cut, arg[1][cut:])
            content = ([first] if not _blank(first[1]) else []) + non_opt
            if content and after:
                content.append((after[0][0], ""))
            self.walk(_dedent(content[:1]) + content[1:] + after, depth + 1)
        elif name in _TITLED:
            if not _blank(arg[1]):
                ends = [arg, *non_opt]
                self.span(arg[0], ends[-1][0] + len(ends[-1][1]))
            if name not in ("rubric", "centered"):
                self.walk(after, depth + 1)
        elif name in _CONTAINERS or ":" in name:
            self.walk(after, depth + 1)
        elif name == "meta":
            self.meta_fields(follow)
        return j  # every other directive (code, include, raw, math, image, ...) is skipped

    def meta_fields(self, vl) -> None:
        """#733: the description / keywords field values of a `.. meta::` directive (its
        dedented content lines). A value may start on the field line or the next one and
        continue on more-indented lines; the slot spans all of them (written back on one
        line). Kept apart from `slots` so the units are numbered after every body unit."""
        t = self.text
        i = 0
        while i < len(vl):
            off, s = vl[i]
            i += 1
            fm = _META_FIELD.match(s) if not _blank(s) and _indent(s) == 0 else None
            if not fm:
                continue
            parts = [(off + fm.end(), s[fm.end() :])] if not _blank(s[fm.end() :]) else []
            while i < len(vl) and not _blank(vl[i][1]) and _indent(vl[i][1]) > 0:
                parts.append(vl[i])
                i += 1
            if not parts:
                continue
            start, end = parts[0][0], parts[-1][0] + len(parts[-1][1])
            while start < end and t[start].isspace():
                start += 1
            while end > start and t[end - 1].isspace():
                end -= 1
            if start < end:
                style = "meta:" + fm.group(1).lower()
                self.meta.append({"kind": "para", "start": start, "end": end, "style": style})
                self.count += 1
                check_segment_cap(self.count)

    # -- grid tables -----------------------------------------------------------------
    def grid_table(self, vl) -> None:
        g = _Grid(vl)
        try:
            ok = g.parse()
        except _GridTooComplex:
            ok = False
        g.steps = -(10**12)  # rendering probes are bounded by the cells already found
        if not ok:
            self.skipped.append({"line": self.line_no(vl[0][0]), "reason": "grid table layout"})
            return
        cells = []
        for c in g.cells:
            raw = g.cell_text(c)
            if raw is None:
                cells.append({"rect": c, "raw": None})
            else:
                cells.append({"rect": c, "raw": raw})
        self.count += len(cells)
        check_segment_cap(self.count)
        self.slots.append(
            {
                "kind": "grid",
                "start": vl[0][0],
                "end": vl[-1][0] + len(vl[-1][1]),
                "grid": g,
                "cells": cells,
                "line": self.line_no(vl[0][0]),
            }
        )

    # -- simple tables ---------------------------------------------------------------
    def simple_table(self, vl, i: int, ends: list) -> int:
        n = len(vl)
        if ends is None or len(ends) != n + 1:
            ends[:] = _simple_ends(vl)
        end = ends[i + 1] + 1 if i + 1 < n and ends[i + 1] < n else None
        if end is None:  # not a table: skip to the next blank line
            while i < n and not _blank(vl[i][1]):
                i += 1
            return i
        t = _Simple(vl[i:end])
        line = self.line_no(vl[i][0])
        if not t.parse():
            self.skipped.append({"line": line, "reason": t.reason or "simple table layout"})
            return end
        self.count += len(t.cells)
        check_segment_cap(self.count)
        self.slots.append(
            {
                "kind": "simple",
                "start": vl[i][0],
                "end": vl[end - 1][0] + len(vl[end - 1][1]),
                "grid": t,
                "cells": [{"rect": c, "raw": c["raw"]} for c in t.cells],
                "line": line,
            }
        )
        return end


# --------------------------------------------------------------------------- table grids


def _simple_ends(vl) -> list[int]:
    """ends[j]: the first line k >= j that is a simple-table border followed by a blank
    line or the end (n when there is none). One backward pass, so a file of many
    unclosed simple-table borders costs linear time."""
    n = len(vl)
    out = [n] * (n + 1)
    for j in range(n - 1, -1, -1):
        closes = _SIMPLE_BORDER.fullmatch(vl[j][1]) and (j + 1 >= n or _blank(vl[j + 1][1]))
        out[j] = j if closes else out[j + 1]
    return out


def _entries(s: str) -> list[str]:
    """One entry per display column: a wide character is followed by a "" pad entry
    (docutils pads double-width characters the same way before scanning a table);
    combining marks stay with their base character."""
    out: list[str] = []
    for ch in s:
        w = _cw(ch)
        if w == 0 and out:
            out[-1] += ch
            continue
        out.append(ch)
        if w == 2:
            out.append("")
    return out


def _wrap(text: str, width: int) -> list[str] | None:
    """Greedy wrap by display width; a word wider than `width` breaks only after a wide
    character (CJK). None when something still cannot fit."""
    words: list[str] = []
    for w in text.split():
        if column_width(w) <= width:
            words.append(w)
            continue
        piece = ""
        for ch in w:
            if column_width(piece + ch) > width:
                if not piece or _cw(piece[-1]) != 2:
                    return None
                words.append(piece)
                piece = ""
            piece += ch
        words.append(piece)
    lines: list[str] = []
    cur, cw = "", 0
    for w in words:
        ww = column_width(w)
        if cur and cw + 1 + ww <= width:
            cur += " " + w
            cw += 1 + ww
        elif not cur:
            cur, cw = w, ww
        else:
            lines.append(cur)
            cur, cw = w, ww
    lines.append(cur)
    return lines


def _cell_ok(lines: list[str]) -> bool:
    """A cell maps to one unit only when it is one plain paragraph."""
    nonblank = [ln for ln in lines if ln.strip()]
    if not nonblank:
        return True
    first = next(k for k, ln in enumerate(lines) if ln.strip())
    last = max(k for k, ln in enumerate(lines) if ln.strip())
    if any(not ln.strip() for ln in lines[first:last]):
        return False
    s = nonblank[0].strip()
    return not (_BULLET.match(s) or _ENUM.match(s) or s.startswith("..") or _GRID_TOP.match(s))


_GRID_MAX_AREA = 400_000  # display cells (rows x columns) of one grid table
_GRID_MAX_STEPS = 4_000_000  # character probes while scanning one grid table


class _GridTooComplex(Exception):
    pass


class _Grid:
    def __init__(self, vl):
        self.vl = vl
        self.rows = [_entries(s.rstrip()) for _, s in vl]
        self.width = max(len(r) for r in self.rows)
        for r in self.rows:
            r.extend(" " * (self.width - len(r)))
        self.cells: list[tuple] = []
        self.steps = 0

    def at(self, r: int, c: int) -> str:
        self.steps += 1
        if self.steps > _GRID_MAX_STEPS:
            raise _GridTooComplex
        e = self.rows[r][c]
        return "-" if e == "=" else e

    def parse(self) -> bool:
        h, w = len(self.rows), self.width
        if h < 3 or self.at(0, 0) != "+" or self.at(h - 1, 0) != "+" or h * w > _GRID_MAX_AREA:
            return False
        self.steps = 0
        corners = [(0, 0)]
        done: set = set()
        while corners:
            top, left = corners.pop(0)
            if (top, left) in done or top >= h - 1 or left >= w - 1:
                continue
            done.add((top, left))
            rect = self.scan(top, left)
            if rect is None:
                continue
            bottom, right = rect
            self.cells.append((top, left, bottom, right))
            corners.extend([(top, right), (bottom, left)])
            corners.sort()
        if not self.cells:
            return False
        # every column of every row must be covered by exactly the found cells
        covered = [[False] * w for _ in range(h)]
        for t, lf, b, rt in self.cells:
            for r in range(t, b + 1):
                for c in range(lf, rt + 1):
                    covered[r][c] = True
        right_edge = max(c for c in range(w) if self.rows[0][c] == "+")
        if any(not covered[r][c] for r in range(h) for c in range(right_edge + 1)):
            return False
        self.cells.sort()
        return True

    def scan(self, top: int, left: int):
        w = self.width
        for right in range(left + 1, w):
            e = self.at(top, right)
            if e == "+":
                bottom = self._down(top, left, right)
                if bottom is not None:
                    return bottom, right
            elif e != "-":
                return None
        return None

    def _down(self, top: int, left: int, right: int):
        for b in range(top + 1, len(self.rows)):
            e = self.at(b, right)
            if e == "+":
                if self._closed(top, left, b, right):
                    return b
            elif e != "|":
                return None
        return None

    def _closed(self, top: int, left: int, bottom: int, right: int) -> bool:
        if self.at(bottom, left) != "+":
            return False
        if any(self.at(bottom, c) not in "-+" for c in range(left + 1, right)):
            return False
        return all(self.at(r, left) in "|+" for r in range(top + 1, bottom))

    def interior(self, rect) -> list[str]:
        t, lf, b, rt = rect
        return ["".join(self.rows[r][lf + 1 : rt]) for r in range(t + 1, b)]

    def cell_text(self, rect):
        lines = self.interior(rect)
        if not _cell_ok(lines):
            return None
        return "\n".join(ln.strip() for ln in lines if ln.strip())

    def render(self, new_cells: dict, slot_line: int) -> str:
        """new_cells: rect -> rendered one-line text. Returns the rebuilt table text."""
        growth: dict[int, int] = {}
        wrapped: dict = {}
        for rect, txt in new_cells.items():
            t, lf, b, rt = rect
            inner = rt - lf - 1
            width = max(1, inner - 2)
            lines = _wrap(txt, width) if txt.strip() else [""]
            if lines is None:
                raise _fit_error(slot_line + t + 1, txt, width)
            extra = len(lines) - (b - t - 1)
            if extra > 0:
                anchor = b - 1
                edges = {x for cell in self.cells for x in (cell[1], cell[3])}
                if any(self.at(anchor, c) == "+" for c in edges):
                    raise _fit_error(slot_line + t + 1, txt, width, grow=True)
                growth[anchor] = max(growth.get(anchor, 0), extra)
            wrapped[rect] = lines
        rows: list[list[str]] = []
        orig_of: list[int | None] = []
        newpos: dict[int, int] = {}
        for r, row in enumerate(self.rows):
            newpos[r] = len(rows)
            rows.append(list(row))
            orig_of.append(r)
            if growth.get(r):
                frame = list(row)
                for t, lf, b, rt in self.cells:
                    if t < r < b:
                        for c in range(lf + 1, rt):
                            frame[c] = " "
                for _ in range(growth[r]):
                    rows.append(list(frame))
                    orig_of.append(None)
        for rect, lines in wrapped.items():
            t, lf, b, rt = rect
            inner = rt - lf - 1
            targets = range(newpos[t] + 1, newpos[b])
            for k, nr in enumerate(targets):
                content = (" " + lines[k]) if k < len(lines) and lines[k] else ""
                ent = _entries(content)
                ent.extend(" " * (inner - len(ent)))
                rows[nr][lf + 1 : rt] = ent[:inner]
        out = []
        for nr, row in enumerate(rows):
            r = orig_of[nr]
            if r is not None and row == self.rows[r]:
                out.append(self.vl[r][1])
            else:
                out.append("".join(row).rstrip())
        return out


class _Simple:
    """A simple table: columns from the top border's ``=`` runs; the last column's text
    may run past its border. Rows start where the first column has text."""

    def __init__(self, vl):
        self.vl = vl
        self.rows = [_entries(s.rstrip()) for _, s in vl]
        self.cols: list[tuple[int, int]] = []
        self.cells: list[dict] = []
        self.reason = ""

    def parse(self) -> bool:
        top = self.rows[0]
        c = 0
        while c < len(top):
            if top[c] == "=":
                s = c
                while c < len(top) and top[c] == "=":
                    c += 1
                self.cols.append((s, c))
            else:
                c += 1
        if len(self.cols) < 2:
            return False
        groups: list[list[int]] = []
        spans: dict[int, list[tuple[int, int]]] = {}
        cur: list[int] | None = None
        for r in range(1, len(self.rows)):
            s = self.vl[r][1]
            if _blank(s) or _SIMPLE_BORDER.fullmatch(s):
                cur = None
                continue
            if _SIMPLE_SPAN.fullmatch(s):
                if groups:
                    spans[len(groups) - 1] = self._runs(self.rows[r])
                cur = None
                continue
            a, b = self.cols[0]
            first_blank = not "".join(self.rows[r][a:b]).strip()
            if first_blank and cur is not None:
                cur.append(r)
            else:
                cur = [r]
                groups.append(cur)
        for gi, rows in enumerate(groups):
            ranges = spans.get(gi, self.cols)
            ranges = [(a, b) for a, b in ranges]
            ranges[-1] = (ranges[-1][0], None)
            # text between ranges is a span the walk cannot place
            for r in rows:
                row = self.rows[r]
                pos = 0
                for a, b in ranges:
                    if "".join(row[pos:a]).strip():
                        self.reason = "text across a simple table column gap"
                        return False
                    pos = b if b is not None else len(row)
            for ci, (a, b) in enumerate(ranges):
                lines = [
                    "".join(self.rows[r][a:b] if b is not None else self.rows[r][a:]) for r in rows
                ]
                if not _cell_ok(lines):
                    raw = None
                else:
                    raw = "\n".join(ln.strip() for ln in lines if ln.strip())
                self.cells.append({"rows": rows, "a": a, "b": b, "first": ci == 0, "raw": raw})
        return True

    @staticmethod
    def _runs(row) -> list[tuple[int, int]]:
        out, c = [], 0
        while c < len(row):
            if row[c] == "-":
                s = c
                while c < len(row) and row[c] == "-":
                    c += 1
                out.append((s, c))
            else:
                c += 1
        return out

    def render(self, new_cells: dict, slot_line: int) -> list[str]:
        rows = [list(r) for r in self.rows]
        inserts: dict[int, int] = {}  # row index -> extra continuation lines after it
        plan = []
        for ci, txt in new_cells.items():
            cell = self.cells[ci]
            a, b = cell["a"], cell["b"]
            line = slot_line + cell["rows"][0]
            if b is None:
                lines = [txt]  # the last column may run past its border
            else:
                lines = _wrap(txt, b - a)
                if lines is None or (cell["first"] and len(lines) > 1):
                    raise _fit_error(line, txt, b - a, first=cell["first"])
            extra = len(lines) - len(cell["rows"])
            if extra > 0:
                last = cell["rows"][-1]
                inserts[last] = max(inserts.get(last, 0), extra)
            plan.append((cell, lines))
        # new layout: original rows plus blank continuation rows after a group's last row
        layout: list[tuple[int | None, list[str]]] = []
        index_of: dict[int, int] = {}
        for r, row in enumerate(rows):
            index_of[r] = len(layout)
            layout.append((r, row))
            for _ in range(inserts.get(r, 0)):
                layout.append((None, []))
        for cell, lines in plan:
            a, b = cell["a"], cell["b"]
            first = index_of[cell["rows"][0]]
            last = index_of[cell["rows"][-1]]
            while last + 1 < len(layout) and layout[last + 1][0] is None:
                last += 1
            for k, li in enumerate(range(first, last + 1)):
                row = layout[li][1]
                txt = lines[k] if k < len(lines) else ""
                ent = _entries(txt)
                if b is None:
                    row[a:] = []
                    row.extend(" " * (a - len(row)))
                    row.extend(ent)
                else:
                    row.extend(" " * (b - len(row)))
                    ent.extend(" " * (b - a - len(ent)))
                    row[a:b] = ent
        out = []
        for r, row in layout:
            if r is not None and row == self.rows[r]:
                out.append(self.vl[r][1])
                continue
            line = "".join(row).rstrip()
            if not line and (r is None or not _blank(self.vl[r][1])):
                continue  # a continuation line the translation no longer needs
            out.append(line)
        return out


def _fit_error(line: int, txt: str, width: int, grow: bool = False, first: bool = False):
    short = txt if len(txt) <= 40 else txt[:37] + "..."
    if first:
        why = f"the first column of a simple table holds one line of {width} columns"
    elif grow:
        why = "the row cannot grow (it shares its last line with a spanning cell)"
    else:
        why = f"a word is wider than the column ({width} columns)"
    return TableFitError(
        f"reStructuredText table at line {line}: the translation '{short}' does not fit its "
        f"cell: {why}. Column widths are kept as in the source; shorten the translation or "
        "widen the column in the source file."
    )


# --------------------------------------------------------------------------- filter


def _decode(data: bytes) -> tuple[str, str]:
    return read_text_upload(data, sig=True)  # #509 refusal + #571 line budget


def _walk(text: str) -> _Walk:
    w = _Walk(text)
    w.walk(list(_lines(text)))
    return w


def _translatable(unit: str) -> bool:
    return any(c.isalpha() for c in strip_placeholders(unit))


def _units(w: _Walk):
    """(slot, cell index or None, unit text, token map) for every translatable unit."""
    for slot in w.slots:
        if slot["kind"] in ("grid", "simple"):
            for ci, cell in enumerate(slot["cells"]):
                if cell["raw"] is None:
                    continue
                unit, imap = _unit_of(cell["raw"])
                if _translatable(unit):
                    yield slot, ci, unit, imap
        else:
            unit, imap = _unit_of(w.text[slot["start"] : slot["end"]])
            if _translatable(unit):
                yield slot, None, unit, imap
    for slot in w.meta:  # #733: page metadata, after every body unit
        unit, imap = _unit_of(w.text[slot["start"] : slot["end"]])
        if _translatable(unit):
            yield slot, None, unit, imap


@register
class RstFilter(Filter):
    fmt = "rst"
    exts = ("rest",)

    def parse(self, data: bytes, provider: str | None = None) -> ParseResult:
        text, enc = _decode(data)
        w = _walk(text)
        units: list[TextUnit] = []
        for slot, _ci, unit, imap in _units(w):
            units.append(
                TextUnit(
                    p_index=len(units), text=unit, inline_map=imap, style=slot.get("style", "")
                )
            )
        check_unit_texts(u.text for u in units)
        skeleton = {
            "format": "rst",
            "units": [{"p_index": u.p_index, "text": u.text, "style": u.style} for u in units],
        }
        if w.skipped:
            skeleton["rst_skipped_tables"] = w.skipped
        return ParseResult(skeleton=note_source_encoding(skeleton, enc), units=units)

    def build(self, original: bytes, paragraph_texts: dict) -> bytes:
        raw = bytes(original)
        text = decode_text_lossless(raw, sig=True)
        w = _walk(text)
        edits: list[tuple[int, int, str]] = []
        table_new: dict[int, dict] = {}
        for k, (slot, ci, unit, imap) in enumerate(_units(w)):
            new = paragraph_texts.get(str(k))
            if new is None or new.strip() == unit:
                continue
            rendered = _render(new, imap)
            if ci is not None:
                table_new.setdefault(id(slot), {"slot": slot, "cells": {}})
                key = slot["cells"][ci]["rect"] if slot["kind"] == "grid" else ci
                table_new[id(slot)]["cells"][key] = rendered
                continue
            if (
                slot["kind"] == "para"
                and not new.lstrip().startswith("\u27e6")
                and _PARA_START.match(rendered)
            ):
                rendered = "\\" + rendered
            edits.append((slot["start"], slot["end"], rendered))
            if slot["kind"] == "title":
                delta = column_width(rendered) - slot["width"]
                new_w = column_width(rendered)
                for a, b, ch in slot["adorn"]:
                    length = max(new_w, (b - a) + delta)
                    edits.append((a, b, ch * length))
        for entry in table_new.values():
            slot = entry["slot"]
            lines = slot["grid"].render(entry["cells"], slot["line"])
            nl = _newline(text, slot["start"])
            edits.append((slot["start"], slot["end"], nl.join(lines)))
        edits.sort()
        out: list[str] = []
        pos = 0
        for a, b, rep in edits:
            out.append(text[pos:a])
            out.append(rep)
            pos = b
        out.append(text[pos:])
        body = encode_text_lossless("".join(out))
        return _BOM + body if raw.startswith(_BOM) and not body.startswith(_BOM) else body


def _newline(text: str, at: int) -> str:
    m = re.compile(r"\r\n|\r|\n").search(text, at)
    return m.group(0) if m else "\n"


__all__ = ["RstFilter", "TableFitError", "column_width"]
