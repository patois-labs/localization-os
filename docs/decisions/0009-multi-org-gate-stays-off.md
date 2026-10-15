# 0009. multi_org_enabled stays off until the org-boundary issues land

- Status: Accepted
- Date: 2026-09-20 (gate rule); preconditions extended 2026-09-26

## Context

Org isolation (org_id on the tenant tables, `access.normalized_org_id`, `org_filter()`) is built behind
the `multi_org_enabled` setting (default "0"). No install has ever run with it on in production. The
2026-09-18 security audit left #177, #178 and #181 (org boundary follow-ups) open, and the 09-26 review
added two LOW preconditions (`--operator user org` can move an org's last admin out; a moved user's
self export includes former-org data).

## Decision

Do not enable `multi_org_enabled` on any install until #177, #178 and #181 land and the multi-org
preconditions are fixed. Single-tenant installs treat the Default org and not-yet-stamped NULL rows as
the same org; strict equality applies only under the gate.

## Consequences

- Code must still be correct under the gate (suites run gate-ON paths), but nothing ships relying on it.
- TM tables are strictly per-project (no org_id); `Job.org_id` does not exist.
- A hosted multi-tenant tier (Postgres row-level security, a re-encrypt tool, a restore drill) is still
  ahead.

## Links

- docs/architecture/tenancy-confinement.md
