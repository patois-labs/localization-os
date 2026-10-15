"""MDX filter (.mdx): Markdown with JSX components, {expressions} and ES modules.

Built on the Markdown filter's line rules (`markdown_filter`): fenced code is skipped,
each translatable line is split into a markup PREFIX (heading #, bullets, >, numbers)
and a translatable CORE, rules / table separators / pure-symbol lines are left alone.
Front matter is handled exactly like the Markdown filter (#731, see markdown_filter: only
the one-line string values of allowlisted keys are units, they come first, the walk is
stamped `md_walk` = 2; a document imported without the stamp exports under the old walk,
where the `---` fences were rules and every other line an ordinary line). On top of that,
the MDX syntax (https://mdxjs.com/docs/what-is-mdx/, micromark-extension-mdxjs-esm / -mdx-jsx /
-mdx-expression):

  - ESM: a line that starts with `import ` or `export ` opens a JavaScript block that
    runs to the next blank line; the whole block is skipped.
  - Flow (block) JSX: a line holding only JSX tags (`<Note type="info">`, `</Note>`,
    `<Chart data={d} />`, a tag spread over several lines) is skipped. The Markdown
    BETWEEN an opening and closing flow tag is ordinary content and is translated.
  - Flow expressions: a line holding only a `{...}` expression (also `{/* comment */}`,
    possibly over several lines) is skipped.
  - Inline JSX and inline `{expressions}` inside a paragraph become protected tokens
    from services/segment (a paired component `<Kbd>Ctrl</Kbd>` -> `⟦g1⟧Ctrl⟦/g1⟧`, a
    self-closing component or an expression -> `⟦x1⟧`), never split points. Inline code
    spans and backslash escapes are kept as text.
  - JSX string attributes (`alt="..."`, `title='...'`) are translated only when the
    attribute name is in the allowlist (`LOC_TMS_MDX_ATTRS`, comma separated, default
    `alt,aria-label,caption,label,placeholder,title`). They are units APPENDED after
    every line unit (like HTML alt text, see app/filters/a11y.py), so changing the
    allowlist never moves a line unit's index. Expression attributes (`title={x}`)
    are never touched.

parse() and build() run the SAME walk over the same decoded text, so unit indices line
up. build() splices replacements into the original text at their offsets, so every
untouched byte (line endings, trailing newline, BOM) is kept.

Documented in: docs/architecture/translation-pipeline.md
"""

import os
import re
from bisect import bisect_left, bisect_right
from dataclasses import dataclass, field

from app.filters import a11y
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
from app.filters.markdown_filter import (
    _FENCE,
    _IMAGE,
    _PREFIX,
    FOOTNOTE_DEF,
    MD_WALK_VERSION,
    CommentBlocks,
    _is_skip_line,
    decode_link_title,
    encode_fm_value,
    encode_link_title,
    front_matter_for,
    front_matter_keys,
    link_ref_def,
)
from app.filters.md_emphasis import (
    escape_block_start,
    fix_cjk_autolinks,
    fix_cjk_emphasis,
    fold_line_breaks,
    restore_cell_escapes,
)
from app.services.segment import _PLACEHOLDER_RE, ph_close, ph_open, ph_point, strip_placeholders

DEFAULT_ATTRS = ("alt", "aria-label", "caption", "label", "placeholder", "title")
ATTR_STYLE = "a11y:mdx-attr"

# One expression or tag may span at most this many characters; past it the `{` or `<`
# is read as a literal character. The whole walk also has a scan budget (below), so a
# file of unclosed braces is refused instead of rescanned to the end from every brace.
_MAX_SPAN = 100_000
_ESM = re.compile(r"(?:import|export)[ \t]")
_TAG_OPEN = re.compile(r"<(?=[A-Za-z_$/>])")
_TAG_NAME = re.compile(r"</?\s*([^\s/>{]*)")
_NAME_RUN = re.compile(r"[^\s=/>{\"'<]+")
_EXPR_SPECIAL = re.compile(r"[{}'\"`/]")
_STR_END = {
    '"': re.compile(r'["\\\n]'),
    "'": re.compile(r"['\\\n]"),
    "`": re.compile(r"[`\\]"),
}
_INLINE_SPECIAL = re.compile(r"[\\`{<]")
_EOL_CHARS = "\r\n\x0b\x0c\x1c\x1d\x1e\x85  "
_CODE_SPAN = re.compile(r"(`+)(?:.+?)(?<!`)\1(?!`)")
_UNESCAPED_SPECIAL = re.compile(r"(?<!\\)([{}<])")
_FOOTNOTE_LEAD = re.compile(FOOTNOTE_DEF.pattern + "$")


def translatable_attrs() -> frozenset:
    """The JSX attribute names whose string values are translated."""
    raw = os.environ.get("LOC_TMS_MDX_ATTRS")
    if raw is None:
        return frozenset(DEFAULT_ATTRS)
    return frozenset(a.strip() for a in raw.split(",") if a.strip())


@dataclass
class _Tag:
    start: int
    end: int
    attrs: list  # (name, value_start, value_end, quote) for string attributes
    names: set  # every attribute name (string or expression valued)


@dataclass
class _LineUnit:
    start: int  # core start offset in the decoded text
    end: int  # core end offset (trailing whitespace excluded)
    text: str  # core with protected tokens
    spans: dict = field(default_factory=dict)  # token id -> {"open"/"close"/"point": (s, e)}
    delim: str = ""  # #735: a link reference definition's title (its opening delimiter)


@dataclass
class _AttrUnit:
    name: str
    start: int
    end: int
    quote: str
    line: int | None  # index of the line unit whose core holds this attribute


class _Scanner:
    """Brace / tag scanner over the decoded text with a shared step budget."""

    def __init__(self, text: str):
        self.t = text
        self.n = len(text)
        self.budget = 4 * len(text) + 1_000_000

    def _spend(self, k: int) -> None:
        self.budget -= k
        if self.budget < 0:
            raise InputLimitError(
                "This MDX file has too much unclosed JSX or {expression} markup to read "
                "safely. Close the open braces and tags, or split the file."
            )

    def _str_end(self, j: int, q: str, limit: int) -> int:
        k = j + 1
        pat = _STR_END[q]
        while True:
            m = pat.search(self.t, k, limit)
            if not m:
                return limit
            if m.group() == "\\":
                k = m.end() + 1
                continue
            return m.end()

    def expr(self, i: int) -> int | None:
        """End offset (exclusive) of the balanced `{...}` starting at i, or None."""
        t = self.t
        limit = min(self.n, i + _MAX_SPAN)
        depth = 0
        j = i
        while j < limit:
            m = _EXPR_SPECIAL.search(t, j, limit)
            if not m:
                break
            j = m.start()
            c = t[j]
            if c == "{":
                depth += 1
                j += 1
            elif c == "}":
                depth -= 1
                j += 1
                if depth == 0:
                    self._spend(j - i)
                    return j
            elif c in "'\"`":
                j = self._str_end(j, c, limit)
            else:  # "/": a comment, or a plain slash
                nxt = t[j + 1 : j + 2]
                if nxt == "/":
                    k = t.find("\n", j, limit)
                    j = limit if k < 0 else k
                elif nxt == "*":
                    k = t.find("*/", j + 2, limit)
                    j = limit if k < 0 else k + 2
                else:
                    j += 1
        self._spend(limit - i)
        return None

    def tag(self, i: int) -> _Tag | None:
        """The JSX tag starting at the `<` at i (opening, closing, self-closing or a
        fragment), or None when it never closes."""
        t = self.t
        limit = min(self.n, i + _MAX_SPAN)
        j = i + 1
        attrs: list = []
        names: set = set()
        last = pending = None
        while j < limit:
            c = t[j]
            if c in " \t\r\n":
                j += 1
            elif c == ">":
                self._spend(j + 1 - i)
                return _Tag(i, j + 1, attrs, names)
            elif c == "/":
                j += 1
            elif c == "=":
                pending, last = last, None
                j += 1
            elif c in "\"'":
                k = t.find(c, j + 1, limit)
                if k < 0:
                    break
                if pending:
                    attrs.append((pending, j + 1, k, c))
                pending = None
                j = k + 1
            elif c == "{":
                e = self.expr(j)
                if e is None:
                    break
                pending = last = None
                j = e
            elif c == "<":
                break
            else:
                m = _NAME_RUN.match(t, j)
                last, pending = m.group(), None
                names.add(last)
                j = m.end()
        self._spend(j - i)
        return None


class _Walk:
    """One pass over an MDX text: the line units and the attribute units."""

    def __init__(
        self,
        text: str,
        allow: frozenset,
        md_walk: int = MD_WALK_VERSION,
        fm_keys: frozenset | None = None,
    ):
        self.text = text
        self.allow = allow
        self.sc = _Scanner(text)
        lines = text.splitlines(keepends=True)
        self.starts: list[int] = []
        self.ends: list[int] = []  # content end (line terminator excluded)
        pos = 0
        for line in lines:
            self.starts.append(pos)
            body = line.splitlines()[0] if line.splitlines() else ""
            self.ends.append(pos + len(body))
            pos += len(line)
        # #731: front matter values are units BEFORE the line units (indices 0..n-1)
        bodies = [text[s:e] for s, e in zip(self.starts, self.ends, strict=True)]
        self.bodies = bodies
        self.walk = md_walk
        self.fm_keys = front_matter_keys() if fm_keys is None else fm_keys
        self.fm = front_matter_for(bodies, md_walk, self.fm_keys)
        self.fm_fields = self.fm.fields if self.fm else []
        self.units: list[_LineUnit] = []
        self.attr_units: list[_AttrUnit] = []
        self.tags: list[_Tag] = []
        self._run()

    def _line_end_at(self, offset: int) -> int:
        li = bisect_right(self.starts, offset) - 1
        return self.ends[li] if li >= 0 else 0

    def _at_eol(self, j: int) -> bool:
        t = self.text
        while j < len(t) and t[j] in " \t":
            j += 1
        return j >= len(t) or t[j] in _EOL_CHARS

    def _record_attrs(self, tag: _Tag, line: int | None) -> None:
        self.tags.append(tag)
        for name, s, e, q in tag.attrs:
            if name in self.allow and any(ch.isalnum() for ch in self.text[s:e]):
                self.attr_units.append(_AttrUnit(name, s, e, q, line))
                check_segment_cap(len(self.fm_fields) + len(self.units) + len(self.attr_units))

    def _run(self) -> None:
        t = self.text
        consumed = 0
        in_fence = False
        # ESM cannot interrupt a paragraph (micromark-extension-mdxjs-esm checks
        # `self.interrupt`): a wrapped prose line "export the file ..." stays prose.
        para = False
        n_lines = len(self.starts)
        li = self.fm.end if self.fm else 0
        # #735 (walk 3): link reference definitions, footnote labels and HTML comment blocks
        # as in the Markdown filter; MDX has no indented code (mdx-md turns it off)
        blocks = self.walk >= 3
        comments = CommentBlocks(self.bodies) if blocks else None
        while li < n_lines:
            s, c = self.starts[li], self.ends[li]
            if s < consumed:
                li += 1
                continue
            bs = s + 1 if li == 0 and t.startswith("﻿") else s
            body = t[bs:c]
            if _FENCE.match(body):
                in_fence = not in_fence
                para = False
                li += 1
                continue
            if in_fence or not body.strip() or _is_skip_line(body):
                para = False
                li += 1
                continue
            if blocks:
                end = comments.end(li)
                if end is not None:
                    para, li = False, end + 1
                    continue
                rd = None if para else link_ref_def(self.bodies, li)
                if rd is not None:
                    if rd.title is not None:
                        tl, ts, te, delim = rd.title
                        ls = self.starts[tl]
                        title = decode_link_title(t[ls + ts : ls + te])
                        if any(ch.isalnum() for ch in title):
                            self.units.append(_LineUnit(ls + ts, ls + te, title, {}, delim))
                            check_segment_cap(
                                len(self.fm_fields) + len(self.units) + len(self.attr_units)
                            )
                    para, li = False, rd.end + 1
                    continue
            if not para and _ESM.match(body):  # ESM block: runs to the next blank line
                li += 1
                while li < n_lines and t[self.starts[li] : self.ends[li]].strip():
                    li += 1
                continue
            first = bs + len(body) - len(body.lstrip(" \t"))
            if t[first] == "{":
                e = self.sc.expr(first)
                if e is not None and self._at_eol(e):
                    consumed = e
                    para = False
                    li += 1
                    continue
            if _TAG_OPEN.match(t, first):
                tags = self._flow_tags(first)
                if tags:
                    for tag in tags:
                        self._record_attrs(tag, None)
                    consumed = tags[-1].end
                    para = False
                    li += 1
                    continue
            prefix = _PREFIX.match(body).group(1)
            heading = "#" in prefix
            fn = FOOTNOTE_DEF.match(body, len(prefix)) if blocks else None
            if fn is not None:  # #735: `[^label]: ` is markup, the text after it a unit
                prefix = body[: fn.end()]
            consumed = self._text_line(bs + len(prefix), c)
            para = not heading  # an ATX heading is one line, it closes at once
            li += 1

    def _flow_tags(self, j: int) -> list:
        """Tags that fill a line up to its end (flow JSX), else []."""
        t = self.text
        tags = []
        while True:
            tag = self.sc.tag(j)
            if tag is None:
                return []
            tags.append(tag)
            j = tag.end
            if self._at_eol(j):
                return tags
            while t[j] in " \t":
                j += 1
            if not _TAG_OPEN.match(t, j):
                return []

    def _text_line(self, cs: int, end: int) -> int:
        """Tokenise one Markdown line core; returns the offset consumed up to."""
        t = self.text
        parts: list = []  # str, or ("tag", _Tag) / ("expr", s, e)
        j = cs
        while j < end:
            m = _INLINE_SPECIAL.search(t, j, end)
            if not m:
                parts.append(t[j:end])
                break
            k = m.start()
            if k > j:
                parts.append(t[j:k])
            ch = t[k]
            if ch == "\\":
                parts.append(t[k : min(k + 2, end)])
                j = min(k + 2, end)
            elif ch == "`":
                run = len(t[k:end]) - len(t[k:end].lstrip("`"))
                close = re.compile(r"(?<!`)" + "`" * run + r"(?!`)").search(t, k + run, end)
                stop = close.end() if close else k + run
                parts.append(t[k:stop])
                j = stop
            elif ch == "{":
                e = self.sc.expr(k)
                if e is None:
                    parts.append("{")
                    j = k + 1
                else:
                    parts.append(("expr", k, e))
                    end = max(end, self._line_end_at(e - 1))
                    j = e
            else:  # "<"
                tag = self.sc.tag(k) if _TAG_OPEN.match(t, k) else None
                if tag is None:
                    parts.append("<")
                    j = k + 1
                else:
                    parts.append(("tag", tag))
                    end = max(end, self._line_end_at(tag.end - 1))
                    j = tag.end
        # trailing whitespace is not part of the core (same as the Markdown filter)
        core_end = end
        if parts and isinstance(parts[-1], str):
            stripped = parts[-1].rstrip()
            core_end -= len(parts[-1]) - len(stripped)
            parts[-1] = stripped
        text, spans = self._tokens(parts)
        index = None
        if any(ch.isalnum() for ch in strip_placeholders(text)):
            index = len(self.units)
            self.units.append(_LineUnit(cs, core_end, text, spans))
            # #571: stop early
            check_segment_cap(len(self.fm_fields) + len(self.units) + len(self.attr_units))
        for p in parts:
            if isinstance(p, tuple) and p[0] == "tag":
                self._record_attrs(p[1], index)
        return end

    def _tokens(self, parts: list) -> tuple[str, dict]:
        """Pair inline tags by name; unpaired tags and expressions become point tokens."""
        partner: dict[int, int] = {}
        stack: list[tuple[int, str]] = []
        for pi, p in enumerate(parts):
            if isinstance(p, tuple) and p[0] == "tag":
                tag = p[1]
                raw = _TagRaw(self.text[tag.start : tag.end])
                if raw.closing:
                    for si in range(len(stack) - 1, -1, -1):
                        if stack[si][1] == raw.name:
                            partner[stack[si][0]] = pi
                            partner[pi] = stack[si][0]
                            del stack[si:]
                            break
                elif not raw.self_closing:
                    stack.append((pi, raw.name))
        out: list[str] = []
        spans: dict = {}
        ids: dict[int, int] = {}
        counter = 0
        for pi, p in enumerate(parts):
            if isinstance(p, str):
                out.append(p)
                continue
            s, e = (p[1], p[2]) if p[0] == "expr" else (p[1].start, p[1].end)
            if pi in partner and partner[pi] < pi:  # a close of a pair
                n = ids[partner[pi]]
                spans[str(n)]["close"] = (s, e)
                out.append(ph_close("g", n))
                continue
            counter += 1
            ids[pi] = counter
            if pi in partner:
                spans[str(counter)] = {"open": (s, e)}
                out.append(ph_open("g", counter))
            else:
                spans[str(counter)] = {"point": (s, e)}
                out.append(ph_point("x", counter))
        return "".join(out), spans


class _TagRaw:
    """Kind and name of one JSX tag from its source text."""

    def __init__(self, raw: str):
        self.closing = raw.startswith("</")
        self.self_closing = raw[:-1].rstrip().endswith("/") and not self.closing
        m = _TAG_NAME.match(raw)
        self.name = m.group(1) if m else ""


def _walk(
    text: str,
    allow: frozenset | None = None,
    md_walk: int = MD_WALK_VERSION,
    fm_keys: frozenset | None = None,
) -> _Walk:
    return _Walk(text, translatable_attrs() if allow is None else allow, md_walk, fm_keys)


def _escape_text(s: str) -> str:
    """Backslash-escape `{`, `}` and `<` a translation introduced (outside code spans),
    so the rebuilt MDX still compiles; already escaped ones are left alone."""
    out = []
    pos = 0
    for m in _CODE_SPAN.finditer(s):
        out.append(_UNESCAPED_SPECIAL.sub(r"\\\1", s[pos : m.start()]))
        out.append(m.group(0))
        pos = m.end()
    out.append(_UNESCAPED_SPECIAL.sub(r"\\\1", s[pos:]))
    return "".join(out)


_ATTR_OPAQUE = {"{": "&#123;", "}": "&#125;", "<": "&#60;", ">": "&#62;", "=": "&#61;"}


def _escape_attr(s: str, quote: str) -> str:
    """A JSX string attribute value. Besides its own quote, #676: `{ } < > =` are written
    as character references (MDX decodes them in a string attribute), so if the MDX
    compiler ever reads the value's surroundings differently from this scanner, the value
    can open no expression, element or attribute value, only literal text."""
    s = re.sub(r"[{}<>=]", lambda m: _ATTR_OPAQUE[m.group()], s)
    return s.replace(quote, "&quot;" if quote == '"' else "&#39;")


def _rematerialize(text: str, spans: dict, raw) -> str:
    """Put the original JSX / expressions back for the tokens; a token with no entry is
    dropped (never left as literal bracket text), like html_filter."""
    out: list[str] = []
    pos = 0
    for m in _PLACEHOLDER_RE.finditer(text):
        out.append(_escape_text(text[pos : m.start()]))
        pos = m.end()
        inner = m.group(0)[1:-1]
        if inner.startswith("/"):
            info = spans.get(re.sub(r"^/[a-z]?", "", inner))
            if info and "close" in info:
                out.append(raw(*info["close"]))
        else:
            info = spans.get(re.sub(r"^[a-z]?", "", inner, count=1))
            if info:
                span = info.get("point") or info.get("open")
                if span:
                    out.append(raw(*span))
    out.append(_escape_text(text[pos:]))
    return "".join(out)


@register
class MdxFilter(Filter):
    # #703: a raw < in a target is backslash-escaped on export and a raw & is plain text, so
    # the quality entity gate does not flag an unescaped &lt; / &amp;.
    escapes_raw_text = True
    fmt = "mdx"
    exts = ()

    def parse(self, data: bytes, provider: str | None = None) -> ParseResult:
        text, enc = read_text_upload(data)  # #509 decode + #571 line budget
        w = _walk(text)
        units: list[TextUnit] = []
        spans = []
        for f in w.fm_fields:  # #731: front matter values first
            idx = len(units)
            check_segment_cap(idx + 1)
            units.append(TextUnit(p_index=idx, text=f.text))
            ls = w.starts[f.line]
            spans.append((ls + f.start, ls + f.end, idx))
        off = len(units)
        for j, lu in enumerate(w.units):
            idx = off + j
            check_segment_cap(idx + 1)
            inline = {
                k: {kind: text[s:e] for kind, (s, e) in v.items()} for k, v in lu.spans.items()
            }
            units.append(TextUnit(p_index=idx, text=lu.text, inline_map=inline))
            spans.append((lu.start, lu.end, idx))
        base = len(units)
        for k, au in enumerate(w.attr_units):
            check_segment_cap(base + k + 1)
            units.append(TextUnit(p_index=base + k, text=text[au.start : au.end], style=ATTR_STYLE))
        check_unit_texts(u.text for u in units)
        missing: list[dict] = []
        n_images = 0
        for lu in w.units:
            for m in _IMAGE.finditer(text, lu.start, lu.end):
                n_images += 1
                if not m.group(1).strip():
                    where = m.group(2).strip().split(" ")[0].rsplit("/", 1)[-1]
                    missing.append(
                        a11y.missing_alt("empty", a11y.anchor_for(m.start(), spans), where)
                    )
        for tag in w.tags:
            name = (_TAG_NAME.match(text, tag.start).group(1) or "").lower()
            if name not in ("img", "image") or text.startswith("</", tag.start):
                continue
            n_images += 1
            alt = [(s, e) for nm, s, e, _q in tag.attrs if nm == "alt"]
            if "alt" not in tag.names:
                kind = "missing"
            elif alt and not text[alt[0][0] : alt[0][1]].strip():
                kind = "empty"
            else:
                continue
            src = [text[s:e] for nm, s, e, _q in tag.attrs if nm == "src"]
            where = src[0].strip().rsplit("/", 1)[-1] if src else name
            missing.append(a11y.missing_alt(kind, a11y.anchor_for(tag.start, spans), where))
        skeleton = {
            "format": "mdx",
            "md_walk": MD_WALK_VERSION,  # #731
            "md_fm_keys": sorted(w.fm_keys),  # gate #731: export reuses this allowlist
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
        """MDX has no language declaration: build() under the stored walk (#731)."""
        return self.build(original, paragraph_texts, md_walk=md_walk, fm_keys=fm_keys)

    def build(
        self,
        original: bytes,
        paragraph_texts: dict,
        md_walk: int = MD_WALK_VERSION,
        fm_keys: frozenset | None = None,
    ) -> bytes:
        text = decode_text_lossless(original)
        w = _walk(text, md_walk=md_walk, fm_keys=fm_keys)
        off = len(w.fm_fields)
        base = off + len(w.units)
        # (start, end, new value, line unit holding it) in document order
        attr_new: list[tuple[int, int, str, int | None]] = []
        for k, au in enumerate(w.attr_units):
            new = paragraph_texts.get(str(base + k))
            if new is not None:
                # an unchanged value keeps its exact source bytes (#676 escapes only new text)
                val = new if new == text[au.start : au.end] else _escape_attr(new, au.quote)
                attr_new.append((au.start, au.end, val, au.line))
        attr_new.sort()
        attr_starts = [a[0] for a in attr_new]

        def raw(s: int, e: int) -> str:
            """The original text of a token, with translated attributes spliced in."""
            pieces, pos = [], s
            for a_start, a_end, val, _line in attr_new[
                bisect_left(attr_starts, s) : bisect_left(attr_starts, e)
            ]:
                pieces.append(text[pos:a_start] + val)
                pos = a_end
            pieces.append(text[pos:e])
            return "".join(pieces)

        reps: list[tuple[int, int, str]] = []
        for k, f in enumerate(w.fm_fields):  # #731: a valid YAML / TOML scalar
            new = paragraph_texts.get(str(k))
            if new is not None and new != f.text:
                val = encode_fm_value(w.fm.kind, f.style, new)
                if val is not None:
                    ls = w.starts[f.line]
                    reps.append((ls + f.start, ls + f.end, val))
        for k, lu in enumerate(w.units):
            new = paragraph_texts.get(str(k + off))
            if new is not None and lu.delim:  # #735: a link title, inside its delimiters
                if new != lu.text:
                    reps.append((lu.start, lu.end, encode_link_title(new, lu.delim)))
            elif new is not None:
                if new != lu.text:  # #695: emphasis marks glued to CJK text
                    new = fold_line_breaks(lu.text, new)  # #714: one line stays one line
                    new = fix_cjk_emphasis(lu.text, new)
                    new = fix_cjk_autolinks(lu.text, new, mdx=True)  # #700
                    new = restore_cell_escapes(lu.text, new)  # #710: table cell `\|`
                    # #704: a line-start unit (not heading text) opens no new block
                    lead = text[text.rfind("\n", 0, lu.start) + 1 : lu.start]
                    lead = _FOOTNOTE_LEAD.sub("", lead)  # #735: footnote text starts a block
                    if "#" not in lead and _PREFIX.fullmatch(lead):
                        new = escape_block_start(lu.text, new)
                reps.append((lu.start, lu.end, _rematerialize(new, lu.spans, raw)))
        for a_start, a_end, val, line in attr_new:
            # an attribute inside a translated line is written through its token (raw);
            # if the translation dropped that token, the attribute goes with it
            if line is None or str(line + off) not in paragraph_texts:
                reps.append((a_start, a_end, val))
        out: list[str] = []
        pos = 0
        for s, e, val in sorted(reps):
            out.append(text[pos:s])
            out.append(val)
            pos = e
        out.append(text[pos:])
        return encode_text_lossless("".join(out))
