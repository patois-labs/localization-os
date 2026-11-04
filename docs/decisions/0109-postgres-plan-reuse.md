# 0109. Postgres reuses query plans across pooled checkouts

Status: Accepted
Date: 2026-10-26

## Context

Nothing in the app was ever server-side prepared on Postgres. psycopg 3 prepares a statement on its 5th execution
on a connection, but `Connection.rollback()` throws away its prepared statements and the execution counts whenever
the connection was in a transaction. Every pooled checkout ends in a rollback (the Session's, then the pool's
reset on return), so Postgres parsed and planned every statement of every request (0.05 to 0.2 ms each; an editor
save runs about 11). The save-path work found it.

Postgres itself keeps a prepared statement across ROLLBACK (PREPARE is not transactional). psycopg clears on
rollback to stay safe in the cases where a kept statement could be missing or stale: a Parse inside a failed
transaction or an aborted pipeline is never created, and a rolled-back DDL change can leave a statement pointing
at a schema that no longer exists.

## Decision

- The pooled engine opens its psycopg connections as `app/db.py::_plan_reuse_connection_class`: its rollback()
  keeps the prepared statements only when the transaction is in good standing (INTRANS, no pipeline) and every
  statement since the last clear began with a plain keyword (reads, DML, CTEs, SET/LOCK, SAVEPOINT/RELEASE).
  A failed transaction, DDL, a rollback to savepoint or any other statement clears exactly as stock psycopg does.
- The ROLLBACK itself is unchanged: every transaction ends the way it did, nothing stays open between requests,
  and `pool_reset_on_return` stays "rollback". psycopg's defaults stay (`prepare_threshold` 5, `prepared_max` 100).
- Postgres through psycopg only. SQLite, other drivers and the pool-less emergency engines are untouched.
- The fix rests on psycopg internals (`Connection._prepared`, `PrepareManager.get/clear`); `verify_pg_plan_reuse`
  pins them so a psycopg upgrade that moves them fails a suite instead of silently restoring re-planning.

## Consequences

- Postgres CPU per request drops and throughput rises where Postgres shares the CPUs with the app.
- About 1 MiB of plan memory per backend at about 80 statements (40 pooled connections: about 50 MiB at most).
- Most statements switch to a generic plan after 5 custom ones (Postgres's `plan_cache_mode=auto`). Every hot
  statement is an id or foreign-key lookup, where a generic plan is the right plan. If a skewed query (one very
  large org or project) turns slow, the fallback is `plan_cache_mode = force_custom_plan` (for the app role, or
  per statement); watch p95 after an upgrade. `pool_recycle` (1 hour) and `prepared_max` bound how long and how
  many statements a connection keeps.
- A schema change made by another process while the app runs (not how deploys work: migrations run before start)
  can fail a cached statement once per connection ("cached plan must not change result type"); the error aborts
  that transaction, whose rollback then clears the cache.

## Links

- ADR 0106 (read and save request cost), ADR 0107 (Postgres recommended).
