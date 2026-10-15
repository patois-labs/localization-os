"""Scenarist SCC caption filter (CEA-608 line-21 closed captions as hex byte pairs).

An .scc file is ASCII text: the header ``Scenarist_SCC V1.0``, then caption lines

    00:00:01:15<TAB>94ae 94ae 9420 9420 9470 9470 c845 4c4c 4f80 942f 942f

Each line is an SMPTE timecode (``;`` before the frames = drop-frame) and a run of
4-hex-digit words. A word is two CEA-608 bytes with odd parity in bit 7. After the
parity bit is stripped, a first byte 0x20-0x7F carries two basic characters; a first
byte 0x10-0x1F is a two-byte code: a preamble address code (PAC: row, column, colour),
a mid-row code (colour/italics/underline, displayed as a space), a miscellaneous
control code (RCL/RU2-4/RDC/CR/EOC/EDM/ENM/...), a tab offset, or a special or
extended character. Control codes are conventionally sent twice; a decoder ignores
an immediate repeat (the redundancy rule).

Units: the caption text of one SCC line (pop-on, roll-up and paint-on alike), rows
joined with ``\\n``. Every code is preserved: the codes before a row's text (mode,
PAC, tab offset, leading mid-row) stay with the row, codes after the text (EOC,
EDM...) stay at the end of the line, and a mid-row code inside a row is a protected
point token ``⟦xN⟧`` (shared services/segment scheme). An untouched line is written
back byte for byte; only a line whose text changed is re-encoded.

Re-encoding (build): the translation must fit CEA-608. Every character must exist
in the 608 basic, special or extended set (extended characters are sent after a
basic fallback character, which they replace). Rows are at most 32 cells: the
translator's own line breaks are kept when they fit, otherwise the text is re-wrapped
into the caption's rows (balanced), growing to at most 4 rows for pop-on/paint-on or
the roll-up depth for roll-up; a row's column moves left when its text would run past
column 32. Characters 608 cannot show (most non-Latin scripts) and text that still
does not fit are NOT written: the export is refused with a list of each problem
caption (timecode + reason). Typographic quotes, dashes and ellipses are mapped to
their 608 equivalents first.

Not units (kept verbatim): lines with text on data channel 2 (CC2/CC4), lines that
backspace or erase in the middle of text (BS, DER, or several captions in one line),
and lines with no text. Timecodes are never changed; a longer translation takes
longer to transmit (one word per frame), which a real caption tool should check.

Spec references: 47 CFR 79.101 (FCC decoder rules, CEA-608 character and code
tables), CTA-608-E; Scenarist SCC layout as documented by McPoodle's SCC tools.

Documented in: docs/architecture/translation-pipeline.md
"""

import re
import unicodedata

from app.filters.base import (
    Filter,
    InputLimitError,
    ParseResult,
    TextUnit,
    check_segment_cap,
    check_unit_texts,
    decode_text_lossless,
    read_text_upload,
    register,
)
from app.filters.xml_text import XmlTargetError

HEADER = "Scenarist_SCC V1.0"
ROW_CELLS = 32
POPON_MAX_ROWS = 4
_TC_RE = re.compile(r"^(\d{2}:\d{2}:\d{2}[:;.,]\d{2})([ \t]+)(.*)$")
_WORD_RE = re.compile(r"^[0-9A-Fa-f]{4}$")
_TOKEN_RE = re.compile(r"⟦x(\d+)⟧")
# A pre-parse budget: words per line. Real lines carry well under 100 words.
MAX_WORDS_PER_LINE = 4096


class SccParseError(ValueError):
    """The file is not a readable Scenarist SCC file."""


class CaptionTargetError(XmlTargetError):
    """A translation cannot be written in the caption format (a character the format
    cannot represent, or text that does not fit). Shown to the user on export."""


# --- CEA-608 character tables (47 CFR 79.101; CTA-608-E) -----------------------------

# Basic set: ASCII except these positions.
_BASIC_DIFF = {
    0x2A: "á",
    0x5C: "é",
    0x5E: "í",
    0x5F: "ó",
    0x60: "ú",
    0x7B: "ç",
    0x7C: "÷",
    0x7D: "Ñ",
    0x7E: "ñ",
    0x7F: "█",
}
BASIC = {b: _BASIC_DIFF.get(b, chr(b)) for b in range(0x20, 0x80)}
# Special characters: first byte 0x11 (channel 1), second byte 0x30-0x3F.
SPECIAL = dict(zip(range(0x30, 0x40), "®°½¿™¢£♪à èâêîôû", strict=True))
# Extended characters: first byte 0x12 / 0x13, second byte 0x20-0x3F.
EXT12 = dict(zip(range(0x20, 0x40), "ÁÉÓÚÜü‘¡*'—©℠•“”ÀÂÇÈÊËëÎÏïÔÙùÛ«»", strict=True))
EXT13 = dict(zip(range(0x20, 0x40), "ÃãÍÌìÒòÕõ{}\\^_¦~ÄäÖöß¥¤|ÅåØø┌┐└┘", strict=True))

# Encoder: char -> ("b", byte) | ("s", b2) | ("e", b1, b2). Basic wins over extended.
_ENC: dict[str, tuple] = {}
for _b2, _c in EXT13.items():
    _ENC[_c] = ("e", 0x13, _b2)
for _b2, _c in EXT12.items():
    _ENC[_c] = ("e", 0x12, _b2)
for _b2, _c in SPECIAL.items():
    _ENC[_c] = ("s", _b2)
for _b, _c in BASIC.items():
    _ENC[_c] = ("b", _b)
_FALLBACK = {"‘": "'", "—": "-", "©": "c", "℠": "s", "•": ".", "“": '"', "”": '"', "«": '"'}
_FALLBACK.update({"»": '"', "{": "(", "}": ")", "\\": "/", "^": "'", "_": "-", "¦": "!"})
_FALLBACK.update({"~": "-", "ß": "s", "¥": "Y", "¤": "o", "|": "!", "*": "."})
_FALLBACK.update(dict.fromkeys("┌┐└┘", "+"))
# Typographic characters with a plain 608 equivalent (applied before encoding).
_NORMALIZE = {
    "’": "'",
    "ʼ": "'",
    "–": "-",
    "‒": "-",
    "−": "-",
    "‐": "-",
    "‑": "-",
    "…": "...",
    " ": " ",
    " ": " ",
    " ": " ",
    "​": "",
    "­": "",
}

# PAC rows: (first byte, second-byte base) per row 1-15 (channel 1).
_PAC_ROWS = {
    1: (0x11, 0x40),
    2: (0x11, 0x60),
    3: (0x12, 0x40),
    4: (0x12, 0x60),
    5: (0x15, 0x40),
    6: (0x15, 0x60),
    7: (0x16, 0x40),
    8: (0x16, 0x60),
    9: (0x17, 0x40),
    10: (0x17, 0x60),
    11: (0x10, 0x40),
    12: (0x13, 0x40),
    13: (0x13, 0x60),
    14: (0x14, 0x40),
    15: (0x14, 0x60),
}
_ROW_OF = {v: k for k, v in _PAC_ROWS.items()}

_MISC = {
    0x20: "RCL",
    0x21: "BS",
    0x22: "AON",
    0x23: "AOF",
    0x24: "DER",
    0x25: "RU2",
    0x26: "RU3",
    0x27: "RU4",
    0x28: "FON",
    0x29: "RDC",
    0x2A: "TR",
    0x2B: "RTD",
    0x2C: "EDM",
    0x2D: "CR",
    0x2E: "ENM",
    0x2F: "EOC",
}
# Codes that may sit between two rows of one caption (row breaks).
_ROW_BREAK = {"PAC", "CR"}
# Codes that make a line too complex to re-encode when they appear after its text started.
_COMPLEX = {"BS", "DER", "EOC", "EDM", "ENM", "RCL", "RDC", "RU2", "RU3", "RU4", "TR", "RTD"}


def _parity(b: int) -> int:
    return b | 0x80 if bin(b).count("1") % 2 == 0 else b


def _word(b1: int, b2: int) -> str:
    return f"{_parity(b1):02x}{_parity(b2):02x}"


class _Ev:
    """One decoded event: a character cell or a (possibly doubled) control code."""

    __slots__ = ("basic", "ch", "char", "info", "kind", "name", "words")

    def __init__(self, kind, words, *, name="", char="", basic=False, info=None, ch=1):
        self.kind = kind  # "char" | "ctrl"
        self.name = name  # ctrl: PAC MID TO CR RU2 EOC ... OTHER XDS
        self.words = words  # raw words (ctrl only)
        self.char = char
        self.basic = basic
        self.info = info
        self.ch = ch


def _classify(b1: int, b2: int):
    """(name, info) of a two-byte code with first byte 0x10-0x1F (parity stripped)."""
    c = b1 & 0x17
    if b2 >= 0x40:
        row = _ROW_OF.get((c, b2 & 0x60))
        if row is not None:
            return "PAC", (row, b2 & 0x1F)
        return "OTHER", None
    if c == 0x11 and 0x20 <= b2 <= 0x2F:
        return "MID", b2
    if c == 0x11 and 0x30 <= b2 <= 0x3F:
        return "SPECIAL", SPECIAL[b2]
    if c == 0x12 and 0x20 <= b2 <= 0x3F:
        return "EXT", EXT12[b2]
    if c == 0x13 and 0x20 <= b2 <= 0x3F:
        return "EXT", EXT13[b2]
    if c in (0x14, 0x15) and 0x20 <= b2 <= 0x2F:
        return _MISC[b2], None
    if c == 0x17 and 0x21 <= b2 <= 0x23:
        return "TO", b2 - 0x20
    return "OTHER", None


def _events(words: list[str], state: dict):
    """Decode a line's words into events. `state` carries the data channel across lines."""
    out: list[_Ev] = []
    prev_ctrl = None  # the previous control word, for the redundancy rule
    for w in words:
        b1, b2 = int(w[:2], 16) & 0x7F, int(w[2:], 16) & 0x7F
        if 0x10 <= b1 <= 0x1F:
            if prev_ctrl == w.lower():
                out[-1].words.append(w)  # the redundant second copy
                prev_ctrl = None
                continue
            prev_ctrl = w.lower()
            ch = 2 if b1 & 0x08 else 1
            state["ch"] = ch
            name, info = _classify(b1, b2)
            if name in ("SPECIAL", "EXT"):
                out.append(_Ev("char", [w], name=name, char=info, ch=ch))
            else:
                out.append(_Ev("ctrl", [w], name=name, info=info, ch=ch))
            continue
        prev_ctrl = None
        if b1 < 0x10 and b1 != 0:
            out.append(_Ev("ctrl", [w], name="XDS", ch=state["ch"]))  # XDS / unknown
            continue
        for b in (b1, b2):
            if b >= 0x20:
                out.append(_Ev("char", None, char=BASIC[b], basic=True, ch=state["ch"]))
    return out


class _Row:
    __slots__ = ("cells", "prefix")

    def __init__(self, prefix):
        self.prefix = prefix  # list[_Ev] (controls before the row's text)
        self.cells = []  # list[_Ev] (chars, and inline control tokens)


class _Line:
    """One caption line: timecode, separator, words, and the decoded row structure."""

    def __init__(self, tc, sep, words, state):
        self.tc, self.sep, self.words = tc, sep, words
        self.mode = state.get("mode", "popon")
        self.depth = state.get("depth", 2)
        evs = _events(words, state)
        self.rows: list[_Row] = []
        self.tail: list[_Ev] = []
        self.unit = True
        cur = None
        pending: list[_Ev] = []
        for ev in evs:
            if ev.kind == "ctrl" and ev.name in ("RU2", "RU3", "RU4"):
                state["mode"], state["depth"] = "rollup", int(ev.name[2])
            elif ev.kind == "ctrl" and ev.name == "RCL":
                state["mode"] = "popon"
            elif ev.kind == "ctrl" and ev.name == "RDC":
                state["mode"] = "painton"
            if ev.kind != "char":
                pending.append(ev)
                continue
            if ev.ch != 1:
                self.unit = False
            if cur is None:
                cur = _Row(pending)
                self.rows.append(cur)
            elif pending:
                names = {p.name for p in pending}
                if names & _COMPLEX:
                    self.unit = False
                if names & _ROW_BREAK:
                    cur = _Row(pending)
                    self.rows.append(cur)
                else:
                    cur.cells.extend(pending)  # inline tokens (mid-row, tab offset, other)
            pending = []
            if (
                ev.name == "EXT"
                and cur.cells
                and cur.cells[-1].kind == "char"
                and cur.cells[-1].basic
            ):
                cur.cells[-1] = ev  # an extended character replaces its fallback
            else:
                cur.cells.append(ev)
        self.tail = pending
        # the mode in force for this caption is the one after its own head codes
        if self.rows:
            self.mode = state.get("mode", "popon")
            self.depth = state.get("depth", 2)
        if any(p.name == "BS" or p.name == "DER" for r in self.rows for p in r.cells):
            self.unit = False

    def texts(self):
        """(row texts, token map {n: _Ev}) with inline controls as ⟦xN⟧ tokens."""
        rows, tokens = [], {}
        for r in self.rows:
            parts = []
            for c in r.cells:
                if c.kind == "char":
                    parts.append(c.char)
                else:
                    n = len(tokens) + 1
                    tokens[n] = c
                    if c.name in ("MID", "OTHER") and parts and parts[-1] != " ":
                        parts.append(" ")  # the code occupies a cell shown as a space
                    parts.append(f"⟦x{n}⟧")
            rows.append("".join(parts))
        return rows, tokens

    def text(self):
        rows, _ = self.texts()
        return "\n".join(r.strip() for r in rows).strip("\n")


def _split_lines(text: str):
    """[(content, eol)] keeping each line's own line ending."""
    parts = re.split(r"(\r\n|\n|\r)", text)
    return [(parts[i], parts[i + 1] if i + 1 < len(parts) else "") for i in range(0, len(parts), 2)]


def _read(data: bytes, *, upload: bool):
    raw = bytes(data)
    bom = raw.startswith(b"\xef\xbb\xbf")
    if upload:
        text, _enc = read_text_upload(raw, sig=True)
    else:
        text = decode_text_lossless(raw, sig=True)
    lines = _split_lines(text)
    first = next((i for i, (c, _e) in enumerate(lines) if c.strip()), None)
    if first is None or lines[first][0].strip() != HEADER:
        raise SccParseError(f"Not a Scenarist SCC file: the first line must be '{HEADER}'.")
    state = {"ch": 1, "mode": "popon", "depth": 2}
    entries = []  # (content, eol, _Line | None)
    for i, (content, eol) in enumerate(lines):
        if i <= first or not content.strip():
            entries.append((content, eol, None))
            continue
        m = _TC_RE.match(content.strip())
        if not m:
            raise SccParseError(f"SCC line {i + 1} is not 'timecode<TAB>hex words'; refused.")
        words = m.group(3).split()
        if len(words) > MAX_WORDS_PER_LINE:
            raise InputLimitError(
                f"SCC line {i + 1} has more than {MAX_WORDS_PER_LINE} code words; refused."
            )
        if not all(_WORD_RE.match(w) for w in words):
            raise SccParseError(f"SCC line {i + 1} has a word that is not 4 hex digits; refused.")
        entries.append((content, eol, _Line(m.group(1), m.group(2), words, state)))
    return bom, entries


def _units(entries):
    """(entry index, _Line, text) of every translatable line, in file order."""
    out = []
    for i, (_c, _e, line) in enumerate(entries):
        if line is None or not line.unit or not line.rows:
            continue
        text = line.text()
        if text.strip():
            out.append((i, line, text))
    return out


# --- encoding ------------------------------------------------------------------------


def normalize_608(text: str) -> str:
    text = unicodedata.normalize("NFC", text)
    return "".join(_NORMALIZE.get(c, c) for c in text)


def unsupported_chars(text: str) -> list[str]:
    """Characters of `text` (tokens excluded) that CEA-608 cannot represent."""
    plain = _TOKEN_RE.sub("", normalize_608(text)).replace("\n", "")
    seen = []
    for c in plain:
        if c not in _ENC and c not in seen:
            seen.append(c)
    return seen


def _atoms(line: str, tokens: dict):
    """A row's text as atoms: ('c', char) and ('t', _Ev); unknown tokens are dropped."""
    out = []
    pos = 0
    for m in _TOKEN_RE.finditer(line):
        out.extend(("c", c) for c in line[pos : m.start()])
        tok = tokens.get(int(m.group(1)))
        if tok is not None:
            out.append(("t", tok))
        pos = m.end()
    out.extend(("c", c) for c in line[pos:])
    return out


def _cells(atoms) -> int:
    n = 0
    prev_space = False
    for kind, v in atoms:
        if kind == "c":
            n += 1
            prev_space = v == " "
        elif v.name == "TO":
            n += v.info
            prev_space = False
        else:  # MID / OTHER / XDS: one cell, consuming a preceding space
            if not prev_space:
                n += 1
            prev_space = False
    return n


def _encode_atoms(atoms) -> list[str]:
    """Words for a row's atoms (characters packed two per word, codes doubled)."""
    words: list[str] = []
    half: list[int] = []  # a pending basic byte

    def flush():
        if half:
            words.append(_word(half[0], 0x00) if len(half) == 1 else _word(half[0], half[1]))
            half.clear()

    def basic(b):
        half.append(b)
        if len(half) == 2:
            flush()

    for i, (kind, v) in enumerate(atoms):
        if kind == "t":
            if v.name in ("MID", "OTHER") and i and atoms[i - 1] == ("c", " "):
                # the code takes the space's cell: drop that space byte
                if half:
                    half.pop()
                elif words and words[-1][2:] == "20":
                    words[-1] = words[-1][:2] + "80"
            flush()
            words.extend(v.words)
            continue
        enc = _ENC[v]
        if enc[0] == "b":
            basic(enc[1])
        elif enc[0] == "s":
            flush()
            words.extend([_word(0x11, enc[1])] * 2)
        else:
            fb = _FALLBACK.get(v) or unicodedata.normalize("NFD", v)[0]
            basic(_ENC[fb][1] if fb in _ENC and _ENC[fb][0] == "b" else 0x20)
            flush()
            words.extend([_word(enc[1], enc[2])] * 2)
    flush()
    return words


def _wrap(words: list[str], width: int, measure) -> list[str]:
    lines: list[str] = []
    cur = ""
    for w in words:
        cand = f"{cur} {w}" if cur else w
        if measure(cand) <= width or not cur:
            cur = cand
        else:
            lines.append(cur)
            cur = w
    if cur:
        lines.append(cur)
    return lines


def _layout(text: str, n_rows: int, max_rows: int, tokens: dict):
    """Rows for a translation, or None when it cannot fit."""

    def measure(s):
        return _cells(_atoms(s, tokens))

    own = [r.strip() for r in text.split("\n") if r.strip()]
    if own and len(own) <= max_rows and all(measure(r) <= ROW_CELLS for r in own):
        return own
    words = " ".join(own).split()
    widths = [measure(w) for w in words]
    if not words or max(widths) > ROW_CELLS:
        return None
    if sum(widths) > ROW_CELLS * max_rows:
        # gate (train-9): cannot fit whatever the breaks (a row is at least the sum of its
        # words); refuse now instead of re-wrapping a huge text once per width (seconds each)
        return None
    target = max(1, min(n_rows, max_rows))
    for width in range(1, ROW_CELLS + 1):  # the narrowest width that keeps the row count
        rows = _wrap(words, width, measure)
        if len(rows) <= target and all(measure(r) <= ROW_CELLS for r in rows):
            return rows
    rows = _wrap(words, ROW_CELLS, measure)
    return rows if len(rows) <= max_rows else None


def _row_pos(row: _Row):
    """(head events, position events, row number or None, PAC attr, column, leading
    spaces). The column is where the row's first cell sits (PAC indent + tab offset)."""
    lead = 0
    for c in row.cells:
        if c.kind != "char" or c.char != " ":
            break
        lead += 1
    idx = max((i for i, p in enumerate(row.prefix) if p.name == "PAC"), default=None)
    if idx is None:
        cr = max((i for i, p in enumerate(row.prefix) if p.name == "CR"), default=-1)
        return row.prefix[: cr + 1], row.prefix[cr + 1 :], None, None, 0, lead
    head, pos = row.prefix[:idx], row.prefix[idx:]
    rnum, attr = pos[0].info
    col = ((attr >> 1) & 0x07) * 4 if attr & 0x10 else 0
    col += sum(p.info for p in pos if p.name == "TO")
    return head, pos, rnum, attr, col, lead


def _pac_words(rnum: int, attr: int, col: int, rest) -> tuple[list[str], int]:
    """New position words: PAC (+ tab offset) for row `rnum` at column `col`, and the
    number of leading spaces still needed (a colour PAC can only start at column 0)."""
    if col == 0:  # any PAC style starts at column 0: keep it
        to, pad = 0, 0
    elif attr & 0x10 or (attr & 0x0E) == 0:  # an indent PAC, or plain white: indent allowed
        attr = 0x10 | ((col // 4) << 1) | (attr & 0x01)
        to, pad = col % 4, 0
    else:  # a colour / italics PAC sits at column 0
        to = min(col, 3)
        pad = col - to
    b1, base = _PAC_ROWS[rnum]
    words = [_word(b1, base | attr)] * 2
    if to:
        words += [_word(0x17, 0x20 + to)] * 2
    for p in rest:
        if p.name not in ("PAC", "TO"):
            words.extend(p.words)
    return words, pad


def _encode_line(line: _Line, text: str):
    """(words, None) or (None, reason) for a translated caption line."""
    text = normalize_608(text)
    bad = unsupported_chars(text)
    if bad:
        shown = " ".join(repr(c) for c in bad[:8])
        return None, f"characters CEA-608 cannot show: {shown}"
    _rows, tokens = line.texts()
    n = len(line.rows)
    max_rows = max(n, line.depth if line.mode == "rollup" else POPON_MAX_ROWS)
    rows = _layout(text, n, max_rows, tokens)
    if rows is None:
        return None, f"does not fit in {max_rows} rows of {ROW_CELLS} characters"
    m = len(rows)
    src = [_row_pos(r) for r in line.rows]
    bottom = src[-1][2]
    out: list[str] = []
    cr_words = next((e.words for s in src for e in s[0] if e.name == "CR"), [_word(0x14, 0x2D)] * 2)
    for i, row_text in enumerate(rows):
        atoms = _atoms(row_text, tokens)
        width = _cells(atoms)
        j = i - (m - n)  # rows align at the bottom
        head, pos, rnum, attr, col, lead = src[j] if j >= 0 else src[0]
        if i == 0:
            head = src[0][0]
        elif j < 1:  # a new row: roll-up moves up with a carriage return
            head = [] if line.mode != "rollup" else [_Ev("ctrl", list(cr_words), name="CR")]
        words = [w for e in head for w in e.words]
        if rnum is None:  # no PAC (roll-up continuing at the cursor)
            words += [w for e in pos for w in e.words]
        else:
            new_r = rnum
            if line.mode != "rollup" and bottom is not None and m != n:
                new_r = min(max(1, bottom - (m - 1)) + i, 15)
            new_c = min(col + lead, ROW_CELLS - width)
            if new_r == rnum and new_c == col and lead == 0 and j >= 0:
                words += [w for e in pos for w in e.words]
            else:
                pw, pad = _pac_words(new_r, attr, new_c, pos[1:])
                words += pw
                atoms = [("c", " ")] * pad + atoms
        out.extend(words)
        out.extend(_encode_atoms(atoms))
    out.extend(w for e in line.tail for w in e.words)
    return out, None


@register
class SccFilter(Filter):
    fmt = "scc"

    def parse(self, data: bytes, provider: str | None = None) -> ParseResult:
        _bom, entries = _read(data, upload=True)
        units: list[TextUnit] = []
        for k, (_i, _line, text) in enumerate(_units(entries)):
            units.append(TextUnit(p_index=k, text=text))
            check_segment_cap(k + 1)
        check_unit_texts(u.text for u in units)
        skipped = sum(1 for _c, _e, ln in entries if ln is not None and ln.rows and not ln.unit)
        skeleton = {
            "format": "scc",
            "units": [{"p_index": u.p_index, "text": u.text, "style": ""} for u in units],
        }
        if skipped:
            skeleton["scc_lines_kept_verbatim"] = skipped
        return ParseResult(skeleton=skeleton, units=units)

    def build(self, original: bytes, paragraph_texts: dict) -> bytes:
        bom, entries = _read(original, upload=False)
        replaced: dict[int, str] = {}
        problems: list[str] = []
        for k, (i, line, text) in enumerate(_units(entries)):
            new = paragraph_texts.get(str(k))
            if new is None or new == text:
                continue
            words, why = _encode_line(line, new)
            if words is None:
                problems.append(f"caption {k + 1} at {line.tc}: {why}")
                continue
            content = entries[i][0]
            lead = content[: len(content) - len(content.lstrip())]
            replaced[i] = f"{lead}{line.tc}{line.sep}{' '.join(words)}"
        if problems:
            more = f" (and {len(problems) - 10} more)" if len(problems) > 10 else ""
            raise CaptionTargetError(
                "The SCC file was not exported: CEA-608 captions allow only Latin characters "
                f"from the 608 set and {ROW_CELLS} characters per row. "
                + "; ".join(problems[:10])
                + more
                + ". Shorten or transliterate these captions and export again."
            )
        body = "".join(replaced.get(i, c) + e for i, (c, e, _l) in enumerate(entries))
        return (b"\xef\xbb\xbf" if bom else b"") + body.encode("utf-8", "surrogateescape")
