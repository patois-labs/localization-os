"""EBU STL subtitle filter (EBU Tech 3264-E, the binary subtitling data exchange format).

An .stl file is one 1024-byte General Subtitle Information (GSI) block followed by
128-byte Text and Timing Information (TTI) blocks. GSI bytes 3-10 (DFC) read
``STL25.01`` or ``STL30.01``; bytes 12-13 (CCT) name the character code table of the
text (00 Latin ISO 6937, 01 Latin/Cyrillic ISO 8859-5, 02 Latin/Arabic 8859-6,
03 Latin/Greek 8859-7, 04 Latin/Hebrew 8859-8); bytes 14-15 (LC) the language.

A TTI block: SGN, SN (2 bytes LE), EBN (FF = last block of a subtitle, 00-EF
extension blocks, FE user data), CS, TCI, TCO, VP, JC, CF (01 = comment), and a
112-byte Text Field (TF). In the TF, 8Ah is CR/LF between rows, 8Fh is unused space
(and ends the text), 00h-1Fh are teletext control codes (each shown as a space),
80h-85h italics/underline/boxing for open subtitles. In table 00 an accented letter
is two bytes, the floating diacritic (C1h-CFh) first.

Units: the text of each subtitle (its TF blocks joined, comments and user-data blocks
excluded), rows joined with ``\\n``. Leading and trailing control codes and spaces of
each row (double height, colours, start/end box) stay with the row; a control code
inside a row is a protected point token ``⟦xN⟧``. Timecodes, positions, every other
field and every untouched block are written back byte for byte.

Re-encoding (build): a translation is encoded in the declared table; a row holds at
most MNC characters (GSI; 40 when absent) and a subtitle keeps its row count (or 2,
whichever is more): the translator's breaks are kept when they fit, otherwise the
text is re-wrapped. When the declared table cannot encode the translation (Russian
into a Latin file) the filter switches CCT to the first table that encodes every
subtitle and re-encodes them all. A subtitle that needs more TF bytes gets extension
blocks (TNB is updated). Text that still does not fit, or characters no table can
encode, refuse the export with a list of each problem subtitle. build_localized
also writes the target language into LC (Tech 3264 Appendix 3; 00 when unlisted).

Spec: EBU Tech 3264-E (1991), https://tech.ebu.ch/docs/tech/tech3264.pdf

Documented in: docs/architecture/translation-pipeline.md
"""

import contextlib
import re
import unicodedata

from app.filters.base import (
    Filter,
    InputLimitError,
    ParseResult,
    TextUnit,
    check_segment_cap,
    check_unit_texts,
    register,
)
from app.filters.scc_filter import CaptionTargetError

GSI_SIZE = 1024
TTI_SIZE = 128
TF_SIZE = 112
DEFAULT_MNC = 40
MIN_MAX_ROWS = 2
MAX_TTI_BLOCKS = 99999  # TNB is five decimal digits
_TOKEN_RE = re.compile(r"⟦x(\d+)⟧")


class StlParseError(ValueError):
    """The file is not a readable EBU STL file."""


# --- character code tables ------------------------------------------------------------

# Table 00 (ISO 6937 as reproduced in Tech 3264 Appendix 2), 0xA0-0xFF; ASCII below.
_T00_HIGH = {
    0xA0: " ",
    0xA1: "¡",
    0xA2: "¢",
    0xA3: "£",
    0xA4: "$",
    0xA5: "¥",
    0xA6: "#",
    0xA7: "§",
    0xA8: "¤",
    0xA9: "‘",
    0xAA: "“",
    0xAB: "«",
    0xAC: "←",
    0xAD: "↑",
    0xAE: "→",
    0xAF: "↓",
    0xB0: "°",
    0xB1: "±",
    0xB2: "²",
    0xB3: "³",
    0xB4: "×",
    0xB5: "µ",
    0xB6: "¶",
    0xB7: "·",
    0xB8: "÷",
    0xB9: "’",
    0xBA: "”",
    0xBB: "»",
    0xBC: "¼",
    0xBD: "½",
    0xBE: "¾",
    0xBF: "¿",
    0xD0: "―",
    0xD1: "¹",
    0xD2: "®",
    0xD3: "©",
    0xD4: "™",
    0xD5: "♪",
    0xD6: "¬",
    0xD7: "¦",
    0xDC: "⅛",
    0xDD: "⅜",
    0xDE: "⅝",
    0xDF: "⅞",
    0xE0: "Ω",
    0xE1: "Æ",
    0xE2: "Đ",
    0xE3: "ª",
    0xE4: "Ħ",
    0xE6: "Ĳ",
    0xE7: "Ŀ",
    0xE8: "Ł",
    0xE9: "Ø",
    0xEA: "Œ",
    0xEB: "º",
    0xEC: "Þ",
    0xED: "Ŧ",
    0xEE: "Ŋ",
    0xEF: "ŉ",
    0xF0: "ĸ",
    0xF1: "æ",
    0xF2: "đ",
    0xF3: "ð",
    0xF4: "ħ",
    0xF5: "ı",
    0xF6: "ĳ",
    0xF7: "ŀ",
    0xF8: "ł",
    0xF9: "ø",
    0xFA: "œ",
    0xFB: "ß",
    0xFC: "þ",
    0xFD: "ŧ",
    0xFE: "ŋ",
    0xFF: "­",
}
# Floating diacritics (sent before the base letter).
_DIACRITIC = {
    0xC1: "̀",
    0xC2: "́",
    0xC3: "̂",
    0xC4: "̃",
    0xC5: "̄",
    0xC6: "̆",
    0xC7: "̇",
    0xC8: "̈",
    0xCA: "̊",
    0xCB: "̧",
    0xCD: "̋",
    0xCE: "̨",
    0xCF: "̌",
}
_MARK_BYTE = {v: k for k, v in _DIACRITIC.items()}
_CODECS = {"00": None, "01": "iso8859_5", "02": "iso8859_6", "03": "iso8859_7", "04": "iso8859_8"}


def _table(cct: str) -> dict[int, str]:
    """byte -> character for the printable bytes of a code table."""
    t = {b: chr(b) for b in range(0x20, 0x7F)}
    if cct == "00":
        t.update(_T00_HIGH)
        return t
    codec = _CODECS[cct]
    for b in range(0xA0, 0x100):
        with contextlib.suppress(UnicodeDecodeError):
            t[b] = bytes([b]).decode(codec)
    return t


_TABLES = {c: _table(c) for c in _CODECS}
_ENCODERS = {}
for _c, _t in _TABLES.items():
    _rev: dict[str, int] = {}
    for _b, _ch in sorted(_t.items(), reverse=True):
        _rev[_ch] = _b  # the lower byte wins ($ -> 24h, # -> 23h)
    _ENCODERS[_c] = _rev
_NORMALIZE = {" ": " ", " ": " ", "​": "", "…": "...", "–": "-", "‑": "-"}
# #679: typographic quotes and the em dash, folded only when the table lacks them (table 01
# Latin/Cyrillic has no « », so Russian MT output with them could never be exported).
_NORMALIZE.update(
    {
        "«": '"',
        "»": '"',
        "„": '"',
        "“": '"',
        "”": '"',
        "‹": "'",
        "›": "'",
        "‚": "'",
        "‘": "'",
        "’": "'",
        "—": "-",
    }
)


def _encode_char(c: str, cct: str) -> bytes | None:
    enc = _ENCODERS[cct]
    if c in enc:
        return bytes([enc[c]])
    if cct == "00":
        d = unicodedata.normalize("NFD", c)
        if len(d) == 2 and d[1] in _MARK_BYTE and d[0] in enc and enc[d[0]] < 0x80:
            return bytes([_MARK_BYTE[d[1]], enc[d[0]]])
    return None


def _prep(text: str, cct: str) -> str:
    text = unicodedata.normalize("NFC", text)
    out = []
    for c in text:
        if c in _NORMALIZE and _encode_char(c, cct) is None:
            out.append(_NORMALIZE[c])
        else:
            out.append(c)
    return "".join(out)


def unsupported_chars(text: str, cct: str, known=None) -> list[str]:
    """The characters of `text` table `cct` cannot hold. `known`: the token numbers of the
    subtitle's codes; a token naming none of them is text (#814.1), and its brackets are
    characters no EBU table holds. None: every token is a code."""
    prepped = _prep(text, cct)
    if known is None:
        plain = _TOKEN_RE.sub("", prepped)
    else:
        plain = _TOKEN_RE.sub(lambda m: "" if int(m.group(1)) in known else m.group(0), prepped)
    plain = plain.replace("\n", "")
    seen: set[str] = set()
    bad: list[str] = []
    for c in _clusters(plain):
        if c not in seen and _encode_char(c, cct) is None:
            seen.add(c)
            bad.append(c)
    return bad


def _clusters(s: str) -> list[str]:
    """Characters of s; a base character followed by one floating-diacritic mark is one cluster.

    Table 00 holds any mark before any ASCII letter (C1h 'x' is x + U+0300 with no precomposed
    form), so the pair is one atom that is encoded as the two bytes (#798.3).
    """
    out: list[str] = []
    for c in s:
        if out and c in _MARK_BYTE and len(out[-1]) == 1 and out[-1] not in _MARK_BYTE:
            out[-1] += c
        else:
            out.append(c)
    return out


def _byte_ok(b: int, cct: str) -> bool:
    """A raw byte (a control code, a byte the old table has no character for) may move to table
    cct only when that table reads it the same way: 00-9F (controls, ASCII, 7F) mean the same
    in every table; an upper-half byte moves only when the new table has no character for it
    (#798.2)."""
    if b < 0xA0:
        return True
    return b not in _TABLES[cct] and not (cct == "00" and b in _DIACRITIC)


# --- TF decoding ----------------------------------------------------------------------


def _spacing(b: int) -> bool:
    return b < 0x20  # teletext control codes occupy a character cell


class _TextRow:
    __slots__ = ("cells", "prefix", "suffix")

    def __init__(self, prefix: bytes, cells: list, suffix: bytes):
        self.prefix, self.cells, self.suffix = prefix, cells, suffix


def _decode_row(raw: bytes, table: dict) -> list:
    """Cells of one row: ('c', char) or ('t', byte)."""
    cells: list = []
    i = 0
    while i < len(raw):
        b = raw[i]
        if b in _DIACRITIC and table is _TABLES["00"]:
            if i + 1 < len(raw) and raw[i + 1] in table and raw[i + 1] < 0x80:
                ch = unicodedata.normalize("NFC", table[raw[i + 1]] + _DIACRITIC[b])
                cells.append(("c", ch, raw[i : i + 2]))
                i += 2
                continue
            cells.append(("t", b, raw[i : i + 1]))
        elif b in table:
            cells.append(("c", table[b], raw[i : i + 1]))
        else:
            cells.append(("t", b, raw[i : i + 1]))
        i += 1
    return cells


class _Subtitle:
    """The text of one subtitle: head bytes, text rows with separators, tail bytes."""

    def __init__(self, tf: bytes, cct: str):
        table = _TABLES[cct]
        self.rows: list[_TextRow] = []
        self.seps: list[bytes] = []  # bytes between consecutive text rows
        self.head = b""
        self.tail = b""
        pieces = re.split(rb"(\x8a+)", tf)
        between = b""
        for k, piece in enumerate(pieces):
            if k % 2 == 1:  # a run of CR/LF
                between += piece
                continue
            cells = _decode_row(piece, table)
            lo = 0
            while lo < len(cells) and (cells[lo][0] == "t" or cells[lo][1] == " "):
                lo += 1
            hi = len(cells)
            while hi > lo and (cells[hi - 1][0] == "t" or cells[hi - 1][1] == " "):
                hi -= 1
            if lo == hi:  # a row with no text: part of the separator
                between += piece
                continue
            prefix = b"".join(c[2] for c in cells[:lo])
            suffix = b"".join(c[2] for c in cells[hi:])
            if self.rows:
                self.seps.append(between)
            else:
                self.head = between
            between = b""
            self.rows.append(_TextRow(prefix, cells[lo:hi], suffix))
        self.tail = between

    def texts(self):
        rows, tokens = [], {}
        for r in self.rows:
            parts: list[str] = []
            for kind, v, _raw in r.cells:
                if kind == "c":
                    parts.append(v)
                    continue
                n = len(tokens) + 1
                tokens[n] = v
                if _spacing(v) and parts and parts[-1] != " ":
                    parts.append(" ")  # the code is shown as a space
                parts.append(f"⟦x{n}⟧")
            rows.append("".join(parts))
        return rows, tokens

    def text(self) -> str:
        return "\n".join(self.texts()[0])


# --- file model -----------------------------------------------------------------------


class _Group:
    """The text blocks of one subtitle (indices into the block list)."""

    def __init__(self, sn: bytes, comment: bool):
        self.sn = sn
        self.comment = comment
        self.blocks: list[int] = []
        self.tf = b""


def _read(data: bytes):
    raw = bytes(data)
    if len(raw) < GSI_SIZE or raw[3:11] not in (b"STL25.01", b"STL30.01"):
        raise StlParseError(
            "Not an EBU STL subtitle file (the GSI block must start with a code page and "
            "STL25.01 or STL30.01). 3D-model .stl files are not translatable."
        )
    if (len(raw) - GSI_SIZE) % TTI_SIZE:
        raise StlParseError(
            "EBU STL file is truncated: the TTI blocks are not a whole number of 128 bytes."
        )
    n = (len(raw) - GSI_SIZE) // TTI_SIZE
    if n > MAX_TTI_BLOCKS:
        raise InputLimitError(f"EBU STL file has more than {MAX_TTI_BLOCKS} TTI blocks; refused.")
    cct = raw[12:14].decode("latin-1")
    if cct.strip() == "":
        cct = "00"
    if cct not in _CODECS:
        raise StlParseError(f"EBU STL character code table '{cct}' is not one of 00-04; refused.")
    blocks = [raw[GSI_SIZE + i * TTI_SIZE : GSI_SIZE + (i + 1) * TTI_SIZE] for i in range(n)]
    groups: list[_Group] = []
    cur = None
    for i, blk in enumerate(blocks):
        sn, ebn = blk[1:3], blk[3]
        if ebn == 0xFE or 0xF0 <= ebn <= 0xFD:
            continue  # user data / reserved: never text
        if cur is None or cur.sn != sn:
            cur = _Group(sn, blk[15] == 0x01)
            groups.append(cur)
            check_segment_cap(len(groups))
        cur.blocks.append(i)
        cur.tf += blk[16:].rstrip(b"\x8f")
        if ebn == 0xFF:
            cur = None
    return raw, cct, blocks, groups


def _mnc(raw: bytes) -> int:
    try:
        v = int(raw[251:253].decode("ascii"))
    except (UnicodeDecodeError, ValueError):
        return DEFAULT_MNC
    return v if v > 0 else DEFAULT_MNC


def _units(raw, cct, groups):
    """(group, _Subtitle, text) per translatable subtitle, in file order."""
    out = []
    for g in groups:
        if g.comment:
            continue
        sub = _Subtitle(g.tf, cct)
        text = sub.text()
        if text.strip():
            out.append((g, sub, text))
    return out


# --- encoding -------------------------------------------------------------------------


def _atoms(line: str, tokens: dict):
    out, pos = [], 0
    # #814.1: a token that names no code of the subtitle is text
    for m in (m for m in _TOKEN_RE.finditer(line) if int(m.group(1)) in tokens):
        out.extend(("c", c) for c in _clusters(line[pos : m.start()]))
        out.append(("t", tokens[int(m.group(1))]))
        pos = m.end()
    out.extend(("c", c) for c in _clusters(line[pos:]))
    return out


def _width(atoms, prev_space: bool = False) -> int:
    n = 0
    for kind, v in atoms:
        if kind == "c":
            n += 1
            prev_space = v == " "
        else:
            if _spacing(v) and not prev_space:
                n += 1
            prev_space = False
    return n


def _encode_atoms(atoms, cct: str) -> bytes:
    out = bytearray()
    for i, (kind, v) in enumerate(atoms):
        if kind == "t":
            if _spacing(v) and i and atoms[i - 1] == ("c", " ") and out and out[-1] == 0x20:
                out.pop()  # the control code takes the space's cell
            out.append(v)
        else:
            out += _encode_char(v, cct)
    return bytes(out)


def _wrap(words, width, w0, w1):
    """Greedy wrap. w0[i] is the width of word i alone, w1[i] its width after a joining space
    (a spacing code right after the space takes no cell of its own); the row width grows
    incrementally, so a wrap is linear in the words (#798.5)."""
    lines, cur, cw = [], "", 0
    for i, w in enumerate(words):
        if not cur:
            cur, cw = w, w0[i]
        elif cw + 1 + w1[i] <= width:
            cur, cw = f"{cur} {w}", cw + 1 + w1[i]
        else:
            lines.append(cur)
            cur, cw = w, w0[i]
    if cur:
        lines.append(cur)
    return lines


def _layout(text: str, n_rows: int, max_rows: int, mnc: int, tokens: dict):
    def measure(s):
        return _width(_atoms(s, tokens))

    own = [r.strip() for r in text.split("\n") if r.strip()]
    if own and len(own) <= max_rows and all(measure(r) <= mnc for r in own):
        return own
    words = " ".join(own).split()
    widths = [measure(w) for w in words]
    if not words or max(widths) > mnc:
        return None
    if sum(widths) > mnc * max_rows:
        # gate (train-9): cannot fit whatever the breaks (a row is at least the sum of its
        # words); refuse now instead of re-wrapping a huge text once per width (13 s at MNC 99)
        return None
    w1 = [_width(_atoms(w, tokens), True) for w in words]
    target = max(1, min(n_rows, max_rows))
    for width in range(1, mnc + 1):
        rows = _wrap(words, width, widths, w1)
        if len(rows) <= target:
            return rows
    rows = _wrap(words, mnc, widths, w1)
    return rows if len(rows) <= max_rows else None


def _double_height(row: _TextRow) -> bool:
    return 0x0D in row.prefix or 0x0F in row.prefix


def _frame_bytes(sub: _Subtitle):
    """Raw bytes around the text rows that are copied as they are."""
    yield sub.head
    yield sub.tail
    yield from sub.seps
    for r in sub.rows:
        yield r.prefix
        yield r.suffix


def _frame_moves(sub: _Subtitle, cct: str) -> bool:
    return all(_byte_ok(b, cct) for chunk in _frame_bytes(sub) for b in chunk)


def _sub_moves(sub: _Subtitle, text: str, cct: str) -> bool:
    """Every byte the rewritten subtitle keeps raw (frame, and the codes `text` uses) means
    the same in table cct (#798.2)."""
    if not _frame_moves(sub, cct):
        return False
    _r, tokens = sub.texts()
    used = {int(m.group(1)) for m in _TOKEN_RE.finditer(text)}
    return all(_byte_ok(v, cct) for n, v in tokens.items() if n in used)


def _encode_subtitle(sub: _Subtitle, text: str, cct: str, mnc: int, *, keep_rows: bool):
    """(TF bytes, row delta, None) or (None, 0, reason)."""
    text = _prep(text, cct)
    _r, tokens = sub.texts()
    bad = unsupported_chars(text, cct, tokens)
    if bad:
        return None, 0, "characters the code table cannot encode: " + " ".join(map(repr, bad[:8]))
    n = len(sub.rows)
    if keep_rows:  # re-encoding an untouched subtitle in another table
        rows = text.split("\n")
    else:
        rows = _layout(text, n, max(n, MIN_MAX_ROWS), mnc, tokens)
        if rows is None:
            return None, 0, f"does not fit in {max(n, MIN_MAX_ROWS)} rows of {mnc} characters"
    m = len(rows)
    out = bytearray(sub.head)
    for i, row_text in enumerate(rows):
        j = i - (m - n)  # rows align at the bottom
        tpl = sub.rows[j] if j >= 0 else sub.rows[0]
        if i:
            if j >= 1:
                out += sub.seps[j - 1]
            else:
                out += (
                    sub.seps[0] if sub.seps else (b"\x8a\x8a" if _double_height(tpl) else b"\x8a")
                )
        out += tpl.prefix + _encode_atoms(_atoms(row_text, tokens), cct) + tpl.suffix
    out += sub.tail
    step = 2 if _double_height(sub.rows[0]) else 1
    return bytes(out), (m - n) * step, None


def _others(groups, cct: str):
    """(group, _Subtitle, text) for the groups that are not units: comments and text-less."""
    out = []
    for g in groups:
        sub = _Subtitle(g.tf, cct)
        text = sub.text()
        if g.comment or not text.strip():
            out.append((g, sub, text))
    return out


def _blocks_for(tf: bytes, headers: list[bytes], cct: str) -> list[bytes]:
    """Split a TF into TTI blocks (headers: the original 16-byte block headers)."""
    chunks, i = [], 0
    data = tf + b"\x8f"  # the last block's TF always ends with 8Fh
    while i < len(data):
        end = min(i + TF_SIZE, len(data))
        if end < len(data) and cct == "00" and data[end - 1] in _DIACRITIC:
            end -= 1  # never split a diacritic from its letter
        chunks.append(data[i:end])
        i = end
    out = []
    for k, chunk in enumerate(chunks):
        hdr = bytearray(headers[k] if k < len(headers) else headers[0])
        hdr[3] = 0xFF if k == len(chunks) - 1 else k
        out.append(bytes(hdr) + chunk + b"\x8f" * (TF_SIZE - len(chunk)))
    return out


# Tech 3264 Appendix 3 language codes, keyed by ISO 639 code.
LANGUAGE_CODES = {
    "sq": 0x01, "br": 0x02, "ca": 0x03, "hr": 0x04, "cy": 0x05, "cs": 0x06, "da": 0x07,
    "de": 0x08, "en": 0x09, "es": 0x0A, "eo": 0x0B, "et": 0x0C, "eu": 0x0D, "fo": 0x0E,
    "fr": 0x0F, "fy": 0x10, "ga": 0x11, "gd": 0x12, "gl": 0x13, "is": 0x14, "it": 0x15,
    "se": 0x16, "la": 0x17, "lv": 0x18, "lb": 0x19, "lt": 0x1A, "hu": 0x1B, "mt": 0x1C,
    "nl": 0x1D, "no": 0x1E, "nb": 0x1E, "nn": 0x1E, "oc": 0x1F, "pl": 0x20, "pt": 0x21,
    "ro": 0x22, "rm": 0x23, "sr": 0x24, "sk": 0x25, "sl": 0x26, "fi": 0x27, "sv": 0x28,
    "tr": 0x29, "wa": 0x2B,
    "zu": 0x45, "vi": 0x46, "uz": 0x47, "ur": 0x48, "uk": 0x49, "th": 0x4A, "te": 0x4B,
    "tt": 0x4C, "ta": 0x4D, "tg": 0x4E, "sw": 0x4F, "srn": 0x50, "so": 0x51, "si": 0x52,
    "sn": 0x53, "sh": 0x54, "rue": 0x55, "ru": 0x56, "qu": 0x57, "ps": 0x58, "pa": 0x59,
    "fa": 0x5A, "pap": 0x5B, "or": 0x5C, "ne": 0x5D, "nd": 0x5E, "mr": 0x5F, "ms": 0x61,
    "mg": 0x62, "mk": 0x63, "lo": 0x64, "ko": 0x65, "km": 0x66, "kk": 0x67, "kn": 0x68,
    "ja": 0x69, "id": 0x6A, "hi": 0x6B, "he": 0x6C, "ha": 0x6D, "gn": 0x6E, "gu": 0x6F,
    "el": 0x70, "ka": 0x71, "ff": 0x72, "prs": 0x73, "cv": 0x74, "zh": 0x75, "my": 0x76,
    "bg": 0x77, "bn": 0x78, "be": 0x79, "bm": 0x7A, "az": 0x7B, "as": 0x7C, "hy": 0x7D,
    "ar": 0x7E, "am": 0x7F,
}  # fmt: skip


def language_code(locale: str | None) -> int:
    """The LC byte value for a BCP 47 locale (00 = unknown / not applicable)."""
    parts = (locale or "").replace("_", "-").lower().split("-")
    lang = parts[0]
    if lang == "nl" and "be" in parts[1:]:
        return 0x2A  # Flemish
    if lang == "iw":
        lang = "he"
    return LANGUAGE_CODES.get(lang, 0x00)


@register
class StlFilter(Filter):
    fmt = "stl"

    def parse(self, data: bytes, provider: str | None = None) -> ParseResult:
        raw, cct, _blocks, groups = _read(data)
        units: list[TextUnit] = []
        for k, (_g, _s, text) in enumerate(_units(raw, cct, groups)):
            units.append(TextUnit(p_index=k, text=text))
            check_segment_cap(k + 1)
        check_unit_texts(u.text for u in units)
        skeleton = {
            "format": "stl",
            "units": [{"p_index": u.p_index, "text": u.text, "style": ""} for u in units],
        }
        return ParseResult(skeleton=skeleton, units=units)

    def _rebuild(self, original: bytes, paragraph_texts: dict, lc: int | None) -> bytes:
        raw, cct, blocks, groups = _read(original)
        mnc = _mnc(raw)
        units = _units(raw, cct, groups)
        changed = {}
        for k, (_g, _s, text) in enumerate(units):
            new = paragraph_texts.get(str(k))
            if new is not None and new != text:
                changed[k] = new
        # The declared table first; another one only when it cannot encode a translation.
        problems: list[str] = []
        named: set[int] = set()  # subtitle indexes already in problems (a set, not a scan)
        chosen = None

        # untouched subtitles must survive a switch too (#679: only the untouched ones; a
        # translated subtitle's old text is replaced, so its characters do not matter)
        # Comment subtitles and text-less subtitles are not units, but their bytes are in the
        # old table too: they move with the file or the switch is refused (#798.1).
        # (Same rule and messages as Bilingua's bilingua-stl write.rs `rebuild`.)
        # the token numbers of each translated subtitle's codes (#814.1: any other token is text)
        known = {k: set(units[k][1].texts()[1]) for k in changed}

        def fits(c: str) -> bool:
            return all(not unsupported_chars(t, c, known[k]) for k, t in changed.items())

        def units_cover(c: str) -> bool:  # the untouched units have characters in table c
            return all(
                k in changed or not unsupported_chars(t, c) for k, (_g, _s, t) in enumerate(units)
            )

        others = [] if fits(cct) else _others(groups, cct)

        def others_lacking(c: str) -> list[str]:
            return [
                f"comment subtitle at block {g.blocks[0] + 1}"
                for g, _s, t in others
                if unsupported_chars(t, c)
            ]

        def unmovable(c: str) -> list[str]:  # subtitles with bytes table c reads differently
            out = [
                f"subtitle {k + 1}"
                for k, (_g, s, t) in enumerate(units)
                if not _sub_moves(s, changed.get(k, t), c)
            ]
            out += [
                f"comment subtitle at block {g.blocks[0] + 1}"
                for g, s, t in others
                if not _sub_moves(s, t, c)
            ]
            return out

        refusing = False
        if fits(cct):
            chosen = cct
        else:
            usable = [cct] + [
                c
                for c in _CODECS
                if c != cct and units_cover(c) and not others_lacking(c) and not unmovable(c)
            ]
            chosen = next((c for c in usable if fits(c)), None)
        if chosen is None:
            refusing = True
            # a table holds the translation and the untouched units, but a comment cannot
            # move there or the file has bytes it would turn into other characters (#798)
            need = next((c for c in _CODECS if c != cct and units_cover(c) and fits(c)), None)
            if need is not None:

                def shown(v: list[str]) -> str:
                    more = f" (and {len(v) - 10} more)" if len(v) > 10 else ""
                    return ", ".join(v[:10]) + more

                why = []
                if unmovable(need):
                    why.append(
                        "these subtitles hold bytes with no equivalent in that table (they "
                        f"would read as other characters): {shown(unmovable(need))}"
                    )
                if others_lacking(need):
                    why.append(
                        "these comment subtitles hold characters that table does not have: "
                        + shown(others_lacking(need))
                    )
                raise CaptionTargetError(
                    f"The EBU STL file was not exported: the translation needs code table "
                    f"{need}, but {'; and '.join(why)}. Remove those bytes or characters, or "
                    f"keep the translation in code table {cct}."
                )
            # name the characters that block the closest table, not every letter the
            # declared table lacks (#679: the error blamed all Cyrillic, the cause was « »)
            usable = [cct] + [c for c in _CODECS if c != cct and units_cover(c)]
            best = min(
                usable,
                key=lambda c: sum(
                    len(unsupported_chars(t, c, known[k])) for k, t in changed.items()
                ),
            )
            for k, t in changed.items():
                bad = unsupported_chars(t, best, known[k])
                if bad:
                    named.add(k)
                    problems.append(
                        f"subtitle {k + 1}: characters no EBU STL code table (00-04) can hold "
                        "together with the rest of the file: " + " ".join(map(repr, bad[:8]))
                    )
            chosen = best  # judge the other subtitles against the same table
        new_tf: dict[int, tuple] = {}  # group first block -> (tf, vp delta)
        for k, (g, sub, text) in enumerate(units):
            if k in changed:
                tf, dvp, why = _encode_subtitle(sub, changed[k], chosen, mnc, keep_rows=False)
            elif chosen != cct:
                tf, dvp, why = _encode_subtitle(sub, text, chosen, mnc, keep_rows=True)
            else:
                continue
            if tf is None:
                if k not in named:
                    named.add(k)
                    problems.append(f"subtitle {k + 1}: {why}")
                continue
            new_tf[g.blocks[0]] = (g, tf, dvp)
        if chosen != cct and not refusing:
            # every subtitle can move here: chosen is another table only when unmovable() is empty
            for g, sub, text in others:
                if not sub.rows:
                    continue  # only control codes: the bytes stay as they are
                tf, dvp, why = _encode_subtitle(sub, text, chosen, mnc, keep_rows=True)
                if tf is None:
                    problems.append(f"comment subtitle at block {g.blocks[0] + 1}: {why}")
                    continue
                new_tf[g.blocks[0]] = (g, tf, 0)
        if problems:
            more = f" (and {len(problems) - 10} more)" if len(problems) > 10 else ""
            raise CaptionTargetError(
                "The EBU STL file was not exported: "
                + "; ".join(problems[:10])
                + more
                + ". Shorten these subtitles (or check the target language) and export again."
            )
        out_blocks: list[bytes] = []
        skip: set[int] = set()
        teletext = raw[11:12] in (b"1", b"2")
        for i, blk in enumerate(blocks):
            if i in skip:
                continue
            if i in new_tf:
                g, tf, dvp = new_tf[i]
                headers = [bytearray(blocks[b][:16]) for b in g.blocks]
                if dvp:
                    vp = headers[0][13] - dvp
                    headers[0][13] = max(1 if teletext else 0, min(vp, 0xFF))
                built = _blocks_for(tf, [bytes(h) for h in headers], chosen)
                if len(built) > 0xF0:
                    raise CaptionTargetError(
                        f"The EBU STL file was not exported: subtitle at block {i + 1} needs "
                        "more than 240 extension blocks."
                    )
                out_blocks.extend(built)
                skip.update(g.blocks)
                continue
            out_blocks.append(blk)
        gsi = bytearray(raw[:GSI_SIZE])
        if chosen != cct:
            gsi[12:14] = chosen.encode("ascii")
        if len(out_blocks) != len(blocks):
            if len(out_blocks) > MAX_TTI_BLOCKS:
                raise CaptionTargetError("The EBU STL file was not exported: over 99999 blocks.")
            gsi[238:243] = f"{len(out_blocks):05d}".encode("ascii")
        if lc is not None:
            gsi[14:16] = f"{lc:02X}".encode("ascii")
        return bytes(gsi) + b"".join(out_blocks)

    def build(self, original: bytes, paragraph_texts: dict) -> bytes:
        return self._rebuild(original, paragraph_texts, None)

    def build_localized(
        self, original: bytes, paragraph_texts: dict, target_locale: str | None = None
    ) -> bytes:
        lc = language_code(target_locale) if target_locale else None
        return self._rebuild(original, paragraph_texts, lc)
