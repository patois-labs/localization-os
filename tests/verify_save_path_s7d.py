"""S7d: the segment save path after its diet.

1. A changed save keeps its committed rows loaded: after the save's own UPDATE no statement
   re-reads the segment or its document (each used to be re-SELECTed by the LLMemory and
   post-edit captures and the response, the document with its whole skeleton).
2. What the save records is unchanged: the response (ok, status, qa), the stored segment and
   the post-edit episode (final_sha, counts) all carry the text this request committed, across a
   first and a second edit; without the memory add-on no memory event is written.
3. The middleware's audit row (written in bookkeeping.scope()) is appended through a group
   on the bookkeeping lane, never inline (S7d found every save row written inline).

Hermetic: throwaway LOC_TMS_DATA_DIR. ASCII-only.
Final line: "SAVE PATH S7D OK".
"""

import hashlib
import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(_ROOT)
sys.path.insert(0, _ROOT)
from tests._bootstrap import init_env

init_env("loctms_savepath_")

from fastapi.testclient import TestClient
from sqlalchemy import event
from sqlmodel import Session, select

from app.db import engine, init_db
from app.main import app
from app.models import (
    AuditLog,
    Document,
    MemoryEvent,
    Project,
    Segment,
    SegmentPostEdit,
    SegStatus,
    TargetLocale,
)
from app.services import audit, settings

init_db()
settings.set_many(
    {"security_auth_required": "0", "llmemory_capture_enabled": "1", "audit_enabled": "1"}
)
settings.invalidate_cache()
client = TestClient(app)
_SRC, _TGT = "en-US", "de-DE"


def _fail(msg):
    print("FAIL:", msg)
    sys.exit(1)


def _segment(source="The first sentence.", target="Der erste Satz."):
    with Session(engine) as s:
        p = Project(name="s7d", source_locale=_SRC, default_provider="mock")
        s.add(p)
        s.commit()
        s.refresh(p)
        s.add(TargetLocale(project_id=p.id, locale=_TGT))
        d = Document(
            project_id=p.id, filename="d.txt", fmt="txt", mode="segmented", status="parsed"
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


_STATEMENTS = []


@event.listens_for(engine, "before_cursor_execute")
def _record(conn, cursor, statement, params, context, executemany):
    _STATEMENTS.append(" ".join(statement.split()).lower())


def _save(seg_id, text):
    _STATEMENTS.clear()
    r = client.post(f"/segments/{seg_id}/save", data={"target_text": text, "confirm": "1"})
    if r.status_code != 200:
        _fail(f"save answered {r.status_code}: {r.text[:200]}")
    return r.json(), list(_STATEMENTS)


def test_no_reselect_after_commit_and_records_unchanged():
    _pid, _did, sid = _segment()
    for n, text in enumerate(["Der erste Satz, neu.", "Der erste Satz, noch einmal neu."], 1):
        body, stmts = _save(sid, text)
        upd = [i for i, s in enumerate(stmts) if s.startswith("update segment set")]
        if len(upd) != 1:
            _fail(f"expected one UPDATE segment, got {len(upd)}")
        after = stmts[upd[0] + 1 :]
        for table in ("segment", "document"):
            again = [
                s for s in after if s.startswith("select") and f"from {table} where {table}.id" in s
            ]
            if again:
                _fail(f"save #{n}: {table} re-SELECTed after its own commit: {again[0][:120]}")
        if body.get("ok") is not True or body.get("status") != SegStatus.EDITED:
            _fail(f"save #{n}: response {body}")
        if not isinstance(body.get("qa"), list):
            _fail(f"save #{n}: response qa missing: {body}")
        with Session(engine) as s:
            seg = s.get(Segment, sid)
            if seg.target_text != text or seg.status != SegStatus.EDITED:
                _fail(f"save #{n}: stored segment {seg.target_text!r} {seg.status}")
            ev = s.exec(select(MemoryEvent).where(MemoryEvent.segment_id == sid)).first()
            if ev is not None:
                _fail(f"save #{n}: a memory event was written without the memory add-on")
            pe = s.exec(select(SegmentPostEdit).where(SegmentPostEdit.segment_id == sid)).all()
            if len(pe) != 1 or pe[0].edit_count != n:
                _fail(
                    f"save #{n}: post-edit episodes {[(p.episode_seq, p.edit_count) for p in pe]}"
                )
            if pe[0].final_sha != hashlib.sha256(text.encode("utf-8")).hexdigest():
                _fail(f"save #{n}: post-edit final_sha does not describe the saved text")
    print(
        "  [1] two edits: no re-SELECT after the commit; response, segment and "
        "post-edit episode all carry the committed text"
    )


def test_save_audit_row_rides_a_bookkeeping_group():
    _pid, _did, sid = _segment()
    lanes = []
    real = audit._submit

    def spy(event_, bookkeeping=False):
        lanes.append((event_.action, bookkeeping))
        return real(event_, bookkeeping)

    audit._submit = spy
    try:
        _save(sid, "Ein ganz neuer Satz.")
    finally:
        audit._submit = real
    mine = [lane for action, lane in lanes if action.startswith("POST /segments/")]
    if mine != [True]:
        _fail(f"the save's middleware audit row did not ride the bookkeeping lane: {lanes}")
    with Session(engine) as s:
        row = s.exec(select(AuditLog).order_by(AuditLog.id.desc())).first()
    if not row or not row.action.startswith("POST /segments/"):
        _fail(f"newest audit row is not the save: {row and row.action}")
    if not audit.verify_chain()[0]:
        _fail("audit chain does not verify")
    print("  [2] the save's audit row is grouped on the bookkeeping lane; chain verifies")


def test_flagged_save_response_matches_a_fresh_read():
    # Gate LOW-4: a save whose text raises QA flags answers exactly the flags a fresh read of
    # the stored row produces (the response is built from the rows kept loaded across commit).
    from app.web.documents import editor_qa_flags

    _pid, _did, sid = _segment("Pay 25 EUR now.", "Zahle 25 EUR jetzt.")
    body, _ = _save(sid, "Zahle 26  EUR jetzt.")
    with Session(engine) as s:
        seg = s.get(Segment, sid)
        fresh = editor_qa_flags(seg.source_text, seg.target_text, seg.qa_flags, seg.target_locale)
    if not fresh:
        _fail("the flagged save raised no QA flag; the probe text no longer exercises the path")
    if body.get("qa") != fresh:
        _fail(f"response qa {body.get('qa')} != fresh read {fresh}")
    print(f"  [3] a flagged save answers the same {len(fresh)} QA flag(s) a fresh read gives")


def main():
    test_no_reselect_after_commit_and_records_unchanged()
    test_save_audit_row_rides_a_bookkeeping_group()
    test_flagged_save_response_matches_a_fresh_read()
    print("SAVE PATH S7D OK")


if __name__ == "__main__":
    main()
