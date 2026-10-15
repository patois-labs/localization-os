"""#495: `async def` routes must not run blocking LLM, parse or DB work on the event loop.

While one of these runs inline every other request on the worker stalls, /ready included,
so the platform can mark the machine unhealthy (the 2026-09-06 outage pattern).

Two proofs, both offline and hermetic (throwaway LOC_TMS_DATA_DIR, provider keys blanked):

  loop lag     each route is driven through the real ASGI app on a real event loop with its
               blocking service call replaced by a `time.sleep(0.6)` stub, next to a 20 ms
               "health probe" ticker. A route that runs the stub inline freezes the ticker for
               the whole sleep; an off-loop route leaves it ticking (max gap well under 0.3 s).
  static guard an AST scan of app/web/*.py: no `async def` calls a known-blocking service
               directly (a reference passed to run_in_threadpool is not a call, so it passes).
  liveness     /health and /ready never reach `security.client_ip` (a DB read) in the
               middleware; GET /api/runs/{id} and /api/batches/{id} are plain `def` routes.

Final line: "ASYNC ROUTES OFFLOOP OK".
"""

import ast
import asyncio
import os
import sys
import time
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(_ROOT)
sys.path.insert(0, _ROOT)
from tests._bootstrap import init_env

init_env("loctms_offloop_")

import httpx
from fastapi.responses import PlainTextResponse

from app.db import init_db
from app.main import app
from app.services import settings

init_db()
from app.kernel import audit as _audit

_audit.log(actor="warmup", action="warmup", target="warmup", ip="127.0.0.1")  # first write is slow
settings.set_many({"security_auth_required": "0", "multi_org_enabled": "0", "project_scoping": "0"})
settings.invalidate_cache()

import app.web.api_cli_misc_3 as api_misc
import app.web.api_cli_projects_2 as api_projects
import app.web.auth as web_auth
import app.web.body_limit as body_limit
import app.web.glossaries as web_glossaries
import app.web.glossaries_approval_can_tune_ai as web_gl_main
import app.web.images as web_images
import app.web.projects_glossary as web_projglos
import app.web.workflows as web_workflows
from app.services import security

BLOCK_S = 0.6
SLEEP_S = BLOCK_S
MAX_GAP_S = 0.3
_FAILS = []


def _check(name, ok, detail=""):
    print(("PASS " if ok else "FAIL ") + name + (f"  {detail}" if detail else ""))
    if not ok:
        _FAILS.append(name)


def _blocker(result=None, exc=None):
    """A stand-in for a slow service call: blocks the CALLING thread, records that it ran."""
    state = {"calls": 0, "thread": None}

    def _stub(*a, **kw):
        state["calls"] += 1
        state["thread"] = __import__("threading").current_thread().name
        time.sleep(SLEEP_S)
        if exc is not None:
            raise exc
        return result

    _stub.state = state
    return _stub


async def _drive(method, url, **kw):
    """Fire one request through the ASGI app while a ticker measures event-loop lag."""
    gaps = []
    stop = asyncio.Event()

    async def ticker():
        last = time.perf_counter()
        while not stop.is_set():
            await asyncio.sleep(0.02)
            now = time.perf_counter()
            gaps.append(now - last)
            last = now

    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False, client=("127.0.0.1", 5))
    async with httpx.AsyncClient(
        transport=transport, base_url="http://127.0.0.1", follow_redirects=False
    ) as client:
        tick = asyncio.create_task(ticker())
        await asyncio.sleep(0.1)  # let the ticker establish a baseline
        t0 = time.perf_counter()
        resp = await client.request(method, url, **kw)
        elapsed = time.perf_counter() - t0
        stop.set()
        await tick
    return resp, max(gaps) if gaps else 0.0, elapsed


def _loop_case(name, method, url, stub, patches, **kw):
    """Drive one route with `stub` installed; assert the stub ran AND the loop kept ticking."""
    import contextlib

    global SLEEP_S
    with contextlib.ExitStack() as stack:
        for target, attr, value in patches:
            stack.enter_context(mock.patch.object(target, attr, value))
        # Warm-up pass with no sleep: first-touch costs (imports, templates, pools) are not
        # event-loop blocking by the route under test and must not count as lag.
        SLEEP_S = 0.0
        asyncio.run(_drive(method, url, **kw))
        SLEEP_S = BLOCK_S
        stub.state["calls"] = 0
        resp, max_gap, elapsed = asyncio.run(_drive(method, url, **kw))
    ran = stub.state["calls"] >= 1
    _check(f"{name}: blocking call reached (status {resp.status_code})", ran)
    _check(
        f"{name}: event loop stayed responsive",
        ran and max_gap < MAX_GAP_S,
        f"max tick gap {max_gap:.3f}s, request {elapsed:.3f}s",
    )
    _check(
        f"{name}: blocking call ran off the loop thread",
        ran and stub.state["thread"] != __import__("threading").main_thread().name,
        str(stub.state["thread"]),
    )


# --- 1. api_image_translate ---------------------------------------------------------
stub = _blocker(result={})
ident = {"actor": "tester", "org_id": 1}
_loop_case(
    "POST /api/images/{id}/translate",
    "POST",
    "/api/images/1/translate",
    stub,
    [
        (web_images, "_api_flag_block", lambda *a, **k: None),
        (web_images.deps, "_require_doc_scope", lambda request: (ident, None)),
        (
            web_images,
            "_api_image_doc",
            lambda s, i, d: (SimpleNamespace(id=1, project_id=1), None),
        ),
        (web_images, "_api_write_block", lambda *a, **k: None),
        (web_images, "_doc_locales", lambda *a, **k: ["de-DE"]),
        (web_images, "_api_image_mock_block", lambda *a, **k: None),
        (web_images.il, "translate_image", stub),
    ],
    json={"locale": "de-DE"},
)

# --- 2. api_jobs_run ----------------------------------------------------------------
from app.models import Job, JobStatus

stub = _blocker(result=Job(kind="translate_document", status=JobStatus.COMPLETED))
_loop_case(
    "POST /api/jobs/run",
    "POST",
    "/api/jobs/run",
    stub,
    [
        (api_misc, "_gate", lambda request: (ident, 1, None)),
        (api_misc, "_write_block", lambda ident: None),
        (api_misc.jobs, "run_sync", stub),
        (api_misc, "_job_api_prepare", lambda request, i, k, p: (None, p)),
    ],
    json={"kind": "translate_document", "payload": {}},
)

# --- 3. web glossary_extract ----------------------------------------------------------
fake_g = SimpleNamespace(id=1, status="draft")
stub = _blocker(result={"blocked": True})
_loop_case(
    "POST /glossaries/{id}/extract",
    "POST",
    "/glossaries/1/extract",
    stub,
    [
        (web_glossaries, "_ai_block", lambda request: (None, None)),
        (web_glossaries, "_visible_or_404", lambda r, s, g: (fake_g, None)),
        (web_glossaries.tex, "visible_document", lambda *a, **k: SimpleNamespace(id=7)),
        (web_glossaries.tex, "extract", stub),
    ],
    data={"document_id": "7", "target_locale": "de-DE", "source_locale": "en-US"},
)

# --- 4. web glossary_build --------------------------------------------------------------
stub = _blocker(result={"blocked": True})
_loop_case(
    "POST /glossaries/{id}/build",
    "POST",
    "/glossaries/1/build",
    stub,
    [
        (web_gl_main, "_ai_block", lambda request: (None, None)),
        (web_gl_main, "_visible_or_404", lambda r, s, g: (fake_g, None)),
        (web_gl_main.tex, "extract", stub),
    ],
    data={"text": "hello", "target_locale": "de-DE", "source_locale": "en-US"},
)

# --- 5. web glossary import (module glossary) --------------------------------------------
stub = _blocker(exc=ValueError("stop"))
_loop_case(
    "POST /glossaries/{id}/import",
    "POST",
    "/glossaries/1/import",
    stub,
    [
        (web_gl_main, "_ai_block", lambda request: (None, None)),
        (web_gl_main, "_visible_or_404", lambda r, s, g: (fake_g, None)),
        (web_gl_main.gm, "import_bytes", stub),
    ],
    data={"source_locale": "en-US", "target_locale": "de-DE"},
    files={"file": ("g.csv", b"a,b\n", "text/csv")},
)

# --- 6. web project TM import + legacy glossary import ------------------------------------
stub = _blocker(exc=ValueError("stop"))
_loop_case(
    "POST /projects/{id}/tm/import",
    "POST",
    "/projects/1/tm/import",
    stub,
    [
        (web_projglos, "_visibility_block", lambda *a, **k: None),
        (web_projglos.tm, "import_tmx", stub),
    ],
    data={"source_locale": "en-US", "target_locale": "de-DE"},
    files={"file": ("t.tmx", b"<tmx/>", "application/xml")},
)
stub = _blocker(exc=ValueError("stop"))
_loop_case(
    "POST /projects/{id}/glossary/import",
    "POST",
    "/projects/1/glossary/import",
    stub,
    [
        (web_projglos, "_visibility_block", lambda *a, **k: None),
        (web_projglos.glossary, "import_bytes", stub),
    ],
    data={"source_locale": "en-US", "target_locale": "de-DE"},
    files={"file": ("g.csv", b"a,b\n", "text/csv")},
)

# --- 7. the /api twins of extract, TM import and glossary import ---------------------------
import base64

import app.web.api_cli_common as _common  # noqa: F401  (patched via the pieces below)


def _api_gate(request):
    return ident, 1, None


_b64 = base64.b64encode(b"x").decode()
stub = _blocker(exc=ValueError("stop"))
_loop_case(
    "POST /api/projects/{id}/tm/import",
    "POST",
    "/api/projects/1/tm/import",
    stub,
    [
        (api_projects, "_gate", _api_gate),
        (api_projects, "_write_block", lambda ident: None),
        (api_projects, "_project_in_org", lambda *a, **k: object()),
        (api_projects.tm, "import_tmx", stub),
    ],
    json={"source": "en-US", "target": "de-DE", "tmx_b64": _b64},
)
stub = _blocker(exc=ValueError("stop"))
_loop_case(
    "POST /api/projects/{id}/glossary/import",
    "POST",
    "/api/projects/1/glossary/import",
    stub,
    [
        (api_projects, "_gate", _api_gate),
        (api_projects, "_write_block", lambda ident: None),
        (api_projects, "_project_in_org", lambda *a, **k: object()),
        (api_projects.glossary, "import_bytes", stub),
    ],
    json={"source": "en-US", "target": "de-DE", "csv_b64": _b64},
)

# --- 8. workflow author repair + confirm ---------------------------------------------------
from app.kernel import setup_author

_review = lambda *a, **k: PlainTextResponse("review")  # noqa: E731
stub = _blocker(exc=setup_author.SetupAuthorError("provider down"))
_loop_case(
    "POST /workflows/author/confirm",
    "POST",
    "/workflows/author/confirm",
    stub,
    [
        (web_workflows, "_ai_block", lambda request: (None, None)),
        (web_workflows, "_error_page", lambda *a, **k: PlainTextResponse("err")),
        (setup_author, "apply_workflow_draft", stub),
    ],
    data={"description": "x"},
)
calls = {"apply": 0}


def _apply_invalid(*a, **k):
    calls["apply"] += 1
    raise setup_author.WorkflowApplyError(["bad draft"])


stub = _blocker(exc=setup_author.SetupAuthorError("provider down"))
_loop_case(
    "POST /workflows/author/repair",
    "POST",
    "/workflows/author/repair",
    stub,
    [
        (web_workflows, "_ai_block", lambda request: (None, None)),
        (web_workflows, "_render_workflow_review", _review),
        (setup_author, "apply_workflow_draft", _apply_invalid),
        (setup_author, "repair_workflow_draft", stub),
    ],
    data={"description": "x"},
)

# --- 9. SCIM body routes + SAML ACS ---------------------------------------------------------
from app.kernel import scim as scim_kernel

stub = _blocker(result={"id": "u1"})
slow_auth = _blocker(result={"org_id": 1, "actor": "scim"})
_loop_case(
    "POST /scim/v2/Users (service call)",
    "POST",
    "/scim/v2/Users",
    stub,
    [
        (scim_kernel, "enabled", lambda: True),
        (scim_kernel, "resolve_identity", lambda token: {"org_id": 1, "actor": "scim"}),
        (scim_kernel, "create_user", lambda *a, **k: (stub(), True)),
    ],
    json={"userName": "a@example.com"},
)
stub = _blocker(result={"id": "u1"})
_loop_case(
    "POST /scim/v2/Users (token lookup)",
    "POST",
    "/scim/v2/Users",
    slow_auth,
    [
        (scim_kernel, "enabled", lambda: True),
        (scim_kernel, "resolve_identity", slow_auth),
        (scim_kernel, "create_user", lambda *a, **k: ({"id": "u"}, True)),
    ],
    json={"userName": "a@example.com"},
)
stub = _blocker(result=(None, "refused"))
_loop_case(
    "POST /auth/saml/acs",
    "POST",
    "/auth/saml/acs",
    stub,
    [
        (web_auth.saml, "enabled", lambda: True),
        (web_auth.saml, "process_acs", stub),
    ],
    data={"SAMLResponse": "x"},
)

# --- 10. body_cap lookup for a body over the anonymous cap -----------------------------------
import app.config as config

stub = _blocker(result=config.REQUEST_MAX_ANON_BODY_BYTES * 4)
_loop_case(
    "body_limit middleware body_cap lookup",
    "POST",
    "/api/jobs/run",
    stub,
    [(body_limit, "body_cap", stub)],
    content=b"x" * (config.REQUEST_MAX_ANON_BODY_BYTES + 10),
)

# --- 11. liveness paths never reach the DB-backed client_ip ------------------------------------
probe = mock.MagicMock(side_effect=AssertionError("client_ip must not run for liveness paths"))
with mock.patch.object(security, "client_ip", probe):
    for path in ("/health", "/ready"):
        resp, _gap, _el = asyncio.run(_drive("GET", path))
        _check(
            f"GET {path} answers without client_ip",
            resp.status_code in (200, 503),
            str(resp.status_code),
        )
_check(
    "client_ip was never called for /health or /ready", probe.call_count == 0, str(probe.call_count)
)

# --- 12. polled GET routes are plain `def` (the framework runs them in the threadpool) ----------
for route in app.routes:
    path = getattr(route, "path", "")
    if path in ("/api/runs/{run_id}", "/api/batches/{batch_id}") and "GET" in getattr(
        route, "methods", ()
    ):
        _check(
            f"GET {path} is a sync route",
            not asyncio.iscoroutinefunction(route.endpoint),
        )

# --- 12b. #746: engine probes ---------------------------------------------------------------
# The engine test / model-list network probes block for seconds; they must run off the loop.
import app.web.api_cli_admin_2 as api_admin2
from app.kernel import engines as k_engines

stub = _blocker(result=(True, "reachable"))
_loop_case(
    "POST /api/admin/engines/{id}/test (#746)",
    "POST",
    "/api/admin/engines/1/test",
    stub,
    [
        (api_admin2, "_r3_gate", lambda request: (ident, 1, None)),
        (api_admin2, "_owned_engine", lambda *a, **k: object()),
        (k_engines, "test_profile", stub),
    ],
)
stub = _blocker(result={"models": [], "blocked": False})
_loop_case(
    "POST /api/admin/engines/{id}/models (#746)",
    "POST",
    "/api/admin/engines/1/models",
    stub,
    [
        (api_admin2, "_r3_gate", lambda request: (ident, 1, None)),
        (api_admin2, "_owned_engine", lambda *a, **k: object()),
        (k_engines, "list_models", stub),
    ],
)
# #746 gate: a query close mails the participants over SMTP (15 s timeout per recipient) from
# an async route; the notifier must run in the bounded pool, never on the loop.
import app.web.api_cli_misc_6 as api_misc6
from app.kernel import notifications as k_notifications
from app.models import QueryStatus as _QS

stub = _blocker(result=True)
_loop_case(
    "POST /api/queries/{id}/status close mail (#746 gate)",
    "POST",
    "/api/queries/1/status",
    stub,
    [
        (api_misc6.deps, "feature_api_block", lambda slug: None),
        (api_misc6, "_gate", lambda request: (ident, 1, None)),
        (api_misc6, "_creator", lambda i: None),
        (
            api_misc6.qthreads,
            "set_status",
            lambda s, **k: SimpleNamespace(status=_QS.CLOSED, project_id=1),
        ),
        (
            api_misc6,
            "audit",
            SimpleNamespace(log=lambda **k: None, customer_for_project=lambda p: None),
        ),
        (api_misc6, "_qthread_dict", lambda t: {"status": t.status}),
        (k_notifications, "on_query_closed", stub),
    ],
    json={"status": "closed"},
)

# --- 13. static guard: no async def calls a known-blocking service directly ---------------------
BLOCKING = {
    # #746
    "eval_runner.run_experiment",
    "qmeta.evaluate",
    "qmeta.evaluate_rules",
    "style_ab.run_ab",
    "context_ab.run_ab",
    "engines.test_profile",
    "engines.list_models",
    "tex.extract",
    "tm.import_tmx",
    "glossary.import_bytes",
    "gm.import_bytes",
    "il.translate_image",
    "jobs.run_sync",
    "setup_author.apply_workflow_draft",
    "setup_author.repair_workflow_draft",
    "setup_author.author_workflow_setup",
    "setup_author.author_project_setup",
    "orchestrator.run_workflow",
    "saml.process_acs",
    "scim.create_user",
    "scim.replace_user",
    "scim.patch_user",
    "scim.create_group",
    "scim.replace_group",
    "scim.patch_group",
    "body_cap",
}


def _dotted(node):
    parts = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if isinstance(node, ast.Name):
        parts.append(node.id)
    return ".".join(reversed(parts))


class _Scan(ast.NodeVisitor):
    def __init__(self, fname):
        self.fname = fname
        self.in_async = 0
        self.hits = []

    def visit_AsyncFunctionDef(self, node):
        self.in_async += 1
        self.generic_visit(node)
        self.in_async -= 1

    def visit_FunctionDef(self, node):
        saved, self.in_async = self.in_async, 0  # a nested sync def runs wherever it is called
        self.generic_visit(node)
        self.in_async = saved

    def visit_Lambda(self, node):
        pass  # handed to run_in_threadpool, not run inline

    def visit_Call(self, node):
        if self.in_async and _dotted(node.func) in BLOCKING:
            self.hits.append(f"{self.fname}:{node.lineno} {_dotted(node.func)}")
        self.generic_visit(node)


hits = []
for py in sorted(Path(_ROOT, "app", "web").glob("*.py")):
    scan = _Scan(py.name)
    scan.visit(ast.parse(py.read_text(encoding="utf-8")))
    hits += scan.hits
_check("no async def calls a known-blocking service inline", not hits, "; ".join(hits))

# --- bounded pool (review) ------------------------------------------------------------
# The offload must use the reviewed bounded worker pool (deps.offload_parse), never
# Starlette's default 40-thread pool (verify_memory_bound pins that by AST). With ONE worker
# and a short wait, two concurrent jobs/run calls: one is served, the other is a retryable
# 503 with Retry-After, not a hang and not a 500.
from app import config as _config

_stub = _blocker(result=Job(kind="translate_document", status=JobStatus.COMPLETED))


async def _two_at_once():
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False, client=("127.0.0.1", 5))
    async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1") as client:
        body = {"kind": "translate_document", "payload": {}}
        return await asyncio.gather(
            client.post("/api/jobs/run", json=body), client.post("/api/jobs/run", json=body)
        )


with (
    mock.patch.object(_config, "STREAM_WORKERS", 1),
    mock.patch.object(_config, "STREAM_WAIT_S", 0.1),
    mock.patch.object(api_misc, "_gate", lambda request: (ident, 1, None)),
    mock.patch.object(api_misc, "_write_block", lambda ident: None),
    mock.patch.object(api_misc.jobs, "run_sync", _stub),
    mock.patch.object(api_misc, "_job_api_prepare", lambda request, i, k, p: (None, p)),
):
    _resps = asyncio.run(_two_at_once())
_codes = sorted(r.status_code for r in _resps)
_check("one worker: one call served, the other a retryable 503", _codes == [200, 503], _codes)
_busy = [r for r in _resps if r.status_code == 503]
_check(
    "the 503 carries Retry-After",
    bool(_busy) and bool(_busy[0].headers.get("retry-after")),
    _busy[0].headers if _busy else "",
)

# --- SAML ACS busy path (gate) --------------------------------------------------------
# process_acs spends the one-time assertion (replay cache + session pop). The ACS must take
# ONE worker admission for the whole sign-in, so a busy pool can only refuse BEFORE anything
# ran: the 503 must not call process_acs and must not delete the correlation cookie (the
# browser's resubmit has to correlate). A second admission after process_acs would burn the
# assertion and turn the "retry" into a replay refusal.
_acs_calls = {"n": 0}


def _acs_ok(request, post_data, *, routed_path):
    _acs_calls["n"] += 1
    return {"email": "sso@example.com", "attributes": {}, "next": "/"}, None


_saml_user = SimpleNamespace(id=1, email="sso@example.com")
_admissions = {"n": 0}
_real_offload = web_auth.offload_parse


async def _counting_offload(func, *a, **kw):
    _admissions["n"] += 1
    return await _real_offload(func, *a, **kw)


with (
    mock.patch.object(web_auth.saml, "enabled", lambda: True),
    mock.patch.object(web_auth.saml, "process_acs", _acs_ok),
    mock.patch.object(
        web_auth.security, "get_or_create_saml_user", lambda *a, **k: (_saml_user, None)
    ),
    mock.patch.object(web_auth.security, "login", lambda request, user: None),
    mock.patch.object(web_auth, "offload_parse", _counting_offload),
):
    _resp, _g, _e = asyncio.run(_drive("POST", "/auth/saml/acs", data={"SAMLResponse": "x"}))
_check("SAML ACS success redirects", _resp.status_code == 303, _resp.status_code)
_check(
    "SAML ACS takes exactly one worker admission for the whole sign-in",
    _admissions["n"] == 1,
    _admissions["n"],
)

_acs_calls["n"] = 0
_hold = _blocker(result=Job(kind="translate_document", status=JobStatus.COMPLETED))


async def _acs_while_busy():
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False, client=("127.0.0.1", 5))
    async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1") as client:
        job = asyncio.create_task(
            client.post("/api/jobs/run", json={"kind": "translate_document", "payload": {}})
        )
        await asyncio.sleep(0.15)  # the job now holds the only worker
        acs = await client.post("/auth/saml/acs", data={"SAMLResponse": "x"})
        await job
        return acs


with (
    mock.patch.object(_config, "STREAM_WORKERS", 1),
    mock.patch.object(_config, "STREAM_WAIT_S", 0.1),
    mock.patch.object(api_misc, "_gate", lambda request: (ident, 1, None)),
    mock.patch.object(api_misc, "_write_block", lambda ident: None),
    mock.patch.object(api_misc.jobs, "run_sync", _hold),
    mock.patch.object(api_misc, "_job_api_prepare", lambda request, i, k, p: (None, p)),
    mock.patch.object(web_auth.saml, "enabled", lambda: True),
    mock.patch.object(web_auth.saml, "process_acs", _acs_ok),
):
    _busy_acs = asyncio.run(_acs_while_busy())
_check("SAML ACS on a busy pool is a 503", _busy_acs.status_code == 503, _busy_acs.status_code)
_check(
    "busy SAML ACS never ran process_acs (assertion unspent)",
    _acs_calls["n"] == 0,
    _acs_calls["n"],
)
_check(
    "busy SAML ACS keeps the correlation cookie for the resubmit",
    web_auth.saml.TXN_COOKIE_NAME not in (_busy_acs.headers.get("set-cookie") or ""),
    _busy_acs.headers.get("set-cookie"),
)

# SCIM on a busy pool: an RFC 7644 503 with Retry-After, and the write never ran.
_scim_writes = {"n": 0}


def _scim_create(*a, **k):
    _scim_writes["n"] += 1
    return {"id": "u"}, True


async def _scim_while_busy():
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False, client=("127.0.0.1", 5))
    async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1") as client:
        job = asyncio.create_task(
            client.post("/api/jobs/run", json={"kind": "translate_document", "payload": {}})
        )
        await asyncio.sleep(0.15)  # the job now holds the only worker
        r = await client.post(
            "/scim/v2/Users", json={"userName": "a@example.com"}, headers={"X-Loc-Token": "t"}
        )
        await job
        return r


with (
    mock.patch.object(_config, "STREAM_WORKERS", 1),
    mock.patch.object(_config, "STREAM_WAIT_S", 0.1),
    mock.patch.object(api_misc, "_gate", lambda request: (ident, 1, None)),
    mock.patch.object(api_misc, "_write_block", lambda ident: None),
    mock.patch.object(api_misc.jobs, "run_sync", _hold),
    mock.patch.object(api_misc, "_job_api_prepare", lambda request, i, k, p: (None, p)),
    mock.patch.object(scim_kernel, "enabled", lambda: True),
    mock.patch.object(scim_kernel, "resolve_identity", lambda token: {"org_id": 1, "actor": "s"}),
    mock.patch.object(scim_kernel, "create_user", _scim_create),
):
    _busy_scim = asyncio.run(_scim_while_busy())
_check("SCIM on a busy pool is a 503", _busy_scim.status_code == 503, _busy_scim.status_code)
_check(
    "the SCIM 503 carries Retry-After",
    bool(_busy_scim.headers.get("retry-after")),
    dict(_busy_scim.headers),
)
_check("busy SCIM never ran the write", _scim_writes["n"] == 0, _scim_writes["n"])

if _FAILS:
    print("FAILED:", ", ".join(_FAILS))
    sys.exit(1)
print("ASYNC ROUTES OFFLOOP OK")
