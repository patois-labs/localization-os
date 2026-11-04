"""S1 item 6 (gate G5): the project GET must not count every TM row on every hit.

Pins app/services/tm_counts.py, the cached `tm.count` / `tm.ft_count` the project GET, the
project page and the customer rollup read:

  (1) the cached value equals the live `tm.count`, and GET /api/projects/{pid} reports it
      under the unchanged `tm_count` / `ft_tm_count` keys;
  (2) a second GET does not run the COUNT again;
  (3) a TM import and a TM entry delete show up at once (invalidation hooks);
  (4) concurrent FIRST callers trigger exactly one COUNT;
  (5) once the TTL passes, the stale value is served at once while ONE background refresh
      runs (never one per caller), and the next read sees the new number;
  (6) the cache is bounded (LRU);
  (7) the budget meter on the same GET is cached for reads (project_budgets.budget_state_cached)
      and refreshed by a job finish or a budget change;
  (8) its shared month index prices each project exactly like the live scan.

HERMETIC: own LOC_TMS_DATA_DIR, provider keys blanked before import. ASCII only.
"""

import os
import sys
import threading
import time

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(_ROOT)
sys.path.insert(0, _ROOT)
from tests._bootstrap import init_env

init_env("loctms_tmcc_")

from fastapi.testclient import TestClient
from sqlmodel import Session, select

from app.db import engine, init_db
from app.main import app
from app.models import Project, SegmentTM
from app.services import apikeys, security, tm, tm_counts, tm_module

_SRC, _TGT = "en-US", "fr-FR"
_REAL_COUNT = tm.count
_REAL_FT = tm.ft_count


class _Probe:
    """Counts calls to tm.count / tm.ft_count; optionally slows each one down."""

    def __init__(self, delay=0.0):
        self.delay = delay
        self.calls = 0
        self._lk = threading.Lock()

    def __enter__(self):
        def counted(session, pid):
            with self._lk:
                self.calls += 1
            if self.delay:
                time.sleep(self.delay)
            return _REAL_COUNT(session, pid)

        tm.count = counted
        return self

    def __exit__(self, *exc):
        tm.count = _REAL_COUNT


_PW = "tmcc-test-pw-123"


_KEY = []


def _client():
    if not _KEY:
        user = security.create_user("tmcc-admin@x.com", "Admin", "admin", _PW)
        _KEY.append(apikeys.create_key(user.id, "tmcc-test", scopes="api docs")[1])
    return TestClient(app, headers={"X-Loc-Token": _KEY[0]})


def _mk_project(name):
    with Session(engine) as s:
        p = Project(name=name, source_locale=_SRC)
        s.add(p)
        s.commit()
        s.refresh(p)
        return p.id


def _tmx(pairs) -> bytes:
    tus = "".join(
        f'<tu><tuv xml:lang="{_SRC}"><seg>{a}</seg></tuv>'
        f'<tuv xml:lang="{_TGT}"><seg>{b}</seg></tuv></tu>'
        for a, b in pairs
    )
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        f'<tmx version="1.4"><header srclang="{_SRC}"/><body>{tus}</body></tmx>'
    ).encode()


def _seed(pid, n, tag="seed"):
    with Session(engine) as s:
        for i in range(n):
            tm.write_back(s, pid, f"{tag} source {i}.", f"{tag} cible {i}.", _SRC, _TGT)


def test_value_and_get_shape_and_no_recount():
    pid = _mk_project("tmcc-a")
    _seed(pid, 3)
    tm_counts._reset_for_tests()
    client = _client()
    with _Probe() as probe:
        r1 = client.get(f"/api/projects/{pid}")
        assert r1.status_code == 200, (r1.status_code, r1.text[:200])
        b1 = r1.json()
        assert b1["tm_count"] == 3 and isinstance(b1["tm_count"], int), b1["tm_count"]
        assert b1["ft_tm_count"] == 0 and isinstance(b1["ft_tm_count"], int), b1["ft_tm_count"]
        with Session(engine) as s:
            assert b1["tm_count"] == _REAL_COUNT(s, pid)
            assert b1["ft_tm_count"] == _REAL_FT(s, pid)
        first = probe.calls
        assert first == 1, f"first GET runs one segment COUNT, ran {first}"
        for _ in range(3):
            b = client.get(f"/api/projects/{pid}").json()
            assert b["tm_count"] == 3
        assert probe.calls == first, f"repeat GETs re-ran COUNT: {probe.calls} vs {first}"
    print("  same value as tm.count, GET shape intact, repeat GET does not recount: OK")


def test_import_and_delete_show_at_once():
    pid = _mk_project("tmcc-b")
    _seed(pid, 2)
    tm_counts._reset_for_tests()
    client = _client()
    assert client.get(f"/api/projects/{pid}").json()["tm_count"] == 2
    with Session(engine) as s:
        tm.import_tmx(
            s,
            pid,
            _tmx([("Imported one", "Importe un"), ("Imported two", "Importe deux")]),
            _SRC,
            _TGT,
        )
    assert client.get(f"/api/projects/{pid}").json()["tm_count"] == 4, "import not visible at once"
    with Session(engine) as s:
        eid = s.exec(select(SegmentTM.id).where(SegmentTM.project_id == pid)).first()
        assert tm_module.delete_entry(s, eid) is True
    assert client.get(f"/api/projects/{pid}").json()["tm_count"] == 3, "delete not visible at once"
    # a write that has no hook (approve write-back) is NOT instant: TTL bounds it
    _seed(pid, 1, tag="late")
    assert client.get(f"/api/projects/{pid}").json()["tm_count"] == 3
    tm_counts.invalidate(pid)
    assert client.get(f"/api/projects/{pid}").json()["tm_count"] == 4
    print("  import + delete invalidate at once; unhooked write waits for TTL/invalidate: OK")


def test_concurrent_first_callers_one_count():
    pid = _mk_project("tmcc-c")
    _seed(pid, 5)
    tm_counts._reset_for_tests()
    out, errs = [], []
    gate = threading.Barrier(8)

    def worker():
        try:
            with Session(engine) as s:
                gate.wait(timeout=10)
                out.append(tm_counts.count(s, pid))
        except Exception as e:
            errs.append(e)

    with _Probe(delay=0.4) as probe:
        ts = [threading.Thread(target=worker) for _ in range(8)]
        for t in ts:
            t.start()
        for t in ts:
            t.join(timeout=30)
        assert not errs, errs
        assert out == [5] * 8, out
        assert probe.calls == 1, f"8 concurrent first callers ran {probe.calls} COUNTs"
    print("  8 concurrent first callers share one COUNT: OK")


def test_stale_served_while_one_refresh_runs():
    pid = _mk_project("tmcc-d")
    _seed(pid, 2)
    tm_counts._reset_for_tests()
    old_ttl = tm_counts.TTL_SECONDS
    try:
        with Session(engine) as s:
            assert tm_counts.count(s, pid) == 2
        _seed(pid, 1, tag="more")  # no hook: the cache still says 2
        tm_counts.TTL_SECONDS = 0.05
        time.sleep(0.15)
        with _Probe(delay=0.5) as probe:
            t0 = time.monotonic()
            with Session(engine) as s:
                vals = [tm_counts.count(s, pid) for _ in range(5)]
            took = time.monotonic() - t0
            assert vals == [2] * 5, f"stale value must be served while refreshing: {vals}"
            assert took < 0.4, f"stale read blocked on the COUNT ({took:.2f}s)"
            deadline = time.monotonic() + 10
            fresh = None
            while time.monotonic() < deadline:
                with Session(engine) as s:
                    fresh = tm_counts.count(s, pid)
                if fresh == 3:
                    break
                time.sleep(0.05)
            assert fresh == 3, f"refresh never landed ({fresh})"
            assert probe.calls == 1, f"5+ stale callers started {probe.calls} refreshes"
    finally:
        tm_counts.TTL_SECONDS = old_ttl
    print("  stale value served at once, one background refresh, new value lands: OK")


def test_lru_bound():
    tm_counts._reset_for_tests()
    old = tm_counts.MAX_KEYS
    try:
        tm_counts.MAX_KEYS = 3
        with Session(engine) as s:
            for pid in range(900, 906):
                tm_counts.count(s, pid)
        assert len(tm_counts._cache) == 3, len(tm_counts._cache)
        assert ("seg", 905) in tm_counts._cache and ("seg", 900) not in tm_counts._cache
    finally:
        tm_counts.MAX_KEYS = old
    print("  cache is LRU-bounded: OK")


def test_budget_meter_cached_for_reads_only():
    """(7) The project GET's budget meter (budget_state) is served from a short cache, so
    it no longer re-prices every Job of the month per hit; a job finish (the
    evaluate_thresholds hook) and a budget change make the next read live; the gates keep
    reading the live figure."""
    from app.services import project_budgets

    pid = _mk_project("tmcc-budget")
    project_budgets.forget_budget_state()
    real = project_budgets.budget_state
    calls = {"n": 0}

    def counted(session, project_id, now=None, **kw):
        calls["n"] += 1
        return real(session, project_id, now, **kw)

    project_budgets.budget_state = counted
    try:
        client = _client()
        b1 = client.get(f"/api/projects/{pid}").json()["budget"]
        assert b1["has_budget"] is False, b1
        assert calls["n"] == 1, calls
        client.get(f"/api/projects/{pid}")
        client.get(f"/api/projects/{pid}")
        assert calls["n"] == 1, f"repeat GETs re-priced the month: {calls}"
        with Session(engine) as s:
            project_budgets.upsert_project_budget(s, pid, 50.0, actor="tmcc")
        b2 = client.get(f"/api/projects/{pid}").json()["budget"]
        assert b2["has_budget"] is True and calls["n"] == 2, (b2, calls)
        project_budgets.evaluate_thresholds(pid)
        client.get(f"/api/projects/{pid}")
        assert calls["n"] == 3, f"a finished job must refresh the meter: {calls}"
        # The gate never reads the cache.
        ok, _spent, _why = project_budgets.project_check(pid)
        assert ok is True
    finally:
        project_budgets.budget_state = real
        project_budgets.forget_budget_state()
    print("  budget meter cached for reads; budget change and job finish refresh it: OK")


def test_budget_index_reconciles_with_live_spend():
    """(8) The read meter's shared month index prices every project exactly like the live
    per-project scan the gates use: first-hit attribution by payload project_id, payload
    document_id, result document_id, and a job whose FIRST key names another project."""
    from datetime import datetime

    from app.models import Document, Job, JobStatus
    from app.services import project_budgets

    pa, pb = _mk_project("tmcc-idx-a"), _mk_project("tmcc-idx-b")
    with Session(engine) as s:
        da = Document(project_id=pa, filename="a.txt", source_text="x")
        db_ = Document(project_id=pb, filename="b.txt", source_text="y")
        s.add(da)
        s.add(db_)
        s.commit()
        s.refresh(da)
        s.refresh(db_)
        now = datetime.utcnow()
        for payload, result, usd in (
            ({"project_id": pa}, {}, 1.25),
            ({"document_id": da.id}, {}, 2.0),
            ({}, {"document_id": db_.id}, 0.5),
            ({"project_id": pb, "document_id": da.id}, {}, 4.0),  # first key wins: pb
            ({"document_id": 10**30}, {}, 9.0),  # hostile id: attributes nowhere
        ):
            s.add(
                Job(
                    kind="translate_document",
                    status=JobStatus.COMPLETED,
                    payload=payload,
                    result=result,
                    cost_usd=usd,
                    created_at=now,
                    finished_at=now,
                )
            )
        s.commit()
        project_budgets._index.clear()
        for pid, want in ((pa, 3.25), (pb, 4.5)):
            live = project_budgets.budget_state(s, pid)["ai_usd"]
            shown = project_budgets.budget_state(s, pid, display=True)["ai_usd"]
            assert abs(live - want) < 1e-9 and abs(shown - live) < 1e-9, (pid, live, shown, want)
    print("  the read meter's month index reconciles with the live per-project spend: OK")


def main():
    init_db()
    test_value_and_get_shape_and_no_recount()
    test_import_and_delete_show_at_once()
    test_concurrent_first_callers_one_count()
    test_stale_served_while_one_refresh_runs()
    test_lru_bound()
    test_budget_meter_cached_for_reads_only()
    test_budget_index_reconciles_with_live_spend()
    print("TM COUNT CACHE OK")


if __name__ == "__main__":
    main()
