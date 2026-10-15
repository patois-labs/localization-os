"""#753 MCP Phase C2: the opt-in MCP OAuth authorization server (app/services/mcp_as.py,
app/web/mcp_oauth_as.py) and the MCP server's OAuth resource-server gate
(integrations/mcp-server/oauth_rs.py). Fully offline.

  1  off by default: no metadata, no token endpoint, an lso_ token is a 401 on /api, the
     settings are host-only (REMOTE_IMMUTABLE) and confirm-to-set, validators refuse junk
  2  switched on but not ready: every missing piece is named, metadata stays 404
  3  RFC 8414 metadata (S256, iss parameter, CIMD supported, no DCR while it is off)
  4  authorize: unknown client / unregistered redirect never redirect; PKCE, resource and
     scope errors redirect back WITH iss; a signed-out person goes through sign-in with the
     request intact; the consent page shows the client and the redirect host (localhost warning)
  5  code exchange: PKCE, client and redirect binding, iss on the response, no-store, one-time
     code (a reused code revokes the grant), narrowed scopes
  6  token exchange (RFC 8693) only for the resource server; the lso_ token works on /api,
     the MCP (lsm_) token does not; audience binding follows mcp_oauth_resource; an OAuth
     identity cannot mint API keys
  7  refresh rotation; a reused refresh token revokes the grant
  8  revocation: own grant over /api and the web page, a foreign id is a 404, account
     revoke-all (disable / erasure path) kills every grant
  9  CIMD safety: client_id mismatch, private address, host trust list, non-public client
 10  dynamic registration only when switched on; stateless signed client ids; loopback port
 11  the MCP server gate: protected resource metadata, 401 challenge with resource_metadata and
     scope, invalid token, 403 insufficient_scope naming every needed scope, the client token is
     swapped for the /api token (never passed through), lsu_ keys pass unchanged, the real
     listener wires the gate only when configured and refuses a half configuration

ASCII-only prints; final line is 'verify_mcp_oauth_as OK'.
"""

import asyncio
import base64
import hashlib
import json
import os
import re
import secrets
import sys
from urllib.parse import parse_qs, urlencode, urlsplit

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(_ROOT)
sys.path.insert(0, _ROOT)
from tests._bootstrap import init_env

init_env("loctms_mcpoauthas_")
os.environ["LOC_STUDIO_BASE_URL"] = "http://testserver"
for _k in (
    "LOC_STUDIO_MCP_OAUTH_RESOURCE",
    "LOC_STUDIO_MCP_OAUTH_ISSUER",
    "LOC_STUDIO_MCP_RS_SECRET",
):
    os.environ.pop(_k, None)

import httpx
from fastapi.testclient import TestClient

from app.db import init_db
from app.main import app
from app.services import apikeys, egress, mcp_as, security, settings
from tests._csrf import csrf_for

PASSWORD = "Atlas meadow copper 2026!"  # pregate: allow-secret disposable fixture
ISSUER = "https://tms.example.com"
RESOURCE = "https://mcp.example.com/mcp"
CLIENT = "https://client.example/oauth/meta.json"
REDIRECT = "https://client.example/callback"
LOOPBACK = "http://127.0.0.1:33418/callback"


def b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode().rstrip("=")


def pkce():
    verifier = b64url(secrets.token_bytes(40))
    return verifier, b64url(hashlib.sha256(verifier.encode()).digest())


class Meta:
    """Client ID Metadata Documents served offline."""

    def __init__(self):
        self.docs = {
            CLIENT: {
                "client_id": CLIENT,
                "client_name": "Example Agent",
                "redirect_uris": [REDIRECT, "http://127.0.0.1/callback"],
                "token_endpoint_auth_method": "none",
            }
        }
        self.fetches = []

    def handle(self, req):
        url = str(req.url)
        self.fetches.append(url)
        doc = self.docs.get(url)
        if doc is None:
            return httpx.Response(404)
        return httpx.Response(200, json=doc, headers={"Cache-Control": "no-store"})


def authorize_url(client_id=CLIENT, redirect=REDIRECT, challenge="", **extra):
    q = {
        "response_type": "code",
        "client_id": client_id,
        "redirect_uri": redirect,
        "state": "st-1",
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "resource": RESOURCE,
        "scope": "api docs keys",
    }
    q.update(extra)
    q = {k: v for k, v in q.items() if v is not None}
    return "/oauth/authorize?" + urlencode(q)


def qs(url):
    return {k: v[0] for k, v in parse_qs(urlsplit(url).query).items()}


def rs_basic(secret):
    return "Basic " + base64.b64encode(f"loc-os-mcp-server:{secret}".encode()).decode()


def main() -> None:
    init_db()
    tc = TestClient(app)
    meta = Meta()
    mcp_as.set_test_transport(httpx.MockTransport(meta.handle))
    egress.set_test_resolver(lambda host: ["93.184.216.34"])

    # ---- (1) off by default -------------------------------------------------------------
    assert settings.get("mcp_oauth_enabled") == "0"
    assert tc.get("/.well-known/oauth-authorization-server").status_code == 404
    assert tc.post("/oauth/token", data={"grant_type": "x"}).status_code == 404
    assert tc.post("/oauth/register", json={}).status_code == 404
    assert tc.get("/api/mcp-oauth/grants", headers={"X-Loc-Token": "lso_x"}).status_code == 401
    for k in (
        "mcp_oauth_enabled",
        "mcp_oauth_resource",
        "mcp_oauth_dcr_enabled",
        "mcp_oauth_cimd_hosts",
        "mcp_oauth_rs_secret_hash",
    ):
        assert k in settings.REMOTE_IMMUTABLE_KEYS and k in settings.SENSITIVE_KEYS, k
    for k, bad in (
        ("mcp_oauth_resource", "http://mcp.example.com/mcp"),
        ("mcp_oauth_resource", "https://mcp.example.com/mcp#x"),
        ("mcp_oauth_resource", "https://u:p@mcp.example.com/mcp"),
        ("mcp_oauth_enabled", "yes"),
        ("mcp_oauth_cimd_hosts", "bad host!"),
        ("mcp_oauth_rs_secret_hash", "plaintext-secret"),
    ):
        try:
            settings.validate_value(k, bad)
            raise AssertionError(f"{k}={bad!r} must be refused")
        except settings.InvalidSettingValue:
            pass
    settings.validate_value("mcp_oauth_resource", "http://localhost:8765/mcp")
    assert settings.is_secret_key("mcp_oauth_rs_secret_hash")
    print("  (1) off by default; settings host-only, confirm-to-set and validated")

    # ---- (2) switched on, not ready ------------------------------------------------------
    admin = security.create_user("ada-admin@example.test", "Ada", "admin", PASSWORD)
    other = security.create_user("bo-linguist@example.test", "Bo", "linguist", PASSWORD)
    settings.set_many({"mcp_oauth_enabled": "1"})
    st = mcp_as.status()
    assert st["enabled"] and not st["active"], st
    text = " | ".join(st["problems"])
    for needle in (
        "sign-in is off",
        "app_base_url is not set",
        "mcp_oauth_resource",
        "resource-server secret",
    ):
        assert needle in text, (needle, text)
    assert tc.get("/.well-known/oauth-authorization-server").status_code == 404
    settings.set_many(
        {
            "security_auth_required": "1",
            "app_base_url": ISSUER + "/tms",
            "mcp_oauth_resource": RESOURCE,
        }
    )
    assert any("has a path" in p for p in mcp_as.problems()), mcp_as.problems()
    settings.set_many({"app_base_url": ISSUER})
    rs_secret = mcp_as.generate_rs_secret(actor="test")
    assert rs_secret.startswith("lsrs_")
    assert (
        settings.get("mcp_oauth_rs_secret_hash") == hashlib.sha256(rs_secret.encode()).hexdigest()
    )
    settings.invalidate_cache()
    assert mcp_as.active(), mcp_as.problems()
    print("  (2) not ready until sign-in, https root issuer, resource and RS secret are set")

    # ---- (3) metadata ---------------------------------------------------------------------
    r = tc.get("/.well-known/oauth-authorization-server")
    assert r.status_code == 200, r.status_code
    m = r.json()
    assert m["issuer"] == ISSUER
    assert m["authorization_endpoint"] == ISSUER + "/oauth/authorize"
    assert m["token_endpoint"] == ISSUER + "/oauth/token"
    assert m["code_challenge_methods_supported"] == ["S256"]
    assert m["authorization_response_iss_parameter_supported"] is True
    assert m["client_id_metadata_document_supported"] is True
    assert m["token_endpoint_auth_methods_supported"] == ["none"]
    assert set(m["scopes_supported"]) == {"api", "docs", "keys"}
    assert "registration_endpoint" not in m
    print("  (3) RFC 8414 metadata: S256, iss parameter, CIMD; no DCR while off")

    # ---- (4) authorize --------------------------------------------------------------------
    verifier, challenge = pkce()
    r = tc.get(
        authorize_url(client_id="https://client.example/nope.json", challenge=challenge),
        follow_redirects=False,
    )
    assert r.status_code == 400 and "location" not in r.headers, r.status_code
    r = tc.get(
        authorize_url(redirect="https://evil.example/cb", challenge=challenge),
        follow_redirects=False,
    )
    assert r.status_code == 400 and "location" not in r.headers
    r = tc.get(authorize_url(challenge=""), follow_redirects=False)
    loc = r.headers.get("location", "")
    assert r.status_code == 303 and loc.startswith(REDIRECT), (r.status_code, loc)
    assert qs(loc)["error"] == "invalid_request" and qs(loc)["iss"] == ISSUER
    assert qs(loc)["state"] == "st-1"
    r = tc.get(
        authorize_url(challenge=challenge, resource="https://other.example/mcp"),
        follow_redirects=False,
    )
    assert qs(r.headers["location"])["error"] == "invalid_target"
    r = tc.get(authorize_url(challenge=challenge, scope="api admin"), follow_redirects=False)
    assert qs(r.headers["location"])["error"] == "invalid_scope"
    # signed out: through sign-in, the signed request survives in `next`
    r = tc.get(authorize_url(challenge=challenge, scope="api docs"), follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"].startswith("/auth/login?next="), r.headers
    nxt = qs(r.headers["location"])["next"]
    assert nxt.startswith("/oauth/consent?r=")
    csrf_for(tc)
    r = tc.post(
        "/auth/login",
        data={"email": admin.email, "password": PASSWORD, "next": nxt},
        follow_redirects=False,
    )
    assert r.status_code == 303 and r.headers["location"] == nxt, r.headers
    csrf_for(tc)
    page = tc.get(nxt)
    assert page.status_code == 200, page.status_code
    assert "Example Agent" in page.text and "client.example" in page.text
    assert 'id="oauth-consent-redirect-host"' in page.text
    assert 'id="oauth-consent-localhost"' not in page.text
    blob = qs(nxt)["r"]
    # a tampered request blob is refused
    r = tc.post(
        "/oauth/consent",
        data={"r": blob[:-3] + "AAA", "decision": "approve"},
        follow_redirects=False,
    )
    assert r.status_code == 400, r.status_code
    # deny
    r = tc.post("/oauth/consent", data={"r": blob, "decision": "deny"}, follow_redirects=False)
    assert qs(r.headers["location"])["error"] == "access_denied"
    assert qs(r.headers["location"])["iss"] == ISSUER
    # approve, narrowing to api only although docs was requested
    r = tc.post("/oauth/consent", data={"r": blob, "decision": "approve"}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"].startswith(REDIRECT + "?"), r.headers
    cb = qs(r.headers["location"])
    assert cb["iss"] == ISSUER and cb["state"] == "st-1" and cb["code"]
    narrow_code = cb["code"]
    # approve again with docs ticked, for the main grant
    verifier, challenge = pkce()
    r = tc.get(authorize_url(challenge=challenge, scope="api docs keys"), follow_redirects=False)
    target = r.headers["location"]
    assert target.startswith("/oauth/consent?r=")
    blob = qs(target)["r"]
    r = tc.post(
        "/oauth/consent",
        data={"r": blob, "decision": "approve", "scope": ["docs"]},
        follow_redirects=False,
    )
    code = qs(r.headers["location"])["code"]
    # loopback redirect: warning shown; any port matches a registered loopback URI
    _v2, c2 = pkce()
    r = tc.get(authorize_url(redirect=LOOPBACK, challenge=c2), follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"].startswith("/oauth/consent"), r.headers
    page = tc.get(r.headers["location"])
    assert 'id="oauth-consent-localhost"' in page.text and "127.0.0.1" in page.text
    print(
        "  (4) authorize: no open redirect, errors carry iss, sign-in keeps the request, "
        "consent shows client + redirect host"
    )

    # ---- (5) code exchange -----------------------------------------------------------------
    def token(data, **kw):
        return tc.post("/oauth/token", data=data, **kw)

    base = {
        "grant_type": "authorization_code",
        "client_id": CLIENT,
        "redirect_uri": REDIRECT,
        "resource": RESOURCE,
    }
    r = token({**base, "code": code, "code_verifier": pkce()[0]})
    assert r.status_code == 400 and r.json()["error"] == "invalid_grant"
    r = token({**base, "code": code, "code_verifier": verifier, "redirect_uri": REDIRECT + "x"})
    assert r.status_code == 400
    r = token({**base, "code": code, "code_verifier": verifier, "client_id": CLIENT + "x"})
    assert r.status_code == 400
    r = token({**base, "code": code, "code_verifier": verifier, "resource": "https://x.example"})
    assert r.status_code == 400 and r.json()["error"] == "invalid_target"
    r = token({**base, "code": code, "code_verifier": verifier})
    assert r.status_code == 200, r.text
    assert r.headers.get("cache-control") == "no-store"
    tok = r.json()
    assert tok["token_type"] == "Bearer" and tok["expires_in"] == 3600
    assert tok["access_token"].startswith("lsm_") and tok["refresh_token"].startswith("lsr_")
    assert tok["scope"] == "api docs", tok["scope"]
    access, refresh = tok["access_token"], tok["refresh_token"]
    # the narrowed grant: api only; its code reused afterwards revokes it
    n = token({**base, "code": narrow_code, "code_verifier": "x" * 43})
    assert n.status_code == 400  # wrong verifier: the code is NOT consumed
    print("  (5) code exchange: PKCE + client + redirect + resource bound, no-store")

    # ---- (6) token exchange + /api -----------------------------------------------------------
    ex = {
        "grant_type": mcp_as.TOKEN_EXCHANGE,
        "subject_token": access,
        "subject_token_type": mcp_as.TT_ACCESS,
    }
    r = token(ex)
    assert r.status_code == 401 and r.json()["error"] == "invalid_client"
    r = token(ex, headers={"Authorization": rs_basic("wrong")})
    assert r.status_code == 401
    r = token(ex, headers={"Authorization": rs_basic(rs_secret)})
    assert r.status_code == 200, r.text
    api_tok = r.json()["access_token"]
    assert api_tok.startswith("lso_") and r.json()["scope"] == "api docs"
    assert (
        token(ex, headers={"Authorization": rs_basic(rs_secret)}).json()["access_token"] == api_tok
    )
    # an lso_ token cannot be exchanged (it is not an MCP-audience token)
    bad = token({**ex, "subject_token": api_tok}, headers={"Authorization": rs_basic(rs_secret)})
    assert bad.status_code == 400
    g = tc.get("/api/mcp-oauth/grants", headers={"X-Loc-Token": api_tok})
    assert g.status_code == 200, g.text
    grants = g.json()["grants"]
    assert len(grants) == 1 and grants[0]["client_name"] == "Example Agent"
    assert grants[0]["scopes"] == "api docs" and "hash" not in json.dumps(grants)
    assert tc.get("/api/mcp-oauth/grants", headers={"X-Loc-Token": access}).status_code == 401
    r = tc.post("/api/apikeys/issue", headers={"X-Loc-Token": api_tok}, json={"scopes": "docs"})
    assert r.status_code == 403 and "MCP OAuth" in r.json()["error"], r.text
    # audience binding: a different configured resource invalidates every token at once
    settings.set_many({"mcp_oauth_resource": "https://mcp2.example.com/mcp"})
    settings.invalidate_cache()
    assert token(ex, headers={"Authorization": rs_basic(rs_secret)}).status_code == 400
    assert tc.get("/api/mcp-oauth/grants", headers={"X-Loc-Token": api_tok}).status_code == 401
    settings.set_many({"mcp_oauth_resource": RESOURCE.upper().replace("/MCP", "/mcp")})
    settings.invalidate_cache()
    assert tc.get("/api/mcp-oauth/grants", headers={"X-Loc-Token": api_tok}).status_code == 200
    settings.set_many({"mcp_oauth_resource": RESOURCE})
    settings.invalidate_cache()
    # the server switched off: lso_ dies with it
    settings.set_many({"mcp_oauth_enabled": "0"})
    settings.invalidate_cache()
    assert tc.get("/api/mcp-oauth/grants", headers={"X-Loc-Token": api_tok}).status_code == 401
    settings.set_many({"mcp_oauth_enabled": "1"})
    settings.invalidate_cache()
    print(
        "  (6) exchange only for the resource server; lso_ works on /api, lsm_ does not; "
        "audience follows mcp_oauth_resource; no key minting"
    )

    # ---- (7) refresh ----------------------------------------------------------------------
    rf = {
        "grant_type": "refresh_token",
        "client_id": CLIENT,
        "refresh_token": refresh,
        "resource": RESOURCE,
    }
    assert token({**rf, "client_id": "https://other.example/x.json"}).status_code == 400
    assert token({**rf, "scope": "api docs keys"}).json()["error"] == "invalid_scope"
    r = token(rf)
    assert r.status_code == 200, r.text
    tok2 = r.json()
    assert tok2["refresh_token"] != refresh and tok2["access_token"] != access
    assert tc.get("/api/mcp-oauth/grants", headers={"X-Loc-Token": api_tok}).status_code == 401
    ex2 = {**ex, "subject_token": tok2["access_token"]}
    api2 = token(ex2, headers={"Authorization": rs_basic(rs_secret)}).json()["access_token"]
    assert tc.get("/api/mcp-oauth/grants", headers={"X-Loc-Token": api2}).status_code == 200
    # replaying the old refresh token revokes the whole grant
    assert token(rf).status_code == 400
    assert tc.get("/api/mcp-oauth/grants", headers={"X-Loc-Token": api2}).status_code == 401
    assert token({**rf, "refresh_token": tok2["refresh_token"]}).status_code == 400
    print("  (7) refresh rotates; a reused refresh token revokes the grant")

    # ---- (8) revocation -----------------------------------------------------------------------
    def new_grant(user_email, scope="api docs"):
        tc.cookies.clear()
        tc.headers.pop("X-CSRF-Token", None)
        csrf_for(tc)
        tc.post(
            "/auth/login",
            data={"email": user_email, "password": PASSWORD, "next": "/"},
            follow_redirects=False,
        )
        csrf_for(tc)
        v, c = pkce()
        loc = tc.get(authorize_url(challenge=c, scope=scope), follow_redirects=False).headers[
            "location"
        ]
        b = qs(loc)["r"]
        loc = tc.post(
            "/oauth/consent",
            data={"r": b, "decision": "approve", "scope": ["docs"]},
            follow_redirects=False,
        ).headers["location"]
        t = token({**base, "code": qs(loc)["code"], "code_verifier": v}).json()
        a = token(
            {**ex, "subject_token": t["access_token"]},
            headers={"Authorization": rs_basic(rs_secret)},
        ).json()["access_token"]
        return t, a

    _t_a, api_a = new_grant(admin.email)
    gid_a = tc.get("/api/mcp-oauth/grants", headers={"X-Loc-Token": api_a}).json()["grants"][0][
        "id"
    ]
    _t_b, api_b = new_grant(other.email)
    # a foreign id answers like a missing one
    r = tc.post(f"/api/mcp-oauth/grants/{gid_a}/revoke", headers={"X-Loc-Token": api_b})
    assert r.status_code == 404
    r2 = tc.post("/api/mcp-oauth/grants/999999/revoke", headers={"X-Loc-Token": api_b})
    assert r2.status_code == 404 and r.json() == r2.json()
    assert tc.get("/api/mcp-oauth/grants", headers={"X-Loc-Token": api_a}).status_code == 200
    # own revoke over /api
    r = tc.post(f"/api/mcp-oauth/grants/{gid_a}/revoke", headers={"X-Loc-Token": api_a})
    assert r.status_code == 200
    assert tc.get("/api/mcp-oauth/grants", headers={"X-Loc-Token": api_a}).status_code == 401
    # web: the connected-apps page lists the signed-in person's own grant and revokes it
    page = tc.get("/account/connected-apps")
    assert page.status_code == 200 and "Example Agent" in page.text
    gid_b = int(re.search(r"/account/connected-apps/(\d+)/revoke", page.text).group(1))
    r = tc.post(f"/account/connected-apps/{gid_b}/revoke", follow_redirects=False)
    assert r.status_code == 303
    assert tc.get("/api/mcp-oauth/grants", headers={"X-Loc-Token": api_b}).status_code == 401
    # account revoke-all (the disable / erasure / revoke-all funnel) kills every grant
    _t, api_c = new_grant(other.email)
    assert tc.get("/api/mcp-oauth/grants", headers={"X-Loc-Token": api_c}).status_code == 200
    apikeys.revoke_all_for_user(other.id, org_id=None)
    assert tc.get("/api/mcp-oauth/grants", headers={"X-Loc-Token": api_c}).status_code == 401
    # a reused authorization code revokes the grant it minted
    v, c = pkce()
    loc = tc.get(authorize_url(challenge=c), follow_redirects=False).headers["location"]
    loc = tc.post(
        "/oauth/consent", data={"r": qs(loc)["r"], "decision": "approve"}, follow_redirects=False
    ).headers["location"]
    cd = qs(loc)["code"]
    t = token({**base, "code": cd, "code_verifier": v}).json()
    assert token({**base, "code": cd, "code_verifier": v}).status_code == 400
    r = token(
        {**ex, "subject_token": t["access_token"]}, headers={"Authorization": rs_basic(rs_secret)}
    )
    assert r.status_code == 400, "code reuse must revoke the grant"
    print("  (8) revocation: own grant (api + web), foreign id 404, revoke-all, code reuse")

    # ---- (9) CIMD safety ------------------------------------------------------------------
    meta.docs["https://client.example/liar.json"] = dict(meta.docs[CLIENT], client_id=CLIENT)
    r = tc.get(
        authorize_url(client_id="https://client.example/liar.json", challenge=c),
        follow_redirects=False,
    )
    assert r.status_code == 400 and "location" not in r.headers
    meta.docs["https://client.example/secret.json"] = dict(
        meta.docs[CLIENT],
        client_id="https://client.example/secret.json",
        token_endpoint_auth_method="client_secret_basic",
    )
    r = tc.get(
        authorize_url(client_id="https://client.example/secret.json", challenge=c),
        follow_redirects=False,
    )
    assert r.status_code == 400
    meta.docs["https://client.example/http.json"] = dict(
        meta.docs[CLIENT],
        client_id="https://client.example/http.json",
        redirect_uris=["http://client.example/cb"],
    )
    r = tc.get(
        authorize_url(
            client_id="https://client.example/http.json",
            challenge=c,
            redirect="http://client.example/cb",
        ),
        follow_redirects=False,
    )
    assert r.status_code == 400
    egress.set_test_resolver(lambda host: ["10.0.0.5"])
    mcp_as.set_test_transport(httpx.MockTransport(meta.handle))  # clears the cache
    before = len(meta.fetches)
    r = tc.get(authorize_url(challenge=c), follow_redirects=False)
    assert r.status_code == 400 and len(meta.fetches) == before, "private address never fetched"
    egress.set_test_resolver(lambda host: ["93.184.216.34"])
    settings.set_many({"mcp_oauth_cimd_hosts": "claude.ai, *.trusted.example"})
    settings.invalidate_cache()
    r = tc.get(authorize_url(challenge=c), follow_redirects=False)
    assert r.status_code == 400 and "client.example" in r.text
    settings.set_many({"mcp_oauth_cimd_hosts": "*.example, client.example"})
    settings.invalidate_cache()
    assert tc.get(authorize_url(challenge=c), follow_redirects=False).status_code == 303
    settings.set_many({"mcp_oauth_cimd_hosts": ""})
    for cid in ("http://client.example/m.json", "https://client.example", "notaurl"):
        r = tc.get(authorize_url(client_id=cid, challenge=c), follow_redirects=False)
        assert r.status_code == 400 and "location" not in r.headers, cid
    print(
        "  (9) CIMD: id mismatch, confidential client, http redirect, private address, "
        "trust list, malformed ids all refused without a redirect"
    )

    # ---- (10) dynamic registration ----------------------------------------------------------
    assert tc.post("/oauth/register", json={"redirect_uris": [LOOPBACK]}).status_code == 404
    settings.set_many({"mcp_oauth_dcr_enabled": "1"})
    settings.invalidate_cache()
    assert "registration_endpoint" in tc.get("/.well-known/oauth-authorization-server").json()
    r = tc.post("/oauth/register", json={"redirect_uris": ["http://evil.example/cb"]})
    assert r.status_code == 400 and r.json()["error"] == "invalid_redirect_uri"
    r = tc.post(
        "/oauth/register",
        json={"redirect_uris": [LOOPBACK], "token_endpoint_auth_method": "client_secret_post"},
    )
    assert r.status_code == 400
    r = tc.post(
        "/oauth/register",
        json={
            "client_name": "Old Client",
            "redirect_uris": ["http://127.0.0.1/cb"],
            "application_type": "native",
        },
    )
    assert r.status_code == 201, r.text
    reg = r.json()
    assert reg["client_id"].startswith("dcr_") and reg["application_type"] == "native"
    assert reg["token_endpoint_auth_method"] == "none"
    v, c = pkce()
    r = tc.get(
        authorize_url(
            client_id=reg["client_id"], redirect="http://127.0.0.1:50123/cb", challenge=c
        ),
        follow_redirects=False,
    )
    assert r.status_code == 303 and r.headers["location"].startswith("/oauth/consent"), r.headers
    forged = reg["client_id"][:-4] + "abcd"
    r = tc.get(
        authorize_url(client_id=forged, redirect="http://127.0.0.1/cb", challenge=c),
        follow_redirects=False,
    )
    assert r.status_code == 400
    settings.set_many({"mcp_oauth_dcr_enabled": "0"})
    settings.invalidate_cache()
    r = tc.get(
        authorize_url(client_id=reg["client_id"], redirect="http://127.0.0.1/cb", challenge=c),
        follow_redirects=False,
    )
    assert r.status_code == 400, "DCR clients stop working when DCR is switched off"
    print("  (10) DCR only when on; stateless signed client ids; loopback any port")

    # ---- (12) Review gate additions -----------------------------------------------------------
    _gate_checks(tc)

    # ---- (11) the MCP server's resource-server gate ----------------------------------------
    _rs_checks(rs_secret, tc)
    mcp_as.set_test_transport(None)
    egress.set_test_resolver(None)
    print("verify_mcp_oauth_as OK")


def _gate_checks(tc) -> None:
    """Review gate (m-oauth): a viewer cannot approve consent; redirect URIs the consent page's
    CSP helper refuses are refused up front (were a 500); an oversized DCR registration is
    refused (its client id could never redeem); per-IP budgets on the unauthenticated
    endpoints, with the resource server's authenticated exchange never charged."""
    from app.services import ratelimit

    ratelimit.reset()
    # a viewer reaches the consent page but cannot approve (the can_write wall, like key mint)
    viewer = security.create_user("vi-viewer@example.test", "Vi", "viewer", PASSWORD)
    tc.cookies.clear()
    tc.headers.pop("X-CSRF-Token", None)
    csrf_for(tc)
    tc.post(
        "/auth/login",
        data={"email": viewer.email, "password": PASSWORD, "next": "/"},
        follow_redirects=False,
    )
    csrf_for(tc)
    _v, c = pkce()
    loc = tc.get(authorize_url(challenge=c), follow_redirects=False).headers["location"]
    assert loc.startswith("/oauth/consent?r="), loc
    r = tc.post(
        "/oauth/consent", data={"r": qs(loc)["r"], "decision": "approve"}, follow_redirects=False
    )
    assert r.status_code == 403 and "location" not in r.headers, r.status_code
    assert mcp_as.list_grants(viewer.id, include_revoked=True) == []
    # redirect URIs: printable ASCII host[:port] only
    for bad in (
        "https://bücher.example/cb",
        "https://a_b.example/cb",
        "https://ok.example/cé",
    ):
        assert mcp_as.redirect_uri_problem(bad), bad
    assert mcp_as.redirect_uri_problem("https://xn--bcher-kva.example/cb") == ""
    # an oversized registration is refused instead of minting a client id no grant can hold
    # random paths: itsdangerous zlib-compresses a repetitive payload
    long_uris = ["https://client.example/" + secrets.token_urlsafe(360) for _ in range(5)]
    try:
        mcp_as.register({"client_name": "Big", "redirect_uris": long_uris})
    except mcp_as.OAuthError as e:
        assert e.error == "invalid_client_metadata", e.error
    else:
        raise AssertionError("an oversized DCR client id must be refused")
    # per-IP budgets
    ratelimit.reset()
    codes = [
        tc.get(authorize_url(client_id="nope", challenge=c), follow_redirects=False).status_code
        for _ in range(mcp_as.AUTHORIZE_PER_MINUTE + 1)
    ]
    assert codes[:-1] == [400] * mcp_as.AUTHORIZE_PER_MINUTE and codes[-1] == 429, codes[-3:]
    ratelimit.reset()
    codes = [
        tc.post("/oauth/token", data={"grant_type": "authorization_code", "code": "x"}).status_code
        for _ in range(mcp_as.TOKEN_PER_MINUTE + 1)
    ]
    assert codes[-1] == 429 and 429 not in codes[:-1], codes[-3:]
    # the resource server's exchange is not charged: still an auth answer, never a 429
    r = tc.post(
        "/oauth/token",
        data={"grant_type": mcp_as.TOKEN_EXCHANGE, "subject_token": "lsm_x"},
        headers={"Authorization": rs_basic("wrong")},
    )
    assert r.status_code == 401, r.status_code
    ratelimit.reset()
    print(
        "  (12) gate: viewer cannot approve; ASCII host redirect URIs only; oversized DCR "
        "refused; per-IP budgets on authorize / token (RS exchange uncharged)"
    )


def _rs_checks(rs_secret, tc) -> None:
    sys.path.insert(0, os.path.join(_ROOT, "integrations", "mcp-server"))
    import oauth_rs

    cfg = oauth_rs.OAuthConfig(RESOURCE, ISSUER, rs_secret, "http://testserver/oauth/token")
    seen = []

    async def downstream(scope, receive, send):
        body = b""
        while True:
            msg = await receive()
            body += msg.get("body", b"")
            if not msg.get("more_body"):
                break
        seen.append(({k.decode(): v.decode() for k, v in scope["headers"]}, body))
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})

    scopes = {
        "translate_text": "api",
        "list_projects": "docs",
        "list_keys": "keys",
        "manage_users": "admin",
    }
    gate = oauth_rs.OAuthResourceGate(
        downstream,
        cfg=cfg,
        mcp_path="/mcp",
        scope_for_tool=scopes.get,
        max_body=1024 * 1024,
        transport=httpx.ASGITransport(app=app),
    )
    from starlette.testclient import TestClient as StarletteClient

    rs = StarletteClient(gate)
    # protected resource metadata, at the path-inserted and the root well-known URLs
    for path in (
        "/.well-known/oauth-protected-resource/mcp",
        "/.well-known/oauth-protected-resource",
    ):
        r = rs.get(path)
        assert r.status_code == 200, (path, r.status_code)
        prm = r.json()
        assert prm["resource"] == RESOURCE and prm["authorization_servers"] == [ISSUER]
        assert set(prm["scopes_supported"]) == {"api", "docs", "keys"}
    assert cfg.prm_url == "https://mcp.example.com/.well-known/oauth-protected-resource/mcp"
    call = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {"name": "list_projects", "arguments": {}},
    }
    # no token: 401 + resource_metadata + scope
    r = rs.post("/mcp", json=call)
    assert r.status_code == 401, r.status_code
    wa = r.headers["www-authenticate"]
    assert wa.startswith("Bearer ") and f'resource_metadata="{cfg.prm_url}"' in wa
    assert 'scope="api docs keys"' in wa and "error=" not in wa
    assert not seen, "nothing reaches the SDK without a token"
    # invalid / foreign tokens: 401 invalid_token (an /api token is not an MCP token)
    for bad in ("lsm_nope", "lso_" + "x" * 43, "random"):
        r = rs.post("/mcp", json=call, headers={"Authorization": f"Bearer {bad}"})
        assert r.status_code == 401 and 'error="invalid_token"' in r.headers["www-authenticate"], (
            bad
        )
    # a real grant: api + docs only
    tc.cookies.clear()
    tc.headers.pop("X-CSRF-Token", None)
    csrf_for(tc)
    tc.post(
        "/auth/login",
        data={"email": "ada-admin@example.test", "password": PASSWORD, "next": "/"},
        follow_redirects=False,
    )
    csrf_for(tc)
    v, c = pkce()
    loc = tc.get(authorize_url(challenge=c), follow_redirects=False).headers["location"]
    loc = tc.post(
        "/oauth/consent",
        data={"r": qs(loc)["r"], "decision": "approve", "scope": ["docs"]},
        follow_redirects=False,
    ).headers["location"]
    t = tc.post(
        "/oauth/token",
        data={
            "grant_type": "authorization_code",
            "client_id": CLIENT,
            "redirect_uri": REDIRECT,
            "code": qs(loc)["code"],
            "code_verifier": v,
            "resource": RESOURCE,
        },
    ).json()
    at = t["access_token"]
    r = rs.post("/mcp", json=call, headers={"Authorization": f"Bearer {at}"})
    assert r.status_code == 200, (r.status_code, r.text)
    hdrs, body = seen[-1]
    assert "authorization" not in hdrs, "the client token is never passed through"
    assert hdrs["x-loc-token"].startswith("lso_"), hdrs
    assert json.loads(body) == call, "the body is replayed intact"
    # insufficient scope: one 403 naming every scope the call needs
    batch = [
        call,
        {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/call",
            "params": {"name": "list_keys", "arguments": {}},
        },
    ]
    n = len(seen)
    r = rs.post("/mcp", json=batch, headers={"Authorization": f"Bearer {at}"})
    assert r.status_code == 403, r.status_code
    wa = r.headers["www-authenticate"]
    assert 'error="insufficient_scope"' in wa and 'scope="api docs keys"' in wa, wa
    assert f'resource_metadata="{cfg.prm_url}"' in wa and len(seen) == n
    # an admin tool is not gated here (no grant can carry admin; the app answers 403)
    adm = dict(call, params={"name": "manage_users", "arguments": {}})
    assert rs.post("/mcp", json=adm, headers={"Authorization": f"Bearer {at}"}).status_code == 200
    # tools/list and friends need only a valid token
    lst = {"jsonrpc": "2.0", "id": 3, "method": "tools/list"}
    assert rs.post("/mcp", json=lst, headers={"Authorization": f"Bearer {at}"}).status_code == 200
    # static per-user keys pass through unchanged (Bearer or X-Loc-Token)
    rs.post("/mcp", json=lst, headers={"Authorization": "Bearer lsu_" + "a" * 20})
    assert seen[-1][0]["authorization"] == "Bearer lsu_" + "a" * 20
    rs.post("/mcp", json=lst, headers={"X-Loc-Token": "lsu_" + "b" * 20})
    assert seen[-1][0]["x-loc-token"] == "lsu_" + "b" * 20
    # an /api token smuggled through the alias header is still exchanged (and refused)
    r = rs.post("/mcp", json=lst, headers={"X-Loc-Token": "lso_" + "c" * 43})
    assert r.status_code == 401
    # revoked grant: the cached exchange is bypassed by /api itself (lso_ dies at once)
    grants = tc.get("/api/mcp-oauth/grants", headers={"X-Loc-Token": hdrs["x-loc-token"]}).json()
    mcp_as.revoke_grant(grants["grants"][-1]["id"])
    assert (
        tc.get("/api/mcp-oauth/grants", headers={"X-Loc-Token": hdrs["x-loc-token"]}).status_code
        == 401
    )
    print(
        "  (11a) gate: PRM, 401 challenge, invalid_token, 403 insufficient_scope with every "
        "needed scope, token swapped (no passthrough), lsu_ unchanged"
    )

    # the real listener: gate only when configured; a half configuration fails at startup
    try:
        oauth_rs.config_from_env("http://testserver", {"LOC_STUDIO_MCP_OAUTH_RESOURCE": RESOURCE})
        raise AssertionError("missing issuer/secret must fail")
    except ValueError as e:
        assert "LOC_STUDIO_MCP_OAUTH_ISSUER" in str(e)
    try:
        oauth_rs.config_from_env(
            "http://testserver",
            {
                "LOC_STUDIO_MCP_OAUTH_RESOURCE": "http://mcp.example.com/mcp",
                "LOC_STUDIO_MCP_OAUTH_ISSUER": ISSUER,
                "LOC_STUDIO_MCP_RS_SECRET": "s",
            },
        )
        raise AssertionError("http resource must fail")
    except ValueError as e:
        assert "LOC_STUDIO_MCP_OAUTH_RESOURCE" in str(e)
    assert oauth_rs.config_from_env("http://testserver", {}) is None
    import server

    plain = server.mcp.streamable_http_app()
    assert not any(m.cls is oauth_rs.OAuthResourceGate for m in plain.user_middleware)
    os.environ.update(
        {
            "LOC_STUDIO_MCP_OAUTH_RESOURCE": RESOURCE,
            "LOC_STUDIO_MCP_OAUTH_ISSUER": ISSUER,
            "LOC_STUDIO_MCP_RS_SECRET": rs_secret,
        }
    )
    try:
        gated = server.mcp.streamable_http_app()
        assert gated.user_middleware[0].cls is oauth_rs.OAuthResourceGate, "gate is outermost"
        with StarletteClient(gated) as lc:
            r = lc.post("/mcp", json=lst, headers={"Accept": "application/json, text/event-stream"})
            assert r.status_code == 401 and "resource_metadata" in r.headers["www-authenticate"]
            assert (
                lc.get("/.well-known/oauth-protected-resource/mcp").json()["resource"] == RESOURCE
            )
    finally:
        for k in (
            "LOC_STUDIO_MCP_OAUTH_RESOURCE",
            "LOC_STUDIO_MCP_OAUTH_ISSUER",
            "LOC_STUDIO_MCP_RS_SECRET",
        ):
            os.environ.pop(k, None)
    assert server._oauth_scope_for_tool("translate_text") in ("api", "docs", "keys")
    assert server._oauth_scope_for_tool("no_such_tool") is None
    print("  (11b) listener: gate outermost only when configured; half configuration refused")
    asyncio.run(asyncio.sleep(0))


if __name__ == "__main__":
    main()
