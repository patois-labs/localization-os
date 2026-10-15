"""Customer archive/unarchive + PromptTemplate customer attachment — verification.

SPEC-customer-archive + SPEC-pt-attach. Offline + hermetic (throwaway
LOC_TMS_DATA_DIR, provider keys blanked -> MockProvider; no network is ever touched).

FEATURE 1 — customer archive/unarchive:
  model + migration
    - customer.archived_at exists + round-trips; a bare customer reads NULL (active).
  service
    - archive stamps archived_at + audits customer.archive; idempotent-safe (a double
      archive is a clean CustomerArchiveError); unarchive clears + audits; unarchiving
      an active one is a clean error.
  picker removal (archive = a selection/listing concern, NOT a kill switch)
    - an archived customer disappears from the PROJECT-CREATION dialog picker, a
      BINDING picker (workflow-scope select), and the CLI `customer list` default
      (--all / --archived reveal it) — while its EXISTING project still renders and is
      structurally untouched; unarchive restores it to every picker.
    - FAIL-PRE-FIX PROOF: the pre-fix picker query was a bare `select(Customer)` (no
      active_only) — reproduced inline, it INCLUDES the archived customer; the new
      active_only() seam excludes it.
  service-level create guard
    - project create for an archived customer is REFUSED (ArchivedCustomerError) at the
      SERVICE (not just omitted from the UI), on the service + web + CLI surfaces.
  RBAC
    - archive/unarchive are strictly admin on web AND /api (a non-admin is 403); CLI is
      trusted-local; a foreign-customer caller gets 404 on the new by-id routes.

FEATURE 2 — PromptTemplate customer attachment + isolation:
  create-with-attachment (service + web + CLI)
    - attaching a customer sets scope=CUSTOMER + customer_id; blank = GLOBAL + NULL
      (byte-identical to today). An archived / foreign customer is refused.
  bound creator forced to own customer
    - a bound caller's PT is FORCED to its own customer (a foreign customer_id is a
      clean error; a blank is force-stamped) — enforced at the SERVICE seam.
  list/read visibility
    - a bound caller sees GLOBAL + own only (a foreign PT is absent), on the service +
      the web /templates page; unbound staff see all.
    - FAIL-PRE-FIX PROOF: the pre-fix list was org-filtered ONLY — reproduced inline it
      LEAKS the foreign customer's template to a bound caller; the new list excludes it.
  existing GLOBAL PTs byte-unaffected.

Final line: "CUSTOMER ARCHIVE PT ATTACH OK".
"""

import io
import os
import sys
from contextlib import redirect_stdout

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(_ROOT)
sys.path.insert(0, _ROOT)
from tests._bootstrap import init_env

init_env("loctms_archpt_")

from fastapi.testclient import TestClient
from sqlalchemy import inspect
from sqlmodel import Session, select

from app.db import engine, init_db
from app.main import app
from app.models import (
    AuditLog,
    Customer,
    Project,
    PromptTemplate,
    Role,
    Scope,
    User,
)
from app.services import apikeys, content, security, settings
from app.services import customers as customer_service
from app.services import templates as template_service
from app.services import workflows as wf_service
from tests._csrf import csrf_for

init_db()


# --- helpers -----------------------------------------------------------------
def _auth_on():
    settings.set_many({"security_auth_required": "1", "multi_org_enabled": "0"})
    settings.invalidate_cache()


def _auth_off():
    settings.set_many({"security_auth_required": "0", "multi_org_enabled": "0"})
    settings.invalidate_cache()


def _customer(name):
    with Session(engine) as s:
        c = Customer(name=name)
        s.add(c)
        s.commit()
        s.refresh(c)
        return c.id


def _project(name, customer_id=None, targets="fr-FR"):
    with Session(engine) as s:
        p = content.project_create(
            s,
            name,
            source_locale="en-US",
            target_locales=targets,
            customer_id=(str(customer_id) if customer_id else ""),
        )
        return p.id


def _user(email, *, role=Role.ADMIN, customer_id=None):
    with Session(engine) as s:
        u = User(email=email, name=email, role=role, active=True, customer_id=customer_id)
        s.add(u)
        s.commit()
        s.refresh(u)
        return u.id


def _cli(argv):
    from app.cli.__main__ import main as cli_main

    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = cli_main(["--json", *argv])
    return rc, buf.getvalue()


def _last_audit(action):
    with Session(engine) as s:
        return s.exec(
            select(AuditLog).where(AuditLog.action == action).order_by(AuditLog.id.desc())
        ).first()


def _opt(cid, name):
    """The EXACT <option> string the customer pickers emit (project-creation dialog +
    workflow/glossary/connection scope selects all render this identical shape), so a
    substring probe is unambiguous — the name appearing elsewhere (a project row, a
    dashboard link) is a DIFFERENT HTML shape and never a false match."""
    return '<option value="%d">%s</option>' % (cid, name)  # noqa: UP031


# ============================================================================
# FEATURE 1 — customer archive/unarchive
# ============================================================================
def test_model_and_migration():
    cols = {c["name"] for c in inspect(engine).get_columns("customer")}
    assert "archived_at" in cols, "customer.archived_at column missing"
    cid = _customer("Model Legacy")
    with Session(engine) as s:
        c = s.get(Customer, cid)
        assert c.archived_at is None, "a bare customer must read archived_at NULL (active)"
        assert customer_service.is_active(c), "NULL archived_at = active"
    print("  model: customer.archived_at exists; bare customer reads NULL/active OK")


def test_archive_unarchive_service_and_audit():
    cid = _customer("Svc Arch")
    with Session(engine) as s:
        customer_service.archive_customer(s, cid, actor="a@x.test")
        c = s.get(Customer, cid)
        assert c.archived_at is not None and not customer_service.is_active(c)
    a = _last_audit("customer.archive")
    assert a and str(cid) in a.target, "archive must audit customer.archive"
    # idempotent-safe: a second archive is a clean error, not a re-stamp
    with Session(engine) as s:
        try:
            customer_service.archive_customer(s, cid, actor="a@x.test")
            assert False, "double-archive must raise"
        except customer_service.CustomerArchiveError:
            pass
    # unarchive clears + audits; unarchiving an active one is a clean error
    with Session(engine) as s:
        customer_service.unarchive_customer(s, cid, actor="a@x.test")
        assert s.get(Customer, cid).archived_at is None
        try:
            customer_service.unarchive_customer(s, cid, actor="a@x.test")
            assert False, "unarchiving an active customer must raise"
        except customer_service.CustomerArchiveError:
            pass
    assert _last_audit("customer.unarchive"), "unarchive must audit customer.unarchive"
    print("  service: archive/unarchive stamp+clear, idempotent-safe errors, audited OK")


def test_archive_removes_from_pickers_but_existing_project_works():
    _auth_off()
    active_id = _customer("PickerActive")
    gone_id = _customer("PickerGone")
    pid = _project("gone-existing-proj", customer_id=gone_id)
    # FAIL-PRE-FIX PROOF: the old picker query was a bare select(Customer) (no
    # active_only). Reproduce it inline — it INCLUDES the to-be-archived customer.
    with Session(engine) as s:
        customer_service.archive_customer(s, gone_id, actor="a@x.test")
        prefix_ids = {c.id for c in s.exec(select(Customer)).all()}
        assert gone_id in prefix_ids, "pre-fix bare query includes archived (proof setup)"
        postfix_ids = {c.id for c in s.exec(customer_service.active_only(select(Customer))).all()}
        assert gone_id not in postfix_ids, "active_only() must exclude the archived customer"
        assert active_id in postfix_ids, "active_only() must keep active customers"

    c = TestClient(app)
    # (1) project-creation picker: the global new-project dialog (base.html).
    body = c.get("/").text
    # 2026-07-14: the customer <select> now preselects when exactly one customer
    # is visible (a project requires a customer), so the option may carry ` selected`.
    # Probe the option START (attributes-tolerant); the archived-exclusion check stays
    # exact (an archived customer is never rendered at all).
    assert ('<option value="%d"' % active_id) in body and "PickerActive" in body, (  # noqa: UP031
        "active customer must be offered"
    )
    assert _opt(gone_id, "PickerGone") not in body, (
        "archived customer must vanish from the project-creation picker"
    )
    # (2) a binding picker: the workflow-scope <select> on the create page
    # /workflows/new (SPEC Workflow Studio Wave A moved the create surface there).
    body = c.get("/workflows/new").text
    assert _opt(active_id, "PickerActive") in body
    assert _opt(gone_id, "PickerGone") not in body, (
        "archived customer must vanish from the workflow-scope binding picker"
    )
    # (3) CLI `customer list` default hides archived; --all / --archived reveal it.
    _rc, out = _cli(["customer", "list"])
    assert "PickerActive" in out and "PickerGone" not in out, (
        "CLI `customer list` default must hide archived"
    )
    _rc, out_all = _cli(["customer", "list", "--all"])
    assert "PickerGone" in out_all, "CLI `customer list --all` must include archived"
    _rc, out_arch = _cli(["customer", "list", "--archived"])
    assert "PickerGone" in out_arch and "PickerActive" not in out_arch, (
        "CLI `customer list --archived` must show ONLY archived"
    )

    # the archived customer's EXISTING project still renders + is structurally untouched
    # (archive is a selection/listing concern, it touches no project/doc/segment rows).
    assert c.get("/projects/%d" % pid).status_code == 200, (  # noqa: UP031
        "an archived customer's existing project must still render"
    )
    with Session(engine) as s:
        assert s.get(Project, pid).customer_id == gone_id, "existing project untouched"

    # unarchive restores it to every picker
    with Session(engine) as s:
        customer_service.unarchive_customer(s, gone_id, actor="a@x.test")
    assert _opt(gone_id, "PickerGone") in c.get("/workflows/new").text, (
        "unarchive must restore the customer to pickers"
    )
    print(
        "  pickers: archived customer removed from create/binding/CLI (existing "
        "project intact); unarchive restores; fail-pre-fix proven OK"
    )


def test_archived_customer_cannot_be_chosen_for_new_project():
    _auth_off()
    cid = _customer("NoNewProj")
    with Session(engine) as s:
        customer_service.archive_customer(s, cid, actor="a@x.test")
    # service-level guard (not just UI omission)
    with Session(engine) as s:
        try:
            content.project_create(s, "svc-blocked", customer_id=str(cid), target_locales="fr-FR")
            assert False, "creating a project for an archived customer must raise"
        except content.ArchivedCustomerError:
            pass
    # web: POST /projects with the archived customer_id is refused (redirect w/ err),
    # no project row created
    c = TestClient(app)
    n_before = _project_count()
    r = c.post(
        "/projects",
        data={
            "name": "web-blocked",
            "source_locale": "en-US",
            "target_locales": ["fr-FR"],
            "customer_id": str(cid),
        },
        follow_redirects=False,
    )
    assert r.status_code == 303 and "err=" in r.headers.get("location", ""), (
        "web create for an archived customer must redirect with an error"
    )
    assert _project_count() == n_before, "no project row may be created for archived"
    # CLI: `project create --customer <archived>` fails with EXIT_USAGE (2)
    rc, _out = _cli(
        ["project", "create", "cli-blocked", "--targets", "fr-FR", "--customer", str(cid)]
    )
    assert rc == 2, f"CLI create for an archived customer must exit EXIT_USAGE ({rc})"
    print("  create-guard: archived customer refused for a new project on service/web/CLI OK")


def test_archived_customer_api_returns_400():
    # GitHub #455: POST /api/projects with an archived customer_id must answer a clean
    # 400 (ok false), NOT a 500. The service raises ArchivedCustomerError; the /api
    # handler's 400 arm must catch it (the web route and CLI already did).
    _auth_on()
    try:
        cid = _customer("ApiArchived")
        with Session(engine) as s:
            customer_service.archive_customer(s, cid, actor="a@x.test")
        adm_uid = _user("api455@x.test", role=Role.ADMIN)
        _r, tok = apikeys.create_key(adm_uid, "k", scopes="docs")
        c = TestClient(app)
        n_before = _project_count()
        r = c.post(
            "/api/projects",
            headers={"X-Loc-Token": tok},
            json={"name": "api-blocked", "targets": "fr-FR", "customer_id": str(cid)},
        )
        assert r.status_code == 400, (
            f"/api create for an archived customer must be 400, not {r.status_code}"
        )
        assert r.json().get("ok") is False, (
            f"archived-customer /api create must be ok=false: {r.json()}"
        )
        assert _project_count() == n_before, "no project row may be created for archived"
        print(
            "  create-guard: /api POST with an archived customer -> clean 400 (ok false), no row OK"
        )
    finally:
        _auth_off()


def _project_count():
    with Session(engine) as s:
        return len(s.exec(select(Project.id)).all())


def test_archive_rbac_admin_only_and_foreign_404():
    _auth_on()
    try:
        home = _customer("RbacHome")
        other = _customer("RbacOther")

        # --- web: a MANAGER (can_manage, NOT admin) is 403 on archive + unarchive ----
        security.create_user("mgr@x.test", "mgr", Role.MANAGER, "pw-123456789")
        wc = TestClient(app)
        csrf_for(wc)
        wc.post(
            "/auth/login",
            data={"email": "mgr@x.test", "password": "pw-123456789", "next": "/"},
            follow_redirects=False,
        )
        csrf_for(wc)
        r = wc.post("/customers/%d/archive" % home, follow_redirects=False)  # noqa: UP031
        assert r.status_code == 403, f"non-admin web archive must be 403 ({r.status_code})"
        with Session(engine) as s:
            assert s.get(Customer, home).archived_at is None, "manager must not archive"

        # --- web: an ADMIN can archive + unarchive ----------------------------------
        security.create_user("adm@x.test", "adm", Role.ADMIN, "pw-123456789")
        ac = TestClient(app)
        csrf_for(ac)
        ac.post(
            "/auth/login",
            data={"email": "adm@x.test", "password": "pw-123456789", "next": "/"},
            follow_redirects=False,
        )
        csrf_for(ac)
        r = ac.post("/customers/%d/archive" % home, follow_redirects=False)  # noqa: UP031
        assert r.status_code == 303
        with Session(engine) as s:
            assert s.get(Customer, home).archived_at is not None, "admin archive worked"
        ac.post("/customers/%d/unarchive" % home, follow_redirects=False)  # noqa: UP031
        with Session(engine) as s:
            assert s.get(Customer, home).archived_at is None, "admin unarchive worked"

        # --- /api: non-admin token is 403; admin token works ------------------------
        tr_uid = _user("apitr@x.test", role=Role.TRANSLATOR)
        _r, tr_tok = apikeys.create_key(tr_uid, "k", scopes="docs")
        api = TestClient(app)
        r = api.post("/api/customers/%d/archive" % home, headers={"X-Loc-Token": tr_tok})  # noqa: UP031
        assert r.status_code == 403, f"non-admin /api archive must be 403 ({r.status_code})"
        adm_uid = _user("apiadm@x.test", role=Role.ADMIN)
        _r, adm_tok = apikeys.create_key(adm_uid, "k", scopes="docs")
        r = api.post("/api/customers/%d/archive" % home, headers={"X-Loc-Token": adm_tok})  # noqa: UP031
        assert r.status_code == 200 and r.json().get("archived") is True
        api.post("/api/customers/%d/unarchive" % home, headers={"X-Loc-Token": adm_tok})  # noqa: UP031

        # --- foreign-customer caller -> 404 on the new by-id routes -----------------
        # A customer-bound admin (constructed directly; real bound accounts are never
        # admin) isolates the RING from the role gate — same technique the permanent
        # route sweep uses.
        with Session(engine) as s:
            bu = User(
                email="bound-adm@x.test",
                name="bound",
                role=Role.ADMIN,
                active=True,
                customer_id=home,
                password_hash=security.hash_password("pw-123456789"),
            )
            s.add(bu)
            s.commit()
        bc = TestClient(app)
        csrf_for(bc)
        bc.post(
            "/auth/login",
            data={"email": "bound-adm@x.test", "password": "pw-123456789", "next": "/"},
            follow_redirects=False,
        )
        csrf_for(bc)
        r = bc.post("/customers/%d/archive" % other, follow_redirects=False)  # noqa: UP031
        assert r.status_code == 404, (
            f"bound caller archiving a FOREIGN customer must 404 ({r.status_code})"
        )
        _r, btok = apikeys.create_key(
            _user("bound-api@x.test", role=Role.ADMIN, customer_id=home), "k", scopes="docs"
        )
        r = api.post("/api/customers/%d/archive" % other, headers={"X-Loc-Token": btok})  # noqa: UP031
        assert r.status_code == 404, "bound /api archive of a foreign customer must 404"
        with Session(engine) as s:
            assert s.get(Customer, other).archived_at is None, "foreign customer untouched"
        print("  RBAC: archive/unarchive admin-only on web + /api; foreign caller 404 OK")
    finally:
        _auth_off()


# ============================================================================
# FEATURE 2 — PromptTemplate customer attachment + isolation
# ============================================================================
def test_pt_create_with_attachment_all_surfaces():
    _auth_off()
    cid = _customer("PtAttach")
    # service: attach vs global
    with Session(engine) as s:
        pt_c = wf_service.create_prompt_template(s, "svc-cust", body="b", customer_id=cid)
        assert pt_c.scope == Scope.CUSTOMER and pt_c.customer_id == cid
        pt_g = wf_service.create_prompt_template(s, "svc-global", body="b")
        assert pt_g.scope == Scope.GLOBAL and pt_g.customer_id is None
    # web
    c = TestClient(app)
    c.post(
        "/templates/prompts",
        data={"name": "web-cust", "body": "b", "customer_id": str(cid)},
        follow_redirects=False,
    )
    with Session(engine) as s:
        pt = s.exec(select(PromptTemplate).where(PromptTemplate.name == "web-cust")).first()
        assert pt and pt.scope == Scope.CUSTOMER and pt.customer_id == cid
    # CLI create --customer
    rc, _out = _cli(
        [
            "workflow",
            "prompt",
            "create",
            "--name",
            "cli-cust",
            "--body",
            "b",
            "--customer",
            str(cid),
        ]
    )
    assert rc == 0
    rc, _out = _cli(["workflow", "prompt", "create", "--name", "cli-global", "--body", "b"])
    assert rc == 0
    with Session(engine) as s:
        pc = s.exec(select(PromptTemplate).where(PromptTemplate.name == "cli-cust")).first()
        pg = s.exec(select(PromptTemplate).where(PromptTemplate.name == "cli-global")).first()
        assert pc.scope == Scope.CUSTOMER and pc.customer_id == cid
        assert pg.scope == Scope.GLOBAL and pg.customer_id is None
    print("  PT create: attach vs global on service/web/CLI OK")


def test_pt_attach_archived_customer_refused():
    _auth_off()
    cid = _customer("PtArchived")
    with Session(engine) as s:
        customer_service.archive_customer(s, cid, actor="a@x.test")
        try:
            wf_service.create_prompt_template(s, "svc-arch", customer_id=cid)
            assert False, "attaching a PT to an archived customer must raise"
        except wf_service.WorkflowError:
            pass
    # web: refused with an error flash, no row
    c = TestClient(app)
    r = c.post(
        "/templates/prompts",
        data={"name": "web-arch", "customer_id": str(cid)},
        follow_redirects=False,
    )
    assert "err=" in r.headers.get("location", "")
    # CLI: EXIT_USAGE
    rc, _out = _cli(["workflow", "prompt", "create", "--name", "cli-arch", "--customer", str(cid)])
    assert rc == 2, f"CLI PT attach to archived customer must exit EXIT_USAGE ({rc})"
    with Session(engine) as s:
        assert (
            s.exec(
                select(PromptTemplate).where(PromptTemplate.name.in_(["web-arch", "cli-arch"]))
            ).first()
            is None
        ), "no PT row may be created for an archived customer"
    print("  PT create: archived customer refused on service/web/CLI OK")


def test_pt_bound_creator_forced_to_own_customer():
    _auth_off()
    home = _customer("PtBoundHome")
    other = _customer("PtBoundOther")
    with Session(engine) as s:
        # a foreign customer_id from a bound caller is refused
        try:
            wf_service.create_prompt_template(
                s, "bound-foreign", customer_id=other, bound_customer_id=home
            )
            assert False, "a bound caller attaching a FOREIGN customer must raise"
        except wf_service.WorkflowError:
            pass
        # a blank customer_id from a bound caller is FORCE-STAMPED to its own customer
        pt = wf_service.create_prompt_template(s, "bound-blank", bound_customer_id=home)
        assert pt.scope == Scope.CUSTOMER and pt.customer_id == home, (
            "a bound caller's PT must be forced to its own customer (never global)"
        )
        # its own customer_id is accepted
        pt2 = wf_service.create_prompt_template(
            s, "bound-own", customer_id=home, bound_customer_id=home
        )
        assert pt2.customer_id == home
    print("  PT bound-creator: foreign refused, blank forced to own, own accepted OK")


def test_pt_list_visibility_and_fail_pre_fix():
    _auth_off()
    home = _customer("PtVisHome")
    other = _customer("PtVisOther")
    with Session(engine) as s:
        g = wf_service.create_prompt_template(s, "vis-global")
        h = wf_service.create_prompt_template(s, "vis-home", customer_id=home)
        f = wf_service.create_prompt_template(s, "vis-foreign", customer_id=other)
        g_id, h_id, f_id = g.id, h.id, f.id

    with Session(engine) as s:
        # unbound staff (customer_id None) sees ALL
        all_ids = {t.id for t in wf_service.list_prompt_templates(s)}
        assert {g_id, h_id, f_id} <= all_ids, "unbound staff must see every template"
        # a bound caller (customer_id=home) sees GLOBAL + own only; foreign absent
        bound_ids = {t.id for t in wf_service.list_prompt_templates(s, customer_id=home)}
        assert g_id in bound_ids and h_id in bound_ids, "bound caller sees global + own"
        assert f_id not in bound_ids, "bound caller must NOT see a foreign customer's PT"

        # FAIL-PRE-FIX PROOF: the pre-fix list applied only the org filter (no customer
        # ring) — reproduce that inline and confirm it LEAKS the foreign PT to a bound
        # caller (the isolation gap the new customer_id ring closes).
        prefix_rows = list(s.exec(select(PromptTemplate)).all())  # org None = no filter
        prefix_ids = {t.id for t in prefix_rows}
        assert f_id in prefix_ids, (
            "pre-fix (org-only) list leaks the foreign customer's PT to a bound caller"
        )

    # web /templates for a customer-bound user shows GLOBAL + own only (foreign absent)
    _auth_on()
    try:
        with Session(engine) as s:
            bu = User(
                email="ptvis-bound@x.test",
                name="b",
                role=Role.LINGUIST,
                active=True,
                customer_id=home,
                password_hash=security.hash_password("pw-123456789"),
            )
            s.add(bu)
            s.commit()
        bc = TestClient(app)
        csrf_for(bc)
        bc.post(
            "/auth/login",
            data={"email": "ptvis-bound@x.test", "password": "pw-123456789", "next": "/"},
            follow_redirects=False,
        )
        body = bc.get("/templates").text
        assert "vis-home" in body and "vis-global" in body, "bound user sees global + own"
        assert "vis-foreign" not in body, "bound user's /templates must not leak foreign PT"
    finally:
        _auth_off()
    print(
        "  PT list: bound sees global+own, unbound sees all, web enforces; fail-pre-fix proven OK"
    )


def test_existing_global_pts_byte_unaffected():
    _auth_off()
    with Session(engine) as s:
        # a PT created with no customer is exactly today's GLOBAL/NULL shape
        pt = wf_service.create_prompt_template(s, "legacy-global", body="b")
        assert pt.scope == Scope.GLOBAL and pt.customer_id is None
        # it is visible in every list (unbound + bound), and stays GLOBAL
        assert any(t.id == pt.id for t in wf_service.list_prompt_templates(s))
        assert any(
            t.id == pt.id for t in wf_service.list_prompt_templates(s, customer_id=_customer("X"))
        )
        assert s.get(PromptTemplate, pt.id).scope == Scope.GLOBAL
    print("  PT existing GLOBAL rows byte-unaffected (scope/customer_id unchanged) OK")


# ============================================================================
# FEATURE 3 — 2026-07 review-fix regressions (3 adversarial-review findings)
# ============================================================================
def test_apply_template_from_archived_customer_clean_error():
    """FIX 1 (MED): a template's customer_id may be archived AFTER the template
    was saved (archive touches no existing template rows). apply_template must
    then raise a clean TemplateError (not a raw content.ArchivedCustomerError) so
    the CLI `template apply` (which only catches TemplateError) exits cleanly
    instead of crashing with a raw traceback — the reviewer's exact repro."""
    _auth_off()
    cid = _customer("TplArchCust")
    pid = _project("tpl-src-proj", customer_id=cid)
    with Session(engine) as s:
        tpl = template_service.save_as_template(s, pid, "tpl-arch-src", scope="customer")
        tpl_id = tpl.id
    with Session(engine) as s:
        customer_service.archive_customer(s, cid, actor="a@x.test")

    # service: apply_template must raise TemplateError, not content.ArchivedCustomerError
    with Session(engine) as s:
        try:
            template_service.apply_template(s, tpl_id, "tpl-arch-new-svc")
            assert False, "applying a template whose customer is archived must raise"
        except content.ArchivedCustomerError:
            assert False, (
                "apply_template must re-wrap ArchivedCustomerError as "
                "TemplateError, not let it escape raw"
            )
        except template_service.TemplateError:
            pass

    # CLI: `template apply` must exit EXIT_USAGE (2), not crash with a raw traceback.
    # Pre-fix this line itself raises (proving the crash); post-fix it returns cleanly.
    rc, _out = _cli(["template", "apply", str(tpl_id), "--name", "tpl-arch-new-cli"])
    assert rc == 2, f"CLI template apply for an archived-customer template must exit 2 ({rc})"

    # web from-template path stays clean too: redirect w/ error, no project row created
    c = TestClient(app)
    n_before = _project_count()
    r = c.post(
        "/projects/from-template",
        data={"template_id": str(tpl_id), "name": "tpl-arch-new-web"},
        follow_redirects=False,
    )
    assert r.status_code == 303 and "err=" in r.headers.get("location", ""), (
        "web from-template for an archived-customer template must redirect with an error"
    )
    assert _project_count() == n_before, "no project row may be created"
    print("  apply_template: archived-customer template refused cleanly on service/CLI/web OK")


def test_user_bind_refuses_archived_customer_but_unbind_allowed():
    """FIX 2 (LOW): a NEW user<->customer binding to an ARCHIVED customer is
    refused at the SERVICE (create_user + set_customer) — the binding picker
    already filters archived out, but a forged same-org POST must be refused too,
    not silently bound. Only NEW bindings/changes are refused: unbinding
    (customer_id=None/blank) and an EXISTING binding to a customer archived
    later both keep working untouched."""
    _auth_off()
    cid = _customer("UserBindArch")
    with Session(engine) as s:
        customer_service.archive_customer(s, cid, actor="a@x.test")

    # service: create_user binding a NEW user to an archived customer is refused
    try:
        security.create_user(
            "bindarch1@x.test", "b1", Role.TRANSLATOR, "pw-123456789", customer_id=cid
        )
        assert False, "create_user binding a new user to an archived customer must raise"
    except ValueError:
        pass
    assert security.get_by_email("bindarch1@x.test") is None, (
        "a refused create_user must not leave a user row behind"
    )

    # service: set_customer binding an EXISTING (unbound) user to an archived
    # customer is refused, with a clean reason (not a silent bind, not a crash)
    uid = _user("bindarch2@x.test", role=Role.TRANSLATOR)
    with Session(engine) as s:
        res = security.set_customer(s, uid, str(cid), actor="a@x.test", org_id=None)
        assert res["ok"] is False and res.get("reason") == "archived_customer"
        assert s.get(User, uid).customer_id is None, "a refused bind must not touch the row"

    # web: a forged same-org POST to /users/{uid}/customer with the archived id
    # must be refused too (the picker filtering it out is not the only guard)
    _auth_on()
    try:
        security.create_user("bindarch-admin@x.test", "adm", Role.ADMIN, "pw-123456789")
        wc = TestClient(app)
        csrf_for(wc)
        wc.post(
            "/auth/login",
            data={"email": "bindarch-admin@x.test", "password": "pw-123456789", "next": "/"},
            follow_redirects=False,
        )
        csrf_for(wc)
        r = wc.post(
            "/users/%d/customer" % uid,  # noqa: UP031
            data={"customer_id": str(cid)},
            follow_redirects=False,  # noqa: RUF100, UP031
        )
        assert r.status_code == 303 and "error=" in r.headers.get("location", ""), (
            "a forged bind POST to an archived customer must redirect with an error"
        )
        with Session(engine) as s:
            assert s.get(User, uid).customer_id is None, "the forged bind must not bind"
    finally:
        _auth_off()

    # an EXISTING binding survives the customer being archived LATER (untouched)
    active_cid = _customer("UserBindActiveThenArch")
    uid2 = _user("bindarch3@x.test", role=Role.TRANSLATOR, customer_id=active_cid)
    with Session(engine) as s:
        customer_service.archive_customer(s, active_cid, actor="a@x.test")
        assert s.get(User, uid2).customer_id == active_cid, (
            "an existing binding must survive the customer being archived later"
        )

    # unbinding (customer_id="") a user FROM an archived customer must still work
    with Session(engine) as s:
        res = security.set_customer(s, uid2, "", actor="a@x.test", org_id=None)
        assert res["ok"] is True and res["new"] is None, (
            "unbinding a user from an archived customer must be allowed"
        )
    print(
        "  user-binding: NEW bind to an archived customer refused (service+web); "
        "unbind + an existing binding stay unaffected OK"
    )


def test_report_picker_labels_archived_customer():
    """FIX 3 (ruling): the report-builder picker deliberately KEEPS archived
    customers listed (reports are retrospective over retained data, unlike the
    user-binding picker which filters them out for NEW associations) but labels
    the row " (archived)" so it reads clearly in the dropdown."""
    _auth_off()
    cid = _customer("ReportArchived")
    _project("report-arch-proj", customer_id=cid)
    with Session(engine) as s:
        customer_service.archive_customer(s, cid, actor="a@x.test")
    c = TestClient(app)
    body = c.get("/reports").text
    assert "ReportArchived (archived)" in body, (
        "an archived customer must still appear in the report picker, labeled"
    )
    print("  report picker: archived customer stays listed, labeled ' (archived)' OK")


def main():
    # Feature 1
    test_model_and_migration()
    test_archive_unarchive_service_and_audit()
    test_archive_removes_from_pickers_but_existing_project_works()
    test_archived_customer_cannot_be_chosen_for_new_project()
    test_archived_customer_api_returns_400()
    test_archive_rbac_admin_only_and_foreign_404()
    # Feature 2
    test_pt_create_with_attachment_all_surfaces()
    test_pt_attach_archived_customer_refused()
    test_pt_bound_creator_forced_to_own_customer()
    test_pt_list_visibility_and_fail_pre_fix()
    test_existing_global_pts_byte_unaffected()
    # Feature 3 (review-fix regressions)
    test_apply_template_from_archived_customer_clean_error()
    test_user_bind_refuses_archived_customer_but_unbind_allowed()
    test_report_picker_labels_archived_customer()
    print("CUSTOMER ARCHIVE PT ATTACH OK")


if __name__ == "__main__":
    main()
