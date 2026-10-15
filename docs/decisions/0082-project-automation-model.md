# 0082. Project automation: one connection feeds many projects, schedules in UTC, catch up once

Status: Accepted
Date: 2026-10-13

## Context

Projects could not pull and push their own content on a schedule; connections carried their own interval and one
project. A design spec listed options and seven questions; the maintainer answered them in two rounds on 10-13.

## Decision

- MVP = project automation + notifications (ADR 0083) + custom fields (ADR 0084). Inbound triggers, workflow mode and
  Jira ticket auto-create are later phases.
- ONE CONNECTION CAN FEED MANY PROJECTS. Two projects never write back over each other: claims per (item, locale),
  unique; a blocked project gets status `blocked`, BOTH projects are notified ("blocked by project X"), and a manager
  can hand ownership over in one click. Claims are released on archive, unbind, project or connection delete; a dead
  owner is taken over (audited).
- Policies: scheduled delta, scheduled FULL pull (allowed, besides the manual Full refresh), manual. Inbound trigger
  (phase 2) is a per-project choice: mark pending, or run right away.
- No default per-run spend cap: the run bound is min(project cap, budget left); the install cap still applies first.
- Schedules are stored and fired in UTC; the UI shows local time alongside.
- After downtime: run ONCE to catch up (one run covers every missed slot, recorded as `catch_up` with
  `missed_slots`), then back to the normal schedule. Slots are consumed by a compare-and-set on `next_at`.
- Managers may bind an organization-wide connection to a customer's project: a non-admin gets a private lane that
  copies the credential server side; admin-only scope fields stay admin-only.
- Kill switches: module `project_automation` (writes_off), existing `sync_scheduler_enabled`, per-project pause.

## Consequences

- N manager-bound projects on one org-wide connection mean N separate reads (open question; a shared reader
  is phase 2/3).
- A full pull re-translates every pair; only TM 100% matches are free.
- Four new additive tables (Alembic chain ends `e1b7c3a9d5f2`).
