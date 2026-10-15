# Project notifications: Architecture (L1)

## 1. Role

This subsystem posts **project status updates to outside channels**: Slack, Microsoft Teams, a signed
generic webhook and email. It is Part B of the project-automation MVP (managers may create channels, not
only admins; decided 2026-10-13). It is a small, pluggable channel abstraction (`app/services/notify.py`) plus routing,
an outbox that doubles as the delivery log, a retry sweep, and the web, CLI, `/api` and MCP doors.

A **channel** is an org-owned destination. Its URL (an incoming-webhook URL is a bearer credential)
and, for the webhook kind, its HMAC signing secret live in one Fernet blob, write-only on every
surface: a door gets a masked tail and presence flags, never the value; the signing secret is shown
once, in the response or page that created or rotated it. A **route** attaches a channel to a scope.
Resolution for an event goes project, content type, project type, customer, org default; per channel
kind the most specific level whose route covers the event wins, a route with no event list means the
default-on events, and a project can be muted. The email kind reuses the existing mailer and SMTP
settings; the older direct emails in `app/services/notifications.py` are unchanged.

A message carries the project and customer names (escaped for the target format, so a project named
`<!channel>` cannot ping a Slack channel), a few counts, a time in 12-hour UTC and a link to sign in.
It **never carries source or target text, record titles, error text or cost figures**.

Part A (project automation runs) plugs in through one call, `notify(event, project, payload)`. It
never raises and never waits on the network, so an automation run settles whether or not any channel
works. This subsystem does not decide when a run happens, create Jira tickets or post anything
inbound; it only reports.

## 2. Invariants

- **Content never leaves.** The facts that reach an outbox row are an allowlist (names, small
  integers, a limit kind, a status link); an unknown payload key is dropped, not forwarded. Enforced
  by `verify_notify`.
- **Secrets are write-only.** The URL and signing secret are Fernet at rest, absent from `to_dict`,
  the audit rows, the delivery log and every response except the one that mints a signing secret.
  Enforced by `verify_notify`.
- **All HTTP goes through the SSRF guard.** One function builds the client and the guard together
  (`app/services/notify.py::_post`); HTTPS only, no redirects, a 10 second timeout, private and
  loopback targets refused unless `egress_private_allowlist` admits them. Slack is pinned to
  hooks.slack.com and Teams to Microsoft Power Platform hosts at save time. Enforced by
  `verify_egress_coverage` and `verify_notify`.
- **A failure never fails a run.** `notify()` swallows and logs; delivery is on a short-lived daemon
  thread; a transient failure retries on the scheduler tick at 1, 5 and 30 minutes and then the row
  is `failed` and visible on the channel page, with one `notify.delivery_failed` audit row.
- **One message per (run, event).** `(channel, dedup key)` is unique in the outbox.
- **Kill switches.** `setting:notify_outbound_enabled` off means nothing is queued or sent (the email
  paths in `app/services/notifications.py` are unchanged); the retry sweep sits under
  `setting:sync_scheduler_enabled` like every other egress sweep; a channel can be turned off alone.
- **Isolation order.** The org and customer ring is checked before the role gate; a foreign or
  missing channel answers the same 404; a customer-bound caller sees and creates only its own
  customer's channels. Managers and admins may create channels and attach them; only the creator or
  an admin may edit, re-point or delete a channel, and attaching to the whole org is admin only. A
  change of a channel's URL or signing secret is audited and emailed (without the URL) to the admins.
- **Errors never name an internal address.** A refused destination answers one fixed sentence; the
  guard's own text (which can carry a resolved private address) goes to the server log only.
- **The scheduler tick never waits on a send.** The retry sweep runs on a daemon thread with a 20
  second wall-clock budget, skips the rest of a channel's rows after its first transport failure,
  never sends a row older than 24 hours, and prunes delivered rows after 30 days (failed after 90).
- **Audit is fail-closed and secret-free:** `notify.channel.create|update|delete|test`,
  `notify.route.set`, `notify.delivery_failed`.

## 3. Where the work happens

| Concern | Pin | One line |
|---|---|---|
| Channels, routing, outbox, adapters | `app/services/notify.py` | The service every door calls; `notify()` is the entry point for events. |
| Entry point | `app/services/notify.py::notify` | Queue an event for every routed channel; never raises. |
| Routing | `app/services/notify.py::resolve` | Most specific level per channel kind. |
| Delivery and retry | `app/services/notify.py::deliver_one` | Claim, send, classify, schedule the retry or fail. |
| Retry sweep | `app/services/notify.py::sweep` | Runs from the scheduler tick, below the sync kill switch. |
| Connector job events | `app/services/notify.py::on_connector_job` | started, finished, failed, needs review, limit hit. |
| Web page and /api | `app/web/notify_routes.py` | `/settings/notifications` and `/api/notify/*`. |
| CLI | `app/cli/cmd_notify.py` | The `notify` command group, local and `--remote`. |
| Project panel | `app/templates/_project_notifications.html` | Attach a channel to, or mute, one project. |
| MCP | `integrations/mcp-server/registry.py` | `project_notifications`: list and attach existing channels only. |

## 4. Data & settings

- **Models:** `app/models.py::NotifyChannel` (destination, kind, state, last test),
  `app/models.py::NotifyRoute` (scope, channel, events; a NULL channel is a mute),
  `app/models.py::NotifyOutbox` (the delivery log). One additive Alembic revision creates the three
  tables; all three are excluded from the data export (`app/services/data_export_part2.py`).
- **Settings:** `setting:notify_outbound_enabled` (default `1`) is the kill switch. The status link
  uses `setting:app_base_url`; without it a message says to open Localization OS and carries no link.
- **Events:** `run.started`, `run.finished`, `run.failed`, `run.needs_review`, `run.limit_hit`,
  `automation.paused`, `automation.blocked`, `project.created`, `project.completed`. Default on:
  finished, failed, needs review, limit hit, automation paused, automation blocked (a run was blocked
  by another project's claims: sent to both projects, counts and project names only). Wired today: connector job phases, a workflow run paused for
  human review, the project-budget 80% and 100% thresholds, project created and completed.
  `automation.paused` and the automation run events arrive with Part A.
- **Webhook signature:** `X-LocOS-Signature: t=<unix>,v1=<hex HMAC-SHA256(secret, t + "." + body)>`,
  with `X-LocOS-Event` and `X-LocOS-Delivery`. Receivers should refuse a timestamp more than 5
  minutes old (`app/services/notify.py::sign_body`).

## 5. Enforced by

- `verify_notify` - validation, secrets, routing, messages, delivery, retries, hooks.
- `verify_egress_coverage` - the one outbound client is guarded and inventoried.
- `verify_cli_parity` and `verify_cli_documentation` - the CLI leaves exist and are documented.
- `verify_mcp_parity` - the MCP tool maps to real routes and the credential routes stay exempt.
- `verify_data_export` - the three tables are classified.

## 6. Battle scars

- **The design first said admin only.** The maintainer changed it on 10-13 (managers may create channels), but
  an agent still must not: the MCP tool lists and attaches existing channels and cannot create one or
  read a URL, so an agent cannot add an outbound destination and have project data posted to it, see
  `docs/specs/mcp-server.md`.
- **Names are the injection surface.** The only free text in a message is a project or customer
  name, so each target format escapes it (Slack `&`, `<`, `>`; Adaptive Card markdown characters).

## 7. Limits & accepted risks

- **Teams hosts are unverified.** The pin lists the documented Microsoft suffixes; adjust it after a
  live test against a real tenant.
- **Slack uses incoming webhooks.** A bot token with a channel picker is a later option.
- **In-process threads.** Delivery after commit uses a daemon thread; a crash between commit and send
  leaves a `pending` row that the next scheduler tick sends.
- **Mute beats everything.** Any manager may mute a project, including one an admin routed to an
  audit channel; the project panel shows the muted state and the change is audited. Whether
  `run.failed` and `automation.paused` should be exempt is an open decision for the maintainer.
- **Part A should pass its session.** `notify()` accepts `session=` so an automation run's event
  commits with its settle; the existing hooks call it after their own commit (best effort), so a
  crash between the two loses that one event.
- **Residual exfiltration risk.** A manager can create a webhook and attach it to projects, which
  sends project and customer names and counts to a destination they choose. The creator-or-admin
  rule, the admin email on a URL change and the audit row bound it; they do not remove it.
- **Secret rotation has no overlap.** A rotated signing secret breaks the receiver until it is
  updated. Slack 429 `Retry-After` is ignored; the retry delays are fixed.
- **No digest yet.** A daily digest per route for frequent schedules is phase 2.
