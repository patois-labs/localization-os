"""Multi-org scoping gaps from the PR 472 review, issue #488 (offline, throwaway dir).

Each item was reported with medium confidence; this suite proves each against the code first.

  [1] vendor leg routes: the vendor's owner (org A) cannot download, poll, import a return into,
      or approve a quote on a dispatch leg whose BUYER org is B (404 on every leg route; the
      vendor's own org's leg still works).
  [2] workflow groups: POST /workflow-groups refuses another org's customer (404, no group),
      never names it (not even when it is archived), turns a nonexistent id into a 404 instead
      of an FK 500, and /workflows/author/confirm and /repair refuse a foreign customer before
      the archived-customer error can name it. The service and CLI agree.
  [3] customer default workflow: another org's GLOBAL workflow cannot become a customer's
      default (web and service); the customer's own org's global workflow still can.
  [4] language catalog: an org admin cannot disable a language for every org (the host-only
      enabled_locales key, #479); proven here so the #488 item can be ticked off.
  [5] templates saved by a customer-bound caller (web and /api) land in that customer, never as
      a global template every customer sees. Reachable with multi-org OFF, so it runs gate off.

Multi-org stays OFF on every real install; this suite only turns it on in a throwaway data dir.
"""

import logging
import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(_ROOT)
sys.path.insert(0, _ROOT)
from tests._bootstrap import init_env

init_env("loctms_mo_488_")

for _n in ("httpx", "httpcore"):
    logging.getLogger(_n).setLevel(logging.WARNING)

from fastapi.testclient import TestClient
from sqlmodel import Session, select

from app.db import engine, init_db
from app.main import app
from app.models import (
    Customer,
    Document,
    Organization,
    Project,
    ProjectTemplate,
    Role,
    User,
    Vendor,
    VendorDispatch,
    VendorDispatchStatus,
    Workflow,
    WorkflowGroup,
    WorkflowRun,
)
from app.services import access, apikeys, customers, languages_catalog, security, settings
from app.services import workflows as wf_service
from tests._cli_runner import run_cli
from tests._csrf import csrf_for, session_data

ADMIN_A = "mo488-a@example.com"
ADMIN_B = "mo488-b@example.com"
BOUND_A = "mo488-bound-a@example.com"
PW = "Mo488-Admin-Pass-2026-x"  # pregate: allow-secret throwaway fixture login

FIX = {}


def _fixture():
    init_db()
    with Session(engine) as s:
        for key in ("a", "b"):
            org = Organization(name=f"Org {key} 488")
            s.add(org)
            s.commit()
            s.refresh(org)
            FIX[f"org_{key}"] = org.id
        for key in ("a", "b"):
            cust = Customer(name=f"Cust {key} 488", org_id=FIX[f"org_{key}"])
            s.add(cust)
            s.commit()
            s.refresh(cust)
            FIX[f"cust_{key}"] = cust.id
        arch = Customer(name="Archived Foreign 488", org_id=FIX["org_b"])
        s.add(arch)
        s.commit()
        s.refresh(arch)
        arch.archived_at = arch.created_at
        s.add(arch)
        s.commit()
        FIX["cust_b_arch"] = arch.id
        legacy = Customer(name="Legacy NullOrg 488", org_id=None)
        s.add(legacy)
        s.commit()
        s.refresh(legacy)
        FIX["cust_null"] = legacy.id
        # Projects, a document and a run per org (for the vendor legs).
        for key in ("a", "b"):
            proj = Project(
                name=f"Proj {key} 488",
                customer_id=FIX[f"cust_{key}"],
                org_id=FIX[f"org_{key}"],
                source_locale="en-US",
            )
            s.add(proj)
            s.commit()
            s.refresh(proj)
            FIX[f"proj_{key}"] = proj.id
            doc = Document(project_id=proj.id, filename=f"d_{key}.md", fmt="md")
            s.add(doc)
            s.commit()
            s.refresh(doc)
            wf = Workflow(name=f"wf {key} 488", scope="global", org_id=FIX[f"org_{key}"])
            s.add(wf)
            s.commit()
            s.refresh(wf)
            FIX[f"gwf_{key}"] = wf.id
            run = WorkflowRun(workflow_id=wf.id, document_id=doc.id, target_locale="fr-FR")
            s.add(run)
            s.commit()
            s.refresh(run)
            FIX[f"run_{key}"] = run.id
            FIX[f"doc_{key}"] = doc.id
        vendor = Vendor(name="Owner A Vendor 488", org_id=FIX["org_a"])
        s.add(vendor)
        s.commit()
        s.refresh(vendor)
        FIX["vendor_a"] = vendor.id
        for key in ("a", "b"):
            leg = VendorDispatch(
                vendor_id=vendor.id,
                workflow_run_id=FIX[f"run_{key}"],
                document_id=FIX[f"doc_{key}"],
                target_locale="fr-FR",
                status=VendorDispatchStatus.DISPATCHED,
                org_id=FIX[f"org_{key}"],
                claim_token=f"tok-{key}-488",
            )
            s.add(leg)
            s.commit()
            s.refresh(leg)
            FIX[f"leg_{key}"] = leg.id
    security.create_user(ADMIN_A, "Admin A", Role.ADMIN, PW, org_id=FIX["org_a"])
    security.create_user(ADMIN_B, "Admin B", Role.ADMIN, PW, org_id=FIX["org_b"])
    security.create_user(
        BOUND_A, "Bound A", Role.TRANSLATOR, PW, customer_id=FIX["cust_a"], org_id=FIX["org_a"]
    )
    with Session(engine) as s:
        uid_a = s.exec(select(User).where(User.email == ADMIN_A)).first().id
        uid_bound = s.exec(select(User).where(User.email == BOUND_A)).first().id
    _r, raw = apikeys.create_key(uid_a, "mo488-a", scopes="api docs admin")
    FIX["hdr_a"] = {"X-Loc-Token": raw}
    _r, raw = apikeys.create_key(uid_bound, "mo488-bound", scopes="api docs")
    FIX["hdr_bound"] = {"X-Loc-Token": raw}
    settings.set_many(
        {"security_auth_required": "1", "remote_admin_enabled": "1", "vendors_enabled": "1"}
    )


def _set_gate(on: bool):
    # THROWAWAY data dir only (init_env): multi-org is never enabled on a real install.
    assert "loctms_mo_488_" in os.environ["LOC_TMS_DATA_DIR"]
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


# --------------------------------------------------------------------------- #
def test_1_vendor_leg_routes_are_buyer_org_scoped():
    _set_gate(True)
    c = _login(ADMIN_A)
    v = FIX["vendor_a"]
    foreign, own = FIX["leg_b"], FIX["leg_a"]
    r = c.get(f"/vendors/{v}/dispatches/{foreign}/xliff", follow_redirects=False)
    assert r.status_code == 404, ("xliff", r.status_code, r.headers.get("location"))
    r = c.post(f"/vendors/{v}/dispatches/{foreign}/poll", follow_redirects=False)
    assert r.status_code == 404, ("poll", r.status_code, r.headers.get("location"))
    r = c.post(
        f"/vendors/{v}/dispatches/{foreign}/return",
        data={"payload": "{}"},
        follow_redirects=False,
    )
    assert r.status_code == 404, ("return", r.status_code, r.headers.get("location"))
    r = c.post(f"/vendors/{v}/dispatches/{foreign}/approve-quote", follow_redirects=False)
    assert r.status_code == 404, ("approve-quote", r.status_code, r.headers.get("location"))
    # Control: the vendor's own org's leg is reachable (any answer but the 404 wall).
    r = c.get(f"/vendors/{v}/dispatches/{own}/xliff", follow_redirects=False)
    assert r.status_code != 404, ("own leg xliff", r.status_code)
    r = c.post(f"/vendors/{v}/dispatches/{own}/poll", follow_redirects=False)
    assert r.status_code != 404, ("own leg poll", r.status_code)
    with Session(engine) as s:
        assert s.get(VendorDispatch, foreign).status == VendorDispatchStatus.DISPATCHED
    print("  [1] vendor leg xliff/poll/return/quote routes 404 on another buyer org's leg")


def test_2_workflow_groups_refuse_foreign_customers():
    _set_gate(True)
    c = _login(ADMIN_A)

    def _groups(name):
        with Session(engine) as s:
            return len(s.exec(select(WorkflowGroup).where(WorkflowGroup.name == name)).all())

    for label, cid in (
        ("foreign", FIX["cust_b"]),
        ("foreign-archived", FIX["cust_b_arch"]),
        ("null-org", FIX["cust_null"]),
        ("nonexistent", 987654),
    ):
        name = f"grp-{label}"
        r = c.post(
            "/workflow-groups",
            data={"name": name, "customer_id": str(cid)},
            follow_redirects=False,
        )
        assert r.status_code == 404, (label, r.status_code, r.text[:200])
        assert "Archived Foreign" not in r.text and "Cust b" not in r.text, label
        assert _groups(name) == 0, label
    r = c.post(
        "/workflow-groups",
        data={"name": "grp-own", "customer_id": str(FIX["cust_a"])},
        follow_redirects=False,
    )
    assert r.status_code == 303, (r.status_code, r.text[:200])
    assert _groups("grp-own") == 1
    # The author review routes: the foreign customer is refused BEFORE the archived error
    # can name it (cross-tenant enumeration oracle).
    for path in ("/workflows/author/confirm", "/workflows/author/repair"):
        r = c.post(
            path,
            data={"customer_id": str(FIX["cust_b_arch"]), "name": "x", "step_count": "0"},
            follow_redirects=False,
        )
        assert r.status_code == 404, (path, r.status_code, r.text[:200])
        assert "Archived Foreign" not in r.text, path
    # Service seam: a concrete other org and a nonexistent customer are refused.
    with Session(engine) as s:
        for cid in (FIX["cust_b"], 987654):
            try:
                wf_service.create_group(s, "grp-svc", customer_id=cid, org_id=FIX["org_a"])
            except wf_service.WorkflowForeignCustomerError:
                pass
            else:
                raise AssertionError(f"create_group accepted customer {cid}")
    assert _groups("grp-svc") == 0
    # CLI, scoped to org A: foreign customer is a clean usage error.
    code, out, err = run_cli(
        [
            "--org",
            str(FIX["org_a"]),
            "workflow",
            "group",
            "create",
            "--name",
            "grp-cli",
            "--customer-id",
            str(FIX["cust_b"]),
        ]
    )
    assert code == 2, (code, out, err)
    assert "no such customer" in err + out, (out, err)  # a real refusal, not an argparse error
    assert _groups("grp-cli") == 0
    foreign_answer = (out + err).replace(str(FIX["cust_b"]), "<id>")
    # Scoping gate LOW: an ARCHIVED customer of another org gets the SAME answer as a live
    # foreign one (never "customer X is archived", which names it), for group and workflow
    # create alike. An own-org archived customer still gets the archived message.
    for verb, label in (
        (["group", "create", "--name", "grp-cli-arch"], "group"),
        (["create", "--name", "wf-cli-arch"], "workflow"),
    ):
        code2, out2, err2 = run_cli(
            [
                "--org",
                str(FIX["org_a"]),
                "workflow",
                *verb,
                "--customer-id",
                str(FIX["cust_b_arch"]),
            ]
        )
        assert code2 == 2, (label, code2, out2, err2)
        text = out2 + err2
        assert "Archived Foreign" not in text and "archived" not in text.lower(), (label, text)
        assert text.replace(str(FIX["cust_b_arch"]), "<id>") == foreign_answer, (
            label,
            text,
            foreign_answer,
        )
    assert _groups("grp-cli-arch") == 0
    # Review gate: a non-canonical id ("0" + id) must echo identically for a live foreign, an
    # archived foreign and a nonexistent customer (the pre-check echoed the coerced id while
    # the service's not-found mapping echoed the raw one: an existence oracle).
    for verb in (["group", "create", "--name", "grp-cli-pad"], ["create", "--name", "wf-cli-pad"]):
        answers = []
        for cid in (FIX["cust_b"], FIX["cust_b_arch"], 987654):
            code3, out3, err3 = run_cli(
                ["--org", str(FIX["org_a"]), "workflow", *verb, "--customer-id", "0" + str(cid)]
            )
            assert code3 == 2, (verb, code3, out3, err3)
            answers.append((out3 + err3).replace("0" + str(cid), "<id>"))
        assert len(set(answers)) == 1 and "<id>" in answers[0], (verb, answers)
    print("  [2]workflow groups refuse foreign / archived-foreign / NULL-org / missing customers")


def test_2b_cli_archived_customer_paths():
    """Review of the scoping LOW: the foreign check must not break the legitimate paths.
    Own-org archived still gets the archived message; a NULL-org customer is refused with
    the same not-found answer as a foreign one (fail closed, like the web route); the
    key-verified --operator door and the gate-off single-tenant path keep today's
    behavior (archived message, no org check)."""
    import hashlib

    _set_gate(True)
    with Session(engine) as s:
        own = Customer(name="Own Archived 488", org_id=FIX["org_a"])
        s.add(own)
        s.commit()
        s.refresh(own)
        own.archived_at = own.created_at
        s.add(own)
        s.commit()
        own_id = own.id
    org = ["--org", str(FIX["org_a"]), "workflow"]
    verbs = (["group", "create", "--name", "g2b"], ["create", "--name", "w2b"])
    code, out, err = run_cli(
        [*org, "group", "create", "--name", "g2b", "--customer-id", str(FIX["cust_b"])]
    )
    foreign = (out + err).replace(str(FIX["cust_b"]), "<id>")
    assert code == 2 and "no such customer" in foreign, (code, out, err)
    for verb in verbs:
        code, out, err = run_cli([*org, *verb, "--customer-id", str(own_id)])
        assert code == 2 and "is archived" in out + err and "Own Archived 488" in out + err, (
            verb,
            out,
            err,
        )
        code, out, err = run_cli([*org, *verb, "--customer-id", str(FIX["cust_null"])])
        assert code == 2, (verb, code, out, err)
        assert (out + err).replace(str(FIX["cust_null"]), "<id>") == foreign, (verb, out, err)
        assert "Legacy NullOrg" not in out + err
    with Session(engine) as s:
        assert not s.exec(select(WorkflowGroup).where(WorkflowGroup.name == "g2b")).all()
    # --operator (key verified): unscoped, so the archived customer is the archived message.
    secret = "op-key-488-throwaway"
    settings.set_many({"operator_key_hash": hashlib.sha256(secret.encode()).hexdigest()})
    settings.invalidate_cache()
    os.environ["LOC_TMS_OPERATOR_KEY"] = secret
    try:
        for verb in verbs:
            code, out, err = run_cli(
                ["--operator", "workflow", *verb, "--customer-id", str(FIX["cust_b_arch"])]
            )
            assert code == 2 and "is archived" in out + err, (verb, code, out, err)
    finally:
        os.environ.pop("LOC_TMS_OPERATOR_KEY", None)
        settings.set_many({"operator_key_hash": ""})
        settings.invalidate_cache()
    # Gate off: single-tenant, no org check at all.
    _set_gate(False)
    try:
        for verb in verbs:
            code, out, err = run_cli(["workflow", *verb, "--customer-id", str(FIX["cust_b_arch"])])
            assert code == 2 and "is archived" in out + err, (verb, code, out, err)
            # Review gate: a nonexistent customer is a clean usage error, never a traceback.
            code, out, err = run_cli(["workflow", *verb, "--customer-id", "987654"])
            assert code == 2 and "no such customer: 987654" in out + err, (verb, code, out, err)
    finally:
        _set_gate(True)
    print(
        "  [2b] CLI archived-customer: own-org message kept, NULL-org fail-closed,"
        " --operator + gate-off unchanged"
    )


def test_3_customer_default_workflow_is_org_checked():
    _set_gate(True)
    c = _login(ADMIN_A)
    cust = FIX["cust_a"]
    r = c.post(
        f"/customers/{cust}/settings",
        data={"default_workflow_id": str(FIX["gwf_b"])},
        follow_redirects=False,
    )
    assert r.status_code == 303 and "err=" in r.headers.get("location", ""), (
        r.status_code,
        r.headers.get("location"),
    )
    with Session(engine) as s:
        assert s.get(Customer, cust).default_workflow_id is None, "another org's workflow stored"
        try:
            customers.set_customer_defaults(s, cust, workflow_id=str(FIX["gwf_b"]))
        except ValueError:
            pass
        else:
            raise AssertionError("service stored another org's global workflow as default")
    r = c.post(
        f"/customers/{cust}/settings",
        data={"default_workflow_id": str(FIX["gwf_a"])},
        follow_redirects=False,
    )
    assert r.status_code == 303 and "saved=1" in r.headers.get("location", ""), (
        r.status_code,
        r.headers.get("location"),
    )
    with Session(engine) as s:
        assert s.get(Customer, cust).default_workflow_id == FIX["gwf_a"]
        s.get(Customer, cust).default_workflow_id = None
        s.add(s.get(Customer, cust))
        s.commit()
    print("  [3] a customer default workflow must live in the customer's own org")


def test_4_language_catalog_is_host_only():
    _set_gate(True)
    before = list(languages_catalog.enabled_locales())
    api = TestClient(app)
    code = before[0] if before else "en-US"
    for verb in ("disable", "enable"):
        r = api.post(f"/api/languages/{code}/{verb}", headers=FIX["hdr_a"])
        assert r.status_code == 403, (verb, r.status_code, r.text[:200])
    assert list(languages_catalog.enabled_locales()) == before
    print("  [4] the language catalog is host-only for an org admin (#479)")


def test_5_bound_caller_templates_stay_in_their_customer():
    _set_gate(False)
    api = TestClient(app)
    r = api.post(
        "/api/templates",
        headers=FIX["hdr_bound"],
        json={"name": "tpl-api-bound", "project_id": FIX["proj_a"], "scope": "global"},
    )
    assert r.status_code == 200, (r.status_code, r.text[:200])
    with Session(engine) as s:
        tpl = s.exec(select(ProjectTemplate).where(ProjectTemplate.name == "tpl-api-bound")).first()
        assert tpl is not None and tpl.customer_id == FIX["cust_a"], tpl.customer_id
    # A project of another customer reads as not found for the bound key.
    r = api.post(
        "/api/templates",
        headers=FIX["hdr_bound"],
        json={"name": "tpl-api-foreign", "project_id": FIX["proj_b"], "scope": "global"},
    )
    assert r.status_code == 404, (r.status_code, r.text[:200])
    c = _login(BOUND_A)
    r = c.post(
        f"/projects/{FIX['proj_a']}/save-template",
        data={"name": "tpl-web-bound", "scope": "global"},
        follow_redirects=False,
    )
    assert r.status_code == 303 and "saved=1" in r.headers.get("location", ""), (
        r.status_code,
        r.headers.get("location"),
    )
    with Session(engine) as s:
        tpl = s.exec(select(ProjectTemplate).where(ProjectTemplate.name == "tpl-web-bound")).first()
        assert tpl is not None and tpl.customer_id == FIX["cust_a"], tpl and tpl.customer_id
    # Internal staff keep the global choice (byte-identical).
    r = api.post(
        "/api/templates",
        headers=FIX["hdr_a"],
        json={"name": "tpl-staff-global", "project_id": FIX["proj_a"], "scope": "global"},
    )
    assert r.status_code == 200, (r.status_code, r.text[:200])
    with Session(engine) as s:
        tpl = s.exec(
            select(ProjectTemplate).where(ProjectTemplate.name == "tpl-staff-global")
        ).first()
        assert tpl is not None and tpl.customer_id is None
    print("  [5] a customer-bound caller's template is its own customer's (web + /api), gate off")


def main():
    _fixture()
    try:
        test_1_vendor_leg_routes_are_buyer_org_scoped()
        test_2_workflow_groups_refuse_foreign_customers()
        test_2b_cli_archived_customer_paths()
        test_3_customer_default_workflow_is_org_checked()
        test_4_language_catalog_is_host_only()
    finally:
        settings.set_many({"multi_org_enabled": "0"})
    test_5_bound_caller_templates_stay_in_their_customer()
    print("verify_multiorg_scoping_488 OK")


if __name__ == "__main__":
    main()
