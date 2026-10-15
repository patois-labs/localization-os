"""Apple .strings filter (iOS / macOS `Localizable.strings`).

Format (Apple "String Resources", Resource Programming Guide:
https://developer.apple.com/library/archive/documentation/Cocoa/Conceptual/LoadingResources/Strings/Strings.html):
  * entries are `"key" = "value";`, the old-style property-list dictionary syntax; the
    whole file may also be wrapped in `{ ... }`;
  * comments are C style: `/* ... */` (usually the translator note above the entry)
    and `// ...` to the end of the line (the old-style plist parser accepts both);
  * inside a quoted string, `\\"` `\\\\` `\\n` `\\r` `\\t` are escapes and `\\Uxxxx` is a
    UTF-16 code unit; the plist parser also reads `\\a \\b \\f \\v`, `\\'` and up to three
    octal digits. An unquoted token (letters, digits, `_$+/:.-`) is also a string;
  * placeholders are printf style with `%@` for objects and positional `%1$@`;
  * keys are never shown to users and are never translated, even when they repeat the
    English value.

Encoding: Apple recommends UTF-8 (Xcode converts to UTF-16 in the built product) and
older files, and `genstrings` output, are UTF-16 with a byte-order mark. The file is
read as UTF-8 (with or without a BOM), UTF-16 with a BOM, or BOM-less UTF-16 (sniffed
from the NUL byte pattern), and rebuilt in exactly that encoding with the same BOM.
Anything else is refused: Xcode would not read it.

Documented in: docs/architecture/translation-pipeline.md
"""

from __future__ import annotations

from app.filters.base import InputLimitError, UnsupportedEncodingError, register
from app.filters.kv_resource import (
    Entry,
    KeyValueFilter,
    ResourceEncoding,
    decode_with,
    sniff_bom,
)

_UNQUOTED = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_$+/:.-")
_ESC = {
    "a": "\a",
    "b": "\b",
    "f": "\f",
    "n": "\n",
    "r": "\r",
    "t": "\t",
    "v": "\v",
    '"': '"',
    "'": "'",
    "\\": "\\",
}
_HEX = set("0123456789abcdefABCDEF")


def unescape(s: str) -> str:
    if "\\" not in s:
        return s
    out: list[str] = []
    i, n = 0, len(s)
    units: list[int] = []  # pending \U code units (a surrogate pair spans two escapes)

    def flush_units():
        if units:
            b = b"".join(u.to_bytes(2, "big") for u in units)
            out.append(b.decode("utf-16-be", "surrogatepass"))
            units.clear()

    while i < n:
        c = s[i]
        if c != "\\" or i + 1 >= n:
            flush_units()
            out.append(c)
            i += 1
            continue
        d = s[i + 1]
        if d in "Uu":
            j = i + 2
            while j < n and j < i + 6 and s[j] in _HEX:
                j += 1
            if j > i + 2:
                units.append(int(s[i + 2 : j], 16))
                i = j
                continue
        flush_units()
        if d in "01234567":
            j = i + 1
            while j < n and j < i + 4 and s[j] in "01234567":
                j += 1
            out.append(chr(int(s[i + 1 : j], 8)))
            i = j
            continue
        out.append(_ESC.get(d, d))
        i += 2
    flush_units()
    return "".join(out)


def escape(s: str) -> str:
    return (
        s.replace("\\", "\\\\")
        .replace('"', '\\"')
        .replace("\n", "\\n")
        .replace("\r", "\\r")
        .replace("\t", "\\t")
    )


def _sniff_utf16(data: bytes) -> str | None:
    """BOM-less UTF-16: an ASCII-heavy text has a NUL in every other byte."""
    if len(data) < 4 or len(data) % 2:
        return None
    even = data[0::2].count(0)
    odd = data[1::2].count(0)
    half = len(data) // 2
    if odd > half * 0.6 and even == 0:
        return "utf-16-le"
    if even > half * 0.6 and odd == 0:
        return "utf-16-be"
    return None


class _Tok:
    __slots__ = ("end", "kind", "start", "text")

    def __init__(self, kind, start, end, text=""):
        self.kind, self.start, self.end, self.text = kind, start, end, text


def _tokens(text: str):
    """Yield tokens: comment, string (quoted; start/end exclude the quotes), word
    (unquoted string), and single punctuation `= ; { }`."""
    i, n = 0, len(text)
    while i < n:
        c = text[i]
        if c in " \t\r\n\f\v﻿":
            i += 1
            continue
        if text.startswith("/*", i):
            j = text.find("*/", i + 2)
            if j == -1:
                raise ValueError("This .strings file has a /* comment that is never closed.")
            yield _Tok("comment", i, j + 2, text[i + 2 : j])
            i = j + 2
            continue
        if text.startswith("//", i):
            j = i
            while j < n and text[j] not in "\r\n":
                j += 1
            yield _Tok("comment", i, j, text[i + 2 : j])
            i = j
            continue
        if c == '"':
            j = i + 1
            while j < n:
                ch = text[j]
                if ch == "\\":
                    j += 2
                    continue
                if ch == '"':
                    break
                j += 1
            if j >= n:
                raise ValueError("This .strings file has a quoted string that is never closed.")
            yield _Tok("string", i + 1, j)
            i = j + 1
            continue
        if c in "=;{}":
            yield _Tok(c, i, i + 1)
            i += 1
            continue
        if c in _UNQUOTED:
            j = i
            while j < n and text[j] in _UNQUOTED:
                j += 1
            yield _Tok("word", i, j)
            i = j
            continue
        raise ValueError(
            f"This .strings file has an unexpected character {c!r} at position {i}; "
            'entries must look like "key" = "value";'
        )


@register
class AppleStringsFilter(KeyValueFilter):
    fmt = "strings"
    exts = ()

    def _decode(self, data: bytes) -> tuple[str, ResourceEncoding]:
        if data.startswith(b"bplist00"):
            raise InputLimitError(
                "This .strings file is a binary property list. Convert it to text first "
                "(plutil -convert xml1, or open and re-save it in Xcode), then upload again."
            )
        bom = sniff_bom(data)
        if bom:
            label = "utf-16" if "16" in bom[1] else "utf-8"
            enc = ResourceEncoding(codec=bom[1], bom=bom[0], label=label)
            return decode_with(data, enc), enc
        # NUL bytes are valid UTF-8 but never in a UTF-8 .strings file: sniff UTF-16 first.
        codec = _sniff_utf16(data) if b"\x00" in data else None
        if codec:
            enc = ResourceEncoding(codec=codec, label="utf-16")
            return decode_with(data, enc), enc
        if b"\x00" not in data:
            try:
                return data.decode("utf-8"), ResourceEncoding(codec="utf-8")
            except UnicodeDecodeError:
                pass
        raise UnsupportedEncodingError(
            "This .strings file is neither UTF-8 nor UTF-16, the encodings Xcode reads; "
            "re-save the file as UTF-8."
        )

    def _budget(self, text: str) -> None:
        super()._budget(text)
        from app import config

        if text.count('"') > 8 * config.MAX_DOCUMENT_SEGMENTS:
            raise InputLimitError(
                "This .strings file has far more strings than MAX_DOCUMENT_SEGMENTS allows "
                f"({config.MAX_DOCUMENT_SEGMENTS}). Split it into smaller files."
            )

    def _scan(self, text: str) -> list[Entry]:
        entries: list[Entry] = []
        comment: list[str] = []
        state = 0  # 0 expect key, 1 expect '=', 2 expect value, 3 expect ';'
        key = None
        val = None
        for tok in _tokens(text):
            if tok.kind == "comment":
                if state == 0:
                    comment.append(tok.text.strip())
                continue
            if state == 0:
                if tok.kind in ("{", "}"):
                    continue
                if tok.kind not in ("string", "word"):
                    raise ValueError(f"This .strings file has a stray {tok.kind!r} at {tok.start}.")
                key, state = tok, 1
            elif state == 1:
                if tok.kind != "=":
                    raise ValueError(f"This .strings file is missing '=' at {tok.start}.")
                state = 2
            elif state == 2:
                if tok.kind not in ("string", "word"):
                    raise ValueError(f"This .strings file is missing a value at {tok.start}.")
                val, state = tok, 3
            else:
                if tok.kind != ";":
                    raise ValueError(f"This .strings file is missing ';' at {tok.start}.")
                raw_key = text[key.start : key.end]
                raw_val = text[val.start : val.end]
                entries.append(
                    Entry(
                        key=unescape(raw_key) if key.kind == "string" else raw_key,
                        value=unescape(raw_val) if val.kind == "string" else raw_val,
                        start=val.start,
                        end=val.end,
                        comment="\n".join(c for c in comment if c),
                        extra={"quoted": val.kind == "string"},
                    )
                )
                comment, state = [], 0
        if state != 0:
            raise ValueError("This .strings file ends in the middle of an entry.")
        return entries

    def _encode_value(self, new: str, entry: Entry, text: str, enc: ResourceEncoding) -> str:
        if entry.extra.get("quoted"):
            return escape(new)
        return '"' + escape(new) + '"'  # an unquoted word becomes a quoted string
