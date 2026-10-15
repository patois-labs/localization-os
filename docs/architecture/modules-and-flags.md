# Modules & Flags: Architecture (L1)

## 1. Role

This subsystem is the **kill-switch layer**: the one declarative place that says which
non-core capabilities an operator may switch off, and what a switch-off must reach on
every surface (web, CLI, /api, MCP). It is the reason "turn Vendors off" is a single flip
that removes the routes, the nav, the CLI group, the capability cards, and the outbound
traffic, rather than a scavenger hunt across hand-mirrored tables.

The **kill-switch law** is the posture every entry inherits: a switched-off capability is
non-enumerable through the app's own runtime responses. A removed feature's routes answer
404, never 403, so a live request cannot distinguish a disabled feature from one that was
never built. Three semantics classes encode the nuances. A `removed` feature is a
default-on kill switch whose routes the middleware 404s. A `writes_off` feature (the paid Evaluations
and LLMemory add-ons) keeps its reads answering so turning it off never erases the evidence of
what it did, and leans on its service-layer write gates. An `opt_in` feature is default-off
and its own controls stay reachable.

The subsystem has two data homes and one write seam. `app/features.py` is the runtime
registry: the `Feature` record, the segment-pattern matcher the middleware uses, and the
on/off predicates. `app/modules.py` is the **module registry**, the manifest superset from
which the `FEATURES` tuple is projected byte-identically, plus the derivation helpers
(router grouping, CLI-parity rows, MCP gate) and the grow-only optional-slug ratchet.
`app/services/features_admin.py` is the shared write door every surface flips a flag
through, so the web form, the CLI, and the `/api/admin/features` routes cannot drift.

It does **not** own the perimeter switch `remote_admin_enabled` (that is a host-only
posture, documented in tenancy-confinement and configuration, deliberately excluded from
this registry) and it does **not** own the enterprise turn-on door for the boot-brick
features (that is configuration).

## 2. Invariants

- **A removed, switched-off feature 404s before auth.** `app/features.py::path_is_off` is
  the middleware's one question, and only default-on kill switches enforce, with a
  more-specific core pattern winning so a nested-core path survives its parent feature's
  off state. Enforced by `verify_features`.
- **The runtime on-test is a strict allowlist.** `app/features.py::Feature.is_enabled` reads
  a feature as on only for the exact string, so a non-canonical stored value fails toward
  off at every layer. Enforced by `verify_features`.
- **The registry may only grow.** `app/features.py::MIN_FEATURES` is a floor the feature
  table can never drop below, the same ratchet doctrine as the optional-slug allowlist.
  Enforced by `verify_features`.
- **`FEATURES` is projected from the manifest, not hand-maintained.**
  `app/modules.py::features_view` reconstructs every `Feature` field verbatim from
  `MODULES`, so the two tables can never disagree. Enforced by `verify_modules`.
- **An MCP tool's feature gate is derived from the manifest.**
  `app/modules.py::mcp_gate_for` derives the gate from the single module owning a tool's
  routes. Enforced by `verify_modules` (check 9, the digest-plus-sabotage derivation). The
  `ValueError` it raises on a tool whose routes span two modules is a code guard the suite
  does not trigger.
- **The wire has exactly one flag door.** `app/services/features_admin.py::set_feature_enabled`
  refuses a remote write of an install-locked feature key (`FeatureLocalOnly`), refuses a
  moved-door feature (`FeatureDoorMoved`), and a same-state write is a no-op that audits
  nothing. Enforced by `verify_feature_flag_admin`.

- **Paid code is optional, and an absent add-on reads as OFF.** `app/editions.py` is the one
  list of paid modules (`PAID_MODULES`) and of which feature slug each add-on backs
  (`PAID_FEATURE_ADDON`); `app/features.py::Feature.is_enabled` returns False for a paid
  feature whose add-on code is not installed, and `app/features.py::is_enabled` reads an
  unknown slug as OFF. The Module rows and the settings stay. Enforced by `verify_features`
  and `verify_core_edition_imports`.
- **Load-bearing setting membership:** feature keys are install-locked or door-moved in
  `app/services/settings.py::REMOTE_IMMUTABLE_KEYS` (`setting:databricks_export_enabled`,
  `setting:data_export_enabled`, `setting:scim_enabled`, `setting:saml_enabled`), read from there
  rather than copied, so adding a key there closes the wire door on the same commit.
- **Core never imports paid code at module level.** Core reaches an optional add-on through
  `app/extensions.py` (provider facades that return a safe default, content reapers, scheduler
  sweeps) or an optional import that tolerates a missing module. Enforced by
  `verify_core_edition_imports`, an AST scan over every core module.

## 3. Where the work happens

| Concern | Pin | One line |
|---|---|---|
| The feature record | `app/features.py::Feature` | The frozen dataclass naming every surface a switch-off must reach. |
| The middleware question | `app/features.py::path_is_off` · `app/features.py::_matches` · `app/features.py::_specificity` | Longest-match, core-override segment matching that decides whether a path is a removed-and-off route. |
| Runtime on/off | `app/features.py::is_enabled` · `app/features.py::card_is_off` · `app/features.py::feature_by_slug` | The strict on-test, the owned-card removal test, and slug lookup. |
| /api route ownership | `app/features.py::api_owner` · `app/features.py::api_claimants` | The unique-owner accounting the module gate and the MCP derivation rest on. |
| The registry floor | `app/features.py::MIN_FEATURES` | The grow-only feature-count ratchet. |
| The manifest record | `app/modules.py::Module` · `app/modules.py::MODULES` | The superset dataclass and the growing table of optional modules. |
| Projection to features | `app/modules.py::features_view` | Rebuilds the `FEATURES` tuple from the manifest, byte-identically. |
| Router + CLI + MCP derivation | `app/modules.py::router_groups` · `app/modules.py::cli_expected` · `app/modules.py::mcp_gate_for` | Lazily composes the router groups, the CLI-parity rows, and each tool's derived gate. |
| The shared write seam | `app/services/features_admin.py::set_feature_enabled` · `app/services/features_admin.py::resolve` · `app/services/features_admin.py::list_features` | One slug resolution, one refusal order, one audited write for all surfaces. |
| The edition manifest | `app/editions.py::PAID_MODULES` · `app/editions.py::PAID_FEATURE_ADDON` · `app/editions.py::feature_installed` | Which modules are paid add-on code, and which feature slug each backs. |
| The extension registry | `app/extensions.py::register` · `app/extensions.py::run_reapers` · `app/extensions.py::run_sweeps` · `app/extensions.py::addon_missing` | How an add-on plugs into the core, and the no-op the core uses when it is absent. |
| The refusal vocabulary | `app/services/features_admin.py::FeatureNotFound` · `app/services/features_admin.py::FeatureDoorMoved` · `app/services/features_admin.py::FeatureLocalOnly` · `app/services/features_admin.py::FeatureValueInvalid` · `app/services/features_admin.py::DOOR_MOVED` | The four named refusals and the door-moved map (SCIM, SAML). |

## 4. Data & settings

- **No dedicated model.** Feature state lives in the `Setting` key/value table under each
  feature's `setting_key`; a registry key is always a `settings.DEFAULTS` key, pinned by
  `verify_features`.
- **The perimeter switch is deliberately not here:** `setting:remote_admin_enabled` is a
  host-only perimeter posture, not a product feature; it appears in `app/features.py` only
  in a derivation note so the MCP server's 404-note set knows the app 404s on it too.

## 5. Enforced by

- `verify_features`: the kill-switch law across web/api/hooks/CLI/cards/nav, the strict
  on-test, the grow-only floor, and the cross-feature reach guard.
- `verify_modules`: the manifest-to-features projection equivalence, the router/CLI/MCP
  derivations, and the optional-slug ratchet.
- `verify_feature_flag_admin`: the shared write seam's four refusals, the one-wire-door
  law, and the sensitive-key ratchet.
- `verify_core_edition_imports`: no core module imports a paid module at module level, and
  lazy reaches into paid code only shrink.

## 6. Battle scars

- **A URL sweep is blind to import-reach; the manifest closes that hole.** A caller that
  mutated a feature's data with the feature off, reached by import rather than by route,
  drove the `service_modules` / `cross_reach_allow` cross-reach guard.
- **The wire flag door is enforced, not just documented.** A leaked admin key could
  otherwise flip an install-locked feature by spelling its raw key into the generic config
  route; the immutable-key refusal in the shared seam closes that bypass.
- **The module machinery is a consolidation, not a plugin framework.** It froze the
  per-module declaration tax into one manifest without adding runtime capability, and the
  four hand-coded `app/main.py` boot branches deliberately stayed hand-coded.

## 7. Limits & accepted risks

- **`feature.toggle` is the complete record of REMOTE flag changes only.** A host operator
  running `config set <feature_key> 0` still writes a separate local `config.set` row; host
  doors are deliberately untouched, the lock is on the wire, stated in the
  `app/services/features_admin.py` docstring.
- **An unknown slug reads as disabled.** `app/features.py::is_enabled` is fail-closed (it
  used to read as enabled); every slug the app itself asks about is registered, which
  `verify_features` pins, and the Settings toggle route validates the slug separately.
- **Per-module assembly inclusion is not achievable while the /api router is shared.** The
  registry relocates only router-to-group tagging, not per-module mounting.
- **Downgraded installs keep add-on data nobody reaps.** The database stays whole in the core
  edition: every paid table is still created and migrated. If a full install is moved onto the
  core edition and a paid table (see `app/editions.py::ADDON_TABLES`) still holds rows, project
  and document delete clear the leftover Advanced and memory rows in plain SQL so a delete never
  fails, but retention purge and GDPR erasure cannot reach the rest of that data, because the
  reaper lives in the add-on. `app.extensions.warn_orphaned_addon_data` logs one boot warning
  naming the add-on and the tables; it deletes nothing. Retention and erasure of that data needs
  the add-on installed.
