"""S9 WP-METER: the read meter's month job index is built by ONE scan per process at a time.

`project_budgets._month_job_index` (S1) loads the month's cost-bearing Job rows to price every
project's read meter. It used to be unguarded: when its 15 s cache entry expired, every concurrent
`budget_state(display=True)` caller (inline cache misses AND every per-project background refresh)
rebuilt it at once, each holding a pool connection for the whole scan. With a large job table that
drained the pool (S7c on a test server: 16 percent 503s at 25 readers). This suite pins:

  (1) K concurrent callers with NO index yet run exactly one scan of the job table;
  (2) K concurrent callers with an EXPIRED index run exactly one scan, and every caller but the
      builder gets the previous index at once (stale-while-revalidate), without waiting;
  (3) background meter refreshes (one thread per stale project) run exactly one scan together when
      the index is stale, and none when it is fresh;
  (4) a failing build raises to its own caller only: waiters never hang, the cache is not poisoned,
      and the next call builds normally;
  (5) the meter figures equal the live per-project scan the gates use (value identity), and the
      figures do not move between a single-flight build and a serial build.

Offline, hermetic (own LOC_TMS_DATA_DIR), ASCII only.
"""

import os
import sys
import threading
import time
from datetime import datetime

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(_ROOT)
sys.path.insert(0, _ROOT)
from tests._bootstrap import init_env

init_env("loctms_meteridx_")

from sqlalchemy import event
from sqlmodel import Session

from app.db import engine, init_db
from app.models import Document, Job, JobStatus, Project
from app.services import project_budgets, reports
from tests._stmt_counter import StatementCounter

K = 12  # concurrent callers
N_PROJECTS = 6
JOBS_PER_PROJECT = 7
SCAN_DELAY_S = 0.4  # every job-table scan sleeps this long, so unguarded callers overlap

_counter = StatementCounter(engine)


def _is_job_scan(statement: str) -> bool:
    s = " ".join(statement.split()).lower()
    return " from job " in s + " " and "job.kind in" in s


@event.listens_for(engine, "before_cursor_execute")
def _slow_job_scan(conn, cursor, statement, parameters, context, executemany):
    if _is_job_scan(statement):
        time.sleep(SCAN_DELAY_S)


def _seed():
    now = datetime.utcnow()
    pids = []
    with Session(engine) as s:
        for i in range(N_PROJECTS):
            p = Project(name=f"meteridx-{i}", source_locale="en-US")
            s.add(p)
            s.commit()
            s.refresh(p)
            d = Document(project_id=p.id, filename=f"d{i}.txt", source_text="x")
            s.add(d)
            s.commit()
            s.refresh(d)
            pids.append(p.id)
            for j in range(JOBS_PER_PROJECT):
                payload = {"project_id": p.id} if j % 2 == 0 else {"document_id": d.id}
                s.add(
                    Job(
                        kind="translate_document",
                        status=JobStatus.COMPLETED,
                        payload=payload,
                        result={},
                        cost_usd=0.25 * (j + 1) + i,
                        created_at=now,
                        finished_at=now,
                    )
                )
            s.commit()
    return pids


def _expire_index():
    """Age the cached index past its TTL without dropping it."""
    with project_budgets._index_lock:
        for lo, ent in list(project_budgets._index.items()):
            project_budgets._index[lo] = (ent[0] - project_budgets._STATE_TTL_S - 5.0, *ent[1:])


def _run_concurrently(fn, n=K, timeout=60.0):
    """Run fn(i) on n threads released together; returns (results, errors, per-thread seconds)."""
    barrier = threading.Barrier(n)
    results, errors, secs = [None] * n, [None] * n, [None] * n

    def work(i):
        barrier.wait()
        t0 = time.monotonic()
        try:
            results[i] = fn(i)
        except Exception as e:
            errors[i] = e
        secs[i] = time.monotonic() - t0

    ts = [threading.Thread(target=work, args=(i,), daemon=True) for i in range(n)]
    for t in ts:
        t.start()
    for t in ts:
        t.join(timeout)
        assert not t.is_alive(), "a caller never returned (waiting forever on a builder?)"
    return results, errors, secs


def _display_ai(pid):
    with Session(engine) as s:
        return project_budgets.budget_state(s, pid, display=True)["ai_usd"]


def _live_ai(pid):
    with Session(engine) as s:
        return project_budgets.budget_state(s, pid)["ai_usd"]


def _scans(m) -> int:
    return sum(c for k, c in m.by_statement.items() if _is_job_scan(k))


def test_no_index_one_scan(pids):
    project_budgets._index.clear()
    with _counter.measure() as m:
        res, errs, _ = _run_concurrently(lambda i: _display_ai(pids[i % len(pids)]))
    assert not any(errs), errs
    assert _scans(m) == 1, f"{K} concurrent first callers must run ONE scan, ran {_scans(m)}"
    for i, v in enumerate(res):
        want = _live_ai(pids[i % len(pids)])
        assert abs(v - want) < 1e-9, (i, v, want)
    print("  concurrent callers with no index: exactly one scan, values equal the live scan: OK")


def test_expired_index_one_scan_stale_served(pids):
    _display_ai(pids[0])  # make sure an index exists
    _expire_index()
    with _counter.measure() as m:
        res, errs, secs = _run_concurrently(lambda i: _display_ai(pids[i % len(pids)]))
    assert not any(errs), errs
    assert _scans(m) == 1, f"{K} callers on an expired index must run ONE scan, ran {_scans(m)}"
    fast = sum(1 for s in secs if s < SCAN_DELAY_S * 0.75)
    assert fast >= K - 2, (
        f"callers must get the stale index without waiting; fast={fast} secs={secs}"
    )
    for i, v in enumerate(res):
        want = _live_ai(pids[i % len(pids)])
        assert abs(v - want) < 1e-9, (i, v, want)
    # the index is fresh again after the builder finished
    with _counter.measure() as m2:
        _display_ai(pids[0])
    assert _scans(m2) == 0
    print(
        "  concurrent callers on an expired index: one scan, others served the previous index: OK"
    )


def _wait_refreshers():
    t0 = time.monotonic()
    while time.monotonic() - t0 < 30:
        with project_budgets._state_lock:
            if not project_budgets._state_refreshing:
                break
        time.sleep(0.02)
    for t in threading.enumerate():
        if t.name == "budget-meter-refresh":
            t.join(30)


def test_refresh_threads_share_one_scan(pids):
    project_budgets.forget_budget_state()
    with Session(engine) as s:
        for pid in pids:
            project_budgets.budget_state_cached(s, pid)  # fills the per-project state cache
    # age every cached state so each read spawns its own refresh thread
    with project_budgets._state_lock:
        for pid, (val, ts) in list(project_budgets._state_cache.items()):
            project_budgets._state_cache[pid] = (val, ts - project_budgets._STATE_TTL_S - 5.0)
    # (a) index fresh: refreshes must not scan
    with _counter.measure() as m:
        with Session(engine) as s:
            for pid in pids:
                project_budgets.budget_state_cached(s, pid)
        _wait_refreshers()
    assert _scans(m) == 0, f"refreshes on a fresh index must not scan, ran {_scans(m)}"
    # (b) index stale: all refreshes together run one scan
    with project_budgets._state_lock:
        for pid, (val, ts) in list(project_budgets._state_cache.items()):
            project_budgets._state_cache[pid] = (val, ts - project_budgets._STATE_TTL_S - 5.0)
    _expire_index()
    with _counter.measure() as m:
        with Session(engine) as s:
            for pid in pids:
                project_budgets.budget_state_cached(s, pid)
        _wait_refreshers()
    assert _scans(m) == 1, (
        f"{len(pids)} refresh threads on a stale index must share ONE scan, ran {_scans(m)}"
    )
    for pid in pids:
        with Session(engine) as s:
            got = project_budgets.budget_state_cached(s, pid)["ai_usd"]
        assert abs(got - _live_ai(pid)) < 1e-9, (pid, got)
    print("  background refreshes: none on a fresh index, one shared scan on a stale one: OK")


def test_failed_build_does_not_hang_or_poison(pids):
    project_budgets._index.clear()
    real = reports._job_cost
    state = {"fail": True, "n": 0}
    lk = threading.Lock()

    def flaky(job):
        with lk:
            state["n"] += 1
            if state["fail"]:
                raise RuntimeError("boom")
        return real(job)

    reports._job_cost = flaky
    try:
        res, errs, _ = _run_concurrently(lambda i: _display_ai(pids[0]), n=6)
        assert all(r is not None or e is not None for r, e in zip(res, errs, strict=True)), (
            "every caller must return"
        )
        assert any(isinstance(e, RuntimeError) for e in errs), (
            "the builder's own caller sees the failure"
        )
        assert not project_budgets._index, "a failed build must not store an index"
        state["fail"] = False
        got = _display_ai(pids[0])
        assert abs(got - _live_ai(pids[0])) < 1e-9
    finally:
        reports._job_cost = real
    print(
        "  a failing build raises to its caller only; waiters return; the cache is not poisoned: OK"
    )


def test_values_equal_serial_build(pids):
    project_budgets._index.clear()
    serial = [_display_ai(p) for p in pids]
    live = [_live_ai(p) for p in pids]
    for a, b in zip(serial, live, strict=True):
        assert abs(a - b) < 1e-9, (a, b)
    print("  meter figures equal the live per-project scan: OK")


def test_late_abandoned_build_never_overwrites_newer(pids):
    """(6) A builder presumed abandoned (older than _BUILD_MAX_S) but still alive finishes AFTER
    the build that took over: its older result must not replace the newer index."""
    project_budgets._index.clear()
    real = project_budgets._build_month_index
    gate, entered = threading.Event(), threading.Event()
    older = {"p": {}, "d": {}, "c": {}}

    def maybe_slow(session, lo, hi):
        if threading.current_thread().name == "late-builder":
            entered.set()
            gate.wait(30)
            return older
        return real(session, lo, hi)

    project_budgets._build_month_index = maybe_slow
    try:
        t = threading.Thread(target=lambda: _display_ai(pids[0]), name="late-builder", daemon=True)
        t.start()
        assert entered.wait(30), "the late builder never started"
        lo = project_budgets._month_start()
        with project_budgets._index_lock:
            done, started = project_budgets._index_building[lo]
            project_budgets._index_building[lo] = (
                done,
                started - project_budgets._BUILD_MAX_S - 1.0,
            )
        want = _live_ai(pids[0])
        got = _display_ai(pids[0])  # takes the abandoned build over and stores a newer index
        assert abs(got - want) < 1e-9, (got, want)
        newer = project_budgets._index[lo][1]
        gate.set()
        t.join(30)
        assert not t.is_alive()
        assert project_budgets._index[lo][1] is newer, "a late older build replaced the index"
        assert abs(_display_ai(pids[0]) - want) < 1e-9
    finally:
        gate.set()
        project_budgets._build_month_index = real
    print("  a late abandoned build never replaces the newer index: OK")


def test_long_idle_index_not_served(pids):
    """(7) Stale-while-revalidate only covers an index that expired recently: after a long idle
    spell (index far older than the TTL) concurrent callers wait for the build in flight
    instead of being served month-old figures."""
    _display_ai(pids[0])  # a current index
    now = datetime.utcnow()
    with Session(engine) as s:
        s.add(
            Job(
                kind="translate_document",
                status=JobStatus.COMPLETED,
                payload={"project_id": pids[1]},
                result={},
                cost_usd=7.5,
                created_at=now,
                finished_at=now,
            )
        )
        s.commit()
    with project_budgets._index_lock:
        for lo, ent in list(project_budgets._index.items()):
            project_budgets._index[lo] = (ent[0] - 3600.0, *ent[1:])
    with _counter.measure() as m:
        res, errs, _ = _run_concurrently(lambda i: _display_ai(pids[1]))
    assert not any(errs), errs
    assert _scans(m) == 1, f"one scan expected, ran {_scans(m)}"
    want = _live_ai(pids[1])
    bad = [v for v in res if abs(v - want) > 1e-9]
    assert not bad, f"{len(bad)} callers were served an index an hour old ({bad[0]} != {want})"
    print("  an index far past its TTL is never served during a build (callers wait): OK")


def main():
    init_db()
    pids = _seed()
    test_values_equal_serial_build(pids)
    test_no_index_one_scan(pids)
    test_expired_index_one_scan_stale_served(pids)
    test_refresh_threads_share_one_scan(pids)
    test_failed_build_does_not_hang_or_poison(pids)
    test_late_abandoned_build_never_overwrites_newer(pids)
    test_long_idle_index_not_served(pids)
    print("METER INDEX SINGLE FLIGHT OK")


if __name__ == "__main__":
    main()
