# Local and self-hosted install security

Existing local databases keep their users, credentials and password-change flags.
Upgrade does not reset or re-seed an existing admin. On a fresh local install,
the seeded accounts must change their passwords at first sign-in before using the app.
Server installs still use the first-admin setup wizard or the local `init` command.

`run.py` defaults to loopback. Local loopback installs accept only `localhost`,
`127.0.0.1`, `[::1]`, the configured `app_base_url` host, and entries in
`security_allowed_hosts`. Other Host headers receive HTTP 400, even on health probes.
This also covers API, MCP and WebSocket entry points through the shared ingress boundary.
Server installs on a cloud host retain their explicit host configuration.

For Tailscale access, add the exact MagicDNS name or tailnet IP from the host:
`config set security_allowed_hosts "my-machine.my-tailnet.ts.net,100.64.0.10"`
using the local app CLI and its confirmation prompt. Include the base URL host if set.
Alternatively set `LOC_TMS_SECURITY_ALLOWED_HOSTS` and restart `run.py`.
Security help describes these settings in the web UI; API and MCP cannot change
host-managed trust settings. Emptying the list retains the local loopback protection.

`LOC_TMS_DATA_DIR` selects storage only. The verify wrapper explicitly supplies
`LOC_TMS_TEST_MODE` with value `1` for its throwaway installs. Test mode refuses a
non-loopback `HOST`; there is no unsafe public-bind exception. On a non-loopback
bind, the shared authentication gate stays enabled even if the stored wall switch is off.
Use `LOC_TMS_SERVER` with value `1` for a remote install and configure its allowed hosts.

New data directories and missing parents are created with POSIX mode 0700. New SQLite
database files and keyfiles are created with mode 0600 before content is written.
The same creation rule covers migrations, vault restore and SQLCipher connections.
Existing files and directories retain their permissions. Windows uses NTFS ACLs;
install into a directory accessible only to the account running the app and administrators.
This upgrade does not rewrite existing ACLs or keys.

## Compose

Set a strong `POSTGRES_PASSWORD` in your private `.env` before running Compose.
Compose refuses an empty or missing value during interpolation, including SQLite-only
launches. For Postgres, set `DATABASE_URL` with that same value and URL-encode it as needed.
There is no bundled database credential.

For Caddy TLS, edit `deploy/Caddyfile` and start with both files:

```sh
docker compose -f docker-compose.yml -f deploy/compose-proxy.yml --profile proxy up -d
```

Add `--profile postgres` to use Postgres. The companion file enables Secure session
cookies and trusts only Caddy at `172.30.218.2`. Caddy refuses to start without the
companion. Dynamic addresses use a separate range so the app cannot take Caddy's IP.
The plain default app remains usable over localhost HTTP when the proxy
profile is not used. With the companion enabled, use the HTTPS URL to sign in.
If the subnet conflicts with your network, change the Compose subnet, Caddy address,
and trusted proxy IP together. Do not trust the entire network or arbitrary forwarded headers.
