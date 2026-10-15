"""Apple Xcode Localization Catalog (.xcloc) filter (file-format Batch 16).

An .xcloc is a folder bundle Xcode writes with Product > Export Localizations or
`xcodebuild -exportLocalizations` (https://developer.apple.com/documentation/xcode/exporting-localizations):

    fr.xcloc/
      contents.json           developmentRegion, targetLocale, toolInfo, version
      Localized Contents/
        fr.xliff              XLIFF 1.2 with every localizable string
        ...                   localizable non-string assets (left alone)
      Source Contents/        context for translators (left alone)
      Notes/                  screenshots and notes for translators (left alone)

A browser cannot upload a folder, so the bundle arrives ZIPPED under the .xcloc
extension (Finder: Compress, then rename `fr.xcloc.zip` to `fr.xcloc`). The bundle may sit
at the archive root or inside one top folder; a macOS `__MACOSX/` shadow tree is ignored.

parse(): every `Localized Contents/*.xliff` directly in the bundle (archive order) goes
through the XLIFF filter unchanged; unit p_index values are offset by the number of
<source> elements in the earlier XLIFF files, so parse and build walk the same units.
build(): only the XLIFF files that receive a translation are rewritten (through the XLIFF
filter); every other member keeps its bytes, name, timestamp and compression method, and a
build with no translations returns the original archive byte for byte.
build_localized(): also stamps the target language: XLIFF `target-language` (through the
XLIFF filter) and the `targetLocale` value in contents.json (a text splice, so the rest of
the JSON keeps its exact layout).

Untrusted input: the archive goes through zip_guard (member count, total size, ratio,
real stream sizes, no encryption) before any member is read; XLIFF keeps the XLIFF filter's
hardened parser and markup budget; contents.json is capped and must be a JSON object.

Documented in: docs/architecture/translation-pipeline.md
"""

from __future__ import annotations

import io
import json
import re
import zipfile

from app.filters import zip_guard
from app.filters.base import (
    Filter,
    InputLimitError,
    ParseResult,
    TextUnit,
    check_segment_cap,
    register,
)

_LABEL = "Xcode localization catalog (.xcloc)"
_CONTENTS = "contents.json"
_LOCALIZED = "Localized Contents/"
# contents.json is a few hundred bytes in every Xcode export; 1 MB is far above any real one.
_MAX_CONTENTS_BYTES = 1024 * 1024
_TARGET_LOCALE_RE = re.compile(r'("targetLocale"\s*:\s*")((?:[^"\\]|\\.)*)(")')


def _open(data: bytes) -> zipfile.ZipFile:
    raw = bytes(data)
    try:
        zf = zipfile.ZipFile(io.BytesIO(raw))
    except (zipfile.BadZipFile, ValueError, OSError) as exc:
        raise ValueError(
            "This .xcloc file is not a zipped Xcode localization catalog. Compress the "
            ".xcloc folder (Finder: Compress), rename the .zip to .xcloc and upload that."
        ) from exc
    zip_guard.check(raw, _LABEL)
    return zf


def _layout(zf: zipfile.ZipFile) -> tuple[str, list[str]]:
    """(bundle prefix, XLIFF member names in archive order). Refuses an archive that is
    not exactly one catalog."""
    names = [i.filename for i in zf.infolist()]
    if len(set(names)) != len(names):
        raise ValueError("This .xcloc archive lists the same file twice. It was not processed.")
    live = [n for n in names if not n.startswith("__MACOSX/")]
    roots = []
    for n in live:
        if n == _CONTENTS or n.endswith("/" + _CONTENTS):
            prefix = n[: -len(_CONTENTS)]
            loc = prefix + _LOCALIZED
            xliffs = [
                m
                for m in live
                if m.startswith(loc) and "/" not in m[len(loc) :] and m.lower().endswith(".xliff")
            ]
            if xliffs:
                roots.append((prefix, xliffs))
    if not roots:
        raise ValueError(
            "This .xcloc archive has no contents.json with a 'Localized Contents/<language>"
            ".xliff' beside it, so it is not an Xcode localization catalog."
        )
    if len(roots) > 1:
        raise ValueError(
            f"This .xcloc archive holds {len(roots)} localization catalogs. Upload one "
            ".xcloc per file."
        )
    return roots[0]


def _read_contents(zf: zipfile.ZipFile, prefix: str) -> tuple[bytes, dict]:
    info = zf.getinfo(prefix + _CONTENTS)
    if info.file_size > _MAX_CONTENTS_BYTES:
        raise InputLimitError(
            f"This .xcloc has a contents.json over {_MAX_CONTENTS_BYTES // 1024} KB. "
            "It was not processed."
        )
    raw = zf.read(info)
    try:
        meta = json.loads(raw.decode("utf-8-sig"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise ValueError("This .xcloc has a contents.json that is not valid JSON.") from exc
    if not isinstance(meta, dict):
        raise ValueError("This .xcloc has a contents.json that is not a JSON object.")
    return raw, meta


def _source_count(raw: bytes) -> int:
    from app.filters.xliff_filter import _parse_xml, _source_elements

    return len(_source_elements(_parse_xml(raw)))


def _mark_translated(raw: bytes, written: set[int]) -> bytes:
    """Xcode exports untranslated strings as `<target state="new">` (a copy of the source)
    and imports `state` as the string's review status, so a target we wrote that carries a
    `state` attribute becomes `translated`. A target without one is left as Xcode wrote it."""
    from lxml import etree

    from app.filters.xliff_filter import _local, _parse_xml, _source_elements

    root = _parse_xml(raw)
    changed = False
    for k, src in enumerate(_source_elements(root)):
        if k not in written:
            continue
        tgt = next((c for c in src.getparent() if _local(c) == "target"), None)
        if tgt is not None and tgt.get("state") not in (None, "translated"):
            tgt.set("state", "translated")
            changed = True
    if not changed:
        return raw
    return etree.tostring(root, xml_declaration=True, encoding="UTF-8")


def _xliff():
    from app.filters.base import get_filter

    return get_filter("xliff")


def _set_target_locale(raw: bytes, tag: str) -> bytes:
    """Replace the targetLocale string value in contents.json, leaving every other byte."""
    text = raw.decode("utf-8")
    new, n = _TARGET_LOCALE_RE.subn(
        lambda m: m.group(1) + json.dumps(tag)[1:-1] + m.group(3), text, count=1
    )
    return new.encode("utf-8") if n else raw


def _copy_info(info: zipfile.ZipInfo) -> zipfile.ZipInfo:
    out = zipfile.ZipInfo(info.filename, date_time=info.date_time)
    out.compress_type = info.compress_type
    out.comment = info.comment
    out.create_system = info.create_system
    out.external_attr = info.external_attr
    out.internal_attr = info.internal_attr
    return out


@register
class XclocFilter(Filter):
    fmt = "xcloc"
    exts = ("xcloc",)
    writes_targets = True  # B-bytes: a target equal to its source is a real write

    def parse(self, data: bytes, provider: str | None = None) -> ParseResult:
        with _open(data) as zf:
            prefix, xliffs = _layout(zf)
            _raw, meta = _read_contents(zf, prefix)
            units: list[TextUnit] = []
            offset = 0
            for name in xliffs:
                raw = zf.read(name)
                for u in _xliff().parse(raw).units:
                    units.append(
                        TextUnit(p_index=offset + u.p_index, text=u.text, inline_map=u.inline_map)
                    )
                    check_segment_cap(len(units))
                offset += _source_count(raw)
        skeleton = {
            "format": "xcloc",
            "bundle": prefix,
            "xliff_files": xliffs,
            "development_region": str(meta.get("developmentRegion") or ""),
            "target_locale": str(meta.get("targetLocale") or ""),
            "units": [{"p_index": u.p_index, "text": u.text, "style": ""} for u in units],
        }
        return ParseResult(skeleton=skeleton, units=units)

    def build(self, original: bytes, paragraph_texts: dict) -> bytes:
        return self._build(original, paragraph_texts, None)

    def build_localized(
        self, original: bytes, paragraph_texts: dict, target_locale: str | None = None
    ) -> bytes:
        return self._build(original, paragraph_texts, target_locale)

    def _build(self, original: bytes, paragraph_texts: dict, target_locale: str | None) -> bytes:
        from app.filters import a11y

        tag = a11y.target_tag(target_locale) if target_locale else ""
        keyed = {}
        for k, v in (paragraph_texts or {}).items():
            try:
                keyed[int(k)] = v
            except (TypeError, ValueError):
                continue
        replaced: dict[str, bytes] = {}
        with _open(original) as zf:
            prefix, xliffs = _layout(zf)
            offset = 0
            for name in xliffs:
                raw = zf.read(name)
                count = _source_count(raw)
                sub = {str(k - offset): v for k, v in keyed.items() if offset <= k < offset + count}
                offset += count
                if tag:
                    new = _xliff().build_localized(raw, sub, target_locale)
                elif sub:
                    new = _xliff().build(raw, sub)
                else:
                    continue
                if sub:
                    new = _mark_translated(new, {int(k) for k in sub})
                if new != raw:
                    replaced[name] = new
            if tag:
                contents_name = prefix + _CONTENTS
                raw, meta = _read_contents(zf, prefix)
                if isinstance(meta.get("targetLocale"), str) and meta["targetLocale"] != tag:
                    new = _set_target_locale(raw, tag)
                    if new != raw:
                        replaced[contents_name] = new
            if not replaced:
                return bytes(original)
            buf = io.BytesIO()
            with zipfile.ZipFile(buf, "w") as out:
                out.comment = zf.comment
                for info in zf.infolist():
                    body = replaced.get(info.filename)
                    if body is None:
                        body = zf.read(info)
                    out.writestr(_copy_info(info), body)
        return buf.getvalue()
