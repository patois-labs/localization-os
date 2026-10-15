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
install (memory backend) is byte-identical to the pre-port behavior, and the opt-in durable
DB backend swaps in at boot without changing anything above the seam. The **passage of time
is the TimeDriver port** (`app/ports/scheduler.py`), so the tick can be driven in-process
or by an external cron.

Crash safety is explicit. The in-memory queue dies with the process, so recovery at boot
re-queues QUEUED rows and fails RUNNING ones (work is never silently lost, only visibly
failed); the durable backend returns eligible interrupted RUNNING jobs to pending under
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
- **The one-shot CLI runs handlers synchronously.** `app/services/jobs.py::run_sync`
  executes in the calling thread and records the same lifecycle rows, since a CLI process
  has no daemon workers. Enforced by `verify_jobs`.
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
| Workers + execution | `app/services/jobs.py::ensure_workers` · `app/services/jobs.py::_worker_loop` · `app/services/jobs.py::_execute` | The daemon pool and the QUEUED to COMPLETED/FAILED lifecycle. |
| Per-engine translate limit | `app/services/jobs.py::_EngineJobSlot` | An engine's `config.max_parallel_jobs` (1 to 64) caps concurrent translate jobs on it, only beneath the global worker cap `LOC_TMS_JOB_WORKERS`; unset = no limit (Issue #613). A queue worker that claims a job over its engine's cap does not wait: the job stays QUEUED (never shown RUNNING), goes back to the tail of the queue in one step (`JobQueue.requeue`, both backends), the worker pauses 1 s and takes the next one, so a capped backlog never starves other engines or job kinds and never spins a worker (a one-shot CLI run does wait). Order among waiting jobs is roughly FIFO. The slot count lives in the server process: a one-shot CLI run and a second app instance on the same DB each count their own. Raising `LOC_TMS_JOB_WORKERS` also raises paid engines unless each has its own `max_parallel_jobs`. Raise the worker cap before the engine cap, and expect more CPU per extra running job (177% of 2 vCPU with two jobs in the 2026-10-07 test). |
| Synchronous + recorded runs | `app/services/jobs.py::run_sync` · `app/services/jobs.py::record_sync_job` | The CLI in-process run and the after-the-fact Job row for work that ran outside the engine. |
| Partial failure | `app/services/jobs.py::partial_failure_error` · `app/services/jobs.py::run_error` · `app/services/jobs.py::last_failed_translate_run` | A translate run with failed segments ends FAILED with the count and first engine error (#612); the export hold reads the newest failed run. Image localization follows the same rule (#624; see image-localization). |
| Crash recovery | `app/services/jobs.py::recover_interrupted` | Delegates boot and live recovery to the active queue adapter. |
| Controlled failures | `app/services/jobs.py::MissingPayloadField` · `app/services/jobs.py::JobFailed` · `app/services/jobs.py::ConnectorRunError` | The clean-failure exception types kept distinct from a real crash. |
| The scheduler tick | `app/services/scheduler.py::tick` · `app/services/scheduler.py::_loop` · `app/services/scheduler.py::start` | One pass over due connections plus the piggybacked lifecycle sweeps, and the daemon. |
| Manual tick serialization | `app/services/scheduler.py::tick_now` | The non-blocking lock behind the external-cron entry that answers busy rather than double-running egress. |
| Schedule state | `app/services/scheduler.py::set_schedule` · `app/services/scheduler.py::_save_state` · `app/services/scheduler.py::interval_minutes` | Consume-slot-first schedule writes, honoring a mid-tick disable. |
| Unified history | `app/services/job_history.py::unified_history` · `app/services/job_history.py::job_visible_in_org` | Folds Job rows and workflow runs into one org-scoped list. |
| Jobs web surface | `app/web/jobs_ui.py::jobs_index` · `app/web/jobs_ui.py::job_status` | The history page and the status-polling endpoint, scoped like projects. |
| Jobs CLI | `app/cli/cmd_job.py::register` | The `job` group that runs handlers synchronously in the one-shot process. |

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
the next boot or manual `db recover-jobs` after expiry. There is no automatic sweeper.
The manual command is always lease-only. F04 heartbeat renewal remains absent: a job
running longer than the one-hour lease can still be re-dispatched.

## 4. Data & settings

- **Model:** `app/models.py::Job`: kind, payload, status, progress, error, and result;
  written by background work and by `record_sync_job`, read by reports and the unified
  history.
- **Settings:** `setting:job_queue_backend` (default `"memory"`, on
  `app/services/settings.py::REMOTE_IMMUTABLE_KEYS`) selects the durable DB queue, the
  boot-brick enterprise feature documented in configuration.
  `setting:sync_scheduler_enabled` (default `"1"`) is the connector-sync and scheduler-egress
  kill switch; `setting:project_auto_close_days` (default `"5"`, `0` = off) and
  `setting:audit_chain_verify_hours` (default `"24"`) gate two of the piggybacked sweeps.

## 5. Enforced by

- `verify_jobs`: submit, the worker lifecycle, and the synchronous CLI path.
- `verify_job_queue`: the JobQueue port's memory and durable adapters, leases, and the
  boot and expired-lease recovery of interrupted jobs.
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
