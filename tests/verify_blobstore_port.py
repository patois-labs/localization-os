"""BlobStore port -- suite for composable-core phase 1, Train 3.

Proves the load-bearing contracts of the port that names the upload-blob seam
(see app/ports/blobstore.py + the SPEC-ports-phase1 spec Train 3):

  A  DEFAULT ADAPTER round-trip: DiskBlobStore put -> get -> delete on a throwaway
     LOC_TMS_DATA_DIR; a missing get() raises OSError (the on-disk read contract).
  B  DELETE SEMANTICS: delete() returns True on an existing blob, False on a missing one
     AND on a re-delete -- byte-for-byte retention._delete_upload_file's best-effort
     contract (True on removal, False otherwise).
  C  FERNET-AT-REST end-to-end on the DEFAULT adapter: with encrypt_uploads=1 the on-disk
     bytes start with the _MAGIC marker and get() returns plaintext; with it off the bytes
     are stored raw; a file written encrypted stays readable after the Setting is toggled
     off (read-path auto-detect -- toggling never orphans).
  D  CANONICAL KEY / retention delete real call path: retention._delete_upload_file keys on
     f"{doc.id}.{doc.fmt}" with NO 'bin' fallback -- a blob PUT under that exact key is
     removed (proving the key form), and delete_document reports file_removed. Since #399
     _delete_upload_file returns a DeleteStatus (REMOVED / MISSING / FAILED), not a bool.
  E  SEAM at real call sites: a registered IN-MEMORY adapter serves a real upload (ingest
     write) + export (export read) + retention delete WITHOUT ever touching disk -- proving
     the six call sites resolve through the port, not a hard-wired storage module.
  F  EXPORT->IMPORT round-trip through the port (the 2026-08-03 data-loss history): a real
     ingest writes the blob via the port, a real build_translated_file reads it back via the
     port and reproduces the translated file, AND the stored blob read straight through
     blobstore.get().get(key) is byte-identical to what was ingested.
  G  TOTALITY (M1): an adapter whose delete() RAISES is swallowed to FAILED by
     retention._delete_upload_file, a real retention.purge sweep still COMPLETES (the aged
     doc is reaped), and the failed delete is REPORTED PENDING through the #399 blob-delete
     outbox -- a phase-3 S3 ClientError can never abort a GDPR/purge sweep, and can never
     lose the blob's retry identity either.
  H  KEY SAFETY (L1): put/get/delete reject path-separator / parent-escape / empty keys with
     ValueError (the traversal door stays shut while keys are still {int}.{fmt}).
  I  TRI-STATE DELETE STATUS (#399): delete_status keeps REMOVED / MISSING / FAILED distinct
     -- FAILED via a REAL unmocked unlink refusal (directory path) -- and delete() stays
     exactly delete_status(key) == REMOVED.

Hermetic + offline (throwaway LOC_TMS_DATA_DIR, MockProvider). ASCII-only prints.
Final line: BLOBSTORE PORT OK.
"""

import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(_ROOT)
sys.path.insert(0, _ROOT)
from tests._bootstrap import init_env

init_env("loctms_blobstore_")

from fastapi.testclient import TestClient
from sqlmodel import Session, select

from app import config, ports
from app.db import engine, init_db
from app.main import app
from app.models import Document, Role, Segment, User
from app.ports import blobstore
from app.services import apikeys, export, retention, settings, storage

init_db()
_LOCALE = "fr-FR"
_SRC = b"Hello world. This is a test."


# --- helpers (modelled on verify_export_held_locale) -------------------------
def _admin_key():
    with Session(engine) as s:
        u = s.exec(select(User).where(User.email == "blob-admin@x.io")).first()
        if not u:
            u = User(email="blob-admin@x.io", name="A", role=Role.ADMIN, is_active=True)
            s.add(u)
            s.commit()
            s.refresh(u)
        uid = u.id
    _row, raw = apikeys.create_key(uid, "blob-api", scopes="api docs")
    return raw


client = TestClient(app)
H = {"X-Loc-Token": _admin_key()}


def _project(name):
    r = client.post(
        "/projects",
        data={
            "name": name,
            "source_locale": "en-US",
            "target_locales": _LOCALE,
            "default_provider": "mock",
        },
        follow_redirects=False,
    )
    return int(r.headers["location"].rsplit("/", 1)[-1])


def _upload(pid, data=_SRC):
    r = client.post(
        f"/projects/{pid}/documents",
        data={"mode": "segmented"},
        files={"file": ("hello.txt", data, "text/plain")},
        follow_redirects=False,
    )
    return int(r.headers["location"].rsplit("/", 1)[-1].split("?")[0])


def _translate_all(doc_id):
    with Session(engine) as s:
        for sg in s.exec(
            select(Segment).where(Segment.document_id == doc_id, Segment.target_locale == _LOCALE)
        ).all():
            sg.target_text = "traduit"
            sg.status = "mt"
            s.add(sg)
        s.commit()


class _InMemoryBlobStore:
    """A registrable override adapter that keeps blobs in a dict -- never touches disk.
    get() raises FileNotFoundError (an OSError subclass, so ingest's `except OSError`
    catches it exactly as a missing disk file would)."""

    def __init__(self):
        self.blobs = {}

    def put(self, key, data):
        self.blobs[key] = bytes(data)

    def get(self, key):
        if key not in self.blobs:
            raise FileNotFoundError(key)
        return self.blobs[key]

    def delete(self, key):
        return self.blobs.pop(key, None) is not None


class _RaisingBlobStore:
    """An adapter whose every operation raises -- the phase-3 hazard (an S3 store raising
    botocore.ClientError) in the abstract. delete() raising must NOT abort a purge sweep."""

    def put(self, key, data):
        raise RuntimeError("put boom")

    def get(self, key):
        raise RuntimeError("get boom")

    def delete(self, key):
        raise RuntimeError("delete boom -- must never abort a GDPR/purge sweep")


# ============================================================================
# A. default adapter round-trip
# ============================================================================
def test_default_adapter_round_trip():
    settings.set_many({"encrypt_uploads": "0"})
    assert "blobstore" not in ports._OVERRIDES, "no override for the default-adapter test"
    store = blobstore.get()
    assert isinstance(store, blobstore.DiskBlobStore), "the default adapter must be DiskBlobStore"
    key = "9001.txt"
    store.put(key, b"payload-A")
    assert store.get(key) == b"payload-A", "put->get must round-trip the exact bytes"
    assert (config.UPLOADS_DIR / key).exists(), "the default adapter must write to disk"
    # A missing key raises OSError (the on-disk read contract the call sites rely on).
    try:
        store.get("does-not-exist.txt")
    except OSError:
        pass
    else:
        raise AssertionError("get() on a missing key must raise OSError")
    assert store.delete(key) is True and not (config.UPLOADS_DIR / key).exists()
    print(
        "  (A) default adapter: DiskBlobStore put->get->delete round-trips on disk; "
        "missing get() raises OSError OK"
    )


# ============================================================================
# B. delete semantics (True on removal, False on missing / re-delete)
# ============================================================================
def test_delete_semantics_match_best_effort_contract():
    settings.set_many({"encrypt_uploads": "0"})
    store = blobstore.get()
    key = "9002.md"
    store.put(key, b"to-delete")
    assert store.delete(key) is True, "delete of an existing blob must return True"
    assert store.delete(key) is False, "a re-delete (now missing) must return False"
    assert store.delete("never-existed.txt") is False, (
        "delete of a missing blob must return False (best-effort, never raise)"
    )
    print(
        "  (B) delete semantics: True on removal, False on missing + re-delete "
        "(retention._delete_upload_file's exact contract) OK"
    )


# ============================================================================
# C. Fernet-at-rest end-to-end on the DEFAULT adapter (encrypt_uploads Setting)
# ============================================================================
def test_encrypt_uploads_setting_honored_end_to_end():
    store = blobstore.get()

    # encrypt ON: on-disk bytes carry the _MAGIC marker; get() returns plaintext.
    settings.set_many({"encrypt_uploads": "1"})
    enc_key = "9003.txt"
    store.put(enc_key, b"secret-bytes")
    raw = (config.UPLOADS_DIR / enc_key).read_bytes()
    assert raw.startswith(storage._MAGIC), (
        "encrypt_uploads=1 must write the _MAGIC-prefixed ciphertext through the port"
    )
    assert b"secret-bytes" not in raw, "the plaintext must not appear on disk when encrypted"
    assert store.get(enc_key) == b"secret-bytes", "get() must decrypt transparently"

    # encrypt OFF: bytes stored raw.
    settings.set_many({"encrypt_uploads": "0"})
    plain_key = "9004.txt"
    store.put(plain_key, b"plain-bytes")
    assert (config.UPLOADS_DIR / plain_key).read_bytes() == b"plain-bytes", (
        "encrypt_uploads=0 must store bytes raw"
    )

    # Toggling the Setting OFF must not orphan the already-encrypted file: read-path
    # auto-detect still decrypts it (the encrypt/decrypt auto-detect invariant).
    assert store.get(enc_key) == b"secret-bytes", (
        "a file written encrypted must stay readable after encrypt_uploads is toggled off"
    )
    print(
        "  (C) encrypt_uploads end-to-end: _MAGIC on disk when on, raw when off, and an "
        "encrypted file survives a toggle-off (auto-detect) OK"
    )


# ============================================================================
# D. canonical key form + retention delete real call path
# ============================================================================
def test_canonical_key_and_retention_delete_path():
    settings.set_many({"encrypt_uploads": "0"})
    # A committed document ALWAYS has a non-empty fmt (ingest raises UnsupportedFormat
    # before any row/blob is created), so the canonical key is f"{doc.id}.{doc.fmt}" with
    # no 'bin' fallback. Put a blob under EXACTLY that key and prove the delete path finds
    # it -- had it kept keying on 'bin', it would have missed this blob.
    with Session(engine) as s:
        proj_id = _project("blob-del")  # need a real project for the FK
        doc = Document(project_id=proj_id, filename="d.txt", fmt="txt")
        s.add(doc)
        s.commit()
        s.refresh(doc)
        did = doc.id
    key = f"{did}.txt"
    blobstore.get().put(key, b"delete-me")
    with Session(engine) as s:
        doc = s.get(Document, did)
        assert retention._delete_upload_file(doc) == blobstore.DeleteStatus.REMOVED, (
            "retention._delete_upload_file must key on f'{id}.{fmt}' and remove the blob"
        )
    assert not (config.UPLOADS_DIR / key).exists(), "the blob must be gone"
    with Session(engine) as s:
        doc = s.get(Document, did)
        assert retention._delete_upload_file(doc) == blobstore.DeleteStatus.MISSING, (
            "a second delete (now missing) must report MISSING, not FAILED (#399)"
        )
    print(
        "  (D) canonical key: retention._delete_upload_file keys on f'{id}.{fmt}' (no "
        "'bin'), removes the blob, and reports MISSING on a re-delete OK"
    )


# ============================================================================
# E. seam -- an in-memory override serves real ingest/export/delete off-disk
# ============================================================================
def test_in_memory_adapter_serves_real_call_sites_off_disk():
    settings.set_many({"encrypt_uploads": "0"})
    mem = _InMemoryBlobStore()
    ports.register("blobstore", mem)
    try:
        assert blobstore.get() is mem, "the override must resolve through blobstore.get()"
        pid = _project("blob-seam")
        did = _upload(pid)  # real ingest -> port.put -> memory
        with Session(engine) as s:
            key = blobstore.document_key(s.get(Document, did))
        assert key.startswith("upload_") and key.endswith(".txt"), key
        assert key in mem.blobs, "the ingest WRITE site must route through the port"
        assert mem.blobs[key] == _SRC, "the in-memory adapter must hold the uploaded bytes"
        assert not (config.UPLOADS_DIR / key).exists(), (
            "with the in-memory override, NOTHING may land on disk"
        )

        _translate_all(did)
        with Session(engine) as s:
            out = export.build_translated_file(s, s.get(Document, did), _LOCALE)
        assert b"traduit" in out, "the export READ site must read the blob through the port"

        with Session(engine) as s:
            assert retention._delete_upload_file(s.get(Document, did)) == (
                blobstore.DeleteStatus.REMOVED
            ), "the retention DELETE site must route through the port (in-memory)"
        assert key not in mem.blobs, "the in-memory blob must be gone after delete"
    finally:
        ports.unregister("blobstore")
    assert isinstance(blobstore.get(), blobstore.DiskBlobStore), (
        "unregister must return resolution to the default DiskBlobStore"
    )
    print(
        "  (E) seam: an in-memory override serves a real ingest WRITE + export READ + "
        "retention DELETE with zero disk I/O; unregister restores the default OK"
    )


# ============================================================================
# F. export->import round-trip through the port (data-loss regression)
# ============================================================================
def test_export_import_round_trip_through_the_port():
    settings.set_many({"encrypt_uploads": "0"})
    assert "blobstore" not in ports._OVERRIDES, "default disk adapter for the round-trip"
    pid = _project("blob-roundtrip")
    did = _upload(pid)  # ingest WRITE via the port
    with Session(engine) as s:
        key = blobstore.document_key(s.get(Document, did))
    # Explicit port-path round-trip assert: the stored blob, read straight through the
    # port, is byte-identical to what ingest wrote (the invariant export depends on).
    assert blobstore.get().get(key) == _SRC, (
        "the blob read back through the port must equal the ingested bytes (no corruption)"
    )
    _translate_all(did)
    with Session(engine) as s:
        out = export.build_translated_file(s, s.get(Document, did), _LOCALE)
    assert b"traduit" in out and b"Hello world" not in out, (
        "export must read the original through the port and emit the translated file"
    )
    print(
        "  (F) export->import: the ingested blob round-trips byte-identical through the "
        "port, and a real export rebuilds the translated file from it OK"
    )


# ============================================================================
# G. totality -- a raising adapter delete never aborts a purge sweep (M1)
# ============================================================================
def test_raising_adapter_never_aborts_a_purge_sweep():
    from datetime import datetime, timedelta

    settings.set_many({"encrypt_uploads": "0"})
    pid = _project("blob-raise")
    with Session(engine) as s:
        doc = Document(project_id=pid, filename="r.txt", fmt="txt")
        s.add(doc)
        s.commit()
        s.refresh(doc)
        did = doc.id
        s.add(
            Segment(
                document_id=did,
                target_locale=_LOCALE,
                order_index=0,
                source_text="a",
                target_text="[fr] a",
            )
        )
        s.commit()
        doc = s.get(Document, did)  # backdate well past any window
        doc.created_at = datetime.utcnow() - timedelta(days=400)
        s.add(doc)
        s.commit()
    ports.register("blobstore", _RaisingBlobStore())
    try:
        # Direct: the retention call-site guard turns a raising delete into FAILED (the
        # exception never escapes _delete_upload_file) -- and FAILED is exactly what the
        # #399 outbox keys on, so a raising adapter can never vanish the retry identity.
        with Session(engine) as s:
            assert retention._delete_upload_file(s.get(Document, did)) == (
                blobstore.DeleteStatus.FAILED
            ), "a raising adapter delete must be swallowed into FAILED (never propagate)"
        # End-to-end: a real purge sweep COMPLETES despite the raising adapter -- the aged
        # doc is still reaped and files removed is 0. Without the guard the sweep would
        # abort at the first aged document.
        settings.set_many({"retention_enabled": "1", "retention_days": "30"})
        res = retention.purge()
        assert res["documents"] >= 1, ("the aged doc must still be purged", res)
        assert res["files"] == 0, ("a raising delete must report zero files removed", res)
        # #399: and the failed delete is REPORTED PENDING (outbox row survives the sweep,
        # retried by every later purge) instead of being lost with the document row.
        assert res["blob_deletes_pending"] >= 1, (
            "a failed blob delete must report pending, never lost",
            res,
        )
    finally:
        ports.unregister("blobstore")
        settings.set_many({"retention_enabled": "0"})
    with Session(engine) as s:
        assert s.get(Document, did) is None, "the aged document row must be gone after purge"
    print(
        "  (G) totality: a raising adapter delete is swallowed to False AND a real "
        "retention.purge sweep completes (doc reaped, files=0) OK"
    )


# ============================================================================
# H. key safety -- traversal keys are rejected at every operation (L1)
# ============================================================================
def test_traversal_key_is_rejected():
    settings.set_many({"encrypt_uploads": "0"})
    assert "blobstore" not in ports._OVERRIDES, "default disk adapter for the key-safety test"
    store = blobstore.get()
    for bad in ("../evil", "..\\evil", "sub/dir.txt", "a\\b.txt", "..", ""):
        for label, op in (
            ("put", lambda k=bad: store.put(k, b"x")),
            ("get", lambda k=bad: store.get(k)),
            ("delete", lambda k=bad: store.delete(k)),
        ):
            try:
                op()
            except ValueError:
                pass
            else:
                raise AssertionError(
                    f"{label}({bad!r}) must be rejected with ValueError (traversal guard)"
                )
    # The guard must NOT over-reject a normal {id}.{fmt} key.
    store.put("7777.txt", b"ok")
    assert store.get("7777.txt") == b"ok"
    assert store.delete("7777.txt") is True
    print(
        "  (H) key safety: put/get/delete reject path-separator / parent-escape / empty "
        "keys with ValueError; a normal {id}.{fmt} key still round-trips OK"
    )


# ============================================================================
# I. tri-state delete status: REMOVED / MISSING / FAILED (#399)
# ============================================================================
def test_delete_status_distinguishes_missing_from_failed():
    settings.set_many({"encrypt_uploads": "0"})
    assert "blobstore" not in ports._OVERRIDES, "default disk adapter for the tri-state test"
    store = blobstore.get()
    # REMOVED -- a real blob, really removed.
    store.put("9005.txt", b"gone-soon")
    assert store.delete_status("9005.txt") == blobstore.DeleteStatus.REMOVED
    # MISSING -- nothing existed (and a re-delete stays MISSING): nothing to retry.
    assert store.delete_status("9005.txt") == blobstore.DeleteStatus.MISSING
    assert store.delete_status("never-existed.txt") == blobstore.DeleteStatus.MISSING
    # FAILED -- reproduced with a REAL, unmocked OS refusal: the key path is a DIRECTORY,
    # so unlink() raises OSError (IsADirectoryError on POSIX, PermissionError on Windows)
    # exactly like an open file handle on Windows does on a real blob.
    blocked = config.UPLOADS_DIR / "9006.txt"
    blocked.mkdir(parents=True, exist_ok=True)
    try:
        assert store.delete_status("9006.txt") == blobstore.DeleteStatus.FAILED, (
            "an unlink refusal must report FAILED, never collapse into MISSING (#399)"
        )
        assert store.delete("9006.txt") is False, (
            "delete() keeps its exact bool contract: True only for REMOVED"
        )
    finally:
        blocked.rmdir()
    # delete() parity on the other two states: REMOVED -> True, MISSING -> False.
    store.put("9007.txt", b"x")
    assert store.delete("9007.txt") is True
    assert store.delete("9007.txt") is False
    print(
        "  (I) delete_status: REMOVED / MISSING / FAILED distinct; FAILED from a real "
        "unlink refusal (directory path); delete() stays the REMOVED projection OK"
    )


def main():
    test_default_adapter_round_trip()
    test_delete_semantics_match_best_effort_contract()
    test_encrypt_uploads_setting_honored_end_to_end()
    test_canonical_key_and_retention_delete_path()
    test_in_memory_adapter_serves_real_call_sites_off_disk()
    test_export_import_round_trip_through_the_port()
    test_raising_adapter_never_aborts_a_purge_sweep()
    test_traversal_key_is_rejected()
    test_delete_status_distinguishes_missing_from_failed()
    print("BLOBSTORE PORT OK")


if __name__ == "__main__":
    main()
