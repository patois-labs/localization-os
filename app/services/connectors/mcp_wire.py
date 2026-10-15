"""A minimal, synchronous MCP CLIENT for the `mcp_cms` connector (prototype, option 2).

Localization OS talks to a CMS's OWN MCP server over the Streamable HTTP transport, in
either protocol era (#754):

  * MODERN (2026-07-28, stateless): no handshake and no session. Every request carries
    `_meta` (`io.modelcontextprotocol/protocolVersion`, `clientInfo`,
    `clientCapabilities`) and the mirrored headers `MCP-Protocol-Version`, `Mcp-Method`
    and, for `tools/call`, `Mcp-Name` (plus `Mcp-Param-*` for parameters a tool marks
    `x-mcp-header`). The connection starts with `server/discover`.
  * LEGACY (2024-11-05 .. 2025-11-25): `initialize` + `notifications/initialized`, then
    an `Mcp-Session-Id` session, closed with DELETE.

`connect()` picks the era the way the spec's Backward Compatibility section (and the
mcp 2.3 Python client's 'auto' mode) does: probe `server/discover` first; a recognized
modern answer (a DiscoverResult naming 2026-07-28, or a modern JSON-RPC error) keeps the
connection modern; anything else (a 4xx without a modern error body, method not found,
a 5xx, a DiscoverResult that lists only handshake versions) falls back to `initialize`.
The `mcp_protocol` connection setting can pin `legacy` or `2026-07-28` instead.
Only `tools/list` and `tools/call` are used after that.

WHY NOT THE `mcp` SDK CLIENT. The repo pins `mcp==2.3.0` for the MCP SERVER under
integrations/mcp-server/, and the app itself never imports `mcp` (requirements.txt note).
The SDK's streamable-HTTP client is async and builds its own HTTP stack, so its traffic would
NOT pass through the connector egress seam (`Connector._client` -> `egress.GuardedTransport`:
SSRF resolve-and-pin on every hop, the private allowlist, no redirects to new hosts, the
32 MiB bounded read, the I/O budget, retry/backoff, the egress audit). Every byte this
client sends goes through the CONNECTOR's own `_request`, so the MCP URL gets exactly the
same guard as every REST connector and the hermetic suites inject a transport the usual way.

SECURITY POSTURE.
  * Tool results are DATA. This module returns parsed JSON (or text) to the caller and never
    interprets it: no result can choose which tool is called next, add arguments, or change
    the session. Server-initiated JSON-RPC requests that arrive inside an SSE stream
    (sampling, elicitation, roots) are ignored and never answered; we declare no client
    capabilities, so a conforming server does not send them, and a modern
    `input_required` result is refused rather than answered.
  * Only the caller names the tool. `McpSession.call_tool` is invoked by the connector with a
    tool name taken from the connection's mapping, never from a server response.
  * Header values mirrored from the body are ASCII-checked and base64-wrapped per the spec
    (`=?base64?...?=`), so a tool name or parameter can never inject a header line.
  * Error text from the server is sanitized (markup, controls and newlines stripped), capped
    and reported as quoted vendor text (`vendor_text.vendor_excerpt`, #748).

Documented in: docs/architecture/connectors.md
"""

from __future__ import annotations

import base64
import contextlib
import json
import logging
import re

from app.services.connectors.base import ConnectorError, io_budget
from app.services.connectors.vendor_text import clean_vendor_text, vendor_excerpt

log = logging.getLogger("loc_tms.connectors")

CLIENT_PROTOCOL = "2025-06-18"  # what the legacy handshake proposes
MODERN_PROTOCOL = "2026-07-28"
HANDSHAKE_PROTOCOLS = ("2024-11-05", "2025-03-26", "2025-06-18", "2025-11-25")
MODERN_PROTOCOLS = (MODERN_PROTOCOL,)
# Versions whose streamable-HTTP wire this client speaks (JSON or SSE response to a POST).
KNOWN_PROTOCOLS = (*HANDSHAKE_PROTOCOLS, *MODERN_PROTOCOLS)
PROTOCOL_MODES = ("auto", "legacy", MODERN_PROTOCOL)
DEFAULT_CALL_TIMEOUT_S = 30.0
MIN_CALL_TIMEOUT_S = 5.0
MAX_CALL_TIMEOUT_S = 300.0
_MAX_ERR = 200
_MAX_TOOL_PAGES = 20

# 2026-07-28 per-request metadata keys and protocol-defined error codes.
META_PROTOCOL = "io.modelcontextprotocol/protocolVersion"
META_CLIENT_INFO = "io.modelcontextprotocol/clientInfo"
META_CLIENT_CAPS = "io.modelcontextprotocol/clientCapabilities"
META_SERVER_INFO = "io.modelcontextprotocol/serverInfo"
HEADER_MISMATCH = -32020
MISSING_CLIENT_CAPABILITY = -32021
UNSUPPORTED_PROTOCOL_VERSION = -32022
_MODERN_ERRORS = (HEADER_MISMATCH, MISSING_CLIENT_CAPABILITY, UNSUPPORTED_PROTOCOL_VERSION)
#: method -> the params key mirrored into Mcp-Name.
_NAME_BEARING = {"tools/call": "name", "prompts/get": "name", "resources/read": "uri"}

_HEADER_SAFE = re.compile(r"^[\x20-\x7E]*$")
_B64_SENTINEL = re.compile(r"^=\?base64\?.*\?=$")
_TCHAR = re.compile(r"^[!#$%&'*+\-.^_`|~0-9A-Za-z]+$")
_JS_SAFE_INT = 2**53 - 1


class McpProtocolError(ConnectorError):
    """The server broke the protocol or answered with a JSON-RPC error."""


class McpToolError(ConnectorError):
    """A tools/call came back with isError: true. `text` is the server's message (data)."""

    def __init__(self, tool: str, text: str):
        self.tool = tool
        self.text = clean_vendor_text(text, _MAX_ERR)
        super().__init__(f"MCP tool {tool!r} reported an error: {vendor_excerpt(text)}")


class _RpcError(McpProtocolError):
    """A JSON-RPC error answer, keeping the code and data for era detection."""

    def __init__(self, method: str, code, message, data=None, status: int = 200):
        self.code = code
        self.data = data
        self.status = status
        super().__init__(f"MCP {method} error {code}: {_clip(message)}")


def _client_name() -> str:
    from app.branding import brand_name

    return brand_name()


def _clip(s) -> str:
    """Remote-server text quoted as vendor data (#748): sanitized, capped, marked."""
    return vendor_excerpt(s)


def encode_header_value(value: str) -> str:
    """A body value as an HTTP header value (spec 'Value Encoding'): plain printable
    ASCII without edge whitespace passes verbatim; anything else (non-ASCII, control
    characters, edge whitespace, or a value that already looks like the sentinel) is
    sent as `=?base64?<utf-8 base64>?=`."""
    value = str(value)
    if (
        _HEADER_SAFE.fullmatch(value)
        and value == value.strip()
        and not _B64_SENTINEL.fullmatch(value)
    ):
        return value
    return "=?base64?" + base64.b64encode(value.encode("utf-8")).decode("ascii") + "?="


def normalize_mode(raw) -> str:
    v = str(raw or "").strip().lower()
    return v if v in PROTOCOL_MODES else "auto"


def normalize_timeout(raw) -> float:
    try:
        v = float(str(raw).strip()) if str(raw or "").strip() else DEFAULT_CALL_TIMEOUT_S
    except ValueError:
        return DEFAULT_CALL_TIMEOUT_S
    if v != v:  # NaN
        return DEFAULT_CALL_TIMEOUT_S
    return min(max(v, MIN_CALL_TIMEOUT_S), MAX_CALL_TIMEOUT_S)


def _parse_sse(body: str) -> list:
    """JSON-RPC messages carried by a text/event-stream body (each event's data lines)."""
    out = []
    data: list[str] = []
    for line in [*body.splitlines(), ""]:
        if line == "":
            if data:
                # a malformed event is skipped, never executed
                with contextlib.suppress(ValueError):
                    out.append(json.loads("\n".join(data)))
                data = []
            continue
        if line.startswith(":"):
            continue
        field, _sep, value = line.partition(":")
        if field == "data":
            data.append(value[1:] if value.startswith(" ") else value)
    return out


def result_data(result: dict):
    """The DATA of a tools/call result: `structuredContent` when the server sends an object,
    else the text content parsed as JSON when it is JSON, else {"text": <joined text>}."""
    sc = result.get("structuredContent")
    if isinstance(sc, dict):
        return sc
    texts = [
        str(c.get("text") or "")
        for c in (result.get("content") or [])
        if isinstance(c, dict) and c.get("type") == "text"
    ]
    joined = "".join(texts)
    try:
        return json.loads(joined)
    except ValueError:
        return {"text": joined}


# ------------------------------------------------------------------ x-mcp-header
def header_params(schema) -> dict | None:
    """{header name: property path} for every `x-mcp-header` in a tool's inputSchema, or
    None when an annotation breaks the spec's constraints (the tool must then be dropped
    from tools/list). Only chains of `properties` keys are walked: an annotation reached
    through items/anyOf/$ref/... is not statically reachable and makes the tool invalid."""
    if not isinstance(schema, dict):
        return {}
    found: dict = {}
    seen: set = set()

    def walk(node, path) -> bool:
        if not isinstance(node, dict):
            return True
        for key, sub in node.items():
            if key == "properties" and isinstance(sub, dict):
                for prop, child in sub.items():
                    if not isinstance(child, dict):
                        continue
                    if "x-mcp-header" in child:
                        name = child.get("x-mcp-header")
                        typ = child.get("type")
                        if (
                            not isinstance(name, str)
                            or not name
                            or not _TCHAR.fullmatch(name)
                            or name.lower() in seen
                            or typ not in ("string", "integer", "boolean")
                        ):
                            return False
                        seen.add(name.lower())
                        found[name] = (*path, prop)
                    if not walk(child, (*path, prop)):
                        return False
            elif isinstance(sub, dict) and _has_header_anywhere(sub):
                return False  # an annotation behind items/anyOf/$defs/...: not reachable
            elif isinstance(sub, list) and any(_has_header_anywhere(x) for x in sub):
                return False
        return True

    return found if walk(schema, ()) else None


def _has_header_anywhere(node) -> bool:
    if isinstance(node, dict):
        return "x-mcp-header" in node or any(_has_header_anywhere(v) for v in node.values())
    if isinstance(node, list):
        return any(_has_header_anywhere(v) for v in node)
    return False


def _param_header_value(value) -> str | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        if abs(value) > _JS_SAFE_INT:
            raise McpProtocolError("An x-mcp-header integer is outside the safe range.")
        return str(value)
    if isinstance(value, str):
        return encode_header_value(value)
    return None  # a non-primitive value is never mirrored


class McpSession:
    """One MCP connection over the connector's guarded HTTP path. Built lazily per
    connector run; `close()` ends a legacy server session (best effort)."""

    def __init__(
        self,
        connector,
        url: str,
        auth_headers: dict,
        *,
        mode: str = "auto",
        call_timeout_s: float = DEFAULT_CALL_TIMEOUT_S,
    ):
        self._c = connector
        self.url = url
        self._auth = dict(auth_headers)
        self.mode = normalize_mode(mode)
        self.call_timeout_s = normalize_timeout(call_timeout_s)
        self.session_id: str | None = None
        self.protocol: str | None = None
        self.server_info: dict = {}
        self._next = 0
        self._tools: dict | None = None
        self._header_params: dict = {}

    @property
    def modern(self) -> bool:
        return self.protocol in MODERN_PROTOCOLS

    # ------------------------------------------------------------------ wire
    def _headers(self, method: str | None = None, params: dict | None = None) -> dict:
        h = {
            **self._auth,
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        }
        if self.modern:
            h["MCP-Protocol-Version"] = self.protocol
            if method:
                h["Mcp-Method"] = method
                key = _NAME_BEARING.get(method)
                if key and isinstance((params or {}).get(key), str):
                    h["Mcp-Name"] = encode_header_value(params[key])
            return h  # no session id on the 2026-07-28 wire
        if self.session_id:
            h["Mcp-Session-Id"] = self.session_id
        if self.protocol:
            h["MCP-Protocol-Version"] = self.protocol
        return h

    def _post(self, payload: dict, headers: dict, *, idempotent: bool = True):
        with self._c._client() as client, io_budget(self.call_timeout_s):
            return self._c._request(
                client,
                "POST",
                self.url,
                content=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                headers=headers,
                idempotent=idempotent,
                timeout=self.call_timeout_s,
            )

    def _meta(self, protocol: str) -> dict:
        return {
            META_PROTOCOL: protocol,
            META_CLIENT_INFO: {"name": _client_name(), "version": "1"},
            META_CLIENT_CAPS: {},
        }

    def _send(
        self,
        method: str,
        params: dict | None,
        *,
        idempotent: bool = True,
        extra_headers: dict | None = None,
    ):
        """POST one request; return (http response, our JSON-RPC message or None)."""
        self._next += 1
        rid = self._next
        msg = {"jsonrpc": "2.0", "id": rid, "method": method}
        if self.modern:
            params = dict(params or {})
            params["_meta"] = {**(params.get("_meta") or {}), **self._meta(self.protocol)}
        if params is not None:
            msg["params"] = params
        headers = self._headers(method, params)
        if extra_headers:
            headers.update(extra_headers)
        r = self._post(msg, headers, idempotent=idempotent)
        return r, self._find_reply(r, rid)

    @staticmethod
    def _find_reply(r, rid):
        ctype = (r.headers.get("content-type") or "").split(";")[0].strip().lower()
        if ctype == "text/event-stream":
            messages = _parse_sse(r.text)
        elif ctype == "application/json" or r.status_code == 200:
            try:
                body = r.json()
            except ValueError:
                return None
            messages = body if isinstance(body, list) else [body]
        else:
            return None
        for m in messages:
            # Only OUR response counts. Server notifications and server->client requests
            # riding the same stream are data we do not act on. An error the server could
            # not tie to a request id (id null) still answers this single POST.
            if not isinstance(m, dict) or ("result" not in m and "error" not in m):
                continue
            if "error" in m and not isinstance(m.get("error"), dict):
                continue  # not a JSON-RPC error object (e.g. a proxy's {"error": "..."})
            if m.get("id") == rid or ("error" in m and m.get("id") is None):
                return m
        return None

    def _rpc(
        self,
        method: str,
        params: dict | None = None,
        *,
        idempotent: bool = True,
        _retried: bool = False,
        extra_headers: dict | None = None,
    ) -> dict:
        if self.protocol is None and method not in ("initialize", "server/discover"):
            self.connect()
        r, reply = self._send(method, params, idempotent=idempotent, extra_headers=extra_headers)
        if (
            r.status_code == 404
            and not self.modern
            and self.session_id
            and not _retried
            and idempotent
        ):
            # The server dropped our session: start a new one and resend once (reads only;
            # a write is never resent on an ambiguous outcome).
            self.session_id = None
            self.protocol = None
            self.initialize()
            return self._rpc(
                method, params, idempotent=idempotent, _retried=True, extra_headers=extra_headers
            )
        if r.status_code in (401, 403):
            raise ConnectorError(
                f"The MCP server refused the credential ({r.status_code}). Check the token, or "
                "reconnect with OAuth if this connection uses it."
            )
        if isinstance(reply, dict) and "error" in reply:
            err = reply.get("error") or {}
            exc = _RpcError(
                method, err.get("code"), err.get("message"), err.get("data"), r.status_code
            )
            if (
                self.modern
                and self.mode == "auto"
                and method != "server/discover"
                and not _retried
                and exc.code in _MODERN_ERRORS
            ):
                # Gate fix (#754): a server that answered discover but then rejects our
                # 2026-07-28 envelope (header mismatch, unsupported version, missing
                # capability) rejected it BEFORE running anything, so under `auto` we
                # drop to the handshake once and resend, writes included. A server that
                # cannot do the handshake either gets the original error.
                return self._downgrade_and_resend(method, params, idempotent, exc)
            raise exc
        if r.status_code != 200:
            raise McpProtocolError(f"MCP {method} answered HTTP {r.status_code}: {_clip(r.text)}")
        if reply is None:
            ctype = (r.headers.get("content-type") or "").split(";")[0].strip().lower()
            if ctype not in ("application/json", "text/event-stream"):
                raise McpProtocolError(f"MCP {method}: response is not JSON.")
            raise McpProtocolError(f"MCP {method}: no response for request {self._next}.")
        sid = r.headers.get("mcp-session-id")
        if sid and method == "initialize":
            if len(sid) > 512 or any(ord(ch) < 0x21 or ord(ch) > 0x7E for ch in sid):
                raise McpProtocolError("MCP server sent an invalid session id.")
            self.session_id = sid
        res = reply.get("result")
        if not isinstance(res, dict):
            raise McpProtocolError(f"MCP {method}: result is not an object.")
        rtype = res.get("resultType")
        if rtype not in (None, "complete"):
            # 2026-07-28: an absent resultType is "complete"; "input_required" asks for
            # sampling/elicitation/roots we never declared; anything else is invalid.
            raise McpProtocolError(
                f"MCP {method}: the server asked for {_clip(rtype)} input this connector "
                "does not provide (it declares no client capabilities)."
            )
        return res

    def _downgrade_and_resend(self, method: str, params, idempotent: bool, exc: Exception):
        log.warning(
            "mcp_cms: %s rejected on the 2026-07-28 wire (%s); falling back to the "
            "initialize handshake",
            method,
            _clip(str(exc)),
        )
        try:
            self.initialize()
        except ConnectorError:
            self.protocol = MODERN_PROTOCOL
            self.session_id = None
            raise exc from None
        # tools/list on the modern wire may have dropped tools; list again on this wire.
        self._tools, self._header_params = None, {}
        return self._rpc(method, params, idempotent=idempotent, _retried=True)

    def _notify(self, method: str) -> None:
        r = self._post({"jsonrpc": "2.0", "method": method}, self._headers())
        if r.status_code not in (200, 202, 204):
            raise McpProtocolError(f"MCP {method} answered HTTP {r.status_code}.")

    # --------------------------------------------------------------- methods
    def connect(self) -> dict:
        """Pick the era (see the module docstring) and set up the connection. Returns the
        DiscoverResult (modern) or InitializeResult (legacy)."""
        if self.mode == "legacy":
            return self.initialize()
        if self.mode == MODERN_PROTOCOL:
            return self.discover(fallback=False)
        return self.discover(fallback=True)

    def discover(self, *, fallback: bool = True) -> dict:
        """`server/discover` at 2026-07-28. With `fallback`, anything that is not positive
        evidence of a modern server runs the legacy `initialize` handshake instead."""
        self.protocol = MODERN_PROTOCOL
        self.session_id = None
        try:
            res = self._rpc("server/discover", {})
        except _RpcError as e:
            self.protocol = None
            if e.code == UNSUPPORTED_PROTOCOL_VERSION:
                supported = (e.data or {}).get("supported") if isinstance(e.data, dict) else None
                supported = [str(v) for v in supported] if isinstance(supported, list) else []
                if any(v in HANDSHAKE_PROTOCOLS for v in supported):
                    if fallback:
                        return self.initialize()
                    raise McpProtocolError(
                        f"The MCP server speaks {_clip(', '.join(supported))}, not "
                        f"{MODERN_PROTOCOL}: set the connection's MCP protocol to auto or legacy."
                    ) from None
                raise McpProtocolError(
                    "The MCP server supports protocol version(s) "
                    f"{_clip(', '.join(supported) or 'unknown')}, none of which this "
                    f"connector speaks ({', '.join(KNOWN_PROTOCOLS)})."
                ) from None
            if e.code in _MODERN_ERRORS or not fallback:
                raise
            return self.initialize()
        except McpProtocolError:
            self.protocol = None
            if not fallback:
                raise
            return self.initialize()  # a 4xx/5xx without a modern error body: legacy
        versions = res.get("supportedVersions")
        versions = [str(v) for v in versions] if isinstance(versions, list) else []
        if MODERN_PROTOCOL not in versions:
            self.protocol = None
            if fallback and (not versions or any(v in HANDSHAKE_PROTOCOLS for v in versions)):
                # A discover-answering server that advertises only handshake versions
                # is a legacy advertisement.
                return self.initialize()
            raise McpProtocolError(
                f"MCP server speaks protocol {_clip(', '.join(versions))}, not supported."
            )
        meta = res.get("_meta") if isinstance(res.get("_meta"), dict) else {}
        info = meta.get(META_SERVER_INFO)
        self.server_info = info if isinstance(info, dict) else {}
        return res

    def initialize(self) -> dict:
        self.protocol = None
        self.session_id = None
        res = self._rpc(
            "initialize",
            {
                "protocolVersion": CLIENT_PROTOCOL,
                "capabilities": {},
                "clientInfo": {"name": _client_name(), "version": "1"},
            },
        )
        ver = str(res.get("protocolVersion") or "")
        if ver not in HANDSHAKE_PROTOCOLS:
            raise McpProtocolError(f"MCP server speaks protocol {_clip(ver)}, not supported.")
        self.protocol = ver
        self.server_info = res.get("serverInfo") if isinstance(res.get("serverInfo"), dict) else {}
        self._notify("notifications/initialized")
        return res

    def list_tools(self) -> dict:
        """{name: tool dict} for every tool the server lists (paged, bounded). On the
        2026-07-28 wire a tool whose `x-mcp-header` annotations break the spec is left
        out (the spec's MUST), with a log line naming it."""
        if self._tools is not None:
            return self._tools
        tools: dict = {}
        cursor = None
        for _ in range(_MAX_TOOL_PAGES):
            res = self._rpc("tools/list", {"cursor": cursor} if cursor else {})
            for t in res.get("tools") or []:
                if not (isinstance(t, dict) and isinstance(t.get("name"), str)):
                    continue
                if self.modern:
                    hp = header_params(t.get("inputSchema"))
                    if hp is None:
                        log.warning(
                            "mcp_cms: tool %r dropped: invalid x-mcp-header annotation",
                            _clip(t["name"]),
                        )
                        continue
                    self._header_params[t["name"]] = hp
                tools[t["name"]] = t
            cursor = res.get("nextCursor")
            if not cursor:
                break
        self._tools = tools
        return tools

    def _param_headers(self, name: str, arguments: dict) -> dict:
        if not self.modern:
            return {}
        if name not in self._header_params and self._tools is None:
            self.list_tools()
        out = {}
        for header, path in (self._header_params.get(name) or {}).items():
            node = arguments
            for key in path:
                node = node.get(key) if isinstance(node, dict) else None
            value = _param_header_value(node)
            if value is not None:
                out[f"Mcp-Param-{header}"] = value
        return out

    def call_tool(self, name: str, arguments: dict, *, idempotent: bool = True):
        """Call ONE tool and return its result DATA. Raises McpToolError on isError."""
        res = self._rpc(
            "tools/call",
            {"name": name, "arguments": arguments},
            idempotent=idempotent,
            extra_headers=self._param_headers(name, arguments),
        )
        if res.get("isError"):
            data = result_data(res)
            text = data.get("text") if isinstance(data, dict) and "text" in data else data
            raise McpToolError(name, text if isinstance(text, str) else json.dumps(text)[:_MAX_ERR])
        return result_data(res)

    def close(self) -> None:
        if self.modern or not self.session_id:
            return  # the 2026-07-28 wire has no session to end
        try:
            with self._c._client() as client:
                self._c._request(client, "DELETE", self.url, headers=self._headers())
        except Exception as e:  # best effort: the server reaps idle sessions anyway
            log.info("mcp_cms: session close failed (%s)", type(e).__name__)
        self.session_id = None
