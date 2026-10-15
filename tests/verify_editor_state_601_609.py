"""Issues #601 and #609: the editor's segment state follows the TEXT, not focus.

Found by the 2026-10-07 night pressure test (T5, T8). The editor POSTs /segments/{id}/save
on every blur. The route always wrote status EDITED, so:
  * #601: tabbing or clicking into an untouched MT segment and out again flipped it to
    "Edited" with no change to the text;
  * #609: after Ctrl+Enter approved a segment the blur of its textarea saved the same text
    and flipped APPROVED back to EDITED, so a reload showed "0 of 28 confirmed" and approved
    and merely-saved segments were indistinguishable on the page and in the API.

Policy under test: a save whose text equals the stored target changes nothing (status,
tags, locks); a changed text still lands EDITED (an approved segment edited again leaves
approved, honestly); approve keeps APPROVED across later no-op saves; the confirmed count
and the API status survive a reload.

Hermetic: throwaway LOC_TMS_DATA_DIR. ASCII-only.
Final line: "EDITOR STATE 601 609 OK".
"""

import os
import re
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(_ROOT)
sys.path.insert(0, _ROOT)
from tests._bootstrap import init_env

init_env("loctms_editorstate_")

from fastapi.testclient import TestClient
from sqlmodel import Session, select

from app.db import engine, init_db
from app.main import app
from app.models import Document, Project, Role, Segment, SegStatus, TargetLocale, User
from app.services import apikeys, quality, segment, settings

init_db()
settings.set_many({"security_auth_required": "0"})
settings.invalidate_cache()
_SRC, _TGT = "en-US", "de-DE"
client = TestClient(app)
_R1 = segment.ph_open("r", 1)
_R1C = segment.ph_close("r", 1)


def _fail(msg):
    print("FAIL:", msg)
    sys.exit(1)


def _doc(n):
    with Session(engine) as s:
        p = Project(name="es601", source_locale=_SRC, default_provider="mock")
        s.add(p)
        s.commit()
        s.refresh(p)
        s.add(TargetLocale(project_id=p.id, locale=_TGT))
        s.commit()
        d = Document(
            project_id=p.id, filename="d.txt", fmt="txt", mode="segmented", status="parsed"
        )
        s.add(d)
        s.commit()
        s.refresh(d)
        ids = []
        for i in range(n):
            seg = Segment(
                document_id=d.id,
                target_locale=_TGT,
                order_index=i,
                source_text=f"Sentence number {i}.",
                target_text=f"Satz Nummer {i}.",
                status=SegStatus.MT,
            )
            s.add(seg)
            s.commit()
            s.refresh(seg)
            ids.append(seg.id)
        return d.id, ids


def _key():
    with Session(engine) as s:
        u = s.exec(select(User).where(User.email == "es601-admin@x.io")).first()
        if not u:
            u = User(email="es601-admin@x.io", name="A", role=Role.ADMIN, is_active=True)
            s.add(u)
            s.commit()
            s.refresh(u)
        uid = u.id
    _row, raw = apikeys.create_key(uid, "es601", scopes="api docs")
    return raw


def _status(seg_id):
    with Session(engine) as s:
        return s.get(Segment, seg_id).status


def _save(seg_id, text):
    r = client.post(f"/segments/{seg_id}/save", data={"target_text": text, "tte_ms": "5"})
    body = r.json()
    if r.status_code != 200 or body.get("ok") is not True:
        _fail(f"save must succeed: {r.status_code} {body}")
    return body


def test_focus_only_save_keeps_state():
    _d, ids = _doc(1)
    sid = ids[0]
    body = _save(sid, "Satz Nummer 0.")  # the blur save of an untouched row
    if body.get("status") != SegStatus.MT or _status(sid) != SegStatus.MT:
        _fail(f"#601: an unchanged save must keep status mt: {body} / {_status(sid)}")
    body = _save(sid, "Satz Nummer 0 geaendert.")
    if body.get("status") != SegStatus.EDITED or _status(sid) != SegStatus.EDITED:
        _fail(f"a changed text must land edited: {body}")
    print("  1. #601: unchanged save keeps mt, changed text lands edited OK")


def test_approved_survives_blur_and_reload():
    doc_id, ids = _doc(4)
    # seg0 approved untouched; seg1 edited then approved; seg2 only saved (edited); seg3 untouched
    r = client.post(f"/segments/{ids[0]}/approve")
    if r.status_code != 200 or r.json().get("status") != SegStatus.APPROVED:
        _fail(f"approve must give approved: {r.status_code} {r.text}")
    _save(ids[1], "Satz eins bearbeitet.")
    client.post(f"/segments/{ids[1]}/approve")
    _save(ids[2], "Satz zwei bearbeitet.")
    # the blur saves that follow Ctrl+Enter (same text as stored) must not undo approval
    for sid, text in ((ids[0], "Satz Nummer 0."), (ids[1], "Satz eins bearbeitet.")):
        body = _save(sid, text)
        if body.get("status") != SegStatus.APPROVED:
            _fail(f"#609: a blur save must keep approved: {body}")
    want = {
        ids[0]: SegStatus.APPROVED,
        ids[1]: SegStatus.APPROVED,
        ids[2]: SegStatus.EDITED,
        ids[3]: SegStatus.MT,
    }
    for sid, st in want.items():
        if _status(sid) != st:
            _fail(f"#609: segment {sid} should be {st}, is {_status(sid)}")
    page = client.get(f"/documents/{doc_id}/editor?locale={_TGT}").text
    if 'id="approvedCount">2</b> of 4 confirmed' not in page:
        _fail("#609: the reloaded editor must count the 2 approved segments")
    for sid, st in want.items():
        m = re.search(r'id="st-%d">([^<]*)<' % sid, page)  # noqa: UP031
        if not m or m.group(1).strip().lower() != {"mt": "mt"}.get(st, st):
            _fail(f"#609: reloaded chip for {sid} should read {st}: {m and m.group(1)}")
    settings.set_many({"security_auth_required": "1", "multi_org_enabled": "0"})
    settings.invalidate_cache()
    try:
        api = client.get(
            f"/api/documents/{doc_id}/segments?detail=segments", headers={"X-Loc-Token": _key()}
        )
        if api.status_code != 200:
            _fail(f"segments API: {api.status_code} {api.text[:200]}")
        details = api.json()["locales"][_TGT]["segment_details"]
        got = {r["id"]: r["status"] for r in details}
        if got != want:
            _fail(f"#609: the API must tell approved from edited: {got} != {want}")
    finally:
        settings.set_many({"security_auth_required": "0"})
        settings.invalidate_cache()
    print("  2. #609: approved survives blur + reload, count 2 of 4, API distinguishes OK")


def test_editing_an_approved_segment_leaves_approved():
    _d, ids = _doc(1)
    client.post(f"/segments/{ids[0]}/approve")
    body = _save(ids[0], "Ganz neuer Text.")
    if body.get("status") != SegStatus.EDITED:
        _fail(f"a real edit of an approved segment must read edited: {body}")
    print("  3. a real edit of an approved segment reads edited OK")


def test_unchanged_save_with_tags():
    """An unchanged, tag-clean save keeps the status; a tag-damaged body is asked about
    exactly as before (the existing guard is not weakened by the no-op shortcut)."""
    with Session(engine) as s:
        p = Project(name="es601tags", source_locale=_SRC, default_provider="mock")
        s.add(p)
        s.commit()
        s.refresh(p)
        s.add(TargetLocale(project_id=p.id, locale=_TGT))
        s.commit()
        d = Document(
            project_id=p.id, filename="t.txt", fmt="txt", mode="segmented", status="parsed"
        )
        s.add(d)
        s.commit()
        s.refresh(d)
        ids = []
        for i, tgt in enumerate(("Klicken Sie " + _R1 + "hier" + _R1C + ".", "Klicken Sie hier.")):
            seg = Segment(
                document_id=d.id,
                target_locale=_TGT,
                order_index=i,
                source_text="Click " + _R1 + "here" + _R1C + ".",
                target_text=tgt,
                status=SegStatus.MT,
            )
            s.add(seg)
            s.commit()
            s.refresh(seg)
            ids.append(seg.id)
    clean, damaged = ids
    body = _save(clean, "Klicken Sie " + _R1 + "hier" + _R1C + ".")
    if body.get("status") != SegStatus.MT or _status(clean) != SegStatus.MT:
        _fail(f"an unchanged tag-clean save must keep mt: {body}")
    r = client.post(f"/segments/{damaged}/save", data={"target_text": "Klicken Sie hier."})
    if not r.json().get("needs_confirm"):
        _fail("an unchanged but tag-damaged body must still ask for confirmation")
    r = client.post(f"/segments/{clean}/save", data={"target_text": "Klicken Sie dort."})
    if not r.json().get("needs_confirm"):
        _fail("a changed text that drops tags must still ask for confirmation")
    print("  4. unchanged clean save keeps state; tag-damaged saves still ask OK")


def test_crlf_target_untouched_round_trip():
    """A browser textarea returns LF for a stored CRLF target; that round trip is not an edit
    (and a stored CRLF body is kept byte-for-byte), while a real change still lands edited."""
    _d, ids = _doc(1)
    sid = ids[0]
    with Session(engine) as s:
        seg = s.get(Segment, sid)
        seg.target_text = "Zeile eins.\r\nZeile zwei."
        s.add(seg)
        s.commit()
    body = _save(sid, "Zeile eins.\nZeile zwei.")
    if body.get("status") != SegStatus.MT or _status(sid) != SegStatus.MT:
        _fail(f"#601: a CRLF target round-tripped as LF must keep mt: {body}")
    with Session(engine) as s:
        if s.get(Segment, sid).target_text != "Zeile eins.\r\nZeile zwei.":
            _fail("an untouched save must not rewrite the stored text")
    body = _save(sid, "Zeile eins.\nZeile drei.")
    if body.get("status") != SegStatus.EDITED:
        _fail(f"a real change on a CRLF target must land edited: {body}")
    print("  5. CRLF target: untouched LF round trip keeps mt, real edit lands edited OK")


def test_only_line_breaks_are_ignored():
    """Review gate: the no-op rule must never hide a needed transition. Only line-break style
    is ignored. A Unicode-normalization difference (NFD vs stored NFC: canonically equal,
    byte-different) IS an edit: a textarea never renormalizes, so an untouched round trip is
    byte-identical, and the stored bytes are what export, TM matching and QA read. A
    whitespace-only change is an edit too. A NEEDS_REVIEW segment keeps its status and badge
    on an unchanged save and is re-judged (badge cleared, edited) on a real fix."""
    _d, ids = _doc(1)
    sid = ids[0]
    nfc = "Grüße."
    nfd = "Grüße."
    with Session(engine) as s:
        seg = s.get(Segment, sid)
        seg.target_text = nfc
        s.add(seg)
        s.commit()
    body = _save(sid, nfd)
    if body.get("status") != SegStatus.EDITED:
        _fail(f"an NFD body over a stored NFC target is an edit: {body}")
    with Session(engine) as s:
        if s.get(Segment, sid).target_text != nfd:
            _fail("the NFD text the linguist posted must be stored as posted")
    _d, ids = _doc(1)
    body = _save(ids[0], "Satz Nummer 0. ")
    if body.get("status") != SegStatus.EDITED:
        _fail(f"a whitespace-only change is an edit: {body}")
    with Session(engine) as s:
        p = Project(name="es601qa", source_locale=_SRC, default_provider="mock")
        s.add(p)
        s.commit()
        s.refresh(p)
        s.add(TargetLocale(project_id=p.id, locale=_TGT))
        s.commit()
        d = Document(
            project_id=p.id, filename="q.txt", fmt="txt", mode="segmented", status="parsed"
        )
        s.add(d)
        s.commit()
        s.refresh(d)
        seg = Segment(
            document_id=d.id,
            target_locale=_TGT,
            order_index=0,
            source_text="Hello {name}.",
            target_text="Hallo.",
            status=SegStatus.NEEDS_REVIEW,
        )
        seg.qa_flags = quality.merge_placeholder_flags(
            seg.qa_flags, quality.placeholder_violation("Hello {name}.", "Hallo.")
        )
        s.add(seg)
        s.commit()
        s.refresh(seg)
        qid, flags_before = seg.id, seg.qa_flags
    if not flags_before:
        _fail("setup: the placeholder badge must be set")
    body = _save(qid, "Hallo.")
    with Session(engine) as s:
        row = s.get(Segment, qid)
        if row.status != SegStatus.NEEDS_REVIEW or row.qa_flags != flags_before:
            _fail(f"an unchanged save must keep needs_review and its badge: {row.status}")
    body = _save(qid, "Hallo {name}.")
    with Session(engine) as s:
        row = s.get(Segment, qid)
        left = [
            f
            for f in (row.qa_flags or [])
            if isinstance(f, dict) and f.get("type") == quality.PLACEHOLDER_FLAG_TYPE
        ]
        if row.status != SegStatus.EDITED or left:
            _fail(f"a real fix must land edited and clear the badge: {body} / {row.qa_flags}")
    print("  6. NFC/NFD and whitespace are edits; needs_review re-judged only on a real fix OK")


def main():
    test_only_line_breaks_are_ignored()
    test_crlf_target_untouched_round_trip()
    test_focus_only_save_keeps_state()
    test_approved_survives_blur_and_reload()
    test_editing_an_approved_segment_leaves_approved()
    test_unchanged_save_with_tags()
    print("EDITOR STATE 601 609 OK")


if __name__ == "__main__":
    main()
