"""MCP Path A -- the claude.ai static-bearer connector, proven over a REAL listener.

Path A (approved 2026-08-06) makes Localization OS's MCP server claude.ai-connectable
with NO OAuth: claude.ai's custom-connector UI sends a fixed
`Authorization: Bearer <token>` on every request, and this server's per-request
bearer dispatch already reads exactly that header. The wave-4 unit suite
(verify_mcp_transport) proves the dispatch/contextvar isolation by SETTING the request
contextvar by hand. THIS suite closes the last mile the SDK bump could have
changed: it boots the ACTUAL MCPServer streamable-http listener under uvicorn on a
loopback port, points a REAL mcp streamable-http client at it with an
`Authorization: Bearer` header, and proves the header travels wire -> Starlette
request -> request middleware -> `_current_http_request()` -> the outbound `X-Loc-Token`,
on mcp 2.2.

Hermetic: there is NO Localization OS app. The server's `_client` is swapped for a
recording httpx MockTransport that captures the outbound `X-Loc-Token` and answers
`/api/languages` with a canned body. So what is under test is the SDK's real wire
path + this server's header plumbing, with nothing else in the way.

WHAT IT ASSERTS (all over a real socket, on the pinned SDK):

  bearer over the wire   a client sending `Authorization: Bearer lsu_WIRE` (env
                         token UNSET) makes the server forward lsu_WIRE to /api.
  concurrent isolation   two clients, two different bearer tokens, both call_tools
                         held IN FLIGHT AT ONCE (a gate inside the recorder), each
                         forwards ITS OWN token; the env fallback is never used.
  missing token          a client sending NO auth header (env unset) gets a clean
                         tool ERROR result naming the `Authorization: Bearer`
                         channel, and nothing is forwarded to /api.
  body cap 413           a POST just over the configured cap gets a clean 413 from
                         the SDK's RequestBodyLimitMiddleware -- not a hang or a
                         crash -- while an under-cap POST is NOT 413'd. Proven on a
                         throwaway MCPServer built with a tiny cap so the probe body
                         stays small (the production 8 MiB value is asserted in
                         verify_mcp_transport).
  session bounds (#387)  over a REAL listener stood up through `_LocMCPServer`'s own
                         defaults: a token-free `initialize` past the session cap gets
                         503 with NO session id (nothing is allocated), the session
                         already open keeps working, and after the idle timeout a new
                         initialize succeeds and the reaped id answers 404. The shipped
                         values are pinned in verify_mcp_transport; the probe lowers the
                         two constants so a cap of 1 is enough to prove the mechanism.

Like verify_mcp_tools / verify_mcp_transport (and UNLIKE verify_mcp), this needs
the mcp SDK and FAILS LOUDLY without it -- a green skip would assert the connector
contract zero times behind a success line. Everything is torn down in `finally`
(both uvicorn threads), and every await is bounded by a timeout, so the suite can
never hang.
"""

import asyncio
import contextlib
import json
import os
import socket
import sys
import threading
import time

import httpx
import httpx2

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(_ROOT)
sys.path.insert(0, _ROOT)

_MCP_DIR = os.path.join(_ROOT, "integrations", "mcp-server")
sys.path.insert(0, _MCP_DIR)

try:
    import server as mcp_server
except ModuleNotFoundError as exc:  # pragma: no cover -- only without the SDK
    if "mcp" in str(exc):
        print(
            "  FATAL: the mcp SDK is not installed, so the claude.ai-connector "
            "suite cannot import server.py. This suite MUST NOT be skipped -- a "
            "green skip would assert the static-bearer connector contract zero "
            "times behind a success message. Install the mcp SDK.",
            file=sys.stderr,
        )
        raise SystemExit(1) from exc
    raise

# Keep output clean: silence httpx/uvicorn/mcp INFO chatter.
import logging

import uvicorn
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
from mcp.server.mcpserver import MCPServer

for _n in ("httpx", "uvicorn", "uvicorn.error", "uvicorn.access", "mcp"):
    logging.getLogger(_n).setLevel(logging.WARNING)

_CALL_TIMEOUT = 30.0  # a generous bound on any single client round-trip


def _bind_free():
    """A socket already BOUND to a free loopback port, returned STILL OPEN.

    Hand it straight to uvicorn (`run(sockets=[sock])`): the usual bind-read-port-
    close-then-let-uvicorn-rebind pattern is a TOCTOU another process could win in
    the gap between the close and the rebind (L6). Keeping the socket open until
    uvicorn owns it closes that gap. SO_REUSEADDR keeps a just-released port out of
    TIME_WAIT on repeat runs."""
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    s.bind(("127.0.0.1", 0))
    return s, s.getsockname()[1]


class _ServerThread:
    """Host an ASGI app (an MCPServer streamable_http_app) on a background uvicorn
    Server, on a PRE-BOUND loopback socket (no free-port TOCTOU). lifespan='on' so
    the session manager's task-group lifespan (streamable_http_app wires
    `session_manager.run()`) starts."""

    def __init__(self, app, sock: socket.socket):
        self.port = sock.getsockname()[1]
        self._sock = sock
        cfg = uvicorn.Config(app, log_level="warning", loop="asyncio", lifespan="on")
        self.server = uvicorn.Server(cfg)
        self.thread = threading.Thread(target=lambda: self.server.run(sockets=[sock]), daemon=True)

    def start(self, timeout: float = 15.0) -> None:
        self.thread.start()
        deadline = time.time() + timeout
        while time.time() < deadline:
            if getattr(self.server, "started", False):
                return
            time.sleep(0.05)
        raise RuntimeError(f"uvicorn did not start within {timeout:.0f}s")

    def stop(self) -> None:
        self.server.should_exit = True
        self.thread.join(timeout=10)
        # uvicorn closes sockets it was handed on shutdown; suppress a double-close.
        with contextlib.suppress(OSError):
            self._sock.close()


class _Recorder:
    """A recording httpx MockTransport backend for the server's /api calls.

    Captures the outbound `X-Loc-Token` of every /api request and answers with a
    canned body that satisfies list_languages. With `gate(n)` armed, it HOLDS every
    call until n are in flight at once -- so the concurrency assertion cannot be
    satisfied by accidental serialization."""

    def __init__(self):
        self.tokens: list = []
        self._n = 0
        self._event = None
        self._count = 0

    def gate(self, n: int) -> None:
        self._n = n

    async def handle(self, request):
        if request.url.path == "/api/account":
            # #751: tools/list (which the client sends before a call) looks up the key's
            # scopes here. That is not a tool's /api call, so it is neither recorded nor
            # gated; verify_mcp_tool_filter owns the filtering itself.
            return httpx.Response(
                200, json={"ok": True, "scopes": ["admin", "api", "docs", "keys"]}
            )
        self.tokens.append(request.headers.get("x-loc-token"))
        if self._n:
            # Created lazily so the asyncio.Event binds to the uvicorn loop that
            # actually runs the handlers (both handler calls share this one loop).
            if self._event is None:
                self._event = asyncio.Event()
            self._count += 1
            if self._count >= self._n:
                self._event.set()
            await asyncio.wait_for(self._event.wait(), timeout=_CALL_TIMEOUT)
        return httpx.Response(200, json={"language_sets": [], "locales": []})


@contextlib.contextmanager
def _backend(rec: _Recorder, env_token: str):
    """Point mcp_server._client at `rec` and pin the env TOKEN, restoring both after.

    The server reads the module globals `_client` and `TOKEN` at call time, and the
    handlers run in the uvicorn thread of the SAME process, so this swap is visible
    to the live listener."""
    saved_client = mcp_server._client
    saved_token = mcp_server.TOKEN

    def _client():
        return httpx.AsyncClient(
            transport=httpx.MockTransport(rec.handle), timeout=mcp_server._HTTP_TIMEOUT
        )

    mcp_server._client = _client
    mcp_server.TOKEN = env_token
    try:
        yield
    finally:
        mcp_server._client = saved_client
        mcp_server.TOKEN = saved_token


async def _list_languages(url: str, bearer: str | None):
    """One real streamable-http round-trip: initialize + call_tool('list_languages').

    `bearer` rides in an `Authorization: Bearer` header -- in mcp 2 the client takes a
    caller-built httpx2.AsyncClient (`http_client=`) whose default headers go out on
    every request, exactly as claude.ai sends it."""
    headers = {"Authorization": "Bearer " + bearer} if bearer else None
    async with (
        httpx2.AsyncClient(headers=headers, timeout=_CALL_TIMEOUT) as http_client,
        streamable_http_client(url, http_client=http_client) as (read, write),
        ClientSession(read, write) as session,
    ):
        await asyncio.wait_for(session.initialize(), timeout=_CALL_TIMEOUT)
        return await asyncio.wait_for(
            session.call_tool("list_languages", {}), timeout=_CALL_TIMEOUT
        )


def _result_text(result) -> str:
    """The concatenated text content of a CallToolResult."""
    out = []
    for block in getattr(result, "content", None) or []:
        txt = getattr(block, "text", None)
        if txt:
            out.append(txt)
    return "\n".join(out)


# ---------------------------------------------------------------------------
def _bearer_url() -> str:
    return _bearer_url.value  # set in main()


async def _test_bearer_over_the_wire():
    """A real client's Authorization: Bearer is forwarded to /api, env UNSET."""
    rec = _Recorder()
    with _backend(rec, ""):  # env token unset -> only the header can authenticate
        result = await _list_languages(_bearer_url(), "lsu_WIRE")
    assert not result.is_error, f"list_languages over the wire errored: {_result_text(result)!r}"
    assert rec.tokens == ["lsu_WIRE"], (
        "the real listener did not forward the request's bearer token to /api "
        f"(wire -> request middleware -> X-Loc-Token broke on this SDK): {rec.tokens!r}"
    )
    print("  [wire] Authorization: Bearer forwarded off a real request to /api (env unset) OK")


async def _test_concurrent_isolation():
    """Two clients, two tokens, both call_tools in flight at once -> each forwards
    ITS OWN token; the env fallback is never used."""
    rec = _Recorder()
    rec.gate(2)
    with _backend(rec, "lsu_ENV"):  # neither request should ever use this
        results = await asyncio.wait_for(
            asyncio.gather(
                _list_languages(_bearer_url(), "lsu_ONE"), _list_languages(_bearer_url(), "lsu_TWO")
            ),
            timeout=_CALL_TIMEOUT,
        )
    for r in results:
        assert not r.is_error, f"a concurrent call errored: {_result_text(r)!r}"
    assert sorted(rec.tokens) == ["lsu_ONE", "lsu_TWO"], (
        "two concurrent real sessions did not each forward their own token "
        f"(cross-request leak over the wire): {rec.tokens!r}"
    )
    assert "lsu_ENV" not in rec.tokens, (
        "a concurrent request fell back to the env token over the wire"
    )
    print(
        "  [concurrency] two in-flight real sessions each forwarded their own "
        "token, no cross-request leak, no env fallback OK"
    )


async def _test_missing_token_clean_error():
    """A real client with NO auth header (env unset) gets a clean tool error naming
    the header channel, and nothing reaches /api."""
    rec = _Recorder()
    with _backend(rec, ""):  # no header AND no env fallback
        result = await _list_languages(_bearer_url(), None)
    assert result.is_error, (
        "a header-less request with no env fallback did NOT return an error result "
        f"(the security law -- no shared identity -- would be broken): {_result_text(result)!r}"
    )
    text = _result_text(result)
    assert "Authorization: Bearer" in text, (
        "the no-token error over the wire did not name the header channel a "
        f"claude.ai connector must set: {text!r}"
    )
    assert rec.tokens == [], f"a no-token request still reached /api over the wire: {rec.tokens!r}"
    print(
        "  [no-token] a header-less request returns a clean error naming the "
        "Authorization channel, nothing forwarded OK"
    )


def _raw_tool_call(url: str, bearer: str, name: str, arguments_json: str) -> dict:
    """One tools/call over the real listener with a HAND-WRITTEN arguments JSON text.

    The SDK client serialises inf/nan to null, so a non-finite number can only reach the
    server as the literal `Infinity` a hostile or sloppy client may send. Returns the
    JSON-RPC result object (content + isError)."""
    hdrs = {
        "content-type": "application/json",
        "accept": "application/json, text/event-stream",
        "authorization": "Bearer " + bearer,
    }
    with httpx.Client(timeout=_CALL_TIMEOUT, headers=hdrs) as c:
        init = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-06-18",
                "capabilities": {},
                "clientInfo": {"name": "refusal-probe", "version": "1"},
            },
        }
        r = c.post(url, content=json.dumps(init))
        sid = r.headers.get("mcp-session-id")
        if sid:
            c.headers["mcp-session-id"] = sid
        c.headers["mcp-protocol-version"] = "2025-06-18"
        c.post(url, content=json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}))
        body = (
            '{"jsonrpc":"2.0","id":2,"method":"tools/call","params":'
            f'{{"name":{json.dumps(name)},"arguments":{arguments_json}}}}}'
        )
        r = c.post(url, content=body)
    for line in r.text.splitlines():
        if line.startswith("data:"):
            return json.loads(line[5:].strip())["result"]
    return json.loads(r.text)["result"]


async def _test_expected_refusals_keep_their_text():
    """mcp 2 hides the text of any exception that is not a ToolError ("Error executing
    tool X" only). Expected refusals must therefore be ToolErrors, with 1.x's wording."""
    rec = _Recorder()
    with _backend(rec, ""):
        # (1) a non-finite number in a request body (a scope number of Infinity): refused, named,
        # and nothing is sent to /api.
        res = await asyncio.to_thread(
            _raw_tool_call,
            _bearer_url(),
            "lsu_REFUSE",
            "project_automation",
            '{"project_id":1,"action":"bind","connection_id":1,"scope":{"max_records":Infinity}}',
        )
        text = _result_text_dict(res)
        assert res.get("isError"), f"a non-finite budget was not an error result: {res!r}"
        assert "not JSON compliant" in text, (
            "a non-finite budget's refusal text was hidden from the client (a non-ToolError "
            f"escaped _call; mcp 2 withholds its text): {text!r}"
        )
        assert rec.tokens == [], f"a refused non-finite body still reached /api: {rec.tokens!r}"
        # (2) a malformed LOC_STUDIO_BASE_URL is a setup error the user can fix: it is
        # relayed naming the underlying cause, and a credential in the URL's userinfo
        # (httpx sends it as Basic auth) is masked, never shown to the client.
        saved = mcp_server.BASE_URL
        mcp_server.BASE_URL = "http://proxyuser:s3cr3t-pw@[::1"
        try:
            res = await asyncio.to_thread(
                _raw_tool_call, _bearer_url(), "lsu_REFUSE", "list_languages", "{}"
            )
        finally:
            mcp_server.BASE_URL = saved
        text = _result_text_dict(res)
        assert res.get("isError") and "Could not build the request" in text, (
            f"a malformed base URL's error text was hidden from the client: {text!r}"
        )
        assert "InvalidURL" in text, f"the underlying cause was not named: {text!r}"
        assert "s3cr3t-pw" not in text and "proxyuser" not in text, (
            f"the base URL's userinfo credential reached the client: {text!r}"
        )
        # (3) an argument too long for a URL (httpx caps a query at 64 KiB) is the
        # caller's to fix, not "could not reach": named, and nothing is sent.
        res = await asyncio.to_thread(
            _raw_tool_call,
            _bearer_url(),
            "lsu_REFUSE",
            "search_tm",
            json.dumps({"q": "a" * 70000}),
        )
        text = _result_text_dict(res)
        assert res.get("isError") and "too long" in text, (
            f"an oversize query argument's refusal text was hidden: {text[:300]!r}"
        )
        assert "Could not reach" not in text, f"an oversize argument blamed the install: {text!r}"
        # (4) a lone surrogate (the 2026-07-28 wire parses with json.loads, which accepts
        # one; httpx's UTF-8 body encode then refuses it): named, and nothing is sent.
        res = await asyncio.to_thread(
            _modern_tool_call,
            _bearer_url(),
            "lsu_REFUSE",
            "project_automation",
            '{"project_id":1,"action":"pause","reason":"a\\ud800"}',
        )
        text = _result_text_dict(res)
        assert res.get("isError") and "surrogates not allowed" in text, (
            f"a lone-surrogate body's refusal text was hidden from the client: {res!r}"
        )
        assert rec.tokens == [], f"a refused request still reached /api: {rec.tokens!r}"
    print(
        "  [refusals] non-finite body, malformed base URL (creds masked), oversize query and "
        "lone surrogate keep their text over the wire OK"
    )


def _modern_tool_call(url: str, bearer: str, name: str, arguments_json: str) -> dict:
    """One tools/call on the 2026-07-28 single-exchange wire (no initialize handshake;
    the envelope rides in params._meta and the routing headers must match it)."""
    pv = "2026-07-28"
    hdrs = {
        "content-type": "application/json",
        "accept": "application/json, text/event-stream",
        "authorization": "Bearer " + bearer,
        "mcp-protocol-version": pv,
        "mcp-method": "tools/call",
        "mcp-name": name,
    }
    meta = {
        "io.modelcontextprotocol/protocolVersion": pv,
        "io.modelcontextprotocol/clientCapabilities": {},
        "io.modelcontextprotocol/clientInfo": {"name": "refusal-probe", "version": "1"},
    }
    body = (
        '{"jsonrpc":"2.0","id":3,"method":"tools/call","params":'
        f'{{"name":{json.dumps(name)},"arguments":{arguments_json},"_meta":{json.dumps(meta)}}}}}'
    )
    with httpx.Client(timeout=_CALL_TIMEOUT, headers=hdrs) as c:
        r = c.post(url, content=body)
    for line in r.text.splitlines():
        if line.startswith("data:"):
            return json.loads(line[5:].strip())["result"]
    return json.loads(r.text)["result"]


def _result_text_dict(res: dict) -> str:
    return "\n".join(b.get("text", "") for b in res.get("content", []) if isinstance(b, dict))


async def _test_body_cap_413():
    """A POST just over the configured cap gets a clean 413 from the SDK middleware;
    an under-cap POST is not 413'd.

    Run on a THROWAWAY MCPServer with a tiny cap so the probe body stays small and the
    413 is read cleanly (the production 8 MiB value is asserted, unit-level, in
    verify_mcp_transport). This proves the SAME RequestBodyLimitMiddleware the real
    server enables returns 413 -- not a hang or a crash -- over a real listener."""
    cap = 2048
    probe = MCPServer("cap-probe")
    sock, _port = _bind_free()
    srv = _ServerThread(probe.streamable_http_app(max_request_body_size=cap), sock)
    url = "http://127.0.0.1:%d/mcp" % srv.port  # noqa: UP031
    try:
        srv.start()  # inside the try so a start failure still closes the socket
        async with httpx.AsyncClient(timeout=10.0) as client:
            # OVER the cap -> 413 (rejected on Content-Length, before the MCP layer).
            over = await client.post(
                url, content=b"x" * (cap + 512), headers={"content-type": "application/json"}
            )
            assert over.status_code == 413, (
                "an over-cap POST returned %d, not a clean 413 -- the body-limit "  # noqa: UP031
                "middleware is not active over the wire" % over.status_code
            )
            # UNDER the cap -> passes the middleware to the MCP layer, which refuses
            # a non-MCP body with some OTHER 4xx. The point is only: NOT 413.
            under = await client.post(
                url, content=b"{}", headers={"content-type": "application/json"}
            )
            assert under.status_code != 413, (
                "an under-cap POST was 413'd (%d) -- the cap is being applied to "  # noqa: UP031
                "bodies within the limit" % under.status_code
            )
    finally:
        srv.stop()
    print(
        "  [body-cap] over-cap POST -> clean 413, under-cap POST not 413'd, over a real listener OK"
    )


def _initialize_session(url: str) -> tuple[int, str | None]:
    """One token-free `initialize` over the wire; returns (status, mcp-session-id or None).

    No auth header on purpose -- `initialize` carries no Localization OS token by design, which
    is exactly why an unbounded session count is a denial-of-service surface (issue #387)."""
    body = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": "2025-06-18",
            "capabilities": {},
            "clientInfo": {"name": "session-bounds-probe", "version": "1"},
        },
    }
    with httpx.Client(timeout=_CALL_TIMEOUT) as c:
        r = c.post(
            url,
            content=json.dumps(body),
            headers={
                "content-type": "application/json",
                "accept": "application/json, text/event-stream",
            },
        )
    return r.status_code, r.headers.get("mcp-session-id")


def _post_for_session(url: str, session_id: str, body: dict) -> int:
    """One POST on an EXISTING session id; returns the HTTP status (the body is not read)."""
    with httpx.Client(timeout=_CALL_TIMEOUT) as c:
        r = c.post(
            url,
            content=json.dumps(body),
            headers={
                "content-type": "application/json",
                "accept": "application/json, text/event-stream",
                "mcp-session-id": session_id,
                "mcp-protocol-version": "2025-06-18",
            },
        )
    return r.status_code


_IDLE_PROBE_SECONDS = 10


def _test_session_bounds_on_a_real_listener():
    """#387 over a REAL listener: past the cap an initialize is refused with 503 and nothing is
    allocated; an idle session gives its slot back to the next initialize.

    Stood up through the PRODUCTION seam -- `mcp_server._LocMCPServer(...).streamable_http_app()`
    with NO session kwargs, so the values come from `_LocMCPServer._listener_defaults`, the same
    path main() takes -- with only the two constants lowered so one slot is enough to probe (the
    shipped values are pinned in verify_mcp_transport). Nothing here mocks the check: the refusal
    is the SDK's own admission test and the release is its own idle reaper, both over a socket.
    """
    saved = (mcp_server._MAX_SESSIONS, mcp_server._SESSION_IDLE_TIMEOUT)
    mcp_server._MAX_SESSIONS = 1
    # The SDK requires a positive, finite timeout. It must comfortably exceed the wall time of the
    # three sequential HTTP round trips (each a fresh httpx client) that run BEFORE the reap is
    # awaited: at 1 s a loaded box reaped the session mid-probe, so the cap check saw 200 instead
    # of 503 and tools/list saw 404 instead of 200 (flake, not a cap bug). The reap wait below
    # polls up to 60 s, so a longer timeout only costs a few seconds.
    mcp_server._SESSION_IDLE_TIMEOUT = _IDLE_PROBE_SECONDS
    probe = mcp_server._LocMCPServer("session-bounds-probe")
    sock, _port = _bind_free()
    srv = _ServerThread(probe.streamable_http_app(), sock)
    url = "http://127.0.0.1:%d/mcp" % srv.port  # noqa: UP031
    try:
        srv.start()
        status, sid = _initialize_session(url)
        assert status == 200 and sid, f"the first initialize got {status}, session {sid!r}"
        noted = _post_for_session(
            url, sid, {"jsonrpc": "2.0", "method": "notifications/initialized"}
        )
        assert noted == 202, "the initialized notification was not accepted on the live session"
        # The cap is now reached: a SECOND initialize is refused at admission.
        status2, sid2 = _initialize_session(url)
        assert status2 == 503, (
            f"an initialize over the session cap returned {status2}, not 503 -- the cap is "
            "not reaching the listener, so session state stays unbounded (#387)"
        )
        assert sid2 is None, (
            f"a refused initialize still came back with a session id ({sid2!r}) -- the cap is "
            "checked after the session is allocated, which is the growth this fix forbids"
        )
        # The session that IS open keeps working; a refusal costs it nothing.
        held = _post_for_session(
            url, sid, {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}}
        )
        assert held == 200, f"the open session answered {held} to tools/list, not 200"
        # Idle release. Probe with NEW initializations only -- touching the open session would
        # reset its idle clock and the reap would never come. A 503 here means "still holding".
        # 30 s ceiling: the SDK fires the reap via an anyio CancelScope inside the transport's
        # connect loop; a loaded CI box can delay that scope's deadline by several seconds.
        deadline = time.time() + 60
        while True:
            status3, sid3 = _initialize_session(url)
            if status3 == 200:
                break
            if time.time() > deadline:
                raise AssertionError(
                    f"a new initialize still got {status3} sixty seconds after the "
                    f"{_IDLE_PROBE_SECONDS}-second idle timeout -- an idle session never "
                    "released its slot"
                )
            time.sleep(0.25)
        assert sid3 and sid3 != sid, f"the freed slot returned session {sid3!r}, not a new id"
        gone = _post_for_session(
            url, sid, {"jsonrpc": "2.0", "id": 3, "method": "tools/list", "params": {}}
        )
        assert gone == 404, f"the reaped session id answered {gone}, not 404"
    finally:
        (mcp_server._MAX_SESSIONS, mcp_server._SESSION_IDLE_TIMEOUT) = saved
        srv.stop()
    print(
        "  [sessions] over a real listener: 503 over the cap with nothing allocated, open "
        "session unaffected, idle session reaped (id 404) and its slot freed OK"
    )


def _test_initialization_admission_limit():
    """Anonymous bursts stop before shared capacity fills; headers cannot buy a bypass."""
    from unittest.mock import patch

    from starlette.testclient import TestClient

    probe = mcp_server._LocMCPServer("admission-probe")
    body = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": "2025-06-18",
            "capabilities": {},
            "clientInfo": {"name": "admission-probe", "version": "1"},
        },
    }
    headers = {"accept": "application/json, text/event-stream"}
    with patch.object(mcp_server, "_admission_now", return_value=100.0, create=True) as clock:
        app = probe.streamable_http_app(max_sessions=100, json_response=True)
        with TestClient(app, base_url="http://127.0.0.1:8000") as client:
            # Path noise (discovery probes, scanners) can never allocate a session, so it must
            # not spend the shared budget either (gate fix: it used to starve initialize).
            for noise in ("/.well-known/oauth-protected-resource", "/favicon.ico", "/x") * 7:
                assert client.get(noise).status_code == 404
            ids = []
            for _ in range(10):
                response = client.post("/mcp", json=body, headers=headers)
                assert response.status_code == 200, response.text
                ids.append(response.headers["mcp-session-id"])
            manager = probe._lowlevel_server._session_manager
            for i in range(30):
                # Fake credentials and rotating forwarded IPs must share the same budget.
                extra = (
                    {}
                    if i % 2 == 0
                    else {
                        "authorization": f"Bearer invented-{i}",
                        "x-loc-token": f"invented-{i}",
                        "x-forwarded-for": f"192.0.2.{i}",
                    }
                )
                response = client.post("/mcp", json=body, headers=headers | extra)
                assert response.status_code == 429, (
                    f"anonymous admission abuse got {response.status_code}, not 429"
                )
                assert response.headers["retry-after"] == "60"
                assert "mcp-session-id" not in response.headers
                assert len(manager._server_instances) == 10
            live_headers = headers | {
                "mcp-session-id": ids[0],
                "mcp-protocol-version": "2025-06-18",
            }
            assert (
                client.post(
                    "/mcp",
                    headers=live_headers,
                    json={"jsonrpc": "2.0", "method": "notifications/initialized"},
                ).status_code
                == 202
            )
            assert (
                client.post(
                    "/mcp",
                    headers=live_headers,
                    json={"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
                ).status_code
                == 200
            )
            # An invented session ID cannot allocate state through the exempt path.
            response = client.post(
                "/mcp", json=body, headers=headers | {"mcp-session-id": "invented-session"}
            )
            assert response.status_code == 404
            assert len(manager._server_instances) == 10
            # GET also reaches the SDK's new-session path; do not admit it for free.
            assert client.get("/mcp", headers=headers).status_code == 429
            # The modern wire is sessionless, so it needs no admission slot.
            modern_headers = headers | {
                "mcp-protocol-version": "2026-07-28",
                "mcp-method": "tools/list",
            }
            response = client.post(
                "/mcp",
                headers=modern_headers,
                json={
                    "jsonrpc": "2.0",
                    "id": 3,
                    "method": "tools/list",
                    "params": {
                        "_meta": {
                            "io.modelcontextprotocol/protocolVersion": "2026-07-28",
                            "io.modelcontextprotocol/clientCapabilities": {},
                            "io.modelcontextprotocol/clientInfo": {"name": "probe", "version": "1"},
                        }
                    },
                },
            )
            assert response.status_code == 200, response.text
            assert "mcp-session-id" not in response.headers
            # Forging the modern header on legacy initialize must not allocate either.
            response = client.post("/mcp", headers=modern_headers, json=body)
            assert response.status_code >= 400
            assert len(manager._server_instances) == 10
            clock.return_value = 160.0
            response = client.post("/mcp", json=body, headers=headers)
            assert response.status_code == 200, response.text
            assert len(manager._server_instances) == 11
            assert client.post("/mcp", json=body, headers=headers).status_code == 429
        # A listener served on a custom path meters THAT path (the path reaches the limiter).
        custom = mcp_server._LocMCPServer("admission-probe-path").streamable_http_app(
            streamable_http_path="/custom", json_response=True
        )
        with TestClient(custom, base_url="http://127.0.0.1:8000") as client:
            codes = [
                client.post("/custom", json=body, headers=headers).status_code for _ in range(11)
            ]
            assert codes == [200] * 10 + [429], codes
    print("  [admission] anonymous burst bounded before allocation, no header bypass, refill OK")


def _test_per_client_admission():
    """#410: actual listener allocation uses isolated, configured API-key budgets."""
    import hashlib
    import json
    from unittest.mock import patch

    from starlette.testclient import TestClient

    keys = ("client-a-fixture", "client-b-fixture")
    config = {
        hashlib.sha256(key.encode()).hexdigest(): {"burst": burst, "refill_per_minute": rate}
        for key, burst, rate in ((keys[0], 2, 2), (keys[1], 3, 1))
    }
    body = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": "2025-06-18",
            "capabilities": {},
            "clientInfo": {"name": "probe", "version": "1"},
        },
    }
    headers = {"accept": "application/json, text/event-stream"}
    with (
        patch.dict(os.environ, {"LOC_STUDIO_MCP_CLIENT_LIMITS": json.dumps(config)}),
        patch.object(mcp_server, "_admission_now", return_value=100.0) as clock,
    ):
        probe = mcp_server._LocMCPServer("client-admission")
        app = probe.streamable_http_app(json_response=True)
        with TestClient(app, base_url="http://127.0.0.1:8000") as client:

            def opening(extra=None):
                return client.post("/mcp", json=body, headers=headers | (extra or {}))

            a = {"authorization": "Bearer " + keys[0]}
            b = {"x-loc-token": keys[1]}
            assert [opening(a).status_code for _ in range(3)] == [200, 200, 429]
            # Both header spellings use the same bucket; bearer beats the alias.
            assert opening({"x-loc-token": keys[0]}).status_code == 429
            assert opening(a | b).status_code == 429
            assert [opening(b).status_code for _ in range(4)] == [200, 200, 200, 429]
            for i in range(10):
                assert opening({"authorization": f"Bearer unknown-{i}"}).status_code == 200
            assert opening().status_code == 429
            assert opening({"x-client-id": "invented"}).status_code == 429
            assert len(probe._lowlevel_server.session_manager._server_instances) == 15
            assert opening(a).headers["retry-after"] == "30"
            clock.return_value = 129.0
            assert opening(a).headers["retry-after"] == "1"
            clock.return_value = 130.0
            assert opening(a).status_code == 200
            assert opening(a).status_code == 429
            assert opening(b).status_code == 429
            assert opening().status_code == 429
            clock.return_value = 160.0
            assert opening(b).status_code == 200
            assert opening().status_code == 200
    print("  [client-admission] isolated limits, aliases, shared fallback, refill OK")


def main() -> None:
    _test_per_client_admission()
    _test_initialization_admission_limit()
    sock, _port = _bind_free()
    server = _ServerThread(mcp_server.mcp.streamable_http_app(), sock)
    _bearer_url.value = "http://127.0.0.1:%d/mcp" % server.port  # noqa: UP031
    try:
        # start() INSIDE the try so a start failure still reaches server.stop(),
        # which closes the pre-bound socket (otherwise it would leak until exit).
        server.start()
        asyncio.run(_test_bearer_over_the_wire())
        asyncio.run(_test_concurrent_isolation())
        asyncio.run(_test_missing_token_clean_error())
        asyncio.run(_test_expected_refusals_keep_their_text())
    finally:
        server.stop()
    # The 413 probe uses its own throwaway listener (tiny cap); run it after the
    # bearer listener is down so there is only ever one server thread at a time.
    asyncio.run(_test_body_cap_413())
    # The session-bound probe is another throwaway listener (cap of 1, 1 s idle timeout), so the
    # probe is bounded by its own deadline instead of by the shipped values.
    _test_session_bounds_on_a_real_listener()
    print("MCP CONNECTOR OK")


if __name__ == "__main__":
    main()
