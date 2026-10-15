"""XSL / XSLT filter: the literal result text of an XSLT 1.0 / 2.0 / 3.0 stylesheet.

What is translated
  * Text that the stylesheet writes to its output: text nodes inside literal result
    elements (``<p>Hello</p>``), inside ``xsl:text``, and inside the sequence-constructor
    instructions whose text is output (``xsl:template``, ``xsl:if``, ``xsl:when``,
    ``xsl:otherwise``, ``xsl:for-each``, ``xsl:element``, ...).
  * Inline structure stays inside one unit: HTML phrasing elements (``<b>``, ``<a>``,
    ``<span>``, ...) and ``xsl:text`` become paired ``⟦gN⟧…⟦/gN⟧`` tokens; ``xsl:value-of``,
    ``xsl:copy-of``, ``xsl:apply-templates``, ``xsl:call-template``, ``xsl:number``,
    comments, entity references and XSLT 3.0 text value template expressions (``{$n}``
    under ``expand-text="yes"``) become protected ``⟦xN⟧`` tokens written back verbatim.
  * Translatable attributes of literal result elements (default ``alt``, ``title``,
    ``placeholder``, ``aria-label``) and the content of ``<xsl:attribute name="title">``
    (same names). Attribute value template expressions (``{@name}``) are protected tokens;
    ``{{`` / ``}}`` are shown as single braces and re-doubled on build.

Never translated: any attribute of an ``xsl:*`` instruction (select, test, match, name,
...), XPath expressions, text directly inside ``xsl:variable`` / ``xsl:param`` /
``xsl:with-param`` / ``xsl:function`` / ``xsl:sequence`` / ``xsl:value-of`` (often a code
value compared elsewhere; literal result elements inside them ARE translated),
``xsl:message``, ``xsl:comment``, ``xsl:processing-instruction``, ``xsl:key``,
``xsl:sort`` and the other declarations, any literal result element marked
``translate="no"`` / ``its:translate="no"`` (with its descendants), and literal result
``<script>`` / ``<style>`` elements (code in the HTML output, #694).

Fidelity: lxml checks the file (hardened: entities never resolved, no DTD load, no
network, no huge tree); a small lexer over the decoded text gives exact character spans
(element counts are cross-checked against lxml). build() splices only the units whose
translation differs from the source text, so every other byte (prolog, namespaces,
attribute quoting, comments, indentation, CRLF, BOM) is identical on round trip. Parse
and build share the one walk, so unit indices always line up: text units first, in
document order, then attribute units in document order.

DOCTYPE: an internal subset is accepted only when it declares simple character entities
(``<!ENTITY nbsp "&#160;">``, the common XSLT idiom); their references stay protected
tokens. Anything else in a DTD is refused (billion laughs / XXE).

Spec references: XSLT 1.0 https://www.w3.org/TR/1999/REC-xslt-19991116 (3.4 whitespace,
7.1.1 literal result elements, 7.1.3 xsl:attribute, 7.2 xsl:text, 7.6.2 attribute value
templates); XSLT 3.0 https://www.w3.org/TR/xslt-30/ (4.3 stylesheet whitespace, 5.6.1
AVTs, 5.6.2 text value templates and [xsl:]expand-text, 5.7 sequence constructors).

Documented in: docs/architecture/translation-pipeline.md
"""

import re

from lxml import etree

from app import config
from app.filters.base import (
    _DECLARED_OK,
    EmptyDocumentError,
    Filter,
    InputLimitError,
    ParseResult,
    TextUnit,
    _codec_name,
    check_segment_cap,
    check_unit_texts,
    note_source_encoding,
    register,
)
from app.filters.xml_guard import make_hardened_parser
from app.filters.xml_text import xml_target_text

XSL_NS = "http://www.w3.org/1999/XSL/Transform"
ITS_NS = "http://www.w3.org/2005/11/its"
_XML_NS = "http://www.w3.org/XML/1998/namespace"

# Attributes of literal result elements (and xsl:attribute names) that hold user text.
TRANSLATABLE_ATTRIBUTES = ("alt", "aria-label", "placeholder", "title")

# xsl:* elements whose direct text children are written to the output as text.
_TEXT_CONTEXT = frozenset(
    {
        "break",
        "catch",
        "copy",
        "document",
        "element",
        "fallback",
        "for-each",
        "for-each-group",
        "if",
        "iterate",
        "matching-substring",
        "merge-action",
        "non-matching-substring",
        "on-completion",
        "on-empty",
        "on-non-empty",
        "otherwise",
        "perform-sort",
        "result-document",
        "template",
        "text",
        "try",
        "when",
        "where-populated",
    }
)
# Literal result elements whose content is code in the HTML output (#694): never units,
# like the HTML filter's script/style (any case). Only in no namespace (HTML output),
# XHTML or SVG: an element named style in another vocabulary (a product catalogue's
# <cat:style>Classic</cat:style>) is document text and stays translatable.
_CODE_RESULT_ELEMENTS = frozenset({"script", "style"})
_CODE_RESULT_NAMESPACES = frozenset(
    {None, "", "http://www.w3.org/1999/xhtml", "http://www.w3.org/2000/svg"}
)
# xsl:* elements whose whole subtree never holds document text.
_SKIP_SUBTREE = frozenset(
    {
        "accumulator",
        "assert",
        "character-map",
        "comment",
        "context-item",
        "decimal-format",
        "global-context-item",
        "import",
        "import-schema",
        "include",
        "key",
        "message",
        "mode",
        "namespace",
        "namespace-alias",
        "output",
        "preserve-space",
        "processing-instruction",
        "sort",
        "strip-space",
        "use-package",
    }
)
# xsl:* instructions that stand inside a sentence as one protected point token.
_POINT_XSL = frozenset(
    {
        "apply-imports",
        "apply-templates",
        "call-template",
        "copy-of",
        "next-match",
        "number",
        "sequence",
        "value-of",
    }
)
# Literal result elements that stay inside a sentence (HTML phrasing content, XSL-FO inlines).
_INLINE_NAMES = frozenset(
    {
        "a",
        "abbr",
        "acronym",
        "b",
        "basic-link",
        "bdi",
        "bdo",
        "big",
        "br",
        "cite",
        "code",
        "data",
        "del",
        "dfn",
        "em",
        "font",
        "i",
        "img",
        "inline",
        "ins",
        "kbd",
        "label",
        "mark",
        "q",
        "s",
        "samp",
        "small",
        "span",
        "strike",
        "strong",
        "sub",
        "sup",
        "time",
        "tt",
        "u",
        "var",
        "wbr",
    }
)
_YES = frozenset({"yes", "true", "1"})
_NO = frozenset({"no", "false", "0"})
_MAX_DEPTH = 200

_NAME = r"[^\s=/<>!?\"']+"
_START_TAG = re.compile(
    r"<(" + _NAME + r")((?:\s+" + _NAME + r"\s*=\s*(?:\"[^\"]*\"|'[^']*'))*)\s*(/?)>"
)
_ATTR = re.compile(r"(" + _NAME + r")\s*=\s*(?:\"([^\"]*)\"|'([^']*)')")
_END_TAG = re.compile(r"</(" + _NAME + r")\s*>")
_ENTITY_REF = re.compile(r"&([A-Za-z_:][\w.:-]*|#x[0-9A-Fa-f]+|#[0-9]+);")
_PREDEF = {"lt": "<", "gt": ">", "amp": "&", "quot": '"', "apos": "'"}
_ENCODING_DECL = re.compile(r"""\A<\?xml[^>]*?\bencoding\s*=\s*["']([A-Za-z0-9._:-]+)["']""")
_DOCTYPE = re.compile(r"<!DOCTYPE\s+(" + _NAME + r")\s*")
_CHAR_REF = r"&#[0-9]{1,7};|&#x[0-9A-Fa-f]{1,6};"
_SIMPLE_ENTITY = re.compile(
    r"<!ENTITY\s+[A-Za-z_][\w.-]*\s+(?:"
    + '"(?:'
    + _CHAR_REF
    + '|[^"&%<]{0,16})"'
    + "|'(?:"
    + _CHAR_REF
    + "|[^'&%<]{0,16})')"
    + r"\s*>"
)
_PROLOG_SKIP = re.compile(r"\s+|<\?.*?\?>|<!--.*?-->", re.DOTALL)
_SUBSET_SKIP = re.compile(r"\s+|<!--(?:(?!-->).){0,2000}-->", re.DOTALL)
_TOKEN = re.compile(r"⟦(/?)([a-z])(\d+)⟧")
_WS_NL = re.compile(r"[ \t\r\n]*\n[ \t\r\n]*")


class XsltParseError(ValueError):
    """An .xsl/.xslt upload that is not well-formed XML, carries a DTD we refuse, or uses an
    encoding we do not read. Caught by the upload routes as a polite parse refusal."""


def check_xslt_budget(raw: bytes) -> None:
    """Pre-parse budget (#571 pattern): refuse before lxml builds a tree."""
    if raw.count(b"<") + raw.count(b"{") > 40 * config.MAX_DOCUMENT_SEGMENTS:
        raise InputLimitError(
            f"This XSLT file has far more markup than MAX_DOCUMENT_SEGMENTS allows "
            f"({config.MAX_DOCUMENT_SEGMENTS}). Split it into smaller files."
        )


# ---------------------------------------------------------------- decode + checks


def _decode(raw: bytes) -> tuple[str, str, bytes]:
    """Return (text, codec, bom). Same decode at parse and build."""
    if raw.startswith(b"\xef\xbb\xbf"):
        codec, bom = "utf-8", b"\xef\xbb\xbf"
    elif raw.startswith(b"\xff\xfe"):
        codec, bom = "utf-16-le", b"\xff\xfe"
    elif raw.startswith(b"\xfe\xff"):
        codec, bom = "utf-16-be", b"\xfe\xff"
    else:
        bom = b""
        m = _ENCODING_DECL.match(raw[:200].decode("ascii", "replace"))
        declared = _codec_name(m.group(1)) if m else None
        if declared in (None, "utf-8"):
            codec = "utf-8"
        elif declared in _DECLARED_OK and declared != "utf-16":
            codec = declared
        else:
            raise XsltParseError(
                f"This XSLT file declares the encoding {m.group(1)!r}, which is not supported; "
                "re-save the file as UTF-8."
            )
    try:
        return raw[len(bom) :].decode(codec), codec, bom
    except UnicodeDecodeError as exc:
        raise XsltParseError(
            f"This XSLT file is not valid {codec} (byte at position {exc.start + len(bom)}); "
            "re-save the file as UTF-8."
        ) from exc


def _doctype_span(text: str) -> tuple[int, int] | None:
    """Locate a DOCTYPE in the prolog and vet its internal subset: only simple character
    entities (and comments) are allowed. Returns the (start, end) span or None.

    Only the prolog is searched (XML declaration, PIs, comments, whitespace before the
    root): a stylesheet comment or xsl:text that merely MENTIONS a DOCTYPE in the body is
    not a declaration (gate fix, train-16)."""
    i = 0
    while True:
        m = _PROLOG_SKIP.match(text, i)
        if not m or m.end() == i:
            break
        i = m.end()
    if not text.startswith("<!DOCTYPE", i):
        if text[i : i + 9].upper() == "<!DOCTYPE":
            raise XsltParseError("This XSLT file has a malformed DOCTYPE.")
        return None
    m = _DOCTYPE.match(text, i)
    if not m:
        raise XsltParseError("This XSLT file has a malformed DOCTYPE.")
    j = m.end()
    lb = text.find("[", j)
    gt = text.find(">", j)
    if gt < 0:
        raise XsltParseError("This XSLT file has a malformed DOCTYPE.")
    if lb < 0 or gt < lb:
        return i, gt + 1  # no internal subset (external id only: never loaded)
    pos = lb + 1
    while True:
        sm = _SUBSET_SKIP.match(text, pos)
        if sm and sm.end() > pos:
            pos = sm.end()
            continue
        em = _SIMPLE_ENTITY.match(text, pos)
        if em:
            pos = em.end()
            continue
        break
    close = re.compile(r"\]\s*>").match(text, pos)
    if not close:
        raise XsltParseError(
            "This XSLT file's DOCTYPE declares more than simple character entities "
            '(such as <!ENTITY nbsp "&#160;">); refused for safety.'
        )
    return i, close.end()


def _check_xml(raw: bytes) -> int:
    """Hardened well-formedness gate; returns the element count lxml sees."""
    try:
        root = etree.fromstring(raw, parser=make_hardened_parser())
    except etree.XMLSyntaxError as exc:
        raise XsltParseError(f"This XSLT file is not well-formed XML: {exc}") from exc
    return sum(1 for _ in root.iter(etree.Element))


# ---------------------------------------------------------------- lexer


class _Attr:
    __slots__ = ("local", "name", "prefix", "quote", "raw", "ve", "vs")

    def __init__(self, name, vs, raw, quote):
        self.name, self.vs, self.raw, self.quote = name, vs, raw, quote
        self.ve = vs + len(raw)
        self.prefix, _, self.local = name.rpartition(":")


class _El:
    __slots__ = ("attrs", "children", "e", "local", "ns", "s", "uri")

    def __init__(self, s, local, uri, ns, attrs):
        self.s, self.local, self.uri, self.ns, self.attrs = s, local, uri, ns, attrs
        self.e = s
        self.children = []

    def attr(self, name: str, uri: str | None = None):
        for a in self.attrs:
            if uri is None and a.name == name:
                return a
            if uri is not None and a.local == name and a.prefix and self.ns.get(a.prefix) == uri:
                return a
        return None


class _Leaf:
    __slots__ = ("e", "kind", "s")

    def __init__(self, kind, s, e):
        self.kind, self.s, self.e = kind, s, e


def _unescape(s: str) -> str:
    def rep(m):
        g = m.group(1)
        if g[0] == "#":
            return chr(int(g[2:], 16) if g[1] in "xX" else int(g[1:]))
        return _PREDEF.get(g, m.group(0))

    return _ENTITY_REF.sub(rep, s)


def _text_leaves(text: str, s: int, e: int, out: list) -> None:
    """Split character data into text leaves and (non-predefined) entity-reference leaves."""
    pos = s
    for m in _ENTITY_REF.finditer(text, s, e):
        g = m.group(1)
        if g[0] == "#" or g in _PREDEF:
            continue
        if m.start() > pos:
            out.append(_Leaf("text", pos, m.start()))
        out.append(_Leaf("entity", m.start(), m.end()))
        pos = m.end()
    if e > pos:
        out.append(_Leaf("text", pos, e))


def _lex(text: str, doctype: tuple[int, int] | None) -> tuple[_El, int]:
    """One pass over well-formed XML: returns (root element, element count)."""
    root = None
    stack: list[_El] = []
    count = 0
    pos, n = 0, len(text)
    while pos < n:
        lt = text.find("<", pos)
        if lt < 0:
            lt = n
        if stack and lt > pos:
            _text_leaves(text, pos, lt, stack[-1].children)
        if lt >= n:
            break
        if doctype and lt == doctype[0]:
            pos = doctype[1]
            continue
        if text.startswith("<!--", lt):
            end, nxt, kind = text.find("-->", lt + 4), 3, "comment"
        elif text.startswith("<![CDATA[", lt):
            end, nxt, kind = text.find("]]>", lt + 9), 3, "cdata"
        elif text.startswith("<?", lt):
            end, nxt, kind = text.find("?>", lt + 2), 2, "pi"
        elif text.startswith("</", lt):
            m = _END_TAG.match(text, lt)
            if not m or not stack:
                raise XsltParseError("This XSLT file could not be read (unbalanced end tag).")
            stack.pop().e = m.end()
            pos = m.end()
            continue
        elif text.startswith("<!", lt):
            raise XsltParseError("This XSLT file could not be read (unexpected declaration).")
        else:
            m = _START_TAG.match(text, lt)
            if not m:
                raise XsltParseError("This XSLT file could not be read (malformed start tag).")
            ns = dict(stack[-1].ns) if stack else {"xml": _XML_NS}
            attrs = []
            for am in _ATTR.finditer(m.group(2)):
                gi = 2 if am.group(2) is not None else 3
                a = _Attr(am.group(1), m.start(2) + am.start(gi), am.group(gi), "\"'"[gi - 2])
                attrs.append(a)
                if a.name == "xmlns":
                    ns[None] = _unescape(a.raw)
                elif a.prefix == "xmlns":
                    ns[a.local] = _unescape(a.raw)
            prefix, _, local = m.group(1).rpartition(":")
            el = _El(lt, local, ns.get(prefix or None), ns, attrs)
            count += 1
            if stack:
                stack[-1].children.append(el)
            elif root is None:
                root = el
            if m.group(3) == "/":
                el.e = m.end()
            else:
                stack.append(el)
                if len(stack) > _MAX_DEPTH:
                    raise XsltParseError(
                        f"This XSLT file nests elements more than {_MAX_DEPTH} deep; refused."
                    )
            pos = m.end()
            continue
        if end < 0:
            raise XsltParseError("This XSLT file could not be read (unterminated markup).")
        if stack:
            stack[-1].children.append(_Leaf(kind, lt, end + nxt))
        pos = end + nxt
    if root is None or stack:
        raise XsltParseError("This XSLT file could not be read.")
    return root, count


# ---------------------------------------------------------------- planner


class _Ctx:
    __slots__ = ("preserve", "text_ok", "tvt")

    def __init__(self, text_ok, tvt, preserve):
        self.text_ok, self.tvt, self.preserve = text_ok, tvt, preserve


def _is_xsl(el: _El) -> bool:
    return el.uri == XSL_NS


def _child_ctx(el: _El, ctx: _Ctx) -> _Ctx | None:
    """The context for `el`'s content, or None when nothing under `el` is translated."""
    tvt, preserve = ctx.tvt, ctx.preserve
    a = el.attr("expand-text") if _is_xsl(el) else el.attr("expand-text", XSL_NS)
    if a is not None:
        v = a.raw.strip().lower()
        tvt = True if v in _YES else False if v in _NO else tvt
    sp = el.attr("space", _XML_NS)
    if sp is not None:
        preserve = sp.raw.strip() == "preserve"
    if _is_xsl(el):
        if el.local in _SKIP_SUBTREE:
            return None
        if el.local == "text":
            doe = el.attr("disable-output-escaping")
            if doe is not None and doe.raw.strip().lower() in _YES:
                return None  # raw markup written unescaped: never shown as text
            return _Ctx(ctx.text_ok, tvt, True)
        if el.local == "attribute":
            name = el.attr("name")
            ok = name is not None and name.raw.strip() in TRANSLATABLE_ATTRIBUTES
            return _Ctx(ok, tvt, preserve)
        return _Ctx(el.local in _TEXT_CONTEXT, tvt, preserve)
    if el.local.lower() in _CODE_RESULT_ELEMENTS and el.uri in _CODE_RESULT_NAMESPACES:
        return None  # #694: script / style output is code, never document text
    for t in (el.attr("translate"), el.attr("translate", ITS_NS)):
        if t is not None and t.raw.strip().lower() == "no":
            return None
    return _Ctx(True, tvt, preserve)


def _inline_kind(node, ctx: _Ctx) -> str:
    """'text' | 'point' | 'paired' | 'break' for a child inside a text-bearing context."""
    if isinstance(node, _Leaf):
        return "text" if node.kind == "text" else "point"
    if _is_xsl(node):
        if node.local in _POINT_XSL:
            has_el = any(isinstance(c, _El) for c in node.children)
            return (
                "break"
                if has_el and node.local in ("apply-templates", "call-template")
                else "point"
            )
        if node.local == "text" and not any(isinstance(c, _El) for c in node.children):
            return "paired" if node.children and _child_ctx(node, ctx) else "point"
        return "break"
    if node.local.lower() not in _INLINE_NAMES:
        return "break"
    cctx = _child_ctx(node, ctx)
    if cctx is None:  # translate="no" inline element: one protected token
        return "point"
    if not node.children:
        return "point"
    if all(_inline_kind(c, cctx) != "break" for c in node.children):
        return "paired"
    return "break"


def _content_span(text: str, el: _El) -> tuple[int, int, int, int]:
    """(open_start, open_end, close_start, close_end) of a paired element."""
    oe = _START_TAG.match(text, el.s).end()  # '>' may sit inside an attribute value
    cs = text.rindex("</", 0, el.e)
    return el.s, oe, cs, el.e


def _has_text(text: str, nodes, ctx: _Ctx) -> bool:
    for c in nodes:
        if isinstance(c, _Leaf):
            if c.kind == "text" and text[c.s : c.e].strip():
                return True
        elif _inline_kind(c, ctx) == "paired":
            cctx = _child_ctx(c, ctx)
            if cctx and cctx.text_ok and _has_text(text, c.children, cctx):
                return True
    return False


def _avt_end(raw: str, i: int) -> int:
    """Index just past the '}' that closes the expression opened at raw[i] == '{', or -1.
    String literals and nested braces (XPath 3.1 maps) are honoured (XSLT 1.0 7.6.2)."""
    depth, j, n = 0, i, len(raw)
    while j < n:
        c = raw[j]
        if c in "\"'":
            k = raw.find(c, j + 1)
            if k < 0:
                return -1
            j = k + 1
            continue
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return j + 1
        j += 1
    return -1


def _split_template(raw: str, base: int, tvt: bool):
    """Split raw character data into ('lit', s, e) and ('expr', s, e) parts (absolute
    offsets). With tvt False everything is literal. Raises ValueError on a malformed
    value template (the caller then leaves the value untranslated)."""
    if not tvt:
        return [("lit", base, base + len(raw))]
    parts, i, start, n = [], 0, 0, len(raw)
    while i < n:
        c = raw[i]
        if (c == "{" and raw.startswith("{{", i)) or (c == "}" and raw.startswith("}}", i)):
            i += 2
        elif c == "{":
            end = _avt_end(raw, i)
            if end < 0:
                raise ValueError("unterminated value template")
            if i > start:
                parts.append(("lit", base + start, base + i))
            parts.append(("expr", base + i, base + end))
            i = start = end
        elif c == "}":
            raise ValueError("lone '}' in value template")
        else:
            i += 1
    if n > start:
        parts.append(("lit", base + start, base + n))
    return parts


class _Unit:
    __slots__ = ("crlf", "ctx", "e", "kind", "pieces", "quote", "s", "text")

    def __init__(self, kind, s, e, text, pieces, ctx, crlf=False, quote=""):
        self.kind, self.s, self.e, self.text, self.pieces = kind, s, e, text, pieces
        self.ctx, self.crlf, self.quote = ctx, crlf, quote


def _lit_display(raw: str, ctx: _Ctx, attr: bool) -> str:
    s = raw.replace("\r\n", "\n").replace("\r", "\n")
    if attr:
        s = re.sub(r"[\t\n]", " ", s)
    elif not ctx.preserve:
        s = _WS_NL.sub(" ", s)
    s = _unescape(s)
    if ctx.tvt:
        s = s.replace("{{", "{").replace("}}", "}")
    return s


class _Planner:
    def __init__(self, text: str):
        self.text = text
        self.units: list[_Unit] = []

    def cap(self):
        check_segment_cap(len(self.units))

    # ---- text units
    def visit(self, el: _El, ctx: _Ctx) -> None:
        kids = el.children
        if not ctx.text_ok:
            for c in kids:
                if isinstance(c, _El):
                    cctx = _child_ctx(c, ctx)
                    if cctx is not None:
                        self.visit(c, cctx)
            return
        run: list = []
        # Directly inside an xsl:* instruction a whitespace-only text node is stripped from
        # the stylesheet (XSLT 1.0 3.4, 3.0 4.3), so it separates outputs: end the run there.
        ws_breaks = _is_xsl(el) and el.local != "text" and not ctx.preserve
        for c in kids:
            if (
                ws_breaks
                and isinstance(c, _Leaf)
                and c.kind == "text"
                and not self.text[c.s : c.e].strip()
            ):
                self.run(run, ctx)
                run = []
            elif _inline_kind(c, ctx) == "break":
                self.run(run, ctx)
                run = []
                cctx = _child_ctx(c, ctx)
                if cctx is not None:
                    self.visit(c, cctx)
            else:
                run.append(c)
        self.run(run, ctx)

    def run(self, nodes: list, ctx: _Ctx) -> None:
        text = self.text
        while True:
            nodes = self._trim(nodes)
            if not nodes or not _has_text(text, nodes, ctx):
                return
            if len(nodes) == 1 and _inline_kind(nodes[0], ctx) == "paired":
                inner = _child_ctx(nodes[0], ctx)
                if inner is None or not inner.text_ok:
                    return
                nodes, ctx = list(nodes[0].children), inner  # unwrap a lone wrapper
                continue
            break
        pieces: list = []  # ('lit', s, e, ctx) | ('point', s, e) | ('open'|'close', s, e, ctx, id)
        self._pieces(nodes, ctx, pieces)
        # trim edge whitespace from the first/last literal pieces
        while pieces and pieces[0][0] == "lit":
            k, s, e, c = pieces[0]
            raw = text[s:e]
            s2 = s + (len(raw) - len(raw.lstrip(" \t\r\n")))
            if s2 >= e:
                pieces.pop(0)
                continue
            pieces[0] = (k, s2, e, c)
            break
        while pieces and pieces[-1][0] == "lit":
            k, s, e, c = pieces[-1]
            raw = text[s:e]
            e2 = s + len(raw.rstrip(" \t\r\n"))
            if e2 <= s:
                pieces.pop()
                continue
            pieces[-1] = (k, s, e2, c)
            break
        if not pieces:
            return
        out, named, n, ids = [], [], 0, {}
        for p in pieces:
            if p[0] == "lit":
                out.append(_lit_display(text[p[1] : p[2]], p[3], attr=False))
                named.append((None, p))
            elif p[0] == "point":
                n += 1
                out.append(f"⟦x{n}⟧")
                named.append((f"x{n}", p))
            elif p[0] == "open":
                n += 1
                ids[p[4]] = n
                out.append(f"⟦g{n}⟧")
                named.append((f"g{n}", p))
            else:
                out.append(f"⟦/g{ids[p[4]]}⟧")
                named.append((f"/g{ids[p[4]]}", p))
        s, e = pieces[0][1], pieces[-1][2]
        self.units.append(_Unit("text", s, e, "".join(out), named, ctx, crlf="\r\n" in text[s:e]))
        self.cap()

    def _trim(self, nodes: list) -> list:
        text = self.text

        def blank(c):
            if isinstance(c, _Leaf):
                return c.kind in ("comment", "pi") or (
                    c.kind == "text" and not text[c.s : c.e].strip()
                )
            return False

        i, j = 0, len(nodes)
        while i < j and blank(nodes[i]):
            i += 1
        while j > i and blank(nodes[j - 1]):
            j -= 1
        return nodes[i:j]

    def _pieces(self, nodes, ctx: _Ctx, out: list) -> None:
        text = self.text
        for c in nodes:
            if isinstance(c, _Leaf):
                if c.kind != "text":
                    out.append(("point", c.s, c.e))
                    continue
                try:
                    parts = _split_template(text[c.s : c.e], c.s, ctx.tvt)
                except ValueError:
                    parts = [("expr", c.s, c.e)]  # malformed template: keep it whole
                for kind, s, e in parts:
                    out.append(("lit", s, e, ctx) if kind == "lit" else ("point", s, e))
                continue
            kind = _inline_kind(c, ctx)
            if kind == "point":
                out.append(("point", c.s, c.e))
                continue
            os_, oe, cs, ce = _content_span(text, c)
            cctx = _child_ctx(c, ctx)
            key = id(c)
            out.append(("open", os_, oe, cctx, key))
            self._pieces(c.children, cctx, out)
            out.append(("close", cs, ce, cctx, key))

    # ---- attribute units
    def attrs(self, el: _El, ctx: _Ctx | None) -> None:
        if ctx is None:
            return
        if not _is_xsl(el):
            for a in el.attrs:
                if a.prefix or a.local not in TRANSLATABLE_ATTRIBUTES:
                    continue
                self._attr_unit(a)
        for c in el.children:
            if isinstance(c, _El):
                self.attrs(c, _child_ctx(c, ctx))

    def _attr_unit(self, a: _Attr) -> None:
        text = self.text
        actx = _Ctx(True, True, False)  # LRE attributes are always AVTs (XSLT 1.0 7.6.2)
        parts: list = []
        pos = a.vs
        for m in _ENTITY_REF.finditer(text, a.vs, a.ve):
            g = m.group(1)
            if g[0] == "#" or g in _PREDEF:
                continue
            parts.append(("lit", pos, m.start()))
            parts.append(("ent", m.start(), m.end()))
            pos = m.end()
        parts.append(("lit", pos, a.ve))
        pieces = []
        try:
            for kind, s, e in parts:
                if kind == "ent":
                    pieces.append(("point", s, e))
                    continue
                for k2, s2, e2 in _split_template(text[s:e], s, True):
                    pieces.append(("lit", s2, e2, actx) if k2 == "lit" else ("point", s2, e2))
        except ValueError:
            return
        pieces = [p for p in pieces if p[1] < p[2]]
        lits = "".join(
            _lit_display(text[p[1] : p[2]], actx, attr=True) for p in pieces if p[0] == "lit"
        )
        if not any(ch.isalnum() for ch in lits):
            return
        out, named, n = [], [], 0
        for p in pieces:
            if p[0] == "lit":
                out.append(_lit_display(text[p[1] : p[2]], actx, attr=True))
                named.append((None, p))
            else:
                n += 1
                out.append(f"⟦x{n}⟧")
                named.append((f"x{n}", p))
        self.units.append(
            _Unit(f"attr:{a.local}", a.vs, a.ve, "".join(out), named, actx, quote=a.quote)
        )
        self.cap()


def _plan(text: str, doctype) -> tuple[list[_Unit], _El, int]:
    """Text units (document order), then attribute units (document order). A simplified
    stylesheet (root is a literal result element, XSLT 1.0 2.3) walks the same way."""
    root, count = _lex(text, doctype)
    p = _Planner(text)
    rctx = _child_ctx(root, _Ctx(False, False, False))
    if rctx is not None:
        p.visit(root, rctx)
    text_units = p.units
    p.units = []
    p.attrs(root, rctx)
    return text_units + p.units, root, count


# ---------------------------------------------------------------- build


def _escape_lit(s: str, ctx: _Ctx, attr: bool, quote: str, crlf: bool) -> str:
    s = s.replace("&", "&amp;").replace("<", "&lt;")
    if attr:
        s = s.replace('"', "&quot;") if quote == '"' else s.replace("'", "&apos;")
        s = s.replace("\t", "&#9;").replace("\n", "&#10;").replace("\r", "&#13;")
    else:
        s = s.replace(">", "&gt;")
        if crlf:
            s = s.replace("\n", "\r\n")
    if ctx.tvt:
        s = s.replace("{", "{{").replace("}", "}}")
    return s


def _rebuild_unit(unit: _Unit, target: str, render) -> str:
    tokens = {name: p for name, p in unit.pieces if name}
    attr = unit.kind != "text"
    out: list[str] = []
    stack: list[str] = []  # open g names
    used: set[str] = set()
    ctxs = [unit.ctx]

    def lit(s):
        if s:
            out.append(_escape_lit(s, ctxs[-1], attr, unit.quote, unit.crlf))

    pos = 0
    for m in _TOKEN.finditer(target):
        lit(target[pos : m.start()])
        pos = m.end()
        close, kind, num = m.group(1), m.group(2), m.group(3)
        name = f"{kind}{num}"
        if close:
            if name in stack:
                while stack:
                    top = stack.pop()
                    ctxs.pop()
                    p = tokens["/" + top]
                    out.append(render(p[1], p[2]))
                    if top == name:
                        break
            continue
        p = tokens.get(name)
        if p is None or name in used:
            continue
        used.add(name)
        out.append(render(p[1], p[2]))
        if p[0] == "open":
            stack.append(name)
            ctxs.append(p[3])
    lit(target[pos:])
    while stack:
        top = stack.pop()
        ctxs.pop()
        p = tokens["/" + top]
        out.append(render(p[1], p[2]))
    for name, p in unit.pieces:  # a dropped protected token is appended, never lost
        if name and p[0] == "point" and name not in used:
            out.append(render(p[1], p[2]))
    return "".join(out)


def _lang_edits(text: str, root: _El, tag: str) -> list:
    """Literal lang / xml:lang on <html> literal result elements -> the target tag."""
    edits = []

    def walk(el):
        if not _is_xsl(el) and el.local.lower() == "html":
            for a in el.attrs:
                if (
                    a.name == "lang" or (a.local == "lang" and el.ns.get(a.prefix) == _XML_NS)
                ) and ("{" not in a.raw):
                    edits.append(
                        (
                            a.vs,
                            a.ve,
                            _escape_lit(tag, _Ctx(True, False, False), True, a.quote, False),
                        )
                    )
        for c in el.children:
            if isinstance(c, _El):
                walk(c)

    walk(root)
    return edits


def _load(raw: bytes, capped: bool):
    raw = bytes(raw)
    if capped:
        check_xslt_budget(raw)
    text, codec, bom = _decode(raw)
    doctype = _doctype_span(text)
    count = _check_xml(raw)
    return text, codec, bom, doctype, count


def _rebuild(original: bytes, paragraph_texts: dict, locale=None) -> bytes:
    text, codec, bom, doctype, _count = _load(original, capped=False)
    units, root, _ = _plan(text, doctype)
    text_edits, attr_edits = [], []
    for i, u in enumerate(units):
        new = paragraph_texts.get(str(i))
        if new is None or new == u.text:
            continue
        (text_edits if u.kind == "text" else attr_edits).append((u, xml_target_text(new)))
    lang = []
    if locale:
        from app.filters.a11y import target_tag

        tag = target_tag(locale)
        if tag:
            lang = _lang_edits(text, root, tag)
    if not text_edits and not attr_edits and not lang:
        return bytes(original)
    # attribute edits as (s, e, replacement); render() applies those inside a slice
    flat = []
    for u, new in attr_edits:
        flat.append((u.s, u.e, None, u, new))
    for s, e, rep in lang:
        flat.append((s, e, rep, None, None))
    flat.sort(key=lambda t: t[0])

    def render(s, e):
        out, last = [], s
        for fs, fe, rep, u, new in flat:
            if fs < s or fe > e or fs < last:
                continue
            out.append(text[last:fs])
            out.append(rep if rep is not None else _rebuild_unit(u, new, lambda a, b: text[a:b]))
            last = fe
        out.append(text[last:e])
        return "".join(out)

    top = []
    for u, new in text_edits:
        top.append((u.s, u.e, _rebuild_unit(u, new, render)))
    spans = [(t[0], t[1]) for t in top]
    for fs, fe, rep, u, new in flat:
        if any(s <= fs and fe <= e for s, e in spans):
            continue  # rendered inside a text unit's tokens
        top.append(
            (fs, fe, rep if rep is not None else _rebuild_unit(u, new, lambda a, b: text[a:b]))
        )
    top.sort(key=lambda t: t[0])
    out, last = [], 0
    for s, e, rep in top:
        if s < last:
            continue
        out.append(text[last:s])
        out.append(rep)
        last = e
    out.append(text[last:])
    return bom + "".join(out).encode(codec, "xmlcharrefreplace")


@register
class XsltFilter(Filter):
    fmt = "xslt"
    exts = ("xsl",)

    def parse(self, data: bytes, provider: str | None = None) -> ParseResult:
        text, codec, _bom, doctype, count = _load(data, capped=True)
        units_, _root, lex_count = _plan(text, doctype)
        if lex_count != count:
            raise XsltParseError("This XSLT file could not be read (element count mismatch).")
        texts = [u.text for u in units_]
        check_unit_texts(texts)
        if not texts:
            raise EmptyDocumentError("This XSLT file has no literal output text to translate.")
        units = [
            TextUnit(p_index=i, text=u.text, style=("" if u.kind == "text" else u.kind))
            for i, u in enumerate(units_)
        ]
        skeleton = {
            "format": "xslt",
            "units": [{"p_index": u.p_index, "text": u.text, "style": u.style} for u in units],
        }
        if codec != "utf-8":
            note_source_encoding(skeleton, codec)
        return ParseResult(skeleton=skeleton, units=units)

    def build(self, original: bytes, paragraph_texts: dict) -> bytes:
        return _rebuild(original, paragraph_texts)

    def build_localized(
        self, original: bytes, paragraph_texts: dict, target_locale: str | None = None
    ) -> bytes:
        """build() plus the target language on literal ``<html lang|xml:lang>``."""
        return _rebuild(original, paragraph_texts, target_locale)
