"""SRT and WebVTT subtitle filter.

An .srt file is a sequence of cues:

    1
    00:00:01,000 --> 00:00:04,000
    Hello world.

Only the text lines are translated; the cue number and the ``-->`` timecode are
preserved. Multi-line cue text is joined with a newline into one unit and split
back out on build.

WebVTT cue markup (#737). In a .vtt file (first line ``WEBVTT``) every cue tag is a
protected token in the unit text (the shared services/segment scheme), so a translation
that drops or adds one is caught by the tag-damage gate instead of shipping a cue with a
lost karaoke timing or a renamed speaker:

* a class / italic / bold / underline / voice / language / ruby / ruby-text span
  (``<c.loud>..</c>``, ``<i>``, ``<b>``, ``<u>``, ``<v Roger>..</v>``, ``<lang fr>``,
  ``<ruby>..<rt>..</rt></ruby>``) whose close tag is in the same cue is a PAIRED token
  ``⟦gN⟧..⟦/gN⟧``; an open tag left unclosed (``<v Roger>`` without ``</v>`` is common),
  a karaoke timestamp tag ``<00:00:08.000>`` and any other tag are point tokens ``⟦xN⟧``;
* leading and trailing tokens are kept out of the unit (``<v Roger>Hi.</v>`` gives the
  unit ``Hi.``), as in the ASS filter, so the voice name is never sent to the engine;
* build writes each token back as its exact source tag (classes, voice name, language
  annotation verbatim), pairs balanced (a stray close is dropped, an unclosed open is
  closed at the end of the cue), and escapes a bare ``<`` / ``&`` / ``-->`` the
  translation brought in (``&lt;`` ``&amp;`` ``--&gt;``). Character references
  (``&amp;`` ``&lt;`` ``&gt;`` ``&lrm;`` ``&rlm;`` ``&nbsp;``, numeric) stay as they are
  in the unit text and are never escaped twice.

A document imported before #737 stored the raw tag text as its unit text; its
translations still carry raw tags. Build recognises such a translation (raw VTT tags, no
token markers) and writes it as it is, without the hoisted source tags, so old documents
export as before. A cue untouched by the translation is written exactly as before.

SRT override blocks (#738). Players (VLC, mpv, MPC-HC, most TVs) honour ASS/SSA override
blocks in SRT cue text: ``{\\an8}`` (top position), ``{\\i1}..{\\i0}``, ``{\\b1}``,
``{\\c&H00FFFF&}``. Every ``{\\...}`` block in an SRT cue is a protected POINT token
``⟦xN⟧`` (the ASS codes are toggles, not well-formed pairs); leading and trailing blocks
stay out of the unit like a WebVTT edge tag, so ``{\\an8}Hi.`` gives the unit ``Hi.``.
Build writes each token back as its exact source block (unknown or repeated markers are
dropped), so a translation that drops or adds one is caught by the tag-damage gate. A
``{...}`` with no backslash is plain text. A pre-#738 translation that holds raw blocks and
no token markers is written as it is. SRT ``<i>`` / ``<b>`` / ``<font>`` stay plain unit
text (see below).

The ``Language:`` header of a WebVTT file (the header block after ``WEBVTT``) is rewritten
to the target language on a localized export; a file without one gets none added. SRT has
no HTML tag tokens (its ``<i>`` / ``<b>`` / ``<font>`` are standard HTML elements the
#719 HTML tag check already judges; only its ``{\\...}`` blocks are tokens) and no
language header.

Spec: W3C WebVTT (www.w3.org/TR/webvtt1/#cue-text).

Documented in: docs/architecture/translation-pipeline.md
"""

import re

from app.filters.base import (
    Filter,
    ParseResult,
    TextUnit,
    check_segment_cap,
    decode_text_lossless,
    encode_like,
    note_source_encoding,
    read_text_upload,
    register,
    text_envelope,
)

_TIMECODE = re.compile(r"-->")
#: WebVTT structural blocks that are NEVER translatable cue text and must survive
#: byte-identical: NOTE (comments), STYLE (CSS), REGION (layout). The file signature
#: block ("WEBVTT ...") is handled separately (it is only the first block).
_VTT_KEYWORDS = ("NOTE", "STYLE", "REGION")

# --- WebVTT cue tags (#737) -------------------------------------------------------
#: Any cue tag as the WebVTT cue-text parser reads it: `<` up to the next `>`.
_VTT_TAG = re.compile(r"<[^<>\n]*>")
_VTT_OPEN = re.compile(r"<(c|i|b|u|v|lang|ruby|rt)((?:\.[^\s.<>]*)*)(?:[ \t\f][^<>]*)?>")
_VTT_CLOSE = re.compile(r"</(c|i|b|u|v|lang|ruby|rt)[ \t\f]*>")
#: A well-formed cue tag a translation may carry as raw text (a pre-#737 document): a span
#: open / close or a timestamp. Any other `<` in a translation is text and is escaped.
_VTT_KNOWN = re.compile(
    r"</?(?:c|i|b|u|v|lang|ruby|rt)(?:\.[^\s.<>]+)*(?:[ \t\f][^<>\n]*)?>"
    r"|<(?:[0-9]{2,}:)?[0-5][0-9]:[0-5][0-9]\.[0-9]{3}>"
)
#: An ASS/SSA override block in SRT cue text (#738): `{\` up to the next `}`.
_SRT_OVERRIDE = re.compile(r"\{\\[^{}\n]*\}")
#: A style toggle inside an override block: `\i1` / `\i0`, `\b1` / `\b700`, `\u`, `\s`, `\r`.
_SRT_TOGGLE = re.compile(r"\\(?:[ibus]\d|r(?![a-z]))")
#: A token marker the translation may carry (unknown ones are dropped at build).
_ANY_MARKER = re.compile(r"⟦/?[a-z]?\d*⟧")
#: `&` that does NOT start a character reference (named or numeric).
_BARE_AMP = re.compile(r"&(?!(?:#[0-9]+|#[xX][0-9a-fA-F]+|[A-Za-z][A-Za-z0-9]*);)")
_LANGUAGE_HEADER = re.compile(r"^(Language[ \t]*:[ \t]*)([^\n]*?)([ \t]*)$", re.IGNORECASE)


def _is_signature(line: str) -> bool:
    """True for a WebVTT file-signature line: the literal ``WEBVTT`` optionally followed
    by whitespace + descriptive text (the spec permits ``WEBVTT - Some title``). A .vtt
    file MUST begin with this line, and it is NOT a translatable unit."""
    t = line.rstrip("\n").rstrip()
    return t == "WEBVTT" or line.startswith("WEBVTT ") or line.startswith("WEBVTT\t")


def _is_structural(first: str) -> bool:
    """True when a block's first line makes the whole block a WebVTT metadata block
    (NOTE / STYLE / REGION) that is preserved verbatim, never translated."""
    for kw in _VTT_KEYWORDS:
        if first == kw or first.startswith(kw + " ") or first.startswith(kw + "\t"):
            return True
    return False


def _parse_cues(data: bytes, layout: dict | None = None):
    """Yield dicts: {'header': [lines before text], 'text': [text lines], 'vtt': bool}.

    Shared by SRT and VTT. A cue's header is its leading identifier line (SRT numeric
    index or a WebVTT string id) and/or its ``-->`` timecode line; only the remaining
    lines are translatable text. WebVTT structural blocks -- the ``WEBVTT`` signature
    (first block) and any NOTE/STYLE/REGION metadata block -- are emitted as all-header,
    all-verbatim (no translatable text), so the signature line and metadata round-trip
    intact instead of being rewritten as ``[xx-XX] WEBVTT``. The signature block is
    flagged ``'signature': True`` (it holds the ``Language:`` header).

    B-bytes: each cue also carries 'pre', the exact text between the previous cue and
    this one (blank lines as written, skipped blank-only blocks), and `layout["tail"]`
    receives what follows the last cue, so build() keeps every untouched byte."""
    s = decode_text_lossless(data, sig=True).replace("\r\n", "\n").replace("\r", "\n")
    core = s.strip("\n")
    lead = s[: len(s) - len(s.lstrip("\n"))]
    pieces = re.split(r"(\n[ \t]*\n)", core)
    pending = lead
    vtt = False
    for bi, block in enumerate(pieces[0::2]):
        sep = pieces[2 * bi + 1] if 2 * bi + 1 < len(pieces) else ""
        lines = block.split("\n")
        if not any(l.strip() for l in lines):  # noqa: E741
            pending += block + sep
            continue
        pre, pending = pending, sep
        first = lines[0].strip()
        if bi == 0 and _is_signature(lines[0]):
            vtt = True
            yield {"header": list(lines), "text": [], "vtt": vtt, "signature": True, "pre": pre}
            continue
        # WebVTT structural blocks are verbatim: NOTE/STYLE/REGION metadata blocks.
        if _is_structural(first):
            yield {"header": list(lines), "text": [], "vtt": vtt, "pre": pre}
            continue
        ti = 0
        # An optional cue-identifier line: a non-timecode line IMMEDIATELY followed by a
        # timecode line (the SRT numeric index, or a WebVTT string id). Kept as header.
        if (
            ti < len(lines)
            and not _TIMECODE.search(lines[ti])
            and ti + 1 < len(lines)
            and _TIMECODE.search(lines[ti + 1])
        ):
            ti += 1
        # The timecode line itself.
        if ti < len(lines) and _TIMECODE.search(lines[ti]):
            ti += 1
        yield {"header": lines[:ti], "text": lines[ti:], "vtt": vtt, "pre": pre}
    if layout is not None:
        layout["tail"] = pending + s[len(lead) + len(core) :]


def _cue_text(new: str, source: str) -> str:
    """#713: a blank line ends a cue in SRT and WebVTT, so a translation with an empty line
    (an engine answer, or one typed in the editor) split the cue and players dropped the text
    after it. Empty lines are removed (CR line breaks count as line breaks); a translation
    with no text left keeps the source cue text."""
    lines = (new or "").replace("\r\n", "\n").replace("\r", "\n").split("\n")
    kept = "\n".join(line for line in lines if line.strip())
    return kept or source


def _vtt_pairs(pieces) -> dict[int, int]:
    """{open piece index: close piece index} for span tags closed in the same cue. A close
    pairs only with the innermost open tag of its name (`</ruby>` first ends an open `<rt>`,
    as the WebVTT parser does); anything else stays a point token, so the tokens are always
    well-formed."""
    stack: list[tuple[int, str]] = []
    pairs: dict[int, int] = {}
    for k, (tok, s) in enumerate(pieces):
        if not tok:
            continue
        m = _VTT_OPEN.fullmatch(s)
        if m:
            stack.append((k, m.group(1)))
            continue
        m = _VTT_CLOSE.fullmatch(s)
        if not m:
            continue
        name = m.group(1)
        if name == "ruby" and stack and stack[-1][1] == "rt":
            stack.pop()  # an <rt> left open is ended by </ruby>: it stays a point token
        if stack and stack[-1][1] == name:
            pairs[stack.pop()[0]] = k
    return pairs


def _vtt_bounds(pieces, pinned: set[int]) -> tuple[int, int]:
    lo, hi = 0, len(pieces)
    while lo < hi:
        tok, s = pieces[lo]
        if (tok and lo not in pinned) or (not tok and not s.strip()):
            lo += 1
            continue
        break
    while hi > lo:
        tok, s = pieces[hi - 1]
        if (tok and hi - 1 not in pinned) or (not tok and not s.strip()):
            hi -= 1
            continue
        break
    return lo, hi


def _vtt_tokenize(text: str):
    """(prefix, core, suffix, tokens) for one WebVTT cue text. `core` is the unit text with
    ``⟦gN⟧``/``⟦/gN⟧``/``⟦xN⟧`` for each tag; tokens maps the marker name to its source
    tag. prefix/suffix are the leading/trailing tags (and the whitespace next to them)
    kept out of the unit. A cue with no tags returns (``""``, text, ``""``, {})."""
    pieces: list[tuple[bool, str]] = []
    pos = 0
    for m in _VTT_TAG.finditer(text):
        if m.start() > pos:
            pieces.append((False, text[pos : m.start()]))
        pieces.append((True, m.group(0)))
        pos = m.end()
    if pos < len(text):
        pieces.append((False, text[pos:]))
    if not any(tok for tok, _s in pieces):
        return "", text, "", {}
    pairs = _vtt_pairs(pieces)
    if any(not tok and s.strip() for tok, s in pieces):
        pinned: set[int] = set()
        while True:
            lo, hi = _vtt_bounds(pieces, pinned)
            more = set()
            for o, c in pairs.items():
                if (lo <= o < hi) != (lo <= c < hi):  # one end in the unit, one hoisted
                    more |= {o, c}
            if more <= pinned:
                break
            pinned |= more
    else:
        lo, hi = 0, len(pieces)  # tags only: keep them all in the unit
    prefix = "".join(s for _t, s in pieces[:lo])
    suffix = "".join(s for _t, s in pieces[hi:])
    core_pieces = list(pieces[lo:hi])
    if core_pieces and not core_pieces[0][0]:
        s = core_pieces[0][1]
        prefix += s[: len(s) - len(s.lstrip())]
        core_pieces[0] = (False, s.lstrip())
    if core_pieces and not core_pieces[-1][0]:
        s = core_pieces[-1][1]
        suffix = s[len(s.rstrip()) :] + suffix
        core_pieces[-1] = (False, s.rstrip())
    closes = {c: o for o, c in pairs.items() if lo <= o < hi}
    ids: dict[int, int] = {}
    tokens: dict[str, str] = {}
    core: list[str] = []
    n = 0
    for k, (tok, s) in enumerate(core_pieces, start=lo):
        if not tok:
            core.append(s)
        elif k in closes:
            tid = ids[closes[k]]
            tokens[f"/g{tid}"] = s
            core.append(f"⟦/g{tid}⟧")
        else:
            n += 1
            if k in pairs and pairs[k] < hi:
                ids[k] = n
                tokens[f"g{n}"] = s
                core.append(f"⟦g{n}⟧")
            else:
                tokens[f"x{n}"] = s
                core.append(f"⟦x{n}⟧")
    return prefix, "".join(core), suffix, tokens


def _vtt_escape(text: str) -> str:
    """Escape what the translation brought into WebVTT cue text: a bare `&` (one that does
    not start a character reference), a `<` that does not open a WebVTT span or timestamp
    tag, and `-->` (which would end the cue). Such raw tags pass through unchanged."""
    out: list[str] = []
    pos = 0

    def plain(s: str) -> str:
        s = _BARE_AMP.sub("&amp;", s).replace("<", "&lt;")
        return s.replace("-->", "--&gt;")

    for m in _VTT_KNOWN.finditer(text):
        if "-->" in m.group(0):
            continue  # gate: `<b -->` / `<c.x-->` would smuggle a cue-ending `-->` through
        out.append(plain(text[pos : m.start()]))
        out.append(m.group(0))
        pos = m.end()
    out.append(plain(text[pos:]))
    return "".join(out)


def _vtt_detokenize(translated: str, tokens: dict[str, str]) -> str:
    """Each marker back to its exact source tag. Pairs are written balanced: a close with
    no matching open is dropped, an open left unclosed is closed at the end of the cue; a
    repeated marker or one that names no source token is dropped (the tag-damage gate
    reports such damage on the segment)."""
    out: list[str] = []
    stack: list[str] = []
    used: set[str] = set()
    pos = 0
    for m in _ANY_MARKER.finditer(translated):
        out.append(_vtt_escape(translated[pos : m.start()]))
        pos = m.end()
        name = m.group(0)[1:-1]
        if name.startswith("/"):
            if stack and stack[-1] == name[1:]:
                stack.pop()
                out.append(tokens[name])
            continue
        if name in tokens and name not in used:
            used.add(name)
            out.append(tokens[name])
            if name[0] == "g":
                stack.append(name)
    out.append(_vtt_escape(translated[pos:]))
    while stack:
        out.append(tokens["/" + stack.pop()])
    return "".join(out)


def _vtt_cue(text: str, translated: str) -> str:
    """The new WebVTT cue text for source cue `text` and its (tokenized) translation."""
    prefix, _core, suffix, tokens = _vtt_tokenize(text)
    tagged = bool(tokens or prefix.strip() or suffix.strip())
    if tagged and not _ANY_MARKER.search(translated) and _VTT_KNOWN.search(translated):
        # imported before #737: the unit held the raw tags, so does its translation
        return _cue_text(_vtt_escape(translated), text)
    return _cue_text(prefix + _vtt_detokenize(translated, tokens) + suffix, text)


def _srt_tokenize(text: str):
    """#738: (prefix, core, suffix, tokens) for one SRT cue text. Every ``{\\...}``
    override block is a point token ``⟦xN⟧``; leading/trailing blocks (and the whitespace
    next to them) are kept out of the unit. A cue with no block returns
    (``""``, text, ``""``, {}); a cue holding only blocks keeps them all in the unit."""
    pieces: list[tuple[bool, str]] = []
    pos = 0
    for m in _SRT_OVERRIDE.finditer(text):
        if m.start() > pos:
            pieces.append((False, text[pos : m.start()]))
        pieces.append((True, m.group(0)))
        pos = m.end()
    if pos < len(text):
        pieces.append((False, text[pos:]))
    if not any(tok for tok, _s in pieces):
        return "", text, "", {}
    if any(not tok and s.strip() for tok, s in pieces):
        lo, hi = _vtt_bounds(pieces, set())
        # Gate: a style toggle at the edge (`{\i1}Watch{\i0} your step`) whose counterpart
        # stays inside the unit is kept in the unit too. Hoisted alone, the engine never sees
        # where the span starts, and dropping the inner `{\i0}` turned the whole cue italic.
        if any(pieces[k][0] for k in range(lo, hi)):
            pinned = {
                k
                for k in (*range(lo), *range(hi, len(pieces)))
                if pieces[k][0] and _SRT_TOGGLE.search(pieces[k][1])
            }
            if pinned:
                lo, hi = _vtt_bounds(pieces, pinned)
    else:
        lo, hi = 0, len(pieces)
    prefix = "".join(s for _t, s in pieces[:lo])
    suffix = "".join(s for _t, s in pieces[hi:])
    core_pieces = list(pieces[lo:hi])
    if core_pieces and not core_pieces[0][0]:
        s = core_pieces[0][1]
        prefix += s[: len(s) - len(s.lstrip())]
        core_pieces[0] = (False, s.lstrip())
    if core_pieces and not core_pieces[-1][0]:
        s = core_pieces[-1][1]
        suffix = s[len(s.rstrip()) :] + suffix
        core_pieces[-1] = (False, s.rstrip())
    tokens: dict[str, str] = {}
    core: list[str] = []
    for tok, s in core_pieces:
        if tok:
            name = f"x{len(tokens) + 1}"
            tokens[name] = s
            core.append(f"⟦{name}⟧")
        else:
            core.append(s)
    return prefix, "".join(core), suffix, tokens


def _srt_cue(text: str, translated: str) -> str:
    """#738: the new SRT cue text for source cue `text` and its (tokenized) translation.
    Each ``⟦xN⟧`` becomes its source block once; an unknown or repeated marker is dropped
    (the tag-damage gate reports it on the segment). SRT text is never escaped."""
    prefix, _core, suffix, tokens = _srt_tokenize(text)
    if not (tokens or prefix or suffix):
        return _cue_text(translated, text)
    if not _ANY_MARKER.search(translated) and _SRT_OVERRIDE.search(translated):
        # imported before #738: the unit held the raw blocks, so does its translation
        return _cue_text(translated, text)
    out: list[str] = []
    used: set[str] = set()
    pos = 0
    for m in _ANY_MARKER.finditer(translated):
        out.append(translated[pos : m.start()])
        pos = m.end()
        name = m.group(0)[1:-1]
        if name in tokens and name not in used:
            used.add(name)
            out.append(tokens[name])
    out.append(translated[pos:])
    return _cue_text(prefix + "".join(out) + suffix, text)


def _stamp_language(lines: list[str], target_locale) -> list[str]:
    """#737: the WebVTT header block's `Language:` line (case-insensitive key) is set to
    the target language tag. A header without one gets none added."""
    from app.filters import a11y

    tag = a11y.target_tag(target_locale)
    if not tag:
        return lines
    out = list(lines)
    for i, line in enumerate(out):
        if i == 0 or _TIMECODE.search(line):
            continue
        m = _LANGUAGE_HEADER.match(line)
        if m:
            out[i] = m.group(1) + tag + m.group(3)
    return out


@register
class SrtFilter(Filter):
    fmt = "srt"
    exts = ("vtt",)

    def parse(self, data: bytes, provider: str | None = None) -> ParseResult:
        # #509: Windows-1252 is converted (the build decode reads it the same way);
        # an unreadable file is refused with a clear message.
        _text, enc = read_text_upload(data, sig=True)
        units: list[TextUnit] = []
        idx = 0
        for cue in _parse_cues(data):
            text = "\n".join(cue["text"]).strip()
            if text:
                text = (_vtt_tokenize if cue["vtt"] else _srt_tokenize)(text)[1]
                units.append(TextUnit(p_index=idx, text=text))
                idx += 1
                check_segment_cap(idx)  # #571: stop before building the rest
        skeleton = {
            "format": "srt",
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
        # B-bytes: an untouched cue is written exactly as it was (with its blank-line
        # spacing); the file keeps its BOM, line endings and code page (when it fits).
        out: list[str] = []
        layout: dict = {}
        k = 0
        for cue in _parse_cues(original, layout):
            text = "\n".join(cue["text"]).strip()
            lines = list(cue["header"])
            if cue.get("signature") and target_locale:
                lines = _stamp_language(lines, target_locale)
            changed = lines != cue["header"]
            if text:
                new = paragraph_texts.get(str(k))
                k += 1
                if new is None:
                    lines.append(text)
                elif cue["vtt"]:
                    if str(new) == _vtt_tokenize(text)[1]:
                        lines.append(text)  # untouched: the source cue text
                    else:
                        lines.append(_vtt_cue(text, str(new)))
                        changed = True
                elif str(new) == _srt_tokenize(text)[1]:
                    lines.append(text)  # untouched: the source cue text
                else:
                    lines.append(_srt_cue(text, str(new)))
                    changed = True
            else:
                lines.extend(cue["text"])
            out.append(cue["pre"])
            out.append("\n".join(lines) if changed else "\n".join(cue["header"] + cue["text"]))
        out.append(layout.get("tail", ""))
        return encode_like("".join(out), text_envelope(original))
