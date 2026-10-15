"""gettext MO filter: compiled message catalogs (.mo).

An MO file is the binary form of a PO catalog (GNU gettext manual, "The Format of GNU MO
Files": https://www.gnu.org/software/gettext/manual/html_node/MO-Files.html). This filter
reads it into the SAME entries `po_filter` works on (context, msgid, msgid_plural, the
indexed msgstr slots, the header), so an MO upload yields exactly the units a PO upload of
the same catalog yields, and every PO rule (fuzzy cleanup, Plural-Forms switched for the
target language) applies unchanged. build() writes a valid MO back; `mo_to_po` /
`build_po` give the PO export of an MO upload.

Layout (all integers 32-bit, in the byte order the magic number announces):

    0   magic 0x950412de          16  T  offset of the translation table
    4   file format revision      20  S  hash table size (0 = no hash table)
    8   N  number of strings      24  H  hash table offset
    12  O  offset of the original table
    revision minor 1 adds, from 28: system-dependent segments and strings.

Each table holds N (length, offset) pairs; each string is `length` bytes at `offset`,
NUL terminated. A context is `msgctxt EOT msgid`; plural forms are NUL separated.

Why not `babel.messages.mofile.read_mo` for the parse: it does no bounds checking before
slicing (an offset past the end reads as an empty string), keeps a context as bytes,
keys the catalog by msgid alone and rebuilds the header from its own field list
(lower-cased, reordered, unknown fields dropped). Its magic numbers are reused here; the
reader below validates every count and offset first and keeps the header text verbatim.
Babel's `write_mo` drops the header's field order the same way, so the writer below
follows the manual directly (sorted originals, no hash table: S = 0 is valid, and GNU
libintl then binary-searches the sorted table).
"""

import re
import struct

from babel.messages.mofile import BE_MAGIC, LE_MAGIC

from app.filters import po_filter
from app.filters.base import (
    _DECLARED_OK,
    Filter,
    InputLimitError,
    ParseResult,
    UnsupportedEncodingError,
    _codec_name,
    check_segment_cap,
    check_unit_texts,
    register,
)

_HEADER_WORDS = 7  # magic, revision, N, O, T, S, H
_SYSDEP_WORDS = 5  # revision minor >= 1: M, offset, sysdep originals count, two offsets
_CHARSET_RE = re.compile(rb"(?im)^(Content-Type:[^\n]*?\bcharset\s*=\s*)([A-Za-z0-9_.:-]{1,40})")


class InvalidMoFile(UnsupportedEncodingError):
    """The upload is not a readable MO file (bad magic, unsupported revision, a count or
    offset that points outside the file). An UnsupportedEncodingError subclass, so every
    surface shows the plain message (422 on the API), never a generic 'parse failed'."""


def _u32(buf: bytes, off: int, endian: str) -> int:
    return struct.unpack_from(endian + "I", buf, off)[0]


def _header(buf: bytes) -> tuple[str, int, int, int]:
    """Validate the fixed header; returns (endian, N, O, T)."""
    if len(buf) < _HEADER_WORDS * 4:
        raise InvalidMoFile("This is not a gettext MO file: it is shorter than the MO header.")
    magic = struct.unpack_from("<I", buf, 0)[0]
    if magic == LE_MAGIC:
        endian = "<"
    elif magic == BE_MAGIC:
        endian = ">"
    else:
        raise InvalidMoFile("This is not a gettext MO file (bad magic number).")
    revision, n, o, t, s, h = (_u32(buf, 4 * i, endian) for i in range(1, _HEADER_WORDS))
    major, minor = revision >> 16, revision & 0xFFFF
    if major > 1:
        raise InvalidMoFile(f"Unsupported MO file format revision {major}.{minor}.")
    from app import config

    if n > config.MAX_DOCUMENT_SEGMENTS:
        raise InputLimitError(
            f"This MO file has {n} strings, more than MAX_DOCUMENT_SEGMENTS "
            f"({config.MAX_DOCUMENT_SEGMENTS}). Split it into smaller catalogs."
        )
    size = len(buf)
    for name, table in (("original", o), ("translation", t)):
        if table < _HEADER_WORDS * 4 or table + 8 * n > size:
            raise InvalidMoFile(f"This MO file is damaged: its {name} table lies outside the file.")
    if s and (h < _HEADER_WORDS * 4 or h + 4 * s > size):
        raise InvalidMoFile("This MO file is damaged: its hash table lies outside the file.")
    # System-dependent strings (<PRIu64> and friends) are expanded per platform by
    # libintl; they have no PO-level text this filter could round-trip, so refuse rather
    # than silently drop them from the rebuilt file.
    sysdep = minor >= 1 and size >= (_HEADER_WORDS + _SYSDEP_WORDS) * 4
    if sysdep and _u32(buf, 36, endian):
        raise InvalidMoFile(
            "This MO file has system-dependent strings (<inttypes.h> macros). "
            "Upload the PO file it was compiled from instead."
        )
    return endian, n, o, t


def _strings(buf: bytes, endian: str, n: int, table: int) -> list[bytes]:
    from app import config

    size = len(buf)
    per_string = 8 * config.MAX_SEGMENT_CHARS  # plural slots share one string
    out = []
    for i in range(n):
        length, off = struct.unpack_from(endian + "II", buf, table + 8 * i)
        # Room for the string AND its NUL terminator (the same bound CPython's gettext
        # and babel apply: a string ending exactly at end-of-file is damaged).
        if off + length >= size:
            raise InvalidMoFile("This MO file is damaged: a string lies outside the file.")
        if length > per_string:
            raise InputLimitError(
                f"This MO file has a string longer than {per_string} bytes "
                "(8 x MAX_SEGMENT_CHARS). Split it into smaller catalogs."
            )
        out.append(buf[off : off + length])
    return out


def _charset(header_msgstr: bytes) -> str:
    m = _CHARSET_RE.search(header_msgstr)
    if not m:
        return "utf-8"
    name = _codec_name(m.group(2).decode("ascii"))
    if name in ("utf-8", "ascii") or name in _DECLARED_OK:
        return name
    return "utf-8"  # "CHARSET" placeholder or an unknown label: msgfmt writes UTF-8


def read_entries(data: bytes) -> tuple[list[dict], str]:
    """The catalog as `po_filter` entry dicts in file (sorted) order, plus the codec the
    strings were decoded with. Raises InvalidMoFile / InputLimitError before any per-string
    work for a header that does not fit the file."""
    buf = bytes(data)
    endian, n, o, t = _header(buf)
    origs = _strings(buf, endian, n, o)
    trans = _strings(buf, endian, n, t)
    # Overlapping offsets could make a small file expand into gigabytes of decoded text.
    if sum(map(len, origs)) + sum(map(len, trans)) > 2 * len(buf):
        raise InputLimitError(
            "This MO file references more string bytes than it holds (overlapping "
            "strings). It is refused; recompile it with msgfmt."
        )
    codec = "utf-8"
    for raw_id, raw_str in zip(origs, trans, strict=True):
        if raw_id == b"":
            codec = _charset(raw_str)
            break
    entries = []
    try:
        for raw_id, raw_str in zip(origs, trans, strict=True):
            ctxt = None
            if b"\x04" in raw_id:
                raw_ctxt, raw_id = raw_id.split(b"\x04", 1)
                ctxt = raw_ctxt.decode(codec)
            ids = raw_id.decode(codec).split("\x00")
            strs = raw_str.decode(codec).split("\x00")
            if len(ids) > 2 or (len(ids) == 1 and len(strs) > 1):
                raise InvalidMoFile("This MO file is damaged: a message has stray NUL bytes.")
            entry = {
                "comments": [],
                "msgctxt": ctxt,
                "msgid": ids[0],
                "msgid_plural": ids[1] if len(ids) == 2 else None,
                "msgstr": None if len(ids) == 2 else strs[0],
                "has_kw": True,
                "msgid_ok": True,
            }
            if len(ids) == 2:
                for i, s in enumerate(strs):
                    entry[f"msgstr{i}"] = s
            entries.append(entry)
            check_segment_cap(len(entries))
    except UnicodeDecodeError as exc:
        raise InvalidMoFile(
            f"This MO file's strings are not valid {codec} (the charset its header declares)."
        ) from exc
    return entries, codec


def mo_to_po(data: bytes) -> bytes:
    """The PO text of an MO catalog (UTF-8; a declared legacy charset rewritten to UTF-8)."""
    entries, codec = read_entries(data)
    if codec != "utf-8":
        for e in entries:
            if po_filter._is_header(e):
                e["msgstr"] = _CHARSET_RE.sub(
                    lambda m: m.group(1) + b"UTF-8", (e["msgstr"] or "").encode()
                ).decode()
    return po_filter._render(entries)


def _slots(entry: dict) -> list[str]:
    idx = sorted(int(k[6:]) for k in entry if k.startswith("msgstr") and k[6:].isdigit())
    return [entry.get(f"msgstr{i}") or "" for i in range(max(idx) + 1 if idx else 2)]


def write_mo(entries: list[dict]) -> bytes:
    """A little-endian revision-0 MO file for `entries` (po_filter dicts).

    Matches msgfmt: comment-only blocks and untranslated messages are left out (the
    header always stays), originals are sorted bytewise, no hash table. An empty plural
    slot in a partly translated message falls back to msgid / msgid_plural, as babel
    and msgfmt runtime lookups do, so a lookup never returns an empty string."""
    pairs: dict[bytes, bytes] = {}
    for e in entries:
        if not e.get("has_kw", True):
            continue
        header = po_filter._is_header(e)
        if e["msgid_plural"] is not None:
            strs = _slots(e)
            if not any(strs):
                continue
            strs = [s or (e["msgid"] if i == 0 else e["msgid_plural"]) for i, s in enumerate(strs)]
            key = (e["msgid"] + "\x00" + e["msgid_plural"]).encode("utf-8", "surrogateescape")
            val = "\x00".join(strs).encode("utf-8", "surrogateescape")
        else:
            if not header and not e.get("msgstr"):
                continue
            key = e["msgid"].encode("utf-8", "surrogateescape")
            val = (e.get("msgstr") or "").encode("utf-8", "surrogateescape")
            if header:
                val = _CHARSET_RE.sub(lambda m: m.group(1) + b"UTF-8", val)
        if e["msgctxt"] is not None:
            key = e["msgctxt"].encode("utf-8", "surrogateescape") + b"\x04" + key
        pairs.setdefault(key, val)  # first wins on a duplicate, as msgfmt reports it
    keys = sorted(pairs)
    n = len(keys)
    o = _HEADER_WORDS * 4
    t = o + 8 * n
    pos = t + 8 * n
    otab, ttab, blob = [], [], []
    for k in keys:
        otab.append(struct.pack("<II", len(k), pos))
        blob.append(k + b"\x00")
        pos += len(k) + 1
    for k in keys:
        v = pairs[k]
        ttab.append(struct.pack("<II", len(v), pos))
        blob.append(v + b"\x00")
        pos += len(v) + 1
    head = struct.pack("<7I", LE_MAGIC, 0, n, o, t, 0, pos)
    return head + b"".join(otab) + b"".join(ttab) + b"".join(blob)


def _mo_from_po(po_bytes: bytes) -> bytes:
    return write_mo(po_filter._parse_entries(po_bytes))


def _with_language_field(po_bytes: bytes) -> bytes:
    """Give a header without a Language field an empty one, so the PO localized build
    stamps the target language and its Plural-Forms. An MO deliverable is read at run
    time, where a Plural-Forms rule that does not match the target picks wrong slots."""
    entries = po_filter._parse_entries(po_bytes)
    header = next((e for e in entries if po_filter._is_header(e)), None)
    if header is None or po_filter._LANGUAGE_FIELD_RE.search(header.get("msgstr") or ""):
        return po_bytes
    header["msgstr"] = (header.get("msgstr") or "").rstrip("\n") + "\nLanguage: \n"
    return po_filter._render(entries)


@register
class MoFilter(Filter):
    fmt = "mo"
    exts = ("gmo",)
    writes_targets = True  # B-bytes: a target equal to its source is a real write

    def parse(self, data: bytes, provider: str | None = None) -> ParseResult:
        result = po_filter.PoFilter().parse(mo_to_po(data))
        check_unit_texts(u.text for u in result.units)
        skeleton = {k: v for k, v in result.skeleton.items() if k != "source_encoding"}
        skeleton["format"] = "mo"
        return ParseResult(skeleton=skeleton, units=result.units)

    def build(
        self, original: bytes, paragraph_texts: dict, *, fallback_keys: set[str] | None = None
    ) -> bytes:
        po = mo_to_po(original)
        out = _mo_from_po(
            po_filter.PoFilter().build(po, paragraph_texts, fallback_keys=fallback_keys)
        )
        # Untouched catalog: hand back the original bytes (its hash table and byte order
        # included) rather than an equivalent recompile.
        return original if out == _mo_from_po(po) else out

    def build_localized(
        self,
        original: bytes,
        paragraph_texts: dict,
        target_locale: str | None = None,
        *,
        fallback_keys: set[str] | None = None,
    ) -> bytes:
        return _mo_from_po(
            build_po(original, paragraph_texts, target_locale, fallback_keys=fallback_keys)
        )


def build_po(
    original: bytes,
    paragraph_texts: dict,
    target_locale: str | None = None,
    *,
    fallback_keys: set[str] | None = None,
) -> bytes:
    """The translated catalog of an MO upload as PO text (the "export as PO" option):
    the PO localized build, so Language and Plural-Forms name the target."""
    po = mo_to_po(original)
    if target_locale:
        po = _with_language_field(po)
    return po_filter.PoFilter().build_localized(
        po, paragraph_texts, target_locale, fallback_keys=fallback_keys
    )
