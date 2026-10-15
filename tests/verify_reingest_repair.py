"""GitHub #451 follow-ups: reingest_document is a safe repair, for every format.

  units    the stored skeleton["units"] always moves to the new parse, not only for a docx whose
           walk changed. A document whose first parse found 0 units (a txt) and one whose stored
           unit list is stale both end up with units that match the rebuilt segments.
  atomic   the old segments are deleted and the new ones added in ONE transaction. A failure
           while the new segments are built leaves the document exactly as it was (segments,
           skeleton, status), instead of a document with no segments.
  guard    a document with translated text is still refused and untouched.

Final line: "REINGEST REPAIR OK".
"""

import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(_ROOT)
sys.path.insert(0, _ROOT)
for _s in (sys.stdout, sys.stderr):
    try:  # noqa: SIM105
        _s.reconfigure(encoding="utf-8", errors="backslashreplace")
    except Exception:
        pass
from tests._bootstrap import init_env

init_env("loctms_reingest_repair_")

from sqlmodel import Session, select

from app.db import engine, init_db
from app.models import Document, Project, Segment, TargetLocale, TransMode
from app.ports import blobstore
from app.services import ingest as ingest_mod
from app.services.ingest import ingest_document, reingest_document

init_db()
TEXT = "First paragraph here.\n\nSecond paragraph here.\n\nThird paragraph here."
FAILS = []


def check(cond, label):
    print(("ok   " if cond else "FAIL ") + label)
    if not cond:
        FAILS.append(label)


def _project(name):
    with Session(engine) as s:
        p = Project(name=name, source_locale="en-US")
        s.add(p)
        s.commit()
        s.refresh(p)
        s.add(TargetLocale(project_id=p.id, locale="fr-FR"))
        s.commit()
        return p.id


def _segments(doc_id):
    with Session(engine) as s:
        rows = s.exec(
            select(Segment).where(Segment.document_id == doc_id).order_by(Segment.order_index)
        ).all()
        return [(r.id, r.target_locale, r.order_index, r.source_text) for r in rows]


# ---- units: a zero-unit first parse ----------------------------------------------------
pid = _project("units zero")
with Session(engine) as s:
    # Uploads refuse a blank file since #573; allow_empty builds the zero-unit document an
    # older install can still hold, which is what the repair is for.
    doc = ingest_document(
        s, s.get(Project, pid), "blank.txt", b"\n   \n", TransMode.SEGMENTED, allow_empty=True
    )
    doc_id = doc.id
    key = blobstore.document_key(doc)
    check(not (doc.skeleton or {}).get("units"), "a blank upload stores no units")
    blobstore.get().put(key, TEXT.encode("utf-8"))
check(reingest_document(doc_id) is True, "reingest repairs the zero-unit document")
with Session(engine) as s:
    units = (s.get(Document, doc_id).skeleton or {}).get("units") or []
segs = _segments(doc_id)
check(len(units) == 3, f"the stored units follow the new parse (got {len(units)})")
check(
    [u["text"] for u in units] == [x[3] for x in segs if x[1] == segs[0][1]],
    "the stored units match the rebuilt segments, in order",
)

# ---- units: a stale list on a healthy document -----------------------------------------
pid = _project("units stale")
with Session(engine) as s:
    doc = ingest_document(
        s, s.get(Project, pid), "ok.txt", TEXT.encode("utf-8"), TransMode.SEGMENTED
    )
    doc_id = doc.id
    stale = dict(doc.skeleton or {})
    stale["units"] = [{"text": "stale unit", "p_index": 0}]
    doc.skeleton = stale
    s.add(doc)
    s.commit()
check(reingest_document(doc_id) is True, "reingest repairs a document with a stale unit list")
with Session(engine) as s:
    units = (s.get(Document, doc_id).skeleton or {}).get("units") or []
check(
    [u["text"] for u in units][:1] == ["First paragraph here."],
    "stale units replaced by the new parse",
)
check(len(units) == 3, f"all three parsed units stored (got {len(units)})")

# ---- atomic: a failure while building the new segments ---------------------------------
pid = _project("atomic")
with Session(engine) as s:
    doc = ingest_document(
        s, s.get(Project, pid), "ok.txt", TEXT.encode("utf-8"), TransMode.SEGMENTED
    )
    doc_id = doc.id
    before_skel = dict(doc.skeleton or {})
before = _segments(doc_id)
check(len(before) == 3, "setup: three segments before the failed repair")

_real = ingest_mod.extract_inline_tags
_calls = {"n": 0}


def _boom(src):
    _calls["n"] += 1
    if _calls["n"] >= 2:
        raise RuntimeError("simulated failure while building the new segments")
    return _real(src)


ingest_mod.extract_inline_tags = _boom
try:
    try:
        reingest_document(doc_id)
        raised = False
    except RuntimeError:
        raised = True
finally:
    ingest_mod.extract_inline_tags = _real
check(raised, "the simulated failure propagates")
after = _segments(doc_id)
check(
    after == before,
    f"the old segments are intact after a failed repair ({len(after)} of {len(before)})",
)
with Session(engine) as s:
    d = s.get(Document, doc_id)
    check(
        (d.skeleton or {}).get("units") == before_skel.get("units"),
        "the skeleton was not half-updated",
    )

# ---- guard: translated text is still refused -------------------------------------------
pid = _project("guard")
with Session(engine) as s:
    doc = ingest_document(
        s, s.get(Project, pid), "ok.txt", TEXT.encode("utf-8"), TransMode.SEGMENTED
    )
    doc_id = doc.id
    seg = s.exec(select(Segment).where(Segment.document_id == doc_id)).first()
    seg.target_text = "Bonjour"
    s.add(seg)
    s.commit()
guard_before = _segments(doc_id)
check(reingest_document(doc_id) is False, "a translated document is refused")
check(_segments(doc_id) == guard_before, "a refused repair changes nothing")

if FAILS:
    print("FAILED:", len(FAILS))
    sys.exit(1)
print("REINGEST REPAIR OK")
