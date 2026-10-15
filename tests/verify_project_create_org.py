"""project_create: the customer and the org stamp follow the caller's org (offline, throwaway dir).

Issue #554 (split from #455). Under an ACTIVE multi-org gate:

  [1] an explicit customer_id that belongs to ANOTHER org is refused on every door (service
      ForeignCustomerError, /api 404 (#580), web flash, CLI exit 2) and no project row is written;
      the caller's own org customer still works.
  [2] a caller that carries an org but no User (an org-bound principal, or the in-process
      `--org` scope) stamps the new project with that org instead of leaving org_id NULL.
  [3] a NULL-org customer is quarantined from a scoped caller (same_org rule), and the
      operator's unscoped CLI door keeps working.
  [4] gate OFF (every install today): an explicit customer_id and a creator-less create
      behave exactly as before (org_id stays NULL, any customer accepted).

Multi-org stays OFF on every real install; this suite only turns it on in a throwaway data dir.
"""

import hashlib
import logging
import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(_ROOT)
sys.path.insert(0, _ROOT)
from tests._bootstrap import init_env

init_env("loctms_pc_org_")

for _n in ("httpx", "httpcore"):
    logging.getLogger(_n).setLevel(logging.WARNING)

from fastapi.testclient import TestClient
from sqlmodel import Session, select

from app.db import engine, init_db
from app.main import app
from app.models import Customer, Organization, Project, Role
from app.services import access, apikeys, content, security, settings
from tests._cli_runner import run_cli
from tests._csrf import csrf_for, session_data

ADMIN_A = "pcorg-a@example.com"
ADMIN_B = "pcorg-b@example.com"
PW = "PcOrg-Admin-Pass-2026-x"  # pregate: allow-secret throwaway fixture login
OP_SECRET = "pcorg-operator-door-value-123"  # pregate: allow-secret throwaway operator key

FIX = {}


def _fixture():
    init_db()
    with Session(engine) as s:
        for key in ("a", "b"):
            org = Organization(name=f"Org {key} pcorg")
            s.add(org)
            s.commit()
            s.refresh(org)
            FIX[f"org_{key}"] = org.id
        for key in ("a", "b"):
            cust = Customer(name=f"Cust {key}", org_id=FIX[f"org_{key}"])
            s.add(cust)
            s.commit()
            s.refresh(cust)
            FIX[f"cust_{key}"] = cust.id
        legacy = Customer(name="Legacy no org", org_id=None)
        s.add(legacy)
        s.commit()
        s.refresh(legacy)
        FIX["cust_null"] = legacy.id
    security.create_user(ADMIN_A, "Admin A", Role.ADMIN, PW, org_id=FIX["org_a"])
    security.create_user(ADMIN_B, "Admin B", Role.ADMIN, PW, org_id=FIX["org_b"])
    with Session(engine) as s:
        from app.models import User

        uid_a = s.exec(select(User).where(User.email == ADMIN_A)).first().id
    _row, raw = apikeys.create_key(uid_a, "pcorg-a", scopes="api docs admin")
    FIX["hdr_a"] = {"X-Loc-Token": raw}
    settings.set_many(
        {
            "security_auth_required": "1",
            "operator_key_hash": hashlib.sha256(OP_SECRET.encode()).hexdigest(),
        }
    )


def _set_gate(on: bool):
    # THROWAWAY data dir only (init_env): multi-org is never enabled on a real install.
    assert "loctms_pc_org_" in os.environ["LOC_TMS_DATA_DIR"]
    settings.set_many({"multi_org_enabled": "1" if on else "0"})
    assert access.multi_org_active() is on


def _login(email) -> TestClient:
    c = TestClient(app)
    c.get("/auth/login")
    pre = session_data(c).get("csrf")
    r = c.post(
        "/auth/login",
        data={"email": email, "password": PW, "next": "/", "csrf_token": pre},
        follow_redirects=False,
    )
    assert r.status_code == 303, r.status_code
    c.get("/settings")
    csrf_for(c)
    return c


def _count(name):
    with Session(engine) as s:
        return len(s.exec(select(Project).where(Project.name == name)).all())


def _project(name):
    with Session(engine) as s:
        return s.exec(select(Project).where(Project.name == name)).first()


def test_1_foreign_customer_refused_everywhere():
    _set_gate(True)
    api = TestClient(app)
    # /api: org A admin key, customer of org B -> refused, nothing written. #580 (merged after
    # this batch was built) answers it at the door with the shared 404 body, so the response
    # is not an existence oracle; the service check below is the wall behind it.
    r = api.post(
        "/api/projects",
        headers=FIX["hdr_a"],
        json={"name": "pc-api-foreign", "customer_id": FIX["cust_b"], "targets": ["fr-FR"]},
    )
    assert r.status_code == 404, (r.status_code, r.text[:200])
    assert _count("pc-api-foreign") == 0
    # The refusal never names the other tenant's customer.
    assert "Cust b" not in r.text
    # Control: the caller's own customer works and the project carries the caller's org.
    r = api.post(
        "/api/projects",
        headers=FIX["hdr_a"],
        json={"name": "pc-api-own", "customer_id": FIX["cust_a"], "targets": ["fr-FR"]},
    )
    assert r.status_code == 200, (r.status_code, r.text[:200])
    assert _project("pc-api-own").org_id == FIX["org_a"]
    # Web form: same refusal as a flash, no row.
    c = _login(ADMIN_A)
    r = c.post(
        "/projects",
        data={
            "name": "pc-web-foreign",
            "source_locale": "en-US",
            "targets": "fr-FR",
            "customer_id": str(FIX["cust_b"]),
        },
        follow_redirects=False,
    )
    assert r.status_code == 303 and "err=" in r.headers.get("location", ""), (
        r.status_code,
        r.headers.get("location"),
    )
    assert _count("pc-web-foreign") == 0
    # Service: ForeignCustomerError, caller scope = the creator's org.
    with Session(engine) as s:
        from app.models import User

        creator = s.exec(select(User).where(User.email == ADMIN_A)).first()
        try:
            content.project_create(s, "pc-svc-foreign", customer_id=FIX["cust_b"], creator=creator)
        except content.ForeignCustomerError:
            pass
        else:
            raise AssertionError("service accepted a customer from another org")
    assert _count("pc-svc-foreign") == 0
    # CLI --org A with org B's customer: exit 2, no row; own customer works.
    code, out, err = run_cli(
        [
            "--org",
            str(FIX["org_a"]),
            "project",
            "create",
            "pc-cli-foreign",
            "--customer-id",
            str(FIX["cust_b"]),
        ]
    )
    assert code == 2, (code, out, err)
    assert _count("pc-cli-foreign") == 0
    code, out, err = run_cli(
        [
            "--org",
            str(FIX["org_a"]),
            "project",
            "create",
            "pc-cli-own",
            "--customer-id",
            str(FIX["cust_a"]),
        ]
    )
    assert code == 0, (code, out, err)
    assert _project("pc-cli-own").org_id == FIX["org_a"]
    print("  [1] a customer from another org is refused on service, /api, web and CLI")


def test_2_org_without_user_stamps_the_org():
    _set_gate(True)
    with Session(engine) as s:
        p = content.project_create(
            s, "pc-token-org", customer_id=FIX["cust_a"], creator=None, org_id=FIX["org_a"]
        )
        assert p is not None and p.org_id == FIX["org_a"], getattr(p, "org_id", None)
        # The poison id of an org-less principal stamps nothing and never persists -1.
        # (A customer cannot be resolved for it either, so no customer is passed.)
        q = content.project_create(
            s, "pc-token-blocked", creator=None, org_id=access.BLOCKED_ORG_ID
        )
        assert q is not None and q.org_id is None, q.org_id
        # An org-bound token may not reach another org's customer either.
        try:
            content.project_create(
                s, "pc-token-foreign", customer_id=FIX["cust_b"], creator=None, org_id=FIX["org_a"]
            )
        except content.ForeignCustomerError:
            pass
        else:
            raise AssertionError("org-bound principal reached another org's customer")
    assert _count("pc-token-foreign") == 0
    print("  [2] an org-bound principal with no User stamps its org and stays in it")


def test_2b_template_apply_forwards_the_org():
    """Review: apply_template did not hand its org to project_create, so an org-bound
    principal with no User applying a template stamped org_id NULL and skipped the customer
    org check."""
    _set_gate(True)
    from app.services import templates as tsvc

    with Session(engine) as s:
        src = content.project_create(
            s,
            "pc-tpl-src",
            target_locales="fr-FR",
            customer_id=FIX["cust_a"],
            creator=None,
            org_id=FIX["org_a"],
        )
        tpl = tsvc.save_as_template(s, src.id, "pc-tpl", scope="global", org_id=FIX["org_a"])
        proj, _w = tsvc.apply_template(
            s,
            tpl.id,
            "pc-tpl-own",
            customer_id=str(FIX["cust_a"]),
            creator=None,
            org_id=FIX["org_a"],
            require_customer=True,
        )
        assert proj.org_id == FIX["org_a"], proj.org_id
        try:
            tsvc.apply_template(
                s,
                tpl.id,
                "pc-tpl-foreign",
                customer_id=str(FIX["cust_b"]),
                creator=None,
                org_id=FIX["org_a"],
                require_customer=True,
            )
        except tsvc.TemplateError:
            pass
        else:
            raise AssertionError("template apply reached another org's customer")
    assert _count("pc-tpl-foreign") == 0
    print("  [2b] template apply stamps the caller's org and refuses another org's customer")


def test_3_null_org_customer_quarantined_and_operator_door():
    _set_gate(True)
    with Session(engine) as s:
        try:
            content.project_create(
                s, "pc-null-cust", customer_id=FIX["cust_null"], creator=None, org_id=FIX["org_a"]
            )
        except content.ForeignCustomerError:
            pass
        else:
            raise AssertionError("a scoped caller bound a NULL-org (quarantined) customer")
    assert _count("pc-null-cust") == 0
    os.environ["LOC_TMS_OPERATOR_KEY"] = OP_SECRET
    try:
        code, out, err = run_cli(
            ["--operator", "project", "create", "pc-op-any", "--customer-id", str(FIX["cust_b"])]
        )
        assert code == 0, (code, out, err)
    finally:
        os.environ.pop("LOC_TMS_OPERATOR_KEY", None)
    assert _project("pc-op-any").customer_id == FIX["cust_b"]
    print("  [3] a NULL-org customer is quarantined; the operator door still crosses orgs")


def test_4_gate_off_unchanged():
    _set_gate(False)
    api = TestClient(app)
    r = api.post(
        "/api/projects",
        headers=FIX["hdr_a"],
        json={"name": "pc-off-any", "customer_id": FIX["cust_b"], "targets": ["fr-FR"]},
    )
    assert r.status_code == 200, (r.status_code, r.text[:200])
    assert _project("pc-off-any").customer_id == FIX["cust_b"]
    with Session(engine) as s:
        p = content.project_create(
            s, "pc-off-token", customer_id=FIX["cust_null"], creator=None, org_id=FIX["org_a"]
        )
        assert p is not None and p.org_id is None, p.org_id
    code, out, err = run_cli(
        ["project", "create", "pc-off-cli", "--customer-id", str(FIX["cust_b"])]
    )
    assert code == 0, (code, out, err)
    print("  [4] gate off: any customer accepted, org_id stays NULL (byte-identical)")


def main():
    _fixture()
    try:
        test_1_foreign_customer_refused_everywhere()
        test_2_org_without_user_stamps_the_org()
        test_2b_template_apply_forwards_the_org()
        test_3_null_org_customer_quarantined_and_operator_door()
    finally:
        settings.set_many({"multi_org_enabled": "0"})
    test_4_gate_off_unchanged()
    print("verify_project_create_org OK")


if __name__ == "__main__":
    main()
