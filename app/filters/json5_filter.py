"""JSON5 and JSONC filter (.json5, .jsonc): JSON with comments, trailing commas and, for
JSON5, unquoted keys, single-quoted strings, hex numbers, Infinity/NaN, leading `+` and
line continuations.

Design (hand-written tokenizer + splice, no new dependency): a regex-driven, ITERATIVE
scanner (no recursion, explicit depth cap) validates the whole file and records the exact
source span of every string VALUE (never keys) in document order. The selection rule is
json_filter's own `_is_translatable` (a string with at least one letter or digit), and the
walk order is json_filter's (object values in order, array items in order, depth first),
so a .json5 file that is also plain JSON yields the same units as the JSON filter. Skipped
strings are still walked, in the same order at parse and build, so unit indices line up.

build() splices each translated value back into its span in the ORIGINAL quote style
(`"..."` or `'...'`), escaping only what that quote style needs; every other character
(comments, keys, commas, number spellings, whitespace, line endings, a UTF-8 BOM) is kept
byte for byte, and an untranslated or unchanged value keeps its original spelling
(including its original escapes). JSONC files are read with the same grammar (JSONC is a
subset of it: JSON plus comments and trailing commas, as VS Code's jsonc mode allows) and
only ever contain double-quoted strings, whose re-escaping is plain JSON escaping.

Duplicate names: JSON5 leaves them unspecified; this filter translates every occurrence
(each is a separate span in the file), where json_filter's OrderedDict keeps one.

Specs: https://spec.json5.org/ (JSON5 1.0.0), https://code.visualstudio.com/docs/languages/json
(JSON with Comments). Documented in: docs/architecture/translation-pipeline.md
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from app import config
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
from app.filters.json_filter import _is_translatable  # the ONE value-selection rule
from app.filters.kv_resource import KeyValueRecord  # the shared record shape (Batch 1, b1)

MAX_DEPTH = 256
_UTF8_BOM = b"\xef\xbb\xbf"

# JSON5 whitespace: TAB LF VT FF CR SPACE NBSP LS PS BOM and the Unicode Zs category.
_WS = "\t\n\v\f\r \u00a0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000\ufeff"
_LINE_END = "\n\r\u2028\u2029"
# Whitespace and comments in one C-speed match; an unterminated block comment is a
# separate error check (the pattern simply stops before it).
_SKIP_RE = re.compile(rf"(?:[{_WS}]+|//[^{_LINE_END}]*|/\*[\s\S]*?\*/)*")
_COMMENT_RE = re.compile(rf"//[^{_LINE_END}]*|/\*[\s\S]*?\*/")
# Unrolled-loop string literals (linear, no nested quantifier): any char but the quote,
# a backslash or LF/CR; or a backslash escape (CRLF counts as one line continuation).
_STR_RE = {
    '"': re.compile(r'"[^"\\\n\r]*(?:\\(?:\r\n|[\s\S])[^"\\\n\r]*)*"'),
    "'": re.compile(r"'[^'\\\n\r]*(?:\\(?:\r\n|[\s\S])[^'\\\n\r]*)*'"),
}
_LITERAL_RE = re.compile(
    r"[+-]?(?:Infinity|NaN|0[xX][0-9a-fA-F]+|(?:\d+\.?\d*|\.\d+)(?:[eE][+-]?\d+)?)"
    r"|true|false|null"
)
# ES5.1 IdentifierName, approximated with Python's Unicode \w (letters, digits, marks,
# connector punctuation) plus `$`, ZWNJ, ZWJ and \uXXXX escapes; must not start with a digit.
_IDENT_RE = re.compile(r"(?:[^\W\d]|\$|\\u[0-9a-fA-F]{4})(?:[\w$\u200c\u200d]|\\u[0-9a-fA-F]{4})*")
_ESC_RE = re.compile(r"\\(?:x([0-9a-fA-F]{2})|u([0-9a-fA-F]{4})|(\r\n|[\s\S]))")
_SIMPLE_ESC = {
    "b": "\b",
    "f": "\f",
    "n": "\n",
    "r": "\r",
    "t": "\t",
    "v": "\v",
    "\n": "",
    "\r": "",
    "\r\n": "",
    "\u2028": "",
    "\u2029": "",
}


def check_json5_budget(text: str, what: str = "This JSON5/JSONC file") -> None:
    """Pre-parse budget (#571 pattern, json_filter.check_json_budget): refuse far above any
    real resource file before any per-token work. Both quote characters count, since JSON5
    strings may be single-quoted; comments bound the token count the same way."""
    limit = config.MAX_DOCUMENT_SEGMENTS
    if text.count('"') + text.count("'") > 40 * limit:
        raise InputLimitError(
            f"{what} has far more strings than MAX_DOCUMENT_SEGMENTS allows "
            f"({limit}). Split it into smaller files."
        )
    if text.count(",") + text.count("[") + text.count("{") + text.count("/") > 20 * limit:
        raise InputLimitError(
            f"{what} has far more values than MAX_DOCUMENT_SEGMENTS allows "
            f"({limit}). Split it into smaller files."
        )


class Json5SyntaxError(ValueError):
    """Not valid JSON5/JSONC. A ValueError, so every upload door shows a 'parse' refusal."""


def _line_col(text: str, pos: int) -> str:
    line = text.count("\n", 0, pos) + 1
    col = pos - (text.rfind("\n", 0, pos) + 1) + 1
    return f"line {line} column {col}"


def _err(text: str, pos: int, msg: str) -> Json5SyntaxError:
    return Json5SyntaxError(f"Invalid JSON5/JSONC at {_line_col(text, pos)}: {msg}")


def _decode_string(text: str, start: int, end: int) -> str:
    """The value of the string literal text[start:end] (quotes included), per JSON5 5.1."""
    body = text[start + 1 : end - 1]
    if "\\" not in body:
        return body

    def one(m: re.Match) -> str:
        if m.group(1):
            return chr(int(m.group(1), 16))
        if m.group(2):
            return chr(int(m.group(2), 16))
        c = m.group(3)
        if c in _SIMPLE_ESC:
            return _SIMPLE_ESC[c]
        if c == "0":
            nxt = body[m.end() : m.end() + 1]
            if nxt.isdigit():
                raise _err(text, start + 1 + m.start(), "\\0 followed by a digit")
            return "\0"
        if c in "123456789":
            raise _err(text, start + 1 + m.start(), f"invalid escape \\{c}")
        if c in "xu":
            raise _err(text, start + 1 + m.start(), f"incomplete \\{c} escape")
        return c  # \' \" \\ and any other character: the character itself

    out = _ESC_RE.sub(one, body)
    if any("\ud800" <= ch <= "\udfff" for ch in out):
        try:  # join \uD83D\uDE00 pairs; a lone surrogate cannot be stored as UTF-8
            out = out.encode("utf-16", "surrogatepass").decode("utf-16")
        except UnicodeDecodeError as exc:
            raise _err(text, start, "lone UTF-16 surrogate escape") from exc
    return out


def _decode_ident(name: str) -> str:
    return re.sub(r"\\u([0-9a-fA-F]{4})", lambda m: chr(int(m.group(1), 16)), name)


@dataclass
class _Str:
    start: int
    end: int
    quote: str
    value: str
    path: tuple
    comment: str
    translatable: bool


def _skip(text: str, pos: int, comments: list | None = None) -> int:
    m = _SKIP_RE.match(text, pos)
    end = m.end()
    if text.startswith("/*", end):
        raise _err(text, end, "unterminated block comment")
    if comments is not None and end > pos:
        gap = text[pos:end]
        for cm in _COMMENT_RE.finditer(gap):
            # a comment on the same line as the previous token belongs to that token
            if comments or any(ch in gap[: cm.start()] for ch in _LINE_END) or pos == 0:
                comments.append(cm.group(0))
    return end


def _comment_text(raw: list) -> str:
    lines = []
    for c in raw:
        if c.startswith("//"):
            lines.append(c[2:].strip())
        else:
            for ln in c[2:-2].splitlines():
                ln = ln.strip().lstrip("*").strip()
                if ln:
                    lines.append(ln)
    return "\n".join(lines)


def _scan(text: str) -> list[_Str]:
    """Validate the whole document and return every string VALUE in walk order."""
    n = len(text)
    out: list[_Str] = []
    stack: list[list] = []  # [opener, path, item_count]
    pending: list[str] = []  # comments since the last structural token
    expect = "value"
    path: tuple = ()
    comment = ""
    translatable = 0
    pos = _skip(text, 0, pending)
    while True:
        c = text[pos] if pos < n else ""
        if expect == "value":
            if c in ("{", "["):
                if len(stack) >= MAX_DEPTH:
                    raise InputLimitError("This JSON5/JSONC file is nested too deeply.")
                stack.append([c, path, 0])
                pos += 1
                expect = "key_or_close" if c == "{" else "value_or_close"
            elif c in ('"', "'"):
                m = _STR_RE[c].match(text, pos)
                if not m:
                    raise _err(text, pos, "unterminated string")
                value = _decode_string(text, pos, m.end())
                ok = _is_translatable(value)
                out.append(_Str(pos, m.end(), c, value, path, comment, ok))
                if ok:
                    translatable += 1
                    check_segment_cap(translatable)  # #571: stop early
                pos = m.end()
                expect = "after_value"
            else:
                m = _LITERAL_RE.match(text, pos)
                if not m or (m.end() < n and (text[m.end()].isalnum() or text[m.end()] in "_$")):
                    raise _err(text, pos, "expected a value")
                pos = m.end()
                expect = "after_value"
            comment = ""
            pending = []
        elif expect == "value_or_close":
            top = stack[-1]
            if c == "]":
                stack.pop()
                pos += 1
                expect = "after_value"
                pending = []
            else:
                path = (*top[1], top[2])
                comment = _comment_text(pending)
                expect = "value"
                continue  # no skip: already at the value
        elif expect == "key_or_close":
            top = stack[-1]
            if c == "}":
                stack.pop()
                pos += 1
                expect = "after_value"
                pending = []
            else:
                comment = _comment_text(pending)
                if c in ('"', "'"):
                    m = _STR_RE[c].match(text, pos)
                    if not m:
                        raise _err(text, pos, "unterminated string")
                    key = _decode_string(text, pos, m.end())
                else:
                    m = _IDENT_RE.match(text, pos)
                    if not m:
                        raise _err(text, pos, "expected a key or '}'")
                    key = _decode_ident(m.group(0))
                pos = _skip(text, m.end())
                if not text.startswith(":", pos):
                    raise _err(text, pos, "expected ':' after a key")
                pos += 1
                path = (*top[1], key)
                expect = "value"
                pending = []
        else:  # after_value
            if not stack:
                if pos < n:
                    raise _err(text, pos, "unexpected content after the top-level value")
                return out
            top = stack[-1]
            if c == ",":
                top[2] += 1
                pos += 1
                expect = "key_or_close" if top[0] == "{" else "value_or_close"
                pending = []
            elif (c == "}" and top[0] == "{") or (c == "]" and top[0] == "["):
                stack.pop()
                pos += 1
                pending = []
            else:
                raise _err(text, pos, "expected ',' or a closing bracket")
        pos = _skip(text, pos, pending)


def _escape(value: str, quote: str) -> str:
    """A string literal body for `value` in `quote` style. Only JSON escapes are used, so a
    double-quoted result is valid JSON/JSONC and a single-quoted one valid JSON5. LS/PS
    are escaped (JSON5 5.2: generators should)."""
    out = []
    for ch in value:
        if ch == "\\":
            out.append("\\\\")
        elif ch == quote:
            out.append("\\" + ch)
        elif ch == "\n":
            out.append("\\n")
        elif ch == "\r":
            out.append("\\r")
        elif ch == "\t":
            out.append("\\t")
        elif ch == "\b":
            out.append("\\b")
        elif ch == "\f":
            out.append("\\f")
        elif ch < " " or ch in "\u2028\u2029\x7f":
            out.append(f"\\u{ord(ch):04x}")
        else:
            out.append(ch)
    return quote + "".join(out) + quote


@register
class Json5Filter(Filter):
    fmt = "json5"
    exts = ("jsonc",)

    @staticmethod
    def _read(data: bytes, *, build: bool = False) -> tuple[str, str]:
        if build:
            return decode_text_lossless(data, sig=True), "utf-8"
        return read_text_upload(data, sig=True)

    def _strings(self, text: str) -> list[_Str]:
        check_json5_budget(text)
        return _scan(text)

    def parse(self, data: bytes, provider: str | None = None) -> ParseResult:
        text, enc = self._read(data)
        strings = self._strings(text)
        values = [s.value for s in strings if s.translatable]
        units = [TextUnit(p_index=i, text=t) for i, t in enumerate(values)]
        check_unit_texts(u.text for u in units)
        skeleton = {
            "format": "json5",
            "units": [{"p_index": u.p_index, "text": u.text, "style": ""} for u in units],
        }
        return ParseResult(skeleton=note_source_encoding(skeleton, enc), units=units)

    def build(self, original: bytes, paragraph_texts: dict) -> bytes:
        raw = bytes(original)
        text, _enc = self._read(raw, build=True)
        parts: list[str] = []
        pos = 0
        i = 0
        for s in self._strings(text):
            if not s.translatable:
                continue
            new = paragraph_texts.get(str(i), paragraph_texts.get(i))
            i += 1
            if new is None or str(new) == s.value:
                continue
            parts.append(text[pos : s.start])
            parts.append(_escape(str(new), s.quote))
            pos = s.end
        parts.append(text[pos:])
        bom = _UTF8_BOM if raw.startswith(_UTF8_BOM) else b""
        return bom + encode_text_lossless("".join(parts))

    def records(self, data: bytes) -> list[KeyValueRecord]:
        """Key-based view (Batch 9): one record per string value, translatable or not.
        Keys are dotted paths (`menu.items[0].label`); `comment` is the comment block
        directly above the key (a comment on the previous value's line is not included)."""
        text, _enc = self._read(data)
        records: list[KeyValueRecord] = []
        seen: set = set()
        n = 0
        for s in self._strings(text):
            key = ""
            for seg in s.path:
                key += f"[{seg}]" if isinstance(seg, int) else ("." if key else "") + str(seg)
            unit = None
            if s.translatable:
                unit = n
                n += 1
            records.append(
                KeyValueRecord(
                    key=key,
                    value=s.value,
                    comment=s.comment,
                    unit_index=unit,
                    duplicate=key in seen,
                )
            )
            seen.add(key)
        return records
