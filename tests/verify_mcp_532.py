"""MCP server fixes, batch remote-db (issue #532).

Offline: the MCP handlers run against an httpx.MockTransport fake of the install (no
network, no provider, no spend); the bulk-keys route runs on the real app via TestClient.

  1. connector_translate with no item_ids is refused at plan AND run time (it would
     translate the connection's whole scope); with ids it plans and runs as before.
  2. (removed: the paid meta-evaluation plan case)
  3. Bulk upsert_keys applies is_plural on an EXISTING key (the route used to report it
     `updated` and leave it singular).
  4. (removed: the paid advanced-tools ToolError case)
  5. A vendor plan reports "not visible" only for a 404; 401/503 are real failures.
  6. manage_install set_config forwards values as given (null/true/list are not stringified).
  7. The 401 text names the per-request key in HTTP mode, not LOC_STUDIO_TOKEN.

Final line: "MCP 532 OK".
"""

import asyncio
import json
import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(_ROOT)
sys.path.insert(0, _ROOT)
from tests._bootstrap import init_env

init_env("loctms_mcp532_")
sys.path.insert(0, os.path.join(_ROOT, "integrations", "mcp-server"))

import httpx
import server as mcp_server

mcp_server.TOKEN = "lsu_fixture_token"
mcp_server.BASE_URL = "http://fake.invalid"


def _fail(msg):
    print("FAIL:", msg)
    sys.exit(1)


class Fake:
    """Scripted install: handler(request) -> httpx.Response; records every request."""

    def __init__(self, handler):
        self.handler = handler
        self.requests = []

    def install(self):
        def _h(req):
            self.requests.append(req)
            return self.handler(req)

        transport = httpx.MockTransport(_h)
        mcp_server._client = lambda: httpx.AsyncClient(transport=transport, timeout=5)


def run(coro):
    return asyncio.run(coro)


def tool(name, **args):
    return run(mcp_server.call_tool_unvalidated(name, args))


def expect_tool_error(name, needle, **args):
    try:
        tool(name, **args)
    except mcp_server.ToolError as exc:
        if needle.lower() not in str(exc).lower():
            _fail(f"{name}: wrong error text: {exc}")
        return exc
    _fail(f"{name}: expected a ToolError, got success")


def j(body, status=200):
    return httpx.Response(status, json=body)


def test_connector_translate_requires_items():
    conn = {"id": 4, "kind": "jira", "name": "J", "status": "connected"}

    def handler(req):
        if req.url.path == "/api/connectors" and req.method == "GET":
            return j({"ok": True, "connections": [conn]})
        if req.url.path.endswith("/jira/translate"):
            return j({"ok": True, "message": "done"})
        return j({"ok": False, "error": "unexpected " + req.url.path}, 404)

    fake = Fake(handler)
    fake.install()
    expect_tool_error("connector_translate", "item_ids", connection_id=4)  # plan, no ids
    # #757: an id-less run cannot be planned, so it cannot be confirmed either (no plan_id)
    expect_tool_error("connector_translate", "plan_id", connection_id=4, confirm=True)
    posts = [r for r in fake.requests if r.method == "POST"]
    if posts:
        _fail(f"a no-ids call reached a write route: {[str(r.url) for r in posts]}")
    plan = tool("connector_translate", connection_id=4, item_ids=["LOC-1"])
    if "LOC-1" not in json.dumps(plan):
        _fail(f"a plan with ids lost them: {plan!r}")
    out = tool(
        "connector_translate",
        connection_id=4,
        item_ids=["LOC-1"],
        confirm=True,
        plan_id=json.loads(plan)["plan_id"],
    )
    sent = [json.loads(r.content) for r in fake.requests if r.method == "POST"]
    if sent != [{"issue_keys": ["LOC-1"]}]:
        _fail(f"unexpected write body {sent!r} (out {out!r})")


def test_upsert_existing_key_applies_is_plural():
    from fastapi.testclient import TestClient
    from sqlmodel import Session

    from app.db import engine, init_db
    from app.main import app
    from app.models import ProjectType
    from app.services import apikeys, security, settings
    from app.services import content as content_svc

    init_db()
    security.seed_initial()
    settings.set_many({"key_projects_enabled": "1"})
    admin = security.get_by_email("admin")
    _k, token = apikeys.create_key(admin.id, "mcp532", scopes="api docs keys")
    with Session(engine) as s:
        p = content_svc.project_create(
            s,
            "plural532",
            source_locale="en-US",
            target_locales="de-DE",
            project_type=ProjectType.KEY_BASED,
        )
        pid = p.id
    client = TestClient(app)
    hdr = {"X-Loc-Token": token}
    r = client.post(
        f"/api/projects/{pid}/keys", headers=hdr, json={"keys": [{"name": "app.items"}]}
    )
    if r.status_code != 200 or len(r.json().get("created", [])) != 1:
        _fail(f"seed key not created: {r.status_code} {r.text}")
    r = client.post(
        f"/api/projects/{pid}/keys",
        headers=hdr,
        json={"keys": [{"name": "app.items", "is_plural": True}]},
    )
    body = r.json()
    if len(body.get("updated", [])) != 1 or body.get("errors"):
        _fail(f"unexpected upsert answer {body!r}")
    from app.models import StringKey

    with Session(engine) as s:
        key = s.get(StringKey, body["updated"][0])
        if not key.is_plural:
            _fail("the key was reported updated but stayed singular")


def test_vendor_plan_distinguishes_404_from_failure():
    def make(status):
        def handler(req):
            return j({"ok": False, "error": "boom"}, status)

        return handler

    Fake(make(404)).install()
    plan = tool("vendor_approve_quote", did=9)
    if "not visible" not in json.dumps(plan):
        _fail(f"a 404 leg must read as not visible: {plan!r}")
    for status in (401, 503):
        Fake(make(status)).install()
        try:
            plan = tool("vendor_approve_quote", did=9)
        except mcp_server.ToolError:
            continue
        _fail(f"HTTP {status} was reported as a plan: {plan!r}")


def test_set_config_forwards_values_as_given():
    bodies = []

    def handler(req):
        if req.url.path == "/api/admin/config" and req.method == "POST":
            bodies.append(json.loads(req.content))
            return j({"ok": True, "changed": [], "settings": {}})
        return j({"ok": False, "error": "unexpected"}, 404)

    Fake(handler).install()
    tool(
        "manage_install",
        action="set_config",
        settings={"a": None, "b": True, "c": ["x", "y"], "d": 5},
    )
    sent = bodies[0]["settings"]
    if sent != {"a": None, "b": True, "c": ["x", "y"], "d": 5}:
        _fail(f"values were rewritten on the way out: {sent!r}")


def test_401_text_is_mode_aware():
    Fake(lambda req: j({"ok": False, "error": "no"}, 401)).install()
    exc = expect_tool_error("list_connectors", "401")
    if "LOC_STUDIO_TOKEN" not in str(exc):
        _fail(f"stdio 401 should still name LOC_STUDIO_TOKEN: {exc}")
    reset = mcp_server._REQUEST_TOKEN.set("lsu_http_caller")
    try:
        exc = expect_tool_error("list_connectors", "401")
    finally:
        mcp_server._REQUEST_TOKEN.reset(reset)
    if "LOC_STUDIO_TOKEN" in str(exc):
        _fail(f"HTTP-mode 401 must not point at the env token: {exc}")


if __name__ == "__main__":
    test_connector_translate_requires_items()
    test_vendor_plan_distinguishes_404_from_failure()
    test_set_config_forwards_values_as_given()
    test_401_text_is_mode_aware()
    test_upsert_existing_key_applies_is_plural()
    print("MCP 532 OK")
