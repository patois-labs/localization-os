"""Managers may define custom project fields (10-13), HTTP level, login wall ON.

  [1] API: a manager creates a customer-scoped field; an org-wide one (no customer) is 403;
      translator / viewer are 403; an admin creates both.
  [2] API: creator-or-admin: another manager cannot edit or archive it (403), the creator can,
      an admin can; a manager can never touch an org-wide field.
  [3] A customer-bound account (never a manager) is refused create and edit, still reads.
  [4] Web: a manager creates a customer field through the settings page, is refused an org-wide
      one, cannot update/archive another's field; viewers are refused the page; the editor shows
      edit forms only for fields the caller may change.
  [5] Audit rows are unchanged (field.define / field.update / field.archive by the manager).

Final line: "CUSTOM FIELDS MANAGERS OK".
"""

import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(_ROOT)
sys.path.insert(0, _ROOT)
from tests._bootstrap import init_env

init_env("loctms_cf_mgr_")

from fastapi.testclient import TestClient
from sqlmodel import Session, select

from app.db import engine, init_db
from app.main import app
from app.models import AuditLog, Customer, Role
from app.services import apikeys, security, settings
from tests._csrf import csrf_for

PW = "Offline-cfmgr-fixture-123!"  # pregate: allow-secret test fixture only


def _login(email):
    w = TestClient(app)
    csrf_for(w)
    r = w.post("/auth/login", data={"email": email, "password": PW}, follow_redirects=False)
    assert r.status_code == 303, r.status_code
    csrf_for(w)
    return w


def main():
    init_db()
    settings.set_many({"security_auth_required": "1", "multi_org_enabled": "0"})
    settings.invalidate_cache()
    with Session(engine) as s:
        cx, cy = Customer(name="CustX"), Customer(name="CustY")
        s.add(cx)
        s.add(cy)
        s.commit()
        s.refresh(cx)
        s.refresh(cy)
        CX, CY = cx.id, cy.id
    users = {
        "adm": security.create_user("adm@example.test", "Adm", Role.ADMIN, PW),
        "m1": security.create_user("m1@example.test", "M1", Role.MANAGER, PW),
        "m2": security.create_user("m2@example.test", "M2", Role.MANAGER, PW),
        "bm": security.create_user("bm@example.test", "BM", Role.LINGUIST, PW, customer_id=CX),
        "tr": security.create_user("tr@example.test", "TR", Role.TRANSLATOR, PW),
        "vw": security.create_user("vw@example.test", "VW", Role.VIEWER, PW),
    }
    key = {k: apikeys.create_key(u.id, "n", scopes="api docs")[1] for k, u in users.items()}
    c = TestClient(app)

    def api(who, method, path, **kw):
        return c.request(method, path, headers={"X-Loc-Token": key[who]}, **kw)

    # [1] create -----------------------------------------------------------------------------
    r = api(
        "m1", "POST", "/api/custom-fields", json={"key": "m1_po", "label": "PO", "customer_id": CX}
    )
    assert r.status_code == 200, r.text
    fid_m1 = r.json()["id"]
    assert r.json()["customer_id"] == CX
    r = api("m1", "POST", "/api/custom-fields", json={"key": "m1_org", "label": "Org wide"})
    assert r.status_code == 403, r.text
    for who in ("tr", "vw"):
        r = api(
            who,
            "POST",
            "/api/custom-fields",
            json={"key": f"{who}_f", "label": "F", "customer_id": CX},
        )
        assert r.status_code == 403, (who, r.text)
    r = api("adm", "POST", "/api/custom-fields", json={"key": "adm_org", "label": "Org wide"})
    assert r.status_code == 200, r.text
    fid_org = r.json()["id"]
    r = api(
        "adm", "POST", "/api/custom-fields", json={"key": "adm_y", "label": "Y", "customer_id": CY}
    )
    assert r.status_code == 200, r.text
    fid_y = r.json()["id"]
    print(
        "  [1] manager creates customer-scoped, org-wide admin only, translator/viewer refused OK"
    )

    # [2] creator-or-admin ---------------------------------------------------------------------
    for body in ({"label": "Hijack"}, {"archived": True}):
        r = api("m2", "PATCH", f"/api/custom-fields/{fid_m1}", json=body)
        assert r.status_code == 403, (body, r.text)
    r = api("m1", "PATCH", f"/api/custom-fields/{fid_m1}", json={"label": "PO number"})
    assert r.status_code == 200 and r.json()["label"] == "PO number", r.text
    r = api("m1", "PATCH", f"/api/custom-fields/{fid_m1}", json={"archived": True})
    assert r.status_code == 200 and r.json()["archived"] is True, r.text
    r = api("m1", "PATCH", f"/api/custom-fields/{fid_m1}", json={"archived": False})
    assert r.status_code == 200, r.text
    for body in ({"label": "Nope"}, {"archived": True}):
        r = api("m1", "PATCH", f"/api/custom-fields/{fid_org}", json=body)
        assert r.status_code == 403, ("org-wide", body, r.text)
        r = api("m1", "PATCH", f"/api/custom-fields/{fid_y}", json=body)
        assert r.status_code == 403, ("admin-made customer field", body, r.text)
    r = api("adm", "PATCH", f"/api/custom-fields/{fid_m1}", json={"label": "PO (admin)"})
    assert r.status_code == 200, r.text
    r = api("vw", "PATCH", f"/api/custom-fields/{fid_m1}", json={"label": "x"})
    assert r.status_code == 403, r.text
    print("  [2] creator-or-admin edit/archive, org-wide and admin-made fields untouchable OK")

    # [2b] (gate) no scope escape: the creator cannot move its field to org-wide or to another
    # customer through PATCH, cannot name an unknown customer, and cannot restore a field an
    # admin archived in another customer
    for body in ({"customer_id": None}, {"customer_id": CY}, {"org_id": None}, {"key": "x"}):
        r = api("m1", "PATCH", f"/api/custom-fields/{fid_m1}", json=body)
        assert r.status_code == 400, (body, r.text)
    got = [f for f in api("adm", "GET", "/api/custom-fields").json()["fields"] if f["id"] == fid_m1]
    assert got and got[0]["customer_id"] == CX and got[0]["key"] == "m1_po", got
    r = api(
        "m1", "POST", "/api/custom-fields", json={"key": "m1_bad", "label": "B", "customer_id": 999}
    )
    assert r.status_code == 400, r.text
    assert (
        api("adm", "PATCH", f"/api/custom-fields/{fid_y}", json={"archived": True}).status_code
        == 200
    )
    r = api("m1", "PATCH", f"/api/custom-fields/{fid_y}", json={"archived": False})
    assert r.status_code == 403, r.text
    assert (
        api("adm", "PATCH", f"/api/custom-fields/{fid_y}", json={"archived": False}).status_code
        == 200
    )
    # the module CLI rows (operator-facing `field` descriptors) state the manager rule too
    from app import modules as M

    rows = dict(M.MODULE_CLI["custom_fields"])
    for leaf in ("field create", "field update"):
        assert "manager" in rows[leaf] and not rows[leaf].endswith("; admin)"), rows[leaf]
    print("  [2b] no scope move via PATCH, unknown customer 400, module CLI rows name managers OK")

    # [3] customer-bound accounts are never managers (create_user refuses it), so a bound
    # account is refused the whole definition surface, whatever customer it names
    r = api(
        "bm", "POST", "/api/custom-fields", json={"key": "bm_f", "label": "BM", "customer_id": CX}
    )
    assert r.status_code == 403, r.text
    for fid in (fid_m1, fid_y):
        assert (
            api("bm", "PATCH", f"/api/custom-fields/{fid}", json={"label": "x"}).status_code == 403
        )
    assert api("bm", "GET", "/api/custom-fields").status_code == 200
    print("  [3] customer-bound account refused the definition surface, still reads OK")

    # [4] web ----------------------------------------------------------------------------------
    w1 = _login("m1@example.test")
    r = w1.get("/settings/custom-fields")
    assert r.status_code == 200, r.status_code
    assert f"cf-edit-{fid_m1}" in r.text, "creator sees its edit form"
    assert f"cf-edit-{fid_org}" not in r.text and f"cf-edit-{fid_y}" not in r.text, (
        "edit forms shown for fields the manager may not change"
    )
    assert '<option value="">Every customer</option>' not in r.text
    r = w1.post(
        "/settings/custom-fields",
        data={"key": "web_ok", "label": "Web", "type": "text", "customer_id": str(CX)},
        follow_redirects=False,
    )
    assert r.status_code == 303 and "err=" not in r.headers["location"], r.headers
    r = w1.post(
        "/settings/custom-fields",
        data={"key": "web_org", "label": "Web org", "type": "text", "customer_id": ""},
        follow_redirects=False,
    )
    assert r.status_code == 403, r.status_code
    for path in (f"/{fid_org}/update", f"/{fid_org}/archive", f"/{fid_y}/archive"):
        r = w1.post("/settings/custom-fields" + path, data={"label": "x"}, follow_redirects=False)
        assert r.status_code == 403, (path, r.status_code)
    w2 = _login("m2@example.test")
    r = w2.post(
        f"/settings/custom-fields/{fid_m1}/update", data={"label": "steal"}, follow_redirects=False
    )
    assert r.status_code == 403, r.status_code
    r = w2.post(f"/settings/custom-fields/{fid_m1}/archive", follow_redirects=False)
    assert r.status_code == 403, r.status_code
    r = w2.post(f"/settings/custom-fields/{fid_m1}/restore", follow_redirects=False)
    assert r.status_code == 403, r.status_code
    r = w1.post(
        f"/settings/custom-fields/{fid_m1}/update", data={"label": "mine"}, follow_redirects=False
    )
    assert r.status_code == 303 and "err=" not in r.headers["location"], r.headers
    wv = _login("vw@example.test")
    assert wv.get("/settings/custom-fields").status_code == 403
    assert (
        wv.post(
            "/settings/custom-fields",
            data={"key": "v", "label": "v", "customer_id": str(CX)},
            follow_redirects=False,
        ).status_code
        == 403
    )
    wa = _login("adm@example.test")
    ra = wa.get("/settings/custom-fields")
    assert '<option value="">Every customer</option>' in ra.text
    assert f"cf-edit-{fid_org}" in ra.text and f"cf-edit-{fid_m1}" in ra.text
    wb = _login("bm@example.test")
    assert wb.get("/settings/custom-fields").status_code == 403
    print("  [4] web: manager scope, creator-or-admin, viewer refused, edit forms gated OK")

    # [5] audit --------------------------------------------------------------------------------
    with Session(engine) as s:
        rows = s.exec(select(AuditLog).where(AuditLog.actor == "m1@example.test")).all()
    actions = {a.action for a in rows}
    assert {"field.define", "field.update", "field.archive"} <= actions, actions
    print("  [5] audit rows unchanged OK")
    print("CUSTOM FIELDS MANAGERS OK")


if __name__ == "__main__":
    main()
