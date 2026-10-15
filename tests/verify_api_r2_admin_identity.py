"""SPEC-full-remote-parity Wave R2: the admin & identity /api routes.

The MOST security-sensitive wave: identity + admin management on the remote surface.
This suite drives the real ASGI app through fastapi.testclient in a genuine TWO-ORG
world (multi_org_enabled ON + the login wall ON) AND with remote_admin_enabled toggled
BOTH ways, proving every law the wave binds itself to:

  * CROSS-ORG 404 on every route -- org A's admin key never reaches an org-B user,
    member, connection or apikey; a foreign id is 404, never 403;
  * the admin-scope MINT REFUSAL -- issuing an admin-scoped key over /api is refused
    (400), so a stolen admin key cannot self-replicate;
  * the remote_admin_enabled GATE -- GDPR erase, retention purge-now and apikey
    revoke-all answer 404 when the switch is off, and work when it is on + admin scope;
  * the GDPR-erase email echo -- a mismatched email is a clean 400 and erases nothing;
  * the last-admin guard -- the sole active admin can be neither disabled nor demoted;
  * apikey issue returns the key ONCE and it is NEVER in the audit rows in cleartext;
  * reset-password does NOT leak a plaintext password over /api;
  * connector create REFUSES a secret field (secrets ride R3);
  * the RBAC matrix -- non-admin keys 403 where excluded, the 403 body leaking nothing;
  * user list is org-scoped; happy paths for the admin-ops family.

ASCII-only; standalone one-process-per-suite script (throwaway LOC_TMS_DATA_DIR, keys
cleared -> MockProvider). Final line: "R2 ADMIN IDENTITY OK".
"""

import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(_ROOT)
sys.path.insert(0, _ROOT)
from tests._bootstrap import init_env

init_env("loctms_r2_")

import logging

for _n in ("httpx", "httpcore"):
    logging.getLogger(_n).setLevel(logging.WARNING)

from fastapi.testclient import TestClient
from sqlmodel import Session, select

from app.db import engine, init_db
from app.models import ApiKey, Connection, Organization, Role, User
from app.services import apikeys, audit, security, settings


# --------------------------------------------------------------------------- #
def _gate_on() -> None:
    settings.set_many({"security_auth_required": "1", "multi_org_enabled": "1"})
    settings.invalidate_cache()


def _gate_off() -> None:
    settings.set_many(
        {"security_auth_required": "0", "multi_org_enabled": "0", "remote_admin_enabled": "0"}
    )
    settings.invalidate_cache()


def _remote_admin(on: bool) -> None:
    settings.set_many({"remote_admin_enabled": "1" if on else "0"})
    settings.invalidate_cache()


def _h(raw: str) -> dict:
    return {"X-Loc-Token": raw}


def _key(user_id: int, scopes="api docs") -> str:
    _row, raw = apikeys.create_key(user_id, "r2-test", scopes=scopes)
    return raw


def _user(email, role, org_id, customer_id=None) -> int:
    u = security.create_user(email, email, role, "r2-password-1234", customer_id=customer_id)
    with Session(engine) as s:
        row = s.get(User, u.id)
        row.org_id = org_id
        s.add(row)
        s.commit()
    return u.id


def _client():
    from app.main import app

    return TestClient(app)


def _is(r, code, what):
    assert r.status_code == code, f"{what}: expected {code}, got {r.status_code} ({r.text[:200]})"


def _forbidden(r, what):
    _is(r, 403, what)
    body = r.json()
    assert body.get("ok") is False, (what, body)
    assert body.get("error") == "your role does not allow this action.", (what, body)


def _seed():
    """One org (A) holding the SOLE install admin, plus non-admin roles + erase
    victims; org B is the cross-org foil carrying a user, a connection and an apikey
    but NO admin (so admin_a stays the only admin install-wide -- the last-admin guard
    is exercisable, and cross-org tests need only foreign OBJECTS, not a foreign admin)."""
    init_db()
    with Session(engine) as s:
        org_a, org_b = Organization(name="Org A"), Organization(name="Org B")
        s.add(org_a)
        s.add(org_b)
        s.commit()
        s.refresh(org_a)
        s.refresh(org_b)
        oa, ob = org_a.id, org_b.id

    settings.set_many({"multi_org_enabled": "1"})
    settings.invalidate_cache()

    admin_a = _user("admina@x.com", Role.ADMIN, oa)  # the SOLE install admin
    manager_a = _user("mgra@x.com", Role.MANAGER, oa)
    linguist_a = _user("linga@x.com", Role.LINGUIST, oa)
    translator_a = _user("tra@x.com", Role.TRANSLATOR, oa)
    viewer_a = _user("viewa@x.com", Role.VIEWER, oa)
    victim1 = _user("victim1@x.com", Role.TRANSLATOR, oa)
    victim2 = _user("victim2@x.com", Role.TRANSLATOR, oa)
    victim3 = _user("victim3@x.com", Role.TRANSLATOR, oa)

    user_b = _user("userb@x.com", Role.TRANSLATOR, ob)  # foreign user
    # An org-B ADMIN: proves the last-admin guard is ORG-SCOPED -- org A must not be
    # able to disable/demote org-A's last admin just because org B still has one.
    admin_b = _user("adminb@x.com", Role.ADMIN, ob)
    # A foreign connection + a foreign apikey (org B) for the cross-org 404 probes.
    with Session(engine) as s:
        conn_b = Connection(kind="airtable", name="B conn", org_id=ob, created_by="userb@x.com")
        s.add(conn_b)
        s.commit()
        s.refresh(conn_b)
        conn_b_id = conn_b.id
    _kb_row, _kb_raw = apikeys.create_key(user_b, "b-key", scopes="api docs")
    with Session(engine) as s:
        key_b_id = s.exec(select(ApiKey.id).where(ApiKey.user_id == user_b)).first()

    keys = {
        # admin_a: one key with the admin scope (covers everything), one WITHOUT it
        # (proves the admin-scope routes 403 a docs-only admin key).
        "admin_scope": _key(admin_a, "api docs admin"),
        "admin_docs": _key(admin_a, "api docs"),
        "manager": _key(manager_a),
        "linguist": _key(linguist_a),
        "translator": _key(translator_a),
        "viewer": _key(viewer_a),
    }
    return {
        "keys": keys,
        "oa": oa,
        "ob": ob,
        "admin_a": admin_a,
        "manager_a": manager_a,
        "victim1": victim1,
        "victim2": victim2,
        "victim3": victim3,
        "user_b": user_b,
        "admin_b": admin_b,
        "conn_b": conn_b_id,
        "key_b": key_b_id,
    }


# --------------------------------------------------------------------------- #
# 1. cross-org 404 on every route
# --------------------------------------------------------------------------- #
def test_cross_org_404(d, api):
    h = _h(d["keys"]["admin_scope"])
    ub, kb, cb = d["user_b"], d["key_b"], d["conn_b"]
    cases = [
        api.get("/api/users/%d" % ub, headers=h),  # noqa: UP031
        api.post("/api/users/%d/role" % ub, headers=h, json={"role": "reviewer"}),  # noqa: UP031
        api.post("/api/users/%d/department" % ub, headers=h, json={"department": "x"}),  # noqa: UP031
        api.post("/api/users/%d/customer" % ub, headers=h, json={"clear": True}),  # noqa: UP031
        api.post("/api/users/%d/enable" % ub, headers=h, json={}),  # noqa: UP031
        api.post("/api/users/%d/disable" % ub, headers=h, json={}),  # noqa: UP031
        api.post(
            "/api/users/%d/reset-password" % ub,  # noqa: UP031
            headers=h,
            json={"password": "long-enough-pw-123"},  # noqa: RUF100, UP031
        ),
        api.get("/api/users/%d/export" % ub, headers=h),  # noqa: UP031
        api.post("/api/users/%d/erase" % ub, headers=h, json={"email": "userb@x.com"}),  # noqa: UP031
        api.post("/api/apikeys/%d/revoke" % kb, headers=h, json={}),  # noqa: UP031
        api.post("/api/apikeys/revoke-all", headers=h, json={"user": "userb@x.com"}),
        api.post("/api/connectors/%d/update" % cb, headers=h, json={"name": "x"}),  # noqa: UP031
        api.post("/api/connectors/%d/test" % cb, headers=h, json={}),  # noqa: UP031
        api.post("/api/connectors/%d/browse" % cb, headers=h, json={}),  # noqa: UP031
        api.post("/api/connectors/%d/schedule" % cb, headers=h, json={"off": True}),  # noqa: UP031
        api.post("/api/connectors/%d/delete" % cb, headers=h, json={}),  # noqa: UP031
    ]
    for i, r in enumerate(cases):
        _is(r, 404, "cross-org route #%d" % i)  # noqa: UP031
    # #231: remote issuance is self-only, so a mint for ANY other user is the same 403
    # whether that user exists, is in org B, or is unknown (no existence oracle).
    for who in ("userb@x.com", "nobody-at-all@x.com"):
        r = api.post("/api/apikeys/issue", headers=h, json={"user": who})
        _is(r, 403, "issue for another user (%s)" % who)  # noqa: UP031
    # ...and the org B user is NOT anonymized by the refused erase.
    with Session(engine) as s:
        assert s.get(User, ub).email == "userb@x.com", "a cross-org erase mutated org B"
    print(
        "  [1] cross-org: A's admin key 404s on all %d of B's routes; the refused "  # noqa: UP031
        "erase left the org-B user untouched" % len(cases)
    )


# --------------------------------------------------------------------------- #
# 2. admin-scope mint refusal
# --------------------------------------------------------------------------- #
def test_admin_mint_refusal(d, api):
    h = _h(d["keys"]["admin_scope"])
    before = _key_count()
    for scopes in ("admin", "api docs admin", "docs,admin"):
        r = api.post("/api/apikeys/issue", headers=h, json={"user": "mgra@x.com", "scopes": scopes})
        _is(r, 400, f"admin-scope mint ({scopes})")
        assert r.json().get("ok") is False, r.text
        assert "admin-scoped" in r.json().get("error", ""), r.text
    # input hygiene (adversarial review LOW): a non-string scopes value and an unknown
    # scope token are each a clean 400, not a junk grant persisted onto a key.
    r = api.post("/api/apikeys/issue", headers=h, json={"user": "mgra@x.com", "scopes": ["admin"]})
    _is(r, 400, "non-string scopes")
    assert "string" in r.json().get("error", ""), r.text
    r = api.post(
        "/api/apikeys/issue", headers=h, json={"user": "mgra@x.com", "scopes": "docs bogus"}
    )
    _is(r, 400, "unknown scope token")
    assert "unknown scope" in r.json().get("error", ""), r.text
    assert _key_count() == before, "a refused admin-scope mint still created a key"
    print(
        "  [2] admin-mint refusal: issuing an admin-scoped key over /api is a 400 "
        "in every spelling; a non-string or unknown scope is a clean 400 too; "
        "NOTHING was minted"
    )


# --------------------------------------------------------------------------- #
# 3. the remote_admin_enabled gate + admin-scope requirement
# --------------------------------------------------------------------------- #
def test_remote_admin_gate(d, api):
    admin = _h(d["keys"]["admin_scope"])
    docs_only = _h(d["keys"]["admin_docs"])
    v = d["victim1"]
    # (a) SWITCH OFF: the three destructive verbs are 404 even for an admin-scope key.
    _remote_admin(False)
    _is(
        api.post("/api/users/%d/erase" % v, headers=admin, json={"email": "victim1@x.com"}),  # noqa: UP031
        404,
        "erase switch-off",
    )
    _is(
        api.post("/api/apikeys/revoke-all", headers=admin, json={"user": "victim1@x.com"}),
        404,
        "revoke-all switch-off",
    )
    _is(
        api.post("/api/admin/retention/purge", headers=admin, json={"confirm": "purge"}),
        404,
        "purge switch-off",
    )
    with Session(engine) as s:
        assert s.get(User, v).email == "victim1@x.com", "a 404 erase still erased"
    # (b) SWITCH ON but a DOCS-ONLY key (no admin scope): 403, not 404 (scope gate).
    _remote_admin(True)
    _is(
        api.post("/api/users/%d/erase" % v, headers=docs_only, json={"email": "victim1@x.com"}),  # noqa: UP031
        403,
        "erase without admin scope",
    )
    assert "admin-scoped" in api.post(
        "/api/users/%d/erase" % v,  # noqa: UP031
        headers=docs_only,
        json={"email": "victim1@x.com"},  # noqa: RUF100, UP031
    ).json().get("error", ""), "scope 403 body"
    # (c) GDPR export needs admin scope but NOT the switch.
    _remote_admin(False)
    r = api.get("/api/users/%d/export" % v, headers=admin)  # noqa: UP031
    _is(r, 200, "export admin-scope")
    assert '"section": "user"' in r.text, r.text[:200]
    _is(api.get("/api/users/%d/export" % v, headers=docs_only), 403, "export without admin scope")  # noqa: UP031
    print(
        "  [3] remote_admin gate: erase/revoke-all/purge are 404 with the switch "
        "off (erasing nothing) and 403 for a docs-only key with it on; export needs "
        "the admin scope but not the switch"
    )


# --------------------------------------------------------------------------- #
# 4. GDPR erase: email echo + the switch-on happy path
# --------------------------------------------------------------------------- #
def test_gdpr_erase(d, api):
    admin = _h(d["keys"]["admin_scope"])
    _remote_admin(True)
    v = d["victim1"]
    # wrong echo -> 400, nothing erased.
    r = api.post("/api/users/%d/erase" % v, headers=admin, json={"email": "WRONG@x.com"})  # noqa: UP031
    _is(r, 400, "erase wrong echo")
    with Session(engine) as s:
        assert s.get(User, v).email == "victim1@x.com", "a mismatched echo erased anyway"
    # missing echo -> 400 too.
    _is(api.post("/api/users/%d/erase" % v, headers=admin, json={}), 400, "erase missing echo")  # noqa: UP031
    # correct echo -> 200, the user is anonymized (email tombstoned, deactivated).
    r = api.post("/api/users/%d/erase" % v, headers=admin, json={"email": "victim1@x.com"})  # noqa: UP031
    _is(r, 200, "erase correct echo")
    assert r.json().get("ok") is True, r.text
    with Session(engine) as s:
        row = s.get(User, v)
        assert row.email != "victim1@x.com" and not row.active, "erase did not anonymize"
    print(
        "  [4] GDPR erase: a wrong/absent email echo is a 400 that erases nothing; "
        "the exact echo anonymizes the user (tombstoned + deactivated)"
    )


# --------------------------------------------------------------------------- #
# 5. the last-admin guard (disable + demote)
# --------------------------------------------------------------------------- #
def test_last_admin_guard(d, api):
    admin = _h(d["keys"]["admin_scope"])
    a = d["admin_a"]
    # admin_a is org A's ONLY admin -- and org B HAS an admin (admin_b). The guard is
    # ORG-SCOPED, so disabling/demoting admin_a is still refused (409): org A must not
    # be able to self-lock its administration because a DIFFERENT org has an admin.
    with Session(engine) as s:
        assert s.get(User, d["admin_b"]).role == Role.ADMIN, "org B must have an admin"
    r = api.post("/api/users/%d/disable" % a, headers=admin, json={})  # noqa: UP031
    _is(r, 409, "disable last org-A admin (org B has admins)")
    assert "administrator" in r.json().get("error", ""), r.text
    r = api.post("/api/users/%d/role" % a, headers=admin, json={"role": "translator"})  # noqa: UP031
    _is(r, 409, "demote last org-A admin (org B has admins)")
    with Session(engine) as s:
        row = s.get(User, a)
        assert row.active and row.role == Role.ADMIN, "the sole org-A admin was changed"
    print(
        "  [5] last-admin guard (org-scoped): org A's sole admin can be neither "
        "disabled nor demoted even though org B has an admin -- no cross-org "
        "self-lock; it stays an active admin"
    )


# --------------------------------------------------------------------------- #
# 6. apikey issue: key shown once, NEVER in the audit cleartext; revoke-all works
# --------------------------------------------------------------------------- #
def test_apikey_lifecycle(d, api):
    admin = _h(d["keys"]["admin_scope"])
    _remote_admin(True)
    # #231: an admin cannot mint a remote key owned by another user (403, nothing made).
    r = api.post(
        "/api/apikeys/issue",
        headers=admin,
        json={"user": "victim2@x.com", "scopes": "docs", "name": "v2"},
    )
    _is(r, 403, "apikey issue for another user")
    # issue for the caller returns the plaintext ONCE.
    r = api.post("/api/apikeys/issue", headers=admin, json={"scopes": "docs", "name": "self"})
    _is(r, 200, "apikey issue (self)")
    body = r.json()
    full = body.get("api_key")
    assert full and full.startswith("lsu_"), body
    assert "admin" not in apikeys.scope_set(body["scopes"]), body
    # the plaintext key is NEVER in the audit rows.
    rows_text = "\n".join(f"{rec}" for rec in audit.iter_export())
    assert full not in rows_text, "the minted key material leaked into the audit log"
    # victim2's key is minted locally (the host path) for the revoke-all check below.
    apikeys.create_key(d["victim2"], "v2", scopes="docs")
    # revoke-all (admin scope + switch on) revokes the newly-issued key.
    r = api.post("/api/apikeys/revoke-all", headers=admin, json={"user": "victim2@x.com"})
    _is(r, 200, "revoke-all")
    assert r.json().get("revoked", 0) >= 1, r.text
    with Session(engine) as s:
        live = s.exec(
            select(ApiKey).where(ApiKey.user_id == d["victim2"], ApiKey.active == True)  # noqa: E712
        ).all()
        assert not live, "revoke-all left a live key"
    print(
        "  [6] apikey issue returns the key once, its cleartext never touches the "
        "audit log, and revoke-all (admin scope + switch) kills every key"
    )


# --------------------------------------------------------------------------- #
# 7. reset-password does NOT leak a plaintext password
# --------------------------------------------------------------------------- #
def test_reset_password_no_leak(d, api):
    # reset-password is an account-security action: admin scope (#270 r2-apiauth-10)
    admin = _h(d["keys"]["admin_scope"])
    r = api.post(
        "/api/users/%d/reset-password" % d["victim3"],  # noqa: UP031
        headers=admin,
        json={"password": "a-brand-new-password-9"},
    )
    _is(r, 200, "reset-password")
    body = r.json()
    assert set(body.keys()) <= {"ok", "email"}, ("password leaked into the response", body)
    assert "a-brand-new-password-9" not in r.text, "the plaintext password was echoed"
    # a too-short password is a clean 400 (policy enforced), and never echoed.
    r = api.post(
        "/api/users/%d/reset-password" % d["victim3"],  # noqa: UP031
        headers=admin,
        json={"password": "short"},  # noqa: RUF100, UP031
    )
    _is(r, 400, "reset-password policy")
    assert "short" not in r.text, "a rejected password was echoed"
    print(
        "  [7] reset-password sets a password and returns only ok+email; the "
        "plaintext is never in the response, and a policy failure is a clean 400"
    )


# --------------------------------------------------------------------------- #
# 8. connector create REFUSES a secret field
# --------------------------------------------------------------------------- #
def test_connector_secret_refusal(d, api):
    h = _h(d["keys"]["admin_docs"])  # admin is can_tune_ai
    before = _conn_count()
    for field in ("api_key", "secret", "token", "password"):
        r = api.post(
            "/api/connectors", headers=h, json={"kind": "airtable", "name": "X", field: "s3cr3t"}
        )
        _is(r, 400, f"connector secret field {field}")
        assert "secret" in r.json().get("error", "").lower(), r.text
    # NESTED under config (adversarial review MEDIUM): must ALSO be refused, nothing
    # stored -- a shallow top-level scan would have stored api_key in cleartext.
    r = api.post(
        "/api/connectors",
        headers=h,
        json={"kind": "airtable", "name": "X", "config": {"api_key": "SECRET", "base_id": "app1"}},
    )
    _is(r, 400, "connector secret nested under config")
    assert "api_key" in r.json().get("error", ""), r.text
    assert _conn_count() == before, "a refused secret create still created a connection"
    # a NON-secret create works.
    r = api.post(
        "/api/connectors",
        headers=h,
        json={"kind": "airtable", "name": "Clean", "config": {"base_id": "app1"}},
    )
    _is(r, 200, "connector non-secret create")
    assert r.json()["auth"] == "none", r.text
    print(
        "  [8] connector create refuses any credential field (400, nothing created) "
        "and a non-secret create lands with auth=none"
    )


# --------------------------------------------------------------------------- #
# 9. the RBAC matrix + org-scoped list
# --------------------------------------------------------------------------- #
def test_rbac_and_scope(d, api):
    view, tr, mgr, ling = (
        _h(d["keys"]["viewer"]),
        _h(d["keys"]["translator"]),
        _h(d["keys"]["manager"]),
        _h(d["keys"]["linguist"]),
    )
    # is_admin routes (docs-scope + is_admin role): viewer/translator/manager/linguist all
    # 403 with the bare ROLE-gate string.
    for k, label in ((view, "viewer"), (tr, "translator"), (mgr, "manager"), (ling, "linguist")):
        _forbidden(api.get("/api/users", headers=k), f"{label} -> user list")
        _forbidden(
            api.post("/api/routing/rules", headers=k, json={"ext": "docx"}),
            f"{label} -> routing add",
        )
    # docs-scope-privilege fix: /api/admin/srx/enable (and its retention sibling)
    # are now ADMIN-SCOPE routes (_r3_gate), so a docs-scoped key is rejected on SCOPE
    # BEFORE the role check -- even an admin-ROLE docs key (admin_docs). The refusal is the
    # scope string, not the role string. Pre-fix this route was docs-scope + role-only, so
    # this assertion FAILS if the fix is reverted (it would 200, or 403 with the role
    # string, for the docs key).
    ad = _h(d["keys"]["admin_docs"])
    r = api.post("/api/admin/srx/enable", headers=ad, json={})
    _is(r, 403, "admin-role docs key -> srx enable is scope-403")
    assert r.json().get("error") == "This endpoint requires an admin-scoped user API key.", (
        "srx/enable must reject a docs-scoped key on SCOPE, not role",
        r.text,
    )
    # members = can_manage, and RING-BEFORE-ROLE: a foreign/missing project is 404
    # (never a 403 that would confirm the project exists), even to a translator.
    _is(
        api.get("/api/projects/999999/members", headers=tr),
        404,
        "translator -> members (ring before role)",
    )
    # connector CRUD = can_tune_ai: a translator lacks it (403).
    _forbidden(
        api.post("/api/connectors", headers=tr, json={"kind": "airtable", "name": "z"}),
        "translator -> connector create",
    )
    # org-scoped user list: the admin sees org-A users only, never the org-B user.
    admin = _h(d["keys"]["admin_docs"])
    r = api.get("/api/users", headers=admin)
    _is(r, 200, "admin user list")
    emails = {u["email"] for u in r.json()["users"]}
    assert "userb@x.com" not in emails, "the org-scoped list leaked an org-B user"
    assert "admina@x.com" in emails, emails
    print(
        "  [9] RBAC: viewer/translator/manager/linguist 403 on is_admin routes "
        "(bare role-gate string); connector create is can_tune_ai; the user list is "
        "org-scoped (no org-B user)"
    )


# --------------------------------------------------------------------------- #
# 10. happy paths across the admin-ops family
# --------------------------------------------------------------------------- #
def test_happy_paths(d, api):
    admin = _h(d["keys"]["admin_docs"])
    admin_scope = _h(d["keys"]["admin_scope"])
    # user create -> role -> disable -> enable
    r = api.post(
        "/api/users",
        headers=admin_scope,
        json={"email": "fresh@x.com", "role": "translator", "department": "docs"},
    )
    _is(r, 200, "user create")
    uid = r.json()["id"]
    assert r.json()["department"] == "docs", r.text
    _is(
        api.post("/api/users/%d/role" % uid, headers=admin_scope, json={"role": "reviewer"}),  # noqa: UP031
        200,
        "user role",
    )
    _is(api.post("/api/users/%d/disable" % uid, headers=admin, json={}), 200, "user disable")  # noqa: UP031
    # #270 gate ruling: enable revives the account's still-active keys, so admin scope
    _is(api.post("/api/users/%d/enable" % uid, headers=admin, json={}), 403, "docs enable")  # noqa: UP031
    _is(api.post("/api/users/%d/enable" % uid, headers=admin_scope, json={}), 200, "user enable")  # noqa: UP031
    # invite
    r = api.post(
        "/api/users/invite",
        headers=admin_scope,
        json={"email": "invited@x.com", "role": "linguist"},
    )
    _is(r, 200, "user invite")
    # #270 r2-apiauth-10: an admin-ROLE key holding only docs is refused on every account-security
    # action (create, role, reset-password, MFA reset, invite) on SCOPE, before the role check.
    scope_msg = "This endpoint requires an admin-scoped user API key."
    for method, path, body in (
        ("post", "/api/users", {"email": "docsmade@x.com", "role": "admin"}),
        ("post", "/api/users/%d/role" % uid, {"role": "admin"}),  # noqa: UP031
        ("post", "/api/users/%d/reset-password" % uid, {"password": "a-brand-new-password-9"}),  # noqa: UP031
        ("delete", "/api/users/%d/mfa" % uid, None),  # noqa: UP031
        ("post", "/api/users/%d/enable" % uid, {}),  # noqa: UP031
        ("post", "/api/users/invite", {"email": "docsinvite@x.com", "role": "admin"}),
    ):
        kw = {"json": body} if body is not None else {}
        rr = getattr(api, method)(path, headers=admin, **kw)
        _is(rr, 403, f"docs-scoped key refused: {method} {path}")
        assert rr.json().get("error") == scope_msg, (path, rr.text)
    assert security.get_by_email("docsmade@x.com") is None, "a docs key created a user"
    assert r.json()["token"] and r.json()["accept_path"].startswith("/auth/accept/"), r.text
    foreign = security.create_invite("foreign-invite@x.com", "translator", "adminb@x.com")
    _is(api.delete(f"/api/users/invites/{foreign.id}", headers=admin), 404, "foreign invite")
    assert security.get_invite(foreign.token) is not None
    # #129: web uses the same durable org boundary; foreign revoke/resend do not audit.
    from app.models import AuditLog
    from tests._csrf import csrf_for

    web = _client()
    csrf_for(web)
    _is(
        web.post(
            "/auth/login",
            data={
                "email": "admina@x.com",
                "password": "r2-password-1234",  # pregate: allow-secret fixture login
            },
            follow_redirects=False,
        ),
        303,
        "web admin login",
    )
    csrf_for(web)
    import io
    import json
    from contextlib import redirect_stdout

    from app.cli.__main__ import main as cli_main

    output = io.StringIO()
    with redirect_stdout(output):
        assert (
            cli_main(
                [
                    "--json",
                    "--org",
                    str(d["oa"]),
                    "user",
                    "invite",
                    "--email",
                    "cli-web@x.com",
                    "--role",
                    "translator",
                ]
            )
            == 0
        )
    cli_created = security.get_invite(json.loads(output.getvalue())["token"])
    assert cli_created.created_by == "cli" and cli_created.org_id == d["oa"]
    assert cli_created.email in web.get("/users").text
    assert cli_created.id in {
        i["id"] for i in api.get("/api/users/invites", headers=admin).json()["invites"]
    }
    _is(
        api.delete(f"/api/users/invites/{cli_created.id}", headers=_h(_key(d["admin_b"]))),
        404,
        "foreign CLI-created invite",
    )
    _is(
        web.post(f"/users/invite/{cli_created.id}/revoke", follow_redirects=False),
        303,
        "web revoke CLI-created invite",
    )
    assert security.get_invite(cli_created.token) is None
    with Session(engine) as s:
        before = len(s.exec(select(AuditLog).where(AuditLog.target == foreign.email)).all())
    _is(
        web.post(f"/users/invite/{foreign.id}/revoke", follow_redirects=False),
        404,
        "web foreign revoke",
    )
    _is(
        web.post(
            "/users/invite",
            data={"email": foreign.email, "role": foreign.role, "resend_id": foreign.id},
            follow_redirects=False,
        ),
        404,
        "web foreign resend",
    )
    with Session(engine) as s:
        assert len(s.exec(select(AuditLog).where(AuditLog.target == foreign.email)).all()) == before
    assert security.get_invite(foreign.token) is not None
    expired = security.create_invite("expired-api@x.com", "translator", "admina@x.com", days=-1)
    expired_list = api.get("/api/users/invites?expired=1", headers=admin)
    _is(expired_list, 200, "expired invitations")
    assert expired.id in {i["id"] for i in expired_list.json()["invites"]}
    assert expired.token not in expired_list.text and foreign.email not in expired_list.text
    output = io.StringIO()
    with redirect_stdout(output):
        assert cli_main(["--json", "--org", str(d["oa"]), "user", "invites", "--expired"]) == 0
    assert json.loads(output.getvalue())["invites"] == expired_list.json()["invites"]
    assert expired.id not in {
        i["id"] for i in api.get("/api/users/invites", headers=admin).json()["invites"]
    }
    for key_name in ("manager", "translator", "viewer"):
        non_admin = _h(d["keys"][key_name])
        _is(api.get("/api/users/invites", headers=non_admin), 403, "non-admin invite list")
        _is(
            api.delete(f"/api/users/invites/{foreign.id}", headers=non_admin),
            403,
            "non-admin revoke",
        )
        _is(
            api.delete(f"/api/users/invites/{expired.id}", headers=non_admin),
            403,
            "non-admin expired delete",
        )
    # SPEC-expired-invite-delete: an EXPIRED invite is deletable (soft) on API + web, but
    # only inside the caller's org; a foreign org's expired invite stays 404 on both.
    foreign_expired = security.create_invite(
        "foreign-lapsed@x.com", "translator", "adminb@x.com", org_id=d["ob"], days=-1
    )
    assert foreign_expired.email not in web.get("/users").text
    with Session(engine) as s:
        before = len(s.exec(select(AuditLog).where(AuditLog.target == foreign_expired.email)).all())
    _is(
        api.delete(f"/api/users/invites/{foreign_expired.id}", headers=admin),
        404,
        "foreign expired API delete",
    )
    _is(
        web.post(f"/users/invite/{foreign_expired.id}/revoke", follow_redirects=False),
        404,
        "foreign expired web delete",
    )
    from app.models import Invite

    with Session(engine) as s:
        assert s.get(Invite, foreign_expired.id).revoked_at is None
        after = len(s.exec(select(AuditLog).where(AuditLog.target == foreign_expired.email)).all())
        assert after == before, "a refused cross-org delete must not audit"
    gone = api.delete(f"/api/users/invites/{expired.id}", headers=admin)
    _is(gone, 200, "expired API delete")
    assert gone.json()["expired"] is True and gone.json()["email"] == expired.email
    assert expired.id not in {
        i["id"] for i in api.get("/api/users/invites?expired=1", headers=admin).json()["invites"]
    }
    _is(api.delete(f"/api/users/invites/{expired.id}", headers=admin), 404, "already deleted")
    with Session(engine) as s:
        assert s.get(Invite, expired.id).revoked_at is not None, "soft delete keeps the row"
        assert any(
            e.action == "user.invite.revoke" and e.detail == "expired"
            for e in s.exec(select(AuditLog).where(AuditLog.target == expired.email)).all()
        )
    web_lapsed = security.create_invite(
        "web-lapsed@x.com", "translator", "admina@x.com", org_id=d["oa"], days=-1
    )
    r_web = web.post(f"/users/invite/{web_lapsed.id}/revoke", follow_redirects=False)
    _is(r_web, 303, "own-org expired web delete")
    assert "deleted=" in r_web.headers["location"]
    assert web_lapsed.id not in {i.id for i in security.expired_invites(d["oa"])}
    invitation = r.json()
    listed = api.get("/api/users/invites", headers=admin)
    _is(listed, 200, "live invitations")
    assert foreign.email not in listed.text and foreign.token not in listed.text
    assert invitation["token"] not in listed.text and '"token"' not in listed.text
    assert any(i["id"] == invitation["id"] for i in listed.json()["invites"])
    revoked = api.delete(f"/api/users/invites/{invitation['id']}", headers=admin)
    _is(revoked, 200, "revoke invitation")
    assert revoked.json()["email"] == invitation["email"] and revoked.json()["revoked_at"]
    assert revoked.json()["expired"] is False, "a pending revoke is not an expired delete"
    assert security.get_invite(invitation["token"]) is None
    _is(api.delete(f"/api/users/invites/{invitation['id']}", headers=admin), 404, "already revoked")
    _is(api.delete("/api/users/invites/999999", headers=admin), 404, "unknown invite")
    # routing rule add -> toggle -> delete
    r = api.post(
        "/api/routing/rules",
        headers=admin,
        json={"ext": "docx", "department": "docs", "priority": 50},
    )
    _is(r, 200, "routing add")
    rid = r.json()["id"]
    assert api.get("/api/routing/rules", headers=admin).json()["rules"], "routing list"
    _is(
        api.post("/api/routing/rules/%d/toggle" % rid, headers=admin, json={}),  # noqa: UP031
        200,
        "routing toggle",
    )
    _is(
        api.post("/api/routing/rules/%d/delete" % rid, headers=admin, json={}),  # noqa: UP031
        200,
        "routing delete",
    )
    # budget set -> list -> delete
    r = api.post("/api/budgets", headers=admin, json={"department": "docs", "limit": 500000})
    _is(r, 200, "budget set")
    bid = r.json()["id"]
    assert any(b["id"] == bid for b in api.get("/api/budgets", headers=admin).json()["budgets"]), (
        "budget list"
    )
    _is(api.post("/api/budgets/%d/delete" % bid, headers=admin, json={}), 200, "budget delete")  # noqa: UP031
    # lqa rulebook set + get. #425 (merged 2026-10-05): a rulebook is one file per pair for
    # the WHOLE install, so with multi-org ON (this world) an org admin's set AND get are a
    # 403 that writes nothing and leaks nothing; the host CLI is the only door. Same shape
    # as the #281 retention block below: refusal under multi-org, round-trip single-org.
    from app.services import lqa as _lqa

    assert _lqa.load_rulebook("en-US", "fr-FR") == "", "no rulebook before the set"
    r = api.post(
        "/api/lqa/rulebook",
        headers=admin,
        json={"source": "en-US", "target": "fr-FR", "text": "Be formal."},
    )
    _is(r, 403, "lqa set under multi-org")
    assert r.json()["error"] == _lqa.RULEBOOK_HOST_ONLY, r.text
    assert _lqa.load_rulebook("en-US", "fr-FR") == "", "a refused set wrote the rulebook"
    _lqa.save_rulebook("en-US", "fr-FR", "HOST RULES")  # the host CLI's write path
    got = api.get("/api/lqa/rulebook?source=en-US&target=fr-FR", headers=admin)
    _is(got, 403, "lqa get under multi-org")
    assert "HOST RULES" not in got.text, "a refused get leaked the host rulebook"
    settings.set_many({"multi_org_enabled": "0"})
    try:
        _is(
            api.post(
                "/api/lqa/rulebook",
                headers=admin,
                json={"source": "en-US", "target": "fr-FR", "text": "Be formal."},
            ),
            200,
            "lqa set",
        )
        got = api.get("/api/lqa/rulebook?source=en-US&target=fr-FR", headers=admin)
        _is(got, 200, "lqa get")
        assert got.json()["rulebook"] == "Be formal.", got.text
    finally:
        settings.set_many({"multi_org_enabled": "1"})
    # semantic + srx toggles + retention config: docs-scope-privilege fix promoted these
    # install-global config-mutating routes to the R3 three-lock preamble (admin SCOPE +
    # is_admin role + remote_admin_enabled), IDENTICAL to POST /api/admin/config. So they
    # need the admin_SCOPE key AND the switch on -- the docs-scope admin key that USED to
    # arm them here is now correctly rejected (that rejection is asserted in
    # test_rbac_and_scope + verify_admin_scope_tier). Arm the switch first.
    _remote_admin(True)
    # #424 (merged 2026-10-05): srx_enabled is an install-wide segmentation key, so under
    # multi-org (this world) the org admin's toggle is the #281 host-only 403 and the setting
    # is untouched; on a single-org install the toggles still round-trip.
    srx_before = settings.get("srx_enabled")
    r = api.post("/api/admin/srx/enable", headers=admin_scope, json={})
    _is(r, 403, "srx enable under multi-org")
    assert "srx_enabled" in r.json()["error"], r.text
    assert settings.get("srx_enabled") == srx_before, "a refused srx enable changed the setting"
    settings.set_many({"multi_org_enabled": "0"})
    try:
        _is(api.post("/api/admin/srx/enable", headers=admin_scope, json={}), 200, "srx enable")
        assert settings.get("srx_enabled") == "1"
        _is(api.post("/api/admin/srx/disable", headers=admin_scope, json={}), 200, "srx disable")
        assert settings.get("srx_enabled") == "0"
    finally:
        settings.set_many({"multi_org_enabled": "1"})
    # #281: this world has multi-org ON, where the retention policy is install-wide and
    # therefore host-only: an org admin's confirmed write is refused (403) ...
    _is(
        api.post(
            "/api/admin/retention/config",
            headers=admin_scope,
            json={"enabled": True, "days": 30, "confirm": True},
        ),
        403,
        "retention config under multi-org",
    )
    # ... and on a single-org install it lands (#280: both keys need confirm).
    settings.set_many({"multi_org_enabled": "0"})
    try:
        _is(
            api.post(
                "/api/admin/retention/config",
                headers=admin_scope,
                json={"enabled": True, "days": 30, "confirm": True},
            ),
            200,
            "retention config",
        )
    finally:
        settings.set_many({"multi_org_enabled": "1"})
    # retention/status stays a docs-scope + is_admin ROLE read (deliberately NOT promoted --
    # a non-destructive status read), so the docs-scope admin key still reaches it.
    st = api.get("/api/admin/retention/status", headers=admin)
    _is(st, 200, "retention status")
    assert st.json()["enabled"] is True and st.json()["days"] == 30, st.text
    # audit verify
    r = api.get("/api/audit/verify", headers=admin)
    _is(r, 200, "audit verify")
    assert r.json()["valid"] is True, r.text
    # lang enable/disable: #479 the enabled-language catalog is install-wide, so under
    # multi-org (this world) it is host-only (403); single-org it still round-trips.
    _is(
        api.post("/api/languages/ja-JP/enable", headers=admin, json={}),
        403,
        "lang enable under multi-org",
    )
    settings.set_many({"multi_org_enabled": "0"})
    try:
        _is(api.post("/api/languages/ja-JP/enable", headers=admin, json={}), 200, "lang enable")
        _is(api.post("/api/languages/ja-JP/disable", headers=admin, json={}), 200, "lang disable")
    finally:
        settings.set_many({"multi_org_enabled": "1"})
    # retention purge-now (switch on, retention armed above) -> 200 removed dict
    _remote_admin(True)
    r = api.post("/api/admin/retention/purge", headers=admin_scope, json={"confirm": "purge"})
    _is(r, 200, "retention purge")
    assert isinstance(r.json().get("removed"), dict), r.text
    # a wrong purge confirm is a clean 400.
    _is(
        api.post("/api/admin/retention/purge", headers=admin_scope, json={"confirm": "nope"}),
        400,
        "purge wrong confirm",
    )
    print(
        "  [10] happy paths: user create/role/disable/enable/invite, routing "
        "add/toggle/delete, budget set/list/delete, lqa set+get, semantic/srx/"
        "retention config, audit verify, lang enable/disable, purge (armed) all "
        "round-trip"
    )


# --------------------------------------------------------------------------- #
def _key_count() -> int:
    with Session(engine) as s:
        return len(s.exec(select(ApiKey.id)).all())


def _conn_count() -> int:
    with Session(engine) as s:
        return len(s.exec(select(Connection.id)).all())


def main() -> None:
    d = _seed()
    _gate_on()
    try:
        api = _client()
        test_cross_org_404(d, api)
        test_admin_mint_refusal(d, api)
        test_remote_admin_gate(d, api)
        test_gdpr_erase(d, api)
        test_last_admin_guard(d, api)
        test_apikey_lifecycle(d, api)
        test_reset_password_no_leak(d, api)
        test_connector_secret_refusal(d, api)
        test_rbac_and_scope(d, api)
        test_happy_paths(d, api)
    finally:
        _gate_off()
    print("R2 ADMIN IDENTITY OK")


if __name__ == "__main__":
    main()
