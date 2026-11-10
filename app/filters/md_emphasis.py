"""CommonMark emphasis next to CJK text (#695, Markdown and MDX).

Markdown and MDX keep emphasis marks as text: the engine writes ``*``, ``_``, ``**`` and
``__`` into the translation itself. CommonMark only reads a delimiter run as emphasis when
it is "flanking" (spec 0.31, 6.2): a ``_`` run between two letters never opens or closes,
and a ``**`` whose inner text starts or ends with punctuation must have whitespace or
punctuation on its outer side. Japanese and Chinese have no spaces, so
``__詳細__オプション`` and ``**「変更を保存」**をクリック`` render with literal marks (live
ja-JP run: 5 of 6 emphasised lines).

``fix_cjk_emphasis`` repairs only pairs the translation wrote and CommonMark would not
read, and only when the source unit had emphasis of that kind: ``_`` / ``__`` becomes
``*`` / ``**`` (no intraword rule), and if that is not enough the punctuation at the
inner edges moves outside the marks (``「**変更を保存**」``). A pair that still would not
parse is left as written. Code spans, autolinks, HTML tags, link destinations and
backslash escapes are never touched; a translation without the source's delimiter kinds
is returned unchanged.
"""

from __future__ import annotations

import re
import unicodedata

_RUN = re.compile(r"\*+|_+")
# Regions whose characters are never delimiters: code spans (matched by equal backtick
# runs), autolinks / HTML tags, link and image destinations, inline-fidelity tokens.
_PROTECT = re.compile(
    r"(?P<tick>`+)[\s\S]*?(?<!`)(?P=tick)(?!`)"
    r"|<[^<>\s][^<>]*>"
    r"|\]\([^()\s]*(?:\s+\"[^\"]*\")?\)"
    r"|⟦/?[a-z]?\d+⟧"
)


def _ws(c: str) -> bool:
    return c == "" or c.isspace()


def _punct(c: str) -> bool:
    if not c:
        return False
    if c.isascii():
        return not c.isalnum() and not c.isspace()
    return unicodedata.category(c)[0] in "PS"


def _ascii_alnum(c: str) -> bool:
    return c.isascii() and c.isalnum()


def _flanks(s: str, a: int, b: int) -> tuple[bool, bool]:
    """(left-flanking, right-flanking) of the delimiter run s[a:b] (CommonMark 6.2)."""
    before = s[a - 1] if a > 0 else ""
    after = s[b] if b < len(s) else ""
    left = not _ws(after) and (not _punct(after) or _ws(before) or _punct(before))
    right = not _ws(before) and (not _punct(before) or _ws(after) or _punct(after))
    return left, right


def _can_open(s: str, a: int, b: int) -> bool:
    left, right = _flanks(s, a, b)
    if s[a] == "*":
        return left
    before = s[a - 1] if a > 0 else ""
    return left and (not right or _punct(before))


def _can_close(s: str, a: int, b: int) -> bool:
    left, right = _flanks(s, a, b)
    if s[a] == "*":
        return right
    after = s[b] if b < len(s) else ""
    return right and (not left or _punct(after))


def _runs(s: str) -> list[tuple[int, int]]:
    """Delimiter runs of one or two marks outside protected regions, unescaped, and not
    an identifier underscore (``my_var``: ASCII letters or digits on both sides)."""
    masked = [False] * len(s)
    for m in _PROTECT.finditer(s):
        for i in range(m.start(), m.end()):
            masked[i] = True
    out = []
    for m in _RUN.finditer(s):
        a, b = m.span()
        if b - a > 2 or any(masked[a:b]):
            continue
        k = a
        while k > 0 and s[k - 1] == "\\":
            k -= 1
        if (a - k) % 2:
            continue  # escaped
        before = s[a - 1] if a > 0 else ""
        after = s[b] if b < len(s) else ""
        if s[a] == "_" and _ascii_alnum(before) and _ascii_alnum(after):
            continue
        if _ws(before) and _ws(after):
            continue  # a lone mark between spaces is text
        out.append((a, b))
    return out


def _pairs(s: str) -> dict[str, list[tuple[tuple[int, int], tuple[int, int]]]]:
    """The translation's delimiter runs per kind ("*", "**", "_", "__"), paired in order."""
    kinds: dict[str, list[tuple[int, int]]] = {}
    for a, b in _runs(s):
        kinds.setdefault(s[a:b], []).append((a, b))
    return {
        k: [(v[i], v[i + 1]) for i in range(0, len(v) - 1, 2)]
        for k, v in kinds.items()
        if len(v) % 2 == 0
    }


def _source_kinds(src: str) -> dict[str, int]:
    """Emphasis pairs per kind that CommonMark reads in the source unit."""
    out: dict[str, int] = {}
    for kind, pairs in _pairs(src).items():
        n = sum(1 for o, c in pairs if _can_open(src, *o) and _can_close(src, *c))
        if n:
            out[kind] = n
    return out


def _try(s: list[str], o: tuple[int, int], c: tuple[int, int], mark: str, move: bool) -> bool:
    """Rewrite the pair in `s` (a list of characters) in place with ``mark`` (and the inner edge
    punctuation moved outside when ``move``); True when CommonMark then reads it. When it does
    not, `s` is left as it was. The length never changes, so a long line with many pairs is
    fixed in linear time (#813.4; the old version rebuilt the whole string for every pair)."""
    inner = s[o[1] : c[0]]
    i, j = 0, len(inner)
    if move:
        while i < j and _punct(inner[i]) and inner[i] not in "*_\\`":
            i += 1
        while j > i and _punct(inner[j - 1]) and inner[j - 1] not in "*_\\`":
            j -= 1
        core = inner[i:j]
        if not core or core[0].isspace() or core[-1].isspace():
            return False
    old = s[o[0] : c[1]]
    s[o[0] : c[1]] = inner[:i] + list(mark) + inner[i:j] + list(mark) + inner[j:]
    oa = o[0] + i
    ca = oa + len(mark) + (j - i)
    if _can_open(s, oa, oa + len(mark)) and _can_close(s, ca, ca + len(mark)):
        return True
    s[o[0] : c[1]] = old
    return False


def fix_cjk_emphasis(src: str, new: str) -> str:
    """Rewrite emphasis pairs in a Markdown translation that CommonMark would print as
    literal marks (see the module docstring). Unchanged when nothing needs it."""
    if not new or ("*" not in new and "_" not in new):
        return new
    wanted = _source_kinds(src)
    if not wanted:
        return new
    pairs = _pairs(new)
    todo = []
    for kind, lst in pairs.items():
        if kind not in wanted:
            continue
        for o, c in lst:
            if not (_can_open(new, *o) and _can_close(new, *c)):
                todo.append((o, c, kind))
    chars = list(new)
    for o, c, kind in sorted(todo, reverse=True):  # right to left: earlier offsets stay valid
        star = "*" * len(kind)
        if not (kind[0] == "_" and _try(chars, o, c, star, False)):
            _try(chars, o, c, star, True)
    return "".join(chars)


# #700: GitHub-flavoured Markdown links a bare URL (extended autolink) only at the line start,
# after whitespace or after * _ ~ (, and the link runs over every following non-space
# character except a trailing ASCII ?!.,:*_~. A Chinese/Japanese translation puts CJK
# punctuation right next to the URL ("project，并镜像在" joined the link; "、https://..." was
# not linked at all). An explicit autolink ends where it says and works in any position.
_BARE_URL = re.compile(r"(?:https?://|www\.)[^\s<>]+")
_URL_TRAIL = "?!.,:*_~"
_URL_RUN = re.compile(r"[^\s<]*")


# Whole inline / reference links (text included): a URL used as link text is already a link
# and must never be wrapped again (gate: "[https://x](https://x)" became "[<https://x](...)>").
_LINKS = re.compile(r"!?\[[^\[\]]*\](?:\([^()\s]*(?:\s+\"[^\"]*\")?\)|\[[^\[\]]*\])")


def _masked_spans(s: str) -> list[tuple[int, int]]:
    return [m.span() for m in _PROTECT.finditer(s)] + [m.span() for m in _LINKS.finditer(s)]


def _inside(spans: list[tuple[int, int]], a: int) -> bool:
    return any(x <= a < y for x, y in spans)


def _glued_before(s: str, a: int) -> bool:
    """True when the character before a URL at `a` is neither a blank nor a mark that may
    open a URL."""
    before = s[a - 1] if a else ""
    return bool(before) and not before.isspace() and before not in "*_~("


# Work budget for the repair (#813.4): one `find` per distinct source URL over the whole
# translation is
# urls x length steps; past this the repair is skipped (the unit keeps what the translator wrote).
_AUTOLINK_BUDGET = 1 << 26


def _source_urls(src: str) -> set[str]:
    spans = _masked_spans(src)
    out = set()
    glued_in_source = set()
    for m in _BARE_URL.finditer(src):
        if _inside(spans, m.start()):
            continue
        url = m.group(0).rstrip(_URL_TRAIL)
        if url.endswith(")") and url.count("(") < url.count(")"):
            url = url[:-1]
        # a URL the source itself glues to its neighbours (a link text, a CJK sentence) is
        # not repaired: the
        # glue is the author's, and wrapping it would rewrite link text (#813.4)
        end = m.start() + len(url)
        if _glued_before(src, m.start()) or any(
            ch not in _URL_TRAIL and ch != ")" for ch in src[end : m.end()]
        ):
            glued_in_source.add(url)
        out.add(url)
    return out - glued_in_source


def fix_cjk_autolinks(src: str, new: str, mdx: bool = False) -> str:
    """Wrap a bare URL of the source that the translation glued to CJK text so GFM would
    not link it, or would add the next words to it, in an explicit link: ``<url>`` in
    Markdown, ``[url](url)`` in MDX (where ``<`` starts JSX). Unchanged otherwise."""
    if not new or ("://" not in new and "www." not in new):
        return new
    urls = _source_urls(src)
    if not urls or len(urls) * len(new) > _AUTOLINK_BUDGET:
        return new
    spans = _masked_spans(new)
    edits = []
    for url in sorted(urls, key=len, reverse=True):
        start = 0
        while (a := new.find(url, start)) >= 0:
            b = a + len(url)
            start = b
            if _inside(spans, a) or any(x < b and a < y for x, y, _ in edits):
                continue
            run = _URL_RUN.match(new, b).group(0)  # what GFM would keep adding
            if run and run[0].isascii() and run[0] not in _URL_TRAIL:
                continue  # the translation's URL goes on (another path, a ")"): leave it
            glued_before = _glued_before(new, a)
            glued_after = bool(run.rstrip(_URL_TRAIL))
            if glued_before or glued_after:
                if url.startswith("www."):
                    # <www.x> is not an autolink (needs a scheme); GFM links www. to http://
                    edits.append((a, b, f"[{url}](http://{url})"))
                else:
                    edits.append((a, b, f"[{url}]({url})" if mdx else f"<{url}>"))
    for a, b, rep in sorted(edits, reverse=True):
        new = new[:a] + rep + new[b:]
    return new


# #704: a line that starts with one of these is block markup in CommonMark (an ordered list
# item, 1 to 9 digits; an ATX heading; a bullet; a block quote). The source escapes such a
# start (`1984\.`, `\#`) when it is prose; the engine drops the backslash (live de-DE/fr-FR:
# "1984. Das war ..." rendered as <ol start="1984">).
_BLOCK_START = re.compile(
    r"(?P<num>[0-9]{1,9})(?P<delim>[.)])(?=[ \t]|$)"  # ASCII digits only (CommonMark)
    r"|(?P<hash>#{1,6})(?=[ \t]|$)"
    r"|(?P<bullet>[-+*])(?=[ \t]|$)"
    r"|(?P<quote>>)"
    r"|(?P<fence>`{3,}|~{3,})"  # #790 gate: a fence would swallow the rest of the file
)


def _block_start(s: str):
    lead = len(s) - len(s.lstrip(" \t"))
    if lead > 3:
        return None  # four spaces: an indented code line in the source too, not a marker
    return _BLOCK_START.match(s, lead)


def _indent(s: str) -> int:
    """The indent of `s` in columns (a tab goes to the next multiple of four)."""
    w = 0
    for c in s:
        if c == " ":
            w += 1
        elif c == "\t":
            w += 4 - w % 4
        else:
            break
    return w


def escape_block_start(src: str, new: str) -> str:
    """A translated line-start unit must not open a block its source did not: when `new`
    starts with an ordered-list number, ATX heading, bullet or `>` and `src` does not, the
    marker is backslash-escaped (``1984\\.``, ``\\#``, ``\\-``, ``\\>``). Unchanged otherwise.
    #790 (structure fuzz, as Bilingua): four or more columns of indent the source line did
    not have would make the line an indented code block, so the blanks go."""
    new = new or ""
    if _indent(new) >= 4 and _indent(src or "") < 4:
        new = new.lstrip(" \t")
    m = _block_start(new)
    if m is None or _block_start(src or "") is not None:
        return new
    at = m.start("delim") if m.group("num") is not None else m.start()
    return new[:at] + "\\" + new[at:]


# #710: a GFM table cell separator is a `|` not preceded by a backslash.
_CELL_PIPE = re.compile(r"(?<!\\)\|")
# A backslash before a space escapes nothing (CommonMark prints it): `Oui \ Non`.
_LONE_BACKSLASH = re.compile(r"\\(?=[ \t])")


def _restore_dropped_pipes(src: str, new: str) -> str:
    """The other half of #710, seen live (fr-FR on the fix branch): the engine kept the
    backslash and dropped the pipe, ``Yes \\| No`` -> ``Oui \\ Non`` (a literal backslash on
    the page). When the source escapes more pipes than the translation and the translation has
    lone ``\\ `` the source does not, those become ``\\|`` again (left to right, at most the
    missing count)."""
    missing = src.count("\\|") - new.count("\\|")
    extra = len(_LONE_BACKSLASH.findall(new)) - len(_LONE_BACKSLASH.findall(src))
    n = min(missing, extra)
    if n <= 0:
        return new
    return _LONE_BACKSLASH.sub(lambda _m: "\\|", new, count=n)


def restore_cell_escapes(src: str, new: str) -> str:
    """A translated table row keeps the source's ``\\|`` escapes (#710). The engine dropped the
    backslash of ``| Yes \\| No | ... |`` (fr ``| Oui | Non | ... |``): the row gained a cell and
    GFM hid the last one, so the translated text disappeared. When `new` has MORE cells than
    `src` and a source cell held escaped pipes the matching translated cell lost, the cells the
    lost escapes split off are joined back with ``\\|``, left to right (table cells keep their
    order). Unchanged when the cell counts already agree or the source escapes no pipe."""
    if not new or "\\|" not in (src or ""):
        return new
    new = _restore_dropped_pipes(src, new)
    s_cells = _CELL_PIPE.split(src)
    t_cells = _CELL_PIPE.split(new)
    excess = len(t_cells) - len(s_cells)
    if excess <= 0:
        return new
    out: list[str] = []
    ti = 0
    for sc in s_cells:
        if ti >= len(t_cells):
            break
        cell = t_cells[ti]
        ti += 1
        lost = sc.count("\\|") - cell.count("\\|")
        while lost > 0 and excess > 0 and ti < len(t_cells):
            cell += "\\|" + t_cells[ti]
            ti += 1
            lost -= 1
            excess -= 1
        out.append(cell)
    out.extend(t_cells[ti:])
    return "|".join(out)


# #714: a line break (with the spaces around it) inside a translation.
_BREAK = re.compile(r"[ \t]*(?:\r\n|\r|\n)[ \t]*")


def fold_line_breaks(src: str, new: str) -> str:
    """A one-line Markdown unit stays one line (#714): a line break the translation added
    (an engine that breaks a long line, Shift+Enter in the editor) cut a heading in two (the
    rest became a paragraph), split a table row, or opened a block when the next line started
    with a marker. It is written as one space, which is what CommonMark renders for a soft
    break anyway. A source that already spans lines keeps the translation as written."""
    if not new or "\n" in (src or "") or "\r" in (src or ""):
        return new
    return _BREAK.sub(" ", new.strip("\r\n"))


__all__ = [
    "escape_block_start",
    "fix_cjk_autolinks",
    "fix_cjk_emphasis",
    "fold_line_breaks",
    "restore_cell_escapes",
]
