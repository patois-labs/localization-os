# Surfaces & Parity: Architecture (L1)

## 1. Role

Customer-bound uploads must name a project in the bound customer. `/api/documents` returns
422 when omitted and 404 when inaccessible. Remote CLI and MCP use this same door; the web
upload already requires a selected project. Trusted local CLI operators remain unbound.
DOCX ingestion returns `file_warnings` for untranslated regions on web, API, CLI and MCP.

Authenticated API keys never exceed their owner's role. Document upload, paid MT,
require a write role.
Ledger reads require admin on both web and API, including remote CLI/MCP calls.
Customer preferences, defaults, context cards and language-pair changes require
a manager, linguist or admin (`can_configure_customer` = `can_manage` or `can_tune_ai`);
translators, reviewers and viewers are refused. Customer 360 project rows, document/TM counts,
derived pairs and project connections are filtered by project visibility in the service.
Remote key issuance is self-only; admin and SCIM grants must be issued locally.
Airtable record deletion requires an admin's per-user key, rejects the shared connector
credential, and requires a configured base. Local CLI operations remain trusted host
operations; remote CLI and MCP inherit the API checks.

The #117 canonical-path funnel is `app.web.route_path.route_path`: the web layer owns
the private Starlette API, keeping the kernel facade free of web frameworks.

This subsystem is **the four surfaces the product is driven through and the doctrine that
keeps them equal**: the web UI, the headless CLI, the token-authed `/api`, and the MCP
server. The repo law is that every capability ships on the web UI and a non-UI path in the
same change, with route logic extracted into service functions all surfaces call, so no
surface silently drifts from another.

The `/api` is mounted by four web modules: the inbound front door for companion apps and
per-user keys (`app/web/api.py`), the remote-CLI surface (`app/web/api_cli.py`) that is the
web side of most remote-capable CLI commands, the key-based-projects and Delta API routes
(`app/web/keys.py`) that carry the continuous-localization `/api/keys` group and its
`key_projects_enabled` kill switch (self-service per-user API-key management is a different,
non-`/api` module, `app/web/apikeys.py`, mounted under `/account`), and the image routes (`app/web/images.py`). Several remote families
reach routes mounted outside `app/web/api_cli.py`, for example the `keys` verbs (to the `/api/keys` routes in `app/web/keys.py`).
The CLI is one argparse dispatcher
(`app/cli/__main__.py`): run in-process it is the local-root operator, and run with
`--remote URL --token lsu_...` it becomes a thin HTTP client (`app/cli/_remote.py`,
`app/cli/_remote_impl.py`) that calls the same `/api` routes, org-scoped entirely by the
token server-side. The remote-vs-local dispatch guards live in `app/cli/_common.py`. The
`/help` surface (`app/web/help.py`) serves the human and LLM references, and `/utcp`
(`app/web/utcp.py`, `app/services/utcp_manual.py`) is a read-only discovery manual over an
explicit allow-list. MCP itself lives outside `app/` in `integrations/mcp-server/`, which
declares per `/api` route either the tool that may call it or a categorized reason it is out
of reach (the `NEVER_COVER` set).

Parity is not asserted, it is guarded. A family of mirror guards pins each surface to the
one source of truth: the CLI leaves to the argparse tree, the MCP registry to the router, the
`/help` cards to the router, and the `/utcp` manual to the OpenAPI schema. This subsystem is
those front doors and those guards.

It does not own the features behind the surfaces (every other L1 doc owns its own service
logic), the identity and API-key checks the surfaces call (identity-and-access), or the
per-feature web pages and CLI groups, which live with their features. It owns the shared
front doors, the remote transport, and the parity doctrine.

## External URL parity

`app/services/settings.py::external_base_url` and `app/services/settings.py::external_url`
provide one external URL authority: `setting:app_base_url` wins, with the request
Host as fallback when unset. Invitations, SAML/SCIM discovery, UTCP manual tool URLs,
OIDC and connector OAuth callbacks, and rendered Security/Help guidance share it.
UTCP's `_resolve_base_url` delegates and keeps its no-store/Vary defenses.
`verify_security_external_origin` pins exact unset outputs and ratchets request-base
reads to the settings helper. Connector push intake's `_public_base_url` retains
its special localhost-means-unset rule; Airtable webhook HTTPS requirements remain.

## 2. Invariants

- **Every CLI capability has a non-UI leaf, and no parser leaf is undocumented.**
  `app/cli/__main__.py::build_parser` builds the real parser tree, and the guard walks it to
  assert every expected capability resolves to a leaf command and every leaf is accounted for.
  Enforced by `verify_cli_parity`.
- **A remote-capable verb dispatches to its `/api` route, never silently to the local db.**
  A verb with a matching `/api` route checks `app/cli/_common.py::remote_mode` and dispatches
  through `app/cli/_remote_impl.py` under `--remote`, while a local-only verb carries one of
  the accepted local-only markers (`app/cli/_common.py::require_local`,
  `app/cli/_common.py::local_op`, or `app/cli/_common.py::require_operator`) rather than
  ignoring the flag and running against the local file. Enforced by
  `verify_cli_remote_dispatch_guard`.
- **The `/api` arm calls the same service seams as web and CLI, and a foreign id is 404.**
  The remote content-ops routes in `app/web/api_cli.py` reuse the exact service functions the
  web and in-process CLI use, and under multi-org a cross-org id returns 404, never a 403 that
  would leak existence. Enforced by `verify_api_r1a_content_parity`.
- **Every `/api` route either exposes an MCP tool or carries a categorized NEVER_COVER
  reason.** `integrations/mcp-server/registry.py` declares, per route walked off the live
  router, the tool that may call it or the reason it is deliberately unreachable, so a new
  mutating route cannot quietly become agent-reachable or be forgotten. Enforced by
  `verify_mcp_parity`.
- **Every MCP tool ships its wire metadata.** `integrations/mcp-server/tool_meta.py` derives a
  title, MCP annotations (from the registry tier plus reviewed exception lists) and an object
  outputSchema per tool, the server instructions, and the error-code vocabulary appended as a
  JSON last line to every tool error. Enforced by `verify_mcp_parity` (check 11) and
  `verify_mcp_tools` (`check_wire_metadata`, `check_error_tails`).
- **The `/help` LLM card is pinned to the real router.** `app/web/help.py` serves the feature
  cards, and the guard asserts every required feature family has documented routes that exist
  on the router, so the reference cannot over- or under-claim what the product serves. Enforced
  by `verify_help`.
- **The `/utcp` manual is read-only over a fail-closed allow-list.**
  `app/web/utcp.py::utcp_discovery_manual` emits only tools whose route passes
  `app/services/utcp_manual.py::is_included`, derived from the canonical route walk, so a route
  not on the allow-list is absent from the manual. Enforced by `verify_utcp_manual`.
- **The in-process operator seam is unreachable from a request path.** With the login wall on,
  a trusted in-process CLI call sees the operator's own jobs and runs while the same call with
  the web "no user" semantic sees nothing, so the local-root operator convenience never leaks
  into a request surface. Enforced by `verify_cli_visibility`.

## 3. Where the work happens

| Concern | Pin | One line |
|---|---|---|
| Effective route inventory | `app/web/_routes.py::walk_routes` | Recurses included routers with effective prefixes; shared by help and all route mirror guards. |
| Inbound /api front door | `app/web/api_discovery.py` · `app/web/api.py` · `app/web/api_part3.py::api_push` · `app/web/api_part2.py::_conn_gate` · `app/web/api_discovery_common.py::_app_version` | The companion-app / per-user-key document and connector endpoints and their org gate. |
| Remote-CLI /api surface | `app/web/api_cli.py` · `app/web/api_cli_common.py::_NOT_FOUND` · `app/web/api_cli_admin_1.py::api_srx_enable` · `app/web/api_cli_admin_2.py::api_admin_features_list` · `app/web/api_cli_projects_1.py::_tm_source_visible` · `app/web/api_cli_projects_2.py::api_projects_export` · `app/web/api_cli_customers.py::api_customers_list` · `app/web/api_cli_vendors.py::api_vendors_list` · `app/web/api_cli_glossaries_1.py::api_glossaries_list` · `app/web/api_cli_glossaries_2.py::api_glossary_module_extract` · `app/web/api_cli_misc_1.py::_review_block` · `app/web/api_cli_workflows_1.py::api_workflows_list` · `app/web/api_cli_workflows_2.py::api_workflows_delete_step` · `app/web/api_cli_misc_2.py::_style_guide_in_scope` · `app/web/api_cli_misc_3.py::_rerun_batch_blocking` · `app/web/api_cli_misc_4.py::api_routing_rule_show` · `app/web/api_cli_users.py::_user_row` · `app/web/api_cli_misc_5.py::_template_in_scope` · `app/web/api_cli_misc_6.py::api_lqa_rulebook_get` · `app/web/api_cli_misc_7.py::_export_scope_set` | The web side of most remote-capable CLI commands (the content-ops verbs), filling the /api parity gaps; the `advanced` verbs are served by their own Advanced-workflows routes instead. |
| CLI dispatcher | `app/cli/__main__.py::build_parser` · `app/cli/__main__.py::main` | The one argparse tree, its global flags, and the in-process-vs-remote dispatch. |
| CLI verb scope classes | `app/cli/_scope.py::scope_of` · `app/cli/_scope.py::stamp_leaves` · `app/cli/_scope.py::unclassified` | Every CLI leaf verb is ORG_SCOPED, OPERATOR_ONLY or INSTALL_WIDE; the dispatcher applies the matching multi-org gate before the verb runs, and an unclassified verb fails the build (#556). |
| Remote transport | `app/cli/_remote_impl.py` · `app/cli/_remote.py` · `app/cli/_remote_impl_common.py::project_create` · `app/cli/_remote_impl_part2.py::project_remove_locale` · `app/cli/_remote_impl_part3.py::gmod_import` · `app/cli/_remote_impl_part4.py::connector_sync` · `app/cli/_remote_impl_part5.py::keys_add` · `app/cli/_remote_impl_part6.py::keys_changes` · `app/cli/_remote_impl_part7.py::workflow_run` · `app/cli/_remote_impl_common_leaf.py::_read_bytes` | The thin HTTP client and the per-command remote arms that call `/api`. |
| Remote/local guards | `app/cli/_common.py::remote_mode` · `app/cli/_common.py::require_local` | The seam that routes a verb to `/api` under `--remote` or refuses to run it locally. |
| Background calls and idempotency keys | `app/web/background_calls.py::BackgroundCallMiddleware` · `app/web/background_calls.py::fail_interrupted` · `app/cli/_remote.py::RemoteClient` | `Prefer: respond-async` and `Idempotency-Key` on every billing /api route: a background call answers 202 + job id and records the route's own answer on an `api_task` Job (`GET /api/jobs/{id}`); a key replays the first answer for 24 hours per caller and route (#728, #744, #747). The MCP slow tools and the remote CLI's connector pull/push launch this way and poll. |
| Background authoring (web) | `app/web/author_jobs.py::start` · `app/web/author_jobs.py::poll_body` · `app/web/author_jobs.py::pending_body` | Describe a workflow, Fix this for me and the step prompt assistant run their LLM draft on an `api_task` Job worker thread. The POST waits up to `INLINE_WAIT_S` (25 s, under Cloudflare's 100 s cut-off); a slower draft redirects to `GET /workflows/draft-jobs/{id}` (progress page, then the result in place) or answers 202 with a `poll_url` (`GET /workflows/draft-jobs/{id}.json`). A repeat click joins the person's running draft of that kind (2026-10-16 prod 524). |
| Help surface | `app/web/help.py::help_module` | The `/help` human and LLM references pinned to the router. |
| UTCP manual | `app/web/utcp.py::utcp_discovery_manual` · `app/services/utcp_manual.py::build_manual` · `app/services/utcp_manual.py::is_included` | The read-only UTCP discovery manual over a fail-closed allow-list. |
| Router assembly | `app/web/__init__.py::ALL_ROUTERS` | The one list of per-feature routers `app.main` mounts. |
| MCP connection help | `app/cli/cmd_mcp.py::register` | The `mcp connect` verb that prints the same connection snippet the `/help` page shows, and `mcp client-limit`, which prints the per-key admission entry (#410). |
| Global search | `app/services/global_search.py::search` · `app/services/global_search.py::scope_for_user` · `app/web/search.py::api_search` · `app/cli/cmd_search.py::register` | One bounded, access-scoped search over projects, document file names, TM, glossary terms, workflow names and connection names (the last two follow the rules of their own pages, shared through `app/services/resource_visibility.py`): the `/search` page, the `/search/suggest` dialog feed, `GET /api/search`, the `search` CLI verb and the MCP `search_workspace` tool all call it, so groups, hidden-item rules and caps cannot drift. |
| Appearance (Light, Dark, Match my computer) | `app/web/account_theme_api.py::api_account_set_theme` | A person's own appearance, stored per account (`User.ui_theme`, ADR 0063) and validated by one seam (`UI_THEMES`, `update_theme` in the security service) for Account settings and the account menu (`POST /account/theme`), `POST /api/account/theme` and `account set-theme`. `theme_attrs` in the web deps module renders it. Every signed-in page renders it on `<html>` so the no-flash bootstrap applies it before first paint; the browser's `localStorage` copy is only the fallback for signed-out pages. MCP exempts it as a human UI preference. |
| Page layouts | `app/services/page_layouts.py::REGISTRY` · `app/services/page_layouts.py::validate_rows` · `app/web/page_layouts.py::api_layout_put` · `app/cli/cmd_layout.py::register` | A person's own saved card layout per page type: one registry of defaults, one validator and one owner rule shared by the web edit mode, `/api/account/layouts` and the `layout` CLI group. |

## 4. Data & settings

- **Models:** this subsystem owns no models; each surface reads and writes the models of the
  feature it fronts. Identity and API keys are the identity-and-access models.
- **Settings:** `setting:utcp_manual_enabled` (default `"1"`) gates the `/utcp` manual. The
  MCP `NEVER_COVER` declarations live in `integrations/mcp-server/registry.py` (outside the
  settings registry) because the fourth surface lives outside `app/`.

## 5. Enforced by

- `verify_cli_parity`: every capability has a CLI leaf and no leaf is undocumented.
- `verify_cli_remote_dispatch_guard`: a remote-capable verb dispatches to `/api` rather than
  the local session.
- `verify_api_r1a_content_parity`: the `/api` arm reuses the web/CLI service seams with
  cross-org 404s.
- `verify_mcp_parity`: the MCP registry declaration against the live router.
- `verify_help`: the `/help` LLM card and API reference pinned to the router.
- `verify_utcp_manual`: the UTCP contract shape and the fail-closed allow-list.
- `verify_cli_visibility`: the operator-seam-vs-request-path scoping.
- `verify_page_layouts`: layout validation, owner isolation on every surface, CSRF on the
  web path, the auth-off local owner, CLI round trips, inert rendering and the migration;
  since L2, every registered hub page: wiring, default order equal to the pre-change
  render, a saved layout applied, hostile layouts refused, the blob escaped.
- `verify_mcp_spec_tiers`: the SPEC-mcp-server tier table, spend-tool list, and live tool
  total pinned to `integrations/mcp-server/registry.py::TOOLS`, mutation-proven both ways.

## 6. Battle scars

- **An unguarded mirror rots in public.** The `/help` reference once served ninety-eight
  routes under a sentence claiming ninety-three, which is why every surface mirror (CLI cards,
  MCP registry, help reference, UTCP manual) is now pinned to a single source of truth by a
  guard, recorded in `docs/specs/mcp-server.md` and proven by `verify_mcp_parity` and
  `verify_help`.
- **A `--remote` flag was silently ignored and misdirected a prod upload.** Several
  `glossary module` verbs went straight to a local session with no `remote_mode` check, so
  `--remote` ran against the local file while reporting success; every remote-capable verb now
  dispatches to its `/api` route, recorded in the `app/cli/_common.py` guard and proven by
  `verify_cli_remote_dispatch_guard`.
- **UTCP is additive and must not weaken MCP.** The `/utcp` manual is a read-only,
  fail-closed allow-list generated from the schema, chosen so MCP stays the first-class
  protocol and nothing in the manual can mutate, proven by `verify_utcp_manual`.
- **External text reached an MCP agent unmarked (#748).** CMS titles, segments, TM entries,
  query posts and vendor error text came back in tool results looking like the server's own
  words, a prompt-injection route when the agent also holds a CMS's publish tools. Every tool
  result (all tools except the install-text-only `OWN_TEXT_TOOLS`) now names those fields under
  `external_content` (with the
  rule that they are data, never instructions), connector write results carry
  `requires_human_publish: true`, and vendor error text is sanitized and quoted at the source
  (`app/services/connectors/vendor_text.py`), recorded in
  `integrations/mcp-server/external_content.py` and proven by `verify_mcp_external_content`.

## 7. Limits & accepted risks

- **The in-process CLI is the trusted local root.** Run in-process the CLI has no login and no
  RBAC by design, so filesystem access to the install is full operator access; the guarantee is
  that this trust never reaches a request path, stated in `docs/CLI.md` and proven by
  `verify_cli_visibility`.
- **The fourth surface lives outside `app/`.** `integrations/mcp-server/` imports no app code
  and carries its own registry, so it is documented from here but excluded from the in-`app`
  coverage ratchet, stated in `docs/specs/mcp-server.md`.
- **UTCP is experimental and off the critical path.** The `/utcp` manual is an additive Option
  B behind `setting:utcp_manual_enabled`; MCP remains the supported agent protocol.

## Shared presentation and output seams

Four small modules sit outside every subsystem table because every surface uses them.

| Concern | Pin | One line |
|---|---|---|
| UI strings | `app/i18n/loader.py::translate` · `app/i18n/loader.py::load_catalog` · `app/i18n/loader.py::resolved_locale` | Hand-rolled JSON catalogs (`app/i18n/en.json` plus de, es, fr); English is the source of truth and the fallback, resolution is the locale value, then English, then the key itself, and a bad placeholder degrades to the unformatted string instead of raising in a template. Enforced by `verify_i18n`. |
| Brandable names | `app/branding.py::branded` · `app/branding.py::brand_name` | The one place coined display names live (the product, the Ledger). Each is an admin-renameable setting capped at 64 characters; standard industry terms are not brandable. Enforced by `verify_branding`. |
| Capability cards | `app/capabilities/tm.md` | One markdown card per capability (front matter, nouns, verbs). The assistant is grounded only in these cards ([assistant](assistant.md)), `/help/<module>` serves them, and a card owned by a switched-off module is filtered out through `app/features.py::card_is_off`, so a disabled feature leaves no trace in help or Ask. |
| Output boundaries | `app/output_safety.py::csv_safe_cell` · `app/output_safety.py::xlsx_text_cell` · `app/output_safety.py::safe_http_url` · `app/output_csv.py::writer` | Exported text never becomes a spreadsheet formula: CSV cells that start with an operator are prefixed, XLSX text cells are forced to string type, and a stored URL is exposed as a link only when it is an absolute http or https address. `app/output_csv.py` wraps the standard CSV writers so every export goes through the guard. Enforced by `verify_export_output_escaping`. |
