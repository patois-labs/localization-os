"""SPEC-full-remote-parity Wave R1b: the remaining content-ops /api routes.

R1b gives /api parity to the content surfaces R1a left web/CLI-only: workflow GROUP
create/list/show/add/set-default, prompt-template create/list, workflow clone/copy/
author, project templates (list/show/create/apply/rename/delete), image upload +
translate, style-guide + context A/B, segmentation profiles, and context packages.

Asserted with BOTH the login wall AND multi-org ON (a genuine TWO-ORG world), so the
behaviour under test is org scoping AND the customer ring AND the role gate at once:

  * CROSS-ORG 404 on every new route -- org A's admin key never reaches org B's group /
    workflow / template / image / style-guide / segprofile / context scope; a foreign id
    is 404, never 403 (a distinguishable refusal would leak existence);
  * the B1/H1/H2 CLASS -- a customer-BOUND caller (a linguist bound to customer A1) is
    refused (404) when it targets ANOTHER customer's group / template / workflow-clone
    target / style-guide (AB) / context scope; EVERY foreign-id parameter is pinned;
  * RBAC exactly as the table says -- can_tune_ai (workflow group/prompt/clone/copy/AB/
    context), can_write (templates create/apply/rename, image upload/translate), is_admin
    (template delete, the whole segprofile family) -- the excluded role gets a 403 whose
    body names nothing (the shared string);
  * IMAGE kill switch -- with image_localization_enabled off, upload + translate 404;
  * SCOPE-BEFORE-VALIDATE -- a foreign path id + a bad body is a 404 (the ring resolves
    before any input validation), never a 400 that leaks existence;
  * the SPEND mock-409 -- style/context A/B and workflow author refuse a keyless REAL
    engine (409) before spending; an explicit mock is the allowed escape hatch;
  * the two-phase translate_image MCP contract -- an UNCONFIRMED call plans and writes
    NOTHING (asserted by counting translated regions), a CONFIRMED one translates;
  * HAPPY PATHS for every route, read back where a read exists.

ASCII-only; standalone one-process-per-suite script (throwaway LOC_TMS_DATA_DIR, keys
cleared -> MockProvider, deterministic + free). Final line: "R1B CONTENT PARITY OK".
"""

import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(_ROOT)
sys.path.insert(0, _ROOT)
from tests._bootstrap import init_env

init_env("loctms_r1b_")

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
    WorkflowStep,
)
from app.services import apikeys, content, security, settings
from app.services import content_types as ct_svc
from app.services import image_localization as il
from app.services import segmentation_profiles as segprof
from app.services import style_guides as sg_svc
from app.services import templates as tpl_svc
from app.services import workflows as wf_svc

_SVG = (
    b'<?xml version="1.0"?><svg xmlns="http://www.w3.org/2000/svg" '
    b'width="200" height="80"><text x="10" y="40">Hello world</text></svg>'
)


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def _gate_on() -> None:
    settings.set_many(
        {"security_auth_required": "1", "multi_org_enabled": "1", "image_localization_enabled": "1"}
    )
    settings.invalidate_cache()


def _gate_off() -> None:
    settings.set_many({"security_auth_required": "0", "multi_org_enabled": "0"})
    settings.invalidate_cache()


def _image_flag(on: bool) -> None:
    settings.set_many({"image_localization_enabled": "1" if on else "0"})
    settings.invalidate_cache()


def _h(raw: str) -> dict:
    return {"X-Loc-Token": raw}


def _key(user_id: int, scopes="api docs keys") -> str:
    _row, raw = apikeys.create_key(user_id, "r1b-test", scopes=scopes)
    return raw


def _user(email, role, org_id, customer_id=None) -> int:
    u = security.create_user(email, email, role, "r1b-password-123", customer_id=customer_id)
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


def _doc(session, project_id, org_id, filename, source, target="de-DE"):
    """A txt document with one translatable segment (non-empty source)."""
    d = Document(
        project_id=project_id,
        filename=filename,
        fmt="txt",
        mode="segmented",
        status="parsed",
        org_id=org_id,
    )
    session.add(d)
    session.commit()
    session.refresh(d)
    seg = Segment(
        document_id=d.id, source_text=source, target_text="", target_locale=target, order_index=0
    )
    session.add(seg)
    session.commit()
    return d.id


def _sg(session, name, org_id, customer_id):
    g = sg_svc.create(
        session,
        name,
        org_id=org_id,
        customer_id=customer_id,
        content_type_id=None,
        source_locale="en-US",
        target_locale="de-DE",
        body="Be concise and formal.",
        created_by="seed",
    )
    sg_svc.approve(session, g.id, approver_email="seed")
    return g.id


def _seed():
    """The two-org world. Org A carries customers A1 + A2 (foreign-customer foils) and
    org B is the cross-org foil. Every object is org-stamped explicitly."""
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
    settings.set_many({"multi_org_enabled": "1", "image_localization_enabled": "1"})
    settings.invalidate_cache()

    admin_a = _user("admina@x.com", Role.ADMIN, oa)
    ling_a = _user("linga@x.com", Role.LINGUIST, oa)
    tr_a = _user("tra@x.com", Role.TRANSLATOR, oa)
    view_a = _user("viewa@x.com", Role.VIEWER, oa)
    admin_b = _user("adminb@x.com", Role.ADMIN, ob)
    ling_bound_a1 = _user("lba1@x.com", Role.LINGUIST, oa)  # bound below
    # A VIEWER bound to customer A1: sees A1's projects (customer binding beats
    # project membership) but fails can_write -- so an image write route resolves its
    # ring (200-visible) and THEN 403s on the role, proving the role gate rather than
    # a 404 the viewer would hit on a project it cannot even see.
    view_bound_a1 = _user("vba1@x.com", Role.VIEWER, oa)

    with Session(engine) as s:
        adm_a = s.get(User, admin_a)
        adm_b = s.get(User, admin_b)
        cust_a1 = content.customer_create(s, "Acme A1", creator=adm_a).id
        cust_a2 = content.customer_create(s, "Acme A2", creator=adm_a).id
        cust_b1 = content.customer_create(s, "Beta B1", creator=adm_b).id
        # Stamp customer orgs BEFORE any dependent create: create_prompt_template /
        # style_guides.create validate the customer via customers.selectable (same-org),
        # which needs the org stamp live at create time.
        from app.models import Customer as _Cust

        for cid_ in (cust_a1, cust_a2):
            s.get(_Cust, cid_).org_id = oa
        s.get(_Cust, cust_b1).org_id = ob
        s.commit()

        # --- org A / customer A1 objects -----------------------------------
        proj_a1 = content.project_create(
            s,
            "A1 Doc",
            source_locale="en-US",
            target_locales="de-DE",
            customer_id=cust_a1,
            creator=adm_a,
            default_provider="mock",
        ).id
        doc_a1 = _doc(s, proj_a1, oa, "a1.txt", "Hello world")
        img_a1 = il.ingest_image(
            s, s.get(Project, proj_a1), "a1.svg", _SVG, actor="admina@x.com"
        ).id
        wf_a = _wf(s, "WF A", oa, "mock", customer_id=cust_a1)
        wf_ungrouped = _wf(s, "WF A ungrouped", oa, "mock", customer_id=cust_a1)
        grp_a1 = wf_svc.create_group(
            s, "GRP A1", customer_id=cust_a1, org_id=oa, created_by="seed"
        ).id
        wf_svc.add_to_group(
            s, wf_a, grp_a1, source_locale="en-US", target_locale="de-DE", make_default=True
        )
        pt_a1 = wf_svc.create_prompt_template(
            s, "PT A1", body="Translate {text}.", org_id=oa, customer_id=cust_a1
        ).id
        sg_a1 = _sg(s, "SG A1", oa, cust_a1)
        ct_a1 = ct_svc.create(s, "CT A1", org_id=oa, customer_id=cust_a1, created_by="seed").id
        tpl_a1 = tpl_svc.save_as_template(
            s, proj_a1, "TPL A1", scope="customer", actor="seed", org_id=oa
        ).id
        # A GLOBAL template (customer_id None, org A) -- the apply-override attack
        # surface: a caller supplies a foreign customer_id the global template does not
        # otherwise pin, so the route ring is the only thing between it and a planted
        # project.
        tpl_global_a = tpl_svc.save_as_template(
            s, proj_a1, "TPL Global A", scope="global", actor="seed", org_id=oa
        ).id

        # --- org A / customer A2 (foreign-customer foils) ------------------
        proj_a2 = content.project_create(
            s,
            "A2 Doc",
            source_locale="en-US",
            target_locales="de-DE",
            customer_id=cust_a2,
            creator=adm_a,
            default_provider="mock",
        ).id
        wf_a2 = _wf(s, "WF A2", oa, "mock", customer_id=cust_a2)
        grp_a2 = wf_svc.create_group(
            s, "GRP A2", customer_id=cust_a2, org_id=oa, created_by="seed"
        ).id
        sg_a2 = _sg(s, "SG A2", oa, cust_a2)
        ct_a2 = ct_svc.create(s, "CT A2", org_id=oa, customer_id=cust_a2, created_by="seed").id
        tpl_a2 = tpl_svc.save_as_template(
            s, proj_a2, "TPL A2", scope="customer", actor="seed", org_id=oa
        ).id

        # --- org B foils ---------------------------------------------------
        proj_b1 = content.project_create(
            s,
            "B1 Doc",
            source_locale="en-US",
            target_locales="de-DE",
            customer_id=cust_b1,
            creator=adm_b,
            default_provider="mock",
        ).id
        img_b1 = il.ingest_image(
            s, s.get(Project, proj_b1), "b1.svg", _SVG, actor="adminb@x.com"
        ).id
        wf_b = _wf(s, "WF B", ob, "mock", customer_id=cust_b1)
        grp_b = wf_svc.create_group(
            s, "GRP B", customer_id=cust_b1, org_id=ob, created_by="seed"
        ).id
        sg_b = _sg(s, "SG B", ob, cust_b1)
        tpl_b = tpl_svc.save_as_template(
            s, proj_b1, "TPL B", scope="customer", actor="seed", org_id=ob
        ).id

        # Segprofiles are org-scoped, is_admin. sp_a1 (org A), sp_b (org B foil).
        sp_a1 = segprof.create_profile("SP A1", customer_id=None, actor="seed", org_id=oa).id
        sp_b = segprof.create_profile("SP B", customer_id=None, actor="seed", org_id=ob).id

        # Bind the linguist + a viewer to customer A1 (the B1/H1/H2 probe + the
        # can-see-but-cannot-write RBAC probe).
        s.get(User, ling_bound_a1).customer_id = cust_a1
        s.get(User, view_bound_a1).customer_id = cust_a1

        # Org-stamp projects unambiguously (seed-time cache brittleness guard).
        for pid_ in (proj_a1, proj_a2):
            s.get(Project, pid_).org_id = oa
        s.get(Project, proj_b1).org_id = ob
        s.commit()

    keys = {
        "admin_a": _key(admin_a),
        "ling_a": _key(ling_a),
        "tr_a": _key(tr_a),
        "view_a": _key(view_a),
        "admin_b": _key(admin_b),
        "ling_bound_a1": _key(ling_bound_a1),
        "view_bound_a1": _key(view_bound_a1),
    }
    return {
        "keys": keys,
        "cust_a1": cust_a1,
        "cust_a2": cust_a2,
        "cust_b1": cust_b1,
        "proj_a1": proj_a1,
        "proj_a2": proj_a2,
        "proj_b1": proj_b1,
        "doc_a1": doc_a1,
        "img_a1": img_a1,
        "img_b1": img_b1,
        "wf_a": wf_a,
        "wf_ungrouped": wf_ungrouped,
        "wf_a2": wf_a2,
        "wf_b": wf_b,
        "grp_a1": grp_a1,
        "grp_a2": grp_a2,
        "grp_b": grp_b,
        "pt_a1": pt_a1,
        "sg_a1": sg_a1,
        "sg_a2": sg_a2,
        "sg_b": sg_b,
        "ct_a1": ct_a1,
        "ct_a2": ct_a2,
        "tpl_a1": tpl_a1,
        "tpl_a2": tpl_a2,
        "tpl_b": tpl_b,
        "tpl_global_a": tpl_global_a,
        "sp_a1": sp_a1,
        "sp_b": sp_b,
    }


def _is(r, code, what):
    assert r.status_code == code, f"{what}: expected {code}, got {r.status_code} ({r.text[:200]})"


def _forbidden(r, what):
    _is(r, 403, what)
    body = r.json()
    assert body.get("ok") is False, (what, body)
    assert body.get("error") == "your role does not allow this action.", (what, body)


def _project_count() -> int:
    with Session(engine) as s:
        return len(s.exec(select(Project.id)).all())


def _img_translated(doc_id, locale="de-DE") -> int:
    """Regions of an image doc with a non-empty target -- the 'did the translate write'
    counter for the two-phase proof."""
    with Session(engine) as s:
        return len(
            [
                sg
                for sg in s.exec(select(Segment).where(Segment.document_id == doc_id)).all()
                if sg.target_locale == locale and (sg.target_text or "").strip()
            ]
        )


# --------------------------------------------------------------------------- #
# 1. cross-org 404 on every new route (foreign PATH ids)
# --------------------------------------------------------------------------- #
def test_cross_org_404(d, api):
    h = _h(d["keys"]["admin_a"])  # org A admin, api docs keys
    gb, wb, tb, ib, pb, sgb, spb = (
        d["grp_b"],
        d["wf_b"],
        d["tpl_b"],
        d["img_b1"],
        d["proj_b1"],
        d["sg_b"],
        d["sp_b"],
    )
    cases = [
        api.get("/api/workflow-groups/%d" % gb, headers=h),  # noqa: UP031
        api.post("/api/workflow-groups/%d/add" % gb, headers=h, json={"workflow_id": d["wf_a"]}),  # noqa: UP031
        api.post("/api/workflow-groups/%d/default" % gb, headers=h, json={"workflow_id": wb}),  # noqa: UP031
        api.post("/api/workflows/%d/clone" % wb, headers=h),  # noqa: UP031
        api.post("/api/workflows/%d/copy" % wb, headers=h, json={"to_pairs": "en-US:fr-FR"}),  # noqa: UP031
        api.get("/api/templates/%d" % tb, headers=h),  # noqa: UP031
        api.post("/api/templates/%d/apply" % tb, headers=h, json={"name": "X"}),  # noqa: UP031
        api.post("/api/templates/%d/rename" % tb, headers=h, json={"name": "X"}),  # noqa: UP031
        api.post("/api/templates/%d/delete" % tb, headers=h),  # noqa: UP031
        api.post(
            "/api/images",
            headers=h,
            files={"file": ("x.svg", _SVG, "image/svg+xml")},
            data={"project_id": str(pb)},
        ),
        api.post("/api/images/%d/translate" % ib, headers=h, json={"locale": "de-DE"}),  # noqa: UP031
        api.post("/api/style-guides/%d/ab" % sgb, headers=h, json={"document": d["doc_a1"]}),  # noqa: UP031
        api.post("/api/context/ab", headers=h, json={"document": d["img_b1"]}),
        api.post("/api/context/documents/%d" % ib, headers=h, json={"context": {}}),  # noqa: UP031
        api.post("/api/context/profiles", headers=h, json={"project_id": pb}),
        api.get("/api/context/resolve?project=%d" % pb, headers=h),  # noqa: UP031
        api.get("/api/segprofiles/%d" % spb, headers=h),  # noqa: UP031
        api.post("/api/segprofiles/%d/clone" % spb, headers=h, json={"new_name": "X"}),  # noqa: UP031
        api.post("/api/segprofiles/%d/set-default" % spb, headers=h, json={}),  # noqa: UP031
        api.post("/api/segprofiles/%d/delete" % spb, headers=h),  # noqa: UP031
        api.post("/api/segprofiles/%d/rename" % spb, headers=h, json={"name": "X"}),  # noqa: UP031
        api.post(
            "/api/segprofiles/assign", headers=h, json={"project_id": pb, "profile_id": d["sp_a1"]}
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
    # (a) another customer's (A2) object by PATH id -> 404.
    _is(api.get("/api/workflow-groups/%d" % d["grp_a2"], headers=h), 404, "bound -> A2 group show")  # noqa: UP031
    _is(
        api.post("/api/workflows/%d/clone" % d["wf_a2"], headers=h),  # noqa: UP031
        404,
        "bound -> A2 workflow clone",
    )
    _is(api.get("/api/templates/%d" % d["tpl_a2"], headers=h), 404, "bound -> A2 template show")  # noqa: UP031
    _is(
        api.post("/api/templates/%d/rename" % d["tpl_a2"], headers=h, json={"name": "X"}),  # noqa: UP031
        404,
        "bound -> A2 template rename",
    )
    _is(
        api.post("/api/style-guides/%d/ab" % d["sg_a2"], headers=h, json={"document": d["doc_a1"]}),  # noqa: UP031
        404,
        "bound -> A2 style-guide AB",
    )
    # (b) own group, but a FOREIGN-customer workflow id in the BODY (the H1/H2 oracle).
    _is(
        api.post(
            "/api/workflow-groups/%d/add" % d["grp_a1"],  # noqa: UP031
            headers=h,
            json={"workflow_id": d["wf_a2"]},  # noqa: RUF100, UP031
        ),
        404,
        "bound -> foreign-customer workflow id in group add",
    )
    # (c) context scope ids in the body -> a foreign customer / project / content-type
    # scope is a 404, never a planted card.
    _is(
        api.post(
            "/api/context/profiles", headers=h, json={"customer_id": d["cust_a2"], "context": {}}
        ),
        404,
        "bound -> foreign-customer context profile",
    )
    _is(
        api.post(
            "/api/context/profiles", headers=h, json={"project_id": d["proj_a2"], "context": {}}
        ),
        404,
        "bound -> foreign-customer project scope in context profile",
    )
    _is(
        api.post(
            "/api/context/profiles", headers=h, json={"content_type_id": d["ct_a2"], "context": {}}
        ),
        404,
        "bound -> foreign-customer content-type scope in context profile",
    )
    print(
        "  [2] customer ring: a customer-A1-bound linguist 404s on A2's group / "
        "workflow-clone / template / style-guide-AB AND on a foreign-customer "
        "workflow/project/content-type id in a body -- every foreign-id param pinned"
    )


# --------------------------------------------------------------------------- #
# 2b. template-apply customer_id override -- the write-target ring (blocker fix)
# --------------------------------------------------------------------------- #
def test_template_apply_customer_ring(d, api):
    """Applying a GLOBAL template takes a body customer_id as the NEW PROJECT's tenant.
    It must funnel through the customer ring: a bound caller's foreign-customer override
    and a staff caller's cross-org override are BOTH 404, and NEITHER creates a project
    (the plant-and-forget / cross-org-FK blocker the adversarial review reproduced)."""
    tpl = d["tpl_global_a"]
    # (a) A1-bound linguist applies the GLOBAL template into customer A2 -> 404, no row.
    before = _project_count()
    _is(
        api.post(
            "/api/templates/%d/apply" % tpl,  # noqa: UP031
            headers=_h(d["keys"]["ling_bound_a1"]),
            json={"name": "Planted A2", "customer_id": d["cust_a2"]},
        ),
        404,
        "bound -> global template applied into a FOREIGN customer",
    )
    assert _project_count() == before, "a 404 template apply planted a project (bound)"
    # (b) Staff admin A applies the GLOBAL template into org B's customer -> 404, no row.
    _is(
        api.post(
            "/api/templates/%d/apply" % tpl,  # noqa: UP031
            headers=_h(d["keys"]["admin_a"]),
            json={"name": "Planted B1", "customer_id": d["cust_b1"]},
        ),
        404,
        "staff -> global template applied into a CROSS-ORG customer",
    )
    assert _project_count() == before, "a 404 template apply planted a project (staff)"
    print(
        "  [2b] template apply: a body customer_id is ringed as the new project's "
        "tenant -- a bound caller's foreign customer AND a staff caller's cross-org "
        "customer are 404 and create no project (row-count proven)"
    )


# --------------------------------------------------------------------------- #
# 3. RBAC exactly per the spec table
# --------------------------------------------------------------------------- #
def test_rbac(d, api):
    view = _h(d["keys"]["view_a"])  # viewer: fails can_write + can_tune_ai
    tr = _h(d["keys"]["tr_a"])  # translator: can_write, NOT can_tune_ai/admin
    ling = _h(d["keys"]["ling_a"])  # linguist: can_tune_ai, NOT admin

    # can_tune_ai routes: a TRANSLATOR (can_write but not tune_ai) is refused.
    _forbidden(
        api.post("/api/workflow-groups", headers=tr, json={"name": "G"}),
        "translator -> group create",
    )
    _forbidden(
        api.post(
            "/api/workflow-groups/%d/add" % d["grp_a1"],  # noqa: UP031
            headers=tr,
            json={"workflow_id": d["wf_ungrouped"]},
        ),
        "translator -> group add",
    )
    _forbidden(
        api.post("/api/prompt-templates", headers=tr, json={"name": "P"}),
        "translator -> prompt create",
    )
    _forbidden(
        api.post("/api/workflows/%d/clone" % d["wf_a"], headers=tr),  # noqa: UP031
        "translator -> workflow clone",  # noqa: RUF100, UP031
    )
    _forbidden(
        api.post(
            "/api/workflows/%d/copy" % d["wf_a"],  # noqa: UP031
            headers=tr,
            json={"to_pairs": "en-US:fr-FR"},  # noqa: RUF100, UP031
        ),
        "translator -> workflow copy",
    )
    _forbidden(
        api.post(
            "/api/style-guides/%d/ab" % d["sg_a1"],  # noqa: UP031
            headers=tr,
            json={"document": d["doc_a1"], "provider": "mock"},
        ),
        "translator -> style AB",
    )
    _forbidden(
        api.post("/api/context/ab", headers=tr, json={"document": d["doc_a1"], "provider": "mock"}),
        "translator -> context AB",
    )
    _forbidden(
        api.post("/api/context/profiles", headers=tr, json={"context": {}}),
        "translator -> context set-profile",
    )
    _forbidden(
        api.post(
            "/api/workflows/author", headers=tr, json={"description": "x", "provider": "mock"}
        ),
        "translator -> workflow author",
    )

    # can_write routes: a VIEWER is refused (translator is allowed -> not asserted here).
    # The template create/apply routes gate the role BEFORE the ring, so an unbound
    # viewer 403s cleanly; the image write routes resolve the ring FIRST (a foreign
    # project is a 404), so a CUSTOMER-BOUND viewer -- which can see A1's project -- is
    # what proves the role gate rather than a project-visibility 404.
    vbound = _h(d["keys"]["view_bound_a1"])
    _forbidden(
        api.post("/api/templates", headers=view, json={"name": "T", "project_id": d["proj_a1"]}),
        "viewer -> template create",
    )
    _forbidden(
        api.post("/api/templates/%d/apply" % d["tpl_a1"], headers=view, json={"name": "P"}),  # noqa: UP031
        "viewer -> template apply",
    )
    _forbidden(
        api.post(
            "/api/images",
            headers=vbound,
            files={"file": ("x.svg", _SVG, "image/svg+xml")},
            data={"project_id": str(d["proj_a1"])},
        ),
        "bound viewer -> image upload",
    )
    _forbidden(
        api.post(
            "/api/images/%d/translate" % d["img_a1"],  # noqa: UP031
            headers=vbound,
            json={"locale": "de-DE"},  # noqa: RUF100, UP031
        ),
        "bound viewer -> image translate",
    )

    # is_admin routes: a LINGUIST (can_tune_ai but not admin) is refused.
    _forbidden(
        api.post("/api/templates/%d/delete" % d["tpl_a1"], headers=ling),  # noqa: UP031
        "linguist -> template delete",
    )
    _forbidden(
        api.post("/api/segprofiles", headers=ling, json={"name": "S"}),
        "linguist -> segprofile create",
    )
    _forbidden(
        api.post("/api/segprofiles/%d/delete" % d["sp_a1"], headers=ling),  # noqa: UP031
        "linguist -> segprofile delete",
    )
    _forbidden(
        api.post(
            "/api/segprofiles/assign",
            headers=ling,
            json={"project_id": d["proj_a1"], "profile_id": d["sp_a1"]},
        ),
        "linguist -> segprofile assign",
    )
    print(
        "  [3] RBAC: translator 403 on can_tune_ai routes, viewer 403 on can_write "
        "routes, linguist 403 on is_admin routes -- all with the shared string"
    )


# --------------------------------------------------------------------------- #
# 4. image kill switch: 404 when image_localization_enabled is off
# --------------------------------------------------------------------------- #
def test_image_kill_switch(d, api):
    h = _h(d["keys"]["admin_a"])
    _image_flag(False)
    try:
        _is(
            api.post(
                "/api/images",
                headers=h,
                files={"file": ("x.svg", _SVG, "image/svg+xml")},
                data={"project_id": str(d["proj_a1"])},
            ),
            404,
            "image upload with feature off",
        )
        _is(
            api.post("/api/images/%d/translate" % d["img_a1"], headers=h, json={"locale": "de-DE"}),  # noqa: UP031
            404,
            "image translate with feature off",
        )
    finally:
        _image_flag(True)
    print(
        "  [4] image kill switch: upload + translate 404 when "
        "image_localization_enabled is off (not enumerable)"
    )


# --------------------------------------------------------------------------- #
# 5. scope-before-validate: foreign path id + bad body -> 404, not 400
# --------------------------------------------------------------------------- #
def test_scope_before_validate(d, api):
    h = _h(d["keys"]["admin_a"])
    # A foreign-org group with a BLANK workflow_id (a body that would 400 on a visible
    # group) is a 404 -- the ring resolves before the body is validated.
    _is(
        api.post("/api/workflow-groups/%d/add" % d["grp_b"], headers=h, json={}),  # noqa: UP031
        404,
        "foreign group + empty body -> 404 not 400",
    )
    # A foreign-org template rename with a BLANK name is a 404, not a 400.
    _is(
        api.post("/api/templates/%d/rename" % d["tpl_b"], headers=h, json={"name": ""}),  # noqa: UP031
        404,
        "foreign template + blank name -> 404",
    )
    # A foreign-org segprofile clone with a BLANK new_name is a 404, not a 400.
    _is(
        api.post("/api/segprofiles/%d/clone" % d["sp_b"], headers=h, json={"new_name": ""}),  # noqa: UP031
        404,
        "foreign segprofile + blank name -> 404",
    )
    print(
        "  [5] scope-before-validate: a foreign path id + a bad body is a 404 "
        "(the ring resolves before input validation), never a 400"
    )


# --------------------------------------------------------------------------- #
# 6. the spend mock-409 (style/context A/B + workflow author)
# --------------------------------------------------------------------------- #
def test_spend_mock_409(d, api):
    h = _h(d["keys"]["ling_a"])  # can_tune_ai
    # A keyless REAL engine (openai, no key -> resolves to mock) is refused (409)
    # BEFORE the harness runs, on every real-engine spend path.
    _is(
        api.post(
            "/api/style-guides/%d/ab" % d["sg_a1"],  # noqa: UP031
            headers=h,
            json={"document": d["doc_a1"], "provider": "openai"},
        ),
        409,
        "style AB keyless-real -> 409",
    )
    _is(
        api.post(
            "/api/context/ab", headers=h, json={"document": d["doc_a1"], "provider": "openai"}
        ),
        409,
        "context AB keyless-real -> 409",
    )
    _is(
        api.post(
            "/api/workflows/author",
            headers=h,
            json={"description": "a formal DE workflow", "provider": "openai"},
        ),
        409,
        "workflow author keyless-real -> 409",
    )
    print(
        "  [6] mock-409: style/context A/B + workflow author refuse a keyless REAL "
        "engine (409) before spending; explicit mock is the escape hatch"
    )


# --------------------------------------------------------------------------- #
# 7. the two-phase translate_image MCP contract (unconfirmed writes nothing)
# --------------------------------------------------------------------------- #
def test_translate_image_two_phase(d):
    import importlib

    _dir = os.path.join(_ROOT, "integrations", "mcp-server")
    if _dir not in sys.path:
        sys.path.insert(0, _dir)
    try:
        server = importlib.import_module("server")
    except Exception as exc:
        reg = importlib.import_module("registry")
        spec = next(t for t in reg.TOOLS if t.name == "translate_image")
        assert spec.tier == "spend", "translate_image must be spend tier"
        assert "confirm" in (spec.input_schema.get("properties") or {}), (
            "a spend tool must declare confirm"
        )
        print(
            f"  [7] two-phase: MCP SDK absent ({type(exc).__name__}); registry confirms translate_image "  # noqa: E501
            "is spend with a confirm gate"
        )
        return

    import asyncio
    import json as _json

    import httpx

    from app.main import app

    server.BASE_URL = "http://testserver"
    server.TOKEN = d["keys"]["admin_a"]
    orig = server._client

    def _patched():
        return httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://testserver",
            headers={"X-Loc-Token": server.TOKEN},
        )

    server._client = _patched
    args = {"document_id": d["img_a1"], "locale": "de-DE"}
    try:
        before = _img_translated(d["img_a1"])
        plan = _json.loads(asyncio.run(server.call_tool_unvalidated("translate_image", dict(args))))
        assert plan.get("plan") is True and plan.get("spent") is False, plan
        assert _img_translated(d["img_a1"]) == before, (
            "an UNCONFIRMED translate_image translated a region"
        )
        # #757: the confirmed call names the plan it confirms
        confirmed = {**args, "confirm": True, "plan_id": plan["plan_id"]}
        asyncio.run(server.call_tool_unvalidated("translate_image", confirmed))
        assert _img_translated(d["img_a1"]) > before, (
            "a CONFIRMED translate_image translated nothing"
        )
        print(
            "  [7] two-phase: an UNCONFIRMED translate_image returns a plan and "
            "translates 0 regions; a CONFIRMED one translates (row-count proven)"
        )
    finally:
        server._client = orig


# --------------------------------------------------------------------------- #
# 8. happy paths for every route (read back where a read exists)
# --------------------------------------------------------------------------- #
def test_happy_paths(d, api):
    h = _h(d["keys"]["admin_a"])  # staff admin in org A (satisfies every gate)

    # --- workflow groups ---
    r = api.post(
        "/api/workflow-groups", headers=h, json={"name": "New Grp", "customer_id": d["cust_a1"]}
    )
    _is(r, 200, "group create")
    new_grp = r.json()["id"]
    r = api.get("/api/workflow-groups", headers=h)
    _is(r, 200, "group list")
    assert any(g["id"] == new_grp for g in r.json()["groups"]), r.text
    r = api.get("/api/workflow-groups/%d" % d["grp_a1"], headers=h)  # noqa: UP031
    _is(r, 200, "group show")
    assert any(v["workflow_id"] == d["wf_a"] for v in r.json()["variants"]), r.text
    r = api.post(
        "/api/workflow-groups/%d/add" % new_grp,  # noqa: UP031
        headers=h,
        json={"workflow_id": d["wf_ungrouped"], "pair": "en-US:de-DE"},
    )
    _is(r, 200, "group add")
    assert r.json()["workflow_id"] == d["wf_ungrouped"], r.text
    r = api.post(
        "/api/workflow-groups/%d/default" % new_grp,  # noqa: UP031
        headers=h,
        json={"workflow_id": d["wf_ungrouped"]},
    )
    _is(r, 200, "group set-default")
    assert r.json()["is_default"] is True, r.text
    # set-default on a non-member -> a clean 400 (not a leak).
    _is(
        api.post(
            "/api/workflow-groups/%d/default" % d["grp_a1"],  # noqa: UP031
            headers=h,
            json={"workflow_id": d["wf_ungrouped"]},
        ),
        400,
        "set-default on a non-member -> 400",
    )

    # --- prompt templates ---
    r = api.post(
        "/api/prompt-templates",
        headers=h,
        json={"name": "New PT", "body": "Translate {text}.", "customer_id": d["cust_a1"]},
    )
    _is(r, 200, "prompt create")
    new_pt = r.json()["id"]
    r = api.get("/api/prompt-templates", headers=h)
    _is(r, 200, "prompt list")
    assert any(t["id"] == new_pt for t in r.json()["prompt_templates"]), r.text

    # --- workflow clone / copy ---
    r = api.post("/api/workflows/%d/clone" % d["wf_a"], headers=h)  # noqa: UP031
    _is(r, 200, "workflow clone")
    assert r.json()["steps"] >= 1 and r.json()["source_id"] == d["wf_a"], r.text
    r = api.post("/api/workflows/%d/copy" % d["wf_a"], headers=h, json={"to_pairs": "en-US:fr-FR"})  # noqa: UP031
    _is(r, 200, "workflow copy")
    assert r.json().get("group_id") is not None, r.text

    # --- project templates ---
    # A customer-scoped create (round-trips) ...
    r = api.post(
        "/api/templates",
        headers=h,
        json={"name": "TPL Cust", "project_id": d["proj_a1"], "scope": "customer"},
    )
    _is(r, 200, "template create (customer)")
    # ... and a GLOBAL create, which an unbound staff /api list surfaces (the /api list
    # mirrors `template list`: customer_id=None -> GLOBAL only; there is no --all arm).
    r = api.post(
        "/api/templates",
        headers=h,
        json={"name": "TPL New", "project_id": d["proj_a1"], "scope": "global"},
    )
    _is(r, 200, "template create (global)")
    new_tpl = r.json()["template"]["id"]
    r = api.get("/api/templates", headers=h)
    _is(r, 200, "template list")
    assert any(t["id"] == new_tpl for t in r.json()["templates"]), r.text
    r = api.get("/api/templates/%d" % d["tpl_a1"], headers=h)  # noqa: UP031
    _is(r, 200, "template show")
    r = api.post("/api/templates/%d/rename" % new_tpl, headers=h, json={"name": "TPL Renamed"})  # noqa: UP031
    _is(r, 200, "template rename")
    assert r.json()["template"]["name"] == "TPL Renamed", r.text
    r = api.post(
        "/api/templates/%d/apply" % d["tpl_a1"],  # noqa: UP031
        headers=h,
        json={"name": "From Template", "customer_id": d["cust_a1"]},
    )
    _is(r, 200, "template apply")
    assert r.json().get("project_id"), r.text
    r = api.post("/api/templates/%d/delete" % new_tpl, headers=h)  # noqa: UP031
    _is(r, 200, "template delete (admin)")
    assert r.json()["deleted"] is True, r.text

    # --- style / context A/B (mock escape hatch: measurement runs, writes nothing) ---
    r = api.post(
        "/api/style-guides/%d/ab" % d["sg_a1"],  # noqa: UP031
        headers=h,
        json={"document": d["doc_a1"], "provider": "mock"},
    )
    _is(r, 200, "style AB (mock)")
    assert "ab" in r.json() and r.json()["style_guide_id"] == d["sg_a1"], r.text
    r = api.post("/api/context/ab", headers=h, json={"document": d["doc_a1"], "provider": "mock"})
    _is(r, 200, "context AB (mock)")
    assert "ab" in r.json(), r.text

    # --- context packages ---
    r = api.post(
        "/api/context/profiles",
        headers=h,
        json={
            "customer_id": d["cust_a1"],
            "name": "A1 default",
            "context": {"formality": "formal", "notes": "House voice."},
        },
    )
    _is(r, 200, "context set-profile")
    assert r.json()["profile"]["customer_id"] == d["cust_a1"], r.text
    r = api.post(
        "/api/context/documents/%d" % d["doc_a1"],  # noqa: UP031
        headers=h,
        json={"context": {"notes": "Doc override."}},
    )
    _is(r, 200, "context set-document")
    r = api.get(
        "/api/context/resolve?customer=%d&document=%d" % (d["cust_a1"], d["doc_a1"]),  # noqa: UP031
        headers=h,  # noqa: RUF100, UP031
    )
    _is(r, 200, "context resolve")
    assert "block" in r.json() and "context" in r.json(), r.text

    # --- segmentation profiles (is_admin) ---
    r = api.post("/api/segprofiles", headers=h, json={"name": "SP New", "description": "custom"})
    _is(r, 200, "segprofile create")
    new_sp = r.json()["segprofile"]["id"]
    r = api.get("/api/segprofiles", headers=h)
    _is(r, 200, "segprofile list")
    assert any(p["id"] == new_sp for p in r.json()["segprofiles"]), r.text
    r = api.get("/api/segprofiles/%d" % new_sp, headers=h)  # noqa: UP031
    _is(r, 200, "segprofile show")
    r = api.post("/api/segprofiles/%d/clone" % new_sp, headers=h, json={"new_name": "SP Clone"})  # noqa: UP031
    _is(r, 200, "segprofile clone")
    clone_sp = r.json()["segprofile"]["id"]
    r = api.post("/api/segprofiles/%d/rename" % clone_sp, headers=h, json={"name": "SP Clone 2"})  # noqa: UP031
    _is(r, 200, "segprofile rename")
    r = api.post("/api/segprofiles/%d/set-default" % new_sp, headers=h, json={})  # noqa: UP031
    _is(r, 200, "segprofile set-default")
    assert r.json()["segprofile"]["is_system_default"] is True, r.text
    r = api.post(
        "/api/segprofiles/assign",
        headers=h,
        json={"project_id": d["proj_a1"], "profile_id": new_sp},
    )
    _is(r, 200, "segprofile assign")
    assert r.json()["segmentation_profile_id"] == new_sp, r.text
    r = api.post("/api/segprofiles/%d/delete" % clone_sp, headers=h)  # noqa: UP031
    _is(r, 200, "segprofile delete")
    assert r.json()["deleted"] is True, r.text

    # --- image upload (translate happy path is the two-phase confirmed leg) ---
    r = api.post(
        "/api/images",
        headers=h,
        files={"file": ("fresh.svg", _SVG, "image/svg+xml")},
        data={"project_id": str(d["proj_a1"])},
    )
    _is(r, 200, "image upload")
    assert r.json().get("document_id"), r.text
    print(
        "  [8] happy paths: workflow groups, prompt templates, clone/copy, project "
        "templates (create/list/show/rename/apply/delete), style+context A/B, context "
        "profile/document/resolve, the segprofile family, image upload all round-trip"
    )


# --------------------------------------------------------------------------- #
def main() -> None:
    d = _seed()
    _gate_on()
    try:
        api = _client()
        test_cross_org_404(d, api)
        test_customer_bound_ring(d, api)
        test_template_apply_customer_ring(d, api)
        test_rbac(d, api)
        test_image_kill_switch(d, api)
        test_scope_before_validate(d, api)
        test_spend_mock_409(d, api)
        test_translate_image_two_phase(d)
        test_happy_paths(d, api)
    finally:
        _gate_off()
    print("R1B CONTENT PARITY OK")


if __name__ == "__main__":
    main()
