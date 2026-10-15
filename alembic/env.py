"""Alembic environment for Localization OS TMS.

The DB URL is resolved from app.config.DB_URL (the single source of truth:
respects DATABASE_URL for Postgres and LOC_TMS_DATA_DIR for the SQLite default),
NOT from alembic.ini. target_metadata is the full SQLModel metadata, populated by
importing app.models (which registers all 41 table classes).

We deliberately skip alembic's logging.fileConfig so it does not clobber the
app's own logging configuration.
"""
import os
import sys

from alembic import context
from sqlalchemy import engine_from_config, pool

# Make the repo root importable so `from app import ...` works regardless of the
# CWD alembic is invoked from.
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from app import config as app_config  # noqa: E402
from app import models  # noqa: E402,F401  (import registers every SQLModel table)

# Alembic Config object (access to values in alembic.ini, if any).
config = context.config

# Resolve the URL from app.config.DB_URL (the single source of truth: respects
# DATABASE_URL + LOC_TMS_DATA_DIR) UNLESS a caller has already supplied one
# explicitly via Config.set_main_option (e.g. a test harness pointing at a fresh
# throwaway DB). An explicit override always wins.
if not config.get_main_option("sqlalchemy.url"):
    config.set_main_option("sqlalchemy.url", app_config.DB_URL)

# NOTE: intentionally no logging.fileConfig(config.config_file_name) here — the
# app owns logging; alembic must not reconfigure it.

target_metadata = models.SQLModel.metadata


def _render_item(type_, obj, autogen_context):
    """Render SQLModel's AutoString as a plain sa.String in generated migrations.

    Autogenerate otherwise emits `sqlmodel.sql.sqltypes.AutoString()` (and would
    need an `import sqlmodel` in every revision). AutoString is just a String, so
    rendering it as sa.String keeps migrations pure-SQLAlchemy and portable to
    Postgres with no extra import. Returning False falls back to the default
    renderer for every other type (Integer, JSON, DateTime, Boolean, Float, ...).
    """
    import sqlmodel  # local import; only needed during autogenerate
    if type_ == "type" and isinstance(obj, sqlmodel.sql.sqltypes.AutoString):
        return "sa.String()"
    return False


def run_migrations_offline() -> None:
    """Emit SQL to stdout without a live DBAPI connection (--sql mode)."""
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        render_item=_render_item,
        render_as_batch=url.startswith("sqlite"),
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Run migrations against a live connection."""
    # When LOC_TMS_DB_ENCRYPTION=sqlcipher is set on a SQLite backend, the DB file
    # is a SQLCipher database; Alembic must key its connection the SAME way the app
    # does (module=sqlcipher3 + a keying creator) or it can't read/write it. On the
    # plain default path engine_kwargs() is {}, so this is byte-unchanged.
    from app.services import dbcrypt  # local import: only needed here
    app_config.prepare_sqlite_file(config.get_main_option("sqlalchemy.url"))
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
        **dbcrypt.engine_kwargs(app_config.DB_PATH),
    )
    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            render_item=_render_item,
            # batch mode makes ALTER-TABLE migrations work on SQLite (which lacks
            # most ALTER support); a no-op cost on Postgres.
            render_as_batch=connection.dialect.name == "sqlite",
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
