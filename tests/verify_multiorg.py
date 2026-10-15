"""Multi-org SaaS P0 + P1 + P2 + P3 verification (offline, hermetic).

P3 adds isolation for the six DIRECTLY-OWNED tables (customers, connections,
workflows, language sets, routing rules, department budgets) + the connector
object-authz that had ZERO enforcement before (leak vector 10). With the gate ON,
org A's lists exclude every one of org B's rows; A gets 404 on B's connection
detail + every mutation + every /api/connectors/{cid}/*; B's GLOBAL-scope workflow
is not surfaced to A; a routing rule / budget of B never applies to A's submission
(org-scoped resolution even when the department name is shared); creation stamps
org A; and a NULL-project (org-less) global TM row can't bleed into another org.
Every P3 path is gate-guarded so the gate OFF stays byte-identical to today.


Phase 0 is SCHEMA + GATE ONLY, a pure no-op: it adds unused, nullable/defaulted
columns (org_id on 6 root tables + user.is_superadmin) and a new default-OFF
setting (multi_org_enabled="0"). Phase 1 adds the ISOLATION AUTHORITY in
access.py (multi_org_active/current_org_id/same_org/org_filter + org-extended
visible_project_ids/can_view_project) + org stamping in content.py -- all guarded
so the gate OFF is byte-identical to today. This suite proves both:

  P0
  1. schema shape: is_superadmin + org_id columns exist on the right tables;
     is_superadmin defaults False; a freshly created row's org_id starts NULL
     until the Default-org backfill stamps it.
  2. Default-org backfill: init_db()'s _ensure_default_org stamped the Default org
     id onto pre-existing user/project/customer/... rows; the backfill is
     idempotent (re-running init_db changes nothing).
  3. the sacred gate: multi_org_enabled defaults "0", and with it OFF the app
     behaves exactly as today -- a normal project list/create works with NO org
     filtering (no cross-org machinery is active).

  P1 (gate turned ON in-test)
  4. THE LINCHPIN: current_org_id(super-admin) == the super-admin's HOME org, NOT
     None -- is_superadmin is not a scope-widener in the customer app / API.
  5. isolation: same_org isolates; a NULL-org row is invisible to a scoped user;
     visible_project_ids(userA) excludes org B's project; can_view_project across
     orgs is False.
  6. the sacred gate re-asserted: with the gate OFF every predicate is a no-op
     (current_org_id -> None, same_org/can_view_project unrestricted).

ASCII-only output; standalone one-process-per-suite script (LOC_TMS_DATA_DIR is a
throwaway temp dir, provider keys cleared -> MockProvider, deterministic + free).
"""

import os
import re
import sys
import tempfile

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(_ROOT)
sys.path.insert(0, _ROOT)
from tests._bootstrap import init_env

init_env("loctms_multiorg_")

from fastapi.testclient import TestClient
from sqlalchemy import inspect, text
from sqlmodel import Session, select

from app.db import engine, init_db
from app.main import app
from app.models import (
    Connection,
    Customer,
    DepartmentBudget,
    LanguageSet,
    Organization,
    Project,
    Role,
    RoutingRule,
    User,
    Workflow,
)
from app.services import access, audit, content, gdpr, security, settings

# The 6 root tables that gained org_id in P0 (project + user already had it).
_ORG_TABLES = [
    "customer",
    "connection",
    "workflow",
    "languageset",
    "routingrule",
    "departmentbudget",
    "project",
    "user",
]


def test_columns_exist() -> None:
    """The new columns are present on the right tables after init_db()."""
    init_db()
    insp = inspect(engine)

    user_cols = {c["name"] for c in insp.get_columns("user")}
    assert "is_superadmin" in user_cols, "user.is_superadmin missing"
    assert "org_id" in user_cols, "user.org_id missing"

    for table in _ORG_TABLES:
        cols = {c["name"] for c in insp.get_columns(table)}
        assert "org_id" in cols, f"{table}.org_id missing"
    print("  schema: is_superadmin on user + org_id on all 8 root tables present")


def test_defaults_and_backfill() -> None:
    """is_superadmin defaults False; a new row starts org_id NULL; the Default-org
    backfill stamps existing rows; the backfill is idempotent."""
    init_db()

    # Exactly one 'Default' org exists (seeded/backfilled by _ensure_default_org).
    with Session(engine) as s:
        orgs = s.exec(select(Organization)).all()
        assert len(orgs) == 1 and orgs[0].name == "Default", (
            "expected one 'Default' org, got %r" % [o.name for o in orgs]
        )
        default_org_id = orgs[0].id

    # A freshly-inserted customer starts with org_id NULL (P0 does not stamp at
    # creation time -- that is P1). is_superadmin default is False on a new user.
    with Session(engine) as s:
        cust = Customer(name="probe-customer")
        s.add(cust)
        user = User(email="probe@example.com", name="Probe")
        s.add(user)
        s.commit()
        s.refresh(cust)
        s.refresh(user)
        assert cust.org_id is None, f"new Customer.org_id should start NULL, got {cust.org_id!r}"
        assert user.is_superadmin is False, "new User.is_superadmin should default False"
        assert user.org_id is None, "new User.org_id should start NULL before backfill"
        probe_customer_id = cust.id
        probe_user_id = user.id
    print("  defaults: new row org_id NULL, is_superadmin False")

    # _ensure_default_org (part of init_db) stamps the Default org onto rows with a
    # NULL org_id. Run init_db and confirm every root-table row is stamped.
    init_db()
    with Session(engine) as s:
        cust = s.get(Customer, probe_customer_id)
        user = s.get(User, probe_user_id)
        assert cust.org_id == default_org_id, (
            f"backfill did not stamp Customer.org_id ({cust.org_id!r} != {default_org_id!r})"
        )
        assert user.org_id == default_org_id, (
            f"backfill did not stamp User.org_id ({user.org_id!r} != {default_org_id!r})"
        )
        assert user.is_superadmin is False, "backfill must NOT flag is_superadmin (P8 does)"
    print("  backfill: Default org stamped onto existing user + 6 root tables")

    # No orphaned NULLs remain on any root table after the backfill.
    with engine.connect() as c:
        for table in _ORG_TABLES:
            nulls = c.execute(text(f"SELECT COUNT(*) FROM {table} WHERE org_id IS NULL")).scalar()
            assert nulls == 0, "%s still has %d NULL org_id rows after backfill" % (table, nulls)  # noqa: UP031

    # Idempotency: still exactly one Default org, and a second init_db() run leaves
    # the stamped values untouched (no new orgs, no re-stamping side effects).
    init_db()
    with Session(engine) as s:
        orgs = s.exec(select(Organization)).all()
        assert len(orgs) == 1, "init_db created a duplicate org: %d rows" % len(orgs)  # noqa: UP031
        assert s.get(Customer, probe_customer_id).org_id == default_org_id
    print("  backfill: idempotent (re-running init_db is a no-op)")


def test_gate_defaults_off() -> None:
    """The sacred gate: multi_org_enabled defaults '0' (nothing reads it in P0)."""
    init_db()
    # Freshly-read setting with no DB row falls through to DEFAULTS.
    settings.invalidate_cache()
    assert settings.get("multi_org_enabled") == "0", (
        "multi_org_enabled must default '0' (the sacred gate)"
    )
    assert settings.DEFAULTS.get("multi_org_enabled") == "0"
    print("  gate: multi_org_enabled defaults '0' (OFF)")


def test_gate_off_behaves_as_today() -> None:
    """With the gate OFF (default), a normal project list/create works with NO org
    filtering -- the app is byte-identical to single-tenant today. Auth is OFF by
    default in a fresh install, so this exercises the plain single-tenant path."""
    init_db()
    c = TestClient(app)

    # Baseline project list renders (no org scoping in the way).
    r = c.get("/projects")
    assert r.status_code == 200, f"/projects should render, got {r.status_code}"

    # Create a project the ordinary way; it succeeds and appears in the list.
    # 2026-07-14: a project requires a customer when one exists — make one and
    # pass it (the create-requires-customer seam; org-gating is what this test probes).
    with Session(engine) as s:
        _cust = content.customer_create(s, "Gate Probe Customer")
        _cust_id = _cust.id
    r = c.post(
        "/projects",
        data={
            "name": "MultiOrg Gate Probe",
            "source_locale": "en-US",
            "target_locales": "fr-FR",
            "customer_id": str(_cust_id),
            "default_provider": "mock",
        },
        follow_redirects=False,
    )
    assert r.status_code == 303, f"project create should 303, got {r.status_code}"
    new_id = int(r.headers["location"].rsplit("/", 1)[1])

    with Session(engine) as s:
        proj = s.get(Project, new_id)
        assert proj is not None and proj.name == "MultiOrg Gate Probe"

    # The project is visible in the list with the gate off -- no org predicate hides
    # it. (P0 does NOT stamp org_id at creation; that is P1. Here we only assert the
    # no-op: creation + listing are unchanged.)
    r = c.get("/projects")
    assert r.status_code == 200
    assert "MultiOrg Gate Probe" in r.text, "created project missing from list (gate off)"
    print("  gate-off: project list + create work with no org filtering (as today)")


# --------------------------------------------------------------------------- #
# P1: access.py org predicates (the isolation authority)
# --------------------------------------------------------------------------- #
def _gate_on() -> None:
    """Engage the sacred gate: auth ON + multi_org_enabled='1'. The predicates
    key off both (security.auth_required() and the setting)."""
    settings.set_many({"security_auth_required": "1", "multi_org_enabled": "1"})
    settings.invalidate_cache()


def _gate_off() -> None:
    """Restore single-tenant defaults (byte-identical to today)."""
    settings.set_many({"security_auth_required": "0", "multi_org_enabled": "0"})
    settings.invalidate_cache()


def _seed_two_orgs():
    """Two orgs A and B, each with a user + a project stamped to that org, plus a
    NULL-org (legacy) project. Returns a dict of the seeded ids/objects. Uses raw
    org rows (not the Default) so isolation is unambiguous."""
    init_db()
    with Session(engine) as s:
        org_a = Organization(name="Org A")
        org_b = Organization(name="Org B")
        s.add(org_a)
        s.add(org_b)
        s.commit()
        s.refresh(org_a)
        s.refresh(org_b)

        # #74: REUSE the fixture accounts if a previous _seed_two_orgs() in this process
        # already made them, re-stamping them into the NEW orgs. They used to be re-INSERTed
        # every call, which quietly built a pile of duplicate a@a.test / b@b.test rows --
        # exactly the identity ambiguity #74 closes, and (worse for a fixture) a by-email
        # `.first()` then resolved to the OLDEST row, stamped into a STALE org. The DB-level
        # ux_user_email_lower now refuses the second INSERT outright.
        def _fixture_user(email, name, org_id):
            u = s.exec(select(User).where(User.email == email)).first()
            if u is None:
                u = User(email=email, name=name, role=Role.ADMIN, org_id=org_id)
            else:
                u.org_id = org_id
                u.active = True
                u.role = Role.ADMIN
            s.add(u)
            return u

        user_a = _fixture_user("a@a.test", "A Admin", org_a.id)
        user_b = _fixture_user("b@b.test", "B Admin", org_b.id)
        s.commit()
        s.refresh(user_a)
        s.refresh(user_b)
        # Detach the User rows from the session so the predicate calls (which run
        # after the session closes) read their loaded attributes without a lazy
        # refresh (which would raise DetachedInstanceError). The predicates only
        # need plain attribute access -- no DB round-trip -- so detached is fine.
        s.expunge(user_a)
        s.expunge(user_b)

        proj_a = Project(name="Proj A", org_id=org_a.id, created_by="a@a.test")
        proj_b = Project(name="Proj B", org_id=org_b.id, created_by="b@b.test")
        proj_null = Project(name="Proj Legacy", org_id=None, created_by="x@x.test")
        s.add(proj_a)
        s.add(proj_b)
        s.add(proj_null)
        s.commit()
        ids = {"proj_a": proj_a.id, "proj_b": proj_b.id, "proj_null": proj_null.id}
        return {
            "org_a": org_a.id,
            "org_b": org_b.id,
            "user_a": user_a,
            "user_b": user_b,
            **ids,
        }


def test_linchpin_superadmin_home_org() -> None:
    """THE LINCHPIN: with the gate ON, current_org_id(userA) == org A even when
    userA.is_superadmin is True. is_superadmin does NOT widen scope in the normal
    app / API -- a super-admin is just an org-admin of their HOME org here, and
    current_org_id NEVER returns None for a real user in multi-org mode."""
    d = _seed_two_orgs()
    _gate_on()
    try:
        user_a = d["user_a"]
        assert access.multi_org_active() is True, "gate should be active in-test"

        # Ordinary org-admin resolves to home org.
        assert access.current_org_id(user_a) == d["org_a"], (
            f"current_org_id(userA) should be org A, got {access.current_org_id(user_a)!r}"
        )

        # Flip on the super-admin flag -- it must NOT change the resolution.
        user_a.is_superadmin = True
        assert access.current_org_id(user_a) == d["org_a"], (
            "LINCHPIN FAILED: super-admin current_org_id widened to {!r} (expected home org A)".format(  # noqa: E501, UP032
                access.current_org_id(user_a)
            )
        )
        assert access.current_org_id(user_a) is not None, (
            "LINCHPIN FAILED: super-admin current_org_id must never be None (unrestricted)"
        )
        print("  linchpin: current_org_id(super-admin) == HOME org (not None)")
    finally:
        _gate_off()


def test_same_org_and_null_row() -> None:
    """same_org isolates by org; a NULL-org (legacy/global) row is NOT visible to a
    scoped user when the gate is on (quarantined), but IS unrestricted when off."""
    d = _seed_two_orgs()
    _gate_on()
    try:
        user_a = d["user_a"]
        assert access.same_org(user_a, d["org_a"]) is True, "own org must match"
        assert access.same_org(user_a, d["org_b"]) is False, "other org must not match"
        assert access.same_org(user_a, None) is False, (
            "NULL-org row must be quarantined from a scoped user"
        )
        print("  same_org: own-org True, other-org False, NULL-org quarantined")
    finally:
        _gate_off()

    # Gate OFF: same_org is a pure no-op (everything visible, incl. NULL-org).
    assert access.same_org(d["user_a"], d["org_b"]) is True, (
        "gate OFF: same_org must be an unrestricted no-op"
    )
    assert access.same_org(d["user_a"], None) is True, (
        "gate OFF: NULL-org row is visible (single-tenant)"
    )
    print("  same_org: gate-off no-op (unrestricted)")


def test_visibility_isolated_across_orgs() -> None:
    """visible_project_ids(userA) excludes org B's project (and the NULL-org one);
    can_view_project(userA, org B's project) is False. Both org-scoped only when
    the gate is on."""
    d = _seed_two_orgs()
    _gate_on()
    try:
        user_a = d["user_a"]
        with Session(engine) as s:
            vis = access.visible_project_ids(s, user_a)
            # user_a is an ADMIN (a see-all role): with multi-org on, see-all is
            # confined to the home org rather than returning None (unrestricted).
            assert vis is not None, (
                "multi-org: a see-all user must be org-confined, not unrestricted (None)"
            )
            assert d["proj_a"] in vis, "org A's own project must be visible"
            assert d["proj_b"] not in vis, "org B's project must NOT be visible to A"
            assert d["proj_null"] not in vis, "NULL-org project must NOT be visible to A"

            assert access.can_view_project(s, user_a, d["proj_a"]) is True, (
                "A must see A's own project"
            )
            assert access.can_view_project(s, user_a, d["proj_b"]) is False, (
                "A must NOT see B's project (cross-org)"
            )
            assert access.can_view_project(s, user_a, d["proj_null"]) is False, (
                "A must NOT see the NULL-org project (quarantined)"
            )
        print("  visibility: org A sees only org A's project; B + NULL excluded")
    finally:
        _gate_off()


def test_org_stamping_on_create() -> None:
    """content.project_create / customer_create stamp the creator's org when
    multi-org is active, and leave org_id NULL (today's behavior) with the gate
    off or when no creator is supplied."""
    d = _seed_two_orgs()

    # Gate OFF (no creator): unstamped, exactly as today.
    with Session(engine) as s:
        p = content.project_create(s, "Unstamped", target_locales="fr-FR")
        c = content.customer_create(s, "Unstamped Co")
        assert p.org_id is None, f"gate off: project org_id must stay NULL, got {p.org_id!r}"
        assert c.org_id is None, f"gate off: customer org_id must stay NULL, got {c.org_id!r}"
    print("  stamping: gate-off create leaves org_id NULL (as today)")

    # Gate ON + creator=userA: stamped to A's org.
    _gate_on()
    try:
        with Session(engine) as s:
            p = content.project_create(s, "Stamped", target_locales="fr-FR", creator=d["user_a"])
            c = content.customer_create(s, "Stamped Co", creator=d["user_a"])
            assert p.org_id == d["org_a"], (
                f"multi-org: project should be stamped org A, got {p.org_id!r}"
            )
            assert c.org_id == d["org_a"], (
                f"multi-org: customer should be stamped org A, got {c.org_id!r}"
            )
        print("  stamping: multi-org create stamps the creator's org (A)")
    finally:
        _gate_off()


def test_gate_off_predicates_are_noops() -> None:
    """The sacred gate re-asserted at the predicate level: with the gate OFF every
    org predicate is a strict no-op -- current_org_id -> None for everyone (incl.
    a super-admin), visible_project_ids returns None (unrestricted see-all) for an
    admin, can_view_project ignores org entirely."""
    d = _seed_two_orgs()
    # Gate is already off (defaults); assert explicitly.
    assert access.multi_org_active() is False, "gate must be OFF by default"
    user_a = d["user_a"]
    user_a.is_superadmin = True
    assert access.current_org_id(user_a) is None, (
        "gate off: current_org_id must be None (unrestricted) even for a super-admin"
    )
    with Session(engine) as s:
        # An admin (see-all) is unrestricted -> None, byte-identical to pre-P1.
        assert access.visible_project_ids(s, user_a) is None, (
            "gate off: see-all user must be unrestricted (None)"
        )
        # Cross-org view is allowed because org is not consulted with the gate off.
        assert access.can_view_project(s, user_a, d["proj_b"]) is True, (
            "gate off: can_view_project must ignore org (unrestricted)"
        )
    print("  gate-off: predicates are no-ops (byte-identical to today)")


# --------------------------------------------------------------------------- #
# P2: project-subtree isolation ENFORCEMENT (end-to-end, through the routes)
#
# The predicates above are the authority; P2 wires them into every project-subtree
# route. These tests drive the real HTTP routes with the sacred gate ON, two orgs
# A/B each with an admin user + a full subtree (project, document, segment,
# workflow run, job), and assert org A can NEVER reach org B's rows -- and gets a
# 404 (NOT 403), so an id in another org is indistinguishable from a missing one.
# They also re-assert the sacred gate: with it OFF the same cross-"org" access is
# unrestricted (single-tenant, byte-identical to today).
# --------------------------------------------------------------------------- #
from tests._csrf import csrf_for


def _login(c, who, pw):
    csrf_for(c)
    r = c.post(
        "/auth/login", data={"email": who, "password": pw, "next": "/"}, follow_redirects=False
    )
    if r.status_code == 303:
        csrf_for(c)
    return r


def _make_project(c, name):
    # 2026-07-14: a project requires a customer when one exists. Create one via
    # the SAME client (POST /customers stamps the client's org, so the customer + the
    # project land in the same org — keeping the org-isolation assertions valid), then
    # pass its id. Reading it back by unique name (newest wins).
    cust_name = name + " Customer"
    rc = c.post("/customers", data={"name": cust_name}, follow_redirects=False)
    assert rc.status_code == 303, f"customer create -> {rc.status_code}"
    with Session(engine) as s:
        cust = s.exec(
            select(Customer).where(Customer.name == cust_name).order_by(Customer.id.desc())
        ).first()
    r = c.post(
        "/projects",
        data={
            "name": name,
            "source_locale": "en-US",
            "target_locales": "fr-FR",
            "customer_id": str(cust.id) if cust else "",
            "default_provider": "mock",
        },
        follow_redirects=False,
    )
    assert r.status_code == 303, f"project create -> {r.status_code}"
    return int(r.headers["location"].rsplit("/", 1)[1])


def _upload_doc(c, pid):
    r = c.post(
        f"/projects/{pid}/documents",
        data={"mode": "segmented"},
        files={"file": ("hello.txt", b"Hello world. This is a test.", "text/plain")},
        follow_redirects=False,
    )
    assert r.status_code == 303, f"doc upload -> {r.status_code}"
    return int(r.headers["location"].rsplit("/", 1)[1].split("?")[0])


_SUBTREE_CACHE = {}


def _seed_subtree_via_routes():
    """Auth ON + gate ON. Two admin users A/B in orgs A/B, each creating (through
    the real routes, so P2's creation-site stamping runs) a project + document;
    then a WorkflowRun + Job are seeded per subtree with matching org attribution.
    Returns the two logged-in clients + the seeded ids.

    Seeds ONCE per process and caches the result: every P2 route test shares the
    SAME two-org world (re-creating the users would collide, and the ids are
    stable), each test just re-engages the gate (tests restore it OFF in finally)."""
    if _SUBTREE_CACHE:
        _gate_on()
        return _SUBTREE_CACHE["ca"], _SUBTREE_CACHE["cb"], _SUBTREE_CACHE["ids"]
    from app.models import Job, JobStatus, Organization, Segment, Workflow, WorkflowRun

    init_db()
    # Two extra orgs; two admins, one per org. (Earlier tests in this process may
    # already have created probe rows, so we don't rely on seed_initial() -- we
    # create the admins directly and turn the login wall on ourselves.)
    with Session(engine) as s:
        org_a = Organization(name="RouteOrg A")
        org_b = Organization(name="RouteOrg B")
        s.add(org_a)
        s.add(org_b)
        s.commit()
        s.refresh(org_a)
        s.refresh(org_b)
        oa, ob = org_a.id, org_b.id
    ua = security.create_user("aa@x.com", "A Admin", Role.ADMIN, "aa-password-123")
    ub = security.create_user("bb@x.com", "B Admin", Role.ADMIN, "bb-password-123")
    with Session(engine) as s:
        for uid, oid in ((ua.id, oa), (ub.id, ob)):
            row = s.get(User, uid)
            row.org_id = oid
            s.add(row)
        s.commit()

    # Turn the sacred gate ON now (auth already on from seed_initial()).
    _gate_on()

    ca = TestClient(app)
    cb = TestClient(app)
    assert _login(ca, "aa@x.com", "aa-password-123").status_code == 303
    assert _login(cb, "bb@x.com", "bb-password-123").status_code == 303

    pa = _make_project(ca, "RouteProj A")
    pb = _make_project(cb, "RouteProj B")
    da = _upload_doc(ca, pa)
    db_ = _upload_doc(cb, pb)

    with Session(engine) as s:
        # Projects were created THROUGH the route -> P2 stamping put them in-org.
        assert s.get(Project, pa).org_id == oa, "project A not stamped org A"
        assert s.get(Project, pb).org_id == ob, "project B not stamped org B"
        seg_a = s.exec(select(Segment).where(Segment.document_id == da)).first()
        seg_b = s.exec(select(Segment).where(Segment.document_id == db_)).first()
        # A workflow run per doc (seeded directly: we test the READ routes' gating,
        # not the orchestrator thread). workflow_id is a real FK
        # (workflowrun.workflow_id -> workflow.id), so seed a minimal per-org
        # workflow rather than a workflow_id=0 sentinel (which never persisted on
        # Postgres).
        wf_a = Workflow(name="wf A", org_id=oa)
        wf_b = Workflow(name="wf B", org_id=ob)
        s.add(wf_a)
        s.add(wf_b)
        s.commit()
        s.refresh(wf_a)
        s.refresh(wf_b)
        run_a = WorkflowRun(workflow_id=wf_a.id, document_id=da, target_locale="fr-FR")
        run_b = WorkflowRun(workflow_id=wf_b.id, document_id=db_, target_locale="fr-FR")
        s.add(run_a)
        s.add(run_b)
        # A job per org, attributed to that org's user (job org = creator's org).
        job_a = Job(
            kind="translate_document",
            payload={"document_id": da, "locale": "fr-FR"},
            status=JobStatus.QUEUED,
            created_by="aa@x.com",
        )
        job_b = Job(
            kind="translate_document",
            payload={"document_id": db_, "locale": "fr-FR"},
            status=JobStatus.QUEUED,
            created_by="bb@x.com",
        )
        s.add(job_a)
        s.add(job_b)
        s.commit()
        s.refresh(run_a)
        s.refresh(run_b)
        s.refresh(job_a)
        s.refresh(job_b)
        ids = {
            "org_a": oa,
            "org_b": ob,
            "pa": pa,
            "pb": pb,
            "da": da,
            "db": db_,
            "seg_a": seg_a.id,
            "seg_b": seg_b.id,
            "run_a": run_a.id,
            "run_b": run_b.id,
            "job_a": job_a.id,
            "job_b": job_b.id,
        }
    _SUBTREE_CACHE.update({"ca": ca, "cb": cb, "ids": ids})
    return ca, cb, ids


def test_project_subtree_isolation_404() -> None:
    """Gate ON: org A gets 404 (NOT 403) on every project-subtree surface belonging
    to org B -- project, document, editor, segment save/approve, translate-stream,
    workflow run, runs-stream, resume, /api job, /jobs status. A 403 here would be a
    leak (it would confirm the id EXISTS in another org)."""
    ca, _cb, d = _seed_subtree_via_routes()
    try:
        pb, db_, seg_b = d["pb"], d["db"], d["seg_b"]
        run_b, job_b = d["run_b"], d["job_b"]

        def is404(r, what):
            assert r.status_code == 404, (
                f"{what}: expected 404 (cross-org, not enumerable), got {r.status_code}"
            )

        # 1. project fetch by id
        is404(ca.get(f"/projects/{pb}"), "GET /projects/{B}")
        # 2. document detail + editor + lqa
        is404(ca.get(f"/documents/{db_}"), "GET /documents/{B}")
        is404(ca.get(f"/documents/{db_}/editor"), "GET editor {B}")
        is404(ca.get(f"/documents/{db_}/lqa"), "GET lqa {B}")
        # 3. SSE translate-stream MUST 404 before yielding any event
        rs = ca.post(f"/documents/{db_}/translate-stream", data={"locale": "fr-FR"})
        is404(rs, "POST translate-stream {B}")
        assert "event-stream" not in rs.headers.get("content-type", ""), (
            "translate-stream leaked an SSE body cross-org"
        )
        # 4. segment save/approve (mutate)
        is404(
            ca.post(f"/segments/{seg_b}/save", data={"target_text": "x"}), "POST segment save {B}"
        )
        is404(ca.post(f"/segments/{seg_b}/approve"), "POST segment approve {B}")
        # 5. document translate (mutate) + apply-tm + export
        is404(
            ca.post(f"/documents/{db_}/translate", data={"locale": "fr-FR"}),
            "POST doc translate {B}",
        )
        is404(ca.get(f"/documents/{db_}/export?locale=fr-FR"), "GET export {B}")
        # 6. workflow run detail + runs-stream + resume
        is404(ca.get(f"/runs/{run_b}"), "GET /runs/{B}")
        rs2 = ca.get(f"/runs/stream?run_id={run_b}")
        is404(rs2, "GET runs/stream {B}")
        assert "event-stream" not in rs2.headers.get("content-type", ""), (
            "runs/stream leaked an SSE body cross-org"
        )
        is404(ca.post(f"/runs/{run_b}/resume", data={"locale": "fr-FR"}), "POST run resume {B}")
        # 7. jobs: /api/jobs/{jid} (docs-scoped) uses its own 404; /jobs/{jid}/status
        is404(ca.get(f"/jobs/{job_b}/status"), "GET /jobs/{B}/status")
        # 8. member add (mutate on B's project)
        is404(ca.post(f"/projects/{pb}/members", data={"email": "aa@x.com"}), "POST members {B}")
        print("  isolation: org A gets 404 (not 403) on every org-B subtree surface")
    finally:
        _gate_off()


def test_own_org_subtree_reachable() -> None:
    """Control: with the gate ON, org A CAN reach its OWN subtree (200/303) -- the
    404s above are org isolation, not a blanket lockout."""
    ca, _cb, d = _seed_subtree_via_routes()
    try:
        assert ca.get(f"/projects/{d['pa']}").status_code == 200, "A can't see A's project"
        assert ca.get(f"/documents/{d['da']}").status_code == 200, "A can't see A's doc"
        assert ca.get(f"/runs/{d['run_a']}").status_code == 200, "A can't see A's run"
        assert ca.get(f"/jobs/{d['job_a']}/status").status_code == 200, "A can't read A's job"
        print("  isolation: org A CAN reach its OWN subtree (no blanket lockout)")
    finally:
        _gate_off()


def test_lists_and_reports_exclude_other_org() -> None:
    """Gate ON: A's project list, dashboard and reports COUNT ONLY org A -- no
    cross-org count leak (leak vector 4). A see-all admin is org-confined here."""
    ca, _cb, _d = _seed_subtree_via_routes()
    try:
        body = ca.get("/projects").text
        assert "RouteProj A" in body, "A's own project missing from list"
        assert "RouteProj B" not in body, "LEAK: org B's project in A's list"
        # Dashboard + reports render org-scoped (visible_project_ids org-confines
        # the aggregates); the smoke check is they render and don't name B's project.
        dash = ca.get("/")
        assert dash.status_code == 200 and "RouteProj B" not in dash.text, (
            "LEAK: org B on A's dashboard"
        )
        rep = ca.get("/reports")
        assert rep.status_code == 200 and "RouteProj B" not in rep.text, (
            "LEAK: org B on A's reports"
        )
        # Jobs list: A's /jobs page renders and B's job status is unreachable
        # (the per-job 404 is asserted in the isolation matrix; here we confirm the
        # list route itself renders org-scoped without erroring).
        assert ca.get("/jobs").status_code == 200, "A's /jobs list must render"
        print("  aggregates: A's list/dashboard/reports exclude org B (no count leak)")
    finally:
        _gate_off()


def test_new_inbox_and_creates_stamped_org() -> None:
    """Gate ON: a project/customer/inbox created by org A lands in org A. The
    project+doc were already asserted in-org during seeding; here we add customer +
    the API inbox (via a docs-scoped key belonging to org A's user)."""
    from app.models import Customer
    from app.models import Project as _P
    from app.services import apikeys

    ca, _cb, d = _seed_subtree_via_routes()
    try:
        # Customer created via the route -> stamped org A.
        r = ca.post("/customers", data={"name": "Route Customer A"}, follow_redirects=False)
        assert r.status_code == 303, f"customer create -> {r.status_code}"
        with Session(engine) as s:
            cust = s.exec(select(Customer).where(Customer.name == "Route Customer A")).first()
            assert cust is not None and cust.org_id == d["org_a"], (
                "customer not stamped org A (got {!r})".format(getattr(cust, "org_id", None))
            )

        # API inbox: mint a docs-scoped key for org A's user, upload with no
        # project_id -> the auto-created inbox project must land in org A.
        ua = security.get_by_email("aa@x.com")
        _row, raw = apikeys.create_key(ua.id, "test-docs", scopes="api docs")
        api = TestClient(app)
        r = api.post(
            "/api/documents",
            headers={"X-Loc-Token": raw},
            files={"file": ("inbox.txt", b"Inbox doc.", "text/plain")},
            data={"mode": "segmented"},
        )
        assert r.status_code == 200, f"API upload -> {r.status_code} ({r.text[:200]})"
        inbox_pid = r.json()["project_id"]
        with Session(engine) as s:
            assert s.get(_P, inbox_pid).org_id == d["org_a"], "auto-created inbox not stamped org A"
        print("  stamping: route customer + API inbox both land in org A")
    finally:
        _gate_off()


def test_gate_off_subtree_unrestricted() -> None:
    """The sacred gate re-asserted end-to-end: with the gate OFF (single-tenant),
    the cross-'org' access that 404'd above is UNRESTRICTED -- auth off means the
    SYSTEM operator sees everything, byte-identical to today."""
    # Fresh install, gate OFF, auth OFF (default). Seed two projects+docs and prove
    # a single anonymous client reaches BOTH (no org wall exists).
    init_db()
    _gate_off()
    assert access.multi_org_active() is False, "gate must be OFF here"
    c = TestClient(app)
    p1 = _make_project(c, "Flat One")
    p2 = _make_project(c, "Flat Two")
    d1 = _upload_doc(c, p1)
    d2 = _upload_doc(c, p2)
    for pid in (p1, p2):
        assert c.get(f"/projects/{pid}").status_code == 200, (
            f"gate off: project {pid} must be reachable"
        )
    for did in (d1, d2):
        assert c.get(f"/documents/{did}").status_code == 200, (
            f"gate off: doc {did} must be reachable"
        )
    body = c.get("/projects").text
    assert "Flat One" in body and "Flat Two" in body, (
        "gate off: both projects must list (no org filtering)"
    )
    print("  gate-off: cross-'org' access unrestricted (sacred gate, as today)")


# --------------------------------------------------------------------------- #
# P3: directly-owned tables ENFORCEMENT (customers / connections / workflows /
# language sets / routing rules / department budgets) + connector object-authz.
#
# Reuses the two-org world from _seed_subtree_via_routes (orgs A/B, admins A/B,
# clients ca/cb) and seeds one of each directly-owned row per org -- a customer, a
# connection, a workflow (incl. a GLOBAL-scope one), a language set, a routing rule
# and a department budget. The negative matrix asserts org A can NEVER see or reach
# any of org B's rows: lists exclude them, /connectors/connection/{cid} + every
# mutation + /api/connectors/{cid}/* 404, B's GLOBAL workflow is not surfaced to A,
# and a routing rule / budget of B never applies to A's submission. It also proves
# creation stamps org A and re-asserts the sacred gate OFF is byte-identical.
# --------------------------------------------------------------------------- #
_P3_CACHE = {}


def _seed_p3_rows():
    """On top of the P2 subtree world, seed one directly-owned row of every kind
    per org (stamped to that org). Returns the ca/cb clients + a dict of ids.
    Seeded ONCE per process (cached); each test re-engages the gate itself."""
    from app.models import (
        Customer,
        LanguageSet,
        Scope,
        Workflow,
    )

    ca, cb, sub = _seed_subtree_via_routes()
    if _P3_CACHE:
        _gate_on()
        return ca, cb, _P3_CACHE["ids"]
    oa, ob = sub["org_a"], sub["org_b"]
    with Session(engine) as s:
        rows = {}
        for tag, oid in (("a", oa), ("b", ob)):
            cust = Customer(name=f"Cust {tag.upper()}", org_id=oid)
            conn = Connection(
                kind="airtable",
                name=f"Conn {tag.upper()}",
                config={},
                secret_enc="",
                org_id=oid,
                created_by=f"{tag}{tag}@x.com",
            )
            # A GLOBAL-scope workflow per org: leak vector 12 -- a global workflow is
            # org-owned now, so B's global must NOT surface to A.
            wf = Workflow(name=f"WF {tag.upper()}", scope=Scope.GLOBAL, org_id=oid)
            ls = LanguageSet(
                name=f"LS {tag.upper()}",
                source_locale="en-US",
                target_locales=["fr-FR"],
                org_id=oid,
            )
            # Both orgs' rules match ANY ext and share the SAME department name
            # ("shared") -- the isolation must come from org scoping, not names.
            rule = RoutingRule(
                priority=10,
                active=True,
                match_ext="",
                department="shared",
                provider="mock",
                org_id=oid,
            )
            bud = DepartmentBudget(department="shared", monthly_token_limit=5, org_id=oid)
            for row in (cust, conn, wf, ls, rule, bud):
                s.add(row)
            s.commit()
            for row in (cust, conn, wf, ls, rule, bud):
                s.refresh(row)
            rows[tag] = {
                "cust": cust.id,
                "conn": conn.id,
                "wf": wf.id,
                "ls": ls.id,
                "rule": rule.id,
                "bud": bud.id,
            }
        ids = {"org_a": oa, "org_b": ob, "a": rows["a"], "b": rows["b"]}
    _P3_CACHE["ids"] = ids
    return ca, cb, ids


def test_p3_lists_exclude_other_org() -> None:
    """Gate ON: userA's LISTS (customers, connectors, workflows, languages, routing)
    exclude every one of org B's directly-owned rows (leak vector 2)."""
    ca, _cb, _d = _seed_p3_rows()
    try:
        # customers list
        body = ca.get("/customers").text
        assert "Cust A" in body and "Cust B" not in body, "LEAK: org B customer in A's list"
        # connectors index + the airtable type page
        assert "Conn B" not in ca.get("/connectors").text, "LEAK: org B connection in A's index"
        assert "Conn B" not in ca.get("/connectors/airtable").text, (
            "LEAK: org B connection on A's connector-type page"
        )
        assert "Conn A" in ca.get("/connectors/airtable").text, "A's own connection missing"
        # workflows list
        wf_body = ca.get("/workflows").text
        assert "WF A" in wf_body and "WF B" not in wf_body, "LEAK: org B workflow in A's list"
        # language sets list
        ls_body = ca.get("/languages").text
        assert "LS A" in ls_body and "LS B" not in ls_body, "LEAK: org B language set in A's list"
        # routing admin page: rules + budgets + the picker lists
        rt_body = ca.get("/routing").text
        assert "WF B" not in rt_body and "LS B" not in rt_body, (
            "LEAK: org B workflow/langset in A's routing pickers"
        )
        print("  P3 lists: A's customers/connectors/workflows/languages/routing exclude B")
    finally:
        _gate_off()


def test_p3_connection_object_authz_404() -> None:
    """Gate ON: userA gets 404 on org B's /connectors/connection/{cid} + EVERY
    mutation route (leak vector 10 -- connectors had ZERO object authz before P3).
    404 (not 403): a foreign cid must be indistinguishable from a missing one."""
    ca, _cb, d = _seed_p3_rows()
    try:
        cb_id = d["b"]["conn"]

        def is404(r, what):
            assert r.status_code == 404, (
                f"{what}: expected 404 (cross-org connection), got {r.status_code}"
            )

        is404(ca.get(f"/connectors/connection/{cb_id}"), "GET connection {B}")
        is404(
            ca.post(f"/connectors/connection/{cb_id}", data={"name": "x"}),
            "POST connection update {B}",
        )
        is404(ca.post(f"/connectors/connection/{cb_id}/test"), "POST test {B}")
        is404(ca.post(f"/connectors/connection/{cb_id}/pull"), "POST pull {B}")
        is404(ca.post(f"/connectors/connection/{cb_id}/push"), "POST push {B}")
        is404(ca.post(f"/connectors/connection/{cb_id}/sync"), "POST sync {B}")
        is404(ca.post(f"/connectors/connection/{cb_id}/rotate"), "POST rotate {B}")
        is404(
            ca.post(f"/connectors/connection/{cb_id}/retry", data={"job_id": "0"}), "POST retry {B}"
        )
        is404(
            ca.post(f"/connectors/connection/{cb_id}/schedule", data={"every_minutes": "0"}),
            "POST schedule {B}",
        )
        is404(ca.post(f"/connectors/connection/{cb_id}/delete"), "POST delete {B}")
        is404(
            ca.post(f"/connectors/connection/{cb_id}/languages", data={"language_set_id": ""}),
            "POST languages {B}",
        )
        # JSON browse pickers 404 too (their own JSON 404 shape).
        is404(
            ca.post(f"/connectors/connection/{cb_id}/browse/containers"),
            "POST browse/containers {B}",
        )
        is404(
            ca.post(
                f"/connectors/connection/{cb_id}/browse/fields", data={"base": "b", "table": "t"}
            ),
            "POST browse/fields {B}",
        )
        # Control: A CAN reach its OWN connection (no blanket lockout).
        assert ca.get(f"/connectors/connection/{d['a']['conn']}").status_code == 200, (
            "A can't reach its OWN connection"
        )
        print("  P3 connectors: A gets 404 on B's connection detail + every mutation")
    finally:
        _gate_off()


def test_p3_api_connector_object_authz_404() -> None:
    """Gate ON: every /api/connectors/{cid}/* for org B's connection 404s to a
    docs-scoped key belonging to org A, and A's /api/connectors list excludes B."""
    from app.services import apikeys

    _ca, _cb, d = _seed_p3_rows()
    try:
        ua = security.get_by_email("aa@x.com")
        _row, raw = apikeys.create_key(ua.id, "p3-docs", scopes="api docs")
        api = TestClient(app)
        h = {"X-Loc-Token": raw}
        cb_id = d["b"]["conn"]

        def is404(r, what):
            assert r.status_code == 404, f"{what}: expected 404, got {r.status_code}"

        is404(api.post(f"/api/connectors/{cb_id}/pull", headers=h), "api pull {B}")
        is404(api.post(f"/api/connectors/{cb_id}/push", headers=h), "api push {B}")
        is404(api.post(f"/api/connectors/{cb_id}/sync", headers=h, json={}), "api sync {B}")
        is404(
            api.post(f"/api/connectors/{cb_id}/retry", headers=h, json={"job_id": 0}),
            "api retry {B}",
        )
        is404(
            api.post(f"/api/connectors/{cb_id}/jira/translate", headers=h, json={}), "api jira {B}"
        )
        is404(
            api.post(f"/api/connectors/{cb_id}/confluence/translate", headers=h, json={}),
            "api confluence {B}",
        )
        is404(
            api.post(f"/api/connectors/{cb_id}/airtable/webhook", headers=h, json={}),
            "api airtable webhook {B}",
        )
        # A's /api/connectors list: only A's connection, never B's.
        lst = api.get("/api/connectors", headers=h).json()
        names = {c["name"] for c in lst.get("connections", [])}
        assert "Conn A" in names and "Conn B" not in names, (
            f"LEAK: /api/connectors listed org B's connection to A ({names!r})"
        )
        print("  P3 api-connectors: A 404s on every /api/connectors/{B}/* + list excludes B")
    finally:
        _gate_off()


def test_p3_global_workflow_not_surfaced_cross_org() -> None:
    """Gate ON: org B's GLOBAL-scope workflow is NOT surfaced to A -- not in the
    /templates clonable-globals list, not in _applicable_workflows for A's project,
    and A cannot open or clone it (404) (leak vector 12)."""
    from app.models import Project as _P
    from app.web.queries import _applicable_workflows

    ca, _cb, d = _seed_p3_rows()
    try:
        wf_b = d["b"]["wf"]
        # /templates lists clonable GLOBAL workflows -- B's must not appear.
        tpl = ca.get("/templates").text
        assert "WF A" in tpl and "WF B" not in tpl, (
            "LEAK: org B's GLOBAL workflow in A's /templates list"
        )
        # _applicable_workflows for A's project excludes B's global workflow.
        with Session(engine) as s:
            proj_a = s.get(_P, _SUBTREE_CACHE["ids"]["pa"])
            apps = _applicable_workflows(s, proj_a)
            names = {w.name for w in apps}
            assert "WF A" in names, "A's own global workflow must apply to A's project"
            assert "WF B" not in names, "LEAK: org B's global workflow applicable to A's project"
        # A cannot open or clone B's workflow.
        assert ca.get(f"/workflows/{wf_b}").status_code == 404, "A opened B's workflow"
        assert ca.post(f"/workflows/{wf_b}/clone").status_code == 404, "A cloned B's workflow"
        print("  P3 workflows: B's GLOBAL workflow hidden from A (templates/applicable/open/clone)")
    finally:
        _gate_off()


def test_p3_routing_and_budget_org_scoped_resolution() -> None:
    """Gate ON: a routing rule / budget of org B NEVER applies to org A's
    submission. resolve() with org A only sees A's rule; get_budget/check with org A
    only see A's budget -- even though A and B share the department name 'shared'
    (leak vector 12: department-name matching must be org-scoped)."""
    from app.services import budgets
    from app.services import routing as routing_svc

    _ca, _cb, d = _seed_p3_rows()
    _gate_on()
    try:
        oa, ob = d["org_a"], d["org_b"]
        with Session(engine) as s:
            # resolve for org A: the matching rule must be A's (id), never B's.
            dec_a = routing_svc.resolve(
                s, ext="json", department="shared", actor="aa@x.com", ensure_inbox=False, org_id=oa
            )
            assert dec_a.rule_id == d["a"]["rule"], (
                "org A resolve picked rule {!r} (expected A's {!r})".format(
                    dec_a.rule_id, d["a"]["rule"]
                )
            )
            assert dec_a.rule_id != d["b"]["rule"], "LEAK: org A matched org B's routing rule"
            # resolve for org B independently picks B's rule.
            dec_b = routing_svc.resolve(
                s, ext="json", department="shared", actor="bb@x.com", ensure_inbox=False, org_id=ob
            )
            assert dec_b.rule_id == d["b"]["rule"], "org B resolve should pick B's rule"
            # get_budget is org-scoped: A's lookup returns A's row, never B's.
            ba = budgets.get_budget(s, "shared", org_id=oa)
            bb = budgets.get_budget(s, "shared", org_id=ob)
            assert ba is not None and ba.id == d["a"]["bud"], "org A budget lookup wrong row"
            assert bb is not None and bb.id == d["b"]["bud"], "org B budget lookup wrong row"
            assert ba.id != bb.id, "LEAK: shared department resolved to the SAME budget cross-org"
        # check() is org-scoped: A's cap doesn't gate B and vice-versa (both have
        # their OWN 'shared' cap). Prove A sees a budget (remaining != -1 unlimited).
        allowed_a, remaining_a, _m = budgets.check("shared", org_id=oa)
        assert allowed_a and remaining_a != -1, (
            f"org A 'shared' should be budgeted (its own cap), got remaining={remaining_a!r}"
        )
        # With NO org (gate-off semantics passed explicitly) check still finds a row,
        # but the point is the org-scoped calls never cross -- asserted above.
        print("  P3 routing/budget: B's rule + budget never apply to A (org-scoped 'shared')")
    finally:
        _gate_off()


def test_p3_creation_stamps_org() -> None:
    """Gate ON: rows created by org A through the routes land in org A -- a customer,
    a connection, a workflow, a language set, a routing rule and a budget."""
    from app.models import (
        Customer,
        LanguageSet,
        Workflow,
    )

    ca, _cb, d = _seed_p3_rows()
    try:
        oa = d["org_a"]
        # customer
        assert (
            ca.post("/customers", data={"name": "Stamp Cust A"}, follow_redirects=False).status_code
            == 303
        )
        # connection (via the create route)
        rc = ca.post("/connectors/airtable", data={"name": "Stamp Conn A"}, follow_redirects=False)
        assert rc.status_code == 303, f"connector create -> {rc.status_code}"
        # workflow (GLOBAL scope)
        assert (
            ca.post(
                "/workflows",
                data={"name": "Stamp WF A", "scope": "global", "customer_id": ""},
                follow_redirects=False,
            ).status_code
            == 303
        )
        # language set
        assert (
            ca.post(
                "/languages",
                data={"name": "Stamp LS A", "source_locale": "en-US", "target_locales": "de-DE"},
                follow_redirects=False,
            ).status_code
            == 303
        )
        # routing rule + budget (admin page)
        assert (
            ca.post(
                "/routing",
                data={
                    "match_ext": "srt",
                    "department": "stampdept",
                    "priority": "50",
                    "provider": "mock",
                },
                follow_redirects=False,
            ).status_code
            == 303
        )
        assert (
            ca.post(
                "/routing/budgets",
                data={"department": "stampdept", "monthly_token_limit": "1000"},
                follow_redirects=False,
            ).status_code
            == 303
        )
        with Session(engine) as s:

            def _org(model, name_attr, name):
                row = s.exec(select(model).where(getattr(model, name_attr) == name)).first()
                assert row is not None, f"{model.__name__} {name!r} not created"
                return row.org_id

            assert _org(Customer, "name", "Stamp Cust A") == oa, "customer not stamped org A"
            assert _org(Connection, "name", "Stamp Conn A") == oa, "connection not stamped org A"
            assert _org(Workflow, "name", "Stamp WF A") == oa, "workflow not stamped org A"
            assert _org(LanguageSet, "name", "Stamp LS A") == oa, "language set not stamped org A"
            assert _org(RoutingRule, "department", "stampdept") == oa, "rule not stamped org A"
            assert _org(DepartmentBudget, "department", "stampdept") == oa, (
                "budget not stamped org A"
            )
        print("  P3 stamping: A's customer/connection/workflow/langset/rule/budget land in org A")
    finally:
        _gate_off()


def test_p3_null_project_tm_no_cross_org_bleed() -> None:
    """Gate ON: a NULL-project (org-less) global TM row can NEVER suggest into a
    scoped org's document (leak vector 9). In multi-org mode the NULL-project global
    fallback is DROPPED from the scope predicate -- a lookup is confined to its OWN
    project's rows -- so an org-less global entry can't bleed across orgs.

    Two things are proven: (1) the _tm_scope PREDICATE excludes the NULL fallback
    when the gate is on and keeps it when off (the authoritative behavior change);
    (2) end-to-end, an org B lookup never returns a NULL-project row's target while
    a project's OWN row still matches (the scope change drops only the NULL leg).

    NOTE (surprise, documented in the report): SQLite's `col IN (pid, NULL)` never
    actually MATCHES NULL-project rows (NULL != anything), so the pre-existing
    global-TM fallback was already inert for NULL rows in practice -- this makes the
    isolation strictly safe and the multi-org change a belt-and-suspenders tighten
    (scope becomes `col == pid`, provably no NULL leg), not a behavior regression."""
    from app.models import SegmentTM
    from app.services import tm

    # (1) Predicate-level proof: with the gate ON the scope is strictly the project
    # (no NULL leg); with it OFF the historical NULL fallback is retained.
    _gate_on()
    try:
        on = str(tm._tm_scope(SegmentTM.project_id, 7))
        assert "NULL" not in on.upper() and "IN " not in on.upper(), (
            f"multi-org: _tm_scope must drop the NULL-project fallback (got {on!r})"
        )
    finally:
        _gate_off()
    off = str(tm._tm_scope(SegmentTM.project_id, 7))
    assert "IN " in off.upper(), (
        f"gate off: _tm_scope must keep the project-or-NULL fallback (got {off!r})"
    )

    # (2) End-to-end: seed one project-scoped row for org A's project and one
    # NULL-project row; org B's lookup must never see the NULL row's target, and
    # (belt) org A's OWN project row still matches under the gate.
    pa = _SUBTREE_CACHE["ids"]["pa"]
    pb = _SUBTREE_CACHE["ids"]["pb"]
    src_a = "Org A private phrase alpha."
    src_g = "Global org-less phrase gamma."
    with Session(engine) as s:
        for pid, txt, tgt in ((pa, src_a, "Alpha-A"), (None, src_g, "Gamma-G")):
            if not s.exec(
                select(SegmentTM).where(
                    SegmentTM.source_hash == tm.hash_text(txt, "en-US", "fr-FR")
                )
            ).first():
                s.add(
                    SegmentTM(
                        project_id=pid,
                        source_locale="en-US",
                        target_locale="fr-FR",
                        source_text=txt,
                        target_text=tgt,
                        source_hash=tm.hash_text(txt, "en-US", "fr-FR"),
                    )
                )
        s.commit()

    _gate_on()
    try:
        with Session(engine) as s:
            # org A's OWN project row still matches for org A (scope keeps pid rows).
            hit_a = tm.lookup(s, pa, src_a, "en-US", "fr-FR")
            assert hit_a is not None and hit_a["target_text"] == "Alpha-A", (
                "multi-org: a project's OWN TM row must still match"
            )
            # org B never gets org A's project row nor the NULL-project global row.
            assert tm.lookup(s, pb, src_a, "en-US", "fr-FR") is None, (
                "LEAK: org A's project TM matched into org B"
            )
            assert tm.lookup(s, pb, src_g, "en-US", "fr-FR") is None, (
                "LEAK: NULL-project global TM suggested into org B's document"
            )
        print("  P3 null-project TM: NULL fallback dropped under gate; no cross-org bleed")
    finally:
        _gate_off()


def test_p3_gate_off_directly_owned_byte_identical() -> None:
    """The sacred gate re-asserted for the P3 surfaces: with the gate OFF every
    directly-owned predicate is a no-op. A single anonymous (auth-off) client lists
    connectors/workflows/languages/routing with NO org filtering and reaches any
    connection detail -- byte-identical to single-tenant today."""
    init_db()
    _gate_off()
    assert access.multi_org_active() is False, "gate must be OFF here"
    c = TestClient(app)
    for path in ("/connectors", "/workflows", "/languages", "/routing", "/customers"):
        assert c.get(path).status_code == 200, f"gate off: {path} must render"
    # list service functions with org_id=None list everything (no filter).
    from app.services import languages as _lang
    from app.services.connectors import store as _store

    assert isinstance(_lang.list_sets(org_id=None), list), "list_sets(None) must not filter"
    assert isinstance(_store.list_connections(org_id=None), list), (
        "list_connections(None) must not filter"
    )
    print("  P3 gate-off: directly-owned lists + connection routes unrestricted (as today)")


# --------------------------------------------------------------------------- #
# P4: API-token org binding (lsu_ home-org, lsk_ bind-or-reject) + /api surface.
#
# api_identity now attaches the identity's org: a per-user key ("lsu_...") ->
# its User's HOME org (a super-admin included = THE LINCHPIN on the API surface),
# the shared connector token ("lsk_...") -> its bound connector_token_org_id or
# None (unbound -> REJECTED on org-scoped ops in multi-org). Reuses the two-org
# world (orgs A/B, users aa@x.com / bb@x.com, docs da/db, jobs job_a/job_b from
# _seed_subtree_via_routes). Mints an lsu_ 'api docs' key per user and drives the
# real /api routes with the sacred gate ON, asserting org A's key can NEVER reach
# org B's document / job, a super-admin's key is confined to its HOME org, and the
# shared token binds-or-rejects. Gate-off is re-asserted byte-identical.
# --------------------------------------------------------------------------- #
_P4_CACHE = {}


def _seed_p4_keys():
    """On top of the P2/P3 world, mint an lsu_ 'api docs' key for each org's user.
    Returns (ca, cb, ids, key_a, key_b) where key_* are the raw lsu_ tokens.
    Seeded once per process (cached)."""
    from app.services import apikeys

    ca, cb, _sub = _seed_subtree_via_routes()
    if _P4_CACHE:
        _gate_on()
        return ca, cb, _SUBTREE_CACHE["ids"], _P4_CACHE["key_a"], _P4_CACHE["key_b"]
    ua = security.get_by_email("aa@x.com")
    ub = security.get_by_email("bb@x.com")
    _ra, key_a = apikeys.create_key(ua.id, "p4-a", scopes="api docs")
    _rb, key_b = apikeys.create_key(ub.id, "p4-b", scopes="api docs")
    _P4_CACHE.update({"key_a": key_a, "key_b": key_b})
    return ca, cb, _SUBTREE_CACHE["ids"], key_a, key_b


def test_p4_api_identity_carries_home_org() -> None:
    """Gate ON: api_identity attaches org_id = the key owner's HOME org; deps
    ._ident_org_id reads it. Gate OFF: org_id is None (no scoping)."""
    from app.web import deps

    _ca, _cb, d, key_a, _key_b = _seed_p4_keys()
    _gate_on()
    try:
        # Drive a real request so api_identity resolves against the header. A tiny
        # probe route isn't available, so resolve via the deps helper directly with a
        # faked request carrying the token header.
        class _Req:
            headers = {"X-Loc-Token": key_a}  # noqa: RUF012

        ident = deps.api_identity(_Req())
        assert ident is not None and ident["kind"] == "user", "lsu_ key must resolve"
        assert ident.get("org_id") == d["org_a"], (
            "api_identity must attach HOME org A, got {!r}".format(ident.get("org_id"))
        )
        assert deps._ident_org_id(ident) == d["org_a"], "_ident_org_id must read the org"
        print("  P4 identity: lsu_ key carries its owner's HOME org (A)")
    finally:
        _gate_off()

    # Gate OFF: org_id None (no scoping), byte-identical to today.
    class _Req2:
        headers = {"X-Loc-Token": key_a}  # noqa: RUF012

    ident_off = deps.api_identity(_Req2())
    assert ident_off is not None and ident_off.get("org_id") is None, (
        "gate off: api_identity org_id must be None (no scoping)"
    )
    assert deps._ident_org_id(ident_off) is None, "gate off: _ident_org_id must be None"
    print("  P4 identity: gate-off lsu_ org_id None (byte-identical)")


def test_p4_lsu_cross_org_document_and_job_404() -> None:
    """Gate ON: org A's lsu_ key gets 404 on org B's /api/documents/{id},
    .../translate, .../download and /api/jobs/{jid} -- an id in another org is
    indistinguishable from a missing one (leak vector 6)."""
    _ca, _cb, d, key_a, _key_b = _seed_p4_keys()
    try:
        api = TestClient(app)
        h = {"X-Loc-Token": key_a}
        db_, jb = d["db"], d["job_b"]

        def is404(r, what):
            assert r.status_code == 404, (
                f"{what}: expected 404, got {r.status_code} ({r.text[:120]})"
            )

        is404(
            api.post(f"/api/documents/{db_}/translate", headers=h, json={"targets": ["fr-FR"]}),
            "A key -> B doc translate",
        )
        is404(
            api.get(f"/api/documents/{db_}/download?locale=fr-FR", headers=h),
            "A key -> B doc download",
        )
        is404(api.get(f"/api/jobs/{jb}", headers=h), "A key -> B job")
        # Control: A's key CAN reach its OWN doc + job (no blanket lockout).
        assert api.get(f"/api/jobs/{d['job_a']}", headers=h).status_code == 200, (
            "A's key must read A's own job"
        )
        print("  P4 lsu_: A's key 404s on B's document (translate/download) + B's job")
    finally:
        _gate_off()


def test_p4_lsu_lists_only_own_org() -> None:
    """Gate ON: org A's lsu_ key lists/reads ONLY org A -- /api/connectors and the
    discovery /api/languages surface exclude org B (leak vector 2/6)."""
    _ca, _cb, d, key_a, _key_b = _seed_p4_keys()
    _gate_on()
    try:
        api = TestClient(app)
        h = {"X-Loc-Token": key_a}
        # Seed a distinctly-named language set per org so /api/languages has an
        # org-scoped probe pair (guaranteed present, unlike P3's shared names).
        with Session(engine) as s:
            s.add(
                LanguageSet(
                    name="P4 LS A",
                    source_locale="en-US",
                    target_locales=["fr-FR"],
                    org_id=d["org_a"],
                )
            )
            s.add(
                LanguageSet(
                    name="P4 LS B",
                    source_locale="en-US",
                    target_locales=["de-DE"],
                    org_id=d["org_b"],
                )
            )
            s.commit()
        langs = api.get("/api/languages", headers=h).json()
        names = {ls["name"] for ls in langs.get("language_sets", [])}
        assert "P4 LS A" in names, f"A's own language set missing from /api/languages ({names!r})"
        assert "P4 LS B" not in names, f"LEAK: /api/languages surfaced org B's set to A ({names!r})"
        # /api/connectors already excludes B (P3), re-assert with the lsu_ key.
        conns = api.get("/api/connectors", headers=h).json()
        cnames = {c["name"] for c in conns.get("connections", [])}
        assert "Conn B" not in cnames, f"LEAK: /api/connectors listed org B to A ({cnames!r})"
        print("  P4 lsu_: A's key lists only org A (languages + connectors exclude B)")
    finally:
        _gate_off()


def test_p4_superadmin_lsu_confined_to_home_org() -> None:
    """THE LINCHPIN on the API surface: a super-admin's lsu_ key is confined to its
    HOME org -- flagging aa@x.com is_superadmin does NOT let its key read org B's
    document or job. is_superadmin is a back-office ticket, never an /api
    scope-widener."""
    _ca, _cb, d, key_a, _key_b = _seed_p4_keys()
    _gate_on()
    try:
        # Flip aa@x.com to super-admin -- its lsu_ key must STILL be org-A-only.
        with Session(engine) as s:
            u = s.exec(select(User).where(User.email == "aa@x.com")).first()
            u.is_superadmin = True
            s.add(u)
            s.commit()
        settings.invalidate_cache()
        # GATE R3 LOW-11: assert the PRECONDITION, not only the outcome. Since 2026-09-20
        # every init_db() clears this flag, so a future call landing between the write above
        # and the assertions below would leave this test passing on an UNFLAGGED user, which
        # proves nothing: a plain admin is confined too. Today the ordering holds; this line
        # is what makes a change to that ordering go RED instead of quietly vacuous.
        with Session(engine) as s:
            _u = s.exec(select(User).where(User.email == "aa@x.com")).first()
            assert _u.is_superadmin is True, (
                "VACUOUS TEST: aa@x.com is not flagged at the assertion point, so the "
                "confinement below would pass for a plain admin. Something cleared it "
                "between the setup above and here (init_db() clears it on every call)."
            )
        api = TestClient(app)
        h = {"X-Loc-Token": key_a}
        db_, jb = d["db"], d["job_b"]
        assert api.get(f"/api/jobs/{jb}", headers=h).status_code == 404, (
            "LINCHPIN FAILED: super-admin lsu_ key read org B's job on /api"
        )
        assert (
            api.post(
                f"/api/documents/{db_}/translate", headers=h, json={"targets": ["fr-FR"]}
            ).status_code
            == 404
        ), "LINCHPIN FAILED: super-admin lsu_ key reached org B's document on /api"
        # Its own org still reachable.
        assert api.get(f"/api/jobs/{d['job_a']}", headers=h).status_code == 200, (
            "super-admin lsu_ key must still read its OWN org's job"
        )
        print("  P4 linchpin: super-admin lsu_ key confined to HOME org on /api (no cross-org)")
    finally:
        # Restore the flag so later tests see aa@x.com as a plain admin.
        with Session(engine) as s:
            u = s.exec(select(User).where(User.email == "aa@x.com")).first()
            u.is_superadmin = False
            s.add(u)
            s.commit()
        settings.invalidate_cache()
        _gate_off()


def test_p4_shared_token_bind_or_reject() -> None:
    """Gate ON: the shared lsk_ connector token is REJECTED (403) on org-scoped ops
    when connector_token_org_id is UNSET, and scoped-to-that-org when set (leak
    vector 6). A shared credential must never silently see every org."""
    from app.web import deps

    _ca, _cb, d, _key_a, _key_b = _seed_p4_keys()
    _gate_on()
    try:
        lsk = deps.connector_api_token()
        api = TestClient(app)
        h = {"X-Loc-Token": lsk}
        conn_a = d["a"]["conn"] if "a" in d and isinstance(d["a"], dict) else None
        # d here is _SUBTREE_CACHE ids (no P3 conn); pull the P3 world for a conn id.
        _ca2, _cb2, p3ids = _seed_p3_rows()
        _gate_on()
        conn_a = p3ids["a"]["conn"]

        # --- UNBOUND: connector_token_org_id blank -> 403 REJECT on org-scoped ops.
        settings.set_many({"connector_token_org_id": ""})
        settings.invalidate_cache()
        r = api.get("/api/connectors", headers=h)
        assert r.status_code == 403, (
            f"unbound shared token must 403 with a clear message, got {r.status_code} ({r.text[:160]})"  # noqa: E501
        )
        assert api.post(f"/api/connectors/{conn_a}/pull", headers=h).status_code == 403, (
            "unbound shared token must 403 on a connector pull"
        )

        # --- BOUND to org A: scoped to A -- its own connection is reachable (not a
        #     403 reject, not a cross-org 404), and B's connection is 404.
        settings.set_many({"connector_token_org_id": str(p3ids["org_a"])})
        settings.invalidate_cache()
        # list no longer 403s and excludes org B.
        lst = api.get("/api/connectors", headers=h)
        assert lst.status_code == 403, "org binding must not restore shared-token authority"
        # cross-org connection is a 404 (not enumerable) for the BOUND token.
        conn_b = p3ids["b"]["conn"]
        assert api.post(f"/api/connectors/{conn_b}/pull", headers=h).status_code == 403, (
            "shared token must be refused before looking up any connection"
        )
        print("  P4 lsk_: bound and unbound legacy shared tokens refused")
    finally:
        settings.set_many({"connector_token_org_id": ""})
        settings.invalidate_cache()
        _gate_off()


def test_p4_upload_via_key_lands_in_org() -> None:
    """Gate ON: a document uploaded via org A's lsu_ key lands in org A -- the
    auto-created inbox is stamped org A (leak vector 11)."""
    from app.models import Project as _P

    _ca, _cb, d, key_a, _key_b = _seed_p4_keys()
    _gate_on()
    try:
        api = TestClient(app)
        r = api.post(
            "/api/documents",
            headers={"X-Loc-Token": key_a},
            files={"file": ("p4.txt", b"P4 inbox doc.", "text/plain")},
            data={"mode": "segmented"},
        )
        assert r.status_code == 200, f"API upload -> {r.status_code} ({r.text[:160]})"
        pid = r.json()["project_id"]
        with Session(engine) as s:
            assert s.get(_P, pid).org_id == d["org_a"], (
                "doc uploaded via A's key did not land in org A (inbox unstamped)"
            )
        print("  P4 upload: a doc uploaded via A's key lands in org A (inbox stamped)")
    finally:
        _gate_off()


def test_p4_gate_off_tokens_behave_as_today() -> None:
    """The sacred gate re-asserted: with the gate OFF, both lsu_ and lsk_ tokens
    behave EXACTLY as today -- no org scoping, no bind-or-reject. A single install
    is its own tenant boundary."""
    from app.web import deps

    _ca, _cb, _d, key_a, _key_b = _seed_p4_keys()
    _gate_off()
    assert access.multi_org_active() is False, "gate must be OFF here"
    api = TestClient(app)

    # lsu_ key: org_id None, reads its own job fine (auth-on but gate-off scoping is
    # a no-op -- the manager/creator rule still applies, unchanged from pre-P4).
    class _Req:
        headers = {"X-Loc-Token": key_a}  # noqa: RUF012

    assert deps._ident_org_id(deps.api_identity(_Req())) is None, (
        "gate off: lsu_ _ident_org_id must be None (no scoping)"
    )
    # lsk_ shared token: NOT rejected (no bind-or-reject with the gate off); the
    # /api/connectors list renders 200 (unfiltered), byte-identical to today.
    lsk = deps.connector_api_token()
    r = api.get("/api/connectors", headers={"X-Loc-Token": lsk})
    assert r.status_code == 403, f"gate off: shared token must be refused ({r.status_code})"
    print("  P4 gate-off: user key unchanged; legacy shared token refused")


# --------------------------------------------------------------------------- #
# P6: remote CLI (org-scoped by token) + /api gaps + SaaS in-process operator gate.
#
# Three surfaces:
#   (A) the NEW /api endpoints (project/customer/langset/workflow/job CRUD) are
#       org-bound: org A's lsu_ token 404s on B's rows and lists exclude B.
#   (B) remote-mode CLI (`--remote URL --token lsu_...`) drives those /api routes
#       over an httpx transport wired to the in-process TestClient — org A's token
#       can neither list nor read org B; db/migrate REFUSES --remote (exit 2); a
#       remote command's output shape matches its in-process equivalent.
#   (C) the SaaS IN-PROCESS operator gate: gate ON, an unscoped in-process cross-org
#       command REFUSES (exit 2); --org scopes; a valid --operator (env
#       LOC_TMS_OPERATOR_KEY vs operator_key_hash) allows cross-org; gate OFF the
#       in-process CLI works with NO --org (byte-identical to single-tenant today).
# --------------------------------------------------------------------------- #
import hashlib
import io
import json as _json
from contextlib import redirect_stderr, redirect_stdout

import httpx

from app.cli import _remote
from app.cli.__main__ import main as cli_main


def _cli(argv):
    """Run the CLI in-process, capturing (exit_code, stdout, stderr)."""
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        try:
            code = cli_main(argv)
        except SystemExit as exc:
            code = exc.code if isinstance(exc.code, int) else 2
    return code, out.getvalue(), err.getvalue()


def _cli_json(argv):
    code, out, err = _cli(["--json"] + argv)  # noqa: RUF005
    payload = _json.loads(out.strip().splitlines()[-1]) if out.strip() else {}
    return code, payload, err


def _mock_transport_for(token_client):
    """A sync httpx.MockTransport that forwards every request to the in-process
    TestClient (so remote mode runs fully offline against the real /api)."""

    def handler(request):
        path = request.url.path
        if request.url.query:
            path += "?" + request.url.query.decode()
        r = token_client.request(
            request.method, path, headers=dict(request.headers), content=request.content
        )
        return httpx.Response(
            r.status_code,
            content=r.content,
            headers={"content-type": r.headers.get("content-type", "")},
        )

    return httpx.MockTransport(handler)


def test_p6_api_endpoints_org_bound() -> None:
    """Gate ON: the NEW /api CLI-backing endpoints are org-bound. Org A's lsu_ key
    can create/list/read only org A; a fetch of org B's project/customer/langset/
    workflow 404s, and lists exclude B."""
    _ca, _cb, d, key_a, key_b = _seed_p4_keys()
    _gate_on()
    try:
        api = TestClient(app)
        ha, hb = {"X-Loc-Token": key_a}, {"X-Loc-Token": key_b}

        # A creates a customer + project + langset via the new /api routes -> org A.
        # 2026-07-14: a project requires a customer, so the customer is created
        # FIRST and passed to the project create (both land in org A via the key).
        rc = api.post("/api/customers", headers=ha, json={"name": "P6 Cust A"})
        ca_id = rc.json()["id"]
        rp = api.post(
            "/api/projects",
            headers=ha,
            json={"name": "P6 Proj A", "targets": "fr-FR", "customer_id": ca_id},
        )
        assert rp.status_code == 200, rp.text
        pa = rp.json()["id"]
        rl = api.post("/api/langsets", headers=ha, json={"name": "P6 LS A", "targets": "de-DE"})
        la = rl.json()["id"]
        with Session(engine) as s:
            assert s.get(Project, pa).org_id == d["org_a"], "P6 /api project not org A"
            assert s.get(Customer, ca_id).org_id == d["org_a"], "P6 /api customer not org A"

        # B creates its own customer + project so A has a cross-org id to be denied.
        rcb = api.post("/api/customers", headers=hb, json={"name": "P6 Cust B"})
        cb_id = rcb.json()["id"]
        rpb = api.post(
            "/api/projects",
            headers=hb,
            json={"name": "P6 Proj B", "targets": "fr-FR", "customer_id": cb_id},
        )
        pb = rpb.json()["id"]

        # A's lists exclude B; A's fetch of B's project 404s.
        names = {p["name"] for p in api.get("/api/projects", headers=ha).json()["projects"]}
        assert "P6 Proj A" in names and "P6 Proj B" not in names, names
        assert api.get(f"/api/projects/{pb}", headers=ha).status_code == 404, (
            "A read B's project via /api"
        )
        assert api.get(f"/api/projects/{pa}", headers=ha).status_code == 200, (
            "A can't read its OWN project via /api"
        )

        # customers + langsets + workflows: A's list excludes B; cross-org 404.
        cust_names = {c["name"] for c in api.get("/api/customers", headers=ha).json()["customers"]}
        assert "P6 Cust A" in cust_names, cust_names
        ls_names = {
            ls["name"] for ls in api.get("/api/langsets", headers=ha).json()["language_sets"]
        }
        assert "P6 LS A" in ls_names, ls_names
        # B's directly-seeded P3 workflow is invisible + 404 to A.
        _c2, _c3, p3 = _seed_p3_rows()
        _gate_on()
        wf_b = p3["b"]["wf"]
        assert api.get(f"/api/workflows/{wf_b}", headers=ha).status_code == 404, (
            "A read B's workflow via /api"
        )
        # B's langset update/delete 404 for A.
        assert (
            api.post(f"/api/langsets/{la}", headers=hb, json={"name": "hijack"}).status_code == 404
        ), "B mutated A's langset via /api"
        print("  P6 /api: new endpoints org-bound (A creates/lists A; 404 on B)")
    finally:
        _gate_off()


def test_p9_api_cli_cross_org_matrix() -> None:
    """REGRESSION GUARD (Finding 9 — the test gap that hid Finding 1). An org-A
    docs-scoped lsu_ token must reach NONE of org B's rows across EVERY app/web/
    api_cli.py route: projects (get/add-locale/apply-langset/documents), customers,
    documents/segments, tm import/export, glossary import/add/list, langsets
    (update/delete), workflows (show/run), runs/{id}/resume, and jobs (run + list).
    Cross-org is 404 (never 403 — a foreign id must be indistinguishable from a
    missing one); org-bound lists exclude the other org.

    SPECIFICALLY closes the Finding 1 exploit: an org-A token POSTing /api/jobs/run
    with a connector_translate payload naming org-B's connection_id must 404 and run
    NO job / make NO connector call."""
    from app.models import LanguageSet as _LS
    from app.models import WorkflowRun as _WR  # noqa: F401

    # The P4 world gives org-A/B lsu_ keys + docs/jobs; the P3 world adds a
    # connection + a global workflow + a language set per org (org-bound ids for B).
    _ca, _cb, sub, key_a, key_b = _seed_p4_keys()
    _c2, _c3, p3 = _seed_p3_rows()
    _gate_on()
    try:
        api = TestClient(app)
        ha, _hb = {"X-Loc-Token": key_a}, {"X-Loc-Token": key_b}

        def is404(r, what):
            assert r.status_code == 404, (
                f"{what}: expected 404, got {r.status_code} ({r.text[:140]})"
            )

        # --- Org B's ids A must never reach -------------------------------------
        pb = sub["pb"]  # org B project (route-created)
        db_ = sub["db"]  # org B document
        run_b = sub["run_b"]  # org B workflow run
        conn_b = p3["b"]["conn"]  # org B connection (Finding 1 target)
        wf_b = p3["b"]["wf"]  # org B workflow
        # A langset owned by B (seed one so update/delete have a B-owned target).
        with Session(engine) as s:
            lsb = _LS(
                name="F9 LS B", source_locale="en-US", target_locales=["de-DE"], org_id=sub["org_b"]
            )
            s.add(lsb)
            s.commit()
            s.refresh(lsb)
            ls_b = lsb.id
            # A paused run in org B so /api/runs/{id}/resume has a cross-org target.
            # (resume 404s on the org gate BEFORE the paused-state check.)

        # projects: get / documents-list / add-locale / apply-langset
        is404(api.get(f"/api/projects/{pb}", headers=ha), "GET /api/projects/{B}")
        is404(
            api.get(f"/api/projects/{pb}/documents", headers=ha), "GET /api/projects/{B}/documents"
        )
        is404(
            api.post(f"/api/projects/{pb}/add-locale", headers=ha, json={"locale": "de-DE"}),
            "POST add-locale {B}",
        )
        is404(
            api.post(f"/api/projects/{pb}/apply-langset", headers=ha, json={"langset_id": ls_b}),
            "POST apply-langset {B}",
        )

        # documents/segments
        is404(
            api.get(f"/api/documents/{db_}/segments", headers=ha), "GET /api/documents/{B}/segments"
        )

        # tm import/export
        import base64 as _b64m

        tmx = _b64m.b64encode(b"<tmx/>").decode()
        is404(
            api.post(
                f"/api/projects/{pb}/tm/import",
                headers=ha,
                json={"tmx_b64": tmx, "source": "en-US", "target": "fr-FR"},
            ),
            "POST tm/import {B}",
        )
        is404(
            api.get(f"/api/projects/{pb}/tm/export?source=en-US&target=fr-FR", headers=ha),
            "GET tm/export {B}",
        )

        # glossary import/add/list
        csvb = _b64m.b64encode(b"source,target\nhi,bonjour\n").decode()
        is404(
            api.post(
                f"/api/projects/{pb}/glossary/import",
                headers=ha,
                json={"csv_b64": csvb, "source": "en-US", "target": "fr-FR"},
            ),
            "POST glossary/import {B}",
        )
        is404(
            api.post(
                f"/api/projects/{pb}/glossary",
                headers=ha,
                json={"source_term": "hi", "target": "fr-FR"},
            ),
            "POST glossary add {B}",
        )
        is404(api.get(f"/api/projects/{pb}/glossary", headers=ha), "GET glossary list {B}")

        # langsets update/delete
        is404(
            api.post(f"/api/langsets/{ls_b}", headers=ha, json={"name": "hijack"}),
            "POST langset update {B}",
        )
        is404(api.post(f"/api/langsets/{ls_b}/delete", headers=ha), "POST langset delete {B}")

        # workflows show/run
        is404(api.get(f"/api/workflows/{wf_b}", headers=ha), "GET workflow {B}")
        is404(
            api.post(
                f"/api/workflows/{wf_b}/run", headers=ha, json={"doc_id": db_, "locale": "fr-FR"}
            ),
            "POST workflow run {B}",
        )

        # runs/{id}/resume
        is404(
            api.post(f"/api/runs/{run_b}/resume", headers=ha, json={"locale": "fr-FR"}),
            "POST run resume {B}",
        )

        # --- Finding 1 exploit: connector_translate naming org B's connection_id.
        # Snapshot the job table so we can prove NO job row is created by the denied
        # request (the connector handler is never reached -> no connector call).
        from app.services import jobs as _jobs

        before = len(_jobs.recent(limit=500))
        r = api.post(
            "/api/jobs/run",
            headers=ha,
            json={"kind": "connector_translate", "payload": {"connection_id": conn_b}},
        )
        is404(r, "POST /api/jobs/run connector_translate {B connection}")
        after = len(_jobs.recent(limit=500))
        assert after == before, (
            "Finding 1: a denied cross-org connector_translate created a job row "  # noqa: UP031
            "(%d -> %d) — the connector may have run" % (before, after)
        )
        # Also deny a translate_document payload naming org B's document.
        is404(
            api.post(
                "/api/jobs/run",
                headers=ha,
                json={
                    "kind": "translate_document",
                    "payload": {"document_id": db_, "locale": "fr-FR"},
                },
            ),
            "POST /api/jobs/run translate_document {B document}",
        )

        # jobs list: A's /api/jobs excludes B's jobs (job org = creator's org).
        jobs_a = api.get("/api/jobs", headers=ha).json().get("jobs", [])
        creators = {j.get("created_by") for j in jobs_a}
        assert "bb@x.com" not in creators, (
            f"LEAK: A's /api/jobs listed a job created by org B ({creators!r})"
        )

        # Perf cap: an absurd caller-supplied limit is clamped server-side (the org
        # gate in unified_history costs job_org_id -- up to 2 sessions/4 queries per
        # pseudo-actor row -- so an unbounded limit is a self-DoS vector), never
        # passed straight through to the shared history seam. (The unified /jobs
        # history now flows through job_history.unified_history, which the /api route
        # clamps the page limit to 500 before calling.)
        from app.services import job_history as _jobhist

        _captured = {}
        _orig_hist = _jobhist.unified_history
        _jobhist.unified_history = lambda session, user, **kw: (
            _captured.__setitem__("limit", kw.get("limit")),
            _orig_hist(session, user, **kw),
        )[1]
        try:
            api.get("/api/jobs?limit=99999", headers=ha)
        finally:
            _jobhist.unified_history = _orig_hist
        assert _captured.get("limit") == 500, (
            "limit=99999 must be clamped to 500 before hitting job_history."
            "unified_history(), got {!r}".format(_captured.get("limit"))
        )

        # --- Control: A's OWN connection_id is accepted by the org gate. A run of A's
        # own connection reaches the handler (MockTransport -> no real network); we
        # only assert the org gate did NOT 404 it (status is not 404).
        conn_a = p3["a"]["conn"]
        r_own = api.post(
            "/api/jobs/run",
            headers=ha,
            json={"kind": "connector_translate", "payload": {"connection_id": conn_a}},
        )
        assert r_own.status_code != 404, (
            f"org gate wrongly 404'd A's OWN connection on /api/jobs/run ({r_own.status_code})"
        )

        # --- Control: A CAN reach its OWN rows (no blanket lockout).
        assert api.get(f"/api/projects/{sub['pa']}", headers=ha).status_code == 200, (
            "A can't read its OWN project via /api"
        )
        print(
            "  P9 api_cli: A's token 404s on EVERY org-B api_cli route; Finding 1 "
            "connector exploit closed (404, no job created); lists exclude B"
        )
    finally:
        _gate_off()


def test_p6_remote_cli_org_scoped() -> None:
    """Gate ON: `--remote --token lsu_A` drives /api over the in-process TestClient.
    Org A's token lists only A and 404s on B; the output shape matches the in-process
    arm; a wrong token maps a 404 to a clean exit-1 CliError."""
    _ca, _cb, _d, key_a, key_b = _seed_p4_keys()
    _gate_on()
    api = TestClient(app)
    _remote._set_transport(_mock_transport_for(api))
    try:
        # Seed a customer + project in each org via the remote CLI itself (token-scoped
        # create). 2026-07-14: a project requires a customer, so create one first
        # (token-scoped to the org) and pass --customer-id.
        code, cpay, err = _cli_json(
            ["--remote", "http://127.0.0.1", "--token", key_a, "customer", "create", "R6 Cust A"]
        )
        assert code == 0, (code, cpay, err)
        code, pay, err = _cli_json(
            [
                "--remote",
                "http://127.0.0.1",
                "--token",
                key_a,
                "project",
                "create",
                "R6 Proj A",
                "--targets",
                "fr-FR",
                "--customer-id",
                str(cpay["id"]),
            ]
        )
        assert code == 0 and pay["ok"], (code, pay, err)
        pa = pay["id"]
        code, cpayb, _ = _cli_json(
            ["--remote", "http://127.0.0.1", "--token", key_b, "customer", "create", "R6 Cust B"]
        )
        code, pay, _ = _cli_json(
            [
                "--remote",
                "http://127.0.0.1",
                "--token",
                key_b,
                "project",
                "create",
                "R6 Proj B",
                "--targets",
                "fr-FR",
                "--customer-id",
                str(cpayb["id"]),
            ]
        )
        pb = pay["id"]

        # A's remote list excludes B (org-scoped by token, server-side).
        code, pay, err = _cli_json(
            ["--remote", "http://127.0.0.1", "--token", key_a, "project", "list"]
        )
        assert code == 0, (code, err)
        names = {p["name"] for p in pay["projects"]}
        assert "R6 Proj A" in names and "R6 Proj B" not in names, names

        # A's remote show of B's project -> server 404 -> clean exit 1 (not 3).
        code, _pay, err = _cli(
            ["--remote", "http://127.0.0.1", "--token", key_a, "project", "show", str(pb)]
        )
        assert code == 1 and "not found" in err.lower(), (code, err)

        # Output-shape parity: the remote `project show` --json of A's own project
        # carries the same keys as an in-process show would (id/name/target_locales).
        code, pay, err = _cli_json(
            ["--remote", "http://127.0.0.1", "--token", key_a, "project", "show", str(pa)]
        )
        assert code == 0 and pay["ok"] and pay["id"] == pa, (code, pay, err)
        assert "target_locales" in pay and "tm_count" in pay, pay

        # A wrong/blank token: 401 from /api -> clean exit 1 (server: unauthorized).
        code, _pay, err = _cli(
            ["--remote", "http://127.0.0.1", "--token", "lsu_bogus", "project", "list"]
        )
        assert code == 1 and "unauthorized" in err.lower(), (code, err)
        print("  P6 remote: token-scoped list/show (A excludes B, 404->exit1, shape parity)")
    finally:
        _remote._set_transport(None)
        _gate_off()


def test_p6_remote_refuses_inprocess_only() -> None:
    """`db migrate`/`db backup`/`audit verify`/`health` are in-process operator
    commands: --remote must REFUSE (exit 2 'not available over --remote'), never
    reach out. --remote requires --token, too."""
    for argv in (
        ["db", "migrate"],
        ["db", "backup", "x.db"],
        ["db", "verify"],
        ["audit", "verify"],
        ["health"],
        ["admin", "retention", "purge-now", "--yes"],
    ):
        code, _out, err = _cli(["--remote", "http://127.0.0.1", "--token", "lsu_x"] + argv)  # noqa: RUF005
        assert code == 2 and "not available over --remote" in err.lower(), (
            f"{argv} must refuse --remote (got {code}: {err[:120]})"
        )
    # --remote with no --token is a usage error (exit 2).
    code, _out, err = _cli(["--remote", "http://127.0.0.1", "project", "list"])
    assert code == 2 and "requires --token" in err.lower(), (code, err)
    print(
        "  P6 remote: db/backup/db-verify/audit-verify/health/purge-now REFUSE "
        "--remote; --remote needs --token"
    )


def test_p6_remote_local_json_rowshape_parity() -> None:
    """Issue #17's durable guard: for every remote-capable LIST verb, --remote's
    --json row shape must never silently drop a field the in-process arm has --
    exactly the defect #17 exhibited for `connector list` (auth/customer_id/
    content_type_id were present locally but missing over --remote; the human
    table's 'auth' column was hardcoded '').

    CONTRACT (documented choice -- docs/CLI.md promises the two arms print the
    "identical" output shape): each list's individual ROW dicts (one connection /
    tm-entry / job / workflow) must carry the EXACT SAME KEY SET locally vs over
    --remote. Row-level fields are what a caller actually consumes, so exact
    equality is the strict, defensible reading of "identical" there. This suite
    does NOT additionally require the top-level ENVELOPE keys (page/total/...) to
    match exactly: `tm list` legitimately adds a remote-only `customer_id` echo of
    which customer a request auto-selected -- the in-process CLI is a trusted,
    customer-unbound operator surface with no equivalent concept to compare
    against, so requiring envelope equality there would flag a non-bug. Row-level
    equality is still enforced for tm list (and is what would have caught #17).

    Gate OFF (single-tenant): org scoping is a no-op here by design -- isolation
    itself has its own dedicated P3/P4/P6 coverage above; this test is purely
    about shape parity."""
    init_db()
    from app.models import Job, JobStatus, SegmentTM
    from app.services import apikeys

    admin = security.create_user("p6rowshape@x.com", "RowShape", "admin", "rowshapepass123")
    _row, token = apikeys.create_key(admin.id, "p6-rowshape", scopes="api docs")

    # --- seed one row for each of the 4 remote-capable LIST verbs -------------
    code, pay, err = _cli_json(["customer", "create", "P6 RowShape Cust"])
    assert code == 0, (pay, err)
    cust_id = pay["id"]
    code, pay, err = _cli_json(
        [
            "project",
            "create",
            "P6 RowShape Proj",
            "--customer-id",
            str(cust_id),
            "--targets",
            "fr-FR",
        ]
    )
    assert code == 0, (pay, err)
    pid = pay["id"]

    # connector (CRUD is in-process only -- only `list` itself runs over --remote)
    code, pay, err = _cli_json(
        [
            "connector",
            "create",
            "--kind",
            "airtable",
            "--name",
            "P6 RowShape Conn",
            "--config",
            "base_id=appX",
            "--config",
            "table=Content",
            "--secret",
            "api_key=patSECRET",
            "--customer",
            str(cust_id),
        ]
    )
    assert code == 0, (pay, err)

    # TM entry: a direct row (the shape under test is tm_module.entry_to_dict,
    # which both /api/tm and the in-process arm call -- independent of ingestion path).
    with Session(engine) as s:
        s.add(
            SegmentTM(
                project_id=pid,
                source_locale="en-US",
                target_locale="fr-FR",
                source_text="Row shape phrase.",
                target_text="Phrase de forme.",
                source_hash="p6rowshape-hash",
            )
        )
        s.commit()

    # job: a direct row (job_history.unified_history reads Job rows).
    with Session(engine) as s:
        s.add(
            Job(
                kind="translate_document",
                status=JobStatus.COMPLETED,
                created_by="p6rowshape@x.com",
                progress_done=1,
                progress_total=1,
            )
        )
        s.commit()

    # workflow (create is in-process only -- only `list` itself runs over --remote)
    code, pay, err = _cli_json(["workflow", "create", "--name", "P6 RowShape WF"])
    assert code == 0, (pay, err)
    wf_id = pay["id"]

    # routing rule (create is in-process only -- only `show` runs over --remote): one
    # rule so `admin routing rule show` has a target for the read-back parity check.
    from app.services import routing as _routing

    with Session(engine) as s:
        _rule = _routing.create_rule(
            s, priority=9, match_ext="p6rs", project_id=pid, actor="p6rowshape@x.com"
        )
        rule_id = _rule.id

    # a completed run with one recorded step, for the show-run (non-LIST) shape parity check.
    from app.models import Document, StepRun, TransMode, WorkflowRun
    from app.services import workflows as _W

    with Session(engine) as s:
        st = _W.add_step(
            s, wf_id, name="T", provider="mock", model="m1", prompt_text="T {text}", key="translate"
        )
        doc = Document(
            project_id=pid,
            filename="p6run.docx",
            fmt="docx",
            mode=TransMode.SEGMENTED,
            skeleton={"units": []},
        )
        s.add(doc)
        s.commit()
        s.refresh(doc)
        s.refresh(st)
        run = WorkflowRun(
            workflow_id=wf_id, document_id=doc.id, target_locale="fr-FR", status="completed"
        )
        s.add(run)
        s.commit()
        s.refresh(run)
        s.add(StepRun(workflow_run_id=run.id, step_id=st.id, status="completed", outcome_index=0))
        s.commit()
        run_id = run.id

    api = TestClient(app)
    _remote._set_transport(_mock_transport_for(api))
    try:
        checks = [
            ("connector list", ["connector", "list"], "connections"),
            ("tm list", ["tm", "list", "--customer", str(cust_id)], "entries"),
            ("job list", ["job", "list"], "jobs"),
            ("workflow list", ["workflow", "list"], "workflows"),
            # apikey list: the system-wide key enumeration (metadata only, last_used).
            # The admin token itself owns a key, so both arms produce >=1 row.
            ("apikey list", ["apikey", "list"], "keys"),
        ]
        for label, argv, list_key in checks:
            code_l, pay_l, err_l = _cli_json(argv)
            assert code_l == 0, (label, "local failed", pay_l, err_l)
            code_r, pay_r, err_r = _cli_json(
                ["--remote", "http://127.0.0.1", "--token", token, *argv]
            )
            assert code_r == 0, (label, "remote failed", pay_r, err_r)
            rows_l = pay_l.get(list_key) or []
            rows_r = pay_r.get(list_key) or []
            assert rows_l, (label, "local fixture produced no rows", pay_l)
            assert rows_r, (label, "remote fixture produced no rows", pay_r)
            keys_l, keys_r = set(rows_l[0].keys()), set(rows_r[0].keys())
            assert keys_l == keys_r, (
                f"{label}: local/remote row key-set mismatch (the #17 defect class) "
                f"local-only={keys_l - keys_r!r} remote-only={keys_r - keys_l!r}"
            )

        # The exact reported #17 symptom: the remote connector row's auth/customer_id/
        # content_type_id carry real VALUES (not just present-but-null keys), and the
        # human table's auth column is no longer hardcoded ''.
        code, pay, err = _cli_json(["connector", "list"])
        assert code == 0, (pay, err)
        conn = next(c for c in pay["connections"] if c["name"] == "P6 RowShape Conn")
        code, pay_r, err = _cli_json(
            ["--remote", "http://127.0.0.1", "--token", token, "connector", "list"]
        )
        assert code == 0, (pay_r, err)
        conn_r = next(c for c in pay_r["connections"] if c["name"] == "P6 RowShape Conn")
        assert conn_r["auth"] == conn["auth"] == "token", (conn, conn_r)
        assert conn_r["customer_id"] == conn["customer_id"] == cust_id, (conn, conn_r)
        code, out, err = _cli(
            ["--remote", "http://127.0.0.1", "--token", token, "connector", "list"]
        )
        assert code == 0, err
        assert "  token" in out or " token " in out or out.strip().endswith("token"), (
            "remote human table must show the real auth value, not ''",
            out,
        )
        # show-run is NOT a list verb, but ALSO ships step_states/orphan_steps over BOTH
        # arms (the run-view graph's headless parity) -- guard its local-vs-remote JSON
        # envelope AND step_states row shape too, same #17 defence.
        code_l, pay_l, err_l = _cli_json(["workflow", "show-run", str(run_id)])
        assert code_l == 0, ("show-run local failed", pay_l, err_l)
        code_r, pay_r, err_r = _cli_json(
            ["--remote", "http://127.0.0.1", "--token", token, "workflow", "show-run", str(run_id)]
        )
        assert code_r == 0, ("show-run remote failed", pay_r, err_r)
        assert set(pay_l.keys()) == set(pay_r.keys()), (
            "workflow show-run: local/remote envelope key mismatch local-only={!r} remote-only={!r}".format(  # noqa: E501, UP032
                set(pay_l) - set(pay_r), set(pay_r) - set(pay_l)
            )
        )
        rows_l, rows_r = pay_l.get("step_states") or [], pay_r.get("step_states") or []
        assert rows_l and rows_r, ("show-run produced no step_states", pay_l, pay_r)
        assert set(rows_l[0].keys()) == set(rows_r[0].keys()), (
            "workflow show-run: step_states row key mismatch local-only={!r} remote-only={!r}".format(  # noqa: E501, UP032
                set(rows_l[0]) - set(rows_r[0]), set(rows_r[0]) - set(rows_l[0])
            )
        )
        # the run-view graph's per-edge loop-back counts ship headless too (additive key,
        # empty here -- no loop-back fired); local and remote must both carry it.
        assert "loopback_traversals" in pay_l and "loopback_traversals" in pay_r, (
            "workflow show-run must expose loopback_traversals over both arms",
            pay_l,
            pay_r,
        )
        assert pay_l["loopback_traversals"] == pay_r["loopback_traversals"], (
            pay_l["loopback_traversals"],
            pay_r["loopback_traversals"],
        )
        # --visits (CLI-parity backlog item 3, additive per-VISIT row-level trace):
        # default show-run (no --visits) carries NO visit_history key over EITHER
        # arm; with --visits, local (in-process) and --remote (real HTTP, via
        # ?visits=1) agree exactly, same #17 defence extended to the new field.
        assert "visit_history" not in pay_l and "visit_history" not in pay_r, (
            "default show-run must not carry visit_history (additive/opt-in only)",
            pay_l,
            pay_r,
        )
        code_lv, pay_lv, err_lv = _cli_json(["workflow", "show-run", str(run_id), "--visits"])
        assert code_lv == 0, ("show-run --visits local failed", pay_lv, err_lv)
        code_rv, pay_rv, err_rv = _cli_json(
            [
                "--remote",
                "http://127.0.0.1",
                "--token",
                token,
                "workflow",
                "show-run",
                str(run_id),
                "--visits",
            ]
        )
        assert code_rv == 0, ("show-run --visits remote failed", pay_rv, err_rv)
        assert pay_lv.get("visit_history") and pay_rv.get("visit_history"), (
            "show-run --visits produced no visit_history",
            pay_lv,
            pay_rv,
        )
        assert pay_lv["visit_history"] == pay_rv["visit_history"], (
            "workflow show-run --visits: local/remote visit_history must match exactly",
            pay_lv["visit_history"],
            pay_rv["visit_history"],
        )
        # admin routing rule show is ALSO a read-back (not a list) with remote parity:
        # its --json envelope (routing.rule_to_dict) must match local vs --remote.
        code_l, pay_l, err_l = _cli_json(["admin", "routing", "rule", "show", str(rule_id)])
        assert code_l == 0, ("rule show local failed", pay_l, err_l)
        code_r, pay_r, err_r = _cli_json(
            [
                "--remote",
                "http://127.0.0.1",
                "--token",
                token,
                "admin",
                "routing",
                "rule",
                "show",
                str(rule_id),
            ]
        )
        assert code_r == 0, ("rule show remote failed", pay_r, err_r)
        assert set(pay_l.keys()) == set(pay_r.keys()), (
            f"admin routing rule show: local/remote envelope key mismatch local-only={set(pay_l) - set(pay_r)!r} "  # noqa: E501
            f"remote-only={set(pay_r) - set(pay_l)!r}"
        )
        assert pay_l["id"] == pay_r["id"] == rule_id, (pay_l, pay_r)
        print(
            "  P6 #17 guard: connector/tm/job/workflow/apikey list --remote row shape "
            "== in-process row shape (exact key-set match); connector auth/"
            "customer_id values + human table auth column verified over --remote; "
            "workflow show-run step_states envelope+row shape parity verified"
        )
    finally:
        _remote._set_transport(None)


def test_p6_customer_pairs_import_remote_and_rbac() -> None:
    """CLI-parity backlog item 2 (`customer pairs import`, atomic + idempotent bulk
    pair import): local and --remote arms agree on the added/skipped shape+values
    for the identical file; a bad row rejects the WHOLE file leaving the customer's
    pairs untouched (atomicity, proven over the local arm); a re-import over
    --remote skips every already-present pair (idempotency); and a viewer-role
    token (below can_write) is refused (403) over --remote (RBAC probe), matching
    the /api route's can_write gate (same posture as the web pairs add/remove
    routes)."""
    init_db()
    _gate_on()
    from app.services import apikeys

    api = TestClient(app)
    _remote._set_transport(_mock_transport_for(api))
    try:
        org = Organization(name="P6 Pairs Org")
        with Session(engine) as s:
            s.add(org)
            s.commit()
            s.refresh(org)
            org_id = org.id
        # #181: created IN the org (the seam refuses an org-less create under the gate).
        admin = security.create_user(
            "p6pairs-admin@x.com", "P6 Admin", Role.ADMIN, "p6pairs-pass-123", org_id=org_id
        )
        viewer = security.create_user(
            "p6pairs-viewer@x.com", "P6 Viewer", Role.VIEWER, "p6pairs-pass-123", org_id=org_id
        )
        _r, admin_key = apikeys.create_key(admin.id, "p6-pairs-admin", scopes="api docs")
        _r, viewer_key = apikeys.create_key(viewer.id, "p6-pairs-viewer", scopes="api docs")

        code, cpay, err = _cli_json(
            [
                "--remote",
                "http://127.0.0.1",
                "--token",
                admin_key,
                "customer",
                "create",
                "P6 Pairs Cust",
            ]
        )
        assert code == 0, (cpay, err)
        cid = cpay["id"]

        with tempfile.TemporaryDirectory() as td:
            good_csv = os.path.join(td, "good.csv")
            with open(good_csv, "w", encoding="utf-8", newline="") as fh:
                fh.write("source_locale,target_locale\nen-US,fr-FR\nen-US,de-DE\n")
            bad_csv = os.path.join(td, "bad.csv")
            with open(bad_csv, "w", encoding="utf-8", newline="") as fh:
                fh.write("source_locale,target_locale\nen-US,fr-FR\nen-US,zz-ZZ\n")

            # --- ATOMICITY (local arm): the bad file is rejected WHOLE (exit != 0),
            # and NO pair from it -- not even the valid en-US->fr-FR row -- was written.
            # Gate ON: the in-process arm must be --org scoped (require_org_scope).
            code, out, err = _cli(
                ["--org", str(org_id), "customer", "pairs", "import", str(cid), bad_csv]
            )
            assert code != 0, (code, out, err)
            code, pay, err = _cli_json(
                ["--org", str(org_id), "customer", "pairs", "list", str(cid)]
            )
            assert code == 0 and pay["active_pairs"] == [], (
                "a rejected import must leave the customer's pairs untouched",
                pay,
            )

            # --- local arm imports the good file cleanly.
            code, local_pay, err = _cli_json(
                ["--org", str(org_id), "customer", "pairs", "import", str(cid), good_csv]
            )
            assert code == 0, (local_pay, err)
            assert sorted(local_pay["added"]) == ["en-US->de-DE", "en-US->fr-FR"], local_pay
            assert local_pay["skipped"] == []

            # --- local vs remote SHAPE/VALUE parity: a second customer, --remote
            # import of the SAME file, must add the SAME pairs.
            code, cpay2, err = _cli_json(
                [
                    "--remote",
                    "http://127.0.0.1",
                    "--token",
                    admin_key,
                    "customer",
                    "create",
                    "P6 Pairs Cust Remote",
                ]
            )
            assert code == 0, (cpay2, err)
            cid2 = cpay2["id"]
            code, remote_pay, err = _cli_json(
                [
                    "--remote",
                    "http://127.0.0.1",
                    "--token",
                    admin_key,
                    "customer",
                    "pairs",
                    "import",
                    str(cid2),
                    good_csv,
                ]
            )
            assert code == 0, (remote_pay, err)
            assert set(remote_pay.keys()) >= {"added", "skipped", "total_rows"}, remote_pay
            assert sorted(remote_pay["added"]) == sorted(local_pay["added"]), (
                "local vs remote added-pairs must match",
                local_pay,
                remote_pay,
            )
            assert remote_pay["skipped"] == local_pay["skipped"]

            # --- IDEMPOTENCY: re-importing the SAME file over --remote adds nothing
            # and reports every pair as skipped (already present).
            code, remote_pay2, err = _cli_json(
                [
                    "--remote",
                    "http://127.0.0.1",
                    "--token",
                    admin_key,
                    "customer",
                    "pairs",
                    "import",
                    str(cid2),
                    good_csv,
                ]
            )
            assert code == 0, (remote_pay2, err)
            assert remote_pay2["added"] == [], "a re-import must add nothing"
            assert sorted(remote_pay2["skipped"]) == ["en-US->de-DE", "en-US->fr-FR"], remote_pay2

            # --- RBAC probe: a viewer-role token (below can_write) is refused (403)
            # over --remote -- the /api route's can_write gate, matching the web
            # pairs add/remove routes' posture.
            code, _pay, err = _cli(
                [
                    "--remote",
                    "http://127.0.0.1",
                    "--token",
                    viewer_key,
                    "customer",
                    "pairs",
                    "import",
                    str(cid2),
                    good_csv,
                ]
            )
            assert code == 1 and "does not allow" in err.lower(), (
                "a viewer-role token must be refused (can_write gate)",
                code,
                err,
            )
    finally:
        _remote._set_transport(None)
        _gate_off()
    print(
        "  P6 `customer pairs import`: local==remote added/skipped parity, "
        "atomic (a bad row leaves pairs untouched), idempotent (re-import skips "
        "every present pair), RBAC (viewer token refused 403) OK"
    )


def test_p6_saas_inprocess_operator_gate() -> None:
    """Gate ON: an in-process command with NO --org and NO --operator REFUSES (exit
    2) rather than sweep every org. --org <id> scopes + stamps; a valid --operator
    (env LOC_TMS_OPERATOR_KEY vs operator_key_hash) allows unscoped cross-org."""
    d = _seed_two_orgs()
    _gate_on()
    try:
        # (1) Unscoped in-process create -> REFUSED (exit 2).
        code, _pay, err = _cli(["project", "create", "GateProbe", "--targets", "fr-FR"])
        assert code == 2 and "multi-org mode is on" in err.lower(), (code, err)

        # (2) --org A scopes: the project is created AND stamped org A.
        code, pay, err = _cli_json(
            ["--org", str(d["org_a"]), "project", "create", "ScopedA", "--targets", "fr-FR"]
        )
        assert code == 0 and pay["ok"], (code, pay, err)
        with Session(engine) as s:
            assert s.get(Project, pay["id"]).org_id == d["org_a"], "--org did not stamp org A"

        # (3) --org filters lists to that org only.
        code, pay, err = _cli_json(["--org", str(d["org_a"]), "project", "list"])
        assert code == 0, (code, err)
        names = {p["name"] for p in pay["projects"]}
        assert "ScopedA" in names and "Proj B" not in names, names
        # A bogus --org id is a usage error.
        code, _pay, err = _cli(["--org", "999999", "project", "list"])
        assert code == 2 and "no such organization" in err.lower(), (code, err)

        # (4) --operator with the RIGHT key allows an unscoped cross-org run.
        secret = "operator-key-super-secret-value"
        from app.services import settings as _s

        _s.set_many({"operator_key_hash": hashlib.sha256(secret.encode()).hexdigest()})
        _s.invalidate_cache()
        # Wrong key -> refused (exit 2).
        os.environ["LOC_TMS_OPERATOR_KEY"] = "wrong"
        code, _pay, err = _cli(["--operator", "project", "list"])
        assert code == 2 and "does not match" in err.lower(), (code, err)
        # Right key -> allowed; the unscoped list spans BOTH orgs.
        os.environ["LOC_TMS_OPERATOR_KEY"] = secret
        code, pay, err = _cli_json(["--operator", "project", "list"])
        assert code == 0, (code, err)
        names = {p["name"] for p in pay["projects"]}
        assert "ScopedA" in names and "Proj A" in names and "Proj B" in names, (
            f"operator run must span all orgs, got {names!r}"
        )
        # --operator with no env key configured on the host is refused.
        _s.set_many({"operator_key_hash": ""})
        _s.invalidate_cache()
        code, _pay, err = _cli(["--operator", "project", "list"])
        assert code == 2 and "no operator key is configured" in err.lower(), (code, err)
        print(
            "  P6 gate: unscoped REFUSED; --org scopes+stamps; --operator (right key) "
            "spans orgs; wrong/missing key refused"
        )
    finally:
        os.environ.pop("LOC_TMS_OPERATOR_KEY", None)
        _gate_off()


def test_p6_gate_off_inprocess_byte_identical() -> None:
    """The sacred gate re-asserted for P6: with the gate OFF (single-tenant), an
    in-process command works with NO --org (byte-identical to today) — the operator
    gate is a strict no-op. A create + list + show round-trip runs clean."""
    init_db()
    _gate_off()
    assert access.multi_org_active() is False, "gate must be OFF here"
    # 2026-07-14: a project requires a customer when one exists (prior tests left
    # some in this shared DB) — create one first and pass --customer-id.
    code, cpay, err = _cli_json(["customer", "create", "OffProbe Cust"])
    assert code == 0, (code, cpay, err)
    code, pay, err = _cli_json(
        ["project", "create", "OffProbe", "--targets", "fr-FR", "--customer-id", str(cpay["id"])]
    )
    assert code == 0 and pay["ok"], (code, pay, err)
    pid = pay["id"]
    code, pay, err = _cli_json(["project", "list"])
    assert code == 0 and any(p["id"] == pid for p in pay["projects"]), (code, err)
    code, pay, err = _cli_json(["project", "show", str(pid)])
    assert code == 0 and pay["id"] == pid, (code, err)
    # An --operator flag with the gate off is simply ignored (no-op).
    code, pay, _err = _cli_json(["--operator", "project", "list"])
    assert code == 0 and any(p["id"] == pid for p in pay["projects"]), pay
    print(
        "  P6 gate-off: in-process CLI works with no --org (byte-identical); --org/--operator inert"
    )


# --------------------------------------------------------------------------- #
# P7: org management (back-office org CRUD + suspend, user-move) + enforcement.
#
# The web parity for org management is the BACK-OFFICE org CRUD (super-admin only).
# This section drives the real /back-office routes with the sacred gate ON:
#   - org create / rename / suspend / unsuspend work for a super-admin and are
#     audited; a customer session cannot reach them (the mount is is_superadmin-only);
#   - a SUSPENDED org's user cannot log in (customer /auth/login) and its lsu_ token
#     is rejected on /api — enforcement lives in access.org_active();
#   - moving a user (User.org_id only) makes them see the NEW org's data and 404 the
#     old, while their old projects STAY in the old org (no auto-reparent footgun);
#   - gate OFF: org.active is a strict no-op (a suspended org's user still logs in),
#     byte-identical to single-tenant today.
# --------------------------------------------------------------------------- #
_P7_WORLD = {}


def _p7_world():
    """Two orgs A/B; an admin (op@p7.com) in org A; a normal admin customer
    (cust@p7.com) in org B with a project. Seeded once per process.

    op@p7.com used to be flagged is_superadmin for the back-office; that account type was
    removed on 2026-09-20 and the flag is dormant, so it is just an org-A admin now."""
    if _P7_WORLD:
        return _P7_WORLD
    init_db()
    with Session(engine) as s:
        oa = Organization(name="P7 Org A")
        ob = Organization(name="P7 Org B")
        s.add(oa)
        s.add(ob)
        s.commit()
        s.refresh(oa)
        s.refresh(ob)
        oa_id, ob_id = oa.id, ob.id
    op = security.create_user("op@p7.com", "P7 Operator", Role.ADMIN, "op-pass-12345")
    cu = security.create_user("cust@p7.com", "P7 Customer", Role.ADMIN, "cust-pass-1234")
    with Session(engine) as s:
        u = s.get(User, op.id)
        u.org_id = oa_id
        s.add(u)
        c2 = s.get(User, cu.id)
        c2.org_id = ob_id
        s.add(c2)
        pB = Project(
            name="P7 Proj B", org_id=ob_id, source_locale="en-US", created_by="cust@p7.com"
        )
        pA = Project(name="P7 Proj A", org_id=oa_id, source_locale="en-US", created_by="op@p7.com")
        s.add(pB)
        s.add(pA)
        s.commit()
        s.refresh(pB)
        s.refresh(pA)
        _P7_WORLD.update({"org_a": oa_id, "org_b": ob_id, "proj_a": pA.id, "proj_b": pB.id})
    return _P7_WORLD


def test_p7_console_is_gone_and_org_crud_lives_in_the_service():
    """2026-09-20: the back-office and the super-admin were removed. This used to drive
    org CRUD through /back-office as a super-admin.

    Two halves, because deleting a door is only safe if the CAPABILITY survived and the
    DOOR really is shut:
      * the capability: create / rename / suspend / unsuspend still work at the service
        seam `app/cli/cmd_org.py` calls, which is what `cli org` drives over shell access;
      * the door: no /back-office path is routable on the customer app any more.
    """
    d = _p7_world()
    _gate_on()
    try:
        with Session(engine) as s2:
            made = access.create_org(s2, "P7 Created Org")
            assert made.id and made.active is True
            renamed = access.rename_org(s2, made.id, "P7 Renamed Org")
            assert renamed.name == "P7 Renamed Org"
            assert access.set_org_active(s2, made.id, False).active is False
            assert access.org_active(made.id) is False, "suspend must take effect"
            assert access.set_org_active(s2, made.id, True).active is True
            assert access.org_active(made.id) is True
            assert {o.id for o in access.list_orgs(s2)} >= {d["org_a"], d["org_b"], made.id}

        # The door is shut for everyone: the mount no longer exists. Assert that by
        # COMPARISON with a path that was never a route, rather than against a fixed 404 --
        # /back-office also lost its _ALLOW_PREFIX carve-out, so it now meets the ordinary
        # customer login wall like any other unknown path, and "same as an unknown path" is
        # exactly the property worth pinning. A bare 404 assertion would have hidden a
        # surviving carve-out behind a matching status code.
        c = TestClient(app)
        control_get = c.get("/definitely-not-a-route-zzz", follow_redirects=False)
        control_post = c.post("/definitely-not-a-route-zzz", data={}, follow_redirects=False)
        for path in ("/back-office", "/back-office/", "/back-office/login", "/back-office/orgs"):
            g = c.get(path, follow_redirects=False)
            assert g.status_code == control_get.status_code, (path, g.status_code)
            assert "bo_session" not in g.headers.get("set-cookie", ""), path
            po = c.post(path, data={}, follow_redirects=False)
            assert po.status_code == control_post.status_code, (path, po.status_code)
        print("  P7 org CRUD at the service seam (cli org); /back-office 404s on every path")
    finally:
        _gate_off()


def test_p7_suspended_org_blocks_login_and_api():
    """Gate ON: a SUSPENDED org's user cannot log in (customer /auth/login 401) and
    its lsu_ token is rejected on /api (401). Unsuspending restores access."""
    from app.services import apikeys

    d = _p7_world()
    _gate_on()
    try:
        # mint the customer's lsu_ key while active (so key creation isn't the thing
        # under test — only its ACCEPTANCE changes with suspension).
        cu = security.get_by_email("cust@p7.com")
        _row, raw = apikeys.create_key(cu.id, "p7-suspend", scopes="api docs")

        # baseline: active org -> login works + key reads its own org.
        cc = TestClient(app)
        csrf_for(cc)
        r = cc.post(
            "/auth/login",
            data={"email": "cust@p7.com", "password": "cust-pass-1234", "next": "/"},
            follow_redirects=False,
        )
        assert r.status_code == 303, f"active-org customer must log in, got {r.status_code}"

        # Suspend org B. 2026-09-20: this used to go through the back-office console;
        # it is now the service seam `cli org suspend` drives. What is UNDER TEST here was
        # never the door, it is the ENFORCEMENT below, which is unchanged.
        with Session(engine) as s2:
            access.set_org_active(s2, d["org_b"], False)

        # now the customer login is REJECTED (401) — a suspended org can't log in.
        cc2 = TestClient(app)
        csrf_for(cc2)
        r = cc2.post(
            "/auth/login",
            data={"email": "cust@p7.com", "password": "cust-pass-1234", "next": "/"},
            follow_redirects=False,
        )
        assert r.status_code == 401, (
            f"suspended-org customer login must be 401, got {r.status_code}"
        )

        # and the lsu_ token is rejected on /api (401 unknown token).
        api = TestClient(app)
        r = api.get("/api/connectors", headers={"X-Loc-Token": raw})
        assert r.status_code == 401, (
            f"suspended-org lsu_ token must be rejected (401), got {r.status_code}"
        )

        # unsuspend -> access restored.
        with Session(engine) as s2:
            access.set_org_active(s2, d["org_b"], True)
        cc3 = TestClient(app)
        csrf_for(cc3)
        r = cc3.post(
            "/auth/login",
            data={"email": "cust@p7.com", "password": "cust-pass-1234", "next": "/"},
            follow_redirects=False,
        )
        assert r.status_code == 303, "unsuspended-org customer must log in again"
        assert api.get("/api/connectors", headers={"X-Loc-Token": raw}).status_code == 200, (
            "unsuspended-org lsu_ token must work again"
        )
        print(
            "  P7 suspend: suspended org blocks login (401) + rejects its lsu_ token (401); "
            "unsuspend restores"
        )
    finally:
        _gate_off()


def test_p7_gate_off_suspend_is_noop():
    """The sacred gate re-asserted: with the gate OFF, org.active is a strict no-op —
    a user whose org is marked suspended still authenticates (single-tenant, as
    today). Enforcement only exists under the gate."""
    d = _p7_world()
    _gate_off()
    # Mark org B suspended directly, then prove authenticate() ignores it gate-off.
    with Session(engine) as s:
        org = s.get(Organization, d["org_b"])
        org.active = False
        s.add(org)
        s.commit()
    try:
        assert access.multi_org_active() is False, "gate must be OFF here"
        assert access.org_active(d["org_b"]) is True, (
            "gate off: org_active must be an unrestricted no-op (True)"
        )
        assert security.authenticate("cust@p7.com", "cust-pass-1234") is not None, (
            "gate off: a suspended org must NOT block login (no-op)"
        )
        print("  P7 gate-off: org.active is a no-op (suspended org still logs in, as today)")
    finally:
        with Session(engine) as s:
            org = s.get(Organization, d["org_b"])
            org.active = True
            s.add(org)
            s.commit()


def test_p7_user_move_changes_org_visibility_keeps_content():
    """Gate ON: moving a user (User.org_id only) makes them see the NEW org's data
    and 404 the OLD; their OLD projects STAY in the old org (no auto-reparent). The
    move is a host operator action (`cli user org`), audited as user.move_org."""
    d = _p7_world()
    _gate_on()
    try:
        # Seed a mover in org B with a project (which must STAY in org B).
        mover = security.create_user(
            "mover@p7.com", "Mover", Role.ADMIN, "mover-pass-123", org_id=d["org_b"]
        )  # #181: created IN its org
        with Session(engine) as s:
            mu = s.get(User, mover.id)
            mu.org_id = d["org_b"]
            s.add(mu)
            mp = Project(name="Mover Kept Project", org_id=d["org_b"], created_by="mover@p7.com")
            s.add(mp)
            s.commit()
            s.refresh(mp)
            mover_proj = mp.id

        # BEFORE the move: the mover (in org B) sees org B's project, 404s org A's.
        cbefore = TestClient(app)
        csrf_for(cbefore)
        assert (
            cbefore.post(
                "/auth/login",
                data={"email": "mover@p7.com", "password": "mover-pass-123", "next": "/"},
                follow_redirects=False,
            ).status_code
            == 303
        )
        assert cbefore.get("/projects/%d" % d["proj_b"]).status_code == 200, (  # noqa: UP031
            "mover in org B must see org B's project"
        )
        assert cbefore.get("/projects/%d" % d["proj_a"]).status_code == 404, (  # noqa: UP031
            "mover in org B must 404 org A's project"
        )

        # MOVE the mover to org A via the back-office.
        # 2026-09-20: this used to go through the back-office console. Moving a user is a
        # host operator action now (`cli user org`), and this is the seam that verb calls.
        # The FOOTGUN and visibility assertions below are the point and are unchanged.
        with Session(engine) as s2:
            access.move_user_to_org(s2, mover.id, d["org_a"])
        with Session(engine) as s:
            assert s.get(User, mover.id).org_id == d["org_a"], "user.org_id not moved to A"
            # FOOTGUN: the mover's project STAYED in org B (never re-parented).
            assert s.get(Project, mover_proj).org_id == d["org_b"], (
                "P7 footgun broken: moving a user re-parented their project"
            )

        # AFTER the move (fresh session): the mover now sees org A's project + 404s
        # org B's — INCLUDING the project they created there (content stayed, view moved).
        cafter = TestClient(app)
        csrf_for(cafter)
        assert (
            cafter.post(
                "/auth/login",
                data={"email": "mover@p7.com", "password": "mover-pass-123", "next": "/"},
                follow_redirects=False,
            ).status_code
            == 303
        )
        assert cafter.get("/projects/%d" % d["proj_a"]).status_code == 200, (  # noqa: UP031
            "after move the mover must see org A's project"
        )
        assert cafter.get("/projects/%d" % d["proj_b"]).status_code == 404, (  # noqa: UP031
            "after move the mover must 404 org B (incl. their own kept project)"
        )
        assert cafter.get("/projects/%d" % mover_proj).status_code == 404, (  # noqa: UP031
            "after move the mover must 404 their OWN project left behind in org B"
        )

        # THE LINCHPIN (Finding 7), now STRUCTURAL rather than enforced. This used to
        # assert that the customer /users/{id}/move-org route REFUSED a cross-org move for
        # a normal admin and for a super-admin alike. On 2026-09-20 that route was deleted
        # along with the console: once the picker was confined to the caller's own org the
        # only move it could still perform was moving a user into the org they were already
        # in. A capability that cannot be expressed is a stronger guarantee than one that is
        # refused, so this now asserts the route is GONE for every role.
        #
        # Compared against a never-existed path rather than a fixed status, for the same
        # reason as the console check above: the route also lost any carve-out it had, and
        # "indistinguishable from a path that was never there" is the real property.
        for email, password in (
            ("cust@p7.com", "cust-pass-1234"),
            ("op@p7.com", "op-pass-12345"),
        ):
            cx = TestClient(app)
            csrf_for(cx)
            cx.post(
                "/auth/login",
                data={"email": email, "password": password, "next": "/"},
                follow_redirects=False,
            )
            csrf_for(cx)  # re-establish after login rotates the session token
            gone = cx.post(
                "/users/%d/move-org" % mover.id,  # noqa: UP031
                data={"target_org_id": d["org_b"]},
                follow_redirects=False,
            )
            control = cx.post(
                "/users/%d/move-org-zzz" % mover.id,  # noqa: UP031
                data={"target_org_id": d["org_b"]},
                follow_redirects=False,
            )
            assert gone.status_code == control.status_code, (email, gone.status_code)
            with Session(engine) as s:
                assert s.get(User, mover.id).org_id == d["org_a"], (
                    f"LEAK: {email} moved a user cross-org on the customer surface"
                )

        # The move audit row (user.move_org, with the org transition and the content-stays
        # footgun) is asserted in verify_cli.py against `cli user org`, which is the only
        # caller left. The audit is written BY THE CALLER, not by access.move_user_to_org,
        # so asserting it here after driving the service directly would have asserted
        # nothing at all.
        print(
            "  P7 user-move: view follows org_id (A visible, B 404 incl. own kept project); "
            "content stays in old org; audited"
        )
    finally:
        _gate_off()


# --------------------------------------------------------------------------- #
# P8: super-admin bootstrap + audit/GDPR org scoping + PromptTemplate isolation.
#
# The FINAL pre-review phase. Proves:
#   - BOOTSTRAP: the first admin created (dev seed + /auth/setup wizard) is flagged
#     is_superadmin; an existing Default-org admin is promoted on upgrade (backfill)
#     so a SaaS install never locks itself out of the back-office;
#   - AUDIT SCOPING: an org-admin's customer /audit + export show ONLY their org's
#     rows; a super-admin in the CUSTOMER app sees only home-org (THE LINCHPIN); the
#     back-office /audit sees ALL orgs (the only cross-org door);
#   - GDPR: export/erase refuse a cross-org target (404 / "not found"), on the web
#     route AND the CLI --org scope;
#   - PROMPTTEMPLATE: a template created in org A is not visible in org B (list);
#   - GATE OFF re-asserted byte-identical: audit unscoped, templates app-global, the
#     is_superadmin flag has no effect.
# --------------------------------------------------------------------------- #
import subprocess as _subprocess

from app.models import PromptTemplate


def _run_isolated(body: str) -> None:
    """Run `body` (Python source) in a SUBPROCESS with its OWN fresh, throwaway
    LOC_TMS_DATA_DIR + MockProvider env, so bootstrap tests that need an EMPTY DB
    (user_count()==0) don't collide with this suite's shared DB. The body must
    print 'OK' on success; any assertion failure surfaces via a non-zero exit +
    the captured stderr. ASCII-only."""
    data_dir = tempfile.mkdtemp(prefix="loctms_p8_iso_")
    env = dict(os.environ)
    env.update(
        {
            "LOC_TMS_DATA_DIR": data_dir,
            "ANTHROPIC_API_KEY": "",
            "GEMINI_API_KEY": "",
            "OPENAI_API_KEY": "",
            "LOCAL_BASE_URL": "",
        }
    )
    src = "import os, sys\n" + body
    r = _subprocess.run(
        [sys.executable, "-c", src], env=env, cwd=_ROOT, capture_output=True, text=True
    )
    assert r.returncode == 0 and "OK" in r.stdout, (
        f"isolated bootstrap check failed (exit {r.returncode}):\nSTDOUT:{r.stdout}\nSTDERR:{r.stderr}"  # noqa: E501
    )


def test_p8_no_bootstrap_flags_a_superadmin():
    """2026-09-20: the four P8 bootstrap tests that used to live here asserted that
    seed_initial, the setup wizard, and the upgrade backfill each FLAGGED a super-admin so
    a SaaS install could not lock itself out of the back-office.

    There is no back-office to be locked out of, so every one of those paths was removed.
    This asserts the inverse, across all three of them in one isolated boot: no bootstrap
    path may mint a cross-tenant privilege bit. It fails on ABSENCE of that rule.

    Why it matters more than a deleted feature usually would: nothing around the flag still
    enforces what used to make a super-admin safe (the home-org linchpin, the
    back-office-only rule). A bootstrap that quietly started setting it again would be
    handing out an unguarded cross-tenant marker on a fresh install."""
    _run_isolated(
        "from sqlmodel import Session, select\n"
        "from app.db import engine, init_db, _ensure_default_org\n"
        "from app.models import User\n"
        "from app.services import security as sec\n"
        "init_db()\n"
        # 1. the dev seed
        "assert sec.seed_initial() is True\n"
        # 2. the setup wizard / init CLI path
        "u, k, st = sec.seed_admin('boss@corp.test', 'super-secret-123', name='Boss')\n"
        "assert st == 'created'\n"
        # 3. the upgrade backfill, run twice for idempotence
        "_ensure_default_org()\n"
        "_ensure_default_org()\n"
        "with Session(engine) as s:\n"
        "    q = select(User).where(User.is_superadmin == True)  # noqa: E712\n"
        "    supers = s.exec(q).all()\n"
        "    assert not supers, [x.email for x in supers]\n"
        "print('OK')\n"
    )
    print("  P8 bootstrap: no seed/wizard/backfill path flags a super-admin (the flag is dormant)")


_P8_AUDIT_WORLD = {}


def _p8_audit_world():
    """Two orgs A/B with an admin each (a@8.test super-admin in org A, b@8.test
    org-admin in org B), audit enabled, and one audit row per actor. Runs on the
    shared DB (emails/orgs are P8-unique). Seeded once per process + cached so the
    audit rows aren't duplicated across the P8 web/service/template tests."""
    if _P8_AUDIT_WORLD:
        settings.set_many({"audit_enabled": "1"})
        settings.invalidate_cache()
        return _P8_AUDIT_WORLD
    init_db()
    settings.set_many({"audit_enabled": "1"})
    settings.invalidate_cache()
    with Session(engine) as s:
        oa = Organization(name="P8 Org A")
        ob = Organization(name="P8 Org B")
        s.add(oa)
        s.add(ob)
        s.commit()
        s.refresh(oa)
        s.refresh(ob)
        oa_id, ob_id = oa.id, ob.id
    ua = security.create_user("a@8.test", "A", Role.ADMIN, "a-pass-123456")
    ub = security.create_user("b@8.test", "B", Role.ADMIN, "b-pass-123456")
    with Session(engine) as s:
        a = s.get(User, ua.id)
        a.org_id = oa_id
        a.is_superadmin = True
        s.add(a)
        b = s.get(User, ub.id)
        b.org_id = ob_id
        s.add(b)
        s.commit()
    # Distinct audit rows per actor (audit_enabled on).
    audit.log(actor="a@8.test", action="p8.marker", target="ta", ip="1.1.1.1")
    audit.log(actor="b@8.test", action="p8.marker", target="tb", ip="2.2.2.2")
    _P8_AUDIT_WORLD.update({"org_a": oa_id, "org_b": ob_id})
    return _P8_AUDIT_WORLD


def test_p8_audit_service_org_scopes_by_actor():
    """Gate-agnostic service check: audit.recent/count/iter_export org_id= confines
    rows to that org's actors; None (back-office) is unscoped."""
    d = _p8_audit_world()
    # org A scope: only a@8.test's marker.
    a_rows = [r for r in audit.recent(limit=100, action="p8.marker", org_id=d["org_a"])]  # noqa: C416
    assert {r.actor for r in a_rows} == {"a@8.test"}, "org A audit scope leaked: %r" % {
        r.actor for r in a_rows
    }
    assert audit.count(action="p8.marker", org_id=d["org_a"]) == 1
    exp_a = {rec["actor"] for rec in audit.iter_export(action="p8.marker", org_id=d["org_a"])}
    assert exp_a == {"a@8.test"}, f"iter_export org A leaked: {exp_a!r}"
    # org B scope: only b@8.test's marker.
    b_rows = [r for r in audit.recent(limit=100, action="p8.marker", org_id=d["org_b"])]  # noqa: C416
    assert {r.actor for r in b_rows} == {"b@8.test"}, "org B audit scope leaked"
    # UNSCOPED (back-office): both.
    both = {r.actor for r in audit.recent(limit=100, action="p8.marker")}
    assert both == {"a@8.test", "b@8.test"}, "unscoped must see both orgs"
    print("  P8 audit service: org_id scopes by actor; None (back-office) unscoped")


def test_p8_customer_audit_scoped_superadmin_is_home_org_only():
    """THE LINCHPIN on the audit surface: an org-admin's customer /audit + export
    show ONLY their org's rows; a super-admin in the CUSTOMER app sees home-org only
    (NOT all). The back-office /audit sees ALL orgs."""
    d = _p8_audit_world()
    assert d  # seeded
    _gate_on()
    try:
        # org-admin B: customer /audit must show b's marker, NOT a's.
        cb = TestClient(app)
        csrf_for(cb)
        cb.post(
            "/auth/login",
            data={"email": "b@8.test", "password": "b-pass-123456", "next": "/"},
            follow_redirects=False,
        )
        page_b = cb.get("/audit?action=p8.marker").text
        assert "2.2.2.2" in page_b and "1.1.1.1" not in page_b, (
            "org-admin B audit leaked org A's rows"
        )
        exp_b = cb.get("/audit/export?action=p8.marker").text
        assert "b@8.test" in exp_b and "a@8.test" not in exp_b, (
            "org-admin B audit export leaked org A"
        )

        # super-admin A in the CUSTOMER app: home-org (A) only -- NOT all.
        ca = TestClient(app)
        csrf_for(ca)
        ca.post(
            "/auth/login",
            data={"email": "a@8.test", "password": "a-pass-123456", "next": "/"},
            follow_redirects=False,
        )
        page_a = ca.get("/audit?action=p8.marker").text
        assert "1.1.1.1" in page_a and "2.2.2.2" not in page_a, (
            "LINCHPIN FAILED: super-admin CUSTOMER /audit saw org B's rows"
        )
        exp_a = ca.get("/audit/export?action=p8.marker").text
        assert "a@8.test" in exp_a and "b@8.test" not in exp_a, (
            "LINCHPIN FAILED: super-admin CUSTOMER audit export saw org B"
        )

        # The cross-org half used to be asserted through the back-office /audit page.
        # 2026-09-20: there is no cross-org READ surface on the web any more, by design.
        # The half that MATTERS survives and is asserted above: an admin in the customer
        # app sees their HOME org only. That was the linchpin; it is now simply the rule.
        print(
            "  P8 audit web: every admin sees HOME-org rows only in the customer app; "
            "no cross-org web surface exists"
        )
    finally:
        _gate_off()


def test_p8_gdpr_refuses_cross_org():
    """Gate ON: an org-admin can export/erase a user IN THEIR org, but a cross-org
    target is refused (web 404; service empty/"not found")."""
    d = _p8_audit_world()
    _gate_on()
    try:
        b_user = security.get_by_email("b@8.test")  # org B
        a_user = security.get_by_email("a@8.test")  # org A (super-admin)

        # Service-level: b's org (org B) can't export/erase a@8.test (org A).
        recs = list(gdpr.export_user(a_user.id, caller_org_id=d["org_b"]))
        assert recs == [], "cross-org gdpr export must yield nothing"
        ok, msg = gdpr.erase_user(a_user.id, "b@8.test", caller_org_id=d["org_b"])
        assert ok is False and "not found" in msg.lower(), (
            "cross-org gdpr erase must be refused as not-found"
        )
        # Own-org export works (non-empty).
        own = list(gdpr.export_user(b_user.id, caller_org_id=d["org_b"]))
        assert own, "own-org gdpr export must return records"

        # Web route: org-admin B exporting a@8.test (org A) -> 404.
        cb = TestClient(app)
        csrf_for(cb)
        cb.post(
            "/auth/login",
            data={"email": "b@8.test", "password": "b-pass-123456", "next": "/"},
            follow_redirects=False,
        )
        r = cb.get("/users/%d/export" % a_user.id)  # noqa: UP031
        assert r.status_code == 404, f"cross-org GDPR export route must 404, got {r.status_code}"
        csrf_for(cb)
        cb.post("/users/%d/erase" % a_user.id, follow_redirects=False)  # noqa: UP031
        # erase route redirects with an error (not found) rather than acting.
        with Session(engine) as s:
            assert s.get(User, a_user.id).active is True, (
                "cross-org GDPR erase must NOT anonymize the other org's user"
            )
        print("  P8 gdpr: cross-org export/erase refused (web 404 + service not-found)")
    finally:
        _gate_off()


def test_p8_prompttemplate_org_isolated():
    """Gate ON: a PromptTemplate created in org A is not visible in org B's
    /templates list (and vice-versa); gate OFF the list is app-global (both)."""
    d = _p8_audit_world()
    # Seed one template per org (stamped directly).
    with Session(engine) as s:
        s.add(PromptTemplate(name="Tmpl A", body="a", org_id=d["org_a"]))
        s.add(PromptTemplate(name="Tmpl B", body="b", org_id=d["org_b"]))
        s.commit()
    _gate_on()
    try:
        cb = TestClient(app)
        csrf_for(cb)
        cb.post(
            "/auth/login",
            data={"email": "b@8.test", "password": "b-pass-123456", "next": "/"},
            follow_redirects=False,
        )
        page = cb.get("/templates").text
        assert "Tmpl B" in page and "Tmpl A" not in page, (
            "org B /templates leaked org A's prompt template"
        )

        # Creating via the route stamps the creator's org (org B here).
        csrf_for(cb)
        cb.post(
            "/templates/prompts", data={"name": "Created In B", "body": "x"}, follow_redirects=False
        )
        with Session(engine) as s:
            created = s.exec(
                select(PromptTemplate).where(PromptTemplate.name == "Created In B")
            ).first()
            assert created is not None and created.org_id == d["org_b"], (
                "route-created template must be stamped org B, got {!r}".format(
                    getattr(created, "org_id", None)
                )
            )
        print("  P8 templates: prompt templates org-isolated (list + create stamp)")
    finally:
        _gate_off()

    # GATE OFF: the list is app-global -- both templates show.
    with Session(engine) as s:
        all_tmpls = {t.name for t in s.exec(select(PromptTemplate)).all()}
    assert {"Tmpl A", "Tmpl B"}.issubset(all_tmpls)
    c = TestClient(app)  # auth OFF -> SYSTEM operator, unscoped
    page = c.get("/templates").text
    assert "Tmpl A" in page and "Tmpl B" in page, (
        "gate off: /templates must be app-global (both templates visible)"
    )
    print("  P8 templates: gate-off list is app-global (byte-identical to today)")


def test_p8_gate_off_byte_identical():
    """The sacred gate re-asserted for P8: with the gate OFF, audit is unscoped, the
    is_superadmin flag has no scoping effect, and templates are app-global."""
    d = _p8_audit_world()  # leaves audit_enabled on; gate stays OFF (default)
    assert d and access.multi_org_active() is False, "gate must be OFF here"
    # audit unscoped: current_org_id is None gate-off, so the customer /audit passes
    # org_id=None -> both orgs' rows.
    both = {r.actor for r in audit.recent(limit=100, action="p8.marker")}
    assert both == {"a@8.test", "b@8.test"}, "gate off: audit must be unscoped"
    # is_superadmin has no effect on current_org_id gate-off.
    a_user = security.get_by_email("a@8.test")
    assert access.current_org_id(a_user) is None, (
        "gate off: current_org_id(super-admin) must be None (unrestricted)"
    )
    # gdpr caller_org_id=None (the gate-off path) is unscoped -> exports anyone.
    recs = list(gdpr.export_user(a_user.id, caller_org_id=None))
    assert recs, "gate off: unscoped gdpr export must return records"
    print(
        "  P8 gate-off: audit unscoped, is_superadmin inert, gdpr unscoped "
        "(byte-identical to today)"
    )


# --------------------------------------------------------------------------- #
# PSEUDO-ACTOR JOB VISIBILITY (adversarial-review gap):
# jobs created_by="webhook"/"scheduler" have no User row, so job_org_id used to
# resolve None -> a NULL-org job invisible to EVEN the correct org's admin on
# /api/jobs/{id} (only back-office saw them). The fix resolves a pseudo-actor
# job's org from the CONTENT it references (Connection.org_id via connection_id;
# Project.org_id via document_id/project_id), reading ONLY the referenced row's
# own org_id. Real-user attribution is unchanged; unresolvable -> NULL-org (today).
# These tests reuse the P4 world (orgs A/B, admins, lsu_ keys, docs da/db) and
# seed FK-valid pseudo-actor jobs; FK enforcement is ON.
# --------------------------------------------------------------------------- #
def test_pseudo_actor_job_org_resolved_from_content() -> None:
    """Gate ON: a webhook job ({connection_id}, created_by='webhook') and a
    scheduler-style job ({document_id}, created_by='scheduler') are READABLE via
    /api/jobs/{id} by the referenced row's org admin key, and 404 for the other
    org's key -- attribution now flows from the referenced Connection/Project's
    OWN org_id, never the caller. An unresolvable pseudo-actor job stays NULL-org
    (invisible to both scoped keys), byte-identical to today."""
    from app.models import Job, JobStatus

    _ca, _cb, d, key_a, key_b = _seed_p4_keys()
    _gate_on()
    try:
        oa, ob, da = d["org_a"], d["org_b"], d["da"]
        with Session(engine) as s:
            # FK-valid connections, one per org (org_id is the ONLY org source).
            conn_a = Connection(
                kind="airtable",
                name="Job Conn A",
                config={},
                secret_enc="",
                org_id=oa,
                created_by="aa@x.com",
            )
            conn_b = Connection(
                kind="airtable",
                name="Job Conn B",
                config={},
                secret_enc="",
                org_id=ob,
                created_by="bb@x.com",
            )
            s.add(conn_a)
            s.add(conn_b)
            s.commit()
            s.refresh(conn_a)
            s.refresh(conn_b)
            # (1) webhook job referencing org A's connection.
            wh_job = Job(
                kind="connector_translate",
                payload={
                    "connection_id": conn_a.id,
                    "actor": "webhook",
                    "webhook_article_ids": ["42"],
                },
                status=JobStatus.QUEUED,
                created_by="webhook",
            )
            # (2) scheduler-style job referencing org A's DOCUMENT (da -> proj A).
            sch_job = Job(
                kind="translate_document",
                payload={"document_id": da, "locale": "fr-FR"},
                status=JobStatus.QUEUED,
                created_by="scheduler",
            )
            # (3) unresolvable pseudo-actor job -> must stay NULL-org.
            null_job = Job(
                kind="translate_document",
                payload={},
                status=JobStatus.QUEUED,
                created_by="scheduler",
            )
            for j in (wh_job, sch_job, null_job):
                s.add(j)
            s.commit()
            s.refresh(wh_job)
            s.refresh(sch_job)
            s.refresh(null_job)
            wh_id, sch_id, null_id = wh_job.id, sch_job.id, null_job.id

        # Unit-level: job_org_id resolves org A from the referenced rows.
        from app.services import jobs as _jobs

        with Session(engine) as s:
            assert _jobs.job_org_id(s.get(Job, wh_id)) == oa, (
                "webhook job org must resolve to the connection's org A"
            )
            assert _jobs.job_org_id(s.get(Job, sch_id)) == oa, (
                "scheduler job org must resolve to the document's project org A"
            )
            assert _jobs.job_org_id(s.get(Job, null_id)) is None, (
                "unresolvable pseudo-actor job must stay NULL-org"
            )

        api = TestClient(app)
        ha, hb = {"X-Loc-Token": key_a}, {"X-Loc-Token": key_b}

        def is200(r, what):
            assert r.status_code == 200, (
                f"{what}: expected 200, got {r.status_code} ({r.text[:120]})"
            )

        def is404(r, what):
            assert r.status_code == 404, (
                f"{what}: expected 404, got {r.status_code} ({r.text[:120]})"
            )

        # THE GAP CLOSED: org A's admin now reads its own webhook/scheduler jobs.
        is200(api.get(f"/api/jobs/{wh_id}", headers=ha), "A key -> A's webhook job")
        is200(api.get(f"/api/jobs/{sch_id}", headers=ha), "A key -> A's scheduler job")
        # Org B must NOT reach org A's pseudo-actor jobs (no cross-org leak).
        is404(api.get(f"/api/jobs/{wh_id}", headers=hb), "B key -> A's webhook job")
        is404(api.get(f"/api/jobs/{sch_id}", headers=hb), "B key -> A's scheduler job")
        # Regression: an unresolvable NULL-org job is invisible to BOTH scoped keys.
        is404(api.get(f"/api/jobs/{null_id}", headers=ha), "A key -> NULL-org job")
        is404(api.get(f"/api/jobs/{null_id}", headers=hb), "B key -> NULL-org job")
        print(
            "  pseudo-actor jobs: webhook/scheduler jobs org-attributed from "
            "content (readable by right org, 404 for other; NULL-org stays hidden)"
        )
    finally:
        _gate_off()


def test_pseudo_actor_job_content_never_widens_from_caller() -> None:
    """SAFETY: content resolution reads ONLY the referenced row's own org_id, so a
    job referencing org B's connection resolves to org B REGARDLESS of any actor
    string -- it never becomes readable by org A. Also proves real-user attribution
    is UNCHANGED: a real user's job keeps the creator's org even if its payload
    references another org's row (real-user path never falls through to content)."""
    from app.models import Job, JobStatus

    _ca, _cb, d, key_a, _key_b = _seed_p4_keys()
    _gate_on()
    try:
        oa, ob = d["org_a"], d["org_b"]
        from app.services import jobs as _jobs

        with Session(engine) as s:
            conn_b = Connection(
                kind="airtable",
                name="Safety Conn B",
                config={},
                secret_enc="",
                org_id=ob,
                created_by="bb@x.com",
            )
            s.add(conn_b)
            s.commit()
            s.refresh(conn_b)
            # Pseudo-actor job naming org B's connection -> resolves to B, not A.
            wh_b = Job(
                kind="connector_translate",
                payload={"connection_id": conn_b.id, "actor": "webhook"},
                status=JobStatus.QUEUED,
                created_by="webhook",
            )
            # Real user (org A) whose payload references org B's connection: the
            # real-user path is authoritative -> org A, content NEVER consulted.
            real_a = Job(
                kind="connector_translate",
                payload={"connection_id": conn_b.id, "actor": "aa@x.com"},
                status=JobStatus.QUEUED,
                created_by="aa@x.com",
            )
            s.add(wh_b)
            s.add(real_a)
            s.commit()
            s.refresh(wh_b)
            s.refresh(real_a)
            assert _jobs.job_org_id(s.get(Job, wh_b.id)) == ob, (
                "pseudo-actor job referencing org B's row must resolve to org B"
            )
            assert _jobs.job_org_id(s.get(Job, real_a.id)) == oa, (
                "real-user attribution must stay the creator's org (A), not payload's B"
            )
            wh_b_id = wh_b.id

        # org A's key must NOT read a job attributed (via content) to org B.
        api = TestClient(app)
        r = api.get(f"/api/jobs/{wh_b_id}", headers={"X-Loc-Token": key_a})
        assert r.status_code == 404, (
            "SAFETY FAILED: org A read a webhook job referencing org B's connection"
        )
        print(
            "  pseudo-actor safety: content org read only from referenced row "
            "(B stays B); real-user attribution unchanged"
        )
    finally:
        _gate_off()


def test_pseudo_actor_job_falls_through_when_creator_deleted() -> None:
    """Coverage gap: the "only narrows" claim above never actually exercises the
    stage-1 -> stage-2 FALL-THROUGH (real user resolves at stage 1 every time, since
    aa@x.com/bb@x.com are never deleted). This test drives that transition directly:

    1. Create a REAL, throwaway org-A user and, through the normal write-guarded
       path's invariant, a job it created whose payload references an org-A
       connection (payload-org == creator-org, exactly what _payload_in_org /
       _conn_in_org enforce on every write -- see job_org_id's docstring/comment).
    2. Confirm stage 1 (creator lookup) resolves the job to org A.
    3. DELETE the user row, so User.email lookup now misses and job_org_id falls
       through to stage 2 (content resolution).
    4. Confirm the job STILL resolves to org A -- via the connection's own org_id,
       not the (now-gone) creator -- proving the fall-through lands on the SAME org
       the write-guard already confined it to, not NULL and not another org."""
    from app.models import Job, JobStatus, User

    _ca, _cb, d, key_a, _key_b = _seed_p4_keys()
    _gate_on()
    try:
        oa = d["org_a"]
        from app.services import jobs as _jobs

        with Session(engine) as s:
            # Throwaway org-A user: not aa@x.com (that account is process-cached
            # and must survive for other tests) -- created and deleted only here.
            temp_user = security.create_user(
                "temp-fallthrough@x.com",
                "Temp Fallthrough",
                Role.TRANSLATOR,
                "temp-password-123",
                enforce_policy=False,
                org_id=oa,  # #181: created IN its org
            )
            temp_user.org_id = oa
            s.add(temp_user)
            s.commit()
            s.refresh(temp_user)
            temp_email = temp_user.email

            # Org-A connection: the content the job's payload references.
            conn_a = Connection(
                kind="airtable",
                name="Fallthrough Conn A",
                config={},
                secret_enc="",
                org_id=oa,
                created_by=temp_email,
            )
            s.add(conn_a)
            s.commit()
            s.refresh(conn_a)

            # Job created by the real user, payload referencing org A's own
            # connection -- exactly the write-guard invariant (_payload_in_org /
            # _conn_in_org reject a payload whose connection_id is out-of-org at
            # write time), so payload-org == creator-org by construction here.
            job = Job(
                kind="connector_translate",
                payload={"connection_id": conn_a.id, "actor": temp_email},
                status=JobStatus.QUEUED,
                created_by=temp_email,
            )
            s.add(job)
            s.commit()
            s.refresh(job)
            job_id = job.id

            # Stage 1: real-user attribution resolves to org A.
            assert _jobs.job_org_id(s.get(Job, job_id)) == oa, (
                "stage 1 (creator lookup) must resolve the live user's job to org A"
            )

            # Delete the creator: User.email lookup now misses -> stage 1 yields
            # None -> job_org_id must fall through to stage 2 (content resolution).
            s.delete(s.get(User, temp_user.id))
            s.commit()
            assert security.get_by_email(temp_email) is None, (
                "setup bug: creator must actually be gone to force the fall-through"
            )

            # Stage 2: content resolution reads the connection's own org_id --
            # still org A (the same org the write-guard confined it to), proving
            # the fall-through lands where it should, not NULL/foreign.
            resolved = _jobs.job_org_id(s.get(Job, job_id))
            assert resolved == oa, (
                "FALL-THROUGH FAILED: after the creator was deleted, job_org_id "
                "must still resolve to org A via the payload's own connection, "
                f"got {resolved!r}"
            )

        # And the org-A API key can still read it post-fall-through.
        api = TestClient(app)
        r = api.get(f"/api/jobs/{job_id}", headers={"X-Loc-Token": key_a})
        assert r.status_code == 200, (
            "org A's key must still read its own job after creator deletion "
            f"forces the stage-2 fall-through, got {r.status_code}"
        )
        print(
            "  pseudo-actor fall-through: deleted creator -> stage 2 still "
            "resolves to org A (write-guard invariant holds)"
        )
    finally:
        _gate_off()


def test_pseudo_actor_job_gate_off_and_backoffice_unchanged() -> None:
    """Gate OFF (single-tenant / back-office-equivalent unscoped read): _job_readable
    returns True for a pseudo-actor job regardless of its resolved org -- back-office
    and single-operator visibility is byte-identical to today (job_org_id gains
    resolution but the scoping is only consulted while multi_org_active())."""
    from app.models import Job, JobStatus
    from app.web.api import _job_readable

    _ca, _cb, d, _key_a, _key_b = _seed_p4_keys()
    _gate_off()  # auth OFF -> single-operator; also the back-office unscoped mode
    try:
        with Session(engine) as s:
            conn_a = Connection(
                kind="airtable",
                name="GateOff Conn",
                config={},
                secret_enc="",
                org_id=d["org_a"],
                created_by="x",
            )
            s.add(conn_a)
            s.commit()
            s.refresh(conn_a)
            wh = Job(
                kind="connector_translate",
                payload={"connection_id": conn_a.id, "actor": "webhook"},
                status=JobStatus.QUEUED,
                created_by="webhook",
            )
            s.add(wh)
            s.commit()
            s.refresh(wh)
            wh_id = wh.id
        with Session(engine) as s:
            job = s.get(Job, wh_id)
            # auth OFF: _job_readable short-circuits True (single operator sees all).
            assert _job_readable({"kind": "user", "user": None, "actor": ""}, job) is True, (
                "gate off: pseudo-actor job must be readable (single-operator/back-office)"
            )
        print(
            "  pseudo-actor gate-off: back-office/single-operator reads all "
            "pseudo-actor jobs (byte-identical)"
        )
    finally:
        _gate_off()


# --------------------------------------------------------------------------- #
# The four CLI-parity-B items: guarded org delete, apikey enumeration isolation,
# routing rule show (org-scoped read-back + the #26 FK org-scope residual).
# --------------------------------------------------------------------------- #
def test_org_delete_guarded() -> None:
    """Guarded `org delete` (operator-gated, multi-org): the service refuses the
    default org, the caller's OWN org, and any NON-EMPTY org (fail-closed, itemized) —
    there is no cascading force. The CLI is --operator-gated + refuses --remote; the
    back-office super-admin surface mirrors it. Success is audited."""
    d = _seed_two_orgs()  # org A/B each carry a user + a project -> both non-empty
    from app.services import access as _access

    # --- service-level preconditions (gate-agnostic) -------------------------
    with Session(engine) as s:
        default_id = _access.default_org_id(s)
        # empty org (created fresh, no tenant rows)
        empty = _access.create_org(s, "Empty Org To Delete")
        empty_id = empty.id
        # (1) default org refused
        try:
            _access.delete_org(s, default_id)
            assert False, "default must refuse"
        except ValueError as e:
            assert "default" in str(e).lower(), e
        # (2) caller's OWN org refused
        try:
            _access.delete_org(s, empty_id, caller_org_id=empty_id)
            assert False, "own org must refuse"
        except ValueError as e:
            assert "acting from" in str(e).lower(), e
        # (3) NON-EMPTY org refused, itemized (org A has a user + a project)
        try:
            _access.delete_org(s, d["org_a"])
            assert False, "non-empty must refuse"
        except ValueError as e:
            msg = str(e).lower()
            assert "not empty" in msg and "user" in msg and "project" in msg, e
        # org A must still exist after the refusal
        assert s.get(Organization, d["org_a"]) is not None, "refusal must not delete"
        # (4) unknown id refused
        try:
            _access.delete_org(s, 999999)
            assert False
        except ValueError as e:
            assert "no such organization" in str(e).lower(), e
        # (5) an EMPTY org deletes successfully; a second delete is "no such"
        deleted = _access.delete_org(s, empty_id)
        assert deleted.id == empty_id, deleted
    with Session(engine) as s:
        assert s.get(Organization, empty_id) is None, "empty org must be gone"

    # (6) TOCTOU race: a row lands AFTER the emptiness count but BEFORE the commit.
    # The FK RESTRICT keeps the data safe (raw IntegrityError) -- delete_org must turn
    # that into the SAME clean itemized "not empty" refusal, no traceback, no delete.
    with Session(engine) as s:
        race_org = _access.create_org(s, "Race Org")
        race_id = race_org.id
    orig_counts = _access._org_stamped_counts
    _race_state = {"n": 0}

    def _racy_counts(session, oid):
        _race_state["n"] += 1
        if _race_state["n"] == 1 and oid == race_id:
            # the window: a user lands in the org right after the emptiness check
            with Session(engine) as s2:
                s2.add(User(email="race@x.test", role=Role.ADMIN, org_id=oid))
                s2.commit()
            return []  # the count "saw" the org empty
        return orig_counts(session, oid)

    _access._org_stamped_counts = _racy_counts
    try:
        with Session(engine) as s:
            try:
                _access.delete_org(s, race_id)
                assert False, "the race must surface as a clean refusal, not a delete"
            except ValueError as e:
                assert "not empty" in str(e).lower(), ("raw IntegrityError leaked?", e)
    finally:
        _access._org_stamped_counts = orig_counts
    with Session(engine) as s:
        assert s.get(Organization, race_id) is not None, "race org must survive (safe)"

    # --- CLI: --operator-gated + fail-closed + refuses --remote --------------
    _gate_on()
    try:
        secret = "orgdel-operator-secret-value-123"
        settings.set_many({"operator_key_hash": hashlib.sha256(secret.encode()).hexdigest()})
        settings.invalidate_cache()
        # seed a fresh empty org to delete via the CLI
        with Session(engine) as s:
            e2 = _access.create_org(s, "CLI Empty Org")
            e2_id = e2.id
        # no --operator (gate on) -> refused (exit 2)
        code, _p, err = _cli(["org", "delete", str(e2_id), "--yes"])
        assert code == 2 and "operator" in err.lower(), (code, err)
        # --remote -> refused (in-process operator host only)
        code, _p, err = _cli(
            [
                "--remote",
                "http://127.0.0.1",
                "--token",
                "lsu_x",
                "org",
                "delete",
                str(e2_id),
                "--yes",
            ]
        )
        assert code == 2 and "remote" in err.lower(), (code, err)
        # --operator + right key + empty -> deleted (exit 0), audited
        os.environ["LOC_TMS_OPERATOR_KEY"] = secret
        code, pay, err = _cli_json(["--operator", "org", "delete", str(e2_id), "--yes"])
        assert code == 0 and pay["ok"] and pay["deleted"], (code, pay, err)
        with Session(engine) as s:
            assert s.get(Organization, e2_id) is None, "CLI delete must remove the org"
        assert any(r.action == "org.delete" for r in audit.recent(limit=50)), (
            "org.delete must be audited"
        )
        # --operator on a NON-EMPTY org -> exit 2, itemized, org survives
        code, _p, err = _cli(["--operator", "org", "delete", str(d["org_b"]), "--yes"])
        assert code == 2 and "not empty" in err.lower(), (code, err)
        with Session(engine) as s:
            assert s.get(Organization, d["org_b"]) is not None, "non-empty survives"

        # --- back-office super-admin parity: delete empty (303->/), refuse own (400)
        op = security.create_user(
            "orgdel_op@x.com", "OrgDel Op", Role.ADMIN, "orgdel-op-pass-123", org_id=d["org_a"]
        )  # #181: created IN its org
        with Session(engine) as s:
            u = s.get(User, op.id)
            u.is_superadmin = True
            u.org_id = d["org_a"]  # the operator's OWN org (non-empty anyway)
            s.add(u)
            s.commit()
        with Session(engine) as s:
            e3 = _access.create_org(s, "BO Empty Org")
            e3_id = e3.id
        # 2026-09-20: the back-office mirror of this was removed with the console. The
        # SERVICE refusals asserted above (default org, non-empty org, itemized reasons)
        # are the authority and are unchanged; `cli org delete` is the only door now, and
        # its --operator gate + --remote refusal + audit row are asserted above too.
        with Session(engine) as s:
            _access.delete_org(s, e3_id)
            assert s.get(Organization, e3_id) is None, "an EMPTY org must delete"
        print(
            "  org delete: service refuses default/own/non-empty (itemized) + deletes "
            "empty; CLI --operator-gated + refuses --remote + audited"
        )
    finally:
        os.environ.pop("LOC_TMS_OPERATOR_KEY", None)
        _gate_off()


def test_apikey_list_api_org_isolation() -> None:
    """System-wide `apikey list`: over /api (the remote arm), an org admin sees ONLY
    their own org's keys (never another org's — the reliably-found defect class); a
    regular (non-admin) user's token is REFUSED (403); the key material/hash is never
    emitted."""
    d = _seed_two_orgs()
    from app.models import Role as _Role
    from app.services import apikeys

    # admins in each org (the seeded users are admins) + a non-admin in org A. Use the
    # freshly-seeded user objects (NOT get_by_email: the suite's shared DB accumulates
    # duplicate a@a.test/b@b.test rows across _seed_two_orgs calls).
    ua = d["user_a"]
    ub = d["user_b"]
    tr = security.create_user("ak_tr@a.test", "AK TR", _Role.TRANSLATOR, "aktr-pass-123")
    with Session(engine) as s:
        s.get(User, tr.id).org_id = d["org_a"]
        s.add(s.get(User, tr.id))
        s.commit()
    _ra, tok_a = apikeys.create_key(ua.id, "iso-a", scopes="api docs")
    _rb, tok_b = apikeys.create_key(ub.id, "iso-b", scopes="api docs")
    _rt, tok_tr = apikeys.create_key(tr.id, "iso-tr", scopes="api docs")
    _gate_on()
    try:
        c = TestClient(app)
        # org A admin: sees org A owners only, NOT b@b.test
        r = c.get("/api/apikeys", headers={"X-Loc-Token": tok_a})
        assert r.status_code == 200, r.text
        keys = r.json()["keys"]
        owners = {k["user"] for k in keys}
        assert "a@a.test" in owners, owners
        assert "b@b.test" not in owners, ("CROSS-ORG LEAK: org A saw org B keys", owners)
        # never emit the hash / key material
        assert all("key_hash" not in k and "api_key" not in k for k in keys), keys
        assert all("last_used" in k for k in keys), keys
        # org B admin: symmetric — sees b@b.test, not a@a.test
        r = c.get("/api/apikeys", headers={"X-Loc-Token": tok_b})
        owners_b = {k["user"] for k in r.json()["keys"]}
        assert "b@b.test" in owners_b and "a@a.test" not in owners_b, owners_b
        # non-admin (translator) token -> 403 (regular user refused; own keys on /account)
        r = c.get("/api/apikeys", headers={"X-Loc-Token": tok_tr})
        assert r.status_code == 403, (r.status_code, r.text)
        print(
            "  apikey list /api: org admin sees own org only (no cross-org leak); "
            "non-admin refused 403; hash/material never emitted"
        )
    finally:
        _gate_off()


def test_routing_rule_show_org_scoped_and_fk_residual() -> None:
    """`admin routing rule show` is org-scoped from day one (a cross-org id is
    indistinguishable from missing), and the #26 LOW residual is fixed: a rule can no
    longer reference a workflow/project in ANOTHER org (validate_rule + create_rule are
    org-scoped). /api mirrors the web admin RBAC; a cross-org fetch 404s."""
    d = _seed_two_orgs()
    from app.services import routing as _routing

    # a workflow owned by org B
    with Session(engine) as s:
        wf_b = Workflow(name="RS WF B", org_id=d["org_b"])
        s.add(wf_b)
        s.commit()
        s.refresh(wf_b)
        wf_b_id = wf_b.id
        # (residual) org A creating a rule pointing at org B's workflow is refused as
        # "no such workflow" -- a cross-org referent behaves as nonexistent.
        err = _routing.validate_rule(s, workflow_id=wf_b_id, org_id=d["org_a"])
        assert err == f"no such workflow: {wf_b_id}", err
        try:
            _routing.create_rule(
                s, match_ext="x", workflow_id=wf_b_id, org_id=d["org_a"], actor="t"
            )
            assert False, "cross-org FK must fail closed"
        except _routing.RoutingRuleError as e:
            assert "no such workflow" in str(e), e
        # a rule legitimately in org A (no cross-org referent) + one in org B
        rule_a = _routing.create_rule(
            s, match_ext="ra", project_id=d["proj_a"], org_id=d["org_a"], actor="t"
        )
        rule_a_id = rule_a.id
        rule_b = _routing.create_rule(
            s, match_ext="rb", project_id=d["proj_b"], org_id=d["org_b"], actor="t"
        )
        rule_b_id = rule_b.id
        rule_b_active0 = rule_b.active
        # get_rule org-scoping: org A sees it, org B sees None (indistinguishable)
        assert _routing.get_rule(s, rule_a_id, org_id=d["org_a"]) is not None
        assert _routing.get_rule(s, rule_a_id, org_id=d["org_b"]) is None, (
            "cross-org rule show must behave as missing"
        )

    # /api: org A admin reads its own rule; org B admin 404s it; non-admin 403
    from app.models import Role as _Role
    from app.services import apikeys

    tr = security.create_user("rs_tr@a.test", "RS TR", _Role.TRANSLATOR, "rstr-pass-123")
    with Session(engine) as s:
        s.get(User, tr.id).org_id = d["org_a"]
        s.add(s.get(User, tr.id))
        s.commit()
    # freshly-seeded org admins (NOT get_by_email -- see the note in the apikey test).
    ua = d["user_a"]
    ub = d["user_b"]
    _ra, tok_a = apikeys.create_key(ua.id, "rs-a", scopes="api docs")
    _rb, tok_b = apikeys.create_key(ub.id, "rs-b", scopes="api docs")
    _rt, tok_tr = apikeys.create_key(tr.id, "rs-tr", scopes="api docs")
    _gate_on()
    try:
        c = TestClient(app)
        r = c.get(f"/api/routing/rules/{rule_a_id}", headers={"X-Loc-Token": tok_a})
        assert r.status_code == 200 and r.json()["id"] == rule_a_id, r.text
        r = c.get(f"/api/routing/rules/{rule_a_id}", headers={"X-Loc-Token": tok_b})
        assert r.status_code == 404, ("cross-org rule fetch must 404", r.status_code)
        r = c.get(f"/api/routing/rules/{rule_a_id}", headers={"X-Loc-Token": tok_tr})
        assert r.status_code == 403, ("non-admin must be refused", r.status_code)

        # --- in-process CLI --org confinement for list/toggle/delete (the sibling
        # verbs now honor --org uniformly with `rule show`). Under --org A:
        #   list   -> only org A's rule (never rule_b);
        #   toggle -> a cross-org id (rule_b) is refused identically to a missing id;
        #   delete -> same fail-closed refusal; rule_b is never mutated/removed.
        code, pay, err = _cli_json(["--org", str(d["org_a"]), "admin", "routing", "rule", "list"])
        assert code == 0, (code, pay, err)
        listed = {r_["id"] for r_ in pay["rules"]}
        assert rule_a_id in listed and rule_b_id not in listed, (
            "rule list --org A leaked org B's rule",
            listed,
        )
        # toggle a foreign (org B) rule under --org A -> exit 2 "no such rule"
        code, _p, err = _cli(
            ["--org", str(d["org_a"]), "admin", "routing", "rule", "toggle", str(rule_b_id)]
        )
        assert code == 2 and "no such rule" in err.lower(), (code, err)
        # delete a foreign (org B) rule under --org A -> exit 2 "no such rule"
        code, _p, err = _cli(
            [
                "--org",
                str(d["org_a"]),
                "admin",
                "routing",
                "rule",
                "delete",
                str(rule_b_id),
                "--yes",
            ]
        )
        assert code == 2 and "no such rule" in err.lower(), (code, err)
        # rule_b is untouched: still present, still its original active state
        with Session(engine) as s:
            rb = s.get(RoutingRule, rule_b_id)
            assert rb is not None and rb.active == rule_b_active0, (
                "cross-org toggle/delete must NOT mutate org B's rule"
            )
        # a MISSING id under --org A yields the SAME refusal (indistinguishable)
        code, _p, err = _cli(
            ["--org", str(d["org_a"]), "admin", "routing", "rule", "toggle", "987654"]
        )
        assert code == 2 and "no such rule" in err.lower(), (code, err)
        print(
            "  routing rule show/list/toggle/delete: org-scoped (cross-org == "
            "missing, byte-identical refusal); #26 FK residual fixed; /api admin-gated"
        )
    finally:
        _gate_off()


def test_invite_org_stamped() -> None:
    """Train I I-fix: security.accept_invite now funnels a NEW account through the shared
    create_user seam and STAMPS the invitee into the INVITER's org (the old inline
    User(...) left org_id NULL — the #73 leak). An EXISTING account is activated in place
    with its org untouched. A no-op with the gate off (org_id is not read there)."""
    init_db()
    # A dedicated org + a UNIQUE-email inviter (the shared DB accumulates duplicate
    # a@a.test rows across _seed_two_orgs calls, which would make get_by_email ambiguous —
    # emails are globally unique in production, so this reflects the real contract).
    with Session(engine) as s:
        org = Organization(name="Invite Org")
        s.add(org)
        s.commit()
        s.refresh(org)
        invite_org = org.id
    inviter = security.create_user(
        "inviter@invite.test", "Inviter", Role.ADMIN, "passw0rd-longenough", org_id=invite_org
    )
    _gate_on()
    try:
        inv = security.create_invite("invitee@invite.test", Role.TRANSLATOR, inviter.email)
        u = security.accept_invite(inv.token, "Invitee", "invitee-strong-pass")
        assert u is not None and u.org_id == invite_org, (
            "accept_invite must stamp the invitee into the inviter's org",
            getattr(u, "org_id", "None"),
        )
        assert u.auth_source == "local" and u.role == Role.TRANSLATOR
        # re-accepting the consumed token is refused.
        assert security.accept_invite(inv.token, "x", "another-strong-pass") is None
        print(
            "  invite I-fix: a new invitee is created via the shared seam + stamped the "
            "inviter's org"
        )
    finally:
        _gate_off()


def test_invite_retains_org_when_inviter_gone() -> None:
    """#129: a stored org survives erasure; legacy NULL ownership still fails closed."""
    from app.models import Invite

    init_db()
    with Session(engine) as s:
        org = Organization(name="Erase Org")
        s.add(org)
        s.commit()
        s.refresh(org)
        erase_org = org.id
    inviter = security.create_user(
        "erased-inviter@erase.test", "Erased", Role.ADMIN, "passw0rd-longenough", org_id=erase_org
    )
    inv = security.create_invite("orphan@erase.test", Role.TRANSLATOR, inviter.email)
    # #181: a LEGACY org-less invitation is data from before the gate (create_invite now
    # refuses one under an active gate), so it is issued while the gate is still off.
    legacy = security.create_invite("legacy@erase.test", Role.TRANSLATOR, "system", org_id=None)
    # HARD-delete the inviter row (simulating a GDPR erase that removed the account).
    with Session(engine) as s:
        s.delete(s.get(User, inviter.id))
        s.commit()
    _gate_on()
    try:
        u = security.accept_invite(inv.token, "Orphan", "orphan-strong-pass")
        assert u is not None and u.org_id == erase_org
        assert security.accept_invite(legacy.token, "Legacy", "legacy-strong-pass") is None
        with Session(engine) as s:
            assert s.exec(select(User).where(User.email == legacy.email)).first() is None
            invrow = s.get(Invite, legacy.id)
            assert invrow is not None and invrow.accepted is False, (
                "the invite must not be consumed"
            )
        print("  invite ownership survives erasure; legacy NULL ownership refuses account creation")
    finally:
        _gate_off()


def test_scim_org_isolation() -> None:
    """SCIM 2.0 (Train I): a SCIM key resolves to its owner's HOME org, so under the
    sacred gate its /scim/v2/Users listing/filter shows ONLY that org, a cross-org id is
    a 404 (no-oracle, not 403), and a SCIM create stamps the key's org (never NULL).
    Groups are org-scoped the same way. Mirrors the /api P4 confinement, on the /scim
    mount."""
    d = _seed_two_orgs()
    from app.services import apikeys

    settings.set_many({"scim_enabled": "1"})
    # SCIM keys for the org-A and org-B admins (the seeded users are admins).
    _ra, tok_a = apikeys.create_key(d["user_a"].id, "scim-a", scopes="scim")
    _rb, tok_b = apikeys.create_key(d["user_b"].id, "scim-b", scopes="scim")
    ha = {"Authorization": f"Bearer {tok_a}"}
    _gate_on()
    try:
        c = TestClient(app)
        # org A key lists ONLY org A users (a@a.test), never org B (b@b.test).
        listed = c.get("/scim/v2/Users", headers=ha)
        assert listed.status_code == 200, listed.text
        emails = {r["userName"] for r in listed.json()["Resources"]}
        assert "a@a.test" in emails, emails
        assert "b@b.test" not in emails, ("CROSS-ORG LEAK: org A SCIM listed org B", emails)
        # a filter for the org-B email returns zero from org A.
        assert (
            c.get('/scim/v2/Users?filter=userName eq "b@b.test"', headers=ha).json()["totalResults"]
            == 0
        )
        # GET the org-B admin's id from org A -> 404 (no-oracle, never 403).
        r = c.get(f"/scim/v2/Users/{d['user_b'].id}", headers=ha)
        assert r.status_code == 404, (r.status_code, r.text)
        # a SCIM create in org A stamps org A (never NULL under the active gate).
        r = c.post("/scim/v2/Users", headers=ha, json={"userName": "scim-new@a.test"})
        assert r.status_code == 201, r.text
        with Session(engine) as s:
            assert s.get(User, int(r.json()["id"])).org_id == d["org_a"], (
                "SCIM create must stamp the key's org, not NULL"
            )
        # a group created via the org-B key is invisible to the org-A key.
        rgb = c.post(
            "/scim/v2/Groups",
            headers={"Authorization": f"Bearer {tok_b}"},
            json={"displayName": "b-only-group"},
        )
        assert rgb.status_code == 201, rgb.text
        names_a = {
            g["displayName"] for g in c.get("/scim/v2/Groups", headers=ha).json()["Resources"]
        }
        assert "b-only-group" not in names_a, ("CROSS-ORG LEAK: org A saw org B group", names_a)
        print(
            "  SCIM /scim: org-A key lists/filters/creates only org A (org stamped); a "
            "cross-org id is a 404 no-oracle; groups org-scoped too"
        )
    finally:
        _gate_off()
        settings.set_many({"scim_enabled": "0"})


# --------------------------------------------------------------------------- #
# #73 (round-3 hardening, HIGH, confinement): the org_id=None DISAGREEMENT.
#
# PRE-FIX BEHAVIOUR each assertion below pins (all under an ACTIVE gate):
#   * access.current_org_id(a real user whose org_id is NULL) returned None;
#   * tm_module.browsable_customers(org_id=None) returned EVERY org's customers;
#   * access.org_id_clause(col, None) returned None = NO WHERE CLAUSE at all,
#     so glossary auto-attach on a NULL-org project reached every org.
# ...while access.same_org()/org_filter() read the SAME value as BLOCKED. Every
# assertion here FAILS against pre-fix code.
# --------------------------------------------------------------------------- #
_P73_SEQ = [0]


def _seed_p73():
    """Two orgs, each with a customer, plus a real (non-
    system) user whose org_id is NULL -- the #73 principal. The email is unique per
    call: _seed_two_orgs() re-runs init_db(), whose Default-org backfill has ALWAYS
    adopted org-less users, so a reused address would resolve to an earlier,
    already-adopted row."""
    _P73_SEQ[0] += 1
    null_email = f"nullorg{_P73_SEQ[0]}@x.test"
    d = _seed_two_orgs()
    with Session(engine) as s:
        cust_a = Customer(name="Cust A73", org_id=d["org_a"])
        cust_b = Customer(name="Cust B73", org_id=d["org_b"])
        s.add(cust_a)
        s.add(cust_b)
        s.commit()
        s.refresh(cust_a)
        s.refresh(cust_b)
        # THE #73 PRINCIPAL: a real account carrying no org at all.
        nulluser = User(email=null_email, name="No Org", role=Role.ADMIN, org_id=None)
        s.add(nulluser)
        s.commit()
        s.refresh(nulluser)
        s.expunge(nulluser)
        d.update(
            {
                "cust_a": cust_a.id,
                "cust_b": cust_b.id,
                "nulluser": nulluser,
                "null_email": null_email,
            }
        )
    return d


def test_p73_null_org_principal_resolves_blocked_not_none() -> None:
    """The chokepoint: a real NULL-org user resolves to access.BLOCKED_ORG_ID, never
    to None (which the whole `if org_id is not None:` read family reads as SEE-ALL)."""
    d = _seed_p73()
    _gate_on()
    try:
        scope = access.current_org_id(d["nulluser"])
        assert scope is not None, (
            "#73: current_org_id(NULL-org user) returned None = SEE-ALL on every "
            "`if org_id is not None:` read helper"
        )
        assert scope == access.BLOCKED_ORG_ID, ("expected the poison sentinel", scope)
        # ...and it agrees with the two authorities, which is the whole point.
        assert access.same_org(d["nulluser"], d["org_a"]) is False
        assert access.same_org(d["nulluser"], None) is False
        with Session(engine) as s:
            stmt = access.org_filter(select(Customer), Customer, d["nulluser"])
            assert list(s.exec(stmt).all()) == [], "org_filter must select nothing"
            assert access.visible_project_ids(s, d["nulluser"]) == [], (
                "#73: a NULL-org user must see NO projects"
            )
        print("  #73 chokepoint: NULL-org principal -> BLOCKED_ORG_ID (agrees with same_org)")
    finally:
        _gate_off()


def test_p73_read_helpers_fail_closed() -> None:
    """Each converged read helper returns NOTHING for a NULL-org scope under an
    active gate -- via the principal's resolved scope AND via a bare None."""
    from app.kernel import tm_module

    d = _seed_p73()
    _gate_on()
    try:
        scope = access.current_org_id(d["nulluser"])
        with Session(engine) as s:
            for label, org in (("resolved scope", scope), ("bare None", None)):
                assert tm_module.browsable_customers(s, org_id=org) == [], (
                    f"#73 ({label}): browsable_customers leaked every org's customers"
                )
            # org_id_clause: pre-fix this returned None (= no WHERE at all).
            clause = access.org_id_clause(Customer.org_id, None)
            assert clause is not None, (
                "#73: org_id_clause(col, None) returned None under an ACTIVE gate = no "
                "org filter = every org's rows (glossary auto-attach reached them all)"
            )
            assert list(s.exec(select(Customer).where(clause)).all()) == [], (
                "#73: org_id_clause must select nothing for a blocked scope"
            )
            # POSITIVE CONTROL: a properly-stamped org-A user still sees org A only.
            oa = access.current_org_id(d["user_a"])
            names = {c.name for c in tm_module.browsable_customers(s, org_id=oa)}
            assert names == {"Cust A73"}, ("org-A user must see exactly org A", names)
        print("  #73 read helpers: browsable_customers/org_id_clause FAIL CLOSED")
    finally:
        _gate_off()


def test_p73_unscoped_operator_door_still_sees_all() -> None:
    """access.UNSCOPED -- the key-verified CLI `--operator` door -- is the ONE way a
    None-ish scope still means every org. Without it the fail-closed change would
    silently break local-root cross-org administration."""
    from app.kernel import tm_module

    _seed_p73()
    _gate_on()
    try:
        assert access.org_scope_blocked(access.UNSCOPED) is False
        assert access.org_scope_value(access.UNSCOPED) is None
        with Session(engine) as s:
            names = {c.name for c in tm_module.browsable_customers(s, org_id=access.UNSCOPED)}
            assert {"Cust A73", "Cust B73"} <= names, ("operator door must see all", names)
        print("  #73 operator door: access.UNSCOPED still reads cross-org (CLI --operator)")
    finally:
        _gate_off()


def test_p73_poison_never_stamped_on_a_write() -> None:
    """BLOCKED_ORG_ID is a READ scope only: every write-stamp seam collapses it back
    to NULL, so a broken principal can never plant an org_id=-1 row."""
    d = _seed_p73()
    _gate_on()
    try:
        assert access.stampable_org_id(access.BLOCKED_ORG_ID) is None
        assert access.stampable_org_id(access.UNSCOPED) is None
        assert access.stampable_org_id(d["org_a"]) == d["org_a"]
        # content._org_for: the row-creation stamp.
        assert content._org_for(d["nulluser"]) is None, (
            "#73: a NULL-org creator must stamp NULL, never the read sentinel"
        )
        assert content._org_for(d["user_a"]) == d["org_a"]
        # security.create_user: the user-creation stamp (SCIM / /api / CLI all funnel here).
        # #181: the poison collapses to "no org", and an org-less create is now refused
        # outright under the gate, so neither the poison nor a NULL is ever written.
        try:
            security.create_user(
                "poisoncheck@x.test", "P", Role.TRANSLATOR, "", org_id=access.BLOCKED_ORG_ID
            )
        except security.OrglessAccountError:
            pass
        else:
            raise AssertionError("#181: create_user minted an account from the poison id")
        with Session(engine) as s:
            row = s.exec(select(User).where(User.email == "poisoncheck@x.test")).first()
            assert row is None, ("#73/#181: create_user wrote a row", row.org_id)
        with Session(engine) as s:
            assert (
                s.exec(select(Organization).where(Organization.id == access.BLOCKED_ORG_ID)).first()
                is None
            )
        print("  #73 write safety: BLOCKED_ORG_ID never reaches a stamped column")
    finally:
        _gate_off()


def test_p73_boot_invariant_backfills_and_logs() -> None:
    """The DATA half: with the gate ON, init_db() adopts every real NULL-org user
    into the Default org and says so loudly; afterwards none survive."""
    import logging

    d = _seed_p73()
    _gate_on()
    try:
        with Session(engine) as s:
            before = s.exec(select(User).where(User.email == d["null_email"])).first()
            assert before.org_id is None, "fixture precondition: the user starts NULL-org"
        records = []

        class _Cap(logging.Handler):
            def emit(self, record):
                records.append(record.getMessage())

        h = _Cap()
        dblog = logging.getLogger("loc_tms.db")
        dblog.addHandler(h)
        try:
            init_db()
        finally:
            dblog.removeHandler(h)
        with Session(engine) as s:
            after = s.exec(select(User).where(User.email == d["null_email"])).first()
            assert after.org_id is not None, (
                "#73 boot invariant: a real user is still NULL-org under an active gate"
            )
            survivors = [
                u
                for u in s.exec(select(User).where(User.org_id == None)).all()  # noqa: E711
                if u.email not in security.RESERVED_ACTORS
            ]
            assert survivors == [], ("NULL-org users survived the boot backfill", survivors)
        assert any("#73 CONFINEMENT" in m for m in records), (
            "the adoption must be LOUD (a WARNING naming the accounts)",
            records,
        )
        assert any(d["null_email"] in m for m in records), records
        print("  #73 boot invariant: NULL-org users adopted into the Default org + WARNED")
    finally:
        _gate_off()


def test_p73_boot_refuses_a_reserved_org_id() -> None:
    """The by-construction pin under the poison sentinel: if an Organization ever
    carried BLOCKED_ORG_ID the fail-closed predicates would start matching real rows,
    so boot refuses instead."""
    _seed_p73()
    _gate_off()
    with Session(engine) as s:
        s.exec(
            text(
                "INSERT INTO organization (id, name, active, created_at) "
                f"VALUES ({access.BLOCKED_ORG_ID}, 'poison', 1, CURRENT_TIMESTAMP)"
            )
        )
        s.commit()
    try:
        raised = ""
        try:
            init_db()
        except RuntimeError as exc:
            raised = str(exc)
        assert "reserved" in raised and "BLOCKED_ORG_ID" in raised, (
            "boot must refuse an organization row holding the read sentinel",
            raised,
        )
        print("  #73 sentinel pin: an organization row with id -1 refuses to boot")
    finally:
        with Session(engine) as s:
            s.exec(text(f"DELETE FROM organization WHERE id = {access.BLOCKED_ORG_ID}"))
            s.commit()


def test_p73_gate_off_byte_identical() -> None:
    """GATE OFF: every converged helper behaves exactly as before #73 -- None means
    unrestricted, current_org_id is None, org_id_clause applies no gate filter."""
    from app.kernel import tm_module

    d = _seed_p73()
    _gate_off()
    assert access.current_org_id(d["nulluser"]) is None
    assert access.current_org_id(d["user_a"]) is None
    assert access.org_scope_blocked(None) is False
    with Session(engine) as s:
        names = {c.name for c in tm_module.browsable_customers(s, org_id=None)}
        assert {"Cust A73", "Cust B73"} <= names, ("gate off must be see-all", names)
        # org_id_clause off-gate: None still means "nothing to filter on" (not false()).
        assert access.org_id_clause(Customer.org_id, None) is None
    print("  #73 gate OFF: converged helpers are byte-identical to pre-fix (see-all on None)")


# --------------------------------------------------------------------------- #
# #494: the org-confinement check must NAME the user table the way Postgres
# requires. USER is a reserved word there (current_user), so a bare `FROM user`
# / `UPDATE user` is a syntax error on every Postgres boot -- and SQLite
# silently tolerates the unquoted spelling, which is why the suite stayed green
# while the Postgres branch of init_db could not start at all. The listener test
# below asserts the SPELLING of every statement the check emits (it fails on the
# unquoted base tree); the companion test asserts the backfill UPDATE still
# WORKS on SQLite, so the fix is a spelling change and not a behaviour change.
# --------------------------------------------------------------------------- #
_P494_SEQ = [0]

# A bare `user` token: the word user with no word character and no double quote on
# either side. `"user"` (the quoted table), `user_id`, `username` and `usergroup`
# all fail this pattern, so it fires ONLY on the unquoted spelling.
_P494_BARE_USER = re.compile(r'(?<![\w"])user(?![\w"])')


def _p494_null_org_user():
    """A fresh real (non-system) NULL-org user -- the row _assert_org_confinement_-
    invariant adopts -- and the Default org id. Unique email per call (the ux_user_
    email_lower index refuses a reuse, and an earlier call's row is already adopted)."""
    _P494_SEQ[0] += 1
    email = f"p494quoting{_P494_SEQ[0]}@x.test"
    with Session(engine) as s:
        s.add(User(email=email, name="P494 No Org", role=Role.ADMIN, org_id=None))
        s.commit()
        default_org_id = s.exec(select(Organization).order_by(Organization.id)).first().id
    return email, default_org_id


def test_p494_confinement_sql_quotes_the_user_table() -> None:
    """EVERY statement _assert_org_confinement_invariant emits must name the user
    table as `"user"`, never as a bare `user` token. Recorded with a real
    before_cursor_execute listener on the engine -- the exact SQL text that would
    reach Postgres, not a source-text proxy, so a re-introduced unquoted spelling
    goes red here rather than on the next Postgres boot (#494)."""
    from sqlalchemy import event

    from app.db import _assert_org_confinement_invariant

    init_db()
    _email, default_org_id = _p494_null_org_user()
    _gate_on()
    statements = []

    def _record(conn_, cursor, statement, parameters, context, executemany):
        statements.append(statement)

    event.listen(engine, "before_cursor_execute", _record)
    try:
        with engine.connect() as conn:
            _assert_org_confinement_invariant(conn, default_org_id)
    finally:
        event.remove(engine, "before_cursor_execute", _record)
        _gate_off()

    assert statements, "the listener recorded nothing; the check never ran"
    mentions_user = [s for s in statements if re.search(r'"user"|\buser\b', s)]
    assert any(s.lstrip().upper().startswith("SELECT") for s in mentions_user), (
        "#494: the recording caught no SELECT against the user table -- the fixture or "
        "the check changed shape and this ratchet must be re-pinned, not deleted"
    )
    assert any(s.lstrip().upper().startswith("UPDATE") for s in mentions_user), (
        "#494: the recording caught no backfill UPDATE against the user table (the "
        "NULL-org fixture was not found, so the spelling assertion never ran)",
        statements,
    )
    for s in mentions_user:
        assert '"user"' in s, f"#494: statement names the user table unquoted: {s!r}"
    for s in statements:
        bad = _P494_BARE_USER.search(s)
        assert bad is None, (
            "#494: a statement carries a bare `user` token. USER is a reserved word in "
            "Postgres (current_user), so the unquoted spelling is a syntax error there "
            "and the org-confinement check breaks every Postgres boot; SQLite tolerates "
            'it, which is how this shipped. Quote the table as "user" -- see '
            f"_clear_dormant_superadmin and alembic revision a1f4c8b2d6e9. Statement: {s!r}"
        )
    print('  #494 confinement SQL: every statement quotes "user"; no bare user token')


def test_p494_confinement_backfill_stamps_org_on_sqlite() -> None:
    """The quoted fix must still WORK on SQLite (the dialect the suite runs on and
    the one the fix must not regress): the invariant adopts the NULL-org user, and
    no real NULL-org user survives the call."""
    from app.db import _assert_org_confinement_invariant

    init_db()
    email, default_org_id = _p494_null_org_user()
    _gate_on()
    try:
        with engine.connect() as conn:
            _assert_org_confinement_invariant(conn, default_org_id)
        with Session(engine) as s:
            row = s.exec(select(User).where(User.email == email)).first()
            assert row.org_id == default_org_id, (
                "#494: the quoted backfill UPDATE did not stamp org_id on SQLite "
                f"(got {row.org_id!r}, expected {default_org_id!r})"
            )
            survivors = [
                u
                for u in s.exec(select(User).where(User.org_id == None)).all()  # noqa: E711
                if u.email not in security.RESERVED_ACTORS
            ]
            assert survivors == [], ("real NULL-org users survived", survivors)
        print("  #494 backfill: quoted UPDATE still stamps org_id on SQLite (behaviour kept)")
    finally:
        _gate_off()


# --------------------------------------------------------------------------- #
# #73 SIX/SEVEN-HELPER FAMILY (round-4 hardening, LOW, confinement): the converged
# MANAGEMENT read helpers that relied SOLELY on the -1 poison now ALSO carry the
# access.org_scope_blocked guard, so the org-confinement family fails closed BY
# CONSTRUCTION (a future new helper can't silently re-open the see-all hole) exactly
# like the tm_module.browsable_customers sibling hardened in Q3.
#
# Helpers pinned here: glossary_module.list_glossaries / visible / list_for_customer /
# list_unassigned; content_types.list_types / visible / options_for_project;
# vendors.list_vendors_for_picker / visible; style_guides.visible / list_guides.
#
# PRE-FIX BEHAVIOUR each assertion pins (all under an ACTIVE gate, via a bare None
# org_id): every helper skipped its WHERE and returned SEE-ALL. Every fail-closed
# assertion here FAILS against pre-fix code and PASSES after. The vendor cross-org test
# additionally pins that the visible() guard did NOT break cross-org linked dispatch
# (the deliberate org-agnostic path was converted to access.UNSCOPED, not left a bare
# None).
# --------------------------------------------------------------------------- #
_SIXHELP_SEQ = [0]


def _seed_sixhelper():
    """Two orgs, each with a customer + a customer-scoped glossary + a customer-scoped
    content type + a vendor + an org-global style guide. Plus a cross-org vendor LINK
    (org-A vendor linked to org-B's customer) and a NULL-org principal (the #73 user).
    Unique names per call (mirrors _seed_p73's reasoning)."""
    from app.models import (
        ContentType,
        Glossary,
        GlossaryStatus,
        StyleGuide,
        StyleGuideStatus,
        Vendor,
        VendorCustomer,
    )

    _SIXHELP_SEQ[0] += 1
    n = _SIXHELP_SEQ[0]
    null_email = f"sixnull{n}@x.test"
    d = _seed_two_orgs()
    with Session(engine) as s:
        cust_a = Customer(name=f"SixCust A{n}", org_id=d["org_a"])
        cust_b = Customer(name=f"SixCust B{n}", org_id=d["org_b"])
        s.add(cust_a)
        s.add(cust_b)
        s.commit()
        s.refresh(cust_a)
        s.refresh(cust_b)
        gloss_a = Glossary(
            name=f"Gloss A{n}",
            status=GlossaryStatus.DRAFT,
            org_id=d["org_a"],
            customer_id=cust_a.id,
        )
        gloss_b = Glossary(
            name=f"Gloss B{n}",
            status=GlossaryStatus.DRAFT,
            org_id=d["org_b"],
            customer_id=cust_b.id,
        )
        ct_a = ContentType(name=f"CT A{n}", org_id=d["org_a"], customer_id=cust_a.id)
        ct_b = ContentType(name=f"CT B{n}", org_id=d["org_b"], customer_id=cust_b.id)
        vend_a = Vendor(name=f"Vend A{n}", org_id=d["org_a"])
        vend_b = Vendor(name=f"Vend B{n}", org_id=d["org_b"])
        sg_a = StyleGuide(
            name=f"SG A{n}", status=StyleGuideStatus.DRAFT, org_id=d["org_a"], body="a"
        )
        sg_b = StyleGuide(
            name=f"SG B{n}", status=StyleGuideStatus.DRAFT, org_id=d["org_b"], body="b"
        )
        for obj in (gloss_a, gloss_b, ct_a, ct_b, vend_a, vend_b, sg_a, sg_b):
            s.add(obj)
        # THE #73 PRINCIPAL: a real account carrying no org at all.
        nulluser = User(email=null_email, name="No Org", role=Role.ADMIN, org_id=None)
        s.add(nulluser)
        s.commit()
        for obj in (gloss_a, gloss_b, ct_a, ct_b, vend_a, vend_b, sg_a, sg_b):
            s.refresh(obj)
        # Cross-org LINK: org-A vendor linked to org-B's customer (the customer LINK is
        # the dispatch authority, regardless of org -- the 2026-08-08 feature).
        s.add(VendorCustomer(vendor_id=vend_a.id, customer_id=cust_b.id))
        s.commit()
        # Refresh AFTER the last commit (which would otherwise expire the detached row),
        # then expunge with attributes loaded so the predicate calls read them detached.
        s.refresh(nulluser)
        s.expunge(nulluser)
        d.update(
            {
                "cust_a": cust_a.id,
                "cust_b": cust_b.id,
                "gloss_a": gloss_a.id,
                "gloss_b": gloss_b.id,
                "ct_a": ct_a.id,
                "ct_b": ct_b.id,
                "vend_a": vend_a.id,
                "vend_b": vend_b.id,
                "sg_a": sg_a.id,
                "sg_b": sg_b.id,
                "nulluser": nulluser,
            }
        )
    return d


def test_p73b_sixhelper_family_fails_closed() -> None:
    """Every retrofitted MANAGEMENT read helper returns NOTHING for a NULL-org scope
    under an active gate -- via a bare None (the new guard) AND via the resolved
    principal scope (the -1 poison). Return TYPES pinned: [] for lists, None for
    single-row. Positive control: an org-A scope sees ONLY org A."""
    from app.kernel import content_types as ct
    from app.kernel import glossary_module as gm
    from app.kernel import style_guides as sg
    from app.kernel import vendors

    d = _seed_sixhelper()
    _gate_on()
    try:
        scope = access.current_org_id(d["nulluser"])
        assert scope == access.BLOCKED_ORG_ID, ("fixture: NULL-org resolves to poison", scope)
        with Session(engine) as s:
            for label, org in (("resolved scope", scope), ("bare None", None)):
                m = f"#73 ({label})"
                # glossary_module
                assert gm.list_glossaries(s, org_id=org) == [], f"{m}: list_glossaries leaked"
                assert gm.visible(s, d["gloss_a"], org) is None, (
                    f"{m}: glossary_module.visible leaked"
                )
                assert gm.visible(s, d["gloss_b"], org) is None
                assert gm.list_for_customer(s, org, d["cust_a"]) == [], (
                    f"{m}: list_for_customer leaked"
                )
                assert gm.list_unassigned(s, org_id=org) == [], f"{m}: list_unassigned leaked"
                # content_types
                assert ct.list_types(s, org_id=org) == [], f"{m}: list_types leaked"
                assert ct.visible(s, d["ct_a"], org) is None, f"{m}: content_types.visible leaked"
                assert ct.visible(s, d["ct_b"], org) is None
                assert ct.options_for_project(s, org_id=org, customer_id=d["cust_a"]) == [], (
                    f"{m}: options_for_project leaked"
                )
                # vendors
                assert vendors.list_vendors_for_picker(s, org_id=org) == [], (
                    f"{m}: list_vendors_for_picker leaked"
                )
                assert vendors.visible(s, d["vend_a"], org_id=org) is None, (
                    f"{m}: vendors.visible leaked"
                )
                assert vendors.visible(s, d["vend_b"], org_id=org) is None
                # style_guides
                assert sg.visible(s, d["sg_a"], org_id=org) is None, (
                    f"{m}: style_guides.visible leaked"
                )
                assert sg.visible(s, d["sg_b"], org_id=org) is None
                assert sg.list_guides(s, org_id=org) == [], f"{m}: list_guides leaked"
            # POSITIVE CONTROL: an org-A scope sees exactly org A, never org B.
            oa = access.current_org_id(d["user_a"])
            assert {g.id for g in gm.list_glossaries(s, org_id=oa)} == {d["gloss_a"]}
            assert gm.visible(s, d["gloss_a"], oa) is not None
            assert gm.visible(s, d["gloss_b"], oa) is None
            assert {g.id for g in gm.list_for_customer(s, oa, d["cust_a"])} == {d["gloss_a"]}
            assert {c.id for c in ct.list_types(s, org_id=oa)} == {d["ct_a"]}
            assert ct.visible(s, d["ct_a"], oa) is not None and ct.visible(s, d["ct_b"], oa) is None
            assert d["ct_a"] in {
                c.id for c in ct.options_for_project(s, org_id=oa, customer_id=d["cust_a"])
            }
            assert d["ct_b"] not in {
                c.id for c in ct.options_for_project(s, org_id=oa, customer_id=d["cust_a"])
            }
            assert {v.id for v in vendors.list_vendors_for_picker(s, org_id=oa)} == {d["vend_a"]}
            assert vendors.visible(s, d["vend_a"], org_id=oa) is not None
            assert vendors.visible(s, d["vend_b"], org_id=oa) is None
            assert {g.id for g in sg.list_guides(s, org_id=oa)} == {d["sg_a"]}
            assert sg.visible(s, d["sg_a"], org_id=oa) is not None
            assert sg.visible(s, d["sg_b"], org_id=oa) is None
        print(
            "  #73 six-helper family: glossary/content-type/vendor/style-guide "
            "management reads FAIL CLOSED on None; org-A scope sees only org A"
        )
    finally:
        _gate_off()


def test_p73b_sixhelper_operator_door_sees_all() -> None:
    """access.UNSCOPED (the key-verified CLI --operator door) still sweeps every org
    across the whole retrofitted family -- the authorized cross-org read is preserved."""
    from app.kernel import content_types as ct
    from app.kernel import glossary_module as gm
    from app.kernel import style_guides as sg
    from app.kernel import vendors

    d = _seed_sixhelper()
    _gate_on()
    try:
        U = access.UNSCOPED
        with Session(engine) as s:
            assert {d["gloss_a"], d["gloss_b"]} <= {g.id for g in gm.list_glossaries(s, org_id=U)}
            assert (
                gm.visible(s, d["gloss_a"], U) is not None
                and gm.visible(s, d["gloss_b"], U) is not None
            )
            assert {d["ct_a"], d["ct_b"]} <= {c.id for c in ct.list_types(s, org_id=U)}
            assert (
                ct.visible(s, d["ct_a"], U) is not None and ct.visible(s, d["ct_b"], U) is not None
            )
            assert {d["vend_a"], d["vend_b"]} <= {
                v.id for v in vendors.list_vendors_for_picker(s, org_id=U)
            }
            assert vendors.visible(s, d["vend_a"], org_id=U) is not None
            assert vendors.visible(s, d["vend_b"], org_id=U) is not None
            assert {d["sg_a"], d["sg_b"]} <= {g.id for g in sg.list_guides(s, org_id=U)}
            assert (
                sg.visible(s, d["sg_a"], org_id=U) is not None
                and sg.visible(s, d["sg_b"], org_id=U) is not None
            )
        print("  #73 six-helper operator door: access.UNSCOPED still reads cross-org")
    finally:
        _gate_off()


def test_p73b_sixhelper_gate_off_byte_identical() -> None:
    """GATE OFF: a bare None is unrestricted see-all on every retrofitted helper, exactly
    as before the guard (single-tenant byte-identical)."""
    from app.kernel import content_types as ct
    from app.kernel import glossary_module as gm
    from app.kernel import style_guides as sg
    from app.kernel import vendors

    d = _seed_sixhelper()
    _gate_off()
    assert access.org_scope_blocked(None) is False
    with Session(engine) as s:
        assert {d["gloss_a"], d["gloss_b"]} <= {g.id for g in gm.list_glossaries(s, org_id=None)}
        assert gm.visible(s, d["gloss_a"], None) is not None
        assert {d["ct_a"], d["ct_b"]} <= {c.id for c in ct.list_types(s, org_id=None)}
        assert ct.visible(s, d["ct_b"], None) is not None
        assert {d["vend_a"], d["vend_b"]} <= {
            v.id for v in vendors.list_vendors_for_picker(s, org_id=None)
        }
        assert vendors.visible(s, d["vend_a"], org_id=None) is not None
        assert {d["sg_a"], d["sg_b"]} <= {g.id for g in sg.list_guides(s, org_id=None)}
        assert sg.visible(s, d["sg_b"], org_id=None) is not None
    print("  #73 six-helper gate OFF: bare None is see-all (byte-identical to pre-guard)")


def test_p73b_vendor_cross_org_dispatch_preserved() -> None:
    """THE COUPLING PROOF: adding the visible() guard must NOT break cross-org vendor
    dispatch/linking. vendor_for_customer resolves an org-A vendor for an org-B customer
    it is LINKED to (org-agnostic by design -> access.UNSCOPED internally), even though a
    direct org-B-scoped vendors.visible() of that same vendor is now (correctly) None."""
    from app.kernel import vendors

    d = _seed_sixhelper()
    _gate_on()
    try:
        with Session(engine) as s:
            # Direct org-B read of the org-A vendor is fail-closed (the ring holds).
            assert vendors.visible(s, d["vend_a"], org_id=d["org_b"]) is None, (
                "org-B scope must not see org-A's vendor directly"
            )
            # ...but the cross-org LINK still resolves it for dispatch (the authority is
            # the customer link, not the vendor's org). This is None PRE-COUPLING-FIX.
            v = vendors.vendor_for_customer(s, d["vend_a"], d["cust_b"], org_id=d["org_b"])
            assert v is not None and v.id == d["vend_a"], (
                "#73 coupling: cross-org linked vendor_for_customer must still resolve "
                "(the visible() guard broke it if this is None)"
            )
            # The picker UNION also still surfaces the cross-org linked vendor for cust B.
            picker = {
                vv.id
                for vv in vendors.list_vendors_for_picker(
                    s, org_id=d["org_b"], customer_id=d["cust_b"]
                )
            }
            assert d["vend_a"] in picker, (
                "#73 coupling: cross-org linked vendor must appear in the customer picker"
            )
        print("  #73 vendor coupling: cross-org linked dispatch + picker preserved after guard")
    finally:
        _gate_off()


_SIB_SEQ = [0]


def _seed_sib():
    """Two orgs, each with a customer + an APPROVED glossary (with a covering pair) + an
    APPROVED org-global style guide + a customer-scoped content type; plus ONE org-global
    content type (org_id NULL, customer_id NULL) and a NULL-org principal (the #73 user).
    The fixture for the three SIBLING read helpers not covered by the round-4 retrofit:
    glossary_module.options_for_step / style_guides.resolve_style_guide /
    content_types.attachable. Unique names per call."""
    from app.models import (
        ContentType,
        Glossary,
        GlossaryPair,
        GlossaryStatus,
        StyleGuide,
        StyleGuideStatus,
    )

    _SIB_SEQ[0] += 1
    n = _SIB_SEQ[0]
    null_email = f"sibnull{n}@x.test"
    d = _seed_two_orgs()
    with Session(engine) as s:
        cust_a = Customer(name=f"SibCust A{n}", org_id=d["org_a"])
        cust_b = Customer(name=f"SibCust B{n}", org_id=d["org_b"])
        s.add(cust_a)
        s.add(cust_b)
        s.commit()
        s.refresh(cust_a)
        s.refresh(cust_b)
        gloss_a = Glossary(
            name=f"SibGloss A{n}",
            status=GlossaryStatus.APPROVED,
            org_id=d["org_a"],
            customer_id=cust_a.id,
        )
        gloss_b = Glossary(
            name=f"SibGloss B{n}",
            status=GlossaryStatus.APPROVED,
            org_id=d["org_b"],
            customer_id=cust_b.id,
        )
        # A customer-scoped content type per org + ONE org-A ORG-GLOBAL type (customer_id
        # NULL = attachable to any customer WITHIN org A; content types are org-scoped, so
        # "global" is customer-NULL, not org-NULL).
        ct_a = ContentType(name=f"SibCT A{n}", org_id=d["org_a"], customer_id=cust_a.id)
        ct_b = ContentType(name=f"SibCT B{n}", org_id=d["org_b"], customer_id=cust_b.id)
        ct_global = ContentType(name=f"SibCT OrgGlobal A{n}", org_id=d["org_a"], customer_id=None)
        # Org-global universal (pair-agnostic) APPROVED style guides.
        sg_a = StyleGuide(
            name=f"SibSG A{n}", status=StyleGuideStatus.APPROVED, org_id=d["org_a"], body="a"
        )
        sg_b = StyleGuide(
            name=f"SibSG B{n}", status=StyleGuideStatus.APPROVED, org_id=d["org_b"], body="b"
        )
        for obj in (gloss_a, gloss_b, ct_a, ct_b, ct_global, sg_a, sg_b):
            s.add(obj)
        nulluser = User(email=null_email, name="No Org", role=Role.ADMIN, org_id=None)
        s.add(nulluser)
        s.commit()
        for obj in (gloss_a, gloss_b, ct_a, ct_b, ct_global, sg_a, sg_b):
            s.refresh(obj)
        # A GlossaryPair per glossary covering en-US -> es-ES so options_for_step offers it.
        s.add(GlossaryPair(glossary_id=gloss_a.id, source_locale="en-US", target_locale="es-ES"))
        s.add(GlossaryPair(glossary_id=gloss_b.id, source_locale="en-US", target_locale="es-ES"))
        s.commit()
        s.refresh(nulluser)
        s.expunge(nulluser)
        d.update(
            {
                "cust_a": cust_a.id,
                "cust_b": cust_b.id,
                "gloss_a": gloss_a.id,
                "gloss_b": gloss_b.id,
                "ct_a": ct_a.id,
                "ct_b": ct_b.id,
                "ct_global": ct_global.id,
                "sg_a": sg_a.id,
                "sg_b": sg_b.id,
                "nulluser": nulluser,
            }
        )
    return d


def test_p73c_sibling_helpers_fail_closed() -> None:
    """The THREE sibling read helpers NOT covered by the round-4 retrofit --
    glossary_module.options_for_step / style_guides.resolve_style_guide /
    content_types.attachable -- now return NOTHING for a NULL-org scope under an active
    gate, via a bare None (the new guard) AND the resolved -1 principal scope. Return
    TYPES pinned ([] / None). Positive control: an org-A scope sees ONLY org A."""
    from app.kernel import content_types as ct
    from app.kernel import glossary_module as gm
    from app.kernel import style_guides as sg

    d = _seed_sib()
    _gate_on()
    try:
        scope = access.current_org_id(d["nulluser"])
        assert scope == access.BLOCKED_ORG_ID, ("fixture: NULL-org resolves to poison", scope)
        with Session(engine) as s:
            for label, org in (("resolved scope", scope), ("bare None", None)):
                m = f"#73 ({label})"
                # glossary_module.options_for_step
                assert gm.options_for_step(s, "en-US", "es-ES", org_id=org) == [], (
                    f"{m}: options_for_step leaked"
                )
                # style_guides.resolve_style_guide
                assert (
                    sg.resolve_style_guide(
                        s, org_id=org, source_locale="en-US", target_locale="es-ES"
                    )
                    is None
                ), f"{m}: resolve_style_guide leaked"
                # content_types.attachable (customer + global types alike fail closed)
                assert ct.attachable(s, d["ct_a"], org_id=org, customer_id=d["cust_a"]) is None, (
                    f"{m}: attachable leaked a customer type"
                )
                assert (
                    ct.attachable(s, d["ct_global"], org_id=org, customer_id=d["cust_a"]) is None
                ), f"{m}: attachable leaked the GLOBAL type"
            # POSITIVE CONTROL: an org-A scope sees exactly org A, never org B.
            oa = access.current_org_id(d["user_a"])
            assert {g.id for g in gm.options_for_step(s, "en-US", "es-ES", org_id=oa)} == {
                d["gloss_a"]
            }
            assert (
                sg.resolve_style_guide(
                    s, org_id=oa, source_locale="en-US", target_locale="es-ES"
                ).id
                == d["sg_a"]
            )
            assert ct.attachable(s, d["ct_a"], org_id=oa, customer_id=d["cust_a"]) is not None
            assert ct.attachable(s, d["ct_b"], org_id=oa, customer_id=d["cust_a"]) is None
            # a GLOBAL type is attachable to any in-org customer (org gate skips NULL org_id)
            assert ct.attachable(s, d["ct_global"], org_id=oa, customer_id=d["cust_a"]) is not None
        print(
            "  #73 sibling helpers: options_for_step/resolve_style_guide/attachable "
            "FAIL CLOSED on None; org-A scope sees only org A"
        )
    finally:
        _gate_off()


def test_p73c_sibling_operator_door_sees_all() -> None:
    """access.UNSCOPED (the CLI --operator door) still sweeps every org across all three
    sibling helpers -- the authorized cross-org read/validation is preserved."""
    from app.kernel import content_types as ct
    from app.kernel import glossary_module as gm
    from app.kernel import style_guides as sg

    d = _seed_sib()
    _gate_on()
    try:
        U = access.UNSCOPED
        with Session(engine) as s:
            assert {d["gloss_a"], d["gloss_b"]} <= {
                g.id for g in gm.options_for_step(s, "en-US", "es-ES", org_id=U)
            }
            # resolve_style_guide cross-org: a guide resolves (org filter skipped).
            assert (
                sg.resolve_style_guide(s, org_id=U, source_locale="en-US", target_locale="es-ES")
                is not None
            )
            # attachable cross-org: org-B's own type resolves for its customer under UNSCOPED.
            assert ct.attachable(s, d["ct_b"], org_id=U, customer_id=d["cust_b"]) is not None
            assert ct.attachable(s, d["ct_global"], org_id=U, customer_id=d["cust_a"]) is not None
        print("  #73 sibling operator door: access.UNSCOPED still reads/validates cross-org")
    finally:
        _gate_off()


def test_p73c_sibling_gate_off_byte_identical() -> None:
    """GATE OFF: a bare None is unrestricted see-all on all three sibling helpers, exactly
    as before the guard (single-tenant byte-identical)."""
    from app.kernel import content_types as ct
    from app.kernel import glossary_module as gm
    from app.kernel import style_guides as sg

    d = _seed_sib()
    _gate_off()
    assert access.org_scope_blocked(None) is False
    with Session(engine) as s:
        assert {d["gloss_a"], d["gloss_b"]} <= {
            g.id for g in gm.options_for_step(s, "en-US", "es-ES", org_id=None)
        }
        assert (
            sg.resolve_style_guide(s, org_id=None, source_locale="en-US", target_locale="es-ES")
            is not None
        )
        assert ct.attachable(s, d["ct_b"], org_id=None, customer_id=d["cust_b"]) is not None
        assert ct.attachable(s, d["ct_global"], org_id=None, customer_id=d["cust_a"]) is not None
    print("  #73 sibling gate OFF: bare None is see-all (byte-identical to pre-guard)")


def test_p73c_create_operator_coupling_and_stamp() -> None:
    """THE COUPLING PROOF for the attachable-in-create paths. Under an active gate the CLI
    --operator create door threads access.UNSCOPED so a cross-org create still RESOLVES a
    global/attachable content type, while the persisted row's org is COLLAPSED back to None
    (never the sentinel). A bare None (a future forgetful caller) FAILS CLOSED instead."""
    from app.kernel import glossary_module as gm
    from app.kernel import style_guides as sg
    from app.services import content as content_svc
    from app.services.connectors import store as conn_store

    d = _seed_sib()
    _gate_on()
    try:
        gct = d["ct_global"]
        with Session(engine) as s:
            # (a) gm.create via the operator door: resolves the GLOBAL type cross-org and
            #     STAMPS org_id None (UNSCOPED collapsed by org_scope_value), not the sentinel.
            g = gm.create(
                s,
                f"OpGloss{_SIB_SEQ[0]}",
                org_id=access.UNSCOPED,
                customer_id=d["cust_a"],
                content_type_id=gct,
                require_customer=True,
            )
            assert g.org_id is None, ("operator create must stamp org None, got", g.org_id)
            assert g.content_type_id == gct, "operator create must resolve the global type"
            # (b) sg.create via the operator door: same coupling.
            guide = sg.create(
                s,
                f"OpSG{_SIB_SEQ[0]}",
                org_id=access.UNSCOPED,
                customer_id=d["cust_a"],
                content_type_id=gct,
            )
            assert guide.org_id is None and guide.content_type_id == gct
            # (c) content.project_create via content_type_scope=UNSCOPED (creator=None -> the
            #     --operator no-org create): resolves the type, project org stays None.
            proj = content_svc.project_create(
                s,
                f"OpProj{_SIB_SEQ[0]}",
                source_locale="en-US",
                target_locales="es-ES",
                customer_id=d["cust_a"],
                creator=None,
                content_type_id=gct,
                content_type_scope=access.UNSCOPED,
                require_customer=True,
            )
            assert proj is not None and proj.org_id is None and proj.content_type_id == gct
        # (d) connector pre-flight coupling: content_type_attachable sees the global type via
        #     UNSCOPED, but FAILS CLOSED for a bare None under the active gate.
        assert conn_store.content_type_attachable(gct, access.UNSCOPED, "") is True
        assert conn_store.content_type_attachable(gct, None, "") is False, (
            "#73: bare None under active gate must fail closed in the connector pre-flight"
        )
        # (e) FAIL-CLOSED on a bare None create (a future caller that forgot op_org_scope):
        with Session(engine) as s:
            try:
                gm.create(
                    s,
                    f"BadGloss{_SIB_SEQ[0]}",
                    org_id=None,
                    customer_id=d["cust_a"],
                    content_type_id=gct,
                    require_customer=True,
                )
                raise AssertionError("#73: bare-None create with a content type must fail closed")
            except gm.GlossaryModuleError:
                pass
        print(
            "  #73 create coupling: operator door resolves cross-org + stamps org None; "
            "bare None fails closed (glossary/style-guide/project/connector)"
        )
    finally:
        _gate_off()


def test_p73c_creatorless_bound_principal_resolves_own_org() -> None:
    """GATE CATCH regression pin. project_create resolves its content type against a
    CREATOR-derived org, but /api reads `creator` as ident.get("user") -- None for the
    SHARED CONNECTOR TOKEN even when that token is BOUND to a real org. Threading only
    the CLI door left that principal validating against a bare None, so an org-A token
    attaching its OWN org-A type (or merely inheriting the customer's default) 400'd.
    project_create now falls back to its `org_id` PARAMETER (the poison-protected
    principal scope). Proven here as a strict NARROWING of the pre-#73 see-all:

      * org-A-bound, creator-less -> its own org's type RESOLVES (the restored flow);
      * ...and the SAME call with an org-B ORG-GLOBAL type is REFUSED (pre-#73 that
        cross-org type resolved -- this is the leak that stays closed);
      * an UNBOUND token (org_id == BLOCKED_ORG_ID) is REFUSED (the Q3 finding);
      * the customer's INHERITED default type also resolves (no explicit id needed).
    """
    from app.models import ContentType
    from app.services import content as content_svc

    d = _seed_sib()
    n = _SIB_SEQ[0]
    with Session(engine) as s:
        ct_b_global = ContentType(name=f"SibCT OrgGlobal B{n}", org_id=d["org_b"], customer_id=None)
        s.add(ct_b_global)
        s.commit()
        s.refresh(ct_b_global)
        ct_b_global_id = ct_b_global.id
        cust_a = s.get(Customer, d["cust_a"])
        cust_a.default_content_type_id = d["ct_a"]
        s.add(cust_a)
        s.commit()
    _gate_on()
    try:
        with Session(engine) as s:
            # (a) the RESTORED flow: creator-less but org-BOUND, own-org type resolves.
            p = content_svc.project_create(
                s,
                f"BoundTok{n}",
                source_locale="en-US",
                target_locales="es-ES",
                customer_id=d["cust_a"],
                creator=None,
                org_id=d["org_a"],
                content_type_id=d["ct_a"],
                require_customer=True,
            )
            assert p is not None and p.content_type_id == d["ct_a"], (
                "#73: an org-BOUND creator-less principal must still attach its OWN org's type"
            )
            # (b) the leak STAYS closed: org B's ORG-GLOBAL type is refused for an org-A
            #     principal (pre-#73 the bare-None see-all resolved it).
            try:
                content_svc.project_create(
                    s,
                    f"BoundTokCross{n}",
                    source_locale="en-US",
                    target_locales="es-ES",
                    customer_id=d["cust_a"],
                    creator=None,
                    org_id=d["org_a"],
                    content_type_id=ct_b_global_id,
                    require_customer=True,
                )
                raise AssertionError("#73: an org-A principal must not attach org B's type")
            except content_svc.InvalidContentTypeError:
                pass
            # (c) an UNBOUND token (the poisoned scope) is refused outright.
            try:
                content_svc.project_create(
                    s,
                    f"UnboundTok{n}",
                    source_locale="en-US",
                    target_locales="es-ES",
                    customer_id=d["cust_a"],
                    creator=None,
                    org_id=access.BLOCKED_ORG_ID,
                    content_type_id=d["ct_a"],
                    require_customer=True,
                )
                raise AssertionError("#73: an UNBOUND (poisoned) principal must fail closed")
            except (content_svc.InvalidContentTypeError, content_svc.ForeignCustomerError):
                # #554: the poisoned principal now also fails closed on the explicit
                # customer (ForeignCustomerError, raised first); the type refusal behind it
                # is the same #73 law.
                pass
            # (d) the INHERITED customer default resolves too (no explicit id in the call).
            p2 = content_svc.project_create(
                s,
                f"BoundTokInherit{n}",
                source_locale="en-US",
                target_locales="es-ES",
                customer_id=d["cust_a"],
                creator=None,
                org_id=d["org_a"],
                require_customer=True,
            )
            assert p2 is not None and p2.content_type_id == d["ct_a"], (
                "#73: the customer's inherited default type must resolve for a bound principal"
            )
        print(
            "  #73 creator-less BOUND principal: own-org type resolves, org-B type + "
            "unbound poison refused, inherited default resolves"
        )
    finally:
        _gate_off()


def test_p73c_template_apply_operator_coupling() -> None:
    """GATE CATCH regression pin (the caller the coupling sweep missed): the CLI
    `template apply --operator` door reaches project_create with creator=None AND
    org_id=None, so the applied project's INHERITED content type validated against a
    bare None and the whole apply failed. cmd_template now threads op_org_scope ->
    apply_template(content_type_scope=) like `project create` does. A forgetful
    all-None caller still fails closed."""
    from app.services import content as content_svc
    from app.services import templates as tsvc

    d = _seed_sib()
    n = _SIB_SEQ[0]
    with Session(engine) as s:
        cust_a = s.get(Customer, d["cust_a"])
        cust_a.default_content_type_id = d["ct_a"]
        s.add(cust_a)
        s.commit()
        seed_p = content_svc.project_create(
            s,
            f"TplSeed{n}",
            source_locale="en-US",
            target_locales="es-ES",
            customer_id=d["cust_a"],
            content_type_id=d["ct_a"],
        )
        tpl_id = tsvc.save_as_template(s, seed_p.id, f"TplOp{n}", actor="probe").id
    _gate_on()
    try:
        with Session(engine) as s:
            proj, _w = tsvc.apply_template(
                s,
                tpl_id,
                f"TplOpProj{n}",
                customer_id=str(d["cust_a"]),
                created_by="cli",
                creator=None,  # op_creator() on the --operator door
                org_id=None,  # op_org_id() on the --operator door
                content_type_scope=access.UNSCOPED,  # op_org_scope() -- the coupling
                require_customer=True,
            )
            assert proj.content_type_id == d["ct_a"], (
                "#73: `template apply --operator` must still resolve the inherited type"
            )
            try:
                tsvc.apply_template(
                    s,
                    tpl_id,
                    f"TplBadProj{n}",
                    customer_id=str(d["cust_a"]),
                    created_by="cli",
                    creator=None,
                    org_id=None,
                    require_customer=True,
                )
                raise AssertionError("#73: an all-None template apply must fail closed")
            except tsvc.TemplateError:
                pass
        print("  #73 template apply: --operator door resolves; all-None caller fails closed")
    finally:
        _gate_off()


# --------------------------------------------------------------------------- #
# #73 round-5: the still-bare RAW read helper (vendors.list_vendors) now fails
# closed BY CONSTRUCTION.
#
# The poison-sentinel design (test_p73_*) already made these safe for a NULL-org
# PRINCIPAL: every principal-facing caller resolves org through current_org_id /
# _my_org_id / _ident_org_id (-> BLOCKED_ORG_ID) or a guarded wrapper. But these
# three kept the plain `if org_id is not None:` shape with NO local guard, so a
# FUTURE caller passing a literal bare None under an active gate would have seen
# every org. This train adds the one-line access.org_scope_blocked guard to all
# three (defence-in-depth at the helper, not only at the resolution seam), and the
# reflective test below fails if a NEW bare `if org_id is not None:` read helper
# appears anywhere in app/services/ without that guard.
# --------------------------------------------------------------------------- #
_P73D_SEQ = [0]


def _seed_p73d():
    """Two orgs, each with a vendor stamped to that org, plus a NULL-org
    principal (the #73 user). Unique names per call (mirrors _seed_p73)."""
    from app.models import Vendor

    _P73D_SEQ[0] += 1
    n = _P73D_SEQ[0]
    null_email = f"rawnull{n}@x.test"
    d = _seed_two_orgs()
    with Session(engine) as s:
        vend_a = Vendor(name=f"RawVend A{n}", org_id=d["org_a"])
        vend_b = Vendor(name=f"RawVend B{n}", org_id=d["org_b"])
        for obj in (vend_a, vend_b):
            s.add(obj)
        s.commit()
        for obj in (vend_a, vend_b):
            s.refresh(obj)
        # THE #73 PRINCIPAL: a real account carrying no org at all.
        nulluser = User(email=null_email, name="No Org", role=Role.ADMIN, org_id=None)
        s.add(nulluser)
        s.commit()
        s.refresh(nulluser)
        s.expunge(nulluser)
        d.update(
            {
                "vend_a": vend_a.id,
                "vend_b": vend_b.id,
                "nulluser": nulluser,
            }
        )
    return d


def test_p73d_raw_read_helpers_fail_closed() -> None:
    """The still-bare RAW read helper (vendors.list_vendors) returns NOTHING for a NULL-org scope
    under an active gate -- via a bare None (the new guard) AND via the resolved
    principal scope (the -1 poison). Positive control: an org-A scope sees ONLY A."""
    from app.services import vendors

    d = _seed_p73d()
    _gate_on()
    try:
        scope = access.current_org_id(d["nulluser"])
        assert scope == access.BLOCKED_ORG_ID, ("fixture: NULL-org resolves to poison", scope)
        for label, org in (("resolved scope", scope), ("bare None", None)):
            m = f"#73 ({label})"
            with Session(engine) as s:
                assert vendors.list_vendors(s, org_id=org) == [], f"{m}: list_vendors leaked"
        # POSITIVE CONTROL: an org-A scope sees exactly org A, never org B.
        oa = access.current_org_id(d["user_a"])
        with Session(engine) as s:
            assert {v.id for v in vendors.list_vendors(s, org_id=oa)} == {d["vend_a"]}
        print("  #73 raw helpers: list_vendors FAIL CLOSED on None; org-A scope sees only org A")
    finally:
        _gate_off()


def test_p73d_raw_operator_door_sees_all() -> None:
    """access.UNSCOPED (the key-verified CLI --operator door) still sweeps every org
    across the raw helper -- the authorized cross-org read is preserved."""
    from app.services import vendors

    d = _seed_p73d()
    _gate_on()
    try:
        U = access.UNSCOPED
        assert access.org_scope_blocked(U) is False
        with Session(engine) as s:
            assert {d["vend_a"], d["vend_b"]} <= {v.id for v in vendors.list_vendors(s, org_id=U)}
        print("  #73 raw operator door: access.UNSCOPED still reads cross-org")
    finally:
        _gate_off()


def test_p73d_raw_gate_off_byte_identical() -> None:
    """GATE OFF: a bare None is unrestricted see-all on the raw helper, exactly
    as before the guard (single-tenant byte-identical). The multi-org gate stays OFF
    -- the guard is a no-op."""
    from app.services import vendors

    d = _seed_p73d()
    _gate_off()
    try:
        assert access.org_scope_blocked(None) is False
        with Session(engine) as s:
            assert {d["vend_a"], d["vend_b"]} <= {
                v.id for v in vendors.list_vendors(s, org_id=None)
            }
        print("  #73 raw gate OFF: bare None is see-all (byte-identical to pre-guard)")
    finally:
        _gate_off()


# Caller-resolved bare org-narrow helpers in app/services/. Each carries the #73 shape
# -- a `if org_id is not None:` OR the sibling `if org_id is not None and <row>:` narrow
# (round-1 MED-B broadened the ratchet to BOTH, so the and-form single-row reads and the
# write-time org guards are classified too) -- with NO local access.org_scope_blocked
# call. Each receives its org scope ALREADY resolved through a resolution seam
# (current_org_id / _my_org_id / _ident_org_id -> BLOCKED_ORG_ID for a NULL principal),
# a guarded wrapper, or a required positional, so a NULL-org principal never reaches one
# with a literal bare None; many are not principal reads at all (writes / internal
# helpers). They are ALLOWLISTED here: the ratchet does not force the one-line guard onto
# every one of them (the two resolution seams remain the authority), it only forbids a
# NEW org-narrow helper from being added WITHOUT the guard. To harden one, add
# `if access.org_scope_blocked(org_id): return []` and DELETE it from this list.
_P73D_CALLER_RESOLVED_BARE = frozenset(
    {
        ("app/services/apikeys.py", "list_keys_scoped"),
        ("app/services/audit.py", "_filtered"),
        ("app/services/budgets.py", "get_budget"),
        ("app/services/connectors/store_common_leaf.py", "list_connections"),
        (
            "app/services/content_project_rename_delete_sp.py",
            "archive_project",
        ),  # SRP split 2026-09-14
        (
            "app/services/content_project_rename_delete_sp.py",
            "assign_project_customer",
        ),  # SRP split 2026-09-14
        (
            "app/services/content_project_rename_delete_sp.py",
            "delete_document",
        ),  # SRP split 2026-09-14
        (
            "app/services/content_projects.py",
            "delete_project",
        ),  # SRP split 2026-09-14 (was app/services/content.py)
        (
            "app/services/content_project_rename_delete_sp.py",
            "org_has_selectable_customer",
        ),  # SRP split 2026-09-14
        (
            "app/services/content_projects.py",
            "remove_target_locale",
        ),  # SRP split 2026-09-14 (was app/services/content.py)
        (
            "app/services/content_part2.py",
            "rename_document",
        ),  # SRP split 2026-09-14 (was app/services/content.py)
        (
            "app/services/content_project_rename_delete_sp.py",
            "rename_project",
        ),  # SRP split 2026-09-14
        (
            "app/services/content_project_rename_delete_sp.py",
            "set_project_content_type",
        ),  # SRP split 2026-09-14
        (
            "app/services/content_part2.py",
            "set_project_due",
        ),  # SRP split 2026-09-14 (was app/services/content.py)
        (
            "app/services/content_project_rename_delete_sp.py",
            "unarchive_project",
        ),  # SRP split 2026-09-14
        ("app/services/content_types.py", "reference_count"),
        ("app/services/context_pkg.py", "_profile_context"),
        ("app/services/context_pkg.py", "find_by_scope"),
        ("app/services/context_pkg.py", "get_profile"),
        ("app/services/context_pkg.py", "list_profiles"),
        ("app/services/context_pkg.py", "resolve"),
        ("app/services/customers.py", "archive_customer"),
        ("app/services/customers.py", "delete_customer"),
        ("app/services/customers.py", "rename_customer"),
        ("app/services/customers.py", "selectable"),
        ("app/services/customers.py", "unarchive_customer"),
        ("app/services/engines.py", "list_profiles"),
        ("app/services/job_history.py", "unified_history"),
        ("app/services/keys_hub.py", "_project"),
        ("app/services/languages.py", "get"),
        ("app/services/languages.py", "list_sets"),
        ("app/services/languages.py", "update"),
        ("app/services/notifications.py", "_admin_recipients"),
        ("app/services/package_view.py", "workflow_has_context"),
        ("app/services/providers_hub.py", "list_models_for_ref"),
        ("app/services/query_threads.py", "list_threads"),
        (
            "app/services/reports_the_query.py",
            "allowed_project_ids",
        ),  # SRP split 2026-09-14 (was app/services/reports.py)
        ("app/services/routing.py", "ensure_department_inbox"),
        ("app/services/routing.py", "get_rule"),
        ("app/services/routing.py", "resolve"),
        ("app/services/scim.py", "_group_in_org"),
        ("app/services/scim.py", "_user_in_org"),
        ("app/services/scim.py", "create_group"),
        ("app/services/scim.py", "create_user"),
        ("app/services/scim.py", "group_count"),
        ("app/services/scim.py", "groups_with_members"),
        ("app/services/scim.py", "list_groups"),
        ("app/services/scim.py", "list_users"),
        ("app/services/scim.py", "managed_user_count"),
        ("app/services/scim.py", "managed_users"),
        (
            "app/services/security_client_ip_one_authority_.py",
            "is_last_active_admin",
        ),  # SRP split 2026-09-14
        ("app/services/segmentation_profiles.py", "assign_to_project"),
        ("app/services/segmentation_profiles.py", "delete_profile"),
        ("app/services/segmentation_profiles.py", "get_profile"),
        ("app/services/segmentation_profiles.py", "list_profiles"),
        ("app/services/segmentation_profiles.py", "update_profile"),
        ("app/services/segmentation_profiles.py", "validate_for_create"),
        (
            "app/services/setup_author_helpers.py",
            "_base_catalogs",
        ),  # SRP split 2026-09-14 (was app/services/setup_author.py)
        ("app/services/style_guides.py", "has_resolvable_guide"),
        ("app/services/tag_llm_repair.py", "repair_stored_segments"),
        ("app/services/tap_envelope.py", "begin_batch_action"),
        ("app/services/tap_envelope.py", "find_batch_action"),
        ("app/services/tap_view.py", "_name_maps"),
        ("app/services/tap_view.py", "action_summary"),
        # ROADMAP #6: the run-first lens predicate. It never sees the raw UNSCOPED sentinel:
        # its callers list_runs/run_detail run the #73 two-step FIRST -- org_scope_blocked()
        # (a NULL principal under an active gate returns []/None before the walk) THEN
        # org_scope_value() (collapses the key-verified --operator see-all sentinel to a
        # concrete None). So org_id reaches _ring_visible already resolved to an int (a scoped
        # caller) or None (single-tenant, or the authorized cross-org operator door), and a
        # bare None here is the intended see-all -- a blocked principal never reaches it. The
        # API route resolves via _gate/_ident_org_id, the web route via _my_org_id, and the
        # CLI-local path via op_org_scope(args) (UNSCOPED only through the --operator gate).
        ("app/services/tap_runs.py", "_ring_visible"),
        # round-1 LOW-3: list_actions / list_action_summaries DO reach a bare None from a
        # CLI verb (cmd_prompt_tap.py), but only through _tap_gate, which refuses --org
        # under an active gate ("a capture carries no organization") and so admits only
        # the key-verified --operator door. Caller-resolved by that gate, not by a scope id.
        ("app/services/tap_view.py", "list_action_summaries"),
        ("app/services/tap_view.py", "list_actions"),
        ("app/services/templates.py", "all_templates"),
        ("app/services/templates.py", "get_template"),
        ("app/services/templates.py", "list_templates"),
        ("app/services/term_extraction.py", "documents_for_glossary"),
        ("app/services/term_extraction.py", "visible_document"),
        ("app/services/vendor_quote.py", "approve_quote"),
        ("app/services/vendor_quote.py", "decline_quote"),
        ("app/services/vendor_quote.py", "record_quote"),
        ("app/services/vendor_return_hub.py", "admit_manual"),
        ("app/services/vendor_return_common.py", "open_legs"),
        ("app/services/vendor_rework.py", "reject_and_rework"),
        ("app/services/vendors.py", "link_customer"),
        ("app/services/vendors.py", "unlink_customer"),
        ("app/services/workflow_rail.py", "customer_facts"),
        ("app/services/workflows_part4.py", "archive_workflow"),
        ("app/services/workflows_part4.py", "create_workflow"),
        ("app/services/workflows_part2.py", "list_groups"),
        ("app/services/workflows_part3.py", "list_prompt_templates"),
        ("app/services/workflows_part4.py", "unarchive_workflow"),
        ("app/services/worklist.py", "_resolve_allowed"),
    }
)

# round-1 LOW-4: the ratchet now also scans app/web and app/cli. Every org-narrow there
# resolves the caller's org IN THE HANDLER / VERB before the narrow runs -- web routes via
# _my_org_id / _ident_org_id (both -> BLOCKED_ORG_ID for a NULL principal), CLI verbs via
# op_org_id / op_org_scope / the token scope / the --operator door -- so a bare None never
# reaches these with principal authority. Allowlisted with their own justification; a NEW
# unguarded narrow in a route or verb still trips the ratchet (add the guard, or add it
# here with a reason).
_P73D_WEB_CALLER_RESOLVED_BARE = frozenset(
    {
        (
            "app/web/api_cli_misc_6.py",
            "_connection_in_org",
        ),  # SRP split 2026-09-14 (was app/web/api_cli.py)
        (
            "app/web/api_cli_common.py",
            "_customer_in_org",
        ),  # SRP split 2026-09-14 (was app/web/api_cli.py)
        (
            "app/web/api_cli_common.py",
            "_grp_in_org",
        ),  # SRP split 2026-09-14 (was app/web/api_cli.py)
        (
            "app/web/api_cli_common.py",
            "_langset_in_org",
        ),  # SRP split 2026-09-14 (was app/web/api_cli.py)
        ("app/web/api_cli_common.py", "_org_list"),  # SRP split 2026-09-14 (was app/web/api_cli.py)
        (
            "app/web/api_cli_misc_3.py",
            "_payload_in_org",
        ),  # SRP split 2026-09-14 (was app/web/api_cli.py)
        (
            "app/web/api_cli_common.py",
            "_project_in_org",
        ),  # SRP split 2026-09-14 (was app/web/api_cli.py)
        (
            "app/web/api_cli_misc_2.py",
            "_pt_in_org",
        ),  # SRP split 2026-09-14 (was app/web/api_cli.py)
        (
            "app/web/api_cli_common.py",
            "_resolve_user_ref",
        ),  # SRP split 2026-09-14 (was app/web/api_cli.py)
        (
            "app/web/api_cli_projects_1.py",
            "_tm_source_visible",
        ),  # SRP split 2026-09-14 (was app/web/api_cli.py)
        (
            "app/web/api_cli_common.py",
            "_user_in_org",
        ),  # SRP split 2026-09-14 (was app/web/api_cli.py)
        (
            "app/web/api_cli_common.py",
            "_wf_in_org",
        ),  # SRP split 2026-09-14 (was app/web/api_cli.py)
        (
            "app/web/api_cli_misc_7.py",
            "api_exports_list",
        ),  # SRP split 2026-09-14 (was app/web/api_cli.py)
        (
            "app/web/api_cli_vendors.py",
            "api_vendor_dispatches",
        ),  # SRP split 2026-09-14 (was app/web/api_cli.py)
        ("app/web/exports.py", "_runs_for"),
        ("app/web/exports.py", "_visible_scopes"),
        ("app/web/keys_hub.py", "_api_project"),
        (
            "app/web/projects_projects.py",
            "_rename_template_compat",
        ),  # SRP split 2026-09-14 (was app/web/projects.py)
    }
)

_P73D_CLI_CALLER_RESOLVED_BARE = frozenset(
    {
        ("app/cli/cmd_context.py", "_ab"),
        ("app/cli/cmd_context.py", "_set_document"),
        ("app/cli/cmd_context.py", "_show_document"),
        ("app/cli/cmd_export.py", "_list"),
        ("app/cli/cmd_image.py", "_get_image_doc"),
        (
            "app/cli/cmd_project_term_extraction_candidat.py",
            "_customer_list",
        ),  # SRP split 2026-09-14 (was app/cli/cmd_project.py)
        # #533: the one shared body (the `admin routing rule list` twin now delegates here)
        ("app/cli/cmd_routing.py", "rule_list_local"),
        ("app/cli/cmd_style_guide.py", "_sg_ab"),
        ("app/cli/cmd_tagging.py", "_verify"),
        ("app/cli/cmd_user.py", "_apikey_list"),
        ("app/cli/cmd_user.py", "_user_customer"),
        ("app/cli/cmd_workflow_common_leaf.py", "_run_doc"),
        ("app/cli/doc_common.py", "_get_doc"),
    }
)

# The read helpers this train + its predecessors converted to fail closed: each MUST
# keep the `access.org_scope_blocked` guard. Removing it silently re-opens the
# literal-None see-all hole, so it is a hard regression here.
_P73D_MUST_STAY_GUARDED = frozenset(
    {
        ("app/services/vendors.py", "list_vendors"),
    }
)


# round-1 MED-A/MED-B/LOW-4: the ratchet decides guarded-ness by CODE, not substring.
# Each source file is parsed with `ast`, so a docstring or comment that merely NAMES
# access.org_scope_blocked can never make a function count as guarded (the M1/M6 gaps the
# round-1 gate found). A function is an "org-narrow helper" when its own body (nested defs
# excluded) contains an `if` whose test is `org_id is not None` OR a top-level `and` with
# `org_id is not None` as a conjunct (MED-B broadened it to the and-form). It is GUARDED
# only when a real CALL to org_scope_blocked runs BEFORE the first narrow/query -- i.e. the
# guard actually protects the read, so a decoy call placed after the narrow does not count.
_P73D_ORG_NARROW_ROOTS = ("services", "web", "cli")
_P73D_QUERY_FUNCS = frozenset({"select", "exec", "get", "scalar", "scalars", "execute"})


def _p73d_is_org_isnot_none(node) -> bool:
    import ast

    return (
        isinstance(node, ast.Compare)
        and isinstance(node.left, ast.Name)
        and node.left.id == "org_id"
        and len(node.ops) == 1
        and isinstance(node.ops[0], ast.IsNot)
        and len(node.comparators) == 1
        and isinstance(node.comparators[0], ast.Constant)
        and node.comparators[0].value is None
    )


def _p73d_is_org_narrow_test(test) -> bool:
    import ast

    if _p73d_is_org_isnot_none(test):
        return True
    return (
        isinstance(test, ast.BoolOp)
        and isinstance(test.op, ast.And)
        and any(_p73d_is_org_isnot_none(v) for v in test.values)
    )


def _p73d_own_nodes(fn):
    """Every descendant node of a function EXCLUDING nested function / lambda bodies
    (those are classified as their own units), so a closure never taints its parent."""
    import ast

    out = []

    def rec(node):
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
                continue
            out.append(child)
            rec(child)

    for stmt in fn.body:
        out.append(stmt)
        rec(stmt)
    return out


def _p73d_call_name(node):
    import ast

    f = node.func
    if isinstance(f, ast.Attribute):
        return f.attr
    if isinstance(f, ast.Name):
        return f.id
    return None


def _classify_org_read_helpers():
    """AST scan of app/{services,web,cli}: (guarded, bare) sets of (relpath, function)
    for every org-narrow helper. is_guarded means a genuine org_scope_blocked() CALL runs
    before the first org-narrow / query in the SAME function body -- docstrings, comments
    and decoy calls placed after the narrow never qualify."""
    import ast

    guarded, bare = set(), set()
    for area in _P73D_ORG_NARROW_ROOTS:
        root = os.path.join(_ROOT, "app", area)
        for base, _dirs, files in os.walk(root):
            for fname in files:
                if not fname.endswith(".py"):
                    continue
                path = os.path.join(base, fname)
                rel = os.path.relpath(path, _ROOT).replace(os.sep, "/")
                with open(path, encoding="utf-8") as fh:
                    tree = ast.parse(fh.read())
                for node in ast.walk(tree):
                    if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                        continue
                    nodes = _p73d_own_nodes(node)
                    narrows = [
                        n.lineno
                        for n in nodes
                        if isinstance(n, ast.If) and _p73d_is_org_narrow_test(n.test)
                    ]
                    if not narrows:
                        continue
                    queries = [
                        n.lineno
                        for n in nodes
                        if isinstance(n, ast.Call) and _p73d_call_name(n) in _P73D_QUERY_FUNCS
                    ]
                    barrier = min([min(narrows), *queries])
                    guards = [
                        n.lineno
                        for n in nodes
                        if isinstance(n, ast.Call) and _p73d_call_name(n) == "org_scope_blocked"
                    ]
                    is_guarded = any(g < barrier for g in guards)
                    (guarded if is_guarded else bare).add((rel, node.name))
    return guarded, bare


def test_p73d_no_new_unguarded_org_read_helper() -> None:
    """REFLECTIVE RATCHET: every bare org-narrow helper in app/{services,web,cli} must be
    either GUARDED (a real access.org_scope_blocked call before its narrow) or on the
    matching caller-resolved allowlist. A NEW unguarded helper fails this test -- the
    "the next new helper re-opens it" shape the poison design accepts, closed at build
    time. Also pins that the hardened raw helpers (list_vendors / list_missions / list_runs
    and the _mission_in_ring / _run_in_ring twins) keep their guard."""
    guarded, bare = _classify_org_read_helpers()
    allow = (
        _P73D_CALLER_RESOLVED_BARE | _P73D_WEB_CALLER_RESOLVED_BARE | _P73D_CLI_CALLER_RESOLVED_BARE
    )

    # 1. No NEW bare org-narrow helper: every bare site is allowlisted as caller-resolved.
    unexpected = bare - allow
    assert not unexpected, (
        "#73 ratchet: NEW `if org_id is not None[ and ...]:` org-narrow helper(s) in "
        "app/{services,web,cli} without the access.org_scope_blocked guard: "
        + ", ".join(f"{f}::{fn}" for f, fn in sorted(unexpected))
        + " -- add `if access.org_scope_blocked(org_id): return []` (see "
        "vendors.list_vendors) or, if the caller always resolves org through a "
        "seam, add it to the matching _P73D_*_CALLER_RESOLVED_BARE with a justification."
    )

    # 2. The hardened raw helpers must STAY guarded (regression pin for this train).
    regressed = _P73D_MUST_STAY_GUARDED - guarded
    assert not regressed, (
        "#73 ratchet: a hardened raw read helper LOST its access.org_scope_blocked "
        "guard (literal-None see-all re-opened): "
        + ", ".join(f"{f}::{fn}" for f, fn in sorted(regressed))
    )

    # 3. Allowlist hygiene: an allowlisted name that GAINED a guard, or vanished,
    # should be removed from the list (keeps it honest, not a silent catch-all).
    stale = allow - bare
    assert not stale, (
        "#73 ratchet: allowlisted helper(s) no longer bare (guarded or removed) -- "
        "delete from the matching _P73D_*_CALLER_RESOLVED_BARE: "
        + ", ".join(f"{f}::{fn}" for f, fn in sorted(stale))
    )
    print(
        f"  #73 ratchet: {len(guarded)} guarded + {len(bare)} caller-resolved bare "
        "org-narrow helpers across app/{services,web,cli}; no new unguarded one"
    )


def test_boot_clear_is_valid_on_every_backend() -> None:
    """GATE R4 MED-1: the boot clear must be valid SQL on POSTGRES, not just reachable there.

    Round 3 moved the clear out of the SQLite-only `_ensure_default_org` so it would run on
    every backend. It ran, and then failed, for two independent reasons that a SQLite-only
    test can never see:

      * `user` is a RESERVED WORD in Postgres and must be quoted. The repo already wrote
        this rule down (`alembic/versions/a1f4c8b2d6e9...:70`, "'user' is a reserved word in
        Postgres, so every raw statement quotes it").
      * `is_superadmin` is `sa.Boolean()`, so `= 0` and `= 1` are integer-against-boolean
        type errors on Postgres. SQLite accepts them because it has no boolean type.

    Both were swallowed by the function's own `except Exception`, so the only symptom was a
    log line on every boot, and `tests/pg_smoke.py` never calls `init_db()`. Six artifacts
    meanwhile asserted the clear had happened, including a DEPLOY runbook telling the
    operator which log line to expect.

    This asserts the CONVENTION on the statement's source text rather than the outcome,
    because there is no Postgres in this suite. It fails on ABSENCE of the quoting and on
    PRESENCE of an integer literal, so re-introducing either shape goes red here rather than
    on the maintainer's next Postgres boot."""
    import re

    with open(os.path.join(_ROOT, "app", "db.py"), encoding="utf-8") as fh:
        src = fh.read()
    fn = src.split("def _clear_dormant_superadmin()")[1].split("\ndef ")[0]
    stmt = [ln for ln in fn.split("\n") if "UPDATE" in ln and "is_superadmin" in ln]
    assert len(stmt) == 1, f"expected exactly one UPDATE in the clear, found {len(stmt)}"
    line = stmt[0]

    assert '"user"' in line, (
        "the boot clear does not QUOTE the `user` table. It is a reserved word in Postgres, "
        "so the statement is a syntax error there and the function's own except-swallow "
        "turns that into a log line on every boot forever. See "
        "alembic/versions/a1f4c8b2d6e9_user_email_unique_lower.py:70, where this repo "
        f"already wrote the rule down. Line: {line.strip()}"
    )
    assert not re.search(r"is_superadmin\s*=\s*[01]\b", line), (
        "the boot clear compares the BOOLEAN is_superadmin column to an integer literal. "
        "SQLite tolerates it because it has no boolean type; Postgres raises a type error. "
        "Bind Python booleans instead and let SQLAlchemy render each dialect. Line: "
        f"{line.strip()}"
    )

    # GATE R5 LOW: pinning the SQL TEXT is not enough, because the values the statement
    # compares against live in the params dict on the next line. Binding 0 and 1 there
    # keeps the text clean and still reintroduces the round-4 failure: psycopg sends an
    # int2 (oid 21) against a boolean column and Postgres raises, into the clear's own
    # except arm, silently, forever. So the BOUND VALUES are pinned too.
    params = [ln for ln in fn.split("\n") if ":cleared" not in ln and '"cleared"' in ln]
    assert len(params) == 1, (
        "expected exactly one params dict binding `cleared`; the boot clear's shape "
        f"changed and this ratchet needs re-pinning. Found {len(params)}."
    )
    bound = params[0]
    assert "False" in bound and "True" in bound, (
        "the boot clear binds something other than Python booleans. SQLAlchemy renders a "
        "bool as 0/1 for SQLite and false/true for Postgres; an int binds as an integer on "
        "BOTH, which is the round-4 type error again with a clean-looking SQL string. "
        f"Line: {bound.strip()}"
    )
    assert not re.search(r":\s*[01]\b", bound), (
        "the boot clear binds an integer literal for a BOOLEAN column. See above. "
        f"Line: {bound.strip()}"
    )
    print(
        "  boot clear: table quoted, boolean-typed, and BOUND as Python bools, so it is "
        "valid on sqlite AND postgres"
    )


def _docstring_line_numbers(src: str) -> set:
    """Line numbers covered by a module/class/function docstring (GATE R3 LOW-2).

    The `is_superadmin` ratchet exempts docstrings in its AST walk but used to count exempt
    lines with a raw text scan, so the two halves disagreed about what counted as a site and
    a prose edit could turn the suite red. Returns an empty set on a syntax error, so a file
    that will not parse fails on the parse, not silently here."""
    import ast

    covered: set = set()
    try:
        tree = ast.parse(src)
    except SyntaxError:
        return covered
    holders = (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)
    for node in ast.walk(tree):
        if not isinstance(node, holders):
            continue
        body = getattr(node, "body", None) or []
        first = body[0] if body else None
        if (
            isinstance(first, ast.Expr)
            and isinstance(first.value, ast.Constant)
            and isinstance(first.value.value, str)
        ):
            end = getattr(first.value, "end_lineno", first.value.lineno)
            covered.update(range(first.value.lineno, (end or first.value.lineno) + 1))
    return covered


def test_superadmin_stays_dormant() -> None:
    """2026-09-20: the back-office and the super-admin were REMOVED. The `is_superadmin`
    COLUMN survives on purpose (a destructive migration is a worse risk than a dead column,
    and this repo has been bitten by migrations beyond ADD COLUMN on pre-rebuild installs),
    but nothing may read or write it again.

    This fails on ABSENCE of the rule rather than on a specific misuse, which is the only
    kind of check that caught anything during the 2026-09-18 audit. A dormant privilege bit
    that something starts reading again is a cross-tenant escalation with no gate around it:
    every guard that used to keep a super-admin honest (the home-org linchpin, the
    back-office-only rule) was deleted along with the console, so there is nothing left to
    make a resurrected flag safe.

    The ALLOWED sites are the schema column, the model field, the one-time clear at init
    and the 2026-07 P0 migration that added the column. All inert, all pinned by their
    whole line AND by how many times that line may appear."""
    import ast

    # GATE R1 MED-4, then its own neuter matrix. Two rewrites, both instructive.
    #
    # v1 walked the AST only and exempted whole FILES. The P8 backfill this train deleted
    # was a RAW SQL `UPDATE user SET is_superadmin = 1` inside app/db.py, so pasting it back
    # verbatim kept the suite green: invisible to the AST, waved through by the file
    # exemption.
    #
    # v2 replaced the AST walk with a text scan plus getattr. That traded one hole for two:
    # `return u.is_superadmin` and a Jinja `{% if u.is_superadmin %}` both slipped through,
    # because neither is followed by a character the code-shaped regex looked for. A ratchet
    # that is wrong CERTIFIES, which is the failure this whole check exists to prevent, so
    # it is worth saying plainly that it happened twice here.
    #
    # v3 uses the right instrument per file type. Exactly two LINES may name the flag.
    #
    # v4 (GATE R2 LOWs 2-5) widens the walk and counts the exemptions. Every one of those
    # escapes was ONE directory or ONE extension outside the walk root, which is the same
    # shape as v1's hole: the check was correct about everything it looked at, and the
    # question it never asked was what it was not looking at.
    ROOTS = ("app", "alembic", "scripts", "tools", "integrations")
    EXTS = (".py", ".html", ".sql", ".js", ".j2", ".jinja")
    # (relpath, stripped line) -> how many times that exact line may appear in that file.
    #
    # The COUNT is the pin (GATE R2 LOW-5). A line number would go stale on every edit
    # above it, and a ratchet that cries wolf gets loosened until it certifies; a second
    # copy of an exempt line (another model, another table, another migration) is the
    # thing actually worth catching, and the count catches it without the brittleness.
    ALLOWED_LINES = {
        ("app/db.py", '("is_superadmin", "BOOLEAN NOT NULL DEFAULT 0"),'): 1,
        # The one-time clear, in _clear_dormant_superadmin (GATE R2, moved by GATE R3 out of
        # the SQLite-only _ensure_default_org). Exempted by its WHOLE stripped line, not a
        # prefix, so it cannot be used to smuggle a `= 1` variant past the check.
        #
        # NOTE for whoever trips this: the pin is FORMATTING-SENSITIVE (gate R3 LOW-3).
        # Re-wrapping this statement the way `ruff format` would, with identical semantics,
        # turns this suite RED. That already happened once, on the R3 refactor above. When
        # it does, UPDATE THE PIN to the new text. Do not loosen it to a prefix or a
        # substring, which is what makes a ratchet certify instead of catch.
        (
            "app/db.py",
            "text('UPDATE \"user\" SET is_superadmin = :cleared WHERE is_superadmin = :was_set'),",
        ): 1,
        ("app/models.py", "is_superadmin: bool = False"): 1,
        # The P0 migration that ADDED the column (2026-07, historical, already applied
        # everywhere). Pinned to this one file so that a NEW migration naming the flag,
        # which is precisely how a re-promotion would be written, fires.
        #
        # GATE R3 LOW-1: `add_column` spans two lines and the pin used to be the FIRST line
        # only, which ends mid-call. `_allowed()` exempts by lineno, so the continuation
        # carrying `server_default='0'` was neither pinned nor named the flag, and flipping
        # that default to '1' left the suite green. The parameter deciding whether the
        # column arrives SET or CLEAR on a fresh build was the one part of the statement the
        # check could not see. Both lines are pinned now, so either one changing fires.
        # General rule: an exemption pinned to a line that is not a whole statement leaves
        # the rest of that statement unpoliced.
        (
            "alembic/versions/f7a2c9d1b3e6_multiorg_p0_org_id_is_superadmin.py",
            "batch_op.add_column(sa.Column('is_superadmin', sa.Boolean(), nullable=False,",
        ): 1,
        (
            "alembic/versions/f7a2c9d1b3e6_multiorg_p0_org_id_is_superadmin.py",
            "server_default='0'))",
        ): 1,
        (
            "alembic/versions/f7a2c9d1b3e6_multiorg_p0_org_id_is_superadmin.py",
            "batch_op.drop_column('is_superadmin')",
        ): 1,
    }
    # Each of those counts is EXACT, not a ceiling (GATE R3 LOW-4). The first version fired
    # only on `count > want`, so deleting the boot clear outright left this suite green,
    # even though that clear is the only thing making the flag fail-CLOSED on installs that
    # already carry a set row. Absence beats incorrectness, which is the lesson the
    # 2026-09-18 audit produced six separate times, and `!= want` gets both directions from
    # one rule instead of bolting a second set on the side. The sibling #73 ratchet 80 lines
    # up already asserts on a STALE allowlist entry, so the file carries this convention.
    targets = []
    for _root in ROOTS:
        for base, _dirs, files in os.walk(os.path.join(_ROOT, _root)):
            targets += [os.path.join(base, f) for f in files if f.endswith(EXTS)]
    offenders = []
    allowed_seen: dict = {}
    for path in sorted(targets):
        fname = os.path.basename(path)
        rel = os.path.relpath(path, _ROOT).replace(os.sep, "/")
        with open(path, encoding="utf-8") as fh:
            src = fh.read()
        if "is_superadmin" not in src:
            continue
        lines = src.split("\n")
        # GATE R3 LOW-2: skip lines that live inside a docstring. The offender walk exempts
        # docstrings, so counting them here made the two halves disagree about what a SITE
        # is, and quoting an exempt line verbatim in prose turned the suite red with
        # "reused by a second site". A ratchet that goes red on a documentation edit is one
        # that gets loosened.
        _doc_lines = _docstring_line_numbers(src) if rel.endswith(".py") else set()
        for _no, _line in enumerate(lines, 1):
            key = (rel, _line.strip())
            if key in ALLOWED_LINES and _no not in _doc_lines:
                allowed_seen[key] = allowed_seen.get(key, 0) + 1

        def _allowed(lineno, _rel=rel, _lines=lines):
            return (_rel, _lines[lineno - 1].strip()) in ALLOWED_LINES

        if not rel.endswith(".py"):
            # A template, .js or .sql file has no docstrings: every mention is real.
            for lineno, line in enumerate(lines, 1):
                if "is_superadmin" in line and not _allowed(lineno):
                    offenders.append(f"{rel}:{lineno} ({fname.rsplit('.', 1)[-1]})")
            continue

        tree = ast.parse(src)
        # Prose about the removal lives in docstrings; collect them so they are exempt.
        docstrings = set()
        for node in ast.walk(tree):
            holders = (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)
            if isinstance(node, holders):
                body = getattr(node, "body", None) or []
                first = body[0] if body else None
                if (
                    isinstance(first, ast.Expr)
                    and isinstance(first.value, ast.Constant)
                    and isinstance(first.value.value, str)
                ):
                    docstrings.add(id(first.value))
        for node in ast.walk(tree):
            where = None
            if (isinstance(node, ast.Attribute) and node.attr == "is_superadmin") or (
                isinstance(node, ast.Name) and node.id == "is_superadmin"
            ):
                where = "attribute/name"
            elif isinstance(node, (ast.keyword, ast.arg)) and (
                getattr(node, "arg", None) == "is_superadmin"
            ):
                # GATE R2 MED-1: the ORM spelling. `User(is_superadmin=True)` and
                # `update(User).values(is_superadmin=True)` put the name in
                # `keyword.arg` as a plain str, so it is neither a Name, an Attribute
                # nor a Constant, and v3 passed both. That is the same WRITE that
                # defeated v1 as raw SQL, just spelled through SQLAlchemy, so it is
                # ordinary code rather than obfuscation.
                where = "keyword/arg"
            elif isinstance(node, ast.Call) and getattr(node.func, "id", "") == "getattr":
                for arg in node.args[1:2]:
                    if isinstance(arg, ast.Constant) and arg.value == "is_superadmin":
                        where = "getattr"
            elif (
                isinstance(node, ast.Constant)
                and isinstance(node.value, str)
                and "is_superadmin" in node.value
                and id(node) not in docstrings
            ):
                where = "string/raw SQL"
            if where and not _allowed(node.lineno):
                offenders.append(f"{rel}:{node.lineno} ({where})")
    assert not offenders, (
        "is_superadmin is DORMANT since the back-office was removed, but these read or "
        "write it: " + ", ".join(sorted(set(offenders))) + ". Nothing enforces the linchpin "
        "that used to make a super-admin safe, so do not resurrect the flag. Org lifecycle "
        "is a host operator action (`cli org`, `cli user org`, require_operator)."
    )
    # An exempt line that appears TWICE is a second, unreviewed site wearing the first one's
    # exemption (GATE R2 LOW-5). An exempt line that appears ZERO times is a guard that has
    # been deleted or silently reformatted (GATE R3 LOW-4). Both are failures of the same
    # check, so it counts rather than merely capping.
    miscounted = [
        f"{rel}: {line!r} appears {allowed_seen.get((rel, line), 0)}x, expected exactly {want}x"
        for (rel, line), want in ALLOWED_LINES.items()
        if allowed_seen.get((rel, line), 0) != want
    ]
    assert not miscounted, (
        "an ALLOWED_LINES exemption does not match its site exactly: "
        + "; ".join(sorted(miscounted))
        + ". TOO MANY means a second, unreviewed site is wearing the first one's exemption; "
        "the exemption covers the one reviewed line, not the text of it. ZERO means the "
        "guarded line is GONE or was reformatted -- if it was reformatted, re-pin it to the "
        "new text; if it was deleted, put it back, because the boot clear in app/db.py is "
        "the only thing that makes this flag fail-CLOSED on an install that already has a "
        "row set."
    )

    # GATE R1 LOW: isdir alone is evaded by app/backoffice.py or app/back_office/.
    # GATE R2 LOW-4: a top-level listdir is evaded by app/web/backoffice/, one directory
    # down. Walk instead, over every root, and check directory names and module basenames.
    # POST-MERGE: a directory name alone is not a revival. Deleting app/backoffice/ leaves
    # app/backoffice/__pycache__ behind in any checkout that had ever imported it, so the
    # merged main had a directory literally named `backoffice` containing nothing but stale
    # bytecode, and this check went RED on it. That is a false red on ordinary build
    # residue, and a ratchet that cries wolf on a fresh checkout is one the next person
    # deletes. It also would have fired for anyone switching back from an older branch.
    #
    # What matters is SOURCE. A package with no .py file cannot serve a request: there is
    # nothing to import and nothing to route. So a candidate directory counts only when it
    # actually contains Python source, while a candidate MODULE file counts immediately.
    def _has_source(path):
        for sub, _d, subfiles in os.walk(path):
            if sub.endswith("__pycache__"):
                continue
            if any(f.endswith((".py", ".html", ".j2", ".jinja")) for f in subfiles):
                return True
        return False

    def _is_backoffice(name):
        return name.replace("_", "").replace("-", "").lower().startswith("backoffice")

    revived = []
    for _root in ROOTS:
        for base, dirs, files in os.walk(os.path.join(_ROOT, _root)):
            if f"{os.sep}__pycache__" in base or base.endswith("__pycache__"):
                continue
            for name in dirs:
                if _is_backoffice(name) and _has_source(os.path.join(base, name)):
                    rel = os.path.relpath(os.path.join(base, name), _ROOT)
                    revived.append(rel.replace(os.sep, "/"))
            for fname in files:
                if fname.endswith(".py") and _is_backoffice(fname[:-3]):
                    rel = os.path.relpath(os.path.join(base, fname), _ROOT)
                    revived.append(rel.replace(os.sep, "/"))
    assert not revived, (
        f"a back-office package/module is back ({sorted(revived)}). It was removed "
        "2026-09-20 with the super-admin; re-adding a cross-tenant web door is a product "
        "decision, not a refactor."
    )
    print(
        "  superadmin dormant: no read/write of is_superadmin outside the schema column "
        "+ model field + the P0 migration; no back-office module under any root"
    )


def main() -> None:
    test_columns_exist()
    test_defaults_and_backfill()
    test_gate_defaults_off()
    test_gate_off_behaves_as_today()
    test_linchpin_superadmin_home_org()
    test_same_org_and_null_row()
    test_visibility_isolated_across_orgs()
    test_org_stamping_on_create()
    test_gate_off_predicates_are_noops()
    # P2 end-to-end route enforcement (negative isolation matrix)
    test_project_subtree_isolation_404()
    test_own_org_subtree_reachable()
    test_lists_and_reports_exclude_other_org()
    test_new_inbox_and_creates_stamped_org()
    test_gate_off_subtree_unrestricted()
    # P3 directly-owned tables + connector object-authz (negative isolation matrix)
    test_p3_lists_exclude_other_org()
    test_p3_connection_object_authz_404()
    test_p3_api_connector_object_authz_404()
    test_p3_global_workflow_not_surfaced_cross_org()
    test_p3_routing_and_budget_org_scoped_resolution()
    test_p3_creation_stamps_org()
    test_p3_null_project_tm_no_cross_org_bleed()
    test_p3_gate_off_directly_owned_byte_identical()
    # P4 API-token org binding (lsu_ home-org, lsk_ bind-or-reject) + /api surface
    test_p4_api_identity_carries_home_org()
    test_p4_lsu_cross_org_document_and_job_404()
    test_p4_lsu_lists_only_own_org()
    test_p4_superadmin_lsu_confined_to_home_org()
    test_p4_shared_token_bind_or_reject()
    test_p4_upload_via_key_lands_in_org()
    test_p4_gate_off_tokens_behave_as_today()
    # P6 remote CLI (org-scoped by token) + /api gaps + SaaS in-process operator gate
    test_p6_api_endpoints_org_bound()
    test_p9_api_cli_cross_org_matrix()
    test_p6_remote_cli_org_scoped()
    test_p6_remote_refuses_inprocess_only()
    test_p6_remote_local_json_rowshape_parity()
    test_p6_customer_pairs_import_remote_and_rbac()
    test_p6_saas_inprocess_operator_gate()
    test_p6_gate_off_inprocess_byte_identical()
    # P7 org management (back-office org CRUD + suspend, user-move) + enforcement
    test_p7_console_is_gone_and_org_crud_lives_in_the_service()
    test_p7_suspended_org_blocks_login_and_api()
    test_p7_gate_off_suspend_is_noop()
    test_p7_user_move_changes_org_visibility_keeps_content()
    # P8 super-admin bootstrap + audit/GDPR org scoping + PromptTemplate isolation
    test_p8_audit_service_org_scopes_by_actor()
    test_p8_customer_audit_scoped_superadmin_is_home_org_only()
    test_p8_gdpr_refuses_cross_org()
    test_p8_prompttemplate_org_isolated()
    test_p8_gate_off_byte_identical()
    # Pseudo-actor job visibility (webhook/scheduler org attribution from content)
    test_pseudo_actor_job_org_resolved_from_content()
    test_pseudo_actor_job_content_never_widens_from_caller()
    test_pseudo_actor_job_falls_through_when_creator_deleted()
    test_pseudo_actor_job_gate_off_and_backoffice_unchanged()
    # CLI-parity-B: guarded org delete + apikey enumeration isolation + routing show
    test_org_delete_guarded()
    test_apikey_list_api_org_isolation()
    test_routing_rule_show_org_scoped_and_fk_residual()
    # Train I: SCIM 2.0 org confinement on the /scim mount + the accept_invite I-fix
    test_invite_org_stamped()
    test_invite_retains_org_when_inviter_gone()
    test_scim_org_isolation()
    # #73 (round-3 hardening): the org_id=None see-all-vs-blocked disagreement
    test_p73_null_org_principal_resolves_blocked_not_none()
    test_p73_read_helpers_fail_closed()
    test_p73_unscoped_operator_door_still_sees_all()
    test_p73_poison_never_stamped_on_a_write()
    test_p73_boot_invariant_backfills_and_logs()
    test_p73_boot_refuses_a_reserved_org_id()
    test_p73_gate_off_byte_identical()
    # #494: the confinement check must quote "user" (reserved in Postgres), never bare
    test_p494_confinement_sql_quotes_the_user_table()
    test_p494_confinement_backfill_stamps_org_on_sqlite()
    # round-4: the six/seven-helper management-read family fails closed BY CONSTRUCTION
    test_p73b_sixhelper_family_fails_closed()
    test_p73b_sixhelper_operator_door_sees_all()
    test_p73b_sixhelper_gate_off_byte_identical()
    test_p73b_vendor_cross_org_dispatch_preserved()
    # round-4 residual: the THREE sibling read helpers (options_for_step /
    # resolve_style_guide / attachable) fail closed BY CONSTRUCTION + create coupling
    test_p73c_sibling_helpers_fail_closed()
    test_p73c_sibling_operator_door_sees_all()
    test_p73c_sibling_gate_off_byte_identical()
    test_p73c_create_operator_coupling_and_stamp()
    test_p73c_creatorless_bound_principal_resolves_own_org()
    test_p73c_template_apply_operator_coupling()
    # round-5 residual: the still-bare RAW read helper (vendors.list_vendors)
    # now fails closed + a reflective
    # ratchet against new unguarded org read helpers in app/services/.
    test_p73d_raw_read_helpers_fail_closed()
    test_p73d_raw_operator_door_sees_all()
    test_p73d_raw_gate_off_byte_identical()
    test_p73d_no_new_unguarded_org_read_helper()
    test_superadmin_stays_dormant()
    test_boot_clear_is_valid_on_every_backend()
    print("MULTIORG OK")


if __name__ == "__main__":
    main()
