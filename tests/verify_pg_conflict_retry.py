"""Postgres conflict retry and ordered row locks (engine redesign S3, target T4) -- hermetic.

Throwaway LOC_TMS_DATA_DIR, blanked provider keys, zero network. Promotes
tests/arch_targets/target_t4_pg_conflict_retry.py into the release gate.

Covers:
  classify   -- app.db.is_retryable_conflict is True for Postgres 40P01 (deadlock) and 40001
                (serialization failure) as SQLAlchemy wraps psycopg errors, and for SQLite
                "database is locked"; False for an IntegrityError, a non-lock OperationalError
                and a plain exception; sqlite_locks=False turns the SQLite match off only.
  retry      -- retry_conflicts re-runs the whole function, returns its result, gives up after
                3 tries re-raising the last conflict unchanged, and lets a non-conflict error
                through on the first try (no retry).
  lock-noop  -- lock_rows_in_order runs NO SQL on SQLite and returns [].
  tmx-chunk  -- the TMX import's chunk loop retries a Postgres conflict raised inside its
                savepoint (the chunk then commits once, counts unchanged), and does NOT retry a
                SQLite "database is locked" there (SQLite behavior unchanged: it propagates).
  postgres   -- ONLY when LOC_TMS_TEST_PG_URL names an empty scratch Postgres database
                (skipped otherwise): two writers updating the same rows in opposite order
                deadlock without help and both commit through retry_conflicts; with
                lock_rows_in_order first they both commit with no deadlock at all; and the
                lock helper really is SELECT ... FOR UPDATE in id order (a second locker waits).
"""

import contextlib
import os
import sys
import threading
import time

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(_ROOT)
sys.path.insert(0, _ROOT)
from tests._bootstrap import init_env

init_env("loctms_pgretry_")

from sqlalchemy import event
from sqlalchemy import exc as sa_exc
from sqlmodel import Session, select

from app import db
from app.db import init_db
from app.models import Project, SegmentTM

PG_URL = os.environ.get("LOC_TMS_TEST_PG_URL") or os.environ.get("LOC_TMS_TARGET_PG_URL") or ""


def _pg_error(cls_name):
    import psycopg.errors as pe

    return sa_exc.OperationalError("UPDATE segment SET ...", {}, getattr(pe, cls_name)())


def _locked():
    return sa_exc.OperationalError("UPDATE", {}, Exception("database is locked"))


def test_classify():
    f = db.is_retryable_conflict
    assert f(_pg_error("DeadlockDetected")) is True
    assert f(_pg_error("SerializationFailure")) is True
    assert f(_locked()) is True
    assert f(sa_exc.IntegrityError("INSERT", {}, Exception("UNIQUE constraint failed"))) is False
    assert f(sa_exc.OperationalError("SELECT", {}, Exception("no such table: x"))) is False
    assert f(ValueError("nope")) is False
    # A unique violation on Postgres (23505) is a real answer, never a retry.
    import psycopg.errors as pe

    assert f(sa_exc.IntegrityError("INSERT", {}, pe.UniqueViolation())) is False
    assert f(_locked(), sqlite_locks=False) is False
    assert f(_pg_error("DeadlockDetected"), sqlite_locks=False) is True
    print("  classify: 40P01/40001/sqlite-locked retry; integrity and others do not OK")


def test_retry():
    calls = {"n": 0}

    def flaky():
        calls["n"] += 1
        if calls["n"] < 3:
            raise _pg_error("DeadlockDetected")
        return "done"

    assert db.retry_conflicts(flaky, "suite") == "done" and calls["n"] == 3

    always = {"n": 0}

    def never():
        always["n"] += 1
        raise _pg_error("SerializationFailure")

    try:
        db.retry_conflicts(never, "suite")
        raise AssertionError("retry_conflicts must give up")
    except sa_exc.OperationalError as e:
        assert db._pg_sqlstate(e) == "40001"
    assert always["n"] == 3, always

    other = {"n": 0}

    def broken():
        other["n"] += 1
        raise sa_exc.IntegrityError("INSERT", {}, Exception("UNIQUE constraint failed"))

    try:
        db.retry_conflicts(broken, "suite")
        raise AssertionError("an integrity error must propagate")
    except sa_exc.IntegrityError:
        pass
    assert other["n"] == 1, other

    locked = {"n": 0}

    def sqlite_busy():
        locked["n"] += 1
        raise _locked()

    try:
        db.retry_conflicts(sqlite_busy, "suite", sqlite_locks=False)
        raise AssertionError("sqlite_locks=False must not retry a SQLite lock")
    except sa_exc.OperationalError:
        pass
    assert locked["n"] == 1, locked
    t0 = time.monotonic()
    calls["n"] = 0
    db.retry_conflicts(flaky, "suite")
    took = time.monotonic() - t0
    assert 0.03 <= took < 1.0, f"two jittered pauses of 20-80 ms each, took {took:.3f}s"
    print("  retry: re-runs, gives up after 3, passes other errors through at once OK")


def test_lock_noop_on_sqlite():
    seen = []

    def spy(conn, cursor, statement, *a):
        seen.append(statement)

    event.listen(db.engine, "before_cursor_execute", spy)
    try:
        with Session(db.engine) as s:
            assert db.lock_rows_in_order(s, SegmentTM, [3, 1, 2]) == []
            assert db.lock_rows_in_order(s, SegmentTM.__table__, []) == []
    finally:
        event.remove(db.engine, "before_cursor_execute", spy)
    assert seen == [], seen
    print("  lock-noop: lock_rows_in_order runs no SQL on SQLite OK")


def _tmx(units):
    body = "".join(
        f'<tu><tuv xml:lang="en-US"><seg>{s}</seg></tuv><tuv xml:lang="fr-FR"><seg>{t}</seg>'
        "</tuv></tu>"
        for s, t in units
    )
    return (
        '<?xml version="1.0" encoding="UTF-8"?><tmx version="1.4"><header srclang="en-US"/>'
        f"<body>{body}</body></tmx>"
    ).encode()


def test_tmx_chunk_retry():
    from app.services import tm

    with Session(db.engine) as s:
        p = Project(name="S3 retry", source_locale="en-US")
        s.add(p)
        s.commit()
        pid = p.id
    real = db.lock_rows_in_order
    fired = {"n": 0}

    def deadlock_once(session, model, ids):
        fired["n"] += 1
        if fired["n"] == 1:
            raise _pg_error("DeadlockDetected")
        return real(session, model, ids)

    db.lock_rows_in_order = deadlock_once
    try:
        with Session(db.engine) as s:
            out = tm.import_tmx(
                s, pid, _tmx([("Hello", "Bonjour"), ("Bye", "Au revoir")]), "en-US", "fr-FR"
            )
    finally:
        db.lock_rows_in_order = real
    assert fired["n"] == 2, fired
    assert out["imported"] == 2 and out["write_chunks"] == 1, out
    with Session(db.engine) as s:
        rows = s.exec(select(SegmentTM).where(SegmentTM.project_id == pid)).all()
    assert sorted(r.target_text for r in rows) == ["Au revoir", "Bonjour"], rows

    def busy(session, model, ids):
        raise _locked()

    db.lock_rows_in_order = busy
    try:
        with Session(db.engine) as s:
            try:
                tm.import_tmx(s, pid, _tmx([("New one", "Nouveau")]), "en-US", "fr-FR")
                raise AssertionError("a SQLite lock inside the chunk must propagate, not retry")
            except sa_exc.OperationalError as e:
                assert db.is_locked_error(e)
    finally:
        db.lock_rows_in_order = real
    with Session(db.engine) as s:
        n = len(s.exec(select(SegmentTM).where(SegmentTM.project_id == pid)).all())
    assert n == 2, f"the failed SQLite chunk must persist nothing, found {n} rows"
    print("  tmx-chunk: a Postgres conflict re-runs the chunk; a SQLite lock is not retried OK")


# ---------------------------------------------------------------------------------------------
# Postgres (LOC_TMS_TEST_PG_URL only)
# ---------------------------------------------------------------------------------------------
def _pg_table():
    from sqlalchemy import Column, Integer, MetaData, Table, create_engine

    eng = create_engine(PG_URL, pool_size=4)
    md = MetaData()
    t = Table("s3_conflict_rows", md, Column("id", Integer, primary_key=True), Column("v", Integer))
    md.drop_all(eng)
    md.create_all(eng)
    with eng.begin() as cx:
        cx.execute(t.insert(), [{"id": 1, "v": 0}, {"id": 2, "v": 0}])
    return eng, md, t


def _opposite_writers(eng, t, use_lock):
    from sqlalchemy import update

    barrier = threading.Barrier(2)
    errors, attempts = [], {"n": 0}

    def writer(order):
        def tx():
            attempts["n"] += 1
            with Session(eng) as s:
                if use_lock:
                    db.lock_rows_in_order(s, t, [2, 1])
                for i in order:
                    s.exec(update(t).where(t.c.id == i).values(v=t.c.v + 1))
                    with contextlib.suppress(threading.BrokenBarrierError):
                        barrier.wait(timeout=3)
                s.commit()

        try:
            db.retry_conflicts(tx, "suite")
        except Exception as e:
            errors.append(repr(e)[:200])

    threads = [threading.Thread(target=writer, args=(o,)) for o in ([1, 2], [2, 1])]
    for th in threads:
        th.start()
    for th in threads:
        th.join(60)
    with eng.connect() as cx:
        vals = [r.v for r in cx.execute(select(t).order_by(t.c.id))]
    return vals, errors, attempts["n"]


def test_pg_conflicts():
    from sqlalchemy import update

    eng, md, t = _pg_table()
    try:
        vals, errors, n = _opposite_writers(eng, t, use_lock=False)
        assert not errors and vals == [2, 2], (vals, errors)
        assert n >= 3, f"without the lock one writer must have deadlocked and re-run ({n} runs)"
        with eng.begin() as cx:
            cx.execute(update(t).values(v=0))
        vals, errors, n = _opposite_writers(eng, t, use_lock=True)
        assert not errors and vals == [2, 2] and n == 2, (vals, errors, n)

        # The lock is real: a second locker waits until the first commits.
        order = []
        holding = threading.Event()

        def first():
            with Session(eng) as s:
                assert db.lock_rows_in_order(s, t, [2, 1, 2]) == [1, 2]
                holding.set()
                time.sleep(0.5)
                order.append("first-commit")
                s.commit()

        def second():
            holding.wait(5)
            with Session(eng) as s:
                db.lock_rows_in_order(s, t, [1])
                order.append("second-locked")
                s.commit()

        a, b = threading.Thread(target=first), threading.Thread(target=second)
        a.start()
        b.start()
        a.join(10)
        b.join(10)
        assert order == ["first-commit", "second-locked"], order
    finally:
        md.drop_all(eng)
        eng.dispose()
    print("  postgres: deadlock retried; ordered locks avoid it; FOR UPDATE really waits OK")


if __name__ == "__main__":
    init_db()
    test_classify()
    test_retry()
    test_lock_noop_on_sqlite()
    test_tmx_chunk_retry()
    if PG_URL:
        test_pg_conflicts()
    else:
        print("  postgres: SKIP (set LOC_TMS_TEST_PG_URL to an empty scratch database)")
    print("PG CONFLICT RETRY OK")
