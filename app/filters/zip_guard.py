"""Zip-bomb guard for the OOXML document formats (security review 2026-09, #224).

DOCX and XLSX are zip packages. The upload cap (MAX_UPLOAD_BYTES) bounds the COMPRESSED
file only, and python-docx / openpyxl inflate every part they touch into memory, so a
0.5 MB DOCX whose image part is 500 MB of zeros drove one parse to over 1.1 GB. This
module checks the package's central directory BEFORE either library opens it and refuses
it with filters.base.InputLimitError when:

  * the sum of the members' uncompressed sizes exceeds ZIP_MAX_UNCOMPRESSED_BYTES,
  * there are more than ZIP_MAX_MEMBERS members, or
  * a member larger than ZIP_RATIO_MIN_BYTES inflates by more than ZIP_MAX_RATIO,
  * a member name leaves the package (absolute, `..`, a colon or drive letter, a control
    character, a component made only of dots or spaces; #795, #814.2), or
  * the same name is listed twice (a reader that keeps the last copy and one that keeps
    the first would see different parts; #814.3), counting names that differ only in
    ASCII case or slash direction as the same (Bilingua's shared zip reader does too).

Declared sizes are only an early rejection. Verify actual stored/deflated streams in
bounded chunks before the libraries can perform an unbounded read. ZipExtFile truncates
AFTER decompression and can accept a forged size with a CRC matching the truncated prefix.

Called at every place a DOCX/XLSX blob is opened: the filters' parse and build (upload,
re-ingest, export) and the gold-rules XLSX loader. A blob that is not a zip at all is left
to the library, which raises its own "not a valid file" error as before.
"""

from __future__ import annotations

import io
import struct
import zipfile
import zlib

from app import config
from app.filters.base import InputLimitError
from app.filters.xml_guard import _RUN as _TAG_RUN
from app.filters.xml_guard import MAX_TAG_ATTRIBUTES, tag_budget_exceeded

_BOM_AND_SPACE = b"\xef\xbb\xbf \t\r\n"  # what may precede the '<' of an XML part
_UTF16_BOMS = (b"\xff\xfe", b"\xfe\xff")


def _mb(n: int) -> str:
    return f"{n / (1024 * 1024):.0f} MB"


def _verify_member(data: bytes, info: zipfile.ZipInfo, label: str) -> None:
    """Measure raw compressed bytes independently of ZipExtFile's declared-size truncation."""

    def invalid():
        return InputLimitError(f"This {label} has invalid ZIP sizes or data. It was not processed.")

    offset = info.header_offset
    if offset < 0 or data[offset : offset + 4] != b"PK\x03\x04":
        raise invalid()
    try:
        name_len, extra_len = struct.unpack_from("<HH", data, offset + 26)
        flags, method = struct.unpack_from("<HH", data, offset + 6)
    except struct.error as exc:
        raise invalid() from exc
    start = offset + 30 + name_len + extra_len
    end = start + info.compress_size
    if end > len(data) or flags & 1 or method != info.compress_type:
        raise invalid()
    if method not in (zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED):
        raise InputLimitError(
            f"This {label} uses unsupported ZIP compression. Save it again in Office."
        )
    decoder = zlib.decompressobj(-15) if method == zipfile.ZIP_DEFLATED else None
    total = crc = 0
    chunk_size = 64 * 1024
    # #792 gate: an XML part (it starts with '<', after a BOM) is kept for the per-tag
    # attribute budget, checked here because every package format calls check() before
    # its XML library (lxml, python-docx, openpyxl) opens a part.
    xml_part: list | None = [] if info.file_size >= _TAG_RUN else None
    first = True

    def count(piece):
        nonlocal total, crc, xml_part, first
        total += len(piece)
        if total > info.file_size:
            raise invalid()
        crc = zlib.crc32(piece, crc)
        if xml_part is not None and piece:
            if first:
                first = False
                head = piece[:4].lstrip(_BOM_AND_SPACE)
                if not (head.startswith(b"<") or piece[:2] in _UTF16_BOMS):
                    xml_part = None
                    return
            xml_part.append(piece)

    try:
        for pos in range(start, end, chunk_size):
            compressed = data[pos : min(pos + chunk_size, end)]
            if decoder is None:
                count(compressed)
                continue
            while compressed:
                count(decoder.decompress(compressed, chunk_size))
                compressed = decoder.unconsumed_tail
                if decoder.unused_data:
                    raise invalid()
        if decoder is not None and not decoder.eof:
            raise invalid()
    except zlib.error as exc:
        raise invalid() from exc
    if total != info.file_size or crc != info.CRC:
        raise invalid()
    if xml_part and tag_budget_exceeded(b"".join(xml_part)):
        raise InputLimitError(
            f"This {label} has a part ({info.filename}) with a start tag of more than "
            f"{MAX_TAG_ATTRIBUTES} attributes or namespace declarations; refused (it would "
            "take far too long to read)."
        )


def unsafe_name(raw: str) -> bool:
    """True when an archive member name could write outside its target folder, or is not
    a plain relative path (Bilingua's bilingua_zip::safe_name: same rules)."""
    if not isinstance(raw, str) or not raw:
        return True
    if any(_is_control(c) for c in raw):
        return True
    name = raw.replace("\\", "/")
    if name.startswith("/") or ":" in name:
        return True
    parts = [c for c in name.split("/") if c not in ("", ".")]
    if not parts or any(c == ".." for c in parts):
        return True
    return any(not c.rstrip(". ") for c in parts)


def _is_control(c: str) -> bool:
    # Rust's char::is_control (Unicode Cc): C0, DEL and the C1 range, as Bilingua
    o = ord(c)
    return o < 0x20 or 0x7F <= o <= 0x9F


_ASCII_LOWER = str.maketrans("ABCDEFGHIJKLMNOPQRSTUVWXYZ", "abcdefghijklmnopqrstuvwxyz")


def name_key(raw: str) -> str:
    """The name two members collide on: `\\` read as `/`, `.` and empty components
    dropped, ASCII case folded (Windows and the Office readers; Bilingua's read_zip)."""
    name = raw.replace("\\", "/")
    key = "/".join(c for c in name.split("/") if c not in ("", "."))
    if name.endswith("/"):
        key += "/"
    return key.translate(_ASCII_LOWER)


def check_names(members, label: str, *, duplicates: bool = True, links: bool = True) -> None:
    """Refuse a package whose member names leave it or repeat (#795, #814.2, #814.3)."""
    seen: set[str] = set()
    for info in members:
        raw = info.filename
        # Bilingua's bilingua_zip refuses both in every package format (parity, #814 follow-up).
        if info.flag_bits & 0x1:
            raise InputLimitError(
                f"This {label} contains an encrypted file inside the archive. It was not processed."
            )
        if links and info.create_system == 3 and (info.external_attr >> 16) & 0o170000 == 0o120000:
            raise InputLimitError(
                f"This {label} contains a symbolic link inside the archive. It was not processed."
            )
        if unsafe_name(raw):
            shown = "".join("?" if _is_control(c) else c for c in raw)[:120]
            raise InputLimitError(
                f"This {label} contains an unsafe path inside the archive ({shown!r}): "
                "absolute, drive-qualified, with a colon, or climbing out with '..'. "
                "It was not processed."
            )
        if duplicates:
            key = name_key(raw)
            if key in seen:
                raise InputLimitError(
                    f"This {label} has duplicate file names inside the archive (the same file "
                    "twice, or names that differ only in case). It was not processed."
                )
            seen.add(key)


def refuse_doctype(
    data: bytes, label: str = "file", skip_prefixes: tuple = ("customXml/",)
) -> None:
    """Refuse an Office package holding a DTD in any XML part (#814.4): PPTX and XLSX
    always did; DOCX read the DOCTYPE through python-docx. One rule for every format.
    customXml/ is skipped: those parts are only scanned for external links (a warning),
    never parsed into units, and a DOCX carrying one with a DTD is still read."""
    try:
        zf = zipfile.ZipFile(io.BytesIO(data))
    except (zipfile.BadZipFile, ValueError, OSError):
        return
    with zf:
        for info in zf.infolist():
            if not info.filename.lower().endswith((".xml", ".rels")):
                continue
            if info.filename.startswith(skip_prefixes):
                continue
            raw = zf.read(info)
            if b"<!DOCTYPE" in raw[:4096] or b"<!ENTITY" in raw:
                raise InputLimitError(f"This {label} contains an unsupported XML document type.")


def check(data: bytes, label: str = "file", *, duplicates: bool = True, links: bool = True) -> None:
    """Refuse an OOXML package that would inflate past the configured limits, or whose
    member names are unsafe or repeated, or that holds an encrypted member or a symbolic
    link. `duplicates=False`: the caller handles repeats. `links=False`: the caller lists
    symbolic links as skipped and never follows them (the batch ZIP)."""
    try:
        zf = zipfile.ZipFile(io.BytesIO(data))
    except (zipfile.BadZipFile, ValueError, OSError):
        return  # not a zip: the format library reports its own parse error
    with zf:
        members = zf.infolist()
        check_names(members, label, duplicates=duplicates, links=links)
        if len(members) > config.ZIP_MAX_MEMBERS:
            raise InputLimitError(
                f"This {label} has {len(members)} internal parts; the limit is "
                f"{config.ZIP_MAX_MEMBERS}. It was not processed."
            )
        total = 0
        for info in members:
            size = max(int(info.file_size), 0)
            total += size
            if size > config.ZIP_RATIO_MIN_BYTES:
                ratio = size / max(int(info.compress_size), 1)
                if ratio > config.ZIP_MAX_RATIO:
                    raise InputLimitError(
                        f"This {label} contains a part that expands {ratio:.0f} times "
                        f"(limit {config.ZIP_MAX_RATIO} times), which is typical of a "
                        "compression bomb. It was not processed."
                    )
        if total > config.ZIP_MAX_UNCOMPRESSED_BYTES:
            raise InputLimitError(
                f"This {label} expands to {_mb(total)} when unpacked; the limit is "
                f"{_mb(config.ZIP_MAX_UNCOMPRESSED_BYTES)}. It was not processed."
            )
        for info in members:
            _verify_member(data, info, label)


# Office extensions that promise a package WITHOUT macros. A macro-enabled package
# ([MS-OFFMACRO2]: main part type "...macroEnabled...", or a VBA project part) named
# with one of these is refused: a macro document disguised under a non-macro name must
# not pass through us and come back out under that same name (gate decision, Batch 4).
NON_MACRO_EXTS = frozenset({"docx", "dotx", "xlsx", "xltx", "pptx", "potx", "ppsx"})
_VBA_PART_TYPE = "application/vnd.ms-office.vbaproject"


class MacroExtensionMismatch(ValueError):
    """A macro-enabled Office package under a non-macro extension. The message is plain."""


def refuse_disguised_macro(data: bytes, ext: str) -> None:
    """Raise MacroExtensionMismatch when `data` is a macro-enabled OOXML package and
    `ext` is a non-macro extension (.docx, .xlsx, .pptx and their template / show forms).
    Call after the filter's parse, so zip_guard.check has already bounded the package."""
    ext = (ext or "").lower().lstrip(".")
    if ext not in NON_MACRO_EXTS:
        return
    from lxml import etree

    macro_ext = {"x": "m"}  # docx -> docm, xlsx -> xlsm, ppsx -> ppsm ...
    hint = ext[:-1] + macro_ext.get(ext[-1], ext[-1])
    msg = (
        f"This .{ext} file contains macros (it is a macro-enabled Office file). "
        f"Macro files are only accepted under their own extension: save it as .{hint} "
        "or remove the macros."
    )
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            names = zf.namelist()
            raw = zf.read("[Content_Types].xml")
        parser = etree.XMLParser(
            resolve_entities=False, no_network=True, load_dtd=False, huge_tree=False
        )
        root = etree.fromstring(raw, parser)
    except Exception as exc:  # unreadable package map: cannot prove it is macro-free
        raise MacroExtensionMismatch(
            f"This .{ext} file's package map could not be read. Save it again in Office."
        ) from exc
    if any(n.lower().rsplit("/", 1)[-1] == "vbaproject.bin" for n in names):
        raise MacroExtensionMismatch(msg)
    for el in root.iter():
        ct = (el.get("ContentType") or "").lower()
        if "macroenabled" in ct or ct == _VBA_PART_TYPE:
            raise MacroExtensionMismatch(msg)
