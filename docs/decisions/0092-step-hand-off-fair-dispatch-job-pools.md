# 0092. Runs hand off between step visits; fair dispatch; short and long job pools; DB job queue by default

Status: Accepted
Date: 2026-10-23

## Context

Stage S2 of the engine redesign. Before it:

- A background workflow run held one capacity slot and one daemon thread from its first step to its last. A run
  with many slow steps kept everything queued behind it waiting for all of them.
- The admission scheduler rotated across fan-outs one run at a time. Every document's batch lists its languages in
  the same order, so it admitted every document's first language, then every second, then every third. Target T3
  (12 documents x 3 languages on a 3-slot mock engine) measured mean time to review es 2.7 s, fr 4.2 s, de 5.8 s;
  a real-engine flood measured es 81 s, fr 174 s, de 258 s.
- One job worker pool served every job kind: a data export waited behind document translations (T1 check 4: 5.6 s
  behind two long jobs).
- The in-memory job queue was the default: work in flight was lost on restart (T1 check 1).

S1 (ADR 0090) made every completed step visit a durable checkpoint, which is what lets a run's execution be split.

## Decision

- **Runs execute in segments and hand off between step visits** (`app/services/run_segments.py`). The run's
  generator, Session and `contextvars.Context` live on a `RunSegment`, not on a thread. At a completed step visit
  (the `step` / `step_skipped` event that follows the visit's checkpoint commit) a segment that has executed for
  `LOC_TMS_STEP_QUANTUM_S` (default 10 s) parks when other work waits, the process admission daemon is alive and its
  Session holds no unflushed change. Parking ends the read transaction (no pooled connection is pinned), moves the
  S1 heartbeat to a parked holder, releases the slot and ends the thread; the row stays RUNNING with its owner. The
  admission scheduler continues it on a new thread with the same name, which resumes the SAME generator. Graph
  semantics (loops, gates, branches, parallel groups, human review, visit caps) therefore cannot change: nothing is
  re-entered or re-derived. `LOC_TMS_STEP_HANDOFF=off` restores the pre-S2 behavior; `always` parks at every
  boundary and is how the suites prove the hand-off is invisible.
- **One fair order for all waiting work** (`_AdmissionScheduler._fair_pick`): customers take turns; within a
  customer, fan-outs take turns, but a batch keeps its turn for up to its width (one document's languages start
  together); within a fan-out a parked run goes first, then new runs in the fan-out's language order, sorted and
  rotated one place per new fan-out (no language is always first). The interactive-launch reservation and yield
  window are unchanged.
- **Postgres claims use `FOR UPDATE SKIP LOCKED`**: the run admission claim and the DB job queue claim lock their
  candidate row and skip rows another process is claiming; SQLite keeps the conditional-UPDATE compare-and-swap
  under its single writer.
- **Short and long job pools** (`jobs.LONG_KINDS` = translate_document, connector_translate, tm_import, and an add-on's embedding backfill).
  Short workers (`LOC_TMS_SHORT_JOB_WORKERS`, default 2) take only short kinds; long workers
  (`LOC_TMS_JOB_WORKERS`) take any kind, long kinds first (so a flood of short jobs cannot starve translation;
  short jobs never wait behind long ones because the short workers serve them). The pool is decided by kind at claim time; no schema
  change (the DB queue filters on `job.kind`).
- **`job_queue_backend` defaults to `db`.** Memory stays a supported opt-out; the one-shot CLI still runs handlers
  synchronously and starts no workers.

## Why not the alternatives

- **A fixed pool of run worker threads.** The run thread's NAME is the in-process liveness signal both sweepers and
  the S1 recovery consult, synchronous CLI batches join member threads, and the key-change actor is inherited
  through `Thread.start` (keys_common: "inheritance stops at thread pools"). Thread count is already bounded by the
  run slots. Segments give the property that mattered (no run holds a slot for its whole life) without a pool.
- **Re-queue the run row and resume from the checkpoint at every step.** Exercises S1's resume path on every step:
  launch guards, budget gates and envelope bookkeeping would re-run mid-run, and S1 can only resume linear runs (no
  graph routing, no parallel group, no FULL_TEXT run with a failed step). Continuing the same generator is exact.
- **Job rows for step tasks.** Would double every run in the jobs history and its retention; the run row already
  carries the queue state, the owner and the heartbeat lease.

## Consequences

- An arriving run waits at most one step visit plus the quantum for a slot held by long runs, instead of a whole
  run. T3 passes 3/3 (spread p95 about 1.4 s against a 1.8 s bound; language means within 3 %).
- A parked continuation is in memory: if its process dies the run resumes from its last checkpoint through S1
  recovery (or fails where S1 cannot resume, as before), but another live process cannot take its next step.
  For S3 (worker processes): workers claim QUEUED rows with SKIP LOCKED; a run stays in the process that started it
  until it pauses, ends or its process dies; the web role must not start the admission daemon. Cross-process step
  migration needs S1-grade checkpoints for every seam and is out of scope here.
- Upgrading an install with no explicit `job_queue_backend` switches it to the durable queue: interrupted RUNNING
  jobs are re-run at boot instead of failed.

Enforced by `verify_step_queue`, `verify_fair_dispatch`, `verify_job_pools` (targets T1 and T3 promoted).
