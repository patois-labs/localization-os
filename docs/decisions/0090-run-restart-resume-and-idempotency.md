# 0090. Workflow runs resume after a restart; each step visit is checkpointed and keyed once

Status: Accepted
Date: 2026-10-22

## Context

A workflow run executes on a daemon thread. A server restart killed the thread and boot recovery marked the run
FAILED ("Interrupted by a server restart."). A container deploy swaps the process, so every deploy failed every run
that was mid-step, and the user had to notice and re-run it. A load review of the engine named this the first
engine gap; target T2 reproduced it. The engine is redesigned in place (Python plus Rust hot paths, no rewrite);
this is stage S1.

## Decision

- **Checkpoint per step visit.** Every step-completion commit (the StepRun insert plus the cursor advance) also stamps
  the StepRun with `idempotency_key = "<run>:<cursor>:<seq>"` (UNIQUE index `ux_steprun_idempotency_key`) and
  `checkpoint` (next cursor, the working texts, the same run-local records a human-review pause persists, the run
  context), in the same transaction. A crash leaves the visit and its checkpoint, or neither. One helper,
  `run_resume.VisitWriter`, serves every seam (segmented: llm/QE main, tm_match, condition skip; FULL_TEXT: main,
  tm_match, protected skip, condition skip; LQA in the shared `_execute_step`). Checkpoints are cleared when the run
  ends.
- **Owner and heartbeat.** Each execution stamps `WorkflowRun.owner` (`<host>:<pid>:<uuid8>/<nonce>`) and one
  daemon thread per process renews `heartbeat_at` every 30 s. Before each visit commits, the writer re-reads the
  owner on its own connection; a thread whose run was taken over stops without writing, and its crash handler and
  terminal write-back leave the run alone. The UNIQUE key backstops the check-then-commit window.
- **Resume, not fail.** Boot recovery and the batch healer call `run_resume.plan`: skip a run whose owner is alive
  (a thread here, another live PID on this host, another host with a heartbeat under 90 s); resume a run whose newest
  StepRun has a checkpoint for its cursor (or that never ran a step) by re-queuing it QUEUED with
  `_resume_kind=restart`; otherwise fail it as before. The admission scheduler starts it like any queued run and the
  run loop seeds itself from the checkpoint (never from an old pause blob, never as an operator edit). A live sweep
  (60 s) re-queues runs whose owner's heartbeat went stale while this server is up (a container that died after a fast
  swap, a crashed CLI). PAUSED and QUEUED runs are never touched. Rows with no owner (written before this change) keep
  the old fail rule. `resume_count` is shown on the run; recovery fails a run after 3 restart resumes in a row
  without a new checkpoint, so a step that kills the process cannot crash-loop the server, while a long batch that
  keeps progressing survives any number of deploys.
- **Liveness.** A same-host owner is live only while its PID is alive AND its heartbeat is within the 10-minute
  activity grace (a reused PID must not pin a dead run). A remote owner is live while its heartbeat is under 90 s.
  The live sweep fails a dead plain run it cannot resume; a dead batch member it cannot resume is left to the batch
  healer at the next boot (or `db recover-jobs`), which also finalizes the batch.

## Consequences

- A deploy during a batch no longer fails the runs in flight: they resume from the step they were on. T2 passes 3/3;
  `verify_run_restart_resume` keeps it in the release gate.
- Accepted residual: the visit IN FLIGHT at the crash runs again, so its provider calls are made and billed again
  (the first calls really happened). Completed visits are never re-run or re-billed. Resume is refused (the run fails,
  as before) for an EXTERNAL step in flight (it would re-dispatch to a vendor), a visit written by a seam without a
  checkpoint (parallel LQA group, visit-cap skip), a FULL_TEXT run with a failed, refused or regressed step or with
  branch routing, and a crash between a human-review resume and the next visit commit (the reviewer's edit is only
  in memory until then).
- One extra read per visit (the owner check) and one heartbeat UPDATE per running run every 30 s.
- Also accepted at review: a superseded thread still finishes the provider calls of the visit it was
  in (it writes nothing afterwards); on a graph workflow `RunGuard.enter` commits `run.context` visit counts without
  an owner check; a run that recovery fails keeps its checkpoint blobs until it is deleted; an attempt that a drain
  or a takeover discards drops out of `Job.cost_usd` (the spend ledger still has it); the admission flip resets
  `started_at` on a resumed run. Job-level `TransientJobError` retry exists, but no handler raises it yet (engine
  calls inside steps already retry 429/5xx/timeouts in `providers.py`); wiring the translate and connector
  handlers is a follow-up.
- S2 (durable step queue) builds on this: each visit becomes a queue task and the checkpoint becomes its input, which
  also gives per-segment granularity.

Code: `app/services/run_resume.py`, migration `b4d8e2f6a9c1`. Architecture: `docs/architecture/workflow-engine.md`.
