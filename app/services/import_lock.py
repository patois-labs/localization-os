"""Documented in: docs/architecture/tm-and-glossary.md

Short-write-lock helpers shared by the TMX, TBX and glossary CSV importers (Issue #584).

SQLite has ONE writer. An import that wrote while it parsed held that lock for the whole
parse (minutes at the #576 row cap), so every other writer on the install waited out the
5 s busy_timeout and failed with "database is locked". The importers are now two-phase:
PHASE 1 parses and validates the whole file and does every read it needs, writing
nothing; PHASE 2 takes the write lock, bulk-writes the rows it built in memory and commits
once. The import stays all-or-nothing, and the lock is held for seconds.

#615: the TMX importer now commits its phase 2 in chunks of at most tm.TMX_WRITE_CHUNK_ROWS rows
(one begin_write_phase per chunk) so the lock is held for fractions of a second even at 300k units;
see tm._import_tmx_attempt and ADR 0061 for what that does to atomicity.

`begin_write_phase` is the start of phase 2. It opens the SAVEPOINT the writes live in and
immediately takes the write lock with a no-op UPDATE, so (a) a failure rolls back to the
savepoint exactly as before, and (b) the lock is acquired BEFORE the importer re-checks
that what it read in phase 1 is still true, instead of at some later first INSERT (which
would be the SQLITE_BUSY_SNAPSHOT shape: read, another connection commits, then write).
"""

from __future__ import annotations

import logging

from app import config

log = logging.getLogger(__name__)


class ImportScopeChanged(ValueError):
    """Another connection changed rows this import relied on between its read phase and
    its write phase. The importer re-reads and retries once (ImportScopeChanged is a
    ValueError so a second failure surfaces as an ordinary, user-readable refusal)."""


def data_version(session) -> int | None:
    """SQLite `PRAGMA data_version` on the session's connection: it changes when ANOTHER
    connection commits. None on other backends (the caller then skips the re-check)."""
    if not config.IS_SQLITE:
        return None
    try:
        return session.connection().exec_driver_sql("PRAGMA data_version").scalar()
    except Exception:  # pragma: no cover - a backend without the pragma re-checks always
        log.debug("PRAGMA data_version unavailable", exc_info=True)
        return -1


def begin_write_phase(session, model):
    """Open the write SAVEPOINT and take the write lock now (a no-op UPDATE on `model`'s
    table writes nothing but starts the write transaction). Returns the savepoint.

    SQLite only: PostgreSQL has no single writer lock to take (and `WHERE 0` is not a
    boolean there), so the savepoint alone is opened. If taking the lock fails (the
    busy timeout ran out), the savepoint is rolled back before the error propagates, so
    the caller's session is not left holding a half-open nested transaction."""
    savepoint = session.begin_nested()
    if not config.IS_SQLITE:
        return savepoint
    table = model.__tablename__
    try:
        session.connection().exec_driver_sql(f"UPDATE {table} SET id = id WHERE 1 = 0")  # noqa: S608 -- table from the model, not user input
    except BaseException:
        rollback_quietly(session, savepoint)
        raise
    return savepoint


def rollback_quietly(session, savepoint) -> None:
    """Undo a failed write phase without losing the caller's session."""
    if savepoint is None:
        return
    try:
        savepoint.rollback()
    except Exception:
        # Last resort: a failed import must never ride a later commit into the DB.
        try:
            session.rollback()
        except Exception:
            log.warning("import: savepoint and session rollback failed", exc_info=True)
