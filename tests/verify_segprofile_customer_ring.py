"""#477 (apiauth-06): /api/segprofiles reads apply the customer ring -- hermetic.

THE BUG: GET /api/segprofiles and GET /api/segprofiles/{pid} filtered by org only, so a
key bound to Customer B listed and opened Customer A's segmentation profiles (names and
descriptions). The web reads are admin-only.

Pinned (auth ON):
  1. A Customer-B-bound key lists only its own and global profiles; Customer A's id is a
     404 like a missing one; its own and the global id are 200.
  2. An unbound staff key still lists and opens every profile.

Final line: "SEGPROFILE CUSTOMER RING OK".
"""

import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(_ROOT)
sys.path.insert(0, _ROOT)
from tests._bootstrap import init_env

init_env("loctms_segprofring_")

from fastapi.testclient import TestClient
from sqlmodel import Session

from app.db import engine, init_db
from app.main import app
from app.models import Customer, Role, User
from app.services import apikeys, security, settings
from app.services import segmentation_profiles as segprof

init_db()

_PW = "segprof-fixture-pass-1"


def _fail(msg):
    print("FAIL:", msg)
    sys.exit(1)


def main():
    with Session(engine) as s:
        a, b = Customer(name="Cust A"), Customer(name="Cust B")
        s.add(a)
        s.add(b)
        s.commit()
        s.refresh(a)
        s.refresh(b)
        cust_a, cust_b = a.id, b.id
    p_a = segprof.create_profile("SM_SEGPROF_A", customer_id=cust_a).id
    p_b = segprof.create_profile("SM_SEGPROF_B", customer_id=cust_b).id
    p_g = segprof.create_profile("SM_SEGPROF_GLOBAL").id
    bound = security.create_user("seg-b@x.com", "B", Role.TRANSLATOR, _PW)
    with Session(engine) as s:
        u = s.get(User, bound.id)
        u.customer_id = cust_b
        s.add(u)
        s.commit()
    staff = security.create_user("seg-s@x.com", "S", Role.TRANSLATOR, _PW)
    kb = apikeys.create_key(bound.id, "b", scopes="api docs")[1]
    ks = apikeys.create_key(staff.id, "s", scopes="api docs")[1]
    settings.set_many({"security_auth_required": "1"})
    settings.invalidate_cache()
    try:
        c = TestClient(app)
        r = c.get("/api/segprofiles", headers={"X-Loc-Token": kb})
        names = {p["name"] for p in r.json().get("segprofiles", [])}
        if r.status_code != 200 or "SM_SEGPROF_A" in names:
            _fail(f"bound list must omit Customer A's profile, got {r.status_code} {names}")
        if not {"SM_SEGPROF_B", "SM_SEGPROF_GLOBAL"} <= names:
            _fail(f"bound list must keep its own and global profiles, got {names}")
        missing = c.get("/api/segprofiles/999999", headers={"X-Loc-Token": kb})
        foreign = c.get(f"/api/segprofiles/{p_a}", headers={"X-Loc-Token": kb})
        if foreign.status_code != 404 or foreign.json() != missing.json():
            _fail(f"foreign profile must 404 like missing, got {foreign.status_code}")
        for pid in (p_b, p_g):
            if c.get(f"/api/segprofiles/{pid}", headers={"X-Loc-Token": kb}).status_code != 200:
                _fail(f"bound key must open profile {pid}")
        print("  1. bound key: own + global only, foreign 404 like missing: OK")
        r = c.get("/api/segprofiles", headers={"X-Loc-Token": ks})
        names = {p["name"] for p in r.json().get("segprofiles", [])}
        if not {"SM_SEGPROF_A", "SM_SEGPROF_B", "SM_SEGPROF_GLOBAL"} <= names:
            _fail(f"staff list must show every profile, got {names}")
        if c.get(f"/api/segprofiles/{p_a}", headers={"X-Loc-Token": ks}).status_code != 200:
            _fail("staff must open Customer A's profile")
        print("  2. unbound staff key: unchanged: OK")
    finally:
        settings.set_many({"security_auth_required": "0"})
        settings.invalidate_cache()
    print("SEGPROFILE CUSTOMER RING OK")


if __name__ == "__main__":
    main()
