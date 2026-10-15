# 0041. Org-default context cards are admin-only; managers keep customer scope

- Status: Accepted
- Date: 2026-09-27

## Context

The context card with no customer, project or content-type scope applies to every project in the org.
Linguists could write it over the API, and web delete was not admin-only.

## Decision

The ORG-DEFAULT context card is admin-only for write AND delete on web, /api, remote CLI and MCP.
Managers keep access to customer defaults, pairs and customer-scoped context cards
(`fix-customer-managers`).

## Links

- docs/architecture/context-and-adaptation.md
