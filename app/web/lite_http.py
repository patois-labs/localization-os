"""A cheaper stand-in for Starlette's BaseHTTPMiddleware (S9 WP-FW).

`@app.middleware("http")` layers cost every request a task group, a memory object stream, a
`_StreamingResponse` relay and, for each `receive()` the handler makes, one more task group.
`LiteHTTPMiddleware` keeps the SAME contract, `dispatch(request, call_next) -> Response`, so the
dispatch function (the security logic) stays byte for byte what it was and only the plumbing
under `call_next` changes:

- The downstream app still runs in a CHILD TASK of the request's task group, started inside
  `call_next` (so it starts with a copy of the context the dispatch has set up, and nothing the
  handler does to a ContextVar can reach the code that runs after `call_next`).
- `call_next` still returns as soon as the downstream app sends `http.response.start`. That
  message is HELD: the child is parked inside its own `send` until the dispatch function has
  returned. The dispatch code that runs between `call_next` and its `return` (the post-handler
  audit row, a replacement 503) therefore runs before one byte of the response leaves, exactly
  as before.
- The response `call_next` returns carries `status_code` and `raw_headers` of the held start
  message. If the dispatch returns THAT object, the child is released and sends the start
  message (built from the object's status and headers, as `_StreamingResponse` did) and every
  later message straight to the server; any other response replaces it: the child's remaining
  messages are dropped and the replacement is sent.
- The request is a `_CachedRequest`: a dispatch that reads the body (the CSRF form token) hands
  the downstream app the cached body, and a dispatch that does not leaves the stream untouched.
  The child's `receive` is that request's `wrapped_receive`, as before.
- An exception the downstream app raises before its response starts is re-raised from
  `call_next`; one raised later is re-raised after the response, as before.

What it drops: the memory stream hop per body chunk (the child sends to the server itself), the
per-`receive` task group and the final empty body message. The bytes on the wire are the same.

`supported()` is False when Starlette no longer has the private pieces this relies on; then
`app.main` falls back to the real `BaseHTTPMiddleware` and nothing changes but the speed.
Pinned by `verify_http_middleware_shape`. Documented in: docs/architecture/identity-and-access.md
"""

from __future__ import annotations

from contextlib import asynccontextmanager

import anyio
from starlette.responses import Response
from starlette.types import ASGIApp, Message, Receive, Scope, Send

try:  # the private Starlette class; supported() reports whether it is still there
    from starlette.middleware.base import _CachedRequest
except Exception:  # pragma: no cover - only on a Starlette without it
    _CachedRequest = None

try:
    BaseExceptionGroup  # noqa: B018 - builtin from Python 3.11
except NameError:  # pragma: no cover - Python 3.10: anyio's own dependency provides it
    from exceptiongroup import BaseExceptionGroup

_DISCONNECT: Message = {"type": "http.disconnect"}
_WAIT, _HELD, _PASS, _DROP = 0, 1, 2, 3


def supported() -> bool:
    """True when the Starlette internals the adapter relies on are present."""
    return _CachedRequest is not None and callable(getattr(_CachedRequest, "wrapped_receive", None))


@asynccontextmanager
async def _collapsing_task_group():
    """A task group that re-raises a lone exception as itself instead of an ExceptionGroup
    (what Starlette's own `create_collapsing_task_group` does; anyio 4 wraps even an exception
    raised in the host body)."""
    try:
        async with anyio.create_task_group() as tg:
            yield tg
    except BaseExceptionGroup as excs:
        if len(excs.exceptions) != 1:
            raise
        exc = excs.exceptions[0]
        context = None if exc.__suppress_context__ else exc.__context__
        raise exc from exc.__cause__ or context


class _Relay(Response):
    """What `call_next` returns: the held start message as a response object. Like
    Starlette's `_StreamingResponse` it has no body of its own (the child streams it)."""

    def __init__(self, status_code: int, raw_headers) -> None:
        self.status_code = status_code
        self.raw_headers = raw_headers
        self.media_type = None
        self.background = None


class LiteHTTPMiddleware:
    def __init__(self, app: ASGIApp, dispatch) -> None:
        self.app = app
        self.dispatch_func = dispatch

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        request = _CachedRequest(scope, receive)
        wrapped_receive = request.wrapped_receive
        started = anyio.Event()  # the child handed over its start message (or ended)
        decided = anyio.Event()  # the dispatch returned: pass the response or drop it
        mode = _WAIT
        start_message: Message | None = None
        debug: list[Message] = []
        final: Response | None = None  # the response the dispatch returned (PASS)
        relay: _Relay | None = None
        app_exc: Exception | None = None
        exception_already_raised = False
        response_sent = False
        called = False

        async def child_receive() -> Message:
            if response_sent:
                return _DISCONNECT
            message = await wrapped_receive()
            if response_sent:
                return _DISCONNECT
            return message

        async def child_send(message: Message) -> None:
            nonlocal mode, start_message
            if mode == _PASS:
                await send(message)
                return
            if mode == _DROP:
                return
            kind = message["type"]
            if kind == "http.response.debug":
                debug.append(message)
                return
            if kind != "http.response.start":
                raise RuntimeError(f"Unexpected message: {message}")
            mode = _HELD
            start_message = message
            started.set()
            await decided.wait()
            if mode != _PASS:
                return
            for held in debug:
                await send(held)
            await send(
                {
                    "type": "http.response.start",
                    "status": final.status_code,
                    "headers": final.raw_headers,
                }
            )

        async def child() -> None:
            nonlocal app_exc
            try:
                await self.app(scope, child_receive, child_send)
            except Exception as exc:
                app_exc = exc
            finally:
                started.set()

        async def call_next(_request) -> Response:
            nonlocal called, relay, exception_already_raised
            if called:
                raise RuntimeError("call_next may only be called once")
            called = True
            tg.start_soon(child)
            await started.wait()
            if start_message is None:
                if app_exc is not None:
                    exception_already_raised = True
                    raise app_exc from app_exc.__cause__ or app_exc.__context__
                raise RuntimeError("No response returned.")
            relay = _Relay(start_message["status"], start_message["headers"])
            return relay

        async with _collapsing_task_group() as tg:
            response = await self.dispatch_func(request, call_next)
            if relay is not None and response is relay:
                final = response
                mode = _PASS
                decided.set()
            else:
                mode = _DROP
                decided.set()
                await response(scope, wrapped_receive, send)
                response_sent = True
        if final is not None and final.background:
            await final.background()
        if app_exc is not None and not exception_already_raised:
            raise app_exc
