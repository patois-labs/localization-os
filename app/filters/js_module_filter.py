"""JavaScript / TypeScript locale modules: `.js .mjs .cjs .ts .mts .cts` files whose only
content is a static message object (vue-i18n, i18next resources, hand-rolled `t()` tables).

Accepted shapes (and nothing else):

  export default { key: 'value', nested: { a: "b" } };          (ESM default export)
  module.exports = { ... };   exports.home = { ... };            (CommonJS)
  const messages = { ... }; export default messages;            (named const, then exported)
  export const en = { ... };  export { en, en as default };      (named exports)
  TypeScript: `: Type` annotations, `as const`, `as Type`, `satisfies Type`,
  `import type ... from '...'`, `type X = ...`, `interface X { ... }` (types are erased,
  they never hold a message value).

The file is TOKENISED, never executed: a small lexer reads JS string literals ('...' and
"..."), template literals WITHOUT `${...}`, numbers, identifiers, comments and punctuation,
and a parser accepts only object and array literals with literal keys, string / template /
number / true / false / null values, static `'a' + 'b'` concatenation, and references to a
const declared earlier in the same file. Anything else (a function call such as
`defineMessages()` or `t()`, a spread `...x`, a computed key `[k]`, a method, an
interpolated template, an arrow function, a plain `import`, `require`, any other
statement) REFUSES the file with a plain reason and a line number, because its value
cannot be proven static.

Which files this filter claims: `.js/.mjs/.cjs/.ts/.mts/.cts` uploads are routed here by
extension, but a file is only ACCEPTED when its whole content has the locale-module
shape above; any other JavaScript or TypeScript (application code, a bundle, a config)
is refused with the line that is not a static literal, never half-translated. A `.ts`
file that is XML (a Qt Linguist file) is handed to the registered `ts` XML filter when
one exists. Connectors that scan folders (OneDrive / SharePoint) do not pick up `.js`
or `.ts` files: only an explicit upload claims one.

Units: every string VALUE with a letter or digit outside its placeholders, in source
order (keys never; other values are skipped exactly the same way on parse and build).
A concatenation is one unit. Protected point tokens (⟦x1⟧): i18next `{{x}}` /
`{{- x}}` / `{{x, number}}` and `$t(key)` nesting, vue-i18n / ICU `{x}` arguments,
`{'@'}` literals and `@:key` / `@.lower:key` links, ICU plural / select / selectordinal
structure (`{n, plural, one {` ... `} other {` ... `}}` and `#`), printf (`%s`, `%1$s`,
`%d`, `%@`), `${x}` written in a plain string, HTML / i18next Trans tags (`<1>`, `<b>`).

Build replaces only the changed literals (byte-for-byte identity otherwise) and writes
each one back in its own quote style, escaped for that quote. A translated concatenation
becomes one literal in the first piece's quote style.

Specs: https://tc39.es/ecma262/#sec-literals-string-literals (string escapes),
https://tc39.es/ecma262/#sec-template-literal-lexical-components (templates),
https://tc39.es/ecma262/#sec-automatic-semicolon-insertion (ASI),
https://www.typescriptlang.org/docs/handbook/release-notes/typescript-4-9.html (satisfies),
https://www.i18next.com/translation-function/interpolation (i18next {{x}}),
https://www.i18next.com/translation-function/nesting ($t(key)),
https://vue-i18n.intlify.dev/guide/essentials/syntax (vue-i18n {x}, {'@'}, @:key),
https://unicode-org.github.io/icu/userguide/format_parse/messages/ (ICU MessageFormat).
"""

import re

from app import config
from app.filters import base as _base
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
_LT = "\n\r\u2028\u2029"  # ECMAScript line terminators

_TOKEN_RE = re.compile(
    r"""
    (?P<ws>[\s\ufeff]+)
  | (?P<comment>//[^\n\r\u2028\u2029]*|/\*.*?\*/)
  | (?P<sq>'(?:[^'\\\n\r]|\\(?:\r\n|[\s\S]))*')
  | (?P<dq>"(?:[^"\\\n\r]|\\(?:\r\n|[\s\S]))*")
  | (?P<num>(?:0[xX][\da-fA-F_]+|0[oO][0-7_]+|0[bB][01_]+
            |(?:\d[\d_]*(?:\.[\d_]*)?|\.\d[\d_]*)(?:[eE][+-]?\d[\d_]*)?)n?)
  | (?P<ident>(?:[^\W\d]|\$)(?:\w|\$|\u200c|\u200d)*)
  | (?P<punct>\.\.\.|=>|\?\.|\?\?|[{}\[\]();,:=.<>|&?!+\-*/%~^@\#])
    """,
    re.S | re.X,
)

_ESC_RE = re.compile(
    r"\\(u\{[0-9A-Fa-f]+\}|u[0-9A-Fa-f]{4}|x[0-9A-Fa-f]{2}|0(?![0-9])|\r\n|[\s\S])"
)
_SIMPLE_ESC = {"b": "\b", "f": "\f", "n": "\n", "r": "\r", "t": "\t", "v": "\v"}

# A statement keyword at the start of a new line ends a TypeScript type that had no `;`.
_STMT_WORDS = {
    "export",
    "const",
    "let",
    "var",
    "type",
    "interface",
    "import",
    "module",
    "exports",
    "declare",
}
_CODE_WORDS = {
    "function": "a function",
    "class": "a class",
    "new": "a `new` expression",
    "async": "an async function",
    "await": "an await expression",
    "require": "a require() call",
    "import": "an import",
    "this": "`this`",
    "typeof": "a typeof expression",
    "void": "a void expression",
    "delete": "a delete expression",
    "yield": "a yield expression",
}

# Placeholders in a DECODED value (non-ICU text). Order matters: literal tokens, i18next
# {{x}} before {x}, nesting $t(), ${x}, vue-i18n links and literals, ICU / vue {x}, tags,
# printf (no space flag, so "50% off" stays text), Python-style %(name)s.
_PH_SRC_RE = re.compile(
    r"⟦[^⟦⟧]{0,20}⟧"
    r"|\{\{[^{}]{0,200}\}\}"
    r"|\$t\([^()]{0,200}\)"
    r"|\$\{[^{}]{0,200}\}"
    r"|@(?:\.[A-Za-z]+)?:(?:\{'[^'{}]*'\}|\([\w.\-]+\)|[\w.\-]*\w)"
    r"|\{'[^'{}]*'\}"
    r"|\{ ?[A-Za-z0-9_$.\-]{1,100} ?(?:, ?[A-Za-z]{1,30} ?(?:,[^{}]{0,200})?)?\}"
    r"|</?[A-Za-z0-9][^<>]{0,500}>"
    # bounded: an unbounded `[-+0#']*\d*` backtracks quadratically on "%000...0" (gate fix)
    r"|%(?:\d{1,3}\$)?[-+0#']{0,5}\d{0,10}(?:\.\d{1,10})?[bcdieEfFgGosuxX@%]"
    r"|%\([A-Za-z_]\w*\)[sdif]"
)
# #693: the placeholders that NAME their argument, so build puts the right value in
# wherever the translation moves them: i18next `{{x}}`, `${x}`, `$t(key)`, vue links `@:x`,
# ICU / vue `{name}` `{0}`, positional printf `%1$s`,
# Python `%(name)s`. Unnumbered printf `%s`, markup tags and vue `{'literal'}` keep their order.
_FREE_PH = re.compile(r"\{\{|\$\{|\$t\(|@|\{ ?[A-Za-z0-9_$.\-]|%\d{1,3}\$|%\(")
_ICU_HINT = re.compile(r"\{\s*[^\s,{}#']+\s*,\s*(?:plural|select|selectordinal)\s*,")
_ICU_ARG = re.compile(r"\{\s*([^\s,{}#']+)\s*(?:,\s*([A-Za-z]+)\s*)?")
_ICU_OFFSET = re.compile(r"\s*offset\s*:\s*\d+")
_ICU_VARIANT = re.compile(r"\s*(=-?\d+(?:\.\d+)?|[^\s{}]+)\s*\{")
_ICU_END = re.compile(r"\s*\}")
_ICU_MAX_DEPTH = 20


class _Refuse(InputLimitError):
    pass


def _line_of(text: str, pos: int) -> int:
    return text.count("\n", 0, pos) + 1


def _refuse(text: str, pos: int, why: str):
    raise _Refuse(
        f"This file is not a plain JavaScript/TypeScript locale module (line "
        f"{_line_of(text, pos)}: {why}). Only `export default {{...}}`, `module.exports = "
        "{...}` or `const x = {...}` files whose values are string literals can be "
        "translated; the file is never executed."
    )


# ---------------------------------------------------------------------------- strings


def _decode(text: str, pos: int, body: str, tmpl: bool) -> str:
    """String / template body -> cooked value (ECMAScript escapes; modules are strict, so
    legacy octal escapes are refused). A template's raw CR / CRLF reads as LF."""
    if tmpl:
        body = body.replace("\r\n", "\n").replace("\r", "\n")

    def rep(m: re.Match) -> str:
        e = m.group(1)
        if e[0] == "u" and len(e) > 1:
            cp = int(e[2:-1] if e[1] == "{" else e[1:], 16)
            if cp > 0x10FFFF:
                _refuse(text, pos, "an invalid \\u{...} escape")
            return chr(cp)
        if e[0] == "x" and len(e) > 1:
            return chr(int(e[1:], 16))
        if e == "0":
            return "\0"
        if e in ("u", "x"):
            _refuse(text, pos, "a malformed \\u or \\x escape")
        if e.isdigit():
            _refuse(text, pos, "a legacy octal escape (not allowed in a module)")
        if e in ("\r\n", "\n", "\r", "\u2028", "\u2029"):
            return ""  # line continuation
        return _SIMPLE_ESC.get(e, e)

    out = _ESC_RE.sub(rep, body)
    try:  # join \uD83D\uDE00 surrogate pairs; a lone surrogate is refused
        return out.encode("utf-16-le", "surrogatepass").decode("utf-16-le")
    except UnicodeDecodeError:
        _refuse(text, pos, "a lone surrogate \\u escape")


# ---------------------------------------------------------------------------- lexer


class _Tok:
    __slots__ = ("cooked", "end", "kind", "nl", "quote", "raw", "start")

    def __init__(self, kind, start, end, raw, nl, value=None, quote=""):
        self.kind, self.start, self.end, self.raw, self.nl = kind, start, end, raw, nl
        self.cooked, self.quote = value, quote


def _template(text: str, pos: int) -> int:
    i, n = pos + 1, len(text)
    while i < n:
        c = text[i]
        if c == "\\":
            i += 2
        elif c == "`":
            return i + 1
        elif c == "$" and text.startswith("${", i):
            _refuse(text, i, "a template literal with ${...} interpolation")
        else:
            i += 1
    _refuse(text, pos, "an unterminated template literal")


def _lex(text: str) -> list:
    toks: list = []
    cap = 60 * config.MAX_DOCUMENT_SEGMENTS
    n = len(text)
    pos = 0
    if text.startswith("#!") or text.startswith("\ufeff#!"):  # hashbang line
        pos = text.find("\n")
        pos = n if pos < 0 else pos
    nl = False
    while pos < n:
        if text[pos] == "`":
            end = _template(text, pos)
            raw = text[pos:end]
            toks.append(_Tok("str", pos, end, raw, nl, _decode(text, pos, raw[1:-1], True), "`"))
            nl, pos = False, end
            continue
        if text.startswith("/*", pos) and text.find("*/", pos + 2) < 0:
            _refuse(text, pos, "an unterminated /* comment")
        m = _TOKEN_RE.match(text, pos)
        if not m:
            _refuse(text, pos, f"unexpected {text[pos : pos + 12]!r}")
        kind, raw = m.lastgroup, m.group()
        if kind in ("ws", "comment"):
            if any(c in raw for c in _LT):
                nl = True
        elif kind in ("sq", "dq"):
            if raw[-1] != raw[0] or len(raw) < 2:
                _refuse(text, pos, "an unterminated string")
            value = _decode(text, pos, raw[1:-1], False)
            toks.append(_Tok("str", m.start(), m.end(), raw, nl, value, raw[0]))
            nl = False
        else:
            toks.append(_Tok(kind, m.start(), m.end(), raw, nl))
            nl = False
        if len(toks) > cap:
            raise InputLimitError(
                "This file has far more tokens than MAX_DOCUMENT_SEGMENTS allows "
                f"({config.MAX_DOCUMENT_SEGMENTS}). Split it into smaller files."
            )
        pos = m.end()
    return toks


# ---------------------------------------------------------------------------- parser


class _Parser:
    def __init__(self, text: str):
        self.text = text
        self.toks = _lex(text)
        self.i = 0
        self.units: list[dict] = []
        self.declared: set[str] = set()
        self.default_ref: str | None = None
        self._walk()

    # -- token helpers
    def _peek(self, k=0):
        j = self.i + k
        return self.toks[j] if j < len(self.toks) else None

    def _next(self):
        t = self._peek()
        if t is None:
            _refuse(self.text, len(self.text), "the file ends in the middle of a statement")
        self.i += 1
        return t

    @staticmethod
    def _is(t, kind, raw=None) -> bool:
        if t is None or t.kind != kind:
            return False
        return raw is None or t.raw == raw

    def _expect(self, kind, raw=None, what=None):
        t = self._next()
        if not self._is(t, kind, raw):
            _refuse(self.text, t.start, f"expected {what or raw or kind}, found {t.raw[:20]!r}")
        return t

    def _end_statement(self):
        t = self._peek()
        if t is None:
            return
        if self._is(t, "punct", ";"):
            self.i += 1
            return
        if t.nl:  # automatic semicolon insertion at a line break
            return
        _refuse(self.text, t.start, f"expected the end of the statement, found {t.raw[:20]!r}")

    def _name(self) -> str:
        t = self._next()
        if t.kind != "ident" or t.raw in _CODE_WORDS:
            _refuse(self.text, t.start, f"expected a name, found {t.raw[:20]!r}")
        return t.raw

    # -- statements
    def _walk(self):
        first = True
        while True:
            t = self._peek()
            if t is None:
                return
            if self._is(t, "punct", ";"):
                self.i += 1
            elif t.kind == "str" and first and t.cooked == "use strict" and t.quote != "`":
                self.i += 1
                self._end_statement()
            elif self._is(t, "ident", "export"):
                self._export()
            elif t.kind == "ident" and t.raw in ("const", "let", "var"):
                self._decl()
            elif self._is(t, "ident", "import"):
                self._import_type()
            elif self._is(t, "ident", "type") and self._is(self._peek(1), "ident"):
                self.i += 1
                self._type_alias()
            elif self._is(t, "ident", "interface"):
                self.i += 1
                self._interface()
            elif self._is(t, "ident", "module") and self._is(self._peek(1), "punct", "."):
                self.i += 2
                t2 = self._next()
                if not self._is(t2, "ident", "exports"):
                    _refuse(self.text, t2.start, "only `module.exports = {...}` is allowed")
                self._cjs_assign()
            elif self._is(t, "ident", "exports") and self._is(self._peek(1), "punct", "."):
                self.i += 1
                self._cjs_assign(prefix_required=True)
            else:
                why = _CODE_WORDS.get(t.raw) if t.kind == "ident" else None
                _refuse(self.text, t.start, why or f"{t.raw[:20]!r} is not a static message export")
            first = False

    def _import_type(self):
        start = self._next()
        if not self._is(self._peek(), "ident", "type"):
            _refuse(
                self.text, start.start, "an import (only `import type ... from '...'` is allowed)"
            )
        self.i += 1
        while True:
            t = self._next()
            if self._is(t, "ident", "from"):
                break
            if not (t.kind == "ident" or (t.kind == "punct" and t.raw in "{},*")):
                _refuse(self.text, t.start, f"unexpected {t.raw[:20]!r} in an import type")
        spec = self._next()
        if spec.kind != "str" or spec.quote == "`":
            _refuse(self.text, spec.start, "expected a module name")
        self._end_statement()

    def _export(self):
        start = self._next()
        t = self._peek()
        if self._is(t, "ident", "default"):
            self.i += 1
            ref = self._value([], 0, root="")
            if ref:
                self.default_ref = ref
            self._end_statement()
        elif t is not None and t.kind == "ident" and t.raw in ("const", "let", "var"):
            self._decl()
        elif self._is(t, "ident", "type") and self._is(self._peek(1), "ident"):
            self.i += 1
            self._type_alias()
        elif self._is(t, "ident", "interface"):
            self.i += 1
            self._interface()
        elif self._is(t, "punct", "{"):
            self.i += 1
            self._export_list()
        else:
            why = _CODE_WORDS.get(t.raw) if t is not None and t.kind == "ident" else None
            _refuse(self.text, start.start, why or "an export that is not a static object")

    def _export_list(self):
        while True:
            t = self._next()
            if self._is(t, "punct", "}"):
                break
            if t.kind != "ident" or t.raw not in self.declared:
                _refuse(self.text, t.start, f"export of {t.raw[:20]!r}, which is not declared here")
            if self._is(self._peek(), "ident", "as"):
                self.i += 1
                alias = self._next()
                if self._is(alias, "ident", "default"):
                    self.default_ref = t.raw
                elif alias.kind != "ident":
                    _refuse(self.text, alias.start, "expected a name")
            t = self._next()
            if self._is(t, "punct", "}"):
                break
            if not self._is(t, "punct", ","):
                _refuse(self.text, t.start, f"expected ',' or '}}', found {t.raw[:20]!r}")
        if self._is(self._peek(), "ident", "from"):
            _refuse(self.text, self._peek().start, "a re-export from another module")
        self._end_statement()

    def _decl(self):
        self.i += 1  # const / let / var
        while True:
            t = self._peek()
            name = self._name()
            if self._is(self._peek(), "punct", ":"):
                self.i += 1
                self._skip_type(stops="=")
            if not self._is(self._peek(), "punct", "="):
                _refuse(self.text, t.start, "a declaration without a literal value")
            self.i += 1
            self._value([name], 0, root=name)
            self.declared.add(name)
            if self._is(self._peek(), "punct", ","):
                self.i += 1
                continue
            break
        self._end_statement()

    def _cjs_assign(self, prefix_required=False):
        path: list = []
        while self._is(self._peek(), "punct", "."):
            self.i += 1
            path.append(self._name())
        if prefix_required and not path:
            _refuse(self.text, self._peek().start if self._peek() else 0, "expected exports.name")
        self._expect("punct", "=", "'='")
        ref = self._value(path, 0, root="")
        if ref and not path:
            self.default_ref = ref
        self._end_statement()

    def _type_alias(self):
        self._name()
        if self._is(self._peek(), "punct", "<"):
            self._skip_type(stops="=")
        self._expect("punct", "=", "'='")
        self._skip_type(stops=";")
        self._end_statement()

    def _interface(self):
        self._name()
        while not self._is(self._peek(), "punct", "{"):
            t = self._next()
            if t.kind not in ("ident", "punct") or t.raw in ("=", ";"):
                _refuse(self.text, t.start, f"unexpected {t.raw[:20]!r} in an interface")
        self._skip_type(stops="")

    def _skip_type(self, stops: str):
        """Skip a TypeScript type (erased at compile time, never a message value):
        balanced brackets; stops at depth 0 before a stop punctuator, a closing bracket of
        the enclosing literal, or a statement keyword on a new line."""
        depth = 0
        moved = False
        while True:
            t = self._peek()
            if t is None:
                return
            punct = t.kind == "punct"
            if depth == 0:
                if punct and t.raw in (*stops, ",", ";", ")", "]", "}", ">"):
                    if not moved and t.raw not in stops:
                        _refuse(self.text, t.start, "a missing type")
                    return
                if moved and t.nl and t.kind == "ident" and t.raw in _STMT_WORDS:
                    return
            if punct and t.raw in ("(", "[", "{", "<"):
                depth += 1
            elif punct and t.raw in (")", "]", "}", ">"):
                depth -= 1
            elif t.kind == "str" and t.quote == "`":
                pass  # a template literal type is still only a type
            self.i += 1
            moved = True

    # -- values
    def _value(self, path: list, depth: int, root: str) -> str | None:
        """Read one value; returns the referenced name when the value is a bare
        reference to a declared const, else None."""
        if depth > _MAX_DEPTH:
            raise InputLimitError("This file is nested too deeply.")
        t = self._next()
        ref = None
        if self._is(t, "punct", "("):
            if self._is(self._peek(), "punct", ")"):
                _refuse(self.text, t.start, "an arrow function")
            ref = self._value(path, depth + 1, root)
            self._expect("punct", ")", "')'")
        elif t.kind == "str":
            pieces = [t]
            while self._is(self._peek(), "punct", "+"):
                self.i += 1
                nxt = self._next()
                if nxt.kind != "str":
                    _refuse(self.text, nxt.start, "only string literals may be concatenated")
                pieces.append(nxt)
            self._unit(pieces, path, root)
        elif self._is(t, "punct", "{"):
            self._object(path, depth + 1, root)
        elif self._is(t, "punct", "["):
            self._array(path, depth + 1, root)
        elif t.kind == "punct" and t.raw in "-+" and self._is(self._peek(), "num"):
            self.i += 1
        elif t.kind == "num" or (t.kind == "ident" and t.raw in ("true", "false", "null")):
            pass
        elif t.kind == "ident":
            nxt = self._peek()
            if self._is(nxt, "punct", "(") or (
                nxt is not None and nxt.kind == "str" and nxt.quote == "`"
            ):
                _refuse(self.text, t.start, f"a function call ({t.raw[:20]}(...))")
            if self._is(nxt, "punct", "=>"):
                _refuse(self.text, t.start, "an arrow function")
            if t.raw in _CODE_WORDS:
                _refuse(self.text, t.start, _CODE_WORDS[t.raw])
            if t.raw not in self.declared:
                _refuse(self.text, t.start, f"{t.raw[:20]!r} is a variable, not a literal value")
            ref = t.raw
        else:
            _refuse(self.text, t.start, f"{t.raw[:20]!r} is not a literal value")
        # postfix: TypeScript `as const` / `as T` / `satisfies T`; anything else is code
        while True:
            nxt = self._peek()
            if nxt is not None and nxt.kind == "ident" and nxt.raw in ("as", "satisfies"):
                self.i += 1
                self._skip_type(stops="")
                continue
            if nxt is not None and nxt.kind == "punct" and not nxt.nl:
                if nxt.raw in ("(", "?."):
                    _refuse(self.text, nxt.start, "a function call")
                if nxt.raw in (".", "["):
                    _refuse(self.text, nxt.start, "a property access")
            break
        return ref

    def _unit(self, pieces: list, path: list, root: str):
        value = "".join(p.cooked for p in pieces)
        self.units.append(
            {
                "start": pieces[0].start,
                "end": pieces[-1].end,
                "quote": pieces[0].quote,
                "multiline": pieces[0].quote == "`" and "\n" in pieces[0].raw,
                "value": value,
                "path": list(path),
                "root": root,
            }
        )
        check_segment_cap(len(self.units))

    def _key(self) -> str:
        t = self._next()
        if self._is(t, "punct", "..."):
            _refuse(self.text, t.start, "a spread (...)")
        if self._is(t, "punct", "["):
            _refuse(self.text, t.start, "a computed key [...]")
        if t.kind == "str":
            if t.quote == "`":
                _refuse(self.text, t.start, "a template literal used as a key")
            return t.cooked
        if t.kind in ("ident", "num"):
            if (
                t.kind == "ident"
                and t.raw in ("get", "set", "async")
                and self._is(self._peek(), "ident")
            ):
                _refuse(self.text, t.start, "a getter, setter or method")
            return t.raw
        if self._is(t, "punct", "*"):
            _refuse(self.text, t.start, "a generator method")
        _refuse(self.text, t.start, f"{t.raw[:20]!r} is not a literal key")

    def _object(self, path: list, depth: int, root: str):
        while True:
            if self._is(self._peek(), "punct", "}"):
                self.i += 1
                return
            kt = self._peek()
            key = self._key()
            t = self._peek()
            if self._is(t, "punct", ":"):
                self.i += 1
                self._value([*path, key], depth, root)
            elif t is not None and t.kind == "punct" and t.raw in "(<":
                _refuse(self.text, kt.start, f"a method ({key[:20]}() {{...}})")
            elif t is not None and t.kind == "punct" and t.raw in ",}":
                if kt.kind != "ident" or key not in self.declared:
                    _refuse(
                        self.text, kt.start, f"shorthand property {key[:20]!r} refers to a variable"
                    )
            else:
                _refuse(self.text, kt.start, f"expected ':' after key {key[:20]!r}")
            t = self._next()
            if self._is(t, "punct", "}"):
                return
            if not self._is(t, "punct", ","):
                _refuse(self.text, t.start, f"expected ',' or '}}', found {t.raw[:20]!r}")

    def _array(self, path: list, depth: int, root: str):
        idx = 0
        while True:
            t = self._peek()
            if self._is(t, "punct", "]"):
                self.i += 1
                return
            if self._is(t, "punct", "..."):
                _refuse(self.text, t.start, "a spread (...)")
            if self._is(t, "punct", ","):
                _refuse(self.text, t.start, "an empty array slot")
            self._value([*path, str(idx)], depth, root)
            idx += 1
            t = self._next()
            if self._is(t, "punct", "]"):
                return
            if not self._is(t, "punct", ","):
                _refuse(self.text, t.start, f"expected ',' or ']', found {t.raw[:20]!r}")


# ---------------------------------------------------------------------------- placeholders


def _icu_split(s: str):
    """Split an ICU MessageFormat string into [(kind, text)]: the plural / select
    structure and simple arguments are tokens, the variant messages are text. Returns
    None when the string is not well-formed ICU (the caller falls back to the regex)."""
    out: list = []
    n = len(s)

    def emit(tok, t: str):
        if not t:
            return
        if out and out[-1][0] == tok and tok != "#":
            out[-1][1] += t
        else:
            out.append([tok, t])

    def message(i: int, depth: int, in_plural: bool) -> int:
        start = i
        while i < n:
            c = s[i]
            if c == "{":
                emit(False, s[start:i])
                i = arg(i, depth)
                start = i
            elif c == "}":
                break
            elif c == "#" and in_plural:
                emit(False, s[start:i])
                emit("#", "#")
                i += 1
                start = i
            elif c == "'" and i + 1 < n and s[i + 1] == "'":
                i += 2
            elif c == "'" and i + 1 < n and s[i + 1] in "{}#|":
                k = s.find("'", i + 1)
                i = n if k < 0 else k + 1
            else:
                i += 1
        emit(False, s[start:i])
        return i

    def arg(i: int, depth: int) -> int:
        if depth > _ICU_MAX_DEPTH:
            raise ValueError("deep")
        m = _ICU_ARG.match(s, i)
        if not m:
            raise ValueError("arg")
        j, typ = m.end(), m.group(2)
        if typ is None:
            if j < n and s[j] == "}":
                emit(True, s[i : j + 1])
                return j + 1
            raise ValueError("arg end")
        if typ in ("plural", "select", "selectordinal"):
            if j >= n or s[j] != ",":
                raise ValueError("comma")
            j += 1
            mo = _ICU_OFFSET.match(s, j)
            if mo:
                j = mo.end()
            tok_start, variants = i, 0
            while True:
                mv = _ICU_VARIANT.match(s, j)
                if not mv:
                    me = _ICU_END.match(s, j)
                    if me and variants:
                        emit(True, s[tok_start : me.end()])
                        return me.end()
                    raise ValueError("variant")
                emit(True, s[tok_start : mv.end()])
                j = message(mv.end(), depth + 1, typ != "select" or depth > 0)
                if j >= n or s[j] != "}":
                    raise ValueError("variant end")
                tok_start, j, variants = j, j + 1, variants + 1
        k = s.find("}", j)
        if k < 0 or "{" in s[j:k]:
            raise ValueError("style")
        emit(True, s[i : k + 1])
        return k + 1

    try:
        if message(0, 0, False) != n:
            return None
    except (ValueError, RecursionError):
        return None
    return out


def _unit_text(value: str) -> tuple[str, dict]:
    pieces = _icu_split(value) if _ICU_HINT.search(value) else None
    if pieces is None:
        pieces = [[False, value]]
    parts, imap, n = [], {}, 0

    def point(raw: str, free: bool = False):
        nonlocal n
        n += 1
        imap[str(n)] = {"point": raw}
        parts.append(ph_free(n) if free else ph_point("x", n))

    for tok, chunk in pieces:
        if tok:
            point(chunk)  # ICU plural / select structure: its order is the message's
            continue
        pos = 0
        for m in _PH_SRC_RE.finditer(chunk):
            parts.append(chunk[pos : m.start()])
            # #693: a named / numbered argument may move in the translation (⟦pN⟧)
            point(m.group(), bool(_FREE_PH.match(m.group())))
            pos = m.end()
        parts.append(chunk[pos:])
    return "".join(parts), imap


def _translatable(unit_text: str) -> bool:
    return any(ch.isalnum() for ch in _PLACEHOLDER_RE.sub("", unit_text))


def _escape(s: str, quote: str, multiline: bool) -> str:
    out = []
    n = len(s)
    for i, ch in enumerate(s):
        if ch == "\\":
            out.append("\\\\")
        elif ch == quote:
            out.append("\\" + ch)
        elif quote == "`" and ch == "$" and i + 1 < n and s[i + 1] == "{":
            out.append("\\$")
        elif ch == "\n":
            out.append("\n" if quote == "`" and multiline else "\\n")
        elif ch == "\r":
            out.append("\\r")
        elif ch == "\t":
            out.append("\\t")
        elif ch in "\u2028\u2029":
            out.append(f"\\u{ord(ch):04x}")
        elif ord(ch) < 0x20 or ord(ch) == 0x7F:
            out.append(f"\\x{ord(ch):02x}")
        else:
            out.append(ch)
    return quote + "".join(out) + quote


def _render(text: str, imap: dict, quote: str, multiline: bool) -> str:
    out, pos = [], 0
    for m in _PLACEHOLDER_RE.finditer(text):
        out.append(text[pos : m.start()])
        pos = m.end()
        inner = m.group(0)[1:-1]
        if is_point_kind(inner[:1]):
            info = imap.get(inner[1:])
            if info:
                out.append(info["point"])
        # unknown or paired tokens are dropped (same tolerance as the php/rc filters)
    out.append(text[pos:])
    return _escape("".join(out), quote, multiline)


# ---------------------------------------------------------------------------- load


def _budget(text: str) -> None:
    lim = config.MAX_DOCUMENT_SEGMENTS
    if text.count("'") + text.count('"') + text.count("`") > 40 * lim:
        raise InputLimitError(
            f"This file has far more strings than MAX_DOCUMENT_SEGMENTS allows ({lim}). "
            "Split it into smaller files."
        )
    if text.count(",") + text.count("{") + text.count("[") + text.count("(") > 20 * lim:
        raise InputLimitError(
            f"This file has far more values than MAX_DOCUMENT_SEGMENTS allows ({lim}). "
            "Split it into smaller files."
        )


def _is_xml(data: bytes) -> bool:
    head = bytes(data[:512])
    for bom in (b"\xef\xbb\xbf", b"\xff\xfe", b"\xfe\xff"):
        if head.startswith(bom):
            head = head[len(bom) :].replace(b"\x00", b"")
            break
    return head.lstrip(b" \t\r\n").startswith(b"<")


def _load(data: bytes) -> tuple[_Parser, str]:
    text, enc = read_text_upload(data)
    _budget(text)
    return _Parser(text), enc


def _units(parser: _Parser) -> list[dict]:
    out = []
    for u in parser.units:
        t, imap = _unit_text(u["value"])
        if _translatable(t):
            out.append({**u, "text": t, "map": imap})
    return out


def _key_of(u: dict, default_ref: str | None) -> str:
    path = u["path"]
    if default_ref and u["root"] == default_ref and path and path[0] == default_ref:
        path = path[1:]
    return ".".join(path)


def is_js_locale_module(data: bytes) -> bool:
    """True when `data` is a static JS/TS locale module this filter accepts (a router or
    connector can ask this before claiming a generic .js/.ts file)."""
    if _is_xml(data):
        return False
    try:
        _load(data)
        return True
    except (InputLimitError, ValueError):
        return False


def _xml_ts_filter(me):
    """The registered Qt Linguist `.ts` XML filter, when one exists (not this filter)."""
    other = _base._FILTERS.get("ts")
    return other if other is not None and other is not me else None


@register
class JsModuleFilter(Filter):
    fmt = "js"
    exts = ("mjs", "cjs", "ts", "mts", "cts")

    def _xml(self, data: bytes):
        if not _is_xml(data):
            return None
        other = _xml_ts_filter(self)
        if other is None:
            raise _Refuse(
                "This file is XML (a Qt Linguist .ts file?), not a JavaScript/TypeScript "
                "locale module."
            )
        return other

    def parse(self, data: bytes, provider: str | None = None) -> ParseResult:
        other = self._xml(data)
        if other is not None:
            return other.parse(data, provider)
        parser, enc = _load(data)
        units = [
            TextUnit(p_index=i, text=u["text"], style="js", inline_map=u["map"])
            for i, u in enumerate(_units(parser))
        ]
        check_unit_texts(u.text for u in units)
        skeleton = {
            "format": "js",
            "units": [{"p_index": u.p_index, "text": u.text, "style": u.style} for u in units],
        }
        return ParseResult(skeleton=note_source_encoding(skeleton, enc), units=units)

    def records(self, data: bytes) -> list[dict]:
        """Key view for keys import (Batch 9): dotted key path (the default export's own
        name is dropped), decoded value, unit index. An XML `.ts` gets the Qt filter's own
        records (when it has them)."""
        other = self._xml(data)
        if other is not None:
            return other.records(data) if hasattr(other, "records") else []
        parser, _ = _load(data)
        return [
            {"key": _key_of(u, parser.default_ref), "value": u["value"], "p_index": i}
            for i, u in enumerate(_units(parser))
        ]

    def build(self, original: bytes, paragraph_texts: dict) -> bytes:
        other = self._xml(original)
        if other is not None:
            return other.build(original, paragraph_texts)
        parser, _enc = _load(original)  # the export is always UTF-8
        text = parser.text
        edits = []
        for i, u in enumerate(_units(parser)):
            new = paragraph_texts.get(str(i))
            if new is None or new == u["text"]:
                continue
            edits.append((u["start"], u["end"], _render(new, u["map"], u["quote"], u["multiline"])))
        if not edits:
            return bytes(original)
        out, pos = [], 0  # one linear join (units are in source order, never overlapping)
        for a, b, val in edits:
            out += [text[pos:a], val]
            pos = b
        out.append(text[pos:])
        return "".join(out).encode("utf-8")

    def build_localized(self, original, paragraph_texts, target_locale=None):
        other = self._xml(original)
        if other is not None:
            return other.build_localized(original, paragraph_texts, target_locale)
        return self.build(original, paragraph_texts)
