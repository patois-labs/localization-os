"""Server-page templates: the HTML filter plus protected code islands (format Batch 16).

Documented in: docs/architecture/translation-pipeline.md

PHP templates (.phtml), JSP (.jsp/.jspf), ASP.NET Web Forms (.aspx/.ascx), ERB (.erb)
and server-side includes (.shtml/.shtm/.stm) are HTML with server code mixed in. The
code is never translated, never executed and never re-ordered:

1. A dialect lexer finds every code island (``<?php ?>``, ``<?= ?>``, ``<% %>``,
   ``<%= %>``, ``<%@ %>``, ``<%-- --%>``, JSP ``${...}``, SSI ``<!--#... -->``) and masks
   each one with a sentinel built from Unicode noncharacters, so the unchanged HTML
   scanner (html_filter) never sees code: a ``>`` or quote inside an island cannot end a
   tag, and an island inside a quoted attribute value stays inside that tag token.
2. In running text an OUTPUT island (``<?= $n ?>``, ``<%= x %>``, ``${x}``, SSI echo) is a
   ``⟦xN⟧`` point token inside its sentence; a STATEMENT island (``<?php if ... ?>``,
   ``<% } %>``, directives, comments) is a unit boundary like a block tag, so control
   flow can never move inside a translated sentence.
3. Build re-derives the same islands from the original bytes and swaps the sentinels
   back, so every island is byte-identical. An output token a translation dropped is
   appended to the end of its unit and a duplicated one is written once: code is never
   lost or repeated.

A file whose islands cannot be tokenised safely is REFUSED with a plain reason rather
than guessed at: an unterminated island, an island used as a tag name or attribute name
(``<<?= $tag ?>>``, ``<div <?php echo $attrs ?>>``), an island that opens a tag it does
not close, or a file that already holds the sentinel characters.

Specs: https://www.php.net/manual/en/language.basic-syntax.phptags.php ,
https://www.php.net/manual/en/language.basic-syntax.comments.php ,
https://www.php.net/manual/en/language.types.string.php (heredoc/nowdoc),
https://jakarta.ee/specifications/pages/3.1/jakarta-server-pages-spec-3.1 (scripting
elements, quoting, EL), https://learn.microsoft.com/en-us/previous-versions/aspnet/ms178135(v=vs.100)
(embedded code blocks), https://docs.ruby-lang.org/en/master/ERB.html ,
https://httpd.apache.org/docs/current/mod/mod_include.html (SSI).
"""

import re

from app import config
from app.filters import a11y
from app.filters.base import (
    Filter,
    InputLimitError,
    ParseResult,
    TextUnit,
    check_segment_cap,
    decode_text_lossless,
    encode_text_lossless,
    note_source_encoding,
    read_text_upload,
    register,
)
from app.filters.html_filter import (
    _SKIP_CONTAINERS,
    HtmlFilter,
    _declare_language,
    _is_tag_token,
    _iter_page_attr_units,
    _iter_units,
    _MarkupToken,
    _missing_alt_findings,
    _parse_tag,
    _rematerialize,
    _TextToken,
    _tokenize,
    attr_strictness,
)
from app.filters.html_guard import check_build, strict_escape
from app.services.segment import _PLACEHOLDER_RE

_S_OPEN = "﷐"  # Unicode noncharacters: never in real text, never in a tag grammar
_S_CLOSE = "﷑"
_SENT_RE = re.compile(_S_OPEN + r"(\d+)" + _S_CLOSE)
_SENT_CHARS_RE = re.compile("[" + _S_OPEN + _S_CLOSE + "]")


class ServerPageRefused(InputLimitError):
    """The server page cannot be tokenised safely; the message names the line."""


class _Island(_MarkupToken):
    """A masked code island in the token stream (its text is the sentinel)."""

    inline_point = False


class _InlineIsland(_Island):
    inline_point = True


def _line(text: str, pos: int) -> int:
    return text.count("\n", 0, pos) + 1


_HINT = "Move the code out of the tag or close it, then upload again."


def _refuse(text: str, pos: int, what: str, hint: str = _HINT) -> None:
    raise ServerPageRefused(
        f"This server page cannot be translated safely: {what} (line {_line(text, pos)}). {hint}"
    )


# --- dialect lexers: each returns [(start, end, inline)] in document order ----------

_PHP_OPEN = re.compile(r"<\?(?:(=)|php(?=\s|$)|(?=\s))", re.I)
_PHP_CODE = re.compile(r"\?>|['\"`]|/\*|//|#(?!\[)|<<<")
_PHP_HEREDOC = re.compile(r"<<<[ \t]*([\"']?)([A-Za-z_][A-Za-z0-9_]*)\1\r?\n")
_PHP_ECHO = re.compile(r"\s*(?:echo|print)\b[^{};]*;?\s*", re.I)


def _php_quote_end(text: str, pos: int) -> int:
    """`pos` at an opening quote; index after the closing one, honouring backslashes."""
    q = text[pos]
    i = pos + 1
    while True:
        j = text.find(q, i)
        if j == -1:
            return -1
        k = j - 1
        while k > pos and text[k] == "\\":
            k -= 1
        if (j - 1 - k) % 2 == 0:  # an even run of backslashes: the quote is real
            return j + 1
        i = j + 1


def _php_islands(text: str) -> list:
    out = []
    i = 0
    while True:
        m = _PHP_OPEN.search(text, i)
        if not m:
            return out
        start, pos = m.start(), m.end()
        end = len(text)  # a file may end in PHP mode with no closing ?>
        while True:
            c = _PHP_CODE.search(text, pos)
            if not c:
                break
            tok = c.group()
            if tok == "?>":
                end = c.end()
                break
            if tok in ("'", '"', "`"):
                pos = _php_quote_end(text, c.start())
                if pos == -1:
                    _refuse(text, c.start(), "a PHP string is never closed")
            elif tok == "/*":
                close = text.find("*/", c.end())
                if close == -1:
                    _refuse(text, c.start(), "a PHP comment is never closed")
                pos = close + 2
            elif tok in ("//", "#"):
                nl = text.find("\n", c.end())
                close = text.find("?>", c.end(), len(text) if nl == -1 else nl)
                if close != -1:  # ?> ends PHP mode even inside a one-line comment
                    end = close + 2
                    break
                pos = len(text) if nl == -1 else nl
            else:  # <<< heredoc / nowdoc
                h = _PHP_HEREDOC.match(text, c.start())
                if not h:
                    pos = c.end()
                    continue
                closer = re.compile(r"(?m)^[ \t]*" + re.escape(h.group(2)) + r"(?![A-Za-z0-9_])")
                cm = closer.search(text, h.end())
                if not cm:
                    _refuse(text, c.start(), "a PHP heredoc is never closed")
                pos = cm.end()
        inline = bool(m.group(1))
        if not inline and m.group(0).lower() != "<?=":
            body = text[m.end() : end - 2 if text.endswith("?>", 0, end) else end]
            inline = bool(_PHP_ECHO.fullmatch(body))
        out.append((start, end, inline))
        i = end


# <% %> family. Inline markers per dialect (the character right after "<%").
_PERCENT_INLINE = {
    "jsp": "=",
    "aspx": "=:#$",
    "erb": "=",
}
_JSP_XML = re.compile(r"<jsp:(scriptlet|declaration|expression)(?=[\s>/])", re.I)


def _percent_end(text: str, s: int, dialect: str) -> int:
    if dialect in ("jsp", "aspx") and text.startswith("<%--", s):
        e = text.find("--%>", s + 4)
        return -1 if e == -1 else e + 4
    pos = s + 2
    while True:
        e = text.find("%>", pos)
        if e == -1:
            return -1
        if dialect == "erb" and text[e - 1] == "%" and e - 1 >= s + 2:
            pos = e + 2  # ERB "%%>" is a literal %> inside the tag
            continue
        return e + 2


_EL_SCAN = re.compile(r"[{}'\"]")


def _el_end(text: str, s: int) -> int:
    """`s` at "${" or "#{": index after the matching brace (quotes honoured)."""
    depth = 0
    i = s + 1
    while True:
        m = _EL_SCAN.search(text, i)
        if not m:
            return -1
        ch = m.group()
        if ch == "{":
            depth += 1
            i = m.end()
        elif ch == "}":
            depth -= 1
            i = m.end()
            if depth == 0:
                return i
        else:
            i = _php_quote_end(text, m.start())
            if i == -1:
                return -1


_PERCENT_OPEN = {
    "jsp": re.compile(
        r"<%|<jsp:(?:scriptlet|declaration|expression)(?=[\s>/])|(?<!\\)[$#]\{", re.I
    ),
    "aspx": re.compile(r"<%"),
    "erb": re.compile(r"<%"),
}


def _percent_islands(dialect: str):
    opener = _PERCENT_OPEN[dialect]
    inline_marks = _PERCENT_INLINE[dialect]

    def lex(text: str) -> list:
        out = []
        i = 0
        while True:
            m = opener.search(text, i)
            if not m:
                return out
            s = m.start()
            tok = m.group()
            if tok in ("${", "#{"):
                e = _el_end(text, s)
                if e == -1:
                    _refuse(text, s, "a JSP expression ${...} is never closed")
                inline = True
            elif tok.lower().startswith("<jsp:"):
                x = _JSP_XML.match(text, s)
                close = re.compile(r"</jsp:" + x.group(1) + r"\s*>", re.I).search(text, x.end())
                if not close:
                    _refuse(text, s, f"<jsp:{x.group(1)}> is never closed")
                e = close.end()
                inline = x.group(1).lower() == "expression"
            elif dialect == "erb" and text.startswith("<%%", s):
                e, inline = s + 3, True  # ERB "<%%" outputs a literal "<%"
            else:
                e = _percent_end(text, s, dialect)
                if e == -1:
                    _refuse(text, s, "a <% code block is never closed with %>")
                mark = text[s + 2 : s + 3]
                inline = bool(mark) and mark in inline_marks and not text.startswith("<%--", s)
                if dialect == "erb" and text.startswith("<%==", s):
                    inline = True
            out.append((s, e, inline))
            i = e

    return lex


_SSI_OPEN = re.compile(r"<!--#([a-zA-Z]+)")
_SSI_INLINE = frozenset({"echo", "fsize", "flastmod"})


def _ssi_islands(text: str) -> list:
    out = []
    i = 0
    while True:
        m = _SSI_OPEN.search(text, i)
        if not m:
            return out
        e = text.find("-->", m.end())
        if e == -1:
            _refuse(text, m.start(), "a server-side include <!--# is never closed with -->")
        out.append((m.start(), e + 3, m.group(1).lower() in _SSI_INLINE))
        i = e + 3


# --- island safety checks ------------------------------------------------------------

# A quoted string literal in an island that STARTS an HTML tag the island never ends
# ('echo "<div";' followed by ' class="x">' in the page): the tag would straddle code.
_TAG_IN_STRING = re.compile(r"""["']\s*</?[A-Za-z][A-Za-z0-9:-]*(?=[\s"'/])""")
_ISLAND_CLOSER = re.compile(r"(?:\?>|%>|-->|</jsp:[a-z]+\s*>)\Z", re.I)
_ATTR_VALUE_RE = re.compile(r"""=\s*("[^"]*"|'[^']*'|[^\s>"']+)""")


def _check_island_body(text: str, s: int, e: int) -> None:
    body = _ISLAND_CLOSER.sub("", text[s:e])  # the island's own ?> / %> / --> is not a tag end
    last = None
    for found in _TAG_IN_STRING.finditer(body):
        last = found
    if last is not None and ">" not in body[last.end() :]:
        _refuse(text, s, "code prints the start of an HTML tag that the page finishes")


def _refuse_masked(masked: str, pos: int, codes: list, what: str) -> None:
    """_refuse at a position in the MASKED text, reporting the original line number."""
    prefix = _SENT_RE.sub(lambda m: codes[int(m.group(1))], masked[:pos])
    _refuse(prefix, len(prefix), what)


def _check_tag(tok: str, masked: str, pos: int, codes: list) -> None:
    """Sentinels in a tag token may only sit inside attribute VALUES."""
    if _S_OPEN not in tok or tok.startswith("<!") or tok.startswith("<?"):
        return
    allowed = [(m.start(1), m.end(1)) for m in _ATTR_VALUE_RE.finditer(tok)]
    for m in _SENT_RE.finditer(tok):
        if not any(a <= m.start() and m.end() <= b for a, b in allowed):
            _refuse_masked(
                masked, pos + m.start(), codes, "code is used as a tag or attribute name"
            )


# --- output escaping per dialect -----------------------------------------------------

_EL_IN_TEXT = re.compile(r"(?<!\\)([$#])\{")


def _escape_jsp(s: str) -> str:
    # Template text "${" / "#{" would be evaluated as EL; "\${" prints it literally.
    return _EL_IN_TEXT.sub(r"\\\1{", s)


class _ServerPageFilter(HtmlFilter):
    """Shared base; subclasses set fmt/exts and a dialect lexer."""

    fmt = ""
    exts: tuple = ()
    label = "server page"

    def __init__(self):
        super().__init__(template_pattern=None)

    # subclass hooks
    def _islands(self, text: str) -> list:  # pragma: no cover - abstract
        raise NotImplementedError

    def _escape_out(self, s: str) -> str:
        return s

    # masking
    def _mask(self, text: str):
        if _SENT_CHARS_RE.search(text):
            m = _SENT_CHARS_RE.search(text)
            _refuse(
                text,
                m.start(),
                "the file contains reserved characters U+FDD0/U+FDD1",
                "Remove those characters, then upload again.",
            )
        islands = self._islands(text)
        if len(islands) > 10 * config.MAX_DOCUMENT_SEGMENTS:
            raise InputLimitError(
                "This server page has far more code blocks than MAX_DOCUMENT_SEGMENTS allows "
                f"({config.MAX_DOCUMENT_SEGMENTS} segments). Split it into smaller files."
            )
        parts, codes, inline, pos = [], [], [], 0
        for k, (s, e, is_inline) in enumerate(islands):
            _check_island_body(text, s, e)
            parts.append(text[pos:s])
            parts.append(f"{_S_OPEN}{k}{_S_CLOSE}")
            codes.append(text[s:e])
            inline.append(is_inline)
            pos = e
        parts.append(text[pos:])
        return "".join(parts), codes, inline

    def _tokens(self, masked: str, codes: list, inline: list, budget=None) -> list:
        """HTML tokens of the masked text, with islands in text split into _Island tokens."""
        out: list = []
        skip_name = ""
        pos = 0
        for tok in _tokenize(masked, None, budget):
            if _is_tag_token(tok):
                _check_tag(tok, masked, pos, codes)
                closing, name, _sc = _parse_tag(tok)
                if skip_name:
                    if closing and name == skip_name:
                        skip_name = ""
                elif name in _SKIP_CONTAINERS and not closing:
                    skip_name = name
                out.append(tok)
            elif _S_OPEN in tok:
                last = 0
                for m in _SENT_RE.finditer(tok):
                    before = tok[last : m.start()]
                    if not skip_name and (before.endswith("<") or before.endswith("</")):
                        _refuse_masked(masked, pos + m.start(), codes, "code is used as a tag name")
                    out.append(_TextToken(before))
                    k = int(m.group(1))
                    out.append((_InlineIsland if inline[k] else _Island)(m.group()))
                    last = m.end()
                out.append(_TextToken(tok[last:]))
            else:
                out.append(tok)
            pos += len(tok)
        return out

    @staticmethod
    def _unmask(s: str, codes: list) -> str:
        return _SENT_RE.sub(lambda m: codes[int(m.group(1))], s)

    @staticmethod
    def _attr_units(tokens: list) -> list:
        # An attribute whose value holds code (alt="<?= $alt ?>") is not translatable text.
        return [
            a
            for a in _iter_page_attr_units(tokens)  # a11y, then page metadata (#733)
            if _S_OPEN not in tokens[a["ti"]][a["start"] : a["end"]]
        ]

    # Filter API
    def parse(
        self,
        data: bytes,
        provider: str | None = None,
        *,
        lenient: bool = False,
        enforce_caps: bool = True,
    ) -> ParseResult:
        if lenient:
            text, enc = bytes(data).decode("utf-8", "replace"), "utf-8"
        else:
            text, enc = read_text_upload(data, declared=True)
        if enforce_caps and text.count("<") > 10 * config.MAX_DOCUMENT_SEGMENTS:
            raise InputLimitError(
                f"This {self.label} file has far more tags than MAX_DOCUMENT_SEGMENTS allows "
                f"({config.MAX_DOCUMENT_SEGMENTS} segments). Split it into smaller files."
            )
        masked, codes, inline = self._mask(text)
        budget = [100 * config.MAX_DOCUMENT_SEGMENTS] if enforce_caps else None
        tokens = self._tokens(masked, codes, inline, budget)
        units: list[TextUnit] = []
        body = _iter_units(tokens, capped=enforce_caps)
        for idx, u in enumerate(body):
            shown = {
                k: {f: self._unmask(v, codes) if isinstance(v, str) else v for f, v in d.items()}
                for k, d in u["map"].items()
            }
            units.append(TextUnit(p_index=idx, text=u["core"], inline_map=shown))
        base = len(units)
        for j, a in enumerate(self._attr_units(tokens)):
            units.append(TextUnit(p_index=base + j, text=a["text"], style=a["style"]))
            if enforce_caps:
                check_segment_cap(base + j + 1)
        skeleton = {
            "format": self.fmt,
            "units": [{"p_index": u.p_index, "text": u.text, "style": u.style} for u in units],
        }
        n_images, missing = _missing_alt_findings(tokens, body)
        if n_images:
            skeleton["a11y_images"] = {"images": n_images, "missing_alt": missing}
        return ParseResult(skeleton=note_source_encoding(skeleton, enc), units=units)

    def _fix_islands(self, text: str, imap: dict) -> str:
        """Each output-island token exactly once: duplicates dropped, missing appended."""
        island_ids = {k for k, d in imap.items() if isinstance(d.get("point"), _Island)}
        if not island_ids:
            return text
        seen: set = set()
        out, pos = [], 0
        for m in _PLACEHOLDER_RE.finditer(text):
            inner = m.group(0)[1:-1]
            key = inner[1:] if inner[:1] == "x" else None
            if key in island_ids:
                out.append(text[pos : m.start()])
                if key not in seen:
                    out.append(m.group(0))
                    seen.add(key)
                pos = m.end()
        out.append(text[pos:])
        missing = sorted(island_ids - seen, key=int)
        return "".join(out) + "".join(f"⟦x{k}⟧" for k in missing)

    def build(self, original: bytes, paragraph_texts: dict) -> bytes:
        text = decode_text_lossless(original, declared=True)
        masked, codes, inline = self._mask(text)
        tokens = self._tokens(masked, codes, inline)
        before = "".join(tokens)
        self._apply_attrs(tokens, paragraph_texts, len(_iter_units(tokens)))
        for k, u in enumerate(_iter_units(tokens)):
            new = paragraph_texts.get(str(k))
            if new is None:
                continue
            new = self._fix_islands(_SENT_CHARS_RE.sub("", str(new)), u["map"])
            pieces = []
            pos = 0
            # Escape only the translated words, never the re-materialized original tags.
            for m in _PLACEHOLDER_RE.finditer(new):
                pieces.append(self._escape_out(new[pos : m.start()]))
                pieces.append(m.group(0))
                pos = m.end()
            pieces.append(self._escape_out(new[pos:]))
            materialized = _rematerialize("".join(pieces), u["map"])
            tokens[u["start"]] = u["lead"] + materialized + u["trail"]
            for j in range(u["start"] + 1, u["end"] + 1):
                tokens[j] = ""
        out = "".join(tokens)
        # #676: re-parse the masked page as a browser would (code islands are opaque text)
        check_build(before, out)
        return encode_text_lossless(self._unmask(out, codes))

    def _apply_attrs(self, tokens: list, paragraph_texts: dict, base: int) -> None:
        import html as _html

        edits: dict = {}
        strict = None
        for j, a in enumerate(self._attr_units(tokens)):
            new = paragraph_texts.get(str(base + j))
            if new is None or new == a["text"]:
                continue
            if strict is None:
                strict = attr_strictness(tokens)
            val = self._escape_out(_html.escape(_SENT_CHARS_RE.sub("", str(new)), quote=True))
            if strict(a["ti"], a["start"], a["end"]):
                val = strict_escape(val)  # #676: opaque to every HTML5 tag state
            edits.setdefault(a["ti"], []).append((a["start"], a["end"], val))
        for ti, spans in edits.items():
            tok = tokens[ti]
            for start, end, val in sorted(spans, reverse=True):
                tok = tok[:start] + val + tok[end:]
            tokens[ti] = type(tokens[ti])(tok) if isinstance(tokens[ti], _MarkupToken) else tok

    def build_localized(
        self, original: bytes, paragraph_texts: dict, target_locale: str | None = None
    ) -> bytes:
        tag = a11y.target_tag(target_locale)
        if not tag:
            return self.build(original, paragraph_texts)
        text = decode_text_lossless(original, declared=True)
        masked, codes, inline = self._mask(text)
        tokens = self._tokens(masked, codes, inline)
        before = list(tokens)
        _declare_language(tokens, tag, a11y.target_direction(target_locale))
        for i, (old, new) in enumerate(zip(before, tokens, strict=True)):
            if old != new and _S_OPEN in old:
                tokens[i] = old  # <html lang="<?= $lang ?>">: a dynamic language stays dynamic
        stamped = self._unmask("".join(tokens), codes)
        return self.build(encode_text_lossless(stamped), paragraph_texts)


@register
class PhpTemplateFilter(_ServerPageFilter):
    fmt = "phtml"
    exts = ()
    label = "PHP template"

    def _islands(self, text):
        return _php_islands(text)


@register
class JspFilter(_ServerPageFilter):
    fmt = "jsp"
    exts = ("jspf",)
    label = "JSP"

    def _islands(self, text):
        return _percent_islands("jsp")(text)

    def _escape_out(self, s):
        return _escape_jsp(s)


@register
class AspxFilter(_ServerPageFilter):
    fmt = "aspx"
    exts = ("ascx",)
    label = "ASP.NET page"

    def _islands(self, text):
        return _percent_islands("aspx")(text)


@register
class ErbFilter(_ServerPageFilter):
    fmt = "erb"
    exts = ()
    label = "ERB template"

    def _islands(self, text):
        return _percent_islands("erb")(text)


@register
class ShtmlFilter(_ServerPageFilter):
    fmt = "shtml"
    exts = ("shtm", "stm")
    label = "SSI page"

    def _islands(self, text):
        return _ssi_islands(text)


# --- server code inside a plain .html/.htm file (#672) ---------------------------------
# Pages are often saved as .html although a server runs them (Okapi's Jericho test page
# Test02.html opens with `<%@ page language="java" %>`). The plain HTML scanner would offer
# such a directive as page text and write it back escaped, so the code would become
# visible text. Okapi's HTML filter (Jericho) treats server tags as code; so do we: when
# an .html file holds a CLOSED PHP or <% %> island, the matching server-page filter
# parses and builds it, and every island is protected exactly as in a .php/.jsp/.aspx
# upload. The dialect is chosen from the ORIGINAL bytes on parse and build alike.
_HTML_PHP_OPEN = re.compile(r"<\?(?:php(?=\s)|=)", re.I)
_HTML_JSP_HINT = re.compile(
    r"<%@\s*taglib\b|<jsp:|<%@\s*page\b[^%]*?\blanguage\s*=\s*[\"']?java\b", re.I
)
_HTML_PERCENT_OPEN = re.compile("<%")


def _closed_after(text: str, opener: re.Pattern, closer: str) -> bool:
    m = opener.search(text)
    return bool(m) and text.find(closer, m.end()) != -1


def server_page_for_html(text: str):
    """The server-page filter for an .html/.htm file that holds server code, else None.

    PHP (`<?php ... ?>`, `<?= ... ?>`) opens with the PHP template filter; `<% ... %>`
    opens as JSP when the page says so (a taglib, a `<jsp:` element or
    `language="java"`), otherwise as an ASP.NET page (the generic `<% %>` lexer with
    `<%-- --%>` comments and no `${...}` expressions). An opener with no closer anywhere
    after it is ordinary text, as before. A page that mixes PHP and `<% %>` is refused.
    """
    php = _closed_after(text, _HTML_PHP_OPEN, "?>")
    percent = _closed_after(text, _HTML_PERCENT_OPEN, "%>")
    if php and percent:
        m = _HTML_PHP_OPEN.search(text)
        _refuse(
            text,
            m.start(),
            "this .html file mixes PHP code and <% %> code",
            "Upload it with its server-page extension (.php, .jsp, .aspx or .erb).",
        )
    if php:
        return PhpTemplateFilter()
    if percent:
        if _HTML_JSP_HINT.search(text):
            return JspFilter()
        return AspxFilter()
    return None


# --- .php: one extension, two formats --------------------------------------------------
# A .php file is either a localization array (Laravel/CodeIgniter, php_array_filter from
# format Batch 11, when that module is present) or a page template (PhpTemplateFilter).
# The choice is made from the ORIGINAL bytes on parse and on build alike, so a document
# always rebuilds with the filter that parsed it. A file that is PHP code only and not a
# language array is refused rather than guessed at.


def _lang_array_module():
    """php_array_filter (format Batch 11) when it is on this line, else None. Only the
    module's own absence counts: a broken import inside it still raises."""
    import importlib

    try:
        return importlib.import_module("app.filters.php_array_filter")
    except ModuleNotFoundError as exc:
        if exc.name != "app.filters.php_array_filter":
            raise
        return None


def _lang_array_filter(data: bytes):
    pa = _lang_array_module()
    if pa is None:
        return None
    check = getattr(pa, "is_php_lang_array", None)
    if check is None or not check(data):
        return None
    return pa.PhpArrayFilter()


def _has_page_text(data: bytes) -> bool:
    text = decode_text_lossless(data, declared=True)
    pos = 0
    for s, e, _inline in _php_islands(text):
        if text[pos:s].strip():
            return True
        pos = e
    return bool(text[pos:].strip())


# Import (and so register) the array filter BEFORE the router below, so the router owns
# fmt "php" whatever order app/filters/__init__.py imports the two modules in.
_PA = _lang_array_module()


@register
class PhpFilter(Filter):
    """Router for .php (registered after php_array_filter, so this entry owns the fmt)."""

    fmt = "php"
    exts = ()

    def _pick(self, data: bytes):
        array_filter = _lang_array_filter(data)
        if array_filter is not None:
            return array_filter
        if not _has_page_text(data):
            raise ServerPageRefused(
                "This .php file is PHP code with no page text and is not a language array "
                "(return [...] or $lang['key'] = '...'), so there is nothing to translate safely."
            )
        return _TEMPLATE

    def parse(self, data: bytes, provider: str | None = None, **kwargs) -> ParseResult:
        f = self._pick(data)
        return f.parse(data, provider, **kwargs) if f is _TEMPLATE else f.parse(data, provider)

    def build(self, original: bytes, paragraph_texts: dict) -> bytes:
        return self._pick(original).build(original, paragraph_texts)

    def build_localized(
        self, original: bytes, paragraph_texts: dict, target_locale: str | None = None
    ) -> bytes:
        return self._pick(original).build_localized(original, paragraph_texts, target_locale)

    def _forward_records(self, data: bytes) -> list[dict]:
        """Key view (keys bridge): only a PHP language array has keys."""
        array_filter = _lang_array_filter(data)
        if array_filter is None:
            raise ServerPageRefused(
                "This .php file is a page template, not a PHP language array "
                "(return [...] or $lang['key'] = '...'), so it has no keys to import. "
                "Upload it as a document instead."
            )
        return array_filter.records(data)


# The router owns fmt "php", so the language-array key view (php_array_filter.records,
# read by the keys bridge through `callable(getattr(filter, "records"))`) is exposed
# here, and only when that module is on this line: a template-only line has no key view.
if _PA is not None and callable(getattr(getattr(_PA, "PhpArrayFilter", None), "records", None)):
    PhpFilter.records = PhpFilter._forward_records

_TEMPLATE = PhpTemplateFilter()
