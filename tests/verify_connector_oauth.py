"""Connector OAuth token lifecycle verification — fully offline via MockTransport.

Provider-agnostic (chunk 2): no real Airtable/Atlassian wiring exists yet, so this
suite registers a SYNTHETIC OAuth provider descriptor into the registry (the
registry is designed for exactly this) and drives the whole lifecycle against a
fake token endpoint served through httpx.MockTransport. Nothing here touches a
real network or the real data DB — it owns a throwaway LOC_TMS_DATA_DIR and clears
the four provider env leaks so MT (never exercised here) would stay on the offline
MockProvider anyway.

Cases:
  - code-exchange stores the v2 oauth shape + computed expires_at
  - refresh persists a ROTATED refresh_token from the response
  - refresh that omits refresh_token keeps the old one
  - refresh FAILURE -> ConnStatus.REAUTH, stale secret returned, no crash
  - SINGLE-FLIGHT: two threads on an expired token -> exactly ONE refresh POST
  - legacy token-auth secret passes through build_fresh untouched (0 token hits)
"""

import os
import sys
import threading
import time

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(_ROOT)
sys.path.insert(0, _ROOT)
from tests._bootstrap import init_env

init_env("loctms_oauth_")

import httpx

from app.db import init_db
from app.models import ConnStatus
from app.services import settings
from app.services.connectors import oauth
from app.services.connectors import oauth as conn_oauth_mod
from app.services.connectors import store as cstore

KIND = "airtable"  # a real connector kind, so build_fresh() can instantiate a class
AUTHORIZE = "https://provider.test/authorize"
TOKEN = "https://provider.test/oauth/token"


def _register_synthetic_provider():
    """A synthetic descriptor for KIND so the generic flow has something to talk
    to. post_token_hook adds a provider-specific key (cloudid) so we can assert it
    survives a later refresh."""

    def hook(oauth_dict, transport):
        oauth_dict = dict(oauth_dict)
        oauth_dict["cloudid"] = "cloud-XYZ"
        return oauth_dict

    oauth.register_provider(
        oauth.OAuthProvider(
            kind=KIND,
            authorize_url=AUTHORIZE,
            token_url=TOKEN,
            scopes="data.records:read data.records:write",
            uses_pkce=True,
            extra_authorize_params={"prompt": "consent"},
            post_token_hook=hook,
        )
    )


class CountingTransport(httpx.BaseTransport):
    """A real httpx transport counting token-endpoint POSTs (a genuine
    BaseTransport subclass so httpx.Client's context-manager works). The token
    handler is swappable per-scenario; every /oauth/token POST increments `posts`."""

    def __init__(self):
        self.posts = 0
        self.token_response = None  # set per scenario (Response or callable)

    def handle_request(self, req: httpx.Request) -> httpx.Response:
        if req.url.path.endswith("/oauth/token") and req.method == "POST":
            self.posts += 1
            resp = self.token_response
            if callable(resp):
                return resp(req)
            return resp
        return httpx.Response(404, json={"path": req.url.path})


def _mk_oauth_conn(oauth_dict: dict, name="OAuth conn"):
    """Create a connection carrying an OAuth (v2) secret directly (bypassing the
    callback, which lands in chunk 3)."""
    conn = cstore.create(
        KIND,
        name,
        {"base_id": "appX", "table": "T", "source_field": "S", "target_field": "D"},
        {},
        actor="admin",
    )
    cstore.set_oauth_secret(conn.id, oauth_dict)
    return conn.id


def main() -> None:
    init_db()
    _register_synthetic_provider()
    settings.set_many(
        {f"oauth_{KIND}_client_id": "client-123", f"oauth_{KIND}_client_secret": "shhh"}
    )

    # ---- (1) code-exchange stores the v2 shape + expires_at ----
    ct = CountingTransport()
    ct.token_response = httpx.Response(
        200,
        json={
            "access_token": "AT-1",
            "refresh_token": "RT-1",
            "expires_in": 3600,
            "scope": "data.records:read",
            "token_type": "Bearer",
        },
    )
    oauth.set_default_transport(ct)

    exchanged = oauth._oauth_from_token_response(
        {
            "access_token": "AT-1",
            "refresh_token": "RT-1",
            "expires_in": 3600,
            "scope": "data.records:read",
            "token_type": "Bearer",
        }
    )
    now = int(time.time())
    assert exchanged["access_token"] == "AT-1" and exchanged["refresh_token"] == "RT-1"
    assert 3590 <= (exchanged["expires_at"] - now) <= 3600, exchanged
    assert exchanged["token_type"] == "Bearer" and exchanged["scope"] == "data.records:read"
    # persisted as v2 shape (auth_mode=oauth + oauth sub-dict)
    cid = _mk_oauth_conn(exchanged)
    conn = cstore.get(cid)
    assert cstore.is_oauth(conn), "connection must read back as OAuth"
    sec = cstore.dec_secret(conn.secret_enc)
    assert sec["auth_mode"] == "oauth" and sec["oauth"]["access_token"] == "AT-1", sec
    print("  code-exchange: v2 shape stored, expires_at computed from expires_in")

    # ---- (6) legacy token-auth secret passes through build_fresh untouched ----
    # (asserted early, before any refresh scenario, so the counter is clean)
    ct.posts = 0
    legacy = cstore.create(
        KIND,
        "Legacy token",
        {"base_id": "appL", "table": "T"},
        {"api_key": "patLEGACY"},
        actor="admin",
    )
    assert not cstore.is_oauth(cstore.get(legacy.id)), "flat secret must be token-mode"
    got = cstore.ensure_fresh_token(legacy.id, transport=ct)
    assert got == {"api_key": "patLEGACY"}, ("legacy creds pass straight through", got)
    conn_l = cstore.build_fresh(legacy.id, transport=ct)  # must instantiate, no token call
    assert conn_l is not None
    assert ct.posts == 0, ("legacy secret must NEVER hit the token endpoint", ct.posts)
    print("  legacy token secret: build_fresh passes through, zero token-endpoint hits")

    # ---- (2) refresh persists a ROTATED refresh_token ----
    ct.posts = 0
    ct.token_response = httpx.Response(
        200,
        json={
            "access_token": "AT-2",
            "refresh_token": "RT-ROTATED",
            "expires_in": 3600,
            "token_type": "Bearer",
        },
    )
    # store an ALREADY-EXPIRED oauth token so ensure_fresh_token refreshes it
    expired = {
        "access_token": "AT-OLD",
        "refresh_token": "RT-1",
        "expires_at": now - 10,
        "scope": "s",
        "token_type": "Bearer",
        "cloudid": "cloud-XYZ",
    }
    cid2 = _mk_oauth_conn(expired, name="Rotate conn")
    fresh = cstore.ensure_fresh_token(cid2, transport=ct)
    assert ct.posts == 1, ("expired token must trigger exactly one refresh", ct.posts)
    assert fresh["oauth"]["access_token"] == "AT-2", fresh
    assert fresh["oauth"]["refresh_token"] == "RT-ROTATED", ("rotated RT must persist", fresh)
    assert fresh["oauth"]["cloudid"] == "cloud-XYZ", ("hook-added key survives refresh", fresh)
    # persisted, not just returned
    persisted = cstore.dec_secret(cstore.get(cid2).secret_enc)["oauth"]
    assert persisted["refresh_token"] == "RT-ROTATED", persisted
    assert cstore.get(cid2).status != ConnStatus.REAUTH
    print("  refresh: rotated refresh_token persisted; hook-added cloudid preserved")

    # ---- (3) refresh omitting refresh_token keeps the old one ----
    ct.posts = 0
    ct.token_response = httpx.Response(
        200, json={"access_token": "AT-3", "expires_in": 3600, "token_type": "Bearer"}
    )  # no refresh_token
    expired2 = {
        "access_token": "AT-OLD",
        "refresh_token": "RT-KEEP",
        "expires_at": now - 10,
        "token_type": "Bearer",
    }
    cid3 = _mk_oauth_conn(expired2, name="Keep RT conn")
    fresh3 = cstore.ensure_fresh_token(cid3, transport=ct)
    assert ct.posts == 1, ct.posts
    assert fresh3["oauth"]["access_token"] == "AT-3", fresh3
    assert fresh3["oauth"]["refresh_token"] == "RT-KEEP", ("old RT kept when omitted", fresh3)
    print("  refresh: response without refresh_token keeps the stored one")

    # ---- (4) refresh FAILURE -> REAUTH status, stale secret, no crash ----
    ct.posts = 0
    ct.token_response = httpx.Response(400, json={"error": "invalid_grant"})
    stale = {
        "access_token": "AT-STALE",
        "refresh_token": "RT-BAD",
        "expires_at": now - 10,
        "token_type": "Bearer",
    }
    cid4 = _mk_oauth_conn(stale, name="Fail conn")
    returned = cstore.ensure_fresh_token(cid4, transport=ct)  # must NOT raise
    assert ct.posts == 1, ct.posts
    assert returned["oauth"]["access_token"] == "AT-STALE", ("stale secret returned", returned)
    assert cstore.get(cid4).status == ConnStatus.REAUTH, "failed refresh must set REAUTH"
    # secret_enc untouched by a failed refresh
    assert cstore.dec_secret(cstore.get(cid4).secret_enc)["oauth"]["access_token"] == "AT-STALE"
    print("  refresh failure: REAUTH status set, stale secret returned, no crash")

    # ---- (5) SINGLE-FLIGHT: two threads, expired token -> exactly ONE POST ----
    ct.posts = 0

    def slow_refresh(req):
        # hold the token endpoint briefly so both threads are guaranteed to be in
        # ensure_fresh_token concurrently; only the lock winner should reach here.
        time.sleep(0.15)
        return httpx.Response(
            200,
            json={
                "access_token": "AT-SINGLE",
                "refresh_token": "RT-SINGLE",
                "expires_in": 3600,
                "token_type": "Bearer",
            },
        )

    ct.token_response = slow_refresh
    expired_sf = {
        "access_token": "AT-OLD",
        "refresh_token": "RT-SF",
        "expires_at": now - 10,
        "token_type": "Bearer",
    }
    cid5 = _mk_oauth_conn(expired_sf, name="Single-flight conn")

    results = {}

    def worker(tag):
        results[tag] = cstore.ensure_fresh_token(cid5, transport=ct)

    t1 = threading.Thread(target=worker, args=("a",))
    t2 = threading.Thread(target=worker, args=("b",))
    t1.start()
    t2.start()
    t1.join()
    t2.join()
    assert ct.posts == 1, ("two racing threads must trigger exactly ONE refresh POST", ct.posts)
    # both threads see the same freshly-persisted token (loser re-read it under lock)
    assert results["a"]["oauth"]["access_token"] == "AT-SINGLE", results["a"]
    assert results["b"]["oauth"]["access_token"] == "AT-SINGLE", results["b"]
    print("  single-flight: 2 threads on an expired token -> exactly ONE refresh POST")

    _chunk3_provider_wiring(now)
    _chunk3_routes()
    _chunk4_ui()
    _review_findings(now)
    test_connection_detail_secret_masked()

    oauth.set_default_transport(None)
    print("Connector OAuth OK")


# ===================== Chunk 3: provider wiring + routes ======================
class _FakeReq:
    """Minimal stand-in for a Starlette Request for the pure-function OAuth flow:
    a mutable .session dict + a .base_url. begin_/complete_authorize only touch
    these two."""

    def __init__(self, base="http://127.0.0.1:8000/", session=None):
        self.session = session if session is not None else {}
        self.base_url = base


def _reregister_builtin_airtable():
    """Cases 1-6 replaced the built-in airtable descriptor with a synthetic one
    (provider.test endpoints). Restore the REAL built-in descriptors so the
    chunk-3 wiring asserts run against the shipped Airtable/Atlassian config."""
    oauth._register_builtin_providers()


def _chunk3_provider_wiring(now: int) -> None:
    from urllib.parse import parse_qsl, urlsplit

    _reregister_builtin_airtable()

    # ---- begin_authorize: Airtable PKCE params ----
    settings.set_many({"oauth_airtable_client_id": "at-client", "oauth_airtable_client_secret": ""})
    req = _FakeReq()
    url = oauth.begin_authorize(req, "airtable", cid=42)
    assert url.startswith("https://airtable.com/oauth2/v1/authorize?"), url
    q = dict(parse_qsl(urlsplit(url).query))
    assert q["response_type"] == "code" and q["client_id"] == "at-client", q
    assert q["code_challenge_method"] == "S256" and q["code_challenge"], q
    assert "data.records:read" in q["scope"] and "schema.bases:read" in q["scope"], q
    assert q["redirect_uri"] == "http://127.0.0.1:8000/connectors/oauth/callback", q
    # PKCE verifier stashed in the session; state binds the cid
    assert req.session["conn_oauth_verifier"] and req.session["conn_oauth_cid"] == 42
    assert q["state"] == req.session["conn_oauth_state"]
    print("  begin_authorize (airtable): PKCE S256 challenge + scopes + bound cid")

    # ---- begin_authorize: Atlassian (Jira) audience/prompt, NO PKCE ----
    settings.set_many(
        {"oauth_atlassian_client_id": "atl-client", "oauth_atlassian_client_secret": "atl-secret"}
    )
    req2 = _FakeReq()
    url2 = oauth.begin_authorize(req2, "jira", cid=7, base_url_host="team.atlassian.net")
    assert url2.startswith("https://auth.atlassian.com/authorize?"), url2
    q2 = dict(parse_qsl(urlsplit(url2).query))
    assert q2["client_id"] == "atl-client", q2
    assert q2["audience"] == "api.atlassian.com" and q2["prompt"] == "consent", q2
    assert "read:jira-work" in q2["scope"] and "offline_access" in q2["scope"], q2
    assert "code_challenge" not in q2, ("Atlassian 3LO uses client_secret, not PKCE", q2)
    assert req2.session.get("conn_oauth_host") == "team.atlassian.net"
    # Jira + Confluence share the ONE atlassian app's client_id
    reqc = _FakeReq()
    urlc = oauth.begin_authorize(reqc, "confluence", cid=8)
    qc = dict(parse_qsl(urlsplit(urlc).query))
    assert qc["client_id"] == "atl-client", ("Confluence shares the Atlassian app", qc)
    assert "write:confluence-content" in qc["scope"], qc
    assert "write:page:confluence" in qc["scope"] and "read:space:confluence" in qc["scope"], qc
    assert "read:hierarchical-content:confluence" in qc["scope"], qc  # direct-children
    # Jira consent is UNCHANGED by the Confluence rebuild: no granular Confluence scope is
    # requested for a Jira connect/re-consent (the grant replaces the old one, and an unenabled
    # scope on the app would break the authorize request), and the string is the shipped one.
    assert q2["scope"] == (
        "read:jira-work write:jira-work read:jira-user read:confluence-content.all "
        "write:confluence-content search:confluence offline_access"
    ), q2
    print("  begin_authorize (atlassian): audience+prompt, shared app, granular scopes, no PKCE")

    # ---- complete_authorize round-trip incl. Atlassian accessible-resources ----
    def atlassian_handler(req: httpx.Request) -> httpx.Response:
        if req.url.path.endswith("/oauth/token"):
            return httpx.Response(
                200,
                json={
                    "access_token": "ATL-AT",
                    "refresh_token": "ATL-RT",
                    "expires_in": 3600,
                    "token_type": "Bearer",
                    "scope": "read:jira-work",
                },
            )
        if req.url.path.endswith("/accessible-resources"):
            # multi-site: the match must pick team.atlassian.net, not the first
            return httpx.Response(
                200,
                json=[
                    {"id": "cloud-OTHER", "name": "Other", "url": "https://other.atlassian.net"},
                    {"id": "cloud-MATCH", "name": "Team", "url": "https://team.atlassian.net"},
                ],
            )
        return httpx.Response(404, json={"path": req.url.path})

    atl_tp = httpx.MockTransport(atlassian_handler)
    # drive complete_authorize with the session begin_authorize set up (req2 / jira)
    (cid_out, oauth_out), err = oauth.complete_authorize(
        req2, code="CODE-XYZ", state=q2["state"], transport=atl_tp
    )
    assert err is None, err
    assert cid_out == 7 and oauth_out["access_token"] == "ATL-AT", oauth_out
    assert oauth_out["cloudid"] == "cloud-MATCH", ("multi-site host match", oauth_out)
    assert oauth_out["refresh_token"] == "ATL-RT", oauth_out
    # session keys are single-use (burned)
    assert "conn_oauth_state" not in req2.session
    print("  complete_authorize (atlassian): token + accessible-resources cloudid (host-matched)")

    # ---- state mismatch -> error, no exchange ----
    reqm = _FakeReq()
    oauth.begin_authorize(reqm, "airtable", cid=99)
    _, err_m = oauth.complete_authorize(reqm, code="C", state="WRONG-STATE")
    assert err_m and "state mismatch" in err_m.lower(), err_m
    print("  complete_authorize: state mismatch rejected")

    # ---- Jira OAuth transport: hits api.atlassian.com/ex/jira/{cloudid} w/ Bearer ----
    from app.services.connectors.confluence import ConfluenceConnector
    from app.services.connectors.jira import JiraConnector

    seen = {}

    def jira_gw(req: httpx.Request) -> httpx.Response:
        seen["host"] = req.url.host
        seen["path"] = req.url.path
        seen["auth"] = req.headers.get("Authorization", "")
        if req.url.path.endswith("/myself"):
            return httpx.Response(200, json={"displayName": "OAuth User"})
        return httpx.Response(200, json={"issues": [], "isLast": True})

    oauth_secret = {
        "auth_mode": "oauth",
        "oauth": {"access_token": "GW-AT", "cloudid": "cloud-MATCH"},
    }
    jc = JiraConnector(
        {"base_url": "https://team.atlassian.net"},
        oauth_secret,
        transport=httpx.MockTransport(jira_gw),
    )
    ok, msg = jc.test()
    assert ok, msg
    assert seen["host"] == "api.atlassian.com", seen
    assert seen["path"] == "/ex/jira/cloud-MATCH/rest/api/3/myself", seen
    assert seen["auth"] == "Bearer GW-AT", ("oauth mode sends Bearer, not basic", seen)
    print("  jira oauth: api.atlassian.com/ex/jira/{cloudid}/rest/api/3 + Bearer")

    # ---- Confluence OAuth transport: gateway path has NO /wiki prefix ----
    cseen = {}

    def conf_gw(req: httpx.Request) -> httpx.Response:
        cseen["path"] = req.url.path
        cseen["host"] = req.url.host
        cseen["auth"] = req.headers.get("Authorization", "")
        return httpx.Response(200, json={"results": [], "size": 0})

    cc = ConfluenceConnector(
        {"base_url": "https://team.atlassian.net"},
        oauth_secret,
        transport=httpx.MockTransport(conf_gw),
    )
    ok2, msg2 = cc.test()
    assert ok2, msg2
    assert cseen["host"] == "api.atlassian.com", cseen
    # v2 (pages/spaces) lives under /wiki on the gateway; the v1 CQL search path has NO /wiki
    assert cseen["path"] == "/ex/confluence/cloud-MATCH/wiki/api/v2/spaces", cseen
    assert cseen["auth"] == "Bearer GW-AT", cseen
    assert cc._api() == "https://api.atlassian.com/ex/confluence/cloud-MATCH/rest/api"
    assert "/wiki" not in cc._api(), ("the gateway v1 root has NO /wiki prefix", cc._api())
    print(
        "  confluence oauth: gateway v2 under /wiki/api/v2, v1 root /rest/api (no /wiki) + Bearer"
    )

    # ---- cloudid format-check rejects a malformed/injected cloudid ----
    bad_secret = {"auth_mode": "oauth", "oauth": {"access_token": "X", "cloudid": "../evil/path"}}
    jc_bad = JiraConnector(
        {"base_url": "https://team.atlassian.net"},
        bad_secret,
        transport=httpx.MockTransport(jira_gw),
    )
    ok3, msg3 = jc_bad.test()
    assert not ok3 and "cloudid" in msg3.lower(), ("bad cloudid must be rejected", msg3)
    assert not oauth.valid_cloudid("../evil") and oauth.valid_cloudid("cloud-MATCH")
    print("  cloudid format-check: '../evil/path' rejected, valid cloudid accepted")

    # ---- TOKEN-MODE REGRESSION: token Jira/Confluence still hit the SITE + basic ----
    tseen = {}

    def jira_site(req: httpx.Request) -> httpx.Response:
        tseen["host"] = req.url.host
        tseen["path"] = req.url.path
        tseen["has_basic"] = req.headers.get("Authorization", "").startswith("Basic ")
        return httpx.Response(200, json={"displayName": "Token User"})

    jt = JiraConnector(
        {"base_url": "https://team.atlassian.net"},
        {"email": "e@x.com", "api_token": "T"},
        transport=httpx.MockTransport(jira_site),
    )
    okt, _ = jt.test()
    assert okt and tseen["host"] == "team.atlassian.net", tseen
    assert tseen["path"] == "/rest/api/3/myself", ("token mode UNCHANGED site path", tseen)
    assert tseen["has_basic"], ("token mode uses basic auth, not Bearer", tseen)
    print("  token-mode regression: jira token still hits SITE /rest/api/3 with basic auth")


def _chunk3_routes() -> None:
    """Route tests with stubbed oauth funcs (no real network). Drives the real
    FastAPI app + session middleware; the login wall is OFF in tests so can_tune_ai
    passes for the anonymous default admin-equivalent (auth not required)."""
    from fastapi.testclient import TestClient

    from app.main import app
    from app.services.connectors import store as cstore2

    c = TestClient(app)

    # provider app configured so the start route proceeds
    settings.set_many(
        {"oauth_atlassian_client_id": "atl-client", "oauth_atlassian_client_secret": "atl-secret"}
    )

    # ---- POST /connectors/{kind}/oauth/start: creates a row, redirects to authorize ----
    before = len(cstore2.list_connections("jira"))
    r = c.post(
        "/connectors/jira/oauth/start",
        data={"name": "Jira OAuth", "base_url": "https://team.atlassian.net"},
        follow_redirects=False,
    )
    assert r.status_code == 303, r.status_code
    assert r.headers["location"].startswith("https://auth.atlassian.com/authorize?"), r.headers[
        "location"
    ]
    after = cstore2.list_connections("jira")
    assert len(after) == before + 1, "oauth start must create the Connection row first"
    new_conn = after[0]  # newest first
    assert new_conn.status == "new"
    print("  route POST /oauth/start: Connection row created, redirect to authorize URL")

    # ---- unconfigured provider -> honest redirect, no row ----
    settings.set_many({"oauth_airtable_client_id": "", "oauth_airtable_client_secret": ""})
    n_at = len(cstore2.list_connections("airtable"))
    r2 = c.post("/connectors/airtable/oauth/start", data={"name": "AT"}, follow_redirects=False)
    assert r2.status_code == 303 and "oauth_unconfigured" in r2.headers["location"], r2.headers.get(
        "location"
    )
    assert len(cstore2.list_connections("airtable")) == n_at, "no row when unconfigured"
    print("  route POST /oauth/start: unconfigured provider -> banner redirect, no row")

    # ---- GET callback with stubbed complete_authorize -> CONNECTED + audit ----
    made = cstore2.create(
        "jira", "Callback target", {"base_url": "https://team.atlassian.net"}, {}, actor="admin"
    )
    c.post("/connectors/jira/oauth/start", data={"cid": made.id}, follow_redirects=False)
    orig_complete = conn_oauth_mod.complete_authorize
    try:
        conn_oauth_mod.complete_authorize = lambda request, code, state: (
            (
                made.id,
                {
                    "access_token": "CB-AT",
                    "refresh_token": "CB-RT",
                    "cloudid": "cloud-CB",
                    "expires_at": 0,
                },
            ),
            None,
        )
        r3 = c.get("/connectors/oauth/callback?code=abc&state=xyz", follow_redirects=False)
        assert (
            r3.status_code == 303 and f"/connectors/connection/{made.id}" in r3.headers["location"]
        ), r3.headers.get("location")
        conn2 = cstore2.get(made.id)
        assert conn2.status == "connected", conn2.status
        assert cstore2.is_oauth(conn2), "callback must persist the v2 OAuth secret"
        assert cstore2.dec_secret(conn2.secret_enc)["oauth"]["access_token"] == "CB-AT"
        print("  route GET /oauth/callback: complete -> v2 secret persisted, CONNECTED")

        # ---- state mismatch / error surfaces as a redirect with msg (no 500) ----
        conn_oauth_mod.complete_authorize = lambda request, code, state: (
            None,
            "OAuth state mismatch - please start the connection again.",
        )
        r4 = c.get("/connectors/oauth/callback?code=abc&state=bad", follow_redirects=False)
        assert r4.status_code == 303 and "/connectors" in r4.headers["location"], r4.headers.get(
            "location"
        )
        assert "msg=" in r4.headers["location"], r4.headers["location"]
        print("  route GET /oauth/callback: state mismatch -> redirect with msg, no 500")
    finally:
        conn_oauth_mod.complete_authorize = orig_complete


# ===================== Chunk 4: UI surfaces (reauth badge, auth-mode, rotation) ==
def _wait_readback(pred, what: str, timeout: float = 10.0) -> None:
    """Block until a just-committed DB write is visible to a FRESH store read,
    before asserting on it through a separate HTTP request.

    The connection-page assertions below write a secret/status on one SQLite
    connection, then read it back through the request handler's OWN Session (a
    DIFFERENT connection). On a loaded machine (e.g. the full verify suite) a
    freshly-committed row can very occasionally lag a hair behind that independent
    read — WAL cross-connection visibility under I/O pressure — which surfaced as a
    rare "oauth auth-mode + scope must render" flake (the handler read the row
    before the OAuth secret was visible and rendered token-mode / redirected).

    SQLite committed-data visibility is monotonic: once a fresh read sees the
    write, every later fresh read (including the handler's) sees it too. Polling a
    fresh store read here therefore makes the write->read handoff explicit and
    deterministic instead of racing it — the same philosophy as verify_semantic_tm's
    wait-for-the-worker fix. It does not weaken any assertion; the render checks
    below are unchanged."""
    import time as _t

    deadline = _t.time() + timeout
    while _t.time() < deadline:
        if pred():
            return
        _t.sleep(0.02)
    raise AssertionError(f"readback barrier timed out ({timeout}s): {what}")


def _chunk4_ui() -> None:
    """UI-facing assertions for chunk 4: auth-mode display, the reauth badge +
    Reconnect button, rotation-card gating by auth mode, and the cid-reuse
    contract on POST /connectors/{kind}/oauth/start."""
    from fastapi.testclient import TestClient
    from sqlmodel import Session as _Session

    from app.db import engine as _engine
    from app.main import app
    from app.models import Connection
    from app.services.connectors import store as cstore3

    c = TestClient(app)

    # ---- oauth-mode connection page: auth-mode + scope/expiry, no rotation card ----
    oauth_conn = cstore3.create(
        "airtable", "OAuth display conn", {"base_id": "appD", "table": "T"}, {}, actor="admin"
    )
    cstore3.set_oauth_secret(
        oauth_conn.id,
        {
            "access_token": "AT-DISPLAY",
            "refresh_token": "RT-DISPLAY",
            "expires_at": int(time.time()) + 3600,
            "scope": "data.records:read",
            "token_type": "Bearer",
        },
    )

    def _oauth_secret_visible() -> bool:
        row = cstore3.get(oauth_conn.id)
        if not row or not cstore3.is_oauth(row):
            return False
        return (
            cstore3.dec_secret(row.secret_enc).get("oauth", {}).get("scope") == "data.records:read"
        )

    _wait_readback(_oauth_secret_visible, "oauth-mode secret + scope readable before render")

    # follow_redirects=False: the connection page returns 200 directly; a 3xx here
    # would mean the handler couldn't see the row (get() -> None -> redirect). Not
    # following it turns that into a crisp status failure instead of a misleading
    # "followed a redirect to a page that lacks the scope" render miss.
    r = c.get(f"/connectors/connection/{oauth_conn.id}", follow_redirects=False)
    assert r.status_code == 200, r.status_code
    page = r.text
    assert "OAuth" in page and "data.records:read" in page, "oauth auth-mode + scope must render"
    # Anchor on the rotate section's own heading: the page's section nav also links
    # "Rotate credentials", so the first text match is no longer the section.
    rotate_anchor = 'id="connection-rotate"'
    assert (
        rotate_anchor not in page
        or "<form" not in page.split(rotate_anchor)[1].split("</section>")[0]
    ), "oauth-mode connection must not show a rotate FORM"
    assert 'action="/connectors/connection/%d/rotate"' % oauth_conn.id not in page, (  # noqa: UP031
        "oauth-mode connection must not render the rotate form"
    )
    print("  connection page (oauth): auth-mode + scope render, rotate form absent")

    # ---- token-mode connection page: "API token", rotation card present ----
    token_conn = cstore3.create(
        "airtable",
        "Token display conn",
        {"base_id": "appT", "table": "T"},
        {"api_key": "patABC"},
        actor="admin",
    )
    _wait_readback(
        lambda: cstore3.get(token_conn.id) is not None,
        "token-mode connection row readable before render",
    )
    r2 = c.get(f"/connectors/connection/{token_conn.id}", follow_redirects=False)
    assert r2.status_code == 200, r2.status_code
    page2 = r2.text
    assert "API token" in page2, "token-mode auth display must render"
    assert 'action="/connectors/connection/%d/rotate"' % token_conn.id in page2, (  # noqa: UP031
        "token-mode connection must render the rotate form"
    )
    print("  connection page (token): 'API token' renders, rotate form present")

    # ---- reauth badge + Reconnect button ----
    with _Session(_engine) as s:
        row = s.get(Connection, oauth_conn.id)
        row.status = "reauth"
        s.add(row)
        s.commit()
    _wait_readback(
        lambda: getattr(cstore3.get(oauth_conn.id), "status", None) == "reauth",
        "reauth status readable before render",
    )
    r3 = c.get(f"/connectors/connection/{oauth_conn.id}", follow_redirects=False)
    assert r3.status_code == 200, r3.status_code
    page3 = r3.text
    assert 'class="badge reauth"' in page3, "distinct reauth badge class must render"
    assert "reconnect needed" in page3.lower()
    assert "Reconnect with OAuth" in page3, "Reconnect button must render for a reauth connection"
    assert 'action="/connectors/airtable/oauth/start"' in page3
    assert f'name="cid" value="{oauth_conn.id}"' in page3, "reconnect form must carry this cid"
    print("  connection page (reauth): distinct '.badge.reauth' + Reconnect button render")

    # ---- cid-reuse: POST /connectors/{kind}/oauth/start with an existing cid ----
    settings.set_many({"oauth_airtable_client_id": "at-client", "oauth_airtable_client_secret": ""})
    before = len(cstore3.list_connections("airtable"))
    r4 = c.post(
        "/connectors/airtable/oauth/start",
        data={"name": oauth_conn.name, "cid": str(oauth_conn.id)},
        follow_redirects=False,
    )
    assert r4.status_code == 303, r4.status_code
    assert r4.headers["location"].startswith("https://airtable.com/oauth2/v1/authorize?"), (
        r4.headers["location"]
    )
    after = len(cstore3.list_connections("airtable"))
    assert after == before, ("reusing an existing cid must NOT create a new row", before, after)
    print("  route POST /oauth/start (cid reuse): no new Connection row, redirect to authorize URL")

    # ---- regression: no cid -> create path still works (existing behavior) ----
    before2 = len(cstore3.list_connections("airtable"))
    r5 = c.post(
        "/connectors/airtable/oauth/start", data={"name": "Fresh OAuth"}, follow_redirects=False
    )
    assert r5.status_code == 303, r5.status_code
    assert r5.headers["location"].startswith("https://airtable.com/oauth2/v1/authorize?"), (
        r5.headers["location"]
    )
    after2 = len(cstore3.list_connections("airtable"))
    assert after2 == before2 + 1, "no cid supplied must still create a new row (regression guard)"
    print("  route POST /oauth/start (no cid): create path unchanged, new row created")

    # ---- HARDENING (adversarial review 2026-08-07): a MALFORMED cid must fall
    # through to the create path (like a stale id), never surface as an unhandled
    # 500. raw_cid.isdigit() used to admit a 40-nines cid (int() ok, then
    # OverflowError binding s.get) and cid="²" (superscript two: isdigit()
    # True but int() -> ValueError). The safe parse coerces both to None, so
    # `conn is None` and we take the SAME new-row create path as "no cid" above.
    for bad_cid, label in (
        ("9" * 40, "40-nines (OverflowError at the DB bind)"),
        ("²", "U+00B2 (isdigit True, int() ValueError)"),
    ):
        n_before = len(cstore3.list_connections("airtable"))
        rb = c.post(
            "/connectors/airtable/oauth/start",
            data={"name": "Bad cid", "cid": bad_cid},
            follow_redirects=False,
        )
        assert rb.status_code == 303, (label, rb.status_code)
        assert rb.headers["location"].startswith("https://airtable.com/oauth2/v1/authorize?"), (
            label,
            rb.headers.get("location"),
        )
        assert len(cstore3.list_connections("airtable")) == n_before + 1, (
            "a malformed cid must fall through to CREATE, not 500",
            label,
        )
    print(
        "  route POST /oauth/start (malformed cid): 40-nines + U+00B2 fall through "
        "to create, no 500"
    )


# ===================== Adversarial-review findings (F1-F5) ====================
def _review_findings(now: int) -> None:
    """Regression tests for the OAuth adversarial-review fixes.

    F1 rotate refuses OAuth mode; F2 containers/schema_fields use build_fresh (one
    refresh on an expired token); F3 a successful refresh clears a prior REAUTH;
    F4 the rich flows re-mint + rebuild before the write phase (write Bearer is the
    NEW token); F5 callback error/cancel redirects to /connectors/{kind}?msg=."""
    _reregister_builtin_airtable()  # ensure the real airtable descriptor
    _register_synthetic_provider()  # ...then re-install the synthetic KIND provider
    settings.set_many(
        {f"oauth_{KIND}_client_id": "client-123", f"oauth_{KIND}_client_secret": "shhh"}
    )

    # ---- F1: rotate_secret refuses an OAuth-mode connection ----
    oauth.set_default_transport(None)
    ok_conn = {
        "access_token": "AT-OK",
        "refresh_token": "RT-OK",
        "expires_at": now + 3600,
        "token_type": "Bearer",
    }
    cid_f1 = _mk_oauth_conn(ok_conn, name="F1 oauth rotate")
    before_secret = cstore.get(cid_f1).secret_enc
    ok, msg = cstore.rotate_secret(cid_f1, {"api_key": "patNEW"}, actor="admin")
    assert ok is False, ("rotate must refuse an OAuth connection", ok, msg)
    assert "reconnect" in msg.lower() and "provider" in msg.lower(), msg
    after = cstore.get(cid_f1)
    assert after.secret_enc == before_secret, (
        "OAuth secret_enc must be untouched by a refused rotate"
    )
    assert cstore.is_oauth(after), "connection must still be v2 OAuth after a refused rotate"
    print("  F1 rotate: refused on OAuth mode, secret_enc unchanged (still v2)")

    # ---- F2: containers() uses build_fresh -> exactly one refresh on expired token ----
    ct = CountingTransport()
    ct.token_response = httpx.Response(
        200,
        json={
            "access_token": "AT-F2",
            "refresh_token": "RT-F2",
            "expires_in": 3600,
            "token_type": "Bearer",
        },
    )

    class _ContainersTransport(httpx.BaseTransport):
        """Counts token POSTs (refresh) AND serves the Airtable meta/bases browse."""

        def __init__(self):
            self.posts = 0

        def handle_request(self, req):
            if req.url.path.endswith("/oauth/token") and req.method == "POST":
                self.posts += 1
                return httpx.Response(
                    200,
                    json={
                        "access_token": "AT-F2",
                        "refresh_token": "RT-F2",
                        "expires_in": 3600,
                        "token_type": "Bearer",
                    },
                )
            if req.url.path.endswith("/meta/bases"):
                return httpx.Response(200, json={"bases": [{"id": "appZ", "name": "Base Z"}]})
            if "/meta/bases/" in req.url.path and req.url.path.endswith("/tables"):
                return httpx.Response(200, json={"tables": []})
            return httpx.Response(404, json={"path": req.url.path})

    f2 = _ContainersTransport()
    expired_f2 = {
        "access_token": "AT-OLD",
        "refresh_token": "RT-F2",
        "expires_at": now - 10,
        "token_type": "Bearer",
        "cloudid": "cloud-XYZ",
    }
    cid_f2 = _mk_oauth_conn(expired_f2, name="F2 containers")
    got = cstore.containers(cid_f2, transport=f2)
    assert f2.posts == 1, ("expired-token containers() must trigger exactly one refresh", f2.posts)
    assert any(b.get("id") == "appZ" for b in got), ("containers() must succeed after refresh", got)
    assert cstore.dec_secret(cstore.get(cid_f2).secret_enc)["oauth"]["access_token"] == "AT-F2"
    print("  F2 containers: expired OAuth token -> one refresh then succeeds (build_fresh)")

    # ---- F3: a successful refresh clears a prior REAUTH -> CONNECTED ----
    fail_tp = CountingTransport()
    fail_tp.token_response = httpx.Response(400, json={"error": "invalid_grant"})
    expired_f3 = {
        "access_token": "AT-OLD",
        "refresh_token": "RT-F3",
        "expires_at": now - 10,
        "token_type": "Bearer",
    }
    cid_f3 = _mk_oauth_conn(expired_f3, name="F3 reauth self-heal")
    cstore.ensure_fresh_token(cid_f3, transport=fail_tp, force=True)  # -> REAUTH
    assert cstore.get(cid_f3).status == ConnStatus.REAUTH, "failed refresh must set REAUTH first"
    good_tp = CountingTransport()
    good_tp.token_response = httpx.Response(
        200,
        json={
            "access_token": "AT-HEALED",
            "refresh_token": "RT-HEALED",
            "expires_in": 3600,
            "token_type": "Bearer",
        },
    )
    cstore.ensure_fresh_token(cid_f3, transport=good_tp, force=True)  # success -> heal
    assert cstore.get(cid_f3).status == ConnStatus.CONNECTED, (
        "a successful refresh must clear REAUTH back to CONNECTED"
    )
    print("  F3 reauth self-heal: failed refresh -> REAUTH, next success -> CONNECTED")

    # ---- F4: rich flow re-mints + rebuilds before the write; write Bearer is NEW ----
    # A stateful transport that mints incrementally-numbered access tokens, each
    # with a life SHORTER than the skew window, so EVERY build_fresh refreshes.
    # jira_translate: build_fresh (fetch) -> token #1; build_fresh (write) -> token
    # #2. The search (fetch) carries token #1; the comment POST (write) token #2.
    _reregister_builtin_airtable()
    settings.set_many(
        {"oauth_atlassian_client_id": "atl-client", "oauth_atlassian_client_secret": "atl-secret"}
    )

    class _JiraFlowTransport(httpx.BaseTransport):
        def __init__(self):
            self.mint = 0
            self.search_bearer = None
            self.write_bearer = None

        def handle_request(self, req):
            p = req.url.path
            if p.endswith("/oauth/token") and req.method == "POST":
                self.mint += 1
                # expires_in below SKEW_SECONDS so the minted token is already
                # "stale" -> the next build_fresh refreshes again.
                return httpx.Response(
                    200,
                    json={
                        "access_token": f"AT-mint-{self.mint}",
                        "refresh_token": f"RT-mint-{self.mint}",
                        "expires_in": 30,
                        "token_type": "Bearer",
                    },
                )
            bearer = req.headers.get("Authorization", "")
            if p.endswith("/search/jql"):
                self.search_bearer = bearer
                return httpx.Response(
                    200,
                    json={
                        "issues": [
                            {"key": "LOC-1", "fields": {"summary": "Hello", "description": "World"}}
                        ],
                        "isLast": True,
                    },
                )
            if "/comment" in p and req.method == "POST":
                self.write_bearer = bearer
                return httpx.Response(201, json={"id": "10001"})
            return httpx.Response(200, json={})

    jflow = _JiraFlowTransport()
    expired_f4 = {
        "access_token": "AT-START",
        "refresh_token": "RT-START",
        "expires_at": now - 10,
        "token_type": "Bearer",
        "cloudid": "cloud-MATCH",
    }
    jconn = cstore.create(
        "jira",
        "F4 jira flow",
        {
            "base_url": "https://team.atlassian.net",
            "source_field": "summary",
            "target_locales": "fr-FR",
            "target_mode": "comment",
        },
        {},
        actor="admin",
    )
    cstore.set_oauth_secret(jconn.id, expired_f4)
    ok4, msg4, _summ4 = cstore.jira_translate(jconn.id, issue_keys=["LOC-1"], transport=jflow)
    assert ok4, ("jira_translate must succeed", msg4)
    assert jflow.mint >= 2, ("fetch + write phases must each re-mint a token", jflow.mint)
    assert jflow.search_bearer and jflow.write_bearer, (jflow.search_bearer, jflow.write_bearer)
    assert jflow.write_bearer != jflow.search_bearer, (
        "write phase must use a token minted AFTER the fetch, not the stale fetch token",
        jflow.search_bearer,
        jflow.write_bearer,
    )
    assert jflow.write_bearer == f"Bearer AT-mint-{jflow.mint}", (
        "write Bearer must be the newest minted token",
        jflow.write_bearer,
        jflow.mint,
    )
    print("  F4 rich flow: jira_translate re-mints before write; write Bearer is the NEW token")

    # ---- F5: callback error=access_denied -> 303 to /connectors/{kind}?msg= ----
    from fastapi.testclient import TestClient

    from app.main import app

    c = TestClient(app)
    # Seed the session with a bound kind by starting an OAuth flow first.
    settings.set_many({"oauth_airtable_client_id": "at-client", "oauth_airtable_client_secret": ""})
    c.post("/connectors/airtable/oauth/start", data={"name": "F5 flow"}, follow_redirects=False)
    r5 = c.get("/connectors/oauth/callback?error=access_denied", follow_redirects=False)
    assert r5.status_code == 303, r5.status_code
    loc = r5.headers.get("location", "")
    assert loc.startswith("/connectors/airtable?"), ("must land on the kind page", loc)
    assert "msg=" in loc, ("failure feedback msg must be present", loc)
    assert "access_denied" in loc, loc
    print("  F5 callback: error=access_denied -> 303 /connectors/{kind}?msg= (feedback preserved)")


# ===================== D7 (audit Cluster D): masked secret context ============
def test_connection_detail_secret_masked():
    """D7 (audit Cluster D, connectors.py connection_detail): the Jinja context
    must never carry the raw decrypted secret dict - an oauth sub-dict (real
    access/refresh tokens) or a future FieldSpec mismarking a real credential as
    secret=False must never be echoable through it. Fix: app.web.connectors
    builds a masked view (`_secret_display`, mirroring the engines.py `key_set`
    pattern) and passes THAT into the template instead of the raw dict.

    This is a standalone test (calls init_db() itself) so it runs correctly
    whether pytest collects it directly (`pytest tests/verify_connector_oauth.py`)
    or main() calls it as part of the full suite subprocess.

    Two angles:
      1. Unit-test `_secret_display` directly against a secret dict shaped like
         a real OAuth connection's (a top-level 'oauth' sub-dict with real
         tokens, alongside a plain non-secret field and a true secret field).
         Proves the masking function itself: drops 'oauth' entirely, reduces
         secret=True fields to a bool, and keeps secret=False field VALUES
         (unchanged UX - the edit form still prefills them, e.g. an account
         email).
      2. End-to-end via TestClient: an OAuth-mode connection's detail page must
         not contain the raw access_token/refresh_token strings anywhere in
         the rendered HTML; a token-mode connection's detail page must still
         echo its non-secret field (email) while never echoing its real
         secret value - proving the masked context doesn't break the existing
         leave-blank-to-keep edit-form UX (parity with the CLI, which never
         echoes secrets either).

    Proving this fails pre-fix: `_secret_display` does not exist before the D7
    fix (connection_detail passed the raw `secret` dict straight into the
    template context under the "secret" key) - importing it raises ImportError,
    and reverting the connectors.py change reproduces that failure directly.
    """
    import json as _json

    from fastapi.testclient import TestClient

    from app.db import init_db as _init_db
    from app.main import app
    from app.services.connectors.base import ConnectorSpec, FieldSpec
    from app.web.connectors import _secret_display

    _init_db()

    # ---- (1) unit test: the masking function itself ----
    fake_spec = ConnectorSpec(
        kind="fake",
        name="Fake",
        icon="",
        cat="",
        blurb="",
        secret_fields=[
            FieldSpec("email", "Account email", secret=False),
            FieldSpec("api_token", "API token", secret=True),
        ],
    )
    raw_secret = {
        "email": "person@example.com",
        "api_token": "REAL-TOKEN-VALUE",
        "auth_mode": "oauth",
        "oauth": {
            "access_token": "AT-RAW-SECRET",
            "refresh_token": "RT-RAW-SECRET",
            "expires_at": 0,
            "scope": "x",
        },
    }
    display = _secret_display(fake_spec, raw_secret)
    assert "oauth" not in display, "the raw oauth sub-dict must never reach the template context"
    assert display["email"] == "person@example.com", (
        "a genuinely non-secret field must still be echoed (edit-form prefill UX)"
    )
    assert display["api_token"] is True, (
        "a secret=True field must be reduced to a presence bool, never the value"
    )
    blob = _json.dumps(display)
    assert "REAL-TOKEN-VALUE" not in blob, "the real secret value leaked through the masked view"
    assert "AT-RAW-SECRET" not in blob and "RT-RAW-SECRET" not in blob, (
        "oauth tokens leaked through the masked view"
    )
    print(
        "  _secret_display: oauth sub-dict dropped, secret=True -> bool, secret=False -> value kept"
    )

    # ---- (2) end-to-end: the actual route + template ----
    c = TestClient(app)

    oauth_cid = _mk_oauth_conn(
        {
            "access_token": "AT-E2E-SECRET",
            "refresh_token": "RT-E2E-SECRET",
            "expires_at": int(time.time()) + 3600,
            "scope": "data.records:read",
            "token_type": "Bearer",
        },
        name="D7 oauth conn",
    )
    r = c.get(f"/connectors/connection/{oauth_cid}", follow_redirects=False)
    assert r.status_code == 200, r.status_code
    page = r.text
    assert "AT-E2E-SECRET" not in page and "RT-E2E-SECRET" not in page, (
        "raw oauth tokens must never appear in the rendered connection page"
    )

    token_conn = cstore.create(
        "confluence",
        "D7 token conn",
        {},
        {"email": "agent@corp.example", "api_token": "REAL-TOKEN-E2E"},
        actor="admin",
    )
    r2 = c.get(f"/connectors/connection/{token_conn.id}", follow_redirects=False)
    assert r2.status_code == 200, r2.status_code
    page2 = r2.text
    assert "agent@corp.example" in page2, (
        "a non-secret field (email) must still prefill in the edit form"
    )
    assert "REAL-TOKEN-E2E" not in page2, "the real secret token must never render in HTML"
    print("  connection_detail page: oauth tokens never render; non-secret fields still prefill")


if __name__ == "__main__":
    main()
