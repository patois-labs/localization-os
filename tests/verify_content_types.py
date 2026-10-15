"""Content types verification (SPEC-content-types, phase 1).

Hermetic + offline (throwaway LOC_TMS_DATA_DIR, provider keys blanked -> MockProvider).
ZERO live calls. Covers the phase-1 slice: the ContentType table + CRUD + scoping +
project-create/settings wiring across all three surfaces (service, CLI, /api).

Covers:

  schema
    - contenttype table + project.content_type_id / customer.default_content_type_id
      columns exist

  service CRUD + scoping
    - create -> row; rename; set_description; get/_require
    - unique-name constraint per (org, customer) scope — incl. the GLOBAL-vs-GLOBAL
      case the DB UNIQUE constraint alone misses (NULLs distinct) that create() catches
    - list_types / visible: org scope + R2-3 customer confinement
    - options_for_project / attachable: a project of customer A may attach GLOBAL or
      customer-A types, NEVER a customer-B type (the R2-3 confinement)

  delete FAILS CLOSED
    - refused (with a live reference count) while a Project references it
    - refused while a Customer.default references it
    - allowed once nothing references it

  project create + settings
    - valid content type attaches; invalid id rejected; a FOREIGN-customer type
      rejected (InvalidContentTypeError) — no orphan project left behind
    - customer default_content_type_id is INHERITED when the caller passes none
    - post-creation change via set_project_content_type + audit rows written; clearing
      works; existing rows keep their type (no restamp — nothing to restamp in phase 1)

  CLI parity (content-type create/list/rename/delete + project create --content-type
    + project set-content-type + customer set-default --content-type)

  /api parity (content-type CRUD + POST /api/projects content_type_id + POST
    /api/projects/{id}/content-type) + can_tune_ai gate + cross-org 404 isolation

  the BYTE-IDENTICAL invariant
    - with zero ContentType rows, project_create behaves identically (content_type_id
      stays NULL) — the backward-compat repo law

  multi-org isolation probe (gate on, throwaway DB only)

Final line: "CONTENT TYPES OK".
"""

import io
import json as _json
import os
import sys
from contextlib import redirect_stdout

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(_ROOT)
sys.path.insert(0, _ROOT)
from tests._bootstrap import init_env

init_env("loctms_ct_")

from sqlalchemy import inspect
from sqlmodel import Session, select

from app.cli.__main__ import main as cli_main
from app.db import engine, init_db
from app.models import (
    AuditLog,
    ContentType,
    Customer,
    Organization,
    Project,
    User,
)
from app.services import (
    apikeys,
    content,
    security,
    settings,
)
from app.services import (
    content_types as ct,
)
from app.services import (
    customers as customer_service,
)


def _fail(msg):
    print("FAIL:", msg)
    sys.exit(1)


# ============================================================================
def test_schema():
    insp = inspect(engine)
    names = set(insp.get_table_names())
    assert "contenttype" in names, names
    assert "content_type_id" in {c["name"] for c in insp.get_columns("project")}
    assert "default_content_type_id" in {c["name"] for c in insp.get_columns("customer")}
    print("  schema: OK")


def test_crud_and_uniqueness():
    with Session(engine) as s:
        c = ct.create(s, "Marketing", description="promo copy", created_by="lin@x.com")
        assert c.id is not None and c.customer_id is None and c.description == "promo copy"
        cid = c.id
    with Session(engine) as s:
        c = ct.rename(s, cid, "Marketing v2")
        assert c.name == "Marketing v2"
        c = ct.set_description(s, cid, "updated")
        assert c.description == "updated"
    # blank name rejected
    with Session(engine) as s:
        try:
            ct.create(s, "   ")
            _fail("blank name should reject")
        except ct.ContentTypeError:
            pass
    # GLOBAL duplicate rejected even though NULLs are distinct in the DB constraint
    with Session(engine) as s:
        try:
            ct.create(s, "Marketing v2")  # same global scope + name
            _fail("global duplicate name should reject")
        except ct.ContentTypeError:
            pass
    # a customer-scoped type with the SAME name as a global one is allowed (diff scope)
    with Session(engine) as s:
        cust = Customer(name="Acme CRUD")
        s.add(cust)
        s.commit()
        s.refresh(cust)
        c2 = ct.create(s, "Marketing v2", customer_id=cust.id)
        assert c2.customer_id == cust.id
        # but a SECOND customer-scoped dup for the same customer rejects
        try:
            ct.create(s, "Marketing v2", customer_id=cust.id)
            _fail("customer-scope duplicate should reject")
        except ct.ContentTypeError:
            pass
    print("  service CRUD + unique-name (incl. global-vs-global): OK")


def test_visibility_and_attachable():
    with Session(engine) as s:
        org1 = s.exec(select(Organization.id).order_by(Organization.id).limit(1)).first()
        org2 = Organization(name="CT Org 2")
        custA = Customer(name="Cust A", org_id=org1)
        custB = Customer(name="Cust B", org_id=org1)
        s.add(org2)
        s.add(custA)
        s.add(custB)
        s.commit()
        s.refresh(org2)
        s.refresh(custA)
        s.refresh(custB)
        g = ct.create(s, "GlobalVis", org_id=org1)  # global, org1
        a = ct.create(s, "AVis", org_id=org1, customer_id=custA.id)  # cust A
        b = ct.create(s, "BVis", org_id=org1, customer_id=custB.id)  # cust B
        o2 = ct.create(s, "Org2Vis", org_id=org2.id)  # other org
        gid, aid, bid, o2id = g.id, a.id, b.id, o2.id
        caid, _cbid = custA.id, custB.id
    with Session(engine) as s:
        # options_for_project: a cust-A project sees GLOBAL + A, never B/other-org
        opts = {t.id for t in ct.options_for_project(s, org_id=org1, customer_id=caid)}
        assert gid in opts and aid in opts, opts
        assert bid not in opts and o2id not in opts, opts
        # an un-customer project sees ONLY global
        opts_none = {t.id for t in ct.options_for_project(s, org_id=org1, customer_id=None)}
        assert opts_none == {gid}, opts_none
        # attachable mirrors it: A-project may attach global + A, not B
        assert ct.attachable(s, gid, org_id=org1, customer_id=caid) is not None
        assert ct.attachable(s, aid, org_id=org1, customer_id=caid) is not None
        assert ct.attachable(s, bid, org_id=org1, customer_id=caid) is None
        # cross-org attach blocked
        assert ct.attachable(s, o2id, org_id=org1, customer_id=caid) is None
        # visible() management seam: org mismatch -> None; customer-bound mismatch -> None
        assert ct.visible(s, o2id, org_id=org1) is None
        assert ct.visible(s, bid, org_id=org1, customer_id=caid) is None
        assert ct.visible(s, aid, org_id=org1, customer_id=caid) is not None
        # list_types org scope + customer confinement
        org1_all = {t.id for t in ct.list_types(s, org_id=org1)}
        assert o2id not in org1_all and gid in org1_all
        bound_a = {t.id for t in ct.list_types(s, org_id=org1, customer_id=caid)}
        assert bound_a == {aid}, bound_a
    print("  visibility + attachable (global/own/foreign-customer, cross-org): OK")


def test_delete_fails_closed():
    with Session(engine) as s:
        c = ct.create(s, "DelType")
        cid = c.id
        # reference it from a project
        p = content.project_create(s, "DelProj", target_locales="fr-FR", content_type_id=cid)
        assert p.content_type_id == cid
    with Session(engine) as s:
        refs = ct.reference_count(s, cid)
        assert refs["projects"] == 1 and refs["total"] == 1, refs
        try:
            ct.delete(s, cid)
            _fail("delete should fail closed while a project references it")
        except ct.ContentTypeError as exc:
            assert "still referenced" in str(exc), str(exc)
    # clear the project ref -> still blocked by a customer default ref
    with Session(engine) as s:
        p = s.exec(select(Project).where(Project.name == "DelProj")).first()
        p.content_type_id = None
        s.add(p)
        cust = Customer(name="DelCust")
        s.add(cust)
        s.commit()
        s.refresh(cust)
        customer_service.set_customer_defaults(s, cust.id, content_type_id=cid)
    with Session(engine) as s:
        refs = ct.reference_count(s, cid)
        assert refs["projects"] == 0 and refs["customer_defaults"] == 1, refs
        try:
            ct.delete(s, cid)
            _fail("delete should fail closed while a customer default references it")
        except ct.ContentTypeError:
            pass
    # clear the default -> delete now succeeds
    with Session(engine) as s:
        cust = s.exec(select(Customer).where(Customer.name == "DelCust")).first()
        cust.default_content_type_id = None
        s.add(cust)
        s.commit()
    with Session(engine) as s:
        ct.delete(s, cid)
        assert s.get(ContentType, cid) is None
    print("  delete FAILS CLOSED (project + customer-default refs, then allowed): OK")


def test_project_create_validation_and_inheritance():
    with Session(engine) as s:
        custA = Customer(name="PC Cust A")
        custB = Customer(name="PC Cust B")
        s.add(custA)
        s.add(custB)
        s.commit()
        s.refresh(custA)
        s.refresh(custB)
        caid, cbid = custA.id, custB.id
        glob = ct.create(s, "PC Global")
        a_type = ct.create(s, "PC A type", customer_id=caid)
        b_type = ct.create(s, "PC B type", customer_id=cbid)
        gid, aid, bid = glob.id, a_type.id, b_type.id
    # valid: global type on a cust-A project
    with Session(engine) as s:
        p = content.project_create(
            s, "PC valid", customer_id=caid, target_locales="fr-FR", content_type_id=gid
        )
        assert p.content_type_id == gid
    # valid: own-customer type
    with Session(engine) as s:
        p = content.project_create(
            s, "PC own", customer_id=caid, target_locales="fr-FR", content_type_id=aid
        )
        assert p.content_type_id == aid
    # invalid: a foreign-customer type on a cust-A project -> reject, NO orphan
    with Session(engine) as s:
        try:
            content.project_create(
                s, "PC foreign", customer_id=caid, target_locales="fr-FR", content_type_id=bid
            )
            _fail("foreign-customer content type must reject")
        except content.InvalidContentTypeError:
            pass
        assert s.exec(select(Project).where(Project.name == "PC foreign")).first() is None, (
            "no orphan project on rejected content type"
        )
    # invalid: a nonexistent id -> reject
    with Session(engine) as s:
        try:
            content.project_create(
                s, "PC bad", customer_id=caid, target_locales="fr-FR", content_type_id=999999
            )
            _fail("nonexistent content type must reject")
        except content.InvalidContentTypeError:
            pass
    # inheritance: customer default applied when caller passes none
    with Session(engine) as s:
        customer_service.set_customer_defaults(s, caid, content_type_id=aid)
    with Session(engine) as s:
        p = content.project_create(
            s, "PC inherit", customer_id=caid, target_locales="fr-FR"
        )  # no content_type_id
        assert p.content_type_id == aid, "customer default should be inherited"
    # an explicit choice overrides the default
    with Session(engine) as s:
        p = content.project_create(
            s, "PC override", customer_id=caid, target_locales="fr-FR", content_type_id=gid
        )
        assert p.content_type_id == gid, "explicit choice must override the default"
    print("  project create validation (valid/invalid/foreign) + inheritance: OK")


def test_set_project_content_type_and_audit():
    with Session(engine) as s:
        c = ct.create(s, "SettingsType")
        p = content.project_create(s, "SettingsProj", target_locales="fr-FR")
        assert p.content_type_id is None
        cid, pid = c.id, p.id
    with Session(engine) as s:
        content.set_project_content_type(s, pid, cid, actor="mgr@x.com")
        assert s.get(Project, pid).content_type_id == cid
    with Session(engine) as s:
        rows = s.exec(select(AuditLog).where(AuditLog.action == "project.content_type_set")).all()
        assert any(str(pid) in (r.target or "") for r in rows), "set must audit"
    # clear it
    with Session(engine) as s:
        content.set_project_content_type(s, pid, "", actor="mgr@x.com")
        assert s.get(Project, pid).content_type_id is None
    with Session(engine) as s:
        rows = s.exec(
            select(AuditLog).where(AuditLog.action == "project.content_type_cleared")
        ).all()
        assert rows, "clear must audit"
    # a foreign/invalid id on the settings path rejects (no write)
    with Session(engine) as s:
        try:
            content.set_project_content_type(s, pid, 999999)
            _fail("invalid content type on settings path must reject")
        except content.InvalidContentTypeError:
            pass
    print("  set_project_content_type + audit (set/clear/reject): OK")


def test_byte_identical_invariant():
    """With a content_type_id of None, project_create leaves content_type_id NULL —
    the backward-compat repo law (a project with no content type behaves as today)."""
    with Session(engine) as s:
        p = content.project_create(s, "PlainProj", target_locales="de-DE")
        assert p.content_type_id is None, "no content type => NULL, byte-identical"
    print("  byte-identical invariant (no content type => NULL): OK")


# --- CLI round-trip ----------------------------------------------------------
def _run_cli(argv):
    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = cli_main(argv)
    return rc, buf.getvalue()


def test_cli_round_trip():
    rc, out = _run_cli(["content-type", "create", "CLI Type", "--description", "from cli"])
    assert rc == 0, out
    with Session(engine) as s:
        c = s.exec(select(ContentType).where(ContentType.name == "CLI Type")).first()
        cid = c.id
    # list --json shows it
    rc, out = _run_cli(["--json", "content-type", "list"])
    assert rc == 0, out
    assert any(t["id"] == cid for t in _json.loads(out)["content_types"])
    # rename
    rc, out = _run_cli(["content-type", "rename", str(cid), "CLI Type 2"])
    assert rc == 0, out
    with Session(engine) as s:
        assert s.get(ContentType, cid).name == "CLI Type 2"
    # project create --content-type (2026-07-14: a project requires a customer
    # when one exists — make one and pass --customer-id; the GLOBAL content type
    # `cid` is attachable to any customer).
    rc, out = _run_cli(["--json", "customer", "create", "CLI CT Cust"])
    assert rc == 0, out
    cli_ct_cust = _json.loads(out)["id"]
    rc, out = _run_cli(
        [
            "--json",
            "project",
            "create",
            "CLI CT Proj",
            "--targets",
            "fr-FR",
            "--content-type",
            str(cid),
            "--customer-id",
            str(cli_ct_cust),
        ]
    )
    assert rc == 0, out
    proj_id = _json.loads(out)["id"]
    with Session(engine) as s:
        assert s.get(Project, proj_id).content_type_id == cid
    # project set-content-type --clear
    rc, out = _run_cli(["project", "set-content-type", str(proj_id), "--clear"])
    assert rc == 0, out
    with Session(engine) as s:
        assert s.get(Project, proj_id).content_type_id is None
    # project set-content-type <id>
    rc, out = _run_cli(["project", "set-content-type", str(proj_id), str(cid)])
    assert rc == 0, out
    with Session(engine) as s:
        assert s.get(Project, proj_id).content_type_id == cid
    # delete fails closed (project still references it) -> nonzero exit
    rc, out = _run_cli(["content-type", "delete", str(cid), "--yes"])
    assert rc != 0, ("delete should fail closed while referenced", out)
    # clear the ref, then delete succeeds
    _run_cli(["project", "set-content-type", str(proj_id), "--clear"])
    rc, out = _run_cli(["content-type", "delete", str(cid), "--yes"])
    assert rc == 0, out
    with Session(engine) as s:
        assert s.get(ContentType, cid) is None
    # customer set-default --content-type
    with Session(engine) as s:
        cust = Customer(name="CLI Default Cust")
        s.add(cust)
        s.commit()
        s.refresh(cust)
        gtype = ct.create(s, "CLI Default Type")
        custid, gtid = cust.id, gtype.id
    rc, out = _run_cli(["customer", "set-default", str(custid), "--content-type", str(gtid)])
    assert rc == 0, out
    with Session(engine) as s:
        assert s.get(Customer, custid).default_content_type_id == gtid
    print("  CLI round-trip (create/list/rename/delete + project + customer default): OK")


# --- /api parity + gates -----------------------------------------------------
_UN = 0


def _mk_user(role, org_id=None):
    global _UN
    _UN += 1
    # #181: org_id at creation (the seam refuses an org-less create under the gate).
    return security.create_user(
        f"ct{role}{_UN}@x.com", role.title(), role, "content-type-pw-123", org_id=org_id
    )


def _key(user):
    _row, raw = apikeys.create_key(user.id, "ct-test", scopes="api docs")
    return raw


def test_api_round_trip_and_gates():
    from fastapi.testclient import TestClient

    from app.main import app

    settings.set_many({"security_auth_required": "1", "multi_org_enabled": "0"})
    settings.invalidate_cache()
    try:
        c = TestClient(app)
        viewer = _mk_user("viewer")
        linguist = _mk_user("linguist")
        vh = {"X-Loc-Token": _key(viewer)}
        lh = {"X-Loc-Token": _key(linguist)}
        # CREATE gate = can_tune_ai: viewer 403, linguist ok
        assert c.post("/api/content-types", headers=vh, json={"name": "V CT"}).status_code == 403
        r = c.post("/api/content-types", headers=lh, json={"name": "API CT"})
        assert r.status_code == 200, r.text
        ctid = r.json()["id"]
        # list includes it
        r = c.get("/api/content-types", headers=lh)
        assert r.status_code == 200 and any(t["id"] == ctid for t in r.json()["content_types"])
        # rename
        r = c.post(f"/api/content-types/{ctid}/rename", headers=lh, json={"name": "API CT 2"})
        assert r.status_code == 200 and r.json()["name"] == "API CT 2", r.text
        # POST /api/projects with content_type_id attaches it. 2026-07-14: a
        # project requires a customer when one exists — make one (GLOBAL CT attaches
        # to any customer) and pass it.
        with Session(engine) as s:
            _apct_cust = content.customer_create(s, "ApiCtCust").id
        r = c.post(
            "/api/projects",
            headers=lh,
            json={
                "name": "API CT Proj",
                "targets": "fr-FR",
                "content_type_id": ctid,
                "customer_id": _apct_cust,
            },
        )
        assert r.status_code == 200, r.text
        pid = r.json()["id"]
        # POST /api/projects/{id}/content-type clears then sets
        r = c.post(f"/api/projects/{pid}/content-type", headers=lh, json={"content_type_id": ""})
        assert r.status_code == 200 and r.json()["content_type_id"] is None, r.text
        r = c.post(f"/api/projects/{pid}/content-type", headers=lh, json={"content_type_id": ctid})
        assert r.status_code == 200 and r.json()["content_type_id"] == ctid, r.text
        # delete fails closed (project references it) -> 400
        r = c.post(f"/api/content-types/{ctid}/delete", headers=lh)
        assert r.status_code == 400, ("delete must fail closed while referenced", r.text)
        # clear ref, then delete OK
        c.post(f"/api/projects/{pid}/content-type", headers=lh, json={"content_type_id": ""})
        r = c.post(f"/api/content-types/{ctid}/delete", headers=lh)
        assert r.status_code == 200 and r.json()["deleted"], r.text
        # a bad content_type_id on project create -> 400 (no 500). Pass a valid
        # customer so this genuinely exercises the bad-content-type path, not the
        # required-customer gate.
        r = c.post(
            "/api/projects",
            headers=lh,
            json={
                "name": "API bad",
                "targets": "fr-FR",
                "content_type_id": 987654,
                "customer_id": _apct_cust,
            },
        )
        assert r.status_code == 400, r.text
    finally:
        settings.set_many({"security_auth_required": "0", "multi_org_enabled": "0"})
        settings.invalidate_cache()
    print("  /api round-trip (CRUD + project attach/settings) + can_tune_ai gate: OK")


def test_api_cross_org_isolation():
    """Under multi-org, org A's token cannot see/rename/delete org B's content type."""
    from fastapi.testclient import TestClient

    from app.main import app

    settings.set_many({"security_auth_required": "1", "multi_org_enabled": "1"})
    settings.invalidate_cache()
    try:
        c = TestClient(app)
        with Session(engine) as s:
            oa = Organization(name="CT Org A")
            ob = Organization(name="CT Org B")
            s.add(oa)
            s.add(ob)
            s.commit()
            s.refresh(oa)
            s.refresh(ob)
            oaid, obid = oa.id, ob.id
        a = _mk_user("linguist", org_id=oaid)
        b = _mk_user("linguist", org_id=obid)
        with Session(engine) as s:
            ua, ub = s.get(User, a.id), s.get(User, b.id)
            ua.org_id, ub.org_id = oaid, obid
            s.add(ua)
            s.add(ub)
            s.commit()
        ha = {"X-Loc-Token": _key(a)}
        hb = {"X-Loc-Token": _key(b)}
        r = c.post("/api/content-types", headers=hb, json={"name": "Org B CT"})
        assert r.status_code == 200, r.text
        ctid = r.json()["id"]
        # org A cannot rename or delete org B's type -> 404
        assert (
            c.post(
                f"/api/content-types/{ctid}/rename", headers=ha, json={"name": "hijack"}
            ).status_code
            == 404
        )
        assert c.post(f"/api/content-types/{ctid}/delete", headers=ha).status_code == 404
        # org A's list does not include it
        r = c.get("/api/content-types", headers=ha)
        assert all(t["id"] != ctid for t in r.json()["content_types"]), r.text
    finally:
        settings.set_many({"security_auth_required": "0", "multi_org_enabled": "0"})
        settings.invalidate_cache()
    print("  /api cross-org isolation (404, list-excluded): OK")


# --- adversarial-review regression tests (2026-07-08 full-diff review) ------
def test_no_xss_in_delete_confirm():
    """Finding 1 (HIGH — stored XSS): a content-type NAME must never be interpolated
    into an inline on* JS handler. The browser HTML-decodes an attribute value BEFORE
    the JS parser runs, so a Jinja-escaped quote (&#39;) decodes back to ' and closes
    the string literal — a name like x');alert(document.cookie);// executes in a
    viewer's browser (a linguist-created name firing in a staff admin's browser on the
    Customer 360 page — a role/tenant boundary crossing; the app has no CSP). Render
    the global list + the Customer 360 section with a breakout-crafted name and assert
    the JS sentinel never lands inside ANY on* handler attribute value."""
    import re

    from fastapi.testclient import TestClient

    from app.main import app

    payload_name = "x');alert(document.cookie);//"
    with Session(engine) as s:
        ct.create(s, payload_name)  # global -> shows on /content-types
        cust = Customer(name="XSS Cust")
        s.add(cust)
        s.commit()
        s.refresh(cust)
        ct.create(s, payload_name, customer_id=cust.id)  # customer-scoped (diff scope OK)
        custid = cust.id
    c = TestClient(app)  # auth off -> can_edit true -> the delete forms render

    def _handler_values(body):
        return re.findall(r'\son[a-zA-Z]+\s*=\s*"([^"]*)"', body) + re.findall(
            r"\son[a-zA-Z]+\s*=\s*'([^']*)'", body
        )

    for url in ("/content-types", f"/customers/{custid}"):
        r = c.get(url)
        assert r.status_code == 200, (url, r.status_code)
        body = r.text
        # sanity: the crafted content-type row actually rendered, so the test really
        # exercises the vulnerable position (the sentinel has no HTML-special chars, so
        # Jinja leaves it verbatim wherever the name is placed).
        assert "alert(document.cookie)" in body, (url, "test did not exercise the row")
        for hv in _handler_values(body):
            assert "alert(document.cookie)" not in hv, (
                url,
                "content-type name interpolated into an on* handler (XSS)",
                hv,
            )
    print("  Finding 1 (XSS): name never lands in an on* handler (both pages): OK")


def test_duplicate_case_insensitive():
    """Finding 2 (MED): the duplicate check must be case-INSENSITIVE (its docstring
    claimed so but used a case-sensitive == ). Marketing/marketing/MARKETING must
    collide in the same scope (else glossaries/TM split across near-identical buckets
    in phases 2-3); a different scope may still reuse the name."""
    with Session(engine) as s:
        ct.create(s, "CaseProbe")
        for variant in ("caseprobe", "CASEPROBE ", "  CaseProbe  "):
            try:
                ct.create(s, variant)
                _fail(f"case-variant duplicate {variant!r} should reject")
            except ct.ContentTypeError:
                pass
        # rename collision is case-insensitive too (rename routes through _duplicate)
        other = ct.create(s, "CaseOther")
        try:
            ct.rename(s, other.id, "caseprobe")
            _fail("rename to a case-variant duplicate should reject")
        except ct.ContentTypeError:
            pass
    # a DIFFERENT scope (a customer) may reuse the name
    with Session(engine) as s:
        cust = Customer(name="CaseCust")
        s.add(cust)
        s.commit()
        s.refresh(cust)
        c2 = ct.create(s, "caseprobe", customer_id=cust.id)
        assert c2.id is not None
    print("  Finding 2: duplicate check case-insensitive (create + rename), scope-aware: OK")


def test_duplicate_non_ascii_case():
    """Finding 2 re-review regression: the fold must use the SAME engine as the DB
    index (both SQLite lower(), ASCII-only) — NOT Python's Unicode str.lower(). Mixing
    them let a real duplicate with a non-ASCII uppercase letter (CAFÉ, Cyrillic/Greek/
    Turkish — all plausible for a localization TMS) slip the friendly pre-check and hit
    the functional unique index as an UNCAUGHT IntegrityError -> HTTP 500. Assert the
    duplicate is now a friendly ContentTypeError at the service AND a clean redirect (no
    500) at the route, for both create() and rename(); a different scope still allows it.

    (Accented lower/upper like café vs CAFÉ are NOT folded by SQLite lower() and are
    legitimately distinct — the fixed pre-check agrees with the index on that; the
    regression is the IDENTICAL-name duplicate, tested below.)"""
    # NOTE ON SEMANTICS: the DB index folds with SQLite lower() (ASCII-only), so an
    # accented letter is NOT case-folded — "CAFÉ" and "café" are legitimately DISTINCT
    # names, and the fixed pre-check (also SQLite lower()) AGREES. The regression the
    # re-review found is an IDENTICAL non-ASCII-UPPERCASE name: pre-fix the pre-check
    # folded it with Python .lower() ("café") while the index used SQLite lower()
    # ("cafÉ"), so a real duplicate slipped the friendly check and hit the index as an
    # uncaught IntegrityError -> 500. Post-fix both fold identically -> friendly reject.
    with Session(engine) as s:
        ct.create(s, "CAFÉ")
        try:
            ct.create(s, "CAFÉ")  # IDENTICAL name -> a genuine duplicate
            _fail("identical non-ASCII duplicate 'CAFE' (accented) should reject friendly")
        except ct.ContentTypeError:
            pass
        # rename onto the identical existing name is likewise friendly (not a 500)
        other = ct.create(s, "CafeOther")
        try:
            ct.rename(s, other.id, "CAFÉ")
            _fail("rename to an identical non-ASCII duplicate should reject friendly")
        except ct.ContentTypeError:
            pass
    # a DIFFERENT scope (a customer) may still reuse the accented name
    with Session(engine) as s:
        cust = Customer(name="CafeCust")
        s.add(cust)
        s.commit()
        s.refresh(cust)
        assert ct.create(s, "CAFÉ", customer_id=cust.id).id is not None
    # HTTP level: a second POST with a non-ASCII-case duplicate must NOT 500. TestClient
    # (auth off -> can_edit) raises on a 500 by default, so a clean 303 redirect is the
    # pass condition. Use a fresh global name to avoid the rows created above.
    from fastapi.testclient import TestClient

    from app.main import app

    c = TestClient(app)
    r1 = c.post("/content-types", data={"name": "MÜNCHEN"}, follow_redirects=False)
    assert r1.status_code == 303, r1.status_code
    r2 = c.post("/content-types", data={"name": "MÜNCHEN"}, follow_redirects=False)
    assert r2.status_code == 303, ("duplicate create must redirect, not 500", r2.status_code)
    assert "err=" in (r2.headers.get("location") or ""), r2.headers.get("location")
    with Session(engine) as s:
        n = len(s.exec(select(ContentType).where(ContentType.name == "MÜNCHEN")).all())
        assert n == 1, ("exactly one row survived the duplicate POST", n)
    print("  Finding 2 re-review: non-ASCII-case duplicate friendly (service + no-500 route): OK")


def test_db_unique_index_enforced():
    """Finding 3 (MED): the DB itself must reject a duplicate (NULL-org, NULL-customer,
    name) single-tenant — a plain UNIQUE(org_id, customer_id, name) is INERT there
    (org_id NULL + NULLs distinct). Prove the FUNCTIONAL unique index is the real
    authority by BYPASSING the service (a raw insert the app-level _duplicate never
    sees), including the case-fold."""
    from sqlalchemy.exc import IntegrityError

    with Session(engine) as s:
        s.add(ContentType(name="DbIdxProbe"))  # global, org NULL, customer NULL
        s.commit()
    raised = False
    with Session(engine) as s:
        s.add(ContentType(name="dbidxprobe"))  # case-folded collision, same NULL scope
        try:
            s.commit()
        except IntegrityError:
            raised = True
            s.rollback()
    if not raised:
        _fail(
            "DB did not reject a case-folded duplicate GLOBAL content type — the "
            "functional unique index is missing or inert (NULLs distinct)"
        )
    print(
        "  Finding 3: DB functional unique index enforces scope+name (NULL-collapsed, "
        "case-folded): OK"
    )


def test_reference_count_scoping():
    """Finding 4 (LOW, defense-in-depth): reference_count accepts OPTIONAL org/customer
    filters so a future phase-2/3 scoped caller can confine the count (delete() still
    calls it unscoped = counts everything = safest fail-closed)."""
    with Session(engine) as s:
        org1 = s.exec(select(Organization.id).order_by(Organization.id).limit(1)).first()
        c = ct.create(s, "RefScopeProbe")
        cid = c.id
        p = Project(name="RefScopeProj", source_locale="en-US", org_id=org1, content_type_id=cid)
        s.add(p)
        s.commit()
        other = Organization(name="RefScope Other Org")
        s.add(other)
        s.commit()
        s.refresh(other)
        other_id = other.id
    with Session(engine) as s:
        assert ct.reference_count(s, cid)["projects"] == 1  # unscoped
        assert ct.reference_count(s, cid, org_id=org1)["projects"] == 1  # matching org
        # a DIFFERENT org's filter excludes the reference (the assert-filter works)
        assert ct.reference_count(s, cid, org_id=other_id)["projects"] == 0
    print("  Finding 4: reference_count optional org/customer scope filter: OK")


def main():
    init_db()
    test_schema()
    test_crud_and_uniqueness()
    test_visibility_and_attachable()
    test_delete_fails_closed()
    test_project_create_validation_and_inheritance()
    test_set_project_content_type_and_audit()
    test_byte_identical_invariant()
    test_cli_round_trip()
    test_api_round_trip_and_gates()
    test_api_cross_org_isolation()
    # adversarial-review regression tests (2026-07-08 full-diff review)
    test_no_xss_in_delete_confirm()
    test_duplicate_case_insensitive()
    test_duplicate_non_ascii_case()
    test_db_unique_index_enforced()
    test_reference_count_scoping()
    print("CONTENT TYPES OK")


if __name__ == "__main__":
    main()
