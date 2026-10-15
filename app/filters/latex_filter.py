"""LaTeX filter (.tex, .ltx).

A scanner over the LaTeX source (no TeX engine, nothing is ever executed, expanded or
followed). Translatable units are the prose of the document body (after
``\\begin{document}``; a file without one, such as a chapter pulled in with ``\\input``,
is all body):

  - paragraphs (split at blank lines, block-level commands, environment boundaries,
    display math, ``\\item``, comments, and in tabular environments at ``&`` and ``\\\\``)
  - the arguments of the block text commands (``\\section`` .. ``\\subparagraph``,
    ``\\chapter``, ``\\part``, ``\\caption``, ``\\title``, ...), the optional short-title
    argument too, and ``\\item[label]`` labels; in the preamble only ``\\title`` and
    ``\\subtitle`` arguments
  - inline text commands are pairs ``⟦gN⟧text⟦/gN⟧`` so their words are translated and
    the command is not: ``\\emph``, ``\\textbf``, ``\\textit``, ``\\footnote``,
    ``\\underline``, ... and ``\\href{url}{text}`` / ``\\textcolor{c}{text}`` (the first
    argument stays in the markup); a bare ``{...}`` group is a pair as well

Never translated, kept byte for byte: the preamble, comments (``%`` to end of line),
math (``$...$``, ``$$...$$``, ``\\(...\\)``, ``\\[...\\]``, ``equation``, ``align``,
``gather``, ``multline``, ``eqnarray``, ``math``, ``displaymath`` and starred forms),
``verbatim``/``Verbatim``/``lstlisting``/``minted``/``comment``/``filecontents``,
``tikzpicture``, ``\\verb|...|``, ``\\lstinline``, every other command with its
arguments (``\\label``, ``\\ref``, ``\\cite``, ``\\url``, ``\\includegraphics``, ...) as a
point token ``⟦xN⟧``, environment names and arguments, tabular column specifications.
``\\input``, ``\\include``, ``\\subfile`` and ``\\bibliography`` are never followed.

Special characters: ``\\& \\% \\$ \\# \\_ \\{ \\}`` read as the plain character and
``~`` as a no-break space, so the translator sees text; on build every one of
``& % $ # _ { } ~ ^ \\`` in the translated words is escaped again (``\\textbackslash{}``,
``\\textasciitilde{}``, ``\\textasciicircum{}``), and a no-break space is written as
``~``. A line break in a translation becomes a space (a blank line would start a new
paragraph).

The allowlists are module constants (TEXT_BLOCK_COMMANDS, TEXT_INLINE_COMMANDS); an
install can add inline text commands with LATEX_TEXT_COMMANDS (comma-separated names,
e.g. ``mynote,keyword``). Changing it changes the units of a file: upload such files
again after changing it.

Encoding: UTF-8 (a BOM is kept), with the shared #509 decode; a Windows-1252 file is
exported as UTF-8 and an ``inputenc`` option ``latin1``/``latin9``/``cp1252``/``ansinew``
is rewritten to ``utf8`` so the output stays truthful. babel/polyglossia language
options are not changed (build_localized is build): the language names differ per
package (babel ngerman vs polyglossia german), so the target is not guessed.

References: LaTeX2e unofficial reference manual (https://latexref.xyz/), "LaTeX2e for
authors" (https://www.latex-project.org/help/documentation/usrguide.pdf).

Documented in: docs/architecture/translation-pipeline.md
"""

from __future__ import annotations

import bisect
import os
import re

from app.filters.base import (
    Filter,
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
_MAX_DEPTH = 40
_MAX_TOKENS = 1000

# Block commands whose arguments are units of their own.
TEXT_BLOCK_COMMANDS = frozenset(
    {
        "part",
        "chapter",
        "section",
        "subsection",
        "subsubsection",
        "paragraph",
        "subparagraph",
        "addpart",
        "addchap",
        "addsec",
        "minisec",
        "caption",
        "captionof",
        "title",
        "subtitle",
        "frametitle",
        "framesubtitle",
    }
)
# Inline commands whose LAST mandatory argument is text: name -> number of leading
# mandatory arguments that stay in the markup.
TEXT_INLINE_COMMANDS = {
    "emph": 0,
    "textbf": 0,
    "textit": 0,
    "textsl": 0,
    "textsc": 0,
    "textup": 0,
    "textmd": 0,
    "textrm": 0,
    "textsf": 0,
    "textnormal": 0,
    "underline": 0,
    "uline": 0,
    "footnote": 0,
    "footnotetext": 0,
    "marginpar": 0,
    "mbox": 0,
    "fbox": 0,
    "enquote": 0,
    "textquote": 0,
    "intertext": 0,
    "text": 0,
    "href": 1,
    "textcolor": 1,
    "colorbox": 1,
    "foreignlanguage": 1,
    "hyperref": 0,
}
# Commands that end a paragraph unit and are skipped with their arguments.
_BOUNDARY_COMMANDS = frozenset(
    {
        "par",
        "item",
        "bibitem",
        "hline",
        "cline",
        "toprule",
        "midrule",
        "bottomrule",
        "cmidrule",
        "centering",
        "raggedright",
        "raggedleft",
        "maketitle",
        "tableofcontents",
        "listoffigures",
        "listoftables",
        "newpage",
        "clearpage",
        "cleardoublepage",
        "pagebreak",
        "vspace",
        "vfill",
        "bigskip",
        "medskip",
        "smallskip",
        "printbibliography",
        "bibliography",
        "bibliographystyle",
        "input",
        "include",
        "includeonly",
        "subfile",
        "appendix",
        "frontmatter",
        "mainmatter",
        "backmatter",
        "usepackage",
        "documentclass",
        "newcommand",
        "renewcommand",
        "providecommand",
        "newenvironment",
        "renewenvironment",
        "setlength",
        "addtolength",
        "setcounter",
        "addtocounter",
        "includegraphics",
        "label",
        "printindex",
        "makeindex",
    }
)
# Arguments read verbatim by the command: `%` and `#` there are not comments.
_VERBATIM_ARGS = frozenset({"url", "href", "path", "nolinkurl", "hyperlink", "hypertarget"})
_WS = re.compile(r"[ \t\r\n]+")  # never \s: a no-break space (~) is text
_DEF_COMMANDS = frozenset({"def", "gdef", "edef", "xdef", "let"})
# Environments skipped whole (contents never translated).
_VERBATIM_ENVS = frozenset(
    {
        "verbatim",
        "verbatim*",
        "Verbatim",
        "Verbatim*",
        "BVerbatim",
        "LVerbatim",
        "lstlisting",
        "minted",
        "comment",
        "filecontents",
        "filecontents*",
        "alltt",
        "spverbatim",
        "tcblisting",
        "luacode",
        "luacode*",
        "pycode",
    }
)
_SKIP_ENVS = frozenset(
    {
        "equation",
        "equation*",
        "align",
        "align*",
        "alignat",
        "alignat*",
        "flalign",
        "flalign*",
        "gather",
        "gather*",
        "multline",
        "multline*",
        "eqnarray",
        "eqnarray*",
        "math",
        "displaymath",
        "array",
        "split",
        "dmath",
        "dmath*",
        "tikzpicture",
        "pgfpicture",
        "picture",
        "forest",
        "axis",
        "circuitikz",
    }
)
_TABULAR_ENVS = {
    "tabular": 1,
    "tabular*": 2,
    "tabularx": 2,
    "tabulary": 2,
    "longtable": 1,
    "supertabular": 1,
    "xtabular": 1,
    "tblr": 1,
}
_ENV_ARGS = {
    "minipage": 1,
    "multicols": 1,
    "thebibliography": 1,
    "wrapfigure": 2,
    "list": 2,
    "otherlanguage": 1,
    "otherlanguage*": 1,
    "subfigure": 1,
    "adjustbox": 1,
    "tcolorbox": 0,
}
_INPUTENC_RE = re.compile(
    r"(\\usepackage\s*\[)\s*(latin1|latin9|cp1252|ansinew)\s*(\]\s*\{inputenc\})"
)


def _extra_inline() -> dict:
    raw = os.environ.get("LATEX_TEXT_COMMANDS", "")
    return {n.strip(): 0 for n in raw.split(",") if re.fullmatch(r"[A-Za-z]{1,50}", n.strip())}


# --------------------------------------------------------------------------- low-level scanning


def _cs(text: str, i: int) -> tuple[str, int]:
    """Control sequence at text[i] == '\\': (name, end). A control word swallows a
    trailing '*' only for known starred forms (handled by callers)."""
    j = i + 1
    if j >= len(text):
        return "", j
    if text[j].isalpha():
        k = j
        while k < len(text) and text[k].isalpha():
            k += 1
        return text[j:k], k
    return text[j], j + 1


_MAPS: dict = {}  # (id(text), verbatim) -> (text, maps); identity-checked, bounded


def _group_maps(text: str, verbatim: bool) -> tuple[dict, dict, list, list]:
    key = (id(text), verbatim)
    hit = _MAPS.get(key)
    if hit is not None and hit[0] is text:
        return hit[1]
    if len(_MAPS) >= 64:
        _MAPS.clear()
    maps = _compute_group_maps(text, verbatim)
    _MAPS[key] = (text, maps)
    return maps


def _compute_group_maps(text: str, verbatim: bool) -> tuple[dict, dict, list, list]:
    """One linear pass over `text` (#571-style budget: an unclosed `{`, `[` or `$`
    repeated many times must not cost a rescan each):
      brace[i]   -> index after the `}` matching the `{` at i
      bracket[i] -> index after the first `]` at the same brace level as the `[` at i
      dollars    -> sorted positions of unescaped `$`
      blanks     -> sorted positions of newlines that start a blank line
    Escapes skip the next character; `%` starts a comment unless `verbatim` (\\url);
    a blank line ends every open group (LaTeX arguments never cross a paragraph)."""
    brace: dict = {}
    bracket: dict = {}
    dollars: list = []
    blanks: list = []
    stack: list = []
    pend: list = [[]]
    n = len(text)
    k = 0
    while k < n:
        c = text[k]
        if c == "\\":
            k += 2
            continue
        if c == "%" and not verbatim:
            nl = text.find("\n", k)
            k = n if nl < 0 else nl + 1
            continue
        if c == "\n" and _blank_line_at(text, k, n):
            blanks.append(k)
            stack.clear()
            pend = [[]]
        elif c == "{":
            stack.append(k)
            pend.append([])
        elif c == "}":
            if stack:
                brace[stack.pop()] = k + 1
                pend.pop()
            else:
                pend[-1] = []
        elif c == "[":
            pend[-1].append(k)
        elif c == "]":
            for p in pend[-1]:
                bracket[p] = k + 1
            pend[-1] = []
        elif c == "$":
            dollars.append(k)
        k += 1
    return brace, bracket, dollars, blanks


def _match_brace(text: str, i: int, limit: int, verbatim: bool = False) -> int | None:
    """text[i] == '{': index after the matching '}' (escapes and comments honoured,
    never across a blank line), or None."""
    e = _group_maps(text, verbatim)[0].get(i)
    return e if e is not None and e <= limit else None


def _match_bracket(text: str, i: int, limit: int) -> int | None:
    """text[i] == '[': index after the first ']' at the same brace level, or None."""
    e = _group_maps(text, False)[1].get(i)
    return e if e is not None and e <= limit else None


def _blank_line_at(text: str, k: int, limit: int) -> bool:
    """text[k] == '\\n' and the next line is blank."""
    j = k + 1
    while j < limit and text[j] in " \t\r":
        j += 1
    return (j < limit and text[j] == "\n") or j >= limit


def _skip_ws(text: str, i: int, limit: int) -> int:
    """Skip spaces and at most one newline (arguments may follow on the next line)."""
    nl = False
    while i < limit and text[i] in " \t\r\n":
        if text[i] == "\n":
            if nl:
                break
            nl = True
        i += 1
    return i


def _args(
    text: str, i: int, limit: int, spaced: bool = False, verbatim: bool = False
) -> list[tuple[str, int, int]]:
    """Adjacent [opt] and {arg} groups after position i: (kind, start, end) with the
    delimiters included. `spaced`: allow whitespace before each group."""
    out = []
    while i < limit:
        j = _skip_ws(text, i, limit) if spaced else i
        if j < limit and text[j] == "{":
            e = _match_brace(text, j, limit, verbatim)
        elif j < limit and text[j] == "[":
            e = _match_bracket(text, j, limit)
        else:
            break
        if e is None:
            break
        out.append((text[j], j, e))
        i = e
    return out


def _end_of_args(args, i: int) -> int:
    return args[-1][2] if args else i


def _find_inline_math_end(text: str, i: int, limit: int) -> int | None:
    """text[i] == '$' (single): index after the next unescaped '$', never across a
    blank line, or None."""
    _, _, dollars, blanks = _group_maps(text, False)
    d = bisect.bisect_right(dollars, i)
    if d >= len(dollars) or dollars[d] >= limit:
        return None
    j = dollars[d]
    bl = bisect.bisect_right(blanks, i)
    if bl < len(blanks) and blanks[bl] < j:
        return None
    return j + 1


def _find(text: str, needle: str, i: int, limit: int) -> int:
    k = text.find(needle, i, limit)
    return limit if k < 0 else k + len(needle)


def _verb_end(text: str, i: int, limit: int) -> int:
    """i is just after \\verb or \\verb*: the delimiter is text[i]."""
    if i >= limit:
        return limit
    if text[i] == "{":
        e = _match_brace(text, i, limit)
        return e if e is not None else i + 1
    d = text[i]
    nl = text.find("\n", i + 1, limit)
    k = text.find(d, i + 1, limit if nl < 0 else nl)
    return (k + 1) if k >= 0 else i + 1


# --------------------------------------------------------------------------- block scan


class _Scan:
    def __init__(self, text: str):
        self.text = text
        self.slots: list[tuple[int, int, bool]] = []  # (start, end, in_tabular)
        self.inline = {**TEXT_INLINE_COMMANDS, **_extra_inline()}

    def unit(self, a: int, b: int, tab: bool = False) -> None:
        t = self.text
        while a < b and t[a].isspace():
            a += 1
        while b > a and t[b - 1].isspace():
            b -= 1
        if a < b:
            self.slots.append((a, b, tab))
            check_segment_cap(len(self.slots))

    def run(self) -> None:
        t = self.text
        doc = re.search(r"\\begin\s*\{document\}", t)
        while doc and _in_comment(t, doc.start()):
            doc = re.compile(r"\\begin\s*\{document\}").search(t, doc.end())
        if doc:
            self.preamble(0, doc.start())
            end = re.compile(r"\\end\s*\{document\}").search(t, doc.end())
            while end and _in_comment(t, end.start()):
                end = re.compile(r"\\end\s*\{document\}").search(t, end.end())
            self.body(doc.end(), end.start() if end else len(t))
        else:
            self.body(0, len(t))

    def preamble(self, a: int, b: int) -> None:
        t = self.text
        i = a
        while i < b:
            c = t[i]
            if c == "%":
                i = _find(t, "\n", i, b)
                continue
            if c == "\\":
                name, e = _cs(t, i)
                if name in ("title", "subtitle"):
                    for _kind, s, en in _args(t, e, b, spaced=True):
                        self.unit(s + 1, en - 1)
                        e = en
                i = e
                continue
            i += 1

    def body(self, a: int, b: int) -> None:
        t = self.text
        stack: list[str] = []
        run: int | None = None
        i = a

        def flush(at: int) -> None:
            nonlocal run
            if run is not None:
                self.unit(run, at, bool(stack) and stack[-1] in _TABULAR_ENVS)
                run = None

        def start(at: int) -> None:
            nonlocal run
            if run is None:
                run = at

        while i < b:
            c = t[i]
            if c == "%":
                flush(i)
                i = _find(t, "\n", i, b)
                continue
            if c == "\n" and _blank_line_at(t, i, b):
                flush(i)
                i += 1
                continue
            if c == "$":
                if t.startswith("$$", i):
                    flush(i)
                    k = t.find("$$", i + 2, b)
                    i = b if k < 0 else k + 2
                    continue
                e = _find_inline_math_end(t, i, b)
                start(i)
                i = e if e is not None else i + 1
                continue
            if c == "&" and stack and stack[-1] in _TABULAR_ENVS:
                flush(i)
                i += 1
                continue
            if c != "\\":
                if not c.isspace():
                    start(i)
                i += 1
                continue
            name, e = _cs(t, i)
            if name == "[":
                flush(i)
                k = t.find("\\]", e, b)
                i = b if k < 0 else k + 2
                continue
            if name == "(":
                start(i)
                k = t.find("\\)", e, b)
                i = b if k < 0 else k + 2
                continue
            if name == "\\":
                if stack and stack[-1] in _TABULAR_ENVS:
                    flush(i)
                    if e < b and t[e] == "*":
                        e += 1
                    args = _args(t, e, b)
                    i = _end_of_args(args, e)
                    continue
                start(i)
                i = e
                continue
            if name in ("verb", "lstinline", "mintinline"):
                start(i)
                if e < b and t[e] == "*":
                    e += 1
                if name == "mintinline":
                    args = _args(t, e, b)
                    e = _end_of_args(args[:1], e)
                    i = _end_of_args(args[1:2], e) if len(args) > 1 else _verb_end(t, e, b)
                    continue
                if name == "lstinline" and e < b and t[e] == "[":
                    k = _match_bracket(t, e, b)
                    e = k if k else e
                i = _verb_end(t, e, b)
                continue
            if name == "begin":
                flush(i)
                envs = _args(t, e, b, spaced=True)
                if not envs or envs[0][0] != "{":
                    i = e
                    continue
                env = t[envs[0][1] + 1 : envs[0][2] - 1].strip()
                e = envs[0][2]
                if env in _VERBATIM_ENVS or env in _SKIP_ENVS:
                    m = re.compile(r"\\end\s*\{" + re.escape(env) + r"\}").search(t, e, b)
                    i = m.end() if m else b
                    continue
                need = _TABULAR_ENVS.get(env, _ENV_ARGS.get(env, 0))
                k = e
                taken = 0
                for kind, _s, en in _args(t, e, b):
                    if kind == "{":
                        if taken >= need:
                            break
                        taken += 1
                    k = en
                stack.append(env)
                i = k
                continue
            if name == "end":
                flush(i)
                envs = _args(t, e, b, spaced=True)
                if envs and envs[0][0] == "{":
                    env = t[envs[0][1] + 1 : envs[0][2] - 1].strip()
                    if env in stack:
                        while stack and stack.pop() != env:
                            pass
                    e = envs[0][2]
                i = e
                continue
            if name in _DEF_COMMANDS:
                flush(i)
                k = e
                while k < b and t[k] not in "{\n":
                    k += 1
                if k < b and t[k] == "{":
                    en = _match_brace(t, k, b)
                    k = en if en else k + 1
                i = k
                continue
            if name in TEXT_BLOCK_COMMANDS:
                flush(i)
                if e < b and t[e] == "*":
                    e += 1
                args = _args(t, e, b, spaced=True)
                if name == "captionof" and args and args[0][0] == "{":
                    args = args[1:]  # the float type
                for _kind, s, en in args:
                    self.unit(s + 1, en - 1)
                i = _end_of_args(args, e)
                continue
            if name == "item":
                flush(i)
                args = _args(t, e, b)
                if args and args[0][0] == "[":
                    self.unit(args[0][1] + 1, args[0][2] - 1)
                    e = args[0][2]
                i = e
                continue
            if name in _BOUNDARY_COMMANDS:
                flush(i)
                if e < b and t[e] == "*":
                    e += 1
                args = _args(t, e, b, spaced=name not in ("item", "par", "centering"))
                i = _end_of_args(args, e)
                continue
            # any other command: part of the current run, arguments included
            start(i)
            if e < b and t[e] == "*":
                e += 1
            i = _end_of_args(_args(t, e, b, verbatim=name in _VERBATIM_ARGS), e)
        flush(b)


def _in_comment(text: str, pos: int) -> bool:
    ls = text.rfind("\n", 0, pos) + 1
    k = ls
    while k < pos:
        if text[k] == "\\":
            k += 2
            continue
        if text[k] == "%":
            return True
        k += 1
    return False


# --------------------------------------------------------------------------- inline tokens

_ESCAPE_IN = {"&": "&", "%": "%", "$": "$", "#": "#", "_": "_", "{": "{", "}": "}"}
_ESCAPE_OUT = {
    "\\": "\\textbackslash{}",
    "{": "\\{",
    "}": "\\}",
    "&": "\\&",
    "%": "\\%",
    "$": "\\$",
    "#": "\\#",
    "_": "\\_",
    "~": "\\textasciitilde{}",
    "^": "\\textasciicircum{}",
    "\u00a0": "~",
}
_SPECIAL_OUT = re.compile("[\\\\{}&%$#_~^\u00a0]")


class _Tok:
    def __init__(self, inline: dict):
        self.n = 0
        self.map: dict = {}
        self.inline = inline

    def point(self, raw: str) -> str:
        self.n += 1
        self.map[str(self.n)] = raw
        return ph_point("x", self.n)

    def pair(self, open_: str, inner: str, close: str, depth: int) -> str:
        self.n += 1
        n = self.n
        self.map[str(n)] = [open_, close]
        return ph_open("g", n) + _tokenize(inner, self, depth + 1) + ph_close("g", n)


def _tokenize(s: str, tok: _Tok, depth: int = 0) -> str:
    out: list[str] = []
    i, n = 0, len(s)
    buf: list[str] = []

    def text(x: str) -> None:
        buf.append(x)

    def emit(x: str) -> None:
        if buf:
            out.append(_WS.sub(" ", "".join(buf)))
            buf.clear()
        out.append(x)

    while i < n:
        c = s[i]
        if tok.n >= _MAX_TOKENS:
            emit(tok.point(s[i:]))
            i = n
            break
        if c == "%":
            e = _find(s, "\n", i, n)
            while e < n and s[e] in " \t":
                e += 1
            emit(tok.point(s[i:e]))
            i = e
            continue
        if c == "~":
            text("\u00a0")
            i += 1
            continue
        if c == "$":
            if s.startswith("$$", i):
                k = s.find("$$", i + 2)
                e = n if k < 0 else k + 2
            else:
                e = _find_inline_math_end(s, i, n) or i + 1
            emit(tok.point(s[i:e]))
            i = e
            continue
        if c == "{":
            e = _match_brace(s, i, n)
            if e is None or depth >= _MAX_DEPTH:
                emit(tok.point(s[i : e or i + 1]))
                i = e or i + 1
                continue
            emit(tok.pair("{", s[i + 1 : e - 1], "}", depth))
            i = e
            continue
        if c == "}":
            emit(tok.point("}"))
            i += 1
            continue
        if c != "\\":
            text(c)
            i += 1
            continue
        name, e = _cs(s, i)
        if name in _ESCAPE_IN:
            text(_ESCAPE_IN[name])
            i = e
            continue
        if name == "(":
            k = s.find("\\)", e)
            e = n if k < 0 else k + 2
            emit(tok.point(s[i:e]))
            i = e
            continue
        if name in ("verb", "lstinline"):
            if e < n and s[e] == "*":
                e += 1
            if name == "lstinline" and e < n and s[e] == "[":
                e = _match_bracket(s, e, n) or e
            e = _verb_end(s, e, n)
            emit(tok.point(s[i:e]))
            i = e
            continue
        if name == "\\":
            if e < n and s[e] == "*":
                e += 1
            e = _end_of_args(_args(s, e, n), e)
            emit(tok.point(s[i:e]))
            i = e
            continue
        star = e
        if star < n and s[star] == "*":
            star += 1
        args = _args(s, star, n, verbatim=name in _VERBATIM_ARGS)
        keep = tok.inline.get(name)
        braces = [a for a in args if a[0] == "{"]
        if keep is not None and depth < _MAX_DEPTH and len(braces) > keep:
            target = braces[keep]
            open_ = s[i : target[1] + 1]
            close = "}"
            e = target[2]
            emit(tok.pair(open_, s[target[1] + 1 : target[2] - 1], close, depth))
            # optional/extra groups after the text argument stay as a point
            i = e
            continue
        end = _end_of_args(args, star)
        # a control word eats the following blanks in TeX; keep them in the token so
        # the translated text keeps its spacing unchanged
        if name.isalpha() and not args:
            k = end
            while k < n and s[k] in " \t":
                k += 1
            if k > end and (k >= n or s[k] not in "\n"):
                end = k
        emit(tok.point(s[i:end]))
        i = end
    if buf:
        out.append(_WS.sub(" ", "".join(buf)))
    return "".join(out)


def _unit_of(raw: str, inline: dict) -> tuple[str, dict]:
    tok = _Tok(inline)
    return _tokenize(raw, tok).strip(), tok.map


def _escape(s: str, tabular: bool) -> str:
    return _SPECIAL_OUT.sub(lambda m: _ESCAPE_OUT[m.group(0)], s)


def _render(translation: str, imap: dict, tabular: bool) -> str:
    t = re.sub(r"[ \t]*(?:(?:\r\n|\r|\n)[ \t]*)+", " ", translation.strip())
    out: list[str] = []
    pos = 0
    opened: list[str] = []  # pair ids whose opening markup was written, innermost last
    words: list[int] = []  # pieces of point tokens that end in a control word (#696)
    for m in _PLACEHOLDER_RE.finditer(t):
        out.append(_escape(t[pos : m.start()], tabular))
        pos = m.end()
        inner = m.group(0)[1:-1]
        closing = inner.startswith("/")
        key = inner.lstrip("/")[1:]
        val = imap.get(key)
        if val is None:
            continue
        if isinstance(val, list):
            # Gate: braces must stay balanced or the whole document stops compiling. A
            # close without its open is dropped; an open the translation never closes
            # (or closes out of order) is closed here.
            if not closing:
                if key not in opened:
                    opened.append(key)
                    out.append(val[0])
            elif key in opened:
                while opened:
                    k = opened.pop()
                    out.append(imap[k][1])
                    if k == key:
                        break
        elif not closing:
            if _ENDS_IN_CONTROL_WORD.search(val):
                words.append(len(out))
            out.append(val)
    out.append(_escape(t[pos:], tabular))
    while opened:
        out.append(imap[opened.pop()][1])
    return _separate_control_words(out, words)


# #696: a control word ends at the first non-letter. The source has a space, `\ `, `{}` or
# punctuation after `\TeX`; a Japanese or Chinese translation glues the token to letters,
# and XeLaTeX / LuaLaTeX (every Unicode letter is catcode 11) or pTeX/upTeX (kanji in
# control sequence names) read `\TeXを` as one undefined command. An empty group ends the
# control word without adding a space.
# #742: TeX also skips every blank after a control word, so a translation that moves the
# token before a word (German `mit \LaTeX setzt`) rendered the two words glued. A blank
# right after such a token gets the same `{}` (`\LaTeX{} setzt`); a token whose source
# already carried its trailing blank does not end in a control word and is left alone.
_ENDS_IN_CONTROL_WORD = re.compile(r"(?<!\\)(?:\\\\)*\\[A-Za-z]+$")


def _separate_control_words(out: list[str], words: list[int]) -> str:
    for i in words:
        nxt = next((piece[0] for piece in out[i + 1 :] if piece), "")
        if nxt.isalpha() or nxt in (" ", "\t"):
            out[i] += "{}"
    return "".join(out)


def _translatable(unit: str) -> bool:
    return any(ch.isalpha() for ch in strip_placeholders(unit))


def _units(text: str):
    sc = _Scan(text)
    sc.run()
    for a, b, tab in sc.slots:
        unit, imap = _unit_of(text[a:b], sc.inline)
        if _translatable(unit):
            yield a, b, tab, unit, imap


@register
class LatexFilter(Filter):
    fmt = "tex"
    exts = ("latex", "ltx")

    def parse(self, data: bytes, provider: str | None = None) -> ParseResult:
        text, enc = read_text_upload(data, sig=True)  # #509 refusal + #571 line budget
        units: list[TextUnit] = []
        for _a, _b, _tab, unit, imap in _units(text):
            units.append(TextUnit(p_index=len(units), text=unit, inline_map=imap))
        check_unit_texts(u.text for u in units)
        skeleton = {
            "format": "tex",
            "units": [{"p_index": u.p_index, "text": u.text, "style": ""} for u in units],
        }
        _MAPS.clear()  # release the document text
        return ParseResult(skeleton=note_source_encoding(skeleton, enc), units=units)

    def build(self, original: bytes, paragraph_texts: dict) -> bytes:
        raw = bytes(original)
        text = decode_text_lossless(raw, sig=True)
        out: list[str] = []
        pos = 0
        for k, (a, b, tab, unit, imap) in enumerate(_units(text)):
            new = paragraph_texts.get(str(k))
            if new is None or new.strip() == unit:
                continue
            out.append(text[pos:a])
            out.append(_render(new, imap, tab))
            pos = b
        out.append(text[pos:])
        result = "".join(out)
        try:
            raw.decode("utf-8")
        except UnicodeDecodeError:
            result = _INPUTENC_RE.sub(lambda m: m.group(1) + "utf8" + m.group(3), result)
        _MAPS.clear()
        body = encode_text_lossless(result)
        return _BOM + body if raw.startswith(_BOM) and not body.startswith(_BOM) else body


__all__ = ["TEXT_BLOCK_COMMANDS", "TEXT_INLINE_COMMANDS", "LatexFilter"]
