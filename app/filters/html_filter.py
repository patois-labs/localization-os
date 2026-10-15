"""HTML filter — block-level units with inline-tag fidelity.

Tokenizes the document into tags (``<...>``) and the text between them. Text
inside <script>/<style> is skipped; every structural tag is left byte-for-byte
identical, so attributes, structure, scripts, and styles survive the round-trip.

Inline-tag fidelity (Phase 5 chunk 6 — mirrors docx_filter.py / xliff_filter.py):
a BLOCK-level element (p, li, h1-6, td, div, ...) is ONE translatable unit. Its
mid-sentence INLINE tags (b, i, em, strong, a, span, code, u, ...) are converted
to paired placeholder tokens (⟦g1⟧…⟦/g1⟧, see services/segment) and its
self-closing inline tags (br, img) to standalone ⟦x1⟧ tokens, so the whole
sentence stays ONE context-complete unit that segments, translates, and
round-trips as a piece. build() re-derives the token->original-tag map from the
ORIGINAL bytes (never persisted through the DB, exactly like docx/xliff) and
re-materializes the exact original inline tags around the translated words.

A block with NO inline tags (a plain ``<p>Sentence one. Sentence two.</p>``) is
carried as plain text with no tokens — byte-identical to the pre-fidelity
behavior. Non-inline tags (block, comments, doctype, unknown) act as unit
boundaries: text on either side of one is never merged, exactly as before.

#734: content under the HTML5 ``translate="no"`` attribute (or Google's
``class="notranslate"``) is not offered: a block holding only such content makes no
unit, and an inline run of it inside a translatable block is one point token written
back verbatim. ``translate="yes"`` re-enables a subtree (see _TranslateScope).
"""

import html as _html
import re
from bisect import bisect_left

from app import config
from app.filters import a11y
from app.filters.base import (
    Filter,
    InputLimitError,
    ParseResult,
    TextUnit,
    check_segment_cap,
    decode_text_lossless,
    encode_like,
    encode_text_lossless,
    note_source_encoding,
    read_text_upload,
    register,
    text_envelope,
)
from app.services.segment import _PLACEHOLDER_RE, ph_close, ph_open, ph_point, strip_placeholders

_TAG_NAME_RE = re.compile(r"<(/?)([a-zA-Z][^\t\n\f\r />]*)")
_SKIP_CONTAINERS = ("script", "style")


class _TextToken(str):
    """Text stays text even when its bytes happen to look like a complete tag."""


class _MarkupToken(str):
    """Comments and CDATA remain skeleton even when their closing delimiter is missing."""


def _is_tag_token(tok: str) -> bool:
    return isinstance(tok, _MarkupToken) or (
        not isinstance(tok, _TextToken) and tok.startswith("<") and tok.endswith(">")
    )


# Inline (phrasing) elements: their text flows inside the surrounding block, so
# they are tokenized INTO the block unit rather than ending it. Everything else
# that is a tag (block-level, comments, doctype, unknown/custom) is a unit
# boundary — matching the pre-fidelity behavior of splitting text at every tag.
_INLINE_TAGS = frozenset(
    {
        "a",
        "abbr",
        "b",
        "bdi",
        "bdo",
        "big",
        "cite",
        "code",
        "data",
        "dfn",
        "em",
        "font",
        "i",
        "ins",
        "del",
        "kbd",
        "label",
        "mark",
        "output",
        "q",
        "rp",
        "rt",
        "ruby",
        "s",
        "samp",
        "small",
        "span",
        "strong",
        "sub",
        "sup",
        "time",
        "tt",
        "u",
        "var",
        # void / self-closing inline elements -> standalone point tokens
        "br",
        "img",
        "wbr",
    }
)
# Inline elements that never carry child content: emit a standalone point token.
_VOID_INLINE = frozenset({"br", "img", "wbr"})


def _split_ws(s: str):
    """Return (leading_ws, core, trailing_ws)."""
    core = s.strip()
    lead = s[: len(s) - len(s.lstrip())]
    trail = s[len(s.rstrip()) :] if core else ""
    return lead, core, trail


def _is_translatable(text: str) -> bool:
    return any(ch.isalnum() for ch in text)


def _parse_tag(tok: str):
    """Return (closing, name, self_closing) for a tag-shaped token."""
    m = _TAG_NAME_RE.match(tok)
    if not m:
        return False, "", False
    closing = bool(m.group(1))
    name = m.group(2).lower()
    self_closing = tok.rstrip(">").rstrip().endswith("/")
    return closing, name, self_closing


# --- #734: the HTML5 `translate` attribute -------------------------------------------
# https://html.spec.whatwg.org/multipage/dom.html#attr-translate: `translate="no"` marks an
# element's text (and its human-text attributes) as not to be translated; descendants
# inherit it and `translate="yes"` re-enables a subtree. Values are ASCII case-insensitive,
# the empty string means yes, anything else inherits. Google's `class="notranslate"`
# (Google Translate, Chrome, Weglot, many CMS plugins) counts as `no` when the element has
# no valid `translate` attribute of its own. The page-level <meta name="google"
# content="notranslate"> is NOT honoured: it only stops a browser offering translation.
# Any attribute, quoted or not, first occurrence wins (as in the HTML tokenizer). Quoted
# values are consumed whole, so a `translate=` inside another attribute's value is never
# read as one.
_ANY_ATTR_RE = re.compile(r"""([^\s"'>/=]+)(?:\s*=\s*(?:"([^"]*)"|'([^']*)'|([^\s>]+)))?""")
# Void elements never have content or an end tag, so they never open a scope.
_VOID_ELEMENTS = frozenset(
    [
        "area",
        "base",
        "br",
        "col",
        "embed",
        "hr",
        "img",
        "input",
        "link",
        "meta",
        "param",
        "source",
        "track",
        "wbr",
        "basefont",
        "bgsound",
        "frame",
        "keygen",
    ]
)
# Start tags that end an open <p> (HTML "close a p element"), so an unclosed
# `<p translate="no">` does not swallow the next paragraph.
_CLOSES_P = frozenset(
    [
        "address",
        "article",
        "aside",
        "blockquote",
        "details",
        "dialog",
        "div",
        "dl",
        "fieldset",
        "figcaption",
        "figure",
        "footer",
        "form",
        "h1",
        "h2",
        "h3",
        "h4",
        "h5",
        "h6",
        "header",
        "hgroup",
        "hr",
        "main",
        "menu",
        "nav",
        "ol",
        "p",
        "pre",
        "section",
        "summary",
        "table",
        "ul",
    ]
)
# Elements with an optional end tag: a start tag of a name on the right closes an open
# element named on the left, searching down the stack no further than the barrier names.
_IMPLIED_END = (
    ({"li"}, {"li"}, {"ul", "ol", "menu"}),
    ({"dt", "dd"}, {"dt", "dd"}, {"dl"}),
    ({"td", "th"}, {"td", "th"}, {"tr", "table"}),
    ({"tr"}, {"tr"}, {"table", "thead", "tbody", "tfoot"}),
    ({"thead", "tbody", "tfoot"}, {"thead", "tbody", "tfoot"}, {"table"}),
    ({"option"}, {"option"}, {"select", "datalist", "optgroup"}),
    ({"optgroup"}, {"optgroup", "option"}, {"select"}),
    ({"rt", "rp"}, {"rt", "rp"}, {"ruby"}),
)


def _tag_attr_values(tok: str) -> dict:
    """Unescaped attribute values of a start tag by lowercased name (first one wins);
    a bare attribute has the empty string as its value."""
    m = _TAG_NAME_RE.match(tok)
    if not m:
        return {}
    end = len(tok) - 1 if tok.endswith(">") else len(tok)
    out: dict = {}
    for a in _ANY_ATTR_RE.finditer(tok, m.end(), end):
        name = a.group(1).lower()
        if name not in out:
            raw = next((g for g in a.group(2, 3, 4) if g is not None), "")
            out[name] = _html.unescape(raw)
    return out


def _own_translate(tok: str):
    """True / False when this start tag sets the translate mode, None when it inherits."""
    if "translate" not in tok.lower():
        return None  # fast path: neither `translate=` nor the `notranslate` class
    attrs = _tag_attr_values(tok)
    value = attrs.get("translate")
    if value is not None and value.isascii():
        value = value.lower()
        if value in ("", "yes"):
            return True
        if value == "no":
            return False
    if "notranslate" in attrs.get("class", "").split():
        return False
    return None


_TRANSLATE_WORD_RE = re.compile("translate", re.I)


def _translate_scope(tokens: list[str]):
    """A _TranslateScope for this token stream, or None when no token even mentions
    `translate` (then every element is translatable and the walk skips the tracking)."""
    if _TRANSLATE_WORD_RE.search("".join(tokens)) is None:
        return None
    return _TranslateScope()


class _TranslateScope:
    """Tracks the inherited translate mode across a token stream (#734). ``feed`` takes
    every tag token outside <script>/<style> in document order and returns the mode of
    that tag (a start tag: its element; an end tag: the element it closes); ``current``
    is the mode of text at this point. Only an approximation of the HTML tree builder:
    void elements, end tags popping to their nearest open match (an orphan is ignored)
    and the common optional end tags (p, li, dt/dd, td/th, tr, option)."""

    def __init__(self):
        self.stack: list[tuple[str, bool]] = []
        self.open: dict[str, int] = {}  # open elements per name: an orphan costs O(1)

    @property
    def current(self) -> bool:
        return self.stack[-1][1] if self.stack else True

    def _cut(self, i: int) -> None:
        for name, _mode in self.stack[i:]:
            self.open[name] -= 1
        del self.stack[i:]

    def _pop_to(self, names, barriers) -> None:
        if not any(self.open.get(n) for n in names):
            return
        # A bounded look down the stack: a void or self-closed start tag pops nothing, so
        # a hostile page repeating one over thousands of open inline tags stays linear.
        for i in range(len(self.stack) - 1, max(-1, len(self.stack) - 65), -1):
            name = self.stack[i][0]
            if name in names:
                self._cut(i)
                return
            if name not in _INLINE_TAGS if barriers is None else name in barriers:
                return

    def feed(self, tok: str, closing: bool, name: str, self_closing: bool) -> bool:
        if not name or tok.startswith(("<!", "<?")):
            return self.current  # comment, doctype, CDATA, processing instruction
        if closing:
            if not self.open.get(name):
                return self.current
            for i in range(len(self.stack) - 1, -1, -1):
                if self.stack[i][0] == name:
                    mode = self.stack[i][1]
                    self._cut(i)
                    return mode
            return self.current  # not reached: the count says one is open
        if name in _CLOSES_P:
            self._pop_to(("p",), None)  # through open inline elements only
        for starts, closes, barriers in _IMPLIED_END:
            if name in starts:
                self._pop_to(closes, barriers)
        own = _own_translate(tok)
        mode = self.current if own is None else own
        if not (self_closing or name in _VOID_ELEMENTS):
            self.stack.append((name, mode))
            self.open[name] = self.open.get(name, 0) + 1
        return mode


def _iter_units(
    tokens: list[str],
    capped: bool = False,
    inline_tags: frozenset = _INLINE_TAGS,
    void_inline: frozenset = _VOID_INLINE,
) -> list[dict]:
    """Walk the flat token stream and return one dict per translatable BLOCK unit,
    in document order. Each dict: ``start``/``end`` (inclusive token-index span of
    the unit's inline content), ``lead``/``core``/``trail`` (the unit text with
    inline tags rendered as placeholder tokens, split into surrounding whitespace),
    and ``map`` (token-id -> original inline tag data for re-materialization).

    parse() reads ``core``/``map``; build() reads the span + ``lead``/``trail``/
    ``map``. Both call this on the SAME bytes so ids and ordering stay identical,
    which is why the map is never persisted (docx/xliff do the same).

    `capped` (parse only, #571): refuse as soon as the unit count passes the per-document
    cap instead of walking a hostile file to the end; build/export never pass it, so a
    document stored before the cap still exports.

    `inline_tags` / `void_inline` (lowercased names): the MadCap Flare filter widens the
    inline set with its MadCap:* phrasing elements; plain HTML uses the defaults.

    #734: text and inline tags under `translate="no"` (see _TranslateScope) are not
    offered. Each run of them inside a block becomes ONE point token whose stored markup
    is the run's exact original bytes, written back verbatim by build(); a block holding
    nothing else produces no unit at all. Boundary tags still end units as before."""
    units: list[dict] = []
    skip_depth = 0
    skip_name = ""
    buf_parts: list[str] = []  # rendered pieces (raw text or placeholder tokens)
    buf_indices: list[int] = []  # token indices this buffer consumed
    inline_map: dict = {}
    stack: list[tuple] = []  # open inline paired tags: (token_id, tag_name)
    counter = 0  # per-block token id (shared paired+point -> unique)
    scope = _translate_scope(tokens)
    run_id = 0  # #734: id of the point token the current no-translate run extends, or 0

    def protect(ti: int, tok: str) -> None:
        nonlocal counter, run_id
        if run_id:
            inline_map[str(run_id)]["point"] += tok
        else:
            counter += 1
            run_id = counter
            inline_map[str(counter)] = {"point": str(tok)}
            buf_parts.append(ph_point("x", counter))
        buf_indices.append(ti)

    def flush():
        nonlocal buf_parts, buf_indices, inline_map, stack, counter, run_id
        run_id = 0
        if buf_indices:
            full = "".join(buf_parts)
            lead, core, trail = _split_ws(full)
            if core and _is_translatable(strip_placeholders(core)):
                units.append(
                    {
                        "start": buf_indices[0],
                        "end": buf_indices[-1],
                        "lead": lead,
                        "core": core,
                        "trail": trail,
                        "map": dict(inline_map),
                    }
                )
                if capped:
                    check_segment_cap(len(units))
        buf_parts = []
        buf_indices = []
        inline_map = {}
        stack = []
        counter = 0

    for ti, tok in enumerate(tokens):
        if getattr(tok, "inline_point", False) and not skip_depth:
            # A server-page output expression (server_page_filter): a point token inside
            # the unit. Plain HTML never produces one, so .html/.htm are unaffected.
            run_id = 0  # its own point even inside a no-translate run (_fix_islands)
            counter += 1
            inline_map[str(counter)] = {"point": tok}
            buf_parts.append(ph_point("x", counter))
            buf_indices.append(ti)
            continue
        if _is_tag_token(tok):
            closing, name, self_closing = _parse_tag(tok)
            if skip_depth:
                if closing and name == skip_name:
                    skip_depth = 0
                continue
            if name in _SKIP_CONTAINERS:
                flush()
                if not closing:
                    skip_depth = 1
                    skip_name = name
                continue
            translate = scope.feed(tok, closing, name, self_closing) if scope else True
            if name in inline_tags and not translate:
                protect(ti, tok)
            elif name in inline_tags:
                run_id = 0
                if name in void_inline or self_closing:
                    counter += 1
                    inline_map[str(counter)] = {"point": tok}
                    buf_parts.append(ph_point("x", counter))
                    buf_indices.append(ti)
                elif closing:
                    # Pop the nearest matching open; an orphan close is dropped.
                    for si in range(len(stack) - 1, -1, -1):
                        if stack[si][1] == name:
                            oid = stack[si][0]
                            del stack[si]
                            inline_map[str(oid)]["close"] = tok
                            buf_parts.append(ph_close("g", oid))
                            buf_indices.append(ti)
                            break
                else:
                    counter += 1
                    inline_map[str(counter)] = {"open": tok, "close": None}
                    stack.append((counter, name))
                    buf_parts.append(ph_open("g", counter))
                    buf_indices.append(ti)
            else:
                # Block-level / comment / doctype / unknown tag, or inside a skip
                # container: a unit boundary. Never merges text across it.
                flush()
            continue
        # text token
        if skip_depth == 0 and tok and scope and not scope.current:
            protect(ti, tok)
        elif skip_depth == 0 and tok:
            run_id = 0
            buf_parts.append(tok)
            buf_indices.append(ti)
    flush()
    return units


def _rematerialize(text: str, inline_map: dict, protected: tuple = (), quotes: bool = True) -> str:
    """Replace placeholder tokens in translated ``text`` with their original inline
    tags. Tolerant like docx/xliff: a token with no map entry (or a close with no
    stored close tag) is DROPPED rather than left as literal ⟦…⟧ text, so a mock/LLM
    that reorders or invents tokens can never leak bracket noise into the output.

    ``quotes`` (default True, the safe choice) also escapes ``"`` and ``'``; a caller
    passes False only for a page where ``_raw_quotes_safe`` holds (#672)."""
    out: list[str] = []
    pos = 0
    for m in _PLACEHOLDER_RE.finditer(text):
        out.append(_escape_text(text[pos : m.start()], protected, quotes=quotes))
        pos = m.end()
        inner = m.group(0)[1:-1]  # strip ⟦ ⟧
        if inner.startswith("/"):
            key = re.sub(r"^/[a-z]?", "", inner)
            info = inline_map.get(key)
            if info and info.get("close"):
                out.append(info["close"])
        else:
            is_point = inner[:1] == "x"
            key = re.sub(r"^[a-z]?", "", inner, count=1)
            info = inline_map.get(key)
            if info is None:
                continue
            if is_point:
                out.append(info.get("point") or "")
            else:
                out.append(info.get("open") or "")
    out.append(_escape_text(text[pos:], protected, quotes=quotes))
    return "".join(out)


# #672 gate: a text node cannot open an attribute ONLY while this scanner and the
# browser's HTML5 tokenizer agree on where every tag and comment ends. They disagree on
# a few malformed constructs (an abrupt `<!-->` comment, `--!>`, `<![CDATA[` outside SVG,
# a quote after `= =`, `</ x>`, raw-text elements such as <xmp>/<noscript>/<textarea>,
# <style>/<script> inside SVG). There the browser can be INSIDE a quoted attribute value
# where we see text, and a translated `"` would close it and add an attribute such as
# onfocus=. So quotes in text are written raw only for a page with none of those
# constructs; anything doubtful keeps the old `&quot;` / `&#x27;` escaping.
_RAW_TEXT_ELEMENTS = frozenset(
    ("xmp", "iframe", "noembed", "noframes", "noscript", "textarea", "title", "plaintext")
)
_FOREIGN_ROOTS = frozenset(("svg", "math"))
_TAG_WS = " \t\n\f\r"
_BOGUS_END_TAG_RE = re.compile(r"</(?![a-zA-Z>])")


def _html5_tag_end(tok: str) -> int:
    """Where an HTML5 tokenizer ends the start or end tag at the beginning of ``tok``
    (index just past its ``>``), or -1 when it never ends inside ``tok``. Follows the
    tag-name / attribute-name / attribute-value states of the WHATWG tokenizer: a quote
    opens a value only in the before-attribute-value state."""
    n = len(tok)
    i = 2 if tok.startswith("</") else 1
    while i < n and tok[i] not in _TAG_WS + "/>":
        i += 1  # tag name
    state = "before_name"
    while i < n:
        c = tok[i]
        if state == "dq" or state == "sq":
            close = tok.find('"' if state == "dq" else "'", i)
            if close == -1:
                return -1
            i = close + 1
            state = "after_quoted"
            continue
        if c == ">":
            return i + 1
        if state == "before_name":
            if c not in _TAG_WS and c != "/":
                state = "name"  # any other character (even = or a quote) starts a name
        elif state == "name":
            if c in _TAG_WS:
                state = "after_name"
            elif c == "/":
                state = "before_name"
            elif c == "=":
                state = "before_value"
        elif state == "after_name":
            if c == "=":
                state = "before_value"
            elif c == "/":
                state = "before_name"
            elif c not in _TAG_WS:
                state = "name"
        elif state == "before_value":
            if c == '"':
                state = "dq"
            elif c == "'":
                state = "sq"
            elif c not in _TAG_WS:
                state = "unquoted"
        elif state == "unquoted":
            if c in _TAG_WS:
                state = "before_name"
        elif state == "after_quoted":
            state = "before_name"
            continue  # reconsume
        i += 1
    return -1


def _raw_quotes_safe(tokens: list[str]) -> bool:
    """True when every markup boundary of this token stream is where an HTML5 browser
    puts it, so a text node is text in the browser too and its quotes may stay raw."""
    starts = set()
    pos = 0
    for tok in tokens:
        starts.add(pos)
        pos += len(tok)
    text = "".join(tokens)
    pos = 0
    raw_body = False  # the text token right after <script>/<style> is opaque raw text
    for tok in tokens:
        pos += len(tok)
        if not tok:
            continue
        if not _is_tag_token(tok):
            if not raw_body and _BOGUS_END_TAG_RE.search(tok):
                return False  # `</ x>`: the browser reads a bogus comment to the first `>`
            raw_body = False
            continue
        raw_body = False
        if tok.startswith("<!--"):
            if tok.startswith(("<!-->", "<!--->")) or "--!>" in tok:
                return False
            continue
        if tok.startswith(("<!", "<?")):  # doctype / CDATA / bogus comment: first `>` ends it
            if ">" in tok[:-1]:
                return False
            continue
        if _html5_tag_end(tok) != len(tok):
            return False
        closing, name, _self = _parse_tag(tok)
        if closing:
            continue
        raw_body = name in _SKIP_CONTAINERS
        if name in _FOREIGN_ROOTS:
            return False
        if name in _RAW_TEXT_ELEMENTS:
            # the browser reads raw text up to the first `</name` + delimiter: that must
            # be where one of our own tokens starts, or the two scans have parted ways
            m = re.compile(r"</" + re.escape(name) + r"[\t\n\f\r />]", re.I).search(text, pos)
            if m and m.start() not in starts:
                return False
    return True


_TEXT_ESCAPE_RE = re.compile(r"""&(?!(?:#[0-9]+|#[xX][0-9a-fA-F]+|[a-zA-Z][a-zA-Z0-9]+);)|[<>]""")
_QUOTED_ESCAPE_RE = re.compile(
    r"""&(?!(?:#[0-9]+|#[xX][0-9a-fA-F]+|[a-zA-Z][a-zA-Z0-9]+);)|[<>"']"""
)
_ESCAPES = {"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#x27;"}


def _escape_text(text: str, protected: tuple = (), quotes: bool = False) -> str:
    """Units contain escaped entities already; preserve them and escape literal markup.

    Text nodes only need a bare ``&``, ``<`` and ``>`` escaped (#672): an apostrophe or
    a double quote is written as itself, so a French ``N'arretez`` stays readable and a
    no-change rebuild is byte-identical. ``quotes=True`` also escapes ``"`` and ``'`` for
    a value that may end up inside an attribute (the connectors' legacy whole-field path).
    """
    if protected:
        pattern = "(" + "|".join(re.escape(s) for s in protected) + ")"
        return "".join(
            part if i % 2 else _escape_text(part, quotes=quotes)
            for i, part in enumerate(re.split(pattern, text))
        )
    rx = _QUOTED_ESCAPE_RE if quotes else _TEXT_ESCAPE_RE
    return rx.sub(lambda m: _ESCAPES[m.group()], text)


# #571 gate: runs that cannot change `_tag_end`'s state after their first character.
_TAG_PLAIN_RUN = re.compile(r"[^>\"'= \t\n\r\f]+")
_TAG_SPACE_RUN = re.compile(r"[ \t\n\r\f]+")


def _tag_end(text: str, pos: int, budget: list[int] | None = None) -> int:
    """Scan once to an unquoted >. An incomplete tag consumes the remaining text.

    `budget` (parse only, #571 gate): a one-item list of scan steps left for the whole
    document; a file of megabyte-long unquoted tags is refused instead of walked."""
    quote = ""
    value_start = False
    unquoted = False
    declaration = text[pos + 1] in "!?"
    i = pos + 1
    while i < len(text):
        if budget is not None:
            budget[0] -= 1
            if budget[0] < 0:
                raise InputLimitError(
                    "This HTML file has far more markup than MAX_DOCUMENT_SEGMENTS allows "
                    f"({config.MAX_DOCUMENT_SEGMENTS} segments). Split it into smaller files."
                )
        char = text[i]
        if quote:
            # #571 gate: nothing inside a quoted value matters until its closing quote,
            # so jump there at C speed (a multi-megabyte data: URI was a Python loop).
            close = text.find(quote, i)
            if close == -1:
                return -1
            quote = ""
            i = close + 1
            continue
        elif char == ">":
            return i + 1
        elif char in "\"'" and (value_start or declaration):
            quote = char
            value_start = False
        elif char == "=" and not unquoted:
            value_start = True
        elif char in " \t\n\r\f":
            unquoted = False
            i = _TAG_SPACE_RUN.match(text, i).end()  # the rest of the run changes nothing
            continue
        else:
            unquoted = unquoted or value_start
            value_start = False
            run = _TAG_PLAIN_RUN.match(text, i)
            if run:  # a quote or "=" here is not a plain character: one step as before
                i = run.end()
                continue
        i += 1
    return -1


def _raw_end(text: str, pos: int, name: str) -> int:
    """Find the HTML raw-text close, including script escaped/double-escaped states.

    Only ASCII tag-name delimiters count. In double-escaped script, </script>
    returns to escaped data; it does not close the element. --> exits either
    escaped state. No JavaScript/CSS quotes or apparent tags are tokenized.
    """
    state = "data"
    i = pos
    size = len(text)
    next_lt = -1  # cached position of the next "<" at or after i (#571 gate)
    while i < size:
        c = text[i]
        if c != "<" and (state == "data" or not text.startswith("-->", i)):
            # #571 gate: only "<" (and "-->" outside the data state) can change anything
            # below, so jump to the next one at C speed instead of one character per
            # loop; a 100 MB script body was seconds of interpreter time per upload.
            if next_lt < i:
                next_lt = text.find("<", i)
                if next_lt == -1:
                    next_lt = size
            nxt = next_lt
            if state != "data":
                close = text.find("-->", i, nxt + 2)
                if close != -1 and close < nxt:
                    nxt = close
            i = nxt
            continue
        if name == "script" and text.startswith("<!--", i) and state == "data":
            state = "escaped"
            # Revisit the dashes so <!--> and <!---> leave escaped data at >.
            i += 2
            continue
        if state != "data" and text.startswith("-->", i):
            state = "data"
            i += 3
            continue
        if text[i] == "<":
            closing = text.startswith("</", i)
            start = i + (2 if closing else 1)
            end = start + len(name)
            if text[start:end].lower() == name and end < len(text) and text[end] in " \t\n\r\f/>":
                if closing:
                    if state != "double":
                        return i
                    state = "escaped"
                elif name == "script" and state == "escaped":
                    state = "double"
                i = end
                continue
        i += 1
    return len(text)


def _tokenize(text: str, template_pattern=None, budget: list[int] | None = None) -> list[str]:
    """Forward-only HTML scanner; every consumed character is visited a bounded time.

    Tags own their quoted attributes, comments and CDATA own their entire contents,
    and raw-text bodies are opaque text. Failed tags consume EOF as text rather than
    retrying at each nested <. Template spans in ordinary text remain in that text.
    """
    templates = (
        {m.start(): m.end() for m in template_pattern.finditer(text)} if template_pattern else {}
    )
    starts = sorted(templates)
    tokens = []
    pos = 0
    i = 0
    size = len(text)
    while i < size:
        if i in templates:
            i = templates[i]
            continue
        if text[i] != "<" or i + 1 == size:
            # #571 gate: nothing before the next "<" or template start can begin a token,
            # so jump there at C speed (one character per loop was 3 s per 20 MB of text).
            nxt = text.find("<", i + 1)
            if nxt == -1:
                nxt = size
            k = bisect_left(starts, i + 1)
            if k < len(starts) and starts[k] < nxt:
                nxt = starts[k]
            i = nxt
            continue
        start = i + 1
        if text[start] == "/":
            start += 1
        if start == size or not (
            "a" <= text[start] <= "z"
            or "A" <= text[start] <= "Z"
            or (start == i + 1 and text[start] in "!?")
        ):
            i += 1
            continue
        if text.startswith("<!--", i) or text.startswith("<![CDATA[", i):
            delimiter = "-->" if text.startswith("<!--", i) else "]]>"
            end = text.find(delimiter, i + (4 if delimiter == "-->" else 9))
            end = size if end == -1 else end + len(delimiter)
        else:
            end = _tag_end(text, i, budget)
            if end == -1:
                break
        tokens.extend((_TextToken(text[pos:i]), _MarkupToken(text[i:end])))
        closing, name, _self_closing = _parse_tag(text[i:end])
        i = pos = end
        if not closing and name in _SKIP_CONTAINERS:
            i = _raw_end(text, pos, name)
            # The next scanner iteration emits the body as text and the close as a tag.
    tokens.append(_TextToken(text[pos:]))
    return tokens


def escape_fragment_translation(
    original: str,
    target: str,
    template_pattern=None,
    protected: tuple | None = None,
) -> str:
    """Protect legacy whole-fragment fields which did not expose inline placeholders.

    Matching tags come only from the original, with the filter's script/style exclusion.
    New or changed markup is literal text, never a new tag.
    """
    if protected is None:
        protected = tuple(template_pattern.findall(original)) if template_pattern else ()
    source_tokens = _tokenize(original, template_pattern)
    target_tokens = _tokenize(target, template_pattern)
    source_tags = [t for t in source_tokens if _is_tag_token(t)]
    target_tags = [t for t in target_tokens if _is_tag_token(t)]
    if not source_tags or source_tags != target_tags:
        return _escape_text(target, protected, quotes=True)
    out = []
    skip_name = ""
    for source, translated in zip(source_tokens, target_tokens, strict=True):
        if _is_tag_token(source):
            closing, name, _self_closing = _parse_tag(source)
            if skip_name:
                if closing and name == skip_name:
                    skip_name = ""
            elif name in _SKIP_CONTAINERS and not closing:
                skip_name = name
            out.append(source)
        else:
            out.append(source if skip_name else _escape_text(translated, protected, quotes=True))
    return "".join(out)


# --- accessibility: alt text units, target language, missing-alt findings ---------
# Attributes whose value is HUMAN text a screen reader speaks, per element. They are
# extracted as `a11y:*` units APPENDED after every body unit (see app/filters/a11y.py),
# so the body units keep their indices for documents ingested before this existed.
_IMG_ATTRS = (
    ("alt", "a11y:img-alt"),
    ("title", "a11y:img-title"),
    ("aria-label", "a11y:img-aria-label"),
)
_A11Y_ATTRS = {
    "img": _IMG_ATTRS,
    "area": _IMG_ATTRS,
    "a": (("title", "a11y:link-title"), ("aria-label", "a11y:link-aria-label")),
}
# One quoted attribute inside a start tag. Unquoted values are left alone (they cannot
# hold a space, so they are rarely human text, and rewriting them would need quoting).
_ATTR_RE = re.compile(r"""(?<=[\s"'])([A-Za-z_:][-A-Za-z0-9_:.]*)(\s*=\s*)("([^"]*)"|'([^']*)')""")
_BARE_ALT_RE = re.compile(r"\salt(?=[\s/>=])", re.I)
_UNQUOTED_ALT_RE = re.compile(r"""\salt\s*=\s*[^\s"'>]""", re.I)


def _tag_attrs(tok: str):
    """Yield (name_lower, value_start, value_end, raw_value) for each QUOTED attribute of
    a start tag token; the span is the value between the quotes."""
    for m in _ATTR_RE.finditer(tok):
        grp = 4 if m.group(4) is not None else 5
        yield m.group(1).lower(), m.start(grp), m.end(grp), m.group(grp)


def _iter_start_tags(tokens: list[str], translatable_only: bool = False):
    """Yield (token_index, name) for every START tag outside <script>/<style>, the same
    skip rule _iter_units applies. `translatable_only` (#734): leave out elements whose
    translate mode is no (their human-text attributes are not offered either)."""
    scope = _translate_scope(tokens) if translatable_only else None
    skip_name = ""
    for ti, tok in enumerate(tokens):
        if not _is_tag_token(tok):
            continue
        closing, name, _self_closing = _parse_tag(tok)
        if skip_name:
            if closing and name == skip_name:
                skip_name = ""
            continue
        if name in _SKIP_CONTAINERS:
            if not closing:
                skip_name = name
            continue
        if scope is not None and not scope.feed(tok, closing, name, _self_closing):
            continue
        if closing or not name or tok.startswith("<!"):
            continue
        yield ti, name


def _iter_attr_units(tokens: list[str]) -> list[dict]:
    """Every translatable accessibility attribute, in document order: token index, value
    span inside that token, the unescaped text and its a11y style."""
    out: list[dict] = []
    for ti, name in _iter_start_tags(tokens, translatable_only=True):
        wanted = _A11Y_ATTRS.get(name)
        if not wanted:
            continue
        styles = dict(wanted)
        for attr, start, end, raw in _tag_attrs(tokens[ti]):
            style = styles.get(attr)
            if style is None:
                continue
            core = _html.unescape(raw).strip()
            # Needs a LETTER: a numeric-only value (alt="12345") has nothing to
            # translate, so it never becomes a segment.
            if not core or not any(ch.isalpha() for ch in core):
                continue
            out.append({"ti": ti, "start": start, "end": end, "text": core, "style": style})
    return out


def _iter_meta_units(tokens: list[str]) -> list[dict]:
    """#733: the page metadata a search result or link preview shows, in document order,
    in the same shape as _iter_attr_units: the quoted `content` of a <meta> whose name is
    description / keywords or whose property / name is og:title, og:description,
    twitter:title, twitter:description (html_guard.meta_text_style, which the export check
    shares). Appended AFTER the accessibility units, so no existing index moves."""
    from app.filters.html_guard import meta_text_style

    out: list[dict] = []
    # gate #733: a meta inside a translate="no" / notranslate scope (#734) is not offered
    for ti, name in _iter_start_tags(tokens, translatable_only=True):
        if name != "meta":
            continue
        tok = tokens[ti]
        # every attribute, quoted or not, first occurrence wins (#734's tokenizer reader;
        # a minified page writes name=description)
        style = meta_text_style(list(_tag_attr_values(tok).items()))
        if style is None:
            continue
        content = next((a for a in _tag_attrs(tok) if a[0] == "content"), None)
        if content is None:
            continue  # absent or unquoted (one word): nothing to rewrite safely
        _attr, start, end, raw = content
        core = _html.unescape(raw).strip()
        if not core or not any(ch.isalpha() for ch in core):
            continue
        out.append({"ti": ti, "start": start, "end": end, "text": core, "style": style})
    return out


def _iter_page_attr_units(tokens: list[str], page_meta: bool = True) -> list[dict]:
    """The attribute units a whole page offers: accessibility attributes, then (#733)
    page metadata when `page_meta`."""
    units = _iter_attr_units(tokens)
    return units + _iter_meta_units(tokens) if page_meta else units


def attr_strictness(tokens: list[str]):
    """#676: a function (token index, value start, value end) -> True when a translated
    value written there must be made opaque (html_guard.attr_value strict). That is every
    value on a page whose markup boundaries a browser may place elsewhere (the #672
    `_raw_quotes_safe` check fails), and any value a browser-equivalent HTML5 parse of the
    page does not read as that same quoted attribute value. Elsewhere `&<>"'` escaping is
    enough, and translated alt text keeps its readable spaces."""
    from app.filters import html_guard

    text = "".join(tokens)
    # a fresh scan of the same text: a caller's stream may merge tokens (Flare variables)
    # or split code islands out of text (server pages), which the check must not see
    if not _raw_quotes_safe(_tokenize(text)):
        return lambda _ti, _s, _e: True
    offsets = []
    pos = 0
    for tok in tokens:
        offsets.append(pos)
        pos += len(tok)
    spans = html_guard.agreed_value_spans(text)

    def strict(ti: int, start: int, end: int) -> bool:
        tok = tokens[ti]
        quote = tok[start - 1] if start else ""
        return (offsets[ti] + start, offsets[ti] + end, quote) not in spans

    return strict


def _apply_attr_units(
    tokens: list[str], paragraph_texts: dict, base: int, page_meta: bool = False
) -> None:
    """Write translated attribute values back into their start-tag tokens, in place.
    Values are HTML-escaped (quotes too), so a translation can never break out of the
    attribute or add a tag, and the token stream keeps its exact shape. #676: where the
    browser could read the value's surroundings differently, the value is also made
    opaque to every HTML5 tag state (see attr_strictness). `page_meta` (#733): also the
    page metadata units, numbered after the accessibility ones."""
    from app.filters.html_guard import attr_value

    edits: dict[int, list] = {}
    strict = None
    for j, a in enumerate(_iter_page_attr_units(tokens, page_meta)):
        new = paragraph_texts.get(str(base + j))
        if new is None or new == a["text"]:
            continue
        if strict is None:
            strict = attr_strictness(tokens)
        val = attr_value(str(new), strict(a["ti"], a["start"], a["end"]))
        edits.setdefault(a["ti"], []).append((a["start"], a["end"], val))
    for ti, spans in edits.items():
        tok = tokens[ti]
        for start, end, val in sorted(spans, reverse=True):
            tok = tok[:start] + val + tok[end:]
        tokens[ti] = tok


def _missing_alt_findings(tokens: list[str], units: list[dict]) -> tuple[int, list[dict]]:
    """(image count, findings) for <img> elements whose alt is missing or blank."""
    spans = [(u["start"], u["end"], k) for k, u in enumerate(units)]
    count = 0
    found: list[dict] = []
    for ti, name in _iter_start_tags(tokens):
        if name != "img":
            continue
        count += 1
        attrs = {a: raw for a, _s, _e, raw in _tag_attrs(tokens[ti])}
        src = _html.unescape(attrs.get("src", "")).rsplit("/", 1)[-1]
        if "alt" not in attrs and _BARE_ALT_RE.search(tokens[ti]) is None:
            found.append(a11y.missing_alt("missing", a11y.anchor_for(ti, spans), src))
        elif "alt" not in attrs and _UNQUOTED_ALT_RE.search(tokens[ti]):
            continue  # unquoted alt=value is non-empty
        elif not _html.unescape(attrs.get("alt", "")).strip():
            found.append(a11y.missing_alt("empty", a11y.anchor_for(ti, spans), src))
    return count, found


def _set_attr(tag: str, attr: str, value: str, add_if_missing: bool = True) -> str:
    """Set `attr` on a start tag (replacing any quoted/unquoted value) or insert it
    right after the element name. Only values html_lang validated reach here."""
    pat = re.compile(r"(\s" + re.escape(attr) + r"""\s*=\s*)("[^"]*"|'[^']*'|[^\s>"']+)""", re.I)
    if pat.search(tag):
        return pat.sub(lambda m: m.group(1) + f'"{value}"', tag, count=1)
    if not add_if_missing:
        return tag
    m = _TAG_NAME_RE.match(tag)
    return tag[: m.end()] + f' {attr}="{value}"' + tag[m.end() :]


def _declare_language(tokens: list[str], tag: str, direction: str) -> None:
    """Point the document's language declaration at the target, in place: the <html>
    element's `lang` (and `xml:lang` when present), its `dir` (set to rtl for an RTL
    target; an existing dir is set to ltr for an LTR one, never added), and a
    `<meta http-equiv="content-language">`. A fragment with no <html> element gets no
    declaration: there is nothing honest to attach it to."""
    for ti, name in _iter_start_tags(tokens):
        tok = tokens[ti]
        if name == "html":
            tok = _set_attr(tok, "lang", tag)
            if re.search(r"\sxml:lang\s*=", tok, re.I):
                tok = _set_attr(tok, "xml:lang", tag)
            if direction == "rtl":
                tok = _set_attr(tok, "dir", "rtl")
            elif direction == "ltr":
                tok = _set_attr(tok, "dir", "ltr", add_if_missing=False)
            tokens[ti] = tok
        elif name == "meta":
            attrs = {a: raw for a, _s, _e, raw in _tag_attrs(tok)}
            if attrs.get("http-equiv", "").strip().lower() == "content-language":
                tokens[ti] = _set_attr(tok, "content", tag, add_if_missing=False)


def a11y_attr_units(data: bytes) -> tuple[list[str], list[dict]]:
    """(tokens, units): the accessibility attribute units of `data` exactly as parse()
    numbers them, each `{p_index, ti, style, text}` where `ti` indexes `tokens`. For a
    caller that must keep some of them OUT of translation (the WordPress connector skips
    image alt inside a Gutenberg cover block)."""
    tokens = _tokenize(decode_text_lossless(data))
    base = len(_iter_units(tokens))
    units = [
        {"p_index": base + j, "ti": a["ti"], "style": a["style"], "text": a["text"]}
        for j, a in enumerate(_iter_attr_units(tokens))
    ]
    return tokens, units


@register
class HtmlFilter(Filter):
    # #703: raw <, > and & in a target are escaped on export (quality entity gate).
    escapes_raw_text = True
    fmt = "html"
    exts = ("htm", "xhtml")

    def __init__(self, template_pattern=None, page_meta: bool = True):
        self.template_pattern = template_pattern
        # #733: offer <meta> description / keywords / og / twitter text (appended after
        # the accessibility units). Off for callers that concatenate several pages into
        # one document (EPUB chapters, e-mail parts): a unit added in one page would move
        # every later page's stored indices.
        self.page_meta = page_meta

    def _server_page(self, text: str):
        """#672: the server-page filter for a plain .html/.htm that holds closed PHP or
        <% %> code islands (see server_page_filter.server_page_for_html), else None.
        Subclasses and template-aware instances keep their own handling."""
        if type(self) is not HtmlFilter or self.template_pattern is not None:
            return None
        from app.filters.server_page_filter import server_page_for_html

        return server_page_for_html(text)

    def parse(
        self,
        data: bytes,
        provider: str | None = None,
        *,
        lenient: bool = False,
        enforce_caps: bool = True,
    ) -> ParseResult:
        # enforce_caps (#571): False only for export re-reading the stored original of a
        # document that was ingested before the segment cap existed, which must stay exportable.
        # lenient: export re-reading the stored original of a document imported BEFORE
        # #509 (no `source_encoding` on its skeleton) to check its unit list. Those
        # units were extracted with a UTF-8 `replace` decode, so the check decodes the
        # same way and never refuses a file an older version accepted.
        if lenient:
            text, enc = bytes(data).decode("utf-8", "replace"), "utf-8"
        else:  # #509: declared charset or Windows-1252 converted, else refused
            text, enc = read_text_upload(data, declared=True)
        server = self._server_page(text)
        if server is not None:
            parsed = server.parse(data, provider, lenient=lenient, enforce_caps=enforce_caps)
            parsed.skeleton["format"] = "html"
            parsed.skeleton["server_code"] = server.fmt
            return parsed
        # #571: tokenizing costs about ten seconds per million tags and holds every token
        # in memory before a unit exists. Ten markup characters per allowed segment
        # (500000 at the default, a page of over 10 MB) is far above any real page; a
        # file past it is refused at the cost of one C-speed count.
        if enforce_caps and text.count("<") > 10 * config.MAX_DOCUMENT_SEGMENTS:
            raise InputLimitError(
                "This HTML file has far more tags than MAX_DOCUMENT_SEGMENTS allows "
                f"({config.MAX_DOCUMENT_SEGMENTS} segments). Split it into smaller files."
            )
        # #571 gate: one hundred tag-scan steps per allowed segment (a real tag takes a
        # handful, a closing tag two) bounds megabyte-long unquoted tags.
        budget = [100 * config.MAX_DOCUMENT_SEGMENTS] if enforce_caps else None
        tokens = _tokenize(text, self.template_pattern, budget)
        units: list[TextUnit] = []
        body = _iter_units(tokens, capped=enforce_caps)
        for idx, u in enumerate(body):
            units.append(TextUnit(p_index=idx, text=u["core"], inline_map=u["map"]))
        # Alt text / titles / aria-labels: appended AFTER the body units (a11y.py), then
        # the page metadata (#733) after those.
        base = len(units)
        for j, a in enumerate(_iter_page_attr_units(tokens, self.page_meta)):
            units.append(TextUnit(p_index=base + j, text=a["text"], style=a["style"]))
            if enforce_caps:
                check_segment_cap(base + j + 1)
        skeleton = {
            "format": "html",
            "units": [{"p_index": u.p_index, "text": u.text, "style": u.style} for u in units],
        }
        n_images, missing = _missing_alt_findings(tokens, body)
        if n_images:
            skeleton["a11y_images"] = {"images": n_images, "missing_alt": missing}
        return ParseResult(skeleton=note_source_encoding(skeleton, enc), units=units)

    def build(self, original: bytes, paragraph_texts: dict) -> bytes:
        text = decode_text_lossless(original, declared=True)
        server = self._server_page(text)
        if server is not None:
            return server.build(original, paragraph_texts)
        tokens = _tokenize(text, self.template_pattern)
        before = "".join(tokens)
        # #672: quotes in text stay raw only where the page parses the same in a browser
        # (no template spans, which can expand to markup at runtime)
        quotes = self.template_pattern is not None or not _raw_quotes_safe(tokens)
        # Attribute values first: only VALUES change, so the unit walk below sees the
        # same structure and its inline maps carry the translated alt text along.
        _apply_attr_units(tokens, paragraph_texts, len(_iter_units(tokens)), self.page_meta)
        for k, u in enumerate(_iter_units(tokens)):
            new = paragraph_texts.get(str(k))
            if new is None:
                continue
            protected = (
                tuple(self.template_pattern.findall(u["core"])) if self.template_pattern else ()
            )
            materialized = _rematerialize(new, u["map"], protected, quotes=quotes)
            tokens[u["start"]] = u["lead"] + materialized + u["trail"]
            for j in range(u["start"] + 1, u["end"] + 1):
                tokens[j] = ""
        out = "".join(tokens)
        # #676: a browser-equivalent re-parse must find only the source's elements and
        # attributes (translated values aside); anything else is refused, never exported
        from app.filters.html_guard import check_build

        check_build(before, out)
        # B-bytes: a page uploaded in a declared legacy code page goes back in it (its
        # meta charset too) when every character fits; else UTF-8, as #509 wrote it.
        return encode_like(out, text_envelope(original, declared=True))

    def build_localized(
        self, original: bytes, paragraph_texts: dict, target_locale: str | None = None
    ) -> bytes:
        tag = a11y.target_tag(target_locale)
        if not tag:
            return self.build(original, paragraph_texts)
        server = self._server_page(decode_text_lossless(original, declared=True))
        if server is not None:
            return server.build_localized(original, paragraph_texts, target_locale)
        # Declare on the ORIGINAL token stream before any translated text is written
        # (only attribute values change, so unit indices are unaffected), so a
        # translation can never be mistaken for a tag.
        tokens = _tokenize(decode_text_lossless(original, declared=True), self.template_pattern)
        _declare_language(tokens, tag, a11y.target_direction(target_locale))
        built = self.build(encode_text_lossless("".join(tokens)), paragraph_texts)
        return encode_like(
            decode_text_lossless(built, declared=True), text_envelope(original, declared=True)
        )
