"""External-origin authority and private route-path import ratchets; offline."""

import ast
import json
import os
import re
import sys
from collections import Counter
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

_ROOT = Path(__file__).resolve().parents[1]
os.chdir(_ROOT)
sys.path.insert(0, str(_ROOT))
from tests._bootstrap import init_env

init_env("loctms_external_origin_")

from fastapi.testclient import TestClient
from starlette._utils import get_route_path
from starlette.requests import Request

from app.db import init_db
from app.main import app
from app.models import Role
from app.services import apikeys, oidc, saml, security, settings
from app.services.connectors import oauth
from app.web import route_path as routing
from tests._csrf import csrf_for


def request(path="/auth/saml/acs", root="", host="attacker.example", scheme="http"):
    return Request(
        {
            "type": "http",
            "method": "GET",
            "path": path,
            "root_path": root,
            "scheme": scheme,
            "server": ("internal", 80),
            "headers": [(b"host", host.encode())],
            "query_string": b"",
            "session": {},
        }
    )


def outputs(client, raw):
    req = request()
    inv = SimpleNamespace(
        email="invite@example.test",
        role="translator",
        token="fixture-invite",  # pregate: allow-secret deterministic invitation fixture
    )
    with (
        patch("app.web.admin.security.create_invite", return_value=inv),
        patch("app.web.admin.mailer.send", return_value=True) as mail,
    ):
        response = client.post(
            "/users/invite",
            data={
                "email": inv.email,
                "role": inv.role,
                "csrf_token": csrf_for(client),
            },
            follow_redirects=False,
        )
        assert response.status_code == 303, response.text
        body = mail.call_args.args[2]
    headers = {"Authorization": f"Bearer {raw}"}
    sp = client.get("/scim/v2/ServiceProviderConfig", headers=headers)
    assert sp.status_code == 200, sp.text
    user = client.get("/scim/v2/Users", headers=headers)
    assert user.status_code == 200, user.text
    manual = client.get("/utcp")
    assert manual.status_code == 200, manual.text
    prepared = saml._prepare(req, routed_path=routing.route_path(req.scope))
    out = {
        "invitation": body,
        "scim_config": sp.json()["meta"]["location"],
        "scim_users": [u["meta"]["location"] for u in user.json()["Resources"]],
        "saml_entity": saml._sp_entity_id(req),
        "saml_acs": saml._acs_url(req),
        "saml_prepare": {k: prepared[k] for k in ("https", "http_host", "server_port")},
        "utcp": [t["tool_call_template"]["url"] for t in manual.json()["tools"]],
        "oidc": oidc.redirect_uri(req),
        "connector_oauth": oauth.redirect_uri(req),
    }
    for page in ("security", "help"):
        response = client.get("/" + page)
        assert response.status_code == 200, response.text
        out[page + "_guidance"] = re.findall(
            r"<code>(https?://[^<]+/(?:auth/oidc|connectors/oauth)/callback)</code>", response.text
        )
        assert out[page + "_guidance"], page
    return out


def main():
    init_db()
    settings.set_many({"scim_enabled": "1", "app_base_url": ""})
    admin = security.create_user(
        "origin@example.test",
        "Origin",
        Role.ADMIN,
        "fixture-password",  # pregate: allow-secret disposable local account password
    )
    _, raw = apikeys.create_key(admin.id, "origin", scopes="scim")
    client = TestClient(app, base_url="http://attacker.example")
    baseline = outputs(client, raw)
    if "--capture" in sys.argv:
        print(json.dumps(baseline, indent=2))
        return
    failures = []

    def check(name, fn):
        try:
            fn()
        except Exception as exc:
            line = f"RED {name}: {type(exc).__name__}: {exc}"
            print(line)
            failures.append(line)

    def equal(actual, expected):
        assert actual == expected, f"{actual!r} != {expected!r}"

    check("unset byte-identical", lambda: equal(baseline, PINNED))
    settings.set_many({"app_base_url": "https://tms.example.com"})
    configured = outputs(client, raw)
    for name, value in PINNED.items():
        expected = json.loads(
            json.dumps(value).replace("http://attacker.example", "https://tms.example.com")
        )
        if name == "saml_prepare":
            expected = {"https": "on", "http_host": "tms.example.com", "server_port": "443"}
        check(name, lambda name=name, expected=expected: equal(configured[name], expected))

    def precedence():
        settings.set_many(
            {"saml_sp_entity_id": "urn:explicit:sp", "saml_sp_acs_url": "https://explicit.test/acs"}
        )
        equal(saml._sp_entity_id(request()), "urn:explicit:sp")
        equal(saml._acs_url(request()), "https://explicit.test/acs")

    check("explicit SAML wins", precedence)

    def origin_edges():
        settings.set_many({"saml_sp_entity_id": "", "saml_sp_acs_url": ""})
        for origin, expected in (
            ("https://tms.example.com:8443/", ("on", "tms.example.com:8443", "8443")),
            ("https://tms.example.com:443", ("on", "tms.example.com", "443")),
            ("http://tms.example.com:80", ("off", "tms.example.com", "80")),
            ("http://tms.example.com:8080", ("off", "tms.example.com:8080", "8080")),
            ("https://[::1]:8443", ("on", "[::1]:8443", "8443")),
        ):
            settings.set_many({"app_base_url": origin})
            req = request("/auth/saml/acs", "/tms")
            prepared = saml._prepare(req, routed_path=routing.route_path(req.scope))
            equal(tuple(prepared[k] for k in ("https", "http_host", "server_port")), expected)
            equal(prepared["script_name"], "/auth/saml/acs")
            equal(settings.external_base_url(), origin.rstrip("/"))
        settings.set_many({"app_base_url": ""})
        equal(settings.external_base_url(), "")
        equal(settings.external_base_url(request("/login", "/tms")), "http://attacker.example/tms")
        # Exact historical fallback strings, including explicit default ports and IPv6.
        for host, scheme, expected in (
            ("attacker.example:80", "http", ("off", "attacker.example:80", "80")),
            ("attacker.example:443", "https", ("on", "attacker.example:443", "443")),
            ("attacker.example:8080", "http", ("off", "attacker.example:8080", "8080")),
            ("[::1]:8443", "https", ("on", "::1:8443", "8443")),
        ):
            req = request(host=host, scheme=scheme)
            prepared = saml._prepare(req, routed_path=routing.route_path(req.scope))
            equal(tuple(prepared[k] for k in ("https", "http_host", "server_port")), expected)
        settings.set_many({"app_base_url": "https://tms.example.com"})
        built = saml.build_settings(request(), require_idp=False)["sp"]
        equal(built["entityId"], "https://tms.example.com/auth/saml/metadata")
        equal(built["assertionConsumerService"]["url"], "https://tms.example.com/auth/saml/acs")

    check("origin edge matrix", origin_edges)

    def funnel():
        for path, root in (
            ("/customers", ""),
            ("/back-office/login", "/back-office"),
            ("/login", "/back-office"),
            ("/a%2Fb", ""),
            ("/a/b", ""),
            ("/routing", "/tms"),
            ("/tms/routing", "/tms"),
            ("/tms-other", "/tms"),
            ("/tms", "/tms"),
            ("", ""),
            ("/", ""),
            ("/tms/", "/tms"),
            ("/tms/tms/login", "/tms"),
            ("/tms%2Flogin", "/tms"),
            ("/back-office-other/login", "/back-office"),
        ):
            req = request(path, root)
            equal(routing.route_path(req.scope), get_route_path(req.scope))

    check("route funnel", funnel)

    def validator():
        settings.set_many({"app_base_url": "https://tms.example.com"})
        for value in (
            "javascript:alert(1)",
            "https:///missing",
            "https://u:p@example.test",  # pregate: allow-secret synthetic rejected userinfo
            "https://example.test/#fragment",
            "https://example.test:99999",
            "https://bad host.test",
            "https://example.test/\\evil",
            "https://example.test/?query",
            "https://example.test/#",
            "https://example.test:0",
            "https://example.test/\npath",
        ):
            try:
                settings.set_many({"app_base_url": value})
            except settings.InvalidSettingValue:
                equal(settings.get("app_base_url"), "https://tms.example.com")
                continue
            raise AssertionError(f"accepted {value!r}")
        settings.set_many({"app_base_url": " HTTPS://TMS.Example.COM/Tms/// "})
        equal(settings.get("app_base_url"), "https://tms.example.com/Tms")
        for path, error in (("auth/callback", ValueError), ("", ValueError), (None, TypeError)):
            try:
                settings.external_url(request(), path)
            except error:
                pass
            else:
                raise AssertionError("unrooted path accepted")

    check("origin validator", validator)

    def proxy_warning():
        settings.set_many({"app_base_url": "https://tms.example.com", "saml_enabled": "1"})
        with (
            patch.object(saml, "available", return_value=True),
            patch.object(security, "proxy_networks", return_value=[]),
        ):
            saml.configure_at_boot()
            assert "trusted_proxy_ips" in saml.PROXY_WARNING
            assert "client" in saml.PROXY_WARNING
            for origin in ("", "http://tms.example.com"):
                settings.set_many({"app_base_url": origin})
                saml.configure_at_boot()
                assert "trusted_proxy_ips" in saml.PROXY_WARNING
                if not origin:
                    equal(
                        saml.PROXY_WARNING,
                        "SAML behind a proxy needs trusted_proxy_ips; "
                        "X-Forwarded-Proto is not honored",
                    )

    check("proxy warning independent of external origin", proxy_warning)

    def legacy_fallback():
        from app.services import notifications

        settings.set_many({"app_base_url": "", "saml_enabled": "1"})
        settings.external_base_url()
        real_get = settings.get

        def legacy_get(key, default=None):
            return "tms.example.com" if key == "app_base_url" else real_get(key, default)

        with (
            patch.object(settings, "get", side_effect=legacy_get),
            patch.object(settings.log, "warning") as warning,
            patch.object(saml, "available", return_value=True),
        ):
            saml.configure_at_boot()
            equal(outputs(client, raw), baseline)
            for path in ("/security", "/help", "/users", "/utcp"):
                response = client.get(path)
                equal(response.status_code, 200)
                if path == "/security":
                    assert 'value="tms.example.com"' in response.text
                    assert "config set app_base_url https://" in response.text
            for path in ("ServiceProviderConfig", "Users", "Groups", "Schemas", "ResourceTypes"):
                equal(
                    client.get(
                        "/scim/v2/" + path, headers={"Authorization": f"Bearer {raw}"}
                    ).status_code,
                    200,
                )
            body = notifications._query_body("Org", "Subject", "Message", 1, "Lead")
            assert "tms.example.com" not in body
            equal(settings.external_base_url(), "")
            equal(warning.call_count, 1)
            assert "config set app_base_url https://" in warning.call_args.args[0]
        settings.external_base_url()
        with (
            patch.object(settings, "get", side_effect=legacy_get),
            patch.object(settings.log, "warning") as warning,
        ):
            settings.external_base_url()
            equal(warning.call_count, 1)

    check("legacy fallback and once per episode warning", legacy_fallback)

    def prefix_matrix():
        from onelogin.saml2.utils import OneLogin_Saml2_Utils

        for base in ("", "https://tms.example.com", "https://tms.example.com/tms"):
            settings.set_many({"app_base_url": base})
            for root in ("", "/tms"):
                for path in ("/auth/saml/acs", root + "/auth/saml/acs"):
                    req = request(path, root)
                    equal(
                        OneLogin_Saml2_Utils.get_self_url_no_query(
                            saml._prepare(req, routed_path=routing.route_path(req.scope))
                        ),
                        saml._acs_url(req),
                    )
                rooted_client = TestClient(app, root_path=root)
                page = rooted_client.get("/security")
                equal(page.status_code, 200)
                equal(
                    "app_base_url has no path" in page.text,
                    bool(root and base == "https://tms.example.com"),
                )

    check("SAML Destination prefix matrix and UI warning", prefix_matrix)

    def saml_handoff():
        tree = ast.parse((_ROOT / "app/web/auth.py").read_text(encoding="utf-8"))
        handoffs = [
            node.value
            for node in ast.walk(tree)
            if isinstance(node, ast.keyword) and node.arg == "routed_path"
        ]
        assert len(handoffs) >= 2, "SAML login and ACS must both supply routed_path"
        expected = ast.dump(ast.parse("routing.route_path(request.scope)", mode="eval").body)
        for value in handoffs:
            assert ast.dump(value) == expected, f"unsafe SAML handoff: {ast.unparse(value)}"
        assert any(
            isinstance(node, ast.ImportFrom)
            and node.module == "app.web"
            and any(
                alias.name == "route_path" and alias.asname == "routing" for alias in node.names
            )
            for node in tree.body
        ), "SAML routing alias must resolve to the leaf route_path module"

    check("SAML route handoff AST", saml_handoff)

    def ratchets():
        imports, base_reads = [], []
        # These are provider profile fields, CLI arguments and outbound client state,
        # never inbound Requests. Pin expression AND count, not entire file exemptions.
        non_request = Counter(
            {
                ("app/cli/cmd_engine.py", "args.base_url"): 2,
                ("app/cli/cmd_engine.py", "prof.base_url"): 1,
                ("app/cli/cmd_mcp.py", "args.base_url"): 1,
                # #264: the sixth use validates the outbound URL before transport
                # creation (check_plain_http); it is not another network sink.
                # #329: uses 7-8 name the server in stream_to_file's "could not reach the
                # server at ..." errors, mirroring _send's two; messages, not network sinks.
                ("app/cli/_remote.py", "self.base_url"): 8,
                # #354: the tailnet-route read of self.base_url is gone (the client now rides
                # the egress guard), so 3 -> 2.
                # #144: test_profile runs the egress door on the stored target before
                # dispatching to the per-kind probe (10 -> 11), and update_profile
                # compares the submitted URL against the stored one so an unchanged
                # URL is not revalidated (11 -> 12).
                # #186: profile_warnings only parses the host to advise about keyless LAN use.
                # #186: test_profile refuses TypeSafe for jev_local before egress validation.
                # #186: _probe_jev pins hosted URLs; local URLs use _client's egress door.
                # FX-4: the Edit notice compares the stored profile URL with its display value.
                # #218: clear_slot_following_keys reads it only to find blank-URL profiles
                # that inherit the local slot, never to send anything.
                ("app/services/engines.py", "prof.base_url"): 20,
                # SRP split 2026-09-15: the providers reads now live in the pieces
                # fix-engine-repoint gate round 1 HIGH-1: both fallback sites pass the
                # profile's own URL to slot_key_for to decide the bearer (2 -> 4).
                ("app/services/providers_hub.py", "prof.base_url"): 4,
                # #186: profile_has_credential checks jev_local URL presence, never sends it.
                ("app/services/providers_common.py", "prof.base_url"): 2,
                ("app/services/providers_part3.py", "self.profile.base_url"): 7,
                # #186: display_target resolves via the hosted pin/local refusal for display.
                # #186: display_target's refused fallback is keyless display data, never sent.
                ("app/services/jev_inspector.py", "prof.base_url"): 2,
                # #186: resolve_target uses jev_base_url; _post rechecks local egress.
                ("app/services/qe_jev.py", "prof.base_url"): 1,
                # #186: _endpoint strips userinfo for display/trace only, no request.
                # #186: _post refuses TypeSafe on a local target before constructing a client.
                # #186: _post local kwargs always run validate_local_engine_target.
                # #186: _post hosted kwargs use the target pinned by resolve_target.
                ("app/services/qe_jev.py", "judge.base_url"): 4,
                # #361: _ref_kind_base reads an engine profile's URL only to classify it as a
                # free self-hosted host for pricing (spend.kind_is_free); nothing is sent.
                ("app/services/spend.py", "prof.base_url"): 1,
            }
        )
        observed_non_request = Counter()

        def base_attributes(tree):
            return [
                node
                for node in ast.walk(tree)
                if isinstance(node, ast.Attribute) and node.attr == "base_url"
            ]

        for source in (
            "req = request; str(req.base_url)",
            "str(self.request.base_url)",
            "def emit(r: Request): return str(r.base_url)",
        ):
            equal(len(base_attributes(ast.parse(source))), 1)
        for p in (_ROOT / "app").rglob("*.py"):
            tree = ast.parse(p.read_text(encoding="utf-8"))
            rel = p.relative_to(_ROOT).as_posix()
            for node in ast.walk(tree):
                if isinstance(node, ast.ImportFrom) and node.module == "starlette._utils":
                    imports.append(rel)
                if isinstance(node, ast.Import):
                    imports.extend(rel for alias in node.names if alias.name == "starlette._utils")
            for node in base_attributes(tree):
                exemption = (rel, ast.unparse(node))
                if exemption in non_request:
                    observed_non_request[exemption] += 1
                else:
                    owners = [
                        n.name
                        for n in ast.walk(tree)
                        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
                        and n.lineno <= node.lineno <= n.end_lineno
                    ]
                    base_reads.append((rel, owners))
        equal(imports, ["app/web/route_path.py"])
        equal(observed_non_request, non_request)
        equal(base_reads, [("app/services/settings.py", ["external_base_url"])])
        for p in (_ROOT / "app/templates").rglob("*.html"):
            source = p.read_text(encoding="utf-8")
            if p.name == "setup_ai.html":
                # Provider configuration fields, never incoming request origins.
                equal(source.count("local.base_url"), 2)
                # #186: the added hosted-Jev fixed-URL label is escaped display data only.
                equal(source.count("e.base_url"), 2)
                source = source.replace("local.base_url", "").replace("e.base_url", "")
            assert not re.search(r"\.base_url\b", source), str(p)

    check("source ratchets", ratchets)
    assert not failures, "external origin checks failed"
    print("External origin OK")


# Captured before production edits; emitter ASTs matched main code tip 12201cb1.
PINNED = {
    "invitation": (
        "You've been invited to Localization OS as 'translator'.\n\n"
        "Set up your account here (expires in 7 days):\n"
        "http://attacker.example/auth/accept/fixture-invite\n"
    ),
    "scim_config": "http://attacker.example/scim/v2/ServiceProviderConfig",
    "scim_users": ["http://attacker.example/scim/v2/Users/1"],
    "saml_entity": "http://attacker.example/auth/saml/metadata",
    "saml_acs": "http://attacker.example/auth/saml/acs",
    "saml_prepare": {"https": "off", "http_host": "attacker.example", "server_port": "80"},
    "utcp": [
        "http://attacker.example/api/batches/{batch_id}",
        "http://attacker.example/api/capabilities/{slug}",
        "http://attacker.example/api/content-types",
        "http://attacker.example/api/context/resolve",
        "http://attacker.example/api/customers",
        "http://attacker.example/api/customers/{cid}/detail",
        "http://attacker.example/api/documents/{doc_id}/download",
        "http://attacker.example/api/documents/{doc_id}/paragraphs",
        "http://attacker.example/api/documents/{doc_id}/segments",
        "http://attacker.example/api/exports",
        "http://attacker.example/api/exports/{run_uuid}",
        "http://attacker.example/api/formats",
        "http://attacker.example/api/glossaries",
        "http://attacker.example/api/glossaries/{gid}",
        "http://attacker.example/api/glossaries/{gid}/candidates",
        "http://attacker.example/api/glossaries/{gid}/export",
        "http://attacker.example/api/images",
        "http://attacker.example/api/images/{doc_id}",
        "http://attacker.example/api/images/{doc_id}/export",
        "http://attacker.example/api/jobs",
        "http://attacker.example/api/jobs/{jid}",
        "http://attacker.example/api/keys/{key_id}/images",
        "http://attacker.example/api/keys/{key_id}/values",
        "http://attacker.example/api/langsets",
        "http://attacker.example/api/languages",
        "http://attacker.example/api/projects",
        "http://attacker.example/api/projects/{pid}",
        "http://attacker.example/api/projects/{pid}/changes",
        "http://attacker.example/api/projects/{pid}/documents",
        "http://attacker.example/api/projects/{pid}/export",
        "http://attacker.example/api/projects/{pid}/glossaries",
        "http://attacker.example/api/projects/{pid}/glossary",
        "http://attacker.example/api/projects/{pid}/glossary/export",
        "http://attacker.example/api/projects/{pid}/keys",
        "http://attacker.example/api/projects/{pid}/keys/export",
        "http://attacker.example/api/projects/{pid}/tm/export",
        "http://attacker.example/api/projects/{pid}/tm-sources",
        "http://attacker.example/api/prompt-templates",
        "http://attacker.example/api/reports/run",
        "http://attacker.example/api/runs/{run_id}",
        "http://attacker.example/api/style-guides",
        "http://attacker.example/api/style-guides/{gid}",
        "http://attacker.example/api/system-prompts",
        "http://attacker.example/api/templates",
        "http://attacker.example/api/templates/{tid}",
        "http://attacker.example/api/tm",
        "http://attacker.example/api/tm/{entry_id}",
        "http://attacker.example/api/tm/stats",
        "http://attacker.example/api/workflow-groups",
        "http://attacker.example/api/workflow-groups/{gid}",
        "http://attacker.example/api/workflows",
        "http://attacker.example/api/workflows/{wid}",
    ],
    "oidc": "http://attacker.example/auth/oidc/callback",
    "connector_oauth": "http://attacker.example/connectors/oauth/callback",
    "security_guidance": [
        "http://attacker.example/auth/oidc/callback",
        "http://attacker.example/connectors/oauth/callback",
    ],
    "help_guidance": ["http://attacker.example/auth/oidc/callback"],
}

if __name__ == "__main__":
    main()
