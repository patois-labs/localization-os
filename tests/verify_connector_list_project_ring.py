"""#474 (apiauth-03): connector lists hide connections bound to hidden projects -- hermetic.

THE BUG: GET /api/connectors and the web /connectors and /connectors/{kind} pages listed
connections by org and customer only, so a translator who is not a member of a project
saw the name, kind, auth mode and project id of connections bound to it. The
per-connection routes already refused them (#370); only the lists leaked.

Pinned (auth ON, project_scoping ON):
  1. /api/connectors: a non-member translator does not see the project-bound connection,
     still sees the unbound one; a member and a manager see both.
  2. Web /connectors/airtable and /connectors: same rule for the session surface.

Final line: "CONNECTOR LIST PROJECT RING OK".
"""

import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(_ROOT)
sys.path.insert(0, _ROOT)
from tests._bootstrap import init_env

init_env("loctms_connlistring_")

from fastapi.testclient import TestClient
from sqlmodel import Session

from app.db import engine, init_db
from app.main import app
from app.models import Connection, Customer, Project, ProjectMember, Role
from app.services import apikeys, security, settings
from tests._csrf import csrf_for

init_db()

_PW = "connlist-fixture-pass-1"
_HIDDEN = "SM_CONN_HIDDEN_PROJECT"
_OPEN = "SM_CONN_UNBOUND"


def _fail(msg):
    print("FAIL:", msg)
    sys.exit(1)


def _seed():
    with Session(engine) as s:
        cust = Customer(name="Cust A")
        s.add(cust)
        s.commit()
        s.refresh(cust)
        proj = Project(name="secret", customer_id=cust.id, created_by="admin@x.com")
        s.add(proj)
        s.commit()
        s.refresh(proj)
        s.add(Connection(kind="airtable", name=_HIDDEN, project_id=proj.id, customer_id=cust.id))
        s.add(Connection(kind="airtable", name=_OPEN))
        s.commit()
        pid = proj.id
    users = {}
    for who, role in (
        ("outsider", Role.TRANSLATOR),
        ("member", Role.TRANSLATOR),
        ("boss", Role.MANAGER),
    ):
        u = security.create_user(f"{who}@x.com", who, role, _PW)
        users[who] = apikeys.create_key(u.id, who, scopes="api docs")[1]
        if who == "member":
            with Session(engine) as s:
                s.add(ProjectMember(project_id=pid, user_id=u.id))
                s.commit()
    return users


def _names(c, key):
    r = c.get("/api/connectors", headers={"X-Loc-Token": key})
    if r.status_code != 200:
        _fail(f"/api/connectors must be 200, got {r.status_code} {r.text[:200]}")
    return {row["name"] for row in r.json()["connections"]}


def _login(c, who):
    csrf_for(c)
    r = c.post(
        "/auth/login",
        data={"email": f"{who}@x.com", "password": _PW, "next": "/"},
        follow_redirects=False,
    )
    if r.status_code != 303:
        _fail(f"login {who} failed: {r.status_code}")
    csrf_for(c)


def main():
    keys = _seed()
    settings.set_many({"security_auth_required": "1", "project_scoping": "1"})
    settings.invalidate_cache()
    try:
        c = TestClient(app)
        names = _names(c, keys["outsider"])
        if _HIDDEN in names:
            _fail("a non-member must not see a connection bound to a hidden project")
        if _OPEN not in names:
            _fail("a non-member must still see an unbound connection")
        for who in ("member", "boss"):
            names = _names(c, keys[who])
            if not {_HIDDEN, _OPEN} <= names:
                _fail(f"{who} must see both connections, got {sorted(names)}")
        print(
            "  1. /api/connectors: non-member omits the hidden-project row; "
            "member and manager see it: OK"
        )

        w = TestClient(app)
        _login(w, "outsider")
        r = w.get("/connectors/airtable")
        if r.status_code != 200:
            _fail(f"web /connectors/airtable must be 200, got {r.status_code}")
        if _HIDDEN in r.text:
            _fail("web /connectors/airtable must not show the hidden-project connection")
        if _OPEN not in r.text:
            _fail("web /connectors/airtable must still show the unbound connection")
        # The index shows a per-kind count: the hidden row must not be counted.
        if ">1 connection<" not in w.get("/connectors").text:
            _fail("web /connectors must count only the outsider's visible airtable connection")
        m = TestClient(app)
        _login(m, "member")
        if _HIDDEN not in m.get("/connectors/airtable").text:
            _fail("web member must see the project-bound connection")
        if ">2 connections<" not in m.get("/connectors").text:
            _fail("web member /connectors must count both airtable connections")
        print("  2. web /connectors and /connectors/airtable: same rule: OK")
    finally:
        settings.set_many({"security_auth_required": "0", "project_scoping": "0"})
        settings.invalidate_cache()
    print("CONNECTOR LIST PROJECT RING OK")


if __name__ == "__main__":
    main()
