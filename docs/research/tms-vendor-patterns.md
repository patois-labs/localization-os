# Vendor-interop research: TMS-side vendor-connection patterns (research notes, 2026-08-08)

> How major TMS platforms attach external LSP vendors to client projects - the model
> our vendor portal mirrors. Primary-source based.

## Smartling

### Vendor-connection model
Two patterns: (1) an **Agency Owner** account type - the LSP's PM creates accounts for their translators/reviewers inside the client's Smartling project, assigned to specific language pairs and workflow steps; (2) a general **Translation Vendor / Linguist** role added directly to a project. No separate "vendor tenant" - the LSP's people operate as scoped users inside the client's single account. Agency setup is white-glove (CSM-mediated), not self-serve API.

### Job handoff lifecycle
A **Job** (named batch of strings/files, due date, cost estimate, custom attributes) is created → `Awaiting Authorization` → PM (or API) **authorizes** → content released into the configured **workflow** (Translation → Editing → Review) → `In Progress` → vendor users work **inside Smartling's browser CAT tool** (not an offline package model) → strings clear the final step → `Completed`, optionally `Closed`. Rework by reopening/reassigning strings; QA issues raised in-app.

### API surface
REST API (`api.smartling.com`) + GraphQL: Authentication, Accounts, Projects, Source Files, Strings, Translations, **Jobs**, Glossary, Issues, Quality Checks, MT, Reports. Job endpoints: create, authorize, add locales/content, status polling. SDKs + CLI. No public webhook/callback system found documented for vendor-side job events (polling-based).

### Auth & tenancy
Bearer-token via per-user API Tokens. Vendor/agency users are scoped users within the client's account - role + assigned language pairs/steps, not a tenant boundary.

### Formats
50+ native formats normalized internally; in-browser work means no vendor-facing bilingual package in the typical flow.

### Pricing/quote
Jobs carry a cost-estimate field; rate/word-count data underlies Cost Estimation, but no public estimate endpoint confirmed - UI-surfaced.

### Analysis (copy/avoid)
Copy: collapsing "vendor" into "scoped user in the buyer's workspace" simplifies the data model and gives real-time visibility. Avoid: only works because Smartling owns the CAT tool; doesn't generalize to vendors who work in their own TMS.

### Sources
- https://help.smartling.com/hc/en-us/articles/1260804843689-Overview-of-the-API
- https://api-reference.smartling.com/
- https://help.smartling.com/hc/en-us/articles/1260804711510-Tutorial-Translation-Jobs-and-Workflows
- https://help.smartling.com/hc/en-us/articles/360000359574-Jobs-Overview
- https://help.smartling.com/hc/en-us/articles/4417200614555--Getting-Started-Guide-for-Agency-Account-Owners
- https://help.smartling.com/hc/en-us/articles/115003066613-Add-an-Agency

## XTM Cloud

### Vendor-connection model
**Inter-server (server-to-server) subcontracting**: the client's XTM Cloud instance connects to the LSP's own separate XTM Cloud instance as a "subcontractor"; chains multiple levels deep. Enabled by the contractor's Administrator, then configured per named subcontractor instance.

### Job handoff lifecycle
Project created on contractor instance → project (or specific languages/steps) **contracted out** → subcontractor's instance **synced in real time** - contractor monitors subcontractor progress live in its own Workbench → subcontractor linguists work in their own instance → completed work flows back through the live sync. Per-LSP custom-field visibility in multi-level chains.

### API surface
REST at `api.xtm-cloud.com`: `POST /projects`, `/projects/multi-create`, `/projects/{id}/target-languages`, `GET /projects/{id}/status` (fetchLevel BUNDLES/STEPS), workflow get/put/assign/start. Subcontractor-specific: `projectManagerType`/`subcontracted` flags, `GET /externals/users`, `GET /users/{userId}/rate-cards`, `GET /lsps/{lspId}/rate-cards`, `POST /projects/{id}/costs`. **Webhooks ("callbacks")**: per-project (in Create Project) or permanent URLs via config; JSON POST; events incl. project creation, job completion, source file updated, activity changed, due-date changed; retry policy **3 attempts, 5000ms response timeout, 60000ms between retries**. No signing/HMAC documented.

### Auth & tenancy
OAuth2 Password Grant, HTTP Basic, custom signature auth (API key + secret + timestamp → SHA256), SAML SSO. Genuinely separate tenancy - the LSP's data lives in its own instance; the connection is real cross-instance sync.

### Pricing/quote
Rate cards are a first-class API resource + a cost-generation endpoint computing project cost including subcontractor fees - the most explicit machine-exposed quote/rate mechanism among the four majors.

### Analysis (copy/avoid)
Copy if you want genuine LSP autonomy (vendor keeps own tool/TM/data residency). Avoid: real-time cross-instance sync + multi-level chains is a huge lift (distributed state, partial-failure handling, per-LSP visibility) - overkill unless targeting enterprise LSP networks.

### Sources
- https://help.xtm.cloud/en/xtm-cloud/13.8/en/multi-level-lsp-language-delivery-chain.html
- https://help.xtm.cloud/en/xtm-cloud/13.8/en/enabling-subcontracting.html
- https://help.xtm.cloud/en/xtm-cloud/13.8/en/setting-up-a-connection-with-xtm-cloud-contractor.html
- https://api.xtm-cloud.com/project-manager-api-rest/documentation/xtm-docs
- https://xtm-cloud.atlassian.net/wiki/spaces/EXKB/pages/3682762753
- https://www.xtm-cloud.com/rest-api/

## Phrase (TMS, formerly Memsource)

### Vendor-connection model
**"Sharing"**: the vendor generates a **Vendor token**; the Buyer registers it under Settings > Administration > Vendors. Jobs are explicitly shared to Vendor organizations by assigning "Provider(s)." Sharing is transitive - Vendor1 can re-share with Vendor2, with cascading notifications back up the chain.

### Job handoff lifecycle
Buyer designates Vendor(s) as Provider on a job → Vendor gets "New Work" notification; sees ONLY jobs directly assigned (not the whole project) → Vendor works the job **inside Phrase TMS** in their own org context (own linguists, own TM/TB attached, can split/join, run analyses, use buyer's MT for pre-translation) → completion notifications propagate through each chain link → Buyer sees vendor-set job status but not vendor linguist identities. Rework = un-accept/reassign or add workflow steps.

### API surface
REST (OpenAPI 3.0) at developers.phrase.com: Buyer, Client, Connector, Conversations, Cost Center, Custom Fields, **Job**, Language AI, **Price List, Project, Provider, Quote, Vendor**. **Webhooks**: job status change/assignment/creation, due-date change; up to 10 retries with backoff to max 30-min interval; auto-deactivates (with admin notification) after 3000 cumulative failures. **Analysis** (TM leverage/match-band breakdown) downloadable and shareable with a vendor as JSON.

### Auth & tenancy
Each Vendor org is a genuinely separate account/tenant, linked via the Vendor-token handshake; access scoped strictly to explicitly-shared jobs.

### Pricing/quote
Most explicit of the four: dedicated **Price List**, **Quote**, and **Analysis** API resources. Analysis computes match-category word counts (match0/50/75/85/95/100, repetitions, internal fuzzies, non-translatables) and leverage - the TM-band breakdown feeding cost is a first-class, vendor-shareable API object.

### Analysis (copy/avoid)
Copy: "share a job, not a project" scoping (vendor cannot enumerate unassigned jobs) - clean minimal-blast-radius model; transitive re-sharing via notification cascades handles sub-vendor chains cheaply without XTM's full sync. The Price List/Quote/Analysis triad is worth mirroring directly. Avoid: 3000-failure webhook deactivation is loose - stricter dead-letter/backoff surfaces breakage faster.

### Sources
- https://support.phrase.com/hc/en-us/articles/5709703948444-Sharing-TMS
- https://developers.phrase.com/en/api/tms/latest/introduction
- https://support.phrase.com/hc/en-us/articles/5709693398812-Webhooks-TMS
- https://support.phrase.com/hc/en-us/articles/5709712007708-Analysis-TMS
- https://support.phrase.com/hc/en-us/articles/17246425155996-Continuous-Analysis-TMS

## memoQ TMS

### Vendor-connection model
Two distinct patterns: (1) **Subvendor groups** - offline/semi-offline: admin assigns documents to a named subvendor group; work via exported **handoff packages**; (2) **Content Connector / online projects** - server polls a content source (FTP, CMS, VCS) feeding a content-connected online project with rule-based auto-assignment.

### Job handoff lifecycle (subvendor groups)
PM assigns documents to a subvendor group → memoQ generates a **handoff package** (`.mqout`: documents as MQXLIFF/XLIFF bilinguals + optional local TM/TB corpora) → transferred to the subvendor, who opens it in their own memoQ (or any XLIFF-capable CAT tool via "Plain XLIFF for other tools" export) and works offline → subvendor exports a return/update package → PM imports it back via `ImportHandoffPackageIntoNewTemplatedProject` / `ImportHandoffUpdatePackageIntoAServerProject`.

### API surface
**WS-API** (SOAP/WSDL): the import-handoff methods above, `ListProjectTranslationDocuments2`, `ListTranslationDocumentAssignments`, `SetTranslationDocumentAssignments` (first-accept, group-sourcing, subvendor-group assignment modes). No webhooks documented - request/response/polling. Licensing via CAL (concurrent, online) or ELM (per-user, offline).

### Formats
**MQXLIFF** (memoQ's XLIFF superset, lossless round-trip) default; **"Plain XLIFF for other tools"** strips proprietary extensions for non-memoQ tools; packages (`.mqout`/`.mqxlz`) can bundle LiveDocs corpora.

### Pricing/quote
No dedicated Price-List/Quote API analog found - analysis/statistics exist as reports, not API-first.

### Analysis (copy/avoid)
Copy: the **dual-mode design** (offline package handoff for arms-length vendors vs live online projects for integrated pipelines) - not every vendor relationship wants real-time sync; buyer picks per-relationship. Also the deliberate degraded-fidelity "Plain XLIFF" escape hatch: don't force every vendor onto your proprietary format. Avoid: SOAP/WSDL transport - keep the package/online duality but over REST + webhooks.

### Sources
- https://helpcenter.memoq.com/hc/en-us/articles/7399508943249-Subvendor-workflow
- https://help.memoq.com/current/en/Places/assign-documents-to-subvendor.html
- https://docs.memoq.com/current/en/Workspace/memoq-handoff-wizard.html
- https://helpcenter.memoq.com/hc/en-us/articles/360010267720-MQXLIFF-format
- https://support.memoq.com/hc/en-us/articles/360010377759-memoQ-s-bilingual-export-formats
- https://docs.memoq.com/current/api-docs/wsapi/api/serverprojectservice/MemoQServices.IServerProjectService.html

## Crowdin / Lokalise / Transifex (brief)

- **Crowdin Enterprise**: Vendors Store + three flows - (1) **API-Integrated Vendors** (e.g., Gengo): a "Translation by API Vendor" workflow step; Create-order forwards untranslated strings to the vendor's API; vendor translates in their own system; translations delivered back automatically. (2) **Task-based vendors**: agency translators auto-added as temporary scoped project members, work inside Crowdin, auto-removed after. (3) Manual/PM-mediated. Webhooks: POST/GET per event, customizable payloads, optional custom auth headers; auto-disable after ≥100 failures in 24h; 30s timeout. Order history shows language, word count, status, price; no public quote API.
- **Lokalise**: broker model - built-in **Orders** against a fixed provider roster (Lokalise, Gengo, Google, DeepL + enterprise LSP partners). Orders API returns per-order word counts per language and total cost in USD - quote/actual cost is a native API field. No external vendor tenancy.
- **Transifex**: similar broker - "Order translations" from a small fixed partner set (e2f in-platform; Gengo/TextMaster external; SimulTrans support-brokered). Ordering appears UI-only; no public Orders API found.

Sources:
- https://support.crowdin.com/enterprise/translation-by-api-vendor/
- https://support.crowdin.com/enterprise/translation-proofreading-by-vendor/
- https://support.crowdin.com/webhooks/
- https://developers.lokalise.com/reference/list-all-orders
- https://docs.lokalise.com/en/articles/2141000-professional-translation-services
- https://help.transifex.com/en/articles/6235673-ordering-translations

## CROSS-PLATFORM SYNTHESIS (agent's analysis)

1. **Two fundamentally different vendor topologies dominate.** (a) "Vendor as scoped account inside the buyer's single tenant" - Smartling, Phrase (buyer-hosted work), memoQ online variant, Crowdin task-based. (b) "Vendor as a genuinely separate system federated via API/sync" - XTM inter-server, memoQ package handoff, Crowdin API-vendor flow. Enterprise/LSP-network platforms lean (b); SaaS product-team platforms lean (a) or hybrid.
2. **Package export/import (offline, file-based) is legacy-but-persistent** - exists because arms-length vendors often refuse server access or work offline; increasingly a "vendor won't integrate" fallback, not the primary path.
3. **Job-scoped, not project-scoped, vendor visibility is the emerging permission norm** (Phrase shared-jobs-only, Crowdin step-scoped, Smartling pair/step-scoped): give the vendor a job/step, never the whole project.
4. **Transitive sub-vendor chains**: Phrase does it cheaply via re-share + notification cascades on its job-sharing primitive; XTM does it "properly" via full multi-instance sync - much heavier.
5. **Webhooks are the standard mechanism, but signing/HMAC verification is largely undocumented publicly** (Phrase, XTM, Crowdin - auth via self-configured custom headers, not platform-issued signatures). A new build can differentiate with first-class HMAC-signed webhooks.
6. **Quote/pricing machine-exposure varies widely.** Phrase (Price List + Quote + Analysis with full match-band breakdown) and Lokalise (per-order cost in USD) expose quoting as real API objects; Smartling/Crowdin/Transifex UI-only; memoQ reports-only. Exposing match-band TM analysis as a shareable API object (Phrase's model) is the standout differentiator worth copying.
7. **Marketplace/broker platforms narrow vendor choice to a curated pre-integrated list** - zero-integration UX for buyers without existing LSP relationships.
8. **File format strategy is consistently "proprietary superset of XLIFF, with a degraded plain-XLIFF escape hatch"** (MQXLIFF vs Plain XLIFF; TXLF; SDLXLIFF). Assume any given vendor is unwilling to work in your UI and needs a genuine standards-based export path.
