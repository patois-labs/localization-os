# Configuration: Architecture (L1)

## 1. Role

Semantic TM backfill uses the same project authorization on the generic job API and
`/api/admin/semantic/backfill`. In multi-org mode, both require a visible project;
the web Setup AI install-wide backfill is refused. Unrestricted multi-org sweeps remain
available to the host CLI operator. Remote API launches also require admin scope,
admin role and `remote_admin_enabled`. The worker caps batches at 1000 rows.

This subsystem is **how the install is configured**: the environment and keyfile bootstrap
that runs before anything else, the runtime settings registry stored in the database, and
the preflighted enablement path for the features that must not be flipped over the wire. It is the
answer to "where does a default come from, who may change it, and does a change take effect
without a restart."

`app/config.py` is the pre-database spine. It reads `.env`, resolves the data directory,
database URL, and paths, decides the at-rest cipher from an environment flag, resolves the
two runtime secrets through the SecretStore port (documented in kernel-and-ports), and
guards a real data directory behind a protect marker so a test run can never write live
data. It works with zero keys set.

`app/services/settings.py` is the runtime registry: `DEFAULTS` is the single source of
every key that has a default, a read-through cache serves the hot path and probes for
cross-process writes so a CLI change reaches a running server within seconds, writes
through the settings API funnel through the `set_many` seam that version-bumps the cache,
and secrets are Fernet-encrypted at rest and never echoed. Except for app_base_url
(described below), value validation is not a
property of that seam: it is applied at the door before the write, not in the seam, and not
by every door (the doors that validate are listed in section 2; the rest apply their own
per-form bounds or none). Saved settings are pushed into the live `config` module so a change reaches
everywhere translation happens.

`app/services/enterprise_enablement.py` is the preflighted turn-on path for the boot-brick
features (OTel, metrics, SAML, SCIM, PII-NER, the durable job queue, the S3 chain): its
`enable` runs an offline preflight against the proposed values and refuses on red unless
forced. The remote doors funnel to it: the wire config door and the flag-admin seam both
refuse these keys (they sit on `REMOTE_IMMUTABLE_KEYS`, section 4), so a remote operator
cannot arm one of these keys through those two doors, and the safe turn-on is
`/settings/enterprise` or `enterprise enable`, unless the enterprise door is forced: its
`app/web/enterprise.py::enterprise_enable` reads a `force` form field that writes past a
red preflight (the same escape hatch as the forced enable in section 7). A host-local `config set` is not gated this
way: its `_set` handler in `app/cli/cmd_config.py` runs the value-validator registry, which
has no entry for these switch keys, and no preflight, so a host operator can write the
switch and produce a boot-brick (the same escape hatch as the forced enable in section 7).
Its web twin is `app/web/enterprise.py` and its CLI twin `app/cli/cmd_enterprise.py`;
`app/cli/cmd_config.py` is the general `config get/set/list/apply` surface plus the local
feature verbs.

It does **not** own the feature registry or the shared flag-write seam (modules-and-flags),
the audit chain that records operator config changes (audit-chain; the boot machine writes
in section 7, `ensure_actor_hash_salt` and the retired `org_name` delete, write no audit
row), or the immutable-key law's tenancy meaning (tenancy-confinement); this layer owns where a value lives, how it is
validated, and how enablement is gated.

## External URL authority

`setting:app_base_url` is the authority for every emitted external URL. The request
base URL, including its Host header, is the fallback when unset; with no request,
background notification links remain omitted. Callers use
`app/services/settings.py::external_base_url` or `app/services/settings.py::external_url`.
Explicit SAML SP EntityID/ACS overrides still win over that base. Connector push intake
retains its localhost-means-unset policy, and Airtable retains its HTTPS requirement.

The app_base_url entry in VALUE_VALIDATORS checks HTTP/HTTPS, a valid host and port,
and refuses credentials, fragments, queries, whitespace/control characters and
backslashes. A query cannot be a base to which route paths are appended. This is a
syntax check, not an egress check. This key is also validated inside set_many before
any write, covering the Security form and all other doors. Invalid legacy rows warn
once per episode and fall back exactly as unset, including at SAML boot. Repair them
on `/security` or with `config set app_base_url https://tms.example.com`.
A path prefix is allowed; writes strip whitespace and trailing slashes and lowercase
scheme and host. The configured path is the external prefix, replacing root_path.
No settings key or migration is added.

## 2. Invariants

- **A cross-process write reaches a running server.** `app/services/settings.py::get`
  probes a cheap version on a cadence so a CLI or second-process write propagates without a
  restart, and `app/services/settings.py::invalidate_cache` re-adopts the DB state and
  re-pushes to config. Enforced by `verify_settings_propagation`.
- **A write through the settings API version-bumps the cache; value validation is a
  per-door contract.** `app/services/settings.py::set_many` bumps the cache version on
  every write so readers pick the change up, and it refuses the internal version row and
  caps brandable names; it validates app_base_url, but not other values. Other value validation
  is enforced per write door, not inside the seam, and the set of validating doors has grown
  over time, so treat the list as a map, not a guarantee for a new door. The singular
  `app/services/settings.py::validate_value` runs on the wire config door (every key before any
  write), on `config set` (the `_set` handler in `app/cli/cmd_config.py`), in
  and at the admin forms
  in `app/web/admin.py` that save the default provider, the local engine URL and sampling, the
  accessibility contact and the login-security fields. The batch `app/services/settings.py::validate_values`
  runs in the `config apply` handler (`_apply` in `app/cli/cmd_config.py`), in the shared admin CLI
  helper `app/cli/cmd_admin_helpers.py`, and at two `app/web/admin.py` saves: the
  `POST /setup-ai/web-verification` save (`setup_ai_web_verification_save`) and the Security page
  session save. Other `set_many` callers do not call the shared registry validators; several apply
  their own per-form bounds instead (for example the spend-cap save refuses an over-cap value,
  `POST /setup-ai/tm` clamps the context floor and `POST /setup-ai/semantic` the semantic cosine,
  `POST /settings` clamps the glossary token cap and the retention save its retention days, and the
  segmentation save refuses a rejected SRX upload), and the `/api` identity and SMTP writers
  validate nothing. Enforced by `verify_settings_propagation` (the version
  bump); per-door validation is a design contract of those doors.
- **Secrets are encrypted at rest and never echoed.** `app/services/settings.py::set_secret`
  Fernet-encrypts, `app/services/settings.py::get_secret` decrypts and fails soft to empty,
  and the keys on `app/services/settings.py::SECRET_SETTING_KEYS` are masked on every read
  surface. Enforced by `verify_cloud_keys` (ciphertext at rest and the set/get round-trip)
  and `verify_cli` (the masked, never-plaintext read surface).
- **The `enable` turn-on door runs an offline preflight first; a host `config set` does
  not.** `app/services/enterprise_enablement.py::enable` refuses on a red offline gate unless
  `force`, but the preflight is a property of that door, not of the switch key: a host-local
  `config set` writes the same key with no preflight (section 1). Enforced by
  `verify_enterprise_enablement`. By design the deciding gate is the
  one `enable` computes with `run_network=False`, so an informational network probe is
  structurally excluded from the enable decision and can never widen or block it. This is a
  property of `enable`, not of `preflight` (whose own exclusion rule is only the
  advisory-name suffix, and whose non-advisory `for_gate` checks such as the S3
  chain-credential resolve do block). The suite does not separately assert it.
- **Disabling never runs a preflight.**
  `app/services/enterprise_enablement.py::disable` flips the enable setting and audits it
  without a preflight. Enforced by `verify_enterprise_enablement` (the no-preflight disable
  and the no-op arm). Leaving the config fields intact so a re-enable restores them is a code
  behavior of `disable`, not separately asserted.
- **A real data directory is guarded against a test write.**
  `app/config.py::assert_data_dir_allowed` and `app/config.py::create_protect_marker` keep
  a hermetic run off the live data directory. Enforced by `verify_data_protection`.
- **The two runtime secrets resolve env-then-keyfile through the port.**
  `app/config.py::_secret` and `app/config.py::_keyfile` resolve the session and encryption
  secrets. Enforced by `verify_secretstore_port` (resolution equivalence).
  `app/config.py::_keyfile` also persists a generated keyfile at 0600 best-effort, a code
  behavior that suite does not check.

## 3. Where the work happens

| Concern | Pin | One line |
|---|---|---|
| Maintenance and direct-work admission | `app/services/maintenance.py::MaintenancePass`, `admitted_work` | One active pass per modeled scheduler or retention parent; direct evaluation, LLMemory and job/report/export executions share run slots. Locks protect counters only. |
| Streaming workers | `app/web/streaming.py::StreamingResponse` | Every sync streaming body uses the dedicated LOC_TMS_STREAM_WORKERS limiter, leaving sync-route capacity free. Every route imports `StreamingResponse` from `app.web.streaming`, never from FastAPI or Starlette directly; a raw import bypasses the limiter and puts the stream's sync iterator back on the shared threadpool (the raw TM export did this once and turned the release sweep red). Pinned by `verify_memory_bound` (its stream-isolation arm). |
| Pre-DB bootstrap | `app/config.py::DATA_DIR` · `app/config.py::DB_PATH` · `app/config.py::DB_URL` · `app/config.py::DB_ENCRYPTION_ON` | The `.env`-driven data directory, database URL, and at-rest cipher decision. |
| Data-dir protection | `app/config.py::assert_data_dir_allowed` · `app/config.py::create_protect_marker` · `app/config.py::data_dir_is_protected` | The marker that keeps a test run off the live data directory. |
| Secret + keyfile resolution | `app/config.py::_secret` · `app/config.py::_keyfile` | Resolve the session and encryption secrets env-then-keyfile, persisting keyfiles at 0600. |
| Framing config | `app/config.py::FRAME_ANCESTORS` | The env-driven frame-ancestors policy (documented in browser-surface). |
| Capacity dials | `app/config.py::DB_POOL_SIZE` · `app/config.py::DB_MAX_OVERFLOW` · `app/config.py::DB_POOL_TIMEOUT_S` · `app/config.py::MAX_CONCURRENT_RUNS` · `app/config.py::_get_num` | The connection-pool sizing, its failure budget, the run-admission ceiling, and the boot-safe numeric reader they all use. |
| Capacity boot guard | `app/config.py::_pool_capacity_floor` · `app/config.py::DB_REQUEST_HEADROOM` · `app/config.py::MAX_CONCURRENT_RUNS_CONFIGURED` · `app/config.py::SQLITE_CACHE_KB` | The largest run ceiling the pool can carry, the request headroom it leaves, what the operator asked for before the clamp, and the per-connection page-cache budget. |
| The settings registry | `app/services/settings.py::DEFAULTS` · `app/services/settings.py::get` · `app/services/settings.py::all_values` · `app/services/settings_common.py::_REDACTED_URL_MARKER` | The single source of every key that has a default and the cross-process-coherent read path. |
| The settings write seam | `app/services/settings.py::set_many` · `app/services/settings.py::validate_values` · `app/services/settings.py::forget` · `app/services/settings.py::invalidate_cache` | The settings-API write seam (version bump, version-row refusal, brand cap), the batch validator the `config apply` handler and the one admin door (`POST /setup-ai/web-verification`) call, key deletion, and cache reset. |
| Secret settings | `app/services/settings.py::get_secret` · `app/services/settings.py::set_secret` · `app/services/settings.py::SECRET_SETTING_KEYS` · `app/services/settings.py::SENSITIVE_KEYS` | Fernet-at-rest storage and the masked/redacted key sets. |
| Immutable-key law | `app/services/settings.py::REMOTE_IMMUTABLE_KEYS` | The host-only key set the wire config door and the flag-admin seam both refuse against. |
| Push to live config | `app/services/settings.py::apply_to_config` | Pushes stored provider, model, and cloud-key values into the running `config` module. |
| Enterprise turn-on | `app/services/enterprise_enablement.py::enable` · `app/services/enterprise_enablement.py::disable` · `app/services/enterprise_enablement.py::preflight` · `app/services/enterprise_enablement.py::status_rows` | The offline boot-brick gate, the never-blocked disable, the preflight, and the status readout. |
| Operator-key step-up | `app/services/enterprise_enablement.py::feature` · `app/services/enterprise_enablement.py::feature_keys` · `app/services/enterprise_enablement.py::verify_operator_key` | The feature catalogue and the operator-key check the write path steps up on. |
| Enterprise web door | `app/web/enterprise.py::enterprise_page` · `app/web/enterprise.py::enterprise_enable` · `app/web/enterprise.py::enterprise_disable` · `app/web/enterprise.py::enterprise_preflight` | The `/settings/enterprise` panel with a two-rung view/act auth model. |
| Enterprise CLI | `app/cli/cmd_enterprise.py::register` | The `enterprise` group: status, on/off, preflight, no network on preflight. |
| Config CLI | `app/cli/cmd_config.py::register` | The cache-coherent `config get/set/list/apply/export-env` group over the same store the web UI writes. |
| Admin + Setup AI web | `app/web/admin.py::settings_page` · `app/web/admin.py::setup_ai` · `app/web/admin.py::settings_save` · `app/web/admin_common.py::PROMPT_TAP_PAGE_ROWS` · `app/web/admin_part2.py::_seg_redirect` · `app/web/admin_part3.py::_engine_cfg_from_form` | The users/security/audit/settings and Setup AI admin pages, which write the same settings store the config surfaces above own. |

## 4. Data & settings

- **Model:** `app/models.py::Setting` (documented in data-and-migrations) is the key/value
  store behind the whole registry; secrets are stored as Fernet tokens under their own keys.
- **The immutable-key set is load-bearing:** keys on
  `app/services/settings.py::REMOTE_IMMUTABLE_KEYS` (for example `setting:multi_org_enabled`,
  `setting:saml_enabled`, `setting:scim_enabled`, `setting:job_queue_backend`) are settable
  on the host only, so no `/api` config route may flip them; the flag-admin seam
  (modules-and-flags) reads the same set.
- **The install-wide refused-extra set is host-only under multi-org:**
  `app/services/settings.py::ORG_ADMIN_REFUSED_EXTRA` is the group of install-wide keys that sit
  outside the confirm and host-only groups. With `setting:multi_org_enabled` on, the `/api` config
  door and the web Security page refuse them to an org admin
  (`app/services/settings.py::security_setting_keys` unions them in), so only the host operator
  writes them. Besides the shared connector token and the global spend brake it now carries the
  custom SRX ruleset (`srx_enabled`, `srx_file`, #424), the install-wide egress targets and shared
  credentials (`smtp_port`/`smtp_tls`/`smtp_user`/`smtp_password`/`smtp_from`,
  `oauth_airtable_*`, `oauth_atlassian_*`, #426), plus `enabled_locales` (#479). A
  strict no-op with the gate off. Enforced by `verify_multiorg_install_wide_files`.
- **The at-rest cipher is env, not a setting:** `config.DB_ENCRYPTION_ON` (env
  `LOC_TMS_DB_ENCRYPTION`) decides SQLCipher before boot, documented in secrets-and-crypto.
- **Capacity is env, not a setting, and the dials are one design point.** Six environment
  values size how much work this process admits. They are read before the database exists
  (the pool is what a `Setting` read would need), so they cannot be `Setting` rows, and each
  is read through `app/config.py::_get_num`, which warns and falls back on a blank or
  malformed value rather than raising at import time - a typo in a deployment secret must not brick
  boot.

  | Environment variable | Default | What it does |
  |---|---|---|
  | `LOC_TMS_DB_POOL_SIZE` | 20 | Connections the SQLAlchemy pool keeps. |
  | `LOC_TMS_DB_MAX_OVERFLOW` | 40 | Extra connections it may open under burst. |
  | `LOC_TMS_DB_POOL_TIMEOUT_S` | 5.0 | How long a caller may WAIT for one before failing. |
  | `LOC_TMS_DB_REQUEST_HEADROOM` | 10 | Connections reserved for ordinary request traffic, not runs. |
  | `LOC_TMS_MAX_CONCURRENT_RUNS` | 8 | Workflow executions admitted at once; 0 disables the gate. |
  | `LOC_TMS_SQLITE_CACHE_KB` | 1000 | SQLite page cache per CONNECTION, in KiB - the memory half of the pool sizing. |
  | `LOC_TMS_GIL_SWITCH_INTERVAL_S` | 0.0005 (Linux), 0.005 (Windows) | Seconds between Python GIL hand-offs (`sys.setswitchinterval`, CPython default 0.005). Lower keeps ordinary requests fast while a big import/export/search runs in another thread (#615); raise it only to trade latency for a little throughput. Windows keeps CPython's 0.005 by default because 0.0005 made concurrent CPU-bound threads 50 to 90 percent slower there (Linux: no measurable cost); set it explicitly to override. |
  | `LOC_TMS_READY_PROBE_TIMEOUT_S` | 2.0 | The `/ready` budget (documented in deployment). |
  | `LOC_TMS_ENGINE_PROFILE_CACHE_TTL_S` | 5.0 | Cross-process staleness bound on the engine-profile memo; 0 disables it (documented in engines-and-providers). |

  The rules that tie them together, applied by `app/db.py::engine` and
  `app/services/orchestrator.py::RUN_SLOTS`: every in-flight run pins ONE pooled connection
  for its whole duration and borrows a second for its own worker threads, so pool size plus
  overflow must be at least `MAX_CONCURRENT_RUNS x DB_CONNECTIONS_PER_RUN` (2) with the
  headroom on top; `DB_POOL_TIMEOUT_S` is a FAILURE budget rather than a queue, because a
  caller waiting on a checkout pins a worker thread (or the event loop) for its whole
  length; and raising `MAX_CONCURRENT_RUNS` without raising the pool with it reintroduces the
  exhaustion the gate exists to prevent. Enforced by `verify_stress_liveness_pool`.

  **The relationship is checked at boot, not left to the operator.**
  `app/config.py::_pool_capacity_floor` computes the largest run ceiling the pool can carry
  - `(DB_POOL_SIZE + DB_MAX_OVERFLOW - DB_REQUEST_HEADROOM) / DB_CONNECTIONS_PER_RUN`, i.e.
  two connections per in-flight run with `DB_REQUEST_HEADROOM` left over for page loads,
  `/api` polls, SSE observers and the job daemons - and a `MAX_CONCURRENT_RUNS` above it is
  CLAMPED with a warning naming the numbers, the dial to turn, and the memory consequence.
  This is the SAME rule `verify_stress_liveness_pool` [A] asserts of the live configuration;
  the guard is strictly stronger because it also holds back the headroom.
  `MAX_CONCURRENT_RUNS_CONFIGURED` keeps what the operator asked for, and `/metrics` exposes
  both as `loc_tms_run_cap_configured` and `loc_tms_run_slots_cap`, so a clamp is visible
  rather than merely effective. Clamping rather than refusing to boot is deliberate: an
  install that starts today must keep starting after an upgrade.

  Worked examples, at the shipped `DB_MAX_OVERFLOW=40` / `DB_REQUEST_HEADROOM=10`:

  | Configuration | Ceiling | Effect |
  |---|---|---|
  | defaults (`POOL_SIZE=20`, cap 8) | `(20+40-10)/2 = 25` | 8 is under it; nothing is clamped |
  | `POOL_SIZE=20`, `MAX_CONCURRENT_RUNS=60` | 25 | clamped to **25**, with a warning |
  | `POOL_SIZE=5`, `MAX_CONCURRENT_RUNS=60` | `(5+40-10)/2 = 17` | clamped to **17** |
  | `POOL_SIZE=5`, `MAX_OVERFLOW=0`, `MAX_CONCURRENT_RUNS=60` | `max(1, (5+0-10)/2) = 1` | clamped to **1** |

  **Capacity delays work; it never cancels it.** A launch beyond the ceiling is refused with
  a retryable 503, but a FAN-OUT wider than the ceiling - a key project's "translate all"
  over (locales x namespace documents), or a batch wider than the free capacity - is
  **queued**, not refused: `app/services/orchestrator.py::_AdmissionScheduler` takes one slot per
  remaining launch as earlier runs finish, and the surfaces report those launches as
  `queued`. All fan-out rows are durable before admission; zero free slots queues the whole plan.

  **What the gate counts, exactly.** Counted AND bounded: single launches, every batch
  member (including re-runs and retry-failed), every key fan-out launch, and Advanced
  missions, within one process. Children share their
  parent's lifetime: MT and Advanced delegate executors are capped by RUN_WORKERS and
  included in the memory estimate below. The historical two-connection allowance is a
  planning factor, not a per-run connection quota. Size DB_POOL_SIZE and headroom for
  delegates too. Other direct/async callers remain exclusions from
  the total bound; [issue #109](https://github.com/patois-labs/localization-os/issues/109)
  remains open.

- **Sizing the pool is also sizing MEMORY, and the arithmetic is published.** SQLite's page
  cache is per CONNECTION, so total worst-case cache is
  `(DB_POOL_SIZE + DB_MAX_OVERFLOW) x SQLITE_CACHE_KB` and the steady state is
  `DB_POOL_SIZE x SQLITE_CACHE_KB`. At the defaults that is **60 x 1000 KiB ≈ 59 MiB peak,
  20 x 1000 KiB ≈ 20 MiB steady**. `app/db.py::_sqlite_pragmas` sets `PRAGMA cache_size`
  explicitly from `app/config.py::SQLITE_CACHE_KB` rather than inheriting SQLite's own
  `-2000` default, which at this pool size would be ~120 MiB peak on the 1 GB machine the
  2026-09-06 flood OOM-killed. Raising the pool without lowering the cache raises resident
  memory proportionally - decide both together. This bounds the CACHE only; the broader
  memory class (worker threads accumulating behind long engine calls, unbounded document
  parsing) is [GitHub issue #109](https://github.com/patois-labs/localization-os/issues/109)
  and is NOT closed by this train.

## 5. Enforced by

- `verify_settings_propagation`: the cross-process cache coherence, the version-bumping
  write seam, and the push-to-config contract.
- `verify_cloud_keys`: a secret Fernet-encrypted at rest (ciphertext, not plaintext) and the
  set_secret / get_secret round-trip.
- `verify_cli`: a secret key masked on the `config get` read surface, never echoed as
  plaintext.
- `verify_data_protection`: the data-dir protect marker that keeps a hermetic run off the
  live data directory.
- `verify_enterprise_enablement`: the offline boot-brick gate, the never-blocked disable,
  the operator-key step-up, and the status rows across web and CLI.
- `verify_secretstore_port`: the env-then-keyfile resolution of the two runtime secrets
  `app/config.py` performs through the port.
- `verify_stress_liveness_pool`: that the pool is sized from these values rather than a
  library default, that total capacity exceeds the run-admission ceiling, and that exceeding
  the ceiling is refused with a named, retryable 503 that writes nothing.

## 6. Battle scars

- **A CLI setting change could not stop a feature on a running server.** The read-through
  cache was coherent only for in-process writes, so an SSH session plus CLI left a running server
  serving a switched-off feature until restart; the fix added the cross-process version
  probe.
- **The safe turn-on moved behind a preflighted door.** Each boot-brick feature ships
  flag-off and immutable, so a naive on-flip could produce an install that refuses to boot;
  the offline preflight and the operator-key step-up made the remote turn-on safe behind one
  preflighted door across web and CLI (a host `config set` still writes past it, section 1).
- **A docs-scope key once reached install-wide config writes.** The R3 config spine now
  refuses a feature key and an immutable key over the wire, closing a privilege-escalation
  path a wire-level sweep passed by design.

## 7. Limits & accepted risks

- **Cross-process propagation is a probe, not a push.** A change reaches a running server
  within the probe interval, not instantly, an availability-over-immediacy trade stated in
  the settings module docstring.
- **A rotated or wrong encryption key reads a secret as empty.**
  `app/services/settings.py::get_secret` fails soft rather than crashing a login or settings
  page, so a mis-keyed secret looks unset and is re-entered, stated in the function.
- **A forced enable can produce a boot-brick.**
  `app/services/enterprise_enablement.py::enable` with `force` writes past a red preflight on
  purpose, so the next restart may refuse to boot, a deliberate operator escape hatch.
- **Non-registry bookkeeping rows are written directly.** Other modules park
  non-registry bookkeeping rows straight in the `app/models.py::Setting` table, outside the
  seam: the glossary repair markers in `app/db.py`, the audit anchor and chain-status rows in
  `app/services/audit.py` and the data-export reap-status row in `app/services/data_export.py`;
  none of those are `DEFAULTS` keys. One registry key is also mutated outside the seam by deletion rather
  than a write: `app/db.py::_fixup_retired_org_name` DELETEs the `org_name` key with direct
  SQL on every boot when it holds a retired product name, outside both `set_many` and
  `app/services/settings.py::forget` (the deletion seam this doc's section-3 row names), then
  hand-bumps the settings version.

## Local provider credential display

Setup AI renders an empty password field with key set / no key / default
(lm-studio) status. Blank saves retain the stored local key; Clear key stores an
empty value. The provider can still use its lm-studio placeholder when no key is
configured. Model/test probes reuse a saved credential only for the saved URL;
an unsaved destination needs an explicit credential. Local-test errors redact the
credential before returning JSON. No fingerprint or plaintext enters the HTML.

`app/services/settings.py::display_value` is the shared masked settings read seam
for local CLI config get/list, the R3 config API and MCP get_config.
Generic config get/list refuse --remote on this checkout.
`app/services/settings.py::local_key_status` supplies the web status through that
seam. The local key's existing plaintext-at-rest storage is unchanged; this train
closes read disclosure. `verify_ingress_boundary` covers display, save/clear,
transport authentication and captured logs.

## Queue admission and emergency relief

Queue admission waits are bounded by `LOC_TMS_QUEUE_DRAIN_WAIT_S` (default 600 seconds,
10 minutes). After that deadline a synchronous waiter that finds the batch's terminal
transition already owned by another thread waits on that owner's completion signal,
bounded by `_SYNC_OWNER_WAIT_S` (twice the pool-checkout timeout) and retried at most
`_SYNC_FINALIZE_ATTEMPTS` (3) times, so the reachable wait past the deadline is
`LOC_TMS_QUEUE_DRAIN_WAIT_S` plus up to 3 x `_SYNC_OWNER_WAIT_S` before it records
`drain_incomplete`. That owner hand-off holds no pooled connection while it waits.
Synchronous batches drive admission on the calling thread and join all
admitted member threads. Waiting wakes on member joins; otherwise reconciliation reads
members with at most one checkout every two seconds, using the emergency engine when
the pool is saturated. Completion is checked before returning an error on every path.
Only a failed reconcile read classified as pool exhaustion returns `db_pool_exhausted`
with a durable batch note and the queued member ids. Other failures return
`wait_internal_error` with the exception message and a logged traceback.
Expiry also bounds RUNNING/PENDING rows with no local member thread, including work
owned by another process: it records `drain_incomplete` without cancelling that work.
Expiry returns a running batch with an explicit `error`
(`drain_incomplete`) naming queued member ids and the capacity deadline reason; CLI
exits non-zero. Already admitted executions are joined even after this queue deadline.
Completed batches finalize even at pool capacity and no longer expose old wait errors.
Result serialization reads only the caller's session. A synchronous result needing
relief binds that entire read to the bounded relief pool, including batch attributes
and member details; ordinary pure reads never open emergency sessions.

Emergency relief admits at most four concurrent requests and four simultaneous
request relief sessions (`db.RELIEF_MAX_SESSIONS`), with no checkout wait.
A separate, unbounded, blocking NullPool engine records crash and recovery notes;
request relief permits can never refuse those bookkeeping writes. Only GET /settings and
the four POST run-cancel routes qualify, and only when the ordinary pool is saturated.
Healthy GET /settings uses the ordinary pool. Excess relief requests receive retryable
503 responses. The admission supervisor checks every lifecycle scheduler interval,
logs dead daemons at ERROR, and retries startup at most once per 30 seconds.
GET /health detail.admission_daemon reports alive, restarted, or dead without DB work.

## Memory sizing (#109, round 1)

Host-only environment settings are read at process start. `config list --group capacity`
shows effective values; `config set` cannot change them. Restart after editing the environment.

| Variable | Default | Meaning |
|---|---:|---|
| `LOC_TMS_ANYIO_WORKERS` | 16 | Anyio default thread limiter tokens per app event loop for sync routes and blocking calls; streams and long waits are off it. Excess calls wait. |
| `LOC_TMS_STREAM_WORKERS` | 32 | Dedicated stream and long-wait limiter per event loop; one token per open SSE tab or waiting caller. Admission is bounded by LOC_TMS_STREAM_WAIT_S. Also caps an independent process-wide pool of provider stream pumps (32 by default); each pump queues at most LOC_TMS_RUN_WORKERS chunks (default 4) and fails with ProviderStalled after PROVIDER_TIMEOUT_S of consumer backpressure. |
| `LOC_TMS_STREAM_WAIT_S` | 5 | LOC_TMS_STREAM_WAIT_S defaults to 5 seconds and bounds stream-token admission. Saturated wait=true returns the non-waiting response with current status, waited: false and retry_after; poll the run or batch status. Saturated SSE returns HTTP 503, code stream_capacity and Retry-After, before response headers start. The shared refusal is: Stream capacity is busy. Retry shortly; existing work continues. CLI and MCP relay that message verbatim. Size LOC_TMS_STREAM_WORKERS above the expected watcher count: one token per open SSE tab or waiting caller, held up to 900 seconds per silent SSE next() or 3600 seconds per run wait, including disconnected non-abandonable workers. |
| `LOC_TMS_RUN_WORKERS` | 4 | Children per MT or Advanced delegate executor. Lower workflow parallelism still wins. |
| `LOC_TMS_JOB_WORKERS` | 2 | Background job consumers, shared by connectors, translation and data exports. This is the global cap on concurrent jobs; an engine's `max_parallel_jobs` (jobs-and-scheduler.md) only lowers translate concurrency beneath it. Raising this value also lets every engine without its own `max_parallel_jobs`, paid ones included, run that many jobs at once. |
| `LOC_TMS_MEMORY_BUDGET_MB` | unset | Optional MiB sizing guard. Unset or 0 does not refuse boot. Positive values refuse an excessive modeled sum or unlimited runs. |

The sizing guard checks requested settings before the connection-pool clamp. It is an
estimate, not an OS RSS limit. Input sizes, Python allocator retention, native libraries
and uncovered callers below can exceed it. A passing guard does not prove arbitrary
uploads or all features fit on a 1 GB host.

For one process, R = MAX_CONCURRENT_RUNS, A = ANYIO_WORKERS, S = STREAM_WORKERS, J = JOB_WORKERS,
W = RUN_WORKERS, P = DB_POOL_SIZE + DB_MAX_OVERFLOW and C = SQLITE_CACHE_KB.
Count K = R + A + S + J + 2 working parents, including two maintenance callers.
Within R: classic/fan-out/batch/Advanced + E + L + D <= R, where E is direct
evaluation execution, L is direct LLMemory distill/janitor and D is direct
job/report/export execution. These share R; they are not extra allowances.
Thread allowance T = 6 + 2 + A + 2S + J + 2R + KW. The second S counts provider pumps.
The extra 2 covers direct maintenance drivers while the two daemon threads sleep.
Six covers main, readiness, admission,
progress, scheduler and retention. Two per run covers execution and batch member
waiters. Parent and worker allowances deliberately overlap for synchronous Advanced
launches. Each admitted mission owns its executor until shutdown, before slot release;
its children cost at most R x W threads and do not compete for their parent's run slot.

Provider pumps have a separate process-wide S limit, including abandoned upstream reads.
The semaphore binds STREAM_WORKERS at module import; capacity settings are boot-time
constants and changing the configured ceiling requires a process restart.
Their queues hold at most W chunks each: S*W = 128 queued chunks at defaults,
plus one producer-held chunk per pump. A full queue blocks the producer for at most
PROVIDER_TIMEOUT_S (default 120 seconds), then closes the source and records
`ProviderStalled("consumer stalled: provider chunk queue full")` out of band.
The consumer drains already queued chunks and then receives the failure on resumption;
it never receives a successful end-of-stream for dropped output. A consumer that never
resumes cannot receive an exception, but its backpressured pump still exits.
Consumers wait for chunk/completion notification without periodic polling. A full-queue
producer checks consumer abandonment at most every 50 ms while preserving the cumulative
PROVIDER_TIMEOUT_S overflow deadline. Closing the consumer signals the producer to stop.
A source close error after normal exhaustion does not turn delivered output into failure;
an existing provider/overflow error remains authoritative. Python cannot interrupt an upstream
`next()` or source `close()` that never returns: such a pump keeps its reservation until
exit. Further pump starts at the ceiling fail with
`ProviderStalled("provider stream pump capacity exhausted")`. Chunk sizes remain outside
the byte estimate, so the queue limit is an item bound, not an invented byte allowance.

Scheduler `tick` and retention `purge` each reserve one of the two modeled maintenance
parents for their entire pass, including child sweeps. Overlap is non-blocking:
direct `tick` returns `None`, `tick_now` returns `(False, None)` (the existing 409/busy
surface), and `purge` returns `{"busy": true}`. A subsequent pass can run after completion
or failure. No admission lock spans a session, dispatch, join or audit write.
The API purge route maps busy to HTTP 409 with `ok=false`, `busy=true` and
`error="a retention purge is already running; try again shortly."`, without a
purge or generic mutation audit row. Local CLI emits that one-line refusal and exits 1.
The remote CLI HTTP adapter and MCP HTTP relay preserve the refusal message.
The retention CLI verb remains host-only and no destructive retention MCP tool exists.
For the embedded external-cron profile, `TimeDriver.sweep()` returns the same
`{"busy": true}` outcome: no sweep ran; retry on a later cron invocation.

`maintenance.admitted_work` reserves one R slot before the direct service body,
including row creation. It covers evaluation `run_experiment`, `start_run` and
`execute_run`; LLMemory `distill` and `janitor`; jobs `run_sync` and `_execute`;
data_export `_execute_job` and `run_export`; reports `run_report` and `to_csv`;
export `build_translated_file`; and databricks_export `run_export`.
Nested synchronous calls share one reservation through outermost completion or
failure. Ownership is thread-local and never copied into independent execution.
An async waiter's cancellation cannot release work still running on its worker.
Scheduler and retention children share their existing maintenance parent, and
job-consumer children share J, so saturation of R does not refuse these parents.
There is no scheduled evaluation launcher in this checkout.

Saturated direct calls raise the existing `RunCapacityError`. Web responses are
503 HTML; API responses are 503 JSON with `ok=false`, `reason=run_capacity`,
`error` and `retry_after`, plus Retry-After. Local CLI reports a nonzero exit
with the same message (JSON also includes `status=503`, `code` and `reason`);
remote CLI preserves the API message and nonzero exit; MCP raises ToolError.
Report and export refusals name the requested operation. On a cap-1 install,
one long run blocks report/CSV and document/project export downloads until it
finishes. These operations retain admission because they allocate real memory.
Wait for the running work to finish and retry the download; clients should honor
the `Retry-After` header (currently 15 seconds) between retries.
The evaluation and LLMemory routes retain their authorization and confirmation
checks before execution admission. No new setting or worker pool is introduced.

The explicit sum in `app/config.py::memory_estimate_mb` is:

```text
resident sizing estimate (MiB) = P*C/1024 + T*8 + K*32 + 256
                                cache     stacks work   baseline
```

The 8 MiB stack allowance is conservative accounting, not measured committed stack or
`threading.stack_size`. The 32 MiB working allowance and 256 MiB baseline are planning
reserves. The registered mock measurement and measured values were kept with the build record.
The default stream allowance alone is 32 x 8 MiB = 256 MiB of thread-stack reserve,
included in T x 8 below, plus the existing conservative parent and child allowances.
One token serves one open SSE tab or waiting caller. Every profile retains the default 32 stream workers.
Each synchronous stream keeps one admitted worker through producer finalization.
That worker opens and closes its poll/export Sessions; Session teardown never runs
on the event loop. Cleanup signals the existing worker, without acquiring another
token or queuing behind other streams. An in-flight next() finishes before release.
The handoff holds at most one next request and one stop signal, with no chunk backlog.
The 2 GB and 4 GB planning sums exceed host RAM, so their boot budgets stay unset
until measured profiles exist; the estimates do not justify reducing shipped capacity.
SQLite uses `cache_size=-C` and `mmap_size=0` on pooled and emergency connections.
Steady pooled cache is pool_size*C/1024; overflow connections close on return.
Emergency connection count retains the existing caller policy, with no new refusal
or blocking lock in bookkeeping.

Per-install-size planning profiles with external LLM inference:

| Install | R | Pool + overflow | C KiB | A / S / W / J | K / T (unmeasured planning reserve) | Explicit MiB sum (unmeasured planning reserve) | Budget MiB |
|---|---:|---|---:|---|---|---|---|
| 1 GB shared-cpu VM | 8 | 20 + 40 | 1000 | 16 / 32 / 4 / 2 | 60 / 346 | 58.6 cache + 2768 stacks + 1920 work + 256 baseline = 5002.6 | unset; no measured profile |
| 2 GB | 4 | 20 + 20 | 1024 | 8 / 32 / 2 / 2 | 48 / 186 | 40 cache + 1488 stacks + 1536 work + 256 baseline = 3320 | unset; no measured profile |
| 4 GB | 8 | 20 + 40 | 1000 | 16 / 32 / 4 / 2 | 60 / 346 | 58.6 cache + 2768 stacks + 1920 work + 256 baseline = 5002.6 | unset; no measured profile |
| 8 GB desktop | 16 | 64 + 64 | 1024 | 24 / 32 / 4 / 2 | 76 / 434 | 128 cache + 3472 stacks + 2432 work + 256 baseline = 6288 | 8192 |

The same per-install rows now include all three newly admitted unit classes:

| Install | E evaluation | L direct LLMemory | D direct job/report/export | Combined run + E + L + D ceiling | Scheduled allowances |
|---|---|---|---|---:|---|
| 1 GB shared-cpu VM | shares R | shares R | shares R | 8 | J=2; maintenance=2 |
| 2 GB | shares R | shares R | shares R | 4 | J=2; maintenance=2 |
| 4 GB | shares R | shares R | shares R | 8 | J=2; maintenance=2 |
| 8 GB desktop | shares R | shares R | shares R | 16 | J=2; maintenance=2 |

Admission moves E, L and D inside the existing R reserve, so K, T and the explicit
MiB sums above are unchanged. No row grants each class an independent R ceiling.

Table note: the 1 GB row preserves main's current defaults, cap 8 and pool 20/40.
Recommendations must not go below those defaults until a measured profile exists.
Its estimate exceeds physical RAM because the reserves are unmeasured, not because a
production profile has been measured to require that sum. No budget is imposed on a 1 GB VM.

| Evidence | MiB | Method / status |
|---|---|---|
| Stack per modeled thread | 8 | unmeasured planning reserve; not committed stack |
| Working set per modeled parent | 32 | unmeasured planning reserve |
| Baseline | 256 | unmeasured planning reserve |
| Eight concurrent mock runs, peak growth | 46 to 50 MiB | Measured Windows Python 3.13.9 process WorkingSetSize via GetProcessMemoryInfo, sampled every 10 ms; warm app baseline, 16 segments/run, first provider call held at a barrier, then completion and recovery, plus 16 keys x 3 locales fan-out and HTTP/CLI/MCP overhead. Two observations: 46.3 and 50.4 MiB. |

The mock result is not a production sizing coefficient. Arbitrary input size and native
inference were not measured. The full measurement record remains in the round-1 reply.
These are conditional sizing profiles, not a complete worst-case RSS guarantee.
Other direct helpers outside the admitted entry points and maintenance helpers
invoked outside the guarded tick/purge parents remain outside a proven total
admission bound. Multiple OS processes do not share these counters. The job
queue retains IDs in memory; durable rows do not bound that queue. The 8 GB desktop
allowance excludes local model weights and inference-server memory. Issue #109 stays
open until these exclusions and workload-size bounds are resolved.
