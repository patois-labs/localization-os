# 0081. Jira jql and Confluence space_key / cql are admin-only

Status: Accepted
Date: 2026-10-11

## Context

#419 residual: a non-admin could widen the scope of a Jira or Confluence connection that holds a stored credential
by editing jql or space_key. ServiceNow and Notion already had admin-only scope fields. The maintainer approved making them admin-only.

## Decision

- Jira `jql` and Confluence `space_key` and `cql` join `SCOPE_ADMIN_FIELDS` (`store_common.scope_admin_error`): once
  a credential is stored, only an admin may change them (blank counts as a change; case-sensitive compare).
- On create the caller supplies the credential, so create stays open to non-admins (jql is required).
