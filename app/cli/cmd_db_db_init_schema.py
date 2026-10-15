"""cmd_db: db init-schema

Documented in: docs/architecture/backups-and-dr.md
"""

import contextlib
import os
import subprocess
import sys

from app.cli._common import (
    EXIT_FAIL,
    EXIT_USAGE,
    CliError,
    _session,
    bounded_id,
    confirm,
    require_local,
    same_file,
)


# --- helpers -----------------------------------------------------------------
def _is_sqlite() -> bool:
    from app import config

    return config.IS_SQLITE


# --- db migrate --------------------------------------------------------------
def _migrate(args, emit) -> int:
    require_local(args)
    from app import config

    if _is_sqlite():
        from app.db import init_db

        init_db()
        emit.result(
            "Schema up to date (SQLite init_db).",
            {"ok": True, "backend": "sqlite", "action": "init_db"},
        )
        return 0
    # Postgres -> shell alembic upgrade head.
    emit.detail("Postgres backend -> alembic upgrade head")
    try:
        proc = subprocess.run(
            [sys.executable, "-m", "alembic", "upgrade", "head"],
            cwd=str(config.BASE_DIR),
            capture_output=emit.as_json,
            text=True,
        )
    except FileNotFoundError as exc:
        raise CliError(f"could not run alembic: {exc}") from exc
    if proc.returncode != 0:
        raise CliError(
            "alembic upgrade head failed"
            + (f": {proc.stderr.strip()}" if getattr(proc, "stderr", None) else "")
        )
    emit.result(
        "Migrations applied (alembic upgrade head).",
        {"ok": True, "backend": "postgres", "action": "alembic upgrade head"},
    )
    return 0


# --- db init-schema ----------------------------------------------------------
def _init_schema(args, emit) -> int:
    require_local(args)
    from app.db import init_db

    init_db()
    emit.result("Schema initialized (init_db).", {"ok": True, "action": "init_db"})
    return 0


# --- db backup ---------------------------------------------------------------
@contextlib.contextmanager
def _private_umask():
    """Files created inside are 0600/0700 from their first byte (#544 item 2): a plain
    backup holds password and key hashes, encrypted connector tokens and all tenant
    content, so it must never exist group/other-readable under a normal 022 umask."""
    old = os.umask(0o077)
    try:
        yield
    finally:
        os.umask(old)


def _chmod_private(path: str) -> None:
    try:  # noqa: SIM105 - belt-and-braces for an existing file kept by --force
        os.chmod(path, 0o600)
    except OSError:
        pass


def _backup(args, emit) -> int:
    require_local(args)
    from app import config

    if getattr(args, "full", False):
        return _backup_full(args, emit)

    dst = args.path
    if not _is_sqlite():
        from app.cli import backup_archive as bk

        if os.path.exists(dst) and not args.force:
            raise CliError(f"destination exists: {dst} (pass --force to overwrite).", EXIT_USAGE)
        if os.path.exists(dst):  # --force keeps the old inode and mode: narrow it BEFORE writing
            _chmod_private(dst)
        try:
            with _private_umask():
                bk._dump_postgres(dst)
        except bk.BackupError as exc:
            raise CliError(str(exc)) from exc
        _chmod_private(dst)
        emit.result(
            f"Postgres dump written -> {dst}", {"ok": True, "backend": "postgres", "path": dst}
        )
        return 0

    src = str(config.DB_PATH)
    if not os.path.isfile(src):
        raise CliError(f"database file not found: {src}")
    if same_file(src, dst):
        raise CliError("backup destination must differ from the live DB path.")
    if os.path.exists(dst) and not args.force:
        raise CliError(f"destination exists: {dst} (pass --force to overwrite).", EXIT_USAGE)
    # WAL-checkpoint (TRUNCATE) so the -wal is folded into the main file, then a
    # single-file copy - the BACKUP.md Option B recipe, done in-process. We use a
    # raw sqlite3 connection (not the app engine) so we don't disturb pool state.
    import sqlite3

    conn = sqlite3.connect(src)
    try:
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        conn.commit()
        # sqlite3's own online .backup API is the atomic, consistent path. The
        # destination is created 0600 (private umask) so the plaintext copy is never
        # readable by another local user (#544 item 2).
        if os.path.exists(dst):  # --force keeps the old inode and mode: narrow it BEFORE writing
            _chmod_private(dst)
        with _private_umask():
            out = sqlite3.connect(dst)
            try:
                conn.backup(out)
            finally:
                out.close()
        _chmod_private(dst)
    finally:
        conn.close()
    emit.result(
        f"Backup written -> {dst}\n"
        "(Also back up DATA_DIR: uploads + keyfiles. See docs/BACKUP.md.)",
        {"ok": True, "backend": "sqlite", "path": dst},
    )
    return 0


# --- db backup --full --------------------------------------------------------
def _backup_full(args, emit) -> int:
    """Whole-install archive: DB + uploads/ + keyfiles in one .tar.gz (gap G-06).
    In-process only (require_local ran in _backup). Prints a LOUD warning when the
    archive contains live keyfile secrets."""
    from app.cli import backup_archive as bk

    try:
        manifest = bk.create_archive(args.path, force=args.force)
    except bk.BackupError as exc:
        code = EXIT_USAGE if "exists" in str(exc) else 1
        raise CliError(str(exc), code) from exc

    n_uploads = sum(1 for m in manifest["members"] if m["kind"] == "upload")
    n_keys = len(manifest["keyfiles_included"])
    if manifest["secrets_included"]:
        # Loud, one line, to stderr (never pollutes --json stdout).
        kf_list = ", ".join(manifest["keyfiles_included"])
        emit.warn(
            f"this archive CONTAINS live secret key material ({kf_list}) in the clear "
            "- store it ENCRYPTED at rest and restrict access (see docs/BACKUP.md)."
        )
    else:
        emit.warn(
            "secrets are env/KMS-sourced, so no keyfiles are in this archive - back "
            "up your secret manager separately or the encrypted data is unrecoverable."
        )
    emit.result(
        f"Full backup written -> {args.path}\n"
        f"  backend={manifest['backend']}  uploads={n_uploads}  keyfiles={n_keys}  "
        f"app={manifest['app_version']}\n"
        f"Verify it now:  loc-tms db verify-backup {args.path}",
        {
            "ok": True,
            "path": args.path,
            "backend": manifest["backend"],
            "uploads": n_uploads,
            "keyfiles_included": manifest["keyfiles_included"],
            "secrets_included": manifest["secrets_included"],
            "app_version": manifest["app_version"],
            "created_utc": manifest["created_utc"],
        },
    )
    return 0


# --- db verify-backup --------------------------------------------------------
def _verify_backup(args, emit) -> int:
    """Restore-verification control: open a --full archive and check manifest
    completeness, per-member sha256, DB integrity, and keyfile presence. Exit
    nonzero on any failure. Read-only."""
    require_local(args)
    from app.cli import backup_archive as bk

    ok, checks = bk.verify_archive(args.path)
    # MED-1: a FAIL must win over a warn in the verdict, so a corrupt archive that also has
    # a warn (e.g. pg_restore absent) reads FAILED, not UNVERIFIED.
    any_fail = any(c["status"] == "fail" for c in checks)
    any_warn = any(c["status"] == "warn" for c in checks)
    verdict = "FAILED" if any_fail else "UNVERIFIED" if any_warn else "PASSED"
    if emit.as_json:
        emit.json(
            {
                "ok": ok,
                "verification": verdict,
                "path": args.path,
                "checks": checks,
            }
        )
    else:
        emit.say(f"db verify-backup ({args.path}):")
        emit.table(
            [(c["check"], c["status"].upper(), c["detail"]) for c in checks],
            headers=("check", "status", "detail"),
        )
        emit.say("")
        emit.say(
            "FAILED: one or more checks failed - see the FAIL row(s). Do NOT rely on this archive."
            if any_fail
            else "UNVERIFIED: archive not validated. Install pg_restore and verify again."
            if any_warn
            else "PASSED: archive integrity checks passed. Prove recovery in a drill. (Detects "
            "corruption/truncation, NOT tampering - the manifest is inside the "
            "archive; record its own sha256 out-of-band, see docs/BACKUP.md.)"
        )
    return 0 if ok else 1


def _assert_looks_like_db(src: str) -> None:
    """Refuse a legacy single-file restore whose source is not a restorable SQLite DB
    (gate finding 5). Plaintext install: require the 16-byte `SQLite format 3\\0`
    magic and SQLite integrity_check. SQLCipher install: the magic
    must be ABSENT and the DB must actually open + pass a keyed integrity check
    with THIS install's key (a wrong-key or non-DB file is refused)."""
    from app.cli.backup_archive import SQLITE_MAGIC

    with open(src, "rb") as fh:
        head = fh.read(16)
    if not _is_encrypted_install():
        if head != SQLITE_MAGIC:
            raise CliError(
                f"{src} is not a SQLite database (missing the 'SQLite format 3' "
                "header). Refusing to overwrite the live DB with a non-database file. "
                "If this is a --full backup archive, pass it directly (it is auto-"
                "detected); if Postgres, use pg_restore.",
                EXIT_USAGE,
            )
        import sqlite3
        from pathlib import Path

        try:
            conn = sqlite3.connect(Path(src).resolve().as_uri() + "?immutable=1", uri=True)
            try:
                rows = conn.execute("PRAGMA integrity_check").fetchall()
                if rows != [("ok",)]:
                    raise ValueError("integrity_check failed")
            finally:
                conn.close()
        except (sqlite3.Error, ValueError) as exc:
            raise CliError(f"{src} failed SQLite integrity_check; refusing restore.") from exc
        return
    # Encrypted install: ciphertext must NOT carry the plaintext magic, and must open.
    if head == SQLITE_MAGIC:
        raise CliError(
            f"{src} is a PLAINTEXT SQLite database but this install is SQLCipher-"
            "encrypted (LOC_TMS_DB_ENCRYPTION=sqlcipher). Refusing the mismatch.",
            EXIT_USAGE,
        )
    try:
        from app.kernel import dbcrypt

        conn = dbcrypt.connect(src)
        try:
            if conn.execute("PRAGMA integrity_check").fetchall() != [("ok",)]:
                raise ValueError("integrity_check failed")
        finally:
            conn.close()
    except Exception as exc:
        raise CliError(
            f"{src} could not be opened as a SQLCipher database with this install's "
            f"key ({type(exc).__name__}). Refusing to overwrite the live DB.",
            EXIT_USAGE,
        ) from exc


def _is_encrypted_install() -> bool:
    from app import config

    return bool(config.DB_ENCRYPTION_ON)


# --- db restore (full archive) -----------------------------------------------
def _restore_full(args, emit, src: str) -> int:
    """DESTRUCTIVE whole-install restore from a --full archive: replaces the DB,
    the uploads tree, AND the encryption keyfiles. Double-confirmed, and refused
    while the server appears to be running (a live writer + a keyfile swap would
    corrupt state)."""
    from app.cli import backup_archive as bk

    # Server-running guard (best-effort; "if detectable" per spec). Honesty line when
    # NOT detected: the probe cannot see a server on a different host/port (finding 11).
    running, where = bk.server_appears_running()
    if running:
        raise CliError(
            f"the server appears to be RUNNING (something is listening on {where}). "
            "A restore replaces the DB + keyfiles out from under a live process. Stop "
            "the server and retry.",
            1,
        )
    emit.detail(
        f"no listener detected on {where} - if your server runs elsewhere, stop it "
        "yourself before continuing (the probe cannot see a different host/port)."
    )

    # Peek the manifest so the confirmation names exactly what will be overwritten,
    # including how many CURRENT upload files (created after the backup) will be
    # DELETED by the wholesale replace (gate finding 9). GitHub #544: the preview must
    # fail like the real call below (a bad manifest.json is a CliError + --json error
    # object, not a raw traceback).
    try:
        dry = bk.restore_archive(src, dry_run=True)
    except bk.BackupError as exc:
        raise CliError(str(exc), 1) from exc
    kf = ", ".join(dry["keyfiles_restored"]) or "none"
    del_note = (
        f" {dry['uploads_deleted']} existing upload file(s) NOT in this archive will be DELETED."
        if dry["uploads_deleted"]
        else ""
    )
    first = (
        f"FULL RESTORE from {src} (backend={dry['backend']}): this OVERWRITES the "
        f"database, the uploads tree ({dry['uploads_restored']} file(s) from the "
        f"archive), and the encryption keyfiles ({kf})." + del_note + " Continue?"
    )
    if dry["status"] == "partial":
        first = (
            f"FULL RESTORE from {src} (backend={dry['backend']}): this restores the "
            f"uploads tree ({dry['uploads_restored']} file(s) from the archive) and "
            f"keyfiles ({kf}); the DATABASE is NOT restored by this command, "
            "a pg_restore recipe follows; exits 1 until it is restored manually."
            + del_note
            + " Continue?"
        )
    if not confirm(emit, first, args.yes):
        raise CliError("aborted.", 1)
    second = (
        "SECOND CONFIRMATION: replacing the keyfiles changes the encryption/session "
        "keys for this install. Any encrypted data NOT covered by this archive "
        "becomes unreadable. Proceed with the destructive restore?"
    )
    if not confirm(emit, second, args.yes):
        raise CliError("aborted.", 1)

    try:
        summary = bk.restore_archive(src, skip_verify=getattr(args, "skip_verify", False))
    except bk.BackupError as exc:
        raise CliError(str(exc), 1) from exc

    status = summary["status"]
    lines = [
        f"PARTIAL restore from {src} (backend={summary['backend']}): the DATABASE WAS NOT RESTORED."
        if status == "partial"
        else f"Full restore complete from {src} (backend={summary['backend']})."
    ]
    if summary["db_restored"]:
        lines.append("  DB restored.")
    if summary["uploads_restored"]:
        lines.append(f"  uploads restored: {summary['uploads_restored']} file(s).")
    if summary["keyfiles_restored"]:
        lines.append(f"  keyfiles restored (0600): {', '.join(summary['keyfiles_restored'])}.")
    if ".audit_key" in summary["keyfiles_restored"]:
        lines.append("  Audit trust state restored together with the database backup.")
    elif ".session_secret" in summary["keyfiles_restored"]:
        lines.append(
            "  Legacy archive: audit key will be re-derived from the restored session secret."
        )
    else:
        lines.append(
            "  Legacy archive: audit key will be re-derived from the configured session secret. "
            "Restore the source install's session secret before starting."
        )
    if summary["pg_restore_hint"]:
        lines.append("  " + summary["pg_restore_hint"].replace("\n", "\n  "))
    if summary.get("leftover_recovery_copies"):
        lines.append(
            "WARNING: recovery copies could not be removed. After confirming the restored "
            "install is healthy, delete these secret-bearing copies or move them to secure "
            "storage before another restore:"
        )
        lines.extend("  " + path for path in summary["leftover_recovery_copies"])
    lines.append(
        "Do NOT start the server until the database is restored."
        if status == "partial"
        else "Restart the server."
    )
    emit.result(
        "\n".join(lines),
        {"ok": status == "complete", "status": status, "restored_from": src, **summary},
    )
    return EXIT_FAIL if status == "partial" else 0


# --- db encrypt / decrypt (reuse the convert tool's functions) ---------------
def _convert(args, emit, direction) -> int:
    require_local(args)
    # Import the ship-it converter's functions - never duplicate the ATTACH +
    # sqlcipher_export logic.
    from scripts import convert_db_encryption as conv

    src, dst = args.src, args.dst
    if not os.path.isfile(src):
        raise CliError(f"source not found: {src}")
    if same_file(src, dst):
        raise CliError("src and dst must differ (this never edits in place).")
    if os.path.exists(dst) and not args.force:
        raise CliError(f"destination exists: {dst} (pass --force to overwrite).", 2)
    if os.path.exists(dst) and args.force:
        os.remove(dst)
    try:
        (conv.encrypt if direction == "encrypt" else conv.decrypt)(src, dst)
    except SystemExit as exc:  # the converter sys.exit(2)s if sqlcipher3 is absent
        raise CliError(
            "SQLCipher binding missing - pip install sqlcipher3.", exc.code or 1
        ) from exc
    except Exception as exc:
        # error, not a crash: decrypting a plaintext source ('file is not a database'),
        # a wrong/missing ENCRYPTION_KEY, or encrypting a non-SQLite source (#27). The
        # aborted export may have left a partial/corrupt dst — remove it before failing.
        if os.path.exists(dst):
            try:  # noqa: SIM105
                os.remove(dst)
            except OSError:
                pass
        hint = (
            "is the source actually SQLCipher-encrypted, and is ENCRYPTION_KEY the "
            "key it was encrypted with?"
            if direction == "decrypt"
            else "is the source a plaintext SQLite database?"
        )
        raise CliError(f"could not {direction} {src}: {exc} -- {hint}") from exc
    if direction == "encrypt":
        emit.result(
            f"Wrote encrypted DB -> {dst}\n"
            "Next: set LOC_TMS_DB_ENCRYPTION=sqlcipher and point the app at it.",
            {"ok": True, "direction": "encrypt", "src": src, "dst": dst},
        )
    else:
        emit.result(
            f"Wrote plaintext DB -> {dst}\n"
            "Next: unset LOC_TMS_DB_ENCRYPTION and point the app at it.",
            {"ok": True, "direction": "decrypt", "src": src, "dst": dst},
        )
    return 0


def _encrypt(args, emit) -> int:
    return _convert(args, emit, "encrypt")


def _decrypt(args, emit) -> int:
    return _convert(args, emit, "decrypt")


# --- db recover-jobs ---------------------------------------------------------
def _recover_jobs(args, emit) -> int:
    require_local(args)
    from app.kernel import batch_runs, jobs, orchestrator

    # at_boot defaults to False on purpose: this sweep is documented to run WITHOUT bouncing a
    # LIVE server, so the durable ("db") adapter reclaims only EXPIRED-lease jobs and never
    # yanks one a live worker is still executing (which the server would then re-run -- a
    # double execution). Boot recovery (app/main.py) passes at_boot=True; this must not.
    touched = jobs.recover_interrupted()
    # The default stays non-destructive; this explicit sweep requires proof of process death.
    batches = batch_runs.recover_interrupted_batches(at_boot=False, recover_orphaned=True)
    # ...and PLAIN single runs (GitHub #44), the third healer main.py runs at startup.
    # Same parity reasoning: an operator must be able to sweep a stuck run without
    # bouncing the server, because a stuck run BLOCKS its locale until it is swept.
    runs = orchestrator.recover_interrupted_runs()
    emit.result(
        f"Recovered {touched} interrupted job(s), {batches} orphaned "
        f"batch(es) and {runs} interrupted run(s). Live or unknown batch owners left untouched.",
        {"ok": True, "recovered": touched, "recovered_batches": batches, "recovered_runs": runs},
    )
    return 0


# --- db protect-data ---------------------------------------------------------
def _protect_data(args, emit) -> int:
    require_local(args)
    from app import config

    created = config.create_protect_marker()
    if created:
        emit.result(
            f"Protected data dir {config.DATA_DIR} (wrote {config.PROTECT_MARKER}).\n"
            "The server now refuses to serve this dir unless "
            "LOC_TMS_ALLOW_REAL_DATA=1 is set (run.py sets it automatically).",
            {
                "ok": True,
                "data_dir": str(config.DATA_DIR),
                "marker": str(config.PROTECT_MARKER),
                "created": True,
            },
        )
    else:
        emit.result(
            f"Data dir {config.DATA_DIR} was already protected "
            f"(marker {config.PROTECT_MARKER} exists).",
            {
                "ok": True,
                "data_dir": str(config.DATA_DIR),
                "marker": str(config.PROTECT_MARKER),
                "created": False,
            },
        )
    return 0


# --- health ------------------------------------------------------------------
def _health(args, emit) -> int:
    require_local(args)
    from sqlalchemy import text

    try:
        with _session() as s:
            s.exec(text("SELECT 1"))
    except Exception as exc:
        emit.result(
            f"UNHEALTHY: {type(exc).__name__}",
            {"ok": False, "status": "error", "detail": type(exc).__name__},
        )
        raise CliError("database did not respond.", 1) from exc
    # Spend-cap status line (global kill switch): surfaced here so `health` doubles
    # as a quick "is the brake on and how close are we" check without a new command
    # (keeps the CLI-parity table unchanged).
    from app.kernel import spend as _spend

    cap = _spend.cap_usd()
    spent = _spend.spent_this_period()
    blocked = _spend.would_block()[0]
    if cap > 0:
        spend_line = f"spend cap ${spent:.2f}/${cap:.2f} this month" + (
            " (REACHED)" if blocked else ""
        )
    else:
        spend_line = f"spend ${spent:.2f} this month (cap off)"
    emit.result(
        f"OK: database responded. {spend_line}.",
        {
            "ok": True,
            "status": "ok",
            "spend": {
                "spent_usd": round(spent, 4),
                "cap_usd": cap,
                "cap_tripped": blocked,
                "cap_enabled": cap > 0,
            },
        },
    )
    return 0


# --- db scrub-scaffold -------------------------------------------------------
def _scrub_scaffold(args, emit) -> int:
    """One-off remediation (core-QA Part 1): find (dry-run) / quarantine (--apply) content
    that echoed our OWN prompt scaffolding and was persisted as a translation BEFORE the
    Q10 gate shipped. Segments route to needs_review + a scaffold qa_flag (text kept);
    poisoned SegmentTM/FullTextTM rows are DELETED (like `tm scrub-hedges`). Local-only,
    scans the whole DB by default; --apply is audit-logged. Dry-run reports nothing-changed
    so the real prod counts can be read before any write."""
    require_local(args)
    from app.kernel import tm_module

    actor = getattr(args, "actor", None) or "cli"
    with _session() as session:
        res = tm_module.scrub_scaffold(
            session, apply=args.apply, actor=actor, project_id=args.scaffold_project
        )
    hits = res["segments"] + res["segment_tm"] + res["full_text"]
    if emit.as_json:
        emit.json({"ok": True, **res})
        return 0
    if hits:
        rows = []
        for h in hits:
            if h["kind"] == "segment":
                loc = h["target_locale"]
                where = f"doc {h['document_id']}"
                act = (
                    "already needs_review+flag"
                    if h["already_quarantined"]
                    else "-> needs_review+flag"
                )
            else:
                loc = h["pair"]
                where = "global" if h["project_id"] is None else f"project {h['project_id']}"
                act = "-> delete"
            rows.append((h["kind"], h["id"], loc, where, act, h["sample"]))
        emit.table(rows, headers=("kind", "id", "locale/pair", "scope", "action", "target[:80]"))
    if res["applied"]:
        msg = (
            f"Quarantined {res['quarantined']} segment(s) (needs_review + scaffold flag) "
            f"and deleted {res['deleted']} scaffold TM entr"
            f"{'y' if res['deleted'] == 1 else 'ies'}. "
            f"Matched {res['matched']} row(s) total."
        )
    else:
        msg = (
            f"Matched {res['matched']} scaffold-echo row(s): "
            f"{res['would_quarantine']} segment(s) to quarantine, "
            f"{res['would_delete']} TM entr{'y' if res['would_delete'] == 1 else 'ies'} "
            f"to delete (dry-run; re-run with --apply to remediate)."
        )
    emit.result(
        msg,
        {
            "ok": True,
            "matched": res["matched"],
            "quarantined": res["quarantined"],
            "deleted": res["deleted"],
            "would_quarantine": res["would_quarantine"],
            "would_delete": res["would_delete"],
            "applied": res["applied"],
        },
    )
    return 0


# --- audit verify ------------------------------------------------------------
def verify_audit_chain(emit) -> int:
    """Shared audit-chain verification core (BUG-5: the ONE implementation behind
    BOTH the top-level `audit verify` and `admin audit verify`, so their output and
    error handling stay converged). Emits the OK/BROKEN result on both text + JSON
    surfaces; returns 0 when intact, raises CliError(exit 1) when the chain is
    broken. Callers apply their own gate (require_local / local_op) first."""
    from app.kernel import audit

    # Share the ONE service function with the scheduled sweep (they must never diverge).
    # record=False = a pure read-only spot check: same (ok, checked, first_bad) shape, but
    # no audit row / status write / latch (only the scheduled path records + monitors).
    ok, checked, first_bad = audit.run_chain_verification(record=False)
    note = (audit.chain_status_snapshot() or {}).get("note")
    if note:
        emit.say("AUDIT NOTE: " + note)
    # #220: an intact chain says nothing about rows never written. Say so when audit logging
    # is off (the hosted alert timer emails on this line); the exit code is unchanged.
    logging_on = audit.enabled()
    if not logging_on:
        emit.say(
            "AUDIT OFF: audit logging is turned off (audit_enabled is 0), so mutating "
            "actions are not being recorded. Turn it on: config set audit_enabled 1"
        )
    if ok:
        emit.result(
            f"OK: audit chain verified ({checked} row(s)).",
            {
                "ok": True,
                "checked": checked,
                "first_bad_id": None,
                "audit_enabled": logging_on,
                "note": note,
            },
        )
        return 0
    where = f" (first bad row id: {first_bad})" if first_bad is not None else ""
    detail = audit._format_chain_broken_detail(
        first_bad, checked, "anchor" if first_bad is None else "tamper"
    )
    hint = audit.legacy_restore_hint(first_bad)
    emit.result(
        f"BROKEN: audit chain failed after {checked} row(s){where}. {detail}"
        + (f" {hint}" if hint else ""),
        {
            "ok": False,
            "checked": checked,
            "first_bad_id": first_bad,
            "note": note,
            "detail": detail,
            "hint": hint,
        },
    )
    raise CliError("audit chain verification failed.", 1)


def _audit_verify(args, emit) -> int:
    require_local(args)
    return verify_audit_chain(emit)


def _audit_rotate_key(args, emit) -> int:
    require_local(args)
    from app.cli import backup_archive as bk
    from app.kernel import audit

    running, where = bk.server_appears_running()
    if running:
        raise CliError(
            f"The server appears to be RUNNING (listener on {where}). "
            "Stop the server and all other writers before rotating the audit key.",
            1,
        )
    if not confirm(emit, "Rotate the audit key? Stop all other writers first.", args.yes):
        raise CliError("Audit key rotation aborted.", EXIT_USAGE)
    try:
        key_id = audit.rotate_key(actor="cli")
    except ValueError as exc:
        raise CliError(str(exc), EXIT_USAGE) from exc
    emit.result("Audit key rotated; retired keys retained for verification.", {"key_id": key_id})
    return 0


def _audit_reset_anchor(args, emit) -> int:
    """HOST-ONLY, DOUBLE-confirmed re-baseline of the tail-truncation anchor (F2 companion).
    For the legitimate `db restore`-to-an-older-backup case only: the monotonic anchor guard
    would otherwise pin the anchor at the pre-restore (higher) head and report a false
    truncation forever. Writes a forced audit.anchor_reset row via the service function."""
    require_local(args)
    from app.cli import backup_archive as bk
    from app.kernel import audit

    running, where = bk.server_appears_running()
    if running:
        raise CliError(
            f"The server appears to be RUNNING (listener on {where}). "
            "Stop the server and all other writers before resetting the audit anchor.",
            1,
        )
    # First confirm (respects --yes / refuses non-interactively without it).
    if not confirm(
        emit,
        "Re-baseline the audit tail-truncation anchor to the CURRENT head? This deliberately "
        "discards any pending truncation signal and should ONLY follow a legitimate DB restore.",
        getattr(args, "yes", False),
    ):
        raise CliError("audit reset-anchor aborted (not confirmed).", EXIT_USAGE)
    # Second, EXPLICIT acknowledgement flag — this is exactly the move an attacker would make.
    if not getattr(args, "ack_regress", False):
        raise CliError(
            "audit reset-anchor also requires --i-understand-this-erases-truncation-evidence "
            "(a deliberate second acknowledgement).",
            EXIT_USAGE,
        )
    try:
        old_head, new_head = audit.reset_anchor(actor="cli")
    except ValueError as exc:
        raise CliError(str(exc), EXIT_USAGE) from exc
    if new_head is None:
        # R5: an EMPTY audit log — reset_anchor wrote nothing; do not claim a row was written.
        emit.result(
            "Nothing to re-baseline (audit log is empty).",
            {"ok": True, "old_head": old_head, "new_head": None, "no_op": True},
        )
        return 0
    emit.result(
        f"Audit anchor re-baselined: head {old_head} -> {new_head} "
        "(forced audit.anchor_reset row written).",
        {"ok": True, "old_head": old_head, "new_head": new_head},
    )
    return 0


def register_cmd_db_db_init_schema_db_sub(db_sub) -> None:
    p = db_sub.add_parser(
        "migrate",
        help="Bring the schema up to date (Postgres: alembic upgrade head; SQLite: init_db).",
    )
    p.set_defaults(func=_migrate)

    p = db_sub.add_parser(
        "init-schema",
        help="Create/upgrade the SQLite schema "
        "via init_db() (Alembic for fresh/stamped real installs; legacy self-heal).",
    )
    p.set_defaults(func=_init_schema)

    p = db_sub.add_parser(
        "backup",
        help="Write a consistent backup of the DB. --full writes ONE .tar.gz "
        "covering the DB + uploads + encryption keyfiles (whole-install recovery).",
    )
    p.add_argument(
        "path",
        help="destination: a DB file (default) OR a .tar.gz archive (with --full).",
    )
    p.add_argument("--force", action="store_true", help="overwrite path if it exists.")
    p.add_argument(
        "--full",
        action="store_true",
        help="write a FULL-INSTALL archive (DB + uploads/ + keyfiles) as one "
        "timestamped .tar.gz with an embedded manifest (gap G-06). The archive "
        "CONTAINS live secret key material when secrets are keyfile-based - store "
        "it encrypted (see docs/BACKUP.md).",
    )
    p.set_defaults(func=_backup)

    p = db_sub.add_parser("backup-full", help="Write a full-install recovery archive.")
    p.add_argument("path", help="destination .tar.gz archive")
    p.add_argument("--force", action="store_true")
    p.set_defaults(func=_backup, full=True)

    p = db_sub.add_parser(
        "verify-backup",
        help="Verify a --full backup archive: manifest completeness, per-member "
        "sha256, PRAGMA integrity_check on the contained DB, and keyfile "
        "presence/non-emptiness. Exit nonzero on ANY failure (the restore-"
        "verification control). Read-only; local-only.",
    )
    p.add_argument("path", help="the .tar.gz archive to verify.")
    p.set_defaults(func=_verify_backup)


def register_cmd_db_db_init_schema_db_sub_2(db_sub) -> None:
    p = db_sub.add_parser(
        "encrypt",
        help="Write a SQLCipher-encrypted copy of a plaintext SQLite DB (never in place).",
    )
    p.add_argument("src", help="plaintext source DB file.")
    p.add_argument("dst", help="encrypted destination file.")
    p.add_argument("--force", action="store_true", help="overwrite dst if it exists.")
    p.set_defaults(func=_encrypt)

    p = db_sub.add_parser(
        "decrypt",
        help="Write a plaintext copy of a SQLCipher-encrypted SQLite DB (never in place).",
    )
    p.add_argument("src", help="encrypted source DB file.")
    p.add_argument("dst", help="plaintext destination file.")
    p.add_argument("--force", action="store_true", help="overwrite dst if it exists.")
    p.set_defaults(func=_decrypt)

    p = db_sub.add_parser(
        "recover-jobs",
        help=(
            "Recover interrupted jobs and plain runs; finalize batches with "
            "confirmed-dead local dispatchers."
        ),
    )
    p.set_defaults(func=_recover_jobs)

    p = db_sub.add_parser(
        "protect-data",
        help="Mark the current data dir PROTECTED "
        "(issue #13): the server then refuses to serve it unless "
        "LOC_TMS_ALLOW_REAL_DATA=1 (run.py sets it automatically).",
    )
    p.set_defaults(func=_protect_data)


def register_cmd_db_db_init_schema_db_sub_3(db_sub) -> None:
    p = db_sub.add_parser(
        "scrub-scaffold",
        help="Scan segments + stored TM (segment + full-text) for prompt-scaffold echoes "
        "(core-QA Part 1 / Q10 cleanup); --apply quarantines them (segments -> "
        "needs_review + scaffold flag, TM -> delete). Dry-run by default. Local-only.",
    )
    p.add_argument(
        "--apply",
        action="store_true",
        help="quarantine the matches (default: dry-run, report only).",
    )
    p.add_argument(
        "--project",
        dest="scaffold_project",
        type=bounded_id,
        default=None,
        help="limit the scan to one project id (default: scan EVERYTHING).",
    )
    p.set_defaults(func=_scrub_scaffold)


def register_cmd_db_db_init_schema(subparsers) -> None:
    p = subparsers.add_parser(
        "health", help="Ping the database (SELECT 1) and report readiness - mirrors /ready."
    )
    p.set_defaults(func=_health)


def register_cmd_db_db_init_schema_audit_sub(audit_sub) -> None:
    p = audit_sub.add_parser(
        "rotate-key", help="HOST-ONLY audit key rotation; refuses a server listener. Stop writers."
    )
    p.add_argument("--yes", action="store_true", help="confirm maintenance rotation.")
    p.set_defaults(func=_audit_rotate_key)
    p = audit_sub.add_parser(
        "verify", help="Verify the tamper-evident audit hash chain end to end."
    )
    p.set_defaults(func=_audit_verify)
    p = audit_sub.add_parser(
        "reset-anchor",
        help="HOST-ONLY re-baseline the tail-truncation anchor to the current head after a "
        "legitimate `db restore` to an OLDER backup (double-confirmed; self-audited).",
    )
    p.add_argument("--yes", action="store_true", help="skip the first confirmation prompt.")
    p.add_argument(
        "--i-understand-this-erases-truncation-evidence",
        dest="ack_regress",
        action="store_true",
        help="second required acknowledgement (this is the exact move an attacker wants).",
    )
    p.set_defaults(func=_audit_reset_anchor)
