"""Trados Studio packages: project packages (.sdlppx) in, return packages (.sdlrpx) out.

A Trados package is a zip of a "package project": one `.sdlproj` manifest at the root
(XML, root element `PackageProject`) plus the project folders, one per language
(`en-US/`, `de-DE/`, ...). The translatable work is the bilingual SDLXLIFF files in the
TARGET language folders; the source folder holds the original files, and the package may
also carry translation memories (`.sdltm`), termbases, file type settings and reports.

What Localization OS does with one:

- `inspect()` reads the zip through `zip_guard` (bomb and forged-size checks), parses
  the `.sdlproj` with the hardened XML parser (no DTD, no entities, no network) for the
  language pair(s), and sorts every member into target SDLXLIFFs (one document each)
  and skipped files (source files, other-language files, TMs, termbases, reports),
  which are listed, never silently dropped.
- Each target SDLXLIFF becomes one document whose stored original is a MEMBER BLOB: a
  small zip holding the original `.sdlproj` and that one SDLXLIFF, byte for byte, under
  their original paths, with a marker in the zip comment naming the member. The
  translation itself is the Batch 5 SDLXLIFF filter (`cat_xliff`), so locked, approved
  and confirmed segments are protected exactly as for a loose `.sdlxliff`.
- build() returns a RETURN PACKAGE for that document: the original `.sdlproj` with only
  `PackageType="ProjectPackage"` switched to `ReturnPackage`, plus the rebuilt SDLXLIFF
  under its original path. `services/sdl_package.build_return_package` merges every
  document of one package into one `.sdlrpx` the same way.

Why a return package carries only the manifest and the target files: RWS describes a
return package as "a ZIP file containing an XML manifest file and the translated project
files" that "does not contain translation memories"; Studio opens it by the project GUID
in the manifest and overwrites the project's bilingual files with the returned ones
(developers.rws.com, Project Automation API, "About Packages" / "Importing a Return
Package"). The source files, TMs and settings stay with the project manager's project.

Documented in: docs/architecture/translation-pipeline.md
"""

from __future__ import annotations

import io
import json
import re
import zipfile
from dataclasses import dataclass, field

from lxml import etree

from app.filters.base import Filter, InputLimitError, ParseResult, register

MARKER = b"LOCOS-SDLPKG/1 "
PACKAGE_EXTS = ("sdlppx", "sdlrpx")
# A .sdlproj is a manifest (settings, file list, task list); 16 MB is far above any
# real project and keeps the XML parse bounded before lxml builds a tree.
MAX_SDLPROJ_BYTES = 16 * 1024 * 1024
# One document per target file: a package with more than this many is split by the
# sender (Trados can create one package per language or per file batch).
MAX_TARGET_FILES = 500
# Listing cap for skipped members (the count is always exact).
MAX_LISTED = 200

_PKG_TYPE_RE = re.compile(rb"""(\bPackageType\s*=\s*)(["'])ProjectPackage\2""")


@dataclass
class PackageInfo:
    filename: str
    project_member: str
    project_index: int
    package_guid: str
    package_type: str
    project_name: str
    source_locale: str
    target_locales: list[str]
    targets: list[dict] = field(default_factory=list)  # {path, locale, index}
    skipped: list[dict] = field(default_factory=list)  # {path, reason}
    skipped_total: int = 0

    def as_dict(self) -> dict:
        return {
            "filename": self.filename,
            "project_name": self.project_name,
            "package_guid": self.package_guid,
            "package_type": self.package_type,
            "source_locale": self.source_locale,
            "target_locales": list(self.target_locales),
            "documents": [{"path": t["path"], "locale": t["locale"]} for t in self.targets],
            "skipped": list(self.skipped),
            "skipped_total": self.skipped_total,
        }


def _norm(name: str) -> str:
    return name.replace("\\", "/")


def _local(el) -> str:
    return etree.QName(el).localname if isinstance(el.tag, str) else ""


def open_zip(data: bytes, label: str = "Trados package") -> zipfile.ZipFile:
    """The package as a ZipFile, after the zip_guard bomb/forged-size checks."""
    from app.filters import zip_guard

    raw = bytes(data)
    zip_guard.check(raw, label)
    try:
        return zipfile.ZipFile(io.BytesIO(raw))
    except (zipfile.BadZipFile, ValueError, OSError) as exc:
        raise InputLimitError(
            f"This {label} is not a valid zip archive. Export the package again from Trados."
        ) from exc


def _parse_sdlproj(raw: bytes):
    from app.filters.xliff_filter import _parse_xml

    if len(raw) > MAX_SDLPROJ_BYTES:
        raise InputLimitError("This Trados package's .sdlproj file is too large to read.")
    try:
        return _parse_xml(raw)
    except ValueError as exc:
        raise InputLimitError(
            f"This Trados package's .sdlproj file could not be read: {exc}"
        ) from exc


def _locale_folder(path: str) -> str:
    parts = _norm(path).split("/")
    return parts[0] if len(parts) > 1 else ""


def _skip_reason(path: str, source: str, targets_lc: set) -> str:
    low = _norm(path).lower()
    folder = _locale_folder(path).lower()
    if low.endswith(".sdltm"):
        return "translation memory (stays in Trados)"
    if low.endswith((".sdltb", ".mdf", ".ldf")) or folder == "termbases":
        return "termbase (stays in Trados)"
    if low.endswith(".sdlxliff") or low.endswith(".sdlxlf"):
        if folder == source.lower():
            return "source-language file (nothing to translate)"
        if folder not in targets_lc:
            return "language is not a target of this package"
        return ""
    if folder == source.lower():
        return "source file (stays in Trados)"
    if folder in targets_lc:
        return "not a bilingual SDLXLIFF file"
    return "package support file (reports, settings)"


def inspect(data: bytes, filename: str = "") -> PackageInfo:
    """Read a .sdlppx/.sdlrpx: language pair(s), target SDLXLIFFs, skipped members.
    Raises InputLimitError (a plain, user-facing message) for anything unusable."""
    zf = open_zip(data)
    with zf:
        infos = zf.infolist()
        projs = [
            (i, info)
            for i, info in enumerate(infos)
            if "/" not in _norm(info.filename) and info.filename.lower().endswith(".sdlproj")
        ]
        if len(projs) != 1:
            raise InputLimitError(
                "This is not a Trados project package: it needs exactly one .sdlproj file "
                f"at the top level (found {len(projs)})."
            )
        p_index, p_info = projs[0]
        root = _parse_sdlproj(zf.read(p_info))
        if _local(root) not in ("PackageProject", "Project"):
            raise InputLimitError(
                "This Trados package's .sdlproj is not a Trados project file "
                f"(root element {_local(root)!r})."
            )
        pairs = []
        for el in root.iter():
            if _local(el) == "LanguageDirection":
                src = (el.get("SourceLanguageCode") or "").strip()
                tgt = (el.get("TargetLanguageCode") or "").strip()
                if src and tgt:
                    pairs.append((src, tgt))
        if not pairs:
            raise InputLimitError(
                "This Trados package's .sdlproj names no language direction "
                "(SourceLanguageCode / TargetLanguageCode)."
            )
        sources = {s.lower() for s, _t in pairs}
        if len(sources) != 1:
            raise InputLimitError(
                "This Trados package has more than one source language; a project here has one."
            )
        source = pairs[0][0]
        targets: list[str] = []
        for _s, t in pairs:
            if t.lower() not in {x.lower() for x in targets}:
                targets.append(t)
        by_lc = {t.lower(): t for t in targets}
        info = PackageInfo(
            filename=filename,
            project_member=p_info.filename,
            project_index=p_index,
            package_guid=(root.get("PackageGuid") or root.get("Guid") or "").strip()[:64],
            package_type=(root.get("PackageType") or "").strip()[:40],
            project_name=(root.get("Name") or p_info.filename.rsplit(".", 1)[0]).strip()[:200],
            source_locale=source,
            target_locales=targets,
        )
        for i, m in enumerate(infos):
            if i == p_index or m.is_dir():
                continue
            reason = _skip_reason(m.filename, source, set(by_lc))
            if not reason:
                info.targets.append(
                    {
                        "path": m.filename,
                        "locale": by_lc[_locale_folder(m.filename).lower()],
                        "index": i,
                    }
                )
                continue
            info.skipped_total += 1
            if len(info.skipped) < MAX_LISTED:
                info.skipped.append({"path": m.filename, "reason": reason})
    if not info.targets:
        raise InputLimitError(
            "This Trados package has no target-language SDLXLIFF files to translate "
            f"(target languages: {', '.join(targets)})."
        )
    if len(info.targets) > MAX_TARGET_FILES:
        raise InputLimitError(
            f"This Trados package has {len(info.targets)} target files; the limit is "
            f"{MAX_TARGET_FILES}. Ask the sender to split the package."
        )
    return info


def member_blob(data: bytes, info: PackageInfo, target: dict, zf=None) -> bytes:
    """The stored original for one target file's document (see module docstring).
    `zf`: the package already opened by open_zip (one zip_guard pass for a whole
    import instead of one per target file); opened here when omitted."""
    own = zf is None
    if own:
        zf = open_zip(data)
    out = io.BytesIO()
    try:
        _write_member_blob(zf, out, info, target)
    finally:
        if own:
            zf.close()
    return out.getvalue()


def _write_member_blob(zf, out, info: PackageInfo, target: dict) -> None:
    with zipfile.ZipFile(out, "w") as oz:
        infos = zf.infolist()
        for i in sorted((info.project_index, target["index"])):
            src = infos[i]
            oz.writestr(_copy_info(src), zf.read(src))
        meta = {
            "member": target["path"],
            "locale": target["locale"],
            "project": info.project_member,
            "order": [0, 1] if info.project_index < target["index"] else [1, 0],
            "package_guid": info.package_guid,
            "package": info.filename,
            "comment": zf.comment.decode("latin-1"),
        }
        oz.comment = MARKER + json.dumps(meta, separators=(",", ":")).encode("utf-8")


def _copy_info(src: zipfile.ZipInfo) -> zipfile.ZipInfo:
    dst = zipfile.ZipInfo(src.filename, date_time=src.date_time)
    dst.compress_type = src.compress_type
    dst.external_attr = src.external_attr
    dst.create_system = src.create_system
    dst.comment = src.comment
    return dst


def member_meta(data: bytes) -> dict | None:
    """The marker of a member blob, or None for anything else (a raw package)."""
    try:
        with zipfile.ZipFile(io.BytesIO(bytes(data))) as zf:
            comment = zf.comment
    except (zipfile.BadZipFile, ValueError, OSError):
        return None
    if not comment.startswith(MARKER):
        return None
    try:
        meta = json.loads(comment[len(MARKER) :].decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return None
    return meta if isinstance(meta, dict) and isinstance(meta.get("member"), str) else None


def flip_package_type(sdlproj: bytes) -> bytes:
    """`PackageType="ProjectPackage"` -> `"ReturnPackage"` on the root element only (the
    switch that distinguishes a return package; every other byte unchanged). A manifest
    without that attribute value is returned as it is."""
    end = sdlproj.find(b">", sdlproj.find(b"PackageProject") if b"PackageProject" in sdlproj else 0)
    head, tail = sdlproj[: end + 1], sdlproj[end + 1 :]
    return _PKG_TYPE_RE.sub(rb"\1\2ReturnPackage\2", head, count=1) + tail


def _open_member(data: bytes):
    """(zipfile, meta, project ZipInfo, member ZipInfo) for a member blob, or for a raw
    package that has exactly one target file (used as if it were a member blob)."""
    meta = member_meta(data)
    if meta is None:
        info = inspect(data)
        if len(info.targets) != 1:
            raise InputLimitError(
                f"This Trados package has {len(info.targets)} target files. Upload it to a "
                "project (one document per file) or create a project from it."
            )
        data = member_blob(data, info, info.targets[0])
        meta = member_meta(data)
    zf = open_zip(data)
    names = {i.filename: i for i in zf.infolist()}
    member, proj = names.get(meta.get("member")), names.get(meta.get("project"))
    low = (meta.get("member") or "").lower()
    if member is None or proj is None or not low.endswith((".sdlxliff", ".sdlxlf")):
        zf.close()
        raise InputLimitError("This stored Trados package document is incomplete.")
    return zf, meta, proj, member


def build_return(
    data: bytes,
    paragraph_texts: dict,
    target_locale: str | None = None,
    *,
    fallback_keys=None,
) -> bytes:
    from app.filters.cat_xliff import SdlxliffFilter

    zf, meta, proj, member = _open_member(data)
    with zf:
        built = SdlxliffFilter().build_localized(
            zf.read(member), paragraph_texts, target_locale, fallback_keys=fallback_keys
        )
        parts = [(proj, flip_package_type(zf.read(proj))), (member, built)]
        if meta.get("order") == [1, 0]:
            parts.reverse()
        return write_package(parts, meta.get("comment", ""))


def write_package(parts: list, comment: str = "") -> bytes:
    """A zip of (ZipInfo, bytes) in the given order; names, dates and compression kept."""
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as oz:
        for info, body in parts:
            oz.writestr(_copy_info(info), body)
        if comment:
            oz.comment = comment.encode("latin-1", "replace")[:65535]
    return out.getvalue()


def return_package_parts(data: bytes) -> tuple:
    """(project ZipInfo, project bytes, [(ZipInfo, bytes)...]) of a built return package."""
    zf = open_zip(data, "return package")
    with zf:
        proj = None
        rest = []
        for info in zf.infolist():
            if "/" not in _norm(info.filename) and info.filename.lower().endswith(".sdlproj"):
                proj = (info, zf.read(info))
            else:
                rest.append((info, zf.read(info)))
        return proj, rest, zf.comment.decode("latin-1")


@register
class SdlPackageFilter(Filter):
    """One target SDLXLIFF of a Trados package; exports as a return package."""

    fmt = "sdlrpx"
    exts = ("sdlppx",)
    passthrough_untouched = False  # B-bytes: the deliverable is a different file

    def parse(self, data: bytes, provider: str | None = None) -> ParseResult:
        from app.filters.cat_xliff import SDL_NS, parse_units

        zf, meta, _proj, member = _open_member(data)
        with zf:
            raw = zf.read(member)
        parsed = parse_units(raw, "sdlxliff", require_ns=SDL_NS)
        parsed.skeleton["format"] = self.fmt
        parsed.skeleton["sdl_package"] = {
            "member": meta.get("member"),
            "locale": meta.get("locale"),
            "package_guid": meta.get("package_guid", ""),
            "package": meta.get("package", ""),
        }
        return parsed

    def build(self, original: bytes, paragraph_texts: dict, *, fallback_keys=None) -> bytes:
        return build_return(original, paragraph_texts, fallback_keys=fallback_keys)

    def build_localized(
        self,
        original: bytes,
        paragraph_texts: dict,
        target_locale: str | None = None,
        *,
        fallback_keys=None,
    ) -> bytes:
        return build_return(original, paragraph_texts, target_locale, fallback_keys=fallback_keys)
