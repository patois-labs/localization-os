"""Mozilla DTD entity file (.dtd) filter.

A Mozilla localization DTD is a list of general entity declarations used by XUL/XHTML:

    <!-- LOCALIZATION NOTE (window.title): shown in the title bar. -->
    <!ENTITY window.title "&brandShortName; Settings">

Grammar: XML 1.0 (Fifth Edition) section 4.2, https://www.w3.org/TR/xml/#sec-entity-decl
(EntityDecl, GEDecl, PEDecl, EntityValue), section 4.1 (CharRef, EntityRef, PEReference)
and section 2.5 (Comments). Mozilla conventions (UTF-8, LOCALIZATION NOTE comments,
`&#037;` for a literal percent): https://firefox-source-docs.mozilla.org/l10n/ and the
compare-locales DTD checks, https://github.com/mozilla/compare-locales .

What is translated: the EntityValue of each GENERAL internal entity. Parameter entities
(`<!ENTITY % name ...>`), external entities (SYSTEM / PUBLIC), PE references (`%x;`),
processing instructions and any other markup declaration are kept byte for byte and
never yield a unit. Entity names (keys) are never translated.

What the translator sees: the value with the predefined entities `&amp; &quot; &apos;`
and character references resolved to plain characters; every other reference
(`&brandShortName;`, `&lt;`, `&gt;`) is shown as written and protected like a tag.
Build escapes `&` as `&amp;`, the value's own quote character as `&quot;`/`&apos;` and
`%` as `&#037;` (an unescaped `%` would start a parameter-entity reference), keeps the
original quote character, and puts every other byte back untouched. An unchanged value
keeps its original spelling.

This is a hand-written scanner, NOT an XML/DTD parser: nothing is expanded, resolved or
fetched (no XXE, no billion laughs). UTF-8 only (with or without BOM), as Mozilla ships.
"""

from __future__ import annotations

import re

from app import config
from app.filters.base import (
    Filter,
    InputLimitError,
    ParseResult,
    TextUnit,
    UnsupportedEncodingError,
    _looks_binary,
    check_line_budget,
    check_segment_cap,
    register,
)

_BOM = b"\xef\xbb\xbf"
_NAME = r"[A-Za-z_:][-A-Za-z0-9._:·-￿]*"
_ENTITY_HEAD = re.compile(r"<!ENTITY(\s+)(%\s+)?(" + _NAME + r")(\s+)")
_REF = re.compile(r"&(#[0-9]+|#x[0-9A-Fa-f]+|" + _NAME + r");")
_PREDEF = {"amp": "&", "quot": '"', "apos": "'"}
_PROTECTED_REF = re.compile(r"&" + _NAME + r";")
_TAG = re.compile(r"</?[A-Za-z][^<>]*>")


class DtdSyntaxError(ValueError):
    """The DTD could not be read; the message is safe to show the uploader."""


class _Entity:
    __slots__ = ("comment", "end", "name", "quote", "raw", "start")

    def __init__(self, name, raw, start, end, quote, comment):
        self.name = name
        self.raw = raw  # raw EntityValue between the quotes
        self.start = start  # span of the raw value in the decoded text
        self.end = end
        self.quote = quote
        self.comment = comment


def _decode(data: bytes) -> tuple[str, bytes]:
    data = bytes(data)
    bom = _BOM if data.startswith(_BOM) else b""
    body = data[len(bom) :]
    try:
        text = body.decode("utf-8")
    except UnicodeDecodeError as exc:
        if _looks_binary(body):
            raise UnsupportedEncodingError(
                "file is not valid UTF-8 and looks like a binary file, not text; "
                "re-save the file as UTF-8."
            ) from exc
        raise UnsupportedEncodingError(
            f"Mozilla .dtd files are UTF-8; byte 0x{body[exc.start]:02x} at position "
            f"{exc.start + len(bom)} is not. Re-save the file as UTF-8."
        ) from exc
    if "\x00" in text:
        raise UnsupportedEncodingError(
            "file is not valid UTF-8 text (it holds NUL bytes); re-save the file as UTF-8."
        )
    return text, bom


def check_dtd_budget(text: str) -> None:
    check_line_budget(text)
    if text.count("<!") > 4 * config.MAX_DOCUMENT_SEGMENTS:
        raise InputLimitError(
            f"This DTD file has far more declarations than MAX_DOCUMENT_SEGMENTS allows "
            f"({config.MAX_DOCUMENT_SEGMENTS}). Split it into smaller files."
        )


def _line_of(text: str, pos: int) -> int:
    return text.count("\n", 0, pos) + 1


def _skip_decl(text: str, pos: int) -> int:
    """End (exclusive) of a markup declaration starting at `pos`, quote-aware."""
    i, n = pos + 2, len(text)
    quote = ""
    while i < n:
        c = text[i]
        if quote:
            if c == quote:
                quote = ""
        elif c in "\"'":
            quote = c
        elif c == ">":
            return i + 1
        i += 1
    raise DtdSyntaxError(
        f"This .dtd file has an unterminated declaration (line {_line_of(text, pos)})."
    )


def _scan(text: str) -> list[_Entity]:
    out: list[_Entity] = []
    comment = ""
    i, n = 0, len(text)
    while i < n:
        c = text[i]
        if c in " \t\r\n":
            if c == "\n" and text.startswith("\n", i + 1):
                comment = ""  # a blank line detaches a note from the next entity
            i += 1
            continue
        if text.startswith("<!--", i):
            j = text.find("-->", i + 4)
            if j < 0:
                raise DtdSyntaxError(
                    f"This .dtd file has an unterminated comment (line {_line_of(text, i)})."
                )
            comment = text[i + 4 : j].strip()
            i = j + 3
            continue
        if text.startswith("<!ENTITY", i):
            m = _ENTITY_HEAD.match(text, i)
            if not m:
                raise DtdSyntaxError(
                    f"This .dtd file has a malformed ENTITY declaration (line {_line_of(text, i)})."
                )
            j = m.end()
            q = text[j] if j < n else ""
            if m.group(2) or q not in "\"'" or not q:
                # parameter entity, or an external entity (SYSTEM / PUBLIC): kept as is
                i = _skip_decl(text, i)
                comment = ""
                continue
            k = text.find(q, j + 1)
            if k < 0:
                raise DtdSyntaxError(
                    f"This .dtd file has an unterminated entity value (line {_line_of(text, i)})."
                )
            close = k + 1
            while close < n and text[close] in " \t\r\n":
                close += 1
            if close >= n or text[close] != ">":
                raise DtdSyntaxError(
                    f"This .dtd file has a malformed ENTITY declaration (line {_line_of(text, i)})."
                )
            out.append(_Entity(m.group(3), text[j + 1 : k], j + 1, k, q, comment))
            comment = ""
            i = close + 1
            continue
        if text.startswith("<!", i) or text.startswith("<?", i):
            i = _skip_decl(text, i) if text.startswith("<!", i) else _skip_pi(text, i)
            comment = ""
            continue
        if c == "%":
            m = re.compile(r"%" + _NAME + r";").match(text, i)
            if m:
                i = m.end()
                comment = ""
                continue
        raise DtdSyntaxError(
            f"This .dtd file has unexpected text outside a declaration (line {_line_of(text, i)})."
        )
    return out


def _skip_pi(text: str, pos: int) -> int:
    j = text.find("?>", pos + 2)
    if j < 0:
        raise DtdSyntaxError(
            f"This .dtd file has an unterminated processing instruction "
            f"(line {_line_of(text, pos)})."
        )
    return j + 2


def _char(ref: str) -> str | None:
    try:
        cp = int(ref[2:], 16) if ref.startswith("#x") else int(ref[1:])
    except ValueError:
        return None
    if cp in (0x9, 0xA, 0xD) or 0x20 <= cp <= 0xD7FF or 0xE000 <= cp <= 0xFFFD:
        return chr(cp)
    if 0x10000 <= cp <= 0x10FFFF:
        return chr(cp)
    return None


def unit_text(raw: str) -> str:
    """Raw EntityValue -> translator text (predefined entities and char refs resolved)."""

    def sub(m: re.Match) -> str:
        ref = m.group(1)
        if ref.startswith("#"):
            ch = _char(ref)
            return ch if ch is not None else m.group(0)
        return _PREDEF.get(ref, m.group(0))

    return _REF.sub(sub, raw)


def encode_value(new: str, quote: str) -> str:
    """Translator text -> raw EntityValue. Entity references (`&brandShortName;`,
    `&lt;`) the translation carries are kept as references; every other `&` is
    written `&amp;`."""
    out: list[str] = []
    pos = 0
    for m in _PROTECTED_REF.finditer(new):
        out.append(_escape(new[pos : m.start()], quote))
        name = m.group(0)[1:-1]
        out.append("&amp;" if name == "amp" else m.group(0))
        pos = m.end()
    out.append(_escape(new[pos:], quote))
    return "".join(out)


def _escape(s: str, quote: str) -> str:
    s = s.replace("&", "&amp;").replace("%", "&#037;")
    return s.replace(quote, "&quot;" if quote == '"' else "&apos;")


def _translatable(value: str) -> bool:
    core = _TAG.sub("", _PROTECTED_REF.sub("", value))
    return any(c.isalnum() for c in core)


def _split_ws(s: str) -> tuple[str, str, str]:
    core = s.strip()
    if not core:
        return s, "", ""
    lead = s[: len(s) - len(s.lstrip())]
    trail = s[len(s.rstrip()) :]
    return lead, core, trail


@register
class DtdFilter(Filter):
    fmt = "dtd"

    def _read(self, data: bytes) -> tuple[str, bytes, list[_Entity]]:
        text, bom = _decode(data)
        check_dtd_budget(text)
        return text, bom, _scan(text)

    @staticmethod
    def _units(entities: list[_Entity]):
        k = 0
        for e in entities:
            value = unit_text(e.raw)
            if not _translatable(value):
                continue
            yield k, e, value
            k += 1

    def parse(self, data: bytes, provider: str | None = None) -> ParseResult:
        _text, _bom, entities = self._read(data)
        units: list[TextUnit] = []
        for idx, e, value in self._units(entities):
            _lead, core, _trail = _split_ws(value)
            if len(core) > config.MAX_SEGMENT_CHARS:
                raise InputLimitError(
                    f"This file has an entity value longer than {config.MAX_SEGMENT_CHARS} "
                    "characters (MAX_SEGMENT_CHARS). Break it into shorter values."
                )
            units.append(TextUnit(p_index=idx, text=core, style=f"dtd:{e.name}"))
            if len(units) > config.MAX_DOCUMENT_SEGMENTS:
                check_segment_cap(len(units))
        skeleton = {
            "format": self.fmt,
            "units": [{"p_index": u.p_index, "text": u.text, "style": u.style} for u in units],
        }
        return ParseResult(skeleton=skeleton, units=units)

    def build(self, original: bytes, paragraph_texts: dict) -> bytes:
        text, bom, entities = self._read(original)
        out: list[str] = []
        pos = 0
        for idx, e, value in self._units(entities):
            new = paragraph_texts.get(str(idx))
            if new is None:
                continue
            lead, core, trail = _split_ws(value)
            if new == core:
                continue
            out.append(text[pos : e.start])
            out.append(encode_value(lead + new + trail, e.quote))
            pos = e.end
        out.append(text[pos:])
        return bom + "".join(out).encode("utf-8")
