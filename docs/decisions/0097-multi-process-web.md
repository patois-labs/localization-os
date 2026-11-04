# 0097. Multi-process web: N web processes as a supported layout

Status: Accepted
Date: 2026-10-23

## Context

Stage S3c of the engine redesign. S3b (ADR 0095) split the work into one
web process (`LOC_TMS_ROLE=web`) and N worker processes. Its Postgres gates showed the single web process as the
ceiling: about 22 requests/s with the box at 450 of 1,200 % CPU, pipeline 737 jobs/min against a 1,400 target. A
3-web-process experiment (`uvicorn --workers 3`) raised the pipeline to 1,050-1,150 jobs/min, but read p95 went from
0.83 s to 1.55 s and save p95 from 2.41 s to 5.2 s, and the layout was not supported: every web process ran the
boot cleanups (which fail the rows a live peer is serving) and kept its own rate-limit windows, SAML replay ids and
one-time stash.

Measured causes of the worse tails (S3c probe):

1. **Uneven load across web processes.** The processes share one listening socket and a kept-alive connection stays
   with the process that accepted it, so the harness's 50 connections split unevenly: per-process CPU 60 / 35 / 29 %
   at read-50. The busiest process sets the p95.
2. **One write per authenticated request on one row.** `apikeys.verify_key` read the key row again and committed an
   `UPDATE apikey SET last_used` on EVERY request. All requests of all processes sharing a key queued on that row lock
   (and a commit each).
3. **Audit-chain lock starvation.** Every mutating request appends to the audit hash chain under a cross-process file
   lock (`audit_state.process_lock`). A process kept the OS lock for as long as ANY of its threads was a user; under
   steady load that count never reached zero, so peers polled every 10 ms up to the 30 s timeout. That is the save
   tail.
4. **The load generator.** One Python asyncio process drives the load; at 70 to 100 requests/s it uses most of a core
   and its own event-loop delay lands in the measured latency. Read ladders above that rate measure the generator.

## Decision

- **`LOC_TMS_WEB_PROCESSES=N`** (`app/config.py`, default 1). The container entrypoint adds `--workers N` to the
  uvicorn command; it needs the split roles (`LOC_TMS_WORKER_PROCESSES` of at least 1, `LOC_TMS_ROLE=web`). The HTTP
  startup of the `all` role refuses N above 1 (each copy would run the job pools and the scheduler); importing the
  configuration does not, so the container's `alembic upgrade head` and every one-shot CLI keep working.
- **One boot at a time.** Web and worker processes take `leader.boot_lock()` around their boot (a blocking Postgres
  advisory lock on a dedicated connection, or a polled file lock on SQLite). The steps are the single process's,
  run serially, so seeding and schema shims never race. The `all` role takes no lock.
- **Cross-process check-then-insert.** The job dedupe doors (`jobs.submit_unique`, `submit_unique_budgeted`: a
  double-clicked Sync now or Translate pending now) and the background-call idempotency lookup take
  `leader.mutex(name)` in the split roles (a Postgres advisory lock on a pooled connection, or a file lock on
  SQLite), so the read and the insert stay atomic when the two requests land on two processes.
- **Boot cleanups are owner-aware.** Rows executed inside a web process (a live translate stream, a background `/api`
  call) carry `payload.owner = jobs.instance_id()` in the web role. Each web process holds a presence lock (Postgres
  advisory lock, or a file lock) for its lifetime; a booting web process fails only rows whose owner is gone
  (`leader.present`), plus ownerless rows (the old rule). Add-on runs were already heartbeat-based. The boot fails
  if the presence lock cannot be taken, and a watcher retakes it if its connection drops (or exits the process, like
  a leader that lost its lock). SQLite presence files of gone processes are swept at boot.
- **Shared state for web-to-web stores**, only when N is above 1: table `sharedstate` (`app/services/shared_state.py`)
  holds the `/api` rate-limit windows (a fixed window per identity with slab reservation, so about one write per ten
  requests; it may refuse up to N x slab hits early per window), SAML assertion replay ids, and the one-time stash
  (encrypted at rest, deleted on read). With one web process these stay in memory exactly as before.
- **Connection rebalancing**, only when N is above 1: every 16th response closes its keep-alive connection, so clients
  come back through accept, which the least busy process wins.
- **Hot-path diet** (every role): the key and its owner in one joined read, `last_used` stamped at most once a minute
  with one conditional UPDATE and no read, the owner handed to `api_identity`; `GET /api/jobs/{id}` went from 6
  statements and a write to 2 reads; the segment list checks for a paused human-review run once per document instead
  of once per language; the project list reads its custom-field definitions once; the project page reads its target
  languages once.
- **Fair audit lock** in the split roles: a process holding the audit OS lock longer than 50 ms stops admitting new
  local writers, releases once they finish and pauses 6 ms so a waiting peer (polling every 2 ms) gets it. Nested
  entries never wait. The `all` role keeps the old behaviour.
- **Connection budget warning.** On Postgres the web boot warns when `(web + worker processes) x (pool + overflow +
  2)` exceeds `max_connections - 5`.

## Measured (Postgres, a 6-core desktop, 3 web + 6 worker processes)

The later change of the audit offload from the shared 16-token pool to its own 4-token limiter was not re-measured.

| Gate | S3b (1 web + 6 workers) | S3c | Target |
|---|---|---|---|
| Pipeline, 192 jobs in flight | 737.5 jobs/min | 884.8 jobs/min | 1,400 |
| Job p95 at 192 in flight | 16.4 s | 17.1 s | 10 s |
| Read p95, 50 users (one driver) | 0.83 s | 1.47 s at 100 req/s (0.34 s with 4 drivers) | 1.0 s |
| Save p95, 100 users | 2.41 s | 3.40 s at 58.5 req/s | 1.0 s |
| Soak, jobs/min with 50 readers | 113 (30 min) | 284.6 (60 min) | 300 |

Same-box A/B, 3 web processes each (the build before this change vs this change): write-50 55.1 -> 67.6 req/s (p95 1.91 -> 1.54 s),
write-100 43.2 -> 56.5 req/s (p95 6.05 -> 4.34 s), pipeline-32 826 -> 936 jobs/min. The pipeline is CPU-bound
at about 0.4 core-seconds per job on this 6-core desktop; the save ladder is bounded by the single audit chain (about
100 appends/s on Postgres); the read ladder above about 95 req/s by the single Python load driver.

## Why not the alternatives

- **A reverse proxy balancing N ports** (Caddy `lb_policy least_conn`): balances per request and is the better answer
  behind a proxy, but it is a deployment change per install; one socket with rebalancing works everywhere, and
  an install can still add per-port upstreams later.
- **Redis for the shared stores.** One more operated service for three small stores; the database already is the
  shared state.
- **Route the audit chain through Postgres advisory locks.** The chain's trust files (witness, key epochs) live in the
  data directory and are published under the same lock; replacing the lock is a security-review change of its own.
  Fairness fixes the measured starvation without changing what is locked.

## Consequences

- `ApiKey.last_used` is "last used, to the minute".
- The shared rate limit is a fixed window: across a window boundary a client can get up to twice its per-minute
  budget within a few seconds (the in-memory limiter of one process is a sliding window). Once a process sees a window
  used up it refuses locally until the window ends, so refusals cost no database write.
- `MAX_BACKGROUND` (8 background `/api` calls at once) is per web process.
- A role-`all` CLI process that writes audit rows does not mark itself waiting, so a fair holder does not yield to it;
  under steady load it can still wait up to the 30 s timeout, as before S3c.
- With N web processes the per-IP sign-in throttle layer stays per process (its budget is N times larger); the durable
  per-account lockout (`LoginThrottle`) is shared and remains the boundary.
- Read and write ladders on one machine are bounded by the Python load generator near 70 to 100 requests/s per driver
  process; claims above that need the generator on another machine or split across processes.
- Gunicorn `--preload` is not supported (all workers would share one instance id).
- The middleware audit append runs on a worker thread in the split roles (only for requests that can get a row; reads
  and the liveness probes stay on the event loop), so a web process waiting its turn for the audit lock no longer
  freezes its other requests; it runs on its own small thread limiter (`app/web/streaming.py::run_audit`), never on
  the shared sync-route pool. The single `all` process keeps it on the loop.
- Known low-severity limits found at review: on Postgres a
  `leader.mutex` waiter holds a pooled connection while it waits (LOW-A); the background-call mutex can block the
  event loop while a peer holds the same bucket (LOW-B); the SQLite presence-file sweep can race a starting peer on
  Linux and macOS (LOW-C); `leader.mutex` is a no-op for role-`all` CLI processes next to a split deployment (LOW-D).

Enforced by `verify_multi_web` (real `uvicorn --workers 2` + a worker), `verify_shared_state`,
`verify_audit_fair_lock` (3 processes x 8 threads on one chain), `verify_worker_roles`.
