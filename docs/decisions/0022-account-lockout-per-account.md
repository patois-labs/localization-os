# 0022. Real per-account login lockout

- Status: Accepted
- Date: 2026-09-27

## Context

The existing login throttle was per-address and in-memory per process, so it was weaker than a security
questionnaire answer about account lockout would imply (#222). Options: reword the claim or build real
lockout.

## Decision

Build real account-level lockout: 20 failures per hour lock the account for 15 minutes, doubling to 60;
a known-device bypass; counters in the DB; admin unlock plus an email to the user. Public copy states
exactly this behaviour.

## Consequences

- The old "login lockout is in-memory per process" gotcha is obsolete.
- Creation quotas must not block real accounts that have no row yet (a review finding, fixed).
- Copy about lockout describes the mechanism above and nothing stronger.
