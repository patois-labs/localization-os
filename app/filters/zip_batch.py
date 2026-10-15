"""ZIP batch upload (Batch 13): one uploaded `.zip` becomes one document per supported member.

A ZIP batch is NOT a filter: it is never registered, so `.zip` stays out of
`supported_exts()` (the Jira attachment allowlist and `/api/formats` keep their meaning, and
a plain `.zip` attached to a Jira issue is still "unsupported, skipped"). The upload doors
(web, /api, CLI) recognise the `.zip` extension BEFORE the normal ingest and hand the bytes
to `expand()`; every supported member then goes through the ordinary `ingest_document`
chokepoint, exactly as if it had been uploaded on its own.

Untrusted input, so:
  * `zip_guard.check` first (member count, total uncompressed size, per-member expansion
    ratio, forged sizes verified against the real deflate stream) before any member is read;
  * a member path that is absolute, carries a drive letter, a `..` component or a control
    character refuses the WHOLE archive (a hostile archive is not partly trusted);
  * nested `.zip` members are never opened (listed as skipped), and neither are symbolic
    links, OS junk (`__MACOSX/`, `.DS_Store`, `Thumbs.db`, `desktop.ini`), case-insensitive
    duplicate paths, members over MAX_UPLOAD_BYTES, or unsupported formats; every one of
    those is LISTED in the result with a reason, never silently dropped;
  * more than ZIP_BATCH_MAX_DOCUMENTS supported members refuses the whole archive.

The member's folder path is kept: the document name is the path with `/` written as `__`
(a document name may not carry a path, issue #142), and the real relative path is stamped
on the document skeleton as `zip_batch = {id, archive, path}` so the translated download
(`delivery.export_project(batch=...)`) rebuilds the same folder structure per target
language.

Documented in: docs/architecture/translation-pipeline.md
"""

from __future__ import annotations

import io
import re
import uuid
import zipfile
from dataclasses import dataclass, field

from app import config
from app.filters import zip_guard
from app.filters.base import InputLimitError, get_filter

BATCH_EXTS = ("zip",)
NAME_SEPARATOR = "__"

_JUNK_NAMES = {".ds_store", "thumbs.db", "desktop.ini"}
_DRIVE_RE = re.compile(r"^[A-Za-z]:")
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")
_S_IFMT = 0o170000
_S_IFLNK = 0o120000


class NotAZipArchive(ValueError):
    """The upload is named `.zip` but is not a readable ZIP archive."""


@dataclass
class ZipMember:
    path: str  # normalised relative path inside the archive ("docs/guide/intro.md")
    name: str  # the document name ("docs__guide__intro.md")
    data: bytes


@dataclass
class ZipBatch:
    batch_id: str
    archive: str
    members: list[ZipMember] = field(default_factory=list)
    skipped: list[dict] = field(default_factory=list)  # {path, reason}


def is_batch_upload(filename: str) -> bool:
    """True for an upload whose extension is a ZIP batch (`.zip`)."""
    name = (filename or "").rsplit("/", 1)[-1].rsplit("\\", 1)[-1]
    return "." in name and name.rsplit(".", 1)[-1].lower() in BATCH_EXTS


def safe_member_path(raw: str) -> str | None:
    """The normalised relative path of an archive member, or None when it is unsafe
    (absolute, drive-qualified, `..`, control characters, empty). Used at upload AND
    again at export, so a stored path can never write outside its locale folder."""
    if not isinstance(raw, str) or not raw or _CONTROL_RE.search(raw):
        return None
    p = raw.replace("\\", "/")
    if p.startswith("/") or _DRIVE_RE.match(p):
        return None
    parts = [c for c in p.split("/") if c not in ("", ".")]
    if not parts or any(c == ".." for c in parts):
        return None
    if any(c != c.strip() or c.endswith(".") for c in parts):
        # Windows silently drops trailing dots/spaces, which would let two members
        # collide on extraction; refuse rather than guess.
        return None
    return "/".join(parts)


def document_name(path: str) -> str:
    """The document name for a member path: folders kept, `/` written as `__`."""
    return NAME_SEPARATOR.join(path.split("/"))


def _ext(path: str) -> str:
    leaf = path.rsplit("/", 1)[-1]
    return leaf.rsplit(".", 1)[-1].lower() if "." in leaf else ""


def expand(data: bytes, archive: str = "upload.zip") -> ZipBatch:
    """Open an uploaded ZIP batch and return its supported members plus the skipped list.

    Raises NotAZipArchive for bytes that are not a ZIP, InputLimitError for an archive over
    a safety limit or carrying an unsafe path (nothing is created in either case)."""
    try:
        zf = zipfile.ZipFile(io.BytesIO(data))
    except (zipfile.BadZipFile, ValueError, OSError) as exc:
        raise NotAZipArchive("This .zip file is not a readable ZIP archive.") from exc
    # Bomb, member-count and forged-size checks over the central directory and the real
    # compressed streams, BEFORE any member is read.
    zip_guard.check(data, label="ZIP archive")
    batch = ZipBatch(batch_id=uuid.uuid4().hex[:12], archive=archive)
    seen: set[str] = set()
    with zf:
        for info in zf.infolist():
            raw = info.filename
            if raw.endswith("/") or info.is_dir():
                continue  # a folder entry carries no content
            path = safe_member_path(raw)
            if path is None:
                raise InputLimitError(
                    "This ZIP archive contains an unsafe path "
                    f"({_CONTROL_RE.sub('?', raw)[:120]!r}): absolute, drive-qualified or "
                    "climbing out with '..'. It was not processed."
                )
            leaf = path.rsplit("/", 1)[-1]
            ext = _ext(path)
            mode = (info.external_attr >> 16) & _S_IFMT
            reason = None
            if mode == _S_IFLNK:
                reason = "symbolic link (not followed)"
            elif path.split("/", 1)[0] == "__MACOSX" or leaf.lower() in _JUNK_NAMES:
                reason = "system file (not translatable)"
            elif path.casefold() in seen:
                reason = "duplicate path (another member already uses this name)"
            elif ext in BATCH_EXTS:
                reason = "nested ZIP archive (not opened; upload it separately)"
            elif not ext or get_filter(ext) is None:
                reason = (
                    f"unsupported format (.{ext})" if ext else "unsupported format (no extension)"
                )
            elif info.file_size > config.MAX_UPLOAD_BYTES:
                reason = f"larger than the upload limit ({config.MAX_UPLOAD_BYTES} bytes)"
            seen.add(path.casefold())
            if reason:
                batch.skipped.append({"path": path, "reason": reason})
                continue
            if len(batch.members) >= config.ZIP_BATCH_MAX_DOCUMENTS:
                raise InputLimitError(
                    "This ZIP archive holds more than "
                    f"{config.ZIP_BATCH_MAX_DOCUMENTS} supported files "
                    "(ZIP_BATCH_MAX_DOCUMENTS). Split it into smaller archives. "
                    "It was not processed."
                )
            batch.members.append(ZipMember(path=path, name=document_name(path), data=zf.read(info)))
    return batch


def stamp(batch: ZipBatch, member: ZipMember) -> dict:
    """The skeleton extra recorded on a document created from a ZIP member."""
    return {"zip_batch": {"id": batch.batch_id, "archive": batch.archive, "path": member.path}}
