"""Project templates verification (P7, item 9), offline + hermetic.

Covers:
  1. save_as_template snapshots languages/provider/workflow/segmentation; unique
     name + no-customer "customer" scope rejected.
  2. apply_template recreates the project with the exact settings + all target
     locales; global template + explicit-customer stays clean.
  3. Isolation: a customer-scoped template invisible in another customer's
     list_templates and un-applicable under a different customer.
  4. Graceful degradation: applying a template whose workflow / segmentation
     profile was deleted (or belongs to another customer) SKIPS with a warning —
     the project is still created, never a crash.
  5. Web: save-template + from-template + delete routes round-trip (warnings
     surfaced via redirect).
  6. CLI: template create/list/show/apply/delete round-trip.
  7. Day-1a review #2: multi-org gate ON, an org-A actor gets not-found applying
     org-B's template (never enumerable, never silently applied); in-org apply
     still works.
  8. Day-1a review #3: a template snapshotted from a legacy/connector project
     (non-catalog locale codes) applies with a skip+warn per dropped code rather
     than a hard TemplateError; an all-invalid-targets snapshot fails cleanly.
  9. Template prefill (new-project dialog "start from a template"): the read-only
     prefill_draft seam + GET /projects/templates/{id}/prefill.json -- draft
     shape, dangling/archived-customer + dangling/customer-scoped-segmentation
     degrade to warnings (never a lost value silently, never a crash), a captured
     default_workflow_id ALWAYS warns (the base create form has no field for it),
     R2-3 + multi-org scoping mirrors apply_template's not-found contract, and the
     dialog's picker markup renders only when a template is actually visible.

Prints ASCII only. Final line: TEMPLATES OK
"""

import io
import json
import os
import sys
from contextlib import redirect_stderr, redirect_stdout

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(_ROOT)
sys.path.insert(0, _ROOT)
from tests._bootstrap import init_env

init_env("loctms_test_")

from fastapi.testclient import TestClient
from sqlmodel import Session, select

from app.cli.__main__ import main as cli_main
from app.db import engine, init_db
from app.main import app
from app.models import (
    AuditLog,
    Customer,
    Organization,
    Project,
    ProjectTemplate,
    Role,
    Scope,
    TargetLocale,
    User,
    Workflow,
)
from app.services import security, settings
from app.services import segmentation_profiles as segprof
from app.services import templates as tplsvc
from tests._csrf import csrf_for

init_db()
client = TestClient(app, follow_redirects=False)


def run(argv):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        try:
            code = cli_main(argv)
        except SystemExit as exc:
            code = exc.code if isinstance(exc.code, int) else 2
    return code, out.getvalue(), err.getvalue()


def run_json(argv):
    code, out, err = run(["--json"] + argv)  # noqa: RUF005
    payload = json.loads(out.strip().splitlines()[-1]) if out.strip() else {}
    return code, payload, err


def _mk(name, customer_id=None, locales=("fr-FR", "de-DE"), provider="claude"):
    with Session(engine) as s:
        p = Project(
            name=name, source_locale="en-US", customer_id=customer_id, default_provider=provider
        )
        s.add(p)
        s.commit()
        s.refresh(p)
        for lc in locales:
            s.add(TargetLocale(project_id=p.id, locale=lc))
        s.commit()
        return p.id


def _locales_of(pid):
    with Session(engine) as s:
        return sorted(
            tl.locale
            for tl in s.exec(select(TargetLocale).where(TargetLocale.project_id == pid)).all()
        )


# --- fixtures shared across tests ---------------------------------------------
with Session(engine) as _s:
    _ca = Customer(name="Tpl Cust A")
    _cb = Customer(name="Tpl Cust B")
    _s.add(_ca)
    _s.add(_cb)
    _s.commit()
    _s.refresh(_ca)
    _s.refresh(_cb)
    CA_ID, CB_ID = _ca.id, _cb.id
    _wa = Workflow(name="A private wf", scope=Scope.CUSTOMER, customer_id=CA_ID)
    _wg = Workflow(name="Global wf", scope=Scope.GLOBAL)
    _s.add(_wa)
    _s.add(_wg)
    _s.commit()
    _s.refresh(_wa)
    _s.refresh(_wg)
    WF_A_ID, WF_G_ID = _wa.id, _wg.id
PROF_ID = segprof.create_profile("Tpl seg prof").id


def test_save_snapshot():
    pid = _mk("Snap src", customer_id=CA_ID)
    with Session(engine) as s:
        proj = s.get(Project, pid)
        proj.default_workflow_id = WF_A_ID
        s.add(proj)
        s.commit()
        segprof.assign_to_project(s, pid, str(PROF_ID))
        tpl = tplsvc.save_as_template(
            s, pid, "Snap tpl", scope="customer", description="d", actor="test"
        )
    assert tpl.customer_id == CA_ID
    assert sorted(tpl.target_locales) == ["de-DE", "fr-FR"]
    assert tpl.default_workflow_id == WF_A_ID
    assert tpl.segmentation_profile_id == PROF_ID
    assert tpl.source_locale == "en-US" and tpl.default_provider == "claude"
    # duplicate name rejected
    with Session(engine) as s:
        try:
            tplsvc.save_as_template(s, pid, "Snap tpl")
            raise AssertionError("duplicate template name accepted")
        except tplsvc.TemplateError:
            pass
    # customer scope on a customer-less project rejected
    pid2 = _mk("No cust")
    with Session(engine) as s:
        try:
            tplsvc.save_as_template(s, pid2, "Nope", scope="customer")
            raise AssertionError("customer scope without customer accepted")
        except tplsvc.TemplateError:
            pass
    print("  save snapshot ok")


def test_apply_exact():
    tpl = tplsvc.get_by_name("Snap tpl")
    with Session(engine) as s:
        proj, warnings = tplsvc.apply_template(s, tpl.id, "Applied proj", created_by="test")
    assert not warnings, warnings
    assert proj.customer_id == CA_ID
    assert proj.default_provider == "claude"
    assert proj.default_workflow_id == WF_A_ID
    assert proj.segmentation_profile_id == PROF_ID
    assert _locales_of(proj.id) == ["de-DE", "fr-FR"]
    print("  apply recreates exact settings ok")


def test_isolation():
    tpl = tplsvc.get_by_name("Snap tpl")  # scoped to CA
    vis_b = tplsvc.list_templates(customer_id=CB_ID)
    assert all(t.id != tpl.id for t in vis_b), "A's template visible to B"
    with Session(engine) as s:
        try:
            tplsvc.apply_template(s, tpl.id, "Steal", customer_id=str(CB_ID))
            raise AssertionError("customer template applied under another customer")
        except tplsvc.TemplateError:
            pass
    # global template visible to both, applies under an explicit customer
    pid = _mk("Glob src")
    with Session(engine) as s:
        gtpl = tplsvc.save_as_template(s, pid, "Glob tpl")
        assert any(t.id == gtpl.id for t in tplsvc.list_templates(customer_id=CB_ID))
        proj, w = tplsvc.apply_template(s, gtpl.id, "Glob applied", customer_id=str(CB_ID))
        assert proj.customer_id == CB_ID and not w
    print("  scope isolation ok")


def test_dangling_refs_degrade():
    # a template referencing a workflow of ANOTHER customer (possible when the
    # template is global but snapshotted a customer project) -> skip + warn
    pid = _mk("Dangle src", customer_id=CA_ID)
    with Session(engine) as s:
        proj = s.get(Project, pid)
        proj.default_workflow_id = WF_A_ID
        s.add(proj)
        s.commit()
        segprof.assign_to_project(s, pid, str(PROF_ID))
        gtpl = tplsvc.save_as_template(s, pid, "Dangle tpl")  # GLOBAL scope
        # apply with NO customer: workflow (CA-private) must be skipped
        p1, w1 = tplsvc.apply_template(s, gtpl.id, "Dangle applied 1")
        assert p1.default_workflow_id is None
        assert any("another customer" in w for w in w1), w1
    # delete the segmentation profile + workflow -> both skipped with warnings
    prof2 = segprof.create_profile("Doomed prof")
    with Session(engine) as s:
        wf_tmp = Workflow(name="Doomed wf", scope=Scope.GLOBAL)
        s.add(wf_tmp)
        s.commit()
        s.refresh(wf_tmp)
        pid3 = _mk("Doom src")
        proj = s.get(Project, pid3)
        proj.default_workflow_id = wf_tmp.id
        s.add(proj)
        s.commit()
        segprof.assign_to_project(s, pid3, str(prof2.id))
        dtpl_id = tplsvc.save_as_template(s, pid3, "Doom tpl").id
        # clear the project's ref so the profile is deletable, then delete refs
        segprof.assign_to_project(s, pid3, "")
        s.delete(s.get(Workflow, wf_tmp.id))
        s.commit()
    assert segprof.delete_profile(prof2.id)
    with Session(engine) as s:
        p2, w2 = tplsvc.apply_template(s, dtpl_id, "Doom applied")
        assert p2.id and p2.default_workflow_id is None
        assert p2.segmentation_profile_id is None
        assert len(w2) == 2, w2  # one warning each
    print("  dangling refs degrade gracefully ok")


def test_web_routes():
    pid = _mk("Web tpl src", customer_id=CA_ID)
    r = client.post(
        f"/projects/{pid}/save-template",
        data={"name": "Web tpl", "scope": "customer", "description": "via web"},
    )
    assert r.status_code == 303 and "saved=1" in r.headers["location"], r.headers
    tpl = tplsvc.get_by_name("Web tpl")
    assert tpl is not None and tpl.customer_id == CA_ID
    # duplicate -> err redirect
    r = client.post(f"/projects/{pid}/save-template", data={"name": "Web tpl"})
    assert "err=" in r.headers["location"]
    # from-template
    r = client.post(
        "/projects/from-template", data={"template_id": str(tpl.id), "name": "Web applied"}
    )
    assert r.status_code == 303 and "/projects/" in r.headers["location"]
    with Session(engine) as s:
        newp = s.exec(select(Project).where(Project.name == "Web applied")).first()
        assert newp is not None and newp.customer_id == CA_ID
    # projects page renders the card; customer 360 lists it
    page = client.get("/projects")
    assert "Create from template" in page.text and "Web tpl" in page.text
    page = client.get(f"/customers/{CA_ID}")
    assert "Web tpl" in page.text
    # delete
    r = client.post(f"/templates/{tpl.id}/delete")
    assert r.status_code == 303
    assert tplsvc.get_by_name("Web tpl") is None
    print("  web routes ok")


def test_cli():
    pid = _mk("CLI tpl src")
    code, payload, _ = run_json(
        [
            "template",
            "create",
            "--from-project",
            str(pid),
            "--name",
            "CLI tpl",
            "--description",
            "via cli",
        ]
    )
    assert code == 0 and payload["template"]["scope"] == "global", payload
    tid = payload["template"]["id"]
    code, payload, _ = run_json(["template", "list"])
    assert code == 0 and any(t["id"] == tid for t in payload["templates"])
    code, out, _ = run(["template", "show", str(tid)])
    assert code == 0 and "CLI tpl" in out
    # #22: this suite's org already has selectable customers (CA_ID/CB_ID, created
    # module-level) -> applying a GLOBAL template requires --customer-id.
    code, payload, _ = run_json(
        ["template", "apply", str(tid), "--name", "CLI applied", "--customer-id", str(CA_ID)]
    )
    assert code == 0 and payload["project_id"], payload
    assert _locales_of(payload["project_id"]) == ["de-DE", "fr-FR"]
    with Session(engine) as s:
        applied_proj = s.get(Project, payload["project_id"])
        assert applied_proj.customer_id == CA_ID, applied_proj.customer_id
    code, out, _ = run(["template", "delete", str(tid), "--yes"])
    assert code == 0
    code, out, _ = run(["template", "show", str(tid)])
    assert code != 0
    print("  cli round-trip ok")


def test_cli_apply_customer_policy():
    """#22 (CLI sweep, HIGH): `template apply` must mirror the web
    POST /projects/from-template route's required-customer policy exactly --
    `app/cli/cmd_template.py::_apply` used to have no --customer-id flag and
    never passed require_customer=True, so it silently created customer-less
    projects even though this org has selectable customers (a direct parity gap
    on the 2026-07-14 'project creation requires a customer' policy).

      1. a GLOBAL template applied with NO --customer-id, in an org that HAS a
         selectable customer, is REFUSED (clean CliError, no project row).
      2. the same apply WITH --customer-id succeeds and the project lands in
         that customer.
      3. a CUSTOMER-scoped template applied with a CONFLICTING --customer-id is
         refused (apply_template's own scope-conflict rule, unchanged).
      4. an org with ZERO selectable customers still permits a customer-less
         apply (org_has_selectable_customer's self-healing banner semantics --
         a pristine/hermetic org isn't blocked from ever creating a project)."""
    pid = _mk("CLI policy src")  # customer-less source project -> GLOBAL template
    code, payload, _ = run_json(
        ["template", "create", "--from-project", str(pid), "--name", "CLI policy tpl"]
    )
    assert code == 0, payload
    tid = payload["template"]["id"]

    # (1) no --customer-id, org HAS selectable customers -> refused, no project.
    code, out, err = run(["template", "apply", str(tid), "--name", "CLI policy no-cust"])
    assert code != 0, (code, out, err)
    assert "customer" in (out + err).lower(), (out, err)
    with Session(engine) as s:
        assert (
            s.exec(select(Project).where(Project.name == "CLI policy no-cust")).first() is None
        ), "#22 REGRESSION: template apply created a customer-less project"

    # (2) --customer-id succeeds, project attached to that customer.
    code, payload, _ = run_json(
        [
            "template",
            "apply",
            str(tid),
            "--name",
            "CLI policy with-cust",
            "--customer-id",
            str(CB_ID),
        ]
    )
    assert code == 0, payload
    with Session(engine) as s:
        proj = s.get(Project, payload["project_id"])
        assert proj.customer_id == CB_ID, proj.customer_id

    # (3) a CUSTOMER-scoped template + a CONFLICTING --customer-id is refused.
    pid_a = _mk("CLI policy cust-scoped src", customer_id=CA_ID)
    code, payload, _ = run_json(
        [
            "template",
            "create",
            "--from-project",
            str(pid_a),
            "--name",
            "CLI policy cust tpl",
            "--scope",
            "customer",
        ]
    )
    assert code == 0, payload
    tid_a = payload["template"]["id"]
    code, out, err = run(
        [
            "template",
            "apply",
            str(tid_a),
            "--name",
            "CLI policy conflict",
            "--customer-id",
            str(CB_ID),
        ]
    )
    assert code != 0, (code, out, err)
    with Session(engine) as s:
        assert s.exec(select(Project).where(Project.name == "CLI policy conflict")).first() is None

    # (4) an org with ZERO selectable customers is NOT blocked (self-healing
    # banner semantics -- mirrors org_has_selectable_customer's rationale). A
    # fresh org (no customers) needs its OWN org-scoped template: a template
    # stamped org_id=None (created while the gate was off, like `tid` above) is
    # not visible to a specific --org scope once the gate is on (mirrors
    # test_apply_cross_org's org isolation), so seed one directly in this org.
    with Session(engine) as s:
        empty_org = Organization(name="Tpl CLI Empty Org")
        s.add(empty_org)
        s.commit()
        s.refresh(empty_org)
        empty_org_id = empty_org.id
        empty_org_tpl = ProjectTemplate(
            name="Empty org tpl",
            source_locale="en-US",
            target_locales=["fr-FR"],
            org_id=empty_org_id,
        )
        s.add(empty_org_tpl)
        s.commit()
        s.refresh(empty_org_tpl)
        empty_org_tpl_id = empty_org_tpl.id
    # multi_org_active() requires BOTH auth_required AND multi_org_enabled (see
    # access.py) -- mirrors test_apply_cross_org's / test_delete_cross_org's
    # settings pattern exactly.
    settings.set_many({"security_auth_required": "1", "multi_org_enabled": "1"})
    try:
        code, payload, _ = run_json(
            [
                "--org",
                str(empty_org_id),
                "template",
                "apply",
                str(empty_org_tpl_id),
                "--name",
                "CLI policy zero-cust org",
            ]
        )
        assert code == 0, payload
        with Session(engine) as s:
            proj = s.get(Project, payload["project_id"])
            assert proj.customer_id is None, proj.customer_id
    finally:
        settings.set_many({"security_auth_required": "0", "multi_org_enabled": "0"})
    print(
        "  cli apply customer policy: refused w/o customer, --customer-id works, "
        "conflict refused, zero-customer org still permitted - #22"
    )


def _login(who, pw):
    # Auth on: the login POST needs a CSRF token, and a successful login rotates
    # the session -> refresh the client's default X-CSRF-Token header.
    csrf_for(client)
    r = client.post(
        "/auth/login", data={"email": who, "password": pw, "next": "/"}, follow_redirects=False
    )
    if r.status_code == 303:
        csrf_for(client)
    return r


def test_delete_scope_auth():
    """#1: with the login wall ON, a plain translator can NOT delete a global (or
    any) template — the web route is manage-gated (403) AND the service raises for
    a translator actor. A manager delete still works. (Runs auth-on, then off.)"""
    # a global template to target
    pid = _mk("Scope src")
    gtpl_id = None
    with Session(engine) as s:
        gtpl_id = tplsvc.save_as_template(s, pid, "Scope tpl").id
    # turn the login wall on and seed a translator + a manager
    settings.set_many({"security_auth_required": "1"})
    try:
        if security.get_by_email("tpl-tx@x.com") is None:
            security.create_user("tpl-tx@x.com", "Tx", Role.TRANSLATOR, "tx-pass-123456")
        if security.get_by_email("tpl-mgr@x.com") is None:
            security.create_user("tpl-mgr@x.com", "Mgr", Role.MANAGER, "mgr-pass-123456")
        # SERVICE seam: a translator actor is denied (raises), template survives.
        with Session(engine) as s:
            tx = s.exec(select(User).where(User.email == "tpl-tx@x.com")).first()
            try:
                tplsvc.delete_template(gtpl_id, actor=tx)
                raise AssertionError("service let a translator delete a template")
            except tplsvc.TemplateError:
                pass
        assert tplsvc.get_template(gtpl_id) is not None, "translator delete leaked"
        # WEB route: translator -> 403, template survives.
        assert _login("tpl-tx@x.com", "tx-pass-123456").status_code == 303
        r = client.post(f"/templates/{gtpl_id}/delete")
        assert r.status_code == 403, r.status_code
        assert tplsvc.get_template(gtpl_id) is not None
        client.post("/auth/logout")
        csrf_for(client)
        # WEB route: manager -> authorized delete works.
        assert _login("tpl-mgr@x.com", "mgr-pass-123456").status_code == 303
        r = client.post(f"/templates/{gtpl_id}/delete")
        assert r.status_code == 303, r.status_code
        assert tplsvc.get_template(gtpl_id) is None, "manager delete did not remove it"
        client.post("/auth/logout")
        csrf_for(client)
    finally:
        settings.set_many({"security_auth_required": "0"})
    print("  delete scope: translator denied (web 403 + service raises), manager ok")


def test_delete_cross_org():
    """#1: with the multi-org gate ON, a delete scoped to org A must NOT reach a
    template in org B — the service org-filters the lookup so a cross-org id is
    not-found (returns False, row survives). An in-org manager delete works."""
    with Session(engine) as s:
        oa = Organization(name="Tpl Org A")
        ob = Organization(name="Tpl Org B")
        s.add(oa)
        s.add(ob)
        s.commit()
        s.refresh(oa)
        s.refresh(ob)
        oa_id, ob_id = oa.id, ob.id
        # a template living in org B
        tb = ProjectTemplate(
            name="Org B tpl", source_locale="en-US", target_locales=["fr-FR"], org_id=ob_id
        )
        s.add(tb)
        # an org-A admin actor
        admin_a = User(
            email="tpl-a-admin@x.com",
            name="A Admin",
            role=Role.ADMIN,
            org_id=oa_id,
            password_hash=security.hash_password("aa-pass-123456"),
        )
        s.add(admin_a)
        s.commit()
        s.refresh(tb)
        s.refresh(admin_a)
        tb_id = tb.id
    settings.set_many({"security_auth_required": "1", "multi_org_enabled": "1"})
    try:
        with Session(engine) as s:
            actor_a = s.get(User, admin_a.id)
            # org-A admin cannot delete org-B's template: not-found (False), survives.
            assert tplsvc.delete_template(tb_id, actor=actor_a) is False, (
                "cross-org delete was allowed"
            )
        assert tplsvc.get_template(tb_id) is not None, "cross-org template was deleted"
        # an in-org (org B) admin CAN delete it.
        with Session(engine) as s:
            admin_b = User(
                email="tpl-b-admin@x.com",
                name="B Admin",
                role=Role.ADMIN,
                org_id=ob_id,
                password_hash=security.hash_password("bb-pass-123456"),
            )
            s.add(admin_b)
            s.commit()
            s.refresh(admin_b)
            bid = admin_b.id
        with Session(engine) as s:
            actor_b = s.get(User, bid)
            assert tplsvc.delete_template(tb_id, actor=actor_b) is True, (
                "in-org admin could not delete its own org's template"
            )
        assert tplsvc.get_template(tb_id) is None
    finally:
        settings.set_many({"security_auth_required": "0", "multi_org_enabled": "0"})
    print("  delete cross-org: org-A actor gets not-found on org-B template; in-org ok")


def test_apply_cross_org():
    """#2: with the multi-org gate ON, applying a template scoped to org A must
    NOT be reachable from org B -- apply_template(actor=) treats it as not-found
    (TemplateError), the web route's org-A actor gets an error redirect + no
    project row, and an in-org apply still works normally."""
    with Session(engine) as s:
        oa = Organization(name="Tpl Apply Org A")
        ob = Organization(name="Tpl Apply Org B")
        s.add(oa)
        s.add(ob)
        s.commit()
        s.refresh(oa)
        s.refresh(ob)
        oa_id, ob_id = oa.id, ob.id
        ta = ProjectTemplate(
            name="Org A apply tpl", source_locale="en-US", target_locales=["fr-FR"], org_id=oa_id
        )
        s.add(ta)
        admin_b = User(
            email="tpl-apply-b@x.com",
            name="B Admin",
            role=Role.ADMIN,
            org_id=ob_id,
            password_hash=security.hash_password("bb-pass-123456"),
        )
        s.add(admin_b)
        s.commit()
        s.refresh(ta)
        s.refresh(admin_b)
        ta_id = ta.id
    # same_org (and hence apply_template's org gate) is a strict no-op while the
    # multi-org gate is off -- turn it ON before either the service-seam or the
    # web-route check, matching test_delete_cross_org's ordering.
    settings.set_many({"security_auth_required": "1", "multi_org_enabled": "1"})
    try:
        # SERVICE seam: an org-B actor gets not-found applying org-A's template.
        with Session(engine) as s:
            actor_b = s.get(User, admin_b.id)
            try:
                tplsvc.apply_template(s, ta_id, "Stolen apply", actor=actor_b)
                raise AssertionError("cross-org apply was allowed")
            except tplsvc.TemplateError:
                pass
        with Session(engine) as s:
            assert s.exec(select(Project).where(Project.name == "Stolen apply")).first() is None, (
                "cross-org apply created a project"
            )
        # WEB route: org-B actor gets an error redirect + no project row.
        assert _login("tpl-apply-b@x.com", "bb-pass-123456").status_code == 303
        r = client.post(
            "/projects/from-template", data={"template_id": str(ta_id), "name": "Web stolen apply"}
        )
        assert "err=" in r.headers.get("location", ""), r.headers
        with Session(engine) as s:
            assert s.exec(select(Project).where(Project.name == "Web stolen apply")).first() is None
        client.post("/auth/logout")
        csrf_for(client)
    finally:
        settings.set_many({"security_auth_required": "0", "multi_org_enabled": "0"})
    # sanity: an in-org apply (no actor / org scope) still works.
    with Session(engine) as s:
        proj, warnings = tplsvc.apply_template(s, ta_id, "In-org apply")
        assert proj is not None and not warnings
    print("  apply cross-org: org-B not-found on org-A template (service + web); in-org apply ok")


def test_apply_grandfathered_locales():
    """#3: a template snapshotted from a legacy/connector project may carry
    non-catalog locale codes (e.g. "iw", "sr-Latn"). apply_template must SKIP
    each invalid target with a warning (not hard-fail via content.project_create
    raising InvalidLocaleError) -- the P7 skip+warn contract. An invalid source
    locale similarly warns and falls back to en-US. An all-invalid snapshot
    fails cleanly with a TemplateError (never a locale-less project)."""
    # Insert the legacy-locale rows DIRECTLY (bypassing validate_codes, exactly
    # like a pre-P2 or connector-synced project would carry them).
    pid = _mk("Legacy locale src", locales=())
    with Session(engine) as s:
        s.add(TargetLocale(project_id=pid, locale="fr-FR"))
        s.add(TargetLocale(project_id=pid, locale="iw"))  # legacy Hebrew code
        s.add(TargetLocale(project_id=pid, locale="sr-Latn"))  # legacy Serbian-Latin
        s.commit()
        tpl = tplsvc.save_as_template(s, pid, "Legacy locale tpl")
    with Session(engine) as s:
        proj, warnings = tplsvc.apply_template(s, tpl.id, "Legacy locale applied")
    assert proj is not None, "apply hard-failed on a grandfathered locale"
    assert _locales_of(proj.id) == ["fr-FR"], _locales_of(proj.id)
    assert any("iw" in w for w in warnings), warnings
    assert any("sr-Latn" in w for w in warnings), warnings
    assert len(warnings) == 2, warnings
    # Invalid source locale: warn + fall back to en-US, valid targets kept.
    with Session(engine) as s:
        s2 = tplsvc.save_as_template(
            s, _mk("Legacy src locale", locales=("de-DE",)), "Legacy src tpl"
        )
        s2.source_locale = "iw"
        s.add(s2)
        s.commit()
        s2_id = s2.id
    with Session(engine) as s:
        proj2, w2 = tplsvc.apply_template(s, s2_id, "Legacy src applied")
    assert proj2.source_locale == "en-US", proj2.source_locale
    assert any("source locale" in w and "en-US" in w for w in w2), w2
    # All targets invalid -> clean TemplateError, no locale-less project.
    pid3 = _mk("All invalid src", locales=())
    with Session(engine) as s:
        s.add(TargetLocale(project_id=pid3, locale="iw"))
        s.commit()
        tpl3 = tplsvc.save_as_template(s, pid3, "All invalid tpl")
    with Session(engine) as s:
        try:
            tplsvc.apply_template(s, tpl3.id, "All invalid applied")
            raise AssertionError("all-invalid-target template applied anyway")
        except tplsvc.TemplateError:
            pass
        assert s.exec(select(Project).where(Project.name == "All invalid applied")).first() is None
    print(
        "  apply grandfathered locales: skip+warn per code, source fallback, "
        "all-invalid fails clean ok"
    )


def test_prefill_draft():
    """Feature: template prefill. The SERVICE seam (templates.prefill_draft) is
    read-only: re-validates locales exactly like apply_template, includes a
    GLOBAL segmentation profile as a value, but a captured default_workflow_id
    ALWAYS degrades to a warning (never a value -- the base create form the
    dialog fills has no field for it; see the docstring). Never creates/mutates
    anything, and a nonexistent id is a plain not-found TemplateError."""
    tpl = tplsvc.get_by_name("Snap tpl")  # CA-scoped; carries WF_A_ID (CA's own
    # customer-scoped workflow) + PROF_ID (a GLOBAL segmentation profile).
    with Session(engine) as s:
        result = tplsvc.prefill_draft(s, tpl.id)
    draft, warnings = result["draft"], result["warnings"]
    assert sorted(draft["target_locales"]) == ["de-DE", "fr-FR"], draft
    assert draft["source_locale"] == "en-US"
    assert draft["default_provider"] == "claude"
    assert draft["customer_id"] == CA_ID
    assert draft["segmentation_profile_id"] == PROF_ID, draft
    assert "default_workflow_id" not in draft, draft
    assert any("default workflow" in w and "A private wf" in w for w in warnings), warnings

    with Session(engine) as s:
        try:
            tplsvc.prefill_draft(s, 999999999)
            raise AssertionError("prefill_draft found a nonexistent template")
        except tplsvc.TemplateError:
            pass
    print("  prefill_draft: draft shape + workflow-always-warns + global-seg-kept ok")


def test_prefill_dangling_and_archived():
    """Degrade paths test_prefill_draft doesn't cover: an ARCHIVED customer
    (retired from every picker -- SPEC-customer-archive; the only reachable
    "dangling customer" case, since ProjectTemplate.customer_id carries a REAL FK
    unlike the workflow/segmentation refs -- a customer row can never actually
    vanish out from under a template, only get archived), a dangling
    segmentation_profile_id, and a CUSTOMER-scoped one (the dialog's segmentation
    picker only ever offers GLOBAL profiles, matching the base create form
    itself). Each degrades to a warning + an omitted key, never a crash or a
    silently-wrong value."""
    from app.services import customers as customer_service

    with Session(engine) as s:
        cust = Customer(name="Archived Prefill Cust")
        s.add(cust)
        s.commit()
        s.refresh(cust)
        cid = cust.id
        archived_tpl = ProjectTemplate(
            name="Archived cust prefill tpl",
            source_locale="en-US",
            target_locales=["fr-FR"],
            customer_id=cid,
        )
        s.add(archived_tpl)
        s.commit()
        s.refresh(archived_tpl)
        archived_id = archived_tpl.id
        customer_service.archive_customer(s, cid, actor="test")
    with Session(engine) as s:
        r2 = tplsvc.prefill_draft(s, archived_id)
    assert "customer_id" not in r2["draft"], r2["draft"]
    assert any("archived" in w for w in r2["warnings"]), r2["warnings"]

    dangling_prof_id = 888888
    with Session(engine) as s:
        seg_tpl = ProjectTemplate(
            name="Dangle seg prefill tpl",
            source_locale="en-US",
            target_locales=["fr-FR"],
            segmentation_profile_id=dangling_prof_id,
        )
        s.add(seg_tpl)
        s.commit()
        s.refresh(seg_tpl)
        r3 = tplsvc.prefill_draft(s, seg_tpl.id)
    assert "segmentation_profile_id" not in r3["draft"], r3["draft"]
    assert any(str(dangling_prof_id) in w and "no longer exists" in w for w in r3["warnings"]), r3[
        "warnings"
    ]

    cust_prof = segprof.create_profile("Prefill cust-scoped prof", customer_id=CA_ID)
    with Session(engine) as s:
        cust_seg_tpl = ProjectTemplate(
            name="Cust seg prefill tpl",
            source_locale="en-US",
            target_locales=["fr-FR"],
            segmentation_profile_id=cust_prof.id,
        )
        s.add(cust_seg_tpl)
        s.commit()
        s.refresh(cust_seg_tpl)
        r4 = tplsvc.prefill_draft(s, cust_seg_tpl.id)
    assert "segmentation_profile_id" not in r4["draft"], r4["draft"]
    assert any("customer-scoped" in w for w in r4["warnings"]), r4["warnings"]
    print("  prefill dangling/archived: customer + segmentation degrade paths ok")


def test_prefill_web_route():
    """Web: GET /projects/templates/{id}/prefill.json returns the SAME {ok, draft,
    warnings} shape POST /projects/author.json's promptable-setup draft uses (so
    the dialog reuses applyDraft()/renderWarnings() verbatim). Unknown id -> a
    plain 404 JSON, never a 500/traceback."""
    tpl = tplsvc.get_by_name("Glob tpl")  # global, no dangling refs
    r = client.get(f"/projects/templates/{tpl.id}/prefill.json")
    assert r.status_code == 200, r.text
    payload = r.json()
    assert payload["ok"] is True
    assert sorted(payload["draft"]["target_locales"]) == ["de-DE", "fr-FR"], payload
    assert "customer_id" not in payload["draft"], payload
    # 2026-07-14: a GLOBAL template prefills no customer, and (a customer exists
    # in this suite) a project requires one — so the only warning is the pick-a-customer
    # prompt. No OTHER (dangling-ref) warnings for this clean global template.
    assert all("Pick a customer" in w for w in payload["warnings"]), payload
    assert any("Pick a customer" in w for w in payload["warnings"]), payload

    r = client.get("/projects/templates/999999999/prefill.json")
    assert r.status_code == 404, r.status_code
    assert r.json()["ok"] is False
    print("  prefill web route: 200 shape + 404 unknown ok")


def test_prefill_scoping():
    """R2-3 + multi-org: mirrors test_apply_cross_org's pattern for this NEW
    read-only seam -- a customer-bound caller cannot fetch a FOREIGN customer's
    template (404, never enumerable), and under the multi-org gate a cross-org id
    is likewise not-found."""
    tpl_a = tplsvc.get_by_name("Snap tpl")  # CA-scoped
    with Session(engine) as s:
        bound_b = User(
            email="tpl-prefill-bound-b@x.com",
            name="Bound B",
            role=Role.TRANSLATOR,
            customer_id=CB_ID,
            password_hash=security.hash_password("bound-pass-123456"),
        )
        s.add(bound_b)
        s.commit()
    settings.set_many({"security_auth_required": "1"})
    try:
        assert _login("tpl-prefill-bound-b@x.com", "bound-pass-123456").status_code == 303
        r = client.get(f"/projects/templates/{tpl_a.id}/prefill.json")
        assert r.status_code == 404, r.status_code
        client.post("/auth/logout")
        csrf_for(client)
    finally:
        settings.set_many({"security_auth_required": "0"})

    with Session(engine) as s:
        oa = Organization(name="Tpl Prefill Org A")
        ob = Organization(name="Tpl Prefill Org B")
        s.add(oa)
        s.add(ob)
        s.commit()
        s.refresh(oa)
        s.refresh(ob)
        ta = ProjectTemplate(
            name="Org A prefill tpl", source_locale="en-US", target_locales=["fr-FR"], org_id=oa.id
        )
        admin_b = User(
            email="tpl-prefill-b-admin@x.com",
            name="B Admin",
            role=Role.ADMIN,
            org_id=ob.id,
            password_hash=security.hash_password("bb-pass-123456"),
        )
        s.add(ta)
        s.add(admin_b)
        s.commit()
        s.refresh(ta)
        ta_id = ta.id
    settings.set_many({"security_auth_required": "1", "multi_org_enabled": "1"})
    try:
        assert _login("tpl-prefill-b-admin@x.com", "bb-pass-123456").status_code == 303
        r = client.get(f"/projects/templates/{ta_id}/prefill.json")
        assert r.status_code == 404, r.status_code
        client.post("/auth/logout")
        csrf_for(client)
    finally:
        settings.set_many({"security_auth_required": "0", "multi_org_enabled": "0"})
    print("  prefill scoping: R2-3 foreign-customer 404 + cross-org 404 ok")


def test_dialog_picker_markup():
    """Web: the "Start from a template" section renders in the GLOBAL new-project
    dialog (rendered on every page via base.html) only when np.templates is
    non-empty, and is entirely OMITTED (not just rendered empty) when no template
    is visible to the caller."""
    # "np-template-pick" is the wrapper <div>'s CSS class -- unlike the
    # data-np-tpl-pick ATTRIBUTE (also referenced by the always-shipped <script>'s
    # querySelector calls, so it's present on every page regardless), the class
    # string appears ONLY inside the conditionally-rendered markup itself.
    page = client.get("/projects")
    assert "np-template-pick" in page.text and "Start from a template" in page.text

    # A brand-new multi-org-scoped org has ZERO visible templates: every template
    # created by this suite carries org_id=None, which an org-scoped all_templates
    # query never matches -- the section must vanish, not render empty.
    with Session(engine) as s:
        org_new = Organization(name="Tpl Pick Empty Org")
        s.add(org_new)
        s.commit()
        s.refresh(org_new)
        admin_new = User(
            email="tpl-pick-empty@x.com",
            name="Empty Org Admin",
            role=Role.ADMIN,
            org_id=org_new.id,
            password_hash=security.hash_password("empty-pass-123456"),
        )
        s.add(admin_new)
        s.commit()
    settings.set_many({"security_auth_required": "1", "multi_org_enabled": "1"})
    try:
        assert _login("tpl-pick-empty@x.com", "empty-pass-123456").status_code == 303
        page = client.get("/projects")
        assert "np-template-pick" not in page.text, (
            "template picker rendered with zero visible templates"
        )
        client.post("/auth/logout")
        csrf_for(client)
    finally:
        settings.set_many({"security_auth_required": "0", "multi_org_enabled": "0"})
    print("  dialog picker markup: present with templates, omitted with none ok")


def test_platform_operator_delete_flag():
    """Day-2 item #6: `delete_template(platform_operator=True)` is the SOLE
    sanctioned bypass of the org-scope not-found check, and ONLY for a GLOBAL
    (org_id NULL) row.

      1. gate OFF: platform_operator=True changes nothing observable -- a plain
         delete_template(tid) already deletes any row single-tenant, so passing
         the flag must be byte-identical (it only matters once same_org can
         return False, i.e. under the gate).
      2. gate ON, GLOBAL row, no actor/org_id: platform_operator=True deletes it
         (the back-office's own call shape -- actor=None, org_id=None).
      3. gate ON, GLOBAL row, WITHOUT the flag: an org-scoped actor still gets
         not-found (unchanged fail-closed default -- this is the behavior the
         flag must NOT alter for ordinary callers).
      4. gate ON, ORG-OWNED row: platform_operator=True is a NO-OP -- the normal
         actor/org_id scope+role checks still apply, so a foreign-org actor is
         still refused even with the flag set (the flag never widens cross-org
         access to a real org's own template)."""
    with Session(engine) as s:
        gtpl = ProjectTemplate(
            name="PO gate-off tpl", source_locale="en-US", target_locales=["fr-FR"], org_id=None
        )
        s.add(gtpl)
        s.commit()
        s.refresh(gtpl)
        gtpl_id = gtpl.id
    # (1) gate off: byte-identical -- deletes fine with or without the flag.
    assert tplsvc.delete_template(gtpl_id, platform_operator=True) is True
    assert tplsvc.get_template(gtpl_id) is None

    with Session(engine) as s:
        oa = Organization(name="PO Org A")
        s.add(oa)
        s.commit()
        s.refresh(oa)
        oa_id = oa.id
        # org_id is a real FK (projecttemplate.org_id -> organization.id): the
        # "different org" that owns otpl must be a REAL org, not oa_id + 1000000
        # (a synthetic id that never persisted on Postgres). Any org != oa_id proves
        # the foreign-org refusal.
        ob = Organization(name="PO Org B")
        s.add(ob)
        s.commit()
        s.refresh(ob)
        ob_id = ob.id
        admin_a = User(
            email="po-admin-a@x.com",
            name="PO Admin A",
            role=Role.ADMIN,
            org_id=oa_id,
            password_hash=security.hash_password("po-pass-123456"),
        )
        gtpl2 = ProjectTemplate(
            name="PO global tpl", source_locale="en-US", target_locales=["fr-FR"], org_id=None
        )
        otpl = ProjectTemplate(
            name="PO org tpl", source_locale="en-US", target_locales=["fr-FR"], org_id=ob_id
        )
        s.add(admin_a)
        s.add(gtpl2)
        s.add(otpl)
        s.commit()
        s.refresh(admin_a)
        s.refresh(gtpl2)
        s.refresh(otpl)
        gtpl2_id, otpl_id = gtpl2.id, otpl.id

    settings.set_many({"security_auth_required": "1", "multi_org_enabled": "1"})
    try:
        # (3) gate on, GLOBAL row, WITHOUT the flag: still not-found for a
        # regular org-scoped actor (the existing fail-closed behavior).
        with Session(engine) as s:
            actor_a = s.get(User, admin_a.id)
            assert tplsvc.delete_template(gtpl2_id, actor=actor_a) is False, (
                "a regular actor deleted a GLOBAL template without the flag"
            )
        assert tplsvc.get_template(gtpl2_id) is not None

        # (2) gate on, GLOBAL row, operator call shape (actor=None, org_id=None):
        # platform_operator=True deletes it.
        assert (
            tplsvc.delete_template(gtpl2_id, actor=None, org_id=None, platform_operator=True)
            is True
        )
        assert tplsvc.get_template(gtpl2_id) is None

        # (4) gate on, ORG-OWNED row: the flag is a NO-OP -- a foreign-org actor
        # (admin_a is in oa_id, otpl lives in a DIFFERENT synthetic org id) is
        # still refused even with platform_operator=True set.
        with Session(engine) as s:
            actor_a = s.get(User, admin_a.id)
            assert (
                tplsvc.delete_template(otpl_id, actor=actor_a, platform_operator=True) is False
            ), "platform_operator widened cross-org access to an ORG-OWNED template"
        assert tplsvc.get_template(otpl_id) is not None
    finally:
        settings.set_many({"security_auth_required": "0", "multi_org_enabled": "0"})
    print("  platform_operator flag: gate-off no-op, global-only bypass, org-owned rows unaffected")


def _tpl_row(name, org_id):
    with Session(engine) as s:
        row = ProjectTemplate(
            name=name, source_locale="en-US", target_locales=["fr-FR"], org_id=org_id
        )
        s.add(row)
        s.commit()
        s.refresh(row)
        return row.id


def test_cli_operator_delete_door():
    """GATE R2 MED-2: the CLI wiring, which had no test at any level.

    The service flag is covered by test_platform_operator_delete_flag above. This covers
    what the drop-backoffice train actually changed, and it is a NARROWING rather than the
    restoration its first comment claimed:

      * `--operator` reaches platform_operator=True and deletes a GLOBAL row. This already
        worked on main, because `--operator` sets `_op_org_id` to None, so the claim that
        the capability had been lost was false.
      * `--operator` can NO LONGER delete another org's ORG-OWNED row. Main allowed that:
        the unscoped lookup found the row and nothing re-checked ownership. This is the one
        real behaviour change and it is the reason the wiring is worth keeping.
      * an ORG-SCOPED run is unchanged and cannot see a global row at all.
    """
    import hashlib
    import os

    init_db()
    settings.set_many(
        {
            "security_auth_required": "1",
            "multi_org_enabled": "1",
            "operator_key_hash": hashlib.sha256(b"cli-op-secret-123").hexdigest(),
            # GATE R5 MED-2: auditing OFF for this whole test. With it ON, the audit
            # assertion below passes whether or not the caller sets force=True, which is
            # exactly the hole the gate measured: it mutated away `force=` on this verb and
            # this suite stayed GREEN while claiming to bind it. `template delete
            # --operator` is the only surviving door to an install-wide destructive verb,
            # so the property worth asserting is that it records the deletion even when the
            # operator has switched recording off.
            "audit_enabled": "0",
        }
    )
    settings.invalidate_cache()
    os.environ["LOC_TMS_OPERATOR_KEY"] = "cli-op-secret-123"
    try:
        with Session(engine) as s:
            org = Organization(name="CLI Op Org")
            s.add(org)
            s.commit()
            s.refresh(org)
            org_id = org.id
        glob = _tpl_row("cli-op-global", None)
        owned = _tpl_row("cli-op-owned", org_id)

        # 1. --operator deletes the GLOBAL row.
        code, out, err = run(["--operator", "template", "delete", str(glob), "--yes"])
        assert code == 0, (code, out, err)
        assert tplsvc.get_template(glob) is None, "the global row survived the operator delete"

        # 1b. GATE R3 MED-2: and it AUDITS. On main this delete went through the back-office
        # route, which wrote `backoffice.template_delete`; that route is gone, so without a
        # row here the only surviving door to an install-wide destructive verb writes
        # nothing. delete_template deliberately does not audit (the caller owns it), which
        # is exactly why deleting the audited caller could silently take the audit with it.
        # This is round-1 MED-1's shape ("coverage deleted rather than relocated") on the
        # audit axis, so it gets an assertion rather than a promise.
        with Session(engine) as s:
            rows = s.exec(select(AuditLog).where(AuditLog.action == "template.delete")).all()
        assert rows, (
            "cli template delete wrote NO audit row. Every other template-delete surface "
            "audits (web, /api) and so does `template rename` beside it."
        )
        assert any(f"template {glob}" == r.target for r in rows), (
            "the audit row does not name the deleted template",
            [r.target for r in rows],
        )
        assert any("GLOBAL" in (r.detail or "") for r in rows), (
            "the operator delete of a GLOBAL row must be distinguishable in the audit "
            "detail from an ordinary org-owned delete",
            [r.detail for r in rows],
        )

        # 2. --operator must NOT delete another org's ORG-OWNED row. THE NARROWING.
        run(["--operator", "template", "delete", str(owned), "--yes"])
        assert tplsvc.get_template(owned) is not None, (
            "REGRESSION: --operator deleted an ORG-OWNED template. The operator door is for "
            "unowned rows only; main allowed this and the drop-backoffice train closed it."
        )

        # 3. An ORG-SCOPED run cannot even see a global row.
        glob2 = _tpl_row("cli-op-global-2", None)
        code, out, err = run(["--org", str(org_id), "template", "delete", str(glob2), "--yes"])
        assert code != 0, ("an org-scoped caller must not delete a global template", out, err)
        assert tplsvc.get_template(glob2) is not None
    finally:
        os.environ.pop("LOC_TMS_OPERATOR_KEY", None)
        settings.set_many(
            {
                "security_auth_required": "0",
                "multi_org_enabled": "0",
                "operator_key_hash": "",
                "audit_enabled": "1",
            }
        )
        settings.invalidate_cache()
    print(
        "  CLI operator door: global deletable (audited even with audit_enabled=0), "
        "ORG-OWNED refused (narrowing), org-scoped blind"
    )


if __name__ == "__main__":
    test_save_snapshot()
    test_apply_exact()
    test_isolation()
    test_dangling_refs_degrade()
    test_web_routes()
    test_cli()
    test_cli_apply_customer_policy()
    test_delete_scope_auth()
    test_delete_cross_org()
    test_apply_cross_org()
    test_apply_grandfathered_locales()
    test_prefill_draft()
    test_prefill_dangling_and_archived()
    test_prefill_web_route()
    test_prefill_scoping()
    test_dialog_picker_markup()
    test_platform_operator_delete_flag()
    test_cli_operator_delete_door()
    print("TEMPLATES OK")
