"""API key expiry + admin-scoped account-security routes (#270 r2-apiauth-10).

A. Optional key expiry
- an expired key is refused EXACTLY like a revoked/unknown key (same 401 body;
  nothing reveals the key existed)
- a not-yet-expired key works; a no-expiry key (the pre-existing shape, i.e. every
  already-issued key after the migration) works
- the org-level apikey_max_age_days setting (blank = no max, the default) caps NEW
  mints at the shared seam: a too-long or missing expiry is a clean refusal on the
  service, the /api mint (400), the /account form (error flash) and the CLI
- mint surfaces show the expiry (CLI human+json, /api body, /account one-time box)
- key listings carry the expiry + an 'expired' state (CLI apikey list in-process)
- CLI --remote with an expired token is refused; a live token still works

B. Account-security changes need the admin scope
- a docs-scoped (MCP) key gets the standard admin-scope refusal on: user create,
  role change, password reset, MFA reset and invite (invite creates users with a
  role, admin included)
- an admin-scoped key (admin role) is still allowed for all five
- revoke-all (admin scope + remote_admin_enabled) behaviour is unchanged

Offline: the CLI --remote arm is forwarded to the in-process TestClient through
_remote._set_transport (no network).
"""

import io
import json as _json
import os
import sys
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timedelta

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(_ROOT)
sys.path.insert(0, _ROOT)
from tests._bootstrap import init_env

init_env("loctms_apikey_expiry_scope_")

from fastapi.testclient import TestClient
from sqlmodel import Session

from app.cli import _remote
from app.cli.__main__ import main as cli_main
from app.db import engine, init_db
from app.main import app
from app.models import ApiKey
from app.services import apikeys, security, settings
from tests._csrf import csrf_for

_SCOPE_MSG = "This endpoint requires an admin-scoped user API key."


def main() -> None:
    init_db()
    security.seed_initial()  # admin/admin + linguist + user1, auth ON
    c = TestClient(app)
    admin = security.get_by_email("admin")
    target = security.get_by_email("user1")

    # =========================================================================
    # A. EXPIRY
    # =========================================================================
    # --- no-expiry key (every pre-existing key keeps this shape) works --------
    _k_never, f_never = apikeys.create_key(admin.id, "no expiry key")
    assert apikeys.verify_key(f_never) is not None, "a no-expiry key must keep working"
    r = c.get("/api/connectors", headers={"X-Loc-Token": f_never})
    assert r.status_code == 200 and r.json()["ok"], (r.status_code, r.text)
    print("  expiry: a no-expiry key (pre-existing shape) still authenticates")

    # --- not-yet-expired key works --------------------------------------------
    _k_fut, f_fut = apikeys.create_key(
        admin.id, "future key", expires_at=datetime.utcnow() + timedelta(days=7)
    )
    assert apikeys.verify_key(f_fut) is not None
    assert c.get("/api/connectors", headers={"X-Loc-Token": f_fut}).status_code == 200
    print("  expiry: a not-yet-expired key authenticates")

    # --- expired key = revoked, exactly ----------------------------------------
    _k_exp, f_exp = apikeys.create_key(
        admin.id, "will expire", expires_at=datetime.utcnow() + timedelta(minutes=5)
    )
    assert apikeys.verify_key(f_exp) is not None, "not expired yet: must work"
    # time passes: the key now reads past its expiry
    with Session(engine) as s:
        row = s.get(ApiKey, _k_exp.id)
        row.expires_at = datetime.utcnow() - timedelta(minutes=1)
        s.add(row)
        s.commit()
    assert apikeys.verify_key(f_exp) is None, "expired key must be refused at the seam"
    r = c.get("/api/connectors", headers={"X-Loc-Token": f_exp})
    assert r.status_code == 401, (r.status_code, r.text)
    assert r.json() == {"ok": False, "error": "unauthorized"}, r.json()
    # BYTE-IDENTICAL refusal to an unknown key: nothing reveals the key existed.
    r2 = c.get("/api/connectors", headers={"X-Loc-Token": "lsu_definitely-not-real"})
    assert r2.status_code == 401 and r2.json() == r.json(), (r2.status_code, r2.json())
    print("  expiry: an expired key 401s with the exact unknown/revoked key shape")

    # --- mint seam: the org-level maximum (apikey_max_age_days) ----------------
    # Default: blank = no maximum -> a no-expiry mint succeeds.
    settings.set_many({"apikey_max_age_days": ""})
    settings.invalidate_cache()
    assert apikeys.max_key_age_days() is None
    apikeys.create_key(admin.id, "uncapped key")
    # Cap set: missing or too-long expiries are refused, a short one passes.
    settings.set_many({"apikey_max_age_days": "30"})
    settings.invalidate_cache()
    assert apikeys.max_key_age_days() == 30
    raised = None
    try:
        apikeys.create_key(admin.id, "capped no-expiry")
    except ValueError as exc:
        raised = str(exc)
    assert raised is not None and "apikey_max_age_days" in raised and "30" in raised, raised
    raised = None
    try:
        apikeys.create_key(
            admin.id, "capped too long", expires_at=datetime.utcnow() + timedelta(days=90)
        )
    except ValueError as exc:
        raised = str(exc)
    assert raised is not None and "apikey_max_age_days" in raised, raised
    _k_ok, f_ok = apikeys.create_key(
        admin.id, "capped fine", expires_at=datetime.utcnow() + timedelta(days=10)
    )
    assert _k_ok.expires_at is not None and apikeys.verify_key(f_ok) is not None
    # A past expiry is refused at mint, even under no cap.
    settings.set_many({"apikey_max_age_days": ""})
    settings.invalidate_cache()
    try:
        apikeys.create_key(
            admin.id, "past expiry", expires_at=datetime.utcnow() - timedelta(days=1)
        )
        assert False, "a past expiry must be refused at mint"
    except ValueError as exc:
        assert "past" in str(exc), str(exc)
    print("  max-age: blank = no max; cap 30 refuses missing/90-day mints, allows 10")

    # --- /api mint: expires_in_days in, expires_at out; cap is a clean 400 ----
    _k_docs, f_docs = apikeys.create_key(admin.id, "docs caller", scopes="api docs")
    r = c.post(
        "/api/apikeys/issue",
        json={"name": "api exp key", "expires_in_days": 5},
        headers={"X-Loc-Token": f_docs},
    )
    assert r.status_code == 200, (r.status_code, r.text)
    body = r.json()
    assert body.get("ok") is True and body.get("api_key", "").startswith("lsu_"), body
    assert body.get("expires_at"), f"the mint response must show the expiry: {body}"
    assert "expires_at" in body and "T" in body["expires_at"], body
    # no expiry requested -> expires_at null
    body = c.post(
        "/api/apikeys/issue", json={"name": "api no exp"}, headers={"X-Loc-Token": f_docs}
    ).json()
    assert body.get("ok") is True and body.get("expires_at") is None, body
    # malformed lifetime -> clean 400
    r = c.post(
        "/api/apikeys/issue",
        json={"name": "bad", "expires_in_days": 0},
        headers={"X-Loc-Token": f_docs},
    )
    assert r.status_code == 400 and "expires_in_days" in r.json().get("error", ""), r.text
    # over the cap -> 400 naming the setting
    settings.set_many({"apikey_max_age_days": "30"})
    settings.invalidate_cache()
    r = c.post(
        "/api/apikeys/issue",
        json={"name": "too long", "expires_in_days": 90},
        headers={"X-Loc-Token": f_docs},
    )
    assert r.status_code == 400 and "apikey_max_age_days" in r.json().get("error", ""), r.text
    settings.set_many({"apikey_max_age_days": ""})
    settings.invalidate_cache()
    print("  /api mint: expires_in_days -> expires_at in the response; cap = clean 400")

    # --- /account form: expiry shown once + cap/malformed refusals flash -------
    csrf_for(c)
    r = c.post(
        "/auth/login",
        data={"email": "admin", "password": "admin", "next": "/"},
        follow_redirects=False,
    )
    assert r.status_code == 303, r.text
    csrf_for(c)  # login rotated the session
    r = c.post(
        "/account/apikeys",
        data={"name": "web exp key", "expires_in_days": "10"},
        follow_redirects=False,
    )
    assert r.status_code == 303 and r.headers["location"].endswith("saved=key"), r.headers
    page = c.get("/account").text
    assert "It expires on" in page, "the one-time display must show the expiry"
    # over the cap: refused, error flash names the setting, NO key created
    settings.set_many({"apikey_max_age_days": "30"})
    settings.invalidate_cache()
    n_before = len(apikeys.list_keys(admin.id))
    csrf_for(c)
    r = c.post(
        "/account/apikeys",
        data={"name": "web too long", "expires_in_days": "60"},
        follow_redirects=False,
    )
    assert r.headers["location"] == "/account", r.headers
    page = c.get("/account").text
    assert "Key not created" in page and "apikey_max_age_days" in page, (
        "the cap refusal must flash on /account naming the setting"
    )
    assert len(apikeys.list_keys(admin.id)) == n_before, "a refused mint must not create a key"
    # malformed lifetime: clean refusal, no traceback
    csrf_for(c)
    r = c.post(
        "/account/apikeys",
        data={"name": "web bad", "expires_in_days": "soon"},
        follow_redirects=False,
    )
    page = c.get("/account").text
    assert "Key not created" in page and "whole number of days" in page, (
        "a malformed lifetime must flash a clean refusal"
    )
    settings.set_many({"apikey_max_age_days": ""})
    settings.invalidate_cache()
    print("  /account form: expiry shown once; cap + malformed lifetime flash clean refusals")

    # --- CLI mint + list: expiry on every arm ----------------------------------
    code, out = _run_cli(["apikey", "issue", "--user", "admin", "--expires-in-days", "5"])
    assert code == 0, out
    assert "expires " in out, f"the human output must show the expiry: {out}"
    code, jout = _run_cli(
        ["--json", "apikey", "issue", "--user", "admin", "--expires-in-days", "5"]
    )
    assert code == 0, jout
    jbody = _json.loads(jout.strip().splitlines()[-1])
    assert jbody["ok"] and jbody["expires_at"], jbody
    code, out = _run_cli(["apikey", "issue", "--user", "admin", "--expires-in-days", "0"])
    assert code != 0 and "whole number of days" in out, out
    # cap enforced on the CLI arm too
    settings.set_many({"apikey_max_age_days": "30"})
    settings.invalidate_cache()
    code, out = _run_cli(["apikey", "issue", "--user", "admin", "--expires-in-days", "90"])
    assert code != 0 and "apikey_max_age_days" in out, out
    settings.set_many({"apikey_max_age_days": ""})
    settings.invalidate_cache()
    print("  CLI mint: --expires-in-days mints + shows the expiry; cap refused cleanly")

    # listing: the expires column shows the date, 'never', and the expired state
    code, out = _run_cli(["apikey", "list", "--user", "admin"])
    assert code == 0, out
    assert "expires" in out, out
    assert "never" in out, "no-expiry keys must list as 'never'"
    assert "(expired)" in out, f"the expired key must carry the expired state: {out}"
    code, jout = _run_cli(["--json", "apikey", "list", "--user", "admin"])
    assert code == 0, jout
    keys = _json.loads(jout.strip().splitlines()[-1])["keys"]
    assert all("expires_at" in k and "expired" in k for k in keys), keys
    print("  CLI list: expires column with date / never / (expired) state")

    # --- CLI --remote arm: an expired token is refused, a live one works -------
    import httpx

    def _mock_transport(client):
        def handler(request):
            path = request.url.path
            if request.url.query:
                path += "?" + request.url.query.decode()
            rr = client.request(
                request.method, path, headers=dict(request.headers), content=request.content
            )
            return httpx.Response(
                rr.status_code,
                content=rr.content,
                headers={"content-type": rr.headers.get("content-type", "")},
            )

        return httpx.MockTransport(handler)

    _k_adm, f_admin = apikeys.create_key(admin.id, "admin scope key", scopes="api docs admin")
    _remote._set_transport(_mock_transport(c))
    try:
        code, out, err = _run_cli_remote(f_exp)
        assert code != 0, f"the expired token must be refused over --remote: {out}{err}"
        assert "unauthorized" in (out + err).lower(), (out, err)
        code, out, err = _run_cli_remote(f_admin)
        assert code == 0, (code, out, err)
        assert "expires" in out and "never" in out, out
    finally:
        _remote._set_transport(None)
    print("  CLI --remote: expired token refused (401), live admin token works")

    # =========================================================================
    # B. ACCOUNT-SECURITY ROUTES REQUIRE THE ADMIN SCOPE
    # =========================================================================
    uid = target.id
    orig_role = security.get_by_email("user1").role
    docs_calls = [
        ("POST", "/api/users", {"email": "docscreate@x.com", "role": "admin"}),
        ("POST", f"/api/users/{uid}/role", {"role": "viewer"}),
        ("POST", f"/api/users/{uid}/reset-password", {"password": "docs-pw-12345"}),
        ("POST", "/api/users/invite", {"email": "docsinvite@x.com", "role": "translator"}),
    ]
    for method, path, payload in docs_calls:
        r = c.request(method, path, json=payload, headers={"X-Loc-Token": f_docs})
        assert r.status_code == 403, (method, path, r.status_code, r.text)
        assert r.json().get("error") == _SCOPE_MSG, (method, path, r.json())
    r = c.request("DELETE", f"/api/users/{uid}/mfa", headers={"X-Loc-Token": f_docs})
    assert r.status_code == 403, (r.status_code, r.text)
    assert r.json().get("error") == _SCOPE_MSG, r.json()
    # the docs key reached NONE of them: no user created, role unchanged, no invite
    assert security.get_by_email("docscreate@x.com") is None
    assert security.get_by_email("docsinvite@x.com") is None
    assert security.get_by_email("user1").role == orig_role, "the role must be untouched"
    print("  admin scope: docs-scoped key 403s on create/role/reset-password/mfa-reset/invite")

    # an admin-scoped key (owner is an admin role) is still allowed on all five
    r = c.post(
        "/api/users",
        json={"email": "scopebob@x.com", "role": "translator"},
        headers={"X-Loc-Token": f_admin},
    )
    assert r.status_code == 200 and r.json().get("ok"), (r.status_code, r.text)
    bob = security.get_by_email("scopebob@x.com")
    assert bob is not None and bob.role == "translator"
    r = c.post(
        f"/api/users/{bob.id}/role", json={"role": "viewer"}, headers={"X-Loc-Token": f_admin}
    )
    assert r.status_code == 200 and r.json().get("new") == "viewer", (r.status_code, r.text)
    r = c.post(
        f"/api/users/{bob.id}/reset-password",
        json={"password": "bob-new-pass-99"},
        headers={"X-Loc-Token": f_admin},
    )
    assert r.status_code == 200 and r.json().get("ok"), (r.status_code, r.text)
    r = c.request("DELETE", f"/api/users/{bob.id}/mfa", headers={"X-Loc-Token": f_admin})
    assert r.status_code == 200 and r.json().get("ok"), (r.status_code, r.text)
    r = c.post(
        "/api/users/invite",
        json={"email": "scopeinvite@x.com", "role": "translator"},
        headers={"X-Loc-Token": f_admin},
    )
    assert r.status_code == 200 and r.json().get("ok"), (r.status_code, r.text)
    print("  admin scope: admin-scoped key allowed on all five account-security routes")

    # --- unchanged: revoke-all keeps its existing admin-scope + switch lock ----
    settings.set_many({"remote_admin_enabled": "1"})
    settings.invalidate_cache()
    _k_t1, _f_t1 = apikeys.create_key(target.id, "target key 1")
    r = c.post(
        "/api/apikeys/revoke-all",
        json={"user": target.email},
        headers={"X-Loc-Token": f_admin},
    )
    assert r.status_code == 200 and r.json().get("ok"), (r.status_code, r.text)
    r = c.post(
        "/api/apikeys/revoke-all",
        json={"user": target.email},
        headers={"X-Loc-Token": f_docs},
    )
    assert r.status_code == 403 and r.json().get("error") == _SCOPE_MSG, (r.status_code, r.text)
    # single-key revoke stays docs-scope + is_admin (unchanged by this fix)
    _k_t2, _f_t2 = apikeys.create_key(target.id, "target key 2")
    kid2 = _k_t2.id
    r = c.post(f"/api/apikeys/{kid2}/revoke", headers={"X-Loc-Token": f_docs})
    assert r.status_code == 200 and r.json().get("ok"), (r.status_code, r.text)
    assert apikeys.verify_key(_f_t2) is None
    settings.set_many({"remote_admin_enabled": "0"})
    settings.invalidate_cache()
    print("  unchanged: revoke-all still admin-scope + switch; single revoke stays docs scope")

    # --- review fix (#270): the cap setting is validated at every door, and an expired
    # key is neither a "live" key in counts nor shown as live in the user detail --------
    for bad in ("abc", "-5", "0", "3651", "1.5", "30 days"):
        try:
            settings.validate_value("apikey_max_age_days", bad)
            assert False, f"{bad!r} must be refused (a garbage cap would silently fail open)"
        except settings.InvalidSettingValue:
            pass
    for good in ("", "1", "30", "3650"):
        settings.validate_value("apikey_max_age_days", good)
    assert "apikey_max_age_days" in settings.SENSITIVE_KEYS
    code, out = _run_cli(["config", "set", "apikey_max_age_days", "abc", "--yes"])
    assert code != 0 and "whole number of days" in out, out
    assert settings.get("apikey_max_age_days") in ("", None), "a refused value must not be stored"
    live_before = apikeys.list_keys(admin.id)
    n_live = sum(1 for k in live_before if k.active and not apikeys.key_is_expired(k))
    assert any(k.active and apikeys.key_is_expired(k) for k in live_before), (
        "fixture: an expired key"
    )
    r = c.get("/api/users", headers={"X-Loc-Token": f_docs})
    row = next(u for u in r.json()["users"] if u["email"] == "admin")
    assert row["active_keys"] == n_live, (row, n_live)
    r = c.get(f"/api/users/{admin.id}", headers={"X-Loc-Token": f_docs})
    shown = {k["id"]: k for k in r.json()["api_keys"]}
    assert shown[_k_exp.id]["expired"] is True and shown[_k_exp.id]["expires_at"], shown
    assert shown[_k_never.id]["expired"] is False and shown[_k_never.id]["expires_at"] is None
    code, jout = _run_cli(["--json", "user", "show", "admin"])
    cli_keys = {k["id"]: k for k in _json.loads(jout.strip().splitlines()[-1])["api_keys"]}
    assert cli_keys[_k_exp.id]["expired"] is True and cli_keys[_k_never.id]["expired"] is False
    code, out = _run_cli(["user", "show", "admin"])
    assert "(expired)" in out and "never" in out, out
    print("  review fix: cap validated + sensitive; active_keys and user detail honour expiry")

    # --- gate fix (#270): the boundary second is expired (<=, never <) -----------------
    assert apikeys.key_is_expired(ApiKey(user_id=admin.id, expires_at=datetime.utcnow()))
    assert not apikeys.key_is_expired(
        ApiKey(user_id=admin.id, expires_at=datetime.utcnow() + timedelta(seconds=30))
    )

    # --- gate fix (#270): a huge / odd lifetime is a clean refusal on every mint surface
    # (an unbounded day count overflowed datetime arithmetic: a 500 on /api and the
    # /account form, a traceback in the CLI) ---------------------------------------------
    for bad in (10**9, 3651, True, 1.5, "abc", "-3", -3, 0):
        r = c.post(
            "/api/apikeys/issue",
            json={"name": "odd", "expires_in_days": bad},
            headers={"X-Loc-Token": f_docs},
        )
        assert r.status_code == 400, (bad, r.status_code, r.text)
        assert "1 to 3650" in r.json().get("error", ""), (bad, r.text)
    for good in (1, "7", 3650):
        r = c.post(
            "/api/apikeys/issue",
            json={"name": "ok", "expires_in_days": good},
            headers={"X-Loc-Token": f_docs},
        )
        assert r.status_code == 200 and r.json()["expires_at"], (good, r.text)
    n_before = len(apikeys.list_keys(admin.id))
    csrf_for(c)
    r = c.post(
        "/account/apikeys",
        data={"name": "web huge", "expires_in_days": "999999999"},
        follow_redirects=False,
    )
    assert r.status_code == 303 and r.headers["location"] == "/account", (r.status_code, r.text)
    page = c.get("/account").text
    assert "Key not created" in page and "1 to 3650" in page, "a huge lifetime must flash"
    assert len(apikeys.list_keys(admin.id)) == n_before, "a refused mint must not create a key"
    code, out = _run_cli(["apikey", "issue", "--user", "admin", "--expires-in-days", "999999999"])
    assert code != 0 and "1 to 3650" in out and "Traceback" not in out, out
    print("  gate fix: huge/odd lifetimes are clean refusals on /api, /account and the CLI")

    # --- gate fix (#270): re-enabling a disabled account needs the admin scope ---------
    # Disable leaves the account's keys active=True (verify_key only skips them while the
    # owner is disabled), so enable REVIVES them, admin-scoped keys included: a docs key
    # must not be able to resurrect an admin credential. Disable stays docs scope.
    victim = security.get_by_email("scopebob@x.com")
    r = c.post(f"/api/users/{victim.id}/role", json={"role": "admin"}, headers=_h(f_admin))
    assert r.status_code == 200, r.text
    _kv, f_victim_admin = apikeys.create_key(victim.id, "victim admin key", "api docs admin")
    assert apikeys.verify_key(f_victim_admin) is not None
    r = c.post(f"/api/users/{victim.id}/disable", headers=_h(f_docs))
    assert r.status_code == 200 and r.json()["active"] is False, r.text
    assert apikeys.verify_key(f_victim_admin) is None, "a disabled owner's key must not work"
    r = c.post(f"/api/users/{victim.id}/enable", headers=_h(f_docs))
    assert r.status_code == 403 and r.json().get("error") == _SCOPE_MSG, (r.status_code, r.text)
    assert security.get_by_email("scopebob@x.com").active is False, "docs key re-enabled a user"
    assert apikeys.verify_key(f_victim_admin) is None, "docs key revived an admin-scoped key"
    r = c.post(f"/api/users/{victim.id}/enable", headers=_h(f_admin))
    assert r.status_code == 200 and r.json()["active"] is True, r.text
    print("  gate fix: enable needs the admin scope (docs key refused, admin key allowed)")

    # --- gate fix (#270): init --issue-key with the cap already set mints at the cap ---
    # (it used to create the admin and THEN refuse the no-expiry key: a half-seeded
    # install whose corrected retry no-ops because a user now exists)
    # 2026-10-05 (main-red fix): this used to fake an empty install by patching
    # security.user_count to 0 on THIS suite's populated DB. Since #414, seed_admin's
    # create_user(first_account_only=True) re-checks for an existing account under the
    # user-mutation lock, so the fake is (correctly) seen through and the call returns
    # 'exists'. The check now runs on a REAL empty install in a child process with its own
    # throwaway data dir, which proves more than the patch did (no faked counter), and the
    # populated-DB call is asserted to be the #414 no-op (no admin, no key minted).
    settings.set_many({"apikey_max_age_days": "30"})
    settings.invalidate_cache()
    try:
        _u, full_busy, status_busy = security.seed_admin(
            "initcap@x.com", "init-cap-password-1", name="Init Cap", issue_key=True
        )
    finally:
        settings.set_many({"apikey_max_age_days": ""})
        settings.invalidate_cache()
    assert (status_busy, full_busy) == ("exists", None), (status_busy, full_busy)
    assert security.get_by_email("initcap@x.com") is None, "seed_admin created a 2nd admin"
    _init_cap_on_empty_install()
    print("  gate fix: init --issue-key under a set cap mints a key at the cap, never half-seeds")

    print("APIKEY EXPIRY + ADMIN SCOPE OK")


_INIT_CAP_CHILD = """
from datetime import datetime, timedelta
from app.db import init_db
from app.services import apikeys, security, settings
init_db()
assert security.user_count() == 0, security.user_count()
settings.set_many({"apikey_max_age_days": "30"})
settings.invalidate_cache()
u, full, status = security.seed_admin(
    "initcap@x.com", "init-cap-password-1", name="Init Cap", issue_key=True
)
assert status == "created" and full and u is not None, (status, full)
k = apikeys.verify_key(full)
assert k is not None and k.expires_at is not None, k
assert k.expires_at <= datetime.utcnow() + timedelta(days=30), k.expires_at
assert k.expires_at > datetime.utcnow() + timedelta(days=29), k.expires_at
print("OK")
"""


def _init_cap_on_empty_install():
    """Run the init-under-cap check on a fresh, EMPTY install in a child process."""
    import subprocess
    import tempfile

    data_dir = tempfile.mkdtemp(prefix="loctms_apikey_initcap_")
    env = dict(os.environ)
    env["LOC_TMS_DATA_DIR"] = data_dir
    try:
        r = subprocess.run(
            [sys.executable, "-c", "import os, sys\n" + _INIT_CAP_CHILD],
            env=env,
            cwd=_ROOT,
            capture_output=True,
            text=True,
            timeout=300,
        )
    finally:
        import shutil

        shutil.rmtree(data_dir, ignore_errors=True)
    assert r.returncode == 0 and "OK" in r.stdout, (
        f"init-cap child failed (exit {r.returncode}):\nSTDOUT:{r.stdout}\nSTDERR:{r.stderr}"
    )


def _h(token):
    return {"X-Loc-Token": token}


def _run_cli(argv):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        try:
            code = cli_main(argv)
        except SystemExit as exc:
            code = exc.code if isinstance(exc.code, int) else 2
    return code, out.getvalue() + err.getvalue()


def _run_cli_remote(token):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        try:
            code = cli_main(["--remote", "http://127.0.0.1", "--token", token, "apikey", "list"])
        except SystemExit as exc:
            code = exc.code if isinstance(exc.code, int) else 2
    lines = [
        line for line in err.getvalue().splitlines() if not line.startswith("warning: --token")
    ]
    return code, out.getvalue(), "\n".join(lines)


if __name__ == "__main__":
    main()
