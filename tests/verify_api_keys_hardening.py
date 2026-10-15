"""#263: shared API budgets and engine-route classification ratchet."""

import ast
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
os.chdir(ROOT)
sys.path.insert(0, str(ROOT))

from tests._bootstrap import init_env

init_env("loctms_api_keys_hardening_")

from tests.verify_ai_opt_out_coverage import _imports, _module_of, scan_source


def engine_graph(overrides):
    """Scope local import aliases to their function, including callback references.

    Reuse the engine boundary detector, but do not merge all local imports into a
    module: store_common's many `_m` delegators refer to different connectors.
    """
    sites, owners, refs, imports, local_imports = {}, {}, [], {}, {}
    for path in (ROOT / "app").rglob("*.py"):
        rel = path.relative_to(ROOT).as_posix()
        if rel == "app/services/ai_optout.py":
            continue
        src = (overrides or {}).get(rel, path.read_text(encoding="utf-8"))
        found, _, owners[rel] = scan_source(rel, src, refs)
        sites.update(found)
        tree = ast.parse(src)
        module_tree = ast.Module(
            body=[
                n
                for n in tree.body
                if not isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
            ],
            type_ignores=[],
        )
        imports[rel] = _imports(module_tree, rel)
        for node in tree.body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                local_imports[f"{rel}::{node.name}"] = _imports(node, rel)
                # Scheduler methods dispatch workers outside the static call graph.
                for call in ast.walk(node):
                    if (
                        isinstance(call, ast.Call)
                        and isinstance(call.func, ast.Attribute)
                        and call.func.attr == "drain_once"
                    ):
                        sites[f"{rel}::{node.name}::drain_once"] = call.lineno
    modules = {_module_of(rel): rel for rel in owners}

    def origin(module, name, seen=frozenset()):
        rel = modules.get(module)
        if rel is None or (module, name) in seen:
            return None
        if name in owners[rel]:
            return f"{rel}::{owners[rel][name]}"
        imported = imports[rel][0].get(name)
        if imported:
            return origin(*imported, seen | {(module, name)})
        return None

    callers = {}
    for rel, owner, receiver, name in refs:
        global_names, global_modules = imports[rel]
        local_names, local_modules = local_imports.get(owner, ({}, {}))
        names = global_names | local_names
        mods = global_modules | local_modules
        target = None
        if receiver is None:
            target = origin(_module_of(rel), name)
            if target is None and name in names:
                target = origin(*names[name])
        elif receiver in mods:
            target = origin(mods[receiver], name)
        if target and target != owner:
            callers.setdefault(target, set()).add(owner)
    return sites, owners, callers


def live_routes(routes, prefix=""):
    """Flatten FastAPI included routers and mounted apps with their effective prefix."""
    for route in routes:
        if hasattr(route, "original_router"):
            yield from live_routes(
                route.original_router.routes, prefix + (route.include_context.prefix or "")
            )
        elif hasattr(route, "routes") and not hasattr(route, "methods"):
            yield from live_routes(route.routes, prefix + route.path)
        else:
            path = prefix + getattr(route, "path", "")
            if path.startswith("/api/"):
                for method in getattr(route, "methods", ()) or ():
                    yield method, path, route.endpoint


def engine_routes(overrides=None):
    """Follow engine calls/references backwards through imports, helpers and workers.

    Reuse the engine-boundary inventory, not the spend classification. Include
    ingestion and queued work whose method/registry dispatch is not static.
    """
    from app.main import app

    sites, owners, callers = engine_graph(overrides)
    reached = set()
    pending = []
    for site in sites:
        rel, qual, _ = site.split("::")
        pending.append(f"{rel}::{owners[rel].get(qual, qual)}")
    pending += [
        "app/services/ingest.py::ingest_document",
        "app/services/jobs.py::submit",
        "app/services/jobs.py::submit_unique",
        "app/services/jobs.py::run_sync",
        "app/services/batch_runs.py::start_batch",
        "app/services/batch_runs.py::rerun_batch",
        "app/services/orchestrator.py::queue_runs",
        "app/services/connectors/flow.py::run_translate_flow",
    ]
    while pending:
        fn = pending.pop()
        if fn in reached:
            continue
        reached.add(fn)
        pending.extend(callers.get(fn, ()))
    routes = set()
    for method, path, endpoint in live_routes(app.routes):
        rel = endpoint.__module__.replace(".", "/") + ".py"
        if f"{rel}::{endpoint.__qualname__}" in reached:
            routes.add((method, path))
    return routes


def test_budgets():
    from fastapi.testclient import TestClient

    from app.db import init_db
    from app.main import app
    from app.models import Role
    from app.services import apikeys, ratelimit, security, settings

    init_db()
    user = security.create_user("limits@example.test", "Limits", Role.ADMIN, FIXTURE_PASSWORD)
    _, raw = apikeys.create_key(user.id, "limits", scopes="api docs admin")
    headers = {"X-Loc-Token": raw}
    c = TestClient(app)
    failures = []
    for setting, method, path in (
        ("api_read_rate_limit_per_minute", "GET", "/api/projects"),
        ("api_rate_limit_per_minute", "POST", "/api/jobs/run"),
        ("api_rate_limit_per_minute", "POST", "/api/documents"),
    ):
        settings.set_many({setting: "2"})
        ratelimit.reset()
        responses = [c.request(method, path, headers=headers) for _ in range(3)]
        if [r.status_code == 429 for r in responses] != [False, False, True]:
            failures.append((path, [r.status_code for r in responses]))
        else:
            r = responses[-1]
            assert r.json()["ok"] is False
            assert r.json()["error"] == "Rate limit exceeded - slow down and retry."
            assert int(r.headers["Retry-After"]) == r.json()["retry_after"] > 0
        settings.set_many({setting: "0"})
        assert all(c.request(method, path, headers=headers).status_code != 429 for _ in range(4))
    assert not failures, failures

    settings.set_many({"api_rate_limit_per_minute": "1", "api_read_rate_limit_per_minute": "1"})
    ratelimit.reset()
    assert c.get("/api/projects", headers=headers).status_code == 200
    assert c.post("/api/jobs/run", headers=headers).status_code == 400
    assert c.get("/api/projects", headers=headers).status_code == 429
    assert c.post("/api/jobs/run", headers=headers).status_code == 429
    # Other identities have their own windows. Multiple keys for one user share a window.
    _, second = apikeys.create_key(user.id, "second", scopes="api docs")
    assert c.get("/api/projects", headers={"X-Loc-Token": second}).status_code == 429
    other = security.create_user("other@example.test", "Other", Role.ADMIN, FIXTURE_PASSWORD)
    _, other_raw = apikeys.create_key(other.id, "other", scopes="api docs")
    assert c.get("/api/projects", headers={"X-Loc-Token": other_raw}).status_code == 200
    assert c.get("/api/projects").status_code == 401
    assert c.get("/health", headers=headers).status_code == 200
    # Cheap writes share the read window; an API metadata route also counts.
    assert c.post("/api/projects", headers=headers).status_code == 429
    assert c.get("/api/health", headers=headers).status_code == 429
    assert c.get("/security", headers=headers).status_code == 200
    settings.set_many({"api_rate_limit_per_minute": "0", "api_read_rate_limit_per_minute": "0"})
    ratelimit.reset()
    settings.set_many({"remote_admin_enabled": "1"})
    payload = c.get("/api/admin/config", headers=headers).json()
    assert "api_read_rate_limit_per_minute" in payload["settings"], payload
    r = c.post(
        "/api/admin/config",
        headers=headers,
        json={
            "settings": {"api_read_rate_limit_per_minute": "600"},
            "confirm": True,
        },
    )
    assert r.status_code == 200, r.text
    for invalid in ("-1", "abc", "1.5"):
        r = c.post(
            "/api/admin/config",
            headers=headers,
            json={
                "settings": {"api_read_rate_limit_per_minute": invalid},
                "confirm": True,
            },
        )
        assert r.status_code == 400, r.text
    assert 'name="api_read_rate_limit_per_minute"' in c.get("/security").text
    print("read, previously unthrottled spend, exactly once, disabled, isolation, settings OK")


def assert_spend_routes(found):
    from app.services.ratelimit import api_budget

    # Export archive jobs never dispatch an engine job kind.
    cheap_jobs = {("POST", "/api/exports")}
    missing = {(m, p) for m, p in found - cheap_jobs if api_budget(m, p) != "spend"}
    assert not missing, f"Unclassified engine routes: {sorted(missing)}"


def test_ratchet():
    from app.services.ratelimit import SPEND_ROUTES, api_budget

    found = engine_routes()
    assert_spend_routes(found)
    for method, path in SPEND_ROUTES:
        concrete = path
        while "{" in concrete:
            start, end = concrete.index("{"), concrete.index("}")
            concrete = concrete[:start] + "123" + concrete[end + 1 :]
        assert api_budget(method, concrete) == "spend", path
        assert api_budget(method, concrete + "/") == "spend", path
        assert api_budget("GET", concrete) == "read", path
    # Independent engine traversal must discover all factory callback routes,
    # not merely rely on their structural /translate suffix.
    from app.main import app

    # Independent structural inventory also covers instance-method engine dispatch.
    actions = frozenset(
        [
            "translate",
            "translate-pending",
            "run",
            "run-batch",
            "rerun",
            "retry-failed",
            "resume",
            "pull",
            "push",
            "sync",
            "extract",
            "distill",
            "ab",
            "test",
            "jev-try",
            "backfill",
            "tick",
            "poll",
            "start",
        ]
    )
    for method, path, endpoint in live_routes(app.routes):
        if method == "POST" and path.rstrip("/").rsplit("/", 1)[-1] in actions:
            assert api_budget(method, path) == "spend", path
        if method == "POST" and path.endswith("/translate"):
            assert (method, path) in found, (path, endpoint)
    print(f"engine-route ratchet and mutation OK ({len(SPEND_ROUTES)} spend routes)")


def test_live_ratchet():
    from fastapi import APIRouter

    from app.main import app
    from app.services.ratelimit import api_budget
    from app.web import api

    rel = "app/web/api.py"
    source = """
probe_router = APIRouter(prefix="/api/gate")

@probe_router.post("/argument")
async def gate_argument(request):
    return await _article_translate(request, 1, conn_store.shopify_translate)

@probe_router.post("/alias")
def gate_alias():
    from app.services.connectors import store_common as alias
    return alias.notion_translate(1)

@probe_router.post("/queued")
def gate_queued():
    from app.services import orchestrator
    return orchestrator.queue_runs(None, [])

@probe_router.post("/keys")
def gate_keys():
    from app.services import keys_hub2 as keys_svc
    return keys_svc.launch_translation(None, 1, 1, "de")

@probe_router.post("/direct")
def gate_direct():
    return _gate_helper()

def _gate_helper():
    from app.kernel.providers import run_completion
    return run_completion()

@probe_router.post("/future/translate")
async def gate_future(request):
    return await _article_translate(request, 1, conn_store.shopify_translate)
"""
    namespace = {**vars(api), "APIRouter": APIRouter}
    exec(compile(source, str(ROOT / rel), "exec"), namespace)
    original = list(app.router.routes)
    try:
        app.include_router(namespace["probe_router"], prefix="/api/nested")
        # Use an API prefix at inclusion too, exercising lazy included routers.
        app.include_router(namespace["probe_router"])
        found = engine_routes({rel: (ROOT / rel).read_text(encoding="utf-8") + source})
        expected = {
            ("POST", f"/api/gate/{name}")
            for name in ("argument", "alias", "queued", "keys", "direct")
        }
        assert expected <= found, ("ratchet missed live engine routes", expected - found)
        assert {("POST", "/api/nested" + p) for _, p in expected} <= found
        missing = {(m, p) for m, p in found if api_budget(m, p) == "read"}
        assert expected <= missing, ("mutation must fail classification ratchet", missing)
        try:
            assert_spend_routes(found)
        except AssertionError as exc:
            assert all(path in str(exc) for _, path in expected), str(exc)
        else:
            raise AssertionError("ratchet accepted temporary unclassified engine routes")
        assert api_budget("POST", "/api/gate/future/translate") == "spend"
    finally:
        app.router.routes[:] = original
    print("live route mutations: arguments, aliases, queue, keys, relative paths OK")


def test_gate_regressions():
    from unittest.mock import patch

    from fastapi.testclient import TestClient

    from app.db import init_db
    from app.main import app
    from app.models import Role
    from app.services import apikeys, ratelimit, security, settings

    init_db()
    user = security.create_user("gate@example.test", "Gate", Role.ADMIN, FIXTURE_PASSWORD)
    _, raw = apikeys.create_key(user.id, "gate", scopes="api docs admin")
    c = TestClient(app, headers={"X-Loc-Token": raw})
    failures = []
    connectors = [
        "akeneo",
        "bitbucket",
        "contentful",
        "document360",
        "freshdesk",
        "github",
        "gitlab",
        "helpscout",
        "hubspot",
        "intercom",
        "notion",
        "salesforce",
        "shopify",
        "strapi",
        "zoho",
    ]
    paths = [f"/api/connectors/999999/{name}/translate" for name in connectors]
    paths.append("/api/projects/999999/keys/translate")
    settings.set_many({"api_rate_limit_per_minute": "1", "api_read_rate_limit_per_minute": "0"})
    for path in [*paths, "/api/translate/text/"]:
        if path.endswith("/"):
            settings.set_many({"api_read_rate_limit_per_minute": "1"})
        ratelimit.reset()
        responses = [c.post(path) for _ in range(2)]
        statuses = [response.status_code for response in responses]
        if statuses[0] == 429 or statuses[1] != 429:
            failures.append(("F2" if path.endswith("/") else "F1", path, statuses))
        # A redirect must not consume a read hit either.
        if path.endswith("/") and any(":read:" in key for key in ratelimit._hits):
            failures.append(("F2", "redirect charged read budget"))
        if path.endswith("/") and [r.status_code for r in responses[0].history] != [307]:
            failures.append(("F2", "expected followed slash redirect"))
    if ratelimit.api_budget("POST", "/api/connectors/1/future/translate") != "spend":
        failures.append(("F1", "future connector classified read"))
    for key in ("api_rate_limit_per_minute", "api_read_rate_limit_per_minute"):
        for value in ("100001", "9" * 300):
            try:
                settings.validate_values({key: value})
            except settings.InvalidSettingValue as exc:
                if "100000" not in str(exc):
                    failures.append(("F3", key, "missing clear maximum"))
            else:
                failures.append(("F3", key, "accepted excessive limit"))
        for value in ("0", "1", "100000"):
            settings.validate_values({key: value})
        method, path = ("GET", "/api/projects") if "read" in key else ("POST", "/api/jobs/run")
        for corrupt in ("broken", "", "-1", "1.5", "100001", None):
            ratelimit.reset()
            with patch.object(settings, "get", return_value=corrupt):
                results = [
                    ratelimit.check_api("gate", method, path)[0]
                    for _ in range(int(settings.DEFAULTS[key]) + 1)
                ]
            if not all(results[:-1]) or results[-1]:
                failures.append(("F4", key, corrupt, "did not enforce default"))
    for explicit in (False, True):
        ratelimit.reset()
        with patch.object(settings, "get", return_value="broken"):
            results = [
                ratelimit.check("legacy", **({"limit": "broken"} if explicit else {}))[0]
                for _ in range(121)
            ]
        if results != [True] * 120 + [False]:
            failures.append(("F4", "check fallback", explicit))
    settings.set_many({"api_rate_limit_per_minute": "0", "api_read_rate_limit_per_minute": "0"})
    ratelimit.reset()
    assert not failures, failures
    print("F1-F4 regression checks OK")


def test_surfaces():
    from app.services import settings

    key = "api_read_rate_limit_per_minute"
    assert settings.DEFAULTS[key] == "600"
    assert key in settings.SENSITIVE_KEYS
    for path in (
        "app/cli/cmd_config.py",
        "app/templates/help.html",
        "app/capabilities/llm.md",
        "app/templates/security.html",
        "app/web/admin.py",
        "docs/CLI.md",
    ):
        assert key in (ROOT / path).read_text(encoding="utf-8"), path
    print("settings surfaces OK")


FIXTURE_PASSWORD = "fixture-only-pass-263"  # pregate: allow-secret offline test fixture


if __name__ == "__main__":
    errors = []
    for regression in (test_gate_regressions, test_live_ratchet):
        try:
            regression()
        except AssertionError as exc:
            errors.append((regression.__name__, str(exc)))
    assert not errors, errors
    test_budgets()
    test_ratchet()
    test_surfaces()
    print("API KEYS HARDENING OK")
