"""SQLite engine + schema init for Vendor Studio. Own DB file, own schema."""

from __future__ import annotations

import os

from sqlmodel import Session, SQLModel, create_engine

from vstudio import models  # noqa: F401
from vstudio.config import settings

_engine = None


def _make_engine():
    from sqlalchemy import event
    from sqlalchemy.pool import NullPool

    path = settings.db_path
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    # NullPool: every request/Session gets a FRESH connection that is fully closed on
    # release. Under uvicorn's threadpool a shared QueuePool can pin a connection to a
    # thread and starve later requests (an intermittent hang); NullPool avoids that
    # entirely, which is the right trade for a low-volume SQLite app. WAL + a busy timeout
    # then let concurrent readers and one writer coexist without "database is locked".
    eng = create_engine(
        f"sqlite:///{path}",
        connect_args={"check_same_thread": False, "timeout": 30},
        poolclass=NullPool,
    )

    @event.listens_for(eng, "connect")
    def _set_pragmas(dbapi_conn, _rec):
        cur = dbapi_conn.cursor()
        cur.execute("PRAGMA journal_mode=WAL")
        cur.execute("PRAGMA busy_timeout=30000")
        cur.execute("PRAGMA synchronous=NORMAL")
        cur.close()

    return eng


def engine():
    global _engine
    if _engine is None:
        _engine = _make_engine()
    return _engine


def reset_engine() -> None:
    """Drop the cached engine (tests re-point VSTUDIO_DB_PATH then call this)."""
    global _engine
    if _engine is not None:
        try:  # noqa: SIM105
            _engine.dispose()
        except Exception:  # noqa: S110 -- test reset; the cached engine is dropped regardless
            pass
    _engine = None


def _column_ddl(col) -> str:
    """The SQLite `ADD COLUMN` fragment for a SQLAlchemy column: a name + a SQLite-friendly
    type, plus a NOT NULL DEFAULT for a non-nullable column (an ALTER ADD COLUMN on a table
    that already has rows REQUIRES a default for a NOT NULL column)."""
    import sqlalchemy as sa

    t = col.type
    if isinstance(t, sa.Boolean):
        typ, default = "BOOLEAN", "0"
    elif isinstance(t, sa.Integer):
        typ, default = "INTEGER", "0"
    elif isinstance(t, sa.Float):
        typ, default = "FLOAT", "0.0"
    elif isinstance(t, sa.DateTime):
        typ, default = "DATETIME", "CURRENT_TIMESTAMP"
    elif isinstance(t, sa.JSON):
        typ, default = "JSON", "'null'"
    else:
        typ, default = "VARCHAR", "''"
    ddl = f'"{col.name}" {typ}'
    if not col.nullable:
        ddl += f" NOT NULL DEFAULT {default}"
    return ddl


def _ensure_columns(eng) -> None:
    """Additive-column shim (mirrors app/db.py's _ADDED_COLUMNS, VS-independent). create_all
    NEVER alters an EXISTING table, so a VS DB built before a column was added to a model is
    missing it (e.g. a pre-Wave-E DB lacks vproject.quote_requested / vsegment.feedback and
    every request that reads the model would 500). For every mapped table that already exists,
    ADD any model column absent from the live table (SQLite-safe: additive, one at a time).
    Fully generic, so a future VS column add is covered without touching this function."""
    from sqlalchemy import inspect as _sa_inspect
    from sqlalchemy import text

    insp = _sa_inspect(eng)
    existing = set(insp.get_table_names())
    with eng.begin() as con:
        for table in SQLModel.metadata.sorted_tables:
            if table.name not in existing:
                continue  # create_all just built it with all columns
            have = {c["name"] for c in insp.get_columns(table.name)}
            for col in table.columns:
                if col.name in have:
                    continue
                con.execute(text(f'ALTER TABLE "{table.name}" ADD COLUMN {_column_ddl(col)}'))


def init_db() -> None:
    """Create tables, backfill any missing additive columns on pre-existing tables, and seed
    the default QA check severities (idempotent)."""
    SQLModel.metadata.create_all(engine())
    _ensure_columns(engine())  # additive-column shim for a pre-Wave-E DB
    from vstudio import qa
    from vstudio.models import VCheckConfig

    with Session(engine()) as s:
        from sqlmodel import select

        existing = {c.check_key for c in s.exec(select(VCheckConfig)).all()}
        for key, sev in qa.DEFAULT_SEVERITIES.items():
            if key not in existing:
                s.add(VCheckConfig(check_key=key, severity=sev))
        s.commit()


def get_session() -> Session:
    return Session(engine())
