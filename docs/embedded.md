# Localization OS TMS - Embedded / Headless Deployment Guide

Operator guide for running Localization OS **embedded inside another product** or as a
**headless API service** driven by scripts, agents, or a host application. It
consolidates the embedded profile in one place: what the mode changes, the full
environment matrix, the Postgres backend, the external-cron pair, trust-host-auth,
and the MCP sidecar - plus the **supported-configuration** statement.

This is **composable-core phase 0** (composable-core roadmap, §7
decision record). It cross-links with [DEPLOY.md](DEPLOY.md) (the general container
guide), [SECRETS.md](SECRETS.md) (the two runtime secrets), [CLI.md](CLI.md) (the
headless surface), and the `/help/llm` capability card (`app/capabilities/llm.md`,
the machine-facing reference). The README "Embedded / headless mode" section is the
short version; this file is the operator's long form.

> **Prime invariant.** The default (non-embedded) install is byte-identical to a
> normal deploy. Nothing here changes behavior unless `LOC_TMS_EMBEDDED=1` is set,
> and trust-host-auth adds a *second* required condition on top of that.

---

## 0. TL;DR

```bash
# Headless API service on SQLite, one command (see the compose file in section 5
# for the full example, including the optional Postgres backend).
LOC_TMS_EMBEDDED=1 LOC_TMS_SERVER=1 \
  uvicorn app.main:app --no-proxy-headers --host 0.0.0.0 --port 8000

# Then bootstrap the first admin + a key HEADLESSLY (there is no /auth/setup page):
python -m app.cli init --from-env --issue-key --key-scopes "docs keys admin"
# -> reads LOC_TMS_ADMIN_EMAIL / LOC_TMS_ADMIN_PASSWORD, enables the login wall,
#    and prints one `lsu_` key carrying the docs + keys + admin scopes.

# Drive the passage of time from an external cron (the in-process scheduler is off):
curl -fsS -X POST -H "X-Loc-Token: lsu_..." http://host:8000/api/admin/tick
```

---

## 1. What embedded mode changes

Setting `LOC_TMS_EMBEDDED=1` recomposes the app as an **API-only** service
(`app/config.py` → `IS_EMBEDDED`, consumed in `app/main.py`):

| Surface | Normal server | Embedded (`LOC_TMS_EMBEDDED=1`) |
|---|---|---|
| Jinja web UI (`/`, `/projects`, `/settings`, …) | served | **not mounted** (a page path is bounced by the wall / 404s) |
| `/static/*` | mounted | **not mounted** → 404 |
| In-process scheduler daemon | started | **not started** (drive it via `POST /api/admin/tick`) |
| Retention purge daemon | started | **not started** (drive it via `POST /api/admin/retention/purge`) |
| Login wall (`security_auth_required`) | ON | **ON** - it is the master switch for `/api` role/scope RBAC |
| `/api/*`, `/webhooks/*`, `/openapi.json` (session or API key), `/utcp` | served | **served** (the whole machine surface stays live) |
| `/health`, `/ready` | served | **served** (container liveness) |

`LOC_TMS_EMBEDDED=1` **implies `LOC_TMS_SERVER=1`** - an embedded install is a real
install, never a test boot, even though it (legitimately) sets `LOC_TMS_DATA_DIR`
for its volume. Everything *above* the daemon tail (init_db, resolver install,
settings apply, crash recovery) runs unchanged; embedded still needs all of it.

The login wall staying **ON** is deliberate: `/api` is token-authed (`X-Loc-Token`),
never session-authed, so walling it costs the headless surface nothing - but without
the wall a valid token would write at any role. The regression guard for all of this
is `tests/verify_embedded_profile.py` (`EMBEDDED PROFILE OK`), which boots embedded,
asserts the assembly, and - in a sibling child process - proves the normal boot is
unchanged and serves a *superset* of the embedded routes.

---

## 2. Environment matrix

Everything an embedded deployment sets. Boot-time env is read once at process start
(`app/config.py`); a Setting is stored in the DB and read live; the trust vars are
env, gated behind a Setting (section 6).

### Mode + storage (boot-time env)

| Variable | Default | Meaning |
|---|---|---|
| `LOC_TMS_EMBEDDED` | unset | `1` → API-only assembly (this whole guide). Implies `LOC_TMS_SERVER`. |
| `LOC_TMS_SERVER` | unset | `1` → real-install posture (wall on, no seeded dev accounts). Set explicitly too for belt-and-suspenders. |
| `LOC_TMS_DATA_DIR` | `./data` | The volume holding uploads, keyfiles, and (on SQLite) the DB. Under embedded it does **not** mark the boot as a test run. |
| `DATABASE_URL` | unset → SQLite | Set `postgresql+psycopg://user:pass@host:5432/db` to use Postgres (section 5). Unset = zero-config SQLite. The psycopg driver is an opt-in extra: `pip install -r requirements.txt -r requirements-postgres.txt` (the container image bundles it; the bare `pip install -r requirements.txt` install does not). |
| `LOC_TMS_PDF_FONT` | unset | Font family **name** to prefer for **PDF export** of translated documents (the typst writer). Unset → typst uses the host's installed fonts with automatic shaping + fallback. The container image ships `fonts-noto-core` + `fonts-noto-cjk`, which cover every default locale (CJK, Arabic/Hebrew RTL, Devanagari/Bengali, Thai, Cyrillic/Greek). On a bare Linux host with no non-Latin fonts and no override, non-Latin scripts may show missing glyphs - install `fonts-noto-core fonts-noto-cjk` or set `LOC_TMS_PDF_FONT_DIR`; the app logs one warning at first export in that case. |
| `LOC_TMS_PDF_FONT_DIR` | unset | Directory (or `os.pathsep`-separated list of directories) of extra TTF/OTF font files to make available to the PDF writer, in addition to the host's system fonts. Use it to add a specific font on a host where installing system font packages isn't an option. |

### Secrets (boot-time env - see [SECRETS.md](SECRETS.md))

| Variable | Default | Meaning |
|---|---|---|
| `LOC_TMS_SESSION_SECRET` | keyfile fallback | Signs session cookies; seeds the persisted `.audit_key` once on first audit use (later rotations never change audit signing; see [SECRETS.md](SECRETS.md) §1). **Pin it from your secret manager** on a container without a persistent volume (an auto-generated keyfile regenerates every boot → everyone logged out). |
| `LOC_TMS_ENCRYPTION_KEY` | keyfile fallback | Fernet key for encrypted uploads + stored connector credentials. **Losing it is unrecoverable.** Pin it and back it up separately from the data volume. |

> **Env-var spelling - read this.** In **embedded mode the `LOC_TMS_`-prefixed
> names are the ONLY honored spelling** for these two secrets: use
> `LOC_TMS_SESSION_SECRET` / `LOC_TMS_ENCRYPTION_KEY`. This is by design, not a
> gap. `SESSION_SECRET` / `ENCRYPTION_KEY` are generic names a *host* application
> very plausibly exports for its own use, and an embedded Localization OS must not
> silently adopt the host's secret - if the host later rotated it, Localization OS's
> sessions would drop and its Fernet-encrypted data would become unreadable. So an
> unprefixed var is deliberately ignored in embedded mode. When that ignored var
> causes the install to fall back to a **freshly generated** keyfile (the
> volumeless regenerate trap), the app logs a WARNING naming it and the prefixed
> fix, so the miss is visible rather than silent; an embedded install already
> running from a **persisted** keyfile ignores the unprefixed var **silently** - it
> is correctly keyed. On a **non-embedded** install both spellings work (prefixed
> wins on conflict) - see [SECRETS.md](SECRETS.md) §1.

### First-admin bootstrap (boot-time env, consumed by `init --from-env`)

| Variable | Meaning |
|---|---|
| `LOC_TMS_ADMIN_EMAIL` | The admin account's email, seeded by `python -m app.cli init --from-env`. |
| `LOC_TMS_ADMIN_PASSWORD` | The admin account's password (12-char minimum). Never echoed. |

### Trust-host-auth (boot-time env - only active under the double condition in section 6)

| Variable | Default | Meaning |
|---|---|---|
| `LOC_TMS_TRUST_PRINCIPAL` | - (**required** when trust mode is on) | The dedicated principal's email; the audit `actor` for every tokenless call. Missing/malformed → the boot fails loudly. |
| `LOC_TMS_TRUST_SCOPES` | `api docs keys` | API-key **scope** grant (`docs`, `keys`, `admin`). |
| `LOC_TMS_TRUST_ROLE` | `viewer` | RBAC **role** (least privilege - read-only). `admin` requires `admin` in the scopes too. |
| `LOC_TMS_TRUST_ORG_ID` | - | **Required when `multi_org_enabled=1`**: the single org the principal binds to. Ignored when multi-org is off. |
| `LOC_TMS_TRUST_HEADER_SECRET` | - (optional) | **(b) source restriction.** A shared secret the host proxy injects as the `X-Loc-Trust-Secret` header on every request; when set, a tokenless request without the matching header gets **401** (it can only *subtract* trust). Blank/unset **and no plural `LOC_TMS_TRUST_HEADER_SECRETS` accept-list set** → 0b behavior (every peer that reaches the port is trusted - a boot **warning** fires). For zero-cutover rotation, set the plural **`LOC_TMS_TRUST_HEADER_SECRETS`** (a comma-separated accept-list) instead - it is read **first** and **takes precedence** when set. An empty or whitespace-only plural is treated as **unset** (it falls through to the singular), whereas a comma-bearing value that resolves to no real element (`","`, `",,,"`, `" , "`) **fails the boot closed**. Must be **ASCII printable** (a non-ASCII value - or one with an interior control char that no HTTP header can carry - is refused at boot; either could never match the latin-1 wire header); `< 32` chars → a weak-secret boot warning. Read at boot only (rotation needs a restart); never logged, never in the DB. There is **no env var for the `X-Loc-Trust-User` header name** - it is fixed, see section 6. |

### Settings (stored in the DB; set on the host with `config set`, never over `/api`)

| Setting | Default | Meaning |
|---|---|---|
| `embedded_trust_host_auth` | `0` | Enables tokenless `/api` under the double condition (section 6). On `REMOTE_IMMUTABLE_KEYS` - it can never be flipped over the API it guards. |

---

## 3. Bootstrapping the first admin (headless)

There is no `/auth/setup` page in embedded mode, so seed the admin from the CLI -
in-process, no server needed (e.g. over `docker exec`):

```bash
python -m app.cli init --from-env --issue-key --key-scopes "docs keys admin"
```

This reads `LOC_TMS_ADMIN_EMAIL` / `LOC_TMS_ADMIN_PASSWORD`, enables the login wall,
and prints one `lsu_` key carrying all three per-surface scopes:

- `docs` - the document translation API,
- `keys` - string keys + the Delta API (`/api/projects/{pid}/changes`: a `since` cursor feed with tombstones, or a `modified_after` current-state feed),
- `admin` - the tick + admin verbs.

`init` is idempotent (it no-ops once an account exists), so it is safe to leave in a
container entrypoint. The `docker-entrypoint.sh` init block ships **commented out by
default**; uncomment it to run the bootstrap automatically when both admin env vars
are present.

---

## 4. The external-cron pair

Because the in-process daemons are off, an external scheduler drives the two things
they normally do. Both mirror a CLI verb, so you can run either over `--remote` or
in-process.

**1. The tick - `POST /api/admin/tick`** (CLI twin: `admin tick`).
Runs one scheduler pass synchronously: the auto-close / vendor-expiry /
prompt-tap-orphan-reap / prompt-tap-TTL sweeps, then (behind `sync_scheduler_enabled`)
the vendor network sweeps + any due connector schedules.

- Gates: **`admin` scope + `admin` role**. Deliberately **not** behind
  `remote_admin_enabled` - a tick is the routine passage of time, not an irreversible
  admin verb.
- Returns `{ok, ran, jobs_enqueued}` - the **count** of enqueued connector-sync jobs
  only, never the job ids (they are install-global and would leak cross-org activity).
- A tick already in progress returns **409** `{ok:false, ran:false, busy:true}` - it
  never double-runs the egress sweeps.
- Cron it on whatever cadence the daemon would have used (e.g. every 30s).

**2. The retention purge - `POST /api/admin/retention/purge`** (CLI twin:
`admin retention purge-now`).
The destructive half. It keeps its **full quad gate**: `admin` scope + `admin` role
+ `remote_admin_enabled` + a `confirm:"purge"` echo in the body. It refuses (409)
when retention is disabled or the window is 0, and refuses (400) a wrong/absent
confirm - deleting nothing either way. Run it on your **retention cadence**, not
every tick.

```bash
# tick (frequent)
curl -fsS -X POST -H "X-Loc-Token: $LOC_TMS_ADMIN_TOKEN" \
     http://host:8000/api/admin/tick

# retention purge (on your retention cadence; requires remote_admin_enabled + confirm)
curl -fsS -X POST -H "X-Loc-Token: $LOC_TMS_ADMIN_TOKEN" \
     -H "Content-Type: application/json" -d '{"confirm":"purge"}' \
     http://host:8000/api/admin/retention/purge
```

---

## 5. Postgres backend (embedded profile)

Postgres is supported **for the embedded/enterprise profile** (locked decision #2 in
the composable-core proposal). Select it by setting `DATABASE_URL` to a
`postgresql+psycopg://...` DSN; leave it unset for the zero-config SQLite default.

On a Postgres URL, **Alembic owns the schema**: `docker-entrypoint.sh` runs
`alembic upgrade head` **before the app boots** (the app's `init_db()` create-tables
path is the SQLite story only). The same happens on the CI Postgres smoke job
(`.github/workflows/ci.yml` → `postgres-smoke`), which builds the schema with
`alembic upgrade head` and runs `tests/pg_smoke.py` against a real Postgres 16.

```yaml
# docker-compose.yml (excerpt) - embedded/headless API service on Postgres + a ticker.
# The repo's docker-compose.yml already ships a `postgres` profile; this shows the
# embedded wiring on top of it.
services:
  loc-studio:
    image: loc-studio:latest
    environment:
      LOC_TMS_EMBEDDED: "1"                 # API-only assembly (implies LOC_TMS_SERVER)
      LOC_TMS_SERVER: "1"                   # explicit real-install signal (belt-and-suspenders)
      LOC_TMS_DATA_DIR: /data               # uploads + keyfiles volume (DB lives in Postgres)
      DATABASE_URL: postgresql+psycopg://locstudio:locstudio@db:5432/locstudio
      # Embedded mode honors ONLY the prefixed spelling (see the spelling note above).
      LOC_TMS_SESSION_SECRET: ${LOC_TMS_SESSION_SECRET}   # pin from your secret manager
      LOC_TMS_ENCRYPTION_KEY: ${LOC_TMS_ENCRYPTION_KEY}
    volumes:
      - loc-data:/data
    depends_on:
      db:
        condition: service_healthy
    healthcheck:
      test: ["CMD", "curl", "-fsS", "http://localhost:8000/ready"]
      interval: 30s

  db:
    image: postgres:16@sha256:1a6ab3f5345eb6dbe04a1349529caabdb0ab09293a09590fad07b2246bfa4b54
    environment:
      POSTGRES_USER: locstudio
      POSTGRES_PASSWORD: locstudio          # change me; never publish 5432 to the host
      POSTGRES_DB: locstudio
    volumes:
      - loc-pgdata:/var/lib/postgresql/data
    healthcheck:
      test: ["CMD-SHELL", "pg_isready -U locstudio -d locstudio"]
      interval: 10s
      timeout: 5s
      retries: 5

  ticker:
    # Any cron/sidecar that POSTs the tick on an interval with an admin lsu_ token
    # (mint it once via `init --issue-key`, section 3). NOTE the DOUBLED $$ below:
    # Compose interpolates single-$ on the HOST, so $$ escapes it to a literal $ that
    # the CONTAINER shell expands from its own env - otherwise the token empties.
    image: curlimages/curl:latest
    entrypoint: ["sh", "-c", "while true; do
        curl -fsS -X POST -H \"X-Loc-Token: $$LOC_TMS_ADMIN_TOKEN\"
             http://loc-studio:8000/api/admin/tick || true;
        sleep 60; done"]
    environment:
      LOC_TMS_ADMIN_TOKEN: ${LOC_TMS_ADMIN_TOKEN}   # an admin-scoped per-user key

volumes:
  loc-data:
  loc-pgdata:
```

**What is and isn't tested for Postgres.** The CI `postgres-smoke` job enforces that
the Alembic schema **builds and round-trips on a real Postgres** (`pg_smoke.py`) - it
does *not* boot the embedded profile. The embedded **assembly** (router split,
skipped daemons, wall on, the tick route) is enforced by
`tests/verify_embedded_profile.py`, which runs on SQLite. The two halves are
independent by construction: the assembly keys off `IS_EMBEDDED` and never off the DB
backend, and the backend selection keys off `DATABASE_URL` and never off the mode.
`verify_embedded_profile.py` pins the seam between them with a config-only child
(`--pg-config-child`): it imports `app.config` with `LOC_TMS_EMBEDDED=1` **and** a
Postgres `DATABASE_URL` and asserts the backend switch (`IS_SQLITE` false, `DB_URL`
honored, still embedded) **without opening a connection** - so "embedded + Postgres"
is checked locally with no live database, and the live schema is CI's job.

---

## 6. Trust-host-auth (tokenless `/api`, embedded only)

An embedded install that sits **behind a host application whose own boundary already
authenticates every caller** can let `/api` calls arrive **without** a per-request
`X-Loc-Token`. It is off by default and engages only under a **double condition** -
both must hold:

1. `LOC_TMS_EMBEDDED=1` (boot env), and
2. the Setting `embedded_trust_host_auth == "1"` - set it on the host with
   `config set embedded_trust_host_auth 1`. It is on `REMOTE_IMMUTABLE_KEYS`, so it
   can **never** be flipped over the very API it guards.

When both hold, a tokenless request resolves to a configured **trust principal** - a
real, **dedicated** user auto-provisioned at boot - whose reach is set by two
independent levers, the API `LOC_TMS_TRUST_SCOPES` and the RBAC `LOC_TMS_TRUST_ROLE`
(both gate `/api`). A **default** principal (`viewer` + `api docs keys`) can read the
docs/keys surfaces but is **403 on every role-gated write**; the admin surface needs
an explicit `LOC_TMS_TRUST_ROLE=admin` **plus** `admin` in the scopes, and even then
the irreversible verbs keep their host-only `remote_admin_enabled` lock.

> **Security posture.** This mode is **only** for deployments where the host's
> boundary already authenticates every caller. It makes tokenless callers trusted, so
> enabling it anywhere `/api` is directly reachable removes authentication from it.

A request that **does** carry a token is still verified exactly as always - a **bad
token is still 401** (trust never launders a wrong token) - and a non-embedded boot,
or the switch off, is byte-identical (the tokenless 401 is unchanged, so the switch is
non-enumerable). The full env table is in section 2; the guarantees are pinned by
`tests/verify_trust_host_auth.py`. See the README "Trust-host-auth" subsection and
the `/help/llm` capability card for the same contract in short form.

#### Hardening: source restriction (b) + attribution (a)

Two **optional**, strictly-narrowing hardening levers sit on top of the door above.
Both are meaningful **only** under the same double condition, and both are dormant
unless you set the env below.

**(b) Source restriction - `LOC_TMS_TRUST_HEADER_SECRET`.** By default, trust mode
trusts *any* network peer that reaches the port. Set this secret (a long random value)
and the host proxy must inject it as the `X-Loc-Trust-Secret` header on every request;
a tokenless request without the matching header gets **401** instead of the principal.
It is checked with a constant-time, fixed-length digest compare (a non-ASCII or
oversized header is a clean 401, never a 500), **before** the principal is resolved, so
it can only ever turn a would-be-trusted request into a 401 - never the reverse. If you
leave it unset while trust is armed, a boot **warning** reminds you the port is open to
every peer that can reach it.

The secret itself **must be ASCII printable** (use a long, random ASCII value). HTTP header values
are transported as opaque bytes and the app decodes them latin-1, so a non-ASCII secret -
or an ASCII value carrying an interior control character (`\n`, `\x0b`, DEL), which no HTTP
header can transport and `.strip()` does not remove - could never match the wire header;
rather than silently 401 every request, the boot **refuses** such a
`LOC_TMS_TRUST_HEADER_SECRET` loudly. A value shorter than 32
characters is accepted but logs a weak-secret boot warning.

**(a) Attribution - the fixed `X-Loc-Trust-User` header.** When (and only when) (b) is
armed, the host proxy may forward the end-user it authenticated as `X-Loc-Trust-User:
<email>`. That value is recorded as an **attribution-only** `on-behalf-of=<email>`
marker in the audit record's **`detail`** field for every mutation - it carries **no
authority**: authorization, ownership, org-scoping, job isolation, inbox placement, and
budget routing all remain the fixed trust principal's (`ident["actor"]` never changes).
The marker lives in `detail` (not the free-form `action`) deliberately: the request path
echoed into `action` is attacker-controlled, so keeping the authoritative attribution in
`detail` means a forged `on-behalf-of=` in a URL path can never be mistaken for a real
one, and a subject-access export masks third-party emails out of `detail`. The
value is validated exactly like a real identity (CRLF/control/over-long/reserved system
names are silently ignored, never written). It is **hard-coupled to (b)**: with no
secret set, `X-Loc-Trust-User` is inert - so no existing install starts annotating just
because it upgraded. There is **no env var** for the header name; `X-Loc-Trust-User` is
fixed.

The **authoritative** attribution is the `detail` field. An `on-behalf-of=` that appears
inside a row's `action` came from the request path (which is attacker-controlled and echoed
verbatim into `action`) and is **forged** - never read `action` as attribution.

Forwarding an end-user identity places it **permanently** in the tamper-evident audit
trail: those rows are retained by design (the audit chain is append-only, GDPR
Art. 17(3)(b)), so a forwarded end-user has **no Art. 15 access or erasure path** to that
record - forward only identities your retention and privacy policy permits to persist.

**Reverse-proxy contract (MUST).** The app cannot strip client-supplied headers, so the
proxy owns half the guarantee. The proxy **MUST overwrite (not append)** both
`X-Loc-Trust-Secret` and `X-Loc-Trust-User`, and **MUST strip any inbound
`X-Loc-Trust-*`** the client sent, so a direct client copy can never survive:

```nginx
# nginx - inside the location that proxies to the embedded /api.
# `$authenticated_email` comes from your auth module (e.g. auth_request); replace the
# secret placeholder below with your own value. Neither name is an nginx built-in, so
# supply them yourself - e.g. hard-code the secret literal, or lift it from the
# environment at startup with the ngx_http_perl / njs / env directives, or a `map`.
proxy_set_header X-Loc-Trust-Secret "REPLACE_WITH_YOUR_SECRET";  # overwrite, never append
proxy_set_header X-Loc-Trust-User   "$authenticated_email";      # from the proxy's own auth
# (proxy_set_header REPLACES the header, so any client-sent copy does not survive - there
#  is no separate strip step to get wrong)
```

```caddy
# Caddy - header_up <name> <value> is a Set: it already REPLACES any client-sent copy,
# so no separate strip is needed (and a wildcard `header_up -X-Loc-Trust-*` would be
# WRONG here - Caddy applies header ops in a fixed order, Add/Set/Delete, so the Delete
# runs AFTER these Sets and would strip the very headers you just injected → every
# request 401s). If you want a blanket strip of inbound copies, do it in a preceding
# handle/route with an explicit `request_header -X-Loc-Trust-*`, never inline here.
reverse_proxy embedded:8000 {
    header_up X-Loc-Trust-Secret {env.LOC_TRUST_SECRET}
    header_up X-Loc-Trust-User   {http.auth.user.email}
}
```

**The secret** - TWO envs (both read **at boot only**, like every `LOC_TMS_TRUST_*`):

- `LOC_TMS_TRUST_HEADER_SECRET` (singular) is a **literal single value** - commas are part
  of the secret and are **never** split. Use this for a normal single-secret deployment
  (even if your secret happens to contain a comma).
- `LOC_TMS_TRUST_HEADER_SECRETS` (plural) is a **comma-separated accept-list** of
  concurrently-valid secrets, for zero-cutover **rotation**. If it is set (non-blank) it
  **takes precedence** over the singular value.

**Rotation without a hard cutover** (using the plural env):

1. Set `LOC_TMS_TRUST_HEADER_SECRETS=<old>,<new>` and restart the instances (a rolling
   restart is fine - every instance honors **both** values during the overlap, so no
   instance 401s the proxy whichever secret it is currently injecting).
2. Migrate the reverse proxy to inject `<new>` (`header_up` / `proxy_set_header`).
3. Set `LOC_TMS_TRUST_HEADER_SECRETS=<new>` (drop `<old>`) and restart to retire the old
   secret - or **unset the plural** `LOC_TMS_TRUST_HEADER_SECRETS` and move back to the
   singular `LOC_TMS_TRUST_HEADER_SECRET=<new>` (leaving the plural set would keep it in
   precedence over the singular).

The overlap lives entirely on the app's **accept** side. The proxy still injects exactly
**one** `X-Loc-Trust-Secret` header - two header **copies** are still refused, so do **not**
try to "send both" from the proxy (that 401s every request). Each accept-list element must
be ASCII-printable and contain **no comma** (the comma is the list delimiter). A plural env
that is set but resolves to **no** value (`","`, `",,,"`, `" , "`) **fails the boot closed**
rather than silently disarming the gate.

---

## 7. MCP sidecar wiring

MCP is **out-of-process by design**: `integrations/mcp-server/server.py` is an HTTP
client of `/api` - the app never imports it - so an embedded install needs no MCP work
in the app. To expose the embedded install to MCP clients, run the sidecar with the
HTTP transport pointed at the embedded `/api`:

```bash
python integrations/mcp-server/server.py \
    --transport streamable-http --host 127.0.0.1 --port 8765
# with LOC_STUDIO_BASE_URL pointing at the embedded install's /api base.
```

**Security posture (existing MCP law - cite it, don't reinvent it).** The
streamable-HTTP listener on FastMCP has **no built-in auth**, so it must sit **behind
your own authenticating, TLS-terminating reverse proxy**. In multi-user HTTP mode each
request carries its **own** `X-Loc-Token` bearer, and you must **leave
`LOC_STUDIO_TOKEN` UNSET** on the listener: with it set, every request would collapse
onto that one identity and all of its scopes. With it unset, a missing header is a
clean per-request setup error, and each caller presents their own scoped `lsu_` key.
The stdio transport (Claude Desktop) is the single-user path and *does* use
`LOC_STUDIO_TOKEN`. Full details: `integrations/mcp-server/README.md`.

---

## 8. Supported configurations

| Configuration | Support |
|---|---|
| **SQLite** | Supported for **standard / local installs** - the zero-config default. |
| **Postgres** (`DATABASE_URL=postgresql+psycopg://…`) | Supported for the **embedded / enterprise profile** - CI-smoked, Alembic-owned schema, entrypoint runs `alembic upgrade head`. |
| Any other database backend | **Unsupported.** The entrypoint skips Alembic for a non-Postgres `DATABASE_URL`, and no schema/behavior is verified for it. |

SQLite remains the sole supported backend for the standard local install; Postgres is
documented and tested specifically for the embedded profile. Anything else is out of
scope.

---

## 9. See also

A runnable independent host and upgrade check live in [examples/embedded-host](../examples/embedded-host/README.md).

- [DEPLOY.md](DEPLOY.md) - the general container/production guide (SQLite, Postgres
  profile, TLS reverse proxy, the data volume).
- [SECRETS.md](SECRETS.md) - the two runtime secrets, KMS injection, and the
  unrecoverable-loss traps.
- [CLI.md](CLI.md) - the headless CLI surface (local + `--remote`).
- `integrations/mcp-server/README.md` - the MCP sidecar, stdio and streamable-HTTP.
- `app/capabilities/llm.md` (`/help/llm`) - the machine-facing capability card,
  including its own "Embedded / headless mode" section.
- The composable-core roadmap: this profile is phase 0 of it.
</content>
