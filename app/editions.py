"""The EDITION MANIFEST -- which modules are PAID (optional add-on) code, in one place.

Localization OS ships as an open core plus optional add-on packages. The add-on code
lives beside the core in the same tree today and is removed from the public tree later;
this module is the single list both sides read:

  * `app.extensions` loads a provider from here (a missing module reads as "not installed"),
  * `app.features` / `app.modules` read `installed()` so a paid feature whose code is absent
    reads as OFF and the Features page can label it,
  * `tests/verify_core_edition_imports.py` AST-scans every CORE module and fails if one
    imports a PAID module at module level (a missing paid module must never break boot).

Pure data plus `importlib.util.find_spec` probes: it imports nothing from the app, so any
layer (features, modules, extensions, the guard) can import it without a cycle.

Documented in: docs/architecture/modules-and-flags.md
"""

from __future__ import annotations

import importlib.util

#: Paid add-on code, keyed by add-on name. Each value is the dotted module names (a module
#: or a whole package) that make up the add-on. A PACKAGE name covers every module under it.
#: The first entry of each group is its MARKER: the add-on counts as installed when the
#: marker module can be found. Shared libraries the paid features merely use stay core and
#: are NOT listed (eval_metrics*, quality_lens, checks, context_ab, style_ab,
#: jev_inspector, query_threads, scim, saml, the Enterprise page).
PAID_MODULES: dict[str, tuple[str, ...]] = {
    "advanced_workflows": (
        "app.services.advanced",
        "app.web.advanced",
        "app.web.advanced_part1",
        "app.cli.cmd_advanced",
        "app.cli.cmd_advanced_part1",
        "app.cli.cmd_advanced_part1_leaf",
    ),
    "evaluations": (
        "app.services.evaluations",
        "app.services.eval_runner",
        "app.services.eval_runner_common",
        "app.services.eval_runner_part2",
        "app.services.eval_promote",
        "app.services.eval_stats",
        "app.services.quality_meta",
        "app.services.quality_meta_common",
        "app.services.quality_meta_hub",
        "app.services.quality_meta_part2",
        "app.services.quality_meta_part3",
        "app.web.evaluations",
        "app.web.evaluations_part1",
        "app.web.evaluations_part2",
        "app.web.evaluations_part3",
        "app.web.api_cli_evaluations_1",
        "app.web.api_cli_evaluations_2",
        "app.cli.cmd_eval",
        "app.cli.eval_common",
        "app.cli.eval_meta",
        "app.cli.eval_reporting",
    ),
    # Web verification (the Advanced tier's web.search tool): its provider-key store and the
    # Setup AI card. Installed separately from the Advanced package so a core Setup AI page
    # can ask "is the card's backend here?" on its own.
    "web_verification": ("app.services.search_keys",),
    "semantic_tm": ("app.services.embeddings",),
    "databricks": (
        "app.services.databricks_export",
        "app.services.databricks_export_actor_pseudonymization_h",
        "app.services.databricks_transport",
        "app.services.training_pairs",
        "app.web.databricks",
        "app.cli.cmd_databricks",
    ),
    "llmemory": (
        "app.services.llmemory",
        "app.services.llmemory_view",
        "app.web.memories",
        "app.web.api_cli_memories",
        "app.cli.cmd_memory",
    ),
    "llmemory_suggestions": (
        "app.services.llmemory_suggestions",
        "app.services.llmemory_suggestions_gdpr_purge_cited_evidenc",
        "app.services.llmemory_suggestion_view",
        "app.web.memory_suggestions",
        "app.web.api_cli_memory_suggestions",
        "app.cli.cmd_memory_suggestions_subgroup_pha",
        "app.cli.cmd_memory_suggestions_subgroup_pha_leaf",
    ),
    "enterprise_observability": (
        "app.services.observability",
        "app.web.metrics",
    ),
    "enterprise_s3": (
        "app.services.s3_blobstore",
        "app.services.s3_exportstore",
    ),
}

#: Feature slug (app.modules.MODULES) -> the add-on whose code that feature IS. A slug in
#: this table is a PAID feature: it reads as OFF when its add-on is not installed. The
#: Module rows themselves stay (the schema and the flags are core).
PAID_FEATURE_ADDON: dict[str, str] = {
    "advanced_workflows": "advanced_workflows",
    "evaluations": "evaluations",
    "databricks": "databricks",
    "llmemory": "llmemory",
    "llmemory_suggestions": "llmemory_suggestions",
}

#: Human label per add-on (the Features page labels a paid row with this).
ADDON_LABEL: dict[str, str] = {
    "advanced_workflows": "Advanced Workflows",
    "evaluations": "Evaluations Lab",
    "web_verification": "Web verification",
    "semantic_tm": "Semantic TM",
    "databricks": "Databricks export",
    "llmemory": "Memory add-on",
    "llmemory_suggestions": "Workflow suggestions",
    "enterprise_observability": "OpenTelemetry and Prometheus metrics",
    "enterprise_s3": "S3 storage",
}

#: The database tables each add-on owns. The tables stay in the core schema (models and
#: migrations are core), but with the add-on's code absent nothing writes to them and nothing
#: reaps them. `app.extensions.warn_orphaned_addon_data` reads this at boot.
ADDON_TABLES: dict[str, tuple[str, ...]] = {
    "advanced_workflows": (
        "advancedmission",
        "advancedrun",
        "advancedtranscriptentry",
        "advancedstagedtarget",
    ),
    "evaluations": (
        "evaldataset",
        "evaldatasetitem",
        "evalcandidate",
        "evalrun",
        "evalrunitem",
        "evalscore",
    ),
    "semantic_tm": ("segmentembedding",),
    "databricks": ("databricksexportrun",),
    "llmemory": ("memoryentry", "memoryevent", "memoryevidence"),
    "llmemory_suggestions": ("memorysuggestion", "memorysuggestionevidence"),
}

_installed_cache: dict[str, bool] = {}


def _module_present(dotted: str) -> bool:
    try:
        return importlib.util.find_spec(dotted) is not None
    except (ImportError, ValueError, AttributeError):
        return False


def paid_module_names() -> frozenset[str]:
    """Every dotted module (or package) name that is paid code."""
    return frozenset(m for mods in PAID_MODULES.values() for m in mods)


def is_paid_module(dotted: str) -> bool:
    """True when `dotted` is a paid module or lives under a paid package."""
    return any(dotted == p or dotted.startswith(p + ".") for p in paid_module_names())


def addon_installed(addon: str) -> bool:
    """True when the add-on's marker module (first entry) is importable. Cached; the
    answer cannot change inside a process short of installing code."""
    hit = _installed_cache.get(addon)
    if hit is None:
        mods = PAID_MODULES.get(addon)
        hit = bool(mods) and _module_present(mods[0])
        _installed_cache[addon] = hit
    return hit


def module_installed(dotted: str) -> bool:
    """True when `dotted` can be found. Non-paid names are always treated as present."""
    if not is_paid_module(dotted):
        return True
    hit = _installed_cache.get(dotted)
    if hit is None:
        hit = _module_present(dotted)
        _installed_cache[dotted] = hit
    return hit


def is_paid_feature(slug: str) -> bool:
    return slug in PAID_FEATURE_ADDON


def feature_installed(slug: str) -> bool:
    """False only for a PAID feature whose add-on code is absent. Every core feature is
    always 'installed'."""
    addon = PAID_FEATURE_ADDON.get(slug)
    return True if addon is None else addon_installed(addon)


def edition_label(slug: str) -> str:
    """'' for a core feature; the add-on's label for a paid one."""
    addon = PAID_FEATURE_ADDON.get(slug)
    return "" if addon is None else ADDON_LABEL.get(addon, addon)


def edition() -> str:
    """'full' when every feature add-on is present, else 'core'. Informational."""
    return "full" if all(addon_installed(a) for a in set(PAID_FEATURE_ADDON.values())) else "core"
