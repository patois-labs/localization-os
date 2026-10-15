"""App-level settings, stored in the Setting key/value table.

Transitional compatibility shim for one release (added 2026-09-14, remove the re-export block after
the next tagged release): `__all__` re-exports _REDACTED_URL_MARKER, _REDACTED_URL_KEYS,
is_redacted_url_key, redact_url_value, InvalidSettingValue, _v_nonneg_number, _v_nonneg_int,
_v_reconcile_grace_seconds, _v_local_engine_url,
_v_egress_list, _parse_external_base_url, _v_app_base_url, _hours_on, _hours_off, _fernet from
app.services.settings_common.

Settings can override the runtime config (default provider + per-provider model)
so the Setup AI / Settings pages actually change behavior everywhere translation
happens — `apply_to_config()` pushes them into the `config` module at startup
and after every save.

Writes made by ANOTHER PROCESS (the CLI, a worker, `fly ssh` + `config set`) reach a
running server within CHECK_INTERVAL_SECONDS — see the cache block below.

Documented in: docs/architecture/configuration.md
"""

import contextlib
import ipaddress
import logging
import math
import os
import re
import sys
import threading
import time
from contextvars import ContextVar
from decimal import Decimal
from urllib.parse import urlsplit, urlunsplit

from sqlalchemy import text
from sqlmodel import Session

from app import branding, config
from app.db import engine
from app.models import Setting
from app.services.settings_common import (
    _REDACTED_URL_KEYS,
    _REDACTED_URL_MARKER,
    InvalidSettingValue,
    _fernet,
    _hours_off,
    _hours_on,
    _parse_external_base_url,
    _v_app_base_url,
    _v_egress_list,
    _v_local_engine_url,
    _v_nonneg_int,
    _v_nonneg_number,
    _v_reconcile_grace_seconds,
    is_redacted_url_key,
    redact_url_value,
)

__all__ = [
    "_REDACTED_URL_KEYS",
    "_REDACTED_URL_MARKER",
    "InvalidSettingValue",
    "_fernet",
    "_hours_off",
    "_hours_on",
    "_parse_external_base_url",
    "_v_app_base_url",
    "_v_egress_list",
    "_v_local_engine_url",
    "_v_nonneg_int",
    "_v_nonneg_number",
    "_v_reconcile_grace_seconds",
    "is_redacted_url_key",
    "math",
    "redact_url_value",
]

log = logging.getLogger("loc_tms.settings")

DEFAULTS = {
    "qe_jev_enabled": "1",
    # Jev confidence floor: at least 0.5 and less than 1.0, default 0.6.
    "qe_jev_confidence_floor": "0.6",
    # The product/brand name shown on every runtime user-visible surface (page
    # titles, app-shell header, CLI banners, notification copy). ONE value drives
    # the whole white-label rename — read it ONLY via app.branding.brand_name(),
    # never hardcode the product name (tests/verify_branding.py enforces this).
    # Blank/whitespace resolves to app.branding.DEFAULT_BRAND at read time.
    "brand_name": branding.DEFAULT_BRAND,
    # Brandable coined FEATURE names (SPEC-brand-as-config §D8). Same validation +
    # web/CLI parity as brand_name; blank resolves to the registry default at read.
    # STANDARD industry terms (TM, glossaries, LQA, ...) are deliberately NOT here.
    "brand_llmemory": branding.REGISTRY["brand_llmemory"],
    "brand_ledger": branding.REGISTRY["brand_ledger"],
    # The install's organization / instance label (customer-supplied). Blank =
    # "not configured": app.branding.org_name() then falls back to the product
    # brand, so a fresh install shows its own name rather than a hardcoded literal.
    "org_name": "",
    # a11y T10 (maintainer decision 3): where a person reports an accessibility problem, shown
    # in the Accessibility section of /help. A plain email address or an https:// link;
    # blank = "Contact your administrator". Instance DATA, not branding: an operator
    # replaces it with their own contact. Validated by _v_accessibility_contact on every
    # write door and re-checked on read by accessibility_contact().
    "accessibility_contact": "",
    "default_provider": config.DEFAULT_PROVIDER,
    # Backup engine for per-segment MT (translate.spec_for_project) when the
    # primary provider errors out mid-translation. Blank = no fallback (today's
    # behavior unchanged unless configured).
    "default_fallback_provider": config.DEFAULT_FALLBACK_PROVIDER,
    "default_source_locale": "en-US",
    # --- language management (P2, item 1) ---
    # enabled_locales: JSON list of the locale codes OFFERED in the project /
    # language-set pickers (a UI-offering filter, NOT a hard validation gate — see
    # languages_catalog.is_valid). Blank/unset falls back to the digest's top-30
    # (languages_catalog.DEFAULT_ENABLED) so a fresh install + every existing suite
    # behaves identically until an admin customizes it on the "Languages" settings
    # page. The full BCP-47 catalog lives in app/resources/languages.json.
    "enabled_locales": "",
    # UI language for the whole install (per-user override lives on User.ui_locale
    # / session["ui_locale"]). "en" keeps English the compiled-in default so every
    # existing test — none of which sets a locale — renders the exact English it pins.
    "default_ui_locale": "en",
    "model_claude": config.DEFAULT_MODELS["claude"],
    "model_gemini": config.DEFAULT_MODELS["gemini"],
    "model_openai": config.DEFAULT_MODELS["openai"],
    # Local / self-hosted (LM Studio etc.). Ships EMPTY: a deployment sets
    # its own server in Setup AI. Blank base = local engine "not configured" (never probed).
    # Existing installs keep whatever value is saved in their DB.
    "local_base_url": config.LOCAL_BASE_URL or "",
    "local_api_key": config.LOCAL_API_KEY or "lm-studio",
    "local_model": config.DEFAULT_MODELS.get("local", ""),
    # #342: sampling params sent on EVERY local-engine call (temperature always, seed
    # only when non-blank). Without them each self-hosted server used its own default
    # (LM Studio 1.0, llama.cpp 0.8), so the same workflow drew different text and
    # different LQA verdicts every run. Judge tasks (LQA/QE/verification) read the
    # separate judge temperature; see _local_sampling in providers_hub.
    "local_temperature": "0.2",
    "local_judge_temperature": "0.0",
    # blank = send no seed key (the server's own behavior); 42 = the measured
    # repeatable default (2026-09-29 engine-parity run: temp 0 + seed 42 made the local
    # router fully repeatable).
    "local_seed": "42",
    # GitHub #341: at run start, ask a router-mode local server (llama.cpp --models-max) to
    # preload the local-engine models the workflow's steps will use, so a later step does not
    # stall on a 10-30 s model load. Best effort, background, capped at 3 models; "0" = off,
    # nothing is sent. See app/services/model_warmup.py.
    "local_model_warmup": "1",
    # bounded concurrency for per-segment MT provider calls (background jobs + SSE).
    # SPEC-parallel-routing Phase 1 gave this key a SECOND job, deliberately rather than
    # adding a knob: it also bounds the LANES of a parallel step group (two checks that
    # run together inside one run — orchestrator._run_parallel_group). Sharing the
    # ceiling is what keeps worst-case in-flight at batch_max_parallel * mt_concurrency
    # instead of growing a third multiplier. CONSEQUENCE WORTH KNOWING: setting this to
    # 1 makes a parallel group's lanes run SEQUENTIALLY. That is honoured, not floored —
    # an install that asked for one provider call at a time gets one — and the group's
    # RESULT is identical either way (lanes never observe each other); only wall-clock
    # changes.
    "mt_concurrency": "4",
    # run-level concurrency ceiling for a fan-out batch (SPEC-workflows-redesign
    # Phase 5a): how many member runs (one per locale) a batch launches in parallel.
    # Default 3, clamped 1..8 (batch_runs.clamp_max_parallel); a per-batch form/flag
    # override is clamped to the same range. MULTIPLIES with mt_concurrency (per-segment
    # within one member) — worst-case in-flight provider calls ~= this * mt_concurrency,
    # so both are clamped conservatively (see app/services/batch_runs.py).
    "batch_max_parallel": "3",
    # global kill switch for scheduled connector syncs ("0" pauses every
    # schedule without touching per-connection config)
    "sync_scheduler_enabled": "1",
    # kill switch for project automation (app/services/project_automation.py): "0" = no project
    # schedule fires, and binding, scheduling and running from a project are refused. Reads and
    # unbinding stay up. Default on is a no-op until a person turns a schedule on.
    "project_automation_enabled": "1",
    # global kill switch for outbound project notifications (Slack, Teams, signed webhook, email
    # channels; app/services/notify.py). "0" = nothing is queued or sent; the older email paths in
    # notifications.py are unchanged. Default on is a no-op until a channel exists.
    "notify_outbound_enabled": "1",
    # --- per-connector-KIND visibility (ffadmin Wave 5) ------------------------
    # CSV of connector kind slugs an admin has DISABLED on this install (e.g.
    # "jira,shopify"). Default "" = every kind enabled = zero behavior change on
    # ship. This is a DIFFERENT mechanism from the feature flags: the Connectors
    # SECTION stays always-on (core, 2026-08-11); this only hides individual
    # KINDS. A disabled kind disappears entirely, following the kill-switch law (a
    # switched-off capability is non-enumerable -- its /connectors tile is gone, its
    # /connectors/{kind} + connection pages 404, its per-kind /api translate route
    # 404s, create is refused on every surface, and the scheduler skips its
    # connections). REGISTRY (app/services/connectors/__init__.py) stays the single
    # source of kinds -- this CSV only ever SUBTRACTS from it, and unknown slugs are
    # ignored (a malformed entry can only ever hide, never surprise-enable). Existing
    # connections of a disabled kind keep their data untouched and return on re-enable.
    # Rides set_many (audited + version-bumped -> a running server picks a toggle up
    # within ~2s, the K3 property). Toggled from /connectors/manage or the catalog;
    # `config set connector_disabled_kinds "..."` is the CLI twin.
    "connector_disabled_kinds": "",
    # mock-egress guard override (see app/services/connectors/egress_guard.py).
    # Default OFF ("0") = guard ON: connector write-backs REFUSE to push non-live
    # (offline MockProvider) MT output to a real external system, so a keyless run
    # can't egress placeholder text to a real Jira/Airtable as if it were a real
    # translation. "1" permits it (a staging system you WANT filled with mock).
    # Env-seeded from LOC_TMS_ALLOW_MOCK_EGRESS; the runtime authority is this
    # Setting so `config set` / the Setup AI UI flip it live.
    "allow_mock_egress": "1" if config.ALLOW_MOCK_EGRESS else "0",
    # --- project lifecycle auto-close (P1, item 2) ---
    # Days a project may sit in COMPLETED before the scheduler tick moves it to
    # CLOSED (still reopenable — living projects). "5" mirrors the XTM 5-day
    # archive default; "0" disables auto-close entirely. The clock anchors on
    # Project.completed_at (set when a project first reaches COMPLETED).
    "project_auto_close_days": "5",
    # Inbound API requests per identity per minute, in separate spend/read windows.
    # Reads includes cheap writes; "0" disables either budget independently.
    "api_rate_limit_per_minute": "120",
    "api_read_rate_limit_per_minute": "600",
    # data retention (Phase B chunk 3): automated purge of aged uploads /
    # documents / terminal jobs + their TM. OFF by default ("0") because purge is
    # DESTRUCTIVE and irreversible; retention_days "0" is also a no-op (no window),
    # so an install is never swept until BOTH are set. Never enable on the real DB
    # without an explicit decision.
    "retention_enabled": "0",
    "retention_days": "0",
    # completion emails (Phase B chunk 5): email the submitter when a
    # translate_document job finishes. Default ON — it is a graceful no-op without
    # SMTP configured (mailer.send returns False), so it costs nothing by default.
    "notify_on_job_finish": "1",
    # human-review assignment emails (Feature B): email the assignee when a
    # workflow pauses at a human_review step that has an assignee_email. Default ON
    # — a graceful no-op without SMTP configured (mailer.send returns False), so it
    # costs nothing by default (mirrors notify_on_job_finish).
    "notify_on_review_assigned": "1",
    # query notifications (SPEC-wave3 §4.6): email the assignee on a new query, the
    # author+assignee (minus the actor) on a reply, and the author on close. Default ON
    # — a graceful no-op without SMTP configured (mailer.send returns False), so it costs
    # nothing by default (mirrors notify_on_review_assigned). ALSO ANDed with the
    # queries_enabled kill switch: no query mail of any kind is sent when queries is off.
    "notify_on_query": "1",
    # per-project budget threshold alerts (CV-2): email the admins when a project
    # crosses 80% / 100% of its monthly USD budget. Default ON — a graceful no-op
    # without SMTP (mailer.send returns False), so it costs nothing by default
    # (mirrors notify_on_review_assigned / notify_on_job_finish).
    "notify_on_budget_threshold": "1",
    # Public URL authority for web links, SSO/provisioning and callback guidance.
    # Blank falls back to the request base. Background review emails have no request,
    # so they omit the direct link and tell the reviewer to open their review queue.
    # The Fly deploy runbook should set this post-deploy.
    "app_base_url": "",
    # --- global spend-cap kill switch (safety brake) ---
    # spend_cap_usd: a monthly USD ceiling on REAL LLM spend. "0" (default) = OFF:
    # nothing is enforced and installs behave exactly as before. A positive float
    # (e.g. "50") is the cap — once the current month's actual spend (priced per
    # app/services/pricing.py from the token usage the provider seam captures)
    # reaches it, run_completion refuses new REAL provider calls with a terminal
    # error until an admin raises or clears it. MockProvider is exempt (priced at 0),
    # so offline/local/test installs never trip. spend_period is FIXED to "month"
    # for now (documented single mode); any other value is treated as month. Edit
    # via the Setup AI page or `loc-tms config set spend_cap_usd <amount>`.
    "spend_cap_usd": "0",
    "spend_period": "month",
    # --- prompt tap (default OFF) ---
    # Opt-in observer that records the EXACT request payload run_completion sends on
    # each completion call, for prompt tuning against LM Studio (whose own logs
    # truncate). OFF ("0") = zero writes, zero file creation — byte-identical to an
    # install without the tap. Flip via the /prompt-tap page toggle or the
    # `prompt-tap on|off` CLI. See app/services/prompt_tap.py.
    "prompt_tap_enabled": "0",
    "prompt_tap_envelopes_enabled": "1",
    # Orphan reaper (residual sweep): a run envelope left RUNNING by a crash / kill /
    # uvicorn-reload is closed once it ages past this many hours AND its backing run is
    # gone/terminal (tap_envelope.reap_orphaned_envelopes, piggybacked on the scheduler
    # tick). Default 24h -- no translation run lives a day; "0" disables the reaper. The
    # reaper only ever CLOSES stale rows, so it runs regardless of prompt_tap_enabled.
    "prompt_tap_orphan_reap_hours": "24",
    # Age-based retention for the per-action run-capture FILES (prompt_tap/actions/*.jsonl),
    # swept on the scheduler tick (prompt_tap.sweep_expired_captures). Those files are
    # append-only and NOT rotated (the 500-record cap is retired for run capture), so without
    # an age-out they grow without bound on a long-lived install -- the soak of 2026-08-14
    # measured ~11 MB/h under load (finding S1). A capture file whose LAST WRITE (mtime) is
    # older than this many days is deleted; a file still being appended keeps a fresh mtime
    # and is never swept, so the file currently being written is protected by construction.
    # ON BY DEFAULT at 30 days. 30d is safe on the first deploy tick: prod's
    # captures only date to 2026-08-06, so nothing is old enough to be swept immediately, and a
    # non-terminal (running/paused) envelope's file is never swept regardless of age (so a
    # paused vendor leg is safe). Raise it for long-retention installs -- e.g. 180 (prod; the
    # Fly volume is 3GB, watch the arithmetic) -- a CONFIG change, not a code change
    # (PROPOSAL-prompt-tap-run-envelopes section 7 / D4). "0" DISABLES the sweep and is the ONLY
    # disable value; a negative/garbage value is treated as invalid (default + a re-arming
    # WARN), never as "delete everything". The sweep also only runs while capturing is ON
    # (prompt_tap_enabled): a disabled tap freezes retention with it (the 2026-08-06 evidence-
    # preservation posture). Envelope ROWS are NOT swept here (they carry the PM history and no
    # prompt text) -- they fall only to the orphan reaper + the guarded purge.
    "prompt_tap_ttl_days": "30",
    # Grace window (seconds) for the guarded-purge staged-file reconciler
    # (ledger_controls.reconcile_staged_purges). A `<stem>.purge-<hex>-<epoch>` file younger than
    # this is left alone because it may belong to a live purge in ANOTHER process (boot vs a
    # running server) that the in-process staging lock cannot see; the age is read from the
    # STAGING epoch carried in the name, not the raw-capture mtime (a rename does not touch mtime).
    # Default 600s (10 min); a real batch's staging window is milliseconds. It governs a
    # data-deletion safety guard, so it is HOST-ONLY (REMOTE_IMMUTABLE) and bounded 0..86400 by a
    # write-door validator; the read side (_reconcile_grace_s) still fails safe to the default.
    "ledger_purge_reconcile_grace_seconds": "600",
    # --- conditional-workflow loop safety caps (SPEC-conditional-workflows §4.2, D4) ---
    # Both back-stop a workflow with backward branch edges (loops) so a runaway graph
    # can never spin forever. Config-overridable; the shipped defaults are the maintainer's D4.
    #   workflow_max_visits: per-step per-run execution ceiling a step INHERITS when its
    #     own max_visits is NULL. Enforced in TWO places (issue #94), so the flag means
    #     what its four operator surfaces say -- a step's own EXECUTIONS, in every role:
    #       * a loop-back that would revisit a step past its ceiling is refused by the
    #         branch seam, which falls through instead;
    #       * a step the cursor REACHES that has already run its ceiling is not executed
    #         (the pre-execution check `_visit_cap_decision`) -- this is what bounds the
    #         gate in the canonical correction loop, where the gate is the branch SOURCE
    #         and the ceiling used to bind only the target.
    #     Either way the loop ends gracefully and the run COMPLETES (flagged in
    #     run.context.loop_ended), NOT a hard fail. WHAT "ends" means depends on the step
    #     (SPEC-conditional-workflows §4.2 erratum E1): a capped GATE ends the RUN, in the
    #     HELD posture when its standing verdict is FAILED (segments needs-review, export
    #     refused until reviewed); a capped PRODUCER is SKIPPED and the run continues so
    #     the gate still judges the last produced text. A non-positive stored ceiling is
    #     treated as unset and inherits this default. ERRATUM E2 (round 2): the HELD
    #     posture is NOT special to the ceiling -- ANY run that ends with a gate's
    #     standing verdict FAILED (or a gate the judge never answered) is held, and
    #     routing is unchanged.
    #   workflow_total_step_cap: absolute run-level cap on TOTAL step executions. Hitting
    #     it fails the run cleanly (FAILED "loop_cap_reached") — the last-resort backstop
    #     against a pathological graph. A linear run executes <= (step count) steps, well
    #     under this, so it never trips on a plain/Phase-2 workflow.
    "workflow_max_visits": "5",
    "workflow_total_step_cap": "50",
    # --- external/vendor dispatch-and-wait (PROPOSAL-external-processing-step Wave A) ---
    # vendor_dispatch_ttl_hours: the default bounded-wait window for a parked external
    # leg before it escalates to a human_review pause (decision 3 — expiry ALWAYS
    # escalates, never hard-fails). A step's `external_wait_hours` overrides this per
    # step; NULL there inherits this. 168h = 7 days (the prompt-tap run-envelope TTL
    # floor). 0 disables the bounded wait (the leg parks indefinitely).
    "vendor_dispatch_ttl_hours": "168",
    # vendor_rework_max_rounds (#632): how many reject-and-rework rounds one dispatch leg may
    # take before the next rejection escalates to a human instead of re-dispatching to the
    # vendor (read by app/services/vendor_rework.py::rework_cap). A whole number from 1 to
    # VENDOR_REWORK_MAX_ROUNDS_CAP, refused at every write door (web, `config set`, POST
    # /api/admin/config, MCP manage_install); a bad stored value reads as this default.
    "vendor_rework_max_rounds": "1",
    # --- vendors (external translation/review) kill switch (ffadmin Wave 3) ---
    # Default ON so the Vendors module is visible; "0" is the emergency off following
    # the kill-switch law (a switched-off capability is non-enumerable -- 404, never
    # 403). Off removes the whole vendor surface: the /vendors + /vendor-studio pages
    # and nav entry, every /api/vendors endpoint, the CLI `vendor` group, the two vendor
    # MCP tools, and the inbound vendor webhooks (/webhooks/vendor-returns +
    # /webhooks/vendor-quotes 404 BEFORE signature work). New egress is refused at the
    # dispatch seam (a run hitting an external step FAILS with the switch named, never a
    # silent skip) and the scheduler's two vendor NETWORK sweeps (return-poll + dispatch-
    # retry) stop; the DB-only expiry lifecycle sweep keeps running. Existing vendors,
    # dispatch legs, rate cards and customer links keep their data untouched -- they
    # simply become unreachable until it is switched back on.
    # IN-FLIGHT NOTE: a dispatch leg already OUT with a vendor when this is switched off
    # can no longer take a return (the webhooks 404, the poll/retry sweeps stop). It is
    # not lost -- the still-running DB-only expiry sweep escalates it to a human_review
    # pause after `vendor_dispatch_ttl_hours`, and once vendors is re-enabled the operator
    # recovers it via a vendor return re-upload (`vendor return` / the /vendors upload) or
    # `vendor redrive`. (Open question: whether to shorten that window.)
    "vendors_enabled": "1",
    # --- reports (operational cost/token/volume dashboard) kill switch (ffadmin Wave 4) ---
    # Default ON so the Reports module is visible; "0" is the emergency off following the
    # kill-switch law (a switched-off capability is non-enumerable -- 404, never 403). Off
    # removes the whole reports surface: the /reports + /reports/run pages and nav entry,
    # GET /api/reports/run, the CLI `report` group, and the run_report MCP tool (which then
    # answers 404 on call, on purpose). Reports is a pure READ/aggregation leaf -- there is
    # no egress or background seam, and no DATA is touched; the numbers simply become
    # unreachable until it is switched back on. (Reports READS other modules' data for the
    # aggregate -- e.g. vendor_spend -- but as the consumer; disabling a data source is a
    # separate switch, and disabling Reports never touches those sources.)
    "reports_enabled": "1",
    # --- queries (threaded queries + comments) kill switch (SPEC-wave3 §5) ---
    # Default ON (2026-08-19, first-class). "removed" semantics REQUIRES default-ON
    # (features.py:129-132 / verify_features.py:170-171); "0" is the emergency off following
    # the kill-switch law (non-enumerable -- 404, never 403). Off removes the /queries page +
    # nav + in-context affordances, every /api/queries* route, the `query` CLI group, the six
    # MCP tools and the queries capability card; notification sends stop; the vendor package
    # omits the queries block and the return SKIPS comment admission (silently). Existing
    # threads stay dormant and invisible; re-enabling restores them byte-identically. Cascade
    # deletes + GDPR export/erase are NOT gated by this flag (referential integrity + a legal
    # obligation are never behind a feature flag -- §5.2's fourth seam).
    "queries_enabled": "1",
    # --- reviewer worklist ("My work") kill switch (SPEC-wave3-collaboration §5) ---
    # Default ON so the /my-work page is reachable; "0" is the emergency off following the
    # kill-switch law (non-enumerable -- 404, never 403). Off removes /my-work + its nav
    # entry, GET /api/worklist (a shared-token 400 becomes moot behind the 404), the CLI
    # `work` group, the `my_work` MCP tool, and this feature's help card. It is a pure
    # READ leaf, so there is no data to preserve; nothing else changes.
    "worklist_enabled": "1",
    # --- content routing (inbound rule CRUD) kill switch (ffadmin Wave 4) ---
    # Default ON so the Content-routing admin surface is visible; "0" is the emergency off
    # following the kill-switch law (non-enumerable -- 404, never 403). Off removes the
    # routing-RULE ADMIN surfaces: the /routing page + nav entry, every /api/routing/rules
    # endpoint, the CLI `routing` group, and the routing-rule actions of the manage_admin_ops
    # MCP tool (which then answer 404 on call). DEPARTMENT BUDGETS ARE NOT PART OF THIS
    # SWITCH -- /routing/budgets, /api/budgets and the `admin budget` CLI stay reachable
    # (spend control is independent of content-routing; a budget caps token spend regardless
    # of which workflow routes a submission). The RUNTIME routing DECISION consumed by the
    # inbound-connector pipeline (routing.resolve, called from app/web/api.py) is core
    # pipeline behavior and is NOT gated by this flag: disabling the rule-authoring surface
    # must not break inbound content routing that existing rules already drive (with no
    # rules, resolve returns the default). Existing rules keep their data untouched; the
    # authoring surfaces simply become unreachable until it is switched back on.
    "routing_enabled": "1",
    # --- segmentation (SRX 2.0) ---
    # OFF by default ("0") = the legacy regex sentence splitter in segment.py
    # (byte-unchanged, the sacred gate). "1" delegates to the SRX rule engine
    # (app/services/srx.py). srx_file blank = the shipped app/resources/default.srx
    # (engineered to reproduce the legacy splitter); a non-blank value means an
    # admin-uploaded custom ruleset lives at DATA_DIR/segmentation.srx.
    "srx_enabled": "0",
    "srx_file": "",
    # --- image localization (Beta) kill switch (SPEC-image-localization 3.6) ---
    # Default ON so the "(Beta)" module is visible; "0" is the emergency off that
    # removes the nav entry and 404s/refuses every /images route, CLI `image`
    # verb, /api/images endpoint, and new image ingestion.
    "image_localization_enabled": "1",
    # --- key-based projects (Beta) kill switch (SPEC-key-based-projects 8) ---
    # Default ON so the type selector + key grid are visible; "0" is the emergency
    # off: the create seam refuses project_type="key_based", the key routes 404, the
    # CLI verbs refuse, and the nav affordances hide. Existing key projects keep their
    # data (nothing is destroyed) -- they simply become unreachable until it is back on.
    "key_projects_enabled": "1",
    # --- per-string in-context image translation context (Batch 14) kill switch ---
    # Default ON (a "removed"-semantics kill switch, so it MUST default on): the image
    # attach/list/delete/set-primary routes 404 when off (web+/api+MCP), the `keys image`
    # CLI verbs refuse, and the orchestrator threads no image bytes. Stored images are kept
    # (nothing is destroyed) -- they simply stop reaching the translation call. Also the
    # `feature_gate` the manage_key_image / list_key_images MCP tools resolve against.
    "key_image_context_enabled": "1",
    # Batch 14 Phase 1.5 (the include_string_images key-export option): aggregate caps on
    # ONE export bundle's image subtree, refused BEFORE any partial ZIP is staged. FILES =
    # total image members; BYTES = total decoded (plaintext) image bytes in the ZIP. String
    # values (the store is text); key_images reads them via settings.get with the
    # config.KEY_IMAGE_EXPORT_MAX_* fallback. Not a kill switch -- the bundle path is armed
    # by key_image_context_enabled above; these only bound its size.
    "key_image_export_max_files": str(config.KEY_IMAGE_EXPORT_MAX_FILES),
    "key_image_export_max_bytes": str(config.KEY_IMAGE_EXPORT_MAX_BYTES),
    # AUTO-UNVERIFY (spec 3, the maintainer's locked call #3): when a key's BASE value changes,
    # every other locale's non-empty translation of that key is flipped to
    # needs_review + flagged `source_changed`. This is THE defining semantic of
    # continuous localization -- without it a source edit silently ships a stale
    # translation. "1" (default ON) install-wide; the per-project override lives under
    # `key_autounverify:<project_id>` (keys.autounverify_enabled is the one reader).
    "key_autounverify": "1",
    # Delta-feed retention: KeyChangeLog rows older than this are pruned, and a delta
    # read with a cursor BELOW the surviving floor answers resync_required rather than
    # silently under-reporting (Graph's 410 pattern). "0" disables pruning entirely.
    "key_changelog_retention_days": "90",
    # --- AI Tagging (inline-fidelity tag integrity) ---------------------------
    # Governs ONLY the mask/restore layer (app/services/ai_tagging.py): when "1"
    # (default) every LLM call — incl. fix steps — sends the ⟦⟧ inline-formatting
    # tokens as opaque «TAG_N» sentinels the model cannot renumber/translate/drop,
    # and they are restored (⟦⟧) BEFORE any verification / write / TM / feedback
    # buffer. "0" turns masking OFF (tokens ride raw in {text} as pre-AI-Tagging).
    # The DETERMINISTIC VERIFICATION GATE (quality.commit_mt_output tag check, the
    # MQM remap, the post-step + export + editor guards) is ALWAYS ON regardless of
    # this switch — it is a pure-honesty backstop, never gated off — so flipping this
    # to "0" only removes the damage-REDUCTION layer, never the damage-DETECTION one.
    "ai_tagging_enabled": "1",
    # AI Tagging 1.5c — the LLM tag-repair rung (app/services/tag_llm_repair.py). "1"
    # (default) lets a segment whose tags the DETERMINISTIC rung could not fix go to an
    # LLM with an explicit per-token brief ("⟦r3⟧ is missing; in the source it wraps
    # '...'") before it quarantines. INDEPENDENT of ai_tagging_enabled: masking off
    # makes damage MORE likely, so repair must stay available. "0" restores the
    # pre-1.5c behavior exactly (deterministic rung, then quarantine) and spends
    # nothing. The verification GATE is never gated off by either switch.
    # DEFAULT OFF (2026-07-25). The rung is code-complete and its prompt-hygiene
    # SIBLINGS (the mask-layer fixes + notation-specific system prompt) ship enabled and
    # are unaffected by this switch -- but two independent reviews returned FIX-FIRST on
    # the RUNG itself with three CRITICAL findings that ship silent corruption into TM and
    # to live connectors.
    # It ships DARK so the measured damage-rate win lands without the risk surface.
    # DO NOT flip this to "1" until C1-C3 + H1-H4 in that report are closed and re-reviewed.
    "tag_llm_repair_enabled": "0",
    # The SECOND, independent placement-verification call. "1" (default) is what lets a
    # repair commit CLEAN: structural + body-preservation locks prove the tokens are
    # back and no word changed, but only this call judges whether they landed in the
    # RIGHT places. "0" keeps repairs but never certifies placement — every LLM repair
    # then commits flagged + NEEDS_REVIEW (half the spend, same honesty, more review).
    "tag_llm_repair_verify_enabled": "1",
    # How many INDEPENDENT placement verdicts must ALL agree before a repair commits
    # clean (1-5, default 2). Live QA 2026-07-25 measured verification as ENGINE-BOUND:
    # claude-haiku-4-5 judged 4/4 of the probe cases correctly and repeatably, while
    # meta-llama-3-8b-instruct approved DELIBERATELY MISPLACED tags on 2 of 3 tries.
    # Unanimity is cheap insurance (a rejection short-circuits, and a good verifier is
    # deterministic so it costs nothing in false negatives) but it is NOT a substitute
    # for pointing the verifier at a capable engine -- inside a workflow the orchestrator
    # routes it to the LQA step's engine for exactly that reason.
    "tag_llm_repair_verify_votes": "2",
    # REVIEW M1 (2026-07-25). Placement verification is ENGINE-BOUND and the gap is large
    # (claude-haiku 4/4 correct and repeatable; meta-llama-3-8b approved deliberately
    # misplaced tags 2 of 3 tries). Inside a workflow the orchestrator routes lock 3 to
    # the LQA step's engine, but the ON-DEMAND surfaces (CLI `tagging repair`, the
    # editor's "fix tags" button) and the plain-MT pretranslate path have no LQA step to
    # borrow from and were verifying with the TRANSLATE engine. Set this to a capable
    # provider name (e.g. "claude") to give those surfaces a trustworthy judge. Blank =
    # fall back to the repair engine, which is honest but only as good as that engine.
    "tag_llm_repair_verify_provider": "",
    # GitHub #41 — what a SEGMENTED export does with a tag-damaged segment. Before 1.7 it
    # shipped the damage into the customer's file, silently collapsing formatting (6 of 16
    # formatted paragraphs in a live DOCX probe), because paragraph_texts_for consulted
    # neither status nor qa_flags nor tag_damage.
    #   "source"     (default) fall back to the SOURCE text for that segment. Its tokens
    #                are correct by construction, so the document's formatting survives
    #                and the unit is VISIBLY untranslated rather than invisibly broken.
    #   "quarantine" refuse the whole export, consistent with the FULL_TEXT 1.6 guard.
    #                Strictest; blocks delivery until a human resolves the segments.
    #   "ship"       the pre-1.7 behaviour (write the damaged target anyway) — an explicit
    #                opt-out, never a default.
    # BUG-04 (QA campaign, prod): the policy is consulted on EVERY format. It used to be
    # skipped unless the document's SOURCE already carried a token — true only for
    # docx/xliff/html — so on md/txt/po/srt/json/xlsx it was dead, and a model that
    # INVENTED mask notation on a tagless source shipped `⟦g1⟧` into the delivered file.
    # A document that is tagless ON BOTH SIDES is still unaffected under every policy
    # (`quality.tag_damage` returns "" for it); what changed is the ASYMMETRIC case.
    "export_tag_damage_policy": "source",
    # --- LQA safety floor (PROPOSAL-lqa-glossary-adjudication Phase 1) ---------
    # Two DETERMINISTIC, zero-spend, LLM-free guards inside the LQA -> fixer correction
    # loop (app/services/lqa_safety.py). Both default ON; both are exact no-ops on an
    # install with no glossary and no TM hits, so turning them on changed nothing there.
    #   glossary_fix_guard_enabled: (C) the FIXER POST-CHECK. Before a step's output
    #     replaces a segment's working text, the glossary/DNT violations of the PRE-fix
    #     and POST-fix text are compared; if the edit INTRODUCED one that was not there
    #     before, the edit is DISCARDED, the pre-fix text kept, and a `fix_reverted`
    #     qa_flag + NEEDS_REVIEW recorded. A DELTA, never an absolute: a pre-existing
    #     violation the edit leaves alone is untouched (the write-back gate already
    #     flags it), and an edit that FIXES a violation is kept. The whole-document
    #     FULL_TEXT path FLAGS (`fix_term_regression`) but never reverts — one
    #     introduced violation must not discard an otherwise good pass. "0" restores the
    #     pre-Phase-1 behaviour exactly (the provider result always wins).
    #   tm_fix_protection_enabled: (D1) the TM PROVENANCE GUARD. Segments filled by a
    #     100% TM match are dropped from a CORRECTION-LOOP step's pending set (a step
    #     gated on prev_lqa:failed, or an LQA on_fail loop-back target), so a fix step
    #     that runs because the DOCUMENT-level LQA failed can no longer rewrite a
    #     byte-exact TM match and report it as fresh MT. Per-step `tm_protect` overrides
    #     in both directions. "0" restores the pre-Phase-1 behaviour exactly.
    "glossary_fix_guard_enabled": "1",
    "tm_fix_protection_enabled": "1",
    # #159: exclude exactly the TM-origin units the check's repairing step refuses.
    # "0" restores whole-document LQA; all-excluded checks skip via on_pass.
    "lqa_tm_lock_exclusion_enabled": "1",
    # --- glossary-aware LQA judge + adjudication (same proposal, Phases 2 and 3) ---
    # Both default ON, and both are exact no-ops on an install with no glossary: no
    # matched term -> the judge prompt renders ZERO extra bytes and the adjudicator is an
    # identity function, so every glossary-less run is byte-identical to pre-Phase-2.
    #   lqa_glossary_context_enabled: (A) the GLOSSARY-AWARE JUDGE. The run's ALREADY-
    #     MATCHED glossary terms are injected into the LQA judge prompt as DATA WITH A
    #     CAVEAT — approved terminology that takes PRECEDENCE OVER THE STYLE GUIDE (the
    #     standing policy: the glossary is always the terminology authority, a style
    #     guide carries mechanics only), with explicit licence to still flag an approved
    #     term that is wrong IN CONTEXT under the dedicated dimension
    #     "Terminology/Approved-term-misuse". Only MATCHED terms are ever sent, never the
    #     termbase. "0" restores the pre-Phase-2 judge prompt byte-for-byte.
    #   lqa_adjudication_enabled: (B) DETERMINISTIC ADJUDICATION. Between the LQA error
    #     assembly and the score, a finding whose returned `span` targets a LEGITIMATELY
    #     APPLIED approved term is DOWNGRADED to `neutral` (penalty 0, cannot fail the
    #     gate) and marked — never deleted. It stays in the card, in the LQAResult rows
    #     and in the report, and is withheld ONLY from the fixer's {lqa_findings} block
    #     with an honest "(N adjudicated)" line. Prompts persuade, gates enforce: this is
    #     the layer that makes A's promise real. "0" restores the pre-Phase-3 score,
    #     gate and findings block exactly.
    "lqa_glossary_context_enabled": "1",
    "lqa_adjudication_enabled": "1",
    # --- concept-glossary renderer token cap (Terminology backend T3, SPEC §9-T3 /
    # LAW-TERM-13) ---------------------------------------------------------------
    # The install-wide default token ceiling for the rendered {glossary} block when a
    # workflow step turns its concept_glossary_render toggle ON with glossary_token_cap
    # left at 0 (inherit). Bounds the rendered concept BLOCK alone (not the assembled
    # prompt); glossary_concepts.render_for_llm drops whole concepts from the tail until
    # the block plus its "(+N more…)" marker fits. Inert until a step opts into the
    # renderer (default OFF), so a fresh install / every existing run is byte-identical.
    "glossary_prompt_token_cap": "1500",
    # --- Paid add-on kill switches (app/editions.py) -----------------------------------
    # The code behind these flags ships in the paid add-ons; the flags stay in the open core
    # (Module rows + the Features page read them, an installed add-on obeys them). With the
    # add-on absent they are inert and the Features page hides them.
    "llmemory_enabled": "1",
    "llmemory_suggestions_enabled": "0",
    "advanced_workflows_enabled": "0",
    "evaluations_enabled": "1",
    "databricks_export_enabled": "0",
    # --- Terminology concept editor (SPEC-terminology-backend T1-lite) -----------
    #   terminology_concept_editor_enabled: the opt_in feature flag (app/modules.py),
    #     DEFAULT OFF this release. The concept/term-store rich EDITOR (create/edit/delete
    #     concept, add/edit/delete term, set audience) is the dual-write reconciler surface
    #     whose round-7 gate found live regressions; it ships behind this switch until T1b
    #     (authority inversion) removes the two-way sync. OFF = every concept-editor WRITE
    #     seam refuses naming the switch on all four surfaces (web POST / /api / the
    #     `glossary ... concepts/terms` CLI leaves / the edit_glossary MCP actions -- all
    #     delegate to app/services/glossary_concepts.py), and a `.glossary.json` import is
    #     allowed ONLY into an empty glossary (no entries/concepts) with >=1 language pair.
    #     Concept READS (list/show, JSON export, the read-only web view) and the migration /
    #     backfill / consolidation seed paths stay open either way. Operator-editable on
    #     every surface like any module switch (NOT host-only).
    "terminology_concept_editor_enabled": "0",
    # --- System prompt overrides (GitHub #356 phase 2a) --------------------------
    #   system_prompt_overrides_enabled: the opt_in feature flag (app/modules.py), DEFAULT
    #     OFF. ON lets an org ADMIN override the editable HEAD of the AI-step system message
    #     and of the machine-translation system message (the locked floor is never
    #     editable), with version history, restore and reset; every run then resolves its
    #     own org's override. OFF = no override is read or written anywhere and every
    #     composed system prompt is byte-identical to the built-in text; the System prompts
    #     catalogue stays read-only. Operator-editable on every surface like any module
    #     switch (NOT host-only).
    "system_prompt_overrides_enabled": "0",
    # --- Ask + Draft (SPEC-ask-draft) ------------------------------------------
    # The grounded module assistant ("Ask about this page") and the
    # describe-what-you-want drafter ("Describe what you want"). See
    # app/services/assist.py.
    #   assist_enabled: the kill switch, default ON. Ask is READ-ONLY (it answers
    #     only from the repo-tracked capability cards, never from the database) and
    #     Draft NEVER WRITES (it renders a pre-filled form whose submit button is the
    #     module's existing, already-gated seam), so the default-on posture costs no
    #     new risk surface. "0" hides every Ask/Draft control AND 404s every assist
    #     route on both surfaces - the /help card pages stay up, because they are
    #     plain documentation with no model call in them.
    #   assist_provider: which engine answers. Blank = the install's
    #     default_provider. The shipped default is `local` because an assist answer
    #     is short, frequent and worth nothing to a cloud bill.
    #   assist_model: blank = the provider's own default model (an engine profile's
    #     model, the local slot's configured model, a built-in cloud default).
    "assist_enabled": "1",
    "assist_calls_per_minute": "6",
    "assist_calls_per_day": "60",
    "assist_provider": "local",
    "assist_model": "",
    # --- UTCP manual endpoint (BACKLOG 6b, experimental) -----------------------
    # The read-only UTCP discovery manual served at GET /utcp
    # (app/services/utcp_manual.py). Default ON: the manual is generated from the
    # OpenAPI schema and exposes a STRICT READ-ONLY SUBSET (allow-listed GET tools) of
    # the /api surface. The FULL schema at /openapi.json needs a session or an API key
    # once the login wall is on (issue #286); this subset stays public so a client can
    # discover the tools before it holds a key. "0" is the emergency off: GET /utcp answers 404
    # (the kill-switch law — a switched-off capability is non-enumerable, not merely
    # forbidden), exactly like image_localization_enabled / key_projects_enabled.
    # MCP stays the first-class agent protocol (PROPOSAL-utcp-decision.md, Option A);
    # this manual is the additive Option B hedge and is marked experimental in its own
    # info block.
    "utcp_manual_enabled": "1",
    # --- TM matching / locks (PROPOSAL-tm-matching-workflow-step §6) ---
    # The three per-TM policy controls the `tm_match` step reads at run time
    # (tm_lock.resolve_policy). v1 = GLOBAL (this codebase has no TM container object;
    # per-project override is the documented next step). Defaults per §2 D2/D3/D11.
    "tm_lock_ice": "1",  # ICE (101) matches hard-lock by default (inert until
    # the ICE/context wave lands -- no context column yet)
    "tm_hundreds_enter_lqa": "1",  # 100s SOFT-lock (enter LQA); "0" -> HARD lock
    "tm_context_floor": "95",  # context-attach floor (deferred context wave)
    # Batch 15 T1 (SPEC-tm-match-step-config §11): the DEFAULTS for a NEW tm_match step
    # (seeded into the five scalar per-step columns at creation) AND the inherited value
    # for an existing (NULL-override) step. tm_fill_threshold "100" = fill exactly the
    # 100 band (byte-identical to pre-Batch-15). tm_fuzzy_lock_mode "soft" = a below-100
    # fuzzy fill enters LQA (the SAFE default per LAW-TM-6; inert while the threshold is
    # 100). NO count pin covers settings DEFAULTS keys (§2.7), so these add freely; the
    # three existing tm_* keys above are NEVER removed or repurposed (doc/registry pins).
    "tm_fill_threshold": "100",  # min match % to FILL for a new/inheriting tm_match step
    "tm_fuzzy_lock_mode": "soft",  # how a below-100 fuzzy fill locks: none|soft|policy
    # Batch 15 T2 (SPEC-tm-match-step-config §9/§11, D15-6): the TM-only delivery empty
    # gate. ON by default -- a deliverable produced by a tm_match workflow (the durable
    # WorkflowRun.used_tm_match marker) that still carries EMPTY targets is HELD from
    # delivery until an audited human approves shipping empty strings. "0" restores the
    # pre-gate source-fill behavior. A settings key, NOT a features.py Feature (delivery is
    # core; the TM_LOCKS_ENABLED precedent), so MIN_FEATURES does not move. Scoped so
    # pure-LLM_AGENT and non-tm_match document partial deliveries are byte-identical
    # (LAW-TM-13/18). NO count pin covers settings DEFAULTS keys (§2.7).
    "tm_empty_delivery_gate": "1",  # hold TM-only empty deliveries for human approval
    # --- security / compliance (audit ON by default; the rest OFF — but a real
    #     install flips security_auth_required ON via security.seed_initial()) ---
    "egress_private_allowlist": "",
    "security_allowed_hosts": "",
    "trusted_proxy_ips": "",
    # #411: sign-in source networks exempt from the per-source failed-login budget
    # (SOURCE_FAIL_LIMIT), so a busy shared NAT (campus or office egress) is not refused
    # correct passwords. Comma list of IPs/CIDRs (IPv4 /8 or narrower, IPv6 /32 or narrower).
    # The per-(name, source) lockout and the account lockout still apply. Empty = none.
    "login_trusted_networks": "",
    "security_auth_required": "0",  # require login + enforce RBAC
    "session_idle_minutes": "240",  # sign out after this much inactivity (0 = off)
    # a11y T9 (maintainer decision 2): ABSOLUTE session limit in hours, counted from sign-in
    # (session["iat"]) and never renewed by activity or keepalive. 0 = no separate limit
    # (the 24 h session cookie window still ends an unused session). Default 24 (the maintainer
    # 09-24, a11y T11; was 20): longer than 20 hours, so the WCAG 2.2.1 20-hour exception
    # applies. Validated 0..24. Installs with a stored value keep it.
    "session_max_hours": "24",
    # Password policy for LOCAL accounts (HECVAT AAAI-03). Defaults follow NIST SP 800-63B;
    # an institution can tighten them. SSO/LDAP/SCIM/SAML accounts have no local password
    # and are unaffected. Read and enforced by app/services/password_policy.py only.
    # password_min_length allows 8 to 128 characters
    # password_require_classes needs 0 to 4 of lower, upper, digit, symbol
    # password_block_common turns on the common list plus email, username and product/company name
    # password_history refuses reuse of the last N passwords (0 to 24)
    # password_max_age_days of 0 never expires (NIST); otherwise change after N days
    "password_min_length": "12",  # pregate: allow-secret setting name
    "password_require_classes": "0",  # pregate: allow-secret setting name
    "password_block_common": "1",  # pregate: allow-secret setting name
    "password_history": "5",  # pregate: allow-secret setting name
    "password_max_age_days": "0",  # pregate: allow-secret setting name
    # Two-step sign-in (TOTP, RFC 6238; HECVAT AAAI-17 / DCTR-14) for accounts that sign in
    # with a password here (local and LDAP; SSO/SAML accounts use their identity provider's
    # MFA). "off" (default: existing installs unchanged; anyone may still enroll), "admins"
    # (every admin must use it), "all" (everyone who signs in with a password). A person who
    # must use it and has not set it up is sent to set it up right after the password step,
    # before any session exists. Read by app/services/mfa.py::policy only.
    "mfa_required": "off",
    # Maximum per-user API key age in days (#270 r2-apiauth-10). BLANK (default) = no
    # maximum: keys never expire unless minted with an expiry, and behaviour is
    # unchanged until an admin sets this. When set to a positive whole number, NEW
    # mints refuse a lifetime longer than the cap or a missing expiry (the clear
    # error names this setting); already-issued keys are never touched. Read by
    # app/services/apikeys.py::max_key_age_days at the one shared mint seam.
    "apikey_max_age_days": "",
    "project_scoping": "1",  # translators/reviewers/viewers see only their projects
    "sso_default_role": "translator",  # role granted to first-time OIDC sign-ins
    # identity-01: OIDC links an account by email (first sign-in, JIT creation or adopting
    # an existing account) only when the id_token says email_verified=true. "1" trusts
    # the configured issuer's email claim without that flag; only for an IdP whose
    # addresses are admin-assigned and that never sends email_verified (some Entra ID
    # setups). Default "0". Once linked, an account is matched by issuer + sub.
    "sso_trust_unverified_email": "0",
    "audit_enabled": "1",  # log every mutating action (ON by default)
    # Contention posture for the audit-row write (maintainer-ratified 2026-08-13; was the
    # audit._AUDIT_FAIL_OPEN module placeholder). "1" (default) = FAIL-OPEN: under sustained
    # SQLite writer-lock contention a shed audit write DROPS the row with one WARN and the
    # mutating request PROCEEDS unaudited (availability over audit completeness). "0" =
    # FAIL-CLOSED: a shed write raises db.WriteContentionError so the request surfaces an
    # honest retryable 503 rather than proceeding unaudited. Read by audit.fail_open(), which
    # snapshots it at the top of audit.log() so the degrade decision never issues a settings
    # read while the DB is under the very contention that triggered the degrade. Treated as
    # conservatively as audit_enabled: SENSITIVE (confirm-to-set) and REMOTE_IMMUTABLE (a
    # leaked admin key must not flip it to fail-open and then induce contention to open
    # unaudited-mutation windows). Set it locally via the Security page or `config set`.
    # #235 (2026-09 security review): a SERVER install (LOC_TMS_SERVER=1 or embedded)
    # defaults to FAIL-CLOSED ("0"); a desktop/dev install keeps the ratified fail-OPEN
    # default. Fail-closed is safe since the same train: the request middleware writes
    # the audit row for a signed-in state-changing request BEFORE the handler runs when
    # the posture is fail-closed (audit.prerecord_request), so a shed write refuses the
    # request with a retryable 503 and NOTHING changes. A stored row always wins.
    "audit_fail_open": "0" if config.IS_SERVER else "1",
    # Scheduled audit-chain verification cadence, in HOURS (gap G-10 / AU-05 monitoring).
    # Default "24" = the scheduler runs verify_chain() once a day (piggybacked on the existing
    # 30s tick, self-throttled — no new daemon), records the result on the audit trail
    # (audit.chain_verified on success, a FORCED audit.chain_broken + ERROR log on failure,
    # latched so a persistent break never spams a row), and exposes the health gauges. "0"
    # (or any non-positive value) turns the scheduled verification OFF — the chain stays
    # verifiable on demand via `audit verify` / the /audit "Verify chain" link. A NORMAL
    # operational cadence knob: NOT on REMOTE_IMMUTABLE_KEYS (see the G-13 policy call), but
    # SENSITIVE since #280 (2026-09 security review): "0" switches the daily check off, so a
    # write over /api or `config set` must be confirmed. If a future decision wants the
    # monitoring cadence host-only that is a one-line add to REMOTE_IMMUTABLE_KEYS below.
    # PERFORMANCE (F10): the sweep runs INLINE on the 30s scheduler tick — verify_chain walks
    # the whole log in bounded 500-row batches (constant memory) but O(n) CPU. On a very large
    # audit table (~250k+ rows) one sweep can exceed a single tick; future work is to run the
    # verification on the job queue / off-thread rather than inline on the tick.
    "audit_chain_verify_hours": "24",
    # --- remote administration master switch (SPEC-full-remote-parity Wave R2) ---
    # OFF by default ("0"). Gates the IRREVERSIBLE remote admin verbs on the /api
    # surface — GDPR erase, retention purge-now, apikey revoke-all (R2), and the
    # whole Tier-3 config/credential surface (R3). With it off, every one of those
    # routes answers 404 (the kill-switch law: a switched-off capability is
    # non-enumerable, not merely forbidden). SETTABLE ONLY LOCALLY: it is on
    # REMOTE_IMMUTABLE_KEYS below, so no /api config route (R3) may ever flip it —
    # the perimeter key stays on the host. A leaked admin key therefore cannot opt an
    # install into remote administration that never opted in itself. Never enable on
    # the real instance without an explicit decision (needs live-tenant QA).
    "remote_admin_enabled": "0",
    # --- embedded trust-host-auth mode (composable-core phase 0, Train 0b) ---
    # OFF by default ("0"). One half of a DOUBLE CONDITION with the boot-time env
    # flag LOC_TMS_EMBEDDED: when — and ONLY when — an install is BOTH embedded AND
    # this switch is "1", a TOKENLESS /api request resolves to a configured trust
    # principal (from env LOC_TMS_TRUST_PRINCIPAL) instead of the usual 401. It
    # exists for the single deployment shape locked in the composable-core decision
    # record: an embedded install sitting BEHIND a host application whose own
    # boundary already authenticates every caller, so per-request X-Loc-Token headers
    # would be redundant. A request that DOES carry a token is still verified exactly
    # as today (a bad token is still 401 — trust never launders a wrong token), and a
    # NON-embedded boot ignores this switch entirely (byte-identical behaviour).
    # SETTABLE ONLY LOCALLY: it is on REMOTE_IMMUTABLE_KEYS below, so no /api config
    # route (R3) may ever flip the auth perimeter over the very API it guards — a
    # leaked admin key can never opt an install into trusting unauthenticated callers.
    # Turning it on REQUIRES LOC_TMS_TRUST_PRINCIPAL (the boot fails loudly without
    # it); see app/services/trust_auth.py. Never enable outside the host-authenticated
    # embedded shape.
    "embedded_trust_host_auth": "0",
    # --- multi-org SaaS gate (the SACRED gate) ---
    # OFF by default ("0") = on-prem single-tenant, byte-identical to today: the
    # install itself is the tenant boundary, org predicates no-op, no cross-org
    # isolation machinery runs. "1" opts into SaaS multi-org isolation. NOTHING
    # reads this in P0 (schema + gate only) — it exists so later phases can guard
    # every new path behind access.multi_org_active(). Never flip on the real DB
    # without an explicit decision.
    "multi_org_enabled": "0",
    # Org the SHARED connector token ("lsk_...") is bound to in multi-org mode
    # (leak vector 6). The shared token carries no user, so it has no home org to
    # resolve; an operator pins it to exactly one org by id here. Blank (the
    # default) = UNBOUND: with multi_org_enabled ON the shared token is REJECTED on
    # org-scoped API operations (403) rather than silently seeing every org; with
    # the gate OFF this is irrelevant (single-tenant, no scoping — byte-identical to
    # today). A per-user API key never needs this (it carries its own home org).
    "connector_token_org_id": "",
    # SHA-256 hex hash of the SaaS in-process operator key (multi-org P6). When
    # multi_org_enabled is "1", an in-process CLI command that would sweep across
    # orgs (no --org scoping) must present --operator with an env LOC_TMS_OPERATOR_KEY
    # whose SHA-256 matches this hash. Blank (default) = no operator key configured:
    # an unscoped cross-org in-process command is REFUSED (exit 2) — a customer/cron
    # on a shared host can't silently sweep every org. Irrelevant with the gate OFF
    # (single-tenant, in-process CLI is local-root over the one org, byte-identical to
    # today). Set it via `config set operator_key_hash <sha256hex>` on the host.
    "operator_key_hash": "",
    "pii_redaction": "0",  # redact PII before sending to an engine
    # pii_ner_enabled: opt-in Presidio NER layer that ADDS unstructured entities (PERSON,
    # LOCATION, NRP, DATE_TIME, ...) to the always-on regex PII findings before content
    # reaches an engine (app/services/pii.py). Default OFF -> pii.detect/redact are
    # byte-identical to the regex-only path and nothing from presidio/spacy is imported.
    # Fully OFFLINE (local spaCy model, no network). HOST-ONLY (REMOTE_IMMUTABLE) + SENSITIVE
    # for the SAME reason as pii_redaction: a leaked admin key must not be able to WIDEN or
    # NARROW what PII is stripped before egress over the wire. pii_ner_spacy_model names the
    # local model the analyzer loads (a plain, non-sensitive Setting).
    "pii_ner_enabled": "0",
    "pii_ner_spacy_model": "en_core_web_lg",
    # pii_ner_entities: the Presidio entity types the NER layer may detect (comma list).
    # Default PERSON only -- DATE_TIME/LOCATION/ORGANIZATION are not PII in most regimes and
    # freezing them destroys legitimate translations ("Berlin" would never become "Berlin"'s
    # localized form). Widen deliberately (e.g. "PERSON,LOCATION"). Blank falls back to PERSON.
    "pii_ner_entities": "PERSON",
    "encrypt_uploads": "0",  # encrypt uploaded files at rest
    "encrypt_db": "0",  # retired vault switch; encryption now requires SQLCipher
    # identity (scaffolded — not wired to a real IdP yet)
    "sso_enabled": "0",
    "sso_provider": "oidc",
    "sso_issuer": "",
    "sso_client_id": "",
    "sso_client_secret": "",
    # LDAP / Active Directory (real search-then-bind auth; see
    # app/services/ldap_auth.py). Default OFF; ldap_default_role mirrors
    # sso_default_role (role granted to first-time LDAP sign-ins). The bind
    # (service-account) password is stored Fernet-encrypted under
    # ldap_bind_password_enc (never plaintext) — see get_secret/set_secret.
    "ldap_enabled": "0",
    "ldap_host": "",
    "ldap_ca_cert": "",
    "ldap_base_dn": "",
    "ldap_bind_dn": "",
    "ldap_bind_password_enc": "",
    "ldap_user_filter": "",
    "ldap_default_role": "translator",
    # --- SCIM 2.0 provisioning server (Train I) -------------------------------------
    # scim_enabled: the opt_in feature flag (app/modules.py). DEFAULT OFF. HOST-ONLY
    # (REMOTE_IMMUTABLE below) — turning on directory provisioning is a trust-posture
    # change a leaked admin key must never make over /api. While off, every /scim/v2/*
    # path 404s (SCIM error shape, non-enumerable) and the `scim` CLI verbs refuse.
    # Auth is the EXISTING ApiKey path with a new "scim" scope (apikeys.ALL_SCOPES);
    # the key's owner must be an active admin. No new credential mechanism.
    "scim_enabled": "0",
    # scim_default_role: the role stamped on a SCIM-provisioned account at CREATE.
    # Role mapping via SCIM is NOT in v1 (enterprise role mapping is a policy decision),
    # so PUT/PATCH never change a user's role — it is only ever set here at create time.
    "scim_default_role": "translator",
    # --- SAML 2.0 SP single sign-on (Train I, BETA) ---------------------------------
    # saml_enabled: the opt_in feature flag. DEFAULT OFF. HOST-ONLY (REMOTE_IMMUTABLE).
    # While off, every /auth/saml/* route 404s and the login button is hidden. The
    # python3-saml (OneLogin) library is an OPT-IN extra (requirements-dev / extras,
    # NOT base requirements — it needs xmlsec native bits); a lazy-import capability
    # check fails LOUD if the flag is on but the library is missing (the s3_blobstore
    # pattern).
    "saml_enabled": "0",
    # The IdP's metadata XML, pasted (offline-friendly). A metadata-URL fetch is a
    # convenience only; the pasted XML is the authority.
    "saml_idp_metadata_xml": "",
    # The SP entityId (audience). Blank => derived from the install base URL.
    "saml_sp_entity_id": "",
    # The SP ACS URL. Blank => derived from the install base URL (/auth/saml/acs).
    "saml_sp_acs_url": "",
    # Role stamped on a first-time SAML sign-in (validated against Role.ALL).
    "saml_default_role": "translator",
    # Refuse IdP-initiated SSO unless explicitly opted in (default REFUSE — SP-initiated
    # with InResponseTo correlation is the safe posture; IdP-initiated has no request to
    # correlate against and is a documented replay/forgery surface).
    "saml_allow_idp_initiated": "0",
    # Optional SP signing material (for signing the AuthnRequest / decrypting assertions).
    # The private key is Fernet-encrypted at rest (SECRET_SETTING_KEYS), mirroring
    # ldap_bind_password_enc; the cert is public. Blank => the SP does not sign requests
    # (validation of the INCOMING assertion signature is always required regardless).
    "saml_sp_x509_cert": "",
    "saml_sp_private_key_enc": "",
    # outbound SMTP for invitation emails (optional)
    "smtp_host": "",
    "smtp_port": "587",
    "smtp_user": "",
    "smtp_password": "",
    "smtp_from": "",
    "smtp_tls": "1",
    # --- connector OAuth provider apps (Connector OAuth plan, chunk 3) ---
    # Per-provider OAuth client credentials, registered ONCE per install (like
    # sso_client_*). Blank by default so suites + fresh installs are unaffected and
    # connectors keep using API-token auth; the "Connect with OAuth" option only
    # appears once a client_id is set. Jira + Confluence share ONE Atlassian 3LO
    # app, hence a single 'atlassian' pair (not per-connector). The client_secret
    # follows the sso_client_secret masked pattern (empty submit never blanks it).
    "oauth_airtable_client_id": "",
    "oauth_airtable_client_secret": "",
    "oauth_atlassian_client_id": "",
    "oauth_atlassian_client_secret": "",
    # --- Vendor Studio SSO handoff (VP-0 / SSO train) ---
    # The base URL of the Vendor Studio companion app, and the shared HMAC secret its
    # SSO handoff token is signed with. Both are HOST-ONLY (env/CLI `config set`; never
    # settable over /api — see REMOTE_IMMUTABLE_KEYS): the secret mints auth tokens into
    # another app, and the URL is where a live token is POSTed. Blank (default) = the
    # "Open Vendor Studio" button is hidden and POST /vendor-studio/open 404s. The secret
    # is Fernet-encrypted at rest (SECRET_SETTING_KEYS). Either may be supplied by an env
    # var (VENDOR_STUDIO_URL / VENDOR_STUDIO_SSO_SECRET) which takes precedence.
    "vendor_studio_url": "",
    "vendor_studio_sso_secret": "",
    # --- Comprehensive data-export (full object-graph dump, Batch 13) --------------
    # data_export_enabled: the WRITES-OFF kill switch (D3/LAW9). DEFAULT ON ("1") -- a
    # confined dump of what the caller can already see is a READ, not a new egress, so
    # there is NO dormant flag; this always-on switch only gates the WRITE path (job
    # submission + staging) while reads/enumeration of already-staged artifacts stay up.
    # HOST-ONLY (REMOTE_IMMUTABLE below) so a leaked admin key cannot silence the feature
    # install-wide, but default-ON so the wire path is live (LAW10). The Module record +
    # feature-flag registration (writes_off) land in Train B; Train A only needs the
    # service-layer switch here.
    "data_export_enabled": "1",
    # custom_fields_enabled: the WRITES-OFF kill switch for custom project fields (default ON).
    "custom_fields_enabled": "1",
    # Retention TTL for a staged artifact, in hours (the reaper's cutoff; Train C sweeps
    # it). Short (3d) because artifacts are staged PLAINTEXT at rest.
    "data_export_retention_hours": "72",
    # Hard ceiling on one artifact's streamed size; an over-cap run finishes `partial`
    # with an error note (never a silent truncation). 5 GiB.
    "data_export_max_bytes": "5368709120",
    # Per-scope cap on concurrently-staged artifacts; a request past it 409s until the
    # reaper frees a slot (Train B/C enforce; the guardrail value lives here).
    "data_export_max_active_artifacts": "20",
    # Download-lease grace: the reaper SKIPS a blob touched within this window so a
    # Range-resumed download is never unlinked mid-stream -- bounded by a HARD ceiling
    # measured from expires_at (DX-16/DX-206), so rolling Range requests cannot pin it.
    "data_export_download_grace_minutes": "30",
    # REFUSE-UNLESS-OPTED-IN on encrypted installs (the maintainer's locked call, DX-208). Plaintext
    # EXPORTS_DIR staging is the normal mode, BUT on an install with encrypt_uploads=1 OR
    # encrypt_db=1 an export is REFUSED at submission unless this is "1" (or S3 staging is
    # configured, which is encrypted in transit + bucket-SSE). SENSITIVE (confirm-on-write)
    # AND HOST-ONLY (REMOTE_IMMUTABLE) below -- acknowledging plaintext-at-rest is a
    # host-operator act, never a remote one.
    "data_export_allow_plaintext_staging": "0",
    # Signed-URL TTL (seconds) for an S3-staged export artifact (Train C, DX-06/§8.3). When the
    # ExportStore adapter is S3 (blobstore_s3_enabled=1), the artifact route 302s the caller to a
    # Range-capable presigned GET scoped to the ONE key with this lifetime. Inert on the default
    # DiskExportStore (presign_get returns None -> served locally over open_range/206). 15 min.
    "data_export_signed_url_ttl": "900",
    # S3-compatible BlobStore adapter (composable-core phase 3, SPEC-s3-blobstore §5.2).
    # DORMANT by default: blobstore_s3_enabled="0" => the adapter is never registered and
    # DiskBlobStore (the uploads/ store) stays the default + sole adapter; boto3 is never
    # imported. The enable switch + bucket + endpoint_url are HOST-ONLY (REMOTE_IMMUTABLE
    # below) -- a data-topology / redirect surface an admin key must never move over /api.
    # region + prefix stay settable (region cannot point at attacker infra; prefix stays
    # inside the fixed bucket). Credentials are env-only (config.S3_* ), never a Setting.
    # prefix is normalized + validated once at boot (M8): trailing '/' appended when
    # non-empty; leading '/', '..' segments, and control chars are REJECTED loudly.
    "blobstore_s3_enabled": "0",
    "blobstore_s3_bucket": "",
    "blobstore_s3_prefix": "",
    "blobstore_s3_region": "",
    "blobstore_s3_endpoint_url": "",
    # Credential-resolution mode (phase-3.1, SPEC §7). "static" (default) = credentials come
    # from the host env keys config.S3_* (byte-identical to phase-3; an enabled boot with no
    # static keys FAILS LOUD). "chain" = build the boto3 client with NO explicit credentials so
    # botocore's DEFAULT chain resolves them (env -> shared config -> ECS task role -> EC2
    # instance profile / EKS IRSA web-identity) -- role-based creds for a containerized / EKS
    # deployment that keeps no long-lived keys on the host. Chain mode PRESERVES the fail-loud
    # posture: configure_at_boot verifies botocore actually resolved credentials at boot and
    # fails the boot (scrubbed S3BlobStoreError) if not -- never a silent 401-at-request-time,
    # never a silent fallback to disk (§8). Any value other than static|chain fails the boot
    # loudly. HOST-ONLY (REMOTE_IMMUTABLE below): it is a credential-provenance switch with no
    # legitimate remote use case, and a remote caller could store an invalid value with HTTP 200
    # and brick the next boot -- so no /api config route may ever move it (round-1 gate verdict).
    "blobstore_s3_credentials": "static",
    # --- Observability: Prometheus /metrics + OpenTelemetry (sprint Train Q2) -------
    # TWO INDEPENDENT, DEFAULT-OFF switches wired at boot exactly like the S3 adapter
    # (app/services/observability.py). All dormant by default: a default install
    # imports neither prometheus_client nor opentelemetry (both opt-in extras, NOT in
    # base requirements.txt) and exposes no /metrics endpoint.
    #   metrics_enabled: "1" mounts an AUTH-REQUIRED GET /metrics (prometheus text) +
    #     an HTTP request count/duration middleware + provider/job counters hooked into
    #     the usage + job seams. On + prometheus_client missing = fail-loud boot. The
    #     /metrics endpoint requires an ADMIN-SCOPED user API key (admin scope + admin
    #     role, mirroring /api/admin/config; 401 unkeyed, 403 for a non-admin token) --
    #     an open metrics endpoint leaks tenant activity. HOST-ONLY (REMOTE_IMMUTABLE):
    #     a boot-time install-topology switch, like blobstore_s3_enabled.
    #   otel_enabled + otel_endpoint: "1" instruments the FastAPI app with an OTLP/HTTP
    #     span exporter to otel_endpoint (e.g. http://collector:4318/v1/traces). On +
    #     empty endpoint = fail-loud boot (a no-op exporter that drops every span is
    #     refused); on + opentelemetry missing = fail-loud boot. Both HOST-ONLY: the
    #     endpoint is an EGRESS redirect surface (spans go there), so a leaked admin key
    #     must never point tracing at attacker infra -- the same posture as
    #     blobstore_s3_endpoint_url / vendor_studio_url.
    "metrics_enabled": "0",
    "otel_enabled": "0",
    "otel_endpoint": "",
    # --- durable job queue backend (sprint Train S, STRETCH) ------------------------
    # Selects HOW background jobs are dispatched to the worker threads, wired at boot
    # behind the JobQueue port (app/ports/jobqueue.py) exactly like the S3 adapter
    # (flag-check-first, fail-loud, strict no-op when default).
    #   "memory" (DEFAULT) = the in-memory queue.Queue the app has always used: jobs are
    #     enqueued to a per-process queue and lost on restart (recover_interrupted then
    #     re-enqueues QUEUED rows + fails the RUNNING ones, visible not silent). This is
    #     byte-identical to pre-Train-S behavior -- the InMemoryJobQueue adapter wraps the
    #     same queue.Queue, so a default install dispatches jobs exactly as before.
    #   "db" = a DURABLE, DB-backed queue (the `jobqueue` table). Jobs survive a process
    #     restart, are CLAIMED via an atomic compare-and-swap UPDATE...WHERE state='pending'
    #     (so two workers -- even across nodes, the HA seam -- can never double-claim one
    #     job), and a RUNNING job whose worker died (its lease expired) returns to pending
    #     on recovery rather than being failed. On + the `jobqueue` table missing = a
    #     fail-loud boot (JobQueueError naming `alembic upgrade head`), never a silent
    #     degrade. Any value other than memory|db fails the boot loudly.
    # HOST-ONLY (REMOTE_IMMUTABLE below): this is an install-TOPOLOGY switch -- it decides
    # how this install dispatches work (single-process ephemeral vs durable/HA) -- so like
    # blobstore_s3_enabled / metrics_enabled a leaked admin key must never flip it over
    # /api. Set it on the host with `config set job_queue_backend db` or the Settings page.
    "job_queue_backend": "memory",
    # --- MCP OAuth authorization server (#753, docs/architecture/mcp-oauth.md) ----------
    # OFF by default. When on, this install is the OAuth 2.1 authorization server for its own
    # remote MCP server (integrations/mcp-server, the resource server): RFC 8414 metadata,
    # authorization code + PKCE S256 with a consent page, Client ID Metadata Documents,
    # audience-bound tokens. All five keys are host-only (REMOTE_IMMUTABLE_KEYS) and confirm to
    # set; the per-user lsu_ API keys are untouched by any of them.
    "mcp_oauth_enabled": "0",
    "mcp_oauth_resource": "",  # canonical MCP URL, e.g. https://mcp.example.com/mcp
    "mcp_oauth_dcr_enabled": "0",  # deprecated dynamic client registration, for older clients
    "mcp_oauth_cimd_hosts": "",  # blank = any public https client metadata host
    "mcp_oauth_rs_secret_hash": "",  # SHA-256 of the resource-server secret (never the secret)
}


# ============================================================================== #
# LOCAL-ONLY SETTINGS (SPEC-full-remote-parity, the perimeter-key law)
# ============================================================================== #
#: Settings keys that a REMOTE caller must NEVER be able to write, whatever scope
#: or role its API key carries. There is no /api config route yet (that is Wave R3),
#: but this list is the CONTRACT R3's POST /api/admin/config must honour: it MUST
#: refuse any key named here, always, with no confirm override — so the R3 route
#: cannot become a back door to the very switch that gates it.
#:
#: `remote_admin_enabled` is the master remote-administration opt-out. If a remote
#: caller could set it, a leaked admin key could turn remote admin ON and then reach
#: every Tier-3 verb — defeating the whole point of the switch. It stays a host-only
#: decision (config set / the Settings page), exactly like `multi_org_enabled` and
#: the operator key, which no key of any kind reaches either.
#:
#: R3 EXPANSION (SPEC-full-remote-parity Wave R3, "the immutable-keys law"): the
#: POST /api/admin/config route can set almost any setting for an admin, so the set
#: of keys it must NEVER touch — whatever scope, role or confirm the caller carries —
#: is the SACRED GATE set, each one catastrophic to flip from off the host:
#:   * remote_admin_enabled  — a remote key must never widen its OWN reach (the
#:                             perimeter switch that gates this very surface);
#:   * security_auth_required — flipping this OFF tears down the login wall / RBAC
#:                             for the whole install remotely;
#:   * multi_org_enabled     — the tenancy gate; flipping it reshapes isolation for
#:                             every org at once;
#:   * encrypt_db / encrypt_uploads — at-rest encryption toggles with data-loss and
#:                             lock-out implications (a remote flip could strand the
#:                             DB/uploads behind a key the caller does not hold, or
#:                             expose them);
#:   * operator_key_hash     — the SaaS cross-org operator credential's hash; setting
#:                             it remotely would let a leaked admin key install its
#:                             own operator key and then sweep every org.
#: The config route refuses ANY key here with a clean 4xx naming it local-only, with
#: no confirm override — so the route can never become a back door to the switches
#: that gate (or would un-gate) it. Settable ONLY locally (`config set` / Settings).
REMOTE_IMMUTABLE_KEYS: frozenset = frozenset(
    {
        # #753: the MCP OAuth authorization server opens a new way to obtain /api access
        # (consented OAuth tokens). Who may run it, which resource it binds tokens to and
        # which client hosts it trusts are host decisions, never a remote key's.
        "mcp_oauth_enabled",
        "mcp_oauth_resource",
        "mcp_oauth_dcr_enabled",
        "mcp_oauth_cimd_hosts",
        "mcp_oauth_rs_secret_hash",
        # Ingress trust is host-only: a leaked admin key must neither shut every
        # network door nor forge audit identity by widening proxy trust.
        "egress_private_allowlist",
        "security_allowed_hosts",
        "trusted_proxy_ips",
        "remote_admin_enabled",
        "security_auth_required",
        # Train 0b (composable-core): the embedded trust-host-auth perimeter switch. Turning
        # it ON makes tokenless /api requests resolve to a trusted principal — the single
        # most reach-widening flip on the whole surface. It must NEVER be settable over the
        # very /api it guards (a leaked admin key that could flip it would opt the install
        # into trusting unauthenticated callers and then walk straight in). Host-only,
        # exactly like security_auth_required — the two together define who /api trusts.
        "embedded_trust_host_auth",
        "multi_org_enabled",
        "encrypt_db",
        "encrypt_uploads",
        "operator_key_hash",
        # R3 review fold-in #1 (forensics/PII-safety are host-only): a leaked admin key
        # must not be able to silence the audit trail install-wide, nor turn off PII
        # redaction before sending content to an engine. Both are SENSITIVE (confirm-per-
        # key) below too, but confirm is not enough for a tear-down of forensics/privacy —
        # like security_auth_required, flipping these off is a HOST-ONLY act.
        "audit_enabled",
        # audit_fail_open rides with audit_enabled: a leaked admin key must not be able to flip
        # the trail to fail-OPEN and then induce write contention to open unaudited-mutation
        # windows (a contention-flavoured version of "silence the audit trail install-wide").
        # Conservative default; relaxing this to allow remote flipping is a one-line removal.
        "audit_fail_open",
        "pii_redaction",
        # pii_ner_enabled rides with pii_redaction: it governs how much PII is stripped before
        # content reaches an engine, so a leaked admin key must not be able to widen/narrow the
        # redaction surface over /api (host-only, exactly like pii_redaction). pii_ner_spacy_model
        # is host-only too: a remote flip to a not-installed model name BOOT-BRICKS the NER layer
        # (fail-loud) and repoints which local model runs -- a config change with no /api business.
        # pii_ner_entities governs the SAME egress surface: it decides which entity TYPES the NER
        # layer strips, so a remote flip to (e.g.) "DATE_TIME" would silently STOP PERSON-name
        # redaction before content reaches a cloud engine -- the exact widen/narrow threat above,
        # host-only for the same reason as its two siblings (round-2 gate finding).
        "pii_ner_enabled",
        "pii_ner_spacy_model",
        "pii_ner_entities",
        # VP-0 / SSO train: the Vendor Studio handoff URL + shared SSO secret are HOST-ONLY.
        # The secret mints bearer tokens that authenticate into the Vendor Studio app, and the
        # URL is where a live token is POSTed; a leaked admin key must never be able to point
        # the handoff at an attacker-controlled Studio, nor install its own signing secret.
        # (Deliberate simplest-compliant choice: no /api route mints or configures these; they
        # are set on the host with `config set` or a Fly secret env var.)
        "vendor_studio_url",
        "vendor_studio_sso_secret",
        # `databricks_export_enabled`: the data-EGRESS master switch of the (paid) Databricks
        # exporter. It is host-only for the same reason `remote_admin_enabled` is -- a leaked
        # admin key must never be able to turn on egress of source content + translations.
        "databricks_export_enabled",
        # S3-compatible BlobStore adapter (composable-core phase 3, SPEC-s3-blobstore §5.5/M7):
        # FOUR host-only members. `blobstore_s3_enabled` is a data-TOPOLOGY switch (it decides
        # where the install's blobs physically live), so like the egress switches above a leaked
        # admin key must never flip it over /api. `blobstore_s3_bucket` and
        # `blobstore_s3_endpoint_url` are the REDIRECT surface: on an install where the switch is
        # already on, the env credentials live in the running process, so a remote flip of the
        # bucket or endpoint would redirect every future blob write to attacker-chosen storage
        # using the process's own creds -- the same "an admin key must not move where data goes"
        # class. `blobstore_s3_credentials` (phase-3.1, static|chain) is a credential-PROVENANCE
        # switch: it has no legitimate remote use case, and a remote caller could store an invalid
        # value with HTTP 200 and brick the NEXT boot (an unknown mode fails the boot loudly) --
        # so it is host-only too (round-1 gate verdict). `blobstore_s3_region` and
        # `blobstore_s3_prefix` STAY settable (region selects an AWS region, not an endpoint;
        # prefix stays within the already-fixed bucket). The verify_r3 / REMOTE_IMMUTABLE suite
        # iterates this frozenset, so the four members are covered by construction -- no per-key
        # test edit.
        "blobstore_s3_enabled",
        "blobstore_s3_bucket",
        "blobstore_s3_endpoint_url",
        "blobstore_s3_credentials",
        # Comprehensive data-export (Batch 13, Train A): TWO host-only members.
        # `data_export_enabled` is the WRITES-OFF kill switch -- host-only for the same
        # reason `remote_admin_enabled` is (a leaked admin key must never silence the
        # feature install-wide), yet default-ON so the wire path stays live (LAW10).
        # `data_export_allow_plaintext_staging` acknowledges plaintext-at-rest staging on
        # an encrypted install (DX-208): a security-posture acknowledgement a remote caller
        # must never make -- host-only, exactly like encrypt_uploads/encrypt_db themselves.
        # The verify_r3 / REMOTE_IMMUTABLE suite iterates this frozenset, so both members
        # are covered by construction -- no per-key test edit.
        "data_export_enabled",
        "data_export_allow_plaintext_staging",
        # Observability (sprint Train Q2): THREE host-only members. `metrics_enabled` is a
        # boot-time install-topology switch (it decides whether this install exposes a
        # metrics endpoint at all), so like blobstore_s3_enabled a leaked admin key must
        # never flip it over /api. `otel_enabled` + `otel_endpoint` are the tracing EGRESS
        # surface: the endpoint is where every request span is POSTed, so a remote flip
        # could redirect trace telemetry to attacker-chosen infra -- the same "an admin key
        # must not move where data/telemetry goes" class as blobstore_s3_endpoint_url /
        # vendor_studio_url. The verify_r3 / REMOTE_IMMUTABLE suite iterates this frozenset,
        # so the three new members are covered by construction -- no per-key test edit.
        "metrics_enabled",
        "otel_enabled",
        "otel_endpoint",
        # Train S (STRETCH): the durable-job-queue backend selector is HOST-ONLY. It is a
        # boot-time install-TOPOLOGY switch (it decides whether this install dispatches
        # background work through an ephemeral in-memory queue or a durable/HA DB queue),
        # so like metrics_enabled / blobstore_s3_enabled a leaked admin key must never flip
        # it over /api -- and a remote flip to "db" on an install whose `jobqueue` table is
        # absent would brick the NEXT boot (fail-loud). The verify_r3 / REMOTE_IMMUTABLE
        # suite iterates this frozenset, so the new member is covered by construction.
        "job_queue_backend",
        # Train I: the SCIM + SAML master switches are HOST-ONLY for the same reason
        # sso_enabled's siblings are — turning on directory provisioning (SCIM can
        # create/deactivate accounts) or a new federated login path (SAML) is a
        # trust-posture change a leaked admin key must never make over the very /api it
        # would then reach. The verify_r3 / REMOTE_IMMUTABLE suite iterates this
        # frozenset, so the two new members are covered by construction.
        "scim_enabled",
        "saml_enabled",
        # Enterprise turn-on UX, GATE ROUND 1 probe A (PROBE-PROVEN). Train I locked the
        # SAML/SCIM master SWITCHES but left SAML's TRUST MATERIAL remotely writable, because
        # at the time `saml_idp_metadata_xml` "existed in no template" (the saml.py:132 hole)
        # and looked like inert config. It is not inert: it IS the trust anchor. saml.py's
        # build_settings() feeds it to python3-saml as the idp block -- entityId, SSO URL AND
        # THE x509 SIGNING CERT that every assertion is validated against. On an install where
        # SAML is already on (the install this panel exists to create), an admin-scoped API key
        # on a remote_admin_enabled host could POST /api/admin/config {saml_idp_metadata_xml:
        # <attacker IdP>} -> HTTP 200, and then mint a self-signed assertion for any user and
        # log in as them. A COMPLETE AUTH BYPASS over the very /api the key already reached --
        # the exact class scim_enabled/saml_enabled were locked for, one field deeper. The SP
        # signing/decryption PAIR joins it: the same trust surface, and the panel gates all
        # three behind the operator key, so leaving them wire-writable would be two doors with
        # two auth stories -- the thing this train exists to end.
        "saml_idp_metadata_xml",
        "saml_sp_x509_cert",
        "saml_sp_private_key_enc",
        # ...and the S3 prefix, for the OTHER reason already recorded above for
        # `blobstore_s3_credentials`: a remote caller can store a value that returns HTTP 200
        # and BRICKS THE NEXT BOOT. _normalize_prefix REFUSES a leading '/', a '..' segment, an
        # empty '//' segment and control chars -- every one of which is a loud S3BlobStoreError
        # at boot on an S3-enabled install. The note above ("prefix stays within the
        # already-fixed bucket") is right about data redirection and silent about the boot, so
        # it was reasoning from the wrong threat. `blobstore_s3_region` DELIBERATELY STAYS
        # settable: it selects an AWS region, is never boot-validated, and cannot brick a boot.
        "blobstore_s3_prefix",
        # Wave C guarded purge (gate round-3 MED-2): the reconciler grace window governs a
        # DATA-DELETION safety guard -- the window in which a staged raw file is protected from a
        # cross-process reconcile that would otherwise resurrect (or, at 0, is exposed to) it. Its
        # sibling `prompt_tap_envelopes_enabled` is refused at EVERY settings door (the ceremony
        # is the only writer), so a leaked admin key must not be able to shorten this guard to 0
        # over the very /api it would then race. Host-only, exactly like the other safety keys;
        # settable ONLY locally (`config set` / Settings). The verify_r3 / REMOTE_IMMUTABLE suite
        # iterates this frozenset, so the member is covered by construction.
        "ledger_purge_reconcile_grace_seconds",
    }
)

# ============================================================================== #
# SENSITIVE + SECRET SETTING KEYS (the shared seam BOTH the config CLI and the R3
# /api config route call — one source of truth, per the web+CLI+api parity rule).
# ============================================================================== #
#: Security-sensitive keys: flipping one changes the trust posture of the whole
#: install, so a WRITE must be CONFIRMED (CLI `config set` prompts unless --yes; the
#: R3 POST /api/admin/config route requires `confirm: true` PER KEY, else a clean
#: 400). This frozenset is the ONE SOURCE OF TRUTH: app/cli/cmd_config.py DERIVES its
#: `_SENSITIVE_KEYS` from it (imported lazily through the kernel facade so CLI
#: registration stays free of an app.services/app.db import), so the two can never
#: diverge and one surface can never skip a confirm the other enforces (verify_cli
#: pins the identity). The install-posture switches that are OUTRIGHT immutable over
#: /api live in REMOTE_IMMUTABLE_KEYS above and are refused before this set is even
#: consulted.
SENSITIVE_KEYS: frozenset = frozenset(
    {
        # #753: the MCP OAuth authorization server switches (also REMOTE_IMMUTABLE above).
        "mcp_oauth_enabled",
        "mcp_oauth_resource",
        "mcp_oauth_dcr_enabled",
        "mcp_oauth_cimd_hosts",
        "mcp_oauth_rs_secret_hash",
        "security_auth_required",
        "egress_private_allowlist",
        "security_allowed_hosts",
        "trusted_proxy_ips",
        "login_trusted_networks",
        "project_scoping",
        "audit_enabled",
        "audit_fail_open",
        "pii_redaction",
        "pii_ner_enabled",
        "encrypt_uploads",
        "encrypt_db",
        "sso_enabled",
        "ldap_enabled",
        # Train I: turning directory provisioning / SAML SSO on or off changes the
        # install's trust posture, so a WRITE must be CONFIRMED (mirrors sso_enabled).
        "scim_enabled",
        "saml_enabled",
        # Batch 13 (data-export, DX-208): acknowledging PLAINTEXT-at-rest export staging on
        # an encrypt_uploads/encrypt_db install is a security-posture change, so a WRITE must
        # be CONFIRMED (mirrors encrypt_uploads). Also REMOTE_IMMUTABLE above (host-only).
        "data_export_allow_plaintext_staging",
        # Batch 15 T2 (B-MED-4): DISARMING the TM-only delivery empty gate lets a
        # tm_match deliverable ship untranslated (source-filled) content WITHOUT a human
        # approval, so a WRITE must be CONFIRMED -- otherwise a remote MCP agent could flip
        # it via manage_install(set_config) and then pull the source-filled file (disarm
        # rather than approve). Mirrors the other posture toggles above.
        "tm_empty_delivery_gate",
        # TOTP train (HECVAT AAAI-17 / DCTR-14): who must use two-step sign-in changes the
        # install's trust posture, so a WRITE must be CONFIRMED (mirrors security_auth_required).
        "mfa_required",
        # #280 (2026-09 security review): every remaining key that matches
        # SECURITY_KEY_PATTERN below. verify_settings_security_lists fails when a new key
        # matches the pattern but is in neither this set nor REMOTE_IMMUTABLE_KEYS.
        # Retention arms the install-wide purge daemon (enabled=1 + days=1 deletes every
        # document older than a day), so arming it or shortening the window is confirmed,
        # exactly like the purge-now verb's confirm:"purge".
        "retention_enabled",
        "retention_days",
        # "0" turns the scheduled audit-chain check off.
        "audit_chain_verify_hours",
        # Session limits and the local password policy (HECVAT AAAI-03): loosening one
        # weakens every account at once.
        "session_idle_minutes",
        "session_max_hours",
        "password_min_length",  # pregate: allow-secret setting name
        "password_require_classes",  # pregate: allow-secret setting name
        "password_block_common",  # pregate: allow-secret setting name
        "password_history",  # pregate: allow-secret setting name
        "password_max_age_days",  # pregate: allow-secret setting name
        # Identity trust material. The issuer, directory host, CA bundle and filters decide
        # WHO may sign in and which server receives LDAP passwords; a hostile value mints
        # sessions or captures passwords. The *_default_role keys accept "admin", which
        # would make every new SSO/LDAP/SCIM/SAML account an administrator.
        "sso_provider",
        "sso_issuer",
        "sso_client_id",
        "sso_client_secret",
        "sso_default_role",
        "sso_trust_unverified_email",
        "ldap_host",
        "ldap_ca_cert",
        "ldap_base_dn",
        "ldap_bind_dn",
        "ldap_bind_password_enc",
        "ldap_user_filter",
        "ldap_default_role",
        "scim_default_role",
        "saml_sp_entity_id",
        "saml_sp_acs_url",
        "saml_default_role",
        "saml_allow_idp_initiated",
        # Gate round 1 L4: where mail, content and links go, who the shared connector token
        # acts for, and the request throttle. smtp_host receives every reset mail (the
        # ldap_host reasoning); app_base_url builds reset/invite links and the SAML ACS
        # default (the twin of saml_sp_acs_url); local_base_url and the SearXNG URL receive
        # content; connector_token_org_id binds the shared token to an org; 0 turns
        # api_rate_limit_per_minute off; allow_mock_egress and the retention windows.
        "smtp_host",
        "app_base_url",
        "local_base_url",
        "connector_token_org_id",
        "api_rate_limit_per_minute",
        "api_read_rate_limit_per_minute",
        # #270: the API key max age is a security cap; lifting it confirms like the rate limits.
        "apikey_max_age_days",
        "allow_mock_egress",
        "data_export_retention_hours",
        "key_changelog_retention_days",
    }
)

#: #280 ratchet: a settings key whose NAME matches this pattern is security-relevant and
#: MUST be in SENSITIVE_KEYS (confirm to set) or REMOTE_IMMUTABLE_KEYS (host-only).
#: tests/verify_audit_settings_lock.py [6] walks DEFAULTS against it, so a new key such as
#: `sso_foo`, `oidc_bar_default_role`, `x_retention_days` or `y_base_url` cannot ship with
#: neither lock.
SECURITY_KEY_PATTERN = re.compile(
    r"^(security_|audit_|sso_|oidc_|ldap_|saml_|scim_|mfa_|pii_|encrypt_|egress_"
    r"|trusted_|remote_admin|multi_org|operator_|embedded_trust|session_|password_"
    r"|project_scoping)"
    r"|_default_role$|pii|allowed_hosts|allowlist"
    r"|retention|_base_url$|smtp_host|rate_limit|token_org|mock_egress"
)

#: Install-wide keys outside the other security groups that org admins must not write.
#: These include the shared companion-app credential and the single global spend brake.
ORG_ADMIN_REFUSED_EXTRA: frozenset = frozenset(
    {
        "connector_api_token",
        "spend_cap_usd",
        "spend_period",
        "assist_calls_per_minute",
        "assist_calls_per_day",
        # #424: the custom SRX ruleset is one install file every org ingests with.
        "srx_enabled",
        "srx_file",
        # #426: install-wide shared credentials. The mail transport and the connector OAuth
        # apps serve every org (smtp_host and every *_base_url were already covered above).
        "smtp_port",
        "smtp_tls",
        "smtp_user",
        "smtp_password",
        "smtp_from",
        "oauth_airtable_client_id",
        "oauth_airtable_client_secret",
        "oauth_atlassian_client_id",
        "oauth_atlassian_client_secret",
        # #479: the enabled-language catalog is one list every org's pickers read.
        "enabled_locales",
        # #538: raw prompt and response capture is one install-wide switch over one shared
        # file; an org admin must not turn it on for every org.
        "prompt_tap_enabled",
    }
)

#: Gate round 1 L3: True while a web or /api request is being handled (set by
#: app/main.py::_security_audit). set_many then refuses an install-wide security key while
#: multi-org is active, so EVERY request door (a new route included) is covered by one
#: list-driven check; host doors (CLI, scheduler, jobs) never set it. The operator-key
#: enterprise door opts out with host_operator_scope().
REQUEST_DOOR: ContextVar = ContextVar("settings_request_door", default=False)


class InstallWideRefused(PermissionError):
    """A request tried to write an install-wide security key under multi-org (#281)."""

    def __init__(self, keys):
        self.keys = sorted(keys)
        super().__init__(
            "Install-wide security settings apply to every organization, so under multi-org "
            "they are a host-operator action. Set them from the CLI on the host: "
            "`config set --help`. Refused key(s): " + ", ".join(self.keys) + "."
        )


@contextlib.contextmanager
def host_operator_scope():
    """Mark a block as a host-operator write (the operator-key step-up door) so the
    request-door multi-org refusal in set_many does not apply to it."""
    token = REQUEST_DOOR.set(False)
    try:
        yield
    finally:
        REQUEST_DOOR.reset(token)


#: #220: the two keys that decide whether (and how reliably) the audit trail records
#: anything. A change to either is ALWAYS recorded with a forced audit row (set_many emits
#: audit.logging_disabled / audit.logging_enabled / audit.fail_open_changed; every door
#: also forces its own row), so turning the trail off can never go unrecorded.
AUDIT_POSTURE_KEYS: frozenset = frozenset({"audit_enabled", "audit_fail_open"})


def security_setting_keys() -> frozenset:
    """Every install-wide security setting: the confirm set, the host-only set and the
    shared connector token. Under multi-org these are host-only on every web and /api door
    (#281)."""
    return SENSITIVE_KEYS | REMOTE_IMMUTABLE_KEYS | ORG_ADMIN_REFUSED_EXTRA


def audit_off_is_host_only() -> bool:
    """#220: True when turning audit logging OFF is a host-only act, refused on the web
    Security page (REMOTE_IMMUTABLE_KEYS already refuses it over /api and MCP). That is any
    server install (LOC_TMS_SERVER / embedded) and any install that requires sign-in, which
    is every hosted and every multi-user install (HECVAT AAAI-09/AAAI-11: audit logging is
    always on). A single-user desktop install with sign-in off may still switch it from the
    page. The host operator keeps `config set audit_enabled 0` either way, and that write
    is itself recorded (forced row)."""
    return bool(config.IS_SERVER) or get("security_auth_required") == "1"


#: Setting keys whose VALUE is a true secret and MUST be stored Fernet-encrypted at
#: rest via set_secret (never a plaintext Setting row). `ldap_bind_password_enc` has
#: always been encrypted; R3 brings the "hardening ride-along" (SPEC §"scope model")
#: keys under the same law — they were PLAINTEXT Setting rows (masked on display only)
#: until now. Any WRITE of one of these (over /api, the web form, or the CLI) routes
#: through set_secret, which encrypts and thereby MIGRATES an existing plaintext row
#: to ciphertext in place. READERS use read_secret_setting (below), which decrypts a
#: Fernet value and falls back to a legacy plaintext row, so the migration window is
#: seamless. This is an EXPLICIT key set — NOT the broad is_secret_key() substring
#: match — because is_secret_key() also matches plaintext-by-design keys like
#: `local_api_key` and `connector_token_org_id` that MUST NOT be Fernet-wrapped
#: (their readers read the raw value live).
SECRET_SETTING_KEYS: frozenset = frozenset(
    {
        "connector_api_token",
        "ldap_bind_password_enc",
        "smtp_password",
        "sso_client_secret",
        "oauth_airtable_client_secret",
        "oauth_atlassian_client_secret",
        "vendor_studio_sso_secret",  # VP-0 / SSO train: the Vendor Studio handoff HMAC secret
        "saml_sp_private_key_enc",  # Train I: the SP signing/decryption private key
    }
)

# Substrings that mark a settings key as a secret for DISPLAY MASKING (get/list and
# the R3 masked config read show "(set)"/"(unset)", never the value). Mirrors
# app/cli/_common._SECRET_MARKERS exactly — the broad masking rule, deliberately
# wider than SECRET_SETTING_KEYS (masking must never under-cover; Fernet-at-rest is
# the narrower, explicit set above). Keep the two marker tuples in sync.
_SECRET_MARKERS = ("password", "secret", "client_secret", "api_key", "_enc", "token")

#: Keys that are CREDENTIAL MATERIAL but whose NAME carries none of the substring
#: markers above, so the substring rule alone would leak them on a read surface.
#: R3 review HIGH: `operator_key_hash` is the SHA-256 hash of the SaaS cross-org
#: operator key — a one-way digest, but one a leaked admin key could pull from GET
#: /api/admin/config and crack offline for cross-org escalation. It is already
#: WRITE-protected (REMOTE_IMMUTABLE_KEYS); this makes it READ-protected too.
_EXPLICIT_SECRET_KEYS: frozenset = frozenset({"operator_key_hash"})

#: Keys whose NAME carries a marker substring ("password") but whose VALUE is a plain,
#: non-secret policy number: the password policy for local accounts (HECVAT AAAI-03).
#: They are shown, audited with before/after values, and settable over MCP set_config.
#: Mirrored EXACTLY in app/cli/_common.NOT_SECRET_KEYS and the MCP server's copy
#: (integrations/mcp-server/server.py); verify_password_policy pins all three.
NOT_SECRET_KEYS: frozenset = frozenset(
    {
        "password_min_length",
        "password_require_classes",
        "password_block_common",
        "password_history",
        "password_max_age_days",
    }
)


def is_secret_key(key: str) -> bool:
    """True when `key` names a secret whose value must be MASKED on any read surface
    (config list / GET /api/admin/config). Substring match (see _SECRET_MARKERS) OR
    an explicit credential-material key (see _EXPLICIT_SECRET_KEYS)."""
    k = (key or "").lower()
    if key in NOT_SECRET_KEYS:
        return False
    return key in _EXPLICIT_SECRET_KEYS or any(m in k for m in _SECRET_MARKERS)


def mask_value(key: str, value):
    """Render a value being WRITTEN (or just read) for `key` the way every read surface does:
    a secret key shows only (set)/(unset), a credential-bearing URL key loses its userinfo,
    query and fragment, any other key is shown as is. Used by confirm prompts, success lines
    and audit details so a secret or `user:pass@` URL never reaches a terminal or the log."""
    if is_secret_key(key):
        return "(set)" if value else "(unset)"
    if is_redacted_url_key(key):
        return redact_url_value(str(value)) if value else value
    return value


def display_value(key: str):
    """One masked settings read seam for web, CLI, remote API and MCP."""
    if is_secret_key(key):
        value = read_secret_setting(key) if is_secret_setting(key) else get(key)
        return mask_value(key, value)
    return mask_value(key, get(key))


def local_key_status():
    """Public status only; never carry credential material into a template."""
    if get("local_api_key") == "lm-studio":
        return "default (lm-studio)"
    return "key set" if display_value("local_api_key") == "(set)" else "no key"


def is_secret_setting(key: str) -> bool:
    """True when `key`'s value must be stored Fernet-encrypted at rest (routed through
    set_secret on write, read via read_secret_setting). The EXPLICIT, narrow set —
    not the display-masking substring match. See SECRET_SETTING_KEYS."""
    return key in SECRET_SETTING_KEYS


def read_secret_setting(key: str) -> str:
    """Migration-safe read of a SECRET_SETTING_KEYS value to plaintext ("" when unset).

    A value stored by set_secret is Fernet ciphertext and decrypts here. A LEGACY
    PLAINTEXT row (one written before R3 routed these keys through set_secret, and not
    yet rewritten) is not a valid Fernet token, so decryption fails — and rather than
    return "" (which would silently break SMTP/OIDC/OAuth on a not-yet-migrated
    install), the raw value is returned as the plaintext it is. The first WRITE through
    set_secret then encrypts it, and every later read decrypts cleanly. (A rotated/lost
    ENCRYPTION_KEY also fails to decrypt; that path already breaks these credentials
    whichever way we fall, so returning the raw ciphertext is no worse and the operator
    re-enters the secret — the documented recovery for a lost keyfile.)"""
    raw = get(key)
    if not raw:
        return ""
    if key == "connector_api_token" and raw.startswith("lsk_"):
        with host_operator_scope():
            set_many({key: raw})
        return raw
    try:
        return _fernet().decrypt(raw.encode()).decode()
    except Exception:
        if key == "connector_api_token":
            return ""  # Never accept damaged ciphertext as a bearer credential.
        return raw


# ============================================================================== #
# READ-THROUGH CACHE + CROSS-PROCESS INVALIDATION
# ============================================================================== #
# settings.get() sits on hot paths (every provider call, PII check, audit write) and
# each miss opens a fresh DB session, so the values are cached in-process. None in
# the cache means "no row in the DB" (fall through to DEFAULTS).
#
# THE BUG THIS BLOCK FIXES (audit K3, reproduced in live QA). The cache used to be
# coherent only for writes made in THIS process ("All writes go through set_many() in
# this process, so the cache stays coherent"). Every other process's write was
# invisible until a restart, which meant the kill switches could not kill a RUNNING
# server: `config set key_projects_enabled 0` from the CLI left the web grid and both
# /api key routes serving 200 while the CLI itself correctly refused, and only a
# uvicorn restart made them 404. On Fly, `fly ssh` + CLI would not stop a feature at
# all until the machine restarted. It affects EVERY setting, but it matters most for
# the switches documented as emergency-off.
#
# THE MECHANISM. One monotonically bumped integer parked in the Setting table under
# _VERSION_KEY (an out-of-DEFAULTS row, the same trick audit._ANCHOR_KEY already
# uses, so there is NO schema change and no migration). set_many() bumps it IN THE
# SAME TRANSACTION as the values it writes, so no reader can ever observe the new
# version without also being able to read the new values. A reader probes that one
# row at most once per CHECK_INTERVAL_SECONDS; when the version moved it drops the
# whole cache AND re-runs apply_to_config() (the provider/model settings are PUSHED
# into the `config` module rather than read through get() at the call sites, so a
# cross-process change is only half-applied without that).
#
# COST. Between probes the cache absorbs every read exactly as before — the hot path
# gains one monotonic clock read and one comparison inside the lock it already took,
# with NO second lock and NO extra DB work. The database cost is bounded to one
# primary-key SELECT per interval per process (~0.5/s), not one per get(). A save that
# changes NOTHING (an empty dict, a dict that filters to empty, or a value re-saved
# identical to the one on disk) does not bump the version at all — see set_many — so it
# costs no other process a cache drop.
#
# ALTERNATIVES REJECTED.
#  * An mtime sentinel file in DATA_DIR (the audit's other suggestion): needs a
#    filesystem WRITE on every save, so a read-only/full data dir breaks propagation
#    SILENTLY; mtime granularity is 1-2s on some filesystems; and the sentinel cannot
#    be written inside the value transaction, so signal and data can disagree.
#  * `PRAGMA data_version` (free, zero writes): it moves when ANY other connection
#    writes ANYTHING, so on a busy install the cache would be dropped continuously —
#    it answers "did the database change", not "did the settings change" — and it
#    does not exist on the Postgres backend.
#  * Dropping the cache / a per-get() read: that is exactly the DB hit per get() this
#    cache exists to avoid.
#  * A new `settings_version` column/table: a schema change (migration + the
#    _ADDED_COLUMNS shim + the nullability-parity guard) to store one integer the
#    existing key/value table already holds perfectly well.
_VERSION_KEY = "__settings_version__"
CHECK_INTERVAL_SECONDS = 2.0  # staleness ceiling; tests turn this down

_cache: dict[str, str | None] = {}
_cache_lock = threading.Lock()
_version: int | None = None  # last version this process observed (None = unknown)
_checked_at: float = 0.0  # monotonic clock of the last version probe
_probing = False  # one thread probes; the rest keep serving the cache
# Bumped on every clear/pop/update of _cache. A cache MISS reads the DB with the lock
# released, so a probe (or a save, or a forget) can land in between; the miss only
# writes its result back if the cache it read against is still the current one.
# Without it a miss that started before a cross-process change could re-seed the
# stale value AFTER the invalidating clear, and it would then survive until the next
# unrelated bump. (Same shape of race keys.forget_project_settings' R11 note calls
# out for the delete path.)
_generation = 0
# In-process save counter, sampled when a probe starts and re-checked when it lands. A
# probe reads the version row with `_cache_lock` RELEASED, so a set_many() can commit
# and publish its own state in between; the probe then discards its now-ambiguous
# reading rather than overwriting it.
#
# THIS ONE IS CHURN AVOIDANCE, NOT CORRECTNESS — unlike `_generation` above, which is
# load-bearing (proven by tests G/G2 in tests/verify_settings_propagation.py). Removing
# the _write_seq check cannot lose or invent a VALUE: a stale probe would only regress
# `_version` to an older NUMBER, and since the next probe then sees that number differ
# from the DB it simply drops the cache one extra time and re-reads. It is kept because
# that redundant drop is free to avoid, and because "a probe overwriting the version a
# local save just published" is state nobody should have to reason about.
_write_seq = 0
# Has this process ever KNOWN the version (adopted one, or published one by saving)?
# It is what tells a `_version is None` probe apart: False means "first reading of my
# life, boot is applying the config right now"; True means "I knew it and something —
# only invalidate_cache() can do this — forgot it", which is the case that must
# re-push into the config module. Deliberately NOT reset by invalidate_cache().
_version_seen_once = False


def _read_version() -> int | None:
    """The settings version currently in the DB, or None when it cannot be read (the
    table does not exist yet, the DB is locked/down, the value is garbage). None means
    "no information" — never "unchanged" — so the caller leaves the cache alone rather
    than guessing. A missing row is a real answer: 0, the pre-first-save state."""
    try:
        with Session(engine) as s:
            row = s.get(Setting, _VERSION_KEY)
        if row is None or not row.value:
            return 0
        return int(row.value)
    except Exception:
        return None


def _probe_version(seen_write_seq: int) -> None:
    """Reconcile this process's cache with the DB's settings version.

    Called at most once per CHECK_INTERVAL_SECONDS, with `_probing` already True and
    `_cache_lock` NOT held — it opens a DB session, and holding the settings lock
    across a DB round trip would serialize every hot-path get() behind it."""
    global _version, _checked_at, _probing, _generation, _version_seen_once
    observed = _read_version()
    changed = False
    with _cache_lock:
        _probing = False
        _checked_at = time.monotonic()
        if observed is not None and _write_seq == seen_write_seq:
            if _version is None:
                # ADOPTION: there is no prior reading to compare against. Two very
                # different situations reach here, and they need different answers.
                #
                #  * THE PROCESS'S FIRST EVER version reading. Boot's own
                #    apply_to_config() call is what populates the cache (it reads
                #    through get(), so this probe usually fires INSIDE it), the config
                #    module is being pushed right now, and there is nothing stale to
                #    repair. Re-applying here would be pure cost — and not harmless
                #    cost: apply_to_config() writes settings-derived values into the
                #    config module, so calling it EARLIER than a process expects
                #    changes what that process sees (a hermetic suite that blanks
                #    LOCAL_BASE_URL, for one, gets the shipped default injected).
                #  * A LATER None, which can only come from invalidate_cache() in an
                #    already-running process. The old code skipped apply_to_config()
                #    here too, justified as "boot already applies, and the cache we
                #    clear is empty anyway". That justification is a boot-time one and
                #    does NOT hold mid-life: the DB may have moved since the last
                #    push, so the value cache refreshes while config.DEFAULT_PROVIDER
                #    / config.DEFAULT_MODELS / the decrypted cloud keys keep the old
                #    one — half-applied. FUTURE-PROOFING, not a live hazard today:
                #    invalidate_cache() has zero callers under app/ or scripts/ (only
                #    tests and a smoke probe reach this path), so this
                #    state is unreachable in production as shipped — but because we
                #    ADOPT the observed version here, no later probe would ever notice
                #    if that changed, so the push stays unconditional.
                _version = observed
                _cache.clear()
                _generation += 1
                changed = _version_seen_once
                _version_seen_once = True
            elif observed != _version:
                _version = observed
                _cache.clear()
                _generation += 1
                changed = True
    if changed:
        try:
            apply_to_config()
        except Exception:
            log.warning(
                "settings: apply_to_config after a cross-process change "
                "failed; cached values are current but the config module "
                "may be stale",
                exc_info=True,
            )


INGRESS_ERRORS: set[str] = set()


def ingress_value(key, value):
    """Host ingress reads degrade to empty, never to a stale stored fallback."""
    try:
        # The base-URL inclusion rule belongs to write doors only. Repointing the URL
        # must not turn a syntactically valid existing pin off on the next read.
        _v_ingress_list(key, value, require_base_host=False)
    except InvalidSettingValue:
        if key not in INGRESS_ERRORS:
            log.error("ingress_config: invalid %s; treating as empty", key)
        INGRESS_ERRORS.add(key)
        return ""
    INGRESS_ERRORS.discard(key)
    return value


EGRESS_VIEW = {"value": "", "source": "file", "valid": True}


def _egress_value(value, source):
    from app.services.egress import parse_allowlist

    try:
        parse_allowlist(value)
        valid = True
    except ValueError:
        if EGRESS_VIEW["valid"] or EGRESS_VIEW["value"] != value:
            log.error("egress_config: invalid; treating as empty")
        valid = False
    EGRESS_VIEW.update(value=value, source=source, valid=valid)
    return value if valid else ""


def egress_policy_view():
    get("egress_private_allowlist")
    return dict(EGRESS_VIEW)


_external_base_invalid = False


def configured_external_base_url() -> str:
    """Read legacy rows tolerantly; warn once per invalid episode, never refuse reads."""
    global _external_base_invalid
    configured = (get("app_base_url") or "").strip()
    try:
        _v_app_base_url("app_base_url", configured)
    except InvalidSettingValue:
        with _cache_lock:
            if not _external_base_invalid:
                log.warning(
                    "Invalid app_base_url; falling back to the request Host. "
                    "Repair in /security or run config set app_base_url https://tms.example.com"
                )
            _external_base_invalid = True
        return ""
    with _cache_lock:
        _external_base_invalid = False
    return configured.rstrip("/")


def external_base_url_warning(request) -> str:
    """Operator remedy without hiding the raw stored value on the Security form."""
    configured = configured_external_base_url()
    if (get("app_base_url") or "").strip() and not configured:
        return (
            "Invalid app_base_url; using the request Host. "
            "Repair here or run config set app_base_url https://tms.example.com"
        )
    if configured and request.scope.get("root_path") and not urlsplit(configured).path:
        return (
            "app_base_url has no path but root_path is non-empty. "
            "The app_base_url path is the external prefix; include your public prefix "
            "(for example https://tms.example.com/tms) if the app is served under it."
        )
    return ""


def external_base_url(request=None) -> str:
    """The configured external URL authority, else the request base, else empty.

    Invalid legacy rows fall back exactly as unset. Without a request the fallback
    stays empty (background email behavior). Writes enforce the URL contract.
    """
    configured = configured_external_base_url()
    if configured:
        return configured
    return str(request.base_url).rstrip("/") if request is not None else ""


def external_url(request, path: str) -> str:
    """Build an external URL for a path beginning with a slash."""
    if not isinstance(path, str):
        raise TypeError("external_url path must be a string")
    if not path.startswith("/"):
        raise ValueError("external_url path must begin with /")
    return external_base_url(request) + path


def get(key: str, default=None):
    global _probing
    if key == "egress_private_allowlist":
        override = os.environ.get("LOC_TMS_EGRESS_PRIVATE_ALLOWLIST")
        if override is not None:
            return _egress_value(override, "env")
    # Host-managed ingress overrides win even over a previously pinned DB value.
    if key in ("security_allowed_hosts", "trusted_proxy_ips"):
        override = os.environ.get("LOC_TMS_" + key.upper())
        if override is not None:
            return ingress_value(key, override)
    with _cache_lock:
        # Staleness check folded into the lock the cache already takes: one clock
        # read + one compare on the hot path, no second lock acquisition.
        probe = not _probing and time.monotonic() - _checked_at >= CHECK_INTERVAL_SECONDS
        if probe:
            _probing = True
        seen_write_seq = _write_seq
        gen = _generation
        hit = key in _cache
        value = _cache.get(key)
    if probe:
        try:
            _probe_version(seen_write_seq)
        finally:
            # Belt and braces: a `_probing` flag left True would disable cross-process
            # propagation for the LIFE OF THE PROCESS and do it silently — the exact
            # failure this whole block exists to end. _probe_version clears it on every
            # path it has, so this is a no-op in practice; it is here so no future edit
            # inside it can reintroduce the stuck-flag shape.
            with _cache_lock:
                _probing = False
                gen = _generation
                hit = key in _cache
                value = _cache.get(key)
    if not hit:
        with Session(engine) as s:
            row = s.get(Setting, key)
        value = row.value if row is not None else None
        with _cache_lock:
            if gen == _generation:
                _cache[key] = value
    if value is None:
        value = DEFAULTS.get(key, default)
    if key in ("security_allowed_hosts", "trusted_proxy_ips"):
        return ingress_value(key, value)
    if key == "egress_private_allowlist":
        return _egress_value(value, "file")
    return value


def invalidate_cache() -> None:
    """Drop the read-through cache (only needed if the Setting table is written
    outside set_many, e.g. by a test poking the DB directly).

    Also forgets the observed version, so the next get() re-adopts whatever the DB
    holds — the right behavior when a test swaps the DB out from under the process.
    In a process that has already seen a version, that re-adoption ALSO re-runs
    apply_to_config() (see _probe_version): the DB may have moved since the last push,
    and a refreshed value cache sitting behind a stale `config` module is worse than
    either alone. A process that has never seen one is booting, where the push is
    already happening, so it is left alone."""
    global _version, _checked_at, _generation
    with _cache_lock:
        _cache.clear()
        _generation += 1
        _version = None
        _checked_at = 0.0


def all_values() -> dict:
    return {k: get(k) for k in DEFAULTS}


def _bump_version(s: Session) -> int:
    """Increment the cross-process settings version INSIDE the caller's open
    transaction and return the new value.

    The INCREMENT itself is done in SQL, not as a read-modify-write in Python, so two
    processes saving at the same instant cannot both compute (and both store) the same
    next number: the UPDATE reads the row under the writer lock it has just taken.
    CAST(... AS INTEGER) / CAST(... AS VARCHAR) are portable across SQLite and Postgres.

    The leading SELECT is a SANITY guard, not the increment: a hand-edited/garbage value
    makes CAST(value AS INTEGER) a silent 0 on SQLite but a hard ERROR on Postgres, and
    a settings SAVE must not become unrunnable because someone poked the row. A
    non-numeric value is normalized to 0 first, so the version self-heals (into a fresh
    sequence) instead of poisoning every future save.

    TWO KNOWN, ACCEPTED LIMITS (2026-08-03 review, both recorded rather than fixed):

     1. The self-heal restarts the sequence at 1, so the version CAN MOVE BACKWARDS
        (..., 412, poke, 1). That is harmless by construction: every reader compares
        with `!=`, never `>`, so a backwards move is detected as a change exactly like a
        forwards one and drops the cache. Nothing in this module treats the version as
        an ordering, only as an identity.
     2. SELECT-then-INSERT is not an upsert. If the version row does not exist yet and
        two processes make their VERY FIRST save at the same instant, both can see NULL
        and both can attempt the INSERT; on Postgres the loser gets a primary-key
        violation and its save fails (on SQLite the writer lock serialises them, so the
        loser's INSERT fails the same way inside its own transaction). Unreproduced and
        very low likelihood — it needs a race inside the one-time window before any
        save has ever happened on this install, and the failed save is loud (an
        exception), never a silent wrong value. An ON CONFLICT DO NOTHING would need
        two dialect-specific spellings; not worth it for that window."""
    conn = s.connection()
    row = conn.execute(
        text("SELECT value FROM setting WHERE key = :k"), {"k": _VERSION_KEY}
    ).fetchone()
    if row is None:
        conn.execute(text("INSERT INTO setting (key, value) VALUES (:k, '1')"), {"k": _VERSION_KEY})
        return 1
    if not str(row[0] or "").strip().isdigit():
        log.warning(
            "settings: the version row held %r; resetting it so saves keep propagating", row[0]
        )
        conn.execute(text("UPDATE setting SET value = '0' WHERE key = :k"), {"k": _VERSION_KEY})
    conn.execute(
        text(
            "UPDATE setting SET value = CAST(CAST(value AS INTEGER) + 1 AS VARCHAR) WHERE key = :k"
        ),
        {"k": _VERSION_KEY},
    )
    fresh = conn.execute(
        text("SELECT value FROM setting WHERE key = :k"), {"k": _VERSION_KEY}
    ).fetchone()
    return int(fresh[0])


def bump_version() -> int:
    """Operator escape hatch (F-R1): bump the cross-process version WITHOUT writing
    any setting value, so every other process re-reads on its next probe.

    set_many()'s no-op-save optimisation (see the "Re-saving the value already on
    disk" comment above) deliberately does not bump the version when a save's value
    already matches the DB -- that is correct for an ordinary re-save, but it also
    means the operator move of re-saving a value AFTER changing the underlying row
    OUT OF BAND (a hand-edited DB, a migration, `fly ssh` + raw SQL -- the case
    invalidate_cache()'s and forget()'s own docstrings both contemplate) no longer
    signals anyone: version N -> N, and every other process keeps serving its stale
    cache. Call this instead: it reuses `_bump_version`'s exact atomic-increment +
    self-heal SQL, so it is the same machinery a real save uses, just without a value
    attached.

    NOT wired into any CLI command or web route in this round -- it exists as a
    building block for whoever adds that surface next."""
    global _version, _checked_at, _write_seq, _version_seen_once
    with Session(engine) as s:
        new_version = _bump_version(s)
        s.commit()
    with _cache_lock:
        _write_seq += 1
        _version = new_version
        _checked_at = time.monotonic()
        _version_seen_once = True
    return new_version


#: Shared per-key VALUE-validator registry (P3 round-2 M1 / round-3 M5). The ONE door every
#: writer of these money-bearing / egress keys passes through, so `config set`, `config apply`,
#: the /api config spine, and the admin "Web verification" form all validate + audit (+ on the
#: wire R3-gate) identically. DELIBERATELY consulted at the four DOORS, NOT inside set_many:
#: a test (or an internal caller) may plant a value directly via set_many, and set_many stays
#: the raw store except for app_base_url URL safety (round-8 L3).
#: The enable flag has no validator (a plain "0"/"1" toggle).
def _v_ingress_list(key, value, *, require_base_host=True, base_url=None):
    from app.services.security import host_name, ingress_entries, proxy_networks

    if not str(value).strip():
        return
    try:
        if key == "security_allowed_hosts":
            if not ingress_entries(value):
                raise ValueError("non-empty host list requires at least one host")
            hosts = {host_name(entry) for entry in ingress_entries(value)}
            if require_base_host:
                if base_url is None:
                    base_url = get("app_base_url")
                try:
                    parsed = _parse_external_base_url(base_url, legacy_host=True)
                    base_host = parsed.hostname or ""
                    if base_host:
                        base_host = host_name(f"[{base_host}]" if ":" in base_host else base_host)
                except ValueError:
                    # An unreadable legacy origin must never prevent setting a pin.
                    base_host = ""
                if base_host and base_host not in hosts:
                    advice = f"[{base_host}]" if ":" in base_host else base_host
                    raise InvalidSettingValue(
                        f"{key}: missing app_base_url host {advice}; "
                        f"add {advice}, or clear/repoint app_base_url"
                    )
        else:
            proxy_networks(value)
    except InvalidSettingValue:
        raise
    except ValueError as exc:
        raise InvalidSettingValue(f"{key}: invalid host or IP/CIDR list: {exc}") from exc


def _v_login_networks(key, value):
    """#411: a comma list of IPs/CIDRs, each IPv4 /8 or narrower and IPv6 /32 or narrower."""
    from app.services.security import login_trusted_networks

    try:
        login_trusted_networks(value)
    except ValueError as exc:
        raise InvalidSettingValue(f"{key}: invalid IP/CIDR list: {exc}") from exc


def _v_default_provider(key: str, value) -> None:
    """#167: the chokepoint that stops arbitrary markup reaching config.DEFAULT_PROVIDER,
    which the app-shell banner renders on every page for every user.

    `config.PROVIDERS` is NOT the whole set of legal values, and validating against it
    alone breaks the product's documented zero-config path:
      * "mock" is the always-available MockProvider. `app/config.py:552` tells operators to
        "set DEFAULT_PROVIDER=mock (or pick Mock in Setup AI)", `providers_hub` resolves it
        as a first-class name, and `app/web/deps.py:163` treats it as a legitimate value.
        It is simply absent from the PROVIDERS list.
    A blank stays REFUSED. `deps.py:163` and `providers_hub` tolerate a blank that is
    already stored, but nothing in the repo ever WRITES one (the only `"default_provider":
    ""` is a read-side fallback dict at `deps.py:421`), so refusing it costs nothing and
    keeps an unset default from being saved by accident.

    Provider NAMES are matched case-insensitively because every consumer lowercases before
    resolving. An `engine:<id>` ref must be exactly canonical, which is what
    `engines.canonicalize_ref` emits, so a non-canonical spelling is never stored.

    Surrounding whitespace is REFUSED rather than tolerated. `set_many` stores the RAW
    value, and while every consumer lowercases before resolving (`providers_hub.py:825`,
    `deps.py:158`), none of them strips. So validating `value.strip()` while storing
    `value` would admit `" mock "`, which then makes `get_provider()` raise on every MT
    call on the install and pins the mock-fallback banner on permanently. A validator
    whose job is to admit only values that work must reject it at the door. Case is a
    different matter and stays tolerated, because the consumers really do lowercase."""
    import re

    if key == "default_fallback_provider" and value == "":
        return  # blank disables the install fallback
    if isinstance(value, str) and value == value.strip() and value:
        lowered = value.lower()
        if lowered == "jev":
            from app.services import engines

            try:
                engines.assert_ref_role(value, "translate")
            except engines.EngineRoleError as exc:
                raise InvalidSettingValue(f"{key}: {exc}") from None
        if lowered == "mock" or lowered in config.PROVIDERS:
            return
        if re.fullmatch(r"engine:[1-9][0-9]*", value):
            # A quality-judge engine (jev / jev_local) can never be the install's
            # translate default: it cannot translate.
            from app.services import engines

            try:
                engines.assert_ref_role(value, "translate")
            except engines.EngineRoleError as exc:
                raise InvalidSettingValue(f"{key}: {exc}") from None
            return
    if isinstance(value, str) and value != value.strip():
        # Say WHY. Otherwise `config set default_provider "mock "` is told to choose a
        # built-in provider having just named one, which reads as a bug in the product.
        raise InvalidSettingValue(f"{key}: remove the surrounding whitespace from {value!r}")
    raise InvalidSettingValue(f"{key}: choose a built-in provider or canonical engine:<id> ref")


def parse_qe_jev_confidence_floor(value, *, clamp=False):
    """Shared floor policy for stored-value recovery and settings write validation."""
    number = float(value)
    if not math.isfinite(number) or number > 1.0:
        raise ValueError("outside range")
    if number < 0.5:
        if not clamp:
            raise ValueError("outside range")
        return 0.5
    return number


def _v_whole_number_in(key, value, upper: int, unit: str) -> None:
    """A plain non-negative whole number no larger than `upper` (0 = off)."""
    s = str(value if value is not None else "").strip()
    # ASCII digits only (str.isdigit would accept superscripts that int() then rejects).
    if not s or len(s) > 9 or any(c not in "0123456789" for c in s):
        raise InvalidSettingValue(
            f"{key} must be a whole number of {unit}, 0 or more (got {value!r})."
        )
    if int(s) > upper:
        raise InvalidSettingValue(f"{key} must be at most {upper} {unit} (got {value!r}).")


#: #632: the most reject-and-rework rounds one vendor dispatch leg may take. Each round is a
#: billable vendor re-send, so the ceiling is small on purpose.
VENDOR_REWORK_MAX_ROUNDS_CAP = 10


def _v_vendor_rework_max_rounds(key, value):
    """#632: a whole number of rounds from 1 to VENDOR_REWORK_MAX_ROUNDS_CAP, ASCII digits only.
    Refused at the door so a typo can never be stored and then silently read as the default."""
    text = str(value if value is not None else "").strip()
    if (
        not text
        or len(text) > 3
        or not text.isascii()
        or not text.isdigit()
        or not 1 <= int(text) <= VENDOR_REWORK_MAX_ROUNDS_CAP
    ):
        raise InvalidSettingValue(
            f"{key} must be a whole number of rework rounds from 1 to "
            f"{VENDOR_REWORK_MAX_ROUNDS_CAP} (got {value!r})."
        )


#: The spend cap's sane ceiling (app.services.spend.MAX_SPEND_CAP_USD is this number; spend
#: imports settings, so the number lives here).
SPEND_CAP_MAX_USD = 10**9
_SPEND_CAP_RE = re.compile(r"(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:[eE]\+?[0-9]{1,3})?")


def spend_cap_value(value) -> str:
    """T11 (HECVAT APPL-09): THE parser for spend_cap_usd, shared by every write door (the
    web Setup AI card, `config set`, POST /api/admin/config and MCP). Blank means "no cap"
    and is stored as "0"; otherwise a plain decimal amount in USD, 0 or more, finite and at
    most SPEND_CAP_MAX_USD. An exponent is read as its plain amount ("1e3" and the "1e+06"
    form an older save could have stored give "1000" and "1000000"). Returns the clean
    stored form ("50", "42.5", "0", "1000"), which set_many stores for every door; raises
    InvalidSettingValue on anything else (negative, text, nan, inf, a signed or oversized
    exponent, over the cap), so a bad value is refused instead of silently turning the cap
    off."""
    s = str(value if value is not None else "").strip()
    if not s:
        return "0"
    # ASCII decimal notation only: digits with at most one point, optionally the "1e+06"
    # form an older save could have stored (no sign, no inf / nan, no thousands
    # separators; [0-9] because \d would admit other scripts' digits).
    if len(s) > 32 or not _SPEND_CAP_RE.fullmatch(s):
        raise InvalidSettingValue(
            "The spend cap must be an amount in US dollars, 0 or more, such as 50 or 42.50. "
            "Leave it blank or enter 0 for no cap."
        )
    amount = float(s)
    if amount > SPEND_CAP_MAX_USD:
        raise InvalidSettingValue(
            f"The spend cap is too large (must be at most ${SPEND_CAP_MAX_USD:,})."
        )
    # Plain notation (never an exponent), so the value the page shows saves again as-is.
    text = format(Decimal(s), "f")
    return (text.rstrip("0").rstrip(".") if "." in text else text) or "0"


def _v_spend_cap_usd(key, value):
    """T11: spend_cap_usd goes through spend_cap_value (one parser for all doors)."""
    spend_cap_value(value)


def _v_session_idle_minutes(key, value):
    """a11y T9: sign out after this many idle minutes, 0 = off, at most 24 hours (the
    session cookie's own window, app.main.SESSION_COOKIE_MAX_AGE, would cap it anyway)."""
    _v_whole_number_in(key, value, 1440, "minutes")


def _v_session_max_hours(key, value):
    """a11y T9: absolute session limit in hours from sign-in, 0 = off, at most 24 (the
    session cookie window)."""
    _v_whole_number_in(key, value, 24, "hours")


def _v_password_min_length(key, value):
    """Password policy: minimum length, 8..128 characters (NIST SP 800-63B floor is 8)."""
    _v_whole_number_in(key, value, 128, "characters")
    if int(str(value).strip()) < 8:
        raise InvalidSettingValue(f"{key} must be at least 8 characters (got {value!r}).")


def _v_password_require_classes(key, value):
    """Password policy: how many of lower, upper, digit, symbol are required, 0..4."""
    _v_whole_number_in(key, value, 4, "character classes")


def _v_password_block_common(key, value):
    """Password policy: 1 = refuse common and personal passwords, 0 = allow them."""
    if str(value if value is not None else "").strip() not in ("0", "1"):
        raise InvalidSettingValue(f"{key} must be 1 (on) or 0 (off) (got {value!r}).")


def _v_password_history(key, value):
    """Password policy: refuse reuse of the last N passwords, 0..24 (0 = off)."""
    _v_whole_number_in(key, value, 24, "passwords")


def _v_password_max_age_days(key, value):
    """Password policy: require a change after N days, 0 = never, at most 3650."""
    _v_whole_number_in(key, value, 3650, "days")


def _v_mfa_required(key, value):
    """Two-step sign-in requirement: one of off, admins, all (app/services/mfa.py)."""
    if str(value if value is not None else "").strip() not in ("off", "admins", "all"):
        raise InvalidSettingValue(f"{key} must be one of off, admins, all (got {value!r}).")


#: a11y T10: longest accepted accessibility contact (the practical email address limit;
#: also a sane ceiling for a support link).
ACCESSIBILITY_CONTACT_MAX = 254

# A plain email address: ASCII letters, digits and ._+- before the @, a dotted host name
# after it. Deliberately narrower than RFC 5322 (no quotes, no spaces, no markup
# characters), because the value becomes a mailto: link on the help page.
_A11Y_EMAIL_RE = re.compile(
    r"[A-Za-z0-9._+-]{1,64}@"
    r"(?:[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?\.)+[A-Za-z]{2,63}"
)
# Characters an https contact link may never carry: whitespace, quotes, angle brackets,
# backticks and backslashes (markup or attribute breakout, and the browser's backslash
# repair of "https:\\host").
_A11Y_URL_BAD = re.compile(r"[\s\"'<>`\\{}|^]")


def accessibility_contact_kind(value) -> str:
    """Return "email", "url" or "" (blank) for a VALID accessibility contact; raise
    InvalidSettingValue for anything else. The one parser behind both the write-door
    validator and the read accessor, so what is stored and what is rendered agree."""
    s = "" if value is None else str(value)
    if s == "":
        return ""
    if s != s.strip():
        raise InvalidSettingValue(
            "accessibility_contact: remove the spaces before or after the value."
        )
    if len(s) > ACCESSIBILITY_CONTACT_MAX:
        raise InvalidSettingValue(
            f"accessibility_contact must be at most {ACCESSIBILITY_CONTACT_MAX} characters."
        )
    if any(ord(c) < 32 or ord(c) == 127 for c in s):
        raise InvalidSettingValue("accessibility_contact must not contain control characters.")
    if _A11Y_EMAIL_RE.fullmatch(s):
        return "email"
    # A link is printable ASCII only: no bidi overrides or look-alike (IDN) letters that
    # make the visible link text read as a different site.
    if (
        s[:8].lower() == "https://"
        and all(33 <= ord(c) <= 126 for c in s)
        and not _A11Y_URL_BAD.search(s)
    ):
        try:
            parsed = urlsplit(s)
            host = parsed.hostname
            parsed.port  # noqa: B018 -- raises ValueError for an out-of-range port
        except ValueError:
            host = None
        else:
            if parsed.username is not None or parsed.password is not None:
                host = None  # "https://trusted@elsewhere" reads as the wrong site
        if host:
            return "url"
    raise InvalidSettingValue(
        "accessibility_contact must be a plain email address (name@example.com) or an "
        "https:// link, or blank."
    )


def _v_accessibility_contact(key, value):
    """a11y T10: a plain email address, an https:// link, or blank."""
    accessibility_contact_kind(value)


def accessibility_contact() -> dict:
    """The accessibility contact for the help page: {"kind", "value", "href"}.

    kind is "email" (href = mailto:), "url" (href = the link) or "" when the setting is
    blank. A stored value that fails the validator (a hand-edited database row) is
    treated as blank, so an unvalidated value never becomes a link."""
    raw = get("accessibility_contact")
    value = "" if raw is None else str(raw)
    try:
        kind = accessibility_contact_kind(value)
    except InvalidSettingValue:
        log.warning("settings: ignoring an invalid stored accessibility_contact")
        kind = ""
    if kind == "email":
        return {"kind": kind, "value": value, "href": "mailto:" + value}
    if kind == "url":
        return {"kind": kind, "value": value, "href": value}
    return {"kind": "", "value": "", "href": ""}


def _v_qe_jev_confidence_floor(key, value):
    try:
        if isinstance(value, bool) or parse_qe_jev_confidence_floor(value) >= 1.0:
            raise ValueError("outside write range")
    except (ValueError, TypeError, OverflowError):
        raise InvalidSettingValue(
            f"{key}: expected a finite number at least 0.5 and less than 1.0"
        ) from None


def _v_local_temperature(key, value):
    """#342: local_temperature / local_judge_temperature, a finite number in 0..2
    (inclusive). Booleans are numbers to float(); refuse them explicitly."""
    msg = f"{key}: expected a number between 0 and 2 (temperature)."
    if isinstance(value, bool):
        raise InvalidSettingValue(msg)
    try:
        t = float(value)
    except (ValueError, TypeError):
        raise InvalidSettingValue(msg) from None
    if not math.isfinite(t) or not (0.0 <= t <= 2.0):
        raise InvalidSettingValue(msg)


def _v_local_seed(key, value):
    """#342: local_seed, blank (send no seed key) or a non-negative whole number."""
    raw = str(value).strip()
    if raw == "":
        return
    if not re.fullmatch(r"[0-9]+", raw):
        raise InvalidSettingValue(
            f"{key}: expected a non-negative whole number (blank sends no seed)."
        )


def _v_local_model_warmup(key, value):
    """#341: local_model_warmup, a boolean-ish switch (0/1/true/false/on/off/yes/no)."""
    if str(value).strip().lower() not in {"0", "1", "true", "false", "on", "off", "yes", "no"}:
        raise InvalidSettingValue(f"{key}: expected 0/1, true/false, on/off or yes/no.")


def _v_assist_allowance(key, value):
    if not re.fullmatch(r"[0-9]+", str(value).strip()):
        raise InvalidSettingValue(f"{key} must be a non-negative integer (0 = unlimited).")
    try:
        int(value)
    except ValueError:
        raise InvalidSettingValue(f"{key} is too large.") from None


def _v_api_rate_limit(key, value):
    if not str(value).isascii() or not str(value).isdigit():
        raise InvalidSettingValue(f"{key} must be a whole number of requests, 0 or more.")
    try:
        if int(value) > 100000:
            raise InvalidSettingValue(f"{key} must be at most 100000 requests per minute.")
    except (ValueError, TypeError):
        raise InvalidSettingValue(f"{key} must be at most 100000 requests per minute.") from None


def _v_apikey_max_age_days(key, value):
    """Blank = no maximum (the default). Otherwise a positive whole number of days, at most
    3650. A garbage value must be refused at the door: it would otherwise be stored and then
    read as "no maximum", silently failing OPEN (#270 r2-apiauth-10)."""
    text = str(value if value is not None else "").strip()
    if not text:
        return
    if not text.isascii() or not text.isdigit() or not 1 <= int(text) <= 3650:
        raise InvalidSettingValue(
            f"{key} must be blank (no maximum) or a whole number of days from 1 to 3650."
        )


def _v_smtp_port(key, value):
    """#454: blank = the 587 default (the Security form stores it that way); anything else
    must be a whole TCP port. A garbage value used to be stored and then raised inside
    mailer.send -- which is documented never to raise -- so an invite request 500'd after
    its commit and skipped its audit row."""
    text = str(value if value is not None else "").strip()
    if not text:
        return
    if not text.isascii() or not text.isdigit() or not 1 <= int(text) <= 65535:
        raise InvalidSettingValue(f"{key} must be blank (587) or a port number from 1 to 65535.")


#: GitHub #502: the longest retention window the install accepts (about 273 years). Past about
#: 730,000 days `now - timedelta(days=N)` overflows the datetime range and every purge raised.
RETENTION_DAYS_MAX = 100_000


def _v_retention_days(key, value):
    """Blank = 0 (no window). Otherwise a whole number of days from 0 to RETENTION_DAYS_MAX,
    ASCII digits only. Refused at the door on every write path (web, CLI, /api, MCP)."""
    text = str(value if value is not None else "").strip()
    if not text:
        return
    if not text.isascii() or not text.isdigit() or int(text) > RETENTION_DAYS_MAX:
        raise InvalidSettingValue(
            f"{key} must be a whole number of days from 0 to {RETENTION_DAYS_MAX}."
        )


def _v_zero_one(key, value):
    """A strict 0/1 switch (identity-01: sso_trust_unverified_email). Anything else is
    refused rather than stored, so a typo can never silently read as on."""
    if str(value if value is not None else "").strip() not in {"0", "1"}:
        raise InvalidSettingValue(f"{key}: expected 0 or 1.")


def _v_finite_number(key, value):
    """#463: a promotion margin must be a finite number. NaN parses as a float and makes every
    `gain < margin` test False, silently disabling the margin bar; infinity makes it unreachable."""
    import math

    try:
        number = float(str(value if value is not None else "").strip())
    except ValueError:
        raise InvalidSettingValue(f"{key} must be a number.") from None
    if not math.isfinite(number):
        raise InvalidSettingValue(f"{key} must be a finite number (not NaN or infinity).")


def _v_mcp_oauth(name: str):
    """#753 validators live in app/services/mcp_as_common.py, imported on first USE so the
    kernel pipeline surface (verify_kernel_facade narrowness bound) does not grow."""

    def _validate(key, value):
        from app.services import mcp_as_common

        return getattr(mcp_as_common, name)(key, value)

    return _validate


VALUE_VALIDATORS = {
    "mcp_oauth_enabled": _v_zero_one,
    "mcp_oauth_dcr_enabled": _v_zero_one,
    "mcp_oauth_resource": _v_mcp_oauth("_v_mcp_oauth_resource"),
    "mcp_oauth_cimd_hosts": _v_mcp_oauth("_v_mcp_oauth_cimd_hosts"),
    "mcp_oauth_rs_secret_hash": _v_mcp_oauth("_v_mcp_oauth_rs_secret_hash"),
    "sso_trust_unverified_email": _v_zero_one,
    "apikey_max_age_days": _v_apikey_max_age_days,
    "retention_days": _v_retention_days,
    "smtp_port": _v_smtp_port,
    "api_rate_limit_per_minute": _v_api_rate_limit,
    "api_read_rate_limit_per_minute": _v_api_rate_limit,
    "assist_calls_per_minute": _v_assist_allowance,
    "assist_calls_per_day": _v_assist_allowance,
    "qe_jev_confidence_floor": _v_qe_jev_confidence_floor,
    "default_provider": _v_default_provider,
    "default_fallback_provider": _v_default_provider,
    "app_base_url": _v_app_base_url,
    "local_base_url": _v_local_engine_url,
    "local_temperature": _v_local_temperature,
    "local_judge_temperature": _v_local_temperature,
    "local_seed": _v_local_seed,
    "local_model_warmup": _v_local_model_warmup,
    "egress_private_allowlist": _v_egress_list,
    "security_allowed_hosts": _v_ingress_list,
    "trusted_proxy_ips": _v_ingress_list,
    "login_trusted_networks": _v_login_networks,
    "ledger_purge_reconcile_grace_seconds": _v_reconcile_grace_seconds,
    "spend_cap_usd": _v_spend_cap_usd,
    "session_idle_minutes": _v_session_idle_minutes,
    "session_max_hours": _v_session_max_hours,
    "accessibility_contact": _v_accessibility_contact,
    "password_min_length": _v_password_min_length,
    "password_require_classes": _v_password_require_classes,
    "password_block_common": _v_password_block_common,
    "password_history": _v_password_history,
    "password_max_age_days": _v_password_max_age_days,
    "mfa_required": _v_mfa_required,
    "vendor_rework_max_rounds": _v_vendor_rework_max_rounds,
}


def validate_value(key: str, value) -> None:
    """Run the per-key value validator for `key` if one is registered; a no-op otherwise.
    Raises InvalidSettingValue on a bad value. Called by every settings WRITE door before the
    store, so a garbage price or an SSRF-blocked host is refused at the door, not stored."""
    if key == "encrypt_db" and str(value) == "1":
        from app.services import dbcrypt, vault

        if not dbcrypt.enabled():
            raise InvalidSettingValue(vault.RETIRED_MESSAGE)
    if key == "prompt_tap_envelopes_enabled":
        raise InvalidSettingValue("Use the guarded ledger envelopes on/off ceremony.")
    fn = VALUE_VALIDATORS.get(key)
    if fn is not None:
        fn(key, value)


def validate_values(items: dict) -> None:
    """validate_value for every key in `items` that has a registered validator. Refuses the
    whole batch (raises on the first bad value) so `config apply` / a multi-key form post is
    all-or-nothing, mirroring set_many's atomic write."""
    for k, v in (items or {}).items():
        if k == "security_allowed_hosts" and "app_base_url" in items:
            _v_ingress_list(k, v, base_url=items["app_base_url"] or "")
        else:
            validate_value(k, v)


def normalize_ldap_host(value: str) -> str:
    """Bare hosts use LDAPS; explicit LDAP requires StartTLS in ldap_auth."""
    # Check before strip/urlsplit, which can silently discard some controls.
    if any(ord(c) < 32 or ord(c) == 127 for c in (value or "")):
        raise ValueError("LDAP host must not contain control characters")
    value = (value or "").strip()
    if not value:
        return ""
    uri = value if "://" in value else "ldaps://" + value
    try:
        parsed = urlsplit(uri)
    except ValueError:
        raise ValueError("LDAP host has an invalid address") from None
    if (
        parsed.scheme.lower() not in {"ldap", "ldaps"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path
        or parsed.query
        or parsed.fragment
        or any(c.isspace() for c in value)
    ):
        raise ValueError("LDAP host must be ldaps://, ldap:// (required StartTLS), or a bare host")
    # Also refuse malformed/non-numeric ports (including scheme-like 'http:host').
    try:
        port = parsed.port
    except ValueError as exc:
        # urlsplit raises on a non-numeric port, and its message quotes the raw value.
        raise ValueError("LDAP host has an invalid port") from exc
    # 65534, not 65535: ldap3/utils/port_validators.py:7 checks `range(0, 65535)`, which
    # EXCLUDES 65535, so accepting it here stores a value that raises LDAPInvalidPortError
    # at every login.
    if port is not None and not 1 <= port <= 65534:
        raise ValueError("LDAP port must be between 1 and 65534")
    # Refuse forms that PARSE but can never pass the TLS hostname check, so the failure
    # lands at configuration time with a clear reason instead of as a silent lockout at
    # every login. ldap3's hostname match is plain lowercase equality, so a trailing dot
    # never matches the certificate, and a raw IDN U-label never matches its A-label.
    host = parsed.hostname
    if "*" in host:
        # ldap3 treats the exact '*' as a bypass for all certificate hostname checks.
        raise ValueError("LDAP host must not contain wildcards")
    if "%" in host:
        raise ValueError("LDAP host must not contain an IPv6 scope or percent escapes")
    if host.endswith("."):
        raise ValueError(
            "LDAP host must not end with a trailing dot (it cannot match a certificate)"
        )
    try:
        host.encode("ascii")
    except UnicodeEncodeError as exc:
        raise ValueError(
            "LDAP host must be ASCII; convert an internationalised name to its punycode form"
        ) from exc
    # An IP literal can NEVER pass certificate validation here, so refuse it at the door
    # rather than storing a configuration that fails at every login. ldap3's
    # utils/tls_backport.py match_hostname consults only DNS SANs (falling back to CN only
    # when there are none) and never reads an `IP Address` SAN, so `ldaps://10.0.0.5`
    # raises LDAPCertificateError even against a certificate issued FOR that IP. This
    # worked on the old CERT_NONE path, so it is part of the breaking upgrade and
    # docs/SECRETS.md lists it as a must-correct form.
    if ":" in host:
        # Bracketed IPv6, or an IPvFuture literal that ldap3's Server() rejects outright.
        raise ValueError(
            "LDAP host must be a DNS name; an IP literal cannot be matched against a "
            "certificate, because only DNS subject-alternative names are checked"
        )
    try:
        ipaddress.ip_address(host)
    except ValueError:
        pass
    else:
        raise ValueError(
            "LDAP host must be a DNS name; an IP literal cannot be matched against a "
            "certificate, because only DNS subject-alternative names are checked"
        )
    # A backslash is not a label separator, so `dc.example\evil.example` would otherwise
    # pass the per-label length check as a single oversized-but-split label.
    if any(not 1 <= len(label) <= 63 for label in host.split(".")) or not all(
        c.isalnum() or c in "-_" for label in host.split(".") for c in label
    ):
        raise ValueError(
            "LDAP DNS labels must be 1 to 63 characters of letters, digits, hyphen or underscore"
        )
    if parsed.netloc.endswith(":"):
        raise ValueError("LDAP host has an empty port")
    return urlunsplit((parsed.scheme.lower(), parsed.netloc, "", "", ""))


#: #218: the legacy local engine slot's placeholder key (LM Studio ignores it). Not a
#: credential, so moving the slot never "clears" it.
_LOCAL_KEY_PLACEHOLDER = "lm-studio"  # pregate: allow-secret public placeholder


def _pin_local_slot_key(items: dict) -> tuple[dict, tuple[str, str, bool] | None]:
    """#218: the legacy local engine slot follows the shared credential-destination rule
    (app/services/credential_origin.py). A write that moves `local_base_url` to another
    ORIGIN clears the stored `local_api_key` unless the same write supplies
    `local_api_key` itself. Before this, a LINGUIST saving /setup-ai/local with a new URL
    and a blank key kept the admin's key, and the connection test then sent it to the
    new URL. Runs in set_many, so every door (web Setup AI, /api/admin/config, `config
    set`, MCP) is covered. Returns the adjusted items and (old, new, cleared) when the
    origin moved, else None."""
    if "local_base_url" not in items:
        return items, None
    from app.services.credential_origin import origin_label, origin_unreadable, same_origin

    old_raw, new_raw = get("local_base_url"), items["local_base_url"]
    # Gate round 2 MEDIUM-1: refuse an unreadable URL at this chokepoint too (a caller that
    # skipped validate_value), and compare fail-closed: an unreadable origin is a move.
    if origin_unreadable(new_raw):
        raise InvalidSettingValue(
            "local_base_url: the local engine URL must look like scheme://host or "
            "scheme://host:port with a plain numeric port; this one could not be read."
        )
    if same_origin(old_raw, new_raw):
        return items, None
    old, new = origin_label(old_raw), origin_label(new_raw)
    cleared = False
    if "local_api_key" not in items:
        stored = str(get("local_api_key") or "").strip()
        if stored and stored != _LOCAL_KEY_PLACEHOLDER:
            items = {**items, "local_api_key": ""}
            cleared = True
    return items, (old, new, cleared)


def _audit_local_slot_move(move: tuple[str, str, bool], actor: str, cleared_ids) -> None:
    """Audit a local-slot origin move (after commit), plus each engine profile whose key
    set_many cleared in the same transaction because it inherits the slot's URL (a `local`
    profile with a blank base_url sends ITS OWN key to whatever the slot points at)."""
    from app.services import engines
    from app.services.credential_origin import audit_repoint

    old, new, cleared = move
    audit_repoint(
        actor=actor or "settings",
        action="settings.local_engine_destination_changed",
        target="setting:local_base_url",
        old_origin=old,
        new_origin=new,
        cleared=cleared,
    )
    engines.audit_slot_following_clear(cleared_ids, old, new, actor=actor or "settings")


def set_many(items: dict, *, actor: str = "") -> None:
    global _version, _checked_at, _generation, _write_seq, _version_seen_once
    # Web, API and CLI callers share this write seam. set_secret supplies ciphertext.
    companion = items.get("connector_api_token")
    if companion:
        try:
            _fernet().decrypt(companion.encode())
        except Exception:
            items = {**items, "connector_api_token": _fernet().encrypt(companion.encode()).decode()}
    # Enforce this boundary for every caller, before any batch values are written.
    # #454: smtp_port belongs here because the web Security form posts it straight into
    # set_many (unlike the CLI and /api doors, which call validate_value per key first), so
    # the registry alone would leave that door storing a port mailer.send cannot parse.
    for key in (
        "default_provider",
        "default_fallback_provider",
        "qe_jev_confidence_floor",
        "accessibility_contact",
        "encrypt_db",
        "smtp_port",
        "retention_days",
        "vendor_rework_max_rounds",
    ):
        if key in items:
            validate_value(key, items[key])
    # The version row is INTERNAL machinery, never a settable setting: a caller that
    # passed it (a hand-built dict, a crafted form field) would otherwise rewrite the
    # cross-process signal every other process depends on.
    if _VERSION_KEY in items:
        log.warning("settings: refusing to set the internal version row via set_many")
        items = {k: v for k, v in items.items() if k != _VERSION_KEY}
    # L4: cap brandable coined-name writes at the SETTINGS layer (strip + 64), so EVERY
    # write path (web form, `config set`, POST /api/admin/config, MCP) is bounded, not just
    # the read accessor. Only the registry `brand_*` keys are touched; nothing else changes.
    if items:
        items = {
            k: (str(v).strip()[: branding._MAX_BRAND_LEN] if k in branding.REGISTRY else v)
            for k, v in items.items()
        }
    # Validate this URL at every write door, before any settings are persisted.
    if "app_base_url" in items:
        validate_value("app_base_url", items["app_base_url"])
        items = dict(items)
        base = str(items["app_base_url"] or "").strip().rstrip("/")
        parsed = _parse_external_base_url(base)
        items["app_base_url"] = urlunsplit(
            (parsed.scheme.lower(), parsed.netloc.lower(), parsed.path, "", "")
        )
    # T11 gate round 1 LOW 4: ONE normalization for the spend cap. Every door (web Setup
    # AI, `config set` / `config apply`, POST /api/admin/config, MCP) stores the canonical
    # form spend_cap_value returns (" 50 " -> "50", "1e3" -> "1000", blank -> "0").
    if "spend_cap_usd" in items:
        items = {**items, "spend_cap_usd": spend_cap_value(items["spend_cap_usd"])}
    for key in ("assist_calls_per_minute", "assist_calls_per_day"):
        if key in items:
            _v_assist_allowance(key, items[key])
            items = {**items, key: str(int(items[key]))}
    # Opt-in extra guard: turning the LDAP toggle ON while the (LGPL, opt-in) ldap3
    # package is absent would save a switch that crashes every subsequent login. Refuse
    # it HERE — the one chokepoint every write path (web security form, `config set`,
    # POST /api/admin/config, MCP manage_install) funnels through — with the clear
    # extras-file message, before anything is written. A lazy import avoids the
    # settings<->ldap_auth import cycle; only a literal "1" for ldap_enabled triggers it.
    if items.get("ldap_enabled") == "1":
        from app.services import ldap_auth

        ldap_auth.require_installed()
    if "ldap_host" in items or items.get("ldap_enabled") == "1":
        host = normalize_ldap_host(items.get("ldap_host", get("ldap_host")))
        if "ldap_host" in items:
            items = {**items, "ldap_host": host}
    # A CA bundle that is not there fails at BIND time, where the only signal is a
    # rate-limited log line. Check it at write time so the operator is told immediately.
    ca_cert = (items.get("ldap_ca_cert") or "").strip()
    if ca_cert and not os.path.isfile(ca_cert):
        raise InvalidSettingValue(f"ldap_ca_cert: no such file: {ca_cert}")
    if items and REQUEST_DOOR.get() and get("multi_org_enabled") == "1":
        # #281 / gate round 1 L3: inside a web or /api request, an install-wide security key
        # is refused for everyone while multi-org is active (no org admin may change it for
        # every org). A strict no-op with the gate off. Lazy import: access imports settings.
        from app.services import access

        # Only keys this write CHANGES count: a form that re-posts every field (the Security
        # page) with its stored values is not a change to them.
        refused = access.install_wide_security_refused(
            [k for k, v in items.items() if (get(k) or "") != (v or "")]
        )
        if refused:
            raise InstallWideRefused(refused)
    items, local_slot_move = _pin_local_slot_key(items)
    if not items:
        # Nothing to write at all (an empty dict, or one that filtered down to empty).
        # A version bump here would be a pure tax on EVERY OTHER process: each drops
        # its whole cache and re-runs apply_to_config (~13 SELECTs) to discover that
        # nothing changed. apply_to_config() still runs so the in-process semantics of
        # a save are byte-identical to before.
        apply_to_config()
        return
    new_version = None
    _monitoring_disabled = False  # F5 bonus: audit_chain_verify_hours transitioned to OFF
    _posture_changes = []  # #220: (key, old, new) for audit_enabled / audit_fail_open
    slot_cleared_ids: list[int] = []
    with Session(engine) as s:
        wrote = False
        for k, v in items.items():
            row = s.get(Setting, k)
            if k in AUDIT_POSTURE_KEYS:
                _old = row.value if row is not None else DEFAULTS.get(k)
                if (_old or "") != (v or ""):
                    _posture_changes.append((k, _old, v))
            if k == "audit_chain_verify_hours":
                # A cadence flip to a non-positive integer turns scheduled audit-chain
                # verification OFF — a security-relevant change a SIEM should see as a
                # DISTINCT event. Detect the transition here (the one chokepoint every write
                # path funnels through) using the current DB/default value as the "before".
                _old = row.value if row is not None else DEFAULTS.get(k)
                _monitoring_disabled = _hours_on(_old) and _hours_off(v)
            if row is not None and row.value == v:
                # Re-saving the value already on disk. Skipping it is not an
                # optimisation of the WRITE (the UPDATE is a no-op either way) but of
                # the SIGNAL: it keeps a no-change save from bumping the version and
                # invalidating every other process's cache. The value still goes into
                # THIS process's cache below via _cache.update(items), which is enough
                # in a WARM process -- but right after invalidate_cache() it is NOT the
                # whole story: the no-bump path here leaves `_checked_at` untouched (it
                # is only reset inside the bump branch), so the apply_to_config() call
                # at the bottom of this function still runs an adoption probe (see
                # _probe_version) that clears the very cache this just seeded. No
                # correctness impact -- the skipped value is identical to the DB's by
                # construction, so re-reading it costs a SELECT, not a wrong answer.
                #
                # RECOVERY (F-R1): if a row was changed OUT OF BAND (a hand-edited DB,
                # a migration, `fly ssh` + raw SQL -- the case invalidate_cache()'s and
                # forget()'s own docstrings both contemplate), re-saving the SAME value
                # through set_many() lands here and, correctly, signals no one. Call
                # settings.bump_version() to force every other process to re-read on
                # its next probe, with no value write of its own.
                continue
            if row is None:
                row = Setting(key=k, value=v)
            else:
                row.value = v
            s.add(row)
            wrote = True
        if local_slot_move is not None and wrote:
            # Gate round 1 LOW-3: clear the keys of the profiles that inherit the slot URL
            # in THIS transaction, so the new URL and the cleared keys commit together and
            # before apply_to_config() below makes the URL live in this process.
            from app.services import engines as _engines

            slot_cleared_ids = _engines.clear_slot_following_keys(s)
        if wrote:
            s.flush()
            # SAME TRANSACTION as the values (see the cache block): a reader can never
            # see the new version without the new values. The other order would let a
            # probe adopt the version, skip the clear, and serve the old values forever.
            new_version = _bump_version(s)
            s.commit()
    with _cache_lock:
        _generation += 1
        _cache.update(items)
        if new_version is not None:
            _write_seq += 1
            # This process wrote it, so the cache is already coherent: adopt the
            # version and reset the probe clock instead of making the next get()
            # re-read the row.
            _version = new_version
            _checked_at = time.monotonic()
            # A save is a version reading too: if invalidate_cache() later forgets it,
            # the probe that re-adopts must re-push into the config module (a process
            # whose first settings act was a SAVE has still "known" the version).
            _version_seen_once = True
    apply_to_config()
    if _posture_changes and wrote:
        _record_audit_posture_changes(_posture_changes)
    if local_slot_move is not None and wrote:
        _audit_local_slot_move(local_slot_move, actor, slot_cleared_ids)
    if _monitoring_disabled and wrote:
        # F5 bonus: emit a DISTINCT SIEM event + ERROR log when scheduled audit-chain
        # verification is turned off. Lazy import (settings is imported BY audit — a
        # module-level import here would be a cycle); audit.log opens its own session, so it
        # is safe to call after this write has committed. force=True so the disable is
        # recorded even if the global audit gate is itself off.
        try:
            from app.services import audit

            audit.log(
                actor="system",
                action="audit.chain_monitoring_disabled",
                detail="audit_chain_verify_hours set to 0 — scheduled chain verification OFF",
                force=True,
            )
            log.error(
                "audit: scheduled audit-chain verification DISABLED "
                "(audit_chain_verify_hours -> 0). The chain is no longer auto-verified; "
                "it stays verifiable on demand via `audit verify`."
            )
        except Exception:
            log.warning(
                "settings: could not audit the audit_chain_verify_hours disable", exc_info=True
            )


def _record_audit_posture_changes(changes) -> None:
    """#220: write a FORCED audit row for every audit_enabled / audit_fail_open change, at
    the one chokepoint every settings door funnels through (web Security page, `config set`,
    `config apply`, the /api spine, a direct service call). force=True, so turning the trail
    OFF is itself recorded even though the gate it describes is now closed; and turning it
    off also logs at ERROR, the signal a host log alert picks up. The actor is the request
    identity the middleware installed (egress.CALLER_ACTOR), else "system" (a host CLI door
    adds its own forced row naming its actor). Never raises: the setting is already saved."""
    try:
        from app.services import audit, egress

        actor = egress._resolve_actor() or "system"
        ip = egress.CALLER_IP.get() or ""
        for key, old, new in changes:
            if key == "audit_enabled":
                action = "audit.logging_disabled" if new != "1" else "audit.logging_enabled"
            else:
                action = "audit.fail_open_changed"
            audit.log(
                actor=actor,
                action=action,
                target=key,
                detail=f"{key} old={old or ''} new={new or ''}",
                ip=ip,
                force=True,
            )
            if key == "audit_enabled" and new != "1":
                log.error(
                    "audit: audit logging turned OFF (audit_enabled=0) by %s. Mutating "
                    "actions are no longer recorded; turn it back on with "
                    "`config set audit_enabled 1`.",
                    actor,
                )
    except Exception:
        log.error("settings: could not record an audit posture change", exc_info=True)


def forget(keys) -> None:
    """Evict `keys` from the read-through cache after their Setting ROWS were deleted
    by someone else's transaction (today: `keys.forget_project_settings`, called inside
    `content.delete_project` so the delete is one transaction with the rest of the
    cascade). The rows are gone, so the cache must not keep serving their last values
    to a project that reuses the id. Deliberately cache-only -- this never writes.

    STILL cache-only after the cross-process work: bumping the shared version row here
    would mean opening a WRITE session while the caller holds an uncommitted delete
    transaction, which on SQLite is the QA-1 circular-wait geometry exactly. The
    generation bump is the in-process half (it invalidates an in-flight miss that would
    otherwise re-seed a just-popped key); an out-of-process reader still picks the
    deletion up only when something else bumps the version, so a deleted project's
    per-project overrides can linger in ANOTHER process's cache until then. Recorded,
    not fixed here -- closing it needs a post-commit hook content.delete_project does
    not have."""
    global _generation
    with _cache_lock:
        _generation += 1
        for k in keys:
            _cache.pop(k, None)


def get_secret(key: str) -> str:
    """Decrypt an encrypted-at-rest setting to plaintext ("" when unset/unreadable)."""
    token = get(key)
    if not token:
        return ""
    try:
        return _fernet().decrypt(token.encode()).decode()
    except Exception:
        # wrong/rotated key or corrupt value — treat as "no secret" rather than
        # crash a login/settings page; the admin re-enters it to fix.
        return ""


def set_secret(key: str, plaintext: str) -> None:
    """Encrypt + store a secret setting. Empty plaintext clears it."""
    value = _fernet().encrypt((plaintext or "").encode()).decode() if plaintext else ""
    set_many({key: value})


def _drop_engine_profile_memo() -> None:
    """Drop the engine-profile memo, only when engines is loaded (no memo otherwise)."""
    engines_mod = sys.modules.get("app.services.engines")
    if engines_mod is not None:
        engines_mod.invalidate_profile_cache()


def apply_to_config() -> None:
    """Push stored settings into the live config module."""
    get("egress_private_allowlist")
    config.LEDGER_ENVELOPES_ENABLED = get("prompt_tap_envelopes_enabled") != "0"
    config.APP_BASE_URL = get("app_base_url") or ""
    config.SECURITY_ALLOWED_HOSTS = get("security_allowed_hosts") or ""
    config.TRUSTED_PROXY_IPS = get("trusted_proxy_ips") or ""
    config.DEFAULT_PROVIDER = get("default_provider")
    config.DEFAULT_FALLBACK_PROVIDER = get("default_fallback_provider")
    config.DEFAULT_MODELS["claude"] = get("model_claude")
    config.DEFAULT_MODELS["gemini"] = get("model_gemini")
    config.DEFAULT_MODELS["openai"] = get("model_openai")
    config.DEFAULT_MODELS["local"] = get("local_model")
    # Gate round 2 LOW-1: read BOTH slot values first, then drop the engine-profile memo,
    # then publish the KEY before the URL. A process adopts a moved slot URL only here (its
    # own write, or a cross-process version probe), and the same commit cleared the keys of
    # the profiles that inherit the slot, so the memo goes before the new URL is visible.
    # Readers (credential_origin.slot_key_for, providers) read the URL first and the key
    # second, so a racing reader can pair the OLD url with the new key, never a new url
    # with the old key.
    _new_slot_url = get("local_base_url")
    _new_slot_key = get("local_api_key")
    _slot_moved = _new_slot_url != config.LOCAL_BASE_URL
    if _slot_moved:
        _drop_engine_profile_memo()
    config.LOCAL_API_KEY = _new_slot_key
    config.LOCAL_BASE_URL = _new_slot_url
    if _slot_moved:
        # Again after publishing: a reader between the first drop and the URL write may
        # have re-filled the memo; the committed rows it read are already cleared, but a
        # second drop costs one row fetch and removes the question.
        _drop_engine_profile_memo()
    # In-app cloud API keys (Claude/Gemini/OpenAI): a key stored + encrypted in the
    # Setting table OVERRIDES the .env one; the env-original (captured once at import)
    # is the fallback when no key is stored. get_secret decrypts the Fernet blob (or
    # "" when unset/undecryptable), so effective = stored || env-original lands in the
    # SAME mutable config attributes every provider call site already reads live. The
    # `cloud_key_*_enc` keys are DELIBERATELY not in DEFAULTS (they are pure encrypted
    # secrets managed via app.services.cloud_keys / the Setup AI page), so they never
    # surface in all_values()/the settings page/`config list`.
    config.ANTHROPIC_API_KEY = get_secret("cloud_key_claude_enc") or config._ENV_ANTHROPIC_API_KEY
    config.GEMINI_API_KEY = get_secret("cloud_key_gemini_enc") or config._ENV_GEMINI_API_KEY
    config.OPENAI_API_KEY = get_secret("cloud_key_openai_enc") or config._ENV_OPENAI_API_KEY
