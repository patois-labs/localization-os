# String keys (key-based projects): Architecture (L1)

## 1. Role

This subsystem is **continuous localization of string keys**: a project type where the unit of work
is a KEY (a string identifier and its per-locale values) rather than a document. It is the surface
a product team's CI syncs against.

The architecture law is that keys are an identity layer on top of the existing Segment spine, never
a parallel pipeline (`app/services/keys.py`). A key's values are real `Segment` rows under
internal, system-managed `Document` rows (one per namespace, format `keys`), so the orchestrator, TM
leverage, glossary matching, LLMemory recall, LQA, the CAT editor, the export tag guard, spend
safety and org/customer isolation all apply to key content with zero pipeline changes. Nothing in
the key service imports or modifies the orchestrator or the translate path: it produces the rows
those modules already consume.

The file layer (`app/services/key_files.py`) is the round trip for the two shipped formats, JSON
(flat and nested) and gettext `.po`, behind the two seams the rest of the code uses. Key parsers
live outside the document Filter registry on purpose: a document filter is positional (skeleton
plus ordered units), but here the key IS the unit of work and the file is a transport, so the
parsers produce key upserts, not skeleton+units.

The web + `/api` surface (`app/web/keys.py`) is the grid, the `/api/keys` group, and the Delta API a
CI consumer polls for "what changed since I last looked". It holds the kill switch on every route
and threads identity into every service guard; the logic lives in the services.

It does not own the general document pipeline (translation-pipeline), the TM/glossary the key
content leverages (tm-and-glossary), or the per-string in-context images (image-localization).

## 2. Invariants

- **Keys are an identity layer over the Segment spine, not a parallel pipeline.**
  `app/services/keys_common.py::is_key_project` gates the type and key values are ordinary `Segment` rows
  under `keys`-format `Document` rows, so no orchestrator or translate code changes for keys.
  Enforced by `verify_keys_core` and `verify_keys_pipeline`.
- **The two shipped formats round-trip through two seams; parsers produce upserts.**
  `app/services/key_files_json_parse_render.py::parse_json_keys` / `app/services/key_files_po_parse_render.py::parse_po_keys` and
  `app/services/key_files_json_parse_render.py::render_json_keys` are the format layer, outside the document Filter
  registry. Enforced by `verify_keys_files`.
- **Every route holds the kill switch and threads identity into the service.**
  `app/web/keys_hub.py::keys_grid` and the `/api/keys` handlers 404 when `setting:key_projects_enabled`
  is off and pass `org_id`/`customer_id` into every seam. Enforced by `verify_keys_surfaces` and
  `verify_keys_isolation`.
- **A char-limit key refuses an over-limit value consistently across surfaces.**
  `app/services/keys_hub.py::_coerce_char_limit` (re-exported through `app/services/keys.py`) is the
  char-limit coercion the service, web and CLI all apply. Enforced by
  `verify_char_limit`.
- **The Delta API answers a cursor-based "what changed" without leaking cross-scope keys.**
  The `/api/keys` change feed is scope-confined and cursor-paged. Enforced by `verify_delta_api` and
  `verify_keys_export_status_filter`.
- **A disabled feature makes zero writes and is non-enumerable.** With
  `setting:key_projects_enabled` off the grid 404s, the create-form type selector disappears and the
  CLI refuses. Enforced by `verify_keys_inertness`.

## 3. Where the work happens

| Concern | Pin | One line |
|---|---|---|
| Key identity layer | `app/services/keys.py` · `app/services/keys_common.py::is_key_project` · `app/services/keys_hub.py::enabled` · `app/services/keys_hub.py::plural_categories_for` · `app/services/keys_part2.py::_enrich_value_entries` · `app/services/keys_hub2.py::gettext_categories_for` · `app/services/keys_hub3.py::gettext_index_map` | The string-key store over the Segment spine, the feature gate, and CLDR plural handling. |
| File round trip | `app/services/key_files.py` · `app/services/key_files_json_parse_render.py::parse_json_keys` · `app/services/key_files_po_parse_render.py::parse_po_keys` · `app/services/key_files_json_parse_render.py::render_json_keys` · `app/services/key_files_po_parse_render_leaf.py::FORMATS` · `app/services/key_files_export.py::export_locale` | JSON (flat + nested) and gettext `.po` import/export producing key upserts. |
| Keys bridge (resource files) | `app/services/key_files_bridge.py::bridge_formats` · `app/services/key_files_bridge.py::parse_resource_keys` · `app/services/key_files_bridge.py::render_resource_keys` · `app/services/key_files_bridge.py::check_cross_format` | Every document filter with a `records()` key view (.properties, .strings, INI, RESJSON, YAML, and each one that merges later) imported into and exported from a key project: discovered from the filter registry, values read through the filter's own parse, exports built by the filter over the stored source file (the template; its blob key includes the project creation time and project delete removes it via `template_blob_keys`), placeholder tokens turned back into real text in key values and back into tokens on export, cross-format key collisions refused. |
| Web + /api + Delta | `app/web/keys.py` · `app/web/keys_hub.py::keys_grid` · `app/web/keys_hub.py::keys_import` · `app/web/keys_hub.py::keys_export` · `app/web/keys_common.py::api_router` · `app/web/keys_hub2.py::key_set_value` | The key grid, the `/api/keys` group, and the change-feed Delta API. |
| CLI surface | `app/cli/cmd_keys.py::register` | The `keys` command group with full `--remote` parity. |
| CLI shared helpers | `app/cli/keys_common.py::_project` / `app/cli/keys_common.py::_flag_gate` / `app/cli/keys_common.py::_resolve_key` | Project/actor/label resolution and the `key_projects_enabled` gate shared by the `keys` verbs. |
| CLI import / export / change feed | `app/cli/keys_import_export.py::register_import_export` / `app/cli/keys_import_export.py::register_changes` / `app/cli/keys_import_export.py::resolve_cursor` | `keys import/export/empties/deliver-*/changes`, including the `--cursor-file` sync cursor. |

## 4. Data & settings

- **Models:** key values are `app/models.py::Segment` rows under system-managed
  `app/models.py::Document` rows (one per namespace, format `keys`), so keys reuse the document
  spine rather than a new table.
- **Settings:** `setting:key_projects_enabled` (default on, the module kill switch) gates the whole feature;
  `setting:key_changelog_retention_days` bounds how long the change feed's history is kept.

## 5. Enforced by

- `verify_keys_core` and `verify_keys_pipeline`: the identity-layer law and the reuse of the spine.
- `verify_keys_files`: the JSON and `.po` round trip through the two seams.
- `verify_keys_surfaces` and `verify_keys_isolation`: the four-surface parity and cross-scope
  confinement.
- `verify_delta_api` and `verify_keys_export_status_filter`: the cursor change feed and export
  filters.
- `verify_keys_inertness`: the zero-write, non-enumerable disabled posture.
- `verify_char_limit`: the consistent over-limit refusal.

## 6. Battle scars

- **Keys had to reuse the Segment spine, not fork a pipeline.** Making key values ordinary segments
  means TM, glossary, LQA, spend and isolation apply for free; a parallel pipeline would have had to
  re-earn every one, recorded in the `app/services/keys.py` module docstring.
- **A CLI `--remote` verb once fell through to a local session and misdirected an upload.** Every
  `keys` verb now dispatches to the remote impl or refuses loudly, kept extinct by a parser-tree
  scan, recorded in the `app/cli/keys_import_export.py` module docstring.

## 7. Limits & accepted risks

- **Only JSON and `.po` ship as formats.** Other formats land later as additive parsers behind the
  same two seams, a locked v1 scope stated in the `app/services/key_files.py` module docstring.
- **A bulk key import is batched, not per-key round trips (#585).** `import_keys` looks up each chunk of 500 keys in one query, reads the namespace document once per chunk and the project's locales once per key (`keys_common.import_memo`; a language added or removed mid-import reaches the very next key), numbers new value rows from memory and inserts them in one SAVEPOINT, and does not reload objects after each commit. It still commits per key, so other writers are never held off, and writes the same feed rows and the one `keys.import` audit row. About 9 queries and 5 to 6 ms per new key (was 25 and 20 ms, and superlinear); `tests/verify_key_import_perf_585.py` holds the bound.
- **The Delta cursor is a change feed, not a full audit.** It answers "what changed since a cursor"
  within retention, not an unbounded history, stated in the `app/web/keys.py` module docstring.

## SQL key paging (F18 / D5)

`list_keys` and `search_keys` compose metadata and correlated value predicates in
one SELECT. Lists retain `(namespace, name, context, id)` order, using SQLite's
default ordering (no COLLATE clause) and PostgreSQL `COLLATE "C"` for all three text
columns. An explicit `namespace=""` selects the root namespace; omission selects
all namespaces. Tags are exact JSON-list membership, not substring matches.
Status and untranslated-locale predicates each mean at least one matching value
row (they need not match the same row). Missing value rows are not untranslated
matches. Guards run before the key SELECT.

`page_keys` applies `id > cursor`, orders by id, and fetches `limit + 1` rows.
The extra row determines exact `has_more`; `next_cursor` is the last returned id
(or the input cursor for an empty page). This is a live traversal, not a snapshot:
new ids may appear later and edits can change filter membership. Renaming does not
move a cursor. The service clamps limits to 1..1000 (default 100); API and grid
refuse invalid limits with 422. The grid defaults to 200 keys, accepts `limit` and
`cursor`, groups only the current page for display (pages follow key id), and preserves every filter in
its next link. Export controls still export their full selected scope.

Archived keys remain excluded by default on grid/API/CLI. The existing service
`include_archived=True` default remains compatible for lifecycle callers.
`key_views` requires an explicit `project_id`, joins values to project-scoped keys,
and uses one query for values and one for bases per chunk of 500 key ids.
`key_view` is its single-key wrapper. A 100-key page plus views uses 1 + 2 SELECTs;
a 1000-key page uses 1 + 4. `iter_keys` yields id-ordered batches (default 1000)
and raises `ValueError` for `batch` below 1 or above 1000.
Consumers requiring a complete sorted list retain `list_keys`.

SQLite connections register `loc_lower(text)` using Python `str.lower`, preserving
the former Unicode lowercase search semantics without casefold's extra expansions.
SQLite uses `loc_lower(column) LIKE loc_lower(:needle)`; PostgreSQL keeps ILIKE.
Literal percent, underscore and backslash in the needle are escaped.
Both dialects use one shared trim character set for `untranslated_in`: ASCII whitespace
(space, tab, newline, carriage return, vertical tab, form feed), U+00A0, U+1680,
U+2000..U+200A, U+2028, U+2029, U+202F, U+205F, U+3000 and U+FEFF (BOM).
SQLite uses `trim`, PostgreSQL `btrim`, with the same characters argument. BOM is
explicitly treated as empty content in addition to those Unicode strip spaces.

The D5 suite counts SELECTs with `before_cursor_execute` and counts actual DBAPI
fetches with a cursor wrapper (SQLite SELECT rowcount is -1). At page size 100,
the service loads 101 key rows in one SELECT. The authenticated API fixture uses
9 SELECTs with warm settings caches: 101 keys, 200 value rows, 200 base-value
scalar rows, and 7 ancillary rows. Query count is independent of project size;
payload work is proportional to page values, including plural forms. The grid
uses 17 SELECTs for 200 keys in the 5,000-key test fixture (one more than before).
Only the grid counts filtered matches, with one extra
`SELECT COUNT(*) FROM (<filtered statement without cursor/limit> LIMIT 10001)`.
It displays the exact count through 10,000 and "10,000+" above that; the API page
route does not count matches and retains its constant query count. `namespaces()`
intentionally remains unchanged.
These bounds concern rows loaded and query count, not constant database scan time
for unindexed substring/JSON predicates. `tests/verify_key_paging_scale.py` runs
at 1,000 and 20,000 keys by default, with `F18_SCALE_KEYS=1000000` for the full
drill. `LOC_TEST_DATABASE_URL` enables the same body against migrated Postgres;
when unset the suite reports an explicit skip. No schema migration is required.
