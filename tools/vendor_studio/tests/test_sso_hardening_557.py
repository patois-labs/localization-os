"""Issue #557 (and #516 item 3): SSO handoff hardening. A signed role claim mapped with the
API-key role table, audience, exp cap, single-use jti, login-CSRF Origin gate, non-ASCII
fail-closed, org scope under multi-org, and the periodic role re-check."""

from __future__ import annotations

import time

import pytest
from fastapi.testclient import TestClient
from vstudio import security
from vstudio.config import settings


@pytest.fixture(autouse=True)
def _fresh(env):
    security.reset_sso_replay_cache()
    yield
    security.reset_sso_replay_cache()


def _client() -> TestClient:
    from vstudio.app import app

    return TestClient(app)


def _post(token, headers=None, c=None):
    c = c or _client()
    r = c.post("/sso", data={"token": token}, headers=headers or {}, follow_redirects=False)
    return c, r


def _role(c):
    vals = [ck.value for ck in c.cookies.jar if ck.name == security.COOKIE_NAME]
    sess = security.decode_session(vals[-1]) if vals else None
    return (sess or {}).get("role")


# --- role claim --------------------------------------------------------------
@pytest.mark.parametrize(
    ("main_role", "studio_role"),
    [
        ("admin", "PM"),
        ("manager", "PM"),
        ("reviewer", "Reviewer"),
        ("linguist", "Reviewer"),
        ("translator", "Translator"),
    ],
)
def test_sso_session_role_comes_from_the_signed_claim(env, main_role, studio_role):
    c, r = _post(security.mint_sso_token("u", role=main_role))
    assert r.status_code == 303, r.text
    assert _role(c) == studio_role


@pytest.mark.parametrize("role", [None, "", "viewer", "vendor", "ADMIN", 7, ["admin"]])
def test_sso_without_a_usable_role_claim_fails_closed(env, role):
    c, r = _post(security.mint_sso_token("u", role=role))
    assert r.status_code == 403
    assert _role(c) is None


def test_sso_role_claim_cannot_be_edited_without_the_secret(env):
    tok = security.mint_sso_token("u", role="translator")
    _body, _, sig = tok.partition(".")
    forged = security.mint_sso_token("u", role="admin", secret="other-secret")
    fbody, _, _ = forged.partition(".")
    c, r = _post(fbody + "." + sig)
    assert r.status_code == 403
    assert _role(c) is None


# --- audience, exp cap, replay ----------------------------------------------
@pytest.mark.parametrize("aud", [None, "somewhere-else", ""])
def test_sso_requires_the_vendor_studio_audience(env, aud):
    _c, r = _post(security.mint_sso_token("u", aud=aud))
    assert r.status_code == 403


def test_sso_exp_is_capped(env):
    _c, r = _post(security.mint_sso_token("u", ttl=3600))
    assert r.status_code == 403
    _c, r = _post(security.mint_sso_token("u", ttl=60))
    assert r.status_code == 303


def test_sso_exp_must_be_finite(env):
    """json.loads accepts NaN/Infinity; NaN would pass the cap and never expire from the
    replay cache."""
    import base64
    import hashlib
    import hmac
    import json

    for bad in (float("nan"), float("inf"), float("-inf")):
        payload = {
            "user": "u",
            "exp": bad,
            "aud": "vendor-studio",
            "jti": "jn",
            "role": "admin",
            "org": None,
            "mo": False,
        }
        body = base64.urlsafe_b64encode(json.dumps(payload).encode()).rstrip(b"=").decode()
        sig = hmac.new(b"test-sso-secret", body.encode(), hashlib.sha256).hexdigest()
        _c, r = _post(body + "." + sig)
        assert r.status_code == 403, bad
    assert not security._SEEN_JTI


def test_sso_token_is_single_use(env):
    tok = security.mint_sso_token("u")
    _c, r = _post(tok)
    assert r.status_code == 303
    c2, r2 = _post(tok)
    assert r2.status_code == 403
    assert _role(c2) is None


def test_sso_token_without_jti_is_refused(env):
    _c, r = _post(security.mint_sso_token("u", jti=""))
    assert r.status_code == 403


def test_a_failed_login_does_not_burn_the_jti(env):
    """A request refused by the Origin gate must not consume the token's jti."""
    tok = security.mint_sso_token("u")
    _, bad = _post(tok, headers={"origin": "https://evil.example"})
    assert bad.status_code == 403
    _, ok = _post(tok)
    assert ok.status_code == 303


# --- non-ASCII fails closed --------------------------------------------------
@pytest.mark.parametrize("token", ["é.x", "abc.é", "éé.éé", "aéb.c"])
def test_sso_non_ascii_token_is_a_403_not_a_500(env, token):
    _c, r = _post(token)
    assert r.status_code == 403, r.status_code
    assert security.verify_sso_token(token) is None


# --- login CSRF (Origin) -----------------------------------------------------
def test_sso_cross_site_origin_is_refused(env):
    c, r = _post(security.mint_sso_token("u"), headers={"origin": "https://evil.example"})
    assert r.status_code == 403
    assert _role(c) is None
    c, r = _post(security.mint_sso_token("u"), headers={"origin": "null"})
    assert r.status_code == 403


def test_sso_from_the_localization_os_origin_is_accepted(env):
    _c, r = _post(security.mint_sso_token("u"), headers={"origin": "https://loc.example.test"})
    assert r.status_code == 303


def test_sso_from_this_studios_own_origin_is_accepted(env):
    _c, r = _post(security.mint_sso_token("u"), headers={"origin": "http://testserver"})
    assert r.status_code == 303


def test_sso_cross_site_fetch_metadata_without_origin_is_refused(env):
    _c, r = _post(security.mint_sso_token("u"), headers={"sec-fetch-site": "cross-site"})
    assert r.status_code == 403


# --- org scope ---------------------------------------------------------------
def test_sso_multi_org_without_a_pinned_org_is_refused(env):
    _c, r = _post(security.mint_sso_token("u", org=3, multi_org=True))
    assert r.status_code == 403


def test_sso_multi_org_pinned_org_must_match(env, monkeypatch):
    monkeypatch.setenv("VSTUDIO_ORG_ID", "3")
    settings.reload()
    _c, r = _post(security.mint_sso_token("u", org=3, multi_org=True))
    assert r.status_code == 303
    _c, r = _post(security.mint_sso_token("u", org=4, multi_org=True))
    assert r.status_code == 403
    _c, r = _post(security.mint_sso_token("u", org=None, multi_org=True))
    assert r.status_code == 403


def test_sso_unpinned_single_tenant_still_works(env):
    _c, r = _post(security.mint_sso_token("u", org=None, multi_org=False))
    assert r.status_code == 303


def test_sso_token_missing_the_multi_org_flag_is_treated_as_multi_org(env):
    import base64
    import hashlib
    import hmac
    import json

    payload = {
        "user": "u",
        "exp": int(time.time()) + 60,
        "aud": "vendor-studio",
        "jti": "j1",
        "role": "admin",
    }
    body = base64.urlsafe_b64encode(json.dumps(payload).encode()).rstrip(b"=").decode()
    sig = hmac.new(b"test-sso-secret", body.encode(), hashlib.sha256).hexdigest()
    _c, r = _post(body + "." + sig)
    assert r.status_code == 403


# --- API-key path under multi-org -------------------------------------------
def _key_login(monkeypatch, body):
    from test_batch_vendor_1005 import _login

    return _login(monkeypatch, body)


def test_apikey_multi_org_without_a_pinned_org_is_refused(env, monkeypatch):
    _c, r = _key_login(monkeypatch, {"ok": True, "role": "admin", "multi_org": True, "org_id": 2})
    assert r.status_code == 403


def test_apikey_multi_org_pinned_org_must_match(env, monkeypatch):
    monkeypatch.setenv("VSTUDIO_ORG_ID", "2")
    settings.reload()
    _c, r = _key_login(monkeypatch, {"ok": True, "role": "admin", "multi_org": True, "org_id": 2})
    assert r.status_code == 303
    _c, r = _key_login(monkeypatch, {"ok": True, "role": "admin", "multi_org": True, "org_id": 9})
    assert r.status_code == 403
    # a main app that does not state the org (an old build) cannot satisfy a pin
    _c, r = _key_login(monkeypatch, {"ok": True, "role": "admin"})
    assert r.status_code == 403


# --- periodic role re-check --------------------------------------------------
def _session_cookie_client(role_at):
    from vstudio.app import app

    c = TestClient(app)
    c.cookies.set(
        security.COOKIE_NAME,
        security.encode_session(
            {"authed": True, "user": "u", "name": "u", "role": "PM", "role_at": role_at}
        ),
    )
    return c


def test_an_old_session_must_sign_in_again_so_the_role_is_reread(env, monkeypatch):
    monkeypatch.setenv("VSTUDIO_ROLE_RECHECK_SECONDS", "600")
    settings.reload()
    fresh = _session_cookie_client(int(time.time()) - 30)
    assert fresh.get("/api/wirelog").status_code != 404
    stale = _session_cookie_client(int(time.time()) - 601)
    assert stale.get("/api/wirelog").status_code == 404


def test_persona_switch_does_not_refresh_the_role_stamp(env, monkeypatch):
    monkeypatch.setenv("VSTUDIO_ROLE_RECHECK_SECONDS", "600")
    settings.reload()
    old = int(time.time()) - 300
    c = _session_cookie_client(old)
    r = c.post("/persona", data={"role": "Reviewer"}, follow_redirects=False)
    vals = [ck.value for ck in c.cookies.jar if ck.name == security.COOKIE_NAME]
    sess = security.decode_session(vals[-1])
    assert r.status_code == 303 and sess["role_at"] == old


def test_a_session_without_a_role_stamp_is_refused(env):
    raw = security._ser().dumps({"authed": True, "user": "u", "role": "PM"})
    assert security.decode_session(raw) is None


@pytest.mark.parametrize("bad", ["-1", "-3600", "soon"])
def test_bad_role_recheck_config_keeps_the_check_on(env, monkeypatch, bad):
    # Gate finding: a negative value used to clamp to 0, which turned the re-check OFF.
    monkeypatch.setenv("VSTUDIO_ROLE_RECHECK_SECONDS", bad)
    settings.reload()
    assert settings.role_recheck_seconds == 3600
    raw = security._ser().dumps({"authed": True, "user": "u", "role": "PM"})
    assert security.decode_session(raw) is None


def test_role_recheck_can_be_turned_off(env, monkeypatch):
    monkeypatch.setenv("VSTUDIO_ROLE_RECHECK_SECONDS", "0")
    settings.reload()
    raw = security._ser().dumps({"authed": True, "user": "u", "role": "PM"})
    assert security.decode_session(raw) is not None
