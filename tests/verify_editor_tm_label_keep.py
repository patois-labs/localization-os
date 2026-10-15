"""Leaving a TM segment without a text change keeps its TM label (not relabelled MT).

Seen on the demo instance: clicking into an untouched 100% TM segment and leaving it
(the editor saves on every blur) rewrote the chip from "TM" to "MT". The chip text comes from
the stored status ("mt" for a TM fill, #610); only the server-rendered page knew it was a fill.
Policy: an unchanged save answers {unchanged: true} and the editor leaves the chip alone; a
changed save relabels and drops the TM fill marker.

Hermetic. ASCII-only. Final line "EDITOR TM LABEL KEEP OK".
"""

import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(_ROOT)
sys.path.insert(0, _ROOT)
from tests._bootstrap import init_env

init_env("loctms_tmlabel_")

from fastapi.testclient import TestClient
from sqlmodel import Session

from app.db import engine, init_db
from app.main import app
from app.models import Document, Project, Segment, SegStatus, TargetLocale
from app.services import settings

init_db()
settings.set_many({"security_auth_required": "0"})
settings.invalidate_cache()
client = TestClient(app)


def _fail(msg):
    print("FAIL:", msg)
    sys.exit(1)


def main():
    with Session(engine) as s:
        p = Project(name="tmlabel", source_locale="en-US", default_provider="mock")
        s.add(p)
        s.commit()
        s.refresh(p)
        s.add(TargetLocale(project_id=p.id, locale="de-DE"))
        s.commit()
        d = Document(
            project_id=p.id, filename="d.txt", fmt="txt", mode="segmented", status="parsed"
        )
        s.add(d)
        s.commit()
        s.refresh(d)
        seg = Segment(
            document_id=d.id,
            target_locale="de-DE",
            order_index=0,
            source_text="Save the file.",
            target_text="Speichern Sie die Datei.",
            status=SegStatus.MT,
            lock_band="100",
            tm_match_pct=100,
        )
        s.add(seg)
        s.commit()
        s.refresh(seg)
        sid, did = seg.id, d.id

    page = client.get(f"/documents/{did}/editor?locale=de-DE")
    if page.status_code != 200:
        _fail(f"editor page {page.status_code}")
    if 'data-tm-fill="1"' not in page.text:
        _fail("the editor page must render the TM fill chip")
    if "if (!data.unchanged)" not in page.text:
        _fail("the editor script must leave the chip alone on an unchanged save")

    r = client.post(
        f"/segments/{sid}/save", data={"target_text": "Speichern Sie die Datei.", "tte_ms": "3"}
    )
    body = r.json()
    if r.status_code != 200 or body.get("ok") is not True:
        _fail(f"save failed: {r.status_code} {body}")
    if body.get("unchanged") is not True or body.get("tm_fill") is not True:
        _fail(f"an unchanged save of a TM fill must say unchanged + tm_fill: {body}")
    if body.get("status") != SegStatus.MT:
        _fail(f"status must stay mt: {body}")

    r = client.post(
        f"/segments/{sid}/save", data={"target_text": "Datei speichern.", "tte_ms": "3"}
    )
    body = r.json()
    if body.get("unchanged") or body.get("status") != SegStatus.EDITED:
        _fail(f"a changed save must relabel (edited), not stay unchanged: {body}")
    print("EDITOR TM LABEL KEEP OK")


if __name__ == "__main__":
    main()
