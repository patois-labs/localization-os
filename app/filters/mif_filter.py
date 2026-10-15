"""Adobe FrameMaker MIF filter (format Batch 14): MIF 8 and later (Unicode, UTF-8).

MIF is a nested statement grammar: `<Name args... <Sub ...> >`, strings written
`` `text' `` with backslash escapes, `#` comments to end of line, and inset facet
lines (starting with `=` or `&`) that are opaque data. Body text lives in

    <Para <PgfTag `Body'> <ParaLine <String `Hello '> <Char Tab> <String `world'> > >

and a paragraph that wraps over several ParaLine statements is ONE unit. Every Para
statement in the file is a unit (text flows, table cells, footnotes, master and
reference pages), in file order.

Inside a paragraph:
  * String text and the printable <Char> names (Tab, HardSpace, EmDash, ...) are text;
    <Char HardReturn> is a newline.
  * Variables, cross-references (<XRef ...> through <XRefEnd>, including the computed
    xref text), markers, font changes, conditional-text switches, anchored frames,
    tables and footnote anchors, structure element begin/end and any other statement
    become `⟦xN⟧` point tokens (consecutive ones share a token). Statements before the
    first text and after the last text stay where they are, outside the unit.
  * A font change switched on and later restored in the same paragraph
    (<Font <FTag `Emphasis'>> ... <Font <FTag `'>>, <FWeight `Bold'> ... <FWeight
    `Regular'>, a nested change restored to the outer font) is a PAIRED token
    `⟦gN⟧words⟦/gN⟧` instead (#680), so a translation that reorders the words moves the
    font change with them; a pair with one end inside the text pulls its other end into
    the unit. A font change that is never restored stays a point token.
  * <TextRectID> is line layout: kept at the start of a rebuilt paragraph, dropped
    elsewhere (FrameMaker reflows the text on open).

Build splices only paragraphs whose translation differs into the original text; an
untouched file is byte-identical. Each pair writes the source's own two Font statements
(a pair MT emptied is dropped). A pair the translation does not write whole, or nests
differently from the source (an in-line Font statement returns to the font around it,
so anywhere else it would format the wrong words), loses its font change and keeps its
words. A rebuilt paragraph is one ParaLine (FrameMaker
re-breaks lines), split after each <Char HardReturn>. Every rebuild is re-parsed.
build_localized() also sets every PgfLanguage / FLanguage keyword to the target
language (NoLanguage when FrameMaker has no keyword for it).

MIF 7 and older is not Unicode (FrameMaker character set, \\xNN escapes) and is
refused with a re-save message; so are MIF book files. Spec: Adobe FrameMaker MIF
Reference ("Character set in strings", "ParaLine statement", "Char statement").
"""

import re

from app.filters.base import (
    Filter,
    InputLimitError,
    ParseResult,
    TextUnit,
    UnsupportedEncodingError,
    check_segment_cap,
    check_unit_texts,
    register,
)

_MAX_DEPTH = 256
#: Statements allowed per allowed segment before lexing (MIF is verbose: a body
#: paragraph with an inline Pgf override is easily 100+ statements).
_STATEMENTS_PER_SEGMENT = 200

_HEAD_RE = re.compile(
    rb"^(?:\xef\xbb\xbf)?\s*(?:#[^\n\r]*[\r\n]+\s*)*<\s*([A-Za-z]+)\s+`?([0-9][0-9.]*)"
)

_LEX_RE = re.compile(
    r"""(?P<str>`(?:[^'\\]|\\.)*')
      |(?P<open><[ \t]*[A-Za-z][A-Za-z0-9_]*)
      |(?P<close>>)
      |(?P<comment>\#[^\n\r]*)
      |(?P<ws>\s+)
      |(?P<facet>[=&][^\n\r]*)
      |(?P<atom>[^\s<>`\#']+)""",
    re.X | re.S,
)
_ATOM_RE = re.compile(r"[^\s<>`#']+")

#: Target locale -> FrameMaker language keyword (PgfLanguage / FLanguage). The list is
#: the MIF Reference's PgfLanguage keyword list (FrameMaker 2019+, "Pgf statement").
#: Region-specific entries are checked before the bare language.
_FM_LANGUAGE = {
    "en-gb": "UKEnglish",
    "en": "USEnglish",
    "de-ch": "SwissGerman",
    "de-at": "AustriaGerman",
    "de": "German",
    "fr-ca": "CanadianFrench",
    "fr": "French",
    "es": "Spanish",
    "ca": "Catalan",
    "it": "Italian",
    "pt-br": "Brazilian",
    "pt": "Portuguese",
    "da": "Danish",
    "nl": "Dutch",
    "nb": "Norwegian",
    "no": "Norwegian",
    "nn": "Nynorsk",
    "fi": "Finnish",
    "sv": "Swedish",
    "ja": "Japanese",
    "zh-hant": "TraditionalChinese",
    "zh-tw": "TraditionalChinese",
    "zh-hk": "TraditionalChinese",
    "zh-mo": "TraditionalChinese",
    "zh": "SimplifiedChinese",
    "ko": "Korean",
    "ar": "Arabic",
    "he": "Hebrew",
    "iw": "Hebrew",
}
_LANGUAGE_STATEMENTS = ("PgfLanguage", "FLanguage")


def fm_language(locale: str | None) -> str | None:
    """FrameMaker language keyword for a locale; NoLanguage when FrameMaker has none
    (better no spelling/hyphenation language than the SOURCE one); None for no locale."""
    if not locale:
        return None
    parts = locale.replace("_", "-").lower().split("-")
    for n in range(len(parts), 0, -1):
        hit = _FM_LANGUAGE.get("-".join(parts[:n]))
        if hit:
            return hit
    return "NoLanguage"


#: <Char name> -> text character. Names not listed here (SoftHyphen, DiscHyphen,
#: NoHyphen, unknown names) stay as protected tokens; HardReturn is a newline.
_CHAR_TEXT = {
    "Tab": "\t",
    "HardSpace": " ",
    "HardHyphen": "‑",
    "NumberSpace": " ",
    "ThinSpace": " ",
    "EnSpace": " ",
    "EmSpace": " ",
    "Cent": "¢",
    "Pound": "£",
    "Yen": "¥",
    "EnDash": "–",
    "EmDash": "—",
    "Dagger": "†",
    "DoubleDagger": "‡",
    "Bullet": "•",
    "HardReturn": "\n",
}

_TOKEN_RE = re.compile(r"⟦/?[a-z]?\d+⟧")
_ANY_TOKEN_RE = re.compile(r"⟦(/?)([a-z]?)(\d+)⟧")
#: Font sub-statements that are not font properties (ignored when comparing fonts).
_FONT_IGNORE = frozenset({"FLocked"})


class _Node:
    __slots__ = ("args", "children", "end", "name", "start")

    def __init__(self, name: str, start: int):
        self.name = name
        self.start = start
        self.end = start
        self.args: list[tuple[str, int, int]] = []  # (kind, start, end) of leaf tokens
        self.children: list[_Node] = []


def _check_version(data: bytes) -> str:
    m = _HEAD_RE.match(data[:8192])
    if not m:
        raise ValueError(
            "This file could not be read as FrameMaker MIF: it must start with a "
            "<MIFFile version> statement."
        )
    kind, version = m.group(1).decode("ascii"), m.group(2).decode("ascii")
    if kind != "MIFFile":
        raise ValueError(
            f"This is a FrameMaker MIF {kind} file. Only document MIF files are supported: "
            "upload each chapter's MIF file instead."
        )
    major = int(re.match(r"\d+", version).group(0))
    if major < 8:
        raise UnsupportedEncodingError(
            f"This is a MIF {version} file. MIF 7 and older are not Unicode and are not "
            "supported: open the document in FrameMaker 8 or later and save it as MIF again."
        )
    return version


def _decode(data: bytes) -> tuple[str, bytes]:
    data = bytes(data)
    bom = b""
    if data.startswith(b"\xef\xbb\xbf"):
        bom, data = data[:3], data[3:]
    try:
        return data.decode("utf-8"), bom
    except UnicodeDecodeError as exc:
        raise UnsupportedEncodingError(
            "This MIF file is not valid UTF-8 (byte 0x%02x at position %d). MIF 8 and later "  # noqa: UP031
            "are UTF-8: save it as MIF again from FrameMaker 8 or later."
            % (data[exc.start], exc.start)
        ) from exc


def _parse_tree(text: str) -> _Node:
    """Lex and nest the whole file. Raises ValueError on malformed input and
    InputLimitError over the statement or depth budget."""
    from app import config

    budget = _STATEMENTS_PER_SEGMENT * config.MAX_DOCUMENT_SEGMENTS
    if text.count("<") > budget:
        raise InputLimitError(
            f"This MIF file has more than {budget} statements "
            f"({_STATEMENTS_PER_SEGMENT} x MAX_DOCUMENT_SEGMENTS). Split it into smaller files."
        )
    root = _Node("", 0)
    stack = [root]
    pos, n = 0, len(text)
    while pos < n:
        m = _LEX_RE.match(text, pos)
        if m is None:
            raise ValueError(
                f"This file could not be read as FrameMaker MIF: unexpected text at position {pos}."
            )
        kind = m.lastgroup
        if kind == "open":
            if len(stack) > _MAX_DEPTH:
                raise InputLimitError("This MIF file is nested too deeply.")
            node = _Node(m.group(0)[1:].strip(), m.start())
            stack[-1].children.append(node)
            stack.append(node)
        elif kind == "close":
            if len(stack) == 1:
                raise ValueError(
                    "This file could not be read as FrameMaker MIF: "
                    f"unbalanced '>' at position {pos}."
                )
            node = stack.pop()
            node.end = m.end()
        elif kind == "facet":
            line_start = text.rfind("\n", 0, pos) + 1
            if text[line_start:pos].strip():
                # Not at the start of a line: an ordinary atom such as `=x` inside a
                # statement. Lex it as an atom so a later `>` on the line still closes.
                m = _ATOM_RE.match(text, pos)
                stack[-1].args.append(("atom", m.start(), m.end()))
        elif kind in ("str", "atom"):
            stack[-1].args.append((kind, m.start(), m.end()))
        pos = m.end()
    if len(stack) != 1:
        raise ValueError(
            f"This file could not be read as FrameMaker MIF: statement <{stack[-1].name}> "
            "is never closed."
        )
    return root


def decode_string(raw: str) -> str:
    """Decode the inside of a MIF string (between ` and ')."""
    out: list[str] = []
    i, n = 0, len(raw)
    while i < n:
        c = raw[i]
        if c != "\\" or i + 1 >= n:
            out.append(c)
            i += 1
            continue
        e = raw[i + 1]
        if e == "t":
            out.append("\t")
        elif e == ">":
            out.append(">")
        elif e == "q":
            out.append("'")
        elif e == "Q":
            out.append("`")
        elif e == "\\":
            out.append("\\")
        elif e == "x" and re.fullmatch(r"[0-9A-Fa-f]{2}", raw[i + 2 : i + 4]):
            # Legacy FrameMaker character set (Mac Roman based); one space follows.
            out.append(bytes([int(raw[i + 2 : i + 4], 16)]).decode("mac_roman"))
            i += 4 + (1 if raw[i + 4 : i + 5] == " " else 0)
            continue
        elif e == "u" and re.fullmatch(r"[0-9A-Fa-f]{4}", raw[i + 2 : i + 6]):
            out.append(chr(int(raw[i + 2 : i + 6], 16)))
            i += 6
            continue
        else:  # unknown escape: keep it as written
            out.append(c + e)
        i += 2
    return "".join(out)


def encode_string(text: str) -> str:
    """Encode text as a MIF string body (MIF 8+: UTF-8 characters written directly)."""
    out: list[str] = []
    for c in text:
        if c == "\\":
            out.append("\\\\")
        elif c == ">":
            out.append("\\>")
        elif c == "'":
            out.append("\\q")
        elif c == "`":
            out.append("\\Q")
        elif c == "\t":
            out.append("\\t")
        elif ord(c) < 0x20 or c == "\x7f":
            continue  # no other control characters in a MIF string
        else:
            out.append(c)
    return "".join(out)


def _string_arg(text: str, node: _Node) -> str | None:
    if len(node.args) != 1 or node.children or node.args[0][0] != "str":
        return None
    _k, a, b = node.args[0]
    return decode_string(text[a + 1 : b - 1])


def _atom_arg(text: str, node: _Node) -> str | None:
    if len(node.args) != 1 or node.children or node.args[0][0] != "atom":
        return None
    _k, a, b = node.args[0]
    return text[a:b]


def _font_props(text: str, node: _Node) -> dict[str, str] | None:
    """A Font statement as {property: value} (whitespace-normalised raw text). An empty
    FTag (no character format) is the paragraph default and is left out. None when the
    statement has loose arguments instead of a property list."""
    if node.args:
        return None
    props: dict[str, str] = {}
    for c in node.children:
        if c.name in _FONT_IGNORE:
            continue
        raw = text[c.start : c.end]
        val = " ".join(raw[raw.index(c.name) + len(c.name) : -1].split())
        if c.name == "FTag" and val in ("`'", ""):
            continue
        props[c.name] = val
    return props


#: Font property values that are the plain setting (used when the paragraph format's own
#: value is not in the file): `<FWeight `Regular'>`, `<FUnderlining FNoUnderlining>` ...
_PLAIN_VALUES = frozenset({"`Regular'", "FNoUnderlining", "No", "FAsTyped", "FNormal", "`Black'"})


def _restores(g: dict, before: dict, f: dict, base: dict) -> bool:
    """Font statement `g` restores the font in effect before opener `f` (#680): it is
    that font exactly (FrameMaker writes an in-line Font as the whole difference from
    the paragraph font), or it resets exactly the properties `f` changed, each to its
    earlier value; where that is the paragraph format's, to the format's own value
    (`base`) or, when the file does not give it, to a plain value such as `Regular'.
    `<FSize 12>` ... `<FSize 14>` is two changes, not a pair."""
    if g == before:
        return True
    changed = {k: v for k, v in f.items() if before.get(k) != v}
    if not changed or set(g) != set(changed):
        return False
    for k, v in g.items():
        if v == f[k]:
            return False
        want = before.get(k, base.get(k))
        if (v != want) if want is not None else (v not in _PLAIN_VALUES):
            return False
    return True


def _pgf_fonts(text: str, root: _Node) -> dict[str, dict[str, str]]:
    """{paragraph tag: its PgfFont properties} from the file's PgfCatalog."""
    out: dict[str, dict[str, str]] = {}
    for cat in root.children:
        if cat.name != "PgfCatalog":
            continue
        for pgf in cat.children:
            if pgf.name == "Pgf":
                tag, font = _pgf_font(text, pgf)
                if tag is not None and font is not None:
                    out[tag] = font
    return out


def _pgf_font(text: str, pgf: _Node) -> tuple[str | None, dict[str, str] | None]:
    tag = font = None
    for c in pgf.children:
        if c.name == "PgfTag":
            tag = _string_arg(text, c)
        elif c.name == "PgfFont":
            font = _font_props(text, c)
    return tag, font


def _pair_fonts(text: str, items: list, fonts: list[int], base: dict) -> dict[int, int]:
    """{opener item index: closer item index} for the paragraph's font changes that a
    later Font statement restores (#680). A restore of an outer change while an inner
    one is still open leaves the inner one a point token; a change never restored, a
    no-op Font or one with loose arguments stays a point too."""
    stack: list[tuple[int, dict, dict]] = []  # (item, font before it, its properties)
    state: dict = {}
    pairs: dict[int, int] = {}
    for i in fonts:
        g = _font_props(text, items[i][1])
        if g is None:
            continue
        for depth in range(len(stack) - 1, -1, -1):
            o, before, f = stack[depth]
            if _restores(g, before, f, base):
                pairs[o] = i
                del stack[depth:]
                state = before
                break
        else:
            if g != state:
                stack.append((i, state, g))
                state = {**state, **g}
    return pairs


class _Para:
    """One Para statement flattened into text / raw / rect items."""

    __slots__ = (
        "first",
        "items",
        "last",
        "lines",
        "node",
        "pairs",
        "parent",
        "style",
        "text",
        "tokens",
    )

    def __init__(self, text: str, node: _Node, pgf_fonts: dict | None = None):
        self.node = node
        self.lines = [c for c in node.children if c.name == "ParaLine"]
        self.style = ""
        for c in node.children:
            if c.name == "PgfTag":
                self.style = _string_arg(text, c) or ""
                break
        items: list[tuple[str, object]] = []
        fonts: list[int] = []  # Font statements outside cross-references
        in_xref = False
        for pl in self.lines:
            for c in pl.children:
                if c.name == "Font" and not in_xref:
                    fonts.append(len(items))
                if in_xref:
                    items.append(("raw", c))
                    in_xref = c.name != "XRefEnd"
                    continue
                if c.name == "String":
                    s = _string_arg(text, c)
                    items.append(("text", s) if s is not None else ("raw", c))
                elif c.name == "Char":
                    ch = _CHAR_TEXT.get(_atom_arg(text, c) or "")
                    items.append(("text", ch) if ch is not None else ("raw", c))
                elif c.name == "TextRectID":
                    items.append(("rect", c))
                elif c.name == "XRef":
                    items.append(("raw", c))
                    in_xref = True
                else:
                    items.append(("raw", c))
        self.items = items
        idx = [i for i, (k, _v) in enumerate(items) if k == "text"]
        lo = idx[0] if idx else len(items)
        hi = idx[-1] if idx else -1
        base = dict((pgf_fonts or {}).get(self.style, {}))
        for c in node.children:  # an in-line Pgf override of the paragraph font
            if c.name == "Pgf":
                base.update(_pgf_font(text, c)[1] or {})
        pairs = _pair_fonts(text, items, fonts, base) if idx else {}
        while True:  # a pair with one end in the unit pulls its other end in
            grow = [(o, c) for o, c in pairs.items() if (lo <= o <= hi) != (lo <= c <= hi)]
            if not grow:
                break
            lo = min([lo] + [o for o, _c in grow])
            hi = max([hi] + [c for _o, c in grow])
        self.first, self.last = lo, hi
        opener = {o: c for o, c in pairs.items() if lo <= o and c <= hi}
        closer = {c: o for o, c in opener.items()}
        parts: list[str] = []
        self.tokens: dict[str, list[_Node]] = {}  # point tokens
        self.pairs: dict[str, tuple[_Node, _Node]] = {}  # paired font changes
        self.parent: dict[str, str | None] = {}  # pair -> enclosing pair in the source
        ids: dict[int, str] = {}
        open_stack: list[str] = []
        run: list[_Node] = []
        n = 0
        for j in range(lo, hi + 1):
            k, v = items[j]
            if k == "rect":  # line layout inside the text: dropped on rebuild
                continue
            if k == "raw" and j not in opener and j not in closer:
                run.append(v)
                continue
            if run:  # shared id counter: x and g tokens number in reading order
                n += 1
                self.tokens[str(n)] = run
                parts.append(f"⟦x{n}⟧")
                run = []
            if j in opener:
                n += 1
                tid = ids[j] = str(n)
                self.pairs[tid] = (v, items[opener[j]][1])
                self.parent[tid] = open_stack[-1] if open_stack else None
                open_stack.append(tid)
                parts.append(f"⟦g{tid}⟧")
            elif j in closer:
                tid = ids[closer[j]]
                open_stack.remove(tid)
                parts.append(f"⟦/g{tid}⟧")
            else:
                parts.append(v)
        self.text = "".join(parts)

    def translatable(self) -> bool:
        if not self.lines or "⟦" in self._plain() or "⟧" in self._plain():
            return False
        return any(ch.isalnum() for ch in _TOKEN_RE.sub("", self.text))

    def _plain(self) -> str:
        return "".join(v for k, v in self.items if k == "text")


def _walk_paras(text: str, root: _Node):
    """Every Para statement in file order (iterative: depth is already bounded)."""
    stack = [iter(root.children)]
    while stack:
        node = next(stack[-1], None)
        if node is None:
            stack.pop()
            continue
        if node.name == "Para":
            yield node
            continue  # a Para never nests another Para
        if node.children:
            stack.append(iter(node.children))


class _Doc:
    def __init__(self, data: bytes):
        self.version = _check_version(bytes(data[:8192]))
        self.text, self.bom = _decode(data)
        self.nl = "\r\n" if "\r\n" in self.text else "\n"
        root = _parse_tree(self.text)
        pgf_fonts = _pgf_fonts(self.text, root)
        self.units: list[_Para] = []
        self.skipped_marker_paras = 0
        for node in _walk_paras(self.text, root):
            p = _Para(self.text, node, pgf_fonts)
            if p.translatable():
                self.units.append(p)
                check_segment_cap(len(self.units))
            elif p.lines and ("⟦" in p._plain() or "⟧" in p._plain()):
                self.skipped_marker_paras += 1


def _raw(text: str, node: _Node) -> str:
    return text[node.start : node.end]


def _live_pairs(p: _Para, matches) -> dict[str, tuple[int, int]]:
    """{pair id: (open match index, close match index)} for each font pair the
    translation writes whole (its first ⟦gN⟧, the first ⟦/gN⟧ after it) and nested as in
    the source: directly inside the same enclosing pair. A restore statement returns to
    the font around its change, so a pair anywhere else, or one whose enclosing pair was
    dropped, would format the wrong words: such a pair is dropped (its words kept, its
    font change lost), the tolerance of the other filters (#680 gate: never a 409)."""
    opens: dict[str, int] = {}
    closes: dict[str, int] = {}
    for k, m in enumerate(matches):
        slash, kind, tid = m.groups()
        if tid not in p.pairs or kind != "g":
            continue
        if not slash:
            opens.setdefault(tid, k)
        elif tid in opens:
            closes.setdefault(tid, k)
    live = {tid: (opens[tid], closes[tid]) for tid in closes}
    while True:
        at = {k: (tid, end) for tid, ks in live.items() for end, k in enumerate(ks)}
        stack: list[str] = []
        bad: set[str] = set()
        for k in sorted(at):
            tid, end = at[k]
            if not end:
                if p.parent[tid] != (stack[-1] if stack else None):
                    bad.add(tid)
                stack.append(tid)
            elif stack and stack[-1] == tid:
                stack.pop()
            else:
                bad.add(tid)
                stack.remove(tid)
        if not bad:
            return live
        for tid in bad:
            del live[tid]


def _drop_empty_pairs(pieces: list[tuple[str, str]]) -> list[tuple[str, str]]:
    """Drop a pair MT emptied (`⟦g1⟧⟦/g1⟧`): a font change around nothing."""
    out: list[tuple[str, str]] = []
    for piece in pieces:
        if piece[0] == "text" and not piece[1]:
            continue
        if piece[0] == "close" and out and out[-1] == ("open", piece[1]):
            out.pop()
            continue
        out.append(piece)
    return out


def _render(doc: _Doc, p: _Para, translated: str) -> str:
    text, nl = doc.text, doc.nl
    first_line = p.lines[0]
    line_start = text.rfind("\n", 0, first_line.start) + 1
    ind = text[line_start : first_line.start]
    ind = ind if not ind.strip() else ""
    cind = ind + " "
    body: list[str] = []
    rect_done = False
    for k, v in p.items[: p.first]:
        if k == "rect":
            if not rect_done:
                body.append(_raw(text, v))
                rect_done = True
        else:
            body.append(_raw(text, v))
    # translated text with tokens; missing point tokens are appended so anchors survive
    used: set[str] = set()
    pos = 0
    pieces: list[tuple[str, str]] = []
    matches = list(_ANY_TOKEN_RE.finditer(translated))
    live = _live_pairs(p, matches)
    for k, m in enumerate(matches):
        pieces.append(("text", translated[pos : m.start()]))
        slash, kind, tid = m.groups()
        if tid in p.pairs:  # written only where the pair is live, else dropped
            if tid in live and k in live[tid]:
                pieces.append(("close" if slash else "open", tid))
        elif kind == "x" and not slash and tid in p.tokens and tid not in used:
            used.add(tid)
            pieces.append(("tok", tid))
        pos = m.end()
    pieces.append(("text", translated[pos:]))
    for tid in p.tokens:
        if tid not in used:
            pieces.append(("tok", tid))
    lines: list[list[str]] = [body]
    for kind, val in _drop_empty_pairs(pieces):
        if kind == "tok":
            lines[-1].extend(_raw(text, n) for n in p.tokens[val] if n.name != "TextRectID")
            continue
        if kind in ("open", "close"):
            lines[-1].append(_raw(text, p.pairs[val][0 if kind == "open" else 1]))
            continue
        val = val.replace("\r\n", "\n").replace("\r", "\n")
        for j, seg in enumerate(val.split("\n")):
            if j:
                lines[-1].append("<Char HardReturn>")
                lines.append([])
            if seg:
                lines[-1].append(f"<String `{encode_string(seg)}'>")
    for k, v in p.items[p.last + 1 :]:
        if k == "raw":
            lines[-1].append(_raw(text, v))
    out = []
    for j, stmts in enumerate(lines):
        head = "" if j == 0 else ind
        # the last close keeps the original line's own trailing comment
        tail = ">" if j == len(lines) - 1 else "> # end of ParaLine"
        out.append(head + "<ParaLine" + nl + "".join(cind + s + nl for s in stmts) + ind + tail)
    # non-ParaLine statements that sat between the first and last ParaLine are kept
    last_line = p.lines[-1]
    for c in p.node.children:
        if c.name != "ParaLine" and first_line.start < c.start < last_line.start:
            out.append(ind + _raw(text, c))
    return nl.join(out)


@register
class MifFilter(Filter):
    fmt = "mif"
    exts = ()

    def parse(self, data: bytes, provider: str | None = None) -> ParseResult:
        doc = _Doc(data)
        units = [
            TextUnit(
                p_index=i,
                text=p.text,
                style=p.style,
                inline_map={
                    **{
                        f"x{t}": "".join(_raw(doc.text, n) for n in ns)
                        for t, ns in p.tokens.items()
                    },
                    **{f"g{t}": _raw(doc.text, o) for t, (o, _c) in p.pairs.items()},
                    **{f"/g{t}": _raw(doc.text, c) for t, (_o, c) in p.pairs.items()},
                },
            )
            for i, p in enumerate(doc.units)
        ]
        check_unit_texts(u.text for u in units)
        skeleton = {
            "format": "mif",
            "mif_version": doc.version,
            "units": [{"p_index": u.p_index, "text": u.text, "style": u.style} for u in units],
        }
        if doc.skipped_marker_paras:
            skeleton["skipped_marker_paragraphs"] = doc.skipped_marker_paras
        return ParseResult(skeleton=skeleton, units=units)

    def build(self, original: bytes, paragraph_texts: dict) -> bytes:
        doc = _Doc(original)
        edits = []
        for i, p in enumerate(doc.units):
            new = paragraph_texts.get(str(i))
            if new is None or new == p.text:
                continue
            edits.append((p.lines[0].start, p.lines[-1].end, _render(doc, p, new)))
        if not edits:
            return bytes(original)
        parts, pos = [], 0
        for a, b, raw in sorted(edits):  # paragraphs never overlap
            parts += [doc.text[pos:a], raw]
            pos = b
        parts.append(doc.text[pos:])
        text = "".join(parts)
        _parse_tree(text)  # every rebuild must still be well-formed MIF
        return doc.bom + text.encode("utf-8")

    def build_localized(
        self, original: bytes, paragraph_texts: dict, target_locale: str | None = None
    ) -> bytes:
        """build() plus the TARGET language: every PgfLanguage and FLanguage keyword
        (paragraph formats, character formats, catalogs) is set to the FrameMaker
        language for the target locale, NoLanguage when FrameMaker has none."""
        out = self.build(original, paragraph_texts)
        lang = fm_language(target_locale)
        if lang is None:
            return out
        doc_bom = b"\xef\xbb\xbf" if out.startswith(b"\xef\xbb\xbf") else b""
        text = out[len(doc_bom) :].decode("utf-8")
        spans: list[tuple[int, int]] = []
        stack = [_parse_tree(text)]
        while stack:
            node = stack.pop()
            for c in node.children:
                if c.name in _LANGUAGE_STATEMENTS and _atom_arg(text, c) is not None:
                    spans.append(c.args[0][1:])
                elif c.children:
                    stack.append(c)
        if not spans:
            return out
        parts, pos = [], 0
        for a, b in sorted(spans):
            parts += [text[pos:a], lang]
            pos = b
        parts.append(text[pos:])
        return doc_bom + "".join(parts).encode("utf-8")
