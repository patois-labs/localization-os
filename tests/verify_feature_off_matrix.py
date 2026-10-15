"""Registry-driven single-off, collateral, all-off and restoration matrix.

Reuses verify_features' app, environment, route and CLI helpers. Parent-first
refusal text (#113/#114) remains owned by its check_refusal_text_truth.
"""

import asyncio
import json
import logging
import sys
import time
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tests import verify_features as base

_ENV_READY = base.F  # Import the shared environment before importing the app.

import httpx
from sqlmodel import Session

from app.db import engine
from app.models import Customer, Project, Role, StringKey
from app.services import apikeys, features_admin, ratelimit, security
from tests._csrf import csrf_for

F = base.F
app = base.app
sys.path.insert(0, str(Path(base._REGISTRY_PATH).parent))
import server as mcp_server

_MCP_REPLIES = []


def flip(feature, enabled):
    features_admin.set_feature_enabled(feature.slug, enabled, actor="matrix")
    base.settings.invalidate_cache()
    assert feature.is_enabled() == enabled


def route_cells():
    """Every removed route/method is covered off; cheap GET representatives on."""
    from app.web._routes import walk_routes

    covered = {}
    representatives = {"/settings", "/settings/enterprise"}
    for feature in base._INSTALLED:
        urls = set(base._feature_routes(feature))
        routes = [
            route
            for route in walk_routes(app)
            if getattr(route, "methods", None) and base._concretize(route.path) in urls
        ]
        if feature.semantics == "removed":
            covered[feature.slug] = {
                (method, base._concretize(route.path))
                for route in routes
                for method in route.methods
            }
            assert covered[feature.slug], feature.slug
        for pattern in feature.api + feature.web:
            candidates = [
                route.path
                for route in routes
                if "GET" in route.methods and F._matches(pattern, route.path)
            ]
            # A POST-only prefix is still probed by GET (405 at baseline).
            if not candidates:
                candidates = [route.path for route in routes if F._matches(pattern, route.path)]
            assert candidates, (feature.slug, pattern)
            chosen = min(candidates, key=lambda p: (p.count("{"), p.count("/"), p))
            representatives.add(base._concretize(chosen))
        # Web declarations name pages as well as prefixes. Exercise each literal
        # page even if the prefix's shortest real route is a child.
        representatives.update(base._concretize(p).replace("*", "1") for p in feature.web)
    return covered, sorted(representatives)


def setup():
    base.init_db()
    for feature in base._INSTALLED:
        assert feature.is_enabled() == feature.default_on(), feature.slug
    base._set("default_provider", "mock")
    base._set("assist_provider", "mock")
    base._set("local_base_url", "")
    base._set("security_auth_required", "1")
    base._set("remote_admin_enabled", "1")
    login_pw = "matrix-test-password"  # pregate: allow-secret test login
    user = security.create_user("matrix@x.com", "Matrix", Role.ADMIN, login_pw)
    _, key_plain = apikeys.create_key(user.id, "matrix", scopes="api,admin,keys,docs")
    with Session(engine) as session:
        customer = Customer(id=1, name="Matrix")
        session.add(customer)
        session.commit()
        project = Project(id=1, name="Matrix", customer_id=1, project_type="key_based")
        session.add(project)
        session.commit()
        session.add(StringKey(id=1, project_id=1, name="matrix"))
        session.commit()
    client = base.TestClient(app)
    csrf_for(client)
    login = client.post(
        "/auth/login",
        data={"email": user.email, "password": login_pw, "next": "/"},
        follow_redirects=False,
    )
    assert login.status_code == 303, login.text
    client.headers["X-Loc-Token"] = key_plain
    home = client.get("/")
    assert home.status_code == 200 and "/auth/logout" in home.text, "session must be logged in"
    return client, key_plain


def cli_cell(group):
    rc, output = base._run_cli(base._CLI_PROBE[group])
    return rc, next(iter(output.splitlines()), "")


def mcp_probes():
    probes = {}
    for feature in base._INSTALLED:
        if feature.semantics != "removed":
            continue
        tools = [t for t in mcp_server.registry.TOOLS if t.feature_gate == feature.mcp_gate]
        if not tools:
            continue  # Registry deliberately has no tool for several removed features.
        reads = [t for t in tools if all(method == "GET" for method, _ in t.routes)]
        if feature.slug == "vendors":
            # Confirmed dispatch against a deliberately absent leg cannot spend.
            probes[feature.slug] = ("vendor_approve_quote", {"did": 999999, "confirm": True})
            continue
        if feature.slug == "routing":
            probes[feature.slug] = (
                "manage_admin_ops",
                {"action": "toggle_rule", "rule_id": 999999},
            )
            continue
        assert reads, f"Add a safe MCP probe for {feature.slug}"
        tool = min(reads, key=lambda t: (len(t.input_schema.get("required", [])), t.name))
        args = {}
        if "project_id" in tool.input_schema.get("properties", {}):
            args["project_id"] = 1
        for name in tool.input_schema.get("required", []):
            prop = tool.input_schema["properties"][name]
            args[name] = prop.get("default", (prop.get("enum") or [1])[0])
        probes[feature.slug] = (tool.name, args)
    return probes


def mcp_cell(probe):
    _MCP_REPLIES.clear()

    async def call():
        name, args = probe
        try:
            if args.get("confirm") is True:  # #757: a confirmed spend names its plan
                from tests._mcp_plan import aconfirmed

                return "ok", await aconfirmed(mcp_server, name, args)
            return "ok", await mcp_server.call_tool_unvalidated(name, args)
        except mcp_server.ToolError as exc:
            return "error", str(exc)

    result = asyncio.run(call())
    assert _MCP_REPLIES, (probe, "MCP probe must reach the app", result)
    return (*result, tuple(_MCP_REPLIES))


def snapshot(client, urls, probes):
    # Each matrix pass starts with empty rate-limit windows: one pass's hundreds of calls must
    # not push the next pass over the per-identity limit (HTTP 429 carry-over, #626).
    ratelimit.reset()
    cells = {}
    for url in urls:
        response = client.get(
            url,
            params={"customer_id": 1, "project_id": 1} if url.startswith("/api/") else None,
            follow_redirects=False,
        )
        assert response.status_code < 500, (url, response.status_code, response.text[:150])
        # API data is compared, HTML compares status; nav is measured separately
        # because every page's shared navigation legitimately changes on a flip.
        value = response.json() if url.startswith("/api/") else None
        cells["route", url] = response.status_code, value
    home = client.get("/")
    assert home.status_code == 200
    for feature in base._INSTALLED:
        for href in feature.nav_hrefs:
            cells["nav", feature.slug, href] = f'href="{href}"' in home.text
        for card in set(feature.cards) | {feature.slug}:
            cells["help", card] = client.get(f"/help/{card}").status_code
        if feature.semantics == "removed":
            for group in feature.cli_groups:
                cells["cli", feature.slug, group] = cli_cell(group)
    for slug, probe in probes.items():
        cells["mcp", slug] = mcp_cell(probe)
    return cells


def assert_matrix(client, urls, probes, baseline, covered, off):
    # Expected ownership comes from the route inventory and registry, never the
    # production predicate under test: an over-broad path_is_off must be collateral.
    removed_urls = {url for slug in off for _, url in covered.get(slug, ())}
    removed_cards = {
        card for feature in base._INSTALLED if feature.slug in off for card in feature.cards
    }
    current = snapshot(client, urls, probes)
    for key, value in current.items():
        kind = key[0]
        expected = baseline[key]
        if kind == "route" and key[1] in removed_urls:
            expected = (404, {"ok": False, "error": F.REMOVED_REFUSAL})
            if not key[1].startswith("/api/"):
                expected = (404, None)
        elif kind == "help" and key[1] in removed_cards:
            expected = 404
        elif kind == "nav" and key[1] in off:
            if F.feature_by_slug(key[1]).nav_gated:
                expected = False
        elif kind == "cli" and key[1] in off:
            feature = F.feature_by_slug(key[1])
            assert value[0] == feature.cli_exit_code and feature.cli_refusal in value[1], (
                key,
                value,
                feature.cli_refusal,
            )
            continue
        elif kind == "mcp" and (
            key[1] in off
            or any(
                base._concretize(path) in removed_urls
                for _, path in mcp_server.registry.tool_by_name(probes[key[1]][0]).routes
            )
        ):
            assert value[0] == "error" and value[1].startswith(F.REMOVED_REFUSAL), (key, value)
            assert any(
                status == 404 and json.loads(body) == {"ok": False, "error": F.REMOVED_REFUSAL}
                for status, body in value[2]
            ), (key, value)
            continue
        assert value == expected, f"off={sorted(off)} {key}: {value!r} != {expected!r}"
    # All methods on every live claimed route, including routes added tomorrow.
    ratelimit.reset()
    for slug in off:
        for method, url in sorted(covered.get(slug, ())):
            response = client.request(method, url, follow_redirects=False)
            assert response.status_code == 404, (slug, method, url, response.status_code)
            if url.startswith("/api/") and method != "HEAD":
                assert response.json() == {"ok": False, "error": F.REMOVED_REFUSAL}, (
                    slug,
                    method,
                    url,
                    response.text,
                )


def assert_write_refused(client, feature):
    writes = {
        "data_export": (
            "/api/exports",
            409,
            "data export is disabled (setting data_export_enabled=0).",
            {},
        ),
        "custom_fields": (
            "/api/custom-fields",
            400,
            "custom project fields are switched off (setting custom_fields_enabled=0).",
            {"key": "matrix_field", "label": "Matrix"},
        ),
    }
    assert {f.slug for f in base._INSTALLED if f.semantics == "writes_off"} == set(writes)
    path, status, message, body = writes[feature.slug]
    response = client.post(path, json=body)
    assert response.status_code == status and response.json() == {"ok": False, "error": message}, (
        feature.slug,
        response.status_code,
        response.text,
    )


def main():
    logging.getLogger().setLevel(logging.WARNING)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    client, key_plain = setup()
    # The 180 s budget covers the matrix work only: the clock starts after the app import and
    # first-run setup, so a slow or loaded box cannot burn it before the first cell runs.
    started = time.monotonic()
    covered, urls = route_cells()
    probes = mcp_probes()

    def relay(request):
        response = client.request(
            request.method,
            str(request.url),
            headers=dict(request.headers),
            content=request.content,
            follow_redirects=False,
        )
        _MCP_REPLIES.append((response.status_code, response.text))
        return httpx.Response(
            response.status_code, content=response.content, headers=response.headers
        )

    transport = httpx.MockTransport(relay)
    with (
        client,
        patch.object(mcp_server, "TOKEN", key_plain),
        patch.object(mcp_server, "_client", lambda: httpx.AsyncClient(transport=transport)),
        patch.object(
            httpx.HTTPTransport, "handle_request", side_effect=AssertionError("network forbidden")
        ),
        patch.object(
            httpx.AsyncHTTPTransport,
            "handle_async_request",
            side_effect=AssertionError("network forbidden"),
        ),
    ):
        baseline = snapshot(client, urls, probes)
        for feature in base._INSTALLED:
            if feature.semantics == "writes_off":
                for pattern in feature.api:
                    assert any(
                        baseline["route", url][0] == 200 for url in urls if F._matches(pattern, url)
                    ), (feature.slug, pattern, "reads must succeed at baseline")
        for feature in base._INSTALLED:
            if feature.semantics == "opt_in":
                door = features_admin.DOOR_MOVED.get(feature.slug, "/settings")
                assert client.get(door).status_code == 200, (feature.slug, door)
                rows = client.get("/api/admin/features").json()
                assert feature.slug in str(rows), (feature.slug, rows)
                print(f"  {feature.slug}: opt-in controls reachable OK", flush=True)
                continue
            if feature.semantics == "removed":
                for href in feature.nav_hrefs:
                    assert baseline["nav", feature.slug, href], (feature.slug, href, "vacuous nav")
            flip(feature, False)
            try:
                assert_matrix(client, urls, probes, baseline, covered, {feature.slug})
                if feature.semantics == "writes_off":
                    assert_write_refused(client, feature)
            finally:
                flip(feature, True)
            assert_matrix(client, urls, probes, baseline, covered, set())
            print(f"  {feature.slug}: off, collateral, restore OK", flush=True)
        default_on = [f for f in base._INSTALLED if f.default_on()]
        try:
            for feature in default_on:
                flip(feature, False)
            assert_matrix(client, urls, probes, baseline, covered, {f.slug for f in default_on})
            assert all(not f.is_enabled() for f in base._INSTALLED)
            for feature in default_on:
                if feature.semantics == "writes_off":
                    assert_write_refused(client, feature)
        finally:
            for feature in reversed(default_on):
                flip(feature, True)
        assert_matrix(client, urls, probes, baseline, covered, set())
    elapsed = time.monotonic() - started
    assert elapsed < 180, f"matrix exceeded 3 minute budget: {elapsed:.1f}s"
    print(f"FEATURE OFF MATRIX ({elapsed:.1f}s) OK")


if __name__ == "__main__":
    main()
