# 0084. Custom project fields: managers define customer-scoped fields; org-wide stays admin

Status: Accepted
Date: 2026-10-13

## Context

Projects needed typed custom fields (for example a Jira ticket link, a cost center) that show as list columns and
filters on web, CLI, API and MCP. The spec proposed admin-only definitions; a second round opened them to managers.

## Decision

- Admins define org-wide fields. Managers define fields scoped to a customer they can see. Edit, archive and restore
  are creator-or-admin; scope, key and type cannot change after create.
- A customer field key may not shadow an org-wide key. Values are written by whoever can edit project settings.
- Regex patterns on text fields are checked for catastrophic backtracking (every variable-length repeat counts).
- Kill switch: module `custom_fields` (writes_off) and setting `custom_fields_enabled`; values are kept when off.

## Consequences

Field-driven Jira live status and auto-create are phase 3 (spec section 4 and 5.6).
