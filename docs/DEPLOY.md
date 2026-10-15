# Localization OS TMS - Deployment Guide

Operator guide for running Localization OS in a container, in production. Covers the
default SQLite deploy, the optional Postgres backend, TLS via a reverse proxy,
the secrets you must manage, and the single data volume you must back up.

See [local install security](LOCAL-INSTALL-SECURITY.md) for the required Compose
proxy companion file, database environment value, local Host rules and upgrade behavior.

The container files it describes are `Dockerfile`, `docker-entrypoint.sh`,
`docker-compose.yml`, `.dockerignore`, and the reverse-proxy samples under `deploy/`.

**Where this guide applies.** This guide covers the generic Docker Compose deployment on a single
cloud VM or any Linux host. The hosted service from Patois Labs runs the same image on a single cloud
VM behind a reverse proxy; the reference kit for that shape is `deploy/aws-hosted`. Every host applies
the same image-scan rule described below before an image serves.

---

## 0. TL;DR

```bash
# SQLite: first set POSTGRES_PASSWORD in your private .env (required by Compose).
export RELEASE="$(git describe --tags --always --dirty)"
export RELEASE_COMMIT="$(git rev-parse HEAD)"
# Complete the Compose image scan gate below before starting.
# After the gate and start, the app is on http://127.0.0.1:8000 (front it with a proxy for
# public traffic). Data persists in the `loc_data` named volume.
```

Run the [Compose image scan gate](#compose-image-scan-gate), then its first-launch
start command. No image may serve before this gate passes.

`docker-compose.yml` passes `RELEASE`/`RELEASE_COMMIT` through to the image as
build args (`args: RELEASE: ${RELEASE:-}` / `RELEASE_COMMIT: ${RELEASE_COMMIT:-}`).
Skip the two `export` lines and the build still succeeds, but with both unset the
image falls back to the fixed `version = "0.8.0"` in `pyproject.toml`, so
`/health` and `python -m app.cli version` report the wrong release.

On first boot the login wall is ON and **no accounts exist yet** - your first
visit to `http://127.0.0.1:8000` redirects to the `/auth/setup` first-run wizard,
where you create the admin account (12-char minimum password). There are **no
seeded default passwords** to change: the container ships closed. The wizard
also asks for a **one-time setup token** (#414), so only someone who can read
the server can claim it: the app prints it at startup (`docker compose logs app`,
the line `enter this one-time setup token: ...`) and keeps it in
`/data/.setup_token` (`docker compose exec app cat /data/.setup_token`). Set
`LOC_TMS_SETUP_TOKEN` (16+ characters) in the environment to choose it yourself.
The log shows the token in full only once, at the first start; a later restart names the file instead.
If the generated file is deleted before setup finishes, restart the app to generate
a new token. Form submissions never generate a replacement token.
The file is deleted once the admin exists, and the wizard creates exactly one
account even if several people submit it at once. Still put it behind the proxy
(or bind localhost) before first boot.

---

For DB queue recovery, topology and PID-reuse limits, see
[jobs and scheduler](architecture/jobs-and-scheduler.md#3-where-the-work-happens).

Upgrade path: bare `loc-job-N`, NULL and unparsable workers are treated as own-host dead
and reclaimed at boot, including fresh leases left by pre-fence releases.

DB queue requirement: SQLite **3.35 or newer** (or Postgres) for guarded recovery's
`UPDATE ... RETURNING`; it ties Job resets to queue rows actually reclaimed.

## 1. Quick start with Docker (SQLite, default)

SQLite is the zero-config default. No database server, no extra services.

```bash
# from the repo root (where Dockerfile + docker-compose.yml live)
export RELEASE="$(git describe --tags --always --dirty)"
export RELEASE_COMMIT="$(git rev-parse HEAD)"
# Complete the Compose image scan gate below before starting.
```

Run the [Compose image scan gate](#compose-image-scan-gate), then its first-launch
start command, using the same shell and deployment environment.

Set `RELEASE`/`RELEASE_COMMIT` first, as above - without them the image builds fine
but reports the stale `pyproject.toml` version instead of your actual release (see
the TL;DR note above).

What happens:
- The image is built (multi-stage: a builder compiles deps, the runtime image
  ships only the venv + app, runs as a non-root `app` user). The app code under `/app`
  is owned by root and read-only to that user; only `/data` is writable, and the build
  fails if that split does not hold.
- Only what `.dockerignore` allow-lists reaches the build: the `app/` package,
  migrations, assets, entrypoint, dependency locks and license files. Tests, docs,
  planning, local data, `.env` files and any other local folder never enter the image.
- The `app` service starts uvicorn **directly, without --reload** (the dev
  loader in `run.py` is intentionally not used in the container).
- `LOC_TMS_DATA_DIR=/data` points the DB, uploads, and keyfiles at the mounted
  `loc_data` volume, so they survive restarts and image upgrades.
- `LOC_TMS_SERVER=1` (set by the image and re-set in compose) declares this a real
  server install. It is what tells startup to turn the login wall on and run the
  scheduler **without** seeding any dev accounts - distinct from the verify test
  suites, which set `LOC_TMS_DATA_DIR` alone and deliberately boot unauthenticated.
- On startup the app runs `init_db()` (Alembic for fresh or stamped real SQLite installs),
  turns the **login wall on**, and starts the connector scheduler. It seeds **no
  accounts**: the first visit lands on `/auth/setup` to create the admin, which asks
  for the one-time setup token printed in the startup log (also `/data/.setup_token`,
  or your `LOC_TMS_SETUP_TOKEN`). There are no default `admin/admin` credentials.

The port is published on `127.0.0.1:8000` only. That is deliberate: put the
reverse proxy (section 3) in front for anything reachable from the network or
internet. To reach it directly on a trusted host, browse to
`http://127.0.0.1:8000`.

Useful commands:

```bash
docker compose logs -f app        # tail app logs
docker compose ps                 # service + health status
docker compose down               # stop (keeps the volume/data)
docker compose down -v            # stop AND DELETE the data volume (destructive)
# For upgrades, follow the checkpoint procedure below.
```

Health endpoints (the image's HEALTHCHECK uses `/ready`):

- `GET /admin/health`: operational details, requires a signed-in administrator.
- `GET /health` - liveness, no DB work.
- `GET /ready` - readiness, checks the DB connection.

### Base image pins

The two base images (`python:3.13-slim` for both Python stages and
`tailscale/tailscale:v1.102.5`) come from the `PYTHON_BASE` and `TAILSCALE_BASE`
arguments at the top of the `Dockerfile`. A pinned value reads
`python:3.13-slim@sha256:<digest>`: Docker pulls that exact image, and the tag stays in
the value so you can see what it is. `scripts/pin_base_images.py` resolves each tag to
its current digest via registry HTTPS and writes it in (no Docker needed), and `--check`
exits non-zero while either value has no digest. To pin for the first time, or to take
a new Debian or Python patch release deliberately, run from the repo folder:

```text
python scripts\pin_base_images.py
python scripts\pin_base_images.py --check
```

Review the `Dockerfile` diff (only the two `ARG` lines change), rebuild, and commit it
like any dependency bump. To move to a new tag (say a new tailscale release), edit the
tag in the `ARG` line first, then run the script.

---

### Hashed lock refresh

Run `scripts/hash_locks.py` after changing pins. It reads the official PyPI JSON API,
records SHA256 for every published distribution (including Windows and Linux wheels),
and preserves markers and provenance notes. It never installs or executes packages.
`scripts/hash_locks.py --check` fails if any pin lacks hashes. Review the lock diffs.
Every container lock install requires hashes, and so does the CI dev install
(`requirements-dev.lock`, the hashed closure of `requirements-dev.txt`, GitHub #412); the
tool's `requirements*.lock` glob covers it automatically, as well as the CI release-check tools lock
(`scripts/locks/requirements-ci-check-tools.lock`). Regenerate the dev lock with `uv pip compile
requirements-dev.txt --universal --python-version 3.13 --no-header -c requirements.lock -c
requirements-saml.lock -c requirements-postgres.lock -c requirements-ldap.lock -o
requirements-dev.lock`, restore its provenance header, then run `scripts/hash_locks.py`. No CI step
runs a pip install without `--require-hashes`, and none installs the unpinned
`requirements-dev.txt` or a bare `pkg==x` spec (`verify_release_lock` enforces it). The explicit SAML lxml fallback uses
`scripts/locks/requirements-saml-lxml.lock`, retaining its advisory warning and wheel-only
policy. It is separate from the additive runtime inventories because it replaces lxml, so
`scripts/audit_runtime_profiles.py` audits it as its own inventory (`saml-lxml-fallback`, in the
dependency audit workflow matrix and the local pregate audit). Its recorded advisory
(PYSEC-2026-87) is a visible, non-blocking WARNING until the exception date in
`FALLBACK_ACCEPTED` (2027-04-06); after that date, or for any other advisory, it fails the audit.

### SQLCipher image option

For a fresh SQLite installation, use the following command with the usual release
arguments shown below. The default is `0`.

```text
docker build --build-arg WITH_SQLCIPHER=1 -t localizationos:sqlcipher .
```

Like every image, it serves only after the [Compose image scan gate](#compose-image-scan-gate)
(or the equivalent gate on your cloud host) has scanned its exact ID.

This installs
`requirements-sqlcipher.lock`, the reviewed build backend lock, and libsqlcipher.
Build isolation is disabled so pip cannot fetch unpinned build dependencies.
Before the first start, set `LOC_TMS_DB_ENCRYPTION=sqlcipher` and provision the encryption
key as described in `docs/SECRETS.md`. Keep the key with the protected backup material.
The mode is chosen at install time for a fresh database. Changing this variable later
does not encrypt an existing instance or convert its database. Postgres uses its own
storage encryption controls. Upload encryption remains a separate option.

### Image pin change log

2026-09-28: multi-platform index digests supplied by the orchestrator from Docker Hub.
Python is shared by builder/runtime and Vendor Studio; hosted/compose Caddy and Postgres
use these same reviewed references. Monthly updates use `scripts/pin_base_images.py`
for the main base ARGs; its `resolve_digest` helper also accepts `caddy:2` and `postgres:16`.
Review and copy any updated digests to the compose, CI, vendor and runbook references.

2026-10-06: Python re-pinned (Debian openssl and pcre2 security fixes) and copied to the Vendor
Studio Dockerfile; tailscale v1.98.9 to v1.102.5 (Go 1.26.6 and x/crypto fixes). The runtime
stage deletes pip (build arg `KEEP_PIP=1` keeps it, for the CI release-image job only), because
the newest pip still vendors msgpack, urllib3 and setuptools versions the image scan flags.

```text
python:3.13-slim
sha256:bf44cdfcb76cd3b41e879bc058fc37ec5872002ccfde7fcb765e218cde0cd79c
tailscale/tailscale:v1.102.5
sha256:c507f3a2a6ab1cabd8d809b98edeb41edbd5c3fb6ad9632ffd098b4c7d0b4065
caddy:2
sha256:0c994536bddb66445885237f1a5dcc1916bccea922661c76b4e9fc24061f9b52
postgres:16
sha256:1a6ab3f5345eb6dbe04a1349529caabdb0ab09293a09590fad07b2246bfa4b54
```

### Release dependency inventory and refresh

The adopted web stack is FastAPI **0.142.2** and Starlette **1.7.0**. Keep the
exact Starlette pin: FastAPI's metadata allows >=0.46.0 without an upper bound,
and every repository template response now uses the 1.x
`TemplateResponse(request, name, context)` signature. The bump closes the five
remaining Starlette advisory exceptions (PYSEC-2026-161, -2280, -2281, -248 and
-249); 0.52.1 had already cleared PYSEC-2026-1941 and -1942.

The lock retains sse-starlette 3.0.3, whose runtime requirement is anyio>=4.7.0;
anyio 4.15.1 satisfies it. Even its optional examples' Starlette >=0.49.1
constraint is satisfied. FastAPI adds the leaf annotated-doc 0.0.5. This offline
train updates only the Starlette lock entry, checking installed Requires-Dist closure
rather than freezing the shared venv's optional/development packages. A fresh
profile vulnerability scan remains a network-enabled CI gate.

The 2026-09-13 dependency batch pins OpenAI SDK 2.54.0, SQLModel 0.0.42,
lxml 6.1.3, and the optional psycopg/binary driver (now 3.3.6). OpenAI stays on
httpx 0.28.1; moving to SDK 3.x requires a coordinated httpx2 transport migration
(tracked as a follow-up). Installed metadata adds no base transitive dependencies.
The development formatter is exactly ruff 0.16.10. Every ruff bump requires
`ruff format .`, repo-wide `ruff check .`, and the `verify_ruff` runner suite.

The 2026-09-28 lock refresh (Dependabot #308, minus SQLModel) moves uvicorn to
0.53.0, Starlette to 1.7.0, alembic to 1.20.0, google-genai to 2.25.0, idna to 3.20,
PyJWT to 2.15.0 and psycopg/binary to 3.3.6; Vendor Studio carries the same uvicorn,
Starlette and idna pins. The base freeze ran under the previous lock as constraints,
so no other transitive version moved. SQLModel stays on 0.0.42: 0.0.45 and later
reject the naive UTC datetimes the models store, which needs its own migration.

The 2026-10-01 follow-up (Dependabot #325, #323, #336) moves uvicorn to 0.54.0 and
SQLModel to 0.0.44 in the base and Vendor Studio pins and locks. Neither adds a transitive
dependency; SQLModel 0.0.44 is still below the 0.0.45 naive-datetime break.

The 2026-10-08 follow-up (Dependabot #629) moves fastapi to 0.142.2, mcp and mcp-types to
2.3.0, google-genai to 2.28.0, PyJWT to 2.15.1, pypdfium2 to 5.14.0, python-dotenv to 1.2.4 and
regex to 2026.9.29 in the base pins and `requirements.lock`, and ruff 0.16.10 plus cyclonedx-bom 7.5.0
in the dev pins and `requirements-dev.lock`. The closure was re-resolved with `uv pip compile --universal
--python-version 3.13` using the previous locks as constraints (only the bumped packages unconstrained);
the only transitive movement is mcp-types. `scripts/hash_locks.py` then re-hashed every lock; the other
locks are byte-identical except the Vendor Studio lock, which must match the root pins (gate L4): it takes
fastapi 0.142.2 and python-dotenv 1.2.4 here and, because fastapi 0.142.2 now requires opentelemetry-api, gains
opentelemetry-api 1.45.0 (the root lock's version; Apache-2.0). Dependabot #555 is the matching Vendor Studio
`requirements.txt` pin. `httpx2-jsfetch` (emscripten-only, pulled by httpx2) stays out of the lock as before.

The Docker builder installs `requirements.lock` plus `requirements-postgres.lock`
with `--no-deps`. It does not resolve new transitive versions during a rebuild.
The base lock remains copyleft-free; the container intentionally also bundles the
LGPL Postgres driver. LDAP and SAML are opt-in host profiles with their own additive
locks (`requirements-ldap.lock` and `requirements-saml.lock`). Install the base lock
and the selected profile lock together. The default SQLite host install needs only
`requirements.lock`.

The image carries the same two profiles only when you ask for them at build time.
`--build-arg WITH_SAML=1` installs `requirements-saml.lock` with `--no-deps` and binary
wheels only for lxml and xmlsec, adds the `libxmlsec1-openssl` system library, and runs
`scripts/saml_smoke.py` as the app user. That smoke fails the build unless python3-saml and
xmlsec import, lxml and xmlsec share the libxml2 major.minor version (the same rule
python-xmlsec applies), and a small SAML document signed with a throwaway in-memory key
verifies while a tampered copy does not. If the SAML smoke fails with the locked lxml, add
`--build-arg SAML_LXML=6.0.0` to use the lxml 6.0.0 wheel, the pairing proven with xmlsec
1.3.17. That image then differs from `requirements.lock` by that one pin, so record it; the
default value `locked` changes nothing. lxml 6.0.0 is inside advisory PYSEC-2026-87 (an XXE,
fixed in lxml 6.1.0), so the fallback prints `WARNING:` lines in the build log; audit such an
image with `pip-audit` and record the result in the change log (the hosted launch
launch checklist has the commands). The warning does not block the build or the launch.
`--build-arg WITH_LDAP=1` installs `requirements-ldap.lock`
(ldap3, LGPL) and checks `import ldap3` the same way. Both default to `0`, so a build
without them (`docker compose up` with no overrides) installs exactly the base and
Postgres locks. With compose, set `WITH_SAML=1` or `WITH_LDAP=1` in the environment or
`.env`; `docker-compose.yml` passes them through. Build a SAML image before you turn on
`saml_enabled`: on an image without it the boot fails with a message that names
`--build-arg WITH_SAML=1`.

```text
docker build --build-arg WITH_SAML=1 --build-arg RELEASE="$(git describe --tags --always)" --build-arg RELEASE_COMMIT="$(git rev-parse HEAD)" -t localizationos:saml .
```

Scan that exact image ID before it serves, as for any other image
([Compose image scan gate](#compose-image-scan-gate), or the equivalent gate on your cloud host).

Licenses the SAML image adds: python3-saml (MIT), xmlsec, the Python binding (MIT),
isodate (BSD-3-Clause), and the Debian `libxmlsec1-openssl` library (MIT). None of these
Python packages is copyleft, so the Python package set of a SAML image stays as clean as
the default one. The Debian library brings its own dependencies (libxmlsec1, and libltdl7,
which is LGPL-2.1+ like the other GNU libraries every Debian base image already carries).
The LDAP image adds ldap3 (LGPL-3.0), in the same class as the Postgres driver the image
already bundles.

The release-image CI job runs only on pushes to main, tags, and manual dispatch,
never on PRs. It builds the image with `KEEP_PIP=1` (the shipped image has no pip) and checks
`pip check` and `import app.main` inside it, then builds the shipped default and asserts pip is
absent from it (a missing interpreter fails the check too). It also runs the release-lock suite
inside the image. The suite follows
`Requires-Dist`, including requested extras, rejects orphan pins, and compares
installed versions to locked versions. LDAP and SAML get separate validation image
layers with their profile locks installed. Freeze artifacts are retained as evidence.
The test tools are mounted separately, without changing the image's runtime packages.

Platform markers exclude `pywin32` and Postgres's `tzdata` on Linux and exclude
`uvloop` on Windows. Keep `uvicorn[standard]` in the direct requirements: it requests
httptools, watchfiles, and the platform-dependent event loop along with its other
extras. This is a package/version guarantee, not a byte-for-byte image guarantee:
these locks have no hashes, and OS packages and base-image tags are separate inputs.

Refresh in clean Python 3.13 virtualenvs, never by freezing the shared development
venv. The following PowerShell commands preserve extras and re-add the exact marked
pins. Run from the repository root after updating direct requirements. Review these
three cross-platform versions deliberately when upgrading them; a Windows freeze
cannot supply the Linux-only uvloop pin. Write LF explicitly on Windows; sort
pywin32 into the base pins with its shipped spacing and keep uvloop last.

```text
python -m venv .lock-refresh
& .\.lock-refresh\Scripts\python.exe -m pip install -r requirements.txt
& .\.lock-refresh\Scripts\python.exe -m pip check
$basePins = @(& .\.lock-refresh\Scripts\python.exe -m pip freeze | Where-Object { $_ -notmatch '^(pywin32|uvloop)==' })
$basePins += 'pywin32==312 ; sys_platform == "win32"'
$sortedPins = [Collections.Generic.SortedDictionary[string,string]]::new([StringComparer]::OrdinalIgnoreCase)
foreach ($pin in $basePins) { $sortedPins.Add(($pin -split '==')[0], $pin) }
$lockLines = @($sortedPins.Values) + @('uvloop==0.22.1; sys_platform != "win32"')
[IO.File]::WriteAllText((Join-Path $PWD 'requirements.lock'), ($lockLines -join "`n") + "`n", [Text.Encoding]::ASCII)
```

Generate each additive profile in its own fresh venv under the base lock constraints.
Only packages absent from the base lock enter an additive lock. Restore the Windows
marker on tzdata explicitly. Keep the recorded optional `Requires-Dist` snapshot in
sync, so a default install can validate those locks without installing opt-ins.
Each profile lock starts with its leading provenance comments, with any recorded
date refreshed from the current date, followed by pins sorted by `name.lower()`
(the natural `pip freeze` order). Comments elsewhere are not carried forward.

```text
$baseNames = Get-Content requirements.lock | Where-Object { $_ -match '==' } | ForEach-Object { (($_ -split '==')[0] -replace '[-_.]+','-').ToLower() }
foreach ($profile in @('postgres', 'ldap', 'saml')) {
    python -m venv ".lock-$profile"
    $profilePython = ".\.lock-$profile\Scripts\python.exe"
    & $profilePython -m pip install --no-deps -r requirements.lock
    & $profilePython -m pip install -c requirements.lock -r "requirements-$profile.txt"
    & $profilePython -m pip check
    $refreshDate = Get-Date -Format yyyy-MM-dd
    $profileHeader = @(foreach ($line in Get-Content "requirements-$profile.lock") {
        if ($line -notmatch '^#') { break }
        $line -replace '\b\d{4}-\d{2}-\d{2}\b', $refreshDate
    })
    $profilePins = @(& $profilePython -m pip freeze | Where-Object { (($_ -split '==')[0] -replace '[-_.]+','-').ToLower() -notin $baseNames -and $_ -notmatch '^tzdata==' })
    if ($profile -eq 'postgres') { $profilePins += 'tzdata==2026.2 ; sys_platform == "win32"' }
    $sortedProfilePins = [Collections.Generic.SortedDictionary[string,string]]::new([StringComparer]::Ordinal)
    foreach ($pin in $profilePins) { $sortedProfilePins.Add((($pin -split '==')[0]).ToLowerInvariant(), $pin) }
    $profileLines = $profileHeader + @($sortedProfilePins.Values)
    [IO.File]::WriteAllText((Join-Path $PWD "requirements-$profile.lock"), ($profileLines -join "`n") + "`n", [Text.Encoding]::ASCII)
}
```

Review Windows and Linux inventories, reject local `file:` URLs, refresh
`.github/locked-profile-metadata.json` from the installed profile distributions'
version and `Requires-Dist` metadata, then run the checks using a lock-installed
environment plus pytest and packaging from the hash-pinned requirements-dev.lock:

```text
python -m pytest tests/test_verify_suites.py -k "verify_release_lock or verify_licenses" -p no:cacheprovider
```

The host suite prints a notice for each absent package, checks optional profile
locks statically using recorded metadata, and checks installed versions wherever
available. Full profile validation is mandatory inside the corresponding CI image.
Only the named optional Presidio consumer constrains base pins from outside the lock.
Pre/dev pins require an explicit `# prerelease-accepted: <reason>` on their line.

The dependency-audit workflow scans the base and every locked extras profile in
separate trees. Additional opt-ins are inventoried in `.github/runtime-profiles.json`:
Presidio, local OCR, and
SQLCipher. These opt-ins resolve under base-lock constraints at audit time; their
retained freeze files and JSON audit results describe that run, not a locked release.
Resolution or audit failures fail the workflow. Embeddings use remote/local HTTP
providers and require no additional Python embedding package; neural evaluation
metrics are not yet implemented. Optional model downloads are not Python package
inventories and are outside this scan.

Each accepted advisory in `.github/pip-audit-ignores.txt` must have `owner`, `expires`,
and `reason` fields after `#`. Expired entries fail CI, including the weekly audit.
After an upgrade, scan all profiles before removing cleared entries; the owner must
review reachability and the expiry of entries that remain. Pillow image inputs are
reachable in the default profile, so the ledger must not describe them as opt-in.

### Local dependency audit and SBOM

The gate's "Native library scan" step (right after "Dependency audit") covers what pip-audit
cannot see: libraries vendored inside wheels (libxml2, libxslt, ...). It scans the gate
interpreter's site-packages offline against `scripts/native_advisories.json` and blocks the
merge on a finding or a scanner error; the dep-audit workflow runs the same advisories over
each profile's downloaded hash-locked wheels (`scripts/native_wheel_audit.py`). Details and
scope limits: `scripts/DEPENDENCY_AUDIT.md`.

The GitHub dependency-audit workflow only runs when Actions minutes are available,
so the local pre-merge gate (`scripts\pregate.cmd`) runs the same audit on every
merge in its "Dependency audit" step. It uses the pinned `pip-audit` from
`requirements-dev.txt`, the same advisory ledger (owner and expiry enforced, expiry
in UTC as in CI) and the same profiles. Every pin of `requirements.lock` and each
profile lock is audited, including the Windows-only and Linux-only pins, and each
opt-in from `.github/runtime-profiles.json` is resolved under the base lock
constraints and audited, without installing anything. An unignored advisory or an
expired ignore fails the gate. If the advisory database cannot be reached, the step
reports WARN: the audit did not run, and that is never a pass. A report that lists
fewer packages than were audited is a FAIL, never a quiet pass.

The locked pins are identical to CI. The opt-in profiles are close to CI, not
identical: the local step resolves them on the Windows host and its Python, while CI
resolves them on ubuntu with Python 3.13. A transitive dependency that only applies on
Linux (or only on Windows) can therefore differ between the two runs. Forcing a Linux
resolve locally needs `--only-binary=:all:`, which changes the answer in a different
way (checked 2026-09-25: all seven profiles resolved identically on both platforms
except `ocr`, where the wheel-only resolve picked `omegaconf 2.0.0` because
`antlr4-python3-runtime` ships no wheel, while CI installs `omegaconf 2.3.1` with
`antlr4-python3-runtime 4.9.3`). The host resolve is kept because it matches CI for
these profiles today; the CI workflow remains the Linux-exact check when it runs.

To run the same audit by hand (about a minute):

```text
python scripts\audit_runtime_profiles.py --local
```

A customer or procurement reviewer may ask for a software bill of materials (SBOM).
Generate a CycloneDX 1.6 JSON SBOM of the exact release locks with the pinned
`cyclonedx-bom` from `requirements-dev.txt`. It works offline, and the same locks
and release always give a byte-identical file:

```text
scripts\sbom.cmd --release v0.9.6
scripts\sbom.cmd --release v0.9.6 --profile ldap --profile saml
scripts\sbom.cmd --release v0.9.6 --profile none
```

The default describes the shipped container image (`requirements.lock` plus
`requirements-postgres.lock`). Add `--profile ldap` or `--profile saml` for a host
install with those opt-ins. `--profile none` covers the SQLite host install (base
lock only). Without `--release`, the version comes from `git describe`. The file
is written to `dist/sbom/localization-os-<release>-<profiles>.cdx.json`. `dist/` is
gitignored and holds no customer data. Send that file together with the release tag
it names. The SBOM lists Python packages only: the OS packages of the container and
the tailscale/tailscaled Go binaries are separate inputs, as noted above, and the
whole-image SBOM comes from the image scan (`scripts/scan_image.sh`, #226; it writes
a CycloneDX image SBOM next to its reports).

---

## 2. Postgres mode (optional)

SQLite is fine for a single instance. Choose Postgres for larger installs,
external backups, or when you want the DB off the app host. On Postgres,
**Alembic owns the schema** and the entrypoint applies migrations on boot.

### 2a. With the bundled `db` service (compose `postgres` profile)

Create a `.env` next to `docker-compose.yml`:

```dotenv
# Postgres credentials for the bundled db service
POSTGRES_USER=locstudio
POSTGRES_PASSWORD=change-me-to-something-strong
POSTGRES_DB=locstudio

# Point the app at that db service (service name `db`, internal network).
# NOTE the driver scheme: postgresql+psycopg:// (psycopg3).
DATABASE_URL=postgresql+psycopg://locstudio:change-me-to-something-strong@db:5432/locstudio
```

Then start with the profile (export `RELEASE`/`RELEASE_COMMIT` first, per section 0,
so the image reports the right version rather than falling back to `pyproject.toml`):

```bash
export COMPOSE_PROFILES=postgres
```

Then complete the [Compose image scan gate](#compose-image-scan-gate) and its
first-launch start command, which also scans and pins the enabled database image.

The `db` service comes up first (compose waits for its healthcheck), then the
`app` entrypoint sees a `postgres...://` `DATABASE_URL` and runs
`alembic upgrade head` before starting uvicorn. Postgres data persists in the
`loc_pgdata` volume; the `loc_data` volume still holds uploads + keyfiles.

### 2b. With an external / managed Postgres

If your Postgres lives elsewhere (RDS, Cloud SQL, an existing server), skip the
`db` service entirely - just set `DATABASE_URL` and run the default `app`:

```dotenv
DATABASE_URL=postgresql+psycopg://user:pass@your-db-host:5432/locstudio
```

```bash
# No postgres profile needed; export RELEASE/RELEASE_COMMIT first (section 0).
```

Complete the [Compose image scan gate](#compose-image-scan-gate) and its first-launch
start command with the default app service.

The entrypoint still runs `alembic upgrade head` on boot because the URL is a
Postgres URL. To run migrations manually instead (e.g. a controlled release
step), you can exec into the container or run alembic from any environment that
has the repo + venv:

```bash
docker compose run --rm app alembic upgrade head
```

`alembic` is in the default `requirements.txt`, but the Postgres **driver**
`psycopg[binary]` (the `postgresql+psycopg://` scheme) is an **opt-in extra** - it
is LGPL, so it is kept out of the default (copyleft-free) install. Install it
alongside the base deps before pointing `DATABASE_URL` at Postgres:

```bash
pip install --no-deps -r requirements.lock -r requirements-postgres.lock
```

The published Docker image already bundles the driver, so the compose flow above
needs no extra step. If `DATABASE_URL` points at Postgres without the driver
present, the app fails fast with a clear `pip install -r requirements-postgres.txt`
hint (never an opaque `ModuleNotFoundError`).

---

## 3. TLS / reverse-proxy setup

Do not expose the app directly on the public internet. Terminate TLS at a
reverse proxy and forward to the app over the internal network. Two ready
samples live in `deploy/`:

- **`deploy/Caddyfile`** (recommended) - automatic HTTPS via Let's Encrypt.
- **`deploy/nginx.conf`** - for shops already running nginx (bring your own certs).

### Caddy (auto-HTTPS), via the compose `proxy` profile

1. Edit `deploy/Caddyfile`: replace `tms.example.com` with your real domain and
   `you@example.com` with an ACME contact email. The domain's DNS must resolve
   to this host and ports 80/443 must be reachable for the ACME challenge.
2. Start with the proxy profile:

   ```bash
   export COMPOSE_FILE=docker-compose.yml:deploy/compose-proxy.yml
   export COMPOSE_PROFILES=proxy
   # combine with Postgres if you use it:
   # export COMPOSE_PROFILES=proxy,postgres
   ```

   Export `RELEASE`/`RELEASE_COMMIT` first (section 0) for either form.
   Complete the [Compose image scan gate](#compose-image-scan-gate) and its first-launch
   start command. This scans and pins both the application and the Caddy proxy.

Caddy publishes 80/443, obtains + renews certs automatically, and reverse-proxies
to `app:8000` on the internal network.

### nginx

Use `deploy/nginx.conf` as a vhost. Point `ssl_certificate` / `ssl_certificate_key`
at certs you manage (certbot, your CA). It proxies to `app:8000` (or
`127.0.0.1:8000` if nginx runs on the host against the published port).

### Proxy notes that matter here

Supported-profile checklist for profiles B/C:

1. Set `app_base_url` to the external URL, then set `security_allowed_hosts` on the
   host to include that URL's hostname and every intended LAN, tailnet or localhost
   alias. The pin defaults to empty; it is never derived or populated automatically.
2. Preserve Host and configure proxy trust as described below. Verify `/admin/health` has
   no `host_pin` warning. Keep the **Host pin recovery** instructions below available
   on the host shell in case an alias was omitted.

- **Only the proxy should be internet-exposed.** The `app` service is published
  on `127.0.0.1:8000` for local admin; the Postgres `db` service is on an
  `internal` (no-egress) network and must never be published.
- **Preserve the `Host` header end-to-end.** The app's CSRF guard is
  same-origin-based: it compares the request's `Origin`/`Referer` against `Host`.
  A proxy that rewrites `Host` will trigger false 403s. Both samples preserve it.
- **SSE endpoints** (live translate, job progress) need buffering off and a long
  read timeout. The nginx sample sets `proxy_buffering off` +
  `proxy_read_timeout 3600s`; Caddy streams these fine by default.
- **Upload size:** both samples cap request bodies at ~100 MB to match the app's
  `MAX_UPLOAD_BYTES` default. Raise both together if you increase the app limit.
  The app also caps every request body itself, before reading it (so a deployment
  without a proxy body cap is covered too): `REQUEST_MAX_ANON_BODY_BYTES` (1 MiB) for
  sign-in, webhook and SCIM paths and any request that is not signed in, and
  `REQUEST_MAX_BODY_BYTES` (the upload cap sent base64 in JSON, plus 1 MiB) for a
  signed-in caller. Over either is a 413. If you raise `MAX_UPLOAD_BYTES` through the
  environment, `REQUEST_MAX_BODY_BYTES` follows it unless you set it explicitly.
  Uploaded DOCX/XLSX files are also refused when they would expand past
  `ZIP_MAX_UNCOMPRESSED_BYTES` (256 MiB) or by more than `ZIP_MAX_RATIO` (200 times)
  when unpacked, and a PDF over `PDF_MAX_PAGES` (1000) pages is refused; a blank PDF
  page is rendered for OCR with its longer edge at most `PDF_OCR_MAX_PIXEL_DIM`
  (4000 px). `PDF_OCR_MAX_TOTAL_PIXELS` (2500000000) caps the sum of rendered page
  pixels per document, about 1000 A4 scans at 150 dpi. All low-text pages are checked
  before the first render or OCR call; text-only pages do not consume this budget.
  An image for image localization is refused from its header, before any decode, when a
  side exceeds 8000 px or it holds more than `IMAGE_MAX_TOTAL_PIXELS` (25000000, 25
  megapixels) pixels in total.
  MediaWiki uploads translate a template parameter only when `WIKI_TEMPLATE_PARAMS` lists
  it (comma-separated `Template:param` pairs, for example `Quote:text,Note:1`; empty by default).
  Every format also stops at `MAX_DOCUMENT_SEGMENTS` (50000 segments per document, 413 when
  over, nothing stored) and `MAX_SEGMENT_CHARS` (100000 characters in one line or paragraph).
  To change either, set it as an environment variable (a platform secret, or `.env`) and restart;
  each extra target locale multiplies the rows an upload writes, so `MAX_DOCUMENT_ROWS`
  (500000 rows, segments x target languages) caps that total too. One connector run (a sync or
  pull from an external source) may buffer at most `CONNECTOR_RUN_CHAR_BUDGET` characters of fetched
  records (50000000 by default; measured at about 70 to 240 MB of peak memory for ordinary
articles and under 1 GB for a pathological source of tiny JSON objects) and is refused whole past it;
  set it the same way. Line-based files also stop
  at 20 lines per allowed segment, and every other format at a budget derived from
  `MAX_DOCUMENT_SEGMENTS` (JSON strings and values, XLIFF and HTML/SVG markup, PO escapes,
  DOCX runs and elements, PDF page objects), so a hostile file is refused before the heavy
  parse. An XLSX counts each text cell as a segment, so a workbook with more than 50000 text
  cells is refused even though `XLSX_MAX_CELLS` allows more cells in total. Large ingests run in a
  bounded worker thread and write in committed chunks, so one big file cannot freeze the
  server (#571).
  The same limits reach every other ingest path (#576): connector pulls and seeders (a
  record, page or run over a limit is refused with the reason on the run, before any row or
  engine call), adding a target language to an ingested document (a clone that would pass
  `MAX_DOCUMENT_ROWS` is skipped when adding a language and refused with a 413 when a
  translation is started), JSON and PO key-file imports, and evaluation datasets. TMX, TBX
  and glossary imports stop at `MAX_DOCUMENT_ROWS` entries per import and roll back whole.
  `DOCX_MAX_PARAGRAPHS` (50000) counts document paragraphs, including empty and table
  paragraphs, before style/tokenization work. This allows long manuals and glossary
  tables while keeping paragraph processing bounded.
  ZIP ratios apply to parts larger than `ZIP_RATIO_MIN_BYTES` (1 MiB); actual expanded
  bytes are verified before parsing. XLSX structure is checked before openpyxl loads it:
  `XLSX_MAX_CELLS` (500000) bounds the sum of worksheet bounding rectangles, including
  merges, hyperlinks, comments and tables, and cumulative cell/reference work (overlaps
  count again). Related comment/table parts are conservatively combined with sheet bounds.
  This default was raised from 250000 so ordinary 20000 x 14 (280000-cell) catalogs fit.
  Rectangle bounds remain necessary because the filter iterates and allocates empty cells;
  sparse workbooks can still be refused even with few populated cells.
  `XLSX_MAX_ROWS` (100000) bounds rows per sheet; `XLSX_MAX_CELL_CHARS`
  (32767, Excel's text limit) bounds cell text. These additional limits prevent a tiny
  sparse or merged sheet from allocating billions of empty cells. They are environment
  settings and apply to document parse/export and rules imports on every surface.
  `XLSX_MAX_TOTAL_TEXT_CHARS` (8388608) also caps loaded/expanded cell text, including
  shared-string and shared-formula references. This additional budget prevents a tiny
  worksheet from multiplying a single long value into gigabytes of ingestion data.
  Request bytes are counted even with Content-Length. Gzip request decoding is not
  supported. Over-cap webhooks return JSON, and SCIM returns its standard error envelope.
  Note the app's own limit is a *ceiling*, not a promise: a TMX/TBX near 100 MB
  made of MANY SMALL units still costs ~30x its size in process memory and
  minutes of synchronous work (measured: 22 MB / 150k units -> ~694 MB, ~122 s),
  so on a 1 GB machine that shape can OOM or outlive a client timeout. The nginx
  sample's `proxy_read_timeout 3600s` covers `location /` (not just SSE), so the
  proxy will wait; the browser and the machine's memory are the real limits.
- **Client IP and proxy trust:** `security.client_ip` is shared by audit and the
  request-identity path. When the `FLY_APP_NAME` platform variable is set, the Fly-Client-IP header is read, accepted only
  from socket peers in `trusted_proxy_ips`; otherwise that header is ignored.
  Set `trusted_proxy_ips` to a comma-separated IP/CIDR list to
  accept X-Forwarded-For only from those socket peers. Walk the chain right to left
  to the first untrusted address; blank trusts no proxy. Restrict direct access and
  ensure proxies append the actual sender. The uvicorn access log records this same
  address. Behind Caddy, set `trusted_proxy_ips` to the proxy's address (for example
  the compose network's subnet); otherwise the access log shows the proxy for every
  request. `/health` and `/ready` always log the socket peer.
- **Proxy forwarding:** every uvicorn launch must use `--no-proxy-headers`
  (`proxy_headers=False` in Python). Docker, the entrypoint, run.py and the QA
  launcher enforce this. The application boundary alone handles forwarded scheme
  and identity, with `trusted_proxy_ips` as the only proxy setting. Local proxy trust
  changes apply live to identity and scheme; environment changes require a
  restart. On every platform, blank trusts no peer, including loopback; configured
  peers must overwrite or safely append proxy headers. Configure only the
  verified edge proxy socket addresses, never an entire private-network or tailnet range.
  Neighbouring machines and userspace tailnet forwarders must remain outside that list.
  The boundary caches its scheme parser and rebuilds it when proxy trust changes.
  A best-effort startup stack check detects an outer uvicorn proxy middleware,
  logs ERROR unconditionally when detected, and adds
  `ingress_config: uvicorn proxy headers on` to `/admin/health` detail without refusing
  boot. ASGI cannot reliably
  expose an original transport peer after an outer rewrite, so custom launchers
  must also disable uvicorn proxy headers.
- **UPGRADE for existing proxy installs (including platform-managed ones):** set, for example,
  `LOC_TMS_TRUSTED_PROXY_IPS=10.0.0.1`, then restart. SAML enabled without trusted
  proxies logs WARNING: "SAML behind a proxy needs trusted_proxy_ips;
  X-Forwarded-Proto is not honored". `/admin/health` includes the warning and stays 200.
  Confirm the actual edge socket addresses for the deployment before configuring
  trust. Without this, audit and lockout use the proxy IP and forwarded HTTPS
  is ignored. Do not guess a private network range or enable wildcard trust.
  The optional Tailscale sidecar is outbound-only: `tailscale up --shields-up`
  rejects incoming tailnet connections. Failed setup stops the daemon before the
  app starts, and `TS_AUTHKEY` is removed from the app environment on every path.
- **Host pin recovery:** there is no loopback exemption. The exact `/health` and
  `/ready` probes may use any syntactically valid Host; they bypass only the Host
  allowlist. Other clients, including metrics scrapes, must send a listed Host when
  `security_allowed_hosts` is pinned. Malformed Host syntax always returns 400.
  Correct `LOC_TMS_SECURITY_ALLOWED_HOSTS` on the host (empty clears the pin),
  or use host `config set security_allowed_hosts ""` / import a corrected
  `config apply` TOML settings file, then restart. Persisted settings live in the
  database Setting table; the TOML file is an operator input to `config apply`.
  Neither ingress setting can be changed through API/MCP or remote CLI.
  Invalid values for either ingress environment override log ERROR and act as
  empty (no proxy trust or no Host pin), without falling back to stored values.
  `/admin/health` detail names each problem as `ingress_config: invalid <key>`.
  A non-empty all-separator Host list is invalid; use an explicit empty value.
  Write doors refuse a non-empty `security_allowed_hosts` list that omits the
  configured `app_base_url` host (case-insensitive, port-independent): add that
  host, or clear/repoint `app_base_url`. Empty remains settable for recovery.
  Existing pins and environment overrides retain syntax-only read validation.
  `app_base_url` is scheme-qualified: `https://host[:port][/prefix]`.
  A `config apply` batch checks the pin against its resulting `app_base_url`.
  When `app_base_url` is configured and the effective pin is empty, `/admin/health`
  detail includes `"host_pin":"unset"` and boot logs one WARNING that the Host
  pin is OFF. Otherwise the `host_pin` key is absent; the default remains `""`.
- **Secure session cookies (`LOC_TMS_SECURE_COOKIES`) - SET THIS behind a proxy.**
  The session cookie (`session`) gets the `Secure` flag only when the
  app knows it is served over https. TLS terminates at your proxy, so the app
  cannot see the scheme and must be told. Values:
  - `auto` (default) - Secure ON when the `FLY_APP_NAME` platform variable is present **or** when
    `LOC_TMS_PUBLIC_BASE_URL` is an `https://…` URL; OFF otherwise (so plain
    `http://localhost` dev login keeps working).
  - `1` / `0` - force Secure on / off.

  A self-hosted install behind nginx/Caddy/cloudflared has no `FLY_APP_NAME`, so
  **set `LOC_TMS_SECURE_COOKIES=1`** (or set `LOC_TMS_PUBLIC_BASE_URL=https://your-domain`)
  or your session cookies will ship without `Secure`. An unrecognized value fails
  the boot loudly. If you set the `app_base_url` setting to an https URL but leave
  Secure resolving off, the app logs a loud warning at every boot.
- **Framing / clickjacking (`LOC_TMS_FRAME_ANCESTORS`).** Every response is sent
  with `X-Frame-Options: DENY` by default, so the app cannot be iframed. If you
  intentionally embed the web UI in another origin, set this to a space-separated
  origin allowlist (e.g. `https://portal.example https://admin.example`); the app
  then emits a `Content-Security-Policy: frame-ancestors …` header instead. The
  value must be a plain origin list - no `;`, control chars, or non-ASCII (those
  fail the boot loudly, to prevent header injection). Leave it blank to stay DENY.

---

### Script Content Security Policy

`LOC_TMS_CSP_MODE` is `enforce` by default. HTML pages, including errors, receive a
per-request nonce policy for scripts, `object-src 'none'`, `base-uri 'none'`, and
`form-action 'self'`. Inline event handlers are blocked. Styles are outside this policy.
The Security page shows the active mode. Restart after changing the environment variable.

For compatibility diagnosis, set `LOC_TMS_CSP_MODE=report-only` to report browser violations
without blocking scripts, or `LOC_TMS_CSP_MODE=off` to omit the script policy. These modes
reduce XSS defense; restore `enforce` after diagnosis. Empty or whitespace-only values
use `enforce`; other invalid values fail startup.
Framing protection and route-specific policies (such as SVG downloads) remain active.

Vendor Studio's handoff permits only the origin of its configured studio URL. Connector
OAuth forms permit their configured provider's authorization origin so form redirects work.
No wildcard or general external form permission is granted. SAML ACS receives forms from
the IdP. SAML uses a plain sign-in link; OIDC uses a button-styled link directly to
`/auth/oidc/login?next=...` with an encoded return path. These GET navigations need no
form exception. OIDC must not start with a form submission: Chromium also checks
`form-action` on the redirect to the IdP. OIDC retains session-bound, one-use state,
nonce validation, and PKCE protections.
See [browser security architecture](architecture/browser-surface.md).

## 4. Required secrets

The app needs two secrets. In production, **set them explicitly** rather than
relying on the auto-generated keyfiles, so you control them and can reproduce
the environment. For sourcing them from a secret manager / cloud KMS, the exact
env/keyfile precedence, and rotation caveats, see **[SECRETS.md](SECRETS.md)**.

| Secret | Env var (either spelling) | What it protects |
|--------|---------------------------|------------------|
| Session secret | `SESSION_SECRET` or `LOC_TMS_SESSION_SECRET` | Signs session cookies; seeds the persisted `.audit_key` once on first audit use, after which rotating it never changes audit signing (see [SECRETS.md](SECRETS.md) §1). |
| Encryption key | `ENCRYPTION_KEY` or `LOC_TMS_ENCRYPTION_KEY` | Fernet key for encrypted uploads and stored connector credentials (Airtable/Jira/Confluence tokens). |

> **Spelling & precedence.** Either spelling works on a normal (non-embedded)
> install; the `LOC_TMS_`-prefixed name wins if both are set, and an existing
> keyfile out-ranks a bare unprefixed var (so setting the env var alone never
> silently overrides a key an install already runs on - see SECRETS.md §1 + §3).
> **In embedded mode (`LOC_TMS_EMBEDDED=1`) only the prefixed spelling is
> honored** (a generic host-exported `SESSION_SECRET` is deliberately not adopted).

> **WARNING - losing `ENCRYPTION_KEY` is unrecoverable.** If you rotate or lose
> it, all Fernet-encrypted uploads and every stored connector token become
> **permanently undecryptable** (the app then behaves as if connections have
> blank credentials). Back this key up somewhere safe and separate from the data
> volume. Changing `SESSION_SECRET` is milder: it invalidates existing sessions
> (everyone must log in again) and breaks verification of the pre-rotation audit
> chain segment.

### Two ways to provide the secrets

**A. Via environment (recommended for production).** Put them in a `.env` next to
the compose file (or your orchestrator's secret store). Generate strong values:

```bash
# session secret (any high-entropy string; hex shown here)
python -c "import secrets; print(secrets.token_hex(32))"
# encryption key (MUST be a valid Fernet key)
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

```dotenv
SESSION_SECRET=<paste the token_hex value>
ENCRYPTION_KEY=<paste the Fernet key>
```

The session secret must be at least 16 characters; shorter values stop the app at
boot (#480). The `token_hex(32)` value above is 64.

Compose passes these into the `app` service. When set via env, the app does
**not** write keyfiles.

**B. Via the keyfiles in the data volume (zero-config default).** If you set
neither env var, on first boot the app generates `.session_secret` and `.enc_key`
inside `/data` (the `loc_data` volume) and reuses them thereafter. This "just
works" for a single instance, but the secrets then live only in the volume - so
your backup of that volume *is* your backup of the keys, and multiple instances
can't share them. Prefer option A for anything beyond a single box.

Provider API keys (optional - the app runs offline on a deterministic mock
engine with none set) are passed the same way: `ANTHROPIC_API_KEY`,
`GEMINI_API_KEY`, `OPENAI_API_KEY`.

---

## 5. The data volume (`/data`) - contents & backup

Everything stateful lives in the single `loc_data` volume mounted at `/data`
(`LOC_TMS_DATA_DIR=/data`):

- `loc_tms.db` - the SQLite database (only in SQLite mode).
- `uploads/` - uploaded source documents (may be Fernet-encrypted at rest).
- `.session_secret` - session-signing / audit-HMAC key (only if not set via env).
- `.enc_key` - the Fernet encryption key (only if not set via env).
- rulebooks / prompt files and other runtime artifacts.

**Back this volume up regularly.** It contains your documents and, in the default
keyfile mode, your only copy of the encryption key. Losing it loses your data;
losing the key inside it loses the ability to decrypt uploads + connector tokens.

Use the full-install archive workflow in Upgrade below for both backends. It
snapshots the SQLite database with the online backup API or uses PostgreSQL
custom-format pg_dump, plus uploads and keyfiles. Quiesce all writers while
capturing a checkpoint so database references and ancillary files agree.
PostgreSQL clients (pg_dump and pg_restore) are included in the image.
Store checkpoints encrypted off the machine; env/KMS secrets require a separate
recoverable secret-store backup. A tar of a live SQLite volume is not a substitute.

Full backup/restore procedures for both backends (WAL checkpointing, cold vs hot
snapshots, `pg_dump`/`pg_restore`, and the DATA_DIR-with-DB consistency rule) are
in **[BACKUP.md](BACKUP.md)**.

---

## 6. Runtime notes / gotchas

- **No `--reload` in the container.** The image runs
  `uvicorn app.main:app --no-proxy-headers --host 0.0.0.0 --port 8000` directly. `run.py`
  (`reload=True`) is dev-only and is not the container entrypoint - the reloader
  can crash mid-batch and is unsafe for production.
- **Binds `0.0.0.0` inside the container, but sits behind the proxy.** Inside the
  container the app listens on all interfaces so the proxy can reach it; the
  container's port is published only on `127.0.0.1` on the host. Public traffic
  goes through the proxy.
- **Single-process job engine.** The background job queue and scheduler are
  in-memory daemon threads (one process). Run a **single** app replica; scaling
  to multiple instances needs a shared broker (not built yet). A restart
  re-queues never-started jobs and marks mid-run jobs as interrupted (visible,
  not silently lost).
- **Non-root by default.** The container runs as the unprivileged `app` user; the
  `/data` volume is owned by it on first create.
- **Postgres migrations run on boot.** With a `postgres...://` `DATABASE_URL`, the
  entrypoint runs `alembic upgrade head` before uvicorn starts. SQLite skips this
  (the app's `init_db()` manages its schema).
- **Executable bit on the entrypoint.** `docker-entrypoint.sh` is invoked via the
  Dockerfile which `chmod +x`'s it during build, so it works even when git on
  Windows did not preserve the `+x` bit in the checkout.
- **Structured JSON logging (opt-in).** Set `LOG_FORMAT=json` (optionally
  `LOG_LEVEL=INFO`) to make the app **and** uvicorn's access/error logs emit one
  JSON object per line (keys: `ts`, `level`, `logger`, `message`, plus
  `traceback`/`exc_info` on errors) for a log shipper. It's off by default
  (`LOG_FORMAT=text`) so behavior is unchanged unless you opt in. Implemented with
  the stdlib only (`app/logging_config.py`) - no extra dependency.
- **Reproducible installs.** `requirements.txt` is the curated direct deps;
  `requirements.lock` (from `pip freeze`) pins the entire transitive tree. For a
  reproducible package inventory, install from the lock. The Dockerfile already
  installs the base and Postgres locks with `--no-deps`; image checks verify
  dependency completeness and installed versions. OS inputs are not byte-pinned.
- **Fonts for translated-PDF export.** The PDF writer (typst) renders with the host's
  system fonts. The Docker image installs `fonts-noto-core` + `fonts-noto-cjk`, which
  cover every default locale. On a bare (non-container) Linux install, non-Latin scripts
  may show missing glyphs unless you install `fonts-noto-core fonts-noto-cjk` (or point
  `LOC_TMS_PDF_FONT_DIR` at a directory of fonts); the app logs one warning at first
  export when it detects neither.

---

## 7. Headless first-admin bootstrap (optional)

A container/CI install can seed its first admin **without** the interactive
`/auth/setup` wizard using the headless CLI (`docs/CLI.md`). The `init` command is
in-process (no server needed), idempotent, and **default-off** - with the env vars
unset, boot behaves exactly as before.

```bash
# before uvicorn: create the admin from env, mint a docs-scoped API key once
LOC_TMS_ADMIN_EMAIL=admin@example.com \
LOC_TMS_ADMIN_PASSWORD='a-strong-passphrase' \
  python -m app.cli init --from-env --issue-key
```

- Enforces the 12-character password minimum and turns on the login wall.
- Idempotent: if an account already exists it no-ops (safe to run on every boot).
  It shares the wizard's atomic first-account guard (#414), so `init` and a setup
  form submitted at the same moment can never both create an admin. `init` needs
  no setup token: it runs on the host, and it removes the token file.
- `--issue-key` prints the key **once** - capture it from stdout / the `--json`
  `api_key` field; it is never recoverable afterwards.

To wire it into the container, uncomment the guarded `init --from-env` line in
`docker-entrypoint.sh` (it only fires when both `LOC_TMS_ADMIN_*` env vars are
set). The same CLI also does offline ops: `python -m app.cli db backup <path>`,
`config apply provisioning.toml`, `audit verify`, `health`. See `docs/CLI.md`.

## Compose image scan gate

Required for every first launch, release upgrade and rollback. Use Bash on the
Docker host with Trivy and its vulnerability database available. Set release build
args as above. Before this procedure, export `COMPOSE_FILE` with the deployment's
base and companion files (colon-separated on this POSIX host) and `COMPOSE_PROFILES`
with the enabled profiles. For Caddy use
`COMPOSE_FILE=docker-compose.yml:deploy/compose-proxy.yml` and
`COMPOSE_PROFILES=proxy` (or `proxy,postgres`). Include any other deployment overrides.
For each new build, remove the previous generated scan override from `COMPOSE_FILE`.
Keep the project name, secrets, volumes and profiles unchanged through maintenance
and start. Do not run concurrent image builds or deployments in this project.

Build without starting. Pull and scan every enabled non-app service, including the
applicable Caddy proxy and Postgres, and resolve every image to its local immutable
ID. The generated override pins each service to a local tag named after its scanned
ID (`localization-os-scanned/<service>:<id>`), so a moved tag cannot change what starts.

```bash
export COMPOSE_FILE="${COMPOSE_FILE:-docker-compose.yml}"
docker compose build app || exit 1
SERVICES="$(docker compose config --services)" || exit 1
SCAN_OVERRIDE="$(mktemp ./compose-scanned.XXXXXX.yml)" || exit 1
printf 'services:\n' > "$SCAN_OVERRIDE" || exit 1
for SERVICE in $SERVICES; do
  if [ "$SERVICE" != app ]; then
    docker compose pull "$SERVICE" || exit 1
  fi
  IMAGE_REF="$(docker compose config --images "$SERVICE")" || exit 1
  IMAGE_ID="$(docker image inspect --format '{{.Id}}' "$IMAGE_REF")" || exit 1
  sh scripts/scan_image.sh "$IMAGE_ID" || case " ${ACCEPTED_SCAN_RISK_IDS:-} " in
    *" $IMAGE_ID "*) echo "ACCEPTED-SCAN-RISK $SERVICE $IMAGE_ID" ;;
    *) exit 1 ;;
  esac
  PINNED="localization-os-scanned/$SERVICE:${IMAGE_ID#sha256:}"
  docker tag "$IMAGE_ID" "$PINNED" || exit 1
  printf '  %s:\n    image: "%s"\n' "$SERVICE" "$PINNED" >> "$SCAN_OVERRIDE" || exit 1
done
export COMPOSE_FILE="$COMPOSE_FILE:$SCAN_OVERRIDE"
```

Any scan finding (exit 1) or scanner error (exit 2) stops the procedure before
activation of the override. Fix and rerun; an exception requires the operator's
written risk acceptance (the operator for each hosted install) for each exact image ID
and findings/error report in the change log before proceeding. Only then rerun with
`ACCEPTED_SCAN_RISK_IDS` holding those exact `sha256:` IDs (space-separated); no other
image passes a failed scan. File the IDs,
reports and override on the Vulnerability Scan Record. A general deployment
approval or an SBOM alone does not satisfy this gate. Keep the override for all
subsequent Compose commands; never rebuild or pull between scanning and starting.
Rebuild and rescan the image on a regular schedule (the MONTHLY in-season rebuild) so base-image fixes are picked up.

Before EVERY start below (first launch, upgrade, rollback), in the same shell, confirm
each service still resolves to its scanned ID:

```bash
for PINNED in $(docker compose config --images); do
  case "$PINNED" in localization-os-scanned/*) ;; *) echo "UNPINNED $PINNED"; exit 1 ;; esac
  [ "$(docker image inspect --format '{{.Id}}' "$PINNED")" = "sha256:${PINNED##*:}" ] || exit 1
done
```

For a first launch, start only after all scans pass and the check above, in the same shell:

```bash
docker compose up -d --no-build --pull never
```

For an upgrade, return to the migration checks below before starting. For rollback,
restore the checkpoint as described below, select the recorded old source and image,
then scan and pin that image and the applicable proxy before serving; omit the build
line when reusing the recorded old image. An old release is not exempt from scanning.
The nightly backup job (`scripts/nightly-backup.sh`) restarts the stopped container
as it was (`docker compose start app`) and never recreates it from a tag; give its
scheduler entry the same `COMPOSE_FILE` (with the scan override), `COMPOSE_PROFILES`
and `IMAGE_TAG` as the deployment.

## Installation schema authority

Fresh real SQLite installs (LOC_TMS_SERVER=1) run Alembic upgrade head from
init_db. Stamped installs also run Alembic, applying pending revisions before
serving. The test-only path uses create_all plus self-heal shims.

Legacy SQLite databases with existing tables but no alembic_version use the
pre-Alembic create_all/self-heal fallback and log a warning. This fallback cannot
apply general non-additive migrations such as drops and constraint changes.
Do not stamp head to hide the gap. Stop the upgrade and arrange a tested data
migration into a fresh Alembic-managed database, preserving identities, uploads
and secrets; deleting the old database is not a data migration.

Postgres init_db requires an initialized Alembic schema. The container entrypoint
runs alembic upgrade head before starting uvicorn; host installs must run that
migration explicitly. Use the base and Postgres locks, including psycopg.

## Upgrade

Run these POSIX shell commands from the deployment checkout. Use the same Compose
project name, environment/secrets, profiles and volumes throughout. Keep the old
image locally and record its digest before selecting the next release. Substitute
your approved tag or commit for RELEASE_REF; source must already be available
for an offline upgrade. The image builds this selected checkout, not a registry
pull. Do not upgrade from a dirty checkout.

```bash
git status --short
git rev-parse HEAD
docker compose images
```

Block ingress, stop the app and all other writers, then capture a pre-upgrade
checkpoint using the OLD image. The entrypoint is overridden on maintenance
commands so it cannot migrate the database before the checkpoint. On an older
release without the backup-full alias, use the equivalent `db backup --full`
command. Copy the
archive off the data volume and verify the copied file before proceeding.

```bash
docker compose stop app
docker compose run --rm --no-deps --entrypoint python app -m app.cli db backup-full /data/checkpoints/pre-upgrade.tar.gz
docker compose run --rm --no-deps --entrypoint python app -m app.cli db verify-backup /data/checkpoints/pre-upgrade.tar.gz
docker compose cp app:/data/checkpoints/pre-upgrade.tar.gz ./pre-upgrade.tar.gz
```

Select the source. Bake git describe and the full commit into RELEASE and
RELEASE_COMMIT in the image; IMAGE_TAG is a Docker-compatible commit identifier.
Without baked identity, version falls back to pyproject.toml. A digest cannot
be known until after build; LOC_TMS_IMAGE_DIGEST may be supplied by deployment
metadata, otherwise the manifest explicitly reports null.

```bash
git checkout --detach RELEASE_REF
export RELEASE="$(git describe --tags --always --dirty)"
export RELEASE_COMMIT="$(git rev-parse HEAD)"
export IMAGE_TAG="$RELEASE_COMMIT"
# Build and pin images using the scan gate before the migration checks.
```

Complete the [Compose image scan gate](#compose-image-scan-gate) now, including
the applicable proxy. Keep its exported `COMPOSE_FILE` for every command below;
do not run the first-launch start command during an upgrade.

Before migration, inspect the new image's graph and the current database stamp.
Require `alembic heads` to show exactly one head and `alembic current` to show
a current revision in its ancestry. An
unstamped legacy database is a STOP condition, handled above. Review release
notes for irreversible data changes. Maintenance commands use the new image
with the entrypoint bypassed; none should start the server yet.

```bash
docker compose run --rm --no-deps --entrypoint alembic app heads
docker compose run --rm --no-deps --entrypoint alembic app current
docker compose run --rm --no-deps --entrypoint python app -m app.cli release manifest --json
docker compose run --rm --no-deps --entrypoint python app -m app.cli db migrate
docker compose up -d --no-build --pull never
curl -fsS http://127.0.0.1:8000/health
docker compose exec app python -m app.cli version
docker compose exec app python -m app.cli db verify
```

Final-state validation requires the `version` command output to equal the selected
RELEASE, db verify to pass with the database stamped at the single head, and a
functional read of saved content, identities and uploads. Use `version` (or
`release manifest`), not `/health`, as the authoritative release check: `/health`
is anonymous and reports only the coarse major.minor liveness form (or `unknown`
when the baked RELEASE is a bare commit hash or `git describe` output that does not
parse as major.minor, which is what this document's own `git describe --tags
--always --dirty` recipe emits on a tag-less checkout), so it confirms the app is
up and roughly which line it is on but never pins the exact release. A
skipped revision check is not an upgrade pass. Retain the checkpoint until
acceptance completes.

For a failed upgrade, stop every writer and block ingress. **never run an old image against a new schema**.
Use the new image's recovery CLI with the entrypoint bypassed to restore the
checkpoint (database, uploads and keyfiles together), then select the recorded
old source and image only AFTER the schema has been restored. Never use an
Alembic downgrade as a substitute for recovering changed data. Before the old image
serves again, run the [Compose image scan gate](#compose-image-scan-gate) on it and the
applicable proxy (omit its build line when reusing the recorded image), then its pin
check, and only then `docker compose up -d --no-build --pull never`.

```bash
docker compose stop app
docker compose run --rm --no-deps --entrypoint python app -m app.cli db restore /data/checkpoints/pre-upgrade.tar.gz --yes
```

For PostgreSQL, this command reports **PARTIAL** and **exits 1** after replacing
uploads and keyfiles and extracting the dump; it does NOT restore the database.
JSON reports `ok: false`, `status: "partial"`, `db_restore_required: true`.
Treat this as incomplete recovery, not permission to start the app. Before
starting any image, complete the emitted pg_restore command with all writers
stopped. Supply credentials through PGPASSWORD or ~/.pgpass. Check its exit
status, then verify the restored database using the matching old release.
A pg_restore --list pass validates a table of contents, not end-to-end
recoverability. Missing pg_restore reports UNVERIFIED
because the dump was never validated; do not accept that checkpoint for upgrade.

A checkpoint captured by a pre-P2a image carries no recorded dump size or pg_dump
version. On this newer image such a checkpoint verifies as UNVERIFIED with a
reason of: dump metadata not recorded (archive written by an earlier release).
That is expected and NOT corruption: the pg_restore --list validation still runs,
and `db restore` of such a checkpoint proceeds with ancillary recovery (PARTIAL,
exit 1 on PostgreSQL; the manual database restore is still required). Only an
UNVERIFIED caused by missing pg_restore blocks the restore. A full recovery drill
proves the contents, identities and files together.

Image tag: this compose file builds `localization-os:${IMAGE_TAG:-local}` (the tag
was previously the fixed `localization-os:latest`). Any operator script, monitoring
rule or hand-written `docker` invocation that named `localization-os:latest` must be
updated to the selected `IMAGE_TAG` (a commit identifier during upgrade) or to
`localization-os:local`, the new default when `IMAGE_TAG` is unset.

## Nightly backup and recovery targets

The documented single-node targets are **RPO 24 h** via nightly backup-full and
**RTO 1 h**. Schedule a root-owned host script with cron at 02:00 daily (or a
Windows Task Scheduler daily trigger on a Windows host). Use an absolute working
directory, the deployment environment and a restricted backup destination.
The script must quiesce writers, create a uniquely dated checkpoint using the
backup-full and verify-backup commands above, copy it to encrypted off-host
storage, verify that copy, and restart the app. Alert on every nonzero exit,
UNVERIFIED result or missing nightly archive; retain previous successful copies.
The backup job needs access to the same env/KMS secrets as the deployment.
BACKUP_DIR must be writable by the container app UID/GID; restrict its host
permissions before installing the scheduler job.

The shipped `scripts/nightly-backup.sh` writes directly into BACKUP_DIR, a mounted
encrypted off-host destination, then verifies that stored archive. It restarts the
app on both success and failure; configure failure alerts in the scheduler. Stop
external writers and block ingress separately. Use a single scheduler instance to
prevent overlapping jobs.

Retention: the script keeps the newest `KEEP` verified archives (default 14, about two
weeks at one per night; `KEEP` must be a whole number of at least 1) and prunes older
`nightly-*.tar.gz` ONLY after the run's verify passes, so a failed run never deletes a
good older copy. An archive is written under a temporary name and takes the
`nightly-*` name only once it has verified, so a failed archive (deleted) or an
UNVERIFIED one (kept as `unverified-nightly-*`) never counts toward `KEEP`. Set `KEEP` to size the
backup mount deliberately; without a bound a full-install archive that includes the
uploads tree fills the mount in weeks and the next backup fails. Alerting: set
`ALERT_CMD` to a command the script runs with a one-line message on any nonzero exit,
so the scheduler can page on a setup or backup error (exit 1, including an unmounted
BACKUP_DIR, an invalid `KEEP` or a failed app stop or restart), a FAILED verify from
corruption (exit 2), an UNVERIFIED verify (exit 3, e.g. pg_restore absent) or a
retention error (exit 4). Downtime: the job
stops the app for the whole backup, so the install is offline until the archive is
written, a window that grows with the uploads tree (on SQLite the DB copy is a fast
online snapshot; uploads dominate). Factor that window and the copy time into the
RTO 1 h target.

Example cron schedule (install this checkout at /opt/localization-os;
percent signs in a date expression belong in the script, not the crontab):

```text
0 2 * * * APP_DIR=/opt/localization-os BACKUP_DIR=/mnt/encrypted-backups sh /opt/localization-os/scripts/nightly-backup.sh >> /var/log/localization-os-backup.log 2>&1
```

The recovery drill belongs on a disposable AWS test instance with disposable data.
That timed exercise is separate from this train; these are targets, not measured
recovery results. See the full recovery runbook for the drill checklist.

## Memory profiles

Use the [memory sizing profiles](architecture/configuration.md#memory-sizing-109-round-1)
for a single process with external inference. These are conditional estimates, with
uncovered work paths listed there; they are not complete RSS guarantees.

| Host | Run cap | Pool + overflow | Cache KiB | Anyio / streams / run children / jobs | Estimate MiB (unmeasured planning reserve) | Budget MiB |
|---|---:|---|---:|---|---:|---|
| 1 GB shared-cpu VM | 8 | 20 + 40 | 1000 | 16 / 32 / 4 / 2 | 5002.6 | unset; no measured profile |
| 2 GB | 4 | 20 + 20 | 1024 | 8 / 32 / 2 / 2 | 3320 | unset; no measured profile |
| 4 GB | 8 | 20 + 40 | 1000 | 16 / 32 / 4 / 2 | 5002.6 | unset; no measured profile |
| 8 GB desktop | 16 | 64 + 64 | 1024 | 24 / 32 / 4 / 2 | 6288 | 8192 |

Table note: the 1 GB row preserves main's cap 8 and pool 20/40 defaults.
Recommendations must not go below these until a measured profile exists. The modeled
sum exceeding 1 GB is not evidence to reduce production capacity or impose a boot budget.

| Evidence | MiB | Method / status |
|---|---|---|
| Stack per modeled thread | 8 | unmeasured planning reserve |
| Default stream thread stacks | 256 | 32 x 8 MiB = 256 MiB; included in the thread allowance, not added twice |
| Working set per modeled parent | 32 | unmeasured planning reserve |
| Baseline | 256 | unmeasured planning reserve |
| Eight concurrent mock runs, peak growth | 46 to 50 MiB | Measured on Windows Python 3.13.9 using GetProcessMemoryInfo / WorkingSetSize sampled every 10 ms from a warm app baseline; 16 segments/run, barrier-held first mock provider call, completion and recovery, 16 keys x 3 locales fan-out and HTTP/CLI/MCP overhead. Two observations: 46.3 and 50.4 MiB. Not a production input-size coefficient. |

Host settings: `LOC_TMS_MAX_CONCURRENT_RUNS`, `LOC_TMS_DB_POOL_SIZE`,
`LOC_TMS_DB_MAX_OVERFLOW`, `LOC_TMS_SQLITE_CACHE_KB`, `LOC_TMS_ANYIO_WORKERS`,
`LOC_TMS_STREAM_WORKERS` (default 32), `LOC_TMS_RUN_WORKERS`, `LOC_TMS_JOB_WORKERS`
and `LOC_TMS_MEMORY_BUDGET_MB`. Anyio defaults to 16 for sync routes; streams and long waits are off
it and wait on their dedicated stream limiter. Excess streams do not block sync routes.
LOC_TMS_STREAM_WAIT_S defaults to 5 seconds and bounds stream-token admission. Saturated wait=true returns the non-waiting response with current status, waited: false and retry_after; poll the run or batch status. Saturated SSE returns HTTP 503, code stream_capacity and Retry-After, before response headers start. The shared refusal is: Stream capacity is busy. Retry shortly; existing work continues. CLI and MCP relay that message verbatim. Size LOC_TMS_STREAM_WORKERS above the expected watcher count: one token per open SSE tab or waiting caller, held up to 900 seconds per silent SSE next() or 3600 seconds per run wait, including disconnected non-abandonable workers.
Defaults match the 1 GB and 4 GB rows. All rows retain 32 stream workers. Budgets below the planning sum remain unset pending measurement.

No production memory profile is claimed. The estimate bounds modeled work within one process only.

### Showcase demo instance

Use a dedicated install. `LOC_TMS_DATA_DIR` selects the database, and `--data-dir`
overrides it before app imports. Run one seed process at a time:

```text
python scripts/showcase_seed.py --data-dir /data/showcase --dry-run --settings
python scripts/showcase_seed.py --data-dir /data/showcase --settings --spend-cap 25 --retention-days 7 --allowed-hosts demo.example.com --mint-key operator@example.com
```

The additive, idempotent seed creates `Showcase (seeded)`, an approved General
glossary with eight Acme Cloud terms across five language pairs (three DNT brands),
and universal Quick Translate and Reviewed Translate workflows. Reviewed adds an
LQA gate at the service's 90/100 pass threshold. The customer settings manifest
records owned IDs; subsequent runs preserve existing parents and repair missing
ones. An existing customer with that name but no seed manifest is refused.

Default runs leave install settings alone. Studio fails closed after a default seed
run unless `advanced_workflows_enabled=1` was already set. Studio needs `--settings`
or `advanced_workflows_enabled=1`. `--settings` writes the eight advertised
`enabled_locales`, a positive finite `spend_cap_usd` (default 25),
`retention_enabled=1`, `retention_days` (default 7), and
`advanced_workflows_enabled=1`. Optional `--allowed-hosts` writes
`security_allowed_hosts`; validation requires the configured `app_base_url` host
to be included. Save the printed before/after table for rollback. Retention also
needs the install's scheduler running.

`--mint-key EMAIL` requires an existing active user and issues an `api docs`-scoped
`Showcase BFF (seeded)` key. Copy the plaintext immediately; it is printed once.
Repeating the command reuses that user's active manifest key with both grants
without revealing it. A legacy manifest key missing `docs` is replaced and revoked
after the new key is recorded. Both `/api/projects` and document routes accept the
new grant; the verifier proves HTTP 200 on project listing and document segments.

Studio Translate is a per-visitor mission, not a seeded workflow. The script
prints its two-MockProvider committee and finite budget fences.
`create_studio_mission(session_or_none, project_id, document_ids=None, *,
org_id=None, customer_id=None)` accepts a committed, single-target visitor project.
The BFF must pass its resolved `org_id` and the showcase `customer_id`, and enforce
visitor ownership. Both rings are forwarded to the mission service. The default
`None` rings are reserved for trusted in-process callers. Studio's
roster is explicitly mock; configure a reviewed live roster separately before
offering live Studio translations. Classic workflows use install-default engines.

`--dry-run` prints a plan without creating seed rows or changing settings values.
A fresh `--dry-run --settings` initializes the schema through the app boot seam
so every value can use the public validator.
`--cleanup` revokes keys by manifest ID only,
deletes manifest workflows (also matching `created_via="showcase-seed"` AND the
showcase customer ID, never workflows belonging to another customer), then the
manifest glossary, then asks the service to delete the customer. Other glossary
or workflow names alone never authorize deletion. Projects and missions remain
for retention purge. A customer reference refusal prints the service reason and
exits 2; rerun cleanup after retention removes the references. Cleanup dry-run
projects the same reference counts after planned parent removal and prints
`would refuse (still referenced by ...)` when customer deletion would be blocked. A clean install
exits 0 with its seeded customer gone. Unexpected errors exit 1.

Cleanup does not revert settings. Restore each saved before-value with
`python -m app.cli config set <key> <before-value>` against the same data directory.
Offline execution evidence: every advertised tier x pair is proven by
`tests/verify_showcase_pairs.py`. Its coverage is all 56 ordered pairs for each
classic tier and eight Studio runs, one pair per source locale. Five additional
Reviewed runs inject an MT provider that mangles every input word: the real
matcher and provider payload prove DNT entries were offered to the step. This
path offers DNT guidance but does not guarantee verbatim preservation against a
provider that ignores it; S2 must not present this evidence as a locked-term badge.
