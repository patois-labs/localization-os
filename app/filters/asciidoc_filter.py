"""AsciiDoc filter (.adoc, .asciidoc, .asc).

A line-grammar walk over the source (no Asciidoctor dependency, nothing is ever
executed or followed). Translatable units:

  - paragraphs (consecutive lines joined into one unit), admonition paragraphs
    (``NOTE:`` .. ``WARNING:`` label kept outside the unit), list items (``*``, ``-``,
    ``.``, ``1.``, ``a.``, ``i)``, callouts ``<1>``, checklist ``[x]``), description
    list terms and descriptions (``::``, ``:::``, ``::::``, ``;;``)
  - section titles (``=`` / ``#`` markers, symmetric trailing markers kept), two-line
    (setext) titles, block titles (``.Title``)
  - table cells of ``|===`` and nested ``!===`` tables (cell specifiers such as ``2+^.^a``
    stay outside the unit; a literal ``l`` cell is skipped)

Never translated, kept byte for byte: listing ``----``, literal ``....``, passthrough
``++++``, comment ``////`` and fenced ```````` blocks; blocks and paragraphs styled
``[source]``, ``[listing]``, ``[literal]``, ``[pass]``, ``[comment]``, ``[stem]`` (and
diagram styles); indented literal paragraphs; line comments ``//``; attribute entries
``:name: value`` (with `` \\`` continuation lines; #733: except the values of the
header's ``:description:`` and ``:keywords:``, which Asciidoctor writes to the HTML
``<meta>`` tags: they are units appended after every body unit, written back on one line)
and the document header's author and revision lines; block attribute lines ``[...]``;
block macros ``image::`` etc.;
preprocessor directives ``include::``, ``ifdef::``, ``ifndef::``, ``ifeval::``,
``endif::`` (an include is never followed: the line is kept as is); breaks; CSV/DSV
tables (``,===``, ``:===``, ``format=csv``).

Inline syntax becomes protected tokens from the shared ``services/segment`` scheme:
attribute references ``{name}``, inline anchors, passthroughs and monospace code are
point tokens ``⟦xN⟧``; formatting marks (``*bold*``, ``__it__``, ``#mark#``, ``^sup^``,
``~sub~``, ``[.role]#x#``) and link/xref/footnote macros with link text are paired
``⟦gN⟧text⟦/gN⟧`` so the text is translated and the markup is not; ``image:``, ``kbd:``,
``btn:``, ``menu:`` and bare URLs are point tokens; a hard line break `` +`` is a point
token. A constrained mark (`` `code` ``, ``*b*``, ``_i_``, ``#m#``, ``+p+``) that a translation
glues to a word character (Japanese, Chinese) is written in its doubled, unconstrained
form on build (#695); a URL link glued to a character Asciidoctor does not accept before
it is written as a ``link:`` macro (#698). parse() and build() walk the same slots in the
same order; build()
re-derives each slot's token map from the original, so nothing but the translated spans
changes.

Encoding: AsciiDoc is UTF-8 (a UTF-8 BOM is kept); the #509 shared decode applies.
``build_localized`` sets the document's ``:lang:`` attribute to the target language.

Spec: https://docs.asciidoctor.org/asciidoc/latest/.
"""

import re
import unicodedata

from app.filters.base import (
    EmptyDocumentError,
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
_LINE_RE = re.compile(r"([^\r\n]*)(\r\n|\r|\n|$)")
_NEWLINE_RE = re.compile(r"\r\n|\r|\n")

# --- block grammar ------------------------------------------------------------------
_VERBATIM_DELIM = re.compile(r"(-{4,}|\.{4,}|\+{4,}|/{4,})")
_FENCE_OPEN = re.compile(r"```[\w+#.-]*")
_COMPOUND_DELIM = re.compile(r"(={4,}|\*{4,}|_{4,}|--)")
_TABLE_DELIM = re.compile(r"([|!,:])={3,}")
_PREPROC = re.compile(r"\\?(?:include|ifdef|ifndef|ifeval|endif)::")
_ATTR_ENTRY = re.compile(r":!?[A-Za-z0-9_][A-Za-z0-9_-]*!?:(?:[ \t]|$)")
_BLOCK_ATTR = re.compile(r"\[.*\]")
_BLOCK_MACRO = re.compile(r"[a-z][\w-]*::[^\s\[]*\[.*\]")
_BREAK = re.compile(r"'{3,}|<{3,}|(?:-[ \t]*){3}|(?:\*[ \t]*){3}|(?:_[ \t]*){3}")
_LINE_COMMENT = re.compile(r"//(?!/)|//$")
_SECTION = re.compile(r"(={1,6}|#{1,6})[ \t]+(?=\S)")
_BLOCK_TITLE = re.compile(r"\.(?=[^.\s])")
_ADMONITION = re.compile(r"(?:NOTE|TIP|IMPORTANT|CAUTION|WARNING):[ \t]+(?=\S)")
_LIST_ITEM = re.compile(
    r"[ \t]*(?:\*{1,5}|-|\.{1,5}|\d{1,9}\.|[a-zA-Z]\.|[ivxlcdmIVXLCDM]{1,10}\)|<\d{1,3}>|<\.>)"
    r"[ \t]+(?:\[[ xX*]\][ \t]+)?(?=\S)"
)
_DLIST = re.compile(r"([ \t]*)([^\s].*?)(:{2,4}|;;)(?:[ \t]+(?=\S)|$)")
_SETEXT = re.compile(r"(?:=+|-+|~+|\^+|\++)")
_VERBATIM_STYLES = frozenset(
    {
        "source",
        "listing",
        "literal",
        "pass",
        "comment",
        "stem",
        "latexmath",
        "asciimath",
        "plantuml",
        "ditaa",
        "graphviz",
        "mermaid",
        "svgbob",
        "a2s",
        "blockdiag",
    }
)
_TABLE_FMT_SKIP = re.compile(r"\bformat\s*=\s*\"?(?:csv|dsv|tsv)", re.IGNORECASE)
# A table cell specifier: span/duplication, alignment, style, e.g. 2+, 3*, .2+^.^a
_CELL_SPEC = re.compile(
    r"(?:^|(?<=[ \t]))((?:\d+\*)?(?:\d+(?:\.\d+)?\+|\.\d+\+)?[<^>]?(?:\.[<^>])?[adehlmsv]?)$"
)

# --- inline grammar -----------------------------------------------------------------
_PAIRED_MACROS = frozenset(
    {"link", "xref", "mailto", "footnote", "http", "https", "ftp", "ftps", "irc", "file"}
)
_INLINE_RE = re.compile(
    r"(?P<hb>[ \t]\+[ \t]*(?:\r\n|\r|\n)[ \t]*)"
    r"|(?P<attr>\\?\{(?:[A-Za-z0-9_][A-Za-z0-9_-]{0,100}|(?:set|counter2?):[^}\r\n]{1,200})\})"
    r"|(?P<pass3>\+\+\+[^\r\n]{0,500}?\+\+\+)"
    r"|(?P<passm>pass:[a-z,]*\[[^\]\r\n]{0,500}\])"
    r"|(?P<pass2>\+\+[^\r\n]{1,500}?\+\+)"
    r"|(?P<pass1>(?<![\w+])\+[^+\s](?:[^+\r\n]{0,500}?[^+\s])?\+(?![\w+]))"
    r"|(?P<mono2>``[^\r\n]{1,500}?``)"
    r"|(?P<mono1>(?<![\w`])`[^`\s](?:[^`\r\n]{0,500}?[^`\s])?`(?![\w`]))"
    r"|(?P<anchor>\[\[[^\[\]\r\n]{1,200}\]\])"
    r"|(?P<xref><<[^<>\r\n]{1,300}>>)"
    r"|(?P<macro>(?<![\w\\])(?P<mname>link|xref|mailto|footnote|footnoteref|image|kbd|btn|menu"
    r"|icon|stem|latexmath|asciimath|anchor|indexterm2?|https?|ftps?|irc|file)"
    r":(?P<mtarget>[^\s\[\]]{0,500})\[(?P<mtext>[^\]\r\n]{0,500})\])"
    r"|(?P<url>(?<![\w/\\<])(?:https?|ftps?|irc)://[^\s\[\]<>\"]*[^\s\[\]<>\".,;:!?)'])"
    r"|(?P<esc>\\[|!*_`#^~{\[<+])"
    r"|(?P<uf>(?P<ufrole>\[[^\[\]\r\n]{1,100}\])?(?P<ufm>\*\*|__|##)(?P<ufin>[^\r\n]{1,500}?)(?P=ufm))"
    r"|(?P<cf>(?<![\w*_#\\])(?P<cfrole>\[[^\[\]\r\n]{1,100}\])?(?P<cfm>[*_#])"
    r"(?P<cfin>(?!(?P=cfm))\S(?:[^\r\n]{0,500}?\S)?)(?P=cfm)(?![\w*_#]))"
    r"|(?P<ss>(?P<ssm>[\^~])(?P<ssin>[^\s^~]{1,200})(?P=ssm))"
)
_SOFT_BREAK = re.compile(r"(?:[ \t]*(?:\r\n|\r|\n))+[ \t]*")
# Line-start syntax that would change a translated paragraph's block type (gate fix).
_BLOCK_START = re.compile(
    r"(?:={1,6}|#{1,6})(?:[ \t]|$)"
    r"|\.(?=[^.\s])|\[|//|:!?[A-Za-z0-9_][A-Za-z0-9_-]*!?:"
    r"|(?:NOTE|TIP|IMPORTANT|CAUTION|WARNING):[ \t]"
    r"|(?:\*{1,5}|-|\.{1,5}|\d{1,9}\.|[a-zA-Z]\.|[ivxlcdmIVXLCDM]{1,10}\)|<\d{1,3}>|<\.>)"
    r"(?:[ \t]|$)"
    r"|-{2,}|\.{4,}|\+|_{4,}|\*{4,}|'{3,}|<{3,}|[|!,:]={3,}|```"
    r"|\\?(?:include|ifdef|ifndef|ifeval|endif)::"
)
_MAX_TOKENS = 1000
_MAX_DEPTH = 8


class NotAsciiDocError(EmptyDocumentError):
    """A ``.asc`` upload that is an OpenPGP armored file, not AsciiDoc (HTTP 422)."""


def _lines(text: str):
    """Yield (start, end, content) per line; end excludes the terminator."""
    for m in _LINE_RE.finditer(text):
        if m.start() == len(text) and not m.group(0):
            break
        yield m.start(1), m.end(1), m.group(1)
        if not m.group(2):
            break


def _first_newline(text: str) -> str:
    m = _NEWLINE_RE.search(text)
    return m.group(0) if m else "\n"


def _block_style(attr_line: str) -> str:
    """First positional of a block attribute line: ``[source%linenums,python]`` -> source."""
    inner = attr_line.strip()[1:-1]
    first = inner.split(",", 1)[0].strip()
    return re.split(r"[#.%]", first, maxsplit=1)[0].strip().lower()


# --------------------------------------------------------------------------- tokens


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
    return _SOFT_BREAK.sub(" ", s)


def _tokenize(s: str, tok: _Tok, depth: int = 0) -> str:
    out: list[str] = []
    pos = 0
    for m in _INLINE_RE.finditer(s):
        if tok.n >= _MAX_TOKENS:
            break
        out.append(_plain(s[pos : m.start()]))
        pos = m.end()
        raw = m.group(0)
        if depth >= _MAX_DEPTH:
            out.append(tok.point(raw))
        elif m.group("macro") is not None:
            name, text = m.group("mname"), m.group("mtext")
            if (
                name in _PAIRED_MACROS
                and any(c.isalpha() for c in text)
                and "=" not in text
                and '"' not in text
            ):
                core = text.rstrip("^")
                lead = len(core) - len(core.lstrip())
                head = raw[: m.start("mtext") - m.start()] + core[:lead]
                out.append(tok.pair(head, core[lead:], text[len(core) :] + "]", depth))
            else:
                out.append(tok.point(raw))
        elif m.group("xref") is not None:
            body = raw[2:-2]
            ref, sep, label = body.partition(",")
            if sep and any(c.isalpha() for c in label):
                lead = len(label) - len(label.lstrip())
                out.append(tok.pair("<<" + ref + "," + label[:lead], label[lead:], ">>", depth))
            else:
                out.append(tok.point(raw))
        elif m.group("uf") is not None:
            role, mark = m.group("ufrole") or "", m.group("ufm")
            out.append(tok.pair(role + mark, m.group("ufin"), mark, depth))
        elif m.group("cf") is not None:
            role, mark = m.group("cfrole") or "", m.group("cfm")
            out.append(tok.pair(role + mark, m.group("cfin"), mark, depth))
        elif m.group("ss") is not None:
            mark = m.group("ssm")
            out.append(tok.pair(mark, m.group("ssin"), mark, depth))
        else:  # hb, attr, pass*, mono*, anchor, url, esc
            out.append(tok.point(raw))
    out.append(_plain(s[pos:]))
    return "".join(out)


def _unit_of(raw: str) -> tuple[str, dict]:
    tok = _Tok()
    text = _tokenize(raw, tok).strip()
    return text, tok.map


def _render(translation: str, imap: dict, cell_sep: str | None, attrs: dict | None = None) -> str:
    """Write a translated unit back as AsciiDoc: tokens back to their markup, any line
    break folded to a space (a blank line would end the block), a table separator in the
    translated words escaped. A constrained mark the translation glued to a word
    character is written in its unconstrained form (#695, see ``_unconstrain``)."""
    t = translation.strip()
    out: list[str] = []
    opens: dict[str, int] = {}  # constrained pair key -> index of its opening piece in out
    marks: list[tuple[int, int, str]] = []  # (open piece, close piece, mark)
    links: list[int] = []  # pieces that start a URL link (#698)
    pos = 0
    for m in _PLACEHOLDER_RE.finditer(t):
        out.append(_text_part(t[pos : m.start()], cell_sep))
        pos = m.end()
        inner = m.group(0)[1:-1]
        closing = inner.startswith("/")
        key = inner.lstrip("/")[1:]
        val = imap.get(key)
        if val is None:
            continue  # unknown token: dropped
        if isinstance(val, list):
            mark = _constrained_pair_mark(val)
            if mark:
                if not closing:
                    opens[key] = len(out)
                elif key in opens:
                    marks.append((opens.pop(key), len(out), mark))
            if not closing and _URL_START.match(val[0]):
                links.append(len(out))
            out.append(val[1] if closing else val[0])
        elif not closing:
            if _constrained_point(val):
                marks.append((len(out), len(out), val[0]))
            if _URL_START.match(val) or _ATTR_REF.fullmatch(val):
                links.append(len(out))
            out.append(val)
    out.append(_text_part(t[pos:], cell_sep))
    _unconstrain(out, marks)
    return _explicit_links(out, links, attrs or {})


# #698: Asciidoctor links a bare URL (also ``https://x[text]``, and an attribute reference
# that holds one, ``{uri-x}[text]``: attributes are substituted first) only after the start
# of the line, a space or one of ``< > ( ) [ ] ; " '`` (InlineLinkRx); a bare URL also runs
# on until whitespace, so a following CJK letter joins it. A translation without spaces
# glues the token to letters, and the reader sees the raw URL. The ``link:`` macro has no
# such rule and renders the same link.
_URL_START = re.compile(r"(?:https?|ftps?|irc|file)://")
_ATTR_REF = re.compile(r"\{[A-Za-z0-9_][A-Za-z0-9_-]*\}")
_LINK_BEFORE_OK = frozenset(" \t<>()[];\"'")
# A bare URL runs on over any of these and only drops a trailing ", . ? ! )" (gate):
# "https://x.org,现在" links "https://x.org,现在".
_BARE_URL_TAIL = re.compile(r"[^\s\[\]<]*")
_DOC_ATTR = re.compile(r"^:([A-Za-z0-9_][A-Za-z0-9_-]*):[ \t]*([^\r\n]*)$", re.MULTILINE)


def _doc_attrs(text: str) -> dict[str, bool]:
    """Attribute name -> whether the document defines it as a URL (gate, #698)."""
    return {
        m.group(1): bool(_URL_START.match(m.group(2).strip())) for m in _DOC_ATTR.finditer(text)
    }


def _explicit_links(out: list[str], links: list[int], attrs: dict[str, bool]) -> str:
    if not links:
        return "".join(out)
    joined = "".join(out)
    starts = []
    n = 0
    for piece in out:
        starts.append(n)
        n += len(piece)
    for i in links:
        start, end = starts[i], starts[i] + len(out[i])
        before = joined[start - 1] if start > 0 else ""
        after = joined[end] if end < len(joined) else ""
        raw = out[i]
        attr = _ATTR_REF.fullmatch(raw) is not None
        if attr:
            # The document's own definition decides; an attribute set elsewhere (CLI,
            # docinfo) counts as a link target only when "[text]" follows it.
            is_url = attrs.get(raw[1:-1])
            if is_url is False or (is_url is None and after != "["):
                continue
        glued = bool(before) and before not in _LINK_BEFORE_OK
        if (attr and after == "[") or "[" in raw:  # {uri}[text], https://x[, https://x[]
            if glued:
                out[i] = "link:" + raw
        else:  # a bare URL (or URL attribute): must not run on into the next letters
            tail = _BARE_URL_TAIL.match(joined, end).group(0).rstrip(",.?!)")
            if glued or tail:
                out[i] = "link:" + raw + "[]"
    return "".join(out)


# #695: AsciiDoc constrained marks (single `` ` ``, ``*``, ``_``, ``#``, ``+``) only format
# when the outside neighbour is not a word character (Asciidoctor's InlineQuote rules:
# "(^|[^\p{Word};:}])*text*(?!\p{Word})", plus a few punctuation exceptions per mark).
# Japanese and Chinese have no spaces, so a translation glues the mark to a letter and
# Asciidoctor shows the literal backticks/asterisks. The unconstrained (doubled) form
# formats anywhere with the same rendering.
_CONSTRAINED_BAD_BEFORE = {"*": ";:}", "_": ";:}", "#": "&;:}", "`": ";:\"'`}", "+": ";:\\"}
_CONSTRAINED_BAD_AFTER = {"`": "\"'`"}


def _constrained_pair_mark(val: list) -> str:
    """The mark of a constrained ``*bold*`` / ``_it_`` / ``#mark#`` pair (optionally
    with a ``[.role]``), else ''."""
    open_ = val[0]
    mark = open_[-1:]
    if mark not in ("*", "_", "#") or val[1] != mark:
        return ""
    if len(open_) > 1 and open_[-2] == mark:
        return ""  # already unconstrained
    return mark


def _constrained_point(raw: str) -> bool:
    """A constrained monospace `` `code` `` or passthrough ``+text+`` point token."""
    return len(raw) >= 3 and raw[0] in "`+" and raw[-1] == raw[0] and raw[1] != raw[0]


def _wordish(c: str) -> bool:
    """Ruby's ``\\p{Word}`` (letters, marks, digits, connector punctuation such as the
    full-width low line ``＿`` a Japanese translation may use)."""
    cat = unicodedata.category(c)
    return c.isalnum() or cat[0] == "M" or cat == "Pc"


def _unconstrain(out: list[str], marks: list[tuple[int, int, str]]) -> str:
    """Rewrite ``out`` in place (the pieces of glued constrained marks doubled)."""
    if not marks:
        return "".join(out)
    ends = []
    n = 0
    for piece in out:
        n += len(piece)
        ends.append(n)
    joined = "".join(out)
    for a, b, mark in marks:
        start, end = ends[a] - len(out[a]), ends[b]
        before = joined[start - 1] if start > 0 else ""
        after = joined[end] if end < len(joined) else ""
        bad = bool(before) and (_wordish(before) or before in _CONSTRAINED_BAD_BEFORE[mark])
        bad = bad or (
            bool(after) and (_wordish(after) or after in _CONSTRAINED_BAD_AFTER.get(mark, ""))
        )
        if a != b:  # a pair: the translated text inside must not start or end with a space
            inner = joined[ends[a] : ends[b] - len(out[b])]
            bad = bad or not inner or inner[0].isspace() or inner[-1].isspace()
        if not bad:
            continue
        if a == b:
            out[a] = mark + out[a] + mark
        else:
            out[a] = out[a] + mark
            out[b] = mark + out[b]
    return "".join(out)


def _text_part(s: str, cell_sep: str | None) -> str:
    s = _SOFT_BREAK.sub(" ", s)
    if cell_sep:
        s = re.sub(r"(?<!\\)" + re.escape(cell_sep), "\\\\" + cell_sep, s)
    return s


# --------------------------------------------------------------------------- block walk


def _slots(text: str) -> list[dict]:
    """Every translatable span in document order: {start, end, sep} where sep is the
    table cell separator for a cell (escaped on build) or None."""
    lines = list(_lines(text))
    slots: list[dict] = []
    para: dict | None = None
    verbatim: str | None = None  # closing delimiter of the verbatim block we are in
    table: dict | None = None  # {"close": "|===", "sep": "|", "style": ""}
    nested: dict | None = None  # a !=== table inside an a| cell
    pending = ""  # last block attribute line, applies to the next block
    skip_para = False  # inside a literal / verbatim-styled paragraph
    header = False  # inside the document header (after the level-0 title)
    seen_block = False
    attr_cont = False  # attribute entry value continues (`` \``)
    i = 0

    def add(start: int, end: int, sep: str | None = None):
        while start < end and text[start].isspace():
            start += 1
        while end > start and text[end - 1].isspace():
            end -= 1
        if start < end:
            slots.append({"start": start, "end": end, "sep": sep})
            check_segment_cap(len(slots))

    def flush():
        nonlocal para
        if para is not None:
            add(para["start"], para["end"])
            para = None

    while i < len(lines):
        s, e, body = lines[i]
        i += 1
        rs = body.rstrip()
        if verbatim is not None:
            if rs == verbatim:
                verbatim = None
            continue
        if attr_cont:
            attr_cont = rs.endswith(" \\") or rs.endswith("\t\\")
            continue
        if _PREPROC.match(body):
            flush()
            continue  # never followed; kept verbatim
        if not rs:
            flush()
            skip_para = False
            header = False
            continue
        if skip_para:
            continue
        if para is not None and _SETEXT.fullmatch(rs) and _setext(para, text, rs):
            flush()  # the one-line paragraph above is a two-line section title
            continue
        if _ATTR_ENTRY.match(body) and para is None:
            attr_cont = rs.endswith(" \\") or rs.endswith("\t\\")
            continue
        if _VERBATIM_DELIM.fullmatch(rs) or _FENCE_OPEN.fullmatch(rs):
            flush()
            verbatim = "```" if rs.startswith("```") else rs
            pending = ""
            seen_block = True
            continue
        if _LINE_COMMENT.match(body):
            flush()
            continue
        tm = _TABLE_DELIM.fullmatch(rs)
        cur = nested or table
        if cur is not None:
            if rs == cur["close"]:
                if nested is not None:
                    nested = None
                else:
                    table = None
                continue
            if tm and tm.group(1) == "!" and nested is None:
                nested = {"close": rs, "sep": "!", "style": ""}
                continue
            _cells(text, s, body, cur, add)
            continue
        if tm:
            flush()
            seen_block = True
            if tm.group(1) in ",:" or _TABLE_FMT_SKIP.search(pending):
                verbatim = rs
            else:
                table = {"close": rs, "sep": tm.group(1), "style": ""}
            pending = ""
            continue
        if _COMPOUND_DELIM.fullmatch(rs):
            flush()
            seen_block = True
            if pending and _block_style(pending) in _VERBATIM_STYLES:
                verbatim = rs
            pending = ""
            continue
        if header:
            continue  # author / revision line of the document header
        if para is None and _BLOCK_ATTR.fullmatch(rs):
            pending = rs
            continue
        if para is None and _BLOCK_MACRO.fullmatch(rs):
            pending = ""
            seen_block = True
            continue
        if para is None and _BREAK.fullmatch(rs):
            continue
        if rs == "+":
            flush()
            continue
        sm = _SECTION.match(body)
        if sm and para is None:
            core_end = e
            marker = sm.group(1)
            if marker[0] == "=":
                tail = re.search(r"[ \t]+" + re.escape(marker) + r"[ \t]*$", body)
                if tail and tail.start() >= sm.end():
                    core_end = s + tail.start()
            add(s + sm.end(), core_end)
            if len(marker) == 1 and not seen_block:
                header = True
            seen_block = True
            pending = ""
            continue
        seen_block = True
        bt = _BLOCK_TITLE.match(body)
        if bt and para is None:
            add(s + 1, e)
            continue
        lm = _LIST_ITEM.match(body)
        if lm:
            flush()
            pending = ""
            para = {"start": s + lm.end(), "end": e}
            continue
        dm = _DLIST.match(rs) if para is not None or not body[:1].isspace() else None
        if dm:  # a description list term (it also ends the previous item's text)
            flush()
            add(s + dm.start(2), s + dm.end(2))
            rest = s + dm.end()
            if rest < s + len(rs):
                para = {"start": rest, "end": e}
            pending = ""
            continue
        if para is None:
            style = _block_style(pending) if pending else ""
            pending = ""
            if style in _VERBATIM_STYLES or body[:1] in (" ", "\t"):
                skip_para = True
                continue
            am = _ADMONITION.match(body)
            para = {"start": s + (am.end() if am else 0), "end": e, "lines": 1}
            continue
        para["end"] = e
        para["lines"] = para.get("lines", 1) + 1
    flush()
    return slots


def _setext(para: dict, text: str, underline: str) -> bool:
    """A one-line plain paragraph underlined to within one character of its length is
    a two-line section title (Asciidoctor setext titles); it stays one unit."""
    if para.get("lines") != 1:
        return False
    title = text[para["start"] : para["end"]].rstrip()
    if not title or title[0] in ".[" or not any(c.isalnum() for c in title):
        return False
    return len(underline) >= 2 and abs(len(title) - len(underline)) <= 1


def _cells(text: str, s: int, body: str, table: dict, add) -> None:
    """Split one table line into cell spans (unescaped separators), keeping each cell's
    specifier (span, alignment, style) outside the unit."""
    sep = table["sep"]
    seps = [k for k, c in enumerate(body) if c == sep and (k == 0 or body[k - 1] != "\\")]
    if not seps:
        if table["style"] != "l":
            add(s, s + len(body), sep)
        return
    bounds = [(-1, seps[0])] + [(seps[j], seps[j + 1]) for j in range(len(seps) - 1)]
    bounds.append((seps[-1], len(body)))
    for j, (a, b) in enumerate(bounds):
        start, end = a + 1, b
        nxt_style = None
        if j < len(bounds) - 1:  # text before a separator may end in the next cell's spec
            seg = body[start:end]
            sm = _CELL_SPEC.search(seg)
            if sm and sm.group(1):
                end = start + sm.start(1)
                spec = sm.group(1)
                nxt_style = spec[-1] if spec[-1] in "adehlmsv" else ""
            else:
                nxt_style = ""
        # j == 0: text before the first separator continues the previous cell.
        if table["style"] != "l":
            add(s + start, s + end, sep)
        if nxt_style is not None:
            table["style"] = nxt_style


# --------------------------------------------------------------------------- filter


def _decode(data: bytes) -> tuple[str, str]:
    text, enc = read_text_upload(data, sig=True)  # #509 refusal + #571 line budget
    if text.lstrip().startswith("-----BEGIN PGP"):
        raise NotAsciiDocError(
            "This .asc file is an OpenPGP signature or key, not an AsciiDoc document."
        )
    return text, enc


def _units(text: str) -> list[tuple[dict, str, dict]]:
    out = []
    for slot in _slots(text):
        unit, imap = _unit_of(text[slot["start"] : slot["end"]])
        out.append((slot, unit, imap))
    return out


def _translatable(unit: str) -> bool:
    return any(c.isalpha() for c in strip_placeholders(unit))


@register
class AsciiDocFilter(Filter):
    fmt = "adoc"
    exts = ("asciidoc", "asc")

    def parse(self, data: bytes, provider: str | None = None) -> ParseResult:
        text, enc = _decode(data)
        check_unit_texts(text[s["start"] : s["end"]] for s in _slots(text))
        units: list[TextUnit] = []
        for _slot, unit, imap in _units(text):
            if _translatable(unit):
                units.append(TextUnit(p_index=len(units), text=unit, inline_map=imap))
        for meta in _header_meta(text):  # #733: appended after every body unit
            unit, imap = _meta_unit(meta["value"])
            units.append(
                TextUnit(p_index=len(units), text=unit, style=meta["style"], inline_map=imap)
            )
            check_segment_cap(len(units))
        check_unit_texts(u.text for u in units if u.style)
        skeleton = {
            "format": "adoc",
            "units": [{"p_index": u.p_index, "text": u.text, "style": u.style} for u in units],
        }
        return ParseResult(skeleton=note_source_encoding(skeleton, enc), units=units)

    def build(self, original: bytes, paragraph_texts: dict) -> bytes:
        return self._build(original, paragraph_texts, None)

    def build_localized(
        self, original: bytes, paragraph_texts: dict, target_locale: str | None = None
    ) -> bytes:
        return self._build(original, paragraph_texts, target_locale)

    def _build(self, original: bytes, paragraph_texts: dict, lang: str | None) -> bytes:
        raw = bytes(original)
        text = decode_text_lossless(raw, sig=True)
        out: list[str] = []
        attrs: dict[str, bool] | None = None
        pos = 0
        slots = [(slot, unit, imap) for slot, unit, imap in _units(text) if _translatable(unit)]
        for meta in _header_meta(text):  # #733: numbered after the body units
            unit, imap = _meta_unit(meta["value"])
            slots.append(({"start": meta["start"], "end": meta["end"], "meta": True}, unit, imap))
        order = sorted(range(len(slots)), key=lambda j: slots[j][0]["start"])
        for j in order:
            slot, unit, imap = slots[j]
            new = paragraph_texts.get(str(j))
            out.append(text[pos : slot["start"]])
            if new is None or new == unit:
                out.append(text[slot["start"] : slot["end"]])
            elif slot.get("meta"):
                out.append(_meta_render(str(new), imap) or text[slot["start"] : slot["end"]])
            else:
                if attrs is None:
                    attrs = _doc_attrs(text)
                rendered = _render(new, imap, slot["sep"], attrs)
                at_line_start = slot["start"] == 0 or text[slot["start"] - 1] in "\r\n"
                if at_line_start and slot["sep"] is None and _BLOCK_START.match(rendered):
                    # A translation that opens with block syntax (a "== " title, a list
                    # marker, ".Title", "[attr]", "NOTE: ", "//", a delimiter...) would turn
                    # the paragraph into another block; {empty} is AsciiDoc's own guard.
                    rendered = "{empty}" + rendered
                out.append(rendered)
            pos = slot["end"]
        out.append(text[pos:])
        result = "".join(out)
        if lang:
            result = set_lang(result, lang)
        body = encode_text_lossless(result)
        return _BOM + body if raw.startswith(_BOM) else body


# --------------------------------------------------------------------------- #733 metadata
# The header's description / keywords attribute entries (Asciidoctor writes them to
# <meta name="description|keywords">). A value continues on the next line when it ends in
# " \" (Asciidoctor's line continuation); continued lines are joined with one space.
_META_ENTRY = re.compile(r":(description|keywords):(?:[ \t]+|$)")
_CONTINUED = re.compile(r"[ \t]\\$")
_META_ATTR_REF = re.compile(r"\\?\{[A-Za-z0-9_][A-Za-z0-9_-]{0,100}\}")


def _header_meta(text: str) -> list[dict]:
    """{start, end, style} of each description / keywords value in the document header
    (leading comments, then a title or attribute entries, until a blank line: the same
    header set_lang finds). An entry whose value holds a hard line break (" +" before a
    continuation) is left alone."""
    out: list[dict] = []
    lines = list(_lines(text))
    started = False
    i = 0
    while i < len(lines):
        s, e, body = lines[i]
        i += 1
        if not body.strip():
            if started:
                break
            continue
        if not started:
            if _LINE_COMMENT.match(body):
                continue
            sm = _SECTION.match(body)
            if not ((sm and sm.group(1) == "=") or _ATTR_ENTRY.match(body)):
                break
            started = True
        m = _META_ENTRY.match(body)
        if not m:
            while _CONTINUED.search(body) and i < len(lines):  # skip its continuation lines
                _s, _e, body = lines[i]
                i += 1
            continue
        parts = [body[m.end() :]]
        end = e
        while _CONTINUED.search(parts[-1]) and i < len(lines) and lines[i][2].strip():
            parts[-1] = parts[-1][:-1].rstrip()
            _s, end, nxt = lines[i]
            i += 1
            parts.append(nxt.strip())
        if any(p.endswith(" +") or p == "+" for p in parts[:-1]):
            continue  # hard line break inside the value
        value = " ".join(p.strip() for p in parts if p.strip())
        if any(c.isalpha() for c in _META_ATTR_REF.sub("", value)):
            out.append(
                {"start": s + m.end(), "end": end, "style": "meta:" + m.group(1), "value": value}
            )
    return out


def _meta_unit(value: str) -> tuple[str, dict]:
    """The unit text of a header value: attribute references are point tokens."""
    imap: dict = {}

    def point(m):
        imap[str(len(imap) + 1)] = m.group(0)
        return ph_point("x", len(imap))

    return _META_ATTR_REF.sub(point, value), imap


def _meta_render(translation: str, imap: dict) -> str:
    """A translated header value on ONE line: line breaks folded to spaces, tokens back
    to their attribute references, and a trailing backslash written as {backslash} so the
    value can never end in Asciidoctor's " \" line continuation."""
    out: list[str] = []
    pos = 0
    for m in _PLACEHOLDER_RE.finditer(translation):
        out.append(translation[pos : m.start()])
        pos = m.end()
        inner = m.group(0)[1:-1]
        val = imap.get(inner.lstrip("/")[1:])
        if isinstance(val, str) and not inner.startswith("/"):
            out.append(val)
    out.append(translation[pos:])
    t = " ".join("".join(out).split())
    n = len(t) - len(t.rstrip("\\"))
    if n:
        t = t[: len(t) - n] + "{backslash}" * n
    return t


_LANG_ENTRY = re.compile(r"^:lang:[ \t]*([^\r\n]*)$", re.MULTILINE)
_SAFE_LANG = re.compile(r"[A-Za-z]{2,8}(?:[-_][A-Za-z0-9]{1,8}){0,4}")


def set_lang(text: str, lang: str) -> str:
    """Declare the target language in the document header (``:lang:``, used by
    Asciidoctor for the HTML ``lang`` attribute). An existing header entry is
    rewritten; otherwise one is added at the end of the header, or a header holding
    only that entry is added at the top. A malformed locale changes nothing."""
    if not _SAFE_LANG.fullmatch(lang or ""):
        return text
    nl = _first_newline(text)
    # The header: leading comment lines, then a title or attribute entries, until a blank line.
    pos = 0
    header_start = None
    for s, e, body in _lines(text):
        if not body.strip():
            if header_start is None:
                pos = e
                continue
            break
        if header_start is None:
            if _LINE_COMMENT.match(body):
                continue
            sm = _SECTION.match(body)
            if (sm and sm.group(1) == "=") or _ATTR_ENTRY.match(body):
                header_start = s
            else:
                break
        pos = e
    if header_start is None:
        return f":lang: {lang}{nl}{nl}" + text
    head = text[header_start:pos]
    m = _LANG_ENTRY.search(head)
    if m:
        a, b = header_start + m.start(1), header_start + m.end(1)
        return text[:a] + lang + text[b:]
    return text[:pos] + f"{nl}:lang: {lang}" + text[pos:]


__all__ = ["AsciiDocFilter", "NotAsciiDocError", "set_lang"]
