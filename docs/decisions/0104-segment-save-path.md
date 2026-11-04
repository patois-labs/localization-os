# 0104. The segment save path: grouped audit rows, a shorter audit lock, no re-reads after commit

Status: Accepted (amends 0100: the bookkeeping lane, the statements under the audit lock, AUDIT_WORKERS)
Date: 2026-10-24

## Context

After ADR 0100 the audit append alone ran at about 360 rows per second, yet the save ladder (`POST
/segments/{id}/save`, 3 web + 6 worker processes, Postgres, a 6-core desktop) stayed at about 55 to 57 req/s with
p95 2.6 to 3.3 s at 100 users (gate G4: 1.0 s). Stage S7d.

What capped it, measured (a wall-clock stack sampler in each web process, per-group and per-request timing in a
throwaway instrumented copy, cProfile of the save in process):

- **Every save's audit row was written alone.** The middleware writes its row inside `bookkeeping.scope()`, and
  the 0100 security fix (M2) sent every bookkeeping caller inline, so no save row ever joined a group: each took
  its own turn through the cross-process audit lock. About 2.7 of the 4 audit threads per web process sat in
  `audit_state.process_lock`, and each save's response waits for its row.
- **The locked section was long.** About 10 ms unloaded, 15 to 22 ms under load: a separate witness-check
  session (2 reads) before an 8-statement transaction, held while other threads competed for the GIL (5 ms
  hand-offs on Windows, #615).
- **The handler re-read what it had just written.** The save's commit expired every loaded row, so the
  memory add-on's capture, the post-edit capture and the response re-SELECTed the segment and its document (with the
  whole skeleton). (The two project reads are not caused by expiry and remain.)
- **The single-process load driver saturated** once the server answered in bursts: its own scheduling delay
  read as an 18 s maximum and a 4 to 5 s p95. All numbers below use `LOAD_PROCS=4`.

## Decision

- **A bookkeeping lane.** The group appender keeps one queue per routing class (`audit._LANES`): bookkeeping
  callers (the middleware's audit tail) group only with each other, so a group's leader routes exactly as each
  of its followers would; every other caller uses the plain lane. A relief-scope caller still writes inline
  (gate A HIGH-4), and the 0100 M2 fallback (a failed group rewritten event by event in each caller's own
  captured context) is unchanged.
- **Five statements under the audit lock.** The witness check now reads inside the group's own transaction,
  still before any row is inserted: one SELECT returns the predecessor id and hash, the witness row's hash and
  the anchor value; then the rows' INSERT, a plain `UPDATE setting` for the anchor (an INSERT only when it is
  missing) and the commit (plus the Postgres advisory lock). Same reads, same order relative to the insert,
  same latch and anchor rules.
- **`streaming.AUDIT_WORKERS` 4 -> 32.** It caps how many middleware rows one group of a web process can carry.
  0100 kept 4 because 16 measured worse; that measurement was taken while every save row was written inline
  (16 inline writers per process thrashing the cross-process lock) and with the saturated single driver.
  Waiting threads hold no database connection. Like the 4 before them they are not in `memory_estimate_mb`.
  Connection headroom (security review LOW-2): at a saturated pool a bookkeeping caller takes an emergency
  (pool-less) connection for its short pre-group reads, so a web process can briefly open up to 32 of them beyond
  its pool (it was 4); size Postgres `max_connections` with that in mind. A bookkeeping group runs under its
  leader's per-request pool-wait budget (LOW-3): that changes only how fast a pool timeout arrives, and a failed
  group is rewritten event by event in each caller's own context.
- **The save keeps its committed rows loaded.** `segment_save` commits with `expire_on_commit` off for that one
  commit (restored at once): nothing on those rows has a server-side default or trigger, so the loaded values
  are the committed values, and the captures and the response read them without a re-SELECT.

Behaviour is unchanged: the response, the stored segment, the memory add-on's event and the post-edit episode are the
same (pinned by `verify_save_path_s7d`, which fails on the pre-change tree), every write is still synchronous,
and the chain format, verification and crash bound of 0100 are untouched.

## Measured (a 6-core desktop, Postgres, 3 web + 6 workers, seed 200, write 100 users, 75 s, LOAD_PROCS=4)

Paired, interleaved runs of the load harness. Each arm ran
twice; the base row and the final row are about 20 minutes apart, on a host other test runs
kept 62 to 79% busy. The single-driver runs (`LOAD_PROCS=1`) showed 1 to 5 errors and a p95 of
4 to 7.5 s in the change's arm only, with that driver at 69 to 74% CPU against 23% for base; with 4 driver
processes both arms ran error-free. The 2.5x effect is far above this noise (the same AUDIT_WORKERS 16 arm measured
126 to 130 req/s in two runs).

| Build | req/s | p50 | p95 | errors |
|---|---|---|---|---|
| before | 57.1 / 57.8 | 1.68 / 1.58 s | 2.87 / 3.02 s | 0 |
| + bookkeeping lane + 5 statements, AUDIT_WORKERS 4 | 93.9 / 93.1 | 1.03 / 1.05 s | 1.44 / 1.49 s | 0 |
| same, AUDIT_WORKERS 16 | 128.0 / 130.4 | 0.74 / 0.74 s | 1.12 / 1.11 s | 0 |
| + save keeps rows loaded, AUDIT_WORKERS 16 | 140.6 / 140.8 | 0.66 / 0.67 s | 1.14 / 1.09 s | 0 |
| + save keeps rows loaded, AUDIT_WORKERS 32 (this ADR) | 146.3 / 145.2 | 0.66 / 0.67 s | 1.01 / 1.03 s | 0 |

In process, one changed save went from 48 ms and 24 statements to 26 ms and 19. The three web processes now
run at about one core each: the save path is CPU-bound in the web tier, no longer waiting on the audit chain.
G4 sits at its 1.0 s line on this desktop; Linux uses a 0.5 ms GIL hand-off (#615), which shortens the audit lock
hold further. Further gains are CPU per save (memory add-on and post-edit captures, the AI opt-out lookup, the
middleware stack) or more web processes.

## Review

A security review of the audit changes passed round 1 with four low-severity findings, fixed: the anchor UPDATE falls back to an INSERT unless it hit the row,
the rollback latch across a retried, shed stage is pinned ([16] in `verify_audit_group_commit`), and the
connection headroom and pool-budget notes above. The code review passed round 1 (byte-identical dumps of eight save
shapes against the pre-change tree) with four low-severity findings, fixed: the measurement caveat above, the project-reads claim,
the audit threads named as outside `memory_estimate_mb` in `docs/architecture/configuration.md`, and a flagged
save in `verify_save_path_s7d` whose response must equal a fresh read.
