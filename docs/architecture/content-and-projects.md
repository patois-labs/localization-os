# Content model, projects, and languages: Architecture (L1)

## 1. Role

This subsystem is the **content model the rest of the product hangs off**: customers and projects,
the content-type taxonomy that scopes a project's language assets, the language catalog and reusable
language sets, the project status lifecycle, and the ability to attach an already-established TM or
glossary to a project.

The shared operations (`app/services/content.py`) hold the business logic that used to live inside
the route handlers, so the web routes and the CLI call one code path for customer/project creation,
applying a language set, archiving and deleting. A content type (`app/services/content_types.py`) is
a small org/customer-scoped taxonomy object a project binds to at creation, which drives the domain
scoping of its glossaries and TM. Language sets (`app/services/languages.py`) are reusable named
target-locale profiles; the language catalog (`app/services/languages_catalog.py`) is the curated
BCP-47 universe shipped as a static resource plus a single enabled-subset setting that filters what
the pickers offer without hard-gating what is valid.

The lifecycle service (`app/services/lifecycle.py`) is the single authority for moving a project
through its status states: a validated manual/automatic transition, an idempotent auto-complete
that fires only when every locale of every document is fully translated, and a reopen when a
completed project gains new work. Project sources (`app/services/project_sources.py`) attach an
existing TM (a reference) or pin an existing glossary to a project, the one seam behind both the
project page and the CLI verbs.

It does not own the translation of that content (translation-pipeline), the TM and glossary entities
themselves (tm-and-glossary), the org/tenancy ring it scopes within (tenancy-confinement), or
promptable setup and templates (project-setup-and-templates).

## 2. Invariants

- **Customer/project operations are one service both surfaces call, no route-local logic.**
  `app/services/content_project_rename_delete_sp.py::customer_create` / `app/services/content_projects.py::project_create` are called
  by the web routes and the CLI alike, with the transaction boundary owned in the helper. Enforced
  by `verify_customers` and `verify_multiorg`.
- **Custom project fields are typed, scoped and safe to render.**
  `app/services/custom_fields.py` is the one service the `/settings/custom-fields` editor, the project
  forms, the `field` CLI group, the `/api/custom-fields` + `/api/projects/{pid}/fields` routes and the
  `manage_custom_fields` / `set_project_fields` MCP tools call. `CustomFieldDef` (org + optional customer
  scope, key unique per org case-folded by a functional index) and `ProjectFieldValue` (typed columns so
  filters and sorts are plain indexed comparisons on SQLite and Postgres). Only `https://` URLs are
  stored or rendered as links (`custom_fields.safe_url`, re-applied at render), values are autoescaped,
  a definition with values is archived never deleted, and a write is all or nothing.
  `custom_fields.set_values` is the seam phase 3 (an auto-created Jira issue) writes the key through.
  **Who may define them:** admins any field; managers only a customer-scoped one and
  only change or archive fields they created (`custom_fields.define_refusal` / `modify_refusal`,
  the notification-channel creator-or-admin rule); org-wide fields are admin only; below manager
  never. Same on web, `/api`, CLI over `--remote` and MCP (all route through the same checks); audit rows unchanged.
  Enforced by `verify_custom_fields`.
- **A content type is org/customer scoped and a foreign one 404s.**
  `app/services/content_types.py::visible` is the one scope filter every surface fetches through, so
  no hand-rolled filter can leak a foreign type. Enforced by `verify_content_types`.
- **The catalog is a static resource; the enabled subset is a soft offering filter.**
  `app/services/languages_catalog.py::is_valid` accepts any catalog code while
  `app/services/languages_catalog.py::enabled_locales` only filters what the pickers offer, so the
  enabled list never hard-gates a valid locale. Enforced by `verify_languages_catalog`.
- **Text carries its own language, and a bad code never breaks a page.**
  `app/services/languages_catalog.py::html_lang` and `app/services/languages_catalog.py::text_direction`
  turn any stored code (legacy `es_ES` included) into a `lang` / `dir` value, or "" for junk, and
  never raise; `app/web/deps.py::langattrs` puts them on the element holding only source or target
  text (a11y, WCAG 3.1.2). Enforced by `verify_lang_of_parts` and `verify_a11y_static` (C11).
- **A connected project's language is stopped, never deleted.** For a project with a content source
  (`app/services/content_part2.py::is_bound_project`) removing a target language marks its row
  `stopped_at` instead of deleting segments (`app/services/content_projects.py::remove_target_locale`,
  preview mode `stop` in `app/services/content_part2.py::remove_locale_preview`): the permanence rule
  does not apply, only the last ACTIVE language is protected, no typed confirmation is needed, and adding
  the language again (or a language set that contains it) brings the same row and its segments back.
  New documents, default batches and completion skip a stopped language; exports and TM downloads keep
  covering it (for key projects too: `app/services/keys_common.py::readable_locales` lets the key file
  export and the delta feed read a stopped language, while `active_locales` keeps new work off it).
  Every other project keeps the delete-with-typed-confirmation removal. Enforced by
  `verify_connected_languages`.
- **Auto-complete fires only when every locale of every document is done.**
  `app/services/lifecycle.py::mark_completed_if_done` never closes a partially-translated
  multi-locale project. Enforced by `verify_lifecycle`.
- **A document's status is derived from its segments, not stored.**
  `app/services/lifecycle.py::document_status_map` computes new, in_translation, in_review or done
  from one grouped segment query per chunk of ids across all languages, and a project with no
  segments reads "No content yet", not in translation (#563). Enforced by `verify_doc_scope_status`.
- **A manual project status move follows a fixed graph, and only the app's own writes skip it.**
  `app/services/lifecycle.py::transition_error` lets the four working states move freely among
  themselves and to completed or cancelled, a completed project reopen, close or cancel, and closed
  and cancelled stay terminal except Reopen; the web form, `/api` and the CLI all refuse a bad move
  with the allowed next states (#580). `app/services/lifecycle.py::set_status` itself stays unguarded
  on purpose, so auto-complete, the living-project reopen and the auto-close clock are never refused.
  Enforced by `verify_api_input_580`.
- **A short project list says what its default filters hid.** The `/api` project list adds a
  `hidden` count of in-scope archived and finished projects the default filters left out, so a short
  list is never mistaken for a narrower scope than the detail route allows; list and detail share one
  scope authority (#607). Enforced by `verify_api_list_detail_scope_607`.
- **An uploaded file name always passes the rename validator.**
  `app/services/content_part2.py::safe_document_filename` keeps the last path component, neutralizes
  the characters `app/services/content_part2.py::validate_document_filename` refuses, trims trailing
  dots, spaces and all Unicode whitespace to a fixed point, and prefixes a Windows device name or a
  leading dot, so whatever an upload stores is accepted unchanged when the document is later renamed
  (#614). Enforced by `verify_upload_filename_rules_614`.
- **Attaching a TM is a reference; pinning a glossary is a pin.**
  `app/services/project_sources.py::add_tm_source` records a `ProjectTmSource` reference and
  `app/services/project_sources.py::add_glossary_pin` pins an existing glossary, the one seam behind
  both surfaces. Enforced by `verify_project_sources`.

## 3. Where the work happens

| Concern | Pin | One line |
|---|---|---|
| Customer/project ops | `app/services/content.py` · `app/services/content_project_rename_delete_sp.py::customer_create` · `app/services/content_projects.py::project_create` · `app/services/content_project_rename_delete_sp.py::assign_project_customer` · `app/services/content_part2.py::set_project_due` | The shared create/assign/archive business logic both surfaces call. |
| AI opt-out | `app/services/ai_optout.py::guard` · `app/services/ai_optout.py::set_project` · `app/services/ai_optout.py::AIScopeMiddleware` · `app/cli/cmd_project_ai.py::_project_set_ai` | The project/customer "no AI" switch, the engine-boundary guard every provider and embedder call passes, the request/run/job scope, and the `set-ai` CLI verbs. |
| Translate comments | `app/services/comment_option.py::enabled_for` · `app/services/comment_option.py::set_for` · `app/services/comment_option.py::stamp_new_project` · `app/cli/cmd_project_ai.py::_project_set_comments` | #690: the per-project option that offers comments and annotations in Office files for translation (Setting row `translate_comments:<id>`; new projects start off, projects that predate it keep Word comments on), read by ingest and passed to the Office filters as `comments=`. |
| Custom fields | `app/services/custom_fields.py::create_def` · `app/services/custom_fields.py::set_values` · `app/services/custom_fields.py::safe_url` · `app/services/custom_fields.py::project_ids_matching` · `app/web/custom_fields.py::custom_fields_index` · `app/cli/cmd_field.py::register` | Typed per-project fields (Jira ticket link, PO number...), their values, list columns, filters and sorts. |
| Content types | `app/services/content_types.py::create` · `app/services/content_types.py::visible` · `app/services/content_types.py::options_for_project` | The org/customer-scoped taxonomy a project binds to. |
| Language sets | `app/services/languages.py::create` · `app/services/languages.py::seed_defaults` | Reusable named target-locale profiles. |
| Language catalog | `app/services/languages_catalog.py::is_valid` · `app/services/languages_catalog.py::enabled_locales` · `app/services/languages_catalog.py::set_enabled` | The static BCP-47 universe and the enabled-subset offering filter. |
| Project lifecycle | `app/services/lifecycle.py::set_status` · `app/services/lifecycle.py::mark_completed_if_done` · `app/services/lifecycle.py::reopen_if_terminal` | The single authority for project status transitions. |
| Attach TM/glossary | `app/services/project_sources.py::add_tm_source` · `app/services/project_sources.py::add_glossary_pin` | The one seam for attaching an established TM or pinning a glossary. |
| Web surface | `app/web/projects.py::projects_index` · `app/web/projects_projects.py::create_project` · `app/web/content_types.py::content_types_index` · `app/web/projects_project_rename_delete_sp.py::author_project_json` · `app/web/projects_glossary.py::project_export` | The projects/customers pages and the `/content-types` surface. |
| CLI surface | `app/cli/cmd_project.py::register` · `app/cli/cmd_content_type.py::register` · `app/cli/cmd_lang.py::register` · `app/cli/cmd_project_project.py::_LIST_CAP` · `app/cli/cmd_project_part2.py::_customer_set_default` · `app/cli/cmd_project_glossary.py::_project_glossaries_remove` · `app/cli/cmd_project_term_extraction_candidat.py::_register_tm` · `app/cli/cmd_project_part5.py::_register_customer` · `app/cli/cmd_project_part6.py::_register_project` | The `project`, `content-type` and `lang` command groups. |

## 4. Data & settings

- **Models:** `app/models.py::Project` (a project, its type, status, source locale and content-type
  binding), `app/models.py::ContentType` (the taxonomy object), and `app/models.py::LanguageSet`
  (a named target-locale profile).
- **Settings:** the enabled-language subset is the `enabled_locales` Setting (a JSON list), managed
  through the language catalog service and `config apply`/`config get`; the catalog itself is the
  static resource `app/resources/languages.json`, edited with no migration.

## 5. Enforced by

- `verify_customers` and `verify_multiorg`: the shared customer/project operations and their org
  scoping.
- `verify_content_types`: the org/customer-scoped taxonomy and its visibility filter.
- `verify_custom_fields`: custom project fields across the service, /api, web, CLI and audit.
- `verify_languages` and `verify_languages_catalog`: language sets, the static catalog, and the
  soft enabled-subset filter.
- `verify_lang_of_parts`: the `lang` / `dir` helpers, the Jinja filters, the rendered pages and
  the `direction` field on the API, MCP and CLI.
- `verify_lifecycle`: the status transitions and the safe auto-complete.
- `verify_project_sources`: attaching a TM reference and pinning a glossary.
- `verify_doc_scope_status`, `verify_api_input_580`, `verify_api_list_detail_scope_607`,
  `verify_upload_filename_rules_614`: the derived document status, the manual status graph, the
  project list disclosure and the upload file-name rule.

## 6. Battle scars

- **Route handlers held business logic that the CLI could not reach.** The customer/project
  operations were extracted into one service so the web and CLI share a single code path rather
  than diverging, recorded in the `app/services/content.py` module docstring.
- **A project could only import a TM/glossary file, never point at an existing one.** Project
  sources added the attach-an-existing seam behind both surfaces so an established memory or
  terminology can be reused, recorded in the `app/services/project_sources.py` module docstring.

## 7. Limits & accepted risks

- **The two project types (document and key-based) are disjoint and immutable.** A project's type
  is fixed at creation, stated in the `app/services/content.py` docstring and enforced upstream at
  ingest; the key-based type is documented in string-keys.
- **Editing the language catalog is an edit to a shipped resource.** The catalog changes with no
  migration because it is a resource file, stated in the `app/services/languages_catalog.py` module
  docstring.
