# 0101. Worker CPU per job: counted claim wake-ups, core statements, pre-built reads

Status: Accepted
Date: 2026-10-24 (S7b)

## Context

S3c (ADR 0097) found the translate pipeline CPU-bound on a 6-core desktop: about 0.4 core-seconds per
job across web, workers and Postgres, gate G1 884.8 jobs/min against 1,400 and G2 job p95 17.1 s
against 10 s at 192 jobs in flight. S7b measured where a worker's CPU goes (mock engine, the load
harness's HTML + Markdown documents, 3 web + 6 worker processes on Postgres, pipeline step 32):

- Per job, under load, one worker spent about 222 ms of CPU. Exact per-call counters
  (`QueryThreadCycleTime` around the functions) split it: the translate handler 117 ms, the job
  claim loop 55 ms, the run admission pass 13 ms, job row updates and the finish hook about 18 ms.
  Stack samplers (py-spy, an in-process sampler) were misleading on Windows: under the GIL they only
  see threads at blocking points, so they charged the CPU to `wait_select` and `Condition.wait`.
- The claim loop was a herd. Every enqueue (in-process, or a `jobs` poke from the web role) called
  `notify_all`, so every idle claimer thread of every worker ran the claim query for every job, and
  every idle thread re-queried once a second: about four claim calls per job. The same poke also
  woke the run admission scheduler, whose pass (batch finalize, queued-run reads) had nothing to do
  for a translate job.
- Inside the handler SQLAlchemy was about 45 % of the CPU and psycopg 16 %; the app's own Python
  9 %. Building a select per call and generating its cache key cost about as much as running it:
  a pre-built select reused with bound values took 0.55 ms where the per-call one took 1.1 ms.
  The per-segment AI opt-out read (one per engine call) cost 1.7 ms through an ORM session.
- JSON column decoding was about 0.3 % of a translate job at the harness's sizes (S5's 6 % was a
  1,902-segment document). orjson would be a new dependency for no measurable gain here: not adopted.
- No measured pure function is big enough for the Rust rule (`.claude/rules/native-module.md`); the
  per-segment QA checks together are about 0.6 ms per segment, and the native module is not installed
  in the load-test environment anyway.

## Decision

- **Counted wake-ups on the DB job queue.** `DbJobQueue._notify(n, pool)` adds n signals for a pool;
  an idle claimer takes one (`_take_signal`: a long-capable claimer takes any, a short-only claimer
  only short ones) and only then runs the claim query. A claim that wins without a signal spends one
  if pending. Idle claimers of one process share one timed poll per pool set per `_POLL_SECONDS`
  (`_await_turn`, polls can be up to about two intervals apart), the fallback for rows no wake-up
  announced (a lost event, `not_before` reached, a lease expired back to pending). `claim(..., idle=True)` (the worker loop after an empty claim) does
  not query on re-entry. `enqueue` derives the signal's pool from the job kind, `hand_back` signals
  one per job, and `jobs._enqueue` pokes other processes with the pool.
- **Wake-ups by kind.** `wake._wake_local` routes received pokes: `jobs` wakes one claimer per job
  for its pool and not the admission scheduler, `runs` wakes only the scheduler, anything else both.
- **One statement for a Postgres claim.** `UPDATE jobqueue ... WHERE id = (pick FOR UPDATE OF q SKIP
  LOCKED) AND state='pending' RETURNING job_id` (`_pg_claim_sql`) on a core connection; SQLite keeps
  select then compare-and-swap. `release` reads only `job.status` on a core connection.
- **Core statements where no object is involved.** `jobs._update` is a core UPDATE on the job table
  (same guarded WHERE as ADR 0096; it no longer goes through a Session). `ai_optout._resolve` reads
  the scope's project and customer flags as fixed SQL text on a fresh core connection: still a fresh
  read before EVERY engine call, an allowed answer is still never cached, a refusal is cached as
  before; eval-dataset scopes keep the ORM path.
- **Hot per-job reads built once.** `app/db.py::prebuilt(key, build)` keeps a statement per process
  and the call passes its values as `bindparam()` parameters: the document chain, the project and
  segment reads of the lifecycle check, the handler's segment count, the paused-review lookup,
  glossary entries, TM-source and glossary-pin links, style guide and context profile resolution
  (the last two keyed by which values are NULL, so the SQL is the same as before). The quality
  check's document-format read builds its select only on a memo miss.

## Consequences

- Same-window A/B on the desktop (3 web + 6 workers, Postgres, other test runs active):
  worker CPU per job 204.6 -> 121.7 ms at 96 jobs in flight and 206.0 -> 120.9 ms at 192 (-41 %);
  Postgres CPU per job about 97 -> 64 ms; throughput 1,021 -> 1,300 and 1,024 -> 1,251 jobs/min;
  job p95 at 192 in flight 14.4 -> 11.4 s. Web CPU per job is unchanged (about 70 ms).
- Pipeline ladder (seed 200, 180 s steps, same layout as S3c, before and after back to back, host
  near 100 % with other test runs active): 96 in flight 987 -> 1,075 jobs/min (p95 7.3 -> 7.1 s),
  192 in flight 930 -> 1,098 jobs/min (p95 17.1 -> 13.1 s); worker CPU per job 195 -> 124 and
  199 -> 124 ms (-36 to -38 %). S3c's reference at 192: 884.8 jobs/min, p95 17.1 s. Gates G1
  (1,400 jobs/min) and G2 (p95 10 s) are still not met on this desktop: web (about 75 ms) and Postgres
  (about 66 ms) per job are now more than half of the total.
- Outputs are byte-identical: a fingerprint of every segment (text, status, flags, confidence), TM
  rows and every export is the same before and after on SQLite and Postgres; translate, export and
  TM suites green; `verify_query_budget` holds (statement counts did not change).
- A job whose signal another claimer spent waits at most about two poll intervals (2 s) in that
  edge case; with every claimer busy, the next finishing claimer looks at once as before. A claim
  that wins without a signal spends one from the first pool it serves, not necessarily the claimed
  job's (a low-severity review finding, accepted: costs at most one poll wait for a short-only claimer).
- The core paths use `engine` directly, not the relief/bookkeeping engine routing; no caller needs
  it today (rule in `.claude/rules/jobs-and-queue.md`).
- A workflow run (MT step + LQA review, 12 segments) moved little (270 -> 265 ms CPU): its cost is
  spread over orchestrator call sites (SQLAlchemy 40 %), the next place to apply `db.prebuilt`.
- The biggest remaining per-job DB call sites in a translate job are the TM reads
  (`tm_hub.example_matches_batch`, `lookup_batch`, S6b's area) and the Prompt Tap writes.
- Enforced by `verify_job_pools` (counted wake-up, shared idle poll, wake routing, Postgres claim SQL
  text), `verify_query_budget`, `verify_ai_opt_out` and the translate/export/TM suites.
