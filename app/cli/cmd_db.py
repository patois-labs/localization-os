"""`db ...` ops commands + `health` + `audit verify`.

Transitional compatibility shim for one release (added 2026-09-14, remove the re-export block after
the next tagged release): `__all__` re-exports _is_sqlite, _migrate, _init_schema, _backup,
_backup_full, _verify_backup, _assert_looks_like_db, _is_encrypted_install, _restore_full, _convert,
_encrypt, _decrypt, _recover_jobs, _protect_data, _health, _scrub_scaffold, verify_audit_chain,
_audit_verify, _audit_reset_anchor from app.cli.cmd_db_db_init_schema.

All in-process: they import the app services and open the real DB (respecting
LOC_TMS_DATA_DIR / DATABASE_URL from config), so they work with no server up.
Nothing here ever runs against a live remote - Phase 1 is local-only.

Documented in: docs/architecture/backups-and-dr.md
"""

import os
import subprocess
import sys

from app.cli.cmd_db_db_init_schema import (
    _assert_looks_like_db,
    _audit_reset_anchor,
    _audit_verify,
    _backup,
    _backup_full,
    _convert,
    _decrypt,
    _encrypt,
    _health,
    _init_schema,
    _is_encrypted_install,
    _is_sqlite,
    _migrate,
    _protect_data,
    _recover_jobs,
    _restore_full,
    _scrub_scaffold,
    _verify_backup,
    register_cmd_db_db_init_schema,
    register_cmd_db_db_init_schema_audit_sub,
    register_cmd_db_db_init_schema_db_sub,
    register_cmd_db_db_init_schema_db_sub_2,
    register_cmd_db_db_init_schema_db_sub_3,
    verify_audit_chain,
)

from ._common import EXIT_FAIL, EXIT_USAGE, CliError, _session, bounded_id, confirm, require_local

__all__ = [
    "EXIT_FAIL",
    "EXIT_USAGE",
    "_assert_looks_like_db",
    "_audit_reset_anchor",
    "_audit_verify",
    "_backup",
    "_backup_full",
    "_convert",
    "_decrypt",
    "_encrypt",
    "_health",
    "_init_schema",
    "_is_encrypted_install",
    "_is_sqlite",
    "_migrate",
    "_protect_data",
    "_recover_jobs",
    "_restore_full",
    "_scrub_scaffold",
    "_session",
    "_verify_backup",
    "bounded_id",
    "subprocess",
    "sys",
    "verify_audit_chain",
]


# --- registration ------------------------------------------------------------
def register(subparsers) -> None:
    db = subparsers.add_parser(
        "db", help="Database ops: migrate, schema, backup/restore, encrypt, recover jobs."
    )
    db_sub = db.add_subparsers(dest="db_cmd", metavar="<subcommand>")
    db_sub.required = True

    register_cmd_db_db_init_schema_db_sub(db_sub)

    restore_help = (
        "Restore from a backup (DESTRUCTIVE). Auto-detects: a --full .tar.gz "
        "archive restores DB + uploads + keyfiles on SQLite (double-confirmed). "
        "On Postgres it restores uploads + keyfiles only; the DATABASE is NOT restored. "
        "A pg_restore recipe follows; reports PARTIAL and exits 1 until the database "
        "is restored manually. Audit trust files travel with full archives. "
        "A plain file restores just the SQLite DB; run audit reset-anchor after restarting."
    )
    p = db_sub.add_parser("restore", help=restore_help, description=restore_help)
    p.add_argument("path", help="backup file OR --full archive to restore from.")
    p.add_argument("--yes", action="store_true", help="skip the confirmation prompt(s).")
    p.add_argument(
        "--skip-verify",
        action="store_true",
        help="(--full archives only) SKIP the pre-restore integrity/sha256 "
        "verification. DANGEROUS: only for desperate partial recovery from a known-"
        "damaged archive; a normal restore verifies first and refuses a bad archive. "
        "On this path the archive's OWN metadata (manifest sizes) is TRUSTED - only an "
        "absolute free-space ceiling still bounds extraction.",
    )
    p.set_defaults(func=_restore)

    register_cmd_db_db_init_schema_db_sub_2(db_sub)

    p = db_sub.add_parser(
        "verify",
        help="Read-only DB integrity/health check: SQLite "
        "PRAGMA integrity_check + foreign_key_check, alembic head vs "
        "the DB's stamped revision, and referential-sanity orphan "
        "counts on a handful of key foreign keys. Exit nonzero on any "
        "failure. Distinct from `audit verify` (which checks the "
        "tamper-evident audit HASH CHAIN, not schema/data integrity). "
        "Local-only by design (see the command's docstring) - not "
        "available over --remote.",
    )
    p.set_defaults(func=_verify)

    register_cmd_db_db_init_schema_db_sub_3(db_sub)

    # top-level `health` and `audit verify` live logically with db ops.
    register_cmd_db_db_init_schema(subparsers)

    audit = subparsers.add_parser("audit", help="Audit-log ops.")
    audit_sub = audit.add_subparsers(dest="audit_cmd", metavar="<subcommand>")
    audit_sub.required = True
    register_cmd_db_db_init_schema_audit_sub(audit_sub)


# --- db restore --------------------------------------------------------------
def _restore(args, emit) -> int:
    require_local(args)
    from app import config

    src = args.path
    if not os.path.isfile(src):
        raise CliError(f"backup file not found: {src}")

    # Auto-route: a --full .tar.gz (manifest.json inside) restores the whole install;
    # anything else is a legacy single-file SQLite DB restore. No new flag needed, and
    # a legacy .db file is never a tar archive so the discriminator can't misfire.
    from app.cli import backup_archive as bk

    if bk.is_full_archive(src):
        return _restore_full(args, emit, src)

    if not _is_sqlite():
        raise CliError(
            "db restore handles SQLite only. For Postgres use pg_restore "
            "(see docs/BACKUP.md section 2c)."
        )
    # Gate finding 5: the server-running guard (and its honesty line) applies to the
    # legacy single-file path too, not just --full.
    running, where = bk.server_appears_running()
    if running:
        raise CliError(
            f"the server appears to be RUNNING (something is listening on {where}). "
            "A restore replaces the DB out from under a live process. Stop the server "
            "and retry.",
            1,
        )
    emit.detail(
        f"no listener detected on {where} - if your server runs elsewhere, stop it "
        "yourself before continuing (the probe cannot see a different host/port)."
    )

    dst = str(config.DB_PATH)
    if not confirm(emit, f"Replace the current database ({dst}) with {src}?", args.yes):
        raise CliError("aborted.", 1)
    from app.cli.legacy_restore import restore_database

    restore_database(src, dst)
    emit.result(
        f"Database restored from {src}.\nRestart the server. "
        "No audit trust files restored: run `audit reset-anchor` after restarting. "
        "Keep the matching audit key/epoch files; see docs/BACKUP.md.",
        {"ok": True, "restored_from": src, "path": dst, "audit_reset_required": True},
    )
    return 0


# --- db verify -----------------------------------------------------------------
# A handful of PORTABLE (SQLAlchemy Core, no ORM) orphan checks: (label, child
# table, child FK column, parent table, parent PK column, nullable). `nullable`
# controls whether a NULL child FK value is itself checked (it is a legitimate
# "no parent" state for those columns) or only a NON-NULL dangling value is
# flagged. Kept small and explicit ("a few key FKs" per the backlog item) rather
# than iterating every FK in the schema - the goal is a fast, honest smoke check,
# not a full constraint audit (that's what PRAGMA foreign_key_check covers on
# SQLite; this is the portable equivalent for the checks worth running on any
# backend, incl. SQLite where the PRAGMA is skipped for some reason).
_ORPHAN_FKS = [
    ("document.project_id -> project.id", "document", "project_id", "project", "id", False),
    ("segment.document_id -> document.id", "segment", "document_id", "document", "id", False),
    (
        "workflowstep.workflow_id -> workflow.id",
        "workflowstep",
        "workflow_id",
        "workflow",
        "id",
        False,
    ),
    (
        "workflowrun.workflow_id -> workflow.id",
        "workflowrun",
        "workflow_id",
        "workflow",
        "id",
        False,
    ),
    (
        "workflowrun.document_id -> document.id",
        "workflowrun",
        "document_id",
        "document",
        "id",
        False,
    ),
    (
        "steprun.workflow_run_id -> workflowrun.id",
        "steprun",
        "workflow_run_id",
        "workflowrun",
        "id",
        False,
    ),
    ("steprun.step_id -> workflowstep.id", "steprun", "step_id", "workflowstep", "id", False),
    (
        "glossaryentry.project_id -> project.id",
        "glossaryentry",
        "project_id",
        "project",
        "id",
        True,
    ),
]


def _verify(args, emit) -> int:
    """Read-only DB integrity/health check (distinct from `audit verify`, which
    checks the tamper-evident audit HASH CHAIN, not schema/data integrity).

    LOCAL-ONLY BY DESIGN: this inspects the DB the CLI process is itself
    configured against (DATABASE_URL / LOC_TMS_DATA_DIR) via read-only queries -
    exactly like `health`, `db migrate`, and `audit verify`, none of which have a
    --remote arm either (require_local refuses one cleanly, exit 2). A remote
    /api health-check endpoint (operator/admin RBAC) would be a reasonable FUTURE
    addition mirroring a live server's own DB, but is a distinct feature (it
    would report on the SERVER's DB, not "the DB this CLI invocation points at")
    and is left unbuilt here - this command's job is the local/operator surface,
    matching the established precedent of its sibling ops commands.

    Runs three kinds of checks, each independently skippable when not applicable
    (a skip is never a failure):
      1. SQLite PRAGMA integrity_check + foreign_key_check - the storage engine's
         own authoritative checks. Skipped (not failed) on a non-SQLite backend;
         check 3 below is the portable equivalent there.
      2. alembic head vs the DB's stamped alembic_version revision - catches a DB
         that is behind the code (`db migrate` needed). Skipped when no
         alembic_version table exists (a legacy pre-alembic DB, or the hermetic
         TEST create_all+shim path - see app/db.py's init_db docstring), since
         that is an EXPECTED state for those paths, not a corruption.
      3. Portable referential-sanity orphan counts on a few key FKs (SQLAlchemy
         Core queries; work on SQLite AND Postgres) - catches dangling foreign
         keys even where PRAGMA foreign_key_check would (SQLite) or could not
         (Postgres) run.

    Every one of THIS command's own queries is a read (SELECT / read-only PRAGMA)
    - it never writes DB CONTENT. It is not a purely side-effect-free filesystem
    operation, though: like every other in-process command (`health`/`audit
    verify` included), it opens the DB through the app's SHARED engine, whose
    connect-time PRAGMA listener (app/db.py's `_sqlite_pragmas`) sets
    journal_mode=WAL/synchronous=NORMAL/foreign_keys=ON on the FIRST connection
    to a SQLite file - so running this (or any other command) against a
    plain-rollback-journal DB can create -wal/-shm side files and flip its
    journal mode. Same footprint as its sibling ops commands, not a special
    write this command introduces. Exits nonzero if ANY check's status is
    "fail" (an "ok"/"skip" mix is a clean exit 0)."""
    require_local(args)
    from sqlalchemy import inspect as sa_inspect
    from sqlalchemy import text

    from app import config
    from app.db import engine

    dialect = engine.dialect.name
    checks: list[dict] = []

    def add(name: str, status: str, detail: str) -> None:
        checks.append({"check": name, "status": status, "detail": detail})

    # --- 1. SQLite's own integrity checks -------------------------------------
    if dialect == "sqlite":
        with engine.connect() as conn:
            rows = conn.exec_driver_sql("PRAGMA integrity_check").fetchall()
        vals = [str(r[0]) for r in rows]
        if vals == ["ok"]:
            add("sqlite_integrity_check", "ok", "database file structure is sound.")
        else:
            add(
                "sqlite_integrity_check",
                "fail",
                "; ".join(vals[:20]) + (f" (+{len(vals) - 20} more)" if len(vals) > 20 else ""),
            )

        with engine.connect() as conn:
            fk_rows = conn.exec_driver_sql("PRAGMA foreign_key_check").fetchall()
        if not fk_rows:
            add("sqlite_foreign_key_check", "ok", "no foreign-key violations.")
        else:
            # Each row: (table, rowid, parent_table, fkid).
            detail = "; ".join(f"{r[0]} rowid={r[1]} -> {r[2]}" for r in fk_rows[:20])
            if len(fk_rows) > 20:
                detail += f" (+{len(fk_rows) - 20} more)"
            add("sqlite_foreign_key_check", "fail", detail)
    else:
        add("sqlite_integrity_check", "skip", f"not applicable on {dialect} (SQLite-only PRAGMA).")
        add(
            "sqlite_foreign_key_check",
            "skip",
            f"not applicable on {dialect} (SQLite-only PRAGMA); see the "
            "referential-sanity orphan checks below for the portable equivalent.",
        )

    # --- 2. alembic head vs the DB's stamped revision -------------------------
    try:
        from alembic.config import Config
        from alembic.script import ScriptDirectory

        cfg = Config(str(config.BASE_DIR / "alembic.ini"))
        cfg.set_main_option("script_location", str(config.BASE_DIR / "alembic"))
        cfg.set_main_option("sqlalchemy.url", config.DB_URL)
        head = ScriptDirectory.from_config(cfg).get_current_head()
        insp = sa_inspect(engine)
        if not insp.has_table("alembic_version"):
            add(
                "alembic_revision",
                "skip",
                f"no alembic_version table on this DB (pre-alembic/legacy DB, or the "
                f"hermetic-test create_all+shim path); code head is {head}.",
            )
        else:
            with engine.connect() as conn:
                row = conn.execute(text("SELECT version_num FROM alembic_version")).fetchone()
            stamped = row[0] if row else None
            if stamped == head:
                add("alembic_revision", "ok", f"DB is at head ({head}).")
            else:
                add(
                    "alembic_revision",
                    "fail",
                    f"DB stamped revision {stamped!r} != code head {head!r} (run `db migrate`).",
                )
    except Exception as exc:
        add("alembic_revision", "fail", f"could not resolve the alembic revision: {exc}")

    # --- 3. portable referential-sanity orphan counts -------------------------
    insp = sa_inspect(engine)
    existing = set(insp.get_table_names())
    with engine.connect() as conn:
        for label, child, fk_col, parent, parent_col, nullable in _ORPHAN_FKS:
            name = f"orphan:{label}"
            if child not in existing or parent not in existing:
                add(name, "skip", "table not present on this DB.")
                continue
            null_guard = f"c.{fk_col} IS NOT NULL AND " if nullable else ""
            n = (
                conn.execute(
                    text(
                        f"SELECT COUNT(*) FROM {child} c LEFT JOIN {parent} p "  # noqa: S608 -- identifiers from the constant _ORPHAN_FKS
                        f"ON c.{fk_col} = p.{parent_col} "
                        f"WHERE {null_guard}p.{parent_col} IS NULL"
                    )
                ).scalar()
                or 0
            )
            if n == 0:
                add(name, "ok", "no orphaned rows.")
            else:
                add(name, "fail", f"{n} orphaned row(s) (dangling {fk_col}).")

    ok_all = all(c["status"] != "fail" for c in checks)
    # Honesty guard (review finding P2): "no failure found" is NOT the same claim
    # as "verified healthy" when EVERY check skipped (e.g. a non-SQLite dialect
    # with none of the sampled tables present yet) - nothing actually ran, so
    # nothing was actually verified. Exit code stays 0 (no failure IS true), but
    # the summary must not claim a pass it didn't earn.
    all_skipped = bool(checks) and all(c["status"] == "skip" for c in checks)
    if emit.as_json:
        emit.json({"ok": ok_all, "all_skipped": all_skipped, "dialect": dialect, "checks": checks})
    else:
        emit.say(f"db verify ({dialect}):")
        emit.table(
            [(c["check"], c["status"].upper(), c["detail"]) for c in checks],
            headers=("check", "status", "detail"),
        )
        emit.say("")
        if all_skipped:
            emit.say(
                f"SKIPPED: all {len(checks)} check(s) were skipped - nothing "
                "was actually verified (not a failure, but not a pass either)."
            )
        elif ok_all:
            emit.say("PASSED: no integrity failures found.")
        else:
            emit.say("FAILED: one or more checks above failed - see the FAIL row(s).")
    return 0 if ok_all else 1
