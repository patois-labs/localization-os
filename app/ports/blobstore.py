"""BlobStore port -- the named upload-blob storage seam (composable-core phase 1, Train 3).

This module NAMES the one blob-storage seam Localization OS has today: the ``uploads/``
directory served by ``app.services.storage``. It is a thin FACADE over
``app.services.storage`` -- WRAP, DON'T MOVE: ``storage.py`` is untouched; the default
adapter delegates ``put``/``get`` to ``storage.write_upload``/``storage.read_upload`` so
the Fernet-at-rest behavior (``_MAGIC``, the ``encrypt_uploads`` Setting, read-path
auto-detect) is byte-identical. After this train, ONLY ``app.ports.blobstore`` imports
``app.services.storage`` -- the six upload call sites (ingest write + ingest/export/
image-loc/web-images reads + retention delete) speak KEYS through this port, and the
``blobstore -> storage`` import-contract row goes green.

THE KEY / PATH SEAM
-------------------
The ``config.UPLOADS_DIR / key`` path join lives INSIDE this adapter, so call sites no
longer construct filesystem paths. That is the seam a future S3 adapter needs: it maps the
same opaque ``key`` onto an object store instead of a directory. New uploads use a
persisted UUID key; ``document_key`` also resolves legacy id.format keys.

DELETE IS A REAL OPERATION (spec BLOCKER-3)
-------------------------------------------
``delete(key) -> bool`` backs retention-purge AND GDPR/document-erase. It is NOT
path-based at the call site: under a future S3 adapter a path-based delete would leave
blobs orphaned and a GDPR erase silently not erasing. The default adapter preserves
``retention._delete_upload_file``'s exact best-effort contract: **True** when a blob was
removed, **False** when the blob was missing OR an ``OSError`` occurred (logged).

MISSING IS NOT FAILURE (#399)
-----------------------------
Collapsing "no blob existed" and "the backend refused to remove it" into the same
``False`` loses the retry identity: an erase that commits its row delete after a
TRANSIENT failure (an open file handle, a flaky S3 HEAD/delete) has no way to know the
key must be retried, and private content outlives its database identity forever.
``delete_status(key)`` keeps the three outcomes distinct -- ``DeleteStatus.REMOVED`` /
``MISSING`` / ``FAILED`` -- and ``delete()`` is exactly
``delete_status(key) == DeleteStatus.REMOVED`` (byte-identical truth table). Callers
that must not lose the key (``retention``) persist a durable retry record ONLY on
``FAILED``; adapters that predate the method (test fakes) are handled conservatively at
the retention seam, where an ambiguous ``delete() -> False`` counts as ``FAILED``.

LAZY-IMPORT INVARIANT (spec HIGH-1 family): this module module-scope-imports ``storage``,
which transitively reaches ``settings`` -> ``app.db``'s import-time engine construction.
That is FINE here because this module is only ever imported LAZILY -- every call site does
``from app.ports import blobstore`` inside a function body, never at module scope, exactly
as the ``storage`` imports it replaces were lazy. Do NOT add a module-scope import of this
module to any config-only import path.

Documented in: docs/architecture/kernel-and-ports.md
"""

import logging
from typing import Protocol, runtime_checkable

from app import config, ports
from app.services import storage

__all__ = ["BlobStore", "DeleteStatus", "DiskBlobStore", "document_key", "get"]


def document_key(doc) -> str:
    """The immutable upload identity, with compatibility for pre-UUID documents."""
    return getattr(doc, "upload_blob_key", None) or f"{doc.id}.{doc.fmt}"


class DeleteStatus:
    """The three outcomes of a blob delete (#399). Plain strings, like every other
    "enum" in this codebase.

    REMOVED -- a blob existed and was removed.
    MISSING -- no blob existed under the key; not an error, nothing to retry.
    FAILED  -- an operational error (OSError / S3 client error); the blob MAY still
    exist, so a caller that erases the key's last durable reference MUST keep the key
    somewhere retryable (retention's BlobDeleteOutbox row)."""

    REMOVED = "removed"
    MISSING = "missing"
    FAILED = "failed"


log = logging.getLogger("loc_tms.blobstore")


@runtime_checkable
class BlobStore(Protocol):
    """Opaque upload-blob storage keyed by a caller-supplied string key.

    ``put`` writes bytes under ``key`` (overwriting); its failure/exception class is
    ADAPTER-DEFINED (the disk adapter surfaces ``OSError``; an S3 adapter its own client
    error) -- callers that must tolerate a failed write guard the call site. ``get`` returns
    the bytes, raising ``OSError`` / ``FileNotFoundError`` when the key is absent, exactly as
    the disk read did.

    ``delete`` removes the blob and returns whether one was actually removed. **delete MUST
    NOT raise on an OPERATIONAL failure** -- it is best-effort and returns ``False`` on any
    backend failure (missing blob, permission error, remote client error). ``retention``
    calls it in a per-document GDPR/purge sweep, so a raising ``delete`` would abort the
    whole sweep mid-loop; every adapter must honor this (the disk adapter swallows
    ``OSError``; a future S3 adapter must likewise return ``False`` rather than raising
    ``botocore.ClientError``). ``retention._delete_upload_file`` ALSO wraps the call in a
    belt-and-suspenders ``try/except`` so a misbehaving adapter still cannot break a sweep.
    A MALFORMED KEY is different: it is a caller-contract violation, not an operational
    failure, so an adapter MAY reject it fast (the disk adapter's ``_safe_key`` raises
    ``ValueError`` on a traversal key) -- that is a fail-loud guard against a bug/attack, and
    the retention wrapper's ``except`` keeps even that from unwinding a sweep.

    ``delete_status`` is the tri-state form of the same operation (#399): it returns a
    ``DeleteStatus`` so a caller can tell MISSING from FAILED and keep the key retryable
    only when a blob may still exist. It obeys the same never-raise-on-operational-failure
    law (FAILED instead). ``delete`` must stay exactly ``delete_status(key) ==
    DeleteStatus.REMOVED``."""

    def put(self, key: str, data: bytes) -> None: ...
    def get(self, key: str) -> bytes: ...
    def delete(self, key: str) -> bool: ...
    def delete_status(self, key: str) -> str: ...


def _safe_key(key: str) -> str:
    """Reject a key that would escape UPLOADS_DIR. Phase-1 keys are always ``{int}.{fmt}``,
    so this only ever rejects a future misuse -- but the port docstring advertises
    caller-supplied keys, so close the traversal door permanently and unconditionally."""
    if (not key) or ("/" in key) or ("\\" in key) or key.startswith(".."):
        raise ValueError(f"unsafe blob key (path separator or parent-dir escape): {key!r}")
    return key


class DiskBlobStore:
    """The DEFAULT adapter: ``config.UPLOADS_DIR / key`` on the local filesystem.

    ``put``/``get`` delegate to ``storage.write_upload``/``storage.read_upload`` so the
    Fernet-at-rest behavior is byte-identical (wrap, don't move). ``delete`` is new adapter
    code reproducing ``retention._delete_upload_file``'s best-effort ``path.unlink()``
    semantics: True on removal, False on a missing blob or an ``OSError``; ``delete_status``
    is the tri-state form (#399) and ``delete`` is its one-line projection. Every operation
    first runs ``_safe_key`` so a traversal key can never reach the filesystem."""

    def put(self, key: str, data: bytes) -> None:
        storage.write_upload(config.UPLOADS_DIR / _safe_key(key), data)

    def get(self, key: str) -> bytes:
        return storage.read_upload(config.UPLOADS_DIR / _safe_key(key))

    def delete(self, key: str) -> bool:
        return self.delete_status(key) == DeleteStatus.REMOVED

    def delete_status(self, key: str) -> str:
        # The blob is removed as an OPAQUE file -- NEVER decrypted first (matches the old
        # retention._delete_upload_file). Best-effort: any OSError is logged, not raised.
        # Unlink FIRST and classify the FileNotFoundError it raises as MISSING: the old
        # exists()-then-unlink check had a TOCTOU window where a blob vanishing between
        # the two calls surfaced the unlink's FileNotFoundError as a (False-path) failure.
        path = config.UPLOADS_DIR / _safe_key(key)
        try:
            path.unlink()
            return DeleteStatus.REMOVED
        except FileNotFoundError:
            return DeleteStatus.MISSING
        except OSError:
            # #399: this (a real refusal -- a Windows open-handle, a permission error) is
            # NOT the same event as "absent"; the caller must keep the key retryable.
            log.warning("Could not delete blob %r at %s", key, path)
            return DeleteStatus.FAILED


def get() -> "BlobStore":
    """The active BlobStore adapter: override-first, else the default ``DiskBlobStore``,
    resolved through the frozen ``app.ports`` registry. Call sites read a blob with
    ``blobstore.get().get(key)``, write with ``.put(key, data)``, delete with
    ``.delete(key)``. Mirrors ``telemetry.get()``'s active-adapter accessor."""
    return ports.get("blobstore")


# Self-register the default adapter as the module's LAST statement (frozen-file docstring
# rule): a default registered BEFORE an import failure lower in the module would survive in
# _DEFAULTS even though the failed module is dropped from sys.modules. No such import here,
# but the rule is followed uniformly.
ports.register_default("blobstore", DiskBlobStore())
