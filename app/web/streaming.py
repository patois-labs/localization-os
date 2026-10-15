"""Streaming bodies use their own bounded workers, never sync-route capacity.

Each synchronous producer is driven and finalized by one admitted worker thread.
Session exits never run on the event loop. Cleanup signals that existing worker;
it does not acquire capacity. An in-flight next() finishes before cleanup/release.
Documented in: docs/architecture/configuration.md
"""

import asyncio
import math
from collections.abc import AsyncIterable
from contextlib import asynccontextmanager
from contextvars import ContextVar, copy_context
from functools import partial
from queue import Queue

import anyio
from anyio.lowlevel import RunVar
from starlette.responses import JSONResponse
from starlette.responses import StreamingResponse as _StreamingResponse

from app import config

_limiter = RunVar("loc_stream_limiter")
_end = object()
_lease = ContextVar("loc_stream_lease", default=None)
CAPACITY_MESSAGE = "Stream capacity is busy. Retry shortly; existing work continues."


def retry_after():
    return max(1, math.ceil(config.STREAM_WAIT_S))


def stream_limiter():
    try:
        return _limiter.get()
    except LookupError:
        limiter = anyio.CapacityLimiter(config.STREAM_WORKERS)
        _limiter.set(limiter)
        return limiter


class _Lease:
    def __init__(self, limiter):
        self.limiter = limiter
        self.held = False

    def release(self):
        if self.held:
            self.held = False
            self.limiter.release_on_behalf_of(self)

    async def __aenter__(self):
        # anyio's worker uses the reservation already owned by this request.
        # Validate ownership instead of allocating/charging a second limiter.
        if not self.held:
            raise RuntimeError(CAPACITY_MESSAGE)

    async def __aexit__(self, *args):
        pass  # stream_slot owns release, after the non-abandonable worker returns.


@asynccontextmanager
async def stream_slot():
    """Bound admission only; an admitted thread retains its token until it returns."""
    lease = _Lease(stream_limiter())
    with anyio.move_on_after(config.STREAM_WAIT_S):
        await lease.limiter.acquire_on_behalf_of(lease)
        lease.held = True
    marker = _lease.set(lease if lease.held else None)
    try:
        yield lease.held
    finally:
        lease.release()
        _lease.reset(marker)


async def run_sync(func, *args, **kwargs):
    """Run blocking work using an already admitted stream token, never the default."""
    lease = _lease.get()
    if lease is None or not lease.held:
        async with stream_slot() as admitted:
            if not admitted:
                raise RuntimeError(CAPACITY_MESSAGE)
            return await run_sync(func, *args, **kwargs)
    return await anyio.to_thread.run_sync(partial(func, *args, **kwargs), limiter=lease)


def _next(iterator):
    return next(iterator, _end)


async def _iterate(content):
    iterator = iter(content)
    # At most one requested next() and one stop signal; no chunk buffering.
    commands = Queue(maxsize=2)
    loop = asyncio.get_running_loop()

    def produce():
        try:
            while (command := commands.get()) is not None:
                context, result = command
                try:
                    item = context.run(_next, iterator)
                except BaseException as exc:
                    loop.call_soon_threadsafe(result.set_exception, exc)
                    return
                loop.call_soon_threadsafe(result.set_result, item)
                if item is _end:
                    return
        finally:
            close = getattr(iterator, "close", None)
            if close is not None:
                close()

    worker = asyncio.create_task(run_sync(produce))
    try:
        while True:
            result = loop.create_future()
            commands.put_nowait((copy_context(), result))
            # No producer is abandoned mid-next, including on disconnect.
            with anyio.CancelScope(shield=True):
                done, _ = await asyncio.wait((result, worker), return_when=asyncio.FIRST_COMPLETED)
                if result not in done:
                    await worker  # propagate admission/worker failure
                item = await result
            if item is _end:
                break
            yield item
    finally:
        commands.put_nowait(None)
        with anyio.CancelScope(shield=True):
            await worker


class StreamingResponse(_StreamingResponse):
    def __init__(self, content, *args, **kwargs):
        self._content = content
        super().__init__(
            content if isinstance(content, AsyncIterable) else _iterate(content), *args, **kwargs
        )

    async def __call__(self, scope, receive, send):
        async with stream_slot() as admitted:
            if not admitted:
                response = JSONResponse(
                    {
                        "ok": False,
                        "code": "stream_capacity",
                        "reason": "stream_capacity",
                        "error": CAPACITY_MESSAGE,
                        "retry_after": retry_after(),
                    },
                    status_code=503,
                    headers={"Retry-After": str(retry_after())},
                )
                await response(scope, receive, send)
                close = getattr(self._content, "aclose", None)
                if close is not None:
                    await close()
                else:
                    close = getattr(self._content, "close", None)
                    if close is not None:
                        close()
                return
            await super().__call__(scope, receive, send)

    async def stream_response(self, send):
        try:
            await super().stream_response(send)
        finally:
            close = getattr(self.body_iterator, "aclose", None)
            if close is not None:
                with anyio.CancelScope(shield=True):
                    await close()
