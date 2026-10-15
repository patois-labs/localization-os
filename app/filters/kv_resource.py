"""Shared base for key-value software resource filters (file-format Batch 1).

Java .properties, Apple .strings, INI (generic + Joomla) and Windows .resjson all have
the same shape: a flat list of KEY -> VALUE entries with comments around them. Each
format module supplies three things:

  _decode(data)      -> (text, ResourceEncoding)   how the bytes are read
  _scan(text)        -> list[Entry]                every entry, in file order, with the
                                                   SPAN of its raw value in `text`
  _encode_value(...) -> str                        a translated value written back in the
                                                   format's own escaping

parse() and build() run the SAME decode and the SAME scan, so unit indices always line
up (the alignment rule every filter follows). build() splices the encoded translations
into the exact spans and leaves every other character alone: comments, keys, separators,
blank lines, line endings, the trailing newline and the byte-order mark survive byte for
byte, and an untranslated (or unchanged) value keeps its original raw spelling.

Keys are never translated. A value with no letters or digits (`%@`, `{0}`, `---`) is
skipped but still walked, exactly like `json_filter._is_translatable`.

records() is the key-based view (Batch 9 wires it into `keys import/export` and the git
connector): one KeyValueRecord per entry, translatable or not, with its key, value,
translator comment, section/context, plural forms and max length when the format has them.

Documented in: docs/architecture/translation-pipeline.md
"""

from __future__ import annotations

import bisect
import re
from dataclasses import dataclass, field

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
    note_source_encoding,
)


@dataclass
class KeyValueRecord:
    """One resource entry as a key-based project sees it (Batch 9 consumes this)."""

    key: str
    value: str
    comment: str = ""
    context: str = ""  # INI section, nested JSON path; "" when the format has none
    plural_forms: dict | None = None  # category -> text; None for a plain string
    max_length: int | None = None
    plural_units: dict | None = None  # category -> unit index of that form (Batch 9 export)
    unit_index: int | None = None  # p_index of the document unit; None = not translatable
    duplicate: bool = False  # the same key (in the same context) appeared earlier


@dataclass
class Entry:
    """A scanned entry. `start`/`end` delimit the RAW value in the decoded text."""

    key: str
    value: str  # decoded value (escapes resolved)
    start: int
    end: int
    comment: str = ""
    context: str = ""
    translatable: bool = True  # False for format metadata (RESJSON `_key.comment`)
    extra: dict = field(default_factory=dict)  # format-private build hints


@dataclass
class ResourceEncoding:
    codec: str  # python codec used to decode ("utf-8", "utf-16-le", "latin-1", ...)
    bom: bytes = b""  # the byte-order mark the file started with (written back)
    out_codec: str = ""  # codec to write with; defaults to `codec`
    label: str = "utf-8"  # what note_source_encoding records
    mode: str = ""  # format-private output policy (.properties: ascii / latin1 / utf-8)


_PLACEHOLDER_ONLY = re.compile(r"\{[^{}]*\}|%(?:\d+\$)?[-#+0-9.]*(?:hh|ll|[hlqLzjt])?[a-zA-Z@]")


def is_translatable(value: str) -> bool:
    """A value with a letter or digit outside its placeholders. `{0}`, `%1$@` and
    `---` are skipped (but still walked), like json_filter._is_translatable."""
    if not any(c.isalnum() for c in value):
        return False
    return any(c.isalnum() for c in _PLACEHOLDER_ONLY.sub("", value))


_BOMS = (
    (b"\xef\xbb\xbf", "utf-8"),
    (b"\xff\xfe", "utf-16-le"),
    (b"\xfe\xff", "utf-16-be"),
)


def sniff_bom(data: bytes) -> tuple[bytes, str] | None:
    # UTF-32 BOMs start with a UTF-16 one; nobody ships UTF-32 resources, refuse it.
    if data[:4] in (b"\xff\xfe\x00\x00", b"\x00\x00\xfe\xff"):
        raise UnsupportedEncodingError("UTF-32 files are not supported; re-save the file as UTF-8.")
    for bom, codec in _BOMS:
        if data.startswith(bom):
            return bom, codec
    return None


def decode_with(data: bytes, enc: ResourceEncoding) -> str:
    body = bytes(data)[len(enc.bom) :]
    try:
        return body.decode(enc.codec)
    except UnicodeDecodeError as exc:
        raise UnsupportedEncodingError(
            f"file is not valid {enc.codec.upper()} (byte 0x{body[exc.start]:02x} at position "
            f"{exc.start + len(enc.bom)}); re-save the file as UTF-8."
        ) from exc


def refuse_binary(data: bytes) -> None:
    if _looks_binary(data):
        raise UnsupportedEncodingError(
            "file is not valid UTF-8 and looks like a binary file, not text; "
            "re-save the file as UTF-8."
        )


class _SpanMap:
    """Maps an index in a LOGICAL string (built from several raw pieces) back to the raw
    text. Used by the .properties continuation-line walk."""

    def __init__(self, pieces: list[tuple[int, int]]):
        self.pieces = pieces
        self.offsets: list[int] = []
        n = 0
        for s, e in pieces:
            self.offsets.append(n)
            n += e - s
        self.length = n

    def raw(self, i: int) -> int:
        if i >= self.length:
            return self.pieces[-1][1]
        k = bisect.bisect_right(self.offsets, i) - 1
        return self.pieces[k][0] + (i - self.offsets[k])


class KeyValueFilter(Filter):
    """Base class; subclasses are @register-ed by their own modules."""

    fmt = "kv"

    # --- format hooks -----------------------------------------------------------------
    def _decode(self, data: bytes) -> tuple[str, ResourceEncoding]:
        raise NotImplementedError

    def _scan(self, text: str) -> list[Entry]:
        raise NotImplementedError

    def _encode_value(self, new: str, entry: Entry, text: str, enc: ResourceEncoding) -> str:
        raise NotImplementedError

    def _unit_text(self, entry: Entry) -> str:
        """The value as the translator sees it (a format may undo a layer of escaping
        that only the runtime needs, e.g. MessageFormat apostrophes)."""
        return entry.value

    def _budget(self, text: str) -> None:
        """Pre-scan budget: the shared line budget. Formats add their own counts."""
        check_line_budget(text)

    # --- shared walk ------------------------------------------------------------------
    def _read(self, data: bytes) -> tuple[str, ResourceEncoding, list[Entry]]:
        text, enc = self._decode(bytes(data))
        self._budget(text)
        entries = self._scan(text)
        return text, enc, entries

    @staticmethod
    def _split_ws(s: str) -> tuple[str, str, str]:
        core = s.strip()
        if not core:
            return s, "", ""
        lead = s[: len(s) - len(s.lstrip())]
        trail = s[len(s.rstrip()) :]
        return lead, core, trail

    def parse(self, data: bytes, provider: str | None = None) -> ParseResult:
        _text, enc, entries = self._read(data)
        units: list[TextUnit] = []
        for e in entries:
            if not (e.translatable and is_translatable(e.value)):
                continue
            _lead, core, _trail = self._split_ws(self._unit_text(e))
            if len(core) > config.MAX_SEGMENT_CHARS:
                raise InputLimitError(
                    f"This file has a value longer than {config.MAX_SEGMENT_CHARS} "
                    "characters (MAX_SEGMENT_CHARS). Break it into shorter values."
                )
            units.append(TextUnit(p_index=len(units), text=core))
            if len(units) > config.MAX_DOCUMENT_SEGMENTS:
                check_segment_cap(len(units))  # #571: stop before building the rest
        skeleton = {
            "format": self.fmt,
            "units": [{"p_index": u.p_index, "text": u.text, "style": ""} for u in units],
        }
        return ParseResult(skeleton=note_source_encoding(skeleton, enc.label), units=units)

    def build(self, original: bytes, paragraph_texts: dict) -> bytes:
        text, enc, entries = self._read(original)
        out: list[str] = []
        pos = 0
        k = 0
        for e in entries:
            if not (e.translatable and is_translatable(e.value)):
                continue
            idx = k
            k += 1
            new = paragraph_texts.get(str(idx))
            if new is None:
                continue
            lead, core, trail = self._split_ws(self._unit_text(e))
            if new == core:
                continue  # unchanged: keep the original raw spelling byte for byte
            out.append(text[pos : e.start])
            out.append(self._encode_value(lead + new + trail, e, text, enc))
            pos = e.end
        out.append(text[pos:])
        return self._encode_output("".join(out), enc)

    def _encode_output(self, text: str, enc: ResourceEncoding) -> bytes:
        if enc.out_codec and enc.out_codec != enc.codec:
            # B-bytes: a converted file (Windows-1252, #509) goes back in its own code page
            # when every character of the output fits; otherwise `out_codec` (UTF-8).
            try:
                return enc.bom + text.encode(enc.codec)
            except UnicodeEncodeError:
                pass
        return enc.bom + text.encode(enc.out_codec or enc.codec)

    def records(self, data: bytes) -> list[KeyValueRecord]:
        """Every entry as a KeyValueRecord, in file order (translatable or not)."""
        _text, _enc, entries = self._read(data)
        out: list[KeyValueRecord] = []
        seen: set[tuple[str, str]] = set()
        k = 0
        for e in entries:
            idx = None
            if e.translatable and is_translatable(e.value):
                idx = k
                k += 1
            ident = (e.context, e.key)
            out.append(
                KeyValueRecord(
                    key=e.key,
                    value=e.value,
                    comment=e.comment,
                    context=e.context,
                    max_length=e.extra.get("max_length"),
                    unit_index=idx,
                    duplicate=ident in seen,
                )
            )
            seen.add(ident)
        return out
