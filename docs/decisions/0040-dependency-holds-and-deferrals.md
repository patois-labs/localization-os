# 0040. Dependency holds and deferred dependency updates

- Status: Accepted
- Date: 2026-10-01

## Context

The 10-01 Dependabot batch mixed safe bumps, a framework migration (mcp 2) and companion-app bumps
that cannot be built or tested offline.

## Decision

- Accepted: uvicorn 0.54.0, sqlmodel 0.0.44, urllib3 2.8.0 (security advisories).
- sqlmodel is HELD at 0.0.44: 0.0.45 breaks naive datetimes.
- mcp 2.2.0 got its own migration (#369, ADR 0028).
- DEFERRED, with a comment on each update: `@forge/bridge` 7 with `@forge/react` 12.2 (a coupled pair
  that needs a Forge deploy to test) and react / react-dom 17 for the Airtable extension (no runnable
  offline build, no security motive).
- Locks are hash-pinned and regenerated only through `scripts/hash_locks.py`.

## Consequences

Any uvicorn bump re-runs the ingress/proxy suites (`app/main.py` wraps `ProxyHeadersMiddleware`).
