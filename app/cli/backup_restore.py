"""Full-install backup archive restore and atomic filesystem rollback.

Split from app.cli.backup_archive as part of single-responsibility refactoring.
Owns:
  * restore_archive atomic restore execution
  * Staging extraction, filesystem swaps, and rollback mechanics
  * Secret-bearing leftover cleanup and stale restore recovery

Documented in: docs/architecture/backups-and-dr.md
"""

from __future__ import annotations

import os
import re
import shutil
import tarfile
import time
from pathlib import Path

from app import branding
from app.cli.backup_common import (
    _EXTRACT_SLACK_BYTES,
    _PRE_RESTORE_SUFFIX,
    _STAGING_PREFIX,
    _STALE_LEFTOVER_AGE_S,
    KEYFILE_NAMES,
    UPLOADS_PREFIX,
    BackupError,
    _pg_connection_args,
    _pid_is_alive,
    _read_manifest,
    is_full_archive,
    verify_archive,
)


def _sweep_restore_leftovers(data_dir) -> None:
    """Remove abandoned staging dirs, preserving live siblings' staging.

    Recovery copies are never swept: age cannot establish whether a displaced
    original belongs to a completed transaction or is still needed for recovery.
    """
    dd = Path(data_dir)
    now = time.time()
    for entry in dd.iterdir():
        name = entry.name
        try:
            if name.startswith(_STAGING_PREFIX):
                m = re.fullmatch(re.escape(_STAGING_PREFIX) + r"(\d+)", name)
                if m and _pid_is_alive(int(m.group(1))):
                    continue  # a live sibling owns it
                if not m and not _older_than(entry, now, _STALE_LEFTOVER_AGE_S):
                    continue  # unparseable pid, recent -> possibly in-flight
                shutil.rmtree(entry, ignore_errors=True)
        except OSError:
            pass


def _older_than(path: Path, now: float, age_s: int) -> bool:
    try:
        return (now - path.stat().st_mtime) > age_s
    except OSError:
        return False


# --- restore -----------------------------------------------------------------
def _set_restore_status(summary: dict, db_member: str | None, backend: str) -> None:
    summary["db_restore_required"] = not summary["db_restored"] and (
        backend == "postgres" or db_member is not None
    )
    summary["status"] = "partial" if summary["db_restore_required"] else "complete"


def restore_archive(path: str, *, dry_run: bool = False, skip_verify: bool = False) -> dict:
    """Restore a full archive onto this host's DATA_DIR (DESTRUCTIVE), ATOMICALLY.

    gate findings 2 + 3: the archive is VERIFIED (manifest + sha256 + integrity) BEFORE
    any destructive write (unless skip_verify, the documented desperate-recovery escape
    hatch), and the swap is staged - the whole archive is extracted into a sibling
    staging dir inside DATA_DIR, then each member is os.replace()d into place (keyfiles
    FIRST - the irreplaceable material) with the displaced original kept as
    `<name>.pre-restore`; any failure mid-swap attempts to roll the originals back.
    Rollback failures report the affected paths and preserve recovery copies.
    Only BackupError escapes.

    SQLite: the contained DB replaces config.DB_PATH (stale -wal/-shm removed). Postgres:
    the DB dump is extracted next to DATA_DIR and the pg_restore recipe is returned (DB
    restore is NOT automated - a named follow-up); uploads + keyfiles are restored
    automatically either way. Uploads are restored WHOLESALE (the tree ends exactly as
    the archive has it - files created after the backup are removed). Keyfiles 0600."""
    from app import config

    if not is_full_archive(path):
        raise BackupError(f"not a {branding.brand_name()} full backup archive (no manifest.json).")

    summary: dict = {
        "backend": None,
        "db_restored": False,
        "uploads_restored": 0,
        "uploads_deleted": 0,
        "keyfiles_restored": [],
        "pg_restore_hint": None,
        "dry_run": dry_run,
        "verified": False,
        "leftover_recovery_copies": [],
    }
    with tarfile.open(path, "r:*") as tar:
        manifest = _read_manifest(tar)
    backend = manifest.get("backend")
    summary["backend"] = backend
    db_member = manifest.get("db_member")
    # #525: a SQLite archive on a Postgres install swapped a file the app never reads,
    # reported db_restored and replaced the keyfiles (making the live Postgres rows'
    # Fernet-encrypted fields unreadable); the reverse stranded a dump. Refuse BEFORE
    # anything is touched, dry run included, so the plan never promises it.
    live_backend = "sqlite" if config.IS_SQLITE else "postgres"

    def refuse_backend_mismatch() -> None:
        if backend in ("sqlite", "postgres") and backend != live_backend:
            raise BackupError(
                f"this archive holds a {backend} database but this install runs "
                f"{live_backend}: refusing to restore it (nothing was changed). Restore it "
                f"on a {backend} install, or migrate the data first."
            )

    # --- count what a real restore would DELETE (files present now, not in the archive)
    archive_uploads = {
        m["path"][len(UPLOADS_PREFIX) :]
        for m in (manifest.get("members") or [])
        if str(m.get("path", "")).startswith(UPLOADS_PREFIX)
    }
    current_uploads = set()
    if config.UPLOADS_DIR.is_dir():
        for root, _dirs, files in os.walk(config.UPLOADS_DIR):
            for f in files:
                rel = os.path.relpath(os.path.join(root, f), config.UPLOADS_DIR).replace(
                    os.sep, "/"
                )
                current_uploads.add(rel)
    summary["uploads_deleted"] = len(current_uploads - archive_uploads)

    if dry_run:
        refuse_backend_mismatch()
        summary["uploads_restored"] = len(archive_uploads)
        summary["keyfiles_restored"] = list(manifest.get("keyfiles_included") or [])
        summary["db_restored"] = backend == "sqlite" and db_member is not None
        if backend == "postgres":
            summary["pg_restore_hint"] = _pg_restore_hint(config, db_member)
        _set_restore_status(summary, db_member, backend)
        return summary

    # --- gate finding 2: VERIFY before any destructive write --------------------
    if not skip_verify:
        ok, checks = verify_archive(path)
        summary["verified"] = ok
        _tail = (
            ". Run `db verify-backup` for the full report, or pass --skip-verify "
            "to force a desperate partial recovery."
        )
        # MED-1: a real FAILURE wins over any warn -- report the corruption first, so a
        # corrupt archive is never misdiagnosed as merely unverified.
        fails = [c["check"] for c in checks if c["status"] == "fail"]
        if fails:
            raise BackupError(
                "archive FAILED verification - refusing to restore a bad archive. "
                f"Failing checks: {', '.join(fails[:8])}"
                + (" ..." if len(fails) > 8 else "")
                + _tail
            )
        # HIGH-3: an advisory warn (a schema-1 archive did not record dump metadata) with
        # the DB integrity otherwise validated does NOT block the restore -- the documented
        # failed-upgrade recovery of a pre-P2a checkpoint must succeed. Only a GENUINE
        # inability to validate the DB contents (pg_restore absent, so the dump was never
        # checked) blocks, and it names the same escape hatch the fail branch does (MED-1).
        unvalidated = [
            c["check"] for c in checks if c["status"] == "warn" and c["check"] == "db_integrity"
        ]
        if unvalidated:
            raise BackupError("UNVERIFIED: pg_restore not available: archive not validated" + _tail)

    # A corrupt archive is still reported as corrupt first; a good one of the wrong
    # backend is refused here, before staging or any swap.
    refuse_backend_mismatch()

    # --- extract everything to a staging dir INSIDE DATA_DIR (same filesystem) ---
    config.DATA_DIR.mkdir(parents=True, exist_ok=True)
    # Sweep abandoned staging, never potentially irreplaceable recovery copies.
    _sweep_restore_leftovers(config.DATA_DIR)
    staging = Path(config.DATA_DIR) / f"{_STAGING_PREFIX}{os.getpid()}"
    if staging.exists():
        shutil.rmtree(staging, ignore_errors=True)
    staging.mkdir(parents=True, exist_ok=True)

    # Release any DB handle THIS process holds (a prior in-process command, or the
    # verify step) so the os.replace DB swap cannot be blocked by our own open handle
    # on Windows (POSIX rename ignores open handles; Windows does not).
    try:
        from app.db import engine

        engine.dispose()
    except Exception:  # noqa: S110 -- a held handle makes the os.replace swap fail loudly later
        pass

    swaps: list[tuple[str, str, str | None]] = []
    try:
        # R2: computed INSIDE the try so a malformed manifest (e.g. bytes=null on the
        # trusted --skip-verify path) surfaces as a wrapped BackupError, never a raw
        # TypeError that breaks the only-BackupError contract. R3: the extraction cap is
        # the SMALLER of the manifest-declared total (tight on the verified path) and an
        # absolute free-space-derived ceiling (so a --skip-verify archive that lies about
        # its sizes still cannot fill the disk).
        declared_cap = sum(int(m.get("bytes") or 0) for m in (manifest.get("members") or []))
        declared_cap += _EXTRACT_SLACK_BYTES
        free_cap = _absolute_extract_ceiling(config)
        max_total = min(declared_cap, free_cap)
        # RES-3: when the FREE-SPACE ceiling is the binding constraint (not the
        # manifest-derived cap), the refusal must name free space, not the bomb wording.
        cap_is_free_space = free_cap < declared_cap

        with tarfile.open(path, "r:*") as tar:
            _safe_extractall(
                tar,
                str(staging),
                max_total_bytes=max_total,
                cap_is_free_space=cap_is_free_space,
            )

        # keyfiles FIRST (irreplaceable material)
        src_keys = staging / "keyfiles"
        # Refuse known conflicts before displacing any installation member. Keep
        # the per-swap guards too, in case a recovery copy appears after preflight.
        targets = [Path(config.UPLOADS_DIR)]
        for name in KEYFILE_NAMES:
            if (src_keys / name).is_file() or name.startswith(".audit_"):
                targets.append(Path(config.DATA_DIR) / name)
        if backend == "sqlite" and db_member and (staging / db_member).is_file():
            targets.append(Path(config.DB_PATH))
        for target in targets:
            _clear_leftover(str(target) + _PRE_RESTORE_SUFFIX)
        # A pre-v2 archive must not inherit another history's format boundary.
        if not (src_keys / ".audit_format").exists():
            old_format = str(Path(config.DATA_DIR) / ".audit_format")
            if os.path.exists(old_format):
                prior_format = old_format + _PRE_RESTORE_SUFFIX
                _clear_leftover(prior_format)
                os.replace(old_format, prior_format)
                swaps.append(("file", old_format, prior_format))
        if not (src_keys / ".audit_key").is_file():
            # Pre-train archives derive their signing key from the restored session
            # secret. Retaining this host's trust files would bind the wrong history.
            for name in (".audit_key", ".audit_epochs", ".audit_head"):
                dst = str(Path(config.DATA_DIR) / name)
                pre = dst + _PRE_RESTORE_SUFFIX
                _clear_leftover(pre)
                if os.path.exists(dst):
                    os.replace(dst, pre)
                    swaps.append(("file", dst, pre))
        # Older audit-aware archives may omit the empty epoch list. Clear the target's
        # unrelated rotations as part of the same rollback-protected swap set.
        if (src_keys / ".audit_key").is_file() and not (src_keys / ".audit_epochs").exists():
            (src_keys / ".audit_epochs").write_bytes(b"[]")
        if (src_keys / ".audit_key").is_file() and not (src_keys / ".audit_head").exists():
            old_head = str(Path(config.DATA_DIR) / ".audit_head")
            if os.path.exists(old_head):
                prior_head = old_head + _PRE_RESTORE_SUFFIX
                _clear_leftover(prior_head)
                os.replace(old_head, prior_head)
                swaps.append(("file", old_head, prior_head))
        for name in KEYFILE_NAMES:
            kf = src_keys / name
            if kf.is_file():
                dst_kf = str(Path(config.DATA_DIR) / name)
                _swap_file(str(kf), dst_kf, swaps)
                try:  # noqa: SIM105
                    os.chmod(dst_kf, 0o600)
                except OSError:
                    pass
                summary["keyfiles_restored"].append(name)

        # DB (sidecar removal is DEFERRED to the success block below - hardening: a
        # rollback must restore the OLD DB WITH its -wal/-shm, not orphan them).
        if backend == "sqlite" and db_member and (staging / db_member).is_file():
            _swap_file(str(staging / db_member), str(config.DB_PATH), swaps)
            summary["db_restored"] = True
        elif backend == "postgres" and db_member and (staging / db_member).is_file():
            dump_dst = config.DATA_DIR / "pg_dump-restore.dump"
            shutil.copyfile(staging / db_member, dump_dst)
            summary["pg_restore_hint"] = _pg_restore_hint(config, str(dump_dst))

        # uploads WHOLESALE (empty archive -> empty tree, so the swap is consistent)
        src_uploads = staging / "uploads"
        if not src_uploads.exists():
            src_uploads.mkdir(parents=True, exist_ok=True)
        _swap_dir(str(src_uploads), str(config.UPLOADS_DIR), swaps)
        summary["uploads_restored"] = sum(1 for f in config.UPLOADS_DIR.rglob("*") if f.is_file())
        _set_restore_status(summary, db_member, backend)

        # success: every swap landed. NOW drop the stale -wal/-shm sidecars of the OLD
        # DB (they no longer match the freshly-swapped single-file snapshot), then drop
        # the .pre-restore backups.
        if summary["db_restored"]:
            for sidecar in (str(config.DB_PATH) + "-wal", str(config.DB_PATH) + "-shm"):
                if os.path.exists(sidecar):
                    _rm(sidecar)
        for kind, _final, pre in swaps:
            if pre and os.path.exists(pre):
                if kind == "dir":
                    shutil.rmtree(pre, ignore_errors=True)
                else:
                    _rm(pre)
        summary["leftover_recovery_copies"] = [
            pre for _kind, _final, pre in swaps if pre and os.path.lexists(pre)
        ]
    except Exception as exc:
        failures = _rollback(swaps)
        if failures:
            raise BackupError(
                f"restore failed: {exc}; rollback incomplete: {'; '.join(failures)}. "
                "Preserve .pre-restore copies and recover the listed paths before retrying."
            ) from exc
        if isinstance(exc, BackupError):
            raise
        raise BackupError(f"restore failed and was rolled back: {exc}") from exc
    finally:
        shutil.rmtree(staging, ignore_errors=True)
    return summary


def _swap_file(src: str, dst: str, swaps: list) -> None:
    """os.replace src -> dst, keeping any displaced original as dst.pre-restore.
    Records the swap for rollback. Same-filesystem (staging is inside DATA_DIR).

    Existing recovery copies block replacement until the operator resolves them.
    Journal the displacement before attempting to install the staged member."""
    pre_path = dst + _PRE_RESTORE_SUFFIX
    _clear_leftover(pre_path)
    pre = None
    if os.path.exists(dst):
        pre = pre_path
        os.replace(dst, pre)
    swaps.append(("file", dst, pre))
    os.replace(src, dst)


def _swap_dir(src: str, dst: str, swaps: list) -> None:
    """os.replace src dir -> dst dir, keeping any displaced original as dst.pre-restore.
    Journal the displacement before installation, as in _swap_file."""
    pre_path = dst + _PRE_RESTORE_SUFFIX
    _clear_leftover(pre_path)
    pre = None
    if os.path.exists(dst):
        pre = pre_path
        os.replace(dst, pre)
    swaps.append(("dir", dst, pre))
    os.replace(src, dst)


def _clear_leftover(path: str) -> None:
    """Refuse to discard an unowned recovery copy, even when its final is absent."""
    if os.path.lexists(path):
        raise BackupError(
            f"existing recovery copy: {path}. Preserve it and recover or move it "
            "to a secure location before retrying restore."
        )


def _rollback(swaps: list) -> list[str]:
    """Reverse completed or incomplete swaps and report every failed recovery.

    R1: when there was NO displaced original (pre is None - the target had no such file,
    e.g. a fresh machine with no keyfiles yet), the just-installed member from the
    archive must be REMOVED, not left in place. Leaving it stranded contradicts the
    'rolls the originals back' contract and would, on a keyfile, leave the archive's live
    key material installed after a failed restore."""
    failures = []
    for kind, final, pre in reversed(swaps):
        try:
            if pre and not os.path.exists(pre):
                raise OSError(f"recovery copy missing: {pre}")
            if kind == "dir" and os.path.exists(final):
                shutil.rmtree(final)
            if pre:
                os.replace(pre, final)
            elif pre is None and kind == "file" and os.path.exists(final):
                # nothing was here before -> discard the archive's file entirely.
                os.remove(final)
        except OSError as exc:
            failures.append(f"{final} (recovery copy: {pre}): {exc}")
    return failures


def _absolute_extract_ceiling(config) -> int:
    """R3: an absolute upper bound on extracted bytes, INDEPENDENT of the (possibly
    untrusted, on --skip-verify) manifest. Derived from free disk space so a legit large
    restore is never rejected while a bomb cannot fill the disk; falls back to a fixed
    ceiling if free space is unknowable."""
    try:
        free = shutil.disk_usage(config.DATA_DIR).free
        # keep a small reserve so the machine never wedges at 100% full.
        return max(0, free - 64 * 1024 * 1024)
    except OSError:
        return 64 * 1024 * 1024 * 1024  # 64 GiB fixed fallback


def _rm(path: str) -> None:
    try:  # noqa: SIM105
        os.remove(path)
    except OSError:
        pass


def _pg_restore_hint(config, dump_path: str) -> str:
    import shlex

    flags, _password = _pg_connection_args(config.DB_URL)
    argv = ["pg_restore", "--clean", "--if-exists", *flags, dump_path]
    return (
        "Postgres DB restore is NOT automated. Supply the password via PGPASSWORD "
        "or ~/.pgpass (libpq postgresql connections), then restore the dump:\n"
        "  " + shlex.join(argv)
    )


def _safe_extractall(
    tar: tarfile.TarFile,
    dest: str,
    *,
    max_total_bytes: int | None = None,
    cap_is_free_space: bool = False,
) -> None:
    """Extract with THREE guards (gate finding 4): (a) a name-based path-traversal /
    absolute-path refusal, (b) a refusal of any link / device / special member
    (the name guard is symlink-blind - probe p4E), and (c) a total-extracted-bytes cap.
    RES-3: when `cap_is_free_space` the cap is the free-disk ceiling, so the refusal
    names FREE SPACE rather than the decompression-bomb wording. Then extractall with
    Python's own `filter="data"` as belt-and-braces (the default filter is None on this
    build, so it MUST be explicit)."""
    dest_abs = os.path.abspath(dest)
    total = 0
    for member in tar.getmembers():
        target = os.path.abspath(os.path.join(dest, member.name))
        if target != dest_abs and not target.startswith(dest_abs + os.sep):
            raise BackupError(f"unsafe archive member path (would escape dest): {member.name!r}")
        if member.issym() or member.islnk():
            raise BackupError(f"archive contains a link member (refused): {member.name!r}")
        if member.ischr() or member.isblk() or member.isfifo() or member.isdev():
            raise BackupError(
                f"archive contains a special/device member (refused): {member.name!r}"
            )
        total += max(0, member.size)
        if max_total_bytes is not None and total > max_total_bytes:
            if cap_is_free_space:
                raise BackupError(
                    f"archive extracts to more than the ~{max_total_bytes} bytes of FREE "
                    "DISK SPACE available - refusing to fill the disk. Free up space or "
                    "restore to a larger volume."
                )
            raise BackupError(
                f"archive extracts to more than the allowed {max_total_bytes} bytes "
                "(decompression-bomb guard)."
            )
    try:
        tar.extractall(dest, filter="data")
    except tarfile.TarError as exc:
        raise BackupError(f"archive extraction refused by the data filter: {exc}") from exc


__all__ = [
    "_clear_leftover",
    "_older_than",
    "_pg_restore_hint",
    "_rollback",
    "_safe_extractall",
    "_set_restore_status",
    "_swap_dir",
    "_swap_file",
    "_sweep_restore_leftovers",
    "restore_archive",
]
