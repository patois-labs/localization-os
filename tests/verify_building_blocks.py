"""Workflow Studio Wave C — the "Building blocks" tab — verification. Offline + hermetic.

The former top-level "Templates" page is FOLDED into the Workflows area as the
"Building blocks" tab (/workflows/building-blocks). This suite proves:

  redirects
    - GET /templates 302s to /workflows/building-blocks (no dead bookmark); a server-sent
      ?err= flash is carried through and shown, a crafted one is dropped (page guard).
  nav retirement
    - the top-level "Templates" nav item is gone (no href="/templates" in the chrome).
  tab strip / IA coherence
    - /workflows shows the Dashboard|Building blocks tab strip with Dashboard active;
      /workflows/building-blocks shows it with Building blocks active + a link back.
  prompt-library CRUD intact (moved, not rebuilt)
    - create -> rename -> delete round-trips on the new page; the create/rename/delete
      routes still redirect to the new tab; the admin delete GUARD fails closed (409)
      while a workflow step pins the template, and succeeds once detached.
  role presets read-only
    - every built-in ROLE_PRESET renders with a "built-in" badge + its body, and carries
      NO rename/delete form (not editable/deletable).
  cross-links (reference-count display policy — review MEDIUM)
    - UNBOUND STAFF see the full honest count + a "(+N not shown)" hint for bindings they
      can't name; a RING-CONFINED viewer (customer-bound) sees the VISIBLE INTERSECTION
      ONLY (count = visible workflows, no volume hint) so a shared global template never
      leaks cross-customer usage volume.
  scoping
    - a customer-bound linguist's library shows GLOBAL + own only (a foreign customer's
      template is absent) on the rendered page.
    - two-org: with the multi-org gate ON, org B's Building-blocks library shows only
      org B's templates (org A's absent), on both the list seam and the rendered page.

Final line: "BUILDING BLOCKS OK".
"""

import os
import re
import sys
from urllib.parse import parse_qs

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(_ROOT)
sys.path.insert(0, _ROOT)
from tests._bootstrap import init_env

init_env("loctms_bblocks_")

from fastapi.testclient import TestClient
from sqlmodel import Session, select

from app.db import engine, init_db
from app.main import app
from app.models import (
    Organization,
    PromptTemplate,
    Role,
    Scope,
    StepType,
    User,
    Workflow,
    WorkflowStep,
)
from app.services import orchestrator, security, settings
from app.services import workflows as wf_service
from tests._csrf import csrf_for

init_db()

BB = "/workflows/building-blocks"


# --- helpers -----------------------------------------------------------------
def _auth_on():
    settings.set_many({"security_auth_required": "1", "multi_org_enabled": "0"})
    settings.invalidate_cache()


def _auth_off():
    settings.set_many({"security_auth_required": "0", "multi_org_enabled": "0"})
    settings.invalidate_cache()


def _gate_on():
    settings.set_many({"security_auth_required": "1", "multi_org_enabled": "1"})
    settings.invalidate_cache()


def _flat(html):
    """Collapse HTML whitespace runs to single spaces so a count phrase that the
    template wraps across lines/indentation still matches a plain substring probe."""
    return re.sub(r"\s+", " ", html)


def login(c, email, pw):
    csrf_for(c)
    r = c.post(
        "/auth/login", data={"email": email, "password": pw, "next": "/"}, follow_redirects=False
    )
    if r.status_code == 303:
        csrf_for(c)
    return r


def _mk_workflow(name, *, scope=Scope.GLOBAL, customer_id=None, org_id=None):
    with Session(engine) as s:
        w = Workflow(name=name, scope=scope, customer_id=customer_id, org_id=org_id)
        s.add(w)
        s.commit()
        s.refresh(w)
        return w.id


def _bind_step(wf_id, pt_id):
    with Session(engine) as s:
        existing = len(orchestrator.get_steps(s, wf_id))
        st = WorkflowStep(
            workflow_id=wf_id,
            order_index=existing,
            name="s",
            step_type=StepType.LLM_AGENT,
            prompt_text="Translate {text}",
            prompt_template_id=pt_id,
        )
        s.add(st)
        s.commit()
        s.refresh(st)
        return st.id


# ============================================================================
def test_redirect_from_old_templates_paths():
    """GET /templates (and its ?err= flash) 302s to the new tab — no dead bookmark."""
    _auth_off()
    c = TestClient(app)
    r = c.get("/templates", follow_redirects=False)
    assert r.status_code == 302, f"GET /templates must redirect, got {r.status_code}"
    assert r.headers["location"] == BB, "GET /templates must 302 to {}, got {!r}".format(
        BB,
        r.headers.get("location"),
    )
    # A CRAFTED ?err= the server never sent this session is dropped by the page guard
    # (#248, and gate round 1 LOW-2 in 28015a5f: an unknown code-shaped value such as
    # "boom" needs a server record too), so the hop must NOT carry it: a link cannot put
    # attacker-written text on the product's page.
    r2 = c.get("/templates?err=boom", follow_redirects=False)
    assert r2.status_code == 302 and r2.headers["location"] == BB, (
        "an unrecorded ?err= must be stripped, not carried ({!r})".format(
            r2.headers.get("location")
        )
    )
    # A REAL error the server sent (a failed POST CRUD route bounces it back as ?err=, and
    # the page guard records it for this session) survives the /templates hop and the user
    # SEES the sentence on the Building blocks tab.
    bad = c.post(
        "/templates/prompts",
        data={"name": "BB Flash", "body": "x", "customer_id": "987654"},
        follow_redirects=False,
    )
    loc = bad.headers.get("location", "")
    assert bad.status_code == 303 and loc.startswith(BB + "?err="), (
        f"a refused create must bounce its error to the tab, got {bad.status_code} {loc!r}"
    )
    flash = loc[len(BB) :]  # "?err=<server-written text>"
    sent = parse_qs(flash[1:])["err"]
    r3 = c.get("/templates" + flash, follow_redirects=False)
    hop = r3.headers.get("location", "")
    assert (
        r3.status_code == 302
        and hop.startswith(BB + "?")
        and parse_qs(hop.split("?", 1)[1]).get("err") == sent
    ), "a server-sent ?err= must survive the /templates redirect ({!r})".format(
        r3.headers.get("location")
    )
    page = c.get("/templates" + flash)
    assert page.status_code == 200 and "customer 987654 is not available" in page.text, (
        "the server-sent error must be visible after the /templates hop"
    )
    # following the redirect lands on a real 200 page
    assert c.get("/templates").status_code == 200, "following /templates must reach a live page"
    print(
        f"  redirect: GET /templates -> {BB} (302; crafted err= dropped, server-sent err= "
        "carried and shown; lands 200)"
    )


def test_nav_item_retired():
    """The top-level Templates nav item is gone from the app chrome."""
    _auth_off()
    c = TestClient(app)
    home = c.get("/").text
    assert 'href="/templates"' not in home, (
        "the retired Templates nav item still links to /templates in the chrome"
    )
    # the dashboard quick-action now points at the new tab, not the retired page
    assert BB in home, "dashboard quick-action should link to the Building blocks tab"
    print('  nav: top-level Templates item retired (no href="/templates" in chrome)')


def test_tab_strip_coherence():
    """Dashboard|Building blocks tab strip on both surfaces, correct active state."""
    _auth_off()
    c = TestClient(app)
    dash = c.get("/workflows").text
    assert 'class="tabs wf-area-tabs"' in dash, "dashboard missing the Workflows tab strip"
    assert (f'href="{BB}"') in dash, "dashboard tab strip must link to Building blocks"
    # Dashboard tab is the active one on /workflows
    assert 'href="/workflows"' in dash and 'aria-current="page">Dashboard' in dash, (
        "Dashboard tab should be marked active on /workflows"
    )

    bb = c.get(BB).text
    assert 'class="tabs wf-area-tabs"' in bb, "building-blocks missing the Workflows tab strip"
    assert 'href="/workflows"' in bb, "building-blocks tab strip must link back to Dashboard"
    assert 'aria-current="page">Building blocks' in bb, (
        "Building blocks tab should be marked active on its own page"
    )
    print("  tabs: Dashboard|Building blocks strip on both surfaces, active state honest")


def test_prompt_library_crud_roundtrip_and_delete_guard():
    """CRUD moved intact: create/rename/delete round-trip, routes redirect to the new
    tab, and the admin delete guard fails closed while a workflow step pins the PT."""
    _auth_on()
    security.seed_initial()  # admin/admin (+ linguist, user1)
    c = TestClient(app)
    assert login(c, "admin", "admin").status_code == 303

    # CREATE -> redirect to the new tab, template shows on the page
    r = c.post(
        "/templates/prompts",
        data={"name": "BB Roundtrip", "body": "Translate {text}"},
        follow_redirects=False,
    )
    assert r.status_code == 303 and r.headers["location"] == BB, (
        "create must 303 to {}, got {} {!r}".format(BB, r.status_code, r.headers.get("location"))
    )
    assert "BB Roundtrip" in c.get(BB).text, "created template not on the Building blocks tab"
    with Session(engine) as s:
        pt_id = s.exec(
            select(PromptTemplate.id).where(PromptTemplate.name == "BB Roundtrip")
        ).first()

    # RENAME -> redirect to the new tab, new name shows
    r = c.post(
        "/templates/prompts/%d/rename" % pt_id,  # noqa: UP031
        data={"name": "BB Renamed"},
        follow_redirects=False,  # noqa: RUF100, UP031
    )
    assert r.status_code == 303 and r.headers["location"] == BB
    body = c.get(BB).text
    assert "BB Renamed" in body and "BB Roundtrip" not in body, "rename didn't take"

    # DELETE GUARD: bind the PT to a workflow step, then the admin delete fails closed.
    wf_id = _mk_workflow("BB Guard WF")
    step_id = _bind_step(wf_id, pt_id)
    r = c.post("/templates/prompts/%d/delete" % pt_id, follow_redirects=False)  # noqa: UP031
    assert r.status_code == 409, (
        f"delete must FAIL CLOSED (409) while a step pins the PT, got {r.status_code}"
    )
    with Session(engine) as s:
        assert s.get(PromptTemplate, pt_id) is not None, "guarded PT must survive the 409"

    # DETACH the step -> the (now unreferenced) PT deletes cleanly, back to the tab.
    with Session(engine) as s:
        s.delete(s.get(WorkflowStep, step_id))
        s.commit()
    r = c.post("/templates/prompts/%d/delete" % pt_id, follow_redirects=False)  # noqa: UP031
    assert r.status_code == 303 and r.headers["location"] == BB, (
        f"unreferenced PT delete must 303 to the tab, got {r.status_code}"
    )
    with Session(engine) as s:
        assert s.get(PromptTemplate, pt_id) is None, "PT should be gone after detach+delete"
    print("  CRUD: create/rename/delete round-trip on the tab; delete guard 409 then clean")


def test_role_presets_read_only():
    """Built-in ROLE_PRESETS render read-only: badge + body, no edit/delete controls."""
    _auth_on()
    security.seed_initial()
    c = TestClient(app)
    assert login(c, "linguist", "linguist").status_code == 303
    body = c.get(BB).text
    assert 'class="chip bb-builtin"' in body and ">built-in<" in body, (
        "role presets must carry a 'built-in' badge"
    )
    # every preset name + its body appears; NONE carries a rename/delete form (only the
    # library templates do, and there are none seeded for this fresh check on presets).
    for name, ptext in orchestrator.ROLE_PRESETS.items():  # noqa: B007
        assert name in body, f"role preset {name!r} not rendered"
    # a preset never emits an editable name input or a delete form of its own — those
    # controls target /templates/prompts/<id>/..., which presets (no id) never produce.
    presets_section = body.split("Role presets", 1)[1]
    assert "/templates/prompts/" not in presets_section.split("</section>", 1)[0], (
        "role presets section must contain no CRUD (edit/delete) controls"
    )
    print("  presets: built-in badge + body, read-only (no CRUD controls) OK")


def test_cross_link_reference_count_honesty():
    """Cross-link display policy (review MEDIUM). UNBOUND STAFF get the full honest
    count + a "(+N not shown)" hint for bindings they can't name. A RING-CONFINED viewer
    (customer-bound) gets the VISIBLE INTERSECTION ONLY — count = visible workflows, NO
    volume hint — so a shared global template never leaks cross-customer usage volume."""
    _auth_on()
    security.seed_initial()
    # A GLOBAL template bound in TWO workflows: one GLOBAL (any admin sees it) and one
    # scoped to a customer a bound linguist can't see.
    from app.models import Customer

    with Session(engine) as s:
        fc = Customer(name="BB Hidden Cust")
        s.add(fc)
        s.commit()
        s.refresh(fc)
        cust_id = fc.id
        pt = PromptTemplate(name="BB Shared PT", body="Translate {text}", scope=Scope.GLOBAL)
        s.add(pt)
        s.commit()
        s.refresh(pt)
        pt_id = pt.id
    vis_wf = _mk_workflow("BB Visible WF", scope=Scope.GLOBAL)
    hidden_wf = _mk_workflow("BB Hidden WF", scope=Scope.CUSTOMER, customer_id=cust_id)
    _bind_step(vis_wf, pt_id)
    _bind_step(hidden_wf, pt_id)

    # UNBOUND STAFF (admin sees both workflows): full honest 2/2, both named, none hidden.
    c = TestClient(app)
    assert login(c, "admin", "admin").status_code == 303
    body = _flat(c.get(BB).text)
    assert "Bound to 2 steps in 2 workflows" in body, (
        "unbound staff cross-link count must be full + honest (2 steps / 2 workflows)"
    )
    assert "BB Visible WF" in body and "BB Hidden WF" in body, (
        "unbound staff should see BOTH bound workflow names"
    )

    # RING-CONFINED viewer (a linguist bound to a DIFFERENT customer): the global PT is
    # visible, but its cross-link shows ONLY the intersection (1 step / 1 workflow —
    # the global one), the foreign workflow is neither named NOR counted, and there is
    # NO "(+N not shown)" volume hint. This is the deliberate no-volume-leak policy.
    with Session(engine) as s:
        oc = Customer(name="BB Viewer Cust")
        s.add(oc)
        s.commit()
        s.refresh(oc)
        other = oc.id
        bl = User(
            email="bb-bound@x.test",
            name="BL",
            role=Role.LINGUIST,
            active=True,
            customer_id=other,
            password_hash=security.hash_password("pw-123456789"),
        )
        s.add(bl)
        s.commit()
    bc = TestClient(app)
    assert login(bc, "bb-bound@x.test", "pw-123456789").status_code == 303
    bbody = _flat(bc.get(BB).text)
    assert "BB Shared PT" in bbody, "bound linguist should see the GLOBAL shared PT"
    assert "Bound to 1 step in 1 workflow" in bbody, (
        "ring-confined viewer must see the VISIBLE-ONLY count (1 step / 1 workflow)"
    )
    assert "BB Visible WF" in bbody, "the visible (global) bound workflow should be named"
    assert "BB Hidden WF" not in bbody, (
        "a workflow outside the bound viewer's scope must NOT be named"
    )
    assert "not shown" not in bbody, (
        "ring-confined viewer must get NO cross-customer volume hint ('(+N not shown)')"
    )
    assert "Bound to 2 " not in bbody, (
        "ring-confined viewer must never see the full cross-customer count"
    )
    print(
        "  cross-links: staff full+honest with hidden note; ring-confined visible-only, "
        "no volume leak OK"
    )


def test_bound_linguist_library_scoping():
    """A customer-bound linguist's library = GLOBAL + own; a foreign customer's PT absent."""
    _auth_on()
    security.seed_initial()
    with Session(engine) as s:
        from app.models import Customer

        home = Customer(name="BB Home Cust")
        foreign = Customer(name="BB Foreign Cust")
        s.add(home)
        s.add(foreign)
        s.commit()
        s.refresh(home)
        s.refresh(foreign)
        hid, fid = home.id, foreign.id
        s.add(PromptTemplate(name="BB-glob", body="b", scope=Scope.GLOBAL))
        s.add(PromptTemplate(name="BB-own", body="b", scope=Scope.CUSTOMER, customer_id=hid))
        s.add(PromptTemplate(name="BB-foreign", body="b", scope=Scope.CUSTOMER, customer_id=fid))
        s.commit()
        bu = User(
            email="bb-scope@x.test",
            name="BU",
            role=Role.LINGUIST,
            active=True,
            customer_id=hid,
            password_hash=security.hash_password("pw-123456789"),
        )
        s.add(bu)
        s.commit()
    bc = TestClient(app)
    assert login(bc, "bb-scope@x.test", "pw-123456789").status_code == 303
    body = bc.get(BB).text
    assert "BB-glob" in body and "BB-own" in body, "bound viewer sees GLOBAL + own"
    assert "BB-foreign" not in body, "bound viewer must NOT see a foreign customer's PT"
    print("  scoping: bound linguist library = GLOBAL + own (foreign PT absent) OK")


def test_two_org_library_isolation():
    """Multi-org gate ON: org B's Building-blocks library shows only org B's templates."""
    init_db()
    with Session(engine) as s:
        oa = Organization(name="BB Org A")
        ob = Organization(name="BB Org B")
        s.add(oa)
        s.add(ob)
        s.commit()
        s.refresh(oa)
        s.refresh(ob)
        oa_id, ob_id = oa.id, ob.id
        s.add(PromptTemplate(name="BB-Tmpl-A", body="a", org_id=oa_id))
        s.add(PromptTemplate(name="BB-Tmpl-B", body="b", org_id=ob_id))
        s.commit()
    # service seam (the list the page renders): org B sees only B's.
    with Session(engine) as s:
        b_names = {t.name for t in wf_service.list_prompt_templates(s, org_id=ob_id)}
    assert "BB-Tmpl-B" in b_names and "BB-Tmpl-A" not in b_names, (
        "list_prompt_templates(org_id=B) leaked org A's template"
    )
    # rendered page: log in as an org-B admin with the gate on.
    ub = security.create_user("bb-orgb@x.test", "OB", Role.ADMIN, "pw-123456789")
    with Session(engine) as s:
        u = s.get(User, ub.id)
        u.org_id = ob_id
        s.add(u)
        s.commit()
    _gate_on()
    try:
        c = TestClient(app)
        assert login(c, "bb-orgb@x.test", "pw-123456789").status_code == 303
        body = c.get(BB).text
        assert "BB-Tmpl-B" in body and "BB-Tmpl-A" not in body, (
            "org B's Building-blocks page leaked org A's template"
        )
    finally:
        _auth_off()
    print("  two-org: Building-blocks library org-isolated (list seam + rendered page) OK")


def main() -> None:
    test_redirect_from_old_templates_paths()
    test_nav_item_retired()
    test_tab_strip_coherence()
    test_prompt_library_crud_roundtrip_and_delete_guard()
    test_role_presets_read_only()
    test_cross_link_reference_count_honesty()
    test_bound_linguist_library_scoping()
    test_two_org_library_isolation()
    print("BUILDING BLOCKS OK")


if __name__ == "__main__":
    main()
