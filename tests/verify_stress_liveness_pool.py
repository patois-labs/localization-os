"""GitHub #99 + #100 (both sev:high, 2026-09-06 night-stress) — the liveness / DB-pool
capacity train. Hermetic + offline: throwaway LOC_TMS_DATA_DIR, blanked provider keys,
every engine call served by an injected delayed mock. No network, no real LLM.

WHAT THE FLOOD FOUND (the planning notes and
-L5-prod.md). Eight concurrent classic runs on an install carrying SQLAlchemy's DEFAULT
pool (size 5 + overflow 10 = 15 connections, 30s timeout):

  * every in-flight run pins ONE pooled connection for its whole duration (the
    orchestrator opens a Session and holds it across every provider call), so the pool
    was spoken for before a single request arrived;
  * `providers.get_provider` re-read the EngineProfile row PER SEGMENT -- twice -- so a
    654-segment document was 1300+ extra checkouts inside one step, from every
    mt_concurrency worker of every run (#100's "amplifier"), and ONLY on the
    `engine:<id>` refs the UI steers operators toward;
  * runs then crashed on `QueuePool limit of size 5 overflow 10 reached`, /api returned
    500 at exactly 30.2s instead of the designed retryable 503, failed runs recorded NO
    reason (the crash handler needed a pooled connection of its own to write one, and
    could not get it), and twelve runs were left RUNNING at step 0 forever;
  * and the DB-FREE /health stalled 20-145s -- because every waiting caller pinned an
    anyio worker thread for the full 30s and the shared threadpool filled. On prod that
    was 35 minutes of 503s ending in an OOM kill.

GROUPS
  [A] CAPACITY IS CONFIGURED, not defaulted: the pool is sized from config, its timeout
      is a short FAILURE budget (not SQLAlchemy's 30s), total capacity comfortably exceeds
      the runs the admission gate will admit, the BOOT GUARD clamps a cap the pool cannot
      carry (proven in fresh processes), a negative cap is refused rather than silently
      turning the gate off, and every connection carries the configured page cache.
  [E2] THE WEB SURFACE under an exhausted pool: an honest 503 with a Retry-After and a
      readable body in milliseconds, rendered with ZERO database reads, and ZERO checkout
      timeouts spent; plus the per-request rule that one request may wait once per STALL
      and the minutes-long scopes (SSE, `wait=true`) that opt out of it entirely.
  [E2c] THE DOORMAN'S RELIEF VALVE: a wedged pool still lets an operator cancel a run or a
      batch and reach the settings that hold the dials, everything else stays shed, and the
      shed log is rate-limited to one line a minute carrying the suppressed count.
  [I] A RESERVATION IS OWNED ONCE: a launch whose thread cannot start returns its own slot
      exactly once and never a live run's.
  [J] DURABLE FAN-OUT ADMISSION: every plan row exists before execution, the shared
      scheduler counts only admitted executions, and keys/batches queue at zero capacity.
      Admission failures retain named row outcomes without leaking execution slots.
  [K] THE MCP RELAY of both capacity reasons and of a reason-less 503.
  [L] CAPACITY IS VISIBLE on a surface: five `/metrics` gauges carry the effective cap, what
      the operator configured, the in-flight count and the pool's checked-out/total.
  [B] THE FLOOD REPRO: 8 concurrent classic runs over the REAL /api, each engine call
      slow, each resolving an `engine:<id>` profile per segment, while /health and /ready
      are probed throughout. Every probe answers inside its budget, no launch 5xxs, no
      run dies of pool exhaustion, and NO run is left non-terminal at step 0.
  [B2] THE FLOOD REPRO AT THE BREAK POINT: more simultaneous long-lived runs than the
      pool has connections. THIS IS THE GROUP THAT FAILS ON MAIN -- measured on main
      @ a5964b50 with this exact fixture: six of the twenty `POST /api/workflows/{id}/run`
      calls raised `QueuePool limit of size 5 overflow 10 reached` straight at the caller
      after 180.6s (a 500 on a real server), `/health` peaked at 180.58s and `/ready` at
      10.86s (the platform budget is 5s), and a run sat FAILED at step 0 carrying
      `Run failed: TimeoutError: QueuePool ...`. The whole train is judged here: the
      install must EITHER carry the load or refuse it at the door with the designed
      503 -- never 500, never a stalled probe, never a run frozen mid-flight.
  [C] LIVENESS vs. A SATURATED THREADPOOL (#99's mechanism): with every anyio worker
      thread pinned by blocked sync handlers, /health -- which touches no database --
      still answers inside its budget, because it is a pure async endpoint that never
      enters the threadpool at all.
  [D] /ready ANSWERS, ALWAYS, AND A BUSY INSTALL IS NOT AN UNREADY ONE: with the connection
      pool fully checked out it answers 200 with `pool: at_capacity` inside
      config.READY_PROBE_TIMEOUT_S (never blocking for the pool timeout, and never taking
      a shedding-but-healthy machine out of the proxy's set), and it still 503s when the
      DATABASE itself cannot answer.
  [E] THE DESIGNED 503, NOT A 500: pool exhaustion and a capacity refusal both render as
      {ok:false, error, reason, retry_after} + a Retry-After header, each with its own
      machine-readable `reason`, and both stay distinguishable from a validation 4xx.
  [F] THE ADMISSION GATE: at capacity a launch is refused BEFORE any row is written (no
      orphan frozen at step 0), the slot is returned when the run ends, and a re-attach
      never consumes one.
  [G] THE HONEST REASON: a run whose execution dies on a pool timeout is FAILED and
      carries POOL_EXHAUSTED_NOTE -- even when the crash handler itself cannot get a
      pooled connection, which is exactly when it matters.
  [H] THE AMPLIFIER IS GONE: N provider resolutions over one engine profile issue ONE
      profile read, and a write to that profile is visible on the very next read.

Final line: "STRESS LIVENESS POOL OK".
"""

import os
import re
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(_ROOT)
sys.path.insert(0, _ROOT)
from tests._api_cli_patch import patch_api_cli
from tests._bootstrap import init_env
from tests._piece_patch import monkeypatch_pieces, patch_pieces, set_pieces

init_env("loctms_stresspool_")

import sqlalchemy.exc as sa_exc
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlmodel import Session, select

from app import config
from app import db as _db
from app.db import engine, init_db
from app.main import app
from app.models import (
    Document,
    EngineProfile,
    Project,
    ProjectType,
    RunStatus,
    Segment,
    SegStatus,
    TargetLocale,
    TransMode,
    WorkflowRun,
)
from app.services import apikeys, orchestrator, providers, security, settings
from app.services import engines as engines_svc
from app.services import workflows as W
from app.services.providers import Provider

init_db()
settings.set_many({"security_auth_required": "0", "multi_org_enabled": "0", "project_scoping": "0"})
settings.invalidate_cache()

_N = 0
#: The admin fixture's password. It must pass the password policy (the old literal
#: "password-1234!!" is refused as too common since the password-policy train), and
#: verify_memory_bound reuses _api_key, so a refusal here reddens that suite too.
FIXTURE_PASSWORD = "Stress-Liveness-Fixture-7q!"  # pregate: allow-secret test-only fixture value


def _uniq(prefix="x"):
    global _N
    _N += 1
    return f"{prefix}-{_N}"


def _api_key(scopes="api docs"):
    admin = security.create_user(_uniq("stress") + "@x.io", _uniq("u"), "admin", FIXTURE_PASSWORD)
    return apikeys.create_key(admin.id, _uniq("key"), scopes=scopes)[1]


# --------------------------------------------------------------------------- #
# fixtures
# --------------------------------------------------------------------------- #
class SlowProvider(Provider):
    """A MockProvider-shaped engine whose calls take real wall-clock time — the flood's
    defining property. A local LLM under contention answered in 60-90s; the point is not
    the number but that the run HOLDS its pooled connection for the whole call."""

    name = "slowmock"
    delay = 0.12

    def complete(self, prompt, system=None, task=None, payload=None, **kw):
        time.sleep(SlowProvider.delay)
        return "ok " + ((payload or {}).get("text", "") or "x")


def _engine_profile() -> int:
    """A LIVE `engine:<id>` profile. This ref matters: the built-in local/claude/gemini
    names resolve through an in-memory map and never touch the DB, so only an engine
    profile reproduces #100's per-segment amplifier."""
    prof = engines_svc.create_profile(
        name=_uniq("StressEngine"), kind="local", base_url="http://127.0.0.1:9/v1", model="m1"
    )
    return prof.id


def _mk_doc(locale: str, n_segments: int) -> int:
    with Session(engine) as s:
        p = Project(name=_uniq("StressProj"), source_locale="en-US")
        s.add(p)
        s.commit()
        s.refresh(p)
        s.add(TargetLocale(project_id=p.id, locale=locale))
        doc = Document(project_id=p.id, filename="d.txt", mode=TransMode.SEGMENTED)
        s.add(doc)
        s.commit()
        s.refresh(doc)
        for i in range(n_segments):
            s.add(
                Segment(
                    document_id=doc.id,
                    target_locale=locale,
                    order_index=i,
                    source_text=f"Sentence number {i}.",
                    status=SegStatus.NEW,
                )
            )
        s.commit()
        return doc.id


def _mk_wf(provider_ref: str) -> int:
    with Session(engine) as s:
        w = W.create_workflow(s, _uniq("StressWf"))
        W.add_step(
            s,
            w.id,
            name="Translate",
            provider=provider_ref,
            model="m1",
            prompt_text="Translate {text}",
            chunk_policy="none",
        )
        return w.id


def _boot_config(env: dict) -> dict:
    """Import `app.config` in a FRESH process under `env` and report what it resolved.

    The capacity dials are import-time values, so the boot guard can only be observed by
    booting. Returns the resolved cap, what the operator asked for, and stderr (the
    warnings are the operator-facing half of the guard)."""
    child = dict(os.environ)
    child.update({k: str(v) for k, v in env.items()})
    code = (
        "import app.config as c;"
        "print('CAP', c.MAX_CONCURRENT_RUNS);"
        "print('CONFIGURED', c.MAX_CONCURRENT_RUNS_CONFIGURED)"
    )
    proc = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        cwd=_ROOT,
        env=child,
        timeout=120,
    )
    assert proc.returncode == 0, ("app.config must always boot", proc.stdout, proc.stderr)
    out = dict(
        line.split(" ", 1) for line in proc.stdout.splitlines() if line.startswith(("CAP", "CONF"))
    )
    return {
        "MAX_CONCURRENT_RUNS": int(out["CAP"]),
        "CONFIGURED": int(out["CONFIGURED"]),
        "stderr": proc.stderr,
    }


def _pool_capacity() -> int:
    """Every connection this pool can hand out, read from the POOL — not from the config
    names this train introduced. Gate A round 1 LOW-8: a probe that drains the pool must
    drain it on main too, or the "measured on main" claim in a docstring is really just
    "AttributeError on main"."""
    pool = engine.pool
    return int(pool.size()) + int(getattr(pool, "_max_overflow", 0))


def _poolless_read(run_id: int):
    """(status, notes) for a run, read over a connection that owes the QueuePool nothing.

    Built here rather than through `db.emergency_session` so [G] runs — and FAILS on its
    real assertion — against the pre-fix tree as well (LOW-8)."""
    from sqlalchemy import create_engine as _ce
    from sqlalchemy.pool import NullPool as _NullPool

    from app.services import dbcrypt as _dbcrypt

    eng = _ce(
        config.DB_URL,
        poolclass=_NullPool,
        connect_args={"check_same_thread": False} if config.IS_SQLITE else {},
        **_dbcrypt.engine_kwargs(config.DB_PATH),
    )
    try:
        with Session(eng) as s:
            row = s.get(WorkflowRun, run_id)
            return None if row is None else (row.status, row.notes or "")
    finally:
        eng.dispose()


def _invalidate_memo() -> None:
    """Drop the engine-profile memo if this tree has one (absent on main — LOW-8)."""
    getattr(engines_svc, "invalidate_profile_cache", lambda *a, **k: None)()


def _wait_slots_idle(timeout=30.0) -> int:
    """Let previously launched executions drain so a capacity test starts from zero."""
    slots = getattr(orchestrator, "RUN_SLOTS", None)
    if slots is None:
        return 0
    deadline = time.time() + timeout
    while time.time() < deadline and slots.in_flight:
        time.sleep(0.05)
    return slots.in_flight


def _wait_terminal(run_ids, timeout=90.0):
    """Return {run_id: status} once every run is terminal (or the timeout expires)."""
    deadline = time.time() + timeout
    terminal = (RunStatus.COMPLETED, RunStatus.FAILED, RunStatus.CANCELLED, RunStatus.PAUSED)
    while time.time() < deadline:
        with Session(engine) as s:
            rows = {rid: s.get(WorkflowRun, rid) for rid in run_ids}
        if all(r is not None and r.status in terminal for r in rows.values()):
            return {rid: (r.status, r.notes or "", r.current_step_index) for rid, r in rows.items()}
        time.sleep(0.1)
    with Session(engine) as s:
        rows = {rid: s.get(WorkflowRun, rid) for rid in run_ids}
    return {
        rid: (
            (r.status if r else None),
            (r.notes if r else "") or "",
            (r.current_step_index if r else -1),
        )
        for rid, r in rows.items()
    }


# --------------------------------------------------------------------------- #
# [A] capacity is CONFIGURED, not SQLAlchemy's default
# --------------------------------------------------------------------------- #
def test_pool_is_sized_for_the_run_concurrency_it_admits():
    pool = engine.pool
    size, overflow = pool.size(), pool._max_overflow
    timeout = pool._timeout
    assert (size, overflow) != (5, 10), (
        "the install must not run on SQLAlchemy's default 5+10 pool — that is 15 "
        "connections for a process that pins one PER IN-FLIGHT RUN (#100)",
        (size, overflow),
    )
    assert size == config.DB_POOL_SIZE and overflow == config.DB_MAX_OVERFLOW, (
        "pool sizing must come from config (an operator dial), not a library default",
        (size, overflow, config.DB_POOL_SIZE, config.DB_MAX_OVERFLOW),
    )
    assert timeout == config.DB_POOL_TIMEOUT_S and timeout <= 10, (
        "pool_timeout is a FAILURE budget, not a queue: 30s of a pinned worker thread "
        "(or event loop) per caller is how a DB-free /health stalls for minutes (#99)",
        timeout,
    )
    cap = config.MAX_CONCURRENT_RUNS
    assert cap > 0, "the admission gate must be ON by default"
    per_run = config.DB_CONNECTIONS_PER_RUN
    assert per_run >= 2, (
        "ONE rule, stated once (gate A round 2, MED-3). An admitted run pins a connection "
        "for its whole duration AND borrows a second for its own workers; at one-per-run "
        "the boot guard was willing to clamp to a cap where every connection but the "
        "headroom is spoken for, which makes `pool_has_no_capacity()` the STEADY state and "
        "the doorman shed everything forever",
        per_run,
    )
    assert size + overflow >= cap * per_run, (
        "total pool capacity must comfortably exceed the runs the gate admits — each "
        "run holds one connection for its whole life, plus its worker threads' churn",
        (size + overflow, cap, per_run),
    )
    # THE RULE, not just the shipped defaults (gate A round 1, MED-2). Asserting the
    # relationship only for the values this repo happens to ship protects nobody: the
    # concrete failing input was `LOC_TMS_MAX_CONCURRENT_RUNS=60` against the default
    # 20+40 pool, which booted silently into exactly the pre-fix exhaustion.
    floor = config._pool_capacity_floor
    assert floor(20, 40, 10) == 25, floor(20, 40, 10)
    assert floor(5, 40, 10) == 17, floor(5, 40, 10)
    assert floor(5, 10, 10) == 2, floor(5, 10, 10)
    assert floor(5, 0, 10) == 1, (
        "a pool too small for ANY headroom must still carry one run, never zero",
        floor(5, 0, 10),
    )
    # THE GUARD IS AT LEAST AS STRONG AS THE INVARIANT THIS SUITE ASSERTS (round 2, MED-3).
    # Round 1 shipped a guard using `size + overflow - headroom` (one connection per run)
    # while this very test asserted `>= cap * 2` of the live config, so a clamped install
    # could satisfy the guard and fail the invariant in the same process. Checked over a
    # spread of pool shapes, not just the shipped one.
    for _sz, _ov, _hr in ((20, 40, 10), (5, 40, 10), (5, 10, 10), (5, 0, 10), (80, 40, 10)):
        _ceiling = floor(_sz, _ov, _hr)
        assert _ceiling * per_run <= _sz + _ov or _ceiling == 1, (
            "a cap the guard is willing to clamp TO must itself satisfy the sizing rule",
            (_sz, _ov, _hr, _ceiling, per_run),
        )
    assert cap <= floor(size, overflow, config.DB_REQUEST_HEADROOM), (
        "the live cap must satisfy the rule the boot guard enforces",
        (cap, size, overflow, config.DB_REQUEST_HEADROOM),
    )
    # DOC-TRUTH ON PROSE ARITHMETIC (gate A round 2, MED-4). `configuration.md` published a
    # worked example of this clamp that was off by 34 — it claimed a ceiling of 1 where the
    # code computes 35 — and `verify_docs_architecture` passed, because a ratchet over
    # citations and symbols cannot see arithmetic in a sentence. So the doc's table of
    # worked examples is RE-DERIVED here from the function itself: every row must be a
    # number this code actually produces.
    with open(
        os.path.join(_ROOT, "docs", "architecture", "configuration.md"), encoding="utf-8"
    ) as _fh:
        _doc = _fh.read()
    for _sz, _ov, _hr, _shape in (
        (20, 40, 10, "({}+{}-{})/2 = {}"),
        (5, 40, 10, "({}+{}-{})/2 = {}"),
        (5, 0, 10, "max(1, ({}+{}-{})/2) = {}"),
    ):
        _expect = floor(_sz, _ov, _hr)
        _row = _shape.format(_sz, _ov, _hr, _expect)
        assert _row in _doc, (
            "the capacity chapter's worked example must quote the ceiling this code "
            "computes; prose arithmetic is the one class the docs ratchet is blind to, "
            "and the round-1 example was off by 34",
            _row,
        )
    assert "clamps the ceiling to 1 rather than" not in _doc, (
        "the round-1 example (POOL_SIZE=5 + cap 60 'clamps the ceiling to 1') measured 35 "
        "and is 17 under the two-per-run rule; it must not come back",
    )
    # ... and the guard actually runs at boot, in a real process, over a config it has
    # never seen. Sub-process because these are import-time values.
    clamped = _boot_config({"LOC_TMS_MAX_CONCURRENT_RUNS": "60"})
    assert clamped["MAX_CONCURRENT_RUNS"] == 25 and clamped["CONFIGURED"] == 60, (
        "a cap the pool cannot carry must be CLAMPED at boot, with the operator's own "
        "value kept visible — booting silently into the pre-fix failure is the bug",
        clamped,
    )
    assert "CLAMPED" in clamped["stderr"], (
        "and the clamp must be LOUD: an operator who never sees the warning cannot know "
        "their dial did not take effect",
        clamped["stderr"][-400:],
    )
    # The clamp bounds the POOL and says so about MEMORY (round 2, MED-3): the machine this
    # train exists because of was OOM-killed, and 25 admitted runs on a 1 GB box is still
    # 25 run threads and their provider fan-out.
    assert "MEMORY" in clamped["stderr"] and "OOM" in clamped["stderr"], (
        "the clamp warning must name the memory consequence it does NOT bound, or an "
        "operator reads a pool clamp as an all-clear on the 1 GB machine that OOM-killed",
        clamped["stderr"][-600:],
    )
    # The doc's smallest worked example, in a real process: 5 + 0 with 10 headroom is the
    # one configuration where 1 really is the answer.
    tiny = _boot_config(
        {
            "LOC_TMS_DB_POOL_SIZE": "5",
            "LOC_TMS_DB_MAX_OVERFLOW": "0",
            "LOC_TMS_MAX_CONCURRENT_RUNS": "60",
        }
    )
    assert tiny["MAX_CONCURRENT_RUNS"] == 1, tiny
    small = _boot_config({"LOC_TMS_DB_POOL_SIZE": "5", "LOC_TMS_MAX_CONCURRENT_RUNS": "60"})
    assert small["MAX_CONCURRENT_RUNS"] == 17, (
        "the round-1 documentation claimed 1 here and the code produced 35; it is 17 under "
        "the two-connections-per-run rule, and the doc now says so",
        small,
    )
    ok = _boot_config({"LOC_TMS_DB_POOL_SIZE": "140", "LOC_TMS_MAX_CONCURRENT_RUNS": "60"})
    assert ok["MAX_CONCURRENT_RUNS"] == 60, (
        "raising the pool WITH the cap must be honoured — the guard bounds the pair, it "
        "does not cap the install",
        ok,
    )
    # LOW-3: a typed minus sign must not turn the gate off. 0 stays a real choice.
    neg = _boot_config({"LOC_TMS_MAX_CONCURRENT_RUNS": "-1"})
    assert neg["MAX_CONCURRENT_RUNS"] == 8, (
        "a NEGATIVE cap is a typo, not a small cap: clamping it to 0 silently DISABLES "
        "the admission gate",
        neg,
    )
    off = _boot_config({"LOC_TMS_MAX_CONCURRENT_RUNS": "0"})
    assert off["MAX_CONCURRENT_RUNS"] == 0 and "gate is OFF" in off["stderr"], off
    # The memory half of the sizing (GitHub #109): an explicit per-connection cache.
    assert config.SQLITE_CACHE_KB > 0, config.SQLITE_CACHE_KB
    if engine.dialect.name == "sqlite":
        with Session(engine) as s:
            got = s.exec(text("PRAGMA cache_size")).first()
        assert int(got[0]) == -int(config.SQLITE_CACHE_KB), (
            "every pooled connection must carry the CONFIGURED page cache, not SQLite's "
            "-2000 default — at 60 connections that default is ~120 MiB on a 1 GB machine",
            got,
        )
    print(
        f"  [A] pool {size}+{overflow} @ {timeout}s, run cap {cap} (ceiling "
        f"{floor(size, overflow, config.DB_REQUEST_HEADROOM)}), cache "
        f"{config.SQLITE_CACHE_KB} KiB/conn — sized, not defaulted"
    )


# --------------------------------------------------------------------------- #
# [B] THE FLOOD REPRO — 8 concurrent classic runs, probes throughout
# --------------------------------------------------------------------------- #
_RUNS = 8
_SEGMENTS = 24
_HEALTH_BUDGET_S = 2.0
_READY_BUDGET_S = 5.0  # the platform's own health-check timeout
#: Minimum probes a "the probes stayed alive" claim needs before it means anything (gate A
#: round 1, LOW-7). The first round asserted only "no probe was slow" over whatever sample
#: the scheduler happened to produce -- 14 in [B2] -- so a run where the prober barely ran
#: would have passed vacuously. The probe interval is tightened and the FLOOR is asserted,
#: which turns a starved prober into a failure instead of a green tick.
_MIN_PROBES = 40
#: And the probes must SPAN the window they claim to cover: a burst of samples in the first
#: 200ms proves nothing about the seconds when the runs were actually in flight.
_MIN_PROBE_SPAN_FRACTION = 0.5


def test_eight_concurrent_runs_keep_the_probes_alive_and_the_pool_intact():
    from unittest.mock import patch

    left = _wait_slots_idle()
    assert left == 0, ("start from an idle install", left)
    providers.register_provider("slowmock", SlowProvider())
    pid = _engine_profile()
    ref = f"engine:{pid}"
    # The REAL resolution path runs (memo, liveness predicate, undecryptable-key guard);
    # only the last step -- building an HTTP client -- is swapped for the offline double,
    # so the per-segment profile reads under test are the genuine ones.
    real_for_profile = providers._provider_for_profile
    set_pieces("app.services.providers", "_provider_for_profile", lambda prof: SlowProvider())
    settings.set_many({"mt_concurrency": "4"})
    settings.invalidate_cache()

    token = {"X-Loc-Token": _api_key()}
    plan = [(_mk_wf(ref), _mk_doc("es-ES", _SEGMENTS)) for _ in range(_RUNS)]

    probes: list[tuple[str, float, int]] = []
    stop = threading.Event()

    try:
        # Keep actual executions in flight long enough for the unchanged 40-sample
        # floor on Windows, where HTTP logging can dominate a 0.12s mock call.
        with patch.object(SlowProvider, "delay", 0.3), TestClient(app) as client:

            def _probe():
                while not stop.is_set():
                    for path in ("/health", "/ready"):
                        t0 = time.time()
                        try:
                            r = client.get(path)
                            code = r.status_code
                        except Exception:
                            code = -1
                        probes.append((path, time.time() - t0, code, time.time()))
                    time.sleep(0.01)

            prober = threading.Thread(target=_probe, name="probe", daemon=True)
            prober.start()

            launches: list = []

            def _launch(item):
                wid, did = item
                r = client.post(
                    f"/api/workflows/{wid}/run",
                    json={"doc_id": did, "locale": "es-ES"},
                    headers=token,
                )
                launches.append((r.status_code, r.json() if r.content else {}))

            window_t0 = time.time()
            with ThreadPoolExecutor(max_workers=_RUNS) as pool:
                list(pool.map(_launch, plan))

            codes = [c for c, _b in launches]
            assert all(c == 200 for c in codes), (
                "every launch must be admitted or honestly refused — never a 500 "
                "(#100: the flood's launches 500'd at exactly the 30s pool timeout)",
                launches,
            )
            run_ids = [b["run_id"] for _c, b in launches]
            outcomes = _wait_terminal(run_ids)
            window = time.time() - window_t0
            stop.set()
            prober.join(timeout=5)
    finally:
        stop.set()
        set_pieces("app.services.providers", "_provider_for_profile", real_for_profile)

    # 1. no run may die of pool exhaustion, and none may be left frozen mid-flight.
    for rid, (status, notes, step) in outcomes.items():
        assert status is not None, ("every launched run must exist", rid)
        assert _db.POOL_EXHAUSTED_NOTE not in notes, (
            "a run must not die of connection-pool exhaustion at this concurrency (#100)",
            (rid, notes),
        )
        assert status in (RunStatus.COMPLETED, RunStatus.PAUSED), (
            "every run must reach a real outcome — the flood left runs RUNNING at step 0 "
            "forever because the crash handler could not get a connection either",
            (rid, str(status), step, notes),
        )

    # 2. the probes stayed answerable THROUGHOUT (this is #99).
    assert len(probes) >= _MIN_PROBES, (
        "too few probes to claim the liveness surface stayed answerable — a starved "
        "prober makes 'no slow probe' vacuously true (LOW-7)",
        len(probes),
    )
    slow_health = [p for p in probes if p[0] == "/health" and p[1] > _HEALTH_BUDGET_S]
    slow_ready = [p for p in probes if p[0] == "/ready" and p[1] > _READY_BUDGET_S]
    assert not slow_health, (
        "/health does NO database work and must answer instantly under run load (#99 "
        "measured 20-145s, and prod turned that into a 35-minute outage)",
        slow_health[:5],
        len(probes),
    )
    assert not slow_ready, (
        "/ready must answer inside the health-check budget, 200 or 503 — never stall",
        slow_ready[:5],
    )
    bad = [p for p in probes if p[2] not in (200, 503)]
    assert not bad, ("a probe must answer 200 or an honest 503, never 5xx-by-crash", bad[:5])
    span = probes[-1][3] - probes[0][3]
    assert span >= (window * _MIN_PROBE_SPAN_FRACTION), (
        "the probes must COVER the window the runs occupied — a burst of samples at the "
        "start says nothing about the seconds the pool was actually under load (LOW-7)",
        (span, window),
    )
    print(
        f"  [B] {_RUNS} concurrent runs x {_SEGMENTS} segments: all terminal, "
        f"{len(probes)} probes all inside budget, 0 pool exhaustion"
    )


# --------------------------------------------------------------------------- #
# [B2] THE BREAK POINT — more simultaneous runs than the pool has connections
# --------------------------------------------------------------------------- #
_OVERSUBSCRIBE = 20


class _Barrier(Provider):
    """Every call parks until released, so all admitted runs are in flight AT ONCE —
    the state the flood reached and the state a duration-based mock cannot guarantee."""

    name = "barriermock"
    release = threading.Event()

    def complete(self, prompt, system=None, task=None, payload=None, **kw):
        _Barrier.release.wait(timeout=60)
        return "held " + ((payload or {}).get("text", "") or "x")


def test_oversubscribed_launches_are_carried_or_refused_never_wedged():
    """MEASURED ON MAIN (a5964b50, this exact fixture): 6 of 20 launches raised
    `QueuePool limit of size 5 overflow 10 reached` at the caller after 180.6s, /health
    peaked at 180.58s, /ready at 10.86s, and a run sat FAILED at step 0 with a QueuePool
    reason. Every assertion below is one of those observations turned into a law."""
    assert _wait_slots_idle() == 0, "start from an idle install"
    _Barrier.release.clear()
    providers.register_provider("barriermock", _Barrier())
    pid = _engine_profile()
    ref = f"engine:{pid}"
    real_for_profile = providers._provider_for_profile
    set_pieces("app.services.providers", "_provider_for_profile", lambda prof: _Barrier())
    settings.set_many({"mt_concurrency": "4"})
    settings.invalidate_cache()
    token = {"X-Loc-Token": _api_key()}
    plan = [(_mk_wf(ref), _mk_doc("es-ES", 2)) for _ in range(_OVERSUBSCRIBE)]

    launches: list = []
    probes: list = []
    stop = threading.Event()
    try:
        with TestClient(app) as client:

            def _probe():
                while not stop.is_set():
                    for path in ("/health", "/ready"):
                        t0 = time.time()
                        try:
                            code = client.get(path).status_code
                        except Exception:
                            code = -1
                        probes.append((path, time.time() - t0, code, time.time()))
                    time.sleep(0.01)

            prober = threading.Thread(target=_probe, name="probe2", daemon=True)
            prober.start()

            def _launch(item):
                wid, did = item
                t0 = time.time()
                try:
                    r = client.post(
                        f"/api/workflows/{wid}/run",
                        json={"doc_id": did, "locale": "es-ES"},
                        headers=token,
                    )
                    launches.append((r.status_code, time.time() - t0, r.json()))
                except Exception as exc:  # on main this is the raw QueuePool TimeoutError
                    launches.append((-1, time.time() - t0, {"exc": f"{type(exc).__name__}: {exc}"}))

            with ThreadPoolExecutor(max_workers=_OVERSUBSCRIBE) as pool:
                list(pool.map(_launch, plan))

            # 1. no launch may crash, and no launch may take the pool timeout to answer.
            for code, took, body in launches:
                assert code in (200, 503), (
                    "an over-subscribed launch is admitted or REFUSED — never a crash "
                    "(#100: main raised QueuePool TimeoutError straight at the caller)",
                    (code, took, body),
                )
                assert took < 30.0, (
                    "and it must answer promptly: a launch that blocks on a connection "
                    "checkout pins a worker thread for the whole pool timeout, which is "
                    "the mechanism behind #99's stalled probes",
                    (code, took),
                )
            refused = [b for c, _t, b in launches if c == 503]
            for body in refused:
                assert body.get("reason") == "run_capacity", (
                    "a refusal must name itself so a client can branch on it",
                    body,
                )
                assert body.get("retry_after", 0) > 0, body
            admitted = [b["run_id"] for c, _t, b in launches if c == 200]
            assert admitted, "the install must still carry work while it sheds the excess"
            # `<=`, and it is only meaningful because NOTHING admitted here can have
            # finished yet: every admitted run is parked on the barrier, which is not
            # released until below. So the bound is over CONCURRENT runs, not over a
            # sequence that happened to drain (LOW-7).
            with Session(engine) as s:
                live = [
                    r
                    for r in (s.get(WorkflowRun, rid) for rid in admitted)
                    if r is not None and r.status in (RunStatus.RUNNING, RunStatus.PENDING)
                ]
            assert len(live) == len(admitted), (
                "the admitted runs must ALL still be in flight for the bound below to be "
                "about concurrency — a run that drained would let the gate look tighter "
                "than it is",
                (len(live), len(admitted)),
            )
            assert len(admitted) <= config.MAX_CONCURRENT_RUNS, (
                "the gate must actually bound what it admits",
                (len(admitted), config.MAX_CONCURRENT_RUNS),
            )

            # 2. a refused launch wrote NOTHING (no orphan to find frozen later).
            with Session(engine) as s:
                total = len(s.exec(select(WorkflowRun.id)).all())
            assert total >= len(admitted)

            # 3. the probes survived the burst (on main /health peaked at 120.7s here).
            # SAMPLE THE PARKED STATE, don't just glance at it (LOW-7). Every admitted run
            # is held on the barrier right now and every excess launch has been refused —
            # this is the state the flood wedged in, so the liveness surface is probed
            # here until there is a real sample, not until the launches happen to return.
            _probe_deadline = time.time() + 20
            while len(probes) < _MIN_PROBES and time.time() < _probe_deadline:
                time.sleep(0.02)
            assert len(probes) >= _MIN_PROBES, (
                "too few probes to claim the liveness surface survived the burst (LOW-7)",
                len(probes),
            )
            slow_health = [p for p in probes if p[0] == "/health" and p[1] > _HEALTH_BUDGET_S]
            slow_ready = [p for p in probes if p[0] == "/ready" and p[1] > _READY_BUDGET_S]
            assert not slow_health, ("/health must not stall while launches queue", slow_health[:5])
            assert not slow_ready, ("/ready must answer inside the check budget", slow_ready[:5])

            _Barrier.release.set()
            outcomes = _wait_terminal(admitted, timeout=90)
            stop.set()
            prober.join(timeout=5)
    finally:
        stop.set()
        _Barrier.release.set()
        set_pieces("app.services.providers", "_provider_for_profile", real_for_profile)
        _wait_slots_idle(30)

    # 4. and every admitted run finished. NONE frozen, none killed by the pool.
    for rid, (status, notes, step) in outcomes.items():
        assert _db.POOL_EXHAUSTED_NOTE not in notes, (
            "an ADMITTED run must not die of pool exhaustion — the gate exists so that "
            "what is let in can be carried",
            (rid, notes),
        )
        assert status in (RunStatus.COMPLETED, RunStatus.PAUSED), (
            "no run may be left non-terminal at step 0 (twelve were, on the flood's install)",
            (rid, str(status), step, notes),
        )
    print(
        f"  [B2] {_OVERSUBSCRIBE} simultaneous launches: {len(admitted)} carried, "
        f"{len(refused)} refused 503/run_capacity, 0 frozen, {len(probes)} probes in budget"
    )


# --------------------------------------------------------------------------- #
# [C] /health vs. a SATURATED anyio threadpool — #99's actual mechanism
# --------------------------------------------------------------------------- #
def test_health_answers_while_every_worker_thread_is_pinned():
    """The DB-free liveness probe must not queue behind application work.

    A SYNC handler is dispatched to the shared anyio worker pool; once that pool is full,
    a sync /health cannot be scheduled at all, however trivial its body — which is why a
    handler returning a literal dict measured 16s, 28s and 145s during the flood. The
    only structural fix is for /health to never enter the threadpool: a pure `async def`.
    We shrink the limiter (from inside the loop, the only place it is reachable) and pin
    every token with handlers that block, then ask for liveness."""
    import anyio

    pinned = 4
    release = threading.Event()
    _entered = threading.Event()
    _entered_count = [0]
    _entered_lock = threading.Lock()

    @app.get("/__stress_probe_limiter")
    async def _limiter(n: int):
        lim = anyio.to_thread.current_default_thread_limiter()
        was = lim.total_tokens
        lim.total_tokens = n
        return {"was": was}

    @app.get("/__stress_probe_block")
    def _block():  # SYNC on purpose: this is what consumes a worker thread
        with _entered_lock:
            _entered_count[0] += 1
            _entered.set()
        release.wait(timeout=30)
        return {"ok": True}

    was = None
    try:
        with TestClient(app) as client:
            was = client.get("/__stress_probe_limiter", params={"n": pinned}).json()["was"]
            blockers = [
                threading.Thread(target=lambda: client.get("/__stress_probe_block"), daemon=True)
                for _ in range(pinned)
            ]
            for t in blockers:
                t.start()
            # Let every blocker actually reach its worker thread: the limiter is only
            # SATURATED once all `pinned` tokens are borrowed, and a request that is still
            # in flight on the loop has not taken one yet.
            deadline = time.time() + 10
            while time.time() < deadline and _entered.wait(0) is False:
                time.sleep(0.05)
            while time.time() < deadline and _entered_count[0] < pinned:
                time.sleep(0.05)
            assert _entered_count[0] >= pinned, (
                "the threadpool was never actually saturated — the test proves nothing",
                _entered_count[0],
            )

            t0 = time.time()
            r = client.get("/health")
            took = time.time() - t0
            assert r.status_code == 200 and r.json().get("status") == "ok", r.text
            assert took < _HEALTH_BUDGET_S, (
                "/health must answer with every worker thread pinned — a sync handler "
                "cannot, which is the whole of #99",
                took,
            )
            release.set()
            for t in blockers:
                t.join(timeout=10)
    finally:
        release.set()
        # RESTORE THE LIMITER IN THE FINALLY, not on the happy path: a failing assert
        # above would otherwise leave the shared threadpool shrunk to 4 tokens for every
        # LATER group in this process, turning one honest failure into a cascade of
        # unrelated slow ones. Its own client, because the one above is already closed.
        if was is not None:
            with TestClient(app) as _restore:
                _restore.get("/__stress_probe_limiter", params={"n": was})
        app.router.routes = [
            r
            for r in app.router.routes
            if getattr(r, "path", "") not in ("/__stress_probe_limiter", "/__stress_probe_block")
        ]
    print(f"  [C] /health answered in {took * 1000:.0f}ms with the threadpool saturated")


# --------------------------------------------------------------------------- #
# [D] /ready answers within its budget even with the pool fully checked out
# --------------------------------------------------------------------------- #
def test_ready_answers_honestly_when_the_pool_is_gone():
    """A SATURATED POOL IS NOT AN UNREADY MACHINE (gate A round 2, MED-1).

    Round 1 moved the platform health check onto `/ready` while `/ready` still ran its SELECT
    through the QueuePool, so the ONE state the doorman exists to survive — every
    connection checked out, `GET /` answered in 0.00s, the install draining — read as "not
    ready". At `min_machines_running = 1` three of those take the only machine out of the
    proxy's healthy set and the proxy then 503s EVERYTHING, including the graceful capacity
    page: the 2026-09-06 outage shape, reproduced by its own fix.

    So the two questions are separated. "Can this process reach the database" is answered
    over a POOL-LESS connection and is the verdict; "is the pool full" is a FIELD.

    FAILS BEHAVIOURALLY ON MAIN (gate A round 1, LOW-8): the pool is drained through the
    POOL's own counters, which exist on every version, so on the pre-fix tree /ready blocks
    for the full checkout timeout and busts _READY_BUDGET_S — it does not merely
    AttributeError on a new config name."""
    from app.web import health as health_mod

    held = []
    try:
        # Boot the app FIRST (its lifespan needs the database), then take every
        # connection: the state under test is a live server whose pool has just gone.
        with TestClient(app) as client:
            for _ in range(_pool_capacity()):
                try:
                    held.append(engine.raw_connection())
                except sa_exc.TimeoutError:
                    break
            assert _db.pool_has_no_capacity() or engine.pool.checkedout() >= _pool_capacity(), (
                "the probe proves nothing unless the pool really is empty",
                engine.pool.checkedout(),
            )
            t0 = time.time()
            r = client.get("/ready")
            took = time.time() - t0
            assert r.status_code == 200, (
                "a BUSY install is a READY install: 503 here takes the only machine out "
                "of the proxy's healthy set while the app is answering in milliseconds, "
                "which is the outage this train exists because of",
                (r.status_code, r.text[:200]),
            )
            body = r.json()
            assert body.get("status") == "ok", body
            assert body.get("pool") == "at_capacity", (
                "and saturation must still be VISIBLE — reported as a field so an operator "
                "or a dashboard can see it, never swallowed",
                body,
            )
            assert took < _READY_BUDGET_S, (
                "/ready must answer inside the health-check budget rather than blocking "
                "for the pool timeout — a late probe kills the machine just as dead",
                took,
            )
            # And liveness is unaffected: /health never asks the database anything.
            t0 = time.time()
            h = client.get("/health")
            assert h.status_code == 200 and (time.time() - t0) < _HEALTH_BUDGET_S

            # THE OTHER HALF: a database that cannot answer is still a 503. Loosening the
            # pool rule must not loosen the probe itself, or /ready stops meaning anything.
            real_ready = health_mod.db_ready
            health_mod.db_ready = lambda: (False, "OperationalError")
            try:
                # A probe already inside the executor is joined by the next caller, so let
                # the in-flight one settle before asserting on the patched verdict.
                for _ in range(60):
                    cf = health_mod._probe_cf
                    if cf is None or cf.done():
                        break
                    time.sleep(0.05)
                t0 = time.time()
                bad = client.get("/ready")
                bad_took = time.time() - t0
            finally:
                health_mod.db_ready = real_ready
            assert bad.status_code == 503 and bad.json().get("detail") == "OperationalError", (
                "an UNREACHABLE database must still fail the readiness probe",
                (bad.status_code, bad.text[:200]),
            )
            assert bad_took < _READY_BUDGET_S, bad_took
            for c in held:
                try:  # noqa: SIM105
                    c.close()
                except Exception:
                    pass
            held = []
    finally:
        for c in held:
            try:  # noqa: SIM105
                c.close()
            except Exception:
                pass
    print(
        f"  [D] /ready with 0 connections free: 200 pool=at_capacity in {took:.2f}s; "
        f"a dead DB still 503s in {bad_took:.2f}s; /health fine"
    )


# --------------------------------------------------------------------------- #
# [E] the designed 503 (with a machine-readable reason), never a 500
# --------------------------------------------------------------------------- #
def test_pool_exhaustion_and_capacity_render_as_honest_503s():
    from fastapi import FastAPI
    from fastapi.responses import JSONResponse

    from app.main import _pool_exhausted, _run_capacity_error

    tmp = FastAPI()
    tmp.add_exception_handler(sa_exc.TimeoutError, _pool_exhausted)
    tmp.add_exception_handler(orchestrator.RunCapacityError, _run_capacity_error)

    @tmp.get("/api/runs/{rid}")
    def _read(rid: int):
        raise sa_exc.TimeoutError(
            "QueuePool limit of size 5 overflow 10 reached, connection timed out"
        )

    @tmp.post("/api/workflows/{wid}/run")
    def _launch(wid: int):
        raise orchestrator.RunCapacityError(8, 8)

    @tmp.post("/api/workflows/{wid}/run-badlocale")
    def _bad(wid: int):
        return JSONResponse({"ok": False, "error": "locale 'zz' not on document"}, status_code=400)

    c = TestClient(tmp, raise_server_exceptions=False)

    r = c.get("/api/runs/5")
    assert r.status_code == 503, ("pool exhaustion is a CAPACITY answer, not a 500", r.status_code)
    body = r.json()
    assert body["ok"] is False and body["reason"] == "db_pool_exhausted", body
    assert body["retry_after"] == _db.POOL_EXHAUSTED_RETRY_AFTER_S, body
    assert r.headers.get("Retry-After") == str(_db.POOL_EXHAUSTED_RETRY_AFTER_S), dict(r.headers)

    r = c.post("/api/workflows/3/run")
    assert r.status_code == 503, r.status_code
    body = r.json()
    assert body["reason"] == "run_capacity" and body["retry_after"] > 0, body
    assert "capacity" in body["error"].lower() and "nothing was launched" in body["error"].lower()
    assert r.headers.get("Retry-After") == str(body["retry_after"])

    rb = c.post("/api/workflows/3/run-badlocale")
    assert rb.status_code == 400 and "reason" not in rb.json(), (
        "a VALIDATION refusal must stay distinguishable from a capacity refusal",
        rb.json(),
    )
    print("  [E] pool exhaustion + capacity render as named, retryable 503s (not 500s)")


# --------------------------------------------------------------------------- #
# [E2] THE WEB SURFACE under an exhausted pool — gate A round 1, HIGH-1
# --------------------------------------------------------------------------- #
_WEB_503_BUDGET_S = 2.0


def _count_pool_timeouts():
    """(install, restore, counter) around SQLAlchemy's own checkout WAIT.

    Counts what a request actually SPENDS: `QueuePool._do_get` is where a caller blocks,
    and the number the gate measured on the pre-fix tip was 23-24 of them in ONE request."""
    from sqlalchemy.pool import QueuePool

    real = QueuePool._do_get
    seen = {"n": 0}

    def counting(self):
        try:
            return real(self)
        except sa_exc.TimeoutError:
            seen["n"] += 1
            raise

    def install():
        QueuePool._do_get = counting

    def restore():
        QueuePool._do_get = real

    return install, restore, seen


def test_web_answers_a_capacity_503_without_touching_the_database():
    """MEASURED ON THE PRE-FIX TIP (b0721abf, this exact probe): `GET /` returned a
    BODYLESS 500 after 115.2s having performed 23 sequential pool-checkout timeouts;
    `GET /projects` and `POST /documents/{id}/run` returned bodyless 500s at 120.2s with
    24 each. The 503 handler existed and was correct — but its WEB half rendered
    `error.html`, which calls `app_meta()` twice plus every Jinja global, and every one of
    those is a database read. Under the one condition the handler exists for, the error
    page could not get a connection either, so the handler raised out and the client got
    an empty 500 after ~90-120s of a PINNED anyio worker thread. Pinned threads are the
    accumulator behind the OOM (GitHub #109).

    Every assertion below is one of those observations turned into a law."""
    did = _mk_doc("es-ES", 2)
    install, restore, seen = _count_pool_timeouts()
    held = []
    results = {}
    try:
        with TestClient(app, raise_server_exceptions=False) as client:
            for _ in range(_pool_capacity()):
                try:
                    held.append(engine.raw_connection())
                except sa_exc.TimeoutError:
                    break
            assert engine.pool.checkedout() >= _pool_capacity(), (
                "the probe proves nothing unless the pool really is empty",
                engine.pool.checkedout(),
            )
            install()
            for label, call in (
                ("GET /", lambda: client.get("/")),
                ("GET /projects", lambda: client.get("/projects")),
                (
                    "POST /documents/{id}/run",
                    lambda: client.post(f"/documents/{did}/run", data={"locale": "es-ES"}),
                ),
            ):
                seen["n"] = 0
                t0 = time.time()
                r = call()
                # r.headers.get, NOT dict(r.headers).get: httpx lower-cases the keys of a
                # plain dict conversion, so a case-sensitive lookup on the dict silently
                # reads None and the assertion below would pass vacuously.
                results[label] = (
                    r.status_code,
                    time.time() - t0,
                    seen["n"],
                    r.text,
                    r.headers.get("Retry-After"),
                )
            seen["n"] = 0
            t0 = time.time()
            h = client.get("/health")
            results["GET /health"] = (h.status_code, time.time() - t0, seen["n"], h.text, None)
            for c in held:
                try:  # noqa: SIM105
                    c.close()
                except Exception:
                    pass
            held = []
    finally:
        restore()
        for c in held:
            try:  # noqa: SIM105
                c.close()
            except Exception:
                pass
        _wait_slots_idle(20)

    for label, (code, took, waits, body, retry_after) in results.items():
        if label == "GET /health":
            assert code == 200 and took < _HEALTH_BUDGET_S, (label, code, took)
            continue
        assert code == 503, (
            "a web request under an exhausted pool is a CAPACITY answer, not a 500 — and "
            "never a 500 with an EMPTY body, which is what the error page produced when "
            "it could not read the database it needed to render itself",
            (label, code, took, body[:120]),
        )
        assert took < _WEB_503_BUDGET_S, (
            "and it must answer PROMPTLY: every second here is a pinned anyio worker "
            "thread, which is the mechanism behind #99's stalled probes and #109's OOM",
            (label, took),
        )
        assert waits == 0, (
            "a request must not WAIT on a pool that is already known to be empty — the "
            "pre-fix tip spent 23-24 sequential checkout timeouts inside one request",
            (label, waits),
        )
        assert retry_after == str(_db.POOL_EXHAUSTED_RETRY_AFTER_S), (
            "a retryable refusal must say when to retry",
            (label, retry_after),
        )
        assert body.strip(), ("a 503 with no body tells the operator nothing", label)
        assert "503" in body, (label, body[:200])
        assert "static/style.css" not in body and "app_meta" not in body, (
            "the capacity page must be the ZERO-READ one: the themed error.html pulls the "
            "stylesheet AND calls app_meta() (org name, auth state, spend cap) — database "
            "reads that cannot complete in the very failure being rendered",
            (label, body[:300]),
        )
    print(
        "  [E2] web under an exhausted pool: "
        + ", ".join(
            f"{k} {v[0]} in {v[1] * 1000:.0f}ms/{v[2]} waits" for k, v in sorted(results.items())
        )
    )


def test_one_request_spends_at_most_one_pool_timeout():
    """The other half of HIGH-1: a request's TOTAL pool waiting is bounded, not just each
    checkout's. The pre-fix tip bounded the checkout (5s instead of SQLAlchemy's 30s) and
    a single `GET /` still ran 23 of them.

    The pool timeout is lowered for this test so the assertions are about COUNTS and a
    generous wall-clock ceiling rather than about this desktop's scheduler (LOW-7)."""
    pool = engine.pool
    real_timeout = pool._timeout
    pool._timeout = 0.25
    install, restore, seen = _count_pool_timeouts()
    held = []
    try:
        for _ in range(_pool_capacity()):
            try:
                held.append(engine.raw_connection())
            except sa_exc.TimeoutError:
                break
        install()

        def _n_checkouts(n):
            refused = 0
            for _ in range(n):
                try:
                    engine.raw_connection().close()
                except sa_exc.TimeoutError:
                    refused += 1
            return refused

        # (1) INSIDE a request scope: every checkout still fails, but only the first WAITS.
        # Run it on its OWN thread, exactly as a request does: a thread starts with an
        # empty context, so the budget this opens dies with the thread instead of leaking
        # into the rest of this process (which is also why a daemon cannot poison itself).
        seen["n"] = 0
        scoped = {}

        def _one_request():
            _db.begin_pool_budget()
            t0 = time.time()
            scoped["refused"] = _n_checkouts(6)
            scoped["wall"], scoped["waits"] = time.time() - t0, seen["n"]

        rt = threading.Thread(target=_one_request, name="one-request", daemon=True)
        rt.start()
        rt.join(timeout=30)
        refused = scoped.get("refused")
        scoped_wall, scoped_waits = scoped.get("wall", 0.0), scoped.get("waits")
        assert refused == 6, ("an empty pool refuses every caller", refused)
        assert scoped_waits == 1, (
            "one request may spend ONE checkout timeout, however many queries it runs — "
            "18 of them at 5s each is 90s of a pinned worker thread per request",
            scoped_waits,
        )
        assert scoped_wall < real_timeout, (scoped_wall, real_timeout)

        # (2) OUTSIDE any request scope (a background run thread, a daemon): unchanged.
        # A long-lived worker must never be able to latch itself out of the database.
        seen["n"] = 0
        unscoped = {}

        def _bare_thread():
            unscoped["refused"] = _n_checkouts(3)
            unscoped["waits"] = seen["n"]

        t = threading.Thread(target=_bare_thread, name="no-budget", daemon=True)
        t.start()
        t.join(timeout=30)
        assert unscoped.get("refused") == 3, unscoped
        assert unscoped.get("waits") == 3, (
            "a thread with no request scope must keep the pre-existing behaviour — one "
            "honest wait per checkout — or a daemon could poison itself for its lifetime",
            unscoped,
        )

        # (3) A STALL BOUND, NOT A ONE-WAY LATCH (gate A round 2, MED-2). The first version
        # set `spent` and never cleared it, so one momentary saturation refused every later
        # checkout in that scope INSTANTLY — measured by the gate on a pool that was by then
        # completely free (60/60 available, "second checkout on a HEALTHY pool: TimeoutError
        # in 0.000s"). A page that survived the stall then died anyway.
        recovered = {}

        def _stall_then_recover():
            _db.begin_pool_budget()
            try:
                engine.raw_connection().close()
                recovered["first"] = "ok"
            except sa_exc.TimeoutError:
                recovered["first"] = "timeout"
            recovered["spent_after_stall"] = _db.pool_budget_spent()
            for c in list(held):
                try:  # noqa: SIM105
                    c.close()
                except Exception:
                    pass
                held.remove(c)
            try:
                conn = engine.raw_connection()
                conn.close()
                recovered["second"] = "ok"
            except sa_exc.TimeoutError:
                recovered["second"] = "timeout"
            recovered["spent_after_recovery"] = _db.pool_budget_spent()

        rt2 = threading.Thread(target=_stall_then_recover, name="stall-recover", daemon=True)
        rt2.start()
        rt2.join(timeout=30)
        assert recovered.get("first") == "timeout", recovered
        assert recovered.get("spent_after_stall") is True, recovered
        assert recovered.get("second") == "ok", (
            "the SAME scope must be able to check out again once the pool recovers — a "
            "per-request one-way latch kills a long request for the rest of its life over "
            "one transient blip",
            recovered,
        )
        assert recovered.get("spent_after_recovery") is False, recovered

        # (4) AND THE MINUTES-LONG SCOPES OPT OUT ENTIRELY. `observe_run` opens a Session
        # per 0.4s poll for up to 900s (~2250 checkouts in one request scope) and the
        # `wait=true` fan-out drain lives as long as every run it launched. A request-scoped
        # budget is the wrong bound for those: they get the per-CHECKOUT timeout instead.
        for _ in range(_pool_capacity()):
            try:
                held.append(engine.raw_connection())
            except sa_exc.TimeoutError:
                break
        seen["n"] = 0
        exempt = {}

        def _exempt_scope():
            _db.begin_pool_budget()
            _db.exempt_pool_budget()
            exempt["refused"] = _n_checkouts(3)
            exempt["waits"] = seen["n"]

        rt3 = threading.Thread(target=_exempt_scope, name="exempt", daemon=True)
        rt3.start()
        rt3.join(timeout=30)
        assert exempt.get("refused") == 3 and exempt.get("waits") == 3, (
            "an exempted scope must fall back to one honest wait PER CHECKOUT, not be "
            "latched out after the first — the SSE observer is the operator's only live "
            "progress view and the drain is a fan-out already in flight",
            exempt,
        )
    finally:
        restore()
        pool._timeout = real_timeout
        for c in held:
            try:  # noqa: SIM105
                c.close()
            except Exception:
                pass
    print(
        f"  [E2b] one request = 1 wait for 6 refusals ({scoped_wall * 1000:.0f}ms); the "
        f"same scope recovers when the pool does; an unscoped thread and an EXEMPT scope "
        f"still wait per checkout (3/3)"
    )


# --------------------------------------------------------------------------- #
# [E2c] the doorman's relief valve and its bounded logging — round 2, MED-7 + LOW-1
# --------------------------------------------------------------------------- #
def test_the_doorman_keeps_a_relief_valve_and_bounds_its_own_logging():
    """A DOORMAN THAT SHEDS EVERYTHING SHEDS THE CURE (gate A round 2, MED-7).

    Round 1's middleware exempted only `/health`, `/ready` and `/static`, so a wedged pool
    shed 100% of every other request — including run cancel, batch cancel and the settings
    page that holds the concurrency dials. There was no in-app move left that could REDUCE
    the load, and with the deploy check pointed at `/ready` (MED-1) the operator could not
    even reach the shed page to learn why. "The flood's worst outcome was the silence
    after it", one layer up.

    The valve is deliberately tiny: cancels and settings, nothing else. Every launch route
    stays shed, because admitting new work into a saturated pool is the whole failure."""
    import app.main as main_mod
    from app.web._routes import walk_routes

    # The set, exactly: what a wedged operator needs, and nothing that could BE the load.
    for relief in (
        "/runs/12/cancel",
        "/api/runs/12/cancel",
        "/settings",
    ):
        assert main_mod._is_relief(relief, "GET" if relief == "/settings" else "POST"), (
            "must survive a wedged pool",
            relief,
        )
    for shed in (
        "/",
        "/projects",
        "/documents/1/run",
        "/api/workflows/1/run",
        "/projects/1/keys/translate",
        "/advanced/missions/1/run",  # the paid add-on routes are not relief in the open core
        "/advanced/runs/12/cancel",
        "/settings-export",  # a SIBLING of /settings, not the settings surface
        "/cancellations",  # ends in neither /cancel nor the settings prefix
        # ROUND 3, HIGH-4: these were ALL relief under round 2's prefix match — 17 routes,
        # growing silently with every new one under /settings/ — including install-wide
        # feature flips and the retention purge. The valve is now an enumerated list.
        "/settings/features/advanced_workflows",
        "/settings/retention",
        "/settings/enterprise/enable",
        "/api/settings/features",
        "/queries/1/cancel-nonsense/cancel",  # any future path ending in /cancel
    ):
        assert not main_mod._is_relief(shed), (
            "the valve must stay small enough that it cannot become the load — and must "
            "not match a prefix SIBLING (the _is_back_office lesson)",
            shed,
        )

    # [E2d] THE ENUMERATION IS TIED TO THE REAL ROUTE TABLE. Round 2's set was asserted
    # against hand-written strings including `/batches/3/cancel`, a route this app does not
    # have — so the suite could not tell how WIDE the prefix really was. Every cancel route
    # the app actually exposes must be relief, and the relief list must name nothing else.
    # The valve relieves POST cancels of running WORK. The MFA sign-in "Start over" button
    # (`POST /auth/mfa/cancel`, a GET until #546 made it CSRF-checked) only clears half-way
    # sign-in session state: it is not a work cancel, and a sign-in cannot finish on a wedged
    # pool anyway. It is excluded by its exact path, and pinned as NOT relief below.
    _NOT_WORK_CANCELS = {"/auth/mfa/cancel"}
    _all_post_cancels = {
        getattr(r, "path", "")
        for r in walk_routes(app)
        if getattr(r, "path", "").endswith("/cancel")
        and "POST" in (getattr(r, "methods", None) or ())
    }
    assert _all_post_cancels >= _NOT_WORK_CANCELS, (
        "an excluded non-work cancel route is gone: drop it from _NOT_WORK_CANCELS",
        _NOT_WORK_CANCELS - _all_post_cancels,
    )
    for _np in _NOT_WORK_CANCELS:
        assert not main_mod._is_relief(_np, "POST"), (
            "a sign-in session cancel must not ride the relief valve",
            _np,
        )
    _cancel_routes = sorted(_all_post_cancels - _NOT_WORK_CANCELS)
    assert _cancel_routes, "the app must expose cancel routes at all"
    for _rp in _cancel_routes:
        assert main_mod._is_relief(_rp.replace("{run_id}", "7"), "POST"), (
            "every cancel route the app exposes must be reachable while the pool is wedged",
            _rp,
        )
    relief_routes = {
        (method, getattr(r, "path", ""))
        for r in walk_routes(app)
        for method in getattr(r, "methods", ())
        if main_mod._is_relief(re.sub(r"\{[^}]+\}", "7", getattr(r, "path", "") or ""), method)
    }
    assert relief_routes == {("GET", "/settings")} | {("POST", p) for p in _cancel_routes}, (
        relief_routes
    )
    assert not main_mod._is_relief("/settings", "POST")

    # And it really passes the door AND ANSWERS: a shed request spends ZERO checkout waits
    # (it never reaches the app); a relief request spends zero too, because it is served off
    # the pool-less engine, and it actually does the thing (round 3, HIGH-4).
    install, restore, seen = _count_pool_timeouts()
    held = []
    measured = {}
    # A live-looking run for the operator to cancel while the pool is gone. A plain row is
    # enough: `cancel_run` is the same seam whatever started it, and this arm is about
    # whether the REQUEST can reach it, not about killing a thread.
    _wid = _mk_wf("mock")
    _did = _mk_doc("de-DE", 1)
    with Session(engine) as s:
        _r = WorkflowRun(
            workflow_id=_wid,
            document_id=_did,
            target_locale="de-DE",
            status=RunStatus.RUNNING,
            current_step_index=0,
        )
        s.add(_r)
        s.commit()
        cancel_run_id = _r.id
        api_row = WorkflowRun(
            workflow_id=_wid, document_id=_did, target_locale="de-DE", status=RunStatus.RUNNING
        )
        s.add(api_row)
        s.commit()
        api_cancel_id = api_row.id
    api_headers = {"X-Loc-Token": _api_key()}
    try:
        with TestClient(app, raise_server_exceptions=False) as client:
            client.get("/settings")  # compile the template before timing pool relief
            for _ in range(_pool_capacity()):
                try:
                    held.append(engine.raw_connection())
                except sa_exc.TimeoutError:
                    break
            assert _db.pool_has_no_capacity(), engine.pool.checkedout()
            install()
            for label, path in (("shed", "/projects"), ("relief", "/settings")):
                seen["n"] = 0
                _t0 = time.time()
                r = client.get(path)
                measured[label] = (r.status_code, seen["n"])
                measured[label + "_wall"] = time.time() - _t0
            # AND THE OPERATOR'S ACTUAL MOVE: cancel a live run with the pool at its
            # ceiling. This is the whole point of the valve, and round 2 never asserted it.
            seen["n"] = 0
            _t0 = time.time()
            rc = client.post(f"/runs/{cancel_run_id}/cancel", follow_redirects=False)
            measured["cancel"] = (rc.status_code, seen["n"])
            measured["cancel_wall"] = time.time() - _t0
            seen["n"] = 0
            _t0 = time.time()
            api_response = client.post(f"/api/runs/{api_cancel_id}/cancel", headers=api_headers)
            measured["api_cancel"] = (api_response.status_code, seen["n"])
            measured["api_cancel_wall"] = time.time() - _t0
            for c in held:
                try:  # noqa: SIM105
                    c.close()
                except Exception:
                    pass
            held = []
    finally:
        restore()
        for c in held:
            try:  # noqa: SIM105
                c.close()
            except Exception:
                pass
    assert measured["shed"][1] == 0, (
        "a shed request must never reach the database at all",
        measured,
    )
    # ROUND 3, HIGH-4: THE ASSERTION THAT MATTERS IS NOT "IT PASSED THE DOOR".
    # Round 2 asserted `/settings` "spent its 1 budgeted wait" — a CHECKOUT-WAIT assertion,
    # which the shipped code satisfied while answering 503 after 5.02s of a pinned worker
    # thread. A relief request must ANSWER, and it must answer without waiting on the pool
    # at all, because the only state it is ever exercised in is the state where no pooled
    # connection exists. So: zero pool waits, a real status, and it is fast.
    assert measured["relief"][0] < 500, (
        "the relief path must ANSWER under a fully checked-out pool, not 503 — a valve "
        "that is exempt from the door and then dies on the pool is worse than shedding",
        measured,
    )
    assert measured["relief"][1] == 0, (
        "and it must not WAIT on the pool to do it: it runs on the pool-less engine",
        measured,
    )
    assert measured["relief_wall"] < 1.0, (
        "well under a second — 5s of a pinned anyio worker thread per retry IS the "
        "accumulator that ended in the OOM kill (#109)",
        measured,
    )
    assert measured["cancel"][0] in (200, 303), (
        "POST /runs/{id}/cancel must SUCCEED with every pooled connection checked out — "
        "measured 503 after 5.03s before this fix, with the run still RUNNING",
        measured,
    )
    assert measured["cancel"][1] == 0 and measured["cancel_wall"] < 1.0, measured
    assert measured["api_cancel"] == (200, 0) and measured["api_cancel_wall"] < 1.0, measured
    with Session(engine) as s:
        assert s.get(WorkflowRun, api_cancel_id).status == RunStatus.CANCELLED
        _after = s.get(WorkflowRun, cancel_run_id)
        assert _after.status == RunStatus.CANCELLED, (
            "and the RUN must actually be cancelled — 'the request passed the door' is not "
            "the property an operator needs during an incident",
            (_after.status, measured),
        )

    # LOW-1: one WARNING per shed request is one line per request for the whole duration
    # of a sustained wedge, and the doorman IS the burst its sibling handler already
    # refuses to be. First line immediate, then at most one a minute carrying the count.
    import logging

    records: list = []

    class _Cap(logging.Handler):
        def emit(self, rec):
            records.append(rec.getMessage())

    cap_handler = _Cap()
    logger = logging.getLogger("loc_tms")
    logger.addHandler(cap_handler)
    saved = (main_mod._shed_log_last, main_mod._shed_log_suppressed)
    try:
        main_mod._shed_log_last = 0.0
        main_mod._shed_log_suppressed = 0
        for _ in range(200):
            main_mod._log_shed("GET", "/projects")
        first_burst = len(records)
        assert first_burst == 1, (
            "200 shed requests must not be 200 log lines — a sustained wedge would drown "
            "every other signal in the log",
            first_burst,
        )
        # Age the window and shed once more: the next line carries what was suppressed.
        main_mod._shed_log_last -= main_mod._SHED_LOG_INTERVAL_S + 1
        main_mod._log_shed("GET", "/projects")
        assert len(records) == 2, records
        main_mod._shed_episode_over()
        main_mod._log_shed("GET", "/projects")
        assert len(records) == 3, "new shedding episode must log immediately"
        assert "199 more shed" in records[1], (
            "nothing may be HIDDEN: the count of what was suppressed is the signal",
            records[1],
        )
    finally:
        logger.removeHandler(cap_handler)
        main_mod._shed_log_last, main_mod._shed_log_suppressed = saved
    print(
        f"  [E2c] relief valve: /projects shed with {measured['shed'][1]} waits, /settings "
        f"passed the door with {measured['relief'][1]}; 200 sheds = 1 log line + a count"
    )


# --------------------------------------------------------------------------- #
# [F] the admission gate: refuse at the door, leave nothing behind
# --------------------------------------------------------------------------- #
def test_capacity_gate_refuses_before_it_writes_anything():
    assert _wait_slots_idle() == 0
    # CLEAR THE BARRIER FIRST. `_Hold.release` is a class-level Event, so a group that ran
    # earlier and set it would let `first` complete instantly — and a run that reaches
    # COMPLETED still holds its slot for the microseconds until its thread's `finally`,
    # which makes the re-attach assertion below race (observed while building this round).
    _Hold.release.clear()
    providers.register_provider("holdslow", _Hold())
    wid, did = _mk_wf("holdslow"), _mk_doc("es-ES", 2)
    other_wid, other_did = _mk_wf("holdslow"), _mk_doc("es-ES", 2)
    token = {"X-Loc-Token": _api_key()}
    old_cap = config.MAX_CONCURRENT_RUNS
    config.MAX_CONCURRENT_RUNS = 1
    try:
        with TestClient(app) as client:
            first = client.post(
                f"/api/workflows/{wid}/run",
                json={"doc_id": did, "locale": "es-ES"},
                headers=token,
            )
            assert first.status_code == 200, first.text
            deadline = time.time() + 10
            while time.time() + 0 < deadline and orchestrator.RUN_SLOTS.in_flight < 1:
                time.sleep(0.02)

            before = _run_count()
            second = client.post(
                f"/api/workflows/{other_wid}/run",
                json={"doc_id": other_did, "locale": "es-ES"},
                headers=token,
            )
            assert second.status_code == 503, ("at capacity = an honest 503", second.text)
            assert second.json()["reason"] == "run_capacity", second.json()
            assert _run_count() == before, (
                "a REFUSED launch must write nothing — the flood's damage was runs that "
                "were admitted, crashed, and stayed RUNNING at step 0 forever",
                (before, _run_count()),
            )
            # A re-attach (double-submit) must not consume a second slot.
            again = client.post(
                f"/api/workflows/{wid}/run",
                json={"doc_id": did, "locale": "es-ES"},
                headers=token,
            )
            assert again.status_code == 200 and again.json()["reattached"] is True, again.text
            assert orchestrator.RUN_SLOTS.in_flight == 1, (
                "re-attaching to a counted run must not count it twice",
                orchestrator.RUN_SLOTS.in_flight,
            )
            _Hold.release.set()
            assert _wait_slots_idle(20) == 0, "the slot must come back when the run ends"
            third = client.post(
                f"/api/workflows/{other_wid}/run",
                json={"doc_id": other_did, "locale": "es-ES"},
                headers=token,
            )
            assert third.status_code == 200, ("capacity recovers", third.text)
            _wait_terminal([third.json()["run_id"]], timeout=30)
    finally:
        config.MAX_CONCURRENT_RUNS = old_cap
        _Hold.release.set()
        _wait_slots_idle(20)
    print("  [F] at capacity: 503 with no row written; the slot returns; re-attach is free")


class _Hold(Provider):
    name = "holdslow"
    release = threading.Event()

    def complete(self, prompt, system=None, task=None, payload=None, **kw):
        _Hold.release.wait(timeout=25)
        return "held " + ((payload or {}).get("text", "") or "x")


def _run_count() -> int:
    with Session(engine) as s:
        return len(s.exec(select(WorkflowRun.id)).all())


# --------------------------------------------------------------------------- #
# [G] the honest reason, written even when the pool has nothing left
# --------------------------------------------------------------------------- #
def test_pool_crash_records_its_reason_with_no_connection_to_spare(monkeypatch=None):
    """FAILS BEHAVIOURALLY ON MAIN (gate A round 1, LOW-8): the verdict is read through a
    pool-less engine this test builds ITSELF (`_poolless_read`), not through
    `db.emergency_session`, so on the pre-fix tree the assertion that fails is "the run is
    FAILED and says why" — the actual regression — rather than an AttributeError on a name
    the fix introduced.

    The flood's real damage was the SILENCE. A run that died on a pool timeout ran a
    crash handler that opened ANOTHER pooled session to record the failure — which timed
    out too — so twelve runs stayed RUNNING at step 0 with `error: null` forever.

    Here the execution dies on a pool timeout AND the pool is genuinely empty while the
    handler runs, so the pool-less bookkeeping engine is the only way the reason lands."""
    assert _wait_slots_idle() == 0
    wid, did = _mk_wf("mock"), _mk_doc("es-ES", 2)
    real_stream = orchestrator.run_workflow_stream
    held = []

    def boom(*a, **k):
        # Take every connection FIRST, so the crash handler that follows genuinely has
        # none — the exact state the flood's handler failed in.
        for _ in range(_pool_capacity()):
            try:
                held.append(engine.raw_connection())
            except sa_exc.TimeoutError:
                break
        raise sa_exc.TimeoutError(
            "QueuePool limit of size 5 overflow 10 reached, connection timed out, timeout 5.00"
        )

    monkeypatch_pieces(monkeypatch, "app.services.orchestrator", "run_workflow_stream", boom)
    try:
        with Session(engine) as s:
            run_id = orchestrator.start_run(s, wid, did, "es-ES")
        deadline = time.time() + 20
        status = notes = None
        while time.time() < deadline:
            row = _poolless_read(run_id)
            if row is not None and row[0] == RunStatus.FAILED:
                status, notes = row
                break
            time.sleep(0.1)
    finally:
        monkeypatch_pieces(
            monkeypatch, "app.services.orchestrator", "run_workflow_stream", real_stream
        )
        for c in held:
            try:  # noqa: SIM105
                c.close()
            except Exception:
                pass
        _wait_slots_idle(20)

    assert status == RunStatus.FAILED, (
        "a run that dies on pool exhaustion must be FAILED, not left RUNNING forever",
        status,
    )
    assert _db.POOL_EXHAUSTED_NOTE in notes, (
        "and it must say WHY — 'error: null' is what made twelve frozen runs unexplainable",
        notes,
    )
    assert "pool" in notes.lower() and "retry" in notes.lower(), notes
    print("  [G] a pool-exhaustion crash lands a FAILED run with the honest reason")


# --------------------------------------------------------------------------- #
# [H] the per-segment amplifier is gone
# --------------------------------------------------------------------------- #
def test_engine_profile_is_read_once_not_once_per_segment():
    """FAILS BEHAVIOURALLY ON MAIN (gate A round 1, LOW-8): the cache-invalidation call is
    tolerated as absent, so what fails on the pre-fix tree is `reads == 1` against the 50
    reads the amplifier actually issued."""
    pid = _engine_profile()
    _invalidate_memo()
    reads = {"n": 0}
    real_get = Session.get

    def counting(self, entity, ident, *a, **k):
        if entity is EngineProfile:
            reads["n"] += 1
        return real_get(self, entity, ident, *a, **k)

    Session.get = counting
    try:
        for _ in range(50):
            assert providers.provider_is_live(f"engine:{pid}") is True
        assert reads["n"] == 1, (
            "50 provider resolutions must issue ONE profile read — this is #100's "
            "amplifier, which turned a 654-segment document into 1300+ pool checkouts",
            reads["n"],
        )
        # ...and a write is visible immediately, not after a TTL.
        engines_svc.update_profile(pid, enabled=False)
        assert providers.provider_is_live(f"engine:{pid}") is False, (
            "a disabled engine must go dead on the very next read — a memo that serves a "
            "stale credential or a stale enabled flag is worse than the amplifier"
        )
    finally:
        Session.get = real_get
        engines_svc.update_profile(pid, enabled=True)
        _invalidate_memo()
    print("  [H] one profile read per resolution burst; writes invalidate immediately")


# --------------------------------------------------------------------------- #
# [I] a reservation is owned ONCE — gate A round 1, HIGH-2
# --------------------------------------------------------------------------- #
class _ThreadStartFails:
    """`threading` as the orchestrator sees it, with `Thread.start()` raising exactly what
    a machine out of threads raises. Everything else proxies to the real module."""

    class Thread:
        def __init__(self, *a, **k):
            pass

        def start(self):
            raise RuntimeError("can't start new thread")

    def __getattr__(self, name):
        return getattr(threading, name)


def test_a_failed_thread_start_never_steals_a_live_runs_slot():
    """MEASURED ON THE PRE-FIX TIP (b0721abf): with one run counted in flight, a launch
    whose `Thread.start()` raised took `in_flight` 1 -> 0. Two releases fired for ONE
    reservation — `_run_in_background`'s `except BaseException` and `start_run`'s
    `finally` — and `_RunSlots.release`'s `max(0, n-1)` clamp hid it by stealing a
    DIFFERENT, live run's slot instead of going negative.

    The trigger is `RuntimeError: can't start new thread`, i.e. exactly the thread/memory
    exhaustion the 2026-09-06 incident ended in. So the admission gate lost its count
    precisely when it was the only thing standing between the install and the wedge, and
    cumulatively: sustained thread exhaustion pins `in_flight` near 0 and defeats it."""
    assert _wait_slots_idle() == 0, "start from an idle install"
    wid, did = _mk_wf("mock"), _mk_doc("es-ES", 2)

    # A slot held on behalf of a run that IS executing (no thread needed: the count is
    # what is under test, and a real run would race the shim below).
    live = orchestrator.RUN_SLOTS.take()[0]
    real_threading = orchestrator.threading
    raised = None
    before = orchestrator.RUN_SLOTS.in_flight
    try:
        set_pieces("app.services.orchestrator", "threading", _ThreadStartFails())
        try:
            with Session(engine) as s:
                orchestrator.start_run(s, wid, did, "es-ES")
        except RuntimeError as exc:
            raised = exc
    finally:
        set_pieces("app.services.orchestrator", "threading", real_threading)

    assert raised is not None, "the probe requires the thread start to actually fail"
    after = orchestrator.RUN_SLOTS.in_flight
    assert before == 1, before
    assert after == 1, (
        "a launch that could not start its thread must give back ITS OWN slot exactly "
        "once — never a second time, which silently steals a live run's slot and "
        "disables the gate under the very exhaustion it exists for",
        (before, after),
    )

    # And the property that makes that impossible rather than merely untrue today.
    tok = orchestrator.RUN_SLOTS.take()[0]
    n = orchestrator.RUN_SLOTS.in_flight
    assert tok.release() is True and orchestrator.RUN_SLOTS.in_flight == n - 1
    assert tok.release() is False and orchestrator.RUN_SLOTS.in_flight == n - 1, (
        "release must be IDEMPOTENT: a second release on one reservation is a no-op, not "
        "another decrement"
    )
    live.release()
    assert _wait_slots_idle(20) == 0
    print("  [I] a failed thread start returns exactly one slot; release is idempotent")


# --------------------------------------------------------------------------- #
# [J] a fan-out is a bounded QUEUE, never unsatisfiable, and never leaks a slot
#     gate A round 1, MED-1; round 2, HIGH-1 + HIGH-2 + MED-5
# --------------------------------------------------------------------------- #
def _key_project(locales: list[str], n_keys: int = 1) -> int:
    """A key-based project with `locales` targets and `n_keys` keys — the shape whose
    "translate all" is (target locales x namespace documents) runs."""
    from app.services import content as content_svc
    from app.services import keys as keys_svc

    with Session(engine) as s:
        cust = content_svc.customer_create(s, _uniq("StressCust"))
        proj = content_svc.project_create(
            s,
            _uniq("StressKeys"),
            source_locale="en-US",
            target_locales=", ".join(locales),
            customer_id=str(cust.id),
            project_type=ProjectType.KEY_BASED,
        )
        pid = proj.id
    with Session(engine) as s:
        project = s.get(Project, pid)
        for i in range(n_keys):
            keys_svc.create_key(
                s, project, f"stress.capacity.{i}", base_value=f"Hello {i}", actor="fixture"
            )
    return pid


def _boom_on_second_launch(real_start, calls, exc_type):
    """A `start_run` stand-in that fails the SECOND launch of a fan-out — the mid-loop
    failure that leaked a pre-paid slot. A factory so the closure binds THIS iteration's
    values (the seam loop below runs it twice)."""

    def _boom(*a, **k):
        calls["n"] += 1
        if calls["n"] == 2:
            raise exc_type("simulated writer-lock loss in commit_launch")
        return real_start(*a, **k)

    return _boom


def test_a_fan_out_bigger_than_the_cap_runs_queued_and_never_leaks_a_slot():
    """A large fan-out creates all rows and drains without holding request slots."""
    from app.services import keys as keys_svc

    old_cap = config.MAX_CONCURRENT_RUNS
    held_tokens = []
    try:
        config.MAX_CONCURRENT_RUNS = 2
        pid = _key_project(["de-DE", "fr-FR", "ru-RU"])
        wid = _mk_wf("mock")
        with Session(engine) as s:
            out = keys_svc.launch_translation(s, s.get(Project, pid), wid, "all", actor="fixture")
        ids = out["launched"] + [r["run_id"] for r in out["queued"]]
        assert len(ids) == 3 and out["queue_group"], out
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            with Session(engine) as s:
                statuses = [s.get(WorkflowRun, rid).status for rid in ids]
            if all(status == RunStatus.COMPLETED for status in statuses):
                break
            time.sleep(0.05)
        assert statuses == [RunStatus.COMPLETED] * 3, statuses
        assert _wait_slots_idle(20) == 0
        message = str(orchestrator.RunCapacityError(8, 8, needed=72))
        assert "queues" in message and "free slots" not in message
        assert "LOC_TMS_MAX_CONCURRENT_RUNS" not in message
    finally:
        config.MAX_CONCURRENT_RUNS = old_cap
        for tok in held_tokens:
            tok.release()
        _wait_slots_idle(20)
    print("  [J] durable fan-out drained all rows; capacity returned")


def _batch_count() -> int:
    from app.models import WorkflowBatch

    with Session(engine) as s:
        return len(s.exec(select(WorkflowBatch.id)).all())


# --------------------------------------------------------------------------- #
# [K] the MCP relay of a capacity 503 — gate A round 1, MED-4
# --------------------------------------------------------------------------- #
def test_mcp_relays_the_capacity_503_as_wait_and_retry():
    """MCP is a claimed surface for this refusal and had no test. An agent that reads a
    bare HTTP 503 as "the install is dead" abandons the work; one that retries immediately
    keeps the install saturated. Both reasons must relay as WAIT, and the reason-less
    fallback must not pretend to know which one it was."""
    import importlib.util

    path = os.path.join(_ROOT, "integrations", "mcp-server", "server.py")
    spec = importlib.util.spec_from_file_location("loc_mcp_server_stress", path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["loc_mcp_server_stress"] = mod
    spec.loader.exec_module(mod)

    import httpx as _httpx

    def _resp(payload, text_body=None):
        return (
            _httpx.Response(
                503,
                json=payload if payload is not None else None,
                request=_httpx.Request("POST", "http://x/api/workflows/1/run"),
            )
            if payload is not None
            else _httpx.Response(
                503, text=text_body or "", request=_httpx.Request("GET", "http://x/api/runs/1")
            )
        )

    cap = mod._explain_status(
        _resp(
            {
                "ok": False,
                # The structured refusal owns its message; MCP relays it verbatim.
                "error": str(orchestrator.RunCapacityError(8, 8)),
                "reason": "run_capacity",
                "retry_after": 15,
            }
        )
    )
    assert cap == str(orchestrator.RunCapacityError(8, 8)), cap
    assert "nothing was launched" in cap.lower(), cap
    assert "nothing was spent" in cap.lower(), cap
    assert "15" in cap and "again" in cap.lower(), ("the wait hint must carry the number", cap)

    pool = mod._explain_status(
        _resp(
            {
                "ok": False,
                "error": _db.POOL_EXHAUSTED_NOTE,
                "reason": "db_pool_exhausted",
                "retry_after": 10,
            }
        )
    )
    assert "half-written" in pool.lower(), pool
    assert "10" in pool, pool
    assert "nothing was launched" not in pool.lower(), (
        "a POOL refusal must not claim a launch was refused — the two reasons exist so a "
        "client can tell them apart",
        pool,
    )

    bare = mod._explain_status(_resp(None, "upstream unavailable"))
    assert "503" in bare and "retry" in bare.lower(), bare
    assert "nothing was launched" not in bare.lower(), (
        "with no reason the relay must not invent one",
        bare,
    )
    print("  [K] MCP relays run_capacity / db_pool_exhausted / no-reason as distinct waits")


# --------------------------------------------------------------------------- #
def test_round3_durable_batch_admission():
    from app.models import AuditLog
    from app.services import batch_runs

    old = orchestrator.ADMISSION
    scheduler = orchestrator._AdmissionScheduler()
    scheduler.ensure_started = lambda: False
    set_pieces("app.services.orchestrator", "ADMISSION", scheduler)
    old_cap = config.MAX_CONCURRENT_RUNS
    config.MAX_CONCURRENT_RUNS = 1
    token = orchestrator.RUN_SLOTS.take()[0]
    try:
        wid, did = _mk_wf("mock"), _mk_doc("es-ES", 1)
        with Session(engine) as s:
            t0 = time.monotonic()
            batch = batch_runs.start_batch(
                s, workflow_id=wid, doc_id=did, locales=["es-ES"], synchronous=False
            )
            assert time.monotonic() - t0 < 1.0, "batch request must not wait for capacity"
            rows = batch_runs.batch_members(s, batch)
            assert len(rows) == 1 and rows[0].status == RunStatus.QUEUED, rows
            assert rows[0].queue_group, "batch members must use the shared admission queue"
            rid, bid = rows[0].id, batch.id
            audit = s.exec(
                select(AuditLog)
                .where(AuditLog.action == "workflow.batch")
                .order_by(AuditLog.id.desc())
            ).first()
            assert audit is not None and "queued=1" in audit.detail and str(rid) in audit.detail
            view = batch_runs.batch_as_dict(s, batch, detail=True)
            assert batch_runs.build_matrix(view)["rows"][0]["status"] == "queued", view
        assert not any(t.name.startswith("loc-batch-dispatch") for t in threading.enumerate())
        assert orchestrator.RUN_SLOTS.in_flight == 1
        with Session(engine) as s:
            batch = s.get(batch_runs.WorkflowBatch, bid)
            batch.dispatcher_owner = {"host": "previous-boot", "pid": 1}
            s.add(batch)
            s.commit()
        # B's safe default is a no-op; explicitly exercise A's boot adoption contract.
        batch_runs.recover_interrupted_batches(at_boot=True)
        with Session(engine) as s:
            assert s.get(WorkflowRun, rid).status == RunStatus.QUEUED
            batch = s.get(batch_runs.WorkflowBatch, bid)
            assert batch.finished_at is None
            assert batch.dispatcher_owner == batch_runs._dispatcher_owner(), (
                "boot must own surviving queued work before admission resumes"
            )
        token.release()
        scheduler.drain_once(slot_wait=0)
        assert _wait_slots_idle(20) == 0
        with Session(engine) as s:
            assert s.get(WorkflowRun, rid).status == RunStatus.COMPLETED
        scheduler.drain_once(slot_wait=0)
        with Session(engine) as s:
            assert s.get(batch_runs.WorkflowBatch, bid).finished_at is not None
    finally:
        token.release()
        config.MAX_CONCURRENT_RUNS = old_cap
        set_pieces("app.services.orchestrator", "ADMISSION", old)


def test_round3_queue_rows_precede_execution():

    from app.services import keys as keys_svc

    pid = _key_project(["de-DE", "fr-FR", "ru-RU"])
    wid = _mk_wf("mock")
    observed = []
    old = orchestrator.ADMISSION
    scheduler = orchestrator._AdmissionScheduler()
    scheduler.ensure_started = lambda: False
    set_pieces("app.services.orchestrator", "ADMISSION", scheduler)

    def capture(*args, **kwargs):
        with Session(engine) as s:
            observed.append(
                len(s.exec(select(WorkflowRun).where(WorkflowRun.workflow_id == wid)).all())
            )
        raise RuntimeError("seeded admission failure")

    try:
        with patch_pieces("app.services.orchestrator", "_run_in_background", capture):
            with Session(engine) as s:
                out = keys_svc.launch_translation(
                    s, s.get(Project, pid), wid, "all", actor="fixture"
                )
            for _ in range(3):
                scheduler.drain_once(slot_wait=0)
        assert observed and all(n == 3 for n in observed), (
            "all rows precede any execution",
            observed,
        )
        ids = out["launched"] + [r["run_id"] for r in out["queued"]]
        assert len(ids) == 3, out
        with Session(engine) as s:
            for rid in ids:
                row = s.get(WorkflowRun, rid)
                assert row.status == RunStatus.FAILED and "seeded admission failure" in row.notes
                assert orchestrator.run_reason(row)["code"] == "queue_launch_failed"
    finally:
        set_pieces("app.services.orchestrator", "ADMISSION", old)


def test_round3_readiness_prose():
    from pathlib import Path

    prose = Path("docs/architecture/deployment.md").read_text(encoding="utf-8")
    assert "Readiness shares the connection pool" not in prose
    assert "reported NOT READY" not in prose
    assert "200" in prose and "at_capacity" in prose and "pool-less" in prose


def test_round3_scheduler_fairness():
    from unittest.mock import patch

    slots = orchestrator._RunSlots()
    scheduler = orchestrator._AdmissionScheduler()
    held = slots.take()[0]
    parked = threading.Event()
    yielded = threading.Event()
    stop = threading.Event()
    original_wait = slots._lock.wait
    original_drain = scheduler.drain_once

    def condition_wait(timeout=None):
        if slots._yield_until > time.monotonic():
            yielded.set()
        else:
            parked.set()
        return original_wait(timeout)

    def drain():
        if stop.is_set():
            raise SystemExit()
        return original_drain(slot_wait=2)

    wid, did = _mk_wf("mock"), _mk_doc("es-ES", 1)
    with Session(engine) as session:
        rows = [
            WorkflowRun(
                workflow_id=wid,
                document_id=did,
                target_locale=f"fair-{n}",
                status=RunStatus.QUEUED,
                queue_group="fairness-r4",
            )
            for n in range(3)
        ]
        for row in rows:
            session.add(row)
        session.commit()
        ids = [r.id for r in rows]
    with (
        patch.object(config, "MAX_CONCURRENT_RUNS", 1),
        patch_pieces("app.services.orchestrator", "RUN_SLOTS", slots),
        patch_pieces("app.services.orchestrator", "ADMISSION", scheduler),
        patch.object(slots._lock, "wait", condition_wait),
        patch.object(scheduler, "drain_once", drain),
    ):
        try:
            scheduler.ensure_started()
            scheduler.notify()
            assert parked.wait(3), "the real admission daemon must be parked behind cap 1"
            assert slots.acquire()[0] is None, "interactive refusal must trigger the yield"
            held.release()
            assert yielded.wait(2), "the parked daemon must enter the real timed yield"
            interactive, _ = slots.acquire()
            assert interactive is not None, "interactive launch must win during the cap-1 yield"
            interactive.release()
        finally:
            held.release()
            stop.set()
            with Session(engine) as session:
                for rid in ids:
                    row = session.get(WorkflowRun, rid)
                    if row.status == RunStatus.QUEUED:
                        orchestrator.cancel_run(session, rid)
            scheduler.notify()
            scheduler._thread.join(30)
            assert not scheduler.alive()


def test_round3_queue_surfaces_and_cancel():
    from unittest.mock import patch

    from app.models import AuditLog

    settings.set_many({"keys_enabled": "1"})
    pid = _key_project(["de-DE", "fr-FR", "ru-RU"])
    wid = _mk_wf("mock")
    key = _api_key("api docs keys")
    headers = {"X-Loc-Token": key}
    scheduler = orchestrator._AdmissionScheduler()
    scheduler.ensure_started = lambda: False
    with (
        patch_pieces("app.services.orchestrator", "ADMISSION", scheduler),
        patch.object(scheduler, "drain_once", return_value=0),
        TestClient(app) as client,
    ):
        response = client.post(
            f"/api/projects/{pid}/keys/translate",
            headers=headers,
            json={"workflow_id": wid, "locale": "all"},
        )
        assert response.status_code == 200, response.text
        body = response.json()
        assert len(body["queued"]) == 3 and body["launched"] == [], body
        ids = [r["run_id"] for r in body["queued"]]
        plain_doc = _mk_doc("es-ES", 1)
        with Session(engine) as s:
            plain = WorkflowRun(
                workflow_id=wid,
                document_id=plain_doc,
                target_locale="es-ES",
                status=RunStatus.QUEUED,
            )
            s.add(plain)
            s.commit()
            plain_id = plain.id
        attached = client.post(
            f"/api/workflows/{wid}/run",
            headers=headers,
            json={"doc_id": plain_doc, "locale": "es-ES"},
        )
        assert attached.status_code == 200 and attached.json()["status"] == "queued", attached.text
        assert attached.json()["reattached"] and attached.json()["run_id"] == plain_id
        with Session(engine) as s:
            orchestrator.cancel_run(s, plain_id)
        with Session(engine) as s:
            audit = s.exec(
                select(AuditLog)
                .where(AuditLog.action == "keys.translate")
                .order_by(AuditLog.id.desc())
            ).first()
            assert audit.ip == "testclient", audit.ip
            assert "queued=3" in audit.detail and all(str(rid) in audit.detail for rid in ids)
        for rid in ids:
            view = client.get(f"/api/runs/{rid}", headers=headers)
            assert view.status_code == 200 and view.json()["status"] == "queued", view.text
            page = client.get(f"/runs/{rid}")
            assert page.status_code == 200 and "queued" in page.text and "Cancel run" in page.text
        import httpx

        from app.cli import _remote
        from tests._cli_runner import run_cli

        for args in (
            ["workflow", "show-run", str(ids[0])],
            ["job", "list", "--runs-only", "--workflow", str(wid)],
        ):
            rc, out, err = run_cli(["--json", *args])
            assert rc == 0 and '"queued"' in out, (rc, out, err)
            _remote._set_transport(
                httpx.MockTransport(
                    lambda request: httpx.Response(
                        200, json=client.get(request.url.raw_path.decode(), headers=headers).json()
                    )
                )
            )
            try:
                rc, out, err = run_cli(
                    ["--remote", "http://127.0.0.1", "--token", key, "--json", *args]
                )
                assert rc == 0 and '"queued"' in out, (rc, out, err)
            finally:
                _remote._set_transport(None)
        import asyncio
        import importlib.util

        spec = importlib.util.spec_from_file_location(
            "loc_mcp_queue", os.path.join(_ROOT, "integrations", "mcp-server", "server.py")
        )
        mod = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = mod
        spec.loader.exec_module(mod)
        mod.TOKEN = key
        transport = httpx.MockTransport(
            lambda request: httpx.Response(
                200, json=client.get(request.url.raw_path.decode(), headers=headers).json()
            )
        )
        with patch.object(mod, "_client", lambda: httpx.AsyncClient(transport=transport)):
            status = asyncio.run(mod.work_status(run_id=ids[0]))
            assert '"queued"' in status, status
        assert orchestrator.adopt_queued_runs() >= 3
        rid = ids[0]
        response = client.post(f"/api/runs/{rid}/cancel", headers=headers)
        assert response.status_code == 200, response.text
        with Session(engine) as s:
            assert s.get(WorkflowRun, rid).status == RunStatus.CANCELLED
        for rid in ids[1:]:
            client.post(f"/api/runs/{rid}/cancel", headers=headers)


def test_round3_observer_threadpool_retry():
    import asyncio

    from starlette.concurrency import iterate_in_threadpool

    wid, did = _mk_wf("mock"), _mk_doc("es-ES", 1)
    with Session(engine) as s:
        row = WorkflowRun(
            workflow_id=wid, document_id=did, target_locale="es-ES", status=RunStatus.RUNNING
        )
        s.add(row)
        s.commit()
        rid = row.id
    real = orchestrator._observer_session
    seen = []

    def transient(bind):
        seen.append(_db._pool_budget.get())
        if len(seen) == 2:
            raise sa_exc.TimeoutError("seeded transient checkout timeout")
        if len(seen) == 3:
            with Session(engine) as s:
                row = s.get(WorkflowRun, rid)
                row.status = RunStatus.COMPLETED
                s.add(row)
                s.commit()
        return real(bind)

    async def consume():
        _db.begin_pool_budget()
        events = []
        async for _, event in iterate_in_threadpool(
            orchestrator.observe_run(rid, poll_interval=0.01, max_seconds=2)
        ):
            events.append(event)
        return events

    with patch_pieces("app.services.orchestrator", "_observer_session", transient):
        events = asyncio.run(consume())
    assert len(seen) >= 3 and all(scope is None for scope in seen), seen
    assert events[-1]["status"] == "completed", events


def test_round3_scheduler_lifecycle_and_groups():
    from unittest.mock import patch

    scheduler = orchestrator._AdmissionScheduler()
    calls = []

    class Thread:
        def __init__(self, **kwargs):
            self.started = False

        def start(self):
            calls.append(1)
            self.started = True

        def is_alive(self):
            return self.started

    with patch.object(orchestrator.threading, "Thread", Thread):
        for _ in range(25):
            scheduler.ensure_started()
    assert len(calls) == 1, "one admission daemon per process"
    with (
        patch_pieces("app.services.orchestrator", "ADMISSION", scheduler),
        patch.object(scheduler, "ensure_started") as start,
    ):
        orchestrator.adopt_queued_runs()
        start.assert_called_once()
    scheduler = orchestrator._AdmissionScheduler()
    with patch_pieces("app.services.orchestrator", "threading", _ThreadStartFails()):
        assert scheduler.ensure_started() is False

    wid, did = _mk_wf("mock"), _mk_doc("es-ES", 1)
    with Session(engine) as s:
        rows = [
            WorkflowRun(
                workflow_id=wid,
                document_id=did,
                target_locale="es-ES",
                queue_group="wide-group",
                status=RunStatus.QUEUED,
            )
            for _ in range(orchestrator.QUEUE_SCAN_LIMIT + 1)
        ]
        rows.append(
            WorkflowRun(
                workflow_id=wid,
                document_id=did,
                target_locale="es-ES",
                queue_group="later-group",
                status=RunStatus.QUEUED,
            )
        )
        s.add_all(rows)
        s.commit()
        scheduler._last_group = "wide-group"
        try:
            assert scheduler._next_queued()[1] == "later-group", (
                "scan window starves later fan-outs"
            )
            scheduler._last_group = "later-group"
            assert scheduler._next_queued()[1] == "wide-group", "must rotate after last group"
        finally:
            for row in rows:
                s.delete(row)
            s.commit()


def test_round3_wait_has_one_total_deadline():
    from types import SimpleNamespace
    from unittest.mock import patch

    from app.services import keys as keys_svc

    scheduler = orchestrator._AdmissionScheduler()
    scheduler.ensure_started = lambda: False
    pid = _key_project(["de-DE", "fr-FR", "ru-RU"])
    wid = _mk_wf("mock")
    elapsed = [0.0]

    def tick(_seconds):
        elapsed[0] += 0.01

    clock = SimpleNamespace(monotonic=lambda: elapsed[0], sleep=tick)
    with (
        patch_pieces("app.services.orchestrator", "ADMISSION", scheduler),
        patch.object(scheduler, "drain_once", return_value=0),
        patch_pieces("app.services.orchestrator", "QUEUE_DRAIN_WAIT_S", 0.02),
        patch_pieces("app.services.keys", "time", clock),
        Session(engine) as s,
    ):
        result = keys_svc.launch_translation(
            s, s.get(Project, pid), wid, "all", actor="fixture", wait=True
        )
        assert elapsed[0] <= 0.04, "wait deadline must not multiply by queued members"
        assert len(result["queued"]) == len(result["drain_incomplete"]) == 3, result
        for row in result["queued"]:
            orchestrator.cancel_run(s, row["run_id"])


def test_round3_comment_contract():
    from pathlib import Path

    source = Path("tests/verify_keys_surfaces.py").read_text(encoding="utf-8")
    assert "capacity for the WHOLE fan-out is taken up front" not in source


def test_round3_cancel_wins_admission_race():
    from unittest.mock import patch

    wid, did = _mk_wf("mock"), _mk_doc("es-ES", 1)
    with Session(engine) as session:
        row = WorkflowRun(
            workflow_id=wid, document_id=did, target_locale="es-ES", status=RunStatus.QUEUED
        )
        session.add(row)
        session.commit()
        rid = row.id
    original = _db.commit_launch

    def cancel_before_commit(session, **kwargs):
        if kwargs.get("what") == f"admit queued run {rid}":
            with Session(engine) as other:
                orchestrator.cancel_run(other, rid)
        return original(session, **kwargs)

    token = orchestrator.RUN_SLOTS.take()[0]
    try:
        with (
            patch.object(_db, "commit_launch", cancel_before_commit),
            patch_pieces("app.services.orchestrator", "_run_in_background") as launch,
        ):
            admitted = orchestrator._AdmissionScheduler()._admit(rid, token)
            assert not admitted and not launch.called, "cancelled queue row must not be admitted"
        with Session(engine) as session:
            assert session.get(WorkflowRun, rid).status == RunStatus.CANCELLED
    finally:
        token.release()


def test_round4_saturated_scheduler_never_opens_session():
    from unittest.mock import patch

    scheduler = orchestrator._AdmissionScheduler()
    scheduler._batches_dirty.set()
    with (
        patch.object(_db, "pool_has_no_capacity", return_value=True),
        patch_pieces("app.services.batch_runs", "finalize_ready_batches") as finalize,
        patch_pieces("app.services.orchestrator", "Session") as session,
    ):
        assert scheduler.drain_once(slot_wait=0) == 0
        assert not finalize.called, "saturated admission must skip batch finalization"
        assert not session.called, "saturated admission must not open a pooled Session"
    scheduler._batches_dirty.clear()
    with (
        patch.object(_db, "pool_has_no_capacity", return_value=False),
        patch.object(scheduler, "_has_queued", return_value=False),
        patch_pieces("app.services.batch_runs", "finalize_ready_batches") as finalize,
    ):
        scheduler.drain_once(slot_wait=0)
        scheduler.drain_once(slot_wait=0)
        assert not finalize.called, "idle admission must not rescan unfinished batches"


def test_round4_sync_batch_drives_admission_and_joins():
    from unittest.mock import patch

    from app.services import batch_runs

    wid, did = _mk_wf("mock"), _mk_doc("es-ES", 1)
    scheduler = orchestrator._AdmissionScheduler()
    cleanup = threading.Event()
    joined = threading.Event()
    original_member = batch_runs._run_member
    original_join = threading.Thread.join

    def member(*args, **kwargs):
        original_member(*args, **kwargs)
        assert cleanup.wait(10), "test cleanup must be released by the caller's join"

    def join(thread, *args, **kwargs):
        if thread.name.startswith("loc-batch-member-"):
            joined.set()
            cleanup.set()
        return original_join(thread, *args, **kwargs)

    try:
        with (
            patch_pieces("app.services.orchestrator", "ADMISSION", scheduler),
            patch.object(scheduler, "ensure_started", return_value=False),
            patch_pieces("app.services.orchestrator", "QUEUE_DRAIN_WAIT_S", 0.5),
            patch_pieces("app.services.batch_runs", "_run_member", member),
            patch.object(threading.Thread, "join", join),
            Session(engine) as s,
        ):
            batch = batch_runs.start_batch(
                s, workflow_id=wid, doc_id=did, locales=["es-ES"], synchronous=True
            )
            assert batch.finished_at is not None, (
                "synchronous caller must drive admission without daemon"
            )
            assert joined.is_set(), (
                "caller must join after the terminal row, through thread cleanup"
            )
            ids = {r.id for r in batch_runs.batch_members(s, batch)}
            assert not any(
                t.name == f"loc-batch-member-{rid}" for t in threading.enumerate() for rid in ids
            )
    finally:
        cleanup.set()
        for thread in threading.enumerate():
            if thread.name.startswith("loc-batch-member-"):
                original_join(thread, 2)


def test_round4_contracts():
    import ast
    import inspect
    from pathlib import Path

    import app.main as main_mod
    from app.services import batch_runs, keys
    from app.web import keys as web_keys

    failures = []

    def check(ok, finding):
        if not ok:
            failures.append(finding)

    check(
        "batch.fail_fast = False" not in inspect.getsource(batch_runs.rerun_batch),
        "M1 persisted fail_fast",
    )
    check(
        hasattr(orchestrator, "SPEND_CAP_STOP_NOTE")
        and hasattr(orchestrator, "FAIL_FAST_STOP_NOTE"),
        "M2 truthful stop reasons",
    )
    with engine.connect() as conn:
        indexes = conn.exec_driver_sql("PRAGMA index_list('workflowrun')").all()
    check(any("status" in row[1] for row in indexes), "M3 queue index")
    migration = Path("alembic/versions/e2b6c9f4a713_workflowrun_queue_group.py")
    index_calls = {
        call.args[0].value
        for call in ast.walk(ast.parse(migration.read_text(encoding="utf-8")))
        if isinstance(call, ast.Call)
        and isinstance(call.func, ast.Attribute)
        and call.func.attr == "create_index"
    }
    check("ix_workflowrun_status_id" in index_calls, "M3 migration queue index")
    check(
        ("ix_workflowrun_status_id", "workflowrun", "(status, id)") in _db._INDEXES,
        "M3 legacy queue index",
    )
    check(
        "method"
        in inspect.signature(__import__("app.main", fromlist=["_is_relief"])._is_relief).parameters,
        "M4 method-aware relief",
    )
    check("ip" in inspect.signature(keys.launch_translation).parameters, "M5 audit caller IP")
    check(not main_mod._is_relief("/settings", "POST"), "M4 settings save must not be relief")
    check(
        not {"max_parallel", "fail_fast", "on_member_done", "slots"}
        & set(inspect.signature(batch_runs._dispatch).parameters),
        "L3 dead dispatch parameters",
    )
    check(
        "[r for _l, _dn, r in queued]" not in inspect.getsource(web_keys.keys_translate),
        "M7 queued SSE tracking",
    )
    check(hasattr(orchestrator.ADMISSION, "supervise"), "M8 daemon supervisor")
    queue_source = inspect.getsource(orchestrator.queue_runs)
    check("    with _RUN_CREATE_LOCK:\n        for wf_id" not in queue_source, "L1 whole-plan lock")
    check(
        "pending/queued/running/paused" in inspect.getsource(orchestrator.cancel_run),
        "L2 cancellation refusal",
    )
    check(not hasattr(web_keys, "_run_actor"), "L3 dead actor helper")
    check(
        "['pending', 'queued', 'running']"
        not in Path("app/templates/run_view.html").read_text(encoding="utf-8"),
        "M7 queued pages must not reconnect streams",
    )
    check(" more ({_names})" not in inspect.getsource(web_keys.keys_translate), "L4 queued more")
    check('actor="system"' not in inspect.getsource(keys.launch_translation), "L5 threshold actor")
    check(
        'info.get("queued")'
        in "".join(
            f.read_text(encoding="utf-8") for f in sorted(Path("app/web").glob("api_cli*.py"))
        ),
        "L7 queued reattachment",
    )
    for path, marker in (
        ("docs/CLI.md", "POST /api/projects/{id}/keys/translate"),
        ("app/templates/help.html", "/api/projects/{pid}/keys/translate"),
        ("app/capabilities/llm.md", "/api/projects/{pid}/keys/translate"),
        ("integrations/mcp-server/registry.py", 'name="translate_keys"'),
    ):
        source = Path(path).read_text(encoding="utf-8")
        location = source.rindex(marker)
        source = source[max(0, location - 500) : location + 1100]
        check(
            "launched" in source
            and "queued" in source
            and "QUEUED" in source
            and "queue_group" in source
            and "admitted immediately" in source
            and "queue_incomplete" in source,
            f"H2 descriptor {path}",
        )
    assert not failures, failures


def test_round5_relief_session_bound():
    from sqlalchemy.exc import TimeoutError

    held = []
    try:
        for _ in range(4):
            session = Session(_db._emergency_engine_for_relief())
            session.connection()
            held.append(session)
        extra = Session(_db._emergency_engine_for_relief())
        try:
            extra.connection()
        except TimeoutError:
            pass
        else:
            raise AssertionError("M4 relief must never exceed four simultaneous sessions")
        finally:
            extra.close()
    finally:
        for session in held:
            session.close()


def test_round4_immediate_ids_and_audit():
    from unittest.mock import patch

    from app.models import AuditLog
    from app.services import keys, project_budgets

    pid = _key_project(["de-DE", "fr-FR", "ru-RU"])
    wid = _mk_wf("mock")
    scheduler = orchestrator._AdmissionScheduler()
    tokens = []

    def launch(*args, slot, **kwargs):
        slot.handed_off = True
        tokens.append(slot)

    with (
        patch.object(config, "MAX_CONCURRENT_RUNS", 1),
        patch_pieces("app.services.orchestrator", "ADMISSION", scheduler),
        patch.object(scheduler, "ensure_started", return_value=False),
        patch_pieces("app.services.orchestrator", "_run_in_background", launch),
        patch.object(project_budgets, "evaluate_thresholds") as thresholds,
        Session(engine) as session,
    ):
        try:
            out = keys.launch_translation(
                session,
                session.get(Project, pid),
                wid,
                "all",
                actor="round4-operator",
                ip="192.0.2.44",
            )
            assert len(out["launched"]) == 1 and len(out["queued"]) == 2, out
            assert len(set(out["launched"] + [r["run_id"] for r in out["queued"]])) == 3
            row = session.exec(
                select(AuditLog)
                .where(AuditLog.action == "keys.translate")
                .order_by(AuditLog.id.desc())
            ).first()
            assert row.ip == "192.0.2.44" and "launched=1 queued=2" in row.detail, row
            assert thresholds.call_args.kwargs["actor"] == "round4-operator"
            for rid in out["launched"] + [r["run_id"] for r in out["queued"]]:
                orchestrator.cancel_run(session, rid)
        finally:
            for token in tokens:
                token.release()


def test_round4_sync_deadline_names_queued_members():
    from unittest.mock import patch

    from app.services import batch_runs

    wid, did = _mk_wf("mock"), _mk_doc("es-ES", 1)
    token = orchestrator.RUN_SLOTS.take()[0]
    try:
        with (
            patch.object(config, "MAX_CONCURRENT_RUNS", 1),
            patch_pieces("app.services.orchestrator", "QUEUE_DRAIN_WAIT_S", 0.01),
            Session(engine) as session,
        ):
            batch = batch_runs.start_batch(
                session, workflow_id=wid, doc_id=did, locales=None, synchronous=True
            )
            out = batch_runs.batch_as_dict(session, batch)
            assert out["status"] == "running" and out["error"]["code"] == "drain_incomplete", out
            ids = [r.id for r in batch_runs.batch_members(session, batch)]
            with Session(engine) as fresh:
                reloaded = fresh.get(batch_runs.WorkflowBatch, batch.id)
                assert batch_runs.batch_as_dict(fresh, reloaded)["error"] == out["error"]
            assert out["error"]["queued_members"] == ids and out["error"]["reason"]
            for rid in ids:
                orchestrator.cancel_run(session, rid)
            import json

            from tests._cli_runner import run_cli

            code, output, error = run_cli(
                ["--json", "workflow", "run", str(did), str(wid), "--all-locales"]
            )
            payload = json.loads(output.strip().splitlines()[-1])
            assert code != 0 and payload["error"]["code"] == "drain_incomplete", (
                code,
                payload,
                error,
            )
            for member in payload["members"]:
                orchestrator.cancel_run(session, member["run_id"])
    finally:
        token.release()


def test_round4_retry_preserves_fail_fast():
    from unittest.mock import patch

    from app.services import batch_runs

    wid, did = _mk_wf("mock"), _mk_doc("es-ES", 1)
    with Session(engine) as session:
        batch = batch_runs.start_batch(
            session, workflow_id=wid, doc_id=did, locales=None, fail_fast=True, synchronous=True
        )
        with patch.object(orchestrator.ADMISSION, "ensure_started", return_value=False):
            batch = batch_runs.rerun_batch(
                session, batch=batch, locales=["es-ES"], synchronous=True
            )
        session.refresh(batch)
        assert batch.fail_fast is True, "retry exemption must never mutate saved fail_fast"
        latest = batch_runs.batch_members(session, batch)[-1]
        assert latest.queue_group.startswith("batch-retry:")
        assert latest.status == RunStatus.COMPLETED


def test_round4_daemon_death_is_supervised():
    from unittest.mock import patch

    scheduler = orchestrator._AdmissionScheduler()
    wid, did = _mk_wf("mock"), _mk_doc("es-ES", 1)
    with Session(engine) as session:
        row = WorkflowRun(
            workflow_id=wid,
            document_id=did,
            target_locale="es-ES",
            status=RunStatus.QUEUED,
            queue_group="supervised-r4",
        )
        session.add(row)
        session.commit()
        rid = row.id
    with patch.object(scheduler, "drain_once", side_effect=SystemExit("seeded daemon death")):
        scheduler.ensure_started()
        scheduler.notify()
        # Join completion, not a two-second logging/CPU deadline: Rich can still
        # be rendering the seeded exception under contention. Death remains required.
        scheduler._thread.join(30)
        assert scheduler.health_detail() == "dead"
    with Session(engine) as session:
        assert session.get(WorkflowRun, rid).status == RunStatus.QUEUED
    with (
        patch.object(scheduler, "ensure_started", return_value=False) as start,
        patch.object(orchestrator.log, "error") as error,
    ):
        scheduler.supervise(now=100)
        scheduler.supervise(now=101)
        assert start.call_count == 1, "restart attempts must be bounded per interval"
        assert error.called, "a dead daemon must be logged at ERROR"
    try:
        assert scheduler.supervise(now=131) == "restarted"
        deadline = time.monotonic() + 4
        while time.monotonic() < deadline:
            with Session(engine) as session:
                if session.get(WorkflowRun, rid).status == RunStatus.COMPLETED:
                    break
            time.sleep(0.02)
        else:
            raise AssertionError("supervisor restart must drain the same durable queued row")
    finally:
        with patch.object(scheduler, "drain_once", side_effect=SystemExit("cleanup")):
            scheduler.notify()
            scheduler._thread.join(30)
        assert not scheduler.alive()

    from contextlib import suppress
    from types import SimpleNamespace
    from unittest.mock import Mock

    from app.services import scheduler as lifecycle_scheduler

    with (
        patch.object(
            lifecycle_scheduler,
            "time",
            SimpleNamespace(sleep=Mock(side_effect=[None, SystemExit()])),
        ),
        patch.object(lifecycle_scheduler, "tick"),
        patch.object(orchestrator.ADMISSION, "supervise") as supervise,
    ):
        with suppress(SystemExit):
            lifecycle_scheduler._loop()
        assert supervise.call_count == 1, "lifecycle loop must actually supervise admission"


def test_round4_queued_observer_is_static():
    wid, did = _mk_wf("mock"), _mk_doc("es-ES", 1)
    with Session(engine) as session:
        run = WorkflowRun(
            workflow_id=wid, document_id=did, target_locale="es-ES", status=RunStatus.QUEUED
        )
        session.add(run)
        session.commit()
        events = list(orchestrator.observe_run(run.id, max_seconds=0.01))
        assert len(events) == 1 and events[0][1] == {
            "type": "closed",
            "run_id": run.id,
            "status": "queued",
        }
        orchestrator.cancel_run(session, run.id)


def test_round4_policy_stop_reasons():
    from unittest.mock import patch

    from app.models import WorkflowBatch
    from app.services import batch_runs

    wid, did = _mk_wf("mock"), _mk_doc("es-ES", 1)
    for capped, code in ((True, "spend_cap_stop"), (False, "fail_fast_stop")):
        with Session(engine) as session:
            batch = WorkflowBatch(workflow_id=wid, document_id=did, fail_fast=True, max_parallel=1)
            session.add(batch)
            session.commit()
            for status in (RunStatus.FAILED, RunStatus.QUEUED):
                row = WorkflowRun(
                    workflow_id=wid,
                    document_id=did,
                    target_locale="es-ES",
                    batch_id=batch.id,
                    status=status,
                )
                session.add(row)
            session.commit()
            with patch.object(batch_runs.spend, "would_block", return_value=(capped, 5, 5)):
                assert not batch_runs.queue_member_allowed(session, row)
            session.refresh(row)
            reason = orchestrator.run_reason(row)
            assert row.status == RunStatus.FAILED and reason["code"] == code, reason
            assert "healthy" not in reason["text"] and ".." not in reason["text"], reason


def test_round4_relief_healthy_pool_and_episode_reset():
    import asyncio
    from unittest.mock import patch

    import app.main as main_mod

    observed = []

    async def endpoint(scope, receive, send):
        observed.append(_db.in_relief_scope())

    async def drive():
        middleware = main_mod._CapacityMiddleware(endpoint)
        for path, method, saturated in (
            ("/health", "GET", True),
            ("/static/style.css", "GET", True),
            ("/settings", "GET", False),
            ("/runs/1/cancel", "POST", True),
        ):
            with (
                patch.object(_db, "pool_has_no_capacity", return_value=saturated),
                patch.object(main_mod, "_shed_episode_over") as reset,
            ):
                await middleware({"type": "http", "path": path, "method": method}, None, None)
                assert reset.call_count == (0 if path in ("/health", "/static/style.css") else 1), (
                    "M3 health must not reset shedding",
                    path,
                )

    asyncio.run(drive())
    assert observed == [False, False, False, True], observed
    _db.begin_pool_budget()


def test_round4_cancel_wins_policy_stop():
    from types import SimpleNamespace
    from unittest.mock import patch

    from app.models import WorkflowBatch
    from app.services import batch_runs

    wid, did = _mk_wf("mock"), _mk_doc("es-ES", 1)
    with Session(engine) as session:
        batch = WorkflowBatch(workflow_id=wid, document_id=did, max_parallel=1)
        session.add(batch)
        session.commit()
        row = WorkflowRun(
            workflow_id=wid,
            document_id=did,
            target_locale="es-ES",
            status=RunStatus.QUEUED,
            batch_id=batch.id,
        )
        session.add(row)
        session.commit()
        rid = row.id
        original_exec = session.exec

        def cancel_after_read(statement, *args, **kwargs):
            result = original_exec(statement, *args, **kwargs)
            if str(statement).startswith("SELECT") and "workflowrun.status =" in str(statement):
                waiting = result.all()
                with Session(engine) as other:
                    orchestrator.cancel_run(other, rid)
                return SimpleNamespace(all=lambda: waiting)
            return result

        with (
            patch.object(session, "exec", cancel_after_read),
            patch.object(batch_runs.spend, "would_block", return_value=(True, 5, 5)),
        ):
            assert not batch_runs.queue_member_allowed(session, row)
        session.refresh(row)
        assert row.status == RunStatus.CANCELLED, (
            "policy stop must not overwrite a concurrent cancel"
        )
        assert orchestrator.run_reason(row)["code"] == "cancelled_by_operator"


def test_round4_wait_keeps_admission_snapshot():
    from unittest.mock import patch

    from app.services import keys

    pid = _key_project(["de-DE", "fr-FR", "ru-RU"])
    wid = _mk_wf("mock")
    scheduler = orchestrator._AdmissionScheduler()
    original = scheduler.drain_once

    def one_immediate(**kwargs):
        if kwargs.get("limit") is not None:
            kwargs["limit"] = 1
        return original(**kwargs)

    with (
        patch_pieces("app.services.orchestrator", "ADMISSION", scheduler),
        patch.object(scheduler, "drain_once", one_immediate),
        Session(engine) as session,
    ):
        try:
            out = keys.launch_translation(
                session, session.get(Project, pid), wid, "all", actor="snapshot", wait=True
            )
            assert len(out["launched"]) == 1 and len(out["queued"]) == 2, out
            assert len(out["started"]) == 3 and out["waiting"] == [], out
        finally:
            with patch.object(scheduler, "drain_once", side_effect=SystemExit("cleanup")):
                scheduler.notify()
                if scheduler._thread:
                    scheduler._thread.join(30)


def test_round5_bookkeeping_survives_relief_exhaustion():
    from contextlib import ExitStack
    from unittest.mock import patch

    from sqlalchemy.exc import TimeoutError

    wid, did = _mk_wf("mock"), _mk_doc("es-ES", 1)
    with Session(engine) as session:
        row = WorkflowRun(workflow_id=wid, document_id=did, status=RunStatus.QUEUED)
        session.add(row)
        session.commit()
        rid = row.id
    with ExitStack() as stack:
        for _ in range(config.DB_POOL_SIZE + config.DB_MAX_OVERFLOW):
            stack.enter_context(engine.connect())
        for _ in range(_db.RELIEF_MAX_SESSIONS):
            relief = stack.enter_context(Session(_db._emergency_engine_for_relief()))
            relief.connection()
        assert _db.pool_has_no_capacity()
        with patch.object(engine.pool, "_timeout", 0.02):
            orchestrator._mark_queue_launch_failed(rid, TimeoutError("gate saturation"))
        with _db.emergency_session() as fresh:
            row = fresh.get(WorkflowRun, rid)
            assert row.status == RunStatus.FAILED and _db.POOL_EXHAUSTED_NOTE in row.notes, (
                "H1 bookkeeping must survive every relief permit being held",
                row,
            )


def test_round5_wait_checkout_budget_and_cursor():
    from unittest.mock import patch

    from sqlalchemy import event

    from app.services import batch_runs

    wid, did = _mk_wf("mock"), _mk_doc("es-ES", 1)
    with patch.object(orchestrator.ADMISSION, "ensure_started"), Session(engine) as s:
        batch = batch_runs.start_batch(
            s, workflow_id=wid, doc_id=did, locales=None, synchronous=False
        )
        bid = batch.id
        s.commit()
    # Stop before the 10 s reconcile boundary; retain the six-checkout ceiling,
    # including the final wait-error read, without racing a fifth periodic reconcile.
    checkouts = []

    def checkout(*args):
        checkouts.append(time.monotonic())

    token = orchestrator.RUN_SLOTS.take()[0]
    event.listen(engine, "checkout", checkout)
    try:
        with (
            patch.object(config, "MAX_CONCURRENT_RUNS", 1),
            patch_pieces("app.services.orchestrator", "QUEUE_DRAIN_WAIT_S", 9.5),
        ):
            error = batch_runs._wait_batch(bid)
        assert error["code"] == "drain_incomplete"
        assert len(checkouts) <= 6, ("M1 at most one checkout per two-second reconcile", checkouts)
    finally:
        event.remove(engine, "checkout", checkout)
        token.release()
        with Session(engine) as s:
            for row in s.exec(select(WorkflowRun).where(WorkflowRun.batch_id == bid)).all():
                orchestrator.cancel_run(s, row.id)


def test_round5_sync_cursor_is_private():
    from app.services import batch_runs

    wid, did = _mk_wf("mock"), _mk_doc("es-ES", 1)
    scheduler = orchestrator.ADMISSION
    previous = scheduler._last_group
    scheduler._last_group = "gate-daemon-cursor"
    try:
        with Session(engine) as s:
            batch_runs.start_batch(s, workflow_id=wid, doc_id=did, locales=None, synchronous=True)
        assert scheduler._last_group == "gate-daemon-cursor", "L5 caller must own its cursor"
    finally:
        scheduler._last_group = previous


def test_round5_wait_pool_failure_is_named():
    from unittest.mock import patch

    from sqlalchemy.exc import TimeoutError

    from app.services import batch_runs

    wid, did = _mk_wf("mock"), _mk_doc("es-ES", 1)
    with patch.object(orchestrator.ADMISSION, "ensure_started"), Session(engine) as s:
        batch = batch_runs.start_batch(
            s, workflow_id=wid, doc_id=did, locales=None, synchronous=False
        )
        bid = batch.id
        s.commit()
    with patch_pieces(
        "app.services.batch_runs", "Session", side_effect=TimeoutError("mid-wait checkout")
    ):
        error = batch_runs._wait_batch(bid)
    assert error["code"] == "db_pool_exhausted", error
    with Session(engine) as s:
        batch = s.get(batch_runs.WorkflowBatch, bid)
        assert "pool saturated during synchronous wait;" in batch.notes
        assert batch_runs.batch_as_dict(s, batch)["error"] == error
        for rid in error["queued_members"]:
            orchestrator.cancel_run(s, rid)
    assert not any(t.name.startswith("loc-batch-member-") for t in threading.enumerate())


def test_round5_cli_pool_error_no_traceback():

    from sqlalchemy.exc import TimeoutError

    from tests._cli_runner import run_cli

    wid, did = _mk_wf("mock"), _mk_doc("es-ES", 1)
    with patch_pieces(
        "app.services.batch_runs", "start_batch", side_effect=TimeoutError("pool gone")
    ):
        code, output, error = run_cli(["workflow", "run", str(did), str(wid), "--all-locales"])
    assert code != 0 and "db_pool_exhausted" in output + error
    assert "Traceback" not in output + error


def test_round5_sync_public_saturated_result():
    import json
    from contextlib import ExitStack
    from unittest.mock import patch

    from app.services import batch_runs
    from tests._cli_runner import run_cli

    wid, did = _mk_wf("mock"), _mk_doc("es-ES", 1)
    original = batch_runs._wait_batch
    with ExitStack() as held:

        def saturate_after_staging(bid, callback=None):
            for _ in range(config.DB_POOL_SIZE + config.DB_MAX_OVERFLOW):
                held.enter_context(engine.connect())
            # A real failed reconcile checkout, not capacity alone, names the pool.
            with (
                patch.object(_db, "pool_has_no_capacity", return_value=False),
                patch.object(engine.pool, "_timeout", 0.02),
            ):
                return original(bid, callback)

        with (
            patch_pieces("app.services.batch_runs", "_wait_batch", saturate_after_staging),
            patch.object(batch_runs.log, "warning") as warning,
        ):
            code, output, error = run_cli(
                ["--json", "workflow", "run", str(did), str(wid), "--all-locales"]
            )
        view = json.loads(output.strip().splitlines()[-1])
        assert code != 0 and view["error"]["code"] == "db_pool_exhausted", view
        assert warning.call_args.kwargs.get("exc_info") is True
        assert "Traceback" not in output + error
        assert view["error"]["queued_members"], view
        with _db.emergency_session() as s:
            batch = s.get(batch_runs.WorkflowBatch, view["id"])
            assert batch_runs.batch_as_dict(s, batch)["error"] == view["error"]
    with Session(engine) as s:
        for rid in view["error"]["queued_members"]:
            orchestrator.cancel_run(s, rid)


def test_round5_queued_page_poll_behavior():
    import json
    import shutil
    from pathlib import Path

    from starlette.requests import Request
    from starlette.responses import JSONResponse

    from app.web import workflows

    snapshot = json.loads(Path("tests/branding_contract_snapshot.json").read_text(encoding="utf-8"))
    assert snapshot["routes"].get("/runs/{run_id}/status.json") == ["GET"]
    wid, did = _mk_wf("mock"), _mk_doc("es-ES", 1)
    with Session(engine) as s:
        row = WorkflowRun(workflow_id=wid, document_id=did, status=RunStatus.QUEUED)
        s.add(row)
        s.commit()
        request = Request({"type": "http", "path": f"/runs/{row.id}/status.json"})
        with patch_pieces("app.web.workflows", "_visibility_block", return_value=None):
            response = workflows.run_status_json(row.id, request, s)
            assert json.loads(response.body)["status"] == "queued"
            row.status = RunStatus.COMPLETED
            s.add(row)
            s.commit()
            assert (
                json.loads(workflows.run_status_json(row.id, request, s).body)["status"]
                == "completed"
            )
        denied = JSONResponse({"ok": False}, status_code=403)
        with patch_pieces("app.web.workflows", "_visibility_block", return_value=denied):
            assert workflows.run_status_json(row.id, request, s) is denied
    node = shutil.which("node")
    if not node:
        print("SKIP queued-page JavaScript behavior: Node is unavailable")
        return
    script = Path("app/static/queuedrun.js").read_text(encoding="utf-8")
    harness = (
        r"""
const assert = require('node:assert/strict');
const tasks = []; let reads = 0, reloads = 0, status = 'queued';
global.document = {visibilityState:'hidden', querySelector:()=>({dataset:{queuedRun:'42'}})};
global.window = {setTimeout:(f, ms)=>{assert(ms>=2000);tasks.push(f)},
                 location:{reload:()=>reloads++}};
global.fetch = async url => {assert.equal(url, '/runs/42/status.json'); reads++;
                            return {ok:true,json:async()=>({status})}};
global.EventSource = function(){throw Error('queued pages must not stream')};
"""
        + script
        + r"""
(async()=>{
  await tasks.shift()(); assert.equal(reads,0);
  document.visibilityState='visible';
  await tasks.shift()(); assert.equal(reads,1); assert.equal(reloads,0);
  status='running'; await tasks.shift()();
  assert.equal(reloads,1); assert.equal(tasks.length,0);
})().catch(e=>{console.error(e);process.exitCode=1});
"""
    )
    result = subprocess.run([node, "-e", harness], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


def test_round5_health_preserves_shed_episode():
    import asyncio
    from unittest.mock import patch

    import app.main as main_mod

    async def endpoint(scope, receive, send):
        pass

    async def send(message):
        pass

    async def drive():
        middleware = main_mod._CapacityMiddleware(endpoint)
        for _ in range(6):
            await middleware(
                {"type": "http", "path": "/projects", "method": "GET", "headers": []}, None, send
            )
            for path in ("/health", "/ready", "/static/app.css"):
                with patch.object(main_mod, "_shed_log_lock") as lock:
                    await middleware({"type": "http", "path": path, "method": "GET"}, None, send)
                    assert not lock.__enter__.called, "M3 probes must never take shed lock"

    main_mod._shed_episode_over()
    with (
        patch.object(_db, "pool_has_no_capacity", return_value=True),
        patch.object(main_mod._logger, "warning") as warning,
    ):
        asyncio.run(drive())
        assert warning.call_count == 1, "M3 six sheds with health probes must log once"
        main_mod._shed_episode_over()
        assert warning.call_count == 2 and "5" in str(warning.call_args), warning.call_args


def test_round5_keys_own_group_and_reattachment_wake():
    from unittest.mock import patch

    from app.services import keys

    pid = _key_project(["de-DE", "fr-FR"])
    wid = _mk_wf("mock")
    with (
        Session(engine) as s,
        patch.object(orchestrator.ADMISSION, "drain_once") as drain,
        patch.object(orchestrator.ADMISSION, "ensure_started") as start,
        patch.object(orchestrator.ADMISSION, "notify") as notify,
    ):
        out = keys.launch_translation(s, s.get(Project, pid), wid, "all", actor="gate")
        assert drain.call_args.kwargs.get("queue_group") == out["queue_group"], (
            "L1 scope immediate admission"
        )
        start.reset_mock()
        notify.reset_mock()
        again = keys.launch_translation(s, s.get(Project, pid), wid, "all", actor="gate")
        assert len(again["queued"]) == 2
        assert start.called and notify.called, "L2 attached-only queue must wake admission"
        unrelated = WorkflowRun(
            workflow_id=wid,
            document_id=s.get(WorkflowRun, out["queued"][0]["run_id"]).document_id,
            status=RunStatus.QUEUED,
            queue_group="000-other-operator",
        )
        s.add(unrelated)
        s.commit()
        scheduler = orchestrator._AdmissionScheduler()
        picked = scheduler._next_queued(queue_group=out["queue_group"])
        assert picked[0] in {r["run_id"] for r in out["queued"]}, "L1 select only this queue group"
        assert not scheduler._has_queued(queue_group="missing-group"), "L1 scoped presence check"
        orchestrator.cancel_run(s, unrelated.id)
        for row in out["queued"]:
            orchestrator.cancel_run(s, row["run_id"])


def test_round5_bounded_pills_and_queued_refresh():
    from pathlib import Path

    from app.web import keys as web_keys

    pid = _key_project(["es-ES"])
    wid = _mk_wf("mock")
    with Session(engine) as s:
        project = s.get(Project, pid)
        from app.services import keys

        did = keys.translatable_documents(s, project)[0].id
        for _ in range(72):
            s.add(WorkflowRun(workflow_id=wid, document_id=did, status=RunStatus.QUEUED))
        s.commit()
        rows = web_keys._queued_run_pills(s, project)
        for row in s.exec(select(WorkflowRun).where(WorkflowRun.document_id == did)).all():
            orchestrator.cancel_run(s, row.id)
        assert len(rows) <= 7, "L3 query must fetch only six pills plus overflow sentinel"
    template = Path("app/templates/project_keys.html").read_text(encoding="utf-8")
    assert "queued_runs[:6]" in template and "Showing the first 6" in template, (
        "L3 truncation notice"
    )
    template = Path("app/templates/run_view.html").read_text(encoding="utf-8")
    assert 'data-queued-run="{{ run.id }}"' in template, "L4 queued page must poll for admission"
    script = Path("app/static/queuedrun.js").read_text(encoding="utf-8")
    assert "visibilityState" in script and "setTimeout" in script and "location.reload" in script


def test_round5_relief_race_is_documented():
    import inspect

    import app.main as main_mod

    assert "first checkout" in inspect.getsource(main_mod._CapacityMiddleware), (
        "L6 document relief race"
    )


def test_round5_wait_note_survives_write_retry():
    import sqlite3
    from unittest.mock import patch

    from sqlalchemy.exc import OperationalError

    from app.services import batch_runs

    wid, did = _mk_wf("mock"), _mk_doc("es-ES", 1)
    with Session(engine) as s:
        batch = batch_runs.WorkflowBatch(workflow_id=wid, document_id=did)
        s.add(batch)
        s.commit()
        bid = batch.id
        s.add(WorkflowRun(workflow_id=wid, document_id=did, batch_id=bid, status=RunStatus.QUEUED))
        s.commit()
    original = Session.commit
    attempts = []

    def commit(session):
        attempts.append(1)
        if len(attempts) == 1:
            raise OperationalError("UPDATE", {}, sqlite3.OperationalError("database is locked"))
        return original(session)

    with patch.object(Session, "commit", commit):
        error = batch_runs._sync_wait_error(bid, pool_failed=True)
    assert len(attempts) == 2
    with Session(engine) as s:
        note = s.get(batch_runs.WorkflowBatch, bid).notes or ""
        assert error["reason"] in note, "M2 a writer-lock rollback must not discard the wait note"


def test_round5_retry_clears_previous_wait_error():
    from unittest.mock import patch

    from app.services import batch_runs

    wid, did = _mk_wf("mock"), _mk_doc("es-ES", 1)
    with Session(engine) as s:
        batch = batch_runs.start_batch(
            s, workflow_id=wid, doc_id=did, locales=None, synchronous=True
        )
        batch.notes = f"operator note. {batch_runs.SYNC_POOL_NOTE} 1 member(s) still queued"
        batch.__dict__["drain_error"] = {"code": "db_pool_exhausted", "reason": batch.notes}
        s.add(batch)
        s.commit()
        with patch.object(orchestrator.ADMISSION, "ensure_started"):
            batch = batch_runs.rerun_batch(s, batch=batch, locales=["es-ES"], synchronous=False)
        view = batch_runs.batch_as_dict(s, batch)
        for row in batch_runs.batch_members(s, batch):
            if row.status == RunStatus.QUEUED:
                orchestrator.cancel_run(s, row.id)
        assert view["error"] is None, (
            "M2 a new attempt must not report its predecessor's wait error"
        )
        assert "operator note." in view["notes"]


def _round6_batch(status):
    from app.models import WorkflowBatch

    wid, did = _mk_wf("mock"), _mk_doc("es-ES", 1)
    with Session(engine) as s:
        batch = WorkflowBatch(
            workflow_id=wid, document_id=did, requested_locales=["es-ES"], status="running"
        )
        s.add(batch)
        s.commit()
        row = WorkflowRun(
            workflow_id=wid,
            document_id=did,
            batch_id=batch.id,
            target_locale="es-ES",
            status=status,
        )
        s.add(row)
        s.commit()
        return batch.id, row.id


def test_round6_completion_before_capacity_and_failure():
    from contextlib import ExitStack

    from app.services import batch_runs

    for saturated in (False, True):
        for failed_read in (False, True):
            bid, _ = _round6_batch(RunStatus.COMPLETED)
            with ExitStack() as stack:
                if saturated:
                    for _ in range(_pool_capacity()):
                        stack.enter_context(engine.connect())
                if failed_read:
                    stack.enter_context(
                        patch_pieces(
                            "app.services.batch_runs",
                            "Session",
                            side_effect=sa_exc.TimeoutError("checkout"),
                        )
                    )
                assert batch_runs._wait_batch(bid) is None, "H1 complete batch must win"
                with _db.emergency_session() as s:
                    batch = s.get(batch_runs.WorkflowBatch, bid)
                    assert batch.status == "completed" and batch.finished_at is not None
                    assert not batch.notes
                    batch.notes = f"{batch_runs.SYNC_POOL_NOTE} 1 member(s) still queued"
                    batch.__dict__["drain_error"] = {"code": "db_pool_exhausted"}
                    assert batch_runs.batch_as_dict(s, batch)["error"] is None, "H1 obsolete error"


def test_round6_wait_exception_pairs():
    from unittest.mock import patch

    from app.services import batch_runs
    from tests._cli_runner import run_cli

    failures = (
        (sa_exc.TimeoutError("checkout"), "db_pool_exhausted"),
        (
            sa_exc.OperationalError("connect", {}, Exception("QueuePool limit reached")),
            "db_pool_exhausted",
        ),
        (ValueError("a plain bug, not the pool"), "wait_internal_error"),
        (sa_exc.OperationalError("select", {}, Exception("no such table")), "wait_internal_error"),
    )
    for exc, code in failures:
        bid, rid = _round6_batch(RunStatus.QUEUED)
        with (
            patch_pieces("app.services.batch_runs", "Session", side_effect=exc),
            patch.object(batch_runs.log, "warning") as warning,
        ):
            error = batch_runs._wait_batch(bid)
        assert error["code"] == code, error
        assert warning.call_args.kwargs.get("exc_info") is True, "M1 retain traceback"
        if code == "wait_internal_error":
            assert str(exc) in error["reason"] and batch_runs.SYNC_POOL_NOTE not in error["reason"]
        with Session(engine) as s:
            batch = s.get(batch_runs.WorkflowBatch, bid)
            assert error["reason"] in batch.notes
            assert batch_runs.batch_as_dict(s, batch)["error"] == error
            did, wid = batch.document_id, batch.workflow_id
            orchestrator.cancel_run(s, rid)
        with patch_pieces("app.services.batch_runs", "_wait_batch", return_value=error):
            status, output, stderr = run_cli(
                ["workflow", "run", str(did), str(wid), "--all-locales"]
            )
        assert status != 0 and code in output + stderr and "Traceback" not in output + stderr


def test_round6_reads_use_only_caller_session():
    from contextlib import ExitStack
    from unittest.mock import patch

    from app.services import batch_runs

    for saturated in (False, True):
        for with_error in (False, True):
            bid, _ = _round6_batch(RunStatus.QUEUED)
            with ExitStack() as stack:
                s = stack.enter_context(Session(engine))
                batch = s.get(batch_runs.WorkflowBatch, bid)
                if with_error:
                    batch.__dict__["drain_error"] = {"code": "drain_incomplete"}
                if saturated:
                    while not _db.pool_has_no_capacity():
                        stack.enter_context(engine.connect())
                s.expire(batch)
                with patch.object(
                    _db,
                    "emergency_session",
                    side_effect=AssertionError("M3 pure read opened emergency session"),
                ):
                    view = batch_runs.batch_as_dict(s, batch, detail=True)
                assert view["id"] == bid and len(view["members"]) == 1


def test_round6_missing_and_unowned_wait_bound():
    from unittest.mock import patch

    from app.services import batch_runs

    assert batch_runs._sync_wait_error(999999)["code"] == "batch_missing"
    assert batch_runs._wait_batch(999999)["code"] == "batch_missing"
    for status in (RunStatus.QUEUED, RunStatus.RUNNING, RunStatus.PENDING):
        bid, rid = _round6_batch(status)
        with (
            patch_pieces("app.services.orchestrator", "QUEUE_DRAIN_WAIT_S", 0.02),
            patch.object(orchestrator.RUN_SLOTS, "acquire_for_queue", return_value=None),
            patch.object(batch_runs.threading.Event, "wait") as wait,
            patch.object(batch_runs.time, "monotonic", side_effect=[0, 1, 1, 1, 1, 1, 1, 1]),
        ):
            # A finite fake clock makes the former endless RUNNING loop fail promptly.
            error = batch_runs._wait_batch(bid)
        assert error["code"] == "drain_incomplete", (status, error)
        assert not wait.called, "L5 expired unowned work must return"
        with Session(engine) as s:
            batch = s.get(batch_runs.WorkflowBatch, bid)
            assert error["reason"] in batch.notes
            orchestrator.cancel_run(s, rid)


def test_round6_incomplete_capacity_is_only_a_read_choice():
    from contextlib import ExitStack

    from app.services import batch_runs

    for saturated in (False, True):
        bid, rid = _round6_batch(RunStatus.QUEUED)
        with ExitStack() as stack:
            if saturated:
                for _ in range(_pool_capacity()):
                    stack.enter_context(engine.connect())
            with patch_pieces("app.services.orchestrator", "QUEUE_DRAIN_WAIT_S", 0):
                error = batch_runs._wait_batch(bid)
            assert error["code"] == "drain_incomplete", "H1 successful read is never pool failure"
        with Session(engine) as s:
            orchestrator.cancel_run(s, rid)
        assert batch_runs._wait_batch(bid) is None
        with Session(engine) as s:
            batch = s.get(batch_runs.WorkflowBatch, bid)
            assert batch_runs.batch_as_dict(s, batch)["error"] is None
            assert batch_runs.DRAIN_TIMEOUT_NOTE not in (batch.notes or "")


def test_round6_optional_tools_skip_cleanly():
    import io
    import shutil
    from contextlib import redirect_stdout
    from unittest.mock import patch

    assert "test_round5_relief_race_is_documented" in globals()
    assert "test_round5_relief_race_and_immutable_history" not in globals()
    output = io.StringIO()
    with patch.object(shutil, "which", return_value=None), redirect_stdout(output):
        test_round5_queued_page_poll_behavior()
    assert "SKIP" in output.getvalue() and "Node" in output.getvalue()


def test_round6_non_http_bypass_preserves_shedding():
    import asyncio
    from unittest.mock import AsyncMock, patch

    import app.main as main_mod

    endpoint = AsyncMock()
    with patch.object(main_mod, "_shed_log_lock") as lock:
        asyncio.run(main_mod._CapacityMiddleware(endpoint)({"type": "websocket"}, None, None))
    assert endpoint.await_count == 1 and not lock.__enter__.called


def test_round6_failed_read_joins_before_completion_verdict():
    from unittest.mock import Mock, patch

    from app.services import batch_runs

    bid, rid = _round6_batch(RunStatus.RUNNING)

    def finish():
        with Session(engine) as s:
            row = s.get(WorkflowRun, rid)
            row.status = RunStatus.COMPLETED
            s.add(row)
            s.commit()

    thread = Mock()
    thread.join.side_effect = finish
    with (
        patch.dict(batch_runs._MEMBER_THREADS, {rid: thread}),
        patch_pieces(
            "app.services.batch_runs", "Session", side_effect=sa_exc.TimeoutError("checkout")
        ),
        patch.object(batch_runs.log, "warning"),
    ):
        error = batch_runs._wait_batch(bid)
    assert thread.join.called
    assert error is None, "H1 completion during cleanup must override a failed read"
    with Session(engine) as s:
        batch = s.get(batch_runs.WorkflowBatch, bid)
        assert batch.finished_at and batch.status == "completed" and not batch.notes


def test_round6_saturated_read_exception_pairs():
    from contextlib import ExitStack
    from unittest.mock import patch

    from app.services import batch_runs

    for status in (RunStatus.QUEUED, RunStatus.COMPLETED):
        for exc, code in (
            (sa_exc.TimeoutError("emergency reconcile read failed"), "db_pool_exhausted"),
            (ValueError("saturated but unrelated bug"), "wait_internal_error"),
        ):
            bid, rid = _round6_batch(status)
            original = _db.emergency_session
            calls = []

            def fail_first_read(calls=calls, exc=exc, original=original):
                calls.append(1)
                if len(calls) == 1:
                    raise exc
                return original()

            with ExitStack() as stack:
                for _ in range(_pool_capacity()):
                    stack.enter_context(engine.connect())
                with (
                    patch.object(_db, "emergency_session", fail_first_read),
                    patch.object(batch_runs.log, "warning") as warning,
                ):
                    error = batch_runs._wait_batch(bid)
                assert warning.call_args.kwargs["exc_info"] is True
                if status == RunStatus.COMPLETED:
                    assert error is None
                else:
                    assert error["code"] == code, error
            with Session(engine) as s:
                batch = s.get(batch_runs.WorkflowBatch, bid)
                if status == RunStatus.COMPLETED:
                    assert batch.finished_at and not batch.notes
                else:
                    assert batch_runs.batch_as_dict(s, batch)["error"] == error
                    orchestrator.cancel_run(s, rid)


def test_round6_wait_note_lifecycle_pairs():
    from app.services import batch_runs

    for internal in (False, True):
        bid, rid = _round6_batch(RunStatus.QUEUED)
        with Session(engine) as s:
            batch = s.get(batch_runs.WorkflowBatch, bid)
            batch.notes = "operator before."
            s.add(batch)
            s.commit()
        error = batch_runs._sync_wait_error(
            bid,
            pool_failed=not internal,
            internal_error=ValueError("first line\nsecond line") if internal else None,
        )
        with Session(engine) as s:
            batch = s.get(batch_runs.WorkflowBatch, bid)
            batch.notes += "\noperator after."
            s.add(batch)
            s.commit()
            assert batch_runs.batch_as_dict(s, batch)["error"] == error
            orchestrator.cancel_run(s, rid)
        assert batch_runs._wait_batch(bid) is None
        with Session(engine) as s:
            batch = s.get(batch_runs.WorkflowBatch, bid)
            assert "operator before." in batch.notes and "operator after." in batch.notes
            assert error["reason"] not in batch.notes
            assert batch_runs.batch_as_dict(s, batch)["error"] is None


def test_round6_non_read_exception_mapping():
    from unittest.mock import patch

    from app.services import batch_runs

    for exc in (None, ValueError("callback bug"), sa_exc.TimeoutError("callback timeout")):
        bid, rid = _round6_batch(RunStatus.QUEUED)
        with Session(engine) as s:
            row = s.get(WorkflowRun, rid)
            s.add(
                WorkflowRun(
                    workflow_id=row.workflow_id,
                    document_id=row.document_id,
                    batch_id=bid,
                    status=RunStatus.COMPLETED,
                    target_locale="fr-FR",
                )
            )
            s.commit()

        def callback(_rid, exc=exc):
            if exc is not None:
                raise exc

        with (
            patch_pieces("app.services.orchestrator", "QUEUE_DRAIN_WAIT_S", 0),
            patch.object(batch_runs.log, "warning") as warning,
        ):
            error = batch_runs._wait_batch(bid, callback)
        assert error["code"] == ("wait_internal_error" if exc else "drain_incomplete")
        assert warning.called == (exc is not None)
        if exc is not None:
            assert warning.call_args.kwargs["exc_info"] is True and str(exc) in error["reason"]
        with Session(engine) as s:
            orchestrator.cancel_run(s, rid)


def test_round6_sync_result_uses_bookkeeping_not_relief():
    """The synchronous result read is BOOKKEEPING: under a full pool it borrows the
    UNBOUNDED bookkeeping engine (db._ensure_emergency_engine) and NEVER a request relief
    permit, so the read is whole (it always completes) and never draws on the operator's
    four-permit incident budget. Renamed from ..._relief_is_whole_and_bounded to match the
    engine policy this train made final: bookkeeping never refuses, so the result read is
    NOT bounded by relief -- re-bounding it would violate that policy (gate A round 9 LOW-4;
    round 12 ruling 4). `emergency_session` is patched to raise only to prove the read does
    not route through it; the bookkeeping engine is a different, unbounded engine."""
    from contextlib import ExitStack
    from unittest.mock import patch

    from app.services import batch_runs

    for saturated in (False, True):
        for error in (None, {"code": "drain_incomplete"}):
            bid, _ = _round6_batch(RunStatus.QUEUED)
            with ExitStack() as stack:
                s = stack.enter_context(Session(engine))
                if saturated:
                    for _ in range(_pool_capacity()):
                        stack.enter_context(engine.connect())
                with patch.object(
                    _db,
                    "emergency_session",
                    side_effect=AssertionError(
                        "result read must not route through emergency_session"
                    ),
                ):
                    result = batch_runs._sync_batch_result(s, bid, error)
                    if not saturated:
                        s.expire(result)
                    view = batch_runs.batch_as_dict(s, result, detail=True)
                assert s.bind is engine and view["id"] == bid and view["members"]
    # Pool full AND all four relief permits held: the result read STILL lands (unbounded
    # bookkeeping), consuming NONE of the operator's relief budget.
    bid, _ = _round6_batch(RunStatus.QUEUED)
    with ExitStack() as stack:
        for _ in range(_pool_capacity()):
            stack.enter_context(engine.connect())
        for _ in range(_db.RELIEF_MAX_SESSIONS):
            stack.enter_context(_db._emergency_engine_for_relief().connect())
        with (
            Session(engine) as s,
            patch.object(
                _db,
                "emergency_session",
                side_effect=AssertionError("result read must not route through emergency_session"),
            ),
        ):
            result = batch_runs._sync_batch_result(s, bid, {"code": "drain_incomplete"})
            assert result.id == bid and s.bind is engine
            assert _db._relief_connections._value == 0


def test_round7_missing_batch_boundaries():
    import asyncio
    import importlib.util
    from unittest.mock import patch

    from mcp.server.mcpserver.exceptions import ToolError

    from app.services import batch_runs
    from tests._cli_runner import run_cli

    key = _api_key()
    spec = importlib.util.spec_from_file_location(
        "loc_mcp_round7", os.path.join(_ROOT, "integrations", "mcp-server", "server.py")
    )
    mcp = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mcp
    spec.loader.exec_module(mcp)
    failures = []
    for surface in (
        "cli",
        "api",
        "rerun-cli",
        "rerun-api",
        "retry-cli",
        "retry-api",
        "mcp",
        "rerun-mcp",
        "retry-mcp",
    ):
        bid, _rid = _round6_batch(RunStatus.FAILED)
        batch_runs.finalize_ready_batches()
        with Session(engine) as s:
            batch = s.get(batch_runs.WorkflowBatch, bid)
            wid, did = batch.workflow_id, batch.document_id
        deleted = []

        def delete_between_passes(ids, deleted=deleted):
            if ids and not deleted:
                with Session(engine) as s:
                    row = s.get(WorkflowRun, next(iter(ids)))
                    current = s.get(batch_runs.WorkflowBatch, row.batch_id)
                    deleted.append(current.id)
                    for member in batch_runs.batch_members(s, current):
                        s.delete(member)
                    s.flush()
                    s.delete(current)
                    s.commit()
            return False

        scheduler = orchestrator._AdmissionScheduler()
        scheduler.ensure_started = lambda: False
        with (
            patch_pieces("app.services.orchestrator", "ADMISSION", scheduler),
            patch.object(orchestrator.RUN_SLOTS, "acquire_for_queue", return_value=None),
            patch_pieces("app.services.batch_runs", "SYNC_RECONCILE_S", 0),
            patch_pieces("app.services.batch_runs", "_join_batch_members", delete_between_passes),
            TestClient(app, raise_server_exceptions=False) as client,
        ):
            try:
                if "cli" in surface:
                    args = (
                        ["workflow", "run", str(did), str(wid), "--all-locales"]
                        if surface == "cli"
                        else ["workflow", "batch", "rerun", str(bid), "--locale", "es-ES"]
                        if surface == "rerun-cli"
                        else ["workflow", "batch", "retry-failed", str(bid)]
                    )
                    code, out, err = run_cli(args)
                    assert code == 1 and "batch_missing" in err and len(err.splitlines()) == 1, (
                        code,
                        out,
                        err,
                    )
                    assert "Traceback" not in out + err
                elif "mcp" in surface:
                    statuses = []

                    class Relay:
                        async def __aenter__(self):
                            return self

                        async def __aexit__(self, *args):
                            pass

                        async def request(self, method, url, statuses=statuses, **kwargs):
                            response = client.request(method, url, **kwargs)
                            if method == "GET":
                                statuses.append(response.status_code)
                            if method == "POST" and response.status_code == 200:
                                # MCP launches asynchronously now: delete between launch
                                # and the real status GET, rather than a synchronous join.
                                current_id = response.json()["id"]
                                with Session(engine) as session:
                                    current = session.get(batch_runs.WorkflowBatch, current_id)
                                    ids = {m.id for m in batch_runs.batch_members(session, current)}
                                delete_between_passes(ids)
                            return response

                    with patch.object(mcp, "_client", Relay), patch.object(mcp, "TOKEN", key):
                        from tests._mcp_plan import aplanned_validated

                        run_args = {
                            "mode": "run_batch"
                            if surface == "mcp"
                            else "rerun"
                            if surface == "rerun-mcp"
                            else "retry_failed",
                            "workflow_id": wid,
                            "document_id": did,
                            "batch_id": bid,
                            "locales": ["es-ES"],
                        }
                        # #757: plan first (its reads are not the GET under test), then
                        # confirm with the plan_id.
                        run_args = asyncio.run(aplanned_validated(mcp, "run_workflow", run_args))
                        statuses.clear()
                        try:
                            result = asyncio.run(mcp.mcp.call_tool("run_workflow", run_args))
                        except ToolError as exc:
                            result = str(exc)
                        else:
                            raise AssertionError(
                                "missing batch returned success instead of ToolError"
                            )
                        assert statuses == [404], statuses
                        assert "not found" in result, result
                else:
                    url = (
                        f"/api/workflows/{wid}/run-batch"
                        if surface == "api"
                        else f"/api/batches/{bid}/rerun"
                        if surface == "rerun-api"
                        else f"/api/batches/{bid}/retry-failed"
                    )
                    response = client.post(
                        url,
                        headers={"X-Loc-Token": key},
                        json={"doc_id": did, "locales": ["es-ES"], "wait": True},
                    )
                    assert response.status_code == 404 and "batch_missing" in response.text, (
                        response.status_code,
                        response.text,
                    )
                assert deleted, "fixture must delete between actual wait/status passes"
                for url in (f"/batches/{deleted[0]}", f"/batches/{deleted[0]}/status.json"):
                    response = client.get(url)
                    assert response.status_code == 404 and "batch_missing" in response.text
            except Exception as exc:
                failures.append((surface, repr(exc)))
    assert not failures, failures


def test_round7_completion_once_at_cli():
    from contextlib import contextmanager
    from unittest.mock import patch

    from app.services import batch_runs
    from tests._cli_runner import run_cli

    wid, did = _mk_wf("mock"), _mk_doc("es-ES", 1)
    with Session(engine) as s:
        s.add(TargetLocale(project_id=s.get(Document, did).project_id, locale="de-DE"))
        s.add(
            Segment(
                document_id=did,
                target_locale="de-DE",
                order_index=0,
                source_text="Sentence number 0.",
            )
        )
        external = WorkflowRun(
            workflow_id=wid, document_id=did, target_locale="de-DE", status=RunStatus.RUNNING
        )
        s.add(external)
        s.commit()
        external_id = external.id
    original = batch_runs._join_batch_members
    finalized = []

    def join_and_sweep(ids):
        joined = original(ids)
        # Force the server's sweep to win before the wait's next completion read.
        batch_runs.finalize_ready_batches()
        return joined

    def record_finalize(*args, **kwargs):
        finalized.append(args[0])
        return real_finalize(*args, **kwargs)

    real_finalize = batch_runs._finalize

    @contextmanager
    def server_daemon():
        scheduler = orchestrator._AdmissionScheduler()
        stop = threading.Event()
        real_drain = scheduler.drain_once

        def drain(*args, **kwargs):
            if stop.is_set():
                raise SystemExit()
            return real_drain(*args, **kwargs)

        with (
            patch_pieces("app.services.orchestrator", "ADMISSION", scheduler),
            patch.object(scheduler, "drain_once", drain),
        ):
            try:
                scheduler.ensure_started()
                yield
            finally:
                stop.set()
                scheduler.notify()
                # Seeded exception logging must finish before the death assertion.
                scheduler._thread.join(30)
                assert not scheduler.alive()

    with (
        server_daemon(),
        patch_pieces("app.services.batch_runs", "_join_batch_members", join_and_sweep),
        patch_pieces("app.services.batch_runs", "_finalize", record_finalize),
        patch.object(
            batch_runs.tap_envelope,
            "finalize_batch_action",
            wraps=batch_runs.tap_envelope.finalize_batch_action,
        ) as envelope,
    ):
        code, out, err = run_cli(["workflow", "run", str(did), str(wid), "--all-locales"])
        assert code == 0, (out, err)
        assert orchestrator.ADMISSION.alive(), "server admission daemon must be alive"
        with Session(engine) as s:
            batch = s.exec(
                select(batch_runs.WorkflowBatch).where(batch_runs.WorkflowBatch.document_id == did)
            ).one()
            bid, finished, notes = batch.id, batch.finished_at, batch.notes
            assert notes.count("Not launched: de-DE.") == 1, notes
            assert finished is not None
        # Round 12: _finalize is now safely idempotent -- the daemon sweep and the wait may
        # each CALL it, but the terminal transition is one conditional DB write, so the batch
        # is finalized exactly ONCE in effect: one finished_at stamp, one durable note (above)
        # and one envelope close. Assert the effect, not the call count (which no longer
        # needs a lock to be one, so it can be more than one under a real daemon race).
        assert finalized.count(bid) >= 1, finalized
        assert envelope.call_count == 1, envelope.call_args_list
        count = sum(call.args[0] == bid for call in envelope.call_args_list)
        assert count == 1
        real_finalize(bid, [], {})
        assert sum(call.args[0] == bid for call in envelope.call_args_list) == count
        with Session(engine) as s:
            batch = s.get(batch_runs.WorkflowBatch, bid)
            assert (batch.finished_at, batch.notes) == (finished, notes)
            orchestrator.cancel_run(s, external_id)


def test_round7_boundary_pool_choices():
    import json
    import traceback
    from contextlib import ExitStack, contextmanager
    from unittest.mock import patch

    from app.services import batch_runs
    from tests._cli_runner import run_cli

    key = _api_key()
    for saturated in (False, True):
        for complete in (False, True):
            for surface in ("cli", "api"):
                wid, did = _mk_wf("mock"), _mk_doc("es-ES", 1)
                scheduler = orchestrator._AdmissionScheduler()
                scheduler.ensure_started = lambda: False
                original_wait = batch_runs._wait_batch
                original_session = batch_runs._batch_session
                original_result = batch_runs._sync_batch_result
                bookkeeping = []
                binds = []
                checkout_errors = []
                original_get = engine.pool._do_get

                def checkout(original_get=original_get, checkout_errors=checkout_errors):
                    try:
                        return original_get()
                    except sa_exc.TimeoutError:
                        checkout_errors.append("".join(traceback.format_stack(limit=35)))
                        raise

                with ExitStack() as held:

                    def wait(
                        bid,
                        callback=None,
                        complete=complete,
                        saturated=saturated,
                        original_wait=original_wait,
                    ):
                        if complete:
                            with Session(engine) as s:
                                batch = s.get(batch_runs.WorkflowBatch, bid)
                                for row in batch_runs.batch_members(s, batch):
                                    row.status = RunStatus.COMPLETED
                                    s.add(row)
                                s.commit()
                        if saturated:
                            for _ in range(_pool_capacity()):
                                held.enter_context(engine.connect())
                        # Bookkeeping must land with BOTH budgets exhausted.
                        for _ in range(_db.RELIEF_MAX_SESSIONS):
                            held.enter_context(_db._emergency_engine_for_relief().connect())
                        return original_wait(bid, callback)

                    @contextmanager
                    def book_session(original_session=original_session, bookkeeping=bookkeeping):
                        with original_session() as s:
                            bookkeeping.append((s.bind, _db.pool_has_no_capacity()))
                            yield s

                    def result(s, bid, error, original_result=original_result, binds=binds):
                        original_bind = s.bind
                        try:
                            return original_result(s, bid, error)
                        finally:
                            binds.append(s.bind is original_bind)

                    with (
                        patch_pieces("app.services.orchestrator", "ADMISSION", scheduler),
                        patch_pieces("app.services.orchestrator", "QUEUE_DRAIN_WAIT_S", 0),
                        patch_pieces("app.services.batch_runs", "_wait_batch", wait),
                        patch_pieces("app.services.batch_runs", "_batch_session", book_session),
                        patch_pieces("app.services.batch_runs", "_sync_batch_result", result),
                        patch.object(engine.pool, "_timeout", 0.02),
                        patch.object(engine.pool, "_do_get", checkout),
                        TestClient(app, raise_server_exceptions=False) as client,
                    ):
                        if surface == "cli":
                            code, out, err = run_cli(
                                ["--json", "workflow", "run", str(did), str(wid), "--all-locales"]
                            )
                            assert code == (0 if complete else 1), (saturated, complete, out, err)
                            assert "Traceback" not in out + err, (saturated, complete, err)
                            view = json.loads(out)
                        else:
                            response = client.post(
                                f"/api/workflows/{wid}/run-batch",
                                headers={"X-Loc-Token": key},
                                json={"doc_id": did, "wait": True},
                            )
                            assert response.status_code == 200, (
                                saturated,
                                complete,
                                response.text,
                                checkout_errors,
                            )
                            view = response.json()
                    assert bookkeeping and all(
                        bind is (_db._ensure_emergency_engine() if full else engine)
                        for bind, full in bookkeeping
                    ), (saturated, complete, surface, bookkeeping)
                    assert binds and all(binds), "L1 restore caller binding on every result"
                    if complete:
                        assert (
                            view["status"] == "completed"
                            and view["finished_at"]
                            and view["error"] is None
                        )
                    else:
                        assert view["error"]["code"] == "drain_incomplete", view
                with Session(engine) as s:
                    batch = s.get(batch_runs.WorkflowBatch, view["id"])
                    for row in batch_runs.batch_members(s, batch):
                        if row.status == RunStatus.QUEUED:
                            orchestrator.cancel_run(s, row.id)


def test_round7_policy_notes_at_cli_and_api():
    import json
    from unittest.mock import patch

    from app.services import batch_runs
    from tests._cli_runner import run_cli

    key = _api_key()
    for reason in ("spend", "fail_fast"):
        for surface in ("cli", "api"):
            wid, did = _mk_wf("mock"), _mk_doc("es-ES", 1)
            with Session(engine) as s:
                s.add(TargetLocale(project_id=s.get(Document, did).project_id, locale="de-DE"))
                s.add(
                    Segment(
                        document_id=did,
                        target_locale="de-DE",
                        order_index=0,
                        source_text="Sentence number 0.",
                    )
                )
                s.commit()
            original = batch_runs._wait_batch

            def wait(bid, callback=None, reason=reason, original=original):
                if reason == "fail_fast":
                    with Session(engine) as s:
                        row = s.exec(
                            select(WorkflowRun)
                            .where(WorkflowRun.batch_id == bid)
                            .order_by(WorkflowRun.id)
                        ).first()
                        row.status = RunStatus.FAILED
                        s.add(row)
                        s.commit()
                with patch.object(
                    batch_runs.spend, "would_block", return_value=(reason == "spend", 1.0, 0.5)
                ):
                    return original(bid, callback)

            scheduler = orchestrator._AdmissionScheduler()
            scheduler.ensure_started = lambda: False
            with (
                patch_pieces("app.services.orchestrator", "ADMISSION", scheduler),
                patch_pieces("app.services.batch_runs", "_wait_batch", wait),
                patch_pieces("app.services.batch_runs", "_notes", wraps=batch_runs._notes) as notes,
                TestClient(app) as client,
            ):
                if surface == "cli":
                    code, out, err = run_cli(
                        [
                            "--json",
                            "workflow",
                            "run",
                            str(did),
                            str(wid),
                            "--all-locales",
                            "--fail-fast",
                        ]
                    )
                    assert code == 0 and "Traceback" not in out + err, (code, out, err)
                    view = json.loads(out)
                else:
                    response = client.post(
                        f"/api/workflows/{wid}/run-batch",
                        headers={"X-Loc-Token": key},
                        json={"doc_id": did, "wait": True, "fail_fast": True},
                    )
                    assert response.status_code == 200
                    view = response.json()
                assert any(call.args[3].get("reason") == reason for call in notes.call_args_list), (
                    "L5 policy note branch must be reached"
                )
                assert ("Spend cap reached" if reason == "spend" else "Fail-fast:") in view[
                    "notes"
                ], view
                assert view["finished_at"] and view["error"] is None
                response = client.get(f"/batches/{view['id']}")
                assert response.status_code == 200 and view["notes"] in response.text


def test_round7_missing_at_worker_and_response_boundaries():
    from contextlib import ExitStack

    from app.services import batch_runs
    from app.web import api_cli

    key = _api_key()
    for seam, mode in (
        ("response", "run"),
        ("response", "rerun"),
        ("response", "retry-failed"),
        ("worker", "rerun"),
        ("worker", "retry-failed"),
    ):
        bid, _ = _round6_batch(RunStatus.FAILED)
        batch_runs.finalize_ready_batches()
        with Session(engine) as s:
            batch = s.get(batch_runs.WorkflowBatch, bid)
            wid, did = batch.workflow_id, batch.document_id
        deleted = []

        def delete(current_id, deleted=deleted):
            with Session(engine) as s:
                batch = s.get(batch_runs.WorkflowBatch, current_id)
                for member in batch_runs.batch_members(s, batch):
                    # Detach terminal runs, retaining their step/envelope history.
                    member.batch_id = None
                    s.add(member)
                s.flush()
                s.delete(batch)
                s.commit()
                deleted.append(current_id)

        original_response = api_cli._batch_wait_response
        original_worker = api_cli.run_in_threadpool

        def response(current_id, delete=delete, original=original_response, **kwargs):
            delete(current_id)
            return original(current_id, **kwargs)

        async def worker(func, *args, delete=delete, original=original_worker, **kwargs):
            if func is api_cli._rerun_batch_blocking:
                delete(args[0])
            return await original(func, *args, **kwargs)

        scheduler = orchestrator._AdmissionScheduler()
        scheduler.ensure_started = lambda: False
        with ExitStack() as stack:
            stack.enter_context(patch_pieces("app.services.orchestrator", "ADMISSION", scheduler))
            stack.enter_context(
                patch_api_cli("_batch_wait_response", response)
                if seam == "response"
                else patch_api_cli("run_in_threadpool", worker)
            )
            client = stack.enter_context(TestClient(app, raise_server_exceptions=False))
            url = (
                f"/api/workflows/{wid}/run-batch" if mode == "run" else f"/api/batches/{bid}/{mode}"
            )
            out = client.post(
                url,
                headers={"X-Loc-Token": key},
                json={"doc_id": did, "locales": ["es-ES"], "wait": True},
            )
            assert deleted and out.status_code == 404 and "batch_missing" in out.text, (
                seam,
                mode,
                out.status_code,
                out.text,
            )


def test_round8_bookkeeping_and_request_accounting():
    import ast
    import importlib
    import inspect
    from concurrent.futures import ThreadPoolExecutor
    from contextlib import ExitStack
    from pathlib import Path
    from unittest.mock import patch

    from starlette.requests import Request

    from app.models import AuditLog
    from app.services import batch_runs

    main_module = importlib.import_module("app.main")
    assert "_finish_wait_batch(" not in inspect.getsource(batch_runs._sync_wait_error)

    callers = []
    for path in Path("app").rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for fn in ast.walk(tree):
            if isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                for node in ast.walk(fn):
                    if (
                        isinstance(node, ast.Call)
                        and isinstance(node.func, ast.Attribute)
                        and node.func.attr == "begin_relief_scope"
                    ):
                        callers.append((path.as_posix(), fn.name))
    assert callers == [("app/main.py", "__call__")], callers

    bid, rid = _round6_batch(RunStatus.QUEUED)
    assert batch_runs._finalize(bid, [], {}) == batch_runs.FINALIZE_NOT_READY
    with Session(engine) as session:
        assert session.get(batch_runs.WorkflowBatch, bid).finished_at is None
        row = session.get(WorkflowRun, rid)
        row.status = RunStatus.COMPLETED
        session.add(row)
        session.commit()
    batch_runs._finalize(bid, [], {})
    with Session(engine) as session:
        assert session.get(batch_runs.WorkflowBatch, bid).finished_at is not None

    for saturated in (False, True):
        bids = [_round6_batch(RunStatus.COMPLETED)[0] for _ in range(2)]
        with Session(engine) as s:
            for bid in bids:
                row = s.get(batch_runs.WorkflowBatch, bid)
                row.notes = "Dispatch marker. Not launched: es-ES."
                s.add(row)
            s.commit()
        lock_checks = []
        from sqlalchemy import event

        checkout_locks = []

        def checkout(*args, checkout_locks=checkout_locks):
            checkout_locks.append(
                "".join(__import__("traceback").format_stack())
                if batch_runs._BATCH_STATE_LOCK._is_owned()
                else True
            )

        original_finalize = batch_runs.tap_envelope.finalize_batch_action

        def envelope(*args, lock_checks=lock_checks, original_finalize=original_finalize, **kwargs):
            lock_checks.append(not batch_runs._BATCH_STATE_LOCK._is_owned())
            return original_finalize(*args, **kwargs)

        with ExitStack() as held:
            if saturated:
                for _ in range(_pool_capacity()):
                    held.enter_context(engine.connect())
            for _ in range(_db.RELIEF_MAX_SESSIONS):
                held.enter_context(_db._emergency_engine_for_relief().connect())
            if saturated:
                for _ in range(_db.RELIEF_MAX_SESSIONS):
                    assert main_module._relief_slots.acquire(blocking=False)
                    held.callback(main_module._relief_slots.release)
                response = TestClient(app).get("/settings")
                assert response.status_code == 503, response.text
            main_module._audit_mutating_request(
                Request(
                    {
                        "type": "http",
                        "method": "POST",
                        "path": f"/api/round8-audit-{saturated}",
                        "headers": [],
                        "client": ("127.0.0.1", 1234),
                        "session": {},
                    }
                )
            )
            with batch_runs._batch_session() as audit_session:
                assert (
                    audit_session.exec(
                        select(AuditLog).where(
                            AuditLog.action == f"POST /api/round8-audit-{saturated}"
                        )
                    ).first()
                    is not None
                )
            selected_engine = _db._ensure_emergency_engine() if saturated else engine
            event.listen(selected_engine, "checkout", checkout)
            held.callback(event.remove, selected_engine, "checkout", checkout)
            with (
                patch.object(batch_runs.tap_envelope, "finalize_batch_action", envelope),
                patch.object(
                    _db, "_emergency_engine_for_relief", wraps=_db._emergency_engine_for_relief
                ) as relief_engine,
                ThreadPoolExecutor(max_workers=2) as workers,
            ):
                assert list(workers.map(batch_runs._wait_batch, bids)) == [None, None]
                assert not relief_engine.called, "bookkeeping attempted request relief"
            assert _db._relief_connections._value == 0
            try:
                with _db._emergency_engine_for_relief().connect():
                    raise AssertionError("spent relief was admitted")
            except sa_exc.TimeoutError:
                pass
            with batch_runs._batch_session() as s:
                for bid in bids:
                    row = s.get(batch_runs.WorkflowBatch, bid)
                    assert row.status == "completed" and row.finished_at
                    assert row.notes == "Dispatch marker.", row.notes
            assert lock_checks == [True, True], lock_checks
            assert checkout_locks and all(x is True for x in checkout_locks), checkout_locks
        # Missing coverage remains visible in the opposite arm.
        assert batch_runs._notes(["de-DE"], {}, [], {}) == "Not launched: de-DE."


def test_round8_two_concurrent_cli_batches():
    import io
    from concurrent.futures import ThreadPoolExecutor
    from contextlib import ExitStack, redirect_stderr, redirect_stdout

    from app.cli.__main__ import main as cli_main
    from app.services import batch_runs

    for saturated in (False, True):
        fixtures = [(_mk_wf("mock"), _mk_doc("es-ES", 1)) for _ in range(2)]
        bids = []
        original_wait = batch_runs._wait_batch
        with ExitStack() as held:

            def saturate(saturated=saturated, held=held):
                if saturated:
                    for _ in range(_pool_capacity()):
                        held.enter_context(engine.connect())
                for _ in range(_db.RELIEF_MAX_SESSIONS):
                    held.enter_context(_db._emergency_engine_for_relief().connect())

            barrier = threading.Barrier(2, action=saturate, timeout=15)

            def wait(bid, callback=None, bids=bids, barrier=barrier, original_wait=original_wait):
                with Session(engine) as session:
                    batch = session.get(batch_runs.WorkflowBatch, bid)
                    batch.notes = "Dispatch marker. Not launched: es-ES."
                    session.add(batch)
                    for row in batch_runs.batch_members(session, batch):
                        row.status = RunStatus.COMPLETED
                        session.add(row)
                    session.commit()
                bids.append(bid)
                barrier.wait()
                return original_wait(bid, callback)

            def run(fixture):
                wid, did = fixture
                return cli_main(["--json", "workflow", "run", str(did), str(wid), "--all-locales"])

            scheduler = orchestrator._AdmissionScheduler()
            scheduler.ensure_started = lambda: False
            out, err = io.StringIO(), io.StringIO()
            with (
                patch_pieces("app.services.orchestrator", "ADMISSION", scheduler),
                patch_pieces("app.services.batch_runs", "_wait_batch", wait),
                redirect_stdout(out),
                redirect_stderr(err),
                ThreadPoolExecutor(max_workers=2) as workers,
            ):
                codes = list(workers.map(run, fixtures))
            assert codes == [0, 0], (codes, out.getvalue(), err.getvalue())
            assert "Traceback" not in out.getvalue() + err.getvalue()
            assert len(bids) == 2
            with batch_runs._batch_session() as session:
                for bid in bids:
                    batch = session.get(batch_runs.WorkflowBatch, bid)
                    assert batch.status == "completed" and batch.finished_at
                    assert batch.notes == "Dispatch marker.", batch.notes


def test_round9_no_checkout_under_batch_locks():
    """Round 12 instrumentation ratchet (HIGH-1 / MED-1). A process-wide checkout listener
    on EVERY engine records any pooled checkout or session open taken by a thread that owns
    _BATCH_STATE_LOCK or _BATCH_FINISH_LOCK, on BOTH the finalize path AND the dispatch path
    (start_batch -> _start_batch_dispatch). The lock discipline is: locks guard in-memory
    state only, so this count must be zero. A positive control proves the listener can see a
    checkout under the lock, so a green result is meaningful."""
    import traceback
    from unittest.mock import patch

    from sqlalchemy import event

    from app.services import batch_runs

    engines = [engine, _db._ensure_emergency_engine(), _db._emergency_engine_for_relief()]

    def _held():
        held = []
        if batch_runs._BATCH_STATE_LOCK._is_owned():
            held.append("_BATCH_STATE_LOCK")
        if batch_runs._BATCH_FINISH_LOCK._is_owned():
            held.append("_BATCH_FINISH_LOCK")
        return held

    # Positive control: a checkout taken while _BATCH_STATE_LOCK is held IS observed.
    control = []

    def control_listener(*a, **k):
        if batch_runs._BATCH_STATE_LOCK._is_owned():
            control.append(True)

    event.listen(engine, "checkout", control_listener)
    try:
        with batch_runs._BATCH_STATE_LOCK, Session(engine) as s:
            s.connection()
    finally:
        event.remove(engine, "checkout", control_listener)
    assert control, "probe cannot see a checkout under the lock"

    violations = []

    def listener(*a, **k):
        held = _held()
        if held:
            violations.append((held, "".join(traceback.format_stack())))

    # Gate round 10 LOW-2: the ratchet must also count SESSION OPENS under a lock, not only
    # pooled checkouts -- a future change that CONSTRUCTS a Session under a lock and uses it
    # only after releasing would slip past a checkout-only listener (a Session opens lazily,
    # so no checkout fires at construction). Instrument Session.__init__ with its own positive
    # control.
    import sqlmodel

    _orig_session_init = sqlmodel.Session.__init__
    session_control = []

    def _control_init(self, *a, control=session_control, **k):
        if _held():
            control.append(True)
        return _orig_session_init(self, *a, **k)

    with (
        patch.object(sqlmodel.Session, "__init__", _control_init),
        batch_runs._BATCH_STATE_LOCK,
    ):
        Session(engine)
    assert session_control, "probe cannot see a Session open under the lock"

    def _session_open_listener(self, *a, **k):
        held = _held()
        if held:
            violations.append((held, "Session.__init__\n" + "".join(traceback.format_stack())))
        return _orig_session_init(self, *a, **k)

    scheduler = orchestrator._AdmissionScheduler()
    scheduler.ensure_started = lambda: False
    for eng in engines:
        event.listen(eng, "checkout", listener)
    try:
        with (
            patch.object(sqlmodel.Session, "__init__", _session_open_listener),
            patch_pieces("app.services.orchestrator", "ADMISSION", scheduler),
        ):
            # DISPATCH path: a real start_batch (async) flushes, marks the batch in-flight,
            # commits, opens its envelope and dispatches its members -- none under a lock.
            wid, did = _mk_wf("mock"), _mk_doc("es-ES", 1)
            with Session(engine) as s:
                batch_runs.start_batch(
                    s, workflow_id=wid, doc_id=did, locales=["es-ES"], created_by="probe"
                )
            # FINALIZE path: a ready batch, a direct _finalize, and the daemon's sweep.
            bid, _ = _round6_batch(RunStatus.COMPLETED)
            assert batch_runs._finalize(bid, [], {}) == batch_runs.FINALIZE_DONE
            _round6_batch(RunStatus.COMPLETED)
            batch_runs.finalize_ready_batches()
            # And the synchronous completion path (join + finalize), also lock-free.
            bid3, _ = _round6_batch(RunStatus.COMPLETED)
            assert batch_runs._finish_wait_batch(bid3) is None
    finally:
        for eng in engines:
            event.remove(eng, "checkout", listener)
    assert not violations, (len(violations), violations[0][0], violations[0][1])


def test_round9_finalize_contention_keeps_pool_and_liveness():
    """Round 12 HIGH-1. Many bookkeeping threads contending on the finalize lock must NOT
    each pin a pooled connection while they queue: lock contention must not become pool
    exhaustion (the 2026-09-06 outage class). With _BATCH_FINISH_LOCK held, 60 _finalize
    threads block on the in-memory claim holding NO connection, so pooled checkouts stay at
    or under the pool ceiling minus the relief reserve, the pool never reads as full, an
    ordinary /api request stays 200, and an unrelated dispatch (which uses the OTHER lock)
    completes in well under three seconds.

    The "ordinary /api request" probe is GET /api/projects, NOT the /api/runs the round-12
    ruling named (gate round 10 LOW-3): there is no COLLECTION route GET /api/runs -- only
    GET /api/runs/{run_id} (app/web/api_cli.py) -- so /api/runs returns 404 in milliseconds
    under this contention and would make a 200 assertion vacuous. /api/projects is a real
    authenticated collection read that exercises the same pooled-checkout path the ruling
    meant to protect."""
    from concurrent.futures import ThreadPoolExecutor

    from app.services import batch_runs

    key = _api_key()
    n = 60
    bids = [_round6_batch(RunStatus.COMPLETED)[0] for _ in range(n)]
    reached = threading.Barrier(n + 1, timeout=30)

    def finalize(bid):
        reached.wait()
        batch_runs._finalize(bid, [], {})

    scheduler = orchestrator._AdmissionScheduler()
    scheduler.ensure_started = lambda: False
    peak = [0]
    with (
        patch_pieces("app.services.orchestrator", "ADMISSION", scheduler),
        TestClient(app) as client,
        ThreadPoolExecutor(max_workers=n) as workers,
    ):
        # Hold the finalize lock so every _finalize thread blocks on the in-memory claim,
        # NOT on a connection. On the pre-fix ordering (checkout before the lock) each thread
        # would instead be holding a pooled connection here and drain the pool.
        batch_runs._BATCH_FINISH_LOCK.acquire()
        try:
            futures = [workers.submit(finalize, bid) for bid in bids]
            reached.wait()
            deadline = time.monotonic() + 1.5
            while time.monotonic() < deadline:
                peak[0] = max(peak[0], engine.pool.checkedout())
                time.sleep(0.02)
            checkedout = engine.pool.checkedout()
            no_capacity = _db.pool_has_no_capacity()
            runs_status = client.get("/api/projects", headers={"X-Loc-Token": key}).status_code
            wid, did = _mk_wf("mock"), _mk_doc("es-ES", 1)
            t0 = time.monotonic()
            with Session(engine) as s:
                batch_runs.start_batch(
                    s, workflow_id=wid, doc_id=did, locales=["es-ES"], created_by="probe"
                )
            dispatch_s = time.monotonic() - t0
        finally:
            batch_runs._BATCH_FINISH_LOCK.release()
        for f in futures:
            f.result(timeout=30)
    reserve = _db.RELIEF_MAX_SESSIONS
    assert peak[0] <= _pool_capacity() - reserve, (peak[0], _pool_capacity(), reserve)
    assert checkedout <= _pool_capacity() - reserve, checkedout
    assert not no_capacity, "lock contention must not exhaust the pool"
    assert runs_status == 200, runs_status
    assert dispatch_s < 3.0, dispatch_s
    with batch_runs._batch_session() as s:
        for bid in bids:
            row = s.get(batch_runs.WorkflowBatch, bid)
            assert row.finished_at is not None and row.status == "completed"


def test_round9_drain_pass_not_blocked_by_sync_wait():
    """Round 12 MED-2. A synchronous wait finishing (joining a slow member thread, then
    finalizing) must not hold _BATCH_FINISH_LOCK across that join, or it stalls the admission
    daemon -- drain_once() finalizes ready batches as its first act. With the join and the
    finalize lock-free, drain_once() completes a full pass in well under a second while the
    waiter is still joining a ~2s member."""
    from unittest.mock import patch

    from app.services import batch_runs

    bid, rid = _round6_batch(RunStatus.COMPLETED)
    started = threading.Event()
    release = threading.Event()

    def slow_member():
        started.set()
        release.wait(10)

    member_thread = threading.Thread(target=slow_member, daemon=True)
    with batch_runs._MEMBER_THREADS_LOCK:
        batch_runs._MEMBER_THREADS[rid] = member_thread
    member_thread.start()
    started.wait(5)

    waiter_done = threading.Event()

    def waiter():
        try:
            batch_runs._finish_wait_batch(bid)
        finally:
            waiter_done.set()

    w = threading.Thread(target=waiter, daemon=True)
    w.start()
    time.sleep(0.3)  # let the waiter enter the member join

    scheduler = orchestrator._AdmissionScheduler()
    scheduler._batches_dirty.set()
    try:
        with patch.object(orchestrator.RUN_SLOTS, "acquire_for_queue", return_value=None):
            t0 = time.monotonic()
            scheduler.drain_once()
            drain_s = time.monotonic() - t0
        assert drain_s < 1.0, drain_s
        assert not waiter_done.is_set(), "waiter should still be joining the slow member"
    finally:
        release.set()
        w.join(10)
        member_thread.join(10)


def _round10_batch_with_action():
    """A ready (all-COMPLETED) batch plus its eagerly-created batch TapAction, still RUNNING
    with zero attached TapRuns -- the zero-leg (M1) shape whose close must reach a terminal
    status. Returns (batch_id, action_id)."""
    from app.models import TapAction, TapStatus
    from app.services import batch_runs, tap_envelope

    bid, _ = _round6_batch(RunStatus.COMPLETED)
    with Session(engine) as s:
        did = s.get(batch_runs.WorkflowBatch, bid).document_id
    aid = tap_envelope.begin_batch_action(
        batch_id=bid,
        requested_locales=["es-ES"],
        initiator="round10",
        surface="batch",
        document_id=did,
    )
    assert aid is not None, "envelopes must be enabled for this fixture"
    with Session(engine) as s:
        assert s.get(TapAction, aid).status == TapStatus.RUNNING
    return bid, aid


def test_round10_envelope_close_reaches_terminal_under_saturation():
    """Gate round 10 HIGH-1. The run-envelope close runs INSIDE bookkeeping.scope(), so on a
    SATURATED pool its helper Sessions reach the UNBOUNDED engine and close the batch
    TapAction to a terminal status -- instead of burning a pool timeout, being swallowed by
    the best-effort guard, and leaving the action 'running' forever while the admission
    daemon's drain pass stalls one pool-timeout per batch.

    Both arms of every touched path (the brief's round-13 rule): the single-batch close and
    the three-batch daemon drain pass are each run on a HEALTHY pool and a FULL pool; the
    action reaches a terminal state in every case, and the saturated drain pass of three
    ready batches stays inside the MED-2 one-second budget."""
    from contextlib import ExitStack

    from app.models import TapAction, TapStatus
    from app.services import batch_runs

    # Single-batch close: terminal on either pool.
    for saturated in (False, True):
        bid, aid = _round10_batch_with_action()
        with ExitStack() as held:
            if saturated:
                for _ in range(_pool_capacity()):
                    held.enter_context(engine.connect())
                assert _db.pool_has_no_capacity(), "arm did not actually saturate the pool"
            assert batch_runs._finalize(bid, [], {}) == batch_runs.FINALIZE_DONE
        with Session(engine) as s:
            status = s.get(TapAction, aid).status
        assert status in TapStatus.TERMINAL and status != TapStatus.RUNNING, (saturated, status)

    # Three-batch daemon drain pass, both arms; the saturated pass must stay under 1 s.
    for saturated in (False, True):
        batch_runs.finalize_ready_batches()  # drain any leftovers so the budget is honest
        triples = [_round10_batch_with_action() for _ in range(3)]
        scheduler = orchestrator._AdmissionScheduler()
        scheduler.ensure_started = lambda: False
        with ExitStack() as held, patch_pieces("app.services.orchestrator", "ADMISSION", scheduler):
            if saturated:
                for _ in range(_pool_capacity()):
                    held.enter_context(engine.connect())
                assert _db.pool_has_no_capacity(), "arm did not actually saturate the pool"
            t0 = time.time()
            batch_runs.finalize_ready_batches()
            elapsed = time.time() - t0
        if saturated:
            assert elapsed < 1.0, ("saturated drain pass exceeded the MED-2 budget", elapsed)
        with Session(engine) as s:
            for _bid, aid in triples:
                status = s.get(TapAction, aid).status
                assert status in TapStatus.TERMINAL and status != TapStatus.RUNNING, (
                    saturated,
                    aid,
                    status,
                )


def test_round10_sync_wait_waits_for_owning_finalizer():
    """Gate round 10 HIGH-2. A synchronous waiter must not collapse 'another thread owns the
    terminal transition' into 'not ready'. When a concurrent finalizer owns an all-COMPLETED
    batch, the waiter WAITS for its completion signal and returns None (the completion-first
    property), never `drain_incomplete`.

    Both arms: an uncontended all-COMPLETED batch returns None; a batch whose owner holds the
    claim through a SLOW phase A (longer than the old 3 x 0.05 s give-up budget) also returns
    None. On the pre-fix code the owned case gave up at ~0.15 s and returned drain_incomplete
    before the owner released at 0.3 s -- reproduced by reverting this fix."""

    from app.services import batch_runs

    # Healthy arm: no contention -> the waiter finalizes and returns None.
    bid_h, _ = _round6_batch(RunStatus.COMPLETED)
    assert batch_runs._finish_wait_batch(bid_h) is None
    with Session(engine) as s:
        assert s.get(batch_runs.WorkflowBatch, bid_h).finished_at is not None

    # Contended arm: an owning finalizer holds the claim through a slow phase A.
    bid, _ = _round6_batch(RunStatus.COMPLETED)
    release_owner = threading.Event()
    real_rollup = batch_runs._rollup_status

    def slow_rollup(*a, **k):
        release_owner.wait(10)  # hold the claim (phase A) until the main thread releases us
        return real_rollup(*a, **k)

    owner_done = threading.Event()

    def owner():
        try:
            batch_runs._finalize(bid, [], {})
        finally:
            owner_done.set()

    waiter_result = []

    def waiter():
        waiter_result.append(batch_runs._finish_wait_batch(bid))

    with patch_pieces("app.services.batch_runs", "_rollup_status", slow_rollup):
        owner_t = threading.Thread(target=owner, name="round10-owner")
        owner_t.start()
        deadline = time.time() + 5
        while bid not in batch_runs._FINALIZING and time.time() < deadline:
            time.sleep(0.005)
        assert bid in batch_runs._FINALIZING, "the owner never claimed the batch"
        waiter_t = threading.Thread(target=waiter, name="round10-waiter")
        waiter_t.start()
        # The buggy waiter gives up its 3 x 0.05 s budget (~0.15 s) HERE and returns
        # drain_incomplete before we release the owner; the fixed waiter parks on the owner's
        # completion Event and outlasts this window.
        time.sleep(0.3)
        release_owner.set()
        waiter_t.join(timeout=30)
        owner_t.join(timeout=30)
    assert owner_done.is_set(), "owner did not finish"
    assert waiter_result == [None], ("owned-by-other must not report incomplete", waiter_result)
    with Session(engine) as s:
        assert s.get(batch_runs.WorkflowBatch, bid).finished_at is not None


@contextmanager
def _hermetic_admission():
    """Stub the shared admission scheduler for a test that calls the REAL start_batch (gate
    round 11 MED-1). Without this the async launch runs orchestrator.ADMISSION.ensure_started(),
    which starts the loc-run-admission daemon; the daemon then launches loc-batch-member-*
    threads and, for the rest of the PROCESS, takes a pooled checkout on every poll. Both leak
    past the test and perturb test_round5_wait_checkout_budget_and_cursor, which counts
    process-wide engine checkouts. ensure_started is made a no-op (the pattern the neighbouring
    tests use at test_round9_no_checkout_under_batch_locks); on exit any member thread that did
    start is joined so nothing outlives the test."""

    from app.services import batch_runs

    scheduler = orchestrator._AdmissionScheduler()
    scheduler.ensure_started = lambda: False
    with patch_pieces("app.services.orchestrator", "ADMISSION", scheduler):
        try:
            yield scheduler
        finally:
            with batch_runs._MEMBER_THREADS_LOCK:
                threads = [t for t in batch_runs._MEMBER_THREADS.values() if t.is_alive()]
            for t in threads:
                t.join(timeout=30)


def test_round10_batch_id_stable_across_launch_retry():
    """Gate round 10 MED-1. The follow-up launch commit uses attempts=1, so a lost writer lock
    RAISES (WriteContentionError) instead of silently rolling back the flushed INSERT and
    re-INSERTing the batch under a NEW PK -- which would strand every downstream write
    (members, TapAction, audit, the in-flight mark) on the id captured BEFORE the commit and
    leave an orphan batch frozen 'running' (the #99 outcome class).

    Both arms: a healthy launch commits exactly one batch with its member attached; a launch
    whose follow-up commit loses the lock once raises and leaves NO orphan batch. A rowid hole
    is punched first so a re-INSERT could not reuse the captured id (the gate's rowid-reuse
    mask). Reverting attempts=1 makes the retry succeed silently, so the launch does NOT raise
    and an orphan batch appears -- both asserted-against here."""
    from unittest.mock import patch

    from app.services import batch_runs

    # Punch a hole so a rolled-back rowid cannot be silently reused by a re-INSERT.
    filler, _ = _round6_batch(RunStatus.COMPLETED)
    with Session(engine) as s:
        for row in s.exec(select(WorkflowRun).where(WorkflowRun.batch_id == filler)).all():
            s.delete(row)
        s.delete(s.get(batch_runs.WorkflowBatch, filler))
        s.commit()

    # Healthy arm: an ordinary launch commits one batch with its member.
    wid, did = _mk_wf("mock"), _mk_doc("es-ES", 1)
    with _hermetic_admission(), Session(engine) as s:
        batch_runs.start_batch(
            s, workflow_id=wid, doc_id=did, locales=["es-ES"], created_by="probe"
        )
    with Session(engine) as s:
        healthy = s.exec(
            select(batch_runs.WorkflowBatch).where(batch_runs.WorkflowBatch.document_id == did)
        ).all()
        assert len(healthy) == 1, [b.id for b in healthy]
        assert s.exec(select(WorkflowRun).where(WorkflowRun.batch_id == healthy[0].id)).first()

    # Contended arm: force the FOLLOW-UP launch commit -- db.commit_launch, the ONE write that
    # carries attempts=1 -- to lose the writer lock once, while letting the REAL commit_launch
    # run so attempts=1 vs the default is what decides the outcome. Target commit_launch itself
    # rather than "the first commit_with_retry": the launch path takes several commit_with_retry
    # calls (member dispatch etc.), and an EARLIER one with the default budget would absorb the
    # injected lock on its own retry, so a first-call hook never reaches the launch commit.
    wid2, did2 = _mk_wf("mock"), _mk_doc("es-ES", 1)
    real_commit_launch = _db.commit_launch

    def flaky_commit_launch(session, **kw):
        orig_commit = session.commit
        state = {"failed": False}

        def one_locked():
            if not state["failed"]:
                state["failed"] = True
                raise sa_exc.OperationalError("stmt", {}, Exception("database is locked"))
            return orig_commit()

        with patch.object(session, "commit", one_locked):
            return real_commit_launch(session, **kw)

    raised = False
    with _hermetic_admission(), patch.object(_db, "commit_launch", flaky_commit_launch):
        try:
            with Session(engine) as s:
                batch_runs.start_batch(
                    s, workflow_id=wid2, doc_id=did2, locales=["es-ES"], created_by="probe"
                )
        except _db.WriteContentionError:
            raised = True
    assert raised, "attempts=1 must surface the lost lock, not silently re-INSERT under a new id"
    with Session(engine) as s:
        orphans = s.exec(
            select(batch_runs.WorkflowBatch).where(batch_runs.WorkflowBatch.document_id == did2)
        ).all()
        assert orphans == [], (
            "a raised launch must leave no committed batch",
            [b.id for b in orphans],
        )
        for m in s.exec(select(WorkflowRun).where(WorkflowRun.document_id == did2)).all():
            assert s.get(batch_runs.WorkflowBatch, m.batch_id) is not None, (
                "a member points at a batch id that no row carries",
                m.batch_id,
            )


def test_round10_low_fixes():
    """Gate round 10 LOWs. LOW-1: orchestrator._bookkeeping_session is annotated and behaves as
    a CONTEXT MANAGER, not a bare Session. LOW-4: _DISPATCH_INFLIGHT is a refcount that
    balances back to empty after a dispatch and whose membership still reads true while a
    window is open. LOW-5: a finalize still writes its coverage rollup note (now derived in
    the write phase)."""
    import inspect
    from collections import Counter

    from app.services import batch_runs

    # LOW-1: a context manager yielding a Session, and the annotation says so.
    ann = inspect.signature(orchestrator._bookkeeping_session).return_annotation
    assert "AbstractContextManager" in str(ann), ann
    cm = orchestrator._bookkeeping_session()
    assert hasattr(cm, "__enter__") and hasattr(cm, "__exit__"), type(cm)
    with cm as s:
        assert isinstance(s, Session)

    # LOW-4: the in-flight guard is a refcount that clears to empty after a normal dispatch,
    # and `id in _DISPATCH_INFLIGHT` reads true only while a window is open (deleted at zero).
    assert isinstance(batch_runs._DISPATCH_INFLIGHT, Counter)
    marker = -987654321  # an id no batch will ever carry
    with batch_runs._BATCH_STATE_LOCK:
        batch_runs._DISPATCH_INFLIGHT[marker] += 1
        batch_runs._DISPATCH_INFLIGHT[marker] += 1
        assert marker in batch_runs._DISPATCH_INFLIGHT
        batch_runs._release_dispatch_inflight(marker)
        assert marker in batch_runs._DISPATCH_INFLIGHT, "still open after one of two releases"
        batch_runs._release_dispatch_inflight(marker)
        assert marker not in batch_runs._DISPATCH_INFLIGHT, "cleared to empty at zero"

    wid, did = _mk_wf("mock"), _mk_doc("es-ES", 1)
    with _hermetic_admission(), Session(engine) as s:
        batch_runs.start_batch(
            s, workflow_id=wid, doc_id=did, locales=["es-ES"], created_by="probe"
        )
    with batch_runs._BATCH_STATE_LOCK:
        assert not batch_runs._DISPATCH_INFLIGHT, dict(batch_runs._DISPATCH_INFLIGHT)

    # LOW-5: the coverage rollup note is written by the finalize (derived in the write phase).
    bid, _ = _round6_batch(RunStatus.COMPLETED)
    with Session(engine) as s:
        row = s.get(batch_runs.WorkflowBatch, bid)
        row.notes = "Dispatch marker. Not launched: es-ES."
        s.add(row)
        s.commit()
    assert batch_runs._finalize(bid, [], {}) == batch_runs.FINALIZE_DONE
    with Session(engine) as s:
        assert s.get(batch_runs.WorkflowBatch, bid).notes == "Dispatch marker."


def test_round14_bounded_wait_times_out_on_a_wedged_owner():
    """Gate round 11 LOW-2. The opposite-condition arm of HIGH-2's bounded owner wait. Every
    other test and probe exercises the arm where the owning finalizer DOES land its terminal
    stamp; this is the arm the bound exists for -- the owner NEVER lands (its completion Event
    is never set). The synchronous waiter must then NOT hang: it waits _SYNC_OWNER_WAIT_S per
    attempt, then returns the NAMED drain_incomplete error carrying DRAIN_TIMEOUT_NOTE, not None.

    The owner is wedged in phase A (a blocked _rollup_status), which claims the batch in
    _FINALIZING and creates its completion Event but never sets it, so a real FINALIZE_OWNED
    waiter parks on that unset Event. _SYNC_OWNER_WAIT_S is patched to 0.2 s so the three
    attempts take ~0.6 s; the waiter runs in its own thread joined with a 30 s timeout, so a
    REGRESSED unbounded wait fails this test (waiter_result stays empty) instead of hanging the
    suite. The owner is released only after the waiter has given up, then joined -- nothing is
    left running."""

    from app.services import batch_runs

    bid, _ = _round6_batch(RunStatus.COMPLETED)
    release_owner = threading.Event()
    real_rollup = batch_runs._rollup_status

    def wedged_rollup(*a, **k):
        # Hold the claim (phase A) so the owner never lands the terminal stamp while the waiter
        # runs; released only after the waiter returns so the owner thread can be joined.
        release_owner.wait(30)
        return real_rollup(*a, **k)

    owner_done = threading.Event()

    def owner():
        try:
            batch_runs._finalize(bid, [], {})
        finally:
            owner_done.set()

    waiter_result = []

    def waiter():
        waiter_result.append(batch_runs._finish_wait_batch(bid))

    with (
        patch_pieces("app.services.batch_runs", "_rollup_status", wedged_rollup),
        patch_pieces("app.services.batch_runs", "_SYNC_OWNER_WAIT_S", 0.2),
    ):
        owner_t = threading.Thread(target=owner, name="round14-wedged-owner")
        owner_t.start()
        deadline = time.time() + 5
        while bid not in batch_runs._FINALIZING and time.time() < deadline:
            time.sleep(0.005)
        assert bid in batch_runs._FINALIZING, "the owner never claimed the batch"
        t0 = time.monotonic()
        waiter_t = threading.Thread(target=waiter, name="round14-waiter")
        waiter_t.start()
        waiter_t.join(timeout=30)
        elapsed = time.monotonic() - t0
        assert waiter_result, "the bounded wait hung: the waiter never returned"
        assert bid in batch_runs._FINALIZING, (
            "the owner must still be wedged when the waiter gives up",
        )
        release_owner.set()
        owner_t.join(timeout=30)
    assert owner_done.is_set(), "owner did not finish after release"
    error = waiter_result[0]
    assert error is not None and error["code"] == "drain_incomplete", (
        "a wedged owner must yield the NAMED drain_incomplete, not a clean result",
        error,
    )
    assert error["reason"] == batch_runs.DRAIN_TIMEOUT_NOTE, error
    # It parked on the owner's completion Event across its attempts (the OWNED arm), not the
    # ~0.15 s not-ready budget (3 x 0.05 s): one 0.2 s bound already exceeds that.
    assert elapsed >= 0.2, ("the waiter did not wait on the owner's completion Event", elapsed)


def main():
    test_round14_bounded_wait_times_out_on_a_wedged_owner()
    test_round10_envelope_close_reaches_terminal_under_saturation()
    test_round10_sync_wait_waits_for_owning_finalizer()
    test_round10_batch_id_stable_across_launch_retry()
    test_round10_low_fixes()
    test_round9_no_checkout_under_batch_locks()
    test_round9_finalize_contention_keeps_pool_and_liveness()
    test_round9_drain_pass_not_blocked_by_sync_wait()
    test_round5_wait_checkout_budget_and_cursor()
    test_round8_bookkeeping_and_request_accounting()
    test_round8_two_concurrent_cli_batches()
    test_round7_missing_at_worker_and_response_boundaries()
    test_round7_boundary_pool_choices()
    test_round7_policy_notes_at_cli_and_api()
    test_round7_missing_batch_boundaries()
    test_round7_completion_once_at_cli()
    test_round6_non_read_exception_mapping()
    test_round6_wait_note_lifecycle_pairs()
    test_round6_saturated_read_exception_pairs()
    test_round6_failed_read_joins_before_completion_verdict()
    test_round6_sync_result_uses_bookkeeping_not_relief()
    test_round6_optional_tools_skip_cleanly()
    test_round6_non_http_bypass_preserves_shedding()
    test_round6_incomplete_capacity_is_only_a_read_choice()
    test_round6_completion_before_capacity_and_failure()
    test_round6_wait_exception_pairs()
    test_round6_reads_use_only_caller_session()
    test_round6_missing_and_unowned_wait_bound()
    test_round5_retry_clears_previous_wait_error()
    test_round5_wait_note_survives_write_retry()
    test_round5_sync_cursor_is_private()
    test_round5_sync_public_saturated_result()
    test_round5_queued_page_poll_behavior()
    test_round5_bookkeeping_survives_relief_exhaustion()
    test_round5_wait_pool_failure_is_named()
    test_round5_cli_pool_error_no_traceback()
    test_round5_health_preserves_shed_episode()
    test_round5_keys_own_group_and_reattachment_wake()
    test_round5_bounded_pills_and_queued_refresh()
    test_round5_relief_race_is_documented()
    test_round4_wait_keeps_admission_snapshot()
    test_round4_cancel_wins_policy_stop()
    test_round4_contracts()
    test_round4_sync_batch_drives_admission_and_joins()
    test_round4_policy_stop_reasons()
    test_round4_relief_healthy_pool_and_episode_reset()
    test_round4_immediate_ids_and_audit()
    test_round4_sync_deadline_names_queued_members()
    test_round4_retry_preserves_fail_fast()
    test_round4_daemon_death_is_supervised()
    test_round4_queued_observer_is_static()
    test_round5_relief_session_bound()
    test_round4_saturated_scheduler_never_opens_session()
    test_round3_cancel_wins_admission_race()
    test_round3_comment_contract()
    test_round3_durable_batch_admission()
    test_round3_queue_rows_precede_execution()
    test_round3_readiness_prose()
    test_round3_scheduler_fairness()
    test_round3_queue_surfaces_and_cancel()
    test_round3_observer_threadpool_retry()
    test_round3_scheduler_lifecycle_and_groups()
    test_round3_wait_has_one_total_deadline()
    test_pool_is_sized_for_the_run_concurrency_it_admits()
    test_eight_concurrent_runs_keep_the_probes_alive_and_the_pool_intact()
    test_oversubscribed_launches_are_carried_or_refused_never_wedged()
    test_health_answers_while_every_worker_thread_is_pinned()
    test_ready_answers_honestly_when_the_pool_is_gone()
    test_pool_exhaustion_and_capacity_render_as_honest_503s()
    test_web_answers_a_capacity_503_without_touching_the_database()
    test_one_request_spends_at_most_one_pool_timeout()
    test_the_doorman_keeps_a_relief_valve_and_bounds_its_own_logging()
    test_capacity_gate_refuses_before_it_writes_anything()
    test_pool_crash_records_its_reason_with_no_connection_to_spare()
    test_engine_profile_is_read_once_not_once_per_segment()
    test_a_failed_thread_start_never_steals_a_live_runs_slot()
    test_a_fan_out_bigger_than_the_cap_runs_queued_and_never_leaks_a_slot()
    test_mcp_relays_the_capacity_503_as_wait_and_retry()
    import ast
    import inspect

    tree = ast.parse(inspect.getsource(__import__(__name__)))
    defined = {
        n.name for n in tree.body if isinstance(n, ast.FunctionDef) and n.name.startswith("test_")
    }
    main_node = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "main")
    called = {
        n.func.id
        for n in ast.walk(main_node)
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
    }
    assert defined <= called, ("unregistered owning tests", defined - called)
    print("STRESS LIVENESS POOL OK")


if __name__ == "__main__":
    main()
