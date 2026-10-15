# 0012. The back-office and the super-admin account are removed

- Status: Accepted
- Date: 2026-09-20

## Context

A separate back-office (`app/backoffice/`, `/back-office`, its own session cookie, impersonation and
MFA settings) and an `is_superadmin` role existed for cross-org operations. They were the largest
cross-tenant attack surface found in the 2026-09-18 audit and had no customer use yet.

## Decision

Delete the back-office and super-admin entirely (merge `ac95ae7b`). Org lifecycle (create, rename,
suspend, unsuspend, delete, move user) is CLI-only behind `require_operator` and `LOC_TMS_OPERATOR_KEY`,
with no web parity. The `is_superadmin` column stays dormant and is cleared at every boot by
`app/db.py::_clear_dormant_superadmin` (fail-closed; `verify_multiorg::test_superadmin_stays_dormant`).

## Consequences

- Break-glass becomes shell access to the host; there is no cross-org remediation path in the UI.
- Re-read this before designing any hosted multi-tenant offering.
- Supersedes the earlier rule that a super-admin resolves to the HOME org, and the back-office TOTP items.

## Links

- docs/architecture/identity-and-access.md
