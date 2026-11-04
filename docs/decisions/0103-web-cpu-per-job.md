# 0103. Web CPU per job: route pre-filter, no settings flush per project, core inserts, one index

Status: Accepted
Date: 2026-10-24 (S7b round 2)

## Context

After ADR 0101 the worker was no longer the largest CPU consumer per translate job: on a 6-core desktop
(3 web + 6 workers, Postgres) a job cost about 73 ms in the web processes, 129 ms in the
workers and 56 to 74 ms in Postgres. The load harness sends, per job, 1/6 project create, 1/3
upload, 1/3 translate request and one status poll. Measured per call with the process cycle
counter and profiled with yappi (CPU clock, all threads):

- **Route matching was about a quarter of the web CPU.** FastAPI 0.142 keeps every
  `include_router` as an `_IncludedRouter` and matches a request by asking each one in turn; an
  included router matches every route under it, and the winner is matched again to handle the
  request. About 580 route checks per /api request (956 for `/api/jobs/1`).
- **Every project create flushed every process's settings.** `comment_option.stamp_new_project`
  bumped the global settings version so other processes would drop a cached per-project value
  for a reused project id; each web and worker process then dropped its whole settings cache and
  re-ran `apply_to_config` (about 40 ms) on its next probe.
- **Uploads built SQLModel objects.** Constructing a `Segment` per unit (pydantic default
  resolution for 17 fields) was most of an upload's CPU.
- The queue enqueue re-read the whole Job row (JSON payload) to copy it; the API key read was a
  select built per request.
- Postgres: `targetlocale` had no index on `project_id`; 11,527 sequential scans read 12 million
  rows during a ladder and no index scan ever happened (the table grows with every project).
- Audit writes (about a sixth of the web CPU) are S7a's area (ADR 0100) and were left alone.

## Decision

- **Route pre-filter** (`app/web/route_prefilter.py`, installed by `app.main`): each included
  router knows the literal prefixes (up to two segments) of every route under it, include prefix
  applied; when none of them fits the request path, its `matches` answers NONE without walking it.
  A route whose first segment is a parameter, a mount, a host route or a frontend group makes the
  router never skipped. FastAPI's own matching still decides, in its own order, so the first match
  is unchanged (one trailing newline is dropped before the lookup, as the route regexes'
  `$` allows it); `install()` refuses on unknown FastAPI internals (an upgrade can only lose the
  speed-up). `verify_route_prefilter` compares every registered route and near misses (3,740 paths
  x 4 methods) router by router against the unpatched answer.
- **Per-project comment option read from its row.** `comment_option.stored` reads the
  `translate_comments:<id>` row directly (one primary-key read per import, always current), so
  `stamp_new_project` no longer bumps the global settings version.
- **Core writes where no object is needed.** Ingest inserts segments with one core INSERT per
  chunk, every column spelled out with the model's constant defaults (same chunked commits; a new
  document segment has no key_id, so the key change feed has nothing to record either way).
  `DbJobQueue.enqueue` is one `INSERT ... SELECT ... RETURNING kind` from the job row (the
  original path remains for a missing job or NULL payload). `apikeys.verify_key` uses a
  pre-built statement (`db.prebuilt`, ADR 0101).
- **Index** `ix_targetlocale_project_id` (alembic d7b3f1a9c2e4, `TargetLocale.project_id`
  index=True, and the `app/db.py` `_INDEXES` shim so existing SQLite installs get it too).

## Consequences

- Same-window A/B on the desktop (100 s steps, before and after back to back), without the index:
  web CPU per job 73.2 -> 61.4 ms (96 in flight) and 72.8 -> 61.5 ms (192), worker CPU 129 ->
  122 ms (the settings flush also hit workers). Isolated (one web process, per route): upload
  54.5 -> 45.8 ms, poll 11.9 -> 9.7 ms, translate request 84 -> 79 ms, project create 41 -> 39 ms.
- Pipeline ladder (seed 200, 180 s steps, 3 web + 6 workers, base then this change back to back, host near 100 % with other test runs active), with the index: 96 in flight 1,199 -> 1,305 jobs/min (p95 6.3 -> 5.7 s), 192 in flight 1,127 -> 1,210 jobs/min (p95 13.1 -> 12.9 s); per job web 75 -> 62 and 79 -> 67 ms, worker 128 -> 120 and 130 -> 122 ms, Postgres 73 -> 66 and 72 -> 67 ms. Gates G1 (1,400 jobs/min) and G2 (p95 10 s at 192 in flight) still not met.
- Outputs byte-identical: a fingerprint of every segment (including the raw stored JSON columns),
  TM rows and 48 exports matches before and after on SQLite and Postgres.
- Workflow runs (MT + LQA) did not move measurably (run-to-run noise on the shared desktop is about
  10 %); their cost is spread over orchestrator reads. Prompt Tap writes on the translate path
  were left as they are: merging the two begin writes saves one commit per job and changes what
  a test patching `begin_action` observes, not worth it.
- Left for later: audit writes on mutating requests (S7a), the translate request's per-locale
  submit path (job insert, enqueue, wake publish per job), TM reads (S6b).
