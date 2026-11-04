"""TARGET T4: Postgres write conflicts (deadlocks, serialization failures) are retried, and multi-row writes take
their row locks in a fixed order so they do not deadlock in the first place.

Why (2026-10-22): a robust Postgres app wraps writes in a conflict retry (re-run the whole
transaction on SQLSTATE 40P01 deadlock or 40001 serialization failure, 3 tries, 20-80 ms jitter) and locks
the job rows in id order before writing many translations, so a bulk write queues behind a step change instead of
deadlocking with it). Our retry law (app.db.is_locked_error) only knows SQLite's "database is locked"; on Postgres
a deadlock surfaces as a 500 to the user or a failed job.

Done when every check passes:
  1. app.db.is_retryable_conflict(exc) is True for Postgres 40P01 and 40001 (as SQLAlchemy wraps them) and for
     SQLite "database is locked", and False for an integrity error.
  2. app.db.retry_conflicts(fn, action) re-runs fn after a conflict, returns its result, and gives up after 3 tries.
  3. app.db.lock_rows_in_order(session, Model, ids) exists (SELECT ... FOR UPDATE ORDER BY id on Postgres, a safe
     no-op on SQLite).
  4. (Postgres only, when LOC_TMS_TARGET_PG_URL points at an empty scratch database) two transactions that update
     the same rows in opposite order both commit when written through lock_rows_in_order + retry_conflicts.
"""

import os
import sys
import threading

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.chdir(_ROOT)
sys.path.insert(0, _ROOT)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from tests._bootstrap import init_env

init_env("loctms_t4_")

from _targets import check, finish, need  # noqa: E402
from sqlalchemy import exc as sa_exc  # noqa: E402

from app import db  # noqa: E402


def _pg_error(cls_name):
    import psycopg.errors as pe

    orig = getattr(pe, cls_name)()
    return sa_exc.OperationalError("UPDATE segment SET ...", {}, orig)


def t_classify():
    f = need(db, "is_retryable_conflict")
    dead = _pg_error("DeadlockDetected")
    ser = _pg_error("SerializationFailure")
    locked = sa_exc.OperationalError("UPDATE", {}, Exception("database is locked"))
    integ = sa_exc.IntegrityError("INSERT", {}, Exception("UNIQUE constraint failed"))
    got = (f(dead), f(ser), f(locked), f(integ))
    return got == (True, True, True, False), f"(deadlock, serialization, sqlite-locked, integrity) -> {got}"


def t_retry():
    retry = need(db, "retry_conflicts")
    calls = {"n": 0}

    def flaky():
        calls["n"] += 1
        if calls["n"] < 3:
            raise _pg_error("DeadlockDetected")
        return "done"

    out = retry(flaky, "target")
    always = {"n": 0}

    def never():
        always["n"] += 1
        raise _pg_error("SerializationFailure")

    gave_up = False
    try:
        retry(never, "target")
    except sa_exc.OperationalError:
        gave_up = True
    return (out == "done" and calls["n"] == 3 and gave_up and always["n"] == 3), \
        f"result={out!r} tries={calls['n']}; gave_up={gave_up} after {always['n']} tries"


def t_lock_order():
    need(db, "lock_rows_in_order")
    return True, "present"


def t_pg_opposite_order():
    url = os.environ["LOC_TMS_TARGET_PG_URL"]
    from sqlalchemy import Column, Integer, MetaData, Table, create_engine, select, update

    eng = create_engine(url, pool_size=4)
    md = MetaData()
    t = Table("t4_rows", md, Column("id", Integer, primary_key=True), Column("v", Integer))
    md.drop_all(eng)
    md.create_all(eng)
    with eng.begin() as cx:
        cx.execute(t.insert(), [{"id": 1, "v": 0}, {"id": 2, "v": 0}])
    lock = need(db, "lock_rows_in_order")
    retry = need(db, "retry_conflicts")
    barrier = threading.Barrier(2)
    errors = []

    def writer(order):
        def tx():
            from sqlmodel import Session

            with Session(eng) as s:
                lock(s, t, [1, 2])  # takes both locks in id order whatever order we write in
                for i in order:
                    s.exec(update(t).where(t.c.id == i).values(v=t.c.v + 1))
                    try:
                        barrier.wait(timeout=2)
                    except threading.BrokenBarrierError:
                        pass
                s.commit()

        try:
            retry(tx, "t4")
        except Exception as e:  # noqa: BLE001
            errors.append(repr(e)[:200])

    a = threading.Thread(target=writer, args=([1, 2],))
    b = threading.Thread(target=writer, args=([2, 1],))
    a.start()
    b.start()
    a.join(30)
    b.join(30)
    with eng.connect() as cx:
        vals = [r.v for r in cx.execute(select(t).order_by(t.c.id))]
    md.drop_all(eng)
    return (not errors and vals == [2, 2]), f"values={vals} errors={errors}"


check("1 conflict classification covers Postgres SQLSTATEs", t_classify)
check("2 retry_conflicts re-runs and gives up after 3", t_retry)
check("3 ordered row-lock helper exists", t_lock_order)
if os.environ.get("LOC_TMS_TARGET_PG_URL"):
    check("4 opposite-order writers both commit on Postgres", t_pg_opposite_order)
else:
    print("  [SKIP] 4 opposite-order writers on Postgres (set LOC_TMS_TARGET_PG_URL to a scratch database)")
finish("T4", "postgres conflict retry")
