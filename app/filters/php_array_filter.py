"""PHP localization arrays: Laravel `lang/*.php` files and CodeIgniter language files.

Two shapes are read (and nothing else):

  Laravel / CodeIgniter 4    <?php return [ 'key' => 'value', 'nested' => [ ... ] ];
  CodeIgniter 3              <?php $lang['key'] = 'value';   (also $lang = array(...);)

The file is TOKENISED, never executed: a small lexer reads PHP string literals
(single-quoted, double-quoted, heredoc, nowdoc), numbers, comments and punctuation,
and a parser accepts only array literals (`[...]`, `array(...)`), `key => value` pairs,
string values, static concatenation of string literals (`'a' . 'b'`), numbers,
true/false/null, `declare(strict_types=1);` and the CodeIgniter direct-access guard
(`defined('BASEPATH') OR exit('...');`). Anything else (a function call such as
`__()` or `env()`, a constant, a variable or `{$x}` interpolated into a value,
`namespace`/`use`, inline HTML after `?>`, attributes) is REFUSED with a plain reason
and a line number, because its value cannot be proven static.

Units: every string VALUE with a letter or digit, in source order (keys never; values
without letters or digits are skipped exactly the same way on parse and build, like
json_filter). A concatenation is one unit. Protected point tokens (⟦x1⟧): Laravel
`:placeholder`, pluralisation conditions (`{0}`, `[1,*]`, `[2,19]`) and the `|`
separator, printf (`%s`, `%1$s`, `%d`), simple ICU arguments (`{0}`, `{name}`,
`{0, number}`), HTML tags.

Build replaces only the changed literals (byte-for-byte identity otherwise) and writes
each one back in the same quote style, escaped for it. A translated concatenation
becomes one literal; a translated heredoc/nowdoc becomes a single-quoted literal.

Specs: https://www.php.net/manual/en/language.types.string.php (string syntax and escapes),
https://www.php.net/manual/en/language.types.array.php (array syntax),
https://laravel.com/docs/12.x/localization (placeholders, pluralisation),
https://codeigniter.com/userguide3/libraries/language.html (CI3 $lang files),
https://codeigniter.com/user_guide/outgoing/localization.html (CI4 return arrays).
"""

import re

from app import config
from app.filters.base import (
    Filter,
    InputLimitError,
    ParseResult,
    TextUnit,
    check_segment_cap,
    check_unit_texts,
    note_source_encoding,
    read_text_upload,
    register,
)
from app.services.segment import _PLACEHOLDER_RE, is_point_kind, ph_free, ph_point

_MAX_DEPTH = 32

_OPEN_RE = re.compile(r"\A﻿?[ \t\r\n]*<\?php(?=[ \t\r\n])", re.IGNORECASE)
_LABEL = r"[A-Za-z_\x80-￿][A-Za-z0-9_\x80-￿]*"
_HEREDOC_RE = re.compile(
    r"<<<[ \t]*(?:\"(" + _LABEL + r")\"|'(" + _LABEL + r")'|(" + _LABEL + r"))\r?\n"
)
_TOKEN_RE = re.compile(
    r"""
    (?P<ws>\s+)
  | (?P<comment>/\*.*?\*/|(?://|\#(?!\[))[^\n?]*(?:\?(?!>)[^\n?]*)*)
  | (?P<sq>'(?:[^'\\]|\\.)*')
  | (?P<dq>"(?:[^"\\]|\\.)*")
  | (?P<num>0[xX][0-9a-fA-F_]+|\d[\d_]*(?:\.\d+)?(?:[eE][+-]?\d+)?)
  | (?P<var>\$[^\W\d]\w*)
  | (?P<ident>[^\W\d]\w*)
  | (?P<close>\?>)
  | (?P<punct>=>|\|\||[\[\](),;=.\-+])
    """,
    re.S | re.X,
)

# Placeholders found in a DECODED value. Order matters: a Laravel plural condition at the
# start of a `|` segment first, then tags, ICU arguments, printf, `:name`, the `|` itself.
_PH_SRC_RE = re.compile(
    r"(?:(?<=\|)|^)[ \t]*(?:\{-?\d+\}|\[\s*(?:-?\d+|\*)\s*,\s*(?:-?\d+|\*)\s*\])"
    r"|⟦[^⟦⟧]{0,20}⟧"
    r"|</?[A-Za-z][^<>]{0,500}>"
    r"|\{[A-Za-z0-9_]+(?:\s*,\s*[A-Za-z]+(?:\s*,\s*[^{}]*)?)?\}"
    r"|%(?:\d+\$)?[-+0#']*\d*(?:\.\d+)?[bcdeEfFgGosuxX%]"
    r"|(?<![\w:]):[A-Za-z_][A-Za-z0-9_]*"
    r"|\|"
)

_FREE_PH = re.compile(r":[A-Za-z_]|%\d+\$")

_SIMPLE_ESC = {
    "n": 0x0A,
    "t": 0x09,
    "r": 0x0D,
    "v": 0x0B,
    "e": 0x1B,
    "f": 0x0C,
    "\\": 0x5C,
    "$": 0x24,
}
_DQ_ESC_RE = re.compile(
    r"\\(?:([ntrvef\\$\"])|([0-7]{1,3})|x([0-9A-Fa-f]{1,2})|u\{([0-9A-Fa-f]+)\})"
)
_INTERP_RE = re.compile(r"\$[A-Za-z_\x80-￿{]|\{\$")


class _Refuse(InputLimitError):
    pass


def _line_of(text: str, pos: int) -> int:
    return text.count("\n", 0, pos) + 1


def _refuse(text: str, pos: int, why: str):
    raise _Refuse(
        f"This PHP file is not a plain localization array (line {_line_of(text, pos)}: {why}). "
        "Only `return [...]` or `$lang['key'] = '...'` files whose values are string "
        "literals can be translated; the file is never executed."
    )


# ---------------------------------------------------------------------------- string decoding


def _decode_sq(body: str) -> str:
    return re.sub(r"\\([\\'])", r"\1", body)


def _decode_dq(text: str, pos: int, body: str, quote: str) -> str:
    """Double-quoted / heredoc body -> value. Escapes produce BYTES (PHP strings are
    bytes); the result must be valid UTF-8. Any interpolation is refused."""
    if _INTERP_RE.search(body.replace("\\\\", "\0\0").replace("\\$", "\0\0")):
        _refuse(text, pos, "a variable is interpolated into a string")
    out = bytearray()
    i = 0
    for m in _DQ_ESC_RE.finditer(body):
        out += body[i : m.start()].encode("utf-8", "surrogatepass")
        i = m.end()
        simple, octal, hexa, uni = m.groups()
        if simple is not None:
            if simple == '"' and quote != '"':
                out += b'\\"'  # heredoc: \" is not an escape
            else:
                out.append(_SIMPLE_ESC.get(simple, ord(simple)))
        elif octal is not None:
            out.append(int(octal, 8) & 0xFF)
        elif hexa is not None:
            out.append(int(hexa, 16))
        else:
            cp = int(uni[:8], 16)
            if cp > 0x10FFFF or 0xD800 <= cp <= 0xDFFF:
                _refuse(text, pos, "an invalid \\u{...} escape")
            out += chr(cp).encode("utf-8")
    out += body[i:].encode("utf-8", "surrogatepass")
    try:
        return out.decode("utf-8")
    except UnicodeDecodeError:
        _refuse(text, pos, "a string escape that is not valid UTF-8")


# ---------------------------------------------------------------------------- lexer


class _Tok:
    __slots__ = ("end", "kind", "quote", "raw", "start", "value")

    def __init__(self, kind, start, end, raw, value=None, quote=""):
        self.kind, self.start, self.end, self.raw = kind, start, end, raw
        self.value, self.quote = value, quote


def _heredoc(text: str, m: re.Match) -> _Tok:
    label = m.group(1) or m.group(2) or m.group(3)
    nowdoc = m.group(2) is not None
    body_start = m.end()
    close_re = re.compile(r"(?m)^([ \t]*)" + re.escape(label) + r"(?![A-Za-z0-9_\x80-￿])")
    c = close_re.search(text, body_start)
    if not c:
        _refuse(text, m.start(), "a heredoc with no closing label")
    indent = c.group(1)
    body = text[body_start : c.start()]
    if body.endswith("\r\n"):
        body = body[:-2]
    elif body.endswith("\n"):
        body = body[:-1]
    if indent:
        lines = body.split("\n")
        for ln in lines:
            if ln.strip("\r") and not ln.startswith(indent):
                _refuse(text, m.start(), "a heredoc line indented less than its closing label")
        body = "\n".join(ln[len(indent) :] if ln.startswith(indent) else "" for ln in lines)
    value = body if nowdoc else _decode_dq(text, m.start(), body, "")
    return _Tok("str", m.start(), c.end(), text[m.start() : c.end()], value, "n" if nowdoc else "h")


def _lex(text: str, pos: int) -> list:
    toks: list = []
    cap = 60 * config.MAX_DOCUMENT_SEGMENTS
    n = len(text)
    while pos < n:
        if text.startswith("<<<", pos):
            hm = _HEREDOC_RE.match(text, pos)
            if not hm:
                _refuse(text, pos, "a malformed heredoc")
            tok = _heredoc(text, hm)
            toks.append(tok)
            pos = tok.end
            continue
        m = _TOKEN_RE.match(text, pos)
        if not m:
            _refuse(text, pos, f"unexpected {text[pos : pos + 12]!r}")
        kind = m.lastgroup
        raw = m.group()
        if kind == "close":
            if text[m.end() :].strip():
                _refuse(text, m.end(), "content after the closing ?> tag")
            toks.append(_Tok("close", m.start(), m.end(), raw))
            break
        if kind == "sq":
            toks.append(_Tok("str", m.start(), m.end(), raw, _decode_sq(raw[1:-1]), "'"))
        elif kind == "dq":
            toks.append(
                _Tok(
                    "str", m.start(), m.end(), raw, _decode_dq(text, m.start(), raw[1:-1], '"'), '"'
                )
            )
        elif kind not in ("ws", "comment"):
            toks.append(_Tok(kind, m.start(), m.end(), raw))
        if len(toks) > cap:
            raise InputLimitError(
                "This PHP file has far more tokens than MAX_DOCUMENT_SEGMENTS allows "
                f"({config.MAX_DOCUMENT_SEGMENTS}). Split it into smaller files."
            )
        pos = m.end()
    return toks


# ---------------------------------------------------------------------------- parser


class _Parser:
    def __init__(self, text: str):
        self.text = text
        om = _OPEN_RE.match(text)
        if not om:
            _refuse(text, 0, "the file must start with <?php")
        self.toks = _lex(text, om.end())
        self.i = 0
        self.units: list[dict] = []
        self._walk()

    # -- token helpers
    def _peek(self, k=0):
        j = self.i + k
        return self.toks[j] if j < len(self.toks) else None

    def _at(self) -> int:
        t = self._peek()
        return t.start if t else len(self.text)

    def _next(self):
        t = self._peek()
        if t is None:
            _refuse(self.text, len(self.text), "the file ends in the middle of a statement")
        self.i += 1
        return t

    def _is(self, t, kind, raw=None) -> bool:
        if t is None or t.kind != kind:
            return False
        return raw is None or t.raw.lower() == raw

    def _expect(self, kind, raw=None, what=None):
        t = self._next()
        if not self._is(t, kind, raw):
            _refuse(self.text, t.start, f"expected {what or raw or kind}, found {t.raw[:20]!r}")
        return t

    def _end_statement(self):
        t = self._peek()
        if self._is(t, "close"):
            return
        self._expect("punct", ";", "';'")

    # -- statements
    def _walk(self):
        while True:
            t = self._peek()
            if t is None or t.kind == "close":
                return
            if self._is(t, "punct", ";"):
                self.i += 1
            elif self._is(t, "ident", "return"):
                self.i += 1
                self._value([], 0)
                self._end_statement()
            elif self._is(t, "ident", "declare"):
                self._declare()
            elif self._is(t, "ident", "defined"):
                self._guard()
            elif t.kind == "var":
                self._assign()
            else:
                _refuse(self.text, t.start, f"{t.raw[:20]!r} is not an array literal")

    def _declare(self):
        self.i += 1
        self._expect("punct", "(", "'('")
        self._expect("ident", "strict_types", "strict_types")
        self._expect("punct", "=", "'='")
        self._expect("num", what="a number")
        self._expect("punct", ")", "')'")
        self._end_statement()

    def _guard(self):
        """defined('BASEPATH') OR exit('No direct script access allowed');"""
        self.i += 1
        self._expect("punct", "(", "'('")
        self._expect("str", what="a constant name")
        self._expect("punct", ")", "')'")
        t = self._next()
        if not (self._is(t, "ident", "or") or self._is(t, "punct", "||")):
            _refuse(self.text, t.start, "only `defined(...) OR exit(...)` is allowed")
        t = self._next()
        if not (self._is(t, "ident", "exit") or self._is(t, "ident", "die")):
            _refuse(self.text, t.start, "only `defined(...) OR exit(...)` is allowed")
        if self._is(self._peek(), "punct", "("):
            self.i += 1
            if self._is(self._peek(), "str"):
                self.i += 1
            self._expect("punct", ")", "')'")
        self._end_statement()

    def _assign(self):
        var = self._next()
        path = [var.raw[1:]] if var.raw[1:] not in ("lang", "LANG") else []
        while self._is(self._peek(), "punct", "["):
            self.i += 1
            if self._is(self._peek(), "punct", "]"):
                path.append("[]")
            else:
                path.append(self._key())
            self._expect("punct", "]", "']'")
        self._expect("punct", "=", "'='")
        self._value(path, 0)
        self._end_statement()

    # -- values
    def _key(self) -> str:
        t = self._next()
        if t.kind == "str":
            return t.value
        if self._is(t, "punct", "-"):
            return "-" + self._expect("num", what="a number").raw
        if t.kind == "num":
            return t.raw
        _refuse(self.text, t.start, f"array key {t.raw[:20]!r} is not a literal")

    def _value(self, path: list, depth: int):
        if depth > _MAX_DEPTH:
            raise InputLimitError("This PHP file is nested too deeply.")
        t = self._next()
        if t.kind == "str":
            pieces = [t]
            while self._is(self._peek(), "punct", "."):
                self.i += 1
                nxt = self._next()
                if nxt.kind != "str":
                    _refuse(self.text, nxt.start, "only string literals may be concatenated")
                pieces.append(nxt)
            value = "".join(p.value for p in pieces)
            if any(ch.isalnum() for ch in value):
                self.units.append(
                    {
                        "start": pieces[0].start,
                        "end": pieces[-1].end,
                        "quote": pieces[0].quote,
                        "value": value,
                        "key": ".".join(path),
                    }
                )
                check_segment_cap(len(self.units))
            return
        if self._is(t, "punct", "["):
            self._items(path, depth, "]")
            return
        if self._is(t, "ident", "array"):
            self._expect("punct", "(", "'('")
            self._items(path, depth, ")")
            return
        if self._is(t, "punct", "-") or self._is(t, "punct", "+"):
            self._expect("num", what="a number")
            return
        if t.kind == "num" or (t.kind == "ident" and t.raw.lower() in ("true", "false", "null")):
            return
        _refuse(self.text, t.start, f"{t.raw[:20]!r} is not a literal value")

    def _items(self, path: list, depth: int, closer: str):
        auto = 0
        while True:
            if self._is(self._peek(), "punct", closer):
                self.i += 1
                return
            # A key is followed by '=>'; find it by scanning ahead over one literal.
            save = self.i
            key = None
            t = self._peek()
            if t is not None and (t.kind in ("str", "num") or self._is(t, "punct", "-")):
                k = self._key()
                if self._is(self._peek(), "punct", "=>"):
                    self.i += 1
                    key = k
                    if re.fullmatch(r"-?\d+", k):
                        auto = max(auto, int(k) + 1)
                else:
                    self.i = save
            if key is None:
                key = str(auto)
                auto += 1
            self._value([*path, key], depth + 1)
            t = self._next()
            if self._is(t, "punct", closer):
                return
            if not self._is(t, "punct", ","):
                _refuse(self.text, t.start, f"expected ',' or '{closer}', found {t.raw[:20]!r}")


# ---------------------------------------------------------------------------- units


def _unit_text(value: str) -> tuple[str, dict]:
    parts, imap, n, pos = [], {}, 0, 0
    for m in _PH_SRC_RE.finditer(value):
        if not m.group():
            continue
        parts.append(value[pos : m.start()])
        n += 1
        imap[str(n)] = {"point": m.group()}
        # #693: Laravel `:name` and positional printf `%1$s` name their argument and may
        # move in the translation (⟦pN⟧); `%s`, tags, `|` and plural conditions keep order.
        parts.append(ph_free(n) if _FREE_PH.match(m.group()) else ph_point("x", n))
        pos = m.end()
    parts.append(value[pos:])
    return "".join(parts), imap


def _escape(s: str, quote: str) -> str:
    if quote == '"':
        out = []
        for ch in s:
            if ch in '\\"$':
                out.append("\\" + ch)
            elif ch == "\n":
                out.append("\\n")
            elif ch == "\r":
                out.append("\\r")
            elif ch == "\t":
                out.append("\\t")
            elif ord(ch) < 0x20 or ord(ch) == 0x7F:
                out.append(f"\\x{ord(ch):02x}")
            else:
                out.append(ch)
        return '"' + "".join(out) + '"'
    return "'" + s.replace("\\", "\\\\").replace("'", "\\'") + "'"


def _render(text: str, imap: dict, quote: str) -> str:
    out, pos = [], 0
    for m in _PLACEHOLDER_RE.finditer(text):
        out.append(text[pos : m.start()])
        pos = m.end()
        inner = m.group(0)[1:-1]
        if is_point_kind(inner[:1]):
            info = imap.get(inner[1:])
            if info:
                out.append(info["point"])
        # unknown or paired tokens are dropped (same tolerance as rc/xliff)
    out.append(text[pos:])
    return _escape("".join(out), '"' if quote == '"' else "'")


def _budget(text: str) -> None:
    lim = config.MAX_DOCUMENT_SEGMENTS
    if text.count("'") + text.count('"') > 40 * lim:
        raise InputLimitError(
            f"This PHP file has far more strings than MAX_DOCUMENT_SEGMENTS allows ({lim}). "
            "Split it into smaller files."
        )
    if text.count(",") + text.count("[") + text.count("(") > 20 * lim:
        raise InputLimitError(
            f"This PHP file has far more values than MAX_DOCUMENT_SEGMENTS allows ({lim}). "
            "Split it into smaller files."
        )


def _load(data: bytes) -> tuple[_Parser, str]:
    text, enc = read_text_upload(data)
    _budget(text)
    return _Parser(text), enc


def _units(parser: _Parser) -> list[dict]:
    out = []
    for u in parser.units:
        t, imap = _unit_text(u["value"])
        out.append({**u, "text": t, "map": imap})
    return out


def is_php_lang_array(data: bytes) -> bool:
    """True when `data` parses as a localization array (a router for a future .php
    server-page filter can ask this before choosing a filter)."""
    try:
        _load(data)
        return True
    except (InputLimitError, ValueError):
        return False


@register
class PhpArrayFilter(Filter):
    fmt = "php"
    exts = ()

    def parse(self, data: bytes, provider: str | None = None) -> ParseResult:
        parser, enc = _load(data)
        units = [
            TextUnit(p_index=i, text=u["text"], style="php", inline_map=u["map"])
            for i, u in enumerate(_units(parser))
        ]
        check_unit_texts(u.text for u in units)
        skeleton = {
            "format": "php",
            "units": [{"p_index": u.p_index, "text": u.text, "style": u.style} for u in units],
        }
        return ParseResult(skeleton=note_source_encoding(skeleton, enc), units=units)

    def records(self, data: bytes) -> list[dict]:
        """Key view for keys import (Batch 9): dotted key path, decoded value, unit index."""
        parser, _ = _load(data)
        return [
            {"key": u["key"], "value": u["value"], "p_index": i} for i, u in enumerate(parser.units)
        ]

    def build(self, original: bytes, paragraph_texts: dict) -> bytes:
        parser, _enc = _load(original)  # the export is always UTF-8
        text = parser.text
        edits = []
        for i, u in enumerate(_units(parser)):
            new = paragraph_texts.get(str(i))
            if new is None or new == u["text"]:
                continue
            edits.append((u["start"], u["end"], _render(new, u["map"], u["quote"])))
        if not edits:
            return bytes(original)
        out, pos = [], 0  # one linear join (units are in source order, never overlapping)
        for a, b, val in edits:
            out += [text[pos:a], val]
            pos = b
        out.append(text[pos:])
        return "".join(out).encode("utf-8")
