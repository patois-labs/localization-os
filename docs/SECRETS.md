# Localization OS TMS - Secrets & Key Management

How Localization OS sources its two runtime secrets, how to inject them from a secret
manager or cloud KMS, and the **unrecoverable** data-loss traps you must avoid.
It cross-links with [DEPLOY.md](DEPLOY.md) (which containers
run) and [BACKUP.md](BACKUP.md) (backing the keys up with the data).

New keyfiles and SQLite files use private creation modes on POSIX; Windows uses NTFS ACLs.
See [local install security](LOCAL-INSTALL-SECURITY.md) for creation and upgrade details.

---

## 1. The two secrets

| Secret | Env var (either spelling) | Keyfile (fallback) | What it protects |
|--------|---------------------------|--------------------|------------------|
| **Session secret** | `SESSION_SECRET` / `LOC_TMS_SESSION_SECRET` | `<DATA_DIR>/.session_secret` | Signs session cookies. On first audit-chain use it also seeds `.audit_key` **once**; after that, changing it never changes audit signing (see the keyfile inventory below). |
| **Encryption key** | `ENCRYPTION_KEY` / `LOC_TMS_ENCRYPTION_KEY` | `<DATA_DIR>/.enc_key` | Fernet key for encrypted uploads at rest, stored connector credentials (Airtable, Jira and the other connector tokens), and the **LDAP service-bind password** (Setting `ldap_bind_password_enc`). |

**All keyfiles in `DATA_DIR`** (the private-file inventory; `db backup --full` carries every
one that exists, DB-only backups carry none of them):

| Keyfile | Created by | What it holds | Rotation |
|---------|------------|---------------|----------|
| `.session_secret` | first boot when no env var supplies it (§1 precedence) | The session secret; signs cookies. Rotating it logs everyone out and does **not** change audit MACs (see `.audit_key`). | §3, rotatable in place |
| `.enc_key` | first boot when no env var supplies it | The Fernet encryption key (uploads, stored credentials, LDAP bind password, SQLCipher DB key). | §3, **not** rotatable in place |
| `.audit_key` | first use of the audit chain (`app/services/audit_state.py::key`) | The audit-chain HMAC signing key, derived from the session secret **once** and persisted; later session-secret rotations never change it, so existing rows keep verifying. | Never rewritten; superseded by `audit rotate-key` (§3) |
| `.audit_epochs` | `audit rotate-key` (`app/services/audit_state.py::save_epoch`) | Each signing key created by `audit rotate-key` plus its `after` row id: the key signs every row after that id until the next epoch takes over. The last entry is the current signing key; `.audit_key` keeps the original key for rows before the first rotation. | Grows on each `audit rotate-key` |
| `.audit_head` | every audit-chain write (`app/services/audit_state.py::advance`) | The MAC'd head witness pinning the newest row id + hash; catches tail truncation even if both DB anchors are deleted. | Rewritten on every audit write |

> **Both spellings are honored** (`app/config.py`, `_secret()`), with a precise,
> data-safe resolution order per secret:
>
> 1. the **prefixed** `LOC_TMS_*` env var - if set, it wins (`source="env"`);
> 2. else an **existing keyfile** in `DATA_DIR` - if present, it wins
>    (`source="keyfile"`). **An existing keyfile out-ranks the unprefixed env
>    var.** This is deliberate: it guarantees an already-running install is never
>    silently switched onto a different key (which would make its encrypted data
>    unreadable). To actually change a persisted secret, follow the rotation
>    procedure in §3 - setting the env var alone will not override the keyfile;
>    the app logs a WARNING when a keyfile out-ranks a *differing* unprefixed var;
> 3. else the **unprefixed** `SESSION_SECRET` / `ENCRYPTION_KEY` env var - if set,
>    it is used exactly like the prefixed spelling (`source="env"`, **no keyfile is
>    written**);
> 4. else the app generates + persists the keyfile inside `DATA_DIR`
>    (`source="keyfile"`).
>
> So on a **fresh** install either spelling works and behaves identically; the
> prefixed spelling wins only when both are set. This is the KMS-ready path - a
> secret manager / KMS just injects the env var at runtime; no code change needed.
>
> **Embedded mode is different (`LOC_TMS_EMBEDDED=1`): ONLY the prefixed
> `LOC_TMS_*` spelling is honored.** `SESSION_SECRET` / `ENCRYPTION_KEY` are
> generic names a *host* application very plausibly exports, and an embedded Loc
> Studio must not silently adopt the host's secret (the host rotating it would
> kill Localization OS's data). In embedded mode a bare unprefixed var is ignored; when
> that ignored var causes the install to fall back to a **freshly generated**
> keyfile (the volumeless regenerate trap), the app warns so the miss is visible
> instead of silent. An embedded install already running from a **persisted**
> keyfile ignores the unprefixed var **silently** - it is correctly keyed, so there
> is nothing to fix. See [embedded.md](embedded.md).

Generate strong values:

```bash
# session secret - any high-entropy string; hex shown here
python -c "import secrets; print(secrets.token_hex(32))"
# encryption key - MUST be a valid Fernet key (url-safe base64, 32 bytes)
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

---

## 2. Sourcing secrets from env / a secret manager / cloud KMS

The pattern is always the same: **the manager/KMS injects the value as an
environment variable** before the app process starts. Localization OS reads
`SESSION_SECRET` / `ENCRYPTION_KEY` (or the `LOC_TMS_*` spellings - see §1 for the
exact precedence and the embedded-mode exception) from the environment and uses the
env value directly: the prefixed spelling never reads or writes a keyfile at all,
and the unprefixed spelling is used without writing one on a fresh install (see §1
for the exact precedence).

**Plain env / `.env` file (Docker Compose):**
```dotenv
SESSION_SECRET=<token_hex value>
ENCRYPTION_KEY=<Fernet key>
```
Compose passes these into the `app` service (see DEPLOY.md §4).

**Docker / Kubernetes secrets:** mount the secret and export it into the env, e.g.
a k8s `Secret` referenced via `env.valueFrom.secretKeyRef` for `SESSION_SECRET`
and `ENCRYPTION_KEY`.

**Cloud secret managers / KMS** (AWS Secrets Manager / SSM, GCP Secret Manager,
Azure Key Vault, HashiCorp Vault): fetch the secret at container start and export
it, e.g.

```bash
# entrypoint snippet - resolve from the manager, then exec the app
export SESSION_SECRET="$(aws secretsmanager get-secret-value \
  --secret-id loc-tms/session-secret --query SecretString --output text)"
export ENCRYPTION_KEY="$(aws secretsmanager get-secret-value \
  --secret-id loc-tms/encryption-key --query SecretString --output text)"
exec uvicorn app.main:app --no-proxy-headers --host 0.0.0.0 --port 8000
```

The same shape works for `gcloud secrets versions access`, `az keyvault secret
show`, or a Vault agent template.

### The keyfile trap (why the app warns)

If you set **neither** env var, the app auto-generates the keyfiles in
`DATA_DIR` on first boot and reuses them. That "just works" for a single box with
a persistent data volume, **but** in a container **without** a persistent
`DATA_DIR` volume the keyfiles regenerate on **every boot** - which silently:

- logs everyone out (session cookies no longer verify), and
- makes every encrypted upload + stored connector token **permanently
  unreadable** (a new Fernet key can't decrypt data written under the old one).

To catch this, on startup the app records where each secret came from
(`config.SESSION_SECRET_SOURCE` / `ENCRYPTION_KEY_SOURCE` = `"env"` or
`"keyfile"`) and, **on a non-SQLite (Postgres, i.e. real) deploy**, logs a
`WARNING` if either secret came from a keyfile rather than env. It is only a
warning, never a hard fail - the keyfile fallback still works and local
dev/tests (SQLite) are exempt so nothing spurious fires there.

---

## 3. Loss & rotation caveats

> **LOSING `ENCRYPTION_KEY` IS UNRECOVERABLE.** All Fernet-encrypted uploads and
> every stored connector token become **permanently undecryptable**. The app then
> behaves as if connections have blank credentials (`dec_secret()` returns `{}`)
> and encrypted uploads/the DB vault can't be opened. Back this key up somewhere
> safe and **separate from the data volume** (a secret manager is ideal). There is
> no recovery path - not a support ticket, not a re-derivation.

> **The LDAP service-bind password** (Setting `ldap_bind_password_enc`) is
> Fernet-encrypted with this same key (`settings.set_secret`/`get_secret`). It is
> **not primary data**: if the key is lost or rotated, the app treats the bind
> password as empty (`get_secret` returns `""` on a decrypt failure) and an admin
> simply re-enters it on the Security page - an LDAP login degrades to local auth
> in the meantime, it never 500s. So the bind password does not raise the stakes
> of an `ENCRYPTION_KEY` loss the way uploads/connector tokens do.

**Rotation:**

- **`SESSION_SECRET` is rotatable.** Changing it invalidates existing sessions;
  everyone must log in again. Audit verification keeps working: `.audit_key`
  preserves the previous derivation independently of the live session key.
  Before the first rotation on an upgraded installation, run `audit verify`
  with the old session key still available to create the audit keyfile.
- **Audit signing keys have retained epochs.** With other writers stopped, run
  `audit rotate-key --yes`. A forced `audit.key_rotated` row is signed by the old
  key and carries a countersignature by the new random key. `.audit_epochs`
  records each new key with its exact row boundary, and `.audit_key` keeps the
  original key, so old history still verifies.
  If a session key was exposed before migration, rotate the audit signing key too.
  Retaining an exposed key cannot restore trust in history signed before exposure.
  Keep `.audit_key`, `.audit_epochs`, `.audit_head`, and `.audit_format` protected alongside the
  session keyfile. `db backup --full` carries all four and restores them together
  with the DB, including onto a host that has already run. DB-only backups do not
  carry them. Even env/KMS installs keep audit signing material on disk; protect it
  from filesystem readers as well as protecting the secret manager.
  An unreadable/corrupt witness logs one loud error per process and reports BROKEN
  through verification and monitoring; audited requests continue. Missing signing
  material counts dropped rows and logs rate-limited errors; fail-closed answers 503.
  Restore matching `.audit_key` and `.audit_epochs` first if either is lost or damaged.
  After investigating a bad witness or a legitimate DB-only rollback, stop the server
  and all other writers, run
  `audit reset-anchor --yes --i-understand-this-erases-truncation-evidence`.
  Then start the server. Reset refuses a detected server listener.
  It replaces a bad witness loudly and records `witness_replaced=1` on the forced
  `audit.anchor_reset` row. It cannot repair row tampering or recover lost keys.
  For a DB-only restore predating the latest key rotation, use a full backup with
  matching trust files instead; an empty audit log cannot be re-baselined.
- **`ENCRYPTION_KEY` - NOT rotatable in place.** Because existing ciphertext was
  produced with the current key, you cannot simply swap in a new key. Rotating it
  requires **decrypting all existing data with the old key and re-encrypting with
  the new one** first (uploads + `Connection.secret_enc` + any DB vault). There is
  no built-in re-encrypt tool yet; treat the encryption key as long-lived and
  guard it accordingly.

---

## 4. Database encryption at rest (SQLite)

SQLite is the zero-config default and is **plaintext on disk** unless you opt into
SQLCipher encryption. The legacy shutdown vault is retired (#395). Postgres is
unaffected (encryption is the DB server's / volume's job there).

| Mode | How it is turned on | On disk while running | On a crash | Dependency |
|------|---------------------|-----------------------|------------|------------|
| **(default) none** | - | plaintext | plaintext | - |
| **Always-on SQLCipher** | `LOC_TMS_DB_ENCRYPTION=sqlcipher` env var (install-time, like `DATABASE_URL`) | **encrypted** | **still encrypted** | optional `sqlcipher3` wheel |

### Always-on SQLCipher mode (recommended for at-rest requirements)

Upgrade note (#395): existing legacy installs with `encrypt_db=1` and no SQLCipher will refuse startup after upgrading; use local `config set encrypt_db 0 --yes` to resume plaintext operation, or convert to SQLCipher as described below.

Legacy `encrypt_db=1` is refused on plaintext SQLite at startup and shutdown,
and new settings writes cannot enable it without SQLCipher. No new Fernet vaults
are written: the old path could omit committed WAL pages and fail to remove
plaintext. Existing `.vault` files can still be restored when the database is
absent, using the original encryption key. Recovery does not restore WAL pages
that an old vault never contained; retain any surviving database and sidecars.
To continue using plaintext SQLite, explicitly disable the old flag with local
`config set encrypt_db 0 --yes`. To encrypt, follow the conversion procedure
below with the app stopped; setting the environment flag alone does not convert
an existing plaintext database.

Set the env var **before** the app starts (the engine is built once at import,
so this is an install-time choice, not a UI toggle):

```dotenv
LOC_TMS_DB_ENCRYPTION=sqlcipher
```

and install the optional binding into the app's environment:

```bash
pip install sqlcipher3==0.6.2   # ships a cp313 win_amd64 wheel; drop-in sqlite3 DBAPI
```

In this mode the SQLite file is a **SQLCipher database - encrypted at all times**
(a crash can never leave plaintext, unlike the shutdown vault). The 32-byte AES
key is **HKDF-SHA256-derived from `ENCRYPTION_KEY`** under a fixed domain-separated
label (`config.sqlcipher_hex_key()`); you do **not** manage a separate DB key. The
same `ENCRYPTION_KEY` loss caveat in §3 applies - **lose it and the DB is
unrecoverable**. When this mode is active the legacy shutdown vault is redundant
and becomes a no-op (it logs and does nothing), even if `encrypt_db=1` is also set.

**Converting an existing plaintext DB** (stop the app first, back up, then):

```bash
# plaintext data/loc_tms.db  ->  encrypted (writes a NEW file; source untouched)
python scripts/convert_db_encryption.py encrypt data/loc_tms.db data/loc_tms.enc.db
# ...move the encrypted file into place, set LOC_TMS_DB_ENCRYPTION=sqlcipher, start.

# reverse (turning encryption off):
python scripts/convert_db_encryption.py decrypt data/loc_tms.db data/loc_tms.plain.db
```

The converter derives the key from `ENCRYPTION_KEY` exactly like the app, uses
SQLCipher's supported `ATTACH` + `sqlcipher_export()` copy, never edits in place,
and refuses to overwrite an existing destination without `--force`.

**Alembic (Postgres path is unaffected):** on an encrypted SQLite DB, Alembic
keys its connection through the same helper (`alembic/env.py` →
`dbcrypt.engine_kwargs`), so `alembic upgrade head` works against the encrypted
file when the env var is set.

### If `sqlcipher3` will not install on your platform

The wheel is available for CPython on common platforms (verified on Windows
py3.13). If no wheel exists for yours and you cannot build SQLCipher, use
**OS-level at-rest encryption** of the data volume with `encrypt_db=0`:
BitLocker (Windows), LUKS/dm-crypt (Linux), or FileVault (macOS). That gives
always-on encryption at the filesystem layer independent of the app.

---

## 5. Backups

Whatever mode you choose, the secrets must be recoverable **together with** the
database and uploads or the data is useless. In keyfile mode the keys live in
`DATA_DIR`, so backing up that volume backs up the keys - see
[BACKUP.md](BACKUP.md) for the DATA_DIR-plus-DB rule. In env/KMS mode the keys
live in your secret manager; make sure that store is itself backed up and that
its recovery is independent of the app's data volume.

LDAP transport validates both certificate chains and hostnames. `ldaps://` uses TLS
on connect; explicit `ldap://` requires StartTLS before any bind credentials are
supplied. Bare hosts default to LDAPS. Failed TLS degrades to local authentication
and logs a rate-limited transport cause. `ldap_ca_cert` is a PEM CA bundle path;
blank uses system roots. This path is public configuration, not an encrypted secret.
Set it on the Security page, with `admin ldap config --ca-cert`, `config set`,
`POST /api/admin/ldap`, or MCP `manage_install` action `set_config`. Like every LDAP and
OIDC key it is security-sensitive: `config set` asks to confirm, and the `/api` and MCP
doors refuse it without `confirm`.

Referrals are never chased. ldap3 would otherwise follow a directory-supplied referral
on a fresh connection carrying the same bind credentials, and for an `ldap://` referral
that connection is built unencrypted with certificate validation off, which would undo
the guarantees above.

### Upgrading an existing LDAP install (read before you upgrade)

This transport hardening is deliberately breaking. On an install where LDAP already
works, directory logins can stop working until the configuration is corrected, and the
login page falls through to local authentication rather than showing an error, so the
first report is usually "my password stopped working". Check these three before
upgrading:

1. **The DC certificate must chain to a trusted root and match the host as spelled.**
   Most AD deployments issue DC certificates from an internal enterprise CA that is not
   in the app host's trust store. Set `ldap_ca_cert` to that CA bundle first.
2. **`ldap://` now requires StartTLS.** A DC that does not offer StartTLS on port 389
   will refuse every bind. Move it to `ldaps://` or enable StartTLS on the DC.
3. **A bare host now means `ldaps://` (636), not cleartext 389.** If the DC serves only
   389, set the scheme explicitly and satisfy point 2.

Previously stored host forms are now refused on save, enable, and connection setup:
wildcards (including `*`), **IP literals** (both IPv4 and IPv6, bracketed or not), empty
DNS labels, labels longer than 63 ASCII characters, labels containing anything other than
letters, digits, hyphen or underscore, control characters, percent escapes, trailing dots,
raw internationalised names, paths, query strings, fragments, userinfo, ports outside
1-65534, and scoped IPv6 addresses such as `[fe80::1%25eth0]`.

Replace these with a concrete DNS name that the directory certificate actually carries as
a subject-alternative name. **An IP literal will not work**, even against a certificate
issued for that IP: the TLS hostname check consults only `DNS` subject-alternative names
and never reads an `IP Address` SAN, so `ldaps://10.0.0.5` cannot validate. That form was
fine before this change only because certificates were not being validated at all, so an
install using one is exactly the case this upgrade breaks.

Existing values are not automatically rewritten; an invalid value causes LDAP
authentication to fall back to local authentication until it is corrected. DNS names
supply TLS SNI.

`loc-tms admin ldap test-login` reports the underlying transport reason, so run it first
after upgrading rather than reading the rate-limited application log.
