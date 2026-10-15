# 0036. The audit chain is fail-closed and never anonymous

- Status: Accepted
- Date: 2026-09-26 to 2026-09-27 (security fix train F5, F21)

## Context

The tamper-evident HMAC audit chain is evidence for security questionnaires and incident review. A write
that silently skipped its audit row, or a row attributed to "anonymous", made the evidence unreliable.

## Decision

- A request whose audit row cannot be written is refused with a retryable 503 (on every install).
- The actor is never "anonymous": `unauthenticated <ip>`, `refused <status>`, `typed-name=`,
  `wrong-key-start=`, `suspended-key=` and similar.
- `audit verify` runs on each host before and after every deploy.

## Links

- docs/architecture/audit-chain.md
