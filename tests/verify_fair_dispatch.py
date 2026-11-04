"""S2 fair dispatch (ADR 0092; promotes target T3, arch_targets/target_t3_fair_languages.py).

Under load our Spanish finished in 81 s, French in 174 s and German in 258 s because every
document's batch listed its languages in the same order and the admission scheduler rotated across
fan-outs one run at a time: it started every document's first language, then every second, then
every third. A customer waits on the LAST language, so the spread is what they feel. The
scheduler now orders waiting work by customer, then fan-out, then language (_fair_pick).

GROUPS
  [B] THE PICK ORDER, unit level: planted QUEUED rows and a private _AdmissionScheduler (no daemon).
      B1 two customers alternate; B2 a batch fan-out keeps the turn for exactly its max_parallel
      picks, then rotates (for two different widths); B3 plain fan-outs rotate every pick; B4 the
      language order is rotated from one new fan-out to the next, so each language goes first
      equally often; B5 the legacy contract that _next_queued rotates past _last_group still
      holds; B6 a fan-out in `skipped` is never returned; B7 a parked run goes before the new rows
      of its own fan-out and takes part in the customer rotation.
  [P] PARKING FIX, unit level (each fails on the pre-fix code, the comments
      say on which line): P1 no starvation under continuous arrivals; P2 fan-outs by numeric run
      id, not text; P3 a parked run past STEP_PARK_MAX_WAIT_S goes first; P4/P4b no park before a
      human review or the end, none for the same customer's own queue; P5 parked languages of one
      batch park and continue together; P6 a batch keeps its turn beside another customer.
  [G] BATCH ADMISSION (ADR 0099): G1 under contention a document's three languages
      start in one admission once three slots are free, not one per freed slot; G2 a batch
      wider than the queue starts in whole chunks, and a batch whose slots never free starts what
      it has after LOC_TMS_BATCH_GANG_WAIT_S (no deadlock), the rest once slots free (no
      starvation); G3 an interactive launch gets the reserve slot while a batch gathers slots;
      G4 a member another worker already claimed is skipped and the rest start together; G5 (end
      to end, after A) two customers' 3-language documents through slow-mock steps longer than
      the quantum on a queue of 5: every document's languages start together and reach review
      within one step-length; G6 a sibling's shared "park" never parks a run before its own
      review; G7 a batch's parked language waits for a sibling still finishing the step, within
      LOC_TMS_BATCH_GANG_WAIT_S; G8 meanwhile its customer's new work does not start.
  [A] THE T3 SCENARIO, end to end: 12 documents arriving 0.25 s apart, each fanned out to 3 locales
      with run-batch, every run making 3 engine calls through an engine that serves 3 at once, the
      global run cap 6. Per-document locale spread p95 <= 2 run-lengths + 0.3 s; the slowest
      locale's mean time to review within 25 % of the fastest; the last document's first locale not
      starved; every run paused at its human review step.

WHICH CHECKS FAIL ON THE OLD ROUND-ROBIN-BY-GROUP CODE (the pre-S2 _next_queued, which picked the
oldest row of the next fan-out after _last_group and was blind to customers and languages):
  B1  fails: groups sorted g1..g4 (customer A) before g5 (customer B) gave A A A A B B B B.
  B2  fails: the old code left a batch after one run (B P B P B B), never "B B P B B P".
  B3  passes on both (rotation across fan-outs is old behaviour kept on purpose).
  B4  fails: rows were taken FIFO by id, so the first language was the same for every fan-out.
  B5  passes on both (the legacy contract is kept).
  B6  passes on both (kept).
  B7  fails: the old scheduler had no notion of a parked run (no `continuations`).
  A   fails: the T3 flood spread (the measured 81/174/258 s shape): the language-mean and spread
      checks fail on the old code, which is what the target was written for.

Hermetic and offline (throwaway LOC_TMS_DATA_DIR, deterministic mock engine). ASCII output.
Final line: "FAIR DISPATCH OK".
"""

import inspect
import os
import secrets
import statistics
import sys
import threading
import time
from types import SimpleNamespace

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(_ROOT)
sys.path.insert(0, _ROOT)
from tests._bootstrap import init_env

os.environ["LOC_TMS_MAX_CONCURRENT_RUNS"] = "6"
init_env("loctms_fairdisp_")

from fastapi.testclient import TestClient
from sqlmodel import Session, select

from app import config
from app.db import engine, init_db

init_db()
from app.main import app
from app.models import (
    Customer,
    Document,
    Project,
    Role,
    RunStatus,
    StepType,
    Workflow,
    WorkflowBatch,
    WorkflowRun,
    WorkflowStep,
)
from app.services import apikeys, engines, orchestrator, providers_hub, run_segments, security

_SEQ = [0]


def seq():
    _SEQ[0] += 1
    return _SEQ[0]


def fail(msg):
    print("FAIL:", msg)
    sys.exit(1)


def wait_until(pred, timeout, every=0.05):
    deadline = time.time() + timeout
    while time.time() < deadline:
        v = pred()
        if v:
            return v
        time.sleep(every)
    return None


def pct(vals, q):
    vals = sorted(vals)
    return vals[min(len(vals) - 1, int(q * len(vals)))] if vals else None


# ---------------------------------------------------------------------------------------------
# [B] pick-order unit checks
# ---------------------------------------------------------------------------------------------
def mk_customer(name):
    with Session(engine) as s:
        c = Customer(name=f"{name}-{seq()}")
        s.add(c)
        s.commit()
        s.refresh(c)
        return c.id


def mk_world(customer_id):
    """(workflow id, document id) for a project of `customer_id`."""
    with Session(engine) as s:
        p = Project(
            name=f"fd-{seq()}",
            source_locale="en-US",
            target_locales="es-ES",
            customer_id=customer_id,
        )
        s.add(p)
        s.commit()
        s.refresh(p)
        d = Document(project_id=p.id, filename="d.txt", fmt="txt", status="new")
        s.add(d)
        w = Workflow(name=f"fdw-{seq()}", scope="global")
        s.add(w)
        s.commit()
        s.refresh(d)
        s.refresh(w)
        return w.id, d.id


def mk_batch(wid, did, width):
    with Session(engine) as s:
        b = WorkflowBatch(workflow_id=wid, document_id=did, max_parallel=width)
        s.add(b)
        s.commit()
        s.refresh(b)
        return b.id


class Plant:
    """Planted QUEUED rows, removed afterwards (so the daemon never sees them)."""

    def __init__(self):
        self.runs = []
        self.batches = []

    def add(self, wid, did, group, locale, batch_id=None):
        with Session(engine) as s:
            r = WorkflowRun(
                workflow_id=wid,
                document_id=did,
                target_locale=locale,
                queue_group=group,
                batch_id=batch_id,
                status=RunStatus.QUEUED,
            )
            s.add(r)
            s.commit()
            s.refresh(r)
            self.runs.append(r.id)
            return r.id

    def close(self):
        with Session(engine) as s:
            for rid in self.runs:
                row = s.get(WorkflowRun, rid)
                if row is not None:
                    s.delete(row)
            s.commit()
            for bid in self.batches:
                row = s.get(WorkflowBatch, bid)
                if row is not None:
                    s.delete(row)
            s.commit()


def drain(sched, skipped=(), limit=100):
    """Serve every QUEUED row in the scheduler's own fair order. Returns [(kind, run id, group,
    customer, locale)]. A served row leaves the queue (it is deleted by Plant.close later)."""
    out = []
    skipped = set(skipped)
    for _ in range(limit):
        picked = sched._fair_pick(skipped)
        if picked is None:
            break
        kind, rid, group, customer = picked
        sched._note_served(group, customer)
        with Session(engine) as s:
            row = s.get(WorkflowRun, rid)
            locale = row.target_locale
            row.status = RunStatus.CANCELLED  # out of the queue
            s.add(row)
            s.commit()
        out.append((kind, rid, group, customer, locale))
    return out


def test_b1_customers_alternate():
    plant = Plant()
    try:
        ca, cb = mk_customer("A"), mk_customer("B")
        wa, da = mk_world(ca)
        wb, db = mk_world(cb)
        for g in ("g1", "g2", "g3", "g4"):  # customer A: four fan-outs of one run each
            plant.add(wa, da, g, "es-ES")
        for _ in range(4):  # customer B: one fan-out of four runs
            plant.add(wb, db, "g5", "es-ES")
        order = drain(orchestrator._AdmissionScheduler())
        custs = [o[3] for o in order]
        assert len(order) == 8, order
        assert all(custs[i] != custs[i + 1] for i in range(7)), (
            f"two customers with waiting work must alternate; served customers: {custs}"
        )
        assert custs.count(str(ca)) == 4 and custs.count(str(cb)) == 4, custs
        shape = "".join("A" if c == str(ca) else "B" for c in custs)
        print(f"  [B1] two customers alternate: {shape}")
    finally:
        plant.close()


def test_b1b_same_customer_fanouts_interleave_with_other_customers():
    # Gate S2 H1: with a SECOND customer waiting, customer A's two multi-run fan-outs must still
    # take turns. A single process-wide group cursor (the other customer's group) sent A back to
    # its first fan-out every time, so "a-zsmall" waited for "a-wide" to drain completely.
    plant = Plant()
    try:
        ca, cb = mk_customer("A"), mk_customer("B")
        wa, da = mk_world(ca)
        wb, db = mk_world(cb)
        for _ in range(6):
            plant.add(wa, da, "a-wide", "es-ES")
        for _ in range(2):
            plant.add(wa, da, "a-zsmall", "es-ES")
        for _ in range(8):
            plant.add(wb, db, "b-only", "es-ES")
        order = drain(orchestrator._AdmissionScheduler())
        a_groups = [o[2] for o in order if o[3] == str(ca)]
        assert len(a_groups) == 8, a_groups
        # "a-zsmall" sorts AFTER "a-wide" on purpose: the old cursor wrapped to the first-sorting
        # fan-out after every other-customer pick, so it is the later one it starved (index 6).
        first_small = a_groups.index("a-zsmall")
        assert first_small <= 1, (
            f"customer A's second fan-out must get a turn right away, not after the first "
            f"drains; A's served fan-outs: {a_groups}"
        )
        assert a_groups[:4] == ["a-wide", "a-zsmall", "a-wide", "a-zsmall"], a_groups
        print(f"  [B1b] same-customer fan-outs interleave beside another customer: {a_groups[:4]}")
    finally:
        plant.close()


def _batch_vs_plain(width, batch_rows, plain_rows):
    plant = Plant()
    try:
        c = mk_customer("C")
        w, d = mk_world(c)
        bid = mk_batch(w, d, width)
        plant.batches.append(bid)
        # the group names sort "batch:<id>" before "plain-1"
        for loc in ("es-ES", "fr-FR", "de-DE", "it-IT", "pt-BR", "ja-JP")[:batch_rows]:
            plant.add(w, d, f"batch:{bid}", loc, batch_id=bid)
        for _ in range(plain_rows):
            plant.add(w, d, "plain-1", "es-ES")
        order = drain(orchestrator._AdmissionScheduler())
        return "".join("B" if o[2].startswith("batch:") else "P" for o in order)
    finally:
        plant.close()


def test_b2_batch_keeps_its_turn():
    # width 2: the batch takes exactly two runs, then the plain fan-out one, and so on
    seq2 = _batch_vs_plain(width=2, batch_rows=4, plain_rows=2)
    assert seq2 == "BBPBBP", f"width 2 batch should hold the turn for 2 picks: {seq2}"
    # width 3: three, then rotate
    seq3 = _batch_vs_plain(width=3, batch_rows=6, plain_rows=2)
    assert seq3 == "BBBPBBBP", f"width 3 batch should hold the turn for 3 picks: {seq3}"
    print(f"  [B2] a batch keeps its turn for exactly max_parallel picks: {seq2} (2), {seq3} (3)")


def test_b3_plain_groups_rotate_every_pick():
    plant = Plant()
    try:
        c = mk_customer("C")
        w, d = mk_world(c)
        for g in ("k-1", "k-2", "k-3"):
            for _ in range(2):
                plant.add(w, d, g, "es-ES")
        order = drain(orchestrator._AdmissionScheduler())
        groups = [o[2] for o in order]
        assert groups == ["k-1", "k-2", "k-3", "k-1", "k-2", "k-3"], groups
        # a run with no queue_group is its own fan-out and rotates the same way
        print(f"  [B3] non-batch fan-outs rotate every pick: {groups}")
    finally:
        plant.close()


def test_b4_language_order_rotates_across_groups():
    plant = Plant()
    try:
        c = mk_customer("C")
        w, d = mk_world(c)
        batch_ids = []
        for _ in range(3):
            bid = mk_batch(w, d, 3)
            plant.batches.append(bid)
            batch_ids.append(bid)
            # every document lists its languages in the SAME (unsorted) order
            for loc in ("es-ES", "fr-FR", "de-DE"):
                plant.add(w, d, f"batch:{bid}", loc, batch_id=bid)
        order = drain(orchestrator._AdmissionScheduler())
        assert len(order) == 9, order
        per_group = {}
        for _kind, _rid, grp, _c, loc in order:
            per_group.setdefault(grp, []).append(loc)
        firsts = [per_group[f"batch:{b}"][0] for b in batch_ids]
        assert len(set(firsts)) == 3, (
            f"the first language must differ between consecutive new fan-outs: {per_group}"
        )
        for b in batch_ids:
            assert sorted(per_group[f"batch:{b}"]) == ["de-DE", "es-ES", "fr-FR"], per_group
        print(f"  [B4] first language per new fan-out: {firsts}; orders {list(per_group.values())}")
    finally:
        plant.close()


def test_b5_next_queued_rotates_past_last_group():
    plant = Plant()
    try:
        c = mk_customer("C")
        w, d = mk_world(c)
        for _ in range(3):
            plant.add(w, d, "wide-group", "es-ES")
        plant.add(w, d, "later-group", "es-ES")
        sched = orchestrator._AdmissionScheduler()
        sched._last_group = "wide-group"
        assert sched._next_queued()[1] == "later-group", "must rotate past the last served group"
        sched._last_group = "later-group"
        assert sched._next_queued()[1] == "wide-group", "must wrap to the first group"
        sched2 = orchestrator._AdmissionScheduler()
        picked = sched2._next_queued()
        assert isinstance(picked, tuple) and len(picked) == 2 and isinstance(picked[0], int), picked
        print("  [B5] _next_queued keeps its (run id, group) shape and rotates past _last_group")
    finally:
        plant.close()


def test_b6_skipped_groups_are_never_returned():
    plant = Plant()
    try:
        c = mk_customer("C")
        w, d = mk_world(c)
        for g in ("s1", "s2", "s3"):
            for _ in range(2):
                plant.add(w, d, g, "es-ES")
        sched = orchestrator._AdmissionScheduler()
        for _ in range(10):
            p = sched._fair_pick({"s1"})
            assert p is not None and p[2] != "s1", p
            sched._note_served(p[2], p[3])
        assert sched._next_queued({"s1", "s2"})[1] == "s3"
        assert sched._next_queued({"s1", "s2", "s3"}) is None
        assert sched._fair_pick({"s1", "s2", "s3"}) is None
        order = drain(orchestrator._AdmissionScheduler(), skipped={"s2"})
        assert {o[2] for o in order} == {"s1", "s3"}, order
        print("  [B6] a skipped fan-out is never picked; all-skipped -> None")
    finally:
        plant.close()


def test_b7_parked_run_goes_first_in_its_group():
    plant = Plant()
    fake = None
    try:
        ca, cb = mk_customer("A"), mk_customer("B")
        wa, da = mk_world(ca)
        wb, db = mk_world(cb)
        for _ in range(2):
            plant.add(wa, da, "pg", "es-ES")
        plant.add(wb, db, "qg", "es-ES")
        # the pick reads `parked_at` (the wait cap and the "parked earliest" order) and
        # a real parked segment carries a `cursor`; the fake needs both.
        fake = SimpleNamespace(
            run_id=987654,
            group="pg",
            customer=str(ca),
            batch_id=None,
            parked_at=time.monotonic(),
            cursor=0,
        )
        with run_segments._lock:
            run_segments.PARKED[987654] = fake
        sched = orchestrator._AdmissionScheduler()
        order = []
        for _ in range(4):
            p = sched._fair_pick(set(), continuations=True)
            assert p is not None
            kind, rid, grp, cust = p
            sched._note_served(grp, cust)
            order.append((kind, grp))
            if kind == "parked":
                with run_segments._lock:
                    run_segments.PARKED.pop(rid, None)
            else:
                with Session(engine) as s:
                    row = s.get(WorkflowRun, rid)
                    row.status = RunStatus.CANCELLED
                    s.add(row)
                    s.commit()
        pg_kinds = [k for k, g in order if g == "pg"]
        assert pg_kinds == ["parked", "run", "run"], (
            f"the parked run comes before its fan-out's new rows: {order}"
        )
        assert order[0][1] != order[1][1], f"customers still alternate: {order}"
        print(f"  [B7] parked run first in its fan-out, customers still alternate: {order}")
    finally:
        with run_segments._lock:
            run_segments.PARKED.pop(987654, None)
        plant.close()


# ---------------------------------------------------------------------------------------------
# [P] parking fix: starvation, numeric order, wait cap, no park before review, siblings
# (real-LLM flood 2026-10-23). Every check
# here fails on the pre-fix code; the reason is stated per check.
# ---------------------------------------------------------------------------------------------
def _park_fake(run_id, group, customer, *, age=0.0, cursor=0):
    """A parked-run stand-in with the attributes the pick reads. `age` is how long it has waited."""
    fake = SimpleNamespace(
        run_id=run_id,
        group=group,
        customer=str(customer),
        batch_id=None,
        parked_at=time.monotonic() - age,
        cursor=cursor,
    )
    with run_segments._lock:
        run_segments.PARKED[run_id] = fake
    return fake


def _unpark_all(ids):
    with run_segments._lock:
        for i in ids:
            run_segments.PARKED.pop(i, None)


def _serve_one(sched):
    """One continuation-aware pick, served the way the daemon does. Returns (kind, id, group,
    customer) or None."""
    p = sched._fair_pick(set(), continuations=True)
    if p is None:
        return None
    kind, rid, grp, cust = p
    sched._note_served(grp, cust)
    if kind == "parked":
        with run_segments._lock:
            run_segments.PARKED.pop(rid, None)
    else:
        with Session(engine) as s:
            row = s.get(WorkflowRun, rid)
            row.status = RunStatus.CANCELLED
            s.add(row)
            s.commit()
    return p


def test_p1_no_starvation_under_continuous_arrivals():
    # PRE-FIX: `_fair_pick` rotated fan-outs with `next(g for g in groups if g > last_g)` (text
    # compare). A new fan-out with a greater key arrives before every pick, so the cursor never
    # wraps: the parked run ("a-parked", sorts before the cursor) and f-0001's remaining runs
    # (behind the cursor) are never picked. Both asserts below fail on that line.
    plant = Plant()
    ids = [987701]
    try:
        c = mk_customer("S")
        w, d = mk_world(c)
        n = [0]

        def arrive(runs=1):
            n[0] += 1
            for _ in range(runs):
                plant.add(w, d, f"f-{n[0]:04d}", "es-ES")

        arrive(3)  # f-0001: three runs, the OLDEST fan-out
        arrive(1)  # f-0002
        sched = orchestrator._AdmissionScheduler()
        for _ in range(2):  # the cursor moves past f-0001 and f-0002
            arrive()
            assert _serve_one(sched) is not None
        # the run parks AFTER its fan-out's turn went by
        _park_fake(ids[0], "a-parked", c)
        later = []
        for _ in range(4):
            arrive()  # a NEW fan-out (higher run id, greater key) before every pick
            later.append(_serve_one(sched))
        kinds = [(p[0], p[2]) for p in later]
        assert ("parked", "a-parked") in kinds, (
            f"the parked run must continue within 4 picks despite continuous arrivals: {kinds}"
        )
        assert ("run", "f-0001") in kinds, (
            f"the oldest fan-out's waiting run must be served within 4 picks: {kinds}"
        )
        print(f"  [P1] parked run and oldest fan-out served despite arrivals: {kinds}")
    finally:
        _unpark_all(ids)
        plant.close()


def test_p2_fanout_order_is_numeric_not_text():
    # PRE-FIX: groups were sorted as text, so "batch:89" came before "batch:9" and the cursor
    # started at groups[0] ("batch:89"): the NEWER fan-out went first. Fails on the first assert.
    plant = Plant()
    try:
        c = mk_customer("N")
        w, d = mk_world(c)
        for _ in range(2):  # batch:9 holds the OLDER run ids
            plant.add(w, d, "batch:9", "es-ES")
        for _ in range(2):
            plant.add(w, d, "batch:89", "es-ES")
        order = [o[2] for o in drain(orchestrator._AdmissionScheduler())]
        assert order[0] == "batch:9", f"the older fan-out (batch:9) goes first: {order}"
        assert order == ["batch:9", "batch:89", "batch:9", "batch:89"], order
        print(f"  [P2] older fan-out first by run id, not text: {order}")
    finally:
        plant.close()


def test_p3_parked_wait_cap_overrides_customer_rotation():
    # PRE-FIX: no wait cap and `parked_at` was never read; the customer cursor (`next c >
    # last_c`) alone chose, so after customer A was served B went first however long A's parked
    # run had waited. The "overdue" assert fails.
    plant = Plant()
    ids = [987711]
    try:
        # A must sort BEFORE B as text, so the old cursor ("next customer greater than the
        # last served") and the new LRU rotation both say "after serving A, B is next"
        c1, c2 = mk_customer("A"), mk_customer("B")
        ca, cb = (c1, c2) if str(c1) < str(c2) else (c2, c1)
        wb, db = mk_world(cb)
        for _ in range(2):
            plant.add(wb, db, "b-only", "es-ES")
        cap = float(getattr(config, "STEP_PARK_MAX_WAIT_S", 60.0))

        def first_pick(age):
            _park_fake(ids[0], "a-parked", ca, age=age)
            try:
                sched = orchestrator._AdmissionScheduler()
                sched._note_served("a-parked", str(ca))  # the rotation says: B next
                return sched._fair_pick(set(), continuations=True)
            finally:
                _unpark_all(ids)

        fresh = first_pick(0.0)
        assert fresh[3] == str(cb) and fresh[0] == "run", f"control: rotation says B: {fresh}"
        overdue = first_pick(cap + 5.0)
        assert overdue[0] == "parked" and overdue[3] == str(ca), (
            f"a parked run past STEP_PARK_MAX_WAIT_S goes first, whatever the rotation: {overdue}"
        )
        print(f"  [P3] fresh parked run waits its turn; one waiting {cap + 5:.0f}s goes first")
    finally:
        _unpark_all(ids)
        plant.close()


def _run_with_steps(customer_id, spec, current_index):
    """A RUNNING WorkflowRun (document of `customer_id`'s project) on a workflow with the steps
    in `spec` (step types), whose next step is `current_index`."""
    wid, did = mk_world(customer_id)
    with Session(engine) as s:
        for i, st in enumerate(spec):
            llm_step = st == StepType.LLM_AGENT
            s.add(
                WorkflowStep(
                    workflow_id=wid,
                    order_index=i,
                    name=f"s{i}",
                    step_type=st,
                    provider="mock" if llm_step else None,
                    prompt_text="Translate {text} into {target_locale}." if llm_step else None,
                    assignee_email=None if llm_step else "pm@fd.test",
                )
            )
        s.commit()
        run = WorkflowRun(
            workflow_id=wid,
            document_id=did,
            target_locale="es-ES",
            status=RunStatus.RUNNING,
            current_step_index=current_index,
        )
        s.add(run)
        s.commit()
        s.refresh(run)
        return run.id


def _segment(run_id, **attrs):
    seg = run_segments.RunSegment(
        run_id=run_id,
        thread_name=f"fd-seg-{run_id}",
        make_stream=None,
        on_crash=lambda exc: None,
        slot=SimpleNamespace(release=lambda **kw: True),
        handoff=True,
        engine=engine,
    )
    for k, v in attrs.items():
        setattr(seg, k, v)
    return seg


def _should_park(seg, step_index):
    """Call `_should_park` with the completed visit's event (the signature); the older
    signature takes only `started`, so on the pre-fix code this stays a real assertion failure
    and not a TypeError."""
    started = time.monotonic() - 100.0  # far past any quantum
    ev = {"type": "step", "step_index": step_index}
    if "event" in inspect.signature(seg._should_park).parameters:
        return seg._should_park(started, ev)
    return seg._should_park(started)


def test_p4_no_park_before_a_pause_only_step():
    # PRE-FIX: `_should_park` was "quantum elapsed and ADMISSION.has_waiting(...)"; with quantum
    # 0 and another customer's row QUEUED it returned True before a human review too. The
    # `assert not park_review` below fails.
    plant = Plant()
    saved = (config.STEP_HANDOFF, config.STEP_QUANTUM_S)
    real_alive = orchestrator.ADMISSION.alive
    try:
        ca, cb = mk_customer("A"), mk_customer("B")
        wb, db = mk_world(cb)
        plant.add(wb, db, "b-waits", "es-ES")  # ANOTHER customer's work waits
        config.STEP_HANDOFF, config.STEP_QUANTUM_S = "auto", 0.0
        orchestrator.ADMISSION.alive = lambda: True
        llm_, rev = StepType.LLM_AGENT, StepType.HUMAN_REVIEW
        r_rev = _run_with_steps(ca, [llm_, llm_, rev], 2)  # next step = human review
        r_llm = _run_with_steps(ca, [llm_, llm_, rev], 1)  # next step = another AI step
        r_end = _run_with_steps(ca, [llm_, llm_], 2)  # next step = the end
        park_review = _should_park(_segment(r_rev), 1)
        park_llm = _should_park(_segment(r_llm), 0)
        park_end = _should_park(_segment(r_end), 1)
        assert park_llm is True, "control: before an AI step the run parks (quantum 0, B waits)"
        assert not park_review, "a run must not park right before a human review step"
        assert not park_end, "a run must not park at the end of the workflow"
        print("  [P4] parks before an AI step, never before human review or the end")
    finally:
        config.STEP_HANDOFF, config.STEP_QUANTUM_S = saved
        orchestrator.ADMISSION.alive = real_alive
        plant.close()


def test_p4b_same_customer_waiting_work_does_not_park():
    # PRE-FIX: `has_waiting(exclude_run=...)` counted ANY queued row, so the customer's own
    # queue made the run park. `assert not same` fails.
    plant = Plant()
    saved = (config.STEP_HANDOFF, config.STEP_QUANTUM_S)
    real_alive = orchestrator.ADMISSION.alive
    try:
        ca = mk_customer("A")
        wa, da = mk_world(ca)
        plant.add(wa, da, "a-own", "es-ES")  # the SAME customer's work waits
        config.STEP_HANDOFF, config.STEP_QUANTUM_S = "auto", 0.0
        orchestrator.ADMISSION.alive = lambda: True
        rid = _run_with_steps(ca, [StepType.LLM_AGENT] * 3, 1)
        same = _should_park(_segment(rid), 0)
        assert not same, "own-customer queued work is no reason to park"
        print("  [P4b] no park for the same customer's own queue")
    finally:
        config.STEP_HANDOFF, config.STEP_QUANTUM_S = saved
        orchestrator.ADMISSION.alive = real_alive
        plant.close()


def test_p5_languages_park_and_continue_together():
    # PRE-FIX: `_sibling_parked_behind` did not exist (AttributeError on the first call), and the
    # pick took ONE parked run per fan-out and then rotated on to the next fan-out, so two
    # batches' parked languages interleaved (77, 78, 77, 78); the order assert fails.
    ids = [987721, 987722, 987723, 987724]
    try:
        c = mk_customer("L")
        sib = _segment(987721, group="batch:77", customer=str(c), cursor=1)
        sib.parked_at = time.monotonic() - 3.0
        with run_segments._lock:
            run_segments.PARKED[987721] = sib

        def me(**kw):
            return _segment(987730, group="batch:77", customer=str(c), **kw)

        assert me(cursor=1)._sibling_parked_behind() is True, "same step: park with the sibling"
        assert me(cursor=2)._sibling_parked_behind() is True, "later step: park with the sibling"
        assert me(cursor=0)._sibling_parked_behind() is False, "earlier step: the sibling is ahead"
        other = _segment(987730, group="batch:78", customer=str(c), cursor=1)
        assert other._sibling_parked_behind() is False, "a different document's batch is no sibling"
        alone = _segment(987721, group="batch:77", customer=str(c), cursor=1)
        assert alone._sibling_parked_behind() is False, "a run is not its own sibling"
        plain = _segment(987730, group="987721", customer=str(c), cursor=1)
        assert plain._sibling_parked_behind() is False, "a plain run has no siblings"
        _unpark_all(ids)

        # the pick continues one batch's parked languages on consecutive picks (two batches of
        # two languages, batch:77 parked first)
        now = time.monotonic()
        for rid, grp, age in (
            (987721, "batch:77", 4.0),
            (987722, "batch:77", 3.9),
            (987723, "batch:78", 2.0),
            (987724, "batch:78", 1.9),
        ):
            seg = _segment(rid, group=grp, customer=str(c), cursor=1)
            seg.parked_at = now - age
            with run_segments._lock:
                run_segments.PARKED[rid] = seg
        sched = orchestrator._AdmissionScheduler()
        served = [_serve_one(sched) for _ in range(4)]
        got = [(p[1], p[2]) for p in served]
        want = [
            (987721, "batch:77"),
            (987722, "batch:77"),
            (987723, "batch:78"),
            (987724, "batch:78"),
        ]
        assert got == want, f"parked siblings continue together, earliest-parked batch first: {got}"
        print(f"  [P5] sibling rule and continuation order: {[g for _, g in got]}")
    finally:
        _unpark_all([*ids, 987730])


def test_p6_batch_keeps_its_turn_beside_another_customer():
    # A document's languages must be admitted (and its parked languages continued) TOGETHER even
    # when another customer waits: the batch's turn (up to its width) outranks the customer
    # rotation, as before (B2 only covers one customer). Without it the flood's
    # languages of one document start seconds apart and reach review up to 10 s apart (the
    # verify_step_queue [G] flood). PRE-FIX this passed (the held-turn check ran before the
    # customer choice); the rewrite moved it behind the customer choice, so two
    # customers alternate EVERY pick: "ABABAB".
    plant = Plant()
    ids = [987741, 987742, 987743]
    try:
        ca, cb = mk_customer("A"), mk_customer("B")
        wa, da = mk_world(ca)
        wb, db = mk_world(cb)
        ba, bb = mk_batch(wa, da, 3), mk_batch(wb, db, 3)
        plant.batches += [ba, bb]
        for loc in ("es-ES", "fr-FR", "de-DE"):
            plant.add(wa, da, f"batch:{ba}", loc, batch_id=ba)
        for loc in ("es-ES", "fr-FR", "de-DE"):
            plant.add(wb, db, f"batch:{bb}", loc, batch_id=bb)
        order = drain(orchestrator._AdmissionScheduler())
        shape = "".join("A" if o[3] == str(ca) else "B" for o in order)
        assert shape in ("AAABBB", "BBBAAA"), (
            f"each document's three languages are admitted together: {shape}"
        )

        # the same for PARKED languages: one document's three parked languages continue on
        # consecutive picks although the other customer has work waiting
        cc, cd = mk_customer("C"), mk_customer("D")
        wd, dd = mk_world(cd)
        bc = mk_batch(wd, dd, 3)  # a batch row gives the group its width 3
        plant.batches.append(bc)
        plant.add(wd, dd, "d-waits", "es-ES")
        plant.add(wd, dd, "d-waits", "es-ES")
        plant.add(wd, dd, "d-waits", "es-ES")
        now = time.monotonic()
        for i, rid in enumerate(ids):
            seg = _segment(rid, group=f"batch:{bc}", customer=str(cc), cursor=1)
            seg.parked_at = now - 5.0 + i * 0.1
            with run_segments._lock:
                run_segments.PARKED[rid] = seg
        sched = orchestrator._AdmissionScheduler()
        sched._note_served("zz", str(cd))  # the rotation says: customer C next
        got = [_serve_one(sched) for _ in range(4)]
        kinds = [(p[0], p[3]) for p in got]
        want = [("parked", str(cc))] * 3 + [("run", str(cd))]
        assert kinds == want, f"three parked languages continue together, then D: {kinds}"
        print(f"  [P6] a batch keeps its turn beside another customer: {shape}; parked {kinds}")
    finally:
        _unpark_all(ids)
        plant.close()


# ---------------------------------------------------------------------------------------------
# [G] batch admission: a document's languages start together (ADR 0099). The after-fix
# flood measured 54% of documents
# with every language at review within 15 s in the default layout: the queue freed slots one at a
# time, so the 2nd and 3rd languages each waited for another run to end. G1, G2 and G5 fail on
# the pre-fix code; G3 and G4 hold on both (kept on purpose).
# ---------------------------------------------------------------------------------------------
class RecordingScheduler(orchestrator._AdmissionScheduler):
    """A private scheduler whose claimed rows start nothing: each launch is recorded and its slot
    is kept, as if the run were executing, until the test releases it."""

    def __init__(self):
        super().__init__()
        self.launched = []  # (monotonic time, run id)
        self.running = []  # the slots of the "executing" runs

    def _launch_claimed(self, run_id, wf_id, doc_id, locale, actor, batch_id, queued_resume, token):
        self.launched.append((time.monotonic(), run_id))
        token.handed_off = True  # the "run" owns it now
        self.running.append(token)


def _batch_plant(plant, width, locales):
    c = mk_customer("G")
    w, d = mk_world(c)
    bid = mk_batch(w, d, width)
    plant.batches.append(bid)
    ids = [plant.add(w, d, f"batch:{bid}", loc, batch_id=bid) for loc in locales]
    return bid, ids


def _drain_in_thread(sched, slot_wait):
    out = {}

    def go():
        out["n"] = sched.drain_once(slot_wait=slot_wait)
        out["t"] = time.monotonic()

    th = threading.Thread(target=go, daemon=True)
    th.start()
    return th


def _release_later(tokens, every):
    def go():
        for tok in tokens:
            time.sleep(every)
            tok.release()

    threading.Thread(target=go, daemon=True).start()


_G_SAVED = (config.MAX_CONCURRENT_RUNS, config.BATCH_GANG_WAIT_S)


def _g_setup(cap, gang_wait):
    config.MAX_CONCURRENT_RUNS = cap
    config.BATCH_GANG_WAIT_S = gang_wait
    orchestrator.RUN_SLOTS._refused_since_yield = 0
    orchestrator.RUN_SLOTS._yield_until = 0.0
    assert orchestrator.RUN_SLOTS.in_flight == 0, orchestrator.RUN_SLOTS.in_flight


def _g_restore(sched=None, busy=()):
    orchestrator._release_all(busy)
    if sched is not None:
        orchestrator._release_all(sched.running)
    config.MAX_CONCURRENT_RUNS, config.BATCH_GANG_WAIT_S = _G_SAVED


def test_g1_languages_admitted_together_under_contention():
    # PRE-FIX: drain_once took ONE slot per admission, so with the queue full the three languages
    # started as three different runs ended (0.3 s, 0.6 s, 0.9 s here): spread about 0.6 s.
    plant = Plant()
    sched = RecordingScheduler()
    busy = []
    try:
        _g_setup(cap=6, gang_wait=5.0)  # the queue holds 5
        busy = orchestrator.RUN_SLOTS.take(5)  # five long runs hold the whole queue share
        _bid, ids = _batch_plant(plant, 3, ["es-ES", "fr-FR", "de-DE"])
        t0 = time.monotonic()
        th = _drain_in_thread(sched, slot_wait=5.0)
        _release_later(busy[:3], 0.3)  # runs end one at a time
        th.join(15)
        assert not th.is_alive(), "drain_once did not return"
        got = sorted(rid for _t, rid in sched.launched)
        assert got == sorted(ids), (got, ids)
        times = [t - t0 for t, _rid in sched.launched]
        spread = max(times) - min(times)
        assert spread <= 0.1, (
            f"a document's languages must start together, not as slots free one by one: "
            f"start times {[round(x, 2) for x in times]}"
        )
        assert min(times) >= 0.85, f"the batch started before three slots were free: {times}"
        print(f"  [G1] 3 languages started {spread * 1000:.0f} ms apart at {min(times):.2f}s")
    finally:
        _g_restore(sched, busy)
        plant.close()


def test_g2_wide_batch_neither_starves_nor_deadlocks():
    # PRE-FIX: the chunk after the first went one run per freed slot (the second chunk's two
    # languages 0.3 s apart); the bounded part (2b) holds on both.
    plant = Plant()
    sched = RecordingScheduler()
    busy = []
    try:
        # 2a. width 5 on a queue of 3: the first 3 together, then the last 2 together
        _g_setup(cap=4, gang_wait=2.0)
        _bid, ids = _batch_plant(plant, 5, ["es-ES", "fr-FR", "de-DE", "it-IT", "pt-BR"])
        t0 = time.monotonic()
        th = _drain_in_thread(sched, slot_wait=5.0)
        assert wait_until(lambda: len(sched.launched) >= 3, 5), sched.launched
        first = list(sched.running[:3])
        _release_later(first[:2], 0.3)
        th.join(15)
        assert not th.is_alive(), "drain_once did not return (deadlock)"
        assert sorted(r for _t, r in sched.launched) == sorted(ids), (sched.launched, ids)
        times = [round(t - t0, 2) for t, _r in sched.launched]
        assert max(times[:3]) - min(times[:3]) <= 0.1, f"first chunk not together: {times}"
        assert times[3] - times[4] <= 0.1 and times[4] - times[3] <= 0.1, (
            f"second chunk not together: {times}"
        )
        assert times[3] >= 0.55, f"second chunk started before two slots were free: {times}"
        _g_restore(sched)

        # 2b. slots that never free enough: the batch starts what it has after the bounded wait,
        # drain_once returns (no deadlock), and the rest start once slots free (no starvation)
        sched = RecordingScheduler()
        _g_setup(cap=4, gang_wait=0.5)
        busy = orchestrator.RUN_SLOTS.take(2)  # one queue slot free, a batch of 3 waiting
        _bid, ids = _batch_plant(plant, 3, ["es-ES", "fr-FR", "de-DE"])
        t0 = time.monotonic()
        n = sched.drain_once(slot_wait=1.0)
        took = time.monotonic() - t0
        assert n == 1 and len(sched.launched) == 1, (n, sched.launched)
        assert took <= 0.5 + 1.0 + 0.5, f"drain_once held on for {took:.2f}s"
        orchestrator._release_all(busy)
        busy = []
        n2 = sched.drain_once(slot_wait=1.0)
        assert n2 == 2 and sorted(r for _t, r in sched.launched) == sorted(ids), (
            n2,
            sched.launched,
        )
        print(
            f"  [G2] width 5 on a queue of 3 started as {times[:3]} + {times[3:]}; "
            f"starved batch started 1 after {took:.2f}s, the rest once slots freed"
        )
    finally:
        _g_restore(sched, busy)
        plant.close()


def test_g3_interactive_launch_gets_through_while_a_batch_waits():
    plant = Plant()
    sched = RecordingScheduler()
    busy = []
    try:
        _g_setup(cap=4, gang_wait=3.0)
        busy = orchestrator.RUN_SLOTS.take(2)
        _bid, _ids = _batch_plant(plant, 3, ["es-ES", "fr-FR", "de-DE"])
        th = _drain_in_thread(sched, slot_wait=5.0)
        # the scheduler holds the one free queue slot for the batch...
        assert wait_until(lambda: orchestrator.RUN_SLOTS.in_flight == 3, 3), (
            orchestrator.RUN_SLOTS.in_flight
        )
        assert not sched.launched, sched.launched
        # ...and a person's launch still finds the interactive reserve free
        tok, observed = orchestrator.RUN_SLOTS.acquire(timeout=0)
        assert tok is not None, f"an interactive launch was refused at in_flight={observed}"
        tok.release()
        orchestrator._release_all(busy)
        busy = []
        th.join(10)
        assert not th.is_alive() and len(sched.launched) == 3, sched.launched
        times = [t for t, _r in sched.launched]
        assert max(times) - min(times) <= 0.1, times
        print("  [G3] an interactive launch got the reserve slot while the batch gathered slots")
    finally:
        _g_restore(sched, busy)
        plant.close()


def test_g4_claim_skips_a_member_another_worker_took():
    # Another worker process claimed one member first: the gang claims the other two together
    # in one transaction and never starts the taken one twice.
    plant = Plant()
    sched = RecordingScheduler()
    try:
        _g_setup(cap=6, gang_wait=1.0)
        _bid, ids = _batch_plant(plant, 3, ["es-ES", "fr-FR", "de-DE"])
        with Session(engine) as s:
            row = s.get(WorkflowRun, ids[1])
            row.status = RunStatus.RUNNING
            s.add(row)
            s.commit()
        n = sched.drain_once(slot_wait=0.5)
        got = sorted(r for _t, r in sched.launched)
        assert n == 2 and got == sorted([ids[0], ids[2]]), (n, got, ids)
        assert orchestrator.RUN_SLOTS.in_flight == 2, orchestrator.RUN_SLOTS.in_flight
        print("  [G4] the member another worker claimed was skipped; the other two started")
    finally:
        _g_restore(sched)
        plant.close()


def test_g6_shared_park_decision_never_parks_before_review():
    # Gate H1: languages can go different ways from one place (a gate loops one back, a
    # per-locale variant). The first sibling's "park" there must not park a sibling whose own
    # next step is a human review. Fails on the first round of ADR 0099 (the shared decision was
    # returned before the run's own pause-only check).
    plant = Plant()
    saved = (config.STEP_HANDOFF, config.STEP_QUANTUM_S)
    real_alive = orchestrator.ADMISSION.alive
    try:
        ca, cb = mk_customer("A"), mk_customer("B")
        wb, db = mk_world(cb)
        plant.add(wb, db, "b-waits", "es-ES")  # another customer waits: a reason to park
        config.STEP_HANDOFF, config.STEP_QUANTUM_S = "auto", 0.0
        orchestrator.ADMISSION.alive = lambda: True
        llm_, rev = StepType.LLM_AGENT, StepType.HUMAN_REVIEW
        r_on = _run_with_steps(ca, [llm_, llm_, llm_, rev], 2)  # next: another AI step
        r_rev = _run_with_steps(ca, [llm_, llm_, rev], 2)  # next: its review
        group = f"batch:{990000 + seq()}"
        first = _segment(r_on, group=group, customer=str(ca))
        second = _segment(r_rev, group=group, customer=str(ca))
        assert _should_park(first, 1) is True, "control: the first sibling parks (B waits)"
        assert _should_park(second, 1) is False, (
            "a sibling's shared decision must not park a run right before its human review"
        )
        assert second.decided == (second.boundaries, False), second.decided
        print("  [G6] the shared park decision never parks a run before its own review")
    finally:
        config.STEP_HANDOFF, config.STEP_QUANTUM_S = saved
        orchestrator.ADMISSION.alive = real_alive
        plant.close()


def test_g7_parked_languages_wait_for_a_sibling_still_stepping():
    # A batch's parked language is not continued while a sibling (running in this process) may
    # still park at its step; it is once the sibling has decided not to, and past
    # BATCH_GANG_WAIT_S whatever the sibling does. The live registry forgets a finished run.
    plant = Plant()
    sib = None
    try:
        c = mk_customer("W")
        w, d = mk_world(c)
        bid = mk_batch(w, d, 2)
        plant.batches.append(bid)
        r_parked = plant.add(w, d, f"batch:{bid}", "es-ES", batch_id=bid)
        r_sib = plant.add(w, d, f"batch:{bid}", "fr-FR", batch_id=bid)
        with Session(engine) as s:
            for rid in (r_parked, r_sib):
                row = s.get(WorkflowRun, rid)
                row.status = RunStatus.RUNNING
                s.add(row)
            s.commit()
        here = _segment(r_parked, group=f"batch:{bid}", customer=str(c), cursor=0)
        here.parked_at = time.monotonic()
        sib = _segment(r_sib, cursor=-1)  # still on its first step: no group known yet
        with run_segments._lock:
            run_segments.LIVE[r_sib] = sib
        sched = orchestrator._AdmissionScheduler()
        assert sched._siblings_still_stepping(here, {}) is True, "sibling on an earlier step"
        sib.cursor, sib.boundaries = 0, 1
        assert sched._siblings_still_stepping(here, {}) is True, "sibling deciding at the step"
        sib.decided = (1, False)
        assert sched._siblings_still_stepping(here, {}) is False, "sibling decided to go on"
        sib.decided = (1, True)
        assert sched._siblings_still_stepping(here, {}) is True, "sibling about to park"
        here.parked_at = time.monotonic() - config.BATCH_GANG_WAIT_S - 1
        assert sched._siblings_still_stepping(here, {}) is False, "bounded by the gang wait"
        sib._finish()
        assert not run_segments.is_live(r_sib), "a finished run leaves the live registry"
        print("  [G7] parked languages wait for a sibling still stepping, within the bound")
    finally:
        if sib is not None:
            with run_segments._lock:
                run_segments.LIVE.pop(sib.run_id, None)
        plant.close()


def test_g8_deferred_parked_work_blocks_its_customers_new_work():
    # Gate R2 M1: while a customer's parked languages wait for a sibling, that customer's
    # NEW documents must not start (finish started work first, 0098); another customer may.
    plant = Plant()
    sib = None
    r_parked = None
    try:
        ca, cb = mk_customer("DA"), mk_customer("DB")
        w, d = mk_world(ca)
        bid = mk_batch(w, d, 2)
        plant.batches.append(bid)
        r_parked = plant.add(w, d, f"batch:{bid}", "es-ES", batch_id=bid)
        r_sib = plant.add(w, d, f"batch:{bid}", "fr-FR", batch_id=bid)
        with Session(engine) as s:
            for rid in (r_parked, r_sib):
                row = s.get(WorkflowRun, rid)
                row.status = RunStatus.RUNNING
                s.add(row)
            s.commit()
        plant.add(w, d, "a-new-doc", "es-ES")  # customer A's new work
        here = _segment(r_parked, group=f"batch:{bid}", customer=str(ca), cursor=0)
        here.parked_at = time.monotonic()
        with run_segments._lock:
            run_segments.PARKED[r_parked] = here
        sib = _segment(r_sib, cursor=-1)  # still on its step
        with run_segments._lock:
            run_segments.LIVE[r_sib] = sib
        sched = orchestrator._AdmissionScheduler()
        assert sched._fair_pick(set(), continuations=True) is None, (
            "customer A's new document started while its parked languages waited for a sibling"
        )
        wb, db = mk_world(cb)
        plant.add(wb, db, "b-new-doc", "es-ES")
        got = sched._fair_pick(set(), continuations=True)
        assert got is not None and got[2] == "b-new-doc", f"another customer is served: {got}"
        print("  [G8] a customer's new work waits while its parked languages wait for a sibling")
    finally:
        if r_parked is not None:
            _unpark_all([r_parked])
        if sib is not None:
            with run_segments._lock:
                run_segments.LIVE.pop(sib.run_id, None)
        plant.close()


# ---------------------------------------------------------------------------------------------
# [A] the T3 scenario
# ---------------------------------------------------------------------------------------------
CALL_S = 0.25
SLOTS = 3
DOCS = 12
STEPS = 3
ARRIVAL_S = 0.25
LOCALES = ["es-ES", "fr-FR", "de-DE"]
_inflight = threading.Semaphore(SLOTS)
CALLS = []


class SlowCapped(providers_hub.MockProvider):
    name = "slowcapped"

    def complete(self, prompt, system=None, task=None, payload=None, images=None, **kw):
        CALLS.append(time.time())
        with _inflight:  # the engine itself only serves SLOTS calls at once
            time.sleep(CALL_S)
        return super().complete(
            prompt, system=system, task=task, payload=payload, images=images, **kw
        )


def test_a_t3_scenario():
    prof = engines.create_profile(
        "T3 capped",
        "local",
        base_url="http://127.0.0.1:9/v1",
        api_key="x",
        model="m",
        cfg={"max_parallel_jobs": SLOTS},
        actor="target",
    )
    engine_name = f"engine:{prof.id}"
    providers_hub.register_provider(engine_name, SlowCapped())
    u = security.create_user("pm@target.test", "Target PM", Role.ADMIN, secrets.token_urlsafe(24))
    _row, key = apikeys.create_key(u.id, name="t3", scopes="api docs")
    # The poll below reads every run several times a second; the per-key API rate limit would
    # answer 429 for most of a minute on a fast box and stall the measurement, not the runs.
    from app.services import settings as _settings

    _settings.set_many(
        {"api_rate_limit_per_minute": "0", "api_read_rate_limit_per_minute": "0"}, actor="verify"
    )
    c = TestClient(app, headers={"X-Loc-Token": key})

    def ok(r):
        assert r.status_code < 300, (
            f"{r.request.method} {r.request.url.path}: {r.status_code} {r.text[:300]}"
        )
        return r.json()

    cust = ok(c.post("/api/customers", json={"name": "T3 Co"}))["id"]
    pid = ok(
        c.post(
            "/api/projects",
            json={
                "name": "t3",
                "customer_id": cust,
                "source_locale": "en-US",
                "targets": ",".join(LOCALES),
                "provider": "mock",
            },
        )
    )["id"]
    wid = ok(
        c.post("/api/workflows", json={"name": "T3", "customer_id": cust, "scope": "customer"})
    )["id"]
    for k in range(STEPS):
        ok(
            c.post(
                f"/api/workflows/{wid}/steps",
                json={
                    "name": f"AI step {k}",
                    "step_type": "llm_agent",
                    "provider": engine_name,
                    "prompt_text": f"Pass {k}: rewrite in {{target_locale}}:\n{{text}}",
                },
            )
        )
    ok(
        c.post(
            f"/api/workflows/{wid}/steps",
            json={
                "name": "Review",
                "step_type": "human_review",
                "assignee_email": "pm@target.test",
            },
        )
    )
    docs = []
    for i in range(DOCS):
        body = (
            f"<html><body><p>Campaign {i}: shorter days, emptier trails.</p></body></html>".encode()
        )
        docs.append(
            ok(
                c.post(
                    "/api/documents",
                    data={"project_id": str(pid)},
                    files={"file": (f"t3-{i}.html", body)},
                )
            )["document_id"]
        )

    t0 = time.time()
    runs = {}  # run_id -> (doc, locale)
    launched = {}  # doc -> launch time
    reached = {}

    def launcher():
        for d in docs:
            launched[d] = time.time() - t0
            b = ok(
                c.post(
                    f"/api/workflows/{wid}/run-batch",
                    json={"doc_id": d, "locales": LOCALES, "max_parallel": 3},
                )
            )
            for m in b["members"]:
                runs[m["run_id"]] = (d, m["locale"])
            time.sleep(ARRIVAL_S)

    launcher_thread = threading.Thread(target=launcher, daemon=True)
    launcher_thread.start()

    def poll():
        if launcher_thread.is_alive():
            time.sleep(0.05)
        for rid in list(runs):
            if rid in reached:
                continue
            st = c.get(f"/api/runs/{rid}").json()
            s = (st.get("status") or "").lower()
            if s in ("paused", "completed", "failed", "cancelled"):
                reached[rid] = (time.time() - t0, s)
        return (not launcher_thread.is_alive()) and len(reached) == len(runs) == DOCS * len(LOCALES)

    done = wait_until(poll, 240, every=0.1)
    assert done, (
        f"the flood did not drain: {len(reached)} of {DOCS * len(LOCALES)} runs reached review"
    )
    run_len = STEPS * CALL_S  # engine time of one run

    # the engine did exactly the work asked of it
    assert len(CALLS) == DOCS * len(LOCALES) * STEPS, (len(CALLS), DOCS * len(LOCALES) * STEPS)

    # 1. per-document locale spread
    spreads = []
    for d in docs:
        ts = [reached[r][0] for r, (dd, _) in runs.items() if dd == d and r in reached]
        assert len(ts) == len(LOCALES), (d, ts)
        spreads.append(max(ts) - min(ts))
    p95 = pct(spreads, 0.95)
    limit = 2 * run_len + 0.3
    assert p95 <= limit, (
        f"per-document locale spread p95={p95:.2f}s > {limit:.1f}s; "
        f"all={[round(x, 2) for x in spreads]}"
    )
    print(f"  [A1] per-document locale spread p95={p95:.2f}s (limit {limit:.1f}s)")

    # 2. no locale systematically last
    by = {
        loc: statistics.mean(
            [
                reached[r][0] - launched[d]
                for r, (d, lc) in runs.items()
                if lc == loc and r in reached
            ]
            or [0]
        )
        for loc in LOCALES
    }
    lo, hi = min(by.values()), max(by.values())
    rounded = {k: round(v, 2) for k, v in by.items()}
    assert lo > 0 and hi <= lo * 1.25, f"a locale is systematically last: {rounded}"
    print(f"  [A2] mean time-to-review by locale: {rounded}")

    # 3. no document starved
    firsts = []
    for d in docs:
        ts = [
            reached[r][0] - launched[d] for r, (dd, _) in runs.items() if dd == d and r in reached
        ]
        firsts.append(min(ts) if ts else 999)
    starve_limit = (DOCS * len(LOCALES) / SLOTS + 2) * run_len + 0.5
    assert max(firsts) <= starve_limit, (
        f"last document's first locale at {max(firsts):.2f}s > {starve_limit:.1f}s"
    )
    print(f"  [A3] last document's first locale at {max(firsts):.2f}s (limit {starve_limit:.1f}s)")

    # every run paused at its human review step, none failed
    bad = {r: v for r, v in reached.items() if v[1] != "paused"}
    assert not bad, f"runs that did not end paused at review: {bad}"
    with Session(engine) as s:
        rows = s.exec(select(WorkflowRun).where(WorkflowRun.id.in_(list(runs)))).all()
        assert all(r.status == RunStatus.PAUSED for r in rows), [r.status for r in rows]
    print(f"  [A4] all {len(runs)} runs paused at review")


# ---------------------------------------------------------------------------------------------
# [G5] end to end through the daemon: two customers' 3-language documents arrive faster than the
# queue drains, every AI step is slower than the quantum (so runs park for the other customer),
# and the queue share (cap 6 - 1 = 5) is not a multiple of 3 (the shape that split documents).
# ---------------------------------------------------------------------------------------------
G5_LOCALES = ["es-ES", "fr-FR", "de-DE"]
G5_DOCS_PER_CUSTOMER = 6
G5_STEP_S = 0.5
G5_QUANTUM_S = 0.3
G5_ARRIVAL_S = 0.15


class SlowStep(providers_hub.MockProvider):
    name = "slowstep"

    def complete(self, prompt, system=None, task=None, payload=None, images=None, **kw):
        time.sleep(G5_STEP_S)
        return super().complete(
            prompt, system=system, task=task, payload=payload, images=images, **kw
        )


def _g5_doc(customer_id):
    from app.models import Segment, SegStatus, TransMode

    with Session(engine) as s:
        p = Project(
            name=f"g5-{seq()}",
            source_locale="en-US",
            target_locales=",".join(G5_LOCALES),
            customer_id=customer_id,
        )
        s.add(p)
        s.commit()
        s.refresh(p)
        d = Document(
            project_id=p.id, filename="d.txt", fmt="txt", mode=TransMode.SEGMENTED, status="new"
        )
        s.add(d)
        s.commit()
        s.refresh(d)
        for loc in G5_LOCALES:
            s.add(
                Segment(
                    document_id=d.id,
                    target_locale=loc,
                    order_index=0,
                    source_text="Press the power button twice.",
                    target_text="",
                    status=SegStatus.NEW,
                )
            )
        s.commit()
        return d.id


def test_g5_slow_mock_flood_languages_start_and_reach_review_together():
    # PRE-FIX: the queue admitted one run per freed slot, so with 5 queue slots and 3 languages
    # a document's languages started as different runs ended and reached review up to a whole
    # run apart; the start-spread and review-spread asserts below fail on cf5b5304b.
    from app.services import batch_runs

    saved = (config.MAX_CONCURRENT_RUNS, config.BATCH_GANG_WAIT_S)
    config.MAX_CONCURRENT_RUNS = 6
    config.BATCH_GANG_WAIT_S = 30.0
    old_quantum = config.STEP_QUANTUM_S
    config.STEP_QUANTUM_S = G5_QUANTUM_S
    slow = f"g5-slow-{seq()}"
    providers_hub.register_provider(slow, SlowStep())
    with Session(engine) as s:
        w = Workflow(
            name=f"g5-{seq()}", scope="global", source_locale="en-US", target_locale="es-ES"
        )
        s.add(w)
        s.commit()
        s.refresh(w)
        for i in range(2):
            s.add(
                WorkflowStep(
                    workflow_id=w.id,
                    order_index=i,
                    name=f"AI {i}",
                    step_type=StepType.LLM_AGENT,
                    provider=slow,
                    prompt_text=f"Pass {i}: {{text}} into {{target_locale}}",
                )
            )
        s.add(
            WorkflowStep(
                workflow_id=w.id,
                order_index=2,
                name="Review",
                step_type=StepType.HUMAN_REVIEW,
                assignee_email="pm@g5.test",
            )
        )
        s.commit()
        wid = w.id
    custs = [mk_customer("GA"), mk_customer("GB")]
    parks0 = run_segments.STATS["parks"]
    docs = {}  # doc id -> run ids
    launched_all = threading.Event()
    trail = {}  # run id -> [(seconds, "park@<step>" | "go")], for the failure message
    real_park, real_continue = run_segments.RunSegment._park, run_segments.RunSegment.continue_on

    def recording_park(self):
        ok = real_park(self)
        if ok:
            trail.setdefault(self.run_id, []).append(
                (round(time.monotonic() - t0, 2), f"park@{self.cursor}")
            )
        return ok

    def recording_continue(self, token):
        trail.setdefault(self.run_id, []).append((round(time.monotonic() - t0, 2), "go"))
        return real_continue(self, token)

    admitted_at = {}  # run id -> the moment the scheduler started it (not the thread's own start)
    real_launch = orchestrator._AdmissionScheduler._launch_claimed

    def recording_launch(self, run_id, *a, **kw):
        admitted_at[run_id] = time.monotonic()
        return real_launch(self, run_id, *a, **kw)

    run_segments.RunSegment._park = recording_park
    run_segments.RunSegment.continue_on = recording_continue
    orchestrator._AdmissionScheduler._launch_claimed = recording_launch

    def launcher():
        try:
            for _i in range(G5_DOCS_PER_CUSTOMER):
                for c in custs:
                    did = _g5_doc(c)
                    with Session(engine) as s:
                        batch = batch_runs.start_batch(
                            s,
                            workflow_id=wid,
                            doc_id=did,
                            locales=G5_LOCALES,
                            created_by="g5",
                            max_parallel=len(G5_LOCALES),
                        )
                        docs[did] = [
                            m["run_id"] for m in batch_runs.batch_as_dict(s, batch)["members"]
                        ]
                    time.sleep(G5_ARRIVAL_S)
        finally:
            launched_all.set()

    try:
        t0 = time.monotonic()
        threading.Thread(target=launcher, daemon=True).start()
        total = 2 * G5_DOCS_PER_CUSTOMER * len(G5_LOCALES)
        reached = {}
        deadline = t0 + 120
        while time.monotonic() < deadline:
            for rid in [r for runs in list(docs.values()) for r in runs]:
                if rid in reached:
                    continue
                with Session(engine) as s:
                    row = s.get(WorkflowRun, rid)
                    st = row.status
                if st == RunStatus.PAUSED:
                    reached[rid] = time.monotonic()
                elif st in (RunStatus.FAILED, RunStatus.CANCELLED, RunStatus.COMPLETED):
                    fail(f"G5 run {rid} ended {st} instead of pausing at its review")
            if launched_all.is_set() and len(reached) == total:
                break
            time.sleep(0.03)
        elapsed = time.monotonic() - t0
    finally:
        config.STEP_QUANTUM_S = old_quantum
        config.MAX_CONCURRENT_RUNS, config.BATCH_GANG_WAIT_S = saved
        run_segments.RunSegment._park = real_park
        run_segments.RunSegment.continue_on = real_continue
        orchestrator._AdmissionScheduler._launch_claimed = real_launch
    assert len(reached) == total, f"only {len(reached)} of {total} runs reached review in 120s"
    start_spreads, review_spreads = [], []
    for runs in docs.values():
        st = [admitted_at[r] for r in runs]
        start_spreads.append(max(st) - min(st))
        rv = [reached[r] for r in runs]
        review_spreads.append(max(rv) - min(rv))
    parks = run_segments.STATS["parks"] - parks0
    detail = (
        f"start spreads={[round(x, 2) for x in start_spreads]} "
        f"review spreads={[round(x, 2) for x in review_spreads]} parks={parks}\n"
        + "\n".join(
            f"  doc {did}: "
            + "; ".join(f"run {r} review {reached[r] - t0:.2f}s {trail.get(r, [])}" for r in runs)
            for did, runs in docs.items()
        )
    )
    assert parks > 0, "no run parked: the flood did not exercise the parked gang path"
    # 0.5 s: robust on a loaded box (gate R2 M2) and still far from the pre-fix 2.25 s
    assert max(start_spreads) <= 0.5, f"a document's languages did not start together: {detail}"
    assert max(review_spreads) <= G5_STEP_S, (
        f"a document's languages reached review more than one step ({G5_STEP_S}s) apart: {detail}"
    )
    print(
        f"  [G5] {total} runs reached review in {elapsed:.1f}s; start spread max "
        f"{max(start_spreads):.2f}s, review spread max {max(review_spreads):.2f}s "
        f"(limit {G5_STEP_S}s), parks={parks}"
    )


TESTS = [
    test_b1_customers_alternate,
    test_b1b_same_customer_fanouts_interleave_with_other_customers,
    test_b2_batch_keeps_its_turn,
    test_b3_plain_groups_rotate_every_pick,
    test_b4_language_order_rotates_across_groups,
    test_b5_next_queued_rotates_past_last_group,
    test_b6_skipped_groups_are_never_returned,
    test_b7_parked_run_goes_first_in_its_group,
    test_p1_no_starvation_under_continuous_arrivals,
    test_p2_fanout_order_is_numeric_not_text,
    test_p3_parked_wait_cap_overrides_customer_rotation,
    test_p4_no_park_before_a_pause_only_step,
    test_p4b_same_customer_waiting_work_does_not_park,
    test_p5_languages_park_and_continue_together,
    test_p6_batch_keeps_its_turn_beside_another_customer,
    test_g1_languages_admitted_together_under_contention,
    test_g2_wide_batch_neither_starves_nor_deadlocks,
    test_g3_interactive_launch_gets_through_while_a_batch_waits,
    test_g4_claim_skips_a_member_another_worker_took,
    test_g6_shared_park_decision_never_parks_before_review,
    test_g7_parked_languages_wait_for_a_sibling_still_stepping,
    test_g8_deferred_parked_work_blocks_its_customers_new_work,
    test_a_t3_scenario,
    test_g5_slow_mock_flood_languages_start_and_reach_review_together,
]


def main():
    # the unit checks run first, with no admission daemon alive in this process
    assert not orchestrator.ADMISSION.alive(), "the unit checks need an idle scheduler"
    for t in TESTS:
        print(f"[{t.__name__}]")
        t0 = time.time()
        t()
        print(f"  ({time.time() - t0:.1f}s)")
    print("FAIR DISPATCH OK")


if __name__ == "__main__":
    main()
