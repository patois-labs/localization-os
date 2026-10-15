"""Background calls and idempotency keys for the slow /api spend routes (#728, #744, #747).

Documented in: docs/architecture/surfaces-and-parity.md

Every billing /api route (and connector pull/push) honours two OPTIONAL request headers. A
request that sends neither is passed through untouched, byte for byte.

* ``Prefer: respond-async`` (RFC 7240). The route runs in the background and the caller gets
  ``202 {"ok": true, "job_id": N, "status_url": "/api/jobs/N", "status": "running"}`` at
  once. ``GET /api/jobs/N`` (MCP ``job_status``, CLI ``job status N``) reports ``running``
  until the route finishes, then ``completed`` (HTTP status below 400) or ``failed``, with
  ``result = {"http_status": S, "response": <the route's own JSON body>}``. Why: an MCP
  client or a short-timeout script used to give up on a sync route (a HubSpot sync of 3 posts
  x 3 languages took about 5 minutes) while the server kept working, then retried and paid
  twice (#728). A second identical background request from the same caller while the first
  is still running gets the LIVE job back (``"existing": true``), never a second run.
* ``Idempotency-Key: <1-255 visible ASCII characters>``. The first request with a key runs
  and its final response is kept for 24 hours on its ``api_task`` Job row (so it survives a
  restart). A repeat with the same key and the same request replays that response (header
  ``Idempotent-Replayed: true``) without running anything (a multipart upload is compared with
  its random boundary removed); while the first is still running
  a background repeat gets the live job (202) and a synchronous repeat gets 409
  ``idempotency_in_progress`` naming the job; the same key on a DIFFERENT request is 422
  ``idempotency_key_reused``. Keys are per caller (the API key's account) and per route, so
  one key may cover an upload and the translate call that follows it. Responses that a
  retry should be allowed to redo (408, 409, 423, 425, 429 and every 5xx, e.g. a busy
  worker) are not kept. Why: the 2026-07-28 MCP revision dropped stream resumability, so a
  client whose stream breaks MUST re-issue the call (#744).

The work itself is the unchanged route: the same identity, gates, validation, audit rows and
spend controls run, inside this process, on a worker thread with its own event loop (so a
slow route does not hold the main loop either). The Job row is bookkeeping only: it carries
no project_id / document_id, so reports and budget meters (which read those payload keys) never
count it, and its cost stays on the rows the route itself writes. A restart while a
background call runs fails its row ("interrupted by a server restart"); the route's own state
(connector sync log, run rows) says how far it got. Cancelling the HTTP request of a
synchronous call does not stop the server work either; that is why both headers exist.
"""

from __future__ import annotations

import asyncio
import contextlib
import contextvars
import hashlib
import json
import re
import threading
from datetime import datetime, timedelta

from sqlmodel import Session, select
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from app.db import engine
from app.models import Job, JobStatus
from app.web.route_path import route_path

#: The Job.kind of a background or idempotent /api call. Not a jobs.HANDLERS kind: the row is
#: never queued (it is inserted RUNNING and finished by this module).
KIND = "api_task"

#: The routes these headers apply to: registry.SPEND_ROUTES (every billing route an MCP spend
#: tool or the CLI calls) plus connector pull and the document upload submit_translation makes
#: before its translate call. verify_mcp_background_calls pins that every SPEND_ROUTES path
#: matches one of these.
ROUTES: tuple[re.Pattern, ...] = tuple(
    re.compile(p)
    for p in (
        r"/api/documents",
        r"/api/documents/\d+/translate",
        r"/api/translate/text",
        r"/api/workflows/\d+/run",
        r"/api/workflows/\d+/run-batch",
        r"/api/workflows/author",
        r"/api/batches/\d+/rerun",
        r"/api/batches/\d+/retry-failed",
        r"/api/glossaries/\d+/extract",
        r"/api/projects/\d+/keys/translate",
        r"/api/images/\d+/translate",
        r"/api/style-guides/\d+/ab",
        r"/api/context/ab",
        r"/api/connectors/\d+/(?:pull|push|sync|retry)",
        r"/api/projects/\d+/runs",
        r"/api/connectors/\d+/[a-z0-9_]+/translate",
        r"/api/vendors/dispatches/\d+/(?:approve-quote|reject-rework)",
    )
)

IDEMPOTENCY_WINDOW = timedelta(hours=24)
_KEY_RE = re.compile(r"[\x21-\x7e]{1,255}")
#: Statuses a retry must be free to redo, so they are never replayed.
_NOT_REPLAYED = frozenset({408, 409, 423, 425, 429})
#: At most this many background calls run at once; more answer a retryable 503.
MAX_BACKGROUND = 8
_SLOTS = threading.BoundedSemaphore(MAX_BACKGROUND)
#: One process-wide lock around find-then-insert, so two identical requests cannot both
#: miss each other (the same guard jobs.submit_unique uses).
_LOCK = threading.Lock()
#: The largest response kept for a replay or a job result (bigger ones keep the status only).
_MAX_KEPT_BYTES = 1024 * 1024
#: A finished row keeps its response body this long (job_status / GET /api/jobs/{id} read it),
#: then the body is dropped and only the status stays. Bounds what api_task rows hold even
#: when the install's retention sweep is off (retention_days defaults to 0).
RESULT_KEEP = timedelta(days=7)
_TRIM_EVERY_S = 3600.0
_last_trim = [0.0]


def covered(method: str, path: str) -> bool:
    return method == "POST" and any(p.fullmatch(path) for p in ROUTES)


def wants_async(headers) -> bool:
    for part in (headers.get("prefer") or "").split(","):
        if part.split(";")[0].strip().lower() == "respond-async":
            return True
    return False


def _sha(*parts) -> str:
    h = hashlib.sha256()
    for p in parts:
        h.update(p if isinstance(p, bytes) else str(p).encode("utf-8", "surrogatepass"))
        h.update(b"\x00")
    return h.hexdigest()


def _normalized(headers, body: bytes) -> bytes:
    """The body as fingerprinted: a multipart body with its random boundary replaced, so a
    re-issued upload of the same file and fields is the same request."""
    ctype = headers.get("content-type") or ""
    m = re.search(r"boundary=\"?([^\";]+)", ctype)
    if ctype.lower().startswith("multipart/") and m:
        return body.replace(m.group(1).encode("latin-1", "replace"), b"BOUNDARY")
    return body


def _error(status: int, code: str, message: str, **extra) -> JSONResponse:
    return JSONResponse({"ok": False, "code": code, "error": message, **extra}, status_code=status)


def _accepted(jid: int, existing: bool) -> JSONResponse:
    body = {"ok": True, "job_id": jid, "status_url": f"/api/jobs/{jid}", "status": "running"}
    if existing:
        body["existing"] = True
    return JSONResponse(body, status_code=202, headers={"Preference-Applied": "respond-async"})


def _find(actor: str, idem: str | None, fingerprint: str, is_async: bool) -> tuple[Job | None, str]:
    """(job, why): the live or kept job this request maps to, or (None, "").

    Only this caller's rows are read (both the key and the fingerprint already include the
    caller, so another caller's row could never match; the filter keeps the scan small)."""
    now = datetime.utcnow()
    with Session(engine) as s:
        rows = s.exec(
            select(Job)
            .where(
                Job.kind == KIND,
                Job.created_by == actor,
                Job.created_at >= now - IDEMPOTENCY_WINDOW,
            )
            .order_by(Job.id.desc())
        ).all()
    if idem:
        for job in rows:
            if (job.payload or {}).get("idem") != idem:
                continue
            if job.status != JobStatus.RUNNING and "http_status" not in (job.result or {}):
                continue  # ended without a kept answer (e.g. a restart): free to run again
            return job, "idem"
    if not is_async:
        return None, ""
    for job in rows:
        p = job.payload or {}
        if job.status == JobStatus.RUNNING and p.get("fp") == fingerprint and p.get("async"):
            return job, "inflight"
    return None, ""


def _insert(actor: str, label: str, fingerprint: str, idem: str | None, is_async: bool) -> int:
    now = datetime.utcnow()
    with Session(engine) as s:
        job = Job(
            kind=KIND,
            payload={"route": label, "fp": fingerprint, "idem": idem, "async": is_async},
            created_by=actor,
            status=JobStatus.RUNNING,
            started_at=now,
            progress_done=0,
            progress_total=1,
            message=f"Running {label}" + (" in the background" if is_async else ""),
        )
        s.add(job)
        s.commit()
        s.refresh(job)
        return job.id


def _decode(raw: bytes, ctype: str):
    if len(raw) > _MAX_KEPT_BYTES:
        return None, True
    if "json" in ctype:
        try:
            return json.loads(raw), False
        except ValueError:
            pass
    return raw.decode("utf-8", "replace"), False


def _finish(jid: int, status: int, raw: bytes, ctype: str) -> None:
    body, too_big = _decode(raw, ctype)
    replayable = status < 500 and status not in _NOT_REPLAYED and not too_big
    error = ""
    if status >= 400:
        if isinstance(body, dict):
            error = str(body.get("error") or body.get("message") or body.get("detail") or "")
        error = (f"HTTP {status}: {error}" if error else f"HTTP {status}")[:500]
    result: dict = {"http_status": status}
    if too_big:
        result["response_truncated"] = True
    elif isinstance(body, (dict, list)):
        result["response"] = body
    else:
        result["response_text"] = body
    with Session(engine) as s:
        job = s.get(Job, jid)
        if job is None:
            return
        job.status = JobStatus.COMPLETED if status < 400 else JobStatus.FAILED
        job.finished_at = datetime.utcnow()
        job.progress_done = 1
        job.message = f"HTTP {status}"
        job.error = error
        job.result = result
        if not replayable:
            payload = dict(job.payload or {})
            payload["idem"] = None  # a retry with this key may run again
            job.payload = payload
        s.add(job)
        s.commit()


def _replay(job: Job) -> Response:
    res = job.result or {}
    status = int(res.get("http_status") or 200)
    headers = {"Idempotent-Replayed": "true", "X-Loc-Job-Id": str(job.id)}
    if "response" in res:
        return JSONResponse(res["response"], status_code=status, headers=headers)
    return Response(
        res.get("response_text") or "", status_code=status, headers=headers, media_type="text/plain"
    )


async def _run(app, scope, body: bytes) -> tuple[int, str, bytes]:
    """Run the downstream app once for this request and capture its response."""
    delivered = False

    async def receive():
        nonlocal delivered
        if not delivered:
            delivered = True
            return {"type": "http.request", "body": body, "more_body": False}
        # Nothing more will come, and the work must not read a missing client as a cancel.
        await asyncio.Event().wait()

    state = {"status": 500, "ctype": "", "chunks": []}

    async def send(message):
        if message["type"] == "http.response.start":
            state["status"] = int(message["status"])
            for k, v in message.get("headers") or []:
                if k.lower() == b"content-type":
                    state["ctype"] = v.decode("latin-1").lower()
        elif message["type"] == "http.response.body":
            state["chunks"].append(message.get("body") or b"")

    await app(scope, receive, send)
    return state["status"], state["ctype"], b"".join(state["chunks"])


def _background(app, scope, body: bytes, jid: int, ctx: contextvars.Context) -> None:
    """Thread body: run the route on a private event loop, record the outcome, free the slot."""

    async def main():
        try:
            status, ctype, raw = await _run(app, scope, body)
        except Exception as exc:  # the route crashed: record it, never lose the row
            _finish(jid, 500, json.dumps({"error": type(exc).__name__}).encode(), "json")
            return
        _finish(jid, status, raw, ctype)

    try:
        ctx.run(asyncio.run, main())
    finally:
        _SLOTS.release()


class BackgroundCallMiddleware:
    """Pure ASGI. Sits innermost (just outside the router), so every outer guard (body caps,
    CSRF/auth audit, feature gate, AI scope, capacity) has already run on the request."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope.get("type") != "http":
            return await self.app(scope, receive, send)
        method = scope.get("method", "")
        path = route_path(scope)
        if not covered(method, path):
            return await self.app(scope, receive, send)
        request = Request(scope)
        is_async = wants_async(request.headers)
        raw_key = request.headers.get("idempotency-key")
        if not is_async and raw_key is None:
            return await self.app(scope, receive, send)  # the untouched default
        from app.web import deps

        ident = deps.api_identity(request)
        if not ident or ident.get("kind") != "user":
            # No per-user caller to own a job: the route answers (401/403) as it always did.
            return await self.app(scope, receive, send)
        if raw_key is not None and not _KEY_RE.fullmatch(raw_key):
            return await _error(
                400,
                "idempotency_key_invalid",
                "Idempotency-Key must be 1 to 255 visible ASCII characters.",
            )(scope, receive, send)
        chunks = []
        while True:
            message = await receive()
            if message["type"] != "http.request":
                return None  # the client left before the body arrived: nothing ran
            chunks.append(message.get("body") or b"")
            if not message.get("more_body"):
                break
        body = b"".join(chunks)
        actor = str(ident.get("actor") or "")
        # The caller as the route sees it: the account, its org, and (embedded trust mode
        # only) the person it acts for, so two people behind one trust principal never
        # share a job or a kept answer.
        caller = (actor, ident.get("org_id"), ident.get("on_behalf_of") or "")
        label = f"POST {path}"
        query = scope.get("query_string") or b""
        fingerprint = _sha(*caller, method, path, query, _normalized(request.headers, body))
        # Per caller AND per route: one key may cover a tool that POSTs twice (an upload,
        # then its translate), and a key can never reach another account's answers.
        idem = _sha("idem", *caller, path, raw_key) if raw_key is not None else None
        with contextlib.suppress(Exception):
            trim_old_results()
        with _LOCK:
            job, why = _find(actor, idem, fingerprint, is_async)
            if job is None:
                if is_async and not _SLOTS.acquire(blocking=False):
                    response = _error(
                        503,
                        "background_busy",
                        f"{MAX_BACKGROUND} background calls are already running. Retry "
                        "shortly; nothing was started.",
                    )
                    response.headers["Retry-After"] = "30"
                    return await response(scope, receive, send)
                jid = _insert(actor, label, fingerprint, idem, is_async)
        if job is not None:
            if why == "idem" and (job.payload or {}).get("fp") != fingerprint:
                response = _error(
                    422,
                    "idempotency_key_reused",
                    "This Idempotency-Key was already used for a different request in the "
                    "last 24 hours. Use a new key for a new request.",
                    job_id=job.id,
                )
            elif job.status == JobStatus.RUNNING:
                if is_async:
                    response = _accepted(job.id, existing=True)
                else:
                    response = _error(
                        409,
                        "idempotency_in_progress",
                        f"The first request with this Idempotency-Key is still running as job "
                        f"{job.id}. Check GET /api/jobs/{job.id} instead of sending it again.",
                        job_id=job.id,
                        status_url=f"/api/jobs/{job.id}",
                    )
            else:
                response = _replay(job)
            return await response(scope, receive, send)
        run_scope = dict(scope)
        if is_async:
            ctx = contextvars.copy_context()
            try:
                threading.Thread(
                    target=_background,
                    args=(self.app, run_scope, body, jid, ctx),
                    name=f"loc-bgcall-{jid}",
                    daemon=True,
                ).start()
            except Exception:
                _SLOTS.release()
                _finish(jid, 503, b'{"error": "could not start the background call"}', "json")
                raise
            return await _accepted(jid, existing=False)(scope, receive, send)
        # Synchronous call with an Idempotency-Key: run it here, keep the answer, send it.
        try:
            status, ctype, raw = await _run(self.app, run_scope, body)
        except Exception:
            _finish(jid, 500, b'{"error": "internal error"}', "json")
            raise
        _finish(jid, status, raw, ctype)
        headers = {"X-Loc-Job-Id": str(jid)}
        if ctype:
            headers["Content-Type"] = ctype
        return await Response(raw, status_code=status, headers=headers)(scope, receive, send)


def trim_old_results(force: bool = False) -> int:
    """Drop the response body of api_task rows finished more than RESULT_KEEP ago (at most
    once an hour unless forced). The row, its status and http_status stay. Returns the count."""
    import time

    if not force and time.monotonic() - _last_trim[0] < _TRIM_EVERY_S:
        return 0
    _last_trim[0] = time.monotonic()
    cutoff = datetime.utcnow() - RESULT_KEEP
    n = 0
    with Session(engine) as s:
        for job in s.exec(
            select(Job).where(
                Job.kind == KIND, Job.status != JobStatus.RUNNING, Job.finished_at < cutoff
            )
        ).all():
            res = job.result or {}
            if "response" not in res and "response_text" not in res:
                continue
            job.result = {"http_status": res.get("http_status"), "response_expired": True}
            s.add(job)
            n += 1
        if n:
            s.commit()
    return n


def fail_interrupted() -> int:
    """Boot only: fail every api_task row a restart cut off (no process can still run it)."""
    n = 0
    now = datetime.utcnow()
    with Session(engine) as s:
        for job in s.exec(
            select(Job).where(Job.kind == KIND, Job.status == JobStatus.RUNNING)
        ).all():
            job.status = JobStatus.FAILED
            job.error = (
                "Interrupted by a server restart. The work may have partly run: check the "
                "connection, run or document before sending it again."
            )
            job.finished_at = now
            payload = dict(job.payload or {})
            payload["idem"] = None
            job.payload = payload
            s.add(job)
            n += 1
        if n:
            s.commit()
    return n
