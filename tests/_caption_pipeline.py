"""Shared web-pipeline check for the broadcast caption suites (verify_scc_filter,
verify_stl_filter): upload through the web route, fill targets, export through the web
route. Imported after init_env, so it runs against the suite's throwaway data dir."""

from fastapi.testclient import TestClient
from sqlmodel import Session, select

from app.db import engine, init_db
from app.main import app
from app.models import Segment

LOCALE = "fr-FR"


def upload(name: str, data: bytes, ctype: str) -> tuple[TestClient, int]:
    init_db()
    client = TestClient(app)
    r = client.post(
        "/projects",
        data={
            "name": f"captions {name}",
            "source_locale": "en-US",
            "target_locales": LOCALE,
            "default_provider": "mock",
        },
        follow_redirects=False,
    )
    pid = int(r.headers["location"].rsplit("/", 1)[-1])
    r = client.post(
        f"/projects/{pid}/documents",
        data={"mode": "segmented"},
        files={"file": (name, data, ctype)},
        follow_redirects=False,
    )
    assert r.status_code in (302, 303), (r.status_code, r.text[:300])
    return client, int(r.headers["location"].rsplit("/", 1)[-1].split("?")[0])


def fill(doc_id: int, prefix: str) -> int:
    """Every target = prefix + source (inline tokens kept); returns the segment count."""
    with Session(engine) as s:
        segs = s.exec(
            select(Segment).where(Segment.document_id == doc_id, Segment.target_locale == LOCALE)
        ).all()
        for sg in segs:
            sg.target_text = prefix + sg.source_text
            sg.status = "mt"
            s.add(sg)
        s.commit()
        return len(segs)


def export(client: TestClient, doc_id: int):
    return client.get(f"/documents/{doc_id}/export", params={"locale": LOCALE})
