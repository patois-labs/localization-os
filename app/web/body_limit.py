"""Request body size limits (security review 2026-09, #287).

Nothing used to bound a request body. The CSRF check reads a form body before anyone is
signed in, and FastAPI parses a JSON body before the API key is checked, so one anonymous
256 MiB form POST to the sign-in page, or a 512 MiB JSON POST to /api with no key, could
take a 1 GB machine down. ``BodyLimitMiddleware`` refuses an oversized body with a clean
413 BEFORE any of it is read:

* a declared ``Content-Length`` over the cap is refused without reading a byte;
* a streamed (chunked) body is counted as it arrives and refused the moment it passes
  the cap. If the route already started answering, the connection is simply cut.

Two caps, both configurable (``app/config.py``):

* ``REQUEST_MAX_ANON_BODY_BYTES`` (1 MiB): every sign-in, webhook and SCIM path, and
  every other request that is not signed in (no valid session, no valid API key).
* ``REQUEST_MAX_BODY_BYTES`` (the upload cap sent base64 in JSON, plus room): a signed-in
  caller, or any caller while sign-in is off. Routes keep their own, smaller caps.

Who is signed in is only worked out for a body larger than the anonymous cap, so a normal
request costs one header read. The middleware is mounted just inside the session
middleware (``scope["session"]`` is decoded) and outside the security middleware, the
page guard and every route. A failure to decide is treated as not signed in.

Documented in: docs/architecture/browser-surface.md
"""

from __future__ import annotations

import logging

from starlette.requests import Request
from starlette.responses import JSONResponse, PlainTextResponse

from app import config
from app.web import route_path as routing

_logger = logging.getLogger("loc_tms.body_limit")

# Paths that are reached before (or instead of) signing in. Their bodies are small by
# contract, so they keep the anonymous cap even for a signed-in browser.
_PRE_AUTH_PREFIXES = ("/auth/", "/webhooks/", "/scim/")


class BodyTooLarge(Exception):
    """Raised from the wrapped receive channel when a streamed body passes the cap."""


def _too_large_response(path: str, cap: int):
    advice = "Upload a smaller file or split the request."
    msg = f"The request is too large (limit {cap} bytes). {advice}"
    headers = {"Connection": "close"}
    if path.startswith("/scim/"):
        return JSONResponse(
            {
                "schemas": ["urn:ietf:params:scim:api:messages:2.0:Error"],
                "status": "413",
                "detail": msg,
            },
            status_code=413,
            headers=headers,
            media_type="application/scim+json",
        )
    if path.startswith(("/api", "/webhooks/")):
        return JSONResponse({"ok": False, "error": msg}, status_code=413, headers=headers)
    return PlainTextResponse(msg, status_code=413, headers=headers)


def _signed_in(scope, path: str) -> bool:
    """True when this request carries a valid identity (or sign-in is off)."""
    from app.kernel import security

    if not security.auth_required():
        return True
    request = Request(scope)
    if path.startswith("/api"):
        from app.web.deps import api_identity

        return api_identity(request) is not None
    session = scope.get("session")
    if not session or not session.get("uid"):
        return False
    # An expired session is not signed in. Checked here (a pure clock read) so the
    # sign-in wall still sees the expired session and answers it with its own page.
    if security.session_clock(session)["expired"]:
        return False
    return security.session_user(request) is not None


def body_cap(scope) -> int:
    """The largest body this request may send (the larger cap only for a known caller)."""
    path = routing.route_path(scope)
    anon = config.REQUEST_MAX_ANON_BODY_BYTES
    if path.startswith(_PRE_AUTH_PREFIXES):
        return anon
    try:
        if _signed_in(scope, path):
            return max(config.REQUEST_MAX_BODY_BYTES, anon)
    except Exception:
        _logger.warning("body limit: could not resolve the caller; using the anonymous cap")
    return anon


async def _cap_off_loop(scope) -> int:
    """`body_cap` for an over-anonymous-size body, run in the reviewed bounded worker pool
    (app/web/deps.py::offload_parse), never on the event loop (#495) and never in
    Starlette's unbounded default pool. When no worker comes free in time the caller cannot
    be resolved, which `body_cap` already answers with the anonymous cap (fail closed)."""
    from app.web.deps import WorkerBusy, offload_parse

    try:
        return await offload_parse(body_cap, scope)
    except WorkerBusy:
        _logger.warning("body limit: no worker free to resolve the caller; using the anonymous cap")
        return config.REQUEST_MAX_ANON_BODY_BYTES


def _declared_length(scope) -> int | None:
    for key, value in scope.get("headers") or ():
        if key == b"content-length":
            try:
                return int(value)
            except ValueError:
                return None
    return None


class BodyLimitMiddleware:
    """Refuse a request body over its cap with 413, before the body is read."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope.get("type") != "http":
            return await self.app(scope, receive, send)
        path = routing.route_path(scope)
        anon = config.REQUEST_MAX_ANON_BODY_BYTES
        declared = _declared_length(scope)
        cap = None
        if declared is not None and declared > anon:
            # The caller lookup is a blocking DB read: off the event loop (#495).
            cap = await _cap_off_loop(scope)
            if declared > cap:
                return await _too_large_response(path, cap)(scope, receive, send)

        # Count actual ASGI bytes even when a host forwards an inaccurate declaration.
        state = {"total": 0, "cap": cap, "exceeded": False, "started": False}

        async def limited_receive():
            if state["exceeded"]:
                raise BodyTooLarge()
            message = await receive()
            if message.get("type") == "http.request":
                state["total"] += len(message.get("body") or b"")
                if state["total"] > anon:
                    if state["cap"] is None:
                        state["cap"] = await _cap_off_loop(scope)
                    if state["total"] > state["cap"]:
                        state["exceeded"] = True
                        raise BodyTooLarge()
            return message

        async def guarded_send(message):
            if state["exceeded"]:
                if not state["started"]:
                    # The route caught the aborted read and answered something else (a
                    # 400 from the JSON parser, say). The honest answer is the 413.
                    state["started"] = True
                    response = _too_large_response(path, state["cap"] or anon)
                    await response(scope, receive, send)
                elif not state.get("replaced"):
                    raise BodyTooLarge()
                state["replaced"] = True
                return  # drop the route's own answer
            if message.get("type") == "http.response.start":
                state["started"] = True
            await send(message)

        try:
            await self.app(scope, limited_receive, guarded_send)
        except BodyTooLarge:
            if not state["started"]:
                state["started"] = True
                await _too_large_response(path, state["cap"] or anon)(scope, receive, send)
            elif not state.get("replaced"):
                raise
        except Exception:
            if state["exceeded"] and not state["started"]:
                state["started"] = True
                await _too_large_response(path, state["cap"] or anon)(scope, receive, send)
                return
            raise
