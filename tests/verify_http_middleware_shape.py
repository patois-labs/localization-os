"""The `http` middleware layers are cheap ASGI and keep every guarantee the old ones gave (S9).

`app.main._security_audit` (auth wall, CSRF, RBAC, audit row) and `_feature_gate` used to be
Starlette `BaseHTTPMiddleware` layers: a task group, a memory stream and a response relay each.
They are now `app.web.lite_http.LiteHTTPMiddleware` (same `dispatch(request, call_next)`
contract, same child task and held start message, no stream) and a plain ASGI `_FeatureGate`.
Route matching asks `APIRouter._get_routes_version()` through a per-router memo
(`app.web.route_prefilter`). This suite pins the SHAPE and the behaviour that makes it safe.

GROUPS
  [A] differential: 22 scenarios run through Starlette's own `BaseHTTPMiddleware` and through
      `LiteHTTPMiddleware` with the same dispatch and endpoint; status, headers, body, the
      exception that escapes, the order of events (the post-handler step runs BEFORE the response
      start leaves), context-variable isolation, body replay after the dispatch read the body
      (urlencoded and multipart), streaming, replacement, short circuit, disconnect.
  [B] the stack: no BaseHTTPMiddleware left in `app.user_middleware`; the layers are where they
      were; an unsupported Starlette falls back to the stock layer.
  [C] the real app, auth ON: CSRF still enforced on a session-cookie POST (form field, header,
      multipart, JSON), the handler still receives the full body after the CSRF read, an audit
      row for a mutating request and none for a GET or a refused CSRF, the audit row lands before
      the response starts, fail-closed pre-record, a shed post-handler audit answers 503 and not
      the handler's body, a handler cannot leak a ContextVar into the audit, the feature gate
      answers 404 before auth, an unhandled error still answers 500 with the security headers.
  [D] route matching: the version memo follows routes added after startup (route, included
      router, route added to an already included router, router included in an included router),
      and `install()` refuses when `_mark_routes_changed` is missing.

Offline, hermetic (throwaway LOC_TMS_DATA_DIR). Final line: HTTP MIDDLEWARE SHAPE OK. ASCII only.
"""

import asyncio
import contextvars
import json
import os
import sys
from unittest.mock import patch

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(_ROOT)
sys.path.insert(0, _ROOT)
from tests._bootstrap import init_env

init_env("loctms_httpmw_")

from fastapi import APIRouter, Request
from fastapi import routing as fr
from fastapi.testclient import TestClient
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse, PlainTextResponse, Response, StreamingResponse
from starlette.routing import Route, WebSocketRoute

from app import db as _db
from app import main as app_main
from app.db import init_db
from app.main import app
from app.models import Role
from app.services import audit, security, settings
from app.web import lite_http, route_prefilter
from tests._csrf import csrf_value

init_db()


def _fail(msg):
    raise AssertionError(msg)


# --------------------------------------------------------------------------- [A] differential
VAR = contextvars.ContextVar("s9_probe_var", default="default")


def _scope(method, path, headers):
    return {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": method,
        "path": path,
        "raw_path": path.encode(),
        "root_path": "",
        "query_string": b"",
        "headers": [(k.encode(), v.encode()) for k, v in headers],
        "client": ("127.0.0.1", 5000),
        "server": ("testserver", 80),
        "scheme": "http",
    }


async def _drive(asgi, method="GET", path="/", chunks=(b"",), headers=(), events=None, hang=3.0):
    """One request through `asgi` the way a server does: the body chunks, then (once the
    response is complete, or when `disconnect_after` says so) http.disconnect. Returns a summary
    that does not depend on how the body was framed into messages."""
    pending = [
        {"type": "http.request", "body": c, "more_body": i < len(chunks) - 1}
        for i, c in enumerate(chunks)
    ]
    done = asyncio.Event()
    sent = []
    outcome = {"exc": None}

    async def receive():
        if pending:
            return pending.pop(0)
        await done.wait()
        return {"type": "http.disconnect"}

    async def send(message):
        sent.append(message)
        if events is not None and message["type"] == "http.response.start":
            events.append("start")
        if message["type"] == "http.response.body" and not message.get("more_body"):
            done.set()

    try:
        await asyncio.wait_for(asgi(_scope(method, path, headers), receive, send), hang)
    except TimeoutError:
        outcome["exc"] = "TIMEOUT"
    except BaseException as exc:
        outcome["exc"] = f"{type(exc).__name__}: {exc}"
    starts = [m for m in sent if m["type"] == "http.response.start"]
    body = b"".join(m.get("body", b"") for m in sent if m["type"] == "http.response.body")
    return {
        "status": starts[0]["status"] if starts else None,
        "starts": len(starts),
        "headers": sorted((k.lower(), v) for k, v in starts[0]["headers"]) if starts else None,
        "body": body,
        "exc": outcome["exc"],
        "events": list(events) if events is not None else None,
    }


def _build(mw_cls, dispatch, endpoint, **route_kw):
    return Starlette(
        routes=[
            Route("/", endpoint, methods=["GET", "POST"]),
            Route("/s", endpoint, methods=["GET", "POST"]),
        ],
        middleware=[Middleware(mw_cls, dispatch=dispatch)],
    )


async def _text(request):
    return PlainTextResponse("hello")


def _scenarios():
    """(name, dispatch factory(events), endpoint factory(events), drive kwargs)."""
    out = []

    def add(name, dispatch, endpoint, **kw):
        out.append((name, dispatch, endpoint, kw))

    # 1 plain pass-through
    async def d_pass(events):
        async def d(request, call_next):
            return await call_next(request)

        return d

    add("pass-through", d_pass, lambda ev: _text)

    # 2 the dispatch sees the status, then returns the same response
    async def d_observe(events):
        async def d(request, call_next):
            events.append("pre")
            response = await call_next(request)
            events.append(f"post:{response.status_code}")
            return response

        return d

    add("observe-status-then-pass", d_observe, lambda ev: _text)

    # 3 replacement after the handler ran
    async def d_replace(events):
        async def d(request, call_next):
            await call_next(request)
            events.append("replacing")
            return PlainTextResponse("replaced", status_code=503)

        return d

    def ep_marker(events):
        async def ep(request):
            events.append("handler")
            return PlainTextResponse("SECRET-HANDLER-BODY")

        return ep

    add("replace-after-handler", d_replace, ep_marker)

    # 4 header mutation on the relayed response
    async def d_header(events):
        async def d(request, call_next):
            response = await call_next(request)
            response.headers["x-added"] = "1"
            return response

        return d

    add("mutate-headers", d_header, lambda ev: _text)

    # 4b status mutation on the relayed response (gate round 1: the start message must be
    # built from the returned object, not the held one)
    async def d_status(events):
        async def d(request, call_next):
            response = await call_next(request)
            response.status_code = 202
            return response

        return d

    add("mutate-status", d_status, lambda ev: _text)

    # 5 short circuit: never calls call_next
    async def d_short(events):
        async def d(request, call_next):
            events.append("short")
            return PlainTextResponse("blocked", status_code=404)

        return d

    add("short-circuit", d_short, ep_marker)

    # 6 the endpoint raises before it answers
    def ep_raise(events):
        async def ep(request):
            raise ValueError("boom-before-start")

        return ep

    add("endpoint-raises", d_pass, ep_raise)

    # 7 streaming body
    def ep_stream(events):
        async def gen():
            for i in range(5):
                yield f"chunk{i};".encode()

        async def ep(request):
            return StreamingResponse(gen(), media_type="text/plain")

        return ep

    add("streaming", d_pass, ep_stream)

    # 8 the stream raises after the response started
    def ep_stream_raises(events):
        async def gen():
            yield b"first;"
            raise ValueError("boom-mid-stream")

        async def ep(request):
            return StreamingResponse(gen(), media_type="text/plain")

        return ep

    add("stream-raises-midway", d_pass, ep_stream_raises)

    # 9 the dispatch reads the body (as the CSRF check does); the endpoint must still get it
    async def d_read_body(events):
        async def d(request, call_next):
            raw = await request.body()
            events.append(f"dispatch-read:{len(raw)}")
            return await call_next(request)

        return d

    def ep_body(events):
        async def ep(request):
            raw = await request.body()
            return PlainTextResponse(raw.decode("latin-1"))

        return ep

    add(
        "dispatch-reads-body",
        d_read_body,
        ep_body,
        method="POST",
        chunks=(b"alpha=1&", b"beta=%C3%A9&", b"gamma=3"),
        headers=(("content-type", "application/x-www-form-urlencoded"),),
    )

    # 9b urlencoded: dispatch parses the token itself, endpoint uses request.form()
    async def d_read_token(events):
        async def d(request, call_next):
            from urllib.parse import parse_qs

            raw = await request.body()
            events.append("token:" + parse_qs(raw.decode()).get("csrf_token", [""])[0])
            return await call_next(request)

        return d

    def ep_form(events):
        async def ep(request):
            form = await request.form()
            return JSONResponse({k: form[k] for k in sorted(form)})

        return ep

    add(
        "urlencoded-form-after-token-read",
        d_read_token,
        ep_form,
        method="POST",
        chunks=(b"csrf_token=T0K&target_text=caf%C3%A9+au+lait&confirm=1",),
        headers=(("content-type", "application/x-www-form-urlencoded"),),
    )

    # 9c multipart: dispatch calls body() then form(); endpoint parses the same form again
    async def d_multipart(events):
        async def d(request, call_next):
            await request.body()
            form = await request.form()
            events.append("csrf:" + str(form.get("csrf_token")))
            return await call_next(request)

        return d

    boundary = "XBOUNDARYX"
    mp = (
        f'--{boundary}\r\nContent-Disposition: form-data; name="csrf_token"\r\n\r\nT0K\r\n'
        f'--{boundary}\r\nContent-Disposition: form-data; name="f"; filename="a.txt"\r\n'
        f"Content-Type: text/plain\r\n\r\nFILE-CONTENT\r\n--{boundary}--\r\n"
    ).encode()

    def ep_mp(events):
        async def ep(request):
            form = await request.form()
            f = form["f"]
            return JSONResponse({"t": form["csrf_token"], "f": (await f.read()).decode()})

        return ep

    add(
        "multipart-after-form-read",
        d_multipart,
        ep_mp,
        method="POST",
        chunks=(mp[:40], mp[40:]),
        headers=(("content-type", f"multipart/form-data; boundary={boundary}"),),
    )

    # 10 dispatch does not read the body: the endpoint streams it chunk by chunk
    def ep_stream_body(events):
        async def ep(request):
            parts = [c async for c in request.stream()]
            return PlainTextResponse("|".join(p.decode() for p in parts))

        return ep

    add(
        "endpoint-streams-body",
        d_pass,
        ep_stream_body,
        method="POST",
        chunks=(b"one", b"two", b"three"),
    )

    # 11 the dispatch raises before call_next
    async def d_raise_before(events):
        async def d(request, call_next):
            raise RuntimeError("dispatch-before")

        return d

    add("dispatch-raises-before", d_raise_before, ep_marker)

    # 12 the dispatch raises after call_next (an audit failure): the handler's body is not sent
    async def d_raise_after(events):
        async def d(request, call_next):
            await call_next(request)
            raise RuntimeError("dispatch-after")

        return d

    add("dispatch-raises-after", d_raise_after, ep_marker)

    # 13 context variables: set before call_next -> handler sees it; handler's own set must not
    # reach the code after call_next
    async def d_ctx(events):
        async def d(request, call_next):
            token = VAR.set("from-dispatch")
            try:
                response = await call_next(request)
            finally:
                VAR.reset(token)
            events.append("after:" + VAR.get())
            return response

        return d

    def ep_ctx(events):
        async def ep(request):
            events.append("handler-sees:" + VAR.get())
            VAR.set("leaked-by-handler")
            return PlainTextResponse("ok")

        return ep

    add("contextvar-isolation", d_ctx, ep_ctx)

    # 14 the post-handler step runs before the response start leaves (events carry 'start')
    async def d_order(events):
        async def d(request, call_next):
            response = await call_next(request)
            await asyncio.sleep(0.02)
            events.append("post")
            return response

        return d

    add("post-step-before-start", d_order, lambda ev: _text)

    # 15 the endpoint streams until the client goes away; the client leaves after the first chunk
    def ep_forever(events):
        async def gen():
            i = 0
            while True:
                yield f"tick{i};".encode()
                i += 1
                await asyncio.sleep(0.01)

        async def ep(request):
            return StreamingResponse(gen(), media_type="text/plain")

        return ep

    add("client-disconnect-ends-stream", d_pass, ep_forever, disconnect_after_start=True)

    # 16 a replacement while the handler streams forever: it must wind down
    async def d_replace_stream(events):
        async def d(request, call_next):
            await call_next(request)
            return PlainTextResponse("replaced", status_code=503)

        return d

    add("replace-endless-stream", d_replace_stream, ep_forever)

    # 17 HEAD-like empty body and a 204
    def ep_204(events):
        async def ep(request):
            return Response(status_code=204)

        return ep

    add("no-content", d_pass, ep_204)

    # 18 the endpoint answers before it reads the body
    def ep_ignores_body(events):
        async def ep(request):
            return PlainTextResponse("ignored")

        return ep

    add(
        "endpoint-ignores-body",
        d_read_body,
        ep_ignores_body,
        method="POST",
        chunks=(b"x" * 1000,),
    )

    # 19 two-step: observe status of an error response and keep it
    def ep_500(events):
        async def ep(request):
            return PlainTextResponse("handled-error", status_code=500)

        return ep

    add("handled-500", d_observe, ep_500)
    return out


async def _run_scenario(mw_cls, name, dispatch_f, endpoint_f, kw):
    events: list = []
    dispatch = await dispatch_f(events)
    asgi = _build(mw_cls, dispatch, endpoint_f(events))
    kw = dict(kw)
    disconnect_after_start = kw.pop("disconnect_after_start", False)
    if disconnect_after_start:
        return await _drive_disconnect(asgi, events)
    return await _drive(asgi, events=events, **kw)


async def _drive_disconnect(asgi, events):
    """Client reads the first body chunk, then hangs up."""
    got_chunk = asyncio.Event()
    sent = []

    async def receive():
        await got_chunk.wait()
        return {"type": "http.disconnect"}

    async def send(message):
        sent.append(message)
        if message["type"] == "http.response.body" and message.get("body"):
            got_chunk.set()

    exc = None
    try:
        await asyncio.wait_for(asgi(_scope("GET", "/", ()), receive, send), 3.0)
    except TimeoutError:
        exc = "TIMEOUT"
    except BaseException as e:
        exc = f"{type(e).__name__}: {e}"
    chunks = [m for m in sent if m["type"] == "http.response.body" and m.get("body")]
    return {
        "status": sent[0].get("status") if sent else None,
        "got_chunk": bool(chunks),
        "exc": exc,
    }


def group_a():
    n = 0
    for name, dispatch_f, endpoint_f, kw in _scenarios():
        base = asyncio.run(_run_scenario(BaseHTTPMiddleware, name, dispatch_f, endpoint_f, kw))
        lite = asyncio.run(
            _run_scenario(lite_http.LiteHTTPMiddleware, name, dispatch_f, endpoint_f, kw)
        )
        if base != lite:
            _fail(f"[A] {name}: BaseHTTPMiddleware {base!r} != LiteHTTPMiddleware {lite!r}")
        if base.get("exc") == "TIMEOUT":
            _fail(f"[A] {name}: timed out under both layers (scenario is broken): {base!r}")
        n += 1
    print(f"  [A] {n} scenarios identical under BaseHTTPMiddleware and LiteHTTPMiddleware OK")

    # spot checks that the scenarios really exercise what their names say
    ev = asyncio.run(
        _run_scenario(
            lite_http.LiteHTTPMiddleware,
            "x",
            *next(s for s in _scenarios() if s[0] == "post-step-before-start")[1:3],
            {},
        )
    )
    if ev["events"] != ["post", "start"]:
        _fail(f"[A] the post-handler step must run before the response start: {ev['events']}")
    rep = asyncio.run(
        _run_scenario(
            lite_http.LiteHTTPMiddleware,
            "x",
            *next(s for s in _scenarios() if s[0] == "replace-after-handler")[1:3],
            {},
        )
    )
    if rep["body"] != b"replaced" or rep["status"] != 503 or rep["starts"] != 1:
        _fail(f"[A] replacement must be the only response: {rep!r}")
    if b"SECRET-HANDLER-BODY" in rep["body"] or "handler" not in rep["events"]:
        _fail(f"[A] the handler runs, its body never leaves: {rep!r}")
    ctx = asyncio.run(
        _run_scenario(
            lite_http.LiteHTTPMiddleware,
            "x",
            *next(s for s in _scenarios() if s[0] == "contextvar-isolation")[1:3],
            {},
        )
    )
    if ctx["events"] != ["handler-sees:from-dispatch", "after:default", "start"]:
        _fail(f"[A] context isolation: {ctx['events']}")
    print("  [A] post-step-before-start, replacement-only and context isolation spot checks OK")

    # websocket and lifespan scopes pass through untouched
    seen = []

    async def ws_endpoint(websocket):
        await websocket.accept()
        await websocket.send_text("hi")
        await websocket.close()

    async def d(request, call_next):
        seen.append("http-dispatch")
        return await call_next(request)

    a = Starlette(
        routes=[WebSocketRoute("/ws", ws_endpoint), Route("/", _text)],
        middleware=[Middleware(lite_http.LiteHTTPMiddleware, dispatch=d)],
    )
    with TestClient(a) as c:
        with c.websocket_connect("/ws") as ws:
            if ws.receive_text() != "hi":
                _fail("[A] websocket through the lite layer")
        if c.get("/").text != "hello":
            _fail("[A] http through the lite layer after lifespan")
    if seen != ["http-dispatch"]:
        _fail(f"[A] dispatch must run for http only: {seen}")
    print("  [A] websocket and lifespan scopes pass through OK")


# --------------------------------------------------------------------------- [B] stack shape
def group_b():
    classes = [m.cls for m in app.user_middleware]
    if lite_http.supported():
        if any(c is BaseHTTPMiddleware for c in classes):
            _fail("[B] a BaseHTTPMiddleware layer is still in the stack")
        lite = [m for m in app.user_middleware if m.cls is lite_http.LiteHTTPMiddleware]
        if len(lite) != 1 or lite[0].kwargs.get("dispatch") is not app_main._security_audit:
            _fail("[B] expected exactly one LiteHTTPMiddleware wrapping _security_audit")
    names = [c.__name__ for c in classes]
    # app.user_middleware lists the OUTERMOST first: capacity (last added) ... background (first).
    order = [
        "_CapacityMiddleware",
        "AIScopeMiddleware",
        "SessionMiddleware",
        "BodyLimitMiddleware",
        "PageGuardMiddleware",
        "_FeatureGate",
        "LiteHTTPMiddleware",
        "BackgroundCallMiddleware",
    ]
    ours = [n for n in names if n in order]
    if ours != order:
        _fail(f"[B] middleware order changed: {ours}")
    print(f"  [B] stack (outermost first): {' > '.join(order)} OK")

    # an unsupported Starlette falls back to the stock layer, in the same position
    scratch = app.__class__()
    with (
        patch.object(app_main, "app", scratch),
        patch.object(lite_http, "_CachedRequest", None),
    ):
        if lite_http.supported():
            _fail("[B] supported() must be False without _CachedRequest")
        app_main._add_http_middleware(app_main._security_audit)
    if [m.cls for m in scratch.user_middleware] != [BaseHTTPMiddleware]:
        _fail("[B] fallback must register the stock BaseHTTPMiddleware")
    print("  [B] unsupported Starlette falls back to BaseHTTPMiddleware OK")


# --------------------------------------------------------------------------- [C] the real app
PW = "httpmw-pw-123456-aA"
CALLS = {"n": 0}
SEEN = {}


async def _probe_form(request: Request):
    CALLS["n"] += 1
    SEEN["recent_action"] = audit.recent()[0].action if audit.recent() else None
    SEEN["recorded"] = audit.REQUEST_RECORDED.get()
    ctype = request.headers.get("content-type", "")
    if ctype.startswith("application/json"):
        return JSONResponse({"json": await request.json()})
    form = await request.form()
    out = {}
    for k in sorted(form):
        v = form[k]
        out[k] = (await v.read()).decode() if hasattr(v, "read") else v
    return JSONResponse(out)


async def _probe_get(request: Request):
    CALLS["n"] += 1
    return PlainTextResponse("got")


async def _probe_leak(request: Request):
    CALLS["n"] += 1
    _db.exempt_pool_budget()
    audit.REQUEST_RECORDED.set(True)
    from app.services import settings as s

    s.REQUEST_DOOR.set(False)
    return PlainTextResponse("leaky")


async def _probe_500(request: Request):
    CALLS["n"] += 1
    return PlainTextResponse("handled-error", status_code=500)


async def _probe_boom(request: Request):
    CALLS["n"] += 1
    raise RuntimeError("unhandled boom")


async def _probe_stream(request: Request):
    async def gen():
        for i in range(5):
            yield f"part{i};".encode()

    return StreamingResponse(gen(), media_type="text/plain")


def _audit_count():
    return len(audit.recent(1000))


def group_c():
    app.add_api_route("/_s9/form", _probe_form, methods=["POST"])
    app.add_api_route("/_s9/get", _probe_get, methods=["GET"])
    app.add_api_route("/_s9/leak", _probe_leak, methods=["POST"])
    app.add_api_route("/_s9/e500", _probe_500, methods=["POST"])
    app.add_api_route("/_s9/boom", _probe_boom, methods=["POST"])
    app.add_api_route("/_s9/stream", _probe_stream, methods=["GET"])

    security.create_user("httpmw@x.com", "Admin", Role.ADMIN, PW)

    # ---- auth OFF first: the audit row lands before the response starts
    settings.set_many({"security_auth_required": "0"})
    settings.invalidate_cache()
    events = []
    real_log = audit.log

    def logging_log(*a, **kw):
        events.append("audit")
        return real_log(*a, **kw)

    with patch.object(audit, "log", logging_log):
        r = asyncio.run(
            _drive(
                app,
                method="POST",
                path="/_s9/form",
                chunks=(b"target_text=hola&confirm=1",),
                headers=(
                    ("host", "testserver"),
                    ("content-type", "application/x-www-form-urlencoded"),
                ),
                events=events,
            )
        )
    if r["status"] != 200 or json.loads(r["body"]) != {"target_text": "hola", "confirm": "1"}:
        _fail(f"[C] auth-off form POST: {r!r}")
    if events != ["audit", "start"]:
        _fail(f"[C] the audit row must be written before the response starts: {events}")
    print(
        "  [C] auth off: handler gets the form; audit row is written before the response start OK"
    )

    # ---- auth ON, signed in
    settings.set_many({"security_auth_required": "1"})
    settings.invalidate_cache()
    client = TestClient(app, raise_server_exceptions=False)
    r = client.post(
        "/auth/login",
        data={
            "email": "httpmw@x.com",
            "password": PW,
            "next": "/",
            "csrf_token": csrf_value(client),
        },
        follow_redirects=False,
    )
    if r.status_code != 303:
        _fail(f"[C] sign-in failed: {r.status_code}")
    tok = csrf_value(client)
    if not tok:
        _fail("[C] no session csrf token")

    # urlencoded form with the token in the body: the handler still gets EVERY field
    before, calls = _audit_count(), CALLS["n"]
    body = {"csrf_token": tok, "target_text": "café über 100% & more=1", "confirm": "1"}
    r = client.post("/_s9/form", data=body)
    if r.status_code != 200 or r.json() != body:
        _fail(f"[C] urlencoded POST: {r.status_code} {r.text}")
    if CALLS["n"] != calls + 1 or _audit_count() != before + 1:
        _fail("[C] a mutating request is audited exactly once")
    if audit.recent()[0].action != "POST /_s9/form":
        _fail(f"[C] audit action: {audit.recent()[0].action}")
    print("  [C] urlencoded form: CSRF token read from the body, handler sees the full body OK")

    # multipart with a file
    before, calls = _audit_count(), CALLS["n"]
    r = client.post(
        "/_s9/form",
        data={"csrf_token": tok, "note": "n1"},
        files={"upload": ("a.txt", b"FILE-BYTES-" + b"z" * 5000, "text/plain")},
    )
    if r.status_code != 200 or r.json() != {
        "csrf_token": tok,
        "note": "n1",
        "upload": "FILE-BYTES-" + "z" * 5000,
    }:
        _fail(f"[C] multipart POST: {r.status_code} {r.text[:200]}")
    print("  [C] multipart form with a file: handler sees field and file after the CSRF read OK")

    # header token + JSON body
    r = client.post("/_s9/form", json={"a": [1, 2, 3]}, headers={"X-CSRF-Token": tok})
    if r.status_code != 200 or r.json() != {"json": {"a": [1, 2, 3]}}:
        _fail(f"[C] header-token JSON POST: {r.status_code} {r.text}")

    # refused: no token (form post -> sign-in), wrong header, cross-origin: no handler, no row
    before, calls = _audit_count(), CALLS["n"]
    r = client.post("/_s9/form", data={"target_text": "x"}, follow_redirects=False)
    if (
        r.status_code != 303
        or "/auth/login" not in r.headers["location"]
        or "expired=1" not in r.headers["location"]
    ):
        _fail(f"[C] missing token: {r.status_code} {r.headers.get('location')}")
    r = client.post(
        "/_s9/form",
        data={"target_text": "x"},
        headers={"X-CSRF-Token": "wrong"},
        follow_redirects=False,
    )
    if r.status_code != 403:
        _fail(f"[C] wrong header token: {r.status_code}")
    r = client.post(
        "/_s9/form",
        data={"csrf_token": tok},
        headers={"Origin": "http://evil.example"},
        follow_redirects=False,
    )
    if r.status_code != 403:
        _fail(f"[C] cross-origin: {r.status_code}")
    if CALLS["n"] != calls or _audit_count() != before:
        _fail("[C] a refused request must not reach the handler or write a row")
    print(
        "  [C] CSRF refusals: 303 to sign-in, 403 header, 403 cross-origin; no handler, no row OK"
    )

    # a GET is not audited
    before = _audit_count()
    r = client.get("/_s9/get")
    if r.status_code != 200 or r.text != "got" or _audit_count() != before:
        _fail(f"[C] GET must pass unaudited: {r.status_code} {_audit_count()} vs {before}")

    # unauthenticated /api POST: 401 and an attributed refusal row with the status
    anon = TestClient(app, raise_server_exceptions=False)
    before = _audit_count()
    r = anon.post("/api/projects", json={})
    row = audit.recent()[0]
    if r.status_code != 401 or _audit_count() != before + 1:
        _fail(f"[C] anonymous /api POST: {r.status_code}, rows {before}->{_audit_count()}")
    if not row.actor.startswith("unauthenticated") or "refused 401" not in (row.detail or ""):
        _fail(f"[C] refusal row: {row.actor!r} {row.detail!r}")
    print(
        "  [C] audit rows: one per mutating request, none for GET or a CSRF refusal, refused 401 OK"
    )

    # fail-closed: the row is written BEFORE the handler runs
    settings.set_many({"audit_fail_open": "0"})
    settings.invalidate_cache()
    SEEN.clear()
    r = client.post("/_s9/form", data={"csrf_token": tok, "k": "v"})
    if (
        r.status_code != 200
        or SEEN.get("recent_action") != "POST /_s9/form"
        or SEEN.get("recorded") is not True
    ):
        _fail(f"[C] fail-closed pre-record: {r.status_code} {SEEN}")
    settings.set_many({"audit_fail_open": "1"})
    settings.invalidate_cache()
    SEEN.clear()
    audit.log(actor="marker", action="marker-row")
    r = client.post("/_s9/form", data={"csrf_token": tok, "k": "v"})
    if SEEN.get("recent_action") != "marker-row" or SEEN.get("recorded") is not False:
        _fail(f"[C] fail-open writes after the handler: {SEEN}")
    if audit.recent()[0].action != "POST /_s9/form":
        _fail("[C] fail-open row missing after the handler")
    print("  [C] fail-closed pre-records before the handler; fail-open records after it OK")

    # a shed post-handler audit answers 503 and not the handler's own body
    async def shed(request, status=None, *, prerecord=False):
        if prerecord:
            return False
        raise _db.WriteContentionError()

    calls = CALLS["n"]
    with patch.object(app_main, "_audit_off_loop", shed):
        r = client.post("/_s9/form", data={"csrf_token": tok, "secret": "S3CR3T"})
    if r.status_code != 503 or "S3CR3T" in r.text or CALLS["n"] != calls + 1:
        _fail(f"[C] shed audit: {r.status_code} {r.text[:200]}")

    # a handled 5xx stays the handler's answer even when its audit write also fails
    async def broken(request, status=None, *, prerecord=False):
        if prerecord:
            return False
        raise RuntimeError("audit storage down")

    with patch.object(app_main, "_audit_off_loop", broken):
        r = client.post("/_s9/e500", data={"csrf_token": tok})
    if r.status_code != 500 or "handled-error" not in r.text:
        _fail(f"[C] handled 5xx must stay the handler's page: {r.status_code} {r.text[:120]}")
    print(
        "  [C] shed audit -> 503 (handler body withheld); handled 5xx survives an audit failure OK"
    )

    # a handler cannot leak a ContextVar into the audit write that follows it
    seen = {}
    real_log = audit.log

    def spying_log(*a, **kw):
        seen["budget"] = _db._pool_budget.get() is not None
        seen["recorded"] = audit.REQUEST_RECORDED.get()
        seen["door"] = settings.REQUEST_DOOR.get()
        return real_log(*a, **kw)

    with patch.object(audit, "log", spying_log):
        r = client.post("/_s9/leak", data={"csrf_token": tok})
    if r.status_code != 200 or seen != {"budget": True, "recorded": False, "door": False}:
        _fail(f"[C] ContextVar isolation around the audit write: {r.status_code} {seen}")
    print("  [C] a handler's ContextVar changes do not reach the audit write OK")

    # unhandled error: 500 with the security headers, no audit row, no hang
    before = _audit_count()
    r = client.post("/_s9/boom", data={"csrf_token": tok})
    if r.status_code != 500 or "x-content-type-options" not in {k.lower() for k in r.headers}:
        _fail(f"[C] unhandled error: {r.status_code} {dict(r.headers)}")
    if _audit_count() != before:
        _fail("[C] an unhandled error must not write the post-handler row")

    # streaming through the whole stack
    r = client.get("/_s9/stream")
    if r.status_code != 200 or r.text != "part0;part1;part2;part3;part4;":
        _fail(f"[C] streaming: {r.status_code} {r.text}")
    if "x-content-type-options" not in {k.lower() for k in r.headers}:
        _fail("[C] security headers missing on a streamed response")
    print("  [C] unhandled error -> 500 + headers + no row; streamed body intact OK")

    # feature gate: 404 BEFORE auth, and a signed-in mutating probe leaves a trail
    settings.set_many({"routing_enabled": "0"})
    settings.invalidate_cache()
    r = anon.get("/routing", follow_redirects=False)
    if r.status_code != 404:
        _fail(f"[C] feature gate must answer 404 before auth: {r.status_code}")
    before = _audit_count()
    r = anon.post("/routing/x", data={}, follow_redirects=False)
    if r.status_code != 404 or _audit_count() != before:
        _fail(f"[C] anonymous gated probe: {r.status_code}, rows {before}->{_audit_count()}")
    r = client.post("/routing/x", data={"csrf_token": tok}, follow_redirects=False)
    if r.status_code != 404 or audit.recent()[0].action != "POST /routing/x":
        _fail(f"[C] signed-in probe of a gated feature: {r.status_code} {audit.recent()[0].action}")
    settings.set_many({"routing_enabled": "1"})
    settings.invalidate_cache()
    print("  [C] feature gate: 404 before auth; signed-in probe audited, anonymous one not OK")

    # liveness paths never need the database and still answer
    r = anon.get("/health")
    if r.status_code != 200:
        _fail(f"[C] /health: {r.status_code}")
    settings.set_many({"security_auth_required": "0"})
    settings.invalidate_cache()


# --------------------------------------------------------------------------- [D] route matching
def _answer(client, path):
    r = client.get(path)
    return r.status_code, r.text


def group_d():
    client = TestClient(app, raise_server_exceptions=False)
    settings.set_many({"security_auth_required": "0"})
    settings.invalidate_cache()

    async def ok(request):
        return PlainTextResponse("dyn-ok")

    # 1 a route added to the app after startup
    app.add_api_route("/api/_s9dyn/one", ok, methods=["GET"])
    # 2 a router included after startup
    inc = APIRouter(prefix="/api/_s9inc")
    inc.add_api_route("/first", ok, methods=["GET"])
    app.include_router(inc)
    # 3 a route added to the ALREADY included router
    inc.add_api_route("/late", ok, methods=["GET"])
    # 4 a router included into the already included router
    child = APIRouter(prefix="/child")
    child.add_api_route("/leaf", ok, methods=["GET"])
    inc.include_router(child)
    # 5 a route added to that nested router afterwards
    child.add_api_route("/later", ok, methods=["GET"])
    # 6 a route with a NEW first-two-segments head added to a router nested two levels down,
    # after everything above it was included and matched once (the ancestors' cached heads
    # and memoized versions must not hide it)
    top = APIRouter()
    top.add_api_route("/api/_s9nest/a", ok, methods=["GET"])
    app.include_router(top)
    mid = APIRouter()
    top.include_router(mid)
    leaf = APIRouter()
    mid.include_router(leaf)
    if client.get("/api/_s9nest/a").status_code == 404:
        _fail("[D] nested setup route not found")
    if client.get("/api/_s9other/b").status_code != 404:
        _fail("[D] unexpected route before it exists")
    leaf.add_api_route("/api/_s9other/b", ok, methods=["GET"])
    paths = [
        "/api/_s9other/b",
        "/api/_s9dyn/one",
        "/api/_s9inc/first",
        "/api/_s9inc/late",
        "/api/_s9inc/child/leaf",
        "/api/_s9inc/child/later",
    ]
    for p in paths:
        # an unauthenticated /api GET answers 401 when it matches a route, 404 when it does not
        status = client.get(p).status_code
        if status == 404:
            _fail(f"[D] route added at run time is not found with the memo installed: {p}")
    with_memo = {p: client.get(p).status_code for p in [*paths, "/api/_s9inc/nope"]}
    route_prefilter.uninstall()
    try:
        without = {p: client.get(p).status_code for p in [*paths, "/api/_s9inc/nope"]}
    finally:
        route_prefilter.install()
    if with_memo != without:
        _fail(f"[D] answers differ with and without the memo: {with_memo} vs {without}")
    print(f"  [D] {len(paths)} routes added after startup (incl. nested, new head) found OK")

    # the memo really is used and really is dropped by a change
    fr_router = fr.APIRouter
    probe = fr_router()
    v1 = probe._get_routes_version()
    cached = probe.__dict__.get(route_prefilter._VERSION_ATTR)
    if cached is None or cached[2] != v1:
        _fail("[D] the routes version should be memoized on the router")
    probe.add_api_route("/x", ok, methods=["GET"])
    v2 = probe._get_routes_version()
    if v2 == v1:
        _fail("[D] adding a route must change the memoized version")
    outer = fr_router()
    inner = fr_router()
    outer.include_router(inner)
    base_version = outer._get_routes_version()
    inner.add_api_route("/y", ok, methods=["GET"])
    if outer._get_routes_version() == base_version:
        _fail("[D] a route added to an included router must change the outer version")
    print("  [D] the memo follows add_api_route / include_router at any depth OK")

    # install() refuses (and patches nothing) when the invalidation hook is missing
    route_prefilter.uninstall()
    saved = fr_router._mark_routes_changed
    try:
        del fr_router._mark_routes_changed
        if route_prefilter.install() is not False:
            _fail("[D] install() must refuse without _mark_routes_changed")
        if getattr(fr_router._get_routes_version, "_locos_routes_version_cache", False):
            _fail("[D] a refused install() must patch nothing")
    finally:
        fr_router._mark_routes_changed = saved
    if not route_prefilter.install():
        _fail("[D] install() should work again on the pinned FastAPI")
    print("  [D] unknown FastAPI internals: install() refuses and patches nothing OK")


def main() -> None:
    group_a()
    group_b()
    group_c()
    group_d()
    print("HTTP MIDDLEWARE SHAPE OK")


if __name__ == "__main__":
    main()
