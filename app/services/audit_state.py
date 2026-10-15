"""Filesystem trust state for the audit chain.

Audit signing and verification rely on three private files in DATA_DIR, all held
outside the database so a DB-only attacker cannot forge a verifying chain:

- ``.audit_key`` is the signing key. ``key()`` derives it once from
  ``sha256("loc-audit:" + SESSION_SECRET)`` and persists it; afterward it re-reads the
  stored bytes and never re-derives them, so rotating SESSION_SECRET does not change the
  audit key and existing rows keep verifying. Session-secret rotation and audit-key
  rotation are deliberately separate.
- ``.audit_epochs`` holds each signing key created by ``audit rotate-key`` with its
  ``after`` row id (appended by ``save_epoch``); that key signs rows after the id until the
  next epoch, the last entry is the current key, and ``.audit_key`` keeps covering rows
  before the first rotation, so history predating a rotation still verifies.
- ``.audit_head`` is the MAC'd head witness pinning the newest row id and hash, which
  catches tail truncation.

The host-only ``audit rotate-key`` command is the dedicated audit-key rotation: it refuses a
detected server listener (stop all writers first), writes a forced ``audit.key_rotated`` row
signed by the old key and countersigned by the new one, then records the new epoch in
``.audit_epochs``. Back up all three files together with the database (``db backup --full``).

Documented in: docs/architecture/audit-chain.md, operator procedure in docs/SECRETS.md
"""

import errno
import hashlib
import hmac
import json
import logging
import os
import secrets
import tempfile
import threading
import time
from contextlib import contextmanager, suppress
from contextvars import ContextVar

from app import config

_KEY_TABLE = ContextVar("audit_key_table", default=None)
_BUFFER = ContextVar("audit_witness_buffer", default=None)
_PENDING = {}
_PUBLISHED = {}
_GENERATION = 0
_logger = logging.getLogger("loc_tms.audit")
_PROCESS_GUARD = threading.Lock()
_PROCESS_USERS = 0
_PROCESS_FD = None
_PROCESS_TIMEOUT = 30.0
_FILES = {}  # path -> (stat signature, bytes, trusted) for the rarely-changing trust files
# The caches below are consulted ONLY inside `cached_trust()` (the audit write path). Verification,
# reset-anchor, rotate and every other maintenance caller read the files fresh every time.
_CACHE_OK = ContextVar("audit_trust_cache", default=False)
# head path -> (stat signature, key bytes, state) of the witness THIS process published or verified
_HEAD = {}
# a file this much older than the cache fill cannot still be mid-rewrite
_SETTLE_NS = 3_000_000_000


class ProcessLockError(ValueError):
    """The shared writer lock is unavailable; never proceed without ownership."""


@contextmanager
def process_lock():
    """Exclude other processes until every local witness publisher has finished.

    Threads share the OS lock and still serialize DB work with audit._chain_lock.
    This preserves concurrent local fsyncs and their generation ordering. Always
    enter BEFORE the chain lock, including verification and nested maintenance.
    All writers of a DB must share DATA_DIR (the keys and witness already require it).
    Never unlink the lock file: replacing its inode would allow two lock owners.
    """
    global _PROCESS_USERS, _PROCESS_FD
    with _PROCESS_GUARD:
        if not _PROCESS_USERS:
            fd = None
            try:
                config.private_directory(config.DATA_DIR)
                fd = os.open(config.DATA_DIR / ".audit_lock", os.O_CREAT | os.O_RDWR, 0o600)
                deadline = time.monotonic() + _PROCESS_TIMEOUT
                while True:
                    try:
                        if os.name == "nt":
                            import msvcrt

                            os.lseek(fd, 0, os.SEEK_SET)
                            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
                        else:
                            import fcntl

                            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                        break
                    except OSError as exc:
                        if exc.errno not in (errno.EACCES, errno.EAGAIN, errno.EDEADLK):
                            raise
                        if time.monotonic() >= deadline:
                            raise TimeoutError("Audit process lock timed out") from exc
                        time.sleep(0.01)
            except (OSError, ImportError) as exc:
                if fd is not None:
                    os.close(fd)
                raise ProcessLockError("Audit process lock unavailable") from exc
            _PROCESS_FD = fd
        _PROCESS_USERS += 1
    try:
        yield
    finally:
        with _PROCESS_GUARD:
            _PROCESS_USERS -= 1
            if not _PROCESS_USERS:
                # Closing releases the OS lock, including on process termination.
                os.close(_PROCESS_FD)
                _PROCESS_FD = None


@contextmanager
def buffered_witness(chain_lock):
    """Fsync outside the chain lock, publish monotonically under that lock.

    Writers see pending state, including its broken latch. A slow older fsync can
    never replace a newer published witness. No background thread or durability loss.
    """
    if _BUFFER.get() is not None:
        yield
        return
    updates = []
    token = _BUFFER.set(updates)
    try:
        yield
    finally:
        _BUFFER.reset(token)
        if updates:
            path, generation, content, state, signing = updates[-1]
            name = None
            try:
                name = _prepare(content)
                identity = _identity(name)
                with chain_lock:
                    if generation > _PUBLISHED.get(path, 0):
                        _publish(name, path)
                        _PUBLISHED[path] = generation
                        _remember_head(path, state, identity, signing)
                    if _PENDING.get(path, (None,))[0] == generation:
                        _PENDING.pop(path, None)
            except OSError as exc:
                with chain_lock:
                    if _PENDING.get(path, (None,))[0] == generation:
                        _PENDING.pop(path, None)
                # Report separately so a simultaneous fail-closed DB exception from
                # the body is never replaced (and swallowed) by a trust-file error.
                _publish_warning(exc)
            finally:
                if name and os.path.exists(name):
                    with suppress(OSError):
                        os.unlink(name)


class TrustStateError(ValueError):
    """Unavailable trust material, independent of the DB contention posture."""


@contextmanager
def snapshot():
    """Resolve keys once for an operation; nested helpers share its immutable table."""
    if _KEY_TABLE.get() is not None:
        yield
        return
    try:
        material, entries = key(), epochs()
        table = (material, tuple((e["after"], bytes.fromhex(e["material"])) for e in entries))
        previous = 0
        for after, epoch_key in table[1]:
            if not isinstance(after, int) or after <= previous or len(epoch_key) != 32:
                raise ValueError("Invalid audit epoch")
            previous = after
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise TrustStateError(
            "Audit key/epoch files unavailable; restore matching trust files"
        ) from exc
    token = _KEY_TABLE.set(table)
    try:
        yield
    finally:
        _KEY_TABLE.reset(token)


@contextmanager
def cached_trust():
    """Opt the audit WRITE path into the trust-file caches (see ``_stable_bytes``, ``read``)."""
    token = _CACHE_OK.set(True)
    try:
        yield
    finally:
        _CACHE_OK.reset(token)


def _signature(st):
    # ctime cannot be set from user space on POSIX, so an in-place edit that restores the old
    # mtime (same size, same inode) still changes the signature; a restore (copy) does too.
    return (st.st_mtime_ns, st.st_ctime_ns, st.st_size, st.st_ino, st.st_dev)


def _stable_bytes(path):
    """``path.read_bytes()`` for a trust file that almost never changes (key, epochs, format).

    Re-opening three small files on every audit row cost about 1.5 ms per row on Windows
    (#618). The bytes are reused only while ``os.stat`` still reports the same mtime, size,
    inode and device, so a replaced, edited, restored or deleted file is re-read on the very
    next call (a missing file still raises FileNotFoundError, so the fail-closed checks are
    unchanged). A file modified within the last few seconds is never served from the cache,
    which closes the same-timestamp window (the "racy git" case). The audit head witness is
    NOT cached: it changes on every write and other processes publish it.
    """
    if not _CACHE_OK.get():
        return path.read_bytes()
    st = os.stat(path)
    sig = _signature(st)
    hit = _FILES.get(path)
    if hit is not None and hit[0] == sig and hit[2]:
        return hit[1]
    data = path.read_bytes()
    settled = time.time_ns() - max(st.st_mtime_ns, st.st_ctime_ns) > _SETTLE_NS
    _FILES[path] = (sig, data, settled)
    return data


def base_key():
    table = _KEY_TABLE.get()
    return table[0] if table is not None else key()


def epoch_table():
    table = _KEY_TABLE.get()
    if table is not None:
        return table[1]
    return tuple((e["after"], bytes.fromhex(e["material"])) for e in epochs())


def key() -> bytes:
    """Persist the original derivation once, before any session-key rotation.

    Keeping the original bytes migrates existing rows without rewriting history.
    Subsequent session rotations never change the audit key.
    """
    path = config.DATA_DIR / ".audit_key"
    try:
        value = _stable_bytes(path).decode("ascii").strip()
    except FileNotFoundError:
        if (config.DATA_DIR / ".audit_head").exists() or (
            config.DATA_DIR / ".audit_epochs"
        ).exists():
            raise TrustStateError("Audit key missing; restore matching trust files") from None
        value, _ = config._keyfile(
            ".audit_key",
            lambda: hashlib.sha256(("loc-audit:" + config.SESSION_SECRET).encode()).hexdigest(),
        )
    result = bytes.fromhex(value)
    if len(result) != 32:
        raise ValueError("Invalid audit keyfile")
    return result


def epochs():
    path = config.DATA_DIR / ".audit_epochs"
    try:
        return json.loads(_stable_bytes(path))
    except FileNotFoundError:
        return []


def row_key(row_id=None):
    """Select a key by a filesystem-pinned epoch boundary, never DB-controlled metadata."""
    selected = base_key()
    for after, material in epoch_table():
        if row_id is None or row_id > after:
            selected = material
    return selected


def new_epoch():
    material = secrets.token_bytes(32)
    return material, hashlib.sha256(material).hexdigest()


def legacy_through(head_id):
    """Pin the last legacy id before any v2 write, outside DB attacker control.

    Call under the audit chain lock, with stopped competing writer processes on
    upgrade (the same maintenance constraint as key rotation). Never advance this
    boundary, including during reset-anchor or subsequent verification.
    """
    path = config.DATA_DIR / ".audit_format"
    try:
        raw = _stable_bytes(path)
    except FileNotFoundError:
        _replace(".audit_format", json.dumps({"version": 2, "legacy_through": head_id}).encode())
        raw = _stable_bytes(path)
    state = json.loads(raw)
    if (
        not isinstance(state, dict)
        or state.get("version") != 2
        or type(state.get("legacy_through")) is not int
        or not 0 <= state["legacy_through"] < 2**63
    ):
        raise TrustStateError("Invalid audit format boundary; restore matching trust files")
    return state["legacy_through"]


def save_epoch(after, material):
    entries = epochs()
    entries.append({"after": after, "material": material.hex()})
    _replace(".audit_epochs", json.dumps(entries).encode())


def _head_signature(path):
    return _signature(os.stat(path))


def _identity(name):
    st = os.stat(name)
    return (st.st_mtime_ns, st.st_size, st.st_ino)


def _remember_head(path, state, identity, signing):
    """Remember the witness this process just published (called under the process lock).

    ``identity`` was taken from our temp file BEFORE the atomic replace (rename keeps mtime,
    size and inode). If the published path no longer shows it, someone else replaced the file
    in between, so nothing is remembered and the next write re-reads and re-checks it.
    """
    try:
        signature = _head_signature(path)
        if (signature[0], signature[2], signature[3]) != identity:
            _HEAD.pop(path, None)
            return
        _HEAD[path] = (signature, signing, dict(state))
    except OSError:
        _HEAD.pop(path, None)


def read(*, trust_own_publish=False):
    """None before initialization; malformed state fails closed at the caller.

    ``trust_own_publish`` is for the audit write path only (#618): when the file on disk is
    byte-for-byte the witness this process last published or verified (same mtime, size,
    inode, device and signing key), the parsed state is reused instead of re-opening and
    re-MACing it. Re-opening the file this process replaced a moment ago cost about 6 ms per
    row on Windows. Any other process's publish, an edit, a restore or a deletion changes the
    signature and falls through to the full read and MAC check. Verification and every
    maintenance command keep the default and always read the file.
    """
    path = config.DATA_DIR / ".audit_head"
    try:
        signature = _head_signature(path)
    except FileNotFoundError:
        _HEAD.pop(path, None)
        return _PENDING.get(path, (None, None))[1]
    hit = _HEAD.get(path) if trust_own_publish and _CACHE_OK.get() else None
    if hit is not None and hit[0] == signature and hit[1] == base_key():
        state = dict(hit[2])
    else:
        envelope = json.loads(path.read_bytes())
        if not isinstance(envelope, dict) or not isinstance(envelope.get("body"), str):
            raise ValueError("Invalid audit head envelope")
        body = envelope["body"]
        expected = hmac.new(base_key(), body.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(expected, envelope["mac"]):
            raise ValueError("Invalid audit head MAC")
        state = json.loads(body)
        if (
            not isinstance(state, dict)
            or type(state.get("id")) is not int
            or not 0 < state["id"] < 2**63
            or not isinstance(state.get("hash"), str)
        ):
            raise ValueError("Invalid audit head id")
        _HEAD[path] = (signature, base_key(), dict(state))
    state = _PENDING.get(path, (None, state))[1]
    if state.get("epochs", 0) > len(epoch_table()):
        raise TrustStateError("Audit epochs missing or inconsistent; restore matching trust files")
    return state


def write(state, *, required=False):
    """Atomically replace durable private state; callers serialize with the chain lock."""
    body = json.dumps(state, sort_keys=True)
    signing = base_key()
    envelope = json.dumps(
        {
            "body": body,
            "mac": hmac.new(signing, body.encode(), hashlib.sha256).hexdigest(),
        }
    ).encode()
    global _GENERATION
    _GENERATION += 1
    path = config.DATA_DIR / ".audit_head"
    updates = _BUFFER.get()
    if updates is not None:
        _PENDING[path] = (_GENERATION, state)
        updates.append((path, _GENERATION, envelope, state, signing))
    else:
        try:
            identity = _replace(".audit_head", envelope)
            _remember_head(path, state, identity, signing)
        except OSError as exc:
            _publish_warning(exc)
            if required:
                raise ValueError("Audit witness write deferred; retry maintenance command") from exc
            return
        _PUBLISHED[path] = _GENERATION
        _PENDING.pop(path, None)


def _publish_warning(exc):
    _logger.warning(
        "Audit witness publish failed (%s); witness trails the DB; next write republishes.",
        type(exc).__name__,
    )


def _prepare(content):
    config.private_directory(config.DATA_DIR)
    fd, name = tempfile.mkstemp(prefix=".audit_head-", dir=config.DATA_DIR)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        return name
    except BaseException:
        if os.path.exists(name):
            os.unlink(name)
        raise


def _replace(filename, content):
    name = _prepare(content)
    try:
        identity = _identity(name)
        _publish(name, config.DATA_DIR / filename)
        return identity
    finally:
        if os.path.exists(name):
            os.unlink(name)


def _publish(name, path):
    """Allow a short-lived Windows reader to release its sharing lock."""
    for attempt in range(5):
        try:
            os.replace(name, path)
            return
        except PermissionError:
            if attempt == 4:
                raise
            time.sleep(0.01)


_UNREAD = object()


def advance(head_id, head_hash, *, allow_regress=False, broken=False, prior=_UNREAD):
    prior = None if allow_regress else read(trust_own_publish=True) if prior is _UNREAD else prior
    if prior and not allow_regress and (prior["id"] >= head_id or prior.get("broken")):
        if broken and not prior.get("broken"):
            write({**prior, "broken": True})
        return
    write(
        {"id": head_id, "hash": head_hash, "broken": broken, "epochs": len(epoch_table())},
        required=allow_regress,
    )
