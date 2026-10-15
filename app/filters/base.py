"""Filter abstraction.

A Filter does two things:
  parse(data)  -> ParseResult(skeleton, units)   # extract translatable text
  build(...)   -> bytes                           # re-inject translations

`units` are paragraph-level TextUnits (stable index back into the document).
Segmentation into sentences happens above this layer (services/segment.py), so
filters stay format-specific and segmentation stays shared.

Documented in: docs/architecture/translation-pipeline.md
"""

import contextlib
import contextvars
import functools
import re
from abc import ABC, abstractmethod
from dataclasses import dataclass, field


class InputLimitError(ValueError):
    """A file was refused because it is over a safety limit (security review 2026-09,
    #224 #272): a zip bomb, a PDF with too many pages. The message is plain, names the
    limit and is safe to show the person who uploaded the file. Every door maps it to a
    clean refusal (413 on /api, a page message on the web, an error line in the CLI)."""


def check_segment_cap(count: int) -> None:
    """Refuse a document that holds more than config.MAX_DOCUMENT_SEGMENTS units or
    segments (#571). Called while a filter walks its input and again while ingest
    splits sentences, so nothing is written for an over-cap file. `count` is the
    running total; call it inside the loop so a hostile file stops early."""
    from app import config

    if count > config.MAX_DOCUMENT_SEGMENTS:
        raise InputLimitError(
            f"This file has more than {config.MAX_DOCUMENT_SEGMENTS} segments "
            "(MAX_DOCUMENT_SEGMENTS). Split it into smaller files."
        )


def check_unit_texts(texts) -> None:
    """The same guard for a finished parse (#571): too many units, or one unit longer
    than config.MAX_SEGMENT_CHARS (a single huge line). Applies to every format."""
    from app import config

    for n, t in enumerate(texts, 1):
        if n > config.MAX_DOCUMENT_SEGMENTS:
            check_segment_cap(n)
        if len(t) > config.MAX_SEGMENT_CHARS:
            raise InputLimitError(
                f"This file has a line or paragraph longer than {config.MAX_SEGMENT_CHARS} "
                "characters (MAX_SEGMENT_CHARS). Break it into shorter lines."
            )


class UnsupportedEncodingError(InputLimitError):
    """An uploaded text file could not be read as text (#509): not UTF-8, no BOM or
    declared charset that decodes it, and not readable as Windows-1252 either (bytes
    undefined there, or a binary file). A subclass of InputLimitError so every surface
    that already shows a refusal's own message (web page error, API JSON error, CLI
    error line, MCP tool error) shows this one too, never a generic 'corrupt'."""


class EmptyDocumentError(InputLimitError):
    """An uploaded file parsed cleanly but carries no text to translate (#573): every
    unit is blank (an empty or whitespace-only file). A subclass of InputLimitError so
    every surface that already shows a refusal's own message (web page error, CLI error
    line, MCP tool error) shows this one too; the API maps it to 422, not the generic
    InputLimitError 413. The message is plain and safe to show the uploader."""


# Text encodings (#509, 2026-10-06): UTF-8 first; then a UTF-16 BOM; then a
# charset the file itself declares (HTML meta, XML declaration, PO Content-Type) when
# it is one of the common legacy text encodings below; then Windows-1252, the usual
# encoding of a "plain text" file saved on Windows. Anything else is refused. The SAME
# decode runs at parse and at build, so unit indices always line up. A declared charset
# is rewritten to UTF-8 in the decoded text so the file stays truthful. Exports: a filter
# that writes through `encode_like` (B-bytes) gives the file back in its own code page
# when every character fits, else UTF-8 (a translation into most languages cannot be
# written in Windows-1252); other filters export UTF-8.
_DECLARED_NAMES = [
    "cp1250",
    "cp1251",
    "cp1252",
    "cp1253",
    "cp1254",
    "cp1255",
    "cp1256",
    "cp1257",
    "cp1258",
    "iso8859-1",
    "iso8859-2",
    "iso8859-5",
    "iso8859-7",
    "iso8859-9",
    "iso8859-15",
    "koi8-r",
    "koi8-u",
    "shift_jis",
    "cp932",
    "euc_jp",
    "gb2312",
    "gbk",
    "gb18030",
    "big5",
    "euc_kr",
    "cp949",
    "utf-16",
]


def _codec_name(label: str) -> str | None:
    import codecs

    try:
        return codecs.lookup(label).name
    except LookupError:
        return None


_DECLARED_OK = {n for n in (_codec_name(x) for x in _DECLARED_NAMES) if n}
_DECL_RE = re.compile(
    r"""(<\?xml[^>]{0,200}?\bencoding\s*=\s*["']\s*|<meta\b[^>]{0,400}?\bcharset\s*=\s*["']?\s*"""
    r"""|Content-Type:[^"\n]{0,100}?\bcharset\s*=\s*)([A-Za-z0-9_.:-]{1,40})""",
    re.IGNORECASE,
)
# C0 controls a text file never carries (TAB, LF, VT, FF, CR, SUB and ESC are allowed).
_BINARY_CONTROLS = bytes(sorted(set(range(0x20)) - {0x09, 0x0A, 0x0B, 0x0C, 0x0D, 0x1A, 0x1B}))


def _declared_codec(data: bytes) -> str | None:
    m = _DECL_RE.search(data[:4096].decode("ascii", "replace"))
    name = _codec_name(m.group(2)) if m else None
    return name if name in _DECLARED_OK else None


def _looks_binary(data: bytes) -> bool:
    if b"\x00" in data:
        return True
    bad = len(data) - len(data.translate(None, _BINARY_CONTROLS))  # C speed on big files
    return bad > 2 + len(data) // 100


def check_line_budget(text: str) -> None:
    """Refuse a text file with an absurd number of lines BEFORE any per-line work (#571
    review): `splitlines()` on 100 MB of newlines builds a list of 100 million entries
    and a per-line regex over it holds the interpreter for minutes, long before a unit
    count exists. Twenty lines per allowed segment is far above any real file (blank
    lines, headers and comments included) and costs one C-speed count."""
    from app import config

    limit = 20 * config.MAX_DOCUMENT_SEGMENTS
    if text.count("\n") + text.count("\r") > limit:
        raise InputLimitError(
            f"This file has more than {limit} lines (20 x MAX_DOCUMENT_SEGMENTS). "
            "Split it into smaller files."
        )


def read_text_upload(data: bytes, *, sig: bool = False, declared: bool = False) -> tuple[str, str]:
    """`_read_text_upload` plus the #571 line budget (every line-based filter reads here)."""
    text, enc = _read_text_upload(data, sig=sig, declared=declared)
    check_line_budget(text)
    return text, enc


def _read_text_upload(data: bytes, *, sig: bool = False, declared: bool = False) -> tuple[str, str]:
    """Decode an uploaded text-like file: returns (text, encoding), encoding one of
    "utf-8", "utf-16", a declared legacy codec name, or "cp1252" (#509).

    `declared`: honour (and rewrite to UTF-8) a charset the file declares (HTML, SVG,
    PO). Raises UnsupportedEncodingError, with a re-save-as-UTF-8 message, when the
    bytes cannot be read meaningfully as any of these (never a U+FFFD decode)."""
    data = bytes(data)
    try:
        utf8 = data.decode("utf-8-sig" if sig else "utf-8")
    except UnicodeDecodeError:
        pass
    else:
        hint = _CODE_PAGE_HINT.get()
        if hint is not None and hint.codec and not hint.forced:
            _warn_if_legacy_as_utf8(data, utf8, hint)
        return utf8, "utf-8"
    text = enc = None
    if data[:2] in (b"\xff\xfe", b"\xfe\xff"):
        try:
            text, enc = data.decode("utf-16"), "utf-16"
        except UnicodeDecodeError:
            text = None
    if text is None and declared:
        codec = _declared_codec(data)
        if codec and codec not in ("utf-8", "utf-16", "iso8859-1", "cp1252"):
            try:
                text, enc = data.decode(codec), codec
            except UnicodeDecodeError:
                text = None
    if text is None:
        if _looks_binary(data):
            raise UnsupportedEncodingError(
                "file is not valid UTF-8 and looks like a binary file, not text; "
                "re-save the file as UTF-8."
            )
        hint = _CODE_PAGE_HINT.get()
        if hint is not None:
            text, enc = _decode_with_hint(data, hint)
        else:
            try:
                text, enc = data.decode("cp1252"), "cp1252"
            except UnicodeDecodeError as exc:
                raise UnsupportedEncodingError(
                    "file is not valid UTF-8 and could not be read as Windows-1252 either "  # noqa: UP031
                    "(byte 0x%02x at position %d); re-save the file as UTF-8."
                    % (data[exc.start], exc.start)
                ) from exc
    if declared:  # only reached for a converted file: UTF-8 input returned above
        text = _DECL_RE.sub(lambda m: m.group(1) + "UTF-8", text)
    return text, enc


# Decision 22: a text file that is not UTF-8 and declares no charset is
# read in the Windows code page of the project's SOURCE language (Greek -> Windows-1253,
# Russian -> Windows-1251, Czech -> Windows-1250, Japanese -> Shift_JIS, ...; the table is
# app/filters/code_pages.py, shared with HTML Help), in place of the Windows-1252 default,
# and the upload WARNS when the decoded letters look wrong. Ingest sets the hint around the
# parse (`source_code_page`) and stamps the code page it used on the skeleton
# (`text_code_page`); export and repair read the stored original with exactly that code page
# (`forced`), so a document imported before the rule (no stamp) is read as it always was.
TEXT_CODE_PAGE_KEY = "text_code_page"
TEXT_ENCODING_WARNING = "text_encoding"


@dataclass
class CodePageHint:
    codec: str | None = None  # the source language's code page; None = Windows-1252
    locale: str = ""  # the project's source locale ("" when not known)
    forced: bool = False  # export / repair: decode exactly with `codec`, no guessing
    used: str | None = None  # the code page the last undeclared legacy decode used
    warnings: list = field(default_factory=list)


_CODE_PAGE_HINT: contextvars.ContextVar = contextvars.ContextVar("source_code_page", default=None)


@contextlib.contextmanager
def source_code_page(
    source_locale: str | None = None, *, codec: str | None = None, forced: bool = False
):
    """Run a parse or build with the decision 22 code page hint. `source_locale` picks the
    code page (a language with none, or a Western one, keeps Windows-1252); `codec` names it
    directly (the stamped one, with `forced=True`, for export and repair)."""
    if codec is None and source_locale and not forced:
        from app.filters.code_pages import codec_for_code_page, target_code_page

        codec = codec_for_code_page(target_code_page(source_locale))
    if codec and _codec_name(codec) == "cp1252":
        codec = None
    hint = CodePageHint(codec=_codec_name(codec) if codec else None, locale=source_locale or "")
    hint.forced = forced
    token = _CODE_PAGE_HINT.set(hint)
    try:
        yield hint
    finally:
        _CODE_PAGE_HINT.reset(token)


def document_code_page(skeleton: dict | None):
    """The forced hint for re-reading a stored original the way its import did: the
    stamped code page, or no hint at all (the #509 rule) for a document without one."""
    cp = (skeleton or {}).get(TEXT_CODE_PAGE_KEY) if isinstance(skeleton, dict) else None
    if isinstance(cp, str) and _codec_name(cp) in _HINT_CODECS:
        return source_code_page(codec=cp, forced=True)
    return contextlib.nullcontext()


def apply_code_page_hint(skeleton: dict, hint: CodePageHint | None) -> dict:
    """After an ingest parse: stamp the code page used (when it was not Windows-1252) and
    put the encoding warning(s) on `file_warnings` (replacing any earlier one)."""
    if hint is None:
        return skeleton
    if hint.used and hint.used != "cp1252":
        skeleton[TEXT_CODE_PAGE_KEY] = hint.used
    else:
        skeleton.pop(TEXT_CODE_PAGE_KEY, None)
    warnings = [
        w
        for w in (skeleton.get("file_warnings") or [])
        if not (isinstance(w, dict) and w.get("kind") == TEXT_ENCODING_WARNING)
    ]
    warnings.extend(hint.warnings)
    if warnings:
        skeleton["file_warnings"] = warnings
    else:
        skeleton.pop("file_warnings", None)
    return skeleton


def _hint_codecs() -> set[str]:
    from app.filters.code_pages import CP_INFO

    return {n for n in (_codec_name(i[0]) for i in CP_INFO.values()) if n}


# gb18030: a Simplified Chinese file GBK cannot read (GB18030 four-byte sequences). It
# decodes every GBK byte pair to the same character, so it is only a fallback for GBK.
_HINT_CODECS = _hint_codecs() | {"gb18030"}
_SUPERSET = {"gbk": "gb18030"}


def _label(codec: str | None) -> str:
    from app.filters.code_pages import codec_label

    return codec_label(codec or "cp1252")


def _add_warning(hint: CodePageHint, message: str) -> None:
    w = {"kind": TEXT_ENCODING_WARNING, "part": "text encoding", "message": message}
    if w not in hint.warnings:
        hint.warnings.append(w)


_RESAVE = "Check the source text; if it is garbled, re-save the file as UTF-8 and upload it again."


def _decode_with_hint(data: bytes, hint: CodePageHint) -> tuple[str, str]:
    """The last step of the #509 decode under a decision 22 hint (see source_code_page)."""
    primary = hint.codec or "cp1252"
    forced = hint.forced
    if hint.used and not forced:
        # Gate: one parse that decodes several legacy parts (a container) reads them all
        # with the code page the first one used, so the single stamp re-reads every part
        # on export exactly as the import did.
        primary, forced = hint.used, True
    where = (
        f", the code page of the project's source language {hint.locale},"
        if (hint.codec and hint.locale)
        else ""
    )
    try:
        text = data.decode(primary)
    except UnicodeDecodeError as exc:
        text, wider = None, _SUPERSET.get(primary)
        if wider and not forced:
            with contextlib.suppress(UnicodeDecodeError):
                text, primary = data.decode(wider), wider
        if text is None and (forced or primary == "cp1252"):
            raise UnsupportedEncodingError(
                f"file is not valid UTF-8 and could not be read as {_label(primary)} either "
                "(byte 0x%02x at position %d); re-save the file as UTF-8."
                % (data[exc.start], exc.start)
            ) from exc
        if text is None:
            return _western_fallback(data, hint, primary, where, exc)
    reason = text_looks_garbled(text, primary, hint.locale, data)
    if reason and not forced and primary != "cp1252":
        try:
            alt = data.decode("cp1252")
        except UnicodeDecodeError:
            alt = None
        if alt is not None and not text_looks_garbled(alt, "cp1252", "", data):
            hint.used = "cp1252"
            _add_warning(
                hint,
                f"This file is not UTF-8 and declares no encoding. Read as {_label(primary)}"
                f"{where or ','} its letters looked wrong ({reason}), so it was read as Western "
                f"European (Windows-1252) instead. {_RESAVE}",
            )
            return alt, "cp1252"
    hint.used = primary
    if reason:
        _add_warning(
            hint,
            f"This file is not UTF-8 and declares no encoding, so it was read as "
            f"{_label(primary)}{where or ','} but its letters look wrong ({reason}). {_RESAVE}",
        )
    return text, primary


def _western_fallback(data: bytes, hint: CodePageHint, primary: str, where: str, exc) -> tuple:
    """The source code page could not read the bytes: Windows-1252 with a warning, or refuse."""
    try:
        alt = data.decode("cp1252")
    except UnicodeDecodeError:
        raise UnsupportedEncodingError(
            f"file is not valid UTF-8 and could not be read as {_label(primary)}{where} "
            "or Windows-1252 either (byte 0x%02x at position %d); re-save the file as UTF-8."
            % (data[exc.start], exc.start)
        ) from exc
    hint.used = "cp1252"
    _add_warning(
        hint,
        f"This file is not UTF-8 and declares no encoding. It could not be read as "
        f"{_label(primary)}{where} so it was read as Western European (Windows-1252) "
        f"instead. {_RESAVE}",
    )
    return alt, "cp1252"


# --- "the decoded letters look wrong" (decision 22) ----------------------------------------
_GARBLE_SAMPLE = 200_000
# Letters plus the Hebrew, Arabic and Thai combining marks (not \w): a Western letter read as
# Hebrew can come out as a vowel point glued to a Latin letter (U+05B7 + "a" for "Ca").
_WORD_RE = re.compile(
    r"(?:[^\W\d_]|[\u0591-\u05c7\u0610-\u061a\u064b-\u065f\u0670\u0e31\u0e34-\u0e3a\u0e47-\u0e4e])+"
)
_UTF8_SEQ = re.compile(
    rb"[\xc2-\xdf][\x80-\xbf]|[\xe0-\xef][\x80-\xbf]{2}|[\xf0-\xf4][\x80-\xbf]{3}"
)
_FAMILY_BY_CP = {
    1253: "greek",
    1251: "cyrillic",
    1255: "hebrew",
    1256: "arabic",
    874: "thai",
    932: "cjk",
    936: "cjk",
    949: "cjk",
    950: "cjk",
}
# Single-byte code pages whose letters rarely form valid UTF-8 byte pairs (Thai, Hebrew and
# Arabic letters often do, so a mixed-UTF-8 check there would cry wolf).
_UTF8_CHECK = {"cp1250", "cp1251", "cp1252", "cp1253", "cp1254", "cp1257", "cp1258"}
_MULTIBYTE = {"cp932", "shift_jis", "gbk", "gb2312", "gb18030", "big5", "cp950", "cp949", "euc_kr"}


def _script(ch: str) -> str:
    o = ord(ch)
    if o < 0x250 or 0x1E00 <= o <= 0x1EFF:
        return "latin"
    if 0x370 <= o <= 0x3FF or 0x1F00 <= o <= 0x1FFF:
        return "greek"
    if 0x400 <= o <= 0x52F:
        return "cyrillic"
    if 0x590 <= o <= 0x5FF or 0xFB1D <= o <= 0xFB4F:
        return "hebrew"
    if 0x600 <= o <= 0x77F or 0xFB50 <= o <= 0xFDFF or 0xFE70 <= o <= 0xFEFF:
        return "arabic"
    if 0xE00 <= o <= 0xE7F:
        return "thai"
    if 0xFF61 <= o <= 0xFF9F:
        return "hwkana"
    if (
        0x3040 <= o <= 0x30FF
        or 0x3400 <= o <= 0x9FFF
        or 0xF900 <= o <= 0xFAFF
        or 0xAC00 <= o <= 0xD7AF
        or 0x1100 <= o <= 0x11FF
        or 0x3130 <= o <= 0x318F
        or 0xFF00 <= o <= 0xFF60
    ):
        return "cjk"
    return "other"


def text_looks_garbled(text: str, codec: str, locale: str = "", data: bytes = b"") -> str | None:
    """A short plain reason when `text` (an undeclared legacy decode of `data` with `codec`)
    looks like the wrong code page, else None. Heuristics, deliberately conservative:

    * letters of two scripts inside one word (Greek read as Western: "Cafι"; Western
      read as Greek); in a CJK file ASCII letters next to kanji are normal and ignored;
    * many words made mostly of accented Latin letters (Russian or Greek read as Western:
      "Ïðèâåò", "ÊáëçìÝñá"); when `codec` is the locale's own code page, a word made only of
      that language's own accented letters does not count (Polish "Zażółć", #760);
    * a single-byte decode of bytes that are mostly valid UTF-8 sequences (a mixed file).
    """
    sample = text[:_GARBLE_SAMPLE]
    cjk = codec in _MULTIBYTE
    native: frozenset = frozenset()
    if locale:
        from app.filters.code_pages import native_letters, target_code_page

        cjk = cjk or _FAMILY_BY_CP.get(target_code_page(locale) or 0) == "cjk"
        native = native_letters(locale, codec)
    words = mixed = heavy = 0
    for word in _WORD_RE.findall(sample):
        nonascii = [ch for ch in word if ord(ch) > 127]
        if not nonascii:
            continue
        words += 1
        if cjk:
            fams = {_script(ch) for ch in nonascii}
            if "hwkana" in fams and len(nonascii) < len(word):
                fams.add("latin")
        else:
            fams = {_script(ch) for ch in word}
        if len(fams) > 1:
            mixed += 1
        elif (
            fams == {"latin"}
            and len(nonascii) >= 3
            and len(nonascii) * 10 >= len(word) * 6
            and not native.issuperset(nonascii)
        ):
            # #760: a word whose accented letters all belong to the source language's own
            # alphabet (read with that language's code page) is ordinary text ("Zażółć").
            heavy += 1
    if words and mixed and (mixed * 5 >= words or mixed >= 10):
        return "letters of different scripts mixed inside words"
    if heavy >= 2 and heavy * 10 >= words * 3:
        return "many words made mostly of accented letters"
    if data and codec in _UTF8_CHECK:
        raw = bytes(data[: _GARBLE_SAMPLE * 2])
        high = sum(1 for b in raw if b > 0x7F)
        seqs = _UTF8_SEQ.findall(raw)
        if len(seqs) >= 3 and sum(len(s) for s in seqs) * 2 >= high:
            return "parts of the file look like UTF-8"
    return None


_NON_ASCII = re.compile("[^\x00-\x7f]")
_SHORT_UTF8 = 16  # at most this many non-ASCII characters: a "short" legacy file


def _warn_if_legacy_as_utf8(data: bytes, text: str, hint: CodePageHint) -> None:
    """Gate (decision 22): a few GBK, Big5 or Korean characters are valid UTF-8 by accident
    surprisingly often (about 1 to 5 in 100 strings of one to four characters). Such a file
    stays UTF-8 (the #509 order: UTF-8 first), but the upload warns when, in a project whose
    source language is Chinese, Japanese or Korean, ALL of these hold: at most 16 non-ASCII
    characters, every one in U+0080..U+07FF (what a two-byte UTF-8 sequence gives), at least
    one past Latin Extended-A (U+0180 on: IPA, Greek, Cyrillic, Hebrew, Arabic, ...; plain
    accented Western letters never warn), no CJK at all, and the source code page reads the
    same bytes cleanly as CJK text. Single-byte code pages are left alone: Greek, Cyrillic,
    Hebrew, Arabic and Thai legacy text practically never forms valid UTF-8."""
    from app.filters.code_pages import code_page_for_codec

    if _FAMILY_BY_CP.get(code_page_for_codec(hint.codec) or 0) != "cjk":
        return
    odd = False
    # C-speed scan between the few non-ASCII characters
    for count, m in enumerate(_NON_ASCII.finditer(text), 1):
        o = ord(m.group())
        if count > _SHORT_UTF8 or o > 0x7FF:
            return
        odd = odd or o >= 0x180
    if not odd:
        return
    try:
        alt = data.decode(hint.codec)
    except UnicodeDecodeError:
        return
    if not any(_script(ch) == "cjk" for ch in alt) or text_looks_garbled(
        alt, hint.codec, hint.locale
    ):
        return
    _add_warning(
        hint,
        f"This file is valid UTF-8, so it was read as UTF-8, but its few non-English letters "
        f"look wrong for the project's source language {hint.locale} (read as "
        f"{_label(hint.codec)} the same bytes give Chinese, Japanese or Korean text). {_RESAVE}",
    )


def warn_if_garbled(text: str, codec: str, label: str = "") -> list[dict]:
    """For a filter with its own legacy decode (Java .properties are ISO-8859-1 by spec):
    the decision 22 warning when the active hint's source language says the letters look
    wrong. Returns the warning(s) added to the hint (also returned for the caller)."""
    hint = _CODE_PAGE_HINT.get()
    if hint is None or hint.forced:
        return []
    reason = text_looks_garbled(text, codec, hint.locale)
    if not reason:
        return []
    _add_warning(
        hint,
        f"This file is not UTF-8, so it was read as {label or _label(codec)}, but its letters look "
        f"wrong ({reason}). {_RESAVE}",
    )
    return list(hint.warnings)


def note_source_encoding(skeleton: dict, encoding: str) -> dict:
    """Record a converted upload's original encoding on the skeleton (UTF-8 files keep
    their skeleton byte-identical: no key is added)."""
    if encoding != "utf-8":
        skeleton["source_encoding"] = encoding
    return skeleton


def decode_text_lossless(data: bytes, *, sig: bool = False, declared: bool = False) -> str:
    """Decode a stored original for BUILD the same way PARSE did (UTF-8, UTF-16 BOM,
    declared charset or Windows-1252), so indices line up and the export is UTF-8.
    A stored original from before #509 that none of those read keeps every byte:
    undecodable bytes become lone surrogates that `encode_text_lossless` writes back."""
    try:
        return read_text_upload(data, sig=sig, declared=declared)[0]
    except UnsupportedEncodingError:
        return bytes(data).decode("utf-8-sig" if sig else "utf-8", "surrogateescape")


def encode_text_lossless(text: str) -> bytes:
    return text.encode("utf-8", "surrogateescape")


# Byte-faithful output (B-bytes, 2026-10-11 decision 1): a translated text file comes
# back in the original's envelope: its byte-order mark, its line endings (when the original
# used one style throughout) and its encoding when every character of the output fits
# (a declared charset is written back with it). A translation that does not fit a legacy
# code page is written as UTF-8, as #509 always did, with the declaration saying so.
@dataclass
class TextEnvelope:
    codec: str = "utf-8"  # python codec to write with
    bom: bytes = b""  # the byte-order mark the original started with
    newline: str = ""  # "\r\n" or "\r" when the original used only that; "" = leave as is
    label: str = ""  # the charset label the original declared ("" = none or not converted)
    declared: bool = False  # the format declares its charset in the file (HTML, XML, PO)


def _newline_style(data: bytes, codec: str) -> str:
    if codec.startswith("utf-16"):
        try:
            text = data.decode(codec)
        except UnicodeDecodeError:
            return ""
        crlf, lf, cr = text.count("\r\n"), text.count("\n"), text.count("\r")
    else:
        crlf, lf, cr = data.count(b"\r\n"), data.count(b"\n"), data.count(b"\r")
    if crlf and crlf == lf == cr:
        return "\r\n"
    if cr and not lf:
        return "\r"
    return ""


def text_envelope(original: bytes, *, declared: bool = False) -> TextEnvelope:
    """The envelope of a stored original, read the way parse read it (#509)."""
    data = bytes(original)
    if data.startswith(b"\xef\xbb\xbf"):
        return TextEnvelope("utf-8", b"\xef\xbb\xbf", _newline_style(data, "utf-8"))
    if data[:2] in (b"\xff\xfe", b"\xfe\xff"):
        codec = "utf-16-le" if data[:2] == b"\xff\xfe" else "utf-16-be"
        label = ""
        if declared:  # parse rewrote a declared charset to UTF-8: remember the file's own
            try:
                m = _DECL_RE.search(data[2:8194].decode(codec, "replace"))
            except LookupError:
                m = None
            label = m.group(2) if m else ""
        return TextEnvelope(codec, data[:2], _newline_style(data[2:], codec), label, declared)
    try:
        _text, enc = _read_text_upload(data, declared=declared)
    except UnsupportedEncodingError:
        enc = "utf-8"  # a pre-#509 original: kept byte for byte by surrogateescape
    label = ""
    if enc != "utf-8" and declared:
        m = _DECL_RE.search(data[:4096].decode("ascii", "replace"))
        named = _codec_name(m.group(2)) if m else None
        if m and named == enc:
            label = m.group(2)
        elif m and enc == "cp1252" and named in ("iso8859-1", "ascii"):
            # B-bytes gate: #509 reads a declared ISO-8859-1 / US-ASCII file as
            # Windows-1252; write it back in the codec its label names (when it fits),
            # never Windows-1252 bytes under a label that says otherwise.
            enc, label = named, m.group(2)
        elif m:
            enc = "utf-8"  # declared one charset, read as another: UTF-8, as #509 wrote it
    if enc == "utf-16":  # a BOM-less UTF-16 never reaches here (BOM checked above)
        enc = "utf-8"
    return TextEnvelope(enc, b"", _newline_style(data, "utf-8"), label, declared)


def encode_like(text: str, env: TextEnvelope) -> bytes:
    """`text` (the rebuilt file, as parse decoded it) in the original's envelope."""
    if text.startswith("﻿"):
        text = text[1:]
    if env.newline:
        text = re.sub(r"\r\n|\r|\n", env.newline, text)
    if env.codec == "utf-8":
        return env.bom + text.encode("utf-8", "surrogateescape")
    restored = text
    if env.label:
        # parse rewrote every declaration to UTF-8 (#509); give each its own label back
        restored = _DECL_RE.sub(
            lambda m: m.group(1) + env.label if m.group(2).upper() == "UTF-8" else m.group(0),
            text,
        )
    if env.declared and not env.codec.startswith("utf-16"):
        # B-bytes gate: the bytes and every declaration in the file must agree (a second
        # declaration, or one past the first 4 KB, may name another charset): if any
        # does not name the codec written, write UTF-8 under the UTF-8 labels parse left.
        for m in _DECL_RE.finditer(restored):
            if _codec_name(m.group(2)) != env.codec:
                return text.encode("utf-8", "surrogateescape")
    try:
        return env.bom + restored.encode(env.codec)  # bom: UTF-16 only on this path
    except (UnicodeEncodeError, LookupError):
        return text.encode("utf-8", "surrogateescape")


def newline_like(original: bytes, piece: bytes) -> bytes:
    """`piece` (UTF-8 bytes spliced into `original`) with the original's line endings,
    when the original used one style throughout."""
    newline = _newline_style(bytes(original), "utf-8")
    return re.sub(rb"\r\n|\r|\n", newline.encode("ascii"), piece) if newline else piece


_XML_DECL_RE = re.compile(rb"^<\?xml[^>]*\?>")
_XML_DECL_ENC_RE = re.compile(rb"""encoding\s*=\s*["']([A-Za-z0-9_.:-]+)["']""")


def _decl_codec(decl: bytes) -> str:
    m = _XML_DECL_ENC_RE.search(decl)
    return _codec_name(m.group(1).decode("ascii")) if m else "utf-8"


def xml_like(original: bytes, built: bytes) -> bytes:
    """B-bytes: a UTF-8 XML document re-serialized by lxml, put back in the original's
    envelope: its own XML declaration (quote style, standalone, or none at all), its
    UTF-8 BOM, its line endings (when one style was used throughout) and the whitespace
    after the root element. Element content and attribute order are lxml's. An original
    in another encoding (UTF-16, a legacy code page) is left to lxml (`built` as is)."""
    src = bytes(original)
    bom = b"\xef\xbb\xbf" if src.startswith(b"\xef\xbb\xbf") else b""
    src = src[len(bom) :]
    if src[:2] in (b"\xff\xfe", b"\xfe\xff") or not built:
        return built
    out = bytes(built)
    if out.startswith(b"\xef\xbb\xbf"):
        out = out[3:]
    m_src, m_out = _XML_DECL_RE.match(src), _XML_DECL_RE.match(out)
    if m_out and _decl_codec(m_out.group(0)) != "utf-8":
        return built
    if m_src and _decl_codec(m_src.group(0)) != "utf-8":
        return built
    if m_out:
        rest = out[m_out.end() :]
        if not m_src and rest.startswith(b"\n"):
            rest = rest[1:]  # lxml's newline after the declaration it added
        out = (m_src.group(0) if m_src else b"") + rest
    trail = src[len(src.rstrip(b" \t\r\n")) :]
    if trail and not out.endswith(trail):
        out = out.rstrip(b" \t\r\n") + trail
    newline = _newline_style(src, "utf-8")
    if newline:
        out = re.sub(rb"\r\n|\r|\n", newline.encode("ascii"), out)
    return bom + out


@dataclass
class TextUnit:
    p_index: int  # stable position of the paragraph within the document
    text: str
    style: str = ""
    # Filter-private reconstruction side-channel for inline formatting/tags.
    # `text` carries placeholder tokens (see services/segment placeholder scheme);
    # inline_map keys those tokens to the data build() needs to rebuild the real
    # inline markup — DOCX: per-run rPr XML snapshots; XLIFF: per-element tag/attrs/
    # self-closing-vs-paired serialization. Empty for every other filter, so nothing
    # else in the pipeline (segment/tm/qa/prompts) is affected — they see plain text.
    inline_map: dict = field(default_factory=dict)


@dataclass
class ParseResult:
    skeleton: dict
    units: list = field(default_factory=list)  # list[TextUnit]


_BUILD_DEPTH: contextvars.ContextVar[int] = contextvars.ContextVar("filter_build_depth", default=0)


def _written(paragraph_texts, kwargs) -> dict:
    fallback = kwargs.get("fallback_keys") or ()
    if not fallback:
        return paragraph_texts
    return {k: v for k, v in paragraph_texts.items() if k not in fallback}


def _parses(filt, original: bytes) -> bool:
    """The upload still passes the filter's own checks (zip, XML and size guards), so a
    build that writes nothing never hands back a file the filter would refuse. A file
    parse refuses goes through the filter's build as before (which refuses it too)."""
    try:
        filt.parse(original)
    except Exception:  # the real build decides
        return False
    return True


def _is_identity(filt, original: bytes, written: dict) -> bool:
    """Every written text is exactly the unit text parse gives for that key."""
    try:
        units = filt.parse(original).units
    except Exception:
        return False
    texts = {str(u.p_index): u.text for u in units}
    return all(k in texts and texts[k] == v for k, v in written.items())


def _byte_guard(fn, name: str):
    """B-bytes (decision 1): a build() that writes nothing returns the original bytes.

    build() with no written text (an empty mapping, or every key a source fallback)
    returns the upload exactly; so does build() of a monolingual format whose every
    written text equals its source unit (the identity mapping). A bilingual format
    (`writes_targets`) writes a target equal to the source on purpose, so only the empty
    case applies there. build_localized() is never short-circuited: a target-language
    build may stamp the language or give plurals the target's forms; an untouched
    EXPORT is returned as uploaded one level up (export._nothing_written). Only the
    OUTERMOST call decides; a filter's own nested build() calls run as written.
    """

    @functools.wraps(fn)
    def guarded(self, original, paragraph_texts, *args, **kwargs):
        if _BUILD_DEPTH.get() or not self.passthrough_untouched:
            return fn(self, original, paragraph_texts, *args, **kwargs)
        if (
            name == "build"
            and isinstance(paragraph_texts, dict)
            and isinstance(original, (bytes, bytearray))
        ):
            written = _written(paragraph_texts, kwargs)
            if not written and _parses(self, bytes(original)):
                return bytes(original)
            if (
                not args
                and set(kwargs) <= {"fallback_keys"}
                and not self.writes_targets
                and _is_identity(self, bytes(original), written)
            ):
                return bytes(original)
        token = _BUILD_DEPTH.set(_BUILD_DEPTH.get() + 1)
        try:
            return fn(self, original, paragraph_texts, *args, **kwargs)
        finally:
            _BUILD_DEPTH.reset(token)

    guarded._byte_guard = True  # type: ignore[attr-defined]
    return guarded


class Filter(ABC):
    fmt: str = "base"
    # B-bytes: False for a filter whose deliverable is a different file than the upload
    # (a PDF is re-typeset, an SDL project package comes back as a return package).
    passthrough_untouched: bool = True
    # B-bytes: a bilingual format (PO, XLIFF family, Qt TS, ...) where writing a target
    # equal to its source is a real change; build() never treats identity as untouched.
    writes_targets: bool = False

    def __init_subclass__(cls, **kwargs):
        super().__init_subclass__(**kwargs)
        for name in ("build", "build_localized"):
            fn = cls.__dict__.get(name)
            if fn is not None and not getattr(fn, "_byte_guard", False):
                setattr(cls, name, _byte_guard(fn, name))

    @abstractmethod
    def parse(self, data: bytes, provider: str | None = None) -> ParseResult:
        """Extract translatable units. `provider` is the engine to use for any
        vision OCR (scanned PDFs); text-based formats ignore it."""
        ...

    @abstractmethod
    def build(self, original: bytes, paragraph_texts: dict) -> bytes:
        """Rebuild the file by replacing paragraph text by p_index.

        paragraph_texts maps str(p_index) -> translated text. Untouched
        paragraphs keep their original content (and all non-text content —
        images, tables, headers — is preserved by editing the original)."""
        ...

    def build_localized(
        self, original: bytes, paragraph_texts: dict, target_locale: str | None = None
    ) -> bytes:
        """build() for a DELIVERABLE: the same rebuild, plus the TARGET language
        declaration (and text direction) wherever the format really carries one
        (VPAT 504.2.2; see app/filters/a11y.py). Formats with no language declaration
        (plain text, JSON, SRT, ...) inherit this default, which is exactly build().
        build() itself stays the byte-faithful rebuild the round-trip ratchets pin."""
        return self.build(original, paragraph_texts)


# The inherited build_localized (formats with no language declaration) is guarded too, so
# an export through it never pays the identity re-parse in its nested build().
Filter.build_localized = _byte_guard(Filter.build_localized, "build_localized")  # type: ignore[method-assign]

_FILTERS: dict[str, Filter] = {}
_ALIASES: dict[str, str] = {}  # file extension -> canonical fmt


def register(cls):
    """Class decorator: instantiate and register a filter by its `fmt`, plus any
    extra file extensions it declares in `exts` (e.g. .htm for the html filter)."""
    _FILTERS[cls.fmt] = cls()
    _ALIASES[cls.fmt] = cls.fmt
    for ext in getattr(cls, "exts", ()):
        _ALIASES[ext.lower()] = cls.fmt
    return cls


# Lazily imported filters: extension/fmt -> (module, canonical fmt). The module is
# imported (and registers itself) the first time one of its names is resolved, so a
# filter that only a few installs use (MadCap Flare projects) stays out of the import
# graph of every process that merely imports app.filters (the kernel pipeline bound).
_LAZY: dict[str, tuple[str, str]] = {}


def register_lazy(module: str, fmt: str, exts: tuple[str, ...] = ()) -> None:
    """Declare a filter by module path without importing it (see _LAZY)."""
    for name in (fmt, *exts):
        _LAZY[name.lower()] = (module, fmt)


def get_filter(fmt: str) -> Filter | None:
    """Resolve a filter by canonical fmt OR by any registered extension alias."""
    found = _FILTERS.get(_ALIASES.get((fmt or "").lower(), fmt))
    if found is None and (fmt or "").lower() in _LAZY:
        import importlib

        importlib.import_module(_LAZY[(fmt or "").lower()][0])
        found = _FILTERS.get(_ALIASES.get((fmt or "").lower(), fmt))
    return found


def supported_formats() -> list[str]:
    return sorted(set(_FILTERS) | {canon for _mod, canon in _LAZY.values()})


def supported_exts() -> list[str]:
    """Every file extension we accept (canonical fmts + their aliases)."""
    return sorted(set(_ALIASES) | set(_LAZY))
