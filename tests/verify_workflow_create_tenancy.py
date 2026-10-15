"""Workflow-create customer_id tenancy (chip task_56b8b2bf) -- hermetic.

THE BUG (pre-existing): POST /workflows (web) stamped the form's customer_id straight
onto the new Workflow with NO org/customer check, while POST /api/workflows funnelled it
through _api_workflow_customer (org ring + bound confinement). So a customer-A-bound web
user -- or a staff user in org A -- could PLANT a workflow into another customer/org's
ring (a 200 it could then never see). Closed by hoisting the tenancy guard to the SHARED
create_workflow seam (WorkflowForeignCustomerError) + a web route pre-check, so BOTH
surfaces refuse the cross-tenant customer_id with the foreign-tenant convention (404).

Pinned:
  1. SERVICE SEAM: create_workflow refuses a bound-caller's foreign customer and a
     cross-ORG customer (multi-org), accepts own-org/own-customer, and is inert
     single-tenant (byte-identical). fail-before/pass-after via git show HEAD.
  2. WEB: a logged-in org-A user is 404'd naming an org-B customer, and a bound user is
     404'd naming a sibling customer -- and NO workflow row is created either way.
  3. API: the /api route still 404s the same cross-org customer_id (regression pin).

Final line: "WORKFLOW CREATE TENANCY OK".
"""

import importlib.util
import os
import subprocess
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(_ROOT)
sys.path.insert(0, _ROOT)
from tests._bootstrap import init_env

_TMP = init_env("loctms_wfcusttenancy_")

from fastapi.testclient import TestClient
from sqlmodel import Session, select

from app.db import engine, init_db
from app.main import app
from app.models import Customer, Organization, Role, User, Workflow
from app.services import apikeys, security, settings
from app.services import workflows as wf
from tests._csrf import csrf_for

init_db()


def _fail(msg):
    print("FAIL:", msg)
    sys.exit(1)


def _org(name):
    with Session(engine) as s:
        o = Organization(name=name)
        s.add(o)
        s.commit()
        s.refresh(o)
        return o.id


def _customer(name, org_id):
    with Session(engine) as s:
        c = Customer(name=name, org_id=org_id)
        s.add(c)
        s.commit()
        s.refresh(c)
        return c.id


def _wf_count_for_customer(cid):
    with Session(engine) as s:
        return len(s.exec(select(Workflow).where(Workflow.customer_id == cid)).all())


# ============================================================================ #
# 1. service seam
# ============================================================================ #
def test_service_seam():
    org_a, org_b = _org("SvcOrg A"), _org("SvcOrg B")
    cust_a = _customer("Svc Cust A", org_a)
    cust_b = _customer("Svc Cust B", org_b)

    # bound caller may only name its own customer
    with Session(engine) as s:
        try:
            wf.create_workflow(
                s,
                "planty",
                scope="customer",
                customer_id=str(cust_b),
                org_id=org_a,
                bound_customer_id=cust_a,
            )
        except wf.WorkflowForeignCustomerError:
            pass
        else:
            _fail("a bound caller naming a foreign customer must raise")
    if _wf_count_for_customer(cust_b) != 0:
        _fail("a refused bound create must write nothing")

    # multi-org: a customer that EXISTS in another org is refused (org_id given)
    with Session(engine) as s:
        try:
            wf.create_workflow(
                s, "crossorg", scope="customer", customer_id=str(cust_b), org_id=org_a
            )
        except wf.WorkflowForeignCustomerError:
            pass
        else:
            _fail("a cross-org customer must raise when org_id is given")
    if _wf_count_for_customer(cust_b) != 0:
        _fail("a refused cross-org create must write nothing")

    # own-org / own-customer is fine
    with Session(engine) as s:
        w = wf.create_workflow(
            s,
            "legit",
            scope="customer",
            customer_id=str(cust_a),
            org_id=org_a,
            bound_customer_id=cust_a,
        )
        if w is None or w.customer_id != cust_a:
            _fail("an own-customer create must succeed and stamp the customer")

    # single-tenant (org_id None, unbound) with a valid customer is byte-identical
    with Session(engine) as s:
        w = wf.create_workflow(s, "single", scope="customer", customer_id=str(cust_b))
        if w is None or w.customer_id != cust_b:
            _fail("single-tenant create with a valid customer must be unchanged")
    print("  1. service seam refuses bound/cross-org, accepts own, inert single-tenant: OK")


def test_service_fail_before_pass_after():
    org_a, org_b = _org("HeadOrg A"), _org("HeadOrg B")
    cust_b = _customer("Head Cust B", org_b)
    old = _head_module("app/services/workflows.py", "_head_wf")
    if old is not None and _no_tenancy_guard(old):
        with Session(engine) as s:
            w = old.create_workflow(
                s, "prefix-plant", scope="customer", customer_id=str(cust_b), org_id=org_a
            )
            planted = w is not None and w.customer_id == cust_b
        if not planted:
            _fail("pre-fix create_workflow was expected to PLANT the foreign customer")
        print("  1b. pre-fix create_workflow stamped a cross-org customer (the bug): OK")
    else:
        print(
            "  1b. HEAD already carries the guard (or git/import unavailable) -- "
            "post-fix delta asserted in test 1"
        )


def _no_tenancy_guard(mod) -> bool:
    """True when the loaded module has NO WorkflowForeignCustomerError (pre-fix)."""
    return not hasattr(mod, "WorkflowForeignCustomerError")


def _head_module(rel_path: str, mod_name: str):
    try:
        src = subprocess.run(
            ["git", "show", f"HEAD~1:{rel_path}"], cwd=_ROOT, capture_output=True, timeout=60
        )
        # HEAD~1 is the commit before the chip-3 commit lands; if that fails (this
        # suite runs pre-commit), fall back to HEAD.
        if src.returncode != 0 or not src.stdout:
            src = subprocess.run(
                ["git", "show", f"HEAD:{rel_path}"], cwd=_ROOT, capture_output=True, timeout=60
            )
    except Exception:
        return None
    if src.returncode != 0 or not src.stdout:
        return None
    path = os.path.join(_TMP, mod_name + ".py")
    with open(path, "wb") as fh:
        fh.write(src.stdout)
    spec = importlib.util.spec_from_file_location(mod_name, path)
    mod = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(mod)
    except Exception:
        return None
    return mod


# ============================================================================ #
# 2 + 3. web + api surfaces (multi-org gate ON)
# ============================================================================ #
def _login(c, who, pw):
    csrf_for(c)
    r = c.post(
        "/auth/login", data={"email": who, "password": pw, "next": "/"}, follow_redirects=False
    )
    if r.status_code == 303:
        csrf_for(c)
    return r


def test_web_and_api_surfaces():
    org_a, org_b = _org("WebOrg A"), _org("WebOrg B")
    cust_a1 = _customer("Web Cust A1", org_a)
    cust_a2 = _customer("Web Cust A2", org_a)
    cust_b = _customer("Web Cust B", org_b)

    # staff admin in org A; a customer-A1-bound user in org A
    staff = security.create_user("wfct-staff@x.com", "Staff", Role.ADMIN, "wfct-staff-pass-1")
    bound = security.create_user("wfct-bound@x.com", "Bound", Role.ADMIN, "wfct-bound-pass-1")
    with Session(engine) as s:
        su = s.get(User, staff.id)
        su.org_id = org_a
        bu = s.get(User, bound.id)
        bu.org_id = org_a
        bu.customer_id = cust_a1
        s.add(su)
        s.add(bu)
        s.commit()
    _row, staff_token = apikeys.create_key(staff.id, "wfct-api", scopes="api docs")

    settings.set_many({"security_auth_required": "1", "multi_org_enabled": "1"})
    settings.invalidate_cache()
    try:
        # --- WEB: staff in org A naming an org-B customer -> 404, nothing created ---
        cs = TestClient(app)
        assert _login(cs, "wfct-staff@x.com", "wfct-staff-pass-1").status_code == 303
        r = cs.post(
            "/workflows",
            data={"name": "cross-org-web", "scope": "customer", "customer_id": str(cust_b)},
            follow_redirects=False,
        )
        if r.status_code != 404:
            _fail(f"web staff cross-org create must 404, got {r.status_code}")
        if _wf_count_for_customer(cust_b) != 0:
            _fail("web cross-org create must write nothing")

        # --- WEB: own-org customer -> 303 created ---
        r = cs.post(
            "/workflows",
            data={"name": "own-org-web", "scope": "customer", "customer_id": str(cust_a1)},
            follow_redirects=False,
        )
        if r.status_code != 303:
            _fail(f"web own-org create must 303, got {r.status_code}")
        if _wf_count_for_customer(cust_a1) < 1:
            _fail("web own-org create must create the workflow")

        # --- WEB: bound (customer A1) user naming a SIBLING customer A2 -> 404 ---
        cb = TestClient(app)
        assert _login(cb, "wfct-bound@x.com", "wfct-bound-pass-1").status_code == 303
        r = cb.post(
            "/workflows",
            data={"name": "bound-sibling-web", "scope": "customer", "customer_id": str(cust_a2)},
            follow_redirects=False,
        )
        if r.status_code != 404:
            _fail(f"web bound-caller sibling-customer create must 404, got {r.status_code}")
        if _wf_count_for_customer(cust_a2) != 0:
            _fail("web bound cross-customer create must write nothing")

        # --- M2 (mutation-visible): a NULL-org (unstamped/legacy) customer plant.
        # The SERVICE guard is concrete-org-only (it must not over-reject NULL-org), so
        # the WEB route's _wf_create_customer_block (access.same_org 404s a NULL-org row
        # from a scoped caller) is the ONLY thing standing between a scoped staff user
        # and planting a workflow onto an unstamped customer. Deleting the block would
        # make this 303 + a planted row -> this asserts the block, not just the service.
        with Session(engine) as s:
            null_org_cust = Customer(name="Unstamped Cust", org_id=None)
            s.add(null_org_cust)
            s.commit()
            s.refresh(null_org_cust)
            null_cid = null_org_cust.id
        r = cs.post(
            "/workflows",
            data={"name": "nullorg-plant", "scope": "customer", "customer_id": str(null_cid)},
            follow_redirects=False,
        )
        if r.status_code != 404:
            _fail(f"web NULL-org customer plant must 404 (the block), got {r.status_code}")
        if _wf_count_for_customer(null_cid) != 0:
            _fail("web NULL-org plant must write nothing")

        # --- M2 (name-leak ordering): a FOREIGN ARCHIVED customer must 404 (tenancy
        # first), NOT a 400 that echoes its name (the H1 cross-org enumeration oracle).
        with Session(engine) as s:
            import datetime as _dt

            arch = Customer(
                name="Foreign Archived Secret",
                org_id=org_b,
                archived_at=_dt.datetime.now(_dt.timezone.utc).replace(tzinfo=None),
            )
            s.add(arch)
            s.commit()
            s.refresh(arch)
            arch_cid = arch.id
        r = cs.post(
            "/workflows",
            data={"name": "foreign-archived", "scope": "customer", "customer_id": str(arch_cid)},
            follow_redirects=False,
        )
        if r.status_code != 404:
            _fail(
                f"foreign archived customer must 404 (tenancy before archived-400), "
                f"got {r.status_code}"
            )
        if "Foreign Archived Secret" in r.text:
            _fail("the refusal must NOT leak the foreign customer's name (enumeration oracle)")
        print(
            "  2. web: cross-org 404 + bound-sibling 404 + NULL-org-plant 404 + "
            "foreign-archived 404 (no name leak), own-org 303: OK"
        )

        # --- API: staff key in org A naming an org-B customer -> 404 (regression pin) ---
        capi = TestClient(app)
        h = {"X-Loc-Token": staff_token}
        r = capi.post(
            "/api/workflows",
            json={"name": "cross-org-api", "scope": "customer", "customer_id": cust_b},
            headers=h,
        )
        if r.status_code != 404:
            _fail(f"api staff cross-org create must 404, got {r.status_code} {r.text}")
        if _wf_count_for_customer(cust_b) != 0:
            _fail("api cross-org create must write nothing")
        # own-org over /api -> 200
        r = capi.post(
            "/api/workflows",
            json={"name": "own-org-api", "scope": "customer", "customer_id": cust_a1},
            headers=h,
        )
        if r.status_code != 200 or r.json().get("customer_id") != cust_a1:
            _fail(f"api own-org create must 200 + stamp customer, got {r.status_code} {r.text}")
        print("  3. api: cross-org 404, own-org 200 (regression pin): OK")
    finally:
        settings.set_many({"security_auth_required": "0", "multi_org_enabled": "0"})
        settings.invalidate_cache()


def main():
    test_service_seam()
    test_service_fail_before_pass_after()
    test_web_and_api_surfaces()
    print("WORKFLOW CREATE TENANCY OK")


if __name__ == "__main__":
    main()
