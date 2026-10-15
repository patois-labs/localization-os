# Third-party notices

Localization OS is original work, licensed under the Apache License 2.0 (see `LICENSE` and
`NOTICE`). This file records third-party material that informed or was adapted into the
codebase, with attribution as required by the source license. Runtime Python dependencies
are installed from `requirements.lock` and keep their own licenses; the default install
is checked to stay free of GPL-family licenses by `tests/verify_licenses.py`.

## Connector factory - reference connectors (MIT)

The connector factory studies
Blackbird's open-source connector repositories at https://github.com/bb-io (and, for Plytix,
Supplyline/plytix-mcp) under their verbatim MIT licenses. The default mode is **reference-and-reimplement**:
the design (endpoint coverage, auth flow, pagination, localization strategy) is
understood from the C# reference and then written fresh in Python. Reimplemented
understanding does not, by itself, require attribution; entries are nonetheless
recorded here for provenance (campaign rule: when in doubt, attribute).

### Strapi connector - `app/services/connectors/strapi.py`

- Source studied: **bb-io/Strapi** (https://github.com/bb-io/Strapi),
  © 2023 Blackbird, MIT License (verified verbatim 2026-08-06 from
  `raw.githubusercontent.com/bb-io/Strapi/main/LICENSE`).
- Nature of use: **reference-and-reimplement.** The Localization OS Strapi connector
  is original Python written against Strapi's public REST API. The bb-io C# app
  informed our understanding of: the i18n plugin's per-locale document model, the
  v4-vs-v5 API split (`documentId` PUT-upsert vs the `localizations` sub-resource),
  the `/api/i18n/locales` enabled-locale probe, `pagination[page]`/`pageCount`
  paging, and the "content type id must be the plural form" diagnostic. No C#
  code, no literal logic port, and no bb-io assets were copied. Recorded here for
  provenance.

### Contentful connector - `app/services/connectors/contentful.py`

- Source studied: **bb-io/Contentful** (https://github.com/bb-io/Contentful),
  © 2023 Blackbird, MIT License (verified verbatim 2026-08-07 from
  `raw.githubusercontent.com/bb-io/Contentful/main/LICENSE` - canonical OSI MIT,
  three components, zero added clauses).
- Nature of use: **reference-and-reimplement.** The Localization OS Contentful connector
  is original Python written against Contentful's public Content Management API
  (CMA). The bb-io C# app informed our understanding of: the `fields.{id}.{locale}`
  field-outer localization model, the schema-driven `field.localized` gate and the
  `effectiveLocale = field.localized ? locale : defaultLocale` rule, the
  space+environment scoping (`/spaces/{space}/environments/{env}/…`), the
  `X-Contentful-Version` optimistic-concurrency header and its
  retry-on-"Version mismatch" behaviour (re-fetch fresh version, re-apply, re-write),
  the locale-scoped partial update (`UpdateEntryForLocale`) that writes only the
  target locale, the `JToken.DeepEquals` no-op skip, and the enabled-locale /
  default-locale probe (`GET /locales`). The rich-text HTML round-trip and the
  recursive linked-entry embedding were studied but deliberately DEFERRED from v1
  (see the module docstring).
  No C# code, no literal logic port, and no bb-io assets were copied. Recorded here
  for provenance.

### Shopify connector - `app/services/connectors/shopify.py`

- Source studied: **bb-io/Shopify** (https://github.com/bb-io/Shopify),
  © 2023 Blackbird, MIT License (verified verbatim 2026-08-07 from
  `raw.githubusercontent.com/bb-io/Shopify/main/LICENSE` - canonical OSI MIT, no
  added clauses, byte-for-byte the same license text as the Strapi and Contentful
  repos).
- Nature of use: **reference-and-reimplement.** The Localization OS Shopify connector is
  original Python written against Shopify's public Admin GraphQL API. The bb-io C#
  app informed our understanding of: the identity-less translation model addressed by
  (resourceId, key, locale), the `translatableResource`/`translatableResources`
  read queries returning `translatableContent { key value digest type }` plus the
  `translations(locale:)` overlay, the `translationsRegister` upsert mutation with its
  required per-key `translatableContentDigest`, the `shopLocales { locale primary
  published }` locale/primary probe, and cursor pagination. Three defects seen in the
  reference were deliberately NOT ported and are instead treated as design
  requirements (see the module docstring): the unsanitized `store_name`
  host-injection, the download-time (stale) digest carried into first-time writes, and
  the silent no-op skip of source-equal translations. The reference's stale hardcoded
  `2024-04` API version was not ported (a current version is pinned). No C# code, no
  literal logic port, and no bb-io assets were copied. Recorded here for provenance.

### HubSpot connector - `app/services/connectors/hubspot.py`

- Source studied: **bb-io/Hubspot** (https://github.com/bb-io/Hubspot),
  © 2023 Blackbird International Inc., MIT License (verified verbatim 2026-08-07 from
  `raw.githubusercontent.com/bb-io/Hubspot/main/LICENSE` - canonical OSI MIT, no added
  clauses, no riders; the copyright holder is spelled "Blackbird International Inc."
  where the Strapi/Contentful/Shopify repos say "Blackbird" - immaterial to the MIT
  grant, recorded verbatim here for provenance).
- **The sibling repo `bb-io/HubspotCRM` has NO license file** (404 at both
  `/main/LICENSE` and `/master/LICENSE`; the GitHub metadata `license` field is null).
  Per the standing rule an unlicensed repo is off-limits: it was **not read, not
  scouted and not referenced** in any form during this build, and nothing in this
  connector derives from it. It is also out of scope on the merits (CRM records have
  no multi-language model).
- Nature of use: **reference-and-reimplement.** The Localization OS HubSpot connector is
  original Python written against HubSpot's public CMS v3 API. The bb-io C# app
  informed our understanding of: the native language-group model (`language`,
  `translatedFromId`, and the `translations` dict of locale -> `{id, slug}` read
  straight off every object), the `multi-language/create-language-variation` call and
  its clone-into-DRAFT semantics, the site-pages / landing-pages / blog-posts endpoint
  split, cursor pagination via `paging.next.after`, the resolve-to-group-primary step
  before creating a variation, and the fact that publishing is always a separate
  action. Several defects seen in the reference were deliberately NOT ported and are
  instead treated as design requirements (see the module docstring): the whole-document
  `<body original="...">` JSON-in-an-HTML-attribute round-trip anchor (we walk the
  module-tree JSON directly and never build that document at all), the silent skipping
  of custom-module text properties outside a hardcoded name list (ours reports every
  prose-looking skip), the `BasePageActions` code path that creates a variation off a
  non-primary id, treating a variation's mere existence as "translated" (which marks a
  crash-window phantom clone - 100% source text - as done forever), and the absence of
  any 429/backoff handling (ours rides the shared `base._request` retry primitive).
  Marketing emails, marketing forms and HubDB were studied and **deliberately deferred**
  from v1 (see the module docstring's V1 SCOPE block).
  No C# code, no literal logic port,
  and no bb-io assets were copied. Recorded here for provenance.

### WordPress connector - `app/services/connectors/wordpress.py`

- Source studied: **bb-io/Wordpress** (https://github.com/bb-io/Wordpress),
  © 2023 Blackbird International Inc., MIT License (verified verbatim 2026-08-07 from
  `raw.githubusercontent.com/bb-io/Wordpress/main/LICENSE` - canonical OSI MIT, no
  added clauses, byte-for-byte the standard MIT text).
- Nature of use: **reference-and-reimplement.** The Localization OS WordPress connector is
  original Python written against WordPress's public REST API (`wp/v2`) and Polylang's
  Languages REST API (`pll/v1`). The bb-io C# app informed our understanding of: the
  Polylang doc-per-locale model (a separate `wp_posts` row per language linked by the
  `translations: {locale: id}` map), the Application-Passwords HTTP Basic auth, the
  `?lang=`/`?translations[xx]=` write-linking params, and the `pll/v1/languages`
  probe. FOUR defects seen in the reference were deliberately NOT ported and are
  instead treated as design requirements (see the module docstring + the digest §6):
  the `content.rendered` read/write that flattens Gutenberg blocks (we read/write
  `content.raw` via `?context=edit`), the absence of any Pro-tier self-detection (we
  detect it and degrade to pure T3, never NPE on a null `translations` field), the
  missing `status` param on pages + publish-only Search default (we send an explicit
  status on both posts and pages - status on CREATE only, omitted on UPDATE so a
  human-published translation is not un-published), and the pagination off-by-one that
  drops the last page (we loop `while page <= total_pages`). Beyond the reference we add
  our own hardening: SHORTCODE MASKING (a `[shortcode]` is a leaf text node to the html
  filter, so we mask each bracket span as a protected placeholder and restore it verbatim,
  never sending it to the translation engine); a FREE-TIER SOURCE SCOPE (Polylang free
  exposes no per-post language signal, so whole-site discovery is refused fail-closed
  unless a source category/tag is configured); ORPHAN RECLAMATION + external-deletion
  detection (matching the other T3 consumers); and excerpt translation. No C# code, no
  literal logic port, and no bb-io assets were copied. Recorded here for provenance. Live
  QA is DEFERRED (Polylang Pro cost) - this connector ships flagged "live QA pending"; see
  the module docstring.


### Akeneo connector - `app/services/connectors/akeneo.py`

- Source studied: **bb-io/Akeneo** (https://github.com/bb-io/Akeneo),
  (c) 2023 Blackbird, MIT License. Verified verbatim from
  `raw.githubusercontent.com/bb-io/Akeneo/main/LICENSE` - canonical OSI MIT, no added clauses.
  Re-verified at the moment of use **2026-08-10** (copyright line `Copyright (c) 2023
  Blackbird`, 21 lines, standard permission grant + "AS IS" disclaimer; a prior scout verified
  it 2026-08-09 and the file was unchanged).
- Nature of use: **reference-and-reimplement.** The Localization OS Akeneo connector is original
  Python written against Akeneo's public REST API. The bb-io C# app informed our understanding
  of: the REST endpoint coverage (the UUID-based `products-uuid` and code-based
  `product-models` endpoints, `attributes`, `locales`); the `values` payload shape - one
  object keyed by attribute code, each an array of `{locale, scope, data}` triples (T1
  field-map); the content round-trip design (export locale-filtered values, then a PARTIAL
  PATCH re-import with **zero read-before-write** - the single design fact deliberately
  replicated, since Akeneo's product PATCH is an officially-documented per-triple partial
  merge); the Connect-Apps OAuth authorization-code flow with its custom
  `SHA256(code_identifier + client_secret)` code challenge (studied, NOT built - v1 ships a
  pasted Bearer access token, the OAuth flow is a future enhancement; nothing branches on how
  the token was obtained); and the polling-based change detection (updated-date search
  narrowed by a SHA256 content hash) - reimplemented here as the framework's own content-hash
  suppression.
- Notable defects/gaps identified in the reference and deliberately NOT ported (treated as
  design requirements): (1) **no retry/backoff/rate-limit handling anywhere** in the C# client
  despite Akeneo's documented 100 req/s general + 3 req/s attribute-option-write limits - our
  writes ride the shared `base._request` retry/backoff; (2) **no operator-visible reporting of
  skipped non-localizable attributes** - we report every skip with a reason via the shared
  SelectionTracker (taxonomy D); (3) **no held-back / no-op accounting** (the reference is not
  framework-aware) - we omit a withheld translation from the write body entirely and record no
  suppression hash on an all-held run (taxonomy B/F); (4) a probable **copy-paste bug** passing
  `ContentTypeConstants.Product` (not `.ProductModel`) into
  `ProductModelActions.GetProductModelHtml`'s `ContentRequest` - not ported (our product-model
  path is a distinct code path keyed on the endpoint); (5) the **dead `PaginateUsingSearchAfter`
  HAL pagination path** (declared, never called) - not replicated (we page/limit-paginate);
  (6) the **password-grant auth** used only in the reference's integration-test convenience
  code - not shipped. No C# code, no literal logic port, and no bb-io assets were copied.
  Recorded here for provenance.

### Drupal connector - `app/services/connectors/drupal.py`

- Source studied: **bb-io/Drupal** (https://github.com/bb-io/Drupal),
  (c) 2023 Blackbird, MIT License. Verified verbatim from
  `raw.githubusercontent.com/bb-io/Drupal/main/LICENSE` - canonical OSI MIT, no added clauses.
  Re-verified at the moment of use **2026-10-10** (copyright line `Copyright (c) 2023
  Blackbird`, standard permission grant + "AS IS" disclaimer).
- Nature of use: **reference-and-reimplement, minimal.** The Localization OS Drupal connector is
  original Python written against Drupal's public JSON:API documentation and the
  `jsonapi_multilingual` module's README. The bb-io C# app is a different integration shape: it
  talks to a custom `tmgmt_blackbird` Drupal module (TMGMT translation jobs under
  `/api/tmgmt/blackbird/...`, one API key), not to JSON:API. What it informed: the decision to
  treat the TMGMT-translator route as a separate product shape (a module on the customer site)
  and keep ours on core JSON:API; its retry policy (a bounded exponential backoff with jitter
  on 429 and 504) - reimplemented here by the shared `base._request`; and its handling of an
  HTML error page from a proxy or login redirect (show the page title, not the body) -
  reimplemented as a bounded JSON:API error summary that never echoes a raw body.
- Notable defects/gaps identified in the reference and deliberately NOT ported (treated as
  design requirements): (1) **the upstream error body is echoed whole** into the exception
  message when the response is not HTML - we surface only the status plus a bounded, control
  character stripped JSON:API `title`/`detail`; (2) **retries only on 429 and 504**, never on
  the other 5xx or on transport errors, and retries POST writes without an idempotency
  distinction - ours retries reads and PATCH on every 5xx and transport error, and a POST
  create only on 429; (3) **the job id is interpolated into the request path with only a blank check** - ours validates the
  resource type against the connection's configured list and the id against a strict UUID
  pattern before any URL is built; (4) **no operator-visible reporting of fields it did not
  translate and no held-back accounting** (a TMGMT job is all-or-nothing) - we report every
  skipped field with a reason and never write a withheld field over a stored translation.
  No C# code, no literal logic port, and no bb-io assets were copied. Recorded here for
  provenance.
- Also studied (documentation only, no code): the Drupal.org JSON:API Translations page and the
  `jsonapi_multilingual` module README (a Drupal.org contrib module under Drupal's GPL licensing, NOT bundled or
  linked; the customer installs it on their own site and we only call its HTTP interface). The
  A review (2026-10-10) also read the module's `src/Controller/EntityResource.php` and Drupal
  core's JSON:API `EntityResource.php` and `ContentEntityBase.php` to verify write behaviour;
  no code was copied.

### Sitecore connector - `app/services/connectors/sitecore.py`

- Source studied: **bb-io/SitecoreGraphQl** (https://github.com/bb-io/SitecoreGraphQl),
  (c) 2023 Blackbird, MIT License. Verified verbatim from
  `raw.githubusercontent.com/bb-io/SitecoreGraphQl/main/LICENSE` - canonical OSI MIT, no added
  clauses - at the moment of use, **2026-10-10** (the sibling repos bb-io/Sitecore and
  bb-io/SitecoreXmCloud were verified the same day and were not read). The shape of the
  `item`, `languages`, `search`, `addItemVersion` and `updateItem` operations was also
  cross-checked against the public Sitecore-Studio-Labs/sitecoreai-cli repository (MIT per
  its GitHub licence metadata); nothing was copied from it.
- Nature of use: **reference-and-reimplement.** The Localization OS Sitecore connector is
  original Python written against Sitecore's public Authoring and Management GraphQL API. The
  bb-io C# app informed our understanding of: the XM Cloud client-credentials token exchange
  (host `auth.sitecorecloud.io`, audience `https://api.sitecorecloud.io`); the authoring
  endpoint path `/sitecore/api/authoring/graphql/v1`; the localization model (one item id,
  numbered versions per language; a language with no version answers version 0, so the
  create-versus-update decision is "version above zero", then `addItemVersion` followed by
  `updateItem` on the new version); the `languages` list; and the `search` query with a
  `_path` criterion and paging used to walk a subtree.
- Notable defects/gaps identified in the reference and deliberately NOT ported (treated as
  design requirements): (1) **GraphQL built by string concatenation** with only a partial
  escape on field values (item ids, languages and paths are spliced straight into the query
  text) - we send every caller or operator value as a GraphQL variable and inline only a
  validated GUID; (2) **no scope check** - any item id in the tenant could be read or
  written - we confine every read and write to a configured content root; (3) **shared
  template fields are written like any other**, which would overwrite the source language -
  we never write a field the API does not state as non-shared; (4) **no held-back or no-op
  accounting** - we omit a withheld field from the update and record no content hash for an
  incomplete pair; (5) **`updateItem` with a null version** when none is known - we always
  address an explicit version; (6) **no retry or rate-limit handling and a token minted on
  every request** - writes ride the shared `base._request` backoff and the token is minted
  once per run and re-minted once on a 401; (7) a missing target version is read as
  `Version > 0` on a possibly null item - we treat a missing item and a version-less language
  as distinct outcomes. No C# code, no literal logic port, and no bb-io assets were copied.
  Recorded here for provenance.

### Plytix connector - `app/services/connectors/plytix.py`

- Source consulted: **Supplyline/plytix-mcp** (https://github.com/Supplyline/plytix-mcp),
  (c) 2025 Eric Pittman, MIT License. Verified verbatim from
  `raw.githubusercontent.com/Supplyline/plytix-mcp/main/LICENSE` on **2026-10-10** (standard MIT
  grant and "AS IS" disclaimer, no added clauses). No bb-io code was read: bb-io has no Plytix
  repository.
- Nature of use: **facts only, no code copied.** The repository's API notes
  (`docs/solutions/api-quirks/plytix-api.md`) were read to confirm endpoint paths and payload
  shapes that the official docs at apidocs.plytix.com publish only as an interactive page: the
  token exchange at `auth.plytix.com/auth/api/get-token`, the 15-minute token life, the v2
  `products/search` request (`filters`, `attributes.<label>` columns, `pagination`), the v2
  `PATCH /products/{id}` request shape with an `attributes` object (that it MERGES rather than
  replaces the attribute set is not documented there), the v1 attribute search and
  the rate-limit windows. The Localization OS connector is original Python.
- Reference gaps deliberately NOT ported: none of its tooling is used; our connector adds the
  attribute-map validation (a source attribute is never written), held-back accounting (#640),
  content-hash suppression and the shared egress / WriteBudget primitives.

### GitHub connector (and the shared git core) - `app/services/connectors/github.py`, `git_core.py`

- Source studied: **bb-io/Github** (https://github.com/bb-io/Github),
  (c) 2023 Blackbird International Inc., MIT License (verified verbatim 2026-10-10 from
  `raw.githubusercontent.com/bb-io/Github/main/LICENSE`, canonical OSI MIT, no added clauses).
- Nature of use: **reference-and-reimplement.** The Localization OS GitHub connector is original
  Python written against GitHub's public REST documentation. The bb-io C# app informed our
  understanding of the action list (list repository content, get files by path, create or update
  a file, create a branch, open a pull request, check that a file exists), of the personal access
  token and OAuth authentication options, and of the one-file-per-locale repository model.
  Deliberately NOT ported: one API call and one commit per file (we build one tree and one commit
  per run), writing straight to a named branch with no fast-forward guard, and any merge of the
  pull request (this connector never merges). No C# code, no literal logic port and no bb-io
  assets were copied. Recorded here for provenance.

## Standards artifacts not shipped - TBX-Basic v3 (DCA)

TBX-Basic v3 (DCA style) glossary import and export are original code. Strict validation
(`strict=true` on a TBX import) additionally needs two official ISO 30042 / LTAC-Global files,
`TBXcoreStructV03.rng` (LTAC-Global/TBX_Core_RNG) and `TBX-Basic_DCA.sch`
(LTAC-Global/TBX-Basic_dialect). Neither upstream repository carries a LICENSE file, so
Localization OS does **not** redistribute them. `scripts/fetch_tbx_schemas.py` downloads them
from the upstream URLs on request (size and sha256 pinned); until then a strict request is
refused with a message saying so. Provenance and the pinned hashes are in
`app/resources/tbx/PROVENANCE.md`. `app/resources/tbx/los-ext-v1.xcs` is Localization OS's own
authored private-namespace (los:) fragment and is not third-party.

## Vendored front-end library: driver.js (MIT)

### driver.js 1.8.0: `app/static/vendor/driver/`

- Source: **driver.js** 1.8.0 from the npm registry
  (https://registry.npmjs.org/driver.js/-/driver.js-1.8.0.tgz; repo
  https://github.com/kamranahmedse/driver.js), (c) Kamran Ahmed, **MIT License**. The
  upstream license text ships beside the files as `app/static/vendor/driver/LICENSE`.
- Nature of use: **verbatim vendored copy** of `dist/driver.js.iife.js` and `dist/driver.css`,
  unmodified below a one-comment provenance banner. Served from `/static` (no CDN) for the
  optional onboarding spotlight, so it **ships** in the product image. Version, tarball
  integrity and upgrade steps: `app/static/vendor/driver/README.md`.

## Bundled fonts (SIL Open Font License 1.1)

### Self-hosted UI typefaces: `app/static/fonts/`

- Source: the official variable or static TrueType files in the `google/fonts` repository
  (`ofl/<family>/`), subset locally to Latin plus the arrow range and saved as `woff2`
  (procedure and per-file table: `app/static/fonts/README.md`). Both families are licensed
  under the **SIL Open Font License, Version 1.1**; each upstream `OFL.txt` ships beside the
  fonts as `OFL-<Family>.txt`, which is what the licence requires for bundling:
  - JetBrains Mono, (c) 2020 The JetBrains Mono Project Authors
    (https://github.com/JetBrains/JetBrainsMono)
  - Public Sans, (c) 2015 The Public Sans Project Authors (https://github.com/uswds/public-sans)
- Nature of use: **subsetted copies** served from `/static` (no CDN), so they **ship** in the
  product image. Subsetting is the only modification, and the fonts are not sold
  separately. Public Sans is the UI face and JetBrains Mono the data face of the Console look
  (DESIGN.md, "Typography"). The earlier Space Grotesk, Hanken Grotesk, IBM Plex and Spectral
  files were removed with the skin system.

### Image typesetting fonts: `assets/fonts/`

- Noto Sans Regular (`NotoSans-Regular.ttf`), (c) The Noto Project Authors
  (https://github.com/notofonts/notofonts.github.io), SIL Open Font License 1.1; license text in
  `assets/fonts/OFL.txt`.
- Noto Sans CJK SC subset (`NotoSansCJK-Regular.otf`), (c) 2014-2021 Adobe, with Reserved Font Name
  'Source' (https://github.com/notofonts/noto-cjk), SIL Open Font License 1.1; license text in
  `assets/fonts/LICENSE-NotoCJK.txt`.
- Nature of use: **unmodified copies** bundled so the image localization renderer draws translated
  text identically on every machine (`assets/fonts/README.md`). They ship in the product image and are
  not sold separately.

## Test-only tooling: axe-core (MPL-2.0)

### axe-core 4.13.0: `tests/fixtures/axe/`

- Source: **axe-core** 4.13.0 from the npm registry (repo https://github.com/dequelabs/axe-core),
  (c) 2015 - 2026 Deque Systems, Inc., **Mozilla Public License 2.0**. `axe.min.js` is vendored
  **unmodified** with the upstream `LICENSE` (MPL-2.0) and `LICENSE-3RD-PARTY.txt` (the MIT
  notices for the components bundled into axe) beside it. The only edit is whitespace: trailing
  spaces removed from two "Applies to:" lines of `LICENSE-3RD-PARTY.txt`; no wording changed.
- Nature of use: **test-only.** `tests/verify_a11y_browser.py` injects it into pages of a
  throwaway test instance to find accessibility violations. It is **not shipped**:
  `.dockerignore` excludes `tests/`, and nothing under `app/` loads it. The file is
  byte-pinned (size + sha256 in `tests/verify_a11y_static.py`, and `binary` in `.gitattributes` so no checkout
  rewrites it). MPL-2.0 is file-level weak copyleft and not GPL-family; because the file is
  distributed unmodified in source form with its license, its terms are met as is.

## Bundled data: common-password list (MIT)

### `app/resources/common_passwords.txt`

- Source: **SecLists** by Daniel Miessler (https://github.com/danielmiessler/SecLists), file
  `Passwords/Common-Credentials/10k-most-common.txt` at commit
  `913b327317496d062bcc7cace524aaad8a693be2` (fetched 2026-09-25), **MIT License**,
  Copyright (c) 2018 Daniel Miessler (verified from the repository's `LICENSE`).
- Nature of use: **derived data, normalized.** Each of the 10,000 entries is reduced to its
  normalized "core" by `app/services/password_policy.py::common_core` (lowercased, a whole
  repeat collapsed, leading and trailing digits and symbols stripped, runs of 3+ identical
  characters collapsed; for example `password1` becomes `password`). Cores shorter than 4
  characters are dropped and duplicates removed, one per line: 8,225 cores, about 60 KB. Read
  offline for the `password_block_common` rule, which compares the same core of a proposed
  password, so "password1234" or "iloveyou2024!" match even though the list's long entries are
  few; nothing is fetched at runtime. (Revised 2026-09-25 in the gate fix round; the first
  version kept only the 2,087 entries of 8+ characters, unnormalized.)
- MIT permission notice: Permission is hereby granted, free of charge, to any person
  obtaining a copy of this software and associated documentation files (the "Software"), to
  deal in the Software without restriction, including without limitation the rights to use,
  copy, modify, merge, publish, distribute, sublicense, and/or sell copies of the Software,
  and to permit persons to whom the Software is furnished to do so, subject to the following
  conditions: The above copyright notice and this permission notice shall be included in all
  copies or substantial portions of the Software. THE SOFTWARE IS PROVIDED "AS IS", WITHOUT
  WARRANTY OF ANY KIND, EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF
  MERCHANTABILITY, FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
  AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER LIABILITY, WHETHER IN
  AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM, OUT OF OR IN CONNECTION WITH THE
  SOFTWARE OR THE USE OR OTHER DEALINGS IN THE SOFTWARE.


## File-format references: Haiku Locale Kit (MIT)

### Haiku `.catkeys` catalogs - `app/filters/catkeys_filter.py`

- Source studied: **Haiku** (https://github.com/haiku/haiku, the GitHub mirror of
  git.haiku-os.org), files `src/tools/locale/PlainTextCatalog.cpp` and
  `src/kits/locale/HashMapCatalog.cpp`, each headed "Copyright 2009, Adrien Destugues,
  pulkomandy@gmail.com. All rights reserved. Distributed under the terms of the MIT License."
  The repository's `License.md` carries the canonical MIT text with no added clauses
  (verified verbatim 2026-10-10 by the format gate, train-9).
- Nature of use: **reference-and-reimplement, one small logic port.** The plain-text catalog
  layout (header `1<TAB>language<TAB>signature<TAB>fingerprint`, entries
  `source<TAB>context<TAB>comment<TAB>translation`) and the escape rules
  (`escapeQuotedChars` / `parseQuotedChars`) were read and written fresh in Python. The
  catalog fingerprint (`CatKey::HashFun`: h = 5 * h + signed byte, then 5 * h + 1, chained
  source -> context -> comment; `HashMapCatalog::ComputeFingerprint`: the uint32 sum of the
  key hashes) is reimplemented as `catkeys_fingerprint`, used only to check a header; the
  filter never rewrites the fingerprint. No C++ code was copied.
- MIT permission notice (Copyright 2009, Adrien Destugues): Permission is hereby granted, free
  of charge, to any person obtaining a copy of this software and associated documentation
  files (the "Software"), to deal in the Software without restriction, including without
  limitation the rights to use, copy, modify, merge, publish, distribute, sublicense, and/or
  sell copies of the Software, and to permit persons to whom the Software is furnished to do
  so, subject to the following conditions: The above copyright notice and this permission
  notice shall be included in all copies or substantial portions of the Software. THE
  SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR IMPLIED, INCLUDING
  BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY, FITNESS FOR A PARTICULAR PURPOSE AND
  NONINFRINGEMENT. IN NO EVENT SHALL THE AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY
  CLAIM, DAMAGES OR OTHER LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE,
  ARISING FROM, OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
  SOFTWARE.

## Container image: Tailscale binaries (BSD-3-Clause)

The optional private-egress sidecar copies the static `tailscale` and `tailscaled` binaries
unmodified from the official `tailscale/tailscale` image (pinned by digest in the `Dockerfile`)
into the runtime image. They run only when `TS_AUTHKEY` is set; without it no Tailscale process
starts. Source: https://github.com/tailscale/tailscale. License: BSD 3-Clause.

- BSD 3-Clause License. Copyright (c) Tailscale Inc & AUTHORS (see https://github.com/tailscale/tailscale/blob/main/LICENSE). All rights reserved.
  Redistribution and use in source and binary forms, with or without modification, are
  permitted provided that the following conditions are met: (1) Redistributions of source code
  must retain the above copyright notice, this list of conditions and the following disclaimer.
  (2) Redistributions in binary form must reproduce the above copyright notice, this list of
  conditions and the following disclaimer in the documentation and/or other materials provided
  with the distribution. (3) Neither the name of the copyright holder nor the names of its
  contributors may be used to endorse or promote products derived from this software without
  specific prior written permission. THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND
  CONTRIBUTORS "AS IS" AND ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED TO,
  THE IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A PARTICULAR PURPOSE ARE
  DISCLAIMED. IN NO EVENT SHALL THE COPYRIGHT HOLDER OR CONTRIBUTORS BE LIABLE FOR ANY DIRECT,
  INDIRECT, INCIDENTAL, SPECIAL, EXEMPLARY, OR CONSEQUENTIAL DAMAGES (INCLUDING, BUT NOT
  LIMITED TO, PROCUREMENT OF SUBSTITUTE GOODS OR SERVICES; LOSS OF USE, DATA, OR PROFITS; OR
  BUSINESS INTERRUPTION) HOWEVER CAUSED AND ON ANY THEORY OF LIABILITY, WHETHER IN CONTRACT,
  STRICT LIABILITY, OR TORT (INCLUDING NEGLIGENCE OR OTHERWISE) ARISING IN ANY WAY OUT OF THE
  USE OF THIS SOFTWARE, EVEN IF ADVISED OF THE POSSIBILITY OF SUCH DAMAGE.

## Container image contents

The published container image (built from the `Dockerfile`) carries more than this repository's
own files. Everything below is installed unmodified from its upstream distribution, and each
component keeps its own license:

- **Base image**: the official `python` slim image (Debian). Python is under the PSF License;
  Debian packages carry their own licenses, and each package's copyright and license text is in
  `/usr/share/doc/<package>/copyright` inside the image.
- **Debian packages added to the runtime image**: `libpq5` and `postgresql-client` (PostgreSQL
  License), `curl` (curl license, MIT-style), `fonts-noto-core` and `fonts-noto-cjk` (SIL Open
  Font License 1.1). Opt-in build arguments add `libxmlsec1-openssl` (SAML, MIT) and
  `libsqlcipher1` (SQLCipher, BSD-style).
- **psycopg (LGPL-3.0)**: the container image includes the `psycopg` PostgreSQL driver
  (`requirements-postgres.lock`), which is licensed under the GNU Lesser General Public License
  v3.0. It is installed unmodified as a separate Python package in `/opt/venv`; it is not linked
  into or combined with Localization OS source files, and you can replace it with another
  version by rebuilding the image with a different pin. Its license text ships with the package
  (`/opt/venv/lib/python*/site-packages/psycopg*/`). The default `pip install -r requirements.txt`
  install does not include it. The optional `ldap3` driver (LGPL-3.0) is likewise installed only
  with the `WITH_LDAP` build argument.
- **Python packages**: everything in `requirements.lock` keeps its own license; the default
  install is checked by `tests/verify_licenses.py`.
- **Tailscale binaries**: see the previous section.

The `LICENSE`, `NOTICE` and `THIRD_PARTY_NOTICES.md` files are copied into the image at `/app`.
