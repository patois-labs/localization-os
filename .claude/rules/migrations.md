---
paths:
  - "alembic/**"
  - "alembic.ini"
  - "app/db.py"
  - "app/models.py"
  - "tests/verify_migrations.py"
---

# Schema and migrations

Why: ADR 0003 (SQLite + SQLModel, Alembic is the schema authority). Architecture: `docs/architecture/data-and-migrations.md`.

- Alembic is the authority on every real install (`init_db()` runs `alembic upgrade head`; on Postgres boot refuses an un-migrated DB). The `create_all` + `_ensure_*` shim is SQLite-only self-heal.
- Exactly ONE head. Chain as of 2026-10-02 (check `alembic heads`): `... a276b2c3d4e5 -> b2c4d6e8f0a1 -> c3d5e7f9a1b2 -> c4e7f2b9a1d3 -> d1a8c5e3b7f2 -> e3b9d5a1c7f4`. When two branches add revisions on the same parent, the SECOND to merge re-points its `down_revision`.
- Revision fields must be annotated (`revision: str = "..."`, `down_revision: ...`) or pregate fails.
- A new nullable column is mirrored in the `_ADDED_COLUMNS` shim in `app/db.py`; a new table is classified in `app/services/data_export_part2.py` (EXPLICIT_EXCLUSIONS etc.) and bumps the `verify_data_export` ratchet; identity columns kept on erasure go in `_IDENTITY_NOT_SWEPT`.
- Postgres: `user` is a reserved word (quote it) and booleans are not integers. `batch_alter_table('user')` once destroyed the unique-email index: check indexes after any batch op.
- Migrations beyond ADD COLUMN are skipped on pre-alembic installs; write them to survive that.
- Migrations are forward-only; rolling back past one needs the pre-deploy snapshot.
- `alembic/` is excluded from ruff on purpose (suites assert migration text). Write migration files with the Write tool, LF.
- A downgrade that drops a column uses native `ALTER TABLE ... DROP COLUMN` on SQLite (precedent `2a98273c3c99`), never `batch_alter_table`: the batch rebuild silently drops expression indexes such as `ux_project_inbox_key`. `verify_migrations::test_downgrade_keeps_every_index` guards it.
- `app/db.py::_clear_dormant_superadmin` runs at every boot; keep it fail-closed.
- Pool timeouts are detected only by `db.is_pool_timeout_error` (SQLAlchemy `TimeoutError` or "queuepool limit"); keep it narrow.
