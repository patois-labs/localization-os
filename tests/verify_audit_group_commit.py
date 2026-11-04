"""Audit group commit (ADR 0100): many appenders, one verifiable chain.

audit.log() callers of one process that append at the same moment are linked and committed as
one group (rows and head anchor in one transaction, one witness publish). This suite pins:

- groups really form, and grouped rows chain event by event (prev_hash links, anchor and
  witness at the group head);
- concurrent appenders across processes (threads in each) leave one valid chain;
- a crash in the middle of a group (after the rows are flushed, before the commit) loses only
  unacknowledged rows: every log() call that returned is in the database, and the chain and
  the cross-process lock survive;
- a crash after the commit but before the witness is published leaves the witness behind by
  at most one group, which still verifies and catches up on the next write;
- tampering with grouped rows (edit, middle delete, reorder, tail truncation) is detected;
- a shed group hands every caller its OWN posture (fail-open drop, fail-closed refusal), a
  poison event fails only its own caller, and ids out of chain order fall back to one-row
  groups;
- a thread already holding the audit locks writes inline instead of waiting on a group.
"""

import json
import os
import subprocess
import sys
import threading
import time
from contextvars import ContextVar
from itertools import pairwise

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(_ROOT)
sys.path.insert(0, _ROOT)
from tests._bootstrap import init_env

_CHILD = len(sys.argv) >= 3 and sys.argv[1] == "child"
_TMP = None if _CHILD else init_env("loctms_audit_group_")

from sqlalchemy import text
from sqlmodel import Session, select

from app import config, db
from app.db import engine, init_db
from app.models import AuditLog, Setting
from app.services import audit, audit_state, settings


def _rows():
    with Session(engine) as s:
        return s.exec(select(AuditLog).order_by(AuditLog.id)).all()


def _forced_group(n, prefix):
    """Write `n` rows that land in ONE group: hold the chain lock so the first caller leads a
    group of one and blocks, while the other n-1 queue; on release they form one group."""
    sizes = []
    real = audit._append_group_locked

    def counting(group, progress):
        sizes.append(len(group))
        return real(group, progress)

    audit._append_group_locked = counting
    errors = []

    def one(i):
        try:
            audit.log(actor="grp@example.com", action=f"{prefix}.row", target=str(i))
        except Exception as exc:  # pragma: no cover - reported below
            errors.append(exc)

    try:
        with audit._chain_lock:
            first = threading.Thread(target=one, args=(0,))
            first.start()
            _wait(lambda: audit._LANES[False].busy, "a leader")
            others = [threading.Thread(target=one, args=(i,)) for i in range(1, n)]
            for t in others:
                t.start()
            _wait(lambda: len(audit._LANES[False].queue) >= n - 1, "queued events")
        for t in [first, *others]:
            t.join(60)
            assert not t.is_alive(), "an appender never returned"
    finally:
        audit._append_group_locked = real
    assert not errors, errors
    return sizes


def _wait(cond, what, timeout=30.0):
    end = time.monotonic() + timeout
    while not cond():
        assert time.monotonic() < end, f"timed out waiting for {what}"
        time.sleep(0.005)


def test_group_forms_and_chains():
    sizes = _forced_group(12, "group")
    assert sizes == [1, 11], sizes
    rows = [r for r in _rows() if r.action == "group.row"]
    assert len(rows) == 12
    group = rows[1:]
    for a, b in pairwise(group):
        assert b.prev_hash == a.row_hash, "grouped rows link event by event"
        assert b.id > a.id and b.ts >= a.ts
    head = _rows()[-1]
    with Session(engine) as s:
        anchor = s.get(Setting, audit._ANCHOR_KEY).value
    assert anchor.startswith(f"{head.id}:{head.row_hash}:"), "anchor names the group head"
    assert audit_state.read()["id"] == head.id, "the witness names the group head"
    assert audit.verify_chain() == (True, len(_rows()), None)
    print("  [1] 12 callers -> groups [1, 11]; links, anchor and witness at the group head")


def test_tamper_on_grouped_rows():
    _forced_group(10, "tamper")
    rows = [r for r in _rows() if r.action == "tamper.row"][1:]
    victim = rows[4]
    with Session(engine) as s:
        s.exec(text("UPDATE auditlog SET detail='forged' WHERE id=:i").bindparams(i=victim.id))
        s.commit()
    ok, _, bad = audit.verify_chain()
    assert not ok and bad == victim.id, ("edit inside a group", ok, bad, victim.id)
    with Session(engine) as s:
        s.exec(text("UPDATE auditlog SET detail='' WHERE id=:i").bindparams(i=victim.id))
        s.commit()
    assert audit.verify_chain()[0], "undoing the edit verifies again"

    # Reorder two grouped rows (swap every hashed field and the links).
    a, b = rows[2], rows[3]
    cols = ("ts", "actor", "action", "target", "detail", "ip", "prev_hash", "row_hash")
    with Session(engine) as s:
        ra, rb = s.get(AuditLog, a.id), s.get(AuditLog, b.id)
        va, vb = {c: getattr(ra, c) for c in cols}, {c: getattr(rb, c) for c in cols}
        for c in cols:
            setattr(ra, c, vb[c])
            setattr(rb, c, va[c])
        s.add(ra)
        s.add(rb)
        s.commit()
    assert not audit.verify_chain()[0], "a reorder inside a group must fail"
    with Session(engine) as s:
        ra, rb = s.get(AuditLog, a.id), s.get(AuditLog, b.id)
        for c in cols:
            setattr(ra, c, va[c])
            setattr(rb, c, vb[c])
        s.add(ra)
        s.add(rb)
        s.commit()
    assert audit.verify_chain()[0], "reorder undone verifies again"

    # Delete one row from the middle of the group.
    mid = rows[5]
    saved = {c: getattr(mid, c) for c in (*cols, "customer_id", "org_id")}
    with Session(engine) as s:
        s.delete(s.get(AuditLog, mid.id))
        s.commit()
    assert not audit.verify_chain()[0], "a deleted grouped row must fail"
    with Session(engine) as s:
        s.add(AuditLog(id=mid.id, **saved))
        s.commit()
    assert audit.verify_chain()[0], "restored row verifies again"

    # Truncate the tail of the group (the anchor and witness name its head).
    with Session(engine) as s:
        for r in rows[-3:]:
            s.delete(s.get(AuditLog, r.id))
        s.commit()
    ok, _, bad = audit.verify_chain()
    assert not ok and bad is None, ("tail truncation of a group", ok, bad)
    audit.log(actor="grp@example.com", action="after.truncation")
    assert not audit.verify_chain()[0], "a write after the truncation must not heal it"
    audit.reset_anchor(actor="test")
    assert audit.verify_chain()[0]
    print("  [2] edit, reorder, middle delete and tail truncation of grouped rows are detected")


def test_shed_group_posture_per_caller():
    settings.set_many({"audit_fail_open": "1"})
    real = db.commit_or_shed
    shed = {"on": True}

    def fake(session, *, stage=None, attempts=db.WRITE_RETRY_ATTEMPTS, keep_loaded=False):
        if shed["on"] and stage is not None:
            stage()
            session.rollback()
            return False
        return real(session, stage=stage, attempts=attempts, keep_loaded=keep_loaded)

    outcomes = {}
    dropped = audit.dropped_writes()
    real_posture = audit.fail_open

    def posture():
        # fail-closed for "closed" and "recorded"; read once at the top of log()
        return threading.current_thread().name not in ("closed", "recorded")

    def one(name):
        if name == "recorded":
            audit.REQUEST_RECORDED.set(True)  # this request's own row is already on record
        try:
            audit.log(actor="shed@example.com", action=f"shed.{name}")
            outcomes[name] = "returned"
        except db.WriteContentionError:
            outcomes[name] = "refused"

    before = len(_rows())
    db.commit_or_shed = fake
    audit.fail_open = posture
    try:
        with audit._chain_lock:
            lead = threading.Thread(target=one, args=("lead",), name="lead")
            lead.start()
            _wait(lambda: audit._LANES[False].busy, "a leader")
            names = ("open", "closed", "recorded")
            others = [threading.Thread(target=one, args=(n,), name=n) for n in names]
            for t in others:
                t.start()
            _wait(lambda: len(audit._LANES[False].queue) >= 3, "queued events")
        for t in [lead, *others]:
            t.join(60)
    finally:
        db.commit_or_shed = real
        audit.fail_open = real_posture
    expected = {"lead": "returned", "open": "returned", "closed": "refused", "recorded": "returned"}
    assert outcomes == expected, outcomes
    assert len(_rows()) == before, "a shed group writes nothing"
    assert audit.dropped_writes() == dropped + 4, "every shed row is counted"
    assert audit.verify_chain()[0]
    print("  [3] a shed group: each caller applies its own posture and REQUEST_RECORDED")


def test_poison_event_fails_only_its_caller():
    real_hash = audit._compute_hash

    def poison(*args, **kw):
        if args[3] == "poison.row":
            raise RuntimeError("poison event")
        return real_hash(*args, **kw)

    errors = {}

    def one(action):
        try:
            audit.log(actor="p@example.com", action=action)
        except RuntimeError as exc:
            errors[action] = str(exc)

    before = len(_rows())
    audit._compute_hash = poison
    try:
        with audit._chain_lock:
            lead = threading.Thread(target=one, args=("poison.lead",))
            lead.start()
            _wait(lambda: audit._LANES[False].busy, "a leader")
            names = ("poison.a", "poison.row", "poison.b")
            others = [threading.Thread(target=one, args=(n,)) for n in names]
            for t in others:
                t.start()
            _wait(lambda: len(audit._LANES[False].queue) >= 3, "queued events")
        for t in [lead, *others]:
            t.join(60)
    finally:
        audit._compute_hash = real_hash
    assert errors == {"poison.row": "poison event"}, errors
    written = {r.action for r in _rows()[before:]}
    assert written == {"poison.lead", "poison.a", "poison.b"}, written
    assert audit.verify_chain()[0]
    print("  [4] a poison event raises in its own caller; the rest of its group is written")


def test_out_of_order_ids_fall_back_to_single_rows():
    real = audit.pairwise
    calls = {"n": 0}

    def once_out_of_order(ids):
        if len(ids) < 2:
            return real(ids)
        calls["n"] += 1
        return [(2, 1)] if calls["n"] == 1 else real(ids)

    before = len(_rows())
    audit.pairwise = once_out_of_order
    try:
        sizes = _forced_group(6, "order")
    finally:
        audit.pairwise = real
    # group of 1 (lead), then the group of 5 is refused once and rewritten one by one
    assert sizes[:2] == [1, 5] and sizes[2:] == [1] * 5, sizes
    assert len(_rows()) == before + 6
    assert audit.verify_chain()[0]
    print("  [5] ids out of chain order roll the group back; every event is rewritten alone")


def test_lock_holder_writes_inline():
    done = []

    def holder():
        with audit_state.process_lock(), audit._chain_lock:
            for i in range(3):
                audit.log(actor="hold@example.com", action="inline.row", target=str(i))
        done.append(True)

    def other():
        for i in range(10):
            audit.log(actor="other@example.com", action="other.row", target=str(i))

    threads = [threading.Thread(target=holder), *[threading.Thread(target=other) for _ in range(3)]]
    for t in threads:
        t.start()
    for t in threads:
        t.join(60)
        assert not t.is_alive(), "deadlock: a lock holder waited on a group"
    assert done == [True]
    assert audit.verify_chain()[0]
    print("  [6] a thread holding the audit locks writes inline; no deadlock with groups")


def _run_group(callers):
    """Run `callers` (thread targets) so that callers[1:] land in ONE group: callers[0] leads a
    group of one and blocks on the chain lock while the others queue behind it."""
    with audit._chain_lock:
        lead = threading.Thread(target=callers[0])
        lead.start()
        _wait(lambda: audit._LANES[False].busy, "a leader")
        others = [threading.Thread(target=c) for c in callers[1:]]
        for t in others:
            t.start()
        _wait(lambda: len(audit._LANES[False].queue) >= len(others), "queued events")
    for t in [lead, *others]:
        t.join(60)
        assert not t.is_alive(), "an appender never returned"


def test_bookkeeping_callers_group_on_their_own_lane():
    # The middleware's audit tail runs in bookkeeping.scope(). Such callers must still form
    # groups (S7d: they used to write inline, one cross-process lock turn per save), and a
    # bookkeeping group must never carry a plain caller's row, nor the other way round.
    from app.services import bookkeeping

    real = audit._append_group_locked
    groups = []

    def recording(group, progress):
        groups.append(([e.action for e in group], bool(db._bookkeeping_scope.get())))
        return real(group, progress)

    def kept(i):
        def run():
            with bookkeeping.scope():
                audit.log(actor="bk@example.com", action=f"bk.{i}")

        return run

    def plain(i):
        return lambda: audit.log(actor="pl@example.com", action=f"pl.{i}")

    lane = audit._LANES[True]
    audit._append_group_locked = recording
    try:
        with audit._chain_lock:
            lead = threading.Thread(target=kept(0))
            lead.start()
            _wait(lambda: lane.busy, "a bookkeeping leader")
            others = [threading.Thread(target=kept(i)) for i in range(1, 6)]
            others += [threading.Thread(target=plain(i)) for i in range(3)]
            for t in others:
                t.start()
            _wait(lambda: len(lane.queue) >= 5, "queued bookkeeping events")
        for t in [lead, *others]:
            t.join(60)
            assert not t.is_alive(), "an appender never returned"
    finally:
        audit._append_group_locked = real
    for actions, scoped in groups:
        kinds = {a.split(".")[0] for a in actions}
        assert len(kinds) == 1, ("a group mixed routing lanes", actions)
        assert scoped == (kinds == {"bk"}), ("a group ran outside its lane's scope", actions)
    assert any(len(a) > 1 and a[0].startswith("bk.") for a, _ in groups), groups
    assert audit.verify_chain()[0]
    print("  [15] bookkeeping callers group on their own lane; lanes never mix")


def test_relief_scope_writes_inline():
    real = audit._submit
    submitted = []

    def spy(event, bookkeeping=False):
        submitted.append(event.action)
        return real(event, bookkeeping)

    def relief():
        db.begin_relief_scope()
        audit.log(actor="ops@example.com", action="relief.row")

    audit._submit = spy
    try:
        t = threading.Thread(target=relief)
        t.start()
        t.join(60)
        audit.log(actor="ops@example.com", action="plain.row")
    finally:
        audit._submit = real
    assert submitted == ["plain.row"], submitted
    assert {"relief.row", "plain.row"} <= {r.action for r in _rows()}
    assert audit.verify_chain()[0]
    print("  [10] a relief-scope caller writes inline in its own context, never in a group")


_MARK = ContextVar("verify_group_mark", default="none")


def test_fallback_runs_each_event_in_its_own_context():
    real = audit._append_group_locked
    seen = []

    def leader_scope_fails(group, progress):
        if len(group) > 1:
            raise TimeoutError("the leader's own pool budget ran out")
        seen.append((group[0].action, _MARK.get()))
        return real(group, progress)

    def caller(name):
        def run():
            _MARK.set(name)
            audit.log(actor="ctx@example.com", action=f"ctx.{name}")

        return run

    names = ["lead", "a", "b", "c"]
    audit._append_group_locked = leader_scope_fails
    try:
        _run_group([caller(n) for n in names])
    finally:
        audit._append_group_locked = real
    assert sorted(seen) == sorted((f"ctx.{n}", n) for n in names), seen
    assert audit.verify_chain()[0]
    print("  [11] a failed group is rewritten event by event, each in its own caller's context")


def test_commit_error_is_not_rewritten():
    real = db.commit_or_shed
    calls = []

    def lost_commit(session, *, stage=None, attempts=db.WRITE_RETRY_ATTEMPTS, keep_loaded=False):
        calls.append(True)
        stage()
        session.rollback()
        raise RuntimeError("connection lost during COMMIT")

    errors = []

    def caller(i):
        def run():
            try:
                audit.log(actor="c@example.com", action="commit.lost", target=str(i))
            except RuntimeError as exc:
                errors.append(str(exc))

        return run

    before = len(_rows())
    db.commit_or_shed = lost_commit
    try:
        _run_group([caller(i) for i in range(5)])
    finally:
        db.commit_or_shed = real
    assert len(calls) == 2, ("an ambiguous commit error must not be rewritten", len(calls))
    assert errors == ["connection lost during COMMIT"] * 5, errors
    assert len(_rows()) == before
    assert audit.verify_chain()[0]
    print("  [12] an error from the commit itself reaches every caller; nothing is rewritten")


def test_lock_unavailable_counts_every_caller():
    real = audit._append_group_locked

    def no_lock(group, progress):
        with audit._chain_lock:  # wait like a real group, so the others can queue
            raise audit_state.ProcessLockError("Audit process lock unavailable")

    def caller(i):
        return lambda: audit.log(actor="l@example.com", action="lock.row", target=str(i))

    dropped = audit.dropped_writes()
    audit._append_group_locked = no_lock
    try:
        _run_group([caller(i) for i in range(5)])
    finally:
        audit._append_group_locked = real
    assert audit.dropped_writes() == dropped + 5, "every caller's row is counted as dropped"
    print("  [13] an unavailable process lock drops and counts each caller's row")


class _Abort(BaseException):
    pass


def test_leader_dying_after_commit_reports_ok():
    real = audit._append_group_locked

    def commit_then_die(group, progress):
        result = real(group, progress)
        if len(group) > 1:
            raise _Abort()
        return result

    returned, aborted = [], []

    def caller(i):
        def run():
            try:
                audit.log(actor="d@example.com", action="die.row", target=str(i))
                returned.append(i)
            except _Abort:
                aborted.append(i)

        return run

    before = len(_rows())
    dropped = audit.dropped_writes()
    audit._append_group_locked = commit_then_die
    try:
        _run_group([caller(i) for i in range(5)])
    finally:
        audit._append_group_locked = real
    assert len(aborted) == 1 and len(returned) == 4, (returned, aborted)
    assert len(_rows()) == before + 5, "the group's rows are durable"
    assert audit.dropped_writes() == dropped, "durable rows are never counted as dropped"
    assert audit.verify_chain()[0]
    print("  [14] a leader dying after the commit: its followers are told ok, nothing dropped")


# --------------------------------------------------------------------------------------------
# Child processes: concurrent appenders, and crashes at chosen points of a group.
# --------------------------------------------------------------------------------------------
def child(mode, tag):
    init_db()
    sizes = []
    real_append = audit._append_group_locked

    def counting(group, progress):
        sizes.append(len(group))
        return real_append(group, progress)

    audit._append_group_locked = counting
    ack_path = config.DATA_DIR / f"ack-{tag}.txt"
    ack = open(ack_path, "a", encoding="ascii")  # noqa: SIM115 - kept open for the crash
    ack_lock = threading.Lock()
    commits = {"n": 0}
    if mode == "crash_mid":
        real_shed = db.commit_or_shed

        def crash_before_commit(session, *, stage=None, **kw):
            commits["n"] += 1
            if commits["n"] == 4 and stage is not None:
                stage()  # rows flushed into the open transaction, never committed
                doomed = [r["target"] for r in session.info.get("audit_group_rows", ())]
                with open(config.DATA_DIR / "doomed-mid.txt", "w", encoding="ascii") as f:
                    f.write("\n".join(doomed))
                    f.flush()
                    os.fsync(f.fileno())
                os._exit(9)
            return real_shed(session, stage=stage, **kw)

        db.commit_or_shed = crash_before_commit
    elif mode == "crash_publish":
        real_prepare = audit_state._prepare

        def crash_before_publish(content):
            commits["n"] += 1
            if commits["n"] == 4:
                os._exit(9)  # the group is committed, its witness never published
            return real_prepare(content)

        audit_state._prepare = crash_before_publish

    def worker(n):
        for i in range(25):
            target = f"{tag}-{n}-{i}"
            audit.log(actor="proc@example.com", action="proc.row", target=target)
            with ack_lock:
                ack.write(target + "\n")
                ack.flush()
                os.fsync(ack.fileno())

    threads = [threading.Thread(target=worker, args=(n,)) for n in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    ack.close()
    print(json.dumps({"groups": len(sizes), "rows": sum(sizes), "max": max(sizes)}))


def _spawn(mode, tag):
    return subprocess.Popen(
        [sys.executable, os.path.abspath(__file__), "child", mode, tag],
        env=dict(os.environ),
        cwd=_ROOT,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )


def _acked(tag):
    path = config.DATA_DIR / f"ack-{tag}.txt"
    return set(path.read_text(encoding="ascii").split()) if path.exists() else set()


def _targets():
    return {r.target for r in _rows() if r.action == "proc.row"}


def test_concurrent_processes_one_chain():
    before = len(_rows())
    procs = [_spawn("normal", f"p{n}") for n in range(3)]
    stats = []
    for proc in procs:
        out, _ = proc.communicate(timeout=300)
        text_out = out.decode(errors="replace")
        assert proc.returncode == 0, text_out[-800:]
        stats.append(json.loads(text_out.strip().splitlines()[-1]))
    assert len(_rows()) == before + 3 * 6 * 25, (len(_rows()), before)
    assert sum(s["rows"] for s in stats) == 3 * 6 * 25
    assert any(s["max"] > 1 for s in stats), ("no group ever formed", stats)
    ok, _, bad = audit.verify_chain()
    assert ok and bad is None, (ok, bad)
    groups = sum(s["groups"] for s in stats)
    print(f"  [7] 3 processes x 6 threads x 25 rows in {groups} groups: one valid chain")


def test_crash_in_the_middle_of_a_group():
    before = len(_rows())
    proc = _spawn("crash_mid", "mid")
    out, _ = proc.communicate(timeout=300)
    assert proc.returncode == 9, (proc.returncode, out.decode(errors="replace")[-800:])
    acked, present = _acked("mid"), _targets()
    assert acked, "the child acknowledged some rows before the crash"
    assert acked <= present, ("an acknowledged row was lost", sorted(acked - present)[:5])
    doomed = set((config.DATA_DIR / "doomed-mid.txt").read_text(encoding="ascii").split())
    assert doomed, "the child recorded the group it crashed in"
    assert not doomed & present, ("a row of the uncommitted group landed", sorted(doomed & present))
    assert not doomed & acked, "a row of the uncommitted group was acknowledged"
    assert len(_rows()) - before < 6 * 25
    ok, _, bad = audit.verify_chain()
    assert ok and bad is None, ("chain after a mid-group crash", ok, bad)
    # The killed process released the cross-process lock: this process can append at once.
    audit.log(actor="after@example.com", action="after.crash.mid")
    assert audit.verify_chain()[0]
    print(f"  [8] crash mid-group: {len(acked)} acknowledged rows all present, chain valid")


def test_crash_after_commit_before_witness():
    proc = _spawn("crash_publish", "pub")
    out, _ = proc.communicate(timeout=300)
    assert proc.returncode == 9, (proc.returncode, out.decode(errors="replace")[-800:])
    acked, present = _acked("pub"), _targets()
    assert acked <= present, ("an acknowledged row was lost", sorted(acked - present)[:5])
    head = _rows()[-1]
    witness = audit_state.read()
    assert witness["id"] < head.id, "the crashed group's witness was never published"
    trailing = [r for r in _rows() if r.id > witness["id"]]
    assert not {r.target for r in trailing} & acked, "a row past the witness was acknowledged"
    ok, _, bad = audit.verify_chain()
    assert ok and bad is None, ("a trailing witness still verifies", ok, bad)
    audit.log(actor="after@example.com", action="after.crash.publish")
    assert audit_state.read()["id"] == _rows()[-1].id, "the next write republishes"
    assert audit.verify_chain()[0]
    print(f"  [9] crash after commit: witness trailed by {len(trailing)} rows, chain valid")


def test_rollback_latch_survives_retried_stage_and_shed():
    # S7d security review LOW-4: the witness check now runs inside the group's stage. A
    # rollback (the witnessed head deleted) found by a stage that runs twice (a retried
    # commit) and then sheds must still persist the broken latch, and a later append that
    # reuses the deleted id must neither clear it nor make the chain verify. The anchor then
    # also takes both write paths: INSERT when the row is missing, UPDATE afterwards.
    from sqlalchemy import delete

    for i in range(3):
        audit.log(actor="latch@example.com", action=f"latch.seed.{i}")
    head = audit_state.read()["id"]
    with Session(engine) as s:
        s.exec(delete(AuditLog).where(AuditLog.id >= head - 1))
        s.commit()
    real = db.commit_or_shed
    stages = []

    def retried_then_shed(session, *, stage=None, attempts=1, keep_loaded=False):
        for _ in range(2):
            stages.append(True)
            stage()
            session.rollback()
        return False

    db.commit_or_shed = retried_then_shed
    try:
        audit.log(actor="latch@example.com", action="latch.after.rollback")
    finally:
        db.commit_or_shed = real
    assert len(stages) == 2, stages
    assert audit_state.read().get("broken") is True, "a shed group must keep the rollback latch"
    audit.log(actor="latch@example.com", action="latch.reuse")
    assert audit_state.read().get("broken") is True, "a reused id must not clear the latch"
    assert not audit.verify_chain()[0], "a detected rollback must keep failing verification"
    with Session(engine) as s:
        s.exec(delete(Setting).where(Setting.key == audit._ANCHOR_KEY))
        s.commit()
    audit.reset_anchor(actor="test")
    assert audit.verify_chain()[0], "reset-anchor re-baselines after the rollback"
    audit.log(actor="latch@example.com", action="latch.anchor.update")
    with Session(engine) as s:
        anchor = s.get(Setting, audit._ANCHOR_KEY).value
        newest = s.exec(select(AuditLog).order_by(AuditLog.id.desc())).first()
    assert anchor.startswith(f"{newest.id}:{newest.row_hash}:"), "the anchor follows the head"
    assert audit.verify_chain()[0]
    print("  [16] a rollback latch survives a retried, shed stage and a reused id; anchor paths")


def main():
    init_db()
    settings.set_many({"audit_enabled": "1"})
    audit.log(actor="seed@example.com", action="seed")
    test_group_forms_and_chains()
    test_tamper_on_grouped_rows()
    test_shed_group_posture_per_caller()
    test_poison_event_fails_only_its_caller()
    test_out_of_order_ids_fall_back_to_single_rows()
    test_lock_holder_writes_inline()
    test_bookkeeping_callers_group_on_their_own_lane()
    test_relief_scope_writes_inline()
    test_fallback_runs_each_event_in_its_own_context()
    test_commit_error_is_not_rewritten()
    test_lock_unavailable_counts_every_caller()
    test_leader_dying_after_commit_reports_ok()
    test_concurrent_processes_one_chain()
    test_crash_in_the_middle_of_a_group()
    test_crash_after_commit_before_witness()
    test_rollback_latch_survives_retried_stage_and_shed()
    print("verify_audit_group_commit: OK")


if __name__ == "__main__":
    if _CHILD:
        child(sys.argv[2], sys.argv[3])
    else:
        main()
