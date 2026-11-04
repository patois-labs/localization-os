---
paths:
  - "app/services/jobs.py"
  - "app/ports/jobqueue.py"
  - "app/worker.py"
  - "tests/verify_job*.py"
---

# Background jobs and the durable queue

Architecture: `docs/architecture/jobs-and-scheduler.md`. ADRs 0090 (lease, drain), 0092 (DB queue default, pools),
0095 (roles).

- A Job row with no `jobqueue` row is invisible to `DbJobQueue.recover`. The in-memory queue never wrote one, so the
  first role-`all` DB-queue boot adopts those rows once (`jobs._adopt_once_after_switch`, marker `.jobqueue-adopted`
  in the data dir; a memory-queue boot removes it). Why: on the upgrade they were stranded QUEUED/RUNNING forever and
  held the dedupe doors (`verify_job_queue_upgrade`). Never make a boot sweep fail RUNNING rows without
  a queue row on every boot: a one-shot CLI `job run` (`jobs.run_sync`) looks exactly like that. Never write
  the marker in role `worker`: workers never fail RUNNING rows, so a worker-first upgrade would strand them. Until a role-`all` boot happens, every worker boot adopts QUEUED rows again.
- `_worker_loop` claims with a 1 s timeout, so a claim can still be open when `drain()` hands jobs back. Keep the
  `_claiming` count and the requeue of a claim that lands during a drain. Why: `verify_job_reliability` failed 2 of 4
  runs with a handed-back job re-claimed at once.
- `DbJobQueue` wake-ups are COUNTED (ADR 0101): `_notify(n, pool)` adds n signals and each idle claimer that takes
  one runs one claim query. Never go back to `notify_all` plus "every waiter queries", and never call `_notify()`
  once for a batch of k jobs (pass `n=k`): a missing signal costs that job up to about two poll intervals. A new cross-process
  poke kind needs a branch in `wake._wake_local` (unknown kinds wake both claimers and admission). Why: the herd was
  about 4 claim calls per job and 55 ms of a worker's 222 ms per translate job (S7b).
- `DbJobQueue._try_claim`/`release`, `jobs._update` and `ai_optout._resolve_fast` use `engine` directly (core
  connections), so they do not go through `db.relief_engine_or` / the bookkeeping engine. No caller runs them inside
  a relief or bookkeeping scope today; route them through it before adding one.
- `_execute` reads the status and then sets RUNNING without a `status='queued'` condition, and `DbJobQueue.release`
  keys on `job_id`: never create a second queue row for one job (a known low-severity limit). Adoption can create one
  only in the window between a peer's Job insert and its `_enqueue`, at a switch boot; on a split-role install that
  never boots role `all` every worker boot is such a boot (narrow, but real across separate nodes).
- A kind in `jobqueue.SERIAL_KINDS` (today `tm_import`, ADR 0105) runs one job per key at a time: `serial_key` is written in the same INSERT ... SELECT as the queue row, `requeue` and the ORM enqueue copy it, and the claim's candidate SELECT skips a pending row whose key has a running or an older pending row. A new write of a `jobqueue` row must set it, or the job runs unserialized. Order is by JOB id (a requeue gets a higher row id), a blocking row counts only while its Job exists and is queued or running (so a queue row whose Job was deleted never blocks its key; project delete also removes its jobs' queue rows, while retention keeps live rows of finished jobs on purpose, #438). A lost flip race is an `IntegrityError` caught in `_try_claim` (claims nothing that round); never turn it into a retry-at-once loop (`claim` does not check its timeout on the `seen` path).
