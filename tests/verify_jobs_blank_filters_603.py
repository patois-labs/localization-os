"""Issue #603 - the Jobs page pager sends empty document= / workflow= params.

Blank (or non-numeric) filter and page params mean "no filter" on /jobs and /api/jobs;
they never produce a raw 422. The Older link of a page past the first 50 rows loads.

Hermetic + offline. Final line: "JOBS BLANK FILTERS 603 OK".
"""

import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(_ROOT)
sys.path.insert(0, _ROOT)
from tests._bootstrap import init_env

init_env("loctms_jobs603_")

from fastapi.testclient import TestClient
from sqlmodel import Session

from app.db import engine, init_db
from app.models import Job
from app.services import apikeys, security

PW = "jobs603-pw-123456"


def main():
    init_db()
    from app.main import app

    with Session(engine) as s:
        for i in range(60):
            s.add(Job(kind="translate_document", status="done", payload={}, created_by=f"t{i}"))
        s.commit()
    u = security.create_user("jobs603@x.com", "J", "admin", PW)
    _row, raw = apikeys.create_key(u.id, "j603", scopes="api docs")
    H = {"X-Loc-Token": raw}
    client = TestClient(app)

    # API: blank filters are "no filter"
    r = client.get("/api/jobs?document=&workflow=&status=&kind=&actor=&type=job", headers=H)
    assert r.status_code == 200, (r.status_code, r.text[:200])
    r = client.get("/api/jobs?document=abc", headers=H)
    assert r.status_code == 422, (r.status_code, r.text[:200])  # non-blank garbage stays 422

    # Web: the exact pager link the template renders (blank document and workflow)
    link = "/jobs?type=job&status=&kind=&actor=&document=&workflow=&page=2"
    web = client.get(link)
    assert web.status_code == 200, (web.status_code, web.text[:200])
    assert client.get("/jobs?page=&document=&workflow=").status_code == 200
    assert client.get("/jobs?document=zzz").status_code == 422
    assert client.get("/jobs?document=0&workflow=-5").status_code == 200
    assert client.get("/jobs?page=abc").status_code == 200  # malformed page stays page 1
    print("  blank jobs filters never 422 (web) and blank = none (api) OK")
    print("JOBS BLANK FILTERS 603 OK")


if __name__ == "__main__":
    main()
