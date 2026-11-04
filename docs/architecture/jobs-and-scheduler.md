# Jobs & Scheduler: Architecture (L1)

## 1. Role

This subsystem is the **background-work engine and its timer**. Long-running work (document
MT, connector runs, embedding backfills, data exports) must never execute inside an HTTP
request handler, so routes call submit, get a Job id instantly, and the UI polls; daemon
worker threads pull ids off a queue and run the registered handlers, each in its own DB
sessions. The scheduler is one daemon thread that wakes on a fixed tick, finds connector
connections whose schedule is due, and enqueues the exact same connector job the "Translate
now" button uses, so progress, history, failure capture, and restart recovery all stay the
job engine's problem.

The **dispatch seam is the JobQueue port** (`app/ports/jobqueue.py`, documented in
kernel-and-ports): enqueue, claim, release, and recover route through it, so a default
install runs on the durable DB queue (`job_queue_backend=db`, the default since 1.1; the
in-memory queue is an opt-out). Jobs run on two worker pools, short and long, so an export or
a sweep never waits behind translate or workflow work. The **passage of time
is the TimeDriver port** (`app/ports/scheduler.py`), so the tick can be driven in-process
or by an external cron.

Crash safety is explicit. The in-memory opt-out dies with the process, so recovery at boot
re-queues QUEUED rows and fails RUNNING ones (work is never silently lost, only visibly
failed); the durable default returns eligible interrupted RUNNING jobs to pending under
the host/PID and lease fence described below, so they re-run. The one-shot CLI has no
daemon workers, so it runs handlers synchronously in the calling process. The scheduler tick also carries a set of piggybacked lifecycle sweeps
(auto-close, vendor expiry, orphan reapers, scheduled audit-chain verify, LLMemory and
data-export housekeeping) that run regardless of the connector-sync kill switch because
they are DB-only housekeeping, while every network-egress sweep sits below that switch.

It does **not** own the queue or timer interfaces themselves (those are ports,
kernel-and-ports), the connector logic it dispatches (connectors), or the workflow-run
lifecycle a launched workflow follows (workflow-engine); the unified /jobs history read
folds both Job rows and workflow runs into one list.

## 2. Invariants

- **Submitting is instant and never runs work in the request.** `app/services/jobs.py::submit`
  writes a Job row, ensures workers, and enqueues through the port, returning the id.
  Enforced by `verify_jobs`.
- **A duplicate in-flight job is coalesced to one.** `app/services/jobs.py::submit_unique`
  checks-then-inserts under `app/services/jobs.py::_submit_lock` so an active run makes both
  the scheduler and the manual sync route reuse the live job. Enforced by `verify_schedule`
  (the submit_unique dedup for an active connector_translate run); the lock's atomicity is a
  code property the suite does not race.
- **An interrupted run is recovered by policy, never silently lost.**
  `app/services/jobs.py::recover_interrupted` delegates to the active queue adapter: the
  memory default re-queues QUEUED and fails RUNNING, the durable backend re-runs interrupted
  RUNNING, and the boot caller reclaims unconditionally while the live sweep reclaims only
  expired leases. Enforced by `verify_job_queue`.
- **A live long job is never dispatched twice.** A process-wide renewer thread extends the
  lease of every job this process's DB-queue workers hold (every 30 s, or lease/3 for a short
  lease; default lease 300 s), so the lease-only live sweep never reclaims it, while a dead
  process stops renewing and its rows expire within one lease. Enforced by
  `verify_job_reliability`.
- **A transient failure retries instead of failing the job.** A handler raising
  `app/services/jobs.py::TransientJobError` sends the job back to QUEUED (`Job.retries` + 1,
  message `Retry n of 4 in s s: ...`), the booked `cost_usd` is kept and accumulates, and it is
  re-dispatched after a backoff of 2, 4, 8, 16 s with jitter (`retry_after` overrides). After
  `RETRY_MAX` (4) retries it FAILS with the last error and the retry count. Only that class
  retries; `is_transient` classifies HTTP 429/502/503/504, timeouts and connection resets for
  handlers to re-raise. No handler raises it yet (engine calls inside steps already retry
  429/5xx and timeouts in `app/services/providers.py`); wiring the translate and connector
  handlers is a follow-up. Enforced by `verify_job_reliability`.
- **Shutdown hands running jobs back, it does not fail them.** `app/services/jobs.py::drain`
  (called first in the app `_shutdown`, `LOC_TMS_DRAIN_SECONDS`, default 8 so it fits inside `docker stop`'s default 10 s; raise both together) stops workers
  claiming, waits for running jobs, then returns each still-RUNNING job to QUEUED (DB queue: the
  row to pending, worker and lease cleared) so the next boot re-runs it. A handler thread that
  outlives the drain cannot overwrite the handed-back job. Enforced by `verify_job_reliability`.
- **Short jobs never wait behind long ones.** Two pools: `LOC_TMS_JOB_WORKERS` (default 2)
  LONG workers named `loc-job-N` and `LOC_TMS_SHORT_JOB_WORKERS` (default 2, minimum 1) SHORT
  workers named `loc-sjob-N`. `app/services/jobs.py::LONG_KINDS` (a mutable set, read live at
  claim time: `translate_document`, `connector_translate`, `tm_import`, and an add-on's `backfill_embeddings`) names the
  long kinds; every other kind (today `data_export`) is short. A short worker claims only short
  kinds; a long worker claims any kind but takes long kinds first, so the install stays
  work-conserving, a short job is never queued behind a translate run (the short workers take
  it) and a flood of short jobs cannot starve translation. The per-engine
  translate cap, transient retry and drain apply to both pools (`drain` covers every running
  job). Enforced by `verify_job_pools`.
- **The durable queue is the default and survives restarts.** `job_queue_backend` defaults to
  `db`; `configure_at_boot` registers `DbJobQueue` (and fails the boot loudly only when the
  `jobqueue` table is missing). `memory` is the opt-out. On Postgres the claim candidate is
  picked with `FOR UPDATE OF q SKIP LOCKED` inside the conditional UPDATE, one statement
  (`app/ports/jobqueue.py::_pg_claim_sql`); SQLite keeps the select then compare-and-swap.
  Enforced by `verify_job_pools` and `verify_job_queue`.
- **One idle claimer per enqueued job (ADR 0101).** `DbJobQueue` counts wake-ups per pool: each
  enqueue (in this process, or a `jobs` poke from another, `app/services/wake.py::_wake_local`)
  adds one signal and exactly one idle claimer that can serve that pool takes it and runs the
  claim query; a claim that wins without one spends a pending signal. Idle claimers otherwise
  share ONE timed poll per pool set about every `_POLL_SECONDS` (`DbJobQueue._await_turn`), the fallback
  for rows no wake-up announced; the worker loop passes `idle=True` after an empty claim so its
  1 s re-entry does not query again. Before, every enqueue woke every idle claimer thread of every
  worker and each idle thread polled every second: about four claim calls per job and a quarter
  of a worker's CPU under a translate load. A `jobs` poke no longer wakes the run admission
  scheduler (a `runs` poke does; `work` wakes both). Enforced by `verify_job_pools`.
- **Rows the in-memory queue left behind are picked up at boot.** The memory backend never
  writes `jobqueue` rows, so after an upgrade (or a switch from `memory` to `db`) a Job it left
  QUEUED or RUNNING has no queue row. At boot on the durable queue
  `app/services/jobs.py::adopt_unqueued_rows` enqueues such QUEUED rows and, in the `all` role,
  fails such RUNNING ownerless rows, the in-memory restart contract; only rows written before
  the process started, so a peer's submit is never raced. It runs once per switch: the first
  role-`all` boot writes `.jobqueue-adopted` in the data dir (`jobs._adopt_once_after_switch`), a
  memory-queue boot removes it, and worker boots never write it (they adopt QUEUED rows only and
  log the RUNNING ones they leave). Without a writable, persistent data dir it runs every boot. `drain` also waits for claims still
  open when it begins, and a claim that lands during a drain is put straight back. Enforced by
  `verify_job_queue_upgrade` and `verify_job_reliability`.
- **The one-shot CLI runs handlers synchronously and starts no workers on the durable
  queue.** `app/services/jobs.py::run_sync` executes in the calling thread and records the
  same lifecycle rows. A CLI process that armed `DbJobQueue` sets
  `jobs.set_cli_no_workers(True)`, so `ensure_workers` (also reached from `recover`) starts no
  daemon threads that would claim jobs the server owns and die with the process; `cli.main`
  clears the flag on return. Enforced by `verify_jobs` and `verify_job_pools`.
- **Background work runs in the roles that execute it, never in the web role.**
  `LOC_TMS_ROLE` (`app/config.py::ROLE`) is `all` (default: one process does everything, as
  before), `web` (HTTP only) or `worker` (`app/worker.py::main`, `python -m app.worker`).
  In the web role `jobs.ensure_workers` and the run admission daemon are no-ops and the boot
  skips job/batch/run recovery and the scheduler (`app/main.py::recover_background_work` runs
  only where `app/config.py::runs_background_work` is true); a submitted job lands on the
  durable queue (armed lazily if this process skipped the boot) and `app/services/wake.py::poke`
  publishes `work:wake` so a worker's `app/services/wake.py::listen` thread claims it at once
  instead of on its next poll. A worker runs the pools, the run admission scheduler, the
  scheduler tick and its sweeps (only the ONE worker holding the `scheduler` leadership,
  `app/services/leader.py::run_when_leader`, so a due connector never syncs twice), and stops on SIGTERM/SIGINT with the same drain as the web
  shutdown. Split roles need `job_queue_backend=db` (the memory backend refuses to boot
  there). Enforced by `verify_worker_roles`. ADR 0095.
- **Enabling a schedule never fires immediately, and a slot is consumed before submit.**
  `app/services/scheduler.py::set_schedule` puts the first fire one full interval out, and
  `app/services/scheduler.py::tick` advances `sync_next_at` before submitting, so a crash
  skips a run and never duplicates one. Enforced by `verify_schedule`.
- **The connector-sync kill switch stops sync and all scheduler egress, not the DB-only
  sweeps.** `app/services/scheduler.py::enabled_globally` gates the connector fires and the
  vendor network sweeps, while auto-close, expiry, reapers, and audit-chain verify run above
  it. Enforced by `verify_schedule`, which proves the above-switch principle on the
  representative LLMemory sweeps (they still run with the kill switch off).
- **A live interval is floored.** `app/services/scheduler.py::set_schedule` clamps a nonzero
  interval to `app/services/scheduler.py::MIN_EVERY_MINUTES` for every caller. Enforced by
  `verify_schedule`.

## 3. Where the work happens

| Concern | Pin | One line |
|---|---|---|
| Submit + dedup | `app/services/jobs.py::submit` · `app/services/jobs.py::submit_unique` · `app/services/jobs.py::HANDLERS` | Instant enqueue, atomic same-key coalescing, and the handler registry. |
| Budgeted submit | `app/services/jobs.py::submit_budgeted` · `app/services/jobs.py::submit_unique_budgeted` · `app/services/jobs.py::LiveBudgetRefused` | Re-checks the department cap and inserts the Job under one lock, so concurrent translate submits cannot all pass the cap before any is counted (#503); a refusal raises `LiveBudgetRefused`. The check and the insert (and `reserve_live_translate`) share one database transaction that holds a write lock on the budget rows (`budgets.check_locked`: `SELECT ... FOR UPDATE` on Postgres, a no-op UPDATE as the first statement on SQLite), so separate processes or `LOC_TMS_JOB_WORKERS` workers cannot both take the last of a cap (#622). |
| Workers + execution | `app/services/jobs.py::ensure_workers` · `app/services/jobs.py::_worker_loop` · `app/services/jobs.py::_execute` | The two daemon pools (long workers claim `("long", "short")`, short workers `("short",)`) and the QUEUED to COMPLETED/FAILED lifecycle. |
| Pools | `app/services/jobs.py::LONG_KINDS` · `app/services/jobs.py::pool_for` | A kind's pool is `long` when it is in `LONG_KINDS` (read live), else `short`. The DB adapter joins `job.kind` at claim time (no extra column); the memory adapter gets the pool at `enqueue(job_id, pool)` and keeps one FIFO per pool. |
| Per-engine translate limit | `app/services/jobs.py::_EngineJobSlot` | An engine's `config.max_parallel_jobs` (1 to 64) caps concurrent translate jobs on it, only beneath the global worker cap `LOC_TMS_JOB_WORKERS`; unset = no limit (Issue #613). A queue worker that claims a job over its engine's cap does not wait: the job stays QUEUED (never shown RUNNING), goes back to the tail of the queue in one step (`JobQueue.requeue`, both backends), the worker pauses 1 s and takes the next one, so a capped backlog never starves other engines or job kinds and never spins a worker (a one-shot CLI run does wait). Order among waiting jobs is roughly FIFO. The slot count lives in the server process: a one-shot CLI run and a second app instance on the same DB each count their own. Raising `LOC_TMS_JOB_WORKERS` also raises paid engines unless each has its own `max_parallel_jobs`. Raise the worker cap before the engine cap, and expect more CPU per extra running job (177% of 2 vCPU with two jobs in the 2026-10-07 test). |
| Synchronous + recorded runs | `app/services/jobs.py::run_sync` · `app/services/jobs.py::record_sync_job` | The CLI in-process run and the after-the-fact Job row for work that ran outside the engine. |
| Partial failure | `app/services/jobs.py::partial_failure_error` · `app/services/jobs.py::run_error` · `app/services/jobs.py::last_failed_translate_run` | A translate run with failed segments ends FAILED with the count and first engine error (#612); the export hold reads the newest failed run. Image localization follows the same rule (#624; see image-localization). |
| Crash recovery | `app/services/jobs.py::recover_interrupted` | Delegates boot and live recovery to the active queue adapter. |
| Lease heartbeat | `DbJobQueue.renew` | Renews held leases from one daemon thread per process; the memory adapter has no lease (no-op). |
| Transient retry | `app/services/jobs.py::TransientJobError` · `app/services/jobs.py::retry_delay` · `app/services/jobs.py::_transient` | Requeue with backoff through `JobQueue.requeue(job_id, delay, error)`: the DB row goes back to pending with `not_before` (claim skips it until due) and `last_error`; the memory queue re-enqueues from a daemon timer so no worker sleeps. |
| Graceful drain | `app/services/jobs.py::drain` · `app/services/jobs.py::resume_after_drain` · `JobQueue.hand_back` | Stop claiming, wait, hand running jobs back to QUEUED. Live-stream translate rows are not queue jobs and are left to `fail_orphaned_live_rows`. The container stop timeout must exceed the drain (`docker stop -t 30`). |
| Controlled failures | `app/services/jobs.py::MissingPayloadField` · `app/services/jobs.py::JobFailed` · `app/services/jobs.py::ConnectorRunError` | The clean-failure exception types kept distinct from a real crash. |
| The scheduler tick | `app/services/scheduler.py::tick` · `app/services/scheduler.py::_loop` · `app/services/scheduler.py::start` | One pass over due connections plus the piggybacked lifecycle sweeps, and the daemon. |
| Manual tick serialization | `app/services/scheduler.py::tick_now` | The non-blocking lock behind the external-cron entry that answers busy rather than double-running egress. |
| Schedule state | `app/services/scheduler.py::set_schedule` · `app/services/scheduler.py::_save_state` · `app/services/scheduler.py::interval_minutes` | Consume-slot-first schedule writes, honoring a mid-tick disable. |
| Unified history | `app/services/job_history.py::unified_history` · `app/services/job_history.py::job_visible_in_org` | Folds Job rows and workflow runs into one org-scoped list. |
| Jobs web surface | `app/web/jobs_ui.py::jobs_index` · `app/web/jobs_ui.py::job_status` | The history page and the status-polling endpoint, scoped like projects. |
| Jobs CLI | `app/cli/cmd_job.py::register` | The `job` group that runs handlers synchronously in the one-shot process. |
| Process roles | `app/worker.py::main` · `app/services/leader.py::run_when_leader` · `app/services/leader.py::try_acquire` · `app/services/leader.py::boot_lock` · `app/services/leader.py::hold_presence` · `app/services/leader.py::present` · `app/services/wake.py::poke` · `app/services/wake.py::listen` · `app/services/wake.py::run_changed` | The worker process entry point (boot, recovery, pools, run admission, scheduler, drain on SIGTERM) and the single scheduler leader among workers (Postgres advisory lock, SQLite file lock; a worker that loses its lock connection exits), and the cross-process wake-ups over the cross-process events bus: `work:wake` from the web role to the workers, `run:<id>` from a worker to the web role's run streams (ADR 0095). With several web processes (ADR 0097) every web and worker process boots under the boot lock, and each web process holds a presence lock so a peer's boot cleanup fails only the rows of web processes that are gone. |

With split roles (ADR 0095) one web process and any number of worker processes share the
database; workers claim jobs with the conditional UPDATE (SQLite) or
`FOR UPDATE SKIP LOCKED` (Postgres), and live peer workers' jobs are expected, so the boot logs them at INFO
and does not latch the foreign-instance word below. In the single-process role (`all`) the
rest of this paragraph applies unchanged.
For `job_queue_backend=db`, ONE app process per database is supported. Boot recovery
reclaims own-host dead PIDs, this process's PID, legacy/unparsable workers (including
bare `loc-job-N` and NULL on upgrade), and NULL/expired leases from any host. Worker
identities retain `hostname:pid:uuid8/loc-job-N`; UUIDs disambiguate logs, not ownership.
Fresh leases on another hostname or a live peer PID on this host are foreign-live:
left running, logged once at ERROR with the classification rule, and latched on `/health`.
The conditional `detail["job_queue"]` word is `db` when armed and clean, or
`db; foreign instance rows seen at boot (n)` for foreign-live rows; memory omits the key.
This detects evidence of another process but does not prevent one from starting.
A reused PID reads as live; lease expiry bounds the lease wait, but reclamation requires
the next boot, manual `db recover-jobs`, or the server's 60 s engine sweep
(`app/services/run_resume.py::start_sweeper`, lease-only) after expiry.
The manual command and the live sweep are always lease-only; because live workers renew
their leases, they never reclaim a running job.

## 4. Data & settings

- **Model:** `app/models.py::Job`: kind, payload, status, progress, error, result and
  `retries` (transient retries so far; also in `status_dict`, so the jobs API and `job` CLI
  show it); `app/models.py::JobQueue` adds `not_before` and `last_error` for the backoff and `serial_key` (ADR 0105: `tm_import:<project id>`, NULL for other kinds; the claim runs one job per key at a time in submission order, a partial unique index on running rows is the guarantee, an `IntegrityError` on a lost flip race claims nothing that round, the in-memory adapter enforces the same rule; `jobqueue.SERIAL_KINDS` names the kinds);
  written by background work and by `record_sync_job`, read by reports and the unified
  history.
- **Settings:** `setting:job_queue_backend` (default `"db"` since 1.1; `"memory"` is the
  opt-out; on `app/services/settings.py::REMOTE_IMMUTABLE_KEYS`) selects the queue, the
  boot-brick enterprise feature documented in configuration. Environment:
  `LOC_TMS_JOB_WORKERS` (long pool, default 2) and `LOC_TMS_SHORT_JOB_WORKERS` (short pool,
  default 2, minimum 1), both per process; `LOC_TMS_ROLE` (`all`, `web`, `worker`; read at
  boot, ADR 0095).
  `setting:sync_scheduler_enabled` (default `"1"`) is the connector-sync and scheduler-egress
  kill switch; `setting:project_auto_close_days` (default `"5"`, `0` = off) and
  `setting:audit_chain_verify_hours` (default `"24"`) gate two of the piggybacked sweeps.

## 5. Enforced by

- `verify_jobs`: submit, the worker lifecycle, and the synchronous CLI path.
- `verify_job_queue`: the JobQueue port's memory and durable adapters, leases, and the
  boot and expired-lease recovery of interrupted jobs.
- `verify_job_pools`: durable-by-default, the short and long pools (short never claims a
  long kind, long prefers long, live `LONG_KINDS`), the Postgres `SKIP LOCKED` candidate
  SQL, the CLI no-workers rule, and the T1 behaviors (heartbeat, transient retry, short job
  not starved, drain).
- `verify_worker_roles`: (worker entry point, a web role that starts no workers,
  web-submitted jobs completed by a worker, `/health` fast while workers are busy), web-role
  launches and resumes as queued rows finished by a worker exactly as the single process
  would, two workers never running the same run twice, run events reaching the web role,
  the memory-backend refusal and the worker's graceful stop.
- `verify_job_reliability`: lease heartbeat under the live sweep, transient retry and
  exhaustion on both backends, `not_before`, and graceful drain.
- `verify_jobs_history`: the unified Job-plus-workflow-run history and its org scoping.
- `verify_schedule`: the consume-slot-first tick, the dedup guard, the kill-switch
  boundary, and the minimum-interval floor.
- `verify_scheduler_port`: the TimeDriver seam behind the tick.

## 6. Battle scars

- **A dispatch seam that was once a bare queue is now the JobQueue port.** Naming enqueue,
  claim, release, and recover behind an adapter let the durable DB backend drop in with a
  memory default that stays byte-identical.
- **A network sweep that ran above the kill switch was the only egress leak.** The vendor
  return-poll and dispatch-retry sweeps were moved below `sync_scheduler_enabled` so turning
  sync off stops all outbound scheduler traffic, while the DB-only lifecycle sweeps
  correctly kept running, recorded in the `app/services/scheduler.py::tick` comment (the
  MEDIUM-6 finding) and proven by `verify_schedule`'s kill-switch boundary.
- **A manual tick concurrent with the daemon could double-fire an egress sweep.** The
  non-blocking `tick_now` lock closes that narrow read-then-write window and is inert for a
  default install that never calls the manual route, recorded in the module docstring.

## 7. Limits & accepted risks

- **The memory backend is single-process.** The in-memory queue dies with the process, so
  an interrupted run is visibly failed rather than resumed; multi-instance durability is the
  opt-in DB backend.
- **Break-and-sweep cadences are not instantaneous.** The tick is a fixed interval and each
  piggybacked sweep self-throttles, so a due sweep fires within its window rather than at the
  moment it becomes due, stated in the scheduler module docstring.
- **A schedule fires at most once on catch-up.** If the server was down past a due time only
  one run fires and the next slot is a full interval out, never a burst, stated in
  `app/services/scheduler.py`.
