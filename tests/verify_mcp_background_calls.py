"""Background calls, idempotency keys and launch-then-poll MCP tools (#728, #743-#745, #747).

Offline, no provider, no spend. Server half on the real app (TestClient, connector work
replaced by slow fakes); MCP half against an httpx.MockTransport fake install AND against the
real app through httpx.ASGITransport.

  A. A billing route with no headers is untouched (synchronous 200).
  B. `Prefer: respond-async` answers 202 + job id while the route still runs; the job reads
     `running`, an identical background call gets the SAME job (no second run), and the
     finished job carries {http_status, response}. A route failure is a failed job.
  C. `Idempotency-Key`: a repeat replays the first answer without running; the same key on
     a different body is 422; a malformed key is 400; a synchronous repeat while the first
     runs is 409 idempotency_in_progress; 429/5xx answers are not kept; keys are per caller.
  D. Bookkeeping: the api_task row names no project/document (reports never count it), a
     foreign user cannot read it, no caller identity means the route answers 401 as before,
     and a restart fails a running row and frees its key.
  E. Every registry.SPEND_ROUTES path is covered by the middleware; every spend tool takes
     idempotency_key.
  F. MCP _call: a read timeout on a write says the work may still be running (never "is the
     app running"); a connect error still says unreachable; a GET timeout says nothing changed.
  G. MCP connector_sync uses the route's async mode and returns the finished summary; a long
     run returns {status: running, job_id, next}; job_status returns the finished job's
     result with lists capped; a background 404 is the same ToolError the sync call raised;
     idempotency_key rides on confirmed spend calls only, and a malformed one is refused.
  H. End to end over ASGI: connector_push and connector_sync through the MCP tools against
     the real app return the real summaries.
  I. Remote CLI: RemoteClient.post_background (connector pull/push) polls to the route's
     answer, keeps the {"ok": false} envelope, and --idempotency-key replays / is validated.

Final line: "MCP BACKGROUND CALLS OK".
"""

import asyncio
import contextlib
import json
import os
import sys
import threading
import time

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(_ROOT)
sys.path.insert(0, _ROOT)
from tests._bootstrap import init_env

init_env("loctms_mcpbg_")
sys.path.insert(0, os.path.join(_ROOT, "integrations", "mcp-server"))

import httpx
import longrun
import registry
import server as mcp_server
from fastapi.testclient import TestClient
from sqlmodel import Session, select

from app.db import engine, init_db
from app.main import app
from app.models import Connection, Job, JobStatus
from app.services import apikeys, security
from app.services.connectors import store as conn_store
from app.web import background_calls as bg
from tests._mcp_plan import with_plan

CHECKS = []


def ok(msg):
    CHECKS.append(msg)
    print("  ok:", msg)


def _fail(msg):
    print("FAIL:", msg)
    sys.exit(1)


def check(cond, msg):
    if not cond:
        _fail(msg)


def wait_for(pred, timeout=10.0):
    end = time.time() + timeout
    while time.time() < end:
        if pred():
            return True
        time.sleep(0.05)
    return False


class SlowPush:
    """conn_store.push_connection stand-in: blocks until released, counts calls."""

    def __init__(self):
        self.calls = 0
        self.gate = threading.Event()
        self.result = (True, "pushed 3", 3)

    def __call__(self, cid, actor=""):
        self.calls += 1
        self.gate.wait(20)
        return self.result


def job(jid):
    with Session(engine) as s:
        return s.get(Job, jid)


def api_tasks():
    with Session(engine) as s:
        return s.exec(select(Job).where(Job.kind == bg.KIND)).all()


# ---------------------------------------------------------------------------------- server
def server_half(c, H, H_U1, cid, push):
    path = f"/api/connectors/{cid}/push"
    ASYNC = {**H, "Prefer": "respond-async"}

    # A. untouched default
    push.gate.set()
    r = c.post(path, headers=H, json={})
    check(r.status_code == 200 and r.json().get("pushed") == 3, f"A sync push: {r.text}")
    check(push.calls == 1, "A: one run")
    check(not api_tasks(), "A: no api_task row without headers")
    ok("A no headers: synchronous 200, no job row")

    # B. background call
    push.gate.clear()
    t0 = time.time()
    r = c.post(path, headers=ASYNC, json={})
    check(r.status_code == 202, f"B launch status {r.status_code} {r.text}")
    check(time.time() - t0 < 5, "B: 202 came back while the route still runs")
    jid = r.json()["job_id"]
    check(r.json()["status_url"] == f"/api/jobs/{jid}", "B status_url")
    check(r.headers.get("preference-applied") == "respond-async", "B Preference-Applied")
    check(wait_for(lambda: push.calls == 2), "B: route started in the background")
    r = c.get(f"/api/jobs/{jid}", headers=H)
    check(r.status_code == 200 and r.json()["status"] == JobStatus.RUNNING, f"B running {r.text}")
    r2 = c.post(path, headers=ASYNC, json={})
    check(r2.status_code == 202 and r2.json()["job_id"] == jid, f"B dedupe {r2.text}")
    check(r2.json().get("existing") is True, "B existing flag")
    time.sleep(0.3)
    check(push.calls == 2, "B: identical background call started nothing new")
    push.gate.set()
    check(wait_for(lambda: job(jid).status == JobStatus.COMPLETED), "B finished")
    body = c.get(f"/api/jobs/{jid}", headers=H).json()
    res = body["result"]
    check(res["http_status"] == 200 and res["response"]["pushed"] == 3, f"B result {res}")
    ok("B Prefer: respond-async -> 202 + job, running, deduped, result {http_status, response}")

    # B2. a failing route is a failed job carrying the route's own error
    push.result = (False, "tenant said no", 0)
    r = c.post(path, headers=ASYNC, json={"x": 1})
    jid_f = r.json()["job_id"]
    check(wait_for(lambda: job(jid_f).status == JobStatus.FAILED), "B2 failed")
    jf = c.get(f"/api/jobs/{jid_f}", headers=H).json()
    check(jf["result"]["http_status"] == 400, f"B2 status {jf}")
    check("tenant said no" in (jf.get("error") or ""), f"B2 error {jf}")
    push.result = (True, "pushed 3", 3)
    ok("B2 a route failure is a FAILED job with the route's status and message")

    # C. idempotency keys
    n = push.calls
    K = {**H, "Idempotency-Key": "key-one"}
    r = c.post(path, headers=K, json={})
    check(r.status_code == 200 and r.json()["pushed"] == 3, f"C first {r.text}")
    check(push.calls == n + 1, "C first ran")
    r = c.post(path, headers=K, json={})
    check(r.status_code == 200 and r.json()["pushed"] == 3, f"C replay {r.text}")
    check(r.headers.get("idempotent-replayed") == "true", "C replay header")
    check(push.calls == n + 1, "C replay did not run")
    r = c.post(path, headers=K, json={"different": True})
    check(r.status_code == 422 and r.json()["code"] == "idempotency_key_reused", r.text)
    r = c.post(path, headers={**H, "Idempotency-Key": "has space"}, json={})
    check(r.status_code == 400 and r.json()["code"] == "idempotency_key_invalid", r.text)
    check(push.calls == n + 1, "C refused requests ran nothing")
    # the same key from ANOTHER caller is a different key (and a different connection
    # would be a different route): user1 cannot see admin's answer
    ok("C Idempotency-Key: replay without running, 422 on reuse, 400 on a bad key")

    push.gate.clear()
    K2 = {**H, "Idempotency-Key": "key-two"}
    r = c.post(path, headers={**K2, "Prefer": "respond-async"}, json={})
    check(r.status_code == 202, r.text)
    jid2 = r.json()["job_id"]
    r = c.post(path, headers=K2, json={})
    check(r.status_code == 409 and r.json()["code"] == "idempotency_in_progress", r.text)
    check(r.json()["job_id"] == jid2, "C2 409 names the job")
    r = c.post(path, headers={**K2, "Prefer": "respond-async"}, json={})
    check(r.status_code == 202 and r.json()["job_id"] == jid2, "C2 async repeat -> same job")
    push.gate.set()
    check(wait_for(lambda: job(jid2).status == JobStatus.COMPLETED), "C2 finished")
    r = c.post(path, headers=K2, json={})
    check(r.status_code == 200 and r.headers.get("idempotent-replayed") == "true", r.text)
    ok("C2 running key: sync repeat 409 idempotency_in_progress, async repeat gets the job")

    # not-kept statuses free the key
    jid3 = bg._insert("admin", "POST x", "fp", "idem-x", False)
    bg._finish(jid3, 429, b'{"error": "slow down"}', "application/json")
    check(job(jid3).payload.get("idem") is None, "C3 429 not kept")
    jid4 = bg._insert("admin", "POST x", "fp", "idem-y", False)
    bg._finish(jid4, 503, b'{"error": "busy"}', "application/json")
    check(job(jid4).payload.get("idem") is None, "C3 503 not kept")
    ok("C3 429 and 5xx answers are not kept for replay")

    # D. bookkeeping
    for row in api_tasks():
        p = row.payload or {}
        check("project_id" not in p and "document_id" not in p, f"D payload {p}")
    r = c.get(f"/api/jobs/{jid}", headers=H_U1)
    check(r.status_code == 404, f"D foreign read {r.status_code}")
    before = len(api_tasks())
    r = c.post(path, headers={"Prefer": "respond-async"}, json={})
    check(r.status_code == 401, f"D no identity -> 401, got {r.status_code}")
    check(len(api_tasks()) == before, "D no row for an unauthenticated call")
    jid5 = bg._insert("admin", "POST y", "fp5", "idem-z", True)
    check(bg.fail_interrupted() >= 1, "D restart sweep")
    j5 = job(jid5)
    check(j5.status == JobStatus.FAILED and "restart" in j5.error, "D restart failed the row")
    check(j5.payload.get("idem") is None, "D restart freed the key")
    ok("D no project/document in payload, foreign 404, 401 untouched, restart sweep")

    # D2 (gate). Lookups read only the caller's rows; old response bodies are dropped.
    jid6 = bg._insert("user1", "POST z", "fp-shared", None, True)
    check(bg._find("admin", None, "fp-shared", True) == (None, ""), "D2 other caller's row")
    found, why = bg._find("user1", None, "fp-shared", True)
    check(found is not None and found.id == jid6 and why == "inflight", "D2 own row found")
    bg._finish(jid6, 200, b'{"ok": true, "big": "x"}', "application/json")
    with Session(engine) as s:
        row = s.get(Job, jid6)
        row.finished_at = row.finished_at - bg.RESULT_KEEP - bg.timedelta(minutes=1)
        s.add(row)
        s.commit()
    check(bg.trim_old_results(force=True) >= 1, "D2 trim ran")
    res = job(jid6).result
    check(res.get("http_status") == 200 and "response" not in res, f"D2 trimmed {res}")
    check(res.get("response_expired") is True, "D2 expired marker")
    check("response" in job(jid).result, "D2 a recent result is kept")
    ok("D2 lookups scoped to the caller's rows; results older than 7 days keep status only")

    # D3 (gate, #746 interplay). A route that offloads blocking work (deps.offload_blocking,
    # the bounded stream-worker pool) still works on a background call's private event loop.
    import contextvars

    async def offloading_route(scope, receive, send):
        await receive()
        from app.web import deps

        name = await deps.offload_blocking(lambda: threading.current_thread().name)
        await send(
            {
                "type": "http.response.start",
                "status": 200,
                "headers": [(b"content-type", b"application/json")],
            }
        )
        await send({"type": "http.response.body", "body": json.dumps({"ran_on": name}).encode()})

    jid7 = bg._insert("admin", "POST offload", "fp-offload", None, True)
    check(bg._SLOTS.acquire(blocking=False), "D3 slot")
    worker = threading.Thread(
        target=bg._background,
        args=(offloading_route, {"type": "http"}, b"{}", jid7, contextvars.copy_context()),
    )
    worker.start()
    worker.join(20)
    res7 = job(jid7).result or {}
    check(job(jid7).status == JobStatus.COMPLETED, f"D3 status {job(jid7).status} {res7}")
    ran_on = (res7.get("response") or {}).get("ran_on")
    check(ran_on and ran_on != worker.name, f"D3 offloaded to a worker thread: {res7}")
    ok("D3 offload_blocking routes run on a background call's own loop (#746 pool)")

    # E. coverage of every billing route
    for method, tpl in sorted(registry.SPEND_ROUTES):
        concrete = tpl
        for ph in ("{doc_id}", "{wid}", "{batch_id}", "{gid}", "{pid}", "{cid}", "{did}"):
            concrete = concrete.replace(ph, "7")
        check(bg.covered(method, concrete), f"E uncovered spend route {method} {tpl}")
    check(not bg.covered("GET", "/api/connectors/7/push"), "E GET never covered")
    check(not bg.covered("POST", "/api/connectors/7/airtable/webhook/translate-pending"), "E")
    for tool in registry.TOOLS:
        if tool.tier == "spend":
            props = tool.input_schema["properties"]
            check("idempotency_key" in props, f"E {tool.name} lacks idempotency_key")
    ok("E every SPEND_ROUTES path covered; every spend tool takes idempotency_key")


# ------------------------------------------------------------------------------ MCP fakes
class Fake:
    def __init__(self, handler):
        self.handler = handler
        self.requests = []

    def install(self):
        def _h(req):
            self.requests.append(req)
            return self.handler(req)

        transport = httpx.MockTransport(_h)
        mcp_server._client = lambda: httpx.AsyncClient(transport=transport, timeout=5)


def tool(name, **args):
    args = with_plan(mcp_server, name, args)  # #757: a confirmed spend carries its plan_id
    return json.loads(asyncio.run(mcp_server.call_tool_unvalidated(name, args)))


def tool_error(name, **args):
    args = with_plan(mcp_server, name, args)
    try:
        asyncio.run(mcp_server.call_tool_unvalidated(name, args))
    except mcp_server.ToolError as exc:
        return str(exc)
    _fail(f"{name}: expected a ToolError")


def mcp_half():
    mcp_server.TOKEN = "lsu" + "_fixture_token"
    mcp_server.BASE_URL = "http://fake.invalid"
    conns = {"connections": [{"id": 5, "kind": "hubspot", "name": "h"}]}

    # F. transport errors
    def raiser(exc):
        def h(req):
            if req.method == "GET" and req.url.path == "/api/connectors":
                return httpx.Response(200, json=conns)
            raise exc

        return h

    Fake(raiser(httpx.ReadTimeout("read timed out"))).install()
    msg = tool_error("connector_push", connection_id=5, confirm=True)
    check("may STILL be running" in msg and "job_status" in msg, f"F write timeout: {msg}")
    check("Is the app running" not in msg, "F no 'is the app running' on a timeout")
    msg = tool_error("job_status", job_id=3)
    check("did not answer this read" in msg and "Nothing was changed" in msg, f"F read: {msg}")
    Fake(raiser(httpx.ConnectError("refused"))).install()
    msg = tool_error("connector_push", connection_id=5, confirm=True)
    check("Could not reach" in msg, f"F connect error: {msg}")
    ok("F read timeout on a write: may still be running; connect error: unreachable")

    # F2 (gate, with m-schema's #756 error tail): a connection dropped mid-answer is coded
    # timeout_maybe_running (not retryable), never "unreachable" (retryable).
    try:
        try:
            raise httpx.RemoteProtocolError("peer closed connection")
        except httpx.RemoteProtocolError as exc:
            raise mcp_server._timeout_error("POST", exc) from exc
    except mcp_server.ToolError as wrapped:
        tail = json.loads(mcp_server._error_tail("connector_push", wrapped))["error"]
    check(tail["code"] == "timeout_maybe_running" and tail["retryable"] is False, f"F2 {tail}")
    ok("F2 a dropped connection is timeout_maybe_running, not a retryable unreachable")

    # G. connector_sync launch-then-poll
    failures = [{"item": f"p{i}", "error": "x" * 900} for i in range(25)]
    state = {"polls": 0}

    def sync_handler(req):
        if req.method == "POST" and req.url.path == "/api/connectors/5/sync":
            sent = json.loads(req.content)
            check(sent.get("async") is True, f"G sync body {sent}")
            check("prefer" not in req.headers, "G native async sends no Prefer header")
            return httpx.Response(
                202, json={"ok": True, "job_id": 41, "status_url": "/api/jobs/41"}
            )
        if req.method == "GET" and req.url.path == "/api/jobs/41":
            state["polls"] += 1
            if state["polls"] < 2:
                return httpx.Response(
                    200,
                    json={
                        "id": 41,
                        "kind": "connector_translate",
                        "status": "running",
                        "result": {},
                    },
                )
            return httpx.Response(
                200,
                json={
                    "id": 41,
                    "kind": "connector_translate",
                    "status": "completed",
                    "message": "done",
                    "result": {
                        "ok": True,
                        "message": "created 5, held 4",
                        "created": 5,
                        "held_back": 4,
                        "failures": failures,
                    },
                },
            )
        return httpx.Response(404, json={"ok": False, "error": "unexpected " + req.url.path})

    longrun.GAP_S = 0.01
    Fake(sync_handler).install()
    out = tool("connector_sync", connection_id=5, confirm=True)
    check(out["finished"] is True and out["created"] == 5 and out["job_id"] == 41, f"G {out}")
    check(out["held_back"] == 4, "G held_back surfaced")
    state["polls"] = 5
    st = tool("job_status", job_id=41)
    check(st["finished"] is True and st["result"]["created"] == 5, f"G job_status {st}")
    check(st["result"]["held_back"] == 4, "G job_status held_back")
    fl = st["result"]["failures"]
    check(len(fl) == 21 and "more not shown" in fl[-1], f"G failures capped: {len(fl)}")
    check(len(fl[0]["error"]) < 600, "G long error text capped")
    ok("G connector_sync async + poll returns the summary; job_status carries a capped result")

    state["polls"] = -10_000
    longrun_wait = longrun.WAIT_S
    longrun.WAIT_S = 0.05
    out = tool("connector_sync", connection_id=5, confirm=True)
    longrun.WAIT_S = longrun_wait
    check(out["status"] == "running" and out["finished"] is False, f"G2 {out}")
    check(out["job_id"] == 41 and "job_status" in out["next"], f"G2 next {out}")
    check("Do NOT call this tool again" in out["next"], "G2 warns against a second launch")
    ok("G2 a long run returns {status: running, job_id, next}")

    # background route failure -> the same ToolError the sync call raised
    def img_handler(req):
        if req.method == "POST" and req.url.path == "/api/images/9/translate":
            return httpx.Response(202, json={"ok": True, "job_id": 50})
        if req.url.path == "/api/jobs/50":
            return httpx.Response(
                200,
                json={
                    "id": 50,
                    "kind": "api_task",
                    "status": "failed",
                    "error": "HTTP 404: not found",
                    "result": {"http_status": 404, "response": {"ok": False, "error": "not found"}},
                },
            )
        return httpx.Response(500)

    Fake(img_handler).install()
    msg = tool_error("translate_image", document_id=9, locale="de-DE", confirm=True)
    check("not found" in msg.lower() and "background job 50" in msg, f"G3 {msg}")
    ok("G3 a background 404 raises the sync call's ToolError")

    # idempotency_key forwarding
    seen = []

    def text_handler(req):
        seen.append(req)
        if req.url.path == "/api/translate/text":
            return httpx.Response(200, json={"ok": True, "translation": "x"})
        return httpx.Response(200, json={"ok": True, "engines": [], "providers": []})

    Fake(text_handler).install()
    tool("translate_text", text="hi", target_locale="de-DE", confirm=True, idempotency_key="abc-1")
    posts = [r for r in seen if r.method == "POST"]
    check(posts and posts[-1].headers.get("idempotency-key") == "abc-1", "G4 header forwarded")
    seen.clear()
    with contextlib.suppress(mcp_server.ToolError):
        asyncio.run(
            mcp_server.call_tool_unvalidated(
                "translate_text", {"text": "hi", "target_locale": "de-DE", "idempotency_key": "p"}
            )
        )
    check(all("idempotency-key" not in r.headers for r in seen), "G4 plan sends no key")
    msg = tool_error(
        "translate_text", text="hi", target_locale="de-DE", confirm=True, idempotency_key="bad key"
    )
    check("idempotency_key must be" in msg, f"G4 malformed key {msg}")
    ok("G4 idempotency_key rides on confirmed spend calls only; malformed refused")

    # G5 (gate). A tool that used the 210 s timeout keeps it on the launch, so an install
    # without background calls (it answers synchronously) is not cut off at 30 s.
    timeouts = {}

    def timeout_handler(req):
        if req.method == "GET" and req.url.path == "/api/connectors":
            return httpx.Response(200, json=conns)
        if req.method == "POST":
            timeouts[req.url.path] = (req.extensions.get("timeout") or {}).get("read")
            return httpx.Response(200, json={"ok": True, "pushed": 1})
        return httpx.Response(404, json={"ok": False})

    Fake(timeout_handler).install()
    tool("connector_sync", connection_id=5, confirm=True)
    tool("connector_push", connection_id=5, confirm=True)
    check(timeouts.get("/api/connectors/5/sync") == 210.0, f"G5 sync timeout {timeouts}")
    check(timeouts.get("/api/connectors/5/push") == 30.0, f"G5 push timeout {timeouts}")
    ok("G5 formerly slow tools keep the 210 s read timeout on the launch")


# ------------------------------------------------------------------------------ end to end
def e2e_half(key, cid, push):
    mcp_server.TOKEN = key
    mcp_server.BASE_URL = "http://testserver"
    transport = httpx.ASGITransport(app=app)
    mcp_server._client = lambda: httpx.AsyncClient(transport=transport, timeout=30)
    push.gate.set()
    push.result = (True, "pushed 7", 7)
    first = with_plan(
        mcp_server,
        "connector_push",
        {"connection_id": cid, "confirm": True, "idempotency_key": "e2e-1"},
    )
    out = tool("connector_push", **first)
    check(out.get("pushed") == 7 and out.get("finished") is True, f"H push {out}")
    n = push.calls
    # the agent's retry re-presents the SAME plan_id and key (#757 lets that replay through)
    out = tool("connector_push", **first)
    check(out.get("pushed") == 7 and push.calls == n, f"H idempotent re-issue ran again: {out}")

    calls = {"n": 0}

    def fake_translate_now(conn_id, actor=""):
        calls["n"] += 1
        return True, "created 5, held 4 for review", {"created": 5, "updated": 0, "held_back": 4}

    real = conn_store.translate_now
    conn_store.translate_now = fake_translate_now
    try:
        out = tool("connector_sync", connection_id=cid, confirm=True)
    finally:
        conn_store.translate_now = real
    check(out.get("created") == 5 and out.get("held_back") == 4, f"H sync {out}")
    check(out.get("finished") is True and calls["n"] == 1, f"H sync ran once {out}")
    st = tool("job_status", job_id=out["job_id"])
    check(st["result"]["held_back"] == 4 and st["kind"] == "connector_translate", f"H {st}")
    ok("H end to end over ASGI: push (idempotent) and sync return real summaries")


# ------------------------------------------------------------------------------- remote CLI
def cli_half(c, key, cid, push):
    from app.cli._common import CliError
    from app.cli._remote import RemoteClient

    def handler(request):
        path = request.url.path + ("?" + request.url.query.decode() if request.url.query else "")
        r = c.request(request.method, path, headers=dict(request.headers), content=request.content)
        return httpx.Response(
            r.status_code,
            content=r.content,
            headers={"content-type": r.headers.get("content-type", "")},
        )

    bridge = httpx.MockTransport(handler)
    push.gate.set()
    push.result = (True, "pushed 9", 9)
    rc = RemoteClient("http://127.0.0.1", key, transport=bridge, idempotency_key="cli-1")
    n = push.calls
    out = rc.post_background(f"/api/connectors/{cid}/push", interval_s=0.05)
    check(out.get("pushed") == 9 and isinstance(out.get("job_id"), int), f"I bg push {out}")
    check(push.calls == n + 1, "I ran once")
    again = rc.post_background(f"/api/connectors/{cid}/push", interval_s=0.05)
    check(again.get("pushed") == 9 and push.calls == n + 1, f"I key replay ran again: {again}")
    push.result = (False, "tenant said no", 0)
    rc2 = RemoteClient("http://127.0.0.1", key, transport=bridge)
    bad = rc2.post_background(f"/api/connectors/{cid}/push", interval_s=0.05)
    check(bad.get("ok") is False and "tenant said no" in bad.get("message", ""), f"I env {bad}")
    push.result = (True, "pushed 3", 3)
    try:
        RemoteClient("http://127.0.0.1", key, transport=bridge, idempotency_key="a b")
    except CliError as exc:
        check(exc.code == 2, f"I bad key exit {exc.code}")
    else:
        _fail("I a malformed --idempotency-key must be a usage error")
    ok("I remote CLI: background pull/push polled to the end, --idempotency-key replays")


def main():
    init_db()
    security.seed_initial()
    admin = security.get_by_email("admin")
    user1 = security.get_by_email("user1")
    _k, key = apikeys.create_key(admin.id, "bg", scopes="api docs")
    _k, key_u1 = apikeys.create_key(user1.id, "bg u1", scopes="api docs")
    H = {"X-Loc-Token": key}
    H_U1 = {"X-Loc-Token": key_u1}
    with Session(engine) as s:
        conn = Connection(kind="hubspot", name="bg test", config={})
        s.add(conn)
        s.commit()
        s.refresh(conn)
        cid = conn.id
    push = SlowPush()
    conn_store.push_connection = push
    c = TestClient(app)
    server_half(c, H, H_U1, cid, push)
    mcp_half()
    e2e_half(key, cid, push)
    cli_half(c, key, cid, push)
    print(f"{len(CHECKS)} checks")
    print("MCP BACKGROUND CALLS OK")


if __name__ == "__main__":
    main()
