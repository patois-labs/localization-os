"""Issue #613: per-engine translate-job concurrency (offline, mock engines only).

- config.max_parallel_jobs is validated on create and update (1..64, blank clears, judges refused)
- the gate caps concurrent translate jobs per engine, never above the global worker cap
- an engine with no setting (or a non-engine provider) takes no slot: behavior unchanged
- the real translate_document handler runs inside the gate, resolving payload provider or the
  project default
"""

import os
import sys
import threading
import time
from io import BytesIO

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(_ROOT)
sys.path.insert(0, _ROOT)
from tests._bootstrap import init_env

init_env("loctms_jobpar_")

from docx import Document as Docx
from fastapi.testclient import TestClient

from app.db import init_db
from app.main import app
from app.services import engines, jobs, translate


def _docx(paragraphs):
    d = Docx()
    for p in paragraphs:
        d.add_paragraph(p)
    b = BytesIO()
    d.save(b)
    return b.getvalue()


def _raises(fn, needle):
    try:
        fn()
    except ValueError as e:
        assert needle in str(e), str(e)
        return
    raise AssertionError(f"expected ValueError containing {needle!r}")


def _peak(provider: str, n_jobs: int) -> int:
    """Run n_jobs concurrent gated sections for `provider`; return the peak concurrency."""
    lock = threading.Lock()
    state = {"now": 0, "peak": 0}

    def work():
        with jobs._EngineJobSlot(provider):
            with lock:
                state["now"] += 1
                state["peak"] = max(state["peak"], state["now"])
            time.sleep(0.15)
            with lock:
                state["now"] -= 1

    threads = [threading.Thread(target=work) for _ in range(n_jobs)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(20)
        assert not t.is_alive(), "gate deadlocked"
    assert not jobs._engine_slots_busy, f"slots leaked: {jobs._engine_slots_busy}"
    return state["peak"]


def test_validation():
    p = engines.create_profile(
        "Loc4", "local", base_url="http://127.0.0.1:9", cfg={"max_parallel_jobs": "4"}
    )
    assert p.config["max_parallel_jobs"] == 4, p.config
    assert engines.max_parallel_jobs_for(f"engine:{p.id}") == 4
    assert engines.max_parallel_jobs_for(f"ENGINE:{p.id}") == 4  # same parse as the resolver
    for bad in ("0", "65", "abc", "2.5", True, -1):
        _raises(
            lambda b=bad: engines.create_profile(
                f"bad{b}", "local", base_url="http://127.0.0.1:9", cfg={"max_parallel_jobs": b}
            ),
            "max_parallel_jobs must be a whole number from 1 to 64",
        )
    # update: set, keep other keys, blank clears
    q = engines.update_profile(p.id, cfg={"max_parallel_jobs": "3", "note": "x"})
    assert q.config["max_parallel_jobs"] == 3 and q.config["note"] == "x", q.config
    q = engines.update_profile(p.id, cfg={"max_parallel_jobs": ""})
    assert "max_parallel_jobs" not in q.config and q.config["note"] == "x", q.config
    assert engines.max_parallel_jobs_for(f"engine:{p.id}") is None
    _raises(lambda: engines.update_profile(p.id, cfg={"max_parallel_jobs": "0"}), "from 1 to 64")
    # a quality judge never runs translate jobs: refused, not silently ignored
    _raises(
        lambda: engines.create_profile(
            "J", "jev_local", base_url="http://127.0.0.1:9", cfg={"max_parallel_jobs": 2}
        ),
        "not quality judges",
    )
    # non-engine providers and unknown engines carry no limit
    assert engines.max_parallel_jobs_for("mock") is None
    assert engines.max_parallel_jobs_for("engine:999999") is None
    assert engines.max_parallel_jobs_for(None) is None
    print("  config.max_parallel_jobs validated on create/update; blank clears; judges refused")
    return p


def test_gate(p):
    ref = f"engine:{p.id}"
    old_workers = jobs.WORKER_COUNT
    try:
        engines.update_profile(p.id, cfg={"max_parallel_jobs": "4"})
        jobs.WORKER_COUNT = 4
        assert _peak(ref, 8) == 4, "limit 4 under 4 workers should allow 4 at once"
        jobs.WORKER_COUNT = 2  # the global cap wins: the setting can only lower
        assert _peak(ref, 8) == 2, "per-engine limit must never exceed the worker cap"
        engines.update_profile(p.id, cfg={"max_parallel_jobs": "1"})
        jobs.WORKER_COUNT = 4
        assert _peak(ref, 4) == 1, "limit 1 serializes the engine"
        engines.update_profile(p.id, cfg={"max_parallel_jobs": ""})
        assert _peak(ref, 4) == 4, "an engine with no setting takes no slot"
        assert _peak("mock", 4) == 4, "a non-engine provider takes no slot"
        assert _peak("", 3) == 3
    finally:
        jobs.WORKER_COUNT = old_workers
    print("  gate caps per engine, clamps to the worker cap, leaves unset engines alone")


def test_handler_uses_gate():
    """The real handler resolves the engine (payload provider, else project default)."""
    c = TestClient(app)
    r = c.post(
        "/projects",
        data={
            "name": "jobpar",
            "source_locale": "en-US",
            "target_locales": "fr-FR",
            "default_provider": "mock",
        },
        follow_redirects=False,
    )
    pid = int(r.headers["location"].rsplit("/", 1)[-1])
    r = c.post(
        f"/projects/{pid}/documents",
        data={"mode": "segmented"},
        files={
            "file": (
                "d.docx",
                _docx(["Alpha beta gamma.", "Delta epsilon."]),
                "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            )
        },
        follow_redirects=False,
    )
    doc_id = int(r.headers["location"].rsplit("/", 1)[-1])
    assert jobs._translate_job_engine(doc_id, {}) == "mock"
    assert jobs._translate_job_engine(doc_id, {"provider": "Engine:7"}) == "engine:7"
    assert jobs._translate_job_engine(999999, {}) == ""

    lock = threading.Lock()
    state = {"now": 0, "peak": 0}
    real_stream = translate.pretranslate_stream
    real_limit = engines.max_parallel_jobs_for
    old_workers = jobs.WORKER_COUNT

    def slow_stream(session, d, loc, spec=None, **kw):
        with lock:
            state["now"] += 1
            state["peak"] = max(state["peak"], state["now"])
        time.sleep(0.2)
        with lock:
            state["now"] -= 1
        yield {"type": "segment"}
        yield {"type": "summary", "translated": 1, "errors": 0}

    def run(limit):
        state["peak"] = 0
        engines.max_parallel_jobs_for = lambda prov: limit if prov == "mock" else None
        ts = [
            threading.Thread(
                target=jobs._run_translate_document,
                args=({"document_id": doc_id, "locale": "fr-FR"}, lambda *a, **k: None),
            )
            for _ in range(4)
        ]
        for t in ts:
            t.start()
        for t in ts:
            t.join(30)
            assert not t.is_alive()
        return state["peak"]

    translate.pretranslate_stream = slow_stream
    try:
        jobs.WORKER_COUNT = 4
        assert run(2) == 2, "handler must run inside the per-engine gate"
        assert run(None) == 4, "no setting: unchanged, all four at once"
    finally:
        translate.pretranslate_stream = real_stream
        engines.max_parallel_jobs_for = real_limit
        jobs.WORKER_COUNT = old_workers
    print("  translate_document handler is gated by its resolved engine")
    return doc_id


def test_no_starvation(doc_id):
    """Review: a capped engine's backlog must not pin the workers. With the engine's one slot
    held, queued translate jobs go back to QUEUED (no worker held) and another job kind still
    runs; once the slot frees they all finish. Before the fix each translate job blocked its
    worker, so the probe job never ran."""
    real_stream = translate.pretranslate_stream
    real_limit = engines.max_parallel_jobs_for
    ran = []

    def quick_stream(session, d, loc, spec=None, **kw):
        yield {"type": "segment"}
        yield {"type": "summary", "translated": 1, "errors": 0}

    jobs.HANDLERS["probe_613"] = lambda payload, progress: ran.append(1) or {"ok": True}
    translate.pretranslate_stream = quick_stream
    engines.max_parallel_jobs_for = lambda prov: 1 if prov == "mock" else None
    holder = jobs._EngineJobSlot("mock")
    holder.__enter__()  # the engine's single slot is busy (a long running job)
    try:
        tids = [
            jobs.submit("translate_document", {"document_id": doc_id, "locale": "fr-FR"})
            for _ in range(jobs.WORKER_COUNT + 2)
        ]
        time.sleep(1.5)  # every worker has claimed (and must have bounced) a translate job
        pid = jobs.submit("probe_613", {})
        deadline = time.time() + 10
        while time.time() < deadline and jobs.get(pid).status != jobs.JobStatus.COMPLETED:
            time.sleep(0.1)
        assert jobs.get(pid).status == jobs.JobStatus.COMPLETED, (
            "a capped engine's backlog starved another job kind (workers pinned)"
        )
        for t in tids:
            assert jobs.get(t).status == jobs.JobStatus.QUEUED, jobs.get(t).status
    finally:
        holder.__exit__(None, None, None)
    deadline = time.time() + 30
    while time.time() < deadline and any(
        jobs.get(t).status != jobs.JobStatus.COMPLETED for t in tids
    ):
        time.sleep(0.2)
    try:
        for t in tids:
            assert jobs.get(t).status == jobs.JobStatus.COMPLETED, (t, jobs.get(t).status)
        assert not jobs._engine_slots_busy, f"slots leaked: {jobs._engine_slots_busy}"
    finally:
        translate.pretranslate_stream = real_stream
        engines.max_parallel_jobs_for = real_limit
        jobs.HANDLERS.pop("probe_613", None)
    print("  capped-engine backlog requeues instead of pinning workers; other kinds run")


def _quick_stream(session, d, loc, spec=None, **kw):
    yield {"type": "segment"}
    yield {"type": "summary", "translated": 1, "errors": 0}


def _wait_done(ids, seconds=30):
    deadline = time.time() + seconds
    while time.time() < deadline and any(
        jobs.get(t).status != jobs.JobStatus.COMPLETED for t in ids
    ):
        time.sleep(0.2)
    for t in ids:
        assert jobs.get(t).status == jobs.JobStatus.COMPLETED, (t, jobs.get(t).status)


def test_deferred_job_never_shows_running(doc_id):
    """Gate: a job waiting for its engine's slot stays QUEUED on every status surface. Before
    the gate fix each deferral cycle wrote RUNNING then QUEUED (about once a second per idle
    worker), so a poller saw it flap and the job-status metric counted a start per cycle."""
    real_stream = translate.pretranslate_stream
    real_limit = engines.max_parallel_jobs_for
    real_update = jobs._update
    writes = []

    def spy(jid, **fields):
        if "status" in fields:
            writes.append((jid, fields["status"]))
        return real_update(jid, **fields)

    translate.pretranslate_stream = _quick_stream
    engines.max_parallel_jobs_for = lambda prov: 1 if prov == "mock" else None
    jobs._update = spy
    holder = jobs._EngineJobSlot("mock")
    holder.__enter__()
    try:
        tid = jobs.submit("translate_document", {"document_id": doc_id, "locale": "fr-FR"})
        time.sleep(2.5)  # several deferral cycles
        mine = [st for j, st in writes if j == tid]
        assert jobs.JobStatus.RUNNING not in mine, f"deferred job flapped through RUNNING: {mine}"
        job = jobs.get(tid)
        assert job.status == jobs.JobStatus.QUEUED and job.started_at is None, job.status
        assert job.message == jobs._DEFER_MESSAGE, job.message
    finally:
        holder.__exit__(None, None, None)
        jobs._update = real_update
    try:
        _wait_done([tid])
        assert not jobs._engine_slots_busy, f"slots leaked: {jobs._engine_slots_busy}"
    finally:
        translate.pretranslate_stream = real_stream
        engines.max_parallel_jobs_for = real_limit
    print("  a deferred job stays QUEUED (no RUNNING flap) and runs once its slot frees")


def test_db_queue_requeue(doc_id):
    """Gate: the durable DB queue. A deferral returns the claim to the TAIL in one step: no
    'failed' dispatch row, exactly one row per job, attempts kept. Before the gate fix the
    worker did enqueue + release, and release (keyed on job_id) marked the old claim 'failed'
    and could also fail a second worker's fresh claim of the new row mid-run, which crash
    recovery then skips (the Job stuck RUNNING)."""
    from sqlmodel import Session, select

    from app import ports
    from app.db import engine as db_engine
    from app.models import Job, JobQueue, JobQueueState
    from app.ports import jobqueue

    q = jobqueue.DbJobQueue(poll_seconds=0.05)

    def rows(jid):
        with Session(db_engine) as s:
            return list(
                s.exec(select(JobQueue).where(JobQueue.job_id == jid).order_by(JobQueue.id))
            )

    # unit: requeue moves a claim behind the backlog in one row
    with Session(db_engine) as s:
        a, b = Job(kind="probe_613b", payload={}), Job(kind="probe_613b", payload={})
        s.add(a)
        s.add(b)
        s.commit()
        aid, bid = a.id, b.id
    q.enqueue(aid)
    q.enqueue(bid)
    assert q.claim("w1", timeout=1) == aid
    q.requeue(aid)
    ra = rows(aid)
    assert len(ra) == 1 and ra[0].state == JobQueueState.PENDING, [(r.state) for r in ra]
    assert ra[0].attempts == 1, ra[0].attempts
    assert ra[0].id > rows(bid)[0].id, "requeued claim must go behind the backlog"
    assert q.claim("w2", timeout=1) == bid, "FIFO: the older job is taken first"
    assert q.claim("w3", timeout=1) == aid
    q.release(bid)
    q.release(aid)
    print("  DbJobQueue.requeue: one pending row at the tail, attempts kept, FIFO preserved")

    # integration: real workers on the DB queue with a capped engine
    real_stream = translate.pretranslate_stream
    real_limit = engines.max_parallel_jobs_for
    translate.pretranslate_stream = _quick_stream
    engines.max_parallel_jobs_for = lambda prov: 1 if prov == "mock" else None
    mem_q = jobqueue.get()
    assert isinstance(mem_q, jobqueue.InMemoryJobQueue), type(mem_q).__name__
    ports.register("jobqueue", q)
    try:
        # wake the workers blocked on the in-memory queue (an unknown id is a no-op run) so
        # their next claim goes to the DB queue
        for _ in range(jobs.WORKER_COUNT):
            mem_q.enqueue(-1)
        time.sleep(0.5)
        holder = jobs._EngineJobSlot("mock")
        holder.__enter__()
        try:
            tids = [
                jobs.submit("translate_document", {"document_id": doc_id, "locale": "fr-FR"})
                for _ in range(3)
            ]
            time.sleep(3)  # several deferral cycles on the DB queue
            for t in tids:
                rs = rows(t)
                assert all(r.state != JobQueueState.FAILED for r in rs), [r.state for r in rs]
                assert len(rs) == 1, f"job {t} has {len(rs)} dispatch rows"
                assert jobs.get(t).status == jobs.JobStatus.QUEUED
        finally:
            holder.__exit__(None, None, None)
        _wait_done(tids)
        for t in tids:
            rs = rows(t)
            assert [r.state for r in rs] == [JobQueueState.DONE], [r.state for r in rs]
        assert not jobs._engine_slots_busy, f"slots leaked: {jobs._engine_slots_busy}"
    finally:
        translate.pretranslate_stream = real_stream
        engines.max_parallel_jobs_for = real_limit
    print("  DB queue: deferred jobs keep one live row, never 'failed', all finish 'done'")


def main() -> None:
    init_db()
    p = test_validation()
    test_gate(p)
    doc_id = test_handler_uses_gate()
    test_no_starvation(doc_id)
    test_deferred_job_never_shows_running(doc_id)
    test_db_queue_requeue(doc_id)  # last: the workers stay on the DB queue afterwards
    print("JOB ENGINE PARALLEL OK")


if __name__ == "__main__":
    main()
