"""CMS via MCP (`mcp_cms`): OAuth 2.1 / MCP authorization. Fully offline.

One in-process fake WORLD behind a single httpx.MockTransport routes by host:
  * cms.test   a Strapi-like MCP server that answers 401 + `WWW-Authenticate: resource_metadata`
               until it sees the current access token, plus its protected resource metadata
  * auth.test  an authorization server: RFC 8414 metadata, optional dynamic client registration,
               PKCE S256 + `resource` checks on the code exchange, rotating refresh tokens

What it asserts:
  1  discovery, dynamic registration and the consent step through the EXISTING
     /connectors/oauth/callback route; the grant is stored encrypted with the facts a refresh needs
  2  a translate run with the OAuth token (only mapped tools called, bearer = current token)
  3  refresh through the single-flight path: one POST for two racing threads, rotated token kept,
     a failed refresh marks the connection REAUTH without a crash
  4  operator-supplied client id + secret (no registration endpoint), secret moved into the grant;
     no registration endpoint and no client id gives an actionable message
  5  SSRF / downgrade guard on EVERY discovered URL (metadata can point anywhere), issuer mix-up,
     resource mismatch, missing PKCE, redirects not followed, size cap, tampered stored endpoint
  6  credential pin (OAuth client id edit clears the grant), redaction (no token in logs/messages)
  7  presets: six vendor presets, all UNVERIFIED, mapping validity
  8  parity surfaces: connection page button, no create-here OAuth card, start refuses without a row
  9  gate 2026-10-10: RFC 9207 `iss` on the callback, posted base_url/mcp_url never steer discovery,
     preset URL used by discovery, RFC 8414 basic auth default, open initialize + metadata still
     authorizes, token audience check, every vendor preset labelled UNVERIFIED in the UI help

ASCII-only prints; final line is 'verify_mcp_oauth OK'.
"""

import html
import json
import logging
import os
import re
import sys
import threading
from urllib.parse import parse_qs, urlsplit

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(_ROOT)
sys.path.insert(0, _ROOT)
from tests._bootstrap import init_env

init_env("loctms_mcpoauth_")

import httpx
from fastapi.testclient import TestClient

from app.db import init_db
from app.main import app
from app.models import ConnStatus
from app.services import egress
from app.services.connectors import mcp_cms as M
from app.services.connectors import mcp_oauth, oauth
from app.services.connectors import store as cstore
from app.services.connectors.base import ConnectorError

MCP_URL = "https://cms.test/mcp"
PRM_URL = "https://cms.test/.well-known/oauth-protected-resource/mcp"
AS = "https://auth.test"
WWW_AUTH = f'Bearer resource_metadata="{PRM_URL}", scope="content:read content:write"'
SECRET_VALUES = []  # every token/secret the fake issued; must never reach logs or messages


def b64url_sha256(v: str) -> str:
    import base64
    import hashlib

    return base64.urlsafe_b64encode(hashlib.sha256(v.encode()).digest()).decode().rstrip("=")


class World:
    def __init__(self, *, dcr=True, auth_required=True):
        self.dcr = dcr
        self.auth_required = auth_required
        self.serve_prm = True
        self.basic_seen: list = []  # client ids that authenticated with HTTP Basic
        self.prm_overrides: dict = {}
        self.as_overrides: dict = {}
        self.as_status = 200
        self.as_redirect = ""
        self.big_as = False
        self.require_client_secret = ""  # token endpoint demands this client secret
        self.token_n = 0
        self.access = "AT-0"
        self.refresh_tokens: set = set()
        self.codes: dict = {}  # code -> (challenge, redirect_uri, client_id, resource)
        self.registered: list = []
        self.token_posts: list = []  # form dicts
        self.refresh_fail = False
        self.docs: dict = {
            "d1": {
                "en": {"documentId": "d1", "locale": "en", "title": "Hello", "body": "Pack light."}
            }
        }
        self.mcp_auth_seen: list = []
        self.calls: list = []
        self.requested: list = []  # every URL the world served

    def transport(self):
        return httpx.MockTransport(self.handle)

    # ------------------------------------------------------------------ AS
    def issue_code(self, authorize_url: str) -> str:
        q = {k: v[0] for k, v in parse_qs(urlsplit(authorize_url).query).items()}
        code = f"code-{len(self.codes) + 1}"
        self.codes[code] = (
            q["code_challenge"],
            q["redirect_uri"],
            q["client_id"],
            q.get("resource"),
        )
        return code

    def _new_tokens(self, with_refresh=True):
        self.token_n += 1
        self.access = f"AT-{self.token_n}"
        out = {"access_token": self.access, "token_type": "Bearer", "expires_in": 3600}
        SECRET_VALUES.append(self.access)
        if with_refresh:
            rt = f"RT-{self.token_n}"
            self.refresh_tokens.add(rt)
            SECRET_VALUES.append(rt)
            out["refresh_token"] = rt
        return out

    def _as_meta(self):
        meta = {
            "issuer": AS,
            "authorization_endpoint": f"{AS}/authorize",
            "token_endpoint": f"{AS}/token",
            "code_challenge_methods_supported": ["S256"],
            "grant_types_supported": ["authorization_code", "refresh_token"],
            "token_endpoint_auth_methods_supported": ["none", "client_secret_post"],
        }
        if self.dcr:
            meta["registration_endpoint"] = f"{AS}/register"
        meta.update(self.as_overrides)
        return {k: v for k, v in meta.items() if v is not None}

    def _token(self, req):
        form = {k: v[0] for k, v in parse_qs(req.content.decode()).items()}
        basic = req.headers.get("authorization") or ""
        if basic.startswith("Basic "):
            import base64
            from urllib.parse import unquote_plus

            cid_, _, sec_ = base64.b64decode(basic[6:]).decode().partition(":")
            self.basic_seen.append(unquote_plus(cid_))
            form["client_secret"] = unquote_plus(sec_)
        self.token_posts.append(form)
        if self.require_client_secret and form.get("client_secret") != self.require_client_secret:
            return httpx.Response(
                401, json={"error": "invalid_client", "error_description": "bad secret"}
            )
        if not form.get("resource"):
            return httpx.Response(400, json={"error": "invalid_target"})
        if form["grant_type"] == "authorization_code":
            rec = self.codes.pop(form.get("code"), None)
            if not rec:
                return httpx.Response(400, json={"error": "invalid_grant"})
            challenge, redirect, client_id, _res = rec
            if (
                b64url_sha256(form.get("code_verifier", "")) != challenge
                or form.get("redirect_uri") != redirect
                or form.get("client_id") != client_id
            ):
                return httpx.Response(400, json={"error": "invalid_grant"})
            return httpx.Response(200, json=self._new_tokens())
        if form["grant_type"] == "refresh_token":
            if self.refresh_fail or form.get("refresh_token") not in self.refresh_tokens:
                return httpx.Response(400, json={"error": "invalid_grant"})
            self.refresh_tokens.discard(form["refresh_token"])  # rotating
            return httpx.Response(200, json=self._new_tokens())
        return httpx.Response(400, json={"error": "unsupported_grant_type"})

    # ----------------------------------------------------------------- MCP
    def _rpc(self, req):
        msg = json.loads(req.content)
        method, rid = msg.get("method"), msg.get("id")
        sid = {"mcp-session-id": "s1"}

        def ok(result, headers=None):
            return httpx.Response(
                200, json={"jsonrpc": "2.0", "id": rid, "result": result}, headers=headers
            )

        def text(data):
            return ok({"content": [{"type": "text", "text": json.dumps(data)}]})

        if method == "initialize":
            return ok(
                {
                    "protocolVersion": "2025-06-18",
                    "capabilities": {},
                    "serverInfo": {"name": "oauth-cms"},
                },
                sid,
            )
        if rid is None:
            return httpx.Response(202)
        if method == "tools/list":
            props = {"locale": {"type": "string"}}
            tools = [
                {"name": n, "inputSchema": {"type": "object", "properties": props}}
                for n in ("list_page", "get_page", "update_page", "publish_page")
            ]
            return ok({"tools": tools})
        if method != "tools/call":  # e.g. the #754 server/discover probe: a legacy server
            return httpx.Response(
                200,
                json={"jsonrpc": "2.0", "id": rid, "error": {"code": -32601, "message": "nope"}},
            )
        a = msg["params"].get("arguments") or {}
        name = msg["params"]["name"]
        self.calls.append(name)
        loc = a.get("locale", "en")
        if name == "list_page":
            return text({"results": [dict(v[loc]) for v in self.docs.values() if loc in v]})
        if name == "get_page":
            v = self.docs.get(a.get("documentId"), {}).get(loc)
            if v is None:
                return ok(
                    {"content": [{"type": "text", "text": "Document not found."}], "isError": True}
                )
            return text({"data": v})
        if name == "update_page":
            cur = self.docs[a["documentId"]].setdefault(
                loc, {"documentId": a["documentId"], "locale": loc}
            )
            cur.update(a["data"])
            return text({"data": cur})
        return text({})

    def handle(self, req: httpx.Request):
        url = str(req.url)
        self.requested.append(url)
        host, path = req.url.host, req.url.path
        if host == "cms.test":
            if path == "/.well-known/oauth-protected-resource/mcp" and self.serve_prm:
                doc = {
                    "resource": MCP_URL,
                    "authorization_servers": [AS],
                    "scopes_supported": ["content:read", "content:write"],
                }
                doc.update(self.prm_overrides)
                return httpx.Response(200, json=doc)
            if path == "/mcp" and req.method == "POST":
                auth = req.headers.get("authorization")
                self.mcp_auth_seen.append(auth)
                if self.auth_required and auth != f"Bearer {self.access}":
                    return httpx.Response(
                        401,
                        headers={"www-authenticate": WWW_AUTH},
                    )
                return self._rpc(req)
            if path == "/mcp" and req.method == "DELETE":
                return httpx.Response(200)
        if host == "auth.test":
            if path == "/.well-known/oauth-authorization-server":
                if self.as_redirect:
                    return httpx.Response(302, headers={"location": self.as_redirect})
                if self.big_as:
                    return httpx.Response(200, content=b'{"x":"' + b"a" * 400_000 + b'"}')
                if self.as_status != 200:
                    return httpx.Response(self.as_status)
                return httpx.Response(200, json=self._as_meta())
            if path == "/register" and req.method == "POST":
                body = json.loads(req.content)
                assert body["token_endpoint_auth_method"] == "none", body
                self.registered.append(body)
                return httpx.Response(
                    201, json={"client_id": "dcr-client-1", "client_secret": "must-be-ignored"}
                )
            if path == "/token" and req.method == "POST":
                return self._token(req)
        return httpx.Response(404)


def _raises(fn, needle):
    try:
        fn()
    except ConnectorError as e:
        assert needle in str(e), (needle, str(e))
        return str(e)
    raise AssertionError(f"expected ConnectorError containing {needle!r}")


_CFG = {
    "mcp_url": MCP_URL,
    "preset": "strapi",
    "content_type": "page",
    "fields": "title, body",
    "source_locale": "en",
    "target_locales": "fr",
    "locales": "en, fr",
}


def _make(name, extra=None, secret=None):
    cfg = dict(_CFG)
    cfg.update(extra or {})
    return cstore.create("mcp_cms", name, cfg, secret or {}, actor="admin")


def _consent(tc, world, cid, iss=None, extra=None):
    """Run the real start route, 'approve' at the fake AS, hit the real callback."""
    data = {"cid": str(cid), "name": "x"}
    data.update(extra or {})
    r = tc.post("/connectors/mcp_cms/oauth/start", data=data, follow_redirects=False)
    assert r.status_code == 200, (r.status_code, r.text[:300])
    link = html.unescape(re.search(r'<a href="([^"]+)"', r.text).group(1))
    code = world.issue_code(link)
    q = {k: v[0] for k, v in parse_qs(urlsplit(link).query).items()}
    url = f"/connectors/oauth/callback?code={code}&state={q['state']}"
    if iss is not None:
        from urllib.parse import quote

        url += "&iss=" + quote(iss, safe="")
    cb = tc.get(url, follow_redirects=False)
    return link, q, cb


def main() -> None:
    init_db()
    records: list = []

    class _H(logging.Handler):
        def emit(self, rec):
            records.append(self.format(rec))

    h = _H()
    logging.getLogger().addHandler(h)
    tc = TestClient(app)

    # ---- (1) discovery + DCR + consent through the existing callback route ---------------
    w = World()
    oauth.set_default_transport(w.transport())
    c1 = _make("dcr")
    link, q, cb = _consent(tc, w, c1.id)
    assert link.startswith(f"{AS}/authorize?"), link
    assert q["code_challenge_method"] == "S256" and q["resource"] == MCP_URL, q
    assert q["client_id"] == "dcr-client-1" and q["scope"] == "content:read content:write", q
    assert q["redirect_uri"].endswith("/connectors/oauth/callback"), q
    assert len(w.registered) == 1 and w.registered[0]["redirect_uris"] == [q["redirect_uri"]]
    assert cb.status_code == 303 and f"/connectors/connection/{c1.id}" in cb.headers["location"], (
        cb.headers
    )
    conn = cstore.get(c1.id)
    assert conn.status == ConnStatus.CONNECTED and cstore.is_oauth(conn)
    sec = cstore.dec_secret(conn.secret_enc)
    og = sec["oauth"]
    assert og["access_token"] == "AT-1" and og["refresh_token"] == "RT-1" and og["expires_at"] > 0
    assert og["mcp"]["token_endpoint"] == f"{AS}/token" and og["mcp"]["client_id"] == "dcr-client-1"
    assert "client_secret" not in og["mcp"], "a registered public client keeps no secret"
    assert "AT-1" not in (conn.secret_enc or ""), "stored encrypted"
    tok_form = w.token_posts[0]
    assert tok_form["resource"] == MCP_URL and "client_secret" not in tok_form, tok_form
    print("  (1) discovery + DCR + PKCE/resource + callback: grant stored encrypted, CONNECTED")

    # a replayed callback cannot finish twice
    cb2 = tc.get("/connectors/oauth/callback?code=code-1&state=zzz", follow_redirects=False)
    assert cb2.status_code in (303, 404) and "AT-" not in cb2.text
    print("  (1b) callback replay / wrong state refused")

    # ---- (2) translate with the OAuth token ----------------------------------------------
    ok, msg, res = cstore.mcp_cms_translate(c1.id, actor="admin", transport=w.transport())
    assert ok and res["created"] == 1, (msg, res)
    assert w.docs["d1"]["fr"]["title"].startswith("[fr]")
    assert set(w.calls) <= {"list_page", "get_page", "update_page"}, w.calls
    assert w.mcp_auth_seen[-1] == "Bearer AT-1"
    ok, msg = cstore.test_connection(c1.id, actor="admin", transport=w.transport())
    assert ok, msg
    print("  (2) translate + test() run on the OAuth bearer; only mapped tools called")

    # ---- (3) refresh: single flight, rotation, failure -> REAUTH -----------------------------
    def expire(cid):
        s = cstore.dec_secret(cstore.get(cid).secret_enc)
        s["oauth"]["expires_at"] = 1
        cstore.set_oauth_secret(cid, s["oauth"])

    expire(c1.id)
    before = len([f for f in w.token_posts if f["grant_type"] == "refresh_token"])
    results = []

    def race():
        results.append(cstore.ensure_fresh_token(c1.id, transport=w.transport()))

    ts = [threading.Thread(target=race) for _ in range(2)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    refreshes = [f for f in w.token_posts if f["grant_type"] == "refresh_token"]
    assert len(refreshes) - before == 1, "two racing threads, one refresh POST"
    assert refreshes[-1]["refresh_token"] == "RT-1" and refreshes[-1]["resource"] == MCP_URL
    og = cstore.dec_secret(cstore.get(c1.id).secret_enc)["oauth"]
    assert og["access_token"] == "AT-2" and og["refresh_token"] == "RT-2", og
    assert og["mcp"]["client_id"] == "dcr-client-1" and og["mcp"]["token_endpoint"] == f"{AS}/token"
    ok, msg, res = cstore.mcp_cms_translate(c1.id, actor="admin", transport=w.transport())
    assert ok and w.mcp_auth_seen[-1] == "Bearer AT-2", (msg, w.mcp_auth_seen[-1])
    expire(c1.id)
    w.refresh_fail = True
    out = cstore.ensure_fresh_token(c1.id, transport=w.transport())
    assert out["oauth"]["access_token"] == "AT-2", "stale secret returned, no crash"
    assert cstore.get(c1.id).status == ConnStatus.REAUTH
    w.refresh_fail = False
    print("  (3) refresh: 1 POST for 2 threads, rotation kept, failure -> REAUTH")

    # ---- (4) operator client id + secret; no registration endpoint ---------------------------
    w4 = World(dcr=False)
    w4.require_client_secret = "client-SECRET-xyz"  # pregate: allow-secret offline fake value
    SECRET_VALUES.append(w4.require_client_secret)
    oauth.set_default_transport(w4.transport())
    c4a = _make("no creds")
    r = tc.post(
        "/connectors/mcp_cms/oauth/start", data={"cid": str(c4a.id)}, follow_redirects=False
    )
    assert (
        r.status_code == 303 and "no%20dynamic%20client%20registration" in r.headers["location"]
    ), r.headers
    assert "oauth/callback" in r.headers["location"], (
        "the message names the callback URL to register"
    )
    c4 = _make(
        "operator client",
        {"oauth_client_id": "console-client"},
        {"oauth_client_secret": w4.require_client_secret},
    )
    link, q, cb = _consent(tc, w4, c4.id)
    assert q["client_id"] == "console-client" and not w4.registered
    assert cb.status_code == 303, cb.headers
    og4 = cstore.dec_secret(cstore.get(c4.id).secret_enc)["oauth"]
    assert og4["mcp"]["client_secret"] == w4.require_client_secret, "secret moved into the grant"
    assert w4.token_posts[0]["client_secret"] == w4.require_client_secret
    expire(c4.id)
    cstore.ensure_fresh_token(c4.id, transport=w4.transport())
    assert (
        w4.token_posts[-1]["grant_type"] == "refresh_token" and w4.token_posts[-1]["client_secret"]
    )
    assert cstore.dec_secret(cstore.get(c4.id).secret_enc)["oauth"]["access_token"] == "AT-2"
    # wrong secret -> clean failure, no echo of the secret
    c4b = _make(
        "wrong secret", {"oauth_client_id": "console-client"}, {"oauth_client_secret": "nope"}
    )
    link, q, cb = _consent(tc, w4, c4b.id)
    assert "Token exchange" in cb.headers["location"] or "msg=" in cb.headers["location"]
    assert "nope" not in cb.headers["location"] and "bad%20secret" not in cb.headers["location"]
    assert not cstore.is_oauth(cstore.get(c4b.id))
    print("  (4) operator client id/secret used and refreshed; missing DCR explained")

    # a server that does not ask for auth AND publishes no metadata has nothing to authorize
    w_open = World(auth_required=False)
    w_open.serve_prm = False
    oauth.set_default_transport(w_open.transport())
    c_open = _make("open")
    r = tc.post(
        "/connectors/mcp_cms/oauth/start", data={"cid": str(c_open.id)}, follow_redirects=False
    )
    assert r.status_code == 303 and "nothing%20to%20authorize" in r.headers["location"], r.headers
    assert not w_open.registered and not w_open.token_posts

    # ---- (5) SSRF and metadata attacks ------------------------------------------------------
    egress.set_test_resolver(
        lambda host: ["10.0.0.5"] if host == "private.test" else ["93.184.216.34"]
    )
    try:

        def disc(world=None, url=MCP_URL):
            wd = world or World()
            return mcp_oauth.discover(url, wd.transport())

        meta = disc()
        assert meta["token_endpoint"] == f"{AS}/token" and meta["resource"] == MCP_URL
        for bad, needle in (
            ("http://169.254.169.254/", "downgrade"),
            ("https://169.254.169.254/", "Blocked target"),
            ("https://localhost/as", "Blocked target"),
            ("https://private.test/as", "Blocked target"),
            ("http://auth.test", "downgrade"),
            ("https://user:pw@auth.test", "credentials"),
            ("file:///etc/passwd", "http(s)"),
            ("ftp://auth.test", "http(s)"),
        ):
            wb = World()
            wb.prm_overrides = {"authorization_servers": [bad]}
            _raises(lambda wb=wb: disc(wb), needle)
        for key, bad, needle in (
            ("token_endpoint", "http://auth.test/token", "downgrade"),
            ("token_endpoint", "https://10.1.2.3/token", "Blocked target"),
            ("authorization_endpoint", "https://localhost/authorize", "Blocked target"),
            ("authorization_endpoint", "https://private.test/authorize", "Blocked target"),
            ("registration_endpoint", "http://169.254.169.254/register", "downgrade"),
            ("registration_endpoint", "https://169.254.169.254/register", "Blocked target"),
            ("token_endpoint", "https://auth.test/token#frag", "fragment"),
            ("authorization_endpoint", "javascript:alert(1)", "http(s)"),
        ):
            wb = World()
            wb.as_overrides = {key: bad}
            _raises(lambda wb=wb: disc(wb), needle)
        wb = World()
        wb.as_overrides = {"issuer": "https://evil.test"}
        _raises(lambda: disc(wb), "different issuer")
        wb = World()
        wb.as_overrides = {"code_challenge_methods_supported": ["plain"]}
        _raises(lambda: disc(wb), "PKCE S256")
        wb = World()
        wb.as_overrides = {"code_challenge_methods_supported": None}
        _raises(lambda: disc(wb), "PKCE S256")
        wb = World()
        wb.prm_overrides = {"resource": "https://other.test/mcp"}
        _raises(lambda: disc(wb), "different resource")
        wb = World()
        wb.prm_overrides = {"resource": None}
        _raises(lambda: disc(wb), "names no resource")
        wb = World()
        wb.prm_overrides = {"scopes_supported": None}
        wb.as_overrides = {"scopes_supported": ["admin:everything"]}
        assert disc(wb)["scopes_supported"] == [], "AS scopes are never a fallback"
        wb = World()
        wb.as_redirect = "https://auth.test/elsewhere"
        _raises(lambda: disc(wb), "publishes no usable metadata")
        assert "https://auth.test/elsewhere" not in wb.requested, "redirects are never followed"
        wb = World()
        wb.big_as = True
        _raises(lambda: disc(wb), "larger than 256 KiB")
        # a private MCP URL is refused unless allowlisted (same guard as every connector)
        _raises(
            lambda: mcp_oauth.discover("https://private.test/mcp", World().transport()),
            "Blocked target",
        )
        _raises(
            lambda: mcp_oauth.discover("http://127.0.0.1/mcp", World().transport()),
            "Blocked target",
        )

        # a WWW-Authenticate pointing the metadata at a private host is refused too
        class PrivatePrm(World):
            def handle(self, req):
                if req.url.host == "cms.test" and req.url.path == "/mcp":
                    return httpx.Response(
                        401,
                        headers={
                            "www-authenticate": 'Bearer resource_metadata="https://private.test/prm"'
                        },
                    )
                return super().handle(req)

        _raises(lambda: disc(PrivatePrm()), "Blocked target")
        # a stored token endpoint that was tampered with is re-checked on refresh: no request
        wt = World()
        facts = {
            "token_endpoint": "https://private.test/token",
            "resource": MCP_URL,
            "client_id": "c",
            "auth_methods": [],
        }
        res_, err = mcp_oauth.refresh({"refresh_token": "RT", "mcp": facts}, wt.transport())
        assert res_ is None and "Blocked target" in err and not wt.requested, (err, wt.requested)
        facts["token_endpoint"] = "http://auth.test/token"
        res_, err = mcp_oauth.refresh({"refresh_token": "RT", "mcp": facts}, wt.transport())
        assert res_ is None and "downgrade" in err and not wt.requested, err
    finally:
        egress.set_test_resolver(None)
    print("  (5) SSRF: private/loopback/metadata/downgrade/credentials/fragment refused")
    print("      issuer mix-up, resource mismatch, no PKCE, redirects, size cap, tampered endpoint")

    # ---- (6) pin + redaction ------------------------------------------------------------------
    w6 = World()
    oauth.set_default_transport(w6.transport())
    c6 = _make("pin")
    _consent(tc, w6, c6.id)

    def has_grant(cid):
        return bool((cstore.dec_secret(cstore.get(cid).secret_enc) or {}).get("oauth"))

    assert has_grant(c6.id)
    cstore.update(c6.id, "pin", {"target_locales": "fr, es"}, None, actor="admin")
    assert has_grant(c6.id), "a non-mapping edit keeps the grant"
    cstore.update(c6.id, "pin", {"oauth_client_id": "other-client"}, None, actor="admin")
    assert not has_grant(c6.id), "a different OAuth client clears the grant"
    c6b = _make("pin url")
    _consent(tc, w6, c6b.id)
    cstore.update(c6b.id, "pin url", {"mcp_url": "https://elsewhere.test/mcp"}, None, actor="admin")
    assert not has_grant(c6b.id), "a new MCP host clears the grant"
    logging.getLogger().removeHandler(h)
    blob = "\n".join(records)
    for s in SECRET_VALUES:
        assert s not in blob, f"{s} reached a log line"
        for cid in (c1.id, c4.id, c4b.id, c6.id):
            conn = cstore.get(cid)
            assert s not in json.dumps(conn.config) and s not in (conn.last_message or ""), s
    print(
        "  (6) pin: client id / host change clears the grant; no token or secret in logs/messages"
    )

    # ---- (7) presets ---------------------------------------------------------------------------
    six = (
        "aem-fragments",
        "sitecore-marketer",
        "document360",
        "notion",
        "google-workspace-docs",
        "microsoft-work-iq",
    )
    for name in six:
        p = M.PRESETS[name]
        assert p["unverified"] is True and p["notes"], name
        m = M.McpCmsConnector({"preset": name, "mcp_url": MCP_URL}, {}).mapping()
        assert m["unverified"] is True
    for name in ("aem-fragments", "sitecore-marketer", "document360"):
        m = M.McpCmsConnector({"preset": name}, {}).mapping()
        assert m["get"] and (m["write"] or name == "aem-fragments"), name
        # guessed argument names are refused at run time until the operator supplies them
        c = M.McpCmsConnector({"preset": name}, {})
        try:
            c._need("get")
            raise AssertionError("guessed preset args must be refused")
        except ConnectorError as e:
            assert "unverified" in str(e), e
        ok_c = M.McpCmsConnector(
            {"preset": name, "get_args": {"id": "{{id}}", "lang": "{{locale}}"}}, {}
        )
        assert ok_c._need("get")["tool"] == m["get"]["tool"]
    assert M.PRESETS["aem-fragments"].get("write_tool") is None
    for name in ("notion", "google-workspace-docs", "microsoft-work-iq"):
        m = M.McpCmsConnector({"preset": name}, {}).mapping()
        assert m["get"] is None and m["write"] is None, name
    assert not M.PRESETS["strapi"].get("unverified")
    wp = World()
    mc = M.McpCmsConnector(
        {"preset": "sitecore-marketer", "mcp_url": MCP_URL, "fields": "title"},
        {"auth_mode": "oauth", "oauth": {"access_token": "AT-0"}},
        transport=wp.transport(),
    )
    ok, msg = mc.test()
    assert not ok and "UNVERIFIED" in msg and "not offered" in msg, msg
    assert M.McpCmsConnector({"preset": "notion"}, {})._url() == "https://mcp.notion.com/mcp"
    print("  (7) six vendor presets, all UNVERIFIED, test() says so and checks tools/list")

    # ---- (8) parity ----------------------------------------------------------------------------
    page = tc.get(f"/connectors/connection/{c4a.id}").text
    assert "Connect with OAuth" in page and "/connectors/mcp_cms/oauth/start" in page
    form = tc.get("/connectors/mcp_cms").text
    assert "OAuth client id" in form and "OAuth client secret" in form
    assert 'data-card="oauth"' not in form, (
        "OAuth needs an existing connection, no create-here card"
    )
    n = len(cstore.list_connections("mcp_cms"))
    r = tc.post(
        "/connectors/mcp_cms/oauth/start",
        data={"name": "new", "base_url": MCP_URL},
        follow_redirects=False,
    )
    assert r.status_code == 303 and "Create%20the%20connection%20first" in r.headers["location"]
    assert len(cstore.list_connections("mcp_cms")) == n, "no row minted by the OAuth start"
    print(
        "  (8) connection page button, form fields, no create-here OAuth card, start mints no row"
    )

    _gate_checks(tc)
    _client_758_checks(tc)
    oauth.set_default_transport(None)
    print("verify_mcp_oauth OK")


def _gate_checks(tc) -> None:
    """(9) Review gate 2026-10-10 findings, each proved red on the pre-gate head."""

    def grant(cid):
        return (cstore.dec_secret(cstore.get(cid).secret_enc) or {}).get("oauth")

    # 9a RFC 9207: the AS promises `iss`; a callback without it, or naming another issuer, is
    # refused BEFORE the code exchange (no token POST, no grant).
    w = World()
    w.as_overrides = {"authorization_response_iss_parameter_supported": True}
    oauth.set_default_transport(w.transport())
    for label, iss in (("no iss", None), ("wrong iss", "https://evil.test")):
        c = _make(f"iss {label}")
        _l, _q, cb = _consent(tc, w, c.id, iss=iss)
        assert cb.status_code == 303 and "issuer" in cb.headers["location"], (label, cb.headers)
        assert not w.token_posts and not grant(c.id), label
    c = _make("iss ok")
    _l, _q, cb = _consent(tc, w, c.id, iss=AS)
    assert cb.status_code == 303 and grant(c.id), cb.headers
    # not advertised: absent is fine, but a present iss must still match
    w2 = World()
    oauth.set_default_transport(w2.transport())
    c = _make("iss unadvertised wrong")
    _l, _q, cb = _consent(tc, w2, c.id, iss="https://evil.test")
    assert "issuer" in cb.headers["location"] and not w2.token_posts and not grant(c.id)
    print("  (9a) RFC 9207 iss: required when advertised, must match when present")

    # 9b a posted base_url / mcp_url never changes the connection or where discovery goes
    w = World()
    oauth.set_default_transport(w.transport())
    c = _make("posted url")
    before = dict(cstore.get(c.id).config)
    link, _q, cb = _consent(
        tc,
        w,
        c.id,
        extra={"base_url": "https://evil.test/mcp", "mcp_url": "https://evil.test/mcp"},
    )
    assert cstore.get(c.id).config == before, "the reconnect form rewrote config"
    hosts = {urlsplit(u).hostname for u in w.requested}
    assert hosts <= {"cms.test", "auth.test"}, hosts
    assert link.startswith(f"{AS}/authorize?") and grant(c.id)
    print("  (9b) posted base_url/mcp_url ignored: config unchanged, traffic only to saved hosts")

    # 9c a preset connection with a blank mcp_url discovers at the preset's URL (the same URL
    # the connector sends the token to), not at an empty string
    w = World()
    oauth.set_default_transport(w.transport())
    egress.set_test_resolver(lambda host: ["93.184.216.34"])
    try:
        c = cstore.create(
            "mcp_cms", "preset url", {"preset": "notion", "mcp_url": ""}, {}, actor="admin"
        )
        r = tc.post(
            "/connectors/mcp_cms/oauth/start", data={"cid": str(c.id)}, follow_redirects=False
        )
    finally:
        egress.set_test_resolver(None)
    assert r.status_code == 303 and "usable%20URL" not in r.headers["location"], r.headers
    assert any(urlsplit(u).hostname == "mcp.notion.com" for u in w.requested), w.requested
    print("  (9c) blank mcp_url + preset: discovery goes to the preset URL")

    # 9d RFC 8414: no token_endpoint_auth_methods_supported means client_secret_basic, with
    # the id and secret form-urlencoded first (RFC 6749 2.3.1)
    w = World(dcr=False)
    w.as_overrides = {"token_endpoint_auth_methods_supported": None}
    w.require_client_secret = "s3cret:with&odd=chars"  # pregate: allow-secret offline fake value
    SECRET_VALUES.append(w.require_client_secret)
    oauth.set_default_transport(w.transport())
    c = _make(
        "basic default",
        {"oauth_client_id": "console:client"},
        {"oauth_client_secret": w.require_client_secret},
    )
    _l, _q, cb = _consent(tc, w, c.id)
    assert grant(c.id), cb.headers
    assert w.basic_seen == ["console:client"], w.basic_seen
    assert w.token_posts[0]["client_secret"] == w.require_client_secret  # from the header
    assert mcp_oauth._auth_method({"auth_methods": []}, True) == "client_secret_basic"
    assert (
        mcp_oauth._auth_method({"auth_methods": ["client_secret_post"]}, True)
        == "client_secret_post"
    )
    print("  (9d) RFC 8414 default auth method is client_secret_basic, urlencoded")

    # 9e an open initialize does not hide a published protected resource metadata document
    w = World(auth_required=False)
    oauth.set_default_transport(w.transport())
    c = _make("open init, protected tools")
    _l, _q, cb = _consent(tc, w, c.id)
    assert grant(c.id), cb.headers
    print("  (9e) 200 on initialize + published metadata: OAuth still offered")

    # 9f audience: a grant recorded for another resource is never sent to this URL
    mc = M.McpCmsConnector(
        {"mcp_url": MCP_URL},
        {
            "auth_mode": "oauth",
            "oauth": {"access_token": "AT-x", "mcp": {"resource": "https://other.test/mcp"}},
        },
    )
    _raises(mc._auth_headers, "different MCP server")
    ok = M.McpCmsConnector(
        {"mcp_url": MCP_URL},
        {"auth_mode": "oauth", "oauth": {"access_token": "AT-x", "mcp": {"resource": MCP_URL}}},
    )
    assert ok._auth_headers() == {"Authorization": "Bearer AT-x"}
    print("  (9f) token audience checked against the connection URL before every send")

    # 9g every vendor preset is labelled UNVERIFIED where operators read it
    page = html.unescape(tc.get("/connectors/mcp_cms").text)
    for name, p in M.PRESETS.items():
        if p.get("unverified"):
            assert p["notes"].startswith("UNVERIFIED"), name
            assert f"'{name}' (UNVERIFIED" in page, name
    g = M.PRESETS["google-workspace-docs"]["notes"]
    assert "NO dynamic client registration" in g and "client id" in g and "secret" in g
    print("  (9g) every vendor preset says UNVERIFIED in the form help; Google needs a client")


def _client_758_checks(tc) -> None:
    """(10) #758: exact issuer comparison, application_type on DCR, Client ID Metadata Docs."""
    from app.services import settings

    def grant(cid):
        return (cstore.dec_secret(cstore.get(cid).secret_enc) or {}).get("oauth")

    # 10a iss is compared by simple string comparison: no trailing-slash normalization
    facts = {"iss_value": AS, "issuer": AS}
    mcp_oauth.check_iss(facts, AS)
    for bad in (AS + "/", AS.upper(), "https://auth.test:443"):
        _raises(lambda b=bad: mcp_oauth.check_iss(facts, b), "different issuer")
    _raises(lambda: mcp_oauth.check_iss({"iss_value": AS + "/"}, AS), "different issuer")
    # ... and the metadata issuer must be IDENTICAL to the one the well-known URL was built from
    w = World()
    w.as_overrides = {"issuer": AS + "/"}
    _raises(lambda: mcp_oauth.discover(MCP_URL, w.transport()), "different issuer")
    w = World()
    w.prm_overrides = {"authorization_servers": [AS + "/"]}
    _raises(lambda: mcp_oauth.discover(MCP_URL, w.transport()), "different issuer")
    # an error response naming another issuer is refused without showing its error code
    w = World()
    oauth.set_default_transport(w.transport())
    c = _make("iss on error")
    r = tc.post("/connectors/mcp_cms/oauth/start", data={"cid": str(c.id)}, follow_redirects=False)
    link = html.unescape(re.search(r'<a href="([^"]+)"', r.text).group(1))
    q = {k: v[0] for k, v in parse_qs(urlsplit(link).query).items()}
    cb = tc.get(
        "/connectors/oauth/callback?error=access_denied&state="
        + q["state"]
        + "&iss=https%3A%2F%2Fevil.test",
        follow_redirects=False,
    )
    loc = cb.headers["location"]
    assert "issuer" in loc and "access_denied" not in loc, loc
    print("  (10a) iss and metadata issuer compared exactly; error responses checked too")

    # 10b DCR sends application_type: web for a remote callback, native for loopback
    w = World()
    oauth.set_default_transport(w.transport())
    c = _make("dcr app type")
    _l, _q, cb = _consent(tc, w, c.id)
    assert grant(c.id), cb.headers
    assert w.registered and w.registered[-1].get("application_type") == "web", w.registered
    assert mcp_oauth.application_type("http://127.0.0.1:8000/connectors/oauth/callback") == "native"
    assert mcp_oauth.application_type("http://localhost:8000/cb") == "native"
    assert mcp_oauth.application_type("https://tms.example.com/connectors/oauth/callback") == "web"
    print("  (10b) dynamic registration carries application_type (web / native)")

    # 10c Client ID Metadata Document: preferred over DCR when the AS advertises it and
    # app_base_url is a public https URL; the client id is the document URL, exactly
    base = "https://tms.example.com"
    doc_url = base + "/connectors/oauth/client-metadata.json"
    assert tc.get("/connectors/oauth/client-metadata.json").status_code == 404
    settings.set_many({"app_base_url": base})
    try:
        r = tc.get("/connectors/oauth/client-metadata.json")
        assert r.status_code == 200, r.status_code
        doc = r.json()
        assert doc["client_id"] == doc_url, doc
        assert doc["redirect_uris"] == [base + "/connectors/oauth/callback"], doc
        assert doc["client_name"] and doc["token_endpoint_auth_method"] == "none", doc
        assert doc["application_type"] == "web", doc
        w = World()
        w.as_overrides = {"client_id_metadata_document_supported": True}
        oauth.set_default_transport(w.transport())
        c = _make("cimd")
        link, q, cb = _consent(tc, w, c.id)
        assert q["client_id"] == doc_url, q
        assert q["redirect_uri"] == base + "/connectors/oauth/callback", q
        assert not w.registered, "CIMD advertised: no dynamic registration"
        assert grant(c.id) and grant(c.id)["mcp"]["client_id"] == doc_url
        # not advertised: dynamic registration as before
        w = World()
        oauth.set_default_transport(w.transport())
        c = _make("cimd not advertised")
        _l, q, _cb = _consent(tc, w, c.id)
        assert w.registered and q["client_id"] == "dcr-client-1", q
        # pre-registered client id still wins over CIMD
        w = World()
        w.as_overrides = {"client_id_metadata_document_supported": True}
        oauth.set_default_transport(w.transport())
        c = _make("pre-registered", {"oauth_client_id": "console-client"})
        _l, q, _cb = _consent(tc, w, c.id)
        assert q["client_id"] == "console-client" and not w.registered, q
    finally:
        settings.set_many({"app_base_url": ""})
    # an http (or absent) app_base_url can never be a client id: DCR fallback
    assert not mcp_oauth.cimd_usable("http://tms.example.com/x.json", "http://tms.example.com/cb")
    assert not mcp_oauth.cimd_usable("https://tms.example.com", "https://tms.example.com/cb")
    assert not mcp_oauth.cimd_usable("https://localhost/x.json", "https://localhost/cb")
    w = World()
    w.as_overrides = {"client_id_metadata_document_supported": True}
    oauth.set_default_transport(w.transport())
    c = _make("cimd no base")
    _l, q, _cb = _consent(tc, w, c.id)
    assert w.registered and q["client_id"] == "dcr-client-1", q
    print("  (10c) CIMD preferred when advertised; document served; DCR fallback otherwise")


if __name__ == "__main__":
    main()
