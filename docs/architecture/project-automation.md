# Project Automation - Architecture (L1)

## 1. Role

Project automation lets a **project** choose where its content comes from and goes back to, and
when it runs. A project binds to a connection (the system its content is pulled from and its
translations are pushed back to) and carries a schedule: UTC times of day on chosen days of the
week, or an interval, in delta mode (only new or changed content) or full mode (everything again,
TM matches are free). A manager can also run it now, pause and resume it, give it a per-run spend
cap, and read the run history on the project's Automation page. It is the same on the web page,
the CLI, the `/api` routes and the two MCP tools.

It does **not** translate or write anything itself. A run is the existing `connector_translate`
job for the project's connection (documented in connectors and jobs-and-scheduler), tagged with
the automation id, so progress, history, failure capture, restart recovery, the archive and AI
opt-out skips, the review gate (a unit held for review holds its whole pair) and each connector's
draft or live posture stay the connector flows' business. Nothing is enabled by default,
enabling a schedule never fires immediately, and the global scheduler switch stops it.

**One connection can feed many projects** (decided 2026-10-13). Per-connection state (content
hashes, watermarks, the id map) cannot be shared by two projects, so a connection that feeds no
project yet is adopted directly by the first project that binds it, and every further project
gets a private **lane**: a clone of the picked connection (same credential, kept in step with it)
that carries its own state and is never listed on the connection screens. Two projects can never
write over each other: the first project whose run reaches an external item owns its write-back
(a claim), a later project skips it and the conflict is reported on the run and in the audit
trail.

## 2. Invariants

- **Schedules are UTC and the math is pure.** `app/services/automation_schedule.py::next_fire`
  returns the first slot strictly after a moment; there is no daylight saving to get wrong. The
  screens show local time beside UTC in the browser only. Enforced by `verify_project_automation`
  (rollover, leap day, strictly-after, validation table).
- **Enabling a schedule never starts a run, and a slot is consumed before the job is submitted.**
  `app/services/project_automation.py::configure` sets `next_at` to the next slot;
  `app/services/project_automation.py::tick` advances it before `jobs.submit_unique`, so a crash
  skips a run and never doubles one; an in-flight run defers the slot. Enforced by
  `verify_project_automation`.
- **The kill switches hold.** `setting:project_automation_enabled` refuses binding, scheduling and
  running and stops the tick (reads and unbind stay). `app/services/scheduler.py::tick` calls the project tick only
  below `setting:sync_scheduler_enabled`; a disabled connector kind, an archived project, an
  AI-off project, a suspended org and a connection that needs reconnecting are skipped with a
  reason, not counted as failures. A connection an enabled automation runs on never also fires
  its own interval schedule (`app/services/project_automation.py::owns_schedule`). Enforced by
  `verify_project_automation` and `verify_schedule`.
- **Money is gated three times.** The project's monthly budget is checked before a run is
  submitted and again when it starts (`app/services/project_automation.py::preflight`); inside
  the run the paid calls stop at the smaller of the optional per-run USD cap and what the budget
  has left (`app/services/spend.py::run_cap`), and a run that stopped there is a partial success,
  never a failed run; the cap never trips the install-wide spend alarm. Enforced by `verify_project_automation` and
  `verify_spend_cap`.
- **Three failed runs in a row pause the automation.** `app/services/project_automation.py::_settle`
  counts failed runs, resets on success, does not count a partial run (some items failed) and
  pauses at once when the connection needs a reconnect. A person resumes. Enforced by
  `verify_project_automation`.
- **A lane follows the picked connection and keeps only the project's own state.** Before every
  run the lane takes the picked connection's credential, posture (publish, draft, hosts,
  locales, switches), customer and health (a lane stuck in REAUTH recovers when the picked
  connection is reconnected); it keeps the project's own scope settings and all run state
  (`app/services/project_automation.py::_sync_credential_to_lane`). A token refreshed on the lane
  is handed back only onto the credential it started from. Enforced by `verify_project_automation`.
- **No project overwrites another's items.** The first project to reach a (record, locale) owns its
  write-back (`app/services/connectors/flow.py::run_translate_flow` asks
  `app/services/connectors/run_ctx.py::claim`), so two projects with different languages share a
  connection freely. A claim is released when its owner is archived, unbound or removed, taken
  over from an archived or unbound owner, and swept after a clean, COMPLETE listing when the item left
  the scope or was deleted upstream (a run that could not read a record, refused one for size or
  halted keeps its claims: `listing_incomplete`). Claims are read once per run, not per pair. Runs that are not automation jobs (webhook, Translate now, the
  connection's own schedule) are claim-aware too once a connection has claims. A run whose pairs
  are all owned elsewhere ends `blocked`, not `ok`, and the page names the owners. Kinds outside
  the flow engine (airtable, jira, the git kinds) serve one project per connection. Enforced by
  `verify_project_automation`.
- **A bound project's own language list is the only list its runs use.** A connection that runs for a
  project with a content source takes its target languages from the project's ACTIVE languages
  (`app/services/connectors/store_common.py::effective_locales` via `bound_project_locales`), in the
  connection's own spelling; an explicit locale list can only narrow it. Every kind reads it through
  `effective_locales`: the shared flow engine, jira and the git trio directly, and airtable (whose
  targets are the language columns it finds) filters those columns with
  `app/services/connectors/store_common.py::locale_is_active`. Adding a language to the project means
  the next run translates and writes only the new language's pairs (the per-pair content hashes of the
  old languages hold). Removing one STOPS it: nothing is deleted, the language row stays with a
  `stopped_at` time, its segments, TM and what was written to the external system stay, its claims are
  released (`app/services/project_automation.py::locale_stopped`) and its per-pair hashes and
  read-back fingerprints are forgotten, so adding it back runs its pairs again
  (`app/services/project_automation.py::locale_added`). The run connection's `target_locales` follows
  the project (`app/services/project_automation.py::sync_run_locales`). Unbound connections keep their
  own list. Enforced by `verify_connected_languages`.
- **One language is one row and one run entry.** Two spellings of a language (`pt-br` and `pt-BR`,
  compared by `languages_catalog.normalize`) never make two `TargetLocale` rows: the connector
  registration (`app/services/connectors/store_common.py::_add_missing_locales`), the first pull
  (`app/services/connectors/store_part2.py::_ensure_project`, which stores the catalog spelling for every kind, as the shared
  flow's stage 5 does), the translate door (`app/web/api_discovery.py::prepare_translate_locale`) and
  add-locale all dedup on the normalized code, and an existing row's spelling wins (rows are never
  renamed; a connector's wire spelling stays in its own config for its API calls). `effective_locales`,
  `bound_project_locales` and `sync_run_locales` list each language once even on a legacy project that
  already holds two spellings. Enforced by `verify_connected_languages`.
- **Editing a run connection's languages edits the project.** On the connection a project runs on
  (the adopted case, `run_connection_id == connection_id`, or the project's own copy), a change of
  `target_locales` / `target_locale` / `language_set_id` through `store.update` or `store.patch_config`
  (web connection form and Languages card, `/api/connectors/{id}/update`, both CLIs; MCP has no
  connection-edit tool) flows into the project's list
  (`app/services/project_automation.py::connection_language_plan`, decided before the save, and
  `apply_connection_language_plan`, after it): a language the new list names is added (a stopped one
  comes back) through `content.add_target_locale`, a language the person removed is stopped through
  `content.remove_target_locale`, then the connection is rewritten to the project's list. An empty list
  changes nothing on the project. Refused before anything is saved: an unknown code (400), an add by a
  caller below manager (403, decision 8; editing a connection stays admin or linguist, so a linguist
  can stop a language there but not add one), a change that would stop every language (400). A
  connection that only FEEDS other projects (each runs on its own copy) refuses a language change
  with 409 "managed by project X" instead of saving one that would change nothing they run. Writes the
  project makes itself (`sync_run_locales`) and a bind's scope run inside
  `connection_follows_project()` and never loop back. Enforced by `verify_connected_languages`.
- **The connection keeps its own spelling.** When the project's list is written back to the run
  connection, a language the connection already names keeps the connection's (wire) spelling, Akeneo
  `fr_FR` or Zendesk `pt-br`; only a language new to the connection takes the project's spelling
  (`app/services/connectors/store_common.py::in_connection_spelling`). The translate doors (`/api`
  translate, `/api/jobs/run`, so MCP `translate_document`) use the project's spelling of a language it
  already has (`app/web/api_discovery.py::project_locale_spelling`), so `pt-br` on a `pt-BR` project
  never seeds or translates a second segment set. The connection page and the `/api` / CLI connection
  rows name the project whose languages a connection follows (`languages_project_id`,
  `app/services/connectors/store_common.py::languages_project_id`). A legacy project that already holds
  two rows for one language (`pt-br` and `pt-BR`) keeps both rows and their segments (nothing is
  merged or deleted, everything stays readable); runs use the first row only, and stopping the language
  stops both rows. Enforced by `verify_connected_languages`.
- **A blocked run tells both projects once, and a manager can hand the items over.** A run whose
  pairs are all owned elsewhere posts `automation.blocked` (counts and project names only; another
  customer's project is never named) to the blocked project and to each owning project, once per
  CHANGE of the block (`ProjectAutomation.blocked_sig`), not on every blocked run. The refused
  pairs are marked `ProjectBindingClaim.wanted_by`. `hand_over` (web button on both project pages,
  `POST /api/projects/{id}/automation/handover`, CLI `project automation handover`, MCP
  `project_automation` action `handover`) moves exactly those claims to the project that wanted
  them: manager or admin, both projects visible to the caller, the receiver bound to the same
  connection, refused while any run that writes as either project is in flight (its automation
  run, or a webhook / Translate now run on a connection it imports into). The hand-over and a run
  reading its claims are serialized by a claim gate (`_claim_gate`: a no-op UPDATE of the project
  rows as the first statement, the SQLite writer lock or the Postgres row locks), so a run either
  loaded its claims before (then it is in flight and the hand-over is refused) or loads them after
  the commit; the old owner can never write a pair it gave away. Audited
  `automation.claim_handed_over` (forced) and shown in both run histories.
  Enforced by `verify_project_automation`.
- **Missed slots run once.** A scheduled slot that fires more than 10 minutes late (downtime, the
  scheduler switched off) is a `catch_up` run: ONE run covers everything missed (delta reads every
  change since the last run, full reads it all), the next slot is computed from now, and the audit
  row and run history record how many slots were missed. Enforced by `verify_project_automation`.
- **Managers bind organization-wide connections to a copy.** A connection with no customer may be
  bound by a manager; the project always gets a private lane, so the shared connection is never
  adopted or narrowed by a non-admin and one project's scope never reaches the next. All scope,
  SSRF and visibility rules are unchanged; the bind is audited with `org_wide`. Enforced by
  `verify_project_automation`.
- **A full pull changes only the hash check.** `app/services/connectors/run_ctx.py::full_pull`
  makes the flow ignore the stored content hash for the run; the review gate, parking of
  hand-edited copies, the draft or live posture and the write ceiling are untouched. Full is
  refused for kinds outside the flow engine. Enforced by `verify_project_automation` and
  `verify_flow_engine_parity` (a run that sets nothing is byte-identical).
- **Scope changes follow the connection rules.** A manager may narrow a lane's scope inside the
  same credential; any other declared setting is admin only (`app/services/project_automation.py::NARROW_KEYS`,
  `app/services/connectors/store_common.py::SCOPE_ADMIN_FIELDS`); a Jira schedule needs an admin
  (#517). Enforced by `verify_project_automation`.
- **Isolation: 404 before 403.** A project or connection the caller cannot see answers like a
  missing one; cost figures are manager and up (ADR 0042). Enforced by
  `verify_project_automation` and `verify_project_scoping`.

- **A connected project is created empty and its uploads are refused.** A project's content
  source is stored (`app/models.py::ProjectIntake`, `Project.intake`: `upload` or `connected`),
  not derived from the binding, because the project exists, and shows its setup menu, before any
  connection is bound. The intake can change only while the project has no documents, no keys and
  no binding (`app/services/connected_projects.py::set_intake`); unbinding keeps it connected;
  every route that adds files by hand (web, `/api`, CLI, images) answers a connected project with
  400 (`app/services/connected_projects.py::refuse_upload`), so the hidden page controls cannot be
  bypassed, while the connector flows write through their own path. A template applied to a
  connected project never binds and never schedules. Enforced by `verify_connected_projects`.
- **Existing connector projects.** A project a connector creates on its first pull
  (`store_part2._ensure_project`, and `git_core._ensure_key_project` for the git trio) is a
  connected project bound to that same connection
  (`connected_projects.adopt_new_project`: `enabled` False, no lane); with the module off, or on
  any error, it stays the legacy project. An existing project that a connection already feeds gets
  "Make this a connected project" (`connected_projects.convert_existing`, manager and up): same
  connection, no lane, no schedule, refused for uploaded documents, inbox and archived projects.
  Nothing converts by itself on upgrade or boot (no migration touches a project).
- **Shape on bind.** A connected project is created as `document`. Binding github, gitlab or
  bitbucket (`connected_projects.KEYS_KINDS`) to one that is still empty sets `project_type` to
  `key_based`, the one allowed change of project type, only while empty and audited
  (`project.type_set`); a keys source on a project with documents, or a documents source on a key
  project that holds keys, is refused (`connected_projects.shape_for_bind`, called from `bind`).
  Key add, upsert and import are refused on a connected project on every surface, like document
  uploads. Adding a language to a bound project (add-locale, apply-langset) needs manager or above
  on the web and /api (so MCP and CLI `--remote`), 404 before 403; the in-process CLI has no roles.
  The Airtable and Jira OAuth start carries a validated `return_to` in the server-side
  OAuth session (`conn_oauth_return`), cleared with the other one-shot keys.

## 3. Where the work happens

| Concern | Pin | One line |
|---|---|---|
| Service: bind, schedule, run, settle | `app/services/project_automation.py::bind` · `app/services/project_automation.py::configure` · `app/services/project_automation.py::run_now` · `app/services/project_automation.py::tick` · `app/services/project_automation.py::execute_job` | Binding (direct, lane, clone), the schedule, the run, the claims and the outcome bookkeeping. |
| Connected projects | `app/services/connected_projects.py::set_intake` · `app/services/connected_projects.py::refuse_upload` · `app/services/connected_projects.py::card_state` · `app/services/connected_projects.py::safe_return_to` · `app/services/connected_projects.py::convert_existing` · `app/services/connected_projects.py::adopt_new_project` · `app/services/connected_projects.py::shape_for_bind` | The intake marker and its switch rule, the upload refusal, the read models for the project page's setup menu and Source card and the Projects list badge, the no-egress Preview, and the allow-list for the `return_to` that "Connect a new system" carries. |
| Schedule math | `app/services/automation_schedule.py::next_fire` | Pure UTC next-fire, validation and the 12-hour description. |
| Run switches | `app/services/connectors/run_ctx.py::scope` | The full-pull and claim switches the flow engine reads. |
| Web and /api | `app/web/project_automation.py` | The Automation page, its forms and the `/api/projects/{id}/automation`, `/source` and `/runs` routes. |
| CLI | `app/cli/cmd_project_automation.py` | `project bind`, `unbind`, `automation show/set/pause/resume`, `run`, `runs`. |
| MCP | `integrations/mcp-server/registry.py::TOOLS` | `project_automation` (read and non-spend writes; an agent cannot arm, resume, go full, change times, or clear or raise the per-run cap) and `project_run` (spend, plan then confirm). |

Hooks into modules other subsystems own (documented where they live, see cost-and-budgets and
jobs-and-scheduler): `app/services/spend.py::run_cap` is the per-run cost scope that blocks paid
calls once a run has spent its cap; `app/services/jobs.py::_run_connector_translate` routes a job
tagged with `automation_id` through `execute_job` (every other job is unchanged);
`app/services/scheduler.py::tick` ticks the project automations below the global kill switch.

## 4. Data & settings

- **Intake:** `app/models.py::Project` carries `intake` (default `upload`; one alembic revision and the
  `app/db.py` shim add the column). The Projects list filters on it (the `intake=connected` query, the
  CLI `--connected` flag and the MCP `list_projects` filter) and shows the bound connector kind, or
  "Not connected yet".
- **Models:** `app/models.py::ProjectAutomation` (one row per project: the picked and the run
  connection, schedule, mode, pause state, failure counter, run cap) and
  `app/models.py::ProjectBindingClaim` (which project owns one external item). A lane is an
  ordinary `app/models.py::Connection` with config `lane_of`, hidden by
  `app/services/connectors/store_common_leaf.py::list_connections`. One alembic revision creates
  both tables; neither is exported (classified in `app/services/data_export_part2.py`).
- **Settings:** `setting:project_automation_enabled` (default `1`) switches the feature off. It obeys `setting:sync_scheduler_enabled`,
  `setting:connector_disabled_kinds` and the project monthly budget.

## 5. Enforced by

- `verify_connected_projects` - creating a connected project on every surface, the intake switch
  rule, the upload refusal, the setup menu and Source card, the list filter and badge, `return_to`, the
  template rule and the customer isolation of step 1.
- `verify_project_automation` - schedule math, binding, configure, tick and settle, claims over
  the real flow engine, the lane credential, the run cap, the API, the page, the CLI and the MCP
  tool.
- `verify_schedule` - the connection-level schedule the project schedule coexists with.
- `verify_spend_cap` - the install-wide cap the per-run cap sits beside.
- `verify_flow_engine_parity` - a run with no automation switches is byte-identical.
- `verify_cli_parity`, `verify_mcp_parity`, `verify_kernel_facade`, `verify_project_scoping` -
  the four-surface ratchets.

## 6. Battle scars

- **A connection owns exactly one project's state.** The first design (a many-to-many binding
  table) would have shared content hashes and watermarks between projects and silently
  corrupted both; the lane keeps every connector's existing state handling untouched (see ADR 0082).
- **A queued job can finish before its submitter writes.** Recording "queued" after submit
  overwrote the real outcome; the submit path now writes only the job id and the page shows an
  in-flight job from the job itself.

## 7. Limits & accepted risks

- **Claims cover the flow-engine kinds only.** Airtable, Jira and the git kinds keep their own
  loops, so one connection of those kinds serves one project (a second binding is refused).
- **A full pull starts over each cycle.** The cursor (`ProjectAutomation.full_cursor`) lets a
  scope larger than the write ceiling be walked over several runs; items edited between runs of
  one cycle are picked up by the next cycle.
- **A run cap can overshoot by the calls in flight** when it is reached.
- **Run history scans the newest 2000 connector jobs** for the automation's tag, and a rotating
  refresh token can still race between the picked connection and a lane running together.
- **Notifications, custom fields, inbound triggers and workflow mode are separate work** (phase
  2 and parallel builders); this subsystem only records the run.
