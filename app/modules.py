"""The MODULE REGISTRY -- one manifest record per toggleable (optional) module.

This is the composable-core Phase 5 consolidation of the toggleable-module
DECLARATION. It is NOT a plugin framework and adds no runtime capability: it is
the single home for what a new optional module must declare, so the per-module
declaration tax stops being spread across hand-mirrored tables.

WHAT LIVES HERE (three things, three mutability rules -- SPEC-module-registry S3):
  * The `Module` MACHINERY -- the frozen-dataclass SUPERSET of app.features.Feature,
    plus the derivation helpers (features_view / router_groups / cli_expected /
    mcp_gate_for). Built across the phase's two trains; with M3 landed the machinery is
    now FROZEN (SPEC-module-registry S3 item 1) -- extend it only via a new spec.
  * The manifest TABLE `MODULES` -- a GROWING table (the MIN_FEATURES tradition): a
    new optional module is a new `Module(...)` row.
  * The optional-slug RATCHET (`OPTIONAL_SLUGS` / `REMOVED_OPTIONAL`) -- a ratcheted
    ALLOWLIST so a capability can never be silently de-flagged (S3.5).

THIS MODULE IMPORTS NOTHING AT MODULE LEVEL beyond stdlib/dataclasses -- exactly the
app/features.py discipline. Router attributes are resolved LAZILY inside
router_groups() (a function-body `import`), never at module scope, so
`import app.modules` succeeds with app.web NOT in sys.modules (S3.6): a guard can
read the manifest cheaply, with no router tree and no DB engine. app.features.Feature
is likewise imported INSIDE features_view(), never at module scope, so there is no
import cycle with app.features (which imports features_view() from here).

WHY `main.py`'s THREE `IS_EMBEDDED` boot branches STAY hand-coded (M10, CUT M2): they
are behavioural boot logic (the /static mount, the startup wall/seed/daemon branch, and
the `_ROUTERS` compose), not declarations, and per-module assembly inclusion is
impossible while `app.web.api_cli.router` is a single shared
router (S1a) -- so `ROUTER_GROUPS` below relocates only the router->group tagging,
never a per-module mount.

Documented in: docs/architecture/modules-and-flags.md
"""

from __future__ import annotations

from dataclasses import dataclass
from dataclasses import fields as _dc_fields


# ============================================================================ #
# The module record -- a SUPERSET of app.features.Feature (every Feature field
# verbatim, so features_view() reproduces the FEATURES tuple byte-identically).
# Data only: the on/off predicates (is_enabled / default_on) stay on Feature, which
# features_view() constructs from these records. No new structural field this train.
# ============================================================================ #
@dataclass(frozen=True)
class Module:
    """One toggleable module and every surface a switch-off must reach -- the
    manifest superset of app.features.Feature (see that class for each field's law)."""

    slug: str
    title: str
    description: str
    setting_key: str
    beta: bool
    # "removed"    -> the middleware 404s its routes (must be default-ON: a kill switch)
    # "writes_off" -> reads stay up; existing service-layer write gates are authoritative
    # "opt_in"     -> DEFAULT-OFF; the flag starts/stops the feature but never 404s a
    #                 surface (its own controls stay reachable), so nothing is "removed"
    semantics: str
    web: tuple[str, ...] = ()
    api: tuple[str, ...] = ()
    hooks: tuple[str, ...] = ()
    nav_hrefs: tuple[str, ...] = ()
    #: True when base.html ALREADY wraps this feature's nav in its own gate today, so
    #: a switch-off really removes the nav item. Wave 2 makes this uniform (every nav
    #: item wrapped in the one `feature_enabled(slug)` global) and flips the rest True;
    #: the live sweep (verify_features check 7) only asserts nav-absence where it holds.
    nav_gated: bool = False
    cli_groups: tuple[str, ...] = ()
    #: A written reason when `cli_groups` is empty (a declared parity hole, not a
    #: hidden one) — check 4 requires it.
    cli_hole_reason: str = ""
    #: The EXACT refusal the in-process CLI dispatch gate raises when this feature is
    #: off, and its exit code -- both BYTE-IDENTICAL to the feature's existing per-verb
    #: (layer-2) gate, so layer 1 is indistinguishable from layer 2 (the behavior-neutral
    #: claim). Blank falls back to the lowercase "<title> is disabled (setting <key>=0)."
    #: shape used by the layer-2 gates that have no message of their own. cli_exit_code
    #: defaults to EXIT_FAIL (1); assist uses EXIT_USAGE (2) because cmd_ask.py:43 does.
    cli_refusal: str = ""
    cli_exit_code: int = 1  # EXIT_FAIL; 2 == EXIT_USAGE (assist)
    mcp_gate: str | None = None
    cards: tuple[str, ...] = ()
    #: The existing per-module Jinja global this feature's `feature_enabled(slug)`
    #: call replaces in Wave 2; kept as an alias for one release. None = the nav/
    #: template surface is not yet gated by a per-feature global at all.
    legacy_global: str | None = None
    seams: tuple[str, ...] = ()
    #: ffadmin Wave 3 (STRUCTURAL) -- the dotted service modules that ARE this feature's
    #: backend. The cross-feature reach guard (verify_features check 11) statically walks
    #: app/**.py for every module OUTSIDE `own_files` that IMPORTS one of these and
    #: requires it to be classified in `cross_reach_allow` with a reason -- so a NEW caller
    #: (the HIGH-1 lesson: projects.py mutated vendor links with the feature off, reached by
    #: import, invisible to a URL sweep) cannot silently reach a feature's guts unclassified.
    service_modules: tuple[str, ...] = ()
    #: Repo-relative (posix) files that constitute the feature ITSELF -- references from
    #: these into `service_modules` are intra-feature, never flagged as cross-feature.
    own_files: tuple[str, ...] = ()
    #: Classified cross-feature callers as ((relpath, one-line reason), ...). Every module
    #: outside `own_files` importing a service_module MUST appear here, or check 11 goes red
    #: naming file:line. A stale entry (no longer importing) also fails, so it stays honest.
    cross_reach_allow: tuple[tuple[str, str], ...] = ()


# ============================================================================ #
# THE MANIFEST TABLE -- the 11 optional modules (each carrying its Feature fields
# verbatim). A GROWING table (S3 item 2): a new optional module is a new Module(...) row.
# ============================================================================ #
MODULES: tuple[Module, ...] = (
    Module(
        slug="images",
        title="Image Localization",
        description=(
            "Localize images: OCR the text regions, translate them, and re-render "
            "the image per locale."
        ),
        setting_key="image_localization_enabled",
        beta=True,
        semantics="removed",
        web=("/images",),
        api=("/api/images",),
        nav_hrefs=("/images",),
        nav_gated=True,
        cli_groups=("image",),
        # Byte-identical to cmd_image.py:_flag_gate (layer 2).
        cli_refusal="image localization is disabled (setting image_localization_enabled=0).",
        mcp_gate="image_localization_enabled",
        cards=("images",),
        legacy_global="image_localization_enabled",
        seams=(
            "image_localization: the generic-upload ingest refuses a new image "
            "in the image filter when off (app/services/image_localization.py).",
        ),
    ),
    Module(
        slug="keys",
        title="Key-based Projects",
        description=(
            "String-key projects and the Delta API — the continuous-localization "
            "loop (key grid, cursor+tombstone /changes feed)."
        ),
        setting_key="key_projects_enabled",
        beta=True,
        semantics="removed",
        web=("/projects/*/keys",),
        api=("/api/keys", "/api/projects/*/keys", "/api/projects/*/changes"),
        # No standalone nav item — the affordances are project-type template
        # conditionals (the create-form type selector + the "Keys" project badge),
        # which are template-level and out of the middleware's reach.
        nav_hrefs=(),
        nav_gated=False,
        cli_groups=("keys",),
        # Byte-identical to cmd_keys.py:_flag_gate (layer 2).
        cli_refusal="key-based projects are disabled (setting key_projects_enabled=0).",
        mcp_gate="key_projects_enabled",
        cards=("keys",),
        legacy_global="key_projects_enabled",
        seams=(
            "keys: content.project_create refuses project_type='key_based' when "
            "off (app/services/content.py); a retention sweep never ages an "
            "internal key document out.",
        ),
    ),
    Module(
        slug="key_images",
        title="Image translation context",
        description=(
            "Per-string in-context images: attach a screenshot to a string key so a "
            "vision-capable engine sees it as context when translating that string."
        ),
        setting_key="key_image_context_enabled",
        beta=True,
        semantics="removed",
        # The SPECIFIC */images sub-tree, so an off-feature 404s only the image routes
        # while the parent keys grid stays up. specificity 5 (web) / 4 (api) strictly
        # out-specifies the `keys` module's /projects/*/keys (3) and /api/keys (2), so
        # features.api_owner returns a unique winner (no tie -> verify_features green).
        web=("/projects/*/keys/*/images",),
        api=("/api/keys/*/images",),
        # No standalone nav item -- the affordances are the Strings grid image panel +
        # the editor gallery (template-level, out of the middleware's reach), like keys.
        nav_hrefs=(),
        nav_gated=False,
        # The CLI lives UNDER the `keys` group's `image` subcommand (MODULE_CLI
        # ['key_images'] rows), not a standalone top-level group -- per-string images are
        # a facet of key-based projects, so they stay under `loc-tms keys image`.
        cli_groups=(),
        cli_hole_reason=(
            "CLI lives under the `keys` group's `image` subcommand (`keys image "
            "add/list/rm/set-primary`, filed under MODULE_CLI['keys']), not a standalone "
            "group -- per-string images are a facet of key-based projects."
        ),
        mcp_gate="key_image_context_enabled",
        cards=(),
        legacy_global=None,
        seams=(
            "key_images: the orchestrator threads NO image bytes when off "
            "(app/services/orchestrator.py, Train T2); attach/list/delete/set-primary "
            "404 on web + /api + MCP, and `keys image` CLI verbs refuse.",
        ),
    ),
    Module(
        slug="evaluations",
        title="Evaluations Lab",
        description=(
            "The quality sandbox: datasets x candidates x scorers, compared with "
            "statistical honesty, with one promotion door back into production."
        ),
        setting_key="evaluations_enabled",
        beta=False,
        semantics="writes_off",
        web=("/evaluations",),
        api=("/api/evaluations",),
        nav_hrefs=("/evaluations",),
        nav_gated=True,
        cli_groups=("eval",),
        mcp_gate="evaluations_enabled",
        cards=("evaluations",),
        legacy_global="evaluations_enabled",
        seams=(
            "evaluations: every WRITE entry point in app/services/evaluations.py "
            "raises EvaluationsDisabled when off; reads stay up on purpose.",
        ),
    ),
    Module(
        slug="llmemory",
        title="LLMemory (Memories)",
        description=(
            "The self-improving-context module: capture, distill, and recall "
            "translation memories that steer future prompts."
        ),
        setting_key="llmemory_enabled",
        beta=False,
        semantics="writes_off",
        web=("/memories",),
        api=("/api/memories",),
        nav_hrefs=("/memories",),
        # base.html does NOT yet wrap the /memories nav in the switch (Wave 2 does),
        # so the live sweep must not assert its nav vanishes yet.
        nav_gated=False,
        cli_groups=("memory",),
        mcp_gate="llmemory_enabled",
        cards=("memories",),
        legacy_global=None,
        seams=(
            "llmemory: capture writes no MemoryEvent and recall returns the empty "
            "result when off (app/services/llmemory.py); reads stay up on purpose. "
            "The lifecycle WRITES are gated too: add_note, distill, invalidate and "
            "invalidate_since all no-op the memory mutation when off (the web distill "
            "route still writes a zero-count audit row -- a pre-existing deferred F-LOW). "
            "INTENTIONAL EXCEPTION: purge stays reachable when off -- it is the GDPR "
            "erasure path, which an operator must be able to run against already-"
            "captured data even after switching the module off.",
        ),
    ),
    Module(
        slug="llmemory_suggestions",
        title="Workflow suggestions",
        description=(
            "The LLMemory workflow-suggestion queue: deterministically-drafted proposals "
            "to change a workflow's routing/step-order/config, reviewed and applied by a "
            "human -- never automatically."
        ),
        setting_key="llmemory_suggestions_enabled",
        beta=True,
        # opt_in (NOT removed): the flag is DEFAULT-OFF, and a default-off "removed" flag is
        # red by construction (verify_features: "removed but DEFAULT-OFF"). OFF = generation
        # inert and every WRITE verb refuses NAMING the switch (the writes-off shape), while
        # the surface's own controls (read-only list/detail) stay reachable, so nothing is
        # "removed" and registry.GATE_404_STYLE does NOT gain this key.
        semantics="opt_in",
        web=("/memory-suggestions",),
        api=("/api/memory-suggestions",),
        nav_hrefs=("/memory-suggestions",),
        nav_gated=True,
        # The CLI leaves live UNDER the `memory` group's `suggestions` subcommand
        # (MODULE_CLI['llmemory']), not a standalone group -- so this module declares no
        # cli_group and records the reason (check 4 requires a non-empty cli_hole_reason).
        cli_groups=(),
        cli_hole_reason=(
            "CLI lives under the `memory` group's `suggestions` subcommand "
            "(MODULE_CLI['llmemory'] rows), not a standalone group -- the queue is a facet "
            "of the LLMemory module, so `loc-tms memory suggestions ...` keeps it there."
        ),
        mcp_gate="llmemory_suggestions_enabled",
        cards=(),
        legacy_global=None,
        # No service_modules declared (the databricks/scim/saml precedent): the suggestion
        # services are reached ONLY through this module's own gated surfaces + the GDPR purge
        # late-import hook (a declared kill-switch exception). check 11 does not apply.
        seams=(
            "llmemory_suggestions: generation writes no MemorySuggestion and every WRITE verb "
            "(generate/approve/reject/apply/retry) refuses naming the switch when off; the "
            "read-only list/detail stay up (the opt_in shape). APPLY is REMOTE_IMMUTABLE -- "
            "its /api route requires the R3 three-lock (remote_admin_enabled) and no MCP tool "
            "may ever apply a workflow change. INTENTIONAL EXCEPTION: purge_suggestions_for "
            "(the GDPR erasure cascade the llmemory purge path calls) is NOT gated on this "
            "flag -- erasure must run with the module switched off, mirroring llmemory.purge.",
        ),
    ),
    Module(
        slug="assist",
        title="Ask + Draft assistant",
        description=(
            'The grounded module assistant ("Ask about this page") and the '
            "describe-what-you-want drafter. Read-only answers; drafts never write."
        ),
        setting_key="assist_enabled",
        beta=False,
        semantics="removed",
        # /assist/ask (the model call). The module-nested draft surfaces (/memories/draft,
        # /evaluations/draft) ship with their add-ons.
        web=("/assist",),
        api=(),
        # No nav item (Ask/Draft are embedded panels).
        nav_hrefs=(),
        nav_gated=False,
        cli_groups=("ask",),
        # Byte-identical to cmd_ask.py:43 (layer 2), INCLUDING its EXIT_USAGE (2).
        cli_refusal="Ask and Draft are switched off for this install "
        "(config set assist_enabled 1).",
        cli_exit_code=2,
        mcp_gate="assist_enabled",
        cards=(),
        legacy_global="assist_enabled",
        seams=(
            "assist: read-only (grounds only on the shipped capability cards) and "
            "draft never writes — its submit is the module's own already-gated seam.",
        ),
    ),
    Module(
        slug="prompt_tap",
        title="LLM Ledger",
        description=(
            "The opt-in observer that records the exact request payload each "
            "completion call sends, for prompt tuning."
        ),
        setting_key="prompt_tap_enabled",
        beta=False,
        # opt_in, NOT removed: prompt_tap_enabled is DEFAULT-OFF and is a CAPTURE switch,
        # not a page kill switch. The /prompt-tap page is admin-only and stays reachable
        # whatever the flag says -- it IS the toggle UI -- so nothing is "removed" when
        # off. Declaring it "removed" would (a) make the middleware 404 /prompt-tap on a
        # default install (default-off), breaking the way you enable it, and (b) print a
        # false "removes it from the app" sentence on the Settings card. See §8/§MEDIUM-1.
        semantics="opt_in",
        web=("/prompt-tap",),
        api=(),
        nav_hrefs=("/prompt-tap",),
        nav_gated=False,
        cli_groups=("prompt-tap", "ledger"),
        mcp_gate="prompt_tap_enabled",
        cards=(),
        legacy_global=None,
        seams=(
            "prompt_tap: the completion tap records nothing when off "
            "(app/services/prompt_tap.py); the /prompt-tap page is the toggle UI.",
        ),
    ),
    Module(
        slug="utcp",
        title="UTCP manual",
        description=(
            "The read-only UTCP discovery manual at GET /utcp — a machine-readable "
            "subset of the /api surface, generated from the OpenAPI schema."
        ),
        setting_key="utcp_manual_enabled",
        beta=False,
        semantics="removed",
        web=("/utcp",),
        api=(),
        nav_hrefs=(),
        nav_gated=False,
        cli_groups=(),
        cli_hole_reason=(
            "UTCP is a machine-readable discovery document served at GET /utcp, the "
            "peer of /openapi.json; there is no operator action to expose on the CLI "
            "(it is generated, not authored)."
        ),
        mcp_gate="utcp_manual_enabled",
        cards=(),
        legacy_global=None,
        seams=(),
    ),
    Module(
        slug="vendors",
        title="Vendors",
        description=(
            "First-class external translation/review vendors: dispatch-and-wait "
            "workflow steps, signed return callbacks, MSA rate cards, and the "
            "quote/approval + reject/rework lifecycle."
        ),
        setting_key="vendors_enabled",
        beta=False,
        semantics="removed",
        # The operator-admin surface (/vendors) AND the in-app Vendor Studio SSO handoff
        # (/vendor-studio/open). The EXTERNAL Vendor Studio Fly app is out of scope -- only
        # the in-app handoff route is gated here. The Customer-360 link/unlink-vendor
        # actions live on the CORE /customers path (projects.py) but are vendor affordances
        # -- they already 404 when off via the shared service seam (F-MED-5); declaring the
        # two POST patterns here brings them under verify_features' sweep and 404s them at
        # the middleware too (each is 3 segments, so it never catches core /customers or
        # /customers/{id}).
        web=(
            "/vendors",
            "/vendor-studio",
            "/customers/*/link-vendor",
            "/customers/*/unlink-vendor",
        ),
        api=("/api/vendors",),
        # The two SIGNED inbound vendor webhooks. In `hooks`, so the middleware 404s them
        # BEFORE any signature work when off (the webhook invariant strengthened, plan §7);
        # a /webhooks/ error goes out as JSON (deps._error_page's /webhooks/ branch).
        hooks=("/webhooks/vendor-returns", "/webhooks/vendor-quotes"),
        nav_hrefs=("/vendors",),
        nav_gated=True,
        cli_groups=("vendor",),
        # Byte-identical to cmd_vendor.py:_flag_gate (layer 2); default EXIT_FAIL like
        # cmd_image's gate (verify_features check 1b pins message + code against it).
        cli_refusal="vendors are disabled (setting vendors_enabled=0).",
        mcp_gate="vendors_enabled",
        cards=("vendors",),
        legacy_global=None,
        seams=(
            "vendors: the egress SEND seams refuse when off, naming the switch, so no "
            "new vendor work leaves the process -- vendor_dispatch.dispatch (the native "
            "wire + manual leg send, reached by dispatch_external_step and retry_leg) "
            "and vendor_dispatch.post_signed_message (the quote-decision notice) raise "
            "ConnectorError; a run hitting an external step FAILS with a readable reason "
            "rather than sending. dispatch_external_step refuses at the TOP (before any "
            "vendor bookkeeping) so a refusal never marks a healthy vendor ERROR. The "
            "vendors.link_customer/unlink_customer mutations refuse at the service seam "
            "(HIGH-1), and workflows._validate_step_vendor refuses external-step "
            "AUTHORING (MEDIUM-2). The scheduler's two vendor NETWORK sweeps "
            "(vendor_return.poll_due_legs + vendor_dispatch.retry_due_legs) skip when "
            "off; the DB-only vendor_dispatch.sweep_expired lifecycle sweep keeps "
            "running (app/services/scheduler.py).",
        ),
        # STRUCTURAL: the vendor service modules, the files that ARE the feature, and every
        # classified cross-feature caller (verify_features check 11 enforces completeness).
        service_modules=(
            "app.services.vendors",
            "app.services.vendor_dispatch",
            "app.services.vendor_return",
            "app.services.vendor_cost",
            "app.services.vendor_quote",
            "app.services.vendor_rework",
            "app.services.vendor_spend",
            "app.services.vendor_studio_sso",
            "app.services.vendor_xliff",
        ),
        own_files=(
            "app/services/vendors.py",
            "app/services/vendor_dispatch.py",
            "app/services/vendor_dispatch_hub.py",
            "app/services/vendor_dispatch_hub2.py",
            "app/services/vendor_dispatch_common.py",
            "app/services/vendor_dispatch_part2.py",
            "app/services/vendor_return.py",
            "app/services/vendor_return_hub.py",
            "app/services/vendor_return_common.py",
            "app/services/vendor_return_part2.py",
            "app/services/vendor_cost.py",
            "app/services/vendor_quote.py",
            "app/services/vendor_rework.py",
            "app/services/vendor_spend.py",
            "app/services/vendor_studio_sso.py",
            "app/services/vendor_xliff.py",
            "app/web/vendors.py",
            "app/cli/cmd_vendor.py",
            "app/cli/vendor_common.py",
            "app/cli/vendor_jobs.py",
        ),
        cross_reach_allow=(
            (
                "app/web/api_cli_vendors.py",  # SRP split 2026-09-14
                "the shared /api+CLI surface; every /api/vendors route is gated first-line by "
                "deps.feature_api_block. ALSO reads vendor_cost's 7-band leverage SEAM for "
                "GET /api/projects/{id}/leverage (CV-1) — a pure classification over persisted "
                "Segment fields, deliberately NOT vendor-gated (works with vendors off).",
            ),
            (
                "app/web/api_cli.py",  # SRP split 2026-09-14
                "the shared /api+CLI surface; every /api/vendors route is gated first-line by "
                "deps.feature_api_block. ALSO reads vendor_cost's 7-band leverage SEAM for "
                "GET /api/projects/{id}/leverage (CV-1) — a pure classification over persisted "
                "Segment fields, deliberately NOT vendor-gated (works with vendors off).",
            ),
            (
                "app/web/api_cli_projects_1.py",  # SRP split 2026-09-14 (shim/piece importer)
                "the shared /api+CLI surface; every /api/vendors route is gated first-line by "
                "deps.feature_api_block. ALSO reads vendor_cost's 7-band leverage SEAM for "
                "GET /api/projects/{id}/leverage (CV-1) — a pure classification over persisted "
                "Segment fields, deliberately NOT vendor-gated (works with vendors off).",
            ),
            (
                "app/web/api_cli_misc_3.py",  # SRP split 2026-09-14 (shim/piece importer)
                "the shared /api+CLI surface; every /api/vendors route is gated first-line by "
                "deps.feature_api_block. ALSO reads vendor_cost's 7-band leverage SEAM for "
                "GET /api/projects/{id}/leverage (CV-1) — a pure classification over persisted "
                "Segment fields, deliberately NOT vendor-gated (works with vendors off).",
            ),
            (
                "app/web/api_discovery_common.py",  # SRP split 2026-09-14
                "the two signed vendor webhooks; gated first-line by deps.feature_block and "
                "404'd pre-signature by the middleware (Feature.hooks).",
            ),
            (
                "app/web/projects.py",
                "the Customer-360 connected-vendors link/unlink; gated first-line by "
                "feature_block AND by the vendors.link/unlink_customer service seam (HIGH-1).",
            ),
            (
                "app/web/projects_projects.py",  # SRP split 2026-09-14
                "the Customer-360 connected-vendors link/unlink; gated first-line by "
                "feature_block AND by the vendors.link/unlink_customer service seam (HIGH-1).",
            ),
            (
                "app/web/workflows.py",
                "workflow web routes render the vendor picker + save external steps; authoring "
                "refused at workflows._validate_step_vendor when off, the picker template-gated.",
            ),
            (
                "app/web/documents.py",
                "reads vendor_dispatch.leg_is_wireless to offer the wireless hand-off; the live "
                "links are template-gated on feature_enabled('vendors') (read-only).",
            ),
            (
                "app/cli/cmd_workflow_part3_leaf.py",  # SRP split 2026-09-14
                "workflow add-step authoring reads vendor_dispatch (protocol names); vendor "
                "step authoring refused at the workflows seam when off.",
            ),
            (
                "app/services/workflows_part2.py",  # SRP split 2026-09-14
                "external-step vendor validation; authoring refused at _validate_step_vendor "
                "when off (MEDIUM-2).",
            ),
            (
                "app/services/orchestrator.py",
                "runtime egress via dispatch_external_step; refuses at the seam before any "
                "vendor bookkeeping when off (MEDIUM-3).",
            ),
            (
                "app/services/orchestrator_part6.py",  # SRP split 2026-09-14
                "runtime egress via dispatch_external_step; refuses at the seam before any "
                "vendor bookkeeping when off (MEDIUM-3).",
            ),
            (
                "app/services/orchestrator_part9.py",  # SRP split 2026-09-14
                "runtime egress via dispatch_external_step; refuses at the seam before any "
                "vendor bookkeeping when off (MEDIUM-3).",
            ),
            (
                "app/services/scheduler.py",
                "the two vendor NETWORK sweeps (poll/retry); skipped via features.is_enabled("
                "'vendors') when off.",
            ),
            (
                "app/services/reports_part3.py",  # SRP split 2026-09-14
                "reporting READS vendor_spend.spend_by_run (read-only forensic aggregation); "
                "no egress, no mutation.",
            ),
            (
                "app/services/project_budgets.py",
                "CV-2 per-project money budget READS vendor_spend.leg_human_cost to count "
                "SETTLED vendor human spend against a project's cap (read-only aggregation, "
                "no egress); the settled-human leg simply contributes $0 when vendors is off.",
            ),
            (
                "app/services/tap_enrich.py",
                "prompt-tap capture READS vendor_return/vendor_spend summaries for context "
                "(read-only).",
            ),
            (
                "app/services/tap_enrich_part1.py",  # SRP split 2026-09-14
                "prompt-tap capture READS vendor_return/vendor_spend summaries for context "
                "(read-only).",
            ),
            (
                "app/services/tap_view.py",
                "prompt-tap view READS vendor_return.leg_to_dict summaries (read-only).",
            ),
            (
                "app/filters/xliff_filter.py",
                "reuses vendor_xliff's hardened XLIFF PARSE seam so the two parse paths cannot "
                "drift; pure format code, no vendor egress.",
            ),
            (
                "app/web/queries.py",
                "CV-1 project page reads vendor_cost's 7-band leverage SEAM "
                "(leverage_over_segments/estimate_band_rows) — a PURE classification over "
                "persisted Segment fields (tm_match_pct/lock_band/source), no dispatch; works "
                "with vendors_enabled=0 (verified).",
            ),
            (
                "app/cli/cmd_project_part2.py",  # SRP split 2026-09-14
                "the `project leverage` verb reads vendor_cost's 7-band leverage SEAM "
                "(project_leverage/estimate_band_rows/project_has_key_docs) — a PURE "
                "classification over persisted Segment fields, no dispatch; works with "
                "vendors_enabled=0 (verified).",
            ),
        ),
    ),
    Module(
        slug="reports",
        title="Reports",
        description=(
            "Operational cost / token / volume / qa / postedit reporting: the "
            "pipeline-health dashboard and the report builder (grouped tables + CSV "
            "export), over the data the caller can see. qa is the MQM LQA "
            "dimension-by-severity cross-tab; postedit is edit distance and "
            "post-editing effort."
        ),
        setting_key="reports_enabled",
        beta=False,
        semantics="removed",
        # The reports page + the builder's GET /reports/run (a read that renders a table
        # or streams a CSV) both nest under /reports and 404 together when off.
        web=("/reports",),
        api=("/api/reports",),
        nav_hrefs=("/reports",),
        nav_gated=True,
        cli_groups=("report",),
        # Byte-identical to cmd_report.py:_flag_gate (layer 2); default EXIT_FAIL like
        # cmd_vendor/cmd_image (verify_features check 1b pins message + code against it).
        cli_refusal="reports are disabled (setting reports_enabled=0).",
        mcp_gate="reports_enabled",
        # Reports owns its capability card (app/capabilities/reports.md); the card
        # 404s on /help + /api/capabilities when this feature is off (check 7).
        cards=("reports",),
        legacy_global=None,
        # No non-URL seam: reports is a pure READ/aggregation leaf reached ONLY through its
        # own gated web/api/cli surfaces -- no scheduler sweep, no egress, no mutation. It
        # READS other modules' data for the aggregate (e.g. vendor_spend.spend_by_run), but
        # as the CONSUMER; that reverse edge is classified in the vendors feature's
        # cross_reach_allow, not here, and disabling Reports never touches a data source.
        # It therefore declares no `service_modules` (check 11 does not apply): nothing
        # OUTSIDE reports mutates or drives reports' guts with the feature off (no HIGH-1
        # shape to guard), so gating its three surfaces is complete.
        seams=(),
    ),
    Module(
        slug="routing",
        title="Content routing",
        description=(
            "Inbound content-routing RULES: match a submission by file extension / "
            "subject / department and pick its project, workflow, language set and "
            "provider. Rule authoring + read-back; the runtime routing decision is "
            "part of the core inbound pipeline."
        ),
        setting_key="routing_enabled",
        beta=False,
        semantics="removed",
        # The /routing admin page (rule list + create + toggle + delete). DEPARTMENT
        # BUDGETS nest under /routing (/routing/budgets*) but are NOT part of this feature
        # -- CORE_WEB carves them back out by longest match (see CORE_WEB below), so the
        # middleware 404s /routing while /routing/budgets stays reachable when off.
        web=("/routing",),
        # Only the routing-RULE endpoints. /api/budgets is a sibling (NOT nested under
        # /api/routing), so it needs no carve-out -- it simply stays in CORE_API.
        api=("/api/routing",),
        nav_hrefs=("/routing",),
        nav_gated=True,
        # Wave 4 BUILDS the top-level `routing` CLI group (cmd_routing.py) -- the routing
        # feature's own gated twin of the web surface, closing the pre-existing parity hole
        # (the routing-rule CRUD previously lived ONLY under the un-gateable `admin` spine,
        # which a feature may never claim). `admin routing rule ...` stays as the admin
        # twin; this group is the feature surface the switch removes.
        cli_groups=("routing",),
        # Byte-identical to cmd_routing.py:_flag_gate (layer 2); default EXIT_FAIL.
        cli_refusal="content routing is disabled (setting routing_enabled=0).",
        mcp_gate="routing_enabled",
        # Routing owns its capability card (app/capabilities/routing.md); the card
        # 404s on /help + /api/capabilities when this feature is off (check 7).
        cards=("routing",),
        legacy_global=None,
        # No `service_modules` (check 11 does not apply): every routing-rule MUTATION
        # (create/toggle/delete) lives on the gated web/api/cli surfaces above, so there is
        # no import-level seam that mutates rules with the feature off (no HIGH-1 shape).
        # The runtime routing DECISION (routing.resolve / department_for /
        # ensure_department_inbox, consumed by the core inbound pipeline in app/web/api.py)
        # is deliberately NOT gated -- disabling the RULE-AUTHORING surface must not break
        # inbound routing that existing rules already drive (with no rules, resolve returns
        # the default). Gating the three authoring surfaces is the complete switch.
        seams=(),
    ),
    Module(
        slug="databricks",
        title="Databricks export",
        description=(
            "The analytical exporter: ship post-edit pairs (training data), step runs + "
            "spend, LQA findings, segments and TM growth to a customer's Databricks "
            "lakehouse. Dormant, admin-gated, read-only, host-only (a data-egress switch)."
        ),
        setting_key="databricks_export_enabled",
        cli_refusal=(
            "the Databricks exporter is disabled; enable databricks_export_enabled on the host."
        ),
        beta=True,
        # opt_in, NOT removed: databricks_export_enabled is DEFAULT-OFF (a data-egress
        # switch, off until an operator opts in). check 1 REJECTS removed+default-off
        # (verify_features.py:170-173), and path_is_off skips default-off, so a "removed"
        # declaration would both fail check 1 and never produce a before-auth 404.
        # Non-enumerability of /api/admin/databricks/* rests ENTIRELY on the after-auth
        # _databricks_gate (api_cli.py) -- ONE layer, byte-for-byte the R3 posture. The
        # key is therefore DELIBERATELY NOT in registry.GATE_404_STYLE (check 5 derives
        # that set from removed-features + remote_admin_enabled; adding an opt_in key goes
        # RED).
        semantics="opt_in",
        web=("/admin/databricks",),
        api=("/api/admin/databricks",),
        # No standalone nav item -- the section is reached from the admin area / a direct
        # URL, and feature_block 404s it when off (non-enumerable). No nav sweep applies to
        # an opt_in feature, so nav_hrefs stays empty.
        nav_hrefs=(),
        nav_gated=False,
        cli_groups=("databricks",),
        # cli_refusal is the verb-level refusal text (#533): opt_in features are NOT swept by
        # the __main__ dispatch gate (which only refuses removed+default-on), so
        # cmd_databricks._flag_gate (-> _common.feature_gate) is the CLI kill switch and
        # verify_features check 1b pins it to this string.
        mcp_gate="databricks_export_enabled",
        cards=(),
        legacy_global=None,
        # The exporter is reached ONLY through its own gated web/api/cli surfaces (a manual
        # batch service -- no scheduler sweep, no import-level mutation seam, no HIGH-1
        # shape), so it declares no service_modules (check 11 does not apply) and no
        # non-URL seam. The transport is egress, but it fires only inside run_export, which
        # is gated on every surface above; a default install never reaches it.
        seams=(),
    ),
    Module(
        slug="queries",
        title="Queries",
        description=(
            "Threaded queries and comments on a project, document, segment or key — "
            "with the vendor round trip and the reviewer worklist's query feed."
        ),
        setting_key="queries_enabled",
        beta=False,
        # A kill switch (2026-08-19, first-class): "removed" 404s every surface
        # and is REQUIRED default-ON (features.py:129-132 / verify_features.py:170-171).
        semantics="removed",
        # "/queries" covers /queries, /queries/{id} and the four POST subpaths (segment
        # tail-match); the middleware 404s them all when off (layer 1).
        web=("/queries",),
        api=("/api/queries",),
        nav_hrefs=("/queries",),
        nav_gated=True,
        cli_groups=("query",),
        # Byte-identical to cmd_query.py:_flag_gate (layer 2); verify_features check 1b
        # pins message + code against it.
        cli_refusal="queries are disabled (setting queries_enabled=0).",
        cli_exit_code=1,
        mcp_gate="queries_enabled",
        cards=("queries",),
        legacy_global=None,  # a NEW module has no legacy global to alias
        service_modules=("app.services.query_threads",),
        own_files=(
            "app/services/query_threads.py",
            "app/web/query_threads.py",
            "app/cli/cmd_query.py",
        ),
        # NEW-1: Train B ships ONLY the row for the import IT adds — notifications.py
        # (the §4.6 open/post/close mails). The documents/worklist/vendor rows land with
        # C/D/E when THEY add their imports, or check 11 goes red (missing-file /
        # stale-entry) in the declaring train's OWN gate.
        cross_reach_allow=(
            (
                "app/web/api_cli.py",
                "the shared /api+CLI surface; every /api/queries route is gated first-line by "
                "deps.feature_api_block, and RBAC is the query_threads service's job (the §4.5 "
                "tenant ring + role rules) on the key's User.",
            ),
            (
                "app/web/api_cli_misc_6.py",  # SRP split 2026-09-14 (piece of app/web/api_cli.py)
                "the shared /api+CLI surface; every /api/queries route is gated first-line by "
                "deps.feature_api_block, and RBAC is the query_threads service's job (the §4.5 "
                "tenant ring + role rules) on the key's User.",
            ),
            (
                "app/services/notifications.py",
                "query open/post/close mails (§4.6) reuse query_threads.flatten_header_text "
                "for the email-header sanitizer + read thread/post rows; no egress beyond "
                "the existing SMTP channel.",
            ),
            (
                "app/services/vendor_return_hub.py",  # SRP split 2026-09-15: the caller moved
                "Train E: a returned vendor comment calls query_threads.record_vendor_post "
                "to land on (or open) the buyer thread; the reply is scoped to the leg's own "
                "document, admitted inside the return's savepoint, cap-refusals reported.",
            ),
        ),
        seams=(
            "queries: the vendor dispatch package omits the `queries` block and the "
            "vendor return SKIPS comment admission (silently, never an error — a "
            "vendor must not be refused because the buyer turned a feature off); "
            "notification sends stop; the worklist's query section disappears while "
            "its other sections survive; existing threads stay in the database, "
            "dormant and invisible, and re-enabling restores them unchanged. Data "
            "lifecycle (cascade deletes + GDPR export/erase) is NOT feature-gated — "
            "it reaches the tables through app/models, not this service (§5.2's "
            "fourth seam).",
        ),
    ),
    Module(
        slug="worklist",
        title="My work",
        description=(
            "The cross-project reviewer worklist: paused review steps that name you, "
            "the queries assigned to you (and your own answered ones), and the documents "
            "in your scope needing quality attention — one page you visit, not a "
            "notification centre."
        ),
        setting_key="worklist_enabled",
        beta=False,
        # A kill switch (2026-08-19, §5): "removed" 404s every surface and is
        # REQUIRED default-ON (features.py:129-132 / verify_features.py:170-171).
        semantics="removed",
        # "/my-work" is the single page; the middleware 404s it when off (layer 1).
        web=("/my-work",),
        api=("/api/worklist",),
        nav_hrefs=("/my-work",),
        nav_gated=True,
        cli_groups=("work",),
        # Byte-identical to cmd_work.py:_flag_gate (layer 2); verify_features check 1b
        # pins message + code against it.
        cli_refusal="the reviewer worklist is disabled (setting worklist_enabled=0).",
        cli_exit_code=1,
        mcp_gate="worklist_enabled",
        cards=("worklist",),
        legacy_global=None,  # a NEW module has no legacy global to alias
        service_modules=("app.services.worklist",),
        own_files=("app/services/worklist.py", "app/web/worklist.py", "app/cli/cmd_work.py"),
        # check 11 (HIGH-1): every file OUTSIDE own_files that imports the worklist service
        # is classified here. api_cli.py hosts GET /api/worklist; web/queries.py reads the
        # dashboard's open-query count from the same service. Both are thin READ callers —
        # no mutation, no egress, no reach past the service's own scoping ring.
        cross_reach_allow=(
            (
                "app/web/api_cli.py",
                "the shared /api surface: GET /api/worklist relays worklist.my_work; "
                "identity is the key's user and the scoping ring is the service's job.",
            ),
            (
                "app/web/api_cli_misc_6.py",  # SRP split 2026-09-14 (piece of app/web/api_cli.py)
                "the shared /api surface: GET /api/worklist relays worklist.my_work; "
                "identity is the key's user and the scoping ring is the service's job.",
            ),
            (
                "app/web/queries.py",
                "the dashboard's 'N queries assigned to you' line reads "
                "worklist.my_open_query_count (§8.4/RV-1), gated on queries_enabled and "
                "scoped to the caller's visible projects — a pure read.",
            ),
        ),
        # A pure READ/aggregation leaf, reached ONLY through its own gated web/api/cli
        # surfaces (the reports argument at modules.py:429-437): no scheduler sweep, no
        # egress, no mutation. The query SECTION disappears when queries_enabled is off
        # (worklist.my_work drops it — §5.2), but that is a read-visibility choice inside
        # this leaf, not a non-URL seam of its own.
        seams=(),
    ),
    Module(
        slug="scim",
        title="SCIM provisioning",
        description=(
            "SCIM 2.0 directory provisioning (Okta / Entra): an IdP creates, updates, "
            "deactivates and groups user accounts through the /scim/v2 endpoint. "
            "Off by default; host-only switch."
        ),
        setting_key="scim_enabled",
        beta=True,
        # opt_in, NOT removed: scim_enabled is DEFAULT-OFF (a provisioning-surface switch,
        # off until an operator opts in). Same shape as databricks -- check 1 REJECTS
        # removed+default-off, and path_is_off skips default-off, so a "removed" declaration
        # would both fail check 1 and never produce a before-auth 404. Non-enumerability of
        # /scim/v2/* rests ENTIRELY on the router's own scim.enabled() guard (a SCIM-shaped
        # 404), so the key is DELIBERATELY NOT in registry.GATE_404_STYLE (an opt_in key is
        # not in that derived set -- verify_features check 5).
        semantics="opt_in",
        # /scim covers every /scim/v2/* path (segment tail-match). These are WEB-group
        # routes (not /api-prefixed), so no /api accounting entry -- api stays ().
        web=("/scim",),
        api=(),
        # No standalone nav item -- the section is a machine endpoint reached by the IdP,
        # not a page a human browses. The read-only Groups view + SCIM badge live on the
        # existing Users admin page. No nav sweep applies to an opt_in feature.
        nav_hrefs=(),
        nav_gated=False,
        cli_groups=("scim",),
        # No cli_refusal pinned here: opt_in features are NOT swept by the __main__ dispatch
        # gate (which only refuses removed+default-on features); the `scim` verbs are
        # read-only (status/groups/users) and gate on scim_enabled at the verb.
        mcp_gate="scim_enabled",
        cards=(),
        legacy_global=None,
        # No service_modules declared (the databricks precedent): the SCIM service is
        # reached ONLY through its own gated /scim router + the `scim` CLI + a pure read on
        # the Users admin page (the SCIM badge + read-only Groups list). There is no
        # scheduler sweep, no egress, no import-level mutation seam (no HIGH-1 shape), so
        # check 11 does not apply and there is no non-URL seam to declare.
        seams=(),
    ),
    Module(
        slug="saml",
        title="SAML SSO",
        description=(
            "SAML 2.0 single sign-on (Service Provider): let users sign in through your "
            "SAML identity provider. BETA; off by default; host-only switch. Requires the "
            "opt-in python3-saml extra on the host."
        ),
        setting_key="saml_enabled",
        beta=True,
        # opt_in, NOT removed: saml_enabled is DEFAULT-OFF (a federated-login switch, off
        # until an operator opts in). Same shape as scim/databricks. Non-enumerability of
        # /auth/saml/* rests on each route's own saml.enabled() guard (a 404), so the key
        # is DELIBERATELY NOT in registry.GATE_404_STYLE (an opt_in key is not in that
        # derived set -- verify_features check 5).
        semantics="opt_in",
        # /auth/saml covers /auth/saml/login, /auth/saml/acs and /auth/saml/metadata
        # (segment tail-match). These are WEB-group routes (in the existing auth.router,
        # NOT /api-prefixed), so no /api accounting entry -- api stays (). It does NOT
        # match /auth/login (segment "saml" != "login"), so the anti-lockout law holds.
        web=("/auth/saml",),
        api=(),
        # The SAML button is rendered inline on the login page (gated on saml_enabled),
        # not a nav item; no nav sweep applies to an opt_in feature.
        nav_hrefs=(),
        nav_gated=False,
        # No CLI verbs: SAML is a browser SSO flow (like OIDC, which also has no CLI
        # group) -- there is nothing an operator drives from the shell.
        cli_groups=(),
        cli_hole_reason=(
            "SAML is a browser SSO login flow (the OIDC precedent): the IdP redirect + the "
            "assertion POST have no headless/CLI equivalent, so there is no CLI verb to add. "
            "Configuration is via `config set saml_*` under the always-on config group."
        ),
        mcp_gate="saml_enabled",
        cards=(),
        legacy_global=None,
        # No service_modules declared (the databricks/scim precedent): the SAML service is
        # reached ONLY through its own gated /auth/saml routes + the boot capability check;
        # no scheduler sweep, no egress, no import-level mutation seam. check 11 does not
        # apply and there is no non-URL seam to declare.
        seams=(),
    ),
    Module(
        slug="data_export",
        title="Data Export",
        description=(
            "RBAC-confined full-object-graph export jobs: a self-describing, checksummed ZIP "
            "of JSONL-per-entity plus a manifest, for a scope you can already see (self-serve) "
            "or the whole workspace (admin). Async: POST starts a job, poll, download when ready."
        ),
        setting_key="data_export_enabled",
        beta=True,
        # writes_off (D3/LAW9): the flag gates the WRITE path (job submission + staging) at the
        # service layer; reads / enumeration of already-staged artifacts stay up. DEFAULT-ON, so
        # it is NOT in registry.GATE_404_STYLE (check 5 derives that from removed features), and
        # the middleware never 404s its routes (_sweep_writes_off asserts path_is_off is inert).
        semantics="writes_off",
        web=("/exports",),
        api=("/api/exports",),
        nav_hrefs=("/exports",),
        nav_gated=True,
        cli_groups=("export",),
        # No cli_refusal pinned: writes_off groups are NOT swept by the __main__ dispatch gate
        # (only removed+default-on features are), and check 1b only pins features that DECLARE
        # one. The CLI kill switch is the service-layer ExportRefused (data_export._check_refuse_
        # gates), surfaced by cmd_export as a CliError naming data_export_enabled -- the
        # databricks/llmemory precedent (a non-removed feature declares no byte-pinned refusal).
        # The Feature invariant is mcp_gate == setting_key (verify_modules check 1); no MCP tool
        # actually claims a /api/exports route -- they are all NEVER_COVER (registry.py), a
        # full-graph dump is not an agent tool -- so this gate is never consulted by any tool
        # derivation. It is set to the setting_key only to satisfy that well-formedness invariant.
        mcp_gate="data_export_enabled",
        cards=("data_export",),
        legacy_global=None,  # a NEW module has no legacy global to alias
        service_modules=("app.services.data_export",),
        own_files=(
            "app/services/data_export.py",
            "app/services/data_export_part1.py",
            "app/services/data_export_part2.py",
            "app/services/data_export_part3.py",
            "app/web/exports.py",
            "app/cli/cmd_export.py",
        ),
        # check 11 (HIGH-1): every file OUTSIDE own_files that imports the data_export service is
        # classified here. Train A/B's tree has TWO such importers: jobs.py (Train A's HANDLERS
        # registration) and api_cli.py (the /api/exports routes). Train C ADDS its two importers
        # (scheduler.py's reaper, gdpr.py's erase->purge hook) IN THE SAME COMMIT that adds the
        # imports -- the `queries` NEW-1 per-train precedent, reconciled with check 11's STALE-entry
        # law (a pre-declared row naming a not-yet-importing file goes RED on `set(allow)-used`).
        cross_reach_allow=(
            (
                "app/services/jobs.py",
                "Train A: registers HANDLERS['data_export'] -> data_export._execute_job "
                "(the async job handler that re-resolves confinement from the persisted row).",
            ),
            (
                "app/web/api_cli_misc_7.py",  # SRP split 2026-09-14 (piece of app/web/api_cli.py)
                "Train B: the /api/exports routes (POST start, GET list/status/artifact) relay "
                "data_export.submit_export + read DataExportRun rows; RBAC (_gate/_owns/admin-at-"
                "fetch) is the route's job, scope confinement the service's.",
            ),
            (
                "app/services/scheduler.py",
                "Train C: the staged-artifact + .tmp-orphan retention reaper "
                "(data_export.reap_scheduled_sweep, one guarded call in scheduler.tick).",
            ),
            (
                "app/services/gdpr_common.py",  # SRP split 2026-09-14
                "Train C: erase -> data_export.purge_artifacts_covering (staged-artifact "
                "content-removal; the document/customer erase paths route through gdpr so the "
                "data_export reach stays declared here + scheduler only, DX-207/DX-306).",
            ),
        ),
        seams=(
            "data_export: the writes-off kill switch lives at the service layer -- "
            "run_export/submit_export raise ExportRefused when data_export_enabled=0 (the job "
            "submission + staging path), while reads/enumeration of already-staged artifacts stay "
            "up. On an encrypt_uploads/encrypt_db install a submit is ALSO refused unless "
            "data_export_allow_plaintext_staging=1 or S3 staging is configured (DX-208). The "
            "artifact ownership gate (_owns: actor + resolved-scope tuple, admin-at-fetch for a "
            "workspace dump) is enforced on every /api read, never widened to can_manage.",
        ),
    ),
    Module(
        slug="advanced_workflows",
        title="Advanced Workflows",
        description=(
            "The LLM Localization Harness: a tool-governed agent tier beside the classic "
            "step-list engine. A mission (brief + fail-closed tool allowlist + model roster + "
            "budget + fences) is executed by a deterministic loop that records a full, "
            "auditable transcript of every decision. BETA; off by default; opt-in."
        ),
        setting_key="advanced_workflows_enabled",
        cli_refusal=(
            "Advanced Workflows is disabled; enable advanced_workflows_enabled on the host."
        ),
        beta=True,
        # opt_in, NOT removed: advanced_workflows_enabled is DEFAULT-OFF (a new agent tier, off
        # until an operator opts in). The databricks/scim shape -- check 1 REJECTS
        # removed+default-off, and path_is_off skips default-off, so a "removed" declaration
        # would both fail check 1 and never produce a before-auth 404. Non-enumerability of
        # /advanced + /api/advanced rests on each route's own feature_block/feature_api_block
        # (a 404 when off), so the key is DELIBERATELY NOT in registry.GATE_404_STYLE (an opt_in
        # key is not in that derived set -- verify_features check 5).
        semantics="opt_in",
        web=("/advanced",),
        api=("/api/advanced",),
        nav_hrefs=("/advanced",),
        nav_gated=True,
        cli_groups=("advanced",),
        # cli_refusal is the verb-level refusal text (#533): opt_in features are NOT swept by
        # the __main__ dispatch gate (only removed+default-on are). The per-verb
        # cmd_advanced._flag_gate (-> _common.feature_gate) is the CLI kill switch and
        # verify_features check 1b pins it to this string.
        mcp_gate="advanced_workflows_enabled",
        # The capability card (app/capabilities/advanced.md) is deliberately NOT owned here: a
        # default-OFF opt_in feature that OWNED a card would fail verify_features check 7 (which
        # asserts an owned card renders 200 while the feature is on, but never turns an opt_in
        # feature on). It ships as an always-on card (the documents/tm precedent), so its docs
        # stay reachable and the harness can be discovered even before it is enabled.
        cards=(),
        legacy_global=None,
        # STRUCTURAL (HIGH-1): the harness service submodules. Every module OUTSIDE own_files
        # that imports one must be classified in cross_reach_allow (check 11), or it goes red.
        # Today NOTHING outside own_files imports them -- the surfaces reach the package ONLY
        # through its __init__ public API via the kernel facade (from app.kernel import advanced),
        # which the check-11 scanner does not treat as an app.services.* import -- so
        # cross_reach_allow is empty and the guard is satisfied. A future in-app importer of the
        # harness guts (a scheduler sweep, a cross-feature reach) would be FORCED to declare here.
        service_modules=(
            "app.services.advanced.harness",
            "app.services.advanced.tools",
            "app.services.advanced.budget",
            "app.services.advanced.transcript",
            "app.services.advanced.planner",
        ),
        own_files=(
            "app/services/advanced/__init__.py",
            "app/services/advanced/harness.py",
            "app/services/advanced/harness_part1.py",
            "app/services/advanced/tools.py",
            "app/services/advanced/tools_hub.py",
            "app/services/advanced/tools_hub2.py",
            "app/services/advanced/tools_hub3.py",
            "app/services/advanced/tools_common.py",
            "app/services/advanced/tools_part2.py",
            "app/services/advanced/tools_part3.py",
            "app/services/advanced/conductor.py",
            "app/services/advanced/conductor_hub.py",  # SRP split 2026-09-14 (pieces import tools)
            "app/services/advanced/conductor_common.py",
            "app/services/advanced/budget.py",
            "app/services/advanced/transcript.py",
            "app/services/advanced/planner.py",
            "app/services/advanced/errors.py",
            "app/web/advanced.py",
            "app/web/advanced_part1.py",
            "app/cli/cmd_advanced.py",
            "app/cli/cmd_advanced_part1.py",
            "app/cli/cmd_advanced_part1_leaf.py",
        ),
        cross_reach_allow=(),
        seams=(
            "advanced_workflows: the opt_in kill switch is enforced at the service layer -- "
            "every public entry point (create_mission/run_mission_now/get_*/list_*/"
            "transcript_view/request_cancel) raises FeatureDisabled when off, and the surfaces "
            "translate it (web feature_block 404, /api feature_api_block 404, the `advanced` CLI "
            "verbs refuse via _flag_gate naming the switch, the five MCP tools relay the 404). "
            "The dispatcher is fail-closed: a tool not in the global allowlist is refused, a tool "
            "not in the mission's allowlist is refused, and EVERY tool call re-validates the "
            "run's org/customer ring (confinement passed explicitly, never ambient). Staged "
            "targets live in the run's OWN table and NEVER touch a classic content table (P0 "
            "deliver is a stub). The classic step-list engine is UNTOUCHED (the HARD RULE).",
        ),
    ),
    Module(
        slug="terminology_concept_editor",
        title="Concept glossary editor",
        description=(
            "The rich concept/term glossary store editor: "
            "create and edit concepts, terms, senses and audiences, dual-writing the legacy "
            "compat entries. Off by default this release while the two-way editor sync is "
            "hardened; the concept READ surfaces stay available, and with it off a "
            ".glossary.json import is allowed only into an EMPTY glossary that has a language "
            "pair."
        ),
        setting_key="terminology_concept_editor_enabled",
        beta=True,
        # opt_in, NOT removed: DEFAULT-OFF, and it gates only the concept-editor WRITE SEAMS
        # inside the always-on CORE /glossaries surface -- it owns NO routes/pages/CLI group of
        # its own to 404, and the concept READ surfaces (list/show concepts, .glossary.json
        # export, the read-only web view) stay reachable when off. The databricks/scim/
        # advanced_workflows opt_in shape: verify_features check 1 REJECTS removed+default-off,
        # and path_is_off skips default-off, so a "removed" declaration would fail the guard and
        # never produce a before-auth 404. Non-enumerability is not the goal here (the routes are
        # core and stay up for reads); the kill switch is the service seam
        # (glossary_concepts._require_concept_editor), so the key is DELIBERATELY NOT in
        # registry.GATE_404_STYLE (an opt_in key is not in that derived set -- check 5).
        semantics="opt_in",
        # No web/api/hooks/nav of its own: the concept write routes live UNDER the core
        # /glossaries surface (never gated to a feature -- reads must stay), and the flag is
        # enforced at the shared service seam, not at a URL.
        web=(),
        api=(),
        nav_hrefs=(),
        nav_gated=False,
        # No standalone CLI group: the concept/term leaves live UNDER the always-on core
        # `glossary` group (`glossary module concepts add/edit/delete`, `... terms
        # add/edit/delete`, `... terms set-audience`), gated at the service seam.
        cli_groups=(),
        cli_hole_reason=(
            "The concept editor is not a standalone surface: its writes are SEAMS inside the "
            "core glossary CLI (`glossary module concepts ...` / `... terms ...`), web, /api and "
            "edit_glossary MCP glossary routes, all gated at the glossary_concepts service seam "
            "when the flag is off. There is no top-level CLI group to add or refuse."
        ),
        mcp_gate="terminology_concept_editor_enabled",
        # Not an owned card: a default-OFF opt_in feature that OWNED a card would fail
        # verify_features check 7 (which never turns an opt_in feature on). The concept editor
        # is documented inside the always-on `glossaries` card.
        cards=(),
        legacy_global=None,
        # No service_modules: the concept store is reached through the core glossary surfaces +
        # the shared glossary_concepts service, whose WRITE seams self-gate on the flag
        # (glossary_concepts._require_concept_editor). There is no OUTSIDE-of-own-files import
        # that MUTATES the store with the feature off (no HIGH-1 shape), so check 11 does not
        # apply and there is no non-URL scheduler/egress seam to declare here.
        seams=(
            "terminology_concept_editor: the concept/term-store EDITOR WRITE seams "
            "(create/edit/delete concept, add/edit/delete term, set audience) raise "
            "ConceptError naming the switch when off, on all four surfaces (web POST, /api, the "
            "`glossary ... concepts/terms` CLI leaves, and the edit_glossary MCP actions -- all "
            "delegate to app/services/glossary_concepts.py). After T1b (LAW-TERM-18 authority "
            "inversion) a `.glossary.json` / concept import (from_v1 / import_concepts_batched via "
            "glossary.import_bytes) is UNGATED on every install -- it upserts only the NAMED "
            "concepts and derives the compat rows by projection, so it can never rewrite an "
            "untouched row -- and a populated import is allowed regardless of the flag. Concept "
            "READS (list/show, JSON export, the read-only web view) stay up regardless.",
        ),
    ),
    Module(
        slug="system_prompt_overrides",
        title="System prompt overrides",
        description=(
            "Let an org admin override the editable head of the AI-step system message and of "
            "the machine-translation system message, and let a workflow carry its own text "
            "(the workflow's text wins over the org's, which wins over the built-in text), with "
            "version history, restore and reset (GitHub #356). The locked floor (marker "
            "clauses, language pair, content fence, the machine-translation content-not-"
            "instructions sentence, output contract) is always added by the app and can never "
            "be edited. Off by default; the System prompts catalogue on Building blocks stays "
            "read-only."
        ),
        setting_key="system_prompt_overrides_enabled",
        beta=True,
        # opt_in, NOT removed: DEFAULT-OFF, and it gates the WRITE and RESOLVE seams inside
        # the always-on CORE /api/system-prompts surface and the Building blocks page (the
        # catalogue READS stay up with the flag off, showing the built-in text). It owns no
        # routes/pages/CLI group of its own to 404 (the terminology_concept_editor shape):
        # verify_features check 1 REJECTS removed+default-off. The kill switch is the service
        # seam (system_prompt_overrides.require_enabled / resolve_*), so the key is
        # DELIBERATELY NOT in registry.GATE_404_STYLE (an opt_in key is not in that derived
        # set -- check 5).
        semantics="opt_in",
        web=(),
        api=(),
        nav_hrefs=(),
        nav_gated=False,
        cli_groups=(),
        cli_hole_reason=(
            "System prompt overrides are not a standalone surface: the edit verbs live under "
            "the always-on core `workflow prompt` group (`workflow prompt system-set`, "
            "`system-reset`, `system-history`, `system-restore`, each with an optional "
            "`--workflow ID`), the web editor is a card on Building blocks and a section of the "
            "workflow builder, and the API is /api/system-prompts/{key} and "
            "/api/workflows/{wid}/system-prompts/{key}; every write refuses at "
            "the system_prompt_overrides service seam when the flag is off. There is no "
            "top-level CLI group to add or refuse."
        ),
        mcp_gate="system_prompt_overrides_enabled",
        # No owned card (an opt_in feature that OWNED a card would fail check 7): the feature
        # is documented in the always-on `workflows` and `llm` cards.
        cards=(),
        legacy_global=None,
        # No service_modules: every caller reaches the store through the kernel facade
        # (`system_prompts`, which wraps it), and the run-time resolvers self-gate on the flag.
        seams=(
            "system_prompt_overrides: save / restore / reset raise OverrideError naming the "
            "switch when the flag is off, on all four surfaces (web card, /api, the "
            "`workflow prompt system-*` CLI verbs, in-process and --remote) because they all "
            "call app/services/system_prompts.py -> system_prompt_overrides, at the org and "
            "the workflow scope alike. The two run-time resolvers (the AI-step head in the "
            "orchestrator, the MT head in the translate hubs) return None, i.e. the built-in "
            "text byte for byte, without reading either override table, when the flag is off; "
            "StepRun.system_prompt_* are written only while it is on.",
        ),
    ),
    Module(
        slug="custom_fields",
        title="Custom project fields",
        description=(
            "Typed fields on projects (a Jira ticket link, a PO number, a tier), defined by admins "
            "(any scope) and managers (one customer, their own fields): "
            "list columns, filters, sorting, project forms, CLI, API and MCP. Switching it off "
            "hides the fields, columns and forms and refuses writes; stored values are kept."
        ),
        setting_key="custom_fields_enabled",
        beta=False,
        # writes_off: reads of already-stored data stay safe (nothing is shown, nothing is
        # lost) and the service layer refuses every write; the middleware never 404s a route.
        semantics="writes_off",
        web=("/settings/custom-fields",),
        api=("/api/custom-fields",),
        nav_hrefs=(),
        nav_gated=False,
        cli_groups=("field",),
        mcp_gate="custom_fields_enabled",
        cards=(),
        legacy_global=None,
        # No service_modules: the service is reached through the kernel facade by the project
        # list, project page, create seams, CLI and MCP relays; every one of them goes through
        # app/services/custom_fields.py, which refuses writes and returns no definitions when
        # the switch is off (the databricks/scim precedent: no non-URL seam to declare).
        seams=(),
    ),
)


# ============================================================================ #
# THE OPTIONAL-SLUG RATCHET (SPEC-module-registry S3.5) -- a ratcheted ALLOWLIST so a
# capability can never be silently de-flagged. OPTIONAL_SLUGS only GROWS; a new module
# adds its slug here (the one edit site the registry ADDS, S0.2). A slug deliberately
# retired from MODULES must be recorded in REMOVED_OPTIONAL with a non-empty reason --
# verify_modules.py check 4 asserts {MODULES} U {REMOVED_OPTIONAL} superset OPTIONAL_SLUGS,
# every reason non-empty, and NO REMOVED_OPTIONAL slug still in MODULES (staleness both
# directions). MIN_FEATURES (app/features.py) stays as a redundant count floor.
# ============================================================================ #
OPTIONAL_SLUGS: frozenset = frozenset(
    {
        "images",
        "keys",
        "key_images",
        "evaluations",
        "llmemory",
        "llmemory_suggestions",
        "assist",
        "prompt_tap",
        "utcp",
        "vendors",
        "reports",
        "routing",
        "databricks",
        "queries",
        "worklist",
        "scim",
        "saml",
        "data_export",
        "advanced_workflows",
        "terminology_concept_editor",
        "system_prompt_overrides",
        "custom_fields",
    }
)

#: (slug, non-empty reason) for a slug DELIBERATELY retired from MODULES. Empty today
#: -- nothing has been de-flagged. A real de-flagging adds its (slug, reason) here.
REMOVED_OPTIONAL: tuple = ()


# ============================================================================ #
# ASSEMBLY RELOCATION -- ROUTER_GROUPS (SPEC-module-registry S3.3). The single home for
# what app/web/__init__.py split into three hand lists: (dotted-router-attr, group)
# pairs, group in {"health","api","web"}. router_groups() composes the three ordered
# lists from it; app/web/__init__.py imports them. This is RELOCATION, not a per-module
# fact -- api_cli.router is shared (S1a), so the router->group edge cannot be attributed
# to a module. Order here IS the pre-migration composition order (health, then api, then
# web), so the composed router set stays byte-identical.
# ============================================================================ #
ROUTER_GROUPS: tuple = (
    ("health.router", "health"),
    ("api.router", "api"),
    ("api_cli.router", "api"),
    ("keys.api_router", "api"),
    ("images.api_router", "api"),
    ("advanced.api_router", "api"),
    ("utcp.router", "api"),
    ("mcp_oauth_as.api_router", "api"),
    ("home.router", "web"),
    ("help.router", "web"),
    ("projects.router", "web"),
    ("documents.router", "web"),
    ("images.router", "web"),
    ("keys.router", "web"),
    ("workflows.router", "web"),
    ("glossaries.router", "web"),
    ("style_guides.router", "web"),
    ("content_types.router", "web"),
    ("custom_fields.router", "web"),
    ("tm.router", "web"),
    ("memories.router", "web"),
    ("memory_suggestions.router", "web"),
    ("evaluations.router", "web"),
    ("connectors.router", "web"),
    ("vendors.router", "web"),
    ("jobs_ui.router", "web"),
    ("auth.router", "web"),
    ("apikeys.router", "web"),
    ("admin.router", "web"),
    ("enterprise.router", "web"),
    ("routing.router", "web"),
    ("databricks.router", "web"),
    ("query_threads.router", "web"),
    ("worklist.router", "web"),
    ("scim.router", "web"),
    ("exports.router", "web"),
    ("advanced.router", "web"),
    ("mcp_oauth_as.router", "web"),
)

_GROUP_ORDER = ("health", "api", "web")


# ============================================================================ #
# CLI MIRROR RELOCATION -- MODULE_CLI (SPEC-module-registry S4 / M3). The module-owned rows
# of tests/verify_cli_parity.py's EXPECTED_COMMANDS, RELOCATED here: (command-path, capability
# description) per optional module, keyed by slug. This is authored content MOVED into the
# manifest, not eliminated -- a new module declares its CLI leaf rows HERE instead of in the
# guard. cli_expected() flattens it into the {path: description} table verify_cli_parity now
# DERIVES its module-owned rows from; verify_modules.py check 8 pins cli_expected() to a
# byte-identity snapshot of the pre-relocation rows. Kept a slug-keyed table (NOT a Module
# field) so the Module record stays the exact Feature superset (S3.1: no new structural field).
# A module with no CLI surface (utcp -- a generated /utcp doc) simply has no entry.
# ============================================================================ #
MODULE_CLI: dict = {
    "images": (
        (
            "image upload",
            "upload an image into a project; region detection at ingest (POST /images/upload parity)",  # noqa: E501
        ),
        ("image list", "list a project's image documents (GET /images parity)"),
        (
            "image show",
            "an image's regions + translations + confidence/overflow flags (GET /images/{id} review page + /api/images/{id} parity)",  # noqa: E501
        ),
        (
            "image translate",
            "translate an image's regions synchronously through the standard pipeline (POST /images/{id}/translate parity)",  # noqa: E501
        ),
        (
            "image render",
            "re-render the localized image + report per-region fit flags (GET /images/{id}/localized preview parity)",  # noqa: E501
        ),
        (
            "image export",
            "export the localized image file (GET /images/{id}/export + /api/images/{id}/export parity)",  # noqa: E501
        ),
    ),
    "keys": (
        (
            "keys add",
            "create a string key, optionally with its base value (POST /projects/{id}/keys/add + POST /api/projects/{id}/keys parity)",  # noqa: E501
        ),
        (
            "keys list",
            "list a project's keys + per-locale values with the grid's filters (GET /projects/{id}/keys + GET /api/projects/{id}/keys parity)",  # noqa: E501
        ),
        (
            "keys set",
            "set one locale's value (or the base value) for a key (the grid's inline edit + PUT /api/keys/{id}/values/{locale} parity)",  # noqa: E501
        ),
        (
            "keys delete",
            "soft-delete a key, tombstoned in the change feed (POST /projects/{id}/keys/{kid}/delete + DELETE /api/keys/{id})",  # noqa: E501
        ),
        (
            "keys restore",
            "undo a soft delete, re-announcing the key and its values on the change feed (the grid's Restore button + POST /api/keys/{id}/restore parity)",  # noqa: E501
        ),
        (
            "keys edit",
            "edit a key's metadata, identity (rename / move namespace / context) and plural setting, all-or-nothing (the grid's Edit drawer + PATCH /api/keys/{id} parity)",  # noqa: E501
        ),
        (
            "keys import",
            "import a JSON or gettext .po key file, merge or cleanup (POST /projects/{id}/keys/import + /api .../keys/import parity)",  # noqa: E501
        ),
        (
            "keys export",
            "export one locale as a key file, per-namespace build file or whole-project transport file (GET .../keys/export parity)",  # noqa: E501
        ),
        (
            "keys changes",
            "read the Delta API's change feed: --since a cursor (tombstones included, --cursor-file for CI sync) or --modified-after a timestamp (current-state rows, keyset-paged, no deletions) (GET /api/projects/{id}/changes parity)",  # noqa: E501
        ),
        (
            "keys translate",
            "launch translation over a key project's namespace for one target locale or --locale all (POST /api/projects/{id}/keys/translate parity)",  # noqa: E501
        ),
        # Batch 15 T2: the delivery empty gate (key/project mode). Held key exports
        # need an audited human approval before shipping empty values.
        (
            "keys empties",
            "list a key project's untranslated (empty) keys that the delivery empty gate is holding back from delivery (GET /api/projects/{id}/keys/empties parity)",  # noqa: E501
        ),
        (
            "keys deliver-approve",
            "approve delivering a held key project's empty values for a locale, audited; refused (400) while a run is still in flight over the project's key documents. A run is a run in either tier: a classic workflow run (a fan-out's member runs included, and the unfinished fan-out ITSELF only while that locale's current-attempt member is not yet created or is non-terminal; a re-run holds its selected locales from acceptance until their fresh members are terminal, and finished locales not selected still release) or an Advanced-Workflow run over the same project/document and locale. (POST /api/projects/{id}/keys/deliver-approve parity)",  # noqa: E501
        ),
        (
            "keys deliver-revoke",
            "revoke a prior empty-delivery approval for a key project + locale, re-holding it (POST /api/projects/{id}/keys/deliver-revoke parity)",  # noqa: E501
        ),
        # Batch 14 per-string image context: the `keys image` subgroup lives UNDER the
        # `keys` cli_group (the key_images module owns no group of its own), so its leaves
        # are filed here (the llmemory `memory suggestions` precedent).
        (
            "keys image add",
            "attach an in-context image to a string key (POST /projects/{id}/keys/{kid}/images + POST /api/keys/{kid}/images parity)",  # noqa: E501
        ),
        (
            "keys image list",
            "list a key's in-context images with their dims + primary flag (GET /api/keys/{kid}/images parity)",  # noqa: E501
        ),
        (
            "keys image rm",
            "soft-delete one of a key's images (POST /projects/{id}/keys/{kid}/images/{iid}/delete + DELETE /api/keys/{kid}/images/{iid} parity)",  # noqa: E501
        ),
        (
            "keys image set-primary",
            "make one image the key's primary, the one sent by default at translation time (POST /api/keys/{kid}/images/{iid}/primary parity)",  # noqa: E501
        ),
    ),
    "evaluations": (
        (
            "eval dataset list",
            "list datasets with their reference coverage (GET /api/evaluations/datasets parity)",
        ),
        (
            "eval dataset create",
            "create a dataset from pasted/uploaded text (POST /api/evaluations/datasets parity)",
        ),
        (
            "eval dataset import-doc",
            "snapshot a production document's segments into a new dataset, one-directional copy (POST /api/evaluations/datasets/import-doc parity)",  # noqa: E501
        ),
        (
            "eval dataset show",
            "show one dataset's detail + its scorer menu (GET /api/evaluations/datasets/{id} parity)",  # noqa: E501
        ),
        (
            "eval candidate list",
            "list candidates (locked = a run already references it) (GET /api/evaluations/candidates parity)",  # noqa: E501
        ),
        (
            "eval candidate create",
            "create a candidate config-under-test (POST /api/evaluations/candidates parity)",
        ),
        (
            "eval run start",
            "launch a run (dataset x candidates x scorers x repeat-N); shows the pre-run cost estimate and refuses to launch without --yes (POST /api/evaluations/runs/start parity)",  # noqa: E501
        ),
        (
            "eval run show",
            "show one run's status, scorers, realized cost vs estimate (GET /api/evaluations/runs/{id} parity)",  # noqa: E501
        ),
        (
            "eval run compare",
            "the comparison view: per-candidate verdict vs a baseline, cost and latency, full per-metric breakdown under --json (GET /api/evaluations/compare parity)",  # noqa: E501
        ),
        (
            "eval export",
            "export the comparison report (one row per item x candidate) to jsonl/csv, stdout by default (GET /api/evaluations/export parity)",  # noqa: E501
        ),
        (
            "eval draft",
            "SPEC-ask-draft section 5: turn a plain-language question into a drafted run plan against the caller's real datasets/candidates + the exact `eval run start` command; --apply --yes launches through that SAME seam, estimate and confirm intact (POST /evaluations/draft parity). Local-only by spec",  # noqa: E501
        ),
        (
            "eval promote",
            "promote a candidate into a production workflow, margin evaluated against the corrected comparison (POST /api/evaluations/promote parity)",  # noqa: E501
        ),
        # SPEC-quality-lenses-p2 section 6 (T2): the Quality Lens meta-evaluation harness.
        # Gold sets are Lab data, so the verbs live in the `eval` group beside the rest.
        (
            "eval gold seed-demo",
            "seed the curated en-US to es-ES DEMO gold set for the Quality Lens meta-evaluation; EXPLICIT and idempotent, never automatic (POST /api/evaluations/gold/seed-demo parity)",  # noqa: E501
        ),
        (
            "eval gold list",
            "show one dataset's gold items, span labels and whether each measurement floor is met (GET /api/evaluations/gold parity)",  # noqa: E501
        ),
        (
            "eval gold import",
            "import gold items + human span labels for a dataset from a labels JSON file, all or nothing (POST /api/evaluations/gold/import parity)",  # noqa: E501
        ),
        (
            "eval meta",
            "meta-evaluate a Quality Lens against a gold set: tie-calibrated pairwise accuracy + major-error recall with precision and f1 (POST /api/evaluations/meta parity)",  # noqa: E501
        ),
    ),
    "llmemory": (
        (
            "memory list",
            "list memories, customer-scoped + kind/status/origin/locale-pair filters (GET /api/memories parity)",  # noqa: E501
        ),
        (
            "memory show",
            "show one memory: content, scope, trust, evidence/use stats (GET /api/memories/{id} parity)",  # noqa: E501
        ),
        (
            "memory events",
            "list the evidence events (before/after receipts) a memory cites (GET /api/memories/{id}/events parity)",  # noqa: E501
        ),
        (
            "memory add-note",
            "create a manual memory - activates immediately, the highest trust tier (POST /api/memories/note parity)",  # noqa: E501
        ),
        (
            "memory invalidate",
            "revoke one memory - content kept, tombstoned (POST /api/memories/{id}/invalidate parity)",  # noqa: E501
        ),
        (
            "memory invalidate-since",
            "rollback verb - revoke every memory created at/after a timestamp within a scope (POST /api/memories/invalidate-since parity)",  # noqa: E501
        ),
        (
            "memory purge",
            "hard-erase one memory's content, admin-only (POST /api/memories/{id}/purge parity)",
        ),
        (
            "memory distill",
            "run the deterministic distillation pass - admit/supersede/conflict, idempotent (POST /api/memories/distill parity)",  # noqa: E501
        ),
        (
            "memory janitor",
            "soft lifecycle sweep - decay-by-last-used, candidate expiry, contradiction demotion (status transitions only, --dry-run to preview) (POST /api/memories/janitor parity)",  # noqa: E501
        ),
        (
            "memory resolve-conflict",
            "adjudicate a conflicted pair - make one memory the active winner, superseding its conflicted siblings (POST /api/memories/{id}/resolve-conflict parity)",  # noqa: E501
        ),
        (
            "memory export",
            "export memories to jsonl or csv, stdout by default (GET /api/memories/export parity)",
        ),
        (
            "memory draft",
            "SPEC-ask-draft section 5: turn plain-language intent into drafted memory note(s) + the exact add-note commands; --apply --yes writes through the SAME llmemory.add_note seam (POST /memories/draft parity). Local-only by spec (Ask+Draft has no /api surface)",  # noqa: E501
        ),
        (
            "memory suggestions list",
            "list workflow suggestions, customer-scoped + status/workflow filters (GET /api/memory-suggestions parity)",  # noqa: E501
        ),
        (
            "memory suggestions show",
            "show one suggestion: kind, status, the rendered diff, proposed change, citations (GET /api/memory-suggestions/{id} parity)",  # noqa: E501
        ),
        (
            "memory suggestions evidence",
            "list the rows (LQAResults/events/entries) a suggestion cites (GET /api/memory-suggestions/{id}/evidence parity)",  # noqa: E501
        ),
        (
            "memory suggestions generate",
            "run the deterministic draft pass (R1/R2/R3), idempotent, proposes rows only - never mutates a workflow (POST /api/memory-suggestions/generate parity)",  # noqa: E501
        ),
        (
            "memory suggestions approve",
            "approve a proposed suggestion (--apply also applies in one call, admin-only) (POST /api/memory-suggestions/{id}/approve parity)",  # noqa: E501
        ),
        (
            "memory suggestions reject",
            "reject a suggestion - terminal, tombstoned (POST /api/memory-suggestions/{id}/reject parity)",  # noqa: E501
        ),
        (
            "memory suggestions apply",
            "apply an approved suggestion - the ONLY workflow-mutating verb, admin-only, remote-gated (POST /api/memory-suggestions/{id}/apply parity)",  # noqa: E501
        ),
        (
            "memory suggestions retry",
            "re-arm a cleanly-blocked suggestion (blocked->approved); refuses a partial-patch block (POST /api/memory-suggestions/{id}/retry parity)",  # noqa: E501
        ),
        (
            "memory suggestions recover",
            "break-glass: mark a stuck 'applying' suggestion blocked (non-retryable) for manual reconciliation, admin-only (POST /api/memory-suggestions/{id}/recover parity)",  # noqa: E501
        ),
        (
            "memory suggestions export",
            "export workflow suggestions to jsonl or csv, stdout by default (GET /api/memory-suggestions/export parity)",  # noqa: E501
        ),
    ),
    "assist": (
        (
            "ask",
            "answer a plain-language question about a module, grounded ONLY in that module's shipped capability card (POST /assist/ask parity)",  # noqa: E501
        ),
    ),
    "prompt_tap": (
        ("prompt-tap on", "turn request capturing ON (POST /prompt-tap/toggle parity)"),
        ("prompt-tap off", "turn request capturing OFF (POST /prompt-tap/toggle parity)"),
        ("prompt-tap status", "report capturing on/off + stored record count"),
        ("ledger status", "read envelope and retained raw-file status (GET /prompt-tap parity)"),
        ("ledger envelopes off", "guarded disable (POST /prompt-tap/envelopes/{state} parity)"),
        ("ledger envelopes on", "audited enable (POST /prompt-tap/envelopes/{state} parity)"),
        ("ledger purge", "guarded dated purge (POST /prompt-tap/purge parity)"),
        (
            "prompt-tap show",
            "print captured request payloads, newest first (GET /prompt-tap parity)",
        ),
        (
            "prompt-tap actions",
            "list translation actions newest-first, the run-envelope view (GET /prompt-tap Runs section parity)",  # noqa: E501
        ),
        (
            "prompt-tap show-action",
            "show one action's envelope + language-pair runs (+ calls) (GET /prompt-tap run-detail parity)",  # noqa: E501
        ),
        (
            "prompt-tap runs",
            "list translation runs newest-first, the run-first view (GET /api/prompt-tap/runs parity)",  # noqa: E501
        ),
        (
            "prompt-tap run",
            "show one run's calls grouped by step then unit; --bodies host-only (GET /api/prompt-tap/runs/{id} parity)",  # noqa: E501
        ),
        (
            "prompt-tap export",
            "download captured payloads for analysis outside the tool - one record by key or the current filter selection, jsonl/json (GET /prompt-tap/download parity, same export_bundle seam)",  # noqa: E501
        ),
        ("prompt-tap clear", "truncate the capture file (POST /prompt-tap/clear parity)"),
    ),
    "vendors": (
        ("vendor add", "create a vendor (name required; connection/rate-card filled later)"),
        ("vendor list", "list vendors (active by default; --include-archived)"),
        (
            "vendor show",
            "show a vendor (connection state, rate card, customer links; never the secret value)",
        ),
        ("vendor update", "update a vendor's name/description/endpoint (partial)"),
        (
            "vendor set-secret",
            "set/merge a vendor credential (signing secret / auth token), kept off argv where possible; never echoed",  # noqa: E501
        ),
        (
            "vendor link-customer",
            "link a vendor to a customer (the fail-closed scope an external step selects through)",
        ),
        ("vendor unlink-customer", "remove a vendor<->customer link"),
        ("vendor rate-card set", "set one MSA rate-card band's cost per word (merged)"),
        ("vendor rate-card show", "show a vendor's MSA rate card (per-band rates)"),
        ("vendor dispatches", "list dispatch legs (work out with a vendor, awaiting return)"),
        (
            "vendor dispatch-show",
            "show ONE dispatch leg + its TM-leverage band breakdown (mirrors GET /api/vendors/dispatches/<id>)",  # noqa: E501
        ),
        (
            "vendor return",
            "ingest a vendor return contract from a file (manual fallback; same guards as the signed callback)",  # noqa: E501
        ),
        ("vendor poll", "poll vendors for ready returns (only vendors with a config.poll_url)"),
        (
            "vendor redrive",
            "re-drive a RETURNED leg whose run never resumed (recovery); continues the parked run, re-committing nothing",  # noqa: E501
        ),
        (
            "vendor quote",
            "record a QUOTE on a quote-gated leg (Wave E R3; mirrors POST /api/vendors/dispatches/<id>/quote)",  # noqa: E501
        ),
        (
            "vendor approve-quote",
            "APPROVE a quote so work proceeds (Wave E R3 spend decision; mirrors POST /api/vendors/dispatches/<id>/approve-quote)",  # noqa: E501
        ),
        (
            "vendor decline-quote",
            "DECLINE a quote and escalate to a human (Wave E R3; mirrors POST /api/vendors/dispatches/<id>/decline-quote)",  # noqa: E501
        ),
        (
            "vendor reject",
            "REJECT a delivered leg and re-dispatch a rework with feedback (Wave E R4; mirrors POST /api/vendors/dispatches/<id>/reject-rework)",  # noqa: E501
        ),
        ("vendor archive", "archive (retire) a vendor - drops it from every picker"),
        ("vendor unarchive", "unarchive (reactivate) a vendor"),
        (
            "vendor delete",
            "delete a vendor - fails closed while a step/dispatch references it, never cascades",
        ),
    ),
    "reports": (
        ("report run", "run a cost/tokens/volume/qa/postedit report (CSV; /reports + /api parity)"),
        ("report kinds", "list report kinds + their columns"),
    ),
    "routing": (
        ("routing rule add", "create a routing rule (/routing + /api/routing parity)"),
        ("routing rule list", "list routing rules in priority order (--remote parity)"),
        ("routing rule show", "read back one routing rule (--remote parity)"),
        ("routing rule toggle", "flip a routing rule active/inactive (--remote parity)"),
        ("routing rule delete", "delete a routing rule (--remote parity)"),
    ),
    "databricks": (
        (
            "databricks export",
            "run one Databricks export batch: stage + optional upload (/admin/databricks + /api/admin/databricks parity)",  # noqa: E501
        ),
        ("databricks exports", "list past Databricks export runs (--remote parity)"),
        ("databricks status", "show one Databricks export run's status + counts (--remote parity)"),
        (
            "databricks setup",
            "one-time: create the Unity Catalog volume + the six stream tables, idempotent (/api/admin/databricks/setup parity)",  # noqa: E501
        ),
    ),
    "queries": (
        (
            "query open",
            'open a query thread scoped to a segment / key / document / project, with an optional assignee (POST /api/queries parity). In-process the author is --actor or "system" (the trusted operator)',  # noqa: E501
        ),
        (
            "query list",
            "list query threads over your visible projects; filter by --status / --project / --document / --segment / --key / --assignee / --mine (GET /api/queries parity)",  # noqa: E501
        ),
        ("query show", "show one thread + its posts (GET /api/queries/{id} parity)"),
        (
            "query reply",
            "post a reply to a thread; a plain reply never marks it answered (POST /api/queries/{id}/posts parity)",  # noqa: E501
        ),
        (
            "query answer",
            "post a reply that marks the thread answered (POST /api/queries/{id}/posts answer=1 parity)",  # noqa: E501
        ),
        (
            "query close",
            "close a thread (author / assignee / reviewer+), or --reopen it (author / reviewer+) (POST /api/queries/{id}/status parity)",  # noqa: E501
        ),
        (
            "query assign",
            "set or clear a thread assignee (author / reviewer+); the target must be an in-scope active user (POST /api/queries/{id}/assign parity)",  # noqa: E501
        ),
    ),
    "worklist": (
        (
            "work list",
            "your cross-project reviewer worklist: paused review steps that name you, queries assigned to / answered for you, and documents needing quality attention; filter with --days / --quality needs_review (GET /api/worklist parity). In-process it needs --actor <email> (the trusted operator has no session user); assignment lives on the workflow DEFINITION, so a review step naming you shows every paused run of that workflow",  # noqa: E501
        ),
    ),
    "scim": (
        (
            "scim status",
            "show SCIM provisioning status: the scim_enabled flag, counts of SCIM-managed users + groups, and the last SCIM sync activity from the audit trail (read-only; provisioning is IdP-push by design)",  # noqa: E501
        ),
        (
            "scim users",
            "list SCIM-managed accounts (auth_source=scim or an adopted external_id): email, name, active, external id (read-only; --org confines under the multi-org gate)",  # noqa: E501
        ),
        (
            "scim groups",
            "list SCIM directory groups + their member emails (read-only; groups are directory metadata with zero authorization effect in v1; --org confines under the multi-org gate)",  # noqa: E501
        ),
    ),
    "custom_fields": (
        (
            "field list",
            "list custom project fields (GET /api/custom-fields + /settings/custom-fields parity)",
        ),
        (
            "field create",
            "define a custom project field (POST /api/custom-fields parity; manager for one "
            "customer, admin for any)",
        ),
        (
            "field update",
            "change a custom project field (PATCH /api/custom-fields/{id} parity; its creating "
            "manager or an admin)",
        ),
        (
            "field archive",
            "archive a custom project field, values kept (PATCH /api/custom-fields/{id} parity)",
        ),
        (
            "field restore",
            "restore an archived custom project field (PATCH /api/custom-fields/{id} parity)",
        ),
    ),
    "data_export": (
        (
            "export run",
            "run one full-object-graph data export - a RBAC-confined ZIP of JSONL-per-entity + manifest staged to EXPORTS_DIR; local runs synchronously as the trusted operator, --remote starts the async job over POST /api/exports and polls it to a terminal state (POST /api/exports + /exports parity)",  # noqa: E501
        ),
        (
            "export list",
            "list past data-export runs, newest first (GET /api/exports parity; actor-filtered over --remote)",  # noqa: E501
        ),
        (
            "export status",
            "show one data-export run's status + entity counts by run id or --uuid (GET /api/exports/{run_uuid} parity)",  # noqa: E501
        ),
        (
            "export download",
            "download a completed export artifact ZIP to a path by run_uuid (Range-resumable; GET /api/exports/{run_uuid}/artifact parity)",  # noqa: E501
        ),
    ),
    "advanced_workflows": (
        (
            "advanced create",
            "create an Advanced-Workflow mission: brief, fail-closed tool allowlist, model roster, scripted actions (P0 planner), budget + fences (POST /api/advanced/missions + /advanced form parity)",  # noqa: E501
        ),
        (
            "advanced run",
            "run a mission synchronously through the harness loop and report the outcome; a run that LOST an authored unit to no candidate ends failed with each lost unit named in the run notes, while a run that only HELD units (lens block, held document, locked target, vanished segment) ends completed and keeps every target it did write (POST /api/advanced/runs parity; the MCP run tool's confirmed half)",  # noqa: E501
        ),
        (
            "advanced status",
            "show one run's status + spend/fence actuals and mission Parallel/NOTICE lines, also over --remote (GET /api/advanced/missions/{mission_id}; mission 404 falls back to core; --json unchanged); a run that LOST an authored unit to no candidate reads failed and names each lost unit in its notes, while a run that only HELD units (lens block, held document, locked target, vanished segment) reads completed with the same delivered-of-authored count (GET /api/advanced/runs/{run_id} parity)",  # noqa: E501
        ),
        (
            "advanced transcript",
            "print one run's durable, auditable transcript in order (GET /api/advanced/runs/{run_id}/transcript parity)",  # noqa: E501
        ),
        (
            "advanced cancel",
            "request cancellation of a non-terminal run; the harness stops it between tool calls (POST /api/advanced/runs/{run_id}/cancel parity)",  # noqa: E501
        ),
        (
            "advanced explain",
            "print how Advanced missions work (conductor, committee, adjudication, the lens, the research arm, fences) - the same operator help the /advanced page shows, from one shared help-text source",  # noqa: E501
        ),
        (
            "advanced show",
            "show a run as per-unit cards (candidates, adjudicated winner + why, lens verdict + spans, research evidence, delivered/held) with --cards; the same card read-model the /advanced run page renders",  # noqa: E501
        ),
        (
            "advanced templates list",
            "list the mission templates: named starting points that prefill a mission's mode, tools, committee, research toggle and fences (GET /api/advanced/templates parity)",  # noqa: E501
        ),
        (
            "advanced templates show",
            "show one mission template: its roster and the advanced create command it prefills (GET /api/advanced/templates?name= parity)",  # noqa: E501
        ),
        (
            "advanced lens list",
            "list the Quality Lenses in your ring (the delivery gate) (GET /api/advanced/lenses parity)",  # noqa: E501
        ),
        (
            "advanced lens show",
            "show one Quality Lens by slug or id (GET /api/advanced/lenses/{lens_id} parity)",
        ),
        (
            "advanced lens create",
            "author a Quality Lens (admin): severities/weights/threshold/checks (POST /api/advanced/lenses parity)",  # noqa: E501
        ),
        (
            "advanced lens update",
            "edit a Quality Lens (admin); a builtin edit forks a customer-owned copy (PATCH /api/advanced/lenses/{lens_id} parity)",  # noqa: E501
        ),
        (
            "advanced lens archive",
            "soft-archive a Quality Lens (admin); it stops gating but is never deleted (POST /api/advanced/lenses/{lens_id}/archive parity)",  # noqa: E501
        ),
    ),
}


def cli_expected(include_absent: bool = False) -> dict:
    """The module-owned CLI leaf rows as a {command-path: description} dict, aggregated from
    MODULE_CLI. This is the source verify_cli_parity.EXPECTED_COMMANDS derives its module rows
    from (M3); core rows stay hand-authored in the guard. Reads only stdlib data -- no app.web,
    no parser -- so it stays standalone-import safe.

    Open-core edition: the rows of a PAID feature whose add-on is not installed describe CLI
    leaves that do not exist in this tree, so they are left out; `include_absent=True` returns the
    full table (the byte-pinned snapshot in verify_modules)."""
    from app import editions

    out: dict = {}
    for slug, rows in MODULE_CLI.items():
        if not include_absent and not editions.feature_installed(slug):
            continue
        for path, desc in rows:
            out[path] = desc
    return out


def mcp_gate_for(tool) -> str | None:
    """The feature_gate an MCP Tool should carry, DERIVED from the manifest: the setting_key of
    the single optional module owning any of the tool's /api routes, or None when every route is
    core (a core/perimeter tool -- e.g. one gated on remote_admin_enabled -- keeps its own
    hand-set gate; it is not a module). Raises ValueError if a tool's routes span two modules (a
    defect). The derivation runs in the GUARD (verify_modules.py check 9), reading this manifest;
    integrations/mcp-server/registry.py still imports nothing and keeps its hand-set literal,
    now PROVEN equal to this derivation. app.features is imported lazily (app_owner lives there),
    never at module scope, so the standalone-import property holds."""
    from app import features as _F

    by_key = {m.slug: m.setting_key for m in MODULES}
    owners = set()
    for _method, path in tool.routes:
        owner = _F.api_owner(path)  # (kind, key) or None; ValueError on a tie
        if owner is not None and owner[0] == "feature":
            owners.add(owner[1])
    if not owners:
        return None
    if len(owners) > 1:
        raise ValueError(
            "tool {!r} routes span modules {!r}".format(getattr(tool, "name", "?"), sorted(owners))
        )
    return by_key[next(iter(owners))]


# ============================================================================ #
# Derivation helpers
# ============================================================================ #
def features_view() -> tuple:
    """The `app.features.FEATURES` tuple, PROJECTED from MODULES onto app.features.Feature
    (every Feature field verbatim). Evaluated ONCE at app.features import into a module-level
    tuple constant. Feature is imported HERE, inside the function -- never at module scope --
    so `import app.modules` pulls in no app.features/app.web and there is no import cycle
    (app.features imports this)."""
    from app.features import Feature

    names = tuple(f.name for f in _dc_fields(Feature))
    return tuple(Feature(**{n: getattr(m, n) for n in names}) for m in MODULES)


def router_groups() -> tuple:
    """Compose (HEALTH_ROUTERS, API_ROUTERS, WEB_ROUTERS) from ROUTER_GROUPS, resolving each
    dotted `<submodule>.<attr>` LAZILY here (a function-body import), never at module scope --
    the app.features.is_enabled pattern. This preserves the standalone-import property (S3.6):
    reading the manifest never drags in the router tree. Order within each group follows
    ROUTER_GROUPS, which IS the pre-migration order, so the composition is byte-identical."""
    import importlib

    from app import editions

    out = {g: [] for g in _GROUP_ORDER}
    for dotted, group in ROUTER_GROUPS:
        submod, attr = dotted.split(".", 1)
        # A PAID router whose code is not installed is skipped (open-core edition): the
        # core boots and serves everything else. Core routers are never skipped.
        if not editions.module_installed("app.web." + submod):
            continue
        router = getattr(importlib.import_module("app.web." + submod), attr)
        out[group].append(router)
    return tuple(out[g] for g in _GROUP_ORDER)
