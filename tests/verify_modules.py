"""The MODULE-REGISTRY guard -- proves app/modules.py's manifest is complete, faithful,
and byte-identical to the tables it consolidates (composable-core phase 5).

WHAT THIS GUARDS (SPEC-module-registry S6). app/modules.py relocates the toggleable-module
declaration into ONE manifest and DERIVES the CLI + MCP mirror tables from it. This suite is
the primary guard that the consolidation is BEHAVIOUR-NEUTRAL -- the manifest reproduces the
exact table each incumbent guard holds, changing a table's SOURCE, never its CONTENTS.

THE CHECKS
  1  well-formedness: every STRUCTURAL manifest field resolves to a real repo object
     (web/api/hooks -> a real route, cli_groups -> a real argparse group, mcp_gate ==
     setting_key in settings.DEFAULTS, cards -> a real capability file, ROUTER_GROUPS attrs
     -> real APIRouter objects); prose fields (description/seams/cross_reach_allow reasons)
     are carved out (S3.2); slugs + setting_keys unique.
  2  snapshot equivalence (anti-drift core): features_view() reproduces the pre-migration
     FEATURES tuple byte-identically (a pinned astuple digest), and router_groups() composes
     the three groups from the exact pre-migration router attrs, in order.
  3  FEATURES identity (M9): app.features.FEATURES is a tuple and every lookup dict shares
     that one tuple's elements -- one identity of "the features" behind every helper.
  4  the ratcheted optional-slug allowlist (S3.5): {MODULES} U {REMOVED_OPTIONAL} superset
     OPTIONAL_SLUGS, OPTIONAL_SLUGS cannot shrink below the shipped floor, every
     REMOVED_OPTIONAL reason non-empty, and NO REMOVED_OPTIONAL slug still in MODULES
     (staleness both directions) -- closes the silent de-flagging hole.
  5  non-vacuity / sabotage: dropping a real surface from a manifest row breaks the
     DOWNSTREAM /api owner accounting (a real route unclaims) -- the derivation is load-bearing.
  6  anti-framework bounding: a planted row with a structural field that has no real referent
     fails well-formedness; a prose-only field with free text does not (the carve-out holds).
  7  standalone-import (S3.6): `import app.modules` succeeds in a fresh interpreter with
     app.web NOT in sys.modules -- the manifest is readable with no router tree / DB engine.
  8  CLI mirror derivation (M3): cli_expected() reproduces the pre-relocation module-owned
     EXPECTED_COMMANDS rows byte-for-byte (a pinned digest), and its key set IS exactly the
     real parser's module-group leaves; sabotage-proven.
  9  MCP mirror derivation (M3): mcp_gate_for(tool) reproduces every module-owned Tool.feature_gate
     literal (and declines core/perimeter tools), byte-identical to the pre-M3 registry;
     sabotage-proven. The machinery FREEZES with M3 (S3 item 1) -- this is the phase's last train.

Hermetic + offline (throwaway LOC_TMS_DATA_DIR, MockProvider). ASCII-only prints. Final line
"MODULES OK". Run through tests/test_verify_suites.py with the explicit suite-name chunk.
"""

import dataclasses
import hashlib
import os
import subprocess
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(_ROOT)
sys.path.insert(0, _ROOT)
from tests._bootstrap import init_env

init_env("loctms_modules_")

from app import editions
from app import features as F
from app import modules as M
from app.cli.__main__ import build_parser
from app.db import init_db
from app.main import app
from app.services import settings
from tests import _routes

# --------------------------------------------------------------------------- #
# Pinned snapshots (captured from the PRE-migration source at main e4f1ce04). Each is a
# deliberately-ratcheted pin in the MIN_FEATURES tradition: it changes only in the diff that
# legitimately changes a record's content, and a reviewer reads that change. Between such
# diffs it is a byte-identity proof that the manifest reproduces the incumbent table exactly.
# --------------------------------------------------------------------------- #
#: sha256 of repr(tuple(dataclasses.astuple(f) for f in FEATURES)) -- every field of all 11.
#: Bumped 2026-08-19 (CV-2 project budgets): the `vendors` module `cross_reach_allow` gained
#: ONE entry for app/services/project_budgets.py -- the per-project money budget READS
#: vendor_spend.leg_human_cost to count settled vendor human spend against a project's cap
#: (read-only aggregation, no egress, works with vendors off, gate-verified). No other
#: manifest field changed; this is the only modules.py drift in the train.
#: Prior bump 2026-08-19 (Wave 2 reports Train B): the `reports` module description now lists the
#: qa/postedit kinds and the widened axis set, the `report run` CLI-catalog one-liner names
#: the five kinds, and the databricks `setup` one-liner says "six stream tables" (Train C's
#: sixth sparse stream) -- all mandated text-truth updates (SPEC-wave2 §8).
#: Prior bump 2026-08-19 (quick-win wave 1 merge): the vendors `cross_reach_allow` gained three
#: entries for the CV-1 7-band leverage seam (queries.py + cmd_project.py new, api_cli.py
#: extended -- pure classification, works with vendors off, gate-verified), and the
#: key_projects CLI row for `keys changes` now describes both --since and --modified-after
#: modes (AP-1 delta-API rework).
#: Prior bump 2026-08-18 (api-correctness train): vendors `web` link/unlink patterns
#: (F-MED-5) + llmemory `seams` invalidate/-since gating prose (F-HIGH-2).
#: Bumped 2026-08-19 (Wave 3 Train B): a NEW `queries` module row (the queries_enabled kill
#: switch — threaded queries/comments, default ON) joins MODULES, so features_view() now
#: reproduces 12 features, not 11. Its cross_reach_allow carries TWO classified callers
#: (app/web/api_cli.py — the shared /api surface, and app/services/notifications.py — the §4.6
#: mails). No incumbent record changed; the drift is the one new row.
#: Re-bumped 2026-08-19 (Wave 3 B/E merge reconciliation): the `queries` row's
#: cross_reach_allow gained a THIRD classified caller — app/services/vendor_return.py (the
#: Train E vendor-comment round-trip's record_vendor_post import). Per-train rule (§5.1): the
#: row lands when E's import merges. No incumbent record changed.
#: Bumped 2026-08-19 (Wave 3 Train D): a NEW `worklist` module row (the worklist_enabled kill
#: switch — the reviewer "My work" page, default ON) joins MODULES, so features_view() now
#: reproduces 13 features, not 12. Its cross_reach_allow carries TWO classified callers
#: (app/web/api_cli.py — GET /api/worklist, and app/web/queries.py — the dashboard's
#: open-query count). No incumbent record changed; the drift is the one new row.
#: Bumped 2026-08-24 (Sprint Train I): a NEW `scim` module row (the scim_enabled opt_in SCIM
#: 2.0 provisioning surface, default OFF — the databricks opt_in shape) joins MODULES, so
#: features_view() now reproduces 14 features, not 13. It declares no service_modules /
#: cross_reach_allow (reached only through its own gated /scim router + `scim` CLI + a pure
#: read on the Users page — the databricks precedent). No incumbent record changed; the
#: drift is the one new row. (Train I's SAML overflow adds a 15th row in its own commit.)
#: Bumped 2026-08-24 (Sprint Train I SAML): a NEW `saml` module row (the saml_enabled opt_in
#: SAML 2.0 SP, default OFF, BETA) joins MODULES, so features_view() now reproduces 15
#: features, not 14. It declares no cli_groups (a browser SSO flow, the OIDC precedent —
#: cli_hole_reason set) and no service_modules; its web pattern is /auth/saml (routes live in
#: the existing auth.router, so no ROUTER_GROUPS change). No incumbent record changed; the
#: drift is the one new row. cli_expected() is unchanged (saml adds no MODULE_CLI rows).
#: Bumped 2026-08-25 (Phase-3 Train A): a NEW `llmemory_suggestions` module row (the opt_in
#: workflow-suggestion queue, default OFF — the prompt_tap/scim opt_in shape) joins MODULES, so
#: features_view() now reproduces 16 features, not 15. It declares no service_modules /
#: cross_reach_allow (reached only through its own gated /memory-suggestions router +
#: /api/memory-suggestions mirror + the `memory suggestions` CLI subgroup + the GDPR
#: purge_suggestions_for late-import hook). No incumbent record changed; the drift is the one row.
#: Bumped 2026-08-27 (Batch 13 data-export Train B): a NEW `data_export` module row (the
#: writes_off full-object-graph export, default ON) joins MODULES, so features_view() now
#: reproduces 17 features, not 16. Its cross_reach_allow carries TWO classified callers
#: (app/services/jobs.py -- Train A's HANDLERS registration, and app/web/api_cli.py -- the
#: /api/exports routes); the Train-C scheduler/gdpr importers are declared with THEIR trains
#: (the `queries` NEW-1 per-train precedent -- a pre-declared stale row would red check 11
#: now). No incumbent record changed; the drift is the one new row.
#: Bumped 2026-08-27 (Batch 14 T1 per-string image context): a NEW `key_images` module row
#: (the key_image_context_enabled removed kill switch, default ON, BETA) joins MODULES, so
#: features_view() now reproduces 18 features, not 17. It declares no service_modules /
#: cross_reach_allow (reached only through its own gated */images web+/api routes + the
#: `keys image` CLI subgroup + the two MCP tools -- the llmemory_suggestions precedent). No
#: incumbent record changed; the drift is the one new row.
#: Bumped 2026-08-30 (brand-as-config, D8/D9): TWO display-text edits, no structural/flag/
#: route/tool drift (the D9 contract snapshot proves the wire is byte-stable). (1) the `scim`
#: module DESCRIPTION dropped the legacy product name for brand-neutral wording ("groups
#: <product> accounts" became "groups user accounts"); (2) the `prompt_tap` module TITLE
#: took the D8 feature rename ("Prompt Tap" became its new default display name "LLM Ledger").
#: Both are pure presentation edits; slug/setting_key/routes/tool gates are unchanged. Still
#: 18 features, no row added or removed.
#: Bumped 2026-09-01 (SPEC-advanced-workflows P0): a NEW `advanced_workflows` module row (the
#: opt_in LLM-localization-harness tier, default OFF, BETA -- the databricks/scim opt_in shape,
#: but WITH service_modules + own_files declared, the queries/data_export STRUCTURAL shape) joins
#: MODULES, so features_view() now reproduces 19 features, not 18. Its cross_reach_allow is empty
#: (nothing outside own_files imports the harness guts -- the surfaces reach the package only
#: through its __init__ public API via the kernel facade). No incumbent record changed; the drift
#: is the one new row.
#: Bumped 2026-09-02 (SPEC-terminology-backend T1-lite): a NEW `terminology_concept_editor`
#: module row (the opt_in DEFAULT-OFF concept-editor WRITE-seam gate -- the databricks/scim
#: opt_in shape, no web/api/cli/mcp surfaces of its own; the kill switch is the
#: glossary_concepts service seam) joins MODULES, so features_view() now reproduces 20 features,
#: not 19. It declares no service_modules / cross_reach_allow (the write seams self-gate; no
#: HIGH-1 import-mutation shape). No incumbent record changed; the drift is the one new row.
#: Re-bumped 2026-09-02 (T1-lite gate round-1 LT-3): the same module row's DESCRIPTION now states
#: the `.glossary.json` import is allowed only into an EMPTY glossary with a language pair (the
#: operator-facing Settings/GET-features string; a pure text edit, no structural/flag drift).
#: Re-bumped 2026-09-05 (T1b A.7 / M-4): the module row's `seams=` prose now states the truth after
#: the authority inversion -- the `.glossary.json` / concept import is UNGATED on every install (a
#: populated import is allowed regardless of the flag), and the flag gates only the concept EDITOR
#: write seams. A pure text edit of the seams description; no structural/flag drift.
#: Re-bumped 2026-09-07 (Prompt Tap Wave C, gate round-1 HIGH-1): the prompt_tap module's
#: `cli_groups` grew from ("prompt-tap",) to ("prompt-tap", "ledger") so the new guarded `ledger`
#: CLI parser group is CLAIMED by a module (else verify_cli_parity flags an unowned group). Since
#: `cli_groups` is a real Feature field projected by features_view(), the snapshot digest moved. A
#: deliberate, reviewed snapshot bump (precedent commit 0dc64498); no incumbent record changed.
#: Bumped 2026-09-14 (T-09): vendors own_files includes vendor_common.py and vendor_jobs.py.
#: Bumped 2026-09-14 (SRP batch): own_files gained the split modules.
#: Bumped 2026-09-14 (SRP batch): own_files gained the split modules.
#: Bumped 2026-09-14 (SRP batch): own_files gained the split modules.
#: Bumped 2026-09-14 (SRP batch): own_files gained the split modules.
#: Bumped 2026-09-14 (SRP batch): own_files gained the split modules.
#: Bumped 2026-09-14 (SRP batch): own_files gained the split modules.
#: Bumped 2026-09-14 (SRP batch): own_files gained the split modules.
#: Bumped 2026-09-14 (SRP batch): own_files gained the split modules.
#: Bumped 2026-09-14 (SRP batch): own_files gained the split modules.
#: Bumped 2026-09-14 (SRP batch): own_files gained the split modules.
#: Bumped 2026-09-14 (SRP batch): own_files gained the split modules.
#: Bumped 2026-09-14 (SRP batch): own_files gained the split modules.
#: Bumped 2026-09-14 (SRP batch): own_files gained the split modules.
#: Bumped 2026-09-14 (SRP batch): own_files gained the split modules.
#: Bumped 2026-09-14 (SRP batch): own_files gained the split modules.
#: Bumped 2026-09-14 (SRP batch): own_files gained the split modules.
#: Bumped 2026-10-01 (#356 phase 2a): a NEW `system_prompt_overrides` module row (the opt_in
#: DEFAULT-OFF org override of two system prompt heads; the terminology_concept_editor shape,
#: no web/api/cli of its own, kill switch at the service seam). No incumbent record changed.
#: Bumped 2026-10-02 (#356 phase 2b): the same row's description, cli_hole_reason and seams text
#: now name the per-workflow scope (no field added, no incumbent record changed).
#: Bumped 2026-10-06 (#533): the `databricks` and `advanced_workflows` rows gained `cli_refusal`
#: (their existing layer-2 CLI messages, now owned by the registry via `_common.feature_gate`).
#: Bumped 2026-10-13 (fields-managers gate): the custom_fields description says managers define
#: customer fields too (10-13). Copy only; no field, flag or route changed.
#: Bumped 2026-10-14 (open-core S2): the `assist` row's `web` dropped its two nested draft surfaces
#: (/memories/draft, /evaluations/draft), which ship with the paid add-ons. No other row moved.
#: Bumped 2026-10-19 (public 1.0.0): the terminology_concept_editor description lost its internal
#: spec label. Copy only; no field, flag or route changed.
_FEATURES_DIGEST = "ab319d8410683d71e93c93749bbffc45ee7d0e3b10b9e5c9123005106bae3e29"

#: The pre-migration router grouping: group -> ordered (submodule, attr) the three hand lists
#: in app/web/__init__.py used to carry. router_groups() must resolve exactly these, in order.
_EXPECTED_ROUTER_GROUPS = {
    "health": (("health", "router"),),
    "api": (
        ("api", "router"),
        ("api_cli", "router"),
        ("keys", "api_router"),
        ("images", "api_router"),
        ("advanced", "api_router"),
        ("utcp", "router"),
        # #753: the MCP OAuth grants /api routes (added after the migration, appended last).
        ("mcp_oauth_as", "api_router"),
    ),
    "web": (
        ("home", "router"),
        ("help", "router"),
        ("projects", "router"),
        ("documents", "router"),
        ("images", "router"),
        ("keys", "router"),
        ("workflows", "router"),
        ("glossaries", "router"),
        ("style_guides", "router"),
        ("content_types", "router"),
        ("custom_fields", "router"),
        ("tm", "router"),
        ("memories", "router"),
        ("memory_suggestions", "router"),
        ("evaluations", "router"),
        ("connectors", "router"),
        ("vendors", "router"),
        ("jobs_ui", "router"),
        ("auth", "router"),
        ("apikeys", "router"),
        ("admin", "router"),
        ("enterprise", "router"),
        ("routing", "router"),
        ("databricks", "router"),
        ("query_threads", "router"),
        ("worklist", "router"),
        ("scim", "router"),
        ("exports", "router"),
        ("advanced", "router"),
        # #753: the MCP OAuth authorization server pages and endpoints.
        ("mcp_oauth_as", "router"),
    ),
}

#: The optional slugs shipped as of this phase -- OPTIONAL_SLUGS is a ratchet that may only
#: GROW, so it must always be a superset of this floor (a silent shrink defeats the de-flag guard).
_OPTIONAL_SLUGS_FLOOR = frozenset(
    {
        "images",
        "keys",
        "key_images",
        "evaluations",
        "llmemory",
        "assist",
        "prompt_tap",
        "utcp",
        "vendors",
        "reports",
        "routing",
        "databricks",
        "queries",
    }
)

#: sha256 of repr(tuple(sorted(cli_expected().items()))) -- the 79 module-owned CLI leaf rows
#: (path + description) as they stood in EXPECTED_COMMANDS BEFORE the M3 relocation.
#: Bumped 2026-08-19 (Wave 2 reports Train B): two modules.py MODULE_CLI one-liners changed --
#: `report run` now names the five kinds (cost/tokens/volume/qa/postedit), and the databricks
#: `setup` row says "six stream tables" (Train C's sixth sparse stream). Gate-verified legitimate.
#: Prior bump 2026-08-19 (quick-win wave 1 merge): the key_projects `keys changes` row description
#: gained the --modified-after mode alongside --since (AP-1 delta-API rework, gate-verified).
#: Bumped 2026-08-19 (Wave 3 Train B): the new `queries` module contributes seven MODULE_CLI rows
#: (query open/list/show/reply/answer/close/assign) to cli_expected(); no incumbent row changed.
#: Bumped 2026-08-19 (Wave 3 Train D): the new `worklist` module contributes one MODULE_CLI row
#: (`work list`) to cli_expected(); no incumbent row changed.
#: Bumped 2026-08-24 (Sprint Train I): the new `scim` module contributes THREE MODULE_CLI rows
#: (scim status/users/groups — read-only operator views) to cli_expected(); no incumbent
#: row changed.
#: Bumped 2026-08-25 (Phase-3 Train A): the `memory suggestions` subgroup contributes TEN
#: MODULE_CLI['llmemory'] rows (suggestions list/show/evidence/generate/approve/reject/apply/
#: retry/recover/export) to cli_expected(); no incumbent row changed (they live under the
#: existing `memory` cli_group, so the module owns no new group). The `recover` leaf is the
#: build-gate MED fix (break-glass for a stuck `applying` row).
#: Bumped 2026-08-25 (LLMemory Phase 2 Train B, rebased onto A): TWO more `memory` cli_group
#: leaves (`memory janitor`, `memory resolve-conflict`) added to MODULE_CLI["llmemory"]. As
#: the last merger of the shared CLI files (§9), the digest is recomputed ONCE over the A+B
#: union (12 llmemory leaves total); no incumbent row changed.
#: Bumped 2026-08-27 (Batch 13 data-export Train B): the new `data_export` module contributes
#: FOUR MODULE_CLI rows (export run/list/status/download) to cli_expected() -- relocated from
#: the Train-A _CORE_EXPECTED placeholder (run/list/status) plus the new `download` leaf. No
#: incumbent row changed.
#: Bumped 2026-08-27 (Batch 14 T1): the new `key_images` module contributes FOUR MODULE_CLI
#: rows (keys image add/list/rm/set-primary) to cli_expected(); they live under the existing
#: `keys` cli_group (the module owns no new group), so no incumbent row changed.
#: Bumped 2026-08-30 (Batch 15 T2): THREE new `keys` cli_group leaves (keys empties,
#: deliver-approve, deliver-revoke -- the TM-only delivery empty gate, key/project mode)
#: added to MODULE_CLI["keys"]; they live under the existing `keys` cli_group, so no incumbent
#: row changed. The three `doc` verbs are CORE (_CORE_EXPECTED), not module-owned, so they do
#: NOT enter cli_expected() and do NOT move this digest.
#: Bumped 2026-09-01 (SPEC-advanced-workflows P0): FIVE new MODULE_CLI rows under the new
#: `advanced_workflows` slug (advanced create/run/status/transcript/cancel) enter cli_expected();
#: no incumbent row changed.
#: Bumped 2026-09-02 (SPEC-quality-lenses-p2 T2): FOUR new MODULE_CLI rows under the existing
#: `evaluations` slug (eval gold seed-demo/list/import, eval meta -- the Quality Lens
#: meta-evaluation harness) enter cli_expected(); they live under the existing `eval` cli_group,
#: so no incumbent row changed.
#: Bumped 2026-09-02 (SPEC-quality-lenses-p2 T3): FIVE new MODULE_CLI rows under the existing
#: `advanced_workflows` slug (advanced lens list/show/create/update/archive -- the Quality Lens
#: delivery-gate CRUD) enter cli_expected(); they live under the existing `advanced` cli_group,
#: so no incumbent row changed.
#: Bumped 2026-09-04 (deferred UX train): FOUR new MODULE_CLI rows under the existing
#: `advanced_workflows` slug (advanced explain, advanced show, advanced templates list/show --
#: operator help, the card run view, and mission templates) enter cli_expected(); they live under
#: the existing `advanced` cli_group, so no incumbent row changed.
#: Bumped 2026-09-05 (ROADMAP #6): TWO new MODULE_CLI rows under the existing `prompt_tap`
#: slug (prompt-tap runs, prompt-tap run -- the run-first Prompt Tap lens) enter cli_expected();
#: they live under the existing `prompt-tap` cli_group, so no incumbent row changed.
#: Re-bumped 2026-09-05 (gate round 2, L6): the two run-first rows' descriptor text moved from
#: `GET /api/tap/runs*` to the folded `GET /api/prompt-tap/runs*` namespace -- copy-only, still
#: under the same `prompt-tap` cli_group, no row added or removed.
#: Re-bumped 2026-09-06 (issue #105 fix round, MED-2): COPY-ONLY on two existing `keys` rows --
#: `keys empties` drops the "a TM-only workflow left held" misnaming (the gate keys on the durable
#: `tm_match` RUN MARKER, so a `tm_match -> llm_agent` run is held identically) and
#: `keys deliver-approve` documents its NEW 400 while a run is in flight. No row added or removed,
#: same `keys` cli_group.
#: Re-bumped 2026-09-06 (issue #108 MED-1, the merge-main round): COPY-ONLY on one existing
#: `advanced_workflows` row -- `advanced status` now states the run-status rule the widened MED-1
#: ruling introduced (an authored unit LOST to no candidate reads `failed` and is named in the
#: notes; a unit merely HELD keeps `completed`). No row added or removed, same `advanced`
#: cli_group.
#: Re-bumped 2026-09-06 (gate B round-2, HIGH-1 + MED-1): COPY-ONLY on two existing rows --
#: `keys deliver-approve` names BOTH run tiers the widened empty gate now sees (a classic
#: workflow run or an Advanced-Workflow run), and `advanced run` gains the same run-status rule
#: its `advanced status` twin and `docs/CLI.md` already carried (the two CLI descriptors for the
#: same command had drifted apart in coverage). No row added, removed or re-grouped.
#: Re-bumped 2026-09-06 (gate B round-3, HIGH-1): COPY-ONLY on ONE existing row --
#: `keys deliver-approve`'s in-flight sentence now also names the fan-out ITSELF.
#: No row added, removed or re-grouped.
#: Re-bumped 2026-09-06 (gate B round-4 MED-1): COPY-ONLY on `keys deliver-approve`.
#: The sentence now states the per-locale narrow predicate, including terminal release.
#: No row added, removed or re-grouped.
#: Re-bumped 2026-09-07 (retro HIGH-1): current-attempt intent on the same one CLI row.
#: Re-bumped 2026-09-07 (Prompt Tap Wave C, gate round-1 HIGH-1): the new guarded `ledger` CLI
#: group added its rows (`ledger status`, `ledger envelopes on|off`, `ledger purge`) to the
#: module-owned EXPECTED_COMMANDS, so cli_expected() gained those keys and the byte-identity
#: digest moved. A deliberate, reviewed snapshot bump (precedent commit 0dc64498); the keys
#: check below proves the derived set equals the real parser's module-group leaves.
#: Re-bumped 2026-09-13 (#91): advanced status copy names remote Parallel/NOTICE,
#: mission GET, 404 fallback and unchanged JSON; no CLI rows added or removed.
#: Re-bumped 2026-10-13 (fields-managers gate): COPY-ONLY on `field create` / `field update`,
#: which now name the manager rule instead of "admin". No row added, removed or re-grouped.
_CLI_DIGEST = "a71c5896d3054236d01f932d46990487c51d7351e99c8769e293e6e9cd4cd382"

#: sha256 of repr(tuple(sorted({tool.name: mcp_gate_for(tool)}.items()))) over the MODULE-
#: owned MCP tools -- the per-Tool feature_gate literals as registry.py held them BEFORE M3.
#: Bumped 2026-08-19 (Wave 3 Train B): six new module-owned tools (list_queries / get_query /
#: open_query / reply_to_query / set_query_status / assign_query), each gated queries_enabled and
#: derived from the queries module's /api/queries routes -- 18 -> 24 module-owned tools.
#: Bumped 2026-08-19 (Wave 3 Train D): one new module-owned tool (my_work), gated
#: worklist_enabled and derived from the worklist module's /api/worklist route -- 24 -> 25.
#: Bumped 2026-08-25 (Phase-3 Train A): one new module-owned tool (browse_memory_suggestions),
#: gated llmemory_suggestions_enabled and derived from the new module's /api/memory-suggestions
#: read routes -- 25 -> 26. The five mutating routes stay NEVER_COVER + EXEMPT (no tool), so
#: they add nothing to the derivation.
#: Bumped 2026-08-27 (Batch 14 T1): TWO new module-owned tools (manage_key_image write,
#: list_key_images read), both gated key_image_context_enabled and derived from the new
#: `key_images` module's /api/keys/*/images routes -- 26 -> 28 module-owned tools. The raw
#: bytes-serve route stays EXEMPT (no tool), so it adds nothing to the derivation.
#: Bumped 2026-09-01 (SPEC-advanced-workflows P0): FIVE new module-owned tools
#: (create_advanced_mission write, run_advanced_mission spend, advanced_run_status read,
#: advanced_run_transcript read, cancel_advanced_run write), each gated advanced_workflows_enabled
#: and derived from the new module's /api/advanced routes -- 28 -> 33 module-owned tools.
#: Bumped 2026-09-02 (SPEC-quality-lenses-p2 T2): ONE new module-owned tool
#: (run_meta_evaluation spend, gated evaluations_enabled, derived from the evaluations
#: module's new /api/evaluations/meta route) -- 33 -> 34 module-owned tools. The gold-set
#: verbs went on the EXISTING browse_evaluations / setup_evaluation tools (one view, two
#: actions), so they add routes to incumbent rows rather than tools to the derivation.
#: Bumped 2026-09-02 (SPEC-quality-lenses-p2 T3): TWO new module-owned tools (advanced_lenses
#: read + manage_advanced_lens write, both gated advanced_workflows_enabled, derived from the
#: advanced_workflows module's new /api/advanced/lenses routes) -- 34 -> 36 module-owned tools.
#: Bumped 2026-09-04 (deferred UX train): ONE new module-owned tool (advanced_templates read,
#: gated advanced_workflows_enabled, derived from the advanced_workflows module's new
#: /api/advanced/templates route) -- 36 -> 37 module-owned tools.
#: Bumped 2026-09-13 (#91): get_advanced_mission adds one Advanced Workflows read
#: tool behind the existing advanced_workflows_enabled gate.
#: Bumped 2026-10-14 (open-core S2): the 17 MCP tools of the paid add-ons (LLMemory, workflow
#: suggestions, Evaluations Lab, Advanced Workflows) left the registry with their code; the
#: derivation of every remaining tool is unchanged.
_MCP_DIGEST = "2c91a6c9f0152b39205e6892bb893e0092201d95856d289c61dc3985179ba1f4"

_REGISTRY_PATH = os.path.join(_ROOT, "integrations", "mcp-server", "registry.py")


def _fail(msg):
    raise AssertionError(msg)


def _features_digest(feats):
    return hashlib.sha256(repr(tuple(dataclasses.astuple(f) for f in feats)).encode()).hexdigest()


def _real_api_paths():
    return {p for _m, p in _routes.real_api_routes(app)}


def _real_web_paths():
    from app.web._routes import walk_routes

    out = set()
    for route in walk_routes(app):
        path = getattr(route, "path", "") or ""
        if path.startswith("/api"):
            continue
        if not (getattr(route, "methods", None) or ()):
            continue
        out.add(path)
    return out


def _parser_top_groups():
    import argparse

    top = set()
    for a in build_parser()._actions:
        if isinstance(a, argparse._SubParsersAction):
            top |= set(a.choices.keys())
    return top


# --------------------------------------------------------------------------- #
# check 1 -- well-formedness (every STRUCTURAL field resolves to a real repo object)
# --------------------------------------------------------------------------- #
def _module_wellformed_problems(module, api_paths, web_paths, groups):
    """Return a list of well-formedness problems for one Module. Prose fields
    (description/seams/cross_reach_allow reasons) are deliberately NOT checked (S3.2)."""
    p = []
    # Open core: a PAID feature whose add-on code is absent has no routes, CLI group or card in
    # this tree (they live in the add-on); the row itself, its flag and its gate still must
    # resolve, so only the structural-presence checks are skipped for it.
    absent = not editions.feature_installed(module.slug)
    for pat in () if absent else module.api:
        if not any(F._matches(pat, x) for x in api_paths):
            p.append(f"api pattern {pat!r} matches no real /api route")
    for pat in () if absent else module.web:
        if not any(F._matches(pat, x) for x in web_paths):
            p.append(f"web pattern {pat!r} matches no real web route")
    for pat in () if absent else module.hooks:
        if not any(F._matches(pat, x) for x in (api_paths | web_paths)):
            p.append(f"hooks pattern {pat!r} matches no real route")
    for g in () if absent else module.cli_groups:
        if g not in groups:
            p.append(f"cli_group {g!r} is not a real argparse group")
    if module.mcp_gate != module.setting_key:
        p.append(f"mcp_gate {module.mcp_gate!r} != setting_key {module.setting_key!r}")
    if module.setting_key not in settings.DEFAULTS:
        p.append(f"setting_key {module.setting_key!r} not in settings.DEFAULTS")
    for card in () if absent else module.cards:
        if not os.path.exists(os.path.join(_ROOT, "app", "capabilities", card + ".md")):
            p.append(f"card {card!r} resolves to no app/capabilities/{card}.md")
    return p


def check_well_formed():
    api_paths, web_paths, groups = _real_api_paths(), _real_web_paths(), _parser_top_groups()
    slugs, keys = set(), set()
    for m in M.MODULES:
        assert m.slug not in slugs, f"duplicate slug {m.slug!r}"
        assert m.setting_key not in keys, f"duplicate setting_key {m.setting_key!r}"
        slugs.add(m.slug)
        keys.add(m.setting_key)
        probs = _module_wellformed_problems(m, api_paths, web_paths, groups)
        assert not probs, "module {!r} not well-formed:\n  {}".format(m.slug, "\n  ".join(probs))
        if not m.cli_groups:
            assert m.cli_hole_reason, (
                f"module {m.slug!r} declares no CLI group but no cli_hole_reason"
            )
    # ROUTER_GROUPS attrs resolve to real APIRouter objects (router_groups() would raise
    # otherwise); every tag is a known group.
    from fastapi import APIRouter

    for group_list in M.router_groups():
        for r in group_list:
            assert isinstance(r, APIRouter), f"router_groups() yielded a non-APIRouter {r!r}"
    assert {g for _d, g in M.ROUTER_GROUPS} <= set(M._GROUP_ORDER), (
        "ROUTER_GROUPS has an unknown group tag"
    )
    print(
        "  [1] well-formed: %d modules, structural fields resolve to real routes/groups/"  # noqa: UP031
        "settings/cards/routers, prose carved out, slugs+keys unique OK" % len(M.MODULES)
    )


# --------------------------------------------------------------------------- #
# check 2 -- snapshot equivalence (features_view + router_groups byte-identity)
# --------------------------------------------------------------------------- #
def check_snapshots():
    dig = _features_digest(M.features_view())
    assert dig == _FEATURES_DIGEST, (
        f"features_view() digest {dig} != pinned pre-migration FEATURES digest {_FEATURES_DIGEST} -- a manifest "  # noqa: E501
        "field drifted from the incumbent table"
    )
    # app.features.FEATURES is that same view (the once-at-import constant).
    assert _features_digest(F.FEATURES) == _FEATURES_DIGEST, (
        "app.features.FEATURES digest drifted from the pinned snapshot"
    )
    # router_groups() resolves exactly the pre-migration attrs, in order (object identity).
    import importlib

    groups = dict(zip(M._GROUP_ORDER, M.router_groups(), strict=False))
    for group, expected in _EXPECTED_ROUTER_GROUPS.items():
        got = groups[group]
        # Open core: a paid router whose add-on code is absent is skipped (modules.router_groups).
        want = [
            getattr(importlib.import_module("app.web." + sm), at)
            for sm, at in expected
            if editions.module_installed("app.web." + sm)
        ]
        assert got == want, (
            f"router_groups()[{group!r}] is not byte-identical to the pinned pre-migration list "
            "(order/identity mismatch)"
        )
    print(
        "  [2] snapshot: features_view() reproduces the pinned FEATURES digest; "
        "router_groups() composes the exact pre-migration three groups, in order OK"
    )


# --------------------------------------------------------------------------- #
# check 3 -- FEATURES identity (M9)
# --------------------------------------------------------------------------- #
def check_features_identity():
    assert isinstance(F.FEATURES, tuple), "app.features.FEATURES must be a tuple constant"
    ident = {id(f) for f in F.FEATURES}
    for name, d in (
        ("_BY_SLUG", F._BY_SLUG),
        ("_BY_KEY", F._BY_KEY),
        ("_CLI_GROUP_TO_FEATURE", F._CLI_GROUP_TO_FEATURE),
        ("_CARD_OWNER", F._CARD_OWNER),
    ):
        for v in d.values():
            assert id(v) in ident, (
                f"{name} holds a Feature that is NOT an element of the FEATURES tuple -- the view "
                "was evaluated more than once"
            )
    print(
        "  [3] FEATURES identity: one tuple constant behind _BY_SLUG/_BY_KEY/"
        "_CLI_GROUP_TO_FEATURE/_CARD_OWNER (no second evaluation) OK"
    )


# --------------------------------------------------------------------------- #
# check 4 -- the ratcheted optional-slug allowlist (S3.5)
# --------------------------------------------------------------------------- #
def check_ratchet():
    module_slugs = {m.slug for m in M.MODULES}
    removed = dict(M.REMOVED_OPTIONAL)
    assert len(removed) == len(M.REMOVED_OPTIONAL), "duplicate slug in REMOVED_OPTIONAL"
    # OPTIONAL_SLUGS may only GROW: it must cover the shipped floor.
    assert M.OPTIONAL_SLUGS >= _OPTIONAL_SLUGS_FLOOR, (
        "OPTIONAL_SLUGS shrank below the shipped floor (a slug was silently dropped): missing {}".format(  # noqa: E501
            ", ".join(sorted(_OPTIONAL_SLUGS_FLOOR - M.OPTIONAL_SLUGS))
        )
    )
    # Coverage: every ratcheted slug is EITHER live in MODULES OR recorded as removed.
    uncovered = M.OPTIONAL_SLUGS - module_slugs - set(removed)
    assert not uncovered, (
        "OPTIONAL_SLUGS entries neither in MODULES nor in REMOVED_OPTIONAL (a capability was "
        "de-flagged without a recorded removal -- the silent-de-flag hole): {}".format(
            ", ".join(sorted(uncovered))
        )
    )
    # Staleness both directions: reasons non-empty, and no removed slug still live.
    for slug, reason in M.REMOVED_OPTIONAL:
        assert reason and reason.strip(), f"REMOVED_OPTIONAL[{slug!r}] has an empty reason"
        assert slug not in module_slugs, (
            f"REMOVED_OPTIONAL names {slug!r} but it is STILL in MODULES (stale/contradictory)"
        )
        assert slug in M.OPTIONAL_SLUGS, (
            f"REMOVED_OPTIONAL names {slug!r} which was never an OPTIONAL_SLUG"
        )
    print(
        "  [4] ratchet: OPTIONAL_SLUGS (%d) covers the floor, {MODULES}U{REMOVED_OPTIONAL} "  # noqa: UP031
        "superset OPTIONAL_SLUGS, reasons non-empty, no stale removal OK" % len(M.OPTIONAL_SLUGS)
    )


# --------------------------------------------------------------------------- #
# check 5 -- non-vacuity / sabotage (a dropped manifest surface reddens /api accounting)
# --------------------------------------------------------------------------- #
def check_sabotage():
    api_paths = _real_api_paths()

    def unclaimed():
        n = 0
        for p in api_paths:
            try:
                if F.api_owner(p) is None:
                    n += 1
            except ValueError:
                n += 1
        return n

    assert unclaimed() == 0, "baseline /api accounting is not clean"
    # Rebuild a MUTATED FEATURES with the images module's /api pattern dropped, install it
    # as the derivation would, and confirm the DOWNSTREAM owner accounting (F.api_owner over
    # the real router) now unclaims a real route -- so a manifest surface drop is load-bearing.
    victim = F.feature_by_slug("images")
    mutated = tuple(f if f.slug != "images" else dataclasses.replace(f, api=()) for f in F.FEATURES)
    saved = F.FEATURES
    F.FEATURES = mutated
    try:
        assert unclaimed() > 0, (
            "dropping the images module's /api pattern left /api accounting clean -- the "
            "manifest->downstream derivation would be vacuous"
        )
    finally:
        F.FEATURES = saved
    assert unclaimed() == 0, "FEATURES was not restored after the sabotage probe"
    assert victim.api, "sanity: the images module really does declare an /api pattern"
    print(
        "  [5] sabotage: dropping a module's /api surface unclaims a real route in the "
        "downstream owner accounting (restored) OK"
    )


# --------------------------------------------------------------------------- #
# check 6 -- anti-framework bounding (a bogus STRUCTURAL referent fails; free prose does not)
# --------------------------------------------------------------------------- #
def check_anti_framework():
    api_paths, web_paths, groups = _real_api_paths(), _real_web_paths(), _parser_top_groups()
    base = F.feature_by_slug("images")
    common = dict(setting_key=base.setting_key, mcp_gate=base.setting_key)  # noqa: C408

    # A planted row with a structural field that has no real referent -> flagged.
    bogus_api = M.Module(
        slug="x",
        title="X",
        description="anything at all",
        beta=False,
        semantics="removed",
        api=("/api/__no_such_route__",),
        **common,
    )
    probs = _module_wellformed_problems(bogus_api, api_paths, web_paths, groups)
    assert any("api pattern" in s for s in probs), (
        "a bogus /api structural referent was NOT flagged -- the anti-framework law is not holding"
    )
    bogus_group = M.Module(
        slug="x",
        title="X",
        description="anything",
        beta=False,
        semantics="opt_in",
        cli_groups=("__no_such_group__",),
        cli_hole_reason="",
        **common,
    )
    probs = _module_wellformed_problems(bogus_group, api_paths, web_paths, groups)
    assert any("cli_group" in s for s in probs), "a bogus cli_group was NOT flagged"

    # A prose-only field with arbitrary free text is NOT a structural referent -> passes.
    prose = M.Module(
        slug="x",
        title="X",
        description="free prose no guard can or should verify -- lorem ipsum",
        beta=False,
        semantics="removed",
        web=("/images",),
        seams=("an arbitrary human seam note that resolves to nothing",),
        **common,
    )
    probs = _module_wellformed_problems(prose, api_paths, web_paths, groups)
    assert not probs, (
        f"a prose-only field was wrongly flagged as a broken structural referent: {probs!r}"
    )
    print(
        "  [6] anti-framework: a bogus /api + a bogus cli_group referent fail well-formedness; "
        "free-text prose (description/seams) does not -- the carve-out holds OK"
    )


# --------------------------------------------------------------------------- #
# check 7 -- standalone-import property (S3.6)
# --------------------------------------------------------------------------- #
def check_standalone_import():
    probe = (
        "import sys, app.modules as m; "
        "assert 'app.web' not in sys.modules, 'app.web was imported'; "
        "assert 'app.features' not in sys.modules, 'app.features was imported'; "
        "assert len(m.MODULES) >= 1 and m.OPTIONAL_SLUGS and m.ROUTER_GROUPS; "
        "print('OK', len(m.MODULES))"
    )
    env = dict(os.environ)
    env["LOC_TMS_DATA_DIR"] = os.path.join(os.environ.get("TEMP", "/tmp"), "loctms_mod_stand")
    proc = subprocess.run(
        [sys.executable, "-c", probe], cwd=_ROOT, env=env, capture_output=True, text=True
    )
    assert proc.returncode == 0, (
        "import app.modules did NOT succeed standalone (rc=%d):\n%s\n%s"  # noqa: UP031
        % (proc.returncode, proc.stdout, proc.stderr)
    )
    assert proc.stdout.strip().startswith("OK"), (
        f"standalone import probe produced unexpected output: {proc.stdout!r} / {proc.stderr!r}"
    )
    print(
        "  [7] standalone-import: `import app.modules` succeeds in a fresh interpreter with "
        "app.web AND app.features absent from sys.modules OK"
    )


# --------------------------------------------------------------------------- #
# check 8 -- CLI mirror derivation (M3): cli_expected() reproduces the pre-relocation
# module-owned EXPECTED_COMMANDS rows byte-for-byte, and its key set IS exactly the real
# parser's module-group leaves (growth-tolerant, non-vacuous). Sabotage-proven.
# --------------------------------------------------------------------------- #
def _leaf_paths(parser, prefix=()):
    subs = [a for a in parser._actions if a.__class__.__name__ == "_SubParsersAction"]
    if not subs:
        yield " ".join(prefix)
        return
    for a in subs:
        for name, sub in a.choices.items():
            yield from _leaf_paths(sub, prefix + (name,))  # noqa: RUF005


def check_cli_derivation():
    # The byte-pinned snapshot covers the FULL table (every add-on's rows); the open core then
    # checks the installed subset against the real parser.
    full = M.cli_expected(include_absent=True)
    ce = M.cli_expected()
    dig = hashlib.sha256(repr(tuple(sorted(full.items()))).encode()).hexdigest()
    assert dig == _CLI_DIGEST, (
        f"cli_expected() digest {dig} != pinned pre-relocation module-row digest {_CLI_DIGEST} -- the "  # noqa: E501
        "relocated CLI rows drifted from EXPECTED_COMMANDS"
    )
    # The derived key set is EXACTLY the parser leaves whose top group is a module cli_group
    # (derived live off the real parser -- grows correctly, and proves no module leaf is missed
    # and no core leaf is captured).
    module_groups = {
        g for m in M.MODULES if editions.feature_installed(m.slug) for g in m.cli_groups
    }
    leaves = {p for p in _leaf_paths(build_parser()) if p}
    expected_keys = {p for p in leaves if p.split()[0] in module_groups}
    assert set(ce) == expected_keys, (
        "cli_expected() keys != the real parser's module-group leaves.\n  missing: {}\n  extra: {}".format(  # noqa: E501, UP032
            sorted(expected_keys - set(ce)), sorted(set(ce) - expected_keys)
        )
    )
    # Sabotage (non-vacuous): drop a real relocated row -> the byte-identity digest reddens.
    saved = M.MODULE_CLI
    victim_slug = "vendors"
    M.MODULE_CLI = {k: (v[1:] if k == victim_slug else v) for k, v in saved.items()}
    try:
        bad = hashlib.sha256(
            repr(tuple(sorted(M.cli_expected(include_absent=True).items()))).encode()
        ).hexdigest()
        assert bad != _CLI_DIGEST, (
            "sabotage: dropping a MODULE_CLI row did NOT change the digest -- the proof is vacuous"
        )
    finally:
        M.MODULE_CLI = saved
    assert (
        hashlib.sha256(
            repr(tuple(sorted(M.cli_expected(include_absent=True).items()))).encode()
        ).hexdigest()
        == _CLI_DIGEST
    ), "MODULE_CLI was not restored"
    print(
        "  [8] CLI derivation: cli_expected() reproduces the pinned pre-relocation module "  # noqa: UP031
        "rows (%d), key set == the real parser's module-group leaves, sabotage-proven OK" % len(ce)
    )


# --------------------------------------------------------------------------- #
# check 9 -- MCP mirror derivation (M3): mcp_gate_for(tool) reproduces every module-owned
# Tool.feature_gate literal (and returns None for core/perimeter tools), byte-for-byte vs the
# pre-M3 registry. registry.py keeps its literal (imports nothing); the derivation lives here.
# Sabotage-proven.
# --------------------------------------------------------------------------- #
def _load_registry():
    import importlib.util

    spec = importlib.util.spec_from_file_location("loc_mcp_registry", _REGISTRY_PATH)
    reg = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = reg
    spec.loader.exec_module(reg)
    return reg


def check_mcp_derivation():
    reg = _load_registry()
    setting_keys = {m.setting_key for m in M.MODULES}
    module_owned = {}
    for tool in reg.TOOLS:
        derived = M.mcp_gate_for(tool)
        if tool.feature_gate in setting_keys:
            # a module-owned tool: the derivation must reproduce its literal exactly.
            assert derived == tool.feature_gate, (
                f"tool {tool.name!r}: mcp_gate_for derived {derived!r} != its feature_gate literal {tool.feature_gate!r}"  # noqa: E501
            )
            module_owned[tool.name] = derived
        else:
            # a core/perimeter tool (e.g. gated on remote_admin_enabled): no module owns its
            # routes, so the derivation declines it (None) and it keeps its own hand-set gate.
            assert derived is None, (
                "tool {!r} is not module-owned (feature_gate={!r}) yet mcp_gate_for derived {!r} -- "  # noqa: E501, UP032
                "the derivation must not invent a module gate".format(
                    tool.name, tool.feature_gate, derived
                )
            )
    dig = hashlib.sha256(repr(tuple(sorted(module_owned.items()))).encode()).hexdigest()
    assert dig == _MCP_DIGEST, (
        f"mcp_gate_for digest {dig} != pinned pre-M3 per-Tool gate digest {_MCP_DIGEST}"
    )
    # Sabotage (non-vacuous): without the routing module's /api surface, mcp_gate_for can no
    # longer derive the MIXED manage_admin_ops tool's gate -> the equality would redden.
    tool = next(t for t in reg.TOOLS if t.name == "manage_admin_ops")
    assert M.mcp_gate_for(tool) == "routing_enabled"
    mutated = tuple(
        f if f.slug != "routing" else dataclasses.replace(f, api=()) for f in F.FEATURES
    )
    saved = F.FEATURES
    F.FEATURES = mutated
    try:
        assert M.mcp_gate_for(tool) != "routing_enabled", (
            "sabotage: dropping the routing module's /api surface still derived its MCP gate -- "
            "the derivation is not reading the manifest"
        )
    finally:
        F.FEATURES = saved
    assert M.mcp_gate_for(tool) == "routing_enabled", "FEATURES was not restored"
    print(
        "  [9] MCP derivation: mcp_gate_for reproduces all %d module-owned Tool gates "  # noqa: UP031
        "(and declines core/perimeter tools), byte-identical to the pre-M3 registry, "
        "sabotage-proven OK" % len(module_owned)
    )


# --------------------------------------------------------------------------- #
# check 10 -- MODULE_CLI slug-KEY integrity (M1+M3 gate LOW follow-up). cli_expected()
# FLATTENS MODULE_CLI ignoring the slug KEY, so check 8 (leading token against the UNION of
# every module's cli_groups) cannot catch a CLI row filed under the WRONG module's slug --
# e.g. a `vendor ...` row parked under the "reports" slug still passes, since `vendor` is
# SOME module's group. Tighten it: each MODULE_CLI[slug] row's leading token must be in THAT
# slug's own Module.cli_groups. A verification tightening only (machinery frozen after M3) --
# cli_expected() and every manifest field are untouched.
# --------------------------------------------------------------------------- #
def _slug_key_problems(module_cli, by_slug) -> list:
    """Every MODULE_CLI row whose leading token is not in its OWN slug's cli_groups (or whose
    slug names no Module). Empty list == clean."""
    probs = []
    for slug, rows in module_cli.items():
        mod = by_slug.get(slug)
        if mod is None:
            probs.append(f"MODULE_CLI slug {slug!r} names no Module in MODULES")
            continue
        allowed = set(mod.cli_groups)
        for path, _desc in rows:
            top = path.split()[0] if path.split() else ""
            if top not in allowed:
                probs.append(
                    f"MODULE_CLI[{slug!r}] row {path!r}: leading token {top!r} not in that module's cli_groups "  # noqa: E501
                    f"{tuple(sorted(allowed))!r} (a CLI row filed under the wrong module's slug)"
                )
    return probs


def check_cli_slug_keys():
    by_slug = {m.slug: m for m in M.MODULES}
    probs = _slug_key_problems(M.MODULE_CLI, by_slug)
    assert not probs, "MODULE_CLI slug-key integrity failed:\n  {}".format("\n  ".join(probs))
    # Sabotage (non-vacuous): refile a REAL row under a DIFFERENT module's slug whose
    # cli_groups do NOT include its leading token. check 8's UNION test would still pass (the
    # token is some module's group), so this proves the per-slug tightening catches exactly
    # what the union test misses.
    saved = M.MODULE_CLI
    donor, host = "vendors", "reports"
    assert donor in saved and host in saved, "sabotage fixture slugs missing from MODULE_CLI"
    moved_row = saved[donor][0]  # ('vendor add', ...) -- leading token 'vendor'
    union = {g for m in M.MODULES for g in m.cli_groups}
    assert moved_row[0].split()[0] in union, "sabotage sanity: the donor token IS a real group"
    M.MODULE_CLI = {**saved, host: saved[host] + (moved_row,)}
    try:
        assert _slug_key_problems(M.MODULE_CLI, by_slug), (
            "sabotage: a row refiled under the wrong slug did NOT redden the per-slug check"
        )
    finally:
        M.MODULE_CLI = saved
    assert not _slug_key_problems(M.MODULE_CLI, by_slug), "MODULE_CLI was not restored"
    print(
        "  [10] CLI slug-key integrity: every MODULE_CLI[slug] row's leading token is in "
        "THAT slug's cli_groups (a mis-filed row check 8's union misses), sabotage-proven OK"
    )


def main():
    init_db()
    check_well_formed()
    check_snapshots()
    check_features_identity()
    check_ratchet()
    check_sabotage()
    check_anti_framework()
    check_standalone_import()
    check_cli_derivation()
    check_mcp_derivation()
    check_cli_slug_keys()
    print("MODULES OK")


if __name__ == "__main__":
    main()
