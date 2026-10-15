# Minimal independent host

Copy this directory outside the product checkout. Use the product's locked Python
environment and put its release directory on `PYTHONPATH` (the release must include
`app`, Alembic migrations, locks and docs). No relative checkout imports are used.
This example uses a dedicated SQLite volume and a single authenticated host user.

From the copied directory, set `LOC_TMS_ADMIN_PASSWORD` to a secret of at least
12 characters. Set these environment variables before any product import:

```text
LOC_TMS_EMBEDDED=1
LOC_TMS_SERVER=1
LOC_TMS_DATA_DIR=<absolute dedicated product data directory>
DATABASE_URL=
LOC_TMS_ADMIN_EMAIL=<first admin email>
```

Pin `LOC_TMS_SESSION_SECRET` and `LOC_TMS_ENCRYPTION_KEY` from your secret manager,
or retain and back up their generated keyfiles in the data directory. Bootstrap:

```text
python -m app.cli --json init --from-env --issue-key --key-scopes "docs keys admin"
python -m uvicorn host:app --no-proxy-headers --host 127.0.0.1 --port 8000
```

Store the one-time `api_key` securely. Subsequent init calls are no-ops and do not
recover or reissue it. Send it as `X-Loc-Token` to `/api/projects` and the host cron
routes. The product web UI is absent. Place a TLS boundary in front for remote use.

## Lifecycle contract

The host forwards ASGI startup before serving requests and shutdown after draining
them. Product startup initializes SQLite, applies settings and recovers interrupted
work; do not bypass it by merely mounting routes. Run one host process per SQLite
install. External cron POSTs `/host/tick` about every 30 seconds and
`/host/retention/purge` on the chosen retention cadence. Both forward to the product
cron pair with the admin key. Retention additionally requires `remote_admin_enabled`,
enabled nonzero retention and body `{"confirm":"purge"}`; refusals delete nothing.

## Storage contract

The host owns the dedicated volume or Postgres database, credentials, backup schedule
and recovery procedure. The product owns its schema, migrations, uploads, settings,
users, keyfiles and audit records within that storage. Do not share product tables
with host tables or edit them directly. For Postgres use the locked postgres profile,
set `DATABASE_URL`, and run `python -m app.cli db migrate` before boot. Back up with
`python -m app.cli db backup-full <archive>` and verify with `db verify-backup <archive>`.

## Auth contract

Ordinary `/api` and cron requests use product API-key authentication and product
role/scope checks. Optional `/host/projects` authenticates `Authorization: Bearer
<HOST_AUTH_TOKEN>` against the host secret and attributes the fixed `HOST_USER`.
Replace that tiny single-user check with your host's session authentication for
multiple users. The host strips incoming trust headers and injects its own values.
Set a printable ASCII `LOC_TMS_TRUST_HEADER_SECRET` of at least 32 characters,
`LOC_TMS_TRUST_PRINCIPAL=<dedicated email>`, `LOC_TMS_TRUST_ROLE=viewer`,
`LOC_TMS_TRUST_SCOPES=api docs keys`, `HOST_AUTH_TOKEN` and `HOST_USER` (ASCII email).
Leave the plural `LOC_TMS_TRUST_HEADER_SECRETS` unset for this singular-secret example.
Then run locally and restart:

```text
python -m app.cli config set embedded_trust_host_auth 1
```

Trust requires BOTH embedded mode and that host-only setting. A header alone grants
nothing. The dedicated principal is the audit actor; the authenticated host user
reaches the audit detail as `on-behalf-of`, without gaining authority or a User row.
The underlying product app must not be separately exposed by the host.

## Disable versus remove

Disable the whole integration by stopping its cron jobs, draining requests and
unmounting this ASGI host. Storage, users, API-key hashes, settings, encrypted files
and audit history remain for re-enable. There is no product command to unmount a
host integration. `python -m app.cli config set embedded_trust_host_auth 0` disables
only trust authentication; API keys still work. Feature-specific `config set` switches
likewise disable features without deleting their data.

Remove means disable first, revoke issued keys with `python -m app.cli apikey
revoke <key-id>`, then have the host operator delete the dedicated product database,
data volume, secret-manager entries and product installation. Decide separately when
to delete backups under your retention policy. There is no whole-product uninstall
or whole-install purge CLI command. `admin retention purge-now` and `ledger purge`
only delete their scoped records and are not removal commands. Host data outside
the dedicated product storage remains the host's responsibility.

## Upgrade compatibility check

Before upgrading, back up and run this command with the candidate release on
`PYTHONPATH`, its locked environment, and a restored copy of the installed storage:

```text
python upgrade_check.py --profile base
```

It exits 0 only when installed schema equals the candidate's single Alembic head,
the selected release-lock profile matches installed dependency versions and backend,
and the candidate embedded env matrix is readable. It prints the candidate matrix
keys without values: compare that list and their documented meanings with the host's
deployment configuration before approving the upgrade. A schema mismatch exits 1;
run `python -m app.cli db migrate` on the copy and repeat the check and host smoke
tests before applying the same migration to the stopped install. Use `--profile
postgres` for Postgres. A green check establishes these prerequisites, not arbitrary
API or semantic compatibility across versions.
