# 0002. FastAPI + Jinja2 + vanilla JS, a modular monolith with named ports

- Status: Accepted
- Date: 2026-07-03 (stack); 2026-08-15 (composable core)

## Context

One developer builds and operates the product; it must self-host on one box, stream
progress, and stay readable to agents.

## Decision

- Server-rendered FastAPI + Jinja2 templates + vanilla JS (EventSource for SSE). No SPA framework, no HTMX.
- One process ("modular monolith with named ports"): services in `app/services/`, a kernel facade with a
  ratchet (`verify_kernel_facade`), ports for external systems (Databricks TelemetrySink first, Postgres
  embedded-only, trust-host-auth embedded mode), packaging in-repo.
- Large files are split by mechanical pure moves (`scripts/srp_split.py`, `srp_move_check.py`), hence
  module names like `*_part2`, `*_common`, `*_leaf`.

## Consequences

- In-memory job queue and a 30-second scheduler thread: one machine per install. Scale-out,
  when needed, is a Postgres-backed queue (`FOR UPDATE SKIP LOCKED`), not Redis/Celery.
- Every feature ships on several surfaces at once (ADR 0011).

## Links

- docs/architecture/kernel-and-ports.md, modules-and-flags.md
