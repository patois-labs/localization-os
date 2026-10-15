# 0083. Project notification channels: managers may create them; creator-or-admin edits

Status: Accepted
Date: 2026-10-13

## Context

Project automation needs to tell people when runs finish, fail, hold or are blocked. Channels hold secrets (webhook
URLs, signing secrets) and add outbound egress, so the spec proposed admin-only creation. The maintainer chose otherwise.

## Decision

- Channels: Slack, Microsoft Teams, signed webhook, email. Google Chat is phase 2.
- Managers can create notification channels (not admin-only). Editing, re-pointing or deleting a channel is
  creator-or-admin; attaching a channel org-wide is admin only; attaching an existing channel to a project is manager.
- A URL or secret change is audited and mailed to admins without the URL. Secrets never appear in audit rows or MCP.
- Every channel URL passes the SSRF guard (https only; private, loopback and metadata addresses refused).
- The delivery log is filtered in SQL by org and visible channel ids. Sends run on a background sweep (20 s budget,
  broken channels skipped). `notify_outbound_enabled` is the kill switch.

## Consequences

Residual exfiltration risk from a manager-created channel is accepted and visible to admins through the audit mail.
