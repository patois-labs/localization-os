"""Zip-bomb guard for the OOXML document formats (security review 2026-09, #224).

DOCX and XLSX are zip packages. The upload cap (MAX_UPLOAD_BYTES) bounds the COMPRESSED
file only, and python-docx / openpyxl inflate every part they touch into memory, so a
0.5 MB DOCX whose image part is 500 MB of zeros drove one parse to over 1.1 GB. This
module checks the package's central directory BEFORE either library opens it and refuses
it with filters.base.InputLimitError when:

  * the sum of the members' uncompressed sizes exceeds ZIP_MAX_UNCOMPRESSED_BYTES,
  * there are more than ZIP_MAX_MEMBERS members, or
  * a member larger than ZIP_RATIO_MIN_BYTES inflates by more than ZIP_MAX_RATIO.

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

    def count(piece):
        nonlocal total, crc
        total += len(piece)
        if total > info.file_size:
            raise invalid()
        crc = zlib.crc32(piece, crc)

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


def check(data: bytes, label: str = "file") -> None:
    """Refuse an OOXML package that would inflate past the configured limits."""
    try:
        zf = zipfile.ZipFile(io.BytesIO(data))
    except (zipfile.BadZipFile, ValueError, OSError):
        return  # not a zip: the format library reports its own parse error
    with zf:
        members = zf.infolist()
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
