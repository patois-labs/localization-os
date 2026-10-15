"""Zipped bilingual XLIFF packages: memoQ .mqxlz, WorldServer .xlz and .wsxz.

All three are plain zip archives whose translatable content is one or more XLIFF 1.2
files; everything else in the archive belongs to the vendor tool:

- memoQ .mqxlz: the memoQ XLIFF export saved compressed. memoQ always zips it when the
  skeleton/preview or the version history is included; the archive holds the
  `.mqxliff` (usually `document.mqxliff`) plus the skeleton data memoQ needs to export
  the translated document (docs.memoq.com, "MQXLIFF export settings").
- WorldServer .xlz: an XLIFF export kit (a `.xlf` with `iws:` segment metadata, plus
  optional skeleton/resources); .wsxz: a WorldServer package of WorldServer XLIFF or
  SDLXLIFF documents with TMX/TBX resources (docs.memoq.com "Import WorldServer
  package"; Okapi WSXZ Package Filter).

What Localization OS does: the archive is opened through `zip_guard` (bomb, forged-size
and encryption checks), every member whose name ends in an XLIFF extension is routed
through the bilingual CAT XLIFF engine (`cat_xliff`: locked / approved / finished /
confirmed segments protected, vendor status written on translated segments,
untranslated targets left empty, byte splice), and every other member (skeleton,
preview, TMX, TBX, manifest) is carried through with identical content. The rebuilt
archive keeps the member order, names, dates, compression method and comments. A build
with nothing to write returns the original archive bytes.

Unit keys: one document per package. A unit's key is its member's offset plus its
position in `cat_xliff.walk()` of that member (protected slots counted, never
renumbered), so parse and build agree on every key from the same bytes.

Documented in: docs/architecture/translation-pipeline.md
"""

from __future__ import annotations

import io
import zipfile

from app.filters.base import Filter, InputLimitError, ParseResult, check_segment_cap, register

XLIFF_MEMBER_EXTS = (".mqxliff", ".sdlxliff", ".sdlxlf", ".mxliff", ".xlf", ".xliff")
# One XLIFF member is parsed whole into memory; real kits are a few MB.
MAX_MEMBER_BYTES = 64 * 1024 * 1024
MAX_XLIFF_MEMBERS = 200
MAX_LISTED = 200

_LABELS = {"mqxlz": "memoQ MQXLZ package", "xlz": "WorldServer package"}


def _label(fmt: str) -> str:
    return _LABELS.get(fmt, "XLIFF package")


def open_zip(data: bytes, fmt: str) -> zipfile.ZipFile:
    from app.filters import zip_guard

    raw = bytes(data)
    label = _label(fmt)
    if not raw.startswith(b"PK"):
        raise InputLimitError(
            f"This {label} is not a zip archive. Export it again from the CAT tool."
        )
    zip_guard.check(raw, label)
    try:
        return zipfile.ZipFile(io.BytesIO(raw))
    except (zipfile.BadZipFile, ValueError, OSError) as exc:
        raise InputLimitError(
            f"This {label} is not a valid zip archive. Export it again from the CAT tool."
        ) from exc


def _is_xliff_member(info: zipfile.ZipInfo) -> bool:
    return not info.is_dir() and info.filename.lower().endswith(XLIFF_MEMBER_EXTS)


def _members(zf: zipfile.ZipFile, fmt: str) -> list[tuple[int, zipfile.ZipInfo]]:
    names = [i.filename for i in zf.infolist()]
    if len(names) != len(set(names)):
        raise InputLimitError(f"This {_label(fmt)} has duplicate file names inside the archive.")
    found = [(i, info) for i, info in enumerate(zf.infolist()) if _is_xliff_member(info)]
    if not found:
        raise InputLimitError(
            f"This {_label(fmt)} contains no XLIFF file (.mqxliff, .xlf, .xliff or "
            ".sdlxliff) to translate."
        )
    if len(found) > MAX_XLIFF_MEMBERS:
        raise InputLimitError(
            f"This {_label(fmt)} has {len(found)} XLIFF files; the limit is "
            f"{MAX_XLIFF_MEMBERS}. Ask the sender to split the package."
        )
    for _i, info in found:
        if info.file_size > MAX_MEMBER_BYTES:
            raise InputLimitError(
                f"The XLIFF file {info.filename!r} in this {_label(fmt)} is larger than "
                f"{MAX_MEMBER_BYTES // (1024 * 1024)} MB."
            )
    return found


def _slots(raw: bytes) -> list:
    from app.filters.cat_xliff import walk
    from app.filters.xliff_filter import _parse_xml

    return walk(_parse_xml(raw, capped=True))


def parse_package(data: bytes, fmt: str) -> ParseResult:
    from app.filters.cat_xliff import parse_units

    zf = open_zip(data, fmt)
    units: list = []
    protected: dict[str, int] = {}
    listing: list[dict] = []
    other_total = 0
    offset = 0
    with zf:
        found = _members(zf, fmt)
        xliff_idx = {i for i, _info in found}
        for i, info in enumerate(zf.infolist()):
            if i in xliff_idx or info.is_dir():
                continue
            other_total += 1
        for _i, info in found:
            raw = zf.read(info)
            try:
                parsed = parse_units(raw, fmt)
            except InputLimitError:
                raise
            except ValueError as exc:
                raise InputLimitError(
                    f"The XLIFF file {info.filename!r} in this {_label(fmt)} could not be "
                    f"read: {exc}"
                ) from exc
            pos = {slot.key: n for n, slot in enumerate(_slots(raw))}
            for u in parsed.units:
                u.p_index = offset + pos[u.p_index]
                units.append(u)
            check_segment_cap(len(units))
            for k, v in (parsed.skeleton.get("protected_units") or {}).items():
                protected[k] = protected.get(k, 0) + v
            if len(listing) < MAX_LISTED:
                listing.append(
                    {"path": info.filename, "offset": offset, "units": len(parsed.units)}
                )
            offset += len(pos)
    skeleton = {
        "format": fmt,
        "units": [{"p_index": u.p_index, "text": u.text, "style": ""} for u in units],
        "package": {"xliff_files": listing, "other_files": other_total},
    }
    if protected:
        skeleton["protected_units"] = protected
    return ParseResult(skeleton=skeleton, units=units)


def _copy_info(src: zipfile.ZipInfo) -> zipfile.ZipInfo:
    dst = zipfile.ZipInfo(src.filename, date_time=src.date_time)
    dst.compress_type = src.compress_type
    dst.external_attr = src.external_attr
    dst.create_system = src.create_system
    dst.comment = src.comment
    return dst


def build_package(
    original: bytes,
    paragraph_texts: dict,
    fmt: str,
    *,
    fallback_keys=None,
    target_locale: str | None = None,
) -> bytes:
    from app.filters.cat_xliff import build_units

    raw_pkg = bytes(original)
    zf = open_zip(raw_pkg, fmt)
    fallback = {str(k) for k in (fallback_keys or ())}
    parts: list[tuple[zipfile.ZipInfo, bytes]] = []
    changed = False
    with zf:
        found = {i for i, _info in _members(zf, fmt)}
        offset = 0
        for i, info in enumerate(zf.infolist()):
            body = zf.read(info)
            if i in found:
                slots = _slots(body)
                inner: dict = {}
                inner_fb: set = set()
                for n, slot in enumerate(slots):
                    gk = str(offset + n)
                    if gk in paragraph_texts:
                        inner[str(slot.key)] = paragraph_texts[gk]
                    if gk in fallback:
                        inner_fb.add(str(slot.key))
                offset += len(slots)
                if inner:
                    new = build_units(
                        body, inner, fallback_keys=inner_fb, target_locale=target_locale
                    )
                elif target_locale:
                    new = build_units(body, {}, target_locale=target_locale)
                else:
                    new = body
                if new != body:
                    changed = True
                    body = new
            parts.append((info, body))
        comment = zf.comment
    if not changed:
        return raw_pkg
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as oz:
        for info, body in parts:
            oz.writestr(_copy_info(info), body)
        if comment:
            oz.comment = comment
    return out.getvalue()


class _XliffPackage(Filter):
    writes_targets = True  # B-bytes: a target equal to its source is a real write

    def parse(self, data: bytes, provider: str | None = None) -> ParseResult:
        return parse_package(data, self.fmt)

    def build(self, original: bytes, paragraph_texts: dict, *, fallback_keys=None) -> bytes:
        return build_package(original, paragraph_texts, self.fmt, fallback_keys=fallback_keys)

    def build_localized(
        self,
        original: bytes,
        paragraph_texts: dict,
        target_locale: str | None = None,
        *,
        fallback_keys=None,
    ) -> bytes:
        """build() plus `target-language` on an inner <file> that has none (a vendor
        file's own target-language is never changed)."""
        return build_package(
            original,
            paragraph_texts,
            self.fmt,
            fallback_keys=fallback_keys,
            target_locale=target_locale,
        )


@register
class MqxlzFilter(_XliffPackage):
    fmt = "mqxlz"
    exts = ()


@register
class XlzFilter(_XliffPackage):
    fmt = "xlz"
    exts = ("wsxz",)
