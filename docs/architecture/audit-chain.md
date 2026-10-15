# Audit Chain - Architecture (L1)

## 1. Role

Exported hashes let a SIEM check linkage, not independently verify integrity. HMAC integrity
requires the dedicated installation audit key and canonical rows. Do not copy that key to a
SIEM. Use the installation's verifier. Anonymous refused mutations are sampled at a maximum
of 60 rows per minute per worker with constant-memory accounting; authenticated actions are
not sampled. The first 60 refused rows in each interval are retained.

This subsystem is the **tamper-evident record of every mutating action**. Each audit row
stores the previous row's hash and its own `row_hash`, an HMAC-SHA256 over
`["audit-v2", prev, ts, actor, action, target, detail, ip, customer_id]`,
serialized as compact JSON with ASCII escaping (UTF-8 bytes), forming a keyed hash chain. The MAC
key is retained in a dedicated audit keyfile, which lives *outside* the database, so an
attacker with database access alone cannot rewrite a row and recompute a chain that
verifies.

**Org scope (#487).** Each row also carries `org_id`, the organization the actor belonged to
when the row was written, stamped at the audit seam from the actor's account (NULL for a
system or unauthenticated event). Like `customer_id` it is a view column outside the hashed
payload, so no `row_hash` changes. Every multi-org view (customer `/audit`, the subject-access
export, the workspace export) filters on it, so a user later moved to another org leaves the
old org's history behind. Rows from before the column are backfilled once, best effort:
the actor's current org, corrected by the recorded `user.move_org` row for rows written before
a move.

Deletion of the tail ("drop my last N rows") is caught separately by a **head anchor**:
every write records the newest row id + hash + a MAC into a Setting row, so a verification
that finds the newest real id has moved backwards fails. The anchor is itself MAC'd, so it
cannot be forged without the key either. A MAC'd `.audit_head` file also pins the
highest observed id and its hash outside the database. Missing anchors, a wiped log,
and older DB copies fail verification even when both Setting rows were deleted.
Normal writes preserve a detected rollback in this file; refilling deleted ids cannot
clear it. The host-only reset operation deliberately re-baselines both witnesses.

On upgrade, `.audit_key` preserves the original session-derived bytes once, without
rewriting existing rows. Later session rotations do not affect audit MACs. Host-only
`audit rotate-key` writes an old-key-signed rotation row with a new-key countersignature,
then atomically persists the new signing epoch in `.audit_epochs`. Verification selects
keys by the filesystem boundaries and checks both signatures at each boundary.
Stop other writer processes for rotation, as for other host maintenance. The existing
chain writer lock serializes threads within a process; it is not a distributed lock.
Filesystem state uses private files, fsync, and atomic replacement. A process crash
between committing a row and updating its witness can leave the witness one write
behind; verification still checks every available row. DB-only rollback protection
starts when the first witness is persisted. Restoring the trust files with an old DB
is outside the DB-only attacker model. See `docs/SECRETS.md` for migration and recovery.

**Write cost and durability (#618).** A row used to cost about 18 ms on Windows (7,000 rows took
128 s) because every `log()` re-opened `.audit_key`, `.audit_format` and the freshly replaced
`.audit_head`, and read the anchor Setting twice. Now `audit_state._stable_bytes` reuses the three
rarely-changing files while `os.stat` reports the same mtime, ctime, size, inode and device (a file
modified or changed in the last 3 seconds is always re-read; a missing file still raises; ctime cannot be
set from user space, so an in-place edit that restores the old mtime is still seen on POSIX),
`audit_state.read(trust_own_publish=True)` reuses the witness this process itself last published (any
other process's publish, an edit, a restore or a deletion changes the signature and forces the full
read and MAC check; both caches work only inside `audit_state.cached_trust()`, which only `log()` enters,
so verification and every maintenance command always read every trust file), and `log()`
hands its one anchor read to `_write_anchor`. Measured locally: 54 to about 99 rows per second.
The durability guarantee is unchanged and no write is batched or deferred: `log()` returns only after
the row is committed (SQLite WAL with `synchronous=NORMAL`, so the commit itself is not fsynced), the
anchor is committed, and the witness file is fsynced and atomically replaced. A crash can still lose
only a row whose `log()` had not returned, and can still leave the anchor or witness one write behind.
What remains per row is two DB commits and one witness fsync; folding the row and anchor commits into
one transaction is a possible follow-up (the shed tests inspect `session.new` between the two).

Beyond writing and verifying, the subsystem runs a **scheduled sweep** (piggybacked on the
scheduler tick) that re-verifies on a cadence, records a forced `chain_broken` row on
failure, latches an anchor-class break so a truncation cannot silently self-recover, and
exposes a monitoring snapshot for the admin readout and the Prometheus gauges. The single
operator recovery path from a legitimate restore-to-older-backup is an explicit,
host-only, self-audited re-baseline.

This is one module (`app/services/audit.py`) documented richly because it is a security
primitive the rest of the system depends on. It does **not** own who is allowed to read
the audit view (that is RBAC + tenancy scoping) or the metrics endpoint that surfaces its
gauges (that is the incident-response/observability subsystem, which reads the snapshot
this module publishes).

## 2. Invariants

- **New rows authenticate unambiguous fields and customer attribution.**
  `app/services/audit_break_class_helpers_log_.py::_compute_hash` uses the v2 JSON array.
  `audit_state.legacy_through` pins the maximum historical row id in private
  `.audit_format` before the first new write or verification. No DB field chooses
  the hash version. Old keyed pipe hashes and pre-keying hashes are accepted only
  within that boundary, before any v2 row; unhashed prefixes are bounded too.
  Reset-anchor and key rotation never extend this boundary.
- **Tail truncation is caught by the MAC'd anchor.** `app/services/audit.py::verify_chain`
  compares the newest row id against the stored head, and `app/services/audit.py::_write_anchor`
  is monotonic (a normal write never moves the head backwards), so truncation evidence is
  preserved - enforced by `verify_audit`.
- **Concurrent full verifications share walks, but never an answer from before the call.**
  `app/services/audit.py::verify_chain` makes a caller that arrives while a walk is running join
  the single pending walk that starts after it, so N concurrent callers cost at most two walks
  instead of N (#608: p50 430 ms alone became 3.1 s at 50 clients). A caller never takes the result
  of a walk that began before its call, because that walk may already be past a row it just tampered
  with or wrote; nothing is cached after a walk ends; a limited (sampled) check is never shared; and
  the callers that already hold the chain lock (`rotate_key`, reset-anchor) call the unshared walk
  directly. Enforced by `verify_perf_608`.
- **An anchor-class break is sticky until an operator resets it.** A now-passing verify is
  refused while `app/services/audit.py::_anchor_break_open` reads true (re-derived from the
  log itself, not from the deletable status Setting row); only
  `app/services/audit.py::reset_anchor` clears it.
- **The MAC key is never in the database.** It is retained in dedicated audit keyfiles,
  so a DB-only attacker cannot recompute a verifying hash.
- **A break is recorded even if auditing is switched off.** The `chain_broken` /
  `chain_status_tampered` rows are forced writes (`app/services/audit.py::log` with
  `force=True`), so nobody can disable the gate and hide a break. (Until 2026-09-20 this said "a super-admin"; that account type is gone, and the property now rests on the forced write rather than on who is blocked from switching the gate off.)
- **Scheduled verification rides the scheduler tick.** Cross-ref
  `app/services/scheduler.py::tick`, which drives
  `app/services/audit.py::verify_chain_scheduled_sweep`.

## 3. Where the work happens

| Concern | Pin | One line |
|---|---|---|
| Witnesses | `app/services/audit_state.py::advance` | Pins head and epochs outside the DB. |
| Append a row | `app/services/audit.py::log` · `app/services/audit_break_class_helpers_log_.py::AUDIT_WRITE_ATTEMPTS` | Chains a new row under a lock and updates the head anchor; forced rows bypass only the gate. |
| Verify the chain | `app/services/audit.py::verify_chain` | Walks the log in batches recomputing hashes and checks the tail against the anchor. |
| The keyed hash | `app/services/audit_break_class_helpers_log_.py::_compute_hash` · `app/services/audit_break_class_helpers_log_.py::_legacy_hash` | The HMAC row hash, plus the pre-upgrade unkeyed hash accepted only for old rows. |
| The head anchor | `app/services/audit_break_class_helpers_log_.py::_anchor_mac` · `app/services/audit.py::_read_anchor` · `app/services/audit.py::_write_anchor` | Computes, reads, and monotonically writes the MAC'd tail-truncation anchor. |
| Scheduled verification | `app/services/audit.py::run_chain_verification` · `app/services/audit.py::verify_chain_scheduled_sweep` | The single service behind the CLI verify and the self-throttling scheduled sweep. |
| Sticky anchor-break latch | `app/services/audit.py::_anchor_break_open` | Re-derives whether an anchor-class break is still open from forced rows in the log. |
| Operator re-baseline | `app/services/audit.py::reset_anchor` | The host-only, self-audited re-baseline after a legitimate restore to an older backup. |
| Monitoring readout | `app/services/audit.py::chain_status_snapshot` · `app/services/audit.py::chain_monitor_view` | The tamper-safe snapshot the admin `/audit` readout and the chain gauges read. |

## 4. Data & settings

- **Model:** `app/models.py::AuditLog` - `actor`, `action`, `target`, `detail`, `ip`, the
  chained `prev_hash` / `row_hash`, and authenticated `customer_id` scoping column
  (historical v1 rows excluded customer attribution). Two MAC'd Setting rows carry the head anchor (`audit_anchor`) and the
  scheduled-verify snapshot (`audit_chain_status`); both are read and written directly, not
  through the settings cache, so an audit write never triggers a settings side effect.
- **Actor values:** a signed-in user's email, the per-user API key holder's email,
  `companion-app` (the shared connector token), `system` / `scheduler` / other named
  background actors, or, for a request that resolved no identity at all,
  `unauthenticated <client ip>` (`unauthenticated (unknown address)` when no address can be
  determined). Such a row's `detail` carries `refused <HTTP status>`, the user agent
  (control characters stripped, about 120 characters) and, when a wrong key or bearer token
  was sent, only its first few characters (`wrong-key-start=...`, never the whole token). A
  genuine key refused only because its org is suspended is named by the display prefix the
  product already shows (`suspended-key=...`). Caller text in detail (the user agent, a typed
  sign-in name) is NFKC-normalized, stripped of control and zero-width characters, bounded
  and JSON-quoted with `;` escaped, so a `"; "` splitter never sees a forged field. A failed
  or locked sign-in names an account as its actor only when the typed name IS an existing
  account; otherwise the actor is `unauthenticated <client ip>` and the typed text is
  evidence in detail: `typed-name="..."` (capped at 80 characters) only when it is an email
  address whose domain belongs to an existing account, else `typed-name-masked id=<8 hex>`,
  because such a name may be a password typed into the wrong box (gate audit-actor round 2,
  LOW-6). A password can itself be a valid address (`Welcome@2024.Fall`), so an address at a
  domain no account uses is masked the same way and keeps nothing else, not even its domain,
  which may be half of the password (gate audit-typedname round 1, LOW-1). Organizations store no domain, so the account table is
  the only domain record; the check is one LIMIT 1 suffix query and masks on any error. The id is
  HMAC-SHA256 over the normalized text, keyed off the session secret under its own label, so
  repeat attempts correlate while the value stays unrecoverable; rotating the secret only
  stops ids correlating across the rotation. An LDAP name the directory bind proved but with
  no local account (`auth.ldap_unprovisioned`) stays readable, quoted and capped in both
  target and detail (`security.typed_name_detail`). One helper set builds these: `app/services/security.py::unauthenticated_actor` and
  `app/services/security.py::unauthenticated_detail`. Rows written before 2026-09-25 used
  `anonymous` for the same case; they are never rewritten (that would break the chain), and
  the `/audit` page alone renders them as `unauthenticated (legacy row)`. The NDJSON export
  keeps the stored bytes so a downstream store can re-verify. Every one of these synthetic
  names is reserved (`app/services/security_client_ip_one_authority_.py::is_reserved_actor`,
  compared after NFKC, case folding, common Cyrillic/Greek lookalikes and removal of every
  zero-width and blank character), so no account, invite or trust principal can claim one, and none ever matches an org's member
  emails, so org-scoped views never show them. A new address carrying a zero-width, format
  or blank-rendering character is refused too
  (`app/services/security_client_ip_one_authority_.py::has_hidden_email_char`), except
  ZWNJ / ZWJ in an IDNA2008 joining context (between two letters of one Indic script after a
  virama, or ZWNJ between joining Arabic, Syriac, NKo or Mongolian letters), which Persian
  and Indic addresses need. Default-ignorable combining marks (variation selectors, the
  combining grapheme joiner and similar) are refused too. A new account or invite whose
  address differs from an existing one only by a joiner is refused
  (`app/services/security_client_ip_one_authority_.py::lookalike_account_id`). Active
  accounts that predate these rules are flagged once per boot by
  `app/services/security.py::warn_lookalike_accounts` (one warning naming account ids only;
  nothing is changed and sign-in is not refused).
- **Settings:** `setting:audit_enabled` (default `"1"`) - the global gate (forced rows
  ignore it). Every change to it is recorded by a forced row written at the settings seam
  (`settings.set_many` emits `audit.logging_disabled` / `audit.logging_enabled`, plus an
  ERROR log line when it goes off), and each door (Security page, `config set`,
  `config apply`) forces its own row too (#220). Turning it OFF is host-only on a server
  install or any install that requires sign-in (`settings.audit_off_is_host_only`): the
  Security page shows the switch locked and refuses an "off" post, `/api` and MCP refuse
  the key outright, and `config set` on the host remains. When it is off, `audit verify`
  prints an `AUDIT OFF:` line and each scheduled verification logs it at ERROR.
  `setting:audit_fail_open` (default `"1"` on a desktop install, `"0"` on a server install,
  #235; host-only on the web on the same installs as `audit_enabled`, gate round 1 L5): the
  contention posture, changes recorded as `audit.fail_open_changed`: fail-open
  drops a shed row and proceeds; fail-closed writes the row of a request that resolved an
  identity BEFORE the handler runs (`app/main.py::_audit_mutating_request` with
  `prerecord=True`),
  so a shed write answers a retryable 503 and nothing changes. Every dropped or refused row
  is counted (`audit.dropped_writes()`) and logged at ERROR with the running total.
  `setting:audit_chain_verify_hours` (default `"24"`, `0` = off) - the scheduled-verify
  cadence; only a literal non-positive integer disables it.

## 5. Enforced by

- `verify_audit` - the keyed hash chain, the anchor, tail-truncation detection, and the
  legacy-hash upgrade window.
- `verify_audit_settings_lock`: audit logging cannot be switched off unrecorded (or at
  all from the web on a sign-in install), fail-closed refuses before the handler, the
  server default, the security-settings confirm ratchet and the multi-org host-only rule
  (#220, #235, #280, #281).
- `verify_audit_chain_schedule` - the scheduled sweep, the re-alert latch, the sticky
  anchor-break, and the tampered-status-row handling.
- `verify_audit_customer` - the `customer_id` stamping and the customer-scoped audit view
  (shared with tenancy confinement).
- `verify_multiorg_batch1005`: the `org_id` stamping and the moved-user cases on the three
  org-confined audit paths (#487), plus the other 2026-10-05 multi-org fixes.
- `verify_audit_perf_618`: the write path's cost budget (#618) as deterministic call counts (no
  trust-file reads and one anchor read per steady-state row), plus the tamper, truncation,
  swapped-witness, deleted-key, threads and multi-process cases run through the cached path.
- `verify_audit_actor_unauthenticated` - every row names who or what acted: an unidentified
  caller is `unauthenticated <ip>` with the refusal status, a wrong token shows only its
  prefix, identified callers keep their own attribution, and no row says `anonymous`.

## 6. Battle scars

- **The chain is a keyed HMAC primitive, not a plain hash.** The `row_hash` is HMAC-SHA256
  keyed off the session-secret keyfile (keyed, not plain sha256), with a MAC'd
  tail-truncation anchor and v2 customer attribution inside the hash input -
  inventoried as controls AU-02, AU-03, AU-04, and AU-13.
- **A chain break is a SEV-1 with a defined recovery.** `app/services/audit.py::run_chain_verification`
  writes a forced `chain_broken` row that is the trigger for the incident procedure (anchor vs. tamper break classes, a sticky anchor-break posture, and `reset-anchor` recovery).
- **A live security sweep confirmed no secret value reaches the audit log.** Lane F's CI-5
  credential-redaction check read vendor secrets back through the audit trail (among other
  surfaces) and found them on zero surfaces, with the R3 config write auditing changed secret
  settings as `(changed)` rather than by value.

## 7. Limits & accepted risks

Stop other writer processes during the v2 upgrade; run `audit verify` once before
restarting them. The first observation pins existing history without rewriting it.
Accepted pre-upgrade limit: a DB-only attacker who inserts rows, including an
unhashed prefix with high ids, before that first observation can have them included
in the legacy epoch. Independently validate existing history before pinning it;
the boundary does not prove that the pre-upgrade database was trustworthy.
Historical pipe-joined rows still permit delimiter-boundary edits and customer
reassignment; verification cannot retroactively authenticate those fields. New
rows use v2 even inside an older restored id range. Keep `.audit_format` with the
other audit trust files; malformed state fails closed. Full backup/restore carries
this boundary, and restoring a pre-v2 archive clears an unrelated host boundary.
A DB-only restore and reset-anchor retain the original boundary.

Even verification of an empty table pins `legacy_through=0`. A later DB-only restore
or migration of pre-v2 rows then fails verification; reset-anchor cannot repair the
format mismatch. Prefer restoring matching trust files with a full backup. For a
known pre-v2 restore only, on a host whose restored chain you have independently
validated against trusted backup/provenance evidence, stop the server and every
writer, preserve copies of the DB and trust files, then delete `.audit_format`
from `DATA_DIR`. Run `audit verify` before any new writes to pin the restored head.
If a legitimate rollback also needs an anchor reset, follow [BACKUP.md](../BACKUP.md),
then verify again before restarting writers. Never delete this file merely to make
an unexplained failure pass: re-pinning admits historical legacy limitations and
does not validate the restore's provenance or reconstruct missing signing keys.

`log()` guards witness reads and writes and key/epoch snapshot failures. One ERROR
per process latches unreadable trust as BROKEN with non-sticky class `trust`; a restart
that verifies green clears it. Witness publication retries sharing violations five
times with 10 ms pauses. Exhaustion warns that the witness trails the DB; the next
write republishes, without latching BROKEN. Missing signing material counts every
dropped row and logs ERROR with the running total at most once per second. The
fail-open setting governs both DB and signing failures: fail-closed answers 503 unless
the request already has its pre-recorded audit row. Restore matching key files before
using the host-only, double-confirmed `audit reset-anchor` command. Stop the server
and all writers, reset, then start; reset refuses a detected server listener.
Reset replaces a corrupt or foreign-MAC witness and records `witness_replaced=1` on
its forced audit row; it cannot repair damaged history or reconstruct missing keys.

Witness advancement follows a successful anchor commit (or its monotonic no-op).
A shed first anchor therefore self-heals on the next write. Initializing a missing
witness when a DB anchor already exists emits WARNING and a status/CLI note, including
on upgrade. Full backup/restore carries the DB and all audit trust files together;
DB-only rollback needs the explicit reset described in [BACKUP.md](../BACKUP.md).

Verification resolves key/epoch material once, snapshots the anchor, witness, and
maximum row id under the chain lock, then walks only that bounded history unlocked.
Normal writes also resolve material once and read the witness once. Witness content
is staged while locked, fsynced outside the chain lock, then published under the lock.
Pending witnesses retain rollback latches; publication generations prevent a slower
older fsync in the same process from replacing a newer witness. A separate OS lock on
`DATA_DIR/.audit_lock` excludes other processes from predecessor reads through anchor
commit and witness publication, including reset and key rotation. Local threads share
that lock until every pending publisher exits, retaining concurrent local fsyncs.
Envelope enable/disable and purge ceremonies take the same process lock before the chain lock.
Windows uses byte-range locking; POSIX uses `flock`. Closing the descriptor (including
process termination) releases ownership. Never remove the lock file while writers run.
All writers of one database must share the same DATA_DIR and a filesystem supporting
these locks, for both SQLite and PostgreSQL; separate per-host trust directories are
not supported. Lock acquisition waits up to 30 seconds; failure drops/counts the row
under fail-open or raises the existing retryable contention error under fail-closed.
It does not latch corrupt trust state. Verification raises a retryable contention error
when the lock is unavailable, without recording a broken-chain result.
Acquisition polls synchronously while holding the local process guard, so a caller
(including an async request thread) and other local entrants can block behind it.
The lock is not fair: continuously overlapping local publishers can keep ownership
and starve another process until its 30-second timeout and configured drop/refusal.
Fsync remains synchronous before `log()` returns.
Explicit reset/rotation maintenance retains its outer lock because it changes
the trust baseline and requires stopped writers. `audit rotate-key` uses the same
listener refusal as `db restore`; neither has a listener override in this release.

`verify_audit_process` exercises real subprocess overlap at predecessor hashing,
witness publication and epoch saving, plus killed holders and lock timeout posture.
It also overlaps envelope setting and purge ceremonies with witness publication.

- **A keyfile compromise defeats the chain.** An attacker who obtains the session-secret
  keyfile *as well as* the database can rewrite rows and recompute a verifying chain; that
  requires filesystem access, at which point nothing app-level survives - stated plainly in
  the module docstring.
- **Under sustained write contention a row can be shed.** The audit write is bounded-retry;
  on exhaustion the `setting:audit_fail_open` posture decides whether the mutating request
  proceeds unaudited (with a loud WARN and an ERROR carrying the running drop count) or is
  refused with a retryable 503 before its handler runs, an availability vs. completeness
  trade-off made explicit as a switch. Under fail-closed, once the request row is written
  before the handler (`audit.REQUEST_RECORDED`), a later detail row in the same request
  (for example `security.update`) that sheds is handled fail-open (ERROR, counted, no
  raise), because the request is on record and its change may have landed. Requests with no
  identity before the handler (sign-in attempts, webhooks, refused probes) keep the
  write-after shape, since their row records the outcome (`refused <status>`, or `system`
  for an accepted webhook); if that row sheds under fail-closed the answer is a clean
  retryable 503 saying the request was handled, never an unhandled 500.
- **Break detection is on a cadence, not instantaneous.** The scheduled sweep runs at most
  once per `setting:audit_chain_verify_hours` (default daily), so a tamper is caught within
  that window rather than at the moment it happens; the `action` column is intentionally
  un-indexed, so the sticky-break re-derivation is O(n) but rides the sweep that already
  walks the whole log.
