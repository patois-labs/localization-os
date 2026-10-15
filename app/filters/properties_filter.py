"""Java .properties filter (also Play Framework `messages` files saved as .properties).

Grammar follows java.util.Properties.load(Reader), Java SE 21:
https://docs.oracle.com/en/java/javase/21/docs/api/java.base/java/util/Properties.html#load(java.io.Reader)
  * natural lines end in LF, CR or CRLF; a line whose first non-blank character is `#`
    or `!` is a comment, and comments never continue;
  * an ODD run of backslashes before the line end continues the logical line; the
    backslash, the line end and the next line's leading blanks are dropped;
  * the key ends at the first unescaped `=`, `:` or blank; blanks, one `=`/`:` and
    more blanks are skipped; the rest of the logical line (trailing blanks included)
    is the value (so build escapes a value's leading blank, `=` or `:`, #732);
  * escapes: \\t \\n \\r \\f, \\uXXXX (exactly four hex digits, anything else is an error),
    and backslash + any other character is that character.

Encoding (load(InputStream) is ISO-8859-1; PropertyResourceBundle reads UTF-8 first
and falls back to ISO-8859-1, Java 9+:
https://docs.oracle.com/en/java/javase/21/docs/api/java.base/java/util/PropertyResourceBundle.html):
  * a file that is valid UTF-8 and holds non-ASCII bytes is rebuilt as UTF-8;
  * a file that is not valid UTF-8 is read as ISO-8859-1 (never Windows-1252) and
    rebuilt as ISO-8859-1, with every character above U+00FF written as \\uXXXX;
  * an all-ASCII file (the native2ascii convention) stays all-ASCII: every character
    above U+007E is written as \\uXXXX, so any Java version reads it.
A UTF-8 byte-order mark is kept (Java itself would read it into the first key).

MessageFormat apostrophes
(https://docs.oracle.com/en/java/javase/21/docs/api/java.base/java/text/MessageFormat.html):
a value with `{n}` arguments is a MessageFormat pattern, where a literal apostrophe is
written `''`. When EVERY apostrophe in the source value is doubled, or the value has none
(#697: "Open the application {0}" -> "Ouvrez l'application {0}"), the translator sees
single apostrophes and build doubles them. A value with a lone apostrophe uses
MessageFormat quoting on purpose and is shown exactly as written; on export an apostrophe
between two letters of the translation (a French/Italian elision, `l'insérer`) is still
written `''` (#708): it never starts a quote, and a single one would swallow the arguments
after it.

Documented in: docs/architecture/translation-pipeline.md
"""

from __future__ import annotations

import re

from app.filters import mozilla_properties
from app.filters.base import InputLimitError, register, warn_if_garbled
from app.filters.kv_resource import (
    Entry,
    KeyValueFilter,
    ResourceEncoding,
    _SpanMap,
    decode_with,
    refuse_binary,
    sniff_bom,
)

_BLANKS = " \t\f"
_ESC = {"t": "\t", "n": "\n", "r": "\r", "f": "\f"}
_MF_ARG = re.compile(r"\{\s*\d+\s*[,}]")
_HEX = set("0123456789abcdefABCDEF")


def _letter(c: str) -> bool:
    return c.isalpha()


def _double_elisions(s: str) -> str:
    """#708: walk a MessageFormat pattern the way java.text.MessageFormat does (`''` is a
    literal apostrophe in or out of a quote, a single `'` toggles quoting) and write `''` for
    every single apostrophe between two letters that would OPEN a quote (`l'insérer {0}`).
    One that would close a quote the translation opened is left alone: it is what keeps the
    arguments after it working."""
    out, quoted, i = [], False, 0
    while i < len(s):
        c = s[i]
        if c != "'":
            out.append(c)
            i += 1
            continue
        if s[i + 1 : i + 2] == "'":
            out.append("''")
            i += 2
            continue
        if not quoted and i and _letter(s[i - 1]) and _letter(s[i + 1 : i + 2]):
            out.append("''")
        else:
            out.append("'")
            quoted = not quoted
        i += 1
    return "".join(out)


def unescape(s: str) -> str:
    if "\\" not in s:
        return s
    out: list[str] = []
    i, n = 0, len(s)
    while i < n:
        c = s[i]
        if c != "\\":
            j = s.find("\\", i)
            j = n if j == -1 else j
            out.append(s[i:j])
            i = j
            continue
        if i + 1 >= n:
            i += 1  # a lone trailing backslash (end of file) is dropped, like Java
            continue
        d = s[i + 1]
        if d == "u":
            hexd = s[i + 2 : i + 6]
            if len(hexd) != 4 or not set(hexd) <= _HEX:
                raise ValueError(
                    "This .properties file has a malformed \\uXXXX escape "
                    f"({s[i : i + 6]!r}); Java refuses it too. Fix the escape and upload again."
                )
            out.append(chr(int(hexd, 16)))
            i += 6
            continue
        out.append(_ESC.get(d, d))
        i += 2
    res = "".join(out)
    if any("\ud800" <= ch <= "\udfff" for ch in res):
        # 😀 is one character to Java (UTF-16 code units); pair them up. A lone
        # surrogate stays as it is (surrogatepass) rather than failing the file.
        res = res.encode("utf-16-le", "surrogatepass").decode("utf-16-le", "surrogatepass")
    return res


def _messageformat_doubled(value: str) -> bool:
    """True when the value is a MessageFormat pattern whose apostrophes are all doubled,
    or that has none at all (#697: the usual English source; a French or Italian
    translation is full of apostrophes and each one must be written ``''``)."""
    if not _MF_ARG.search(value):
        return False
    return "'" not in value.replace("''", "")


def _logical_lines(text: str):
    """Yield ('blank'|'comment'|'entry', pieces) where pieces are (start, end) raw spans
    of each natural line's content (continuation backslash and leading blanks excluded)."""
    pos, n = 0, len(text)
    while pos < n:
        # one natural line
        eol = pos
        while eol < n and text[eol] not in "\r\n":
            eol += 1
        nxt = eol + (2 if text.startswith("\r\n", eol) else 1 if eol < n else 0)
        s = pos
        while s < eol and text[s] in _BLANKS:
            s += 1
        if s == eol:
            yield "blank", [(pos, eol)]
            pos = nxt
            continue
        if text[s] in "#!":
            yield "comment", [(s, eol)]
            pos = nxt
            continue
        pieces = []
        start, end, cur_next = s, eol, nxt
        while True:
            bs = 0
            while end - bs - 1 >= start and text[end - bs - 1] == "\\":
                bs += 1
            if bs % 2 == 1 and cur_next < n:
                pieces.append((start, end - 1))
                # continuation: next natural line, leading blanks dropped
                p = cur_next
                e2 = p
                while e2 < n and text[e2] not in "\r\n":
                    e2 += 1
                n2 = e2 + (2 if text.startswith("\r\n", e2) else 1 if e2 < n else 0)
                while p < e2 and text[p] in _BLANKS:
                    p += 1
                start, end, cur_next = p, e2, n2
                continue
            pieces.append((start, end))
            break
        yield "entry", pieces
        pos = cur_next


@register
class PropertiesFilter(KeyValueFilter):
    fmt = "properties"
    exts = ()

    def _decode(self, data: bytes) -> tuple[str, ResourceEncoding]:
        bom = sniff_bom(data)
        if bom and bom[1] != "utf-8":
            raise InputLimitError(
                "This .properties file is UTF-16; Java reads .properties as UTF-8 or "
                "ISO-8859-1 only. Re-save it as UTF-8 and upload again."
            )
        bom_bytes = bom[0] if bom else b""
        body = data[len(bom_bytes) :]
        try:
            text = body.decode("utf-8")
        except UnicodeDecodeError:
            refuse_binary(data)
            enc = ResourceEncoding(codec="latin-1", label="iso8859-1", mode="latin1")
            text = decode_with(data, enc)
            # Decision 22: Java reads a non-UTF-8 .properties as ISO-8859-1 (kept), but the
            # upload warns when the letters look wrong for the project's source language.
            warn_if_garbled(text, "latin-1", "ISO-8859-1 (the Java .properties encoding)")
            return text, enc
        if b"\x00" in body:
            refuse_binary(body)
        mode = mozilla_properties.output_mode(text)  # Mozilla dialect stays UTF-8 (Batch 11)
        return text, ResourceEncoding(codec="utf-8", bom=bom_bytes, label="utf-8", mode=mode)

    def _scan(self, text: str) -> list[Entry]:
        entries: list[Entry] = []
        comment: list[str] = []
        for kind, pieces in _logical_lines(text):
            if kind == "blank":
                comment = []
                continue
            if kind == "comment":
                s, e = pieces[0]
                comment.append(text[s + 1 : e].strip())
                continue
            smap = _SpanMap(pieces)
            logical = "".join(text[s:e] for s, e in pieces)
            i, n = 0, len(logical)
            while i < n:
                c = logical[i]
                if c == "\\":
                    i += 2
                    continue
                if c in "=:" or c in _BLANKS:
                    break
                i += 1
            key_raw = logical[: min(i, n)]
            while i < n and logical[i] in _BLANKS:
                i += 1
            if i < n and logical[i] in "=:":
                i += 1
                while i < n and logical[i] in _BLANKS:
                    i += 1
            value_raw = logical[i:]
            start = smap.raw(i)
            end = pieces[-1][1]
            entries.append(
                Entry(
                    key=unescape(key_raw),
                    value=unescape(value_raw),
                    start=start,
                    end=end,
                    comment="\n".join(comment),
                )
            )
            comment = []
        return entries

    def _unit_text(self, entry: Entry) -> str:
        if _messageformat_doubled(entry.value):
            return entry.value.replace("''", "'")
        return entry.value

    def _encode_value(self, new: str, entry: Entry, text: str, enc: ResourceEncoding) -> str:
        if _messageformat_doubled(entry.value):
            new = new.replace("''", "'").replace("'", "''")
        elif _MF_ARG.search(entry.value):
            new = _double_elisions(new)  # #708: an elision never opens a quote
        mode = enc.mode
        limit = 0x7E if mode == "ascii" else 0xFF if mode == "latin1" else None
        out: list[str] = []
        for idx, ch in enumerate(new):
            if ch == "\\":
                out.append("\\\\")
            elif ch == "\n":
                out.append("\\n")
            elif ch == "\r":
                out.append("\\r")
            elif ch == "\t":
                out.append("\\t")
            elif ch == "\f":
                out.append("\\f")
            elif ch == " " and idx == 0:
                out.append("\\ ")  # a leading blank would be eaten as separator space
            elif ch in "=:" and idx == 0:
                # #732: on a `key value` line (blank separator) Java's load() takes a
                # leading `=` / `:` as the separator and drops it; `\=` / `\:` reads back
                # as the character on every separator form.
                out.append("\\" + ch)
            elif ord(ch) < 0x20 or ch == "\x7f" or (limit is not None and ord(ch) > limit):
                # Java chars are UTF-16 code units: a supplementary character becomes a
                # surrogate pair of escapes.
                b = ch.encode("utf-16-be", "surrogatepass")
                for k in range(0, len(b), 2):
                    out.append("\\u{:04X}".format(int.from_bytes(b[k : k + 2], "big")))
            else:
                out.append(ch)
        return "".join(out)
