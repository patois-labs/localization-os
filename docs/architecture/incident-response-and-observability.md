# Incident Response & Observability - Architecture (L1)

## 1. Role

This subsystem is the **operational-visibility and alerting layer**: the email notifications that tell
a person when something needs their attention, and the host-only switches and seams through which
the paid observability add-on (the Prometheus/OpenTelemetry recording engine and the auth-gated
`GET /metrics` endpoint) plugs in. Together with the ops runbook it closes the "no monitoring,
no alerting, no incident plan" gap.

The open core owns no metric engine: `app/main.py::_arm_observability` asks the extension
registry (`app/extensions.py`) for the `observability` provider and does nothing when it is not
installed, so a core install mounts no `/metrics` route and records no metrics. The Enterprise
page still carries the two host-only switches, and their preflight reports a missing add-on as a
blocking check.

The **email notifications** service (`app/services/notifications.py`) is the install's only
push channel: it emails the submitter when a translation job finishes, admins when a project
crosses a spend threshold, an assignee when a human-review step pauses, and query
participants - each a graceful no-op when SMTP is unconfigured.

What it does **not** own. It reads the audit chain's health but does not compute it - the
scheduled verification, the anchor, and `app/services/audit.py::chain_status_snapshot` are
[audit-chain](audit-chain.md). It does not own the `/metrics` authorization *primitives*
(`app/web/deps.py::_require_admin_scope` is [identity-and-access](identity-and-access.md)); it
only composes them into the endpoint's gate. Enablement is wired through the Phase-2
enterprise door, documented separately.

## 2. Invariants

- **Every metric label is bounded and identifier-free.** The HTTP route label is a route
  template / mount prefix / `__unmatched__` (never a raw path), the method is allowlisted to
  the nine standard verbs (else `other`), and no label carries a customer/user/document/project
  id - so a scrape can be neither a cardinality bomb nor a cross-tenant leak.
- **A boot switch that is on but unsatisfiable fails loudly.** An armed switch missing its
  opt-in library - or `otel_enabled` with an empty or path-less endpoint - raises at boot
  rather than serving a dormant no-op exporter that silently drops spans.
- **A notification failure never crashes the work it observes.** Most functions in
  `app/services/notifications.py` open their own DB session off the request path
  (`app/services/notifications.py::on_job_finished`, the human-review pause mail, and the three
  query mails); `app/services/notifications.py::on_project_budget_threshold` is instead called
  with the caller's session at an already-committed chokepoint. Every one swallows a mail
  failure (logged, never raised) and no-ops when its toggle is off or SMTP is unconfigured -
  enforced by `verify_notifications`.

## 3. Where the work happens

| Concern | Pin | One line |
|---|---|---|
| Email notifications | `app/services/notifications.py::on_job_finished` · `app/services/notifications.py::on_project_budget_threshold` · `app/services/notifications.py::enabled` | Emails the job submitter on completion and admins on a project-budget threshold; graceful no-op without SMTP. |
| Invitation mailer | `app/services/mailer.py::send` · `app/services/mailer.py::configured` | The optional SMTP mailer for invitation emails, a no-op that shows the invite link to share manually when SMTP is unset. |

## 4. Data & settings

- **Metrics reads, no metric state of its own.** The registry is in-process; the two gauge
  collectors read the DB at scrape time - the run/segment counts off `app/models.py::WorkflowRun`
  and `app/models.py::Segment` (install-global aggregates by status), and the chain gauges off
  the audit status the scheduler persists.
- **Settings:** `setting:metrics_enabled` (default `"0"`) and `setting:otel_enabled` (default
  `"0"`) arm the two switches; `setting:otel_endpoint` (default blank) is the OTLP/HTTP
  collector URL. `setting:notify_on_job_finish` (default `"1"`) and
  `setting:notify_on_budget_threshold` (default `"1"`) gate the two emails documented here
  (they default on because they cost nothing without SMTP). `metrics_enabled`, `otel_enabled`,
  and `otel_endpoint` are **host-only** (on `app/services/settings.py::REMOTE_IMMUTABLE_KEYS`),
  so no `/api` config write can arm observability over the wire.

## 5. Enforced by

- `verify_notifications` - the self-filtering, the toggle/SMTP no-ops, and the
  never-raise-on-mail-failure guarantee.

## 6. Battle scars

- **`/metrics` and OTel shipped auth-gated and label-bounded in the enterprise sprint.** The
  admin-scoped endpoint (with a sync handler so a heavy DB-gauge scrape never stalls the event
  loop), the bounded identifier-free labels, and the fail-loud OTel exporter were built as the
  sprint's observability train.
- **The auth gate and the OTel fail-loud posture are inventoried as controls.** The
  `metrics_enabled` admin gate (AV-09), the `otel_enabled` OTLP tracing with bounded labels and
  bad-endpoint refusal (AV-10), and the scheduled/monitored chain verification the gauges
  surface (AU-05) are mapped to code evidence.
- **The chain gauges exist to power the incident-response runbook.** The scheduled audit-chain
  verification and the `/metrics` gauges are the detection sources for SEV classification and triage.

## 7. Limits & accepted risks

- **Alert *rules* are deployment config, not code.** The signals now exist - forced
  `chain_broken` rows, ERROR logs, and the `/metrics` gauges - but wiring them into an external
  alertmanager (the paging integration) is operator-side, the recorded residual on gap G-10.
- **Notifications are email-only, best-effort, and unassigned steps notify nobody.** There is
  no in-app notification centre, digest, Slack, or webhook target; without SMTP nothing is sent
  and nothing is logged, and an unassigned human step or query emails no one by design.
