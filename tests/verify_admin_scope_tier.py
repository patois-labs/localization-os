"""docs-scope-privilege remediation: the install-global admin-config tier.

Proves the fix that promoted the install-global config-MUTATING admin routes off the
weak docs-scope + is_admin-ROLE gate onto the R3 three-lock preamble (admin SCOPE +
is_admin role + remote_admin_enabled), the SAME locks POST /api/admin/config and
api_retention_purge already carry, and routed their setting writes through the shared
validation spine (_write_validated_settings): REMOTE_IMMUTABLE-key law, unknown-key
refusal, sensitive-key confirm, secret-masked audit.

The HIGH it closes: a `docs`-scoped API key with the admin ROLE could arm the retention
daemon (install-wide, cross-tenant, irreversible deletion) via /api/admin/retention/config,
run install-wide PAID embedding backfills, and install an install-global SRX ruleset --
all from the WEAKEST scope, bypassing the switch + validation spine its siblings enforce.

For EACH promoted route this asserts, as a LAW that fails if the fix is reverted:
  (a) a docs-scope key -- EVEN with the admin role -- is REJECTED on SCOPE (403), the
      pre-fix regression (pre-fix it was 200 / a role-403);
  (b) an admin-scope key SUCCEEDS (switch on);
  (c) an admin-scope key is BLOCKED (404, non-enumerable) when remote_admin_enabled is off;
  (e) a successful write emits an audit row.
Plus, through the shared spine (POST /api/admin/config, which now calls the same helper):
  (d) an IMMUTABLE key and an UNKNOWN key are each refused (400) before any write.
And (f): config reads retain all three R3 locks.
The deliberately-docs retention STATUS read still
accepts one.

ASCII-only; standalone one-process-per-suite script. Final line: "ADMIN SCOPE TIER OK".
"""

import base64
import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(_ROOT)
sys.path.insert(0, _ROOT)
from tests._bootstrap import init_env

init_env("loctms_adminscopetier_")

import logging

for _n in ("httpx", "httpcore"):
    logging.getLogger(_n).setLevel(logging.WARNING)

from fastapi.testclient import TestClient
from sqlmodel import Session, select

from app.db import engine, init_db
from app.models import AuditLog, Customer, Organization, Role, User
from app.services import apikeys, security, settings

_SCOPE_MSG = "This endpoint requires an admin-scoped user API key."
_NOT_FOUND_MSG = "not found"

# A valid SRX payload for the validate/upload success cases (the shipped ruleset).
from app.services import srx as _srx

_VALID_SRX_B64 = base64.b64encode(_srx._DEFAULT_SRX_PATH.read_bytes()).decode()

# Every route the fix promoted docs-scope -> admin-scope + remote-admin lock, with a body
# that would SUCCEED for an admin-scope key on a switched-on install and the success code.
_PROMOTED = [
    ("/api/admin/srx/enable", {}, 200),
    ("/api/admin/srx/disable", {}, 200),
    ("/api/admin/srx/validate", {"srx_b64": _VALID_SRX_B64}, 200),
    ("/api/admin/srx/upload", {"srx_b64": _VALID_SRX_B64}, 200),
    # #280: both retention keys are security-sensitive, so a success carries confirm.
    ("/api/admin/retention/config", {"enabled": True, "days": 7, "confirm": True}, 200),
]


def _gate_on() -> None:
    settings.set_many({"security_auth_required": "1"})
    settings.invalidate_cache()


def _gate_off() -> None:
    settings.set_many({"security_auth_required": "0", "remote_admin_enabled": "0"})
    settings.invalidate_cache()


def _remote_admin(on: bool) -> None:
    settings.set_many({"remote_admin_enabled": "1" if on else "0"})
    settings.invalidate_cache()


def _h(raw: str) -> dict:
    return {"X-Loc-Token": raw}


def _is(r, code, what) -> None:
    assert r.status_code == code, f"{what}: expected {code}, got {r.status_code} ({r.text[:200]})"


def _err(r, msg, what) -> None:
    got = r.json().get("error")
    assert got == msg, f"{what}: expected error {msg!r}, got {got!r} ({r.text[:200]})"


def _audit_count() -> int:
    with Session(engine) as s:
        return len(s.exec(select(AuditLog.id)).all())


def _seed():
    init_db()
    with Session(engine) as s:
        org = Organization(name="Org A")
        s.add(org)
        s.commit()
        s.refresh(org)
        oa = org.id
    u = security.create_user("admin@x.com", "admin@x.com", Role.ADMIN, "tier-password-1234")
    with Session(engine) as s:
        row = s.get(User, u.id)
        row.org_id = oa
        s.add(row)
        s.commit()
    admin_id = u.id
    # A NON-admin (translator) docs key, to prove the scope gate fires BEFORE the role gate.
    tr = security.create_user("tr@x.com", "tr@x.com", Role.TRANSLATOR, "tier-password-1234")
    with Session(engine) as s:
        row = s.get(User, tr.id)
        row.org_id = oa
        s.add(row)
        s.commit()

    def _key(uid, scopes):
        _row, raw = apikeys.create_key(uid, "tier-test", scopes=scopes)
        return raw

    external = security.create_user(
        "external@x.com", "external", Role.TRANSLATOR, "tier-password-1234"
    )
    with Session(engine) as session:
        customer = Customer(name="External customer", org_id=oa)
        session.add(customer)
        session.commit()
        row = session.get(User, external.id)
        row.org_id = oa
        row.customer_id = customer.id
        session.add(row)
        session.commit()

    return {
        # admin_scope = the fix's required grant; admin_docs = admin ROLE but docs SCOPE
        # (the exact key that COULD arm these routes pre-fix); translator_docs = below role.
        "admin_scope": _key(admin_id, "api docs admin"),
        "admin_docs": _key(admin_id, "api docs"),
        "translator_docs": _key(tr.id, "api docs"),
        "translator_admin": _key(tr.id, "api docs admin"),
        "external_docs": _key(external.id, "api docs"),
        "external_admin": _key(external.id, "api docs admin"),
    }


def _client():
    from app.main import app

    return TestClient(app)


def test_promoted_routes(keys, api) -> None:
    admin_scope = _h(keys["admin_scope"])
    admin_docs = _h(keys["admin_docs"])
    translator_docs = _h(keys["translator_docs"])

    for path, body, ok_code in _PROMOTED:
        # (a) docs-scope key, EVEN with the admin role, is rejected on SCOPE (403). This is
        # the pre-fix regression: the route used to accept a docs key (200) OR 403 on the
        # ROLE string; post-fix the SCOPE gate (_require_admin_scope) fires first.
        _remote_admin(False)
        r = api.post(path, headers=admin_docs, json=body)
        _is(r, 403, f"{path}: admin-ROLE docs key rejected on scope")
        _err(r, _SCOPE_MSG, f"{path}: admin-ROLE docs key")
        # a below-role docs key is rejected the same way (scope is checked before role).
        r = api.post(path, headers=translator_docs, json=body)
        _is(r, 403, f"{path}: translator docs key rejected on scope")
        _err(r, _SCOPE_MSG, f"{path}: translator docs key")

        # (c) admin-SCOPE key, but remote_admin_enabled OFF -> 404 (non-enumerable, the
        # kill-switch law), NOT a 200 -- the write must never land with the switch off.
        r = api.post(path, headers=admin_scope, json=body)
        _is(r, 404, f"{path}: admin-scope blocked with remote-admin off")
        _err(r, _NOT_FOUND_MSG, f"{path}: admin-scope, switch off")

        # (b)+(e) admin-SCOPE key + switch ON -> success, and an audit row is written.
        _remote_admin(True)
        before = _audit_count()
        r = api.post(path, headers=admin_scope, json=body)
        _is(r, ok_code, f"{path}: admin-scope success (switch on)")
        assert _audit_count() > before, f"{path}: no audit row written on success"

    print(
        "  [1] promoted routes: docs key (admin role AND translator) 403 on SCOPE; "
        "admin-scope 404 with the switch off; admin-scope 200/202 + audit row with it on"
    )


def test_spine_refuses_immutable_and_unknown(keys, api) -> None:
    # (d) The retention/semantic/srx writes now flow through the SAME validation spine as
    # POST /api/admin/config (_write_validated_settings). Prove the spine's two hard laws
    # through that route (the shared helper): an IMMUTABLE key and an UNKNOWN key are each
    # refused (400) before any write, with no confirm override.
    admin_scope = _h(keys["admin_scope"])
    _remote_admin(True)
    r = api.post(
        "/api/admin/config",
        headers=admin_scope,
        json={"settings": {"remote_admin_enabled": "1"}, "confirm": True},
    )
    _is(r, 400, "spine: immutable key refused")
    assert "LOCAL-ONLY" in r.json().get("error", ""), r.text
    r = api.post(
        "/api/admin/config",
        headers=admin_scope,
        json={"settings": {"no_such_setting_key": "x"}},
    )
    _is(r, 400, "spine: unknown key refused")
    assert "unknown setting key" in r.json().get("error", ""), r.text
    print(
        "  [2] shared spine: an immutable key (remote_admin_enabled) and an unknown key are "
        "each 400'd before any write -- the same helper the promoted routes now call"
    )


def test_no_other_route_regressed(keys, api) -> None:
    # (f) The routes NOT promoted did not silently move scope.
    admin_scope = _h(keys["admin_scope"])
    admin_docs = _h(keys["admin_docs"])
    for enabled in (False, True):
        _remote_admin(enabled)
        for path in ("/api/admin/config", "/api/admin/ldap"):
            for key in (
                "admin_docs",
                "translator_docs",
                "translator_admin",
                "external_docs",
                "external_admin",
            ):
                response = api.get(path, headers=_h(keys[key]))
                _is(response, 403, f"{path}: {key}, remote_admin={enabled}")
            response = api.get(path, headers=admin_scope)
            _is(response, 200 if enabled else 404, f"{path}: admin, remote_admin={enabled}")
    # The retention STATUS read was DELIBERATELY left docs-scope + is_admin role (a
    # non-destructive read), so a docs-scope admin key still reaches it (did not tighten).
    r = api.get("/api/admin/retention/status", headers=admin_docs)
    _is(r, 200, "retention status still docs-scope readable")
    # ...and a below-role docs key is still refused there on ROLE (the role gate intact).
    r = api.get("/api/admin/retention/status", headers=_h(keys["translator_docs"]))
    _is(r, 403, "retention status still role-gated for a non-admin")
    print(
        "  [3] LDAP and config retain full R3 gates; retention STATUS "
        "stays a docs-scope + is_admin-role read (intentionally not promoted)"
    )


def main() -> None:
    keys = _seed()
    _gate_on()
    try:
        api = _client()
        test_promoted_routes(keys, api)
        test_spine_refuses_immutable_and_unknown(keys, api)
        test_no_other_route_regressed(keys, api)
    finally:
        _gate_off()
    print("ADMIN SCOPE TIER OK")


if __name__ == "__main__":
    main()
