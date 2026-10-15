"""Localization OS - the MCP fourth surface (docs/specs/mcp-server.md, wave 1).

A standalone stdio MCP server that lets an agent drive Localization OS in plain
English. It is a THIN HTTP CLIENT of the Localization OS /api surface: it holds no
translation logic, no scoping filter, no RBAC decision, and imports NOTHING from
the `app/` package (the app never imports `mcp` either). It talks to a running
install over HTTP with the `X-Loc-Token` header carrying a per-user `lsu_` key.

WHAT CHANGED IN WAVE 1: this file is now a DISPATCHER, not five hand-written
tools. Every tool is built from `registry.py` -- its name, its summary and its
JSON-Schema input -- and bound to a handler function of the same name in
HANDLERS. Adding a tool is one `Tool(...)` entry plus one handler; there is no
third place to edit, and `tests/verify_mcp_parity.py` fails if the two halves
disagree in either direction.

WAVE 2 added breadth: families E-I (workflows/runs, language assets + TM,
the operational reads), plus workflow authoring and the
R1a/R1b content-parity tools plus the Wave R4 connector tools plus the Wave R2
admin & identity tools (manage_users, manage_admin_ops), plus the Wave 3
collaboration tools (queries, reviewer worklist, reviewer LQA findings),
so the open core's tools of the
spec's section-3 table ship (the paid add-ons carry their own). Two mechanics arrived with
them and are worth knowing before editing:

  * SLOW CALLS. Workflow tools bound launch plus polling to 45 seconds and may
    return running with done:false and a work_status instruction. Synchronous
    launches use _SLOW_TIMEOUT, under the ~4-minute Claude Desktop kill.
  * PLAN-MODE POSTS. _PLAN_SAFE_REQUESTS whitelists the only non-GET requests plan
    mode may send (empty in the open core), the request may not carry confirm, and
    verify_mcp_parity pins the whitelist to SPEND_ROUTES so it can never grow a
    plain write.

THE SPEND CONTRACT, IN ONE PLACE. Maintainer decision 5: every spend-bearing tool is
two-phase. `_dispatch` is the only function that knows it -- a tier="spend" tool
called without `confirm=true` NEVER reaches its handler; it gets `_plan`, which
performs READ calls only and returns the engine, the scope, the size and the
budget state so the agent can tell the user what it is about to spend. A
contextvar makes that structural rather than careful: while planning, any non-GET
request raises. The handlers of spend tools re-assert `confirm` for the same
reason a seatbelt has a buckle.

Say the honest thing about `confirm`: an agent can set it itself. It is a
disclosure mechanism and a speed bump, not a security control. The real controls
are all server-side and already shipped (the mock-engine 409, department budgets,
spend_cap_usd, per-identity rate limits, the acknowledged-estimate 409, RBAC and
org pinning), and the irreversible verbs are absent from the tool list entirely.

Why this exact shape (the platform limits this design works around):
  * MCP tool calls never receive uploaded binaries - tools get model-written
    JSON only. Over stdio the server has REAL filesystem access, so the model
    passes a FILE PATH; the server reads the bytes off disk and POSTs them.
  * Claude Desktop silently kills a tool call at ~4 minutes. So NOTHING blocks:
    submit_translation kicks off in <2s and returns job ids; job_status is an
    async long-poll capped at ~45s per CALL and the model re-polls; the
    translation itself runs in Localization OS's background job engine.
  * A ~1MB tool-result cap (+33% for base64) makes returning file bytes in chat
    a dead end. get_result and export_keys WRITE to LOC_STUDIO_OUTPUT_DIR on the
    local disk and return the path - the bytes never touch the tool result. That is
    local stdio only: over streamable-http nothing is written to this host's disk and
    the file comes back inline, capped at 4 MiB (#518).

Config (environment variables):
  LOC_STUDIO_BASE_URL   base URL of the running Localization OS app
                        (default http://127.0.0.1:8000)
  LOC_STUDIO_TOKEN      a per-user API key ("lsu_...", mint one at /account).
                        Which SCOPES it needs depends on which tools you use:
                        `docs` for the document + project families, `keys` for the
                        key/Delta family, `api` for translate_text. REQUIRED for
                        stdio; without it every tool returns a clear setup error
                        instead of calling the API. In streamable-http mode it is
                        instead the FALLBACK: each request may carry its own token
                        in an `Authorization: Bearer` / `X-Loc-Token` header, and
                        the env token is used only when a request omits one.
  LOC_TMS_OUTPUT_DIR    directory get_result / export_keys write files into
                        (default: ./loc-studio-output next to this file; local
                        stdio only, streamable-http returns the file inline).
                        LOC_STUDIO_OUTPUT_DIR is the older name and still works;
                        when both are set LOC_TMS_OUTPUT_DIR wins
  LOC_STUDIO_INPUT_DIRS folders (os.pathsep-separated, absolute) a stdio tool's
                        file_path may point into (default: your Documents and
                        Downloads; the output dir is always allowed). Hidden files
                        and folders, extensions the API would not accept, and files
                        over LOC_STUDIO_MAX_INPUT_BYTES (default 100 MiB) are
                        refused before reading (#755, see loc_file_guard.py)
  LOC_STUDIO_MCP_HUMAN_CONFIRM
                        off (default) | ask | require. With ask/require a confirmed
                        spend call also asks the HUMAN through an MRTR elicitation
                        on 2026-07-28 clients that declare elicitation; require
                        refuses the spend on clients that cannot (#757)
  LOC_STUDIO_MCP_MAX_SESSIONS
                        streamable-http only: how many sessions may be open at once
                        (default 2000, allowed 1..10000). While that many are open, a
                        request that would open another gets 503 and nothing is
                        allocated -- the bound that keeps an unauthenticated caller
                        from growing session state forever (issue #387).
  LOC_STUDIO_MCP_SESSION_IDLE_TIMEOUT
                        streamable-http only: seconds a session with nothing in flight
                        survives before it is reaped and frees its slot (default
                        14400 = 4 h, allowed 60..86400). A reaped session id answers
                        404 and the client re-initializes.
  LOC_STUDIO_MCP_CLIENT_LIMITS
                        streamable-http only: JSON map of SHA-256 API-key fingerprints
                        to burst/refill_per_minute integers (1..10000, max 1000 keys).
                        Unlisted callers share 10 admissions, refilling one per minute.
                        Restart the listener to apply; see README for the JSON shape.
  LOC_STUDIO_MCP_TOOLSETS
                        comma list of toolsets to load (#751): a family name
                        (orientation, projects, documents, keys, workflows, assets,
                        memory, evaluations, ops) or connectors / admin; default all.
                        Tools outside it are neither listed nor callable. Over HTTP a
                        connection may narrow it with an `X-Loc-Toolsets` header or a
                        `?toolsets=` query on the MCP URL. An unknown name fails startup.
  LOC_STUDIO_MCP_SCOPE_FILTER
                        on (default) hides from tools/list every tool whose grant the
                        calling key lacks (its scopes come from GET /api/account; when
                        they cannot be read, only the api tools and whoami are listed);
                        off lists every tool, for admins and debugging.
  LOC_STUDIO_MCP_OAUTH_RESOURCE
                        streamable-http only, opt-in (#753): the public https URL
                        clients connect to (e.g. https://mcp.example.com/mcp). Set it
                        and the listener becomes an OAuth 2.1 resource server
                        (oauth_rs.py); unset (the default) changes nothing.
  LOC_STUDIO_MCP_OAUTH_ISSUER
                        the Localization OS app_base_url (the authorization server).
  LOC_STUDIO_MCP_RS_SECRET
                        the resource-server secret from the app's MCP OAuth page or
                        `mcp-oauth rs-secret`; used only for the token exchange.

TRANSPORTS (wave 3, remote auth in wave 4). stdio is the default and is what
Claude Desktop / Claude Code launch. `--transport streamable-http` serves the SAME
registry and the SAME handlers over HTTP, for an agent runtime the operator
controls (Claude Code, Codex, their own tooling). Three facts about it are
load-bearing and all are the SDK's real behaviour, not aspiration:

  * PER-REQUEST TOKEN (wave 4). In HTTP mode each incoming MCP request carries its
    OWN lsu_ key in its HTTP headers - `Authorization: Bearer lsu_...` (or the
    `X-Loc-Token: lsu_...` alias) - and that token, not the process env, is what
    authenticates THAT request's outbound /api calls. The token is captured into a
    contextvar in _dispatch (the same task-isolated pattern plan mode uses), so two
    concurrent requests can never read each other's key and a request never falls
    back mid-flight to the env token. LOC_STUDIO_TOKEN is stdio-only; HTTP requests
    without their own header are refused at tool/resource execution. THE SERVER
    VALIDATES NOTHING: it forwards whatever token arrived and relays the app's own
    401/403 honestly - no hashing, no lookup, zero credential logic duplicated here.
    A request with no token in either place gets a clean per-tool error naming what
    is missing. What wave 4 did NOT add: a listener-level OAuth/WWW-Authenticate
    401. The mcp SDK's server (MCPServer) only authenticates a listener
    via OAuth 2.1 bearer verification (a TokenVerifier), which is spec Phase C2, not
    v1; there is no clean shared-secret 401 hook that would still let a client
    `initialize` and list tools before authing, so the per-tool error is the honest
    v1 semantics. PHASE C2 (#753) is now an OPT-IN: with LOC_STUDIO_MCP_OAUTH_RESOURCE
    set (plus LOC_STUDIO_MCP_OAUTH_ISSUER and LOC_STUDIO_MCP_RS_SECRET) the listener is an
    OAuth 2.1 resource server (oauth_rs.py): RFC 9728 metadata, a listener-level 401 /
    403 insufficient_scope, audience-bound tokens exchanged at the app (never passed
    through to /api). Unset, everything below holds unchanged. Run HTTP mode behind
    TLS you control (Fly edge or your own proxy).
    PATH A (2026-08-06): the SERVER half of a claude.ai static-bearer connector is
    complete and verified end-to-end (verify_mcp_connector drives a REAL listener) -
    claude.ai's connector UI has a "custom headers" field for a fixed
    `Authorization: Bearer lsu_...`, exactly the header this dispatch reads. But the
    CLAUDE.AI half is UNPROVEN here: request-header auth is a GATED BETA ("contact
    Anthropic for early access") with an OPEN BUG (anthropics/claude-ai-mcp#644, filed
    2026-07-19, still open; related #110/#112/#10) in which claude.ai never sends the
    header and instead attempts an OAuth authorization_code flow using the header NAME
    as client_id. Nobody has connected claude.ai to THIS server yet. DEGRADED SIGNATURE
    if the header never arrives: initialize + tools/list answer ANONYMOUSLY (200 +
    session id + all 48 tool defs), so claude.ai ADDS the connector and DISPLAYS its
    tools; the failure only surfaces on the first tool CALL as "No Localization OS token on
    this request" - a header-not-reaching-the-server problem, not a broken server. The
    OAuth-DIRECTORY connector flow (DCR + PKCE + RFC 9728) stays Phase C2. See
    README.md "Connect from claude.ai".
  * BOUNDED SESSIONS (issue #387). Because `initialize` carries no key (the bullet above),
    every session is allocated to a caller who has proved nothing, so BOTH of mcp 2's
    bounds are ON: at most `LOC_STUDIO_MCP_MAX_SESSIONS` (default 2000) sessions are open
    at once -- past that, a request that would open one gets 503 and NOTHING is allocated,
    which is the pre-session admission control the SDK offers without OAuth -- and a
    session with nothing in flight is reaped after `LOC_STUDIO_MCP_SESSION_IDLE_TIMEOUT`
    (default 4 h), so abandoned sessions give their capacity back. The first mcp-2 port
    set both to None to keep 1.x behaviour; that is what this closes.
  * DEFAULT BIND IS LOOPBACK, ON A PORT THAT IS NOT THE APP'S. --host defaults to
    127.0.0.1 and --port to 8765 (deliberately not 8000, which the Localization OS app
    itself serves; a shared default would collide the moment both run on one box).
    A non-localhost --host clears FastMCP's localhost DNS-rebinding allowlist, the
    same posture FastMCP adopts when constructed with a non-localhost host - the
    TLS boundary in front becomes the security boundary.

Run: `python server.py` (stdio, the default) or
`python server.py --transport streamable-http [--host H] [--port P]`. The startup
scope report prints to STDERR in BOTH modes (stdout is stdio's protocol channel).
See README.md for the Claude Desktop, Claude Code and remote configs.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import contextlib
import contextvars
import functools
import hashlib
import inspect
import json
import logging
import math
import os
import re
import sys
import time
from pathlib import Path
from typing import Annotated, Any, Literal

import httpx
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ResourceError as _SdkResourceError
from mcp.server.mcpserver.exceptions import ToolError as _SdkToolError
from mcp.server.mcpserver.exceptions import UnexpectedToolError as _SdkUnexpectedToolError
from mcp.types import CallToolResult, TextContent, ToolAnnotations
from pydantic import Field, WrapValidator
from starlette.datastructures import Headers
from starlette.responses import PlainTextResponse


def _none_passthrough(v, handler):
    """Let an EXPLICIT null through as Python None, else validate normally.

    Part of the M1 fix (see _signature_from_schema): an optional plain-string
    parameter is annotated as exactly `str` so FastMCP's pre_parse_json cannot
    JSON-decode a JSON-shaped string value. But a bare `str` would then REFUSE an
    explicit `null` -- which LLM clients and schema-to-typed-object clients routinely
    send for an unset optional, and which `str | None` on main accepted. This wrap
    validator restores that: `null -> None`, everything else goes to the normal
    string validator (so `q=5` is still a string_type error). pydantic hoists this
    into FieldInfo.metadata, leaving FieldInfo.annotation exactly `str`, so the
    pre_parse_json skip is preserved (proven against mcp 1.28.1 and re-verified on
    1.29.0 - func_metadata.pre_parse_json is byte-identical across the bump)."""
    return None if v is None else handler(v)


# The registry sits next to this file and imports nothing (that is what lets the
# accounting guard load it in an SDK-less environment). It is the single source
# of truth for what tools exist, what they may call and what the model sees.
_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))
import external_content  # noqa: E402  (#748: marks customer/vendor text as data)
import loc_file_guard  # noqa: E402  (#755: which local files a stdio tool may read)
import loc_plan_binding  # noqa: E402  (#757: confirm bound to the plan the user saw)
import longrun  # noqa: E402
import oauth_rs  # noqa: E402
import registry  # noqa: E402
import tool_meta  # noqa: E402

# --- configuration ---------------------------------------------------------
BASE_URL = os.environ.get("LOC_STUDIO_BASE_URL", "http://127.0.0.1:8000").rstrip("/")
TOKEN = os.environ.get("LOC_STUDIO_TOKEN", "").strip()
_DEFAULT_OUT = Path(__file__).resolve().parent / "loc-studio-output"
# LOC_TMS_OUTPUT_DIR is the current name; LOC_STUDIO_OUTPUT_DIR is the older name and is still read.
OUTPUT_DIR = Path(
    os.environ.get("LOC_TMS_OUTPUT_DIR")
    or os.environ.get("LOC_STUDIO_OUTPUT_DIR")
    or str(_DEFAULT_OUT)
)

# Per-request HTTP timeout. Kept short - no tool call may approach the ~4-min
# Claude Desktop kill; the long work is the background job, polled separately.
_HTTP_TIMEOUT = httpx.Timeout(30.0, connect=10.0)

# The exception: synchronous launches. 210s keeps a real LLM run
# alive while staying comfortably under the ~4-min client kill; if a run is
# slower than this, the honest failure is the timeout message naming the tool,
# not a longer wait the client would kill silently anyway.
_SLOW_TIMEOUT = httpx.Timeout(210.0, connect=10.0)
_WORKFLOW_WAIT_S = 45.0

# job_status long-poll shape: total budget per CALL and the gap between polls.
# 45s is comfortably under the ~4-min kill; the model re-invokes if still running.
_POLL_BUDGET_S = 45.0
_POLL_GAP_S = 2.5

_TERMINAL = {"completed", "failed"}

#: #752: the guide pointer used to be appended to all 72 tools' descriptions (about
#: 9 K characters of repetition in every tools/list). It is now said ONCE, in the
#: server `instructions` (tool_meta.SERVER_INSTRUCTIONS), which also names the tool
#: route to the same card (whoami card="llm") for hosts that never read resources.

#: Maximum streamable-http POST body FastMCP accepts before returning a clean 413
#: (mcp>=1.29's RequestBodyLimitMiddleware; the SDK default is 4 MiB). We set it
#: EXPLICITLY - above the 4 MiB default, but deliberately NOT above 8 MiB.
#:
#: WHY ABOVE 4 MiB. Over remote HTTP there is NO shared filesystem, so the only way
#: to send a bulk import is INLINE in a tool argument (import_keys `data`, manage_tm
#: TMX `data`, edit_glossary `data`); the stdio file_path path reads the SERVER's
#: disk and is unavailable to a remote client. Measured wire size of the JSON-RPC
#: tools/call envelope (scripts/mcp_body_size.py) depends HEAVILY on key/value
#: shape, so the numbers below name their shape rather than pretending to one:
#:   import_keys, 30,000 keys:    ~1.3 MiB (short) .. ~2.8 MiB (medium) .. ~4.3 MiB (long)
#:   manage_tm,   20,000 TMX units: ~3.3 MiB (short seg) .. ~5.2 MiB (medium seg)
#: where short =~20-char key /~25-char value, medium =~38/48, long =~55/90. So a
#: real large single inline import can exceed the 4 MiB default; the largest
#: LEGITIMATE body measured is ~5.2 MiB (a 20k-unit TMX at medium segment length).
#:
#: WHY NOT ABOVE 8 MiB. The listener has NO auth of its own, so a POST body is
#: buffered into memory PRE-AUTH (RequestBodyLimitMiddleware reads up to the cap
#: before any token is checked). Adversarial measurement on the production listener
#: (probe_cap.py): 4 concurrent 15.9 MiB POSTs drove RSS 60.7 -> 184.7 MB (~2x the
#: body per request). On a 256 MB shared-cpu-1x Fly machine that is an OOM at ~4
#: concurrent unauthenticated requests (comfortable at 1-2 GB - but the point is the
#: pre-auth amplifier, not any one machine). 8 MiB HALVES it while still clearing the
#: ~5.2 MiB largest legitimate body with ~1.5x headroom.
#:
#: IF YOU HIT THE 8 MiB 413. Use a bulk path that does not inline the file: stdio
#: mode with a file_path (the server reads bytes off disk - no wire-size limit at
#: all), or chunk/page the import (import_keys is merge-only, so repeated
#: namespace- or locale-sliced imports compose). See README "Request-body cap".
#:
#: verify_mcp_transport asserts this value; verify_mcp_connector proves an over-cap
#: POST gets a clean 413 over a real listener.
_MAX_REQUEST_BODY_SIZE = 8 * 1024 * 1024  # 8 MiB


def _listener_int(name: str, default: int, low: int, high: int, unit: str) -> int:
    """Read one bounded integer listener setting from the environment.

    Shared by the two session bounds below. Empty/unset -> the default. Anything else must be a
    whole number inside [low, high] or startup fails naming the variable and the allowed range:
    an operator typo (`MAX_SESSIONS=2ooo`, `=0`, `=unlimited`) must not silently buy an
    unbounded listener, and silently clamping would let a mistake read as a policy. The range is
    what makes the setting "bounded operator configuration" rather than an off switch: no value
    here turns either bound off."""
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        raise ValueError(f"{name} must be a whole number of {unit}s, got {raw!r}") from None
    if not low <= value <= high:
        raise ValueError(f"{name} must be between {low} and {high} {unit}s, got {value}")
    return value


#: Maximum open streamable-http sessions (mcp 2's admission control; over the cap a request that
#: would open a session gets 503, existing sessions unaffected).
#:
#: WHY FINITE (issue #387). `initialize` carries no Localization OS key - the tool-level per-request
#: token is what authenticates, and the SDK has no listener-level 401 hook short of OAuth (Phase
#: C2) - so EVERY session is allocated to a caller who has proved nothing. An unbounded count let a
#: passer-by accumulate sessions, each with its transport and live task, until the process restarts:
#: a denial of service with small requests and no account. 2000 is owner's suggested figure for
#: #369 ("low thousands"); a 256 MB box should run lower (`LOC_STUDIO_MCP_MAX_SESSIONS=200`).
#: The cap is checked BEFORE the transport exists, so a refused initialize allocates nothing.
_MAX_SESSIONS = _listener_int("LOC_STUDIO_MCP_MAX_SESSIONS", 2000, 1, 10_000, "session")

#: Seconds a session may go with no request in flight before the listener reaps it and frees its
#: slot (a reaped session id then answers 404 and the client re-initializes; spec behaviour every
#: real client handles). Same reason as the cap: reaping is what keeps the cap from being filled
#: permanently by abandoned sessions. 4 h is owner's "a few hours" for #369 - long enough that a
#: desktop client that idles overnight keeps its session.
_SESSION_IDLE_TIMEOUT = _listener_int(
    "LOC_STUDIO_MCP_SESSION_IDLE_TIMEOUT", 4 * 60 * 60, 60, 24 * 60 * 60, "second"
)


def _admission_now() -> float:
    return time.monotonic()


def _client_admission_limits() -> dict[str, tuple[int, int]]:
    """Read bounded per-key overrides at listener construction, never log their contents."""
    name = "LOC_STUDIO_MCP_CLIENT_LIMITS"
    raw = os.environ.get(name, "").strip()
    if not raw:
        return {}

    def invalid():
        return ValueError(
            f"{name} must be a JSON object of at most 1000 lowercase SHA-256 key fingerprints; "
            "each value must contain only burst and refill_per_minute integers in 1..10000"
        )

    def unique_object(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise invalid()
            result[key] = value
        return result

    try:
        value = json.loads(raw, object_pairs_hook=unique_object)
    except (ValueError, RecursionError):
        raise invalid() from None
    if not isinstance(value, dict) or len(value) > 1000:
        raise invalid()
    limits = {}
    for fingerprint, limit in value.items():
        if (
            not re.fullmatch(r"[0-9a-f]{64}", fingerprint)
            or not isinstance(limit, dict)
            or set(limit) != {"burst", "refill_per_minute"}
            or any(type(n) is not int or not 1 <= n <= 10000 for n in limit.values())
        ):
            raise invalid()
        limits[fingerprint] = (limit["burst"], limit["refill_per_minute"])
    return limits


class _SessionAdmissionLimit:
    """Bounded token buckets before SDK session allocation.

    Unlisted callers share 10 opening requests, then one per minute. Only an
    operator-listed key earns a separate budget. Check every HTTP
    method without a session ID: the SDK can allocate before validating the body.
    A supplied ID cannot allocate: the SDK serves it or returns 404. Existing
    sessions therefore remain usable even when opening requests are throttled.

    Only the MCP endpoint itself is metered (gate fix): any other path (an OAuth
    discovery probe, a favicon, an internet scanner's random URL) is a router 404
    that can never allocate a session, and metering it let path noise starve the
    shared budget so legitimate `initialize` calls got 429.
    """

    def __init__(self, app, path: str = "/mcp", client_limits=None):
        self.app = app
        self._path = path
        self._tokens = 10.0
        self._updated = _admission_now()
        self._limits = client_limits or {}
        self._clients = {
            key: (float(burst), self._updated) for key, (burst, _) in self._limits.items()
        }

    def _client_key(self, headers):
        auth = headers.get("authorization") or ""
        token = auth[7:].strip() if auth[:7].lower() == "bearer " else ""
        token = token or (headers.get("x-loc-token") or "").strip()
        try:
            token = _sanitize_token(token)
        except _SdkToolError:
            return None
        fingerprint = hashlib.sha256(token.encode("ascii")).hexdigest() if token else None
        return fingerprint if fingerprint in self._limits else None

    def _is_endpoint(self, scope) -> bool:
        path = scope.get("path", "")
        root = scope.get("root_path", "")
        if root and path.startswith(root):
            path = path[len(root) :]
        return path == self._path

    async def __call__(self, scope, receive, send):
        headers = Headers(scope=scope) if scope["type"] == "http" else None
        if (
            headers is not None
            and self._is_endpoint(scope)
            and headers.get("mcp-session-id") is None
            # This SDK wire path never allocates retained sessions. Keep modern
            # tool calls independent of the legacy initialization budget.
            and headers.get("mcp-protocol-version") != "2026-07-28"
        ):
            now = _admission_now()
            key = self._client_key(headers)
            burst, rate = self._limits[key] if key else (10, 1)
            tokens, updated = self._clients[key] if key else (self._tokens, self._updated)
            tokens = min(float(burst), tokens + (now - updated) * rate / 60.0)
            # No await between reading and spending: concurrent ASGI tasks cannot
            # overdraw the bucket. State grows only with operator configuration.
            admitted = tokens >= 1.0
            tokens -= int(admitted)
            if key:
                self._clients[key] = (tokens, now)
            else:
                self._tokens, self._updated = tokens, now
            if not admitted:
                retry = max(1, math.ceil((1.0 - tokens) * 60.0 / rate))
                await PlainTextResponse(
                    "Session admission rate exceeded; retry later.",
                    status_code=429,
                    headers={"Retry-After": str(retry)},
                )(scope, receive, send)
                return
        await self.app(scope, receive, send)


#: Test seam: an httpx transport for the OAuth token exchange (None = the network).
_OAUTH_TRANSPORT = None


def _oauth_scope_for_tool(name: str):
    """The grantable OAuth scope (api/docs/keys) a tool needs, or None (unknown tool, or an
    admin tool: no OAuth grant ever carries admin, so the app's own 403 answers it)."""
    spec = registry.tool_by_name(name)
    scope = getattr(spec, "scope", None)
    return scope if scope in oauth_rs.SCOPES else None


class _LocMCPServer(MCPServer):
    """MCPServer with the body cap and the session bounds applied wherever a listener is built.

    mcp 2 moved `max_request_body_size` (and host/port/path/transport_security) off the
    constructor and onto `run()` / `streamable_http_app()`, whose default is 4 MiB. The
    cap must hold for EVERY way of standing the listener up (main(), or an embedder or
    test serving `mcp.streamable_http_app()` directly), so the 8 MiB default lives here
    rather than at one call site."""

    @staticmethod
    def _listener_defaults(kwargs: dict) -> dict:
        kwargs.setdefault("max_request_body_size", _MAX_REQUEST_BODY_SIZE)
        # mcp 2 also started reaping idle sessions and capping open sessions. The first port of
        # this server disabled both to keep 1.x behaviour -- and that is the bug in issue #387:
        # `initialize` needs no Localization OS key, so an unauthenticated caller could open
        # sessions and keep their transports and live tasks forever. Both bounds are now finite
        # (see _MAX_SESSIONS / _SESSION_IDLE_TIMEOUT); passing a value still overrides, but the
        # defaults can no longer be None, and nothing here re-disables them.
        kwargs.setdefault("session_idle_timeout", _SESSION_IDLE_TIMEOUT)
        kwargs.setdefault("max_sessions", _MAX_SESSIONS)
        return kwargs

    def streamable_http_app(self, **kwargs):
        client_limits = _client_admission_limits()
        # #753: read (and validate) the opt-in OAuth resource-server config BEFORE building the
        # app, so a half-configured listener fails at startup instead of serving anonymously.
        oauth_cfg = oauth_rs.config_from_env(BASE_URL)
        app = super().streamable_http_app(**self._listener_defaults(kwargs))
        path = kwargs.get("streamable_http_path", "/mcp")
        app.add_middleware(
            _SessionAdmissionLimit,
            path=path,
            client_limits=client_limits,
        )
        if oauth_cfg is not None:
            # Added LAST, so it is the OUTERMOST layer: an unauthenticated request is answered
            # 401 before the admission budget is spent or any body is buffered (closes L3 of
            # the 2026-10-11 interop review for OAuth listeners).
            app.add_middleware(
                oauth_rs.OAuthResourceGate,
                cfg=oauth_cfg,
                mcp_path=path,
                scope_for_tool=_oauth_scope_for_tool,
                max_body=_MAX_REQUEST_BODY_SIZE,
                transport=_OAUTH_TRANSPORT,
            )
        return app

    async def run_streamable_http_async(self, **kwargs):
        return await super().run_streamable_http_async(**self._listener_defaults(kwargs))

    # #751: tools/list follows the key's scopes and the active toolsets, and a call to a
    # tool outside the active toolsets is refused. The logic lives in the TOOL FILTERING
    # section below register_tools(); these hooks are the only seam into the SDK. The
    # filter runs on the SDK's list, so #752's titles/annotations/outputSchema survive.
    async def list_tools(self):
        return await _filter_listed_tools(await super().list_tools())

    async def call_tool(self, name, arguments, context=None):
        """#756: every expected tool error leaves with a machine-readable JSON last line.

        The ONE place all tool failures pass (our handlers' ToolErrors, the SDK's
        argument-validation and unknown-tool ToolErrors), so the tail cannot be
        forgotten by a new raise site. A crash (UnexpectedToolError) keeps the SDK's
        withheld text and gets no tail: its message must not grow detail."""
        try:
            _check_toolset_call(name)  # #751: inside the try, so a refusal gets the tail
            return await super().call_tool(name, arguments, context)
        except _SdkUnexpectedToolError:
            raise
        except _SdkToolError as exc:
            if _ERROR_TAIL_MARK in str(exc):
                raise
            tailed = _SdkToolError(f"{exc}\n{_error_tail(name, exc)}")
            # Keep the SDK's own cause (a pydantic ValidationError is logged by field name).
            raise tailed from (exc.__cause__ or exc)


class _HttpBind:
    """What the streamable-http listener is told at run() time.

    mcp 1.x kept these on the live `FastMCP.settings`; mcp 2 takes them as arguments to
    `run()`, so main() reads them from here. `_configure_http_bind` owns the values."""

    host: str = "127.0.0.1"
    port: int = 8000
    streamable_http_path: str = "/mcp"
    transport_security = None  # None + non-localhost host = protection OFF; see below


_HTTP_BIND = _HttpBind()


def _server_version() -> str:
    """serverInfo.version, COARSENED to MAJOR.MINOR (#752).

    serverInfo is answered before any key is checked (initialize, server/discover, and
    the 2026-07-28 per-response `_meta`), so it follows the app's rule for keyless
    surfaces (app.release.coarse_version, audit 2026-09-05 L4): the exact patch release
    is a free fingerprint. The full release stays on the authenticated release_manifest
    tool. Read from the checkout this server ships in (RELEASE, else pyproject.toml);
    this module imports nothing from `app`."""
    root = Path(__file__).resolve().parents[2]
    raw = ""
    with contextlib.suppress(OSError):
        raw = (root / "RELEASE").read_text(encoding="utf-8").strip()
    if not raw:
        with contextlib.suppress(OSError):
            match = re.search(
                r'^version\s*=\s*"([^"]+)"',
                (root / "pyproject.toml").read_text(encoding="utf-8"),
                re.MULTILINE,
            )
            raw = match.group(1) if match else ""
    core = raw[1:] if raw[:1] in ("v", "V") else raw
    parts = core.split(".")
    if len(parts) >= 2 and all(p.isascii() and p.isdigit() for p in parts[:2]):
        return f"{parts[0]}.{parts[1]}"
    return "unknown"


mcp = _LocMCPServer(
    tool_meta.SERVER_NAME,
    title=tool_meta.SERVER_TITLE,
    instructions=tool_meta.SERVER_INSTRUCTIONS,
    website_url=tool_meta.SERVER_WEBSITE,
    version=_server_version(),
)

#: The ASGI request behind the MCP message being handled, set by the middleware below
#: for the whole handler call (None on stdio). mcp 2 removed the ambient
#: `request_ctx` / `get_context()` access v1 used, and a STATIC resource cannot take a
#: `Context` parameter, so a middleware around every inbound message is the one seam
#: that serves tools and resources alike.
_MCP_REQUEST: contextvars.ContextVar = contextvars.ContextVar("loc_mcp_http_request", default=None)
#: The ServerRequestContext of the message being handled (any transport), for #757.
_MCP_CALL: contextvars.ContextVar = contextvars.ContextVar("loc_mcp_call", default=None)


async def _bind_request_middleware(ctx, call_next):
    # `ctx.request`, NOT getattr(..., None): the SDK marks this middleware API
    # provisional, and a silent None here would read as stdio and fall back to the
    # env token on an HTTP call. If a 2.x minor renames the field, every message
    # fails loudly instead (verify_mcp_transport pins this seam).
    token = _MCP_REQUEST.set(ctx.request)
    # #757: the message itself (protocol version, raw params incl. MRTR inputResponses /
    # requestState, which the SDK's RequestStateBoundary has already unsealed).
    call_token = _MCP_CALL.set(ctx)
    try:
        return await call_next(ctx)
    finally:
        _MCP_CALL.reset(call_token)
        _MCP_REQUEST.reset(token)


mcp.middleware.append(_bind_request_middleware)

#: True while `_plan` is running. A planning call performs READ requests only;
#: any attempt to mutate while planning is a bug in a planner, and it fails loudly
#: instead of quietly billing somebody. A contextvar rather than a module global
#: so concurrent tool calls cannot leak the flag into each other.
_HTTP_MODE = False  # Captured by main before the listener starts; used for the scope report.

#: Whether tools may read a caller-supplied path off THIS machine. DENIED until a start
#: path explicitly allows it, which only local stdio does.
#:
#: Fail-closed on purpose. The first version of this guard keyed off `_HTTP_MODE`, which
#: defaults to False, so ANY start path that did not go through `main()` left remote file
#: reads fully enabled -- including importing this module and serving
#: `mcp.streamable_http_app()` directly, which is exactly how
#: `tests/verify_mcp_connector.py:325` starts its listener. A protection that depends on
#: the operator choosing one particular entry point is not a protection.
_LOCAL_FILE_READS = False
#: #755: the allowed input folders when a caller (a test, an embedder) named them
#: explicitly; None means LOC_STUDIO_INPUT_DIRS or the Documents/Downloads default.
_INPUT_ROOTS: list | None = None


def _allow_local_file_reads(allowed: bool, roots: list | None = None) -> None:
    """Open or close caller-supplied path reads. Only a local stdio start calls this.
    `roots` overrides the allowed input folders (#755); omitted, the env/default ones."""
    global _LOCAL_FILE_READS, _INPUT_ROOTS
    _LOCAL_FILE_READS = bool(allowed)
    _INPUT_ROOTS = list(roots) if roots is not None else None


_PLANNING: contextvars.ContextVar = contextvars.ContextVar("loc_mcp_planning", default=False)
#: The tool whose call is in flight, so an error can name the grant IT needs.
_CURRENT_TOOL: contextvars.ContextVar = contextvars.ContextVar("loc_mcp_tool", default=None)
#: The token THIS request must use for its /api calls, in streamable-http mode.
#: Set ONCE per request in _dispatch - the header token if the request carried one,
#: else the env TOKEN - and read by _active_token() for every /api call the request
#: makes. A contextvar for the same reason _PLANNING is one: each MCP request is
#: handled in its own asyncio task (the SDK's `tg.start_soon` per message), so the
#: value cannot leak between two concurrent requests, and the precedence is frozen
#: for the request's whole life - no /api call re-evaluates header-vs-env or falls
#: back mid-flight. UNSET in stdio mode (default None) -> _active_token() returns
#: the env TOKEN, so the stdio path is byte-identical to before wave 4.
_REQUEST_TOKEN: contextvars.ContextVar = contextvars.ContextVar(
    "loc_mcp_request_token", default=None
)


# --- internal helpers ------------------------------------------------------
class ToolError(_SdkToolError):
    """A user-facing tool failure. MCPServer turns a raised ToolError into an
    error tool result carrying the message; we raise this with a friendly,
    actionable message. It subclasses the SDK's ToolError because mcp 2 withholds
    the text of any OTHER exception from the client ("Error executing tool X" only)."""


class _HttpToolError(ToolError):
    """A ToolError raised for a non-2xx answer, carrying the HTTP status so a caller
    that treats ONLY a 404 as "not visible" (the vendor plans) can tell it from a
    401/503/connection fault and re-raise those."""

    status_code: int = 0
    #: #756 extras for the error tail, read off the response in `_call`.
    retry_after_s: int | None = None
    server_code: str | None = None
    has_server_message: bool = False


#: The key that starts the JSON tail line, so a re-raise never tails twice.
_ERROR_TAIL_MARK = '{"error": {"code"'


def _error_tail(tool_name: str, exc: BaseException) -> str:
    """The JSON last line of a tool error (#756): `{"error": {code, http_status,
    retryable, retry_after_s, required_scope[, server_code]}}`.

    Classified from the exception CHAIN, never from the prose: an _HttpToolError
    carries the status; a transport failure is the httpx cause our ToolError was
    raised from (a timeout means the work may still be running); a pydantic
    ValidationError is a bad argument; anything else our handlers raise is a
    refusal before sending (or the plan-mode fence, which is internal)."""
    spec = registry.tool_by_name(tool_name)
    scope = spec.scope if spec is not None else None
    chain = []
    node: BaseException | None = exc
    while node is not None and len(chain) < 8:
        chain.append(node)
        node = node.__cause__
    info = None
    for item in chain:
        if isinstance(item, _HttpToolError) and item.status_code:
            code = tool_meta.code_for_status(
                item.status_code, has_server_message=item.has_server_message
            )
            info = tool_meta.error_info(
                code,
                http_status=item.status_code,
                retry_after_s=item.retry_after_s,
                required_scope=scope if item.status_code == 403 else None,
                server_code=item.server_code,
            )
            break
        # A dropped connection mid-answer (RemoteProtocolError) is like a timeout: the
        # request was sent, so the work may still be running (#745), never "unreachable".
        if isinstance(item, (httpx.TimeoutException, httpx.RemoteProtocolError)):
            info = tool_meta.error_info("timeout_maybe_running")
            break
        if isinstance(item, httpx.TransportError):
            info = tool_meta.error_info("unreachable")
            break
        if type(item).__name__ == "ValidationError":
            info = tool_meta.error_info("invalid_argument")
            break
    if info is None:
        text = str(exc)
        if spec is None and text.startswith("Unknown tool"):
            info = tool_meta.error_info("unknown_tool")
        elif "internal error" in text:
            info = tool_meta.error_info("internal")
        else:
            info = tool_meta.error_info("refused")
    return json.dumps({"error": info}, separators=(", ", ": "))


_SERVER_CODE_RE = re.compile(r"[a-z][a-z0-9_]{0,39}")


def _current_http_request():
    """The Starlette Request behind THIS tool call in streamable-http mode, or None
    in stdio (and outside any call).

    mcp 2 threads the ASGI request onto the per-message `ServerRequestContext.request`
    (the streamable-http transport sets it as message metadata) and has no ambient
    request_ctx / get_context(), so `_bind_request_middleware` parks it in `_MCP_REQUEST`
    for the duration of each handler call. In stdio that field is None. Every
    failure to introspect degrades to None (treated as stdio -> env fallback) rather
    than raising, so a transport quirk can never turn a tool call into a traceback -
    the pinned SDK's shape is stable, and this is only the read side of it."""
    request = _MCP_REQUEST.get()
    # Only a real HTTP request exposes `.headers`; guard so a future transport that
    # parks something else on `.request` cannot be mistaken for one.
    if request is None or not hasattr(request, "headers"):
        return None
    return request


def _sanitize_token(tok: str) -> str:
    """The header token, with embedded control chars stripped and a hard refusal of
    non-transportable bytes.

    Two defenses, both cheap:
      * CONTROL CHARS (CR/LF and friends) are stripped - defense in depth against a
        header-smuggled `lsu_...\\r\\nX-Evil: 1`; a real lsu_ key never contains them.
      * NON-ASCII bytes are refused with a clean ToolError. A Latin-1 high byte in the
        header would otherwise blow up httpx's header encoder with a raw
        UnicodeEncodeError surfaced as an ugly tool result; an lsu_ key is ASCII, so
        this is a clean 'that is not a valid token' instead of a traceback."""
    if not tok:
        return ""
    cleaned = "".join(ch for ch in tok if ch >= " " and ch != "\x7f")
    try:
        cleaned.encode("ascii")
    except UnicodeError as exc:
        raise ToolError(
            "The token in the request header contains invalid characters (an lsu_ "
            "key is ASCII). Check the 'Authorization: Bearer' / 'X-Loc-Token' "
            "value."
        ) from exc
    return cleaned


def _request_header_token() -> str | None:
    """The lsu_ token from THIS HTTP request's headers, or None outside an HTTP
    request (stdio).

    Accepts `Authorization: Bearer <token>` and the `X-Loc-Token: <token>` alias,
    Authorization winning. Returns "" (not None) when an HTTP request is present but
    carries neither header - the caller then returns a clean no-token error.
    THE SERVER VALIDATES NOTHING about whether
    the token is a real key here: it reads whatever the request sent and forwards it,
    and the app's own 401/403 is the verdict. It only sanitizes the value so a
    malformed header cannot smuggle CRLF or crash the outbound encoder."""
    request = _current_http_request()
    if request is None:
        return None
    headers = request.headers  # case-insensitive (Starlette Headers)
    auth = headers.get("authorization") or ""
    tok = ""
    if auth[:7].lower() == "bearer ":
        tok = auth[7:].strip()
    if not tok:
        tok = (headers.get("x-loc-token") or "").strip()
    return _sanitize_token(tok)


def _active_token() -> str:
    """The token THIS request must present to /api.

    In streamable-http mode _dispatch has already frozen the header-vs-env decision
    into _REQUEST_TOKEN, so this just returns it. In stdio mode the contextvar is
    unset and this returns the env TOKEN - identical to pre-wave-4 behaviour."""
    req = _REQUEST_TOKEN.get()
    return req if req is not None else TOKEN


def _headers() -> dict[str, str]:
    # HEADER ONLY, never a query string: query strings land in proxy and access
    # logs, and this one is a credential. The value is the per-request token in
    # HTTP mode, the env token in stdio - _active_token() resolves which.
    out = {"X-Loc-Token": _active_token()}
    # #744: a confirmed spend call's idempotency key rides on every request it makes (set
    # and reset by _dispatch; the install keys it per caller and per route).
    key = longrun.IDEMPOTENCY_KEY.get()
    if key:
        out["Idempotency-Key"] = key
    return out


def _require_token() -> None:
    if _active_token():
        return
    # An HTTP request in flight sets _REQUEST_TOKEN to a (possibly empty) string;
    # stdio leaves it None. So a non-None value with an empty token means "this
    # request carried no header". The environment cannot authenticate an HTTP caller.
    if _REQUEST_TOKEN.get() is not None:
        raise ToolError(
            "No Localization OS token on this request. In streamable-http mode each "
            "request authenticates itself: send 'Authorization: Bearer lsu_...' "
            "(or 'X-Loc-Token: lsu_...') with a per-user key minted at "
            f"{_public_base_url()}/account. LOC_STUDIO_TOKEN is used only by stdio."
        )
    raise ToolError(
        "Localization OS is not configured: set the LOC_STUDIO_TOKEN environment "
        "variable to a per-user API key (mint one at "
        f"{_public_base_url()}/account; tick the scopes the tools you want need - "
        "'Documents scope' for documents and projects, 'Keys scope' for the "
        "key/Delta family)."
    )


def _public_base_url() -> str:
    """BASE_URL as a CLIENT may see it: any `user:password@` is masked.

    httpx sends URL userinfo as Basic auth, so a LOC_STUDIO_BASE_URL behind an
    authenticating proxy can carry a real credential, and every message that names
    the install (setup, 403, connection errors, whoami) reaches the model and, in
    streamable-http mode, every remote user. The greedy netloc match masks up to the
    LAST '@' before the path, so a raw '@' inside a password is masked too."""
    return re.sub(r"^((?:[A-Za-z][A-Za-z0-9+.\-]*:)?//)?[^/?#]*@", r"\1***@", BASE_URL, count=1)


def _timeout_error(method: str, exc: Exception) -> ToolError:
    """#745: the install was reached (or may have been) but did not answer in time. For a
    write that is NOT "unreachable": the work may still be running, and a blind retry is how
    a spend runs twice. A read is just slow."""
    if method == "GET":
        return ToolError(
            f"Localization OS at {_public_base_url()} did not answer this read in time (it may "
            f"be busy). Nothing was changed by it; try again shortly. Underlying error: "
            f"{type(exc).__name__}"
        )
    return ToolError(
        f"Localization OS at {_public_base_url()} did not answer in time. The operation may "
        "STILL be running on the server (a cancelled or timed-out tool call does not stop "
        "server work). Check job_status, work_status, connector_status "
        "before retrying, and do not call this tool again with "
        "confirm=true until you have checked (pass the same idempotency_key if you do). "
        f"Underlying error: {type(exc).__name__}"
    )


def _friendly_connection_error(exc: Exception) -> ToolError:
    return ToolError(
        f"Could not reach Localization OS at {_public_base_url()} (or it did not answer in "
        f"time). Is the app running (python run.py)? Underlying error: "
        f"{type(exc).__name__}: {exc}"
    )


def _explain_status(resp: httpx.Response) -> str:
    """Turn a non-2xx response into a clear, model-readable message.

    NEVER echoes request headers (the token rides in one) and always truncates
    the server's body - a review lens for every wave of this build."""
    body: Any = None
    try:
        body = resp.json()
        server_msg = body.get("error") or body.get("message") or body.get("detail") or ""
    except Exception:
        server_msg = (resp.text or "").strip()[:300]
    code = resp.status_code
    spec = registry.tool_by_name(_CURRENT_TOOL.get() or "")
    if code == 401:
        if _REQUEST_TOKEN.get() is not None:
            # HTTP mode: the env token is ignored, so naming it sends the caller the
            # wrong way (#532).
            return (
                "Localization OS rejected the API token (401). Check the key sent with "
                "this request (Authorization: Bearer or X-Loc-Token) is valid and active."
            )
        return (
            "Localization OS rejected the API token (401). Check LOC_STUDIO_TOKEN "
            "is a valid, active key."
        )
    if code == 403:
        if server_msg:
            token = _active_token()
            message = str(server_msg).replace(token, "[redacted]") if token else str(server_msg)
            return f"Localization OS refused this call (403): {message[:300]}"
        grant = (
            (f"the '{spec.scope}' scope")
            if spec
            else "the scope this route needs (one of: api, docs, keys, admin)"
        )
        if spec is not None and spec.scope == "admin":
            # #756: the admin grant is not on the account page; it is minted only on
            # the install itself, so the checkbox advice would send the caller nowhere.
            how = (
                "An 'admin' scoped key can only be issued locally on the install by an "
                "administrator (never over the API or the account page)"
            )
        else:
            how = (
                f"Mint a scoped key at {_public_base_url()}/account (the 'Documents scope' "
                "and 'Keys scope' checkboxes; an 'admin' scoped key is issued only locally "
                "on the install)"
            )
        return (
            f"Localization OS refused this call (403). Either the token lacks "
            f"{grant} or your role lacks the rung this action needs. {how}; "
            f"call whoami to see your role."
        )
    if code == 503:
        # A CAPACITY refusal (Localization OS #99/#100): the install is at its concurrent-
        # run ceiling, or its database connection pool is momentarily spent. Nothing was
        # started and nothing was spent, and the correct client behaviour is to WAIT and
        # try again — so say that explicitly rather than letting an agent read a generic
        # HTTP 503 as a dead install and abandon the work (or, worse, retry immediately in
        # a loop and keep the install saturated). The `reason` field distinguishes the two
        # without parsing prose.
        reason = (body.get("code") or body.get("reason")) if isinstance(body, dict) else None
        wait = (body or {}).get("retry_after") if isinstance(body, dict) else None
        hint = f" Wait about {wait}s and call this tool again." if wait else " Retry shortly."
        if reason == "stream_capacity":
            return server_msg or "Stream capacity is busy. Retry shortly; existing work continues."
        if reason == "run_capacity":
            # Structured capacity classification owns the complete shared message.
            if server_msg:
                return server_msg
            return (
                "Localization OS is already running as many workflows as it admits at once."
                " Nothing was launched and nothing was spent." + hint
            )
        if reason == "db_pool_exhausted":
            base = server_msg or "Localization OS had no free database connection."
            extra = "" if "half-written" in base.lower() else " Nothing was left half-written."
            return base + extra + hint
        # LOW-5: with NO body at all, `server_msg or resp.text[:200]` is "" and the old
        # f-string rendered a dangling "(503): . Retry shortly." Fall back to a whole
        # sentence instead of punctuating an empty one.
        detail = (server_msg or resp.text[:200] or "").strip().rstrip(".")
        return (
            f"Localization OS is temporarily unavailable (503): {detail}.{hint}"
            if detail
            else f"Localization OS is temporarily unavailable (503).{hint}"
        )
    if code == 409:
        # A refusal-before-anything-happened. Wave 1 assumed the mock-engine
        # hard-fail was the only 409; wave 2's routes added two more (the
        # Evaluations Lab's disabled switch and its acknowledged-estimate
        # mismatch), so the engine-specific advice is now appended ONLY when
        # the server sent no message of its own - a disabled-feature 409 telling
        # the caller to configure an MT engine was worse than no advice.
        # #756: "nothing was spent" is a promise, so it is made only where it is KNOWN
        # true: a 409 whose body code names a refusal-before-launch (run_conflict: the
        # launch is refused before any run row is written, pinned by verify_mcp_tools
        # check_213), or one of the reviewed pre-launch routes below. Any other route's
        # 409 (about 160 raise sites, not all audited) says only what is certain.
        if server_msg:
            if _is_pre_side_effect_409(resp, body):
                return server_msg + " (Nothing was started and nothing was spent.)"
            return (
                server_msg
                + " (Refused as a conflict with the current state; check it before retrying.)"
            )
        return (
            "No translation engine configured. (No job was started; an "
            "administrator must configure an MT engine.)"
        )
    if code == 413:
        return server_msg or "The file is too large for Localization OS's upload limit."
    if code == 415:
        return server_msg or "That file format is not supported."
    if code == 422:
        return server_msg or "Localization OS refused a parameter as out of range."
    if code == 404:
        # A KILL-SWITCHED FEATURE ANSWERS 404, NOT 403, so it cannot be
        # enumerated - and this server relays that verbatim rather than guessing
        # which it was. The registry's feature_gate field is documentation and
        # guard material; nothing here reads a setting.
        msg = server_msg or "Not found (it may not exist, or is not yours)."
        avail = (body or {}).get("available") if isinstance(body, dict) else None
        if avail:
            msg += " Available: {}.".format(", ".join(str(a) for a in list(avail)[:20]))
        # ONLY the gates that really present as 404 get the note. The keys and
        # image families answer 404 when switched off (indistinguishable from a
        # missing object, on purpose); LLMemory and the Evaluations Lab do NOT -
        # their reads keep answering and their writes refuse with the switch
        # named - so claiming "404 may mean the switch" for them would be a lie
        # in the one place an agent goes to diagnose one.
        if spec is not None and spec.feature_gate in registry.GATE_404_STYLE:
            msg += (
                f" NOTE: {spec.name} also answers 404 when the '{spec.feature_gate}' feature is switched "  # noqa: E501
                "off - a switched-off feature is indistinguishable from a "
                "missing object, on purpose."
            )
        return msg
    return f"Localization OS returned HTTP {code}: {server_msg or resp.text[:200]}"


#: Body codes whose 409 is a refusal BEFORE anything was written or spent.
_PRE_SIDE_EFFECT_409_CODES: frozenset = frozenset({"run_conflict"})

#: Routes whose stale-estimate 409 is a refusal before launch, reviewed for #756 (empty in the
#: open core; the paid Evaluations add-on registers its two launches here).
_PRE_SIDE_EFFECT_409_ROUTES: frozenset = frozenset()


def _is_pre_side_effect_409(resp: httpx.Response, body: Any) -> bool:
    code = (body.get("code") or body.get("reason")) if isinstance(body, dict) else None
    if code in _PRE_SIDE_EFFECT_409_CODES:
        return True
    # Gate: only a stale-estimate 409 (body `needs: acknowledge_estimate_usd`) is returned
    # before anything runs.
    if not (isinstance(body, dict) and body.get("needs") == "acknowledge_estimate_usd"):
        return False
    try:
        req = resp.request
    except RuntimeError:  # a Response built without a request (tests)
        return False
    # endswith: LOC_STUDIO_BASE_URL may carry a path prefix in front of /api.
    return any(
        req.method == method and req.url.path.endswith(path)
        for method, path in _PRE_SIDE_EFFECT_409_ROUTES
    )


def _annotate_http_error(err: _HttpToolError, resp: httpx.Response) -> None:
    """Copy the machine fields of a non-2xx answer onto `err` for the #756 tail.

    Only bounded, typed values: a whole-second retry hint (body `retry_after` or the
    Retry-After header, 0..86400) and a snake_case server `code`/`reason` of at most
    40 chars. Free text never rides in the tail; the prose message already carries it."""
    body: Any = None
    with contextlib.suppress(Exception):
        body = resp.json()
    body = body if isinstance(body, dict) else {}
    err.has_server_message = bool(
        body.get("error") or body.get("message") or body.get("detail")
    ) or (not body and bool((resp.text or "").strip()))
    raw_wait = body.get("retry_after")
    if raw_wait is None:
        raw_wait = resp.headers.get("retry-after")
    with contextlib.suppress(TypeError, ValueError, OverflowError):
        wait = int(float(raw_wait))
        if 0 <= wait <= 86400:
            err.retry_after_s = wait
    raw_code = body.get("code") or body.get("reason")
    if isinstance(raw_code, str) and _SERVER_CODE_RE.fullmatch(raw_code):
        err.server_code = raw_code


def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(timeout=_HTTP_TIMEOUT)


#: The ONLY non-GET requests plan mode may send: a POST whose unconfirmed call computes a cost
#: estimate and returns BEFORE anything launches. The open core has none (an add-on that has one
#: registers it here and in registry.PLAN_ESTIMATE_ROUTES). `_call` refuses the request anyway
#: if its body carries `confirm`/`live`, and verify_mcp_parity reads this set out of the AST and
#: fails if any entry is not a registry.PLAN_ESTIMATE_ROUTES route on a spend tool.
_PLAN_SAFE_REQUESTS: frozenset = frozenset()


async def _call(
    client: httpx.AsyncClient,
    method: str,
    path: str,
    *,
    params: dict | None = None,
    json_body: Any = None,
    data: dict | None = None,
    files: dict | None = None,
    timeout: httpx.Timeout | None = None,
    headers: dict | None = None,
) -> httpx.Response:
    """One HTTP call to the install, with the token, and nothing else.

    The single seam every handler goes through, so the token check, the
    connection error, the status translation and the PLAN-ONLY invariant each
    exist exactly once. `timeout` overrides the short default only for the
    routes that are synchronous by design (see _SLOW_TIMEOUT)."""
    _require_token()
    if method != "GET" and _PLANNING.get():
        plan_safe = (method, path) in _PLAN_SAFE_REQUESTS and not (
            isinstance(json_body, dict)
            # BOTH names of the spend gate: `confirm` everywhere, and `live` on
            # /api/evaluations/meta, whose flag the CLI and the spec call --live.
            and (json_body.get("confirm") or json_body.get("live"))
        )
        if not plan_safe:
            raise ToolError(
                f"internal error: a spend PLAN tried to call {method} {path}. Planning is "
                "read-only by contract; nothing was sent."
            )
    if json_body is not None:
        # Strict JSON, EXACTLY as httpx 0.28 encodes it (httpx._content.encode_json: these
        # dumps arguments, then UTF-8): a non-finite float (inf/nan, e.g. budget_usd) or a
        # lone surrogate in a string (the 2026-07-28 HTTP transport parses with json.loads,
        # which accepts one) is a caller error, and mcp 2 hides the text of any non-ToolError
        # exception, so the refusal must be a ToolError carrying the encoder's own wording
        # (what 1.x relayed). Nothing is sent. TypeError (an unserialisable value) is a
        # handler bug, not a caller error, and stays hidden.
        try:
            json.dumps(
                json_body, ensure_ascii=False, separators=(",", ":"), allow_nan=False
            ).encode("utf-8")
        except ValueError as exc:
            raise ToolError(str(exc)) from exc
    try:
        resp = await client.request(
            method,
            f"{BASE_URL}{path}",
            headers={**_headers(), **(headers or {})},
            params=params,
            json=json_body,
            data=data,
            files=files,
            **({"timeout": timeout} if timeout else {}),
        )
    except httpx.InvalidURL as exc:
        # Raised while BUILDING the request, so it is never "could not reach": either
        # LOC_STUDIO_BASE_URL is malformed (every call fails) or an argument made the URL
        # too long (httpx caps each component at 64 KiB, e.g. a huge search_tm q). httpx's
        # text names the bad component (host/port/length), never the userinfo.
        raise ToolError(
            f"Could not build the request to Localization OS at {_public_base_url()}: "
            f"{type(exc).__name__}: {exc}. If every call fails like this, "
            "LOC_STUDIO_BASE_URL is malformed; otherwise an argument is too long or "
            "malformed for a URL. Nothing was sent."
        ) from exc
    except UnicodeEncodeError as exc:
        # A lone surrogate in a query/form value (the JSON body is pre-checked above).
        raise ToolError(
            f"An argument holds text that cannot be sent as UTF-8 ({exc}). Nothing was sent."
        ) from exc
    except (httpx.ReadTimeout, httpx.WriteTimeout, httpx.RemoteProtocolError) as exc:
        # #745: the request reached the install (or may have): answer "may still be running",
        # never "is the app running?". ConnectError/ConnectTimeout/PoolTimeout (nothing was
        # sent) still fall through to the unreachable message below.
        raise _timeout_error(method, exc) from exc
    except httpx.TransportError as exc:
        # TransportError, not ConnectError: a TIMEOUT against a slow or wedged
        # install is a TimeoutException, a sibling of ConnectError rather than a
        # subclass, so catching only the latter turned the single most likely
        # real-world failure into a raw traceback in the model's tool result.
        raise _friendly_connection_error(exc) from exc
    if resp.status_code >= 400:
        if resp.status_code == 404 and path.startswith("/api/capabilities/"):
            try:
                card_error = resp.json()
            except ValueError:
                card_error = None
            if isinstance(card_error, dict) and set(card_error) == {"ok", "error"}:
                # Card refusals relay the install's complete JSON body (#113).
                raise ToolError(resp.text)
        http_err = _HttpToolError(_explain_status(resp))
        http_err.status_code = resp.status_code
        _annotate_http_error(http_err, resp)
        raise http_err
    return resp


async def _get_json(client: httpx.AsyncClient, path: str, **kw) -> dict:
    resp = await _call(client, "GET", path, **kw)
    return resp.json()


async def _post_json(client: httpx.AsyncClient, path: str, body: Any) -> dict:
    resp = await _call(client, "POST", path, json_body=body)
    return resp.json()


async def _launch(
    client: httpx.AsyncClient,
    path: str,
    body: dict,
    *,
    native_async: bool = False,
    slow: bool = False,
) -> dict:
    """#728/#747: POST a slow spend/write route as a background call and wait (bounded) for
    it. Returns the route's own answer if it finished in time, else a running handle with
    job_id and a `next` instruction (see longrun.py). `slow`: the tool used _SLOW_TIMEOUT
    before; an install without background calls still answers it synchronously, so the
    launch keeps that read timeout (a current install answers 202 at once either way)."""
    return await longrun.launch_and_wait(
        _call,
        client,
        path,
        body,
        explain=_explain_status,
        http_error=_HttpToolError,
        native_async=native_async,
        launch_timeout=_SLOW_TIMEOUT if slow else None,
    )


async def _patch_json(client: httpx.AsyncClient, path: str, body: Any) -> dict:
    resp = await _call(client, "PATCH", path, json_body=body)
    return resp.json()


def _filename_from_disposition(disposition: str) -> str:
    """Extract the filename from a Content-Disposition header, tolerating the
    quoted `attachment; filename="foo.de-DE.docx"` form the API sends.

    THE `Path(val).name` BELOW IS A LOAD-BEARING SECURITY CONTROL, NOT TIDINESS.
    The header is attacker-influenceable: AUDIT-keys-live-qa-2026-08-03 found that
    `?namespace=../../evil` survives the server-side download-filename sanitizer
    (`/` and `..` are not stripped), so the bytes we are about to write could
    otherwise land outside LOC_STUDIO_OUTPUT_DIR. Taking the basename neutralises
    it on this path; the server-side fix remains open on its own ticket. Do not
    "simplify" this line - verify_mcp_tools.py regression-tests it directly."""
    if not disposition:
        return ""
    for part in disposition.split(";"):
        part = part.strip()
        if part.lower().startswith("filename="):
            val = part[len("filename=") :].strip().strip('"')
            # guard against path traversal in a hostile header
            return Path(val).name
    return ""


#: #518: the largest export a REMOTE (non-stdio) call returns inline. Bigger ones are refused
#: with a pointer to the web UI / API download rather than streamed through a tool reply.
_INLINE_OUTPUT_MAX_BYTES = 4 * 1024 * 1024


def _inline_output(fname: str, content: bytes) -> dict:
    """The remote-mode twin of `_write_output`: return the content, touch no disk.

    A remote streamable-http caller cannot read this host's files, so a `{"path": ...}`
    reply gave them nothing but the host's absolute path, while every tenant shared one
    OUTPUT_DIR (fixed names such as memories.jsonl overwritten across tenants) and any
    authenticated user could fill the disk. Text comes back as UTF-8 `content`, anything
    else as `content_base64`; the name is the basename only."""
    if len(content) > _INLINE_OUTPUT_MAX_BYTES:
        raise ToolError(
            "this export is %d bytes, over the %d byte limit for a remote reply. Download it "  # noqa: UP031
            "from the web UI or the /api route instead." % (len(content), _INLINE_OUTPUT_MAX_BYTES)
        )
    out = {"filename": Path(fname).name, "bytes": len(content)}
    try:
        out["encoding"] = "utf-8"
        out["content"] = content.decode("utf-8")
    except UnicodeDecodeError:
        out["encoding"] = "base64"
        out.pop("content", None)
        out["content_base64"] = base64.b64encode(content).decode("ascii")
    return out


def _write_output(fname: str, content: bytes) -> dict:
    """Write bytes under LOC_STUDIO_OUTPUT_DIR and describe the result (local stdio only).

    Fail-closed like the file READS: unless a local stdio start opened local file access
    (`_allow_local_file_reads`), nothing is written and the content is returned inline
    instead (#518).

    Belt and braces with `_filename_from_disposition`: the resolved path is
    re-checked against the output directory, so even a name that somehow survived
    basenaming cannot escape it."""
    if not _LOCAL_FILE_READS:
        return _inline_output(fname, content)
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    root = OUTPUT_DIR.resolve()
    out_path = (root / Path(fname).name).resolve()
    if out_path.parent != root:
        raise ToolError(f"refusing to write outside the output directory ({root})")
    out_path.write_bytes(content)
    return {"path": str(out_path), "bytes": len(content)}


def _require_confirmed(name: str, confirm) -> None:
    """A spend handler's own buckle.

    `_dispatch` already answers an unconfirmed spend call with a plan and never
    reaches here. This exists so a direct caller of the handler (a test, a script,
    a refactor that bypasses the dispatcher) cannot spend by accident. It is an
    assertion, not a second implementation of the contract.

    `confirm is not True`, matching `_dispatch` EXACTLY, not `not confirm`. The
    latter was the wave-1 truthiness shape: the strings "false"/"0"/"no"/"off",
    and 1/[]/{} are all truthy-or-falsy in ways that do not mean the boolean the
    contract requires. This backstop demands the boolean True so a future direct-
    handler refactor cannot reintroduce the wave-1 money bug behind the buckle."""
    if confirm is not True:
        raise ToolError(
            f"{name} spends money and was called without confirm=true. Call it again "
            "without confirm to get a read-only plan (engine, scope, size), tell "
            "the user what it will spend, and only then pass confirm=true."
        )


def _seg(value) -> str:
    """One model-supplied value, safe to splice into a URL PATH.

    Percent-encodes everything including `/`, so a slug or locale the model wrote
    can only ever be one path segment. Without it, `card="../../etc/passwd"` is
    normalised by httpx into a request for a completely different path - the tool
    would then report on whatever that path happened to answer. Ids go through
    int() and need no quoting; anything free-text goes through here."""
    from urllib.parse import quote

    return quote(str(value), safe="")


def _clean_locales(targets) -> list[str]:
    return [str(t).strip() for t in (targets or []) if str(t).strip()]


#: #755: extensions each fixed-format reader accepts (the receiving route's own list).
#: Documents and key files use the install's live /api/formats list instead.
_INPUT_EXTS: dict = {
    "image": ("png", "jpg", "jpeg"),
    "glossary file": ("csv", "tsv", "tbx", "mtf", "xml", "json"),
    "TMX or SDLTM file": ("tmx", "sdltm", "xml"),
}
#: Key files when /api/formats cannot be read (a keys-only token has no docs scope).
_KEY_FILE_EXTS = (
    "json", "po", "pot", "properties", "strings", "stringsdict", "yaml", "yml", "xml",
    "arb", "xliff", "xlf", "resx", "resw", "ts", "php", "ftl", "lang", "ini", "csv",
)  # fmt: skip
_IMAGE_MAX_BYTES = 8 * 1024 * 1024
_FORMATS_CACHE: dict = {}
_FORMATS_TTL_S = 300.0


def _local_read_guard(file_path: str, what: str, exts=None, *, read: bool = True):
    """The #755 guard in front of every caller-supplied path: stdio only, inside the
    allowed input folders, no hidden parts, an accepted extension, under the cap."""
    if not _LOCAL_FILE_READS:
        raise ToolError(
            "file_path is only available to a local stdio server. Use inline data instead."
        )
    roots = loc_file_guard.configured_roots(OUTPUT_DIR, _INPUT_ROOTS)
    cap = loc_file_guard.max_bytes()
    if what == "image":
        cap = min(cap, _IMAGE_MAX_BYTES)
    if exts is None:
        exts = _INPUT_EXTS.get(what)
    fn = loc_file_guard.read if read else loc_file_guard.check
    try:
        return fn(file_path, what=what, roots=roots, exts=exts, cap=cap)
    except loc_file_guard.FileRefused as exc:
        raise ToolError(str(exc)) from None


def _read_local_file(file_path: str, what: str = "file", exts=None) -> bytes:
    return _local_read_guard(file_path, what, exts)


async def _upload_exts(kind: str) -> tuple:
    """Extensions the install's upload routes accept, from GET /api/formats (cached
    5 minutes). `kind="key file"` falls back to the key-format list when the token
    cannot read the catalogue; a document upload fails closed instead."""
    now = time.monotonic()
    hit = _FORMATS_CACHE.get(BASE_URL)
    exts = hit[1] if hit and now - hit[0] < _FORMATS_TTL_S else None
    if exts is None:
        try:
            async with _client() as client:
                body = await _get_json(client, "/api/formats")
            exts = tuple(
                str(f.get("ext") or "").lower().lstrip(".")
                for f in [*(body.get("formats") or []), *(body.get("batch_formats") or [])]
                if isinstance(f, dict) and f.get("ext")
            )
            _FORMATS_CACHE[BASE_URL] = (now, exts)
        except ToolError:
            if kind != "key file":
                raise
            exts = ()
    if kind == "key file":
        return tuple(sorted({*exts, *_KEY_FILE_EXTS}))
    return exts


# ===========================================================================
# HANDLERS -- one per registry Tool, named exactly after it, with a signature
# that is exactly its input_schema's properties (verify_mcp_parity AST-reads
# this file and compares the two, so the halves cannot drift).
#
# NOTHING HERE DECIDES ANYTHING. No scoping filter, no RBAC, no retry-until-
# success, no id probing, no fabricated confirmation. Where a tool composes calls
# (create_project = find or create the customer, then create the project) the
# composition is a fixed, documented two-step, not a workflow engine.
# ===========================================================================


# --- family A: orientation --------------------------------------------------
async def whoami(card: str | None = None) -> str:
    async with _client() as client:
        me = await _get_json(client, "/api/account")
        scopes = me.get("scopes")
        out: dict = {
            "account": {k: me.get(k) for k in ("email", "name", "role", "ui_locale")},
            # #751: this key's OWN grants (GET /api/account `scopes`); an older install
            # without the field gets the honest note instead of a guess.
            "scopes": (
                scopes
                if isinstance(scopes, list)
                else (
                    "not reported by this install. A 403 means this key lacks a "
                    "scope or your role lacks the rung."
                )
            ),
            "toolsets": _active_toolset_names(),
            "base_url": _public_base_url(),
        }
        slug = str(card or "").strip()
        if slug:
            # _seg(slug) percent-encodes the model-supplied card slug into ONE path
            # segment; verify_mcp_parity's path-splice guard scans this f-string and
            # confirms the _seg() quoting (its JoinedStr surface).
            body = await _get_json(client, f"/api/capabilities/{_seg(slug)}")
            out["card"] = {
                "slug": body.get("slug"),
                "title": body.get("title"),
                "text": body.get("text") or body.get("body") or "",
            }
    return json.dumps(out)


async def release_manifest() -> str:
    async with _client() as client:
        return json.dumps(await _get_json(client, "/api/release"))


async def list_languages() -> str:
    async with _client() as client:
        body = await _get_json(client, "/api/languages")
    return json.dumps(
        {
            "language_sets": body.get("language_sets", []),
            "locales": body.get("locales", []),
            # a11y T5: each locale's writing direction ("rtl" / "ltr" / "" when the
            # code is not a language tag); {} from an older host.
            "directions": body.get("directions", {}),
        }
    )


async def list_capabilities() -> str:
    async with _client() as client:
        formats = await _get_json(client, "/api/formats")
        langsets = await _get_json(client, "/api/langsets")
        ctypes = await _get_json(client, "/api/content-types")
    return json.dumps(
        {
            "formats": formats.get("formats", []),
            # Batch 13: upload containers (a .zip = one document per supported file).
            "batch_formats": formats.get("batch_formats", []),
            "language_sets": langsets.get("language_sets", []),
            "content_types": ctypes.get("content_types", []),
        }
    )


# --- family B: projects and customers ---------------------------------------
async def list_projects(
    view: str = "projects",
    project_id: int | None = None,
    include_archived_customers: bool = False,
    fields: dict | None = None,
    sort_field: str | None = None,
    descending: bool = False,
    intake: str | None = None,
) -> str:
    mode = str(view or "projects").strip() or "projects"
    if mode not in ("projects", "project", "documents", "leverage", "fields"):
        raise ToolError(
            f"unknown view {mode!r}; use projects, project, documents, leverage or fields."
        )
    if mode in ("project", "documents", "leverage", "fields") and project_id is None:
        raise ToolError(f"view={mode} needs a project_id.")
    async with _client() as client:
        if mode == "project":
            body = await _get_json(client, "/api/projects/%d" % int(project_id))  # noqa: UP031
            body.pop("ok", None)
            return json.dumps({"project": body})
        if mode == "fields":
            body = await _get_json(client, "/api/projects/%d/fields" % int(project_id))  # noqa: UP031
            body.pop("ok", None)
            return json.dumps(body)
        if mode == "documents":
            body = await _get_json(client, "/api/projects/%d/documents" % int(project_id))  # noqa: UP031
            return json.dumps(
                {
                    "project_id": int(project_id),
                    "documents": body.get("documents", []),
                    # Batch 13: ZIP batch uploads (id, archive, document count).
                    "zip_batches": body.get("zip_batches", []),
                }
            )
        if mode == "leverage":
            # CV-1: the 7-band word-based TM-leverage breakdown for the project.
            body = await _get_json(client, "/api/projects/%d/leverage" % int(project_id))  # noqa: UP031
            body.pop("ok", None)
            # project_id top-level (aligning with view=documents), not nested.
            pid = body.pop("project_id", int(project_id))
            return json.dumps({"project_id": pid, "leverage": body})
        proj_params: dict = {}
        # Custom project fields: filter on {key: expression}, order by one key.
        for fkey, fexpr in (fields or {}).items():
            proj_params[f"field.{str(fkey).strip()}"] = str(fexpr)
        if str(sort_field or "").strip():
            proj_params["sort"] = f"field.{str(sort_field).strip()}"
            if descending:
                proj_params["dir"] = "desc"
        if str(intake or "").strip():
            # Connected projects: "connected" lists only projects whose content comes from a
            # connected system; "upload" only the others.
            if str(intake).strip().lower() not in ("connected", "upload"):
                raise ToolError('intake must be "connected" or "upload".')
            proj_params["intake"] = str(intake).strip().lower()
        projects = await _get_json(client, "/api/projects", params=proj_params or None)
        params = {"archived": "all"} if include_archived_customers else None
        customers = await _get_json(client, "/api/customers", params=params)
    # M5b: surface the AU-7 truncation flags -- silent truncation is the exact failure
    # AU-7 abolishes, and dropping the flag here re-introduced it on the MCP surface.
    # A truncated listing means "there are more; narrow or page" -- never "that's all".
    return json.dumps(
        {
            "projects": projects.get("projects", []),
            "customers": customers.get("customers", []),
            "projects_truncated": bool(projects.get("truncated")),
            # #607: in-scope projects the default archive/status filters left out.
            "projects_hidden_by_default": projects.get("hidden", {}),
            "customers_truncated": bool(customers.get("truncated")),
        }
    )


async def create_project(
    name: str,
    customer_name: str | None = None,
    customer_id: int | None = None,
    source_locale: str = "en-US",
    target_locales: list[str] | None = None,
    project_type: str = "document",
    due_date: str | None = None,
    custom_fields: dict | None = None,
    intake: str = "upload",
) -> str:
    """DOCUMENTED COMPOSITE (SPEC section 3, family B): POST /api/projects 400s
    without a customer_id, so an agent handed only a customer NAME has no on-ramp
    at all. The composite is exactly two fixed steps - look the name up, create it
    only if it is absent - and the reply says which of the two happened."""
    if not str(name or "").strip():
        raise ToolError("A project needs a name.")
    cust_name = str(customer_name or "").strip()
    created_customer = False
    async with _client() as client:
        cid = customer_id
        if cid is None:
            if not cust_name:
                raise ToolError(
                    "Every project belongs to a customer. Pass customer_id, or "
                    "customer_name and this tool will find or create it."
                )
            # M5c/NEW-2: resolve the name via the server's casefold+trim exact match
            # (?name=), not by scanning the paginated, 1000-capped roster -- a name past
            # row 1000, or a non-ASCII/padded name, otherwise missed and CREATED A
            # DUPLICATE customer. `archived=all` so an existing archived customer is
            # reused rather than duplicated too.
            existing = await _get_json(
                client, "/api/customers", params={"name": cust_name, "archived": "all"}
            )
            # F-2: the belt-and-braces re-filter must AGREE with the server contract --
            # casefold + trim on BOTH sides. The old `.strip().lower()` (ASCII lower,
            # query untrimmed) discarded the server's correct match for a padded or
            # non-ASCII name, so a duplicate was created anyway.
            needle = cust_name.strip().casefold()
            match = [
                c
                for c in existing.get("customers", [])
                if str(c.get("name", "")).strip().casefold() == needle
            ]
            if match:
                cid = match[0].get("id")
            else:
                made = await _post_json(client, "/api/customers", {"name": cust_name})
                cid = made.get("id")
                created_customer = True
        body = {
            "name": str(name).strip(),
            "customer_id": cid,
            "source_locale": str(source_locale or "en-US").strip(),
            "targets": ",".join(_clean_locales(target_locales)),
            "project_type": str(project_type or "document").strip(),
        }
        if str(due_date or "").strip():
            body["due_date"] = str(due_date).strip()
        if custom_fields:
            body["custom_fields"] = custom_fields
        want_intake = str(intake or "upload").strip().lower()
        if want_intake not in ("upload", "connected"):
            raise ToolError('intake must be "upload" or "connected".')
        if want_intake == "connected":
            # A connected project is a Documents project whose content comes from a connected
            # system; the shape is not a choice here.
            if str(project_type or "document").strip() != "document":
                raise ToolError(
                    'intake="connected" creates a Documents project; leave project_type as '
                    '"document".'
                )
            body["intake"] = "connected"
        project = await _post_json(client, "/api/projects", body)
    project.pop("ok", None)
    out = {"project": project, "customer_id": cid, "customer_created": created_customer}
    if want_intake == "connected":
        # MCP never creates a connection or sees a secret: bind an EXISTING one with
        # project_automation(action="bind"); when none fits, a person sets one up on this page.
        out["next"] = (
            "The project is created empty. Bind an existing connection with "
            'project_automation(action="bind", connection_id=...). If no connection fits, a '
            "person connects a new system on the project page: "
            + _public_base_url().rstrip("/")
            + str(project.get("setup_url") or f"/projects/{project.get('id')}#source")
        )
    return json.dumps(out)


#: action -> (path template, the field that action reads, how to build the body).
#: A table rather than a chain of ifs, so "which routes can this tool reach" reads
#: next to the registry entry that declares them.
_CONFIGURE_ACTIONS: dict = {
    "rename": ("/api/projects/%d/rename", "name", lambda v: {"name": v}),
    "due": ("/api/projects/%d/due", "due_date", lambda v: {"due_date": v}),
    "content-type": (
        "/api/projects/%d/content-type",
        "content_type_id",
        lambda v: {"content_type_id": v},
    ),
    "set-customer": ("/api/projects/%d/set-customer", "customer_id", lambda v: {"customer_id": v}),
    # Connected projects: switch an EMPTY project between uploads and a connected source.
    "intake": ("/api/projects/%d/intake", "intake", lambda v: {"intake": v}),
    # Connected projects: bind the connection that already feeds an EXISTING project (never
    # creates a connection, schedules or runs anything). connection_id is optional.
    "make-connected": ("/api/projects/%d/make-connected", "", lambda v: {}),
    "add-locale": ("/api/projects/%d/add-locale", "locale", lambda v: {"locale": v}),
    "apply-langset": ("/api/projects/%d/apply-langset", "langset_id", lambda v: {"langset_id": v}),
    "pair-providers": (
        "/api/projects/%d/pair-providers",
        "provider_mode",
        lambda v: {"provider_mode": v},
    ),
    # #690: translate comments in Office files (one switch for every Office format)
    "comments": (
        "/api/projects/%d/translate-comments",
        "translate_comments",
        lambda v: {"translate_comments": v},
    ),
}


async def configure_project(
    project_id: int,
    action: str,
    name: str | None = None,
    due_date: str | None = None,
    content_type_id: int | None = None,
    customer_id: int | None = None,
    locale: str | None = None,
    langset_id: int | None = None,
    provider_mode: str | None = None,
    pair_providers: dict | None = None,
    translate_comments: bool | None = None,
    intake: str | None = None,
    connection_id: int | None = None,
) -> str:
    act = str(action or "").strip()
    if act not in _CONFIGURE_ACTIONS:
        raise ToolError(
            "unknown action {!r}; use one of: {}".format(act, ", ".join(sorted(_CONFIGURE_ACTIONS)))
        )
    path_tpl, field, build = _CONFIGURE_ACTIONS[act]
    if act == "make-connected":
        async with _client() as client:
            out = await _post_json(
                client,
                path_tpl % int(project_id),
                {} if connection_id is None else {"connection_id": int(connection_id)},
            )
        out.pop("ok", None)
        return json.dumps({"action": act, "project_id": int(project_id), "result": out})
    given = {
        "name": name,
        "due_date": due_date,
        "content_type_id": content_type_id,
        "customer_id": customer_id,
        "locale": locale,
        "langset_id": langset_id,
        "provider_mode": provider_mode,
        "translate_comments": translate_comments,
        "intake": intake,
    }[field]
    # `due` is the one action whose EMPTY value is meaningful (clear the date).
    if given is None or (field != "due_date" and not str(given).strip()):
        raise ToolError(f"action={act} needs {field}.")
    body = build(given)
    if act == "pair-providers":
        body["pair_providers"] = pair_providers or {}
    async with _client() as client:
        out = await _post_json(client, path_tpl % int(project_id), body)
    out.pop("ok", None)
    return json.dumps({"action": act, "project_id": int(project_id), "result": out})


#: Arming recurring paid, live-writing runs (turning a schedule on or resuming it, a full pull,
#: changing when or how often it fires) is a decision for a person on the web, CLI or API; an agent
#: starts one run through project_run, which is two-phase (plan, then confirm).
_AUTOMATION_HUMAN_ONLY = (
    "An agent cannot turn a schedule on, resume it, switch it to full mode or change when it "
    "runs: that arms recurring paid runs that write into a live system. A person does it on the "
    "web page, the CLI or the API. To run once, use project_run (plan, then confirm)."
)

_AUTOMATION_ACTIONS = ("get", "history", "bind", "unbind", "set", "pause", "resume", "handover")

#: The per-run spend cap: an agent may only make it tighter. Clearing or raising it lets an
#: already-armed schedule spend more per run, which is a person's decision like arming it.
_AUTOMATION_CAP_HUMAN_ONLY = (
    "An agent can only lower a project's per-run spend cap (or set one where there is none); "
    "clearing or raising it is a decision for a person on the web page, the CLI or the API."
)


async def _automation_cap_only_lowers(client, base: str, new_cap) -> None:
    """Refuse a run_cap_usd that clears or raises the project's current per-run cap."""
    try:
        new = float(new_cap)
    except (TypeError, ValueError):
        raise ToolError("run_cap_usd must be a number.") from None
    if isinstance(new_cap, bool) or new != new or new <= 0:
        raise ToolError(_AUTOMATION_CAP_HUMAN_ONLY)
    current = (await _get_json(client, base + "/automation")).get("run_cap_usd")
    if current is not None and new > float(current):
        raise ToolError(_AUTOMATION_CAP_HUMAN_ONLY)


#: The ONLY scope settings an agent may pass to project_automation bind: the settings that
#: NARROW what a project reads inside a connection's own credential (the manager-level list,
#: project_automation.NARROW_KEYS). Hosts, roots, JQL, repositories and live-edit switches are
#: never reachable from here; the install refuses them for a manager too. An unknown name is
#: refused by NAME before any HTTP call, never dropped.
_AUTOMATION_SCOPE_FIELDS = frozenset(
    {
        "view",
        "max_records",
        "max_results",
        "max_pages",
        "label_names",
        "section_id",
        "category_id",
        "max_articles",
        "folder_id",
        "department_id",
        "help_center_id",
        "record_type_id",
        "source_depth",
        "content_types",
        "fields",
        "exclude_fields",
        "max_entries",
        "resource_types",
        "include_subfolders",
        "max_files",
        "extensions",
        "skus",
        "source_paths",
        "document_types",
        "max_items",
        "post_types",
        "source_category",
        "source_tag",
        "source_status",
        "target_locales",
    }
)


async def project_automation(
    project_id: int,
    action: str,
    connection_id: int | None = None,
    clone: bool | None = None,
    scope: dict | None = None,
    enabled: bool | None = None,
    mode: str | None = None,
    times: list[str] | None = None,
    days: list[str] | None = None,
    interval_minutes: int | None = None,
    auto_pause_after: int | None = None,
    run_cap_usd: float | None = None,
    move_schedule: bool | None = None,
    reason: str | None = None,
    limit: int | None = None,
    to_project_id: int | None = None,
    from_project_id: int | None = None,
) -> str:
    """A project's content source and run schedule (project automation). Bills nothing:
    enabling a schedule never starts a run (project_run does, behind the spend gate). An
    agent can bind an EXISTING connection but never create one or see a secret, and a
    connection or project the token cannot see is a 404, never a write."""
    act = str(action or "").strip()
    if act not in _AUTOMATION_ACTIONS:
        raise ToolError(
            "unknown action {!r}; use one of: {}".format(act, ", ".join(_AUTOMATION_ACTIONS))
        )
    pid = int(project_id)
    base = "/api/projects/%d" % pid  # noqa: UP031
    if (
        act == "resume"
        or enabled is True
        or str(mode or "").lower() == "full"
        or times is not None
        or days is not None
        or interval_minutes is not None
        or move_schedule
    ) and act in ("resume", "set"):
        raise ToolError(_AUTOMATION_HUMAN_ONLY)
    async with _client() as client:
        if act == "get":
            out = await _get_json(client, base + "/automation")
        elif act == "history":
            params = {"limit": int(limit)} if limit else None
            out = await _get_json(client, base + "/runs", params=params)
        elif act == "bind":
            if connection_id is None:
                raise ToolError(
                    "action=bind needs connection_id (an existing connection; list them with "
                    "list_connectors). An agent never creates a connection: if none fits, a "
                    "person connects a new system on the project page, "
                    + _public_base_url().rstrip("/")
                    + f"/projects/{pid}#source."
                )
            body: dict = {"connection_id": int(connection_id)}
            if clone is not None:
                body["clone"] = bool(clone)
            if scope:
                bad = sorted(str(k) for k in scope if k not in _AUTOMATION_SCOPE_FIELDS)
                if bad:
                    raise ToolError(
                        "scope may only narrow what the project reads; not available to an "
                        "agent: {}. Allowed: {}.".format(
                            ", ".join(bad), ", ".join(sorted(_AUTOMATION_SCOPE_FIELDS))
                        )
                    )
                body["scope"] = scope
            out = await _post_json(client, base + "/source", body)
        elif act == "unbind":
            out = await _post_json(client, base + "/source/unbind", {})
        elif act == "handover":
            if (to_project_id is None) == (from_project_id is None):
                raise ToolError("action=handover needs to_project_id or from_project_id (one).")
            hbody = (
                {"to_project_id": int(to_project_id)}
                if to_project_id is not None
                else {"from_project_id": int(from_project_id)}
            )
            out = await _post_json(client, base + "/automation/handover", hbody)
        elif act == "pause":
            out = await _post_json(client, base + "/automation/pause", {"reason": reason or ""})
        elif act == "resume":
            raise ToolError(_AUTOMATION_HUMAN_ONLY)  # unreachable: refused above
        else:  # set
            if run_cap_usd is not None:
                await _automation_cap_only_lowers(client, base, run_cap_usd)
            body = {}
            for key, val in (
                ("enabled", enabled),
                ("mode", mode),
                ("times", times),
                ("days", days),
                ("interval_minutes", interval_minutes),
                ("auto_pause_after", auto_pause_after),
                ("run_cap_usd", run_cap_usd),
                ("move_schedule", move_schedule),
            ):
                if val is not None:
                    body[key] = val
            if not body:
                raise ToolError("action=set needs at least one setting to change.")
            resp = await _call(client, "PUT", base + "/automation", json_body=body)
            out = resp.json()
    out.pop("ok", None)
    return json.dumps({"action": act, "project_id": pid, **out})


async def project_run(
    project_id: int,
    mode: str | None = None,
    confirm: bool = False,
    idempotency_key: str | None = None,
) -> str:
    """Run a project's content source now (confirmed half; the plan lives in the planner).
    Enqueues the run and waits up to 45 s, then hands back the job id to poll with
    job_status. A run already in flight is handed back, never stacked."""
    _require_confirmed("project_run", confirm)
    body: dict = {}
    if mode:
        body["mode"] = str(mode)
    async with _client() as client:
        out = await _launch(
            client,
            "/api/projects/%d/runs" % int(project_id),  # noqa: UP031
            body,
            native_async=True,
            slow=True,
        )
    out.pop("ok", None)
    return json.dumps({"project_id": int(project_id), **out})


# --- custom project fields (SPEC-project-automation part C) ----------------
_FIELD_ACTIONS = ("list", "create", "update", "archive", "restore")


async def manage_custom_fields(
    action: str,
    field_id: int | None = None,
    key: str | None = None,
    label: str | None = None,
    type: str | None = None,
    options: list[str] | None = None,
    key_pattern: str | None = None,
    link_system: str | None = None,
    help_text: str | None = None,
    required: bool | None = None,
    show_in_list: bool | None = None,
    filterable: bool | None = None,
    list_order: int | None = None,
    customer_id: int | None = None,
    include_archived: bool = False,
) -> str:
    act = str(action or "").strip()
    if act not in _FIELD_ACTIONS:
        raise ToolError(f"unknown action {act!r}; use one of: {', '.join(_FIELD_ACTIONS)}.")
    if act in ("update", "archive", "restore") and field_id is None:
        raise ToolError(f"action={act} needs field_id.")
    async with _client() as client:
        if act == "list":
            out = await _get_json(
                client,
                "/api/custom-fields",
                params={"archived": "1"} if include_archived else None,
            )
        elif act == "create":
            if not str(key or "").strip() or not str(label or "").strip():
                raise ToolError("action=create needs key and label.")
            payload: dict = {"key": str(key).strip(), "label": str(label).strip()}
            for name, val in (
                ("type", type),
                ("options", options),
                ("key_pattern", key_pattern),
                ("link_system", link_system),
                ("help_text", help_text),
                ("required", required),
                ("show_in_list", show_in_list),
                ("filterable", filterable),
                ("list_order", list_order),
                ("customer_id", customer_id),
            ):
                if val is not None:
                    payload[name] = val
            out = await _post_json(client, "/api/custom-fields", payload)
        else:
            if act == "update":
                payload = {}
                for name, val in (
                    ("label", label),
                    ("options", options),
                    ("key_pattern", key_pattern),
                    ("link_system", link_system),
                    ("help_text", help_text),
                    ("required", required),
                    ("show_in_list", show_in_list),
                    ("filterable", filterable),
                    ("list_order", list_order),
                ):
                    if val is not None:
                        payload[name] = val
                if not payload:
                    raise ToolError("action=update with nothing to change.")
            else:
                payload = {"archived": act == "archive"}
            out = await _patch_json(client, "/api/custom-fields/%d" % int(field_id), payload)  # noqa: UP031
    out.pop("ok", None)
    return json.dumps({"action": act, **out})


async def set_project_fields(project_id: int, fields: dict) -> str:
    if not isinstance(fields, dict) or not fields:
        raise ToolError('fields must be an object like {"jira_ticket": "LOC-123"}.')
    async with _client() as client:
        out = await _patch_json(
            client,
            "/api/projects/%d/fields" % int(project_id),  # noqa: UP031
            {"fields": fields},
        )
    out.pop("ok", None)
    return json.dumps(out)


# --- family C: documents ----------------------------------------------------
async def submit_translation(
    file_path: str,
    targets: list[str],
    project_id: int | None = None,
    confirm: bool = False,
    idempotency_key: str | None = None,
) -> str:
    _require_confirmed("submit_translation", confirm)
    _local_read_guard(file_path, "file", read=False)  # path refusals before any /api call
    data = _read_local_file(file_path, "file", await _upload_exts("file"))
    src = Path(str(file_path)).expanduser()
    locales = _clean_locales(targets)
    form: dict[str, str] = {}
    if project_id is not None:
        form["project_id"] = str(project_id)

    async with _client() as client:
        up = await _call(
            client, "POST", "/api/documents", data=form, files={"file": (src.name, data)}
        )
        up_body = up.json()
        doc_id = up_body.get("document_id")
        if not doc_id:
            raise ToolError(f"Upload succeeded but returned no document id: {up.text[:200]}")
        # #687 (four-surface rule): the upload's file notices (e.g. a bilingual file
        # translated only into its own target language) reach the agent as they reach
        # the web page and the CLI.
        file_warnings = [
            {"kind": w.get("kind"), "message": w.get("message")}
            for w in (up_body.get("file_warnings") or [])
            if isinstance(w, dict) and w.get("message")
        ]
        jobs = []
        budget_refused: list = []
        if locales:
            # An EMPTY targets list uploads ONLY - that is how an agent stages a
            # document for a workflow run rather than plain MT, and it is why no
            # separate upload_document tool exists.
            tr = await _post_json(
                client,
                # doc_id is the id THIS install's own upload response returned
                # (server-authored, not model free-text); the parity guard's
                # approved-splice table reviews (/api/documents/%s/translate, doc_id).
                f"/api/documents/{doc_id}/translate",
                {"targets": locales},
            )
            jobs = tr.get("jobs", [])
            # #503: the department cap can run out partway through a multi-locale request;
            # the 202 then names the locales it did NOT enqueue. Pass that on so the agent
            # does not read a silent gap as "all requested languages started".
            budget_refused = tr.get("budget_refused") or []
    out: dict = {
        "document_id": doc_id,
        "jobs": [{"locale": j.get("locale"), "job_id": j.get("job_id")} for j in jobs],
    }
    if budget_refused:
        out["budget_refused"] = list(budget_refused)
    if file_warnings:
        out["file_warnings"] = file_warnings
    return json.dumps(out)


async def job_status(
    job_id: int | None = None,
    status: str | None = None,
    kind: str | None = None,
    document_id: int | None = None,
    include_runs: bool = False,
    limit: int = 20,
) -> str:
    if job_id is None:
        params: dict = {"limit": int(limit or 20)}
        if str(status or "").strip():
            params["status"] = str(status).strip()
        if str(kind or "").strip():
            params["kind"] = str(kind).strip()
        if document_id is not None:
            params["document"] = int(document_id)
        if include_runs:
            params["include_runs"] = "true"
        async with _client() as client:
            body = await _get_json(client, "/api/jobs", params=params)
        return json.dumps({"total": body.get("total"), "jobs": body.get("jobs", [])})

    deadline = asyncio.get_event_loop().time() + _POLL_BUDGET_S
    last: dict = {}
    async with _client() as client:
        while True:
            last = await _get_json(client, "/api/jobs/%d" % int(job_id))  # noqa: UP031
            state = str(last.get("status", "")).lower()
            if state in _TERMINAL:
                break
            if asyncio.get_event_loop().time() >= deadline:
                break
            await asyncio.sleep(_POLL_GAP_S)
    # #743: a finished job carries its RESULT (counts, created/updated, held back, failures,
    # message; capped), so an agent can report held content instead of a bare "done".
    return json.dumps(longrun.status_view(last))


async def get_result(document_id: int, locale: str, as_fmt: str = "") -> str:
    loc = str(locale or "").strip()
    if not loc:
        raise ToolError("No locale given. Provide the target locale, e.g. 'de-DE'.")
    params = {"locale": loc}
    if str(as_fmt or "").strip():
        params["as_fmt"] = str(as_fmt).strip().lower()  # "po": a .mo document as .po
    async with _client() as client:
        resp = await _call(
            client,
            "GET",
            "/api/documents/%d/download" % int(document_id),  # noqa: UP031
            params=params,
        )
        content = resp.content
        fname = _filename_from_disposition(resp.headers.get("content-disposition", ""))
    if not fname:
        fname = f"doc{document_id}.{loc}.bin"
    written = _write_output(fname, content)
    return json.dumps({**written, "locale": loc})


async def apply_tm(document_id: int, locale: str) -> str:
    loc = str(locale or "").strip()
    if not loc:
        raise ToolError("No locale given. Provide the target locale, e.g. 'de-DE'.")
    async with _client() as client:
        out = await _post_json(
            client,
            "/api/documents/%d/apply-tm" % int(document_id),  # noqa: UP031
            {"locale": loc},  # noqa: RUF100, UP031
        )
    out.pop("ok", None)
    return json.dumps(out)


async def approve_segment(
    segment_id: int,
    acknowledge_placeholder_loss: bool = False,
    propagate: bool = True,
    propagation: str = "",
) -> str:
    """Approve a reviewed segment and save its pair to TM (POST /api/segments/{id}/
    approve). The server runs the refusal/language guards; a hedge/non-translation is
    WITHHELD from TM (tm_written:false + a tm_skipped reason and a tm_skipped_code) while the
    segment keeps its approved target. A held/deleted-key/foreign id rides back as a clean
    error (the /api route's 404/400 -> _post_json raises).

    A target that drops or adds a real placeholder ({count}, %s) is approved but also
    WITHHELD from TM (tm_skipped_code "placeholder_integrity") unless
    acknowledge_placeholder_loss is true (Issue #562).

    Approving also copies the target onto the document's other open repeats of the same
    source (Issue #617 item 6): the reply carries `propagated` (segment ids) and
    `propagation_refused`; propagate=false approves this segment only.

    propagation="undo" | "unlink" | "link" does NOT approve: it manages the segment's repeats
    (POST /api/segments/{id}/propagation). undo restores the repeats this segment's last approval
    filled, only those still exactly as propagated (reply: restored, kept); unlink stops copying
    text to and from the segment; link resumes it."""
    mode = str(propagation or "").strip().lower()
    if mode:
        if mode not in ("undo", "unlink", "link"):
            raise ToolError("propagation must be undo, unlink or link.")
        pbody = {"undo": True} if mode == "undo" else {"linked": mode == "link"}
        async with _client() as client:
            pout = await _post_json(
                client,
                "/api/segments/%d/propagation" % int(segment_id),  # noqa: UP031
                pbody,
            )
        pout.pop("ok", None)
        return json.dumps(pout)
    body: dict = {}
    if acknowledge_placeholder_loss is True:
        body["acknowledge_placeholder_loss"] = True
    if propagate is False:
        body["propagate"] = False
    async with _client() as client:
        out = await _post_json(client, "/api/segments/%d/approve" % int(segment_id), body)  # noqa: UP031
    out.pop("ok", None)
    return json.dumps(out)


async def translate_text(
    text: str,
    target_locale: str,
    source_locale: str | None = None,
    provider: str | None = None,
    confirm: bool = False,
    idempotency_key: str | None = None,
) -> str:
    _require_confirmed("translate_text", confirm)
    body: dict = {"text": str(text or ""), "target_locale": str(target_locale or "").strip()}
    if str(source_locale or "").strip():
        body["source_locale"] = str(source_locale).strip()
    if str(provider or "").strip():
        body["provider"] = str(provider).strip()
    async with _client() as client:
        out = await _post_json(client, "/api/translate/text", body)
    out.pop("ok", None)
    return json.dumps(out)


# --- family D: keys and the Delta loop --------------------------------------
async def list_keys(
    project_id: int | None = None,
    key_id: int | None = None,
    namespace: str | None = None,
    tag: str | None = None,
    q: str | None = None,
    status: str | None = None,
    untranslated_in: str | None = None,
    cursor: int = 0,
    limit: int = 100,
    include_archived: bool = False,
) -> str:
    if key_id is None and project_id is None:
        raise ToolError("Pass project_id to page a catalogue, or key_id for one key.")
    async with _client() as client:
        if key_id is not None:
            body = await _get_json(client, "/api/keys/%d/values" % int(key_id))  # noqa: UP031
            return json.dumps({"key": body.get("key")})
        params: dict = {"cursor": int(cursor or 0), "limit": int(limit or 100)}
        for pname, value in (
            ("namespace", namespace),
            ("tag", tag),
            ("q", q),
            ("status", status),
            ("untranslated_in", untranslated_in),
        ):
            if str(value or "").strip():
                params[pname] = str(value).strip()
        if include_archived:
            params["archived"] = "1"
        body = await _get_json(client, "/api/projects/%d/keys" % int(project_id), params=params)  # noqa: UP031
    body.pop("ok", None)
    return json.dumps(body)


#: Field names upsert_keys may forward to PATCH /api/keys/{key_id}.
#:
#: AN ALLOWLIST, NOT A DENYLIST, AND THE REASON IS A WRITTEN PROMISE. The app's
#: own EDIT_FIELDS includes `is_archived`, and archiving a key REMOVES IT FROM
#: EVERY DOWNSTREAM BUILD FILE while the change feed reports op="updated". A
#: consumer that trusts key_changes therefore keeps shipping a string the product
#: has retired, and never sees the deletion it was promised. That would break
#: three things this wave wrote down: DELETE /api/keys/{key_id} in NEVER_COVER,
#: the restore-route exemption ("undo a delete an agent cannot perform"), and
#: key_changes' pinned "a deletion arrives as an entry with op=deleted, not as an
#: absence". `is_hidden` is refused with it: same effect on a build file, same
#: silence on the feed.
#:
#: A NEW app-side EDIT_FIELDS ENTRY IS THEREFORE REFUSED BY DEFAULT rather than
#: quietly forwarded - which is the whole point of the direction of this list.
#: `force` IS DELIBERATELY ABSENT. The app's own EDIT_FIELDS
#: accepts it, and `force=True` on a plural demotion (is_plural=false) DESTROYS the
#: key's non-`other` translated forms while the change feed still reports
#: op="updated" - the same silent-loss class as is_archived/is_hidden, only the
#: casualty is translations rather than a whole key. A human on the web grid or
#: `keys edit --single --force` on the CLI can accept that loss deliberately; an
#: LLM over MCP must not, so the flag is not forwarded. Guard-enforced (it is in
#: verify_mcp_parity's _DESTRUCTIVE_FIELD_NAMES, so re-adding it here goes red) and
#: regression-tested (verify_mcp_tools proves the edit seam refuses it and no
#: request body carries it).
_KEY_EDIT_FIELDS: frozenset = frozenset(
    {
        "name",
        "namespace",
        "context",
        "description",
        "base",
        "tags",
        "char_limit",
        "source_file",
        "custom",
        "is_plural",
        "plural_arg_name",
    }
)

#: The same allowlist for a BULK row. POST /api/projects/{pid}/keys reads
#: is_archived/is_hidden on both its create and its update path, so the bulk mode
#: is the identical escape hatch wearing an array's clothes - the review found it
#: on `fields`; it lives here too.
_KEY_ROW_FIELDS: frozenset = frozenset(
    {
        "name",
        "namespace",
        "context",
        "description",
        "base",
        "tags",
        "char_limit",
        "source_file",
        "custom",
        "is_plural",
        "plural_arg_name",
    }
)

#: Why each refused field is refused, so the message teaches instead of scolding.
_KEY_FIELD_REFUSALS: dict = {
    "is_archived": (
        "archiving removes a key from every downstream build file "
        "while the change feed still says op=updated - a silent "
        "deletion, and no MCP tool may delete a key"
    ),
    "is_hidden": (
        "hiding a key removes it from build output with no deletion entry on the change feed"
    ),
    "force": (
        "`force` accepts the destruction of a plural key's translated "
        "non-other forms on demotion; that loss is a human decision, made "
        "on the web grid or with `keys edit --single --force` on the CLI, "
        "and is deliberately not available over MCP"
    ),
}

#: The service refuses to demote a plural key whose non-`other` forms still hold a
#: translation (PluralDemotionRefused), because the demotion destroys those rows;
#: its verbatim message tells the caller to "re-run with force". True for a human,
#: a dead end over MCP - `force` is not forwarded (see _KEY_EDIT_FIELDS). This mark
#: is the stable phrase in that refusal, so the edit handler can append the one
#: fact that turns a loop into an actionable next step without adding app/ logic.
_PLURAL_REFUSAL_MARK = "translated plural forms"


def _plural_demotion_note(exc: ToolError) -> ToolError:
    """`exc` unchanged, unless it is the plural-demotion refusal - then the MCP
    reality is appended: `force` lives on the human surfaces, not here."""
    msg = str(exc)
    if _PLURAL_REFUSAL_MARK not in msg:
        return exc
    return ToolError(
        msg + " NOTE: `force` is deliberately unavailable over MCP - accepting the "
        "loss of a plural key's translated forms is a human decision, made on the "
        "web key grid or with `keys edit --single --force` on the CLI."
    )


def _allowed_key_fields(given: dict, allowed: frozenset, where: str) -> dict:
    """`given`, refused outright if it names anything outside `allowed`.

    Refuse rather than silently drop: an agent that asked to archive a key and got
    a cheerful 200 would report success for something that did not happen, which
    is worse than the refusal."""
    bad = sorted(k for k in given if k not in allowed)
    if bad:
        why = "; ".join(
            f"{k}: {_KEY_FIELD_REFUSALS[k]}"
            for k in bad
            if k in _KEY_FIELD_REFUSALS  # noqa: RUF100, UP032
        )
        raise ToolError(
            "{} does not accept {}. {}Allowed: {}.".format(
                where, ", ".join(bad), (why + ". ") if why else "", ", ".join(sorted(allowed))
            )
        )
    return given


async def upsert_keys(
    project_id: int | None = None,
    keys: list[dict] | None = None,
    key_id: int | None = None,
    locale: str | None = None,
    text: str | None = None,
    forms: dict | None = None,
    fields: dict | None = None,
) -> str:
    """Three writes, one task ("change these strings"), and the modes are mutually
    exclusive so a half-specified call is refused rather than guessed at."""
    modes = [
        keys is not None,
        key_id is not None and bool(str(locale or "").strip()),
        key_id is not None and fields is not None,
    ]
    if sum(1 for m in modes if m) != 1:
        raise ToolError(
            "Pick exactly one mode: bulk (project_id + keys), set one value "
            "(key_id + locale + text or forms), or edit metadata (key_id + fields)."
        )
    async with _client() as client:
        if modes[0]:
            if project_id is None:
                raise ToolError("Bulk mode needs project_id.")
            if not isinstance(keys, list):
                raise ToolError("`keys` must be a list of key objects.")
            rows = []
            for i, row in enumerate(keys):
                if not isinstance(row, dict):
                    raise ToolError("keys[%d] is not an object." % i)  # noqa: UP031
                rows.append(_allowed_key_fields(row, _KEY_ROW_FIELDS, "keys[%d]" % i))  # noqa: UP031
            out = await _post_json(
                client,
                "/api/projects/%d/keys" % int(project_id),  # noqa: UP031
                {"keys": rows},  # noqa: RUF100, UP031
            )
            # PARTIAL SUCCESS BY CONTRACT: `errors` rides back verbatim. A 200 is
            # not a promise that every key landed.
            out.pop("ok", None)
            return json.dumps(out)
        if modes[1]:
            if forms is None and text is None:
                raise ToolError("Pass text (simple key) or forms (plural key).")
            body = {"forms": forms} if forms is not None else {"text": text}
            resp = await _call(
                client,
                "PUT",
                f"/api/keys/{int(key_id)}/values/{_seg(str(locale).strip())}",
                json_body=body,
            )
        else:
            if not isinstance(fields, dict) or not fields:
                raise ToolError("Edit mode needs a non-empty `fields` object.")
            checked = _allowed_key_fields(fields, _KEY_EDIT_FIELDS, "`fields`")
            try:
                resp = await _call(client, "PATCH", "/api/keys/%d" % int(key_id), json_body=checked)  # noqa: UP031
            except ToolError as exc:
                # A plural demotion that would destroy translations comes back as
                # PluralDemotionRefused. Relay it verbatim, plus the fact that
                # `force` is a human-only lever over here.
                raise _plural_demotion_note(exc) from exc
        out = resp.json()
    out.pop("ok", None)
    return json.dumps(out)


async def import_keys(
    project_id: int,
    file_path: str | None = None,
    data: str | None = None,
    filename: str | None = None,
    fmt: str | None = None,
    locale: str | None = None,
    namespace: str | None = None,
) -> str:
    """MERGE ONLY, permanently.

    The API's import body accepts `cleanup`, which RETIRES every key absent from
    the uploaded file and emits a tombstone per key to every downstream CI
    consumer. That is a bulk delete. This tool must never expose it and never
    send it: DELETE /api/keys/{key_id} is a NEVER_COVER route, and the
    /api/keys/{key_id}/restore exemption is justified by "an agent cannot delete a
    key" - a cleanup import would falsify both. Guard-enforced (verify_mcp_parity's
    forbidden-property check) and regression-tested (verify_mcp_tools asserts no
    request body this tool sends carries the flag)."""
    text_data = str(data) if data is not None else None
    fname = str(filename or "").strip()
    if str(file_path or "").strip():
        _local_read_guard(file_path, "key file", read=False)
        raw = _read_local_file(file_path, "key file", await _upload_exts("key file"))
        payload = {"data_b64": base64.b64encode(raw).decode("ascii")}
        fname = fname or Path(str(file_path)).name
    elif text_data is not None:
        payload = {"data": text_data}
    else:
        raise ToolError("Nothing to import: pass file_path or data.")
    body = {**payload, "filename": fname or "upload.json"}
    for field, value in (("fmt", fmt), ("locale", locale), ("namespace", namespace)):
        if str(value or "").strip():
            body[field] = str(value).strip()
    async with _client() as client:
        out = await _post_json(client, "/api/projects/%d/keys/import" % int(project_id), body)  # noqa: UP031
    out.pop("ok", None)
    return json.dumps(out)


async def export_keys(
    project_id: int,
    locale: str | None = None,
    fmt: str = "json",
    namespace: str | None = None,
    status: str | None = None,
    empty_as: str | None = None,
    archived: bool = False,
) -> str:
    # NO `include_string_images` here BY DESIGN (Batch 14 Phase 1.5, gate round-1 H2). The
    # in-context image bundle is an OPERATOR download (web + /api + CLI); binding it to an MCP
    # tool would let an agent CAUSE the whole project's customer pixels to be pulled to disk in
    # one call -- the exact exfiltration surface SPEC-string-image-context.md 8.x and the
    # raw-serve route forbid. An agent has no image-export use case; the option is simply absent.
    # By the same token the export's structured error `code` (key_image_export_max, an /api + CLI
    # wire-contract field) has NO MCP surface: the capability it guards is not exposed here (L9).
    params: dict = {"fmt": str(fmt or "json").strip() or "json"}
    if str(locale or "").strip():
        params["locale"] = str(locale).strip()
    if str(namespace or "").strip():
        params["namespace"] = str(namespace).strip()
    # K9 (live-QA F5): the status filter is the fourth surface's parity gap -- MCP had
    # only project_id/locale/fmt/namespace while the route (and the CLI/web) also take
    # status/empty_as/archived. Forward all three; each is a thin, read-only query param.
    if str(status or "").strip():
        params["status"] = str(status).strip()
    if str(empty_as or "").strip():
        params["empty_as"] = str(empty_as).strip()
    if archived:
        params["archived"] = "1"
    async with _client() as client:
        resp = await _call(
            client,
            "GET",
            "/api/projects/%d/keys/export" % int(project_id),  # noqa: UP031
            params=params,  # noqa: RUF100, UP031
        )
        content = resp.content
        fname = _filename_from_disposition(resp.headers.get("content-disposition", ""))
        # The tag-guard facts live in headers on a raw-bytes response; relay them
        # rather than dropping the only channel this route has to report them.
        notes = {k: v for k, v in resp.headers.items() if k.lower().startswith("x-loc-")}
    if not fname:
        fname = "project{}.{}.{}".format(project_id, (locale or "source"), params["fmt"])
    written = _write_output(fname, content)
    return json.dumps(
        {
            **written,
            "project_id": int(project_id),
            "locale": str(locale or "").strip(),
            "export_notes": notes,
        }
    )


async def key_changes(
    project_id: int,
    since: int = 0,
    limit: int = 500,
    modified_after: str | None = None,
    after_id: int | None = None,
    locale: str | None = None,
) -> str:
    """Two mutually-exclusive modes (mirrors GET /api/projects/{pid}/changes):
    the CHANGELOG feed (`since` cursor, the ONLY mode with deletions) or the
    MODIFIED-AFTER feed (`modified_after` ISO-8601 UTC timestamp, current-state value
    rows changed since then, optional `locale`, NO deletions). To page modified_after,
    feed the returned `next_modified_after` straight back as `modified_after` while
    `has_more` is true -- it is a self-contained `ISO~id` cursor token, so you need not
    also thread `next_after_id` (that param still works if you prefer it). Passing both
    modes is an error server-side."""
    mod = str(modified_after or "").strip()
    if mod:
        params = {"modified_after": mod, "limit": int(limit or 500)}
        if after_id is not None:
            params["after_id"] = int(after_id)
        if str(locale or "").strip():
            params["locale"] = str(locale).strip()
    else:
        params = {"since": int(since or 0), "limit": int(limit or 500)}
    async with _client() as client:
        body = await _get_json(client, "/api/projects/%d/changes" % int(project_id), params=params)  # noqa: UP031
    body.pop("ok", None)
    return json.dumps(body)


#: file extension -> the MIME to declare on a base64 attach (the server still sniffs the
#: magic bytes and rejects a mismatch, so this is only the DECLARED type).
_KEY_IMAGE_MIME_BY_EXT = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
}


async def manage_key_image(
    action: str,
    key_id: int,
    image_id: int | None = None,
    caption: str | None = None,
    file_path: str | None = None,
    image_base64: str | None = None,
    mime: str | None = None,
) -> str:
    """Attach / delete / set-primary a string key's in-context image. Image BYTES ride as
    base64 on the attach action (never a URL -- no SSRF); pass file_path (read + encoded
    for you) OR image_base64 directly. Delete is a reversible soft-delete."""
    act = str(action or "").strip().lower()
    if act not in ("attach", "delete", "set_primary"):
        raise ToolError('action must be one of "attach", "delete", "set_primary".')
    async with _client() as client:
        if act == "attach":
            if str(file_path or "").strip():
                raw = _read_local_file(file_path, "image")
                b64 = base64.b64encode(raw).decode("ascii")
                ext = Path(str(file_path)).suffix.lower()
                declared = str(mime or "").strip() or _KEY_IMAGE_MIME_BY_EXT.get(ext, "image/png")
            elif str(image_base64 or "").strip():
                b64 = str(image_base64).strip()
                declared = str(mime or "").strip() or "image/png"
            else:
                raise ToolError("attach needs file_path or image_base64 (a PNG or JPEG).")
            body: dict = {"image_base64": b64, "mime": declared}
            if str(caption or "").strip():
                body["caption"] = str(caption).strip()
            out = await _post_json(client, "/api/keys/%d/images" % int(key_id), body)  # noqa: UP031
            out.pop("ok", None)
            return json.dumps(out)
        if image_id is None:
            raise ToolError("%s needs image_id (from list_key_images)." % act)  # noqa: UP031
        if act == "delete":
            resp = await _call(
                client,
                "DELETE",
                "/api/keys/%d/images/%d" % (int(key_id), int(image_id)),  # noqa: UP031
            )
        else:
            resp = await _call(
                client,
                "POST",
                "/api/keys/%d/images/%d/primary" % (int(key_id), int(image_id)),  # noqa: UP031
            )
        out = resp.json()
    out.pop("ok", None)
    return json.dumps(out)


async def list_key_images(key_id: int) -> str:
    """List a string key's in-context images (metadata only -- the raw bytes never ride a
    tool result). The ids feed manage_key_image's delete / set_primary actions."""
    async with _client() as client:
        out = await _get_json(client, "/api/keys/%d/images" % int(key_id))  # noqa: UP031
    out.pop("ok", None)
    return json.dumps(out)


# --- family E: workflows and runs --------------------------------------------
async def list_workflows(workflow_id: int | None = None) -> str:
    async with _client() as client:
        if workflow_id is not None:
            body = await _get_json(client, "/api/workflows/%d" % int(workflow_id))  # noqa: UP031
            body.pop("ok", None)
            return json.dumps({"workflow": body})
        body = await _get_json(client, "/api/workflows")
    return json.dumps({"workflows": body.get("workflows", [])})


async def run_workflow(
    mode: str,
    workflow_id: int | None = None,
    document_id: int | None = None,
    locale: str | None = None,
    locales: list[str] | None = None,
    batch_id: int | None = None,
    max_parallel: int | None = None,
    fail_fast: bool = False,
    confirm: bool = False,
    idempotency_key: str | None = None,
) -> str:
    """Launch with a bounded wait (45 seconds across launch and status polling).

    All four modes launch with wait=false, then poll work_status every 2.5 seconds
    within the remaining budget. Slow runs return an id, never a wait-expiry error.
    May return running: done:false, status and next name the work_status call to make.
    done:true means terminal or paused, not necessarily successful. A paused run is
    returned as-is; resuming it is a human's act. Applies to all four modes.
    """
    _require_confirmed("run_workflow", confirm)
    m = str(mode or "").strip()
    loc_list = _clean_locales(locales)
    deadline = asyncio.get_running_loop().time() + _WORKFLOW_WAIT_S

    async def _bounded_call(*args, **kwargs):
        remaining = max(0.001, deadline - asyncio.get_running_loop().time())
        kwargs["timeout"] = httpx.Timeout(remaining)
        try:
            async with asyncio.timeout(remaining):
                return await _call(*args, **kwargs)
        except TimeoutError as exc:
            raise ToolError(
                "Workflow launch transport timed out; work may still be running. "
                "Use work_status to inspect the document or batch before launching again."
            ) from exc

    async with _client() as client:
        if m == "run":
            if workflow_id is None or document_id is None or not str(locale or "").strip():
                raise ToolError("mode=run needs workflow_id, document_id and locale.")
            resp = await _bounded_call(
                client,
                "POST",
                "/api/workflows/%d/run" % int(workflow_id),  # noqa: UP031
                json_body={
                    "doc_id": int(document_id),
                    "locale": str(locale).strip(),
                    "wait": False,
                },
            )
        elif m == "run_batch":
            if workflow_id is None or document_id is None:
                raise ToolError("mode=run_batch needs workflow_id and document_id.")
            body: dict = {"doc_id": int(document_id), "wait": False}
            if loc_list:
                body["locales"] = loc_list
            if max_parallel is not None:
                body["max_parallel"] = int(max_parallel)
            if fail_fast:
                body["fail_fast"] = True
            resp = await _bounded_call(
                client,
                "POST",
                "/api/workflows/%d/run-batch" % int(workflow_id),  # noqa: UP031
                json_body=body,
            )
        elif m == "rerun":
            if batch_id is None:
                raise ToolError("mode=rerun needs batch_id.")
            rerun_locales = loc_list or _clean_locales([locale] if locale else [])
            if not rerun_locales:
                raise ToolError("mode=rerun needs locale or locales.")
            resp = await _bounded_call(
                client,
                "POST",
                "/api/batches/%d/rerun" % int(batch_id),  # noqa: UP031
                json_body={"locales": rerun_locales, "wait": False},
            )
        elif m == "retry_failed":
            if batch_id is None:
                raise ToolError("mode=retry_failed needs batch_id.")
            resp = await _bounded_call(
                client,
                "POST",
                "/api/batches/%d/retry-failed" % int(batch_id),  # noqa: UP031
                json_body={"wait": False},
            )
        else:
            raise ToolError(f"unknown mode {m!r}; use run, run_batch, rerun or retry_failed.")
        out = resp.json()
    identity = (
        {"run_id": out["run_id"]} if out.get("run_id") else {"batch_id": out.get("id", batch_id)}
    )
    poll_options = {"detail": True} if m in {"rerun", "retry_failed"} else {}
    terminal = {"completed", "failed", "cancelled", "paused", "partial"}
    while not (m == "retry_failed" and str(out.get("status", "")).lower() in terminal):
        remaining = deadline - asyncio.get_running_loop().time()
        if remaining <= 0:
            break
        try:
            async with asyncio.timeout(remaining):
                out.update(json.loads(await work_status(**identity, **poll_options)))
        except TimeoutError:
            break
        if str(out.get("status", "")).lower() in terminal:
            break
        await asyncio.sleep(min(_POLL_GAP_S, max(0, deadline - asyncio.get_running_loop().time())))
    out["done"] = str(out.get("status", "")).lower() in terminal
    if not out["done"]:
        out.setdefault("status", "running")
        key, value = next(iter(identity.items()))
        out["next"] = f"Call work_status with {key}={value} to check progress."
    out.pop("ok", None)
    return json.dumps({"mode": m, **out})


async def work_status(
    run_id: int | None = None,
    batch_id: int | None = None,
    document_id: int | None = None,
    detail: bool = False,
    step_diff: bool = False,
    from_step_run_id: int | None = None,
    to_step_run_id: int | None = None,
) -> str:
    given = [v is not None for v in (run_id, batch_id, document_id)]
    if sum(1 for g in given if g) != 1:
        raise ToolError("Pass exactly one of run_id, batch_id or document_id.")
    if (step_diff or from_step_run_id is not None or to_step_run_id is not None) and (
        run_id is None
    ):
        raise ToolError("step_diff (and from/to step run ids) needs run_id.")
    async with _client() as client:
        if run_id is not None and (
            step_diff or from_step_run_id is not None or to_step_run_id is not None
        ):
            # GitHub #340: the per-segment word diff of two step runs of ONE run.
            params = {}
            if from_step_run_id is not None:
                params["from"] = int(from_step_run_id)
            if to_step_run_id is not None:
                params["to"] = int(to_step_run_id)
            body = await _get_json(
                client,
                "/api/runs/%d/step-diff" % int(run_id),  # noqa: UP031
                params=params or None,
            )
        elif run_id is not None:
            params = {"findings": "1", "visits": "1"} if detail else None
            body = await _get_json(client, "/api/runs/%d" % int(run_id), params=params)  # noqa: UP031
        elif batch_id is not None:
            params = {"detail": "steps"} if detail else None
            body = await _get_json(client, "/api/batches/%d" % int(batch_id), params=params)  # noqa: UP031
        else:
            params = {"detail": "segments"} if detail else None
            body = await _get_json(
                client,
                "/api/documents/%d/segments" % int(document_id),  # noqa: UP031
                params=params,  # noqa: RUF100, UP031
            )
    body.pop("ok", None)
    return json.dumps(body)


async def ledger_summary(
    action_id: str | None = None,
    project_id: int | None = None,
    document_id: int | None = None,
    batch_id: int | None = None,
    status: str | None = None,
    since: str | None = None,
    until: str | None = None,
    failures: bool = False,
    limit: int = 50,
) -> str:
    """run-190 Item C: the LLM Ledger diagnostics, BODIES WITHHELD. With `action_id` it
    returns one action's per-run summary (counts, statuses, error classes, finish reasons,
    spend, timings, the per-call failure shape); without it, a newest-first LIST filtered by
    project_id / document_id / batch_id / status / since / until / failures / limit. NEVER
    returns a prompt or response body (the raw call bodies stay host-only) — it is a thin
    client of the two GET /api/prompt-tap/actions[...] routes, org-scoped server-side by the
    token. The action-list response also includes read-only ledger_status (envelope
    recording, raw capture, retained file counts/date and frozen age-out). No disable
    or purge tool is offered."""
    async with _client() as client:
        if action_id is not None:
            # _seg() percent-encodes the model-supplied action id into ONE path segment so a
            # `../..` can never cross a path boundary to a route this tool never declared.
            body = await _get_json(
                client, f"/api/prompt-tap/actions/{_seg(str(action_id).strip())}/summary"
            )
        else:
            params: dict = {"limit": int(limit)}
            if project_id is not None:
                params["project_id"] = int(project_id)
            if document_id is not None:
                params["document_id"] = int(document_id)
            if batch_id is not None:
                params["batch_id"] = int(batch_id)
            if status:
                params["status"] = status
            if since:
                params["since"] = since
            if until:
                params["until"] = until
            if failures:
                params["failures"] = "1"
            body = await _get_json(client, "/api/prompt-tap/actions", params=params)
    body.pop("ok", None)
    return json.dumps(body)


async def ledger_runs(
    run_id: str | None = None,
    since: str | None = None,
    until: str | None = None,
    failures: bool = False,
    limit: int = 50,
) -> str:
    """ROADMAP #6: the LLM Ledger by TRANSLATION RUN, BODIES WITHHELD. With `run_id` it
    returns one run's calls grouped by step then unit (per-call metadata digests: lengths,
    sha256, a 24-char head only for a body over 48 chars); without it, a newest-first LIST
    filtered by since / until /
    failures / limit. NEVER returns a prompt or response body (the raw call bodies stay
    host-only) -- a thin client of the two GET /api/prompt-tap/runs[...] routes, org-scoped by
    the token."""
    async with _client() as client:
        if run_id is not None:
            # _seg() percent-encodes the model-supplied run id into ONE path segment so a
            # `../..` can never cross a path boundary to a route this tool never declared.
            body = await _get_json(client, f"/api/prompt-tap/runs/{_seg(str(run_id).strip())}")
        else:
            params: dict = {"limit": int(limit)}
            if since:
                params["since"] = since
            if until:
                params["until"] = until
            if failures:
                params["failures"] = "1"
            body = await _get_json(client, "/api/prompt-tap/runs", params=params)
    body.pop("ok", None)
    return json.dumps(body)


#: The step fields create_workflow / edit_workflow may forward, allowlisted (REFUSED,
#: not silently dropped, like the key allowlists) -- the tool descriptions promise
#: exactly that, so the allowlist must match what the ROUTE actually accepts, per
#: action, or the promise is a lie (adversarial review M3).
#:
#: ADD surface (create_workflow's step objects + edit_workflow action=add_step): the
#: `workflow add-step` flags. It has NO assignee_email (the CLI add-step parser has no
#: --assignee-email) and none of the edit-only toggles -- the add route drops those, so
#: they must be refused here, not accepted-then-dropped.
_WORKFLOW_STEP_FIELDS: frozenset = frozenset(
    {
        "name",
        "step_type",
        "provider",
        "model",
        "fallback_provider",
        "fallback_model",
        "prompt_text",
        "prompt_template_id",
        "chunk_policy",
        "redact_pii",
        "adaptive_feedback",
        "adaptive_feedback_window",
        "doc_context_prepass",
        "qe_gate",
        "qe_threshold",
        "qe_provider",
        # QE auto-fix (Train B): opt-in post-editor loop on a Jev QE gate (the route
        # role-checks the fixer and clamps the rounds 1-5).
        "qe_autofix",
        "qe_autofix_provider",
        "qe_autofix_max_rounds",
        "qe_autofix_cross_check",
        "lqa_threshold",
        "tm_examples",
        "tm_examples_window",
        "use_image_context",
        # Terminology T3 (SPEC §9-T3): the concept-glossary renderer toggle + caps flow
        # generically through the step objects (llm_agent-only; refused elsewhere).
        "concept_glossary_render",
        "glossary_concept_cap",
        "glossary_token_cap",
        "tm_protect",
        # run-190 Item D / M5: the over-translation guard opt-out flows generically through
        # the step objects (llm_agent-only tristate; the service refuses it elsewhere).
        "over_translation_guard",
        "memory_mode",
        # Batch 15 T1 (SPEC-tm-match-step-config §8.E): the six tm_match config fields
        # flow generically through create_workflow's step objects (tm_match-only; the
        # service refuses one set on a non-tm_match step).
        "tm_fill_threshold",
        "tm_fuzzy_lock_mode",
        "tm_lock_ice",
        "tm_hundreds_enter_lqa",
        "tm_context_floor",
        "tm_pool_selection",
        "glossary_id",
        "run_condition",
        "key",
        "branch",
        "max_visits",
        "parallel_group",
        "vendor_id",
        "external_wait_hours",
    }
)
#: EDIT surface (edit_workflow action=edit_step): the add surface PLUS the fields the
#: `edit-step` CLI adds -- assignee_email and the deliberate toggles clear_branch /
#: no_parallel_group / pin / unpin. Kept as its OWN literal (not a `|` union) so the
#: accounting guard's AST reader can read it.
_WORKFLOW_EDIT_FIELDS: frozenset = frozenset(
    {
        "name",
        "step_type",
        "provider",
        "model",
        "fallback_provider",
        "fallback_model",
        "prompt_text",
        "prompt_template_id",
        "chunk_policy",
        "redact_pii",
        "adaptive_feedback",
        "adaptive_feedback_window",
        "doc_context_prepass",
        "qe_gate",
        "qe_threshold",
        "qe_provider",
        # QE auto-fix (Train B): opt-in post-editor loop on a Jev QE gate (the route
        # role-checks the fixer and clamps the rounds 1-5).
        "qe_autofix",
        "qe_autofix_provider",
        "qe_autofix_max_rounds",
        "qe_autofix_cross_check",
        "lqa_threshold",
        "tm_examples",
        "tm_examples_window",
        "use_image_context",
        # Terminology T3 (SPEC §9-T3): the concept-glossary renderer toggle + caps flow
        # generically through the step objects (llm_agent-only; refused elsewhere).
        "concept_glossary_render",
        "glossary_concept_cap",
        "glossary_token_cap",
        "tm_protect",
        # run-190 Item D / M5: the over-translation guard opt-out on the edit surface too.
        "over_translation_guard",
        "memory_mode",
        # Batch 15 T1 (SPEC-tm-match-step-config §8.E): the six tm_match config fields on
        # the edit surface too (edit_workflow action=edit_step).
        "tm_fill_threshold",
        "tm_fuzzy_lock_mode",
        "tm_lock_ice",
        "tm_hundreds_enter_lqa",
        "tm_context_floor",
        "tm_pool_selection",
        "glossary_id",
        "assignee_email",
        "run_condition",
        "key",
        "branch",
        "max_visits",
        "parallel_group",
        "vendor_id",
        "external_wait_hours",
        "clear_branch",
        "no_parallel_group",
        "pin",
        "unpin",
    }
)


async def create_workflow(
    name: str,
    scope: str = "global",
    customer_id: int | None = None,
    steps: list[dict] | None = None,
) -> str:
    """Author a workflow (+ optionally its ordered steps) in one call. Bills nothing.

    PARTIAL SUCCESS BY CONTRACT: the workflow is created, then each step is added in
    order; a step the graph rejects is reported in `steps[].error` and the rest still
    land, so the response accounts for EVERY step. Re-run edit_workflow to fix a
    rejected one -- nothing is rolled back."""
    checked_steps = []
    if steps is not None:
        if not isinstance(steps, list):
            raise ToolError("`steps` must be a list of step objects.")
        for i, st in enumerate(steps):
            if not isinstance(st, dict):
                raise ToolError(f"steps[{i}] is not an object.")
            checked_steps.append(_allowed_key_fields(st, _WORKFLOW_STEP_FIELDS, f"steps[{i}]"))
    async with _client() as client:
        body: dict = {"name": name, "scope": str(scope or "global")}
        if customer_id is not None:
            body["customer_id"] = int(customer_id)
        created = await _post_json(client, "/api/workflows", body)
        wid = created.get("id")
        results = []
        if checked_steps:
            for i, checked in enumerate(checked_steps):
                try:
                    out = await _post_json(client, "/api/workflows/%d/steps" % int(wid), checked)  # noqa: UP031
                    row = {
                        "index": i,
                        "ok": True,
                        "step_id": out.get("id"),
                        "order_index": out.get("order_index"),
                    }
                    if out.get("warnings"):
                        row["warnings"] = out["warnings"]
                    results.append(row)
                except ToolError as exc:
                    results.append({"index": i, "ok": False, "error": str(exc)})
        created.pop("ok", None)
        return json.dumps(
            {
                **created,
                "steps": results,
                "steps_added": sum(1 for r in results if r["ok"]),
                "steps_failed": sum(1 for r in results if not r["ok"]),
            }
        )


async def _workflow_system_prompt_action(act: str, workflow_id: int, key, body) -> str:
    """edit_workflow's system prompt actions (#630): the workflow's two editable heads. Thin
    forwards of the /api routes, which own the admin-or-manager gate, the feature flag and the
    audit."""
    wid = int(workflow_id)
    async with _client() as client:
        if act == "get_system_prompts":
            out = await _get_json(client, "/api/workflows/%d/system-prompts" % wid)  # noqa: UP031
            out.pop("ok", None)
            return json.dumps({"action": act, **out})
        k = str(key or "").strip()
        if k not in ("step_head", "mt_head"):
            raise ToolError(f"action={act} needs key: step_head or mt_head.")
        path = f"/api/workflows/{int(workflow_id)}/system-prompts/{_seg(k)}"
        if act == "set_system_prompt":
            if not isinstance(body, str):
                raise ToolError("action=set_system_prompt needs body (the new text).")
            resp = await _call(client, "PUT", path, json_body={"body": body})
            out = resp.json()
        else:  # restore_default_system_prompt
            out = await _post_json(client, path + "/restore-default", {})
    out.pop("ok", None)
    return json.dumps({"action": act, **out})


async def edit_workflow(
    action: str,
    workflow_id: int,
    step_id: int | None = None,
    fields: dict | None = None,
    position: int | None = None,
    before: str | None = None,
    after: str | None = None,
    parallel_group: str | None = None,
    clear_parallel_group: bool = False,
    key: str | None = None,
    body: str | None = None,
) -> str:
    """Change a workflow's steps or its system prompt, one action per call. Bills nothing.

    get_system_prompts / set_system_prompt / restore_default_system_prompt (#630) read, set
    and restore the workflow's two editable system prompt heads (key step_head or mt_head);
    the server enforces the same gate (admin or manager), feature flag and audit as the web page.

    add_step / edit_step forward `fields` (the step field map, allowlist-checked); an
    edit is PARTIAL (only the keys you pass change). Step actions need can_tune_ai (admin or
    linguist); a manager may edit_step with `fields` holding only prompt_text (an empty
    prompt_text restores the step's default prompt). delete_step / move_step take a
    step_id. Every action re-validates the whole graph fail-closed and is refused while
    the workflow has an in-flight run -- a WorkflowError rides back verbatim."""
    act = str(action or "").strip()
    known = (
        "add_step",
        "edit_step",
        "delete_step",
        "move_step",
        "get_system_prompts",
        "set_system_prompt",
        "restore_default_system_prompt",
    )
    if act not in known:
        raise ToolError("unknown action {!r}; use one of: {}".format(act, ", ".join(known)))
    if act.endswith("system_prompt") or act == "get_system_prompts":
        return await _workflow_system_prompt_action(act, workflow_id, key, body)
    if act != "add_step" and step_id is None:
        raise ToolError(f"action={act} needs step_id.")
    checked = None
    if fields is not None:
        if not isinstance(fields, dict):
            raise ToolError("`fields` must be an object.")
        # add_step hits the ADD route (no assignee_email / edit-only toggles); edit_step
        # hits the EDIT route (accepts them). Validate against the surface the chosen
        # action actually forwards, so an unaccepted field is REFUSED, never dropped (M3).
        allow = _WORKFLOW_EDIT_FIELDS if act == "edit_step" else _WORKFLOW_STEP_FIELDS
        checked = _allowed_key_fields(fields, allow, "`fields`")
    async with _client() as client:
        if act == "add_step":
            out = await _post_json(
                client,
                "/api/workflows/%d/steps" % int(workflow_id),  # noqa: UP031
                checked or {},  # noqa: RUF100, UP031
            )
        elif act == "edit_step":
            if not checked:
                raise ToolError("action=edit_step needs a non-empty `fields` object.")
            out = await _post_json(
                client,
                "/api/workflows/%d/steps/%d/edit" % (int(workflow_id), int(step_id)),  # noqa: UP031
                checked,
            )
        elif act == "delete_step":
            out = await _post_json(
                client,
                "/api/workflows/%d/steps/%d/delete" % (int(workflow_id), int(step_id)),  # noqa: UP031
                {},  # noqa: RUF100, UP031
            )
        else:  # move_step
            move_body: dict = {}
            if position is not None:
                move_body["position"] = int(position)
            elif str(before or "").strip():
                move_body["before"] = str(before).strip()
            elif str(after or "").strip():
                move_body["after"] = str(after).strip()
            else:
                raise ToolError("action=move_step needs position, before or after.")
            if clear_parallel_group:
                move_body["clear_parallel_group"] = True
            elif str(parallel_group or "").strip():
                move_body["parallel_group"] = str(parallel_group).strip()
            out = await _post_json(
                client,
                "/api/workflows/%d/steps/%d/move" % (int(workflow_id), int(step_id)),  # noqa: UP031
                move_body,
            )
    out.pop("ok", None)
    return json.dumps({"action": act, **out})


async def cancel_workflow_run(run_id: int) -> str:
    """Cancel an in-flight run (pending/running/paused -> cancelled). TERMINAL and NOT
    resumable; bills/refunds nothing. An already-terminal run rides back as a clean error
    (POST /api/runs/{id}/cancel returns 409 -> _post_json raises)."""
    async with _client() as client:
        out = await _post_json(client, "/api/runs/%d/cancel" % int(run_id), {})  # noqa: UP031
    out.pop("ok", None)
    return json.dumps(out)


# --- family F: language assets ------------------------------------------------
#: Term-metadata keys edit_glossary may forward on add_term / edit_term.
#:
#: SAME DIRECTION AS THE KEY ALLOWLISTS: allowlist, not denylist, refuse rather
#: than drop. The app's own `glossary.clean_meta` allowlists these very keys and
#: SILENTLY drops the rest - but a silent drop is exactly the failure mode the
#: wave-1 review flagged (an agent that asked for something and got a cheerful
#: 200 reports success for a thing that did not happen). So the refusal happens
#: here, by name, before the wire. Mirrors app/services/glossary._GLOSSARY_META_COLS.
_GLOSSARY_TERM_META_FIELDS: frozenset = frozenset(
    {
        "part_of_speech",
        "definition",
        "usage_example",
        "subject_field",
        "notes",
    }
)


async def list_assets(
    view: str = "glossaries",
    glossary_id: int | None = None,
    style_guide_id: int | None = None,
    customer_id: int | None = None,
    status: str | None = None,
    fmt: str = "csv",
    dialect: str = "tbx-basic",
    strict_portable: bool = False,
    cursor: int | None = None,
    limit: int | None = None,
    offset: int | None = None,
) -> str:
    v = str(view or "glossaries").strip() or "glossaries"
    needs_gid = ("glossary", "candidates", "glossary_export", "concepts")
    if v in needs_gid and glossary_id is None:
        raise ToolError(f"view={v} needs glossary_id.")
    if v == "style_guide" and style_guide_id is None:
        raise ToolError("view=style_guide needs style_guide_id.")
    async with _client() as client:
        if v == "glossaries":
            params = {"customer": int(customer_id)} if customer_id is not None else None
            body = await _get_json(client, "/api/glossaries", params=params)
            return json.dumps({"glossaries": body.get("glossaries", [])})
        if v == "glossary":
            params = {
                k: value
                for k, value in (("cursor", cursor), ("limit", limit), ("offset", offset))
                if value is not None
            }
            body = await _get_json(client, "/api/glossaries/%d" % int(glossary_id), params=params)  # noqa: UP031
            body.pop("ok", None)
            return json.dumps({"glossary": body})
        if v == "candidates":
            params = {"status": str(status).strip()} if str(status or "").strip() else None
            body = await _get_json(
                client,
                "/api/glossaries/%d/candidates" % int(glossary_id),  # noqa: UP031
                params=params,  # noqa: RUF100, UP031
            )
            body.pop("ok", None)
            return json.dumps(body)
        if v == "concepts":
            # SPEC-terminology-backend §7.5: the concept/term store as a
            # glossary-concept/v1 document.
            body = await _get_json(
                client,
                "/api/glossaries/%d/concepts" % int(glossary_id),  # noqa: UP031
            )
            body.pop("ok", None)
            return json.dumps(body)
        if v == "glossary_export":
            use_fmt = str(fmt or "csv").strip().lower() or "csv"
            params = {"format": use_fmt}
            if use_fmt == "tbx":
                # TBX-Basic v3 (DCA) dialect controls (§6.4/§6.5): dialect tbx-basic
                # (default, los: private ns) or tbx-core; strict_portable drops los:.
                params["dialect"] = str(dialect or "tbx-basic").strip().lower() or "tbx-basic"
                if strict_portable:
                    params["strict_portable"] = "1"
            body = await _get_json(
                client,
                "/api/glossaries/%d/export" % int(glossary_id),  # noqa: UP031
                params=params,  # noqa: RUF100, UP031
            )
            data = base64.b64decode(body.get("csv_b64") or "")
            written = _write_output(
                "glossary%d.%s" % (int(glossary_id), body.get("format", use_fmt)),  # noqa: UP031
                data,  # noqa: RUF100, UP031
            )
            return json.dumps(
                {
                    **written,
                    "glossary_id": int(glossary_id),
                    "format": body.get("format", use_fmt),
                    # M1: name the los: field types a portable / tbx-core export dropped.
                    "dropped_fields": body.get("dropped_fields") or [],
                }
            )
        if v == "style_guides":
            body = await _get_json(client, "/api/style-guides")
            return json.dumps({"style_guides": body.get("style_guides", [])})
        if v == "style_guide":
            body = await _get_json(client, "/api/style-guides/%d" % int(style_guide_id))  # noqa: UP031
            body.pop("ok", None)
            return json.dumps({"style_guide": body})
    raise ToolError(
        f"unknown view {v!r}; use glossaries, glossary, candidates, "
        "glossary_export, style_guides or style_guide."
    )


async def edit_glossary(
    action: str,
    glossary_id: int | None = None,
    name: str | None = None,
    customer_id: int | None = None,
    content_type_id: int | None = None,
    source_term: str | None = None,
    target_term: str | None = None,
    source: str | None = None,
    target: str | None = None,
    dnt: bool | None = None,
    case_sensitive: bool | None = None,
    meta: dict | None = None,
    entry_id: int | None = None,
    candidate_id: int | None = None,
    file_path: str | None = None,
    data: str | None = None,
    filename: str | None = None,
    pairs: list[str] | None = None,
    concept_id: int | None = None,
    term_id: int | None = None,
    term: str | None = None,
    locale: str | None = None,
    status: str | None = None,
    term_type: str | None = None,
    audience: str | None = None,
    definition: str | None = None,
    domain: str | None = None,
    notes: str | None = None,
    term_ids: list[int] | None = None,
    strict: bool | None = None,
    concepts: bool | None = None,
) -> str:
    act = str(action or "").strip()
    known = (
        "create",
        "add_term",
        "edit_term",
        "import",
        "approve",
        "unapprove",
        "set_pairs",
        "set_content_type",
        "accept_candidate",
        "drop_candidate",
        "add_concept",
        "edit_concept",
        "delete_concept",
        "add_concept_term",
        "edit_concept_term",
        "delete_concept_term",
        "set_audience",
    )
    if act not in known:
        raise ToolError("unknown action {!r}; use one of: {}".format(act, ", ".join(known)))
    if act != "create" and glossary_id is None:
        raise ToolError(f"action={act} needs glossary_id.")
    clean_meta = None
    if meta is not None:
        if not isinstance(meta, dict):
            raise ToolError("`meta` must be an object.")
        clean_meta = _allowed_key_fields(meta, _GLOSSARY_TERM_META_FIELDS, "`meta`")
    async with _client() as client:
        if act == "create":
            if not str(name or "").strip():
                raise ToolError("action=create needs a name.")
            body: dict = {"name": str(name).strip()}
            if customer_id is not None:
                body["customer_id"] = int(customer_id)
            if content_type_id is not None:
                body["content_type_id"] = int(content_type_id)
            out = await _post_json(client, "/api/glossaries", body)
        elif act == "add_term":
            if not str(source_term or "").strip():
                raise ToolError("action=add_term needs source_term.")
            body = {
                "source_term": str(source_term).strip(),
                "target_term": str(target_term or ""),
                "source": str(source or "").strip(),
                "target": str(target or "").strip(),
                "dnt": bool(dnt),
            }
            if clean_meta:
                body["meta"] = clean_meta
            out = await _post_json(client, "/api/glossaries/%d/terms" % int(glossary_id), body)  # noqa: UP031
        elif act == "edit_term":
            if entry_id is None:
                raise ToolError("action=edit_term needs entry_id.")
            # PRESENT-ONLY SEMANTICS, deliberately: the API updates only the keys
            # in the body, so an omitted target_term/dnt must stay omitted here -
            # sending dnt=false for a caller who never mentioned it would CLEAR a
            # do-not-translate flag nobody asked to touch.
            body = {}
            if target_term is not None:
                body["target_term"] = str(target_term)
            if dnt is not None:
                body["dnt"] = bool(dnt)
            if case_sensitive is not None:
                body["case_sensitive"] = bool(case_sensitive)
            if clean_meta is not None:
                body["meta"] = clean_meta
            if not body:
                raise ToolError(
                    "action=edit_term with nothing to change: pass target_term, dnt, "
                    "case_sensitive or meta."
                )
            out = await _post_json(
                client,
                "/api/glossaries/%d/terms/%d/edit" % (int(glossary_id), int(entry_id)),  # noqa: UP031
                body,  # noqa: RUF100, UP031
            )
        elif act == "import":
            if str(file_path or "").strip():
                raw = _read_local_file(file_path, "glossary file")
                fname = str(filename or "").strip() or Path(str(file_path)).name
            elif data is not None:
                raw = str(data).encode("utf-8")
                fname = str(filename or "").strip() or "glossary.csv"
            else:
                raise ToolError("action=import needs file_path or data.")
            if not str(source or "").strip() or not str(target or "").strip():
                raise ToolError("action=import needs source and target locales.")
            out = await _post_json(
                client,
                "/api/glossaries/%d/import" % int(glossary_id),  # noqa: UP031
                {
                    "csv_b64": base64.b64encode(raw).decode("ascii"),
                    "source": str(source).strip(),
                    "target": str(target).strip(),
                    "filename": fname,
                    "strict": bool(strict),
                    "concepts": bool(concepts),
                },
            )
        elif act in ("approve", "unapprove"):
            out = await _post_json(client, f"/api/glossaries/{int(glossary_id)}/{act}", {})
        elif act == "set_pairs":
            cleaned = [str(p).strip() for p in (pairs or []) if str(p).strip()]
            if not cleaned:
                raise ToolError(
                    'action=set_pairs needs a non-empty pairs list ("SRC:TGT" strings).'
                )
            out = await _post_json(
                client,
                "/api/glossaries/%d/pairs" % int(glossary_id),  # noqa: UP031
                {"pairs": cleaned},  # noqa: RUF100, UP031
            )
        elif act == "set_content_type":
            out = await _post_json(
                client,
                "/api/glossaries/%d/content-type" % int(glossary_id),  # noqa: UP031
                {
                    "content_type_id": (
                        int(content_type_id) if content_type_id is not None else None
                    )
                },
            )
        elif act in ("accept_candidate", "drop_candidate"):
            if candidate_id is None:
                raise ToolError(f"action={act} needs candidate_id.")
            verb = "accept" if act == "accept_candidate" else "drop"
            body = {}
            if verb == "accept" and target_term is not None:
                body["target_term"] = str(target_term)
            out = await _post_json(
                client,
                f"/api/glossaries/{int(glossary_id)}/candidates/{int(candidate_id)}/{verb}",
                body,
            )
        else:
            # SPEC-terminology-backend §7.5: the concept/term store editor. Every write
            # delegates to the /api/glossaries/{gid}/concepts/... routes (which delegate
            # to the shared glossary_concepts service), so this stays pure wire plumbing.
            out = await _edit_concept_store(
                client,
                act,
                int(glossary_id),
                concept_id=concept_id,
                term_id=term_id,
                term=term,
                locale=locale,
                status=status,
                term_type=term_type,
                audience=audience,
                definition=definition,
                domain=domain,
                notes=notes,
                dnt=dnt,
                term_ids=term_ids,
            )
    out.pop("ok", None)
    return json.dumps({"action": act, **out})


async def _edit_concept_store(
    client,
    act: str,
    glossary_id: int,
    *,
    concept_id=None,
    term_id=None,
    term=None,
    locale=None,
    status=None,
    term_type=None,
    audience=None,
    definition=None,
    domain=None,
    notes=None,
    dnt=None,
    term_ids=None,
) -> dict:
    """Route one concept/term-store write to its /api route (SPEC-terminology-backend §7.5)."""
    base = "/api/glossaries/%d/concepts" % int(glossary_id)  # noqa: UP031
    if act == "add_concept":
        body: dict = {}
        for k, v in (("definition", definition), ("domain", domain), ("notes", notes)):
            if v is not None:
                body[k] = str(v)
        return await _post_json(client, base, body)
    if act in ("edit_concept", "delete_concept", "add_concept_term") and concept_id is None:
        raise ToolError(f"action={act} needs concept_id.")
    if act == "edit_concept":
        body = {}
        for k, v in (("definition", definition), ("domain", domain), ("notes", notes)):
            if v is not None:
                body[k] = str(v)
        return await _post_json(client, f"{base}/{int(concept_id)}/edit", body)
    if act == "delete_concept":
        return await _post_json(client, f"{base}/{int(concept_id)}/delete", {})
    if act == "add_concept_term":
        if not str(term or "").strip():
            raise ToolError("action=add_concept_term needs term.")
        body = {"term": str(term)}
        for k, v in (
            ("locale", locale),
            ("status", status),
            ("term_type", term_type),
            ("audience", audience),
        ):
            if v is not None:
                body[k] = str(v)
        if dnt is not None:
            body["dnt"] = bool(dnt)
        return await _post_json(client, f"{base}/{int(concept_id)}/terms", body)
    if act in ("edit_concept_term", "delete_concept_term"):
        if concept_id is None or term_id is None:
            raise ToolError(f"action={act} needs concept_id and term_id.")
        if act == "delete_concept_term":
            return await _post_json(
                client, f"{base}/{int(concept_id)}/terms/{int(term_id)}/delete", {}
            )
        body = {}
        for k, v in (
            ("term", term),
            ("status", status),
            ("term_type", term_type),
            ("audience", audience),
        ):
            if v is not None:
                body[k] = str(v)
        if dnt is not None:
            body["dnt"] = bool(dnt)
        if not body:
            raise ToolError("action=edit_concept_term with nothing to change.")
        return await _post_json(client, f"{base}/{int(concept_id)}/terms/{int(term_id)}/edit", body)
    if act == "set_audience":
        ids = [int(t) for t in (term_ids or [])]
        if not ids:
            raise ToolError("action=set_audience needs a non-empty term_ids list.")
        if not str(audience or "").strip():
            raise ToolError('action=set_audience needs an audience (e.g. "human,mt,llm").')
        return await _post_json(
            client, f"{base}/terms/audience", {"term_ids": ids, "audience": str(audience)}
        )
    raise ToolError(f"unhandled concept action {act!r}")


async def extract_terms(
    glossary_id: int,
    document_id: int,
    target: str | None = None,
    source: str | None = None,
    confirm: bool = False,
    idempotency_key: str | None = None,
) -> str:
    _require_confirmed("extract_terms", confirm)
    body: dict = {"document_id": int(document_id)}
    if str(target or "").strip():
        body["target"] = str(target).strip()
    if str(source or "").strip():
        body["source"] = str(source).strip()
    async with _client() as client:
        out = await _launch(client, "/api/glossaries/%d/extract" % int(glossary_id), body)  # noqa: UP031
    out.pop("ok", None)
    return json.dumps(out)


async def manage_style_guide(
    action: str,
    style_guide_id: int | None = None,
    name: str | None = None,
    body: str | None = None,
    customer_id: int | None = None,
    content_type_id: int | None = None,
    source_locale: str | None = None,
    target_locale: str | None = None,
    clear_content_type: bool = False,
    clear_pair: bool = False,
    status: str | None = None,
) -> str:
    act = str(action or "").strip()
    if act not in ("create", "update", "set_status"):
        raise ToolError(f"unknown action {act!r}; use create, update or set_status.")
    if act != "create" and style_guide_id is None:
        raise ToolError(f"action={act} needs style_guide_id.")
    async with _client() as client:
        if act == "create":
            if not str(name or "").strip():
                raise ToolError("action=create needs a name.")
            payload: dict = {"name": str(name).strip(), "body": str(body or "")}
            if customer_id is not None:
                payload["customer_id"] = int(customer_id)
            if content_type_id is not None:
                payload["content_type_id"] = int(content_type_id)
            if str(source_locale or "").strip():
                payload["source_locale"] = str(source_locale).strip()
            if str(target_locale or "").strip():
                payload["target_locale"] = str(target_locale).strip()
            out = await _post_json(client, "/api/style-guides", payload)
        elif act == "update":
            # Present-only semantics, same reason as edit_term: the API changes
            # only the keys in the body.
            payload = {}
            if name is not None:
                payload["name"] = str(name)
            if body is not None:
                payload["body"] = str(body)
            if content_type_id is not None:
                payload["content_type_id"] = int(content_type_id)
            if clear_content_type:
                payload["clear_content_type"] = True
            if source_locale is not None:
                payload["source_locale"] = str(source_locale)
            if target_locale is not None:
                payload["target_locale"] = str(target_locale)
            if clear_pair:
                payload["clear_pair"] = True
            if not payload:
                raise ToolError("action=update with nothing to change.")
            out = await _post_json(
                client,
                "/api/style-guides/%d/update" % int(style_guide_id),  # noqa: UP031
                payload,  # noqa: RUF100, UP031
            )
        else:
            if not str(status or "").strip():
                raise ToolError("action=set_status needs status.")
            out = await _post_json(
                client,
                "/api/style-guides/%d/set-status" % int(style_guide_id),  # noqa: UP031
                {"status": str(status).strip()},
            )
    out.pop("ok", None)
    return json.dumps({"action": act, **out})


async def manage_formality(
    action: str,
    scope: str | None = None,
    id: int | None = None,
    target_locale: str | None = None,
    register: str | None = None,
) -> str:
    act = str(action or "").strip()
    if act not in ("registers", "show", "set", "clear"):
        raise ToolError(f"unknown action {act!r}; use registers, show, set or clear.")
    async with _client() as client:
        if act == "registers":
            params = {"target_locale": str(target_locale).strip()} if target_locale else None
            out = await _get_json(client, "/api/formality/registers", params=params)
        else:
            sc = str(scope or "").strip()
            if sc not in ("customer", "project", "document") or id is None:
                raise ToolError(f"action={act} needs scope (customer, project or document) and id.")
            if act == "show":
                out = await _get_json(client, "/api/formality", params={"scope": sc, "id": int(id)})
            else:
                if not str(target_locale or "").strip():
                    raise ToolError(f"action={act} needs target_locale.")
                if act == "set" and not str(register or "").strip():
                    raise ToolError("action=set needs register (use action=clear to remove one).")
                out = await _post_json(
                    client,
                    "/api/formality",
                    {
                        "scope": sc,
                        "id": int(id),
                        "target_locale": str(target_locale).strip(),
                        "register": str(register).strip() if act == "set" else None,
                    },
                )
    out.pop("ok", None)
    return json.dumps({"action": act, **out})


async def project_notifications(
    action: str,
    project_id: int | None = None,
    channel_id: int | None = None,
    scope: str | None = None,
    ref: str | None = None,
    events: list[str] | None = None,
) -> str:
    act = str(action or "").strip()
    if act not in ("channels", "routes", "show", "attach", "mute", "unmute"):
        raise ToolError(
            f"unknown action {act!r}; use channels, routes, show, attach, mute or unmute."
        )
    async with _client() as client:
        if act == "channels":
            out = await _get_json(client, "/api/notify/channels")
        elif act == "routes":
            params = {"scope": str(scope).strip()} if scope else None
            out = await _get_json(client, "/api/notify/routes", params=params)
        elif act in ("show", "mute", "unmute"):
            if project_id is None:
                raise ToolError(f"action={act} needs project_id.")
            path = "/api/notify/projects/%d" % int(project_id)  # noqa: UP031
            if act == "show":
                out = await _get_json(client, path)
            else:
                out = await _post_json(client, path + "/mute", {"muted": act == "mute"})
        elif act == "attach":
            if channel_id is None or not str(scope or "").strip():
                raise ToolError("action=attach needs channel_id and scope.")
            out = await _post_json(
                client,
                "/api/notify/routes",
                {
                    "channel_id": int(channel_id),
                    "scope": str(scope).strip(),
                    "ref": str(ref or "").strip(),
                    "events": [str(e) for e in (events or [])],
                },
            )
    out.pop("ok", None)
    return json.dumps({"action": act, **out})


async def search_tm(
    view: str = "search",
    entry_id: int | None = None,
    customer_id: int | None = None,
    q: str | None = None,
    source_locale: str | None = None,
    target_locale: str | None = None,
    project_id: int | None = None,
    page: int = 1,
    per_page: int = 50,
) -> str:
    v = str(view or "search").strip() or "search"
    async with _client() as client:
        if v == "entry":
            if entry_id is None:
                raise ToolError("view=entry needs entry_id.")
            body = await _get_json(client, "/api/tm/%d" % int(entry_id))  # noqa: UP031
            body.pop("ok", None)
            return json.dumps({"entry": body})
        params: dict = {}
        if customer_id is not None:
            params["customer"] = int(customer_id)
        if v == "stats":
            body = await _get_json(client, "/api/tm/stats", params=params or None)
            body.pop("ok", None)
            return json.dumps(body)
        if v != "search":
            raise ToolError(f"unknown view {v!r}; use search, stats or entry.")
        params.update({"page": int(page or 1), "per_page": int(per_page or 50)})
        for pname, value in (
            ("q", q),
            ("source_locale", source_locale),
            ("target_locale", target_locale),
        ):
            if str(value or "").strip():
                params[pname] = str(value).strip()
        if project_id is not None:
            params["project_id"] = int(project_id)
        body = await _get_json(client, "/api/tm", params=params)
    body.pop("ok", None)
    return json.dumps(body)


async def search_workspace(
    q: str,
    groups: list[str] | None = None,
    limit: int = 5,
    offset: int = 0,
) -> str:
    params: dict = {
        "q": str(q or ""),
        "limit": 5 if limit is None else int(limit),
        "offset": 0 if offset is None else int(offset),
    }
    wanted = [str(g).strip() for g in (groups or []) if str(g).strip()]
    if wanted:
        params["group"] = ",".join(wanted)
    async with _client() as client:
        body = await _get_json(client, "/api/search", params=params)
    body.pop("ok", None)
    return json.dumps(body)


async def manage_tm(
    action: str,
    entry_id: int | None = None,
    target_text: str | None = None,
    project_id: int | None = None,
    file_path: str | None = None,
    data: str | None = None,
    source_locale: str | None = None,
    target_locale: str | None = None,
    acknowledge_placeholder_loss: bool = False,
) -> str:
    act = str(action or "").strip()
    # Issue #567: forwarded only for a real boolean true (fail closed, like approve_segment).
    ack = {"acknowledge_placeholder_loss": True} if acknowledge_placeholder_loss is True else {}
    if act == "edit":
        if entry_id is None or target_text is None:
            raise ToolError("action=edit needs entry_id and target_text.")
        async with _client() as client:
            out = await _post_json(
                client,
                "/api/tm/%d/edit" % int(entry_id),  # noqa: UP031
                {"target": str(target_text), **ack},  # noqa: RUF100, UP031
            )
        out.pop("ok", None)
        return json.dumps({"action": act, **out})
    if act not in ("import", "export"):
        raise ToolError(f"unknown action {act!r}; use edit, import or export.")
    if (
        project_id is None
        or not str(source_locale or "").strip()
        or not str(target_locale or "").strip()
    ):
        raise ToolError(f"action={act} needs project_id, source_locale and target_locale.")
    src_loc, tgt_loc = str(source_locale).strip(), str(target_locale).strip()
    async with _client() as client:
        if act == "import":
            if str(file_path or "").strip():
                raw = _read_local_file(file_path, "TMX or SDLTM file")
            elif data is not None:
                raw = str(data).encode("utf-8")
            else:
                raise ToolError("action=import needs file_path or data.")
            out = await _post_json(
                client,
                "/api/projects/%d/tm/import" % int(project_id),  # noqa: UP031
                {
                    "tmx_b64": base64.b64encode(raw).decode("ascii"),
                    "source": src_loc,
                    "target": tgt_loc,
                    **ack,
                },
            )
            out.pop("ok", None)
            return json.dumps({"action": act, **out})
        body = await _get_json(
            client,
            "/api/projects/%d/tm/export" % int(project_id),  # noqa: UP031
            params={"source": src_loc, "target": tgt_loc},
        )
        raw = base64.b64decode(body.get("tmx_b64") or "")
    written = _write_output("project%d.%s-%s.tmx" % (int(project_id), src_loc, tgt_loc), raw)  # noqa: UP031
    return json.dumps(
        {
            "action": act,
            **written,
            "project_id": int(project_id),
            "source": src_loc,
            "target": tgt_loc,
        }
    )


async def attach_tm_source(
    project_id: int, action: str, source_project_id: int | None = None
) -> str:
    """list / add / remove another project's TM as a read-only source. Nothing is
    copied; the server enforces the same-customer-or-house rule and refuses a
    foreign source (400) rather than attach it."""
    act = str(action or "").strip()
    if act not in ("list", "add", "remove"):
        raise ToolError(f"unknown action {act!r}; use list, add or remove.")
    async with _client() as client:
        if act == "list":
            body = await _get_json(client, "/api/projects/%d/tm-sources" % int(project_id))  # noqa: UP031
            return json.dumps(
                {"project_id": int(project_id), "tm_sources": body.get("tm_sources", [])}
            )
        if source_project_id is None:
            raise ToolError(f"action={act} needs source_project_id.")
        path = (
            "/api/projects/%d/tm-sources" % int(project_id)  # noqa: UP031
            if act == "add"
            else "/api/projects/%d/tm-sources/remove" % int(project_id)  # noqa: UP031
        )
        out = await _post_json(client, path, {"source_project_id": int(source_project_id)})
    out.pop("ok", None)
    return json.dumps({"action": act, **out})


async def pin_glossary(project_id: int, action: str, glossary_id: int | None = None) -> str:
    """list / add / remove a module-glossary pin on a project. A draft glossary may be
    pinned but contributes nothing until approved; the server refuses a glossary the
    project's customer may not use."""
    act = str(action or "").strip()
    if act not in ("list", "add", "remove"):
        raise ToolError(f"unknown action {act!r}; use list, add or remove.")
    async with _client() as client:
        if act == "list":
            body = await _get_json(client, "/api/projects/%d/glossaries" % int(project_id))  # noqa: UP031
            return json.dumps(
                {"project_id": int(project_id), "glossaries": body.get("glossaries", [])}
            )
        if glossary_id is None:
            raise ToolError(f"action={act} needs glossary_id.")
        path = (
            "/api/projects/%d/glossaries" % int(project_id)  # noqa: UP031
            if act == "add"
            else "/api/projects/%d/glossaries/remove" % int(project_id)  # noqa: UP031
        )
        out = await _post_json(client, path, {"glossary_id": int(glossary_id)})
    out.pop("ok", None)
    return json.dumps({"action": act, **out})


async def translate_keys(
    project_id: int,
    workflow_id: int,
    locale: str,
    confirm: bool = False,
    idempotency_key: str | None = None,
) -> str:
    """Launch translation over a KEY project (confirmed half; the plan lives in the
    planner). Refuses the offline mock engine (409) before spending, and a workflow
    this token cannot see is a 404 — never a launch.

    READ `queued` AS WELL AS `launched`. A project-wide translate is (locales x documents)
    runs and the install admits only so many at once, so the pairs it had no free slot for
    come back under `queued` — each already a real run row, waiting, with its id — and start
    on their own as earlier runs finish. `launched` alone is NOT "the project is translated";
    poll the queued run ids too (`work_status`). `queue_incomplete`, when present, names
    pairs whose row could not be written at all: those need a retry."""
    _require_confirmed("translate_keys", confirm)
    async with _client() as client:
        out = await _post_json(
            client,
            "/api/projects/%d/keys/translate" % int(project_id),  # noqa: UP031
            {"workflow_id": int(workflow_id), "locale": str(locale or "").strip()},
        )
    out.pop("ok", None)
    return json.dumps(out)


# --- family I: operational reads -----------------------------------------------
async def run_report(
    kind: str = "cost",
    group_by: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    customer_id: int | None = None,
    project_id: int | None = None,
    provider: str | None = None,
) -> str:
    params: dict = {"kind": str(kind or "cost").strip() or "cost"}
    for pname, value in (
        ("group_by", group_by),
        ("from", date_from),
        ("to", date_to),
        ("provider", provider),
    ):
        if str(value or "").strip():
            params[pname] = str(value).strip()
    if customer_id is not None:
        params["customer_id"] = int(customer_id)
    if project_id is not None:
        params["project_id"] = int(project_id)
    async with _client() as client:
        body = await _get_json(client, "/api/reports/run", params=params)
    body.pop("ok", None)
    return json.dumps(body)


# --- Queries (SPEC-wave3 §4.4) — thin relays of /api/queries*. NOTHING decides here;
# the server enforces the tenant ring + §4.5 RBAC. `qid` goes through int() (no free-text
# path segment). A disabled queries_enabled 404s the route, and _explain_status appends
# the removed-feature note (queries_enabled is in GATE_404_STYLE).
async def list_queries(
    status: str | None = None,
    project_id: int | None = None,
    document_id: int | None = None,
    segment_id: int | None = None,
    key_id: int | None = None,
    assignee: str | None = None,
    mine: bool = False,
    limit: int = 50,
    offset: int = 0,
) -> str:
    params: dict = {}
    if str(status or "").strip():
        params["status"] = str(status).strip()
    for pname, value in (
        ("project", project_id),
        ("document", document_id),
        ("segment", segment_id),
        ("key", key_id),
    ):
        if value is not None:
            params[pname] = int(value)
    if str(assignee or "").strip():
        params["assignee"] = str(assignee).strip()
    if mine:
        params["mine"] = "1"
    params["limit"] = int(limit)
    params["offset"] = int(offset)
    async with _client() as client:
        body = await _get_json(client, "/api/queries", params=params)
    body.pop("ok", None)
    return json.dumps(body)


async def get_query(query_id: int) -> str:
    async with _client() as client:
        body = await _get_json(client, "/api/queries/%d" % int(query_id))  # noqa: UP031
    body.pop("ok", None)
    return json.dumps(body)


async def open_query(
    body: str,
    subject: str | None = None,
    segment_id: int | None = None,
    document_id: int | None = None,
    key_id: int | None = None,
    project_id: int | None = None,
    assignee_email: str | None = None,
) -> str:
    payload: dict = {"body": str(body or ""), "subject": str(subject or "")}
    for pname, value in (
        ("segment_id", segment_id),
        ("document_id", document_id),
        ("key_id", key_id),
        ("project_id", project_id),
    ):
        if value is not None:
            payload[pname] = int(value)
    if str(assignee_email or "").strip():
        payload["assignee_email"] = str(assignee_email).strip()
    async with _client() as client:
        out = await _post_json(client, "/api/queries", payload)
    out.pop("ok", None)
    return json.dumps(out)


async def reply_to_query(query_id: int, body: str, answer: bool = False) -> str:
    payload = {"body": str(body or ""), "answer": bool(answer)}
    async with _client() as client:
        out = await _post_json(client, "/api/queries/%d/posts" % int(query_id), payload)  # noqa: UP031
    out.pop("ok", None)
    return json.dumps(out)


async def set_query_status(query_id: int, status: str) -> str:
    payload = {"status": str(status or "").strip()}
    async with _client() as client:
        out = await _post_json(client, "/api/queries/%d/status" % int(query_id), payload)  # noqa: UP031
    out.pop("ok", None)
    return json.dumps(out)


async def assign_query(query_id: int, assignee_email: str | None = None) -> str:
    payload = {"assignee_email": str(assignee_email or "")}
    async with _client() as client:
        out = await _post_json(client, "/api/queries/%d/assign" % int(query_id), payload)  # noqa: UP031
    out.pop("ok", None)
    return json.dumps(out)


async def my_work(
    days: int = 90, quality: str | None = None, limit: int = 50, offset: int = 0
) -> str:
    """The key owner's reviewer worklist — a thin relay of GET /api/worklist. NOTHING
    decides here; the server resolves identity from the key's user (a shared connector
    token is refused 400) and owns the scoping ring. A disabled worklist_enabled 404s the
    route."""
    params: dict = {"days": int(days), "limit": int(limit), "offset": int(offset)}
    if str(quality or "").strip():
        params["quality"] = str(quality).strip()
    async with _client() as client:
        body = await _get_json(client, "/api/worklist", params=params)
    body.pop("ok", None)
    return json.dumps(body)


# --- reviewer LQA findings (SPEC-wave3 §6.5) — path ids are ints (%d), no _seg needed ---
async def list_lqa_findings(document_id: int, locale: str | None = None) -> str:
    params: dict = {}
    if str(locale or "").strip():
        params["locale"] = str(locale).strip()
    async with _client() as client:
        body = await _get_json(
            client,
            "/api/documents/%d/lqa/findings" % int(document_id),  # noqa: UP031
            params=params,  # noqa: RUF100, UP031
        )
    body.pop("ok", None)
    return json.dumps(body)


async def log_lqa_finding(
    document_id: int, segment_id: int, dimension: str, severity: str, note: str
) -> str:
    payload = {
        "segment_id": int(segment_id),
        "dimension": str(dimension or ""),
        "severity": str(severity or ""),
        "note": str(note or ""),
    }
    async with _client() as client:
        out = await _post_json(client, "/api/documents/%d/lqa/findings" % int(document_id), payload)  # noqa: UP031
    out.pop("ok", None)
    return json.dumps(out)


async def edit_lqa_finding(finding_id: int, dimension: str, severity: str, note: str) -> str:
    payload = {
        "dimension": str(dimension or ""),
        "severity": str(severity or ""),
        "note": str(note or ""),
    }
    async with _client() as client:
        out = await _post_json(client, "/api/lqa/findings/%d" % int(finding_id), payload)  # noqa: UP031
    out.pop("ok", None)
    return json.dumps(out)


async def withdraw_lqa_finding(finding_id: int) -> str:
    async with _client() as client:
        out = await _post_json(client, "/api/lqa/findings/%d/withdraw" % int(finding_id), {})  # noqa: UP031
    out.pop("ok", None)
    return json.dumps(out)


async def list_connectors() -> str:
    async with _client() as client:
        body = await _get_json(client, "/api/connectors")
    return json.dumps({"connections": body.get("connections", [])})


async def list_images(
    document_id: int | None = None, locale: str | None = None, download: bool = False
) -> str:
    loc = str(locale or "").strip()
    async with _client() as client:
        if document_id is None:
            if download:
                raise ToolError("download=true needs document_id and locale.")
            body = await _get_json(client, "/api/images")
            return json.dumps({"images": body.get("images", [])})
        if download:
            if not loc:
                raise ToolError("download=true needs locale.")
            resp = await _call(
                client,
                "GET",
                "/api/images/%d/export" % int(document_id),  # noqa: UP031
                params={"locale": loc},  # noqa: RUF100, UP031
            )
            content = resp.content
            fname = _filename_from_disposition(resp.headers.get("content-disposition", ""))
            if not fname:
                fname = "image%d.%s.bin" % (int(document_id), loc)  # noqa: UP031
            written = _write_output(fname, content)
            return json.dumps({**written, "document_id": int(document_id), "locale": loc})
        params = {"locale": loc} if loc else None
        body = await _get_json(client, "/api/images/%d" % int(document_id), params=params)  # noqa: UP031
    body.pop("ok", None)
    return json.dumps(body)


async def manage_workflow_group(
    action: str,
    group_id: int | None = None,
    workflow_id: int | None = None,
    name: str | None = None,
    customer_id: int | None = None,
    description: str | None = None,
    pair: str | None = None,
    make_default: bool = False,
) -> str:
    """Multilingual workflow groups (create/list/show/add/set-default). Bills nothing.
    A foreign-org/customer group or workflow is a 404, never a launch or a leak. Needs
    can_tune_ai; set-default also a manager, and a default change that moves an inherited
    system prompt needs an admin or a manager."""
    act = str(action or "").strip()
    async with _client() as client:
        if act == "list":
            body = await _get_json(client, "/api/workflow-groups")
            return json.dumps({"groups": body.get("groups", [])})
        if act == "create":
            if not str(name or "").strip():
                raise ToolError("action=create needs name.")
            b: dict = {"name": str(name)}
            if customer_id is not None:
                b["customer_id"] = int(customer_id)
            if description is not None:
                b["description"] = str(description)
            out = await _post_json(client, "/api/workflow-groups", b)
            out.pop("ok", None)
            return json.dumps(out)
        if group_id is None:
            raise ToolError(f"action={act} needs group_id.")
        if act == "show":
            out = await _get_json(client, "/api/workflow-groups/%d" % int(group_id))  # noqa: UP031
            out.pop("ok", None)
            return json.dumps(out)
        if act == "add":
            if workflow_id is None:
                raise ToolError("action=add needs workflow_id.")
            b = {"workflow_id": int(workflow_id)}
            if pair is not None and str(pair).strip():
                b["pair"] = str(pair).strip()
            if make_default:
                b["make_default"] = True
            out = await _post_json(client, "/api/workflow-groups/%d/add" % int(group_id), b)  # noqa: UP031
            out.pop("ok", None)
            return json.dumps(out)
        if act == "set-default":
            if workflow_id is None:
                raise ToolError("action=set-default needs workflow_id.")
            out = await _post_json(
                client,
                "/api/workflow-groups/%d/default" % int(group_id),  # noqa: UP031
                {"workflow_id": int(workflow_id)},
            )
            out.pop("ok", None)
            return json.dumps(out)
        raise ToolError(f"unknown action {act!r}; use create/list/show/add/set-default.")


async def clone_workflow(workflow_id: int) -> str:
    """Deep-copy a workflow and its steps into a new \"(copy)\" workflow. Bills nothing.
    A workflow this token cannot see (another org or customer) is a 404."""
    async with _client() as client:
        out = await _post_json(client, "/api/workflows/%d/clone" % int(workflow_id), {})  # noqa: UP031
    out.pop("ok", None)
    return json.dumps(out)


async def translate_image(
    document_id: int, locale: str, confirm: bool = False, idempotency_key: str | None = None
) -> str:
    """Translate an image's regions into one locale (confirmed half; the plan lives in
    the planner). Refuses the offline mock engine (409) before spending, and an image
    this token cannot see is a 404 -- never a translate."""
    _require_confirmed("translate_image", confirm)
    async with _client() as client:
        out = await _launch(
            client,
            "/api/images/%d/translate" % int(document_id),  # noqa: UP031
            {"locale": str(locale or "").strip()},
        )
    out.pop("ok", None)
    return json.dumps(out)


# --- family I: connector tools (Wave R4) --------------------------------------
#: kind -> (path suffix under /api/connectors/{cid}/, the id field its body wants).
#: connector_translate dispatches on the connection's OWN kind (read from GET
#: /api/connectors), so the model never has to know which platform verb a connection
#: is - it names a connection id and the items. Jira/Confluence take issue_keys/
#: page_ids; the article-style connectors all take article_ids (the /api/_article_
#: translate shared body). A kind with no per-item translate route (airtable via the
#: webhook/records family) is refused with a pointer to the right
#: tool, never silently sent to a route that does not exist.
_CONNECTOR_TRANSLATE_ROUTES: dict = {
    "jira": ("jira/translate", "issue_keys"),
    "confluence": ("confluence/translate", "page_ids"),
    "document360": ("document360/translate", "article_ids"),
    "freshdesk": ("freshdesk/translate", "article_ids"),
    "zoho": ("zoho/translate", "article_ids"),
    "strapi": ("strapi/translate", "article_ids"),
    "contentful": ("contentful/translate", "article_ids"),
    "shopify": ("shopify/translate", "article_ids"),
    "hubspot": ("hubspot/translate", "article_ids"),
    "intercom": ("intercom/translate", "article_ids"),
    "salesforce": ("salesforce/translate", "article_ids"),
    "notion": ("notion/translate", "article_ids"),
    "helpscout": ("helpscout/translate", "article_ids"),
    "wordpress": ("wordpress/translate", "article_ids"),
    "akeneo": ("akeneo/translate", "article_ids"),
    "drupal": ("drupal/translate", "article_ids"),
    "mcp_cms": ("mcp_cms/translate", "article_ids"),
    "gdocs": ("gdocs/translate", "article_ids"),
    "servicenow": ("servicenow/translate", "article_ids"),
    "msgraph": ("msgraph/translate", "article_ids"),
    "github": ("github/translate", "article_ids"),
    "bitbucket": ("bitbucket/translate", "article_ids"),
    "gitlab": ("gitlab/translate", "article_ids"),
    "sanity": ("sanity/translate", "article_ids"),
    "sitecore": ("sitecore/translate", "article_ids"),
    "zendesk": ("zendesk/translate", "article_ids"),
    "plytix": ("plytix/translate", "article_ids"),
}

#: The one sentence every connector spend plan states about the egress guard. The
#: MCP server is a thin /api client and NO /api route exposes the allow_mock_egress
#: Setting or a connection's resolved engine liveness, so the plan cannot READ the
#: flag (exactly as it cannot read spend caps -- budget.readable is False too). What
#: it CAN do, and must, is surface the guard so the user knows the confirmed call
#: fails LOUD (MockEgressBlocked, nothing written) rather than pushing mock
#: placeholder text into a real tenant. The /api route enforces it; the tool neither
#: reimplements nor bypasses it.
_MOCK_EGRESS_NOTE = (
    "A connector write REFUSES non-live (offline MockProvider) MT output into a real "
    "external tenant unless the allow_mock_egress Setting is ON. That flag and the "
    "connection's resolved engine liveness are enforced SERVER-SIDE and are not "
    "readable over /api, so this plan cannot show their values - but the confirmed "
    "call fails loud (nothing is written) if a mock engine would egress to a real "
    "tenant."
)


async def _connection_by_id(client: httpx.AsyncClient, cid: int) -> dict | None:
    """The one connection with id==cid from GET /api/connectors, or None if the
    caller cannot see it (another org/customer, or it does not exist).

    The list route is already org- AND customer-scoped server-side, so this read can
    only ever return a connection the token is allowed to see - a foreign cid is
    simply absent here, exactly as it 404s on the action route. No cross-tenant
    enrichment leaks through the plan."""
    body = await _get_json(client, "/api/connectors")
    for conn in body.get("connections", []):
        if conn.get("id") == int(cid):
            return conn
    return None


async def connector_status() -> str:
    """The connector subsystem status: the health probe (which kinds this build ships)
    plus the caller's own org/customer-scoped connection roster."""
    async with _client() as client:
        health = await _get_json(client, "/api/connectors/health")
        body = await _get_json(client, "/api/connectors")
    return json.dumps(
        {
            "healthy": bool(health.get("ok")),
            "connector_kinds": health.get("connectors", []),
            "connections": body.get("connections", []),
        }
    )


async def connector_pull(connection_id: int) -> str:
    """Pull external content INTO Localization OS (write, not spend -- no MT, and nothing
    written in the external tenant). A foreign connection is a 404 from the route."""
    async with _client() as client:
        out = await _launch(client, "/api/connectors/%d/pull" % int(connection_id), {})  # noqa: UP031
    out.pop("ok", None)
    return json.dumps({"connection_id": int(connection_id), **out})


_CONNECTOR_TRANSLATE_NO_IDS = (
    "connector_translate needs item_ids: with none, the server would pull, translate and "
    "write back the connection's WHOLE scope (up to its run cap). Name the items to "
    "translate, or call connector_sync to run the connection's full flow on purpose."
)


async def connector_translate(
    connection_id: int,
    item_ids: list[str] | None = None,
    target_locales: list[str] | None = None,
    confirm: bool = False,
    idempotency_key: str | None = None,
) -> str:
    """Translate specific item(s) of one connection back into the live external tenant
    (confirmed half; the plan lives in the planner). Dispatches on the connection's own
    kind; refuses a kind with no per-item translate route rather than guess."""
    _require_confirmed("connector_translate", confirm)
    cid = int(connection_id)
    async with _client() as client:
        conn = await _connection_by_id(client, cid)
        if conn is None:
            # Same verdict the action route would give: a connection the token cannot
            # see is not found. Do not probe the platform route for a 404.
            raise ToolError(
                "Connector connection %d not found (it may not exist, or is not yours)." % cid  # noqa: UP031
            )
        kind = str(conn.get("kind") or "").strip().lower()
        route = _CONNECTOR_TRANSLATE_ROUTES.get(kind)
        if route is None:
            raise ToolError(
                f"The {kind!r} connector has no per-item translate route over MCP. Use "
                "connector_sync to run its full translate flow."
            )
        suffix, id_field = route
        body: dict = {}
        ids = [str(i).strip() for i in (item_ids or []) if str(i).strip()]
        if not ids:
            raise ToolError(_CONNECTOR_TRANSLATE_NO_IDS)
        body[id_field] = ids
        locs = _clean_locales(target_locales)
        if locs:
            body["target_locales"] = locs
        out = await _launch(client, f"/api/connectors/{int(cid)}/{suffix}", body)
    out.pop("ok", None)
    return json.dumps({"connection_id": cid, "kind": kind, **out})


async def connector_push(
    connection_id: int, confirm: bool = False, idempotency_key: str | None = None
) -> str:
    """Push one connection's finished translations back into the live external tenant
    (confirmed half; the plan lives in the planner)."""
    _require_confirmed("connector_push", confirm)
    async with _client() as client:
        out = await _launch(client, "/api/connectors/%d/push" % int(connection_id), {})  # noqa: UP031
    out.pop("ok", None)
    return json.dumps({"connection_id": int(connection_id), **out})


async def vendor_approve_quote(
    did: int,
    quoted_cost_usd: float | None = None,
    confirm: bool = False,
    idempotency_key: str | None = None,
) -> str:
    """Approve a quote-gated vendor leg's quote so work proceeds (confirmed half; the plan
    lives in the planner). A SPEND decision -- it authorizes the vendor's quoted human cost."""
    _require_confirmed("vendor_approve_quote", confirm)
    body: dict = {}
    if quoted_cost_usd is not None:
        body["quoted_cost_usd"] = quoted_cost_usd
    async with _client() as client:
        out = await _post_json(client, "/api/vendors/dispatches/%d/approve-quote" % int(did), body)  # noqa: UP031
    out.pop("ok", None)
    return json.dumps({"dispatch_id": int(did), **out})


async def vendor_reject_rework(
    did: int, note: str | None = None, confirm: bool = False, idempotency_key: str | None = None
) -> str:
    """Reject a delivered vendor leg and re-dispatch a rework to the same vendor (confirmed
    half; the plan lives in the planner). A SPEND decision -- billable human rework. Re-sends
    a JOB; it never injects content (the rework return funnels through the guarded seam)."""
    _require_confirmed("vendor_reject_rework", confirm)
    body: dict = {}
    if note:
        body["note"] = str(note)
    async with _client() as client:
        out = await _post_json(client, "/api/vendors/dispatches/%d/reject-rework" % int(did), body)  # noqa: UP031
    out.pop("ok", None)
    return json.dumps({"dispatch_id": int(did), **out})


async def connector_sync(
    connection_id: int, confirm: bool = False, idempotency_key: str | None = None
) -> str:
    """Sync one connection (pull, translate, push) and write back into the live external
    tenant (confirmed half; the plan lives in the planner).

    #728: a real sync takes minutes (HubSpot, 3 posts x 3 languages: about 5), so the old
    synchronous POST timed out while the server finished the sync, and the agent saw a
    failure. Now it uses the route's own deduplicated async mode (the remote CLI's #530
    path): 202 + job id at once, a retry while it runs gets the SAME job, and the tool
    waits up to 45 s before handing back the job id to poll with job_status, whose result
    carries the counts, held-back pairs and failures (#743)."""
    _require_confirmed("connector_sync", confirm)
    async with _client() as client:
        out = await _launch(
            client,
            "/api/connectors/%d/sync" % int(connection_id),  # noqa: UP031
            {},
            native_async=True,
            slow=True,
        )
    out.pop("ok", None)
    return json.dumps({"connection_id": int(connection_id), **out})


async def connector_retry(
    connection_id: int, job_id: int, confirm: bool = False, idempotency_key: str | None = None
) -> str:
    """Re-run only the failed items of a prior connector run (confirmed half; the plan
    lives in the planner). Enqueues a retry job and returns its id; the route refuses a
    job that does not belong to this connection."""
    _require_confirmed("connector_retry", confirm)
    async with _client() as client:
        out = await _post_json(
            client,
            "/api/connectors/%d/retry" % int(connection_id),  # noqa: UP031
            {"job_id": int(job_id)},  # noqa: RUF100, UP031
        )
    out.pop("ok", None)
    return json.dumps({"connection_id": int(connection_id), **out})


async def manage_users(
    action: str,
    user_id: int | None = None,
    email: str | None = None,
    role: str | None = None,
    name: str | None = None,
    department: str | None = None,
) -> str:
    """Create a user, set a role, enable or disable one (SPEC-full-remote-parity R2).
    Bills nothing. The sharpest verbs (erase, reset-password, api-key minting) are
    deliberately not here. A uid in another org is a 404, never a leak."""
    act = str(action or "").strip()
    async with _client() as client:
        if act == "create":
            if not str(email or "").strip():
                raise ToolError("action=create needs email.")
            if not str(role or "").strip():
                raise ToolError("action=create needs role.")
            b: dict = {"email": str(email), "role": str(role)}
            if name is not None:
                b["name"] = str(name)
            if department is not None:
                b["department"] = str(department)
            out = await _post_json(client, "/api/users", b)
            out.pop("ok", None)
            return json.dumps(out)
        if user_id is None:
            raise ToolError(f"action={act} needs user_id.")
        if act == "role":
            if not str(role or "").strip():
                raise ToolError("action=role needs role.")
            out = await _post_json(client, "/api/users/%d/role" % int(user_id), {"role": str(role)})  # noqa: UP031
            out.pop("ok", None)
            return json.dumps(out)
        if act in ("enable", "disable", "unlock"):
            out = await _post_json(client, f"/api/users/{int(user_id)}/{act}", {})
            out.pop("ok", None)
            return json.dumps(out)
        raise ToolError(f"unknown action {act!r}; use create/role/enable/disable/unlock.")


async def manage_admin_ops(
    action: str,
    rule_id: int | None = None,
    ext: str | None = None,
    department: str | None = None,
    priority: int | None = None,
    project_id: int | None = None,
    provider: str | None = None,
    monthly_limit: int | None = None,
    source: str | None = None,
    target: str | None = None,
    rulebook_text: str | None = None,
) -> str:
    """Operator admin an agent may drive: add/toggle a routing rule, set a department
    budget, install an LQA rulebook (SPEC-full-remote-parity R2). Bills nothing.
    Deletes, GDPR, credentials and config switches are host-only. Rows are org-scoped,
    so a foreign id is a 404.

    A routing rule matches on ext + department and targets a project and/or provider.
    Subject matching and workflow / language-set targeting are RESERVED FOR V2 and are
    not consulted at run time yet, so this tool exposes no such knob (the server 400s
    a rule that carries one)."""
    act = str(action or "").strip()
    async with _client() as client:
        if act == "add_rule":
            b: dict = {}
            if ext is not None:
                b["ext"] = str(ext)
            if department is not None:
                b["department"] = str(department)
            if priority is not None:
                b["priority"] = int(priority)
            if project_id is not None:
                b["project"] = int(project_id)
            if provider is not None:
                b["provider"] = str(provider)
            out = await _post_json(client, "/api/routing/rules", b)
            out.pop("ok", None)
            return json.dumps(out)
        if act == "toggle_rule":
            if rule_id is None:
                raise ToolError("action=toggle_rule needs rule_id.")
            out = await _post_json(client, "/api/routing/rules/%d/toggle" % int(rule_id), {})  # noqa: UP031
            out.pop("ok", None)
            return json.dumps(out)
        if act == "set_budget":
            if not str(department or "").strip():
                raise ToolError("action=set_budget needs department.")
            if monthly_limit is None:
                raise ToolError("action=set_budget needs monthly_limit.")
            out = await _post_json(
                client, "/api/budgets", {"department": str(department), "limit": int(monthly_limit)}
            )
            out.pop("ok", None)
            return json.dumps(out)
        if act == "set_rulebook":
            if not (str(source or "").strip() and str(target or "").strip()):
                raise ToolError("action=set_rulebook needs source and target.")
            out = await _post_json(
                client,
                "/api/lqa/rulebook",
                {"source": str(source), "target": str(target), "text": str(rulebook_text or "")},
            )
            out.pop("ok", None)
            return json.dumps(out)
        raise ToolError(
            f"unknown action {act!r}; use add_rule/toggle_rule/set_budget/set_rulebook."
        )


# --- manage_install (Wave R3) ------------------------------------------------
# The NON-SECRET config/credential surface. THE SECURITY REFINEMENT (a deviation
# from the spec's literal "manage_install covers provider keys"): a SECRET VALUE
# must NEVER transit an MCP tool call - the model would author it, violating the
# "never write a secret into a prompt/tool-call" guardrail. So this tool REFUSES
# secret-marked and install-locked keys CLIENT-SIDE (before any HTTP call), covers
# only non-secret config, engine create/update/test, provider key CLEAR + masked
# SHOW; every secret-SET path is /api-only. The keys below mirror
# app.services.settings (SECRET display markers + REMOTE_IMMUTABLE_KEYS) - kept as
# local constants so the MCP server stays a pure HTTP client, and the /api route
# enforces the same refusal server-side (defense in depth).
#
# _INSTALL_LOCKED_KEYS MUST equal app.services.settings.REMOTE_IMMUTABLE_KEYS
# EXACTLY. It used to be a hand-curated 6-key subset that silently rotted as R2/R3
# and later sprints (Q1 blobstore, Q2 metrics/otel, P pii_ner) grew the server-side
# set to 25 -- a defense-in-depth GAP (not a live hole; the /api route is the real
# enforcer). Rather than the server importing `app` (it is a pure HTTP client by
# design), tests/verify_mcp_parity.py AST-reads this frozenset and fails RED the
# moment it drifts from REMOTE_IMMUTABLE_KEYS in either direction -- so a new
# host-only key added over there forces this copy to be updated in the same diff.
_SECRET_MARKERS = ("password", "secret", "client_secret", "api_key", "_enc", "token")
_INSTALL_LOCKED_KEYS = frozenset(
    {
        # #753: the MCP OAuth authorization server switches (host-only over /api).
        "mcp_oauth_enabled",
        "mcp_oauth_resource",
        "mcp_oauth_dcr_enabled",
        "mcp_oauth_cimd_hosts",
        "mcp_oauth_rs_secret_hash",
        "egress_private_allowlist",
        "security_allowed_hosts",
        "trusted_proxy_ips",
        "audit_enabled",
        "audit_fail_open",
        "blobstore_s3_bucket",
        "blobstore_s3_credentials",
        "blobstore_s3_enabled",
        "blobstore_s3_endpoint_url",
        "blobstore_s3_prefix",
        "data_export_allow_plaintext_staging",
        "data_export_enabled",
        "databricks_export_enabled",
        "embedded_trust_host_auth",
        "encrypt_db",
        "encrypt_uploads",
        "job_queue_backend",
        "ledger_purge_reconcile_grace_seconds",
        "metrics_enabled",
        "multi_org_enabled",
        "operator_key_hash",
        "otel_enabled",
        "otel_endpoint",
        "pii_ner_enabled",
        "pii_ner_entities",
        "pii_ner_spacy_model",
        "pii_redaction",
        "remote_admin_enabled",
        "saml_enabled",
        "saml_idp_metadata_xml",
        "saml_sp_private_key_enc",
        "saml_sp_x509_cert",
        "scim_enabled",
        "security_auth_required",
        "vendor_studio_sso_secret",
        "vendor_studio_url",
    }
)


# Password-policy keys (HECVAT AAAI-03): "password" in the NAME, a plain policy number in
# the VALUE, so set_config may carry them. Mirrors app.services.settings.NOT_SECRET_KEYS
# exactly; tests/verify_password_policy.py AST-reads this frozenset and pins the two.
_NOT_SECRET_KEYS = frozenset(
    {
        "password_min_length",
        "password_require_classes",
        "password_block_common",
        "password_history",
        "password_max_age_days",
    }
)


def _looks_secret(key: str) -> bool:
    if key in _NOT_SECRET_KEYS:
        return False
    k = (key or "").lower()
    return any(m in k for m in _SECRET_MARKERS)


def _reject_secret_config(config: dict | None) -> None:
    """An engine `config` object is free-form (formality/region/model_type/...), and
    one KIND of config key IS a secret: the Lara/Amazon `access_key_secret` pair, which
    the engine service Fernet-encrypts. A model must never author it, so manage_install
    refuses any secret-LOOKING config key client-side - the smuggle a free-form object
    would otherwise carry past every name-based check."""
    if not isinstance(config, dict):
        return
    bad = sorted(k for k in config if _looks_secret(k))
    if bad:
        raise ToolError(
            "manage_install refuses secret-looking engine config key(s) {}: a secret "
            "value must never pass through a tool call. Set it over /api.".format(", ".join(bad))
        )


async def manage_install(
    action: str,
    settings: dict | None = None,
    confirm: list | None = None,
    engine_id: int | None = None,
    name: str | None = None,
    kind: str | None = None,
    base_url: str | None = None,
    model: str | None = None,
    config: dict | None = None,
    enabled: bool | None = None,
    provider: str | None = None,
    source: str | None = None,
    target: str | None = None,
    source_locale: str | None = None,
    target_locale: str | None = None,
    threshold: int | None = None,
) -> str:
    """Install administration (SPEC-full-remote-parity R3), gated on
    remote_admin_enabled. Reads the masked config; sets NON-SECRET settings; creates/
    updates/tests an engine's non-secret fields; clears or masked-shows a provider key.
    For a quality-judge engine (jev / jev_local): inspect_engine returns its criteria,
    request template and decision rule; jev_try scores one segment through it (hosted
    Jev bills a fraction of a cent per try and honours the spend cap) and returns the
    verdict, the request, the raw answer and the auto-fix post-editor prompt. No key.
    LDAP: set_config accepts ldap_ca_cert (PEM CA bundle path; blank uses system roots)
    and ldap_host (bare host uses LDAPS; ldap:// requires StartTLS). Certificates and
    hostnames are always validated. get_config returns these non-secret settings.
    Security-sensitive keys (sign-in, LDAP/OIDC/SAML trust, default roles, password and
    session policy, retention, audit-check cadence) are set only when named in `confirm`.
    It NEVER carries a secret value - a secret-marked or install-locked key is refused
    here, and every secret-SET path (provider key set, engine api_key, connector/vendor/
    ldap/oidc/smtp secrets) is /api-only. Bills nothing except jev_try, which is two-phase: without
    confirm=["jev_try"] it only returns a plan. Config changes are install-wide."""
    act = str(action or "").strip()
    async with _client() as client:
        if act == "get_config":
            return json.dumps(
                {
                    k: v
                    for k, v in (await _get_json(client, "/api/admin/config")).items()
                    if k != "ok"
                }
            )
        if act == "set_config":
            if not isinstance(settings, dict) or not settings:
                raise ToolError("action=set_config needs a non-empty `settings` map.")
            bad_secret = sorted(k for k in settings if _looks_secret(k))
            if bad_secret:
                raise ToolError(
                    "manage_install refuses secret-marked key(s) {}: a secret value "
                    "must never pass through a tool call. Set it over /api instead.".format(
                        ", ".join(bad_secret)
                    )
                )
            bad_locked = sorted(k for k in settings if k in _INSTALL_LOCKED_KEYS)
            if bad_locked:
                raise ToolError(
                    "these keys are install-locked and can only be set on the host: {}.".format(
                        ", ".join(bad_locked)
                    )
                )
            # Values go through AS GIVEN (#532): the route validates scalars and normalizes
            # bools itself, and refuses null/list/dict. str() here turned null into "None",
            # true into "True" and a list into a Python repr that was then stored.
            b: dict = {"settings": dict(settings.items())}
            if confirm:
                b["confirm"] = list(confirm)
            out = await _post_json(client, "/api/admin/config", b)
            out.pop("ok", None)
            return json.dumps(out)
        if act == "create_engine":
            if not str(name or "").strip() or not str(kind or "").strip():
                raise ToolError("action=create_engine needs name and kind.")
            _reject_secret_config(config)
            b = {"name": str(name), "kind": str(kind)}
            if base_url is not None:
                b["base_url"] = str(base_url)
            if model is not None:
                b["model"] = str(model)
            if isinstance(config, dict):
                b["config"] = config
            if enabled is not None:
                b["enabled"] = bool(enabled)
            out = await _post_json(client, "/api/admin/engines", b)
            out.pop("ok", None)
            return json.dumps(out)
        if act == "update_engine":
            if engine_id is None:
                raise ToolError("action=update_engine needs engine_id.")
            _reject_secret_config(config)
            b = {}
            if name is not None:
                b["name"] = str(name)
            if base_url is not None:
                b["base_url"] = str(base_url)
            if model is not None:
                b["model"] = str(model)
            if isinstance(config, dict):
                b["config"] = config
            if enabled is not None:
                b["enabled"] = bool(enabled)
            out = await _post_json(client, "/api/admin/engines/%d/update" % int(engine_id), b)  # noqa: UP031
            out.pop("ok", None)
            return json.dumps(out)
        if act == "test_engine":
            if engine_id is None:
                raise ToolError("action=test_engine needs engine_id.")
            out = await _post_json(client, "/api/admin/engines/%d/test" % int(engine_id), {})  # noqa: UP031
            return json.dumps(out)
        if act == "inspect_engine":
            if engine_id is None:
                raise ToolError("action=inspect_engine needs engine_id.")
            out = await _get_json(client, "/api/admin/engines/%d/inspect" % int(engine_id))  # noqa: UP031
            out.pop("ok", None)
            return json.dumps(out)
        if act == "jev_try":
            if engine_id is None:
                raise ToolError("action=jev_try needs engine_id.")
            fields = {
                "source": source,
                "target": target,
                "source_locale": source_locale,
                "target_locale": target_locale,
            }
            missing = sorted(k for k, v in fields.items() if not str(v or "").strip())
            if missing:
                raise ToolError("action=jev_try needs {}.".format(", ".join(missing)))
            # #518: a hosted Jev try bills, so it is two-phase like every spend tool: without
            # "jev_try" in `confirm` it returns a plan and sends nothing.
            if not (isinstance(confirm, list) and "jev_try" in [str(c) for c in confirm]):
                return json.dumps(
                    {
                        "plan": True,
                        "action": "jev_try",
                        "engine_id": int(engine_id),
                        "spends": "A hosted Jev engine bills a fraction of a cent per try and "
                        "honours the spend cap; a local judge server is free. Nothing was sent.",
                        "to_run": 'Tell the user, then call again with confirm=["jev_try"].',
                    }
                )
            b = {k: str(v) for k, v in fields.items()}
            if threshold is not None:
                b["threshold"] = int(threshold)
            out = await _post_json(client, "/api/admin/engines/%d/jev-try" % int(engine_id), b)  # noqa: UP031
            return json.dumps(out)
        if act in ("clear_provider_key", "show_provider_key"):
            if not str(provider or "").strip():
                raise ToolError(f"action={act} needs provider.")
            # _seg(provider) percent-encodes the model-supplied provider name into ONE
            # path segment; verify_mcp_parity's path-splice guard scans these f-strings
            # and confirms the _seg() quoting (its JoinedStr surface).
            if act == "clear_provider_key":
                out = await _post_json(
                    client,
                    f"/api/admin/providers/{_seg(provider)}/key/clear",
                    {},
                )
            else:
                out = await _get_json(
                    client,
                    f"/api/admin/providers/{_seg(provider)}/key",
                )
            out.pop("ok", None)
            return json.dumps(out)
        raise ToolError(
            f"unknown action {act!r}; use get_config/set_config/create_engine/update_engine/"
            "test_engine/inspect_engine/jev_try/clear_provider_key/show_provider_key."
        )


#: name -> handler. The OTHER half of the registry/server cross-check: a Tool with
#: no handler cannot be registered, and a handler with no Tool entry is never
#: reachable. register_tools() refuses both.
HANDLERS: dict = {
    "whoami": whoami,
    "release_manifest": release_manifest,
    "list_languages": list_languages,
    "list_capabilities": list_capabilities,
    "list_projects": list_projects,
    "create_project": create_project,
    "configure_project": configure_project,
    "project_automation": project_automation,
    "project_run": project_run,
    "submit_translation": submit_translation,
    "job_status": job_status,
    "get_result": get_result,
    "apply_tm": apply_tm,
    "approve_segment": approve_segment,
    "translate_text": translate_text,
    "list_keys": list_keys,
    "upsert_keys": upsert_keys,
    "import_keys": import_keys,
    "export_keys": export_keys,
    "key_changes": key_changes,
    "list_workflows": list_workflows,
    "run_workflow": run_workflow,
    "work_status": work_status,
    "ledger_summary": ledger_summary,
    "ledger_runs": ledger_runs,
    "create_workflow": create_workflow,
    "edit_workflow": edit_workflow,
    "cancel_workflow_run": cancel_workflow_run,
    "list_assets": list_assets,
    "edit_glossary": edit_glossary,
    "extract_terms": extract_terms,
    "manage_style_guide": manage_style_guide,
    "manage_custom_fields": manage_custom_fields,
    "set_project_fields": set_project_fields,
    "manage_formality": manage_formality,
    "project_notifications": project_notifications,
    "search_tm": search_tm,
    "search_workspace": search_workspace,
    "manage_tm": manage_tm,
    "attach_tm_source": attach_tm_source,
    "pin_glossary": pin_glossary,
    "translate_keys": translate_keys,
    "manage_key_image": manage_key_image,
    "list_key_images": list_key_images,
    "run_report": run_report,
    "list_connectors": list_connectors,
    "connector_status": connector_status,
    "connector_pull": connector_pull,
    "connector_translate": connector_translate,
    "connector_push": connector_push,
    "connector_sync": connector_sync,
    "connector_retry": connector_retry,
    "vendor_approve_quote": vendor_approve_quote,
    "vendor_reject_rework": vendor_reject_rework,
    "list_images": list_images,
    "manage_workflow_group": manage_workflow_group,
    "clone_workflow": clone_workflow,
    "translate_image": translate_image,
    "manage_users": manage_users,
    "manage_admin_ops": manage_admin_ops,
    "manage_install": manage_install,
    "list_queries": list_queries,
    "get_query": get_query,
    "open_query": open_query,
    "reply_to_query": reply_to_query,
    "set_query_status": set_query_status,
    "assign_query": assign_query,
    "my_work": my_work,
    "list_lqa_findings": list_lqa_findings,
    "log_lqa_finding": log_lqa_finding,
    "edit_lqa_finding": edit_lqa_finding,
    "withdraw_lqa_finding": withdraw_lqa_finding,
}


# ===========================================================================
# THE TWO-PHASE SPEND CONTRACT (maintainer decision 5) -- implemented ONCE.
# ===========================================================================
async def _plan_submit_translation(kwargs: dict) -> dict:
    """Read-only preview of a document translation.

    Honest about what is knowable BEFORE the upload: the segment count is not,
    because segmentation happens server-side on ingest. What IS knowable is the
    file, the locale count and - when a project is named - the engine that project
    resolves to, which is the fact the user actually wants stated."""
    # Same fail-closed flag as _read_local_file. This planner reads independently of the
    # helper AND runs before any token check, so it is the pre-auth path: it must be gated
    # on the same condition, not on _HTTP_MODE, which defaults open.
    if kwargs.get("file_path") and not _LOCAL_FILE_READS:
        raise ToolError(
            "file_path is only available to a local stdio server. Use inline data instead."
        )
    locales = _clean_locales(kwargs.get("targets"))
    src = Path(str(kwargs.get("file_path") or "")).expanduser()
    scope: dict = {"file": str(src), "locales": locales, "locale_count": len(locales)}
    warnings: list[str] = []
    try:
        # #755: the plan reads only what the executing call would be allowed to read.
        raw = _local_read_guard(str(kwargs.get("file_path") or ""), "file")
    except ToolError as exc:
        warnings.append(f"{exc} This call would fail before spending.")
    else:
        scope["source_bytes"] = len(raw)
        scope["source_lines"] = sum(
            1 for line in raw.decode("utf-8", "replace").splitlines() if line.strip()
        )
    scope["segments"] = (
        "unknown until upload: segmentation happens server-side, "
        "so the exact unit count is not knowable from here"
    )
    engine: dict = {
        "resolved": None,
        "note": (
            "resolved server-side at execution; a project's "
            "default_provider or its per-pair mapping decides it"
        ),
    }
    project_id = kwargs.get("project_id")
    if project_id is not None:
        async with _client() as client:
            project = await _get_json(client, "/api/projects/%d" % int(project_id))  # noqa: UP031
        engine = {
            "resolved": project.get("default_provider"),
            "provider_mode": project.get("provider_mode"),
            "pair_providers": project.get("pair_providers"),
            "note": "read from the project; per-pair overrides shown above",
        }
        scope["project"] = {
            "id": project.get("id"),
            "name": project.get("name"),
            "customer": project.get("customer"),
            "target_locales": project.get("target_locales"),
        }
        unknown = [loc for loc in locales if loc not in (project.get("target_locales") or [])]
        if unknown:
            warnings.append(
                "locales not yet on this project: {} (they would be added)".format(
                    ", ".join(unknown)
                )
            )
    else:
        # DO NOT say "it lands in your inbox project". It may not: the install's
        # ROUTING RULES are consulted first and decide both the destination
        # project and, where a rule sets one, the engine - and the fallback inbox
        # is per-DEPARTMENT and shared, not per-user. /api exposes no route that
        # previews a routing decision (GET /api/routing/rules/{id} is an exempt
        # orphan read and cannot enumerate), so the honest answer is that this
        # plan cannot tell you, not a confident guess.
        scope["project"] = (
            "none given - the SERVER decides. Its routing rules pick the "
            "destination project and can override the engine; only if no rule "
            "matches does it fall back to a shared per-department inbox. /api "
            "exposes no route to preview that decision, so neither the project "
            "nor the engine below can be resolved before the call"
        )
        engine["note"] = (
            "UNRESOLVED: with no project_id a routing rule may set the engine. "
            "Name a project_id to get a resolved engine in this plan"
        )
    return {
        "what_would_happen": (
            "Upload %s and start machine translation into %d locale(s): %s."  # noqa: UP031
            % (src.name or "the file", len(locales), ", ".join(locales) or "none")
        ),
        "scope": scope,
        "engine": engine,
        "warnings": warnings,
    }


async def _plan_translate_text(kwargs: dict) -> dict:
    text = str(kwargs.get("text") or "")
    provider = str(kwargs.get("provider") or "").strip()
    target = str(kwargs.get("target_locale") or "").strip()
    return {
        "what_would_happen": (
            "Translate one string of %d characters into %s."  # noqa: UP031
            % (len(text), target or "(no target given)")
        ),
        "scope": {
            "characters": len(text),
            "units": 1,
            "source_locale": (kwargs.get("source_locale") or "the install default"),
            "target_locale": target,
        },
        "engine": {
            "resolved": provider or None,
            "note": (
                "the install's default engine when no provider is "
                "given; this endpoint does NOT refuse an unconfigured "
                "engine - it returns mock output, so read "
                "provider_used in the real reply"
            ),
        },
        "warnings": ([] if target else ["no target_locale - this call would fail before spending"]),
    }


async def _plan_run_workflow(kwargs: dict) -> dict:
    """Read-only preview of a workflow run: the step graph names the engines,
    the document's segment counts size the spend, a batch read names what a
    rerun would touch. GET-only - the contextvar backstop enforces it."""
    mode = str(kwargs.get("mode") or "").strip()
    scope: dict = {"mode": mode or "(no mode given)"}
    warnings: list[str] = []
    engine: dict = {"resolved": None, "note": ("each step names its own provider; see steps below")}
    async with _client() as client:
        wid = kwargs.get("workflow_id")
        doc_id = kwargs.get("document_id")
        targets = _clean_locales(kwargs.get("locales"))
        if mode == "run":
            targets = _clean_locales([kwargs.get("locale") or ""])
        batch_id = kwargs.get("batch_id")
        if batch_id is not None and mode in {"rerun", "retry_failed"}:
            batch = await _get_json(client, "/api/batches/%d" % int(batch_id))  # noqa: UP031
            members = batch.get("members") or []
            # Only the latest attempt of each locale controls retry-failed.
            latest = {}
            for member in members:
                latest[member.get("locale")] = member
            failed = [loc for loc, member in latest.items() if member.get("status") == "failed"]
            scope["batch"] = {
                "id": batch_id,
                "status": batch.get("status"),
                "members": len(members),
                "failed_locales": failed,
            }
            wid, doc_id = batch.get("workflow_id"), batch.get("document_id")
            targets = (
                failed
                if mode == "retry_failed"
                else (targets or _clean_locales([kwargs.get("locale") or ""]))
            )
        if doc_id is not None:
            seg = await _get_json(client, "/api/documents/%d/segments" % int(doc_id))  # noqa: UP031
            locales = seg.get("locales") or {}
            scope["document"] = {
                "id": seg.get("id"),
                "filename": seg.get("filename"),
                "locales": {loc: entry.get("segments") for loc, entry in locales.items()},
            }
            if mode == "run_batch" and not targets:
                targets = list(locales)
        if wid is not None and doc_id is not None:
            variants = []
            for loc in targets:
                if loc not in locales:
                    warnings.append(
                        f"locale {loc!r} is not on document {doc_id} - this call "
                        "would fail before spending"
                    )
                    continue
                preview = await _get_json(
                    client,
                    f"/api/workflows/{int(wid)}/plan",
                    params={"doc_id": int(doc_id), "locale": loc},
                )
                variants.append(preview)
                warnings.extend(preview.get("warnings", []))
            scope["variants"] = variants
            if variants:
                first = variants[0]
                scope["workflow"] = {
                    "id": first["workflow_id"],
                    "name": first["workflow_name"],
                    "steps": len(first["steps"]),
                }
                if mode != "run":
                    scope["workflow"] = {
                        v["locale"]: {
                            "id": v["workflow_id"],
                            "name": v["workflow_name"],
                            "steps": len(v["steps"]),
                        }
                        for v in variants
                    }
                engine = {
                    "resolved": [
                        dict(st, locale=v["locale"], workflow_id=v["workflow_id"])
                        for v in variants
                        for st in v["steps"]
                    ],
                    "note": first["note"],
                }
                pauses = [st["step"] for st in engine["resolved"] if st["type"] == "human_review"]
                if pauses:
                    warnings.append(
                        "This workflow pauses for human review at: "
                        + ", ".join(pauses)
                        + "; ONLY a human can resume it."
                    )
    warnings.append(
        "the confirmed call waits up to 45 seconds including polling; done:false "
        "means still running, with next naming work_status and the run/batch id"
    )
    return {
        "what_would_happen": (
            "Launch mode=%s with a bounded wait, unless a refusal warning applies; "
            "priced step, fallback, judge and post-editor calls bill the account. "
            "Fallbacks are used when the step engine fails." % (mode or "?")
        ),
        "scope": scope,
        "engine": engine,
        "warnings": warnings,
    }


async def _plan_extract_terms(kwargs: dict) -> dict:
    gid = kwargs.get("glossary_id")
    scope: dict = {"document_id": kwargs.get("document_id"), "target": kwargs.get("target")}
    warnings: list[str] = []
    if gid is not None:
        async with _client() as client:
            g = await _get_json(client, "/api/glossaries/%d" % int(gid))  # noqa: UP031
        scope["glossary"] = {
            "id": g.get("id"),
            "name": g.get("name"),
            "status": g.get("status"),
            "pairs": g.get("pairs"),
        }
    return {
        "what_would_happen": (
            "Run one LLM term-extraction pass over document {} into glossary "
            "{}'s review queue. Nothing is added to the glossary itself; each "
            "candidate needs an explicit accept.".format(kwargs.get("document_id"), gid)
        ),
        "scope": scope,
        "engine": {
            "resolved": None,
            "note": (
                "the install's default engine; a spend-cap block "
                "is a clean refusal with no candidates created"
            ),
        },
        "warnings": warnings,
    }


async def _plan_translate_keys(kwargs: dict) -> dict:
    """Read-only preview of a key-project run: the workflow's step graph names the
    engines, the project names the locales the fan-out would touch. GET-only, and
    every enrichment read is best-effort — a keys-scoped token cannot read the
    docs-scoped project/workflow, so a 403 degrades the plan rather than failing it
    (the launch itself needs only the keys scope)."""
    pid, wid = kwargs.get("project_id"), kwargs.get("workflow_id")
    loc = str(kwargs.get("locale") or "").strip()
    scope: dict = {"project_id": pid, "workflow_id": wid, "locale": loc}
    warnings: list[str] = []
    engine: dict = {
        "resolved": None,
        "note": (
            "each workflow step names its own provider; a translate "
            "step on a keyless REAL engine refuses (409) before "
            "spending, exactly like document translate"
        ),
    }
    async with _client() as client:
        if wid is not None:
            try:
                wf = await _get_json(client, "/api/workflows/%d" % int(wid))  # noqa: UP031
                steps = wf.get("steps") or []
                engine = {
                    "resolved": [
                        {
                            "step": st.get("name"),
                            "type": st.get("type"),
                            "provider": st.get("provider"),
                            "model": st.get("model"),
                        }
                        for st in steps
                    ],
                    "note": "per-step engines read from the workflow",
                }
                scope["workflow"] = {
                    "id": wf.get("id"),
                    "name": wf.get("name"),
                    "steps": len(steps),
                }
            except Exception:
                warnings.append(
                    f"could not read workflow {wid} to preview its engines "
                    "(token scope or visibility); the launch still "
                    "resolves it server-side"
                )
        if pid is not None:
            try:
                proj = await _get_json(client, "/api/projects/%d" % int(pid))  # noqa: UP031
                targets = proj.get("target_locales") or []
                scope["project"] = {
                    "id": proj.get("id"),
                    "name": proj.get("name"),
                    "target_locales": targets,
                }
                fan = targets if loc == "all" else [loc]
                scope["would_launch_locales"] = [x for x in fan if x]
                if loc and loc != "all" and loc not in targets:
                    warnings.append(
                        f"locale {loc!r} is not a target on this project - this "
                        "call would fail before spending"
                    )
            except Exception:  # noqa: S110 -- dry-run preview only; the launch validates server-side
                pass
    warnings.append(
        "each launched run is asynchronous; read run status + qa_flags "
        "afterwards, and remember partial is not success"
    )
    return {
        "what_would_happen": (
            "Launch translation over key project {} through workflow {} into {}.".format(
                pid, wid, "every target locale" if loc == "all" else (loc or "?")
            )
        ),
        "scope": scope,
        "engine": engine,
        "warnings": warnings,
    }


async def _plan_translate_image(kwargs: dict) -> dict:
    """Read-only preview of an image translate: the image names its regions and the
    locales already on it; the project resolves the engine per pair. GET-only, and every
    enrichment read is best-effort so a visibility 403 degrades the plan, never fails it."""
    did = kwargs.get("document_id")
    loc = str(kwargs.get("locale") or "").strip()
    scope: dict = {"document_id": did, "locale": loc}
    warnings: list[str] = []
    engine: dict = {
        "resolved": None,
        "note": (
            "the image's project resolves the engine per pair; a "
            "keyless REAL engine refuses (409) before spending, "
            "exactly like document translate"
        ),
    }
    async with _client() as client:
        if did is not None:
            try:
                img = await _get_json(client, "/api/images/%d" % int(did))  # noqa: UP031
                scope["filename"] = img.get("filename")
                scope["kind"] = img.get("kind")
                scope["locales_present"] = img.get("locales")
                scope["regions"] = len(img.get("regions") or [])
                if loc and loc not in (img.get("locales") or []):
                    warnings.append(
                        f"locale {loc!r} is not on this image - the translate "
                        "would fail (400) before spending"
                    )
            except Exception:
                warnings.append(
                    f"could not read image {did} to enrich the plan "
                    "(visibility or feature gate); the translate still "
                    "resolves it server-side"
                )
    warnings.append(
        "read region status + qa_flags afterwards; a refusal-flagged "
        "region is withheld and needs_review is not a finished translation"
    )
    return {
        "what_would_happen": ("Translate image #{} into {} and re-render.".format(did, loc or "?")),
        "scope": scope,
        "engine": engine,
        "warnings": warnings,
    }


async def _plan_connector(kwargs: dict, action: str) -> dict:
    """Read-only preview shared by connector_translate / push / sync / retry.

    GET-ONLY, so the contextvar backstop is satisfied: it reads GET /api/connectors
    to NAME the external target (the connection's kind + name + status + project), and
    surfaces the mock-egress posture. It deliberately does NOT preview the record/
    issue/page COUNT: the count/preview routes (e.g. jira/count) are POSTs, and
    planning is read-only by contract (the count route is not a billing route, so it
    cannot be a PLAN_ESTIMATE_ROUTES whitelist entry either), so the honest answer is
    that the count is resolved at run time and bounded server-side by
    MAX_WRITES_PER_RUN -- not a number this plan can invent. A foreign connection is
    absent from the scoped list, so the plan says so rather than leaking one."""
    cid = kwargs.get("connection_id")
    warnings: list[str] = []
    # external_target and mock_egress live INSIDE scope: the _plan envelope only
    # carries scope/engine/estimate/warnings through, so a top-level key would be
    # silently dropped -- the whole point of the plan is that the user SEES these.
    target = {"note": "resolved at execution"}
    if cid is not None:
        async with _client() as client:
            conn = await _connection_by_id(client, int(cid))
        if conn is None:
            warnings.append(
                f"connection {cid} is not visible to this token (another org/customer, "
                "or it does not exist) - the confirmed call would 404, never a write"
            )
        else:
            target = {
                "connection_id": conn.get("id"),
                "kind": conn.get("kind"),
                "name": conn.get("name"),
                "status": conn.get("status"),
                "project_id": conn.get("project_id"),
                "customer_id": conn.get("customer_id"),
            }
            if (
                action == "translate"
                and str(conn.get("kind") or "").strip().lower() not in _CONNECTOR_TRANSLATE_ROUTES
            ):
                warnings.append(
                    "the {!r} connector has no per-item translate route; use "
                    "connector_sync instead".format(conn.get("kind"))
                )
    scope: dict = {
        "connection_id": cid,
        "external_target": target,
        "item_ids": kwargs.get("item_ids"),
        "target_locales": kwargs.get("target_locales") or ("the connection's configured targets"),
        "count": (
            "resolved at run time (the count/preview routes are POSTs a read-only "
            "plan cannot call); the server caps total writes per run "
            "(MAX_WRITES_PER_RUN)"
        ),
        "mock_egress": {"allow_mock_egress": "not readable over /api", "note": _MOCK_EGRESS_NOTE},
    }
    if kwargs.get("job_id") is not None:
        scope["retry_job_id"] = kwargs.get("job_id")
    return {
        "what_would_happen": (
            f"Run connector {action} for connection {cid} and WRITE the result back into the "
            "live external tenant."
        ),
        "scope": scope,
        "engine": {
            "resolved": None,
            "note": ("the connection's project resolves the engine per pair, server-side"),
        },
        "warnings": warnings,
    }


async def _plan_connector_translate(kwargs: dict) -> dict:
    # Refuse at PLAN time too (#532): a plan with `item_ids: null` looks like a per-item
    # plan but the run would cover the whole scope, so never offer one to approve.
    if not [i for i in (kwargs.get("item_ids") or []) if str(i).strip()]:
        raise ToolError(_CONNECTOR_TRANSLATE_NO_IDS)
    return await _plan_connector(kwargs, "translate")


async def _plan_connector_push(kwargs: dict) -> dict:
    return await _plan_connector(kwargs, "push")


async def _plan_connector_sync(kwargs: dict) -> dict:
    return await _plan_connector(kwargs, "sync")


async def _plan_connector_retry(kwargs: dict) -> dict:
    return await _plan_connector(kwargs, "retry")


async def _plan_project_run(kwargs: dict) -> dict:
    """Read-only preview of project_run: GET-only, names the connection and the schedule."""
    pid = kwargs.get("project_id")
    warnings: list[str] = []
    scope: dict = {"project_id": pid, "mode": kwargs.get("mode") or "the schedule's mode"}
    async with _client() as client:
        try:
            auto = await _get_json(client, "/api/projects/%d/automation" % int(pid))  # noqa: UP031
        except _HttpToolError:
            auto = {}
    if not auto.get("bound"):
        warnings.append(
            f"project {pid} is not visible to this token or has no content source - the "
            "confirmed call would be refused, never a write"
        )
    else:
        scope.update(
            {
                "connection": {
                    "id": auto.get("connection_id"),
                    "name": auto.get("connection_name"),
                    "kind": auto.get("connection_kind"),
                    "status": auto.get("connection_status"),
                },
                "schedule": auto.get("schedule"),
                "paused": auto.get("paused"),
                "per_run_spend_cap_usd": auto.get("run_cap_usd"),
                "count": (
                    "resolved at run time; the server caps total writes per run "
                    "(MAX_WRITES_PER_RUN) and the project's monthly budget is checked first"
                ),
            }
        )
        if auto.get("paused"):
            warnings.append("the automation is paused; a manual run is still allowed")
        if (kwargs.get("mode") or auto.get("mode")) == "full":
            warnings.append("a full run translates and writes every item again")
    return {
        "what_would_happen": (
            f"Run project {pid}'s content source: pull from its connection, translate what "
            "changed and WRITE the result back into the live external system."
        ),
        "scope": scope,
        "engine": {
            "resolved": None,
            "note": "the project resolves the engine per pair, server-side",
        },
        "warnings": warnings,
    }


#: The ONLY leg fields the quote/rework plans surface. Field-whitelisted so a future edit to a
#: planner (or a new consumer of this helper) cannot dump the full leg dict -- endpoint_host,
#: contract counts, protocol/backoff internals etc. stay off the agent surface (the claim token
#: is already never in leg_to_dict). Add a field here deliberately, never by returning the raw dict.
_PLAN_LEG_FIELDS: tuple[str, ...] = (
    "id",
    "status",
    "target_locale",
    "quoted_cost_usd",
    "quote_currency",
    "estimate_cost_usd",
    "actual_cost_usd",
    "rework_round",
)


async def _dispatch_leg_by_id(client: httpx.AsyncClient, did) -> dict | None:
    """The plan-relevant fields of the dispatch leg with id==did from
    GET /api/vendors/dispatches/{did}, or None when the caller cannot see it (another org, or
    it does not exist -- an identical 404). FIELD-WHITELISTED to _PLAN_LEG_FIELDS (never the
    endpoint host or any internal), and the claim token is never in the read anyway. GET-only,
    so it is plan-safe."""
    try:
        body = await _get_json(client, "/api/vendors/dispatches/%d" % int(did))  # noqa: UP031
    except _HttpToolError as exc:
        # Only a 404 means "not visible to this token". A 401/403/503 or a connection
        # fault is a real failure and must read as one, not as a missing leg (#532).
        if exc.status_code == 404:
            return None
        raise
    leg = body.get("dispatch") if isinstance(body, dict) else None
    if not isinstance(leg, dict):
        return None
    return {k: leg.get(k) for k in _PLAN_LEG_FIELDS}


async def _plan_vendor_approve_quote(kwargs: dict) -> dict:
    """Read-only plan for approving a vendor quote (finding #8). GETs the leg so the confirm
    authorizes a figure the operator was ACTUALLY SHOWN -- the recorded quote, the estimate,
    the leg state -- not an opaque 'amount already on the leg'."""
    did = kwargs.get("did")
    warnings: list[str] = []
    scope: dict = {"dispatch_id": did}
    async with _client() as client:
        leg = await _dispatch_leg_by_id(client, did)
    if leg is None:
        warnings.append(
            f"dispatch {did} is not visible to this token (another org, or it does not exist) - "
            "the confirmed call would 404, never an approval"
        )
    else:
        recorded = leg.get("quoted_cost_usd")
        override = kwargs.get("quoted_cost_usd")
        scope.update(
            {
                "status": leg.get("status"),
                "recorded_quote_usd": recorded,
                "amount_to_approve_usd": override if override is not None else recorded,
                "quote_currency": leg.get("quote_currency") or "USD",
                "scoping_estimate_usd": leg.get("estimate_cost_usd"),
                "target_locale": leg.get("target_locale"),
            }
        )
        if leg.get("status") != "quoted":
            warnings.append(
                "leg is {!r}, not 'quoted' - the confirmed call would 409 (nothing to approve)".format(  # noqa: E501
                    leg.get("status")
                )
            )
        if (override if override is not None else recorded) in (None, 0, 0.0):
            warnings.append(
                "no positive quote is recorded - approval refuses a $0/unknown quote; record "
                "the vendor's quote first, or pass quoted_cost_usd"
            )
    return {
        "what_would_happen": (
            f"APPROVE the vendor's quote on dispatch {did}: authorize the quoted human cost, let "
            "work proceed (QUOTED -> DISPATCHED so a return may be admitted), restart the wait "
            "clock, and notify the vendor. Nothing is authorized until you confirm."
        ),
        "scope": scope,
        "engine": {"resolved": None, "note": "human vendor work, priced by the vendor's MSA"},
        "warnings": warnings,
    }


async def _plan_vendor_reject_rework(kwargs: dict) -> dict:
    """Read-only plan for rejecting a delivered leg and re-dispatching a rework (finding #8).
    GETs the leg so the plan reports the leg state + its rework round vs the cap before the
    confirm re-sends billable work."""
    did = kwargs.get("did")
    warnings: list[str] = []
    scope: dict = {
        "dispatch_id": did,
        "note": kwargs.get("note") or "(none)",
        "cap": (
            "rework rounds are capped by the vendor_rework_max_rounds setting "
            "(default 1); at the cap this escalates to a human, no vendor "
            "round"
        ),
    }
    async with _client() as client:
        leg = await _dispatch_leg_by_id(client, did)
    if leg is None:
        warnings.append(
            f"dispatch {did} is not visible to this token (another org, or it does not exist) - "
            "the confirmed call would 404, never a rework"
        )
    else:
        scope.update(
            {
                "status": leg.get("status"),
                "current_rework_round": leg.get("rework_round"),
                "actual_cost_usd": leg.get("actual_cost_usd"),
                "target_locale": leg.get("target_locale"),
            }
        )
        if leg.get("status") != "returned":
            warnings.append(
                "leg is {!r}, not 'returned' - only a delivered return can be rejected (409)".format(  # noqa: E501
                    leg.get("status")
                )
            )
    return {
        "what_would_happen": (
            f"REJECT the delivered vendor work on dispatch {did} and RE-DISPATCH a rework to the "
            "same vendor with structured feedback (which segments failed which guard lanes + "
            "your note). At the rework cap it escalates to a human instead. It re-sends a job; "
            "it never injects content. Nothing is sent until you confirm."
        ),
        "scope": scope,
        "engine": {"resolved": None, "note": "billable human rework, priced by the MSA"},
        "warnings": warnings,
    }


#: tool name -> a READ-ONLY planner. A spend tool with no planner still gets the
#: envelope below; a planner only adds the honest per-tool detail.
PLANNERS: dict = {
    "translate_image": _plan_translate_image,
    "submit_translation": _plan_submit_translation,
    "translate_text": _plan_translate_text,
    "run_workflow": _plan_run_workflow,
    "extract_terms": _plan_extract_terms,
    "translate_keys": _plan_translate_keys,
    "connector_translate": _plan_connector_translate,
    "connector_push": _plan_connector_push,
    "connector_sync": _plan_connector_sync,
    "connector_retry": _plan_connector_retry,
    "project_run": _plan_project_run,
    "vendor_approve_quote": _plan_vendor_approve_quote,
    "vendor_reject_rework": _plan_vendor_reject_rework,
}


async def _plan(spec, kwargs: dict) -> str:
    """The read-only half of every spend tool. THE HANDLER IS NEVER CALLED HERE."""
    detail: dict = {}
    token = _PLANNING.set(True)
    try:
        planner = PLANNERS.get(spec.name)
        if planner is not None:
            detail = await planner(kwargs)
    finally:
        _PLANNING.reset(token)
    what = detail.get("what_would_happen", f"Run {spec.name}, which bills this account.")
    # #757: the confirmed call must bring this id back with the SAME arguments.
    plan_id = _PLANS.issue(
        spec.name, kwargs, loc_plan_binding.caller_fingerprint(_active_token()), what
    )
    return json.dumps(
        {
            "plan": True,
            "spent": False,
            "tool": spec.name,
            "tier": spec.tier,
            "plan_id": plan_id,
            "plan_expires_in_s": int(_PLANS.ttl_s),
            "what_would_happen": what,
            "scope": detail.get("scope", {}),
            "engine": detail.get("engine", {}),
            "budget": {
                "readable": False,
                "note": (
                    "spend caps, department budgets and per-identity rate limits "
                    "are enforced server-side and are not readable over /api; a "
                    "blocked call answers 429 or a refusal, never silence"
                ),
            },
            "estimate": detail.get("estimate"),
            "warnings": detail.get("warnings", []),
            "next_step": (
                "NOTHING WAS SENT AND NOTHING WAS SPENT. Tell the user the engine and "
                f"the scope above, then call {spec.name} again with the same arguments plus "
                f'confirm=true and plan_id="{plan_id}" if they agree. Changed arguments, '
                "a reused or an expired plan_id are refused."
            ),
        }
    )


#: #757: plans issued by _plan, redeemed once by a confirmed call (per process).
_PLANS = loc_plan_binding.PlanStore()
_HUMAN_CONFIRM_ENV = "LOC_STUDIO_MCP_HUMAN_CONFIRM"
_MRTR_VERSION = "2026-07-28"


def _client_can_elicit(call) -> bool:
    """True when THIS message speaks 2026-07-28 (MRTR) and its per-request _meta
    declares form elicitation (`{}` or `{"form": ...}`)."""
    ver = str(getattr(call, "protocol_version", "") or "")
    if not ver or ver < _MRTR_VERSION:
        return False
    params = getattr(call, "params", None) or {}
    meta = params.get("_meta") if isinstance(params, dict) else None
    caps = (meta or {}).get("io.modelcontextprotocol/clientCapabilities") or {}
    eli = caps.get("elicitation") if isinstance(caps, dict) else None
    return isinstance(eli, dict) and (not eli or "form" in eli)


def _human_confirmation(spec, kwargs: dict, caller: str, plan_id, idem=None):
    """Optional human approval of a confirmed spend (#757, LOC_STUDIO_MCP_HUMAN_CONFIRM).

    off (default): nothing here; the plan_id binding alone applies.
    ask: on a 2026-07-28 client that declares elicitation, the first confirmed round
      returns an MRTR InputRequiredResult (an elicitation/create form asking the HUMAN
      to approve the plan's summary); the client retries with the answer. A yes runs,
      a no or cancel consumes the plan and refuses. Other clients run on the plan_id.
    require: like ask, but a client that cannot elicit is refused (spend only from a
      host that can show the prompt, or from the web app / CLI).
    Returns None to continue, or the InputRequiredResult to send."""
    mode = loc_plan_binding.human_confirm_mode(os.environ.get(_HUMAN_CONFIRM_ENV))
    if mode == "off":
        return None
    summary = _PLANS.check(spec.name, kwargs, caller, plan_id, idem)  # plan errors first
    call = _MCP_CALL.get()
    if not _client_can_elicit(call):
        if mode == "require":
            raise loc_plan_binding.PlanRefused(
                "spend_refused: this server requires a person to approve each spend in the "
                "MCP client, and this client cannot show an approval prompt (it needs MCP "
                "2026-07-28 with elicitation). Nothing was sent. Run it from the web app or "
                "the CLI instead."
            )
        return None
    params = getattr(call, "params", None) or {}
    answer = loc_plan_binding.human_answer(
        params.get("inputResponses"), str(plan_id), params.get("requestState")
    )
    if answer is True:
        return None
    if answer is False:
        _PLANS.redeem(spec.name, kwargs, caller, plan_id)  # a "no" is final for this plan
        raise loc_plan_binding.PlanRefused(
            "spend_refused: the person declined in the approval prompt. Nothing was sent "
            "and nothing was spent."
        )
    from mcp_types import InputRequiredResult

    return InputRequiredResult.model_validate(
        {
            "resultType": "input_required",
            "inputRequests": {
                loc_plan_binding.CONFIRM_KEY: loc_plan_binding.elicitation_request(
                    f"{summary} This bills the account. Approve?"
                )
            },
            "requestState": str(plan_id),
        }
    )


@contextlib.contextmanager
def _request_token_frozen():
    """Freeze THIS request's /api token for the duration of the block.

    The ONE place the per-request header-vs-env decision is made, entered by BOTH
    _dispatch (tools) and the two resource handlers, so a tool call and a resource
    read share identical token semantics - the review found the resource path
    bypassed this, so a header-authed remote caller with an empty env token got the
    misleading stdio 'set LOC_STUDIO_TOKEN' error and both resources were unusable.
    Freeze once at the top of the handling task (same task the handler + its
    _call()s run in, like _PLANNING): in HTTP mode _request_header_token() returns a
    string (the header token, or "" when the request omitted it), we resolve
    the header alone once and store it, so no /api call downstream re-decides or
    leaks across requests. In stdio it returns None -> we set nothing ->
    _active_token() reads the env TOKEN, unchanged from before."""
    hdr = _request_header_token()  # may raise ToolError on a malformed header token
    reset = _REQUEST_TOKEN.set(hdr) if hdr is not None else None
    try:
        yield
    finally:
        if reset is not None:
            _REQUEST_TOKEN.reset(reset)


async def _dispatch(spec, handler, kwargs: dict) -> str:
    """The one place the spend contract lives.

    A tier="spend" tool called without confirm=true never reaches its handler: it
    gets a read-only plan. Everything else is a straight call.

    `is not True`, NOT `not kwargs.get("confirm")`. The truthiness test was a
    money bug: through any caller that does not run the pydantic arg model, the
    STRINGS "false", "no", "0" and "off" are all truthy in Python, so every one of
    them spent (proven, with spend-ledger and audit rows to show for it). The gate
    now demands the boolean itself; a string that merely looks like consent is a
    plan. Through FastMCP the arg model coerces "false"/"0"/"no" to real booleans
    first, so both paths agree - but this branch no longer DEPENDS on that."""
    token = _CURRENT_TOOL.set(spec.name)
    try:
        with _request_token_frozen():
            if spec.tier == "spend":
                # #757: plan_id is the dispatcher's argument, never the handler's.
                plan_id = kwargs.pop(registry.PLAN_ID_PARAM, None)
                if kwargs.get("confirm") is not True:
                    return external_content.mark(spec.name, await _plan(spec, kwargs))
            try:
                key = longrun.check_key(kwargs.get("idempotency_key"))
            except ValueError as exc:
                raise ToolError(str(exc)) from exc
            if spec.tier == "spend":
                caller = loc_plan_binding.caller_fingerprint(_active_token())
                try:
                    pending = _human_confirmation(spec, kwargs, caller, plan_id, key)
                    if pending is not None:
                        return pending  # an MRTR InputRequiredResult, not text: never marked
                    # A replay with the idempotency_key the plan was redeemed with passes;
                    # the API dedups that key, so nothing is spent twice (m-jobs #747).
                    _PLANS.redeem(spec.name, kwargs, caller, plan_id, key)
                except loc_plan_binding.PlanRefused as exc:
                    raise ToolError(str(exc)) from None
            idem_token = longrun.IDEMPOTENCY_KEY.set(key if spec.tier == "spend" else None)
            try:
                return external_content.mark(spec.name, await handler(**kwargs))
            finally:
                longrun.IDEMPOTENCY_KEY.reset(idem_token)
    finally:
        _CURRENT_TOOL.reset(token)


async def call_tool_unvalidated(name: str, arguments: dict | None = None) -> str:
    """Dispatch one tool by name WITHOUT the pydantic arg model. Not the client path.

    A real MCP client's arguments always pass through the schema-derived arg model
    first (FastMCP does it in `mcp.call_tool`); this seam deliberately skips it, so
    it is where a hostile or sloppy argument arrives at `_dispatch` raw. That makes
    it the right harness for proving the dispatcher is safe ON ITS OWN - and the
    WRONG harness for proving a contract the arg model is part of. The suite uses
    it for the first and `mcp.call_tool` for the second; the review that renamed
    this found the money gate's only evidence coming from the seam that skips
    validation."""
    spec = registry.tool_by_name(name)
    if spec is None:
        raise ToolError(f"no such tool: {name}")
    return await _dispatch(spec, HANDLERS[name], dict(arguments or {}))


# ===========================================================================
# REGISTRATION -- FastMCP tools built FROM the registry, not hand-written.
# ===========================================================================
_JSON_TYPES: dict = {
    "string": str,
    "integer": int,
    "number": float,
    "boolean": bool,
    "object": dict,
    "array": list,
    "null": type(None),
}

#: Every JSON-Schema keyword the derivation below actually TRANSLATES into the
#: pydantic model that validates a call.
#:
#: THE DERIVATION FAILS CLOSED ON ANYTHING ELSE, and that is the whole point of
#: naming them. Until this list existed the derivation failed OPEN: `pattern`,
#: `maxLength`, `minItems`, `const`, `format` and a nested `properties` were all
#: silently dropped, so the first tool to use one would have ADVERTISED a
#: constraint it did not enforce with the whole suite green. Zero constraints were
#: dropped at the time this landed - the cost of keeping it that way is one
#: deliberate edit here, next to the code that would have to learn the keyword.
#: verify_mcp_parity checks the registry against this list too, so the failure
#: arrives when the schema is written rather than when the tool is called.
_HONOURED_SCHEMA_KEYWORDS: frozenset = frozenset(
    {
        "type",  # -> the annotation
        "items",  # -> list[T]
        "enum",  # -> Literal[...]
        "minimum",  # -> Field(ge=...)
        "maximum",  # -> Field(le=...)
        "default",  # -> the parameter default
        "description",  # -> Field(description=...)
    }
)


class SchemaDerivationError(RuntimeError):
    """A registry schema says something the derivation cannot enforce.

    Raised at IMPORT time (register_tools runs at module scope), so a tool that
    would lie to the model about its constraints stops the server from starting
    rather than shipping."""


def _check_keywords(where: str, node: dict) -> None:
    extra = sorted(set(node or {}) - _HONOURED_SCHEMA_KEYWORDS)
    if extra:
        raise SchemaDerivationError(
            "{} uses JSON-Schema keyword(s) {}, which the tool derivation does "
            "not enforce. Advertising a constraint nothing checks is worse than "
            "not advertising it: teach _signature_from_schema the keyword and add "
            "it to _HONOURED_SCHEMA_KEYWORDS, or drop it from the schema.".format(
                where, ", ".join(extra)
            )
        )


def _annotation_for(prop: dict, where: str = "a property"):
    """The Python annotation a JSON-Schema property maps to, so FastMCP's pydantic
    model validates exactly what the registry advertises."""
    ptype = prop.get("type")
    if ptype not in _JSON_TYPES:
        raise SchemaDerivationError(
            f"{where} has type {ptype!r}, which the derivation cannot map to an annotation; an "
            "unmapped type used to become `Any`, i.e. no validation at all"
        )
    base = _JSON_TYPES[ptype]
    if ptype == "array":
        items = prop.get("items") or {}
        _check_keywords(f"{where}.items", items)
        item = _JSON_TYPES.get(items.get("type"), Any)
        base = list[item] if item is not Any else list
    if prop.get("enum"):
        base = Literal[tuple(prop["enum"])]  # type: ignore[misc]
    return base


def _signature_from_schema(schema: dict) -> inspect.Signature:
    """An inspect.Signature built from an input_schema.

    FastMCP derives a tool's advertised parameters from `inspect.signature(fn)`,
    so this is what makes the REGISTRY the thing the model sees, rather than
    whatever a wrapper happened to be written with."""
    props = (schema or {}).get("properties") or {}
    required = [r for r in ((schema or {}).get("required") or []) if r in props]
    params = []
    for name in list(required) + [p for p in props if p not in required]:
        prop = props[name]
        _check_keywords(f"property {name!r}", prop)
        ann = _annotation_for(prop, f"property {name!r}")
        # A CONSTRAINT THE SCHEMA ADVERTISES MUST BE ONE THE TOOL ENFORCES.
        # `limit` carries a maximum; advertising it while accepting anything would
        # make the model's contract a suggestion, and the refusal would arrive
        # later as a 422 from the API instead of before the call.
        bounds = {k: prop[j] for k, j in (("ge", "minimum"), ("le", "maximum")) if j in prop}
        field = Field(description=str(prop.get("description") or ""), **bounds)
        if name in required:
            params.append(
                inspect.Parameter(
                    name, inspect.Parameter.KEYWORD_ONLY, annotation=Annotated[ann, field]
                )
            )
            continue
        if "default" in prop:
            annotated = Annotated[ann, field]
            default = prop["default"]
        elif ann is str:
            # M1 fix (audit AUDIT-mcp-live-2026-08-05.md): a plain OPTIONAL string
            # must be annotated as EXACTLY `str`, never `str | None`. FastMCP's
            # func_metadata.pre_parse_json (mcp 1.28/1.29; byte-identical across the
            # bump, re-verified 2026-08-06) JSON-decodes any
            # argument whose field annotation `is not str` (server/fastmcp/utilities/
            # func_metadata.py:160). A `str | None` param that receives a value which
            # is itself valid JSON -- e.g. import_keys(data='{"app.beta":"Beta"}') --
            # is therefore decoded to a dict BEFORE validation and rejected with a
            # baffling "Input should be a valid string". Exact-`str` bypasses
            # pre_parse_json entirely (proven against the installed SDK), while
            # `default=None` keeps the parameter optional and preserves the
            # absent -> None contract every handler relies on. This is deliberately
            # NARROW: non-string optionals (list/dict/int) KEEP `| None`, because for
            # them pre-parsing a JSON string into the real value is the DESIRED
            # behaviour (e.g. targets='["de-DE"]'). Enum/Literal strings keep `| None`
            # too -- their allowed values are never JSON-object-shaped, so they never
            # trip the coercion, and `ann` for them is a Literal, not `str`.
            #
            # WrapValidator(_none_passthrough) restores one thing bare `str` would
            # break: an EXPLICIT null. `str | None` on main accepted `null` (mapping to
            # None); bare `str` refuses it, and LLM/typed-object clients routinely send
            # null for an unset optional (F1, caught in adversarial review). The wrap
            # validator maps null -> None while leaving FieldInfo.annotation exactly
            # `str` (pydantic hoists it into .metadata), so the pre_parse_json skip
            # holds AND `q=5` is still a string_type refusal.
            #
            # NOTE (F3): the DERIVED arg model's model_json_schema() renders this field
            # as {"type":"string","default":null} -- self-contradictory on paper -- but
            # it is never what the client sees: _advertise_registry_schema overrides the
            # tool's advertised schema with the registry's verbatim, and check_registration
            # (verify_mcp_tools) goes red if that override ever stops taking effect.
            annotated = Annotated[ann, field, WrapValidator(_none_passthrough)]
            default = None
        else:
            annotated = Annotated[ann | None, field]  # type: ignore[operator]
            default = None
        params.append(
            inspect.Parameter(
                name, inspect.Parameter.KEYWORD_ONLY, annotation=annotated, default=default
            )
        )
    return inspect.Signature(params, return_annotation=str)


def _description(spec) -> str:
    # #752: the summary only; the guide pointer is said once in the server instructions.
    # #748: plus the one external-content sentence on every marked tool.
    return spec.summary + external_content.description_suffix(spec.name)


def _structured(text: str, spec=None) -> CallToolResult:
    """#750: one handler answer as BOTH a text block (older clients, unchanged bytes)
    and structuredContent (the parsed JSON object, conforming to the advertised
    outputSchema). A handler answer is always json.dumps(...) of a dict today; any
    other JSON value, or text that is not JSON, is wrapped as {"value": ...} so the
    structured half is still an object.

    Gate (#750): clients (the mcp Python client, Claude Code) REJECT a result whose
    structuredContent does not match outputSchema, which would turn a working tool
    into a failing one. Most fields are relayed from /api, so if one ever collides
    with a typed field the schema declares (say a numeric `spent`), the answer is
    wrapped as {"value": ...} instead (always conforming); the text block is unchanged."""
    try:
        value = json.loads(text)
    except (TypeError, ValueError):
        value = text
    if not isinstance(value, dict):
        value = {tool_meta.NON_OBJECT_KEY: value}
    elif spec is not None and not tool_meta.conforms(value, tool_meta.output_schema_for(spec)):
        logging.getLogger("loc_mcp").warning(
            "%s: answer does not match its outputSchema; sent as {value: ...}", spec.name
        )
        value = {tool_meta.NON_OBJECT_KEY: value}
    return CallToolResult(content=[TextContent(type="text", text=text)], structured_content=value)


def _tool_callable(spec, handler):
    async def _tool(**kwargs) -> CallToolResult:
        out = await _dispatch(spec, handler, kwargs)
        if not isinstance(out, str):
            return out  # #757 MRTR: an InputRequiredResult goes to the client as it is
        return _structured(out, spec)

    _tool.__name__ = spec.name
    _tool.__doc__ = _description(spec)
    # A bare CallToolResult return: the SDK passes our result through untouched and
    # derives no schema of its own; the registry-derived one is attached below.
    _tool.__signature__ = _signature_from_schema(spec.input_schema).replace(
        return_annotation=CallToolResult
    )
    return _tool


def _advertise_registry_schema(server: MCPServer, spec) -> None:
    """Show the model the registry's schema verbatim, not pydantic's rendering.

    The derived model validates the same names, types and requiredness (it is
    built from the same schema), but pydantic renders an optional parameter as an
    `anyOf` and loses the authored ordering. The registry text is what was
    reviewed, so the registry text is what ships. Best-effort against SDK
    internals on purpose: if they move, the derived schema still describes the
    same parameters, and verify_mcp_tools' schema-parity assertion goes red."""
    tools = getattr(getattr(server, "_tool_manager", None), "_tools", None)
    tool = tools.get(spec.name) if isinstance(tools, dict) else None
    if tool is not None:
        try:  # noqa: SIM105
            tool.parameters = dict(spec.input_schema)
        except Exception:  # noqa: S110 -- SDK internals moved; verify_mcp_tools parity catches it
            pass


def _advertise_output_schema(server: MCPServer, spec) -> None:
    """#750: advertise the registry-derived outputSchema for one tool.

    FuncMetadata documents that its fields are read live and that a schema published
    without a model is advertised but not validated server-side, which is what we
    want: the schema declares only server-fixed fields (tool_meta.output_schema_for).
    Tool.output_schema is a cached_property, so any cached value is dropped.
    verify_mcp_tools asserts tools/list carries it, so SDK drift goes red, not silent."""
    tools = getattr(getattr(server, "_tool_manager", None), "_tools", None)
    tool = tools.get(spec.name) if isinstance(tools, dict) else None
    if tool is None:
        return
    tool.fn_metadata.output_schema = tool_meta.output_schema_for(spec)
    tool.__dict__.pop("output_schema", None)


def register_tools(server: MCPServer = mcp) -> list[str]:
    """Build every registry Tool onto `server`. Returns the names registered.

    Both directions are errors, not warnings: a Tool with no handler cannot be
    registered, and a handler with no Tool entry would never be reachable."""
    orphans = sorted(set(HANDLERS) - {t.name for t in registry.TOOLS})
    if orphans:
        raise RuntimeError("handlers with no registry entry: {}".format(", ".join(orphans)))
    names = []
    for spec in registry.TOOLS:
        handler = HANDLERS.get(spec.name)
        if handler is None:
            raise RuntimeError(
                f"registry tool {spec.name!r} has no handler in server.py - adding a tool is "
                "one Tool(...) entry plus one handler"
            )
        meta = tool_meta.annotations_for(spec)
        server.add_tool(
            _tool_callable(spec, handler),
            name=spec.name,
            title=meta["title"],
            description=_description(spec),
            annotations=ToolAnnotations.model_validate(meta),
        )
        _advertise_registry_schema(server, spec)
        _advertise_output_schema(server, spec)
        names.append(spec.name)
    return names


register_tools()


# ===========================================================================
# TOOL FILTERING (#751) -- which registered tools a connection SEES.
#
# Every tool stays registered; this only decides what tools/list shows (the
# 2026-07-28 spec lets the list vary by the authorization on the request) and
# refuses a call outside the active toolsets. Two independent narrowings:
#
#   * TOOLSETS: LOC_STUDIO_MCP_TOOLSETS (comma list of registry.toolsets() names,
#     default all) is the operator's ceiling, in stdio and HTTP. Over HTTP a
#     connection may narrow it further with an `X-Loc-Toolsets` header or a
#     `?toolsets=` query on the MCP URL, never widen it. A tool outside the active
#     toolsets is neither listed nor callable.
#   * SCOPES: a tool whose grant the calling key lacks is not listed. The key's
#     grants come from GET /api/account `scopes` (the caller's OWN key only),
#     cached per key fingerprint for _SCOPE_TTL_S. /api/account needs `docs`, so a
#     key without it answers 403: then docs tools are hidden, `admin` tools are
#     hidden too (the grant cannot be confirmed) and `api`/`keys` tools stay. When
#     the grants cannot be read at all (no token, a 401, an unreachable install, an
#     odd answer) the list FAILS NARROW: only the `api` tools plus `whoami`, whose
#     call explains the problem; that answer is cached for _SCOPE_FAIL_TTL_S only,
#     so the next tools/list retries. Never everything. Filtering is a convenience:
#     the API is the real control, and a scope-hidden tool stays callable (the API
#     answers 403 naming the grant).
#     LOC_STUDIO_MCP_SCOPE_FILTER=off lists every tool (admin / debugging).
# ===========================================================================
_SCOPE_TTL_S = 60.0
_SCOPE_FAIL_TTL_S = 5.0
_SCOPE_CACHE_MAX = 1024
_SCOPE_TIMEOUT = httpx.Timeout(5.0, connect=3.0)
#: sha256(token) -> (expires_at, scopes or None). Bounded; cleared when full.
_SCOPE_CACHE: dict[str, tuple[float, frozenset | None]] = {}
#: The grants assumed for a key whose /api/account answered 403 (no `docs`).
_NO_DOCS_SCOPES: frozenset = frozenset({"api", "keys"})
#: The grants assumed when the lookup failed (no token, 401, unreachable, malformed).
#: Compared by identity in _filter_listed_tools, which then also keeps `whoami`.
_UNKNOWN_SCOPES: frozenset = frozenset({"api"})


def _env_toolsets() -> frozenset | None:
    """LOC_STUDIO_MCP_TOOLSETS, resolved. An unknown name fails startup naming it."""
    raw = os.environ.get("LOC_STUDIO_MCP_TOOLSETS", "")
    try:
        return registry.resolve_toolsets(raw)
    except ValueError as exc:
        raise ValueError(f"LOC_STUDIO_MCP_TOOLSETS: {exc}") from None


def _env_scope_filter() -> bool:
    raw = os.environ.get("LOC_STUDIO_MCP_SCOPE_FILTER", "").strip().lower()
    if raw in ("", "on", "1", "true", "yes"):
        return True
    if raw in ("off", "0", "false", "no"):
        return False
    raise ValueError(f"LOC_STUDIO_MCP_SCOPE_FILTER must be on or off, got {raw!r}")


_ENV_TOOLSETS: frozenset | None = _env_toolsets()
_SCOPE_FILTER: bool = _env_scope_filter()


def _request_toolsets_raw() -> str:
    """The toolset list THIS HTTP request asked for (header, then query), or ""."""
    request = _current_http_request()
    if request is None:
        return ""
    raw = (request.headers.get("x-loc-toolsets") or "").strip()
    if not raw:
        query = getattr(request, "query_params", None)
        raw = str((query.get("toolsets") if query is not None else "") or "").strip()
    return raw


def _active_toolsets() -> frozenset | None:
    """Tool names the active toolsets allow, or None for every tool.

    The operator's env set is the ceiling; a request's own set intersects it.
    Raises ValueError for an unknown request toolset name."""
    requested = registry.resolve_toolsets(_request_toolsets_raw())
    if requested is None:
        return _ENV_TOOLSETS
    if _ENV_TOOLSETS is None:
        return requested
    return requested & _ENV_TOOLSETS


def _active_toolset_names() -> list[str] | str:
    """The active toolsets for whoami: every toolset wholly loaded, sorted, or "all"."""
    allowed = _active_toolsets()
    if allowed is None:
        return registry.ALL_TOOLSETS
    return sorted(n for n, members in registry.toolsets().items() if set(members) <= allowed)


def _active_toolset_names_env() -> list[str]:
    """The operator's toolsets (the env ceiling) for the startup report; [] = all."""
    if _ENV_TOOLSETS is None:
        return []
    return sorted(n for n, members in registry.toolsets().items() if set(members) <= _ENV_TOOLSETS)


def _check_toolset_call(name: str) -> None:
    try:
        allowed = _active_toolsets()
    except ValueError as exc:
        raise ToolError(f"Toolset header or query: {exc}") from None
    if allowed is not None and name not in allowed:
        raise ToolError(
            f"{name} is not in this connection's toolsets, so it is not available here. "
            "Ask the operator to add its toolset (LOC_STUDIO_MCP_TOOLSETS, or the "
            "X-Loc-Toolsets header / ?toolsets= query on the MCP URL)."
        )


async def _fetch_key_scopes() -> frozenset:
    """The calling key's grants from GET /api/account; _UNKNOWN_SCOPES when unknown."""
    token = _active_token()
    if not token:
        return _UNKNOWN_SCOPES
    fingerprint = hashlib.sha256(token.encode("ascii", "replace")).hexdigest()
    now = time.monotonic()
    hit = _SCOPE_CACHE.get(fingerprint)
    if hit is not None and hit[0] > now:
        return hit[1]
    scopes: frozenset = _UNKNOWN_SCOPES
    try:
        async with _client() as client:
            resp = await _call(client, "GET", "/api/account", timeout=_SCOPE_TIMEOUT)
        listed = resp.json().get("scopes")
        if isinstance(listed, list) and all(isinstance(s, str) for s in listed):
            scopes = frozenset(listed) | {"api"}
    except _HttpToolError as exc:
        scopes = _NO_DOCS_SCOPES if exc.status_code == 403 else _UNKNOWN_SCOPES
    except (ToolError, ValueError, AttributeError):
        scopes = _UNKNOWN_SCOPES
    ttl = _SCOPE_FAIL_TTL_S if scopes is _UNKNOWN_SCOPES else _SCOPE_TTL_S
    if len(_SCOPE_CACHE) >= _SCOPE_CACHE_MAX:
        _SCOPE_CACHE.clear()
    _SCOPE_CACHE[fingerprint] = (now + ttl, scopes)
    return scopes


async def _filter_listed_tools(tools: list) -> list:
    """tools/list narrowed by the active toolsets and the calling key's scopes."""
    try:
        allowed = _active_toolsets()
    except ValueError as exc:
        # An unknown toolset in the request's header/query: an invalid-params error the
        # client sees verbatim (a plain exception would reach it as "internal error").
        from mcp.shared.exceptions import MCPError
        from mcp_types import INVALID_PARAMS

        raise MCPError(code=INVALID_PARAMS, message=f"Toolset header or query: {exc}") from None
    scopes = None
    if _SCOPE_FILTER:
        try:
            with _request_token_frozen():
                scopes = await _fetch_key_scopes()
        except ToolError:  # a malformed header token: fail narrow, whoami explains
            scopes = _UNKNOWN_SCOPES
    keep_whoami = scopes is _UNKNOWN_SCOPES
    out = []
    for tool in tools:
        spec = registry.tool_by_name(tool.name)
        if allowed is not None and tool.name not in allowed:
            continue
        if (
            scopes is not None
            and spec is not None
            and spec.scope not in scopes
            and not (keep_whoami and tool.name == "whoami")
        ):
            continue
        out.append(tool)
    return out


# ===========================================================================
# RESOURCES -- the server's self-description, fetched LIVE from the install.
# ===========================================================================
def _resource_errors(uri: str):
    """Relay a ToolError raised under a resource read with its text, as mcp 1.x did.

    mcp 2 treats any exception that is not its own ResourceError as a crash and tells
    the client only "Error reading resource <uri>"; the setup/auth/relayed-4xx message
    is the whole point of the error, so it is re-raised as a ResourceError carrying the
    same "Error reading resource <uri>: <message>" text 1.x sent."""

    def deco(fn):
        @functools.wraps(fn)
        async def wrapper(*args, **kwargs):
            try:
                return await fn(*args, **kwargs)
            except ToolError as exc:
                raise _SdkResourceError(f"Error reading resource {uri}: {exc}") from exc

        return wrapper

    return deco


@mcp.resource(
    "locstudio://guide/llm",
    name="Localization OS driver's manual",
    description=(
        "The 'Are you an LLM? Read here' capability card of the "
        "CONNECTED install: the data model, the API, the CLI, "
        "the flows, and the rules an autonomous agent must not "
        "break."
    ),
    mime_type="text/markdown",
)
@_resource_errors("locstudio://guide/llm")
async def resource_guide_llm() -> str:
    """Served from GET /api/capabilities/llm rather than from a bundled copy: a
    remote MCP process must not describe a build it is not talking to.

    Enters _request_token_frozen() so a resource read forwards THIS request's header
    token exactly as a tool call does (the freeze lives in _dispatch, which resources
    do not go through - without this a header-authed remote caller with an empty env
    token would get the misleading stdio setup error)."""
    with _request_token_frozen():
        async with _client() as client:
            body = await _get_json(client, "/api/capabilities/llm")
        return body.get("text") or body.get("body") or ""


@mcp.resource(
    "locstudio://openapi",
    name="Localization OS OpenAPI schema",
    description=(
        "The live machine index of every route the connected "
        "install serves, generated from its code."
    ),
    mime_type="application/json",
)
@_resource_errors("locstudio://openapi")
async def resource_openapi() -> str:
    # Same per-request token freeze as the guide resource (see its docstring).
    with _request_token_frozen():
        async with _client() as client:
            resp = await _call(client, "GET", "/openapi.json")
        return resp.text


# ===========================================================================
def scope_report(http_mode: bool = False) -> str:
    """The startup note, printed to STDERR (stdout is the protocol channel).

    `http_mode` adapts the tail: in streamable-http mode tokens arrive PER REQUEST,
    so an empty env token is expected (not a misconfiguration) and the empty-token
    WARNING is suppressed; instead the report states that each request must carry
    its own key and points at the per-tool scope list above.

    It prints at startup, before any request, so it reports what it can KNOW
    (which grant each tool needs, which kill switches matter, which tools spend,
    the operator's toolsets). The calling key's own grants arrive per request on
    GET /api/account `scopes` (#751) and narrow tools/list then; GET /api/apikeys,
    which lists OTHER keys, stays exempt as credential enumeration."""
    by_scope: dict = {}
    for spec in registry.TOOLS:
        by_scope.setdefault(spec.scope, []).append(spec.name)
    lines = ["loc-studio MCP: %d tools against %s" % (len(registry.TOOLS), _public_base_url())]  # noqa: UP031
    for scope in sorted(by_scope):
        lines.append(
            "  scope %-5s -> %d tool(s): %s"  # noqa: UP031
            % (scope, len(by_scope[scope]), ", ".join(sorted(by_scope[scope])))
        )
    gated = sorted({t.feature_gate for t in registry.TOOLS if t.feature_gate})
    if gated:
        # Split by SEMANTICS, derived from registry.GATE_404_STYLE (the removed-style
        # set) so this prose cannot drift as features are added: a removed-style switch
        # 404s its whole surface when off; the rest are writes_off (reads keep
        # answering, only writes refuse). Hand-listing here went stale once already
        # (it named only keys+image after vendors/reports/routing had landed).
        removed = [g for g in gated if g in registry.GATE_404_STYLE]
        writes = [g for g in gated if g not in registry.GATE_404_STYLE]
        parts = []
        if removed:
            parts.append(
                "{} answer 404 when off, exactly like a missing object".format(", ".join(removed))
            )
        if writes:
            parts.append(
                "{} keep their reads answering and refuse only their "
                "writes, naming the switch".format(", ".join(writes))
            )
        lines.append("  kill switches: {} ({})".format(", ".join(gated), "; ".join(parts)))
    spend = sorted(t.name for t in registry.TOOLS if t.tier == "spend")
    lines.append(
        "  spend tools (two-phase - an unconfirmed call only plans): %s"
        % (", ".join(spend) or "none")
    )
    lines.append(
        "  tools/list hides a tool whose grant the calling key lacks (read from GET "
        "/api/account); LOC_STUDIO_MCP_SCOPE_FILTER=%s"
        % ("on" if _SCOPE_FILTER else "off - every tool is listed")
    )
    lines.append(
        "  toolsets: %s (LOC_STUDIO_MCP_TOOLSETS; names: %s)"  # noqa: UP031
        % (
            ", ".join(_active_toolset_names_env()) or registry.ALL_TOOLSETS,
            ", ".join(sorted(registry.toolsets())),
        )
    )
    if http_mode:
        # Per-request auth: each request brings its own key; the env token is ignored.
        # An empty one is NORMAL here (no WARNING). The per-tool scope
        # list above is what a caller uses to scope the key it will send.
        lines.append(
            "  HTTP mode: tokens arrive PER REQUEST - each request must "
            "carry its own lsu_ key in an 'Authorization: Bearer lsu_...' "
            "(or 'X-Loc-Token: lsu_...') header. The scopes each tool needs "
            "are listed above; LOC_STUDIO_TOKEN is ignored in HTTP mode."
        )
        if TOKEN:
            lines.append("  WARNING: ignoring LOC_STUDIO_TOKEN; HTTP callers need their own key.")
    elif not TOKEN:
        lines.append("  WARNING: LOC_STUDIO_TOKEN is empty - every tool will return a setup error.")
    return "\n".join(lines)


# ===========================================================================
# TRANSPORT (wave 3). stdio by default; --transport streamable-http serves the
# same tools over HTTP for an agent runtime the operator controls, behind their
# own proxy. See the module docstring for the auth + bind story.
# ===========================================================================
#: Hosts for which FastMCP auto-enables DNS-rebinding protection (its own list).
#: A bind to any of these keeps the module-level wildcard localhost allowlist; a
#: bind to anything else clears it - see _configure_http_bind.
_LOCALHOST: frozenset = frozenset({"127.0.0.1", "localhost", "::1"})

#: Default HTTP port. NOT 8000 on purpose: the Localization OS app serves 8000, and a
#: shared default would collide the instant an operator runs both on one machine.
_DEFAULT_HTTP_PORT = 8765


def _normalize_host(host: str) -> str:
    """The bind host, stripped and lower-cased.

    Both are load-bearing for the rebinding decision below: `--host LOCALHOST`,
    `Localhost` and `' 127.0.0.1 '` all name loopback, and without normalizing
    them the membership test would miss and silently turn DNS-rebinding protection
    OFF while still binding loopback. Lower-casing a real hostname is safe (DNS is
    case-insensitive); stripping whitespace is a plain paste-error fix."""
    return str(host).strip().lower()


def _port_arg(value: str) -> int:
    """argparse type for --port: a TCP port in 1..65535, refused cleanly otherwise
    (so an out-of-range port fails at parse time with a usage message, not later as
    an obscure bind error)."""
    try:
        port = int(value)
    except (TypeError, ValueError) as e:
        raise argparse.ArgumentTypeError(f"port must be an integer, got {value!r}") from e
    if not (1 <= port <= 65535):
        raise argparse.ArgumentTypeError("port must be in 1..65535, got %d" % port)  # noqa: UP031
    return port


def _configure_http_bind(host: str, port: int) -> None:
    """Point the FastMCP listener at host:port with the right rebinding posture.

    `host` is normalized (stripped + lower-cased) before ANYTHING is decided, and
    the normalized value is what is BOTH tested for loopback AND stored as the bind
    host - so a padded or upper-cased loopback host cannot bind loopback while
    taking the non-localhost (protection-off) branch.

    We SET transport_security explicitly for both cases rather than leaning on
    whatever the module-level FastMCP("loc-studio") happened to build, so the
    function is self-contained and idempotent (calling it twice cannot leave a
    stale posture from a previous call):

      * a LOCALHOST bind gets FastMCP's own localhost allowlist - 127.0.0.1:* /
        localhost:* / [::1]:*, wildcard on port so any --port is fine - so the
        listener still validates the Host header against loopback;
      * a NON-localhost bind gets `None`, which the TransportSecurityMiddleware
        treats as protection OFF. That is exactly what FastMCP's __init__ does when
        constructed with a non-localhost host: the allowlist would otherwise reject
        every request a reverse proxy forwards under the real hostname, so the
        proxy in front becomes the security boundary instead.

    An empty/whitespace-only host is a caller bug (it would let uvicorn bind ALL
    interfaces with protection off); main() rejects it before we get here, and this
    asserts the invariant so a direct caller cannot slip past it.

    uvicorn reads settings.host/port live at run() time, so mutating them here
    (rather than reconstructing FastMCP and re-registering every tool and resource)
    is sufficient and does not disturb the registry wiring."""
    h = _normalize_host(host)
    if not h:
        raise ValueError("bind host must not be empty")
    _HTTP_BIND.host = h
    _HTTP_BIND.port = port
    if h in _LOCALHOST:
        # Mirror FastMCP.__init__'s localhost posture exactly.
        from mcp.server.transport_security import TransportSecuritySettings

        _HTTP_BIND.transport_security = TransportSecuritySettings(
            enable_dns_rebinding_protection=True,
            allowed_hosts=["127.0.0.1:*", "localhost:*", "[::1]:*"],
            allowed_origins=["http://127.0.0.1:*", "http://localhost:*", "http://[::1]:*"],
        )
    else:
        _HTTP_BIND.transport_security = None


def _build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="server.py",
        description=(
            "Localization OS MCP server (the fourth surface). stdio by "
            "default; --transport streamable-http serves the same tools "
            "over HTTP for an agent runtime you control, behind your own "
            "TLS. The SERVER half of a claude.ai static-bearer connector is "
            "ready and verified; the claude.ai half depends on a GATED BETA "
            "(request-header auth) with an open bug (claude-ai-mcp#644) and "
            "is unproven. The OAuth-directory connector flow stays Phase C2. "
            "See README.md 'Connect from claude.ai'."
        ),
    )
    p.add_argument(
        "--transport",
        choices=("stdio", "streamable-http"),
        default="stdio",
        help=(
            "stdio (default) for a local client such as Claude Desktop or Claude "
            "Code; streamable-http to serve the same tools over HTTP. The HTTP "
            "listener has NO auth of its own - put an authenticating proxy in "
            "front."
        ),
    )
    p.add_argument(
        "--host",
        default="127.0.0.1",
        help=(
            "streamable-http bind interface (default 127.0.0.1: loopback only). "
            "Use 0.0.0.0 ONLY behind your own authenticating reverse proxy; a "
            "non-localhost host clears FastMCP's localhost rebinding allowlist."
        ),
    )
    p.add_argument(
        "--port",
        type=_port_arg,
        default=_DEFAULT_HTTP_PORT,
        help=(
            "streamable-http port in 1..65535 (default %d; deliberately not "  # noqa: UP031
            "8000, which the Localization OS app itself serves)." % _DEFAULT_HTTP_PORT
        ),
    )
    return p


def main(argv: list[str] | None = None) -> None:
    """Entrypoint. stdio is the default transport; --transport streamable-http
    serves the same registry, handlers and env-token /api model over HTTP.

    The scope report goes to STDERR in BOTH modes (in stdio mode stdout is the
    JSON-RPC protocol channel and must carry nothing else)."""
    global _HTTP_MODE
    parser = _build_arg_parser()
    args = parser.parse_args(argv)
    http = args.transport == "streamable-http"
    _HTTP_MODE = http
    # Local stdio is the ONLY start path that opens caller-supplied file reads. Any other
    # way of standing this server up (importing the module and serving the ASGI app, a
    # test harness, an embedder) leaves them denied, which is the safe default.
    _allow_local_file_reads(not http)
    print(scope_report(http_mode=http), file=sys.stderr, flush=True)
    if http:
        # Reject an empty/whitespace host cleanly here (with the usage message)
        # rather than let "" fall through to uvicorn as an all-interfaces bind with
        # rebinding protection off.
        if not _normalize_host(args.host):
            parser.error("--host must not be empty when serving streamable-http")
        _configure_http_bind(args.host, args.port)
        try:
            oauth_cfg = oauth_rs.config_from_env(BASE_URL)
        except ValueError as exc:
            parser.error(str(exc))
        if oauth_cfg is not None:
            print(
                f"  OAuth resource server ON (#753): resource {oauth_cfg.resource}, "
                f"authorization server {oauth_cfg.issuer}; every MCP request needs a token "
                f"(OAuth access token or a per-user lsu_ key); metadata at {oauth_cfg.prm_url}",
                file=sys.stderr,
                flush=True,
            )
        # Wave 4: the listener now takes a PER-REQUEST lsu_ token from each request's
        # Authorization/X-Loc-Token header (the env token is ignored). It
        # still performs no listener-level OAuth 401 (that is Phase C2), so run it
        # behind TLS you control - Fly edge or your own proxy - which is also the
        # DNS-rebinding boundary once --host is non-localhost.
        print(
            "  transport: streamable-http serving http://%s:%d%s  (each request "  # noqa: UP031
            "authenticates itself with a per-request lsu_ token in an "
            "'Authorization: Bearer' / 'X-Loc-Token' header; there is no "
            "listener-level OAuth 401 - run it behind TLS you control. The server "
            "half of a claude.ai static-bearer connector is ready here; the "
            "claude.ai half is a gated beta with an open bug - see README. Request "
            "bodies over %d MiB get a 413; at most %d sessions stay open and an idle "
            "one is reaped after %d s, so an unauthenticated caller cannot grow "
            "session state without limit)"
            % (
                _HTTP_BIND.host,
                _HTTP_BIND.port,
                _HTTP_BIND.streamable_http_path,
                _MAX_REQUEST_BODY_SIZE // (1024 * 1024),
                _MAX_SESSIONS,
                _SESSION_IDLE_TIMEOUT,
            ),
            file=sys.stderr,
            flush=True,
        )
        mcp.run(
            transport="streamable-http",
            host=_HTTP_BIND.host,
            port=_HTTP_BIND.port,
            streamable_http_path=_HTTP_BIND.streamable_http_path,
            transport_security=_HTTP_BIND.transport_security,
            max_request_body_size=_MAX_REQUEST_BODY_SIZE,
        )
    else:
        print("  transport: stdio", file=sys.stderr, flush=True)
        mcp.run()


if __name__ == "__main__":
    main()
