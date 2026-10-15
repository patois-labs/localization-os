# 0037. Client IPs come only from measured, explicitly trusted proxies

- Status: Accepted
- Date: 2026-09-27 (F8 #288); corrected 2026-09-28

## Context

Rate limits, lockout and audit need the real client IP. Trusting forwarded headers from anyone lets a
caller forge their address; trusting nothing collapses every client to the edge address.

## Decision

- `LOC_TMS_TRUSTED_PROXY_IPS` lists the measured proxy peers only (/32 or /128), never a guessed range,
  and never loopback, `100.64.0.0/10`, `fd7a:115c:a1e0::/48` or `fdaa::/16`. There is no automatic trust
  derived from a hosting platform's environment variables.
- Measure the peers your reverse proxy or platform edge actually presents (public traffic and health
  checks can arrive from different addresses), and list each one.
- Missing or unsafe trust is loud: boot ERROR, `/health` `ingress_config` not ok, a Security page banner.

## Consequences

A recreated proxy container or platform machine can get new peer addresses: re-measure before deploying.

## Links

- docs/DEPLOY.md
