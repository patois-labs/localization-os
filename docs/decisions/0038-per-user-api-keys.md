# 0038. Per-user API keys with scopes, expiry and rate limits; the shared connector token is retired

- Status: Accepted
- Date: 2026-09-27 (#232 approved, #263, #264); 2026-10-01 (#270 expiry)

## Context

A shared `lsk_` connector token reached translation routes; keys never expired; spend routes had no
per-key rate limit; the remote CLI sent its key over plain http.

## Decision

- Per-user `lsu_` keys only; the shared `lsk_` token is retired (#232). The Airtable extension/automation
  and Jira Forge app move to per-user keys later.
- Optional expiry `expires_at` (whole days 1 to 3650 on web, API and CLI) with an install cap
  `apikey_max_age_days`; expired equals revoked (identical 401). Re-enabling a user needs the admin scope.
- Rate limits: 120/min on spend routes, 600/min on reads, admin settings, 0 = off (#263).
- Remote CLI over plain http only for loopback and the tailnet (`100.64.0.0/10`, `*.ts.net`); otherwise
  `--insecure` (#264).
- Assistant call limits are admin settings (`assist_calls_per_minute` / `assist_calls_per_day`, defaults
  6 / 60, 0 = unlimited); whether to raise the daily default is left to the maintainer.

## Links

- docs/architecture/identity-and-access.md
