# 0110. Cross-process live events: Postgres LISTEN/NOTIFY, SQLite polling table

Status: Accepted
Date: 2026-10-22

## Context

Engine redesign (Python + Rust inside, no rewrite), stage S3 "Multi-process on Postgres",
target T7. Live updates (run progress over SSE) read in-process state, so once a step runs in a worker process (T6)
or a second instance, the web process never hears about it. A common answer is Postgres NOTIFY with one LISTEN
connection per replica fanning out to SSE subscribers. We must also keep the zero-config SQLite install working,
where there is no NOTIFY.

## Decision

- One module, `app/services/events.py`: `publish(channel, payload, session=None)` and `subscribe(channel)` (an
  iterator, plus `poll(timeout)` for loops that need keep-alives). Contract on both backends: a subscription sees
  the events published after it started, per channel in publish order, each once.
- **Postgres:** `pg_notify('loc_tms_events', envelope)`; the app channel rides inside the JSON envelope, so Postgres
  sees one fixed channel. Each process keeps ONE dedicated autocommit listen connection outside the pool (a LISTEN
  pins its connection) and fans out to in-process subscribers. A payload over 7000 bytes (NOTIFY caps at 8000) is
  stored in `liveevent` and the NOTIFY carries its id.
- **SQLite:** every event is a `liveevent` row. One poller thread per process, running only while the process has
  subscribers, reads rows past an id cursor every 250 ms (a local publish wakes it at once). SQLite's single writer
  makes ids visible in order; `sqlite_autoincrement` keeps ids increasing after the retention sweep (10 minutes).
- `publish(session=...)` joins the caller's transaction: the event exists only if that transaction commits (NOTIFY
  semantics on Postgres, the row on SQLite).
- Events are hints, not records: subscribers re-read state from the database. The bus is not durable across a
  listener reconnect, and a full subscriber buffer (10,000) drops the oldest and counts them.
- The table goes through Alembic (`b8d4f2a6c1e9`), is excluded from data export, and no existing SSE stream is
  rewired yet: S3 proper does that together with the worker split.

## Consequences

- Workers and extra instances can drive live progress without sharing memory with the web process.
- SQLite pays one short read every 250 ms per process with subscribers, and one row per event; Postgres pays no table
  writes for normal events.
- Gate: `verify_live_events` (SQLite always; Postgres when `LOC_TMS_TEST_PG_URL` is set) and
  the T7 target (`tests/arch_targets/`).

## Alternatives considered

- A Redis or other broker: one more service to run on a self-hosted install; rejected.
- Polling the run tables directly from each SSE stream (today's shape): no cross-process wake-up and N pollers per run.
- Storing every Postgres event in the table and notifying ids only: extra writes for no gain under 7000 bytes.
