"""#475 (apiauth-04): customer rename needs can_configure_customer -- hermetic.

THE BUG: #284 raised customer settings to can_configure_customer (admin, manager,
linguist), but customer rename stayed at can_write, so any translator or reviewer could
rename any customer in the org on web and /api (remote CLI).

Pinned (auth ON):
  1. /api/customers/{cid}/rename: translator and reviewer 403, name unchanged; manager,
     linguist and admin 200.
  2. Web POST /customers/{cid}/rename: translator 403, name unchanged; manager 303. The
     customer page hides the rename form from a translator.

Final line: "CUSTOMER RENAME GATE OK".
"""

import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(_ROOT)
sys.path.insert(0, _ROOT)
from tests._bootstrap import init_env

init_env("loctms_custrenamegate_")

from fastapi.testclient import TestClient
from sqlmodel import Session

from app.db import engine, init_db
from app.main import app
from app.models import Customer, Role
from app.services import apikeys, security, settings
from tests._csrf import csrf_for

init_db()

_PW = "custrename-fixture-pass-1"


def _fail(msg):
    print("FAIL:", msg)
    sys.exit(1)


def _name(cid):
    with Session(engine) as s:
        return s.get(Customer, cid).name


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
    with Session(engine) as s:
        cust = Customer(name="Original Name")
        s.add(cust)
        s.commit()
        s.refresh(cust)
        cid = cust.id
    keys = {}
    for role in (Role.TRANSLATOR, Role.REVIEWER, Role.MANAGER, Role.LINGUIST, Role.ADMIN):
        u = security.create_user(f"{role}@x.com", role, role, _PW)
        keys[role] = apikeys.create_key(u.id, role, scopes="api docs")[1]
    settings.set_many({"security_auth_required": "1"})
    settings.invalidate_cache()
    try:
        c = TestClient(app)
        for role in (Role.TRANSLATOR, Role.REVIEWER):
            r = c.post(
                f"/api/customers/{cid}/rename",
                json={"name": f"renamed by {role}"},
                headers={"X-Loc-Token": keys[role]},
            )
            if r.status_code != 403:
                _fail(f"/api rename as {role} must be 403, got {r.status_code} {r.text[:200]}")
            if _name(cid) != "Original Name":
                _fail(f"/api rename as {role} changed the customer name")
        for role in (Role.MANAGER, Role.LINGUIST, Role.ADMIN):
            r = c.post(
                f"/api/customers/{cid}/rename",
                json={"name": f"renamed by {role}"},
                headers={"X-Loc-Token": keys[role]},
            )
            if r.status_code != 200 or _name(cid) != f"renamed by {role}":
                _fail(f"/api rename as {role} must be 200, got {r.status_code} {r.text[:200]}")
        print("  1. /api: translator and reviewer 403; manager, linguist, admin 200: OK")

        before = _name(cid)
        w = TestClient(app)
        _login(w, Role.TRANSLATOR)
        page = w.get(f"/customers/{cid}")
        if page.status_code != 200:
            _fail(f"translator customer page must be 200, got {page.status_code}")
        if f"/customers/{cid}/rename" in page.text:
            _fail("the customer page must not offer the rename form to a translator")
        r = w.post(
            f"/customers/{cid}/rename", data={"name": "web translator"}, follow_redirects=False
        )
        if r.status_code != 403 or _name(cid) != before:
            _fail(f"web rename as translator must be 403 and change nothing, got {r.status_code}")
        m = TestClient(app)
        _login(m, Role.MANAGER)
        r = m.post(f"/customers/{cid}/rename", data={"name": "web manager"}, follow_redirects=False)
        if r.status_code != 303 or _name(cid) != "web manager":
            _fail(f"web rename as manager must 303 and save, got {r.status_code}")
        print("  2. web: translator 403 (no form), manager 303: OK")
    finally:
        settings.set_many({"security_auth_required": "0"})
        settings.invalidate_cache()
    print("CUSTOMER RENAME GATE OK")


if __name__ == "__main__":
    main()
