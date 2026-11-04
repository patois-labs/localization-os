# Move an install from SQLite to Postgres, and run web + workers

Postgres is the recommended database once an install runs more than one process (ADR 0107). SQLite allows one
writer at a time: one web process serves the product well, but several worker processes (`LOC_TMS_ROLE=worker`)
all write job, run and segment rows, and that is where Postgres pays off. SQLite (WAL, one machine) is supported for
the split roles too; Postgres is the recommendation once there are more than a couple of workers.

## The copy command

`loc-tms db copy-to-postgres <sqlite_file> [--target-env NAME] [--check] [--batch-size N]` (host CLI only; it has no
web, API or MCP surface on purpose: it reads a file on the host and writes to a database server the operator names;
see `docs/CLI.md`).

- **Preflight**, read only, refuses with exit 1 and writes nothing: SQLite and target at the **same Alembic
  revision**; the target holds nothing but what the migrations seed (the `alembic_version` row and the default
  organization row, which the copy replaces); no foreign-key orphans in the source (SQLite never enforced them;
  Postgres does); no data in a source table the target lacks. `--check` stops here.
- **Copy, in one transaction**: parents first, in batches; the foreign keys that sit in a cycle or point at their own
  table are dropped for the copy and re-added afterwards (the re-add validates every row); values are converted to
  the Postgres types, NUL characters are removed from text (counted in the report), every serial sequence is reset to
  the highest copied id, and per-table row counts are compared before COMMIT.
- Any failure rolls everything back, including the dropped keys, so the same command can be run again. A second run
  against a finished copy is refused ("target not empty"). To redo a finished copy: drop the database, create it,
  `alembic upgrade head`, copy again.
- The SQLite file is opened read-only and never changed. Uploads and key files are not copied; they stay in the data
  folder.
- The target URL is read from an environment variable (`LOC_TMS_TARGET_PG_URL`, or the name given with
  `--target-env`), never from the command line, because it carries the password. It is redacted in all output.
  `postgres://` and `postgresql://` URLs both work for the copy; the app itself wants `postgresql+psycopg://`.

Rehearsal numbers (a local Postgres container on a desktop): a 4.3 MB install with 5,044 rows copied in about 1.4 s;
a 147 MB copy with 504,044 rows in about 47 s; a 423 MB install with 833,425 rows in about 72 s. That is around
10,000 rows per second locally. A network hop to a managed database is slower: plan on 2 to 4 times that, and
measure on a copy of your own data before you announce downtime.

## Before you start

1. You are on Localization OS 1.1 or newer (`loc-tms db copy-to-postgres --help` works).
2. `job_queue_backend` is `db` (the default since 1.1). The split roles need it.
3. The SQLite install is not SQLCipher-encrypted (`encrypt_db` is 0). If it is, run `loc-tms db decrypt` first.
4. A Postgres server and an empty database exist. Create an owner role for the app and let the same role run
   `alembic upgrade head`: the copy drops and re-adds foreign keys, so the role must own the tables. Use the same
   Postgres major version for `pg_dump` and restore.
5. **Connection budget.** Every process has its own pool of up to `LOC_TMS_DB_POOL_SIZE` + `LOC_TMS_DB_MAX_OVERFLOW`
   connections. With 1 web + N workers the ceiling is (1 + N) times that, against Postgres `max_connections` (100 by
   default). Either lower the pool per process (for example `LOC_TMS_DB_POOL_SIZE=10`, `LOC_TMS_DB_MAX_OVERFLOW=10` on
   workers) or raise `max_connections`, and leave headroom for short audit bookkeeping connections (ADR 0104). The web
   boot warns when the arithmetic does not fit. "Remaining connection slots are reserved" is the usual first outage.

## A. Rehearse on a copy (no downtime, do this first)

1. Copy the data folder to a scratch folder with the app stopped, or use a full backup (`loc-tms db backup-full`):
   the `.db`, `-wal` and `-shm` files together.
2. Bring the copy to the current schema: `LOC_TMS_DATA_DIR=<scratch> loc-tms db migrate`.
3. Create a scratch Postgres database and migrate it:
   `DATABASE_URL=postgresql+psycopg://USER@HOST/scratch alembic upgrade head` (password through the environment or
   `.pgpass`, not the command line).
4. Set `LOC_TMS_TARGET_PG_URL` to the scratch database, run `loc-tms db copy-to-postgres <scratch>/loc_tms.db --check`,
   then the same without `--check`. Write down the time: it is your downtime estimate.
5. Point a throwaway app at the copy (`DATABASE_URL=...scratch...`, `sync_scheduler_enabled=0`, no connectors) and
   check sign-in, a project page, `loc-tms db verify` and `loc-tms audit verify`.
6. Drop the scratch database.

## B. Cutover (downtime)

1. Announce the window and take a full backup with the app stopped (`loc-tms db backup-full`). This is the
   rollback point.
2. Stop the app. Nothing may write to the SQLite file from here on.
3. `loc-tms db migrate` against the SQLite data folder (seconds).
4. On the empty Postgres database: `alembic upgrade head`. This can be done before step 2 to shorten the window.
5. `loc-tms db copy-to-postgres <data dir>/loc_tms.db` with `LOC_TMS_TARGET_PG_URL` set. Exit 0 and "row counts match
   per table" is the gate. Any other result: go to Rollback; the target is untouched.
6. Start the app with `DATABASE_URL=postgresql+psycopg://...` and the **same data folder** (uploads, key files and the
   `.audit_*` trust files stay where they are; the audit chain continues).
7. Verify (section C). Keep `loc_tms.db` and its sidecar files untouched for rollback.

## C. Verify

- `/health` and `/ready` answer 200, and the log has no ERROR lines and shows the migrations already at head.
- `loc-tms db verify` and `loc-tms audit verify` pass.
- One sign-in, one project page, one real translation with a placeholder and a tag.
- `select count(*) from pg_stat_activity` stays under `max_connections` with every process up.
- If you turned the scheduler off for the copy, turn it back on only after this passes.

## D. Process roles: one web process plus N workers

The default changes nothing: `LOC_TMS_ROLE=all` (or unset) is the single process.

- **One container, N workers** (simplest on a single machine): set `LOC_TMS_WORKER_PROCESSES=N` on the app container.
  The entrypoint then runs the web command with `LOC_TMS_ROLE=web` and N `python -m app.worker` with
  `LOC_TMS_ROLE=worker`, and stays PID 1 as a small supervisor: SIGTERM goes to every child, it waits up to
  `LOC_TMS_STOP_WAIT_SECONDS` (default `LOC_TMS_DRAIN_SECONDS` + 10) and then kills stragglers. If any child dies the
  supervisor stops the rest and exits nonzero, so the container's restart policy relaunches the whole group and boot
  recovery reclaims expired leases. Give `docker stop` a grace period above the stop wait (for example `-t 40`).
- **Several web processes**: `LOC_TMS_WEB_PROCESSES=N` (needs `LOC_TMS_WORKER_PROCESSES` of at least 1) runs uvicorn
  with `--workers N` (ADR 0097).
- **Separate containers**: the same image, `LOC_TMS_ROLE=web` in one and `LOC_TMS_ROLE=worker` with the command
  `python -m app.worker` in others, all with the same `DATABASE_URL` and the same data volume (uploads and key files
  must be shared). Every container runs `alembic upgrade head` on Postgres at start, so on an upgrade start the web
  container first, wait for `/ready`, then start the workers.
- How many workers: start with about one per spare core. One worker is already a full Python process; more workers
  than cores only add contention.
- The image's healthcheck calls `/health` on the web process. A dead worker is detected by the supervisor (one
  container) or your orchestrator (separate containers).

## Rollback

Postgres is a one-way door for new data: writes made after the cutover exist only in Postgres, and there is no
Postgres-to-SQLite copy.

- **Before real use** (the copy or the checks failed): stop the app and start it again without `DATABASE_URL` on the
  untouched SQLite file. Nothing was lost.
- **After users wrote to Postgres**: either fix forward on Postgres, or go back to the SQLite file and accept that work
  done since the cutover is lost (list it from the audit log first). Do not edit the SQLite file by hand.
- Keep the SQLite files and the backup until you are sure. From then on back up with `pg_dump` plus the data folder;
  `db restore` does not restore a Postgres database (`docs/BACKUP.md`).
