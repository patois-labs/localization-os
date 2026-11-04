"""S2 step hand-off (ADR 0092): a run gives its slot back between step visits and the fair
scheduler continues it, WITHOUT changing what the run does.

Covers app/services/run_segments.py (RunSegment, PARKED, STATS), the park/continue seams in
orchestrator._AdmissionScheduler, batch_runs_hub (_run_member, run_queued_member) and run_resume
(park/adopt).

GROUPS
  [A] BYTE-IDENTITY UNDER FORCED HAND-OFF. For each workflow shape the same document is run
      twice, once with config.STEP_HANDOFF="off" (the pre-S2 behaviour: one thread, one slot) and
      once with "always" (park and continue at EVERY step boundary). The final segment targets,
      the StepRun sequence (step order, status, output text, error, outcome index), the run
      status, step index, notes and visit counters must be identical, and
      run_segments.STATS["parks"] must have grown in the "always" pass (so the proof is not
      vacuous). Shapes: linear 3 AI steps; a gated correction loop whose backward branch edge is
      taken twice; a parallel check group; a human review pause then resume; a visit-cap stop.
  [B] RESPONSIVENESS. With the run cap small and every queue slot held by a long multi-step run
      of customer A, a short run of customer B queued behind them finishes before the long runs do
      when hand-off is on (auto, quantum 0; a run parks only for ANOTHER customer's work),
      and (the contrast) waits for a whole long run when hand-off is off.
  [C] A PARKED RUN STAYS LIVE: _run_thread_alive is True, the live sweep does not re-queue it
      even with a backdated heartbeat, the parked holder keeps its heartbeat beating, the row
      stays RUNNING with its owner.
  [D] CANCEL WHILE PARKED ends CANCELLED (not FAILED, not COMPLETED) and returns the slot.
  [E] RunSegment UNIT CHECKS with a fake generator: context variables and the launching actor
      cross the thread hand-off; a crash after a park calls on_crash once and releases the slot
      once per segment; a Session with pending changes is never parked.
  [F] BATCH JOINS: a synchronous (CLI) batch never parks, even in "always" mode (the same batch
      through the daemon does).

  [G] FLOOD. Two customers whose 3-language documents arrive continuously through slow
      AI steps (3 AI steps + human review, every step slower than the quantum, a small run cap):
      every run reaches review within a hard bound (no starvation), no run parks right before the
      review (a recorded `_park` at the last boundary fails it), and a document's languages reach
      review within about one step of each other.

Hermetic and offline (throwaway LOC_TMS_DATA_DIR, deterministic mock providers). ASCII output.
Final line: "STEP QUEUE OK".
"""

import contextvars
import faulthandler
import os
import re
import sys
import threading
import time
from datetime import UTC, datetime, timedelta

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(_ROOT)
sys.path.insert(0, _ROOT)
from tests._bootstrap import init_env

os.environ["LOC_TMS_MAX_CONCURRENT_RUNS"] = "6"
init_env("loctms_stepq_")

from sqlalchemy import text
from sqlmodel import Session, select

from app import config
from app.db import engine, init_db

init_db()
from app.models import (
    Customer,
    Document,
    Project,
    RunStatus,
    Segment,
    SegStatus,
    StepRun,
    StepType,
    TransMode,
    Workflow,
    WorkflowRun,
    WorkflowStep,
)
from app.services import (
    batch_runs,
    keys_common,
    lqa,
    orchestrator,
    orchestrator_part4,
    orchestrator_part6,
    providers_hub,
    run_resume,
    run_segments,
)
from app.services import keys as keys_svc

ADMISSION = orchestrator.ADMISSION
RUN_SLOTS = orchestrator.RUN_SLOTS
TERMINAL = (RunStatus.COMPLETED, RunStatus.FAILED, RunStatus.CANCELLED, RunStatus.PAUSED)

_SEQ = [0]


def seq():
    _SEQ[0] += 1
    return _SEQ[0]


def utcnow():
    """Naive UTC now (what the run rows store), without the utcnow() deprecation warning."""
    return datetime.now(UTC).replace(tzinfo=None)


def fail(msg):
    print("FAIL:", msg)
    sys.exit(1)


def wait_until(pred, timeout, every=0.03):
    deadline = time.time() + timeout
    while time.time() < deadline:
        v = pred()
        if v:
            return v
        time.sleep(every)
    return None


def set_mode(mode, quantum=None):
    config.STEP_HANDOFF = mode
    if quantum is not None:
        config.STEP_QUANTUM_S = quantum


# ---------------------------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------------------------
SOURCES = [
    "The battery indicator shows the remaining charge.",
    "Press the power button twice.",
    "Keep the device away from water.",
]
LOCALE = "es-419"
REPAIRED = "El indicador de bateria muestra la carga restante."


def mk_customer(name="C"):
    with Session(engine) as s:
        c = Customer(name=f"{name}-{seq()}")
        s.add(c)
        s.commit()
        s.refresh(c)
        return c.id


def mk_project(name="SQ", locales=(LOCALE,), customer_id=None):
    with Session(engine) as s:
        p = Project(
            name=f"{name}-{seq()}",
            source_locale="en-US",
            target_locales=",".join(locales),
            customer_id=customer_id,
        )
        s.add(p)
        s.commit()
        s.refresh(p)
        return p.id


def mk_doc(pid, sources=None, locale=LOCALE, locales=None):
    with Session(engine) as s:
        d = Document(
            project_id=pid, filename="d.txt", fmt="txt", mode=TransMode.SEGMENTED, status="new"
        )
        s.add(d)
        s.commit()
        s.refresh(d)
        for loc in locales or [locale]:
            for i, src in enumerate(sources or SOURCES):
                s.add(
                    Segment(
                        document_id=d.id,
                        target_locale=loc,
                        order_index=i,
                        source_text=src,
                        target_text="",
                        status=SegStatus.NEW,
                    )
                )
        s.commit()
        return d.id


def mk_wf(steps_spec, name="W"):
    with Session(engine) as s:
        w = Workflow(
            name=f"{name}-{seq()}", scope="global", source_locale="en-US", target_locale=LOCALE
        )
        s.add(w)
        s.commit()
        s.refresh(w)
        for i, spec in enumerate(steps_spec):
            st = WorkflowStep(workflow_id=w.id, order_index=i, **spec)
            if st.step_type == StepType.LLM_AGENT and not st.prompt_text:
                st.prompt_text = "Translate {text} into {target_locale}."
            s.add(st)
        s.commit()
        return w.id


def launch(wid, did, locale=LOCALE):
    with Session(engine) as s:
        return orchestrator.start_run(s, wid, did, locale, created_by="sq@test")


def run_row(rid):
    with Session(engine) as s:
        return s.get(WorkflowRun, rid)


def settled(rid, states=TERMINAL, timeout=90):
    """The run reached one of `states` AND its execution is fully over (no thread, not parked)."""

    def pred():
        r = run_row(rid)
        if r is None or r.status not in states:
            return False
        return not orchestrator_part4._run_thread_alive(rid)

    if not wait_until(pred, timeout):
        r = run_row(rid)
        fail(
            f"run {rid} did not settle in {states}: status={r and r.status} "
            f"notes={r and r.notes!r} parked={run_segments.is_parked(rid)}"
        )
    return run_row(rid)


def quiesce(timeout=30):
    """Nothing parked, nothing in flight (so STATS / in_flight deltas mean something)."""
    ok = wait_until(lambda: not run_segments.parked() and RUN_SLOTS.in_flight == 0, timeout)
    if not ok:
        fail(
            f"engine not idle: parked={[s.run_id for s in run_segments.parked()]} "
            f"in_flight={RUN_SLOTS.in_flight}"
        )


_VOLATILE = re.compile(r"\d{4}-\d\d-\d\d[ T]\d\d:\d\d(:\d\d)?(\.\d+)?")


def snapshot(rid, wid, did, locale=LOCALE):
    """Everything a hand-off must not change, with ids / timestamps / durations / owner removed."""
    with Session(engine) as s:
        run = s.get(WorkflowRun, rid)
        order = {
            st.id: st.order_index
            for st in s.exec(select(WorkflowStep).where(WorkflowStep.workflow_id == wid))
        }
        srs = [
            (
                order.get(sr.step_id),
                str(sr.status),
                sr.output_text or "",
                sr.error or "",
                sr.outcome_index,
            )
            for sr in s.exec(
                select(StepRun).where(StepRun.workflow_run_id == rid).order_by(StepRun.id)
            )
        ]
        segs = [
            (g.order_index, g.target_text or "", str(g.status))
            for g in s.exec(
                select(Segment)
                .where(Segment.document_id == did, Segment.target_locale == locale)
                .order_by(Segment.order_index)
            )
        ]
        ctx = {
            k: run.context.get(k)
            for k in ("visits", "total_steps_run", "loop_ended", "gate_hold", "warnings")
            if isinstance(run.context, dict) and k in run.context
        }
        if "visits" in ctx:  # a keyless step is counted as "id:<row id>": row ids differ
            ctx["visits"] = {re.sub(r"^id:\d+$", "id:N", k): v for k, v in ctx["visits"].items()}
        notes = _VOLATILE.sub("<ts>", (run.notes or "")).replace(str(rid), "<rid>")
        return {
            "status": str(run.status),
            "step_index": run.current_step_index,
            "notes": notes,
            "context": ctx,
            "step_runs": srs,
            "segments": segs,
        }


def diff_snapshots(a, b):
    out = []
    for k in a:
        if a[k] != b[k]:
            out.append(f"{k}:\n    off   = {a[k]!r}\n    always= {b[k]!r}")
    return out


# ---------------------------------------------------------------------------------------------
# Deterministic providers
# ---------------------------------------------------------------------------------------------
class Distinct(providers_hub.MockProvider):
    """A different answer on every call (so a loop makes visible progress and the engine's
    no-progress guard stays out of the way). One instance per pass: both passes see the same
    call sequence."""

    name = "distinct"

    def __init__(self):
        super().__init__()
        self.calls = 0
        self._lock = threading.Lock()

    def complete(self, prompt, system=None, task=None, payload=None, images=None, **kw):
        with self._lock:
            self.calls += 1
            n = self.calls
        return f"Texto traducido numero {n}."


class Judge:
    """A stand-in for lqa.score_text: the first `fail_first` verdicts fail, the rest pass."""

    def __init__(self, fail_first):
        self.fail_first = fail_first
        self.calls = 0
        self._lock = threading.Lock()

    def __call__(
        self,
        source,
        target,
        source_locale,
        target_locale,
        rulebook="",
        provider_name=None,
        style_guide="",
        tap_meta=None,
        spec=None,
        glossary_terms=None,
        lqa_threshold=None,
    ):
        with self._lock:
            self.calls += 1
            ok = self.calls > self.fail_first
        return _card(ok)


class Slow(providers_hub.MockProvider):
    name = "slow"

    def __init__(self, delay):
        super().__init__()
        self.delay = delay
        self.log = []  # (monotonic time, marker)

    def complete(self, prompt, system=None, task=None, payload=None, images=None, **kw):
        time.sleep(self.delay)
        return super().complete(
            prompt, system=system, task=task, payload=payload, images=images, **kw
        )


# ---------------------------------------------------------------------------------------------
# [A] byte identity
# ---------------------------------------------------------------------------------------------
def llm(provider, key=None, **kw):
    d = {
        "name": kw.pop("name", f"AI {seq()}"),
        "step_type": StepType.LLM_AGENT,
        "provider": provider,
    }
    if key:
        d["key"] = key
    d.update(kw)
    return d


def shape_linear(tag):
    wid = mk_wf(
        [
            llm("mock", prompt_text="Pass one {text} into {target_locale}"),
            llm("mock", prompt_text="Pass two {text} into {target_locale}"),
            llm("mock", prompt_text="Pass three {text} into {target_locale}"),
        ],
        "lin",
    )
    return wid, mk_doc(mk_project()), None, None


def shape_loop(tag):
    # bad=3: translate (call 1), fix (2) -> lqa2 fails, fix (3) -> lqa2 fails, fix (4) repairs.
    # The backward edge lqa2 -> fix is taken TWICE.
    name = f"sq-loop-{tag}"
    prov = Distinct()
    judge = Judge(fail_first=3)
    providers_hub.register_provider(name, prov)
    wid = mk_wf(
        [
            llm(name, name="Translate"),
            {
                "name": "Initial check",
                "step_type": StepType.LQA,
                "key": "lqa1",
                "lqa_threshold": 90,
                "branch": {"on_pass": "end", "on_fail": "next"},
            },
            llm(
                name,
                key="fix",
                name="Post-Edit Fix",
                max_visits=5,
                run_condition={"ref": "prev_lqa", "op": "failed", "value": None},
                prompt_text="Fix {prior} into {target_locale}.",
            ),
            {
                "name": "Re-check",
                "step_type": StepType.LQA,
                "key": "lqa2",
                "lqa_threshold": 90,
                "branch": {"on_pass": "end", "on_fail": {"step": "fix"}},
            },
        ],
        "loop",
    )
    return wid, mk_doc(mk_project(), SOURCES[:1]), prov, judge


def shape_capped(tag):
    # The provider never repairs: the fix step hits its visit cap and the run stops.
    name = f"sq-cap-{tag}"
    prov = Distinct()
    judge = Judge(fail_first=10_000)
    providers_hub.register_provider(name, prov)
    wid = mk_wf(
        [
            llm(name, name="Translate"),
            {
                "name": "Check",
                "step_type": StepType.LQA,
                "key": "lqa1",
                "lqa_threshold": 90,
                "branch": {"on_pass": "end", "on_fail": "next"},
            },
            llm(
                name,
                key="fix",
                name="Fix",
                max_visits=2,
                run_condition={"ref": "prev_lqa", "op": "failed", "value": None},
                prompt_text="Fix {prior} into {target_locale}.",
            ),
            {
                "name": "Re-check",
                "step_type": StepType.LQA,
                "key": "lqa2",
                "lqa_threshold": 90,
                "branch": {"on_pass": "end", "on_fail": {"step": "fix"}},
            },
        ],
        "cap",
    )
    return wid, mk_doc(mk_project(), SOURCES[:1]), prov, judge


def _card(passed=True):
    return {
        "score": 100.0 if passed else 40.0,
        "passed": passed,
        "errors": [],
        "gate": "auto_approved" if passed else "needs_review",
        "cost_usd": 0.0,
        "tokens_in": 1,
        "tokens_out": 1,
    }


def _stub_score_text(
    source,
    target,
    source_locale,
    target_locale,
    rulebook="",
    provider_name=None,
    style_guide="",
    tap_meta=None,
    spec=None,
    glossary_terms=None,
    lqa_threshold=None,
):
    return _card(True)


def shape_parallel(tag):
    wid = mk_wf(
        [
            llm("mock", name="Translate", prompt_text="Translate {text} into {target_locale}"),
            {
                "name": "Check A",
                "step_type": StepType.LQA,
                "provider": "mock",
                "key": "pa",
                "parallel_group": "checks",
            },
            {
                "name": "Check B",
                "step_type": StepType.LQA,
                "provider": "mock",
                "key": "pb",
                "parallel_group": "checks",
            },
            llm("mock", name="Polish", prompt_text="Polish {text} into {target_locale}"),
        ],
        "par",
    )
    return wid, mk_doc(mk_project()), None, None


def shape_review(tag):
    wid = mk_wf(
        [
            llm("mock", prompt_text="Pass one {text} into {target_locale}"),
            llm("mock", prompt_text="Pass two {text} into {target_locale}"),
            {"name": "Review", "step_type": StepType.HUMAN_REVIEW, "assignee_email": "pm@sq.test"},
            llm("mock", name="After review", prompt_text="Final {text} into {target_locale}"),
        ],
        "rev",
    )
    return wid, mk_doc(mk_project()), None, None


SHAPES = [
    ("linear 3 AI steps", shape_linear, {"status": "completed"}),
    (
        "gated correction loop (backward edge x2)",
        shape_loop,
        {"status": "completed", "min_fix_visits": 3},
    ),
    ("parallel check group", shape_parallel, {"status": "completed", "stub_lqa": True}),
    ("human review pause + resume", shape_review, {"status": "completed", "resume": True}),
    ("visit-cap stop", shape_capped, {"status": None}),
]


def run_shape(builder, mode, tag, expect):
    """Run one shape under `mode`; returns (snapshot, parks_delta, extras)."""
    set_mode(mode, quantum=0.0)
    quiesce()
    wid, did, prov, judge = builder(f"{tag}-{mode}")
    parks0 = run_segments.STATS["parks"]
    if judge is not None:
        lqa.score_text = judge
    elif expect.get("stub_lqa"):
        lqa.score_text = _stub_score_text
    try:
        rid = launch(wid, did)
        r = settled(rid)
        if expect.get("resume"):
            assert r.status == RunStatus.PAUSED, (r.status, r.notes)
            paused_snap = snapshot(rid, wid, did)
            with Session(engine) as s:
                assert orchestrator.resume_run_async(s, rid, LOCALE, "") is True
            r = settled(rid, states=(RunStatus.COMPLETED, RunStatus.FAILED, RunStatus.CANCELLED))
        else:
            paused_snap = None
    finally:
        lqa.score_text = _REAL_SCORE_TEXT
    quiesce()
    snap = snapshot(rid, wid, did)
    snap["paused_snapshot"] = paused_snap
    extras = {
        "fix_visits": sum(1 for x in snap["step_runs"] if x[0] == 2),
        "provider_calls": getattr(prov, "calls", None),
        "judge_calls": getattr(judge, "calls", None),
    }
    return snap, run_segments.STATS["parks"] - parks0, extras


_REAL_SCORE_TEXT = lqa.score_text


def test_a_byte_identity():
    for label, builder, expect in SHAPES:
        off, parks_off, ex_off = run_shape(builder, "off", label, expect)
        alw, parks_alw, ex_alw = run_shape(builder, "always", label, expect)
        assert parks_off == 0, f"[{label}] mode off must never park (parks={parks_off})"
        assert parks_alw > 0, (
            f"[{label}] mode always never parked (STATS parks delta {parks_alw}): the identity "
            "proof would be vacuous"
        )
        d = diff_snapshots(off, alw)
        assert not d, f"[{label}] hand-off changed the run:\n  " + "\n  ".join(d)
        assert ex_off["provider_calls"] == ex_alw["provider_calls"], (label, ex_off, ex_alw)
        assert ex_off["judge_calls"] == ex_alw["judge_calls"], (label, ex_off, ex_alw)
        if expect.get("status"):
            assert off["status"] == expect["status"], f"[{label}] status {off['status']}: {off}"
        if expect.get("min_fix_visits"):
            assert ex_off["fix_visits"] >= expect["min_fix_visits"], (
                label,
                ex_off,
                off["step_runs"],
            )
        if expect.get("resume"):
            assert off["paused_snapshot"] is not None and alw["paused_snapshot"] is not None
            assert off["paused_snapshot"]["status"].endswith("paused"), off["paused_snapshot"]
        assert len(off["step_runs"]) >= 3, (label, off["step_runs"])
        print(
            f"  [A] {label}: identical ({len(off['step_runs'])} step runs, final "
            f"{off['status']}); parks off={parks_off} always={parks_alw}"
        )
    # the visit-cap shape really stopped on the cap (the loop ended, the gate holds the export)
    capped, _, ex = run_shape(shape_capped, "off", "cap-check", {})
    ctx = capped["context"]
    assert ctx.get("loop_ended") == ["fix"], ctx
    assert (ctx.get("gate_hold") or {}).get("cause") == "loop_exhausted", ctx
    assert ex["fix_visits"] == 2, (ex, capped["step_runs"])
    print(f"  [A] visit-cap shape: loop ended at the ceiling, fix visits={ex['fix_visits']}")


# ---------------------------------------------------------------------------------------------
# [B] responsiveness
# ---------------------------------------------------------------------------------------------
def responsiveness_pass(mode):
    config.MAX_CONCURRENT_RUNS = 3  # the queue keeps one slot for interactive launches: 2 for it
    set_mode(mode, quantum=0.0)
    quiesce()
    parks0, cont0 = run_segments.STATS["parks"], run_segments.STATS["continues"]
    slow_name = f"sq-slow-{mode}"
    providers_hub.register_provider(slow_name, Slow(0.25))
    long_wf = mk_wf([llm(slow_name) for _ in range(6)], "long")  # about 1.5 s of engine time
    short_wf = mk_wf([llm("mock", prompt_text="Short {text} into {target_locale}")], "short")
    members = {}
    # a run hands its slot back only for ANOTHER customer's waiting work (within one
    # customer, started work finishes first). So the long runs are customer A's and the short
    # run is customer B's.
    cust_a, cust_b = mk_customer("A"), mk_customer("B")
    with Session(engine) as s:
        for tag in ("long-1", "long-2"):
            did = mk_doc(mk_project(customer_id=cust_a), SOURCES[:1])
            batch = batch_runs.start_batch(
                s, workflow_id=long_wf, doc_id=did, locales=[LOCALE], created_by="t", max_parallel=1
            )
            members[tag] = batch_runs.batch_as_dict(s, batch)["members"][0]["run_id"]
    assert wait_until(
        lambda: all(run_row(r).status == RunStatus.RUNNING for r in members.values()), 30
    ), "both long runs should be running (holding both queue slots)"
    with Session(engine) as s:
        did = mk_doc(mk_project(customer_id=cust_b), SOURCES[:1])
        batch = batch_runs.start_batch(
            s, workflow_id=short_wf, doc_id=did, locales=[LOCALE], created_by="t", max_parallel=1
        )
        members["short"] = batch_runs.batch_as_dict(s, batch)["members"][0]["run_id"]
    t0 = time.monotonic()
    done = {}
    deadline = time.monotonic() + 60
    while len(done) < 3 and time.monotonic() < deadline:
        for tag, rid in members.items():
            if tag not in done and run_row(rid).status in TERMINAL:
                done[tag] = time.monotonic() - t0
        time.sleep(0.01)
    assert len(done) == 3, f"[{mode}] runs did not all finish: {done}"
    statuses = {t: str(run_row(r).status) for t, r in members.items()}
    assert all(v.endswith("completed") for v in statuses.values()), (mode, statuses)
    quiesce()
    config.MAX_CONCURRENT_RUNS = 6
    return {
        "short": done["short"],
        "first_long": min(done["long-1"], done["long-2"]),
        "parks": run_segments.STATS["parks"] - parks0,
        "continues": run_segments.STATS["continues"] - cont0,
    }


def test_b_responsiveness():
    off = responsiveness_pass("off")
    auto = responsiveness_pass("auto")
    print(
        f"  [B] off : short run done at {off['short']:.2f}s, first long run at "
        f"{off['first_long']:.2f}s (parks={off['parks']})"
    )
    print(
        f"  [B] auto: short run done at {auto['short']:.2f}s, first long run at "
        f"{auto['first_long']:.2f}s (parks={auto['parks']}, continues={auto['continues']})"
    )
    assert off["parks"] == 0, off
    # Without hand-off the short run waits behind a whole long run (the contrast).
    assert off["short"] >= off["first_long"], f"contrast failed: {off}"
    # With hand-off it gets a slot at a step boundary, long before any long run ends.
    assert auto["parks"] > 0 and auto["continues"] > 0, f"no hand-off happened: {auto}"
    assert auto["short"] < auto["first_long"], (
        f"the short run did not finish before the long runs under hand-off: {auto}"
    )
    assert auto["short"] < off["short"], (auto, off)


# ---------------------------------------------------------------------------------------------
# [C] / [D] a parked run
# ---------------------------------------------------------------------------------------------
class hold_parked:
    """Keep the admission daemon from continuing parked runs, so a test can inspect one."""

    def __enter__(self):
        ADMISSION._serves_parked = lambda batch_id, queue_group: False
        return self

    def __exit__(self, *exc):
        if "_serves_parked" in ADMISSION.__dict__:
            del ADMISSION._serves_parked
        ADMISSION.notify()


def park_one(label):
    set_mode("always", quantum=0.0)
    quiesce()
    wid, did, _p, _j = shape_linear(label)
    rid = launch(wid, did)
    assert wait_until(lambda: run_segments.is_parked(rid), 30), (
        f"run {rid} never parked: {run_row(rid).status}"
    )
    # the first thread has ended: the run is alive only because it is parked
    needle = "%s%d-" % (orchestrator.RUN_THREAD_PREFIX, rid)  # noqa: UP031
    assert wait_until(
        lambda: not any(t.name.startswith(needle) and t.is_alive() for t in threading.enumerate()),
        10,
    ), "the parked run's thread should have ended"
    return rid, wid, did


def test_c_parked_run_stays_live():
    with hold_parked():
        rid, _wid, _did = park_one("live")
        row = run_row(rid)
        owner = row.owner
        assert row.status == RunStatus.RUNNING and owner, (row.status, owner)
        assert RUN_SLOTS.in_flight == 0, f"a parked run holds no slot ({RUN_SLOTS.in_flight})"
        assert orchestrator_part4._run_thread_alive(rid) is True
        assert orchestrator._run_thread_alive(rid) is True
        # backdate the heartbeat past the staleness window: the sweep must still leave it alone
        stale = utcnow() - timedelta(minutes=10)
        with Session(engine) as s:
            s.execute(
                text("UPDATE workflowrun SET heartbeat_at = :t WHERE id = :id"),
                {"t": stale, "id": rid},
            )
            s.commit()
        hb = run_row(rid).heartbeat_at
        assert hb < utcnow() - run_resume.HEARTBEAT_STALE, "the heartbeat should be stale"
        n = run_resume.sweep_stale_runs()
        row = run_row(rid)
        assert n == 0, f"the live sweep re-queued {n} run(s) although the run is parked"
        assert row.status == RunStatus.RUNNING and row.owner == owner, (row.status, row.owner)
        assert not row.resume_count, row.resume_count
        assert run_segments.is_parked(rid), "the sweep must not have disturbed the parked run"
        # the parked holder keeps the heartbeat beating
        run_resume._beat_once()
        beat = run_row(rid).heartbeat_at
        assert beat > utcnow() - timedelta(seconds=60), f"parked run not beaten: {beat}"
        print(
            f"  [C] parked run {rid}: alive, no slot held, stale heartbeat not swept, "
            "parked holder re-beats it"
        )
    # released: it continues and completes
    r = settled(rid)
    assert r.status == RunStatus.COMPLETED, (r.status, r.notes)
    quiesce()


def test_d_cancel_while_parked():
    with hold_parked():
        rid, wid, did = park_one("cancel")
        base = RUN_SLOTS.in_flight
        assert base == 0, base
        with Session(engine) as s:
            out = orchestrator_part6.cancel_run(s, rid, actor="sq@test")
        assert out["status"] == RunStatus.CANCELLED, out
        assert run_row(rid).status == RunStatus.CANCELLED
    # the scheduler now continues the (cancelled) parked run, which must wind down
    r = settled(rid, states=(RunStatus.CANCELLED, RunStatus.FAILED, RunStatus.COMPLETED))
    quiesce()
    assert r.status == RunStatus.CANCELLED, (
        f"cancel while parked ended {r.status}, not cancelled: {r.notes!r}"
    )
    assert RUN_SLOTS.in_flight == base, f"slot leaked: {RUN_SLOTS.in_flight} (was {base})"
    assert not run_segments.is_parked(rid)
    snap = snapshot(rid, wid, did)
    done_steps = [x for x in snap["step_runs"] if x[1].endswith("completed")]
    print(
        f"  [D] cancelled while parked -> {r.status}; slot back to {RUN_SLOTS.in_flight}; "
        f"{len(done_steps)} of 3 steps had completed"
    )


# ---------------------------------------------------------------------------------------------
# [E] RunSegment unit checks (a fake generator)
# ---------------------------------------------------------------------------------------------
CV = contextvars.ContextVar("sq_cv", default="unset")
_PLANT = {}


def plant_run():
    if not _PLANT:
        _PLANT["wid"], _PLANT["did"], _p, _j = shape_linear("plant")
    with Session(engine) as s:
        row = WorkflowRun(
            workflow_id=_PLANT["wid"],
            document_id=_PLANT["did"],
            target_locale=LOCALE,
            status=RunStatus.RUNNING,
            started_at=utcnow(),
        )
        s.add(row)
        s.commit()
        s.refresh(row)
        return row.id


def end_plant(rid):
    with Session(engine) as s:
        row = s.get(WorkflowRun, rid)
        row.status = RunStatus.COMPLETED
        row.finished_at = utcnow()
        s.add(row)
        s.commit()


def fake_segment(make_stream, *, actor=None, on_crash=None, on_end=None):
    rid = plant_run()
    token = RUN_SLOTS.take()[0]
    name = f"sq-fake-{rid}"
    seg = run_segments.RunSegment(
        run_id=rid,
        thread_name=name,
        make_stream=make_stream,
        on_crash=on_crash or (lambda exc: None),
        on_end=on_end,
        slot=token,
        handoff=True,
    )
    first = []
    th = threading.Thread(target=lambda: first.append(seg.drive()), name=name)
    if actor:
        with keys_svc.actor_scope(actor):
            th.start()
    else:
        th.start()
    th.join(30)
    assert not th.is_alive(), "first segment hung"
    return rid, seg, token, first[0] if first else None


def continue_all(seg, limit=20):
    """Continue the segment by hand until its execution is over. Returns the threads used."""
    threads = []
    for _ in range(limit):
        if seg.done.is_set():
            break
        parked = run_segments.take_parked(seg.run_id)
        if parked is None:
            break
        token = RUN_SLOTS.take()[0]
        threads.append(parked.continue_on(token))
        threads[-1].join(30)
        assert not threads[-1].is_alive(), "continuation hung"
    return threads


def test_e_run_segment_units():
    set_mode("always", quantum=0.0)
    with hold_parked():
        quiesce()
        # --- E1: context variables and the actor cross the thread hand-off -------------------
        seen = []

        def stream_ctx(session, slot):
            CV.set("set-in-segment-1")
            seen.append(("1", threading.current_thread(), CV.get(), keys_common.current_actor()))
            yield {"type": "step", "index": 0}
            seen.append(("2", threading.current_thread(), CV.get(), keys_common.current_actor()))
            yield {"type": "step", "index": 1}
            seen.append(("3", threading.current_thread(), CV.get(), keys_common.current_actor()))
            yield {"type": "done"}

        rid, seg, tok1, first = fake_segment(stream_ctx, actor="alice@sq.test")
        assert first == "parked", first
        assert CV.get() == "unset", "the run's context must not leak into the launcher"
        assert seg.is_parked_now and run_segments.is_parked(rid)
        threads = continue_all(seg)
        assert seg.done.is_set() and len(threads) == 2, (seg.done.is_set(), len(threads))
        assert [x[0] for x in seen] == ["1", "2", "3"], seen
        assert len({id(x[1]) for x in seen}) == 3, "each segment should run on its own thread"
        assert all(x[2] == "set-in-segment-1" for x in seen), (
            f"a context variable set in segment 1 must be visible later: {seen}"
        )
        assert all(x[3] == "alice@sq.test" for x in seen), f"the actor must carry: {seen}"
        assert all(x[1].name == f"sq-fake-{rid}" for x in seen), [x[1].name for x in seen]
        assert seg.segments == 3 and not run_segments.is_parked(rid)
        end_plant(rid)
        print("  [E1] context variable and actor identical in all 3 segments on 3 threads")

        # --- E2: a crash after a park -------------------------------------------------------
        crashes, ends = [], []
        gives = []
        real_give_back = RUN_SLOTS._give_back

        def counting_give_back(*a, **kw):
            gives.append(1)
            return real_give_back(*a, **kw)

        def stream_crash(session, slot):
            yield {"type": "step", "index": 0}
            raise RuntimeError("boom after the park")

        base = RUN_SLOTS.in_flight
        RUN_SLOTS._give_back = counting_give_back
        try:
            rid, seg, tok1, first = fake_segment(
                stream_crash, on_crash=crashes.append, on_end=lambda: ends.append(1)
            )
            assert first == "parked" and tok1.released, (first, tok1.released)
            assert len(gives) == 1 and not crashes and not ends, (gives, crashes, ends)
            continue_all(seg)
        finally:
            del RUN_SLOTS._give_back
        assert len(crashes) == 1 and isinstance(crashes[0], RuntimeError), crashes
        assert len(ends) == 1, f"on_end should run once, ran {len(ends)}"
        assert len(gives) == 2, f"the slot must be released once per segment, got {len(gives)}"
        assert RUN_SLOTS.in_flight == base, (RUN_SLOTS.in_flight, base)
        assert seg.done.is_set() and not run_segments.is_parked(rid)
        end_plant(rid)
        print("  [E2] crash after a park: on_crash x1, on_end x1, slot released once per segment")

        # --- E3: a Session with pending changes is never parked ------------------------------
        marks = []

        def stream_pending(session, slot):
            session.add(Project(name=f"pending-{seq()}", source_locale="en-US"))
            marks.append(("new", threading.current_thread().name))
            yield {"type": "step", "index": 0}  # a new object is pending: must not park
            session.commit()
            proj = session.exec(select(Project).limit(1)).first()
            proj.name = proj.name + "!"
            session.add(proj)
            assert session.dirty or session.new
            marks.append(("dirty", threading.current_thread().name))
            yield {"type": "step_skipped", "index": 1}  # a modified object: must not park
            session.commit()
            marks.append(("clean", threading.current_thread().name))
            yield {"type": "step", "index": 2}  # clean: parks
            marks.append(("after", threading.current_thread().name))
            yield {"type": "done"}

        parks0 = run_segments.STATS["parks"]
        rid, seg, tok1, first = fake_segment(stream_pending)
        assert first == "parked", first
        assert [m[0] for m in marks] == ["new", "dirty", "clean"], (
            f"the segment should have run past both unflushed boundaries, then parked: {marks}"
        )
        assert len({m[1] for m in marks}) == 1
        assert run_segments.STATS["parks"] - parks0 == 1, "only the clean boundary may park"
        continue_all(seg)
        assert [m[0] for m in marks] == ["new", "dirty", "clean", "after"], marks
        assert seg.done.is_set()
        end_plant(rid)
        print("  [E3] pending new/dirty objects block a park; the clean boundary parks")
        quiesce()


# ---------------------------------------------------------------------------------------------
# [F] a synchronous batch never parks
# ---------------------------------------------------------------------------------------------
def test_f_sync_batch_never_parks():
    set_mode("always", quantum=0.0)
    quiesce()
    ADMISSION.ensure_started()
    assert ADMISSION.alive()
    locales = [LOCALE, "fr-FR"]
    pid = mk_project("sync", locales)
    did = mk_doc(pid, locales=locales)
    wid = mk_wf(
        [
            llm("mock", prompt_text=f"Sync pass {k} {{text}} into {{target_locale}}")
            for k in range(3)
        ],
        "sync",
    )
    # Contrast (so the zero below means something): the SAME batch through the daemon parks.
    parks0 = run_segments.STATS["parks"]
    with Session(engine) as s:
        batch = batch_runs.start_batch(
            s,
            workflow_id=wid,
            doc_id=mk_doc(pid, locales=locales),
            locales=locales,
            created_by="web@test",
            max_parallel=2,
        )
        daemon_ids = [m["run_id"] for m in batch_runs.batch_as_dict(s, batch)["members"]]
    for rid in daemon_ids:
        settled(rid)
    quiesce()
    assert run_segments.STATS["parks"] > parks0, "the daemon-run batch should park in 'always' mode"
    # A CLI process has no admission daemon draining the queue: keep this process's daemon from
    # stealing the batch's QUEUED rows, so the synchronous scheduler is what runs them. Patching
    # `drain_once` alone was not enough: a pass the daemon had ALREADY entered (woken by the last
    # daemon-run member's end, slow in finalize_ready_batches on a loaded box) still saw the new
    # rows, claimed one, parked it ("always" mode) and, patched, never continued it, so the
    # synchronous wait joined a parked member until the 600 s wrapper timeout (a rare hang,
    # stack-dumped 2026-10-24). The pick and both claims are instance attributes read on every
    # use, so a pass already running can neither pick nor claim the new rows either.
    parks0, cont0 = run_segments.STATS["parks"], run_segments.STATS["continues"]
    blocked = {
        "drain_once": lambda **kw: 0,
        "_fair_pick": lambda *a, **kw: None,
        "_admit": lambda *a, **kw: False,
        "_admit_gang": lambda *a, **kw: [],
    }
    for name, stub in blocked.items():
        setattr(ADMISSION, name, stub)
    try:
        with Session(engine) as s:
            batch = batch_runs.start_batch(
                s,
                workflow_id=wid,
                doc_id=did,
                locales=locales,
                created_by="cli@test",
                max_parallel=2,
                synchronous=True,
            )
            out = batch_runs.batch_as_dict(s, batch)
    finally:
        for name in blocked:
            delattr(ADMISSION, name)
    members = out["members"]
    assert len(members) == 2, members
    states = {m["locale"]: str(m["status"]) for m in members}
    assert all(v.endswith(("completed", "paused")) for v in states.values()), states
    assert run_segments.STATS["parks"] == parks0, (
        f"a synchronous batch parked {run_segments.STATS['parks'] - parks0} time(s)"
    )
    assert run_segments.STATS["continues"] == cont0
    assert not run_segments.parked()
    for m in members:
        n = len(snapshot(m["run_id"], wid, did, locale=m["locale"])["step_runs"])
        assert n == 3, (m, n)
    quiesce()
    print(f"  [F] synchronous batch finished {states} with 0 parks in 'always' mode")


# ---------------------------------------------------------------------------------------------
# [G] a continuous two-customer flood through slow AI steps (the shape of the real-LLM
#     flood of 2026-10-23, shrunk): every AI step is slower than the quantum, so a run is
#     eligible to park at every boundary. Three AI steps + human review, 3 languages per document.
# ---------------------------------------------------------------------------------------------
G_LOCALES = ["es-419", "fr-FR", "de-DE"]
G_DOCS_PER_CUSTOMER = 5
G_STEP_S = 0.6
G_QUANTUM_S = 0.4
G_ARRIVAL_S = 0.4
G_HARD_BOUND_S = 90.0


def test_g_flood_parks_only_for_other_customers_and_never_before_review():
    # PRE-FIX: quantum 0.4 s < every step, and `has_waiting` counted any queued row,
    # so the last boundary (before the human review) parked too: `bad_parks` below would be
    # non-empty (run_segments._should_park had no "next step is pause-only" test).
    config.MAX_CONCURRENT_RUNS = 4  # the queue keeps one slot for interactive launches: 3 for it
    set_mode("auto", quantum=G_QUANTUM_S)
    quiesce()
    parks0 = run_segments.STATS["parks"]
    slow_name = f"sq-flood-{seq()}"
    providers_hub.register_provider(slow_name, Slow(G_STEP_S))
    wid = mk_wf(
        [llm(slow_name, prompt_text="Pass {text} into {target_locale}") for _ in range(3)]
        + [{"name": "Review", "step_type": StepType.HUMAN_REVIEW, "assignee_email": "pm@sq.test"}],
        "flood",
    )
    review_index = 3
    cust = {"A": mk_customer("A"), "B": mk_customer("B")}
    proj = {k: mk_project(f"fl{k}", locales=G_LOCALES, customer_id=v) for k, v in cust.items()}

    parks = []  # (run id, the step index the run was about to start)
    park_log = {}  # run id -> [(seconds since the start, step index it was about to start)]
    t_start = time.monotonic()
    real_park = run_segments.RunSegment._park

    def recording_park(self):
        ok = real_park(self)
        if ok:
            with Session(engine) as s:
                row = s.get(WorkflowRun, self.run_id)
                parks.append((self.run_id, row.current_step_index if row else None))
                park_log.setdefault(self.run_id, []).append(
                    (round(time.monotonic() - t_start, 1), row.current_step_index if row else None)
                )
        return ok

    docs = {}  # doc id -> {"customer": ..., "runs": [run ids], "launched": monotonic}
    launched_all = threading.Event()

    def launcher():
        try:
            for _i in range(G_DOCS_PER_CUSTOMER):
                for k in ("A", "B"):  # the two customers' documents arrive interleaved
                    did = mk_doc(proj[k], SOURCES[:1], locales=G_LOCALES)
                    with Session(engine) as s:
                        batch = batch_runs.start_batch(
                            s,
                            workflow_id=wid,
                            doc_id=did,
                            locales=G_LOCALES,
                            created_by="t",
                            max_parallel=len(G_LOCALES),
                        )
                        runs = [m["run_id"] for m in batch_runs.batch_as_dict(s, batch)["members"]]
                    docs[did] = {"customer": k, "runs": runs, "launched": time.monotonic()}
                    time.sleep(G_ARRIVAL_S)
        finally:
            launched_all.set()

    run_segments.RunSegment._park = recording_park
    try:
        t0 = time.monotonic()
        threading.Thread(target=launcher, daemon=True).start()
        reached = {}  # run id -> monotonic time it was first seen paused at the review
        total = 2 * G_DOCS_PER_CUSTOMER * len(G_LOCALES)
        deadline = t0 + G_HARD_BOUND_S
        while time.monotonic() < deadline:
            all_runs = [r for d in list(docs.values()) for r in d["runs"]]
            for rid in all_runs:
                if rid in reached:
                    continue
                st = run_row(rid).status
                if st == RunStatus.PAUSED:
                    reached[rid] = time.monotonic()
                elif st in (RunStatus.FAILED, RunStatus.CANCELLED, RunStatus.COMPLETED):
                    fail(f"flood run {rid} ended {st} instead of pausing at its review")
            if launched_all.is_set() and len(reached) == total:
                break
            time.sleep(0.05)
        elapsed = time.monotonic() - t0
    finally:
        run_segments.RunSegment._park = real_park
    assert len(reached) == total, (
        f"starvation: only {len(reached)} of {total} runs reached review within "
        f"{G_HARD_BOUND_S:.0f}s; parked now: {[s.run_id for s in run_segments.parked()]}"
    )

    # 1. no park at the last boundary (the next step is the human review), and parking happened
    #    at the earlier boundaries (so the check is not vacuous)
    bad_parks = [p for p in parks if p[1] == review_index]
    assert not bad_parks, f"runs parked right before their human review: {bad_parks}"
    assert parks, "no run parked at all: the flood did not exercise the hand-off"
    assert all(idx in (1, 2) for _rid, idx in parks), parks
    assert run_segments.STATS["parks"] - parks0 == len(parks), (run_segments.STATS, len(parks))

    # 2. a document's languages reach review together: within about one step plus margin
    spreads = []
    for d in docs.values():
        ts = [reached[r] for r in d["runs"]]
        spreads.append(max(ts) - min(ts))
    limit = G_STEP_S + 1.0
    assert max(spreads) <= limit, (
        f"a document's languages reached review {max(spreads):.2f}s apart (> {limit:.1f}s); "
        f"all={[round(x, 2) for x in spreads]}\n"
        + "\n".join(
            f"  doc {did} ({d['customer']}) launched {d['launched'] - t_start:.1f}s: "
            + ", ".join(
                f"run {r} review {reached[r] - t_start:.1f}s parks {park_log.get(r)}"
                for r in d["runs"]
            )
            for did, d in docs.items()
        )
    )

    # 3. neither customer was left behind the other
    mean_wait = {
        k: sum(
            reached[r] - d["launched"]
            for d in docs.values()
            if d["customer"] == k
            for r in d["runs"]
        )
        / (G_DOCS_PER_CUSTOMER * len(G_LOCALES))
        for k in ("A", "B")
    }
    assert max(mean_wait.values()) <= 2.5 * min(mean_wait.values()), mean_wait

    # the engine did exactly the work asked of it, and every run stands at the review step
    with Session(engine) as s:
        for d in docs.values():
            for r in d["runs"]:
                row = s.get(WorkflowRun, r)
                assert row.status == RunStatus.PAUSED and row.current_step_index == review_index, (
                    r,
                    row.status,
                    row.current_step_index,
                )
    quiesce()
    config.MAX_CONCURRENT_RUNS = 6
    print(
        f"  [G] {total} runs reached review in {elapsed:.1f}s; parks={len(parks)} "
        f"(steps {sorted({i for _r, i in parks})}), spread max={max(spreads):.2f}s "
        f"(limit {limit:.1f}s), mean wait A={mean_wait['A']:.1f}s B={mean_wait['B']:.1f}s"
    )


TESTS = [
    test_a_byte_identity,
    test_b_responsiveness,
    test_c_parked_run_stays_live,
    test_d_cancel_while_parked,
    test_e_run_segment_units,
    test_f_sync_batch_never_parks,
    test_g_flood_parks_only_for_other_customers_and_never_before_review,
]


def main():
    # A hang must leave evidence: the wrapper keeps no output from a suite it kills at 600 s, so
    # dump every thread's stack and exit well before that (the suite takes about 30 s).
    faulthandler.dump_traceback_later(480, exit=True)
    for t in TESTS:
        print(f"[{t.__name__}]")
        t0 = time.time()
        t()
        print(f"  ({time.time() - t0:.1f}s)")
    print("STEP QUEUE OK")


if __name__ == "__main__":
    main()
