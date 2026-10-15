"""#754: the `mcp_cms` MCP client speaks BOTH protocol eras. Fully offline.

MCP 2026-07-28 removed the initialize handshake and the session: every request carries
`_meta` (protocolVersion, clientInfo, clientCapabilities) and the mirrored headers
MCP-Protocol-Version / Mcp-Method / Mcp-Name (+ Mcp-Param-* for `x-mcp-header`
parameters), and a client finds the era with `server/discover`, falling back to
`initialize` for older servers (spec: basic/versioning "Backward Compatibility" and
basic/transports/streamable-http "Backward Compatibility").

What it asserts:
  1  a STRICT modern-only fake server (rejects initialize, validates every header against
     the body, refuses a session id): test() connects at 2026-07-28 with server/discover,
     tools/call carries _meta + Mcp-Method + Mcp-Name (+ base64 for a non-ASCII tool name)
     + Mcp-Param-Region for an x-mcp-header parameter, close() sends no DELETE
  2  a tool with an invalid x-mcp-header annotation is dropped from tools/list
  3  legacy servers still work: a 400 without a JSON-RPC body, a -32601, and a -32022 that
     names only handshake versions all fall back to initialize + session id
  4  a modern-only server whose versions we do not speak fails with a clear message and
     never receives initialize; mcp_protocol=2026-07-28 never falls back; legacy never probes
  5  an input_required result (we declare no capabilities) is refused, not answered
  6  call_timeout_s reaches the transport (default 30 s, a connection's 120 s, clamped)
  7  the REAL pinned mcp SDK server (MCPServer, in-process): auto negotiates 2026-07-28
     with no session id; mcp_protocol=legacy runs the handshake against the same server
  8  surfaces: the two new connection fields are on the spec (web form + CLI config)

ASCII-only prints; final line is 'verify_mcp_cms_era OK'.
"""

import base64
import json
import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(_ROOT)
sys.path.insert(0, _ROOT)
from tests._bootstrap import init_env

init_env("loctms_mcpera_")

import httpx

from app.db import init_db
from app.services.connectors import mcp_cms as M
from app.services.connectors import mcp_wire as W
from app.services.connectors.base import ConnectorError

FAKE_BEARER = "tok-ERA-1234567890"  # pregate: allow-secret offline fake server value
URL = "http://cms.test/mcp"
MODERN = "2026-07-28"
_LOCALES = ["en", "fr"]


def _cfg(**extra):
    cfg = {
        "mcp_url": URL,
        "list_tool": "list_page",
        "get_tool": "get_page",
        "write_tool": "update_page",
        "list_args": '{"locale": "{{source_locale}}"}',
        "list_items_path": "results",
        "get_item_path": "data",
        "id_field": "documentId",
        "get_args": '{"documentId": "{{id}}", "locale": "{{locale}}"}',
        "write_args": '{"documentId": "{{id}}", "locale": "{{locale}}", "data": "{{fields}}"}',
        "locales": "en, fr",
        "fields": "title, body",
        "source_locale": "en",
        "target_locales": "fr",
    }
    cfg.update(extra)
    return cfg


def _mc(transport, **extra):
    return M.McpCmsConnector(_cfg(**extra), {"bearer_token": FAKE_BEARER}, transport=transport)


def _b64(value: str) -> str:
    return "=?base64?" + base64.b64encode(value.encode("utf-8")).decode("ascii") + "?="


def _tools():
    loc = {"type": "string", "enum": _LOCALES}
    return [
        {
            "name": "list_page",
            "inputSchema": {"type": "object", "properties": {"locale": loc}},
        },
        {
            "name": "get_page",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "documentId": {"type": "string"},
                    "locale": loc,
                    "region": {"type": "string", "x-mcp-header": "Region"},
                },
                "required": ["documentId"],
            },
        },
        {
            "name": "update_page",
            "inputSchema": {
                "type": "object",
                "properties": {"documentId": {"type": "string"}, "locale": loc, "data": {}},
            },
        },
        {
            # x-mcp-header on a number is forbidden: the tool must be dropped
            "name": "bad_header_tool",
            "inputSchema": {
                "type": "object",
                "properties": {"n": {"type": "number", "x-mcp-header": "N"}},
            },
        },
        {"name": "café_tool", "inputSchema": {"type": "object", "properties": {}}},
    ]


class ModernOnly:
    """A strict 2026-07-28-only Streamable HTTP server (behind httpx.MockTransport)."""

    def __init__(self, supported=(MODERN,), input_required=False):
        self.supported = list(supported)
        self.input_required = input_required
        self.posts: list = []
        self.headers: list = []
        self.deletes = 0
        self.docs = {"d1": {"en": {"documentId": "d1", "title": "Hello", "body": "Body"}}}

    def transport(self):
        return httpx.MockTransport(self._handle)

    def _err(self, rid, code, message, data=None, status=400):
        err = {"code": code, "message": message}
        if data is not None:
            err["data"] = data
        return httpx.Response(status, json={"jsonrpc": "2.0", "id": rid, "error": err})

    def _ok(self, rid, result):
        result = {"resultType": "complete", **result}
        result.setdefault("_meta", {})[W.META_SERVER_INFO] = {"name": "modern-cms", "version": "9"}
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": rid, "result": result})

    def _handle(self, request: httpx.Request):
        if request.method == "DELETE":
            self.deletes += 1
            return httpx.Response(405)
        if request.headers.get("authorization") != f"Bearer {FAKE_BEARER}":
            return httpx.Response(401)
        msg = json.loads(request.content)
        self.posts.append(msg)
        self.headers.append(dict(request.headers))
        rid = msg.get("id")
        method = msg.get("method")
        params = msg.get("params") or {}
        meta = params.get("_meta") or {}
        hv = request.headers.get("mcp-protocol-version")
        if method == "initialize" or W.META_PROTOCOL not in meta:
            # Legacy client against a modern-only server: the spec says reject.
            return self._err(rid, -32602, "missing _meta (this server speaks 2026-07-28 only)")
        assert "mcp-session-id" not in request.headers, "a session id on the modern wire"
        if hv != meta.get(W.META_PROTOCOL):
            return self._err(rid, W.HEADER_MISMATCH, "MCP-Protocol-Version != _meta")
        if meta[W.META_PROTOCOL] not in self.supported:
            return self._err(
                rid,
                W.UNSUPPORTED_PROTOCOL_VERSION,
                "Unsupported protocol version",
                {"supported": self.supported, "requested": meta[W.META_PROTOCOL]},
            )
        assert isinstance(meta.get(W.META_CLIENT_CAPS), dict), meta
        assert isinstance(meta.get(W.META_CLIENT_INFO), dict), meta
        if request.headers.get("mcp-method") != method:
            return self._err(rid, W.HEADER_MISMATCH, "Mcp-Method != method")
        if method == "server/discover":
            return self._ok(
                rid, {"supportedVersions": self.supported, "capabilities": {"tools": {}}}
            )
        if method == "tools/list":
            return self._ok(rid, {"tools": _tools()})
        if method != "tools/call":
            return self._err(rid, -32601, "Method not found", status=404)
        name = params.get("name")
        if request.headers.get("mcp-name") != W.encode_header_value(name):
            return self._err(rid, W.HEADER_MISMATCH, "Mcp-Name != params.name")
        args = params.get("arguments") or {}
        region_hdr = request.headers.get("mcp-param-region")
        if name == "get_page" and "region" in args and region_hdr != args["region"]:
            return self._err(rid, W.HEADER_MISMATCH, "Mcp-Param-Region != region")
        if self.input_required:
            return httpx.Response(
                200,
                json={
                    "jsonrpc": "2.0",
                    "id": rid,
                    "result": {
                        "resultType": "input_required",
                        "inputRequests": {"q": {"method": "elicitation/create", "params": {}}},
                    },
                },
            )
        if name == "get_page":
            v = self.docs.get(args.get("documentId"), {}).get(args.get("locale", "en"))
            if v is None:
                return self._ok(
                    rid, {"content": [{"type": "text", "text": "not found"}], "isError": True}
                )
            return self._ok(rid, {"content": [{"type": "text", "text": json.dumps({"data": v})}]})
        if name == "update_page":
            d = self.docs.setdefault(args["documentId"], {})
            d[args.get("locale", "en")] = {"documentId": args["documentId"], **args["data"]}
            return self._ok(rid, {"content": [{"type": "text", "text": json.dumps({"ok": 1})}]})
        return self._ok(rid, {"content": [{"type": "text", "text": "{}"}]})


class Legacy:
    """A 2025-06-18 handshake server. `discover` picks how it answers server/discover."""

    def __init__(self, discover="400-plain"):
        self.discover = discover
        self.posts: list = []
        self.session = "sess-legacy-1"

    def transport(self):
        return httpx.MockTransport(self._handle)

    def _handle(self, request: httpx.Request):
        if request.method == "DELETE":
            return httpx.Response(200)
        msg = json.loads(request.content)
        self.posts.append(msg)
        method, rid = msg.get("method"), msg.get("id")
        if method == "server/discover":
            if self.discover == "400-plain":
                return httpx.Response(400, text="Bad Request: no session")
            if self.discover == "-32601":
                return httpx.Response(
                    200,
                    json={"jsonrpc": "2.0", "id": rid, "error": {"code": -32601, "message": "x"}},
                )
            if self.discover == "-32022-legacy":
                return httpx.Response(
                    400,
                    json={
                        "jsonrpc": "2.0",
                        "id": rid,
                        "error": {
                            "code": -32022,
                            "message": "Unsupported protocol version",
                            "data": {"supported": ["2025-11-25", "2025-06-18"]},
                        },
                    },
                )
            if self.discover == "-32000-idnull":
                return httpx.Response(
                    400,
                    json={
                        "jsonrpc": "2.0",
                        "id": None,
                        "error": {"code": -32000, "message": "Bad Request: No valid session ID"},
                    },
                )
        if method == "initialize":
            return httpx.Response(
                200,
                json={
                    "jsonrpc": "2.0",
                    "id": rid,
                    "result": {
                        "protocolVersion": "2025-06-18",
                        "capabilities": {"tools": {}},
                        "serverInfo": {"name": "legacy-cms", "version": "1"},
                    },
                },
                headers={"mcp-session-id": self.session},
            )
        if request.headers.get("mcp-session-id") != self.session:
            return httpx.Response(400, json={"error": "missing session"})
        if "id" not in msg:
            return httpx.Response(202)
        assert "mcp-method" not in request.headers, "modern headers on the legacy wire"
        assert "_meta" not in (msg.get("params") or {}), "modern _meta on the legacy wire"
        if method == "tools/list":
            tools = [t for t in _tools() if t["name"] in ("list_page", "get_page", "update_page")]
            return httpx.Response(
                200, json={"jsonrpc": "2.0", "id": rid, "result": {"tools": tools}}
            )
        return httpx.Response(
            200,
            json={
                "jsonrpc": "2.0",
                "id": rid,
                "result": {"content": [{"type": "text", "text": '{"data": {}}'}]},
            },
        )


class WpAdapterLike:
    """Shaped like the WordPress MCP Adapter 0.7.0 HTTP transport (wordpress-demo
    site-data, McpWireOrchestrator + HttpRequestHandler + McpVersionNegotiator): DUAL-ERA.
    A request whose header OR _meta names 2026-07-28 is served statelessly after the
    envelope headers are checked (exact MCP-Protocol-Version, header == _meta, Mcp-Method,
    Mcp-Name with base64 decoding, Mcp-Param-* looked up with "_" folded to "-", object
    clientCapabilities); anything else is the 2025-11-25 lifecycle (initialize, then
    Mcp-Session-Id). Errors are JSON-RPC bodies with HTTP 400. `reject_params=True`
    imitates an adapter build that rejects our parameter header (-32020) before running
    the tool, to prove `auto` falls back to the handshake on its own."""

    def __init__(self, reject_params=False):
        self.reject_params = reject_params
        self.posts: list = []
        self.eras: list = []
        self.writes = 0
        self.deletes = 0
        self.session = "wp-sess-7"

    def transport(self):
        return httpx.MockTransport(self._handle)

    @staticmethod
    def _err(rid, code, message, status=400):
        return httpx.Response(
            status, json={"jsonrpc": "2.0", "id": rid, "error": {"code": code, "message": message}}
        )

    @staticmethod
    def _decode(v):
        if v is None:
            return None
        if v.startswith("=?base64?") and v.endswith("?="):
            return base64.b64decode(v[9:-2]).decode("utf-8")
        return v if all(0x20 <= ord(c) <= 0x7E for c in v) else None

    @staticmethod
    def _tools():
        return [
            {
                "name": "northwind-list-posts",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "post_type": {"type": "string", "x-mcp-header": "post_type"},
                        "locale": {"type": "string"},
                    },
                },
            },
            {
                "name": "northwind-update-post",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "id": {"type": "integer", "x-mcp-header": "Post-Id"},
                        "data": {"type": "object"},
                    },
                },
            },
        ]

    def _result(self, rid, method, params):
        if method == "server/discover":
            return {
                "supportedVersions": [MODERN, "2025-11-25"],
                "capabilities": {"tools": {"listChanged": False}},
                "instructions": "Northwind",
            }
        if method == "tools/list":
            return {"tools": self._tools()}
        if method == "tools/call":
            if params.get("name") == "northwind-update-post":
                self.writes += 1
            return {"content": [{"type": "text", "text": json.dumps({"ok": True, "items": []})}]}
        return None

    def _handle(self, request: httpx.Request):
        if request.method == "DELETE":
            self.deletes += 1
            return httpx.Response(200)
        msg = json.loads(request.content)
        self.posts.append(msg)
        rid, method = msg.get("id"), msg.get("method")
        params = msg.get("params") or {}
        meta = params.get("_meta") if isinstance(params.get("_meta"), dict) else {}
        hdr = {k.lower(): v for k, v in request.headers.items()}
        body_rev = meta.get(W.META_PROTOCOL)
        if MODERN in (body_rev, hdr.get("mcp-protocol-version")):
            self.eras.append(("modern", method))
            if hdr.get("mcp-protocol-version") != MODERN or body_rev not in (None, MODERN):
                return self._err(rid, W.HEADER_MISMATCH, "MCP-Protocol-Version mismatch")
            assert "mcp-session-id" not in hdr, "a session id on the 2026 wire"
            if self._decode(hdr.get("mcp-method")) != method:
                return self._err(rid, W.HEADER_MISMATCH, "Mcp-Method mismatch")
            if method == "tools/call" and self._decode(hdr.get("mcp-name")) != params.get("name"):
                return self._err(rid, W.HEADER_MISMATCH, "Mcp-Name mismatch")
            if not isinstance(meta.get(W.META_CLIENT_CAPS), dict):
                return self._err(rid, -32602, "object clientCapabilities required")
            if method == "tools/call":
                if self.reject_params:
                    return self._err(rid, W.HEADER_MISMATCH, "Mcp-Param-Post-Id is missing")
                args = params.get("arguments") or {}
                tool = next(t for t in self._tools() if t["name"] == params.get("name"))
                for prop, sch in tool["inputSchema"]["properties"].items():
                    ann = sch.get("x-mcp-header")
                    if not ann or args.get(prop) is None:
                        continue
                    key = ("mcp-param-" + ann.lower()).replace("_", "-")
                    got = self._decode(
                        next((v for k, v in hdr.items() if k.replace("_", "-") == key), None)
                    )
                    want = args[prop]
                    want = ("true" if want else "false") if isinstance(want, bool) else str(want)
                    if got != want:
                        return self._err(rid, W.HEADER_MISMATCH, f"{key} mismatch")
            res = self._result(rid, method, params)
            if res is None:
                return self._err(rid, -32601, "Method not found", status=404)
            return httpx.Response(200, json={"jsonrpc": "2.0", "id": rid, "result": res})
        self.eras.append(("legacy", method))
        if method == "initialize":
            return httpx.Response(
                200,
                json={
                    "jsonrpc": "2.0",
                    "id": rid,
                    "result": {
                        "protocolVersion": "2025-06-18",
                        "capabilities": {"tools": {}},
                        "serverInfo": {"name": "northwind", "version": "0.7.0"},
                    },
                },
                headers={"mcp-session-id": self.session},
            )
        if hdr.get("mcp-session-id") != self.session:
            return self._err(rid, -32600, "The 2025 lifecycle requires initialization")
        if "id" not in msg:
            return httpx.Response(202)
        assert "mcp-method" not in hdr and not meta, "modern envelope on the legacy session"
        res = self._result(rid, method, params)
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": rid, "result": res})


def _wp_adapter_like() -> None:
    from app.services.connectors.contract import Topology  # noqa: F401  (import check only)

    def sess(fake, mode="auto"):
        return W.McpSession(_FakeConn(fake.transport()), URL, {}, mode=mode)

    # (9a) auto against the dual-era adapter: modern, stateless, parameter headers mirrored
    wp = WpAdapterLike()
    s = sess(wp)
    s.connect()
    assert s.protocol == MODERN and s.session_id is None, (s.protocol, s.session_id)
    s.call_tool("northwind-list-posts", {"post_type": "post", "locale": "en"})
    s.call_tool("northwind-update-post", {"id": 42, "data": {"title": "Bonjour"}})
    assert wp.writes == 1, wp.writes
    assert all(era == "modern" for era, _ in wp.eras), wp.eras
    s.close()
    assert wp.deletes == 0
    # (9b) an adapter that rejects our envelope on tools/call: auto falls back by itself,
    # the write runs exactly once (the rejected attempt never ran) on the legacy session
    wp = WpAdapterLike(reject_params=True)
    s = sess(wp)
    s.connect()
    assert s.protocol == MODERN
    s.call_tool("northwind-update-post", {"id": 42, "data": {"title": "Bonjour"}})
    assert wp.writes == 1, wp.writes
    assert s.protocol == "2025-06-18" and s.session_id == wp.session, (s.protocol, s.session_id)
    assert ("legacy", "initialize") in wp.eras and wp.eras[-1] == ("legacy", "tools/call"), wp.eras
    s.call_tool("northwind-update-post", {"id": 43, "data": {"title": "Salut"}})
    assert wp.writes == 2 and wp.eras[-1] == ("legacy", "tools/call"), wp.eras
    # pinned 2026-07-28 never falls back: the error surfaces and nothing ran
    wp = WpAdapterLike(reject_params=True)
    s = sess(wp, mode=MODERN)
    try:
        s.call_tool("northwind-update-post", {"id": 42, "data": {}})
    except W.McpProtocolError as e:
        assert "-32020" in str(e), e
    else:
        raise AssertionError("pinned 2026-07-28 fell back")
    assert wp.writes == 0 and ("legacy", "initialize") not in wp.eras, wp.eras
    # (9c) legacy pin against the same adapter keeps the handshake (today's demo behaviour)
    wp = WpAdapterLike()
    s = sess(wp, mode="legacy")
    s.call_tool("northwind-update-post", {"id": 7, "data": {}})
    assert wp.writes == 1 and all(era == "legacy" for era, _ in wp.eras), wp.eras
    s.close()
    assert wp.deletes == 1

    # (9d) a modern-ONLY server that rejects the envelope: the downgrade's initialize fails
    # too, and the ORIGINAL -32020 is what the operator sees
    class _RejectingModern(ModernOnly):
        def _handle(self, request):
            msg = json.loads(request.content)
            if msg.get("method") == "tools/call":
                self.posts.append(msg)
                return self._err(msg.get("id"), W.HEADER_MISMATCH, "Mcp-Name mismatch")
            return super()._handle(request)

    rm = _RejectingModern()
    s = W.McpSession(_FakeConn(rm.transport()), URL, {"Authorization": f"Bearer {FAKE_BEARER}"})
    s.connect()
    try:
        s.call_tool("get_page", {"documentId": "d1"})
    except W.McpProtocolError as e:
        assert "-32020" in str(e) and "Mcp-Name" in str(e), e
    else:
        raise AssertionError("a rejected envelope was swallowed")
    assert s.protocol == MODERN, s.protocol
    print(
        "  (9) WordPress-adapter-shaped dual-era server: auto goes 2026-07-28 statelessly; an "
        "envelope rejection falls back to the handshake once (write runs once); pinned modes hold"
    )


class _FakeConn:
    """Just enough of a Connector for McpSession: the real guarded _request path."""

    def __init__(self, transport):
        self._mc = _mc(transport)

    def _client(self):
        return self._mc._client()

    def _request(self, *a, **kw):
        return self._mc._request(*a, **kw)


def _methods(fake):
    return [p.get("method") for p in fake.posts]


def main() -> None:
    init_db()

    # ---- (1) strict modern-only server -------------------------------------------------
    fake = ModernOnly()
    mc = _mc(fake.transport())
    ok, msg = mc.test()
    assert ok, msg
    s = mc.session()
    assert s.protocol == MODERN and s.session_id is None, (s.protocol, s.session_id)
    assert "modern-cms" in msg and "MCP 2026-07-28" in msg, msg
    assert _methods(fake)[0] == "server/discover" and "initialize" not in _methods(fake)
    disc_hdr = fake.headers[0]
    assert disc_hdr.get("mcp-protocol-version") == MODERN, disc_hdr
    assert disc_hdr.get("mcp-method") == "server/discover", disc_hdr
    out = s.call_tool("get_page", {"documentId": "d1", "locale": "en", "region": "us-west1"})
    assert out["data"]["title"] == "Hello", out
    h = fake.headers[-1]
    assert h.get("mcp-method") == "tools/call" and h.get("mcp-name") == "get_page", h
    assert h.get("mcp-param-region") == "us-west1", h
    call = fake.posts[-1]
    assert call["params"]["_meta"][W.META_PROTOCOL] == MODERN, call
    assert call["params"]["_meta"][W.META_CLIENT_CAPS] == {}, call
    s.call_tool("café_tool", {})
    assert fake.headers[-1].get("mcp-name") == _b64("café_tool"), fake.headers[-1]
    assert W.encode_header_value(" pad ") == _b64(" pad ")
    assert W.encode_header_value("=?base64?x?=") == _b64("=?base64?x?=")
    assert W.encode_header_value("a\r\nX-Evil: 1") == _b64("a\r\nX-Evil: 1")
    s.call_tool("update_page", {"documentId": "d1", "locale": "fr", "data": {"title": "B"}})
    assert fake.docs["d1"]["fr"]["title"] == "B"
    mc.close()
    assert fake.deletes == 0, "the 2026-07-28 wire has no session to DELETE"
    print("  (1) strict modern server: discover, _meta, Mcp-Method/Name/Param headers, no session")

    # ---- (2) invalid x-mcp-header -> tool dropped ------------------------------------------
    tools = _mc(ModernOnly().transport()).session().list_tools()
    assert "bad_header_tool" not in tools and "get_page" in tools, sorted(tools)
    assert W.header_params({"type": "object", "properties": {"a": {"type": "array"}}}) == {}
    bad = {"type": "object", "properties": {"a": {"type": "array", "items": {"x-mcp-header": "A"}}}}
    assert W.header_params(bad) is None
    dup = {
        "type": "object",
        "properties": {
            "a": {"type": "string", "x-mcp-header": "Dup"},
            "b": {"type": "string", "x-mcp-header": "dup"},
        },
    }
    assert W.header_params(dup) is None
    nested = {
        "type": "object",
        "properties": {
            "o": {"type": "object", "properties": {"r": {"type": "string", "x-mcp-header": "R"}}}
        },
    }
    assert W.header_params(nested) == {"R": ("o", "r")}
    print("  (2) invalid x-mcp-header tool dropped; reachable nested annotation kept")

    # ---- (3) legacy servers fall back ------------------------------------------------------
    for shape in ("400-plain", "-32601", "-32022-legacy", "-32000-idnull"):
        leg = Legacy(shape)
        mc = _mc(leg.transport())
        ok, msg = mc.test()
        assert ok, (shape, msg)
        s = mc.session()
        assert s.protocol == "2025-06-18" and s.session_id == leg.session, (shape, s.protocol)
        assert _methods(leg)[:3] == ["server/discover", "initialize", "notifications/initialized"]
        mc.close()
    print("  (3) legacy fallbacks: plain 400, -32601, -32022 naming handshake versions, -32000")

    # ---- (4) incompatible / pinned modes ---------------------------------------------------
    future = ModernOnly(supported=["2099-01-01"])
    ok, msg = _mc(future.transport()).test()
    assert not ok and "2099-01-01" in msg and "none of which" in msg, msg
    assert "initialize" not in _methods(future), _methods(future)
    leg = Legacy("400-plain")
    ok, msg = _mc(leg.transport(), mcp_protocol=MODERN).test()
    assert not ok and "initialize" not in _methods(leg), (msg, _methods(leg))
    leg = Legacy("-32022-legacy")
    ok, msg = _mc(leg.transport(), mcp_protocol=MODERN).test()
    assert not ok and "auto or legacy" in msg and "initialize" not in _methods(leg), msg
    leg = Legacy("400-plain")
    ok, msg = _mc(leg.transport(), mcp_protocol="legacy").test()
    assert ok and "server/discover" not in _methods(leg), (msg, _methods(leg))
    modern = ModernOnly()
    ok, msg = _mc(modern.transport(), mcp_protocol="legacy").test()
    assert not ok, msg
    assert W.normalize_mode("LEGACY") == "legacy" and W.normalize_mode("x") == "auto"
    print("  (4) unknown-only versions refused clearly; pinned modes never cross eras")

    # ---- (5) input_required is refused ---------------------------------------------------------
    ir = ModernOnly(input_required=True)
    s = _mc(ir.transport()).session()
    try:
        s.call_tool("get_page", {"documentId": "d1"})
    except W.McpProtocolError as e:
        assert "input_required" in str(e), e
    else:
        raise AssertionError("an input_required result was accepted")
    print("  (5) input_required (MRTR) result refused: we declare no capabilities")

    # ---- (6) call timeout --------------------------------------------------------------------
    seen: list = []

    def timed(request):
        seen.append((request.extensions.get("timeout") or {}).get("read"))
        return ModernOnly()._handle(request)

    _mc(httpx.MockTransport(timed)).session()
    assert seen and abs(seen[-1] - 30.0) < 1.0, seen
    seen.clear()
    _mc(httpx.MockTransport(timed), call_timeout_s="120").session()
    assert seen and 100.0 < seen[-1] <= 120.0, seen
    assert W.normalize_timeout("9999") == 300.0 and W.normalize_timeout("1") == 5.0
    assert W.normalize_timeout("nan") == 30.0 and W.normalize_timeout("") == 30.0
    print("  (6) call_timeout_s reaches the transport (30 s default, 120 s set, clamped)")

    # ---- (7) the real SDK server --------------------------------------------------------------
    _real_sdk()

    # ---- (9) a server shaped like the WordPress MCP Adapter 0.7.0 ----------------------------
    _wp_adapter_like()

    # ---- (8) surfaces -------------------------------------------------------------------------
    keys = {f.name for f in M.SPEC.config_fields}
    assert {"mcp_protocol", "call_timeout_s"} <= keys, keys
    assert "2026-07-28" in M.SPEC.docs_url
    print("  (8) mcp_protocol + call_timeout_s on the connector spec (web form + CLI config)")

    print("verify_mcp_cms_era OK")


class _AsgiBridge(httpx.BaseTransport):
    _HOP = frozenset({"host", "content-length", "transfer-encoding", "content-encoding"})

    def __init__(self, tc, log):
        self.tc = tc
        self.log = log

    def handle_request(self, request):
        request.read()
        hdrs = {k: v for k, v in request.headers.items() if k.lower() not in self._HOP}
        hdrs["accept-encoding"] = "identity"
        self.log.append((request.method, dict(request.headers)))
        r = self.tc.request(request.method, str(request.url), headers=hdrs, content=request.content)
        out = [(k, v) for k, v in r.headers.multi_items() if k.lower() not in self._HOP]
        return httpx.Response(r.status_code, headers=out, content=r.content)


def _real_sdk():
    from mcp.server.mcpserver import MCPServer
    from mcp.server.transport_security import TransportSecuritySettings
    from starlette.testclient import TestClient

    srv = MCPServer("sdk-era-cms")

    @srv.tool()
    def list_page(locale: str = "en") -> dict:
        return {"results": [{"documentId": "r1", "title": "Hi", "body": "B"}]}

    @srv.tool()
    def get_page(documentId: str, locale: str = "en") -> dict:
        return {"data": {"documentId": documentId, "title": "Hi", "body": "B"}}

    @srv.tool()
    def update_page(documentId: str, data: dict, locale: str = "en") -> dict:
        return {"data": data}

    asgi = srv.streamable_http_app(
        transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False)
    )
    with TestClient(asgi, base_url="http://cms.test") as client:
        log: list = []
        mc = _mc(_AsgiBridge(client, log))
        ok, msg = mc.test()
        assert ok and "sdk-era-cms" in msg and "MCP 2026-07-28" in msg, msg
        s = mc.session()
        assert s.protocol == MODERN and s.session_id is None
        s.call_tool("update_page", {"documentId": "r1", "locale": "fr", "data": {"title": "X"}})
        assert all("mcp-session-id" not in h for _m, h in log), "session id on the modern wire"
        mc.close()
        assert all(m == "POST" for m, _h in log), log

        log.clear()
        mc = _mc(_AsgiBridge(client, log), mcp_protocol="legacy")
        ok, msg = mc.test()
        assert ok and "sdk-era-cms" in msg, msg
        s = mc.session()
        assert s.protocol in W.HANDSHAKE_PROTOCOLS and s.session_id, (s.protocol, s.session_id)
        mc.close()
    print("  (7) real mcp SDK server: auto -> 2026-07-28 stateless; legacy -> handshake + session")


if __name__ == "__main__":
    try:
        main()
    except ConnectorError as e:  # pragma: no cover - a failure prints, never hides
        raise AssertionError(str(e)) from e
