"""Microsoft HTML Help 1.x project files: .hhc (table of contents), .hhk (index), .hhp (project).

What is translated
  * .hhc / .hhk (sitemap files): inside every ``<OBJECT type="text/sitemap">``, the value of
    each ``<param name="Name" value="...">`` (the Contents entry, the index keyword, and in an
    index the extra topic titles that follow the keyword) and of each ``<param name="Keyword">``.
    Everything else is never touched: Local, URL, ImageNumber, New, Merge, Type, FrameName,
    WindowName, Comment, the ``text/site properties`` object, comments and markup.
  * ``<param name="See Also" value="X">`` in an index is not a unit of its own: it must name an
    index keyword exactly, so on build it is rewritten to the translation of the keyword whose
    source text is X (and left alone when no keyword X was translated).
  * .hhp (project file): ``Title=`` in [OPTIONS] and the caption (first quoted field) of each
    [WINDOWS] definition. On a localized build ``Language=`` is set to the target language's
    LCID ("0x40c French (France)"). Every other line is kept byte for byte.

Encodings (#509 rules, decoded explicitly here, never through the shared UTF-8-only path):
HTML Help 1.x builds its Contents, Index and Search panes in the Windows ANSI code page of the
help language, so these files are normally ANSI. Decode order: a UTF-8 or UTF-16 BOM; valid
UTF-8 (pure ASCII counts as ANSI, code page unknown); a charset the file declares in a meta tag;
else Windows-1252. A binary-looking or undecodable file is refused. The decode is the same at
parse and at build, so unit indices always line up.

Output encoding: build() writes the source encoding (byte-identical when nothing changed).
build_localized() writes an ANSI file in the TARGET language's ANSI code page (Russian
windows-1251, Japanese shift_jis/932, ...), rewrites a declared meta charset and the charset
field of a site-properties ``Font`` param to match, and refuses with a clear message
(HtmlHelpEncodingError, a ValueError) when a character cannot be represented in that code page.
With HTMLHELP_UTF8_FALLBACK=1 (environment, restart) such a file is written as UTF-8 instead and
a warning is logged: HTML Help Workshop will not show those characters correctly in the
Contents/Index panes. A source that is already UTF-8 (BOM or non-ASCII UTF-8) or UTF-16 stays in
that encoding.

Sources (format behaviour verified against): Microsoft HTML Help 1.4 documentation on Microsoft
Learn (previous-versions/windows/desktop/htmlhelp) and the community sitemap spec
(chmspec.nongnu.org/latest/Sitemap.html).

Documented in: docs/architecture/translation-pipeline.md
"""

import codecs
import html
import locale as _pylocale
import logging
import re

from app import config
from app.filters import html_guard
from app.filters.base import (
    _DECL_RE,
    _DECLARED_OK,
    EmptyDocumentError,
    Filter,
    InputLimitError,
    ParseResult,
    TextUnit,
    UnsupportedEncodingError,
    _codec_name,
    _looks_binary,
    check_line_budget,
    check_segment_cap,
    check_unit_texts,
    note_source_encoding,
    register,
)
from app.filters.code_pages import CP_INFO, LANG_CP, split_locale, target_code_page
from app.filters.xml_text import XmlTargetError

_LOG = logging.getLogger(__name__)

TRANSLATED_PARAMS = ("name", "keyword")
SEE_ALSO = "see also"

# ---------------------------------------------------------------------------------------------
# Code pages
# ---------------------------------------------------------------------------------------------
# Windows ANSI code page per language and per code page: shared with the #509 text decode
# (decision 22) in app/filters/code_pages.py. A language with no ANSI code page we know of:
# the output then tries the source code page.
_LANG_CP = LANG_CP
_CP_INFO = CP_INFO
_SOURCE_OK = set(_DECLARED_OK) | {c for c in (_codec_name(i[0]) for i in _CP_INFO.values()) if c}
_split_locale = split_locale


def _build_lcids() -> dict[str, int]:
    out: dict[str, int] = {}
    for lcid, name in sorted(_pylocale.windows_locale.items()):
        if lcid < 0x100 or "_" not in name:
            continue  # neutral LCIDs (0x4 zh_CHS) are not what HHW writes
        out.setdefault(name.lower(), lcid)
        out.setdefault(name.split("_")[0].lower(), lcid)  # first (primary) LCID of the language
    return out


_LCIDS = _build_lcids()


def target_lcid(target_locale: str | None) -> tuple[int, str] | None:
    """(LCID, English display name) for a target locale, as HHW writes Language=, or None."""
    lang, script, region = _split_locale(target_locale or "")
    if not lang:
        return None
    if lang == "zh" and not region:
        region = "TW" if script == "Hant" else "CN"
    lcid = _LCIDS.get(f"{lang}_{region}".lower()) if region else None
    if lcid is None:
        lcid = _LCIDS.get(lang)
    if lcid is None:
        return None
    name = ""
    try:
        from babel import Locale

        posix = _pylocale.windows_locale[lcid]
        name = Locale.parse(posix).get_display_name("en") or ""
    except Exception:  # an LCID name babel does not know (sr_SP): fall back to the tag
        try:
            from babel import Locale

            name = Locale.parse(target_locale.replace("-", "_")).get_display_name("en") or ""
        except Exception:
            name = ""
    import unicodedata  # ASCII only: the name must fit any target code page

    name = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode("ascii")
    return lcid, name


# ---------------------------------------------------------------------------------------------
# Decode / encode
# ---------------------------------------------------------------------------------------------
class HtmlHelpEncodingError(XmlTargetError):
    """The translated file cannot be written in the target language's ANSI code page. The
    message names the code page, the first character that does not fit and the option."""


class _Enc:
    __slots__ = ("ansi", "bom", "codec")

    def __init__(self, codec: str, bom: bytes = b"", ansi: bool = False):
        self.codec, self.bom, self.ansi = codec, bom, ansi


def check_htmlhelp_budget(raw: bytes) -> None:
    """Pre-parse budget (#571 pattern): every sitemap entry needs several '<'; forty per
    allowed segment is far above any real Contents or Index file."""
    if raw.count(b"<") > 40 * config.MAX_DOCUMENT_SEGMENTS:
        raise InputLimitError(
            f"This HTML Help file has far more entries than MAX_DOCUMENT_SEGMENTS allows "
            f"({config.MAX_DOCUMENT_SEGMENTS}). Split it into smaller files."
        )


def _hint_codec(hint_cp: int | None) -> str | None:
    """The Python codec of an HTML Help project's ANSI code page (decision 21), or None."""
    info = _CP_INFO.get(hint_cp) if isinstance(hint_cp, int) else None
    return _codec_name(info[0]) if info else None


def _decode(raw: bytes, hint_cp: int | None = None) -> tuple[str, _Enc]:
    """`hint_cp`: the code page of the .hhp project's Language= (decision 21, the maintainer
    2026-10-11). It is used only for a file that declares no charset of its own, in place
    of the Windows-1252 default; a BOM, valid UTF-8 and a declared charset still win."""
    raw = bytes(raw)
    hint = _hint_codec(hint_cp)
    if raw.startswith(b"\xef\xbb\xbf"):
        enc = _Enc("utf-8", b"\xef\xbb\xbf")
    elif raw.startswith(b"\xff\xfe"):
        enc = _Enc("utf-16-le", b"\xff\xfe")
    elif raw.startswith(b"\xfe\xff"):
        enc = _Enc("utf-16-be", b"\xfe\xff")
    else:
        enc = None
    if enc is not None:
        try:
            return raw[len(enc.bom) :].decode(enc.codec), enc
        except UnicodeDecodeError as exc:
            raise UnsupportedEncodingError(
                f"This HTML Help file starts with a {enc.codec} byte-order mark but is not "
                f"valid {enc.codec} (byte at position {exc.start + len(enc.bom)}); re-save the "
                "file as UTF-8 or in its Windows ANSI code page."
            ) from exc
    if _looks_binary(raw):
        raise UnsupportedEncodingError(
            "file looks like a binary file, not an HTML Help file; re-save the file as UTF-8 "
            "or in its Windows ANSI code page."
        )
    try:
        text = raw.decode("utf-8")
        if text.isascii():
            m = _DECL_RE.search(text[:4096])
            declared = _codec_name(m.group(2)) if m else None
            if declared and declared in _SOURCE_OK and declared not in ("utf-8", "utf-16"):
                return text, _Enc(declared, ansi=True)
            return text, _Enc(hint or "cp1252", ansi=True)
        return text, _Enc("utf-8")
    except UnicodeDecodeError:
        pass
    if _looks_binary(raw):
        raise UnsupportedEncodingError(
            "file is not valid UTF-8 and looks like a binary file, not an HTML Help file; "
            "re-save the file as UTF-8 or in its Windows ANSI code page."
        )
    m = _DECL_RE.search(raw[:4096].decode("ascii", "replace"))
    declared = _codec_name(m.group(2)) if m else None
    last = None
    for codec in (declared, hint, "cp1252"):
        if not codec or codec not in _SOURCE_OK or codec in ("utf-8", "utf-16"):
            continue
        try:
            return raw.decode(codec), _Enc(codec, ansi=True)
        except UnicodeDecodeError as exc:
            last = exc
            continue
    raise UnsupportedEncodingError(
        "file is not valid UTF-8 and could not be read as Windows-1252 or its declared "  # noqa: UP031
        "charset either (byte 0x%02x at position %d); re-save the file as UTF-8 or in its "
        "Windows ANSI code page." % (raw[last.start], last.start)
    )


# Windows-1258 (Vietnamese) has the base letters with their vowel marks (â ă ê ô ơ ư đ) but
# writes most toned letters as base + a COMBINING tone mark, the way Windows' own code page
# 1258 conversion does (#681: precomposed NFC text such as 'ổ' was refused outright).
_CP1258_TONES = frozenset("̣̀́̃̉")


def _cp1258_text(text: str) -> str:
    import unicodedata

    out = []
    for ch in text:
        try:
            ch.encode("cp1258")
            out.append(ch)
            continue
        except UnicodeEncodeError:
            pass
        d = unicodedata.normalize("NFD", ch)
        marks = d[1:]
        vowel = "".join(m for m in marks if m not in _CP1258_TONES)
        base = unicodedata.normalize("NFC", d[0] + vowel)
        alt = base + "".join(m for m in marks if m in _CP1258_TONES)
        try:
            alt.encode("cp1258")
            out.append(alt)
        except UnicodeEncodeError:
            out.append(ch)  # no representation: _encode refuses it with the character named
    return "".join(out)


def _utf8_fallback_enabled() -> bool:
    return bool(getattr(config, "HTMLHELP_UTF8_FALLBACK", False))


# #715: typographic characters a translation carries that no Windows ANSI code page needs to
# refuse the file over: French writes a NARROW no-break space (U+202F) before `: ; ! ?`, and
# thin spaces, non-breaking hyphens and zero-width marks come from TM and engines. Written as
# the nearest code-page character, like the STL and SCC filters do; letters are never mapped.
_NOBREAK_SPACES = frozenset("\u2007\u202f")
_HYPHENS = frozenset("\u2010\u2011\u2043\u2212")
_ZERO_WIDTH = frozenset("\u200b\u2060\ufeff")


def _typographic_fallback(text: str, codec: str) -> str:
    import unicodedata

    try:
        text.encode(codec)
        return text  # the common case: one pass, nothing to map
    except UnicodeEncodeError:
        pass
    out = []
    for ch in text:
        try:
            ch.encode(codec)
            out.append(ch)
            continue
        except UnicodeEncodeError:
            pass
        if ch in _ZERO_WIDTH:
            continue
        if ch in _HYPHENS:
            out.append("-")
        elif ch in _NOBREAK_SPACES or unicodedata.category(ch) == "Zs":
            nbsp = ch in _NOBREAK_SPACES
            try:
                "\u00a0".encode(codec)
                out.append("\u00a0" if nbsp else " ")
            except UnicodeEncodeError:
                out.append(" ")
        else:
            out.append(ch)  # no representation: _encode refuses it with the character named
    return "".join(out)


def _for_codepage(text: str, codec: str) -> str:
    text = _cp1258_text(text) if codecs.lookup(codec).name == "cp1258" else text
    return _typographic_fallback(text, codec)


def _encode(text: str, codec: str, bom: bytes, label: str) -> bytes:
    try:
        return bom + _for_codepage(text, codec).encode(codec)
    except UnicodeEncodeError as exc:
        ch = exc.object[exc.start]
        if _utf8_fallback_enabled():
            _LOG.warning(
                "HTML Help file written as UTF-8: %r (U+%04X) does not fit the %s code page; "
                "HTML Help Workshop will not show it correctly in the Contents/Index panes",
                ch,
                ord(ch),
                label,
            )
            return text.encode("utf-8")
        raise HtmlHelpEncodingError(
            f"The translated HTML Help file cannot be written in the {label} code page: the "
            f"character {ch!r} (U+{ord(ch):04X}) has no representation there, and HTML Help "
            "1.x reads Contents, Index and project files in the help language's Windows ANSI "
            "code page. Fix the text (a translation or an untranslated value), or have the "
            "operator set HTMLHELP_UTF8_FALLBACK=1 to write UTF-8 instead (with a warning)."
        ) from None


# ---------------------------------------------------------------------------------------------
# Sitemap (.hhc / .hhk) walk
# ---------------------------------------------------------------------------------------------
# Gate (train-9): names and unquoted values never contain '<' (HTML: an unquoted value cannot),
# and each attribute is atomic with a possessive list, so a tag that never closes fails in one
# pass instead of backtracking across the rest of the file (20 KB of "<a x " took 17 s).
_ATTR_PART = r"""(?>[^\s=<>/"']+(?:\s*=\s*(?:"[^"]*"|'[^']*'|[^\s<>"']*))?)"""
_TAG = re.compile(r"<(/?)([A-Za-z][\w:-]*)((?:\s+" + _ATTR_PART + r")*+)\s*/?>")
_ATTR = re.compile(r"""([^\s=<>/"']+)(?:\s*=\s*(?:"([^"]*)"|'([^']*)'|([^\s<>"']*)))?""")


class _Span:
    __slots__ = ("end", "kind", "quote", "start", "text")

    def __init__(self, start, end, text, kind, quote):
        self.start, self.end, self.text, self.kind, self.quote = start, end, text, kind, quote


def _attrs(tag_m) -> dict[str, tuple[int, int, str, str]]:
    """lower name -> (value start, value end, raw value, quote) in document offsets."""
    out: dict[str, tuple[int, int, str, str]] = {}
    base = tag_m.start(3)
    for am in _ATTR.finditer(tag_m.group(3)):
        name = am.group(1).lower()
        for gi, q in ((2, '"'), (3, "'"), (4, "")):
            if am.group(gi) is not None:
                out.setdefault(name, (base + am.start(gi), base + am.end(gi), am.group(gi), q))
                break
        else:
            out.setdefault(name, (base + am.end(1), base + am.end(1), "", ""))
    return out


def _translatable(value: str) -> bool:
    return any(c.isalnum() for c in value)


def _walk_sitemap(text: str) -> tuple[list[_Span], list[_Span], list[_Span]]:
    """(units, see_also spans, font spans) in document order."""
    units: list[_Span] = []
    see_also: list[_Span] = []
    fonts: list[_Span] = []
    obj_type: str | None = None
    pos = 0
    while True:
        lt = text.find("<", pos)
        if lt < 0:
            break
        if text.startswith("<!--", lt):
            end = text.find("-->", lt + 4)
            pos = len(text) if end < 0 else end + 3
            continue
        m = _TAG.match(text, lt)
        if not m:
            pos = lt + 1  # a stray '<' in lenient HTML: not a tag
            continue
        pos = m.end()
        closing, tag = m.group(1), m.group(2).lower()
        if tag == "object":
            if closing:
                obj_type = None
            else:
                t = _attrs(m).get("type")
                obj_type = html.unescape(t[2]).strip().lower() if t else ""
            continue
        if tag != "param" or closing or obj_type is None:
            continue
        a = _attrs(m)
        if "name" not in a or "value" not in a:
            continue
        pname = html.unescape(a["name"][2]).strip().lower()
        vs, ve, vraw, quote = a["value"]
        value = html.unescape(vraw)
        if obj_type == "text/sitemap":
            if pname in TRANSLATED_PARAMS and _translatable(value):
                units.append(_Span(vs, ve, value, pname, quote))
                check_segment_cap(len(units))
            elif pname == SEE_ALSO and value.strip():
                see_also.append(_Span(vs, ve, value, pname, quote))
        elif obj_type == "text/site properties" and pname == "font":
            fonts.append(_Span(vs, ve, value, pname, quote))
    return units, see_also, fonts


# #676: the only attribute value a sitemap translation changes
_FREE_IN_SITEMAP = frozenset((("param", "value"),))


def _escape_attr(s: str, quote: str) -> str:
    s = s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    s = s.replace('"', "&quot;")
    if quote == "'":
        s = s.replace("'", "&#39;")
    s = s.replace("\r", " ").replace("\n", " ")
    return s if quote else f'"{s}"'


def _load(raw: bytes, capped: bool, hint_cp: int | None = None) -> tuple[str, _Enc]:
    raw = bytes(raw)
    if capped:
        check_htmlhelp_budget(raw)
    text, enc = _decode(raw, hint_cp)
    check_line_budget(text)
    return text, enc


def _out_encoding(enc: _Enc, target_locale: str | None, localized: bool):
    """(codec, bom, label, code page or None) for the rebuilt file."""
    if not enc.ansi:
        return enc.codec, enc.bom, enc.codec, None
    cp = target_code_page(target_locale) if localized else None
    if cp is not None:
        codec, _label, _cs = _CP_INFO[cp]
        return codec, b"", f"Windows-{cp}" if cp >= 1250 else f"Windows {cp}", cp
    for n, (codec, _label, _cs) in _CP_INFO.items():
        if _codec_name(codec) == _codec_name(enc.codec):
            return enc.codec, b"", f"source (Windows {n})", n
    return enc.codec, b"", f"source ({enc.codec})", None


def _meta_charset_edit(text: str, cp: int | None, utf8: bool):
    m = _DECL_RE.search(text[:4096])
    if not m or (cp is None and not utf8):
        return None
    label = "UTF-8" if utf8 else _CP_INFO[cp][1]
    return (m.start(2), m.end(2), label)


def _font_edit(sp: _Span, cp: int | None):
    """`Font` = "face,size,charset": set the charset to the target code page's GDI charset."""
    if cp is None:
        return None
    parts = sp.text.split(",")
    if len(parts) != 3 or not parts[2].strip().isdigit():
        return None
    want = str(_CP_INFO[cp][2])
    if parts[2].strip() == want:
        return None
    parts[2] = want
    return (sp.start, sp.end, _escape_attr(",".join(parts), sp.quote))


def _apply(text: str, edits: list[tuple[int, int, str]]) -> str:
    out, last = [], 0
    for s, e, new in sorted(edits, key=lambda x: x[0]):
        out.append(text[last:s])
        out.append(new)
        last = e
    out.append(text[last:])
    return "".join(out)


def _finish(text, enc, edits, target_locale, localized, extra_edits):
    """Encode the edited text; ANSI targets get the meta charset / Font charset rewritten."""
    codec, bom, label, cp = _out_encoding(enc, target_locale, localized)
    all_edits = list(edits)
    if localized and enc.ansi:
        all_edits += [e for e in extra_edits(cp) if e]
    new_text = _apply(text, all_edits)
    try:
        _for_codepage(new_text, codec).encode(codec)
        fits = True
    except UnicodeEncodeError:
        fits = False
    if not fits and _utf8_fallback_enabled() and enc.ansi:
        meta = _meta_charset_edit(text, cp, True)
        if meta:
            new_text = _apply(text, [*all_edits, meta])
        return _encode(new_text, codec, bom, label)  # logs the warning, writes UTF-8
    if localized and enc.ansi:
        meta = _meta_charset_edit(text, cp, False)
        if meta and _codec_name(text[meta[0] : meta[1]]) != _codec_name(_CP_INFO[cp][0]):
            new_text = _apply(text, [*all_edits, meta])
    return _encode(new_text, codec, bom, label)


@register
class HhcFilter(Filter):
    """HTML Help table of contents (.hhc). The index (.hhk) is the same sitemap format."""

    fmt = "hhc"
    kind = "Contents"

    def parse(
        self, data: bytes, provider: str | None = None, *, hhp_code_page: int | None = None
    ) -> ParseResult:
        """`hhp_code_page`: the ANSI code page of the help project's language (.hhp
        Language=), used when the file declares none (decision 21); stamped on the
        skeleton when it decided the decode, so every export reads the file the same way."""
        text, enc = _load(data, capped=True, hint_cp=hhp_code_page)
        spans, _see, _fonts = _walk_sitemap(text)
        texts = [s.text for s in spans]
        check_unit_texts(texts)
        if not texts:
            raise EmptyDocumentError(
                f"This HTML Help {self.kind} file has no entry names (param Name / Keyword "
                'inside OBJECT type="text/sitemap") to translate.'
            )
        units = [TextUnit(p_index=i, text=t) for i, t in enumerate(texts)]
        skeleton = {
            "format": self.fmt,
            "units": [{"p_index": u.p_index, "text": u.text, "style": ""} for u in units],
        }
        if enc.codec != "utf-8":
            note_source_encoding(skeleton, enc.codec)
        if enc.ansi and hhp_code_page is not None and enc.codec == _hint_codec(hhp_code_page):
            skeleton["hhp_code_page"] = hhp_code_page
        return ParseResult(skeleton=skeleton, units=units)

    def _rebuild(self, original, paragraph_texts, target_locale, localized, hint_cp=None):
        text, enc = _load(original, capped=False, hint_cp=hint_cp)
        spans, see_also, fonts = _walk_sitemap(text)
        edits, renamed = [], {}
        agreed = None

        def value(sp, new):
            # #676: a value a browser-equivalent parse does not read at this same span is
            # written opaque to every HTML5 tag state (this walker is not that parser)
            nonlocal agreed
            if agreed is None:
                agreed = html_guard.agreed_value_spans(text)
            out = _escape_attr(new, sp.quote)
            if (sp.start, sp.end, sp.quote) not in agreed:
                out = html_guard.strict_escape(out)
            return out

        for i, sp in enumerate(spans):
            new = paragraph_texts.get(str(i))
            if new is None or new == sp.text:
                continue
            renamed.setdefault(sp.text, new)
            edits.append((sp.start, sp.end, value(sp, new)))
        for sp in see_also:  # keep See Also cross-references pointing at the renamed keyword
            new = renamed.get(sp.text)
            if new is not None:
                edits.append((sp.start, sp.end, value(sp, new)))
        if not edits and not localized:
            return bytes(original)
        # #676: the translated sitemap must parse, in a browser, into the source's elements
        html_guard.check_build(text, _apply(text, edits), extra=_FREE_IN_SITEMAP, xml=False)
        return _finish(
            text,
            enc,
            edits,
            target_locale,
            localized,
            lambda cp: [_font_edit(f, cp) for f in fonts],
        )

    def build(
        self, original: bytes, paragraph_texts: dict, *, hhp_code_page: int | None = None
    ) -> bytes:
        return self._rebuild(original, paragraph_texts, None, False, hhp_code_page)

    def build_localized(
        self,
        original: bytes,
        paragraph_texts: dict,
        target_locale: str | None = None,
        *,
        hhp_code_page: int | None = None,
    ) -> bytes:
        if not target_locale:
            return self.build(original, paragraph_texts, hhp_code_page=hhp_code_page)
        return self._rebuild(original, paragraph_texts, target_locale, True, hhp_code_page)


@register
class HhkFilter(HhcFilter):
    """HTML Help index (.hhk): keywords (first Name, or Keyword), extra topic titles (later
    Names in the same entry) translated; See Also follows its keyword's translation."""

    fmt = "hhk"
    kind = "Index"


# ---------------------------------------------------------------------------------------------
# Project file (.hhp)
# ---------------------------------------------------------------------------------------------
_SECTION = re.compile(r"\A\s*\[([^\]]*)\]\s*\Z")
_KEYVAL = re.compile(r"\A(\s*)([^=\r\n]+?)(\s*=[ \t]*)(.*?)([ \t]*)\Z")
_WINDOW = re.compile(r'\A(\s*[^=\r\n]+?\s*=\s*")([^"\r\n]*)(")')


def _walk_hhp(text: str):
    """(units, language span or None). Units: [OPTIONS] Title= and [WINDOWS] captions."""
    units: list[_Span] = []
    lang = None
    section = ""
    pos = 0
    for line in text.splitlines(keepends=True):
        body = line.rstrip("\r\n")
        start = pos
        pos += len(line)
        sm = _SECTION.match(body)
        if sm:
            section = sm.group(1).strip().upper()
            continue
        if body.lstrip().startswith(";"):
            continue
        if section == "OPTIONS":
            km = _KEYVAL.match(body)
            if not km:
                continue
            key = km.group(2).strip().lower()
            vs, ve = start + km.start(4), start + km.end(4)
            if key == "title" and _translatable(km.group(4)):
                units.append(_Span(vs, ve, km.group(4), "title", "raw"))
                check_segment_cap(len(units))
            elif key == "language":
                lang = _Span(vs, ve, km.group(4), "language", "raw")
        elif section == "WINDOWS":
            wm = _WINDOW.match(body)
            if wm and _translatable(wm.group(2)):
                units.append(_Span(start + wm.start(2), start + wm.end(2), wm.group(2), "w", '"'))
                check_segment_cap(len(units))
    return units, lang


def hhp_code_page(data: bytes) -> int | None:
    """The ANSI code page of an HTML Help project's language: `Language=0x419 Russian` in
    [OPTIONS] -> 1251 (decision 21). None when the file names no language, names one with
    no ANSI code page we know, or cannot be read."""
    try:
        text, _enc = _load(data, capped=True)
        _units, lang = _walk_hhp(text)
    except (ValueError, InputLimitError):
        return None
    if lang is None:
        return None
    token = (lang.text.split() or [""])[0]
    try:
        lcid = int(token, 16) if token.lower().startswith("0x") else int(token)
    except ValueError:
        return None
    posix = _pylocale.windows_locale.get(lcid)
    return target_code_page(posix) if posix else None


def _hhp_value(new: str, kind: str) -> str:
    new = new.replace("\r", " ").replace("\n", " ").strip()
    return new.replace('"', "'") if kind == "w" else new


@register
class HhpFilter(Filter):
    """HTML Help project file (.hhp): Title= and window captions; Language= on localized build."""

    fmt = "hhp"

    def parse(self, data: bytes, provider: str | None = None) -> ParseResult:
        text, enc = _load(data, capped=True)
        spans, _lang = _walk_hhp(text)
        texts = [s.text for s in spans]
        check_unit_texts(texts)
        if not texts:
            raise EmptyDocumentError(
                "This HTML Help project file has no Title= or window caption to translate."
            )
        units = [TextUnit(p_index=i, text=t) for i, t in enumerate(texts)]
        skeleton = {
            "format": "hhp",
            "units": [{"p_index": u.p_index, "text": u.text, "style": ""} for u in units],
        }
        if enc.codec != "utf-8":
            note_source_encoding(skeleton, enc.codec)
        return ParseResult(skeleton=skeleton, units=units)

    def _rebuild(self, original, paragraph_texts, target_locale, localized):
        text, enc = _load(original, capped=False)
        spans, lang = _walk_hhp(text)
        edits = []
        for i, sp in enumerate(spans):
            new = paragraph_texts.get(str(i))
            if new is None or new == sp.text:
                continue
            edits.append((sp.start, sp.end, _hhp_value(new, sp.kind)))
        if localized and lang is not None:
            got = target_lcid(target_locale)
            if got:
                lcid, name = got
                value = f"0x{lcid:x} {name}".rstrip()
                if value != lang.text:
                    edits.append((lang.start, lang.end, value))
        if not edits and not localized:
            return bytes(original)
        return _finish(text, enc, edits, target_locale, localized, lambda cp: [])

    def build(self, original: bytes, paragraph_texts: dict) -> bytes:
        return self._rebuild(original, paragraph_texts, None, False)

    def build_localized(
        self, original: bytes, paragraph_texts: dict, target_locale: str | None = None
    ) -> bytes:
        if not target_locale:
            return self.build(original, paragraph_texts)
        return self._rebuild(original, paragraph_texts, target_locale, True)


# Reference codecs so an unknown-name refactor fails at import, not at the first export.
for _c, _l, _s in _CP_INFO.values():
    codecs.lookup(_c)
