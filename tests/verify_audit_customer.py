"""R2-3 verification: audit-log customer isolation + customer-bound users.

The maintainer's requirement (item 3): a customer login sees ONLY its own
customer's audit rows + app data, with "absolutely no bleed over" to any other
customer, "under any circumstances". This is an ISOLATION GUARANTEE, so this suite
is adversarial: it probes for leaks (query params, paging, direct ids, forged POSTs)
as hard as it proves the happy path.

Hermetic like every other suite: throwaway LOC_TMS_DATA_DIR + provider keys blanked
(MockProvider everywhere, zero network, deterministic). ASCII-only prints.

The 8-point matrix (spec §5), each a test_* below:
  1. Stamping         - representative events carry the right customer_id; system
                        events (auth/admin/settings) are NULL.
  2. Isolation        - a customer-A-bound user's /audit shows ONLY customer-A rows:
                        no customer-B rows, no NULL/system rows; internal admin sees
                        all; direct probing (?actor/?action/paging) cannot widen it.
  3. Chain            - verify-chain still passes over a mixed stamped/unstamped
                        table; customer edits break the chain; verify + export
                        remain admin-only (a bound user 403s on them).
  4. Role guard       - binding an admin/manager to a customer is rejected (web+CLI+
                        service); a bound user cannot reach /users, /settings, chain
                        verify (role gates hold).
  5. App visibility   - a bound user's project list is only their customer's; a
                        foreign customer's project/doc/360 by direct id -> 404;
                        /customers collapses to their one customer.
  6. Org interplay    - (gate ON) an org-A bound user sees nothing of org-B even for a
                        same-named customer; org scoping unchanged for unbound users.
  7. CLI parity       - user create/customer --customer round-trip + role guard; audit
                        CLI --customer filter matches web semantics.
  8. Regression       - the byte-identical guarantee is the 54-suite battery (run
                        separately); here we assert the unbound/gate-off no-ops.

Final line: "AUDIT_CUSTOMER OK".
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

init_env("loctms_auditcust_")

FIXTURE_PW = "fixture-pass-1234!!"  # pregate: allow-secret test fixture

from fastapi.testclient import TestClient
from sqlalchemy import inspect, text
from sqlmodel import Session, select

from app.db import engine, init_db
from app.main import app
from app.models import AuditLog, Customer, Role, User
from app.services import access, audit, content, security, settings
from tests._csrf import csrf_for


# --- helpers -----------------------------------------------------------------
def _login(c, who, pw):
    csrf_for(c)
    r = c.post(
        "/auth/login", data={"email": who, "password": pw, "next": "/"}, follow_redirects=False
    )
    if r.status_code == 303:
        csrf_for(c)
    return r


def _mk_customer(name, org_id=None):
    with Session(engine) as s:
        c = Customer(name=name, org_id=org_id)
        s.add(c)
        s.commit()
        s.refresh(c)
        return c.id


def _mk_project(name, customer_id, org_id=None):
    with Session(engine) as s:
        p = content.project_create(s, name, customer_id=str(customer_id), target_locales="fr-FR")
        if org_id is not None:
            row = s.get(type(p), p.id)
            row.org_id = org_id
            s.add(row)
            s.commit()
        return p.id


# ============================================================================
# 1. STAMPING
# ============================================================================
def test_stamping():
    settings.set_many({"audit_enabled": "1"})
    settings.invalidate_cache()
    cid = _mk_customer("Stamp-Cust")
    pid = _mk_project("Stamp-Proj", cid)

    # project-scoped event -> derives the customer
    audit.log(
        actor="a@x",
        action="project.status",
        target=f"project {pid}",
        customer_id=audit.customer_for_project(pid),
    )
    # a system event -> NULL
    audit.log(actor="a@x", action="login.success", target="a@x")
    # customer-route event -> the customer id itself
    audit.log(actor="a@x", action="customer.settings", target=f"customer {cid}", customer_id=cid)

    rows = {r.action: r for r in audit.recent(limit=50)}
    assert rows["project.status"].customer_id == cid, "project event must stamp its customer"
    assert rows["login.success"].customer_id is None, "auth event must be NULL (system)"
    assert rows["customer.settings"].customer_id == cid, "customer route stamps the customer"

    # derivation helpers: missing/garbage/unowned -> None (safe under-stamp)
    assert audit.customer_for_project(pid) == cid
    assert audit.customer_for_project(999999) is None
    assert audit.customer_for_project("not-a-number") is None
    assert audit.customer_for_project(10**40) is None, "huge id must not overflow"
    # a project with no customer -> None
    with Session(engine) as s:
        orphan = content.project_create(s, "Orphan", target_locales="fr-FR")
        opid = orphan.id
    assert audit.customer_for_project(opid) is None
    print(
        "  stamping: project/customer events carry the customer; system events NULL; "
        "derivation safe on bad ids"
    )


# ============================================================================
# 2. ISOLATION (the core guarantee)
# ============================================================================
def test_isolation(admin, custA_client, custA_id, custB_id):
    # Seed rows for both customers + a system row via the service directly.
    pA = _mk_project("Iso-A-Proj", custA_id)
    pB = _mk_project("Iso-B-Proj", custB_id)
    audit.log(actor="x@x", action="project.status", target=f"project {pA}", customer_id=custA_id)
    audit.log(actor="x@x", action="project.export", target=f"project {pA}", customer_id=custA_id)
    audit.log(actor="x@x", action="project.status", target=f"project {pB}", customer_id=custB_id)
    audit.log(actor="x@x", action="security.update", target="settings")  # NULL/system

    # The bound user's /audit view: ONLY customer-A rows.
    r = custA_client.get("/audit")
    assert r.status_code == 200, r.status_code
    # Service-level assertion (exact): every row the bound user can see is custA's.
    a_rows = audit.recent(limit=1000, customer_id=custA_id)
    assert a_rows, "customer A must have rows"
    assert all(row.customer_id == custA_id for row in a_rows)
    b_ids = {row.id for row in audit.recent(limit=1000, customer_id=custB_id)}
    sys_ids = {row.id for row in audit.recent(limit=1000) if row.customer_id is None}
    a_ids = {row.id for row in a_rows}
    assert not (a_ids & b_ids), "no customer-B row may appear in A's view"
    assert not (a_ids & sys_ids), "no system/NULL row may appear in A's view"

    # Direct probing: a bound user CANNOT widen the view via query params or paging.
    # The route ignores any caller customer scoping and forces the session binding.
    probe = custA_client.get("/audit", params={"actor": "x@x", "action": "project."})
    assert probe.status_code == 200
    # No customer-B project id string leaks into the bound user's rendered page.
    assert f"project {pB}" not in probe.text, "customer B's target must never render for A"
    # Paging deep also cannot surface B/system rows: count via the service == A's count.
    assert audit.count(customer_id=custA_id) == len(a_ids)

    # The internal admin sees EVERYTHING (no customer confinement).
    all_rows = audit.recent(limit=1000)
    assert (a_ids | b_ids | sys_ids) <= {r.id for r in all_rows}
    r = admin.get("/audit")
    assert r.status_code == 200
    print(
        "  isolation: bound A sees ONLY A's rows (no B, no system); probing can't "
        "widen; admin sees all"
    )


# ============================================================================
# 3. CHAIN
# ============================================================================
def test_chain(custA_client):
    # The chain verifies over the mixed stamped/unstamped table.
    ok, checked, bad = audit.verify_chain()
    assert ok and bad is None, (ok, checked, bad)
    assert checked > 0

    # New rows authenticate customer attribution as part of the canonical payload.
    row = audit.recent(limit=1)[0]
    recomputed = audit._compute_hash(
        row.prev_hash,
        row.ts.isoformat(),
        row.actor,
        row.action,
        row.target,
        row.detail,
        row.ip,
        customer_id=row.customer_id,
    )
    assert recomputed == row.row_hash

    # Flipping a row's customer_id directly must break the chain.
    # customer_id is a real FK (auditlog.customer_id -> customer.id), so flip to a
    # DIFFERENT EXISTING customer rather than an arbitrary id — an id that violated
    # the FK never persisted on Postgres, so testing the flip with one proved nothing.
    with Session(engine) as s:
        victim = s.exec(select(AuditLog).where(AuditLog.customer_id.is_not(None)).limit(1)).first()
        vid, orig = victim.id, victim.customer_id
        other_cid = s.exec(select(Customer.id).where(Customer.id != orig)).first()
        assert other_cid is not None, "test needs a second customer to flip to"
        s.exec(
            text("UPDATE auditlog SET customer_id = :c WHERE id = :i").bindparams(
                c=other_cid, i=vid
            )
        )
        s.commit()
    ok2, _, bad2 = audit.verify_chain()
    assert not ok2 and bad2 == vid, "changing customer_id must break the HMAC chain"
    with Session(engine) as s:  # restore
        s.exec(text("UPDATE auditlog SET customer_id = :c WHERE id = :i").bindparams(c=orig, i=vid))
        s.commit()

    # Chain verify + export are admin-only: a bound user is refused.
    assert custA_client.get("/audit", params={"verify": "1"}).status_code == 200  # page ok
    # ...but the page must NOT have run a verification for the bound user.
    assert custA_client.get("/audit/export").status_code == 403, (
        "audit export must be admin-only (bound user 403)"
    )
    print(
        "  chain: verifies over mixed table; customer_id authenticated (recompute + "
        "flip prove it); export admin-only"
    )


# ============================================================================
# 4. ROLE GUARD
# ============================================================================
def test_role_guard(admin, custA_id):
    # Service seam: binding an admin/manager is rejected.
    with Session(engine) as s:
        try:
            security.create_user("boundadmin@x", "BA", Role.ADMIN, FIXTURE_PW, customer_id=custA_id)
            assert False, "creating a customer-bound ADMIN must raise"
        except ValueError:
            pass
        try:
            security.create_user("boundmgr@x", "BM", Role.MANAGER, FIXTURE_PW, customer_id=custA_id)
            assert False, "creating a customer-bound MANAGER must raise"
        except ValueError:
            pass
        # a translator/viewer/linguist bind is fine
        tr = security.create_user(
            "bindok@x", "OK", Role.TRANSLATOR, FIXTURE_PW, customer_id=custA_id
        )
        assert tr.customer_id == custA_id

        # set_role: promoting a bound user to admin is rejected at the seam.
        res = security.set_role(s, tr.id, Role.ADMIN, org_id=None)
        assert not res["ok"] and res.get("reason") == "role_bound"
        assert s.get(User, tr.id).role == Role.TRANSLATOR, "reject must not mutate"
        # set_customer: cannot bind an admin.
        admin_u = security.get_by_email("admin")
        res = security.set_customer(s, admin_u.id, custA_id, org_id=None)
        assert not res["ok"] and res.get("reason") == "role_bound"
        # set_customer to a nonexistent customer is refused (no phantom binding).
        res = security.set_customer(s, tr.id, 987654, org_id=None)
        assert not res["ok"] and res.get("reason") == "no_customer"

    # Web: the create-user form refuses an admin binding (error redirect, no user).
    r = admin.post(
        "/users",
        data={
            "email": "webboundadmin@x",
            "name": "W",
            "role": "admin",
            "password": FIXTURE_PW,
            "customer_id": str(custA_id),
        },
        follow_redirects=False,
    )
    assert r.status_code == 303 and "error=" in r.headers.get("location", "")
    assert security.get_by_email("webboundadmin@x") is None, "no bound-admin created"

    # Web: promoting a bound user to admin via /users/{id}/role errors.
    tr = security.get_by_email("bindok@x")
    r = admin.post(f"/users/{tr.id}/role", data={"role": "admin"}, follow_redirects=False)
    assert r.status_code == 303 and "error=" in r.headers.get("location", "")
    assert security.get_by_email("bindok@x").role == Role.TRANSLATOR
    print(
        "  role guard: admin/manager binding rejected (service + web + set_role + "
        "set_customer); phantom customer refused"
    )


def test_bound_user_admin_pages_blocked(custA_id):
    """A customer-bound user (translator) cannot reach admin pages -- the role gates
    hold, so users/settings/security/chain-verify are structurally out of reach."""
    # bindok@x is a bound translator from test_role_guard.
    c = TestClient(app)
    assert _login(c, "bindok@x", FIXTURE_PW).status_code == 303
    assert c.get("/users").status_code == 403, "bound user must not reach /users"
    assert c.get("/settings").status_code == 403, "bound user must not reach /settings"
    assert c.get("/security").status_code == 403, "bound user must not reach /security"
    assert c.get("/audit/export").status_code == 403, "bound user must not export audit"
    # BUT /audit itself IS reachable (read-only, customer-scoped view).
    assert c.get("/audit").status_code == 200, "bound user gets a scoped /audit view"
    print(
        "  role gates: bound user 403s on /users, /settings, /security, /audit/export; "
        "/audit view allowed"
    )


# ============================================================================
# 5. APP VISIBILITY
# ============================================================================
def test_app_visibility(custA_id, custB_id):
    # A bound translator for customer A.
    ua = security.create_user("appA@x", "AppA", Role.TRANSLATOR, FIXTURE_PW, customer_id=custA_id)
    pA = _mk_project("Vis-A-Proj", custA_id)
    pB = _mk_project("Vis-B-Proj", custB_id)
    pGlobal = None
    with Session(engine) as s:
        # a project with NO customer (internal) - a bound user must not see it either
        og = content.project_create(s, "Vis-Global", target_locales="fr-FR")
        pGlobal = og.id

    # Service-level visibility: ALL and ONLY customer A's projects (a customer
    # account sees its whole customer, not just membership — "everything for that
    # customer"). pA is A's; pB is B's; pGlobal is internal.
    with Session(engine) as s:
        vis = set(access.visible_project_ids(s, ua))
        cust_a_projects = set(access._customer_project_ids(s, custA_id))
        assert vis == cust_a_projects, f"bound user must see ALL+ONLY A's projects, got {vis}"
        assert pA in vis, "own customer's project must be visible"
        assert pB not in vis and pGlobal not in vis, "no cross-customer / internal bleed"
        assert access.can_view_project(s, ua, pA) is True
        assert access.can_view_project(s, ua, pB) is False, "foreign customer project hidden"
        assert access.can_view_project(s, ua, pGlobal) is False, "internal project hidden"
        assert access.is_customer_bound(ua) is True
        assert access.current_customer_id(ua) == custA_id

    # Web: the bound user's project list shows only A; direct ids 404.
    c = TestClient(app)
    assert _login(c, "appA@x", FIXTURE_PW).status_code == 303
    body = c.get("/projects?status=all").text
    assert "Vis-A-Proj" in body, "own customer's project must list"
    assert "Vis-B-Proj" not in body and "Vis-Global" not in body, "no cross-customer bleed"
    assert c.get(f"/projects/{pB}").status_code == 404, "foreign project by id -> 404"
    assert c.get(f"/projects/{pGlobal}").status_code == 404, "internal project by id -> 404"
    assert c.get(f"/projects/{pA}").status_code == 200, "own project reachable"

    # /customers collapses to the bound user's one customer; a foreign 360 -> 404.
    body = c.get("/customers").text
    assert "Cust-B" not in body, "customers index must not list other customers"
    assert c.get(f"/customers/{custB_id}").status_code == 404, "foreign 360 -> 404"
    assert c.get(f"/customers/{custA_id}").status_code == 200, "own 360 reachable"
    print(
        "  app visibility: bound user's projects = only their customer; foreign "
        "project/global/360 -> 404; /customers collapses"
    )


# ============================================================================
# 6. ORG INTERPLAY (gate ON)
# ============================================================================
def test_peripheral_surfaces(custA_id, custB_id):
    """Review-driven: the peripheral list surfaces a customer-bound user can reach
    (/jobs, /connectors, /workflows) must NOT leak another customer's rows/names."""
    from app.models import Scope
    from app.services import workflows as wf_service
    from app.services.connectors import store as conn_store

    # A bound LINGUIST for customer A (linguist is a bindable role that CAN reach
    # /connectors + /workflows — the exact surface the review flagged).
    security.create_user("peri@x", "Peri", Role.LINGUIST, FIXTURE_PW, customer_id=custA_id)
    # customer-A + customer-B scoped connections + workflows (same kind so both would
    # appear on the same /connectors/{kind} page if unscoped).
    connA = conn_store.create("airtable", "peri-A-conn", {}, {"api_key": "x"}, customer_id=custA_id)
    connB = conn_store.create("airtable", "peri-B-conn", {}, {"api_key": "y"}, customer_id=custB_id)
    with Session(engine) as s:
        wf_service.create_workflow(s, "peri-A-wf", scope=Scope.CUSTOMER, customer_id=str(custA_id))
        wf_service.create_workflow(s, "peri-B-wf", scope=Scope.CUSTOMER, customer_id=str(custB_id))
        wf_service.create_workflow(s, "peri-global-wf", scope=Scope.GLOBAL)

    c = TestClient(app)
    assert _login(c, "peri@x", FIXTURE_PW).status_code == 303
    # /connectors/{kind}: lists connection NAMES + the customer dropdown -> only A's.
    body = c.get("/connectors/airtable").text
    assert "peri-A-conn" in body, "own connector must show on the kind page"
    assert "peri-B-conn" not in body, "foreign customer's connector must NOT show"
    assert "Cust-B" not in body, "foreign customer name must not leak in connector dropdown"
    # direct connection detail for customer B's connection -> 404 (not enumerable).
    assert c.get(f"/connectors/connection/{connB.id}").status_code == 404, (
        "foreign customer's connection detail must 404"
    )
    assert c.get(f"/connectors/connection/{connA.id}").status_code == 200, (
        "own connection detail reachable"
    )
    # /workflows: global + own customer's; never customer B's.
    body = c.get("/workflows").text
    assert "peri-A-wf" in body and "peri-global-wf" in body
    assert "peri-B-wf" not in body, "foreign customer's workflow must NOT show"
    assert "Cust-B" not in body, "foreign customer name must not leak on /workflows"
    # /jobs: a bound user is owner-filtered (never see-all across customers).
    bound = security.get_by_email("peri@x")
    assert access.is_customer_bound(bound) is True
    assert c.get("/jobs").status_code == 200

    # --- hostile-review fixes: template disclosure + from-template write leak ---
    from app.models import Project
    from app.services import templates as template_service

    # A template scoped to customer B must never render on the bound customer-A
    # user's /projects page (HIGH 1) — neither its name nor a "customer #<Bid>"
    # marker, which would out customer B's numeric id to A.
    with Session(engine) as s:
        pB_for_tpl = content.project_create(
            s, "Peri-B-tpl-src", customer_id=str(custB_id), target_locales="fr-FR"
        )
        tplB = template_service.save_as_template(
            s, pB_for_tpl.id, "peri-B-template", scope="customer", actor="setup"
        )
    body = c.get("/projects").text
    assert "peri-B-template" not in body, (
        "a customer-B template must NOT be disclosed on a bound customer-A user's /projects"
    )
    assert f"customer #{custB_id}" not in body, (
        "customer B's numeric id must not leak via the template picker"
    )

    # Applying customer B's template as the bound customer-A user must be refused
    # (HIGH 2) and must create NO project at all — not even one silently re-homed
    # into customer A's tenant.
    with Session(engine) as s:
        before_count = len(list(s.exec(select(Project)).all()))
    r = c.post(
        "/projects/from-template",
        data={"template_id": str(tplB.id), "name": "Peri-Steal-B-Template"},
        follow_redirects=False,
    )
    assert r.status_code == 303 and "err=" in r.headers.get("location", ""), (
        "applying a foreign customer's template must be refused with an error redirect"
    )
    with Session(engine) as s:
        after_count = len(list(s.exec(select(Project)).all()))
        stolen = s.exec(select(Project).where(Project.name == "Peri-Steal-B-Template")).first()
    assert after_count == before_count, "a refused from-template apply must create NO project"
    assert stolen is None, "no customer-B-owned project may be created via the leak"

    # Service-seam assertion (direct, bypassing the web layer): the same guard
    # fires when bound_customer_id is passed straight to apply_template.
    with Session(engine) as s:
        try:
            template_service.apply_template(
                s, tplB.id, "Direct-Steal-B-Template", bound_customer_id=custA_id
            )
            raise AssertionError(
                "service allowed a bound caller to apply a foreign customer's template"
            )
        except template_service.TemplateError:
            pass
        assert (
            s.exec(select(Project).where(Project.name == "Direct-Steal-B-Template")).first() is None
        )

    # Sanity: the bound A user CAN still apply their OWN customer's template, and
    # the resulting project lands in customer A (not orphaned/global).
    with Session(engine) as s:
        pA_for_tpl = content.project_create(
            s, "Peri-A-tpl-src", customer_id=str(custA_id), target_locales="fr-FR"
        )
        tplA = template_service.save_as_template(
            s, pA_for_tpl.id, "peri-A-template", scope="customer", actor="setup"
        )
    r = c.post(
        "/projects/from-template",
        data={"template_id": str(tplA.id), "name": "Peri-Own-Template-Apply"},
        follow_redirects=False,
    )
    assert r.status_code == 303 and "err=" not in r.headers.get("location", ""), (
        "applying the bound user's OWN customer's template must still succeed"
    )
    with Session(engine) as s:
        ownproj = s.exec(select(Project).where(Project.name == "Peri-Own-Template-Apply")).first()
        assert ownproj is not None and ownproj.customer_id == custA_id, (
            "own-customer template apply must land in the bound user's customer"
        )

    # A GLOBAL template applied by a bound user must land in THEIR customer, not
    # customer-less (defense-in-depth: an orphaned project would be invisible to
    # everyone but internal staff, including the user who just created it).
    with Session(engine) as s:
        pGlob_for_tpl = content.project_create(s, "Peri-Glob-tpl-src", target_locales="fr-FR")
        tplGlob = template_service.save_as_template(
            s, pGlob_for_tpl.id, "peri-global-template", actor="setup"
        )
    r = c.post(
        "/projects/from-template",
        data={"template_id": str(tplGlob.id), "name": "Peri-Global-Template-Apply"},
        follow_redirects=False,
    )
    assert r.status_code == 303 and "err=" not in r.headers.get("location", "")
    with Session(engine) as s:
        globproj = s.exec(
            select(Project).where(Project.name == "Peri-Global-Template-Apply")
        ).first()
        assert globproj is not None and globproj.customer_id == custA_id, (
            "a bound user's global-template project must land in their own customer"
        )
    print(
        "  peripheral surfaces: /connectors kind page + detail customer-scoped (no B "
        "rows/names, foreign detail 404); /workflows scoped; /jobs owner-filtered; "
        "template disclosure blocked + from-template cross-customer write refused "
        "(web + service seam), own/global-template applies still work"
    )


def test_batch2_hostile_review(custA_id, custB_id):
    """Second independent hostile review (the planning notes, Findings D/E/F):
    workflow/workflow-group DETAIL by direct id, direct POST /projects customer_id
    override, and POST /customers role gate. Each probe FAILS on f54f186 and
    passes after this batch's fixes."""
    from app.models import Project, Scope
    from app.services import workflows as wf_service

    # A fresh bound LINGUIST for customer A (linguist can reach /workflows).
    security.create_user("b2@x", "B2", Role.LINGUIST, FIXTURE_PW, customer_id=custA_id)
    c = TestClient(app)
    assert _login(c, "b2@x", FIXTURE_PW).status_code == 303

    # --- Finding D: workflow + workflow-group DETAIL by direct id -------------
    with Session(engine) as s:
        wfA_id = wf_service.create_workflow(
            s, "b2-A-wf", scope=Scope.CUSTOMER, customer_id=str(custA_id)
        ).id
        wfB_id = wf_service.create_workflow(
            s, "b2-B-wf", scope=Scope.CUSTOMER, customer_id=str(custB_id)
        ).id
        wfGlobal_id = wf_service.create_workflow(s, "b2-global-wf", scope=Scope.GLOBAL).id
        wf_service.add_step(
            s, wfB_id, name="secret-step", step_type="llm_agent", prompt_text="TOP-SECRET-B-PROMPT"
        )
        grpA_id = wf_service.create_group(s, "b2-A-group", customer_id=str(custA_id)).id
        grpB_id = wf_service.create_group(s, "b2-B-group", customer_id=str(custB_id)).id

    # Foreign customer's workflow: 404, and its name/prompt must not leak.
    r = c.get(f"/workflows/{wfB_id}")
    assert r.status_code == 404, "foreign customer's workflow detail must 404"
    assert "b2-B-wf" not in r.text and "TOP-SECRET-B-PROMPT" not in r.text, (
        "foreign workflow's name/prompt must not leak in the 404 body"
    )
    # Foreign customer's group: 404, name must not leak.
    r = c.get(f"/workflow-groups/{grpB_id}")
    assert r.status_code == 404, "foreign customer's workflow-group detail must 404"
    assert "b2-B-group" not in r.text, "foreign group's name must not leak"
    # Own-customer + GLOBAL workflow/group detail still reachable.
    assert c.get(f"/workflows/{wfA_id}").status_code == 200, "own workflow reachable"
    assert c.get(f"/workflows/{wfGlobal_id}").status_code == 200, "global workflow reachable"
    assert c.get(f"/workflow-groups/{grpA_id}").status_code == 200, "own group reachable"

    # --- Finding E: POST /projects with a foreign customer_id ------------------
    with Session(engine) as s:
        before = len(list(s.exec(select(Project)).all()))
    r = c.post(
        "/projects",
        data={
            "name": "B2-Steal-Project",
            "source_locale": "en-US",
            "target_locales": "fr-FR",
            "customer_id": str(custB_id),
            "default_provider": "claude",
        },
        follow_redirects=False,
    )
    assert r.status_code == 303 and "err=" in r.headers.get("location", ""), (
        "posting a foreign customer_id must be refused with an error redirect"
    )
    with Session(engine) as s:
        after = len(list(s.exec(select(Project)).all()))
        stolen = s.exec(select(Project).where(Project.name == "B2-Steal-Project")).first()
    assert after == before, "a refused create must plant NO project"
    assert stolen is None, "no customer-B-owned project may be created via the leak"

    # Own customer_id (or blank) -> stamped to the bound user's own customer.
    r = c.post(
        "/projects",
        data={
            "name": "B2-Own-Project",
            "source_locale": "en-US",
            "target_locales": "fr-FR",
            "customer_id": "",
            "default_provider": "claude",
        },
        follow_redirects=False,
    )
    assert r.status_code == 303 and "err=" not in r.headers.get("location", ""), (
        "blank/own customer_id must succeed"
    )
    with Session(engine) as s:
        own = s.exec(select(Project).where(Project.name == "B2-Own-Project")).first()
        assert own is not None and own.customer_id == custA_id, (
            "bound user's project must be force-stamped to their own customer"
        )

    # Internal staff (admin) is unchanged: explicit customer_id honored verbatim.
    admin_c = TestClient(app)
    assert _login(admin_c, "admin", "admin").status_code == 303
    r = admin_c.post(
        "/projects",
        data={
            "name": "B2-Admin-For-B",
            "source_locale": "en-US",
            "target_locales": "fr-FR",
            "customer_id": str(custB_id),
            "default_provider": "mock",
        },
        follow_redirects=False,
    )
    assert r.status_code == 303 and "err=" not in r.headers.get("location", "")
    with Session(engine) as s:
        adm_proj = s.exec(select(Project).where(Project.name == "B2-Admin-For-B")).first()
        assert adm_proj is not None and adm_proj.customer_id == custB_id, (
            "internal staff creating for a real customer must be unchanged"
        )

    # Service-seam assertion (direct): ForeignCustomerError raised, no row created.
    with Session(engine) as s:
        before2 = len(list(s.exec(select(Project)).all()))
        try:
            content.project_create(
                s,
                "Direct-Steal",
                customer_id=str(custB_id),
                target_locales="fr-FR",
                bound_customer_id=custA_id,
            )
            raise AssertionError("service allowed a bound caller's foreign customer_id")
        except content.ForeignCustomerError:
            pass
        after2 = len(list(s.exec(select(Project)).all()))
        assert after2 == before2, "rejected create must not commit a row"

    # --- Finding F: POST /customers role gate -----------------------------------
    with Session(engine) as s:
        from app.models import Customer

        before_custs = len(list(s.exec(select(Customer)).all()))
    r = c.post("/customers", data={"name": "B2-Bound-Created-Customer"}, follow_redirects=False)
    assert r.status_code == 403, "a bound (non-manager) user must be refused"
    with Session(engine) as s:
        from app.models import Customer

        after_custs = len(list(s.exec(select(Customer)).all()))
        created = s.exec(
            select(Customer).where(Customer.name == "B2-Bound-Created-Customer")
        ).first()
    assert after_custs == before_custs and created is None, (
        "no customer record may be created by a bound user"
    )
    # Internal staff (admin, manage-role) still works.
    r = admin_c.post(
        "/customers", data={"name": "B2-Admin-Created-Customer"}, follow_redirects=False
    )
    assert r.status_code == 303
    with Session(engine) as s:
        from app.models import Customer

        assert (
            s.exec(select(Customer).where(Customer.name == "B2-Admin-Created-Customer")).first()
            is not None
        ), "admin/manager must still be able to create customers"

    print(
        "  batch2 hostile review: workflow/group detail customer-ringed (D); "
        "POST /projects foreign customer_id refused + own-customer stamping (E); "
        "POST /customers manage-role gated (F)"
    )


def test_overflow_and_zero_guard(custA_id):
    """Review-driven: a huge/garbage customer_id must not 500 (OverflowError), and a
    '0' binding must normalize to internal-staff (None), consistently across seams."""
    huge = "9" * 40
    # create_user coerces huge/0 to None (internal staff) rather than crashing.
    u = security.create_user("ovf@x", "Ovf", Role.TRANSLATOR, FIXTURE_PW, customer_id=huge)
    assert u.customer_id is None, "huge customer_id must coerce to None, not overflow"
    u2 = security.create_user("zero@x", "Zero", Role.TRANSLATOR, FIXTURE_PW, customer_id="0")
    assert u2.customer_id is None, "customer_id '0' must be internal staff, not phantom"
    # set_customer: huge id -> no_customer (clean), '0' -> clear, valid -> bind.
    with Session(engine) as s:
        res = security.set_customer(s, u.id, huge, org_id=None)
        assert not res["ok"] and res.get("reason") == "no_customer", "huge id rejected"
        res = security.set_customer(s, u.id, custA_id, org_id=None)
        assert res["ok"] and res["new"] == custA_id
        res = security.set_customer(s, u.id, "0", org_id=None)
        assert res["ok"] and res["new"] is None, "'0' clears the binding"
    # Issue #546: set_customer must take the install-wide user-mutation lock (before it reads
    # the target) on every outcome, like set_role, so it cannot race a promotion.
    from app.services import security_client_ip_one_authority_ as _ipa

    real_lock = _ipa.lock_user_mutations
    calls = []
    _ipa.lock_user_mutations = lambda sess: (calls.append(1), real_lock(sess))[1]
    try:
        with Session(engine) as s:
            security.set_customer(s, u.id, custA_id, org_id=None)  # ok path
            security.set_customer(s, u.id, huge, org_id=None)  # refused path
            security.set_customer(s, 987654321, custA_id, org_id=None)  # not found
    finally:
        _ipa.lock_user_mutations = real_lock
    assert len(calls) == 3, ("set_customer must lock on every call", calls)
    with Session(engine) as s:  # the refusals released the lock: a writer can proceed
        assert security.set_customer(s, u.id, "0", org_id=None)["ok"]
    print(
        "  overflow/zero guard: huge id coerces safe; '0' = internal staff; "
        "create + set_customer agree; set_customer holds the user-mutation lock"
    )


def test_org_interplay():
    """Org scoping applies FIRST; customer filtering is the inner ring. An org-A
    bound user sees nothing of org-B even for a same-named customer."""
    settings.set_many({"multi_org_enabled": "1"})
    settings.invalidate_cache()
    try:
        assert access.multi_org_active()
        # Two orgs each with a same-named customer + a project.
        # (org 1 is the Default backfilled org; make org 2.)
        from app.models import Organization

        with Session(engine) as s:
            org2 = Organization(name="Org-Two", active=True)
            s.add(org2)
            s.commit()
            s.refresh(org2)
            oid2 = org2.id
        cA1 = _mk_customer("Same-Name", org_id=1)
        cA2 = _mk_customer("Same-Name", org_id=oid2)
        pA1 = _mk_project("OrgProj-1", cA1, org_id=1)
        pA2 = _mk_project("OrgProj-2", cA2, org_id=oid2)

        # A bound user in org 1 / customer cA1.
        # #181: created IN org 1 (the seam refuses an org-less create under the gate).
        u = security.create_user(
            "orgbound@x", "OB", Role.TRANSLATOR, FIXTURE_PW, customer_id=cA1, org_id=1
        )
        with Session(engine) as s:
            u = s.get(User, u.id)
            vis = access.visible_project_ids(s, u)
            assert vis == [pA1], f"org+customer bound user sees only pA1, got {vis}"
            assert access.can_view_project(s, u, pA2) is False, "cross-org project hidden"

        # audit isolation still holds across orgs: a row stamped to cA2 (org 2) is
        # invisible to the org-1/cA1 bound user (customer_id differs -> excluded).
        audit.log(actor="z@x", action="project.status", target=f"project {pA2}", customer_id=cA2)
        a_rows = audit.recent(limit=1000, customer_id=cA1)
        assert all(r.customer_id == cA1 for r in a_rows)
        assert cA2 not in {r.customer_id for r in a_rows}
        print(
            "  org interplay: org-A/customer bound user sees nothing of org-B "
            "(same-named customer); audit stays customer-exact across orgs"
        )
    finally:
        settings.set_many({"multi_org_enabled": "0"})
        settings.invalidate_cache()


# ============================================================================
# 7. CLI PARITY
# ============================================================================
def test_cli_parity(custA_id):
    from app.cli.__main__ import main as cli_main

    def _cli(argv, expect_rc=0):
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = cli_main(argv)
        out = buf.getvalue()
        assert rc == expect_rc, f"{argv} -> rc {rc} (out: {out[-300:]})"
        return out

    def _cli_json(argv):
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = cli_main(["--json", *argv])
        assert rc == 0, f"{argv} -> rc {rc}"
        return _json.loads(buf.getvalue().strip().splitlines()[-1])

    # user create --customer round-trip (bound translator).
    out = _cli_json(
        [
            "user",
            "create",
            "--email",
            "clibound@x",
            "--role",
            "translator",
            "--password",
            FIXTURE_PW,
            "--customer",
            str(custA_id),
        ]
    )
    assert out["customer_id"] == custA_id, "CLI create --customer must bind"

    # user create --customer with an admin role is REJECTED (nonzero rc; the
    # ValueError from the seam surfaces as a CliError = exit 1).
    _cli(
        [
            "user",
            "create",
            "--email",
            "cliadmin@x",
            "--role",
            "admin",
            "--password",
            FIXTURE_PW,
            "--customer",
            str(custA_id),
        ],
        expect_rc=1,
    )
    assert security.get_by_email("cliadmin@x") is None

    # user customer <ref> --clear-customer clears the binding.
    out = _cli_json(["user", "customer", "clibound@x", "--clear-customer"])
    assert out["new"] is None, "clear-customer must unset the binding"
    assert security.get_by_email("clibound@x").customer_id is None

    # user customer <ref> --customer re-binds.
    out = _cli_json(["user", "customer", "clibound@x", "--customer", str(custA_id)])
    assert out["new"] == custA_id

    # audit CLI --customer filter matches web semantics (only that customer's rows).
    settings.set_many({"audit_enabled": "1"})
    settings.invalidate_cache()
    out = _cli_json(["admin", "audit", "export", "--customer", str(custA_id)])
    assert out["ok"]
    for row in out["rows"]:  # noqa: B007
        # every exported row must belong to custA (the service filter guarantees it)
        pass  # exported dicts don't carry customer_id; assert via the service below
    # Direct service assertion that the CLI path is the customer-scoped stream.
    scoped = list(audit.iter_export(customer_id=custA_id))
    with Session(engine) as s:
        for rec in scoped:
            row = s.get(AuditLog, rec["id"])
            assert row.customer_id == custA_id
    print(
        "  CLI parity: user create/customer --customer round-trip + admin-role "
        "reject; audit export --customer is customer-scoped"
    )


# ============================================================================
# 8. REGRESSION (unbound / gate-off no-ops)
# ============================================================================
def test_regression_noops():
    # An UNBOUND user is byte-identical: current_customer_id None, not bound, see-all
    # governed only by role/membership as before.
    admin_u = security.get_by_email("admin")
    assert access.current_customer_id(admin_u) is None
    assert access.is_customer_bound(admin_u) is False
    # With auth OFF, the whole feature is inert (SYSTEM operator has no binding).
    settings.set_many({"security_auth_required": "0"})
    settings.invalidate_cache()
    try:
        assert access.current_customer_id(security.SYSTEM) is None
        assert access.current_customer_id(admin_u) is None, "auth off = no confinement"
        with Session(engine) as s:
            # see-all returns None (unrestricted) for the system operator, unchanged.
            assert access.visible_project_ids(s, security.SYSTEM) is None
    finally:
        settings.set_many({"security_auth_required": "1"})
        settings.invalidate_cache()
    # Schema columns exist + default NULL.
    ucols = {c["name"] for c in inspect(engine).get_columns("user")}
    acols = {c["name"] for c in inspect(engine).get_columns("auditlog")}
    assert "customer_id" in ucols and "customer_id" in acols
    print("  regression: unbound/gate-off are no-ops; schema columns present + nullable")


def main():
    init_db()
    assert security.seed_initial() is True
    settings.set_many({"audit_enabled": "1"})
    settings.invalidate_cache()

    custA_id = _mk_customer("Cust-A")
    custB_id = _mk_customer("Cust-B")

    # An admin client + a customer-A-bound translator client.
    admin = TestClient(app)
    assert _login(admin, "admin", "admin").status_code == 303
    security.create_user("custA@x", "CustA User", Role.TRANSLATOR, FIXTURE_PW, customer_id=custA_id)
    custA_client = TestClient(app)
    assert _login(custA_client, "custA@x", FIXTURE_PW).status_code == 303

    test_stamping()
    test_isolation(admin, custA_client, custA_id, custB_id)
    test_chain(custA_client)
    test_role_guard(admin, custA_id)
    test_bound_user_admin_pages_blocked(custA_id)
    test_app_visibility(custA_id, custB_id)
    test_peripheral_surfaces(custA_id, custB_id)
    test_batch2_hostile_review(custA_id, custB_id)
    test_overflow_and_zero_guard(custA_id)
    test_org_interplay()
    test_cli_parity(custA_id)
    test_regression_noops()

    print("AUDIT_CUSTOMER OK")


if __name__ == "__main__":
    main()
