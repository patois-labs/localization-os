# Localization OS TMS - Backup & Restore

How to back up and restore Localization OS for both storage backends. It cross-links
with [DEPLOY.md](DEPLOY.md) (the data volume + compose) and
[SECRETS.md](SECRETS.md) (the encryption key you must keep with the data).

On the hosted reference setup (`deploy/aws-hosted`, a single cloud VM) a release script stops the
app and archives its bind-mounted data before every swap, and restores it automatically if the new
container fails. The recipes below are the generic ones for any install.

---

## 0a. One-command full backup (recommended)

`loc-tms db backup --full <archive.tar.gz>` writes a single archive covering the DB
**plus** `uploads/` **plus** the encryption keyfiles, with an embedded manifest, and
`loc-tms db verify-backup <archive.tar.gz>` verifies it (manifest + per-member sha256 +
`PRAGMA integrity_check` + keyfile presence). `loc-tms db restore <archive.tar.gz>`
restores the whole install on SQLite. On Postgres it restores uploads and keyfiles
and extracts the dump, but reports **PARTIAL**, exits **1**, and returns JSON
`status: "partial"`, `ok: false`, `db_restore_required: true`. Complete the emitted
`pg_restore` recipe manually with all writers stopped; do not start the server
until the database is restored. Schedule the backup, keep several generations, rehearse a restore at least quarterly, and keep the
encryption key apart from the archive (see [SECRETS.md](SECRETS.md)).
The manual, backend-specific recipes below remain valid and explain what the archive
contains.

Full archives include `.audit_key`, `.audit_epochs`, `.audit_head`, and `.audit_format` as private
0600 keyfile members. `verify-backup` checks their presence, size, and hashes.
Restore replaces the audit trust files with the DB, so same-host restores, a clean
host after key rotation, and a host that has already run keep a verifiable chain.
The CLI reports that audit trust state was restored. Keep all writers stopped for
restore and audit key rotation. Hot backup captures trust files before the DB
snapshot so the witness can trail, but cannot lead, the saved DB head. Do not rotate
keys during a backup. Postgres still requires the emitted manual DB restore step.

A plain `loc-tms db backup <path>` (no `--full`) is written owner-only (0600) on Linux and macOS for both SQLite and Postgres. Windows has no POSIX modes: the file takes the ACL of its folder, so store it in a folder only the service account can read.

A plain DB-file restore preserves the host's trust files and prints a recovery hint.
The source must be a standalone SQLite backup without WAL, SHM, or journal sidecars.
Legacy restore stages and integrity-checks a private copy (using the install's key
for SQLCipher), disposes the CLI's pooled handles, and sets aside the old database
and sidecars. It verifies the installed database's integrity and SHA-256 against
the staged backup before reporting success. A failed move or verification rolls
back the old files; if rollback cannot finish, the error names the retained recovery
directory. Stop all database users first. `--skip-verify` applies only to full archives.
With the server and all other writers stopped, investigate the expected rollback and run
`audit reset-anchor --yes --i-understand-this-erases-truncation-evidence`, then
`audit verify`, then start the server. This discards truncation evidence and audits the reset.
It needs the matching signing key and epoch files; copying only the DB to another
host cannot recover lost keys. Use a full archive for migration or for restoring
history predating a key rotation. Older full archives without `.audit_key` set aside the
target's three audit trust files with rollback protection. On the next start, the audit
key is re-derived from the restored session secret, as before this audit upgrade.
No reset is needed. For env-secret installs, restore the source session secret in the
environment before starting the server.

### Legacy DB-only restore after empty-table verification

`audit verify` on an empty table deliberately creates `.audit_format` with
`legacy_through=0`. A later DB-only restore or migration of pre-v2 audit rows will
fail verification. `audit reset-anchor` retains that boundary and cannot fix this
format mismatch. Prefer a full backup with matching audit trust files.

Manual recovery is supported only on a host whose restored chain you have
independently validated against trusted backup/provenance evidence:

1. Stop the server, scheduler, and all other writers. Preserve copies of the restored
   DB and existing audit trust files for investigation. Confirm that the restored
   history is pre-v2 and that the signing key and epoch files match it.
2. Delete only `DATA_DIR/.audit_format`. Do not delete the signing keys or witness.
3. Run `audit verify` before any new writes. This re-pins the restored head as the
   legacy boundary. A remaining legitimate rollback/witness failure needs the
   double-confirmed `audit reset-anchor` command described above, then another
   `audit verify` before restarting writers.

Never use this procedure to silence an unexplained tamper failure. Re-pinning
cannot prove provenance: legacy rows retain ambiguous field boundaries and
unauthenticated customer attribution. Before the first upgrade pin, a DB-only
attacker can also insert rows (including an unhashed prefix with high ids) that
become legacy. This pre-upgrade window is an accepted limit; independently validate
history with writers stopped before the initial verification.

## 0. The one rule that matters

**Back up the `DATA_DIR` volume TOGETHER with the database, as one consistent
set.** `DATA_DIR` (`/data` in the container) holds:

- `uploads/` - uploaded source documents (may be Fernet-encrypted at rest),
- `.session_secret` and `.enc_key` - the two secrets, **only when not injected via
  env** (see SECRETS.md),
- rulebooks / prompt files and other runtime artifacts,
- `.audit_key`, `.audit_epochs`, `.audit_head`, and `.audit_format`, including on env/KMS installs,
- `loc_tms.db` (+ `-wal` / `-shm`) - the database, **in SQLite mode only**.

If you back up the database but lose `DATA_DIR`, you lose the uploads and - in
keyfile mode - the **encryption key**, which makes every encrypted upload and
stored connector token **permanently unreadable** (SECRETS.md §3). If you use
env/KMS for the secrets, `DATA_DIR` still holds the uploads, so it still must be
backed up; just make sure the secret manager is backed up too.

---

## 1. SQLite backend (default)

Everything lives under `DATA_DIR`, so a backup is essentially "copy `DATA_DIR`
consistently." The only subtlety is the SQLite WAL.

### 1a. Hot backup (app running) - checkpoint the WAL first

The engine runs in WAL mode (`app/db.py`), so recent writes may be in
`loc_tms.db-wal`, not yet in `loc_tms.db`. Copying `loc_tms.db` alone can miss
them. Two safe options:

**Option A - `.backup` (preferred; atomic, no downtime):**
```bash
# writes a single consistent loc_tms.db snapshot (folds in the WAL)
sqlite3 /data/loc_tms.db ".backup '/backup/loc_tms.db'"
```
Then also copy the rest of `DATA_DIR` (uploads + keyfiles):
```bash
cp -a /data/uploads /backup/uploads
cp -a /data/.session_secret /data/.enc_key /backup/ 2>/dev/null || true
```

**Option B - checkpoint, then copy the DB triplet consistently:**
```bash
sqlite3 /data/loc_tms.db "PRAGMA wal_checkpoint(TRUNCATE);"
# copy the db AND its sidecars together, in the same pass
cp -a /data/loc_tms.db /data/loc_tms.db-wal /data/loc_tms.db-shm /backup/ 2>/dev/null || true
```
Copying `loc_tms.db` **with** its `-wal`/`-shm` sidecars keeps them consistent.

### 1b. Cold backup (app stopped) - simplest & safest

Stop the app (`docker compose stop app`) and copy the **whole** `DATA_DIR`. With
no writer, everything is consistent and there's nothing to checkpoint:

```bash
docker compose stop app
docker run --rm -v loc_data:/data -v "$PWD":/backup alpine \
  tar czf /backup/loc_data-$(date +%F).tar.gz -C /data .
docker compose start app
```

This tarball is a complete backup: DB + uploads + keyfiles in one file.

### 1c. DB-at-rest encryption

Two optional modes affect what a backup captures (see
[SECRETS.md §4](SECRETS.md#4-database-encryption-at-rest-sqlite)):

**Always-on SQLCipher (`LOC_TMS_DB_ENCRYPTION=sqlcipher`).** The `loc_tms.db` file
is a SQLCipher database - **encrypted at all times**, running or stopped. Any
backup (hot §1a or cold §1b) captures ciphertext; you need `ENCRYPTION_KEY` (or
`.enc_key`) to open it. Restore is a plain file copy - no decrypt step, the app
opens it transparently on boot. This is the safer mode: a backup taken mid-run is
never plaintext.

**Legacy shutdown vault (retired, #395).** New vault writes are refused.
Existing `loc_tms.db.vault` files need the original `.enc_key` to restore, and
may omit committed WAL pages. Preserve any surviving plaintext database and
sidecars for recovery; do not assume the vault is the latest complete backup.
Plain SQLite backups contain plaintext. Use SQLCipher for database encryption
and see [SECRETS.md](SECRETS.md#4-database-encryption-at-rest-sqlite) for migration.

Either way, `ENCRYPTION_KEY` / `.enc_key` must be in the backup set (or your secret
manager) or an encrypted DB is unopenable.

### 1d. Restore (SQLite)

```bash
docker compose stop app        # or ensure nothing is writing
# restore the whole DATA_DIR from the tarball
docker run --rm -v loc_data:/data -v "$PWD":/backup alpine \
  sh -c "rm -rf /data/* /data/.session_secret /data/.enc_key; \
         tar xzf /backup/loc_data-YYYY-MM-DD.tar.gz -C /data"
docker compose start app
```
If you took an Option-A `.backup` snapshot, drop the restored `loc_tms.db` into
`DATA_DIR` (remove any stale `-wal`/`-shm`) and restore `uploads/` + the keyfiles
alongside it. On next boot the app runs `init_db()` (create-if-missing + additive
migrations) against the restored DB.

---

## 2. Postgres backend (optional)

On Postgres the **database** lives in Postgres (the `loc_pgdata` volume or your
managed instance) and `DATA_DIR` still holds **uploads + keyfiles**. You must back
up **both**.

### 2a. Back up the database - `pg_dump`

```bash
# bundled db service (compose postgres profile)
docker compose exec db pg_dump -U "$POSTGRES_USER" -Fc "$POSTGRES_DB" > loc_tms-$(date +%F).dump
# external / managed Postgres
pg_dump -Fc "postgresql://user:pass@host:5432/locstudio" > loc_tms-$(date +%F).dump
```
`-Fc` (custom format) is compact and restores selectively with `pg_restore`.

### 2b. Back up DATA_DIR (uploads + keyfiles) - same as SQLite §1b

```bash
docker run --rm -v loc_data:/data -v "$PWD":/backup alpine \
  tar czf /backup/loc_data-$(date +%F).tar.gz -C /data .
```
(No DB file here in Postgres mode, but uploads + `.enc_key`/`.session_secret` are,
so this is still required.)

### 2c. Restore (Postgres)

```bash
# 1) restore the database (into an empty/target DB)
docker compose exec -T db pg_restore -U "$POSTGRES_USER" -d "$POSTGRES_DB" --clean --if-exists < loc_tms-YYYY-MM-DD.dump
#    external:
pg_restore --clean --if-exists -d "postgresql://user:pass@host:5432/locstudio" loc_tms-YYYY-MM-DD.dump

# 2) restore DATA_DIR (uploads + keyfiles)
docker run --rm -v loc_data:/data -v "$PWD":/backup alpine \
  sh -c "tar xzf /backup/loc_data-YYYY-MM-DD.tar.gz -C /data"

# 3) scan, pin and start the app; the entrypoint runs `alembic upgrade head` for the
#    Postgres URL. Run the Compose image scan gate in docs/DEPLOY.md first (it builds or
#    reuses, scans and pins every image), then its pin check, then:
docker compose up -d --no-build --pull never
```
Never let a start after a restore build or pull: that serves an image the scan gate never
saw (#385).
The dump was taken from an Alembic-migrated schema, so it restores at the same
revision; `alembic upgrade head` on boot is then a no-op (or applies any newer
revisions). Make sure the restored `.enc_key` matches the key that encrypted the
data, or encrypted uploads/connector tokens won't decrypt (SECRETS.md §3).

---

## 3. Checklist

- [ ] Backup includes the **database** (SQLite file via `.backup`/cold copy, or
      `pg_dump`).
- [ ] Backup includes **`DATA_DIR`** (`uploads/` + keyfiles) captured with the DB.
- [ ] `.enc_key` (or your KMS copy of `ENCRYPTION_KEY`) is recoverable - without it
      encrypted data is lost forever.
- [ ] Restores are tested end-to-end (restore into a scratch instance and log in).
