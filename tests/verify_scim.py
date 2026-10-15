"""SCIM 2.0 provisioning verification (Train I; offline, hermetic).

Proves the /scim/v2 server behaves per SPEC-sprint-train-I-scim-saml:

  FLAG-OFF          every /scim path 404s (SCIM error shape), even to a valid key.
  AUTH              Bearer AND X-Loc-Token both work on /scim; a tokenless request 401s
                    (WWW-Authenticate: Bearer); a key without the scim scope 403s; a
                    non-admin owner 403s; Bearer is REJECTED on /api (no widening off-mount).
  USERS             create (201) -> get -> filter (userName / externalId) -> PATCH active
                    false -> the deactivated user cannot log in -> reactivate; PUT replace.
  ADOPTION          an existing OIDC-JIT (auth_source=sso) account is ADOPTED (externalId
                    stamped, login method unchanged), not duplicated.
  LAST ADMIN        deactivating the last active admin -> 409 (guard surfaced, not bypassed).
  FILTER            an unsupported filter -> 400 invalidFilter (SCIM error shape).
  PAGINATION        the ListResponse envelope math (totalResults / startIndex / itemsPerPage).
  GROUPS            create + get + membership PATCH add/remove; a group is DIRECTORY METADATA
                    ONLY -> a group member gains NO project visibility (the zero-authz pin).
  MULTI-ORG         cross-org confinement: an org-A SCIM key never lists / gets an org-B
                    user (a cross-org id is a 404 no-oracle, never a 403).

ASCII-only output; standalone one-process-per-suite script (LOC_TMS_DATA_DIR is a
throwaway temp dir, provider keys cleared -> MockProvider, deterministic + free).

Run standalone: python tests/verify_scim.py  ->  prints "... SCIM OK".
"""

import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(_ROOT)
sys.path.insert(0, _ROOT)
from tests._bootstrap import init_env

init_env("loctms_scim_")

from fastapi.testclient import TestClient
from sqlmodel import Session, select

from app.db import engine, init_db
from app.main import app
from app.models import Organization, Project, Role, User, UserGroup, UserGroupMember
from app.services import access, apikeys, security, settings

SCHEMA_ERROR = "urn:ietf:params:scim:api:messages:2.0:Error"


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def _admin_key(email="admin@x.com", scopes="scim", org_id=None):
    u = security.create_user(email, "Admin", Role.ADMIN, "passw0rd-longenough", org_id=org_id)
    _row, raw = apikeys.create_key(u.id, "scim", scopes=scopes)
    return u, raw


def _bearer(raw):
    return {"Authorization": f"Bearer {raw}"}


def _is_scim_error(body, status):
    return (
        isinstance(body, dict)
        and body.get("schemas") == [SCHEMA_ERROR]
        and body.get("status") == str(status)
    )


# --------------------------------------------------------------------------- #
# checks
# --------------------------------------------------------------------------- #
def check_flag_off():
    init_db()
    c = TestClient(app)
    _u, raw = _admin_key("off@x.com")
    # feature default OFF -> every /scim path 404s, in SCIM error shape, even for a valid key.
    for method, path in (
        ("get", "/scim/v2/Users"),
        ("get", "/scim/v2/ServiceProviderConfig"),
        ("post", "/scim/v2/Users"),
        ("get", "/scim/v2/Groups"),
    ):
        r = getattr(c, method)(path, headers=_bearer(raw))
        assert r.status_code == 404, f"flag-off {method} {path} = {r.status_code}"
        assert _is_scim_error(r.json(), 404), f"flag-off {path} not SCIM error shape: {r.text}"
    print("  flag-off: every /scim path 404s in SCIM error shape (even a valid key)")


def check_auth():
    settings.set_many({"scim_enabled": "1"})
    c = TestClient(app)
    admin, raw = _admin_key("auth@x.com")
    # tokenless -> 401 + WWW-Authenticate: Bearer, SCIM error shape.
    r = c.get("/scim/v2/Users")
    assert r.status_code == 401 and r.headers.get("www-authenticate") == "Bearer", r.headers
    assert _is_scim_error(r.json(), 401)
    # Bearer works.
    assert c.get("/scim/v2/Users", headers=_bearer(raw)).status_code == 200
    # X-Loc-Token works too (both accepted on /scim only).
    assert c.get("/scim/v2/Users", headers={"X-Loc-Token": raw}).status_code == 200
    # a key WITHOUT the scim scope -> 403.
    _row2, raw_docs = apikeys.create_key(admin.id, "docs", scopes="docs")
    assert c.get("/scim/v2/Users", headers=_bearer(raw_docs)).status_code == 403
    # a scim key owned by a NON-admin -> 403.
    t = security.create_user("t@x.com", "T", Role.TRANSLATOR, "passw0rd-longenough")
    _row3, raw_t = apikeys.create_key(t.id, "scim", scopes="scim")
    assert c.get("/scim/v2/Users", headers=_bearer(raw_t)).status_code == 403
    # a bogus token -> 401.
    assert c.get("/scim/v2/Users", headers=_bearer("lsu_nope")).status_code == 401
    # Bearer is REJECTED on /api (no widening off the /scim mount) — /api reads only
    # X-Loc-Token, so a Bearer-only request is unauthenticated.
    assert c.get("/api/projects", headers=_bearer(raw)).status_code == 401
    print("  auth: Bearer+X-Loc-Token work on /scim; no-scope/non-admin 403; Bearer 401 on /api")


def check_user_lifecycle():
    settings.set_many({"scim_enabled": "1"})
    c = TestClient(app)
    _admin, raw = _admin_key("life@x.com")
    H = _bearer(raw)
    # create
    r = c.post(
        "/scim/v2/Users",
        headers=H,
        json={
            "userName": "Alice@X.com",
            "name": {"formatted": "Alice A"},
            "externalId": "ext-alice",
            "active": True,
        },
    )
    assert r.status_code == 201, r.text
    body = r.json()
    uid = body["id"]
    assert body["userName"] == "alice@x.com", "email must be normalized lowercase"
    assert body["externalId"] == "ext-alice"
    assert body["active"] is True
    # get
    assert c.get(f"/scim/v2/Users/{uid}", headers=H).json()["id"] == uid
    # created with the scim_default_role
    with Session(engine) as s:
        assert s.get(User, int(uid)).role == (settings.get("scim_default_role") or "translator")
        assert s.get(User, int(uid)).auth_source == "scim"
    # filter userName + externalId
    assert (
        c.get('/scim/v2/Users?filter=userName eq "alice@x.com"', headers=H).json()["totalResults"]
        == 1
    )
    assert (
        c.get('/scim/v2/Users?filter=externalId eq "ext-alice"', headers=H).json()["totalResults"]
        == 1
    )
    # give Alice a password so we can prove active=false blocks a real login.
    with Session(engine) as s:
        row = s.get(User, int(uid))
        row.password_hash = security.hash_password("alice-longpassword")
        s.add(row)
        s.commit()
    # Train sso-localpw: a hash on a SCIM (identity-provider) account never signs it in.
    assert security.authenticate("alice@x.com", "alice-longpassword") is None, (
        "a local password must not sign in a SCIM account"
    )
    # The shape that DOES sign in with a password while SCIM owns its lifecycle: a local
    # password account SCIM manages (adopted, or switched to local by an admin reset).
    with Session(engine) as s:
        row = s.get(User, int(uid))
        row.auth_source = "local"
        s.add(row)
        s.commit()
    assert security.authenticate("alice@x.com", "alice-longpassword") is not None
    # PATCH active false -> deactivated user cannot log in
    r = c.patch(
        f"/scim/v2/Users/{uid}",
        headers=H,
        json={"Operations": [{"op": "replace", "path": "active", "value": False}]},
    )
    assert r.status_code == 200 and r.json()["active"] is False
    assert security.authenticate("alice@x.com", "alice-longpassword") is None, (
        "deactivated must not log in"
    )
    # reactivate via PUT replace
    r = c.put(
        f"/scim/v2/Users/{uid}",
        headers=H,
        json={"userName": "alice@x.com", "name": {"formatted": "Alice B"}, "active": True},
    )
    assert (
        r.status_code == 200 and r.json()["active"] is True and r.json()["displayName"] == "Alice B"
    )
    assert security.authenticate("alice@x.com", "alice-longpassword") is not None, (
        "reactivated logs in"
    )
    # DELETE -> soft deactivate (row still exists)
    assert c.delete(f"/scim/v2/Users/{uid}", headers=H).status_code == 204
    with Session(engine) as s:
        row = s.get(User, int(uid))
        assert row is not None and row.active is False, "DELETE must soft-deactivate, never remove"
    print("  users: create/get/filter/PATCH-deactivate(login blocked)/PUT-reactivate/soft-DELETE")


def check_adoption():
    settings.set_many({"scim_enabled": "1"})
    c = TestClient(app)
    _admin, raw = _admin_key("adopt@x.com")
    H = _bearer(raw)
    # an existing OIDC-JIT account (auth_source=sso, no local password).
    bob = security.create_user("bob@x.com", "Bob", Role.TRANSLATOR, "", auth_source="sso")
    r = c.post(
        "/scim/v2/Users",
        headers=H,
        json={"userName": "bob@x.com", "externalId": "ext-bob", "active": True},
    )
    assert r.status_code == 200, "adoption returns 200 (matched an existing account), not 201"
    with Session(engine) as s:
        row = s.get(User, bob.id)
        assert row.external_id == "ext-bob", "externalId must be stamped on adoption"
        assert row.auth_source == "sso", "adoption must NOT change the login method (auth_source)"
    # exactly one account with that email (no duplicate created).
    with Session(engine) as s:
        n = len(s.exec(select(User).where(User.email == "bob@x.com")).all())
        assert n == 1, f"adoption duplicated the account ({n} rows)"
    print(
        "  adoption: an existing OIDC user is adopted (externalId stamped, login "
        "unchanged), not duplicated"
    )


def check_last_admin_and_filter():
    settings.set_many({"scim_enabled": "1"})
    c = TestClient(app)
    # Earlier checks left several active admins in the shared DB; deactivate them all so
    # the account we create is genuinely the LAST active admin the guard must protect.
    with Session(engine) as s:
        for other in s.exec(
            select(User).where(User.role == Role.ADMIN, User.active == True)  # noqa: E712
        ).all():
            other.active = False
            s.add(other)
        s.commit()
    admin, raw = _admin_key("solo-admin@x.com")
    H = _bearer(raw)
    # the key owner is (in this fresh install) the only active admin -> DELETE 409.
    r = c.delete(f"/scim/v2/Users/{admin.id}", headers=H)
    assert r.status_code == 409 and _is_scim_error(r.json(), 409), r.text
    # a PATCH active=false on the last admin is also refused.
    r = c.patch(
        f"/scim/v2/Users/{admin.id}",
        headers=H,
        json={"Operations": [{"op": "replace", "path": "active", "value": False}]},
    )
    assert r.status_code == 409, r.text
    # invalid filter -> 400 invalidFilter, SCIM error shape.
    r = c.get('/scim/v2/Users?filter=userName co "a"', headers=H)
    assert r.status_code == 400 and r.json().get("scimType") == "invalidFilter", r.text
    print(
        "  guards: last-admin deactivate 409 (DELETE + PATCH); unsupported filter 400 invalidFilter"
    )


def check_pagination():
    settings.set_many({"scim_enabled": "1"})
    c = TestClient(app)
    _admin, raw = _admin_key("page@x.com")
    H = _bearer(raw)
    for i in range(5):
        c.post("/scim/v2/Users", headers=H, json={"userName": f"p{i}@x.com"})
    r = c.get("/scim/v2/Users?startIndex=1&count=2", headers=H).json()
    assert r["schemas"] == ["urn:ietf:params:scim:api:messages:2.0:ListResponse"]
    assert r["startIndex"] == 1 and r["itemsPerPage"] == 2, r
    total = r["totalResults"]
    assert total >= 6, total  # 5 created + the admin
    r2 = c.get("/scim/v2/Users?startIndex=3&count=2", headers=H).json()
    assert r2["startIndex"] == 3 and r2["itemsPerPage"] == 2 and r2["totalResults"] == total
    print(
        "  pagination: 1-based startIndex + count slice the ListResponse envelope (totals stable)"
    )


def check_groups_zero_authz():
    # auth ON so project_scoping actually bites (a non-member translator is refused);
    # single-tenant otherwise (multi_org off).
    settings.set_many({"scim_enabled": "1", "project_scoping": "1", "security_auth_required": "1"})
    c = TestClient(app)
    admin, raw = _admin_key("grp@x.com")
    H = _bearer(raw)
    # a translator + a project the translator is NOT a member of (project_scoping ON).
    r = c.post("/scim/v2/Users", headers=H, json={"userName": "worker@x.com"})
    worker_id = int(r.json()["id"])
    with Session(engine) as s:
        proj = Project(name="secret-proj", created_by=admin.email)
        s.add(proj)
        s.commit()
        s.refresh(proj)
        proj_id = proj.id
        worker = s.get(User, worker_id)
        # baseline: worker cannot see the project (not a member, project_scoping on).
        before = access.can_view_project(s, worker, proj_id)
    assert before is False, "baseline: a non-member translator must not see the project"
    # create a group and add the worker via PATCH.
    rg = c.post("/scim/v2/Groups", headers=H, json={"displayName": "engineers"})
    gid = rg.json()["id"]
    r = c.patch(
        f"/scim/v2/Groups/{gid}",
        headers=H,
        json={
            "Operations": [{"op": "add", "path": "members", "value": [{"value": str(worker_id)}]}]
        },
    )
    assert r.status_code == 200 and len(r.json()["members"]) == 1, r.text
    with Session(engine) as s:
        assert (
            s.exec(select(UserGroupMember).where(UserGroupMember.group_id == int(gid))).first()
            is not None
        ), "membership row must exist"
        worker = s.get(User, worker_id)
        # THE ZERO-AUTHZ PIN: group membership grants NO project visibility, and creates
        # no ProjectMember edge — visibility is unchanged from the baseline.
        after = access.can_view_project(s, worker, proj_id)
        from app.models import ProjectMember

        pm = s.exec(select(ProjectMember).where(ProjectMember.user_id == worker_id)).all()
    assert after is False and after == before, (
        "SECURITY: group membership granted project visibility (zero-authz breached)"
    )
    assert not pm, "SECURITY: group membership created a ProjectMember edge (zero-authz breached)"
    # remove member via the Entra-style path filter.
    r = c.patch(
        f"/scim/v2/Groups/{gid}",
        headers=H,
        json={"Operations": [{"op": "remove", "path": f'members[value eq "{worker_id}"]'}]},
    )
    assert r.status_code == 200 and len(r.json()["members"]) == 0, r.text
    # DELETE the group removes it + its edges, never the users.
    assert c.delete(f"/scim/v2/Groups/{gid}", headers=H).status_code == 204
    with Session(engine) as s:
        assert s.get(UserGroup, int(gid)) is None
        assert s.get(User, worker_id) is not None, "deleting a group must never delete users"
    print(
        "  groups: create/PATCH add+remove/DELETE; a group member gains NO project "
        "visibility (zero-authz pin)"
    )


def check_multiorg_confinement():
    """Cross-org confinement: an org-A SCIM key never lists / gets an org-B user."""
    # engage the sacred gate (auth on + multi_org_enabled).
    settings.set_many(
        {"scim_enabled": "1", "security_auth_required": "1", "multi_org_enabled": "1"}
    )
    c = TestClient(app)
    with Session(engine) as s:
        oa = Organization(name="Org A")
        ob = Organization(name="Org B")
        s.add(oa)
        s.add(ob)
        s.commit()
        s.refresh(oa)
        s.refresh(ob)
        org_a, org_b = oa.id, ob.id
    _admin_a, raw_a = _admin_key("a-admin@x.com", org_id=org_a)
    H = _bearer(raw_a)
    assert access.multi_org_active(), "the sacred gate must be engaged for this check"
    # a user in ORG B (created directly, stamped org B).
    b_user = security.create_user(
        "b-user@x.com", "B User", Role.TRANSLATOR, "passw0rd-longenough", org_id=org_b
    )
    # org-A key: listing shows ONLY org A users; the org-B user is absent.
    listed = c.get("/scim/v2/Users", headers=H).json()
    emails = {r["userName"] for r in listed["Resources"]}
    assert "b-user@x.com" not in emails, "SECURITY: org-A key listed an org-B user"
    assert "a-admin@x.com" in emails
    # org-A key: GET the org-B user's id -> 404 (no-oracle, not 403).
    r = c.get(f"/scim/v2/Users/{b_user.id}", headers=H)
    assert r.status_code == 404 and _is_scim_error(r.json(), 404), r.text
    # a filter for the org-B email returns zero from org A.
    assert (
        c.get('/scim/v2/Users?filter=userName eq "b-user@x.com"', headers=H).json()["totalResults"]
        == 0
    )
    # a SCIM create in org A stamps org A (never NULL under the active gate).
    r = c.post("/scim/v2/Users", headers=H, json={"userName": "a-new@x.com"})
    assert r.status_code == 201
    with Session(engine) as s:
        assert s.get(User, int(r.json()["id"])).org_id == org_a, (
            "SCIM create must stamp the key's org"
        )
    # restore the single-tenant posture for any later run.
    settings.set_many({"security_auth_required": "0", "multi_org_enabled": "0"})
    print(
        "  multi-org: org-A key lists/creates only org A; a cross-org id is a 404 "
        "no-oracle; create stamps org A"
    )


def check_group_uniqueness():
    """The unique indexes ux_usergroup_org_name + ux_usergroupmember_group_user are
    ENFORCED on THIS (SQLite create_all) build path — proving db._ensure_indexes now
    builds them so all three build paths agree. A duplicate group name in the same org is
    a 409 (not a silent second row); a rename to a taken name is a 409 (not a 500); a
    repeated member add is idempotent (no dup row, no error)."""
    from sqlalchemy import text as _text

    settings.set_many(
        {"scim_enabled": "1", "security_auth_required": "0", "multi_org_enabled": "0"}
    )
    # The indexes physically exist on this engine (the gate's SHOULD-FIX 1a). Read
    # sqlite_master directly: SQLAlchemy's SQLite inspect().get_indexes() SKIPS functional
    # (expression) indexes like ux_usergroup_org_name's coalesce(org_id,-1), so it would
    # not see it even though it is present + enforcing.
    with engine.connect() as _conn:
        built = {
            r[0]
            for r in _conn.execute(
                _text(
                    "SELECT name FROM sqlite_master WHERE type='index' "
                    "AND tbl_name IN ('usergroup','usergroupmember')"
                )
            )
        }
    assert "ux_usergroup_org_name" in built, ("ux_usergroup_org_name not built", built)
    assert "ux_usergroupmember_group_user" in built, (
        "ux_usergroupmember_group_user not built",
        built,
    )
    c = TestClient(app)
    _admin, raw = _admin_key("uniq@x.com")
    H = _bearer(raw)
    # duplicate name in the same org -> 409 uniqueness (the index is live, not dead code).
    assert c.post("/scim/v2/Groups", headers=H, json={"displayName": "Alpha"}).status_code == 201
    dup = c.post("/scim/v2/Groups", headers=H, json={"displayName": "Alpha"})
    assert dup.status_code == 409 and dup.json().get("scimType") == "uniqueness", dup.text
    # a rename to a taken name -> 409, never a 500 (SHOULD-FIX 1b).
    rb = c.post("/scim/v2/Groups", headers=H, json={"displayName": "Beta"})
    beta_id = rb.json()["id"]
    put = c.put(f"/scim/v2/Groups/{beta_id}", headers=H, json={"displayName": "Alpha"})
    assert put.status_code == 409 and put.json().get("scimType") == "uniqueness", put.text
    pat = c.patch(
        f"/scim/v2/Groups/{beta_id}",
        headers=H,
        json={"Operations": [{"op": "replace", "path": "displayName", "value": "Alpha"}]},
    )
    assert pat.status_code == 409, pat.text
    # membership is DB-idempotent: adding the same member twice keeps exactly one edge.
    u = c.post("/scim/v2/Users", headers=H, json={"userName": "gm@x.com"})
    uid = u.json()["id"]
    add_op = {"Operations": [{"op": "add", "path": "members", "value": [{"value": str(uid)}]}]}
    assert c.patch(f"/scim/v2/Groups/{beta_id}", headers=H, json=add_op).status_code == 200
    r2 = c.patch(f"/scim/v2/Groups/{beta_id}", headers=H, json=add_op)
    assert r2.status_code == 200 and len(r2.json()["members"]) == 1, (
        "a repeated member add must be idempotent (one edge)",
        r2.text,
    )
    with Session(engine) as s:
        n = len(
            s.exec(select(UserGroupMember).where(UserGroupMember.group_id == int(beta_id))).all()
        )
        assert n == 1, f"membership not DB-idempotent: {n} rows"
    print(
        "  group uniqueness: ux_usergroup_org_name + ux_usergroupmember_group_user ENFORCED "
        "on the SQLite path (dup name 409, rename->409 not 500, membership idempotent)"
    )


def check_group_create_atomic():
    """create_group is ATOMIC: a create whose members array carries a bad / cross-org id
    must fail with NOTHING persisted -- no orphan empty group, and the name stays free for
    a clean retry. Pre-fix, the group was committed BEFORE _set_members ran, so a rejected
    member left an orphan empty group AND burned the name (every retry then 409'd). Also
    proves the scim.group.create audit row is written ONLY on success, never on the 400."""
    from app.services import audit as audit_svc

    settings.set_many(
        {"scim_enabled": "1", "security_auth_required": "0", "multi_org_enabled": "0"}
    )
    c = TestClient(app)
    _admin, raw = _admin_key("atomic@x.com")
    H = _bearer(raw)
    # one genuinely valid member in this org.
    valid_id = int(
        c.post("/scim/v2/Users", headers=H, json={"userName": "valid@x.com"}).json()["id"]
    )
    # a user id that is NOT in this org (well past any created row) -> _set_members 400s.
    bogus_id = valid_id + 100_000

    def _audit_creates(target):
        with Session(engine) as s:
            from app.models import AuditLog

            return len(
                s.exec(
                    select(AuditLog).where(
                        AuditLog.action == "scim.group.create", AuditLog.target == target
                    )
                ).all()
            )

    # create "Alpha Team" with [valid, bogus] -> 400, and NOTHING persists.
    r = c.post(
        "/scim/v2/Groups",
        headers=H,
        json={
            "displayName": "Alpha Team",
            "members": [{"value": str(valid_id)}, {"value": str(bogus_id)}],
        },
    )
    assert r.status_code == 400 and r.json().get("scimType") == "invalidValue", r.text
    with Session(engine) as s:
        orphan = s.exec(select(UserGroup).where(UserGroup.name == "Alpha Team")).first()
    assert orphan is None, "SECURITY/CORRECTNESS: a failed create left an ORPHAN group row"
    assert _audit_creates("Alpha Team") == 0, "no scim.group.create audit row on the 400 path"

    # the name is still FREE: a retry with only valid members succeeds 201 (was 409 pre-fix).
    r = c.post(
        "/scim/v2/Groups",
        headers=H,
        json={"displayName": "Alpha Team", "members": [{"value": str(valid_id)}]},
    )
    assert r.status_code == 201, ("name must stay free after a failed create", r.text)
    body = r.json()
    assert len(body["members"]) == 1 and body["members"][0]["value"] == str(valid_id), body
    gid = int(body["id"])
    with Session(engine) as s:
        n = len(s.exec(select(UserGroupMember).where(UserGroupMember.group_id == gid)).all())
        assert n == 1, f"the valid member must be applied on the successful create ({n} rows)"
    assert _audit_creates("Alpha Team") == 1, "exactly one scim.group.create audit row on success"

    # a duplicate name -> 409 uniqueness, unchanged (the pre-check path).
    dup = c.post("/scim/v2/Groups", headers=H, json={"displayName": "Alpha Team"})
    assert dup.status_code == 409 and dup.json().get("scimType") == "uniqueness", dup.text

    # a create with only valid members and no collision is the clean 201 baseline.
    ok = c.post(
        "/scim/v2/Groups",
        headers=H,
        json={"displayName": "Bravo Team", "members": [{"value": str(valid_id)}]},
    )
    assert ok.status_code == 201 and len(ok.json()["members"]) == 1, ok.text

    # sanity: audit_svc is the same module the service logs through (guards a refactor rename).
    assert hasattr(audit_svc, "log")
    print(
        "  group create atomic: a bad member -> 400 with NOTHING persisted (no orphan, name "
        "free for retry); audit row only on success; dup-name 409 + valid 201 unchanged"
    )


def check_group_rename_race():
    """Simulate a TRUE post-precheck race: with scim._name_taken stubbed to False (as if a
    concurrent writer took the name AFTER the pre-check passed), a rename still collides at
    the DB unique index. Both PUT and PATCH(rename op + member op, rename FIRST) must
    surface a clean 409 — not a 500 from the pending name change AUTOFLUSHING during the
    member query and escaping the commit-time backstop (the round-2 gate finding). This is
    the raced path the _name_taken pre-check deliberately cannot cover."""
    from app.services import scim as scim_svc

    settings.set_many(
        {"scim_enabled": "1", "security_auth_required": "0", "multi_org_enabled": "0"}
    )
    c = TestClient(app)
    _admin, raw = _admin_key("race@x.com")
    H = _bearer(raw)
    c.post("/scim/v2/Groups", headers=H, json={"displayName": "RaceA"})  # the taken name
    g2 = c.post("/scim/v2/Groups", headers=H, json={"displayName": "RaceB"}).json()["id"]
    uid = c.post("/scim/v2/Users", headers=H, json={"userName": "racem@x.com"}).json()["id"]
    orig = scim_svc._name_taken
    scim_svc._name_taken = lambda *a, **k: False  # pre-check passes; DB index is the guard
    try:
        put = c.put(f"/scim/v2/Groups/{g2}", headers=H, json={"displayName": "RaceA"})
        assert put.status_code == 409, (
            "PUT race must be 409, not 500",
            put.status_code,
            put.text[:200],
        )
        assert put.json().get("scimType") == "uniqueness", put.text[:200]
        patch = c.patch(
            f"/scim/v2/Groups/{g2}",
            headers=H,
            json={
                "Operations": [
                    {"op": "replace", "path": "displayName", "value": "RaceA"},
                    {"op": "add", "path": "members", "value": [{"value": str(uid)}]},
                ]
            },
        )
        assert patch.status_code == 409, (
            "PATCH rename+member race must be 409, not 500",
            patch.status_code,
            patch.text[:200],
        )
        assert patch.json().get("scimType") == "uniqueness", patch.text[:200]
    finally:
        scim_svc._name_taken = orig
    # the failed renames rolled back cleanly: RaceB still exists under its own name.
    assert c.get(f"/scim/v2/Groups/{g2}", headers=H).json()["displayName"] == "RaceB", (
        "a rolled-back rename must leave the group unchanged"
    )
    print(
        "  group rename race: PUT + PATCH(rename+member) surface 409 (not 500) under a "
        "simulated post-precheck collision; clean rollback"
    )


def check_group_delete_with_members():
    """delete_group is FK-SAFE and atomic when the group HAS members. Pre-fix, delete_group
    deleted the member rows and the group in ONE unit-of-work and committed once; with no
    declared ORM cascade, SQLAlchemy could order the parent (usergroup) DELETE before the
    child (usergroupmember) DELETEs -> "FOREIGN KEY constraint failed" -> HTTP 500 (the live
    Okta group-unpush crash). The flush()-before-delete-parent fix forces the child DELETEs
    to the DB first. This check REPRODUCES that 500 on old code: it MUST fail pre-fix.
    Also pins: users survive, exactly one scim.group.delete audit row (success-only), and the
    404 path (nonexistent id) is unchanged."""
    from app.models import AuditLog

    settings.set_many(
        {"scim_enabled": "1", "security_auth_required": "0", "multi_org_enabled": "0"}
    )
    c = TestClient(app)
    _admin, raw = _admin_key("gdel@x.com")
    H = _bearer(raw)
    # two real members in this org.
    m1 = int(c.post("/scim/v2/Users", headers=H, json={"userName": "gdel-a@x.com"}).json()["id"])
    m2 = int(c.post("/scim/v2/Users", headers=H, json={"userName": "gdel-b@x.com"}).json()["id"])
    # a group WITH those members (create carries them -> real edges exist at delete time).
    rg = c.post(
        "/scim/v2/Groups",
        headers=H,
        json={"displayName": "DeleteMe", "members": [{"value": str(m1)}, {"value": str(m2)}]},
    )
    assert rg.status_code == 201, rg.text
    gid = int(rg.json()["id"])
    with Session(engine) as s:
        n = len(s.exec(select(UserGroupMember).where(UserGroupMember.group_id == gid)).all())
    assert n == 2, f"precondition: the group must have 2 member edges before delete ({n})"

    def _delete_audits(target):
        with Session(engine) as s:
            return len(
                s.exec(
                    select(AuditLog).where(
                        AuditLog.action == "scim.group.delete", AuditLog.target == target
                    )
                ).all()
            )

    # THE REGRESSION: DELETE a group that has members. Pre-fix this 500s (FK violation).
    r = c.delete(f"/scim/v2/Groups/{gid}", headers=H)
    assert r.status_code == 204, ("delete of a group WITH members must be 204, not 500", r.text)
    with Session(engine) as s:
        assert s.get(UserGroup, gid) is None, "the group row must be gone"
        assert (
            s.exec(select(UserGroupMember).where(UserGroupMember.group_id == gid)).first() is None
        ), "the membership edges must be gone"
        assert s.get(User, m1) is not None and s.get(User, m2) is not None, (
            "deleting a group must NEVER delete its member users"
        )
    assert _delete_audits("DeleteMe") == 1, "exactly one scim.group.delete audit row on success"

    # a group with NO members still deletes cleanly (the always-worked path stays green).
    empty_gid = int(
        c.post("/scim/v2/Groups", headers=H, json={"displayName": "EmptyDel"}).json()["id"]
    )
    assert c.delete(f"/scim/v2/Groups/{empty_gid}", headers=H).status_code == 204
    with Session(engine) as s:
        assert s.get(UserGroup, empty_gid) is None

    # a nonexistent id -> 404 (no audit row on the refused path), unchanged.
    r = c.delete(f"/scim/v2/Groups/{gid + 500_000}", headers=H)
    assert r.status_code == 404 and _is_scim_error(r.json(), 404), r.text
    print(
        "  group delete: DELETE of a group WITH members -> 204 (no FK 500), group+edges gone, "
        "users untouched, one success-only audit row; empty-group + 404 paths unchanged"
    )


def check_group_delete_cross_org():
    """delete_group cross-org confinement is unchanged: an org-A key deleting an org-B group
    id -> 404 (no-oracle), and the org-B group + its edges are UNTOUCHED."""
    settings.set_many(
        {"scim_enabled": "1", "security_auth_required": "1", "multi_org_enabled": "1"}
    )
    c = TestClient(app)
    with Session(engine) as s:
        oa = Organization(name="Org DA")
        ob = Organization(name="Org DB")
        s.add(oa)
        s.add(ob)
        s.commit()
        s.refresh(oa)
        s.refresh(ob)
        org_a, org_b = oa.id, ob.id
    assert access.multi_org_active(), "the sacred gate must be engaged for this check"
    _admin_a, raw_a = _admin_key("da-admin@x.com", org_id=org_a)
    Ha = _bearer(raw_a)
    _admin_b, raw_b = _admin_key("db-admin@x.com", org_id=org_b)
    Hb = _bearer(raw_b)
    # a member + a group in ORG B, built through org B's own key (real edge).
    b_member = int(
        c.post("/scim/v2/Users", headers=Hb, json={"userName": "db-m@x.com"}).json()["id"]
    )
    b_gid = int(
        c.post(
            "/scim/v2/Groups",
            headers=Hb,
            json={"displayName": "OrgB Grp", "members": [{"value": str(b_member)}]},
        ).json()["id"]
    )
    # org-A key deleting the org-B group id -> 404 no-oracle, and org B's group survives intact.
    r = c.delete(f"/scim/v2/Groups/{b_gid}", headers=Ha)
    assert r.status_code == 404 and _is_scim_error(r.json(), 404), r.text
    with Session(engine) as s:
        assert s.get(UserGroup, b_gid) is not None, "SECURITY: an org-A key deleted an org-B group"
        assert (
            s.exec(select(UserGroupMember).where(UserGroupMember.group_id == b_gid)).first()
            is not None
        ), "the org-B membership edge must survive a cross-org delete attempt"
    settings.set_many({"security_auth_required": "0", "multi_org_enabled": "0"})
    print("  group delete cross-org: org-A key deleting an org-B group -> 404, org B untouched")


def check_group_member_dedup():
    """Duplicate member ids in a create/replace are IDEMPOTENT, not a spurious name-409.
    Pre-fix, members:[{value:"N"},{value:"N"}] inserted two identical (group_id,user_id)
    rows -> the ux_usergroupmember_group_user unique index fired at commit -> the
    IntegrityError handler (which assumes a NAME collision) returned a WRONG 409 uniqueness
    for a name that is FREE. This misled Okta/Entra. After the dedup, [N,N] -> the group has
    user N exactly once, HTTP 201/200. The legitimate duplicate-NAME 409 is UNCHANGED."""
    settings.set_many(
        {"scim_enabled": "1", "security_auth_required": "0", "multi_org_enabled": "0"}
    )
    c = TestClient(app)
    _admin, raw = _admin_key("dedup@x.com")
    H = _bearer(raw)
    uid = int(c.post("/scim/v2/Users", headers=H, json={"userName": "dedup-m@x.com"}).json()["id"])

    # CREATE with a duplicated id -> 201 (was a wrong 409 pre-fix), single membership edge.
    r = c.post(
        "/scim/v2/Groups",
        headers=H,
        json={"displayName": "DedupCreate", "members": [{"value": str(uid)}, {"value": str(uid)}]},
    )
    assert r.status_code == 201, ("dup member ids on create must be 201, not a name-409", r.text)
    gid = int(r.json()["id"])
    assert len(r.json()["members"]) == 1, ("dup member ids must collapse to one member", r.json())
    with Session(engine) as s:
        n = len(s.exec(select(UserGroupMember).where(UserGroupMember.group_id == gid)).all())
        assert n == 1, f"dup member ids on create must yield exactly ONE edge ({n})"

    # a REAL duplicate NAME still -> 409 uniqueness (the dedup did NOT mask a name collision).
    dup = c.post("/scim/v2/Groups", headers=H, json={"displayName": "DedupCreate"})
    assert dup.status_code == 409 and dup.json().get("scimType") == "uniqueness", dup.text

    # REPLACE (PUT) with duplicated ids on a group that ALREADY holds that member -> 200
    # (was a wrong 409 pre-fix). This exercises TWO faults at once: the payload dedup AND
    # the retained-member churn (the member is deleted+re-added in one flush -> the member
    # unique index fires -> _commit_group_or_409 mislabels it a name-409). Both must be gone.
    r = c.put(
        f"/scim/v2/Groups/{gid}",
        headers=H,
        json={"displayName": "DedupCreate", "members": [{"value": str(uid)}, {"value": str(uid)}]},
    )
    assert r.status_code == 200, (
        "dup member ids on PUT replace must be 200, not a name-409",
        r.text,
    )
    assert len(r.json()["members"]) == 1, ("PUT dup ids must collapse to one member", r.json())
    with Session(engine) as s:
        n = len(s.exec(select(UserGroupMember).where(UserGroupMember.group_id == gid)).all())
        assert n == 1, f"dup member ids on PUT must yield exactly ONE edge ({n})"

    # RETAINED-member PUT (no duplicates at all): a full-member-list PUT that KEEPS the
    # existing member while ADDING a second is the shape real IdPs send. Pre-fix the retained
    # member's delete+re-add churned the unique index -> a wrong name-409. Must be 200 with
    # both members present.
    uid2 = int(
        c.post("/scim/v2/Users", headers=H, json={"userName": "dedup-m2@x.com"}).json()["id"]
    )
    r = c.put(
        f"/scim/v2/Groups/{gid}",
        headers=H,
        json={"displayName": "DedupCreate", "members": [{"value": str(uid)}, {"value": str(uid2)}]},
    )
    assert r.status_code == 200, ("a retained-member PUT must be 200, not a churn name-409", r.text)
    with Session(engine) as s:
        rows = s.exec(select(UserGroupMember).where(UserGroupMember.group_id == gid)).all()
        assert {row.user_id for row in rows} == {uid, uid2}, (
            "a retained-member PUT must end with exactly the target set",
            [row.user_id for row in rows],
        )
    print(
        "  group member dedup: dup ids on create/PUT -> 201/200 single edge; a retained-member "
        "PUT no longer churns to a wrong 409; a real duplicate NAME still 409 uniqueness"
    )


def check_deactivation_ends_credentials():
    """identity-03: deactivating an account ENDS its credentials instead of suspending
    them. A session cookie minted before the account was disabled (web toggle, /api
    disable, CLI user disable, SCIM PATCH/PUT active=false, SCIM DELETE) stays dead after
    it is enabled again, and the account's API keys are revoked, so re-enabling starts
    clean: the person signs in again and mints new keys."""
    from tests._cli_runner import run_cli
    from tests._csrf import csrf_for

    settings.set_many({"scim_enabled": "1", "security_auth_required": "1"})
    settings.invalidate_cache()
    pw = "passw0rd-longenough"  # pregate: allow-secret throwaway fixture password
    admin, scim_raw = _admin_key("revive-admin@x.com")
    _row, admin_raw = apikeys.create_key(admin.id, "ops", scopes="api docs admin")
    H = _bearer(scim_raw)

    def sign_in(email):
        c = TestClient(app)
        c.get("/auth/login")
        r = c.post(
            "/auth/login",
            data={"email": email, "password": pw, "csrf_token": csrf_for(c)},
            follow_redirects=False,
        )
        assert r.status_code == 303, (r.status_code, r.text[:300])
        return c

    def replay(cookie):
        rc = TestClient(app)
        rc.cookies.set("session", cookie)
        return rc.get("/account", follow_redirects=False).status_code

    def key_ok(raw):
        return TestClient(app).get("/api/projects", headers={"X-Loc-Token": raw}).status_code == 200

    def victim(tag):
        u = security.create_user(f"revive-{tag}@x.com", "V", Role.TRANSLATOR, pw)
        c = sign_in(u.email)
        cookie = c.cookies.get("session")
        _k, raw = apikeys.create_key(u.id, "laptop", scopes="docs")
        assert replay(cookie) == 200 and key_ok(raw), tag
        return u, cookie, raw

    def assert_clean(tag, cookie, raw, u):
        assert replay(cookie) == 303, f"{tag}: a pre-disable session came back"
        assert not key_ok(raw), f"{tag}: a pre-disable API key came back"
        # the person signs in again normally: re-enabling did not lock them out
        assert sign_in(u.email).get("/account", follow_redirects=False).status_code == 200, tag

    admin_web = sign_in(admin.email)
    # web Users page toggle (disable, then enable)
    u, cookie, raw = victim("web")
    for _ in range(2):
        r = admin_web.post(
            f"/users/{u.id}/toggle",
            data={"csrf_token": csrf_for(admin_web)},
            follow_redirects=False,
        )
        assert r.status_code == 303, r.status_code
    assert security.get_by_email(u.email).active
    assert_clean("web toggle", cookie, raw, u)
    # /api disable + enable (the JSON says how many keys were revoked)
    u, cookie, raw = victim("api")
    api = TestClient(app)
    r = api.post(f"/api/users/{u.id}/disable", headers={"X-Loc-Token": admin_raw})
    assert r.status_code == 200 and r.json().get("keys_revoked") == 1, r.text
    r = api.post(f"/api/users/{u.id}/enable", headers={"X-Loc-Token": admin_raw})
    assert r.status_code == 200, r.text
    assert_clean("/api", cookie, raw, u)
    # CLI user disable + enable
    u, cookie, raw = victim("cli")
    code, out, err = run_cli(["user", "disable", u.email, "--yes"])
    assert code == 0 and "1 API key" in out, (code, out, err)
    code, out, err = run_cli(["user", "enable", u.email])
    assert code == 0, (code, out, err)
    assert_clean("CLI", cookie, raw, u)
    # SCIM PATCH active=false then true, PUT active=false then true, DELETE then PATCH true
    patch_op = "urn:ietf:params:scim:api:messages:2.0:PatchOp"
    for how in ("patch", "put", "delete"):
        u, cookie, raw = victim("scim-" + how)
        if how == "patch":
            body = {
                "schemas": [patch_op],
                "Operations": [{"op": "replace", "value": {"active": False}}],
            }
            r = TestClient(app).patch(f"/scim/v2/Users/{u.id}", headers=H, json=body)
        elif how == "put":
            body = {"userName": u.email, "active": False}
            r = TestClient(app).put(f"/scim/v2/Users/{u.id}", headers=H, json=body)
        else:
            r = TestClient(app).delete(f"/scim/v2/Users/{u.id}", headers=H)
        assert r.status_code in (200, 204), (how, r.status_code, r.text)
        assert replay(cookie) == 303 and not key_ok(raw), how
        body = {"schemas": [patch_op], "Operations": [{"op": "replace", "value": {"active": True}}]}
        r = TestClient(app).patch(f"/scim/v2/Users/{u.id}", headers=H, json=body)
        assert r.status_code == 200 and r.json()["active"] is True, r.text
        assert_clean("SCIM " + how, cookie, raw, u)
    settings.set_many({"security_auth_required": "0"})
    settings.invalidate_cache()
    print("  deactivation: web/api/CLI/SCIM disable ends sessions and revokes API keys;")
    print("                re-enabling never revives a pre-disable cookie or key")


def check_scim_token_owner_disable_is_loud():
    """#422 gate: disabling the account that owns a SCIM token revokes that token for good
    (it is an admin-equivalent credential, so it must not revive either). An operator is
    warned first (Users page confirm, CLI prompt) and told after (/api and CLI warning), and
    every such revocation writes a scim.token_revoked audit row, including when the identity
    provider deprovisions the token's own owner. The last active admin is still refused."""
    from app.models import AuditLog
    from tests._cli_runner import run_cli
    from tests._csrf import csrf_for

    settings.set_many({"scim_enabled": "1", "security_auth_required": "1"})
    settings.invalidate_cache()
    pw = "passw0rd-longenough"  # pregate: allow-secret throwaway fixture password

    def revoked_rows(email):
        with Session(engine) as s:
            rows = s.exec(
                select(AuditLog).where(
                    AuditLog.action == "scim.token_revoked", AuditLog.target == email
                )
            ).all()
        return [r.detail for r in rows]

    keeper, _keeper_scim = _admin_key("scim-keeper@x.com")
    _r, keeper_raw = apikeys.create_key(keeper.id, "ops", scopes="api docs admin")
    # /api: the reply warns, the audit row names the token
    owner, owner_scim = _admin_key("scim-owner-api@x.com")
    r = TestClient(app).post(f"/api/users/{owner.id}/disable", headers={"X-Loc-Token": keeper_raw})
    body = r.json()
    assert r.status_code == 200 and body.get("scim_tokens_revoked") == 1, r.text
    assert "SCIM token" in body.get("warning", ""), body
    assert len(revoked_rows(owner.email)) == 1, revoked_rows(owner.email)
    assert TestClient(app).get("/scim/v2/Users", headers=_bearer(owner_scim)).status_code == 401
    TestClient(app).post(f"/api/users/{owner.id}/enable", headers={"X-Loc-Token": keeper_raw})
    assert TestClient(app).get("/scim/v2/Users", headers=_bearer(owner_scim)).status_code == 401, (
        "re-enable revived a SCIM token"
    )
    # CLI: the confirm prompt and the result both warn
    owner, _raw = _admin_key("scim-owner-cli@x.com")
    code, out, err = run_cli(["user", "disable", owner.email, "--yes"])
    assert code == 0 and "WARNING" in out and "SCIM token" in out, (code, out, err)
    assert len(revoked_rows(owner.email)) == 1
    # Users page: the toggle of a SCIM token owner asks first and says what stops
    owner, _raw = _admin_key("scim-owner-web@x.com")
    plain = security.create_user("plain-web@x.com", "P", Role.TRANSLATOR, pw)
    security.create_user("scim-web-admin@x.com", "A", Role.ADMIN, pw)
    c = TestClient(app)
    c.get("/auth/login")
    r = c.post(
        "/auth/login",
        data={"email": "scim-web-admin@x.com", "password": pw, "csrf_token": csrf_for(c)},
        follow_redirects=False,
    )
    assert r.status_code == 303, r.status_code
    page = c.get("/users").text
    owner_form = page.split(f'action="/users/{owner.id}/toggle"', 1)[1].split(">", 1)[0]
    plain_form = page.split(f'action="/users/{plain.id}/toggle"', 1)[1].split(">", 1)[0]
    assert "data-action-confirm" in owner_form and "SCIM token" in owner_form, owner_form
    assert "data-action-confirm" in plain_form and "SCIM token" not in plain_form, plain_form
    # SCIM deprovisioning the token's OWN owner: honored (offboarding wins), audited loudly,
    # and SCIM with that token stops; another admin's token keeps working.
    owner, owner_scim = _admin_key("scim-owner-self@x.com")
    patch_op = "urn:ietf:params:scim:api:messages:2.0:PatchOp"
    body = {"schemas": [patch_op], "Operations": [{"op": "replace", "value": {"active": False}}]}
    r = TestClient(app).patch(f"/scim/v2/Users/{owner.id}", headers=_bearer(owner_scim), json=body)
    assert r.status_code == 200 and r.json()["active"] is False, r.text
    rows = revoked_rows(owner.email)
    assert len(rows) == 1 and "identity provider" in rows[0], rows
    assert TestClient(app).get("/scim/v2/Users", headers=_bearer(owner_scim)).status_code == 401
    assert TestClient(app).get("/scim/v2/Users", headers=_bearer(_keeper_scim)).status_code == 200
    settings.set_many({"security_auth_required": "0"})
    settings.invalidate_cache()
    print("  SCIM token owner: disable warns first, revokes the token for good, audits")
    print("                    scim.token_revoked (also on IdP self-deprovision)")


def check_active_string_booleans():
    """#481: a string "false" for `active` deactivates on POST, PUT and PATCH (it used to
    read as True via bool("false")); a PUT or POST without `active` keeps the stored
    value; a non-boolean `active` is a SCIM 400."""
    settings.set_many(
        {"scim_enabled": "1", "security_auth_required": "0", "multi_org_enabled": "0"}
    )
    c = TestClient(app)
    _admin, raw = _admin_key("strbool@x.com")
    H = _bearer(raw)

    def _active(uid):
        return c.get(f"/scim/v2/Users/{uid}", headers=H).json()["active"]

    for i, spelling in enumerate(("false", "False", "FALSE", False)):
        r = c.post(
            "/scim/v2/Users",
            headers=H,
            json={"userName": f"sb-put{i}@x.com", "active": True},
        )
        uid = r.json()["id"]
        r = c.put(
            f"/scim/v2/Users/{uid}",
            headers=H,
            json={"userName": f"sb-put{i}@x.com", "active": spelling},
        )
        assert r.status_code == 200 and r.json()["active"] is False, (spelling, r.text)
        assert _active(uid) is False
        # PUT without `active` must not re-activate
        r = c.put(f"/scim/v2/Users/{uid}", headers=H, json={"userName": f"sb-put{i}@x.com"})
        assert r.status_code == 200 and r.json()["active"] is False, ("PUT omit", r.text)
        # POST adopting the deactivated account with "false" keeps it off
        r = c.post(
            "/scim/v2/Users",
            headers=H,
            json={"userName": f"sb-put{i}@x.com", "active": spelling},
        )
        assert r.status_code == 200 and r.json()["active"] is False, ("POST adopt", r.text)
    # fresh POST with "false" creates an inactive account
    r = c.post("/scim/v2/Users", headers=H, json={"userName": "sb-new@x.com", "active": "false"})
    assert r.status_code == 201 and r.json()["active"] is False, r.text
    # PATCH with a string boolean (path and pathless forms)
    uid = c.post("/scim/v2/Users", headers=H, json={"userName": "sb-patch@x.com"}).json()["id"]
    r = c.patch(
        f"/scim/v2/Users/{uid}",
        headers=H,
        json={"Operations": [{"op": "replace", "path": "active", "value": "False"}]},
    )
    assert r.status_code == 200 and r.json()["active"] is False, r.text
    # garbage is refused, never read as a boolean
    for bad in ("maybe", 5, None, [], {}):
        uid2 = c.post(
            "/scim/v2/Users", headers=H, json={"userName": f"sb-bad{abs(hash(str(bad)))}@x.com"}
        ).json()["id"]
        r = c.put(
            f"/scim/v2/Users/{uid2}",
            headers=H,
            json={"userName": "ignored@x.com", "active": bad},
        )
        assert r.status_code == 400 and _is_scim_error(r.json(), 400), (bad, r.text)
        assert _active(uid2) is True
    r = c.patch(
        f"/scim/v2/Users/{uid2}",
        headers=H,
        json={"Operations": [{"op": "replace", "path": "active", "value": "maybe"}]},
    )
    assert r.status_code == 400, r.text
    assert _active(uid2) is True
    print("  string booleans on POST/PUT/PATCH, omitted active kept, garbage 400")


def check_patch_urn_qualified_active():
    """#481 gate: RFC 7644 lets a PATCH name the attribute by its fully qualified URN.
    `urn:ietf:params:scim:schemas:core:2.0:User:active` (path form and pathless key form)
    must deactivate, not be ignored as an unsupported path (a silent deprovision no-op)."""
    settings.set_many(
        {"scim_enabled": "1", "security_auth_required": "0", "multi_org_enabled": "0"}
    )
    c = TestClient(app)
    _admin, raw = _admin_key("urnpatch@x.com")
    H = _bearer(raw)
    urn = "urn:ietf:params:scim:schemas:core:2.0:User:active"
    bodies = (
        {"Operations": [{"op": "replace", "path": urn, "value": False}]},
        {"Operations": [{"op": "Replace", "value": {urn: "False"}}]},
    )
    for i, body in enumerate(bodies):
        uid = c.post("/scim/v2/Users", headers=H, json={"userName": f"urn-{i}@x.com"}).json()["id"]
        r = c.patch(f"/scim/v2/Users/{uid}", headers=H, json=body)
        assert r.status_code == 200 and r.json()["active"] is False, (body, r.text)
        assert c.get(f"/scim/v2/Users/{uid}", headers=H).json()["active"] is False
    # the qualified path is parsed strictly too
    r = c.patch(
        f"/scim/v2/Users/{uid}",
        headers=H,
        json={"Operations": [{"op": "replace", "path": urn, "value": "maybe"}]},
    )
    assert r.status_code == 400, r.text
    print("  PATCH: the URN-qualified active path deactivates (strict boolean)")


def check_group_patch_remove_then_add_and_double_add():
    """#481 (group PATCH half): remove then add of the same member in one request leaves
    the member in; two adds of one member are a clean no-op (it used to 409 or drop the
    member, because every op read the committed rows, not the pending ones)."""
    settings.set_many(
        {"scim_enabled": "1", "security_auth_required": "0", "multi_org_enabled": "0"}
    )
    c = TestClient(app)
    _admin, raw = _admin_key("grpops@x.com")
    H = _bearer(raw)
    muid = int(c.post("/scim/v2/Users", headers=H, json={"userName": "gm-1@x.com"}).json()["id"])
    gid = int(
        c.post(
            "/scim/v2/Groups",
            headers=H,
            json={"displayName": "PatchOps", "members": [{"value": str(muid)}]},
        ).json()["id"]
    )
    r = c.patch(
        f"/scim/v2/Groups/{gid}",
        headers=H,
        json={
            "Operations": [
                {"op": "remove", "path": "members", "value": [{"value": str(muid)}]},
                {"op": "add", "path": "members", "value": [{"value": str(muid)}]},
            ]
        },
    )
    assert r.status_code == 200, r.text
    with Session(engine) as s:
        rows = s.exec(select(UserGroupMember).where(UserGroupMember.group_id == gid)).all()
        assert [x.user_id for x in rows] == [muid], ("remove then add must keep member", rows)
    # two adds of a new member in one PATCH
    m2 = int(c.post("/scim/v2/Users", headers=H, json={"userName": "gm-2@x.com"}).json()["id"])
    op = {"op": "add", "path": "members", "value": [{"value": str(m2)}]}
    r = c.patch(f"/scim/v2/Groups/{gid}", headers=H, json={"Operations": [op, op]})
    assert r.status_code == 200, ("double add must not 409", r.text)
    with Session(engine) as s:
        rows = s.exec(select(UserGroupMember).where(UserGroupMember.group_id == gid)).all()
        assert sorted(x.user_id for x in rows) == sorted([muid, m2]), rows
    print("  group PATCH: remove-then-add keeps the member, double add is a no-op")


def main():
    check_flag_off()
    check_auth()
    check_user_lifecycle()
    check_adoption()
    check_last_admin_and_filter()
    check_pagination()
    check_groups_zero_authz()
    check_group_uniqueness()
    check_group_create_atomic()
    check_group_rename_race()
    check_group_delete_with_members()
    check_group_delete_cross_org()
    check_group_member_dedup()
    check_deactivation_ends_credentials()
    check_scim_token_owner_disable_is_loud()
    check_multiorg_confinement()
    check_active_string_booleans()
    check_patch_urn_qualified_active()
    check_group_patch_remove_then_add_and_double_add()
    print("SCIM OK")


if __name__ == "__main__":
    main()
