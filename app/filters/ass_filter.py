"""SSA / ASS subtitle filter (SubStation Alpha v4 ``.ssa`` and Advanced SubStation v4+ ``.ass``).

A script is an INI-like file of ``[Section]`` blocks. Only two things are translated:

* the ``Text`` field of every ``Dialogue:`` line in ``[Events]``. ``Text`` is the LAST
  field named by the section's ``Format:`` line (10 fields in both v4 and v4+), so the
  line is split on the first ``fields - 1`` commas only and the text may hold commas;
* the ``Title:`` header of ``[Script Info]`` (free text, informational, shown by some
  players and media libraries). ``<untitled>`` and a blank title are left alone.

Everything else is copied byte for byte: ``Comment:`` events, styles, fonts and graphics
sections, other headers, ``;`` comments, the other Dialogue fields, line endings (LF or
CRLF, per line) and a UTF-8 byte-order mark.

Inside the text, override blocks ``{...}`` (tags such as ``{\\i1}`` and brace comments)
and the escapes ``\\N`` (hard break), ``\\n`` (soft break) and ``\\h`` (hard space) are
protected as point tokens ``⟦xN⟧`` (the shared services/segment scheme). Leading and
trailing tokens/whitespace (a ``{\\an8\\pos(...)}`` positioning block, a trailing ``\\N``)
are kept out of the unit entirely so the translator only sees mid-line tokens. A style
switch turned on and later off in the same line (``{\\i1}word{\\i0}``, ``{\\b1}`` ...
``{\\b0}``, ``{\\u1}``/``{\\s1}`` ..., any of them ... ``{\\r}``) is a PAIRED token
``⟦gN⟧word⟦/gN⟧`` instead (#680), so a translation that reorders the words moves the
switch with them; a pair with one end inside the text pulls its other end into the unit.
Build writes pairs balanced (a stray close is dropped, an unclosed open is closed at the
end of the line, a pair MT emptied is dropped). Build re-derives the token map from the
stored original, so no side channel is needed.

A line in drawing mode (any override block with ``\\p`` > 0) holds vector commands, not
words, and is never a unit. Parse and build share ``_walk`` so units align by index.

Specs: libass ASS File Format Guide (github.com/libass/libass/wiki/ASS-File-Format-Guide),
Aegisub override tags (aegisub.org/docs/latest/ass_tags/).

Documented in: docs/architecture/translation-pipeline.md
"""

import re

from app.filters.base import (
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

_BOM = b"\xef\xbb\xbf"
# One protected token: an override block, or one of the three text escapes.
_TOKEN_RE = re.compile(r"\{[^}]*\}|\\[Nnh]")
# A drawing-mode switch inside an override block: \p1, \p2, ... (\p0 ends it).
_DRAWING_RE = re.compile(r"\\p\s*([0-9]+)")
_ANY_MARKER_RE = re.compile(r"⟦/?[a-z]?\d*⟧")
_EVENT_RE = re.compile(r"^(Dialogue[ \t]*:[ \t]*)(.*)$", re.DOTALL)
_FORMAT_RE = re.compile(r"^Format[ \t]*:(.*)$", re.IGNORECASE | re.DOTALL)
_TITLE_RE = re.compile(r"^(Title[ \t]*:[ \t]*)(.*?)([ \t]*)$", re.IGNORECASE | re.DOTALL)
_LINE_RE = re.compile(r"[^\r\n]*(?:\r\n|\r|\n)|[^\r\n]+\Z")
# libass reads a `Language:` Script Info header (two letters, a shaping hint).
_LANGUAGE_RE = re.compile(r"^(Language[ \t]*:[ \t]*)([^\r\n]*?)([ \t]*)$", re.IGNORECASE)
# Layer|Marked, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
_DEFAULT_FIELDS = 10


def _split_eol(line: str) -> tuple[str, str]:
    if line.endswith("\r\n"):
        return line[:-2], "\r\n"
    if line.endswith(("\n", "\r")):
        return line[:-1], line[-1]
    return line, ""


def _in_drawing_mode(text: str) -> bool:
    for m in _TOKEN_RE.finditer(text):
        tok = m.group(0)
        if tok.startswith("{"):
            for d in _DRAWING_RE.finditer(tok):
                if int(d.group(1)) > 0:
                    return True
    return False


def _tokenize(text: str):
    """Split a Text field into (prefix, core_with_tokens, suffix, tokens).

    prefix/suffix are the leading/trailing runs of protected tokens and whitespace,
    kept verbatim; core carries ``⟦xN⟧`` for each mid-line token (tokens[N-1] is its
    source). Returns None when nothing translatable is left (blank or tags only)."""
    pieces: list[tuple[bool, str]] = []  # (is_token, text)
    pos = 0
    for m in _TOKEN_RE.finditer(text):
        if m.start() > pos:
            pieces.append((False, text[pos : m.start()]))
        pieces.append((True, m.group(0)))
        pos = m.end()
    if pos < len(text):
        pieces.append((False, text[pos:]))
    if not any(not tok and s.strip() for tok, s in pieces):
        return None
    pairs = _pair_blocks(pieces)
    pinned: set[int] = set()
    while True:
        lo, hi = _core_bounds(pieces, pinned)
        more = set()
        for o, c in pairs.items():
            if (lo <= o < hi) != (lo <= c < hi):  # one end in the unit, one hoisted out
                more |= {o, c}
        if more <= pinned:
            break
        pinned |= more
    prefix = "".join(s for _t, s in pieces[:lo])
    suffix = "".join(s for _t, s in pieces[hi:])
    core_pieces = list(pieces[lo:hi])
    if core_pieces and not core_pieces[0][0]:  # whitespace hoisted off the edges
        s = core_pieces[0][1]
        prefix += s[: len(s) - len(s.lstrip())]
        core_pieces[0] = (False, s.lstrip())
    if core_pieces and not core_pieces[-1][0]:
        s = core_pieces[-1][1]
        suffix = s[len(s.rstrip()) :] + suffix
        core_pieces[-1] = (False, s.rstrip())
    tokens: dict[str, str] = {}
    core = []
    ids: dict[int, int] = {}
    closes_at: dict[int, list[int]] = {}
    for o, c in pairs.items():
        if lo <= o < hi:
            closes_at.setdefault(c, []).append(o)
    n = 0
    for k, (tok, s) in enumerate(core_pieces, start=lo):
        if not tok:
            core.append(s)
            continue
        if k not in closes_at:
            n += 1
        if k in closes_at:
            openers = sorted(closes_at[k], reverse=True)  # innermost first
            for o in openers:
                tid = ids[o]
                # one pair ends here: the source block; several: the inner ones their own
                # reset, the outermost the source block (a `{\r}` also resets colours and
                # fonts set in between, which must not run on past it: #680 gate)
                own = len(openers) == 1 or o == openers[-1]
                tokens[f"/g{tid}"] = s if own else _toggle_reset(pieces[o][1])
                core.append(f"⟦/g{tid}⟧")
        elif k in pairs and pairs[k] < hi:
            ids[k] = n
            tokens[f"g{n}"] = s
            core.append(f"⟦g{n}⟧")
        else:
            tokens[f"x{n}"] = s
            core.append(f"⟦x{n}⟧")
    return prefix, "".join(core), suffix, tokens


#: An override block of only bold / italic / underline / strikeout switches (#680):
#: `{\i1}` ... `{\i0}`, `{\b1}` (or a weight, `{\b700}`) ... `{\b0}`, `{\r}` resets all.
_TOGGLE_BLOCK_RE = re.compile(r"\{(?:\\(?:[ius][01]|b[0-9]{1,4}))+\}")
_TOGGLE_RE = re.compile(r"\\([iusb])([0-9]+)")


def _toggle_reset(open_block: str) -> str:
    keys = [m.group(1) for m in _TOGGLE_RE.finditer(open_block)]
    return "{" + "".join(f"\\{k}0" for k in reversed(keys)) + "}"


def _pair_blocks(pieces) -> dict[int, int]:
    """{opener piece index: closer piece index} for style switches turned on and later
    off in the same line (#680), so MT moves `{\\i1}word{\\i0}` with its word. A block
    that turns some switches on and others off, or an off block that resets only part of
    an opener or crosses another, leaves the openers it touches as point tokens."""
    stack: list[tuple[int, set]] = []
    pairs: dict[int, int] = {}
    for k, (tok, s) in enumerate(pieces):
        if not tok:
            continue
        if s == "{\\r}":
            on, off = set(), None  # None: every switch
        elif _TOGGLE_BLOCK_RE.fullmatch(s):
            ops = [(m.group(1), int(m.group(2)) != 0) for m in _TOGGLE_RE.finditer(s)]
            on = {key for key, v in ops if v}
            off = {key for key, v in ops if not v}
            if on and off:
                stack = [e for e in stack if not (e[1] & (on | off))]
                continue
        else:
            continue
        if on:
            stack.append((k, on))
            continue
        while stack and (off is None or any(e[1] & off for e in stack)):
            o, keys = stack.pop()
            if off is None or keys <= off:
                pairs[o] = k
    return pairs


def _core_bounds(pieces, pinned: set[int]) -> tuple[int, int]:
    """[lo, hi) of the pieces that stay in the unit: leading and trailing tokens and
    whitespace-only text are hoisted out, except pinned tokens (a pair end whose other
    end is in the unit)."""
    lo = 0
    while lo < len(pieces):
        tok, s = pieces[lo]
        if (tok and lo not in pinned) or (not tok and not s.strip()):
            lo += 1
            continue
        break
    hi = len(pieces)
    while hi > lo:
        tok, s = pieces[hi - 1]
        if (tok and hi - 1 not in pinned) or (not tok and not s.strip()):
            hi -= 1
            continue
        break
    return lo, hi


def _detokenize(translated: str, tokens: dict[str, str]) -> str:
    """Put each token back as its source block. Pairs are written balanced: a close
    with no open (or crossing another) is dropped, an open left unclosed is closed at
    the end of the line, a pair MT emptied is dropped. A line break typed into the
    translation becomes ``\\N`` (a raw newline would end the Dialogue line); a marker
    that names no source token is dropped (the export tag guard reports such damage)."""
    out: list[str] = []
    stack: list[tuple[str, int]] = []  # (open token name, len(out) after the open)
    used: set[str] = set()

    def close(name: str, mark: int):
        if all(not piece for piece in out[mark:]):
            del out[mark - 1 :]  # nothing between: no switch around nothing
        else:
            out.append(tokens["/" + name])

    pos = 0
    for m in _ANY_MARKER_RE.finditer(translated):
        out.append(translated[pos : m.start()])
        pos = m.end()
        name = m.group(0)[1:-1]
        if name.startswith("/"):
            if stack and stack[-1][0] == name[1:]:
                close(*stack.pop())
            continue
        if name in tokens and name not in used:
            used.add(name)
            out.append(tokens[name])
            if name[0] == "g":
                stack.append((name, len(out)))
    out.append(translated[pos:])
    while stack:
        close(*stack.pop())
    s = "".join(out)
    return s.replace("\r\n", "\\N").replace("\r", "\\N").replace("\n", "\\N")


def _walk(text: str):
    """Yield every line of the script as (kind, data):

    ("raw", line)  copied verbatim;
    ("unit", (head, tail_parts, eol, line)) where the line is head + prefix + core +
    suffix + eol and tail_parts = (prefix, core, suffix, tokens).

    Lines end only at CR, LF or CRLF (str.splitlines would also split inside a Text field
    at U+2028, U+0085, form feed and friends, cutting a subtitle in two)."""
    section = ""
    n_fields = _DEFAULT_FIELDS
    text_last = True
    for line in _LINE_RE.findall(text):
        body, eol = _split_eol(line)
        stripped = body.strip()
        if stripped.startswith("[") and stripped.endswith("]"):
            section = stripped[1:-1].strip().lower()
            if section == "events":
                n_fields, text_last = _DEFAULT_FIELDS, True
            yield "raw", line
            continue
        if section == "script info":
            m = _TITLE_RE.match(body)
            if m and m.group(2).strip() and m.group(2).strip().lower() != "<untitled>":
                yield "unit", (m.group(1), ("", m.group(2), m.group(3), []), eol, line)
                continue
        elif section == "events":
            fm = _FORMAT_RE.match(body)
            if fm:
                names = [f.strip().lower() for f in fm.group(1).split(",")]
                n_fields = max(len(names), 1)
                text_last = bool(names) and names[-1] == "text"
                yield "raw", line
                continue
            em = _EVENT_RE.match(body)
            if em and text_last:
                rest = em.group(2)
                parts = rest.split(",", n_fields - 1)
                if len(parts) == n_fields:
                    field = parts[-1]
                    check_unit_texts([field])  # one absurd line is refused before tokenizing
                    tk = None if _in_drawing_mode(field) else _tokenize(field)
                    if tk is not None:
                        head = em.group(1) + rest[: len(rest) - len(field)]
                        yield "unit", (head, tk, eol, line)
                        continue
        yield "raw", line


def _stamp_language(lines: list[str], target_locale) -> list[str]:
    """Localized export: set the [Script Info] `Language:` header (libass shaping hint,
    two-letter ISO 639-1) to the target language, adding one after the section header
    when the script has none. A target with no two-letter code drops a stale header."""
    from app.filters import a11y

    tag = a11y.target_tag(target_locale)
    if not tag:
        return lines
    lang = tag.split("-")[0].lower()
    two = lang if len(lang) == 2 and lang.isalpha() else ""
    out: list[str] = []
    section, info_at, found = "", -1, False
    for line in lines:
        body, eol = _split_eol(line)
        stripped = body.strip()
        if stripped.startswith("[") and stripped.endswith("]"):
            section = stripped[1:-1].strip().lower()
            out.append(line)
            if section == "script info" and info_at < 0:
                info_at = len(out)
            continue
        m = _LANGUAGE_RE.match(body) if section == "script info" else None
        if m:
            found = True
            if two:
                out.append(m.group(1) + two + m.group(3) + eol)
            continue
        out.append(line)
    if not found and two and info_at >= 0:
        eol = _split_eol(out[info_at - 1])[1] or "\n"
        out.insert(info_at, f"Language: {two}{eol}")
    return out


def _decode(data: bytes) -> str:
    return decode_text_lossless(data, sig=True)


@register
class AssFilter(Filter):
    fmt = "ass"
    exts = ("ssa",)

    def parse(self, data: bytes, provider: str | None = None) -> ParseResult:
        _text, enc = read_text_upload(data, sig=True)  # #509 decode + #571 line budget
        units: list[TextUnit] = []
        for kind, item in _walk(_decode(data)):
            if kind != "unit":
                continue
            core = item[1][1]
            units.append(TextUnit(p_index=len(units), text=core))
            check_segment_cap(len(units))
        check_unit_texts(u.text for u in units)
        skeleton = {
            "format": "ass",
            "units": [{"p_index": u.p_index, "text": u.text, "style": ""} for u in units],
        }
        return ParseResult(skeleton=note_source_encoding(skeleton, enc), units=units)

    def build(self, original: bytes, paragraph_texts: dict) -> bytes:
        return self._rebuild(original, paragraph_texts)

    def build_localized(
        self, original: bytes, paragraph_texts: dict, target_locale: str | None = None
    ) -> bytes:
        return self._rebuild(original, paragraph_texts, target_locale)

    def _rebuild(self, original: bytes, paragraph_texts: dict, target_locale=None) -> bytes:
        out: list[str] = []
        k = 0
        for kind, item in _walk(_decode(original)):
            if kind == "raw":
                out.append(item)
                continue
            head, (prefix, core, suffix, tokens), eol, line = item
            translated = paragraph_texts.get(str(k))
            k += 1
            if translated is None or str(translated) == core:
                out.append(line)  # untouched: the source line byte for byte
                continue
            new_core = _detokenize(str(translated), tokens)
            if not tokens and head.lower().startswith("title"):
                new_core = new_core.replace("\\N", " ")  # a header is one line of text
            out.append(head + prefix + new_core + suffix + eol)
        if target_locale:
            out = _stamp_language(out, target_locale)
        body = encode_text_lossless("".join(out))
        bom = bytes(original[:3]) == _BOM or bytes(original[:2]) in (b"\xff\xfe", b"\xfe\xff")
        return (_BOM + body) if bom else body
