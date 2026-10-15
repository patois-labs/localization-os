"""#622: the department budget reserve-then-spend step is atomic ACROSS processes.

Before this, jobs.submit_budgeted / submit_unique_budgeted / reserve_live_translate
serialized "check the cap, insert the Job" under an in-process lock only. Two app processes
(or LOC_TMS_JOB_WORKERS=4) each saw the same remaining budget and both inserted. The check
and the insert now share one database transaction that holds a write lock on the budget
rows (budgets.check_locked), so exactly one racer for the last of a budget wins.

Offline and hermetic (throwaway LOC_TMS_DATA_DIR, no provider). Covers:
  [1] FOUR separate OS processes race jobs.submit_budgeted for a cap with room for one job
      (each sleeps between the check and the insert to force the interleaving): exactly one
      is admitted, three refused, exactly one Job row exists.
  [2] The same race across separate connections in one process with the in-process lock
      REMOVED (the database lock alone must serialize): submit_budgeted,
      submit_unique_budgeted and reserve_live_translate each admit exactly one.
  [3] An unbudgeted department never takes the lock and is never refused.
  [4] The Postgres branch takes SELECT ... ORDER BY id FOR UPDATE (compiled, not executed:
      no Postgres on the test box), scoped to the org when given.

Final line: "BUDGET CROSS PROCESS OK".
"""

import contextlib
import os
import subprocess
import sys
import threading
import time

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(_ROOT)
sys.path.insert(0, _ROOT)

_CHILD = len(sys.argv) > 1 and sys.argv[1] == "--child"
if not _CHILD:
    from tests._bootstrap import init_env

    init_env("loctms_budxproc_")

from sqlmodel import Session, select

from app.db import engine, init_db
from app.models import Document, Job, Project, Segment, TargetLocale, TransMode
from app.services import budgets, jobs, settings

init_db()
settings.set_many({"security_auth_required": "0", "multi_org_enabled": "0", "project_scoping": "0"})
settings.invalidate_cache()

CAP = 5_000  # tokens; one 8,000-token job uses it up, so only one racer may be admitted
EST = 8_000
DEPT = "racers"


def _payload(dept=DEPT):
    return {"document_id": 0, "locale": "fr-FR", "est_tokens": EST, "department": dept}


def _job_rows():
    with Session(engine) as s:
        return len(s.exec(select(Job).where(Job.kind == "translate_document")).all())


def _child_main(ready_path, go_path):
    """One racing process: widen the check -> insert gap, wait for the green light, race."""
    real_submit = jobs.submit

    def slow_submit(kind, payload, actor="", **kw):
        time.sleep(0.6)  # the interleaving window the old code left open
        return real_submit(kind, payload, actor=actor, **kw)

    jobs.submit = slow_submit
    jobs.ensure_workers = lambda: None  # never start a worker: the Job row is what counts
    open(ready_path, "w").close()
    while not os.path.exists(go_path):
        time.sleep(0.01)
    try:
        jobs.submit_budgeted(
            "translate_document", _payload(), actor="racer@example.com", department=DEPT
        )
        print("RESULT admitted")
    except jobs.LiveBudgetRefused:
        print("RESULT refused")


def _seed_budget(dept, limit):
    with Session(engine) as s:
        budgets.upsert_budget(s, dept, limit)


def test_1_four_processes_one_winner():
    _seed_budget(DEPT, CAP)
    tmp = os.environ["LOC_TMS_DATA_DIR"]
    go = os.path.join(tmp, "go.flag")
    procs, readies = [], []
    for n in range(4):
        ready = os.path.join(tmp, f"ready{n}.flag")
        readies.append(ready)
        procs.append(
            subprocess.Popen(
                [sys.executable, os.path.abspath(__file__), "--child", ready, go],
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
            )
        )
    deadline = time.time() + 120
    while not all(os.path.exists(r) for r in readies):
        assert time.time() < deadline, "racing processes never became ready"
        assert all(p.poll() is None for p in procs), "a racing process died early"
        time.sleep(0.05)
    open(go, "w").close()
    outs = []
    for p in procs:
        out, _ = p.communicate(timeout=120)
        outs.append(out)
        assert p.returncode == 0, out
    results = sorted(line.split()[1] for o in outs for line in o.splitlines() if "RESULT" in line)
    assert results == ["admitted", "refused", "refused", "refused"], (results, outs)
    assert _job_rows() == 1, f"expected exactly one Job row, got {_job_rows()}"
    print("  [1] four processes race the last of a cap: exactly one admitted, one Job row")


def _clear_jobs():
    with Session(engine) as s:
        for j in s.exec(select(Job)).all():
            s.delete(j)
        s.commit()


@contextlib.contextmanager
def _db_lock_only():
    """Remove the in-process lock so ONLY the database reservation lock can serialize."""
    real = jobs._live_lock
    jobs._live_lock = contextlib.nullcontext()
    real_submit = jobs.submit
    real_ensure = jobs.ensure_workers

    def slow_submit(kind, payload, actor="", **kw):
        time.sleep(0.4)
        return real_submit(kind, payload, actor=actor, **kw)

    jobs.submit = slow_submit
    jobs.ensure_workers = lambda: None
    try:
        yield
    finally:
        jobs._live_lock = real
        jobs.submit = real_submit
        jobs.ensure_workers = real_ensure


def _race(fn, n=4):
    gate = threading.Barrier(n)
    out = []

    def run():
        gate.wait()
        try:
            fn()
            out.append("admitted")
        except jobs.LiveBudgetRefused:
            out.append("refused")

    threads = [threading.Thread(target=run) for _ in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=120)
    return sorted(out)


def _mk_live_doc():
    with Session(engine) as s:
        p = Project(name="XProcLive", source_locale="en-US")
        s.add(p)
        s.commit()
        s.refresh(p)
        s.add(TargetLocale(project_id=p.id, locale="de-DE"))
        d = Document(
            project_id=p.id,
            filename="x.txt",
            fmt="txt",
            mode=TransMode.SEGMENTED,
            skeleton={"units": [{"text": "x"}]},
        )
        s.add(d)
        s.commit()
        s.refresh(d)
        s.add(
            Segment(
                document_id=d.id,
                target_locale="de-DE",
                order_index=0,
                source_text="word " * 8000,
                target_text="",
            )
        )
        s.commit()
        return d.id


def test_2_db_lock_alone_serializes_every_door():
    _seed_budget(DEPT, CAP)
    with _db_lock_only():
        _clear_jobs()
        got = _race(
            lambda: jobs.submit_budgeted(
                "translate_document", _payload(), actor="t@example.com", department=DEPT
            )
        )
        assert got == ["admitted", "refused", "refused", "refused"], ("submit_budgeted", got)
        assert _job_rows() == 1

        _clear_jobs()
        counter = iter(range(1000))
        got = _race(
            lambda: jobs.submit_unique_budgeted(
                "translate_document",
                {**_payload(), "document_id": next(counter)},
                actor="t@example.com",
                dedupe_key=("document_id", "locale"),
                department=DEPT,
            )
        )
        assert got == ["admitted", "refused", "refused", "refused"], ("submit_unique", got)
        assert _job_rows() == 1

        _clear_jobs()
        doc_id = _mk_live_doc()
        real_submit = jobs.submit  # (slow wrapper) unused by the live door; keep the window:
        real_estimate = jobs._live_estimate

        def slow_estimate(*a, **k):
            time.sleep(0.4)
            return real_estimate(*a, **k)

        jobs._live_estimate = slow_estimate
        try:
            got = _race(
                lambda: jobs.reserve_live_translate(doc_id, "de-DE", department=DEPT, actor="t")
            )
        finally:
            jobs._live_estimate = real_estimate
        assert real_submit is jobs.submit
        assert got == ["admitted", "refused", "refused", "refused"], ("reserve_live", got)
        assert _job_rows() == 1
    print("  [2] DB lock alone (no in-process lock): submit_budgeted, submit_unique_budgeted,")
    print("      reserve_live_translate each admit exactly one of four racers")


def test_3_unbudgeted_department_not_locked_or_refused():
    _clear_jobs()
    calls = []
    real_lock = budgets.lock_budget_rows
    budgets.lock_budget_rows = lambda *a, **k: calls.append(1)
    try:
        for _ in range(3):
            with Session(engine) as s:
                allowed, rem, msg = budgets.check_locked(s, "no-such-dept")
            assert (allowed, rem, msg) == (True, -1, "")
        with Session(engine) as s:
            assert budgets.check_locked(s, "") == (True, -1, "")
    finally:
        budgets.lock_budget_rows = real_lock
    assert not calls, "an unbudgeted department must not take the reservation lock"
    print("  [3] unbudgeted / blank department: never locked, never refused")


def test_4_postgres_branch_row_locks():
    from sqlalchemy.dialects import postgresql

    class _Dialect:
        name = "postgresql"

    class _Bind:
        dialect = _Dialect()

    class _Res:
        def all(self):
            return []

    class _FakeSession:
        stmts = []  # noqa: RUF012

        def get_bind(self):
            return _Bind()

        def exec(self, stmt):
            self.stmts.append(stmt)
            return _Res()

    fs = _FakeSession()
    budgets.lock_budget_rows(fs)
    sql = str(fs.stmts[-1].compile(dialect=postgresql.dialect())).upper()
    assert "FOR UPDATE" in sql and "ORDER BY" in sql, sql
    print("  [4] Postgres branch: SELECT id ... ORDER BY id FOR UPDATE on all budget rows")


def test_5_unique_door_takes_submit_lock_before_db_lock():
    """Review: submit_unique_budgeted must wait for _submit_lock BEFORE it takes the database
    write lock. A plain submit_unique caller holds _submit_lock and then needs the SQLite
    writer lock; if the budgeted door held the writer lock while waiting for _submit_lock,
    the plain caller would fail with 'database is locked' after busy_timeout."""
    _clear_jobs()
    _seed_budget(DEPT, CAP)
    real_ensure = jobs.ensure_workers
    jobs.ensure_workers = lambda: None
    out = {}

    def door():
        try:
            out["r"] = jobs.submit_unique_budgeted(
                "translate_document",
                {**_payload(), "document_id": 77},
                actor="x@example.com",
                dedupe_key="document_id",
                department=DEPT,
            )
        except Exception as exc:
            out["r"] = exc

    t = threading.Thread(target=door)
    try:
        with jobs._submit_lock:  # stands in for a plain submit_unique caller mid-flight
            t.start()
            time.sleep(1.0)  # the door is now blocked on _submit_lock
            assert t.is_alive(), "the door should be waiting for _submit_lock"
            started = time.monotonic()
            with Session(engine) as s:  # the plain caller's write must NOT wait on the door
                s.add(Job(kind="translate_document", payload={"document_id": 1}, created_by="p"))
                s.commit()
            took = time.monotonic() - started
            assert took < 2.0, f"a writer waited {took:.1f}s on the blocked budgeted door"
        t.join(10)
        assert not t.is_alive() and isinstance(out.get("r"), tuple), out
    finally:
        jobs.ensure_workers = real_ensure
        _clear_jobs()
    print("  [5] unique door waits for _submit_lock without holding the database write lock")


if __name__ == "__main__":
    if _CHILD:
        _child_main(sys.argv[2], sys.argv[3])
        raise SystemExit(0)
    failures = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
            except Exception as exc:
                failures += 1
                import traceback

                print(f"  FAIL {name}: {exc}")
                traceback.print_exc()
    if failures:
        print(f"verify_budget_cross_process FAILED ({failures})")
        raise SystemExit(1)
    print("BUDGET CROSS PROCESS OK")
