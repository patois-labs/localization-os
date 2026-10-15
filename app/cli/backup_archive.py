"""Full-install backup archive creation (gap G-06).

Owns SQLite snapshots, PostgreSQL dumps, upload/keyfile collection, and archive
creation. Verification and shared contracts live in backup_common; restore and
rollback live in backup_restore.

Transitional compatibility shim for one release, re-exporting these names:
    _EXTRACT_SLACK_BYTES, _PRE_RESTORE_SUFFIX, _STAGING_PREFIX
    _STALE_LEFTOVER_AGE_S, _UPLOAD_SKIP_SUFFIXES, DB_ARC_PG, DB_ARC_SQLITE
    KEYFILE_NAMES, KEYFILES_PREFIX, MANIFEST_NAME, MANIFEST_SCHEMA, SQLITE_MAGIC
    SUPPORTED_MANIFEST_SCHEMAS, UPLOADS_PREFIX, BackupError, _pg_connection_args
    _pid_is_alive, _read_manifest, _verify_postgres_member, _verify_sqlite_member
    app_version, branding, hashlib, is_full_archive, server_appears_running
    sha256_file, socket, sys, time, verify_archive, _absolute_extract_ceiling
    _clear_leftover, _older_than, _pg_restore_hint, _rm, _rollback, _safe_extractall
    _set_restore_status, _swap_dir, _swap_file, _sweep_restore_leftovers
    restore_archive

2026-09-14: re-exports are read-compatible only; monkeypatch the owning module
(`backup_restore` / `backup_common`).

History: full-install backup/restore design.
Documented in: docs/architecture/backups-and-dr.md
"""

from __future__ import annotations

import json
import os
import re
import shutil
import sqlite3
import tarfile
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from app.cli._common import same_file
from app.cli.backup_common import (
    _EXTRACT_SLACK_BYTES,
    _PRE_RESTORE_SUFFIX,
    _STAGING_PREFIX,
    _STALE_LEFTOVER_AGE_S,
    _UPLOAD_SKIP_SUFFIXES,
    DB_ARC_PG,
    DB_ARC_SQLITE,
    KEYFILE_NAMES,
    KEYFILES_PREFIX,
    MANIFEST_NAME,
    MANIFEST_SCHEMA,
    SQLITE_MAGIC,
    SUPPORTED_MANIFEST_SCHEMAS,
    UPLOADS_PREFIX,
    BackupError,
    _pg_connection_args,
    _pid_is_alive,
    _read_manifest,
    _verify_postgres_member,
    _verify_sqlite_member,
    app_version,
    branding,
    hashlib,
    is_full_archive,
    server_appears_running,
    sha256_file,
    socket,
    sys,
    verify_archive,
)
from app.cli.backup_restore import (
    _absolute_extract_ceiling,
    _clear_leftover,
    _older_than,
    _pg_restore_hint,
    _rm,
    _rollback,
    _safe_extractall,
    _set_restore_status,
    _swap_dir,
    _swap_file,
    _sweep_restore_leftovers,
    restore_archive,
    time,
)


# --- creation ----------------------------------------------------------------
def _snapshot_sqlite(dst_file: str) -> None:
    """Write a single consistent snapshot of the live SQLite DB to dst_file using the
    online .backup() API - a transactionally-consistent point-in-time copy that folds
    in the WAL, so there is NO manual checkpoint to race a concurrent reader (gate
    finding 1: the old SQLCipher branch raw-copied ciphertext after a checkpoint that
    silently returned BUSY, losing committed rows / producing a malformed snapshot).

    Plaintext DB: stdlib sqlite3 .backup(). SQLCipher DB: sqlcipher3's .backup() from a
    keyed source into a keyed destination, so the snapshot stays encrypted at rest and
    is complete. Both raise on any failure - a backup is never silently partial."""
    from app import config

    src = str(config.DB_PATH)
    if config.DB_ENCRYPTION_ON:
        from app.kernel import dbcrypt

        src_conn = dbcrypt.connect(src)
        try:
            out_conn = dbcrypt.connect(dst_file)
            try:
                src_conn.backup(out_conn)
            finally:
                out_conn.close()
        finally:
            src_conn.close()
        return
    conn = sqlite3.connect(src)
    try:
        out = sqlite3.connect(dst_file)
        try:
            conn.backup(out)
        finally:
            out.close()
    finally:
        conn.close()


def _dump_postgres(dst_file: str) -> str | None:
    """pg_dump -Fc the configured Postgres DB to dst_file. Raises a credential-safe
    BackupError when pg_dump is not on PATH. The password is passed via
    the PGPASSWORD env, NEVER in argv (gate finding 12: a DB_URL in argv is visible in
    `ps`); host/port/db/user go as flags.

    Returns the ``pg_dump --version`` banner when it is readable, else None. LOW-3: the
    banner is a COSMETIC provenance field recorded in the manifest -- once the dump itself
    has been written, an unreadable/nonstandard banner (a vendor build, a wrapper, a
    localized binary) must NOT turn a good backup into no backup. An absent banner is
    recorded as None and verify reports UNVERIFIED, rather than losing the archive."""
    import subprocess

    from app import config

    if not shutil.which("pg_dump"):
        from app.db_url import redact_db_url

        raise BackupError(
            "Postgres backend but pg_dump is not on PATH. Install the Postgres client "
            "tools for " + redact_db_url(config.DB_URL) + ". Supply the password via "
            "PGPASSWORD or ~/.pgpass."
        )
    flags, password = _pg_connection_args(config.DB_URL)
    env = dict(os.environ)
    argv = ["pg_dump", "-Fc", *flags]
    if password:
        env["PGPASSWORD"] = password
    # GitHub #544: pg_dump writes to a private temp sibling in the destination dir and is
    # renamed into place ONLY on success (os.replace), the same way create_archive stages
    # its tarball. The old code opened dst_file "wb" before pg_dump ran, so a `db backup
    # PATH --force` whose dump failed (DB down, auth error, disk full) truncated the last
    # good dump to a 0-byte/partial file with no cleanup. On any failure the temp sibling
    # is unlinked and the previous dump survives untouched.
    dst_abs = os.path.abspath(dst_file)
    fd, tmp_dump = tempfile.mkstemp(
        dir=os.path.dirname(dst_abs), prefix=os.path.basename(dst_abs) + ".", suffix=".partial"
    )
    try:
        with os.fdopen(fd, "wb") as out:
            proc = subprocess.run(argv, stdout=out, stderr=subprocess.PIPE, env=env)  # noqa: S603 -- fixed pg_dump argv, no shell
        if proc.returncode != 0:
            raise BackupError("pg_dump failed (nonzero exit).")
        os.replace(tmp_dump, dst_file)
    except BaseException:
        # never strand a 0-byte/partial sibling where the last good dump was
        try:
            if os.path.exists(tmp_dump):
                os.unlink(tmp_dump)
        except OSError:
            pass
        raise
    # The dump is on disk and renamed into place. Read the banner best-effort; never
    # raise past this point.
    banner = None
    try:
        version = subprocess.run(["pg_dump", "--version"], capture_output=True, text=True)  # noqa: S607 -- pg_dump from PATH by design
        candidate = version.stdout.strip()
        if not version.returncode and re.fullmatch(r"pg_dump \(PostgreSQL\) [^\r\n]+", candidate):
            banner = candidate
    except OSError:
        banner = None
    return banner


def _iter_uploads(uploads_dir: Path):
    """Yield (abs_path, arcname) for every file under uploads_dir, skipping scratch
    suffixes. Empty when the dir is absent (a fresh install with no uploads yet)."""
    if not uploads_dir.is_dir():
        return
    for root, _dirs, files in os.walk(uploads_dir):
        for name in sorted(files):
            if name.endswith(_UPLOAD_SKIP_SUFFIXES):
                continue
            abs_path = Path(root) / name
            rel = abs_path.relative_to(uploads_dir).as_posix()
            yield str(abs_path), UPLOADS_PREFIX + rel


def _keyfile_filter(tarinfo: tarfile.TarInfo) -> tarfile.TarInfo:
    """Force keyfile members to 0600 and zero ownership metadata, so key material is
    never widened by a platform whose filesystem mode bits are synthetic (Windows)
    and the archive is reproducible."""
    tarinfo.mode = 0o600
    tarinfo.uid = tarinfo.gid = 0
    tarinfo.uname = tarinfo.gname = ""
    return tarinfo


def create_archive(dst_path: str, *, force: bool) -> dict:
    """Build the full-install backup archive at dst_path and return its manifest dict.

    Raises BackupError on any hard failure (dst exists w/o force, missing DB, pg_dump
    absent). The returned manifest includes `secrets_included` so the caller can print
    the right warning. The archive is written to a private (0600) temp sibling and
    renamed into place, and every created ancestor dir is 0700, so the secret-bearing bytes
    are private from the first byte (GitHub #397)."""
    from app import config

    dst = Path(dst_path)
    if dst.exists() and not force:
        raise BackupError(f"destination exists: {dst} (pass --force to overwrite).")
    # gate finding 8 / probe p8: the parent may not exist (the runbook's own Fly recipe
    # writes to /data/backups/...). Create it (0700 when we make it) instead of a raw
    # FileNotFoundError to the operator. Private-at-creation: the mode rides on the mkdir
    # call (not a create-then-chmod) so the new dir never exists world/group-accessible
    # under a normal umask; the chmod is belt-and-braces for a strict umask. Existing
    # parents are deliberately left unchanged (the operator set their mode).
    parent = dst.parent
    missing = []
    ancestor = parent
    while not ancestor.exists():
        if ancestor.parent == ancestor:
            raise BackupError(f"destination root does not exist: {ancestor}")
        missing.append(ancestor)
        ancestor = ancestor.parent
    if not ancestor.is_dir():
        raise BackupError(f"cannot create backup directory {parent}: {ancestor} is not a directory")
    for directory in reversed(missing):
        try:
            os.mkdir(directory, 0o700)
        except FileExistsError:
            if not directory.is_dir():
                raise BackupError(
                    f"cannot create backup directory {directory}: path is not a directory"
                ) from None
            continue  # Another creator owns this directory and its permissions.
        except OSError as exc:
            raise BackupError(f"cannot create backup directory {directory}: {exc}") from exc
        try:  # noqa: SIM105
            os.chmod(directory, 0o700)
        except OSError:
            pass

    backend = "sqlite" if config.IS_SQLITE else "postgres"
    if backend == "sqlite":
        src_db = str(config.DB_PATH)
        if not os.path.isfile(src_db):
            raise BackupError(f"database file not found: {src_db}")
        if same_file(src_db, dst):
            raise BackupError("archive destination must differ from the live DB path.")

    members: list[dict] = []
    keyfiles_included: list[str] = []

    with tempfile.TemporaryDirectory(prefix="loctms_backup_") as stage:
        stage_p = Path(stage)
        # Capture trust BEFORE the DB snapshot: a hot-backup witness may safely trail
        # the DB head, but must never lead it. Key rotation requires stopped writers.
        keys_staged = stage_p / "keyfiles"
        keys_staged.mkdir()
        for name in KEYFILE_NAMES:
            source = config.DATA_DIR / name
            if source.is_file():
                shutil.copyfile(source, keys_staged / name)
        if (keys_staged / ".audit_key").exists() and not (keys_staged / ".audit_epochs").exists():
            (keys_staged / ".audit_epochs").write_bytes(b"[]")
        # --- DB member (staged, so its sha256 is over the snapshot we archive) ----
        db_arc = DB_ARC_SQLITE if backend == "sqlite" else DB_ARC_PG
        db_staged = stage_p / db_arc
        db_staged.parent.mkdir(parents=True, exist_ok=True)
        if backend == "sqlite":
            _snapshot_sqlite(str(db_staged))
        else:
            pg_dump_version = _dump_postgres(str(db_staged))
        members.append(
            {
                "path": db_arc,
                "kind": "db",
                "bytes": db_staged.stat().st_size,
                "sha256": sha256_file(db_staged),
            }
        )

        # --- uploads (added from disk; sha256 over the on-disk file) --------------
        upload_members: list[tuple[str, str]] = list(_iter_uploads(config.UPLOADS_DIR))
        for abs_path, arc in upload_members:
            members.append(
                {
                    "path": arc,
                    "kind": "upload",
                    "bytes": os.path.getsize(abs_path),
                    "sha256": sha256_file(abs_path),
                }
            )

        # --- keyfiles (only those persisted on disk; env/KMS secrets aren't here) --
        keyfile_members: list[tuple[str, str]] = []
        for name in KEYFILE_NAMES:
            p = keys_staged / name
            if p.is_file() and p.stat().st_size > 0:
                arc = KEYFILES_PREFIX + name
                keyfile_members.append((str(p), arc))
                keyfiles_included.append(name)
                members.append(
                    {
                        "path": arc,
                        "kind": "keyfile",
                        "bytes": p.stat().st_size,
                        "sha256": sha256_file(p),
                    }
                )

        manifest = {
            "manifest_schema": MANIFEST_SCHEMA,
            "created_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "app_version": app_version(),
            "backend": backend,
            "db_encryption": "sqlcipher" if config.DB_ENCRYPTION_ON else "",
            "data_dir": str(config.DATA_DIR),
            "db_member": db_arc,
            "keyfiles_included": keyfiles_included,
            "secrets_included": bool(keyfiles_included),
            "session_secret_source": config.SESSION_SECRET_SOURCE,
            "encryption_key_source": config.ENCRYPTION_KEY_SOURCE,
            "tool": "loc-tms db backup --full",
            "members": members,
        }
        if backend == "postgres":
            # dump_size is always recorded (schema 2); the pg_dump version banner only when
            # it was readable (LOW-3) -- an absent banner leaves the key out and verify warns.
            manifest["dump_size"] = db_staged.stat().st_size
            if pg_dump_version:
                manifest["pg_dump_version"] = pg_dump_version
        manifest_staged = stage_p / MANIFEST_NAME
        manifest_staged.write_text(json.dumps(manifest, indent=2), encoding="utf-8")

        # --- write the tarball into a private temp sibling, then rename ------------
        # mkstemp creates the sibling 0600 atomically (O_CREAT|O_EXCL) under an
        # unpredictable name, so the secret-bearing archive is private from the first
        # byte. The old predictable `<dst>.partial` written by tarfile.open (mode
        # 0666 & ~umask) then chmod'd after close left it group/other-readable for a
        # normal 022 umask throughout creation, and a sibling account in the same backup
        # dir could open it and hold the fd past the chmod (GitHub #397). Passing the
        # already-open file object keeps tarfile from re-opening it under a wider mode.
        fd, tmp_out = tempfile.mkstemp(dir=str(parent), prefix=dst.name + ".", suffix=".partial")
        try:
            with os.fdopen(fd, "wb") as raw, tarfile.open(fileobj=raw, mode="w:gz") as tar:
                tar.add(manifest_staged, arcname=MANIFEST_NAME)
                tar.add(db_staged, arcname=db_arc)
                for abs_path, arc in upload_members:
                    tar.add(abs_path, arcname=arc)
                for abs_path, arc in keyfile_members:
                    tar.add(abs_path, arcname=arc, filter=_keyfile_filter)
            # raw is closed here; the 0600 sibling is renamed to dst so the final file
            # is private the instant it appears (the archive can hold .enc_key in the clear).
            os.replace(tmp_out, dst)
        finally:
            if os.path.exists(tmp_out):
                try:  # noqa: SIM105
                    os.unlink(tmp_out)
                except OSError:
                    pass
    try:  # noqa: SIM105 - belt-and-braces after the rename
        os.chmod(dst, 0o600)
    except OSError:
        pass
    return manifest


__all__ = [
    "DB_ARC_PG",
    "DB_ARC_SQLITE",
    "KEYFILES_PREFIX",
    "KEYFILE_NAMES",
    "MANIFEST_NAME",
    "MANIFEST_SCHEMA",
    "SQLITE_MAGIC",
    "SUPPORTED_MANIFEST_SCHEMAS",
    "UPLOADS_PREFIX",
    "_EXTRACT_SLACK_BYTES",
    "_PRE_RESTORE_SUFFIX",
    "_STAGING_PREFIX",
    "_STALE_LEFTOVER_AGE_S",
    "_UPLOAD_SKIP_SUFFIXES",
    "BackupError",
    "_absolute_extract_ceiling",
    "_clear_leftover",
    "_older_than",
    "_pg_connection_args",
    "_pg_restore_hint",
    "_pid_is_alive",
    "_read_manifest",
    "_rm",
    "_rollback",
    "_safe_extractall",
    "_set_restore_status",
    "_swap_dir",
    "_swap_file",
    "_sweep_restore_leftovers",
    "_verify_postgres_member",
    "_verify_sqlite_member",
    "app_version",
    "branding",
    "create_archive",
    "hashlib",
    "is_full_archive",
    "restore_archive",
    "server_appears_running",
    "sha256_file",
    "socket",
    "sys",
    "time",
    "verify_archive",
]
