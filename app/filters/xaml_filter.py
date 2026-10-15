"""XAML filter: string resources in WPF / UWP / WinUI / .NET MAUI / Avalonia XAML files.

What is translated
  * Resource strings: every ``<sys:String>`` (``xmlns:sys="clr-namespace:System;..."`` or
    ``using:System``) and every XAML 2009 ``<x:String>`` element, usually inside a
    ``<ResourceDictionary>`` and keyed by ``x:Key``. The element's character content is one
    unit. A String whose content holds markup (a comment, CDATA, a child element) is skipped.
  * Optional, off by default (config.XAML_VIEW_ATTRIBUTES): the ``Content``, ``Text``,
    ``ToolTip``, ``Header`` and ``Title`` attributes of plain XAML views. They are appended
    AFTER every resource string, so flipping the option between upload and export never
    shifts a resource string's unit index.

Never translated: markup extensions (``{Binding ...}``, ``{StaticResource ...}``,
``{x:Static ...}``, ``{DynamicResource ...}``) in content or attributes; values with no
letters or digits; anything else in the file. An attribute escaped with the XAML ``{}``
prefix is translated after the prefix, and the prefix is kept.

Fidelity: the file is checked with lxml (hardened: no DTD, no entities, no network, no huge
tree), then a small lexer over the decoded text finds the exact character spans of the
units. build() splices only those spans, so every other byte (namespaces, attribute order
and quoting, comments, indentation, line endings, BOM) is identical on round trip. Parse
and build share that one walk, so unit indices always line up.

Encodings: UTF-8 (with or without BOM), UTF-16 with a BOM, or a common legacy charset the
XML declaration names. The rebuilt file is written in the SAME encoding (characters the
legacy charset cannot hold become XML character references, legal in content and
attribute values).

Spec references (Microsoft Learn): XAML resources / ResourceDictionary and x:Key, the XAML
2009 x:String intrinsic type, the {} escape sequence and markup extensions.

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
from app.filters.xml_text import xml_target_text

_XAML_NS = (
    "http://schemas.microsoft.com/winfx/2006/xaml",
    "http://schemas.microsoft.com/winfx/2009/xaml",
)
VIEW_ATTRIBUTES = ("Content", "Text", "ToolTip", "Header", "Title")
_XMLNS_XML = "http://www.w3.org/XML/1998/namespace"

# A markup extension: `{Name ...}` or `{prefix:Name ...}`, Name starting with a letter.
# `{0}` and `{}`-escaped text are NOT markup extensions.
_MARKUP_EXT = re.compile(r"\A\s*\{\s*[A-Za-z_][\w.]*(?::[A-Za-z_][\w.]*)?(?:[\s,}]|\Z)")
_NAME = r"[^\s=/<>!?\"']+"
_START_TAG = re.compile(
    r"<(" + _NAME + r")((?:\s+" + _NAME + r"\s*=\s*(?:\"[^\"]*\"|'[^']*'))*)\s*(/?)>"
)
_ATTR = re.compile(r"(" + _NAME + r")\s*=\s*(?:\"([^\"]*)\"|'([^']*)')")
_END_TAG = re.compile(r"</(" + _NAME + r")\s*>")
_ENTITY = re.compile(r"&(#x[0-9A-Fa-f]+|#[0-9]+|lt|gt|amp|quot|apos);")
_PREDEF = {"lt": "<", "gt": ">", "amp": "&", "quot": '"', "apos": "'"}
_ENCODING_DECL = re.compile(r"""\A<\?xml[^>]*?\bencoding\s*=\s*["']([A-Za-z0-9._:-]+)["']""")


_DTD_MSG = "This XAML file declares a DOCTYPE / DTD; refused (XAML never needs one)."


class XamlParseError(ValueError):
    """A .xaml upload that is not well-formed XML, carries a DTD, or uses an encoding we do
    not read. Caught by the upload routes as a polite parse refusal."""


def check_xaml_budget(raw: bytes) -> None:
    """Pre-parse budget (#571 pattern): refuse before lxml builds a tree. Every element,
    comment and attribute-bearing tag needs a '<'; forty per allowed segment is far above
    any real resource dictionary or view."""
    if raw.count(b"<") > 40 * config.MAX_DOCUMENT_SEGMENTS:
        raise InputLimitError(
            f"This XAML file has far more elements than MAX_DOCUMENT_SEGMENTS allows "
            f"({config.MAX_DOCUMENT_SEGMENTS}). Split it into smaller files."
        )


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
            raise XamlParseError(
                f"This XAML file declares the encoding {m.group(1)!r}, which is not supported; "
                "re-save the file as UTF-8."
            )
    try:
        return raw[len(bom) :].decode(codec), codec, bom
    except UnicodeDecodeError as exc:
        raise XamlParseError(
            f"This XAML file is not valid {codec} (byte at position {exc.start + len(bom)}); "
            "re-save the file as UTF-8."
        ) from exc


def _check_xml(raw: bytes) -> None:
    """Hardened well-formedness gate (same parser posture as xliff_filter/vendor_xliff)."""
    if re.search(rb"<!DOCTYPE", raw[:4096] or b"", re.IGNORECASE):
        raise XamlParseError(
            "This XAML file declares a DOCTYPE / DTD; refused (XAML never needs one)."
        )
    parser = etree.XMLParser(
        resolve_entities=False,
        no_network=True,
        load_dtd=False,
        dtd_validation=False,
        huge_tree=False,
    )
    try:
        root = etree.fromstring(raw, parser=parser)
    except etree.XMLSyntaxError as exc:
        raise XamlParseError(f"This XAML file is not well-formed XML: {exc}") from exc
    info = root.getroottree().docinfo
    if info.doctype or info.internalDTD is not None or info.externalDTD is not None:
        raise XamlParseError(
            "This XAML file declares a DOCTYPE / DTD; refused (XAML never needs one)."
        )


def _unescape(s: str) -> str:
    def rep(m):
        g = m.group(1)
        if g[0] == "#":
            return chr(int(g[2:], 16) if g[1] in "xX" else int(g[1:]))
        return _PREDEF[g]

    return _ENTITY.sub(rep, s)


def _is_string_ns(uri: str | None) -> bool:
    if not uri:
        return False
    if uri in _XAML_NS:
        return True
    u = uri.replace(" ", "")
    return u in ("clr-namespace:System", "using:System") or u.startswith("clr-namespace:System;")


def _translatable(value: str) -> bool:
    return any(c.isalnum() for c in value) and not _MARKUP_EXT.match(value)


class _Span:
    __slots__ = ("crlf", "end", "escaped", "kind", "quote", "start", "text")

    def __init__(self, start, end, text, kind, quote="", crlf=False, escaped=False):
        self.start, self.end, self.text, self.kind = start, end, text, kind
        self.quote, self.crlf, self.escaped = quote, crlf, escaped


def _walk(text: str, with_attrs: bool) -> list[_Span]:
    """One pass over well-formed XAML: resource-string spans first, then (optionally) view
    attribute spans, each group in document order."""
    resources: list[_Span] = []
    attrs: list[_Span] = []
    stack: list[dict] = []  # {name, ns, is_string, content_start, mixed}
    pos, n = 0, len(text)
    while True:
        lt = text.find("<", pos)
        if lt < 0:
            break
        if text.startswith("<!--", lt):
            end = text.find("-->", lt + 4)
            nxt = end + 3
        elif text.startswith("<![CDATA[", lt):
            end = text.find("]]>", lt + 9)
            nxt = end + 3
        elif text.startswith("<?", lt):
            end = text.find("?>", lt + 2)
            nxt = end + 2
        elif text.startswith("</", lt):
            m = _END_TAG.match(text, lt)
            if not m or not stack:
                raise XamlParseError("This XAML file could not be read (unbalanced end tag).")
            top = stack.pop()
            if top["is_string"] and not top["mixed"]:
                raw = text[top["content_start"] : lt]
                core = raw.strip(" \t\r\n")
                value = _unescape(core.replace("\r\n", "\n").replace("\r", "\n"))
                if _translatable(value):
                    lead = len(raw) - len(raw.lstrip(" \t\r\n"))
                    s = top["content_start"] + lead
                    resources.append(_Span(s, s + len(core), value, "text", crlf="\r\n" in raw))
                    check_segment_cap(len(resources) + len(attrs))
            pos = m.end()
            continue
        elif text.startswith("<!", lt):
            raise XamlParseError(
                "This XAML file declares a DOCTYPE / DTD; refused (XAML never needs one)."
            )
        else:
            m = _START_TAG.match(text, lt)
            if not m:
                raise XamlParseError("This XAML file could not be read (malformed start tag).")
            if stack:
                stack[-1]["mixed"] = True
            parent_ns = stack[-1]["ns"] if stack else {}
            ns = dict(parent_ns)
            raw_attrs = []
            for am in _ATTR.finditer(m.group(2)):
                aname = am.group(1)
                gi = 2 if am.group(2) is not None else 3
                vstart = m.start(2) + am.start(gi)
                vraw = am.group(gi)
                raw_attrs.append((aname, vstart, vraw, '"' if gi == 2 else "'"))
                if aname == "xmlns":
                    ns[None] = _unescape(vraw)
                elif aname.startswith("xmlns:"):
                    ns[aname[6:]] = _unescape(vraw)
            qname = m.group(1)
            prefix, _, local = qname.rpartition(":")
            uri = ns.get(prefix or None)
            is_string = local == "String" and _is_string_ns(uri)
            if with_attrs and "." not in local:
                for aname, vstart, vraw, quote in raw_attrs:
                    if aname not in VIEW_ATTRIBUTES:
                        continue
                    norm = re.sub(r"[\t\r\n]", " ", vraw.replace("\r\n", " "))
                    value = _unescape(norm)
                    off = 0
                    if value.startswith("{}"):
                        off, value = 2, value[2:]
                    elif _MARKUP_EXT.match(value) or value.lstrip().startswith("{"):
                        continue  # a markup extension (or an unescaped brace: never text)
                    if not any(c.isalpha() for c in value):  # Content="42" is a value
                        continue
                    attrs.append(
                        _Span(
                            vstart + off,
                            vstart + len(vraw),
                            value,
                            "attr",
                            quote=quote,
                            escaped=bool(off),
                        )
                    )
                    check_segment_cap(len(resources) + len(attrs))
            if m.group(3) != "/":
                stack.append(
                    {"ns": ns, "is_string": is_string, "content_start": m.end(), "mixed": False}
                )
            pos = m.end()
            continue
        if end < 0:
            raise XamlParseError("This XAML file could not be read (unterminated markup).")
        if stack:
            stack[-1]["mixed"] = True
        pos = nxt
    if pos > n:
        raise XamlParseError("This XAML file could not be read.")
    return resources + attrs


def _escape_text(s: str, crlf: bool) -> str:
    s = s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    return s.replace("\n", "\r\n") if crlf else s


def _escape_attr(s: str, quote: str) -> str:
    s = s.replace("&", "&amp;").replace("<", "&lt;")
    s = s.replace('"', "&quot;") if quote == '"' else s.replace("'", "&apos;")
    return s.replace("\t", "&#9;").replace("\n", "&#10;").replace("\r", "&#13;")


def _view_attrs_enabled() -> bool:
    return bool(getattr(config, "XAML_VIEW_ATTRIBUTES", False))


def _load(raw: bytes, capped: bool):
    raw = bytes(raw)
    if capped:
        check_xaml_budget(raw)
    text, codec, bom = _decode(raw)
    _check_xml(raw)
    return text, codec, bom


@register
class XamlFilter(Filter):
    fmt = "xaml"

    def parse(self, data: bytes, provider: str | None = None) -> ParseResult:
        text, codec, _bom = _load(data, capped=True)
        with_attrs = _view_attrs_enabled()
        spans = _walk(text, with_attrs)
        texts = [s.text for s in spans]
        check_unit_texts(texts)
        if not texts:
            hint = (
                ""
                if with_attrs
                else " View attributes (Content, Text, ToolTip, Header, Title) are only "
                "translated when XAML_VIEW_ATTRIBUTES is on."
            )
            raise EmptyDocumentError(
                "This XAML file has no sys:String or x:String resources to translate." + hint
            )
        units = [TextUnit(p_index=i, text=t) for i, t in enumerate(texts)]
        skeleton = {
            "format": "xaml",
            "units": [{"p_index": u.p_index, "text": u.text, "style": ""} for u in units],
        }
        if codec not in ("utf-8",):
            note_source_encoding(skeleton, codec)
        return ParseResult(skeleton=skeleton, units=units)

    def build(self, original: bytes, paragraph_texts: dict) -> bytes:
        text, codec, bom = _load(original, capped=False)
        spans = _walk(text, _view_attrs_enabled())
        out, last, changed = [], 0, False
        # Unit order is resources-then-attributes; splice in file order.
        for i, sp in sorted(enumerate(spans), key=lambda p: p[1].start):
            new = paragraph_texts.get(str(i))
            if new is None or new == sp.text:
                continue
            new = xml_target_text(new)
            out.append(text[last : sp.start])
            if sp.kind == "text":
                out.append(_escape_text(new, sp.crlf))
            else:
                if not sp.escaped and new.lstrip().startswith("{"):
                    new = "{}" + new  # never let a translation turn into a markup extension
                out.append(_escape_attr(new, sp.quote))
            last = sp.end
            changed = True
        if not changed:
            return bytes(original)
        out.append(text[last:])
        return bom + "".join(out).encode(codec, "xmlcharrefreplace")
