# 0107. Postgres is the recommended database for multi-process installs; SQLite stays supported

Status: Accepted
Date: 2026-10-25

## Context

The engine redesign (ADRs 0090 to 0106 and 0110) made Postgres a full backend: worker processes, the durable step queue,
cross-process events and conflict retry. Measured on a 16-core test server with the same scenarios, Postgres with
4 web + 8 worker processes served about 1,870 reads/s and 800 saves/s and ran a 60-minute soak at about 800 jobs/min.
SQLite allows one writer at a time, so a split install on SQLite tops out much earlier on saves and concurrent
imports, and one SQLite process uses little more than one core.

## Decision

- Postgres is the recommended database for any install that runs more than one process (split web and worker
  roles, `LOC_TMS_WEB_PROCESSES` above 1) and the target of new feature work.
- SQLite stays a supported backend: the default for a new install, local development, tests and single-process
  installs. Removing it, or ending its support, would be a separate decision.
- An existing SQLite install moves with `loc-tms db copy-to-postgres` (`scripts/sqlite_to_postgres.py`), following
  `docs/guides/postgres-migration.md`.
- Load targets of the engine are judged on Postgres.

## Consequences

- New features may require Postgres where concurrency makes SQLite a poor fit; they say so in their ADR.
- Backups of a Postgres install are a `pg_dump` plus the data directory (uploads, keys, the audit trust files).
