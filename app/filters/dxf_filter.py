"""AutoCAD DXF (ASCII) filter.

parse(): the drawing text of an ASCII DXF file becomes translatable units, in file order,
from the ENTITIES and BLOCKS sections only:
  - TEXT: group 1 (the text string),
  - MTEXT: groups 3 (250-character chunks) + 1 (the last chunk), joined into ONE unit,
  - ATTRIB: group 1 (the value); ATTDEF: group 1 (the default value) and group 3 (the
    prompt); a multiline attribute's embedded MTEXT (the 1/3 groups after group 101) is
    its own unit,
  - DIMENSION: group 1 (the text override; empty, a single space and a bare `<>` are not
    text), with `<>` (the measurement) and `[]` (alternate units) protected,
  - MULTILEADER / MLEADER: group 304 (the MText content).
Layer names (8), block names (2), tags (2), style names (7, DIMENSION 3), handles (5,
330, 360...), XDATA (1000+) and every other group are never touched. HEADER, TABLES,
CLASSES and OBJECTS are never units.

Inline codes are protected tokens from services/segment: MTEXT format codes (\\P, \\X,
\\~ shown as a no-break space, \\L \\l \\O \\o \\K \\k, \\f...; \\F...; \\H...; \\C...;
\\c...; \\T...; \\Q...; \\W...; \\A...; \\p...; and a whole \\S...; stack) are point
tokens (⟦x1⟧); a balanced `{...}` group is a paired token (⟦g2⟧...⟦/g2⟧); `\\\\`, `\\{`,
`\\}` are the literal characters. Special characters %%d %%c %%p %%% %%nnn (and the
%%u / %%o toggles) and ^-control sequences are point tokens. `\\U+XXXX` (and
`\\M+NXXXX` of a known Asian code page) are shown as the character itself.

build(): only units whose text changed are rewritten, as a byte splice of the group-code
value lines: every other byte of the file is identical. A changed MTEXT is re-chunked
into group-3 pieces of at most 250 characters plus a final group 1 (never splitting an
escape), written where the first original chunk stood.

Encodings (#509 stays strict for shared text filters; DXF decodes explicitly):
AC1021 (AutoCAD 2007) and later are UTF-8. Earlier files use the $DWGCODEPAGE code page
(ANSI_1252 when the header has none); a translated character that the code page cannot
hold is written as \\U+XXXX, as AutoCAD itself does. A file with no $ACADVER (minimal R12
writers) is read as UTF-8 when it is valid UTF-8 with non-ASCII bytes, else ANSI_1252.
Binary DXF is refused with a plain reason.

Specs: Autodesk DXF Reference (help.autodesk.com/view/OARX/2024/ENU/?guid=GUID-235B22E0-
A567-4CF6-92D3-38A2306D73F3), MTEXT entity (GUID-5E5DB93B-F8D3-4433-ADF7-E92E250D2BAB),
HEADER variables (GUID-A85E8E67-27CD-4C59-BE61-4DC9FADBE74A), MTEXT format codes
(AutoCAD help GUID-7D8BB40F-5C4E-4AE5-BD75-9ED7112E5967).
"""

import re

from app.filters.base import (
    Filter,
    InputLimitError,
    ParseResult,
    TextUnit,
    UnsupportedEncodingError,
    check_segment_cap,
    check_unit_texts,
    note_source_encoding,
    register,
)
from app.services.segment import _PLACEHOLDER_RE, ph_close, ph_open, ph_point

# ---------------------------------------------------------------------------- encoding

_BINARY_SENTINEL = b"AutoCAD Binary DXF"
# $DWGCODEPAGE values (Windows ANSI pages, the DOS pages older releases wrote) -> codec.
_CODEPAGES = {
    "ANSI_874": "cp874",
    "ANSI_932": "cp932",
    "ANSI_936": "gbk",
    "ANSI_949": "cp949",
    "ANSI_950": "cp950",
    "ANSI_1250": "cp1250",
    "ANSI_1251": "cp1251",
    "ANSI_1252": "cp1252",
    "ANSI_1253": "cp1253",
    "ANSI_1254": "cp1254",
    "ANSI_1255": "cp1255",
    "ANSI_1256": "cp1256",
    "ANSI_1257": "cp1257",
    "ANSI_1258": "cp1258",
    "DOS437": "cp437",
    "DOS850": "cp850",
    "DOS852": "cp852",
    "DOS855": "cp855",
    "DOS857": "cp857",
    "DOS860": "cp860",
    "DOS861": "cp861",
    "DOS863": "cp863",
    "DOS864": "cp864",
    "DOS865": "cp865",
    "DOS866": "cp866",
    "DOS869": "cp869",
    "DOS932": "cp932",
    "UTF8": "utf-8",
}
# \M+N: MIF (multibyte interchange) code page numbers.
_MIF = {"1": "cp932", "2": "cp950", "3": "cp949", "5": "gbk"}


def _refuse(msg: str):
    raise UnsupportedEncodingError(
        msg + " Save the drawing as an ASCII DXF (AutoCAD 2007 or later is UTF-8)."
    )


def _malformed(msg: str):
    raise InputLimitError("This is not a valid ASCII DXF file: " + msg)


# ---------------------------------------------------------------------------- pair walk

# One group: code line, newline, value line, newline (or end of file).
_PAIR_RE = re.compile(rb"[ \t]*(-?\d{1,4})[ \t]*(\r\n|\n|\r)([^\r\n]*)(\r\n|\n|\r|\Z)")
_TRAIL_RE = re.compile(rb"[ \t\r\n\x1a\x00]*\Z")

_TEXTISH = {"TEXT", "MTEXT", "ATTRIB", "ATTDEF", "DIMENSION", "MULTILEADER", "MLEADER"}


class _Unit:
    __slots__ = ("kind", "pairs", "style")

    def __init__(self, kind, style):
        self.kind = kind  # "text" (TEXT semantics), "mtext" (MTEXT codes), "dim" (MTEXT + <>)
        self.style = style
        self.pairs = []  # [(code, code_start, code_end, val_start, val_end, end)]


def _line_budget(data: bytes) -> None:
    from app import config

    limit = 200 * config.MAX_DOCUMENT_SEGMENTS
    if data.count(b"\n") + data.count(b"\r") > 2 * limit:
        raise InputLimitError(
            f"This DXF file has more than {limit} group codes (200 x MAX_DOCUMENT_SEGMENTS). "
            "Split the drawing or export only the layouts you need."
        )


def _walk(data: bytes):
    """-> (units in file order, acadver str, dwgcodepage str). Raw byte spans only."""
    if data.lstrip()[: len(_BINARY_SENTINEL)] == _BINARY_SENTINEL:
        raise InputLimitError(
            "This is a binary DXF file. Only ASCII DXF can be translated: in AutoCAD use "
            "SAVEAS, pick DXF and set the format to ASCII (not Binary)."
        )
    _line_budget(data)
    pos = 3 if data[:3] == b"\xef\xbb\xbf" else 0
    n = len(data)
    units: list[_Unit] = []
    section = None
    expect_section_name = False
    header_var = None
    acadver = codepage = None
    entity = None
    after101 = False
    mt_unit = None  # the open MTEXT-chunk unit of the current entity part
    first = True
    while pos < n:
        m = _PAIR_RE.match(data, pos)
        if m is None:
            if _TRAIL_RE.match(data, pos):
                break
            line = data.count(b"\n", 0, pos) + 1
            _malformed(f"line {line} is not a group code followed by a value.")
        code = int(m.group(1))
        val = m.group(3)
        if first:
            first = False
            if code not in (0, 999):
                _malformed("it does not start with a group code 0 or 999.")
        span = (code, m.start(1), m.end(1), m.start(3), m.end(3), m.end())
        pos = m.end()
        if code == 0:
            v = val.strip()
            expect_section_name = v == b"SECTION"
            if v in (b"SECTION", b"ENDSEC", b"EOF"):
                section = None if v != b"SECTION" else section
                entity = None
            elif section in (b"ENTITIES", b"BLOCKS"):
                name = v.decode("ascii", "replace").upper()
                entity = name if name in _TEXTISH else None
            else:
                entity = None
            after101 = False
            mt_unit = None
            if v == b"EOF":
                break
            continue
        if expect_section_name and code == 2:
            section = val.strip().upper()
            expect_section_name = False
            continue
        if section == b"HEADER":
            if code == 9:
                header_var = val.strip().upper()
            elif header_var == b"$ACADVER" and code == 1:
                acadver = val.strip().decode("ascii", "replace").upper()
            elif header_var == b"$DWGCODEPAGE" and code == 3:
                codepage = val.strip().decode("ascii", "replace").upper()
            continue
        if entity is None or code >= 1000:
            continue
        if code == 101:
            after101 = True
            mt_unit = None
            continue
        if entity == "MTEXT" or (after101 and entity in ("ATTRIB", "ATTDEF")):
            if after101 and entity == "MTEXT":
                continue  # MTEXT embedded column data: no text
            if code in (1, 3):
                if mt_unit is None:
                    mt_unit = _Unit("mtext", "dxf:mtext")
                    units.append(mt_unit)
                    check_segment_cap(len(units))
                mt_unit.pairs.append(span)
            continue
        unit = None
        if entity == "TEXT" and code == 1:
            unit = _Unit("text", "dxf:text")
        elif entity == "ATTRIB" and code == 1:
            unit = _Unit("text", "dxf:attrib")
        elif entity == "ATTDEF" and code == 1:
            unit = _Unit("text", "dxf:attdef")
        elif entity == "ATTDEF" and code == 3:
            unit = _Unit("text", "dxf:prompt")
        elif entity == "DIMENSION" and code == 1:
            unit = _Unit("dim", "dxf:dimension")
        elif entity in ("MULTILEADER", "MLEADER") and code == 304:
            unit = _Unit("mtext", "dxf:mleader")
        if unit is not None:
            unit.pairs.append(span)
            units.append(unit)
            check_segment_cap(len(units))
    return units, acadver, codepage


def _codec(data: bytes, acadver, codepage) -> str:
    ver = None
    if acadver and re.fullmatch(r"AC\d{4}", acadver):
        ver = int(acadver[2:])
    if ver is not None and ver >= 1021:
        return "utf-8"
    if codepage:
        codec = _CODEPAGES.get(codepage)
        if codec is None:
            _refuse(
                f"This DXF file declares code page {codepage} ($DWGCODEPAGE), "
                "which is not supported."
            )
        return codec
    if ver is None and not data.isascii():
        try:
            data.decode("utf-8")
            return "utf-8"
        except UnicodeDecodeError:
            pass
    return "cp1252"


def _decode_value(raw: bytes, codec: str, start: int) -> str:
    try:
        return raw.decode(codec)
    except UnicodeDecodeError as exc:
        _refuse(
            f"Byte 0x{raw[exc.start]:02x} at position {start + exc.start} is not valid in this "
            f"drawing's encoding ({codec})."
        )
    raise AssertionError("unreachable")


# ---------------------------------------------------------------------------- inline codes

_HEX4 = re.compile(r"[0-9A-Fa-f]{4}")
_SEMI_CODES = set("ACcFfHpQTW")  # \X...; codes
_BARE_CODES = set("PXNLlOoKk")  # codes with no argument


def _unicode_escape(s: str, i: int):
    """At s[i] == '\\': a \\U+XXXX or \\M+NXXXX escape -> (char or None, length)."""
    d = s[i + 1 : i + 3]
    if d in ("U+", "u+") and _HEX4.fullmatch(s[i + 3 : i + 7] or ""):
        cp = int(s[i + 3 : i + 7], 16)
        if 0xD800 <= cp <= 0xDFFF:
            # a surrogate pair written as two escapes
            if 0xD800 <= cp <= 0xDBFF and s[i + 7 : i + 10] in ("\\U+", "\\u+"):
                lo = s[i + 10 : i + 14]
                if _HEX4.fullmatch(lo or "") and 0xDC00 <= int(lo, 16) <= 0xDFFF:
                    full = 0x10000 + ((cp - 0xD800) << 10) + (int(lo, 16) - 0xDC00)
                    return chr(full), 14
            return None, 7
        return chr(cp), 7
    if d in ("M+", "m+") and s[i + 3 : i + 4] in _MIF and _HEX4.fullmatch(s[i + 4 : i + 8] or ""):
        try:
            ch = bytes.fromhex(s[i + 4 : i + 8]).decode(_MIF[s[i + 3]])
        except (UnicodeDecodeError, ValueError):
            return None, 8
        return (ch, 8) if len(ch) == 1 else (None, 8)
    return None, 0


def _lex(s: str, kind: str) -> list[tuple[str, str]]:
    """[("t", text) | ("x", raw) | ("o", raw) | ("c", raw)]; o/c are brace candidates."""
    mtext = kind != "text"
    items: list[tuple[str, str]] = []
    buf: list[str] = []

    def flush():
        if buf:
            items.append(("t", "".join(buf)))
            buf.clear()

    def tok(k, raw):
        flush()
        items.append((k, raw))

    i, n = 0, len(s)
    while i < n:
        c = s[i]
        if c == "\\" and i + 1 < n:
            ch, ln = _unicode_escape(s, i)
            if ln:
                if ch is not None and ch.isprintable():
                    buf.append(ch)
                else:
                    tok("x", s[i : i + ln])
                i += ln
                continue
            if not mtext:
                buf.append(c)  # TEXT: a backslash is a literal character
                i += 1
                continue
            d = s[i + 1]
            if d in "\\{}":
                buf.append(d)
                i += 2
            elif d == "~":
                buf.append("\u00a0")
                i += 2
            elif d == "S":
                j = i + 2
                while j < n and s[j] != ";":
                    j += 2 if s[j] == "\\" else 1
                end = min(j + 1, n)
                tok("x", s[i:end])
                i = end
            elif d in _SEMI_CODES:
                j = s.find(";", i + 2)
                end = n if j < 0 else j + 1
                tok("x", s[i:end])
                i = end
            else:  # bare codes (\P, \X, \L ...) and any unknown \c: two characters
                tok("x", s[i : i + 2])
                i += 2
            continue
        if c == "\\":  # trailing backslash
            if mtext:
                tok("x", c)
            else:
                buf.append(c)
            i += 1
            continue
        if c == "%" and s.startswith("%%", i):
            e = s[i + 2 : i + 3]
            if e and e in "dDcCpPuUoOkK%":
                tok("x", s[i : i + 3])
                i += 3
                continue
            if re.fullmatch(r"\d{3}", s[i + 2 : i + 5]):
                tok("x", s[i : i + 5])
                i += 5
                continue
        if c == "^" and i + 1 < n:
            tok("x", s[i : i + 2])
            i += 2
            continue
        if mtext and c == "{":
            tok("o", c)
            i += 1
            continue
        if mtext and c == "}":
            tok("c", c)
            i += 1
            continue
        if kind == "dim" and s.startswith(("<>", "[]"), i):
            tok("x", s[i : i + 2])
            i += 2
            continue
        buf.append(c)
        i += 1
    flush()
    # pair the braces; an unmatched one is a point token
    stack, pairs = [], {}
    for idx, (k, _raw) in enumerate(items):
        if k == "o":
            stack.append(idx)
        elif k == "c" and stack:
            pairs[stack.pop()] = idx
    closes = {v: k for k, v in pairs.items()}
    out = []
    for idx, (k, raw) in enumerate(items):
        if (k == "o" and idx not in pairs) or (k == "c" and idx not in closes):
            k = "x"
        out.append((k, raw))
    return out


def _unit_text(items) -> tuple[str, dict, bool]:
    parts, imap, n, alpha = [], {}, 0, False
    open_ids: list[int] = []
    for k, raw in items:
        if k == "t":
            parts.append(raw)
            alpha = alpha or any(ch.isalpha() for ch in raw)
        elif k == "x":
            n += 1
            imap[str(n)] = {"point": raw}
            parts.append(ph_point("x", n))
        elif k == "o":
            n += 1
            imap[str(n)] = {"open": raw, "close": "}"}
            open_ids.append(n)
            parts.append(ph_open("g", n))
        else:
            parts.append(ph_close("g", open_ids.pop()))
    return "".join(parts), imap, alpha


_CTRL_RE = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")


def _escape(s: str, kind: str) -> str:
    s = _CTRL_RE.sub("", s.replace("\r\n", "\n").replace("\r", "\n"))
    s = re.sub(r"%{2,}", lambda m: "%%%" * len(m.group()), s)  # %%% is one literal %
    s = s.replace("^", "^ ")
    if kind == "text":
        return s.replace("\n", " ").replace("\t", " ")
    s = s.replace("\\", "\\\\").replace("{", "\\{").replace("}", "\\}")
    return s.replace("\u00a0", "\\~").replace("\n", "\\P").replace("\t", "^I")


def _render(text: str, imap: dict, kind: str) -> str:
    present = {m.group(0) for m in _PLACEHOLDER_RE.finditer(text)}
    out, pos = [], 0
    for m in _PLACEHOLDER_RE.finditer(text):
        out.append(_escape(text[pos : m.start()], kind))
        pos = m.end()
        tokn = m.group(0)
        inner = tokn[1:-1]
        closing = inner.startswith("/")
        body = inner[1:] if closing else inner
        info = imap.get(body[1:]) if body[:1] in "xg" else None
        if not info:
            continue  # unknown token dropped (same tolerance as rc/html/docx/xliff)
        if "point" in info:
            out.append(info["point"])
            continue
        # a paired brace survives only with its partner, so braces stay balanced
        partner = ph_open("g", int(body[1:])) if closing else ph_close("g", int(body[1:]))
        if partner in present:
            out.append(info["close"] if closing else info["open"])
    out.append(_escape(text[pos:], kind))
    return "".join(out)


def _fit_codec(s: str, codec: str) -> str:
    """Characters the drawing's code page cannot hold become \\U+XXXX (two escapes, a
    surrogate pair, above U+FFFF), as AutoCAD writes them. UTF-8 drawings keep every char."""
    if codec == "utf-8":
        return s
    try:
        s.encode(codec)
        return s
    except UnicodeEncodeError:
        pass
    out = []
    for ch in s:
        try:
            ch.encode(codec)
            out.append(ch)
        except UnicodeEncodeError:
            cp = ord(ch)
            if cp > 0xFFFF:
                cp -= 0x10000
                out.append(f"\\U+{0xD800 + (cp >> 10):04X}\\U+{0xDC00 + (cp & 0x3FF):04X}")
            else:
                out.append(f"\\U+{cp:04X}")
    return "".join(out)


def _encode_value(s: str, codec: str) -> bytes:
    return s.encode(codec, "surrogatepass" if codec == "utf-8" else "strict")


_ATOM_RE = re.compile(r"\\U\+[0-9A-Fa-f]{4}|\\M\+\d[0-9A-Fa-f]{4}|\\.|\^.|.", re.S)
CHUNK = 250


def _chunks(s: str) -> list[str]:
    """Split an MTEXT value into pieces of at most 250 characters, never inside an escape."""
    if len(s) < CHUNK:
        return [s]
    out, cur, size = [], [], 0
    for m in _ATOM_RE.finditer(s):
        a = m.group()
        if size + len(a) > CHUNK:
            out.append("".join(cur))
            cur, size = [], 0
        cur.append(a)
        size += len(a)
    out.append("".join(cur))
    if len(out[-1]) >= CHUNK:  # the last group 1 holds fewer than 250 characters
        out.append("")
    return out


# ---------------------------------------------------------------------------- filter


def _load(data: bytes):
    data = bytes(data)
    units, acadver, codepage = _walk(data)
    codec = _codec(data, acadver, codepage)
    parsed = []
    for u in units:
        if u.kind == "mtext" and len(u.pairs) > 1:
            ordered = [p for p in u.pairs if p[0] == 3] + [p for p in u.pairs if p[0] != 3]
        else:
            ordered = u.pairs
        raw = b"".join(data[p[3] : p[4]] for p in ordered)
        value = _decode_value(raw, codec, ordered[0][3])
        if not value.strip():
            continue
        text, imap, alpha = _unit_text(_lex(value, u.kind))
        if not alpha:
            continue
        parsed.append({"unit": u, "text": text, "map": imap})
    return parsed, codec, acadver


def _code_line(template: bytes, code: int) -> bytes:
    if template != template.lstrip():
        return str(code).encode().rjust(len(template))
    return str(code).encode()


@register
class DxfFilter(Filter):
    fmt = "dxf"
    exts = ()

    def parse(self, data: bytes, provider: str | None = None) -> ParseResult:
        parsed, codec, acadver = _load(data)
        units = [
            TextUnit(p_index=i, text=p["text"], style=p["unit"].style, inline_map=p["map"])
            for i, p in enumerate(parsed)
        ]
        check_unit_texts(u.text for u in units)
        skeleton = {
            "format": "dxf",
            "acadver": acadver or "",
            "units": [{"p_index": u.p_index, "text": u.text, "style": u.style} for u in units],
        }
        return ParseResult(skeleton=note_source_encoding(skeleton, codec), units=units)

    def build(self, original: bytes, paragraph_texts: dict) -> bytes:
        data = bytes(original)
        parsed, codec, _acadver = _load(data)
        edits: list[tuple[int, int, bytes]] = []
        for i, p in enumerate(parsed):
            new = paragraph_texts.get(str(i))
            if new is None or new == p["text"]:
                continue
            u = p["unit"]
            value = _fit_codec(_render(new, p["map"], u.kind), codec)
            if u.style != "dxf:mtext":
                # single-value groups: replace the value line only
                code, _cs, _ce, vs, ve, _end = u.pairs[0]
                edits.append((vs, ve, _encode_value(value, codec)))
                continue
            # MTEXT: re-chunk, written where the first chunk stood; other chunks removed
            first = min(u.pairs, key=lambda q: q[1])
            line_start = data.rfind(b"\n", 0, first[1]) + 1
            line_start = max(line_start, data.rfind(b"\r", 0, first[1]) + 1)
            template = data[line_start : first[2]]
            nl1 = data[first[2] : first[3]].lstrip(b" \t")
            nl2 = data[first[4] : first[5]] or nl1
            pieces = _chunks(value)
            block = bytearray()
            for k, piece in enumerate(pieces):
                code = 1 if k == len(pieces) - 1 else 3
                block += _code_line(template, code) + nl1 + _encode_value(piece, codec)
                block += nl2 if k < len(pieces) - 1 or first[5] > first[4] else b""
            edits.append((line_start, first[5], bytes(block)))
            for q in u.pairs:
                if q is first:
                    continue
                ls = max(data.rfind(b"\n", 0, q[1]), data.rfind(b"\r", 0, q[1])) + 1
                edits.append((ls, q[5], b""))
        if not edits:
            return data
        out, pos = [], 0
        for a, b, val in sorted(edits, key=lambda e: e[0]):
            out += [data[pos:a], val]
            pos = b
        out.append(data[pos:])
        return b"".join(out)
