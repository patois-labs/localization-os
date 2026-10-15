"""Issue #607 - /api list routes and detail routes must agree on scope for the same key.

The maintainer's decision: an admin API key with NO customer binding is ORG-WIDE. So every list
that has a detail sibling must show every row the detail route would return to that key
(modulo the documented default filters: archived / terminal status hidden, which each
list lets the caller widen with ?archived=all / ?status=all). A customer-BOUND key must
stay confined to its own customer in BOTH the list and the detail (never widened).

Hermetic + offline. Final line: "API LIST DETAIL SCOPE 607 OK".
"""

import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(_ROOT)
sys.path.insert(0, _ROOT)
from tests._bootstrap import init_env

init_env("loctms_scope607_")

from fastapi.testclient import TestClient
from sqlmodel import Session

from app.db import engine, init_db
from app.models import Customer
from app.services import apikeys, content, security

PW = "scope607-pw-123456"


def _customer(name):
    with Session(engine) as s:
        c = Customer(name=name)
        s.add(c)
        s.commit()
        s.refresh(c)
        return c.id


def _project(name, cust_id):
    with Session(engine) as s:
        p = content.project_create(
            s, name, source_locale="en-US", target_locales="de-DE", customer_id=str(cust_id)
        )
        return p.id


def _key(email, role="admin", customer_id=None):
    u = security.create_user(email, "S", role, PW, customer_id=customer_id)
    _row, raw = apikeys.create_key(u.id, "s607", scopes="api docs admin")
    return {"X-Loc-Token": raw}


def _ids(resp, key):
    assert resp.status_code == 200, (resp.status_code, resp.text[:300])
    return {r["id"] for r in resp.json()[key]}


def test_unbound_admin_list_matches_detail():
    client = TestClient(app)
    ca = _customer("S607 Acme")
    cb = _customer("S607 Beta")
    pa = [_project(f"S607-A{i}", ca) for i in range(3)]
    pb = [_project(f"S607-B{i}", cb) for i in range(2)]
    everything = set(pa) | set(pb)

    H = _key("s607-admin@x.com")
    listed = _ids(client.get("/api/projects", headers=H), "projects")
    # detail route: every project answers 200 for the unbound admin key
    for pid in everything:
        assert client.get(f"/api/projects/{pid}", headers=H).status_code == 200, pid
    assert everything <= listed, ("unbound admin list narrower than detail", everything - listed)

    cust_list = _ids(client.get("/api/customers", headers=H), "customers")
    for cid in (ca, cb):
        assert client.get(f"/api/customers/{cid}/detail", headers=H).status_code == 200, cid
    assert {ca, cb} <= cust_list, cust_list
    print("  [A] unbound admin key: /api/projects and /api/customers list == detail access OK")
    return ca, cb, pa, pb


def test_bound_key_not_widened(ca, cb, pa, pb):
    client = TestClient(app)
    HB = _key("s607-bound@x.com", role="viewer", customer_id=ca)
    # a customer account may need a non-admin role; fall back handled in _key by caller
    listed = _ids(client.get("/api/projects", headers=HB), "projects")
    assert listed == set(pa), ("bound key must see only its own customer", listed)
    # hidden counts are computed inside the bound key's own scope: archiving another
    # customer's project must not move this key's count (no cross-customer leak)
    from datetime import datetime

    from app.models import Project

    with Session(engine) as s:
        pr = s.get(Project, pb[0])
        pr.archived_at = datetime.utcnow()
        s.add(pr)
        s.commit()
    hb = client.get("/api/projects", headers=HB).json()["hidden"]
    assert hb == {"archived": 0, "finished": 0}, ("hidden count leaked across customers", hb)
    with Session(engine) as s:  # restore: later cases share this DB
        pr = s.get(Project, pb[0])
        pr.archived_at = None
        s.add(pr)
        s.commit()
    for pid in pb:
        assert client.get(f"/api/projects/{pid}", headers=HB).status_code == 404, pid
    for pid in pa:
        assert client.get(f"/api/projects/{pid}", headers=HB).status_code == 200, pid
    print("  [B] customer-bound key stays confined to its own customer (list and detail) OK")


def test_default_filters_disclosed():
    """The only list-vs-detail gap left is the documented archive/status default filter;
    the list must SAY how many it hid, and the widened list must equal the detail reach."""
    from datetime import datetime

    from app.models import Project, ProjectStatus

    client = TestClient(app)
    H = _key("s607-admin2@x.com")
    cid = _customer("S607 Gamma")
    live = _project("S607-live", cid)
    arch = _project("S607-arch", cid)
    done = _project("S607-done", cid)
    with Session(engine) as s:
        pr = s.get(Project, arch)
        pr.archived_at = datetime.utcnow()
        s.add(pr)
        pr = s.get(Project, done)
        pr.status = ProjectStatus.COMPLETED
        s.add(pr)
        s.commit()
    body = client.get("/api/projects", headers=H).json()
    ids = {p["id"] for p in body["projects"]}
    assert live in ids and arch not in ids and done not in ids, ids
    assert body["hidden"] == {"archived": 1, "finished": 1}, body.get("hidden")
    for pid in (live, arch, done):  # detail still reaches all three
        assert client.get(f"/api/projects/{pid}", headers=H).status_code == 200, pid
    wide = _ids(client.get("/api/projects?archived=all&status=all", headers=H), "projects")
    assert {live, arch, done} <= wide, wide
    wbody = client.get("/api/projects?archived=all&status=all", headers=H).json()
    assert wbody["hidden"] == {"archived": 0, "finished": 0}, wbody["hidden"]
    print(
        "  [C] default archive/status filters are disclosed via `hidden`; widened list == detail OK"
    )


def test_membership_ring_counts():
    """Review gate: an UNBOUND non-see-all key (viewer) sees only its member projects, and
    its `hidden` counts only archived/finished projects inside that membership ring; a
    named ?status= counts only archived rows of that status."""
    from datetime import datetime

    from app.models import Project, ProjectStatus
    from app.services import access

    client = TestClient(app)
    HV = _key("s607-viewer@x.com", role="viewer")
    cid = _customer("S607 Delta")
    mine = _project("S607-mine", cid)
    mine_done = _project("S607-mine-done", cid)
    other_arch = _project("S607-other-arch", cid)
    other_done = _project("S607-other-done", cid)
    with Session(engine) as s:
        for pid in (mine, mine_done):
            ok, msg = access.add_member(s, pid, "s607-viewer@x.com")
            assert ok, msg
        s.commit()
        for pid, arch, st in (
            (mine_done, True, ProjectStatus.COMPLETED),
            (other_arch, True, None),
            (other_done, False, ProjectStatus.COMPLETED),
        ):
            pr = s.get(Project, pid)
            if arch:
                pr.archived_at = datetime.utcnow()
            if st:
                pr.status = st
            s.add(pr)
        s.commit()
    body = client.get("/api/projects", headers=HV).json()
    assert {p["id"] for p in body["projects"]} == {mine}, body["projects"]
    # only mine_done (archived + completed, counted once, as archived) is in the ring
    assert body["hidden"] == {"archived": 1, "finished": 0}, ("ring leak", body["hidden"])
    assert client.get(f"/api/projects/{other_arch}", headers=HV).status_code == 404
    st = client.get("/api/projects?status=draft", headers=HV).json()
    assert st["hidden"]["archived"] == 0, ("archived count ignores ?status=", st["hidden"])
    print("  [E] unbound viewer: hidden counts stay inside its membership ring OK")


def test_other_pairs(ca, cb):
    """Other /api list-vs-detail pairs for an unbound admin key: customer-scoped
    workflows and glossaries of two customers are listed AND reachable by id."""
    from app.kernel import glossary_module as gm
    from app.services import workflows

    client = TestClient(app)
    H = _key("s607-admin3@x.com")
    with Session(engine) as s:
        wfs = [
            workflows.create_workflow(s, f"S607-wf-{c}", scope="customer", customer_id=str(c)).id
            for c in (ca, cb)
        ]
        gls = [gm.create(s, f"S607-gl-{c}", customer_id=c).id for c in (ca, cb)]
    wl = _ids(client.get("/api/workflows", headers=H), "workflows")
    gl = _ids(client.get("/api/glossaries", headers=H), "glossaries")
    for w in wfs:
        assert client.get(f"/api/workflows/{w}", headers=H).status_code == 200, w
    for g in gls:
        assert client.get(f"/api/glossaries/{g}", headers=H).status_code == 200, g
    assert set(wfs) <= wl, (wfs, wl)
    assert set(gls) <= gl, (gls, gl)
    print("  [D] unbound admin key: /api/workflows and /api/glossaries list == detail access OK")


def main():
    init_db()
    from app.services import settings

    # Auth ON: the customer binding and key scoping are inert while the login wall is off.
    settings.set_many({"security_auth_required": "1"})
    settings.invalidate_cache()
    ca, cb, pa, pb = test_unbound_admin_list_matches_detail()
    test_bound_key_not_widened(ca, cb, pa, pb)
    test_default_filters_disclosed()
    test_membership_ring_counts()
    test_other_pairs(ca, cb)
    print("API LIST DETAIL SCOPE 607 OK")


from app.main import app

if __name__ == "__main__":
    main()
