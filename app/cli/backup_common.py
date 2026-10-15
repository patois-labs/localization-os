"""Leaf common module for backup and restore operations.

Owns:
  * BackupError and common exceptions
  * Archive constants and manifest schema definitions
  * Manifest reader and full archive detection
  * Process liveness and server running probes
  * Database member verification routines (SQLite and PostgreSQL)

Documented in: docs/architecture/backups-and-dr.md
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import socket
import sqlite3
import sys
import tarfile
import tempfile
from pathlib import Path

from app import branding

MANIFEST_NAME = "manifest.json"
# Schema 2 (P2a recovery) records dump_size + the pg_dump version banner for Postgres
# archives. Schema 1 archives (written by any earlier release) carry neither and are
# still ACCEPTED at verify time: their absent dump metadata is reported as an advisory
# `warn`, not a `fail`, so the DEPLOY.md failed-upgrade recovery -- a checkpoint captured
# by the OLD image, restored by the NEW one -- succeeds instead of being refused.
MANIFEST_SCHEMA = 2
SUPPORTED_MANIFEST_SCHEMAS = (1, 2)
DB_ARC_SQLITE = "db/loc_tms.db"
DB_ARC_PG = "db/pg_dump.dump"
UPLOADS_PREFIX = "uploads/"
KEYFILES_PREFIX = "keyfiles/"
SQLITE_MAGIC = b"SQLite format 3\x00"  # first 16 bytes of every plaintext SQLite DB
# The two config keyfiles (app/config.py _secret): the Fernet encryption key and the
# session-signing secret. `.protected` is deliberately EXCLUDED - it is a local
# server guard (issue #13) that run.py re-creates, not data, and carrying it into a
# restore would surprise an operator by locking the restored dir.
KEYFILE_NAMES = (
    ".enc_key",
    ".session_secret",
    ".audit_key",
    ".audit_epochs",
    ".audit_head",
    ".audit_format",
)
# uploads members matching these are skipped as transient scratch (never real data).
_UPLOAD_SKIP_SUFFIXES = (".tmp", ".part")
# Slack over the manifest's declared total, allowed during extraction before the
# decompression-bomb guard trips. verify already refuses any unlisted/oversized
# member, so this is defense-in-depth (esp. on the documented --skip-verify path).
_EXTRACT_SLACK_BYTES = 16 * 1024 * 1024
# A staging dir with an UNPARSEABLE pid is swept only once it is older than this,
# protecting potentially live restore artifacts (RES-1/RES-2). Recovery copies
# (*.pre-restore) are never age-swept.
_STALE_LEFTOVER_AGE_S = 600  # 10 minutes
_STAGING_PREFIX = ".restore-staging-"
_PRE_RESTORE_SUFFIX = ".pre-restore"


class BackupError(Exception):
    """A clean, expected backup/restore/verify failure (mapped to a CliError by the
    command layer). Carries no traceback."""


def _pid_is_alive(pid: int) -> bool:
    """Dependency-free liveness check for a restore's owning process. CONSERVATIVE: an
    inconclusive check returns True (assume alive), so the leftover sweep errs toward
    KEEPING a possibly-live sibling's dir rather than deleting it.

    win32: `os.kill(pid, 0)` on Windows TERMINATES the process (it maps to
    TerminateProcess for non-CTRL signals) - so we must NOT use it. Instead OpenProcess
    via ctypes and WaitForSingleObject(0): WAIT_TIMEOUT => still running, WAIT_OBJECT_0
    => exited. ERROR_ACCESS_DENIED means the process EXISTS (alive); ERROR_INVALID_
    PARAMETER means no such pid (dead). POSIX: os.kill(pid, 0) - ProcessLookupError =>
    dead, PermissionError => alive-but-not-ours."""
    if pid <= 0:
        return False
    if sys.platform == "win32":
        try:
            import ctypes
            from ctypes import wintypes

            SYNCHRONIZE = 0x00100000
            ERROR_INVALID_PARAMETER = 87
            WAIT_OBJECT_0 = 0x0
            k32 = ctypes.windll.kernel32
            k32.OpenProcess.restype = wintypes.HANDLE
            handle = k32.OpenProcess(SYNCHRONIZE, False, pid)
            if not handle:
                # no handle: dead only if the failure is "no such pid"; anything else
                # (e.g. access denied) means it EXISTS -> treat as alive.
                return k32.GetLastError() != ERROR_INVALID_PARAMETER
            try:
                # 0ms wait: WAIT_OBJECT_0 (signaled) => the process has exited.
                return k32.WaitForSingleObject(handle, 0) != WAIT_OBJECT_0
            finally:
                k32.CloseHandle(handle)
        except Exception:
            return True  # cannot tell -> assume alive (never delete a live sibling)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return True
    return True


def app_version() -> str:
    from app.release import app_version as read_version

    return read_version()


def sha256_file(path) -> str:
    """Streaming sha256 hex digest of a file (bounded memory on a large upload/DB)."""
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def server_appears_running() -> tuple[bool, str]:
    """Best-effort "is the app serving this host" probe: a short TCP connect to
    config.HOST:PORT. Returns (running, "host:port"). If nothing is listening we
    proceed; a false positive (something ELSE on the port) errs safe by refusing a
    destructive restore. This is the "if detectable" guard the spec asks for - it is
    intentionally conservative (it CANNOT see a server bound to a different host/port),
    so the runbook + the CLI both tell the operator to stop the server themselves."""
    from app import config

    host = config.HOST
    if host in ("0.0.0.0", "::", ""):  # noqa: S104 -- host comparison, not a bind
        host = "127.0.0.1"
    where = f"{host}:{config.PORT}"
    try:
        with socket.create_connection((host, config.PORT), timeout=0.5):
            return True, where
    except OSError:
        return False, where


def _pg_connection_args(db_url: str) -> tuple[list[str], str | None]:
    """Parse the same SQLAlchemy URL the app accepts; never render parse errors."""
    from sqlalchemy.engine import make_url

    try:
        parts = make_url(db_url)
    except Exception:
        raise BackupError("Invalid PostgreSQL connection configuration.") from None

    # MED-2: emit a flag ONLY for a component the URL actually supplies. Inventing
    # localhost/5432/postgres defaults would silently redirect a socket- or env-driven
    # install (postgresql+psycopg:///db, ?host=/var/run/postgresql) to TCP localhost for
    # both the real dump AND the printed restore recipe. An omitted flag lets libpq honor
    # PGHOST/PGPORT/PGUSER/PGDATABASE or the default unix socket, exactly as before.
    #
    # A socket directory / non-default port supplied in the URL QUERY (libpq's own
    # `?host=/var/run/postgresql&port=5433` form, which SQLAlchemy parses into parts.query,
    # NOT parts.host/parts.port) IS honored -- otherwise the code would drop the operator's
    # configured socket target and libpq would silently fall back to the default socket, the
    # exact silent redirect this helper exists to prevent (and which its own comment above
    # names as covered).
    def _q(key: str) -> str | None:
        val = parts.query.get(key)
        if isinstance(val, (tuple, list)):
            val = val[0] if val else None
        return val if isinstance(val, str) and val else None

    argv: list[str] = []
    host = parts.host or _q("host")
    if host:
        argv += ["-h", host]
    port = parts.port or _q("port")
    if port:
        argv += ["-p", str(port)]
    if parts.username:
        argv += ["-U", parts.username]
    if parts.database:
        argv += ["-d", parts.database]
    return argv, parts.password


# --- verification ------------------------------------------------------------
def _read_manifest(tar: tarfile.TarFile) -> dict:
    try:
        fh = tar.extractfile(MANIFEST_NAME)
    except KeyError:
        fh = None
    if fh is None:
        raise BackupError(
            f"archive has no {MANIFEST_NAME} - not a {branding.brand_name()} full backup."
        )
    try:
        manifest = json.loads(fh.read().decode("utf-8"))
    except (ValueError, UnicodeDecodeError) as exc:
        raise BackupError(f"{MANIFEST_NAME} is not valid JSON: {exc}") from exc
    if not isinstance(manifest, dict) or "members" not in manifest:
        raise BackupError(f"{MANIFEST_NAME} is missing required fields.")
    return manifest


def is_full_archive(path: str) -> bool:
    """True iff path is a tar.gz carrying our manifest.json. Cheap discriminator so
    `db restore` can auto-route a full archive vs a legacy single-file DB backup
    without a new flag. Never raises."""
    try:
        if not tarfile.is_tarfile(path):
            return False
        with tarfile.open(path, "r:*") as tar:
            return MANIFEST_NAME in tar.getnames()
    except (OSError, tarfile.TarError):
        return False


def verify_archive(path: str) -> tuple[bool, list[dict]]:
    """Open the archive and run the restore-verification control. Returns
    (ok, checks) where each check is {check,status,detail} and status is
    ok/fail/warn/skip. ok is False on failure or unverified warning. Checks:

      1. manifest present + parseable + schema understood
      2. NO unlisted members: every file member (except manifest.json) is in the
         manifest (gate finding 4 / probe p2#7: an injected unlisted payload was
         previously accepted + extracted on restore)
      3. every manifest member present as a REGULAR FILE with matching size + sha256
         (a manifest that names a directory member no longer raises - finding 4)
      4. integrity_check on a temp copy of the contained DB: plaintext via sqlite3,
         SQLCipher via a KEYED connection (the key is available in-process) rather
         than skipping (gate finding 1); pg_dump member -> pg_restore --list + metadata
      5. every keyfile the manifest claims is present + non-empty
    """
    checks: list[dict] = []

    def add(name: str, status: str, detail: str) -> None:
        checks.append({"check": name, "status": status, "detail": detail})

    if not os.path.isfile(path):
        add("archive_exists", "fail", f"no such file: {path}")
        return False, checks
    if not tarfile.is_tarfile(path):
        add("archive_format", "fail", "not a tar archive (expected a .tar.gz backup).")
        return False, checks

    with tarfile.open(path, "r:*") as tar:
        members_ti = tar.getmembers()
        names = {ti.name for ti in members_ti}
        try:
            manifest = _read_manifest(tar)
        except BackupError as exc:
            add("manifest", "fail", str(exc))
            return False, checks

        schema = manifest.get("manifest_schema")
        if schema not in SUPPORTED_MANIFEST_SCHEMAS:
            add("manifest_schema", "fail", f"unsupported manifest_schema {schema!r}.")
            return False, checks
        add("manifest", "ok", f"schema {schema}, app {manifest.get('app_version')}.")

        members = manifest.get("members") or []
        if not members:
            add("members", "fail", "manifest lists no members.")
        listed = {m.get("path") for m in members} | {MANIFEST_NAME}

        # 2. no unlisted members of ANY kind (a legit archive emits only the listed
        # regular files - no dir/link/device entries - so ANYTHING unlisted, incl. an
        # injected payload, a decompression bomb, or a symlink escape, is a failure)
        for ti in members_ti:
            if ti.name not in listed:
                add(
                    f"unlisted:{ti.name}",
                    "fail",
                    "present in the archive but NOT listed in the manifest.",
                )

        # 3. per-member presence + regular-file + size + sha256
        for m in members:
            mpath = m.get("path")
            label = f"member:{mpath}"
            if mpath not in names:
                add(label, "fail", "listed in manifest but absent from the archive.")
                continue
            ti = tar.getmember(mpath)
            if not ti.isfile():
                add(label, "fail", "listed member is not a regular file (dir/link/other).")
                continue
            if ti.size != m.get("bytes"):
                add(label, "fail", f"size {ti.size} != manifest {m.get('bytes')}.")
                continue
            fh = tar.extractfile(mpath)
            if fh is None:
                add(label, "fail", "member could not be read as a regular file.")
                continue
            digest = hashlib.sha256()
            for chunk in iter(lambda fh=fh: fh.read(1024 * 1024), b""):
                digest.update(chunk)
            if digest.hexdigest() != m.get("sha256"):
                add(label, "fail", "member does not match the manifest (corruption/truncation).")
            else:
                add(label, "ok", f"{m.get('kind')} {ti.size} bytes, sha256 verified.")

        # 4. integrity of the contained DB
        db_member = manifest.get("db_member")
        backend = manifest.get("backend")
        if db_member not in names:
            add("db_integrity", "fail", "DB member missing from the archive.")
        elif backend == "postgres":
            _verify_postgres_member(tar, db_member, manifest, add)
        else:
            _verify_sqlite_member(tar, db_member, manifest, add)

        # 5. keyfiles present + non-empty
        claimed = manifest.get("keyfiles_included") or []
        if not claimed:
            add(
                "keyfiles",
                "skip",
                "no keyfiles in this archive (secrets are env/KMS-sourced - back "
                "those up separately).",
            )
        for name in claimed:
            arc = KEYFILES_PREFIX + name
            if arc not in names:
                add(f"keyfile:{name}", "fail", "claimed by manifest but absent.")
            elif tar.getmember(arc).size <= 0:
                add(f"keyfile:{name}", "fail", "present but EMPTY (unusable key material).")
            else:
                add(f"keyfile:{name}", "ok", f"{tar.getmember(arc).size} bytes.")

    ok = all(c["status"] not in {"fail", "warn"} for c in checks)
    return ok, checks


def _verify_postgres_member(tar, db_member, manifest, add) -> None:
    import subprocess

    member = tar.getmember(db_member)
    if not member.isfile() or member.size < 27:
        add("db_integrity", "fail", "truncated pg_dump custom-format header.")
        return
    # HIGH-3 + LOW-3 + LOW-4: a schema-1 archive (written by an earlier release) carries no
    # dump metadata, and a build whose pg_dump --version was unreadable records no banner.
    # The ONLY hard-fail metadata signal is a recorded dump SIZE that disagrees with the
    # archived member (truncation/tamper, below); every BANNER problem (absent, malformed,
    # or disagreeing with the dump's own TOC) is an advisory `warn` that continues to the
    # pg_restore --list validation without a version cross-check.
    banner = manifest.get("pg_dump_version")
    size = manifest.get("dump_size")
    # A recorded dump SIZE that disagrees with the archived member is a hard corruption /
    # truncation / tamper signal -> `fail`. This is the one metadata lane that fails.
    if size is not None and size != member.size:
        add("dump_metadata", "fail", "recorded dump size does not match the archived member.")
        return
    # LOW-4 (round 2): with the size matched (or unrecorded), the archived dump is intact, so
    # any BANNER problem -- absent, malformed, or (below) disagreeing with the dump's own TOC
    # -- is ADVISORY only. It coarsens the verdict to UNVERIFIED (a `warn`) but must NEVER
    # `fail` an already-written, restorable backup over a version string. Absent/malformed
    # here -> warn, then continue to the pg_restore --list validation without a cross-check.
    well_formed = bool(
        isinstance(banner, str)
        and banner
        and re.fullmatch(r"pg_dump \(PostgreSQL\) [^\r\n]+", banner)
    )
    if well_formed:
        pass  # well-formed banner: kept for the TOC cross-check below
    else:
        if not banner:
            detail = (
                "dump metadata not recorded (archive written by an earlier release)."
                if size is None
                else "pg_dump version not recorded; validated without a version cross-check."
            )
        else:
            detail = (
                "recorded pg_dump version banner is malformed; validated without a "
                "version cross-check."
            )
        add("dump_metadata", "warn", detail)
        banner = None
    fh = tar.extractfile(db_member)
    if fh.read(5) != b"PGDMP":
        add("db_integrity", "fail", "DB member is not a pg_dump custom-format dump.")
        return
    if not shutil.which("pg_restore"):
        add("db_integrity", "warn", "pg_restore not available: archive not validated")
        return
    with tempfile.TemporaryDirectory(prefix="loctms_pgverify_") as tmp:
        dump = Path(tmp) / "archive.dump"
        fh.seek(0)
        with dump.open("wb") as out:
            shutil.copyfileobj(fh, out)
        try:
            proc = subprocess.run(  # noqa: S603 -- fixed pg_restore argv, no shell
                ["pg_restore", "--list", str(dump)],  # noqa: S607 -- from PATH by design
                capture_output=True,
                text=True,
                timeout=120,
            )
        except (OSError, subprocess.TimeoutExpired):
            add("db_integrity", "fail", "pg_restore --list could not complete.")
            return
    toc = proc.stdout
    entries = re.findall(
        r"(?m)^\d+;\s+\d+\s+\d+\s+"
        r"(?:TABLE(?: DATA)?|SCHEMA|FUNCTION|SEQUENCE|VIEW|TYPE|INDEX|CONSTRAINT)\s+\S+",
        toc,
    )
    # pg_restore rejecting the dump or producing an empty/invalid TOC is a genuine integrity
    # failure -> `fail`. A recorded-banner-vs-TOC version DISAGREEMENT is NOT (LOW-4): the size
    # matched above so the dump is intact and restorable, so it is an advisory `dump_metadata`
    # warn, and db_integrity stays `ok`.
    if proc.returncode or not entries:
        add(
            "db_integrity",
            "fail",
            "pg_restore rejected the dump or produced an empty/invalid TOC.",
        )
        return
    add(
        "db_integrity",
        "ok",
        "pg_restore --list validated the TOC; a recovery drill must prove data restoration.",
    )
    if banner:
        version = re.search(r"Dumped by pg_dump version:\s*(\S+)", toc)
        if not (version and version[1] == banner.split()[2]):
            add(
                "dump_metadata",
                "warn",
                "recorded pg_dump version does not match the dump's TOC banner; "
                "validated without a version match.",
            )


def _archived_encryption_key(tar) -> str | None:
    """The ENCRYPTION_KEY stored in the archive's keyfiles/.enc_key, or None when the
    archive carries none (an env/KMS-sourced key is backed up separately)."""
    name = KEYFILES_PREFIX + ".enc_key"
    try:
        member = tar.getmember(name)
    except KeyError:
        return None
    if not member.isfile() or member.size > 4096:
        return None
    fh = tar.extractfile(member)
    value = (fh.read().decode("utf-8", "replace") if fh else "").strip()
    return value or None


def _sqlcipher_integrity_rows(tar, tmp_db):
    """integrity_check rows of a SQLCipher archive member, keyed with the ARCHIVED key
    first (#525): on a fresh host or after a key change the live ENCRYPTION_KEY does not
    open it, which left only --skip-verify (that also drops the sha256 and unlisted-member
    checks). Falls back to the live key (an env/KMS key the archive does not carry); the
    last error is raised if no candidate opens it."""
    from app import config
    from app.kernel import dbcrypt

    candidates: list[str | None] = []
    archived = _archived_encryption_key(tar)
    if archived:
        candidates.append(config.sqlcipher_hex_key(archived))
    candidates.append(None)  # the live key
    last: Exception | None = None
    for hex_key in candidates:
        try:
            conn = dbcrypt.connect(tmp_db, hex_key)
            try:
                return conn.execute("PRAGMA integrity_check").fetchall()
            finally:
                conn.close()
        except Exception as exc:
            last = exc
    raise last  # type: ignore[misc]


def _verify_sqlite_member(tar, db_member, manifest, add) -> None:
    """integrity_check the contained SQLite DB by extracting it to a temp file and
    opening it - KEYED via sqlcipher3 when the archive is SQLCipher (gate finding 1:
    the old code skipped integrity_check on the encrypted branch, so it passed a
    lossy/malformed snapshot). The archive's own keyfile key is tried first, then the live key."""
    encrypted = manifest.get("db_encryption") == "sqlcipher"
    with tempfile.TemporaryDirectory(prefix="loctms_vbk_") as tmp:
        tmp_db = os.path.join(tmp, "verify.db")
        fh = tar.extractfile(db_member)
        if fh is None:
            add("db_integrity", "fail", "DB member could not be read as a regular file.")
            return
        with open(tmp_db, "wb") as out:
            shutil.copyfileobj(fh, out)
        if os.path.getsize(tmp_db) < 100:
            add("db_integrity", "fail", "truncated database header.")
            return
        try:
            if encrypted:
                rows = _sqlcipher_integrity_rows(tar, tmp_db)
            else:
                conn = sqlite3.connect(tmp_db)
                try:
                    rows = conn.execute("PRAGMA integrity_check").fetchall()
                finally:
                    conn.close()
        except Exception as exc:
            how = "keyed SQLCipher" if encrypted else "SQLite"
            add("db_integrity", "fail", f"cannot open the contained {how} DB: {exc}")
            return
        vals = [str(r[0]) for r in rows]
        if vals == ["ok"]:
            kind = "keyed SQLCipher integrity_check" if encrypted else "PRAGMA integrity_check"
            add("db_integrity", "ok", f"{kind} = ok.")
        else:
            add("db_integrity", "fail", "; ".join(vals[:10]))


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
    "_pg_connection_args",
    "_pid_is_alive",
    "_read_manifest",
    "_verify_postgres_member",
    "_verify_sqlite_member",
    "app_version",
    "is_full_archive",
    "server_appears_running",
    "sha256_file",
    "verify_archive",
]
