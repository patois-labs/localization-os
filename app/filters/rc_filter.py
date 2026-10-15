"""Windows resource script (.rc) filter.

parse(): the human text of a resource script becomes translatable units, in file order:
  - STRINGTABLE strings,
  - DIALOG / DIALOGEX captions and control texts (LTEXT, PUSHBUTTON, GROUPBOX, CONTROL
    and the other text controls; never class names, font faces or icon/bitmap names),
  - MENU / MENUEX items and popups (MENUITEM SEPARATOR and empty MENUEX items skipped),
  - VERSIONINFO string values that people read (FileDescription, ProductName, Comments,
    LegalCopyright, LegalTrademarks); version numbers, file names and company names stay.
Everything else (ACCELERATORS, RCDATA, TEXTINCLUDE, DLGINIT, ICON/BITMAP file names, user
types) is skipped. Preprocessor lines are skipped as text: `#include`, `#define`, `#if` are
NEVER executed, followed or evaluated (a string inside an `#if 0` region is still read).

Escapes: `""` and `\\"` are a quote, `\\\\` a backslash, `\\`+newline a line continuation
(the next line's leading whitespace is dropped, as rc.exe does); `\\'` and `\\?` are not
escapes (a narrow string keeps the backslash, a wide one drops it).
Control escapes (`\\n`, `\\t`, `\\r`, `\\a`, `\\0`, octal, narrow `\\xhh`) and printf /
FormatMessage placeholders (`%s`, `%1!d!`, `%%`) become point tokens (⟦x1⟧) from
services/segment, rebuilt from the original bytes. In an L"" string, `\\xhhhh` of a
printable character is shown as that character.

build(): only units whose text changed are rewritten (the literal is re-escaped in place,
its L prefix kept); every other byte is identical. For menu items and dialog controls whose
source carries exactly one `&` accelerator, the target keeps exactly one: extra single `&`
become `&&`, and a missing one is put before the same letter or appended as `(&X)`.

Encodings (#509 stays strict for the shared text filters; .rc decodes explicitly):
UTF-16 LE/BE with BOM, BOM-less UTF-16 LE, UTF-8 with BOM, then a `#pragma code_page(N)`
the file declares (65001 = UTF-8, or a common Windows/ISO code page), then UTF-8, then
Windows-1252 (rc.exe's usual ANSI default). The build writes the same encoding back; when
a translation does not fit an ANSI code page the file is written as UTF-8 and every
`#pragma code_page` becomes 65001 (one is added at the top when the file had none).

build_localized(): also rewrites the LANGUAGE statements to the target's Windows LANGID
(`LANGUAGE 0x07, 0x01`) when every LANGUAGE statement in the file names the same language
(a multi-language script is left alone), plus the VERSIONINFO StringFileInfo block id and
the VarFileInfo Translation language.

Specs: learn.microsoft.com/windows/win32/menurc/ (stringtable-resource, dialogex-resource,
menu-resource, menuex-resource, versioninfo-resource, language-statement,
pragma-directives, control-control).
"""

import bisect
import locale as _pylocale
import re

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
    note_source_encoding,
    register,
)
from app.services.segment import _PLACEHOLDER_RE, is_point_kind, ph_free, ph_point

# ---------------------------------------------------------------------------- encoding

_PRAGMA_RE = re.compile(rb"(?im)^[ \t]*#[ \t]*pragma[ \t]+code_page[ \t]*\(([^)\r\n]{0,20})\)")
_PRAGMA_TEXT_RE = re.compile(
    r"(?im)^([ \t]*#[ \t]*pragma[ \t]+code_page[ \t]*\()([^)\r\n]{0,20})(\))"
)
# Code pages a `#pragma code_page(N)` may name (the #509 legacy set, as Windows numbers).
_CODEPAGES = {
    "65001": "utf-8",
    "1250": "cp1250",
    "1251": "cp1251",
    "1252": "cp1252",
    "1253": "cp1253",
    "1254": "cp1254",
    "1255": "cp1255",
    "1256": "cp1256",
    "1257": "cp1257",
    "1258": "cp1258",
    "932": "cp932",
    "936": "gbk",
    "949": "cp949",
    "950": "cp950",
    "874": "cp874",
    "437": "cp437",
    "850": "cp850",
    "866": "cp866",
    "20866": "koi8_r",
    "21866": "koi8_u",
    "28591": "latin-1",
    "28592": "iso8859-2",
    "28595": "iso8859-5",
    "28597": "iso8859-7",
    "28599": "iso8859-9",
    "28605": "iso8859-15",
    "54936": "gb18030",
    "51949": "euc_kr",
    "20932": "euc_jp",
}


def _refuse(msg: str):
    raise UnsupportedEncodingError(
        msg + " Re-save the resource script as UTF-16 (Unicode) or UTF-8."
    )


def _decode(data: bytes) -> tuple[str, str]:
    """(text, encoding label). Labels: utf-16-le-bom, utf-16-be-bom, utf-16-le, utf-8-bom,
    utf-8, or a Python codec name for an ANSI code page. Raises UnsupportedEncodingError."""
    data = bytes(data)
    if data[:2] == b"\xff\xfe":
        try:
            return data[2:].decode("utf-16-le"), "utf-16-le-bom"
        except UnicodeDecodeError:
            _refuse("This .rc file starts with a UTF-16 byte-order mark but is not valid UTF-16.")
    if data[:2] == b"\xfe\xff":
        try:
            return data[2:].decode("utf-16-be"), "utf-16-be-bom"
        except UnicodeDecodeError:
            _refuse("This .rc file starts with a UTF-16 byte-order mark but is not valid UTF-16.")
    if data[:3] == b"\xef\xbb\xbf":
        try:
            return data[3:].decode("utf-8"), "utf-8-bom"
        except UnicodeDecodeError:
            _refuse("This .rc file starts with a UTF-8 byte-order mark but is not valid UTF-8.")
    head = data[:4096]
    if len(head) >= 4 and len(data) % 2 == 0:
        odd = head[1::2]
        if odd.count(0) >= 0.9 * len(odd) and head[0::2].count(0) < 0.1 * len(odd):
            try:
                return data.decode("utf-16-le"), "utf-16-le"
            except UnicodeDecodeError:
                pass
    pages = {m.group(1).strip().upper() for m in _PRAGMA_RE.finditer(data)}
    pages.discard(b"DEFAULT")
    if len(pages) > 1:
        _refuse(
            "This .rc file switches between several code pages (#pragma code_page); "
            "one file can only be read in one encoding."
        )
    if pages:
        num = pages.pop().decode("ascii", "replace")
        codec = _CODEPAGES.get(num)
        if codec is None:
            _refuse(f"This .rc file declares code page {num}, which is not supported.")
        try:
            return data.decode(codec), ("utf-8" if codec == "utf-8" else codec)
        except UnicodeDecodeError as exc:
            _refuse(
                f"This .rc file declares code page {num} but byte 0x{data[exc.start]:02x} at "
                f"position {exc.start} is not valid in it."
            )
    try:
        return data.decode("utf-8"), "utf-8"
    except UnicodeDecodeError:
        pass
    if _looks_binary(data):
        _refuse("This .rc file is not valid UTF-8 or UTF-16 and looks like a binary file.")
    try:
        return data.decode("cp1252"), "cp1252"
    except UnicodeDecodeError as exc:
        _refuse(
            "This .rc file is not valid UTF-8 or UTF-16 and could not be read as "
            f"Windows-1252 either (byte 0x{data[exc.start]:02x} at position {exc.start})."
        )
    raise AssertionError("unreachable")


def _encode(text: str, enc: str) -> bytes:
    if enc == "utf-16-le-bom":
        return b"\xff\xfe" + text.encode("utf-16-le", "surrogatepass")
    if enc == "utf-16-be-bom":
        return b"\xfe\xff" + text.encode("utf-16-be", "surrogatepass")
    if enc == "utf-16-le":
        return text.encode("utf-16-le", "surrogatepass")
    if enc == "utf-8-bom":
        return b"\xef\xbb\xbf" + text.encode("utf-8", "surrogatepass")
    if enc == "utf-8":
        if not text.isascii() and not _PRAGMA_TEXT_RE.search(text):
            # rc.exe reads a BOM-less file as ANSI unless told otherwise
            text = "#pragma code_page(65001)" + ("\r\n" if "\r\n" in text else "\n") + text
        return text.encode("utf-8", "surrogatepass")
    try:
        return text.encode(enc)
    except UnicodeEncodeError:
        pass
    # The translation does not fit the ANSI code page: write UTF-8 and say so to rc.exe.
    if _PRAGMA_TEXT_RE.search(text):
        text = _PRAGMA_TEXT_RE.sub(lambda m: m.group(1) + "65001" + m.group(3), text)
    else:
        nl = "\r\n" if "\r\n" in text else "\n"
        text = "#pragma code_page(65001)" + nl + text
    return text.encode("utf-8", "surrogatepass")


# ---------------------------------------------------------------------------- lexer

_TOKEN_RE = re.compile(
    r"""(?P<ws>[ \t\r\n\f\v]+)
      |(?P<lc>//[^\n]*)
      |(?P<bc>/\*(?:[^*]|\*(?!/))*+(?:\*/|\Z))
      |(?P<str>L?"(?:[^"\\\n]|\\(?:\r?\n|.)|"")*+")
      |(?P<num>0[xX][0-9a-fA-F]+[lLuU]*|\d+[lLuU]*)
      |(?P<id>[A-Za-z_][A-Za-z0-9_]*)
      |(?P<p>.)""",
    re.X | re.S,
)
_PREPROC_LINE_RE = re.compile(r"(?:\\\r?\n|[^\n])*")


class _Tok:
    __slots__ = ("end", "kind", "start", "up", "val")

    def __init__(self, kind, start, end, val):
        self.kind, self.start, self.end, self.val = kind, start, end, val
        self.up = val.upper() if kind == "id" else ""


def _lex(text: str) -> list[_Tok]:
    toks: list[_Tok] = []
    pos, n = 0, len(text)
    line_start = True  # only spaces/tabs since the last newline
    while pos < n:
        ch = text[pos]
        if ch == "#" and line_start:
            m = _PREPROC_LINE_RE.match(text, pos)
            pos = m.end()  # the whole directive line (with continuations) is skipped
            continue
        m = _TOKEN_RE.match(text, pos)
        kind = m.lastgroup
        end = m.end()
        if kind == "ws":
            seg = m.group()
            nl = seg.rfind("\n")
            if nl != -1:
                line_start = seg[nl + 1 :].strip(" \t") == ""
            pos = end
            continue
        line_start = False
        if kind in ("lc", "bc"):
            pos = end
            continue
        if kind == "p" and ch == '"':
            # an unterminated string: skip the rest of the line (rc.exe refuses it)
            nl = text.find("\n", pos)
            pos = n if nl == -1 else nl
            continue
        toks.append(_Tok(kind, pos, end, m.group()))
        pos = end
    return toks


# ---------------------------------------------------------------------------- strings

_PLACEHOLDER_SRC_RE = re.compile(
    r"%%|%[1-9]\d?(?:![^!\n]{1,12}!)?|%[-+#0]*(?:\d+|\*)?(?:\.(?:\d+|\*))?"
    r"(?:hh|h|ll|l|L|z|j|t|I64|I32|I|w)?[diouxXeEfFgGaAcCsSpn]"
)
_FREE_PH = re.compile(r"%[1-9]")
# rc.exe escapes (resinator literals.zig mirrors rc.exe): \\ and \" only; \' and \? are not.
_SIMPLE_ESC = {"\\": "\\", '"': '"'}


def _literal_pieces(lit: str) -> tuple[list[tuple[str, str]], bool]:
    """Split a string token (with quotes and optional L) into [("t", text) | ("x", raw)]."""
    wide = lit[0] in "Lu"
    body = lit[2:-1] if wide else lit[1:-1]
    pieces: list[tuple[str, str]] = []
    buf: list[str] = []

    def flush_text():
        if buf:
            pieces.append(("t", "".join(buf)))
            buf.clear()

    i, n = 0, len(body)
    while i < n:
        c = body[i]
        if c == '"':  # "" inside a literal is one quote
            buf.append('"')
            i += 2
            continue
        if c != "\\" or i + 1 >= n:
            buf.append(c)
            i += 1
            continue
        d = body[i + 1]
        if d in _SIMPLE_ESC:
            buf.append(_SIMPLE_ESC[d])
            i += 2
            continue
        if d in "'?":
            # not an RC escape: a narrow string keeps the backslash, a wide one drops it
            buf.append(d if wide else "\\" + d)
            i += 2
            continue
        if d == "\n" or (d == "\r" and body[i + 2 : i + 3] == "\n"):
            # line continuation: rc.exe also drops the next line's leading whitespace
            i += 2 if d == "\n" else 3
            while i < n and body[i] in "\r\n\t \x0b\x0c\xa0":
                i += 1
            continue
        if d in "xX":
            m = re.match(r"[0-9a-fA-F]{1,4}" if wide else r"[0-9a-fA-F]{1,2}", body[i + 2 :])
            raw = body[i : i + 2 + (m.end() if m else 0)]
            if wide and m:
                ch = chr(int(m.group(), 16))
                if ch.isprintable() and not (0xD800 <= ord(ch) <= 0xDFFF):
                    buf.append(ch)
                    i += len(raw)
                    continue
        elif d in "01234567":
            m = re.match(r"[0-7]{1,3}", body[i + 1 :])
            raw = body[i : i + 1 + m.end()]
        else:
            raw = body[i : i + 2]
        flush_text()
        pieces.append(("x", raw))
        i += len(raw)
    flush_text()
    # printf / FormatMessage placeholders inside the text pieces become tokens too
    out: list[tuple[str, str]] = []
    for kind, val in pieces:
        if kind != "t":
            out.append((kind, val))
            continue
        pos = 0
        for m in _PLACEHOLDER_SRC_RE.finditer(val):
            if m.start() > pos:
                out.append(("t", val[pos : m.start()]))
            out.append(("x", m.group()))
            pos = m.end()
        if pos < len(val):
            out.append(("t", val[pos:]))
    return out, wide


def _unit_text(pieces) -> tuple[str, dict, bool]:
    parts, imap, n, alnum = [], {}, 0, False
    for kind, val in pieces:
        if kind == "t":
            parts.append(val)
            alnum = alnum or any(ch.isalnum() for ch in val)
        else:
            n += 1
            imap[str(n)] = {"point": val}
            # #693: FormatMessage `%1` / `%1!d!` names its argument and may move (⟦pN⟧);
            # printf `%s`, `%%` and escapes keep their order.
            parts.append(ph_free(n) if _FREE_PH.match(val) else ph_point("x", n))
    return "".join(parts), imap, alnum


def _escape_text(s: str, wide: bool) -> str:
    out = []
    for ch in s:
        if ch == "\\":
            out.append("\\\\")
        elif ch == '"':
            out.append('""')
        elif ch == "\n":
            out.append("\\n")
        elif ch == "\r":
            out.append("\\r")
        elif ch == "\t":
            out.append("\\t")
        elif ord(ch) < 0x20 or ord(ch) == 0x7F:
            out.append(("\\x%04x" if wide else "\\%03o") % ord(ch))
        else:
            out.append(ch)
    return "".join(out)


def _render_literal(text: str, imap: dict, wide: bool) -> str:
    out, pos = [], 0
    for m in _PLACEHOLDER_RE.finditer(text):
        out.append(_escape_text(text[pos : m.start()], wide))
        pos = m.end()
        inner = m.group(0)[1:-1]
        if is_point_kind(inner[:1]):
            info = imap.get(inner[1:])
            if info:
                out.append(info["point"])
        # unknown or paired tokens are dropped (same tolerance as html/docx/xliff)
    out.append(_escape_text(text[pos:], wide))
    return ('L"' if wide else '"') + "".join(out) + '"'


# ---------------------------------------------------------------------------- accelerators


def _accel_positions(text: str) -> list[int]:
    """Indices of the characters a single `&` marks (`&&` is a literal ampersand)."""
    res, i, n = [], 0, len(text)
    while i < n:
        if text[i] == "&":
            if i + 1 < n and text[i + 1] == "&":
                i += 2
                continue
            if i + 1 < n and text[i + 1] not in " \t" and text[i + 1] != "⟦":
                res.append(i + 1)
            i += 1
            continue
        i += 1
    return res


_CJK_RE = re.compile("[ᄀ-ᇿ぀-ヿ㄰-㆏㐀-鿿가-힯豈-﫿]")


def _fix_accelerator(source: str, target: str, imap: dict) -> str:
    """Keep exactly one accelerator in `target` when `source` has exactly one."""
    src = _accel_positions(source)
    if len(src) != 1:
        return target
    tgt = _accel_positions(target)
    if len(tgt) == 1:
        return target
    if len(tgt) > 1:
        for p in reversed(tgt[1:]):  # later ones become a literal ampersand
            target = target[: p - 1] + "&" + target[p - 1 :]
        return target
    letter = source[src[0]]
    # the text before a tab token (the shortcut after the tab is never marked)
    cut = len(target)
    for m in _PLACEHOLDER_RE.finditer(target):
        info = imap.get(m.group()[2:-1]) if is_point_kind(m.group()[1:2]) else None
        if info and info["point"] in ("\\t", "\\011", "\\x09"):
            cut = m.start()
            break
    plain = _PLACEHOLDER_RE.sub(lambda m: "\0" * len(m.group()), target[:cut])
    idx = plain.lower().find(letter.lower()) if letter.isalnum() else -1
    if idx != -1 and letter.isascii() and plain[idx].isascii():
        return target[:idx] + "&" + target[idx:]
    if not _CJK_RE.search(plain):
        # alphabetic scripts mark a letter of the word itself (Windows UI convention);
        # only CJK targets append the source letter as (&X)
        idx = next((k for k, ch in enumerate(plain) if ch.isalpha()), -1)
        if idx != -1:
            return target[:idx] + "&" + target[idx:]
    # CJK-style: append (&X) before the tab and before trailing "..." / ":"
    head = target[:cut]
    stripped = head.rstrip()
    for tail in ("...", "…", ":", "："):
        if stripped.endswith(tail):
            stripped = stripped[: -len(tail)]
            break
    return stripped + "(&" + letter.upper() + ")" + target[len(stripped) :]


# ---------------------------------------------------------------------------- walker

_TEXT_CONTROLS = {
    "LTEXT", "RTEXT", "CTEXT", "PUSHBUTTON", "DEFPUSHBUTTON", "PUSHBOX", "CHECKBOX",
    "AUTOCHECKBOX", "RADIOBUTTON", "AUTORADIOBUTTON", "AUTO3STATE", "STATE3", "GROUPBOX",
}  # fmt: skip
_OTHER_CONTROLS = {"CONTROL", "ICON", "EDITTEXT", "COMBOBOX", "LISTBOX", "SCROLLBAR"}
_IMAGE_STYLES = {"SS_ICON", "SS_BITMAP", "SS_ENHMETAFILE", "SS_OWNERDRAW"}
_VERSION_KEYS = {"FileDescription", "ProductName", "Comments", "LegalCopyright", "LegalTrademarks"}


def _is_open(t):
    return (t.kind == "id" and t.up == "BEGIN") or (t.kind == "p" and t.val == "{")


def _is_close(t):
    return (t.kind == "id" and t.up == "END") or (t.kind == "p" and t.val == "}")


class _Script:
    """Lexed script: `units` (literal spans in file order), LANGUAGE argument spans and
    the VERSIONINFO language spans."""

    def __init__(self, text: str):
        self.text = text
        self.toks = _lex(text)
        self._nl = [m.start() for m in re.finditer("\n", text)]
        self.units: list[dict] = []
        self.langs: list[tuple[int, int]] = []
        self.ver_blocks: list[tuple[int, int]] = []  # 8-hex StringFileInfo block id spans
        self.ver_trans: list[tuple[int, int]] = []  # Translation language number spans
        self._walk()

    def _line(self, pos):
        return bisect.bisect_right(self._nl, pos)

    def _unit(self, tok, style):
        pieces, wide = _literal_pieces(tok.val)
        text, imap, ok = _unit_text(pieces)
        if not ok:
            return
        self.units.append(
            {
                "start": tok.start,
                "end": tok.end,
                "wide": wide,
                "style": style,
                "text": text,
                "map": imap,
            }
        )
        check_segment_cap(len(self.units))

    def _language(self, i):
        """LANGUAGE a, b at toks[i]; returns the index after it."""
        t = self.toks
        if (
            i + 3 < len(t)
            and t[i + 1].kind in ("id", "num")
            and t[i + 2].kind == "p"
            and t[i + 2].val == ","
            and t[i + 3].kind in ("id", "num")
        ):
            self.langs.append((t[i + 1].start, t[i + 3].end))
            return i + 4
        return i + 1

    def _header(self, i, caption=False):
        """Optional statements up to the body's BEGIN; returns the index of BEGIN (or len)."""
        t = self.toks
        while i < len(t) and not _is_open(t[i]):
            if t[i].kind == "id" and t[i].up == "LANGUAGE":
                i = self._language(i)
                continue
            if (
                caption
                and t[i].kind == "id"
                and t[i].up == "CAPTION"
                and i + 1 < len(t)
                and t[i + 1].kind == "str"
            ):
                self._unit(t[i + 1], "rc:caption")
                i += 2
                continue
            if _is_close(t[i]):  # malformed: a stray END before any body
                return i
            i += 1
        return i

    def _body(self, i, handler):
        """Walk a BEGIN..END body starting at toks[i] (the BEGIN). Returns index after END."""
        t = self.toks
        if i >= len(t) or not _is_open(t[i]):
            return i
        depth, i = 1, i + 1
        while i < len(t) and depth:
            tok = t[i]
            if _is_open(tok):
                depth += 1
                i += 1
            elif _is_close(tok):
                depth -= 1
                i += 1
            else:
                i = handler(i, depth)
        return i

    # --- per resource type handlers (return the next index) ---
    def _h_strings(self, i, depth):
        if self.toks[i].kind == "str":
            self._unit(self.toks[i], "rc:string")
        return i + 1

    def _h_menu(self, i, depth):
        t = self.toks
        if (
            t[i].kind == "id"
            and t[i].up in ("MENUITEM", "POPUP")
            and i + 1 < len(t)
            and t[i + 1].kind == "str"
        ):
            self._unit(t[i + 1], "rc:menu")
            return i + 2
        return i + 1

    def _h_dialog(self, i, depth):
        t = self.toks
        tok = t[i]
        if tok.kind != "id" or (tok.up not in _TEXT_CONTROLS and tok.up not in _OTHER_CONTROLS):
            return i + 1
        if tok.up in _TEXT_CONTROLS:
            if i + 1 < len(t) and t[i + 1].kind == "str":
                self._unit(t[i + 1], "rc:control")
                return i + 2
            return i + 1
        if tok.up == "CONTROL" and i + 1 < len(t) and t[i + 1].kind == "str":
            j, image = i + 2, False
            while j < len(t):
                u = t[j]
                if _is_open(u) or _is_close(u):
                    break
                if u.kind == "id" and (u.up in _TEXT_CONTROLS or u.up in _OTHER_CONTROLS):
                    break
                if u.kind == "id" and u.up in _IMAGE_STYLES:
                    image = True
                j += 1
            if not image:
                self._unit(t[i + 1], "rc:control")
            return i + 2
        return i + 1

    def _h_version(self, i, depth):
        t = self.toks
        tok = t[i]
        if tok.kind == "id" and tok.up == "BLOCK" and i + 1 < len(t) and t[i + 1].kind == "str":
            s = t[i + 1]
            if re.fullmatch(r'L?"[0-9A-Fa-f]{8}"', s.val):
                q = s.val.index('"') + 1
                self.ver_blocks.append((s.start + q, s.start + q + 4))
            return i + 2
        if tok.kind == "id" and tok.up == "VALUE" and i + 1 < len(t) and t[i + 1].kind == "str":
            key = t[i + 1].val.lstrip("L").strip('"').removesuffix("\\0").strip()
            j = i + 2
            if j < len(t) and t[j].kind == "p" and t[j].val == ",":
                j += 1
            if key == "Translation" and j < len(t) and t[j].kind == "num":
                self.ver_trans.append((t[j].start, t[j].end))
                return j + 1
            if key in _VERSION_KEYS and j < len(t) and t[j].kind == "str":
                self._unit(t[j], "rc:version")
                return j + 1
            return j
        return i + 1

    def _skip_unknown(self, i, type_idx):
        """An unknown or skipped resource: the rest of its line (and lines continued by a
        trailing comma), then a BEGIN..END body if one follows."""
        t = self.toks
        line = self._line(t[type_idx].start)
        i = type_idx + 1
        while i < len(t) and not _is_open(t[i]):
            if self._line(t[i].start) != line:
                if t[i - 1].kind == "p" and t[i - 1].val == ",":
                    line = self._line(t[i].start)
                else:
                    break
            i += 1
        if i < len(t) and _is_open(t[i]):
            i = self._body(i, lambda k, d: k + 1)
        return i

    def _walk(self):
        t = self.toks
        i = 0
        while i < len(t):
            tok = t[i]
            up = tok.up
            if up == "LANGUAGE":
                i = self._language(i)
            elif up == "STRINGTABLE":
                i = self._body(self._header(i + 1), self._h_strings)
            elif up in ("VERSION", "CHARACTERISTICS", "RCINCLUDE"):
                i = self._skip_unknown(i, i)
            elif tok.kind in ("id", "num", "str") and i + 1 < len(t) and t[i + 1].kind == "id":
                typ = t[i + 1].up
                if typ in ("DIALOG", "DIALOGEX"):
                    i = self._body(self._header(i + 2, caption=True), self._h_dialog)
                elif typ in ("MENU", "MENUEX"):
                    i = self._body(self._header(i + 2), self._h_menu)
                elif typ == "VERSIONINFO":
                    i = self._body(self._header(i + 2), self._h_version)
                else:
                    i = self._skip_unknown(i, i + 1)
            elif _is_open(tok):
                i = self._body(i, lambda k, d: k + 1)
            else:
                i += 1


# ---------------------------------------------------------------------------- LANGID


def _langid(target_locale) -> int | None:
    from app.filters import a11y

    tag = a11y.target_tag(target_locale)
    if not tag:
        return None
    parts = tag.split("-")
    lang = parts[0].lower()
    region = next((p.upper() for p in parts[1:] if len(p) == 2 and p.isalpha()), "")
    script = next((p.title() for p in parts[1:] if len(p) == 4 and p.isalpha()), "")
    if lang == "zh" and not region:
        region = {"Hant": "TW", "Hans": "CN"}.get(script, "CN")
    table = _pylocale.windows_locale
    if region:
        hits = sorted(k for k, v in table.items() if v == f"{lang}_{region}")
        if hits:
            return hits[0]
    same = sorted(k for k, v in table.items() if v.split("_")[0] == lang)
    default = [k for k in same if (k >> 10) == 1]
    return (default or same or [None])[0]


def _stamp_edits(script: _Script, text: str, target_locale) -> list[tuple[int, int, str]]:
    lcid = _langid(target_locale)
    if lcid is None:
        return []
    edits: list[tuple[int, int, str]] = []
    norm = {re.sub(r"\s+", "", text[a:b]).upper() for a, b in script.langs}
    if len(norm) == 1:  # a multi-language script is left alone
        val = f"0x{lcid & 0x3FF:02x}, 0x{lcid >> 10:02x}"
        edits += [(a, b, val) for a, b in script.langs]
    if len(script.ver_blocks) == 1:
        a, b = script.ver_blocks[0]
        new = f"{lcid:04x}"
        upper = any(ch in "ABCDEF" for ch in text[a : a + 8])  # e.g. "040904B0"
        edits.append((a, b, new.upper() if upper else new))
    if len(script.ver_trans) == 1:
        a, b = script.ver_trans[0]
        old = text[a:b]
        edits.append((a, b, f"0x{lcid:x}" if old.lower().startswith("0x") else str(lcid)))
    return edits


# ---------------------------------------------------------------------------- filter


def _budget(text: str) -> None:
    from app import config

    check_line_budget(text)
    if text.count('"') > 40 * config.MAX_DOCUMENT_SEGMENTS:
        raise InputLimitError(
            "This resource script has far more strings than MAX_DOCUMENT_SEGMENTS allows "
            f"({config.MAX_DOCUMENT_SEGMENTS}). Split it into smaller files."
        )


def _load(data: bytes) -> tuple[_Script, str]:
    text, enc = _decode(data)
    _budget(text)
    return _Script(text), enc


@register
class RcFilter(Filter):
    fmt = "rc"
    exts = ()

    def parse(self, data: bytes, provider: str | None = None) -> ParseResult:
        script, enc = _load(data)
        units = [
            TextUnit(p_index=i, text=u["text"], style=u["style"], inline_map=u["map"])
            for i, u in enumerate(script.units)
        ]
        check_unit_texts(u.text for u in units)
        skeleton = {
            "format": "rc",
            "units": [{"p_index": u.p_index, "text": u.text, "style": u.style} for u in units],
        }
        return ParseResult(skeleton=note_source_encoding(skeleton, enc), units=units)

    def _rebuild(self, original: bytes, paragraph_texts: dict, target_locale=None) -> bytes:
        script, enc = _load(original)
        text = script.text
        edits = []
        for i, u in enumerate(script.units):
            new = paragraph_texts.get(str(i))
            if new is None or new == u["text"]:
                continue
            if u["style"] in ("rc:menu", "rc:control"):
                new = _fix_accelerator(u["text"], new, u["map"])
            edits.append((u["start"], u["end"], _render_literal(new, u["map"], u["wide"])))
        if target_locale is not None:
            edits += _stamp_edits(script, text, target_locale)  # never overlaps a literal
        if not edits:
            return bytes(original)
        # one linear pass (re-slicing the whole text per edit is quadratic)
        parts, pos = [], 0
        for a, b, val in sorted(edits):
            parts += [text[pos:a], val]
            pos = b
        parts.append(text[pos:])
        text = "".join(parts)
        if text == script.text:
            return bytes(original)
        return _encode(text, enc)

    def build(self, original: bytes, paragraph_texts: dict) -> bytes:
        return self._rebuild(original, paragraph_texts)

    def build_localized(
        self, original: bytes, paragraph_texts: dict, target_locale: str | None = None
    ) -> bytes:
        return self._rebuild(original, paragraph_texts, target_locale or "")
