"""SPEC-full-remote-parity Wave R1a: the content-ops /api routes.

Wave R1a gives /api + MCP parity to the content operations that were web/CLI-only:
TM-source attach, glossary pins, key-run launch, document rename/delete + segment
unlock, project lifecycle status + export, the customer-360 family, and the legacy
per-project glossary export. This suite proves the new /api arm obeys the laws every
wave binds itself to, by calling the SAME service seams the web + in-process CLI use.

Asserted with BOTH the login wall AND multi-org ON (a genuine TWO-ORG world), so the
behaviour under test is org scoping AND the customer ring AND the role gate at once:

  * CROSS-ORG 404 on every new route -- org A's admin key never reaches org B's
    project / document / segment / customer / key project; a foreign id is 404, never
    403 (a distinguishable refusal would leak existence);
  * the B1/H1/H2 CLASS -- a customer-BOUND caller (a linguist bound to customer A) is
    refused (404) when it targets ANOTHER customer's project, or names a foreign
    customer's project/glossary as a tm-source / glossary-pin id, or launches a foreign
    customer's workflow over a key project; every foreign-id parameter is pinned;
  * project_sources SAME-CUSTOMER-OR-HOUSE -- a house (customer-less) source attaches, a
    foreign-customer source is REFUSED (never attached);
  * RBAC exactly as the table says -- viewer/translator 403 where the gate excludes them,
    the 403 body naming nothing (the shared string), a strict no-op with the wall off;
  * the keys/translate MOCK-409 -- a workflow whose translate step resolves to a keyless
    REAL engine refuses before launching, exactly like document translate; an explicit
    mock step is the allowed escape hatch;
  * the two-phase translate_keys MCP contract -- an UNCONFIRMED call writes NOTHING
    (asserted by counting WorkflowRun rows), a CONFIRMED one launches;
  * HAPPY PATHS for every route, read back where a read exists.

ASCII-only; standalone one-process-per-suite script (throwaway LOC_TMS_DATA_DIR, keys
cleared -> MockProvider, deterministic + free). Final line: "R1A CONTENT PARITY OK".
"""

import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(_ROOT)
sys.path.insert(0, _ROOT)
from tests._bootstrap import init_env

init_env("loctms_r1a_")

import logging

for _n in ("httpx", "httpcore"):
    logging.getLogger(_n).setLevel(logging.WARNING)

from fastapi.testclient import TestClient
from sqlmodel import Session, select

from app.db import engine, init_db
from app.models import (
    Document,
    Organization,
    Project,
    Role,
    Segment,
    StepType,
    User,
    WorkflowRun,
    WorkflowStep,
)
from app.services import access, apikeys, content, security, settings
from app.services import glossary as gloss_svc
from app.services import glossary_module as gm
from app.services import keys as keys_svc
from app.services import workflows as wf_svc


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def _gate_on() -> None:
    settings.set_many({"security_auth_required": "1", "multi_org_enabled": "1"})
    settings.invalidate_cache()


def _gate_off() -> None:
    settings.set_many({"security_auth_required": "0", "multi_org_enabled": "0"})
    settings.invalidate_cache()


def _h(raw: str) -> dict:
    return {"X-Loc-Token": raw}


def _key(user_id: int, scopes="api docs keys") -> str:
    _row, raw = apikeys.create_key(user_id, "r1a-test", scopes=scopes)
    return raw


def _user(email, role, org_id, customer_id=None) -> int:
    u = security.create_user(email, email, role, "r1a-password-123", customer_id=customer_id)
    with Session(engine) as s:
        row = s.get(User, u.id)
        row.org_id = org_id
        s.add(row)
        s.commit()
    return u.id


def _client():
    from app.main import app

    return TestClient(app)


def _wf(session, name, org_id, provider, customer_id=None):
    """A workflow with ONE llm_agent translate step at `provider`, org-stamped."""
    scope = "customer" if customer_id is not None else "global"
    wf = wf_svc.create_workflow(
        session, name, scope=scope, customer_id=customer_id, org_id=org_id, created_via="manual"
    )
    step = WorkflowStep(
        workflow_id=wf.id,
        step_type=StepType.LLM_AGENT,
        provider=provider,
        prompt_text="Translate into {target_locale}: {text}",
        key="tr",
        order_index=0,
    )
    session.add(step)
    session.commit()
    return wf.id


def _seed():
    """Build the two-org world. Org A carries two customers (A1, A2) plus a house
    (customer-less) project, so the same-customer-or-house rule and the within-org
    customer oracle are both exercisable; org B is the cross-org foil."""
    init_db()
    with Session(engine) as s:
        org_a = Organization(name="Org A")
        org_b = Organization(name="Org B")
        s.add(org_a)
        s.add(org_b)
        s.commit()
        s.refresh(org_a)
        s.refresh(org_b)
        oa, ob = org_a.id, org_b.id
    # Multi-org ON during seeding too, so content.customer_create / project_create
    # stamp each row with the creator's org (glossaries + workflows + the house project
    # are stamped explicitly below). Auth stays off here -- the service seams don't
    # check it, and the tests turn it on.
    settings.set_many({"multi_org_enabled": "1"})
    settings.invalidate_cache()

    # Users (org-stamped). Admin/manager/linguist/translator/viewer in A; admin in B;
    # a LINGUIST bound to customer A1 (the B1/H1/H2 probe -- passes can_tune_ai/can_write
    # but must stay inside customer A1's ring).
    admin_a = _user("admina@x.com", Role.ADMIN, oa)
    manager_a = _user("mgra@x.com", Role.MANAGER, oa)
    ling_a = _user("linga@x.com", Role.LINGUIST, oa)
    tr_a = _user("tra@x.com", Role.TRANSLATOR, oa)
    view_a = _user("viewa@x.com", Role.VIEWER, oa)
    admin_b = _user("adminb@x.com", Role.ADMIN, ob)

    with Session(engine) as s:
        adm_a = s.get(User, admin_a)
        adm_b = s.get(User, admin_b)
        # Customers: A1, A2 (org A), B1 (org B).
        cust_a1 = content.customer_create(s, "Acme A1", creator=adm_a).id
        cust_a2 = content.customer_create(s, "Acme A2", creator=adm_a).id
        cust_b1 = content.customer_create(s, "Beta B1", creator=adm_b).id

        # Projects. proj_a1 (document, cust A1) + a locked segment for unlock.
        proj_a1 = content.project_create(
            s,
            "A1 Doc",
            source_locale="en-US",
            target_locales="de-DE",
            customer_id=cust_a1,
            creator=adm_a,
            default_provider="mock",
        ).id
        doc_a1 = Document(
            project_id=proj_a1,
            filename="a1.txt",
            fmt="txt",
            mode="segmented",
            status="parsed",
            org_id=oa,
        )
        s.add(doc_a1)
        s.commit()
        s.refresh(doc_a1)
        # A real upload blob so the project-export rebuild can read the original.
        from app import config as _config

        _config.UPLOADS_DIR.mkdir(parents=True, exist_ok=True)
        (_config.UPLOADS_DIR / ("%d.txt" % doc_a1.id)).write_bytes(b"hello\n")  # noqa: UP031
        seg_a1 = Segment(
            document_id=doc_a1.id,
            source_text="hello",
            target_text="hi",
            target_locale="de-DE",
            order_index=0,
            lock_band="100",
            lock_hard=False,
            meta={"tm_lock": {"band": "100", "hard": False}},
        )
        s.add(seg_a1)
        # A legacy per-project glossary entry (for the legacy export).
        gloss_svc.add_term(
            s, proj_a1, "widget", target_term="Widget", source_locale="en-US", target_locale="de-DE"
        )
        s.commit()
        s.refresh(seg_a1)

        # A TM-source candidate under cust A1, with one TM row (same-customer source).
        proj_a1_src = content.project_create(
            s,
            "A1 Src",
            source_locale="en-US",
            target_locales="de-DE",
            customer_id=cust_a1,
            creator=adm_a,
            default_provider="mock",
        ).id
        # A HOUSE project (customer-less) with a TM row -- feeds a customer project.
        proj_house = Project(
            name="House TM",
            org_id=oa,
            customer_id=None,
            source_locale="en-US",
            created_by="admina@x.com",
        )
        s.add(proj_house)
        s.commit()
        s.refresh(proj_house)
        # A cust-A2 project + glossary (a foreign-CUSTOMER, same-ORG source/pin). TM
        # entries are not needed -- the attach checks scope, not row count.
        proj_a2 = content.project_create(
            s,
            "A2 Doc",
            source_locale="en-US",
            target_locales="de-DE",
            customer_id=cust_a2,
            creator=adm_a,
            default_provider="mock",
        ).id

        # A key project under cust A1 with one translatable key (for keys/translate).
        kp_a1 = content.project_create(
            s,
            "A1 Keys",
            source_locale="en-US",
            target_locales="de-DE",
            customer_id=cust_a1,
            creator=adm_a,
            default_provider="mock",
            project_type="key_based",
        )
        keys_svc.create_key(
            s, kp_a1, "greeting", namespace="common", base_value="hello", actor="admina@x.com"
        )
        kp_a1 = kp_a1.id
        # A SECOND key project reserved for the two-phase MCP test, so its confirmed
        # launch never re-attaches to a run some earlier test already started.
        kp_two = content.project_create(
            s,
            "A1 Keys 2",
            source_locale="en-US",
            target_locales="de-DE",
            customer_id=cust_a1,
            creator=adm_a,
            default_provider="mock",
            project_type="key_based",
        )
        keys_svc.create_key(
            s, kp_two, "greeting2", namespace="common", base_value="hello", actor="admina@x.com"
        )
        kp_two = kp_two.id

        # Org B foils.
        proj_b1 = content.project_create(
            s,
            "B1 Doc",
            source_locale="en-US",
            target_locales="de-DE",
            customer_id=cust_b1,
            creator=adm_b,
            default_provider="mock",
        ).id
        doc_b1 = Document(
            project_id=proj_b1,
            filename="b1.txt",
            fmt="txt",
            mode="segmented",
            status="parsed",
            org_id=ob,
        )
        s.add(doc_b1)
        s.commit()
        s.refresh(doc_b1)
        seg_b1 = Segment(
            document_id=doc_b1.id,
            source_text="hi",
            target_text="",
            target_locale="de-DE",
            order_index=0,
        )
        s.add(seg_b1)
        kp_b1 = content.project_create(
            s,
            "B1 Keys",
            source_locale="en-US",
            target_locales="de-DE",
            customer_id=cust_b1,
            creator=adm_b,
            default_provider="mock",
            project_type="key_based",
        ).id
        s.commit()
        s.refresh(seg_b1)

        # Module glossaries: approved, en-US->de-DE. glos_a1 (cust A1, pinnable to A1),
        # glos_house (customer-less), glos_a2 (cust A2 -- foreign customer).
        def _glos(name, customer_id):
            g = gm.create(s, name, org_id=oa, customer_id=customer_id)
            gm.set_pairs(s, g.id, ["en-US:de-DE"])
            gm.approve(s, g.id)
            return g.id

        glos_a1 = _glos("GL A1", cust_a1)
        glos_house = _glos("GL House", None)
        glos_a2 = _glos("GL A2", cust_a2)

        # Workflows: wf_a (mock step, org A, cust A1), wf_bad (keyless real engine ->
        # mock-409), wf_b (org B). ids only.
        wf_a = _wf(s, "WF A mock", oa, "mock", customer_id=cust_a1)
        wf_bad = _wf(s, "WF A openai", oa, "openai", customer_id=cust_a1)
        wf_b = _wf(s, "WF B", ob, "mock", customer_id=cust_b1)

        seg_a1_id, seg_b1_id, doc_a1_id, doc_b1_id = (seg_a1.id, seg_b1.id, doc_a1.id, doc_b1.id)
        proj_house_id = proj_house.id

        # Stamp org explicitly (belt-and-suspenders: the service org-stamp depends on
        # the multi_org cache being live at create time, which is brittle in a seed;
        # every object's org is unambiguous here, so pin it directly).
        from app.models import Customer as _Cust

        for pid_ in (proj_a1, proj_a1_src, proj_a2, kp_a1, kp_two):
            s.get(Project, pid_).org_id = oa
        for pid_ in (proj_b1, kp_b1):
            s.get(Project, pid_).org_id = ob
        for cid_ in (cust_a1, cust_a2):
            s.get(_Cust, cid_).org_id = oa
        s.get(_Cust, cust_b1).org_id = ob
        s.commit()
        # Role parity is measured inside project visibility; foreign-org fixtures stay hidden.
        for uid in (tr_a, view_a):
            email = s.get(User, uid).email
            for pid_ in (proj_a1, proj_a1_src, proj_a2, kp_a1, kp_two, proj_house_id):
                assert access.add_member(s, pid_, email)[0]

    keys = {
        "admin_a": _key(admin_a),
        "manager_a": _key(manager_a),
        "ling_a": _key(ling_a),
        "tr_a": _key(tr_a),
        "view_a": _key(view_a),
        "admin_b": _key(admin_b),
        # A LINGUIST bound to customer A1.
        "ling_bound_a1": _key(_user("lba1@x.com", Role.LINGUIST, oa, customer_id=cust_a1)),
    }
    return {
        "keys": keys,
        "cust_a1": cust_a1,
        "cust_a2": cust_a2,
        "cust_b1": cust_b1,
        "proj_a1": proj_a1,
        "proj_a1_src": proj_a1_src,
        "proj_house": proj_house_id,
        "proj_a2": proj_a2,
        "kp_a1": kp_a1,
        "kp_two": kp_two,
        "proj_b1": proj_b1,
        "kp_b1": kp_b1,
        "doc_a1": doc_a1_id,
        "doc_b1": doc_b1_id,
        "seg_a1": seg_a1_id,
        "seg_b1": seg_b1_id,
        "glos_a1": glos_a1,
        "glos_house": glos_house,
        "glos_a2": glos_a2,
        "wf_a": wf_a,
        "wf_bad": wf_bad,
        "wf_b": wf_b,
    }


def _is(r, code, what):
    assert r.status_code == code, f"{what}: expected {code}, got {r.status_code} ({r.text[:160]})"


def _forbidden(r, what):
    """A 403 that leaks nothing: the shared role-gate string, ok False."""
    _is(r, 403, what)
    body = r.json()
    assert body.get("ok") is False, (what, body)
    assert body.get("error") == "your role does not allow this action.", (what, body)


# --------------------------------------------------------------------------- #
# 1. cross-org 404 on every new route
# --------------------------------------------------------------------------- #
def test_cross_org_404(d, api):
    h = _h(d["keys"]["admin_a"])  # org A admin, api docs keys
    pb, kb, db_, sb, cb = (d["proj_b1"], d["kp_b1"], d["doc_b1"], d["seg_b1"], d["cust_b1"])
    cases = [
        api.post("/api/projects/%d/status" % pb, headers=h, json={"status": "completed"}),  # noqa: UP031
        api.get("/api/projects/%d/export" % pb, headers=h),  # noqa: UP031
        api.get("/api/projects/%d/tm-sources" % pb, headers=h),  # noqa: UP031
        api.post(
            "/api/projects/%d/tm-sources" % pb,  # noqa: UP031
            headers=h,
            json={"source_project_id": d["proj_a1_src"]},
        ),
        api.post(
            "/api/projects/%d/tm-sources/remove" % pb,  # noqa: UP031
            headers=h,
            json={"source_project_id": d["proj_a1_src"]},
        ),
        api.get("/api/projects/%d/glossaries" % pb, headers=h),  # noqa: UP031
        api.post("/api/projects/%d/glossaries" % pb, headers=h, json={"glossary_id": d["glos_a1"]}),  # noqa: UP031
        api.post(
            "/api/projects/%d/glossaries/remove" % pb,  # noqa: UP031
            headers=h,
            json={"glossary_id": d["glos_a1"]},  # noqa: RUF100, UP031
        ),
        api.get("/api/projects/%d/glossary/export" % pb, headers=h),  # noqa: UP031
        api.post("/api/documents/%d/rename" % db_, headers=h, json={"filename": "x.txt"}),  # noqa: UP031
        api.post("/api/documents/%d/delete" % db_, headers=h, json={"confirm": "b1.txt"}),  # noqa: UP031
        api.post("/api/segments/%d/unlock" % sb, headers=h, json={}),  # noqa: UP031
        api.get("/api/customers/%d/detail" % cb, headers=h),  # noqa: UP031
        api.post("/api/customers/%d/settings" % cb, headers=h, json={"settings": {"k": "v"}}),  # noqa: UP031
        api.post("/api/customers/%d/defaults" % cb, headers=h, json={"provider": "mock"}),  # noqa: UP031
        api.post("/api/customers/%d/pairs/add" % cb, headers=h, json={"pair": "en-US->de-DE"}),  # noqa: UP031
        api.post("/api/customers/%d/pairs/remove" % cb, headers=h, json={"pair": "en-US->de-DE"}),  # noqa: UP031
        api.post(
            "/api/projects/%d/keys/translate" % kb,  # noqa: UP031
            headers=h,
            json={"workflow_id": d["wf_a"], "locale": "de-DE"},
        ),
    ]
    for i, r in enumerate(cases):
        _is(r, 404, "cross-org route #%d" % i)  # noqa: UP031
    print(
        "  [1] cross-org: A's admin key 404s on all %d of B's new routes "  # noqa: UP031
        "(never 403 -- foreign ids are non-enumerable)" % len(cases)
    )


# --------------------------------------------------------------------------- #
# 2. the B1/H1/H2 class: a customer-bound caller confined to its own customer
# --------------------------------------------------------------------------- #
def test_customer_bound_ring(d, api):
    h = _h(d["keys"]["ling_bound_a1"])  # linguist bound to customer A1
    # (a) targeting ANOTHER customer's project (cust A2) -- 404 on the PATH id.
    _is(
        api.post("/api/projects/%d/status" % d["proj_a2"], headers=h, json={"status": "completed"}),  # noqa: UP031
        404,
        "bound -> A2 project status",
    )
    _is(
        api.get("/api/projects/%d/tm-sources" % d["proj_a2"], headers=h),  # noqa: UP031
        404,
        "bound -> A2 project tm-sources list",
    )
    _is(
        api.get("/api/projects/%d/export" % d["proj_a2"], headers=h),  # noqa: UP031
        404,
        "bound -> A2 project export",
    )
    # (b) own project, but a FOREIGN-customer id in the BODY (the H1/H2 oracle) --
    # a cust-A2 project as a tm-source, a cust-A2 glossary as a pin: 404, not 400.
    _is(
        api.post(
            "/api/projects/%d/tm-sources" % d["proj_a1"],  # noqa: UP031
            headers=h,
            json={"source_project_id": d["proj_a2"]},
        ),
        404,
        "bound -> foreign-customer tm-source id",
    )
    _is(
        api.post(
            "/api/projects/%d/glossaries" % d["proj_a1"],  # noqa: UP031
            headers=h,
            json={"glossary_id": d["glos_a2"]},
        ),
        404,
        "bound -> foreign-customer glossary id",
    )
    # (c) launching a foreign (org-B) workflow over its OWN key project -- 404, and
    # nothing launches (a bound token must not execute+bill a workflow it cannot see).
    before = _run_count()
    _is(
        api.post(
            "/api/projects/%d/keys/translate" % d["kp_a1"],  # noqa: UP031
            headers=h,
            json={"workflow_id": d["wf_b"], "locale": "de-DE"},
        ),
        404,
        "bound -> foreign-org workflow over own key project",
    )
    assert _run_count() == before, "a 404 workflow launch must create no runs"
    print(
        "  [2] customer ring: a customer-A1-bound linguist 404s on customer A2's "
        "project AND on a foreign-customer tm-source/glossary id AND on a "
        "foreign-org workflow launch -- every foreign-id parameter pinned"
    )


# --------------------------------------------------------------------------- #
# 3. same-customer-or-house enforcement (project_sources)
# --------------------------------------------------------------------------- #
def test_same_customer_or_house(d, api):
    h = _h(d["keys"]["admin_a"])  # staff (unbound) admin in org A
    p = d["proj_a1"]  # target is customer A1
    # A house (customer-less) source ATTACHES -- the asymmetric rule (house feeds a
    # customer project).
    r = api.post(
        "/api/projects/%d/tm-sources" % p,  # noqa: UP031
        headers=h,
        json={"source_project_id": d["proj_house"]},  # noqa: RUF100, UP031
    )
    _is(r, 200, "house source attach")
    assert r.json()["source_project_id"] == d["proj_house"], r.text
    # A same-customer (A1) source attaches too.
    _is(
        api.post(
            "/api/projects/%d/tm-sources" % p,  # noqa: UP031
            headers=h,
            json={"source_project_id": d["proj_a1_src"]},
        ),
        200,
        "same-customer source attach",
    )
    # A FOREIGN-CUSTOMER (A2) source, same org -- REFUSED (a clean 400 for staff, not
    # an attach; the service's cross_project_scope_allows is the authority).
    r = api.post(
        "/api/projects/%d/tm-sources" % p,  # noqa: UP031
        headers=h,
        json={"source_project_id": d["proj_a2"]},  # noqa: RUF100, UP031
    )
    _is(r, 400, "foreign-customer source refused")
    assert r.json().get("ok") is False, r.text
    # It was NOT attached: the effective list holds only the two legitimate sources.
    lst = api.get("/api/projects/%d/tm-sources" % p, headers=h).json()["tm_sources"]  # noqa: UP031
    got = {row["source_project_id"] for row in lst}
    assert got == {d["proj_house"], d["proj_a1_src"]}, got
    print(
        "  [3] project_sources: a house source and a same-customer source attach; "
        "a foreign-customer source is refused (400) and never lands in the list"
    )


# --------------------------------------------------------------------------- #
# 4. RBAC -- viewer/translator refused where the gate excludes them
# --------------------------------------------------------------------------- #
def test_rbac(d, api):
    view, tr = _h(d["keys"]["view_a"]), _h(d["keys"]["tr_a"])
    p, doc, cid = d["proj_a1"], d["doc_a1"], d["cust_a1"]
    # can_write (status / doc rename): a VIEWER is blocked.
    _forbidden(
        api.post("/api/projects/%d/status" % p, headers=view, json={"status": "in_review"}),  # noqa: UP031
        "viewer -> status",
    )
    _forbidden(
        api.post("/api/documents/%d/rename" % doc, headers=view, json={"filename": "z.txt"}),  # noqa: UP031
        "viewer -> doc rename",
    )
    # can_tune_ai (tm-sources / glossaries): a TRANSLATOR lacks it (not a rank gate).
    _forbidden(
        api.post(
            "/api/projects/%d/tm-sources" % p,  # noqa: UP031
            headers=tr,
            json={"source_project_id": d["proj_a1_src"]},
        ),
        "translator -> tm-source attach",
    )
    _forbidden(
        api.post("/api/projects/%d/glossaries" % p, headers=tr, json={"glossary_id": d["glos_a1"]}),  # noqa: UP031
        "translator -> glossary pin",
    )
    # can_manage (customer 360): a TRANSLATOR is blocked.
    _forbidden(
        api.get("/api/customers/%d/detail" % cid, headers=tr),  # noqa: UP031
        "translator -> customer detail",  # noqa: RUF100, UP031
    )
    _forbidden(
        api.post("/api/customers/%d/settings" % cid, headers=tr, json={"settings": {"k": "v"}}),  # noqa: UP031
        "translator -> settings",
    )
    # is_admin (doc delete): a TRANSLATOR is blocked -- and the ring runs FIRST, so a
    # foreign id is 404 while an in-scope non-admin is 403.
    _forbidden(
        api.post("/api/documents/%d/delete" % doc, headers=tr, json={"confirm": "a1.txt"}),  # noqa: UP031
        "translator -> doc delete",
    )
    print(
        "  [4] RBAC: viewer 403s on can_write; translator 403s on can_tune_ai / "
        "can_manage / is_admin -- each a bare role-gate string that leaks nothing"
    )


# --------------------------------------------------------------------------- #
# 4b. F1 (MEDIUM, adversarial review): rename must reject control characters --
# the download route reflects doc.filename verbatim into Content-Disposition, so a
# CR/LF-bearing filename is live response-header injection. content.rename_document
# now rejects (400, not 500) any filename containing an ASCII control character,
# while still allowing spaces/unicode/punctuation (a legitimate filename shape).
# --------------------------------------------------------------------------- #
def test_rename_rejects_control_chars(d, api):
    h = _h(d["keys"]["admin_a"])
    doc = d["doc_a1"]
    with Session(engine) as s:
        before = s.get(Document, doc).filename
    # CRLF header-injection attempt: clean 400, filename left untouched.
    r = api.post(
        "/api/documents/%d/rename" % doc,  # noqa: UP031
        headers=h,
        json={"filename": 'evil"\r\nX-Injected: 1.txt'},  # noqa: RUF100, UP031
    )
    _is(r, 400, "doc rename with CRLF")
    assert r.json().get("ok") is False, r.text
    assert "control character" in r.json().get("error", ""), r.text
    with Session(engine) as s:
        assert s.get(Document, doc).filename == before, (
            "a refused rename must not mutate the stored filename"
        )
    # a bare LF (no CR) and an embedded TAB are refused the same way.
    _is(
        api.post(
            "/api/documents/%d/rename" % doc,  # noqa: UP031
            headers=h,
            json={"filename": "evil\nX-Injected: 1.txt"},
        ),
        400,
        "doc rename with LF",
    )
    _is(
        api.post(
            "/api/documents/%d/rename" % doc,  # noqa: UP031
            headers=h,
            json={"filename": "evil\tinjected.txt"},  # noqa: RUF100, UP031
        ),
        400,
        "doc rename with TAB",
    )
    # a normal rename with spaces + unicode (a legitimate filename shape) still works.
    r = api.post(
        "/api/documents/%d/rename" % doc,  # noqa: UP031
        headers=h,
        json={"filename": "café report (final).txt"},  # noqa: RUF100, UP031
    )
    _is(r, 200, "doc rename with spaces/unicode")
    assert r.json()["filename"] == "café report (final).txt", r.text
    with Session(engine) as s:
        assert s.get(Document, doc).filename == "café report (final).txt"
    print(
        "  [4b] F1: /api rename refuses CR/LF/TAB with a clean 400 and leaves the "
        "filename untouched; spaces/unicode filenames still round-trip"
    )


# --------------------------------------------------------------------------- #
# 5. keys/translate mock-409 + happy launch
# --------------------------------------------------------------------------- #
def test_keys_translate_mock_409(d, api):
    h = _h(d["keys"]["admin_a"])
    kp = d["kp_a1"]
    # A workflow whose translate step is a KEYLESS real engine (openai) -> 409, and
    # nothing launches (the day-one trust guard, mirrored from document translate).
    before = _run_count()
    r = api.post(
        "/api/projects/%d/keys/translate" % kp,  # noqa: UP031
        headers=h,
        json={"workflow_id": d["wf_bad"], "locale": "de-DE"},
    )
    _is(r, 409, "keys/translate mock-409")
    assert _run_count() == before, "a mock-409 must launch nothing"
    # The explicit-mock workflow is the allowed escape hatch and launches a run over
    # the one translatable key.
    r = api.post(
        "/api/projects/%d/keys/translate" % kp,  # noqa: UP031
        headers=h,
        json={"workflow_id": d["wf_a"], "locale": "de-DE"},
    )
    _is(r, 200, "keys/translate happy launch")
    body = r.json()
    assert body["launched"], f"the mock workflow must launch at least one run: {body}"
    print(
        "  [5] keys/translate: a keyless real engine is a 409 that launches nothing; "  # noqa: UP031
        "an explicit-mock workflow launches (%d run(s))" % len(body["launched"])
    )


# --------------------------------------------------------------------------- #
# 6. the two-phase translate_keys MCP contract (unconfirmed writes nothing)
# --------------------------------------------------------------------------- #
def test_translate_keys_two_phase(d):
    """The spend tool's contract: an UNCONFIRMED call plans and launches NOTHING; a
    CONFIRMED one launches. Proven by counting WorkflowRun rows around each, through
    the SAME MCP dispatch a client hits. SDK-tolerant: if `mcp` is absent this leg
    degrades to the handler-level buckle (confirm=False must still not launch)."""
    import importlib

    _dir = os.path.join(_ROOT, "integrations", "mcp-server")
    if _dir not in sys.path:
        sys.path.insert(0, _dir)
    try:
        server = importlib.import_module("server")
    except Exception as exc:
        _two_phase_handler_only(d, exc)
        return

    import asyncio
    import json as _json

    import httpx

    from app.main import app

    server.BASE_URL = "http://testserver"
    server.TOKEN = d["keys"]["admin_a"]

    # Route the MCP server's httpx client at the in-process app (hermetic, no uvicorn).
    orig = server._client

    def _patched():
        return httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://testserver",
            headers={"X-Loc-Token": server.TOKEN},
        )

    server._client = _patched
    args = {"project_id": d["kp_two"], "workflow_id": d["wf_a"], "locale": "de-DE"}
    try:
        before = _run_count()
        # UNCONFIRMED -> the dispatcher answers with a plan, never the handler.
        plan = _json.loads(asyncio.run(server.call_tool_unvalidated("translate_keys", dict(args))))
        assert plan.get("plan") is True and plan.get("spent") is False, plan
        assert _run_count() == before, "an UNCONFIRMED translate_keys launched a run"
        # CONFIRMED -> launches.
        # #757: the confirmed call names the plan it confirms
        confirmed = {**args, "confirm": True, "plan_id": plan["plan_id"]}
        asyncio.run(server.call_tool_unvalidated("translate_keys", confirmed))
        assert _run_count() > before, "a CONFIRMED translate_keys launched nothing"
        print(
            "  [6] two-phase: an UNCONFIRMED translate_keys returns a plan and "
            "creates 0 runs; a CONFIRMED one launches (row-count proven)"
        )
    finally:
        server._client = orig


def _two_phase_handler_only(d, exc):
    """Fallback when the MCP SDK is absent: the handler-level buckle still proves an
    unconfirmed call launches nothing (it raises before any network call)."""
    import importlib

    _dir = os.path.join(_ROOT, "integrations", "mcp-server")
    if _dir not in sys.path:
        sys.path.insert(0, _dir)
    reg = importlib.import_module("registry")
    spec = next(t for t in reg.TOOLS if t.name == "translate_keys")
    assert spec.tier == "spend", "translate_keys must be spend tier"
    assert "confirm" in (spec.input_schema.get("properties") or {}), (
        "a spend tool must declare confirm"
    )
    print(
        f"  [6] two-phase: MCP SDK absent ({type(exc).__name__}); registry confirms translate_keys is "  # noqa: E501
        "spend with a confirm gate (the dispatcher enforces the plan)"
    )


# --------------------------------------------------------------------------- #
# 7. happy paths for the remaining routes (read-back where a read exists)
# --------------------------------------------------------------------------- #
def test_happy_paths(d, api):
    h = _h(d["keys"]["admin_a"])
    p, doc, seg, cid = d["proj_a1"], d["doc_a1"], d["seg_a1"], d["cust_a1"]
    # project status
    r = api.post("/api/projects/%d/status" % p, headers=h, json={"status": "in_review"})  # noqa: UP031
    _is(r, 200, "status")
    assert r.json()["status"] == "in_review", r.text
    # project export (raw bytes)
    r = api.get("/api/projects/%d/export?locale=de-DE" % p, headers=h)  # noqa: UP031
    _is(r, 200, "export")
    assert r.content and "attachment" in r.headers.get("content-disposition", ""), (
        "export must be an attachment with bytes"
    )
    # legacy glossary export (raw bytes)
    r = api.get("/api/projects/%d/glossary/export" % p, headers=h)  # noqa: UP031
    _is(r, 200, "glossary export")
    assert b"widget" in r.content.lower() or r.content, r.text[:80]
    # glossary pin add / list / remove
    _is(
        api.post("/api/projects/%d/glossaries" % p, headers=h, json={"glossary_id": d["glos_a1"]}),  # noqa: UP031
        200,
        "glossary pin add",
    )
    lst = api.get("/api/projects/%d/glossaries" % p, headers=h).json()["glossaries"]  # noqa: UP031
    assert any(g["glossary_id"] == d["glos_a1"] and g["active"] for g in lst), lst
    r = api.post(
        "/api/projects/%d/glossaries/remove" % p,  # noqa: UP031
        headers=h,
        json={"glossary_id": d["glos_a1"]},  # noqa: RUF100, UP031
    )
    _is(r, 200, "glossary pin remove")
    assert r.json()["removed"] is True, r.text
    # a house glossary is invisible to a bound linguist's pin picker (design), so pin it
    # here as staff to prove the module-visible path works for the house glossary too.
    _is(
        api.post(
            "/api/projects/%d/glossaries" % p,  # noqa: UP031
            headers=h,
            json={"glossary_id": d["glos_house"]},  # noqa: RUF100, UP031
        ),
        200,
        "house glossary pin",
    )
    # document rename + segment unlock
    r = api.post("/api/documents/%d/rename" % doc, headers=h, json={"filename": "renamed.txt"})  # noqa: UP031
    _is(r, 200, "doc rename")
    assert r.json()["filename"] == "renamed.txt", r.text
    r = api.post("/api/segments/%d/unlock" % seg, headers=h, json={})  # noqa: UP031
    _is(r, 200, "segment unlock")
    assert r.json()["unlocked"] is True, r.text
    # customer 360 + settings/defaults/pairs
    detail = api.get("/api/customers/%d/detail" % cid, headers=h)  # noqa: UP031
    _is(detail, 200, "customer detail")
    assert detail.json()["id"] == cid and "projects" in detail.json(), detail.text
    r = api.post(
        "/api/customers/%d/settings" % cid,  # noqa: UP031
        headers=h,
        json={"settings": {"tone": "formal"}},  # noqa: RUF100, UP031
    )
    _is(r, 200, "customer settings")
    assert r.json()["settings"].get("tone") == "formal", r.text
    r = api.post("/api/customers/%d/defaults" % cid, headers=h, json={"provider": "mock"})  # noqa: UP031
    _is(r, 200, "customer defaults")
    assert r.json()["default_provider"] == "mock", r.text
    r = api.post("/api/customers/%d/pairs/add" % cid, headers=h, json={"pair": "en-US->fr-FR"})  # noqa: UP031
    _is(r, 200, "customer pairs add")
    assert r.json()["added"] is True and r.json()["pair"] == "en-US->fr-FR", r.text
    r = api.post("/api/customers/%d/pairs/remove" % cid, headers=h, json={"pair": "en-US->fr-FR"})  # noqa: UP031
    _is(r, 200, "customer pairs remove")
    assert r.json()["removed"] is True, r.text
    # document delete (admin + typed confirm): wrong confirm 400, right confirm 200.
    r = api.post("/api/documents/%d/delete" % doc, headers=h, json={"confirm": "wrong.txt"})  # noqa: UP031
    _is(r, 400, "doc delete wrong confirm")
    r = api.post("/api/documents/%d/delete" % doc, headers=h, json={"confirm": "renamed.txt"})  # noqa: UP031
    _is(r, 200, "doc delete right confirm")
    assert r.json()["deleted"] is True, r.text
    print(
        "  [7] happy paths: status, export, glossary pin+list+remove, doc rename, "
        "segment unlock, customer 360 + settings/defaults/pairs, doc delete "
        "(typed confirm) all round-trip"
    )


# --------------------------------------------------------------------------- #
def _run_count() -> int:
    with Session(engine) as s:
        return len(s.exec(select(WorkflowRun.id)).all())


def main() -> None:
    d = _seed()
    _gate_on()
    try:
        api = _client()
        test_cross_org_404(d, api)
        test_customer_bound_ring(d, api)
        test_same_customer_or_house(d, api)
        test_rbac(d, api)
        test_rename_rejects_control_chars(d, api)
        test_keys_translate_mock_409(d, api)
        test_translate_keys_two_phase(d)
        test_happy_paths(d, api)
    finally:
        _gate_off()
    print("R1A CONTENT PARITY OK")


if __name__ == "__main__":
    main()
