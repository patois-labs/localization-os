# 0100. Audit chain writes are group-committed

Status: Accepted (amends the write path of the audit chain; the chain format, verification and trust files are unchanged). Amended by 0104: bookkeeping callers group on their own lane instead of writing inline, the witness check reads inside the group transaction, AUDIT_WORKERS 32.
Date: 2026-10-24

## Context

S3c (ADR 0097) found that editor saves cap near 100 per second because every save appends one row to
the tamper-evident audit chain (`docs/architecture/audit-chain.md`), and every row took its own turn through the
cross-process audit lock (`DATA_DIR/.audit_lock`): a witness check, a predecessor read, a row commit, a separate head
anchor commit and a witness fsync. Gate G4 (save p95 at 100 users) measured 3.4 s against a 1.0 s target. Compliance
mappings (controls AU-02, AU-03, AU-04, AU-13) rely on the chain, so the redesign must not weaken it. A group-commit
redesign was approved on the condition of a security review.

### Guarantees before this change (written down before changing anything)

- **Ordering.** Rows chain in id order. `audit.log` read the newest row and inserted the next one under an in-process
  `RLock` (`_chain_lock`) inside the cross-process OS lock (`audit_state.process_lock`, byte-range lock on Windows,
  `flock` on POSIX, fair mode in the split roles). All writers of one database must share one `DATA_DIR`. `ts` was set
  inside the lock, so it never decreased with id within a process.
- **Hash linking.** `prev_hash` is the previous row's `row_hash`; `row_hash` is HMAC-SHA256 over the canonical JSON
  `["audit-v2", prev, ts, actor, action, target, detail, ip, customer_id]` with the key of the row's epoch from
  `.audit_key` / `.audit_epochs` (outside the database). The id and `org_id` are not hashed.
- **What verify checks.** Every row up to a snapshot head, in id order: the `prev_hash` link, the recomputed hash (v2,
  or v1 / unkeyed legacy only inside the pinned `.audit_format` boundary), the rotation row at each epoch boundary;
  then the tail against the MAC'd anchor Setting (newest id below the anchor head fails), a missing anchor with keyed
  rows fails, and the MAC'd `.audit_head` witness must name an existing row with a matching hash and must not carry
  the broken latch. Rows need not be contiguous.
- **Crash behaviour.** `log()` returned only after the row commit, the anchor commit and the witness fsync plus atomic
  replace. A crash could lose only a row whose `log()` had not returned, and could leave the anchor or the witness one
  write behind; verification accepts a trailing anchor or witness and the next write catches them up. SQLite runs WAL
  with `synchronous=NORMAL` (a commit survives a process crash, not necessarily a power cut); Postgres commits are
  durable.
- **Shed posture.** The row insert had two attempts and the anchor one; a shed row was dropped (fail-open, counted, WARN
  plus ERROR) or refused with a retryable 503 (fail-closed), with the `REQUEST_RECORDED` downgrade; missing signing
  material or an unavailable process lock counted and dropped (or refused) the row. A shed anchor alone was tolerated.
- **Key rotation.** Host-only, writers stopped; it holds both locks, writes an old-key-signed rotation row with a
  new-key proof, then saves the epoch. `reset-anchor` likewise holds both locks. Both call `log()` while holding them.
- **Forced rows** (chain_broken, anchor_reset, key_rotated, operator verbs) bypass only the `audit_enabled` gate.

## Decision

- **Group commit inside a process, no timer, no background thread.** `log()` freezes its fields into an event and hands
  it to `audit._submit`. The first caller that finds no active leader leads: it takes the audit locks once and writes
  up to `GROUP_MAX` (64) queued events, oldest first, as one group, repeating until its own event is written, then
  hands the role on. A group is whatever queued while the previous group held the locks, so an idle process writes
  groups of one at the old latency and a busy one batches in proportion to its load (natural batching; a linger
  window would only add latency).
- **One transaction per group.** On Postgres the transaction first takes `pg_advisory_xact_lock` on a fixed key (a
  database-level second guard for this path; the file lock stays the authority, since the ledger ceremonies' own
  audit row and the witness check do not take the advisory lock); then it reads the predecessor, builds every row (timestamps, links and HMACs in arrival order),
  flushes them, checks that the assigned ids ascend above the predecessor in link order (otherwise the group rolls back
  and every event is rewritten alone), and stages the head anchor in the same transaction. One commit. The witness is
  then fsynced and published once (`buffered_witness`, outside the chain lock, under the process lock).
- **Across processes** groups take turns on the existing `.audit_lock` (fair mode in the split roles), one group per
  turn. The chain format, verification, export, retention and GDPR paths are unchanged: a grouped row is an ordinary
  row and verifies event by event.
- **Every write stays synchronous.** A caller is released only after its group's commit and the witness publication,
  or with its own outcome. Security events need no special path: no audit write is deferred.
- **Per-caller outcomes.** A shed group (the commit lost the SQLite writer lock twice) sheds every row in it, and each
  caller applies its OWN posture snapshot and `REQUEST_RECORDED` state; every row is counted. Trust-file and process
  lock failures likewise reach each caller's own `_signing_drop`. A non-lock error before the commit (one bad event,
  or the leader's own pool budget running out) rewrites the group one event at a time, each in its own caller's
  captured context (`contextvars.copy_context()` at `log()`), so only the caller it belongs to raises. An error from
  the commit itself is ambiguous (the rows may be durable) and is never rewritten: every caller of the group gets it,
  as the one caller of a single-row write did. A leader that dies (BaseException) after its commit reports its
  followers' rows as written, and its own unwritten event is taken off the queue.
- **Nested writers write inline.** A thread already inside `process_lock` or holding `_chain_lock` (rotate-key,
  reset-anchor, the ledger envelope ceremonies) writes its row as a group of one and never waits on another thread's
  group, which would need the locks it holds. Lock order is unchanged: process lock, chain lock, database.
- **Relief and bookkeeping scopes write inline** (security review M2). A request in the capacity doorman's relief
  scope (gate A HIGH-4: cancel and settings at a saturated pool) or a bookkeeping scope reaches the database through
  its own pool-less routing; a leader in another context would not apply it, so such a caller never joins a group.
- The middleware's audit worker limiter in the split roles stays at 4 threads (`streaming.AUDIT_WORKERS`). Raising it
  to 16 was tried and measured worse end to end (write-50 p95 2.5 to 3.1 s against 1.5 s, and dropped connections):
  more threads per web process doing the pre-group reads cost more than the bigger groups saved.

## Crash bound

No event is acknowledged and then lost. Precisely:

- A crash before a group's commit loses that group and nothing else. None of its callers had returned (all were
  blocked in `log()`), so the set of lost events is exactly the set of unacknowledged `log()` calls, as before: under
  the old design the callers queued on the lock were equally unacknowledged and equally lost.
- The rows and the head anchor commit atomically, so the anchor can no longer trail the rows (it could trail by one
  write before). The monotonic anchor rule is unchanged.
- A crash after the commit and before the witness is published leaves the witness behind by at most one group (at most
  `GROUP_MAX` rows, before: one row). Verification already accepts a trailing witness and checks every available row;
  the next write republishes it. A tail truncation of rows newer than the trailing witness is then guarded by the
  anchor alone, which committed with them; a DB-only attacker able to replay an older, validly MAC'd anchor value
  could hide that truncation, as before, but the exposed window is now up to one group of rows instead of one row.
- What a write-after middleware row describes (the handler already ran) and the SQLite `synchronous=NORMAL` power-cut
  caveat are unchanged.

Tested by `verify_audit_group_commit` (a child process killed after flushing a group but before its commit, and one
killed after a commit but before the witness publish; every acknowledged row present, the chain and the lock intact).

## Security review

A security review of the design and the code ran before merge. Round 1 failed on:
M1 (the suites' shed fakes could not see flushed rows, so the fail-closed 503 went untested; the group now keeps its
rows referenced until the commit), M2 (the leader wrote followers' rows in its own context; relief and bookkeeping
callers now write inline and the fallback runs each event in its own context), L1 (an ambiguous commit error was
rewritten, possibly writing rows twice), L2 (BaseException outcomes), L4 (crash-bound wording) and the advisory-lock
wording; all fixed, with regression tests [10] to [14] in `verify_audit_group_commit`.

The final review passed the change with four low-severity findings, fixed: dead
`_write_anchor` parameters, stale sentences in `audit-chain.md`, two crash-test assertions that could not fail, and
the audit worker threads missing from the memory estimate (moot after the revert to 4).

## Measured (a 6-core desktop, Postgres)

Audit append alone (a small benchmark script, role web, 20 s per shape, chain verified after each):

| Appenders | Before | After |
|---|---|---|
| 1 process x 1 thread | 69.6 rows/s, p95 16.1 ms | 75.8 rows/s, p95 15.5 ms |
| 3 processes x 4 threads | 77.0 rows/s, p95 360 to 400 ms | 102.7 rows/s, p95 216 to 239 ms |
| 3 processes x 16 threads | 77.2 rows/s, p95 1.0 to 1.7 s | 361.7 rows/s, p95 214 to 253 ms |

Save ladder end to end (the load harness, 3 web + 6 worker processes, seed 200, 120 s steps,
round 3 with the final AUDIT_WORKERS = 4): write-50 56.9 req/s p95 1.51 s before, 55.0 req/s p95 1.55 s after;
write-100 55.8 req/s p95 3.27 s before, 55.5 req/s p95 2.81 s after, no errors either side. The desktop ran at 66 to 99%
host CPU from other test runs, and earlier rounds moved by up to 2x between identical runs, so
the end-to-end comparison is within noise: on this desktop the save path is bound by CPU and the single load driver, not by
the audit chain, whose own ceiling rose from about 77 to about 360 appends per second. G4 (save p95 1.0 s at 100
users) is not met by this change alone; a rerun on a quiet server is the follow-up.

## Consequences

- The per-row cost on the chain (two commits, a witness fsync, three reads) becomes a per-group cost (see Measured).
- A group holds the cross-process lock for one transaction of up to 64 rows; a shed group drops (or refuses) up to 64
  rows at once instead of one, each counted and logged (a flood that sheds now sheds whole groups; fail-closed
  installs refuse those requests instead).
- Tests that fake `commit_or_shed` must look for audit rows the stage already flushed (`session.identity_map`), not
  only `session.new`.
