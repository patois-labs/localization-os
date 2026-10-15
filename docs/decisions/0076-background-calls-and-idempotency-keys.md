# 0076. Background calls and idempotency keys for slow /api routes and MCP tools

Status: Accepted
Date: 2026-10-11

## Context

#728 and the review's H1: `connector_sync` and at least seven other spend/write MCP tools gave up after 30 to 210 s
while the work kept running, so an agent saw an error for a call that succeeded and might repeat a paid run.
#743 #744 #745 #747.

## Decision

- `/api` routes accept `Prefer: respond-async` (202 + job id, `GET /api/jobs/{id}`) and `Idempotency-Key` on
  billing routes: per caller + route, 24 h, replay marked `Idempotent-Replayed`, 422 on reuse with a different
  request. Requests without either header are untouched.
- MCP: ten slow tools launch in the background and poll up to 45 s, then answer `{status: running, job_id, next}`;
  `job_status` returns the result. Spend tools take `idempotency_key`. A dropped connection says the call may still
  be running and must not be confirmed again (`timeout_maybe_running`).
- CLI: global `--idempotency-key`; remote connector pull/push poll.
- Bounded: 8 background threads (more = 503 with Retry-After); restart marks rows "may have partly run"; bodies of
  rows finished more than 7 days ago are dropped.

## Consequences

- No cancel for a background api_task job yet. Sync advanced runs and eval experiments without the header still
  hold a worker.
