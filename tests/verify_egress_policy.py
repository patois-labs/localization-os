"""Private egress policy, offline regression suite.

Documented in: docs/architecture/secrets-and-crypto.md
"""

import os
import sys
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from tests._bootstrap import init_env
from tests._piece_patch import patch_pieces

init_env("loctms_egress_policy_")

from app.db import init_db
from app.services import egress, settings

KEY = "egress_private_allowlist"
ENV = "LOC_TMS_EGRESS_PRIVATE_ALLOWLIST"


def blocked(url):
    try:
        egress.validate_target(url)
    except egress.EgressBlocked as exc:
        return str(exc)
    raise AssertionError(f"accepted {url}")


def test_policy():
    with patch.dict(os.environ, {ENV: ""}):
        egress.set_test_resolver(
            lambda h: ["8.8.8.8" if h == "public.example.com" else "10.20.30.40"]
        )
        for url in ("https://10.20.30.40", "https://jira.internal", "https://jira.example.com"):
            blocked(url)
        assert egress.validate_target("https://public.example.com")
    for entry in ("jira.internal", "10.20.0.0/16"):
        with patch.dict(os.environ, {ENV: entry}):
            assert egress.validate_target("https://jira.internal")
    with patch.dict(os.environ, {ENV: "jira.internal"}):
        blocked("https://sub.jira.internal")
        egress.set_test_resolver(lambda h: ["169.254.169.254"])
        blocked("https://jira.internal")
    with patch.dict(os.environ, {ENV: "10.20.0.0/16"}):
        egress.set_test_resolver(lambda h: ["10.21.0.1"])
        blocked("https://jira.example.com")


def test_refusal_messages():
    cases = {
        "https://10.20.30.40": "Blocked target: 10.20.30.40 is not a public address.",
        "https://jira.internal": "Blocked target: 'jira.internal' is a private/internal name.",
        "https://jira.example.com": (
            "Blocked target: jira.example.com resolves to non-public address 10.20.30.40."
        ),
    }
    with patch.dict(os.environ, {ENV: ""}):
        egress.set_test_resolver(lambda host: ["10.20.30.40"])
        for url, message in cases.items():
            try:
                egress.validate_target(url)
            except egress.EgressBlocked as exc:
                assert str(exc) == message + " not on egress_private_allowlist"
            else:
                raise AssertionError("private target accepted")


def test_validator():
    assert KEY in settings.SENSITIVE_KEYS
    assert KEY in settings.REMOTE_IMMUTABLE_KEYS
    for value in (
        "0.0.0.0/0",
        "::/0",
        "10.0.0.0/7",
        "*.internal",
        "127.0.0.1",
        "169.254.0.0/16",
        "fe80::/32",
        "fc00::/31",
        "metadata.google.internal",
    ):
        try:
            settings.validate_value(KEY, value)
        except settings.InvalidSettingValue:
            pass
        else:
            raise AssertionError(f"accepted {value}")


def test_invalid_env():
    with patch.dict(os.environ, {ENV: "0.0.0.0/0"}):
        settings.apply_to_config()
        assert settings.get(KEY) == ""
        assert settings.egress_policy_view() == {
            "value": "0.0.0.0/0",
            "source": "env",
            "valid": False,
        }


def test_redirects_and_audit():
    import json
    from types import SimpleNamespace

    import httpx

    from app.services import audit
    from app.services.connectors.base import Connector, ConnectorError, _validate_target

    sent = []

    def reply(request):
        sent.append(str(request.url))
        return httpx.Response(302, headers={"location": "https://10.99.0.1"})

    with patch.dict(os.environ, {ENV: "10.20.0.0/16"}), patch.object(audit, "log") as log:
        egress.set_test_resolver(lambda h: ["10.20.30.40"])
        _validate_target("https://jira.example.com")
        assert log.call_count == 0, "preflight must not audit"
        connector = SimpleNamespace(_transport=httpx.MockTransport(reply))
        token = egress.CALLER_IP.set("203.0.113.7")
        try:
            with Connector._client(connector) as client:
                try:
                    client.get("https://jira.example.com", follow_redirects=True)
                except ConnectorError:
                    pass
                else:
                    raise AssertionError("redirect escaped guard")
        finally:
            egress.CALLER_IP.reset(token)
        assert sent == ["https://jira.example.com"]
        assert log.call_count == 1
        row = log.call_args.kwargs
        assert row["action"] == "egress.private_target" and not row.get("force", False)
        assert row["ip"] == "203.0.113.7"
        assert json.loads(row["detail"]) == {
            "hostname": "jira.example.com",
            "resolved_address": "10.20.30.40",
            "matched_entry": "10.20.0.0/16",
            "subsystem": "connectors",
            "attempt_count": 1,
        }
        log.reset_mock()
        attempts = []

        def retry(request):
            attempts.append(request)
            return httpx.Response(500 if len(attempts) < 3 else 200)

        connector._transport = httpx.MockTransport(retry)
        with Connector._client(connector) as client, patch("app.services.connectors.base._sleep"):
            assert (
                Connector._request(connector, client, "GET", "https://jira.example.com").status_code
                == 200
            )
            assert log.call_count == 1
            assert json.loads(log.call_args.kwargs["detail"])["attempt_count"] == 3
            Connector._request(connector, client, "GET", "https://jira.example.com")
            assert log.call_count == 2, "independent requests must not deduplicate"
        log.reset_mock()
        egress.set_test_resolver(lambda host: ["10.20.0.5", "10.20.0.6", "10.20.0.5"])
        with Connector._client(connector) as client:
            Connector._request(connector, client, "GET", "https://jira.example.com")
        assert log.call_count == 1, "multiple DNS answers are one transport attempt"
        detail = json.loads(log.call_args.kwargs["detail"])
        assert detail["attempt_count"] == 1
        assert detail["resolved_targets"] == [
            {"resolved_address": "10.20.0.5", "matched_entry": "10.20.0.0/16"},
            {"resolved_address": "10.20.0.6", "matched_entry": "10.20.0.0/16"},
        ]
        egress.set_test_resolver(lambda host: ["10.20.30.40"])
        log.reset_mock()
        from app.services import vendor_dispatch, vendor_return

        with patch.object(
            egress,
            "validate_target",
            side_effect=AssertionError("scheme must be checked before target validation"),
        ):
            try:
                vendor_return._guard_poll_target("http://jira.example.com")
            except ConnectorError as exc:
                assert "https" in str(exc)
            else:
                raise AssertionError("cleartext poll accepted")
        vendor = SimpleNamespace(endpoint_url="http://jira.example.com")
        with (
            patch_pieces("app.services.vendor_dispatch", "_refuse_when_vendors_off"),
            patch.object(
                egress,
                "validate_target",
                side_effect=AssertionError("scheme must be checked before target validation"),
            ),
        ):
            for send in (
                lambda: vendor_dispatch._native_send(vendor, {}),
                lambda: vendor_dispatch.post_signed_message(vendor, {}),
            ):
                try:
                    send()
                except ConnectorError as exc:
                    assert "https" in str(exc)
                else:
                    raise AssertionError("cleartext send accepted")
        assert log.call_count == 0


def test_surfaces_and_remote_lock():
    import asyncio
    import json
    from types import SimpleNamespace

    import httpx
    from fastapi.testclient import TestClient

    from app.cli import cmd_config
    from app.main import app
    from app.services import apikeys, security

    security.create_user("egress-admin", "Admin", "admin", "egress-password-123")
    user = security.get_by_email("egress-admin")
    _, token = apikeys.create_key(user.id, "egress", scopes="api docs admin")
    _, docs_token = apikeys.create_key(user.id, "egress-docs", scopes="api docs")
    headers = {"X-Loc-Token": token}
    client = TestClient(app)
    settings.set_many({"remote_admin_enabled": "1", KEY: "10.20.0.0/16"})
    expected = {"value": "10.20.0.0/16", "source": "file", "valid": True}
    assert client.get("/api/admin/config", headers=headers).json()["settings"][KEY] == expected
    docs = client.get("/api/admin/config", headers={"X-Loc-Token": docs_token})
    assert docs.status_code == 403
    response = client.post(
        "/api/admin/config",
        headers=headers,
        json={"settings": {KEY: "10.0.0.0/8"}, "confirm": True},
    )
    assert response.status_code == 400 and "local" in response.text.lower()
    assert settings.get(KEY) == "10.20.0.0/16"

    class Emit:
        as_json = True

        def json(self, value):
            self.value = value

    emit = Emit()
    with patch.object(cmd_config, "remote_mode", return_value=False):
        cmd_config._show(SimpleNamespace(), emit)
    assert emit.value["settings"][KEY] == expected
    with (
        patch.object(cmd_config, "remote_mode", return_value=True),
        patch.object(
            cmd_config._remote_impl,
            "_client",
            return_value=SimpleNamespace(get=lambda path: client.get(path, headers=headers).json()),
        ),
    ):
        cmd_config._show(SimpleNamespace(), emit)
    assert emit.value["settings"][KEY] == expected
    sys.path.insert(
        0,
        str(__import__("pathlib").Path(__file__).resolve().parents[1] / "integrations/mcp-server"),
    )
    import server

    async def mcp_checks():
        with patch.object(
            server,
            "_client",
            side_effect=lambda: httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app),
                base_url="http://testserver",
                headers=headers,
            ),
        ):
            data = json.loads(await server.manage_install(action="get_config"))
            assert data["settings"][KEY] == expected
            try:
                await server.manage_install(action="set_config", settings={KEY: "10.0.0.0/8"})
            except Exception as exc:
                assert "local" in str(exc) or "locked" in str(exc)
            else:
                raise AssertionError("MCP wrote host policy")

    with (
        patch.object(server, "TOKEN", token),
        patch.object(server, "_post_json", side_effect=AssertionError("MCP attempted wire write")),
    ):
        asyncio.run(mcp_checks())
    settings.set_many(
        {"security_auth_required": "1", "security_allowed_hosts": "known.example.com"}
    )
    health = client.get("/health", headers={"Host": "unknown.example.com"}).json()
    settings.set_many({"security_auth_required": "0", "security_allowed_hosts": ""})
    # P2a adds a coarse `version` top-level key; the train-A merge (bc9c7e00) makes `detail`
    # a MAP and adds `admission_daemon` + `ingress_config` liveness words. Pin the egress
    # WORD as its own key and pin that no OTHER egress word leaks, without re-litigating
    # either ruling -- the same accommodation P4 already grants P2a's `version`.
    assert health["status"] == "ok"
    # Since #270 the anonymous /health carries no detail map; the egress word lives in the
    # administrator detail (/admin/health), built by the same DB-free _health_details.
    assert "detail" not in health, health
    from starlette.requests import Request

    from app.web.health import _health_details

    def admin_detail():
        scope = {"type": "http", "method": "GET", "path": "/admin/health", "headers": []}
        return json.loads(_health_details(Request(scope)).body)["detail"]

    assert admin_detail()["egress_config"] == "ok"
    with patch.dict(os.environ, {ENV: "invalid*"}):
        settings.apply_to_config()
        assert settings.get(KEY) == ""
        health = client.get("/health").json()
        assert health["status"] == "ok"
        assert admin_detail()["egress_config"] == "invalid"
        assert client.get("/api/admin/config", headers=headers).json()["settings"][KEY] == {
            "value": "invalid*",
            "source": "env",
            "valid": False,
        }
    settings.set_many({KEY: "", "remote_admin_enabled": "0"})
    health = client.get("/health").json()
    assert health["status"] == "ok"
    assert "egress_config" not in admin_detail()


def test_fixed_boundaries():
    for entry, address in (("fc00::/32", "fc00::1"), ("100.64.0.0/10", "100.64.0.1")):
        with patch.dict(os.environ, {ENV: entry}):
            egress.set_test_resolver(lambda h, address=address: [address])
            assert egress.validate_target("https://jira.example.com")
    for address in (
        "::1",
        "fe80::1",
        "0.1.2.3",
        "127.2.3.4",
        "224.0.0.1",
        "240.0.0.1",
        "::ffff:127.0.0.1",
        "::",
    ):
        with patch.dict(os.environ, {ENV: "jira.internal"}):
            egress.set_test_resolver(lambda h, address=address: [address])
            blocked("https://jira.internal")
    with patch.dict(os.environ, {ENV: "jira.internal"}):
        egress.set_test_resolver(lambda h: ["10.20.30.40", "169.254.169.254"])
        blocked("https://jira.internal")


def test_resolution_required():
    with patch.dict(os.environ, {ENV: "jira.internal"}):
        for resolver in (None, lambda host: [], lambda host: (_ for _ in ()).throw(OSError())):
            egress.set_test_resolver(resolver)
            blocked("https://jira.internal")
    with patch.dict(os.environ, {ENV: ""}):
        egress.set_test_resolver(None)
        blocked("https://jira.internal")
        assert egress.validate_target("https://public.example.com")
    settings.set_many({KEY: "invalid*"})
    assert settings.get(KEY) == ""
    assert settings.egress_policy_view() == {"value": "invalid*", "source": "file", "valid": False}
    settings.set_many({KEY: ""})


def test_vendor_poll_policy():
    from app.services import vendor_return

    with patch.dict(os.environ, {ENV: "jira.internal"}):
        with patch_pieces(
            "app.services.vendor_return", "_resolve_host_ips", return_value=["10.20.30.40"]
        ):
            assert vendor_return._guard_poll_target("https://jira.internal")
        with patch_pieces(
            "app.services.vendor_return", "_resolve_host_ips", return_value=["169.254.169.254"]
        ):
            try:
                vendor_return._guard_poll_target("https://jira.internal")
            except Exception as exc:
                assert "Blocked target" in str(exc)
            else:
                raise AssertionError("poll reached metadata")


def test_invalid_entrypoint():
    import json
    import subprocess

    with patch.dict(os.environ, {ENV: "0.0.0.0/0"}):
        result = subprocess.run(
            [sys.executable, "-m", "app.cli", "--json", "config", "show"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=90,
        )
        assert result.returncode == 0, result.stderr
        assert json.loads(result.stdout)["settings"][KEY] == {
            "value": "0.0.0.0/0",
            "source": "env",
            "valid": False,
        }


def test_request_audit_ip():
    import asyncio
    import json

    import httpx
    from sqlmodel import Session, select

    from app.db import engine
    from app.main import app
    from app.models import AuditLog
    from app.services import apikeys, security

    user = security.get_by_email("egress-admin")
    _, raw_token = apikeys.create_key(user.id, "audit attribution", scopes="api docs admin")

    def probe():
        with httpx.Client(
            transport=egress.GuardedTransport(
                "connectors", httpx.MockTransport(lambda request: httpx.Response(200))
            )
        ) as outbound:
            outbound.get("https://jira.internal")
        return {"ok": True}

    app.add_api_route("/api/_egress_policy_probe", probe, methods=["GET"])
    route = app.routes[-1]

    async def run():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app, client=("203.0.113.7", 1234)),
            base_url="http://testserver",
        ) as client:
            response = await client.get(
                "/api/_egress_policy_probe",
                headers={"X-Forwarded-For": "1.1.1.1", "X-Loc-Token": raw_token},
            )
            assert response.status_code == 200, response.text

    try:
        with patch.dict(os.environ, {ENV: "jira.internal"}):
            egress.set_test_resolver(lambda h: ["10.20.30.40"])
            asyncio.run(run())
        with Session(engine) as session:
            row = session.exec(
                select(AuditLog)
                .where(AuditLog.action == "egress.private_target")
                .order_by(AuditLog.id.desc())
            ).first()
            assert row.ip == "203.0.113.7"
            assert row.actor == "egress-admin"
            assert json.loads(row.detail)["subsystem"] == "connectors"
        assert egress.CALLER_IP.get() == ""
        assert egress.CALLER_ACTOR.get() == "system"
    finally:
        app.routes.remove(route)


def test_descriptors():
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    for name in (
        "app/templates/help.html",
        "docs/CLI.md",
        "app/capabilities/llm.md",
        "docs/architecture/secrets-and-crypto.md",
        "docs/architecture/deployment.md",
    ):
        content = (root / name).read_text(encoding="utf-8")
        for word in (KEY, "egress.private_target"):
            assert word in content, (name, word)
        if name != "docs/architecture/secrets-and-crypto.md":
            assert "secrets-and-crypto.md#private-egress-exceptions" in content
            assert "169.254.0.0/16" not in content[content.index("Private egress exceptions") :]
    canonical = (root / "docs/architecture/secrets-and-crypto.md").read_text(encoding="utf-8")
    for phrase in (
        "IPv4-mapped IPv6",
        "::ffff:10.20.0.5",
        "IDNA",
        "attempt_count",
        "audit_enabled",
        "HTTPS_PROXY",  # HIGH-1: the proxy law is stated in the canonical policy
        "ASCII",  # MED-1: ASCII hosts follow httpx acceptance, not an IDNA STD3 gate
        # Round 4 LOW-6: the doc states the guard checks reachability, never ASCII syntax.
        "reachability of the resolved address",
    ):
        assert phrase in canonical
    # LOW-5 / LOW-6: the agent (llm.md) and public (CLI.md) surfaces retain the actionable
    # operational content (always-blocked destinations, audit row fields) rather than only a
    # link to the internal architecture doc, which is not on the public portal.
    for surface in ("app/capabilities/llm.md", "docs/CLI.md"):
        section = (root / surface).read_text(encoding="utf-8")
        section = section[section.index("Private egress exceptions") :]
        assert "attempt_count" in section, surface
        assert "metadata" in section, surface
        assert "HTTPS_PROXY" in section, surface
    deployment = (root / "docs/architecture/deployment.md").read_text(encoding="utf-8")
    for phrase in (
        "confirm `valid: true`",
        "ENTIRE list",
        "ANY hostname",
        'egress_private_allowlist = "jira.internal"',
    ):
        assert phrase in deployment
    matrix = (root / "tests/verify_admin_scope_tier.py").read_text(encoding="utf-8")
    for phrase in (
        "for enabled in (False, True)",
        '"external_admin"',
        '"translator_admin"',
        '("/api/admin/config", "/api/admin/ldap")',
        "200 if enabled else 404",
    ):
        assert phrase in matrix
    oauth = (root / "app/services/connectors/oauth.py").read_text(encoding="utf-8")
    assert "fixed provider registry, never user configuration" in oauth


def test_idna_and_errors():
    # A genuinely non-ASCII entry and target still normalize through IDNA and match by punycode.
    for entry in ("töst.example.com", "xn--tst-sna.example.com"):
        with patch.dict(os.environ, {ENV: entry}):
            egress.set_test_resolver(lambda host: ["10.20.0.5"])
            assert egress.validate_target("https://töst.example.com")
            assert egress.validate_target("https://xn--tst-sna.example.com")
    with patch.dict(os.environ, {ENV: "xn--bcher-kva.example"}):
        egress.set_test_resolver(lambda host: ["10.20.0.5"])
        assert egress.validate_target("https://bücher.example")  # encoded, matches punycode entry
    # MED-1: legal ASCII hosts httpx itself accepts must NOT be refused here. These resolve
    # public (no allowlist), so before this fix an IDNA STD3 gate wrongly blocked plain egress.
    with patch.dict(os.environ, {ENV: ""}):
        egress.set_test_resolver(lambda host: ["8.8.8.8"])
        for url in (
            "https://my_host.example.com",
            "https://_acme.example.com",
            "https://host-.example.com",
        ):
            assert egress.validate_target(url), url
    # LOW-3: a URL with no host gets its own refusal, not the entry-validator grammar sentence.
    try:
        egress.validate_target("https://")
    except egress.EgressBlocked as exc:
        assert "no host" in str(exc).lower()
        assert "wildcards" not in str(exc)
    else:
        raise AssertionError("empty host accepted")
    for value in ("example.com/", "http://jira.internal", "jira.internal:8443", "jira.internal,"):
        try:
            settings.validate_value(KEY, value)
        except settings.InvalidSettingValue as exc:
            assert str(exc).startswith(KEY + ":")
            assert "hostnames or IPv4/IPv6 CIDRs" in str(exc)
        else:
            raise AssertionError(value)
    # LOW-4: an IPv4-mapped answer to an allowlisted IPv4 range is refused WITH an actionable
    # hint, not a bare block, since ::/8 admits no exception.
    with patch.dict(os.environ, {ENV: "10.20.0.0/16"}):
        egress.set_test_resolver(lambda host: ["::ffff:10.20.0.5"])
        try:
            egress.validate_target("https://jira.example.com")
        except egress.EgressBlocked as exc:
            assert "IPv4-mapped" in str(exc) and "native IPv4" in str(exc)
        else:
            raise AssertionError("mapped address accepted")
    # Round 4 LOW-2: when the embedded IPv4 is ITSELF always-blocked the hint's advice ("use a
    # native IPv4 DNS answer") is impossible to follow, so the refusal is plain, with no hint.
    with patch.dict(os.environ, {ENV: "10.20.0.0/16"}):
        for mapped in ("::ffff:127.0.0.1", "::ffff:169.254.169.254", "::ffff:0.0.0.0"):
            egress.set_test_resolver(lambda host, m=mapped: [m])
            try:
                egress.validate_target("https://jira.example.com")
            except egress.EgressBlocked as exc:
                assert "IPv4-mapped" not in str(exc), (mapped, str(exc))
                assert "native IPv4" not in str(exc), (mapped, str(exc))
            else:
                raise AssertionError(f"mapped always-blocked address accepted: {mapped}")
    # Round 4 LOW-3: a malformed non-ASCII TARGET gets the target-refusal wording (an invalid IDN),
    # not the allowlist ENTRY grammar; a malformed non-ASCII ENTRY still gets the entry grammar.
    try:
        egress.validate_target("https://\U0001f600.example")
    except egress.EgressBlocked as exc:
        assert "not a valid idn" in str(exc).lower(), str(exc)
        assert "wildcards" not in str(exc).lower(), str(exc)
    else:
        raise AssertionError("malformed non-ASCII target accepted")
    try:
        settings.validate_value(KEY, "\U0001f600.example")
    except settings.InvalidSettingValue as exc:
        assert "hostnames or IPv4/IPv6 CIDRs" in str(exc), str(exc)
        assert "not a valid IDN" not in str(exc), str(exc)
    else:
        raise AssertionError("malformed non-ASCII entry accepted")


def test_http_connector_error():
    from types import SimpleNamespace

    import httpx
    from fastapi.testclient import TestClient

    from app.main import app
    from app.services.connectors.base import Connector
    from app.web import connectors

    def fields(*args):
        connector = SimpleNamespace(_transport=httpx.MockTransport(lambda req: httpx.Response(200)))
        with Connector._client(connector) as client:
            return Connector._request(connector, client, "GET", "https://10.99.0.1")

    with (
        patch.dict(os.environ, {ENV: ""}),
        patch_pieces("app.web.connectors", "_ai_block", return_value=(None, None)),
        patch_pieces(
            "app.web.connectors",
            "_conn_or_404",
            return_value=(SimpleNamespace(kind="airtable"), None),
        ),
        patch.object(connectors.conn_store, "schema_fields", side_effect=fields),
    ):
        response = TestClient(app).post(
            "/connectors/connection/1/browse/fields", data={"base": "b", "table": "t"}
        )
        assert response.status_code == 400, response.text
        assert "Blocked target" in response.json()["error"]


def test_proxy_mounts():
    # HIGH-1 / MED-1: the guard must not disable httpx's corporate-proxy support, AND it must mount
    # EXACTLY what a plain httpx.Client(trust_env=True) mounts, key for key and None for None, in
    # every proxy environment. A NO_PROXY pattern maps to None (httpx's own fallback to the base
    # transport), not a fresh guarded HTTPTransport, so a proxy install does not pay one extra TLS
    # context and connection pool per NO_PROXY entry on every client it builds. LOW-1: this parity
    # is ASSERTED (not "a proxy is mounted"), and get_environment_proxies is pinned to the mount map
    # so a future httpx semantic change fails loudly.
    from types import SimpleNamespace

    import httpx
    from httpx._utils import get_environment_proxies

    from app.services.connectors.base import Connector

    proxy_keys = (
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "ALL_PROXY",
        "NO_PROXY",
        "http_proxy",
        "https_proxy",
        "all_proxy",
        "no_proxy",
    )
    subsystems = ("connectors", "web.search", "vendor_dispatch", "vendor_return")
    # Six proxy environments: clean, each scheme, ALL_PROXY, a real NO_PROXY list, and NO_PROXY=*.
    envs = {
        "clean": {},
        "https": {"HTTPS_PROXY": "http://proxy.corp:8080"},
        "http": {"HTTP_PROXY": "http://proxy.corp:3128"},
        "all": {"ALL_PROXY": "http://proxy.corp:9999"},
        "both+noproxy": {
            "HTTPS_PROXY": "http://proxy.corp:8080",
            "HTTP_PROXY": "http://proxy.corp:3128",
            "NO_PROXY": "jira.internal,10.20.0.0/16,sub.corp.example",
        },
        "noproxy=*": {"HTTPS_PROXY": "http://proxy.corp:8080", "NO_PROXY": "*"},
    }

    def clean_env(extra):
        # httpx skips a *_proxy var whose value is "", so blanking keys would make the proxy cases
        # vacuous; instead drop every proxy key from the (already snapshotted) environ, then apply
        # only this case's real vars. Callers wrap this in a bare patch.dict(os.environ) to restore.
        for key in proxy_keys:
            os.environ.pop(key, None)
        os.environ.update(extra)

    for name, env in envs.items():
        with patch.dict(os.environ):
            clean_env(env)
            with httpx.Client(trust_env=True) as plain:
                plain_all = sorted(p.pattern for p in plain._mounts)
                plain_none = sorted(p.pattern for p, v in plain._mounts.items() if v is None)
                plain_non_none = sum(1 for v in plain._mounts.values() if v is not None)
            gep = get_environment_proxies()
            for sub in subsystems:
                kwargs = egress.guarded_transport_kwargs(sub)
                assert isinstance(kwargs["transport"], egress.GuardedTransport)
                with httpx.Client(**kwargs) as g:
                    guarded_all = sorted(p.pattern for p in g._mounts)
                    guarded_none = sorted(p.pattern for p, v in g._mounts.items() if v is None)
                    assert guarded_all == plain_all, (name, sub, guarded_all, plain_all)
                    assert guarded_none == plain_none, (name, sub, "None-for-None parity broken")
                    non_none = [v for v in g._mounts.values() if v is not None]
                    assert all(isinstance(v, egress.GuardedTransport) for v in non_none), (
                        name,
                        sub,
                    )
                    assert len(non_none) == plain_non_none, (name, sub, "extra transports built")
                    # LOW-1: the mount map is pinned to get_environment_proxies, key for key.
                    gmap = {p.pattern: v for p, v in g._mounts.items()}
                    assert set(gmap) == set(gep), (name, sub, set(gmap), set(gep))
                    for key, proxy_url in gep.items():
                        assert (gmap[key] is None) == (proxy_url is None), (name, sub, key)
    proxy_env = {"HTTPS_PROXY": "http://proxy.corp:8080", "HTTP_PROXY": "http://proxy.corp:8080"}
    with patch.dict(os.environ):
        clean_env(proxy_env)
        # The real production connector client (transport is None) still builds the proxy mounts.
        with Connector._client(SimpleNamespace(_transport=None)) as c:
            assert any(
                type(t.inner._pool).__name__ == "HTTPProxy"
                for t in c._mounts.values()
                if t is not None
            ), "connector prod client lost the proxy"
        # An explicit inner transport (a test double) is guarded directly, no env mounts.
        double = egress.guarded_transport_kwargs(
            "connectors", transport=httpx.MockTransport(lambda r: None)
        )
        assert "mounts" not in double and isinstance(double["transport"], egress.GuardedTransport)
    # An allowlisted private target still validates per hop through a proxied, guarded client.
    with patch.dict(os.environ):
        clean_env({**proxy_env, ENV: "10.20.0.0/16"})
        egress.set_test_resolver(lambda h: ["10.99.0.1"])  # out of range -> refused
        blocked("https://jira.example.com")
        egress.set_test_resolver(lambda h: ["10.20.0.5"])  # in range -> approved
        assert egress.validate_target("https://jira.example.com")


def test_single_validation_seam():
    # LOW-1: the guarded transport is the ONE validation seam. No request event hook is
    # installed, so a target is resolved and validated once per hop, not twice.
    from types import SimpleNamespace

    import httpx

    from app.services.connectors.base import Connector

    conn = SimpleNamespace(_transport=httpx.MockTransport(lambda r: httpx.Response(200)))
    with Connector._client(conn) as client:
        assert client._event_hooks.get("request", []) == [], (
            "no request hook: transport is the seam"
        )
        real = egress.validate_target
        calls = []

        def counting(*a, **k):
            calls.append(1)
            return real(*a, **k)

        with patch.dict(os.environ, {ENV: ""}):
            egress.set_test_resolver(lambda h: ["8.8.8.8"])
            with patch.object(egress, "validate_target", side_effect=counting):
                client.get("https://public.example.com")
        assert len(calls) == 1, f"validated {len(calls)}x; expected a single seam"


def test_audit_best_effort():
    # MED-2: the audit flush is best-effort and runs outside the request finally. An audit
    # write that raises never replaces the caller's result, and a failed actor lookup still
    # writes the row under `system`.
    import httpx
    from sqlmodel import Session, select

    from app.db import engine
    from app.models import AuditLog
    from app.services import audit

    def approved_egress():
        with httpx.Client(
            transport=egress.GuardedTransport(
                "connectors", httpx.MockTransport(lambda r: httpx.Response(200))
            )
        ) as c:
            return c.get("https://jira.internal").status_code

    with patch.dict(os.environ, {ENV: "jira.internal"}):
        egress.set_test_resolver(lambda h: ["10.20.30.40"])
        with patch.object(audit, "log", side_effect=RuntimeError("audit db down")):
            assert approved_egress() == 200, "an audit failure must not break the egress"

        def boom():
            raise RuntimeError("identity lookup failed")

        token = egress.CALLER_ACTOR.set(boom)
        try:
            assert approved_egress() == 200
        finally:
            egress.CALLER_ACTOR.reset(token)
        with Session(engine) as session:
            row = session.exec(
                select(AuditLog)
                .where(AuditLog.action == "egress.private_target")
                .order_by(AuditLog.id.desc())
            ).first()
            assert row.actor == "system", "failed actor resolution must record system"


def test_transport_kwargs_forwarded():
    # Round 4 LOW-4: transport-affecting client kwargs (verify, limits, http2, ... and any future
    # one) must be built into the guarded base transport AND every guarded proxy transport, the way
    # httpx.Client builds them into the transport it makes itself. Passing them to httpx.Client
    # beside our explicit transport would silently drop them, so a self-signed connector (verify=
    # False) or a tuned pool (limits=) would be ignored with no signal.
    import ssl

    import httpx

    limits = httpx.Limits(max_connections=7, max_keepalive_connections=3)

    def carries(guarded):
        pool = guarded.inner._pool
        return pool._ssl_context.verify_mode == ssl.CERT_NONE and pool._max_connections == 7

    for sub in ("connectors", "web.search", "vendor_dispatch", "vendor_return"):
        base = egress.guarded_transport_kwargs(sub, verify=False, limits=limits)["transport"]
        assert isinstance(base, egress.GuardedTransport)
        assert carries(base), f"{sub}: base transport dropped verify/limits"
    # Under a corporate proxy the guarded PROXY transport must carry them too.
    with patch.dict(os.environ, {"HTTPS_PROXY": "http://proxy.corp:8080"}):
        kwargs = egress.guarded_transport_kwargs("connectors", verify=False, limits=limits)
        proxied = [t for t in kwargs["mounts"].values() if t is not None]
        assert proxied and all(carries(t) for t in proxied), "proxy transport dropped verify/limits"
    # Opposite condition: the default build verifies TLS and keeps httpx's default pool size.
    default = egress.guarded_transport_kwargs("connectors")["transport"]
    assert default.inner._pool._ssl_context.verify_mode == ssl.CERT_REQUIRED
    assert default.inner._pool._max_connections == 100
    # Round 5 LOW-2: an explicit transport passed WITH transport-construction kwargs builds them
    # into the wrapped transport the same way the implicit path does, instead of silently dropping
    # them on the early return.
    explicit = egress.guarded_transport_kwargs(
        "connectors", transport=httpx.HTTPTransport(), verify=False, limits=limits
    )["transport"]
    assert isinstance(explicit, egress.GuardedTransport)
    assert carries(explicit), "explicit-transport path dropped verify/limits"
    # Opposite condition: an explicit transport with NO such kwargs is guarded verbatim, so a test
    # double keeps its fixed route.
    double = httpx.MockTransport(lambda r: httpx.Response(200))
    passthru = egress.guarded_transport_kwargs("connectors", transport=double)["transport"]
    assert passthru.inner is double, "an explicit transport with no kwargs must be wrapped as-is"


def test_audit_flush_both_outcomes():
    # Round 5 MED-1 (corrects round 4 LOW-5): the audit flush runs on BOTH outcomes (request
    # completed, request raised), exactly once per logical attempt, inside a total guard. A private
    # egress whose transport raises still records its egress.private_target row (the burst of
    # refused internal hops an operator most needs), while a best-effort audit write cannot replace
    # mask the caller's own result or exception, and a BaseException in audit.log cannot lose a
    # successful response.
    import json
    from types import SimpleNamespace

    import httpx

    from app.services import audit
    from app.services.connectors.base import Connector

    def record_one():
        rows = egress._ATTEMPTS.get()
        row = rows.setdefault(
            ("jira.internal", "connectors"), {"attempt_count": 0, "approvals": set()}
        )
        row["attempt_count"] += 1
        row["approvals"].add(("10.20.30.40", "jira.internal"))

    with patch.dict(os.environ, {ENV: "10.20.0.0/16"}):
        egress.set_test_resolver(lambda h: ["10.20.30.40"])

        # A completed request flushes exactly one row (unchanged).
        with patch.object(audit, "log") as log:
            connector = SimpleNamespace(
                _transport=httpx.MockTransport(lambda r: httpx.Response(200))
            )
            with Connector._client(connector) as client:
                assert (
                    Connector._request(
                        connector, client, "GET", "https://jira.example.com"
                    ).status_code
                    == 200
                )
            assert egress._ATTEMPTS.get() is None, "contextvar must reset in finally"
            assert log.call_count == 1, "a completed request flushes its audit row"
            assert json.loads(log.call_args.kwargs["detail"])["attempt_count"] == 1

        # Round 5: a transport that raises after N retries STILL flushes one row carrying the
        # counted attempts (round 4 dropped it entirely), and the caller's own error propagates.
        def always_down(request):
            raise httpx.ConnectError("connection refused")

        with patch.object(audit, "log") as log:
            connector = SimpleNamespace(_transport=httpx.MockTransport(always_down))
            with (
                Connector._client(connector) as client,
                patch("app.services.connectors.base._sleep"),
            ):
                try:
                    Connector._request(connector, client, "GET", "https://jira.example.com")
                except httpx.ConnectError:
                    pass
                else:
                    raise AssertionError("a raising transport must surface its error")
            assert egress._ATTEMPTS.get() is None
            assert log.call_count == 1, "a raising body flushes its row on the exception path"
            assert json.loads(log.call_args.kwargs["detail"])["attempt_count"] == 3, (
                "the counted retries are carried on the exception path"
            )

        # audit.log raising an Exception never replaces the caller's own exception; a warning fires.
        with (
            patch.object(audit, "log", side_effect=RuntimeError("audit db down")),
            patch.object(egress.log, "warning") as warn,
        ):
            try:
                with egress.audit_attempts():
                    record_one()
                    raise httpx.ConnectError("transport down")
            except httpx.ConnectError:
                pass
            else:
                raise AssertionError("the caller's own exception must propagate")
            assert egress._ATTEMPTS.get() is None
            assert warn.called, "a failed audit flush logs a warning"

        # A BaseException in audit.log during a SUCCESSFUL request is swallowed by the total guard:
        # the caller's result survives and a warning fires, rather than a lost response.
        with (
            patch.object(audit, "log", side_effect=KeyboardInterrupt),
            patch.object(egress.log, "warning") as warn,
        ):
            with egress.audit_attempts():
                record_one()
            survived = True
            assert survived, "a BaseException in audit.log must not escape the flush"
            assert egress._ATTEMPTS.get() is None
            assert warn.called, "a swallowed BaseException logs a warning"


def test_allowlist_db_unavailable_fails_closed():
    # The guard NEVER requires the settings database. `settings.get` opens a Session
    # against the `setting` table; a caller that guards egress before the schema exists
    # (the tailnet suite ran the connector guard with no DB -> OperationalError) must still
    # get a decision. Two arms: DB PRESENT honors a written entry through the real DB read
    # path; DB ABSENT grants no private exceptions, never propagates the DB error,
    # and warns exactly ONCE per outage episode.
    from sqlalchemy.exc import OperationalError

    # Arm 1: store PRESENT with an entry, no env override, so the genuine DB read runs.
    with patch.dict(os.environ):
        os.environ.pop(ENV, None)
        settings.set_many({KEY: "10.20.0.0/16"})
        egress.set_test_resolver(lambda h: ["10.20.0.5"])  # in range -> approved
        assert egress.validate_target("https://jira.example.com")
        egress.set_test_resolver(lambda h: ["10.99.0.1"])  # out of range -> refused
        blocked("https://jira.example.com")
        settings.set_many({KEY: ""})

    # Arm 2: store UNREACHABLE (simulated no-table / no-engine). The guard returns
    # EgressBlocked, never the OperationalError, and logs one WARNING for the outage episode.
    boom = OperationalError("SELECT", {}, Exception("no such table: setting"))
    egress._ALLOWLIST_STORE_WARNED = False
    egress._ALLOWLIST_STORE_LAST_WARNING = float("-inf")
    with patch.dict(os.environ):
        os.environ.pop(ENV, None)
        egress.set_test_resolver(lambda h: ["10.20.0.5"])
        with (
            patch.object(settings, "get", side_effect=boom),
            patch.object(egress.log, "warning") as warn,
        ):
            blocked("https://jira.internal")  # empty allowlist -> private target refused
            blocked("https://jira.internal")  # still refused; warns only the first time
            assert warn.call_count == 1, ("store-unavailable warns exactly once", warn.call_count)
        # A resolved public target still validates with the store down (no exception needed).
        with patch.object(settings, "get", side_effect=boom):
            egress.set_test_resolver(lambda h: ["8.8.8.8"])
            assert egress.validate_target("https://public.example.com")
    egress._ALLOWLIST_STORE_WARNED = False
    egress._ALLOWLIST_STORE_LAST_WARNING = float("-inf")


def test_store_outage_dns_failure_declared_private_name():
    """MED: losing the store must not erase P4's exact-name DNS refusal."""
    target = "https://operator-private.example.com"
    with patch.dict(os.environ):
        os.environ.pop(ENV, None)
        settings.set_many({KEY: "operator-private.example.com"})
        try:
            with patch.object(egress, "_RESOLVER", side_effect_dns_failure):
                blocked(target)  # configured install refuses before the outage
                with patch.object(settings, "get", side_effect=RuntimeError("store down")):
                    message = blocked(target)
                    assert "settings store unavailable" in str(message), message
                    assert "operator-private" not in str(message), message
        finally:
            settings.set_many({KEY: ""})


def side_effect_dns_failure(host):
    raise OSError("offline DNS failure")


def test_store_outage_address_matrix():
    with patch.object(settings, "get", side_effect=RuntimeError("store down")):
        blocked("https://10.20.0.5")
        assert egress.validate_target("https://public.example.com", resolver=lambda h: ["8.8.8.8"])
    with patch.object(settings, "get", return_value=""):
        assert egress.validate_target(
            "https://public.example.com", resolver=side_effect_dns_failure
        ), "a known empty policy preserves the pre-existing DNS-failure outcome"
        assert egress.validate_target("https://public.example.com", resolver=lambda h: ["8.8.8.8"])


def test_store_outage_warning_episodes():
    with (
        patch.object(egress, "_ALLOWLIST_STORE_WARNED", False),
        patch.object(egress, "_ALLOWLIST_STORE_LAST_WARNING", float("-inf")),
        patch.object(egress.time, "monotonic", return_value=100.0) as clock,
        patch.object(egress.log, "warning") as warn,
    ):
        for episode in range(2):
            clock.return_value = 100.0 + episode * (egress._ALLOWLIST_STORE_WARNING_COOLDOWN + 1)
            with patch.object(settings, "get", side_effect=RuntimeError("store down")):
                for _ in range(3):
                    assert egress._allowlist_entries() == ([], False)
                clock.return_value += egress._ALLOWLIST_STORE_WARNING_COOLDOWN + 1
                assert egress._allowlist_entries() == ([], False)
                assert warn.call_count == episode + 1, "one warning per burst"
            with patch.object(settings, "get", return_value=""):
                assert egress._allowlist_entries() == ([], True)
        assert warn.call_count == 2, "successful read must re-arm the outage warning"


def test_store_outage_warning_flapping():
    with (
        patch.object(egress, "_ALLOWLIST_STORE_WARNED", False),
        patch.object(egress, "_ALLOWLIST_STORE_LAST_WARNING", float("-inf")),
        patch.object(egress.time, "monotonic", return_value=100.0) as clock,
        patch.object(egress.log, "warning") as warn,
    ):
        for offset in (0, 1, 2, egress._ALLOWLIST_STORE_WARNING_COOLDOWN):
            clock.return_value = 100.0 + offset
            with patch.object(settings, "get", side_effect=RuntimeError("store down")):
                assert egress._allowlist_entries() == ([], False)
            with patch.object(settings, "get", return_value=""):
                assert egress._allowlist_entries() == ([], True)
        assert warn.call_count == 1, ("flapping cooldown", warn.call_count)


def test_store_outage_warning_concurrent_burst():
    import inspect
    import threading

    source, start = inspect.getsourcelines(egress._allowlist_entries)
    assignment = start + next(
        i for i, line in enumerate(source) if "_ALLOWLIST_STORE_WARNED = True" in line
    )
    burst = threading.Barrier(8)
    unchecked = threading.Barrier(8)
    errors = []

    def unavailable(*args):
        burst.wait(timeout=10)
        raise RuntimeError("store down")

    def tracer(frame, event, arg):
        # Force the adjudication's check/set race only when the assignment is
        # unprotected. Waiting here under the real lock would deadlock the probe.
        if (
            frame.f_code is egress._allowlist_entries.__code__
            and event == "line"
            and frame.f_lineno == assignment
            and not egress._ALLOWLIST_STORE_LOCK.locked()
        ):
            unchecked.wait(timeout=10)
        return tracer

    def worker():
        try:
            assert egress._allowlist_entries() == ([], False)
        except BaseException as exc:
            errors.append(type(exc).__name__)

    with (
        patch.object(egress, "_ALLOWLIST_STORE_WARNED", False),
        patch.object(egress, "_ALLOWLIST_STORE_LAST_WARNING", float("-inf")),
        patch.object(settings, "get", side_effect=unavailable),
        patch.object(egress.log, "warning") as warn,
    ):
        previous_trace = threading.gettrace()
        threading.settrace(tracer)
        threads = [threading.Thread(target=worker, daemon=True) for _ in range(8)]
        try:
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(15)
        finally:
            threading.settrace(previous_trace)
        assert not errors and not any(thread.is_alive() for thread in threads), errors
        assert warn.call_count == 1, ("concurrent burst", warn.call_count)
        assert warn.call_args.kwargs["exc_info"] is True


def _rebind_probe(second_answer, *, allowed=False, https=False, offline=False, proxy=False):
    import socket
    import ssl
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    import httpx
    from httpcore._backends.sync import SyncStream

    from app.services.connectors.base import ConnectorError

    received = []
    tls_calls = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            assert self.headers["Host"] == f"rebind.test:{server.server_port}"
            received.append(self.headers.get("Authorization"))
            self.send_response(200)
            self.send_header("Content-Length", "0")
            self.end_headers()

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    original_resolve = socket.getaddrinfo
    original_connect = socket.socket.connect
    original_info = SyncStream.get_extra_info
    original_blocked = egress._is_blocked_ip
    lookups = []

    def resolve(host, port, *args, **kwargs):
        # Only the pinned validated answer (or the proxy's own address) may reach
        # the OS resolver; the rebinding name itself never does.
        assert host in ("rebind.test", second_answer), host  # No external DNS.
        lookups.append(host)
        return original_resolve(second_answer, port, *args, **kwargs)

    def connect(sock, address):
        # Metadata/CGNAT cannot be bound on this machine. Route their synthetic
        # sockets to the listener and report the simulated peer below.
        return original_connect(sock, ("127.0.0.1", address[1]))

    resolver_lookups = {"n": 0}

    def resolver(host):
        # The attacker alternates answers: the preflight sees a public one, every
        # later lookup (the pre-connect pin in connect_tcp) gets the rebinding.
        assert host == "rebind.test"
        resolver_lookups["n"] += 1
        return ["93.184.216.34"] if resolver_lookups["n"] % 2 == 1 else [second_answer]

    def info(stream, key):
        if key == "server_addr" and second_answer != "127.0.0.1":
            return (second_answer, server.server_port)
        return original_info(stream, key)

    def tls(stream, ssl_context, server_hostname=None, timeout=None):
        # Inspect httpcore's actual TLS invocation; no test certificate dependency.
        assert ssl_context.check_hostname
        assert ssl_context.verify_mode == ssl.CERT_REQUIRED
        assert server_hostname == "rebind.test"
        tls_calls.append(server_hostname)
        return stream

    refused = False
    try:
        with (
            patch.object(egress, "_RESOLVER", None if offline else resolver),
            patch.dict(os.environ, {ENV: ""}),
            patch.object(socket, "getaddrinfo", resolve),
            patch.object(socket.socket, "connect", connect),
            patch.object(SyncStream, "get_extra_info", info),
            patch.object(SyncStream, "start_tls", tls),
            patch.object(
                egress,
                "ALWAYS_BLOCKED",
                tuple(
                    n for n in egress.ALWAYS_BLOCKED if not (allowed and str(n) == "127.0.0.0/8")
                ),
            ),
            patch.object(
                egress,
                "_is_blocked_ip",
                lambda ip: original_blocked(ip) and not (allowed and ip.is_loopback),
            ),
            httpx.Client(
                transport=egress.GuardedTransport(
                    "connectors",
                    httpx.HTTPTransport(proxy=f"http://127.0.0.1:{server.server_port}")
                    if proxy
                    else None,
                    error_type=ConnectorError,
                ),
                timeout=2,
            ) as client,
        ):
            try:
                client.get(
                    f"{'https' if https else 'http'}://rebind.test:{server.server_port}/api/i18n/locales",
                    headers={"Authorization": "Bearer rebind-regression"},
                )
            except ConnectorError:
                refused = True
    finally:
        server.shutdown()
        server.server_close()
        thread.join(2)
    if proxy:
        expected_lookups = ["127.0.0.1"]
    elif offline:
        expected_lookups = ["rebind.test", second_answer]
    elif allowed:
        expected_lookups = [second_answer]
    else:
        expected_lookups = []
    assert lookups == expected_lookups, (lookups, expected_lookups)
    if allowed or proxy:
        assert received == ["Bearer rebind-regression"] and not refused
        assert tls_calls == (["rebind.test"] if https else [])
        return
    assert not tls_calls, "a denied peer must be closed even before TLS"
    assert not received and refused, (
        f"DNS rebind to {second_answer}: {len(received)} requests reached listener; "
        f"guard refusal={refused}"
    )
    assert resolver_lookups["n"] == 2, (
        f"expected exactly a preflight and a pre-connect pin lookup, got {resolver_lookups['n']}"
    )


def test_rebind_loopback():
    _rebind_probe("127.0.0.1")


def test_rebind_metadata():
    _rebind_probe("169.254.169.254")


def test_rebind_tailnet():
    _rebind_probe("100.64.0.7")


def test_rebind_before_tls():
    _rebind_probe("127.0.0.1", https=True)


def test_rebind_open_vs_closed_ports_indistinguishable():
    # #327: through a rebinding name the guard must not leak internal port state.
    # The pre-connect pin refuses before any TCP handshake, so an OPEN and a
    # CLOSED internal port give the SAME caller-visible answer and the open port
    # never sees a connection.
    import socket
    import threading

    import httpx

    from app.services.connectors.base import ConnectorError

    accepted = []
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.bind(("127.0.0.1", 0))
    listener.listen(16)
    open_port = listener.getsockname()[1]

    def accept_loop():
        while True:
            try:
                conn, _ = listener.accept()
            except OSError:
                return
            accepted.append(conn.getpeername())
            conn.close()

    accept_thread = threading.Thread(target=accept_loop, daemon=True)
    accept_thread.start()
    # A CLOSED port: bind to learn a currently-free port, then release it.
    scope = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    scope.bind(("127.0.0.1", 0))
    closed_port = scope.getsockname()[1]
    scope.close()

    resolver_lookups = {"n": 0}

    def resolver(host):
        # The attacker alternates answers: preflights see a public one, the
        # pre-connect pin sees the rebinding answer to the internal address.
        assert host == "scan.test"
        resolver_lookups["n"] += 1
        return ["93.184.216.34"] if resolver_lookups["n"] % 2 == 1 else ["127.0.0.1"]

    # The OS resolver rebinds too; a pinned dial must never reach it.
    original_resolve = socket.getaddrinfo

    def resolve(host, port, *args, **kwargs):
        if host == "scan.test":
            return original_resolve("127.0.0.1", port, *args, **kwargs)
        return original_resolve(host, port, *args, **kwargs)

    original_connect = socket.socket.connect
    dials = []

    def connect(sock, address):
        dials.append(address)
        return original_connect(sock, address)

    errors = {}
    try:
        with (
            patch.object(egress, "_RESOLVER", resolver),
            patch.dict(os.environ, {ENV: ""}),
            patch.object(socket, "getaddrinfo", resolve),
            patch.object(socket.socket, "connect", connect),
            httpx.Client(
                transport=egress.GuardedTransport("connectors", None, error_type=ConnectorError),
                timeout=2,
            ) as client,
        ):
            for label, port in (("open", open_port), ("closed", closed_port)):
                try:
                    client.get(f"http://scan.test:{port}/")
                except ConnectorError as exc:
                    errors[label] = str(exc)
    finally:
        listener.close()
        accept_thread.join(2)
    assert set(errors) == {"open", "closed"}, (
        f"both refused internal ports must raise the guard error, got: {errors}"
    )
    # Same caller-visible answer: the open port must not read differently.
    assert errors["open"] == errors["closed"], (errors["open"], errors["closed"])
    assert errors["open"].startswith("Blocked target"), errors["open"]
    # The refusal is pre-connect: no dial was attempted and the open internal
    # port saw no connection.
    assert not dials, dials
    assert not accepted, accepted
    assert resolver_lookups["n"] == 4, (
        f"expected a preflight and a pre-connect pin lookup per request, "
        f"got {resolver_lookups['n']}"
    )


def test_pin_unresolvable_name_refused_before_dial():
    # #327 self-review HIGH-1: when the pin's resolver cannot answer a name (raises
    # or answers empty), the guard must REFUSE before any dial. Dialing the name
    # would let per-query rebinds (public to the preflight, nothing to the pin,
    # internal to the OS dial) complete a TCP handshake with an open internal
    # port and leak open/closed state again.
    import socket
    import threading

    import httpx

    from app.services.connectors.base import ConnectorError

    accepted = []
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.bind(("127.0.0.1", 0))
    listener.listen(16)
    open_port = listener.getsockname()[1]

    def accept_loop():
        while True:
            try:
                conn, _ = listener.accept()
            except OSError:
                return
            accepted.append(conn.getpeername())
            conn.close()

    accept_thread = threading.Thread(target=accept_loop, daemon=True)
    accept_thread.start()

    # The OS dial would answer the rebinding name with the internal address.
    original_resolve = socket.getaddrinfo

    def resolve(host, port, *args, **kwargs):
        return original_resolve("127.0.0.1", port, *args, **kwargs)

    original_connect = socket.socket.connect
    dials = []

    def connect(sock, address):
        dials.append(address)
        return original_connect(sock, address)

    class PinFail:
        """Preflight (call 1) sees a public answer; the pin (call 2) fails."""

        def __init__(self, mode):
            self.mode = mode
            self.n = 0

        def __call__(self, host):
            self.n += 1
            if self.n % 2 == 1:
                return ["93.184.216.34"]
            if self.mode == "empty":
                return []
            raise socket.gaierror("simulated resolver outage")

    errors = {}
    try:
        with (
            patch.dict(os.environ, {ENV: ""}),
            patch.object(socket, "getaddrinfo", resolve),
            patch.object(socket.socket, "connect", connect),
        ):
            for label in ("empty", "raise"):
                with (
                    patch.object(egress, "_RESOLVER", PinFail(label)),
                    httpx.Client(
                        transport=egress.GuardedTransport(
                            "connectors", None, error_type=ConnectorError
                        ),
                        timeout=2,
                    ) as client,
                ):
                    try:
                        client.get(f"http://scan.test:{open_port}/")
                    except ConnectorError as exc:
                        raise AssertionError(
                            f"{label}: an unresolvable name is a connection failure, "
                            f"not a policy block: {exc}"
                        ) from exc
                    except httpx.ConnectError as exc:
                        errors[label] = str(exc)
    finally:
        listener.close()
        accept_thread.join(2)
    # Gate note 1: a resolution failure is a normal httpx.ConnectError (a
    # TransportError, so connector retries and web.search treat it as a transport
    # failure), never the guard's policy error.
    assert set(errors) == {"empty", "raise"}, (
        f"an unresolvable name must raise httpx.ConnectError, got: {errors}"
    )
    for label, message in errors.items():
        assert not message.startswith("Blocked target"), (label, message)
        assert "scan.test" in message, (label, message)
    # The refusal is pre-dial: no TCP handshake, the open internal port saw nothing.
    assert not dials, dials
    assert not accepted, accepted


def test_default_resolver_preserves_getaddrinfo_order():
    # Gate note 2: the pin dials answers in getaddrinfo (RFC 6724) order; a set
    # de-dup would randomise IPv4/IPv6 order.
    import socket

    answers = [
        (socket.AF_INET6, 0, 0, "", ("2606:4700::1", 0, 0, 0)),
        (socket.AF_INET, 0, 0, "", ("93.184.216.34", 0)),
        (socket.AF_INET6, 0, 0, "", ("2606:4700::1", 0, 0, 0)),
        (socket.AF_INET, 0, 0, "", ("8.8.8.8", 0)),
        (socket.AF_INET6, 0, 0, "", ("2001:db8::9", 0, 0, 0)),
        (socket.AF_INET, 0, 0, "", ("93.184.216.34", 0)),
    ]
    want = ["2606:4700::1", "93.184.216.34", "8.8.8.8", "2001:db8::9"]
    with patch.object(socket, "getaddrinfo", lambda host, port, *a, **k: answers):
        for _ in range(50):
            assert egress._default_resolver("dual.test") == want


def test_pin_uses_transport_resolver():
    # Gate note 3: the pre-connect pin resolves with the transport's OWN resolver when one
    # is passed (not only the global seam), and a bad answer from it is refused before
    # any dial.
    import socket
    from unittest.mock import Mock

    calls = []

    def own(host):
        calls.append(host)
        return ["93.184.216.34"] if len(calls) % 2 == 1 else ["127.0.0.1"]

    def other_global(host):
        raise AssertionError("global resolver must not be consulted when the transport has one")

    dials = []
    original_connect = socket.socket.connect

    def connect(sock, address):
        dials.append(address)
        return original_connect(sock, address)

    with (
        patch.object(egress, "_RESOLVER", other_global),
        patch.dict(os.environ, {ENV: ""}),
        patch.object(socket.socket, "connect", connect),
    ):
        guard = egress.GuardedTransport("connectors", None, resolver=own)
        backend = guard.inner._pool._network_backend
        assert isinstance(backend, egress._PeerCheckedBackend) and backend.resolver is own
        inner = Mock()
        backend.inner = inner
        # Pin lookup 1: the public answer is accepted and dialed (mock inner).
        inner.connect_tcp.return_value.get_extra_info.return_value = ("93.184.216.34", 80)
        backend.connect_tcp("pin.test", 80)
        assert calls == ["pin.test"] and inner.connect_tcp.call_args[0][0] == "93.184.216.34"
        # Pin lookup 2: the same transport resolver now answers loopback: refused.
        try:
            backend.connect_tcp("pin.test", 80)
        except egress.EgressBlocked:
            pass
        else:
            raise AssertionError("a bad answer from the transport resolver was dialed")
        assert calls == ["pin.test", "pin.test"]
        assert inner.connect_tcp.call_count == 1 and not dials
        guard.close()


def test_guard_fails_closed_if_httpcore_seam_moves():
    # Gate note 4: if httpcore stops reading pool._network_backend, refuse to build the
    # client rather than silently degrade to preflight-only.
    import httpcore
    import httpx

    # Healthy seam builds fine (and installs our backend).
    ok = egress.GuardedTransport("connectors")
    assert isinstance(ok.inner._pool._network_backend, egress._PeerCheckedBackend)
    ok.close()

    # (a) the attribute is gone.
    class NoAttr:
        pass

    try:
        egress._install_peer_backend(NoAttr(), "connectors", egress.EgressBlocked, None)
    except RuntimeError as exc:
        assert "_network_backend" in str(exc)
    else:
        raise AssertionError("a pool without the seam must refuse")

    # (b) the attribute exists but the pool no longer hands it to connections.
    class DeadSeam(httpcore.ConnectionPool):
        def create_connection(self, origin):
            return httpcore.HTTPConnection(origin=origin)  # ignores self._network_backend

    try:
        egress._install_peer_backend(DeadSeam(), "connectors", egress.EgressBlocked, None)
    except RuntimeError as exc:
        assert "no longer hands" in str(exc)
    else:
        raise AssertionError("a pool that ignores the seam must refuse")

    # And through the public constructor with a transport whose pool ignores the seam.
    real = httpx.HTTPTransport()
    real._pool = DeadSeam()
    try:
        egress.GuardedTransport("connectors", real)
    except RuntimeError:
        pass
    else:
        raise AssertionError("GuardedTransport must not build over a dead seam")


def test_connected_name_and_tls():
    _rebind_probe("127.0.0.1", allowed=True)
    _rebind_probe("127.0.0.1", allowed=True, https=True)
    _rebind_probe("127.0.0.1", allowed=True, offline=True)


def test_explicit_private_proxy_connection():
    _rebind_probe("127.0.0.1", proxy=True)


def test_peer_ipv6_and_private_exception():
    from unittest.mock import Mock

    import httpx

    # Public IPv6 and approved private peers keep the original hostname policy.
    # The pin resolves the name to the address being simulated so the post-connect
    # peer check below is what runs, not the pre-connect pin.
    for address, entry in (("2606:4700:4700::1111", ""), ("10.20.30.40", "peer.test")):
        inner = httpx.HTTPTransport()
        backend = inner._pool._network_backend
        stream = Mock()
        stream.get_extra_info.return_value = (address, 80)
        with (
            patch.object(backend, "connect_tcp", return_value=stream),
            patch.object(egress, "_RESOLVER", lambda h, address=address: [address]),
            patch.dict(os.environ, {ENV: entry}),
        ):
            guard = egress.GuardedTransport("connectors", inner)
            assert guard.inner._pool._network_backend.connect_tcp("peer.test", 80) is stream
            stream.close.assert_not_called()
            guard.close()


def test_peer_missing_or_denied_is_closed():
    from unittest.mock import Mock

    import httpx

    for host, peer in (
        ("peer.test", None),
        ("peer.test", ("not-an-ip", 80)),
        ("peer.test", ("10.20.30.40", 80)),
        ("peer.test", ("::ffff:127.0.0.1", 80)),
        ("8.8.8.8.", ("127.0.0.1", 80)),
    ):
        inner = httpx.HTTPTransport()
        stream = Mock()
        stream.get_extra_info.return_value = peer
        with (
            patch.object(inner._pool._network_backend, "connect_tcp", return_value=stream),
            # The pin sees a public answer and passes; the mocked stream's peer is
            # what the post-connect check must refuse. IP-literal hosts skip the pin.
            patch.object(egress, "_RESOLVER", lambda h: ["8.8.8.8"]),
            patch.dict(os.environ, {ENV: ""}),
        ):
            guard = egress.GuardedTransport("connectors", inner)
            try:
                guard.inner._pool._network_backend.connect_tcp(host, 80)
            except egress.EgressBlocked:
                pass
            else:
                raise AssertionError(f"accepted connected peer {peer}")
            stream.close.assert_called_once()
            stream.write.assert_not_called()
            stream.start_tls.assert_not_called()
            guard.close()


def test_all_dns_answers_still_required():
    import httpx

    for answers in (
        ["93.184.216.34", "8.8.8.8"],
        ["93.184.216.34", "127.0.0.1"],
        ["100.64.0.7", "93.184.216.34"],
    ):
        sent = []

        def reply(request, sent=sent):
            sent.append(request)
            return httpx.Response(200)

        with (
            patch.dict(os.environ, {ENV: ""}),
            httpx.Client(
                transport=egress.GuardedTransport(
                    "connectors",
                    httpx.MockTransport(reply),
                    resolver=lambda h, answers=answers: answers,
                )
            ) as client,
        ):
            try:
                client.get("http://multi.test")
            except egress.EgressBlocked:
                assert answers != ["93.184.216.34", "8.8.8.8"]
            assert bool(sent) == (answers == ["93.184.216.34", "8.8.8.8"])


def test_dependency_pin():
    # LOW-2: the direct idna import edge in app/services/egress.py must be pinned.
    from pathlib import Path

    req = (Path(__file__).resolve().parents[1] / "requirements.txt").read_text(encoding="utf-8")
    assert "idna==" in req, "idna import edge must be pinned in requirements.txt"


def main():
    init_db()
    settings.set_many({"audit_enabled": "1"})
    invoked = set()
    failures = []
    for test in (
        test_rebind_loopback,
        test_rebind_metadata,
        test_rebind_tailnet,
        test_rebind_before_tls,
        test_rebind_open_vs_closed_ports_indistinguishable,
        test_pin_unresolvable_name_refused_before_dial,
        test_default_resolver_preserves_getaddrinfo_order,
        test_pin_uses_transport_resolver,
        test_guard_fails_closed_if_httpcore_seam_moves,
        test_connected_name_and_tls,
        test_explicit_private_proxy_connection,
        test_peer_ipv6_and_private_exception,
        test_peer_missing_or_denied_is_closed,
        test_all_dns_answers_still_required,
        test_policy,
        test_idna_and_errors,
        test_http_connector_error,
        test_proxy_mounts,
        test_single_validation_seam,
        test_audit_best_effort,
        test_transport_kwargs_forwarded,
        test_audit_flush_both_outcomes,
        test_allowlist_db_unavailable_fails_closed,
        test_store_outage_dns_failure_declared_private_name,
        test_store_outage_address_matrix,
        test_store_outage_warning_episodes,
        test_store_outage_warning_flapping,
        test_store_outage_warning_concurrent_burst,
        test_dependency_pin,
        test_validator,
        test_refusal_messages,
        test_invalid_env,
        test_redirects_and_audit,
        test_surfaces_and_remote_lock,
        test_fixed_boundaries,
        test_descriptors,
        test_request_audit_ip,
        test_vendor_poll_policy,
        test_invalid_entrypoint,
        test_resolution_required,
    ):
        invoked.add(test.__name__)
        try:
            test()
        except Exception as exc:
            import traceback

            traceback.print_exc()
            failures.append(f"{test.__name__}: {type(exc).__name__}: {exc}")
    assert invoked == {n for n, v in globals().items() if n.startswith("test_") and callable(v)}
    assert not failures, "\n".join(failures)
    print("verify_egress_policy OK")


if __name__ == "__main__":
    main()
