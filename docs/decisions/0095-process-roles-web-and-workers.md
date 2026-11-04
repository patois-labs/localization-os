# 0095. Process roles: one web process and N worker processes

Status: Accepted
Date: 2026-10-23

## Context

Stage S3 of the engine redesign. One Python process served HTTP and ran the
job workers, the workflow-run threads, the scheduler tick and every sweep, so it used about one core (pipeline flat at
about 350 jobs/min on SQLite and Postgres) and pages slowed while jobs ran. Target T6
reproduced it. S1 (ADR 0090) made runs resumable and owned (owner token + heartbeat), S2 (ADR 0092) put every run and
job on durable queues with `SKIP LOCKED` claims on Postgres, and S3a (ADR 0110) added a cross-process events bus.
Those are the pieces a process split needs.

## Decision

- **`LOC_TMS_ROLE` = `all` | `web` | `worker`** (`app/config.py`). `all` is the default and is the single process
  exactly as before. `web` serves HTTP only. `worker` is `python -m app.worker` (`app/worker.py`): no HTTP; it runs
  the job pools, the run admission scheduler with S2 step hand-off, the scheduler tick and sweeps, the S1 stale-run
  and expired-lease sweep and the boot recovery. Run as many workers as there are cores to spare.
- **The web role executes nothing.** `jobs.ensure_workers` and `ADMISSION.ensure_started` are no-ops there; the boot
  skips job/batch/run recovery and the scheduler. A launch (`orchestrator.start_run`) becomes a durable QUEUED row
  through `queue_runs` after the same eager refusals (key trash document, archived workflow/project, FULL_TEXT x
  tm_match, project budget); the double-submit guard re-attaches, a different workflow's run is refused as before; a
  capacity 503 no longer applies (capacity is the workers'). A human-review resume (`resume_run_async`) is ONE
  conditional UPDATE PAUSED -> QUEUED that advances the cursor exactly like the single-process claim and stores the
  edited text in `run.context["_queued_resume"]`; the worker that admits the row pops it and calls the very same
  `_run_in_background(..., seed_text=..., is_resume=True)`. A queued resume (and an S1 restart resume) is never
  re-gated by batch width or a fail-fast stop: it is a run already in flight.
- **Any worker takes the next run.** Workers claim QUEUED rows and jobs from the database (conditional UPDATE on
  SQLite, `FOR UPDATE SKIP LOCKED` on Postgres), so two never take the same one. A run then executes in the worker
  that admitted it, segment by segment (S2); a run whose worker dies is resumed from its last checkpoint by any
  peer's S1 sweep (or the next boot), and the job lease heartbeat hands its jobs back the same way.
- **Exactly one worker runs the scheduler tick and the retention purge** (`app/services/leader.py`). Their sweeps
  (connector syncs, vendor polls and retries, notifications, reapers, purges) are serialized only inside one process,
  so N workers would fire a due connector N times. The `scheduler` leadership is a Postgres session advisory lock on a
  dedicated connection (works across hosts) or an exclusive lock on a file in the data directory (SQLite, one box);
  the others retry every 15 s and take over when the leader stops. A leader whose lock connection fails exits, so two
  leaders never overlap.
- **Live translate streams belong to the web process.** They run in the request (not on the queue), so their orphans
  are failed by the web boot (or the single process's boot), never by a booting worker, which would otherwise fail a
  stream the live web process is still serving.
- **Wake-ups ride the S3a bus** (`app/services/wake.py`). The web role publishes `work:wake` after it queues a job or a
  run; each worker's listener turns it into the local wake-ups. Workers publish `run:<id>` after each step event,
  persisted progress counter and run end; the web role's run stream (`observe_run`) waits on that channel and
  re-reads the rows when an event arrives (at least every 2 s otherwise). Events are hints: the rows stay the truth.
- **Split roles require the durable job queue** (`job_queue_backend=db`, the default since S2): the web boot refuses
  `memory`, and a web-role process that skipped the boot arms the DB queue on its first submit. With split roles the
  boot no longer reports live peer workers' jobs as a "foreign instance".
- **Containers**: `LOC_TMS_WORKER_PROCESSES=N` (default 0 = the single `all` process) makes the entrypoint start N
  workers plus the web process and supervise them (docs/guides/postgres-migration.md, docs/DEPLOY.md).

## Why not the alternatives

- **Move a parked run's next step to another worker.** A run's generator cannot be handed to another process; doing
  it through the S1 checkpoint would re-enter the run (launch guards, budget gates, envelope bookkeeping) on every
  hand-off and only works for the run shapes S1 can resume (no graph routing, no parallel groups). Throughput scales
  with many runs across workers; a single run is one sequence of dependent steps anyway.
- **A broker (Redis, RabbitMQ).** The database is already the queue and Postgres already has LISTEN/NOTIFY; a broker
  would add an operated service for nothing the gates need.
- **Web keeps executing interactive launches.** That puts CPU-heavy steps back on the web process's GIL (T6 check 4)
  and makes capacity per web process again.

## Consequences

- A launch in the web role answers "queued" and the run page shows it QUEUED until a worker admits it (usually well
  under a second with the wake-up). Single-process installs are unchanged.
- The synchronous paths still execute where they are called: the one-shot CLI (`workflow run`, `job run`), the `/api`
  `wait=true` resume, add-on runs and the live translate stream. They are request- or command-scoped by
  design.
- SQLite supports split roles on one box (WAL, one writer at a time); Postgres is the recommendation for
  more than a couple of workers (docs/guides/postgres-migration.md).
- The fair-dispatch cursors (S2) are per worker; fairness holds in expectation, not as one global rotation.

Enforced by `verify_worker_roles` (target T6 promoted), `verify_step_queue`, `verify_fair_dispatch`, `verify_job_pools`.
