# Deployment: Architecture (L1)

## 1. Role

This subsystem is **how the app runs in production**: the container health surface a proxy
health-checks, the opt-in structured logging the server profile emits, and the tailnet
egress seam that lets a cloud machine privately reach a self-hosted model server. It is the
architecture behind the operator guide in [`docs/DEPLOY.md`](../DEPLOY.md), which stays the how-to;
This doc is the how-it-works and the constraints that shape it.

## Where production runs

A hosted install is one VM running two containers on one docker network: the app, and a reverse
proxy (Caddy) in front of it. A CDN or edge proxy can sit in front of the reverse proxy; the
reference kit in `deploy/aws-hosted` shows the Cloudflare shape, in which the security group admits
ports 80 and 443 only from the edge ranges and Caddy refuses any request that lacks the
edge-injected origin header. The app trusts exactly one proxy peer, Caddy's fixed address, through
`setting:trusted_proxy_ips`, so the client IP in the audit log comes from the edge through Caddy (the
trusted-proxy rule is in [identity and access](identity-and-access.md), decision
[0037](../decisions/0037-trusted-proxy-ips-measured.md)). If the host reaches a model server over a
tailnet, Tailscale runs on the host, not in the app container, so the container reaches the model
server through the host and the bundled tailscale binaries stay inert.

Releases are built on the host from a clean archive of one commit, scanned by exact image ID with
`scripts/scan_image.sh`, booted as a check-only canary on a copy of the live data, and only then
swapped in, with the previous container kept as `-old` for rollback (decision
[0007](../decisions/0007-release-identity-and-clean-deploys.md)). The generic hosted-instance kit is in
`deploy/aws-hosted/aws-redeploy.sh` with the Cloudflare proxy config in
`deploy/aws-hosted/caddy/Caddyfile.cloudflare`. Run the full suite sweep before every deploy.

The shipped topology is a **single machine**. Localization OS is a modular monolith on one
SQLite (or Postgres) core, so a production deploy is one FastAPI process behind a reverse
proxy, with the durable job queue and Postgres as the multi-instance path (documented in
jobs-and-scheduler and data-and-migrations). The proxy contract is two probes:
`app/web/health.py` serves an instant no-DB liveness answer and a DB-coupled readiness
answer, mounted in every profile including the embedded one that drops the whole web
surface.

Logging is opt-in and non-invasive. `app/logging_config.py` leaves the default text output
untouched, so every verify suite and a normal local run keep human-readable logs; setting
the server profile's JSON format swaps in a structured formatter for log aggregation. In
either format the HTTP client libraries never log a request URL.

Local engine requests pass `egress.validate_local_engine_target` through
`providers.local_engine_client_kwargs` before proxy kwargs are built. The SOCKS
path is validated too: metadata, link-local, unspecified and multicast targets
are refused; LAN, loopback and tailnet hosts remain usable. Unresolvable LAN names
are allowed. See [the fixed deny set and accepted CGNAT/Alibaba overlap](secrets-and-crypto.md#local-engine-egress-door).

The tailnet seam is a narrow, opt-in egress rule. `app/services/tailnet.py` routes LOCAL
and self-hosted engine HTTP through a Tailscale sidecar's SOCKS5 proxy, and only for tailnet
destinations, so a cloud machine can privately reach a home model server without exposing it.
On the AWS hosted deployment Tailscale runs on the host instead, so the sidecar is not used there.

It does **not** own the runtime secrets and paths bootstrap (configuration), the security
headers and proxy trust middleware (browser-surface and the spine `app/main.py`), or the
backup and restore lifecycle (backups-and-dr); this layer owns the container-facing
surfaces and the deploy-time egress and logging seams.

Batch recovery: `loc-tms db recover-jobs` is an install-wide local sweep. It recovers jobs
through their queue adapter, heals plain runs through their thread/activity guards, and
finalizes batches only when their persisted dispatcher PID on this host is confirmed-dead.
It fails interrupted RUNNING/PENDING members and preserves PAUSED members. The result reports
actual recovered counts. Live processes, reused PIDs, foreign hosts, unreadable ownership
and legacy batches without an owner are left untouched. A dead dispatcher thread inside a
live process also stays untouched. Only after confirming all workers have stopped, restart
the server to recover those unknown-owner batches. Startup passes `at_boot=True` explicitly;
a bare batch recovery call is a no-op. New launches and accepted re-runs persist process
ownership before any member can launch, including the lazy-member window.
Durable QUEUED members are preserved for admission and keep their batch unfinished. At boot,
those batches adopt the new process owner before admission resumes, so a later manual sweep
cannot mistake newly admitted work for the previous process's orphaned members.
Boot recovery deliberately ignores dispatcher_owner under the single-instance, install-wide restart assumption, so a restart can fail a live foreign-host dispatcher's members and release delivery holds; multi-host awareness remains an accepted single-node-first residual.

## 2. Invariants

- **Liveness never touches the database; readiness deliberately does.**
  `app/web/health.py::health` returns instantly without a DB call, and `app/web/health.py::ready`
  runs one SELECT so an unreachable DB returns 503, the whole point of a readiness probe. This
  DB-coupling split is a design property stated in the `app/web/health.py::health` and
  `app/web/health.py::ready` docstrings; the 503 arm is exercised by
  `verify_stress_liveness_pool` (`verify_embedded_profile` proves only that the probes stay
  mounted and answer).
- **A probe answers, and answers promptly, whatever the app is doing.**
  `app/web/health.py::health` is a pure `async def`, so it never enters the shared anyio
  worker threadpool and cannot queue behind blocked application handlers;
  `app/web/health.py::ready` runs its query on its own dedicated thread
  (`app/web/health.py::_probe_pool`) under `app/config.py::READY_PROBE_TIMEOUT_S` and returns
  503 rather than blocking past the proxy's check budget. Neither path reads a setting: both
  are skipped by the feature gate (`app/main.py::_LIVENESS_PATHS`) and short-circuit the auth
  wall before it reads one. Enforced by `verify_stress_liveness_pool`.
- **A capacity refusal never needs the database to render.** `app/main.py::_retryable_503`
  answers JSON on `/api` and `app/web/deps.py::static_error_page` - a self-contained page
  with no template, no Jinja globals and no settings read - on the web, and
  `app/web/deps.py::_error_page` falls back to it whenever the request has already spent
  its pool-wait budget. An error page that reads the database is unavailable in exactly the
  failure it is being shown for. `app/main.py::_CapacityMiddleware` is the outermost user
  middleware: it opens the request's pool-wait budget and sheds a request with that 503
  when `app/db.py::pool_has_no_capacity` reports every connection checked out, so a
  saturated install answers in milliseconds instead of parking a worker thread. Enforced by
  `verify_stress_liveness_pool`.
- **The health probes exist in every profile.** `app/web/health.py::router` is mounted in
  all modes, including the embedded profile that drops the Jinja web surface, so a proxy
  always has its liveness answers. Enforced by `verify_embedded_profile`.
- **Structured logging is opt-in and default-neutral.**
  `app/logging_config.py::configure_logging` leaves the root logger alone under the default
  text format, so the human-readable output every suite runs under is unchanged. Enforced by
  `verify_ops`.
- **No HTTP client request line reaches the log.** In every format,
  `app/logging_config.py::quiet_http_client_loggers` floors `httpx` and `httpcore` (and
  `urllib3`) at WARNING and the LLM SDK loggers at INFO, because httpx prints the full request
  URL at INFO. Provider keys and customer text also stay out of URLs: Google Translate and the
  Gemini model list send the key as a header and the text in the body (#302). The app's own
  access log is scrubbed the same way: `app/logging_config.py::AccessLogRedactFilter` sits on
  the `uvicorn.access` logger in every format (installed by `configure_logging`, and by
  `run.py`'s uvicorn log config), drops the whole query string (search text such as the
  editor's concordance `q`, OAuth and OIDC `code`/`state`) and blanks the invite token in
  `/auth/accept/<token>` at any `root_path`/mount prefix (the match is the `auth/accept`
  segment boundary at any depth, not a leading anchor, and everything after that boundary
  is blanked; #402). Enforced by `verify_ops` and `verify_engines`.
- **Tailnet routing is destination-scoped.** `app/services/tailnet.py::is_tailnet_host`
  gates `app/services/tailnet.py::tailnet_client_kwargs` so only tailnet hosts route through
  the sidecar proxy and all other egress is untouched. Enforced by `verify_tailnet`.

## 3. Where the work happens

| Concern | Pin | One line |
|---|---|---|
| Release source | `app/release.py::release_identity` · `app/release.py::release_manifest` | Baked RELEASE and commit, lock inventory and installed profile availability; shared by health, API and CLI. |
| Release commands | `app/cli/cmd_release.py::register` | version and release manifest, local or remote; docs-scoped API and MCP read parity. |
| Liveness + readiness | `app/web/health.py::health` · `app/web/health.py::ready` · `app/web/health.py::router` | The instant no-DB liveness answer, the DB-coupled readiness answer, and the all-profiles router. |
| Structured logging | `app/logging_config.py::configure_logging` · `app/logging_config.py::JsonFormatter` | The opt-in JSON logging (root logger untouched under text output); HTTP client request lines quieted in every format. |
| Tailnet egress seam | `app/services/tailnet.py::is_tailnet_host` · `app/services/tailnet.py::tailnet_proxy_url` · `app/services/tailnet.py::tailnet_client_kwargs` | The destination-scoped SOCKS5 routing for LOCAL and self-hosted engine HTTP. |

## Proxy runtime configuration

All uvicorn launch paths disable its outer ProxyHeadersMiddleware with
`--no-proxy-headers` (`proxy_headers=False` for Python launches), including Docker,
run.py, the entrypoint, QA and embedded examples. The application Host boundary
alone forwards scheme and resolves client identity. `setting:trusted_proxy_ips`
is the only proxy setting. Local proxy trust changes apply live to identity and
scheme; environment changes require a restart. An empty list trusts no peer,
including loopback, on every platform. A platform edge proxy requires configured edge socket
peers for both forwarded scheme and its client header. The scheme parser and trust cache are built
at startup and rebuilt when effective proxy trust changes. A best-effort boot
stack check detects outer uvicorn proxy middleware and unconditionally logs ERROR
once at startup when detected, adding `ingress_config: uvicorn proxy headers on`
to `/health` detail without refusing boot;
ASGI cannot guarantee detection of every custom wrapper. Custom launches must
also disable uvicorn proxy headers. SAML without proxy trust logs WARNING
and adds `/health` detail, never failing boot for invalid proxy trust.
See the upgrade note in [DEPLOY](../DEPLOY.md#proxy-notes-that-matter-here).

The entrypoint ratchet does not cover `sh -c "uvicorn ..."` or gunicorn with UvicornWorker; neither launch form is used in this repository.

`setting:security_allowed_hosts` pins hostnames independently of port. There is
no loopback exemption. The exact `/health` and `/ready` probes may use any syntactically valid Host;
they bypass only the Host allowlist. Other paths including metrics scrapes
need a listed Host. Malformed syntax always returns 400. Both ingress settings are local-only. Recovery: edit
`LOC_TMS_SECURITY_ALLOWED_HOSTS` on the host (empty clears), or import a corrected
TOML settings file with `config apply` / use `config set security_allowed_hosts ""`,
then restart. Persisted values reside in the database Setting table.
Invalid ingress overrides log ERROR and become empty without stored fallback;
`/health` detail reports `ingress_config: invalid <key>` without touching the DB.
With `setting:app_base_url` configured and the effective pin empty, `/health` detail
includes `"host_pin":"unset"`; otherwise the key is absent. Boot logs one WARNING
in the same condition. The handler reads mirrored config attributes, without DB work.
The pin defaults to empty and is never auto-populated. Non-empty Host list writes
must include the configured `app_base_url` host (case-insensitive, port-independent):
add that host, or clear/repoint `app_base_url`. Empty remains settable for recovery.
Existing pins and environment overrides retain syntax-only read validation.
Non-empty all-separator Host lists are invalid. The identity-and-access owner
and `verify_ingress_boundary` describe and exercise the same boundary.

## 4. Data & settings

- **No settings key owns this layer.** The health probes are unconditional, structured
  logging is chosen by the server log format (config, environment `LOG_FORMAT` via
  `app/config.py`), and the tailnet seam reads the sidecar's environment (documented in the
  module) rather than a `settings.get` key.
- **One environment dial touches the probes:** `app/config.py::READY_PROBE_TIMEOUT_S`
  (`LOC_TMS_READY_PROBE_TIMEOUT_S`, default 2.0 seconds) is the wall-clock budget
  `app/web/health.py::ready` gives the database before it answers 503. Keep it comfortably
  under the proxy's own check timeout (5 seconds is typical). It is an environment
  value, not a `Setting` row, precisely because a probe must not need a database read to
  know how long it may wait. The connection-pool dials it interacts with live in
  configuration.
- **Topology is file-defined, not runtime:** the container shape lives in `Dockerfile` and
  `docker-compose.yml` (the AWS shape is in `deploy/aws-hosted`); the data directory, database URL, and framing
  policy come from `app/config.py` (documented in configuration and browser-surface).

## 5. Enforced by

- `verify_embedded_profile`: the health and readiness probes mounted in every profile,
  including the embedded surface-dropping one.
- `verify_stress_liveness_pool`: liveness answering with the worker threadpool saturated,
  readiness answering 200 with `pool: at_capacity` inside its budget with the connection pool fully checked out, and
  the probes staying inside budget through a launch burst.
- `verify_ops`: the opt-in structured-logging configuration and its default-neutral no-op.
- `verify_tailnet`: the destination-scoped tailnet routing and its inertness off-tailnet.

## 6. Battle scars

- **Readiness stayed DB-coupled on purpose.** A volume flood once stalled `/ready` because a
  tap write held a pooled connection under contention; the fix shortened the tap write rather
  than decoupling the probe, because a readiness check that does not touch the DB is not a
  readiness check, recorded in `app/web/health.py`.
- **A DB-free liveness probe still took production down.** Under twelve concurrent runs the
  connection pool exhausted, every waiting caller pinned an anyio worker thread for the full
  30-second checkout timeout, and `/health` - a handler that returns a literal dict - queued
  behind them for 20 to 145 seconds; the proxy's 5-second check went critical and the machine
  served 503s for 35 minutes before an OOM kill. The cure was structural, not a bigger budget:
  liveness left the threadpool entirely, readiness got its own thread and a deadline, and the
  pool was sized for the work the app actually admits (`app/web/health.py`).
- **The health surface was extracted so the embedded profile keeps exactly it.** The probes
  moved into their own router so a headless build can mount them and drop everything else,
  recorded in [`docs/DEPLOY.md`](../DEPLOY.md) and the module docstring.
- **Tailnet egress is opt-in and private by construction.** The sidecar route reaches a home
  model server without a public endpoint and only for tailnet destinations.

## 7. Limits & accepted risks

- **Single machine is the shipped constraint.** One process on one core is the default
  topology; horizontal scale requires Postgres plus the durable job queue and is bounded by
  the single-writer story.
- **Readiness uses a pool-less connection with a deadline.** `/ready` runs a real query
  through `app/db.py::emergency_session`. A healthy database returns 200 even when the
  request pool is full, with `pool: at_capacity`; the machine stays in proxy rotation
  while the app sheds excess traffic. Database failure or expiry of
  `app/config.py::READY_PROBE_TIMEOUT_S` returns 503, as implemented in `app/web/health.py`.
- **A probe that answers cannot tell you the app is working, so the deploy check watches
  the one that can say no.** `/health` now answers 200 by construction, which means a
  wedged machine would report healthy forever if a health check polled it. A proxy or
  orchestrator health check therefore polls `/ready` - which returns an honest 503 inside
  `app/config.py::READY_PROBE_TIMEOUT_S` when the database does not answer - and `/health`
  is documented as process liveness only, for a supervisor that should restart a dead
  process rather than a busy one (`Dockerfile`'s `HEALTHCHECK`). Run-level health is still
  a different question and belongs to the observability surface
  (incident-response-and-observability), not to a probe.
- **The tailnet sidecar is a deploy-time capability, not a product feature.** It exists only
  when the production image carries the Tailscale sidecar, so it is absent and inert on a
  default install, stated in `app/services/tailnet.py`.

## Private egress exceptions

`egress_private_allowlist` is host-only and read-only on remote surfaces.
Prefer an exact hostname when configuring a private connector. A CIDR approves
ANY hostname resolving into that range, including attacker-controlled public names.
See the [canonical private egress policy](secrets-and-crypto.md#private-egress-exceptions)
for matching, always-blocked destinations, health status and `egress.private_target` audit rows.
The same list admits a private OIDC identity provider (`sso_issuer`) by exact hostname; the full per-client table is
the [outbound client inventory](secrets-and-crypto.md#outbound-client-inventory).

Add an exact hostname to a host-managed TOML file, then run
`loc-tms config apply /path/to/settings.toml --yes`:

```text
[settings]
egress_private_allowlist = "jira.internal"
```

Alternatively set `LOC_TMS_EGRESS_PRIVATE_ALLOWLIST` and restart the service.
Run `loc-tms --json config show` and confirm `valid: true`, the expected value,
and source after setting the variable. A trailing comma or host:port entry invalidates
the ENTIRE list, leaving private egress blocked. Use hostnames without schemes or ports.
Test the connector and inspect its `egress.private_target` audit row with auditing enabled.
Health reports only status; use authenticated config reads to inspect the configured value.

## Release and installation truth

`app/db.py::init_db` routes fresh and stamped real SQLite installs through
Alembic; legacy unstamped databases use self-heal with a warning and cannot apply
general non-additive migrations. PostgreSQL requires Alembic initialization;
the container entrypoint migrates before serving. See [DEPLOY](../DEPLOY.md#upgrade)
for source selection, checkpoint backup, one-head/current-revision preconditions,
health version and database verification, and schema recovery after failure.
`verify_db_verify` and `verify_docs_architecture` parse the fenced CLI commands.
`verify_release_manifest` pins the four release surfaces and build identity.
Single-node targets are RPO 24 h and RTO 1 h; the timed drill is a separate AWS demo
exercise. Nightly scheduling uses `scripts/nightly-backup.sh`; its exit codes are 1 setup or
backup error, 2 FAILED verify, 3 UNVERIFIED verify and 4 retention error (see `docs/DEPLOY.md`).

## Readiness timing and restart limits (#109)

These numbers describe a typical proxy health check of 5 seconds, sampled every 15 seconds
after a 20 second startup grace. The hosted-instance release script waits up to 600 seconds for
`/ready` after a swap and restores the old container on failure; there is no restart watchdog.

The `/ready` probe has a 2 second application deadline, inside the proxy's
5 second timeout. A new database failure can therefore be observed within approximately 17 seconds
(or 20 seconds if the HTTP request itself times out), after startup grace.
These are detection bounds, not a promised restart time. Service checks affect
routing; this repository configures no external restart controller. Automatic cycle
time for a live wedged process is therefore unbounded. Process exit/OOM recovery is
a different mechanism.

D8 requires pool saturation to remain a 200 with `pool: at_capacity`: a shedding
machine stays reachable for cancellation. Pool saturation alone must not trigger
cycling. A failed database probe returns 503 and returns to 200 when the database
answers again. `verify_memory_bound` saturates and releases the pool, checks overflow
connections return to the configured retained size within 5 seconds, and checks the
failed-probe/recovered-probe pair. A restart watchdog remains an explicit deployment
gap; do not infer one from a single-instance setting or the service check.

## Scripts and release tooling

`scripts/` holds three kinds of file. Only two ship in the image: the allow-list `.dockerignore`
names `scripts/saml_smoke.py` (the build-time SAML smoke, run by the Dockerfile when the image is built
with SAML) and `scripts/convert_db_encryption.py` (imported by `db encrypt` and `db decrypt`);
`verify_docker_image` fails when a runtime reference is not allow-listed.

| Concern | Pin | One line |
|---|---|---|
| Image build checks | `scripts/saml_smoke.py` · `scripts/pin_base_images.py` | The SAML build smoke, and the tool that resolves and rewrites the digest pins of the base images. |
| Encryption conversion | `scripts/convert_db_encryption.py` | The one-time converter between a plaintext SQLite database and a SQLCipher one, behind the `db encrypt` verbs. |
| Dependency locks and audit | `scripts/lock_inventory.py` · `scripts/hash_locks.py` · `scripts/audit_runtime_profiles.py` · `scripts/release_lock.py` · `scripts/wheel_native_inventory.py` · `scripts/native_wheel_audit.py` | One list of every install lock, hash coverage for exact locks, the offline advisory audit of the pinned profiles, the release inventory and image-freeze comparison, and the inventory of native libraries bundled inside wheels (the pre-gate scans the installed environment; the dependency-audit workflow downloads each locked wheel with hashes required and fails closed, #620). |
| Scan and SBOM | `scripts/scan_image.sh` · `scripts/sbom.py` | The image scan that gates every swap (exit 0 clean, 1 fixable findings, 2 usage error) and the CycloneDX SBOM of the locked Python packages. |
| Backups | `scripts/nightly-backup.sh` | The quiesced single-node nightly checkpoint with verify and retention (exit codes in [DEPLOY](../DEPLOY.md)). |
| Demo and test instances | `scripts/test_instance.py` · `scripts/qa_server.py` · `scripts/showcase_seed.py` · `scripts/seed_demo_content.py` | Throwaway real-engine instances under `.test-instances/`, the QA preview launcher, and the demo seeders. Tools only: the app never invokes them. |
| Test tooling | `scripts/pregate.py` · `scripts/suite_owners.py` | The offline pre-gate and the selector that names which verify suites own a changed file. |

The one-off probes and
campaign scripts in `scripts/` are developer instruments that no product code imports: the UX
instruments `scripts/ux_screenshots.py` and `scripts/ux_test_inventory.py`, the pure-move split
planner `scripts/srp_split.py` with `scripts/srp_move_check.py`, the inventory probe
`scripts/connector_config_writers.py`, the reviewer comparison `scripts/review_diff.py`, the demo
image maker `scripts/make_demo_image.py` with `scripts/seed_demo_content_common.py`, the
MCP body-size reproduction `scripts/mcp_body_size.py` and the throwaway contrast check `scripts/console_contrast_check.py`.
The rest is support data and Windows wrappers: `scripts/native_advisories.json` is the advisory list
that `scripts/native_wheel_audit.py` and the pre-gate read, `scripts/DEPENDENCY_AUDIT.md` holds the
dependency-audit operator notes, `scripts/pregate.cmd`, `scripts/sbom.cmd` and
`scripts/test-instance.cmd` run the matching Python tools with the repo's virtual environment.
