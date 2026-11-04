"""TM import concurrency (ADR 0105): concurrent imports into one project lose nothing -- hermetic.

Throwaway LOC_TMS_DATA_DIR, blanked provider keys, zero network. Born from the S7c finding:
seven chunk uploads of one 2M-unit TMX became seven tm_import jobs running at once on Postgres
(4 web + 8 worker processes), four failed on ux_segmenttm_upsert_key and only 734,494 of 1,726,003
units were stored.

Covers (every check runs on SQLite; the Postgres leg repeats them against a real server):
  slip        -- a concurrent writer commits some of a chunk's NEW units after the chunk re-read
                 which hashes are present and before its INSERT. The import must not fail: the
                 slipped units take the late path (file target wins, counts move from imported to
                 updated or skipped), each unit is stored once.
  cas-miss    -- a writer changes a row between the chunk's re-read and its compare-and-set UPDATE.
                 The update is retried on the fresh target (the file's target wins, #584), never
                 silently dropped, and the counts stay exact.
  threads     -- four threads import overlapping chunks into one project at the same time
                 (serialization bypassed): no error, every unit stored exactly once, the summed
                 `imported` equals the distinct units, every call's imported+updated+skipped equals
                 its own unit count, each stored target is one a chunk carried.
  sql         -- the claim's candidate SELECT carries the serial_key exclusion on both dialects and
                 the Postgres claim still builds from it; serial_key_for names tm_import per project.
  index       -- the PARTIAL UNIQUE index (one running row per serial_key) exists and works: two
                 running rows of one key are refused, different keys and NULL keys are not.
  claim       -- a pending row whose key is running is not claimed (FIFO per key, other kinds pass);
                 an expired lease frees the key; the IntegrityError of a lost flip race is "nothing
                 this round" (the row stays pending, no lease is left), never a crash.
  memory      -- the in-memory queue enforces the same rule in-process.
  jobs        -- four tm_import jobs of overlapping chunks submitted to real `python -m app.worker`
                 processes: all COMPLETE, no two ran at once (started/finished intervals), every unit
                 stored once, the last submitted chunk's target wins every overlap, summed counts
                 are exact, a waiting job says so.
  orphan      -- a pending queue row whose Job row is gone
                 does not hold its key; the project-delete cascade removes the queue rows of the jobs
                 it deletes.
  order       -- per-key order is JOB id order: a requeued older import still runs first (DB and
                 memory queues).
  keytwin     -- serial_key_for and the SQL key written by enqueue agree for int, float, digit string
                 and unusable payload ids.
  delcount    -- a row deleted between the chunk's re-read and its compare-and-set does not leave its
                 skipped units counted.
  migration   -- (SQLite) revision e5a1c9d3b7f2 backfills serial_key of pending/running tm_import
                 queue rows (one running row per key keeps it) and creates both indexes.

The Postgres leg runs only when LOC_TMS_TEST_PG_URL names an empty scratch Postgres database
(skipped otherwise, like verify_pg_conflict_retry); it is run in a child process of this file
(TMRACE_LEG=pg) because the database engine is fixed at import. The child wipes the public schema of
that database first, so the URL must name a database whose name ENDS with "_test".

Final line: "TM IMPORT CONCURRENCY OK". ASCII-only.
"""

# ruff: noqa: E501 -- long assertion messages carry expected-vs-seen detail
import contextlib
import itertools
import os
import subprocess
import sys
import tempfile
import threading
import time
from datetime import datetime, timedelta

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(_ROOT)
sys.path.insert(0, _ROOT)

LEG = os.environ.get("TMRACE_LEG", "")
PG_URL = os.environ.get("LOC_TMS_TEST_PG_URL", "")

if LEG == "pg":
    # The child: point the app at the scratch Postgres BEFORE anything imports app.db, and wipe it.
    import sqlalchemy as _sa

    _url = os.environ["DATABASE_URL"]
    _name = _sa.engine.make_url(_url).database or ""
    assert _name.endswith("_test"), (
        f"refusing to wipe database {_name!r}: its name must END with '_test' "
        "(this leg drops the whole public schema)"
    )
    _eng = _sa.create_engine(_url, isolation_level="AUTOCOMMIT")
    with _eng.connect() as _cx:
        _cx.execute(_sa.text("DROP SCHEMA public CASCADE"))
        _cx.execute(_sa.text("CREATE SCHEMA public"))
    _eng.dispose()
else:
    os.environ.pop("DATABASE_URL", None)

os.environ["LOC_TMS_ROLE"] = "web"
from tests._bootstrap import init_env

DATA = init_env("loctms_tmrace_")

import sqlalchemy as sa
from sqlalchemy import event
from sqlmodel import Session, select

from app.db import engine, init_db
from app.models import Job, JobStatus, Project, SegmentTM, TargetLocale
from app.ports import jobqueue
from app.services import settings, tm, tm_import_job
from app.services.tm_common import hash_text

if LEG == "pg":
    # Postgres boots only an alembic-migrated database: build it the production way (this also
    # runs the serial_key revision on Postgres).
    from alembic.config import Config as _AlembicConfig

    from alembic import command as _alembic_command

    _cfg = _AlembicConfig(os.path.join(_ROOT, "alembic.ini"))
    _cfg.set_main_option("script_location", os.path.join(_ROOT, "alembic"))
    _alembic_command.upgrade(_cfg, "head")
init_db()
settings.set_many(
    {
        "security_auth_required": "0",
        "multi_org_enabled": "0",
        "project_scoping": "0",
        "api_rate_limit_per_minute": "0",
        "api_read_rate_limit_per_minute": "0",
        "job_queue_backend": "db",
    },
    actor="verify_tm_import_concurrency",
)
settings.invalidate_cache()

SRC, TGT = "en-US", "de-DE"
PG = engine.dialect.name == "postgresql"
FAILS: list[str] = []
WORKERS: list = []
_N = [0]
PY = sys.executable


def ok(msg):
    print("  OK " + msg, flush=True)


def check(fn):
    """Run one check; a failure is recorded and the run goes on, so one pre-fix run shows every
    part that fails."""
    name = fn.__name__
    try:
        fn()
    except Exception as exc:
        import traceback

        FAILS.append(name)
        tb = traceback.format_exc()
        print(f"  FAIL {name}: {type(exc).__name__}: {str(exc)[:400]}", flush=True)
        print("    " + tb.strip().splitlines()[-3].strip()[:200], flush=True)


# ---------------------------------------------------------------------------- fixtures
def project() -> int:
    _N[0] += 1
    with Session(engine) as s:
        p = Project(name=f"TmRace-{_N[0]}", source_locale=SRC, default_provider="mock")
        s.add(p)
        s.commit()
        s.refresh(p)
        s.add(TargetLocale(project_id=p.id, locale=TGT))
        s.commit()
        return p.id


def _tu(src, tgt):
    return (
        f'<tu><tuv xml:lang="{SRC}"><seg>{src}</seg></tuv>'
        f'<tuv xml:lang="{TGT}"><seg>{tgt}</seg></tuv></tu>'
    )


def tmx(units) -> bytes:
    return (
        '<?xml version="1.0" encoding="UTF-8"?><tmx version="1.4"><header srclang="en-US"/>'
        "<body>" + "".join(_tu(s, t) for s, t in units) + "</body></tmx>"
    ).encode()


def src_of(u: int) -> str:
    return f"Race sentence number {u} of the overlap set"


N_CHUNKS, CHUNK_UNITS, STEP = 4, 6000, 3500  # chunk i covers [i*STEP, i*STEP + CHUNK_UNITS)


def chunk_range(i: int) -> range:
    return range(i * STEP, i * STEP + CHUNK_UNITS)


def chunk_units(i: int):
    return [(src_of(u), f"Chunk{i} target {u}") for u in chunk_range(i)]


def distinct_units() -> int:
    return len({u for i in range(N_CHUNKS) for u in chunk_range(i)})


def expected_last_wins() -> dict:
    """{source text: target}: for each unit the target of the LAST chunk that carries it."""
    out = {}
    for i in range(N_CHUNKS):
        for u in chunk_range(i):
            out[src_of(u)] = f"Chunk{i} target {u}"
    return out


def stored(pid) -> list:
    with Session(engine) as s:
        return [
            (r.source_hash, r.source_text, r.target_text)
            for r in s.exec(select(SegmentTM).where(SegmentTM.project_id == pid))
        ]


def hashes_unique(rows) -> bool:
    hs = [r[0] for r in rows]
    return len(hs) == len(set(hs))


@contextlib.contextmanager
def small_chunks(n=500):
    old = (tm.TMX_WRITE_CHUNK_ROWS, tm.TMX_CHUNK_PAUSE_S)
    tm.TMX_WRITE_CHUNK_ROWS, tm.TMX_CHUNK_PAUSE_S = n, 0
    try:
        yield
    finally:
        tm.TMX_WRITE_CHUNK_ROWS, tm.TMX_CHUNK_PAUSE_S = old


# ---------------------------------------------------------------------------- (a) write path
_INSERT_SLIP = sa.text(
    "INSERT INTO segmenttm (project_id, source_locale, target_locale, source_text, target_text, "
    "source_hash, created_at, use_count, activity_at) "
    "VALUES (:p, :sl, :tl, :s, :t, :h, :now, 0, :now)"
)


def check_slip():
    """A concurrent writer's units commit between the chunk's re-read and its INSERT."""
    pid = project()
    units = [(f"Slip unit {i} text", f"File target {i}") for i in range(30)]
    state = {"armed": True, "busy": False}

    def _slip(conn, cursor, statement, parameters, context, executemany):
        if not state["armed"] or state["busy"]:
            return
        if not statement.lstrip().upper().startswith("INSERT INTO SEGMENTTM"):
            return
        state["busy"] = True
        try:
            state["armed"] = False
            now = datetime.utcnow()
            for i in range(15):  # 0-9: another target; 10-14: the same target as the file
                other = f"Other writer {i}" if i < 10 else f"File target {i}"
                conn.execute(
                    _INSERT_SLIP,
                    {
                        "p": pid,
                        "sl": SRC,
                        "tl": TGT,
                        "s": units[i][0],
                        "t": other,
                        "h": hash_text(units[i][0], SRC, TGT),
                        "now": now,
                    },
                )
        finally:
            state["busy"] = False

    event.listen(engine, "before_cursor_execute", _slip)
    try:
        with Session(engine) as s:
            res = tm.import_tmx(s, pid, tmx(units), SRC, TGT)
    finally:
        event.remove(engine, "before_cursor_execute", _slip)
    assert not state["armed"], "the slip never fired (no INSERT INTO segmenttm seen)"
    rows = stored(pid)
    assert len(rows) == 30 and hashes_unique(rows), f"{len(rows)} rows stored, want 30 once each"
    by_src = {r[1]: r[2] for r in rows}
    assert by_src == dict(units), "the file's target must win every slipped unit (#584 semantics)"
    got = (res["imported"], res["updated"], res["skipped"])
    assert got == (15, 10, 5), f"counts imported/updated/skipped {got}, want (15, 10, 5): {res}"
    ok("slip: units stored by a concurrent writer after the re-read take the late path, none lost")


def check_cas_miss():
    """A writer changes a row between the re-read and the compare-and-set UPDATE."""
    pid = project()
    old = [(f"Cas unit {i} text", f"Old target {i}") for i in range(20)]
    new = [(s, f"New target {i}") for i, (s, _t) in enumerate(old)]
    with Session(engine) as s:
        tm.import_tmx(s, pid, tmx(old), SRC, TGT)
    state = {"armed": True, "busy": False}

    def _third(conn, cursor, statement, parameters, context, executemany):
        if not state["armed"] or state["busy"]:
            return
        if not statement.lstrip().upper().startswith("UPDATE SEGMENTTM SET TARGET_TEXT"):
            return
        state["busy"] = True
        try:
            state["armed"] = False
            conn.execute(
                sa.text("UPDATE segmenttm SET target_text = 'Third writer' WHERE project_id = :p"),
                {"p": pid},
            )
        finally:
            state["busy"] = False

    event.listen(engine, "before_cursor_execute", _third)
    try:
        with Session(engine) as s:
            res = tm.import_tmx(s, pid, tmx(new), SRC, TGT)
    finally:
        event.remove(engine, "before_cursor_execute", _third)
    assert not state["armed"], "the third writer never fired (no CAS UPDATE seen)"
    rows = stored(pid)
    assert len(rows) == 20 and hashes_unique(rows), f"{len(rows)} rows, want 20"
    assert {r[1]: r[2] for r in rows} == dict(new), (
        "a compare-and-set that missed must be retried on the fresh target: "
        f"{sorted({r[2] for r in rows})[:3]}"
    )
    got = (res["imported"], res["updated"], res["skipped"])
    assert got == (0, 20, 0), f"counts {got}, want (0, 20, 0): {res}"
    ok("cas-miss: a row changed between re-read and update is retried, the file's target stored")


def check_threads():
    pid = project()
    barrier = threading.Barrier(N_CHUNKS)
    results, errors = {}, {}

    def run(i):
        try:
            with Session(engine) as s:
                data = tmx(chunk_units(i))
                barrier.wait(timeout=60)
                results[i] = tm.import_tmx(s, pid, data, SRC, TGT)
        except Exception as exc:
            errors[i] = f"{type(exc).__name__}: {str(exc)[:200]}"
            with contextlib.suppress(threading.BrokenBarrierError):
                barrier.abort()

    with small_chunks(500):
        ths = [threading.Thread(target=run, args=(i,)) for i in range(N_CHUNKS)]
        for t in ths:
            t.start()
        for t in ths:
            t.join(300)
    assert not errors, f"imports failed: {errors}"
    rows = stored(pid)
    want = distinct_units()
    assert len(rows) == want and hashes_unique(rows), (
        f"{len(rows)} rows stored, want {want} once each"
    )
    carried = {}
    for i in range(N_CHUNKS):
        for s_, t_ in chunk_units(i):
            carried.setdefault(s_, set()).add(t_)
    bad = [r[1] for r in rows if r[2] not in carried[r[1]]]
    assert not bad, f"{len(bad)} stored targets no chunk carried, e.g. {bad[:2]}"
    total_imported = sum(r["imported"] for r in results.values())
    assert total_imported == want, f"summed imported {total_imported}, want {want}: {results}"
    for i, r in results.items():
        kept = r["imported"] + r["updated"] + r["skipped"]
        assert kept == CHUNK_UNITS, (
            f"chunk {i}: imported+updated+skipped {kept}, want {CHUNK_UNITS}"
        )
    ok(f"threads: {N_CHUNKS} concurrent imports, {want} units once each, counts exact")


# ---------------------------------------------------------------------------- (b) queue
def _mk_job(kind="tm_import", pid=1, **extra) -> int:
    payload = {"project_id": pid, **extra} if kind == "tm_import" else dict(extra)
    with Session(engine) as s:
        j = Job(kind=kind, payload=payload, status=JobStatus.QUEUED)
        s.add(j)
        s.commit()
        s.refresh(j)
        return j.id


def _qrow(jid):
    with engine.connect() as cx:
        return cx.execute(
            sa.text(
                "SELECT state, worker, lease_expires_at, serial_key FROM jobqueue WHERE job_id=:j"
            ),
            {"j": jid},
        ).first()


def _wipe_queue():
    with engine.begin() as cx:
        cx.execute(sa.text("DELETE FROM jobqueue"))
        cx.execute(sa.text("DELETE FROM job"))


def check_sql():
    from app.services import jobs as _jobs

    for postgres in (False, True):
        sql, _extra = jobqueue._candidate_sql(("long", "short"), _jobs.LONG_KINDS, postgres)
        assert "serial_key" in sql and "NOT EXISTS" in sql.upper(), (
            f"postgres={postgres}: the candidate SELECT has no serial_key exclusion: {sql}"
        )
        if postgres:
            assert sql.rstrip().endswith("FOR UPDATE OF q SKIP LOCKED"), sql
            claim = jobqueue._pg_claim_sql(sql)
            assert "RETURNING job_id" in claim and "serial_key" in claim, claim
    assert jobqueue.serial_key_for("tm_import", {"project_id": 7}) == "tm_import:7"
    assert jobqueue.serial_key_for("tm_import", {"project_id": "7"}) == "tm_import:7"
    assert jobqueue.serial_key_for("tm_import", {}) is None
    assert jobqueue.serial_key_for("tm_import", None) is None
    assert jobqueue.serial_key_for("translate_document", {"project_id": 7}) is None
    ok("sql: the candidate SELECT carries the serial_key exclusion on both dialects")


def check_index():
    _wipe_queue()
    with engine.connect() as cx:
        if PG:
            rows = cx.execute(
                sa.text("SELECT indexname, indexdef FROM pg_indexes WHERE tablename='jobqueue'")
            ).fetchall()
        else:
            rows = cx.execute(
                sa.text(
                    "SELECT name, sql FROM sqlite_master WHERE type='index' AND tbl_name='jobqueue'"
                )
            ).fetchall()
    by = {r[0]: " ".join((r[1] or "").split()).lower() for r in rows}
    ux = by.get("ux_jobqueue_serial_running")
    assert ux is not None, f"ux_jobqueue_serial_running missing; indexes {sorted(by)}"
    assert "unique" in ux and "serial_key" in ux and "'running'" in ux, ux
    assert "ix_jobqueue_serial_active" in by, f"ix_jobqueue_serial_active missing; {sorted(by)}"
    j = [_mk_job(pid=100 + i) for i in range(6)]
    now = datetime.utcnow()

    def put(jid, state, key):
        with engine.begin() as cx:
            cx.execute(
                sa.text(
                    "INSERT INTO jobqueue (job_id, kind, payload, state, attempts, enqueued_at, "
                    "updated_at, serial_key) VALUES (:j, 'tm_import', '{}', :st, 0, :n, :n, :k)"
                ),
                {"j": jid, "st": state, "n": now, "k": key},
            )

    put(j[0], "running", "tm_import:1")
    put(j[1], "pending", "tm_import:1")  # pending twin of a running key: allowed
    put(j[2], "running", "tm_import:2")  # another key: allowed
    put(j[3], "running", None)
    put(j[4], "running", None)  # NULL keys never collide
    try:
        put(j[5], "running", "tm_import:1")
    except sa.exc.IntegrityError:
        pass
    else:
        raise AssertionError("two running rows of one serial_key must be refused")
    ok("index: one running row per serial_key (partial unique index), NULL keys and pending free")


def check_claim():
    _wipe_queue()
    q = jobqueue.DbJobQueue(lease_seconds=300, poll_seconds=0.05)
    a1, a2, b1, other = _mk_job(pid=1), _mk_job(pid=1), _mk_job(pid=2), _mk_job("data_export")
    for jid in (a1, a2, b1, other):
        q.enqueue(jid)
    assert _qrow(a1)[3] == "tm_import:1" and _qrow(other)[3] is None, (_qrow(a1), _qrow(other))
    first = q.claim("w1", timeout=1.0)
    assert first == a1, f"oldest row of the oldest key goes first, got {first}"
    got = [q.claim("w2", timeout=1.0), q.claim("w3", timeout=1.0)]
    assert sorted(got) == sorted([b1, other]), (
        f"project 1 is busy: only project 2's import and the other kind may run, got {got}"
    )
    assert q.claim("w4", timeout=0.3) is None, "the second import of project 1 must wait"
    assert _qrow(a2)[0] == "pending"
    # An expired lease frees the key: recovery returns the row to pending (id order kept).
    with engine.begin() as cx:
        cx.execute(
            sa.text("UPDATE jobqueue SET lease_expires_at=:x WHERE job_id=:j"),
            {"x": datetime.utcnow() - timedelta(seconds=5), "j": a1},
        )
    assert q.recover() >= 1
    assert q.claim("w5", timeout=1.0) == a1, "after lease expiry the OLDER import runs again first"
    assert q.claim("w6", timeout=0.3) is None
    # Finished: the waiting one runs.
    with engine.begin() as cx:
        cx.execute(sa.text("UPDATE job SET status='completed' WHERE id=:j"), {"j": a1})
    q.release(a1)
    assert q.claim("w7", timeout=1.0) == a2, "the key is free again: the waiting import is next"

    # The flip race: a claim whose candidate pick ignores the exclusion (two Postgres claimers
    # that both saw no running row) loses on the unique index and must report "nothing".
    _wipe_queue()
    c1, c2 = _mk_job(pid=9), _mk_job(pid=9)
    q.enqueue(c1)
    q.enqueue(c2)
    assert q.claim("r1", timeout=1.0) == c1
    real = jobqueue._candidate_sql

    def blind(pools, long_kinds, postgres):
        sql, extra = real(pools, long_kinds, postgres)
        return sql.replace("q.serial_key IS NULL OR NOT EXISTS", "1 = 1 OR NOT EXISTS"), extra

    jobqueue._candidate_sql = blind
    try:
        lost = q.claim("r2", timeout=0.5)
    finally:
        jobqueue._candidate_sql = real
    assert lost is None, f"a lost flip race must claim nothing, got {lost}"
    st = _qrow(c2)
    assert st[0] == "pending" and st[1] is None and st[2] is None, f"no lease may be left: {st}"
    q.release(c1)
    assert q.claim("r3", timeout=1.0) == c2
    ok("claim: FIFO per key, expired lease frees the key, a lost flip race claims nothing")


def check_memory():
    _wipe_queue()
    q = jobqueue.InMemoryJobQueue()
    a1, a2, b1, other = _mk_job(pid=1), _mk_job(pid=1), _mk_job(pid=2), _mk_job("data_export")
    for jid in (a1, a2, b1, other):
        q.enqueue(jid)
    got = [q.claim("m", timeout=0.5) for _ in range(3)]
    assert got == [a1, b1, other], f"project 1's second import must be skipped over, got {got}"
    assert q.claim("m", timeout=0.2) is None
    q.release(a1)
    assert q.claim("m", timeout=0.5) == a2
    ok("memory: the in-memory queue serializes one project's imports in submission order")


def check_orphan_and_cascade():
    _wipe_queue()
    q = jobqueue.DbJobQueue(lease_seconds=300, poll_seconds=0.05)
    old, new = _mk_job(pid=5), _mk_job(pid=5)
    q.enqueue(old)
    q.enqueue(new)
    assert q.claim("o0", timeout=0.3) == old  # sanity: the older one goes first
    q.hand_back([old])  # back to pending, still the older of the key
    with engine.begin() as cx:
        cx.execute(sa.text("DELETE FROM job WHERE id=:j"), {"j": old})
    got = q.claim("o1", timeout=1.0)
    assert got == new, f"an orphaned pending row (its Job deleted) must not block its key: {got}"
    # the project-delete cascade removes the queue rows of the jobs it deletes
    _wipe_queue()
    from app.services import content_projects

    pid = project()
    j1, j2 = _mk_job(pid=pid), _mk_job(pid=pid)
    keep = _mk_job(pid=pid + 100000)
    for jid in (j1, j2, keep):
        q.enqueue(jid)
    with Session(engine) as s:
        content_projects.delete_project(s, pid, actor="verify")
        s.commit()
    with engine.connect() as cx:
        left = {r[0] for r in cx.execute(sa.text("SELECT job_id FROM jobqueue"))}
    assert left == {keep}, f"project delete must remove its jobs' queue rows, left {left}"
    ok("orphan: a queue row without its Job holds no key; project delete removes its queue rows")


def check_finished_running_row_stalls_nothing():
    """Gate round 2 M2: a RUNNING queue row whose Job already finished (the window between the
    terminal write and release, longer if release fails) keeps blocking ITS key, so the next
    row of that key is not the head candidate failing its flip every round, and another
    project's import is still claimed."""
    _wipe_queue()
    q = jobqueue.DbJobQueue(lease_seconds=300, poll_seconds=0.05)
    a, b = _mk_job(pid=21), _mk_job(pid=21)
    c = _mk_job(pid=22)
    for jid in (a, b, c):
        q.enqueue(jid)
    assert q.claim("f1", timeout=1.0) == a
    with engine.begin() as cx:
        cx.execute(
            sa.text("UPDATE job SET status=:s WHERE id=:j"), {"s": JobStatus.COMPLETED, "j": a}
        )
    got = q.claim("f2", timeout=1.0)
    assert got == c, f"another project's import must still be claimed, got {got}"
    assert q.claim("f3", timeout=0.3) is None, "the key's next import waits for release"
    q.release(a)
    assert q.claim("f4", timeout=1.0) == b
    ok("finished-but-running row: blocks only its own key, other projects keep flowing")


def check_order_after_requeue():
    _wipe_queue()
    q = jobqueue.DbJobQueue(lease_seconds=300, poll_seconds=0.05)
    a, b = _mk_job(pid=3), _mk_job(pid=3)
    q.enqueue(a)
    q.enqueue(b)
    assert q.claim("q1", timeout=1.0) == a
    q.requeue(a)  # a fresh pending row with a HIGHER row id than b's
    got = q.claim("q2", timeout=1.0)
    assert got == a, f"a requeued older import must still run before the later one, got {got}"
    q.release(a)
    assert q.claim("q3", timeout=1.0) == b
    mq = jobqueue.InMemoryJobQueue()
    mq.enqueue(a)
    mq.enqueue(b)
    assert mq.claim("m1", timeout=0.3) == a
    mq.requeue(a)
    got = mq.claim("m2", timeout=0.3)
    assert got == a, f"memory queue: the requeued older import must run first, got {got}"
    ok("order: per-key order follows the job id, also after a requeue (DB and memory)")


def check_key_twin():
    _wipe_queue()
    payloads = [
        {"project_id": 7},
        {"project_id": 7.0},
        {"project_id": 7.9},
        {"project_id": -3.5},
        {"project_id": 1000.0},
        {"project_id": "7"},
        {"project_id": "007"},
        {"project_id": "7.0"},
        {"project_id": "abc"},
        {"project_id": True},
        {"project_id": None},
        {"project_id": 1e30},
        {"project_id": []},
        {},
    ]
    sql = jobqueue._serial_key_sql(PG)
    for pl in payloads:
        jid = _mk_job(pid=0)
        with Session(engine) as s:
            job = s.get(Job, jid)
            job.payload = dict(pl)
            s.add(job)
            s.commit()
        with engine.connect() as cx:
            got = cx.execute(sa.text(f"SELECT {sql} FROM job WHERE id=:j"), {"j": jid}).scalar()
        want = jobqueue.serial_key_for("tm_import", pl)
        assert got == want, f"payload {pl!r}: SQL key {got!r}, python key {want!r}"
    assert jobqueue.serial_key_for("tm_import", {"project_id": 7.0}) == "tm_import:7"
    ok("keytwin: the SQL key and serial_key_for agree for int, float, string and bad ids")


def check_deleted_row_counts():
    """A row deleted between the chunk's re-read and its compare-and-set leaves no skipped unit."""
    pid = project()
    s_ = "Deleted unit one text"
    with Session(engine) as s:
        tm.import_tmx(s, pid, tmx([(s_, "T0")]), SRC, TGT)
    state = {"armed": True, "busy": False}

    def _gone(conn, cursor, statement, parameters, context, executemany):
        if not state["armed"] or state["busy"]:
            return
        if not statement.lstrip().upper().startswith("UPDATE SEGMENTTM SET TARGET_TEXT"):
            return
        state["busy"] = True
        try:
            state["armed"] = False
            conn.execute(sa.text("DELETE FROM segmenttm WHERE project_id = :p"), {"p": pid})
        finally:
            state["busy"] = False

    event.listen(engine, "before_cursor_execute", _gone)
    try:
        with Session(engine) as s:
            # the same source twice: the first unit matches the stored target (skipped), the
            # second changes it (updated); then the row vanishes before the update lands
            res = tm.import_tmx(s, pid, tmx([(s_, "T0"), (s_, "T1")]), SRC, TGT)
    finally:
        event.remove(engine, "before_cursor_execute", _gone)
    assert not state["armed"], "the delete never fired (no CAS UPDATE seen)"
    got = (res["imported"], res["updated"], res["skipped"], res.get("concurrent_deleted"))
    assert got == (0, 0, 0, 1), f"counts imported/updated/skipped/deleted {got}, want (0, 0, 0, 1)"
    ok("delcount: a row deleted before the compare-and-set leaves no skipped unit counted")


# ---------------------------------------------------------------------------- (b) real workers
class Worker:
    def __init__(self, extra_env=None):
        with tempfile.NamedTemporaryFile(
            "w+b", prefix="tmr_worker_", suffix=".log", delete=False
        ) as lf:
            self.logname = lf.name
        env = dict(os.environ, LOC_TMS_ROLE="worker", LOC_TMS_DATA_DIR=DATA, LOG_FORMAT="json")
        env["LOC_TMS_JOB_WORKERS"] = "3"
        env.pop("TMRACE_LEG", None)
        env.update(extra_env or {})
        self.log = open(self.logname, "wb")  # noqa: SIM115 -- the child owns it until it exits
        self.proc = subprocess.Popen(
            [PY, "-m", "app.worker"], cwd=_ROOT, env=env, stdout=self.log, stderr=self.log
        )
        self.pid = self.proc.pid
        self.wpid = None
        WORKERS.append(self)

    def text(self):
        try:
            with open(self.logname, "rb") as f:
                return f.read().decode("utf-8", "replace")
        except OSError:
            return ""

    def wait_up(self, timeout=120):
        import re

        end = time.time() + timeout
        pat = re.compile(r"worker (\d+) up")
        while time.time() < end:
            if self.proc.poll() is not None:
                raise AssertionError(
                    f"worker exited {self.proc.returncode}: {self.text()[-1500:]!r}"
                )
            m = pat.search(self.text())
            if m:
                self.wpid = int(m.group(1))
                return self
            time.sleep(0.2)
        raise AssertionError(f"worker not up in {timeout}s: {self.text()[-1500:]!r}")

    def kill(self):
        for pid in (self.wpid, self.pid):
            if pid:
                if os.name == "nt":
                    subprocess.run(
                        ["taskkill", "/F", "/T", "/PID", str(pid)], capture_output=True, timeout=30
                    )
                else:
                    with contextlib.suppress(OSError):
                        os.kill(pid, 9)
        with contextlib.suppress(Exception):
            self.proc.wait(10)
        with contextlib.suppress(Exception):
            self.log.close()


def stop_workers():
    for w in list(WORKERS):
        with contextlib.suppress(Exception):
            w.kill()
    WORKERS.clear()


def check_jobs():
    import io

    _wipe_queue()
    pid = project()
    for _ in range(2):
        Worker().wait_up()
    ids = []
    msgs = {}
    for i in range(N_CHUNKS):
        data = tmx(chunk_units(i))
        token, size = tm_import_job.stage_upload(b"", io.BytesIO(data))
        jid = tm_import_job.submit(
            pid, token, SRC, TGT, filename=f"chunk{i}.tmx", size=size, actor="tmrace"
        )
        ids.append(jid)
        with Session(engine) as s:
            msgs[jid] = s.get(Job, jid).message or ""
    end = time.time() + 420
    rows = {}
    while time.time() < end:
        with Session(engine) as s:
            rows = {j: s.get(Job, j) for j in ids}
            for j in rows.values():
                s.expunge(j)
        if all(r.status in (JobStatus.COMPLETED, JobStatus.FAILED) for r in rows.values()):
            break
        time.sleep(0.5)
    tails = " | ".join(w.text()[-500:] for w in WORKERS)
    for j in ids:
        r = rows[j]
        assert r.status == JobStatus.COMPLETED, (
            f"job {j} ended {r.status}: {r.error!r} message {r.message!r}; worker tails {tails!r}"
        )
    # No two ran at once.
    spans = sorted((rows[j].started_at, rows[j].finished_at, j) for j in ids)
    for (s0, e0, a), (s1, _e1, b) in itertools.pairwise(spans):
        assert e0 is not None and s1 >= e0, (
            f"jobs {a} ({s0} to {e0}) and {b} (from {s1}) overlapped: imports of one project must not run at once"
        )
    # FIFO: they ran in submission order.
    order = [j for _s, _e, j in spans]
    assert order == ids, f"run order {order}, want submission order {ids}"
    rows_t = stored(pid)
    want = distinct_units()
    assert len(rows_t) == want and hashes_unique(rows_t), f"{len(rows_t)} rows stored, want {want}"
    exp = expected_last_wins()
    wrong = [r[1] for r in rows_t if exp[r[1]] != r[2]]
    assert not wrong, (
        f"{len(wrong)} units do not carry the last submitted chunk's target, e.g. {wrong[:2]}"
    )
    res = [rows[j].result or {} for j in ids]
    imported = sum(r.get("imported", 0) for r in res)
    assert imported == want, (
        f"summed imported {imported}, want {want}: {[r.get('imported') for r in res]}"
    )
    for k, r in enumerate(res):
        kept = r.get("imported", 0) + r.get("updated", 0) + r.get("skipped", 0)
        assert kept == CHUNK_UNITS, f"job {k}: imported+updated+skipped {kept}, want {CHUNK_UNITS}"
    waiting = [j for j in ids[1:] if "another import" in msgs[j].lower()]
    assert len(waiting) >= 2, (
        f"jobs submitted behind a running import should say so; messages {msgs}"
    )
    stop_workers()
    ok(
        f"jobs: {N_CHUNKS} tm_import jobs on 2 worker processes ran one at a time, in order, {want} units"
    )


# ---------------------------------------------------------------------------- migration
def check_migration():
    if PG:
        return
    import json
    import sqlite3

    from alembic.config import Config

    from alembic import command

    work = tempfile.mkdtemp(prefix="loctms_tmrace_mig_")
    cfg = Config(os.path.join(_ROOT, "alembic.ini"))
    cfg.set_main_option("script_location", os.path.join(_ROOT, "alembic"))
    path = os.path.join(work, "loc_tms.db")
    cfg.set_main_option("sqlalchemy.url", "sqlite:///" + path.replace("\\", "/"))
    prev = os.environ.get("LOC_TMS_DATA_DIR")
    os.environ["LOC_TMS_DATA_DIR"] = work
    try:
        command.upgrade(cfg, "d6b3f8a2c4e1")
        cx = sqlite3.connect(path)
        cols = {r[1] for r in cx.execute("PRAGMA table_info(jobqueue)")}
        assert "serial_key" not in cols, "precondition: the parent revision has no serial_key"
        now = "2026-10-24 10:00:00.000000"
        rows = [
            (1, "tm_import", {"project_id": 5}, "pending"),
            (2, "tm_import", {"project_id": 5}, "running"),
            (3, "tm_import", {"project_id": 6}, "running"),
            (4, "tm_import", {"project_id": 6}, "running"),  # a second RUNNING twin (pre-fix state)
            (5, "tm_import", {"project_id": 5}, "done"),
            (6, "translate_document", {"project_id": 5}, "pending"),
            (7, "tm_import", {}, "pending"),
        ]
        for jid, kind, payload, state in rows:
            cx.execute(
                "INSERT INTO jobqueue (job_id, kind, payload, state, attempts, enqueued_at, "
                "updated_at) VALUES (?, ?, ?, ?, 0, ?, ?)",
                (jid, kind, json.dumps(payload), state, now, now),
            )
        cx.commit()
        cx.close()
        command.upgrade(cfg, "head")
        cx = sqlite3.connect(path)
        got = dict(cx.execute("SELECT job_id, serial_key FROM jobqueue"))
        names = {r[0] for r in cx.execute("SELECT name FROM sqlite_master WHERE type='index'")}
        cx.close()
    finally:
        if prev is None:
            os.environ.pop("LOC_TMS_DATA_DIR", None)
        else:
            os.environ["LOC_TMS_DATA_DIR"] = prev
    assert got[1] == "tm_import:5" and got[2] == "tm_import:5", got
    assert got[3] == "tm_import:6" and got[4] is None, f"one running twin keeps the key: {got}"
    assert got[5] is None and got[6] is None and got[7] is None, got
    assert {"ux_jobqueue_serial_running", "ix_jobqueue_serial_active"} <= names, names
    ok(
        "migration: pending/running tm_import queue rows get their serial_key, a running twin is left free"
    )


# ---------------------------------------------------------------------------- main
def run_all():
    check(check_slip)
    check(check_cas_miss)
    check(check_threads)
    check(check_sql)
    check(check_index)
    check(check_claim)
    check(check_memory)
    check(check_orphan_and_cascade)
    check(check_finished_running_row_stalls_nothing)
    check(check_order_after_requeue)
    check(check_key_twin)
    check(check_deleted_row_counts)
    check(check_migration)
    check(check_jobs)  # last: it starts worker processes
    stop_workers()


def main() -> int:
    leg = "postgres" if PG else "sqlite"
    print(f"-- TM import concurrency: {leg} leg", flush=True)
    run_all()
    if FAILS:
        print(f"TM IMPORT CONCURRENCY ({leg}) FAILED: {', '.join(FAILS)}", flush=True)
        return 1
    print(f"TM IMPORT CONCURRENCY ({leg}) leg ok", flush=True)
    return 0


def run_pg_child() -> int:
    env = dict(os.environ, TMRACE_LEG="pg")
    env["DATABASE_URL"] = PG_URL
    proc = subprocess.run([PY, os.path.abspath(__file__)], cwd=_ROOT, env=env, timeout=1500)
    return proc.returncode


if __name__ == "__main__":
    try:
        rc = main()
    finally:
        stop_workers()
    if LEG != "pg":
        if PG_URL:
            print("-- postgres leg (child process)", flush=True)
            pg_rc = run_pg_child()
            rc = rc or pg_rc
        else:
            print("  postgres: SKIP (set LOC_TMS_TEST_PG_URL to an empty scratch database)")
    if rc == 0 and LEG != "pg":
        print("TM IMPORT CONCURRENCY OK")
    sys.exit(rc)
