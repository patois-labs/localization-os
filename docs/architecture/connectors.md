# Connectors: Architecture (L1)

## 1. Role

This subsystem is the **integration layer to external content systems** (Jira, Airtable,
Zendesk, Contentful, and the rest): the connector framework and its registry, the shared
translate-and-write-back flow with its spend ceilings and egress guard, the per-department
budgets, and the Prompt Tap request observer with its envelope history.

A connector (`app/services/connectors/base.py`) knows how to talk to one external system
through injected credentials and an injected httpx transport, so the whole surface is
unit-testable offline. The formal capability declaration and six-verb lifecycle live in the
contract (`app/services/connectors/contract.py`); the polymorphic write-back seam is the
locale-mapping strategy (`app/services/connectors/strategy.py`), whose topology (not the
vendor) decides the read/write logic. The shared translate pipeline
(`app/services/connectors/flow.py`) owns the cross-cutting stages once (the write ceiling,
the egress guard, the refusal exclusion) so a per-connector spec supplies only the
vendor-specific hooks. The registry (`app/services/connectors/__init__.py`) maps a slug to
its connector, and the store (`app/services/connectors/store.py`) holds connection rows and
the shared write budget.

Two guards protect real systems: the egress guard
(`app/services/connectors/egress_guard.py`) stops mock output from being pushed to a live
system, and per-department budgets (`app/services/budgets.py`) cap monthly translation spend.
The Prompt Tap (`app/services/prompt_tap.py`) is an opt-in read-only observer of the exact
provider request; the tap envelopes (`app/services/tap_envelope.py`) are the always-on
action/run history the viewer (`app/services/tap_view.py`) reads.

It does not own the translation itself (translation-pipeline hands units to the workflow and
returns them), the background jobs and scheduler that drive a connector sync
(jobs-and-scheduler), or the individual vendor adapters, which are plugins on this framework
and are documented as they are hardened.

## 2. Invariants

Connector action authorization resolves the connection's stored project as well as its
organization and customer. A global connection cannot grant access to an inaccessible
project, including through generic jobs, dedicated API actions, or web actions (#370).

Connector API responses have a 32 MiB cap on both wire and decoded bytes for saved
connections (including tests, browse, pull, push and sync). The unsaved browse/test
door uses 8 MiB. The shared HTTP helper streams success and error bodies and closes
rejected responses. Airtable, WordPress and Confluence list requests retry an
oversized page once at half its size, preserving its position. A second size refusal
asks the user to reduce source content or selected fields. Airtable requests only
the source, target and title fields used by the connection or dynamic translation jobs.

Each HTTP request has one 30-second I/O budget shared across all retry attempts.
Socket operations use the remaining budget, including reads before headers arrive.
Actual backoff sleep time extends the deadline and does not spend the I/O budget.
Numeric Retry-After on 429/5xx overrides exponential backoff, and Contentful's
X-Contentful-RateLimit-Reset is honoured the same way when Retry-After is absent;
HTTP-date values use the backoff fallback. Each sleep is capped at 60 seconds, with at most 120 seconds
of sleep per request and three attempts. If another wait would exceed that total,
the provider response is returned, preserving its retryable 429 rate-limit status
(the service asked us to slow down), instead of reporting an I/O timeout.
Gzip (including concatenated members) and zlib or raw deflate are decoded with a
bounded output buffer under the same cap. Unsupported content encodings are refused.

These limits apply to unsaved and saved browsing, connection tests, pull, push,
and sync through the same service used by web, API, CLI and MCP. Error-reporting
operations ask the caller to try a smaller request or retry later; best-effort
container pickers retain their empty-list fallback. Limits are per HTTP request,
not per entire paginated sync. Jira attachment downloads retain their separate
attachment byte limit; fixed-host OAuth exchanges use their separate client.

- **OAuth consent is bound to the connection scope at start.**
  The callback rechecks the current role, organization and customer ring before
  exchanging a code. It also compares the original kind, organization, customer
  and creation timestamp with the current connection. Missing or changed connections
  return 404 and consume the pending state. The store repeats authorization under
  a database write lock and commits credentials and CONNECTED status together,
  closing the scope-change window during the provider exchange. See
  app/services/connectors/oauth.py, app/web/connectors_common.py and
  app/services/connectors/store_part2.py; verified by verify_connector_isolation_gaps.
- **Airtable calls stay inside the connection's configured base and table.**
  Every store function that accepts a caller-supplied base or table runs
  `store_common._airtable_scope_target` first: `dynamic_translate` (the convention
  webhook and `connector airtable-translate`), `register_airtable_webhook`,
  `schema_fields` (the field picker and `connector browse --table`) and the record
  create/delete pair (#268). An override that differs from the configured value, or
  any override on a connection with no configured base/table, is refused before a
  request leaves the process. Because the configured base/table is the scope, a
  non-admin may not change it while the connection holds a saved credential
  (`_airtable_retarget_error`: /api update answers 403, the web form refuses)
  unless the same web save enters their own token; an OAuth-mode connection
  always needs an admin, since a typed token would sit unused beside the OAuth
  one. Refusals write a `connector.retarget_refused` audit row. Verified by
  verify_airtable_scope.
- **A Google Docs translation is all-or-nothing per document (gdocs).** A document with ANY unit held for review (NEEDS_REVIEW) is neither created nor updated: no copy appears, an existing translation keeps every stored translation, no content hash is recorded, and the pair is retried after approval (`flow.variant_exists` pinned False in `app/services/connectors/gdocs.py`). The copy is made under a visible "[Translating] " name and renamed only after the single atomic Docs batchUpdate succeeded, so a half-built copy is never mistaken for a finished one.
- **A Google Docs translation edited by hand is left alone (gdocs).** After each write the copy's Drive `modifiedTime` is stored in `gdocs_target_tags`; before re-translating a changed source the copy is read back and, if it moved, the pair is parked with no MT spend. The Docs write carries `requiredRevisionId`, so an edit between the read and the write fails (400) instead of being overwritten. A text-only source change rewrites the existing translated Doc IN PLACE (same file id, so readers' links, sharing and comments survive) through one atomic batchUpdate; this needs the recorded layout (`gdocs_written`: source-to-copy segment map, source shape digest, text digest of what we wrote) and is refused for a changed structure or a Doc whose text no longer matches what we wrote (a quiet hand edit), in which case a fresh copy is built, gets the sharing a person added to the old one (Drive permissions copied with no notification email; owner, inherited and service-account entries skipped), and the old one is trashed only after the id map names the new one; if the sharing cannot be copied the old Doc is kept (not trashed) with a warning (never a permanent delete; the link changes, with a warning). Google Docs has no draft state, so every write (create, in-place update, rebuild) is LIVE for anyone reading the translated Doc; an in-place update additionally requires every source unit to sit in the same paragraph / position as the unit it overwrites (`DocUnit.slot`), else it rebuilds. Switches: `update_in_place=0`, `protect_edited_copies=0`; live check.
- **Mock output never egresses to a real external system.**
  `app/services/connectors/egress_guard.py::assert_egress_allowed` fires only when the egress
  is real (not an httpx mock transport) and the MT is not live, so a keyless run cannot push
  placeholder text to a real system unless `allow_mock_egress` is set. Enforced by
  `verify_mock_egress`.
- **The shared flow owns the per-run write ceiling once.**
  `app/services/connectors/flow.py::run_translate_flow` applies the shared
  `app/services/connectors/store_common_leaf.py::WriteBudget` so a batch larger than the ceiling stops at
  the ceiling, reports `ceiling_hit`, leaves the remainder unwritten, and does not advance the
  watermark past a trimmed item. Enforced by `verify_connector_ceilings`.
- **A connector is transport-injected and runs fully offline.** Every network call on
  `app/services/connectors/base.py::Connector` goes through the injected transport, so
  `connector test`/`browse --offline` succeed against a canned mock transport with no network
  or keys. Enforced by `verify_connector_offline`.
- **Department spend is attributed to the submit-time stamped department.**
  `app/services/budgets.py::check` gates a submission and
  `app/services/budgets.py::spend_this_month` aggregates only the current month's jobs stamped to
  the department; an unset or limit-0 department is unlimited, and a department at its cap is
  refused (the /api path enqueues zero jobs). Enforced by `verify_budgets`.
- **Raw Prompt Tap capture is off by default and makes no new payload writes when off.**
  `app/services/prompt_tap.py::enabled` reads `prompt_tap_enabled` (default `"0"`), and with it
  off the capture seam is skipped and `app/services/prompt_tap.py::capture` re-checks before it
  would touch the filesystem, so a disabled install creates no tap files or directories.
  Enforced by `verify_prompt_tap`.
- **Tap envelopes are the always-on history and stop cleanly when disabled.**
  Action kinds are `workflow`, `direct`, and `advanced`. An Advanced mission run creates
  one action and one language-pair leg before its RUNNING commit. Its `action_id` is the
  ledger action id; capture `run_id` is the TapRun id, with `advanced_correlation_id`
  retaining `advrun-N`. After the terminal commit, completed maps to completed, every
  failed shape to failed, and cancelled to abandoned. Observer writes use isolated short
  sessions; disabled or unavailable envelopes preserve the UUID/advrun capture fallback.
  The run's own budget and spend accounting remain authoritative and unchanged.
  `app/services/tap_envelope.py::envelopes_enabled` defaults on (fail-safe to on), and with the
  `prompt_tap_envelopes_enabled` setting set to `0` a run creates no envelope rows. Enforced by
  `verify_tap_envelopes`.
- **The tap viewer is org-confined and admin-only.**
  `app/services/tap_view.py::record_visible_to_org` filters a captured record to the caller's
  org, and the viewer pages and download are admin-only. Enforced by `verify_tap_viewer`.

## 3. Where the work happens

`app/services/connectors/template_integrity.py` owns template span multiset comparison
for HubSpot, WordPress, and Document360 delivery. It checks both raw and entity-decoded
syntax without changing the payload. Added, changed, duplicated, or missing spans hold
the field (HubSpot) or article (WordPress and Document360), with reasons in shared run
reports and logs. Independent spans may reorder; raw/false wrappers require span order.
WordPress detection covers positional attributes and case-sensitive registered names,
including bracket prose such as `[see note]` that could execute a registered `see` tag.
Escaped `[[tag]]` cannot become live `[tag]`. HubSpot and Document360 also check assembled
bodies, including spans split across paragraphs. HubSpot compares against the chosen
skeleton with translated units replaced by their source units, preserving held target text.
Forward-only delimiter scans and at most eight entity-decoding layers bound validation
work; excessive encoding depth holds the field. WordPress masking keeps its narrower
syntax with possessive quantifiers to prevent backtracking on unclosed attributes.

| Concern | Pin | One line |
|---|---|---|
| Templates | `app/services/connectors/template_integrity.py` | Span multiset validation. |
| Guarded envelope controls | `app/services/ledger_controls.py` | Shared typed ceremonies, transactional audit, purge and retained raw-file status. |
| Guarded controls CLI | `app/cli/cmd_ledger.py` | Host operator twin of the Ledger settings controls. |
| Registry | `app/services/connectors/__init__.py` | The slug-to-connector `REGISTRY` the web, CLI, and /api surfaces resolve a connector through. |
| Connector base | `app/services/connectors/base.py::Connector` | The uniform test/containers/pull/push surface over an injected transport. |
| Capability contract | `app/services/connectors/contract.py::ConnectorPlugin` | The declarative capability + six-verb lifecycle for a new connector. |
| Write-back strategy | `app/services/connectors/strategy.py::FieldMapStrategy` · `app/services/connectors/strategy.py::SubResourceStrategy` · `app/services/connectors/strategy.py::DocPerLocaleStrategy` | The topology-driven write-back seam: field-map (T1), sub-resource (T2), and doc-per-locale (T3), each behind the one `LocaleMappingStrategy` interface. |
| Bounded fetch | `app/services/connectors/base.py::fetch_cap` · `app/services/connectors/base.py::item_char_limit` | Every connector record limit (max_articles, max_pages, max_entries, max_records) is clamped to `MAX_DOCUMENT_SEGMENTS` + 1, so a fetch stops requesting pages once the list is provably over the cap. A listed record over 10 x `MAX_SEGMENT_CHARS` is dropped before it is buffered (`Connector._keep_rows`, reason recorded on the run) or, for Airtable, Jira and Confluence, stops the run (`Connector._refuse_oversize`). flow.py refuses a run whose list is over the cap before any project row or engine call (#579). One run may also buffer at most `base.run_char_budget()` characters in total (the smaller of `MAX_DOCUMENT_SEGMENTS` x `MAX_SEGMENT_CHARS` / 10 and the `CONNECTOR_RUN_CHAR_BUDGET` ceiling, 50 million characters by default: measured at the #587 gate at about 70 to 240 MB of peak memory for ordinary articles and under 1 GB for a pathological source of tiny JSON objects; set the environment variable and restart to change it): `_keep_rows`, `_refuse_oversize`, `_charge_record` (every full by-id fetch and every listed-then-refetched record: Document360, Freshdesk, Zoho, Intercom, Zendesk, WordPress, Shopify, Strapi, Contentful, Akeneo, Help Scout, HubSpot), the Zendesk export and the Notion and Help Scout generic pulls charge it, and crossing it raises `FetchBudgetError`, which refuses the whole run (nothing stored, no engine call) (#587). The Notion pull lists pages it could not read (`skipped_pages`) in the pull message and run log. |
| Shared translate flow | `app/services/connectors/flow.py::run_translate_flow` · `app/services/connectors/flow.py::TranslateFlowSpec` | The one pipeline that owns the ceiling, egress guard, and refusal exclusion. |
| Egress guard | `app/services/connectors/egress_guard.py::assert_egress_allowed` · `app/services/connectors/egress_guard.py::egress_is_real` | Blocks mock output from reaching a real external system. |
| Connection store + budget | `app/services/connectors/store_common.py` · `app/services/connectors/store.py` · `app/services/connectors/store_common_leaf.py::WriteBudget` · `app/services/connectors/store_part2.py::is_oauth` · `app/services/connectors/store_part3.py::containers` · `app/services/connectors/store_part4.py::airtable_delete_records` · `app/services/connectors/store_common.py::confluence_translate` | Connection rows and the shared per-run write ceiling. |
| Tenant host allowlist | `app/services/connectors/tenant_host.py::TENANT_HOSTS` | The three vendors whose request host is composed from an operator-supplied handle; the handle must match a single ASCII DNS label, so it can never carry a path, a port, a second host, or a lookalike separator. |
| Credential destination pin | `app/services/connectors/store_part2.py::_credential_host` · `app/services/connectors/store_part2.py::_pin_secret_to_host` | Every connector kind is classified as tenant-host, config-key host, or fixed vendor host; a config write that moves the destination clears the stored credential and errors the connection. |
| Department budgets | `app/services/budgets.py::check` · `app/services/budgets.py::spend_this_month` · `app/services/budgets.py::check_locked` | Monthly per-department spend gating by the stamped department. Under multi-org the sum is per org (a job's org is its creator's, or the content it references; `spend_this_month(org_id=)`), and the queued translate doors re-check the cap and insert the Job under one lock (`submit_budgeted`, owned by jobs-and-scheduler.md; the API door reports locales it refused in `budget_refused`, or answers 429 when none was enqueued) (#503); that re-check is atomic across processes through a database write lock on the budget rows (`check_locked`, #622). |
| Prompt Tap capture | `app/services/prompt_tap.py::enabled` · `app/services/prompt_tap.py::capture` | The opt-in, off-by-default request observer. |
| Tap envelopes | `app/services/tap_envelope.py::begin_action` · `app/services/tap_envelope.py::commit_tap_write` · `app/services/tap_envelope_common.py::_now` | The always-on action/run history writer. |
| Tap viewer | `app/services/tap_view.py::list_actions` · `app/services/tap_view.py::record_visible_to_org` | The org-confined, admin-only read model. |
| Web surface | `app/web/connectors.py` · `app/web/connectors_common.py::connectors_index` | The `/connectors` management pages. |
| Connector CLI | `app/cli/cmd_connector.py::register` | The `connector` command group. |
| Prompt-tap CLI | `app/cli/cmd_prompt_tap.py::register` · `app/cli/prompt_tap_common.py::_strip_record_keys` · `app/cli/prompt_tap_verify.py::_actions` | Capture controls and root registration; shared tenant gate and key scrubber; action/run inspection. |
| Tap enrichment | `app/services/tap_enrich.py::enrich` · `app/services/tap_enrich.py::assemble_batch_manifest` · `app/services/tap_enrich_part1.py::_hash` | Populates the tap envelopes' asset manifest, per-leg metrics and glossary-adherence roll-up as identity + content-hash only, never a full-content snapshot. |
| Tap by run | `app/services/tap_runs.py::list_runs_page` · `app/services/tap_runs.py::run_detail` | The read-only, ring-confined per-run grouping lens over the captures: bodies-withheld digests over `/api`/MCP, full text host-only. |
| airtable connector | `app/services/connectors/airtable.py` · `app/services/connectors/airtable_part1.py::API` · `app/services/connectors/airtable_part2.py::SPEC` | airtable: part1; airtable: part2. |
| sitecore connector | `app/services/connectors/sitecore.py` · `app/services/connectors/sitecore_flow.py::sitecore_translate` | A T2 consumer of the Authoring GraphQL API: items under a content root, each language a version of the same item (addItemVersion then updateItem; a version whose fields failed is deleted again so language fallback survives); the live schema is introspected first and a mismatch fails closed; CM host must be under sitecorecloud.io; shared fields are never written, nothing is published. |
| akeneo connector | `app/services/connectors/akeneo.py` · `app/services/connectors/akeneo_part1.py::log` | akeneo: part1. |
| contentful connector | `app/services/connectors/contentful.py` · `app/services/connectors/contentful_part1.py::log` · `app/services/connectors/contentful_part2.py::ContentfulConnector` | contentful: part1; contentful: part2. |
| jira connector | `app/services/connectors/jira.py` · `app/services/connectors/jira_part1.py::log` · `app/services/connectors/jira_part2.py::JiraConnector` | jira: part1; jira: part2. |
| plytix connector | `app/services/connectors/plytix.py::PlytixConnector` | A PIM with no locale dimension: each language is its own attribute, mapped by an operator-supplied `attribute_map` (T1); a PATCH of the target attributes only, never the source attribute. Until merge semantics are proven per connection (`plytix_patch_semantics`), the first write of each run is a canary that re-reads the product and stops, restores and blocks the connection on any lost attribute. |
| salesforce connector | `app/services/connectors/salesforce.py` · `app/services/connectors/salesforce_common.py::API_VERSION` · `app/services/connectors/salesforce_part2.py::_sf_build_message` | salesforce: part1; salesforce: part2. |
| shopify connector | `app/services/connectors/shopify.py` · `app/services/connectors/shopify_part1.py::log` · `app/services/connectors/shopify_part2.py::ShopifyConnector` | shopify: part1; shopify: part2. |
| strapi connector | `app/services/connectors/strapi.py` · `app/services/connectors/strapi_field_classification_heu.py::log` · `app/services/connectors/strapi_part2.py::SPEC` | strapi: field-classification heuristics (review h3, tightened per n2); strapi: part2. |
| mcp_cms connector (prototype) | `app/services/connectors/mcp_cms.py::McpCmsConnector` · `app/services/connectors/mcp_wire.py::McpSession` · `app/services/connectors/mcp_oauth.py` | A CMS reached through its own MCP server: four mapped tools (list, get, write, publish), JSON argument templates, the T2 flow; a minimal sync MCP client over the guarded connector HTTP path. Both protocol eras (#754): `McpSession.connect` probes `server/discover` and stays on the stateless 2026-07-28 wire (per-request `_meta`, `MCP-Protocol-Version` / `Mcp-Method` / `Mcp-Name` / `Mcp-Param-*` headers with the base64 sentinel, no session, invalid `x-mcp-header` tools dropped, `input_required` refused) or falls back to the `initialize` handshake on any answer that is not a modern one; the `mcp_protocol` setting pins `legacy` or `2026-07-28`, and `call_timeout_s` (5 to 300 s) sets each call's I/O budget through `base.io_budget`. OAuth 2.1 MCP authorization (protected resource metadata, authorization server metadata, PKCE, dynamic registration or an operator client) discovers every endpoint per connection with an SSRF and scheme-downgrade check on each URL, completes through the shared connector callback route and refreshes through the single-flight `oauth.refresh` path; the vendor presets are UNVERIFIED until a live run. |
| Vendor text quoting (#748) | `app/services/connectors/vendor_text.py::vendor_excerpt` · `app/services/connectors/vendor_text.py::vendor_quote` | Every vendor HTTP body, GraphQL error message and remote MCP server error that a connector puts into its own message goes through `vendor_excerpt`: a JSON error body is first summarized to code + message + first field error (HubSpot category, Contentful sys.id/details, Jira errorMessages/errors, Strapi/Graph/Google `error` objects), markup, control, bidi and zero-width characters and newlines are stripped, credential-looking values (`Bearer ...`, `api_key=...`) become `***` before the cut, the excerpt is capped at 160 characters and quoted as `vendor text: "..."`, so a hostile vendor body cannot forge a second line or read as our instruction on the web page, in the CLI, or in an MCP agent's tool result. Never interpolate `r.text[:N]` into a message. |
| gdocs connector | `app/services/connectors/gdocs.py::GoogleDocsConnector` · `app/services/connectors/gdocs_google.py::GoogleMixin` | Google Docs: a Drive folder (My Drive or a shared drive) of Google Docs. Each translation is a Drive `files.copy` of the source whose text is replaced by one atomic Docs `batchUpdate` (styles, lists, tables, links, tabs kept), written as a new Doc in a per-locale folder under the target folder (T3 id map). Service account key; held units, hand-edited copies and out-of-scope ids never cause a write. |
| msgraph connector | `app/services/connectors/msgraph.py::MsGraphConnector` · `app/services/connectors/msgraph_graph.py::GraphDriveMixin` | Microsoft 365 Files: SharePoint libraries and OneDrive as Graph drives. A folder-based round trip (source folder, our existing file filters, translated copies in per-locale folders under a target root, T3 id map). App-only client credentials; held units, hand-edited copies and out-of-scope ids never cause a write. |
| drupal connector | `app/services/connectors/drupal.py::DrupalConnector` · `app/services/connectors/drupal_fields.py::classify` | EXPERIMENTAL (alpha-dependent: JSON:API Multilingual 1.0.0-alpha5). Drupal entities (nodes, terms, custom blocks, media) as per-language translations (T2) over core JSON:API plus that module: every PATCH carries the target `langcode` (the module validates it; core without the module refuses to relabel the source), an existence probe that proves the module is active before and again right before every write (a write answered in the wrong language halts the run), unpublished creates, published translations left alone unless opted in, whole-field formatted text, Simple OAuth / Basic / Bearer auth. |
| github connector (git core) | `app/services/connectors/github.py::GitHubConnector` · `app/services/connectors/git_core.py::run_git_translate` | Pull-request localization of resource files: the source file is imported into a key project, translated, and the target files come back as ONE commit on a working branch and ONE pull request (never the base branch, never forced). `app/services/connectors/git_core.py` holds the provider interface (`GitProvider`), the path and branch safety rules and the loop, so GitLab and Bitbucket are thin providers; `app/services/connectors/github.py` is GitHub REST v3 only. Branch ownership (gate 2026-10-10): the core records `<kind>_branch_state` (branch, head it wrote, base it forked from, generation; server-owned, `store.update`/`create` drop it from caller config) and only builds on a branch whose head is exactly that commit; a same-named branch it did not write is refused, a reviewer's commits on an open PR pause the connection, and when the base changes a target file under an open PR the run cuts `<branch>-N` from the base, opens a PR that names the old one and closes the old one (`GitProvider.close_pull_request`). The PR title is set once; updates rewrite only the block between the `localization-sync` markers (`update_pull_request(number, body)` has no title). Executable (tree mode 100755) and Git LFS pointer targets are refused per pair by the shared core (`RepoFile.kind` executable, or a content check for the LFS pointer header on small existing targets) instead of being overwritten. |
| bitbucket connector (git core) | `app/services/connectors/bitbucket.py::BitbucketConnector` · `app/services/connectors/git_core.py::run_git_translate` | Bitbucket Cloud provider on the shared git core (same loop, same ownership, base-moved and title/body rules as GitHub). Bitbucket specifics: fixed host `api.bitbucket.org` (no URL field; the base goes through `git_core.validate_api_url`, and `next` links and the commit `Location` header must pass the same pinned check, host, repository prefix, no userinfo, port, fragment or dot segments), scoped API token or repository access token (app passwords are deprecated, unsupported); ONE `POST /src` multipart commit with `branch` + `parents` (a stale tip is a 409, so the fast-forward is atomic); no ref-update call, so `GitProvider.can_fast_forward = False` and the core cuts `<branch>-N` after a merged pull request; the listing has no blob sha, so `RepoFile.sha` is a lazy git blob hash; symlink = `attributes:[link]`, submodule = `attributes:[subrepository]`, and a target marked `executable` or `lfs` is refused per pair (an upload without `x-attributes` would drop the bit or replace the LFS pointer); protected-branch pre-check reads branch restrictions only when the token has the admin scope (otherwise Bitbucket refuses the commit itself); old pull requests are declined, never merged. Runbook:. |
| gitlab connector (git core) | `app/services/connectors/gitlab.py::GitLabConnector` | Merge-request localization on the same core as GitHub, REST v4 only. Project path may include subgroups (`git_core.NESTED_REPO_KINDS`); the project is looked up once, pinned to `path_with_namespace` (archived refused) and then addressed by numeric id. Token: `PRIVATE-TOKEN`, scope `api` (GitLab documents `write_repository` as Git-over-HTTP only, so it cannot call the commits API); https-only base URL through the SSRF guard. One commit = one `POST /repository/commits` with an `actions` array (create or update per file, base64); the commits API has no whole-branch compare-and-swap (`start_sha` is rejected on an existing branch unless forced), so every update action carries `last_commit_id` (GitLab rejects an overwrite of a target file changed meanwhile) and the provider checks the branch head before and the new commit's parent after (a moved branch fails the run loudly, nothing is overwritten). GitLab has no ref-update call, so `can_fast_forward = False` and the core starts a fresh branch after a merge; merge requests are opened with `remove_source_branch`. Ancestry via `repository/merge_base`; tree listing is keyset-paginated (`Link rel=next`, followed only on the same endpoint); symlink and submodule detection from the tree `mode` and `type` (anything but a plain blob is refused). Protected: the branch's `protected` flag plus the `protected_branches` rules (wildcards matched). MRs are found by source and target branch AND project id (a fork's same-named branch is not ours). Executable (tree mode 100755) and Git LFS pointer targets are refused per pair by the shared core (`RepoFile.kind` executable, or a content check for the LFS pointer header on small existing targets) instead of being overwritten. |
| sanity connector | `app/services/connectors/sanity.py::SanityConnector` · `app/services/connectors/sanity_strategy.py::SanityFieldStrategy` | Sanity in BOTH localization models, chosen per connection: field level (internationalized-array language items, T1 delta patches guarded by `ifRevisionID`) and document level (a linked per-language document, T3 reserve-then-fill over the id map, a stateless rule 6 that never touches a translation Sanity links but this connection did not create). Drafts only, never published: a translation that is already published gets a draft overlay (built from the published translation, never the source), its live text is unchanged, and the pair is listed under `live_kept` in the run result; the only non-draft write is an inserted reference in the `translation.metadata` link document (a live-edit type in the plugin). Portable Text is translated block by block. |
| cmd_admin connector | `app/cli/cmd_admin.py` · `app/cli/cmd_admin_helpers.py::_register_tick` | cmd_admin: helpers. |

## 4. Data & settings

- **Models:** `app/models.py::Connection` (a configured connector connection),
  `app/models.py::ConnectorIdMap` (the external-to-internal id map for doc-per-locale
  topologies), `app/models.py::DepartmentBudget` (a per-department monthly cap), and
  `app/models.py::TapAction` / `app/models.py::TapRun` (the tap envelope history).
- **Settings:** `setting:prompt_tap_enabled` (default `"0"`) gates the Prompt Tap;
  `setting:connector_disabled_kinds` (default empty) lets an operator disable connector kinds.
  The per-run write ceiling is `app/config.py::MAX_WRITES_PER_RUN` (default 500, env), and the
  envelope history is controlled by the `prompt_tap_envelopes_enabled` setting (default on).

## 5. Enforced by

- `verify_template_injection`: added template syntax, count changes, encoded delimiters,
  plain fields, accessibility attributes, and unchanged or reordered source spans.
- `verify_mock_egress`: the egress guard's real-egress-and-not-live firing rule and its
  override.
- `verify_connector_ceilings`: the shared `WriteBudget` per-run ceiling and the trimmed-item
  watermark clamp.
- `verify_connector_offline`: the transport-injected offline `connector test`/`browse`.
- `verify_budgets`: the stamped-department attribution, the unlimited/at-cap gating, and the
  /api 429.
- `verify_prompt_tap`: the off-by-default zero-write posture and the admin-only download.
- `verify_tap_envelopes`: the always-on envelope recording and the disabled-off path.
- `verify_tap_viewer`: the org-confined, admin-only viewer.
- `verify_tap_runs`: the per-run grouping lens is read-only and best-effort, withholds bodies over `/api`/MCP (digest = lengths + full-text sha256 + a head only past `2*HEAD_CHARS`), confines a run to its ring (foreign run hidden from the list and a non-enumerable 404 on detail), and lets a TTL-swept run vanish for free.

## 6. Battle scars

- **A keyless connector run once pushed mock text to a real system.** In live QA a keyless
  write-back translated on the deterministic MockProvider and pushed `[xx] ...` placeholders to
  a real Jira; the egress guard now keys on live-MT-and-real-transport so mock output cannot
  egress, recorded in the `app/services/connectors/egress_guard.py` module docstring and proven
  by `verify_mock_egress`.
- **Four near-copy connector flows were the main defect generator.** The sibling T2 flows were
  near-copies of one implicit pipeline, so a cross-cutting guard had to be ported site by site
  and some never made it; the shared flow engine now owns the invariant stages once, recorded
  in the `app/services/connectors/flow.py`
  module docstring.
- **A mid-month department change must not migrate a job's spend.** Spend is attributed by the
  department stamped at submit time (the same key the budget gated on), so a re-department never
  moves a job's already-burned tokens, recorded in the `app/services/budgets.py` module
  docstring and proven by `verify_budgets`.

## 7. Limits & accepted risks

- **A pair with a held-back unit is never created or marked done (#640).** In the shared flow engine (`app/services/connectors/flow.py`, write-back stage) a (record, locale) pair where any unit is NEEDS_REVIEW or has no target text records no content hash, so the next run retries it after approval, and a NEW locale variant is not created while a unit is held (field-mapped creates would leave the field empty, doc-per-locale and article creates would fill it with the source text). An existing variant is still updated, the held unit keeping its stored value where the connector can. The run result carries `held_back` (pairs not created) and `partial_held` (pairs updated with a unit held). Shopify has no variant record, but an unregistered key shows the SOURCE text on the storefront, so it gates the first write the same way (a locale with no registered key yet is held; one that already has keys is updated); `exists_target` is not used for this because it means "every tracked key is covered". Contentful also refuses to create a locale when a field fails a CMA validation (a validation block: parked, not done, see #665 below).
- **A pair written incompletely drops its older stored hash (#666).** When the shared flow writes a pair with a unit held (#640) or a field the write left out (`write_incomplete`, #665), the pair's OLDER content hash no longer describes the target (some fields are new, some old), so `app/services/connectors/flow.py` stage 9 removes it (`FlowCtx.hash_drop`, persisted by `patch_config_merge(hashes_remove=...)`, also on an aborted run). Without this, a source edit back to exactly that older text would hash-match and the mixed target would be treated as done. Confluence sets `hash_drop_casefold` so its legacy mixed-case keys (read back into the pair case-insensitively) go too. The git core (GitHub, GitLab, Bitbucket) does the same for a file committed with a held key. Jira (fields/comments), Jira attachments and Airtable write each pair all-or-nothing, so a held pair there writes nothing and its stored hash stays true. A failed write keeps the stored hash (nothing changed), a fully written pair is unaffected (no extra MT), and a stored hash EQUAL to the current source hash is kept: it was recorded by a complete write of that same source, so the held fields still match it and dropping it would re-translate the pair on every run while the unit stays held (an explicit retry of an unchanged pair).
- **A Contentful pair with a CMA-validation hold is never marked done (#665, 2026-10-10).** Both a pair written with a field held by a validation (the valid fields land, the offending one keeps its stored value, listed as `incomplete`) and a pair where every field fails validation (nothing written) record NO content hash. Because the failure is deterministic for an unchanged source, the pair is parked instead of re-translated on every scheduled run: `contentful_blocked` (reason, shown on the connection page and CLI) plus `contentful_blocked_src` (source hash), checked by the flow spec hook `parked`. The run reports it as NOT DONE (`parked` in the result); a source edit or Retry re-attempts it and a complete write clears the park. Review (NEEDS_REVIEW) and Rich Text placeholder holds are not parked and retry every run.
- **Draft mode never writes into a live translation (Freshdesk, Intercom, Zendesk Guide, 2026-10-10).** With Publish after write off, a (record, locale) pair whose stored translation is already PUBLISHED is neither overwritten (that would put unreviewed machine text on the customer's site) nor re-stamped as a draft (that would unpublish it, #465). The flow spec hook `parked` probes the stored translation before any MT for pairs that would otherwise be translated and skips a live one; the write path refuses it again (an explicit retry bypasses the hook). The pair records no content hash and is listed in the run message and in `live_kept` in the run result. Nothing is stored: once the translation is unpublished in the vendor system, or Publish after write is turned on, the next run writes it. Zendesk Guide also re-sends a held field with its stored value on update (a partly held draft stays a draft), so the write is safe whether its translation PUT merges or replaces.
- **Contentful Rich Text is all-or-nothing per field (#648).** `app/filters/contentful_rich_text.py` turns each text block into one segment with paired and point placeholders and rebuilds the field from the SOURCE tree; embedded entries and assets, unknown node types and every `data` payload are copied unchanged. If any block is held for review or its placeholders do not round-trip, the field is left out of the write (the stored value stays) and the pair follows the #640 rules: no new locale, no content hash, retried next run.
- **A Microsoft 365 Files copy is all-or-nothing per file (msgraph).** A rebuilt file replaces the whole copy, so a file with ANY unit held for review (NEEDS_REVIEW) is neither created nor updated: the existing copy keeps every stored translation, no content hash is recorded, and the pair is retried after approval (`flow.variant_exists` pinned False in `app/services/connectors/msgraph.py`).
- **A Microsoft 365 Files copy edited by hand is left alone.** After each write the copy's content identity (a content hash when Graph returns one, else the cTag) is stored in `msgraph_target_tags`; before re-translating a changed source the copy is read back and, if it no longer matches, the pair is parked with no download and no MT spend, and the write itself carries `If-Match`. SharePoint can change a file after upload (document property promotion), so the check has a per-connection switch (`protect_edited_copies=0`) and a live-QA check.
- **Microsoft 365 Files file bytes go only to the drive's own tenant (msgraph gate).** A download redirect or upload-session URL is used only when its host is exactly `<tenant>.sharepoint.com` or `<tenant>-my.sharepoint.com`, the tenant label read from the drive's own `webUrl` (`msgraph_graph.file_hosts`); another tenant's SharePoint, consumer OneDrive and national-cloud hosts are refused and no bearer token is ever sent to a file host. A run walks at most 500 folders (`MAX_FOLDERS`) and lists at most 5000 files, else it is refused before any download or MT; Max files is capped at 500 (`MAX_FILES_CEILING`) and writes by the shared WriteBudget.
- **Microsoft 365 Files has no PDF round trip.** The connector uses only our existing file filters (DOCX, XLSX, PPTX and their macro and template siblings such as .docm, .xlsm, .pptm, .potx, plus HTML, Markdown, text, JSON, PO, SRT); .pdf, legacy binary Office (.doc, .xls, .ppt) and images are listed as skipped with a reason on every run. A macro file is rebuilt with its macros and content type unchanged. A source file renamed after its first translation keeps its old copy name (the copy is found by drive item id).
- **The write-back strategies are single-process safe, not cross-process conditional.** All
  three topologies ship and are consumed by real connectors (field-map / T1, sub-resource / T2,
  doc-per-locale / T3), each serializing its read-modify-write span with an in-process striped
  lock; a concurrent human edit made from another process between a T1 record GET and its
  whole-map PUT can still be overwritten, because cross-process conditional writes
  (ETag / If-Match) remain a deferred tier, stated in the
  `app/services/connectors/strategy.py` write-back comments and tracked as a deferred tier.
- **Anonymous connector-health is a coarse liveness ping.** `GET /api/connectors/health`
  returns only `{ok, version}` to a keyless caller with the version coarsened to MAJOR.MINOR
  (an exact patch string is a free version fingerprint) and touches no DB; the enabled
  connector-kind enumeration is served only to a docs-scoped key, in
  `app/web/api_part2.py::api_health`.
- **The write ceiling stops a run, it does not resume it.** A batch that hits
  `app/config.py::MAX_WRITES_PER_RUN` leaves the remainder unwritten and reports it rather than
  auto-continuing, an intentional cost guard stated in `app/services/connectors/store.py`.
- **Envelopes fail safe to on.** A settings read error leaves envelope recording enabled
  because the history must not silently stop, the opposite posture from the fail-off Prompt Tap
  capture, stated in the `app/services/tap_envelope.py` module docstring.

### Guarded envelope controls

Envelope metadata recording defaults ON; raw payload capture is separately opt-in.
Raw capture OFF stops NEW raw payload capture. Envelopes and orphan status maintenance
continue unless envelope recording is separately disabled. Existing raw files are retained;
their age-out is FROZEN while capture is off. The 30-day default is not a guaranteed
maximum, and running/paused envelopes' files are never age-swept. The same sweep also
enforces the byte budget (`prompt_tap_actions_max_bytes`, default 512 MB): over it, the oldest
settled per-action files (not running/paused, not written in the last 10 minutes, never a
symlink; a budget under 1 MB is ignored as a misconfiguration) are deleted down to 80 percent
of the budget. The Ledger shows the retained raw-file count and oldest last-write date (UTC)
and, to an unscoped caller, the raw size and the last prune.

The Ledger settings area provides audited admin ceremonies. The WEB ceremony uses the same
posture as the other destructive web admin controls (retention, segmentation): an admin
session plus CSRF plus the typed phrase, and it does NOT depend on `remote_admin_enabled`
(so it is reachable on a default install; the card renders only when the caller may drive
it; the super-admin requirement this used to carry under active multi-org went away with that account type on 2026-09-20, and the install-wide ceremony is refused on the web under an active gate in favour of the host CLI twin). The /api twin additionally requires
the full R3 three-lock preamble (admin scope, admin role, `remote_admin_enabled`). Host CLI
uses the trusted local operator gate. The purge cutoff must be a PAST UTC date (a
today/future date is refused). Generic settings writes refuse the envelope key on web, API,
MCP and CLI. While envelope recording is OFF every Ledger page displays a persistent banner
and /health detail reports `ledger_envelopes: off` (cached state).

| Command | Behavior |
|---|---|
| `ledger status` | Read envelope state and retained raw-file counts/date. Host-only. |
| `ledger envelopes off --confirm "disable envelope recording"` | Exact typed phrase; optional --reason; audited disable. |
| `ledger envelopes on` | One-step audited enable. |
| `ledger purge --before 2020-01-01 --confirm "purge envelopes before 2020-01-01"` | Dry-run counts; date must match exactly. |
| `ledger purge --before 2020-01-01 --confirm "purge envelopes before 2020-01-01" --apply` | Delete terminal envelopes finished strictly before UTC cutoff and their per-action raw files, transaction per batch. |

Prefix commands with `loc-tms`. The web twin uses the same typed phrases and cutoff date,
with Apply unchecked by default, and answers the browser form with a redirect and flashed
counts (the /api twin returns JSON). Running/paused envelopes and parents with protected
child runs are retained. Envelopes have no clear button. Legacy clear semantics are
unchanged. MCP `ledger_summary` exposes read-only `ledger_status` on its action-list
response; no disable or purge tool exists (intentional destructive LLM-action exception).
Export and backup retention obligations are outside this purge's scope.

**Purge staging and the hard-kill window.** An applied purge stages each per-action raw
file by renaming it to `<stem>.purge-<hex>-<epoch>` inside the batch transaction, then unlinks the
staged file after the DB commit (or restores it on a transaction failure). A database
transaction cannot make a filesystem rename power-loss atomic, so a process KILLED between
the rename and the commit can leave a `<stem>.purge-<hex>-<epoch>` file on disk.
`ledger_controls.reconcile_staged_purges()` settles that residue and runs both at boot and
on the scheduler housekeeping pass (inside the TTL sweep, before its capture-off early
return, so it runs even while raw capture is OFF). It uses the `TapAction` row as the source
of truth: the row is GONE means the purge committed, so the staged file is deleted; the row
EXISTS means the purge did not commit, so the file is restored to its `<stem>.jsonl` name.
`status()` counts leftover staged files as `staged_purge_files` so the operator can see the
debt until the next pass clears it.

Reconciliation must never race a purge that is still running (otherwise it would rename a
staged file back while the purge is about to commit its deletion, resurrecting a raw payload
the ceremony already reported and audited as gone). Three guards close that window: a purge
holds one process staging lock across its whole rename/commit/cleanup window and
reconciliation takes the same lock non-blocking (so it can never observe an in-flight purge in
this process); reconciliation leaves alone any staged file younger than a grace window to cover
a live purge in another process that an in-process lock cannot see; and the reconcile directory
listing is throttled to at most once per hour on the scheduler tick (boot forces a scan, and a
tick turned away by the staging lock does not consume the throttle). The grace window's age is
read from the STAGING epoch carried in the staged file name, not the file's mtime: a rename
preserves the raw-capture mtime, which is already days old for any purgeable action (a terminal
action finished before a past-UTC cutoff), so an mtime grace could never protect a real staged
file. The window is bounded on both sides: a staging epoch (or a legacy or malformed name's
mtime) more than a small skew ahead of now is a clock artifact, not a live purge, so it is
clamped to old residue and settled rather than deferred forever, with a WARNING naming the file.
The window is the setting `ledger_purge_reconcile_grace_seconds` (default 600 seconds, bounded
0..86400 by a write-door validator); because it governs a data-deletion safety guard it is
host-only (REMOTE_IMMUTABLE), settable only with `config set` on the host, never over `/api` and
not on the web Settings page. The purge's post-commit cleanup is missing-file tolerant and never raises after the
commit; its audit row reports the raw-file count STAGED for removal inside the ceremony
transaction (equal to the removed set under the staging lock, since the row must commit before
any file is physically removed), and the physically reclaimed count is returned to the caller.
