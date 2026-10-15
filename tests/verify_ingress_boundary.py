"""Ingress path, proxy trust and local credential regressions (offline).

Documented in: docs/architecture/identity-and-access.md
"""

import asyncio
import io
import ipaddress
import logging
import os
import re
import shutil
import subprocess
import sys
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from tests._bootstrap import init_env
from tests._piece_patch import patch_pieces

init_env("loctms_ingress_")

from fastapi.testclient import TestClient
from sqlmodel import Session
from starlette._utils import get_route_path
from starlette.requests import Request
from starlette.responses import Response

from app import config
from app.db import engine, init_db
from app.main import (
    HostBoundaryMiddleware,
    _audit_mutating_request,
    _feature_gate,
    _security_audit,
    app,
)
from app.models import Customer
from app.services import apikeys, audit, providers, security, settings
from app.web import route_path as routing
from tests._csrf import csrf_for

SECRET = "invented-private-ingress-key-12345"


async def forbidden(scope, receive, send):
    raise AssertionError("routing reached")


def _detail_response():
    """Test the DB-free detail builder; public health no longer exposes these fields."""
    import httpx

    from app.web.health import _health_details

    return httpx.Response(200, content=_health_details(request("/health")).body)


def test_high1_remote_host_lock():
    _remote_lock("security_allowed_hosts", "attacker.invalid")


def _remote_lock(key, value):
    settings.set_many({"remote_admin_enabled": "1", key: ""})
    u = security.get_by_email("ingress-admin")
    _, token = apikeys.create_key(u.id, "immutable", scopes="api docs admin")
    c = TestClient(app)
    try:

        def post(k, v):
            return c.post(
                "/api/admin/config",
                headers={"X-Loc-Token": token},
                json={"settings": {k: v}, "confirm": {k: True}},
            )

        reference = post("audit_enabled", "0")
        result = post(key, value)
        assert result.status_code == reference.status_code == 400, result.text
        assert result.json()["error"] == reference.json()["error"].replace("audit_enabled", key)
        assert settings.get(key) == ""
    finally:
        settings.set_many({key: "", "remote_admin_enabled": "0"})


def test_med1_proxy_lock_and_prefixes():
    _remote_lock("trusted_proxy_ips", "10.0.0.0/8")
    for value in ("0.0.0.0/0", "::/0", "10.0.0.0/7", "2001:db8::/31"):
        try:
            settings.validate_value("trusted_proxy_ips", value)
        except settings.InvalidSettingValue as exc:
            assert "/8" in str(exc) and "/32" in str(exc)
        else:
            raise AssertionError(f"broad proxy trust accepted: {value}")


def test_med2_malformed_identity():
    # 2026-09-20: this used to assert through the back-office _ip_allowed reader as well.
    # The subject was always security.client_ip returning "" for a malformed identity, and
    # that is what the fail-closed behaviour rested on; the allowlist was one consumer of
    # it and went with the console.
    settings.set_many({"trusted_proxy_ips": "198.51.100.0/24"})
    try:
        with patch.object(config, "fly_app_name", ""):
            for raw in (
                "junk",
                "203.0.113.9, junk",
                "[2001:db8::9]",
                "203.0.113.9,",
                "203.0.113.9, ,198.51.100.25",
                "203.0.113.9:0",
                "203.0.113.9:65536",
                "[2001:db8::9]:bad",
                "",
                # #289: the detail keeps the RIGHT end of an over-long chain, so the
                # control character and quote sit at the right to stay in the evidence.
                "x" * 1500 + 'junk\n"',
            ):
                req = request(method="POST", headers=[(b"x-forwarded-for", raw.encode())])
                assert security.client_ip(req) == "", raw
                _audit_mutating_request(req)
                row = audit.recent()[0]
                assert row.ip == "" and "malformed-x-forwarded-for=" in row.detail
                assert "\n" not in row.detail and len(row.detail) < 1200
                if "\n" in raw:
                    assert "\\n" in row.detail and '\\"' in row.detail
            for raw, expected in (
                ("203.0.113.9:1234", "203.0.113.9"),
                ("[2001:db8::9]:443", "2001:db8::9"),
                ("[::ffff:203.0.113.9]:443", str(ipaddress.IPv6Address("::ffff:203.0.113.9"))),
            ):
                assert (
                    security.client_ip(request(headers=[(b"x-forwarded-for", raw.encode())]))
                    == expected
                )
            assert security.client_ip(request(headers=[])) == "198.51.100.20"
            assert (
                TestClient(app).get("/health", headers={"X-Forwarded-For": "junk"}).status_code
                == 200
            )
    finally:
        settings.set_many({"trusted_proxy_ips": ""})


def test_med3_root_path():
    from app.db import WriteContentionError
    from app.main import _write_contention_error
    from app.web.deps import _error_page

    settings.set_many({"routing_enabled": "0", "security_auth_required": "1"})

    async def reachable(req):
        return Response("reachable")

    try:
        for prefix in ("", "/tms"):
            req = request(prefix + "/routing", root_path="/tms")
            assert asyncio.run(_feature_gate(req, reachable)).status_code == 404
            req = request(prefix + "/api/projects", root_path="/tms")
            assert asyncio.run(_security_audit(req, reachable)).status_code == 200
            assert _error_page(req, 503, "busy").media_type == "application/json"
            response = asyncio.run(_write_contention_error(req, WriteContentionError()))
            assert response.status_code == 503 and response.media_type == "application/json"
            req = request(prefix + "/customers", root_path="/tms", method="POST")
            _audit_mutating_request(req)
            assert audit.recent()[0].action == "POST /customers"
            assert TestClient(app, root_path="/tms").get(prefix + "/routing").status_code == 404
    finally:
        settings.set_many({"routing_enabled": "1"})


def test_med4_loopback_recovery():
    async def accepted(scope, receive, send):
        await Response("reachable")(scope, receive, send)

    with patch.object(config, "SECURITY_ALLOWED_HOSTS", "wrong.invalid"):
        for peer in ("127.0.0.1", "127.33.44.55", "::1", "198.51.100.20"):
            for path in ("/health", "/ready", "/auth/login", "/api/admin/config"):
                messages = []

                async def send(message, messages=messages):
                    messages.append(message)

                scope = request(path, client=(peer, 80)).scope
                asyncio.run(HostBoundaryMiddleware(accepted)(scope, None, send))
                assert messages[0]["status"] == (200 if path in ("/health", "/ready") else 400)
                messages.clear()
                scope["headers"] = [(b"host", b"bad/path")]
                asyncio.run(HostBoundaryMiddleware(forbidden)(scope, None, send))
                assert messages[0]["status"] == 400


def test_local_ingress_env_precedence():
    settings.set_many({"security_allowed_hosts": "stored.invalid", "trusted_proxy_ips": "10.0.0.1"})
    try:
        with (
            patch.dict(
                os.environ,
                {
                    "LOC_TMS_SECURITY_ALLOWED_HOSTS": "recovered.test",
                    "LOC_TMS_TRUSTED_PROXY_IPS": "198.51.100.0/24",
                },
            ),
            patch.object(config, "fly_app_name", ""),
        ):
            settings.apply_to_config()
            assert (
                config.SECURITY_ALLOWED_HOSTS
                == settings.get("security_allowed_hosts")
                == "recovered.test"
            )
            req = request(headers=[(b"x-forwarded-for", b"203.0.113.9")])
            assert security.client_ip(req) == "203.0.113.9"
        with patch.dict(
            os.environ, {"LOC_TMS_SECURITY_ALLOWED_HOSTS": "", "LOC_TMS_TRUSTED_PROXY_IPS": ""}
        ):
            settings.apply_to_config()
            assert config.SECURITY_ALLOWED_HOSTS == "" and settings.get("trusted_proxy_ips") == ""
    finally:
        settings.set_many({"security_allowed_hosts": "", "trusted_proxy_ips": ""})


def test_med5_saml_proxy_warning():
    from app.services import saml

    message = "SAML behind a proxy needs trusted_proxy_ips; X-Forwarded-Proto is not honored"
    logs = io.StringIO()
    handler = logging.StreamHandler(logs)
    handler.setLevel(logging.WARNING)
    logging.getLogger("loc_tms").addHandler(handler)
    try:
        with patch.object(saml, "available", return_value=True):
            for enabled, fly, proxies, expected in (
                ("1", "", "", True),
                ("0", "", "", False),
                ("1", "fly-test", "", True),
                ("1", "", "10.0.0.1", False),
            ):
                settings.set_many({"saml_enabled": enabled, "trusted_proxy_ips": proxies})
                with patch.object(config, "fly_app_name", fly):
                    saml.configure_at_boot()
                    response = _detail_response()
                    assert response.status_code == 200
                    assert (response.json()["detail"].get("saml_proxy") == message) == expected
            assert message in logs.getvalue()
    finally:
        logging.getLogger("loc_tms").removeHandler(handler)
        settings.set_many({"saml_enabled": "0", "trusted_proxy_ips": ""})
        saml.configure_at_boot()


def test_low1_websocket_host():
    messages = []

    async def send(message):
        messages.append(message)

    scope = request(host="bad/path").scope
    scope["type"] = "websocket"
    asyncio.run(HostBoundaryMiddleware(forbidden)(scope, None, send))
    assert messages == [{"type": "websocket.close", "code": 1008}]
    messages.clear()
    scope["headers"] = [(b"host", b"other.test")]
    with patch.object(config, "SECURITY_ALLOWED_HOSTS", "pinned.test"):
        asyncio.run(HostBoundaryMiddleware(forbidden)(scope, None, send))
    assert messages == [{"type": "websocket.close", "code": 1008}]
    messages.clear()

    async def accepted(scope, receive, send):
        await send({"type": "websocket.accept"})
        await send({"type": "websocket.close", "code": 1000})

    with patch.object(config, "SECURITY_ALLOWED_HOSTS", "other.test"):
        asyncio.run(HostBoundaryMiddleware(accepted)(scope, None, send))
    assert messages == [{"type": "websocket.accept"}, {"type": "websocket.close", "code": 1000}]


def test_med5_forwarding_preserves_socket():
    import uvicorn

    assert '"--no-proxy-headers"' in Path("Dockerfile").read_text()
    observed = []

    async def endpoint(scope, receive, send):
        observed.append((scope["scheme"], scope["client"][0], security.client_ip(Request(scope))))
        await Response("served")(scope, receive, send)

    settings.set_many({"trusted_proxy_ips": "198.51.100.0/24"})
    try:
        with (
            patch.object(config, "fly_app_name", ""),
            patch.dict(os.environ, {"FORWARDED_ALLOW_IPS": "198.51.100.0/24"}),
        ):
            server = uvicorn.Config(HostBoundaryMiddleware(endpoint), proxy_headers=False)
            server.load()
            for header, identity in (
                (b"junk", ""),
                (b"203.0.113.9:443", "203.0.113.9"),
                (b"[2001:db8::9]:443", "2001:db8::9"),
            ):
                scope = request(
                    headers=[
                        (b"host", b"testserver"),
                        (b"x-forwarded-proto", b"https"),
                        (b"x-forwarded-for", header),
                    ]
                ).scope

                async def send(message):
                    pass

                asyncio.run(server.loaded_app(scope, None, send))
                assert observed[-1] == ("https", "198.51.100.20", identity)
            with patch.object(config, "SECURITY_ALLOWED_HOSTS", "other.test"):
                messages = []

                async def send(message):
                    messages.append(message)

                scope = request(
                    headers=[(b"host", b"testserver"), (b"x-forwarded-for", b"127.0.0.1")]
                ).scope
                asyncio.run(server.loaded_app(scope, None, send))
                assert messages[0]["status"] == 400
    finally:
        settings.set_many({"trusted_proxy_ips": ""})


def test_access_log_client_identity():
    """The server's access log names the one-authority client IP, not the proxy.

    uvicorn runs with --no-proxy-headers, and its access line reads the SERVER scope's
    "client". The app resolves security.client_ip (trusted_proxy_ips walk) and the host
    boundary copies it there at response start. The app's own scope keeps the socket
    peer, and a forged chain from an untrusted peer never reaches the log.
    """
    import inspect

    import uvicorn
    from uvicorn.protocols.http import h11_impl, httptools_impl

    from app.main import ACCESS_LOG_CLIENT

    for module in (h11_impl, httptools_impl):
        assert "get_client_addr(self.scope)" in inspect.getsource(module), module.__name__
    seen = []
    real_client_ip = security.client_ip

    def spy(req):
        seen.append((req.client.host, ACCESS_LOG_CLIENT in req.scope))
        return real_client_ip(req)

    settings.set_many({"trusted_proxy_ips": "198.51.100.0/24"})
    try:
        with (
            patch.object(config, "fly_app_name", ""),
            patch.object(config, "TRUSTED_PROXY_IPS", "198.51.100.0/24"),
            patch.object(security, "client_ip", spy),
        ):
            server = uvicorn.Config(app, proxy_headers=False)
            server.load()
            for path, peer, chain, logged in (
                ("/customers", "198.51.100.20", b"203.0.113.9", "203.0.113.9"),
                ("/api/customers", "198.51.100.20", b"203.0.113.10", "203.0.113.10"),
                ("/auth/login", "198.51.100.20", b"2001:db8::9", "2001:db8::9"),
                # Untrusted peer: its forged chain is ignored; the socket peer is logged.
                ("/customers", "192.0.2.5", b"203.0.113.9", "192.0.2.5"),
                # Malformed chain has no identity: keep the socket peer, never the chain.
                ("/customers", "198.51.100.20", b"junk", "198.51.100.20"),
                # Liveness never reads settings; it keeps the socket peer.
                ("/health", "198.51.100.20", b"203.0.113.9", "198.51.100.20"),
            ):
                scope = request(
                    path=path,
                    client=(peer, 4321),
                    headers=[(b"host", b"testserver"), (b"x-forwarded-for", chain)],
                ).scope
                scope.pop("session")
                messages = []

                async def receive():
                    return {"type": "http.request", "body": b"", "more_body": False}

                async def send(message, messages=messages):
                    messages.append(message)

                seen.clear()
                asyncio.run(server.loaded_app(scope, receive, send))
                assert messages and messages[0]["type"] == "http.response.start", path
                assert scope["client"] == (logged, 4321), (path, scope["client"])
                # Every app-side read still saw the socket peer (decisions unchanged).
                assert all(host == peer for host, _ in seen), (path, seen)
                if path != "/health":
                    assert seen and seen[0][1], (path, seen)
    finally:
        settings.set_many({"trusted_proxy_ips": ""})


def test_low2_parser_and_probe_case():
    from app.web.admin import _local_probe_key

    for key, value in (
        ("trusted_proxy_ips", "10.0.0.0/8,"),
        ("security_allowed_hosts", "example.test,"),
    ):
        settings.validate_value(key, value)
    with patch.object(config, "SECURITY_ALLOWED_HOSTS", "testserver,"):
        assert TestClient(app).get("/health").status_code == 200
    with patch.object(config, "SECURITY_ALLOWED_HOSTS", ", ,"):
        assert TestClient(app).get("/health").status_code == 200
    with (
        patch.object(config, "LOCAL_BASE_URL", "http://engine.test:1234/v1"),
        patch.object(config, "LOCAL_API_KEY", SECRET),
    ):
        assert _local_probe_key("HTTP://ENGINE.TEST:1234/v1", "") == SECRET
        for url in (
            "http://evil.test/v1",
            "http://engine.test:1234/V1",
            "http://user@engine.test:1234/v1",
        ):
            assert _local_probe_key(url, "") != SECRET


def test_low3_docs_placement():
    cli = Path("docs/CLI.md").read_text(encoding="utf-8")
    llm = Path("app/capabilities/llm.md").read_text(encoding="utf-8")
    assert "security_allowed_hosts" in cli[cli.index("### Config") : cli.index("### Enterprise")]
    assert "trusted_proxy_ips" in llm[llm.index("## Authenticating") : llm.index("## The HTTP API")]
    for filename in ("docs/DEPLOY.md", "docs/architecture/deployment.md"):
        content = Path(filename).read_text(encoding="utf-8")
        assert "LOC_TMS_SECURITY_ALLOWED_HOSTS" in content and "restart" in content.lower()


def test_low4_boundary_assertions():
    import inspect

    source = inspect.getsource(test_host_and_route_matrix)
    assert 'patch("app.main._security_audit"' not in source
    assert "async def forbidden" not in source


def login(email="ingress-admin"):
    c = TestClient(app)
    csrf_for(c)
    r = c.post(
        "/auth/login",
        data={"email": email, "password": "ingress-password-123"},
        follow_redirects=False,
    )
    assert r.status_code == 303, r.text
    csrf_for(c)
    return c


def request(path="/customers", host="testserver", **extra):
    scope = {
        "type": "http",
        "http_version": "1.1",
        "scheme": "http",
        "method": "GET",
        "path": path,
        "root_path": "",
        "query_string": b"",
        "headers": [(b"host", host.encode())],
        "client": ("198.51.100.20", 1234),
        "server": ("testserver", 80),
        "session": {},
    }
    scope.update(extra)
    return Request(scope)


def test_path_wall():
    async def private(req):
        return Response("AUDIT_SYNTHETIC_PRIVATE_CUSTOMER")

    settings.set_many({"security_auth_required": "1"})
    for path, host in (
        ("/customers", "testserver/health?probe="),
        ("/projects", "testserver/api?probe="),
    ):
        r = asyncio.run(_security_audit(request(path, host), private))
        assert r.status_code == 303, (path, r.status_code)
        assert b"AUDIT_SYNTHETIC_PRIVATE_CUSTOMER" not in r.body


def test_proxy_trust():
    from app.web.deps import _ip

    req = request(headers=[(b"fly-client-ip", b"192.0.2.7")])
    settings.set_many({"trusted_proxy_ips": ""})
    with patch.object(config, "fly_app_name", ""):
        assert security.client_ip(req) == "198.51.100.20"
    with patch.object(config, "fly_app_name", "fly-test"):
        assert security.client_ip(req) == "198.51.100.20"
        settings.set_many({"trusted_proxy_ips": "198.51.100.20"})
        assert security.client_ip(req) == "192.0.2.7"
        assert _ip(req) == "192.0.2.7"
    settings.set_many({"trusted_proxy_ips": "10.0.0.0/8,2001:db8::/32"})
    with patch.object(config, "fly_app_name", ""):
        for peer, expected in (("10.1.2.3", "192.0.2.7"), ("198.51.100.20", "198.51.100.20")):
            req = request(
                client=(peer, 80),
                headers=[(b"x-forwarded-for", b"192.0.2.99, 192.0.2.7, 10.0.0.2")],
            )
            assert security.client_ip(req) == expected
        bad = request(client=("10.0.0.1", 80), headers=[(b"x-forwarded-for", b"garbage")])
        assert security.client_ip(bad) == ""
        # #289: garbage LEFT of the client is client-controlled and ignored; the first
        # untrusted hop walked from the right (192.0.2.7) is the identity.
        malformed_left = request(
            client=("10.0.0.1", 80), headers=[(b"x-forwarded-for", b"garbage, 192.0.2.7, 10.0.0.2")]
        )
        assert security.client_ip(malformed_left) == "192.0.2.7"
    settings.set_many({"trusted_proxy_ips": ""})


def test_288_fly_peer_boundary():
    """The review probe's rotating Fly header cannot buy fresh login budgets."""
    from app.web.deps import _ip

    observed = []

    async def endpoint(scope, receive, send):
        req = Request(scope)
        observed.append((scope["scheme"], security.client_ip(req), _ip(req)))
        await Response("served")(scope, receive, send)

    async def send(message):
        pass

    settings.set_many({"trusted_proxy_ips": "198.51.100.20"})
    try:
        with patch.object(config, "fly_app_name", "fly-test"):
            boundary = HostBoundaryMiddleware(endpoint)
            for peer in ("127.0.0.1", "::1", "100.64.0.2", "fdaa:0:1:a7b:1::2"):
                email = f"fly-probe-{peer}@example.org"
                allowed = 0
                for i in range(200):
                    req = request(
                        client=(peer, 5555),
                        headers=[
                            (b"host", b"testserver"),
                            (b"fly-client-ip", f"203.0.113.{i}".encode()),
                            (b"x-forwarded-for", f"203.0.113.{i}".encode()),
                            (b"x-forwarded-proto", b"https"),
                        ],
                    )
                    ip = security.client_ip(req)
                    if security.reserve_login_attempt(email, ip):
                        allowed += 1
                        security.settle_login_failure(email, ip)
                    asyncio.run(boundary(req.scope, None, send))
                    assert observed[-1] == ("http", peer, peer), observed[-1]
                assert allowed == 5, allowed
                req.scope["method"] = "POST"
                assert _audit_mutating_request(req, status=401)
                assert audit.recent()[0].ip == peer
                security.clear_login_failures(email, peer)
            for raw, expected in ((b"192.0.2.7", "192.0.2.7"), (b"junk", "198.51.100.20")):
                req = request(
                    headers=[
                        (b"host", b"testserver"),
                        (b"fly-client-ip", raw),
                        (b"x-forwarded-proto", b"https"),
                    ]
                )
                asyncio.run(boundary(req.scope, None, send))
                assert observed[-1] == ("https", expected, expected), observed[-1]
            settings.set_many({"trusted_proxy_ips": ""})
            asyncio.run(boundary(req.scope, None, send))
            assert observed[-1] == ("http", "198.51.100.20", "198.51.100.20")
    finally:
        settings.set_many({"trusted_proxy_ips": ""})


def test_289_malformed_left_ignored():
    """#289: one malformed XFF token left of the client no longer blanks the address.

    The chain is walked from the right, one hop at a time, skipping trusted proxies;
    the first untrusted, well-formed hop is the client. Tokens left of it are
    client-controlled and never parsed. A malformed hop in a position only a trusted
    proxy writes (at or right of the client) still has no identity, and the audit
    detail keeps the right end of an over-long chain."""
    settings.set_many({"trusted_proxy_ips": "172.30.0.10/32,173.245.48.0/20"})
    try:
        with patch.object(config, "fly_app_name", ""):
            peer = ("172.30.0.10", 80)
            # The reviewer's probe: the two malformed-LEFT chains now resolve to the
            # real client instead of "" (garbage left of the client is ignored).
            for raw, expected in (
                ("198.51.100.7, 173.245.48.5", "198.51.100.7"),
                ("203.0.113.1, 198.51.100.7, 173.245.48.5", "198.51.100.7"),
                ("nope, 198.51.100.7, 173.245.48.5", "198.51.100.7"),
                ("x" * 130 + ", 198.51.100.7, 173.245.48.5", "198.51.100.7"),
            ):
                req = request(client=peer, headers=[(b"x-forwarded-for", raw.encode())])
                assert security.client_ip(req) == expected, raw
            # A malformed hop BETWEEN the client and the edge is a position only a
            # trusted proxy writes: the chain cannot be trusted, so no identity.
            req = request(
                client=peer,
                headers=[(b"x-forwarded-for", b"198.51.100.7, nope, 173.245.48.5")],
            )
            assert security.client_ip(req) == ""
            # An absent header still gives the peer.
            assert security.client_ip(request(client=peer, headers=[])) == "172.30.0.10"
            # An all-trusted chain keeps today's result (the left-most hop).
            req = request(
                client=peer, headers=[(b"x-forwarded-for", b"173.245.48.6, 173.245.48.5")]
            )
            assert security.client_ip(req) == "173.245.48.6"
            # The sign-in lockout key for the malformed-left case uses the real client
            # address, not "" (the old behaviour) and not the proxy: keys no longer
            # collapse, so the lockout still tracks the caller. (#384: the key is a
            # digest now, so the pin compares two keys rather than reading one back.)
            req = request(
                client=peer,
                headers=[(b"x-forwarded-for", b"nope, 198.51.100.7, 173.245.48.5")],
            )
            assert security._lockout_key("a@b.test", security.client_ip(req)) == (
                security._lockout_key("a@b.test", "198.51.100.7")
            )
            assert security._lockout_key("a@b.test", "") != security._lockout_key(
                "a@b.test", "198.51.100.7"
            )
            # The audit detail keeps the RIGHT end of an over-long chain (where the
            # proxies and the real client sit), still bounded and marked truncated.
            raw = "x" * 130 + ", 198.51.100.7, nope, 173.245.48.5"
            req = request(client=peer, headers=[(b"x-forwarded-for", raw.encode())])
            assert security.client_ip(req) == ""
            detail = security.client_ip_detail(req)
            assert detail.startswith("malformed-x-forwarded-for=")
            assert "198.51.100.7" in detail and "173.245.48.5" in detail, detail
            assert "(truncated)" in detail, detail
    finally:
        settings.set_many({"trusted_proxy_ips": ""})


def test_local_key_echo():
    settings.set_many({"security_auth_required": "1", "local_api_key": SECRET})
    settings.apply_to_config()
    r = login().get("/setup-ai")
    assert r.status_code == 200
    assert SECRET not in r.text
    assert 'id="lkey" value=""' in r.text and "key set" in r.text


def test_host_and_route_matrix():
    settings.set_many({"security_auth_required": "1", "security_allowed_hosts": ""})
    c = TestClient(app)
    for path in ("/customers", "/projects"):
        r = c.get(path, follow_redirects=False)
        assert r.status_code == 303 and r.headers["location"] == f"/auth/login?next={path}"
        for host in (
            "testserver/health?probe=",
            "testserver/api?probe=",
            "test server",
            "testserver?x",
            "testserver#x",
            "",
            "testserver:bad",
            "user@testserver",
        ):
            r = c.get(path, headers={"Host": host}, follow_redirects=False)
            assert r.status_code == 400, (host, r.status_code)
            assert "AUDIT_SYNTHETIC_PRIVATE_CUSTOMER" not in r.text
    boundary = TestClient(HostBoundaryMiddleware(forbidden))
    assert boundary.get("/", headers={"Host": "bad/path"}).status_code == 400
    settings.set_many({"security_allowed_hosts": "example.test"})
    assert c.get("/health", headers={"Host": "other.test"}).status_code == 200
    assert c.get("/health", headers={"Host": "example.test:8443"}).status_code == 200
    settings.set_many({"security_allowed_hosts": ""})
    assert c.get("/health").status_code == 200
    assert c.get("/%63ustomers", follow_redirects=False).headers["location"] == (
        "/auth/login?next=/customers"
    )
    for label, headers in (
        ("missing HTTP/1.1", []),
        ("duplicate", [(b"host", b"testserver"), (b"host", b"other.test")]),
        ("malformed NUL", [(b"host", b"bad\x00host")]),
        ("malformed tab", [(b"host", b"bad\thost")]),
    ):
        messages = []

        async def send(message, messages=messages):
            messages.append(message)

        asyncio.run(HostBoundaryMiddleware(forbidden)(request(headers=headers).scope, None, send))
        assert messages[0]["status"] == 400, label
        assert messages[-1]["body"] == b"Invalid Host", (label, messages)

    async def accepted(scope, receive, send):
        await Response("accepted")(scope, receive, send)

    no_host_10 = request(headers=[]).scope
    no_host_10["http_version"] = "1.0"
    messages = []

    async def send_10(message):
        messages.append(message)

    asyncio.run(HostBoundaryMiddleware(accepted)(no_host_10, None, send_10))
    assert messages[0]["status"] == 200 and messages[-1]["body"] == b"accepted", messages

    settings.set_many({"security_allowed_hosts": "example.test"})
    forged = c.get("/auth/login", headers={"Host": "attacker.invalid"})
    assert forged.status_code == 400 and forged.text == "Invalid Host", (
        forged.status_code,
        forged.text,
    )
    settings.set_many({"security_allowed_hosts": ""})
    assert c.get("/health", headers={"Host": "[::1]:8443"}).status_code == 200

    async def probe(scope, receive, send):
        await Response("alive")(scope, receive, send)

    with patch.object(settings, "get", side_effect=AssertionError("boundary touched settings DB")):
        assert TestClient(HostBoundaryMiddleware(probe)).get("/health").status_code == 200
    r = c.get("/health", headers={"Host": "bad/path"})
    assert r.status_code == 400 and r.headers["x-content-type-options"] == "nosniff"
    assert c.get("/auth/login").status_code == 200
    assert c.get("/static/not-present.css", follow_redirects=False).status_code == 404

    async def public(req):
        return Response("public")

    for path in ("/health", "/static/a.css", "/auth/login", "/api/projects", "/metrics"):
        assert asyncio.run(_security_audit(request(path), public)).status_code == 200
    assert c.get("/api/projects").status_code == 401
    u = security.get_by_email("ingress-admin")
    _, token = apikeys.create_key(u.id, "ingress", scopes="api docs admin")
    assert c.get("/api/projects", headers={"X-Loc-Token": token}).status_code == 200
    admin = login()
    assert admin.get("/customers").status_code == 200
    assert "AUDIT_SYNTHETIC_PRIVATE_CUSTOMER" in admin.get("/customers").text
    assert admin.get("/users").status_code == 200
    assert login("ingress-linguist").get("/users").status_code == 403


def test_canonical_routing_and_audit():
    for path, root, expected in (
        ("/customers", "", "/customers"),
        ("/back-office/login", "/back-office", "/back-office/login"),
        ("/login", "/back-office", "/back-office/login"),
        ("/a%2Fb", "", "/a%2Fb"),
        ("/a/b", "", "/a/b"),
    ):
        req = request(path, "testserver/health?", root_path=root)
        assert security.request_path(req) == expected
        assert security.request_path(req) == root + get_route_path(req.scope)
        assert routing.route_path(req.scope) == get_route_path(req.scope)
    settings.set_many({"security_auth_required": "0", "routing_enabled": "0"})

    async def reachable(req):
        return Response("reachable")

    req = request("/routing", "testserver/health?")
    assert asyncio.run(_feature_gate(req, reachable)).status_code == 404
    assert (
        TestClient(app).get("/routing", headers={"Host": "testserver/health?"}).status_code == 400
    )
    req = request("/customers", "testserver/health?", method="POST")
    _audit_mutating_request(req)
    assert audit.recent()[0].action == "POST /customers"
    settings.set_many({"routing_enabled": "1"})


def test_key_read_save_transport_and_logs():
    from app.cli.__main__ import main as cli_main
    from app.web import admin as web_admin
    from app.web.api_cli import _masked_setting

    settings.set_many(
        {
            "security_auth_required": "1",
            "local_api_key": SECRET,
            "local_base_url": "http://engine.test:1234",
            "remote_admin_enabled": "1",
        }
    )
    logs = io.StringIO()
    handler = logging.StreamHandler(logs)
    logging.getLogger().addHandler(handler)
    try:
        output = io.StringIO()
        with redirect_stdout(output):
            assert cli_main(["config", "get", "local_api_key"]) == 0
        assert SECRET not in output.getvalue() and "(set)" in output.getvalue()
        # Generic config reads are explicitly local-only on this checkout.
        output = io.StringIO()
        with redirect_stdout(output):
            assert (
                cli_main(
                    [
                        "--remote",
                        "http://127.0.0.1",
                        "--token",
                        "unused",
                        "config",
                        "get",
                        "local_api_key",
                    ]
                )
                != 0
            )
        assert SECRET not in output.getvalue()
        assert _masked_setting("local_api_key") == "(set)"
        u = security.get_by_email("ingress-admin")
        _, token = apikeys.create_key(u.id, "ingress-read", scopes="api docs admin")
        r = TestClient(app).get("/api/admin/config", headers={"X-Loc-Token": token})
        assert r.status_code == 200 and SECRET not in r.text
        assert r.json()["settings"]["local_api_key"] == "(set)"
        # Real MCP handler and HTTP adapter, with only the transport replaced.
        import httpx

        sys.path.insert(0, str(Path("integrations/mcp-server").resolve()))
        import server as mcp_server

        def mcp_client():
            return httpx.AsyncClient(transport=httpx.ASGITransport(app=app))

        with (
            patch.object(mcp_server, "_client", mcp_client),
            patch.object(mcp_server, "BASE_URL", "http://testserver"),
            patch.object(mcp_server, "TOKEN", token),
        ):
            result = asyncio.run(mcp_server.manage_install("get_config"))
        assert SECRET not in result and '"local_api_key": "(set)"' in result
        # Both new ingress settings inherit the real remote sensitive-key gate.
        for key, value in (
            ("security_allowed_hosts", "testserver"),
            ("trusted_proxy_ips", "10.0.0.0/8"),
        ):
            api = TestClient(app)
            headers = {"X-Loc-Token": token}
            body = {"settings": {key: value}}
            assert api.post("/api/admin/config", headers=headers, json=body).status_code == 400
            body["confirm"] = {key: True}
            assert api.post("/api/admin/config", headers=headers, json=body).status_code == 400
            settings.set_many({key: ""})
        c = login()
        data = {"base_url": config.LOCAL_BASE_URL, "api_key": "", "local_model": "mock-model"}
        assert c.post("/setup-ai/local", data=data, follow_redirects=False).status_code == 303
        assert settings.get("local_api_key") == SECRET

        def transport(req):
            assert req.headers["authorization"] == f"Bearer {SECRET}"
            return httpx.Response(200, json={"data": [{"id": "model-one"}]})

        # #354: model listing now goes through the door's own client, so inject the transport.
        assert providers.local_list_models(transport=httpx.MockTransport(transport)) == [
            "model-one"
        ]

        def completion_transport(req):
            assert req.headers["authorization"] == f"Bearer {SECRET}"
            return httpx.Response(
                200,
                json={
                    "id": "test",
                    "object": "chat.completion",
                    "created": 0,
                    "model": "mock-model",
                    "choices": [
                        {
                            "index": 0,
                            "message": {"role": "assistant", "content": "Bonjour"},
                            "finish_reason": "stop",
                        }
                    ],
                },
            )

        assert (
            providers.local_complete(
                None,
                None,
                "mock-model",
                "Hello",
                transport=httpx.MockTransport(completion_transport),
            )
            == "Bonjour"
        )
        with patch_pieces("app.web.admin", "local_complete", side_effect=RuntimeError(SECRET)):
            r = c.post("/setup-ai/local/test", data=data)
            assert r.status_code == 200 and SECRET not in r.text
        assert web_admin._local_probe_key("http://other.test", "") != SECRET
        data["clear_key"] = "1"
        assert c.post("/setup-ai/local", data=data, follow_redirects=False).status_code == 303
        assert settings.get("local_api_key") == "" and config.LOCAL_API_KEY == ""
        assert "no key" in c.get("/setup-ai").text
        assert SECRET not in logs.getvalue()
        assert all(SECRET not in row.detail for row in audit.recent())
    finally:
        logging.getLogger().removeHandler(handler)
        settings.set_many({"remote_admin_enabled": "0"})


def test_settings_validation_and_proxy_startup():
    for key, valid, invalid in (
        ("security_allowed_hosts", "example.test,[::1]", "evil/path"),
        ("trusted_proxy_ips", "10.0.0.0/8,::1", "10.0.0.0/999"),
    ):
        assert key in settings.SENSITIVE_KEYS and key in settings.DEFAULTS
        settings.validate_value(key, valid)
        try:
            settings.validate_value(key, invalid)
        except settings.InvalidSettingValue:
            pass
        else:
            raise AssertionError(f"{key} accepted malformed input")
    docker = Path("Dockerfile").read_text()
    entry = Path("docker-entrypoint.sh").read_text()
    assert "--forwarded-allow-ips=*" not in docker
    assert "LOC_TMS_FORWARDED_ALLOW_IPS" not in entry
    assert "app.proxy_config" not in entry
    assert "--no-proxy-headers" in entry
    shell = shutil.which("sh") or "C:/Program Files/Git/bin/bash.exe"
    block = entry[entry.index("# Disable uvicorn proxy headers") :]
    for key, value in (("TRUSTED_PROXY_IPS", "not-an-ip"), ("SECURITY_ALLOWED_HOSTS", "bad/host")):
        env = dict(os.environ, **{"LOC_TMS_" + key: value})
        out = subprocess.run(
            [shell, "-c", block, "entrypoint", "printf", "%s", "booted"],
            env=env,
            text=True,
            capture_output=True,
            check=True,
        )
        assert out.stdout.endswith("booted"), out.stdout


def test_r3_high1_outer_proxy():
    import uvicorn

    for enabled in (False, True):
        with patch.object(config, "SECURITY_ALLOWED_HOSTS", "pinned.test"):
            server = uvicorn.Config(
                HostBoundaryMiddleware(forbidden),
                proxy_headers=enabled,
                forwarded_allow_ips="198.51.100.0/24",
            )
            server.load()
            messages = []

            async def send(message, messages=messages):
                messages.append(message)

            scope = request(
                headers=[(b"host", b"other.test"), (b"x-forwarded-for", b"127.0.0.1")]
            ).scope
            asyncio.run(server.loaded_app(scope, None, send))
            assert messages[0]["status"] == 400


def test_r3_proxy_self_check():
    import uvicorn

    from app.web.health import _health_details

    observed = []

    async def endpoint(scope, receive, send):
        if scope["type"] == "lifespan":
            await send({"type": "lifespan.startup.complete"})
            return
        observed.append(security.client_ip(Request(scope)))
        await _health_details(Request(scope))(scope, receive, send)

    for enabled in (True, False):
        server = uvicorn.Config(
            HostBoundaryMiddleware(endpoint),
            proxy_headers=enabled,
            forwarded_allow_ips="*",
        )
        server.load()
        with (
            patch("app.main._logger.error") as error,
            patch.object(config, "fly_app_name", ""),
            patch.object(settings, "get", return_value="10.0.0.0/8"),
        ):

            async def boot_send(message):
                assert message["type"] == "lifespan.startup.complete"

            asyncio.run(server.loaded_app({"type": "lifespan"}, None, boot_send))
            assert error.call_count == int(enabled), error.call_args_list
            if enabled:
                error.assert_called_once_with(
                    "uvicorn proxy headers are ON; client identity is forgeable; "
                    "launch with --no-proxy-headers"
                )
            for _ in range(2):
                scope = request(
                    "/health",
                    client=("203.0.113.77", 5),
                    headers=[
                        (b"host", b"testserver"),
                        (b"x-forwarded-for", b"10.0.0.5, 198.51.100.9"),
                    ],
                ).scope

                messages = []

                async def send(message, messages=messages):
                    messages.append(message)

                asyncio.run(server.loaded_app(scope, None, send))
                assert messages[0]["status"] == 200
                assert observed[-1] == ("198.51.100.9" if enabled else "203.0.113.77")
                assert (
                    b"ingress_config: uvicorn proxy headers on" in messages[1]["body"]
                ) == enabled
            assert error.call_count == int(enabled), error.call_args_list


def test_r3_invalid_env():
    from app.cli.__main__ import main as cli_main
    from app.services import saml

    settings.set_many({"remote_admin_enabled": "1"})
    u = security.get_by_email("ingress-admin")
    _, token = apikeys.create_key(u.id, "invalid-env", scopes="api docs admin")
    try:
        for key, bad in (
            ("trusted_proxy_ips", "not-an-ip"),
            ("trusted_proxy_ips", "0.0.0.0/0"),
            ("security_allowed_hosts", "bad/host"),
        ):
            stored = "stored.test" if key == "security_allowed_hosts" else "198.51.100.0/24"
            settings.set_many({key: stored})
            with patch.dict(os.environ, {"LOC_TMS_" + key.upper(): bad}):
                with patch.object(settings.log, "error") as error:
                    assert settings.get(key) == ""
                    settings.apply_to_config()
                    assert error.called
                assert getattr(config, key.upper()) == ""
                with TestClient(app) as c:
                    response = c.get("/api/admin/config", headers={"X-Loc-Token": token})
                    assert response.status_code == 200, response.text
                    assert (
                        "ingress_config: invalid " + key
                        in (_detail_response().json()["detail"]["ingress_config"])
                    )
                with redirect_stdout(io.StringIO()):
                    assert cli_main(["config", "list"]) in (None, 0)
            settings.set_many({key: ""})
            settings.apply_to_config()
            assert _detail_response().json()["detail"]["ingress_config"] == "ok"
        # LOW-3 (gate round 11): a whole-body pin on the anonymous, host-allowlist-exempt
        # /health -- the key-set assertion the train-A merge dropped from verify_egress_policy
        # when detail became a map, with no suite left constraining the KEY SET of a route
        # that answers in every mode. Top level is exactly these three keys; detail carries
        # every train's word and no other (admission_daemon = train A daemon supervisor,
        # ingress_config = main P1a, egress_config = P4, ledger_envelopes = Wave C, saml_proxy
        # = train A), and the two unconditional keys are always present. A future train that
        # adds a field to this anonymous body must update this assertion.
        body = TestClient(app).get("/health").json()
        assert set(body) == {"status", "version"}, body
        assert body["status"] == "ok" and isinstance(body["version"], str), body
        body = _detail_response().json()
        assert set(body["detail"]) <= {
            "admission_daemon",
            "ingress_config",
            "egress_config",
            "ledger_envelopes",
            "saml_proxy",
        }, body["detail"]
        assert {"admission_daemon", "ingress_config"} <= set(body["detail"]), body["detail"]
        settings.set_many({"saml_enabled": "1", "trusted_proxy_ips": "0.0.0.0/0"})
        with (
            patch.object(saml, "available", return_value=True),
            patch.object(config, "fly_app_name", ""),
        ):
            saml.configure_at_boot()
            assert "SAML behind a proxy needs" in saml.PROXY_WARNING
    finally:
        settings.set_many(
            {"remote_admin_enabled": "0", "saml_enabled": "0", "trusted_proxy_ips": ""}
        )
        saml.configure_at_boot()


# test_r3_backoffice_root_path lived here and was REMOVED on 2026-09-20 with the
# operator console it drove. What it actually protected -- that the ingress boundary
# normalizes a stripped/non-stripping proxy root_path before a mount extends it -- is
# still covered by the /static and /api cases in this suite, which exercise the same
# app.web.route_path seam.


def test_r3_multiline_xff():
    settings.set_many({"trusted_proxy_ips": "198.51.100.0/24"})
    try:
        with patch.object(config, "fly_app_name", ""):
            for lines, expected in (
                (("9.9.9.9", "203.0.113.5"), "203.0.113.5"),
                (("9.9.9.9, 198.51.100.20", "203.0.113.5"), "203.0.113.5"),
                # junk RIGHT of the client is a proxy-written position: no identity.
                (("203.0.113.5", "junk"), ""),
                # #289: junk LEFT of the client is client-controlled and ignored.
                (("junk", "203.0.113.5"), "203.0.113.5"),
            ):
                req = request(headers=[(b"x-forwarded-for", line.encode()) for line in lines])
                assert security.client_ip(req) == expected, lines
                if not expected:
                    assert "junk" in security.client_ip_detail(req)
    finally:
        settings.set_many({"trusted_proxy_ips": ""})


def _assert_documented_launches(content, filename):
    launches = [line for line in content.splitlines() if "uvicorn app.main:app" in line]
    assert launches, filename
    for line in launches:
        assert "--no-proxy-headers" in line, (filename, line)


def test_r4_launch_docs_nonvacuous():
    for content in ("no launch example", "uvicorn app.main:app --port 8000"):
        try:
            _assert_documented_launches(content, "synthetic.md")
        except AssertionError:
            pass
        else:
            raise AssertionError(f"unchecked documentation: {content}")
    _assert_documented_launches("uvicorn app.main:app --no-proxy-headers", "synthetic.md")


def test_r4_probe_paths():
    from urllib.parse import urlsplit

    from app.main import _HOST_ALLOWLIST_EXEMPT

    assert {"/health", "/ready"} == _HOST_ALLOWLIST_EXEMPT
    docker = Path("Dockerfile").read_text()
    healthcheck = re.search(r"(?m)^HEALTHCHECK .*?(?=\n\n)", docker, re.S)
    assert healthcheck, "Dockerfile HEALTHCHECK missing"
    urls = re.findall(r"http://[^\s]+", healthcheck[0])
    assert urls, "Dockerfile HEALTHCHECK URL missing"
    assert all(urlsplit(url).path in _HOST_ALLOWLIST_EXEMPT for url in urls)

    async def endpoint(scope, receive, send):
        await Response("served")(scope, receive, send)

    with patch.object(config, "SECURITY_ALLOWED_HOSTS", "pinned.test"):
        client = TestClient(HostBoundaryMiddleware(endpoint))
        for path in ("/health", "/ready", "/health/", "/ready/", "/health/more", "/metrics"):
            expected = 200 if path in _HOST_ALLOWLIST_EXEMPT else 400
            assert client.get(path, headers={"host": "127.0.0.1:8000"}).status_code == expected
            assert client.get(path, headers={"host": "bad/host"}).status_code == 400


def test_r3_launch_flags():
    import ast

    for filename in ("run.py", "scripts/qa_server.py"):
        tree = ast.parse(Path(filename).read_text())
        calls = [
            n
            for n in ast.walk(tree)
            if isinstance(n, ast.Call)
            and isinstance(n.func, ast.Attribute)
            and n.func.attr == "run"
            and isinstance(n.func.value, ast.Name)
            and n.func.value.id == "uvicorn"
        ]
        assert calls, filename
        assert all(
            any(
                k.arg == "proxy_headers"
                and isinstance(k.value, ast.Constant)
                and k.value.value is False
                for k in call.keywords
            )
            for call in calls
        ), filename
    assert '"--no-proxy-headers"' in Path("Dockerfile").read_text()
    entry = Path("docker-entrypoint.sh").read_text()
    block = entry[entry.index("# Disable uvicorn proxy headers") :]
    shell = shutil.which("sh") or "C:/Program Files/Git/bin/bash.exe"
    # Capture argv after the real entrypoint policy without launching a server.
    block = block.replace('exec "$@"', 'printf "arg:%s\\n" "$@"')
    for args in (
        ("uvicorn", "app.main:app", "--proxy-headers"),
        ("python", "-m", "uvicorn", "app.main:app", "--proxy-headers"),
    ):
        out = subprocess.run(
            [shell, "-c", block, "entrypoint", *args], text=True, capture_output=True, check=True
        )
        assert "arg:--no-proxy-headers" in out.stdout.splitlines(), out.stdout
    for filename in ("docs/embedded.md", "docs/DEPLOY.md", "docs/SECRETS.md"):
        _assert_documented_launches(Path(filename).read_text(encoding="utf-8"), filename)


def test_r3_single_proxy_setting():
    observed = []

    async def endpoint(scope, receive, send):
        observed.append((scope["scheme"], security.client_ip(Request(scope))))
        await Response("served")(scope, receive, send)

    async def send(message):
        pass

    try:
        with (
            patch.object(config, "fly_app_name", ""),
            patch.dict(
                os.environ, {"LOC_TMS_FORWARDED_ALLOW_IPS": "*", "FORWARDED_ALLOW_IPS": "*"}
            ),
        ):
            for peer in ("127.0.0.1", "198.51.100.20"):
                boundary = HostBoundaryMiddleware(endpoint)
                for trusted in ("", peer, "", peer):
                    settings.set_many({"trusted_proxy_ips": trusted})
                    scope = request(
                        client=(peer, 80),
                        headers=[
                            (b"host", b"testserver"),
                            (b"x-forwarded-proto", b"https"),
                            (b"x-forwarded-for", b"203.0.113.9"),
                        ],
                    ).scope
                    asyncio.run(boundary(scope, None, send))
                    assert observed[-1] == (("https", "203.0.113.9") if trusted else ("http", peer))
    finally:
        settings.set_many({"trusted_proxy_ips": ""})


def test_r3_boundary_cache():
    from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware

    observed = []

    async def endpoint(scope, receive, send):
        if scope["type"] == "lifespan":
            # Model settings.apply_to_config during application startup.
            config.TRUSTED_PROXY_IPS = "198.51.100.0/24"
            await send({"type": "lifespan.startup.complete"})
            return
        observed.append(scope["scheme"])
        await Response("served")(scope, receive, send)

    async def send(message):
        pass

    with (
        patch.object(config, "TRUSTED_PROXY_IPS", ""),
        patch.object(config, "fly_app_name", ""),
        patch("app.main.ProxyHeadersMiddleware", wraps=ProxyHeadersMiddleware) as constructor,
    ):
        boundary = HostBoundaryMiddleware(endpoint)
        asyncio.run(boundary({"type": "lifespan"}, None, send))
        assert constructor.call_count == 1
        for _ in range(2):
            scope = request(
                headers=[(b"host", b"testserver"), (b"x-forwarded-proto", b"https")]
            ).scope
            asyncio.run(boundary(scope, None, send))
        assert constructor.call_count == 1, constructor.call_count
        assert observed == ["https", "https"]


def test_r3_descriptors():
    content = Path("app/capabilities/llm.md").read_text(encoding="utf-8")
    lines = content.splitlines()
    enumerations = [
        " ".join(lines[max(0, i - 2) : i + 2])
        for i, line in enumerate(lines)
        if "`operator_key_hash`" in line
    ]
    assert len(enumerations) == 3
    assert all("security_allowed_hosts" in x and "trusted_proxy_ips" in x for x in enumerations)
    for filename in (
        "docs/DEPLOY.md",
        "docs/architecture/deployment.md",
        "docs/architecture/identity-and-access.md",
        "docs/CLI.md",
        "app/templates/help.html",
    ):
        content = Path(filename).read_text(encoding="utf-8")
        assert "LOC_TMS_FORWARDED_ALLOW_IPS" not in content
        assert "no loopback exemption" in content.lower()
    for filename in (
        "app/capabilities/llm.md",
        "app/templates/help.html",
        "docs/CLI.md",
        "docs/DEPLOY.md",
        "docs/architecture/deployment.md",
    ):
        content = Path(filename).read_text(encoding="utf-8")
        content = " ".join(re.sub(r"</?code>", "", content).replace("`", "").split())
        assert (
            "The exact /health and /ready probes may use any syntactically valid Host; "
            "they bypass only the Host allowlist."
        ) in content, filename
        assert (
            "Local proxy trust changes apply live to identity and scheme; "
            "environment changes require a restart."
        ) in content, filename
        for sentence in re.split(r"[.!?]\s+", content):
            if "trusted_proxy_ips" in sentence:
                assert "restart" not in sentence.lower(), (filename, sentence)
        assert not re.search(
            r"LOC_TMS_TRUSTED_PROXY_IPS(?:\s+environment overrides)?, then restart", content
        ), filename
    assert "ingress_config: uvicorn proxy headers on" in Path("app/capabilities/llm.md").read_text(
        encoding="utf-8"
    )


def test_r3_separator_pin():
    for value in (",", " , ", ",,", " , , , "):
        try:
            settings.validate_value("security_allowed_hosts", value)
        except settings.InvalidSettingValue:
            pass
        else:
            raise AssertionError(f"separator-only pin accepted: {value!r}")


def test_fly_trust_signals():
    from app.web.health import _health_details

    settings.set_many({"security_auth_required": "1"})
    client = login()
    try:
        with patch.object(config, "fly_app_name", "fly-test"):
            for trust, expected in (
                ("", "fly proxy trust unset"),
                ("127.0.0.1", "fly proxy trust unsafe"),
                ("::1", "fly proxy trust unsafe"),
                ("100.64.0.0/10", "fly proxy trust unsafe"),
                ("fd7a:115c:a1e0::/48", "fly proxy trust unsafe"),
                ("fdaa::/16", "fly proxy trust unsafe"),
                ("126.0.0.0/7", "fly proxy trust unsafe"),
                ("172.16.5.9/32", "ok"),
            ):
                with patch.object(config, "TRUSTED_PROXY_IPS", trust):
                    with patch.object(settings, "get", side_effect=AssertionError("DB read")):
                        response = _health_details(request("/health"))
                    assert expected.encode() in response.body, (trust, response.body)
                    with patch("app.services.security.logging.error") as log:
                        security.check_fly_proxy_at_boot()
                    assert log.call_count == (expected != "ok"), trust
                    if expected != "ok":
                        assert "LOC_TMS_TRUSTED_PROXY_IPS" in str(log.call_args)
                    page = client.get("/security")
                    assert page.status_code == 200
                    assert ("Set LOC_TMS_TRUSTED_PROXY_IPS" in page.text) == (expected != "ok")
            settings.set_many({"trusted_proxy_ips": ""})
            with patch.object(security, "_fly_peer_logged", False, create=True):
                with patch("app.services.security.logging.error") as log:
                    for _ in range(3):
                        assert (
                            security.client_ip(
                                request(
                                    client=("172.16.5.9", 1234),
                                    headers=[(b"fly-client-ip", b"203.0.113.5")],
                                )
                            )
                            == "172.16.5.9"
                        )
                assert log.call_count == 1, log.call_args_list
                assert "172.16.5.9" in str(log.call_args)
        with patch.object(config, "fly_app_name", ""):
            assert security.fly_proxy_trust_status() == ""
        source = Path("app/main.py").read_text(encoding="utf-8")
        assert "security.check_fly_proxy_at_boot()" in source
    finally:
        settings.set_many({"trusted_proxy_ips": ""})


def test_gate_probe_reuse():
    # Reuse the gate's offline reproducer when available on the review machine.
    probe = Path(os.environ.get("F8_GATE_PROBE", "/nonexistent"))
    if probe.exists():
        result = subprocess.run(
            [sys.executable, str(probe)], capture_output=True, text=True, timeout=90
        )
        assert result.returncode == 0 and "ALL OK" in result.stdout, result.stdout + result.stderr
        print("  reused gate probe: ALL OK")
    assert "un-spoofable Fly-Client-IP" not in Path("app/web/deps.py").read_text("utf-8")


def test_host_pin_health():
    from app.web.health import health

    assert asyncio.iscoroutinefunction(health)
    settings.set_many({"app_base_url": "", "security_allowed_hosts": ""})
    baseline = TestClient(app).get("/health").content
    try:
        for base, pin in (
            ("https://tms.example.com", ""),
            ("https://tms.example.com", "tms.example.com"),
            ("", ""),
            ("", "other.example"),
        ):
            settings.set_many({"app_base_url": base, "security_allowed_hosts": pin})
            with patch.object(engine, "connect", side_effect=AssertionError("health DB access")):
                response = TestClient(app).get("/health")
                with patch.object(
                    settings, "get", side_effect=AssertionError("health settings read")
                ):
                    direct = asyncio.run(health(request("/health")))
                assert direct.body == response.content
            assert response.status_code == 200
            assert "detail" not in response.json()
            response = _detail_response()
            if base and not pin:
                assert response.json()["detail"].get("host_pin") == "unset", response.text
            else:
                assert "host_pin" not in response.json()["detail"]
                assert TestClient(app).get("/health").content == baseline
    finally:
        settings.set_many({"app_base_url": "", "security_allowed_hosts": ""})


def test_host_pin_validator():
    try:
        settings.set_many({"app_base_url": "https://tms.example.com"})
        settings.validate_value("security_allowed_hosts", "")
        settings.validate_value("security_allowed_hosts", "tms.example.com, other.example")
        settings.validate_value("security_allowed_hosts", "TMS.EXAMPLE.COM:8443")
        try:
            settings.validate_value("security_allowed_hosts", "other.example")
        except settings.InvalidSettingValue as exc:
            assert "add tms.example.com, or clear/repoint app_base_url" in str(exc), str(exc)
        else:
            raise AssertionError("missing app_base_url host accepted by validator")
    finally:
        settings.set_many({"app_base_url": ""})
    settings.validate_value("security_allowed_hosts", "other.example")


def test_host_pin_cli():
    from app.cli.__main__ import main as cli_main

    try:
        settings.set_many({"app_base_url": "https://tms.example.com"})
        for value in ("tms.example.com, other.example", ""):
            assert cli_main(["config", "set", "security_allowed_hosts", value, "--yes"]) == 0
            assert settings.get("security_allowed_hosts") == value
        result = cli_main(["config", "set", "security_allowed_hosts", "other.example", "--yes"])
        assert result != 0, "missing app_base_url host accepted by config set"
        assert settings.get("security_allowed_hosts") == ""
    finally:
        settings.set_many({"app_base_url": "", "security_allowed_hosts": ""})


def test_host_pin_legacy_and_batch():
    from contextlib import redirect_stderr

    from app.cli.__main__ import main as cli_main
    from app.models import Setting

    def run(*args):
        output = io.StringIO()
        with redirect_stdout(output), redirect_stderr(output):
            rc = cli_main(["config", *args, "--yes"])
        return rc, output.getvalue()

    try:
        for base, host in (
            ("tms.example.com", "tms.example.com"),
            ("tms.example.com:8443", "tms.example.com"),
            ("https://tms.example.com ", "tms.example.com"),
            # pregate: allow-secret legacy URL userinfo fixture, no real credential
            ("https://admin@tms.example.com", "tms.example.com"),
            ("https://[2001:db8::1]:8443", "[2001:db8::1]"),
            ("https://[broken", ""),
            ("https://bad host", ""),
        ):
            # Plant a pre-validator row without weakening the #121 write door.
            with Session(engine) as session:
                session.merge(Setting(key="app_base_url", value=base))
                session.commit()
            settings.invalidate_cache()
            assert run("set", "security_allowed_hosts", host or "other.example")[0] == 0
            rc, message = run("set", "security_allowed_hosts", "other.example")
            assert rc == (2 if host else 0), (base, rc, message)
            if host:
                assert "security_allowed_hosts" in message and "app_base_url" in message
                assert f"add {host}, or clear/repoint app_base_url" in message, message
                assert run("set", "security_allowed_hosts", host)[0] == 0
            assert run("set", "security_allowed_hosts", "")[0] == 0

        for base, pin, expected in (
            ("https://b.example", "b.example", 0),
            ("", "b.example", 0),
            ("https://b.example", "other.example", 2),
            ("https://b.example", "a.example", 2),
        ):
            for reverse in (False, True):
                settings.set_many(
                    {"app_base_url": "https://a.example", "security_allowed_hosts": "a.example"}
                )
                lines = [f'app_base_url = "{base}"', f'security_allowed_hosts = "{pin}"']
                if reverse:
                    lines.reverse()
                path = Path(os.environ["TEMP"]) / "host-pin-batch.toml"
                path.write_text("[settings]\n" + "\n".join(lines) + "\n", encoding="utf-8")
                rc, message = run("apply", str(path))
                assert rc == expected, (lines, rc, message)
                assert settings.get("app_base_url") == (
                    base if expected == 0 else "https://a.example"
                )
                assert settings.get("security_allowed_hosts") == (
                    pin if expected == 0 else "a.example"
                )
                if expected:
                    assert "app_base_url host b.example" in message, message
    finally:
        settings.set_many({"app_base_url": "", "security_allowed_hosts": ""})


def test_host_pin_api_local_only():
    # The real API spine refuses this immutable key BEFORE value validation, even empty.
    settings.set_many({"app_base_url": "https://tms.example.com"})
    try:
        for value in ("other.example", "tms.example.com", ""):
            _remote_lock("security_allowed_hosts", value)
    finally:
        settings.set_many({"app_base_url": ""})


def test_host_pin_existing_read():
    # Changing the external URL must not silently turn an existing Host pin off.
    try:
        settings.set_many(
            {"app_base_url": "https://tms.example.com", "security_allowed_hosts": "other.example"}
        )
        assert config.SECURITY_ALLOWED_HOSTS == "other.example"
        assert TestClient(app).get("/auth/login").status_code == 400
    finally:
        settings.set_many({"app_base_url": "", "security_allowed_hosts": ""})


def main():
    init_db()
    security.create_user("ingress-admin", "Admin", "admin", "ingress-password-123")
    security.create_user("ingress-linguist", "Linguist", "linguist", "ingress-password-123")
    with Session(engine) as session:
        session.add(Customer(name="AUDIT_SYNTHETIC_PRIVATE_CUSTOMER"))
        session.commit()
    invoked = set()
    failures = []
    for test in (
        test_fly_trust_signals,
        test_gate_probe_reuse,
        test_host_pin_health,
        test_host_pin_validator,
        test_host_pin_cli,
        test_host_pin_legacy_and_batch,
        test_host_pin_api_local_only,
        test_host_pin_existing_read,
        test_r4_probe_paths,
        test_r4_launch_docs_nonvacuous,
        test_r3_high1_outer_proxy,
        test_r3_proxy_self_check,
        test_r3_invalid_env,
        test_r3_multiline_xff,
        test_r3_launch_flags,
        test_r3_boundary_cache,
        test_r3_single_proxy_setting,
        test_r3_descriptors,
        test_r3_separator_pin,
        test_high1_remote_host_lock,
        test_med1_proxy_lock_and_prefixes,
        test_med2_malformed_identity,
        test_med3_root_path,
        test_med4_loopback_recovery,
        test_local_ingress_env_precedence,
        test_med5_saml_proxy_warning,
        test_med5_forwarding_preserves_socket,
        test_access_log_client_identity,
        test_low1_websocket_host,
        test_low2_parser_and_probe_case,
        test_low3_docs_placement,
        test_low4_boundary_assertions,
        test_path_wall,
        test_proxy_trust,
        test_288_fly_peer_boundary,
        test_289_malformed_left_ignored,
        test_local_key_echo,
        test_host_and_route_matrix,
        test_canonical_routing_and_audit,
        test_key_read_save_transport_and_logs,
        test_settings_validation_and_proxy_startup,
    ):
        invoked.add(test.__name__)
        try:
            test()
        except Exception as exc:
            failures.append(f"{test.__name__}: {type(exc).__name__}: {exc}")
    assert invoked == {n for n, v in globals().items() if n.startswith("test_") and callable(v)}
    assert not failures, "\n".join(failures)
    print("verify_ingress_boundary OK")


if __name__ == "__main__":
    main()
