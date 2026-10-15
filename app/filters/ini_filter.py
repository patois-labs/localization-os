"""INI filter: generic Windows-style INI and Joomla language files.

There is no single INI standard; this follows the common ground of the Windows profile
API files and PHP `parse_ini_file`, plus the Joomla language-file rules
(https://manual.joomla.org/docs/general-concepts/multilingual/language-files/):
  * `[section]` lines start a section (recorded as the entry's context);
  * a line whose first non-blank character is `;` or `#` is a comment (Joomla uses `;`);
  * `key = value` splits at the FIRST `=`; blanks around key and value are layout;
  * a value wrapped in double quotes is quoted: inside it `\\"` is a literal quote
    (Joomla's current rule) and `"_QQ_"` is the legacy Joomla spelling of one (PHP
    concatenated the quoted pieces around the `_QQ_` constant);
  * an unquoted value is taken as written (no inline comments: the Windows profile API
    has none, and Joomla values are always quoted);
  * a line with no `=` that is not a section or comment is kept as is.

On build a translated quoted value writes `"` back in the source value's own dialect
(`"_QQ_"` when the source used it and never `\\"`, else `\\"`). INI values are one line
(Joomla 4.4.1+ refuses a file with a hard line break inside a value), so a line break in
a translation is written as a space.

Encoding: UTF-8 (BOM kept) and UTF-16 with a BOM (Windows Unicode INI) are rebuilt in the
same encoding. A Windows-1252 file is converted (#509) and exported in Windows-1252 when
every character of the output fits (B-bytes), else as UTF-8.

Documented in: docs/architecture/translation-pipeline.md
"""

from __future__ import annotations

from app.filters.base import read_text_upload, register
from app.filters.kv_resource import (
    Entry,
    KeyValueFilter,
    ResourceEncoding,
    decode_with,
    sniff_bom,
)


def _lines(text: str):
    """Yield (start, end) of each natural line's content (terminator excluded)."""
    pos, n = 0, len(text)
    while pos < n:
        eol = pos
        while eol < n and text[eol] not in "\r\n":
            eol += 1
        yield pos, eol
        pos = eol + (2 if text.startswith("\r\n", eol) else 1)


@register
class IniFilter(KeyValueFilter):
    fmt = "ini"
    exts = ()

    def _decode(self, data: bytes) -> tuple[str, ResourceEncoding]:
        bom = sniff_bom(data)
        if bom:
            label = "utf-16" if "16" in bom[1] else "utf-8"
            enc = ResourceEncoding(codec=bom[1], bom=bom[0], label=label)
            return decode_with(data, enc), enc
        text, codec = read_text_upload(data)  # UTF-8, else Windows-1252 (#509), else refused
        if codec == "utf-8":
            return text, ResourceEncoding(codec="utf-8")
        return text, ResourceEncoding(codec=codec, out_codec="utf-8", label=codec)

    def _scan(self, text: str) -> list[Entry]:
        entries: list[Entry] = []
        comment: list[str] = []
        section = ""
        for s, e in _lines(text):
            line = text[s:e]
            body = line.strip()
            if not body:
                comment = []
                continue
            if body[0] in ";#":
                comment.append(body[1:].strip())
                continue
            if body.startswith("[") and body.endswith("]"):
                section = body[1:-1].strip()
                comment = []
                continue
            eq = line.find("=")
            if eq == -1:
                comment = []
                continue
            key = line[:eq].strip()
            vs = eq + 1
            ve = len(line)
            while vs < ve and line[vs] in " \t":
                vs += 1
            while ve > vs and line[ve - 1] in " \t":
                ve -= 1
            raw = line[vs:ve]
            quoted = len(raw) >= 2 and raw[0] == '"' and raw[-1] == '"'
            if quoted:
                inner = raw[1:-1]
                value = inner.replace('"_QQ_"', '"').replace('\\"', '"')
                start, end = s + vs + 1, s + ve - 1
                dialect = "qq" if '"_QQ_"' in inner and '\\"' not in inner else "bs"
            else:
                value, start, end, dialect = raw, s + vs, s + ve, ""
            entries.append(
                Entry(
                    key=key,
                    value=value,
                    start=start,
                    end=end,
                    comment="\n".join(c for c in comment if c),
                    context=section,
                    extra={"quoted": quoted, "dialect": dialect},
                )
            )
            comment = []
        return entries

    def _encode_value(self, new: str, entry: Entry, text: str, enc: ResourceEncoding) -> str:
        new = " ".join(new.replace("\r\n", "\n").replace("\r", "\n").split("\n"))
        if not entry.extra.get("quoted"):
            return new
        if entry.extra.get("dialect") == "qq":
            return new.replace('"', '"_QQ_"')
        # Undo an already-escaped quote first so `\"` in a translation is not doubled.
        return new.replace('\\"', '"').replace('"', '\\"')
