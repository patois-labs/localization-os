"""QuarkXPress Tags (XPress Tags tagged text) filter: .xtg and .tag files.

XPress Tags is QuarkXPress's tagged plain-text interchange format (File > Save Text
with the "XPress Tags" format, File > Import with "Interpret XPress Tags"). A file is

    <v19.00><e9>
    @Body=[S"",""]<*L*h"Standard"f"Helvetica"z12>
    @Heading:Hello <B>bold<P> world<\\n>second line
    Text that keeps the previous paragraph style sheet.

  * The header `<vNN.NN><eN>` (version, encoding) is kept byte for byte. The `<e>`
    number names the file's encoding: 0 Mac Roman, 1 Windows Latin (1252), 2 ISO-8859-1,
    3 Japanese Windows (932), 6 Big5, 7 Simplified Chinese (GB2312; read as GBK, its
    superset), 8 Unicode UTF-16, 9 Unicode UTF-8, 19 Korean Windows (949). Japanese Mac
    (21) and Korean Mac (20) have no exact codec here and are refused with a re-save
    message. The file is decoded explicitly in that encoding and written back in it
    (#509); a translation the legacy encoding cannot hold switches the whole export to
    UTF-8 and rewrites the header to `<e9>`. A file with no `<e>` tag goes through the
    shared text decode (UTF-8, UTF-16 BOM, else Windows-1252).
  * Each line (hard return; CR, LF or CRLF, kept as written) is one paragraph. Style
    sheet definitions (`@Name=...`, `@Font set 1=[F]...`) and bookmark definitions
    (`~Name=<...>`) are not text. `@Name:`, `@$:` (Normal) and `@:` (No Style) at the
    start of a paragraph apply a style sheet; it is the unit's style and stays applied
    to later paragraphs until changed.
  * Inside a paragraph every tag `<...>` (character attributes `<B>` `<f"Font">` `<z12>`
    `<c"Red">` `<$>`, paragraph attributes `<*L>` `<*t(...)>`, character style sheets
    `<@Name>`, special characters `<\\n>` `<\\t>` `<\\i>` `<\\#13>`, index markers
    `<XI,...>`, hyperlinks and rubi `<A(...)>`) becomes a `⟦xN⟧` point token;
    consecutive tags share one token. Tags before the first text and after the last
    text stay outside the unit. The escapes `<\\@>` `<\\<>` `<\\\\>` `<\\>>` are text
    (the literal character) and are re-escaped on build.
  * A character attribute switched on and reset later in the same paragraph (`<B>`
    ... `<B$>`, `<I>` ... `<P>`, `<f"Arial">` ... `<f$>`, `<@Emph>` ... `<@$>`,
    anything ... `<$>`) is a PAIRED token `⟦gN⟧...⟦/gN⟧` around the words it formats
    (#680), so a translation that reorders words moves the formatting with them. A
    pair with one end inside the text pulls its other end into the unit (a reset after
    the last text is part of the unit); a pair around the whole text stays outside.
    What does not nest cleanly (a tag that both resets and sets, a partial or crossed
    reset, an attribute never reset) stays point tokens.

Build splices only paragraphs whose translation differs; an untouched file is
byte-identical. Each pair is written as its opening tags and, while a run's closes
stay together, the source's own reset tags (else the exact reset of that pair's
attributes, `<B$>` `<f$>` ...; a source `<$>` stays `<$>` on the outermost), so every
attribute is switched on and off around the moved words; a pair MT emptied is dropped.
A pair a translation does not write whole (its open or its close missing, or the close
first) loses its formatting and keeps its words; repeats are dropped, so an attribute
never runs on past its words. Point tokens
a translation dropped are appended at the end of the paragraph (index markers and
anchors are never lost); unknown and duplicate point tokens are dropped. Every rebuild
is re-lexed before it is returned.

Spec: Quark, "A Guide to XPress Tags 2023" (Understanding XPress Tags, Character and
Paragraph Formatting, Style Sheets, Special Characters + Encoding, Indexing).

Documented in: docs/architecture/translation-pipeline.md
"""

import re

from app.filters.base import (
    Filter,
    InputLimitError,
    ParseResult,
    TextUnit,
    UnsupportedEncodingError,
    check_line_budget,
    check_segment_cap,
    check_unit_texts,
    note_source_encoding,
    read_text_upload,
    register,
)

#: `<eN>` -> Python codec. None = known Quark encoding with no exact codec here.
_E_CODECS: dict[int, str | None] = {
    0: "mac_roman",
    1: "cp1252",
    2: "latin-1",
    3: "cp932",
    6: "big5",
    7: "gbk",
    8: "utf-16",
    9: "utf-8",
    19: "cp949",
    20: None,  # Korean Mac (KSC5601, Apple variant)
    21: None,  # Japanese Mac (Apple Shift-JIS variant)
}
_E_NAMES = {20: "Korean Mac", 21: "Japanese Mac"}

_HEAD_TAG_RE = re.compile(r"<(?:v[0-9][0-9.]{0,15}|e[0-9]{1,3})>")
_E_TAG_RE = re.compile(rb"^(?:<v[0-9][0-9.]{0,15}>)?[ \t]*<e([0-9]{1,3})>")
_LINE_RE = re.compile(r"[^\r\n]*(?:\r\n|\r|\n)?")
#: `@Name=` style sheet / font set definition; `~Name=<` bookmark definition. Style
#: sheet names cannot contain `" : = @` (Quark, Style Sheets).
_DEF_RE = re.compile(r'@[^":=@<\r\n]{0,255}=|~[^<>="\r\n]{1,255}=<')
_APPLY_RE = re.compile(r'@([^":=@<\r\n]{0,255}):')
#: `<\X>` one-character specials and `<\#nnn>` decimal character codes.
_SPECIAL_RE = re.compile(r"<\\(?:#[0-9]{1,7}|.)>", re.S)
_ESCAPES = {"<\\@>": "@", "<\\<>": "<", "<\\\\>": "\\", "<\\>>": ">"}
_ANY_TOKEN_RE = re.compile(r"⟦(/?)([a-z]?)(\d+)⟧")

#: Longest single tag (a hyperlink or rubi `<A(...)>` can be long, never this long).
_MAX_TAG_CHARS = 65536
_MAX_PAREN_DEPTH = 64
#: Tags allowed per allowed segment, counted before lexing.
_TAGS_PER_SEGMENT = 200


def check_tags_budget(text: str) -> None:
    """Pre-parse budget: refuse an absurd number of tags before any per-tag work."""
    from app import config

    limit = _TAGS_PER_SEGMENT * config.MAX_DOCUMENT_SEGMENTS
    if text.count("<") > limit:
        raise InputLimitError(
            f"This XPress Tags file has more than {limit} tags "
            f"({_TAGS_PER_SEGMENT} x MAX_DOCUMENT_SEGMENTS). Split it into smaller files."
        )


# ---------------------------------------------------------------- encoding


class _Enc:
    __slots__ = ("bom", "codec", "e_num")

    def __init__(self, codec: str, bom: bytes = b"", e_num: int | None = None):
        self.codec = codec  # utf-8 | utf-16-le | utf-16-be | a legacy codec
        self.bom = bom  # the byte-order mark the file started with, written back
        self.e_num = e_num  # the <e> number the file declares (None = no tag)


_E_TEXT_RE = re.compile(r"(?:<v[0-9][0-9.]{0,15}>)?[ \t]*<e([0-9]{1,3})>")


def _codec_for(num: int) -> str:
    if num not in _E_CODECS:
        raise UnsupportedEncodingError(
            f"This XPress Tags file declares an unknown encoding <e{num}>. Re-save it "
            "from QuarkXPress as Unicode (UTF-8 or UTF-16)."
        )
    codec = _E_CODECS[num]
    if codec is None:
        raise UnsupportedEncodingError(
            f"This XPress Tags file is in the {_E_NAMES[num]} encoding (<e{num}>), which "
            "is not supported. Re-save it from QuarkXPress as Unicode (UTF-8 or UTF-16)."
        )
    return codec


def _decode(data: bytes) -> tuple[str, _Enc]:
    """Decode by the bytes first (a BOM, or UTF-16 without one), else by the `<e>`
    header tag, else the shared #509 text decode (UTF-8, else Windows-1252)."""
    data = bytes(data)
    text = None
    if data.startswith(b"\xef\xbb\xbf"):
        enc = _Enc("utf-8", data[:3])
    elif data.startswith(b"\xff\xfe"):
        enc = _Enc("utf-16-le", data[:2])
    elif data.startswith(b"\xfe\xff"):
        enc = _Enc("utf-16-be", data[:2])
    elif data.startswith(b"<\x00"):
        enc = _Enc("utf-16-le")
    elif data.startswith(b"\x00<"):
        enc = _Enc("utf-16-be")
    else:
        m = _E_TAG_RE.match(data[:64])
        if m is None:
            text, codec = read_text_upload(data)  # BOMs were handled above
            enc = _Enc(codec)
        else:
            num = int(m.group(1))
            codec = _codec_for(num)
            # <e8> on 8-bit bytes: the bytes are the truth, read them as UTF-8
            enc = _Enc("utf-8" if codec == "utf-16" else codec, e_num=num)
    if text is None:
        body = data[len(enc.bom) :]
        if enc.codec.startswith("utf-16") and len(body) % 2:
            raise UnsupportedEncodingError(
                "This XPress Tags file is UTF-16 but has an odd number of bytes. Re-save "
                "it from QuarkXPress."
            )
        try:
            text = body.decode(enc.codec)
        except UnicodeDecodeError as exc:
            raise UnsupportedEncodingError(
                "This XPress Tags file could not be read in its encoding "  # noqa: UP031
                "(%s; byte 0x%02x at position %d). Re-save it from QuarkXPress as Unicode "
                "(UTF-8 or UTF-16)." % (enc.codec, body[exc.start], exc.start + len(enc.bom))
            ) from exc
    if enc.e_num is None:  # a BOM or UTF-16 file: the tag is read from the text
        m = _E_TEXT_RE.match(text, 0, 64)
        enc.e_num = int(m.group(1)) if m else None
    if "\x00" in text:
        raise UnsupportedEncodingError(
            "This file is not XPress Tags text (it contains NUL characters)."
        )
    check_line_budget(text)
    return text, enc


def _encode(text: str, enc: _Enc) -> bytes:
    if enc.codec.startswith("utf-"):
        return enc.bom + text.encode(enc.codec)
    try:
        return text.encode(enc.codec)
    except UnicodeEncodeError:
        pass
    # The translation does not fit the legacy encoding: the whole file becomes UTF-8.
    m = re.match(r"((?:<v[0-9][0-9.]{0,15}>)?[ \t]*)<e[0-9]{1,3}>", text)
    if m:
        return (text[: m.end(1)] + "<e9>" + text[m.end() :]).encode("utf-8")
    return b"\xef\xbb\xbf" + text.encode("utf-8")  # no <e> tag: a BOM marks UTF-8


# ---------------------------------------------------------------- lexing


def _scan_tag(text: str, i: int, end: int, line_no: int) -> int:
    """`text[i] == '<'`: return the index just past the tag's closing `>`. Quotes
    ("...") and parentheses are honoured (a rubi or hyperlink `<A(...)>` nests tags);
    a backslash inside a tag protects the next character."""
    m = _SPECIAL_RE.match(text, i, end)
    if m:
        return m.end()
    depth = 0
    quoted = False
    j = i + 1
    stop = min(end, i + _MAX_TAG_CHARS)
    while j < stop:
        ch = text[j]
        if ch == "\\":
            j += 2
            continue
        if quoted:
            if ch == '"':
                quoted = False
        elif ch == '"':
            quoted = True
        elif ch == "(":
            depth += 1
            if depth > _MAX_PAREN_DEPTH:
                raise InputLimitError(
                    f"An XPress Tags tag on line {line_no} nests more than "
                    f"{_MAX_PAREN_DEPTH} parentheses."
                )
        elif ch == ")":
            depth = max(0, depth - 1)
        elif ch == ">" and depth == 0:
            return j + 1
        j += 1
    raise ValueError(
        f"This file could not be read as XPress Tags: the tag starting on line {line_no} "
        "is not closed with '>' on the same line."
    )


def _lex(text: str, start: int, end: int, line_no: int) -> list[tuple[str, int, int, str]]:
    """Items of one paragraph: ("text", a, b, value) / ("tag", a, b, raw)."""
    items: list[tuple[str, int, int, str]] = []
    i = start
    while i < end:
        lt = text.find("<", i, end)
        if lt < 0:
            items.append(("text", i, end, text[i:end]))
            break
        if lt > i:
            items.append(("text", i, lt, text[i:lt]))
        j = _scan_tag(text, lt, end, line_no)
        raw = text[lt:j]
        if raw in _ESCAPES:
            items.append(("text", lt, j, _ESCAPES[raw]))
        else:
            items.append(("tag", lt, j, raw))
        i = j
    return items


# ---------------------------------------------------------------- attribute pairs (#680)

#: Type style toggles (Quark, Character Formatting): <B> bold, <I> italic, <O> outline,
#: <S> shadow, <U> underline, <W> word underline, </> strikethrough, <//> double
#: strikethrough, <R> all caps, <K> small caps, <+> superscript, <-> subscript, <V>
#: superior. <P> is plain (all of them off); <B$> resets one to the style sheet, <$> all.
_TOGGLE_SET = frozenset(("//", "B", "I", "O", "S", "U", "W", "R", "K", "V", "+", "-", "/"))
#: Valued character attributes: font, size, colour, shade, horizontal / vertical scale,
#: kern, track, baseline shift, opacity. <f$> resets one to the style sheet.
_VALUE_KEYS = frozenset("fzcshyktbo")
_VALUE_RE = re.compile(r'"(?:[^"\\]|\\.)*"|\$|-?[0-9]+(?:\.[0-9]+)?|-?\.[0-9]+')
_STYLE_NAME_RE = re.compile(r'[^"<>$\\][^"<>\\]{0,255}')


def _attr_ops(raw: str) -> list[tuple[str, str | None]] | None:
    """A character attribute tag as ops, or None for any other tag (specials, paragraph
    attributes, index markers, hyperlinks, rubi, anything not understood). Ops:
    ("$", None) reset all; ("P", None) plain; (key, "$") reset one; (key, None) toggle;
    (key, value) set a value; ("@", name) apply a character style sheet."""
    body = raw[1:-1]
    if not body:
        return None
    if body[0] == "@":  # character style sheet <@Name>; <@$> / <@$p> reset it
        rest = body[1:]
        if rest in ("$", "$p"):
            return [("@", "$")]
        return [("@", rest)] if _STYLE_NAME_RE.fullmatch(rest) else None
    ops: list[tuple[str, str | None]] = []
    i, n = 0, len(body)
    while i < n:
        ch = body[i]
        if ch in "$P":
            ops.append((ch, None))
            i += 1
            continue
        if ch in _VALUE_KEYS:
            m = _VALUE_RE.match(body, i + 1)
            if m is None:
                return None
            ops.append((ch, m.group(0)))
            i = m.end()
            continue
        key = "//" if body.startswith("//", i) else ch
        if key not in _TOGGLE_SET:
            return None
        i += len(key)
        if i < n and body[i] == "$":
            ops.append((key, "$"))
            i += 1
        else:
            ops.append((key, None))
    return ops


def _reset_tags(keys) -> str:
    """Tags that put `keys` back to the paragraph's style sheet (last set, first reset)."""
    return "".join(f"<{k}$>" for k in reversed(list(keys)))


class _Opener:
    __slots__ = ("close_run", "keys", "run", "tags")

    def __init__(self, run: int, tags: list[int], keys: dict):
        self.run = run
        self.tags = tags  # item indices of the run's opening tags
        self.keys = keys  # attribute -> value (None for a toggle)
        self.close_run: int | None = None  # the later run that resets every key


class _Run:
    __slots__ = ("closer_tags", "closes", "opener", "tags")

    def __init__(self):
        self.tags: list[int] = []  # item indices of consecutive tags (no text between)
        self.closer_tags: list[int] = []
        self.closes: list[_Opener] = []  # pairs this run ends, innermost first
        self.opener: _Opener | None = None


def _pair_runs(items, runs: list[_Run]) -> list[_Opener]:
    """Match each character attribute change with the later run that resets it (#680).

    An attribute switched on (`<B>`, `<I>`, `<f"Arial">`, `<z14>`, `<c"Red">`,
    `<@Emph>`) and reset later in the same paragraph (`<B$>`, `<B>` again, `<P>`, `<$>`,
    `<f$>` ...) is a pair, so MT moves the span with its words. The opening tags of one
    run form one opener; a run's resets close the innermost open pairs whose every
    attribute they reset. What does not nest cleanly (a tag that both resets and sets, a
    reset of only part of an opener, crossed resets) leaves the openers it touches as
    point tokens, the pre-#680 behaviour."""
    stack: list[_Opener] = []
    pairs: list[_Opener] = []
    for r, run in enumerate(runs):
        open_keys = {k for o in stack for k in o.keys}
        run_open: dict = {}
        opener_tags: list[int] = []
        close_keys: set = set()
        close_all = False
        for t in run.tags:
            ops = _attr_ops(items[t][3])
            if ops is None:
                continue
            t_close: set = set()
            t_all = False
            t_open: dict = {}
            clash = False
            for key, val in ops:
                if key == "$":
                    t_all, t_open = True, {}
                elif key == "P":
                    t_close |= open_keys & _TOGGLE_SET
                    t_open = {k: v for k, v in t_open.items() if k not in _TOGGLE_SET}
                elif val == "$":
                    if key in open_keys:
                        t_close.add(key)
                    t_open.pop(key, None)
                elif key in run_open or key in t_open:
                    clash = True  # the same attribute twice in one run: left alone
                elif key in open_keys and key not in close_keys:
                    t_close.add(key)  # a toggle switched back, or a value switched
                    if val is not None:
                        t_open[key] = val
                else:
                    t_open[key] = val
            closes = bool(t_close) or (t_all and bool(stack))
            if clash or (closes and t_open):
                # sets and resets in one tag: the openers it resets lose their pairing
                hit = open_keys if t_all else t_close
                stack[:] = [o for o in stack if not (set(o.keys) & hit)]
                open_keys = {k for o in stack for k in o.keys}
                continue
            if closes:
                run.closer_tags.append(t)
                close_keys |= t_close
                close_all = close_all or t_all
            elif t_open:
                opener_tags.append(t)
                run_open.update(t_open)
        while stack and (close_all or any(set(o.keys) & close_keys for o in stack)):
            top = stack.pop()
            if close_all or set(top.keys) <= close_keys:
                top.close_run = r
                run.closes.append(top)
                pairs.append(top)
            # else: reset only in part, or above a deeper reset: a point token
        if not run.closes:
            run.closer_tags = []  # resets of nothing paired are plain point tags
        if run_open:
            run.opener = _Opener(r, opener_tags, run_open)
            stack.append(run.opener)
    for run in runs:
        if run.opener is not None and run.opener.close_run is None:
            run.opener = None  # never reset in this paragraph: point tokens
    return pairs


class _Para:
    __slots__ = (
        "close_groups",
        "end",
        "pairs",
        "reset_all",
        "resets",
        "start",
        "style",
        "text",
        "tokens",
    )

    def __init__(self, items, style: str):
        idx = [n for n, it in enumerate(items) if it[0] == "text"]
        first, last = idx[0], idx[-1]
        runs: list[_Run] = []
        run_of: dict[int, int] = {}
        for n, it in enumerate(items):
            if it[0] != "tag":
                continue
            if n == 0 or items[n - 1][0] != "tag":
                runs.append(_Run())
            runs[-1].tags.append(n)
            run_of[n] = len(runs) - 1
        pairs = _pair_runs(items, runs)
        lead = run_of[0] if items[0][0] == "tag" else None
        trail = run_of[len(items) - 1] if items[-1][0] == "tag" else None
        # A pair with one end inside the text pulls its other end into the unit; a pair
        # around the whole text (both ends outside it) stays in the skeleton.
        inc_lead = inc_trail = False
        changed = True
        while changed:
            changed = False
            for o in pairs:
                o_out = o.run == lead and not inc_lead
                c_out = o.close_run == trail and not inc_trail
                if o_out and not c_out:
                    inc_lead = changed = True
                elif c_out and not o_out:
                    inc_trail = changed = True
        lo = min(runs[lead].opener.tags) if inc_lead else first
        hi = max(runs[trail].closer_tags) if inc_trail else last
        self.start = items[lo][1]
        self.end = items[hi][2]
        self.style = style
        #: token name -> raw tags: "x1" point, "g2" opening tags, "/g2" closing tags
        self.tokens: dict[str, str] = {}
        self.pairs: list[str] = []  # paired ids, in source order
        self.resets: dict[str, str] = {}  # paired id -> the reset of exactly its keys
        self.close_groups: list[list[str]] = []  # ids closed by one run, innermost first
        #: outermost ids of groups the source ends with a reset-all `<$>`, which also
        #: resets attributes set by point tokens inside the span (#680 gate)
        self.reset_all: set[str] = set()
        live = {id(o) for o in pairs if o.run != lead or inc_lead}
        ids: dict[int, str] = {}
        parts: list[str] = []
        pending: list[str] = []
        count = 0

        def new_id() -> str:
            nonlocal count
            count += 1
            return str(count)

        def flush():
            if pending:
                tid = new_id()
                self.tokens[f"x{tid}"] = "".join(pending)
                parts.append(f"⟦x{tid}⟧")
                pending.clear()

        n = lo
        while n <= hi:
            if items[n][0] == "text":
                flush()
                parts.append(items[n][3])
                n += 1
                continue
            run = runs[run_of[n]]
            closes = [o for o in run.closes if id(o) in live]
            opener = run.opener if run.opener is not None and id(run.opener) in live else None
            closer_tags = set(run.closer_tags) if closes else set()
            opener_tags = set(opener.tags) if opener else set()
            done_close, done_open = not closes, opener is None
            while n <= hi and items[n][0] == "tag":
                if not done_close and (n in closer_tags or n in opener_tags):
                    flush()  # every close comes before the run's opener
                    group = []
                    for o in closes:
                        tid = ids[id(o)]
                        self.tokens[f"/g{tid}"] = self.resets[tid]
                        parts.append(f"⟦/g{tid}⟧")
                        group.append(tid)
                    # the source's own reset tags, written while the closes stay together
                    self.tokens[f"/g{group[-1]}"] = "".join(items[t][3] for t in run.closer_tags)
                    self.close_groups.append(group)
                    if any(("$", None) in (_attr_ops(items[t][3]) or ()) for t in run.closer_tags):
                        self.reset_all.add(group[-1])
                    done_close = True
                if n in closer_tags:
                    n += 1
                    continue
                if n in opener_tags:
                    if not done_open:
                        flush()
                        tid = new_id()
                        ids[id(opener)] = tid
                        self.tokens[f"g{tid}"] = "".join(items[t][3] for t in opener.tags)
                        self.resets[tid] = _reset_tags(opener.keys)
                        self.pairs.append(tid)
                        parts.append(f"⟦g{tid}⟧")
                        done_open = True
                    n += 1
                    continue
                pending.append(items[n][3])
                n += 1
        flush()
        self.text = "".join(parts)


class _Doc:
    __slots__ = ("_style", "enc", "skipped_marker_paras", "text", "units")

    def __init__(self, data: bytes):
        self.text, self.enc = _decode(data)
        check_tags_budget(self.text)
        self.units: list[_Para] = []
        self.skipped_marker_paras = 0
        self._walk()

    def _walk(self) -> None:
        text = self.text
        self._style = ""
        pos = 0
        line_no = 0
        n = len(text)
        while pos < n:
            m = _LINE_RE.match(text, pos)
            line_end = m.end()
            if line_end == pos:  # pragma: no cover - _LINE_RE always advances before EOF
                break
            body_end = line_end
            while body_end > pos and text[body_end - 1] in "\r\n":
                body_end -= 1
            line_no += 1
            self._line(pos, body_end, line_no)
            pos = line_end

    def _line(self, a: int, b: int, line_no: int) -> None:
        text = self.text
        if line_no == 1:  # header tags <vNN.NN><eN> (in either order)
            while True:
                m = _HEAD_TAG_RE.match(text, a, b)
                if not m:
                    break
                a = m.end()
        if _DEF_RE.match(text, a, b):
            return
        m = _APPLY_RE.match(text, a, b)
        if m:
            name = m.group(1)
            self._style = "Normal" if name == "$" else name
            a = m.end()
        items = _lex(text, a, b, line_no)
        plain = "".join(v for k, _a, _b, v in items if k == "text")
        if not any(ch.isalnum() for ch in plain):
            return
        if "⟦" in plain or "⟧" in plain:
            self.skipped_marker_paras += 1
            return
        self.units.append(_Para(items, self._style))
        check_segment_cap(len(self.units))


# ---------------------------------------------------------------- build


def escape_text(s: str) -> str:
    """Literal text -> XPress Tags text: `\\ < @` escaped, a line break becomes the
    New line tag `<\\n>` (a paragraph is one line), other C0 controls dropped."""
    out = []
    for ch in s.replace("\r\n", "\n").replace("\r", "\n"):
        if ch == "\\":
            out.append("<\\\\>")
        elif ch == "<":
            out.append("<\\<>")
        elif ch == "@":
            out.append("<\\@>")
        elif ch == "\n":
            out.append("<\\n>")
        elif ch == "\t" or ord(ch) >= 0x20:
            out.append(ch)
    return "".join(out)


def _live_pairs(p: _Para, matches) -> dict[str, tuple[int, int]]:
    """{paired id: (open match index, close match index)} for each pair a translation
    writes whole: its first `⟦gN⟧` and the first `⟦/gN⟧` after it. Any other occurrence
    (a lone open or close, a repeat, a pair id written as `⟦xN⟧`) is dropped at build,
    so an attribute is only ever switched on together with its reset and never runs on
    past the words it formatted (#680 gate; the dxf/html/docx tolerance: words kept,
    a broken span's formatting lost). Crossed pairs are fine: each attribute is
    independent state in XPress Tags and every close writes its own reset."""
    paired = set(p.pairs)
    opens: dict[str, int] = {}
    closes: dict[str, int] = {}
    for k, m in enumerate(matches):
        slash, kind, tid = m.groups()
        if tid not in paired or kind != "g":
            continue
        if not slash:
            opens.setdefault(tid, k)
        elif tid in opens:
            closes.setdefault(tid, k)
    return {tid: (opens[tid], closes[tid]) for tid in closes}


def _render(p: _Para, translated: str) -> str:
    matches = list(_ANY_TOKEN_RE.finditer(translated))
    live = _live_pairs(p, matches)
    # A run's closes are written as the source's own reset tags while they stay
    # together and in order; once MT separates them (or drops one) each writes its own.
    close_as: dict[str, str] = {}
    single: set[str] = set()
    for group in p.close_groups:
        ks = [live[t][1] if t in live else None for t in group]
        intact = None not in ks and all(
            ks[j + 1] == ks[j] + 1 and matches[ks[j]].end() == matches[ks[j + 1]].start()
            for j in range(len(ks) - 1)
        )
        for t in group[:-1]:
            close_as[t] = "" if intact else p.resets[t]
        last = group[-1]
        if intact:
            close_as[last] = p.tokens[f"/g{last}"]
        else:  # a source <$> stays a reset-all, so nothing set inside runs on past it
            close_as[last] = "<$>" if last in p.reset_all else p.resets[last]
        if len(group) == 1:
            single.add(last)
    write = {k: tid for tid, ks in live.items() for k in ks}
    paired = set(p.pairs)
    used: set[str] = set()
    emptied: set[str] = set()
    out: list[str] = []
    pos = 0
    for k, m in enumerate(matches):
        out.append(escape_text(translated[pos : m.start()]))
        pos = m.end()
        slash, kind, tid = m.groups()
        if tid in paired:
            if write.get(k) != tid:
                continue  # not part of a pair written whole: dropped
            if slash:
                if tid not in emptied:
                    out.append(close_as[tid])
                continue
            c = live[tid][1]
            if tid in single and c == k + 1 and matches[c].start() == m.end():
                emptied.add(tid)  # MT moved every word out: no attribute on nothing
                continue
            out.append(p.tokens[f"g{tid}"])
            continue
        name = f"x{tid}"
        if not slash and kind == "x" and name in p.tokens and name not in used:
            used.add(name)
            out.append(p.tokens[name])
    out.append(escape_text(translated[pos:]))
    out.extend(raw for name, raw in p.tokens.items() if name[0] == "x" and name not in used)
    rendered = "".join(out)
    if rendered.startswith("~") and _DEF_RE.match(rendered):
        # A translation reading `~Name=<` would turn the paragraph into a bookmark
        # definition (gate train-11): write the tilde as its character code.
        rendered = "<\\#126>" + rendered[1:]
    return rendered


@register
class QuarkTagsFilter(Filter):
    fmt = "xtg"
    exts = ("tag",)

    def parse(self, data: bytes, provider: str | None = None) -> ParseResult:
        doc = _Doc(data)
        units = [
            TextUnit(
                p_index=i,
                text=p.text,
                style=p.style,
                inline_map=dict(p.tokens),
            )
            for i, p in enumerate(doc.units)
        ]
        check_unit_texts(u.text for u in units)
        skeleton = {
            "format": "xtg",
            "units": [{"p_index": u.p_index, "text": u.text, "style": u.style} for u in units],
        }
        if doc.enc.e_num is not None:
            skeleton["xtg_e"] = doc.enc.e_num
        note_source_encoding(skeleton, doc.enc.codec)
        if doc.skipped_marker_paras:
            skeleton["skipped_marker_paragraphs"] = doc.skipped_marker_paras
        return ParseResult(skeleton=skeleton, units=units)

    def build(self, original: bytes, paragraph_texts: dict) -> bytes:
        doc = _Doc(original)
        edits = []
        for i, p in enumerate(doc.units):
            new = paragraph_texts.get(str(i))
            if new is None or new == p.text:
                continue
            edits.append((p.start, p.end, _render(p, new)))
        if not edits:
            return bytes(original)
        parts, pos = [], 0
        for a, b, raw in edits:  # units are in file order and never overlap
            parts += [doc.text[pos:a], raw]
            pos = b
        parts.append(doc.text[pos:])
        text = "".join(parts)
        out = _encode(text, doc.enc)
        _Doc(out)  # every rebuild must still lex as XPress Tags
        return out
