# 0010. A foreign id answers exactly like a missing id (404 before 403)

- Status: Accepted
- Date: 2026-10-02 (made explicit and swept); principle since the 2026-09 security review

## Context

#356 phase 2b put the admin-role check before the org/customer ring check on the per-workflow
system-prompt routes, so a caller could tell a foreign workflow id (403) from a missing one (404).
`verify_connector_isolation_gaps` went red.

## Decision

On every route that takes a row id, the ring check (org, customer, project) runs BEFORE any role or
admin gate. A foreign id returns the same status, headers and body as a missing id, within timing
noise. Only in-ring callers can receive 403. Only id-independent checks (`_ai_block`, CSRF middleware)
may precede the ring check. The sweep's allowlist matches exact id-less paths only.

## Consequences

- New routes must be added to `verify_project_scoping` and covered by the isolation sweep.
- Known residual (unfiled): web `_wf_or_404` does not ring the workflow's project the way /api
  `_wf_in_org` does.

## Links

- .claude/rules/api-routes.md
