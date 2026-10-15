"""Launch-then-poll for the slow MCP tools, idempotency keys, and the job_status result view.

#728 / #743 / #744 / #745 / #747. Imported only by server.py. It imports nothing from server.py
(server.py passes its own `_call`, error classes and status explainer in), so it can never
be the second copy of server.py that `python server.py` + `import server` would load.

THE PATTERN (copied from run_workflow): a slow spend/write tool POSTs with
`Prefer: respond-async` (or the connector sync route's own `{"async": true}`), so the install
answers 202 with a job id within a second. The tool then polls GET /api/jobs/{id} inside one
bounded budget (WAIT_S, the run_workflow figure). Finished inside the budget: the tool returns
the route's own answer, exactly as the synchronous call used to. Still running: it returns
`{"status": "running", "finished": false, "job_id": N, "next": ...}` and the agent polls
job_status. The work never depends on the tool call staying open, so a client timeout or
cancel no longer reads as a failure while the server keeps spending.

An install that predates async mode answers the POST synchronously (200 with the finished
body, no job id): that answer is returned as is.
"""

from __future__ import annotations

import asyncio
import contextvars
import re
from typing import Any

import httpx

#: Total budget per tool call across launch and polling (run_workflow's _WORKFLOW_WAIT_S).
WAIT_S = 45.0
#: Gap between job polls.
GAP_S = 2.5
#: Launch timeout: the install answers a background launch at once, so a slow answer here is
#: a busy install, not a long job.
LAUNCH_TIMEOUT = httpx.Timeout(30.0, connect=10.0)

#: The idempotency key of the spend call in flight. Set by server._dispatch for a confirmed
#: spend tool (reset after), read by server._headers, so EVERY POST the tool makes carries it.
#: The install scopes a key per caller AND per route, so one key covers a tool that makes
#: several POSTs (submit_translation uploads, then translates).
IDEMPOTENCY_KEY: contextvars.ContextVar = contextvars.ContextVar("loc_mcp_idem", default=None)
_KEY_RE = re.compile(r"[\x21-\x7e]{1,255}")

TERMINAL = frozenset({"completed", "failed", "cancelled"})

#: job_status result caps: list fields keep this many entries, strings this many characters.
LIST_CAP = 20
TEXT_CAP = 500
_CTRL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


def check_key(key) -> str | None:
    """The key to send, or None. Raises ValueError on a malformed one (the caller wraps it)."""
    if key is None:
        return None
    text = str(key)
    if not _KEY_RE.fullmatch(text):
        raise ValueError(
            "idempotency_key must be 1 to 255 visible ASCII characters (no spaces); "
            "nothing was sent."
        )
    return text


def running_next(job_id: int) -> str:
    return (
        f"The work is still running on the server as job {job_id}. Call job_status with "
        f"job_id={job_id} (it waits up to 45 s per call) until finished is true. Do NOT call "
        "this tool again to check: that could start the work twice."
    )


def _clean(value, depth: int = 0):
    """Cap lists and strings and strip control characters, recursively (external text from a
    connected tenant can be long or hostile; the agent only needs a bounded view)."""
    if depth > 6:
        return None
    if isinstance(value, str):
        text = _CTRL.sub("", value)
        return text if len(text) <= TEXT_CAP else text[:TEXT_CAP] + "...[truncated]"
    if isinstance(value, list):
        out = [_clean(v, depth + 1) for v in value[:LIST_CAP]]
        if len(value) > LIST_CAP:
            out.append(f"...[{len(value) - LIST_CAP} more not shown]")
        return out
    if isinstance(value, dict):
        out = {}
        for i, (k, v) in enumerate(value.items()):
            if i >= 60:
                out["_truncated_keys"] = len(value) - 60
                break
            out[_clean(str(k), depth + 1)] = _clean(v, depth + 1)
        return out
    return value


def job_result(job: dict) -> tuple[Any, int | None]:
    """(result view, route http status or None) for a job read from GET /api/jobs/{id}.

    A background /api call (kind api_task) stores {http_status, response}: the view is the
    route's own response. Any other job kind (connector_translate, translate_document, ...)
    stores its summary directly."""
    res = job.get("result") or {}
    if job.get("kind") == "api_task" and isinstance(res, dict) and "http_status" in res:
        status = int(res.get("http_status") or 0)
        if "response" in res:
            body = res["response"]
        elif "response_text" in res:
            body = {"text": res["response_text"]}
        else:
            body = {"note": "the answer was too large to keep; read the work's own status"}
        return body, status
    return res, None


def status_view(job: dict) -> dict:
    """The job_status answer for one job: state, progress, and for a FINISHED job its result
    (#743): counts, created/updated, held back, failures, message, all capped."""
    state = str(job.get("status") or "").lower()
    out = {
        "job_id": job.get("id"),
        "kind": job.get("kind"),
        "status": job.get("status"),
        "done": job.get("done"),
        "total": job.get("total"),
        "message": _clean(job.get("message")),
        "error": _clean(job.get("error")),
        "finished": state in TERMINAL,
    }
    if state in TERMINAL:
        body, http_status = job_result(job)
        if isinstance(body, dict):
            body = {k: v for k, v in body.items() if k != "ok" or http_status is None}
        out["result"] = _clean(body)
        if http_status is not None:
            out["http_status"] = http_status
    return out


async def launch_and_wait(
    call,
    client: httpx.AsyncClient,
    path: str,
    body: dict,
    *,
    explain,
    http_error,
    native_async: bool = False,
    wait_s: float | None = None,
    launch_timeout: httpx.Timeout | None = None,
) -> dict:
    """POST `path` as a background call and wait (bounded) for the result.

    `call` is server._call, `explain` server._explain_status and `http_error` server's
    _HttpToolError: a finished job whose route answered 4xx/5xx raises exactly the error the
    synchronous call would have raised. Returns the route's answer when it finished inside
    the budget, else the running handle."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + (WAIT_S if wait_s is None else wait_s)
    send = dict(body)
    if native_async:
        # The route's own async mode (deduplicated per connection against EVERY run, the
        # scheduler's and webhooks' included). No Prefer header: wrapping that 202 in a
        # second background call would hand back a job whose result is just another job id.
        send["async"] = True
        headers: dict = {}
    else:
        headers = {"Prefer": "respond-async"}
    resp = await call(
        client,
        "POST",
        path,
        json_body=send,
        headers=headers,
        timeout=launch_timeout or LAUNCH_TIMEOUT,
    )
    try:
        launched = resp.json()
    except ValueError:
        launched = {}
    # Only a real 202 is a launch (a response without a status code, e.g. a scripted test
    # double, is read as the finished synchronous answer, as before).
    accepted = getattr(resp, "status_code", 200) == 202
    if not accepted or not isinstance(launched, dict) or "job_id" not in launched:
        # Finished synchronously (an older install, or an idempotent replay of a finished call).
        out = launched if isinstance(launched, dict) else {"result": launched}
        out.setdefault("finished", True)
        return out
    job_id = int(launched["job_id"])
    existing = bool(launched.get("existing"))
    job: dict = {}
    hops = 0
    while True:
        r = await call(client, "GET", "/api/jobs/%d" % job_id)  # noqa: UP031
        job = r.json()
        if str(job.get("status") or "").lower() in TERMINAL:
            inner, inner_status = job_result(job)
            if (
                inner_status == 202
                and isinstance(inner, dict)
                and isinstance(inner.get("job_id"), int)
                and hops < 1
            ):
                # The route itself answered "queued as job M" (a connector retry): follow M.
                job_id, hops = int(inner["job_id"]), hops + 1
                continue
            break
        remaining = deadline - loop.time()
        if remaining <= 0:
            out = {
                "status": "running",
                "finished": False,
                "job_id": job_id,
                "message": job.get("message"),
                "next": running_next(job_id),
            }
            if existing:
                out["existing"] = True
                out["note"] = (
                    "An identical call was already running; this is that job, nothing new "
                    "was started."
                )
            return out
        await asyncio.sleep(min(GAP_S, max(0.0, remaining)))
    result, http_status = job_result(job)
    if http_status is not None and http_status >= 400:
        err = http_error(
            explain(httpx.Response(http_status, json=result if isinstance(result, dict) else {}))
            + f" (background job {job_id})"
        )
        err.status_code = http_status
        raise err
    out = dict(result) if isinstance(result, dict) else {"result": result}
    if http_status is None:
        # A native job (connector sync): its status is the verdict, its result the summary.
        out.setdefault("status", job.get("status"))
        if str(job.get("status") or "").lower() != "completed" and job.get("error"):
            out.setdefault("error", job.get("error"))
    out["job_id"] = job_id
    out["finished"] = True
    if existing:
        out["existing"] = True
    return out
