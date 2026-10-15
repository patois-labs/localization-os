"""MCP wave 3 -- the --transport streamable-http packaging flag.

docs/specs/mcp-server.md wave 3 + section 11 (SDK / transport / deployment). Wave 3
adds ONE runtime capability to the server: the same registry, handlers and
env-token /api model can be served over streamable-HTTP instead of stdio, for an
agent runtime the operator controls behind their own proxy. This suite pins the
WIRING of that flag -- the entry-mode selection, the safe bind defaults, and the
DNS-rebinding posture -- without booting a real listener (the socket path is
smoke-tested by hand; verify_mcp / verify_mcp_tools cover the tools themselves).

WHAT IT ASSERTS:

  sdk pin         mcp is on the 2.x line at >=2.2,<3 (the MCPServer port).
  body cap        the streamable-http request-body cap is set EXPLICITLY (8 MiB,
                  above the SDK's 4 MiB default but capped to limit the pre-auth
                  memory amplifier) and reaches every listener build (mcp 2 takes it
                  at run()/app time).
                  The over-the-wire 413 is in verify_mcp_connector.
  session bounds  mcp 2's session cap and idle timeout are ON and FINITE (issue #387:
                  `initialize` carries no Localization OS key, so session state is
                  unauthenticated growth; an unbounded listener let a passer-by
                  accumulate sessions until restart). The values, that they reach every
                  listener build, and that the env overrides are bounded. The over-the-wire
                  refuse-and-release is in verify_mcp_connector.
  argparse        default transport is stdio; streamable-http parses; an unknown
                  transport (e.g. the sse the SDK also has but this server does
                  NOT expose) is REFUSED, not silently accepted.
  safe defaults   --host defaults to 127.0.0.1 (loopback) and --port to a value
                  that is NOT 8000 -- the app itself serves 8000, so a shared
                  default would collide the instant both run on one machine.
  dispatch        main() routes to mcp.run() for stdio and
                  mcp.run(transport="streamable-http") for the flag, and applies
                  the host/port/transport_security to mcp.run() as kwargs.
  rebinding       a localhost bind keeps the SDK's localhost allowlist (so the
                  listener still validates the Host header); a NON-localhost bind
                  clears it -- matching the SDK's own default, so the proxy in
                  front becomes the security boundary instead of a stale
                  127.0.0.1 allowlist rejecting every proxied request.
  scope report    the startup scope report prints to STDERR in BOTH modes (stdout
                  is stdio's JSON-RPC channel and must carry nothing else), and
                  the HTTP mode additionally names the endpoint + the no-listener-
                  auth caveat.

The suite imports server.py, which needs the `mcp` SDK. Without it the server
cannot be imported at all, so -- like verify_mcp_tools, and unlike the protocol
suite verify_mcp -- this FAILS LOUDLY rather than green-skipping: a cheerful skip
would assert the transport contract zero times behind a success line.
"""

import asyncio
import contextlib
import io
import os
import sys

import httpx

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
            "  FATAL: the mcp SDK is not installed, so the transport-wiring "
            "suite cannot import server.py. This suite MUST NOT be skipped -- a "
            "green skip would assert the --transport contract zero times behind "
            "a success message. Install the mcp SDK.",
            file=sys.stderr,
        )
        raise SystemExit(1) from exc
    raise


class _FakeRun:
    """Stand-in for MCPServer.run that records how it was called and never blocks.

    main() ends in a blocking mcp.run(); this captures the transport and the listener
    kwargs it was handed (mcp 2 takes host/port/path/transport_security/body cap as
    run() arguments) instead of serving forever."""

    def __init__(self):
        self.calls = []

    def __call__(self, transport=None, **kwargs):
        self.calls.append({"transport": transport, "kwargs": kwargs})


@contextlib.contextmanager
def _patched_run():
    """Run main() with mcp.run replaced, capturing stderr and the run() calls.

    _HTTP_BIND host/port/transport_security are saved and restored so one case's
    bind cannot leak into the next."""
    fake = _FakeRun()
    settings = mcp_server._HTTP_BIND
    saved = (mcp_server.mcp.run, settings.host, settings.port, settings.transport_security)
    mcp_server.mcp.run = fake
    err = io.StringIO()
    try:
        with contextlib.redirect_stderr(err):
            yield fake, err
    finally:
        (mcp_server.mcp.run, settings.host, settings.port, settings.transport_security) = saved


def test_sdk_on_supported_line():
    """The SDK window: mcp 2.x at >=2.2 and <3.

    The server targets the mcp 2 line (MCPServer, per-run listener kwargs, the
    streamable_http_client). >=2.2 is the version this port was built and verified
    on; <3 because another major is another migration. Asserting the window here
    turns 'somebody installed the wrong major' into a clear suite failure instead of
    an obscure runtime one."""
    import re
    from importlib.metadata import version

    raw = version("mcp")
    m = re.match(r"^(\d+)\.(\d+)", raw)
    assert m, (
        f"could not parse a major.minor from the installed mcp version {raw!r}; "
        "expected something like 2.2.0"
    )
    major, minor = int(m.group(1)), int(m.group(2))
    assert (major, minor) >= (2, 2), (
        f"mcp {raw} is below the required 2.2 floor; this server is ported to the mcp 2 "
        "API (MCPServer, run()-time listener kwargs)"
    )
    assert major < 3, (
        f"mcp {raw} is 3.x or newer - a major this server has not been ported to "
        "(see the requirements.txt pin note)."
    )
    print(f"  [sdk] mcp {raw} is on the supported line (>=2.2, <3) OK")


def test_body_cap_set_explicitly():
    """The streamable-http request-body cap is set EXPLICITLY (not riding the SDK
    default) and applied to every listener build.

    mcp 1.29+ has a 4 MiB default cap; this server sets 8 MiB deliberately: above 4 MiB
    because a real large single INLINE bulk import (import_keys/manage_tm/edit_glossary
    `data`) over remote HTTP exceeds it - the only bulk path a remote client has,
    since file_path reads the server's disk and is stdio-only - but capped at 8 MiB
    (not higher) because the body is buffered PRE-AUTH, so a bigger cap is a bigger
    unauthenticated memory amplifier (see server.py _MAX_REQUEST_BODY_SIZE for the
    measurement). The wire enforcement (a >cap POST -> 413 over a real listener) is
    proven in verify_mcp_connector."""
    cap = mcp_server._MAX_REQUEST_BODY_SIZE
    assert cap == 8 * 1024 * 1024, (
        "the body cap is %d bytes, expected 8 MiB - if this changed on purpose, "  # noqa: UP031
        "update the measurement note in server.py and this assertion together" % cap
    )
    # It must be ABOVE the SDK's 4 MiB default (the whole reason we set it) and a
    # positive int (StreamableHTTPSessionManager rejects <= 0 at run()).
    assert cap > 4 * 1024 * 1024, "the cap must exceed the 4 MiB SDK default"
    # And it must actually reach the listener: mcp 2 takes the cap as an argument to
    # streamable_http_app()/run(), so a direct `mcp.streamable_http_app()` (an embedder,
    # verify_mcp_connector) must still get 8 MiB, not the SDK's 4 MiB default. Capture
    # the kwargs the lowlevel app builder receives.
    seen = {}

    def _record(**kwargs):
        from starlette.applications import Starlette

        seen.update(kwargs)
        return Starlette()

    low = mcp_server.mcp._lowlevel_server
    saved = low.streamable_http_app
    low.streamable_http_app = _record
    try:
        mcp_server.mcp.streamable_http_app()
        default_cap = seen.get("max_request_body_size")
        seen.clear()
        mcp_server.mcp.streamable_http_app(max_request_body_size=1234)
        explicit_cap = seen.get("max_request_body_size")
    finally:
        low.streamable_http_app = saved
    assert default_cap == cap, (
        f"mcp.streamable_http_app() default body cap is {default_cap!r}, not {cap}; "
        "the _LocMCPServer default did not take effect"
    )
    assert explicit_cap == 1234, "an explicit max_request_body_size must still win"
    print(
        "  [body-cap] 8 MiB set explicitly (above the 4 MiB SDK default, capped to "
        "limit the pre-auth memory amplifier) and applied to every listener build OK"
    )


def test_session_bounds_finite_and_applied():
    """Issue #387: the session cap and idle timeout are ON and FINITE on every listener build.

    The streamable-http listener answers `initialize` WITHOUT a Localization OS credential --
    the per-request token is checked at tool/resource execution, and the SDK has no listener-
    level 401 short of OAuth (Phase C2). So every session, with its transport and its live
    server task, is allocated to a caller who has proved nothing. The first mcp-2 port passed
    `max_sessions=None` / `session_idle_timeout=None` to keep 1.x's never-reaped posture, which
    made retained session state unbounded: five token-free requests bought five sessions, and
    no number of them freed anything without a restart. Both bounds must therefore be finite
    numbers (never None), must be the ones server.py documents, and must reach the SDK manager
    however the listener is stood up: `streamable_http_app()` (an embedder,
    verify_mcp_connector) AND the `run(transport="streamable-http")` path main() takes. An
    embedder may still pass its own values; only the DEFAULTS are pinned here.
    """
    cap = mcp_server._MAX_SESSIONS
    idle = mcp_server._SESSION_IDLE_TIMEOUT
    assert isinstance(cap, int) and cap > 0, f"max_sessions must be a positive int, got {cap!r}"
    assert isinstance(idle, (int, float)) and idle > 0, (
        f"session_idle_timeout must be a positive number of seconds, got {idle!r}"
    )
    # The values themselves, so nobody quietly re-widens them (change together with the
    # measurement note on the constants in server.py).
    assert cap == 2000, f"the session cap is {cap!r}, not the documented 2000"
    assert idle == 4 * 60 * 60, f"the idle timeout is {idle!r}s, not the documented 4 h"

    # They must actually reach the listener. mcp 2 takes both as arguments to
    # streamable_http_app()/run(), so capture what the lowlevel app builder is handed.
    seen = {}

    def _record(**kwargs):
        from starlette.applications import Starlette

        seen.update(kwargs)
        return Starlette()

    low = mcp_server.mcp._lowlevel_server
    saved = low.streamable_http_app
    low.streamable_http_app = _record
    try:
        mcp_server.mcp.streamable_http_app()
        assert seen["max_sessions"] == cap and seen["session_idle_timeout"] == idle, seen
        seen.clear()
        mcp_server.mcp.streamable_http_app(max_sessions=7, session_idle_timeout=60.0)
        assert seen["max_sessions"] == 7 and seen["session_idle_timeout"] == 60.0, seen
    finally:
        low.streamable_http_app = saved

    # And through the run() path main() uses: _LocMCPServer.run_streamable_http_async must add
    # the same defaults before handing off to the SDK (the SDK's own default is 10 000 / 30 min;
    # ours is the documented posture). Patch the BASE implementation so no uvicorn is started.
    from mcp.server.mcpserver import MCPServer

    ran = {}

    async def _fake_run(self, **kwargs):
        ran.update(kwargs)

    saved_run = MCPServer.run_streamable_http_async
    MCPServer.run_streamable_http_async = _fake_run
    try:
        asyncio.run(mcp_server.mcp.run_streamable_http_async())
    finally:
        MCPServer.run_streamable_http_async = saved_run
    assert ran.get("max_sessions") == cap and ran.get("session_idle_timeout") == idle, ran
    print(
        f"  [sessions] cap {cap} / idle {idle}s, finite, applied to streamable_http_app() "
        "and to run(), explicit overrides still win OK"
    )


def test_session_bounds_env_config_is_bounded():
    """The operator can move the session bounds within a range -- and cannot switch them off.

    Drives the REAL parser (`_listener_int`) that produced the module constants, so an
    `LOC_STUDIO_MCP_MAX_SESSIONS=2ooo` in an env file fails startup naming the variable
    instead of quietly buying an unbounded listener. Nothing in the accepted set disables a
    bound: no zero, no negative, no sentinel."""
    name = "LOC_STUDIO_MCP_SESSION_IDLE_TIMEOUT"
    default, low, high = 4 * 60 * 60, 60, 24 * 60 * 60

    def parse(raw):
        saved = os.environ.get(name)
        os.environ[name] = raw
        try:
            return mcp_server._listener_int(name, default, low, high, "second")
        finally:
            if saved is None:
                del os.environ[name]
            else:
                os.environ[name] = saved

    assert parse("") == default, "an empty setting must fall back to the documented default"
    assert parse("900") == 900
    assert parse("  900  ") == 900, "surrounding whitespace (a paste from an env file) must parse"
    assert parse(str(low)) == low and parse(str(high)) == high, "the range is inclusive"
    for bad in ("0", "-1", "59", str(high + 1), "1.5", "abc", "unlimited", "none", "1e60"):
        try:
            parse(bad)
        except ValueError as exc:
            assert name in str(exc), f"the refusal did not name {name}: {exc}"
        else:  # pragma: no cover -- a regression lands here
            raise AssertionError(f"{name}={bad!r} was accepted; the bound is not fenced")
    # The cap is parsed by the same function with its own range (1..10000 sessions).
    cap_name = "LOC_STUDIO_MCP_MAX_SESSIONS"
    saved_cap = os.environ.get(cap_name)
    os.environ[cap_name] = "200"
    try:
        assert mcp_server._listener_int(cap_name, 2000, 1, 10_000, "session") == 200
    finally:
        if saved_cap is None:
            del os.environ[cap_name]
        else:
            os.environ[cap_name] = saved_cap
    print("  [sessions] env override parses, bad values refused by name, no off switch OK")


def test_argparse_defaults_and_choices():
    p = mcp_server._build_arg_parser()

    default = p.parse_args([])
    assert default.transport == "stdio", (
        f"default transport must be stdio, got {default.transport!r}"
    )
    assert default.host == "127.0.0.1", f"default bind must be loopback, got {default.host!r}"

    # The default port must not be the app's 8000 (a collision the instant both
    # processes run on one box). This is the load-bearing default, so assert it by
    # value, not just that it parses.
    assert default.port == mcp_server._DEFAULT_HTTP_PORT
    assert mcp_server._DEFAULT_HTTP_PORT != 8000, (
        "the MCP HTTP default port collides with the Localization OS app's 8000"
    )

    http = p.parse_args(["--transport", "streamable-http", "--host", "0.0.0.0", "--port", "9001"])
    assert http.transport == "streamable-http"
    assert http.host == "0.0.0.0" and http.port == 9001

    # An unknown transport is REFUSED. `sse` is a real MCPServer transport that this
    # server deliberately does not expose, so it is the honest thing to try.
    for bad in ("sse", "http", "tcp", ""):
        try:
            # argparse prints usage to stderr on a bad choice; swallow it so the
            # suite's own output stays clean.
            with contextlib.redirect_stderr(io.StringIO()):
                p.parse_args(["--transport", bad])
        except SystemExit:
            pass
        else:  # pragma: no cover -- a regression would land here
            raise AssertionError(
                f"--transport {bad!r} was accepted; the choices set does not fence the flag"
            )
    print("  [argparse] stdio default, loopback+non-8000 defaults, unknown transport refused OK")


def test_configure_http_bind_rebinding_posture():
    settings = mcp_server._HTTP_BIND
    saved = (settings.host, settings.port, settings.transport_security)
    try:
        # A localhost bind keeps the SDK's allowlist (the module-level server was
        # built with the default localhost host, so it is a real
        # TransportSecuritySettings, not None) -- the listener still validates Host.
        mcp_server._configure_http_bind("127.0.0.1", 8765)
        assert settings.host == "127.0.0.1" and settings.port == 8765
        assert settings.transport_security is not None, (
            "a localhost bind dropped the DNS-rebinding allowlist"
        )

        # A non-localhost bind CLEARS it -> protection off, proxy is the boundary
        # (exactly what the SDK does for a non-localhost host).
        mcp_server._configure_http_bind("0.0.0.0", 9002)
        assert settings.host == "0.0.0.0" and settings.port == 9002
        assert settings.transport_security is None, (
            "a non-localhost bind kept a localhost allowlist that would reject "
            "every proxied request under the real hostname"
        )

        for name in ("localhost", "::1"):
            mcp_server._configure_http_bind(name, 8765)
            assert settings.transport_security is not None, f"{name} should be treated as localhost"

        # NORMALIZATION: a padded or upper-cased loopback host still binds loopback
        # AND keeps the protected posture -- the S1 defect was that any of these
        # took the non-localhost branch and silently turned protection off.
        for raw, expect_host in (
            ("LOCALHOST", "localhost"),
            ("Localhost", "localhost"),
            (" 127.0.0.1", "127.0.0.1"),
            ("127.0.0.1 ", "127.0.0.1"),
            ("  ::1  ", "::1"),
        ):
            mcp_server._configure_http_bind(raw, 8765)
            assert settings.transport_security is not None, (
                f"{raw!r} bound loopback but lost DNS-rebinding protection"
            )
            assert settings.host == expect_host, (
                f"{raw!r} was stored as {settings.host!r}, not normalized to {expect_host!r}"
            )

        # An empty/whitespace host is a caller bug even at this level.
        for bad in ("", "   "):
            try:
                mcp_server._configure_http_bind(bad, 8765)
            except ValueError:
                pass
            else:  # pragma: no cover
                raise AssertionError(f"_configure_http_bind accepted empty host {bad!r}")
    finally:
        (settings.host, settings.port, settings.transport_security) = saved
    print(
        "  [bind] localhost keeps the allowlist, non-localhost clears it, "
        "padded/cased loopback normalizes + stays protected, empty refused OK"
    )


def test_port_range_and_empty_host_refused():
    p = mcp_server._build_arg_parser()

    # A valid port parses to an int.
    assert p.parse_args(["--port", "9000"]).port == 9000

    # Out-of-range / non-integer ports are refused at parse time.
    for bad in ("0", "-1", "65536", "70000", "abc"):
        try:
            with contextlib.redirect_stderr(io.StringIO()):
                p.parse_args(["--port", bad])
        except SystemExit:
            pass
        else:  # pragma: no cover
            raise AssertionError(f"--port {bad!r} was accepted; the range check is missing")

    # An empty/whitespace host is refused by main() (with the usage message)
    # before it can become an all-interfaces bind. mcp.run is patched so the
    # refusal, not a served socket, is what we observe.
    for bad in ("", "   "):
        with _patched_run() as (fake, _err):
            try:
                with contextlib.redirect_stderr(io.StringIO()):
                    mcp_server.main(["--transport", "streamable-http", "--host", bad])
            except SystemExit:
                pass
            else:  # pragma: no cover
                raise AssertionError(f"main() accepted an empty --host {bad!r}")
            assert fake.calls == [], f"main() reached mcp.run() with an empty host {bad!r}"
    print("  [validate] port range enforced, empty --host refused before serving OK")


def test_main_dispatches_stdio():
    with _patched_run() as (fake, err):
        mcp_server.main([])
    assert fake.calls == [{"transport": None, "kwargs": {}}], (
        f"stdio main() did not call mcp.run() with the default transport: {fake.calls!r}"
    )
    text = err.getvalue()
    # The scope report (a real line every run prints) must be on STDERR.
    assert "loc-studio MCP:" in text, "stdio mode printed no scope report to stderr"
    assert "transport: stdio" in text
    print("  [dispatch] stdio -> mcp.run(), scope report on stderr OK")


def test_main_dispatches_streamable_http():
    with _patched_run() as (fake, err):
        mcp_server.main(["--transport", "streamable-http", "--host", "127.0.0.1", "--port", "8793"])
        # Read the settings INSIDE the context: _patched_run restores host/port on
        # exit so one case's bind cannot leak into the next.
        applied_host = mcp_server._HTTP_BIND.host
        applied_port = mcp_server._HTTP_BIND.port
    assert len(fake.calls) == 1 and fake.calls[0]["transport"] == "streamable-http", (
        f"http main() did not call mcp.run(transport='streamable-http'): {fake.calls!r}"
    )
    kw = fake.calls[0]["kwargs"]
    assert kw["host"] == "127.0.0.1" and kw["port"] == 8793, f"listener kwargs wrong: {kw!r}"
    assert kw["streamable_http_path"] == "/mcp", kw
    assert kw["max_request_body_size"] == mcp_server._MAX_REQUEST_BODY_SIZE, kw
    assert kw["transport_security"] is not None, "a loopback bind lost its rebinding allowlist"
    # The bind was applied to the settings uvicorn actually reads.
    assert applied_host == "127.0.0.1"
    assert applied_port == 8793

    text = err.getvalue()
    assert "loc-studio MCP:" in text, "http mode printed no scope report to stderr"
    # The HTTP line names the endpoint and states the wave-4 per-request auth story
    # (the fact section 11 says is most likely to be misheard). Pre-wave-4 this
    # asserted "NO auth"; the listener now takes a per-request token, so the honest
    # caveat changed with it.
    assert "streamable-http" in text and ":8793/mcp" in text, (
        "http mode did not name its endpoint on stderr"
    )
    assert "per-request" in text and "Authorization: Bearer" in text, (
        "http mode did not state the per-request-token auth story"
    )
    assert "no listener-level OAuth 401" in text, (
        "http mode did not state that there is still no listener-level OAuth 401"
    )
    # In HTTP mode the empty-env-token WARNING must NOT fire - tokens arrive per
    # request, so an empty env token is expected, not a misconfiguration.
    assert "WARNING: LOC_STUDIO_TOKEN is empty" not in text, (
        "http mode printed the empty-token WARNING that is only meaningful in stdio"
    )
    assert "tokens arrive PER REQUEST" in text, (
        "http scope report did not state that tokens arrive per request"
    )
    print(
        "  [dispatch] streamable-http -> mcp.run(transport=...), bind applied, "
        "endpoint + per-request-auth caveat + no empty-token WARNING on stderr OK"
    )


def test_run_signature_supports_streamable_http():
    """The transport string this server passes must be one MCPServer.run accepts.

    If a future SDK bump renamed the transport, main() would raise at run() time in
    production; asserting the literal against the SDK's own Literal catches it in
    the suite instead."""
    import inspect
    import typing

    sig = inspect.signature(mcp_server.mcp.run)
    ann = sig.parameters["transport"].annotation
    # The SDK uses `from __future__ import annotations`, so the annotation may be a
    # stringized Literal[...] rather than the evaluated object. Handle both.
    if isinstance(ann, str):
        ok = "streamable-http" in ann
        shown = ann
    else:
        allowed = set(typing.get_args(ann))
        ok = "streamable-http" in allowed
        shown = sorted(allowed)
    assert ok, f"the pinned mcp SDK's MCPServer.run no longer accepts 'streamable-http': {shown}"
    print("  [sdk] MCPServer.run accepts the 'streamable-http' transport OK")


# ===========================================================================
# WAVE 4 -- per-request bearer-token forwarding in streamable-http mode.
#
# These drive the REAL dispatch path (call_tool_unvalidated -> _dispatch ->
# handler -> _call -> _headers) with only ONE test double: a recorded httpx
# transport that captures the outbound X-Loc-Token instead of hitting a live app.
# The request context is set up EXACTLY as the streamable-http server sets it --
# the server's `_MCP_REQUEST` contextvar, with the Starlette-shaped request parked
# there by the request middleware -- so the isolation being asserted is the production
# mechanism, not a stand-in for it. No uvicorn, no network, no mock of our own code.
# ===========================================================================


class _FakeHeaders:
    """Case-insensitive header lookup, like Starlette's Headers (which is what
    request_context.request.headers is in HTTP mode)."""

    def __init__(self, mapping):
        self._d = {str(k).lower(): v for k, v in (mapping or {}).items()}

    def get(self, key, default=None):
        return self._d.get(str(key).lower(), default)


class _FakeHttpRequest:
    """The only shape _current_http_request() reads off request_context.request: a
    `.headers` attribute. A real Starlette Request has far more; this is the read
    surface the server actually touches."""

    def __init__(self, headers):
        self.headers = _FakeHeaders(headers)


class _Gate:
    """A single-event-loop barrier for N coroutines (asyncio is single-threaded, so
    no lock is needed). Every waiter blocks until the Nth arrives -- used to hold two
    /api calls IN FLIGHT AT ONCE so the concurrency assertion cannot be satisfied by
    accidental serialization."""

    def __init__(self, n):
        self._n = n
        self._count = 0
        self._event = asyncio.Event()

    async def wait(self):
        self._count += 1
        if self._count >= self._n:
            self._event.set()
        await self._event.wait()


def _make_handler(recorded, gate=None):
    """A recorded httpx transport handler: capture the outbound X-Loc-Token, then
    (optionally) hold at the gate before answering GET /api/languages canned."""

    async def handler(request):
        recorded.append(request.headers.get("x-loc-token"))
        if gate is not None:
            await gate.wait()
        # A body that satisfies both list_languages (language_sets/locales) and the
        # guide resource (text), so one handler serves tool AND resource tests.
        return httpx.Response(200, json={"language_sets": [], "locales": [], "text": "ok"})

    return handler


@contextlib.contextmanager
def _recorder_and_env(recorded, env_token, gate=None):
    """Point mcp_server._client at a recorded MockTransport and pin the env TOKEN,
    restoring both afterwards (so no case leaks its transport or token into the
    next)."""
    saved_client = mcp_server._client
    saved_token = mcp_server.TOKEN
    handler = _make_handler(recorded, gate)

    def _client():
        return httpx.AsyncClient(
            transport=httpx.MockTransport(handler), timeout=mcp_server._HTTP_TIMEOUT
        )

    mcp_server._client = _client
    mcp_server.TOKEN = env_token
    try:
        yield
    finally:
        mcp_server._client = saved_client
        mcp_server.TOKEN = saved_token


@contextlib.contextmanager
def _http_request(headers):
    """Park a request carrying `headers` where the server's request middleware parks the
    transport's request (mcp 2 gives handlers no ambient request_ctx). Reset on exit."""
    req = _FakeHttpRequest(headers)
    token = mcp_server._MCP_REQUEST.set(req)
    try:
        yield
    finally:
        mcp_server._MCP_REQUEST.reset(token)


async def _drive(headers, env_token, recorded=None):
    """Run list_languages once under an HTTP request carrying `headers`, against a
    recorded transport with `env_token` as the env fallback. Returns the recorded
    outbound token list."""
    recorded = [] if recorded is None else recorded
    with _recorder_and_env(recorded, env_token), _http_request(headers):
        await mcp_server.call_tool_unvalidated("list_languages", {})
    return recorded


async def _test_header_beats_env_async():
    # Authorization: Bearer wins over a set env token.
    rec = await _drive({"authorization": "Bearer lsu_HEADER"}, "lsu_ENV")
    assert rec == ["lsu_HEADER"], f"header token was not forwarded / did not beat env: {rec!r}"
    # The X-Loc-Token alias is honoured too.
    rec = await _drive({"x-loc-token": "lsu_ALIAS"}, "lsu_ENV")
    assert rec == ["lsu_ALIAS"], f"X-Loc-Token alias not forwarded: {rec!r}"
    # Authorization beats the alias when both are present.
    rec = await _drive({"authorization": "Bearer lsu_A", "x-loc-token": "lsu_B"}, "lsu_ENV")
    assert rec == ["lsu_A"], f"Authorization did not win over X-Loc-Token: {rec!r}"
    print("  [forward] header token forwarded, beats env, X-Loc-Token alias, Authorization wins OK")


async def _test_env_fallback_async():
    from mcp.server.mcpserver.exceptions import ResourceError
    from server import ToolError

    for headers in ({}, {"authorization": "Bearer   "}, {"x-loc-token": "  "}):
        for action in ("tool", "guide", "openapi"):
            rec = []
            with _recorder_and_env(rec, "lsu_ENV"), _http_request(headers):
                try:
                    if action == "tool":
                        await mcp_server.call_tool_unvalidated("list_languages", {})
                    elif action == "guide":
                        await mcp_server.resource_guide_llm()
                    else:
                        await mcp_server.resource_openapi()
                except (ToolError, ResourceError) as exc:
                    # tools raise ToolError; resources re-raise it as the SDK's ResourceError
                    assert "No Localization OS token" in str(exc)
                else:
                    raise AssertionError("HTTP caller inherited the environment key")
            assert rec == [], rec
    print("  [auth] headerless tools and resources cannot inherit the environment key OK")


async def _test_stdio_uses_env_async():
    # No request context at all (stdio): the header machinery is inert and the env
    # token is used, byte-identical to pre-wave-4.
    rec = []
    with _recorder_and_env(rec, "lsu_STDIO"):
        # deliberately NO _http_request(): no request is parked
        await mcp_server.call_tool_unvalidated("list_languages", {})
    assert rec == ["lsu_STDIO"], f"stdio path did not use the env token: {rec!r}"
    print("  [stdio] no request context -> env token, header machinery inert OK")


async def _test_no_token_clean_error_async():
    from server import ToolError

    # HTTP request, no header, no env fallback -> clean per-request error naming the
    # header. NOT a traceback, and nothing forwarded.
    rec = []
    with _recorder_and_env(rec, ""), _http_request({"content-type": "application/json"}):
        try:
            await mcp_server.call_tool_unvalidated("list_languages", {})
        except ToolError as exc:
            msg = str(exc)
        else:  # pragma: no cover
            raise AssertionError("no-token HTTP request did not raise ToolError")
    assert "Authorization: Bearer" in msg and "per-user key" in msg, (
        f"HTTP no-token error did not name the header channel: {msg!r}"
    )
    assert rec == [], f"a no-token call still reached /api: {rec!r}"
    # stdio, no env token -> the env-oriented setup error (not the header one).
    with _recorder_and_env([], ""):
        try:
            await mcp_server.call_tool_unvalidated("list_languages", {})
        except ToolError as exc:
            smsg = str(exc)
        else:  # pragma: no cover
            raise AssertionError("no-token stdio call did not raise ToolError")
    assert "LOC_STUDIO_TOKEN" in smsg, (
        f"stdio no-token error did not name the env variable: {smsg!r}"
    )
    print("  [no-token] HTTP names the header, stdio names the env var, nothing forwarded OK")


async def _test_no_leakage_after_header_request_async():
    # Reset proof: a header-token request, THEN an env-only request in the SAME task.
    # If _dispatch failed to reset _REQUEST_TOKEN, the second call would still see
    # the first request's header token instead of the env token.
    rec = []
    with _recorder_and_env(rec, "lsu_ENV"):
        with _http_request({"authorization": "Bearer lsu_FIRST"}):
            await mcp_server.call_tool_unvalidated("list_languages", {})
        # No request context now -> must fall to env, proving the contextvar reset.
        await mcp_server.call_tool_unvalidated("list_languages", {})
    assert rec == ["lsu_FIRST", "lsu_ENV"], (
        f"contextvar leaked across calls (reset broken): {rec!r}"
    )
    print("  [reset] header token does not leak into the next env-only call OK")


async def _test_concurrent_isolated_tokens_async():
    # Two requests IN FLIGHT AT ONCE, each with its own header token, both held at a
    # gate inside the recorded transport so neither can finish before the other
    # starts. Each must forward ITS OWN token -- proving the per-request contextvar
    # is task-isolated under real concurrency, not merely across sequential calls.
    rec = []
    gate = _Gate(2)
    saved_client = mcp_server._client
    saved_token = mcp_server.TOKEN
    handler = _make_handler(rec, gate)

    def _client():
        return httpx.AsyncClient(
            transport=httpx.MockTransport(handler), timeout=mcp_server._HTTP_TIMEOUT
        )

    mcp_server._client = _client
    mcp_server.TOKEN = "lsu_ENV"  # neither request should ever use this

    async def one(tok):
        # _MCP_REQUEST.set INSIDE the task: asyncio copies the context at task
        # creation, so each task's request (and thus _REQUEST_TOKEN) is its own.
        with _http_request({"authorization": "Bearer " + tok}):
            await mcp_server.call_tool_unvalidated("list_languages", {})

    try:
        await asyncio.gather(one("lsu_ONE"), one("lsu_TWO"))
    finally:
        mcp_server._client = saved_client
        mcp_server.TOKEN = saved_token

    assert sorted(rec) == ["lsu_ONE", "lsu_TWO"], (
        f"concurrent requests did not each forward their own token (leakage): {rec!r}"
    )
    assert "lsu_ENV" not in rec, "a concurrent request fell back to the env token"
    print(
        "  [concurrency] two in-flight requests each forwarded their own token, "
        "no cross-request leak, no env fallback OK"
    )


async def _test_resource_forwarding_async():
    from mcp.server.mcpserver.exceptions import ResourceError
    from server import ToolError

    # RESOURCE reads freeze the per-request token too (they bypass _dispatch). With an
    # empty env token, a header-authed resource read must forward the HEADER token.
    rec = []
    with _recorder_and_env(rec, ""), _http_request({"authorization": "Bearer lsu_RES"}):
        out = await mcp_server.resource_guide_llm()
    assert rec == ["lsu_RES"], f"resource did not forward the header token: {rec!r}"
    assert out == "ok", f"resource body not returned: {out!r}"
    # A no-token resource read in HTTP mode names the HEADER channel (not the stdio
    # env error), and reaches /api zero times.
    rec = []
    with _recorder_and_env(rec, ""), _http_request({"content-type": "application/json"}):
        try:
            await mcp_server.resource_openapi()
        except ResourceError as exc:
            # mcp 2 only relays the text of its own ResourceError, so the resource
            # wrapper re-raises the ToolError as one (1.x relayed it as-is).
            msg = str(exc)
            assert msg.startswith("Error reading resource locstudio://openapi: "), msg
            assert isinstance(exc.__cause__, ToolError), exc.__cause__
        else:  # pragma: no cover
            raise AssertionError("no-token resource read did not raise ResourceError")
    assert "Authorization: Bearer" in msg, (
        f"resource no-token error did not name the header channel: {msg!r}"
    )
    assert rec == [], f"a no-token resource read still reached /api: {rec!r}"
    print(
        "  [resource] guide/openapi resources freeze + forward the per-request "
        "token, and name the header channel when it is missing OK"
    )


async def _test_bad_token_chars_async():
    from server import ToolError

    # A non-ASCII header token is refused cleanly (would otherwise crash httpx's
    # header encoder), and nothing is forwarded.
    rec = []
    with _recorder_and_env(rec, ""), _http_request({"authorization": "Bearer lsu_béad"}):
        try:
            await mcp_server.call_tool_unvalidated("list_languages", {})
        except ToolError as exc:
            msg = str(exc)
        else:  # pragma: no cover
            raise AssertionError("a non-ASCII token was accepted")
    assert "invalid characters" in msg, f"bad-char token error unclear: {msg!r}"
    assert rec == [], f"a bad-char token still reached /api: {rec!r}"
    # Embedded CR/LF are stripped (header-smuggling defense in depth); the cleaned
    # token is what gets forwarded.
    rec = []
    with _recorder_and_env(rec, ""), _http_request({"x-loc-token": "lsu_cl\r\nean"}):
        await mcp_server.call_tool_unvalidated("list_languages", {})
    assert rec == ["lsu_clean"], f"control chars not stripped from token: {rec!r}"
    print("  [hardening] non-ASCII token refused cleanly, CR/LF stripped OK")


async def _test_request_middleware_seam_async():
    """The seam the wire depends on: the middleware is registered on the live server,
    parks the SDK context's `.request` for exactly the handler call, and fails LOUD
    (not silently None -> env-token fallback) if the SDK context ever lacks `.request`."""
    assert mcp_server._bind_request_middleware in mcp_server.mcp.middleware, (
        "the request middleware is not registered: HTTP calls would see no request"
    )
    sentinel = _FakeHttpRequest({"authorization": "Bearer lsu_SEAM"})

    class _Ctx:
        request = sentinel

    async def _next(ctx):
        return mcp_server._current_http_request()

    seen = await mcp_server._bind_request_middleware(_Ctx(), _next)
    assert seen is sentinel, f"middleware did not park ctx.request: {seen!r}"
    assert mcp_server._MCP_REQUEST.get() is None, "parked request leaked past the call"

    class _NoRequest:
        pass

    try:
        await mcp_server._bind_request_middleware(_NoRequest(), _next)
    except AttributeError:
        pass
    else:
        raise AssertionError("a context without .request must fail loudly, not fall back to env")
    assert mcp_server._MCP_REQUEST.get() is None
    print("  [seam] request middleware registered, scoped to the call, fails loud on SDK drift OK")


def test_per_request_token_forwarding():
    asyncio.run(_test_request_middleware_seam_async())
    asyncio.run(_test_header_beats_env_async())
    asyncio.run(_test_env_fallback_async())
    asyncio.run(_test_stdio_uses_env_async())
    asyncio.run(_test_no_token_clean_error_async())
    asyncio.run(_test_no_leakage_after_header_request_async())
    asyncio.run(_test_concurrent_isolated_tokens_async())
    asyncio.run(_test_resource_forwarding_async())
    asyncio.run(_test_bad_token_chars_async())


def test_client_admission_config():
    """#410: operator mistakes fail closed before a listener is built."""
    import json
    from unittest.mock import patch

    key = "a" * 64
    valid = {key: {"burst": 2, "refill_per_minute": 3}}
    for value in (
        "not json",
        "[]",
        json.dumps({"raw-key": valid[key]}),
        json.dumps({key: {"burst": 0, "refill_per_minute": 1}}),
        json.dumps({key: {"burst": 1, "refill_per_minute": -1}}),
        json.dumps({key: {"burst": True, "refill_per_minute": 1}}),
        json.dumps({key: {"burst": 1, "refill_per_minute": 1.5}}),
        json.dumps({key: {"burst": 10001, "refill_per_minute": 1}}),
        json.dumps({key: {"burst": 1, "refill_per_minute": 10001}}),
        json.dumps({key: {"burst": 1}}),
        json.dumps({key: valid[key] | {"typo": 2}}),
        '{"' + key + '": {"burst": 1, "burst": 2, "refill_per_minute": 1}}',
        json.dumps({f"{i:064x}": valid[key] for i in range(1001)}),
    ):
        # Windows caps a real environment value at 32767 characters; exercise
        # the parser's entry bound without failing in os.environ.__setitem__.
        with patch.object(
            os, "environ", dict(os.environ) | {"LOC_STUDIO_MCP_CLIENT_LIMITS": value}
        ):
            try:
                mcp_server._LocMCPServer("invalid-config").streamable_http_app()
            except ValueError as exc:
                assert "LOC_STUDIO_MCP_CLIENT_LIMITS" in str(exc)
                assert "raw-key" not in str(exc)
            else:
                raise AssertionError("invalid per-client admission configuration accepted")
    with patch.dict(os.environ, {"LOC_STUDIO_MCP_CLIENT_LIMITS": json.dumps(valid)}):
        mcp_server._LocMCPServer("valid-config").streamable_http_app()


def test_client_admission_concurrency():
    import hashlib
    from unittest.mock import patch

    async def drive():
        admitted = []
        refused = []

        async def downstream(scope, receive, send):
            admitted.append(scope)
            await asyncio.sleep(0)

        async def send(message):
            if message["type"] == "http.response.start":
                refused.append(message["status"])

        fingerprint = hashlib.sha256(b"concurrent-fixture").hexdigest()
        limiter = mcp_server._SessionAdmissionLimit(
            downstream, client_limits={fingerprint: (20, 2)}
        )
        scope = {
            "type": "http",
            "path": "/mcp",
            "headers": [(b"authorization", b"Bearer concurrent-fixture")],
        }
        await asyncio.gather(*(limiter(scope, None, send) for _ in range(50)))
        assert len(admitted) == 20 and refused == [429] * 30
        assert len(limiter._clients) == 1

    with patch.object(mcp_server, "_admission_now", return_value=100.0):
        asyncio.run(drive())


def test_client_limit_cli_matches_listener():
    """#410 CLI parity: `mcp client-limit` prints an entry the listener accepts, rejects what
    the listener rejects, and never echoes the raw key."""
    import contextlib
    import hashlib
    import io
    import json
    from unittest.mock import patch

    from app.cli.__main__ import main as cli_main

    def run(*argv, env=None):
        out, err = io.StringIO(), io.StringIO()
        with (
            patch.dict(os.environ, env or {}),
            contextlib.redirect_stdout(out),
            contextlib.redirect_stderr(err),
        ):
            code = cli_main(list(argv))
        return code, out.getvalue() + err.getvalue()

    secret = "lsu_cli-limit-fixture-key"
    fp = hashlib.sha256(secret.encode()).hexdigest()
    code, out = run(
        "--json", "mcp", "client-limit", "--key-env", "CLI_LIMIT_KEY",
        "--burst", "20", "--refill-per-minute", "2", env={"CLI_LIMIT_KEY": secret},
    )  # fmt: skip
    assert code == 0 and secret not in out, out
    payload = json.loads(out)
    assert payload["fingerprint"] == fp and payload["entry"] == {
        "burst": 20,
        "refill_per_minute": 2,
    }
    with patch.dict(os.environ, {"LOC_STUDIO_MCP_CLIENT_LIMITS": json.dumps(payload["map"])}):
        assert mcp_server._client_admission_limits() == {fp: (20, 2)}
    code, out = run(
        "mcp", "client-limit", "--fingerprint", fp, "--burst", "1", "--refill-per-minute", "1"
    )
    assert code == 0 and fp in out and secret not in out
    for argv in (
        ("--fingerprint", fp, "--burst", "0", "--refill-per-minute", "1"),
        ("--fingerprint", fp, "--burst", "1", "--refill-per-minute", "10001"),
        ("--fingerprint", "ABC", "--burst", "1", "--refill-per-minute", "1"),
        ("--key-env", "CLI_LIMIT_UNSET_VAR", "--burst", "1", "--refill-per-minute", "1"),
    ):
        code, out = run("mcp", "client-limit", *argv)
        assert code != 0, argv


def main() -> None:
    test_client_admission_config()
    test_client_admission_concurrency()
    test_client_limit_cli_matches_listener()
    test_sdk_on_supported_line()
    test_body_cap_set_explicitly()
    test_session_bounds_finite_and_applied()
    test_session_bounds_env_config_is_bounded()
    test_argparse_defaults_and_choices()
    test_configure_http_bind_rebinding_posture()
    test_port_range_and_empty_host_refused()
    test_main_dispatches_stdio()
    test_main_dispatches_streamable_http()
    test_run_signature_supports_streamable_http()
    test_per_request_token_forwarding()
    print("MCP TRANSPORT OK")


if __name__ == "__main__":
    main()
