"""RUN OBSERVABILITY: in-step PROGRESS (GitHub #101) + the terminal REASON (GitHub #106).

TWO DEFECTS FROM THE 2026-09-06 NIGHT STRESS PASS, both "the product knows and does not say".

#101 -- THE MIDDLE OF A LONG RUN IS DARK. A StepRun row is only written when a step ENDS, so
a classic run over a multi-segment document reported `step_states: []` on /api, `translated:
0` on the document, the same nothing through `workflow show-run`, and -- because /runs/stream
is `observe_run` polling StepRun rows -- `run_started` then SILENCE on the SSE stream, for the
entire 10-25 minutes of its `llm_agent` step. Measured on this install: 12+ minutes frozen on
a 102-segment document while the (default-OFF) LLM Ledger's call counter climbed 4 -> 45, and
20 minutes on a 258-segment one. An operator could not tell "working" from "wedged", and the
campaign's own harness raised false STUCK alarms on healthy runs.

#106 -- A RECONCILED RUN SAYS WHY IT DIED NOWHERE. A run moved to a terminal state without its
executor present (boot reconcile after the prod OOM kill, the staleness sweep, an operator
cancel) has no StepRun to blame, so `failure_shape` and `step_states` are empty BY
CONSTRUCTION. Four prod runs ended `failed` / `cancelled` with `warnings: []` and nothing
else: "the engine was unreachable", "the fence tripped", "the machine was killed and the run
was reconciled at boot" are five different operator responses behind one word. The
reconcilers already stamp a distinct sentence on `run.notes`; it never left the DB.

CELLS
  [P1] A REAL multi-segment run publishes progress FROM INSIDE the step: read through the
       provider seam mid-step, the counter has already moved -- both live and in the
       PERSISTED columns another process reads -- while `step_states` is still empty, the
       exact window that used to be dark. It also asserts the #100 invariant: not one
       progress write opened a Session on a fan-out POOL WORKER thread.
  [P2] The counter lands on N/N with the right unit, and BOTH read models (the /api envelope
       and the CLI's) report it identically.
  [P3] A run that published nothing reports `progress: null` -- "no progress recorded", never
       a false 0%.
  [P4] The SSE stream forwards a `progress` event when the counter moves, and only then.
  [P5] A progress flush NEVER WAITS ON THE CONNECTION POOL (gate F round 1, H1): with every
       connection checked out (pool_size + max_overflow held), the call a translation thread
       makes returns in milliseconds, the write is SKIPPED rather than queued, and the
       throttle is not marked satisfied -- so the counter catches up as soon as a slot frees.
  [P6] A step that DIES mid-fan-out reports the units that actually landed, not a cosmetic
       N/N (gate F round 1, M2): 6 segments, the 2nd provider call raises DeadEngineError.
  [P7] No executor leaks live ticker state (gate F round 1, L1).
  [P8] The run page keeps the LIVENESS half of the counter after an SSE update (gate F
       round 1, M4) and attributes it to a step (L3) -- and it settles that line ONLY on an
       ending (gate F round 3, MED-1): `observe_run`'s 15-minute observer ceiling closes the
       stream on a run that is still RUNNING, so the page reopens the stream and keeps
       ageing instead of freezing, and a late `progress` cannot un-freeze a real ending (L5).
       Round 4 pins the REOPEN itself: only a VISIBLE tab renews the server-side worker
       thread (MED-1), the 24-window cap and the replay de-dup are ratcheted (LOW-1/LOW-2),
       and the cap agrees with both the page's promise and `observe_run`'s ACCEPTED note.
  [R1] The BOOT RECONCILER's reason reaches all three surfaces with a stable code.
  [R2] A BATCH MEMBER reconciled at boot carries its own reason (it used to get none: the
       note went to the batch row, which no run surface reads).
  [R3] An operator CANCEL is a named reason too, and rides the `closed` SSE event.
  [R4] A COMPLETED run carries NO reason -- with or without an informational note on the
       shared `notes` column, because `reason` answers "why did this end badly?".
  [R5] A FAILED run whose notes hold ONLY an informational sentence reports NO reason (gate
       F round 1, M1): `run.notes` is a shared appending column, and publishing a tm_match
       pool note as the terminal cause is a new lie beside a `failure_shape` that names a
       different one. When a terminal note IS present the text starts at THAT sentence.
  [R6] A batch member that CRASHES (not one reconciled at boot) carries its own reason (gate
       F round 1, H2) -- the sibling handler in orchestrator._run_in_background stamped one
       and this one stamped nothing, which is #106's own reported trigger.
  [R7] The terminal-write SWEEP H2 asked for: no path leaves a run FAILED with nothing on
       the row. The last one was the empty-document guard, whose reason only ever rode a
       live event.
  [R8] THE RATCHET under the closed reason allowlist (gate F round 2, M2). Every one of
       the module's own terminal-reconciler note constants is discovered at RUNTIME and
       must resolve to a distinct non-None code -- so rewording a sentence, or adding a
       seventh reconciler, fails HERE instead of silently returning that path to
       `reason: null` (#106 reinstated) with every suite green. It also covers the two
       codes that had no cell at all before this round (`superseded_stale`,
       `superseded_by_recovery`) and the documented precedence between them.
  [R9] A POOL-EXHAUSTED crash is named on every handler (GitHub #350). A connection-pool
       checkout timeout is one of the crash handlers' own classes, and all three handlers
       (the sync `run_workflow` arm, `orchestrator._run_in_background`, the batch member)
       must end the row FAILED carrying ONLY the pool note, in the same order as each
       other, resolving to `pool_exhausted` on every surface -- before the fix the two
       plain-run handlers left `reason: null` and the batch member reported `run_crashed`
       (it stamped the crash note too). The R8 ratchet now sees `db.POOL_EXHAUSTED_NOTE`
       as well, so an unmapped pool note fails the suite instead of going quiet.

Hermetic + offline: throwaway LOC_TMS_DATA_DIR, provider keys blanked, MockProvider only.
ASCII-only output.
"""

import contextlib
import inspect
import json
import os
import re
import sys
import threading
import time
from datetime import datetime, timedelta

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(_ROOT)
sys.path.insert(0, _ROOT)
from tests._bootstrap import init_env
from tests._piece_patch import set_pieces

init_env("loctms_runobs_")

from fastapi.testclient import TestClient
from sqlmodel import Session, select

from app.db import engine, init_db
from app.main import app
from app.models import (
    Customer,
    Document,
    Project,
    ProjectType,
    Role,
    RunStatus,
    Segment,
    SegStatus,
    StepRun,
    StepType,
    TransMode,
    Workflow,
    WorkflowBatch,
    WorkflowRun,
)
from app.services import apikeys, batch_runs, orchestrator, run_overlay, security, settings
from app.services import workflows as wf_service
from tests._cli_runner import run_cli

SRC = "en-US"
TGT = "de-DE"
_SEQ = [0]


def _seq() -> int:
    _SEQ[0] += 1
    return _SEQ[0]


def _fail(msg):
    print("FAIL:", msg, file=sys.stderr)
    sys.exit(1)


def _project_and_doc(segments=6):
    """A customer + project + a SEGMENTED document with `segments` real Segment rows -- the
    shape a segmented llm_agent step fans out over. Returns (project_id, doc_id, n)."""
    with Session(engine) as s:
        cust = Customer(name="RunObs Cust %d" % _seq())  # noqa: UP031
        s.add(cust)
        s.commit()
        s.refresh(cust)
        proj = Project(
            name="RunObs %d" % _seq(),  # noqa: UP031
            source_locale=SRC,
            default_provider="mock",
            customer_id=cust.id,
            project_type=ProjectType.DOCUMENT,
        )
        s.add(proj)
        s.commit()
        s.refresh(proj)
        doc = Document(
            project_id=proj.id, filename=f"runobs-{_seq()}.txt", mode=TransMode.SEGMENTED
        )
        s.add(doc)
        s.commit()
        s.refresh(doc)
        for i in range(segments):
            s.add(
                Segment(
                    document_id=doc.id,
                    target_locale=TGT,
                    order_index=i,
                    source_text="Sentence number %d in the source document." % i,  # noqa: UP031
                    status=SegStatus.NEW,
                )
            )
        s.commit()
        return proj.id, doc.id, segments


def _workflow():
    with Session(engine) as s:
        w = Workflow(
            name="RunObs WF %d" % _seq(),  # noqa: UP031
            scope="global",
            source_locale=SRC,
            target_locale=TGT,
        )
        s.add(w)
        s.commit()
        s.refresh(w)
        wf_service.add_step(
            s,
            w.id,
            name="Translate",
            step_type=StepType.LLM_AGENT,
            provider="mock",
            prompt_text="Translate to {target_locale}: {text}",
            chunk_policy="none",
            tm_examples=False,
        )
        return w.id


def _plant_running(did, wid, status=RunStatus.RUNNING):
    """A bare WorkflowRun row with no executor -- the shape the counter/reason surfaces read."""
    with Session(engine) as s:
        run = WorkflowRun(
            workflow_id=wid,
            document_id=did,
            target_locale=TGT,
            status=status,
            created_by="planted",
        )
        s.add(run)
        s.commit()
        s.refresh(run)
        return run.id


def _run_row(rid):
    with Session(engine) as s:
        return s.get(WorkflowRun, rid)


def _api_headers():
    settings.set_many({"security_auth_required": "1", "multi_org_enabled": "0"})
    settings.invalidate_cache()
    u = security.create_user(
        "runobs-%d@x.com" % _seq(),  # noqa: UP031
        "RunObs Admin",
        Role.ADMIN,
        "runobs-password-123456",
    )
    _row, raw = apikeys.create_key(u.id, "runobs-key-%d" % _seq(), scopes="api docs")  # noqa: UP031
    return {"X-Loc-Token": raw}


def _api_run(client, headers, rid):
    r = client.get("/api/runs/%d" % rid, headers=headers)  # noqa: UP031
    assert r.status_code == 200, (r.status_code, r.text)
    return r.json()


def _cli_show_run(rid, as_json=True):
    """`workflow show-run <rid>` through the REAL CLI entry point (in-process arm)."""
    argv = (["--json"] if as_json else []) + ["workflow", "show-run", str(rid)]
    rc, out, err = run_cli(argv)
    return rc, out + err


# ============================================================================
# [P1]/[P2] in-step progress on a REAL run
# ============================================================================
#: Per-provider-call beat in [P1]: the MockProvider returns instantly, so without it every
#: segment of the fixture run lands inside one throttle window and the cell would prove only
#: that the FINAL forced write happens -- not that the counter moves DURING the step.
_CALL_BEAT_S = 0.12
_MID = {"seen": None, "row": None, "status": None, "step_states": None}


def test_p1_progress_moves_inside_the_step():
    _pid, did, nseg = _project_and_doc(segments=6)
    wid = _workflow()

    real_completion = orchestrator.run_completion
    state = {"calls": 0, "run_id": None, "lock": threading.Lock()}

    def spy(*a, **kw):
        """Wraps the provider seam so the run is observed WHILE its only step is still
        running -- the window that used to show nothing at all. Each call takes a beat (the
        MockProvider is instant; a real engine takes seconds to minutes per segment, which is
        the whole premise of #101), and on the 4th segment call we read the run back from a
        FRESH session -- exactly what /api, the CLI and the SSE poller read."""
        out = real_completion(*a, **kw)
        time.sleep(_CALL_BEAT_S)
        with state["lock"]:
            state["calls"] += 1
            n = state["calls"]
        if n == 4 and state["run_id"]:
            with Session(engine) as s:
                run = s.get(WorkflowRun, state["run_id"])
                _MID["seen"] = orchestrator.run_progress(run)
                # The PERSISTED columns, read raw: what a DIFFERENT process (the server
                # answering /api for a headless CLI run) can see. The live in-memory
                # counter above is only visible inside the executing process, so a fix
                # that moved only that would leave the cross-process surfaces dark.
                _MID["row"] = (run.progress_done, run.progress_total, run.progress_unit)
                _MID["status"] = run.status
                _MID["step_states"] = run_overlay.run_step_states(
                    s.exec(select(StepRun).where(StepRun.workflow_run_id == run.id)).all(),
                    orchestrator.get_steps(s, run.workflow_id),
                    run_status=run.status,
                )["step_states"]
        return out

    set_pieces("app.services.orchestrator", "run_completion", spy)
    # THE #100 INVARIANT, ASSERTED. Every progress PERSIST must happen on the run's own
    # thread: a Session opened per unit from a fan-out worker is the exact pattern behind
    # the sibling HIGH (SQLAlchemy QueuePool exhaustion at 4-6 concurrent runs, which left
    # runs stuck forever). The counter still MOVES on the worker threads -- in memory only.
    real_write = orchestrator._write_progress_row
    writer_threads = set()

    def spy_write(*a, **kw):
        writer_threads.add(threading.current_thread().name)
        return real_write(*a, **kw)

    set_pieces("app.services.orchestrator", "_write_progress_row", spy_write)
    # The persist throttle is wall-clock (5 s by default -- nothing beside a 10-25 minute
    # step). Scale it, and the simulated per-call latency, down together so the same
    # throttled path runs at test speed instead of being bypassed.
    real_interval = orchestrator.PROGRESS_MIN_INTERVAL_S
    set_pieces("app.services.orchestrator", "PROGRESS_MIN_INTERVAL_S", 0.05)
    try:
        with Session(engine) as s:
            rid = orchestrator.start_run(s, wid, did, TGT)
        state["run_id"] = rid
        _await_terminal(rid)
    finally:
        set_pieces("app.services.orchestrator", "run_completion", real_completion)
        set_pieces("app.services.orchestrator", "_write_progress_row", real_write)
        set_pieces("app.services.orchestrator", "PROGRESS_MIN_INTERVAL_S", real_interval)

    if _MID["seen"] is None:
        _fail(
            "no progress was visible from inside the step: a run was still reporting "
            "nothing mid-step (#101)"
        )
    seen = _MID["seen"]
    if not (0 <= seen["done"] <= seen["total"]) or seen["total"] != nseg:
        _fail(f"mid-step progress is not honest: {seen!r} (document has {nseg} segments)")
    if seen["done"] < 1:
        _fail("mid-step progress never moved off 0 while 4 segments had been translated")
    if seen["done"] >= seen["total"]:
        _fail("the mid-step read landed after the step finished; the cell proves nothing")
    if _MID["status"] != RunStatus.RUNNING:
        _fail(f"the mid-step read did not happen while the run was RUNNING: {_MID['status']!r}")
    if _MID["step_states"]:
        _fail(
            "fixture assumption broken: step_states was NON-empty mid-step, so this is not "
            "the dark window #101 is about: {!r}".format(_MID["step_states"])
        )
    r_done, r_total, r_unit = _MID["row"]
    if r_total != nseg or r_unit != "segments":
        _fail("the PERSISTED progress columns are wrong mid-step: {!r}".format(_MID["row"]))
    if not (1 <= r_done < nseg):
        _fail(
            "the persisted counter never moved mid-step ({!r}): a run executed by another "
            "process would still look dark on /api (#101)".format(_MID["row"])
        )
    if not writer_threads:
        _fail("no progress was ever persisted: the cross-process surfaces stay dark (#101)")
    pooled = sorted(t for t in writer_threads if t.startswith("ThreadPoolExecutor"))
    if pooled:
        _fail(
            f"a progress persist opened a DB Session on a POOL WORKER thread {pooled!r} -- that is "
            "the #100 pattern (QueuePool exhaustion); ticks must be memory-only off the "
            "run's own thread"
        )
    print(
        f"  [P1] mid-step read (4th segment call): live progress {seen['done']}/{seen['total']} "
        f"{seen['unit']}, persisted {r_done}/{r_total}, run RUNNING, "
        "no worker-thread DB session OK"
    )
    return rid, nseg


def test_p2_final_progress_on_every_surface(rid, nseg):
    run = _run_row(rid)
    prog = orchestrator.run_progress(run)
    if not prog or prog["done"] != nseg or prog["total"] != nseg:
        _fail(f"a finished run must report N/N segments, got {prog!r} (N={nseg})")
    if prog["unit"] != "segments" or not prog["at"]:
        _fail(f"progress unit/timestamp not published honestly: {prog!r}")

    headers = _api_headers()
    client = TestClient(app)
    body = _api_run(client, headers, rid)
    if body.get("progress") != prog:
        _fail(f"/api/runs/{rid} progress {body.get('progress')!r} != {prog!r}")

    rc, out = _cli_show_run(rid)
    if rc != 0 or ('"progress"' not in out):
        _fail(f"CLI show-run --json carries no progress: rc={rc} out={out[:400]}")
    import json as _json

    payload = _json.loads(out)
    if payload.get("progress") != prog:
        _fail(f"CLI/--api progress envelopes differ: {payload.get('progress')!r} vs {prog!r}")
    rc2, human = _cli_show_run(rid, as_json=False)
    if rc2 != 0 or (f"progress: {nseg}/{nseg} segments") not in human:
        _fail(f"CLI human output shows no progress line: {human[:400]}")
    settings.set_many({"security_auth_required": "0"})
    settings.invalidate_cache()
    print(
        f"  [P2] final progress {nseg}/{nseg} segments identical on /api, CLI --json "
        "and CLI human OK"
    )


def test_p3_no_progress_is_null_not_zero():
    _pid, did, _n = _project_and_doc(segments=2)
    wid = _workflow()
    with Session(engine) as s:
        run = WorkflowRun(
            workflow_id=wid,
            document_id=did,
            target_locale=TGT,
            status=RunStatus.PENDING,
            created_by="planted",
        )
        s.add(run)
        s.commit()
        s.refresh(run)
        rid = run.id
    if orchestrator.run_progress(_run_row(rid)) is not None:
        _fail("a run that published nothing must report progress None, not 0/0")
    headers = _api_headers()
    client = TestClient(app)
    body = _api_run(client, headers, rid)
    if body.get("progress") is not None:
        _fail(f"/api must report progress null for a run with none: {body.get('progress')!r}")
    if run_overlay.format_progress_ascii(None) != "":
        _fail("the progress renderer must print nothing for a run with no progress")
    settings.set_many({"security_auth_required": "0"})
    settings.invalidate_cache()
    print("  [P3] a run with no published progress reports null (no false 0%) OK")


def test_p4_sse_forwards_progress_on_change():
    """observe_run emits a `progress` event when the counter moves -- and only then."""
    _pid, did, _n = _project_and_doc(segments=3)
    wid = _workflow()
    with Session(engine) as s:
        run = WorkflowRun(
            workflow_id=wid,
            document_id=did,
            target_locale=TGT,
            status=RunStatus.RUNNING,
            created_by="planted",
        )
        s.add(run)
        s.commit()
        s.refresh(run)
        rid = run.id
    orchestrator.publish_run_progress(rid, 2, 7, "segments", force=True)
    events = []
    gen = orchestrator.observe_run(rid, poll_interval=0.01, max_seconds=1.5)
    for _run, ev in gen:
        events.append(ev)
        if ev["type"] == "progress" and len([e for e in events if e["type"] == "progress"]) == 1:
            # Move the counter once; the NEXT poll must emit exactly one more event.
            orchestrator.publish_run_progress(rid, 5, 7, "segments", force=True)
        if len(events) > 40:
            break
    progress_events = [e for e in events if e["type"] == "progress"]
    if len(progress_events) < 2:
        _fail(f"the SSE stream did not forward the moving counter: {progress_events!r}")
    if progress_events[0]["done"] != 2 or progress_events[-1]["done"] != 5:
        _fail(f"progress events do not track the counter: {progress_events!r}")
    dones = [e["done"] for e in progress_events]
    if len(dones) != len(set(dones)):
        _fail(f"the stream repeated an unchanged counter (must emit on CHANGE only): {dones!r}")
    orchestrator._progress_forget(rid)  # planted directly: drop the live entry by hand
    print(f"  [P4] SSE `progress` events: {dones!r}, emitted on change only OK")


def test_p5_flush_never_waits_on_an_exhausted_pool():
    """GATE F ROUND 1, H1. The first cut opened `Session(engine)` INLINE on the run's own
    thread, which is a QueuePool checkout with SQLAlchemy's default pool_timeout=30. Under
    the exhaustion of the sibling HIGH (#100 -- QueuePool limit reached at 4-6 concurrent
    runs) every flush blocked the TRANSLATION thread for a full 30 s and then swallowed the
    TimeoutError at debug: measured at 30.00 s on this worktree, once per collected future,
    i.e. ~51 minutes of pure telemetry stall on a 102-segment step. A counter added to prove
    a run is alive must not be the thing that stops it moving.

    Here every connection is checked out (pool_size + max_overflow) before the flush."""
    from app.db import engine as _eng

    _pid, did, _n = _project_and_doc(segments=2)
    wid = _workflow()
    rid = _plant_running(did, wid)

    pool = _eng.pool
    capacity = int(pool.size()) + int(getattr(pool, "_max_overflow", 10))
    held = []
    try:
        for _ in range(capacity):
            held.append(_eng.connect())
        if pool.checkedin() != 0:
            _fail("fixture assumption broken: the pool still has an idle connection")
        # What a translation thread actually calls, on an exhausted pool.
        t0 = time.perf_counter()
        orchestrator.publish_run_progress(rid, 3, 9, "segments", force=True, step="Translate")
        publish_s = time.perf_counter() - t0
        t1 = time.perf_counter()
        orchestrator._flush_progress(rid, force=True)
        flush_s = time.perf_counter() - t1
        # And the write path itself: it must SKIP, not queue behind the translations.
        t2 = time.perf_counter()
        wrote = orchestrator._write_progress_row(
            rid, 3, 9, "segments", datetime.utcnow(), "Translate", 0
        )
        write_s = time.perf_counter() - t2
        if publish_s > 0.25 or flush_s > 0.25:
            _fail(
                "a progress publish BLOCKED the caller with the pool exhausted: "
                f"publish {publish_s:.2f}s / flush {flush_s:.2f}s (H1: it must never wait)"
            )
        if write_s > 1.0:
            _fail(f"_write_progress_row waited {write_s:.2f}s on an exhausted pool (H1)")
        if wrote:
            _fail("_write_progress_row took a pool slot for telemetry while the pool was full")
        st = orchestrator._progress_state.get(rid) or {}
        if st.get("flushed_done", -1) != -1:
            _fail(
                "a SKIPPED write marked the throttle satisfied: the counter would then be "
                "stale until the next unit moved it (H1: stamp flushed_at only on success)"
            )
    finally:
        for c in held:
            with contextlib.suppress(Exception):
                c.close()
    # Read the row only now: the assertion itself needs a connection, and this cell's whole
    # premise is that there was none.
    if not orchestrator.progress_drain(10.0):
        _fail("the background progress writer never settled after the exhausted-pool flush")
    if _run_row(rid).progress_at is not None:
        _fail("a row was written while the pool was exhausted; the skip did not happen")
    # With the pool healthy again the very next flush catches up (nothing was lost).
    orchestrator._flush_progress(rid, force=True)
    if not orchestrator.progress_drain(10.0):
        _fail("the background progress writer never drained after the pool freed up")
    row = _run_row(rid)
    if (row.progress_done, row.progress_total) != (3, 9):
        _fail(
            "the counter did not catch up once the pool freed: "
            f"{row.progress_done}/{row.progress_total}"
        )
    orchestrator._progress_forget(rid)
    print(
        f"  [P5] pool exhausted ({capacity} checked out): publish {publish_s * 1000:.1f} ms, "
        f"write skipped in {write_s * 1000:.1f} ms, throttle not consumed, catch-up OK"
    )


def test_p6_failed_step_counts_only_units_that_landed():
    """GATE F ROUND 1, M2. `finish`'s docstring promised the honest 7-of-102, but `on_done`
    ticked for EVERY future including the ones that raised, and `with ThreadPoolExecutor(...)`
    joins on exit -- so every callback had fired before `except DeadEngineError`. A 6-segment
    document whose 2nd provider call died persisted `6 / 6 segments` beside `status: failed`,
    which an operator reads as "everything finished"."""
    _pid, did, nseg = _project_and_doc(segments=6)
    wid = _workflow()
    real = orchestrator._run_step_on_segment
    state = {"n": 0, "lock": threading.Lock()}

    def boom(*a, **kw):
        with state["lock"]:
            state["n"] += 1
            n = state["n"]
        if n == 2:
            raise orchestrator.DeadEngineError("engine:99 is disabled")
        return real(*a, **kw)

    set_pieces("app.services.orchestrator", "_run_step_on_segment", boom)
    try:
        with Session(engine) as s:
            rid = orchestrator.start_run(s, wid, did, TGT)
        status = _await_terminal(rid)
    finally:
        set_pieces("app.services.orchestrator", "_run_step_on_segment", real)
    if status != RunStatus.FAILED:
        _fail(f"fixture assumption broken: the run should have FAILED, got {status!r}")
    prog = orchestrator.run_progress(_run_row(rid))
    if not prog:
        _fail("a failed segmented step published no progress at all")
    if prog["done"] >= prog["total"]:
        _fail(
            "a FAILED step reported a cosmetic {}/{} -- the future that RAISED was counted "
            "as a completed unit (M2)".format(prog["done"], prog["total"])
        )
    if prog["done"] != nseg - 1:
        _fail(
            f"the counter is not the number of units that landed: {prog['done']}/{prog['total']} "
            f"with exactly one of {nseg} calls raising"
        )
    line = run_overlay.format_progress_ascii(prog)
    if f"{nseg - 1}/{nseg} segments" not in line:
        _fail(f"the console line does not report the honest count: {line!r}")
    print(
        f"  [P6] one of {nseg} segment calls raised -> progress {prog['done']}/{prog['total']} "
        "(no cosmetic N/N beside `failed`) OK"
    )


def test_p7_no_executor_leaks_ticker_state():
    """GATE F ROUND 1, L1. Only `_run_in_background` dropped the live entry, so the
    synchronous CLI path and batch members left one dict per run in a long-lived server --
    and because a leaked entry's `at` is newer than any persisted row, `run_progress` served
    the STALE live entry for that id forever (and, after SQLite rowid reuse, for whatever run
    inherited it)."""
    _pid, did, _n = _project_and_doc(segments=3)
    wid = _workflow()
    with Session(engine) as s:
        summary = orchestrator.run_workflow(s, wid, did, TGT)  # SYNCHRONOUS path
    rid = summary["run_id"]
    if rid in orchestrator._progress_state:
        _fail(
            "the synchronous executor leaked live progress state for run "
            f"{rid}: a stale entry can never be aged out and shadows the row forever (L1)"
        )
    prog = orchestrator.run_progress(_run_row(rid))
    if not prog or prog["done"] != 3:
        _fail(f"the synchronous run did not persist its final count: {prog!r}")

    # THE CASE THE TICKER'S OWN step-boundary CANNOT COVER: a step that dies on an
    # exception the step machinery does not catch (H2's `sqlalchemy.exc.TimeoutError`
    # under pool exhaustion is exactly this shape), so `_prog.finish` is never reached.
    # Only the run-stream wrapper's `finally` can drop the entry, and it must.
    _pid, did2, _n2 = _project_and_doc(segments=6)
    wid2 = _workflow()
    real = orchestrator._run_step_on_segment

    def boom(*a, **kw):
        raise RuntimeError("pool timeout while resolving engine:2")

    set_pieces("app.services.orchestrator", "_run_step_on_segment", boom)
    rid2 = None
    try:
        with Session(engine) as s:
            for ev in orchestrator.run_workflow_stream(s, wid2, did2, TGT):
                if ev.get("type") == "run_started":
                    rid2 = ev["run_id"]
    except RuntimeError:
        pass
    finally:
        set_pieces("app.services.orchestrator", "_run_step_on_segment", real)
    if rid2 is None:
        _fail("fixture assumption broken: the crashing run never announced its id")
    if rid2 in orchestrator._progress_state:
        _fail(
            "a step that died OUTSIDE the ticker's step boundary leaked its live entry for "
            f"run {rid2}: `run_progress` would serve that stale counter forever (L1)"
        )
    print("  [P7] neither the synchronous executor nor a hard mid-step crash leaks state OK")


def _js_block(body, opener):
    """The body of a top-level `function foo(...) {` in the page's inline script, cut at the
    first closing brace on the script's own two-space indentation. Used to pin WHERE a guard
    sits rather than merely that its text appears somewhere in the page."""
    if opener not in body:
        _fail(f"the run page no longer defines {opener!r} (gate F round 3, MED-1)")
    return body.split(opener, 1)[1].split("\n  }\n", 1)[0]


def _assert_the_freeze_is_conditional(body, rid):
    """GATE F ROUND 3, MED-1 + L5. `freezeProgress` must fire only for an ENDING.

    Round 2 wired it to EVERY `closed` event, and `observe_run` emits `closed` from five
    places -- the run row being gone, the live pause inside the step loop, the
    paused-already-replayed close, a terminal status, and its own 15-minute
    OBSERVER CEILING, which reports the run's real status (RUNNING) and says in its own
    docstring that it is "not a terminal verdict". So on exactly the 10-25 minute
    `llm_agent` steps GitHub #101 exists for, the page settled the line forever at t=900 s
    and a wedged run went back to being pixel-identical to a healthy one. The round-2 pin
    made it worse by ratcheting the UNCONDITIONAL call in, so this replaces it with the
    conditional behaviour, pinned four ways:

      (a) the `closed` branch decides on the STATUS before it freezes, and the ceiling arm
          RETURNS rather than falling through into the freeze;
      (b) the page's live-status list actually contains the status word `observe_run` puts
          on a real ceiling close (measured here, through `json.dumps`, not assumed), and
          is exactly the non-terminal, non-paused, non-queued part of `RunStatus` -- so a new run
          status cannot slip past it silently;
      (c) the ceiling arm REOPENS the stream and leaves the ageing beat running, and a
          `progress` event arriving after a real freeze cannot un-freeze the line (L5);
      (d) the reopen is VISIBLE-tab-only, bounded, doc-true and de-duplicated -- see
          `_assert_the_reopen_is_bounded_and_watched` (gate F round 4).
    """
    # (a) order inside the `closed` branch: gate -> reopen -> return -> only then freeze.
    if "ev.type === 'closed'" not in body:
        _fail("the run page no longer has an SSE `closed` branch (MED-1)")
    tail = body.split("ev.type === 'closed'", 1)[1]
    gate_at = tail.find("STILL_LIVE_STATUSES")
    reopen_at = tail.find("reopenStream()")
    freeze_at = tail.find("freezeProgress(")
    if gate_at == -1 or reopen_at == -1:
        _fail(
            "the `closed` branch does not distinguish an ENDING from `observe_run`'s "
            "15-minute observer ceiling: every close freezes the progress line, so a run "
            "still RUNNING at t=900 s loses its liveness half forever (MED-1)"
        )
    if freeze_at == -1:
        _fail("the `closed` branch no longer freezes a genuinely ended run (L6)")
    if not gate_at < reopen_at < freeze_at:
        _fail(
            "the `closed` branch freezes the progress line before it has decided whether "
            f"the close is an ending (gate at {gate_at}, reopen at {reopen_at}, freeze at "
            f"{freeze_at}) (MED-1)"
        )
    if "return;" not in tail[reopen_at:freeze_at]:
        _fail(
            "the observer-ceiling arm of the `closed` branch falls through into "
            "`freezeProgress`, so the run it just decided is STILL RUNNING freezes anyway "
            "(MED-1)"
        )
    # (b) the list is the real one, measured against a real ceiling close.
    m = re.search(r"const STILL_LIVE_STATUSES = \[([^\]]*)\]", body)
    if not m:
        _fail("the run page declares no STILL_LIVE_STATUSES list to gate the freeze (MED-1)")
    live_words = {w.strip().strip("'\"") for w in m.group(1).split(",") if w.strip()}
    all_statuses = {v for k, v in vars(RunStatus).items() if k.isupper() and isinstance(v, str)}
    # SKIPPED is a StepRun-only status -- "a RUN is never SKIPPED" (app/models.py:163-166) --
    # so it can never reach a `closed` event and belongs to neither list.
    # QUEUED has no executing worker and renders statically (Gate A round 4, M7).
    expected_live = (
        all_statuses
        - set(orchestrator._TERMINAL)
        - {RunStatus.PAUSED, RunStatus.SKIPPED, RunStatus.QUEUED}
    )
    if live_words != expected_live:
        _fail(
            f"the page's live-status list {sorted(live_words)} is not the non-terminal, "
            f"non-paused, non-queued part of RunStatus {sorted(expected_live)}: a run in a status "
            "neither list knows would be frozen (or reconnected) wrongly (MED-1)"
        )
    ceiling = None
    # Bounded on purpose (round-4 carried note): a bare `for ... break` over a generator
    # that stopped closing would HANG this suite rather than fail it.
    probe = orchestrator.observe_run(rid, poll_interval=0.05, max_seconds=0.0)
    for i, (_run, ev) in enumerate(probe):
        if ev.get("type") == "closed":
            ceiling = json.loads(json.dumps(ev))
            break
        if i > 200:
            _fail(
                "observe_run emitted 200 events at a 0 s ceiling without ever closing: the "
                "page would never learn the window ended (MED-1)"
            )
    if ceiling is None:
        _fail("observe_run did not close at its ceiling on a planted RUNNING run (MED-1)")
    if ceiling.get("status") not in live_words:
        _fail(
            "observe_run's OBSERVER-CEILING close carries "
            f"status={ceiling.get('status')!r}, which the page does not count as live: the "
            "page would freeze the progress line of a run that is still executing (MED-1)"
        )
    # (c) the ceiling arm reopens and does not stop the beat; a late progress cannot
    #     un-freeze a real ending.
    reopen = _js_block(body, "function reopenStream()")
    if "connectSSE(" not in reopen:
        _fail(
            "the observer-ceiling arm does not reopen the stream, so a run longer than 15 "
            "minutes stops receiving steps and progress altogether (MED-1)"
        )
    for stopper in ("clearInterval", "freezeProgress"):
        if stopper in reopen:
            _fail(
                f"the observer-ceiling reopen calls {stopper!r}: the ageing beat is the "
                "only liveness signal while the stream is between generators (MED-1)"
            )
    if "progressFrozen = true" not in _js_block(body, "function freezeProgress("):
        _fail("freezing the progress line does not latch, so a later event can un-freeze it (L5)")
    if "if (progressFrozen) return;" not in _js_block(body, "function setProgress("):
        _fail(
            "a `progress` event arriving AFTER the freeze repaints an ageing line that "
            "nothing counts up any more, leaving a stale age forever (L5)"
        )
    _assert_the_reopen_is_bounded_and_watched(body)


def _assert_the_reopen_is_bounded_and_watched(body):
    """GATE F ROUND 4, MED-1 + LOW-1 + LOW-2. The reopen the round-3 fix introduced is a
    SERVER-side commitment -- `/runs/{id}/stream` is a sync generator, so Starlette holds one
    anyio thread-limiter token (40 shared with every sync route) for the life of each stream
    -- and round 3 shipped its two fail-safes UNRATCHETED: deleting the whole
    `ceilingReopens` block, or replacing `if (replayPending)` with `if (false)`, both left
    this suite green (the round-4 gate measured both). So they are pinned here, together with
    the round-4 fix itself:

      (d1) the reopen is gated on the tab being VISIBLE, and RETURNS before `connectSSE`
           when it is not, so a forgotten background tab does not renew a 15-minute worker
           thread 24 times over; a `visibilitychange` listener picks the stream back up.
      (d2) the reopen is BOUNDED by a numeric cap, checked with a `return` before
           `connectSSE`, and the counter really increments.
      (d3) the cap and `observe_run`'s own ceiling agree with the hours the page promises
           the operator AND with the bound `observe_run`'s ACCEPTED note now states -- the
           one place in the tree that costs this out said "15 min" for a whole round while
           the page renewed it 24 times.
      (d4) the replay DE-DUP survives: without it every ceiling boundary repaints the whole
           timeline on top of itself (24 copies of every step card on a 6-hour run).
    """
    reopen = _js_block(body, "function reopenStream()")
    conn_at = reopen.find("connectSSE(")
    # (d1) a hidden tab does not renew the commitment.
    vis_at = reopen.find("visibilityState")
    if vis_at == -1 or vis_at > conn_at:
        _fail(
            "`reopenStream` does not check `document.visibilityState` before it reconnects: "
            "an unwatched background tab renews a 15-minute SSE worker thread up to "
            "MAX_CEILING_REOPENS times, which is the round-4 MED-1 defect (MED-1)"
        )
    if "return;" not in reopen[vis_at:conn_at]:
        _fail(
            "`reopenStream` reads `document.visibilityState` but falls through to "
            "`connectSSE` anyway, so a hidden tab still takes a worker thread (MED-1)"
        )
    # ... and it PARKS the intent rather than dropping it. Matching the bare word
    # "visibilitychange" is not enough -- it also appears in this branch's own comment, so
    # deleting the listener outright left this arm green (measured while writing it).
    ret_at = reopen.find("return;", vis_at)
    m_park = re.search(r"(\w+) = true;", reopen[vis_at:ret_at])
    if not m_park:
        _fail(
            "the hidden-tab branch of `reopenStream` records nothing, so the stream is "
            "abandoned rather than parked: the page goes permanently deaf (MED-1)"
        )
    listener_at = body.find("addEventListener('visibilitychange'")
    if listener_at == -1:
        _fail(
            "the run page registers no `visibilitychange` listener, so a page hidden "
            "across one observer ceiling never picks the stream back up (MED-1)"
        )
    listener = body[listener_at:].split("\n  });", 1)[0]
    for needle in (m_park.group(1), "reopenStream("):
        if needle not in listener:
            _fail(
                f"the `visibilitychange` listener does not read {needle!r}: a tab brought "
                "back to the front does not resume the stream it parked (MED-1)"
            )
    # (d2) and a watched tab is still bounded.
    cap_at = reopen.find("MAX_CEILING_REOPENS")
    if cap_at == -1 or cap_at > conn_at:
        _fail(
            "`reopenStream` no longer checks a reopen cap before reconnecting: a page left "
            "open on a wedged run reconnects forever, which is exactly what the fix's own "
            "comment promises it does not do (round 4, LOW-1)"
        )
    if "return;" not in reopen[cap_at:conn_at]:
        _fail(
            "the reopen cap is read but not enforced -- no `return` between the check and "
            "`connectSSE` (round 4, LOW-1)"
        )
    if "+= 1" not in reopen[:conn_at]:
        _fail(
            "nothing increments the reopen counter, so the cap can never be reached "
            "(round 4, LOW-1)"
        )
    # (d3) DOC TRUTH: cap x observe_run's own window == the hours the page promises, and the
    # generator's ACCEPTED note states that bound rather than the bare 15 minutes.
    m = re.search(r"const MAX_CEILING_REOPENS = (\d+)", body)
    if not m:
        _fail("the run page declares no numeric MAX_CEILING_REOPENS (round 4, LOW-1)")
    cap = int(m.group(1))
    if cap < 1:
        _fail(f"MAX_CEILING_REOPENS = {cap} disables the reopen entirely (MED-1)")
    window_s = inspect.signature(orchestrator.observe_run).parameters["max_seconds"].default
    hours = cap * float(window_s) / 3600.0
    if f"{hours:g} hours" not in body:
        _fail(
            f"the page promises the operator a window that is not {cap} x {window_s} s = "
            f"{hours:g} hours: the 'live updates stopped' line and the cap have drifted "
            "apart (round 4, LOW-1)"
        )
    src = inspect.getsource(orchestrator.observe_run)
    note = src.split("max_seconds bounds", 1)[-1].split("from app.db import engine", 1)[0]
    for needle in (str(cap), f"{hours:g} h", "visible"):
        if needle not in note:
            _fail(
                f"`observe_run`'s ACCEPTED thread-pool note does not mention {needle!r}: it "
                "costs the stream out as a single 15-minute commitment while the run page "
                f"renews it up to {cap} times per visible tab (MED-1)"
            )
    # (d4) the replay de-dup.
    if "replayPending = true" not in reopen[:conn_at]:
        _fail(
            "the reopen does not arm the replay flag, so the fresh generator's replay is "
            "painted UNDER the cards this page already holds (round 4, LOW-2)"
        )
    started = body.split("ev.type === 'run_started'", 1)[-1].split("else if (ev.type", 1)[0]
    for needle in ("if (replayPending)", "replayPending = false", "log.innerHTML = ''"):
        if needle not in started:
            _fail(
                f"the `run_started` branch no longer carries {needle!r}: a reopened stream "
                "replays every recorded step on top of the cards already on the page, so a "
                "6-hour run shows 24 copies of every step (round 4, LOW-2)"
            )


def test_p8_run_page_keeps_the_liveness_line():
    """GATE F ROUND 1, M4. The server-rendered line carried "(last update <ts>)"; the SSE
    handler then overwrote it with a bare counter and IGNORED `ev.at`, which the event does
    carry. From the first progress event on, a run whose counter froze five minutes ago was
    pixel-identical to one that ticked a second ago -- on the primary surface, and that
    discrimination is the whole point of #101.

    GATE F ROUND 3 extends it: the round-2 "stop ageing a terminal run" fix must not fire on
    `observe_run`'s OBSERVER CEILING, which is not an ending. `_assert_the_freeze_is_
    conditional` carries that half."""
    _pid, did, _n = _project_and_doc(segments=4)
    wid = _workflow()
    rid = _plant_running(did, wid)
    orchestrator.publish_run_progress(rid, 2, 4, "segments", force=True, step="Translate")
    if not orchestrator.progress_drain(10.0):
        _fail("the background progress writer never drained")
    client = TestClient(app)
    body = client.get(f"/runs/{rid}").text
    if 'id="runProgress"' not in body:
        _fail("the run page carries no progress element")
    if "data-at=" not in body:
        _fail("the run page does not hand the client the timestamp the liveness line needs")
    if "setProgress(ev)" not in body:
        _fail("the SSE progress branch does not go through the renderer that keeps `at`")
    if "last update ${secs} s ago" not in body:
        _fail("the run page renders no relative age, so a frozen counter is indistinguishable")
    if "`Progress: ${ev.done}/${ev.total} ${ev.unit}`" in body:
        _fail(
            "the SSE handler still overwrites the progress line with a bare counter, "
            "dropping the liveness half (M4)"
        )
    # ... and it STOPS ageing once the run ends (gate F round 2, L6). The beat exists to
    # say "this counter has not moved" about a run that should be moving; on a finished run
    # it kept counting up forever, which is the same false alarm pointed the other way.
    if "clearInterval(progressTimer)" not in body:
        _fail(
            "the run page never stops the 1 s ageing beat, so a COMPLETED/FAILED run's "
            "page keeps reporting `last update N h ago` forever (L6)"
        )
    for branch in ("freezeProgress('run completed')", "freezeProgress(`run ${ev.status"):
        if branch not in body:
            _fail(f"a terminal SSE branch does not freeze the progress line: {branch!r} (L6)")
    _assert_the_freeze_is_conditional(body, rid)
    # The step attribution (L3) rides the same line on every surface.
    prog = orchestrator.run_progress(_run_row(rid))
    if prog.get("step") != "Translate":
        _fail(f"the counter is not attributed to its step: {prog!r}")
    if "step 0 Translate" not in run_overlay.format_progress_ascii(prog):
        _fail("the console line does not name the step the counter belongs to (L3)")
    orchestrator._progress_forget(rid)
    print(
        "  [P8] the run page keeps `last update N s ago` after an SSE update, step named, "
        "and freezes ONLY on an ending (the observer ceiling reopens) OK"
    )


# ============================================================================
# [R1]-[R4] the terminal reason
# ============================================================================
def _plant_orphan(did, wid, hours_old=3.0, batch_id=None):
    with Session(engine) as s:
        run = WorkflowRun(
            workflow_id=wid,
            document_id=did,
            target_locale=TGT,
            status=RunStatus.RUNNING,
            batch_id=batch_id,
            created_by="planted",
            started_at=datetime.utcnow() - timedelta(hours=hours_old),
        )
        s.add(run)
        s.commit()
        s.refresh(run)
        return run.id


def test_r1_boot_reconcile_reason_on_every_surface():
    _pid, did, _n = _project_and_doc(segments=2)
    wid = _workflow()
    rid = _plant_orphan(did, wid)
    healed = orchestrator.recover_interrupted_runs()
    if healed < 1:
        _fail("the boot reconciler did not heal the planted orphan")
    run = _run_row(rid)
    if run.status != RunStatus.FAILED:
        _fail(f"the planted orphan should be FAILED, got {run.status!r}")
    reason = orchestrator.run_reason(run)
    if not reason or reason["code"] != orchestrator.REASON_RESTART:
        _fail(f"a boot-reconciled run must carry a NAMED reason, got {reason!r}")

    headers = _api_headers()
    client = TestClient(app)
    body = _api_run(client, headers, rid)
    if body.get("reason") != reason:
        _fail(f"/api/runs/{rid} carries no reason: {body.get('reason')!r}")
    if body.get("failure_shape") or body.get("step_states"):
        _fail("fixture assumption broken: this run should have no step evidence at all")

    rc, out = _cli_show_run(rid, as_json=False)
    if rc != 0 or orchestrator.REASON_RESTART not in out or "server restart" not in out:
        _fail(f"CLI show-run does not print the reason: {out[:400]}")
    settings.set_many({"security_auth_required": "0"})
    settings.invalidate_cache()
    print(f"  [R1] boot reconcile -> reason {reason['code']} on the DB, /api and the CLI OK")


def test_r2_batch_member_reconcile_reason():
    _pid, did, _n = _project_and_doc(segments=2)
    wid = _workflow()
    with Session(engine) as s:
        b = WorkflowBatch(
            workflow_id=wid,
            document_id=did,
            requested_locales=[TGT],
            status=RunStatus.RUNNING,
            created_by="planted",
        )
        s.add(b)
        s.commit()
        s.refresh(b)
        bid = b.id
    rid = _plant_orphan(did, wid, batch_id=bid)
    batch_runs.recover_interrupted_batches(at_boot=True)
    run = _run_row(rid)
    if run.status != RunStatus.FAILED:
        _fail(f"the batch healer should have failed the member, got {run.status!r}")
    reason = orchestrator.run_reason(run)
    if not reason or reason["code"] != orchestrator.REASON_RESTART:
        _fail(
            "a batch MEMBER reconciled at boot must carry its own reason (the batch row's "
            f"note is not readable from any run surface), got {reason!r}"
        )
    print("  [R2] batch-member reconcile -> reason {} on the member run OK".format(reason["code"]))


def test_r3_cancel_reason_rides_the_closed_event():
    _pid, did, _n = _project_and_doc(segments=2)
    wid = _workflow()
    rid = _plant_orphan(did, wid, hours_old=0.0)
    with Session(engine) as s:
        orchestrator.cancel_run(s, rid, actor="runobs")
    reason = orchestrator.run_reason(_run_row(rid))
    if not reason or reason["code"] != orchestrator.REASON_CANCELLED:
        _fail(f"a cancelled run must carry the cancel reason, got {reason!r}")
    closed = None
    for _run, ev in orchestrator.observe_run(rid, poll_interval=0.01, max_seconds=2.0):
        if ev["type"] == "closed":
            closed = ev
            break
    if not closed or closed.get("reason", {}).get("code") != orchestrator.REASON_CANCELLED:
        _fail(f"the `closed` SSE event carries no reason for a cancelled run: {closed!r}")
    print(f"  [R3] operator cancel -> reason {reason['code']}, carried on the closed event OK")


def test_r4_ordinary_run_has_no_reason():
    _pid, did, _n = _project_and_doc(segments=3)
    wid = _workflow()
    with Session(engine) as s:
        rid = orchestrator.start_run(s, wid, did, TGT)
    status = _await_terminal(rid)
    if status != RunStatus.COMPLETED:
        _fail(f"fixture run ended {status!r}, expected completed")
    if orchestrator.run_reason(_run_row(rid)) is not None:
        _fail("an ordinary completed run must carry NO reason (the happy path is unchanged)")
    # AND a COMPLETED run that carries an INFORMATIONAL note still reports none. run.notes
    # is shared: a tm_match pool that was not readable, a vendor dead-letter escalation.
    # Those already surface as `warnings`; a failure-shaped `reason` on a healthy run would
    # be a new lie, so `run_reason` is scoped to FAILED/CANCELLED.
    with Session(engine) as s:
        run = s.get(WorkflowRun, rid)
        orchestrator._stamp_note(run, "tm_match TM pool 7 not readable -- skipped")
        s.add(run)
        s.commit()
    noted = _run_row(rid)
    if not (noted.notes or "").strip():
        _fail("fixture assumption broken: the informational note was not stored")
    if orchestrator.run_reason(noted) is not None:
        _fail(
            "a COMPLETED run with an informational note must report NO reason: `reason` is "
            "why a run ENDED BADLY, and that note is already a `warnings` entry"
        )
    if run_overlay.format_reason_ascii(None) != "":
        _fail("the reason renderer must print nothing when there is no reason")
    print("  [R4] a completed run reports no reason, informational note or not OK")


def test_r5_an_informational_note_is_not_a_terminal_reason():
    """GATE F ROUND 1, M1. `run.notes` is a SHARED, APPENDING column: `orchestrator:6123`
    stamps a tm_match-pool sentence on it MID-RUN, and vendor_dispatch / vendor_rework stamp
    dead-letter escalations on it. The first cut fell through to an `other` code carrying the
    WHOLE column, so a run that failed on a dead engine rendered
    `Reason: tm_match TM pool 5 not readable -- skipped` next to a `failure_shape` naming a
    completely different cause -- on /api, `workflow show-run` (local and --remote), the SSE
    `closed` event and the run page. The docstring's justification ("those already surface as
    `warnings`") was also false: `run.context["warnings"]` only ever carries
    `fulltext_tm_match_noop`."""
    _pid, did, _n = _project_and_doc(segments=2)
    wid = _workflow()
    rid = _plant_running(did, wid, status=RunStatus.FAILED)
    info = "tm_match TM pool 5 not readable -- skipped"
    with Session(engine) as s:
        run = s.get(WorkflowRun, rid)
        orchestrator._stamp_note(run, info)
        s.add(run)
        s.commit()
    run = _run_row(rid)
    if (run.context or {}).get("warnings"):
        _fail("fixture assumption broken: the note must NOT have reached run.context warnings")
    if orchestrator.run_reason(run) is not None:
        _fail(
            "a FAILED run whose notes hold only an INFORMATIONAL sentence must report NO "
            f"reason; got {orchestrator.run_reason(run)!r} (M1)"
        )
    headers = _api_headers()
    client = TestClient(app)
    if _api_run(client, headers, rid).get("reason") is not None:
        _fail("/api published an unrelated informational note as the terminal reason (M1)")
    settings.set_many({"security_auth_required": "0"})
    settings.invalidate_cache()

    # Now the terminal reconciler speaks: the code is named AND the text starts at THAT
    # sentence rather than republishing the earlier informational one as the cause.
    with Session(engine) as s:
        run = s.get(WorkflowRun, rid)
        orchestrator._stamp_note(run, orchestrator.RESTART_NOTE)
        s.add(run)
        s.commit()
    reason = orchestrator.run_reason(_run_row(rid))
    if not reason or reason["code"] != orchestrator.REASON_RESTART:
        _fail(f"the terminal note was not recognised: {reason!r}")
    if reason["text"] != orchestrator.RESTART_NOTE:
        _fail(
            "the reason text must be the TERMINAL sentence, not the whole accumulated "
            f"notes column: {reason['text']!r}"
        )
    if info in reason["text"]:
        _fail("the informational note was republished inside the terminal reason (M1)")
    print("  [R5] an informational note is not a reason; a terminal note names only itself OK")


def test_r6_batch_member_crash_carries_a_reason():
    """GATE F ROUND 1, H2. `batch_runs._run_member`'s crash handler wrote FAILED and stamped
    NOTHING, unlike its sibling `orchestrator._run_in_background`. That is #106's own reported
    trigger: a per-segment `engines.get_profile` raising `sqlalchemy.exc.TimeoutError` under
    pool exhaustion (#100) is neither DeadEngineError nor UnderTranslationError, so it escapes
    `run_workflow_stream` and, for a BATCH member, lands exactly here -- leaving
    `{"status":"failed","reason":null,"failure_shape":[],"warnings":[]}`. The train had fixed
    only the boot RECONCILE of members."""
    from sqlalchemy.exc import OperationalError

    from app.db import engine as _eng

    _pid, did, _n = _project_and_doc(segments=2)
    wid = _workflow()
    real = orchestrator.run_workflow_stream

    # (a) a plain crash -> the bounded crash note.
    rid = _plant_running(did, wid)

    def boom(*a, **kw):
        raise RuntimeError("pool timeout while resolving engine:2")
        yield  # pragma: no cover - generator shape

    set_pieces("app.services.orchestrator", "run_workflow_stream", boom)
    try:
        batch_runs._run_member(_eng, rid, wid, did, TGT)
    finally:
        set_pieces("app.services.orchestrator", "run_workflow_stream", real)
    run = _run_row(rid)
    if run.status != RunStatus.FAILED:
        _fail(f"a crashed batch member must be FAILED, got {run.status!r}")
    reason = orchestrator.run_reason(run)
    if not reason or reason["code"] != orchestrator.REASON_CRASH:
        _fail(f"a CRASHED batch member carries no reason: {reason!r} (H2)")
    if "RuntimeError" not in reason["text"]:
        _fail(f"the crash reason does not name the failure: {reason['text']!r}")
    if rid in orchestrator._progress_state:
        _fail("the crashed batch member leaked live progress state (L1)")

    # (b) the SQLite writer-lock crash -> the distinct, retryable contention note, exactly
    # as the sibling handler in orchestrator._run_in_background chooses it.
    rid2 = _plant_running(did, wid)

    def locked(*a, **kw):
        raise OperationalError("UPDATE workflowrun", {}, Exception("database is locked"))
        yield  # pragma: no cover - generator shape

    set_pieces("app.services.orchestrator", "run_workflow_stream", locked)
    try:
        batch_runs._run_member(_eng, rid2, wid, did, TGT)
    finally:
        set_pieces("app.services.orchestrator", "run_workflow_stream", real)
    reason2 = orchestrator.run_reason(_run_row(rid2))
    if not reason2 or reason2["code"] != orchestrator.REASON_WRITE_CONTENTION:
        _fail(f"a write-contention crash on a batch member is not named: {reason2!r} (H2)")

    # (c) a CANCELLED member is never resurrected to FAILED by the crash handler.
    rid3 = _plant_running(did, wid, status=RunStatus.CANCELLED)
    set_pieces("app.services.orchestrator", "run_workflow_stream", boom)
    try:
        batch_runs._run_member(_eng, rid3, wid, did, TGT)
    finally:
        set_pieces("app.services.orchestrator", "run_workflow_stream", real)
    if _run_row(rid3).status != RunStatus.CANCELLED:
        _fail("the batch-member crash handler resurrected a CANCELLED run to FAILED")
    print(
        "  [R6] batch-member CRASH -> {} / {} named, cancelled member untouched OK".format(
            reason["code"], reason2["code"]
        )
    )


def test_r6b_sync_arm_crash_carries_a_reason():
    """#337 N-1. The SYNCHRONOUS `run_workflow` arm (local CLI / vendor) pre-creates its
    own row under _RUN_CREATE_LOCK, and its crash handler marked that row FAILED with no
    note -- unlike the siblings `orchestrator._run_in_background` and
    `batch_runs._run_member`, which is #106's reasonless shape on the one-shot arm. It
    now stamps the same note the siblings choose: the retryable write-contention sentence
    or the bounded crash text, and the error still escapes to the caller. The mark-FAILED
    write itself rides a bookkeeping session + the bounded writer-lock retry, so a rival
    writer holding the lock past one busy_timeout window cannot leave the row RUNNING."""
    from sqlalchemy.exc import OperationalError

    real = orchestrator.run_workflow_stream

    def _crash_arm(did, wid, exc, on_boom=None):
        """Crash the stream of a fresh sync-arm launch; return (raised, the run row)."""

        def boom(*a, **kw):
            if on_boom is not None:
                on_boom()
            raise exc
            yield  # pragma: no cover - generator shape

        set_pieces("app.services.orchestrator", "run_workflow_stream", boom)
        try:
            raised = None
            with Session(engine) as s:
                try:
                    orchestrator.run_workflow(s, wid, did, TGT)
                except Exception as got:  # the point is that it escapes to the caller
                    raised = got
            with Session(engine) as s:
                row = s.exec(
                    select(WorkflowRun)
                    .where(WorkflowRun.document_id == did, WorkflowRun.workflow_id == wid)
                    .order_by(WorkflowRun.id.desc())
                ).first()
            return raised, row
        finally:
            set_pieces("app.services.orchestrator", "run_workflow_stream", real)

    # (a) a plain crash -> the bounded crash note, and the error still reaches the caller.
    _pid, did, _n = _project_and_doc(segments=2)
    wid = _workflow()
    raised, row = _crash_arm(did, wid, RuntimeError("sync arm exploded mid-step"))
    if not isinstance(raised, RuntimeError):
        _fail(f"the sync-arm crash must still escape to the caller, got {raised!r}")
    if row is None or row.status != RunStatus.FAILED:
        _fail("a crashed sync-arm run must be marked FAILED on its pre-created row")
    reason = orchestrator.run_reason(row)
    if not reason or reason["code"] != orchestrator.REASON_CRASH:
        _fail(f"a CRASHED sync-arm run carries no reason: {reason!r} (N-1)")
    if "RuntimeError" not in reason["text"]:
        _fail(f"the sync-arm crash reason does not name the failure: {reason['text']!r}")

    # (b) the SQLite writer-lock crash -> the distinct, retryable contention note, exactly
    # as the sibling handlers choose it.
    _pid2, did2, _n2 = _project_and_doc(segments=2)
    wid2 = _workflow()
    raised2, row2 = _crash_arm(
        did2, wid2, OperationalError("UPDATE workflowrun", {}, Exception("database is locked"))
    )
    if not isinstance(raised2, OperationalError):
        _fail(f"the sync-arm crash must still escape to the caller, got {raised2!r}")
    if row2 is None or row2.status != RunStatus.FAILED:
        _fail("a crashed sync-arm run must be marked FAILED on its pre-created row")
    reason2 = orchestrator.run_reason(row2)
    if not reason2 or reason2["code"] != orchestrator.REASON_WRITE_CONTENTION:
        _fail(f"a write-contention crash on the sync arm is not named: {reason2!r} (N-1)")

    # (c) the mark-FAILED write must RIDE OUT a rival writer that holds the lock past one
    # busy_timeout window (#337 reviewer round): the first fix committed on the caller's
    # own session with no retry, so on a lock-caused crash IT was the next lock victim,
    # logged "could not mark sync-arm run ... failed", and left the row RUNNING with no
    # reason (the pre-#337 shape). The bookkeeping session + commit_with_retry picks the
    # write up on a later attempt once the rival releases. The rival grabs the lock
    # INSIDE the simulated crash, so the contention is deterministic: the handler's own
    # write is what fights for it.
    import sqlite3

    _pid3, did3, _n3 = _project_and_doc(segments=2)
    wid3 = _workflow()
    # isolation_level=None (autocommit) so pysqlite never manages the transaction on
    # our behalf: the explicit BEGIN IMMEDIATE / ROLLBACK are the whole lock story.
    # check_same_thread=False: the crash (main thread) grabs, the timer thread releases.
    lock_con = sqlite3.connect(
        str(engine.url.database), timeout=10, isolation_level=None, check_same_thread=False
    )
    released = threading.Event()

    def _grab_lock():
        lock_con.execute("BEGIN IMMEDIATE")
        threading.Thread(
            target=lambda: (
                time.sleep(6.5),  # longer than one 5s busy_timeout window
                lock_con.execute("ROLLBACK"),
                lock_con.close(),
                released.set(),  # set only after the lock is provably down
            ),
            daemon=True,
        ).start()

    raised3, row3 = _crash_arm(
        did3,
        wid3,
        OperationalError("UPDATE workflowrun", {}, Exception("database is locked")),
        on_boom=_grab_lock,
    )
    if not released.wait(30):
        _fail("the rival writer never released the lock; the test environment is wedged")
    try:  # noqa: SIM105
        lock_con.close()
    except Exception:  # closing a mid-transaction connection can raise
        pass
    if not isinstance(raised3, OperationalError):
        _fail(f"the sync-arm crash must still escape to the caller, got {raised3!r}")
    if row3 is None or row3.status != RunStatus.FAILED:
        _fail(
            "a crashed sync-arm run must be marked FAILED even while a rival writer holds "
            "the lock past one busy window: status={!r} (N-1)".format(getattr(row3, "status", None))
        )
    reason3 = orchestrator.run_reason(row3)
    if not reason3 or reason3["code"] != orchestrator.REASON_WRITE_CONTENTION:
        _fail(f"a write-contention crash under a live rival is not named: {reason3!r} (N-1)")
    print(
        "  [R6b] sync-arm CRASH -> {} / {} / {}-under-live-rival named, error still "
        "escapes to the caller OK".format(reason["code"], reason2["code"], reason3["code"])
    )


def test_r7_no_terminal_write_leaves_the_row_silent():
    """GATE F ROUND 1, H2's sweep. `_empty_document_run` marked the run FAILED and yielded
    an `error` event -- fine live, but the event is gone by the time anyone reads the run
    back, and that path writes no StepRun either, so `_failure_reason` fell through to empty
    notes and the persisted run said `failed` and nothing else. It is not a reconciler
    sentence, so `run_reason` correctly still reports None: what it must NOT be is silent."""
    with Session(engine) as s:
        cust = Customer(name="RunObs Empty %d" % _seq())  # noqa: UP031
        s.add(cust)
        s.commit()
        s.refresh(cust)
        proj = Project(
            name="RunObs Empty %d" % _seq(),  # noqa: UP031
            source_locale=SRC,
            default_provider="mock",
            customer_id=cust.id,
            project_type=ProjectType.DOCUMENT,
        )
        s.add(proj)
        s.commit()
        s.refresh(proj)
        doc = Document(project_id=proj.id, filename=f"empty-{_seq()}.txt", mode=TransMode.SEGMENTED)
        s.add(doc)
        s.commit()
        s.refresh(doc)
        did = doc.id
    wid = _workflow()
    with Session(engine) as s:
        summary = orchestrator.run_workflow(s, wid, did, TGT)
    if summary.get("status") != "failed" or summary.get("error") != orchestrator.EMPTY_DOCUMENT_MSG:
        _fail(f"fixture assumption broken: {summary!r}")
    run = _run_row(summary["run_id"])
    if orchestrator.EMPTY_DOCUMENT_MSG not in (run.notes or ""):
        _fail(
            "the empty-document guard left the run FAILED with an EMPTY row: read back "
            "later, `_failure_reason` has nothing to report (H2 sweep)"
        )
    if orchestrator.run_reason(run) is not None:
        _fail(
            "this is not a RECONCILER sentence and must not be published as the terminal "
            f"`reason`: {orchestrator.run_reason(run)!r}"
        )
    print("  [R7] the empty-document terminal write no longer leaves a silent row OK")


#: THE SUFFIX CONTRACT (gate F round 2 M2, widened in round 3 L2). A sentence a terminal
#: write stamps on `run.notes` must be a module-level constant named `*_NOTE` or `*_MSG` --
#: those two suffixes are what [R8]'s discovery walk can see, and anything else ships
#: unratcheted (i.e. can silently return that path to `reason: null`, #106 reinstated). New
#: terminal writes should use `_NOTE`; `_MSG` is discovered too because the module already
#: contains one such sentence stamped on a terminal row (`EMPTY_DOCUMENT_MSG`), which proves
#: the `_NOTE` convention is not universal.
#:
#: This set names the discovered constants that are deliberately NOT terminal-reconciler
#: sentences, with the reason each is exempt. Anything else ending in `_NOTE`/`_MSG` must
#: appear in [R8]'s expected map, so a NEW reconciler sentence cannot ship without a reason
#: code. The opt-out cannot be used to SILENCE a live code: dropping a note from `expected`
#: leaves its `REASON_*` produced by nothing, which the completeness half fails on.
_NOT_A_TERMINAL_REASON = {
    # Stamped MID-RUN by the output-hygiene pass on a run that then goes on to COMPLETE.
    # It describes a repair, not an ending, and `run_reason` is scoped to FAILED/CANCELLED
    # anyway -- see [R4].
    "HYGIENE_NOTE",
    # Stamped by the empty-document guard on a run it then FAILS, so it IS a terminal write
    # -- but deliberately not a reconciler VERDICT: that run failed for a cause the ordinary
    # error surfaces state (orchestrator:1134-1141), and `run_reason` correctly reports None
    # for it. [R7] is the cell that pins that path's row-level honesty.
    "EMPTY_DOCUMENT_MSG",
}


def test_r8_every_terminal_note_resolves_to_a_reason():
    """GATE F ROUND 2, M2: THE RATCHET UNDER THE CLOSED ALLOWLIST.

    `_reason_patterns()` is a closed allowlist keyed on hand-clipped SENTENCE FRAGMENTS
    (`WRITE_CONTENTION_NOTE[:40]`, the literal "Superseded: no activity"), and an unmatched
    note now correctly returns None. Those two facts together are a trap: reword any
    reconciler constant -- a one-word edit a maintainer would make freely -- or add a
    SEVENTH reconciler sentence, and that path silently drops back to `reason: null`, which
    is GitHub #106 reinstated, with every suite green. Before this cell `grep -rln
    run_reason tests/` returned one file and `STALE_NOTE` / `SUPERSEDED_ABORT_NOTE` appeared
    in it zero times: two of the six codes shipped with no coverage at all.

    So this walks the module's own constants at RUNTIME rather than restating their text:

      1. DISCOVERY -- every `*_NOTE` / `*_MSG` string constant in `orchestrator` is either
         expected below or in the commented `_NOT_A_TERMINAL_REASON` opt-out. A new one
         fails here. (Round 3, L2: `_MSG` is in the walk because the module already carries
         `EMPTY_DOCUMENT_MSG`, a sentence stamped on a run that is then FAILED, so the
         `_NOTE` convention was demonstrably not universal. The suffix contract is stated
         on `_NOT_A_TERMINAL_REASON`.)
      2. RESOLUTION -- each note, stamped on a REAL failed run row, resolves to its code,
         and `text` is that sentence (not the accumulated column).
      3. COMPLETENESS -- every `REASON_*` code the module exports is produced by one of
         those RESOLUTIONS, so a code whose allowlist fragment no longer matches anything
         fails.
      4. DISTINCTNESS + PRECEDENCE -- each sentence matches exactly ONE allowlist fragment
         (otherwise tuple order, not the sentence, picks its code), the codes actually
         returned are pairwise distinct, and the allowlist's declared
         SUPERSEDED_ABORT_NOTE-outranks-the-sweep precedence holds in BOTH stamping orders
         (round 3, L3/L4: it is the allowlist's order that decides, not last-write-wins).

    The batch-member paths (`batch_runs._run_member`'s crash handler and
    `recover_interrupted_batches`) stamp these SAME constants through `_orch`, so they are
    ratcheted here too; [R2] and [R6] prove the stamping itself."""
    _pid, did, _n = _project_and_doc(segments=2)
    wid = _workflow()

    # (1) DISCOVERY. Constant name -> (the sentence as a reconciler stamps it, the code).
    expected = {
        "SPEND_CAP_STOP_NOTE": (
            orchestrator.SPEND_CAP_STOP_NOTE,
            orchestrator.REASON_SPEND_CAP_STOP,
        ),
        "FAIL_FAST_STOP_NOTE": (
            orchestrator.FAIL_FAST_STOP_NOTE,
            orchestrator.REASON_FAIL_FAST_STOP,
        ),
        "QUEUE_LAUNCH_FAILED_NOTE": (
            orchestrator.QUEUE_LAUNCH_FAILED_NOTE % "seeded failure",
            orchestrator.REASON_QUEUE_LAUNCH_FAILED,
        ),
        "RESTART_NOTE": (orchestrator.RESTART_NOTE, orchestrator.REASON_RESTART),
        # Defined in app.db (the one shared sentence for pool exhaustion) and re-exported
        # by the orchestrator module so THIS walk can see it, the way it sees every other
        # constant the reconcilers stamp: the three crash handlers check pool timeout
        # first, and an unmapped pool note must fail HERE, not ship as `reason: null`.
        "POOL_EXHAUSTED_NOTE": (
            orchestrator.POOL_EXHAUSTED_NOTE,
            orchestrator.REASON_POOL_EXHAUSTED,
        ),
        # Carries the superseding run's id; the reconciler stamps `STALE_NOTE % run_id`.
        "STALE_NOTE": (orchestrator.STALE_NOTE % 4242, orchestrator.REASON_STALE),
        "CANCEL_NOTE": (orchestrator.CANCEL_NOTE, orchestrator.REASON_CANCELLED),
        "WRITE_CONTENTION_NOTE": (
            orchestrator.WRITE_CONTENTION_NOTE,
            orchestrator.REASON_WRITE_CONTENTION,
        ),
        "SUPERSEDED_ABORT_NOTE": (
            orchestrator.SUPERSEDED_ABORT_NOTE,
            orchestrator.REASON_SUPERSEDED_ABORT,
        ),
    }
    # ROUND 3, L2: discovery covers `_MSG` as well as `_NOTE`. The suffix is the whole
    # reach of this ratchet, and `EMPTY_DOCUMENT_MSG` -- a sentence stamped on a run that is
    # then FAILED -- proved `_NOTE` is not universal, so the next terminal sentence named
    # `*_MSG` would have shipped invisible to it. See `_NOT_A_TERMINAL_REASON` for the
    # contract this states.
    found = {
        name
        for name in dir(orchestrator)
        if (name.endswith("_NOTE") or name.endswith("_MSG"))
        and isinstance(getattr(orchestrator, name), str)
    }
    unclassified = found - set(expected) - _NOT_A_TERMINAL_REASON
    if unclassified:
        _fail(
            f"orchestrator grew note constant(s) {sorted(unclassified)} that this ratchet "
            "does not classify. If a terminal reconciler stamps it, add it to `expected` "
            "AND to `_reason_patterns()` (otherwise `run_reason` returns None for that "
            "path = #106 reinstated); if it is informational, or a terminal write that "
            "deliberately publishes no verdict, add it to `_NOT_A_TERMINAL_REASON` with "
            "the reason (M2)"
        )
    missing = set(expected) - found
    if missing:
        _fail(f"orchestrator no longer defines {sorted(missing)}: this ratchet is stale (M2)")
    # `_crash_note` is a FUNCTION, not a constant, so discovery cannot see it; it is the
    # catch-all for a run that died outside the step machinery and is pinned by hand.
    expected["_crash_note()"] = (
        orchestrator._crash_note(RuntimeError("engine:99 is disabled")),
        orchestrator.REASON_CRASH,
    )

    # (2) RESOLUTION, against real rows. `resolved` records what `run_reason` ACTUALLY
    # returned for each note (round 3, L3: steps 3 and 4 read this, not the fixture map).
    resolved = {}
    for name, (text, code) in sorted(expected.items()):
        rid = _plant_running(did, wid, status=RunStatus.FAILED)
        with Session(engine) as s:
            run = s.get(WorkflowRun, rid)
            orchestrator._stamp_note(run, text)
            s.add(run)
            s.commit()
        reason = orchestrator.run_reason(_run_row(rid))
        if reason is None:
            _fail(
                f"{name} resolves to NO reason code. Its sentence and the closed allowlist "
                "in `_reason_patterns()` have drifted apart, so every run ended by that "
                "reconciler reports `reason: null` on /api, the CLI, SSE and the run page "
                "-- GitHub #106, reinstated silently (M2)"
            )
        if reason["code"] != code:
            _fail(f"{name} resolved to {reason['code']!r}, expected {code!r} (M2)")
        if reason["text"] != text.strip():
            _fail(f"{name}'s reason text is not its own sentence: {reason['text']!r} (M2)")
        resolved[name] = reason["code"]

    # (3) COMPLETENESS: no exported code is unreachable from a real note.
    exported = {
        name: getattr(orchestrator, name)
        for name in dir(orchestrator)
        if name.startswith("REASON_") and isinstance(getattr(orchestrator, name), str)
    }
    produced = set(resolved.values())
    orphan_codes = {n: c for n, c in exported.items() if c not in produced}
    if orphan_codes:
        _fail(
            f"reason code(s) {sorted(orphan_codes)} are exported but no reconciler note "
            "resolves to them: either the allowlist fragment no longer matches its "
            "sentence, or the code is dead (M2)"
        )

    # (4) DISTINCTNESS -- of the ALLOWLIST, not of this cell's own map (round 3, L3; the
    # first cut compared `len(expected.values())` against a set built from those same
    # values, which is true of any dict with distinct values and says nothing about
    # `_reason_patterns()`). Two properties, both read off the real allowlist:
    #   (a) each note sentence matches EXACTLY ONE fragment. A fragment that is also a
    #       substring of another reconciler's sentence would leave tuple ORDER, not
    #       specificity, deciding that sentence's code -- so a later reorder, or a reword
    #       of the fragment that "loses", silently changes what a monitor branches on.
    #   (b) the codes `run_reason` actually returned for the notes are pairwise distinct,
    #       so two live reconcilers cannot collapse onto one code.
    # ROUND 4, LOW-3: this compares FRAGMENTS, which is what the property above says. The
    # round-3 cut collected `code`s, so two DIFFERENT fragments that both matched one
    # sentence and shared a code gave `len(hits) == 1` and passed -- benign (when the codes
    # agree, tuple order cannot change the answer) but weaker than the comment claimed, and
    # a later split of one code across two fragments would have landed unseen.
    for name, (text, _code) in sorted(expected.items()):
        hits = {frag for frag, _c in orchestrator._reason_patterns() if frag and frag in text}
        if len(hits) != 1:
            _fail(
                f"{name}'s sentence matches {len(hits)} allowlist fragments {sorted(hits)}: "
                "with more than one fragment inside a single sentence the code is decided "
                "by tuple ORDER in `_reason_patterns()` rather than by the sentence, so a "
                "reorder silently changes it (round 3, L3; fragments not codes, round 4 "
                "LOW-3)"
            )
    if len(produced) != len(resolved):
        collisions = sorted(n for n in resolved if list(resolved.values()).count(resolved[n]) > 1)
        _fail(
            f"the {len(resolved)} terminal notes collapse onto {len(produced)} codes "
            f"({collisions} share one), so those reconcilers are indistinguishable to every "
            "consumer that branches on `reason.code` (M2)"
        )
    # ... and the PRECEDENCE the allowlist declares.
    rid = _plant_running(did, wid, status=RunStatus.FAILED)
    with Session(engine) as s:
        run = s.get(WorkflowRun, rid)
        orchestrator._stamp_note(run, orchestrator.STALE_NOTE % 4243)
        orchestrator._stamp_note(run, orchestrator.SUPERSEDED_ABORT_NOTE)
        s.add(run)
        s.commit()
    reason = orchestrator.run_reason(_run_row(rid))
    if not reason or reason["code"] != orchestrator.REASON_SUPERSEDED_ABORT:
        _fail(
            "a run carrying BOTH the sweep's sentence and its own executor's "
            "superseded-abort must report the code the ALLOWLIST'S DECLARED PRECEDENCE "
            "gives it (`superseded_by_recovery`, first in `_reason_patterns()` of the two); "
            f"got {reason!r} (gate F round 1 L5 / round 2 M2)"
        )
    # ROUND 3, L4: and the mechanism really is the allowlist's order, NOT last-write-wins on
    # `notes` -- `run_reason` scans `_reason_patterns()` in order and takes the first
    # fragment present. Stamping the same two sentences in the OPPOSITE order must give the
    # same code; pinned here so nobody reads the pin above as "the last note wins" and adds
    # a note assuming that.
    rid = _plant_running(did, wid, status=RunStatus.FAILED)
    with Session(engine) as s:
        run = s.get(WorkflowRun, rid)
        orchestrator._stamp_note(run, orchestrator.SUPERSEDED_ABORT_NOTE)
        orchestrator._stamp_note(run, orchestrator.STALE_NOTE % 4244)
        s.add(run)
        s.commit()
    reason_rev = orchestrator.run_reason(_run_row(rid))
    if not reason_rev or reason_rev["code"] != orchestrator.REASON_SUPERSEDED_ABORT:
        _fail(
            "the terminal reason changed when the SAME two notes were stamped in the "
            f"opposite order ({reason_rev!r}): the code must come from the allowlist's "
            "declared precedence, not from the order sentences landed in `notes` (L4)"
        )
    print(
        f"  [R8] all {len(expected)} terminal reconciler notes resolve to distinct codes, "
        "no code is orphaned, precedence holds OK"
    )


def test_r9_pool_exhausted_crash_is_named_on_every_handler():
    """GITHUB #350. A connection-pool checkout timeout is one of the crash handlers' own
    classes (GitHub #100), and every one of the three handlers must end the row FAILED
    carrying the SAME note set in the SAME order: only `db.POOL_EXHAUSTED_NOTE`, no
    generic `Run failed:` crash note stacked on top, resolving to `pool_exhausted` on
    every surface. Before the fix the two plain-run handlers (the sync `run_workflow`
    arm, `orchestrator._run_in_background`) stamped the pool note but the allowlist had
    no entry for it, so the run ended `failed` with `reason: null`; the batch member
    stamped the crash note AND the pool note, so it reported `run_crashed` instead."""
    from sqlalchemy.exc import TimeoutError as _SATimeoutError

    from app.db import POOL_EXHAUSTED_NOTE

    def _pool_timeout():
        return _SATimeoutError(
            "QueuePool limit of size 5 overflow 10 reached, "
            "connection timed out after 30.00 seconds"
        )

    def _boom(*a, **kw):
        raise _pool_timeout()
        yield  # pragma: no cover - generator shape

    def _assert_pool_reason(rid, where):
        run = _run_row(rid)
        if run is None or run.status != RunStatus.FAILED:
            _fail(f"a pool-exhausted crash on {where} must end FAILED, got {run!r}")
        reason = orchestrator.run_reason(run)
        if not reason or reason["code"] != orchestrator.REASON_POOL_EXHAUSTED:
            _fail(
                f"a pool-exhausted crash on {where} does not resolve to "
                f"`pool_exhausted`, got {reason!r} (#350)"
            )
        if (run.notes or "") != POOL_EXHAUSTED_NOTE:
            _fail(
                f"the handlers must stamp the SAME note set in the SAME order on {where}: "
                f"expected only {POOL_EXHAUSTED_NOTE!r}, got {run.notes!r} (#350)"
            )
        if "Run failed: " in (run.notes or ""):
            _fail(f"a crash note was stacked on the pool note on {where} (#350)")
        return reason

    real = orchestrator.run_workflow_stream

    # (a) the batch member.
    _pid, did, _n = _project_and_doc(segments=2)
    wid = _workflow()
    rid = _plant_running(did, wid)
    set_pieces("app.services.orchestrator", "run_workflow_stream", _boom)
    try:
        batch_runs._run_member(engine, rid, wid, did, TGT)
    finally:
        set_pieces("app.services.orchestrator", "run_workflow_stream", real)
    reason = _assert_pool_reason(rid, "a batch member")

    # (b) the synchronous `run_workflow` arm: the crash still escapes to the caller.
    _pid2, did2, _n2 = _project_and_doc(segments=2)
    wid2 = _workflow()
    set_pieces("app.services.orchestrator", "run_workflow_stream", _boom)
    try:
        raised = None
        with Session(engine) as s:
            try:
                orchestrator.run_workflow(s, wid2, did2, TGT)
            except Exception as got:
                raised = got
    finally:
        set_pieces("app.services.orchestrator", "run_workflow_stream", real)
    if not isinstance(raised, _SATimeoutError):
        _fail(f"the sync-arm pool-exhausted crash must still escape to the caller, got {raised!r}")
    with Session(engine) as s:
        row = s.exec(
            select(WorkflowRun)
            .where(WorkflowRun.document_id == did2, WorkflowRun.workflow_id == wid2)
            .order_by(WorkflowRun.id.desc())
        ).first()
    if row is None:
        _fail("the sync arm left no WorkflowRun row to mark FAILED (#350)")
    _assert_pool_reason(row.id, "the sync run_workflow arm")

    # (c) `orchestrator._run_in_background`, the async arm.
    _pid3, did3, _n3 = _project_and_doc(segments=2)
    wid3 = _workflow()
    rid3 = _plant_running(did3, wid3)
    set_pieces("app.services.orchestrator", "run_workflow_stream", _boom)
    try:
        orchestrator._run_in_background(wid3, did3, TGT, run_id=rid3)
    finally:
        set_pieces("app.services.orchestrator", "run_workflow_stream", real)
    status = _await_terminal(rid3, timeout=60)
    if status != RunStatus.FAILED:
        _fail(f"a pool-exhausted crash in _run_in_background must end FAILED, got {status!r}")
    _assert_pool_reason(rid3, "the _run_in_background arm")

    print(
        f"  [R9] pool-exhausted crash -> {reason['code']} with the one shared note set, "
        "on the batch member, the sync arm and the async arm OK"
    )


def test_r9b_pool_note_selection_is_one_shared_and_narrow():
    """GITHUB #350 review fix. The three crash handlers share ONE note selection
    (`orchestrator._crash_terminal_note`): a pool timeout -- bare, or the `__cause__` of a
    wrapper -- gives the pool note; a lost writer lock gives the contention note; every
    unrelated error (including a builtin TimeoutError and an OperationalError that is not a
    QueuePool limit) keeps the bounded crash note and resolves to `run_crashed`. The pool
    note also outranks a benign step error in `_failure_reason`, like the contention note."""
    from sqlalchemy.exc import OperationalError
    from sqlalchemy.exc import TimeoutError as _SATimeoutError

    from app.db import POOL_EXHAUSTED_NOTE

    pool = _SATimeoutError("QueuePool limit of size 5 overflow 10 reached, timed out")
    wrapped = RuntimeError("provider lookup failed")
    wrapped.__cause__ = pool
    pool_op = OperationalError("SELECT 1", {}, Exception("QueuePool limit of size 5 reached"))
    cases = {
        "bare pool timeout": (pool, POOL_EXHAUSTED_NOTE),
        "pool timeout as __cause__": (wrapped, POOL_EXHAUSTED_NOTE),
        "OperationalError naming a QueuePool limit": (pool_op, POOL_EXHAUSTED_NOTE),
        "writer lock": (
            OperationalError("INSERT", {}, Exception("database is locked")),
            orchestrator.WRITE_CONTENTION_NOTE,
        ),
        "builtin TimeoutError": (TimeoutError("read timed out"), None),
        "unrelated OperationalError": (
            OperationalError("SELECT", {}, Exception("no such table: foo")),
            None,
        ),
        "plain RuntimeError": (RuntimeError("boom"), None),
    }
    for label, (exc, want) in cases.items():
        got = orchestrator._crash_terminal_note(exc)
        if want is None:
            want = orchestrator._crash_note(exc)
        if got != want:
            _fail(f"_crash_terminal_note({label}) = {got!r}, expected {want!r} (#350)")
    # unrelated errors resolve to run_crashed, never pool_exhausted.
    _pid, did, _n = _project_and_doc(segments=2)
    wid = _workflow()
    for label in ("builtin TimeoutError", "unrelated OperationalError", "plain RuntimeError"):
        exc = cases[label][0]
        rid = _plant_running(did, wid, status=RunStatus.FAILED)
        with Session(engine) as s:
            run = s.get(WorkflowRun, rid)
            orchestrator._stamp_note(run, orchestrator._crash_terminal_note(exc))
            s.add(run)
            s.commit()
        reason = orchestrator.run_reason(_run_row(rid))
        if not reason or reason["code"] != orchestrator.REASON_CRASH:
            _fail(f"{label} must resolve to run_crashed, got {reason!r} (#350)")
    # _failure_reason: the pool note outranks a benign step error.
    rid = _plant_running(did, wid, status=RunStatus.FAILED)
    with Session(engine) as s:
        run = s.get(WorkflowRun, rid)
        orchestrator._stamp_note(run, POOL_EXHAUSTED_NOTE)
        s.add(run)
        s.commit()
    run = _run_row(rid)

    class _SR:
        error = "LQA safety floor: informational"

    text = orchestrator._failure_reason(run, [_SR()])
    if text != POOL_EXHAUSTED_NOTE[:500]:
        _fail(f"the pool note must outrank a benign step error in _failure_reason, got {text!r}")
    # The fourth handler, `_mark_queue_launch_failed`, detects a pool timeout by the SAME
    # rule (bare or wrapped via __cause__); its fallback stays queue_launch_failed.
    for label, exc, want_code in (
        ("bare pool timeout", pool, orchestrator.REASON_POOL_EXHAUSTED),
        ("pool timeout as __cause__", wrapped, orchestrator.REASON_POOL_EXHAUSTED),
        (
            "builtin TimeoutError",
            TimeoutError("read timed out"),
            orchestrator.REASON_QUEUE_LAUNCH_FAILED,
        ),
    ):
        rid = _plant_running(did, wid, status=RunStatus.QUEUED)
        orchestrator._mark_queue_launch_failed(rid, exc)
        row = _run_row(rid)
        reason = orchestrator.run_reason(row)
        if row.status != RunStatus.FAILED or not reason or reason["code"] != want_code:
            _fail(
                f"queue-launch failure on {label} must end FAILED with {want_code}, "
                f"got {row.status!r} / {reason!r} (#350 gate)"
            )
    print("  [R9b] one shared crash-note selection: pool (bare/wrapped) / lock / crash OK")


def _await_terminal(rid, timeout=180):
    deadline = time.time() + timeout
    while time.time() < deadline:
        run = _run_row(rid)
        if run and run.status in (RunStatus.COMPLETED, RunStatus.FAILED, RunStatus.CANCELLED):
            return run.status
        time.sleep(0.2)
    _fail(f"run {rid} never reached a terminal state")


def main():
    init_db()
    settings.set_many({"security_auth_required": "0"})
    settings.invalidate_cache()
    rid, nseg = test_p1_progress_moves_inside_the_step()
    test_p2_final_progress_on_every_surface(rid, nseg)
    test_p3_no_progress_is_null_not_zero()
    test_p4_sse_forwards_progress_on_change()
    test_p5_flush_never_waits_on_an_exhausted_pool()
    test_p6_failed_step_counts_only_units_that_landed()
    test_p7_no_executor_leaks_ticker_state()
    test_p8_run_page_keeps_the_liveness_line()
    test_r1_boot_reconcile_reason_on_every_surface()
    test_r2_batch_member_reconcile_reason()
    test_r3_cancel_reason_rides_the_closed_event()
    test_r4_ordinary_run_has_no_reason()
    test_r5_an_informational_note_is_not_a_terminal_reason()
    test_r6_batch_member_crash_carries_a_reason()
    test_r6b_sync_arm_crash_carries_a_reason()
    test_r7_no_terminal_write_leaves_the_row_silent()
    test_r8_every_terminal_note_resolves_to_a_reason()
    test_r9_pool_exhausted_crash_is_named_on_every_handler()
    test_r9b_pool_note_selection_is_one_shared_and_narrow()
    print("verify_run_observability OK")


if __name__ == "__main__":
    main()
