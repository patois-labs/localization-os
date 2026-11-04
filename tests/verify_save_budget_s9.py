"""S9 (WP-SAVE): the statement, commit and checkout budget of POST /segments/{id}/save.

S7d removed the re-reads after the save's commit. S9 pins what is left, for the two shapes
the editor sends most:

  * a CHANGED save (an edit that continues the linguist's own post-edit episode, the steady
    state of a working session): post-edit episode recorded (the memory event belongs to the
    optional memory add-on and is a no-op in the core);
  * an UNCHANGED save (focus moving through a row: the text equals the stored target).

Counted on the shared engine, all threads (the middleware's audit group runs on a worker
thread but the response waits for it): statements, commits and pool checkouts. The audit
group's own statements (advisory lock, witness read, auditlog INSERT, anchor UPDATE) are
reported but excluded from the handler budget; its commit and checkout are inside the totals.

Also pins WHAT the save records: one post-edit episode per changed save, the episode
continuing across saves, no memory event without the memory add-on, and that a post-edit
failure or a post-edit commit lock never costs the save.

Hermetic: throwaway LOC_TMS_DATA_DIR. ASCII-only. Final line: "SAVE BUDGET S9 OK".
"""

import hashlib
import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(_ROOT)
sys.path.insert(0, _ROOT)
from tests._bootstrap import init_env

init_env("loctms_savebudget_")

from fastapi.testclient import TestClient
from sqlalchemy import event
from sqlmodel import Session, select

from app.db import engine, init_db
from app.main import app
from app.models import (
    Document,
    MemoryEvent,
    Project,
    Segment,
    SegmentPostEdit,
    SegStatus,
    TargetLocale,
)
from app.services import postedit, settings

init_db()
settings.set_many(
    {"security_auth_required": "0", "llmemory_capture_enabled": "1", "audit_enabled": "1"}
)
settings.invalidate_cache()
client = TestClient(app)
_SRC, _TGT = "en-US", "de-DE"

# The budgets. Handler statements exclude the audit group's own (see _is_audit).
# CHANGED, steady state (continuing episode): segment+document+paused-run flag, UPDATE segment,
# then after the save's commit ONE read of project+customer+newest post-edit episode for both
# observers (2026-10-26; it replaces the memory capture's scope read and the post-edit capture's
# last-episode read), memory INSERT, SAVEPOINT, ONE episode UPDATE, RELEASE = 7 handler
# statements (S9: 8; baseline: 14 here, 17 on a test Postgres).
CHANGED_HANDLER_STATEMENTS = 7
UNCHANGED_HANDLER_STATEMENTS = 1
# Commits per CHANGED save: the segment, ONE shared commit for both observers, the audit group.
CHANGED_COMMITS = 3
UNCHANGED_COMMITS = 1  # only the audit group
CHANGED_CHECKOUTS = 3
UNCHANGED_CHECKOUTS = 1 + 1  # the handler's read transaction (rolled back) + the audit group

_STMTS = []
_COMMITS = [0]
_CHECKOUTS = [0]


@event.listens_for(engine, "before_cursor_execute")
def _record(conn, cursor, statement, params, context, executemany):
    _STMTS.append(" ".join(statement.split()).lower())


@event.listens_for(engine, "commit")
def _commit(conn):
    _COMMITS[0] += 1


@event.listens_for(engine.pool, "checkout")
def _checkout(dbapi_con, rec, proxy):
    _CHECKOUTS[0] += 1


def _fail(msg):
    print("FAIL:", msg)
    sys.exit(1)


def _is_audit(s):
    return (
        "auditlog" in s
        or "pg_advisory" in s
        or s.startswith("update setting")
        or s.startswith("insert into setting")
        or ("from setting" in s and "audit" in s)
        or s in ("begin", "commit", "rollback")
        or s.startswith("pragma")
    )


def _segment(source="The first sentence.", target="Der erste Satz.", customer=False):
    with Session(engine) as s:
        p = Project(name="s9", source_locale=_SRC, default_provider="mock")
        s.add(p)
        s.commit()
        s.refresh(p)
        s.add(TargetLocale(project_id=p.id, locale=_TGT))
        d = Document(
            project_id=p.id,
            filename="d.txt",
            fmt="txt",
            mode="segmented",
            status="parsed",
            skeleton={"units": [{"text": "x" * 50}] * 5},
        )
        s.add(d)
        s.commit()
        s.refresh(d)
        seg = Segment(
            document_id=d.id,
            target_locale=_TGT,
            order_index=0,
            source_text=source,
            target_text=target,
            status=SegStatus.MT,
        )
        s.add(seg)
        s.commit()
        s.refresh(seg)
        return p.id, d.id, seg.id


def _save(seg_id, text):
    _STMTS.clear()
    _COMMITS[0] = 0
    _CHECKOUTS[0] = 0
    r = client.post(f"/segments/{seg_id}/save", data={"target_text": text, "confirm": "1"})
    if r.status_code != 200:
        _fail(f"save answered {r.status_code}: {r.text[:200]}")
    handler = [s for s in _STMTS if not _is_audit(s)]
    return r.json(), handler, _COMMITS[0], _CHECKOUTS[0]


def test_changed_save_budget():
    _pid, _did, sid = _segment()
    _save(sid, "Der erste Satz, neu.")  # opens the episode (INSERT)
    body, stmts, commits, checkouts = _save(sid, "Der erste Satz, noch einmal neu.")
    if body.get("ok") is not True:
        _fail(f"changed save answered {body}")
    for s in stmts:
        print("    |", s[:110])
    print(f"    handler={len(stmts)} commits={commits} checkouts={checkouts}")
    if len(stmts) > CHANGED_HANDLER_STATEMENTS:
        _fail(f"changed save: {len(stmts)} handler statements > {CHANGED_HANDLER_STATEMENTS}")
    if commits > CHANGED_COMMITS:
        _fail(f"changed save: {commits} commits > {CHANGED_COMMITS}")
    if checkouts > CHANGED_CHECKOUTS:
        _fail(f"changed save: {checkouts} pool checkouts > {CHANGED_CHECKOUTS}")
    fmt = [s for s in stmts if s.startswith("select document.fmt")]
    if fmt:
        _fail("the save re-SELECTed document.fmt for a document it already loaded")
    projects = [s for s in stmts if s.startswith("select project.")]
    if len(projects) > 1:
        _fail(f"the save read the project {len(projects)} times")
    print(
        f"  [1] changed save: {len(stmts)} handler statements, {commits} commits, "
        f"{checkouts} checkouts"
    )


def test_unchanged_save_budget():
    _pid, _did, sid = _segment()
    body, stmts, commits, checkouts = _save(sid, "Der erste Satz.")
    if not body.get("unchanged"):
        _fail(f"expected an unchanged save, got {body}")
    print(f"    handler={len(stmts)} commits={commits} checkouts={checkouts}")
    if len(stmts) > UNCHANGED_HANDLER_STATEMENTS:
        _fail(f"unchanged save: {len(stmts)} handler statements > {UNCHANGED_HANDLER_STATEMENTS}")
    if commits > UNCHANGED_COMMITS:
        _fail(f"unchanged save: {commits} commits > {UNCHANGED_COMMITS}")
    print(f"  [2] unchanged save: {len(stmts)} handler statements, {commits} commits")


def test_observers_share_one_commit_and_survive_each_other():
    _pid, _did, sid = _segment()
    _save(sid, "Eins.")
    _save(sid, "Zwei.")
    with Session(engine) as s:
        evs = s.exec(select(MemoryEvent).where(MemoryEvent.segment_id == sid)).all()
        pes = s.exec(select(SegmentPostEdit).where(SegmentPostEdit.segment_id == sid)).all()
    if len(evs) != 0 or len(pes) != 1 or pes[0].edit_count != 2:
        _fail(f"events {len(evs)} episodes {[(p.episode_seq, p.edit_count) for p in pes]}")

    # A post-edit capture that raises must not lose the save.
    real = postedit.capture

    def boom(*a, **k):
        raise RuntimeError("pe down")

    postedit.capture = boom
    try:
        _save(sid, "Drei.")
    finally:
        postedit.capture = real
    with Session(engine) as s:
        evs = s.exec(select(MemoryEvent).where(MemoryEvent.segment_id == sid)).all()
        pes = s.exec(select(SegmentPostEdit).where(SegmentPostEdit.segment_id == sid)).all()
        seg = s.get(Segment, sid)
    if len(evs) != 0 or seg.target_text != "Drei.":
        _fail(f"a failed post-edit capture lost the save: {len(evs)}")
    if pes[0].edit_count != 2:
        _fail("the failed capture changed the episode")

    _save(sid, "Vier.")
    with Session(engine) as s:
        evs = s.exec(select(MemoryEvent).where(MemoryEvent.segment_id == sid)).all()
        pes = s.exec(select(SegmentPostEdit).where(SegmentPostEdit.segment_id == sid)).all()
        seg = s.get(Segment, sid)
    if seg.target_text != "Vier." or len(evs) != 0:
        _fail(f"the save after a failed capture: text {seg.target_text!r}, {len(evs)} events")
    # The "Drei." save recorded no episode (its capture raised), so "Vier." opened episode 2.
    newest = max(pes, key=lambda p: p.episode_seq)
    if newest.episode_seq != 2 or newest.final_sha != hashlib.sha256(b"Vier.").hexdigest():
        _fail(f"the post-edit row after a failed capture: {[(p.episode_seq) for p in pes]}")
    print("  [3] post-edit episodes continue; a failed capture never costs the save")


def test_post_edit_lock_does_not_cost_the_memory_event():
    # A writer lock that only the post-edit commit loses sheds the shared commit; the memory
    # event still gets its own attempt (before S9 it had its own commit and survived).
    from sqlalchemy.exc import OperationalError
    from sqlalchemy.orm import Session as SASession

    from app import db as _db

    _pid, _did, sid = _segment()
    orig_commit, orig_sleep = SASession.commit, _db.time.sleep

    def locking_commit(self, *a, **k):
        if any(isinstance(o, SegmentPostEdit) for o in self):
            raise OperationalError(
                "INSERT INTO segmentpostedit", {}, Exception("database is locked")
            )
        return orig_commit(self, *a, **k)

    try:
        SASession.commit = locking_commit
        _db.time.sleep = lambda *_a, **_k: None
        r = client.post(f"/segments/{sid}/save", data={"target_text": "Unter Sperre."})
    finally:
        SASession.commit = orig_commit
        _db.time.sleep = orig_sleep
    if r.status_code != 200:
        _fail(f"a shed post-edit commit failed the save: {r.status_code}")
    with Session(engine) as s:
        seg = s.get(Segment, sid)
        evs = s.exec(select(MemoryEvent).where(MemoryEvent.segment_id == sid)).all()
    if seg.target_text != "Unter Sperre." or len(evs) != 0:
        _fail(f"under a post-edit lock: text {seg.target_text!r}, {len(evs)} memory events")
    print("  [4] a post-edit commit lock sheds the episode only; the save stays")


def test_stale_prefetched_episode_is_re_read():
    # 2026-10-26: the save hands postedit.capture the episode its observer read loaded. A
    # concurrent save can make that stale; the worst case is "no episode yet" while one exists:
    # the capture then tries to open episode 1 again, hits the (segment_id, episode_seq) unique
    # index, and the race retry must RE-READ (not reuse the stale prefetch) and record the edit.
    _pid, _did, sid = _segment()
    _save(sid, "Erste Fassung.")
    with Session(engine) as s:
        seg = s.get(Segment, sid)
        row = postedit.capture_guarded(
            s,
            segment=seg,
            pre="Erste Fassung.",
            post="Zweite Fassung.",
            actor_kind="human",
            actor="other@example.test",
            prefetched_last=None,
        )
        s.commit()
    with Session(engine) as s:
        pes = s.exec(select(SegmentPostEdit).where(SegmentPostEdit.segment_id == sid)).all()
    seqs = sorted(p.episode_seq for p in pes)
    if row is None or seqs != [1, 2]:
        _fail(f"stale prefetch: capture returned {row!r}, episodes {seqs} (want [1, 2])")
    print("  [6] a stale prefetched episode hits the unique index and is re-read on the retry")


def test_stale_prefetched_older_episode_is_metrics_only():
    # Gate r1 MEDIUM-1: a prefetched episode that a concurrent save has since superseded (the
    # read and the capture are not atomic) continues the older episode (one more edit on it)
    # instead of opening a third; no exception, no new row, nothing else touched. Post-edit
    # metrics only.
    _pid, _did, sid = _segment()
    with Session(engine) as s:
        seg = s.get(Segment, sid)
        postedit.capture_guarded(
            s,
            segment=seg,
            pre="Der erste Satz.",
            post="A eins.",
            actor_kind="human",
            actor="a@x.test",
        )
        postedit.capture_guarded(
            s, segment=seg, pre="A eins.", post="B zwei.", actor_kind="human", actor="b@x.test"
        )
        s.commit()
    with Session(engine) as s:
        seg = s.get(Segment, sid)
        older = s.exec(
            select(SegmentPostEdit).where(
                SegmentPostEdit.segment_id == sid, SegmentPostEdit.episode_seq == 1
            )
        ).one()
        row = postedit.capture_guarded(
            s,
            segment=seg,
            pre="A eins.",
            post="A drei.",
            actor_kind="human",
            actor="a@x.test",
            prefetched_last=older,
        )
        s.commit()
    with Session(engine) as s:
        pes = sorted(
            s.exec(select(SegmentPostEdit).where(SegmentPostEdit.segment_id == sid)).all(),
            key=lambda p: p.episode_seq,
        )
    if row is None or [p.episode_seq for p in pes] != [1, 2] or pes[0].edit_count != 2:
        _fail(
            f"stale older prefetch: {[(p.episode_seq, p.edit_count) for p in pes]} "
            "(want episode 1 continued to 2 edits, episode 2 untouched)"
        )
    if pes[1].edit_count != 1 or pes[1].actor != "b@x.test":
        _fail("the stale prefetch touched the newer episode")
    print("  [7] a stale older prefetch continues that episode only (accepted, metrics only)")


def _reads_scope(stmt):
    # A FROM or JOIN of the project, customer or post-edit table (not a column like project_id).
    return any(f" {t} " in f" {stmt} " for t in ("project", "customer", "segmentpostedit"))


def test_one_observer_read_and_a_light_first_statement():
    # Gate r2 LOW-1, redone for the post-commit read: an unchanged save reads no project,
    # customer or episode at all (its one statement is the light first statement), and a changed
    # save reads the project, the customer and the NEWEST episode in one statement.
    from app.web.documents_part3 import _scope_and_episode

    _pid, _did, sid = _segment()
    _body, stmts, _c, _k = _save(sid, "Der erste Satz.")
    if len(stmts) != 1 or _reads_scope(stmts[0]):
        _fail(f"unchanged save statements: {[x[:80] for x in stmts]}")
    pid, did, sid = _segment()
    with Session(engine) as s:
        seg = s.get(Segment, sid)
        for pre, post in (("Der erste Satz.", "A eins."), ("A eins.", "B zwei.")):
            postedit.capture_guarded(
                s, segment=seg, pre=pre, post=post, actor_kind="human", actor=post
            )
        s.commit()
        proj, cust, pe = s.exec(_scope_and_episode(), params={"seg_id": sid, "doc_id": did}).one()
    if proj is None or proj.id != pid or cust is not None or pe is None or pe.episode_seq != 2:
        _fail(f"observer read: {proj and proj.id} {cust} {pe and pe.episode_seq}")
    _body, stmts, _c, _k = _save(sid, "C drei.")
    # (Not counted: the capture's cum_* origin lookup, episode 1, read only from episode 2 on.)
    reads = [
        x
        for x in stmts
        if x.startswith("select")
        and _reads_scope(x)
        and "order by segmentpostedit.episode_seq asc" not in x
    ]
    if len(reads) != 1:
        _fail(
            f"changed save: {len(reads)} project/episode reads (want 1): {[x[:80] for x in reads]}"
        )
    print("  [9] unchanged save: one light statement; changed save: one observer read")


def main():
    test_changed_save_budget()
    test_unchanged_save_budget()
    test_observers_share_one_commit_and_survive_each_other()
    test_post_edit_lock_does_not_cost_the_memory_event()
    test_stale_prefetched_episode_is_re_read()
    test_stale_prefetched_older_episode_is_metrics_only()
    test_one_observer_read_and_a_light_first_statement()
    print("SAVE BUDGET S9 OK")


if __name__ == "__main__":
    main()
