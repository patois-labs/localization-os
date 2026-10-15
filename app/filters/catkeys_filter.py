"""Haiku catalog filter (.catkeys, the plain-text catalog of the Haiku Locale Kit).

Layout (Haiku src/tools/locale/PlainTextCatalog.cpp, MIT): UTF-8, one record per line,
fields separated by TAB.
  * Header: ``<archive version>\\t<language>\\t<app signature>\\t<fingerprint>``. The
    archive version is 1; the language is the lower-case English language name
    (``german``, ``portuguese (brazil)``).
  * Entries: ``<source>\\t<context>\\t<comment>\\t<translation>``. Source, comment and
    translation are escaped with backslash (``\\\\``, ``\\n``, ``\\t``, ``\\"``); the
    runtime reader also understands ``\\a \\b \\f \\r \\v \\xNN``.

What is translated: the TRANSLATION column of every entry whose source has a letter. The
unit text is the unescaped SOURCE string; build() writes the escaped translation into the
fourth field and leaves source, context, comment and every untouched line byte-identical.
build_localized() also sets the header language to the target's English name.

Fingerprint: Haiku computes it from the source strings only (HashMapCatalog.cpp,
ComputeFingerprint: the uint32 sum of CatKey hashes, HashFun h = 5*h + signed byte per
character, then 5*h + 1, chained over source, context and comment, unescaped). Translating
never changes a source, context or comment, so the header fingerprint is KEPT as it is
(it is what the application's compiled-in catalog check compares against).
``catkeys_fingerprint()`` recomputes it for diagnostics; it was checked against real
Haiku catalogs.

Documented in: docs/architecture/translation-pipeline.md
"""

from dataclasses import dataclass

from app.filters.base import (
    EmptyDocumentError,
    Filter,
    InputLimitError,
    ParseResult,
    TextUnit,
    UnsupportedEncodingError,
    check_line_budget,
    check_segment_cap,
    check_unit_texts,
    register,
)
from app.filters.kv_resource import KeyValueRecord  # Batch 1's shared record type

ARCHIVE_VERSION = "1"
_UTF8_BOM = b"\xef\xbb\xbf"
_MAX_BYTES = 32 * 1024 * 1024
_UNESCAPE = {
    "a": "\a",
    "b": "\b",
    "f": "\f",
    "n": "\n",
    "r": "\r",
    "t": "\t",
    "v": "\v",
    '"': '"',
}


class CatkeysParseError(ValueError):
    """A .catkeys upload that is not a Haiku plain-text catalog. Caught by the upload
    routes as a polite parse refusal."""


@dataclass
class _Entry:
    line_no: int
    source: str
    context: str
    comment: str
    raw_translation: str
    tr_start: int  # translation field span in the decoded text
    tr_end: int


@dataclass
class _Walk:
    text: str
    bom: bool
    header: list  # [version, language, signature, fingerprint]
    lang_span: tuple  # (start, end) of the header language field
    entries: list
    units: list  # entry index per unit


def unescape(raw: str) -> str:
    """Haiku parseQuotedChars: \\a \\b \\f \\n \\r \\t \\v \\" and \\xNN; any other escaped
    character (including a backslash) stands for itself."""
    if "\\" not in raw:
        return raw
    out = bytearray()
    i, n = 0, len(raw)
    while i < n:
        c = raw[i]
        if c == "\\" and i + 1 < n:
            nxt = raw[i + 1]
            if nxt == "x":
                hexpart = raw[i + 2 : i + 4]
                if len(hexpart) == 2 and all(h in "0123456789abcdefABCDEF" for h in hexpart):
                    out.append(int(hexpart, 16))
                    i += 4
                    continue
                out += nxt.encode("utf-8")
                i += 2
                continue
            out += _UNESCAPE.get(nxt, nxt).encode("utf-8")
            i += 2
            continue
        out += c.encode("utf-8")
        i += 1
    return out.decode("utf-8", "replace")


def escape(s: str) -> str:
    """Haiku escapeQuotedChars (\\\\, \\n, \\t, \\"), plus \\r so a translation can never
    break the line."""
    s = s.replace("\\", "\\\\").replace("\n", "\\n").replace("\t", "\\t")
    return s.replace('"', '\\"').replace("\r", "\\r")


def _hash(s: str, h: int) -> int:
    for b in s.encode("utf-8"):
        h = (5 * h + (b - 256 if b >= 128 else b)) & 0xFFFFFFFF
    return (5 * h + 1) & 0xFFFFFFFF


def _key_hash(source: str, context: str, comment: str) -> int:
    return _hash(comment, _hash(context, _hash(source, 0)))


def _decode(raw: bytes) -> tuple[str, bool]:
    raw = bytes(raw)
    if len(raw) > _MAX_BYTES:
        raise InputLimitError(
            f"This .catkeys file is larger than {_MAX_BYTES // (1024 * 1024)} MB. "
            "Split the catalog."
        )
    bom = raw.startswith(_UTF8_BOM)
    try:
        text = raw[3 if bom else 0 :].decode("utf-8")
    except UnicodeDecodeError as exc:
        raise UnsupportedEncodingError(
            f"This .catkeys file is not valid UTF-8 (byte at position {exc.start}); Haiku "
            "catalogs are UTF-8. Re-save the file as UTF-8."
        ) from exc
    if "\x00" in text:
        raise UnsupportedEncodingError(
            "This .catkeys file looks like a binary file, not a text catalog."
        )
    check_line_budget(text)
    return text, bom


def _walk(raw: bytes) -> _Walk:
    text, bom = _decode(raw)
    pos, n = 0, len(text)
    header = None
    lang_span = (0, 0)
    entries: list[_Entry] = []
    units: list[int] = []
    line_no = 0
    while pos < n:
        nl = text.find("\n", pos)
        end = n if nl < 0 else nl
        content_end = end - 1 if end > pos and text[end - 1] == "\r" else end
        line = text[pos:content_end]
        line_no += 1
        if header is None:
            fields = line.split("\t")
            if len(fields) != 4 or fields[0].strip() != ARCHIVE_VERSION:
                raise CatkeysParseError(
                    "This file is not a Haiku catalog: the first line must be "
                    "'1<TAB>language<TAB>signature<TAB>fingerprint'."
                )
            if not fields[3].strip().isdigit():
                raise CatkeysParseError("This Haiku catalog's header fingerprint is not a number.")
            header = fields
            ls = pos + len(fields[0]) + 1
            lang_span = (ls, ls + len(fields[1]))
        elif line:
            fields = line.split("\t", 3)
            if len(fields) != 4:
                raise CatkeysParseError(
                    f"Line {line_no} of this Haiku catalog does not have four TAB-separated "
                    "fields (source, context, comment, translation)."
                )
            tr_start = pos + len(line) - len(fields[3])
            e = _Entry(
                line_no,
                unescape(fields[0]),
                fields[1],
                unescape(fields[2]),
                fields[3],
                tr_start,
                content_end,
            )
            if any(c.isalpha() for c in e.source):
                units.append(len(entries))
                check_segment_cap(len(units))
            entries.append(e)
        pos = end + 1
    if header is None:
        raise CatkeysParseError("This .catkeys file is empty.")
    return _Walk(text, bom, header, lang_span, entries, units)


def catkeys_fingerprint(data: bytes) -> tuple[int, int]:
    """(fingerprint declared in the header, fingerprint recomputed from the entries the
    way Haiku's HashMapCatalog::ComputeFingerprint does)."""
    walk = _walk(data)
    total = 0
    for e in walk.entries:
        total = (total + _key_hash(e.source, e.context, e.comment)) & 0xFFFFFFFF
    return int(walk.header[3].strip()), total


def language_name(locale: str) -> str:
    """The header language for a BCP 47 target, as Haiku writes it: the lower-case English
    name (`de` -> `german`, `pt-BR` -> `portuguese (brazil)`). Falls back to the code."""
    code = (locale or "").strip()
    if not code:
        return ""
    try:
        from babel import Locale

        name = Locale.parse(code.replace("-", "_")).get_display_name("en")
    except Exception:  # unknown or malformed locale: keep the code
        name = None
    out = (name or code).lower()
    return out.replace("\t", " ").replace("\r", " ").replace("\n", " ")


@register
class CatkeysFilter(Filter):
    fmt = "catkeys"
    writes_targets = True  # B-bytes: a target equal to its source is a real write

    def parse(self, data: bytes, provider: str | None = None) -> ParseResult:
        walk = _walk(data)
        texts = [walk.entries[i].source for i in walk.units]
        check_unit_texts(texts)
        if not texts:
            raise EmptyDocumentError("This Haiku catalog has no strings to translate.")
        units = [TextUnit(p_index=k, text=t) for k, t in enumerate(texts)]
        skeleton = {
            "format": "catkeys",
            "units": [{"p_index": u.p_index, "text": u.text, "style": ""} for u in units],
        }
        return ParseResult(skeleton=skeleton, units=units)

    def build(self, original: bytes, paragraph_texts: dict) -> bytes:
        return self._build(original, paragraph_texts, None)

    def build_localized(
        self, original: bytes, paragraph_texts: dict, target_locale: str | None = None
    ) -> bytes:
        """build() plus the header language set to the target (the fingerprint is kept:
        it depends on the source strings only)."""
        return self._build(original, paragraph_texts, target_locale)

    def _build(self, original: bytes, paragraph_texts: dict, target_locale: str | None) -> bytes:
        walk = _walk(original)
        edits: list[tuple[int, int, str]] = []
        lang = language_name(target_locale) if target_locale else ""
        if lang and lang != walk.header[1]:
            edits.append((walk.lang_span[0], walk.lang_span[1], lang))
        for k, e_idx in enumerate(walk.units):
            new = paragraph_texts.get(str(k))
            if new is None:
                continue
            e = walk.entries[e_idx]
            if new == unescape(e.raw_translation):
                continue
            edits.append((e.tr_start, e.tr_end, escape(new)))
        if not edits:
            return bytes(original)
        text, out, last = walk.text, [], 0
        for start, end, rep in edits:  # header first, then entries: already in file order
            out.append(text[last:start])
            out.append(rep)
            last = end
        out.append(text[last:])
        return (_UTF8_BOM if walk.bom else b"") + "".join(out).encode("utf-8")

    def records(self, data: bytes) -> list[KeyValueRecord]:
        """Key-based view (Batch 9): one record per entry (translatable or not). ``key`` is
        the unescaped source string, ``context`` the Haiku context (B_TRANSLATION_CONTEXT),
        ``comment`` the developer comment, ``value`` the current translation (unescaped).
        A catalog key is (source, context, comment); a repeat of all three is
        ``duplicate``."""
        walk = _walk(data)
        unit_of = {e_idx: k for k, e_idx in enumerate(walk.units)}
        records: list[KeyValueRecord] = []
        seen: set = set()
        for e_idx, e in enumerate(walk.entries):
            ident = (e.source, e.context, e.comment)
            records.append(
                KeyValueRecord(
                    key=e.source,
                    value=unescape(e.raw_translation),
                    comment=e.comment,
                    context=e.context,
                    unit_index=unit_of.get(e_idx),
                    duplicate=ident in seen,
                )
            )
            seen.add(ident)
        return records
