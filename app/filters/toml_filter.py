"""TOML filter (file-format Batch 11): Hugo i18n, go-i18n message files and any other
TOML resource file (spec: https://toml.io/en/v1.0.0).

Only string VALUES are translated. Keys (bare, quoted or dotted), table headers,
numbers, booleans, dates, comments, blank lines, indentation and line endings are
never touched: build() splices each translated value into the exact span of its
original string token and leaves every other byte alone, so an untranslated file
rebuilds byte for byte (the UTF-8 byte-order mark too).

Reading: the stdlib `tomllib` validates the whole file first (an invalid file is
refused with its parse error), then a small scanner walks the same text in file
order and records the span, quoting kind and key path of every string value. Each
scanned value is cross-checked against the `tomllib` value at the same key path; a
disagreement refuses the file instead of guessing. parse() and build() run the same
scan, so unit indices always line up.

Writing: the translated value keeps its source quoting when it can (basic "...",
literal '...', multi-line basic \"\"\"...\"\"\", multi-line literal '''...''').
A literal string cannot hold an apostrophe, a newline or a control character, so a
translation that needs one is written as the matching basic string with escapes
(literal -> basic, multi-line literal -> multi-line basic). The rebuilt file is
parsed again with `tomllib` and every value checked before it is returned.

Message files (go-i18n v2, Hugo i18n): a table whose keys are all go-i18n message
fields is a message (go-i18n's own isMessage rule); its plural categories (zero,
one, two, few, many, other) are separate units styled `toml:<category>` and its
`description` is a translator note, not text. `id`, `hash`, `leftdelim` and
`rightdelim` are never translated anywhere. Go template actions
(`{{.Count}}`, `{{ T "x" }}`, or the table's own leftdelim/rightdelim pair) become
point tokens (the shared x-token scheme from services/segment), so a translation
cannot damage them; printf verbs and {name} placeholders stay protected by the
shared placeholder check.

Documented in: docs/architecture/translation-pipeline.md
"""

from __future__ import annotations

import re

import tomllib

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
    check_unit_texts,
    register,
)

_BOM = b"\xef\xbb\xbf"
_MAX_DEPTH = 64  # nested arrays / inline tables
_BARE_KEY = re.compile(r"[A-Za-z0-9_-]+")
_SCALAR = re.compile(r"[^,\]\}#\r\n]*")
_TOKEN_RE = re.compile(r"⟦(/?)([a-z]?)(\d+)⟧")
PLURAL_KEYS = ("zero", "one", "two", "few", "many", "other")
# go-i18n reserved message fields that are metadata, never text to translate.
_META_KEYS = {"id", "hash", "leftdelim", "rightdelim"}
_RESERVED = _META_KEYS | {"description", *PLURAL_KEYS}


def _is_message(table) -> bool:
    """go-i18n's own rule (v2/i18n/message.go isMessage): a table is a message when
    every key is reserved (id, description, hash, leftdelim, rightdelim, the plural
    categories, case-insensitive, with a string value; or exactly `translation`)."""
    if not isinstance(table, dict) or not table:
        return False
    return all(
        k == "translation" or (k.lower() in _RESERVED and isinstance(v, str))
        for k, v in table.items()
    )


_NAMED_ESCAPES = {"b": "\b", "t": "\t", "n": "\n", "f": "\f", "r": "\r", '"': '"', "\\": "\\"}
_NAMED_ESCAPES["e"] = "\x1b"  # TOML 1.1 (a 1.0 parser refuses it before we get here)
_ESCAPE_OUT = {v: "\\" + k for k, v in _NAMED_ESCAPES.items() if k != "e"}


class _ScanError(ValueError):
    pass


# ---------------------------------------------------------------------------- decode


def _decode(data: bytes) -> tuple[str, bytes]:
    """TOML files are UTF-8 by definition (spec: "A TOML file must be a valid UTF-8
    encoded Unicode document"). A UTF-8 BOM is accepted and written back."""
    data = bytes(data)
    bom = _BOM if data.startswith(_BOM) else b""
    body = data[len(bom) :]
    if body[:2] in (b"\xff\xfe", b"\xfe\xff") or _looks_binary(body):
        raise UnsupportedEncodingError(
            "This TOML file is not UTF-8 text (TOML files must be UTF-8); "
            "re-save the file as UTF-8."
        )
    try:
        return body.decode("utf-8"), bom
    except UnicodeDecodeError as exc:
        raise UnsupportedEncodingError(
            "This TOML file is not valid UTF-8 (byte 0x%02x at position %d); "  # noqa: UP031
            "re-save the file as UTF-8." % (body[exc.start], exc.start)
        ) from exc


def _budget(text: str) -> None:
    """Pre-parse budget (#571): bound the string and value counts at C speed before
    `tomllib` builds a tree."""
    check_line_budget(text)
    cap = config.MAX_DOCUMENT_SEGMENTS
    if text.count('"') + text.count("'") > 40 * cap:
        raise InputLimitError(
            f"This TOML file has far more strings than MAX_DOCUMENT_SEGMENTS allows ({cap}). "
            "Split it into smaller files."
        )
    if text.count("=") + text.count(",") + text.count("[") + text.count("{") > 20 * cap:
        raise InputLimitError(
            f"This TOML file has far more values than MAX_DOCUMENT_SEGMENTS allows ({cap}). "
            "Split it into smaller files."
        )


def _loads(text: str) -> dict:
    try:
        return tomllib.loads(text)
    except RecursionError as exc:
        raise InputLimitError("This TOML file is nested too deeply.") from exc
    except tomllib.TOMLDecodeError as exc:
        raise ValueError(f"This TOML file could not be read: {exc}") from exc


# ---------------------------------------------------------------------------- scanner


class _Scan:
    """Walk a VALID TOML document in file order and record every string value:
    {"path", "kind", "start", "end", "value", "lead"} where start/end delimit the
    whole token (quotes included) and `lead` is the newline that directly follows
    an opening multi-line delimiter (trimmed by TOML, kept by build)."""

    def __init__(self, text: str):
        self.t = text
        self.i = 0
        self.n = len(text)
        self.strings: list[dict] = []
        self._aot: dict[tuple, int] = {}
        self._run()

    # -- low-level
    def _err(self, what: str):
        line = self.t.count("\n", 0, self.i) + 1
        raise _ScanError(f"This TOML file could not be read ({what} on line {line}).")

    def _ws(self):
        t, i, n = self.t, self.i, self.n
        while i < n and t[i] in " \t":
            i += 1
        self.i = i

    def _ws_nl(self):
        """Whitespace, newlines and comments (inside arrays and inline tables)."""
        t, n = self.t, self.n
        while self.i < n:
            c = t[self.i]
            if c in " \t\r\n":
                self.i += 1
            elif c == "#":
                while self.i < n and t[self.i] not in "\r\n":
                    self.i += 1
            else:
                break

    def _eol(self):
        self._ws()
        t = self.t
        if self.i < self.n and t[self.i] == "#":
            while self.i < self.n and t[self.i] not in "\r\n":
                self.i += 1
        if self.i < self.n and t[self.i] not in "\r\n":
            self._err("unexpected text after a value")

    def _expect(self, s: str):
        if not self.t.startswith(s, self.i):
            self._err(f"expected {s!r}")
        self.i += len(s)

    # -- keys
    def _key(self) -> list[str]:
        parts = []
        while True:
            self._ws()
            c = self.t[self.i : self.i + 1]
            if c in ('"', "'"):
                kind, value, _s, _e, _lead = self._string()
                if kind.startswith("ml"):
                    self._err("multi-line string used as a key")
                parts.append(value)
            else:
                m = _BARE_KEY.match(self.t, self.i)
                if not m:
                    self._err("expected a key")
                parts.append(m.group(0))
                self.i = m.end()
            self._ws()
            if self.t.startswith(".", self.i):
                self.i += 1
                continue
            return parts

    def _resolve(self, keys: list[str]) -> list:
        out: list = []
        for k in keys:
            out.append(k)
            idx = self._aot.get(tuple(out))
            if idx is not None:
                out.append(idx)
        return out

    # -- strings
    def _string(self):
        t, start = self.t, self.i
        if t.startswith('"""', start):
            return self._ml(start, '"', True)
        if t.startswith("'''", start):
            return self._ml(start, "'", False)
        if t[start] == "'":
            end = t.find("'", start + 1)
            nl = t.find("\n", start + 1)
            if end < 0 or (0 <= nl < end):
                self._err("unterminated literal string")
            self.i = end + 1
            return "literal", t[start + 1 : end], start, end + 1, ""
        # basic
        i, out = start + 1, []
        while True:
            if i >= self.n or t[i] in "\r\n":
                self._err("unterminated string")
            c = t[i]
            if c == '"':
                break
            if c == "\\":
                i, ch = self._escape(i)
                out.append(ch)
                continue
            out.append(c)
            i += 1
        self.i = i + 1
        return "basic", "".join(out), start, i + 1, ""

    def _escape(self, i: int) -> tuple[int, str]:
        t = self.t
        e = t[i + 1 : i + 2]
        if e in _NAMED_ESCAPES:
            return i + 2, _NAMED_ESCAPES[e]
        width = {"u": 4, "U": 8, "x": 2}.get(e)
        if width:
            hexd = t[i + 2 : i + 2 + width]
            if len(hexd) == width and all(h in "0123456789abcdefABCDEF" for h in hexd):
                cp = int(hexd, 16)
                if cp <= 0x10FFFF and not 0xD800 <= cp <= 0xDFFF:
                    return i + 2 + width, chr(cp)
        self._err("invalid escape")
        return i, ""  # unreachable

    def _ml(self, start: int, q: str, basic: bool):
        t, n = self.t, self.n
        i = start + 3
        lead = ""
        if t.startswith("\r\n", i):
            lead = "\r\n"
        elif t.startswith("\n", i):
            lead = "\n"
        i += len(lead)
        out = []
        while True:
            if i >= n:
                self._err("unterminated multi-line string")
            c = t[i]
            if c == q and t.startswith(q * 3, i):
                run = 3
                while run < 5 and i + run < n and t[i + run] == q:
                    run += 1
                out.append(q * (run - 3))
                end = i + run
                break
            if basic and c == "\\":
                j = i + 1
                while j < n and t[j] in " \t":
                    j += 1
                if j < n and t[j] in "\r\n":  # line-ending backslash: trim to next text
                    while j < n and t[j] in " \t\r\n":
                        j += 1
                    i = j
                    continue
                i, ch = self._escape(i)
                out.append(ch)
                continue
            if c == "\r" and t.startswith("\r\n", i):
                out.append("\n")
                i += 2
                continue
            out.append(c)
            i += 1
        self.i = end
        kind = "ml_basic" if basic else "ml_literal"
        return kind, "".join(out), start, end, lead

    # -- values
    def _value(self, path: list, depth: int):
        if depth > _MAX_DEPTH:
            raise InputLimitError("This TOML file is nested too deeply.")
        c = self.t[self.i : self.i + 1]
        if c in ('"', "'"):
            kind, value, s, e, lead = self._string()
            rec = {"path": tuple(path), "kind": kind, "start": s, "end": e}
            self.strings.append({**rec, "value": value, "lead": lead})
            check_segment_cap(len(self.strings))
        elif c == "[":
            self.i += 1
            k = 0
            while True:
                self._ws_nl()
                if self.t.startswith("]", self.i):
                    self.i += 1
                    break
                self._value([*path, k], depth + 1)
                k += 1
                self._ws_nl()
                if self.t.startswith(",", self.i):
                    self.i += 1
                elif not self.t.startswith("]", self.i):
                    self._err("expected ',' or ']' in an array")
        elif c == "{":
            self.i += 1
            first = True
            while True:
                self._ws_nl()
                if self.t.startswith("}", self.i):
                    self.i += 1
                    break
                if not first:
                    self._expect(",")
                    self._ws_nl()
                    if self.t.startswith("}", self.i):  # TOML 1.1 trailing comma
                        self.i += 1
                        break
                first = False
                keys = self._key()
                self._expect("=")
                self._ws()
                self._value([*path, *keys], depth + 1)
        else:
            m = _SCALAR.match(self.t, self.i)
            if not m or not m.group(0).strip():
                self._err("expected a value")
            self.i = m.start() + len(m.group(0).rstrip(" \t"))

    def _run(self):
        t = self.t
        current: list = []
        while True:
            self._ws_nl()
            if self.i >= self.n:
                return
            if t.startswith("[[", self.i):
                self.i += 2
                keys = self._key()
                self._expect("]]")
                parent = self._resolve(keys[:-1])
                full = (*parent, keys[-1])
                idx = self._aot.get(full, -1) + 1
                self._aot[full] = idx
                current = [*full, idx]
            elif t.startswith("[", self.i):
                self.i += 1
                keys = self._key()
                self._expect("]")
                current = self._resolve(keys)
            else:
                keys = self._key()
                self._expect("=")
                self._ws()
                self._value([*current, *keys], 0)
            self._eol()


# ---------------------------------------------------------------------------- document


def _lookup(data, path):
    node = data
    for p in path:
        if isinstance(p, int):
            if not isinstance(node, list) or p >= len(node):
                return None, None
        elif not isinstance(node, dict) or p not in node:
            return None, None
        parent, node = node, node[p]
    return parent, node


_ACTION_MAX = 500  # longest template action protected as one token


def _actions(value: str, left: str, right: str) -> list[tuple[int, int]]:
    """Spans of template actions `left ... right` (at most _ACTION_MAX characters
    inside), found with bounded C-speed str.find so hostile text stays linear."""
    spans, pos = [], 0
    while True:
        a = value.find(left, pos)
        if a < 0:
            return spans
        inner = a + len(left)
        b = value.find(right, inner, inner + _ACTION_MAX + len(right))
        if b < 0:
            pos = a + 1
            continue
        spans.append((a, b + len(right)))
        pos = b + len(right)


def _tokenize(value: str, delims: tuple[str, str]) -> tuple[str, dict, str]:
    """(text with point tokens, inline map, text with the actions removed)."""
    out, bare, imap, pos = [], [], {}, 0
    for k, (a, b) in enumerate(_actions(value, *delims), 1):
        out.append(value[pos:a])
        bare.append(value[pos:a])
        out.append(f"⟦x{k}⟧")
        imap[str(k)] = {"point": value[a:b]}
        pos = b
    out.append(value[pos:])
    bare.append(value[pos:])
    return "".join(out), imap, "".join(bare)


def _detokenize(text: str, imap: dict) -> str:
    def sub(m):
        if m.group(1) or m.group(2) != "x":
            return ""  # paired or foreign tokens are dropped (html/docx/xliff tolerance)
        info = imap.get(m.group(3))
        return info["point"] if info else ""

    return _TOKEN_RE.sub(sub, text)


def _key_text(path) -> str:
    parts = []
    for p in path:
        if isinstance(p, int):
            parts.append(f"[{p}]")
        else:
            seg = p if _BARE_KEY.fullmatch(p) else '"' + _escape_basic(p) + '"'
            parts.append(("." if parts else "") + seg)
    return "".join(parts)


class _Doc:
    def __init__(self, data: bytes):
        self.text, self.bom = _decode(data)
        _budget(self.text)
        self.data = _loads(self.text)
        try:
            self.strings = _Scan(self.text).strings
        except RecursionError as exc:
            raise InputLimitError("This TOML file is nested too deeply.") from exc
        self.units: list[dict] = []
        for s in self.strings:
            parent, val = _lookup(self.data, s["path"])
            if val != s["value"]:
                raise ValueError(
                    "This TOML file could not be read consistently "
                    f"(value at {_key_text(s['path'])}); check it with a TOML validator."
                )
            s["parent"] = parent
            self._classify(s)
            if s["unit"]:
                self.units.append(s)
                check_segment_cap(len(self.units))
        nl = self.text.find("\n")
        self.newline = "\r\n" if nl > 0 and self.text[nl - 1] == "\r" else "\n"

    @staticmethod
    def _classify(s: dict) -> None:
        path, parent = s["path"], s["parent"]
        key = path[-1] if path else ""
        is_message = isinstance(key, str) and _is_message(parent)
        lkey = key.lower() if isinstance(key, str) else ""
        style = "toml"
        delims = ("{{", "}}")
        unit = True
        if lkey in _META_KEYS or (is_message and lkey == "description"):
            unit = False
        if is_message and lkey in PLURAL_KEYS:
            style = f"toml:{lkey}"
        if isinstance(parent, dict):
            ld, rd = parent.get("leftdelim"), parent.get("rightdelim")
            if isinstance(ld, str) and isinstance(rd, str) and ld and rd:
                delims = (ld, rd)
        text, imap, bare = _tokenize(s["value"], delims)
        if unit and not any(c.isalnum() for c in bare):
            unit = False
        s.update(unit=unit, style=style, text=text, map=imap, message=is_message)


# ---------------------------------------------------------------------------- rendering


def _escape_basic(s: str) -> str:
    out = []
    for ch in s:
        if ch in _ESCAPE_OUT:
            out.append(_ESCAPE_OUT[ch])
        elif ord(ch) < 0x20 or ord(ch) == 0x7F:
            out.append("\\u%04X" % ord(ch))  # noqa: UP031
        else:
            out.append(ch)
    return "".join(out)


def _escape_ml_basic(s: str, lead: str, nl: str) -> str:
    out = []
    n = len(s)
    i = 0
    while i < n:
        ch = s[i]
        if ch == '"':
            j = i
            while j < n and s[j] == '"':
                j += 1
            run = j - i
            # a run of three quotes would close the string, and quotes at the very end
            # would merge with the delimiter: escape those, keep ordinary ones readable
            out.append('\\"' * run if run >= 3 or j == n else '"' * run)
            i = j
            continue
        if ch == "\\":
            out.append("\\\\")
        elif ch == "\n":
            out.append("\\n" if (i == 0 and not lead) else nl)
        elif ch == "\t":
            out.append("\t")
        elif ch in _ESCAPE_OUT and ch != '"':
            out.append(_ESCAPE_OUT[ch])
        elif ord(ch) < 0x20 or ord(ch) == 0x7F:
            out.append("\\u%04X" % ord(ch))  # noqa: UP031
        else:
            out.append(ch)
        i += 1
    return "".join(out)


def _bad_control(s: str, allow: str) -> bool:
    return any((ord(c) < 0x20 or ord(c) == 0x7F) and c not in allow for c in s)


def _render(s: dict, value: str, file_nl: str) -> str:
    kind, lead = s["kind"], s["lead"]
    value = value.replace("\r\n", "\n")
    nl = lead or file_nl
    if kind == "literal":
        if "'" not in value and not _bad_control(value, "\t"):
            return "'" + value + "'"
        kind = "basic"
    if kind == "basic":
        return '"' + _escape_basic(value) + '"'
    if kind == "ml_literal":
        ok = (
            "'''" not in value
            and not value.endswith("'")
            and not _bad_control(value, "\t\n")
            and not (value.startswith("\n") and not lead)
        )
        if ok:
            return "'''" + lead + value.replace("\n", nl) + "'''"
    return '"""' + lead + _escape_ml_basic(value, lead, nl) + '"""'


# ---------------------------------------------------------------------------- filter


@register
class TomlFilter(Filter):
    fmt = "toml"
    exts = ()

    def parse(self, data: bytes, provider: str | None = None) -> ParseResult:
        doc = _Doc(data)
        units = [
            TextUnit(p_index=i, text=u["text"], style=u["style"], inline_map=u["map"])
            for i, u in enumerate(doc.units)
        ]
        check_unit_texts(u.text for u in units)
        skeleton = {
            "format": "toml",
            "units": [{"p_index": u.p_index, "text": u.text, "style": u.style} for u in units],
        }
        return ParseResult(skeleton=skeleton, units=units)

    def build(self, original: bytes, paragraph_texts: dict) -> bytes:
        doc = _Doc(original)
        edits, expected = [], {}
        for i, u in enumerate(doc.units):
            new = paragraph_texts.get(str(i))
            if new is None or new == u["text"]:
                continue
            value = _detokenize(new, u["map"])
            if value == u["value"]:
                continue
            edits.append((u["start"], u["end"], _render(u, value, doc.newline)))
            expected[u["start"]] = value.replace("\r\n", "\n")
        if not edits:
            return bytes(original)
        text = _splice(doc.text, edits)
        out = doc.bom + text.encode("utf-8")
        _verify(doc, out, expected)
        return out

    def records(self, data: bytes) -> list[dict]:
        """Key-based view (Batch 9 keys import/export): one record per string value in
        file order, with the KeyValueRecord field names from kv_resource. Message
        tables fold their plural categories into `plural_forms` on the `other` record
        and carry `description` as the comment."""
        doc = _Doc(data)
        unit_index = {id(u): i for i, u in enumerate(doc.units)}
        out = []
        for s in doc.strings:
            path, parent = s["path"], s["parent"]
            rec = {
                "key": _key_text(path),
                "value": s["value"],
                "comment": "",
                "context": _key_text(path[:-1]) if len(path) > 1 else "",
                "plural_forms": None,
                "max_length": None,
                "unit_index": unit_index.get(id(s)),
            }
            if s["message"]:
                desc = parent.get("description")
                rec["comment"] = desc if isinstance(desc, str) else ""
                if path[-1] == "other":
                    rec["plural_forms"] = {
                        k: parent[k] for k in PLURAL_KEYS if isinstance(parent.get(k), str)
                    }
            out.append(rec)
        return out


def _splice(text: str, edits: list[tuple[int, int, str]]) -> str:
    """Replace non-overlapping spans in one linear pass (re-slicing the whole text per
    edit is quadratic: 50,000 edits on a few MB would copy gigabytes)."""
    parts, pos = [], 0
    for a, b, raw in sorted(edits):
        parts.append(text[pos:a])
        parts.append(raw)
        pos = b
    parts.append(text[pos:])
    return "".join(parts)


def _verify(doc: _Doc, out: bytes, expected: dict) -> None:
    """The rebuilt file must still be valid TOML, hold the same string values at the
    same places, and carry each translation exactly (never ship a broken file)."""
    try:
        new = _Doc(out)
    except (ValueError, InputLimitError) as exc:
        raise ValueError(f"The translated TOML file could not be rebuilt safely: {exc}") from exc
    if len(new.strings) != len(doc.strings):
        raise ValueError("The translated TOML file could not be rebuilt safely (value count).")
    for old, cur in zip(doc.strings, new.strings, strict=True):
        if old["path"] != cur["path"]:
            raise ValueError("The translated TOML file could not be rebuilt safely (key paths).")
        want = expected.get(old["start"], old["value"])
        if cur["value"] != want:
            raise ValueError(
                "The translated TOML file could not be rebuilt safely "
                f"(value at {_key_text(old['path'])})."
            )
