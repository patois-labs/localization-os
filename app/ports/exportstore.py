"""ExportStore port -- the staged full-dump export-artifact seam (Batch 13, Train A).

A SIBLING of BlobStore (app/ports/blobstore.py), NOT a widening of it. The full-dump
export (SPEC-data-export) stages a per-run ZIP that must be:

  * STREAMED to storage a chunk at a time (never buffered whole -- LAW6 artifact-wise),
  * RANGE-seekable so a completed artifact's download is resumable (206 Partial Content),
  * optionally delivered through a signed URL when an object store backs it.

BlobStore cannot do this: it is a bytes-in/bytes-out Protocol whose disk adapter stores
blobs Fernet-encrypted-at-rest (encrypt_uploads), and a Fernet blob is NOT seekable, so
``open_range`` is unimplementable there. BlobStore is also ``runtime_checkable`` and pinned
by a frozen import-contract fixture + put/get/delete-only test doubles, which widening would
break (SPEC-data-export §2.3/§8.3, DX-204/DX-205). So export staging is THIS separate port,
with its own Protocol, adapters, registration, and verify suite -- ``BlobStore`` and its pins
are left untouched.

THE STORAGE CLASS IS FIXED AT REGISTRATION (DX-205). There is no ``purpose`` switch: call
sites speak KEYS through ``exportstore.get()``, and the concrete class (disk vs S3) is chosen
once in ``configure_at_boot``, exactly like ``blobstore``. Keys are FLAT (``_safe_key`` rejects
any path separator / parent-dir escape), so there is no ``exports/`` sub-namespace -- the key
form is ``dataexport_<run_uuid>.zip``.

TRAIN SPLIT (DX-305): Train A ships the Protocol + ``DiskExportStore`` implementing ALL FOUR
methods (``presign_get`` returns ``None`` -> serve locally), so A passes its own conformance
suite. Train C adds ONLY ``app/services/s3_exportstore.py::S3ExportStore`` (a real
``presign_get`` + multipart ``put_stream`` + ``Range`` ``open_range``), self-registering from
its own ``configure_at_boot`` -- it edits NO file here.

PORT CONVENTIONS (carried verbatim from blobstore.py's rules, DX-307):
  1. ``ports.register_default("exportstore", DiskExportStore())`` is this module's LAST
     statement (the frozen-file rule: a default registered before an import failure lower in
     the module would survive in ``_DEFAULTS`` even though the failed module is dropped).
  2. LAZY-IMPORT INVARIANT -- every call site (``data_export.py``, and in later trains
     ``api_cli.py``/``scheduler.py``/``gdpr.py``) does ``from app.ports import exportstore``
     INSIDE a function body, never at module scope (this module module-scope-imports
     ``app.config`` -> ``app.db``'s import-time engine construction). Do NOT add a module-scope
     import of this module to any config-only import path.
  3. This module must NOT module-scope-import ``s3_exportstore`` -- the S3 adapter (Train C)
     self-registers from its OWN ``configure_at_boot`` (enable-check the FIRST statement,
     no-op when off), mirroring ``s3_blobstore.py``.

Documented in: docs/architecture/data-export.md
"""

import logging
import os
from collections.abc import Iterable, Iterator
from typing import Protocol, runtime_checkable

from app import config, ports

__all__ = ["DiskExportStore", "ExportStore", "get"]

log = logging.getLogger("loc_tms.exportstore")

# Read in 1 MiB windows -- bounded memory on a large artifact (the backup_archive.sha256_file
# idiom). Also the chunk size ``put_stream`` writes and ``open_range`` yields.
_CHUNK = 1024 * 1024


@runtime_checkable
class ExportStore(Protocol):
    """Opaque, RANGE-SEEKABLE storage for staged full-dump export artifacts, keyed by a
    caller-supplied FLAT string key (``dataexport_<run_uuid>.zip``).

    ``put_stream`` writes an iterable of byte chunks under ``key`` (overwriting), streaming to
    storage a part at a time -- it NEVER buffers the whole artifact -- and returns the total
    bytes written. Its failure/exception class is ADAPTER-DEFINED (the disk adapter surfaces
    ``OSError``; an S3 adapter its own client error).

    ``open_range`` yields the bytes of ``key`` over the INCLUSIVE byte range ``[start, end]``
    (``end=None`` -> to EOF), raising ``FileNotFoundError`` when the key is absent -- the seam
    a 206 Partial Content response streams over.

    ``presign_get`` returns a Range-capable signed URL for a direct client fetch, or ``None``
    to signal "serve locally" (the disk adapter always returns ``None``).

    ``delete`` removes the artifact and returns whether one was actually removed. Like
    ``BlobStore.delete`` it MUST NOT raise on an OPERATIONAL failure (missing blob / permission
    error / remote client error) -- it is best-effort and returns ``False`` -- so the retention
    reaper's per-artifact sweep can never abort mid-loop. A MALFORMED KEY is different (a
    caller-contract violation, not an operational failure): an adapter MAY reject it fast (the
    disk adapter's ``_safe_key`` raises ``ValueError`` on a traversal key)."""

    def put_stream(self, key: str, chunks: Iterable[bytes]) -> int: ...
    def open_range(self, key: str, start: int, end: int | None) -> Iterator[bytes]: ...
    def presign_get(self, key: str, *, ttl_seconds: int) -> str | None: ...
    def delete(self, key: str) -> bool: ...


def _safe_key(key: str) -> str:
    """Reject a key that would escape EXPORTS_DIR. Keys are always the flat
    ``dataexport_<run_uuid>.zip`` form, so this only ever rejects a bug/attack -- but the port
    docstring advertises caller-supplied keys, so close the traversal door permanently and
    unconditionally, mirroring ``blobstore._safe_key``."""
    if (not key) or ("/" in key) or ("\\" in key) or key.startswith(".."):
        raise ValueError(f"unsafe export key (path separator or parent-dir escape): {key!r}")
    return key


def _ensure_dir() -> None:
    """Create EXPORTS_DIR 0o700 on first use (mirrors backup_archive's on-disk secret posture,
    DX-208). ``os.chmod`` is applied unconditionally so an existing looser dir is tightened.
    On Windows the POSIX mode is a best-effort no-op -- never raises for that reason."""
    d = config.EXPORTS_DIR
    d.mkdir(parents=True, exist_ok=True)
    try:  # noqa: SIM105
        os.chmod(d, 0o700)
    except OSError:
        pass


class DiskExportStore:
    """The DEFAULT adapter: a PLAINTEXT, Range-seekable file under
    ``config.EXPORTS_DIR / key``.

    Deliberately NOT ``storage.write_upload``'s Fernet path (a Fernet blob is not seekable, so
    ``open_range`` could not serve a 206, DX-205). ``put_stream`` writes chunks to a temp
    sibling then atomically renames (the backup_archive idiom, so a poll never sees a half-zip);
    ``open_range`` seeks the plaintext file; ``presign_get`` returns ``None`` (serve locally).
    The dir is 0o700 and each artifact 0o600 (DX-208). Every operation first runs ``_safe_key``
    so a traversal key can never reach the filesystem."""

    def put_stream(self, key: str, chunks: Iterable[bytes]) -> int:
        safe = _safe_key(key)
        _ensure_dir()
        final = config.EXPORTS_DIR / safe
        # A unique temp sibling in the SAME dir so os.replace is atomic (never cross-device).
        tmp = config.EXPORTS_DIR / f".{safe}.{os.getpid()}.{id(chunks) & 0xFFFFFF:x}.tmp"
        total = 0
        try:
            # Open 0o600 from the start (mode arg on open() is masked by umask, so chmod too).
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "wb") as fh:
                for chunk in chunks:
                    if not chunk:
                        continue
                    b = bytes(chunk)
                    fh.write(b)
                    total += len(b)
            try:  # noqa: SIM105
                os.chmod(tmp, 0o600)
            except OSError:
                pass
            os.replace(tmp, final)
        except BaseException:
            # Never leave a partial temp behind on any failure (or a KeyboardInterrupt).
            try:  # noqa: SIM105
                os.unlink(tmp)
            except OSError:
                pass
            raise
        try:  # noqa: SIM105
            os.chmod(final, 0o600)
        except OSError:
            pass
        return total

    def open_range(self, key: str, start: int, end: int | None) -> Iterator[bytes]:
        path = config.EXPORTS_DIR / _safe_key(key)
        # Raise BEFORE returning the generator so a missing key surfaces at call time, not on
        # first iteration (a generator body's raise would otherwise be deferred).
        if not path.exists():
            raise FileNotFoundError(str(path))
        start = max(0, int(start or 0))
        return self._range_reader(path, start, end)

    @staticmethod
    def _range_reader(path, start: int, end: int | None) -> Iterator[bytes]:
        with open(path, "rb") as fh:
            fh.seek(start)
            # `end` is INCLUSIVE (HTTP Range semantics); None = to EOF.
            remaining = None if end is None else max(0, int(end) - start + 1)
            while True:
                want = _CHUNK if remaining is None else min(_CHUNK, remaining)
                if want <= 0:
                    break
                buf = fh.read(want)
                if not buf:
                    break
                yield buf
                if remaining is not None:
                    remaining -= len(buf)

    def presign_get(self, key: str, *, ttl_seconds: int) -> str | None:
        # No signed URL for local disk -- _safe_key is still enforced so the contract is
        # uniform across adapters, and the artifact route serves this key over open_range.
        _safe_key(key)
        return None

    def delete(self, key: str) -> bool:
        # Best-effort (mirrors BlobStore.delete's law): True on removal, False on a missing
        # artifact or an OSError; NEVER raises operationally so the retention reaper's sweep
        # cannot abort mid-loop. A malformed key still raises ValueError (a caller bug/attack).
        path = config.EXPORTS_DIR / _safe_key(key)
        try:
            if path.exists():
                path.unlink()
                return True
        except OSError:
            log.warning("Could not delete export artifact %r at %s", key, path)
        return False


def get() -> "ExportStore":
    """The active ExportStore adapter: override-first, else the default ``DiskExportStore``,
    resolved through the frozen ``app.ports`` registry. Call sites stage with
    ``exportstore.get().put_stream(key, chunks)``, serve a range with ``.open_range(...)``,
    a signed URL with ``.presign_get(...)``, and reap with ``.delete(key)``. Mirrors
    ``blobstore.get()``."""
    return ports.get("exportstore")


# Self-register the default adapter as the module's LAST statement (frozen-file docstring
# rule -- see the module docstring, convention 1). No import failure can occur below this line.
ports.register_default("exportstore", DiskExportStore())
