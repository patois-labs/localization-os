"""Always-on SQLite-at-rest encryption via SQLCipher (opt-in, env-gated).

This module is the single source of truth for turning a SQLite engine into a
SQLCipher (encrypted-on-disk) one. Both the app (`app/db.py`) and Alembic
(`alembic/env.py`) route through here so they key the connection identically.

Mode is chosen at INSTALL time by `LOC_TMS_DB_ENCRYPTION=sqlcipher` (see
`config.DB_ENCRYPTION_ON`) — never a runtime Setting, because the engine is built
once at import. The plain-SQLite default path never imports the optional
`sqlcipher3` binding: `enabled()` short-circuits on the env flag first.

Binding: `sqlcipher3` (SQLCipher's own sqlite3-compatible DBAPI, ships a
cp313 win_amd64 wheel). It is a drop-in for stdlib `sqlite3`, so SQLAlchemy's
standard pysqlite dialect drives it unchanged when we pass `module=sqlcipher3`
plus a keying `creator` — the app keeps its normal `sqlite:///` URL, WAL PRAGMA
listener, and everything else. We key inside the creator (not via the
`sqlite+pysqlcipher` dialect) precisely so the app's existing sqlite dialect and
event hooks stay byte-identical.

Documented in: docs/architecture/secrets-and-crypto.md
"""

import logging

from app import config

log = logging.getLogger("loc_tms.dbcrypt")

_KEY_PRAGMA = "PRAGMA key = \"x'{hexkey}'\""


def enabled() -> bool:
    """True only when opt-in SQLCipher mode is active on a SQLite backend."""
    return config.DB_ENCRYPTION_ON


def _import_binding():
    """Import the optional sqlcipher3 DBAPI, or raise a clear, actionable error."""
    try:
        from sqlcipher3 import dbapi2 as sqlcipher  # type: ignore
    except ImportError as exc:  # pragma: no cover - only without the optional dep
        raise RuntimeError(
            "LOC_TMS_DB_ENCRYPTION=sqlcipher is set but the optional 'sqlcipher3' "
            "binding is not installed. Run: pip install sqlcipher3 "
            "(see requirements.txt / docs/SECRETS.md)."
        ) from exc
    return sqlcipher


def key_connection(dbapi_conn, hex_key: str | None = None) -> None:
    """Apply the SQLCipher key PRAGMA to a freshly-opened DBAPI connection.

    Uses the raw-hex key form (no KDF ambiguity); the 32-byte key is derived from
    ENCRYPTION_KEY by config.sqlcipher_hex_key(). `hex_key` overrides it (backup
    verification with the archive's own key, #525)."""
    cur = dbapi_conn.cursor()
    try:
        cur.execute(_KEY_PRAGMA.format(hexkey=hex_key or config.sqlcipher_hex_key()))
    finally:
        cur.close()


def connect(db_path, hex_key: str | None = None):
    """Open a keyed SQLCipher connection to `db_path` (a plain sqlcipher3 conn).

    Shared by the engine creator, Alembic, and the convert tool so keying is
    identical everywhere."""
    sqlcipher = _import_binding()
    from pathlib import Path

    if str(db_path) != ":memory:":
        config.prepare_private_file(Path(db_path))
    conn = sqlcipher.connect(str(db_path), check_same_thread=False)
    key_connection(conn, hex_key)
    return conn


def engine_kwargs(db_path):
    """Extra create_engine kwargs that turn a `sqlite:///` engine into SQLCipher.

    Returns {} when encryption is off, so the default engine construction is
    byte-unchanged. When on: swap the DBAPI module to sqlcipher3 and supply a
    keying creator. The app keeps its normal sqlite URL + dialect + event hooks."""
    if not enabled():
        return {}
    sqlcipher = _import_binding()
    log.info("SQLCipher always-on DB encryption ACTIVE (encrypted at rest).")
    return {
        "module": sqlcipher,
        "creator": lambda: connect(db_path),
    }
