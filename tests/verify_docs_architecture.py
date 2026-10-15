"""The DOC-TRUTH guard -- proves the open-architecture documentation cannot rot silently.

WHAT THIS GUARDS (PROPOSAL-open-architecture-docs.md sec L3). The architecture docs make a
promise: every architectural claim is pinned to CODE, and a reader (human or LLM) descends
root map -> subsystem doc -> module -> the code and its verify suites. A doc whose pins have
drifted is worse than no doc. This suite bakes the text-truth sweep into CI so the promise is
DURABLE, the same way verify_modules.py makes the module registry's consolidation durable.

THE LAYERS IT POLICES
  * L0 -- the map: root ARCHITECTURE.md + llms.txt.
  * L1 -- the subsystem docs: docs/architecture/<subsystem>.md (README.md and _*.md are
    authoring scaffolding, NOT subsystem docs, and are excluded from the L1 set).
  * L2 -- in-code module docstrings (the `Documented in:` back-pointer).

THE CHECKS (docs scanned = L0 ARCHITECTURE.md + llms.txt, every L1 subsystem doc, AND the
authoring scaffolding README.md + _TEMPLATE.md)
  1  reference resolution, three pin carriers, none fail-open:
       * inline-code `...`  -- REPO-ROOT-relative path / path::symbol (or Class.method), setting:,
         verify_ suite. A span carrying `::` or ending in .py/.md is an INTENDED pin and FAILS
         when unresolvable; backslashes normalize, a leading ./ strips; <placeholder>/*glob spans
         are prose.
       * markdown link [text](tgt) -- target is DOC-DIR-relative (../../ from an L1 doc), must
         exist and stay INSIDE the repo; a backticked-identifier TEXT over a .py target ALSO
         resolves that identifier as a symbol (unless it is the file's own stem).
       * bare path token anywhere (mermaid + fences) -- app|tests|docs|... /...ext must exist;
         and a hard-wrapped (line-split) inline-code span is detected and failed.
     Line-number patterns (file.ext:N, #N/@N/L N attached to a filename, #Lnn, :Lnn, "line N",
     "lines N-M") FAIL; URLs are stripped first and the L0/L1/L2/L3 layer terms never trip.
     [self-tested end-to-end: fabricated path/symbol/setting/suite/[SYMBOL](file)/link/bare-token
      pins, repo-escapes, malformed fail-open cases, hard-wraps, and every line-number form are
      each proven caught; real pins and clean prose pass.]
  2  coverage ratchet (two-sided, the OPTIONAL_SLUGS doctrine): every in-scope module
     (app/services, app/web, app/cli, app/ports, app/kernel -- .py plus any
     SUBSTANTIVE __init__.py) is EITHER claimed by exactly one L1 doc's "Where the work happens"
     TABLE ROW (a prose list does NOT count) OR listed in DOC_ALLOWLIST with a reason. A new
     module with no home fails CI. The DOCUMENTED set may only GROW (>= _DOCUMENTED_FLOOR) and the
     allowlist may only SHRINK (<= _ALLOWLIST_CEILING); a post-seed allowlist entry needs an
     AUTHORED reason (not the auto-filled seed). Spine app-root modules and integrations/mcp-server
     are excluded WITH STATED REASONS (see _SPINE_EXCLUDED). [self-tested: undoc-module, double-
     cover, floor-regress, seed-reason, ceiling, and prose-vs-table sabotage each proven caught.]
  3  section contract: every L1 doc carries the 7 required sections. [self-tested.]
  4  single-claim: no module is claimed by two different L1 docs. [self-tested.]
  5  L2 back-pointer: every module CLAIMED by an L1 doc carries, IN ITS MODULE DOCSTRING (not a
     comment or stray literal), a `Documented in: docs/architecture/<doc>.md` line. [self-tested.]
  6  index integrity: every existing L1 doc is listed in BOTH ARCHITECTURE.md and llms.txt.
     [self-tested: an unlisted L1 doc is proven to be caught.]
  Plus: no non-scaffolding doc under docs/architecture/ may hide behind a leading underscore.

Phase 4 note (Batch 11): the ratchet is CLOSED. Every in-scope module is now either claimed by an
L1 doc's "Where the work happens" table (pinned in _DOCUMENTED_FLOOR) or carries an AUTHORED reason
in _AUTHORED_ALLOWLIST. The anonymous Phase-0 seed (_ALLOWLISTED_MODULES) is EMPTY, so DOC_ALLOWLIST
holds only genuinely-exempt files (connector adapters and their support leaves, two infrastructure
adapters/leaves, and four cross-cutting CLI consoles), each with an authored one-line reason. The
guard distinguishes the two: _PHASE0_SEED_MODULES is the (now empty) seed snapshot the authored-
reason law reads against, so any allowlist entry must carry a non-seed, non-empty reason, and
check_coverage_ratchet asserts the anonymous seed is zero. The standing rule from here: no new
module ships without a documentation home (a doc table row, or an authored allowlist reason).

KNOWN LIMITATIONS (round-2 gate AD-103/105/106/107, accepted as residual risk for Phase 1 --
recorded, not fixed; revisit if a Phase-1 gate trips on one):
  * AD-103: the hard-wrap detector counts backticks per line, so an inline ``` / ~~~ fence run,
    or a deliberately unbalanced-backtick construct, can false-positive; keep inline code spans
    on one line and fenced blocks on their own lines.
  * AD-105: content inside a markdown link's TEXT (other than a lone identifier for the AD-02
    symbol inference) is not scanned as pins.
  * AD-106: a table row with >=2 non-empty cells still "claims" even if the description cells are
    junk, and one row may claim multiple modules -- the ratchet checks placement, not prose quality.
  * AD-107: a span containing a < > or * placeholder/glob is skipped WHOLE, so a real pin sharing
    a span with a placeholder is not resolved.

Hermetic + offline (throwaway LOC_TMS_DATA_DIR, no network, no DB writes -- only settings.DEFAULTS
is read, for setting-key resolution). ASCII-only prints. Final line "DOCS ARCHITECTURE OK". Run
through tests/test_verify_suites.py with the explicit suite-name chunk.
"""

import ast
import os
import re
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(_ROOT)
sys.path.insert(0, _ROOT)
from tests._bootstrap import init_env

init_env("loctms_docs_arch_")

from app.services import settings

# --------------------------------------------------------------------------- #
# The layers, on disk.
# --------------------------------------------------------------------------- #
_ARCH_MD = os.path.join(_ROOT, "ARCHITECTURE.md")
_LLMS_TXT = os.path.join(_ROOT, "llms.txt")
_L1_DIR = os.path.join(_ROOT, "docs", "architecture")

#: The six in-scope package roots for the L2 coverage ratchet (PROPOSAL sec L2).
_L2_SCOPE = ("app/services", "app/web", "app/cli", "app/ports", "app/kernel")

# AD-08: SCOPE BOUNDARIES, stated (not silent). The universe = every .py in _L2_SCOPE, INCLUDING
# a SUBSTANTIVE __init__.py (one whose AST body is more than a module docstring / __future__
# import -- e.g. app/kernel/__init__.py's meta_path finder, app/web/__init__.py's router
# composition, app/ports/__init__.py's registry). A trivial/empty __init__.py (app/services,
# app/cli) is a package marker with no behavior to document and stays out. DELIBERATELY EXCLUDED,
# each with a reason:
#   * The app-root SPINE modules (app/config.py, app/db.py, app/models.py, app/features.py,
#     app/modules.py, app/main.py, app/logging_config.py) -- they are outside the six package
#     roots the proposal scopes; they are documented FROM the L0 map and will be CLAIMED (not
#     forced) by the Phase 2 data-and-migrations / configuration / kernel-and-ports docs. A claim
#     on a spine module is honored (it just is not required). Recorded in PROPOSAL sec 7.
#   * integrations/mcp-server/ -- the fourth surface lives OUTSIDE app/ (the app imports no MCP
#     code); it carries its own README + registry and is documented by the Phase 3
#     surfaces-and-parity doc.
_SPINE_EXCLUDED = (
    "app/config.py",
    "app/db.py",
    "app/models.py",
    "app/features.py",
    "app/modules.py",
    "app/main.py",
    "app/logging_config.py",
)

#: The 7-section contract every L1 subsystem doc must satisfy (PROPOSAL sec L1). Compared
#: against each doc's headings, normalized (leading '#', numbering, case, whitespace stripped).
_REQUIRED_SECTIONS = (
    "role",
    "invariants",
    "where the work happens",
    "data & settings",
    "enforced by",
    "battle scars",
    "limits & accepted risks",
)

# The top-level repo directories a slash-path pin may point into, plus the root files a bare-name
# pin may name. A backtick span that does not start with one of these (and has no '/') is prose,
# not a pin, and is left alone -- keeping the guard real without flagging ordinary words.
_PATH_ROOTS = (
    "app/",
    "tests/",
    "integrations/",
    "alembic/",
    "scripts/",
    "docs/",
    "planning/",
    "tools/",
    "assets/",
    "deploy/",
)
_ROOT_FILES = {
    "ARCHITECTURE.md",
    "llms.txt",
    "README.md",
    "PRODUCT.md",
    "HANDOFF.md",
    "DESIGN.md",
    "fly.toml",
    "pyproject.toml",
    "alembic.ini",
    "Dockerfile",
    "docker-compose.yml",
}

# --------------------------------------------------------------------------- #
# THE COVERAGE RATCHET (grow-only, the app/modules.py OPTIONAL_SLUGS doctrine).
#
# DOC_ALLOWLIST: {module_path: reason} -- every in-scope module that has no subsystem doc yet.
# A NEW module absent from both this allowlist and every L1 doc's table fails check 2, so the
# allowlist is a CHECKED-IN, explicit list (never computed) -- that is what makes the ratchet
# non-vacuous. Each entry carries a non-empty reason. The allowlist SHRINKS as docs are written.
#
# _DOCUMENTED_FLOOR: the modules that MUST be claimed by an L1 doc -- a grow-only pin, exactly
# like _OPTIONAL_SLUGS_FLOOR in verify_modules.py. Empty in Phase 0; every phase that writes a
# doc adds that doc's modules here, so a later change can never silently un-document a module.
# --------------------------------------------------------------------------- #
_PHASE0_SEED = (
    "Phase 0 seed: in-scope module with no subsystem doc yet. Batch 11 Phase 1-4 will move it "
    "into a docs/architecture/<subsystem>.md 'Where the work happens' table (and into "
    "_DOCUMENTED_FLOOR); until then it is explicitly, visibly un-documented -- not silently so."
)

#: PHASE 4 (Batch 11): the anonymous Phase-0 seed is CLOSED. Every in-scope module is now
#: either claimed by an L1 doc's 'Where the work happens' table (and pinned in
#: _DOCUMENTED_FLOOR) or carries an AUTHORED reason in _AUTHORED_ALLOWLIST below. This tuple
#: is deliberately EMPTY so _PHASE0_SEED_MODULES is empty: any allowlist entry must carry an
#: authored (non-seed, non-empty) reason, and check_coverage_ratchet asserts the seed is zero.
_ALLOWLISTED_MODULES: tuple = ()
#: The Phase-0 seed set -- exactly the modules seeded above with the shared auto-filled reason.
#: This is the SNAPSHOT the authored-reason law reads against (AD-09). It is deliberately built
#: from _ALLOWLISTED_MODULES, which is the FROZEN Phase-0 seed tuple and MUST NOT grow: a new
#: undocumented module is added to _AUTHORED_ALLOWLIST below (NOT here), so it lands OUTSIDE this
#: set and the authored-reason branch actually fires on it (round-2 AD-100 -- previously the seed
#: set aliased a mutable seed list, so the branch could never fire in production).
_PHASE0_SEED_MODULES: frozenset = frozenset(_ALLOWLISTED_MODULES)

#: AD-100 (option B): post-seed allowlist additions live HERE, each with an AUTHORED reason
#: explaining why the module has no doc yet. A module added to this dict is NOT in
#: _PHASE0_SEED_MODULES, so an entry carrying the auto-filled seed reason (or an empty one) is
#: rejected. New undocumented modules (from batches that merge AFTER this Phase-0 seed) go here,
#: not into the frozen _ALLOWLISTED_MODULES.
_AUTHORED_ALLOWLIST: dict[str, str] = {
    # Concrete connector adapters and their support leaves: plugins on the connector
    # framework documented in docs/architecture/connectors.md, not distinct subsystems
    # (connectors.md sec 1 and sec 7 defer the individual adapters by design).
    "app/services/connectors/confluence.py": (
        "concrete Confluence connector adapter; a plugin on the connector framework "
        "documented in docs/architecture/connectors.md."
    ),
    "app/services/connectors/document360.py": (
        "concrete Document360 connector adapter; a plugin on the connector framework "
        "documented in docs/architecture/connectors.md."
    ),
    "app/services/connectors/freshdesk.py": (
        "concrete Freshdesk connector adapter; a plugin on the connector framework documented "
        "in docs/architecture/connectors.md."
    ),
    "app/services/connectors/harvest.py": (
        "concrete Harvest connector adapter; a plugin on the connector framework documented "
        "in docs/architecture/connectors.md."
    ),
    "app/services/connectors/helpscout.py": (
        "concrete Help Scout connector adapter; a plugin on the connector framework "
        "documented in docs/architecture/connectors.md."
    ),
    "app/services/connectors/hubspot.py": (
        "concrete HubSpot connector adapter; a plugin on the connector framework documented "
        "in docs/architecture/connectors.md."
    ),
    "app/services/connectors/intercom.py": (
        "concrete Intercom connector adapter; a plugin on the connector framework documented "
        "in docs/architecture/connectors.md."
    ),
    "app/services/connectors/notion.py": (
        "concrete Notion connector adapter; a plugin on the connector framework documented in "
        "docs/architecture/connectors.md."
    ),
    "app/services/connectors/servicenow.py": (
        "concrete ServiceNow connector adapter; a plugin on the connector framework documented "
        "in docs/architecture/connectors.md."
    ),
    "app/services/connectors/wordpress.py": (
        "concrete WordPress connector adapter; a plugin on the connector framework documented "
        "in docs/architecture/connectors.md."
    ),
    "app/services/connectors/zendesk.py": (
        "concrete Zendesk connector adapter; a plugin on the connector framework documented "
        "in docs/architecture/connectors.md."
    ),
    "app/services/connectors/zoho.py": (
        "concrete Zoho connector adapter; a plugin on the connector framework documented in "
        "docs/architecture/connectors.md."
    ),
    "app/services/connectors/airtable_webhooks.py": (
        "Airtable webhook support for the Airtable connector adapter; a plugin-support leaf "
        "on the connector framework documented in docs/architecture/connectors.md."
    ),
    "app/services/connectors/notion_blocks.py": (
        "Notion block helpers for the Notion connector adapter; a plugin-support leaf on the "
        "connector framework documented in docs/architecture/connectors.md."
    ),
    "app/services/connectors/fieldmap.py": (
        "the shared field-map helper for connector adapters; a plugin-support leaf on the "
        "connector framework documented in docs/architecture/connectors.md."
    ),
    "app/services/connectors/idmap.py": (
        "the external-to-internal id map for doc-per-locale connector adapters; a "
        "plugin-support leaf on the connector framework documented in "
        "docs/architecture/connectors.md."
    ),
    "app/services/connectors/oauth.py": (
        "the shared OAuth helper for connector adapters; a plugin-support leaf on the "
        "connector framework documented in docs/architecture/connectors.md."
    ),
    "app/services/connectors/tracking.py": (
        "the connector sync tracking helper; a plugin-support leaf on the connector framework "
        "documented in docs/architecture/connectors.md."
    ),
    "app/services/connectors/webhooks.py": (
        "the shared inbound-webhook helper for connector adapters; a plugin-support leaf on "
        "the connector framework documented in docs/architecture/connectors.md."
    ),
    # Infrastructure adapters and a leaf lookup, plus cross-cutting CLI consoles: thin
    # shells over already-documented services, no architecture of their own.
    "app/services/mime.py": (
        "pure Document.fmt to Content-Type lookup leaf relocated into the kernel; no "
        "subsystem of its own, re-exported by the web deps shim."
    ),
    "app/cli/cmd_account.py": (
        "thin self-service account CLI over "
        "app/services/onboarding.py; adds no architecture beyond those documented services."
    ),
    "app/cli/cmd_init.py": (
        "install bootstrap CLI over init_db and security.seed_admin; a thin shell over "
        "data-and-migrations and identity-and-access."
    ),
}

DOC_ALLOWLIST = {**dict.fromkeys(_ALLOWLISTED_MODULES, _PHASE0_SEED), **_AUTHORED_ALLOWLIST}

#: AD-09: the ALLOWLIST CEILING -- the symmetric partner of the grow-only floor. The allowlist
#: may only SHRINK (docs get written); it can never grow past this count without a deliberate,
#: reviewed bump of this constant. So a new undocumented module cannot be hidden by quietly
#: enlarging the allowlist -- the author must either document it or consciously raise the ceiling.
_ALLOWLIST_CEILING = len(DOC_ALLOWLIST)

#: Grow-only floor: modules that MUST be claimed by an L1 doc. Empty in Phase 0. Each later
#: phase appends its documented modules here; the guard asserts claimed >= this floor, so a
#: module that has EARNED a doc can never regress to allowlist-only.
#: Open core (S2, 2026-10-14): the floor entries of the 70 PAID modules (app/editions.py) went
#: when that code left the public tree for the add-on repos, with their L1 docs. Reviewed shrink.
_DOCUMENTED_FLOOR: frozenset = frozenset(
    {
        "app/services/maintenance.py",
        "app/cli/cmd_release.py",
        # PHASE 4 (Batch 11): the ratchet is closed. This floor pins EVERY in-scope module
        # an L1 doc claims (Phases 1-4). It is grow-only: a claimed module can never regress
        # to allowlist-only. Generated from the live 'Where the work happens' tables, grouped
        # by the L1 doc that claims each module so a floor regression names its owning doc.
        # advanced-workflows (35)
        "app/services/checks.py",
        "app/services/quality_lens.py",
        "app/services/quality_lens_part2.py",
        "app/services/quality_lens_seed_constants_copies_of.py",
        # assistant (4)
        "app/cli/cmd_ask.py",
        "app/services/assist.py",
        "app/services/assist_settings_keys_registered.py",
        "app/services/assist_settings_keys_registered_leaf.py",
        # audit-chain (2)
        "app/services/audit.py",
        "app/services/audit_break_class_helpers_log_.py",
        # backups-and-dr (3)
        "app/cli/backup_archive.py",
        "app/cli/cmd_db.py",
        "app/cli/cmd_db_db_init_schema.py",
        # browser-surface (1)
        "app/web/security_headers.py",
        # collaboration-and-review (7)
        "app/cli/cmd_query.py",
        "app/cli/cmd_work.py",
        "app/services/query_threads.py",
        "app/services/segment_review.py",
        "app/services/worklist.py",
        "app/web/query_threads.py",
        "app/web/worklist.py",
        # configuration (11)
        "app/cli/cmd_config.py",
        "app/cli/cmd_enterprise.py",
        "app/config.py",
        "app/services/enterprise_enablement.py",
        "app/services/settings.py",
        "app/services/settings_common.py",
        "app/web/admin.py",
        "app/web/admin_common.py",
        "app/web/admin_part2.py",
        "app/web/admin_part3.py",
        "app/web/enterprise.py",
        # connectors (49)
        "app/cli/cmd_admin.py",
        "app/cli/cmd_admin_helpers.py",
        "app/cli/cmd_connector.py",
        "app/cli/cmd_prompt_tap.py",
        "app/cli/prompt_tap_common.py",
        "app/cli/prompt_tap_verify.py",
        "app/services/budgets.py",
        "app/services/connectors/__init__.py",
        "app/services/connectors/airtable.py",
        "app/services/connectors/airtable_part1.py",
        "app/services/connectors/airtable_part2.py",
        "app/services/connectors/akeneo.py",
        "app/services/connectors/akeneo_part1.py",
        "app/services/connectors/base.py",
        "app/services/connectors/contentful.py",
        "app/services/connectors/contentful_part1.py",
        "app/services/connectors/contentful_part2.py",
        "app/services/connectors/contract.py",
        "app/services/connectors/egress_guard.py",
        "app/services/connectors/flow.py",
        "app/services/connectors/jira.py",
        "app/services/connectors/jira_part1.py",
        "app/services/connectors/jira_part2.py",
        "app/services/connectors/salesforce.py",
        "app/services/connectors/salesforce_common.py",
        "app/services/connectors/salesforce_part2.py",
        "app/services/connectors/shopify.py",
        "app/services/connectors/shopify_part1.py",
        "app/services/connectors/shopify_part2.py",
        "app/services/connectors/store.py",
        "app/services/connectors/store_common.py",
        "app/services/connectors/store_common_leaf.py",
        "app/services/connectors/store_part2.py",
        "app/services/connectors/store_part3.py",
        "app/services/connectors/store_part4.py",
        "app/services/connectors/strapi.py",
        "app/services/connectors/strapi_field_classification_heu.py",
        "app/services/connectors/strapi_part2.py",
        "app/services/connectors/strategy.py",
        "app/services/prompt_tap.py",
        "app/services/tap_enrich.py",
        "app/services/tap_enrich_part1.py",
        "app/services/tap_envelope.py",
        "app/services/tap_envelope_common.py",
        "app/services/tap_runs.py",
        "app/services/tap_view.py",
        "app/web/connectors.py",
        "app/web/connectors_common.py",
        # content-and-projects (23)
        "app/cli/cmd_content_type.py",
        "app/cli/cmd_lang.py",
        "app/cli/cmd_project.py",
        "app/cli/cmd_project_glossary.py",
        "app/cli/cmd_project_part2.py",
        "app/cli/cmd_project_part5.py",
        "app/cli/cmd_project_part6.py",
        "app/cli/cmd_project_project.py",
        "app/cli/cmd_project_term_extraction_candidat.py",
        "app/services/content.py",
        "app/services/content_part2.py",
        "app/services/content_project_rename_delete_sp.py",
        "app/services/content_projects.py",
        "app/services/content_types.py",
        "app/services/languages.py",
        "app/services/languages_catalog.py",
        "app/services/lifecycle.py",
        "app/services/project_sources.py",
        "app/web/content_types.py",
        "app/web/projects.py",
        "app/web/projects_glossary.py",
        "app/web/projects_project_rename_delete_sp.py",
        "app/web/projects_projects.py",
        # context-and-adaptation (6)
        "app/cli/cmd_context.py",
        "app/services/adaptive.py",
        "app/services/context_ab.py",
        "app/services/context_pkg.py",
        # cost-and-budgets (6)
        "app/cli/cmd_routing.py",
        "app/services/pricing.py",
        "app/services/project_budgets.py",
        "app/services/routing.py",
        "app/services/spend.py",
        "app/web/routing.py",
        # data-and-migrations (6)
        "app/db.py",
        "app/models.py",
        "app/services/gdpr.py",
        "app/services/gdpr_common.py",
        "app/services/ids.py",
        "app/services/retention.py",
        # data-export (13)
        "app/cli/cmd_export.py",
        "app/ports/exportstore.py",
        "app/services/data_export.py",
        "app/services/data_export_part1.py",
        "app/services/data_export_part2.py",
        "app/services/data_export_part3.py",
        "app/web/exports.py",
        # deployment (3)
        "app/logging_config.py",
        "app/services/tailnet.py",
        "app/web/health.py",
        # engines-and-providers (12)
        "app/cli/cmd_engine.py",
        "app/cli/cmd_providers.py",
        "app/services/engines.py",
        "app/services/llm_json.py",
        "app/services/pair_providers.py",
        "app/services/prompts.py",
        "app/services/providers.py",
        "app/services/providers_common.py",
        "app/services/providers_hub.py",
        "app/services/providers_part2.py",
        "app/services/providers_part3.py",
        "app/services/providers_part4.py",
        # identity-and-access (13)
        "app/cli/cmd_user.py",
        "app/services/access.py",
        "app/services/apikeys.py",
        "app/services/mfa.py",
        "app/services/password_policy.py",
        "app/services/ratelimit.py",
        "app/services/security.py",
        "app/services/security_client_ip_one_authority_.py",
        "app/services/trust_auth.py",
        "app/web/apikeys.py",
        "app/web/auth.py",
        "app/web/auth_mfa.py",
        "app/web/deps.py",
        # image-localization (6)
        "app/cli/cmd_image.py",
        "app/services/image_localization.py",
        "app/services/image_localization_part1.py",
        "app/services/key_images.py",
        "app/services/ocr.py",
        "app/web/images.py",
        # incident-response-and-observability (4)
        "app/services/mailer.py",
        "app/services/notifications.py",
        # inline-tags (4)
        "app/cli/cmd_tagging.py",
        "app/services/ai_tagging.py",
        "app/services/tag_llm_repair.py",
        "app/services/tag_repair.py",
        # jobs-and-scheduler (5)
        "app/cli/cmd_job.py",
        "app/services/job_history.py",
        "app/services/jobs.py",
        "app/services/scheduler.py",
        "app/web/jobs_ui.py",
        # project-automation (6)
        "app/cli/cmd_project_automation.py",
        "app/services/automation_schedule.py",
        "app/services/connected_projects.py",
        "app/services/connectors/run_ctx.py",
        "app/services/project_automation.py",
        "app/web/project_automation.py",
        # kernel-and-ports (12)
        "app/kernel/__init__.py",
        "app/kernel/pipeline.py",
        "app/ports/__init__.py",
        "app/ports/_selftest_probe.py",
        "app/ports/blobstore.py",
        "app/ports/identity.py",
        "app/ports/jobqueue.py",
        "app/ports/relational.py",
        "app/ports/scheduler.py",
        "app/ports/secrets.py",
        "app/ports/telemetry.py",
        "app/services/_selftest_reverse_probe.py",
        # llmemory (10)
        # modules-and-flags (5)
        "app/editions.py",
        "app/extensions.py",
        "app/features.py",
        "app/modules.py",
        "app/services/features_admin.py",
        # project-setup-and-templates (6)
        "app/cli/cmd_template.py",
        "app/services/onboarding.py",
        "app/services/setup_author.py",
        "app/services/setup_author_helpers.py",
        "app/services/setup_author_part2.py",
        "app/services/templates.py",
        # quality-lqa-evaluations (30)
        "app/cli/cmd_lqa.py",
        "app/services/eval_metrics.py",
        "app/services/eval_metrics_judge_qe_qe_score_segmen.py",
        "app/services/eval_metrics_the_result_type.py",
        "app/services/lqa.py",
        "app/services/lqa_attribution.py",
        "app/services/lqa_downstream_findings_bloc.py",
        "app/services/lqa_safety.py",
        "app/services/postedit.py",
        "app/services/qa.py",
        "app/services/qe.py",
        "app/services/quality.py",
        "app/services/quality_common.py",
        "app/services/quality_hub.py",
        "app/services/quality_part2.py",
        "app/services/scaffold.py",
        # reporting-and-dashboard (8)
        "app/cli/cmd_report.py",
        "app/services/reports.py",
        "app/services/reports_csv.py",
        "app/services/reports_part3.py",
        "app/services/reports_the_query.py",
        "app/services/reports_the_query_leaf.py",
        "app/web/home.py",
        "app/web/queries.py",
        # secrets-and-crypto (9)
        "app/services/cloud_keys.py",
        "app/services/credential_origin.py",
        "app/services/dbcrypt.py",
        "app/services/egress.py",
        "app/services/pii.py",
        "app/services/secrets_scrub.py",
        "app/services/storage.py",
        "app/services/vault.py",
        # segmentation (4)
        "app/cli/cmd_segprofile.py",
        "app/services/segment.py",
        "app/services/segmentation_profiles.py",
        "app/services/srx.py",
        # sso-provisioning (6)
        "app/cli/cmd_scim.py",
        "app/services/ldap_auth.py",
        "app/services/oidc.py",
        "app/services/saml.py",
        "app/services/scim.py",
        "app/web/scim.py",
        # mcp-oauth (4)
        "app/cli/cmd_mcp_oauth.py",
        "app/services/mcp_as.py",
        "app/services/mcp_as_common.py",
        "app/web/mcp_oauth_as.py",
        # string-keys (18)
        "app/cli/cmd_keys.py",
        "app/cli/keys_common.py",
        "app/cli/keys_import_export.py",
        "app/services/key_files.py",
        "app/services/key_files_export.py",
        "app/services/key_files_json_parse_render.py",
        "app/services/key_files_po_parse_render.py",
        "app/services/key_files_po_parse_render_leaf.py",
        "app/services/keys.py",
        "app/services/keys_common.py",
        "app/services/keys_hub.py",
        "app/services/keys_hub2.py",
        "app/services/keys_hub3.py",
        "app/services/keys_part2.py",
        "app/web/keys.py",
        "app/web/keys_common.py",
        "app/web/keys_hub.py",
        "app/web/keys_hub2.py",
        # style-guides (4)
        "app/cli/cmd_style_guide.py",
        "app/services/style_ab.py",
        "app/services/style_guides.py",
        "app/web/style_guides.py",
        # surfaces-and-parity (46)
        "app/cli/__main__.py",
        "app/cli/_common.py",
        "app/cli/_remote.py",
        "app/cli/_remote_impl.py",
        "app/cli/_remote_impl_common.py",
        "app/cli/_remote_impl_common_leaf.py",
        "app/cli/_remote_impl_part2.py",
        "app/cli/_remote_impl_part3.py",
        "app/cli/_remote_impl_part4.py",
        "app/cli/_remote_impl_part5.py",
        "app/cli/_remote_impl_part6.py",
        "app/cli/_remote_impl_part7.py",
        "app/cli/cmd_mcp.py",
        "app/services/utcp_manual.py",
        "app/web/__init__.py",
        "app/web/api.py",
        "app/web/api_cli.py",
        "app/web/api_cli_admin_1.py",
        "app/web/api_cli_admin_2.py",
        "app/web/api_cli_common.py",
        "app/web/api_cli_customers.py",
        "app/web/api_cli_glossaries_1.py",
        "app/web/api_cli_glossaries_2.py",
        "app/web/api_cli_misc_1.py",
        "app/web/api_cli_misc_2.py",
        "app/web/api_cli_misc_3.py",
        "app/web/api_cli_misc_4.py",
        "app/web/api_cli_misc_5.py",
        "app/web/api_cli_misc_6.py",
        "app/web/api_cli_misc_7.py",
        "app/web/api_cli_projects_1.py",
        "app/web/api_cli_projects_2.py",
        "app/web/api_cli_users.py",
        "app/web/api_cli_vendors.py",
        "app/web/api_cli_workflows_1.py",
        "app/web/api_cli_workflows_2.py",
        "app/web/api_discovery.py",
        "app/web/api_discovery_common.py",
        "app/web/api_part2.py",
        "app/web/api_part3.py",
        "app/web/help.py",
        "app/web/utcp.py",
        # tenancy-confinement (5)
        "app/cli/cmd_org.py",
        "app/services/customers.py",
        # terminology (9)
        "app/services/glossary_concepts.py",
        "app/services/glossary_concepts_common.py",
        "app/services/glossary_concepts_part2.py",
        "app/services/glossary_concepts_part3.py",
        "app/services/glossary_concepts_part4.py",
        "app/services/glossary_concepts_part5.py",
        "app/services/glossary_tbx.py",
        "app/services/tbx_conformance.py",
        "app/services/term_extraction.py",
        # tm-and-glossary (16)
        "app/services/glossary.py",
        "app/services/glossary_common.py",
        "app/services/glossary_hub.py",
        "app/services/glossary_module.py",
        "app/services/glossary_module_locale_canonicalization_.py",
        "app/services/glossary_part2.py",
        "app/services/glossary_part3.py",
        "app/services/tm.py",
        "app/services/tm_common.py",
        "app/services/tm_hub.py",
        "app/services/tm_lock.py",
        "app/services/tm_module.py",
        "app/services/tm_part2.py",
        "app/web/glossaries.py",
        "app/web/glossaries_approval_can_tune_ai.py",
        "app/web/tm.py",
        # translation-pipeline (32)
        "app/cli/cmd_doc.py",
        "app/cli/doc_common.py",
        "app/cli/doc_inspect.py",
        "app/services/delivery.py",
        "app/services/export.py",
        "app/services/export_gate.py",
        "app/services/fulltext_paragraphs.py",
        "app/services/ingest.py",
        "app/services/orchestrator.py",
        "app/services/orchestrator_part10.py",
        "app/services/orchestrator_part2.py",
        "app/services/orchestrator_part3.py",
        "app/services/orchestrator_part4.py",
        "app/services/orchestrator_part5.py",
        "app/services/orchestrator_part6.py",
        "app/services/orchestrator_part7.py",
        "app/services/orchestrator_part8.py",
        "app/services/orchestrator_part9.py",
        "app/services/orchestrator_terminology_backen.py",
        "app/services/refusal.py",
        "app/services/refusal_common.py",
        "app/services/refusal_part2.py",
        "app/services/tm_delivery.py",
        "app/services/tm_delivery_the_three_valued_gate_de.py",
        "app/services/translate.py",
        "app/services/translate_common.py",
        "app/services/translate_hub.py",
        "app/services/translate_hub2.py",
        "app/web/documents.py",
        "app/web/documents_document_level_context_o.py",
        "app/web/documents_part3.py",
        "app/web/documents_tm_only_delivery_empty_g.py",
        # vendor-outsourcing (20)
        "app/cli/cmd_vendor.py",
        "app/cli/vendor_common.py",
        "app/cli/vendor_jobs.py",
        "app/services/vendor_cost.py",
        "app/services/vendor_dispatch.py",
        "app/services/vendor_dispatch_common.py",
        "app/services/vendor_dispatch_hub.py",
        "app/services/vendor_dispatch_hub2.py",
        "app/services/vendor_dispatch_part2.py",
        "app/services/vendor_quote.py",
        "app/services/vendor_return.py",
        "app/services/vendor_return_common.py",
        "app/services/vendor_return_hub.py",
        "app/services/vendor_return_part2.py",
        "app/services/vendor_rework.py",
        "app/services/vendor_spend.py",
        "app/services/vendor_studio_sso.py",
        "app/services/vendor_xliff.py",
        "app/services/vendors.py",
        "app/web/vendors.py",
        # workflow-engine (31)
        "app/cli/cmd_workflow.py",
        "app/cli/cmd_workflow_common.py",
        "app/cli/cmd_workflow_common_leaf.py",
        "app/cli/cmd_workflow_part2.py",
        "app/cli/cmd_workflow_part2_leaf.py",
        "app/cli/cmd_workflow_part3.py",
        "app/cli/cmd_workflow_part3_leaf.py",
        "app/cli/cmd_workflow_part4.py",
        "app/services/batch_runs.py",
        "app/services/batch_runs_common.py",
        "app/services/batch_runs_hub.py",
        "app/services/batch_runs_hub2.py",
        "app/services/package_view.py",
        "app/services/run_overlay.py",
        "app/services/workflow_rail.py",
        "app/services/workflow_shapes.py",
        "app/services/workflow_stats.py",
        "app/services/workflows.py",
        "app/services/workflows_common.py",
        "app/services/workflows_common_leaf.py",
        "app/services/workflows_part2.py",
        "app/services/workflows_part2_leaf.py",
        "app/services/workflows_part3.py",
        "app/services/workflows_part4.py",
        "app/services/workflows_part5.py",
        "app/services/workflows_part6.py",
        "app/web/workflows.py",
        "app/web/workflows_common.py",
        "app/web/workflows_part2.py",
        "app/web/workflows_part3.py",
        "app/web/workflows_part4.py",
    }
)


def _fail(msg):
    raise AssertionError(msg)


# --------------------------------------------------------------------------- #
# Enumerate the in-scope module universe (the same walk verify used to seed the allowlist).
# --------------------------------------------------------------------------- #
def _init_is_substantive(abs_path):
    """True if an __init__.py carries real content (AST body beyond the module docstring and
    __future__ imports) -- e.g. a finder, a registry, or router composition."""
    try:
        tree = ast.parse(_read(abs_path))
    except (SyntaxError, OSError):
        return True  # unparseable: keep it in scope rather than silently drop it
    for node in tree.body:
        if isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant):
            continue  # module docstring
        if isinstance(node, ast.ImportFrom) and node.module == "__future__":
            continue
        return True
    return False


def in_scope_modules():
    out = []
    for d in _L2_SCOPE:
        for root, _dirs, files in os.walk(os.path.join(_ROOT, d)):
            for f in files:
                if not f.endswith(".py"):
                    continue
                abs_path = os.path.join(root, f)
                if f == "__init__.py" and not _init_is_substantive(abs_path):
                    continue  # trivial package marker -- nothing to document
                rel = os.path.relpath(abs_path, _ROOT).replace(os.sep, "/")
                out.append(rel)
    return set(out)


# --------------------------------------------------------------------------- #
# Reference grammar. Three pin carriers, each enforced:
#   * inline-code spans `...`     -> REPO-ROOT-relative path / path::symbol / setting: / verify_
#   * markdown links [text](tgt)  -> target is DOC-DIR-relative (../../ from an L1 doc is the
#                                    only correct form); a backticked-identifier TEXT over a .py
#                                    target ALSO resolves that identifier as a symbol (AD-02).
#   * bare path tokens anywhere   -> app|tests|docs|... /...ext, fences included (AD-07).
# A span that carries `::` or ends in .py/.md is an INTENDED pin and FAILS when unresolvable
# (never fails open); backslashes are normalized and a leading ./ stripped (AD-06).
# --------------------------------------------------------------------------- #
_BACKTICK = re.compile(r"`([^`\n]+)`")
_MD_LINK_FULL = re.compile(r"\[([^\]]*)\]\(([^)]+)\)")
_SETTING = re.compile(r"^setting:([a-z][a-z0-9_]*)$")
_SUITE = re.compile(r"^verify_[a-z0-9_]+$")
_PATHISH = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_./-]*$")
_IDENT = re.compile(r"^[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)?$")  # foo or Class.method (link TEXT)
_BARE_PATH = re.compile(
    r"(?<![\w./-])(?:app|tests|docs|planning|integrations|alembic|scripts)/"
    r"[\w./-]*\.[A-Za-z][A-Za-z0-9]{0,6}"
)

# Line-number patterns forbidden in L0/L1 (design principle 2: stable anchors, never line
# numbers). URLs are stripped first (so http(s)://host:port#frag never false-positives). The
# file-anchored forms fire ONLY on a SOURCE/CONFIG extension (AD-102): that precisely excludes a
# host:port like "example.com:8443" (TLD "com" is not a source ext) and a ratio like "4.5:1"
# (ext "5"), while still catching audit.py:418 / x.html:120 / fly.toml:12. Three further AD-102
# carve-outs, each proven by a self-test: a numbered-heading anchor (`README.md#3-the-...`, a `#`
# followed by a slug) is allowed, only a bare `#<n>` line ref is banned; the L-form skips the
# L0-L3 LAYER terms ("ARCHITECTURE.md L0"); and a `@<n>x` retina marker ("logo.png @2x") is
# allowed, only a `@ <n>` line ref is banned.
_SRC_EXT = r"(?:py|md|html?|toml|ya?ml|jsx?|tsx?|css|cfg|ini|sh|ps1|sql|txt|jinja|j2|json)"
_LINENO_PATTERNS = (
    re.compile(rf"[\w-]+\.{_SRC_EXT}:\d+"),  # audit.py:418, x.html:120, fly.toml:12
    re.compile(rf"[\w-]+\.{_SRC_EXT}#\d+(?![-\w])"),  # audit.py#418 (NOT README.md#3-anchor)
    re.compile(rf"[\w-]+\.{_SRC_EXT}\s+L(?![0-3]\b)\d+\b"),  # audit.py L418 (NOT the L0 layer term)
    re.compile(rf"[\w-]+\.{_SRC_EXT}\s*@\s*\d+(?!x)"),  # audit.py @ 418 (NOT logo.png @2x)
    re.compile(r"#L\d+"),  # github line anchor
    re.compile(r":L\d+"),
    re.compile(r"\blines?\s+\d+\b", re.IGNORECASE),  # "line 42" / "lines 12-15"
)
_URL = re.compile(r"https?://\S+")


def _norm(span):
    """Normalize a candidate pin: backslashes -> /, strip a leading ./ and surrounding space."""
    span = span.strip().replace("\\", "/")
    while span.startswith("./"):
        span = span[2:]
    return span


def _module_symbols(abs_path):
    """Top-level def/class/assigned names in a .py file, via AST (no import). Also collects
    `Class.method` dotted names so a method pin can resolve."""
    with open(abs_path, encoding="utf-8") as fh:
        tree = ast.parse(fh.read(), filename=abs_path)
    names = set()
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.add(node.name)
        elif isinstance(node, ast.Assign):
            for t in node.targets:
                if isinstance(t, ast.Name):
                    names.add(t.id)
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            names.add(node.target.id)
        if isinstance(node, ast.ClassDef):
            for sub in node.body:
                if isinstance(sub, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    names.add(node.name + "." + sub.name)
    return names


def _looks_like_path(span):
    if "<" in span or ">" in span:
        return False  # a `docs/architecture/<subsystem>.md` placeholder is not a pin
    if not _PATHISH.match(span):
        return False
    if span in _ROOT_FILES:
        return True
    return "/" in span and any(span.startswith(r) for r in _PATH_ROOTS)


def _intended_pin(span):
    """A span nobody could mean as prose: it carries a symbol selector or a source extension."""
    return "::" in span or span.endswith((".py", ".md"))


def _symbol_problem(abs_path, symbol, raw, path):
    if not path.endswith(".py"):
        return f"symbol pin `{raw}`: {path!r} is not a .py file (no symbols to resolve)"
    try:
        syms = _module_symbols(abs_path)
    except (SyntaxError, OSError) as exc:
        return f"symbol pin `{raw}`: {path!r} did not parse ({exc})"
    if symbol not in syms:
        return f"symbol pin `{raw}`: {symbol!r} is not a top-level symbol of {path}"
    return None


def _resolve_root_path(path, symbol, raw):
    """Resolve a REPO-ROOT-relative path (+ optional symbol). Returns an error or None."""
    abs_path = os.path.join(_ROOT, path)
    if symbol is not None:
        if not os.path.isfile(abs_path):
            return f"symbol pin `{raw}`: path {path!r} is not a file"
        return _symbol_problem(abs_path, symbol, raw, path)
    if not os.path.exists(abs_path):
        return f"path pin `{raw}` resolves to no file or directory"
    return None


def _resolve_backtick(span, defaults):
    """Resolve one inline-code span (repo-root-relative). None if it resolves or is prose."""
    raw = span
    if any(c in span for c in "<>*"):
        return None  # a placeholder/glob span (`app/<name>.py`, `<path>::<symbol>`, `*.md`)
    span = _norm(span)
    m = _SETTING.match(span)
    if m:
        return (
            None
            if m.group(1) in defaults
            else (f"setting pin `setting:{m.group(1)}` names no key in settings.DEFAULTS")
        )
    if _SUITE.match(span):
        if not os.path.exists(os.path.join(_ROOT, "tests", span + ".py")):
            return f"suite pin `{raw}` names no tests/{span}.py"
        return None
    if "::" in span:
        path, _, symbol = span.partition("::")
        return _resolve_root_path(path.strip(), symbol.strip(), raw)
    if _looks_like_path(span) or _intended_pin(span):
        return _resolve_root_path(span, None, raw)
    return None


def _resolve_link(text, target, base_dir, doc_rel):
    """Resolve one markdown link. `target` is DOC-DIR-relative. Enforces target existence,
    repo-containment, and -- when TEXT is a bare identifier over a .py target -- the symbol."""
    tgt = target.split("#", 1)[0].strip()
    if not tgt or tgt.startswith(("http://", "https://", "mailto:")):
        return None
    tgt = _norm(tgt)
    path_part, _, symbol = tgt.partition("::")
    symbol = symbol.strip() or None
    abs_path = os.path.normpath(os.path.join(base_dir, path_part))
    root = os.path.normpath(_ROOT)
    try:
        inside = os.path.commonpath([abs_path, root]) == root
    except ValueError:  # different drive (Windows) -> definitely outside the repo
        inside = False
    if not inside:
        return f"{doc_rel}: link target `{target}` escapes the repo ({abs_path})"
    if not os.path.exists(abs_path):
        return f"{doc_rel}: link target `{target}` resolves to no file or directory"
    rel = os.path.relpath(abs_path, _ROOT).replace(os.sep, "/")
    if symbol is not None:
        prob = _symbol_problem(abs_path, symbol, target, rel)
        if prob:
            return f"{doc_rel}: {prob}"
    # AD-02: a `[SYMBOL](path.py)` citation -- backticked-identifier TEXT over a .py target --
    # must resolve SYMBOL against that file. But when the TEXT is just the file's own stem
    # (e.g. `[verify_audit](tests/verify_audit.py)`), it names the FILE, not a symbol -- skip.
    ident = text.strip().strip("`").strip()
    stem = os.path.splitext(os.path.basename(rel))[0]
    if rel.endswith(".py") and _IDENT.match(ident) and ident != stem:
        prob = _symbol_problem(abs_path, ident, f"[{ident}]({target})", rel)
        if prob:
            return f"{doc_rel}: link-text symbol {prob}"
    return None


def reference_problems(text, defaults, base_dir=_ROOT, doc_rel="<doc>"):
    """Every unresolved pin in `text`: markdown links (doc-dir-relative), then inline-code spans
    and bare path tokens (repo-root-relative). Links are resolved FIRST and then stripped, so a
    link's backticked TEXT and its target are not re-scanned as root-relative pins."""
    probs = []
    for link_text, target in _MD_LINK_FULL.findall(text):
        p = _resolve_link(link_text, target, base_dir, doc_rel)
        if p:
            probs.append(p)
    residual = _MD_LINK_FULL.sub(" ", text)  # drop [text](target) constructs
    for span in _BACKTICK.findall(residual):
        p = _resolve_backtick(span, defaults)
        if p:
            probs.append(f"{doc_rel}: {p}" if not p.startswith(doc_rel) else p)
    # AD-07: bare repo-path tokens anywhere (mermaid + fences included).
    for tok in _BARE_PATH.findall(residual):
        tok = _norm(tok)
        if not os.path.exists(os.path.join(_ROOT, tok)):
            probs.append(f"{doc_rel}: bare path token `{tok}` resolves to no file or directory")
    # AD-07: a hard-wrapped inline-code span (a pin split across a newline) shows up as an odd
    # backtick count on a non-fence line -- detect and fail rather than silently miss the pin.
    probs.extend(_unbalanced_backtick_problems(residual, doc_rel))
    return probs


def _unbalanced_backtick_problems(text, doc_rel):
    probs, in_fence = [], False
    for ln in text.splitlines():
        if ln.lstrip().startswith("```"):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        if ln.count("`") % 2 == 1:
            probs.append(
                f"{doc_rel}: line with an unbalanced backtick (a hard-wrapped inline-code pin?): "
                f"{ln.strip()[:80]!r}"
            )
    return probs


def lineno_problems(text):
    """Every forbidden line-number pattern occurrence in `text` (URLs stripped first so a
    https://host:port or a #frag in a link never false-positives)."""
    stripped = _URL.sub("", text)
    probs = []
    for pat in _LINENO_PATTERNS:
        for hit in pat.findall(stripped):
            probs.append(f"line-number pattern {hit!r} is forbidden in architecture docs")
    return probs


# --------------------------------------------------------------------------- #
# L1 parsing: headings, sections, and the modules a doc CLAIMS in "Where the work happens".
# --------------------------------------------------------------------------- #
def _normalize_heading(line):
    h = line.lstrip("#").strip()
    h = re.sub(r"^\d+[.)]\s*", "", h)  # strip "1. " / "1) " numbering
    return h.strip().lower()


def _headings(text):
    return [_normalize_heading(ln) for ln in text.splitlines() if ln.lstrip().startswith("#")]


def section_problems(text):
    """Required-section headings missing from an L1 doc."""
    have = set(_headings(text))
    return [f"missing required section '{s}'" for s in _REQUIRED_SECTIONS if s not in have]


def _section_body(text, normalized_title):
    """The text under the heading whose normalized title == normalized_title, up to the next
    heading of the same-or-shallower level (simplest: up to the next '#'-started line)."""
    lines = text.splitlines()
    out, capturing = [], False
    for ln in lines:
        if ln.lstrip().startswith("#"):
            if capturing:
                break
            capturing = _normalize_heading(ln) == normalized_title
            continue
        if capturing:
            out.append(ln)
    return "\n".join(out)


def _row_module_paths(cell, base_dir):
    """The app/*.py module paths pinned inside one table cell -- from backtick spans (root-
    relative) and markdown link targets (doc-dir-relative, normalized to root-relative)."""
    out = set()
    for span in _BACKTICK.findall(cell):
        path = _norm(span).partition("::")[0]
        if path.startswith("app/") and path.endswith(".py"):
            out.add(path)
    for _text, target in _MD_LINK_FULL.findall(cell):
        tgt = _norm(target.split("#", 1)[0]).partition("::")[0]
        if not tgt or tgt.startswith(("http://", "https://", "mailto:")):
            continue
        rel = os.path.relpath(os.path.normpath(os.path.join(base_dir, tgt)), _ROOT)
        rel = rel.replace(os.sep, "/")
        if rel.startswith("app/") and rel.endswith(".py"):
            out.add(rel)
    return out


def claimed_modules(text, base_dir=_L1_DIR):
    """The in-scope module paths a doc CLAIMS. AD-04: a claim counts only when its pin sits in a
    real markdown TABLE ROW (|-delimited) with at least two non-empty cells (the pin cell plus a
    description) -- a prose sentence that merely lists paths does NOT document them."""
    body = _section_body(text, "where the work happens")
    claimed = set()
    for ln in body.splitlines():
        s = ln.strip()
        if not s.startswith("|"):
            continue
        cells = [c.strip() for c in s.strip("|").split("|")]
        if all(set(c) <= set("-: ") for c in cells):  # header separator row
            continue
        nonempty = [c for c in cells if c]
        if len(nonempty) < 2:  # not a real described row
            continue
        for c in cells:
            claimed |= _row_module_paths(c, base_dir)
    return claimed


def l1_docs():
    """Absolute paths of the real L1 subsystem docs (docs/architecture/*.md, excluding
    README.md and _-prefixed scaffolding)."""
    if not os.path.isdir(_L1_DIR):
        return []
    out = []
    for f in sorted(os.listdir(_L1_DIR)):
        if not f.endswith(".md") or f == "README.md" or f.startswith("_"):
            continue
        out.append(os.path.join(_L1_DIR, f))
    return out


def _read(path):
    with open(path, encoding="utf-8") as fh:
        return fh.read()


# --------------------------------------------------------------------------- #
# check 1 -- reference resolution + line-number ban over the LIVE L0/L1 docs. THIS is the check
# that runs against the actual ARCHITECTURE.md / llms.txt / subsystem docs on every push.
# --------------------------------------------------------------------------- #
#: The authoring scaffolding under docs/architecture/ -- NOT subsystem docs, but AD-10 requires
#: they too pass the reference + line-number checks (their <placeholder> spans self-skip).
_SCAFFOLDING = ("README.md", "_TEMPLATE.md")


def _scaffolding_docs():
    return [
        os.path.join(_L1_DIR, f) for f in _SCAFFOLDING if os.path.isfile(os.path.join(_L1_DIR, f))
    ]


def check_live_references():
    defaults = settings.DEFAULTS
    # AD-10: no non-scaffolding doc may hide behind a leading underscore (which would exempt it
    # from the L1 checks). The only sanctioned _-prefixed file is the template.
    if os.path.isdir(_L1_DIR):
        for f in os.listdir(_L1_DIR):
            if f.endswith(".md") and f.startswith("_") and f not in _SCAFFOLDING:
                _fail(
                    f"docs/architecture/{f} is _-prefixed (guard-exempt) but not sanctioned "
                    "scaffolding -- rename it so it is enforced as an L1 subsystem doc"
                )
    scanned = 0
    for path in [_ARCH_MD, _LLMS_TXT, *l1_docs(), *_scaffolding_docs()]:
        assert os.path.isfile(path), f"required doc missing: {os.path.relpath(path, _ROOT)}"
        text = _read(path)
        rel = os.path.relpath(path, _ROOT).replace(os.sep, "/")
        base_dir = os.path.dirname(path)
        refp = reference_problems(text, defaults, base_dir=base_dir, doc_rel=rel)
        assert not refp, "unresolved reference(s):\n  " + "\n  ".join(refp)
        lnp = lineno_problems(text)
        assert not lnp, "{}: forbidden line number(s):\n  {}".format(rel, "\n  ".join(lnp))
        scanned += 1
    print(
        "  [1] live references: %d doc(s) scanned (L0 + L1 + scaffolding); every backtick/link/"  # noqa: UP031
        "bare-token path, symbol, setting, and suite pin resolves; no line numbers OK" % scanned
    )


# --------------------------------------------------------------------------- #
# check 1b -- reference-grammar self-tests (non-vacuity: the resolver really catches a broken
# pin, and the line-number ban really fires).
# --------------------------------------------------------------------------- #
def check_reference_selftest():
    d = settings.DEFAULTS
    # A REAL pin of each kind resolves (no problem).
    for good in (
        "`app/services/audit.py::verify_chain`",
        "`app/services/settings.py::DEFAULTS`",
        "`app/ports/`",
        "`app/modules.py`",
        "`setting:multi_org_enabled`",
        "`verify_modules`",
    ):
        probs = reference_problems(good, d)
        assert not probs, f"a REAL pin was wrongly flagged: {good} -> {probs}"
    # A FABRICATED pin of each kind is caught.
    cases = {
        "`app/services/audit.py::__no_such_symbol__`": "symbol",
        "`app/services/__nope__.py`": "no file or directory",
        "`app/services/audit.py::__x__`": "symbol",
        "`setting:no_such_setting_xyz`": "settings.DEFAULTS",
        "`verify___no_such_suite__`": "tests/",
        "`app/services/settings.py::verify_chain`": "top-level symbol",
    }
    for bad, needle in cases.items():
        probs = reference_problems(bad, d)
        assert probs, f"a FABRICATED pin was NOT caught: {bad}"
        assert any(needle in p for p in probs), (
            f"pin {bad} produced the wrong diagnostic {probs!r} (expected {needle!r})"
        )
    # Prose in backticks that is not pin-shaped must be left alone. (A bare `verify_x` is NOT
    # here: by convention it names the suite tests/verify_x.py, so a function must be pinned as
    # `path::symbol` -- writing a function bare is deliberately flagged as a missing suite.)
    for prose in ("`MockProvider`", "`/api/projects`", "`app.services.audit`", "`Session`"):
        assert not reference_problems(prose, d), f"prose {prose} was wrongly treated as a pin"

    # AD-01: markdown-link targets, resolved RELATIVE TO THE DOC'S DIRECTORY. From an L1 doc the
    # only correct form is ../../ ; a broken one and a repo-escape are both caught, a real one
    # passes. base_dir = docs/architecture (an L1 doc's home).
    assert not reference_problems(
        "[the chain](../../app/services/audit.py)", d, base_dir=_L1_DIR
    ), "AD-01: a valid ../../ link from an L1 doc was wrongly flagged"
    assert any(
        "resolves to no file" in p
        for p in reference_problems("[x](../../app/services/__NO__.py)", d, base_dir=_L1_DIR)
    ), "AD-01: a broken ../../ link target was NOT caught"
    assert any(
        "escapes the repo" in p
        for p in reference_problems("[x](../../../../../etc/passwd)", d, base_dir=_L1_DIR)
    ), "AD-01: a repo-escaping link target was NOT caught"

    # AD-02: the `[SYMBOL](path.py)` citation style resolves the SYMBOL, not just the file.
    assert not reference_problems("[`MODULES`](app/modules.py)", d), (
        "AD-02: a real [SYMBOL](path.py) citation was wrongly flagged"
    )
    assert any(
        "link-text symbol" in p and "MODULEZ_FAKE" in p
        for p in reference_problems("[MODULEZ_FAKE](app/modules.py)", d)
    ), "AD-02: a fabricated [SYMBOL](path.py) citation was NOT caught"

    # AD-06: malformed pins fail CLOSED (they used to fail open / pass silently), while a pin that
    # is merely non-canonical (backslashes, leading ./) resolves after normalization.
    assert any("is not a file" in p for p in reference_problems("`app.services.audit::x`", d)), (
        "AD-06: a dotted-module symbol pin did NOT fail closed"
    )
    assert any("no file or directory" in p for p in reference_problems("`models.py`", d)), (
        "AD-06: a bare `.py` intended pin that does not exist at root did NOT fail closed"
    )
    assert not reference_problems("`app\\services\\audit.py`", d), (
        "AD-06: a backslash path did not normalize to a resolving pin"
    )
    assert not reference_problems("`./app/modules.py`", d), (
        "AD-06: a ./-prefixed path did not normalize to a resolving pin"
    )

    # AD-07: bare repo-path tokens OUTSIDE backticks/links (mermaid + prose) are resolved.
    assert any(
        "bare path token" in p
        for p in reference_problems("see app/services/__nope__.py in the tree", d)
    ), "AD-07: a bare undocumented path token was NOT caught"
    assert not reference_problems("the map lives in app/services/audit.py yes", d), (
        "AD-07: a valid bare path token was wrongly flagged"
    )
    # AD-07: a hard-wrapped inline-code pin (odd backticks on a non-fence line) is caught.
    assert any(
        "unbalanced backtick" in p
        for p in reference_problems("see `app/services/audit.py::verify_\nchain` here", d)
    ), "AD-07: a hard-wrapped (line-split) inline-code span was NOT caught"

    print(
        "  [1b] reference self-test: real path/symbol/setting/suite/link pins pass; fabricated "
        "path/symbol/link/[SYMBOL](file)/bare-token pins, repo-escapes, malformed fail-open, and "
        "hard-wraps are each caught; non-pin prose is left alone OK"
    )


def check_lineno_selftest():
    # AD-05: every forbidden form is caught -- not just .py:N but any file.ext:N, the # / @ / L
    # variants, and the word forms.
    for bad in (
        "see app/services/audit.py:418 for the chain",
        "the anchor (see line 42) is MAC'd",
        "spanning lines 12-15 of the file",
        "github permalink #L100",
        "models.py:88-90 carries it",
        "foo.py:L12",
        "the template.html:120 row",
        "fly.toml:12 sets it",
        "audit.py#418 in the blame",
        "audit.py @ 418 roughly",
        "audit.py L418 for detail",
    ):
        assert lineno_problems(bad), f"a line number was NOT caught: {bad!r}"
    # Clean prose does NOT trip the ban. AD-102: five false-positive classes must all PASS --
    # a numbered-HEADING anchor (this repo numbers its headings and the contract tells authors to
    # cross-link), the corpus's own L0-L3 LAYER vocabulary, a bare host:port (deployment doc), a
    # retina @<n>x marker, and a contrast ratio -- alongside the earlier real-pin / URL cases.
    for good in (
        "`app/services/audit.py::verify_chain` forms the keyed hash chain",
        "the request pipeline runs through app/kernel",
        "the guidelines here are 3 stable anchors",
        "descend L0 -> L1 -> L2 -> L3 through the layers",
        "the ratio is 4.5:1 in both themes",
        "see https://example.com:8080/blob/main/x.py#L10 elsewhere",
        "cross-link to README.md#3-the-pin-grammar for the rules",  # numbered-heading anchor
        "the map lives at ARCHITECTURE.md L0 and descends",  # L0-L3 layer vocabulary
        "the proxy listens on example.com:8443 in production",  # bare host:port
        "the diagram renders logo.png @2x on retina displays",  # retina marker
    ):
        assert not lineno_problems(good), f"clean prose was wrongly flagged: {good!r}"
    print(
        "  [1c] line-number self-test: file.ext:N / #N / @N / 'L N' / 'line N' / 'rows N-M' / "
        "#Lnn / :Lnn each caught; heading-anchors, L0-L3 vocab, host:port, @Nx, ratios, URLs not OK"
    )


# --------------------------------------------------------------------------- #
# check 2 -- the coverage ratchet (grow-only), over the REAL tree, plus sabotage self-tests.
# --------------------------------------------------------------------------- #
def _coverage_problems(
    universe, allowlist, claimed_by, floor=_DOCUMENTED_FLOOR, ceiling=_ALLOWLIST_CEILING
):
    """Return (problems, claimed_set). claimed_by: {module_path: [doc_rel, ...]}. `floor` is the
    grow-only documented-module floor and `ceiling` the shrink-only allowlist ceiling (both
    parameterized so the ratchet laws can be sabotage-tested)."""
    probs = []
    claimed = set(claimed_by)
    allow = set(allowlist)

    # (a) allowlist hygiene: reasons non-empty AND -- for a module added AFTER the Phase-0 seed --
    #     AUTHORED (not the auto-filled seed reason, AD-09); no stale entries for modules that no
    #     longer exist; no module BOTH allowlisted and claimed (the REMOVED_OPTIONAL staleness law);
    #     and the allowlist may only SHRINK past its pinned ceiling (AD-09).
    for mod, reason in allowlist.items():
        if not (reason and reason.strip()):
            probs.append(f"DOC_ALLOWLIST[{mod!r}] has an empty reason")
        elif mod not in _PHASE0_SEED_MODULES and reason == _PHASE0_SEED:
            probs.append(
                f"DOC_ALLOWLIST[{mod!r}] was added after the Phase-0 seed but carries the "
                "auto-filled seed reason -- give it an AUTHORED reason (why it has no doc yet)"
            )
        if mod not in universe:
            probs.append(f"DOC_ALLOWLIST names {mod!r}, which is not an in-scope module (stale)")
    if len(allowlist) > ceiling:
        probs.append(
            f"DOC_ALLOWLIST grew past its ceiling ({len(allowlist)} > {ceiling}) -- the allowlist "
            "may only shrink; document the new module(s) or consciously raise _ALLOWLIST_CEILING"
        )
    for mod in allow & claimed:
        probs.append(
            f"{mod!r} is BOTH in DOC_ALLOWLIST and claimed by an L1 doc "
            "(remove it from the allowlist once it has a documentation home)"
        )
    # (b) coverage: every in-scope module is claimed OR allowlisted. A new module with no home
    #     lands here -- the ratchet's whole point.
    uncovered = universe - claimed - allow
    if uncovered:
        probs.append(
            "in-scope module(s) with NO documentation home (not claimed by an L1 doc and not in "
            "DOC_ALLOWLIST): " + ", ".join(sorted(uncovered))
        )
    # (c) grow-only floor: documented modules may only grow.
    regressed = floor - claimed
    if regressed:
        probs.append(
            "module(s) in _DOCUMENTED_FLOOR no longer claimed by any L1 doc (a doc was silently "
            "un-written): " + ", ".join(sorted(regressed))
        )
    # (d) single-claim: no module claimed by two docs.
    for mod, docs in claimed_by.items():
        if len(docs) > 1:
            probs.append(f"{mod!r} is claimed by more than one L1 doc: {', '.join(sorted(docs))}")
    return probs, claimed


def _live_claimed_by():
    """{module_path: [doc_rel,...]} built from every real L1 doc's 'Where the work happens'."""
    out = {}
    for doc in l1_docs():
        rel = os.path.relpath(doc, _ROOT).replace(os.sep, "/")
        for mod in claimed_modules(_read(doc), base_dir=os.path.dirname(doc)):
            out.setdefault(mod, []).append(rel)
    return out


def check_coverage_ratchet():
    universe = in_scope_modules()
    # The static allowlist must exactly enumerate the un-documented universe today (Phase 0),
    # so a NEW file added to the tree is genuinely un-covered and fails -- proving the list is
    # not vacuously computed. (Allowlist may be a strict subset once docs exist.)
    claimed_by = _live_claimed_by()
    probs, claimed = _coverage_problems(universe, DOC_ALLOWLIST, claimed_by)
    assert not probs, "coverage ratchet failed:\n  " + "\n  ".join(probs)

    # SABOTAGE 1 (non-vacuity): a planted NEW module absent from allowlist and claims is caught.
    planted = "app/services/__planted_undocumented__.py"
    sab_universe = universe | {planted}
    sp, _ = _coverage_problems(sab_universe, DOC_ALLOWLIST, claimed_by)
    assert any("NO documentation home" in p and planted in p for p in sp), (
        "sabotage: a new undocumented module did NOT redden the coverage ratchet"
    )
    # SABOTAGE 2: a module BOTH allowlisted and claimed is caught (staleness law).
    victim = next(iter(DOC_ALLOWLIST))
    sp2, _ = _coverage_problems(universe, DOC_ALLOWLIST, {victim: ["docs/architecture/x.md"]})
    assert any("BOTH in DOC_ALLOWLIST and claimed" in p for p in sp2), (
        "sabotage: a doubly-covered module did NOT redden the ratchet"
    )
    # SABOTAGE 3: a floor module no longer claimed by any doc reddens the grow-only law. Pass a
    # sabotaged floor naming a SYNTHETIC never-existing module (the __planted_undocumented__
    # convention of SABOTAGE 1/4): `regressed = floor - claimed` is a pure set difference that
    # never resolves paths against disk, so a name nothing claims always fires the law -- and no
    # later phase documenting a real module can ever break this fixture again.
    sp3, _ = _coverage_problems(
        universe, DOC_ALLOWLIST, claimed_by, floor=frozenset({"app/services/__never_claimed__.py"})
    )
    assert any("no longer claimed by any L1 doc" in p for p in sp3), (
        "sabotage: an un-claimed floor module did NOT redden the grow-only floor law"
    )
    # SABOTAGE 4 (AD-09/AD-100): exercise the REAL wiring. Extend the ACTUAL DOC_ALLOWLIST with a
    # poisoned authored entry for a module OUTSIDE the seed set (built off DOC_ALLOWLIST so the
    # probe tracks the live allowlist size, not a hard-coded seed count). A seed-reason authored
    # entry AND an empty-reason one are each caught (the authored-reason law can now actually fire,
    # which the aliased snapshot broke in round 2).
    newmod = "app/services/__new_undoc__.py"
    sab_univ4 = universe | {newmod}
    for bad_reason, needle in ((_PHASE0_SEED, "auto-filled seed reason"), ("", "empty reason")):
        poisoned = {**DOC_ALLOWLIST, newmod: bad_reason}  # newmod is NOT in _PHASE0_SEED_MODULES
        spx, _ = _coverage_problems(sab_univ4, poisoned, claimed_by, ceiling=len(poisoned))
        assert any(needle in p for p in spx), (
            f"sabotage: a post-seed authored entry with a {needle!r} was NOT caught (AD-100)"
        )
    # SABOTAGE 5 (AD-09): the allowlist growing ONE past its ceiling is caught.
    over = {**DOC_ALLOWLIST, newmod: "an authored reason"}
    sp5, _ = _coverage_problems(sab_univ4, over, claimed_by, ceiling=_ALLOWLIST_CEILING)
    assert any("grew past its ceiling" in p for p in sp5), (
        "sabotage: an allowlist over its ceiling did NOT redden AD-09"
    )
    # AD-04: a "claim" must sit in a real TABLE ROW; a prose sentence listing paths does NOT count.
    prose_doc = (
        "## Where the work happens\n\nSee `app/services/audit.py` and `app/services/access.py` "
        "throughout the tree.\n\n## Next\n"
    )
    assert claimed_modules(prose_doc, base_dir=_L1_DIR) == set(), (
        "AD-04: prose-listed modules were wrongly counted as documented claims"
    )
    table_doc = (
        "## Where the work happens\n\n| Concern | Pin | One line |\n|---|---|---|\n"
        "| chain | `app/services/audit.py::verify_chain` | the HMAC chain |\n\n## Next\n"
    )
    assert claimed_modules(table_doc, base_dir=_L1_DIR) == {"app/services/audit.py"}, (
        "AD-04: a real table-row claim was NOT counted"
    )

    assert claimed >= _DOCUMENTED_FLOOR, "floor is not a subset of claimed (invariant broken)"
    assert len(DOC_ALLOWLIST) <= _ALLOWLIST_CEILING, "allowlist exceeds its own ceiling"
    # PHASE 4: the anonymous Phase-0 seed is CLOSED. The seed tuple and its snapshot are empty, so
    # every DOC_ALLOWLIST entry is an AUTHORED entry (the authored-reason law above then requires a
    # non-seed, non-empty reason on each). This is the durable statement that the ratchet is closed.
    anon = [m for m in DOC_ALLOWLIST if m in _PHASE0_SEED_MODULES]
    assert not _ALLOWLISTED_MODULES and not _PHASE0_SEED_MODULES and not anon, (
        "the anonymous Phase-0 seed must be empty (Phase 4 closed the ratchet): "
        + ", ".join(sorted(anon))
    )
    print(
        "  [2] coverage ratchet: %d in-scope modules (substantive __init__ included), %d "  # noqa: UP031
        "allowlisted (0 anonymous seed + %d authored, ceiling %d), %d claimed; floor grow-only + "
        "ceiling shrink-only; undoc-module + double-cover + floor-regress + seed-reason + ceiling "
        "sabotage each caught; anonymous seed proven zero OK"
        % (
            len(universe),
            len(DOC_ALLOWLIST),
            len(_AUTHORED_ALLOWLIST),
            _ALLOWLIST_CEILING,
            len(claimed),
        )
    )


# --------------------------------------------------------------------------- #
# check 3 -- section contract over real L1 docs + self-test.
# --------------------------------------------------------------------------- #
_SYNTH_L1 = "\n".join(
    f"## {i + 1}. {t}"
    for i, t in enumerate(
        (
            "Role",
            "Invariants",
            "Where the work happens",
            "Data & settings",
            "Enforced by",
            "Battle scars",
            "Limits & accepted risks",
        )
    )
)


def check_section_contract():
    for doc in l1_docs():
        rel = os.path.relpath(doc, _ROOT).replace(os.sep, "/")
        probs = section_problems(_read(doc))
        assert not probs, "{}: {}".format(rel, "; ".join(probs))
    # Self-test: a complete synthetic doc passes; dropping any one section fails naming it.
    assert not section_problems(_SYNTH_L1), "a complete synthetic L1 doc was wrongly rejected"
    broken = _SYNTH_L1.replace("## 6. Battle scars", "## 6. War stories")
    probs = section_problems(broken)
    assert any("battle scars" in p for p in probs), (
        "section-contract self-test: a doc missing 'Battle scars' was NOT flagged"
    )
    print(
        "  [3] section contract: %d L1 doc(s) carry all 7 required sections; a doc missing one "  # noqa: UP031
        "is proven to fail OK" % len(l1_docs())
    )


# --------------------------------------------------------------------------- #
# check 4 -- L2 back-pointer over real claims + self-test.
# --------------------------------------------------------------------------- #
def _l2_marker_missing(src, module_rel, doc_rel):
    """Pure predicate: None if the module's DOCSTRING carries `Documented in: <doc_rel>`, else a
    problem string. AD-04: anchored to the AST module docstring, not a whole-source substring, so
    the marker cannot be satisfied by an unrelated comment or string literal elsewhere."""
    marker = "Documented in: " + doc_rel
    try:
        doc = ast.get_docstring(ast.parse(src)) or ""
    except SyntaxError as exc:
        return f"{module_rel}: did not parse ({exc})"
    if marker not in doc:
        return f"{module_rel}: module docstring missing back-pointer '{marker}'"
    return None


def _l2_pointer_problem(module_rel, doc_rel):
    """Read module_rel's source and apply _l2_marker_missing."""
    try:
        src = _read(os.path.join(_ROOT, module_rel))
    except OSError as exc:
        return f"{module_rel}: unreadable ({exc})"
    return _l2_marker_missing(src, module_rel, doc_rel)


def check_l2_backpointer():
    claimed_by = _live_claimed_by()
    probs = []
    for mod, docs in claimed_by.items():
        for doc_rel in docs:
            p = _l2_pointer_problem(mod, doc_rel)
            if p:
                probs.append(p)
    assert not probs, "L2 back-pointer failures:\n  " + "\n  ".join(probs)
    # Self-test (on the pure predicate, DOCSTRING-anchored -- AD-04): the marker in the docstring
    # passes; its absence is flagged; and -- the point of the fix -- the marker present ONLY in a
    # comment or a non-docstring string literal does NOT satisfy it.
    doc_rel = "docs/architecture/example.md"
    without = '"""A module.\n\nMain entry points ...\n"""\n'
    with_ptr = f'"""A module.\n\nDocumented in: {doc_rel}\n"""\n'
    in_comment = f'"""A module."""\n# Documented in: {doc_rel}\nX = 1\n'
    in_literal = f'"""A module."""\nNOTE = "Documented in: {doc_rel}"\n'
    assert _l2_marker_missing(without, "app/x.py", doc_rel), (
        "L2 self-test: a source lacking the back-pointer was NOT flagged"
    )
    assert _l2_marker_missing(with_ptr, "app/x.py", doc_rel) is None, (
        "L2 self-test: a docstring back-pointer was wrongly flagged"
    )
    assert _l2_marker_missing(in_comment, "app/x.py", doc_rel), (
        "AD-04: a back-pointer in a COMMENT wrongly satisfied the docstring-anchored check"
    )
    assert _l2_marker_missing(in_literal, "app/x.py", doc_rel), (
        "AD-04: a back-pointer in a non-docstring literal wrongly satisfied the check"
    )
    n_claims = sum(len(v) for v in claimed_by.values())
    print(
        "  [4] L2 back-pointer: every claimed module names its doc via a DOCSTRING 'Documented "
        f"in: ...' line ({n_claims} claim(s) today); comment/literal placement rejected OK"
    )


# --------------------------------------------------------------------------- #
# check 5 -- index integrity + self-test.
# --------------------------------------------------------------------------- #
def _index_problems(arch_text, llms_text, doc_rels):
    probs = []
    for rel in doc_rels:
        if rel not in arch_text:
            probs.append(f"L1 doc {rel} is not listed in ARCHITECTURE.md")
        if rel not in llms_text:
            probs.append(f"L1 doc {rel} is not listed in llms.txt")
    return probs


def check_index_integrity():
    arch_text, llms_text = _read(_ARCH_MD), _read(_LLMS_TXT)
    doc_rels = [os.path.relpath(d, _ROOT).replace(os.sep, "/") for d in l1_docs()]
    probs = _index_problems(arch_text, llms_text, doc_rels)
    assert not probs, "index integrity failed:\n  " + "\n  ".join(probs)
    # Self-test: an unlisted synthetic L1 doc is caught in both indexes.
    sp = _index_problems("(no docs listed)", "(no docs listed)", ["docs/architecture/ghost.md"])
    assert any("ARCHITECTURE.md" in p for p in sp) and any("llms.txt" in p for p in sp), (
        "index self-test: an unlisted L1 doc was NOT caught in both indexes"
    )
    print(
        "  [5] index integrity: every existing L1 doc (%d) is listed in ARCHITECTURE.md AND "  # noqa: UP031
        "llms.txt; an unlisted doc is proven to fail OK" % len(doc_rels)
    )


def check_assistant_claims():
    paths = (
        "docs/architecture/assistant.md",
        "app/services/assist.py",
    )
    phrases = ("structurally impossible", "cannot invent a feature", "can never disagree")
    for path in paths:
        content = " ".join(_read(path).lower().split())
        for phrase in phrases:
            assert phrase not in content, f"{path} still claims {phrase}"
    print("  [6] assistant claims: three over-claims absent across both surfaces OK")


def main():
    from tests._deploy_doc_truth import check_deploy_commands

    check_assistant_claims()
    check_deploy_commands()
    check_live_references()
    check_reference_selftest()
    check_lineno_selftest()
    check_coverage_ratchet()
    check_section_contract()
    check_l2_backpointer()
    check_index_integrity()
    print("DOCS ARCHITECTURE OK")


if __name__ == "__main__":
    main()
