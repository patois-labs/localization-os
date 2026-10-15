"""#676: what a browser makes of a built HTML page, and the export check built on it.

The html filter family (html, xhtml, server pages, EPUB chapters, Flare topics, email HTML
parts, HTML Help sitemaps) finds attribute values and text with its own forward scanner
(`html_filter._tokenize`). On a few malformed constructs that scanner and a browser's
HTML5 tokenizer part ways (an abrupt `<!-->` comment, `--!>`, `<![CDATA[` outside SVG,
`</ x>`, raw-text elements such as <xmp> or <noscript>, <style>/<script> inside SVG). Where
they do, the browser can be OUTSIDE a quoted attribute value where the scanner sees the
alt text of a later tag, so a translated alt such as `x" onfocus=alert(1) y="` (quotes
escaped) still lands in the browser as attribute names, and `onfocus` becomes live.

Three layers, each enough on its own for the attack above:

1. `browser_view()`: a WHATWG HTML5 tokenizer (all tag, attribute, comment, bogus comment,
   CDATA, doctype, RCDATA / RAWTEXT / script-data / PLAINTEXT states) driven by the parts
   of tree construction that switch tokenizer states (foreign SVG / MathML content with its
   integration points and breakout tags, <select>, <noscript>, <frameset>). It runs with
   scripting on with the 2025 <select> parser (current Chrome, a normal page load) and
   scripting off with the legacy <select> rules (DOMParser, older engines, JS disabled);
   a page holding both <noscript> and <select> is also parsed in the two other
   combinations (current Chrome's DOMParser or JS off; an older engine's page load).
2. `attr_value()`: a translated value is written with `& < > " '` escaped as before, and on
   a page where the two scans might disagree also with every character an HTML5 tag state
   reacts to outside quotes (whitespace, `/`, `=`, backtick) as a character reference. Such
   a value is ONE opaque run in every tokenizer state: it can never end an attribute, start
   one or close a tag, so whatever the browser thinks surrounds it, no attribute appears.
3. `check_build()`: after the build, the output is re-parsed in every mode (and as XML for
   an XHTML document that is well-formed) and the export is refused unless every element
   the browser builds already exists in the source with the same attributes and values,
   except values the filters translate (alt, title, aria-label, ...) or the language
   declaration sets (lang, xml:lang, dir, content-language), and every script / style body
   already exists in the source.

Values are compared raw (as written); a page whose structure did not change compares equal.
"""

from __future__ import annotations

import re

from app.filters.base import InputLimitError

_WS = "\t\n\f\r "
_NAME_RUN = re.compile(r"[^\t\n\f\r />]*")
_ATTR_NAME_RUN = re.compile(r"[^\t\n\f\r />=]*")
_UNQUOTED_RUN = re.compile(r"[^\t\n\f\r >]*")
_WS_RUN = re.compile(r"[\t\n\f\r ]*")
# Fast path for the common tag shape: whitespace-separated attributes whose names hold no
# quote, "<" or "=", values double- or single-quoted or unquoted without quote, "=", "<"
# or backtick. Any string this grammar accepts is tokenized identically by the HTML5
# states in _read_tag; anything else falls through to them.
_FAST_ATTR_SRC = (
    r"[\t\n\f\r ]+([^\t\n\f\r />=\"'<][^\t\n\f\r />=\"'<]*)"
    r"(?:[\t\n\f\r ]*=[\t\n\f\r ]*(?:\"([^\"]*)\"|'([^']*)'|([^\t\n\f\r >\"'=<`]+)))?"
)
_FAST_ATTR = re.compile(_FAST_ATTR_SRC)
_FAST_TAG = re.compile(
    r"([^\t\n\f\r />]*)((?:"
    + _FAST_ATTR_SRC.replace("(", "(?:").replace("(?:?:", "(?:")
    + r")*)[\t\n\f\r ]*(/?)>"
)

_RCDATA = frozenset(("title", "textarea"))
_RAWTEXT = frozenset(("style", "xmp", "iframe", "noembed", "noframes"))
_VOID = frozenset(
    (
        "area",
        "base",
        "basefont",
        "bgsound",
        "br",
        "col",
        "embed",
        "frame",
        "hr",
        "image",
        "img",
        "input",
        "keygen",
        "link",
        "meta",
        "param",
        "source",
        "track",
        "wbr",
    )
)
# "in foreign content": an HTML start tag with one of these names leaves SVG / MathML
_BREAKOUT = frozenset(
    (
        "b",
        "big",
        "blockquote",
        "body",
        "br",
        "center",
        "code",
        "dd",
        "div",
        "dl",
        "dt",
        "em",
        "embed",
        "h1",
        "h2",
        "h3",
        "h4",
        "h5",
        "h6",
        "head",
        "hr",
        "i",
        "img",
        "li",
        "listing",
        "menu",
        "meta",
        "nobr",
        "ol",
        "p",
        "pre",
        "ruby",
        "s",
        "small",
        "span",
        "strong",
        "strike",
        "sub",
        "sup",
        "table",
        "tt",
        "u",
        "ul",
        "var",
    )
)
_SVG_HTML_IP = frozenset(("foreignobject", "desc", "title"))
_MATH_TEXT_IP = frozenset(("mi", "mo", "mn", "ms", "mtext"))
# legacy "in select" insertion mode: the start tags it does not ignore
_SELECT_KEEP = frozenset(("option", "optgroup", "hr", "script", "template"))
_SELECT_CLOSE = frozenset(("input", "keygen", "textarea", "select"))

# Attribute values the filters translate or the language declaration sets: they may differ
# between the source and the built page. Everything else must be byte-identical.
_FREE_ATTRS = frozenset(("alt", "title", "aria-label", "lang", "xml:lang", "dir"))

# integration-point kinds on the element stack
_IP_NONE, _IP_HTML, _IP_MATH_TEXT, _IP_ANNOTATION = 0, 1, 2, 3


class UnsafeMarkupRefused(InputLimitError):
    """The built page would not parse in a browser the way the source page does."""


class StartTag:
    __slots__ = ("attrs", "end", "name", "ns", "start")

    def __init__(self, name, ns, start, end, attrs):
        self.name, self.ns, self.start, self.end, self.attrs = name, ns, start, end, attrs


class BrowserView:
    """The parts of an HTML5 parse the export check needs."""

    __slots__ = ("bodies", "spans", "starts", "uncertain")

    def __init__(self):
        self.starts: list[StartTag] = []  # elements the browser creates, in order
        self.spans: set = set()  # (value start, value end, quote) of every attribute value
        self.bodies: list[tuple[str, int, str]] = []  # (name, offset, text) of script/style
        self.uncertain = False  # a construct whose parse depends on the browser


def _read_tag(text: str, p: int, view: BrowserView | None):
    """Tag name and attribute states from `p` (the first name character) to the end of the
    tag. Returns (name, attrs, self_closing, end) or None when the tag runs into EOF (the
    browser drops it). `attrs` keeps the first of duplicate names, as the browser does;
    every value span (duplicates and end-tag attributes too) goes into `view.spans`."""
    fast = _FAST_TAG.match(text, p)
    if fast:  # a tag in plain syntax: the regex grammar is a subset of the states below
        attrs = []
        seen = set()
        for am in _FAST_ATTR.finditer(text, fast.start(2), fast.end(2)):
            aname = am.group(1).lower()
            value = None
            for g, q in ((2, '"'), (3, "'"), (4, "")):
                if am.group(g) is not None:
                    value = am.group(g)
                    if view is not None:
                        view.spans.add((am.start(g), am.end(g), q))
                    break
            if aname not in seen:
                seen.add(aname)
                attrs.append((aname, value))
        return fast.group(1).lower(), attrs, bool(fast.group(3)), fast.end()
    n = len(text)
    m = _NAME_RUN.match(text, p)
    name = m.group().lower()
    i = m.end()
    attrs: list[tuple[str, str | None]] = []
    seen: set = set()
    while True:
        if i >= n:
            return None
        c = text[i]
        if c in _WS:
            i = _WS_RUN.match(text, i).end()
            continue
        if c == ">":
            return name, attrs, False, i + 1
        if c == "/":
            if i + 1 < n and text[i + 1] == ">":
                return name, attrs, True, i + 2
            i += 1
            continue
        # attribute name: a leading "=" is part of the name (before-attribute-name state)
        start = i
        i = _ATTR_NAME_RUN.match(text, i + 1 if c == "=" else i).end()
        aname = text[start:i].lower()
        value = None
        j = _WS_RUN.match(text, i).end()
        if j < n and text[j] == "=":
            i = _WS_RUN.match(text, j + 1).end()
            if i >= n:
                return None
            q = text[i]
            if q in "\"'":
                close = text.find(q, i + 1)
                if close == -1:
                    return None
                value = text[i + 1 : close]
                if view is not None:
                    view.spans.add((i + 1, close, q))
                i = close + 1
            elif q == ">":
                value = ""
            else:
                e = _UNQUOTED_RUN.match(text, i).end()
                value = text[i:e]
                if view is not None:
                    view.spans.add((i, e, ""))
                i = e
        else:
            i = j
        if aname not in seen:
            seen.add(aname)
            attrs.append((aname, value))


def _comment_end(text: str, p: int) -> int:
    """`p` just after `<!--`: index after the comment as HTML5 ends it."""
    n = len(text)
    if text.startswith(">", p):
        return p + 1  # <!--> abrupt closing
    if text.startswith("->", p):
        return p + 2  # <!---> abrupt closing
    a = text.find("-->", p)
    b = text.find("--!>", p)
    ends = [x + 3 for x in (a,) if x != -1] + [x + 4 for x in (b,) if x != -1]
    return min(ends) if ends else n


def _raw_close(text: str, p: int, name: str) -> int:
    """Index of the `<` of the appropriate end tag of an RCDATA / RAWTEXT element."""
    m = re.compile(r"</" + re.escape(name) + r"[\t\n\f\r />]", re.I).search(text, p)
    return m.start() if m else len(text)


def browser_view(text: str, *, scripting: bool = True, legacy_select: bool = False):
    """Parse `text` as an HTML5 browser does, as far as tokenizer states go."""
    from app.filters.html_filter import _raw_end  # script data escape states, shared

    view = BrowserView()
    n = len(text)
    stack: list[list] = []  # [name, ns, ip_kind, body_start or -1]
    select_open = False
    i = 0

    def foreign_top() -> bool:
        return bool(stack) and stack[-1][1] != "html"

    def pop_to(idx: int, at: int) -> None:
        while len(stack) > idx:
            name, _ns, _ip, body = stack.pop()
            if body >= 0:
                view.bodies.append((name, body, text[body:at]))

    def html_mode_for_start(name: str, attrs) -> bool:
        if not stack:
            return True
        top = stack[-1]
        if top[1] == "html" or top[2] == _IP_HTML:
            return True
        if top[2] == _IP_MATH_TEXT and name not in ("mglyph", "malignmark"):
            return True
        return top[2] == _IP_ANNOTATION and name == "svg"

    def breakout(name: str, attrs) -> bool:
        if name in _BREAKOUT:
            return True
        return name == "font" and any(a in ("color", "face", "size") for a, _v in attrs)

    def pop_foreign(at: int) -> None:
        k = len(stack)
        while k and stack[k - 1][1] != "html" and stack[k - 1][2] not in (_IP_HTML, _IP_MATH_TEXT):
            k -= 1
        pop_to(k, at)

    def html_end(name: str, at: int) -> None:
        nonlocal select_open
        if name == "select":
            select_open = False
        for k in range(len(stack) - 1, -1, -1):
            if stack[k][1] == "html" and stack[k][0] == name:
                pop_to(k, at)
                return
            if stack[k][2] != _IP_NONE:
                return  # a special (integration point) element stops the search

    while i < n:
        lt = text.find("<", i)
        if lt == -1:
            break
        i = lt
        if i + 1 >= n:
            break
        c = text[i + 1]
        if c == "!":
            if text.startswith("--", i + 2):
                i = _comment_end(text, i + 4)
            elif text[i + 2 : i + 9].lower() == "doctype":
                e = text.find(">", i + 9)
                i = n if e == -1 else e + 1
            elif text.startswith("[CDATA[", i + 2) and foreign_top():
                e = text.find("]]>", i + 9)
                i = n if e == -1 else e + 3
            else:
                e = text.find(">", i + 2)  # bogus comment
                i = n if e == -1 else e + 1
            continue
        if c == "?":
            e = text.find(">", i + 2)
            i = n if e == -1 else e + 1
            continue
        if c == "/":
            d = text[i + 2] if i + 2 < n else ""
            if not d:
                break
            if d == ">":
                i += 3  # </> is dropped
                continue
            if not (d.isascii() and d.isalpha()):
                e = text.find(">", i + 2)  # </ x> and friends: bogus comment
                i = n if e == -1 else e + 1
                continue
            tag = _read_tag(text, i + 2, view)
            if tag is None:
                break
            name, _attrs, _sc, end = tag
            if foreign_top():
                if name in ("br", "p"):
                    pop_foreign(i)
                    html_end(name, i)
                else:  # "in foreign content", any other end tag
                    k = len(stack) - 1
                    while True:
                        if stack[k][0] == name:
                            pop_to(k, i)
                            break
                        k -= 1
                        if k < 0:
                            break
                        if stack[k][1] == "html":
                            html_end(name, i)
                            break
            else:
                html_end(name, i)
            i = end
            continue
        if not (c.isascii() and c.isalpha()):
            i += 1  # a lone "<" is text
            continue
        tag = _read_tag(text, i + 1, view)
        if tag is None:
            break
        name, attrs, sc, end = tag
        start = i
        i = end
        while True:  # a breakout start tag is processed again after popping
            if not html_mode_for_start(name, attrs):
                if breakout(name, attrs):
                    pop_foreign(start)
                    continue
                top = stack[-1]
                ns = top[1]
                ip = _IP_NONE
                if ns == "svg" and name in _SVG_HTML_IP:
                    ip = _IP_HTML
                elif ns == "math" and name in _MATH_TEXT_IP:
                    ip = _IP_MATH_TEXT
                elif ns == "math" and name == "annotation-xml":
                    enc = next((v for a, v in attrs if a == "encoding"), None) or ""
                    html_enc = enc.strip().lower() in ("text/html", "application/xhtml+xml")
                    ip = _IP_HTML if html_enc else _IP_ANNOTATION
                view.starts.append(StartTag(name, ns, start, end, attrs))
                if not sc:
                    body = end if name in ("script", "style") else -1
                    stack.append([name, ns, ip, body])
                break
            # HTML insertion modes
            if legacy_select and select_open and name not in _SELECT_KEEP:
                if name in _SELECT_CLOSE:
                    select_open = False
                    for k in range(len(stack) - 1, -1, -1):
                        if stack[k][0] == "select" and stack[k][1] == "html":
                            pop_to(k, start)
                            break
                    if name == "select":
                        break
                else:
                    break  # ignored: no element, no state switch
            if name in ("svg", "math"):
                view.starts.append(StartTag(name, name, start, end, attrs))
                if not sc:
                    stack.append([name, name, _IP_NONE, -1])
                break
            if name == "image":
                name = "img"
            view.starts.append(StartTag(name, "html", start, end, attrs))
            if name == "select":
                select_open = True
            if name == "frameset":
                view.uncertain = True
            raw = name in _RCDATA or name in _RAWTEXT or (name == "noscript" and scripting)
            if raw or name == "script":
                close = (
                    _raw_end(text, i, "script") if name == "script" else _raw_close(text, i, name)
                )
                if name in ("script", "style"):
                    view.bodies.append((name, i, text[i:close]))
                i = close
                # the "text" insertion mode: the end tag pops this element and nothing else
                if close < n:
                    tag = _read_tag(text, close + 2, view)
                    i = n if tag is None else tag[3]
            elif name == "plaintext":
                i = n
            elif name not in _VOID:
                stack.append([name, "html", _IP_NONE, -1])
            break
    pop_to(0, n)
    return view


# (scripting, legacy_select). The scripting flag only changes <noscript>, the select flag
# only <select>: the first mode alone decides a page with neither, the first two cover
# both values of each flag, and a page with both elements needs every combination, each a
# real browser (gate of #676: scripting off + the 2025 <select> parser is today's DOMParser).
_MODES = ((True, False), (False, True), (False, False), (True, True))
_NOSCRIPT = re.compile(r"<noscript", re.I)
_SELECT = re.compile(r"<select", re.I)
_CACHE: dict = {}  # the source page's views, shared by attr_strictness and check_build


def browser_views(text: str) -> list[BrowserView]:
    """The page in every mode that can differ: the modes only differ on <noscript> and
    <select>, so a page without either is parsed once."""
    key = (len(text), hash(text))
    hit = _CACHE.get(key)
    if hit is not None and hit[0] == text:
        return hit[1]
    n_modes = (1 if _NOSCRIPT.search(text) else 0) + (1 if _SELECT.search(text) else 0)
    modes = _MODES[: (1, 2, 4)[n_modes]]
    views = [browser_view(text, scripting=sc, legacy_select=sel) for sc, sel in modes]
    if len(_CACHE) >= 4:
        _CACHE.clear()
    _CACHE[key] = (text, views)
    return views


def agreed_value_spans(text: str) -> set:
    """Value spans (start, end, quote) every browser mode reads as an attribute value of a
    tag; empty when the page holds a construct whose parse depends on the browser."""
    views = browser_views(text)
    if any(v.uncertain for v in views):
        return set()
    out = views[0].spans
    for v in views[1:]:
        out = out & v.spans
    return out


_STRICT_RE = re.compile(r"[\t\n\f\r /=`]")


def attr_value(value: str, strict: bool = False, quote_style: str = "html") -> str:
    """A translated attribute value, escaped for a quoted attribute. `strict` (a page where
    the filter's scan and a browser's may disagree) also writes every character an HTML5
    tag state reacts to as a character reference, so the value is one opaque run in every
    tokenizer state. `quote_style="xml"` writes `&apos;` (XML topics) instead of `&#x27;`."""
    v = str(value).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    v = v.replace('"', "&quot;").replace("'", "&apos;" if quote_style == "xml" else "&#x27;")
    if strict:
        v = _STRICT_RE.sub(lambda m: f"&#{ord(m.group())};", v)
    return v


def strict_escape(escaped: str) -> str:
    """Make an already-escaped attribute value opaque (see attr_value)."""
    return _STRICT_RE.sub(lambda m: f"&#{ord(m.group())};", escaped)


def _line(text: str, pos: int) -> int:
    return text.count("\n", 0, pos) + 1


# #733: page metadata a search engine or a link preview shows. A <meta> whose `name` is
# one of the first two, or whose `property` / `name` is one of the social keys, has its
# `content` offered for translation (style). Every other meta (robots, viewport,
# og:type / og:url / og:image, twitter:card, theme-color, generator, any http-equiv) is
# never touched.
META_NAME_STYLES = {"description": "meta:description", "keywords": "meta:keywords"}
META_SOCIAL_STYLES = {
    "og:title": "meta:og-title",
    "og:description": "meta:og-description",
    "twitter:title": "meta:twitter-title",
    "twitter:description": "meta:twitter-description",
}


def meta_text_style(attrs) -> str | None:
    """The unit style of a <meta> whose `content` is page text, else None. `attrs` is
    the tag's (name, value) pairs, first occurrence of a name winning as in a browser;
    values may still hold character references."""
    import html as _html

    first: dict = {}
    for a, v in attrs:
        first.setdefault(a.lower(), v)
    if "http-equiv" in first:
        return None

    def key(attr: str) -> str:
        return _html.unescape(first.get(attr) or "").strip().lower()

    name, prop = key("name"), key("property")
    return (
        META_NAME_STYLES.get(name) or META_SOCIAL_STYLES.get(prop) or META_SOCIAL_STYLES.get(name)
    )


def _free(tag_name: str, attr: str, attrs, extra: frozenset) -> bool:
    if attr in _FREE_ATTRS or (tag_name, attr) in extra:
        return True
    if tag_name == "meta" and attr == "content":
        equiv = next((v for a, v in attrs if a == "http-equiv"), None) or ""
        if equiv.strip().lower() == "content-language":
            return True
        return meta_text_style(attrs) is not None  # #733
    return False


def _signature(st: StartTag, extra: frozenset):
    return (
        st.ns,
        st.name,
        tuple(
            sorted((a, "\0free" if _free(st.name, a, st.attrs, extra) else v) for a, v in st.attrs)
        ),
    )


_REFUSAL = (
    "This page cannot be exported safely: in the translated file a web browser would read "
    "{what} at line {line} differently from the source page, so a translation could add "
    "markup or an attribute the source does not have. The source page has markup that "
    "browsers and the translation filter read differently (for example an abrupt comment "
    "<!-->, a <style> or <script> inside SVG, a stray </ x>, or an unclosed <textarea> or "
    "<xmp>). Correct that markup in the source file, upload it again and export again."
)


def _refuse(text: str, pos: int, what: str):
    raise UnsafeMarkupRefused(_REFUSAL.format(what=what, line=_line(text, pos)))


def check_build(
    before: str, after: str, *, extra: frozenset = frozenset(), xml: bool = True
) -> None:
    """Refuse (UnsafeMarkupRefused) unless `after`, the built page, gives a browser only
    elements and script/style bodies that `before`, the page the build started from, gives
    it too; translated values (_FREE_ATTRS and `extra` (tag, attribute) pairs) may differ.
    `xml`: also compare the two as XML when both are well-formed (XHTML, EPUB, Flare)."""
    if before == after:
        return
    sources = browser_views(before)
    for (scripting, legacy_select), vb in zip(_MODES[: len(sources)], sources, strict=True):
        va = browser_view(after, scripting=scripting, legacy_select=legacy_select)
        known = {_signature(st, extra) for st in vb.starts}
        for st in va.starts:
            if _signature(st, extra) not in known:
                _refuse(after, st.start, f"the <{st.name}> tag")
        bodies = {(name, body) for name, _pos, body in vb.bodies}
        for name, pos, body in va.bodies:
            if (name, body) not in bodies:
                _refuse(after, pos, f"the content of a <{name}> element")
    if xml:
        _check_xml(before, after, extra)


def _xml_view(text: str, extra: frozenset):
    """(element signatures, script/style bodies) of a well-formed XML document, else None."""
    if "<" not in text:
        return None
    from lxml import etree

    parser = etree.XMLParser(
        resolve_entities="internal",
        no_network=True,
        load_dtd=False,
        huge_tree=False,
        recover=False,
    )
    try:
        root = etree.fromstring(text.encode("utf-8"), parser)
    except (etree.XMLSyntaxError, ValueError):
        return None
    sigs, bodies = [], set()
    for el in root.iter():
        if not isinstance(el.tag, str):
            continue
        local = etree.QName(el).localname.lower()
        attrs = [(etree.QName(k).localname.lower(), v) for k, v in el.attrib.items()]
        names = list(el.attrib)
        sig = (
            el.tag,
            tuple(
                sorted(
                    (k, "\0free" if _free(local, a, attrs, extra) else v)
                    for k, (a, v) in zip(names, attrs, strict=True)
                )
            ),
        )
        sigs.append((sig, el.sourceline or 1))
        if local in ("script", "style"):
            bodies.add((local, "".join(el.itertext())))
    return sigs, bodies


def _check_xml(before: str, after: str, extra: frozenset) -> None:
    xb = _xml_view(before, extra)
    if xb is None:
        return  # not XML: nothing reads it as XML
    xa = _xml_view(after, extra)
    if xa is None:
        return  # an XML reader shows a parse error, never markup
    known = {sig for sig, _line in xb[0]}
    for sig, line in xa[0]:
        if sig not in known:
            raise UnsafeMarkupRefused(
                _REFUSAL.format(what="an element read as XML", line=line).replace(
                    "a web browser", "an XML reader"
                )
            )
    for name, body in xa[1]:
        if (name, body) not in xb[1]:
            raise UnsafeMarkupRefused(
                _REFUSAL.format(what=f"the content of a <{name}> element", line=1).replace(
                    "a web browser", "an XML reader"
                )
            )
