# 0106. Read and save request cost: narrower reads, one observer commit, a lighter middleware layer, a shorter audit lock

Status: Accepted
Date: 2026-10-24

## Context

After S1-S8 the translate pipeline met its targets, but the two request benchmarks did not (a 16-core test server,
Postgres, before S7d): page reads at 50 users 1,096/s, editor saves at 100 users 160/s; with S7d (ADR 0104), 1,083
reads/s and 483 saves/s. Stage S9 "Reads and saves".

Measured on a 6-core desktop (Postgres, the load harness's exact requests):

- The four read-cycle GETs (`/api/projects?limit=20`, `/api/projects/{pid}`, `/api/projects/{pid}/documents`,
  `/api/documents/{did}/segments?detail=segments`) ran about 23 statements and 9 pool checkouts per cycle (steady
  state), loaded unused JSON and blob columns (`Document.skeleton`, `Customer.settings`,
  `SegmentationProfile.srx_blob`) and cost 7 to 14 ms CPU each in process. SQLAlchemy + psycopg were 40 to 50 % of
  self time, the framework about 17 %.
- The save ran 17 statements and 4 commits (segment, memory event, post-edit row, audit group).
- On the test server the reads were CPU-bound in only 4 web processes (host 21 %). The saves plateaued near 500/s from 50 to
  800 users at 15 % host CPU: the audit hash chain, whose appends all serialize under one cross-process lock, topped
  out at 440 to 640 rows/s on the desktop; inside one group the database transaction took 4.7 ms for one row and 10.5 ms
  for eight (SQLModel row construction, ORM flush), plus 2.2 ms of witness fsync and publish.

## Decision

Four packages, each reviewed before merge (the three that touch tenancy, auth or audit with a security review).

- **Read handlers (WP-READS).** Each handler loads only the columns it returns; the fixed-shape statements are
  pre-built (`db.prebuilt`); the list's two `hidden` counts are one statement and customer names come only for the
  page's customers; the documents and segments routes derive what they can from rows they already hold. The
  visibility check keeps one home: `_project_in_org` gains a `ring=` mode that runs the same three checks in the same
  order on a two-column read for unrestricted callers; everyone else takes the old path, and `access.can_view_project`
  still decides. No caching. Statements per cycle 23 -> 14.
- **Save handler (WP-SAVE).** Segment, document and the paused-run flag in one pre-built read; the `document.fmt`
  re-read is gone; the memory add-on reads project and customer once; the memory event and the post-edit row share one
  commit. A shed of that shared commit (or a writer lock already at the memory event's flush, which is where SQLite WAL
  reports it) gives the memory event its own bounded `commit_or_shed`, as before, so capture still never fails a
  committed save and the event survives a lock only the post-edit row lost. 17 statements + 4 commits -> 12 + 3.
- **Framework (WP-FW).** `_security_audit` runs on `app/web/lite_http.py::LiteHTTPMiddleware`, an adapter with
  BaseHTTPMiddleware's contract (child task, held start message, cached body, response replacement) without the
  memory stream, relay and per-receive task group; it falls back to the stock layer if Starlette's private
  `_CachedRequest` changes. `_feature_gate` is plain ASGI around the same decision. FastAPI's recursive
  `_get_routes_version` is memoized and invalidated by every `_mark_routes_changed`. A streamed response is no longer
  read one chunk ahead of the client, so a response whose app raises mid-stream is aborted (it used to end as a body
  that looked complete), and a live-translate stream cut by the client stops before the next provider call and is
  charged exactly what it spent (the review checked: never under- or double-charged). The API-key lookup is unchanged:
  an AUTOCOMMIT variant was refused by the security review (it checked out a pooled connection before the
  relief/bookkeeping routing applied) and reverted.
- **Audit chain (WP-AUDIT).** Row values that do not depend on the predecessor are prepared before any lock; under the
  locks only `ts`, `prev_hash` and `row_hash` are computed, and the group is written with one `INSERT ... RETURNING`
  (Postgres: `unnest` over array parameters; SQLite: a multi-row insert, per-row without RETURNING). The combined
  witness read and the anchor update are pre-built; the per-row org lookup is one pre-built statement. Lock order,
  witness rules, the acknowledgement point (fsynced and published before any caller is released), shed/lock/trust
  outcomes and the chain format are unchanged; a scripted sequence gives byte-identical chain, anchor and witness on
  both trees and both databases. A None in a hashed field, which the baseline stored as "" but hashed as null (a row
  that later fails verify), is now refused with an IntegrityError; no caller passes one.

Guards: `tests/verify_read_budget_s9.py`, `tests/verify_save_budget_s9.py`, `tests/verify_audit_group_s9.py`,
`tests/verify_http_middleware_shape.py` (each red on the pre-change tree).

## Measured

Desktop (i5-10400F, Postgres, 3 web + 6 worker processes, LOAD_PROCS=4, 60 s steps, interleaved, two runs each; host
at 89 to 98 % CPU, so the tip numbers are box-bound):

| | before | after (S9) |
|---|---|---|
| reads, 50 users | 205-231 req/s, p95 0.28-0.32 s | 379-384 req/s, p95 0.17-0.18 s |
| web CPU per read | 9.0-11.6 ms | 5.6-6.1 ms |
| saves, 100 users | 128-137 req/s, p95 1.07-1.17 s | 208-219 req/s, p95 0.73-0.77 s |
| web CPU per save | 18.4-20.0 ms | 8.2-10.2 ms |
| audit append, 1 process x 16 threads | 389 rows/s | 699 rows/s |
| audit append, 4 processes x 16 threads | 366 rows/s | 445 rows/s |

SQLite (same layout and driver): reads 258 -> 516 req/s (p95 0.29 -> 0.13 s, web CPU 10.6 -> 5.8 ms per request).
Saves stay bound by SQLite's single writer: 74-80 -> 76-84 req/s, p95 about 3.0-3.2 s on both trees, web CPU 23 ->
10.7 ms per save. Both trees answer 0.4 to 0.8 % of saves with a 500 from the save's own bare `session.commit()`
(SQLite "database is locked" after the 5 s busy timeout, 3 web + 6 worker processes on one file): pre-existing, not
caused by this change; a follow-up should shed that commit like the observers do.

## Not done (follow-ups)

- More web processes on the test server: reads there were capped by 4 web processes at 21 % host CPU; the harness's
  `LS_WEB_PROCS` should match the cores the load test owns.
- The audit chain stays one serialized chain: beyond about 2x, saves need a design change (overlapping the witness
  fsync with the DB commit, or per-process sub-chains with a merged witness), which changes the audit design.
- On Windows a cross-process `audit_state.read()` costs 3 to 6 ms after another process published the witness.

## Addendum (same day): the budget meter's month index no longer herds as data grows

S7c found on the test server that reads degrade with data: after a 60-minute soak (11,500 projects, about 20,000 jobs) 25
readers fell to 19 req/s with 16 % "connection pool exhausted". Cause: `GET /api/projects/{pid}` prices the display
budget meter from `project_budgets._month_job_index`, which loaded every cost-bearing job of the month (full rows, all
projects) and was rebuilt every 15 s per process by every concurrent caller at once (cache misses plus every
per-project refresh thread), each holding a pool connection. Now one build per process at a time from five columns;
concurrent callers get the previous index only while it is recent (a build that started within one TTL of its
expiry), otherwise they wait for the build; a stored index records its build start so a late build never overwrites a
newer one; refresh threads wait before opening a session. Gates keep their live per-project scan. Values
byte-identical (randomized 40-project DB, 211- and 3,011-project load DBs). Guard
`tests/verify_meter_index_single_flight.py`. Staleness: under 3 x 15 s plus a refresh and two builds while a project
is being read, typically 2 x 15 s (docs/architecture/cost-and-budgets.md).

Desktop, a 3,013-project Postgres test instance (18,080 jobs), 3 web + 6 workers, 25 readers, 3 interleaved rounds:
reads 208-222 -> 331-337 req/s, web CPU 13.8-14.6 -> 8.8-9.1 ms per request, project GET p99 3.3-5.8 s -> 0.2-0.44 s,
worst 9-12 s -> 1.2 s, 0 errors. (One run right after the merge had 4 client timeouts at 60 s in its first second;
not reproduced in 3 more runs here or 6 at review.)
