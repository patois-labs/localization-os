"""Headless-CLI capability-parity harness - suite #42.

THE MACHINE CHECK behind the web+CLI parity rule: *every* capability in the
Headless CLI master plan's Part 1 capability inventory must have a NON-UI path,
and this suite proves each one actually exists by INTROSPECTING the argparse
parser tree in app/cli/__main__.py (it builds the real parser, walks the nested
subparsers, and asserts every expected command path resolves to a leaf command).

WHY BY-CONSTRUCTION: EXPECTED_COMMANDS below is the declarative capability ->
command-path table. The suite builds the actual parser, collects its leaf command
paths, and asserts:
  (a) every EXPECTED command path exists in the parser  (nothing was renamed /
      removed out from under a capability), AND
  (b) every parser leaf is accounted for in EXPECTED_COMMANDS  (nothing shipped
      without a capability entry -> forces this table to stay complete).
So the harness FAILS if a command is removed OR renamed (a) and FAILS if a new
command is added without registering it here (b). A bogus expected entry (a
command path that does not exist) also fails (a) - i.e. the table cannot lie.

>>> THE PARITY RULE (read before adding a feature) <<<
Any new user-facing feature MUST ship BOTH its web route AND its CLI command, and
MUST add an entry to EXPECTED_COMMANDS below. That third step is what this suite
enforces: a new leaf command with no entry fails check (b); a promised capability
whose command was dropped fails check (a). This is the guardrail that keeps the
CLI a first-class, complete operator surface rather than drifting behind the UI.

INHERENTLY-INTERACTIVE EXCEPTIONS: a tiny set of capabilities cannot be fully
headless (connector OAuth *authorize* is a browser consent round-trip - see
INTERACTIVE_EXCEPTIONS). For those we assert they are DOCUMENTED as exceptions
(in docs/CLI.md) rather than requiring a command, and we assert the CLI adjuncts
that DO exist for them (oauth-status / set-oauth-secret) are present.

Hermetic like the rest: throwaway LOC_TMS_DATA_DIR + cleared provider keys at
import (introspection touches no DB, but importing app.cli pulls in app.models
etc., so we stay consistent with the other suites). ASCII-only prints.
"""

import os
import re
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(_ROOT)
sys.path.insert(0, _ROOT)

# Throwaway data dir + no provider keys (keeps this suite identical in posture to
# the others even though building the parser opens no DB and calls no provider).
from tests._bootstrap import init_env

init_env("loctms_parity_")

from app.cli.__main__ import build_parser
from app.modules import cli_expected

# ============================================================================
# THE CAPABILITY -> CLI-COMMAND TABLE.
#
# Each entry is  "<command path>": "<capability from the master plan Part 1
# inventory>".  The command path is a space-joined argparse leaf (group +
# subcommands). This is the single source of truth the parser is checked against.
#
# Grouped by the Part 1 inventory sections so a reviewer can see, section by
# section, that every UI-only-today capability now has a non-UI path.
# ============================================================================
_CORE_EXPECTED = {
    "lqa jev-refine": "CLI-only bounded Jev-guided post-edit transcript experiment",
    "lqa qe-compare": "compare LLM and Jev QE routing on JSONL pairs (Lab comparison adjunct)",
    "version": "read baked release identity",
    "release manifest": "read release inventory locally or remotely",
    "db backup-full": "full-install backup alias",
    # --- (iii) Ops that were startup/doc-only: schema, backup/restore, encrypt,
    #     job recovery, audit-chain verify, health. -----------------------------
    "db migrate": "bring the schema up to date (Postgres alembic / SQLite init_db)",
    "db init-schema": "create/upgrade the SQLite schema (init_db)",
    "db backup": "consistent DB backup (WAL-checkpoint + atomic copy / pg_dump); "
    "--full writes a whole-install .tar.gz (DB + uploads + keyfiles, gap G-06)",
    "db verify-backup": "verify a --full backup archive: manifest completeness, "
    "per-member sha256, PRAGMA integrity_check on the contained DB, keyfile "
    "presence/non-emptiness (the restore-verification control)",
    "db restore": "restore from a backup (destructive); auto-detects a --full "
    "archive (DB + uploads + keyfiles) vs a legacy single-file DB backup",
    "db encrypt": "write a SQLCipher-encrypted copy of a plaintext DB",
    "db decrypt": "write a plaintext copy of an encrypted DB",
    "db recover-jobs": "re-queue interrupted jobs after a restart",
    "db protect-data": "mark the data dir PROTECTED so the server refuses to serve "
    "it without LOC_TMS_ALLOW_REAL_DATA=1 (issue #13 data-dir guard)",
    "db scrub-scaffold": "scan segments + stored TM for prompt-scaffold echoes (core-QA "
    "Part 1 cleanup); --apply quarantines them (segments -> "
    "needs_review + scaffold flag, TM -> delete)",
    "db verify": "read-only DB integrity/health check - SQLite PRAGMA integrity_check "
    "+ foreign_key_check, alembic head vs stamped revision, portable "
    "referential-sanity orphan counts (distinct from `audit verify`, "
    "which checks the audit hash chain, not schema/data integrity)",
    "health": "DB readiness probe (mirror of /ready)",
    "audit verify": "verify the tamper-evident audit hash chain end to end",
    "audit reset-anchor": "HOST-ONLY, double-confirmed break-glass: re-baseline the "
    "tail-truncation anchor to the current head after a legitimate `db restore` to an OLDER "
    "backup (G-10 train). Deliberately NO web route — it is the exact 'erase truncation "
    "evidence' move an attacker wants, so it stays a local-root CLI op like `db restore` / "
    "`db backup` (justified host/admin asymmetry, per the parity memory)",
    "audit rotate-key": "HOST-ONLY maintenance: rotate the audit signing key, keeping retired "
    "keys for verification (F21 #279). Refuses while the server is listening. Deliberately NO "
    "web route, like `audit reset-anchor` and `db restore` (key material never leaves the host)",
    # --- (ii) Config keys with a store but NO UI form (+ the generic surface). -
    "config get": "read one setting (secrets masked)",
    "config set": "set one setting - unlocks mt_concurrency / sync_scheduler_enabled"
    " / api_rate_limit_per_minute / session_idle_minutes / project_scoping",
    "config show": "configured private egress policy with source, local or remote",
    "config list": "list settings, grouped, secrets masked",
    "config apply": "apply many settings from TOML atomically (declarative provisioning)",
    "config export-env": "scaffold a .env of install-time env vars (secrets masked)",
    "config features": "list the toggleable features + their on/off state (the CLI "
    "twin of the Settings > Features card; remote-capable via "
    "GET /api/admin/features)",
    # The WRITE twin of `config features`, and the parity fix it closes: the Settings >
    # Features card (POST /settings/features/{slug}) had no headless equivalent at all --
    # `config set <key> 0|1` is require_local, so a remote/headless operator could not
    # flip a flag on an install they were not sitting in front of. Both verbs work
    # locally and over --remote (POST /api/admin/features/{slug}).
    "config feature enable": "turn one feature ON by slug (the headless twin of the "
    "Settings > Features card; POST /settings/features/{slug} parity)",
    "config feature disable": "turn one feature OFF by slug (the headless twin of the "
    "Settings > Features card; POST /settings/features/{slug} parity)",
    # --- Enterprise turn-on UX: the ONE enablement door for the six flag-OFF sprint
    #     features (OTel/metrics/SAML/SCIM/PII-NER/job-queue) + the S3 chain, with a
    #     boot-brick preflight. CLI twin of the /settings/enterprise panel; both are thin
    #     callers of app/services/enterprise_enablement.py.
    "enterprise status": "each enterprise feature's on/off state + OFFLINE preflight "
    "(green/red + reason); no network probes (web /settings/enterprise panel-load parity)",
    "enterprise preflight": "run one feature's full preflight incl. bounded live-network "
    "probes (OTel POST / S3 chain-resolve) -- the web 'Run preflight' button parity",
    "enterprise enable": "enable one feature: OFFLINE preflight first, refuse on red unless "
    "--force, then flip the switch (POST /settings/enterprise/enable parity)",
    "enterprise disable": "disable one feature; never runs a preflight (POST "
    "/settings/enterprise/disable parity)",
    # --- (iii) Headless first-admin seed. ------------------------------------
    "init": "headless first-admin bootstrap + login-wall enable (idempotent, default-off)",
    # --- (i) Customer / project create + apply-langset (route-body extraction). -
    "customer create": "create a customer (UI-only logic extracted to services.content)",
    "customer list": "list customers",
    "customer show": "customer 360 (projects/connectors/workflows/pairs/TM; GET /customers/{id} parity)",  # noqa: E501
    "customer update": "set a customer's preferences (settings blob; POST /customers/{id}/settings parity)",  # noqa: E501
    "customer set-default": "set a customer's scoped defaults (provider/workflow/segmentation)",
    "customer pairs list": "list a customer's language pairs (declared + derived)",
    "customer pairs add": "add an explicit 'src->tgt' pair (POST /customers/{id}/pairs parity)",
    "customer pairs remove": "remove an explicit 'src->tgt' pair (POST /customers/{id}/pairs/remove parity)",  # noqa: E501
    "customer pairs import": "bulk-import language pairs from CSV (source_locale,"
    "target_locale columns), atomic + idempotent (CLI-parity "
    "backlog item 2; POST /api/customers/{id}/pairs/import parity)",
    "customer rename": "rename a customer (SPEC-rename-delete; POST /customers/{id}/rename parity)",
    "customer delete": "delete a customer - fails closed while anything references it, "
    "no cascade (SPEC-rename-delete; POST /customers/{id}/delete parity)",
    "customer archive": "archive (retire) a customer - drops it from every picker + the "
    "default list (SPEC-customer-archive; POST /customers/{id}/archive parity)",
    "customer unarchive": "unarchive (reactivate) a customer - offer it in pickers again "
    "(SPEC-customer-archive; POST /customers/{id}/unarchive parity)",
    "customer set-ai": "AI opt-out for every project of a customer (HECVAT DPAI-08; "
    "POST /customers/{id}/ai-opt-out parity)",
    "project create": "create a project + its target locales (extracted to services.content)",
    "project author": "AI-draft a validated project setup from a plain-language "
    "description (SPEC-promptable-setup; POST /projects/author.json parity)",
    "project list": "list projects",
    "project show": "full project detail (locales, docs, TM/glossary counts)",
    "project leverage": "7-band word-based TM-leverage breakdown (CV-1; the project page "
    "7-band bar + GET /api/projects/{id}/leverage parity)",
    "project add-locale": "add one target locale (additive, idempotent)",
    "project remove-locale": "remove one target locale - untranslated locales only "
    "(translated = permanent), deletes seeded segments, keeps "
    "TM + glossary, typed-confirm + last-locale + permanence "
    "guard (POST /projects/{id}/remove-locale parity)",
    "project apply-langset": "apply a Language Set to a project (extracted apply-langset)",
    "project status": "set a project's lifecycle status (P1; POST /projects/{id}/status parity)",
    "project set-due": "set/clear a project's optional due date (Feature A; POST /projects/{id}/due parity)",  # noqa: E501
    "project set-content-type": "set/clear a project's content type (SPEC-content-types; "
    "POST /projects/{id}/content-type parity)",
    "project set-intake": "switch an EMPTY project between uploads and a connected source "
    "(Connected projects; POST /projects/{id}/intake parity)",
    "project make-connected": "make an existing connection-fed project a connected project "
    "(Connected projects; POST /projects/{id}/make-connected parity)",
    "project set-field": "set/clear a custom project field value (POST /projects/{id}/fields)",
    "project set-providers": "set per-pair provider mode + mapping (Phase 2; POST /projects/{id}/pair-providers parity)",  # noqa: E501
    "project set-customer": "assign a customer to a customer-less project - from-NULL "
    "only, no reassignment (POST /projects/{id}/set-customer parity)",
    "project export": "download a project's translated files, per-locale or ZIP "
    "(P1; GET /projects/{id}/export parity)",
    "project tm-sources list": "list a project's ATTACHED TM sources (project page "
    "'Translation memory sources' card parity)",
    "project tm-sources add": "attach another project's TM as a source - a reference, "
    "not a copy (POST /projects/{id}/tm-sources/add parity)",
    "project tm-sources remove": "detach a TM source, restoring the previous matching "
    "(POST /projects/{id}/tm-sources/remove parity)",
    "project glossaries list": "list a project's ATTACHED module glossaries (project "
    "page 'Attached glossaries' card parity)",
    "project glossaries add": "attach/pin an existing module glossary to a project "
    "(POST /projects/{id}/glossaries/add parity)",
    "project glossaries remove": "detach a pinned glossary from a project "
    "(POST /projects/{id}/glossaries/remove parity)",
    "project rename": "rename a project (SPEC-rename-delete; POST /projects/{id}/rename parity)",
    "project delete": "full-cascade project delete - itemized pre-counts, --force typed "
    "confirm, fails closed on connection/routing-rule referrers "
    "(SPEC-rename-delete; POST /projects/{id}/delete parity)",
    "project archive": "archive (retire) a project - drops it from the default list + "
    "pickers and blocks new runs, data preserved + reversible "
    "(residual sweep; POST /projects/{id}/archive parity)",
    "project unarchive": "unarchive (reactivate) a project - list it + make it runnable "
    "again (residual sweep; POST /projects/{id}/unarchive parity)",
    "project set-ai": "AI opt-out - switch AI off/on for a project, no content to any AI "
    "engine (HECVAT DPAI-08; POST /projects/{id}/ai-opt-out parity)",
    "project set-comments": "translate comments in Office files on/off for a project "
    "(#690; POST /projects/{id}/translate-comments parity)",
    # --- Project automation (MVP part A): a project's content source and run schedule.
    #     Web page + /api/projects/{id}/automation|source|runs + these leaves + the MCP tools
    #     project_automation / project_run share services.project_automation. ------------
    "project bind": "set a project's content source: the connection it pulls from and writes "
    "back to (POST /api/projects/{id}/source parity)",
    "project unbind": "remove a project's content source (POST /api/projects/{id}/source/unbind "
    "parity)",
    "project automation show": "the project's content source, schedule and last run "
    "(GET /api/projects/{id}/automation parity)",
    "project automation set": "change the project's schedule (UTC), mode, caps and pause rule "
    "(PUT /api/projects/{id}/automation parity)",
    "project automation pause": "pause the project's schedule "
    "(POST /api/projects/{id}/automation/pause parity)",
    "project automation resume": "resume the project's schedule "
    "(POST /api/projects/{id}/automation/resume parity)",
    "project automation handover": "hand claimed items over to / take them from another project "
    "(POST /api/projects/{id}/automation/handover parity)",
    "project run": "run the project's content source now (POST /api/projects/{id}/runs parity)",
    "project runs": "the project's recent automation runs (GET /api/projects/{id}/runs parity)",
    # --- (i) Documents: upload / translate / export. -------------------------
    "doc upload": "ingest a file into a project as a document",
    "doc list": "list a project's documents",
    "doc show": "per-locale segment + translated counts",
    "doc translate": "MT pre-translate a document SYNCHRONOUSLY in-process",
    "doc export": "rebuild the format-preserving translated file",
    "doc empties": "list a document's untranslated (empty) targets that the delivery "
    "empty gate is holding back from delivery (parity with GET /documents/{id}/empties "
    "+ GET /api/documents/{id}/empties)",
    "doc deliver-approve": "approve delivering a held (doc, locale)'s empty targets as "
    "empty strings, audited; refused while a run is in flight over the (doc, locale) "
    "(parity with POST /documents/{id}/deliver-approve + "
    "POST /api/documents/{id}/deliver-approve; Batch 15 T2 delivery empty gate)",
    "doc deliver-revoke": "revoke a prior empty-delivery approval for a (doc, locale) -> "
    "re-held (parity with POST /documents/{id}/deliver-revoke + "
    "POST /api/documents/{id}/deliver-revoke)",
    "doc paragraphs": "FULL_TEXT per-paragraph view with the inline-tag delta "
    "(web/CLI parity with GET /documents/{id}/paragraphs and "
    "GET /api/documents/{id}/paragraphs) - issue #42",
    "doc revert-paragraph": "recover a quarantined FULL_TEXT document by replacing one "
    "damaged paragraph with its SOURCE text (audited; parity "
    "with POST /documents/{id}/paragraphs/{i}/revert)",
    "doc set-paragraph": "hand-repair one FULL_TEXT paragraph's translation (parity "
    "with POST /documents/{id}/paragraphs/{i}/save)",
    "doc apply-tm": "fill 100%% TM matches into a document (web/CLI parity with "
    "POST /documents/{id}/apply-tm -> content.apply_tm_to_document)",
    "doc unlock-segment": "clear a TM lock on one segment, audited (PROPOSAL-tm-matching "
    "item 5b; parity with POST /documents/segments/{id}/unlock -> "
    "tm_lock.unlock_segment)",
    "doc approve-segment": "approve a reviewed segment and save its pair to TM, audited "
    "(chip task_9c665111; parity with POST /segments/{id}/approve "
    "+ POST /api/segments/{id}/approve -> "
    "segment_review.approve_and_write_tm)",
    "doc segment-propagation": "unlink/relink a segment from repeat propagation or undo its "
    "last propagation, audited (Issue #617 item 6; parity with POST "
    "/segments/{id}/propagation + POST /api/segments/{id}/propagation -> "
    "segment_review.set_propagation / undo_propagation)",
    "doc rename": "rename a document's filename - storage is id-keyed, blob unaffected "
    "(SPEC-rename-delete; POST /documents/{id}/rename parity)",
    "doc delete": "delete a document + blob + segments; project TM untouched "
    "(SPEC-rename-delete; POST /documents/{id}/delete parity)",
    # --- Reviewer-authored LQA findings (SPEC-wave3-collaboration §6.5). A CORE surface
    #     (no feature flag, §5.3), so its leaves live here, not in a module's MODULE_CLI.
    #     Manages FINDINGS only; running a scoring is web-only (the QA-2 PARTIAL gap, §9).
    "lqa log": "log one reviewer MQM finding on a segment (--document N --segment M "
    "--dimension D --severity S --note T); reviewer+ on /api "
    "(POST /api/documents/{id}/lqa/findings parity). Running a scoring is web-only",
    "lqa findings": "list reviewer-authored findings for a document, optionally one locale "
    "(GET /api/documents/{id}/lqa/findings parity)",
    "lqa edit": "edit a reviewer finding's dimension/severity/note; own-row or manager+ "
    "(POST /api/lqa/findings/{fid} parity)",
    "lqa withdraw": "soft-withdraw a reviewer finding (stamps withdrawn_at, never deletes; "
    "own-row or manager+; POST /api/lqa/findings/{fid}/withdraw parity)",
    # --- (h) Inline-tag integrity (AI Tagging Phase 1). -----------------------
    # Batch re-verify of inline-formatting tag integrity over existing targets; flags
    # damaged segments for review (never auto-repairs). The web surface for the results
    # is the existing needs-review queue / editor tag_integrity chips (no dedicated
    # route), mirroring how the fresh-MT gate quarantines at commit_mt_output.
    "tagging verify": "scan stored targets for broken inline-formatting tokens; --apply "
    "flags damaged segments NEEDS_REVIEW (needs-review queue parity)",
    # AI Tagging 1.5c. Web twin = POST /segments/{id}/tag-repair (the editor's repair
    # action on a tag_integrity-flagged segment); both surfaces are thin callers of
    # tag_llm_repair.repair_stored_segments, so they cannot drift.
    "tagging repair": "AI-repair broken inline-formatting tokens on damaged segments; "
    "--apply persists (web: POST /segments/{id}/tag-repair)",
    # --- (i) Translation memory. ----------------------------------------------
    "tm import": "import a TMX into the project segment TM",
    "tm export": "export the project segment TM to TMX",
    # TM management module: browse / usage / lifecycle over segment TM rows
    # (/tm page + /api/tm read+delete parity).
    "tm list": "browse TM entries paginated + filterable (GET /api/tm parity)",
    "tm search": "substring search across TM source/target text (GET /api/tm?q= parity)",
    "tm show": "show one TM entry + usage stats (GET /api/tm/{id} parity)",
    "tm edit": "edit a TM entry's target text in place - source is the match key, runs "
    "the refusal guard (POST /api/tm/{id}/edit parity)",
    "tm stats": "TM summary: totals, per-pair, usage split (GET /api/tm/stats parity)",
    "tm delete": "delete TM entries — one id or bulk-by-filter with --dry-run "
    "(POST /api/tm/{id}/delete + /api/tm/bulk-delete parity)",
    "tm scrub-hedges": "scan stored TM for refusal hedges; --apply removes them "
    "(POST /api/tm/scrub-hedges parity)",
    # --- (i) Glossary. -------------------------------------------------------
    "glossary import": "import a glossary CSV",
    "glossary add": "add/update one glossary term",
    "glossary list": "list a project's glossary entries",
    "glossary export": "export a project's glossary to CSV incl. TBX-ish meta columns "
    "(GET /projects/{id}/glossary/export parity)",
    # Dedicated glossary module (Phase 5-B): first-class glossaries with an approval
    # lifecycle + language-pair association + the workflow-step dropdown.
    "glossary module create": "create a draft module glossary",
    "glossary module list": "list module glossaries",
    "glossary module show": "show one module glossary (status, pairs, terms)",
    "glossary module approve": "approve a module glossary",
    "glossary module unapprove": "revert a module glossary to draft",
    "glossary module set-pairs": "set a glossary's language pairs",
    "glossary module set-content-type": "set/clear a glossary's content type "
    "(SPEC-content-types phase 2)",
    "glossary module add-term": "add a term to a module glossary",
    "glossary module edit-term": "edit a term's target/DNT + TBX-ish metadata "
    "(term database; POST /glossaries/{id}/terms/{eid}/edit "
    "+ /api/glossaries/{gid}/terms/{eid}/edit parity)",
    "glossary module set-customer": "assign a legacy unassigned glossary to a customer, "
    "one-time from-NULL only (customer-first; POST "
    "/glossaries/{id}/assign-customer + "
    "/api/glossaries/{gid}/set-customer parity)",
    "glossary module import": "import a CSV into a module glossary",
    "glossary module export": "export a module glossary to CSV incl. TBX-ish meta "
    "columns, round-trips import (GET /glossaries/{id}/export "
    "+ /api/glossaries/{gid}/export parity)",
    "glossary module delete": "delete a module glossary",
    "glossary module rename": "rename a module glossary - does not revert approval "
    "(SPEC-rename-delete; POST /glossaries/{id}/rename + "
    "/api/glossaries/{gid}/rename parity)",
    # Term extraction + candidate review (feat/term-extraction, research Option C).
    "glossary module extract": "extract candidate terms from a document into the "
    "glossary's review queue - nothing auto-added (POST "
    "/glossaries/{id}/extract parity)",
    "glossary module candidates list": "list term candidates awaiting review, or by "
    "--status (glossary detail review grid parity)",
    "glossary module candidates accept": "accept a candidate into the glossary as a "
    "term (POST /glossaries/{id}/candidates/{cid}/"
    "accept parity)",
    "glossary module candidates drop": "drop a candidate; persists so re-extraction "
    "never re-proposes it (POST /glossaries/{id}/"
    "candidates/{cid}/drop parity)",
    # Concept/term rich store (SPEC-terminology-backend T1, D1/D2). The concept level
    # beneath the compat termbase; every leaf delegates to glossary_concepts (web/CLI
    # parity) under the POST /api/glossaries/{gid}/concepts[...] routes.
    "glossary module concepts list": "list a glossary's concepts (glossary-concept/v1)",
    "glossary module concepts show": "show one concept and its terms",
    "glossary module concepts add": "create a concept (optionally seeding terms)",
    "glossary module concepts edit": "edit a concept's fields",
    "glossary module concepts delete": "delete a concept and its terms (destructive)",
    "glossary module terms add": "add a term to a concept",
    "glossary module terms edit": "edit a concept term",
    "glossary module terms delete": "delete a concept term (destructive)",
    "glossary module terms set-audience": "bulk-set the audience of one or more terms",
    # --- Content types (per-customer domain scoping, SPEC-content-types). ----
    "content-type create": "create a content type (global or --customer)",
    "content-type list": "list content types (global + customer-scoped)",
    "content-type rename": "rename a content type",
    "content-type delete": "delete a content type (refused while referenced)",
    # --- Style guides (SPEC-workflows-redesign Track B, B2 — the flagship asset). --
    # --- MT formality (#617 item 7, ADR 0062): document > project > customer, per target
    #     language; GET/POST /api/formality + web POST /formality share services.formality. ---
    "formality registers": "list the formality registers (GET /api/formality/registers parity)",
    "formality show": "show a scope's formality entries and the register in force "
    "(GET /api/formality parity)",
    "formality set": "set the formality register for one target language "
    "(POST /api/formality parity; web POST /formality)",
    "formality clear": "clear one formality entry so the next level applies "
    "(POST /api/formality parity)",
    # --- Project notifications (project automation Part B): channels, routes, per-project view;
    #     /api/notify/* + web /settings/notifications share services.notify. ---
    "notify channel list": "list notification channels (GET /api/notify/channels parity)",
    "notify channel add": (
        "create a Slack, Teams, signed-webhook or email channel (POST "
        "/api/notify/channels parity; web /settings/notifications)"
    ),
    "notify channel update": (
        "rename, re-point, enable or disable a channel, rotate a webhook "
        "signing secret (POST /api/notify/channels/{id} parity)"
    ),
    "notify channel delete": (
        "delete a channel with its routes and delivery log (DELETE "
        "/api/notify/channels/{id} parity)"
    ),
    "notify channel test": "send a test message (POST /api/notify/channels/{id}/test parity)",
    "notify channel deliveries": (
        "show a channel's delivery log (GET /api/notify/channels/{id}/deliveries parity)"
    ),
    "notify channel retry": (
        "re-queue a channel's failed deliveries (POST /api/notify/channels/{id}/retry parity)"
    ),
    "notify route list": "list notification routes (GET /api/notify/routes parity)",
    "notify route set": (
        "attach a channel to a project, content type, project type, customer "
        "or the org (POST /api/notify/routes parity)"
    ),
    "notify route clear": "remove a route (DELETE /api/notify/routes/{rid} parity)",
    "notify project show": (
        "show a project's effective channels per event (GET /api/notify/projects/{id} parity)"
    ),
    "notify project mute": (
        "make a project post to no channel (POST /api/notify/projects/{id}/mute parity)"
    ),
    "notify project unmute": (
        "let a project post to its channels again (POST /api/notify/projects/{id}/mute parity)"
    ),
    "style-guide create": "create a draft style guide (org-global or scoped; "
    "POST /api/style-guides + web /style-guides parity)",
    "style-guide list": "list style guides (org-global + customer-scoped; "
    "GET /api/style-guides parity)",
    "style-guide show": "show one style guide (scope, status, full body; "
    "GET /api/style-guides/{id} parity)",
    "style-guide update": "update a guide's name/body/content-type/pair (body + scope "
    "edits revert approval; POST /api/style-guides/{id}/update parity)",
    "style-guide set-status": "set a guide's status draft|approved "
    "(POST /api/style-guides/{id}/set-status; web approve/unapprove parity)",
    "style-guide delete": "delete a style guide (POST /api/style-guides/{id}/delete parity)",
    "style-guide ab": "A/B measure whether a style guide improves quality (Track B, B3) — "
    "same segments with the guide ON vs OFF, both scored by the QE judge, "
    "QE + style-error delta + verdict (document page 'A/B test style "
    "guide' action -> POST /documents/{id}/style-ab parity)",
    # --- Language catalog + enabled-languages registry (P2, item 1). ---------
    "lang list": "list languages (enabled subset, or --all for the full catalog)",
    "lang enable": "enable catalog codes (offered in the pickers; /settings/languages parity)",
    "lang disable": "disable codes (removed from pickers; grandfathered projects unaffected)",
    # --- Language Sets (reusable target-locale profiles). --------------------
    "langset create": "create a Language Set",
    "langset list": "list Language Sets",
    "langset update": "update a Language Set (targets replace)",
    "langset delete": "delete a Language Set (destructive)",
    # --- (i)/(iv) Connectors: full CRUD + operate + token/OAuth adjuncts. -----
    "connector create": "create a connection (token-auth fully CLI-managed)",
    "connector list": "list connections",
    "connector show": "connection detail (auth STATE only, never secrets)",
    "connector update": "merge connection config / credentials",
    "connector delete": "delete a connection (destructive)",
    "connector prune-config": "prune stale content-hash entries a dropped target locale "
    "left in a connection's config (dry-run by default; --apply "
    "executes; residual sweep)",
    "connector test": "test credentials against the external system",
    "connector browse": "list containers / Airtable table fields",
    "connector rotate": "rotate credentials only after proving the NEW ones work",
    "connector schedule": "set/clear the automated-sync schedule",
    "connector pull": "pull source content into a Localization OS document",
    "connector push": "push translated segments back",
    "connector sync": "automated pull -> MT -> push, synchronously in-process",
    "connector retry": "re-run only the items that failed in a prior run",
    "connector jira-translate": "translate Jira issues into labelled comments/fields",
    "connector jira-count": "preview how many Jira issues a scoped JQL would touch, "
    "translating none (POST /api/connectors/{id}/jira/count parity)",
    "connector jira-translate-pending": "translate the Jira connection's pending "
    "issues (recorded by verified pushes while "
    "auto-translate-on-change is off; POST "
    "/api/connectors/{id}/jira/translate-pending parity)",
    "connector confluence-translate": "translate Confluence pages into localized child pages",
    "connector airtable-translate": "convention-driven Airtable translation",
    "connector airtable-webhook register": "register the official Airtable webhook "
    "(POST /api/connectors/{cid}/airtable/webhook/register parity)",
    "connector airtable-webhook unregister": "delete the official Airtable webhook "
    "(POST /api/connectors/{cid}/airtable/webhook/unregister parity)",
    "connector airtable-webhook status": "official Airtable webhook state "
    "(GET /api/connectors/{cid}/airtable/webhook parity)",
    "connector airtable-webhook translate-pending": "translate ping-recorded pending "
    "records (POST /api/connectors/{cid}/airtable/webhook/translate-pending parity)",
    "connector airtable-webhook health": "probe the official Airtable webhook health / "
    "watched-field-deleted error state (POST /api/connectors/{cid}/airtable/webhook/"
    "health parity)",
    "connector airtable-record create": "create new Airtable record(s) from a JSON "
    "field-map array (POST /api/connectors/{cid}/airtable/records parity)",
    "connector airtable-record delete": "delete Airtable record(s) by id "
    "(POST /api/connectors/{cid}/airtable/records/delete parity)",
    "connector zendesk-translate": "translate Zendesk Guide articles into per-locale "
    "translation objects (T2 SubResourceStrategy)",
    "connector document360-translate": "translate Document360 articles into per-language "
    "article versions (T2 SubResourceStrategy)",
    "connector strapi-translate": "translate Strapi collection-type entries into per-locale "
    "entry variants (T2 SubResourceStrategy; v5 documentId / v4 "
    "localization; POST /api/connectors/{cid}/strapi/translate parity)",
    "connector contentful-translate": "translate Contentful entries into new per-locale "
    "field keys on each entry (T1 field-map; fields.{id}."
    "{locale}, version-locked read-merge-write; POST "
    "/api/connectors/{cid}/contentful/translate parity)",
    "connector shopify-translate": "translate Shopify resources into registered per-locale "
    "translations (T2-flavored SubResourceStrategy variant; "
    "translationsRegister keyed by resource+key+locale, "
    "digest-gated; POST /api/connectors/{cid}/shopify/"
    "translate parity)",
    "connector hubspot-translate": "translate HubSpot site pages, landing pages and blog "
    "posts into HubSpot language variations (T3 with native "
    "linkage; translations dict + translatedFromId, draft by "
    "default and never published; POST /api/connectors/{cid}/"
    "hubspot/translate parity)",
    "connector freshdesk-translate": "translate Freshdesk Solutions articles into per-language "
    "article versions (T2 SubResourceStrategy)",
    "connector zoho-translate": "translate Zoho Desk articles into per-locale translations "
    "(T2 SubResourceStrategy)",
    "connector intercom-translate": "translate Intercom Help Center articles into the "
    "article's per-locale translated_content map (T1 "
    "FieldMapStrategy; POST /api/connectors/{cid}/intercom/translate "
    "parity)",
    "connector salesforce-translate": "translate Salesforce Knowledge articles into "
    "per-language translation versions (T2 "
    "SubResourceStrategy; POST /api/connectors/{cid}/"
    "salesforce/translate parity)",
    "connector salesforce-rediscover-fields": "Describe Knowledge__kav + persist the "
    "per-record-type translatable field map "
    "(POST /api/connectors/{cid}/salesforce/"
    "rediscover-fields parity)",
    "connector notion-translate": "translate Notion pages into per-locale child pages "
    "(T3 DocPerLocaleStrategy; POST /api/connectors/{cid}/"
    "notion/translate parity)",
    "connector notion-resolve-link": "adopt/ignore a Notion needs_link id-map row (a "
    "pre-existing target the map never made; SPEC-t3 "
    "rule 6; POST /api/connectors/{cid}/notion/"
    "resolve-link parity)",
    "connector helpscout-translate": "translate Help Scout Docs articles into per-locale "
    "articles in mapped collections (T3 DocPerLocaleStrategy; "
    "POST /api/connectors/{cid}/helpscout/translate parity)",
    "connector helpscout-resolve-link": "adopt/ignore a Help Scout needs_link id-map row "
    "(a pre-existing target the map never made; SPEC-t3 "
    "rule 6; POST /api/connectors/{cid}/helpscout/"
    "resolve-link parity)",
    "connector wordpress-translate": "translate WordPress posts/pages into per-locale posts "
    "in the target language (T3 DocPerLocaleStrategy via "
    "Polylang; raw block content preserved; POST "
    "/api/connectors/{cid}/wordpress/translate parity)",
    "connector wordpress-resolve-link": "adopt/ignore a WordPress needs_link id-map row (a "
    "pre-existing Polylang-linked target the map never "
    "made; SPEC-t3 rule 6; POST /api/connectors/{cid}/"
    "wordpress/resolve-link parity)",
    "connector akeneo-translate": "translate Akeneo product and product-model attribute values into per-locale values on the same record (T1 partial-PATCH merge; POST /api/connectors/{cid}/akeneo/translate parity)",  # noqa: E501
    "connector mcp_cms-translate": "translate CMS items through the CMS's own MCP server into per-locale item variants (T2 SubResourceStrategy over mapped MCP tools; POST /api/connectors/{cid}/mcp_cms/translate parity)",  # noqa: E501
    "connector gdocs-translate": "translate Google Docs into new Google Docs in per-locale Drive folders (T3 document per locale; POST /api/connectors/{cid}/gdocs/translate parity)",  # noqa: E501
    "connector servicenow-translate": "translate ServiceNow Knowledge articles into one Draft article per language linked by Parent (T3; POST /api/connectors/{cid}/servicenow/translate parity)",  # noqa: E501
    "connector servicenow-resolve-link": "adopt/ignore a ServiceNow needs_link id-map row (a translation that already exists under the source's Parent but that the map never created; "  # noqa: E501
    "POST /api/connectors/{cid}/servicenow/resolve-link parity)",
    "connector msgraph-translate": "translate SharePoint or OneDrive files into translated copies in per-locale folders (T3 document per locale; POST /api/connectors/{cid}/msgraph/translate parity)",  # noqa: E501
    "connector plytix-translate": "translate Plytix product text attributes into mapped per-language attributes (T1 attribute map; POST /api/connectors/{cid}/plytix/translate parity)",  # noqa: E501
    "connector drupal-translate": "translate Drupal entities into per-language entity translations (T2 SubResourceStrategy over JSON:API Multilingual; POST /api/connectors/{cid}/drupal/translate parity)",  # noqa: E501
    "connector github-translate": "localize GitHub resource files into per-locale files committed on a working branch with one pull request (T3 over the shared git-provider core; POST /api/connectors/{cid}/github/translate parity)",  # noqa: E501
    "connector sanity-translate": "translate Sanity documents into per-language items or linked per-language documents (draft only; POST /api/connectors/{cid}/sanity/translate parity)",  # noqa: E501
    "connector sitecore-translate": "translate Sitecore XM Cloud content items into language versions of the same item (T2 SubResourceStrategy; Authoring GraphQL addItemVersion + updateItem; POST /api/connectors/{cid}/sitecore/translate parity)",  # noqa: E501
    "connector bitbucket-translate": "localize Bitbucket resource files into per-locale files committed on a working branch with one pull request (T3 over the shared git-provider core; POST /api/connectors/{cid}/bitbucket/translate parity)",  # noqa: E501
    "connector gitlab-translate": "localize GitLab resource files into per-locale files committed on a working branch with one merge request (T3 over the shared git-provider core; POST /api/connectors/{cid}/gitlab/translate parity)",  # noqa: E501
    # factory:anchor cli_parity.expected  (scripts/factory inserts new connector entries above this line)  # noqa: E501
    "connector token-regen": "regenerate the shared connector API token (lsk_)",
    "connector oauth-status": "report OAuth provider-app configured + connection auth mode",
    "connector set-oauth-secret": "inject provider-minted OAuth tokens (CI adjunct)",
    # --- Engine registry (MT/LLM EngineProfile CRUD + test) (P3, item 6). -----
    "engine create": "create an MT/LLM engine profile (Setup AI 'Add engine' parity)",
    "engine list": "list engine profiles (key STATE only, never the key value)",
    "engine show": "one engine profile's detail (config + key state, never the key)",
    "engine update": "update an engine profile (config merged; secret rotated if given)",
    "engine delete": "delete an engine profile (destructive)",
    "engine models": "list an engine's models with its stored key (Setup AI Fetch models parity)",
    "engine test": "per-kind reachability/auth probe (Setup AI per-row Test parity)",
    # Jev Train C: the quality-judge Inspector (Setup AI Inspect page + env import parity).
    "engine inspect": "a judge engine's criteria, request template and rule (Inspect page)",
    "engine jev-try": "score one segment through a judge engine (Inspect page Try it)",
    "engine import-jev-env": "create a hosted Jev engine from TYPESAFE_API_KEY (Setup AI)",
    # --- Provider model discovery (workflow-builder dropdown parity). ----------
    "providers models": "list a provider ref's models (workflow-builder model "
    "dropdown + Setup AI cloud/local fetch parity)",
    "providers key set": "set/replace an in-app cloud API key, encrypted (Setup AI "
    "cloud-tile Save-key parity)",
    "providers key show": "show each cloud key's source + last4 fingerprint (never the key value)",
    "providers key clear": "remove an app-stored cloud key, falling back to .env "
    "(Setup AI cloud-tile Remove parity)",
    # --- Segmentation profiles (named SRX rulesets, P6, item 8). --------------
    "segprofile list": "list segmentation profiles (/settings segprofiles parity)",
    "segprofile show": "one profile's metadata + in-use counts (blob never echoes)",
    "segprofile create": "create a profile (built-in rules or --file custom SRX)",
    "segprofile clone": "clone-before-edit (rulesets are immutable)",
    "segprofile rename": "rename a profile (/settings segprofiles rename parity)",
    "segprofile delete": "delete a profile (refused while referenced)",
    "segprofile set-default": "mark/clear the single system-default profile",
    "segprofile assign": "set/clear a project's segmentation override",
    # --- Context packages + standing cards (SPEC-context-metadata phase 1). ----
    "context show": "resolve + render the effective context package for a scope, with "
    "per-field provenance (customer page / settings card + /api/translate/text "
    "resolution parity)",
    "context set-profile": "create/update a standing context card at a scope "
    "(customer/content-type/project/org default; customer page "
    "'Context cards' + settings org-default card parity)",
    "context delete-profile": "delete a context card by id (customer page / settings "
    "card delete parity)",
    "context set-document": "set/edit a document's context override (Document.context), "
    "or clear it (document page 'Edit document-level context "
    "override' form POST /documents/{id}/context parity)",
    "context show-document": "show a document's stored context override + the effective "
    "resolved package (document page effective card parity)",
    "context ab": "A/B measure whether context improves quality — same segments with "
    "context ON vs OFF, both scored by the QE judge, delta + verdict "
    "(document page 'A/B test context' action -> POST "
    "/documents/{id}/context-ab parity)",
    # --- Project templates (reusable setup snapshots, P7, item 9). ------------
    "template create": "snapshot a project's setup ('Save as template' parity)",
    "template list": "list templates (global + a customer's own; --all admin view)",
    "template show": "one template's captured setup",
    "template apply": "create a project from a template (auto-fill parity)",
    "template rename": "rename a template (web route owned by the projects surface)",
    "template delete": "delete a template (snapshots only, nothing references them)",
    # --- (i) Users / access: role/dept/toggle extraction + headless pw reset. -
    "user create": "create a user",
    "user list": "list users",
    "user show": "one user's detail + their API keys",
    "user role": "set a user's role (extracted security.set_role)",
    "user department": "set a user's department (extracted security.set_department)",
    "user customer": "bind/clear a user's customer (R2-3 customer-account isolation)",
    "user enable": "re-activate a user (extracted security.set_active)",
    "user disable": "deactivate a user (extracted; last-admin guard; destructive)",
    "user reset-password": "HEADLESS admin password reset (added security.admin_set_password)",
    "user unlock": "Host break-glass when the only admin is locked out of password sign-in.",
    # TOTP train: two-step sign-in admin parity (web Users page column + Reset, /api
    # GET/DELETE /api/users/{uid}/mfa); mfa-reset is also the host break-glass.
    "user mfa-status": "a user's two-step sign-in state (Users page column parity)",
    "user mfa-reset": "reset a user's two-step sign-in + end their sessions (Users page Reset)",
    "user org": "move a user to another org (multi-org P7; back-office move parity)",
    "user invite": "create an invite token",
    "user invites": "list live invitation metadata without tokens",
    "user invite-revoke": "revoke a pending or delete an expired invitation (soft)",
    "user export": "GDPR subject-access export (NDJSON, no secrets)",
    "user erase": "GDPR erasure / anonymize (destructive, irreversible)",
    # --- Self-service account view. --------------------
    "account show": "show an account's profile + onboarding flag "
    "(GET /api/account parity; mirrors the web /account page)",
    # --- Appearance (ADR 0063): stored per account; web /account/theme (Account settings
    #     and the account menu) + POST /api/account/theme share security.UI_THEMES. ---
    "account set-theme": "set the account's appearance: light, dark or system "
    "(POST /api/account/theme parity; self-service remotely, --user local-only)",
    # --- Onboarding-flag state parity (SPEC-onboarding-tour Phase 3). The tour
    #     checklist UX is web-only; this is the headless read/reset of the flag. ---
    "account reset-onboarding": "clear onboarding_dismissed_at so the account is "
    "tour-eligible again (POST /api/account/onboarding-reset "
    "parity; self-service remotely, --user local-only)",
    # --- Editable page layouts (train L1): a person's OWN saved card layout per page
    #     type. Web /account/layouts + /api/account/layouts + these leaves share
    #     services.page_layouts (validation + owner rules). -----------------------
    "layout list": "list your saved page layouts, saved or default per page "
    "(GET /api/account/layouts parity)",
    "layout show": "show one page's layout rows with card names "
    "(GET /api/account/layouts/{page_key} parity)",
    "layout set": "save a page layout from JSON rows of 1-3 cards (web PUT "
    "/account/layouts/{page_key} + PUT /api/account/layouts/{page_key} parity)",
    "layout reset": "reset a page layout to the default (web + /api DELETE parity; "
    "self-service remotely, --user local-only)",
    # --- Global search (#595): projects, document file names, TM and glossary terms,
    #     scoped to what the caller may open. /search + /search/suggest + GET /api/search +
    #     this leaf + the MCP `search` tool share services.global_search. -------------
    "search": "search projects, documents, TM and glossary terms (GET /api/search parity)",
    # --- MCP fourth-surface connection help (wave 4). A PURE FORMATTER: no /api
    #     route, no DB - it prints the remote streamable-http connection snippet,
    #     mirroring the web /help "Connect an AI agent" section (help.html). ------
    "mcp connect": "print the remote MCP (streamable-http) connection snippet for a "
    "base URL - the claude mcp add + header form (mirrors the /help "
    "'Connect an AI agent' section; no /api route, pure formatter)",
    "mcp client-limit": "print the LOC_STUDIO_MCP_CLIENT_LIMITS entry (#410) giving one API "
    "key its own MCP session-admission budget (listener environment, not an app setting; "
    "no /api route, pure formatter; mirrors the /help 'Connect an AI agent' section)",
    # --- #753 MCP OAuth authorization server (host-only; web /security/mcp-oauth). ---
    "mcp-oauth status": "MCP OAuth authorization server state and what is missing (GET "
    "/security/mcp-oauth parity; host-only, the switches are host-only settings)",
    "mcp-oauth rs-secret": "mint the MCP resource-server secret, shown once, hash stored "
    "(POST /security/mcp-oauth/rs-secret parity; host-only)",
    "mcp-oauth grants": "list consented MCP OAuth grants, metadata only (GET "
    "/account/connected-apps + GET /api/mcp-oauth/grants parity; host-only operator view)",
    "mcp-oauth revoke": "revoke one MCP OAuth grant (POST /account/connected-apps/{gid}/revoke "
    "+ POST /api/mcp-oauth/grants/{gid}/revoke parity; host-only operator view)",
    # --- Per-user API keys. --------------------------------------------------
    "apikey issue": "mint a per-user lsu_ key (printed once)",
    "apikey list": "list API keys (never the value)",
    "apikey revoke": "revoke one key by id",
    "apikey revoke-all": "revoke every key a user holds (destructive)",
    # --- Project membership. -------------------------------------------------
    "member add": "grant a user visibility into a project",
    "member remove": "revoke a membership (destructive)",
    "member list": "list a project's members",
    # --- (iii) Synchronous job run + the job surface. ------------------------
    "job run": "run a job kind SYNCHRONOUSLY (jobs.run_sync, never submit-and-exit)",
    "job status": "show one job's status/progress/result",
    "job list": "list recent jobs (filter by kind/status)",
    # --- (i) Workflow CRUD + prompt-template + clone + run/resume (extraction). -
    "workflow create": "create a workflow (extracted services.workflows)",
    "workflow list": "list workflows",
    "workflow show": "show a workflow and its ordered steps",
    "workflow add-step": "append a step (extracted add-step)",
    "workflow edit-step": "edit an existing step in place (partial update; whole-graph "
    "re-validation, fail-closed)",
    "workflow delete-step": "delete a step (extracted delete-step)",
    "workflow move-step": "reorder a step (--to/--before/--after; the reorder seam, "
    "POST /workflows/{id}/steps/{sid}/move parity)",
    "workflow clone": "deep-copy a workflow and its steps (extracted clone)",
    "workflow author-prompt": "AI-draft a step prompt (prompt-template)",
    "workflow author": "AI-draft a whole workflow (name + language pair(s) + ordered "
    "steps) from a plain-language description, review + confirm "
    "(SPEC-promptable-setup; POST /workflows/author + "
    "/workflows/author/confirm parity)",
    "workflow run": "run a workflow over a document/locale SYNCHRONOUSLY",
    "workflow show-run": "show a run's status + a paused human-review run's review "
    "text with explicit segment boundaries (GET /api/runs/{id} "
    "parity; mirrors the /runs/{id} run-view page)",
    "workflow step-diff": "what a step CHANGED, segment by segment, word-level "
    "(GitHub #340 'Highlight differences': the run's newest translating output "
    "vs its previous translating step, or any two StepRun ids via --from/--to; "
    "read-only split of the recorded StepRun text - no provider call; "
    "GET /api/runs/{id}/step-diff parity; mirrors the /runs/{id}/step-diff page)",
    "workflow resume": "resume a paused human-review run SYNCHRONOUSLY (safe re-pause "
    "on segment-count mismatch; --edited-segments structured input)",
    "workflow cancel-run": "cancel an in-flight run (pending/running/paused -> cancelled) "
    "- the escape from a run stuck paused at human review / an "
    "external vendor step, and the cancel that unblocks mid-flight "
    "re-authoring (web POST /runs/{id}/cancel; /api POST "
    "/api/runs/{run_id}/cancel)",
    # --- Fan-out batch monitor + actions (SPEC-workflows-redesign Phase 5b). ---
    "workflow batch show": "show a batch's locale x steps matrix (ASCII; --json = the "
    "detail poll shape; GET /api/batches/{id}?detail=steps parity; "
    "mirrors the /batches/{id} monitor page)",
    "workflow batch rerun": "re-run ONE locale of a batch as a fresh member run into the "
    "same batch (web POST /batches/{id}/rerun; /api POST "
    "/api/batches/{id}/rerun)",
    "workflow batch retry-failed": "re-run EVERY failed locale of a batch as one action "
    "(web POST /batches/{id}/retry-failed; /api POST "
    "/api/batches/{id}/retry-failed)",
    # --- Multilingual workflow groups (P5, item 4). --------------------------
    "workflow group create": "create a multilingual workflow group (global/customer)",
    "workflow group list": "list workflow groups + their variant counts",
    "workflow group show": "show a group and its per-language-pair variants",
    "workflow group add": "add an existing workflow to a group as a pair variant "
    "(--default marks the group fallback)",
    "workflow group set-default": "make an existing group member the group's fallback "
    "default variant (web POST /workflow-groups/{id}/default "
    "parity)",
    "workflow copy": "copy a workflow to other language pair(s), optionally "
    "LLM-adapting each step's instruction (post-edit required)",
    # --- SPEC-rename-delete Branch 1: rename + delete across web + CLI + /api. -
    "workflow rename": "rename a workflow (web POST /workflows/{id}/rename; "
    "/api POST /api/workflows/{wid}/rename)",
    "workflow delete": "delete a workflow, cascading its steps + run history, "
    "fail-closed on external refs / in-flight runs (web POST "
    "/workflows/{id}/delete; /api POST /api/workflows/{wid}/delete)",
    "workflow archive": "archive (retire) a workflow - drops it from the default list + "
    "pickers + dispatch and blocks new runs, steps + history preserved "
    "+ reversible (residual sweep; POST /api/workflows/{wid}/archive parity)",
    "workflow unarchive": "unarchive (reactivate) a workflow - list it + make it "
    "dispatchable/runnable again (residual sweep; POST "
    "/api/workflows/{wid}/unarchive parity)",
    "workflow group rename": "rename a workflow group (web POST "
    "/workflow-groups/{id}/rename; /api parity)",
    "workflow group delete": "delete an empty workflow group, fail-closed on members "
    "(web POST /workflow-groups/{id}/delete; /api parity)",
    "workflow prompt create": "create a reusable prompt template, global or attached to "
    "a customer via --customer (SPEC-pt-attach; web POST "
    "/templates/prompts parity)",
    "workflow prompt list": "list reusable prompt templates (web /workflows/building-blocks)",
    "workflow prompt system": "show the built-in pipeline system prompts, read-only (web "
    "/workflows/building-blocks System prompts section; GET /api/system-prompts)",
    "workflow prompt system-set": (
        "save a new version of an editable system-prompt head, admin only, flag-gated (web POST "
        "/workflows/building-blocks/system-prompts/{key}/save; PUT /api/system-prompts/{key})"
    ),
    "workflow prompt system-reset": (
        "reset an editable system-prompt head to the built-in text (web POST "
        ".../system-prompts/{key}/reset; DELETE /api/system-prompts/{key})"
    ),
    "workflow prompt system-history": (
        "version history of an editable system-prompt head (web Building blocks System prompts; "
        "GET /api/system-prompts/{key})"
    ),
    "workflow prompt system-restore": (
        "save a copy of an older system-prompt head version as the newest (web POST "
        ".../system-prompts/{key}/restore; POST /api/system-prompts/{key}/restore)"
    ),
    "workflow prompt rename": "rename a prompt template (web POST "
    "/templates/prompts/{id}/rename; /api parity)",
    "workflow prompt delete": "delete a prompt template, fail-closed while a step "
    "pins it (web POST /templates/prompts/{id}/delete; /api parity)",
    # --- (iii)/(i) Admin intelligence surface: semantic / srx / retention /
    #     ldap / oidc / audit / gdpr + routing-rule & budget CRUD (extraction) +
    #     lqa rulebook. ----------------------------------------------------------
    "admin srx enable": "enable SRX segmentation",
    "admin srx disable": "disable SRX segmentation",
    "admin srx status": "show the segmentation policy",
    "admin srx validate": "parse-validate an .srx file (writes nothing)",
    "admin srx upload": "validate then install a custom SRX ruleset",
    "admin retention config": "set the data-retention policy (never purges)",
    "admin retention status": "show the retention policy",
    "admin retention purge-now": "run one retention sweep NOW (destructive)",
    "admin ldap config": "set LDAP settings (bind password stored encrypted)",
    "admin ldap status": "show LDAP settings (bind password masked)",
    "admin ldap test-login": "attempt a real LDAP login",
    "admin oidc config": "set OIDC settings (client secret masked)",
    "admin oidc status": "show OIDC settings (client secret masked)",
    "admin audit export": "stream the audit chain as NDJSON (SIEM export)",
    "admin audit verify": "verify the keyed audit hash chain",
    "admin gdpr export": "GDPR subject-access export (NDJSON)",
    "admin gdpr erase": "GDPR erasure / anonymize (destructive)",
    "admin routing rule add": "create a routing rule (extracted routing.create_rule)",
    "admin routing rule list": "list routing rules in priority order",
    "admin routing rule show": "read back one routing rule (/api + --remote parity)",
    "admin routing rule toggle": "flip a routing rule active/inactive (extracted)",
    "admin routing rule delete": "delete a routing rule (extracted routing.delete_rule)",
    "admin budget set": "create/update a department token cap (extracted budgets.upsert)",
    "admin budget list": "list budgets + this month's spend",
    "admin budget delete": "delete a budget (extracted budgets.delete_budget)",
    "admin budget project set": "create/update a project USD cap (CV-2; project_budgets.upsert_project_budget; web /routing/project-budgets, /api/projects/{id}/budget)",  # noqa: E501
    "admin budget project list": "list project budgets + this month's spend (CV-2)",
    "admin budget project delete": "delete a project budget (CV-2; project_budgets.delete_project_budget)",  # noqa: E501
    "admin lqa rulebook get": "read a language-pair LQA rulebook",
    "admin lqa rulebook set": "install a language-pair LQA rulebook",
    "admin tick": "run one scheduler pass NOW (piggybacked sweeps + due connector "
    "schedules) - the external-cron entry for embedded/headless installs "
    "(POST /api/admin/tick parity; --remote-capable)",
    # --- (P7) Organization management: cross-org operator CRUD + suspend. The web
    #     parity is the BACK-OFFICE org CRUD (super-admin only, /back-office mount).
    "org create": "create an organization (back-office org-create parity)",
    "org list": "list organizations (back-office org list parity)",
    "org rename": "rename an organization (back-office org-rename parity)",
    "org show": "show one org (name, status, counts)",
    "org suspend": "suspend an org - blocks login/API (back-office suspend parity)",
    "org unsuspend": "re-activate a suspended org (back-office unsuspend parity)",
    "org delete": "delete an EMPTY org, fail-closed (back-office org-delete parity)",
    # --- Batch 13 data-export: the `export run/list/status` rows were a Train-A _CORE_EXPECTED
    #     placeholder; Train B RELOCATED them (plus the new `export download` leaf) into the
    #     data_export module's MODULE_CLI (cli_expected()), so they are removed here (TA-05:
    #     _CORE_EXPECTED and cli_expected() must be DISJOINT; the merge would otherwise
    #     double-count silently).
}

# The module-owned CLI leaf rows RELOCATED to app/modules.py's manifest (M3): their command
# paths + descriptions now live in MODULE_CLI there, and verify_modules.py pins that
# cli_expected() reproduces the pre-relocation module rows byte-for-byte. Merging them here
# keeps EXPECTED_COMMANDS's (a)/(b) accounting unchanged -- only the SOURCE of the module
# rows moved; the CORE rows above stay hand-authored.
EXPECTED_COMMANDS = {**_CORE_EXPECTED, **cli_expected()}


# ============================================================================
# INHERENTLY-INTERACTIVE EXCEPTIONS.
#
# Part 1 (iv): connector OAuth *authorize* is a browser consent round-trip
# (Airtable / Atlassian have no device flow for these apps), so it CANNOT be a
# headless command. For each such capability we require that (1) it is DOCUMENTED
# as an exception in docs/CLI.md, and (2) the CLI adjuncts that DO cover the
# headless slice of it are present in the parser.
# ============================================================================
INTERACTIVE_EXCEPTIONS = {
    "connector oauth authorize": {
        # A substring that must appear in docs/CLI.md proving it's documented as
        # a deliberate, explained exception (not a forgotten gap).
        "doc_marker": "OAuth authorize stays interactive",
        # Headless adjuncts that MUST exist for the CI/token slice of OAuth.
        "adjunct_commands": ["connector oauth-status", "connector set-oauth-secret"],
    },
}


# ----------------------------------------------------------------------------
# Parser introspection: collect every leaf command path from the real parser.
# ----------------------------------------------------------------------------
def _leaf_paths(parser, prefix=()):
    """Yield the space-joined leaf command paths of an argparse parser tree.

    A 'leaf' is a (sub)parser with no further subparsers action - i.e. a real,
    dispatchable command. Groups that only hold subcommands are not leaves.
    """
    subparser_actions = [a for a in parser._actions if a.__class__.__name__ == "_SubParsersAction"]
    if not subparser_actions:
        yield " ".join(prefix)
        return
    for action in subparser_actions:
        for name, sub in action.choices.items():
            yield from _leaf_paths(sub, prefix + (name,))  # noqa: RUF005


def _actual_leaf_paths():
    parser = build_parser()
    return {p for p in _leaf_paths(parser) if p}


# ----------------------------------------------------------------------------
# Check (a): every EXPECTED command path actually exists in the parser.
# A renamed/removed command -> the promised capability has no CLI path -> FAIL.
# A bogus EXPECTED entry (path that never existed) -> also FAIL (table can't lie).
# ----------------------------------------------------------------------------
def test_every_expected_capability_has_a_command():
    actual = _actual_leaf_paths()
    missing = sorted(cmd for cmd in EXPECTED_COMMANDS if cmd not in actual)
    assert not missing, (
        "Capabilities in the master-plan inventory with NO resolving CLI command "
        "(removed/renamed, or a bogus expected entry):\n  " + "\n  ".join(missing)
    )
    print(
        "  every one of %d inventory capabilities resolves to a real CLI command OK"  # noqa: UP031
        % len(EXPECTED_COMMANDS)
    )


# ----------------------------------------------------------------------------
# Check (b): every shipped leaf command is registered in EXPECTED_COMMANDS.
# A new command with no entry -> FAIL, forcing the parity table to stay complete
# (this is what enforces "add your CLI command + a parity entry" for new features).
# ----------------------------------------------------------------------------
def test_no_command_ships_without_a_capability_entry():
    actual = _actual_leaf_paths()
    unregistered = sorted(cmd for cmd in actual if cmd not in EXPECTED_COMMANDS)
    assert not unregistered, (
        "CLI leaf commands that ship WITHOUT an EXPECTED_COMMANDS parity entry "
        "(add each here + its web route so parity is enforced):\n  " + "\n  ".join(unregistered)
    )
    # TA-05: _CORE_EXPECTED (hand-authored) and cli_expected() (module-owned MODULE_CLI) must be
    # DISJOINT. A leaf in BOTH (a Train-A CORE placeholder a later train ALSO adds to MODULE_CLI
    # without removing the placeholder) would silently double-count -- the dict merge hides it and
    # the accounting still passes. This asserts the split so the omission fails loudly.
    both = sorted(set(_CORE_EXPECTED) & set(cli_expected()))
    assert not both, (
        "CLI leaves double-classified in BOTH _CORE_EXPECTED and MODULE_CLI (cli_expected); "
        "remove the core placeholder when the module row is added:\n  " + "\n  ".join(both)
    )
    print("  all %d shipped CLI leaf commands are registered in the parity table OK" % len(actual))  # noqa: UP031


# ----------------------------------------------------------------------------
# Self-check: prove the harness is REAL by construction - a bogus expected path
# must be caught by the same membership test check (a) uses. If this ever passes
# a non-existent command, check (a) would be meaningless.
# ----------------------------------------------------------------------------
def test_harness_rejects_a_bogus_command_path():
    actual = _actual_leaf_paths()
    assert "connector definitely-not-a-real-command" not in actual, (
        "sanity: a fabricated path must not resolve"
    )
    # And the real dispatch path this stands in for DOES exist, so the check is
    # discriminating (not trivially always-true).
    assert "connector sync" in actual, "expected a known-good command to resolve"
    print("  harness is discriminating: bogus path rejected, real path accepted OK")


# ----------------------------------------------------------------------------
# Interactive exceptions: documented + their headless adjuncts exist.
# ----------------------------------------------------------------------------
def test_interactive_exceptions_are_documented_with_adjuncts():
    actual = _actual_leaf_paths()
    cli_doc = os.path.join(_ROOT, "docs", "CLI.md")
    with open(cli_doc, encoding="utf-8") as fh:
        doc_text = fh.read()

    for cap, spec in INTERACTIVE_EXCEPTIONS.items():
        marker = spec["doc_marker"]
        assert marker in doc_text, (
            f"interactive-exception capability {cap!r} is NOT documented in docs/CLI.md "
            f"(expected marker {marker!r})"
        )
        for adj in spec["adjunct_commands"]:
            assert adj in actual, (
                f"interactive exception {cap!r} promises headless adjunct {adj!r} but it is "
                "not in the parser"
            )
    print(
        "  %d interactive-exception(s) documented in docs/CLI.md + adjuncts present OK"  # noqa: UP031
        % len(INTERACTIVE_EXCEPTIONS)
    )


# ----------------------------------------------------------------------------
# Every group and command exposes --help (friendliness contract). We just prove
# each leaf parser has a help-producing action; argparse guarantees -h, so this
# is a cheap invariant that also fails loudly if a leaf is somehow malformed.
# ----------------------------------------------------------------------------
# ----------------------------------------------------------------------------
# Check (c): every EXPECTED_COMMANDS leaf is DOCUMENTED in docs/CLI.md.
#
# The apikey-list stale-row / org-delete + rule-show missing-row drift (a feature
# shipped with the CLI.md diff empty) motivated this guard. A leaf is "documented"
# when its exact command path appears in a backtick code span in CLI.md (`<path>`
# or `<path> ...`). DOC_EXEMPT holds the PRE-EXISTING leaves that CLI.md documents
# only in GROUPED / family form (e.g. `admin semantic enable` / `disable`, the
# connector `*-translate` families) — an explicit, visible doc-debt ledger. A NEW
# feature command must be documented INDIVIDUALLY (never added to DOC_EXEMPT without
# cause); that is exactly what catches the drift and prevents it recurring.
# ----------------------------------------------------------------------------
DOC_EXEMPT: frozenset = frozenset()
# 2026-09-17 (#162): EMPTIED. The CLI.md fact-check gave every one of the 54 remaining
# entries its own table row, so the ledger is paid off. Keeping stale entries here is
# worse than useless: this suite stops checking them, so a row deleted tomorrow would
# pass. `verify_cli_documentation` now ratchets EVERY leaf and flag against docs/CLI.md,
# which is the durable version of what this list was approximating by hand.
# 2026-08-04 documentation audit: the `engine`, `providers`, `segprofile`, `context`
# and `content-type` groups plus `project author` / `project status` came OFF this list
# in the same change that gave each of them a real section in docs/CLI.md -- THIRTY
# leaves (84 exempt -> 54). They shipped with zero documentation behind an exemption
# that made that invisible: the ledger was doing its job as a ledger and none as a
# deadline. They cannot rot back out -- the assertion below now holds every one of
# them to a row of its own.


def test_every_expected_command_is_documented_in_cli_md():
    cli_doc = os.path.join(_ROOT, "docs", "CLI.md")
    with open(cli_doc, encoding="utf-8") as fh:
        doc_text = fh.read()

    def _documented(cmd: str) -> bool:
        # A backtick code span beginning with the exact command path, followed by a
        # space (args) or the closing backtick.
        return re.search(r"`" + re.escape(cmd) + r"(?:[ `])", doc_text) is not None

    undocumented = {c for c in EXPECTED_COMMANDS if not _documented(c)}
    # Every undocumented leaf MUST be a known grouped-form exemption. A NEW command
    # with no CLI.md row and no exemption -> FAIL (the drift-catcher).
    orphans = sorted(undocumented - DOC_EXEMPT)
    assert not orphans, (
        "CLI leaf commands in EXPECTED_COMMANDS with NO row in docs/CLI.md (document "
        "each individually; do NOT add a NEW feature to DOC_EXEMPT):\n  " + "\n  ".join(orphans)
    )
    # Keep DOC_EXEMPT honest: a leaf listed as exempt that is NOT actually in the
    # parser is stale (removed command) and should be pruned from the exemption set.
    actual = _actual_leaf_paths()
    stale_exempt = sorted(c for c in DOC_EXEMPT if c not in actual)
    assert not stale_exempt, (
        "DOC_EXEMPT lists commands that no longer ship (prune them):\n  "
        + "\n  ".join(stale_exempt)
    )
    print(
        "  every EXPECTED_COMMANDS leaf is documented in docs/CLI.md or a known "  # noqa: UP031
        "grouped-form exemption (%d exempt) OK" % len(DOC_EXEMPT)
    )


def test_every_leaf_command_has_help():
    parser = build_parser()
    leaves = list(_leaf_paths(parser))
    assert leaves, "parser produced no leaf commands"

    # Re-walk to confirm each leaf parser carries a help action (argparse adds one
    # unless add_help=False was passed - which no command does).
    def _check(p):
        subs = [a for a in p._actions if a.__class__.__name__ == "_SubParsersAction"]
        if not subs:
            assert any(a.__class__.__name__ == "_HelpAction" for a in p._actions), (
                "a leaf command is missing its --help action"
            )
            return
        for a in subs:
            for sub in a.choices.values():
                _check(sub)

    _check(parser)
    print("  every CLI group + leaf command exposes --help OK")


def test_paid_command_groups_absent_without_their_addon():
    """Open core: a paid add-on's CLI group (memory / eval / advanced / databricks) is registered
    only when its module is present; without it the group is simply not there (no traceback)."""
    from app import editions
    from app.modules import MODULES

    top = {p.split()[0] for p in _leaf_paths(build_parser()) if p}
    for m in MODULES:
        if editions.is_paid_feature(m.slug) and not editions.feature_installed(m.slug):
            leaked = sorted(set(m.cli_groups) & top)
            assert not leaked, f"{m.slug}: CLI group(s) {leaked} registered without the add-on"
    print("  paid add-on CLI groups are absent while their add-ons are not installed OK")


if __name__ == "__main__":
    failures = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
            except AssertionError as exc:
                failures += 1
                print(f"  FAIL {name}: {exc}")
    if failures:
        print("verify_cli_parity FAILED (%d)" % failures)  # noqa: UP031
        raise SystemExit(1)
    print("verify_cli_parity OK")
