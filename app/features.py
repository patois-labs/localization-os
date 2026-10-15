"""The central feature-flag registry — one declarative table of the NON-CORE
features an admin may switch off, and what a switch-off must reach on every surface.

THE KILL-SWITCH LAW (the posture every entry here inherits, from keys.py / images.py):
    A switched-off capability is NON-ENUMERABLE THROUGH THE APP'S OWN RESPONSES. Its
    routes answer 404, never 403 — a 404 is indistinguishable from a route that was
    never built, so a live request cannot tell a disabled feature from one that was
    never built. (The static /openapi.json schema still LISTS the route shape; the
    doctrine is about the app's runtime responses, not the published contract.)
    `semantics == "removed"` features get
    that treatment: the middleware (app/main._feature_gate) 404s their web/api/hooks
    patterns before auth, and the per-route `_flag_off_block` calls stay as layer 2.
    `semantics == "writes_off"` features (Evaluations, LLMemory) deliberately keep
    their READS answering — turning the Lab or the memory module off must not hide
    the evidence of what it did — so their existing service-layer write gates stay
    authoritative and the middleware never touches their routes.

HISTORICAL-FACT EXCEPTION (ffadmin Wave 3, LOW-1) — a DELIBERATE, documented seam.
    A removed feature's OWN surfaces go dark, but a CORE page that merely RECORDS a past
    interaction with the feature keeps showing that fact. When `vendors` is off, the run
    view and the document actions row still report that a run parked on an external vendor
    dispatch (the forensic record — the SAME written principle as evaluations'/llmemory's
    reads-stay-up: turning a feature off must not erase the evidence of what it did), but
    they render the vendor as PLAIN TEXT, never a live link/form into the removed /vendors
    surface. The LIVE affordances on core pages (the Customer-360 "Connected vendors"
    link/unlink panel, the workflow-builder vendor picker) DO vanish with the feature —
    only the read-only forensic fact remains.

PERIMETER vs PRODUCT — why `remote_admin_enabled` is NOT in this registry.
    A feature flag here is a PRODUCT switch: it removes a capability an operator does
    not want, on THIS install, across web+CLI+/api+MCP. `remote_admin_enabled` is a
    PERIMETER switch — it decides whether the irreversible /api admin verbs (R2/R3)
    are reachable OVER THE WIRE at all. It stays REMOTE_IMMUTABLE (host-only), keeps
    its scope-first `_r3_gate` ordering (a perimeter switch on an already-authed
    surface — a different job from flag-before-auth), and is not a toggleable product
    feature. It appears in this module only inside GATE_404_STYLE's derivation note,
    because — like a removed feature — the app 404s on it (a switched-off perimeter is
    non-enumerable too), and the MCP server's 404-note set must know that.

/back-office IS GONE (2026-09-20). It WAS a separate ASGI mount with its own session
cookie, auth wall and CSRF (app/backoffice/), gated by the SACRED `multi_org_enabled`
switch; it was never a product feature and no Feature here has ever claimed a
/back-office pattern, so its removal changes nothing in this registry. Recorded rather
than deleted because the reasoning still holds for anything mounted beside the customer
app: `_feature_gate` does not reach into a separate mount, so gating a flagged feature
there would be a CONSCIOUS decision made against that mount's own auth, never something
this registry does by accident.

THIS MODULE IMPORTS NOTHING AT MODULE LEVEL (stdlib/dataclasses only). `settings`
is imported inside the functions that read it, exactly like the MCP registry.py — so
`build_parser()` and every mirror guard stay DB-free and import-free, and this table
can be read by a guard that runs with no app context.

Documented in: docs/architecture/modules-and-flags.md
"""

from __future__ import annotations

from dataclasses import dataclass

from app.modules import features_view

# Falsy spellings of a DEFAULTS value, read ONLY by default_on() to classify how a key
# SHIPS (a real kill switch vs a default-off opt-in). This is deliberately tolerant
# because it interprets the repo-authored DEFAULTS constant, not runtime input. It is
# NOT the runtime on/off test: is_enabled() is a STRICT `== "1"` whitelist (matching the
# four delegated services), so a non-canonical RUNTIME value fails toward OFF at every
# layer. _OFF has exactly one consumer, default_on(); the runtime path never touches it.
_OFF = frozenset({"0", "false", "no", "off", ""})

#: Ratchet: this table may only grow. Wave 1 seeded seven existing-flag features;
#: Wave 3 added `vendors` (the eighth); Wave 4 added `reports` + `routing` (ten);
#: composable-core phase 2 added `databricks` (the eleventh). Batch 13 added
#: `data_export` (the writes_off full-object-graph export module) as the twelfth. Batch 14
#: adds `key_image_context` (the per-string image-context module) as the thirteenth.
MIN_FEATURES = 13


# ============================================================================ #
# Segment-pattern matching (the `_is_back_office` lesson, generalized)
# ============================================================================ #
def _seg(p: str) -> list[str]:
    return [s for s in (p or "").strip("/").split("/") if s]


def _matches(pattern: str, path: str) -> bool:
    """True when `path` matches segment-pattern `pattern`.

    `*` matches EXACTLY ONE segment. A pattern matches a path that EXTENDS it only at
    a segment boundary (a tail): `/projects/*/keys` matches `/projects/12/keys/3/base`
    but never `/projects/12/keysX` — 'keys' != 'keysX', and the glue an f-string prefix
    check would allow is exactly the bug this avoids. Literals compare by equality, so
    a literal pattern segment never matches a route-template placeholder (`{pid}`),
    while a `*` matches any segment INCLUDING `{pid}` — that is what lets the SAME
    function classify both a live URL (`/api/projects/8/keys`) and a route template
    (`/api/projects/{pid}/keys`)."""
    ps, xs = _seg(pattern), _seg(path)
    if len(ps) > len(xs):
        return False
    for p, x in zip(ps, xs, strict=False):
        if p == "*":
            continue
        if p != x:
            return False
    return True


def _specificity(pattern: str) -> int:
    """How specific a match is: the number of leading segments the pattern fixes.
    Longest (most segments) wins, so `/api/projects/*/keys` (4) beats the broad
    `/api/projects` (2) on a keys route."""
    return len(_seg(pattern))


# ============================================================================ #
# The feature record
# ============================================================================ #
@dataclass(frozen=True)
class Feature:
    """One toggleable feature and every surface a switch-off must reach.

    `setting_key` is an EXISTING app.services.settings.DEFAULTS key, never renamed.
    `mcp_gate` equals `setting_key` (the MCP registry documents the gate by the same
    key). `web`/`api`/`hooks` are segment patterns the middleware 404s when the
    feature is removed+off. `cli_groups` are top-level argparse groups the CLI
    dispatch gate refuses. `cards` are capability-card slugs this feature OWNS (Wave 2
    404s them). `seams` are prose pointers at the non-URL, service-layer enforcement
    points a URL sweep can never reach."""

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

    def is_enabled(self) -> bool:
        """True ONLY when the setting reads the canonical on-value "1" -- STRICT, a
        whitelist, byte-for-byte the predicate of every service helper this delegation
        stands in for (image_localization.enabled / keys.enabled / utcp_manual.enabled /
        assist.enabled are all `str(get(...)) == "1"`). A non-canonical value -- a
        hand-set "true"/"on"/"yes", or the documented `config set <key> true` incident
        -- reads as OFF at EVERY layer here (middleware, route gate, nav, card), so this layer can
        never split-brain against the strict services (which would serve a route this
        layer 404s, or vice-versa). Fail toward OFF is the kill-switch-safe direction.
        On an UNREADABLE Setting table (bare import-time contexts) it falls back to
        default_on() -- the same fail-to-default posture the service helpers document."""
        from app import editions

        if not editions.feature_installed(self.slug):
            # Open-core edition: a PAID feature whose add-on code is not installed is OFF,
            # whatever its stored flag says (the Module row and the setting stay; the code
            # is what is missing). Always True when the add-on is present.
            return False
        from app.services import settings

        try:
            return str(settings.get(self.setting_key, "1")) == "1"
        except Exception:
            return self.default_on()

    def default_on(self) -> bool:
        """True when this key ships ON (a real kill switch). A DEFAULT-OFF flag is an
        opt-IN, not a kill switch: 'off' is its normal state, so the middleware and the
        CLI dispatch gate must NOT 404/refuse it (that would change a default install).
        Read from DEFAULTS, so it never depends on runtime state."""
        from app.services import settings

        val = str(settings.DEFAULTS.get(self.setting_key, "")).strip().lower()
        return val not in _OFF


# ============================================================================ #
# THE REGISTRY -- the 11 optional modules, now sourced from the MODULE MANIFEST.
#
# The Feature(...) literals RELOCATED to app/modules.py's MODULES table (composable-
# core phase 5, M1). FEATURES is app.modules.features_view() evaluated ONCE here into a
# module-level tuple CONSTANT (never a property/callable) -- the four lookup dicts below
# derive from that same tuple object, so there is one identity of "the features" behind
# every helper (verify_modules check 3). features_view() reproduces this tuple
# byte-identically (verify_modules check 2 pins it); every incumbent guard stays green.
# ============================================================================ #
FEATURES: tuple[Feature, ...] = features_view()

_BY_SLUG: dict[str, Feature] = {f.slug: f for f in FEATURES}
_BY_KEY: dict[str, Feature] = {f.setting_key: f for f in FEATURES}
#: top-level CLI group -> owning feature (for the __main__ dispatch gate).
_CLI_GROUP_TO_FEATURE: dict[str, Feature] = {g: f for f in FEATURES for g in f.cli_groups}
#: capability-card slug -> the feature that OWNS it (for the card-gating helper). A
#: card each feature declares in `cards`; the guard asserts these resolve to real cards.
_CARD_OWNER: dict[str, Feature] = {c: f for f in FEATURES for c in f.cards}


# ============================================================================ #
# CORE — everything NOT toggleable, classified ON PURPOSE so the guards close.
#
# CORE_API / CORE_CLI are the disjoint complement of the feature patterns above:
# verify_features asserts {feature-claimed} U {CORE_*} == the REAL route table / the
# REAL parser, so a NEW route under a flagged pattern is auto-gated, and a route under
# a new prefix fails the build until it is classified here with an honest reason.
# ============================================================================ #
#: /api path patterns (longest-match; a feature pattern that nests deeper wins) that
#: are CORE — always-on. Reasons per §1 of the plan (the translation pipeline, the
#: identity/forensics spine, and the sections whose own flags land in later waves).
CORE_API: dict[str, str] = {
    "/api/account": "the signed-in user's own account — the identity spine.",
    "/api/admin": "install administration (engines, providers, retention, semantic, "
    "srx, smtp, ldap, oidc, config) — the admin spine; anti-lockout.",
    "/api/apikeys": "API-key credential lifecycle — the identity spine.",
    "/api/mcp-oauth": "a person's own MCP OAuth grants (#753) -- credential lifecycle, the "
    "identity spine.",
    "/api/audit": "the tamper-evident audit trail (export/verify) — the forensics spine.",
    "/api/batches": "fan-out batch runs — part of the translation pipeline.",
    "/api/budgets": "department spend budgets -- CORE, not part of the `routing` feature "
    "(plan Q4): spend control is independent of content-routing (a "
    "budget caps token spend regardless of which workflow routes a "
    "submission). A sibling of /api/routing (not nested), so no carve-out "
    "is needed here -- the routing feature's /api/routing pattern never "
    "matches /api/budgets.",
    "/api/capabilities": "capability cards — orientation/whoami; a feature may never "
    "gate its own re-enable documentation (anti-lockout).",
    "/api/connectors": "core section; individual kinds are enable/disableable via the "
    "connector_disabled_kinds setting (ffadmin Wave 5) -- a disabled "
    "kind's connections/routes 404 per-kind at the connector service "
    "seam, but the SECTION itself stays always-on.",
    "/api/content-types": "content typing — entangled with workflow steps / glossaries.",
    "/api/context": "context-metadata packages — part of the translation pipeline.",
    "/api/customers": "customers — part of the translation pipeline.",
    "/api/documents": "documents + the CAT editor — the translation pipeline itself.",
    "/api/formats": "the supported file-format catalogue — orientation.",
    "/api/glossaries": "glossaries — entangled with workflow steps (glossary enforcement).",
    "/api/jobs": "async job observability — core operability.",
    "/api/langsets": "language sets — part of the translation pipeline.",
    "/api/languages": "the locale catalogue — orientation.",
    "/api/lqa": "the LQA rulebook — the quality spine of the workflow engine.",
    "/api/projects": "projects — the translation pipeline itself (the keys/changes "
    "sub-paths are carved out by the 'keys' feature, which nests deeper).",
    "/api/prompt-templates": "saved prompt templates — workflow authoring.",
    "/api/system-prompts": "read-only built-in pipeline system prompts, for workflow authoring.",
    "/api/prompt-tap": "run-190 Item C + ROADMAP #6: the prompt-tap Ledger bodies-withheld "
    "diagnostic reads -- the action-first SUMMARY (actions list + per-action summary) and the "
    "RUN-first view (/api/prompt-tap/runs list + one run's calls grouped by step then unit). "
    "Read of run/call metadata (counts, finish reasons, error classes, spend) with NO prompt/"
    "response bodies; the raw recorder stays host-only. Core admin/diagnostics spine, not a "
    "toggleable feature.",
    # /api/reports is now the `reports` feature (Wave 4), not core.
    # /api/routing is now the `routing` feature (Wave 4), not core. /api/budgets stays
    # core just below -- spend budgets are a sibling path, independent of content routing.
    "/api/release": "release identity + locked dependency inventory (docs scope) — core "
    "operability / deployment provenance, not a toggleable feature.",
    "/api/formality": "MT formality per target language (#617 item 7) - feeds the translation "
    "prompt and style-guide block, part of the translation pipeline.",
    "/api/notify": "project notification channels and routes (Slack, Teams, signed webhook, "
    "email) - project automation Part B; guarded by the notify_outbound_enabled kill switch.",
    "/api/runs": "workflow-run observability + control — part of the pipeline.",
    "/api/search": "global search over what the caller may open (#595) - orientation.",
    "/api/segments": "segment approve/unlock — the CAT editor.",
    "/api/segprofiles": "segmentation profiles — part of the translation pipeline.",
    "/api/style-guides": "style guides — entangled with workflow steps (style context).",
    "/api/templates": "project templates — part of the translation pipeline.",
    "/api/tm": "translation memory — entangled with workflow steps (TM-match).",
    "/api/translate": "ad-hoc text translation — the translation pipeline itself.",
    "/api/users": "user management — the identity spine.",
    # /api/vendors is now the `vendors` feature (Wave 3), not core.
    "/api/workflow-groups": "workflow groups — part of the translation pipeline.",
    "/api/workflows": "workflows — the translation pipeline itself.",
}

#: top-level CLI group -> reason it is CORE (always-on). The complement of the
#: features' cli_groups; check 4 asserts every parser group is in one set or the other.
CORE_CLI: dict[str, str] = {
    "account": "the signed-in user's own account — identity spine.",
    "admin": "install administration (semantic/srx/retention/ldap/oidc/audit/gdpr/"
    "routing/budget/lqa) — the admin spine.",
    "apikey": "API-key credential lifecycle — identity spine.",
    "audit": "audit-chain verify — forensics spine.",
    "config": "app settings (the same store the Settings UI writes) — admin spine.",
    "connector": "the Connectors section — always-on (per-kind visibility is Wave 5).",
    "content-type": "content typing — entangled with workflows / glossaries.",
    "context": "context-metadata packages — the translation pipeline.",
    "customer": "customers — the translation pipeline.",
    "db": "schema / backup / restore / recover — host operator commands.",
    "doc": "documents + the CAT editor — the translation pipeline itself.",
    "engine": "MT engine profiles — the translation pipeline.",
    "enterprise": "the enterprise turn-on door (OTel/metrics/SAML/SCIM/PII-NER/job-queue/S3 "
    "enable/disable + preflight) — admin spine, the CLI twin of /settings/enterprise.",
    # Batch 13 data-export: the `export` group was a Train-A CORE_CLI PLACEHOLDER; Train B
    # relocates it to the data_export feature's cli_groups (app/modules.py) and REMOVES it here
    # (TA-05: CORE_CLI and feature cli_groups must be disjoint). A writes_off group is never
    # middleware-404'd, so it stays always-registered; the kill switch is service-layer.
    "glossary": "glossaries — entangled with workflow steps.",
    "health": "the DB readiness probe — core operability / anti-lockout.",
    "init": "headless first-admin bootstrap — the identity spine.",
    "job": "async job control/observability — core operability.",
    "lang": "the locale catalogue — orientation.",
    "langset": "language sets — the translation pipeline.",
    "layout": "the signed-in user's own page layouts (train L1): identity spine, like account.",
    "lqa": "reviewer-authored MQM findings (log/list/edit/withdraw) — the quality spine, "
    "core like `tagging`; running a scoring stays web-only (SPEC-wave3 §6.5).",
    "mcp": "the MCP server connect helper — the fourth surface's own launcher.",
    "mcp-oauth": "the opt-in MCP OAuth authorization server (#753): status, resource-server "
    "secret, grants -- identity spine.",
    "member": "project membership — the identity/access spine.",
    "org": "organization management — the identity/tenancy spine.",
    "project": "projects — the translation pipeline itself.",
    "providers": "provider models + keys — the translation pipeline.",
    # P2a release identity (read-only, scope-gated, never a toggleable feature): the CLI twin
    # of the CORE_API `/api/release` claim. `verify_features` check 4 is the first tree to run
    # these two P2a parser groups beside Wave C, so they are classified here (gate round-3 HIGH-2).
    "release": "release inventory + build identity — core operability / deployment provenance.",
    # `report` is now the `reports` feature's CLI group (Wave 4), not core.
    # (`routing` is the routing feature's NEW top-level CLI group, Wave 4 -- it is a
    # feature group, never core; `admin routing rule ...` stays under the core `admin`
    # group as the admin-console twin.)
    "search": "global search over projects, documents, TM and glossary terms (#595): the CLI "
    "twin of the CORE_API `/api/search` claim - orientation, like `lang`.",
    "formality": "MT formality per target language (#617 item 7): the CLI twin of the CORE_API "
    "`/api/formality` claim - it steers the translation prompt itself.",
    "notify": "project notification channels and routes: the CLI twin of the CORE_API "
    "`/api/notify` claim.",
    "segprofile": "segmentation profiles — the translation pipeline.",
    "style-guide": "style guides — entangled with workflow steps.",
    "tagging": "inline-tag verify/repair — the quality spine (never gated off).",
    "template": "project templates — the translation pipeline.",
    "tm": "translation memory — entangled with workflow steps.",
    "user": "user management — the identity spine.",
    "version": "the baked release identity — core operability / deployment provenance.",
    # `vendor` is now the `vendors` feature's CLI group (Wave 3), not core.
    "workflow": "workflows / runs / batches / groups — the translation pipeline itself.",
}

#: CORE web-path patterns that must stay reachable even when a FEATURE whose web pattern
#: is a shorter prefix is switched off -- the web-side longest-match override (the mirror
#: of CORE_API's longest-match on /api). It exists for exactly one case today: department
#: budgets nest UNDER the routing admin page (/routing/budgets*) yet are CORE, not part of
#: the `routing` feature (plan Q4). path_is_off() and the guard's live sweep both
#: resolve a web path to its LONGEST matching pattern, so a more-specific CORE_WEB entry
#: beats the shorter feature pattern -- the middleware 404s /routing when routing is off
#: while /routing/budgets stays reachable. Unlike CORE_API this is NOT part of a disjoint-
#: union accounting (web routes are gated by the middleware, not enumerated against a
#: settings table); it is purely the "core wins the longest match" override, so it lists
#: only the nested-core exceptions, never every core web route.
CORE_WEB: dict[str, str] = {
    "/routing/budgets": "department spend budgets -- CORE, not the `routing` feature "
    "(plan Q4). Nested under the routing admin page, so this "
    "MORE-SPECIFIC pattern wins the longest match over the routing "
    "feature's /routing web pattern: routing OFF 404s /routing but "
    "leaves /routing/budgets (+ /api/budgets) reachable.",
    "/routing/project-budgets": "per-project money budgets (CV-2) -- CORE, not the "
    "`routing` feature. Same carve-out as /routing/budgets: nested "
    "under the routing admin page, this MORE-SPECIFIC pattern wins the "
    "longest match so routing OFF 404s /routing but leaves "
    "/routing/project-budgets reachable.",
}


# ============================================================================ #
# Runtime helpers (imported by the middleware, deps, the CLI gate, the guard)
# ============================================================================ #
def feature_by_slug(slug: str) -> Feature | None:
    return _BY_SLUG.get(slug)


def is_enabled(slug: str) -> bool:
    """Whether feature `slug` is on. An UNKNOWN slug reads as OFF (fail-closed): a template or
    link that names a feature this install does not have must not render a door into a 404.
    (It used to read as enabled; every slug the app itself asks about is registered, which
    verify_features pins.) A paid feature whose add-on is not installed is OFF too, see
    Feature.is_enabled. The Settings toggle route validates the slug separately."""
    f = _BY_SLUG.get(slug)
    return False if f is None else f.is_enabled()


def feature_for_cli_group(group: str) -> Feature | None:
    return _CLI_GROUP_TO_FEATURE.get(group)


REMOVED_REFUSAL = "Not found."


def card_is_off(slug: str) -> bool:
    """True when `slug` names a capability card OWNED by a switched-OFF feature.

    The full-removal doctrine for OWNED cards (plan §3): a disabled feature's card
    404s on /help/{slug} and /api/capabilities/{slug} and drops out of every card
    enumeration (assist.card_slugs filters on this), so the docs go dark with the
    feature. This holds EVEN for a `writes_off` feature (Evaluations, LLMemory): the
    reads of its DATA stay up, but the card is the feature's MANUAL and is removed --
    the card is a doc surface, not a data read. A slug no feature owns is never off
    (returns False), so unrelated cards are untouched. The one authority both card
    routes and the enumeration filter call; never re-derive the owner ad hoc."""
    f = _CARD_OWNER.get((slug or "").strip().lower())
    return f is not None and not f.is_enabled()


def path_is_off(path: str) -> bool:
    """The middleware's one question: is `path` a route of a REMOVED feature that is
    switched off? Only default-ON kill switches enforce (a default-OFF opt-in like
    prompt_tap must never 404 its own page on a default install). Hot path when
    nothing is off: a handful of 2s-cached settings reads and no match work.

    LONGEST-MATCH with a CORE_WEB override: a more-specific CORE_WEB pattern beats a
    shorter switched-off feature pattern, so a CORE path NESTED under a feature's web
    prefix stays reachable when the feature is off. The one live case: /routing/budgets
    (core) under /routing (the routing feature) -- routing OFF 404s /routing but leaves
    /routing/budgets up (plan Q4). Empty CORE_WEB (the pre-Wave-4 state) makes
    this byte-identical to the old any-match behavior."""
    best_off = -1
    for f in FEATURES:
        if f.semantics != "removed" or not f.default_on():
            continue
        if f.is_enabled():
            continue
        for pat in f.web + f.api + f.hooks:
            if _matches(pat, path):
                best_off = max(best_off, _specificity(pat))
    if best_off < 0:
        return False
    # A strictly MORE-SPECIFIC core-web pattern keeps the path reachable (a nested-core
    # carve-out like /routing/budgets under the switched-off /routing feature).
    for pat in CORE_WEB:  # noqa: SIM110
        if _matches(pat, path) and _specificity(pat) > best_off:
            return False
    return True


def api_claimants(path: str) -> list[tuple[int, str, str]]:
    """Every (specificity, kind, key) whose pattern matches an /api `path`, kind in
    {'feature','core'}. The accounting guard takes the max specificity and asserts it
    is UNIQUE (no ties) and that every real route has exactly one such owner."""
    out: list[tuple[int, str, str]] = []
    for f in FEATURES:
        for pat in f.api:
            if _matches(pat, path):
                out.append((_specificity(pat), "feature", f.slug))
    for pat in CORE_API:
        if _matches(pat, path):
            out.append((_specificity(pat), "core", pat))
    return out


def api_owner(path: str) -> tuple[str, str] | None:
    """The single longest-match owner of an /api `path` as (kind, key), or None when
    unclaimed. Raises ValueError on a tie (two patterns of equal top specificity) — a
    tie is a registry defect the guard surfaces, never something to resolve silently."""
    claims = api_claimants(path)
    if not claims:
        return None
    top = max(c[0] for c in claims)
    winners = [c for c in claims if c[0] == top]
    if len(winners) > 1:
        raise ValueError(f"ambiguous owner for {path}: {winners!r}")
    return (winners[0][1], winners[0][2])


# --- the honest semantics sentence for the Settings card (plan §4) ---
# F-HIGH-1: the "removed" sentence no longer claims ALL CLI verbs refuse. A removed
# feature MAY keep an admin-console TWIN of a verb alive under the always-on `admin`
# spine (routing's `admin routing rule ...` — documented in cmd_routing.py); that twin
# is a deliberate exception, so the copy carves it out rather than lying. The clause is
# vacuously true for the removed features that have no such twin.
_SEMANTICS_SENTENCE = {
    "removed": (
        "Switching this off removes its pages and API routes; its own CLI verbs "
        "and MCP tool refuse (any admin-console twin noted on the feature is a "
        "deliberate exception). Data is kept."
    ),
    "writes_off": ("Stops new work and hides the section; existing records stay readable."),
    "opt_in": (
        "Off by default. Turning it on starts the feature; turning it off stops "
        "new activity. Its own controls stay reachable either way."
    ),
}

# F-MED-6: two rows where the per-semantics default is FALSE for that specific feature,
# so they carry an accurate per-slug sentence instead. `databricks` is opt_in but its
# own controls are NON-ENUMERABLE when off (the /admin/databricks page + /api routes
# 404 via the _databricks_gate) — it does NOT "stay reachable either way". `llmemory`
# is writes_off but its nav entry is NOT gated yet (nav_gated=False, a Wave-2 gap), so
# the section is NOT hidden — unlike evaluations, which does hide (nav_gated=True).
_SEMANTICS_SENTENCE_BY_SLUG = {
    "databricks": (
        "Off by default. Turning it on exposes its admin controls and starts "
        "exports; turning it off removes them again (the page 404s). Data "
        "already exported is not recalled."
    ),
    "llmemory": (
        "Stops new writes; existing memories stay readable. The nav entry stays "
        "visible (its removal lands in a later wave)."
    ),
}


def semantics_sentence(feature: Feature) -> str:
    return _SEMANTICS_SENTENCE_BY_SLUG.get(feature.slug) or _SEMANTICS_SENTENCE[feature.semantics]


def _rebrand(text: str) -> str:
    """Substitute each brand-registry DEFAULT coined name with its LIVE value (M4).

    The feature manifest (app/modules.py) is a static, import-cycle-sensitive registry
    that stores the DEFAULT coined names as literals; the /settings + /enterprise feature
    cards render its title/description, so a white-labelled install must see its own names
    HERE too. Defaults-only substitution (customer text can't collide); no-op by default.
    Import branding lazily — features.py stays import-light like its settings use."""
    from app import branding

    if not isinstance(text, str):
        return text
    for key, default in branding.REGISTRY.items():
        live = branding.branded(key)
        if default != live and default in text:
            text = text.replace(default, live)
    return text


def ui_cards() -> list[dict]:
    """The rows the Settings > Features card renders — one per toggleable feature, in
    registry order, each with its live on/off state and honest semantics sentence. Coined
    feature names in the title/description are rebranded to the install's live names (M4)."""
    from app import editions

    return [
        {
            "slug": f.slug,
            "title": _rebrand(f.title),
            "description": _rebrand(f.description),
            "beta": f.beta,
            "semantics": f.semantics,
            "semantics_sentence": _rebrand(semantics_sentence(f)),
            "enabled": f.is_enabled(),
            "setting_key": f.setting_key,
            # Open-core edition: "installed" is False for a paid feature whose add-on code is
            # absent; "edition_label" names the add-on ("" for a core feature) so the
            # Features page can label or hide the row.
            "installed": editions.feature_installed(f.slug),
            "edition_label": editions.edition_label(f.slug),
        }
        for f in FEATURES
    ]
