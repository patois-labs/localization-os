"""Background drafting for the web authoring routes (Describe a workflow, Fix this for me,
the step prompt assistant). The new-project "Describe it" box, the /api door and the remote
CLI are a follow-up (the /api door already takes ``Prefer: respond-async``).

Documented in: docs/architecture/surfaces-and-parity.md

Why: with a 27B local model at about 7 tokens a second, "Describe a workflow" took 194 to
222 s per draft. A proxy such as Cloudflare cuts a browser request off at 100 s (524), so
the person got a blank error page, clicked again, and started a second and a third paid
draft while the first was still running.

THE PATTERN. The route checks its gates as before, then hands the drafting call to
``start``: it runs on a worker thread with its own DB session, on an ``api_task`` Job row
(the same bookkeeping row the /api ``Prefer: respond-async`` calls use, so a restart fails it
through ``background_calls.fail_interrupted`` and GET /api/jobs/{id} can read it). The route
then waits up to ``INLINE_WAIT_S`` for the result. A fast engine finishes inside that wait and
the response is exactly what it was before. A slow one gets a handle instead: the HTML route
redirects to GET /workflows/draft-jobs/{id} (a progress page that polls, then renders the finished
review in place); a JSON route answers ``{"ok": true, "pending": true, "job_id": N,
"poll_url": "/workflows/draft-jobs/N.json"}`` and its page polls that URL.

Repeat clicks are a no-op: while a person has a draft of one kind running, another POST of
that kind joins the running job (``existing``) instead of starting a second engine call.

The job row carries no project_id / document_id, so reports and budget meters never count it;
the drafting call's spend lands on the ledger rows the service itself writes, as before.
"""

from __future__ import annotations

import contextvars
import hashlib
import json
import logging
import threading
import time
from collections.abc import Callable
from datetime import datetime
from typing import Any

from sqlmodel import Session, select

from app.db import engine
from app.models import Job, JobStatus
from app.web import background_calls

#: How long a POST waits for its draft before handing back a handle. Well under the 100 s
#: Cloudflare proxy cut-off, long enough that a cloud model's draft returns in one response.
INLINE_WAIT_S = 25.0
#: The progress page stops polling and shows the timeout message after this long.
GIVE_UP_S = 15 * 60
#: The kinds of draft. One running draft per person per kind.
KINDS = {
    "workflow_author": "POST /workflows/author",
    "workflow_repair": "POST /workflows/author/repair",
    "step_prompt": "POST /workflows/{id}/author",
}

_LOCK = threading.Lock()
#: In-process completion signals, so a waiting request wakes the moment its draft is ready.
_DONE: dict[int, threading.Event] = {}
#: Jobs whose worker thread is alive in THIS process (production runs one process). A RUNNING
#: row not in here has no worker (its thread died without recording an outcome): never join it.
_LIVE: set[int] = set()
_log = logging.getLogger(__name__)


class Busy(Exception):
    """Every background slot is taken; nothing was started."""

    message = (
        "Too many AI drafts are running on this server right now. Nothing was started; "
        "try again in a minute."
    )


def _fingerprint(*parts: Any) -> str:
    h = hashlib.sha256()
    for p in parts:
        h.update(str(p).encode("utf-8", "surrogatepass"))
        h.update(b"\x00")
    return h.hexdigest()


def _running_for(actor: str, org_id, kind: str) -> Job | None:
    with Session(engine) as s:
        rows = s.exec(
            select(Job)
            .where(
                Job.kind == background_calls.KIND,
                Job.created_by == actor,
                Job.status == JobStatus.RUNNING,
            )
            .order_by(Job.id.desc())
        ).all()
    for job in rows:
        p = job.payload or {}
        if p.get("author_kind") == kind and p.get("org_id") == org_id:
            if elapsed_s(job) >= GIVE_UP_S or job.id not in _LIVE:
                # Past the point where its page gave up (the person was told to try a faster
                # model), or no worker runs it: never trap a new click on it.
                continue
            return job
    return None


def _jsonable(o):
    if isinstance(o, (set, frozenset, tuple)):
        return list(o)
    return str(o)


def _finish(jid: int, status: int, response: dict) -> None:
    # Stored as JSON on the row; the waiting request and the progress page both render
    # from this one stored copy, so a fast and a slow draft render identically.
    response = json.loads(json.dumps(response, default=_jsonable))
    error = ""
    if status >= 400:
        error = str(response.get("error") or f"HTTP {status}")[:500]
    with Session(engine) as s:
        job = s.get(Job, jid)
        if job is not None:
            job.status = JobStatus.COMPLETED if status < 400 else JobStatus.FAILED
            job.finished_at = datetime.utcnow()
            job.progress_done = 1
            job.message = f"HTTP {status}"
            job.error = error
            job.result = {"http_status": status, "response": response}
            s.add(job)
            s.commit()
    ev = _DONE.pop(jid, None)
    if ev is not None:
        ev.set()


def _body(jid: int, work: Callable[[Session], tuple[int, dict]], ctx) -> None:
    try:
        try:
            with Session(engine) as s:
                status, response = ctx.run(work, s)
        except BaseException as exc:  # never lose the row: log it, record a clean failure
            _log.exception("author job %s: the draft crashed", jid)
            status, response = (
                500,
                {
                    "ok": False,
                    "error": f"The draft failed unexpectedly ({type(exc).__name__}). Try again.",
                },
            )
        try:
            _finish(jid, status, response)
        except Exception:  # an unstorable result must not leave the row RUNNING
            _log.exception("author job %s: could not store the result", jid)
            try:
                _finish(
                    jid, 500, {"ok": False, "error": "The draft could not be stored. Try again."}
                )
            except Exception:
                _log.exception("author job %s: could not record the failure", jid)
    finally:
        _LIVE.discard(jid)
        ev = _DONE.pop(jid, None)
        if ev is not None:
            ev.set()  # wake any waiter; it then reads the row
        background_calls._SLOTS.release()


def start(
    kind: str,
    *,
    actor: str,
    org_id,
    params: dict,
    work: Callable[[Session], tuple[int, dict]],
) -> tuple[int, bool, bool]:
    """Start (or join) this person's draft of ``kind``. Returns (job_id, existing, same).
    A running draft of the same kind is joined (``existing``); the caller decides what a
    join with DIFFERENT parameters (``same`` False) means: the JSON routes refuse it (409)
    so another request's draft never lands in this form, the HTML routes show that draft's
    page with a notice. A draft older than GIVE_UP_S is never joined.

    ``work(session)`` runs on the worker thread and returns (http_status, response dict).
    ``params`` (the description, provider, model, ...) are fingerprinted so a joined click
    can tell whether it asked for the same thing (``same``). Raises Busy when no slot is
    free."""
    fp = _fingerprint(kind, actor, org_id, *sorted((k, str(v)) for k, v in params.items()))
    with _LOCK:
        job = _running_for(actor, org_id, kind)
        if job is not None:
            return job.id, True, (job.payload or {}).get("fp") == fp
        if not background_calls._SLOTS.acquire(blocking=False):
            raise Busy()
        now = datetime.utcnow()
        jid = None
        try:
            with Session(engine) as s:
                row = Job(
                    kind=background_calls.KIND,
                    payload={
                        "route": KINDS[kind],
                        "author_kind": kind,
                        "org_id": org_id,
                        "fp": fp,
                        "idem": None,
                        "async": False,
                        "params": params,
                    },
                    created_by=actor,
                    status=JobStatus.RUNNING,
                    started_at=now,
                    progress_done=0,
                    progress_total=1,
                    message="Drafting",
                )
                s.add(row)
                s.commit()
                s.refresh(row)
                jid = row.id
            _DONE[jid] = threading.Event()
            _LIVE.add(jid)
            threading.Thread(
                target=_body,
                args=(jid, work, contextvars.copy_context()),
                name=f"loc-author-{jid}",
                daemon=True,
            ).start()
        except Exception:
            background_calls._SLOTS.release()
            if jid is not None:
                _LIVE.discard(jid)  # the row exists but no worker runs it: never leave it RUNNING
                _finish(jid, 503, {"ok": False, "error": "The draft could not be started."})
            raise
    return jid, False, True


def get(jid: int, *, actor: str, org_id) -> Job | None:
    """This person's author job, or None (missing and foreign look the same)."""
    with Session(engine) as s:
        job = s.get(Job, jid)
    if job is None or job.kind != background_calls.KIND:
        return None
    p = job.payload or {}
    if not p.get("author_kind") or job.created_by != actor or p.get("org_id") != org_id:
        return None
    return job


def wait(jid: int, timeout: float) -> Job | None:
    """Block up to ``timeout`` seconds; the finished job, or None while it still runs."""
    ev = _DONE.get(jid)
    if ev is not None:
        ev.wait(timeout)
    deadline = time.monotonic() + (0 if ev is not None else timeout)
    while True:
        with Session(engine) as s:
            job = s.get(Job, jid)
        if job is None or job.status != JobStatus.RUNNING:
            return job
        if time.monotonic() >= deadline:
            return None
        time.sleep(0.5)


def peek(jid: int) -> Job | None:
    """One read: the finished job, or None while it still runs (async routes poll this
    through deps.offload_blocking, so no worker is held for the whole inline wait)."""
    with Session(engine) as s:
        job = s.get(Job, jid)
    if job is None or job.status != JobStatus.RUNNING:
        return job
    return None


def response_of(job: Job) -> tuple[int, dict]:
    res = job.result or {}
    body = res.get("response")
    if not isinstance(body, dict):
        if job.error:
            return 500, {"ok": False, "error": job.error}
        return 500, {"ok": False, "error": "The draft result is no longer available."}
    return int(res.get("http_status") or 200), body


def elapsed_s(job: Job) -> int:
    start_at = job.started_at or job.created_at or datetime.utcnow()
    end = job.finished_at or datetime.utcnow()
    return max(0, int((end - start_at).total_seconds()))


def poll_body(job: Job) -> dict:
    """The JSON a page polls: running, or the finished route answer plus its status."""
    if job.status == JobStatus.RUNNING:
        secs = elapsed_s(job)
        return {
            "ok": True,
            "pending": True,
            "status": "running",
            "job_id": job.id,
            "elapsed_s": secs,
            "timed_out": secs >= GIVE_UP_S,
        }
    status, body = response_of(job)
    out = dict(body)
    out.setdefault("ok", status < 400)
    out["status"] = "completed" if status < 400 else "failed"
    out["job_id"] = job.id
    out["http_status"] = status
    return out


def pending_body(jid: int, existing: bool, same: bool) -> dict:
    body = {
        "ok": True,
        "pending": True,
        "status": "running",
        "job_id": jid,
        "poll_url": f"/workflows/draft-jobs/{jid}.json",
    }
    if existing:
        body["existing"] = True
        body["notice"] = existing_notice(same)
    return body


def busy_other_body(jid: int) -> dict:
    """A JSON route's answer when the person already has a DIFFERENT draft of this kind
    running: nothing is started, and that draft is never handed back as this one's."""
    return {
        "ok": False,
        "code": "draft_in_progress",
        "error": "You already have an AI draft of this kind running. Wait for it to finish "
        "(it can take a few minutes on a local model), then try again.",
        "job_id": jid,
    }


def existing_notice(same: bool) -> str:
    if same:
        return "This draft was already running, so the click did not start another one."
    return (
        "You already have a draft of this kind running (from different text), so the "
        "click did not start another one. This page shows that draft."
    )
