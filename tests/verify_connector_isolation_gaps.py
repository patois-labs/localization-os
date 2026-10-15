"""Connector customer-isolation gaps (task_bcae962f) — regression suite.

Two PRE-EXISTING gaps (found in the Content Types phase-3 review, NOT introduced by
it) are closed here and pinned so they can never silently reopen:

  GAP 1 (attach / caller-binding): the connector customer_id validation checked
    same-ORG but not same-CALLER-BINDING, so a customer-BOUND caller could scope /
    re-scope a connection to a DIFFERENT customer in the same org. Fixed at BOTH the
    web helper (app.web.connectors._valid_customer_id) AND the authoritative store
    seam (store.create/update via bound_customer_id -> _confine_customer_id), so web,
    CLI and /api inherit the confinement uniformly.

  GAP 2 (enumerate / list): GET /api/connectors applied only the org filter, so a
    customer-bound per-user API key could enumerate connections outside its customer
    binding. Fixed by threading deps._ident_customer_id into list_connections, exactly
    as the web connector list already does.

  GAP 3 (act — coordinator-accepted follow-up, same family): the shared object-authz
    predicate api._conn_in_org checked ORG only, so every /api/connectors/{cid}/*
    ACTION route (pull/push/sync/retry, the per-kind translate/webhook/resolve-link/
    rediscover verbs) let a bound key act on a foreign customer's connection. Fixed in
    the ONE predicate, mirroring the web _conn_or_404 customer ring exactly.

Everything is offline + hermetic (throwaway LOC_TMS_DATA_DIR, provider keys blanked
-> MockProvider; connections are created with store.create, which does NO network I/O;
the browse/oauth probes stub the connector build so nothing hits a network). "caller
binding" == access.current_customer_id for a web session User / deps._ident_customer_id
for a per-user API key; BOTH gate on auth_required(), so under auth OFF the binding is
inert (unbound identities / the default install) and /api matches the web session (F1).
The local in-process CLI is a trusted, NON-bound operator (cmd_template.py precedent)
and is deliberately unaffected.

Coverage (each gap/finding test is written to FAIL against the pre-fix code):
  (a) a customer-bound caller cannot pass another customer's id through
      _valid_customer_id even same-org (web helper), and the store seam confines it too;
  (b) a customer-bound /api caller listing connectors sees ONLY its binding's
      connections (own + global), never a foreign customer's;
  (c) org-scoped behaviour for UNBOUND callers is UNCHANGED (no over-restriction) on
      both the web helper, the store seam, and the /api list;
  (d) cross-org behaviour stays fail-closed (gate ON): the customer confinement never
      widens the org boundary, and a bound caller can never seam-plant a foreign-org
      customer;
  (e) GAP 3: /api/connectors/{cid}/* action routes honour the binding (foreign -> 404);
adversarial-review follow-ups:
  (f) END-TO-END web routes: a bound SESSION's connector CREATE is force-to-own'd and
      its UPDATE PRESERVES an existing connection's scope (MED-3, no global capture) —
      asserted on the PERSISTED customer_id, pinning the web layer end to end;
  (g) HIGH-1: the web browse pickers (containers/fields) 404 a foreign customer's
      connection (they now route through the shared _conn_or_404 customer ring);
  (h) HIGH-2: connector_oauth_start's reconnect lookup will NOT reuse/mutate a foreign
      customer's connection (its credentials can't be overwritten via a forged cid);
  (i) F1: under auth OFF a bound user's lsu_ key is NOT customer-confined — /api list
      is unfiltered and matches the web session;
  (j) PERMANENT ROUTE SWEEP: enumerate EVERY connector-by-id route from the live app
      router (self-maintaining — a new route is covered automatically) and assert each
      404s a bound caller's FOREIGN cid before any network I/O. Guards the miss-a-route
      defect class that produced both HIGH findings; un-gateable routes escape only via
      the named _SWEEP_ALLOWLIST (currently just the HMAC webhook intake).
  (k) the same self-discovering sweep over the workflow family (foreign -> 404);
  (l) SCOPE-BEFORE-VALIDATE: GET /api/workflows/{wid}/plan and POST
      /api/workflow-groups/{gid}/add 404 a cross-org / foreign-customer / missing id
      BEFORE their query/body validation, while in-scope callers keep the 400s.

Final line: "CONNECTOR ISOLATION GAPS OK".
"""

import os
import re
import sys
import time

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(_ROOT)
sys.path.insert(0, _ROOT)
from tests._bootstrap import init_env
from tests._piece_patch import set_pieces

init_env("loctms_iso_gaps_")

from fastapi.testclient import TestClient
from sqlmodel import Session, select

from app.db import engine, init_db
from app.main import app
from app.models import (
    Connection,
    Customer,
    Organization,
    PromptTemplate,
    Role,
    Scope,
    User,
    Workflow,
    WorkflowGroup,
)
from app.services import apikeys, security, settings
from app.services.connectors import store
from app.web.connectors import _valid_customer_id
from tests._csrf import csrf_for


# --- fake request whose session_user resolves to a chosen User -----------------
class _Req:
    """The minimal shape security.session_user(request) reads: a `.session` dict.
    uid=None models an anonymous/no-session request.

    #138: a session must also carry `se`, the generation it was minted under, or
    `session_user` refuses it exactly as it refuses a cookie minted before that change --
    which is CORRECT product behaviour, and would otherwise make every bound caller here
    read as anonymous (binding None), so the confinement under test never ran and the
    suite reported a tenancy leak that does not exist. Stamp it the way `security.login`
    does, from the user's CURRENT epoch."""

    def __init__(self, uid=None):
        if uid is None:
            self.session = {}
            return
        with Session(engine) as s:
            u = s.get(User, uid)
            epoch = int(getattr(u, "session_epoch", 0) or 0) if u else 0
        # #247: session_user also enforces the idle/absolute limits, and a session with no
        # `iat` has no absolute time left, so it reads as signed out. Stamp `la`/`iat` the
        # way security.login does, or every bound caller silently resolves to None again.
        now = int(time.time())
        self.session = {"uid": uid, "se": epoch, "la": now, "iat": now}


# --- seeding helpers -----------------------------------------------------------
def _customer(name, org_id=None):
    with Session(engine) as s:
        c = Customer(name=name, org_id=org_id)
        s.add(c)
        s.commit()
        s.refresh(c)
        return c.id


def _org(name):
    with Session(engine) as s:
        o = Organization(name=name, active=True)
        s.add(o)
        s.commit()
        s.refresh(o)
        return o.id


def _user(email, *, customer_id=None, org_id=None, role=Role.VIEWER):
    with Session(engine) as s:
        u = User(
            email=email, name=email, role=role, active=True, customer_id=customer_id, org_id=org_id
        )
        s.add(u)
        s.commit()
        s.refresh(u)
        return u.id


def _conn_customer(cid):
    """The stored customer_id of a connection, read back fresh."""
    with Session(engine) as s:
        return s.get(Connection, cid).customer_id


def _auth_on(multi_org=False):
    settings.set_many(
        {"security_auth_required": "1", "multi_org_enabled": "1" if multi_org else "0"}
    )
    settings.invalidate_cache()


def _auth_off():
    settings.set_many({"security_auth_required": "0", "multi_org_enabled": "0"})
    settings.invalidate_cache()


def _api_conn_names(token):
    """Connection names GET /api/connectors returns for a given X-Loc-Token."""
    r = TestClient(app).get("/api/connectors", headers={"X-Loc-Token": token})
    assert r.status_code == 200, f"api list expected 200, got {r.status_code} ({r.text[:160]})"
    return {c["name"] for c in r.json().get("connections", [])}


def _login(email, *, customer_id=None, role=Role.LINGUIST, password="bound-pass-123456"):
    """Create a real account (with a password) and return a logged-in TestClient
    carrying its session cookie + a valid CSRF header. Auth must already be ON. Used
    by the END-TO-END web-route pins so the routes run with a genuine bound SESSION
    (not a synthesized request) — exactly the surface the adversarial review exercised."""
    security.create_user(email, email, role, password, customer_id=customer_id)
    c = TestClient(app)
    csrf_for(c)  # mint the session CSRF token
    r = c.post(
        "/auth/login",
        data={"email": email, "password": password, "next": "/"},
        follow_redirects=False,
    )
    assert r.status_code == 303, f"login failed for {email}: {r.status_code} ({r.text[:120]})"
    csrf_for(c)  # login rotated the session -> refresh the token
    return c


# --- permanent route sweep: discovery + allowlist (see test_j) -----------------
# A connector-by-id route can escape the sweep ONLY by being named here, with a
# reason. This is the single, auditable place a route is allowed past the ring.
_SWEEP_ALLOWLIST = {
    # HMAC-scoped webhook intake: authed by the connection's per-connection webhook
    # SIGNATURE, not a bound-caller identity (an external provider can't carry our
    # session / API token). There is NO caller to confine, so the R2-3 customer ring
    # does not apply — a caller who can't sign for the connection gets 401 (== missing).
    # Signature verification runs before any per-connection work. See
    # app/web/api.py::connector_webhook_intake.
    "/webhooks/connectors/{connection_id}": "HMAC per-connection signature; no bound identity",
    # R3 credential routes: gated by the `admin` SCOPE + is_admin + remote_admin_enabled,
    # ALL of which fire before the customer ring. A bound non-admin sweep caller gets a
    # uniform 403 ("requires an admin-scoped user API key") that is identical on every R3
    # route and reveals NOTHING about this connection's existence (not a per-connection
    # oracle) -- so it is not a customer-isolation leak. The customer ring IS still
    # enforced for an admin caller with the switch on: a foreign cid is 404, proven in
    # tests/verify_api_r3_config_credentials.py::test_connector_vendor (cross-org).
    "/api/connectors/{cid}/secret": "R3 admin-scope+switch gate before the ring; ring proven in verify_api_r3",  # noqa: E501
    "/api/connectors/{cid}/oauth-secret": "R3 admin-scope+switch gate before the ring; ring proven in verify_api_r3",  # noqa: E501
}

# HTTP methods a real caller can drive (exclude HEAD/OPTIONS auto-added by the router).
_SWEEP_METHODS = ("GET", "POST", "PUT", "PATCH", "DELETE")


def _connector_cid_routes():
    """Every registered route that reaches a connection BY ID — its path carries {cid}
    or {connection_id} under a /connectors/ path. DISCOVERED from the LIVE app router
    (never hardcoded), so a NEW connector-by-id route is swept automatically the moment
    it is registered — the whole point of promoting this into the permanent suite."""
    from app.web._routes import walk_routes

    out = []
    for r in walk_routes(app):
        path = getattr(r, "path", "")
        methods = getattr(r, "methods", None) or set()
        if "/connectors/" in path and ("{cid}" in path or "{connection_id}" in path):
            out.append((path, frozenset(m for m in methods if m in _SWEEP_METHODS)))
    return out


# --- (a) GAP 1: caller-binding confinement (web helper + store seam) -----------
def test_a_bound_caller_cannot_attach_foreign_customer():
    _auth_on()
    try:
        cust_a = _customer("A-attach")
        cust_b = _customer("B-attach")
        bound = _user("bound-a@x.test", customer_id=cust_a)
        # Fixture guard: the probe below is only meaningful when the session resolves.
        assert getattr(security.session_user(_Req(bound)), "id", None) == bound, (
            "_Req no longer models a live session (session_user returned None)"
        )

        # --- web helper: a bound caller passing customer B (same org) is CONFINED to
        #     its OWN customer A (the core GAP-1 proof; pre-fix returned str(cust_b)).
        got = _valid_customer_id(_Req(bound), str(cust_b))
        assert got != str(cust_b), (
            "GAP 1 (web): a customer-bound caller passed a FOREIGN customer id through "
            f"_valid_customer_id (got {got!r} for foreign {cust_b!r})"
        )
        assert got == str(cust_a), (
            f"a bound caller must be confined to its OWN customer (got {got!r}, want {cust_a!r})"
        )
        # blank / no id also cannot make a bound caller's connection global.
        assert _valid_customer_id(_Req(bound), "") == str(cust_a), (
            "a bound caller with a blank customer_id must still be confined to its own"
        )

        # --- store seam: the authoritative layer web/CLI/api all inherit. A bound
        #     caller (bound_customer_id) can never plant a foreign customer.
        conn = store.create(
            "airtable", "seam-bound-attach", {}, {}, customer_id=cust_b, bound_customer_id=cust_a
        )
        assert conn.customer_id == cust_a, (
            "GAP 1 (seam): store.create planted a FOREIGN customer for a bound caller "
            f"(got {conn.customer_id!r}, want own {cust_a!r})"
        )
        # update(): a bound caller can NEVER re-scope an existing connection to a
        # foreign customer. Post-MED-3 this holds by PRESERVING the existing scope
        # (the connection was created as A above), not by force-to-own. See test_f for
        # the load-bearing MED-3 case (a bound caller can't capture a GLOBAL connection).
        store.update(
            conn.id, "seam-bound-attach", {}, None, customer_id=cust_b, bound_customer_id=cust_a
        )
        assert _conn_customer(conn.id) == cust_a, (
            "GAP 1 (seam/update): a bound caller re-scoped a connection to a foreign customer"
        )
        print("  (a) bound caller confined to own customer on web helper + store seam OK")
    finally:
        _auth_off()


# --- (b) GAP 2: a bound /api caller lists only its binding (own + global) -------
def test_b_bound_api_list_excludes_foreign_customer():
    _auth_on()
    try:
        cust_a = _customer("A-list")
        cust_b = _customer("B-list")
        # own, global, and a FOREIGN customer's connection.
        store.create("airtable", "listA-own", {}, {}, customer_id=cust_a)
        store.create("jira", "listA-global", {}, {"email": "e", "api_token": "t"})
        store.create(
            "confluence", "listB-foreign", {}, {"email": "e", "api_token": "t"}, customer_id=cust_b
        )

        bound = _user("bound-list@x.test", customer_id=cust_a)
        _row, token = apikeys.create_key(bound, "iso-list-key")
        names = _api_conn_names(token)

        assert "listA-own" in names and "listA-global" in names, (
            f"a bound caller must still see its OWN + GLOBAL connections ({names!r})"
        )
        assert "listB-foreign" not in names, (
            "GAP 2: /api/connectors leaked a FOREIGN customer's connection to a bound "
            f"caller ({names!r})"
        )
        print("  (b) bound /api caller sees own+global, never a foreign customer OK")
    finally:
        _auth_off()


# --- (c) unbound callers are UNCHANGED (no over-restriction regression) ---------
def test_c_unbound_caller_unchanged():
    _auth_on()
    try:
        cust_a = _customer("A-unbound")
        cust_b = _customer("B-unbound")
        store.create("airtable", "unb-own-a", {}, {}, customer_id=cust_a)
        store.create("jira", "unb-global", {}, {"email": "e", "api_token": "t"})
        store.create(
            "confluence", "unb-own-b", {}, {"email": "e", "api_token": "t"}, customer_id=cust_b
        )

        # web helper: an UNBOUND staff caller keeps the same-org pass-through — any
        # real same-org customer id is accepted unchanged (byte-identical to today).
        staff = _user("staff@x.test", customer_id=None, role=Role.ADMIN)
        assert _valid_customer_id(_Req(staff), str(cust_b)) == str(cust_b), (
            "an unbound staff caller must keep the same-org customer id unchanged"
        )

        # store seam: an unbound caller (the default, no bound_customer_id) leaves the
        # requested scope as-is. Uses only the pre-existing call shape so this whole
        # test is a byte-identical guard that holds BOTH pre- and post-fix (proving the
        # fix does not over-restrict the unbound path).
        c1 = store.create("airtable", "unb-seam-b", {}, {}, customer_id=cust_b)
        assert c1.customer_id == cust_b, "unbound seam create must keep the requested id"

        # /api list for an UNBOUND staff key: sees ALL of its org's connections,
        # including both customers' (the filter must not over-restrict).
        _row, token = apikeys.create_key(staff, "iso-unbound-key")
        names = _api_conn_names(token)
        assert {"unb-own-a", "unb-global", "unb-own-b"} <= names, (
            "an unbound caller must see every connection (over-restriction regression) ({!r})".format(  # noqa: E501, UP032
                names
            )
        )
        print("  (c) unbound callers unchanged on web helper, seam, and /api list OK")
    finally:
        _auth_off()


# --- (d) cross-org stays fail-closed (gate ON) ---------------------------------
def test_d_cross_org_fail_closed():
    _auth_on(multi_org=True)
    try:
        org_a = _org("Iso Org A")
        org_b = _org("Iso Org B")
        a_d = _customer("A-xorg", org_id=org_a)  # customer in org A
        a_other = _customer("A2-xorg", org_id=org_a)  # a DIFFERENT customer in org A
        b_ob = _customer("B-xorg", org_id=org_b)  # customer in org B

        store.create("airtable", "xorg-A-own", {}, {}, org_id=org_a, customer_id=a_d)
        store.create("jira", "xorg-A-global", {}, {"email": "e", "api_token": "t"}, org_id=org_a)
        store.create("airtable", "xorg-A-other", {}, {}, org_id=org_a, customer_id=a_other)
        store.create(
            "confluence",
            "xorg-B",
            {},
            {"email": "e", "api_token": "t"},
            org_id=org_b,
            customer_id=b_ob,
        )

        bound = _user("bound-xorg@x.test", customer_id=a_d, org_id=org_a)
        _row, token = apikeys.create_key(bound, "iso-xorg-key")
        names = _api_conn_names(token)
        assert "xorg-A-own" in names and "xorg-A-global" in names, (
            f"a bound caller must see its own org+customer + org-global ({names!r})"
        )
        assert "xorg-A-other" not in names, (
            f"same-org DIFFERENT-customer connection must not leak to a bound caller ({names!r})"
        )
        assert "xorg-B" not in names, (
            f"cross-ORG connection must stay fail-closed for a bound caller ({names!r})"
        )

        # store seam: a bound caller can never plant a FOREIGN-ORG customer — confined
        # to its own customer.
        conn = store.create(
            "airtable", "xorg-seam", {}, {}, org_id=org_a, customer_id=b_ob, bound_customer_id=a_d
        )
        assert conn.customer_id == a_d, (
            "cross-org seam plant: a bound caller forced a foreign-org customer "
            f"(got {conn.customer_id!r}, want own {a_d!r})"
        )

        # web helper: an UNBOUND staff caller in org A cannot pass an org-B customer id
        # (item 6 same-org drop) -> '' (global), fail-closed.
        staff_a = _user("staff-a@x.test", customer_id=None, org_id=org_a, role=Role.ADMIN)
        assert _valid_customer_id(_Req(staff_a), str(b_ob)) == "", (
            "an unbound staff caller must not attach a cross-ORG customer id"
        )
        print("  (d) cross-org fail-closed on /api list, store seam, and web helper OK")
    finally:
        _auth_off()


# --- (e) GAP 3: /api/connectors/{cid}/* ACTION routes honour the binding --------
# The write-capable sibling of GAP 2 (coordinator-accepted into scope): the shared
# object-authz predicate api._conn_in_org checked ORG only, so a customer-bound
# per-user key could pull/push/sync/retry a DIFFERENT customer's connection in the
# same org. Fixed in the ONE predicate (mirroring the web _conn_or_404 exactly);
# every /api/connectors/{cid}/* action route funnels through it.
#
# Probes are deliberately NETWORK-FREE (no connector is ever built):
#   read  = GET  /api/connectors/{cid}/airtable/webhook  (pure config read)
#   write = POST /api/connectors/{cid}/retry {"job_id": 0} (blocked at the gate ->
#           404; PAST the gate it fails on job validation -> 400 "no failed items",
#           before any job is enqueued or any connector/network is touched).
def test_e_bound_api_actions_confined_to_binding():
    _auth_on()
    try:
        cust_a = _customer("A-act")
        cust_b = _customer("B-act")
        own = store.create("airtable", "act-own", {}, {}, customer_id=cust_a)
        foreign = store.create("airtable", "act-foreign", {}, {}, customer_id=cust_b)
        glob = store.create("airtable", "act-global", {}, {})

        # LINGUIST: customer-bindable (CUSTOMER_BINDABLE_ROLES) AND passes the
        # _ai_role_block can_tune_ai gate — so these probes exercise the CUSTOMER
        # ring, not the role ladder.
        bound = _user("bound-act@x.test", customer_id=cust_a, role=Role.LINGUIST)
        _row, token = apikeys.create_key(bound, "iso-act-key")
        api = TestClient(app)
        h = {"X-Loc-Token": token}

        # (a) FOREIGN customer's connection -> 404 on a read AND a write action.
        r = api.get(f"/api/connectors/{foreign.id}/airtable/webhook", headers=h)
        assert r.status_code == 404, (
            "GAP 3 (read): a bound key reached a FOREIGN customer's connection via "
            f"GET .../airtable/webhook (got {r.status_code})"
        )
        r = api.post(f"/api/connectors/{foreign.id}/retry", headers=h, json={"job_id": 0})
        assert r.status_code == 404, (
            "GAP 3 (write): a bound key reached a FOREIGN customer's connection via "
            f"POST .../retry (got {r.status_code})"
        )

        # (b) OWN connection still works: read is a 200; the write action gets PAST
        #     the authz gate (400 on job validation, never a 404) — and a GLOBAL
        #     (NULL-customer) connection stays reachable, matching the web predicate.
        r = api.get(f"/api/connectors/{own.id}/airtable/webhook", headers=h)
        assert r.status_code == 200, (
            f"a bound key must still read its OWN connection (got {r.status_code})"
        )
        r = api.post(f"/api/connectors/{own.id}/retry", headers=h, json={"job_id": 0})
        assert r.status_code == 400 and "retry" in r.text, (
            "a bound key's write on its OWN connection must pass the gate "
            f"(expected 400 'no failed items', got {r.status_code}: {r.text[:120]})"
        )
        r = api.get(f"/api/connectors/{glob.id}/airtable/webhook", headers=h)
        assert r.status_code == 200, (
            f"a bound key must still reach a GLOBAL connection (got {r.status_code})"
        )

        # (c) Legacy shared credentials have no connector authority in any customer.
        from app.web import deps as _deps

        lsk = {"X-Loc-Token": _deps.connector_api_token()}
        for c in (own, foreign, glob):
            r = api.get(f"/api/connectors/{c.id}/airtable/webhook", headers=lsk)
            assert r.status_code == 403, (
                f"shared token must be refused (conn {c.name} -> {r.status_code})"
            )
        # ... and an unbound STAFF per-user key too (no over-restriction).
        staff = _user("staff-act@x.test", customer_id=None, role=Role.ADMIN)
        _row, stok = apikeys.create_key(staff, "iso-act-staff")
        r = api.get(f"/api/connectors/{foreign.id}/airtable/webhook", headers={"X-Loc-Token": stok})
        assert r.status_code == 200, (
            f"unbound staff key must reach any same-org connection (got {r.status_code})"
        )
        print(
            "  (e) bound /api actions: foreign 404 (read+write), own+global pass, "
            "unbound unchanged OK"
        )
    finally:
        _auth_off()


# --- (f) END-TO-END web routes: bound CREATE force-to-own; bound UPDATE preserves ---
# Reviewer #2 proved that reverting the bound layer on the web create/update routes
# left the suite GREEN (the seam-only tests above don't exercise the routes). This
# drives the ACTUAL POST routes with a bound SESSION and asserts the PERSISTED
# customer_id — so a refactor can't silently drop the web layer. It also pins MED-3:
# a bound caller's UPDATE must never re-home an existing connection (esp. capturing a
# shared GLOBAL one into their own customer).
def test_f_web_routes_persist_bound_scope():
    _auth_on()
    try:
        cust_a = _customer("A-web")
        cust_b = _customer("B-web")
        client = _login("web-bound@x.test", customer_id=cust_a, role=Role.LINGUIST)

        # CREATE: a bound-A caller submits a FOREIGN customer_id=B; the connection must
        # persist as A (force-to-own on create), never B.
        r = client.post(
            "/connectors/airtable",
            data={"name": "web-create-A", "customer_id": str(cust_b)},
            follow_redirects=False,
        )
        assert r.status_code == 303, (
            f"create POST should redirect ({r.status_code}: {r.text[:120]})"
        )
        with Session(engine) as s:
            created = s.exec(select(Connection).where(Connection.name == "web-create-A")).first()
        assert created is not None, "web create did not persist a connection"
        assert created.customer_id == cust_a, (
            "WEB CREATE: a bound caller's connection was NOT force-to-own'd "
            f"(persisted customer_id={created.customer_id!r}, want own {cust_a!r})"
        )

        # UPDATE / MED-3: a bound-A caller saves settings on an EXISTING GLOBAL
        # connection, submitting customer_id=B. It must STAY GLOBAL (None) — never
        # captured into A (nor B). This is the load-bearing MED-3 pin.
        glob = store.create("airtable", "web-global", {}, {})  # customer NULL
        assert glob.customer_id is None
        r = client.post(
            f"/connectors/connection/{glob.id}",
            data={"name": "web-global", "customer_id": str(cust_b)},
            follow_redirects=False,
        )
        assert r.status_code == 303, f"update POST should redirect ({r.status_code})"
        assert _conn_customer(glob.id) is None, (
            "MED-3: a bound caller CAPTURED a shared GLOBAL connection on update "
            f"(customer_id now {_conn_customer(glob.id)!r}, must stay None/global)"
        )

        # UPDATE on the bound caller's OWN connection: scope likewise preserved (a bound
        # caller cannot re-scope an existing connection in EITHER direction).
        own = store.create("airtable", "web-own-A", {}, {}, customer_id=cust_a)
        r = client.post(
            f"/connectors/connection/{own.id}",
            data={"name": "web-own-A", "customer_id": str(cust_b)},
            follow_redirects=False,
        )
        assert r.status_code == 303
        assert _conn_customer(own.id) == cust_a, (
            "a bound caller re-homed its OWN connection to a foreign customer on update "
            f"(now {_conn_customer(own.id)!r})"
        )
        print(
            "  (f) web routes: bound CREATE force-to-own; bound UPDATE preserves scope "
            "(no GLOBAL capture) OK"
        )
    finally:
        _auth_off()


# --- (g) HIGH-1: web browse pickers apply the customer ring --------------------
def test_g_high1_browse_routes_customer_gated():
    _auth_on()
    try:
        cust_a = _customer("A-brw")
        cust_b = _customer("B-brw")
        own = store.create("airtable", "brw-own", {}, {}, customer_id=cust_a)
        foreign = store.create("airtable", "brw-foreign", {}, {}, customer_id=cust_b)
        client = _login("brw-bound@x.test", customer_id=cust_a, role=Role.LINGUIST)

        # FOREIGN -> 404 (blocked BEFORE any connector build; no stub needed).
        for path in (
            f"/connectors/connection/{foreign.id}/browse/containers",
            f"/connectors/connection/{foreign.id}/browse/fields",
        ):
            r = client.post(path, data={"base": "b", "table": "t"}, follow_redirects=False)
            assert r.status_code == 404, (
                f"HIGH-1: a bound caller reached a FOREIGN customer's connection via {path} "
                f"(got {r.status_code})"
            )

        # OWN -> reachable (stub the connector build so it stays network-free).
        saved_c, saved_f = store.containers, store.schema_fields
        set_pieces(
            "app.services.connectors.store",
            "containers",
            lambda cid, transport=None: [{"id": "appX", "name": "Own"}],
        )
        set_pieces(
            "app.services.connectors.store",
            "schema_fields",
            lambda cid, base, table, transport=None: ["Notes"],
        )
        try:
            r = client.post(
                f"/connectors/connection/{own.id}/browse/containers", follow_redirects=False
            )
            assert r.status_code == 200 and r.json()["ok"] is True, (
                f"a bound caller must still browse its OWN connection (got {r.status_code})"
            )
            r = client.post(
                f"/connectors/connection/{own.id}/browse/fields",
                data={"base": "appX", "table": "Own"},
                follow_redirects=False,
            )
            assert r.status_code == 200 and r.json()["fields"] == ["Notes"], r.text
        finally:
            set_pieces("app.services.connectors.store", "containers", saved_c)
            set_pieces("app.services.connectors.store", "schema_fields", saved_f)
        print("  (g) HIGH-1 web browse pickers: foreign 404, own reachable OK")
    finally:
        _auth_off()


# --- (h) HIGH-2: oauth-reconnect lookup applies the customer ring --------------
def test_h_high2_oauth_reconnect_no_foreign_mutation():
    _auth_on()
    try:
        # Enable the OAuth option for airtable (PKCE: a client_id is enough).
        settings.set_many({"oauth_airtable_client_id": "cli_test_id"})
        settings.invalidate_cache()
        cust_a = _customer("A-oauth")
        cust_b = _customer("B-oauth")
        foreign = store.create(
            "airtable",
            "oauth-foreign-B",
            {"base_url": "https://orig.example"},
            {},
            customer_id=cust_b,
        )
        before = (store.get(foreign.id).config or {}).get("base_url")
        client = _login("oauth-bound@x.test", customer_id=cust_a, role=Role.LINGUIST)

        with Session(engine) as s:
            n_before = len(s.exec(select(Connection)).all())
        # Bound-A drives reconnect naming the foreign-B cid + a NEW base_url. The
        # foreign connection must be untouched (its config never patch_config'd, so the
        # OAuth callback can't overwrite B's credentials); a NEW row is created instead.
        client.post(
            "/connectors/airtable/oauth/start",
            data={
                "cid": str(foreign.id),
                "base_url": "https://attacker.example",
                "name": "reconnect-attempt",
            },
            follow_redirects=False,
        )
        after = (store.get(foreign.id).config or {}).get("base_url")
        assert after == before, (
            "HIGH-2: a bound caller MUTATED a foreign customer's connection via oauth "
            f"reconnect (base_url {before!r} -> {after!r})"
        )
        with Session(engine) as s:
            all_rows = s.exec(select(Connection)).all()
            new_rows = [c for c in all_rows if c.id != foreign.id and c.name == "reconnect-attempt"]
        assert len(all_rows) == n_before + 1, (
            "oauth reconnect on a foreign cid must fall through to CREATE a new row, "
            f"never reuse the foreign one (rows {n_before} -> {len(all_rows)})"
        )
        assert _conn_customer(foreign.id) == cust_b, "foreign connection was re-homed"
        # Review fold-in: the NEW row a bound-A caller creates must be force-to-own'd to
        # customer A (same create-seam idiom as connector_create), not left global.
        assert new_rows and new_rows[0].customer_id == cust_a, (
            "oauth-start CREATE-new for a bound caller must be scoped to its OWN customer "
            f"(got {new_rows[0].customer_id if new_rows else None!r}, want {cust_a!r}) - not left global"  # noqa: E501
        )
        print(
            "  (h) HIGH-2 oauth reconnect: foreign untouched; new row created + "
            "scoped to the bound caller OK"
        )
    finally:
        settings.set_many({"oauth_airtable_client_id": ""})
        settings.invalidate_cache()
        _auth_off()


# --- (i) F1: under auth OFF a bound key is NOT confined (/api matches web) -------
def test_i_auth_off_api_matches_web():
    _auth_off()  # explicit: the auth-OFF single-operator regime
    try:
        cust_a = _customer("A-authoff")
        cust_b = _customer("B-authoff")
        store.create("airtable", "ao-own-a", {}, {}, customer_id=cust_a)
        store.create("jira", "ao-global", {}, {"email": "e", "api_token": "t"})
        store.create(
            "confluence", "ao-foreign-b", {}, {"email": "e", "api_token": "t"}, customer_id=cust_b
        )
        # _user + create_key don't gate on auth; the User carries customer_id=A but under
        # auth OFF _ident_customer_id (like access.current_customer_id) resolves to None.
        bound = _user("ao-bound@x.test", customer_id=cust_a, role=Role.LINGUIST)
        _row, token = apikeys.create_key(bound, "iso-ao-key")
        names = _api_conn_names(token)
        assert "ao-foreign-b" in names, (
            "F1: under auth OFF the /api list OVER-restricted a bound user's key "
            "(foreign connection hidden) - it must match the web session, which is "
            f"unfiltered (SYSTEM operator) ({names!r})"
        )
        assert {"ao-own-a", "ao-global", "ao-foreign-b"} <= names, (
            f"auth-OFF /api list must be unfiltered ({names!r})"
        )
        print("  (i) F1 auth-OFF: bound key's /api list is unfiltered, matches web OK")
    finally:
        _auth_off()


# --- (j) PERMANENT ROUTE SWEEP: every connector {cid} route honours the ring -----
# Promotes the re-review's route enumeration into the suite so a FUTURE connector
# route that forgets _conn_or_404 / _conn_in_org is auto-caught — the miss-a-route
# defect class behind BOTH HIGH findings this wave. It DISCOVERS routes from the live
# app router (self-maintaining), drives each with a bound-A LINGUIST (real logged-in
# session for web + a bound-A lsu_ key for /api, so role gates PASS and the CUSTOMER
# ring is what's exercised) against a FOREIGN customer-B connection, and asserts every
# route/method returns 404 BEFORE any network I/O (the ring is the first object gate,
# so a foreign cid 404s before any connector is built). Own connection reachable;
# neither list surface leaks the foreign row. Un-gateable routes escape ONLY via the
# named _SWEEP_ALLOWLIST above.
def test_j_sweep_all_connector_cid_routes():
    _auth_on()
    try:
        cust_a = _customer("A-sweep")
        cust_b = _customer("B-sweep")
        own = store.create("airtable", "sweep-own-A", {}, {}, customer_id=cust_a)
        foreign = store.create("airtable", "sweep-foreign-B", {}, {}, customer_id=cust_b)

        web = _login("sweep-web@x.test", customer_id=cust_a, role=Role.LINGUIST)
        key_user = _user("sweep-api@x.test", customer_id=cust_a, role=Role.LINGUIST)
        _row, token = apikeys.create_key(key_user, "sweep-key")
        api = TestClient(app)
        h = {"X-Loc-Token": token}

        routes = _connector_cid_routes()
        assert routes, "route enumeration returned NOTHING — discovery broke"
        discovered = {p for p, _m in routes}
        # The allowlist may only name routes that still exist (no stale escape hatch).
        stale = set(_SWEEP_ALLOWLIST) - discovered
        assert not stale, f"allowlist names route(s) that no longer exist: {sorted(stale)!r}"

        leaks, covered, swept = [], 0, set()
        for path, methods in sorted(routes):
            if path in _SWEEP_ALLOWLIST:
                continue
            is_api = path.startswith("/api/")
            client, headers = (api, h) if is_api else (web, {})
            url = path.replace("{cid}", str(foreign.id)).replace("{connection_id}", str(foreign.id))
            for method in sorted(methods):
                resp = client.request(method, url, headers=headers, follow_redirects=False)
                covered += 1
                swept.add(path)
                # A bound caller's FOREIGN cid must be indistinguishable from missing:
                # 404 (web _error_page / api JSON), BEFORE the route body / any network.
                # Anything else means the ring was skipped or the foreign action ran.
                if resp.status_code != 404:
                    leaks.append(
                        "{} {} -> {} loc={!r} body={!r}".format(
                            method,
                            path,
                            resp.status_code,
                            resp.headers.get("location", ""),
                            resp.text[:80],
                        )
                    )
        assert not leaks, (
            "SWEEP: connector {cid} route(s) did NOT 404 a bound caller's FOREIGN cid "
            "(customer ring missing or bypassed):\n  " + "\n  ".join(leaks)
        )
        assert covered > 0, "swept 0 route/method pairs — enumeration/filter broke"

        # Sanity: the OWN connection is reachable PAST the ring (network-free read), so
        # the ring isn't just blanket-denying everything.
        r = api.get(f"/api/connectors/{own.id}/airtable/webhook", headers=h)
        assert r.status_code == 200, f"own connection must pass the ring (got {r.status_code})"
        # Neither list surface leaks B.
        api_names = {
            c["name"] for c in api.get("/api/connectors", headers=h).json().get("connections", [])
        }
        assert "sweep-own-A" in api_names and "sweep-foreign-B" not in api_names, (
            f"/api list leaked foreign B or hid own A: {api_names!r}"
        )

        print(
            "  (j) route sweep: %d connector-by-id routes discovered, %d swept x methods "  # noqa: UP031
            "= %d route/method pairs -> foreign cid 404 on ALL; %d allowlisted (%s); "
            "own reachable; lists clean OK"
            % (
                len(discovered),
                len(swept),
                covered,
                len(_SWEEP_ALLOWLIST),
                ", ".join(sorted(_SWEEP_ALLOWLIST)) or "none",
            )
        )
    finally:
        _auth_off()


# --- (k) WORKFLOW-FAMILY ROUTE SWEEP (adversarial review, FIX 1 follow-in) -------
# The connector sweep (test_j) enumerates /connectors/{cid} routes ONLY, which is
# EXACTLY why the /api workflow-family customer-ring gap slipped this suite. This sweep
# extends the same self-discovering pattern to the WORKFLOW FAMILY — the web routes
# /workflows/{id}, /workflow-groups/{id}, /templates/prompts/{id} AND their /api
# equivalents /api/workflows, /api/workflow-groups, /api/prompt-templates — asserting a
# bound caller's FOREIGN customer resource is 404 on EVERY id-bearing route/method.
# DISCOVERED from the live router (self-maintaining): a NEW workflow-family route is
# covered the moment it registers. Scoped to the workflow family only (this branch owns
# those routes); other families are swept by their own suites.
_WF_FAMILY_PREFIXES = (
    "/workflows/",
    "/workflow-groups/",
    "/templates/prompts/",
    "/api/workflows/",
    "/api/workflow-groups/",
    "/api/prompt-templates/",
)

# A workflow-family id route escapes the ring sweep ONLY by being named here, with a
# reason. Matching is by EXACT route path, so an entry can never cover an id route. Every
# id-bearing workflow-family route funnels through _wf_or_404 / _grp_or_404 /
# _prompt_template_or_404 (web) or the /api _*_in_org helpers; the only entries are the
# org-level head routes below, whose {key} placeholder is a head name, not a row id.
_WF_SWEEP_ALLOWLIST: dict = {
    # ORG-level system prompt head writes (#356 phase 1): the {key} is a head name, not a
    # workflow/group/template id, and there is no customer-owned row to hide, so the sweep's
    # "foreign id" substitution is meaningless here. They stay admin-only (403 for a linguist);
    # the per-workflow routes (/workflows/{workflow_id}/system-prompts/..., /api/workflows/
    # {wid}/system-prompts/...) are NOT allowlisted and are swept like every other id route.
    "/workflows/building-blocks/system-prompts/{key}/save": "org-level head, no row id",
    "/workflows/building-blocks/system-prompts/{key}/reset": "org-level head, no row id",
    "/workflows/building-blocks/system-prompts/{key}/restore": "org-level head, no row id",
}


def _workflow_family_id_routes():
    """Every registered workflow-family route reaching a row BY ID (path starts with a
    family prefix AND carries a path param), from the LIVE app router — never hardcoded."""
    from app.web._routes import walk_routes

    out = []
    for r in walk_routes(app):
        path = getattr(r, "path", "")
        methods = getattr(r, "methods", None) or set()
        if "{" not in path:
            continue
        if any(path.startswith(p) for p in _WF_FAMILY_PREFIXES):
            out.append((path, frozenset(m for m in methods if m in _SWEEP_METHODS)))
    return out


def _wf_family_kind(path):
    """Which foreign row a family path's LEADING id addresses: group / template / wf."""
    if "/workflow-groups/" in path:
        return "group"
    if "prompt" in path:  # /templates/prompts/ + /api/prompt-templates/
        return "template"
    return "workflow"


def _fill_family_path(path, foreign_id):
    """Leading path param -> the FOREIGN family id; any further param -> a dummy. The
    ring resolves the family row FIRST, so a foreign leading id 404s before any
    sub-param is consulted."""
    first = re.sub(r"\{[^}]+\}", str(foreign_id), path, count=1)
    return re.sub(r"\{[^}]+\}", "1", first)


def test_k_sweep_workflow_family_routes():
    _auth_on()
    try:
        cust_a = _customer("A-wfsweep")
        cust_b = _customer("B-wfsweep")
        with Session(engine) as s:
            wf_b = Workflow(name="wfsweep-foreign", customer_id=cust_b)
            g_b = WorkflowGroup(name="grpsweep-foreign", customer_id=cust_b)
            t_b = PromptTemplate(
                name="ptsweep-foreign", scope=Scope.CUSTOMER, customer_id=cust_b, body="x {text}"
            )
            s.add(wf_b)
            s.add(g_b)
            s.add(t_b)
            s.commit()
            s.refresh(wf_b)
            s.refresh(g_b)
            s.refresh(t_b)
            foreign = {"workflow": wf_b.id, "group": g_b.id, "template": t_b.id}

        # bound-A LINGUIST: customer-bindable AND passes the family can_tune_ai/can_write
        # gates, so these probes exercise the CUSTOMER ring, not the role ladder. Delete
        # routes are admin-gated, but the ring resolves BEFORE that gate, so a bound
        # caller still gets 404 (never a 403 that leaks existence) — pinning the reorder.
        web = _login("wfsweep-web@x.test", customer_id=cust_a, role=Role.LINGUIST)
        key_user = _user("wfsweep-api@x.test", customer_id=cust_a, role=Role.LINGUIST)
        # docs-scoped per-user key: the /api workflow-family routes gate on _gate ->
        # _require_doc_scope (an "api"-only key is 403), same as the CLI's lsu_ key.
        _row, token = apikeys.create_key(key_user, "wfsweep-key", scopes="api docs")
        api = TestClient(app)
        h = {"X-Loc-Token": token}
        # A benign form payload so the web routes with a REQUIRED Form field (group
        # add / set-default: workflow_id; author: description) parse PAST FastAPI's
        # dependency validation and REACH the ring — otherwise a 422 (pre-body,
        # non-enumerable) would mask the 404 we want to assert. Extra keys are ignored
        # by the routes that don't declare them.
        web_form = {"workflow_id": "1", "description": "x"}

        routes = _workflow_family_id_routes()
        assert routes, "workflow-family route enumeration returned NOTHING — discovery broke"
        discovered = {p for p, _m in routes}
        stale = set(_WF_SWEEP_ALLOWLIST) - discovered
        assert not stale, f"allowlist names route(s) that no longer exist: {sorted(stale)!r}"

        leaks, covered, swept = [], 0, set()
        for path, methods in sorted(routes):
            if path in _WF_SWEEP_ALLOWLIST:
                continue
            is_api = path.startswith("/api/")
            client, headers = (api, h) if is_api else (web, {})
            url = _fill_family_path(path, foreign[_wf_family_kind(path)])
            for method in sorted(methods):
                data = None if (is_api or method == "GET") else web_form
                resp = client.request(
                    method, url, headers=headers, data=data, follow_redirects=False
                )
                covered += 1
                swept.add(path)
                # A bound caller's FOREIGN customer resource must be indistinguishable
                # from missing: 404, BEFORE the route body / any mutation.
                if resp.status_code != 404:
                    leaks.append(
                        "{} {} -> {} loc={!r} body={!r}".format(
                            method,
                            path,
                            resp.status_code,
                            resp.headers.get("location", ""),
                            resp.text[:80],
                        )
                    )
        assert not leaks, (
            "WF-FAMILY SWEEP: route(s) did NOT 404 a bound caller's FOREIGN customer "
            "resource (customer ring missing or bypassed):\n  " + "\n  ".join(leaks)
        )
        assert covered > 0, "swept 0 route/method pairs — enumeration/filter broke"

        # Sanity: the bound caller's OWN workflow is reachable PAST the ring, so the ring
        # isn't blanket-denying everything.
        with Session(engine) as s:
            own = Workflow(name="wfsweep-own", customer_id=cust_a)
            s.add(own)
            s.commit()
            s.refresh(own)
            own_id = own.id
        r = api.get(f"/api/workflows/{own_id}", headers=h)
        assert r.status_code == 200, f"own workflow must pass the ring (got {r.status_code})"

        print(
            "  (k) workflow-family sweep: %d id-bearing routes discovered, %d swept x "  # noqa: UP031
            "methods = %d route/method pairs -> foreign customer 404 on ALL; %d "
            "allowlisted; own reachable OK"
            % (len(discovered), len(swept), covered, len(_WF_SWEEP_ALLOWLIST))
        )
    finally:
        _auth_off()


# --- (l) SCOPE-BEFORE-VALIDATE on /api workflow routes with query/body validation ---
# test_k sweeps with EMPTY bodies, so a handler that validates its input BEFORE the
# org/customer ring showed up there only by accident (GET /api/workflows/{wid}/plan ->
# 400 "doc_id is required" for a foreign wid). This pins the ordering explicitly with
# DELIBERATELY INVALID input: a foreign id (cross-ORG and same-org foreign CUSTOMER)
# must 404 exactly like a missing id, while an in-scope caller still gets the 400.
def _l_probe(api, h, own, target, *, expect_valid_400):
    """Drive the two validating handlers against `target` ids with invalid input.
    Returns [(label, status, body)] for every probe that is not the expected status."""
    bad = []
    wf_id, grp_id, own_wf = target["workflow"], target["group"], own["workflow"]
    probes = [
        ("plan no doc_id", "GET", f"/api/workflows/{wf_id}/plan", None, "doc_id is required"),
        (
            "plan no locale",
            "GET",
            f"/api/workflows/{wf_id}/plan?doc_id=1",
            None,
            "locale is required",
        ),
        (
            "plan non-int doc_id",
            "GET",
            f"/api/workflows/{wf_id}/plan?doc_id=abc&locale=fr-FR",
            None,
            "positive integer",
        ),
        (
            "group add bad pair",
            "POST",
            f"/api/workflow-groups/{grp_id}/add",
            {"workflow_id": own_wf, "pair": "not-a-pair"},
            "invalid pair",
        ),
    ]
    if not expect_valid_400:
        # own group + FOREIGN workflow with a bad pair: the workflow ring must also
        # resolve before the pair parse.
        probes.append(
            (
                "group add foreign wf bad pair",
                "POST",
                f"/api/workflow-groups/{own['group']}/add",
                {"workflow_id": wf_id, "pair": "not-a-pair"},
                "invalid pair",
            )
        )
    for label, method, url, body, err in probes:
        r = api.request(method, url, headers=h, json=body)
        if expect_valid_400:
            ok = r.status_code == 400 and err in r.text
        else:
            ok = r.status_code == 404 and err not in r.text
        if not ok:
            bad.append((label, r.status_code, r.text[:120]))
    return bad


def _l_seed(org_id, customer_id, tag):
    with Session(engine) as s:
        wf = Workflow(name=f"l-wf-{tag}", org_id=org_id, customer_id=customer_id)
        grp = WorkflowGroup(name=f"l-grp-{tag}", org_id=org_id, customer_id=customer_id)
        s.add(wf)
        s.add(grp)
        s.commit()
        s.refresh(wf)
        s.refresh(grp)
        return {"workflow": wf.id, "group": grp.id}


def test_l_scope_before_validate_workflow_api():
    # Cross-ORG: an unbound org-A caller probing org-B ids.
    _auth_on(multi_org=True)
    try:
        org_a = _org("L Org A")
        org_b = _org("L Org B")
        own = _l_seed(org_a, None, "a")
        foreign = _l_seed(org_b, None, "b")
        missing = {"workflow": 9_999_901, "group": 9_999_902}
        caller = _user("l-xorg@x.test", org_id=org_a, role=Role.LINGUIST)
        _row, token = apikeys.create_key(caller, "l-xorg-key", scopes="api docs")
        api, h = TestClient(app), {"X-Loc-Token": token}
        for name, target in (("cross-org", foreign), ("missing", missing)):
            bad = _l_probe(api, h, own, target, expect_valid_400=False)
            assert not bad, f"(l) {name} id must 404 before validation: {bad!r}"
        bad = _l_probe(api, h, own, own, expect_valid_400=True)
        assert not bad, f"(l) in-org validation errors must stay 400: {bad!r}"
    finally:
        _auth_off()

    # Same-org foreign CUSTOMER: a customer-bound caller probing another customer's ids.
    _auth_on()
    try:
        cust_a = _customer("A-lscope")
        cust_b = _customer("B-lscope")
        own = _l_seed(None, cust_a, "ca")
        foreign = _l_seed(None, cust_b, "cb")
        caller = _user("l-bound@x.test", customer_id=cust_a, role=Role.LINGUIST)
        _row, token = apikeys.create_key(caller, "l-bound-key", scopes="api docs")
        api, h = TestClient(app), {"X-Loc-Token": token}
        bad = _l_probe(api, h, own, foreign, expect_valid_400=False)
        assert not bad, f"(l) foreign-customer id must 404 before validation: {bad!r}"
        bad = _l_probe(api, h, own, own, expect_valid_400=True)
        assert not bad, f"(l) bound caller's own validation errors must stay 400: {bad!r}"
    finally:
        _auth_off()
    print(
        "  (l) scope-before-validate: /plan + /workflow-groups/{gid}/add 404 foreign "
        "(cross-org + foreign customer) and missing ids before validation; in-scope 400s kept"
    )


# --- (m) RING BEFORE ROLE on the per-workflow system-prompt writes (#356 2b) ---------
# The admin-only head writes used to answer 403 to a customer-bound linguist BEFORE the
# customer ring, so a FOREIGN workflow id answered 403 while a MISSING id answered 403 too
# only by accident of the gate order; the ring check must run first (404 for foreign and
# missing, identical), and IN the ring the admin-only rule still refuses (403).
def test_m_system_prompt_writes_ring_before_role():
    _auth_on()
    try:
        cust_a = _customer("A-spring")
        cust_b = _customer("B-spring")
        with Session(engine) as s:
            wf_b = Workflow(name="spring-foreign", customer_id=cust_b)
            wf_a = Workflow(name="spring-own", customer_id=cust_a)
            s.add(wf_b)
            s.add(wf_a)
            s.commit()
            s.refresh(wf_b)
            s.refresh(wf_a)
            foreign_id, own_id = wf_b.id, wf_a.id
        missing_id = max(foreign_id, own_id) + 9000
        web = _login("spring-web@x.test", customer_id=cust_a, role=Role.LINGUIST)
        key_user = _user("spring-api@x.test", customer_id=cust_a, role=Role.LINGUIST)
        _row, token = apikeys.create_key(key_user, "spring-key", scopes="api docs")
        api = TestClient(app)
        h = {"X-Loc-Token": token}
        head = "step_head"
        calls = [
            ("PUT", "/api/workflows/{w}/system-prompts/" + head, {"json": {"body": "x"}}, True),
            ("DELETE", "/api/workflows/{w}/system-prompts/" + head, {}, True),
            (
                "POST",
                "/api/workflows/{w}/system-prompts/" + head + "/restore",
                {"json": {"version": 1}},
                True,
            ),
            (
                "POST",
                "/workflows/{w}/system-prompts/" + head + "/save",
                {"data": {"body": "x"}},
                False,
            ),
            ("POST", "/workflows/{w}/system-prompts/" + head + "/reset", {}, False),
            (
                "POST",
                "/workflows/{w}/system-prompts/" + head + "/restore",
                {"data": {"version": "1"}},
                False,
            ),
        ]
        for method, tpl, kw, is_api in calls:
            client, headers = (api, h) if is_api else (web, {})
            codes, bodies = {}, {}
            for label, wid in (("foreign", foreign_id), ("missing", missing_id), ("own", own_id)):
                r = client.request(
                    method, tpl.format(w=wid), headers=headers, follow_redirects=False, **kw
                )
                codes[label] = r.status_code
                # the per-response CSP nonce is the only part allowed to differ
                bodies[label] = re.sub(r'nonce="[^"]+"', 'nonce=""', r.text)
            assert codes["foreign"] == 404 and codes["missing"] == 404, (method, tpl, codes)
            assert bodies["foreign"] == bodies["missing"], (
                "a foreign id must be byte-identical to a missing one",
                method,
                tpl,
            )
            assert codes["own"] == 403, (
                "in-ring linguist stays refused (admin-only)",
                method,
                tpl,
                codes,
            )
        print("  (m) system-prompt writes: foreign/missing 404 before admin 403; in-ring 403 OK")
    finally:
        _auth_off()


def test_n_oauth_callback_current_scope():
    """#378: scope changes before or during exchange cannot replace credentials."""
    from urllib.parse import parse_qs, urlsplit

    import httpx

    from app.services.connectors import oauth

    _auth_on()
    settings.set_many({"oauth_airtable_client_id": "scope-test-client"})
    a, b = _customer("oauth-A"), _customer("oauth-B")
    own_org, other_org = _org("oauth-own-org"), _org("oauth-other-org")
    failures = []
    try:
        for scenario in (
            "before",
            "during",
            "caller",
            "caller-during",
            "org",
            "org-during",
            "caller-org",
            "caller-org-during",
            "global",
            "staff",
            "deleted",
            "ok",
        ):
            _auth_on()
            client = _login(
                f"oauth-{scenario}@x.test", customer_id=None if scenario == "staff" else a
            )
            with Session(engine) as s:
                user = s.exec(select(User).where(User.email == f"oauth-{scenario}@x.test")).one()
                if "org" in scenario:
                    user.org_id = own_org
                    s.add(user)
                    s.commit()
                org_id = user.org_id
            conn = store.create(
                "airtable",
                scenario,
                {},
                {"api_token": "original"},  # pregate: allow-secret fixture
                customer_id=a,
                org_id=org_id,
            )
            if "org" in scenario:
                _auth_on(multi_org=True)
            original = conn.secret_enc
            calls = []

            def move(scenario=scenario, conn=conn):
                if scenario.startswith("caller"):
                    with Session(engine) as s:
                        user = s.exec(
                            select(User).where(User.email == f"oauth-{scenario}@x.test")
                        ).one()
                        if "org" in scenario:
                            user.org_id = other_org
                        else:
                            user.customer_id = b
                        s.add(user)
                        s.commit()
                elif scenario.startswith("org"):
                    with Session(engine) as s:
                        row = s.get(Connection, conn.id)
                        row.org_id = other_org
                        s.add(row)
                        s.commit()
                elif scenario == "deleted":
                    with Session(engine) as s:
                        s.delete(s.get(Connection, conn.id))
                        s.commit()
                else:
                    store.update(
                        conn.id, scenario, {}, {}, customer_id="" if scenario == "global" else b
                    )

            def exchange(request, scenario=scenario, calls=calls, move=move):
                calls.append(request)
                if "during" in scenario:
                    move()
                return httpx.Response(
                    200,
                    json={"access_token": "replacement"},  # pregate: allow-secret fixture
                )

            oauth.set_default_transport(httpx.MockTransport(exchange))
            start = client.post(
                "/connectors/airtable/oauth/start", data={"cid": conn.id}, follow_redirects=False
            )
            assert "state=" in start.headers.get("location", ""), (scenario, start.headers)
            state = parse_qs(urlsplit(start.headers["location"]).query)["state"][0]
            if "during" not in scenario and scenario != "ok":
                move()
            if scenario in ("before", "caller", "deleted", "org", "caller-org"):
                assert client.get(f"/connectors/connection/{conn.id}").status_code == 404
            response = client.get(
                "/connectors/oauth/callback",
                params={"state": state, "code": "test-code"},
                follow_redirects=False,
            )
            saved = store.get(conn.id)
            if scenario == "ok":
                assert response.status_code == 303 and saved.status == "connected"
                assert store.dec_secret(saved.secret_enc)["oauth"]["access_token"] == "replacement"
            else:
                if saved and (saved.secret_enc != original or saved.status == "connected"):
                    failures.append(f"{scenario}: changed credentials/status")
                if response.status_code != 404:
                    failures.append(f"{scenario}: callback returned {response.status_code}")
                if len(calls) != (1 if "during" in scenario else 0):
                    failures.append(f"{scenario}: unexpected token exchange")
            count = len(calls)
            client.get("/connectors/oauth/callback", params={"state": state, "code": "replay"})
            assert len(calls) == count, "state must be consumed even on rejection"
        assert not failures, failures
    finally:
        oauth.set_default_transport(None)
        _auth_off()


def main():
    init_db()
    test_n_oauth_callback_current_scope()
    test_a_bound_caller_cannot_attach_foreign_customer()
    test_b_bound_api_list_excludes_foreign_customer()
    test_c_unbound_caller_unchanged()
    test_d_cross_org_fail_closed()
    test_e_bound_api_actions_confined_to_binding()
    test_f_web_routes_persist_bound_scope()
    test_g_high1_browse_routes_customer_gated()
    test_h_high2_oauth_reconnect_no_foreign_mutation()
    test_i_auth_off_api_matches_web()
    test_j_sweep_all_connector_cid_routes()
    test_k_sweep_workflow_family_routes()
    test_l_scope_before_validate_workflow_api()
    test_m_system_prompt_writes_ring_before_role()
    print("CONNECTOR ISOLATION GAPS OK")


if __name__ == "__main__":
    main()
