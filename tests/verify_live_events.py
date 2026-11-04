"""Cross-process live events (app/services/events.py, engine redesign S3, target T7) -- hermetic.

Throwaway LOC_TMS_DATA_DIR, blanked provider keys, zero network. Promotes
tests/arch_targets/target_t7_cross_process_events.py into the release gate (ADR 0110).

Covers, on SQLite (always) and on Postgres (only when LOC_TMS_TEST_PG_URL names an empty
scratch database; the Postgres half runs in a child process with DATABASE_URL pointed there,
creates only the liveevent table and drops it again; skipped cleanly otherwise):
  cross-process -- an event published by ANOTHER process reaches a subscriber here within 2 s
                   of being published (measured from the publisher's own timestamp, so the
                   child's start-up time does not count).
  burst         -- 200 events from another process all arrive, in order, none duplicated, and
                   a second subscriber on the same channel gets the same 200.
  isolation     -- a subscriber only sees its own channel, and nothing published before it
                   subscribed.
  transactional -- publish(session=...) is delivered only when that transaction commits, and
                   never when it rolls back.
  oversized     -- a payload over the NOTIFY limit arrives intact (Postgres: through the table,
                   the NOTIFY carries the id).
  lifecycle     -- poll(timeout) returns [] on silence; close() ends iteration.
  sweep         -- (SQLite) the retention sweep deletes old rows and ids keep increasing after
                   the newest row was swept (AUTOINCREMENT), so a cursor never goes backwards.
"""

import os
import subprocess
import sys
import threading
import time

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(_ROOT)
sys.path.insert(0, _ROOT)
PG_CHILD = len(sys.argv) > 1 and sys.argv[1] == "--pg-child"
PG_URL = os.environ.get("LOC_TMS_TEST_PG_URL") or ""
if not PG_CHILD:
    os.environ.pop("DATABASE_URL", None)  # the SQLite half is always SQLite
from tests._bootstrap import init_env

DATA = init_env("loctms_events_")

from datetime import timedelta

from sqlmodel import Session

from app import db
from app.models import LiveEvent
from app.services import events

PUBLISHER = r"""
import sys, time
sys.path.insert(0, sys.argv[1])
from app import db  # warm up: the timestamps measure delivery, not app import
from app.services import events
channel, n = sys.argv[2], int(sys.argv[3])
for i in range(n):
    events.publish(channel, {"i": i, "t": time.time()})
"""


def _publish_from_child(channel: str, n: int) -> None:
    env = dict(os.environ, LOC_TMS_DATA_DIR=DATA)
    subprocess.run(
        [sys.executable, "-c", PUBLISHER, _ROOT, channel, str(n)],
        env=env,
        cwd=_ROOT,
        timeout=120,
        check=True,
    )


def _drain(sub, n: int, timeout: float) -> list:
    got: list = []
    deadline = time.monotonic() + timeout
    while len(got) < n and time.monotonic() < deadline:
        got.extend(sub.poll(timeout=0.5))
    return got


def test_cross_process():
    with events.subscribe("xp") as sub:
        _publish_from_child("xp", 1)
        got = _drain(sub, 1, 10)
        recv = time.time()
    assert [g["i"] for g in got] == [0], got
    lag = recv - got[0]["t"]
    assert lag <= 2.0, f"cross-process delivery took {lag:.2f}s (limit 2 s)"
    print(f"  cross-process: delivered {lag:.2f}s after publish OK")


def test_burst():
    a = events.subscribe("burst")
    b = events.subscribe("burst")
    got_a: list = []

    def reader():
        for payload in a:
            got_a.append(payload["i"])
            if len(got_a) >= 200:
                break

    th = threading.Thread(target=reader, daemon=True)
    th.start()
    _publish_from_child("burst", 200)
    th.join(20)
    got_b = [p["i"] for p in _drain(b, 200, 20)]
    a.close()
    b.close()
    assert got_a == list(range(200)), f"iterator got {len(got_a)}, ordered={got_a == sorted(got_a)}"
    assert got_b == list(range(200)), f"poller got {len(got_b)}, ordered={got_b == sorted(got_b)}"
    print("  burst: 200 events, in order, no duplicates, to both subscribers OK")


def test_isolation():
    events.publish("iso", {"early": True})
    with events.subscribe("iso") as sub, events.subscribe("other") as other:
        events.publish("other", {"o": 1})
        events.publish("iso", {"late": True})
        got = _drain(sub, 1, 5)
        time.sleep(0.6)
        got += sub.poll(timeout=0)
        got_other = _drain(other, 1, 5)
    assert got == [{"late": True}], got
    assert got_other == [{"o": 1}], got_other
    print("  isolation: own channel only, nothing from before subscribe OK")


def test_transactional():
    with events.subscribe("tx") as sub:
        with Session(db.engine) as s:
            events.publish("tx", {"n": "rolled back"}, session=s)
            s.rollback()
        with Session(db.engine) as s:
            events.publish("tx", {"n": "committed"}, session=s)
            assert sub.poll(timeout=0.8) == [], "must not be delivered before the commit"
            s.commit()
        got = _drain(sub, 1, 5)
        time.sleep(0.6)
        got += sub.poll(timeout=0)
    assert got == [{"n": "committed"}], got
    print("  transactional: delivered on commit, never on rollback OK")


def test_oversized():
    big = {"text": "x" * (events.NOTIFY_INLINE_MAX_BYTES * 3), "k": "end"}
    with events.subscribe("big") as sub:
        events.publish("big", big)
        events.publish("big", {"small": 1})
        got = _drain(sub, 2, 5)
    assert got == [big, {"small": 1}], [type(g) for g in got]
    print("  oversized: a payload over the NOTIFY limit arrives intact and in order OK")


def test_lifecycle():
    sub = events.subscribe("quiet")
    t0 = time.monotonic()
    assert sub.poll(timeout=0.3) == []
    assert time.monotonic() - t0 >= 0.25
    done = threading.Event()

    def reader():
        for _ in sub:
            pass
        done.set()

    threading.Thread(target=reader, daemon=True).start()
    sub.close()
    assert done.wait(3), "close() must end iteration"
    try:
        events.subscribe("")
        raise AssertionError("an empty channel must be refused")
    except ValueError:
        pass
    print("  lifecycle: poll times out empty, close() ends iteration OK")


def test_sweep_sqlite():
    tbl = LiveEvent.__table__
    events.publish("sweep", {"n": 1})
    with db.engine.begin() as cx:
        top = cx.execute(tbl.select().order_by(tbl.c.id.desc())).first().id
        cx.execute(tbl.update().values(created_at=events._now() - timedelta(hours=1)))
    removed = events._maybe_sweep(force=True)
    assert removed >= 1, removed
    with db.engine.connect() as cx:
        assert cx.execute(tbl.select()).first() is None, "every old row is swept"
    with events.subscribe("sweep") as sub:
        events.publish("sweep", {"n": 2})
        got = _drain(sub, 1, 5)
    with db.engine.connect() as cx:
        new_top = cx.execute(tbl.select().order_by(tbl.c.id.desc())).first().id
    assert new_top > top, f"ids must keep increasing after a sweep ({top} -> {new_top})"
    assert got == [{"n": 2}], got
    print("  sweep: old rows deleted, ids keep increasing (AUTOINCREMENT) OK")


def test_no_stall_sqlite():
    """Gate HIGH-1 / MED-2: a sweep never waits on a writer. publish(session=) after the
    caller flushed a write must not block on its own writer lock, and the poller's sweep
    must give up at once while another connection holds the lock."""
    from app.models import Project

    events._last_sweep["at"] = 0.0  # a sweep is due on the next no-session publish
    with Session(db.engine) as s:
        s.add(Project(name="stall probe", source_locale="en-US"))
        s.flush()  # this session now holds the SQLite writer lock
        t0 = time.monotonic()
        events.publish("stall", {"n": 1}, session=s)
        took = time.monotonic() - t0
        s.commit()
    assert took < 1.0, f"publish(session=) took {took:.2f}s (the sweep waited on the lock)"
    with db.engine.connect() as holder:
        holder.exec_driver_sql("BEGIN IMMEDIATE")  # another connection holds the writer lock
        try:
            t0 = time.monotonic()
            assert events._maybe_sweep(force=True) == 0
            took = time.monotonic() - t0
        finally:
            holder.exec_driver_sql("ROLLBACK")
    assert took < 1.0, f"a blocked sweep waited {took:.2f}s instead of giving up"
    with db.engine.connect() as cx:
        bt = cx.exec_driver_sql("PRAGMA busy_timeout").scalar()
    assert bt == db.BUSY_TIMEOUT_MS, f"pooled connection left with busy_timeout {bt}"
    for bad in ("", "x" * (events.CHANNEL_MAX_LEN + 1), None):
        try:
            events.publish(bad, {})
            raise AssertionError(f"channel {bad!r} must be refused")
        except ValueError:
            pass
    print("  no-stall: transactional publish and a blocked sweep never wait on the lock OK")


def _run_all(dialect: str) -> None:
    test_cross_process()
    test_burst()
    test_isolation()
    test_transactional()
    test_oversized()
    test_lifecycle()
    if dialect == "sqlite":
        test_sweep_sqlite()
        test_no_stall_sqlite()


def _pg_child() -> None:
    assert db.engine.dialect.name == "postgresql", db.engine.dialect.name
    LiveEvent.__table__.drop(db.engine, checkfirst=True)
    LiveEvent.__table__.create(db.engine)
    try:
        _run_all("postgresql")
        with db.engine.connect() as cx:
            rows = cx.execute(LiveEvent.__table__.select()).all()
        assert len(rows) == 1 and rows[0].channel == "big", (
            f"on Postgres only the oversized payload is stored, found {len(rows)} rows"
        )
    finally:
        db.engine.dispose()
        LiveEvent.__table__.drop(db.engine, checkfirst=True)
        db.engine.dispose()
    print("LIVE EVENTS POSTGRES OK")


def _pg_half() -> None:
    if not PG_URL:
        print("  postgres: SKIP (set LOC_TMS_TEST_PG_URL to an empty scratch database)")
        return
    env = dict(os.environ, DATABASE_URL=PG_URL, PYTHONIOENCODING="utf-8")
    env.pop("LOC_TMS_DATA_DIR", None)
    p = subprocess.run(
        [sys.executable, os.path.abspath(__file__), "--pg-child"],
        env=env,
        cwd=_ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=300,
    )
    out = [ln for ln in (p.stdout or "").splitlines() if ln.strip()]
    for ln in out:
        print("  [pg]" + ln)
    assert p.returncode == 0 and out and out[-1].endswith("OK"), (p.stderr or "")[-2000:]


if __name__ == "__main__":
    if PG_CHILD:
        _pg_child()
        sys.exit(0)
    db.init_db()
    assert db.engine.dialect.name == "sqlite"
    _run_all("sqlite")
    _pg_half()
    print("LIVE EVENTS OK")
