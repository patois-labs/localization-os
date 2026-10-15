"""ExportStore port -- conformance suite (Batch 13, Train A).

Proves the load-bearing contracts of the NEW sibling-of-BlobStore port that names the staged
full-dump export-artifact seam (app/ports/exportstore.py + SPEC-data-export §8.3):

  A  PROTOCOL conformance: DiskExportStore is a runtime_checkable ExportStore (all four
     methods present); the default adapter resolves through the frozen ports registry.
  B  put_stream -> open_range ROUND-TRIP: a streamed multi-chunk artifact reads back
     byte-identical over the FULL range AND an inclusive PARTIAL [start,end] range (the 206
     seam); put_stream returns the exact byte count; a missing key raises FileNotFoundError.
  C  presign_get returns None on the disk adapter (-> serve locally).
  D  delete SEMANTICS: True on removal, False on missing + re-delete, never raises
     operationally (mirrors BlobStore.delete's law).
  E  _safe_key: put_stream/open_range/delete reject path-separator / parent-escape / empty
     keys with ValueError; a normal flat key still round-trips.
  F  FILESYSTEM POSTURE (DX-208): EXPORTS_DIR is 0o700 and each artifact 0o600 (POSIX only;
     on Windows os.chmod is a best-effort no-op, so the mode assertion is skipped there).
  G  OVERRIDE SEAM: a registered in-memory adapter serves put_stream/open_range/delete
     off-disk; unregister restores the default DiskExportStore.
  H  BlobStore UNTOUCHED (DX-204): app.ports.blobstore + its _safe_key still reject traversal
     keys and expose only put/get/delete -- widening did NOT happen.

Hermetic + offline (throwaway LOC_TMS_DATA_DIR). ASCII-only prints.
Final line: EXPORTSTORE PORT OK.
"""

import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(_ROOT)
sys.path.insert(0, _ROOT)
from tests._bootstrap import init_env

init_env("loctms_exportstore_")

from app import config, ports
from app.ports import exportstore

_POSIX = os.name == "posix"


class _InMemoryExportStore:
    """A registrable override adapter that keeps artifacts in a dict -- never touches disk.
    Implements exactly the four ExportStore methods."""

    def __init__(self):
        self.blobs = {}

    def put_stream(self, key, chunks):
        data = b"".join(bytes(c) for c in chunks)
        self.blobs[key] = data
        return len(data)

    def open_range(self, key, start, end):
        if key not in self.blobs:
            raise FileNotFoundError(key)
        data = self.blobs[key]
        stop = None if end is None else end + 1
        yield data[start:stop]

    def presign_get(self, key, *, ttl_seconds):
        return None

    def delete(self, key):
        return self.blobs.pop(key, None) is not None


def _read(store, key, start=0, end=None) -> bytes:
    return b"".join(store.open_range(key, start, end))


# ============================================================================
# A. protocol conformance + default resolution
# ============================================================================
def test_protocol_conformance():
    assert "exportstore" not in ports._OVERRIDES, "no override for the default-adapter test"
    store = exportstore.get()
    assert isinstance(store, exportstore.DiskExportStore), "default adapter must be DiskExportStore"
    # runtime_checkable isinstance: the disk adapter structurally satisfies the Protocol.
    assert isinstance(store, exportstore.ExportStore), (
        "DiskExportStore must satisfy the runtime_checkable ExportStore Protocol (all 4 methods)"
    )
    for meth in ("put_stream", "open_range", "presign_get", "delete"):
        assert callable(getattr(store, meth, None)), f"missing method {meth}"
    print("  (A) protocol: DiskExportStore is a runtime_checkable ExportStore (4 methods) OK")


# ============================================================================
# B. put_stream -> open_range round-trip (full + partial 206 range)
# ============================================================================
def test_put_stream_open_range_round_trip():
    store = exportstore.get()
    key = "dataexport_roundtrip.zip"
    payload = b"0123456789abcdef" * 4  # 64 bytes
    chunks = [payload[i : i + 10] for i in range(0, len(payload), 10)]
    n = store.put_stream(key, iter(chunks))
    assert n == len(payload), f"put_stream must return the exact byte count ({n} != {len(payload)})"
    assert (config.EXPORTS_DIR / key).exists(), "the disk adapter must stage to EXPORTS_DIR"
    # FULL range.
    assert _read(store, key, 0, None) == payload, "full open_range must reproduce the bytes"
    # PARTIAL, INCLUSIVE [start, end] (the 206 seam): bytes 5..11 inclusive == payload[5:12].
    assert _read(store, key, 5, 11) == payload[5:12], "inclusive [start,end] range must be exact"
    # A single byte.
    assert _read(store, key, 0, 0) == payload[0:1], "single-byte range [0,0] must yield one byte"
    # Open-ended from an offset.
    assert _read(store, key, 60, None) == payload[60:], "open-ended range must reach EOF"
    # A missing key raises FileNotFoundError (at call time, not on first iteration).
    try:
        store.open_range("dataexport_missing.zip", 0, None)
    except FileNotFoundError:
        pass
    else:
        raise AssertionError("open_range on a missing key must raise FileNotFoundError")
    print(
        "  (B) round-trip: put_stream returns the byte count; open_range reproduces full + "
        "inclusive-partial + single-byte + open-ended ranges; missing key -> FileNotFoundError OK"
    )


# ============================================================================
# C. presign_get returns None (serve locally)
# ============================================================================
def test_presign_get_none():
    store = exportstore.get()
    assert store.presign_get("dataexport_roundtrip.zip", ttl_seconds=900) is None, (
        "the disk adapter's presign_get must return None (serve locally)"
    )
    print("  (C) presign_get: disk adapter returns None (serve locally) OK")


# ============================================================================
# D. delete semantics (True/False, never raises)
# ============================================================================
def test_delete_semantics():
    store = exportstore.get()
    key = "dataexport_delete.zip"
    store.put_stream(key, [b"to-delete"])
    assert store.delete(key) is True, "delete of an existing artifact must return True"
    assert not (config.EXPORTS_DIR / key).exists(), "the artifact must be gone after delete"
    assert store.delete(key) is False, "a re-delete (now missing) must return False"
    assert store.delete("dataexport_neverexisted.zip") is False, (
        "delete of a missing artifact must return False (best-effort, never raise)"
    )
    print("  (D) delete: True on removal, False on missing + re-delete (best-effort) OK")


# ============================================================================
# E. _safe_key rejection at every method
# ============================================================================
def test_safe_key_rejection():
    store = exportstore.get()
    for bad in ("../evil", "..\\evil", "sub/dir.zip", "a\\b.zip", "..", ""):
        for label, op in (
            ("put_stream", lambda k=bad: store.put_stream(k, [b"x"])),
            ("open_range", lambda k=bad: list(store.open_range(k, 0, None))),
            ("delete", lambda k=bad: store.delete(k)),
            ("presign_get", lambda k=bad: store.presign_get(k, ttl_seconds=1)),
        ):
            try:
                op()
            except ValueError:
                pass
            else:
                raise AssertionError(f"{label}({bad!r}) must be rejected with ValueError")
    # A normal flat key still works.
    store.put_stream("dataexport_ok.zip", [b"ok"])
    assert _read(store, "dataexport_ok.zip") == b"ok"
    assert store.delete("dataexport_ok.zip") is True
    print("  (E) _safe_key: traversal/empty keys rejected on every method; flat key round-trips OK")


# ============================================================================
# F. filesystem posture (0700 dir / 0600 artifact) -- POSIX only
# ============================================================================
def test_filesystem_posture():
    store = exportstore.get()
    key = "dataexport_perms.zip"
    store.put_stream(key, [b"perms"])
    if _POSIX:
        dir_mode = os.stat(config.EXPORTS_DIR).st_mode & 0o777
        art_mode = os.stat(config.EXPORTS_DIR / key).st_mode & 0o777
        assert dir_mode == 0o700, f"EXPORTS_DIR must be 0o700, got {oct(dir_mode)}"
        assert art_mode == 0o600, f"the artifact must be 0o600, got {oct(art_mode)}"
        print("  (F) posture: EXPORTS_DIR 0o700 + artifact 0o600 (POSIX) OK")
    else:
        print("  (F) posture: POSIX mode bits not enforced on this platform (Windows); skipped OK")
    store.delete(key)


# ============================================================================
# G. override seam (in-memory adapter off-disk); unregister restores default
# ============================================================================
def test_override_seam():
    mem = _InMemoryExportStore()
    ports.register("exportstore", mem)
    try:
        assert exportstore.get() is mem, "the override must resolve through exportstore.get()"
        assert isinstance(mem, exportstore.ExportStore), "the in-memory double must conform"
        key = "dataexport_mem.zip"
        n = mem.put_stream(key, [b"abc", b"def"])
        assert n == 6 and key in mem.blobs, "the in-memory adapter must hold the streamed bytes"
        assert not (config.EXPORTS_DIR / key).exists(), "with the override, nothing lands on disk"
        assert _read(mem, key, 1, 3) == b"bcd", "the in-memory adapter's range read must be exact"
        assert mem.delete(key) is True
    finally:
        ports.unregister("exportstore")
    assert isinstance(exportstore.get(), exportstore.DiskExportStore), (
        "unregister must return resolution to the default DiskExportStore"
    )
    print("  (G) override: in-memory adapter serves off-disk; unregister restores the default OK")


# ============================================================================
# H. BlobStore untouched (DX-204)
# ============================================================================
def test_blobstore_untouched():
    from app.ports import blobstore

    # BlobStore still exposes only put/get/delete (no open_range/put_stream widened in).
    assert not hasattr(blobstore.BlobStore, "open_range"), (
        "BlobStore must NOT have grown open_range -- ExportStore is a SEPARATE port (DX-204)"
    )
    # Its _safe_key still rejects traversal keys.
    try:
        blobstore.DiskBlobStore().get("../evil")
    except ValueError:
        pass
    else:
        raise AssertionError("blobstore._safe_key must still reject traversal keys")
    print("  (H) BlobStore untouched: no open_range on the Protocol; _safe_key still bites OK")


def main():
    test_protocol_conformance()
    test_put_stream_open_range_round_trip()
    test_presign_get_none()
    test_delete_semantics()
    test_safe_key_rejection()
    test_filesystem_posture()
    test_override_seam()
    test_blobstore_untouched()
    print("EXPORTSTORE PORT OK")


if __name__ == "__main__":
    main()
