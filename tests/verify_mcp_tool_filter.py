"""verify_mcp_tool_filter - issue #751: tools/list follows the key's scopes and toolsets.

Review finding M3 (MCP interop review): all 89 tools (about 27 K tokens of
definitions) were listed to every client whatever its key could do, `whoami` could not
say what the key may do, and there was no way to load a named subset. This suite pins:

  1. GET /api/account carries the CALLING key's own `scopes` (sorted, `api` always in it),
     and nothing about any other key.
  2. registry.toolsets(): one toolset per family plus `connectors` and `admin`; every tool
     is in at least one; `all` (or empty) means every tool; an unknown name is refused.
  3. tools/list over the real app hides a tool whose grant the key lacks:
       * api+docs key        -> no keys-scope and no admin-scope tools
       * api+docs+keys+admin -> all tools
       * api+keys key        -> /api/account answers 403 (no docs), so docs-scope
                                tools are hidden; admin tools stay hidden because the
                                grant cannot be confirmed; keys + api tools stay
       * no token / 401 / unreachable -> FAIL NARROW: only the `api` tools + whoami
                                (never everything; the failure is cached briefly)
       * LOC_STUDIO_MCP_SCOPE_FILTER=off -> unfiltered (admin / debugging)
     and keeps registry order.
  4. Toolsets: LOC_STUDIO_MCP_TOOLSETS (stdio + HTTP default) narrows the list and
     refuses a call to a tool outside it; in HTTP mode an `X-Loc-Toolsets` header or a
     `?toolsets=` query narrows further but never widens the operator's set.
  5. whoami reports the key's scopes and the active toolsets.
  6. The scope lookup is cached per key, so a second tools/list costs no /api call.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import socket
import sys
import threading
import time

logging.getLogger("uvicorn").setLevel(logging.WARNING)
logging.getLogger("uvicorn.access").setLevel(logging.WARNING)
logging.getLogger("mcp").setLevel(logging.WARNING)

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(_ROOT)
sys.path.insert(0, _ROOT)
from tests._bootstrap import init_env

init_env("loctms_mcpfilter_")

_MCP_DIR = os.path.join(_ROOT, "integrations", "mcp-server")
sys.path.insert(0, _MCP_DIR)

import httpx
import registry
import server as mcp_server
import uvicorn
from fastapi.testclient import TestClient

from app.db import init_db
from app.main import app
from app.services import apikeys, security, settings


def _free_port() -> int:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


class _UvicornThread:
    def __init__(self, port: int):
        cfg = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning", loop="asyncio")
        self.server = uvicorn.Server(cfg)
        self.thread = threading.Thread(target=self.server.run, daemon=True)

    def start(self, timeout: float = 15.0) -> None:
        self.thread.start()
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self.server.started:
                return
            time.sleep(0.05)
        raise AssertionError("uvicorn did not start in time")

    def stop(self) -> None:
        self.server.should_exit = True
        self.thread.join(timeout=10)


class _FakeHeaders:
    def __init__(self, mapping):
        self._d = {str(k).lower(): v for k, v in (mapping or {}).items()}

    def get(self, key, default=None):
        return self._d.get(str(key).lower(), default)


class _FakeHttpRequest:
    """The read surface the server touches on a streamable-http request."""

    def __init__(self, headers, query=None):
        self.headers = _FakeHeaders(headers)
        self.query_params = dict(query or {})


@contextlib.contextmanager
def _http_request(headers, query=None):
    token = mcp_server._MCP_REQUEST.set(_FakeHttpRequest(headers, query))
    try:
        yield
    finally:
        mcp_server._MCP_REQUEST.reset(token)


@contextlib.contextmanager
def _module(**values):
    saved = {k: getattr(mcp_server, k) for k in values}
    for k, v in values.items():
        setattr(mcp_server, k, v)
    mcp_server._SCOPE_CACHE.clear()
    try:
        yield
    finally:
        for k, v in saved.items():
            setattr(mcp_server, k, v)
        mcp_server._SCOPE_CACHE.clear()


def _listed(token=None, **module):
    values = {"TOKEN": token} if token is not None else {}
    values.update(module)
    with _module(**values):
        return [t.name for t in asyncio.run(mcp_server.mcp.list_tools())]


def _names(pred):
    return [t.name for t in registry.TOOLS if pred(t)]


ALL = [t.name for t in registry.TOOLS]


# --- 1. /api/account carries the calling key's own scopes -------------------
def check_account_scopes(docs_key, full_key, keys_key) -> None:
    c = TestClient(app)
    body = c.get("/api/account", headers={"X-Loc-Token": docs_key}).json()
    assert body.get("scopes") == ["api", "docs"], body
    body = c.get("/api/account", headers={"X-Loc-Token": full_key}).json()
    assert body.get("scopes") == ["admin", "api", "docs", "keys"], body
    # Own key only: nothing that names, counts or lists any other credential.
    for k in ("keys", "api_keys", "prefix", "key_id"):
        assert k not in body, (k, body)
    # Auth required; a key without `docs` is refused (the filter's 403 branch).
    assert c.get("/api/account").status_code == 401
    assert c.get("/api/account", headers={"X-Loc-Token": keys_key}).status_code == 403
    print("  /api/account: scopes = the calling key's own sorted grants")


# --- 2. registry toolsets -----------------------------------------------------
def check_registry_toolsets() -> None:
    ts = registry.toolsets()
    for fam in registry.FAMILIES:
        assert ts[fam] == tuple(_names(lambda t, f=fam: t.family == f)), fam
    assert set(ts["connectors"]) == {
        "list_connectors",
        "connector_status",
        "connector_pull",
        "connector_translate",
        "connector_push",
        "connector_sync",
        "connector_retry",
    }, ts["connectors"]
    assert set(_names(lambda t: t.scope == "admin")) <= set(ts["admin"]), ts["admin"]
    covered = {n for names in ts.values() for n in names}
    assert covered == set(ALL), set(ALL) - covered
    assert registry.resolve_toolsets("") is None
    assert registry.resolve_toolsets("all") is None
    assert registry.resolve_toolsets(" orientation, connectors ") == frozenset(
        ts["orientation"] + ts["connectors"]
    )
    try:
        registry.resolve_toolsets("orientation,nope")
    except ValueError as exc:
        assert "nope" in str(exc) and "connectors" in str(exc), exc
    else:
        raise AssertionError("an unknown toolset must be refused")
    print("  registry: toolsets per family + connectors + admin; unknown refused")


# --- 3. scope filtering -------------------------------------------------------
def check_scope_filter(docs_key, full_key, keys_key) -> None:
    got = _listed(docs_key)
    assert got == _names(lambda t: t.scope in ("api", "docs")), sorted(set(ALL) - set(got))
    assert "list_keys" not in got and "manage_users" not in got
    assert len(got) < len(ALL)

    assert _listed(full_key) == ALL

    got = _listed(keys_key)
    assert got == _names(lambda t: t.scope in ("api", "keys")), got
    assert "whoami" not in got and "manage_install" not in got

    # Gate (#751): a failed lookup never lists everything. No token, a rejected token
    # and an unreachable install each list only the api tools plus whoami.
    narrow = _names(lambda t: t.scope == "api" or t.name == "whoami")
    assert _listed("") == narrow, _listed("")
    assert _listed("ls" + "u_not_a_real_key") == narrow, "401: fail narrow"
    saved_base = mcp_server.BASE_URL
    mcp_server.BASE_URL = "http://127.0.0.1:1"
    try:
        assert _listed(docs_key) == narrow, "unreachable install: fail narrow"
    finally:
        mcp_server.BASE_URL = saved_base
    assert len(narrow) < len(ALL) and "list_keys" not in narrow
    # The failure is cached only briefly: a later tools/list retries the lookup.
    assert mcp_server._SCOPE_FAIL_TTL_S <= 5.0 < mcp_server._SCOPE_TTL_S

    # Filtering is a convenience, not the boundary: a scope-hidden tool called anyway
    # is refused by the API (403 naming the grant), never served.
    with _module(TOKEN=docs_key, _ENV_TOOLSETS=None):
        try:
            asyncio.run(mcp_server.mcp.call_tool("list_keys", {"project_id": 1}))
        except mcp_server._SdkToolError as exc:
            assert "403" in str(exc) or "keys" in str(exc).lower(), exc
        else:
            raise AssertionError("the API must refuse a docs-only key on a keys tool")
    assert _listed(docs_key, _SCOPE_FILTER=False) == ALL, "filter off lists everything"
    counts = [len(_listed(k)) for k in (docs_key, keys_key, full_key)]
    print(
        f"  tools/list: docs key {counts[0]}, keys key {counts[1]}, full key {counts[2]}"
        f" of {len(ALL)}; failed lookup = {len(narrow)}; off = all"
    )


# --- 4. toolsets ------------------------------------------------------------
def check_toolsets(full_key, docs_key) -> None:
    demo = frozenset(registry.toolsets()["orientation"] + registry.toolsets()["connectors"])
    got = _listed(full_key, _ENV_TOOLSETS=demo)
    assert got == [n for n in ALL if n in demo], got
    assert len(got) == 11, got

    # A call outside the toolset is refused before it reaches any handler.
    with _module(TOKEN=full_key, _ENV_TOOLSETS=demo):
        try:
            asyncio.run(mcp_server.mcp.call_tool("list_projects", {}))
        except mcp_server._SdkToolError as exc:
            assert "toolset" in str(exc).lower() and "list_projects" in str(exc), exc
            # #756: the refusal leaves with the machine-readable JSON error tail too.
            assert '{"error"' in str(exc).splitlines()[-1], exc
        else:
            raise AssertionError("a tool outside the toolsets must be refused")
        res = asyncio.run(mcp_server.mcp.call_tool("whoami", {}))
        body = json.loads("".join(getattr(b, "text", "") for b in res.content))
        assert body["toolsets"] == ["connectors", "orientation"], body
        assert body["scopes"] == ["admin", "api", "docs", "keys"], body

    # HTTP: a header narrows; a query narrows; neither widens the operator's set.
    with _module(TOKEN="", _ENV_TOOLSETS=None):
        with _http_request({"Authorization": f"Bearer {full_key}", "X-Loc-Toolsets": "keys"}):
            got = [t.name for t in asyncio.run(mcp_server.mcp.list_tools())]
        assert got == list(registry.toolsets()["keys"]), got
        with _http_request({"Authorization": f"Bearer {docs_key}"}, {"toolsets": "connectors"}):
            got = [t.name for t in asyncio.run(mcp_server.mcp.list_tools())]
        assert got == list(registry.toolsets()["connectors"]), got
    with _module(TOKEN="", _ENV_TOOLSETS=demo):
        with _http_request(
            {"Authorization": f"Bearer {full_key}", "X-Loc-Toolsets": "keys,connectors"}
        ):
            got = [t.name for t in asyncio.run(mcp_server.mcp.list_tools())]
        assert got == list(registry.toolsets()["connectors"]), got
        with _http_request({"Authorization": f"Bearer {full_key}", "X-Loc-Toolsets": "bogus"}):
            try:
                asyncio.run(mcp_server.mcp.list_tools())
            except Exception as exc:
                assert "bogus" in str(exc), exc
            else:
                raise AssertionError("an unknown request toolset must be refused")
    print("  toolsets: env narrows + refuses calls; header/query narrow, never widen")


# --- 5. whoami ----------------------------------------------------------------
def check_whoami(docs_key) -> None:
    with _module(TOKEN=docs_key, _ENV_TOOLSETS=None):
        body = json.loads(asyncio.run(mcp_server.call_tool_unvalidated("whoami", {})))
    assert body["scopes"] == ["api", "docs"], body
    assert body["toolsets"] == "all", body
    print("  whoami: reports the key's scopes and the active toolsets")


# --- 6. cache -------------------------------------------------------------------
def check_cache(full_key_for_meta) -> None:
    calls = []

    async def handler(request):
        calls.append(request.url.path)
        return httpx.Response(200, json={"ok": True, "scopes": ["api", "docs"]})

    saved_client = mcp_server._client

    def _client():
        return httpx.AsyncClient(transport=httpx.MockTransport(handler))

    mcp_server._client = _client
    try:
        with _module(TOKEN="ls" + "u_cache", _ENV_TOOLSETS=None):
            first = [t.name for t in asyncio.run(mcp_server.mcp.list_tools())]
            second = [t.name for t in asyncio.run(mcp_server.mcp.list_tools())]
    finally:
        mcp_server._client = saved_client
    assert first == second == _names(lambda t: t.scope in ("api", "docs"))
    assert calls == ["/api/account"], calls
    # The filter runs on the SDK's list, so #750/#752 metadata survives it: every listed
    # tool keeps its annotations and outputSchema, identical to the registered tool.
    with _module(TOKEN=full_key_for_meta, _ENV_TOOLSETS=None):
        listed = {t.name: t for t in asyncio.run(mcp_server.mcp.list_tools())}
    full = {t.name: t for t in asyncio.run(mcp_server.MCPServer.list_tools(mcp_server.mcp))}
    assert listed and set(listed) <= set(full)
    for name, tool in listed.items():
        assert tool.annotations == full[name].annotations, name
        assert tool.output_schema == full[name].output_schema, name
        assert tool.annotations is not None and tool.output_schema, name
    print("  cache: one /api/account lookup per key, reused by the next tools/list")


# --- 7. the real wire: a sessionless 2026-07-28 tools/list over the ASGI listener ---
_MODERN_META = {
    "io.modelcontextprotocol/protocolVersion": "2026-07-28",
    "io.modelcontextprotocol/clientCapabilities": {},
    "io.modelcontextprotocol/clientInfo": {"name": "filter-probe", "version": "1"},
}


def _wire_list(client, headers, path="/mcp") -> list:
    resp = client.post(
        path,
        headers={
            "accept": "application/json, text/event-stream",
            "content-type": "application/json",
            "mcp-protocol-version": "2026-07-28",
            "mcp-method": "tools/list",
            **headers,
        },
        json={"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {"_meta": _MODERN_META}},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert "result" in body, body
    return [t["name"] for t in body["result"]["tools"]]


def check_real_wire(docs_key, full_key) -> None:
    with _module(TOKEN="", _ENV_TOOLSETS=None):
        listener = mcp_server.mcp.streamable_http_app(json_response=True)
        with TestClient(listener, base_url="http://127.0.0.1:8765") as client:
            got = _wire_list(client, {"authorization": f"Bearer {docs_key}"})
            assert got == _names(lambda t: t.scope in ("api", "docs")), got
            got = _wire_list(client, {"authorization": f"Bearer {full_key}"})
            assert got == ALL, len(got)
            got = _wire_list(
                client, {"authorization": f"Bearer {full_key}", "x-loc-toolsets": "connectors"}
            )
            assert got == list(registry.toolsets()["connectors"]), got
            got = _wire_list(
                client, {"authorization": f"Bearer {full_key}"}, "/mcp?toolsets=orientation"
            )
            assert got == list(registry.toolsets()["orientation"]), got
    print("  real wire: sessionless tools/list follows the bearer key, header and query")


# --- 8. CLI parity: account show --remote prints scopes; mcp connect --toolsets ---
def check_cli(docs_key) -> None:
    from app.cli import _remote
    from app.cli.cmd_mcp import TOOLSET_NAMES
    from tests._cli_runner import run_cli

    assert set(TOOLSET_NAMES) == set(registry.toolsets()), "CLI toolset names drifted"

    c = TestClient(app)

    def handler(request):
        path = request.url.path
        if request.url.query:
            path += "?" + request.url.query.decode()
        resp = c.request(
            request.method, path, headers=dict(request.headers), content=request.content
        )
        return httpx.Response(resp.status_code, content=resp.content, headers=resp.headers)

    _remote._set_transport(httpx.MockTransport(handler))
    try:
        base = ["--remote", "http://127.0.0.1", "--token", docs_key, "account", "show"]
        rc, out, err = run_cli(base)
        assert rc == 0 and "key scopes: api docs" in out, (rc, out, err)
        rc, out, err = run_cli(["--json", *base])
        assert rc == 0 and json.loads(out)["scopes"] == ["api", "docs"], (rc, out, err)
    finally:
        _remote._set_transport(None)

    rc, out, err = run_cli(
        [
            "--json",
            "mcp",
            "connect",
            "--base-url",
            "https://h.test",
            "--toolsets",
            "keys,connectors",
        ]
    )
    assert rc == 0, (rc, err)
    body = json.loads(out)
    assert body["endpoint"] == "https://h.test/mcp?toolsets=keys,connectors", body
    assert body["toolsets"] == ["keys", "connectors"], body
    assert '"https://h.test/mcp?toolsets=keys,connectors"' in body["claude_code"], body
    rc, out, err = run_cli(["--json", "mcp", "connect", "--base-url", "https://h.test"])
    assert rc == 0 and json.loads(out)["endpoint"] == "https://h.test/mcp", out
    rc, _out, err = run_cli(["mcp", "connect", "--base-url", "https://h.test", "--toolsets", "x"])
    assert rc == 2 and "x" in err, (rc, err)
    print("  CLI: account show --remote prints key scopes; mcp connect --toolsets")


def main() -> None:
    init_db()
    security.seed_initial()
    settings.set_many({"key_projects_enabled": "1"})
    admin = security.get_by_email("admin")
    _k, docs_key = apikeys.create_key(admin.id, "filter docs", scopes="api docs")
    _k, full_key = apikeys.create_key(admin.id, "filter full", scopes="api docs keys admin")
    _k, keys_key = apikeys.create_key(admin.id, "filter keys", scopes="api keys")

    check_registry_toolsets()
    check_account_scopes(docs_key, full_key, keys_key)
    check_cli(docs_key)

    port = _free_port()
    mcp_server.BASE_URL = f"http://127.0.0.1:{port}"
    srv = _UvicornThread(port)
    try:
        srv.start()
        check_scope_filter(docs_key, full_key, keys_key)
        check_toolsets(full_key, docs_key)
        check_whoami(docs_key)
        check_cache(full_key)
        check_real_wire(docs_key, full_key)
    finally:
        srv.stop()
    print("MCP TOOL FILTER OK")


if __name__ == "__main__":
    main()
