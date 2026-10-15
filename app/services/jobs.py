"""Background job engine (Phase 1 of the enterprise roadmap).

Long-running work (document MT, connector runs) must never execute inside an
HTTP request handler — a 500-record sync would hold the request open for
minutes and die with the connection. Instead routes call `submit()` (instant:
writes a Job row, enqueues, returns the id) and the UI polls
GET /jobs/{id}/status.

Design notes, deliberately simple for a single-process deployment:
- Worker threads (daemon) pull job ids off a queue.Queue; handlers are plain
  functions registered in HANDLERS. Each handler opens its own DB sessions.
- Progress is written to the Job row (SQLite in WAL mode: readers don't block).
- Crash/restart safety: the in-memory queue dies with the process, so
  recover_interrupted() (called at startup) re-queues QUEUED rows and marks
  RUNNING rows failed — work is never silently lost, only visibly failed.
- Multi-instance deployments need a real broker (Redis/Postgres queue) — see
  ENTERPRISE_ROADMAP.md Phase 4.

Documented in: docs/architecture/jobs-and-scheduler.md
"""

from __future__ import annotations

import contextlib
import contextvars
import logging
import os
import socket
import threading
import time
import uuid
from datetime import datetime

from sqlmodel import Session, select

from app import config
from app.db import engine
from app.models import Job, JobStatus
from app.services.maintenance import accounted_work, admitted_work

log = logging.getLogger("loc_tms.jobs")

WORKER_COUNT = config.JOB_WORKERS
_INSTANCE_ID = f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:8]}"


def instance_id() -> str:
    """Identity of this process incarnation, shared by all its job workers."""
    return _INSTANCE_ID


class MissingPayloadField(ValueError):
    """A handler's payload is missing a field it requires (e.g. `translate_document`
    with no `document_id`). Raised explicitly at the point a required key is read,
    instead of leaning on the bare KeyError a `payload["x"]` lookup raises — that
    let a genuine handler bug (an IndexError/KeyError from broken logic, both
    LookupError subclasses) get silently swallowed as "missing field" too. _execute
    catches this one specifically for the clean-failure path; anything else falls
    through to the crash-logging branch."""


class JobFailed(RuntimeError):
    """A handler's controlled failure that still carries structured data worth
    keeping on the Job row. _execute stores BOTH error=str(e) AND result=.result,
    so a FAILED connector run can still expose its .failures for a targeted retry
    (a plain RuntimeError just sets error, as before)."""

    def __init__(self, message: str, result: dict | None = None):
        super().__init__(message)
        self.result: dict = result or {}


class ConnectorRunError(RuntimeError):
    """A connector run's controlled TOTAL failure with no data worth keeping (a dead
    connection, a bad connection id, bad creds, an unroutable webhook kind). Raised
    DELIBERATELY at the _run_connector_translate sites below so _execute can give it
    the clean-failure path (FAILED + a one-line error, no scary traceback) instead of
    treating it as an unexpected crash.

    Mirrors the MissingPayloadField philosophy (see _execute): a dedicated, explicitly
    raised type is caught for the clean path, so a genuine handler BUG (AttributeError,
    a real KeyError from broken logic) still falls through to log.exception and stays
    debuggable. A bare RuntimeError would have been indistinguishable from a crash."""


def _require(payload: dict, key: str):
    """payload[key], or a clean MissingPayloadField instead of a bare KeyError —
    the explicit guard the handlers below use for their required fields."""
    if key not in payload:
        raise MissingPayloadField(f"missing required field: {key!r}")
    return payload[key]


# The job DISPATCH seam is now the JobQueue port (app/ports/jobqueue.py, sprint Train S):
# enqueue / claim / release / recover route through jobqueue.get(), whose DEFAULT adapter
# (InMemoryJobQueue) wraps the SAME queue.Queue this module used to own -- so a default
# install (job_queue_backend="memory") is byte-identical to before. The opt-in "db" backend
# swaps in a durable adapter at boot without changing anything above this seam.
_started = False
_start_lock = threading.Lock()
# Serializes the check-then-insert in submit_unique so two callers can't both
# pass the "no active job for this connection" test and each enqueue one.
_submit_lock = threading.Lock()


# --- handlers ---
def all_failed_error(stats: dict | None) -> str:
    """The first engine error text when a translate run's summary shows at least one
    error and nothing landed (no engine translation, no TM hit), else "". One rule for
    every door (background job, live stream, CLI) so none reports a dead engine as done."""
    st = stats or {}
    landed = (
        int(st.get("translated", 0) or 0)
        + int(st.get("from_segment_tm", 0) or 0)
        + int(st.get("from_full_text_tm", 0) or 0)
    )
    if int(st.get("errors", 0) or 0) and not landed:
        return str(st.get("first_error") or "") or "every segment failed"
    return ""


def partial_failure_error(stats: dict | None, total: int | None = None) -> str:
    """#612: the job error text when a translate run landed SOME segments but at least one
    failed (an engine 502 window, a cut stream). "" when nothing failed, or when nothing
    landed (that is `all_failed_error`'s case). Names the failed count, the first engine
    error and the way out, so the Job row, API, CLI, MCP and web all say the same thing."""
    st = stats or {}
    errors = int(st.get("errors", 0) or 0)
    if not errors or all_failed_error(st):
        return ""
    landed = (
        int(st.get("translated", 0) or 0)
        + int(st.get("from_segment_tm", 0) or 0)
        + int(st.get("from_full_text_tm", 0) or 0)
    )
    of = f" of {total}" if total else ""
    first = str(st.get("first_error") or "").strip()
    why = f" (first engine error: {first})" if first else ""
    return (
        f"Partially translated: {errors}{of} segment(s) failed{why}; the other {landed} "
        "were saved. Retry failed segments by running translate again: it picks up only "
        "the untranslated segments."
    )[:500]


def run_error(stats: dict | None, total: int | None = None) -> str:
    """One rule for every door: the error text of a translate run that did not fully
    succeed (all failed, or partly failed), else ""."""
    return all_failed_error(stats) or partial_failure_error(stats, total)


def last_failed_translate_run(doc_id: int, locale: str, *, scan: int = 400) -> dict | None:
    """#612: the newest `translate_document` job for (document, locale) when it ended
    FAILED carrying failed segments (`result.errors` > 0), as {"job_id", "errors",
    "first_error"}; None when the newest such job succeeded, never failed that way, or
    there is none. The export hold reads this: a run that is still queued or running is
    SKIPPED (gate: a re-run parked at an engine cap, or an abandoned live-stream reservation
    left RUNNING, must not release the hold before the missing units are filled; there is
    no in-flight export gate), and any newer FINISHED clean run supersedes an older failed
    one. Bounded scan of the `scan` newest translate jobs."""
    with Session(engine) as s:
        rows = s.exec(
            select(Job.id, Job.status, Job.payload, Job.result)
            .where(Job.kind == "translate_document")
            .order_by(Job.id.desc())
            .limit(scan)
        ).all()
    for jid, status, payload, result in rows:
        p = payload or {}
        try:
            same = int(p.get("document_id")) == int(doc_id) and p.get("locale") == locale
        except (TypeError, ValueError):
            same = False
        if not same or status in (JobStatus.QUEUED, JobStatus.RUNNING):
            continue
        if status != JobStatus.FAILED:
            return None
        errors = int((result or {}).get("errors", 0) or 0)
        if not errors:
            return None
        return {
            "job_id": jid,
            "errors": errors,
            "first_error": str((result or {}).get("first_error") or ""),
        }
    return None


# --- per-engine translate-job concurrency (Issue #613) ---
# The worker pool (WORKER_COUNT = LOC_TMS_JOB_WORKERS, default 2) is the GLOBAL cap on jobs of
# every kind. An engine profile may add a per-engine limit (config.max_parallel_jobs): at most
# that many translate jobs for that engine run at once. The limit can only lower concurrency
# beneath the worker cap (min(limit, WORKER_COUNT)); an engine with no setting takes no slot
# and behaves exactly as before. A queue worker that claims a job whose engine is at its limit
# does NOT wait: the job goes back to QUEUED behind the rest of the queue (JobDeferred) and the
# worker takes the next job, so a capped engine's backlog can never pin every worker and starve
# other engines or other job kinds. A one-shot caller (CLI `job run`) has no queue and waits.
_engine_slots_cv = threading.Condition()
_engine_slots_busy: dict[str, int] = {}
_CURRENT_JOB: contextvars.ContextVar[int | None] = contextvars.ContextVar(
    "loc_current_job", default=None
)


def current_job_id() -> int | None:
    """The id of the job whose handler is running in this context (None outside a job)."""
    return _CURRENT_JOB.get()


_worker_ctx = threading.local()  # .deferrable: this thread is a queue worker that may requeue


_DEFER_PAUSE_S = 1.0
_DEFER_MESSAGE = "Waiting for a free slot on this engine"


class JobDeferred(Exception):
    """A queue worker found the job's engine at its max_parallel_jobs cap. The job goes back
    to QUEUED and behind the rest of the queue, so the worker is free for other jobs (a
    capped engine's backlog never pins every worker). Never reaches a caller."""


def _translate_job_engine(doc_id, payload: dict) -> str:
    """The engine a translate job will call: the payload's pinned provider, else the project
    default. "" when it cannot be resolved (then no per-engine limit applies)."""
    provider = str(payload.get("provider") or "").strip()
    if provider:
        return provider.lower()
    try:
        from app.models import Document, Project

        with Session(engine) as s:
            doc = s.get(Document, doc_id)
            project = s.get(Project, doc.project_id) if doc else None
            return str(getattr(project, "default_provider", "") or "").strip().lower()
    except Exception:
        return ""


class _EngineJobSlot:
    """Context manager: hold one of an engine's ``max_parallel_jobs`` slots for a job."""

    def __init__(self, provider: str):
        self.key = ""
        self.limit = 0
        if provider:
            from app.services import engines

            raw = engines.max_parallel_jobs_for(provider)
            if raw:
                self.key = provider
                self.limit = max(1, min(raw, WORKER_COUNT))

    def __enter__(self):
        if not self.key:
            return self
        with _engine_slots_cv:
            while _engine_slots_busy.get(self.key, 0) >= self.limit:
                if getattr(_worker_ctx, "deferrable", False):
                    raise JobDeferred(self.key)
                _engine_slots_cv.wait()  # one-shot callers (CLI run) have no queue to return to
            _engine_slots_busy[self.key] = _engine_slots_busy.get(self.key, 0) + 1
        return self

    def __exit__(self, *exc):
        if self.key:
            with _engine_slots_cv:
                n = _engine_slots_busy.get(self.key, 1) - 1
                if n > 0:
                    _engine_slots_busy[self.key] = n
                else:
                    _engine_slots_busy.pop(self.key, None)
                _engine_slots_cv.notify_all()
        return False


def _engine_at_cap(payload: dict) -> str:
    """The engine key when a translate job's engine has no free max_parallel_jobs slot right
    now, else "". A look, not a reservation: the handler's _EngineJobSlot is the authority."""
    try:
        slot = _EngineJobSlot(_translate_job_engine(payload.get("document_id"), payload))
    except Exception:
        return ""  # never lose a job over the look-ahead; the handler decides
    if not slot.key:
        return ""
    with _engine_slots_cv:
        return slot.key if _engine_slots_busy.get(slot.key, 0) >= slot.limit else ""


def _run_translate_document(payload: dict, progress) -> dict:
    """MT-pretranslate one document/locale (TM-first, glossary-aware, parallel
    provider calls). payload: {document_id, locale, [provider]}.

    PII: this path scrubs source only when the global pii_redaction setting is on
    (translate.pretranslate_stream's default). The API no longer FORCES redaction
    here (behavior change 2026-07-03) — PII is a per-step choice on the workflow
    path (WorkflowStep.redact_pii) and the global setting is the blanket fallback.

    AN INTERNAL KEY DOCUMENT IS NOT FOUND HERE (SPEC-key-based-projects §2.5, fix
    round 2 W1(c)). This handler is the THIRD translate launcher that resolves a
    document id itself, and both its surfaces reached key internals through it:
    `POST /api/jobs/run` with a plain DOCS-scoped token (the `keys` scope exists
    precisely so those are separate grants — A-F3), and `job run translate_document`
    on the CLI. The refusal reuses `translate.DocumentNotFound` so the answer is
    byte-identical to a genuinely missing id — non-enumerable, matching
    `deps._visible_doc` / `api_cli._visible_document`."""
    from app.models import Document, Segment
    from app.services import translate

    doc_id, locale = _require(payload, "document_id"), _require(payload, "locale")
    from app.services import ai_optout

    with Session(engine) as s:
        _doc = s.get(Document, doc_id)
        if _doc is None or _doc.fmt == "keys":
            raise translate.DocumentNotFound(f"no such document: {doc_id}")
        # AI opt-out (DPAI-08): a job queued before the switch was flipped still refuses.
        ai_optout.refuse_for_document(s, _doc, "machine translation")
    # provider: an effective provider stamped by the API when a routing rule (or an
    # explicit caller override) wins over the project default (Finding 1). Absent =>
    # the project default (spec_for_project inside pretranslate_stream).
    provider = str(payload.get("provider") or "").strip()
    spec = translate.spec_for_provider(provider) if provider else None
    with Session(engine) as s:
        total = len(
            s.exec(
                select(Segment.id).where(
                    Segment.document_id == doc_id, Segment.target_locale == locale
                )
            ).all()
        )
    if total == 0:
        raise JobFailed(
            "No segments for this document and locale. Add the target locale and seed "
            "its segments before running translation."
        )
    done, stats = 0, {}
    from app.services import segment_review

    with _EngineJobSlot(_translate_job_engine(doc_id, payload)), Session(engine) as s:
        # #617 item 4 (gate): a job queued before a run paused on human review fails cleanly.
        try:
            segment_review.refuse_if_awaiting_review(s, doc_id, locale)
        except segment_review.AwaitingReviewError as exc:
            raise JobFailed(str(exc)) from exc
        # #702: report the total as soon as the work starts (a document whose first segment
        # takes minutes, e.g. one chunked #683 unit, used to show 0/0 until it finished).
        progress(0, total, f"0/{total} segments")
        for ev in translate.pretranslate_stream(s, doc_id, locale, spec=spec):
            if ev.get("type") == "segment":
                done += 1
                progress(done, total, f"{done}/{total} segments")
            elif ev.get("type") == "summary":
                stats = {k: v for k, v in ev.items() if k != "type"}
    # Issue #572: a run with zero successes and at least one error is a FAILED job. It
    # used to land COMPLETED with `error: ""`, `translated: 0`, `errors: 2`, so an API
    # caller polling the job could not tell a dead engine from an empty document (the
    # production lane hit exactly this on a model that was not served). JobFailed keeps
    # the stats on the row and promotes the first engine error text to Job.error, which
    # every status surface (web progress, CLI, MCP, /api/jobs/{id}) already reads.
    dead = all_failed_error(stats)
    if dead:
        raise JobFailed(dead, result=stats)
    # Issue #612: a PARTIAL failure (some segments landed, some hit engine errors, e.g. an
    # MT restart's 502 window) is FAILED too, never plain "completed" with an empty error:
    # the stats stay on the row (result.errors / first_error), the error text names the
    # count and the first engine error, and the segments that did land stay saved, so
    # running translate again resumes exactly the missing ones.
    partial = partial_failure_error(stats, total)
    if partial:
        failed = int(stats.get("errors", 0) or 0)
        progress(
            done, total, f"{max(total - failed, 0)}/{total} segments translated, {failed} failed"
        )
        raise JobFailed(partial, result=stats)
    # P1: auto-complete the project when this finish leaves every locale of every
    # document fully translated. Additive + idempotent; a partial multi-locale
    # project is left active. Best-effort — a lifecycle hiccup must not fail the job.
    try:
        from app.models import Document
        from app.services import lifecycle

        with Session(engine) as s:
            doc = s.get(Document, doc_id)
            if doc is not None:
                lifecycle.mark_completed_if_done(s, doc.project_id, actor="system")
    except Exception:  # noqa: S110 -- project auto-complete is cosmetic; the job succeeded
        pass
    return stats


def _connection_ai_opted_out(cid) -> str | None:
    """The name of the project (or customer) that switched AI off for this connection's
    runs, else None. Checks the sync project AND the connection's customer, so a first
    sync (no project yet) of an opted-out customer is skipped too. Fails CLOSED: an
    unreadable state reports the connection as opted out."""
    from app.models import Connection, Customer
    from app.services import ai_optout

    try:
        with Session(engine) as s:
            conn = s.get(Connection, cid)
            if conn is None:
                return None
            if conn.project_id and ai_optout.is_opted_out(s, conn.project_id):
                from app.models import Project

                proj = s.get(Project, conn.project_id)
                return proj.name if proj is not None else f"project {conn.project_id}"
            cust_id = getattr(conn, "customer_id", None)
            if cust_id:
                cust = s.get(Customer, cust_id)
                if cust is not None and bool(getattr(cust, "ai_opt_out", False)):
                    return cust.name
    except Exception:
        log.warning("AI opt-out state unreadable for connection %s; skipping", cid, exc_info=True)
        return f"connection {cid}"
    return None


def _run_connector_translate(payload: dict, progress) -> dict:
    """The `connector_translate` handler. A job tagged with `automation_id` (a project
    automation run: scheduled, manual or full pull) goes through
    project_automation.execute_job, which applies the run cap, the full-pull / claim switches
    and the lane credential sync around the ordinary run below and settles the automation's
    bookkeeping afterwards. Every other job is the ordinary run, byte-identical to before."""
    if (payload or {}).get("automation_id") is not None:
        from app.services import project_automation

        return project_automation.execute_job(payload, progress, _run_connector_translate_core)
    return _run_connector_translate_core(payload, progress)


def _run_connector_translate_core(payload: dict, progress) -> dict:
    """One-click connector 'Translate now', or a retry of just the failed items.

    payload: {connection_id, actor} (full run) — or, when retry_failures is
    present, {connection_id, actor, retry_failures: [...]} to re-run only the
    subset of items that failed in a prior run (store.retry_failed).

    Status semantics:
    - ok=True (with or without item failures) -> COMPLETED, result carries the
      full summary (including any result.failures) so the UI can still offer a
      targeted retry after a partial-but-mostly-successful run.
    - ok=False WITH a non-empty summary -> FAILED via JobFailed, which keeps BOTH
      error and the structured result (so .failures survive for a retry).
    - ok=False with an empty/None summary (dead connection, bad connection id, bad
      creds — no data worth keeping) -> ConnectorRunError, FAILED with just the error
      via _execute's CLEAN-failure path (no traceback), not an unexpected-crash dump."""
    from app.services.connectors import store as conn_store

    cid = _require(payload, "connection_id")
    from app.services import access

    conn = conn_store.get(cid)
    if conn is not None and not access.org_active(conn.org_id):
        msg = "Sync skipped: the connection's organization is suspended or unavailable."
        progress(1, 1, msg)
        return {"ok": True, "message": msg, "skipped_inactive_org": True}
    actor = payload.get("actor", "")
    # Archive gate (residual sweep M6): SKIP any connector run (scheduled sync / webhook /
    # ping-pull / jira-pending / retry / manual) whose SYNC PROJECT is archived -- no paid
    # MT or external writes into a retired project. Clean no-op + logged connection status,
    # never a failure. (translate_now guards the CLI/direct path too.) NOTE: inbox ROUTING
    # still lands inbound mail in an archived inbox project -- archiving does not stop
    # delivery (documented twin-creation hazard); this only stops the paid TRANSLATE step.
    _arch_name = conn_store.connection_project_archived(cid)
    # AI opt-out (DPAI-08): a connector run machine-translates, so a connection whose sync
    # project (or customer) opted out of AI is skipped the same clean way as an archived one.
    _ai_off = _connection_ai_opted_out(cid)
    if _ai_off is not None and _arch_name is None:
        from app.models import ConnStatus

        msg = (
            f"Sync skipped: AI is switched off for '{_ai_off}', and connector runs use "
            f"machine translation. No content was sent to any AI engine."
        )
        try:
            conn_store._set_status(cid, ConnStatus.CONNECTED, msg, synced=False)
            conn_store._log(cid, "translate", True, 0, msg, actor)
        except Exception:  # noqa: S110 -- status/history write only; the skip is still reported
            pass
        progress(1, 1, msg)
        return {"message": msg, "ok": True, "created": 0, "updated": 0, "skipped_ai_opt_out": True}
    if _arch_name is not None:
        from app.models import ConnStatus

        msg = (
            f"Sync skipped: the connection's project '{_arch_name}' is archived "
            f"(unarchive it to resume translating)."
        )
        try:
            conn_store._set_status(cid, ConnStatus.CONNECTED, msg, synced=False)
            conn_store._log(cid, "translate", True, 0, msg, actor)
        except Exception:  # noqa: S110 -- status/history write only; the skip is still reported
            pass
        progress(1, 1, msg)
        return {"message": msg, "ok": True, "created": 0, "updated": 0, "skipped_archived": True}
    retry = payload.get("retry_failures")
    # Kind-generic changed-id list (webhook_record_ids); fall back to the legacy
    # zendesk-only alias so a job enqueued before the generalization still runs.
    webhook_ids = payload.get("webhook_record_ids") or payload.get("webhook_article_ids")
    # Ping-then-pull marker (Airtable): a bare ping carries NO ids and must re-fetch
    # payloads from the stored cursor rather than fall through to a full sweep.
    ping_pull = bool(payload.get("webhook_ping_pull"))
    if payload.get("jira_translate_pending"):
        # Jira Phase 2: release the connection's PENDING issue keys (the "Translate
        # pending now" action / CLI verb) — translate exactly those and clear them.
        progress(0, 1, "Translating pending Jira issue(s)…")
        ok, msg, summary = conn_store.jira_translate_pending(cid, actor=actor)
    elif retry:
        progress(0, 1, "Retrying failed items…")
        ok, msg, summary = conn_store.retry_failed(cid, retry, actor=actor)
    elif ping_pull:
        # Airtable webhook ping: pull payloads from the cursor, filter to source-
        # field changes, and (auto-on) translate or (auto-off) record pending.
        progress(0, 1, "Fetching Airtable webhook payloads…")
        ok, msg, summary = conn_store.consume_airtable_payloads(cid, actor=actor)
    elif webhook_ids:
        # Webhook-triggered targeted sync: translate ONLY the record ids the
        # (verified) event named. The connector re-fetches each record (thin
        # payloads are never trusted) before translating. Dispatch by connection
        # kind via an EXPLICIT mapping (F3, adversarial review — durable-shape fix):
        # the old chain ended `else: zendesk_translate`, so a verified webhook for
        # any kind NOT explicitly branched (e.g. Airtable — its verifier exists but
        # this branch isn't posture-gated for it yet) silently misrouted that kind's
        # record ids into zendesk_translate and errored instead of failing loud with
        # a clear cause. A kind with no entry here now fails the job outright with
        # a "no webhook dispatch for kind X" message — no accidental cross-kind call
        # — which also keeps the upcoming Airtable-webhook wiring a pure addition
        # (one new dict entry) instead of a chain-order edit.
        ids = [str(x) for x in webhook_ids]
        conn = conn_store.get(cid)
        kind = conn.kind if conn else ""

        def _confluence(cid, ids, actor):
            progress(0, 1, "Translating changed page(s)…")
            return conn_store.confluence_translate(cid, page_ids=ids, actor=actor)

        def _jira(cid, ids, actor):
            # Jira Phase 2: a verified push (Automation web request / raw webhook)
            # named specific issue keys. Translate ONLY those issues; jira_translate
            # re-checks its per-(issue,locale) content hash so an unchanged issue is
            # suppressed even here. Reaches this branch only when the connection
            # opted into auto_translate_on_change (else the intake records pending).
            progress(0, 1, "Translating changed issue(s)…")
            return conn_store.jira_translate(cid, issue_keys=ids, actor=actor)

        def _zendesk(cid, ids, actor):
            progress(0, 1, "Translating changed article(s)…")
            return conn_store.zendesk_translate(cid, article_ids=ids, actor=actor)

        _WEBHOOK_DISPATCH = {"confluence": _confluence, "jira": _jira, "zendesk": _zendesk}
        handler = _WEBHOOK_DISPATCH.get(kind)
        if handler is None:
            raise ConnectorRunError(f"no webhook dispatch for kind {kind!r}")
        ok, msg, summary = handler(cid, ids, actor)
    else:
        progress(0, 1, "Running connector translation…")
        ok, msg, summary = conn_store.translate_now(cid, actor=actor)
    progress(1, 1, msg)
    if ok:
        return {"message": msg, "ok": True, **(summary or {})}
    if summary:
        # Failed run that still produced structured data — mark FAILED but keep it.
        raise JobFailed(msg, {"message": msg, "ok": False, **summary})
    raise ConnectorRunError(msg)  # total failure -- no data worth keeping


def _run_data_export(payload, progress):
    """Full-object-graph export handler (Batch 13, Train A). Lazy-imports data_export so
    jobs.py has no module-scope dependency on the walker (which module-scope imports every
    model). The scope was RBAC-resolved under the caller at submit time; this reconstructs it
    from the payload and streams + stages the artifact (SPEC-data-export §5.2/§6)."""
    from app.services import data_export

    return data_export._execute_job(payload, progress)


HANDLERS = {
    "translate_document": _run_translate_document,
    "connector_translate": _run_connector_translate,
    "data_export": _run_data_export,
}


# --- engine ---
def _insert_queued_job(s: Session, kind: str, payload: dict | None, actor: str) -> int:
    """The Job insert shared by submit() and the budgeted doors: AI opt-out refusal, row,
    commit. Returns the job id."""
    if kind == "translate_document":
        # AI opt-out (DPAI-08): refuse at the door (AIOptOutRefused is a ValueError),
        # before a Job row exists, so the caller gets a plain refusal immediately.
        from app.services import ai_optout

        ai_optout.refuse_for_document(s, (payload or {}).get("document_id"), "machine translation")
    job = Job(kind=kind, payload=payload or {}, created_by=actor)
    s.add(job)
    s.commit()
    s.refresh(job)
    return job.id


def submit(kind: str, payload: dict, actor: str = "", *, session: Session | None = None) -> int:
    """Insert a QUEUED job and hand it to the workers. `session`, when given, is the
    caller's own transaction (the budgeted doors hold the department reservation lock on
    it, #622): the Job is inserted and committed there, which also releases that lock."""
    if kind not in HANDLERS:
        raise ValueError(f"Unknown job kind: {kind}")
    if session is not None:
        jid = _insert_queued_job(session, kind, payload, actor)
    else:
        with Session(engine) as s:
            jid = _insert_queued_job(s, kind, payload, actor)
    from app.ports import jobqueue

    ensure_workers()
    jobqueue.get().enqueue(jid)
    return jid


def submit_budgeted(
    kind: str, payload: dict, actor: str = "", *, department: str = "", org_id=None
) -> int:
    """submit() for a paid translate job, with the department budget check and the insert
    under ONE lock (#503 item 2).

    budgets.check used to run at the launch gate and the Job row (which carries the
    est_tokens the next check counts as in-flight spend) was inserted later, with nothing
    holding the two together: twenty concurrent 50,000-token requests against a 10,000-token
    remainder all passed the check and enqueued a million tokens. Here the check is repeated
    under `_live_lock`, the lock the live-stream reservation already uses, so the first job
    uses up what is left of the cap and a concurrent submit sees it and is refused
    (LiveBudgetRefused, HTTP 429 at the doors). Like every MT door, a submit is allowed while
    anything remains. #622: the check and the insert also share ONE database transaction
    holding the department reservation lock (budgets.check_locked), so two app processes or
    workers cannot both pass the check for the last of a cap.

    A blank department or no budget row is unlimited, so this is `submit` for them."""
    from app.services import budgets

    with _live_lock, Session(engine) as s:
        # #622: check, insert and commit in ONE transaction that holds the database-level
        # reservation lock (budgets.check_locked), so a second process cannot also pass.
        allowed, _rem, msg = budgets.check_locked(s, (department or "").strip(), org_id=org_id)
        if not allowed:
            s.rollback()
            raise LiveBudgetRefused(msg)
        return submit(kind, payload, actor=actor, session=s)


def submit_unique_budgeted(
    kind: str,
    payload: dict,
    actor: str = "",
    *,
    dedupe_key: str | tuple,
    department: str = "",
    org_id=None,
) -> tuple[int, bool]:
    """submit_unique() under the same department budget lock as submit_budgeted (#503 with
    #563): a job already QUEUED or RUNNING for the same dedupe key is handed back (no new
    spend, so no budget check); otherwise the cap is re-checked and the job inserted while
    `_live_lock` is held. Raises LiveBudgetRefused when the cap is used up. Lock order is
    always _live_lock then _submit_lock (nothing takes them the other way round)."""
    from app.services import budgets

    keys = (dedupe_key,) if isinstance(dedupe_key, str) else tuple(dedupe_key)
    want = tuple((payload or {}).get(k) for k in keys)
    with _live_lock:
        with Session(engine) as s:
            for j in s.exec(
                select(Job).where(
                    Job.kind == kind, Job.status.in_([JobStatus.QUEUED, JobStatus.RUNNING])
                )
            ).all():
                if tuple((j.payload or {}).get(k) for k in keys) == want:
                    return j.id, False
        # #622: the cap check and the insert share one transaction holding the database
        # reservation lock, so another process cannot also pass for the last of the cap.
        # Review: take _submit_lock BEFORE the database lock (submit_unique skips it when it is
        # handed this session). Plain submit_unique callers hold _submit_lock and then wait for
        # the SQLite writer lock; taking the two in the other order here would make them wait
        # on each other until busy_timeout and fail the plain caller with "database is locked".
        with _submit_lock, Session(engine) as s:
            allowed, _rem, msg = budgets.check_locked(s, (department or "").strip(), org_id=org_id)
            if not allowed:
                s.rollback()
                raise LiveBudgetRefused(msg)
            return submit_unique(kind, payload, actor=actor, dedupe_key=dedupe_key, session=s)


def estimate_translate_tokens(doc_id: int, locale: str) -> int:
    """The est_tokens stamp for a queued translate_document job: source characters of the
    locale's segments / 4 (the heuristic POST /api/documents/{id}/translate stamps), at least
    1. Lets every queued door put the same in-flight figure on its Job (#503)."""
    from app.models import Segment

    with Session(engine) as s:
        chars = sum(
            len(t or "")
            for t in s.exec(
                select(Segment.source_text).where(
                    Segment.document_id == doc_id, Segment.target_locale == locale
                )
            ).all()
        )
    return max(1, chars // 4)


def submit_unique(
    kind: str,
    payload: dict,
    actor: str = "",
    dedupe_key: str | tuple = "connection_id",
    *,
    session: Session | None = None,
) -> tuple[int, bool]:
    """Submit a job unless one of the same kind with matching payload [dedupe_key]
    field(s) is already QUEUED or RUNNING. Returns (job_id, created): on a dedup
    hit created is False and job_id is the live job. The single guard against
    stacking a second connector run behind an in-flight one — atomic under
    _submit_lock so a check-then-insert can't race itself.

    dedupe_key is normally a single payload field name (the plain "same connection"
    guard every other caller uses). It also accepts a TUPLE of field names for a
    COMPOUND match — e.g. (F2, adversarial review) "Translate pending now"
    double-click: keying on connection_id alone would coalesce the pending-release
    onto ANY other in-flight job for that connection (a full sync, a retry, ...)
    and the pending set would never get its own dedicated run; keying on
    (connection_id, "jira_translate_pending") only coalesces a second rapid
    pending-release with a first one already in flight, leaving every other job
    kind for the connection free to run alongside it as before."""
    if kind not in HANDLERS:
        raise ValueError(f"Unknown job kind: {kind}")
    keys = (dedupe_key,) if isinstance(dedupe_key, str) else tuple(dedupe_key)
    want = tuple((payload or {}).get(k) for k in keys)
    # `session`, when given, is the caller's own transaction (the budgeted door holds the
    # department reservation lock on it, #622); the caller closes it.
    own = session is None
    s = Session(engine) if own else session
    try:
        # A caller-owned session means the budgeted door already holds _submit_lock (it must
        # take it before the database reservation lock), and the lock is not reentrant.
        with contextlib.nullcontext() if not own else _submit_lock:
            active = s.exec(
                select(Job).where(
                    Job.kind == kind, Job.status.in_([JobStatus.QUEUED, JobStatus.RUNNING])
                )
            ).all()
            for j in active:
                got = tuple((j.payload or {}).get(k) for k in keys)
                if got == want:
                    return j.id, False
            job = Job(kind=kind, payload=payload or {}, created_by=actor)
            s.add(job)
            s.commit()
            s.refresh(job)
            jid = job.id
    finally:
        if own:
            s.close()
    from app.ports import jobqueue

    ensure_workers()
    jobqueue.get().enqueue(jid)
    return jid, True


@admitted_work
def run_sync(kind: str, payload: dict, actor: str = "cli") -> Job:
    """Run a job handler SYNCHRONOUSLY in the CALLING thread and return the
    finished Job row. Additive to submit()/submit_unique() — no daemon worker is
    started and nothing is enqueued.

    This is what the one-shot CLI needs: a `python -m app.cli` process has no
    daemon worker threads to pick a QUEUED job up (they die with the process), so
    submit()-and-exit would leave the job forever QUEUED. run_sync writes the
    same Job lifecycle rows (QUEUED -> RUNNING -> COMPLETED/FAILED) and reports
    progress the same way _execute does, but blocks until the handler returns.

    A failing handler does NOT raise: like the worker path, the failure is
    recorded on the Job (status=FAILED, error, and — for JobFailed — result) and
    the returned Job carries it. Callers inspect job.status."""
    if kind not in HANDLERS:
        raise ValueError(f"Unknown job kind: {kind}")
    with Session(engine) as s:
        job = Job(kind=kind, payload=payload or {}, created_by=actor)
        s.add(job)
        s.commit()
        s.refresh(job)
        jid = job.id
    _execute(jid)
    return get(jid)


def record_sync_job(
    kind: str,
    payload: dict,
    *,
    actor: str = "cli",
    result: dict | None = None,
    total: int = 0,
    status: str = JobStatus.COMPLETED,
    error: str = "",
) -> Job:
    """Persist a FINISHED Job row for translation work that already ran
    synchronously OUTSIDE the job system, so it is visible to reports and the
    unified jobs history exactly like the async path — WITHOUT re-running it.

    WHY (issue #16b): the CLI `doc translate` path consumes
    translate.pretranslate_stream() directly (for its streaming per-segment UX +
    pre-flight estimate) instead of going through run_sync(), so historically it
    wrote NO Job row. But reports.py reads Job rows for per-project cost/tokens and
    job_history.py reads them for the jobs list — so CLI-synchronous translation was
    invisible on BOTH surfaces (and, since run_report only folds a project's Segment
    volume once it sees a Job for that project, the VOLUME report was empty too, even
    though the segments existed). Recording the SAME translate_document Job row the
    async handler would have written (payload {document_id, locale}, the _new_stats
    result dict) makes the in-process CLI == the web/api path on every surface, with
    no schema change and no new report source.

    WHY NOT read a different source (the alternative considered): SpendLedger — the
    other place CLI spend lands — is a per-PERIOD GLOBAL aggregate (PK "YYYY-MM",
    only accumulated_usd; no project/customer/provider/token attribution), so it
    cannot drive a per-project SCOPED report without leaking cross-tenant cost, and
    carries no token counts at all. The Job.result is the only attributable, tokenful
    source of truth for CLI work — so we write it, matching the async path.

    Does NOT execute a handler and does NOT enforce/record spend (the caller's
    translate path already did, through the provider seam) — it only records the
    finished row. finished_at == now so reports bucket/range it on the run date."""
    now = datetime.utcnow()
    with Session(engine) as s:
        job = Job(
            kind=kind,
            payload=payload or {},
            created_by=actor,
            status=status,
            error=(error or "")[:500],
            result=_without_cost(result),
            cost_usd=_stream_cost(result),
            progress_done=total,
            progress_total=total,
            started_at=now,
            finished_at=now,
        )
        s.add(job)
        s.commit()
        s.refresh(job)
        return job


# --- live (streamed) translation: reserve before start, settle after ------------------
# The web live stream runs translate.pretranslate_stream inside the response, not as a
# queued job, so on its own it left no Job row and neither budget meter saw it (gate
# fix-livestream round 1, M1): the department meter sums translate_document Job rows
# (budgets.spend_this_month) and the project meter prices cost Jobs
# (project_budgets._ai_job_spend). The stream therefore opens the SAME translate_document
# Job row the background path writes, never enqueued, and settles it when it ends.
#
# While it runs the row carries the estimate in BOTH places the meters read: payload
# est_tokens (the department meter's in-flight figure for RUNNING jobs) and a provisional
# result priced on the resolved engine (the project meter prices result tokens whatever
# the status). The check and the insert happen under one lock, so once a reservation
# uses up what is left of a cap, a concurrent start sees it and is refused (like every MT
# door, a start is allowed while anything remains). #622: the department check and the
# Job insert also share one database transaction holding the reservation lock
# (budgets.check_locked), so two processes cannot both take the last of a cap.
# A process that dies mid-stream leaves the row RUNNING with no queue entry; boot recovery
# fails it on both queue backends (jobs.fail_orphaned_live_rows), keeping its charge.
_live_lock = threading.Lock()


class LiveBudgetRefused(Exception):
    """A department or project budget cap refuses a live translation (HTTP 429)."""


def _live_estimate(s: Session, doc_id: int, locale: str, source_locale: str) -> tuple[int, int]:
    """(input_tokens, output_tokens) the stream is expected to spend, gate round 2 M1-R.

    Input counts what is really SENT per untranslated segment: the base translation
    system prompt and user wrapper (built by the same `prompts.translation_prompt` the
    stream uses, without the per-document extras, so it is a floor) plus the segment's
    source. Output is the pre-flight estimate's `est_output_tokens` over the source
    (translate.estimate_translation's convention). Chars / 4 throughout (`est_tokens`)."""
    from app.models import Segment
    from app.services import prompts
    from app.services.providers import est_output_tokens
    from app.services.segment import est_tokens

    sources = [
        seg.source_text or ""
        for seg in s.exec(
            select(Segment).where(Segment.document_id == doc_id, Segment.target_locale == locale)
        ).all()
        if not (seg.target_text or "").strip()
    ]
    if not sources:
        return 1, 0
    try:
        system, prompt, _payload = prompts.translation_prompt("", source_locale, locale, [])
        overhead = est_tokens((system or "") + (prompt or ""))
    except Exception:
        overhead = 300  # about a 1.2k-char system prompt, if the builder cannot run here
    src_tokens = sum(est_tokens(t) for t in sources)
    return src_tokens + overhead * len(sources), est_output_tokens(src_tokens)


def _stream_cost(stats: dict | None) -> float | None:
    """The ledger figure a finished translate stream carries in its summary ("cost_usd",
    put there by translate.pretranslate_stream from the ledger's own bookings), or None when
    absent or not a finite non-negative number (-> unknown, priced as before)."""
    raw = (stats or {}).get("cost_usd")
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        return None
    cost = float(raw)
    return cost if cost >= 0.0 and cost != float("inf") else None


def _charge(result: dict | None) -> int:
    res = result or {}
    try:
        return int(res.get("tokens_in", 0) or 0) + int(res.get("tokens_out", 0) or 0)
    except (TypeError, ValueError):
        return 0


def _live_provider(s: Session, project, locale: str) -> str:
    try:
        from app.services import pair_providers

        return (pair_providers.resolve_provider(s, project, project.source_locale, locale)).lower()
    except Exception:
        return (project.default_provider or config.DEFAULT_PROVIDER or "").lower()


def reserve_live_translate(
    doc_id: int, locale: str, *, department: str = "", actor: str = "", org_id=None
) -> int:
    """Check both budget caps and open the stream's RUNNING Job row atomically.
    Raises LiveBudgetRefused(message) at a cap. Returns the job id to settle."""
    from app.models import Document, Project
    from app.services import budgets, project_budgets

    dept = (department or "").strip()
    with _live_lock, Session(engine) as s:
        # #622: ONE transaction from the department check to the Job commit, holding the
        # database reservation lock (budgets.check_locked) so another process cannot also
        # pass the check for the last of the cap. The lock is released by commit/rollback.
        allowed, _rem, msg = budgets.check_locked(s, dept, org_id=org_id)
        if not allowed:
            s.rollback()
            raise LiveBudgetRefused(msg)
        doc = s.get(Document, doc_id)
        project = s.get(Project, doc.project_id) if doc is not None else None
        pid = project.id if project is not None else None
        p_allowed, _p_rem, p_msg = project_budgets.project_check(pid)
        if not p_allowed:
            # Release the reservation lock first: the alert below writes on its own session.
            s.rollback()
            # H1 parity: the blocked submission is the 100% crossing, alert first.
            project_budgets.evaluate_thresholds(pid, actor=actor)
            raise LiveBudgetRefused(p_msg)
        now = datetime.utcnow()
        src_locale = project.source_locale if project is not None else ""
        est_in, est_out = _live_estimate(s, doc_id, locale, src_locale)
        est = est_in + est_out
        provider = _live_provider(s, project, locale) if project is not None else ""
        job = Job(
            kind="translate_document",
            payload={
                "document_id": doc_id,
                "locale": locale,
                "department": dept.lower(),
                "est_tokens": est,
                "live_stream": True,
            },
            created_by=actor,
            status=JobStatus.RUNNING,
            result={
                "tokens_in": est_in,
                "tokens_out": est_out,
                "provider_used": {provider: 1} if provider else {},
                "estimated": True,
            },
            message="live translation (streamed)",
            started_at=now,
        )
        s.add(job)
        s.commit()
        s.refresh(job)
        jid = job.id
    if pid is not None:
        project_budgets.evaluate_thresholds(pid, actor=actor)
    return jid


def settle_live_translate(
    jid: int,
    stats: dict | None,
    *,
    done: int = 0,
    error: str = "",
    partial: dict | None = None,
) -> None:
    """Close a live stream's Job row. `stats` is the stream's summary event: the real
    tokens and engines replace the reservation (COMPLETED). Without a summary (client
    cut off, engine error) the row turns FAILED and is charged the LARGER of the
    reservation estimate and `partial`, the stream's running totals up to the cut
    (gate round 2 M1-R: a late cut-off had spent far more than the estimate, an early
    one less than the calls still in flight may cost). Never raises."""
    try:
        with Session(engine) as s:
            job = s.get(Job, jid)
            if job is None or job.status != JobStatus.RUNNING:
                return
            if stats:
                # #364: a settled stream stores the figure the ledger booked for it; a
                # cut-off (below) leaves it NULL, so the charge stays the conservative
                # estimate, since calls still in flight when the client left may book more.
                job.cost_usd = _stream_cost(stats)
                job.result = _without_cost(stats)
                # #572: a stream whose every segment failed is FAILED with the engine's
                # error text, like the background job (the tokens stay on the row).
                dead = run_error(stats, job.progress_total or None)
                job.status = JobStatus.FAILED if dead else JobStatus.COMPLETED
                if dead:
                    job.error = dead[:500]
            else:
                if partial and _charge(partial) > _charge(job.result):
                    job.result = {**_without_cost(partial), "partial": True}
                job.status = JobStatus.FAILED
                job.error = error or "live translation stopped before it finished"
            job.progress_done = done
            job.finished_at = datetime.utcnow()
            s.add(job)
            s.commit()
    except Exception:
        log.warning("could not settle live translation job %s", jid, exc_info=True)


def ensure_workers() -> None:
    global _started
    with _start_lock:
        if _started:
            return
        for i in range(WORKER_COUNT):
            threading.Thread(target=_worker_loop, name=f"loc-job-{i}", daemon=True).start()
        _started = True


def _worker_loop() -> None:
    from app.ports import jobqueue

    worker_id = f"{instance_id()}/{threading.current_thread().name}"
    while True:
        q = jobqueue.get()  # call-time lookup: honors a boot-time swap to the DB adapter
        jid = q.claim(worker_id)
        if jid is None:  # only a finite-timeout claim returns None; the blocking path never does
            continue
        _worker_ctx.deferrable = True
        _worker_ctx.deferred = False
        try:
            with accounted_work():
                _execute(jid)
        except Exception:
            log.exception("Job %s crashed outside its handler", jid)
        finally:
            deferred = getattr(_worker_ctx, "deferred", False)
            _worker_ctx.deferred = False
            if deferred:
                try:
                    # One atomic step per adapter (never enqueue + release: on the DB queue
                    # release keys on job_id and would also fail a second worker's fresh claim).
                    q.requeue(jid)
                except Exception:
                    # The claim is left in place: lease expiry / boot recovery hands it back.
                    log.exception("Job %s could not be requeued; it stays QUEUED", jid)
            else:
                q.release(jid)
        if deferred:
            time.sleep(_DEFER_PAUSE_S)  # do not spin on a backlog that is all at its cap


def _update(jid: int, **fields) -> None:
    with Session(engine) as s:
        job = s.get(Job, jid)
        if not job:
            return
        for k, v in fields.items():
            setattr(job, k, v)
        s.add(job)
        s.commit()
    # Observability (Train Q2): count job STATUS transitions (RUNNING/COMPLETED/FAILED)
    # from the one lifecycle setter -- a progress-only _update (no `status` field) is
    # skipped. Lazy import + a strict no-op when metrics is off; never perturbs the job.
    if "status" in fields:
        from app import extensions as _extensions

        _extensions.observability.observe_job_status(fields["status"])


@admitted_work
def _execute(jid: int) -> None:
    with Session(engine) as s:
        job = s.get(Job, jid)
        if not job or job.status != JobStatus.QUEUED:
            return
        kind, payload, message = job.kind, dict(job.payload or {}), job.message
    if kind == "translate_document" and getattr(_worker_ctx, "deferrable", False):
        full = _engine_at_cap(payload)
        if full:
            # #613: the engine is at its cap. Requeue WITHOUT flipping to RUNNING, so status
            # surfaces (web, API, CLI, MCP) keep showing QUEUED and the job-status metric is
            # not bumped once a second while the job waits. The in-handler check below still
            # covers the race where the last slot is taken after this look.
            if message != _DEFER_MESSAGE:
                _update(jid, message=_DEFER_MESSAGE)
            log.debug("Job %s deferred before start: engine %s at its cap", jid, full)
            _worker_ctx.deferred = True
            return
    _update(jid, status=JobStatus.RUNNING, started_at=datetime.utcnow())
    if kind == "connector_translate":
        from app.services import notify

        notify.on_connector_job(jid, "started")  # never raises; a no-op without channels

    def progress(done: int, total: int | None = None, message: str = "") -> None:
        fields = {"progress_done": done, "message": message[:300]}
        if total is not None:
            fields["progress_total"] = total
        _update(jid, **fields)

    from app.services import spend

    # #364: collect the USD the ledger books while this job runs (every call, failover and
    # failed attempts included, a free engine's 0) and store it on the row with the terminal
    # status. A handler-supplied "cost_usd" in the result is never trusted: it is dropped.
    with spend.cost_scope() as cost_sink:
        _run_handler(jid, kind, payload, progress, cost_sink)
    if getattr(_worker_ctx, "deferred", False):
        return  # requeued, not finished: no terminal notification

    # Terminal chokepoint: notify the submitter their translation finished. Guarded
    # so a mail failure NEVER crashes the worker (mirrors scheduler's guarded audit).
    _after_execute(jid, kind)


def _run_handler(jid: int, kind: str, payload: dict, progress, cost_sink) -> None:
    """Run one job handler and record its terminal state plus the booked cost."""
    from app.services import ai_optout, translate
    from app.services.providers import DeadEngineError

    _worker_ctx.deferred = False
    try:
        # AI opt-out (DPAI-08): the job runs inside its document/project AI scope, so any
        # engine call a handler makes passes ai_optout.guard with project context.
        job_token = _CURRENT_JOB.set(jid)
        try:
            with ai_optout.scope(
                project_id=payload.get("project_id"), document_id=payload.get("document_id")
            ):
                result = HANDLERS[kind](payload, progress)
        finally:
            _CURRENT_JOB.reset(job_token)
        _update(
            jid,
            status=JobStatus.COMPLETED,
            finished_at=datetime.utcnow(),
            result=_without_cost(result),
            cost_usd=cost_sink.total,
        )
    except JobDeferred as e:
        # Engine at its max_parallel_jobs cap: back to QUEUED, no cost, no notification. The
        # worker loop re-enqueues the id (see _worker_loop).
        log.info("Job %s (%s) deferred: engine %s is at its parallel-jobs limit", jid, kind, e)
        _update(
            jid,
            status=JobStatus.QUEUED,
            started_at=None,
            progress_done=0,
            message=_DEFER_MESSAGE,
        )
        _worker_ctx.deferred = True
    except JobFailed as e:
        # Controlled failure that still carries data (e.g. a connector run that
        # reached the API but had item failures): FAILED, but keep both.
        log.warning("Job %s (%s) failed with data: %s", jid, kind, e)
        _update(
            jid,
            status=JobStatus.FAILED,
            finished_at=datetime.utcnow(),
            error=str(e)[:500],
            result=_without_cost(e.result),
            cost_usd=cost_sink.total,
        )
    except (
        translate.DocumentNotFound,
        MissingPayloadField,
        DeadEngineError,
        ConnectorRunError,
        ai_optout.AIOptOutRefused,
    ) as e:
        # The controlled, expected conditions (e.g. translate_document with a
        # nonexistent document_id, a payload missing a required key, or a stored
        # engine:<id> ref that went dead — DeadEngineError) are data, NOT a crash —
        # record a clean message and skip log.exception so the one-shot CLI prints
        # no scary traceback.
        #
        # Deliberately NOT a bare `except (LookupError, KeyError)`: IndexError and
        # KeyError are both LookupError subclasses, so that used to swallow a
        # genuine handler bug (a broken dict/list access inside a handler) as a
        # benign "not found" too, with no traceback to debug it from. Catching
        # these two specific, deliberately-raised classes keeps the clean-failure
        # behavior for the controlled cases while letting real bugs fall through
        # to the log.exception branch below.
        log.warning("Job %s (%s) failed: %s", jid, kind, e)
        _update(
            jid,
            status=JobStatus.FAILED,
            finished_at=datetime.utcnow(),
            error=str(e)[:500],
            cost_usd=cost_sink.total,
        )
    except Exception as e:
        log.exception("Job %s (%s) failed", jid, kind)
        _update(
            jid,
            status=JobStatus.FAILED,
            finished_at=datetime.utcnow(),
            error=f"{type(e).__name__}: {e}"[:500],
            cost_usd=cost_sink.total,
        )


def _without_cost(result: dict | None) -> dict:
    """A job result without any "cost_usd" key: the figure lives in Job.cost_usd, written
    only from the ledger's own total, never from what a handler or summary says."""
    out = dict(result or {})
    out.pop("cost_usd", None)
    return out


def _after_execute(jid: int, kind: str) -> None:
    # self-filters to translate_document jobs with an email created_by; no-ops when
    # notify_on_job_finish is off or SMTP is unconfigured.
    try:
        from app.services import notifications

        with Session(engine) as s:
            finished = s.get(Job, jid)
        if finished is not None:
            notifications.on_job_finished(finished)
    except Exception:
        log.exception("Completion notification failed for job %s", jid)

    if kind == "connector_translate":
        from app.services import notify

        notify.on_connector_job(jid, "settled")  # never raises; a no-op without channels

    # CV-2 (H1b): a finished job's AI spend has now LANDED, so a project can cross its
    # 80%/100% budget threshold with NO new submission at all. Evaluate the alert for the
    # job's project at this async chokepoint (idempotent; own Session(engine)). Guarded so
    # it never crashes the worker. Best-effort project resolution via the reports seam.
    try:
        from app.services import project_budgets

        with Session(engine) as s:
            job = s.get(Job, jid)
            # N5: bound-safe resolution — a hostile/out-of-range document_id never reaches
            # a session.get bind (reports._job_project_id OverflowErrors on one, which
            # silently killed this hook), so it works on every job, not just clean ids.
            project_id = project_budgets.job_project_id_safe(s, job)
        if project_id is not None:
            project_budgets.evaluate_thresholds(project_id, actor="system")
    except Exception:
        log.exception("Budget threshold eval failed for job %s", jid)


# --- queries ---
def get(jid: int) -> Job | None:
    with Session(engine) as s:
        return s.get(Job, jid)


def recent(limit: int = 50) -> list[Job]:
    with Session(engine) as s:
        return list(
            s.exec(select(Job).order_by(Job.created_at.desc(), Job.id.desc()).limit(limit)).all()
        )


def job_org_id(job: Job):
    """The org a Job belongs to, for multi-org isolation, or None.

    A Job carries no org_id column and no direct project link. Its org is
    resolved in two stages:

    1. REAL-USER attribution (unchanged): the org of the user who created it
       (Job.created_by == User.email). This is authoritative — a real user's
       job belongs to that user's org, full stop.

    2. PSEUDO-ACTOR content resolution (added): jobs created by the pseudo-actors
       "webhook" / "scheduler" (and any other created_by that is not a real User)
       have no user to look up, so stage 1 yields None. Rather than leaving them
       NULL-org — invisible to even the correct org's admin on /api/jobs/{id} —
       we derive the org from the CONTENT the job references:
         payload connection_id -> Connection.org_id
         payload document_id   -> Document.project_id -> Project.org_id
         payload project_id    -> Project.org_id
       The org is read ONLY from the referenced row's own org_id, NEVER from any
       caller-supplied value — a job that references org A's data can therefore
       only ever resolve to org A. This strictly NARROWS the NULL-org set (adds
       correct attribution); it can never make an org-A job readable by org B.

    Returns None when nothing resolves (the byte-identical fallback: unknown
    actor with no resolvable content -> NULL-org -> foreign to every scoped
    caller). Callers gate this behind access.multi_org_active(); pure lookup."""
    if job is None:
        return None
    actor = getattr(job, "created_by", "") or ""
    from app.models import User

    if actor:
        with Session(engine) as s:
            user = s.exec(select(User).where(User.email == actor)).first()
        if user is not None:
            # Real user: authoritative, even if their org is None. Do NOT fall
            # through to content resolution — real-user attribution is unchanged.
            return getattr(user, "org_id", None)
    # Pseudo-actor (webhook/scheduler) or unknown actor: resolve from the row the
    # job references. Only the referenced row's own org_id is trusted.
    #
    # Defense-in-depth note: this stage-2 fall-through is safe ONLY because every
    # job-creation path that accepts a caller-supplied payload enforces, at write
    # time, that payload-org == creator-org (see api_cli._payload_in_org /
    # ._conn_in_org, which 404 a job run whose payload references an out-of-org
    # connection/project/document). That invariant is what makes content
    # resolution here equivalent to creator resolution for any real, correctly
    # created job. A future job-creation path that skips that write guard would
    # silently break this coupling -- it must enforce the same invariant, or this
    # fall-through can no longer be trusted.
    return _org_from_payload(getattr(job, "payload", None))


def _org_from_payload(payload):
    """Resolve a job's org from the rows its payload references (pseudo-actor
    path). Returns the org_id of the referenced Connection / Project (directly,
    or via a Document's project), or None if nothing resolves. Never derives an
    org from anything but the referenced row's stored org_id."""
    payload = payload or {}
    from app.models import Connection, Document, Project

    with Session(engine) as s:
        cid = payload.get("connection_id")
        if cid is not None:
            conn = s.get(Connection, cid)
            if conn is not None and conn.org_id is not None:
                return conn.org_id
        pid = payload.get("project_id")
        if pid is not None:
            proj = s.get(Project, pid)
            if proj is not None and proj.org_id is not None:
                return proj.org_id
        did = payload.get("document_id")
        if did is not None:
            doc = s.get(Document, did)
            if doc is not None:
                proj = s.get(Project, doc.project_id)
                if proj is not None and proj.org_id is not None:
                    return proj.org_id
    return None


def status_dict(job: Job) -> dict:
    return {
        "id": job.id,
        "kind": job.kind,
        "status": job.status,
        "done": job.progress_done,
        "total": job.progress_total,
        "message": job.message,
        "error": job.error,
        "result": job.result or {},
    }


def recover_interrupted(at_boot: bool = False) -> int:
    """Recover jobs a restart interrupted. DELEGATES to the active JobQueue adapter
    (app/ports/jobqueue.py). The DEFAULT in-memory adapter re-enqueues the QUEUED rows and
    FAILS the RUNNING ones (byte-identical to the pre-Train-S behavior -- the memory queue is
    lost with the process, so an interrupted run is visibly failed, never silently lost). The
    durable "db" adapter returns interrupted RUNNING jobs to pending so they RE-RUN.

    ``at_boot`` gates the durable adapter's lease handling (a no-op for the in-memory adapter):
    the server ``_startup`` passes ``at_boot=True`` (no worker is alive -> reclaim every
    RUNNING job unconditionally); the ``db recover-jobs`` CLI sweep leaves it FALSE (it may run
    against a LIVE server, so it reclaims only EXPIRED-lease jobs and never yanks one a live
    worker is still executing -- see DbJobQueue.recover). Safe-by-default: a caller that omits
    the flag never double-dispatches a live job. Returns the count touched."""
    from app.ports import jobqueue

    touched = jobqueue.get().recover(at_boot)
    if at_boot:
        touched += fail_orphaned_live_rows()
    return touched


def fail_orphaned_live_rows() -> int:
    """Fail every RUNNING live-stream Job row (gate round 2 L2-R). A live stream's row has
    no queue entry, so the durable "db" adapter's recovery never sees it, and at boot no
    process can still be streaming it. Left RUNNING it would block /api translate for
    that document and locale (`_active_translate_locales`) indefinitely. The row keeps
    its result, so its charge stays on both meters. Boot only: a live sweep against a
    running server could fail a stream still in progress. Returns the count."""
    n = 0
    now = datetime.utcnow()
    with Session(engine) as s:
        for job in s.exec(
            select(Job).where(Job.kind == "translate_document", Job.status == JobStatus.RUNNING)
        ).all():
            if not (job.payload or {}).get("live_stream"):
                continue
            job.status = JobStatus.FAILED
            job.error = "live translation interrupted by a restart"
            job.finished_at = now
            s.add(job)
            n += 1
        if n:
            s.commit()
    return n
