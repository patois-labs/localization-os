---
module: connectors
title: Connectors
summary: Push-model companions that translate content in place in external systems - Airtable, Jira, Strapi, Contentful, Shopify, HubSpot, Akeneo, WordPress, GitHub, GitLab, Bitbucket, Microsoft 365 Files (SharePoint and OneDrive), ServiceNow, Sitecore, Plytix, Google Docs, Sanity, Drupal, Confluence, Zendesk, Salesforce Knowledge, Notion and more (28 kinds, one of which reaches any CMS through that CMS's own MCP server) - pulling content in, translating it through the ordinary pipeline, and writing each language back as the source system's own per-locale version. An admin can enable or disable individual connector kinds per install.
nouns:
  Connector kind: "A supported external system type (airtable, jira, confluence, zendesk, document360, freshdesk, zoho, intercom, salesforce, notion, helpscout, strapi, contentful, shopify, hubspot, wordpress, akeneo, mcp_cms, github, gitlab, bitbucket, msgraph, gdocs, sanity, sitecore, drupal, plytix, servicenow; drupal is experimental and mcp_cms is a prototype). No kind is archived right now: every retired kind has been rebuilt. One central registry lists every kind; the connectors surface renders generically across them."
  Connection: "One configured link to an external account of a given kind - its credentials, its scope (a JQL, a base/table, a collection map), and its default content type. You can have many connections of the same kind."
  Translate now: "The one-click round-trip on a connection: pull the matched content, translate it, and push each language back - synchronously or as a background job."
  Webhook intake: "A generalized signed inbound endpoint: an external system notifies Localization OS that content changed, the delivery is verified, and the change is recorded (button-trigger by default - no surprise spend - or auto-translate when you opt in)."
  Source-context harvest: "The differentiator: when a connector ingests content it harvests the source system's own metadata (title, section, labels) straight into the document's translation context, so the model translates knowing what the content is. Allowlisted, sanitized, and layered below explicit context."
  Archived kind: "A code-tier state: a kind retired from NEW connections pending a rebuild. Existing connections of an archived kind keep working and stay fully drivable; only creation is blocked."
  Disabled kind: "An install-tier state: a kind an admin has switched off on THIS install via connector_disabled_kinds. A disabled kind disappears from every surface entirely - the kill-switch law, applied per kind."
  Manage / Catalog: "Two admin pages. Manage lists every kind on this install with an enable/disable toggle; Catalog (Get more connectors) shows every connector developed - active, archived and planned - with an enable button for disabled ones."
verbs:
  See your connections: "Web: the Connectors page (it shows only enabled kinds). CLI: `connector list`, and `connector show 5` for one connection's config and credential state (never the secret values)."
  Add a connection: "Web: pick a kind on the Connectors page and fill its fields. CLI: `connector create --kind airtable --name \"Support base\"` with repeatable --config and --secret flags. Auth is per-connection API token (zero-setup) or OAuth."
  Translate content: "Web: Translate now on a connection. CLI: `connector sync 5` runs the automated pull, translate and push round-trip; `connector pull 5` and `connector push 5` are the halves."
  Test a connection: "Web: the connection's Test control. CLI: `connector test 5 --offline` uses a deterministic canned transport (CI/agent smoke) instead of the network."
  Localize a repository: "Web: Translate now on a GitHub or GitLab connection. CLI: `connector github-translate 5` or `connector gitlab-translate 5` (optionally --article-ids and --locales); API: POST /api/connectors/{cid}/github/translate or /gitlab/translate; MCP: connector_translate. Bitbucket Cloud: `connector bitbucket-translate 5`, POST /api/connectors/{cid}/bitbucket/translate. The source files are imported into a key project, translated, and the target-language files come back as ONE commit on a working branch and ONE pull request (GitLab: merge request). The base branch is never written and the request is never merged for you."
  Manage which kinds are available: "Web (admin): the Manage connectors page toggles each kind; Get more connectors opens the catalog. CLI/headless: `config set connector_disabled_kinds \"jira,shopify\"` - the setting is a CSV of kind slugs, and it is the twin of the toggle."
needs:
  - "A connection of an enabled, non-archived kind, with credentials the external system accepts (an API token, or an OAuth grant set up in the Settings UI - the browser consent leg is web-only)."
  - "For webhook/push intake from a cloud system: a public HTTPS base URL, since Atlassian/Airtable cloud cannot reach localhost. Set App base URL first, or use a poll schedule instead."
  - "For a disabled or archived kind: nothing to do until it is re-enabled. A disabled kind's connections are preserved and hidden; creating a new connection of an archived kind is refused with a clear message."
limits:
  - "One run fetches at most MAX_DOCUMENT_SEGMENTS + 1 records, each under 10 x MAX_SEGMENT_CHARS characters, and at most CONNECTOR_RUN_CHAR_BUDGET (default 50000000) characters in total. A run over the total is refused whole with the reason on the run and the connection (nothing stored, no translation); raise the setting on a large host or narrow the connection's scope."
  - "GitHub connections keep their strings in a KEY project (the key-projects setting must be on). A key removed from the source file follows the key-project delete rules; a key that is needs-review, blank or tag-damaged is held back, never written as a translation, and the file keeps the value already in the repository. JSON (flat or nested), gettext PO and every resource format with a key view (Java .properties, Apple .strings, INI, RESJSON, YAML, and each further resource filter as it lands) are supported; a resource file's translations are written into a copy of the repository's own source file, so its comments, order and encoding stay as they are. Other formats are refused with a clear message."
  - "The Connectors SECTION is core and always-on - there is no section-level switch. Granularity is per-KIND instead: an admin disables individual kinds, and the section itself never goes away."
  - "A disabled kind is non-enumerable, per the kill-switch law: its tile vanishes from the Connectors page, its per-kind routes 404, create refuses, and every verb targeting an existing connection of that kind refuses - the scheduler skips those connections and the webhook 404s before signature work. Re-enabling restores everything; no data is lost."
  - "Archived (code tier) and disabled (install tier) are orthogonal axes: archived blocks only new-connection creation while existing ones keep working; disabled hides the kind entirely on this install. A kind can be one, both, or neither."
  - "connector_disabled_kinds only ever SUBTRACTS from the registry: an unresolvable slug in the CSV is ignored, and a factory-added kind ships enabled automatically (absent from the CSV)."
  - "Source-context harvest captures a small allowlisted set of fields per connector, never the raw record, sanitized against prompt break-out; it is per-connection (harvest_context, on by default) and layered BELOW an explicit request or a content-type context card."
  - "OAuth authorize is browser-only (no device flow for these apps); token-auth connections are fully CLI-managed, but OAuth setup happens in the Settings UI."
connects:
  - module: documents
    how: "A connector pulls external content into an ordinary Localization OS document and pushes the translated result back, so parsing, the editor, QA flags and export all apply to connector content exactly as to an uploaded file."
  - module: workflows
    how: "Translate now runs the connection's content through the normal MT workflow, with the same run view, spend safety and TM/glossary leverage as any other translation."
  - module: glossary
    how: "Harvested source metadata (title, section, labels) enriches the translation context, which layers alongside glossary and content-type context to steer terminology and tone per connection."
  - module: engines
    how: "A connection can override the provider for its content; otherwise it uses the project's engine, and every connector run goes through the normal spend ledger."
---

## What this is for

Most of your translatable content does not live in files you upload - it lives in
Airtable, Jira, a CMS, a help center. Connectors let Localization OS reach into those
systems, **translate content in place**, and write each language back as the source
system's own per-locale version, either on a one-click Translate now or on a signed
webhook when content changes.

The differentiator is **source-context harvest**: when a connector ingests content,
it captures the source system's own metadata - the article title, its section, its
labels - straight into the translation context, so the model translates the support
FAQ knowing it is a support FAQ. It is allowlisted, sanitized, and layered below
anything you set explicitly.

## Kinds: two independent axes

A connector kind has two orthogonal states, and it helps to keep them apart:

### Archived - a code tier

A kind can be **archived**: retired from NEW connections pending a rebuild. This is
a property of the code, not a per-install choice. Existing connections of an
archived kind keep working and stay fully drivable through every verb; only creating
a new one is refused. No kind is archived in this release: Confluence, Zendesk,
Document360, Freshdesk, Zoho Desk, Intercom, Salesforce Knowledge, Notion and Help Scout
were rebuilt and are open for new connections again.

### Jira batches are admin-only

On a Jira connection, anything that runs the saved JQL or releases the pending set (Translate now,
`/api/connectors/{cid}/sync`, a JQL translate, more than 10 named issues, translate-pending, the
generic `connector_translate` job, a hand-written retry list of more than 10 issues, and turning on
a sync schedule) needs an admin account on the web, the API, the remote CLI and MCP. Linguists and
managers can still translate up to 10 named issues and turn a schedule off. Each named issue key must
have the plain `ABC-123` shape. (#517)

### Disabled - an install tier

An admin can **disable** a kind on this install via `connector_disabled_kinds` (a
CSV of kind slugs, toggled from the **Manage connectors** page or headlessly with
`config set connector_disabled_kinds "jira,shopify"`). A disabled kind disappears
**entirely** - the kill-switch law applied per kind: no tile on the Connectors page,
its per-kind routes 404, `connector create --kind` refuses, and every verb touching
an existing connection of that kind refuses through the one shared guard. The
scheduler skips those connections and the inbound webhook 404s before it verifies a
signature. Re-enable the kind from the **Catalog** (Get more connectors) and every
connection returns with its data intact.

The two axes are independent: a kind can be active-and-enabled, active-but-disabled,
archived-but-enabled, or archived-and-disabled. The **Connectors section itself is
core and always-on** - only individual kinds toggle.

## The normal path

1. **Add a connection** of an enabled kind with its credentials and scope.
2. **Translate now** (`connector sync 5`), or set a schedule, or wire a webhook so
   the system notifies you on change (button-trigger by default - no surprise spend).
3. Each language is written back as the source system's own per-locale version, and
   harvested metadata quietly improves the terminology as it goes.

## Vendor error text

When a source system answers with an error, the run message quotes it as
`vendor text: "..."`: a JSON error body is shortened to its error code and message (and
the first field that failed), markup, line breaks, invisible characters and anything that
looks like a credential are removed, and the excerpt stops at 160 characters. The same quoted text shows on the connection page, in
`connector sync` output, in `/api` replies and in MCP results, where connector results
also say `requires_human_publish: true` (an AI agent must ask you before publishing
anything in the source system).
