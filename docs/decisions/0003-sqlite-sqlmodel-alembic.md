# 0003. SQLModel over SQLite by default, Postgres supported, Alembic is the schema authority

- Status: Accepted
- Date: 2026-07-03 (SQLite default); 2026-07-10 (Alembic authority on every install)

## Context

Self-hosted installs want zero-dependency storage; hosted and enterprise installs may want Postgres. The
early SQLite `create_all` shim could not change nullability and skipped real migrations on old installs.

## Decision

- SQLModel over SQLite (on disk, WAL mode) is the default; Postgres is supported through
  `DATABASE_URL=postgresql+psycopg://...` (psycopg 3, opt-in `requirements-postgres.txt`).
- Alembic is the schema authority on every real install: `init_db()` runs `alembic upgrade head` at
  boot; on Postgres an un-migrated database is refused. The `create_all` + `_ensure_*` shim is a
  SQLite-only self-heal.
- Optional at-rest encryption via SQLCipher (`LOC_TMS_DB_ENCRYPTION=sqlcipher`, `WITH_SQLCIPHER=1` image).

## Consequences

- Exactly one Alembic head; migrations are forward-only.
- WAL mode means a backup must include `-wal`/`-shm` or be checkpointed (see docs/BACKUP.md).
- SQLite is the default and the most exercised path; Postgres is covered by a CI smoke job.

## Links

- docs/architecture/data-and-migrations.md; docs/BACKUP.md; .claude/rules/migrations.md
