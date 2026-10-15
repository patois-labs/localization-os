# Backups & Disaster Recovery - Architecture (L1)

## 1. Role

This subsystem is the **whole-install backup, verification, and restore** core. It produces
one timestamped, self-describing `.tar.gz` archive covering everything a machine loss would
otherwise destroy: the database (a consistent snapshot), the uploads tree, and - critically,
the Fernet encryption keyfiles that make every encrypted upload and stored secret readable.
It can verify an archive without touching the live install, and restore one atomically onto a
blank machine.

The archive is self-describing: an embedded `manifest.json` records the schema version,
timestamp, app version, backend, and a per-member sha256, so verification is a closed check
against the archive's own contents. Verification is a hard gate on restore - a restore
verifies the whole archive (manifest completeness, no unlisted members, per-member sha256,
and an integrity check on a temp copy of the contained DB) **before any destructive write**,
unless the operator forces the documented desperate-recovery escape hatch.

The public verbs are the `db` CLI group - `app/cli/cmd_db_db_init_schema.py::_backup_full`,
`app/cli/cmd_db_db_init_schema.py::_verify_backup`, and `app/cli/cmd_db_db_init_schema.py::_restore_full` - which are thin
wrappers over the archive core documented here; the CLI module itself is left for the Phase-2
jobs/data docs. There is no web or `/api` surface: this runs in-process against the host's
real data directory and is **host-CLI-only by design**, because a remote surface that
streamed keyfiles off-box would be an exfiltration channel.

What it does **not** own. It does not own the encryption of the keyfiles it carries, nor the
SQLCipher keying it reuses for a snapshot - that is [secrets-and-crypto](secrets-and-crypto.md).
It does not own the tamper-evidence of the data inside the archive (the audit chain is
[audit-chain](audit-chain.md)). And it is a data-durability tool, not a tenancy or access
control.

## 2. Invariants

- **Legacy files are staged and verified too.** `app/cli/legacy_restore.py::restore_database`
  validates the staged database with `PRAGMA integrity_check`, disposes the CLI pool,
  and moves the old database and sidecars into private rollback storage. Sidecar move
  errors abort. Final integrity and SHA-256 checks must pass before success; failed
  rollback retains recovery files and reports their location. The source must be a
  standalone backup, without SQLite sidecars. `--skip-verify` cannot bypass this path.
- **A restore verifies the whole archive before any destructive write.** `app/cli/backup_restore.py::restore_archive`
  runs `app/cli/backup_common.py::verify_archive` first and refuses a failing archive,
  unless the operator passes the documented `--skip-verify` desperate-recovery flag -
  enforced by `verify_backup_full`.
- **Extraction refuses traversal, links, and decompression bombs.** The staged extraction
  rejects any member whose path would escape the destination, any symlink/hardlink/device
  member, and any archive that extracts past a size cap (the smaller of the manifest total
  and a free-disk ceiling), then uses Python's own `data` tar filter as belt-and-braces.
- **Staged swaps journal originals before installing replacements, keyfiles first.**
  `app/cli/backup_restore.py::_swap_file` and `_swap_dir` record each displacement
  immediately. Any swap failure triggers `_rollback`, including an incomplete swap.
  Rollback attempts every journal entry and reports failures with affected paths.
  Existing `.pre-restore` copies are preserved regardless of age, and block a swap
  that would overwrite them. Only a successful restore cleans its own journaled copies.
  Conflicts are checked before the first swap. Failed success-path cleanup leaves the
  restore successful but reports `leftover_recovery_copies` in JSON and a CLI warning
  listing paths to remove or secure after confirming the restored install is healthy.
  Before retrying, recover the original or move the copy to secure storage; it can
  contain irreplaceable key material. Checked against `app/cli/backup_restore.py`.
- **Keyfile members are forced to 0600 and the archive to 0600.** The archive may hold live
  key material in the clear, so both the created archive and every restored keyfile are
  narrowed to owner-only. Archive siblings are created atomically at 0600 and every
  missing destination ancestor at 0700; existing directories keep their permissions.
  A missing filesystem root or a file blocking the destination directory fails with
  `BackupError`; ancestor discovery stops at the root instead of looping indefinitely.
  A hard kill can leave a private `<destination>.<random>.partial` file; remove it manually once no backup is running.
- **A plain `db backup <path>` (SQLite copy or `pg_dump` file) is created 0600 as well (#544).** It holds password and key hashes, encrypted connector tokens and all tenant content, so the command runs under a 0077 umask (the file never exists group- or other-readable) and chmods the result 0600, including a file kept by `--force`. On Windows there are no POSIX permission bits: the umask has no effect and `chmod 0600` only clears the read-only flag, so the backup inherits the ACL of its destination folder. Put it in a folder only the service account can read.
- **The DB snapshot is transactionally consistent, encrypted-aware.** The SQLite snapshot
  uses the online `.backup()` API (folding in the WAL, no checkpoint race); a SQLCipher DB is
  snapshotted and later verified through a keyed connection (cross-ref
  `app/services/dbcrypt.py::connect`), so an encrypted backup is never a lossy raw-ciphertext copy.
- **Only `BackupError` escapes the verbs.** `app/cli/backup_common.py::BackupError` is the
  single clean failure type the command layer maps to a CLI error; an unexpected exception
  mid-restore is caught, rolled back, and re-wrapped.

## 3. Where the work happens

| Concern | Pin | One line |
|---|---|---|
| Restore a legacy SQLite file | `app/cli/legacy_restore.py::restore_database` | Stages and validates a standalone backup, disposes pooled handles, preserves old DB/sidecars, and verifies the final recovery point with rollback on failure. |
| URL presentation | `app/db_url.py::redact_db_url` | Replaces database URL userinfo with *** in rendered errors; restore hints use libpq flags and environment/pgpass credentials. |
| Create the archive | `app/cli/backup_archive.py::create_archive` | Snapshots the DB, adds uploads + keyfiles, writes the sha256 manifest, and renames a 0600 tarball into place. |
| Verify the archive | `app/cli/backup_common.py::verify_archive` · `app/cli/backup_common.py::is_full_archive` | The restore-verification control: manifest, no-unlisted-members, per-member sha256, and a DB integrity check. |
| Restore the archive | `app/cli/backup_restore.py::restore_archive` · `app/cli/backup_restore.py::_swap_file` · `app/cli/backup_restore.py::_swap_dir` · `app/cli/backup_restore.py::_rollback` | Verifies, extracts to staging, then atomically swaps each member (keyfiles first) with rollback on any failure. |
| Error type + manifest constants | `app/cli/backup_common.py::BackupError` · `app/cli/backup_common.py::MANIFEST_SCHEMA` · `app/cli/backup_common.py::KEYFILE_NAMES` | The clean failure type, the manifest schema version, and the two keyfiles the archive carries. |
| cmd_db connector | `app/cli/cmd_db.py` · `app/cli/cmd_db_db_init_schema.py::_is_sqlite` | cmd_db: db init-schema. |

## 4. Data & settings

- **Config-driven, not settings-gated.** What the archive covers is fixed by config, not by
  `settings.get` keys: `app/config.py::DB_PATH` (the SQLite database),
  `app/config.py::UPLOADS_DIR` (the uploads tree), `app/config.py::DATA_DIR` (the root that
  holds the keyfiles and the restore staging dir), and `app/config.py::DB_ENCRYPTION_ON`
  (whether the snapshot and verify use a keyed SQLCipher connection).
- **The manifest, not a table.** There is no model or Setting for backups: the archive's
  `manifest.json` member carries the schema version (`app/cli/backup_common.py::MANIFEST_SCHEMA`),
  timestamp, app version, backend, and the per-member sha256 list;
  `app/cli/backup_common.py::KEYFILE_NAMES` names the two keyfiles (`.enc_key`,
  `.session_secret`) it includes.

## 5. Enforced by

- `verify_legacy_restore` covers pooled WAL handles, corrupt legacy backups, locked
  sidecars, partial copy, failed installation, and verification-triggered rollback.
- `verify_backup_full` - the full round trip: create, the verification control (including the
  unlisted-member and traversal refusals), and the atomic staged restore with rollback.
- `verify_restore_recovery` covers failed installation renames, rollback error reporting,
  and preservation of recovery copies on retry.
- `verify_db_verify` - the `db verify-backup` operator control surfaced over the CLI.

## 6. Battle scars

- **The whole-install archive closed the single riskiest DR gap.** The legacy `db backup`
  captured only the SQLite file, leaving uploads and the Fernet keyfiles unrecoverable; the
  `--full` archive (DB + uploads + keyfiles + sha256 manifest), the `verify-backup` control,
  and the atomic verified restore are inventoried as controls AV-11 / AV-12 / AV-13 and gap
  G-06.
- **Verify-before-swap and the no-archive-passphrase rationale are in the runbook.** The DR
  procedure, the deliberate decision *not* to add an archive-level passphrase (the
  circular-key problem - you would need a key to protect the key backup), and the
  encrypt-at-destination storage guidance follow it.

## 7. Limits & accepted risks

- **Backups are manual and unscheduled; no drill has been run.** The archive + verify +
  restore all exist and pass their hermetic suite, but wiring a scheduled backup job and
  actually performing (and logging) a restore drill are named follow-ups, and the RPO/RTO
  targets are unratified placeholders - tracked in the G-06 residual.
- **The archive holds live key material in the clear.** By design there is no archive
  passphrase (the circular-key rationale above); the file is created 0600 and the operator is
  told to encrypt at the destination - record each archive's own sha256 out-of-band for
  tamper-evidence, since the manifest lives inside the archive and only detects corruption.
- **Postgres DB restore is not automated.** A Postgres-backend restore replaces uploads and
  keyfiles automatically, extracts the dump, and reports **PARTIAL** with exit `1`.
  The database has NOT been restored. JSON carries `ok: false`, `status: "partial"`,
  and `db_restore_required: true`. Run the printed `pg_restore` recipe successfully
  before starting the server. Complete SQLite restores retain their human output and
  exit `0` with `ok: true`, `status: "complete"`, `db_restore_required: false`.
  Both dry-run and real summaries derive status from whether a database member was
  present but not restored (dry-run retains its existing planned-action semantics).
  This operation is local CLI only, with no web/API/MCP restore action; full
  automation is a named follow-up.
- **Concurrent restores remain unsupported.** Existing recovery copies block swaps,
  but the check and rename are not a cross-process lock. Run only one restore at a
  time; a restore lock remains an open item.

PostgreSQL archives record dump_size and pg_dump_version in the manifest.
Verification rejects trivial headers and requires pg_restore --list to return a
parseable nonempty schema/data table of contents. A recorded dump size that
disagrees with the archived member fails; a recorded pg_dump version that is
absent, malformed or disagrees with the dump's own TOC is advisory (a
dump_metadata warn, verdict UNVERIFIED) and does not block a restore, because the
dump itself is intact and pg_restore --list validated it. Missing pg_restore
yields warn and UNVERIFIED in human and JSON output, with a nonzero exit. Restore refuses that checkpoint
unless the operator explicitly skips verification. This check cannot prove data
restoration; the AWS recovery drill does that separately. PostgreSQL restore
still extracts the dump and swaps ancillary state, requiring manual pg_restore
before restarting any image. Passwords use PGPASSWORD or ~/.pgpass and never argv.
