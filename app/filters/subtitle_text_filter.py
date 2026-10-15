"""Plain-text subtitle filters: YouTube SBV (.sbv), SubViewer 2.0 and MicroDVD (.sub).

Modelled on ``srt_filter`` (text-only units, timecodes untouched, a multi-line cue is one
unit with its lines joined by ``\\n``), but span-based for byte fidelity: parse records
where each cue's text sits in the decoded file, and build replaces ONLY those spans. Every
other byte (timecodes, headers, blank lines, CRLF, trailing whitespace, a UTF-8 BOM) is
written back exactly as it was.

SBV (YouTube "SubViewer" captions, plain UTF-8, no style markup)::

    0:00:00.599,0:00:04.160
    >> ALICE: Hi, my name is Alice

SubViewer 2.0 (.sub): an optional ``[INFORMATION] ... [END INFORMATION]`` header and
``[COLF]``/``[STYLE]``/``[SUBTITLE]`` tag lines, then ``hh:mm:ss.xx,hh:mm:ss.xx`` timecode
lines, each followed by text where ``[br]`` is a line break; cues are separated by a blank
line. Header and tag lines are never translated.

MicroDVD (.sub): one cue per line, ``{start}{end}text`` in FRAMES, ``|`` is a line break,
``{y:i}``/``{c:$BBGGRR}``/``{f:Font}``/``{s:12}``/``{P:x,y}`` control codes are kept as
protected point tokens (``⟦x1⟧``). A lowercase (line-scoped) code with words after it
on its line is a PAIRED token around those words, ``⟦g1⟧words⟦/g1⟧`` (#680), so a
translation that reorders lines moves the style with its words; build writes the code
where ``⟦g1⟧`` lands (again after a line break inside the pair); a pair the translation
breaks loses its code and keeps its words. A ``{1}{1}23.976`` frame-rate line and
``{DEFAULT}`` lines are not translated.

``.sub`` is shared by both and by binary VobSub (image subtitles with a ``.idx``), so the
content is sniffed. A file that looks like BOTH SubViewer and MicroDVD, or like neither
(SubViewer 1.0 included), is refused with a plain message rather than guessed at.

Documented in: docs/architecture/translation-pipeline.md
"""

import re

from app.filters.base import (
    EmptyDocumentError,
    Filter,
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
from app.services.segment import (
    _PLACEHOLDER_RE,
    ph_close,
    ph_open,
    ph_point,
    strip_placeholders,
)

_BOM = b"\xef\xbb\xbf"
# One line's content plus its terminator (CRLF, CR or LF; none on the last line).
_LINE_RE = re.compile(r"([^\r\n]*)(\r\n|\r|\n|$)")
_NEWLINE_RE = re.compile(r"\r\n|\r|\n")
# SBV and SubViewer 2.0 share the timecode shape: H:MM:SS.fff,H:MM:SS.fff (SBV writes
# milliseconds, SubViewer 2.0 hundredths; 1-3 fraction digits are accepted for both).
_TIME_PAIR = re.compile(
    r"^[ \t]*\d{1,2}:\d{2}:\d{2}\.\d{1,3}[ \t]*,[ \t]*\d{1,2}:\d{2}:\d{2}\.\d{1,3}[ \t]*$"
)
_SV_BR = re.compile(r"\[br\]", re.IGNORECASE)
_SV1_TIME = re.compile(r"^[ \t]*\[\d{2}:\d{2}:\d{2}\][ \t]*$")
_MDVD_CUE = re.compile(r"^([ \t]*)\{(\d{1,10})\}\{(\d{0,10})\}")
# A control code: {y:i}, {Y:b,u}, {c:$0000FF}, {f:Arial}, {s:12}, {P:10,20}, {H:Greek}.
_MDVD_CODE = re.compile(r"\{[A-Za-z]:[^{}\r\n]{0,200}\}")
_MDVD_LEAD = re.compile(r"(?:[ \t]*\{[A-Za-z]:[^{}\r\n]{0,200}\})*[ \t]*")
_FPS_TEXT = re.compile(r"^[ \t]*\d{1,3}(?:\.\d{1,6})?[ \t]*$")
_VOBSUB_MAGIC = b"\x00\x00\x01\xba"  # MPEG-PS pack header: binary VobSub
_SNIFF_LINES = 400


class UnrecognizedSubtitleError(EmptyDocumentError):
    """A ``.sub`` file whose content is not readable SubViewer 2.0 or MicroDVD text.
    An EmptyDocumentError subclass so every surface shows the message (API 422)."""


def _lines(text: str):
    """Yield (start, end, content) for each line; end excludes the line terminator."""
    for m in _LINE_RE.finditer(text):
        if m.start() == len(text) and not m.group(0):
            break
        yield m.start(1), m.end(1), m.group(1)
        if not m.group(2):
            break


def _first_newline(text: str) -> str:
    m = _NEWLINE_RE.search(text)
    return m.group(0) if m else "\n"


def _core(text: str, start: int, end: int) -> tuple[int, int]:
    """Shrink [start, end) to exclude leading/trailing whitespace (kept verbatim)."""
    while start < end and text[start].isspace():
        start += 1
    while end > start and text[end - 1].isspace():
        end -= 1
    return start, end


# --------------------------------------------------------------------------- sniffing


def sniff_sub(data: bytes) -> str:
    """Return "subviewer" or "microdvd" for a .sub upload, or raise a plain refusal."""
    raw = bytes(data)
    if raw[:4] == _VOBSUB_MAGIC:
        raise UnrecognizedSubtitleError(
            "This .sub file is a VobSub image subtitle (binary, paired with a .idx file), "
            "not text. Convert it to SRT with an OCR tool, then upload the .srt."
        )
    text = decode_text_lossless(raw, sig=True)
    micro = sv = sv1 = 0
    seen = 0
    for _s, _e, line in _lines(text):
        if not line.strip():
            continue
        seen += 1
        if _MDVD_CUE.match(line):
            micro += 1
        elif _TIME_PAIR.match(line):
            sv += 1
        elif _SV1_TIME.match(line):
            sv1 += 1
        if seen >= _SNIFF_LINES:
            break
    if micro and sv:
        raise UnrecognizedSubtitleError(
            "This .sub file mixes MicroDVD ({frame}{frame}text) and SubViewer "
            "(hh:mm:ss.xx,hh:mm:ss.xx) lines, so its format is ambiguous. "
            "Save it as one format (or as .srt) and upload it again."
        )
    if micro:
        return "microdvd"
    if sv:
        return "subviewer"
    if sv1:
        raise UnrecognizedSubtitleError(
            "This .sub file is SubViewer 1.0 ([hh:mm:ss] time lines), which is not "
            "supported. Save it as SubViewer 2.0 or .srt and upload it again."
        )
    raise UnrecognizedSubtitleError(
        "This .sub file is not MicroDVD ({frame}{frame}text) or SubViewer 2.0 "
        "(hh:mm:ss.xx,hh:mm:ss.xx) subtitles. Save it as .srt and upload it again."
    )


# --------------------------------------------------------------------------- slots
# A slot is one translatable cue: {"start", "end"} offsets of its text core in the
# decoded file, "text" (the unit text), "map" (point-token id -> original code) and
# "joiner" (what a line break in a translation is written as).


def _timed_block_slots(text: str, *, subviewer: bool) -> list[dict]:
    """SBV and SubViewer 2.0: a timecode line, then text lines up to a blank line (or
    the next timecode line). Lines before the first timecode (the SubViewer header) and
    every timecode line are verbatim."""
    slots: list[dict] = []
    nl = _first_newline(text)
    cur: list[tuple[int, int, str]] | None = None

    def close():
        if not cur:
            return
        start, end = _core(text, cur[0][0], cur[-1][1])
        if start >= end:
            return
        raw = text[start:end]
        unit = _NEWLINE_RE.sub("\n", raw)
        if subviewer:
            unit = _SV_BR.sub("\n", unit)
            br = _SV_BR.search(raw)
            joiner = br.group(0) if br else (_first_newline(raw) if "\n" in unit else "[br]")
        else:
            joiner = _first_newline(raw) if "\n" in unit else nl
        unit = "\n".join(p.strip() for p in unit.split("\n"))
        if not unit.strip():
            return
        slots.append({"start": start, "end": end, "text": unit, "map": {}, "joiner": joiner})
        check_segment_cap(len(slots))

    for s, e, line in _lines(text):
        if _TIME_PAIR.match(line):
            close()
            cur = []
        elif not line.strip():
            close()
            cur = None
        elif cur is not None:
            cur.append((s, e, line))
    close()
    return slots


def _microdvd_slots(text: str) -> list[dict]:
    slots: list[dict] = []
    for s, e, line in _lines(text):
        m = _MDVD_CUE.match(line)
        if not m:
            continue  # {DEFAULT} lines, [BEGIN]/[END], junk: verbatim
        body_start = s + m.end()
        start, end = _core(text, body_start, e)
        if start >= end:
            continue
        body = text[start:end]
        first, last = m.group(2), m.group(3)
        if first in ("0", "1") and last in ("0", "1") and _FPS_TEXT.match(body):
            continue  # {1}{1}23.976 frame-rate declaration
        # Codes that open the cue apply to its first line (or, uppercase, the whole cue):
        # they stay verbatim OUTSIDE the unit so a translation can never move them. In a
        # cue of several lines a lowercase one styles only the first line's words, so it
        # (and what follows it) stays in the unit as a pair around them (#680).
        lead = _MDVD_LEAD.match(body)
        if lead and lead.end():
            cut = lead.end()
            if "|" in _MDVD_CODE.sub("", body[cut:]):
                for cm in _MDVD_CODE.finditer(body, 0, cut):
                    if cm.group(0)[1].islower():
                        cut = cm.start()
                        break
            start, end = _core(text, start + cut, end)
            if start >= end:
                continue  # codes only, nothing to translate
            body = text[start:end]
        imap, unit, heads = _microdvd_unit(body)
        if not strip_placeholders(unit).strip():
            continue  # codes only, nothing to translate
        slots.append(
            {"start": start, "end": end, "text": unit, "map": imap, "joiner": "|", "heads": heads}
        )
        check_segment_cap(len(slots))
    return slots


def _microdvd_unit(body: str) -> tuple[dict, str, set]:
    """A MicroDVD cue body (leading codes already outside) as (map, unit text).

    A lowercase code (`{y:i}`, `{c:$BBGGRR}`, `{f:Font}`, `{s:12}`) styles the rest of
    its own line, so a run of them followed by words on that line is a PAIRED token
    around those words, `⟦g1⟧words⟦/g1⟧` (#680): a translation that reorders lines or
    words moves the style with its words. Uppercase (whole-cue) codes, position codes
    and codes with no words after them on the line stay point tokens `⟦xN⟧`. Point
    tokens map "N" -> code; pairs map "gN" -> codes and "/gN" -> "" (no close code).
    The third value names the pairs whose code opens its line in the source."""
    lines: list[list[tuple[bool, str]]] = [[]]

    def add_text(s: str) -> None:
        for j, seg in enumerate(s.split("|")):
            if j:
                lines.append([])
            if seg:
                lines[-1].append((False, seg))

    pos = 0
    for cm in _MDVD_CODE.finditer(body):
        add_text(body[pos : cm.start()])
        lines[-1].append((True, cm.group(0)))
        pos = cm.end()
    add_text(body[pos:])
    imap: dict = {}
    heads: set[str] = set()
    n = 0
    out_lines: list[str] = []
    for line in lines:
        out: list[str] = []
        opened: list[int] = []
        k = 0
        while k < len(line):
            if not line[k][0]:
                out.append(line[k][1])
                k += 1
                continue
            j = k
            while j < len(line) and line[j][0]:
                j += 1
            run = [s for _c, s in line[k:j]]
            rest = "".join(s for c, s in line[j:] if not c)
            styles = [code for code in run if code[1].islower()] if rest.strip() else []
            for code in run:
                if code not in styles:
                    n += 1
                    imap[str(n)] = code
                    out.append(ph_point("x", n))
            if styles:
                n += 1
                imap[f"g{n}"] = "".join(styles)
                imap[f"/g{n}"] = ""
                if not strip_placeholders("".join(out)).strip():
                    heads.add(str(n))
                out.append(ph_open("g", n))
                opened.append(n)
            k = j
        # Spaces around a | break are layout, not text (the SBV path strips per line too).
        out_lines.append(
            "".join(out).strip(" \t") + "".join(ph_close("g", g) for g in reversed(opened))
        )
    return imap, "\n".join(out_lines), heads


def _microdvd_text(t: str, slot: dict) -> str:
    """Put a translated MicroDVD unit's codes back. A paired style code is written where
    its ⟦gN⟧ is, at the start of that line when it opened its line in the source (a
    line-scoped code is read at a line start), and again at the start of each line that
    begins inside the pair; a pair MT emptied is dropped. A pair the translation does not
    write whole (its first ⟦gN⟧ and the first ⟦/gN⟧ after it) loses its style code and
    keeps its words, so a code never styles a line it did not style (#680 gate: the
    tolerance of the other filters, never a 409)."""
    imap = slot["map"]
    pairs = {key[1:] for key in imap if key[:1] == "g"}
    matches = list(_PLACEHOLDER_RE.finditer(t))
    opens: dict[str, int] = {}
    closes: dict[str, int] = {}
    for k, m in enumerate(matches):
        inner = m.group(0)[1:-1]
        slash, name = inner.startswith("/"), inner.lstrip("/")
        if name[:1] != "g" or name[1:] not in pairs:
            continue
        if not slash:
            opens.setdefault(name[1:], k)
        elif name[1:] in opens:
            closes.setdefault(name[1:], k)
    live = {num: (opens[num], closes[num]) for num in closes}
    empty = set()  # pairs MT emptied: no style code around nothing
    for num, (a, b) in live.items():
        if not strip_placeholders(t[matches[a].end() : matches[b].start()]).strip():
            empty.add(num)
    heads = slot.get("heads", set())
    out: list[str] = [""]  # out[head]: the style codes that open the current line
    head = 0
    open_pairs: list[str] = []

    def text(s: str) -> None:
        nonlocal head
        for j, seg in enumerate(re.split(r"\r\n|\r|\n", s)):
            if j:
                out.append("\n")
                head = len(out)
                out.append("".join(imap[f"g{num}"] for num in open_pairs))
            out.append(seg)

    pos = 0
    for k, m in enumerate(matches):
        text(t[pos : m.start()])
        pos = m.end()
        inner = m.group(0)[1:-1]
        num = inner.lstrip("/")[1:]
        if inner[:1] == "x":
            out.append(imap.get(inner[1:], ""))  # unknown tokens are dropped
        elif num not in live or num in empty or k not in live[num]:
            continue  # not part of a pair written whole: dropped
        elif inner[:1] == "g":
            if num in heads:
                out[head] += imap[inner]
            else:
                out.append(imap[inner])
            open_pairs.append(num)
        else:
            open_pairs.remove(num)
    text(t[pos:])
    return "".join(out).strip()


def _slots(text: str, dialect: str) -> list[dict]:
    if dialect == "microdvd":
        return _microdvd_slots(text)
    return _timed_block_slots(text, subviewer=dialect == "subviewer")


def _render(translation: str, slot: dict, dialect: str) -> str:
    """Write a translated unit back in the file's own syntax."""
    t = translation.strip()
    if dialect == "microdvd":
        t = _microdvd_text(t, slot)
    # A line break never becomes a blank line (which would end an SBV/SubViewer cue) or
    # a raw newline (which would end a MicroDVD cue line).
    return re.sub(r"[ \t]*(?:\r\n|\r|\n)+[ \t]*", lambda _m: slot["joiner"], t)


def _parse(data: bytes, fmt: str, dialect: str) -> ParseResult:
    text, enc = read_text_upload(data, sig=True)  # #509 refusal + #571 line budget
    slots = _slots(text, dialect)
    check_unit_texts([s["text"] for s in slots])
    units = [
        TextUnit(p_index=i, text=s["text"], inline_map=dict(s["map"])) for i, s in enumerate(slots)
    ]
    skeleton = {
        "format": fmt,
        "dialect": dialect,
        "units": [{"p_index": u.p_index, "text": u.text, "style": ""} for u in units],
    }
    return ParseResult(skeleton=note_source_encoding(skeleton, enc), units=units)


def _build(original: bytes, paragraph_texts: dict, dialect: str) -> bytes:
    raw = bytes(original)
    text = decode_text_lossless(raw, sig=True)
    out: list[str] = []
    pos = 0
    for i, slot in enumerate(_slots(text, dialect)):
        new = paragraph_texts.get(str(i))
        out.append(text[pos : slot["start"]])
        if new is None or new == slot["text"]:
            out.append(text[slot["start"] : slot["end"]])
        else:
            out.append(_render(new, slot, dialect))
        pos = slot["end"]
    out.append(text[pos:])
    body = encode_text_lossless("".join(out))
    return _BOM + body if raw.startswith(_BOM) else body


@register
class SbvFilter(Filter):
    """YouTube SubViewer captions (.sbv)."""

    fmt = "sbv"

    def parse(self, data: bytes, provider: str | None = None) -> ParseResult:
        return _parse(data, "sbv", "sbv")

    def build(self, original: bytes, paragraph_texts: dict) -> bytes:
        return _build(original, paragraph_texts, "sbv")


@register
class SubFilter(Filter):
    """SubViewer 2.0 or MicroDVD (.sub), told apart by content."""

    fmt = "sub"

    def parse(self, data: bytes, provider: str | None = None) -> ParseResult:
        return _parse(data, "sub", sniff_sub(data))

    def build(self, original: bytes, paragraph_texts: dict) -> bytes:
        return _build(original, paragraph_texts, sniff_sub(original))


__all__ = ["SbvFilter", "SubFilter", "UnrecognizedSubtitleError", "sniff_sub"]
