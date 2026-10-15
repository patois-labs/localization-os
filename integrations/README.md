# Localization OS integrations - the two sides of a connector

Connecting Localization OS to Jira/Airtable has **two sides**, and you only strictly
need the first one:

### 1. This side (the Localization OS connector) - always required
Configured in the app under **Connectors**. You give Localization OS an API token for
the external system; Localization OS then **pulls** content in (creating a project +
document you translate with MT / a workflow / the editor) and **pushes** the
translations back. This works against a plain `localhost` install - nothing has
to be deployed anywhere.

### 2. Their side (a companion app) - optional, for in-context UX
A small app that runs **inside Airtable or Jira** so a user can click a button
there ("Send to Localization OS") instead of opening Localization OS. It doesn't do the
translating - it just calls Localization OS's **inbound API** to trigger a sync.

```
  Airtable / Jira  ──(button)──▶  companion app  ──HTTPS──▶  Localization OS inbound API
                                                             /api/connectors/{id}/pull
                                                             /api/connectors/{id}/push
```

## Inbound API contract
Companion apps authenticate with a **per-user API key** from Account, sent in the
`X-Loc-Token` header. Writes require an admin or linguist role and respect the user's
org and customer access. Legacy shared connector tokens are refused, including for
connection lists, job status, text translation and capability cards.

The Airtable extension asks each viewer for a key and keeps it only in memory.
GlobalConfig is visible to all base collaborators: remove old `apiToken` values and
rotate previously shared keys. Automation inputs and Forge backends exercise the
configured account's authority; use a dedicated customer-confined linguist account
and restrict who can run those integrations.

Inbound signed webhooks dedup using signed body fields or the body hash, never unsigned
delivery headers. Zendesk signs timestamp plus body and requires freshness within five
minutes. Include a changing edit identity in thin payloads. With multi-org enabled,
suspended organizations cannot accept webhooks or run scheduled or queued connector jobs.

| Method | Path | Purpose |
|--------|------|---------|
| `GET`  | `/api/connectors/health` | liveness probe: `{ok, version}`, **no token needed**. A docs-scoped key additionally gets a `connectors` array of the enabled connector kinds. |
| `GET`  | `/api/connectors` | list connections (token) |
| `POST` | `/api/connectors/{id}/sync` | **Translate now**: fan out to all the connection's languages (one call). On a Jira connection this is the saved-JQL batch: admin account only |
| `POST` | `/api/connectors/{id}/jira/translate` | translate issue(s) → labelled comments - `{issue_key\|issue_keys\|jql, target_locales}`; a `jql` run or more than 10 issues is a batch, admin account only |
| `POST` | `/api/connectors/{id}/confluence/translate` | translate page(s) → child pages - `{page_id\|page_ids\|space_key\|cql, target_locales}` |
| `POST` | `/api/connectors/{id}/airtable/webhook` | convention-driven Airtable run - `{base_id, table, record_id(s), source_field, jobs}` |
| `POST` | `/api/connectors/{id}/pull` | (Airtable/Jira) import source content → a Localization OS document |
| `POST` | `/api/connectors/{id}/push` | (Airtable/Jira) write the latest translations back |

Every endpoint except `/health` requires the `X-Loc-Token` header. The Airtable
automation calls **`/airtable/webhook`**, the Jira Forge app calls **`/jira/translate`**,
and the generic "Translate" button calls **`/sync`** (which dispatches per connector
kind). `pull`/`push` run a single stage (e.g. pull, post-edit in Localization OS, then push)
and are Airtable/Jira only - Confluence writes via `/confluence/translate`.

## Reaching a localhost Localization OS from the cloud
Airtable Extensions and Atlassian Forge run in the vendor cloud, so they can't
see `http://127.0.0.1:8000`. For real use, expose Localization OS with a tunnel and
point the companion app at the public URL:

```bash
cloudflared tunnel --url http://127.0.0.1:8000      # or: ngrok http 8000
```

## The scaffolds
- [`airtable-extension/`](airtable-extension) - an Airtable Custom Extension (Blocks SDK).
- [`jira-forge-app/`](jira-forge-app) - an Atlassian Forge app (issue panel).

Each is a self-contained starter you build/deploy with the vendor CLI; see its
own README.
