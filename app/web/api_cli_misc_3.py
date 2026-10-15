"""/api routes: smaller areas

Documented in: docs/architecture/surfaces-and-parity.md
"""

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from sqlmodel import Session, select

from app import db as _db
from app.db import engine
from app.kernel import (
    access,
    batch_runs,
    job_history,
    jobs,
    orchestrator,
    run_overlay,
    security,
)
from app.kernel import keys as keys_svc
from app.models import (
    Document,
    JobStatus,
    RunStatus,
    Segment,
    StepRun,
    WorkflowBatch,
    WorkflowRun,
)
from app.web.api_cli_common import (
    _NOT_FOUND,
    PROMPT_TAP_LIST_CAP,
    PROMPT_TAP_LIST_DEFAULT,
    _admin_block,
    _ai_block,
    _batch_wait_response,
    _creator,
    _cust_of,
    _drain_run_to_terminal,
    _gate,
    _int_id,
    _json,
    _project_in_org,
    _scrub_spend,
    _stream_wait_route,
    _write_block,
)
from app.web.deps import WorkerBusy, busy_response, offload_parse, optional_id_param
from app.web.streaming import run_sync as run_in_threadpool

router = APIRouter()


def _rerun_batch_blocking(batch_id: int, locales, actor: str) -> int:
    """Re-run `locales` of a batch to finalization in a FRESH session — the `wait=true`
    worker for BOTH /api/batches/{id}/rerun and /retry-failed (AUDIT follow-up to Q2; the
    same DoS class the /run-batch wait path fixed). Called ONLY via `run_in_threadpool`;
    `rerun_batch(synchronous=True)` runs `batch_runs._dispatch` and blocks the worker thread
    until every re-dispatched member is terminal, so it never runs on the event loop.

    Returns the batch id; the route re-reads it for the response shape. Raises
    batch_runs.BatchError on an invalid request (caller maps to 400).

    No per-member race, exactly like `_run_batch_blocking`: `rerun_batch` re-dispatches
    through the SAME `_dispatch`, whose per-(wf, doc, locale) check-and-create is serialized
    under `orchestrator._RUN_CREATE_LOCK` (the already-running skip/re-attach guard) whether
    synchronous or not. Two concurrent wait=true re-runs over one batch cannot double-launch a
    locale — the second sees the first's committed member and skips. This worker only needs to
    stay OFF the event loop, which it does. The batch is re-fetched here because the request-
    thread session that validated the launch is already closed by the time we run."""
    with Session(engine) as s:
        batch = s.get(WorkflowBatch, batch_id)
        if batch is None:
            raise batch_runs.BatchMissing(batch_id)
        batch = batch_runs.rerun_batch(
            s, batch=batch, locales=list(locales), created_by=actor, synchronous=True
        )
        return batch.id


@router.get("/api/batches/{batch_id}")
def api_batches_show(batch_id: int, request: Request):
    """A batch's live status JSON (Phase 5a; the 5b monitor poll + `workflow batch show`
    endpoint). Same org+customer scope as the run-view read: a batch whose document is in
    another org/customer 404s (indistinguishable from missing). Read-only — creates no state.
    `?detail=steps` attaches per-member step_runs + the requested-step baseline (the matrix
    payload); the light shape stays light for a plain status check."""
    ident, org_id, blocked = _gate(request)
    if blocked:
        return blocked
    detail = request.query_params.get("detail") == "steps"
    with Session(engine) as s:
        batch = s.get(WorkflowBatch, batch_id)
        if batch is None:
            return JSONResponse(_NOT_FOUND, status_code=404)
        doc = s.get(Document, batch.document_id)
        if doc is None or _project_in_org(s, doc.project_id, org_id, ident) is None:
            return JSONResponse(_NOT_FOUND, status_code=404)
        out = batch_runs.batch_as_dict(s, batch, detail=detail)
    return JSONResponse(_scrub_spend(ident, {"ok": True, **out}))


@router.post("/api/batches/{batch_id}/rerun")
@_stream_wait_route
async def api_batches_rerun(batch_id: int, request: Request):
    """Re-run one (or more) locale(s) of a batch as fresh member runs INTO the same batch
    (SPEC Phase 5b) and return the (RUNNING) matrix shape immediately.

    CONTRACT CHANGE (2026-08-11, AUDIT follow-up to Q2 — the SAME DoS class /run-batch fixed,
    on an adjacent route). This used to re-dispatch SYNCHRONOUSLY (rerun_batch(synchronous=
    True) joins every re-run member thread) directly on the event loop, so one re-run DoSed
    the whole instance for its duration. THE INVARIANT: batch execution never runs on the
    FastAPI event loop.

    DEFAULT (async): rerun_batch spawns the daemon dispatcher (synchronous=False) and this
    returns the batch_as_dict(detail) matrix at once — status RUNNING (finished_at cleared for
    the re-dispatch), the re-run locales' cells back to pending/running. Poll GET
    /api/batches/{id} to a terminal status (the remote CLI does this). wait=true uses
    bounded stream admission. Saturation returns waited:false and the current matrix;
    keep polling. An admitted wait re-dispatches off the loop and returns the
    re-finalized matrix. Same auth/scope as run-batch:
    docs-scoped key, org+customer ring, can_write. Body: {locale} or {locales:[...]}, wait?."""
    ident, org_id, blocked = getattr(request.state, "stream_wait_identity", None) or _gate(request)
    if blocked:
        return blocked
    blocked = _write_block(ident)
    if blocked:
        return blocked
    body = await _json(request)
    wait = bool(body.get("wait")) and getattr(request.state, "stream_wait_admitted", True)
    raw = body.get("locales")
    if isinstance(raw, list):
        locales = [str(x) for x in raw]
    elif body.get("locale"):
        locales = [str(body.get("locale"))]
    else:
        return JSONResponse(
            {"ok": False, "error": "give a locale (or locales[]) to re-run."}, status_code=400
        )
    with Session(engine) as s:
        batch = s.get(WorkflowBatch, batch_id)
        if batch is None:
            return JSONResponse(_NOT_FOUND, status_code=404)
        doc = s.get(Document, batch.document_id)
        if doc is None or _project_in_org(s, doc.project_id, org_id, ident) is None:
            return JSONResponse(_NOT_FOUND, status_code=404)
        if not wait:
            # DEFAULT: async daemon re-dispatch; return the initial (RUNNING) matrix at once.
            try:
                batch = batch_runs.rerun_batch(
                    s, batch=batch, locales=locales, created_by=ident["actor"], synchronous=False
                )
            except batch_runs.BatchBudgetExhausted as exc:
                return JSONResponse({"ok": False, "error": str(exc)}, status_code=429)
            except batch_runs.BatchError as exc:
                return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
            out = batch_runs.batch_as_dict(s, batch, detail=True)
            return JSONResponse(_scrub_spend(ident, {"ok": out["status"] != "failed", **out}))
    # wait=true: re-dispatch to finalization OFF the loop, in a threadpool worker, then read
    # the re-finalized matrix back. The validation session above is closed here.
    try:
        batch_id = await run_in_threadpool(_rerun_batch_blocking, batch_id, locales, ident["actor"])
    except batch_runs.BatchBudgetExhausted as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=429)
    except batch_runs.BatchError as exc:
        return JSONResponse(
            {"ok": False, "error": str(exc)},
            status_code=404 if isinstance(exc, batch_runs.BatchMissing) else 400,
        )
    return _batch_wait_response(batch_id, detail=True, ident=ident)


@router.post("/api/batches/{batch_id}/retry-failed")
@_stream_wait_route
async def api_batches_retry_failed(batch_id: int, request: Request):
    """Re-run EVERY failed locale of a batch as one action (SPEC Phase 5b) and return the
    (RUNNING) matrix shape immediately.

    CONTRACT CHANGE (2026-08-11, AUDIT follow-up to Q2 — same DoS class as /rerun). This used
    to re-dispatch SYNCHRONOUSLY on the event loop. THE INVARIANT: batch execution never runs
    on the FastAPI event loop. DEFAULT is now an async daemon re-dispatch (RUNNING matrix +
    retried[] at once); wait=true uses bounded stream admission. Saturation returns
    waited:false and a current, possibly running matrix; poll GET /api/batches/{id}.
    An admitted wait re-dispatches off the loop and returns the re-finalized matrix.
    Same auth/scope as run-batch. Body: {wait?} (none needed).

    When nothing has failed it is a no-op that returns the current (detail) shape with
    retried=[] — no launch, sync either way."""
    ident, org_id, blocked = getattr(request.state, "stream_wait_identity", None) or _gate(request)
    if blocked:
        return blocked
    blocked = _write_block(ident)
    if blocked:
        return blocked
    body = await _json(request)
    wait = bool(body.get("wait")) and getattr(request.state, "stream_wait_admitted", True)
    with Session(engine) as s:
        batch = s.get(WorkflowBatch, batch_id)
        if batch is None:
            return JSONResponse(_NOT_FOUND, status_code=404)
        doc = s.get(Document, batch.document_id)
        if doc is None or _project_in_org(s, doc.project_id, org_id, ident) is None:
            return JSONResponse(_NOT_FOUND, status_code=404)
        reason = batch_runs.rerun_unavailable_reason(s, batch)
        if reason:
            return JSONResponse({"ok": False, "error": reason}, status_code=400)
        locales = batch_runs.failed_locales(s, batch)
        if not locales:
            out = batch_runs.batch_as_dict(s, batch, detail=True)
            return JSONResponse(_scrub_spend(ident, {"ok": True, "retried": [], **out}))
        if not wait:
            # DEFAULT: async daemon re-dispatch of the failed set; RUNNING matrix at once.
            try:
                batch = batch_runs.rerun_batch(
                    s, batch=batch, locales=locales, created_by=ident["actor"], synchronous=False
                )
            except batch_runs.BatchBudgetExhausted as exc:
                return JSONResponse({"ok": False, "error": str(exc)}, status_code=429)
            except batch_runs.BatchError as exc:
                return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
            out = batch_runs.batch_as_dict(s, batch, detail=True)
            return JSONResponse(
                _scrub_spend(ident, {"ok": out["status"] != "failed", "retried": locales, **out})
            )
    # wait=true: re-dispatch the failed set to finalization OFF the loop, then re-read.
    try:
        batch_id = await run_in_threadpool(_rerun_batch_blocking, batch_id, locales, ident["actor"])
    except batch_runs.BatchBudgetExhausted as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=429)
    except batch_runs.BatchError as exc:
        return JSONResponse(
            {"ok": False, "error": str(exc)},
            status_code=404 if isinstance(exc, batch_runs.BatchMissing) else 400,
        )
    return _batch_wait_response(batch_id, detail=True, retried=locales, ident=ident)


@router.get("/api/runs/{run_id}")
def api_runs_show(run_id: int, request: Request):
    """Issue #23 (a): read a run's status and, for a PAUSED human-review run, the
    text-to-review with explicit machine-usable segment boundaries. Read-only mirror of
    the run-view page (same newest-PAUSED-row state); no new run state is created.

    BUG-09(1): the payload previously carried run_id/status/step states but no
    workflow_id/document_id/target_locale/timestamps, so a run could not be
    attributed without a second lookup -- now additive top-level fields, always on
    (cheap column reads, no new query shape).

    BUG-07 (`?findings=1`): attach the LQA MQM findings block for this run, read-only
    and from EXISTING storage -- no new table, no re-scoring call (that would be a
    live LLM call on every read, which a read surface must never trigger). The
    orchestrator's LQA step already renders+persists the fenced findings text onto
    `WorkflowRun.context["lqa_findings_block"]` so a resume can feed it back to a
    fixer step; this just reads that same field. `null` when the run never reached an
    LQA step (or the field predates this run). Opt-in, mirroring `?visits=1` -- the
    default payload is unchanged without it."""
    ident, org_id, blocked = _gate(request)
    if blocked:
        return blocked
    with Session(engine) as s:
        run = s.get(WorkflowRun, run_id)
        if run is None:
            return JSONResponse(_NOT_FOUND, status_code=404)
        doc = s.get(Document, run.document_id)
        if doc is None or _project_in_org(s, doc.project_id, org_id, ident) is None:
            return JSONResponse(_NOT_FOUND, status_code=404)
        view = orchestrator.paused_review(s, run_id)
        status = str(run.status)
        meta = {
            "workflow_id": run.workflow_id,
            "document_id": run.document_id,
            "target_locale": run.target_locale,
            "created_by": run.created_by or None,
            "started_at": run.started_at.isoformat() if run.started_at else None,
            "finished_at": run.finished_at.isoformat() if run.finished_at else None,
            # Run-level warnings, mirrored across local and remote views.
            "warnings": (run.context or {}).get("warnings") or [],
            "qe_steps": (run.context or {}).get("qe_steps"),
            **(
                {"tm_offers": run.context["tm_offers"]}
                if (run.context or {}).get("tm_offers") is not None
                else {}
            ),
            # #610: how many segments the tm_match step filled from the TM ({filled, relocked,
            # unmatched, total}); absent on a run with no segmented tm_match step.
            **(
                {"tm_match": run.context["tm_match_summary"]}
                if (run.context or {}).get("tm_match_summary") is not None
                else {}
            ),
            # GitHub #101: IN-STEP PROGRESS ("N of M segments/chunks" + when it last
            # moved). null when the run published none -- honest "no progress recorded",
            # never a false 0%. This is the field that makes a 10-25 minute llm_agent step
            # readable without turning on the default-OFF LLM Ledger.
            "progress": orchestrator.run_progress(run),
            # GitHub #106: WHY a FAILED/CANCELLED run ended that way, when a RECONCILER
            # put it there (boot reconcile after a crash, staleness sweep, operator cancel).
            # Those paths write no StepRun, so `failure_shape`/`step_states` are empty for
            # them by construction and the operator used to get `failed` + a timestamp and
            # nothing else. {code, text}; null on every run that did not end badly.
            "reason": orchestrator.run_reason(run),
            # Run notes (a pinned glossary skipped for not covering the run's languages,
            # a content-type skip, etc.), stamped onto WorkflowRun.notes by the run.
            # Read-only; null when the run has none.
            "notes": run.notes or None,
        }
        if request.query_params.get("findings"):
            meta["lqa_findings"] = (run.context or {}).get("lqa_findings_block")
        # Parity with the web run-view GRAPH (SPEC-conditional-workflows Phase 4 deferred
        # item): expose the per-step-key status/visits the graph draws, so the same run
        # state is readable headless. Additive fields; existing keys are byte-identical.
        step_runs = s.exec(
            select(StepRun).where(StepRun.workflow_run_id == run_id).order_by(StepRun.id)
        ).all()
        steps = orchestrator.get_steps(s, run.workflow_id)
        graph = run_overlay.run_step_states(step_runs, steps, run_status=run.status)
        # run-190 Item C: the per-step FAILURE SHAPE (attempts, provider, model, finish
        # reason, provider error class, output length, elapsed) — metadata only, no bodies,
        # so it is safe on every surface incl. --remote — attached BY DEFAULT. Before this
        # the useful run-190 fact (3 attempts, all finish_reason=length, 0 output) was
        # reachable only via `fly ssh console` into the host Ledger.
        graph["failure_shape"] = run_overlay.failure_shape(step_runs, steps)
        # ?visits=1 (CLI-parity backlog item 3): additive, opt-in per-VISIT
        # row-level trace -- mirrors ?detail=segments (QA-6). Computed ONLY when
        # asked, so the default response stays byte-identical without it.
        if request.query_params.get("visits"):
            graph["visit_history"] = run_overlay.visit_history(step_runs, steps)
    # #574: AI spend and tokens (per-step, per-visit) are manager-and-above, as on the run page.
    if view is None:
        return JSONResponse(
            _scrub_spend(
                ident,
                {"ok": True, "run_id": run_id, "status": status, "paused": False, **meta, **graph},
            )
        )
    return JSONResponse(_scrub_spend(ident, {"ok": True, "paused": True, **view, **meta, **graph}))


@router.get("/api/runs/{run_id}/step-diff")
def api_runs_step_diff(run_id: int, request: Request):
    """GitHub #340: the per-segment before/after/changed diff of two step runs of
    ONE run (word-level ops per segment, 'N of M segments changed'). The API/CLI/
    MCP twin of the /runs/{id}/step-diff page: SAME data, SAME auth and org scope
    as GET /api/runs/{id} -- a run (or document) the caller cannot see is a
    uniform 404, indistinguishable from a missing id. Read-only; no run state.

    ?from=<step_run_id>&to=<step_run_id> name the pair (both must be StepRun rows
    of THIS run; anything else is a 404, not enumerable). Omitted, the pair
    defaults to the run's newest translating output vs its previous translating
    step; a run with no comparable pair answers ok:true with both sides null
    (honest 'not recorded'), never an invented diff.

    A plain `def` ON PURPOSE: the word diff is CPU-bound, and FastAPI runs a sync
    handler on the worker threadpool, so it can never block the event loop (an
    `async def` here froze every user for the duration of one big segment)."""
    ident, org_id, blocked = _gate(request)
    if blocked:
        return blocked
    with Session(engine) as s:
        run = s.get(WorkflowRun, run_id)
        if run is None:
            return JSONResponse(_NOT_FOUND, status_code=404)
        doc = s.get(Document, run.document_id)
        if doc is None or _project_in_org(s, doc.project_id, org_id, ident) is None:
            return JSONResponse(_NOT_FOUND, status_code=404)
        step_runs = s.exec(
            select(StepRun).where(StepRun.workflow_run_id == run_id).order_by(StepRun.id)
        ).all()
        steps = orchestrator.get_steps(s, run.workflow_id)
        run_ids = {sr.id for sr in step_runs}
        raw_from = request.query_params.get("from", "").strip()
        raw_to = request.query_params.get("to", "").strip()
        if raw_from or raw_to:
            try:
                from_id = int(raw_from)
                to_id = int(raw_to)
            except ValueError:
                return JSONResponse(_NOT_FOUND, status_code=404)
            if from_id not in run_ids or to_id not in run_ids:
                return JSONResponse(_NOT_FOUND, status_code=404)
        else:
            from_id, to_id = run_overlay.step_diff_default_pair(step_runs, steps)
        view = run_overlay.step_diff_view(run, step_runs, steps, from_id=from_id, to_id=to_id)
    return JSONResponse({"ok": True, **view})


# run-190 Item C: the LLM Ledger SUMMARY over /api (and thus --remote + MCP). BODIES-
# WITHHELD by construction — these routes call tap_view's summary read model, which returns
# only tap_enrich._metric_row metadata (counts, statuses, error classes, finish reasons,
# spend, timings), NEVER a prompt or response body. The host-only refusal for raw bodies
# (`--calls`, show, export) stays: there is deliberately NO /api route that serves them.
# Org-scoped by the caller's token (org_id from _gate), so a multi-org caller only sees its
# own actions; single-tenant (org_id None) sees all, exactly like the run API.
@router.get("/api/prompt-tap/actions", summary="Ledger actions", operation_id="api_ledger_actions")
async def api_prompt_tap_actions(request: Request):
    """List prompt-tap Ledger ACTIONS newest-first as bodies-withheld summaries (counts,
    statuses, error classes, finish reasons, spend, timings). Filters (query params):
    project_id, document_id, batch_id, status, since, until, failures, limit (default 50,
    max 500 — a `truncated` flag says more exist). Safe on every surface — no prompt/response
    text is ever returned (`bodies_withheld: true`)."""
    ident, org_id, blocked = _gate(request)
    if blocked:
        return blocked
    blocked = _admin_block(ident)
    if blocked:
        return blocked
    # H5 (gate round 1): reach the telemetry read model through the KERNEL FACADE, the K2
    # shell law the enforced `telemetry` port contract pins — the same import the CLI half
    # uses (app/cli/cmd_prompt_tap.py). A direct app.services import is a facade break.
    from app.kernel import tap_view

    qp = request.query_params
    # M4 (gate round 1): bound the list route — default 50, hard cap 500, a `truncated`
    # flag, and a 400 on a malformed limit (never a silent coerce-to-50). Fetch cap+1 so the
    # flag is honest. A per-call `shape` list inside each summary is capped in tap_view.
    raw_limit = qp.get("limit")
    if raw_limit in (None, ""):
        limit = PROMPT_TAP_LIST_DEFAULT
    else:
        try:
            limit = int(raw_limit)
        except (TypeError, ValueError):
            return JSONResponse(
                {"ok": False, "error": "limit must be an integer."}, status_code=400
            )
        if limit < 1:
            return JSONResponse(
                {"ok": False, "error": "limit must be a positive integer."}, status_code=400
            )
    limit = min(limit, PROMPT_TAP_LIST_CAP)
    actions = tap_view.list_action_summaries(
        limit=limit + 1,
        project_id=_int_id(qp.get("project_id")),
        document_id=_int_id(qp.get("document_id")),
        batch_id=_int_id(qp.get("batch_id")),
        status=qp.get("status") or None,
        since=qp.get("since") or None,
        until=qp.get("until") or None,
        has_failures=qp.get("failures") in ("1", "true", "yes"),
        org_id=org_id,
        # H3 (gate round 1): the R2-3 within-org customer ring — a customer-bound key sees
        # only its own customer's Ledger actions, exactly like GET /api/runs and /api/projects.
        customer_id=_cust_of(ident),
        user=ident.get("user"),
    )
    from app.kernel import ledger_controls

    truncated = len(actions) > limit
    actions = actions[:limit]
    return JSONResponse(
        {
            "ok": True,
            "count": len(actions),
            "truncated": truncated,
            "bodies_withheld": True,
            "actions": actions,
            "ledger_status": ledger_controls.status(
                org_id=org_id,
                customer_id=_cust_of(ident),
                scoped=access.multi_org_active(),
                use_cache=True,
            ),
        }
    )


@router.get(
    "/api/prompt-tap/actions/{action_id}/summary",
    summary="Ledger action summary",
    operation_id="api_ledger_action_summary",
)
async def api_prompt_tap_action_summary(action_id: str, request: Request):
    """One prompt-tap Ledger action's bodies-withheld summary (envelope + per-run counts /
    statuses / error classes / finish reasons / spend / timings + the per-call failure
    SHAPE). No prompt/response text — the raw bodies stay HOST-ONLY."""
    ident, org_id, blocked = _gate(request)
    if blocked:
        return blocked
    blocked = _admin_block(ident)
    if blocked:
        return blocked
    # H5 (gate round 1): kernel facade, not a direct app.services import (the K2 shell law).
    # The action-id format guard lives inside tap_view.action_summary (an allowlisted
    # prompt_tap reader) so this web module never imports the telemetry WRITE seam directly.
    from app.kernel import tap_view

    # H3 (gate round 1): pass the within-org customer ring so a foreign customer's action
    # 404s (non-enumerable), the same confinement _project_in_org gives GET /api/runs/{id}.
    summary = tap_view.action_summary(
        (action_id or "").strip(),
        org_id=org_id,
        customer_id=_cust_of(ident),
        user=ident.get("user"),
    )
    if summary is None:
        return JSONResponse(_NOT_FOUND, status_code=404)
    return JSONResponse({"ok": True, "bodies_withheld": True, "action": summary})


# ROADMAP #6: the RUN-first Ledger lens over /api (and thus --remote + MCP). Folded under
# the feature's existing /api/prompt-tap namespace (L6, gate round 1) beside /api/prompt-tap/
# actions, so it needs NO new CORE_API entry. Like the action summary above, these are BODIES-
# WITHHELD by construction -- tap_runs returns only the per-call metadata digest (lengths +
# sha256 + a head only past 2*24 chars), NEVER a full prompt/response body. The host-only refusal
# for raw bodies stays: `?bodies=1` is REFUSED here (400), the same "no /api route serves raw
# bodies" posture -- the full text is reachable only host-side (the web page for an admin,
# `prompt-tap run <id> --bodies` on the host). Org + within-org customer scoped by the caller's
# token; single-tenant sees all, exactly like the action routes.
@router.get("/api/prompt-tap/runs")
async def api_tap_runs(request: Request):
    """List translation RUNS newest-first as bodies-withheld summaries (kind, call/failure
    counts, first/last timestamps, engines, total spend when recorded, token totals). Filters
    (query params): since, until (yyyy-mm-dd; a malformed date is a 400), failures, limit
    (default 50, max 500) and an opaque `cursor` -- a `truncated` flag + `next_cursor` page past
    the limit. Safe on every surface -- no prompt/response text is ever returned."""
    ident, org_id, blocked = _gate(request)
    if blocked:
        return blocked
    blocked = _admin_block(ident)
    if blocked:
        return blocked
    from app.kernel import tap_runs, tap_view

    qp = request.query_params
    raw_limit = qp.get("limit")
    if raw_limit in (None, ""):
        limit = PROMPT_TAP_LIST_DEFAULT
    else:
        try:
            limit = int(raw_limit)
        except (TypeError, ValueError):
            return JSONResponse(
                {"ok": False, "error": "limit must be an integer."}, status_code=400
            )
        if limit < 1:
            return JSONResponse(
                {"ok": False, "error": "limit must be a positive integer."}, status_code=400
            )
    limit = min(limit, PROMPT_TAP_LIST_CAP)
    # L3 (gate r1) + L1 (gate r2): reject a malformed since/until (400) AND normalise an accepted
    # date to canonical yyyy-mm-dd before the service compares it lexically against an ISO `ts`.
    # An unpadded date like '2026-9-5' parses but, forwarded raw, sorts wrong (the service does
    # prefix arithmetic on the string) and silently means "no runs"/"no filter". Pass the
    # canonical `.date().isoformat()`. tap_view._parse_date is the sibling validator.
    norm = {}
    for label in ("since", "until"):
        val = qp.get(label) or None
        if not val:
            norm[label] = None
            continue
        d = tap_view._parse_date(val)
        if d is None:
            return JSONResponse(
                {"ok": False, "error": f"{label} must be a yyyy-mm-dd date."}, status_code=400
            )
        norm[label] = d.date().isoformat()
    page = tap_runs.list_runs_page(
        limit=limit,
        cursor=qp.get("cursor") or None,
        since=norm["since"],
        until=norm["until"],
        has_failures=qp.get("failures") in ("1", "true", "yes"),
        org_id=org_id,
        customer_id=_cust_of(ident),
        user=ident.get("user"),
    )
    runs = page["runs"]
    return JSONResponse(
        {
            "ok": True,
            "count": len(runs),
            "truncated": page["truncated"],
            "next_cursor": page["next_cursor"],
            "bodies_withheld": True,
            "runs": runs,
        }
    )


@router.get("/api/prompt-tap/runs/{run_id}")
async def api_tap_run_detail(run_id: str, request: Request):
    """One translation run's calls grouped by step then unit, BODIES WITHHELD (per-call
    lengths + sha256 + a head only past 2*24 chars, never the full text). A foreign or unknown
    run id is a non-enumerable 404. `?bodies=1` is REFUSED (400) -- the raw call bodies stay
    host-only."""
    ident, org_id, blocked = _gate(request)
    if blocked:
        return blocked
    blocked = _admin_block(ident)
    if blocked:
        return blocked
    if request.query_params.get("bodies") in ("1", "true", "yes"):
        return JSONResponse(
            {
                "ok": False,
                "error": "raw call bodies are host-only; this route serves the "
                "bodies-withheld view. Read the payloads on the host "
                "(the web viewer, or `prompt-tap run <id> --bodies`).",
            },
            status_code=400,
        )
    from app.kernel import tap_runs

    detail = tap_runs.run_detail(
        (run_id or "").strip(),
        bodies=False,
        include_keys=False,
        org_id=org_id,
        customer_id=_cust_of(ident),
        user=ident.get("user"),
    )
    if detail is None:
        return JSONResponse(_NOT_FOUND, status_code=404)
    # L4 (gate round 1): DERIVE the flag from the detail, don't hardcode `true` -- a hardcoded
    # literal would lie "bodies_withheld" even if a future edit let a body through.
    return JSONResponse(
        {"ok": True, "bodies_withheld": bool(detail.get("bodies_withheld", True)), "run": detail}
    )


@router.post("/api/runs/{run_id}/resume")
@_stream_wait_route
async def api_runs_resume(run_id: int, request: Request):
    """Resume a paused workflow run and return a launch ACK immediately.

    CONTRACT CHANGE (2026-08-11, AUDIT follow-up to Q2 — the SAME DoS class /run fixed). This
    route was the odd one out: it ran the WHOLE remainder of the workflow inline with
    `orchestrator.resume_workflow`, an `async def` awaiting a BLOCKING, minutes-to-hours
    function directly on the event loop, so one resume starved the loop for its duration (the
    web `POST /runs/{id}/resume` route already launched async via `resume_run_async`). THE
    INVARIANT: the orchestrator's blocking execution must NEVER run on the FastAPI event loop.

    DEFAULT (async launch): `resume_run_async` advances past the human step + continues the
    run on a BACKGROUND THREAD, and this returns {ok, kind:"run", run_id, status:"running"} at
    once — NOT the finished run. Poll GET /api/runs/{run_id} to a terminal/paused state (the
    remote CLI `workflow resume` does this). The double-submit guard is preserved: only a
    PAUSED run resumes, so a second concurrent resume no-ops (400 at the not-paused guard).

    wait=true (opt-in back-compat): launches the resume the SAME way on the request thread,
    then DRAINS the run to a terminal/paused state OFF the loop in a threadpool worker (a
    read-only observe poll) and returns the finished-run shape
    {ok, run_id, status, steps, text|paused_step}. Even this path never blocks the event loop.
    Stream admission is bounded: saturation returns waited:false and current status;
    poll GET /api/runs/{run_id}. The observation ceiling may also return a running run.
    (`steps` counts the whole run's steps here, the observe-replay shape, not just the
    post-resume steps the old inline path reported — a display-only difference.)

    EITHER WAY the edited payload is validated + captured on the REQUEST thread BEFORE any
    launch (prepare_resume_seed): a segment-count mismatch is a clean 400 against a run left
    PAUSED + re-resumable, never a dead background run."""
    ident, org_id, blocked = getattr(request.state, "stream_wait_identity", None) or _gate(request)
    if blocked:
        return blocked
    blocked = _write_block(ident)  # web /runs/{id}/resume is can_write
    if blocked:
        return blocked
    body = await _json(request)
    wait = bool(body.get("wait")) and getattr(request.state, "stream_wait_admitted", True)
    with Session(engine) as s:
        run = s.get(WorkflowRun, run_id)
        if run is None:
            return JSONResponse(_NOT_FOUND, status_code=404)
        doc = s.get(Document, run.document_id)
        if doc is None or _project_in_org(s, doc.project_id, org_id, ident) is None:
            return JSONResponse(_NOT_FOUND, status_code=404)
        if run.status != RunStatus.PAUSED:
            return JSONResponse(
                {"ok": False, "error": f"run {run_id} is not paused (status={run.status})."},
                status_code=400,
            )
        # Issue #493: the run's own target locale is the only locale it can resume in; an
        # explicit different locale in the body is refused rather than silently honoured.
        locale = str(run.target_locale or "").strip()
        _asked = str(body.get("locale") or "").strip()
        if locale and _asked and _asked != locale:
            return JSONResponse(
                {
                    "ok": False,
                    "error": f"run {run_id} targets {locale}; it cannot be resumed as {_asked}.",
                },
                status_code=400,
            )
        locale = locale or _asked or _first_locale(s, run.document_id)
        if not locale:
            return JSONResponse(
                {"ok": False, "error": "could not infer a locale."}, status_code=400
            )
        # HIGH-1 (step isolation) + round-3 wedge guard: refuse a human resume of a run
        # parked at an EXTERNAL vendor step -- approving it by hand would overwrite the
        # vendor's content with the pre-dispatch source. Refuse for an OPEN leg (return via
        # the vendor-return path) AND a WEDGED RETURNED-but-unresumed leg (recover via
        # `vendor redrive`).
        from app.kernel import vendor_dispatch as _vd

        _vleg, _vstate = _vd.vendor_pause_state(s, run_id)
        if _vleg is not None and _vd.blocks_human_resume(_vstate):
            _emsg = {
                "dispatched": (
                    "run is paused at an external vendor step; its content must "
                    "return through the vendor return path, not a human resume."
                ),
                "retry_wait": (
                    "run's external dispatch is being retried after a transient "
                    "failure; it cannot be resumed by hand while the package is "
                    "still trying to go out."
                ),
                "wedged": (
                    "run's external vendor step returned but did not resume "
                    "(wedged); recover it with `vendor redrive`, not a human resume."
                ),
            }[_vstate]
            return JSONResponse({"ok": False, "run_id": run_id, "error": _emsg}, status_code=409)
        # Issue #23 (b): validate the edited payload's segment count BEFORE any state
        # change. `edited_segments` (a JSON array of strings) is the structured path;
        # otherwise `edited_text` (blank = accept the produced MT). On mismatch the run
        # is left PAUSED + re-resumable and we return a clean 400 (not a FAILED run).
        # This ALSO does the LLMemory capture (with the acting identity), so the async
        # launch below passes capture=False to resume_run_async — no double-write.
        raw_segments = body.get("edited_segments")
        try:
            seed_text = orchestrator.prepare_resume_seed(
                s,
                run_id,
                edited_text=str(body.get("edited_text") or ""),
                edited_segments=raw_segments if isinstance(raw_segments, list) else None,
                # LLMemory attribution (adversarial review S20): the operator who
                # submitted these edits is the author of every correction event they
                # produce. Without this the whole resume seam wrote ANONYMOUS
                # provenance while the editor-save seam beside it wrote real names.
                actor=ident["actor"],
            )
        except orchestrator.ResumeMismatch as exc:
            return JSONResponse(
                {"ok": False, "run_id": run_id, "status": "paused", "error": str(exc)},
                status_code=400,
            )
        except ValueError as exc:
            return JSONResponse({"ok": False, "run_id": run_id, "error": str(exc)}, status_code=400)
        # LAUNCH on the request thread (BOTH paths). resume_run_async advances past the human
        # step + spawns the background execution — fast; NOT the minutes-long run. The
        # PAUSED→RUNNING flip is taken under _RUN_CREATE_LOCK inside resume_run_async AND is
        # event-loop-serialized here (this handler is async and does not await before this
        # point), so a rival resume — a default POST or a wait=true worker — cannot double-
        # launch or double-capture: it reads a non-PAUSED row and 400s at the not-paused guard
        # above. capture=False: prepare_resume_seed already captured with the acting identity.
        orchestrator.resume_run_async(
            s, run_id, locale, seed_text, actor=ident["actor"], capture=False
        )
        if not wait:
            # DEFAULT: return the launch ack at once.
            return JSONResponse({"ok": True, "kind": "run", "run_id": run_id, "status": "running"})
    # wait=true: DRAIN the launched run to a terminal/paused state OFF the event loop (the
    # poll can take minutes), then return the finished-run shape. Only the read-only DRAIN is
    # offloaded — the launch already happened on the request thread above. The validation
    # session is closed here; _drain_run_to_terminal opens its own sessions.
    summary = await run_in_threadpool(_drain_run_to_terminal, run_id)
    status = summary.get("status")
    # A resume left 'rejected' (count mismatch) leaves the run PAUSED + re-resumable — report
    # a clean 400, not a terminal failure. (Unreachable in practice: prepare_resume_seed above
    # already count-validated the seed, so the observe-drain never sees a resume_rejected.)
    if status == "rejected":
        return JSONResponse(
            {
                "ok": False,
                "run_id": run_id,
                "status": "paused",
                "steps": len(summary.get("steps", [])),
                "error": summary.get("error") or "resume refused (segment count mismatch)",
            },
            status_code=400,
        )
    # A resume that ends 'failed' (e.g. a failed step) must NOT report ok:true — mirror
    # the CLI fix so an API caller polling `ok` sees the real failure, not a success-lie.
    if status not in ("paused", "completed"):
        err = summary.get("error") or "resume did not complete (run failed)"
        return JSONResponse(
            {
                "ok": False,
                "run_id": run_id,
                "status": status or "failed",
                "steps": len(summary.get("steps", [])),
                "error": err,
            },
            status_code=400,
        )
    out = {"ok": True, "run_id": run_id, "status": status, "steps": len(summary.get("steps", []))}
    if status == "paused":
        out["paused_step"] = summary.get("paused_step")
    else:
        out["text"] = summary.get("text", "")
    return JSONResponse(out)


@router.post("/api/runs/{run_id}/cancel")
async def api_runs_cancel(run_id: int, request: Request):
    """Cancel an in-flight workflow run (pending/running/paused -> CANCELLED). The headless
    twin of POST /runs/{id}/cancel and the CLI `workflow cancel-run` (AUDIT BUG-A205): the
    only escape from a run stuck PAUSED at a human_review / external-vendor step, and the
    cancel that unblocks mid-flight re-authoring. Gated exactly like resume — _gate +
    _write_block, a foreign-org run is an identical 404. An already-terminal run answers a
    clean 409 (never a 500); any open vendor leg is closed CANCELLED so no late return can
    strand the run."""
    ident, org_id, blocked = _gate(request)
    if blocked:
        return blocked
    blocked = _write_block(ident)  # web /runs/{id}/cancel is can_write
    if blocked:
        return blocked
    # POOL-LESS INSIDE A RELIEF SCOPE (gate A round 3, HIGH-4). This route is one of the four
    # the capacity doorman admits at a fully checked-out pool, and it is only useful there if
    # it can actually get a connection: measured before this fix, `POST /api/runs/1/cancel`
    # at 60/60 returned 503 after 5.03s with the run still RUNNING.
    with Session(_db.relief_engine_or(engine)) as s:
        run = s.get(WorkflowRun, run_id)
        if run is None:
            return JSONResponse(_NOT_FOUND, status_code=404)
        doc = s.get(Document, run.document_id)
        if doc is None or _project_in_org(s, doc.project_id, org_id, ident) is None:
            return JSONResponse(_NOT_FOUND, status_code=404)
        try:
            out = orchestrator.cancel_run(s, run_id, actor=ident["actor"])
        except orchestrator.RunCancelError as exc:
            return JSONResponse({"ok": False, "run_id": run_id, "error": str(exc)}, status_code=409)
    return JSONResponse(out)


# ============================================================================
# Jobs (run synchronously / list)  — status is already GET /api/jobs/{jid}
# ============================================================================
# Issue #370 (apiauth-01): the generic door must NOT hand out the internal jobs.HANDLERS
# registry after only the docs scope + can_write -- a translator's docs key could then
# reach install-wide backfill and connector translation outside its role. Each launchable
# kind has to clear the same scope/role/switch checks its DEDICATED launch endpoint
# enforces; a kind absent here answers the same 400 as an unknown name (a newly
# registered handler is NOT reachable through this door until it gets a policy below).
# data_export is internal-only: its row-derived scope belongs to the original owner,
# not the caller of this door. Remote exports must use /api/exports authorization.
_API_JOB_KINDS = frozenset({"translate_document", "connector_translate"})


# Payload keys a connector_translate caller may NOT set through this door: the signed-webhook
# internals (/webhooks/* verifies a per-caller signature before it sets them; here they would
# arrive unsigned, uncapped and aimed at arbitrary external record ids).
_WEBHOOK_ONLY_KEYS = ("webhook_record_ids", "webhook_article_ids", "webhook_ping_pull")


def _job_api_prepare(request: Request, ident, kind: str, payload: dict):
    """Gate AND rebuild a generic /api/jobs/run launch so it is exactly as strict as the
    kind's DEDICATED launch endpoint (issue #370). Returns (blocked, payload): a ready 4xx
    JSONResponse (or None) and the payload to dispatch. The payload is REBUILT from an
    allowlist, never passed through, and every check below calls the SAME function the
    dedicated endpoint uses, so the two doors cannot drift apart again:

      * connector_translate -> can_tune_ai (the /api/connectors/{cid}/* role); payload is
        {connection_id, actor=caller, retry_failures (api_discovery_common.
        checked_retry_failures, the dedicated retry door's count cap + sanitizer),
        jira_translate_pending (bool)}. Webhook-only keys are a 400.
      * translate_document -> api_discovery.translate_launch_gate (viewer refusal, department
        token budget 429, project USD cap 429, mock-engine 409) and translate_locales_block
        (catalog check), the overlap guard, and the server-stamped department / est_tokens /
        provider exactly as POST /api/documents/{id}/translate stamps them.

    The door has ALREADY applied the docs scope + can_write and the payload org + key-document
    rings, so a foreign resource 404s before any role gate could become an existence oracle."""
    if kind == "connector_translate":
        return _prepare_connector_translate(ident, payload)
    if kind == "translate_document":
        return _prepare_translate_document(ident, payload)
    return None, payload


def _prepare_connector_translate(ident, payload: dict):
    blocked = _ai_block(ident)
    if blocked:
        return blocked, payload
    if any(k in payload for k in _WEBHOOK_ONLY_KEYS):
        return (
            JSONResponse(
                {"ok": False, "error": "webhook-only payload keys are not accepted here."},
                status_code=400,
            ),
            payload,
        )
    # Connector attribution names the AUTHENTICATED caller, never a payload-forged actor;
    # the dedicated connector endpoints pass actor=ident["actor"] too.
    clean = {"connection_id": payload.get("connection_id"), "actor": ident["actor"]}
    if payload.get("retry_failures") is not None:
        from app.web.api_discovery_common import checked_retry_failures

        failures, refused = checked_retry_failures(payload["retry_failures"])
        if refused:
            return refused, payload
        clean["retry_failures"] = failures
        # A retry list on this door is always caller-built: on Jira, more than a few
        # named issues is a batch in disguise, admin only (#517 gate).
        blocked = _jira_job_admin_block(
            ident, clean.get("connection_id"), keys=[f.get("issue_key") for f in failures]
        )
        if blocked:
            return blocked, payload
    if payload.get("jira_translate_pending"):
        clean["jira_translate_pending"] = True
    if "retry_failures" not in clean or clean.get("jira_translate_pending"):
        # No retry list (the pending flag wins over a retry list in the worker) means the
        # job runs the saved batch (a full sync, or a pending release): on a Jira
        # connection that is paid and org-wide, admin only, exactly like
        # POST /api/connectors/{cid}/sync (#517).
        blocked = _jira_job_admin_block(ident, clean.get("connection_id"))
        if blocked:
            return blocked, payload
    return None, clean


def _jira_job_admin_block(ident, connection_id, keys=None):
    """keys=None: the job runs a saved batch (admin only on Jira). keys=[...]: a
    caller-built retry list, admin only past the named-issue cap."""
    from app.kernel.connectors import store as conn_store
    from app.web.api_discovery import _jira_batch_admin_block, _jira_named_keys_block

    try:
        # The SAME lookup the worker does (jobs._run_connector_translate passes the raw
        # payload value), so a "5.0" or "5" spelling cannot dodge the gate yet still
        # resolve to the Jira connection at run time.
        conn = conn_store.get(connection_id)
    except Exception:
        conn = None
    if not conn:
        return None
    if keys is None:
        return _jira_batch_admin_block(ident, conn.kind)
    return _jira_named_keys_block(ident, conn.kind, keys)


def _prepare_translate_document(ident, payload: dict):
    from app.web import api_discovery as _disc

    doc_id, locale = payload.get("document_id"), payload.get("locale")
    if not isinstance(doc_id, int) or not isinstance(locale, str) or not locale.strip():
        return (
            JSONResponse(
                {"ok": False, "error": "translate_document needs document_id and locale."},
                status_code=400,
            ),
            payload,
        )
    locale = locale.strip()
    refused = _disc.translate_locales_block([locale])
    if refused:
        return refused, payload
    caller_provider = payload.get("provider")
    caller_provider = caller_provider.strip() if isinstance(caller_provider, str) else ""
    with Session(engine) as s:
        doc = s.get(Document, doc_id)
        project, denied = _disc._doc_authz(s, ident, doc)
        if denied:
            return denied, payload
        # one language, one spelling: a target the project already has is used in its spelling
        locale = _disc.project_locale_spelling(s, doc.project_id, locale)
        blocked, gate = _disc.translate_launch_gate(
            s, ident, doc, project, [locale], caller_provider
        )
        if blocked:
            return blocked, payload
        # Resolve while the session is open, before seeding commits and expires
        # the project captured by provider_for (same ordering as the dedicated door).
        eff = gate["provider_for"](locale)
        blocked = _disc.prepare_translate_locale(s, doc, locale)
        if blocked:
            return blocked, payload
        est = max(
            1,
            sum(
                len(st or "")
                for st in s.exec(
                    select(Segment.source_text).where(
                        Segment.document_id == doc_id, Segment.target_locale == locale
                    )
                ).all()
            )
            // 4,
        )
    # Overlap guard: never stack a second paid-MT run on the same (document, locale).
    if locale in _disc._active_translate_locales(doc_id):
        return (
            JSONResponse(
                {"ok": False, "error": "a translate job for this document and locale is active."},
                status_code=409,
            ),
            payload,
        )
    clean = {"document_id": doc_id, "locale": locale, "est_tokens": est}
    if gate["dept"]:
        clean["department"] = gate["dept"]
    if eff != gate["project_default"]:
        clean["provider"] = eff
    return None, clean


@router.post("/api/jobs/run")
async def api_jobs_run(request: Request):
    """Run a job kind SYNCHRONOUSLY (jobs.run_sync), mirroring `job run`. The job is
    attributed to the caller so it lands in their org (jobs.job_org_id reads the
    creator's org). Payloads that name a document/project are org-checked."""
    ident, org_id, blocked = _gate(request)
    if blocked:
        return blocked
    # A job run mutates state (translate_document / connector_translate
    # both write) -- gate as a write action (parity with the
    # web translate/run paths, which require can_write).
    blocked = _write_block(ident)
    if blocked:
        return blocked
    body = await _json(request)
    kind = str(body.get("kind") or "")
    payload = body.get("payload") or {}
    if kind not in _API_JOB_KINDS:
        return JSONResponse({"ok": False, "error": f"unknown job kind: {kind}"}, status_code=400)
    if not isinstance(payload, dict):
        return JSONResponse({"ok": False, "error": "payload must be an object."}, status_code=400)
    # Org-guard any project/document the payload references so a caller can't run a
    # job against another org's rows.
    denied = _payload_in_org(payload, org_id, ident)
    if denied:
        return denied
    # ...and an INTERNAL KEY DOCUMENT is not found here either, whatever the org
    # gate's state (fix round 2, W1(c)/A-F3).
    denied = _payload_key_document_block(payload)
    if denied:
        return denied
    # Per-kind gate + payload rebuild, delegating to the dedicated endpoint's own validators
    # (#370). Runs AFTER the org + key-document rings so a foreign resource still 404s before
    # a role gate could turn it into a 403 existence oracle.
    blocked, payload = _job_api_prepare(request, ident, kind, payload)
    if blocked:
        return blocked
    # A translate_document job runs for minutes: never on the event loop (#495).
    try:
        job = await offload_parse(jobs.run_sync, kind, payload, actor=ident["actor"])
    except WorkerBusy as exc:
        return busy_response(str(exc))
    d = jobs.status_dict(job)
    ok = job.status == JobStatus.COMPLETED
    return JSONResponse({"ok": ok, **d}, status_code=200 if ok else 400)


@router.get("/api/jobs")
def api_jobs_list(
    request: Request,
    kind: str = "",
    status: str = "",
    actor: str = "",
    type: str = "",
    include_runs: bool = False,
    limit: int = 50,
    offset: int = 0,
    document: str = "",
    workflow: str = "",
):
    """Unified execution history (background jobs + optionally workflow runs), scoped,
    filtered and paginated. Mirrors `job list [--include-runs]` and the web /jobs page
    by calling the SAME job_history.unified_history seam with the caller's own User
    (its org + customer + membership confinement), so all three surfaces stay in
    lockstep on scope."""
    ident, _org_id, blocked = _gate(request)
    if blocked:
        return blocked
    # Perf/DoS cap: unified_history's org gate is N+1 per pseudo-actor row, so an
    # unbounded caller-supplied limit is a self-DoS vector. Clamp; callers page.
    limit = max(1, min(limit, 500))
    # Type: explicit `type` wins; else include_runs -> both, default jobs-only (keeps
    # the historical `GET /api/jobs` = jobs-only shape for existing callers).
    type_ = (type or "").strip().lower() or ("all" if include_runs else "job")
    # Document/workflow filters: scoped-existence (an out-of-scope or unknown id is
    # confined out by the caller's own User, so it yields an empty list — never an
    # existence oracle). 0 = unset (a positive id is a real filter).
    # #603: blank = unset (the web pager and filter form send blank params); a non-blank
    # non-integer is the usual 422, as before.
    doc_f = optional_id_param("document", document)
    wf_f = optional_id_param("workflow", workflow)
    with Session(engine) as session:
        rows, total = job_history.unified_history(
            session,
            _creator(ident),
            type=type_,
            status=status,
            kind=kind,
            actor=actor,
            limit=limit,
            offset=offset,
            document_id=doc_f,
            workflow_id=wf_f,
        )
    # Single shared serializer (job_history.HistoryRow.as_api_dict) so /api and BOTH
    # CLI arms (in-process + --remote) emit an identical row shape and never drift.
    out = [r.as_api_dict() for r in rows]
    return JSONResponse({"ok": True, "total": total, "jobs": out})


def _first_locale(s: Session, doc_id: int):
    locs = sorted(
        {
            sg.target_locale
            for sg in s.exec(select(Segment).where(Segment.document_id == doc_id)).all()
        }
    )
    return locs[0] if locs else None


def _payload_key_document_block(payload: dict):
    """404 a job run whose payload names an INTERNAL KEY DOCUMENT, or None.

    The document routes hide a key project's internal documents behind
    `_visible_document`, but `POST /api/jobs/run` takes a document id in a PAYLOAD,
    not in a path — so `{"kind": "translate_document", "payload": {"document_id":
    <trash>}}` reached the key internals with a plain DOCS-scoped token and
    translated a soft-deleted key's parked values (fix round 2, W1(c); same class as
    A-F3, whose whole point is that `keys` is a SEPARATE grant from `docs`).

    UNCONDITIONAL, unlike `_payload_in_org`: this is not a tenancy wall that a
    single-tenant install can no-op: internal key documents are not addressable
    through the document surfaces for ANY caller. Answers the shared `_NOT_FOUND`
    body so a probe cannot tell an internal document from a missing one.

    `jobs._run_translate_document` carries the same refusal at the SERVICE seam (a
    clean FAILED job) so the CLI's `job run` arm is honest too; this one exists so
    the HTTP surface answers 404 before any work starts.

    `document_id` is caller-supplied JSON, so it can be a malformed shape rather than
    a plain int: a dict or list makes `Session.get` raise `InvalidRequestError` (it
    can't formulate a primary key from a non-scalar), and an oversized int makes the
    sqlite3 driver raise `OverflowError` binding the query parameter -- either one
    would otherwise be a raw 500 for an authenticated docs-scoped caller. Coercing
    with `int()` and resolving to no document on any failure just means THIS block
    has nothing to say about the id; the caller falls through to `jobs.run_sync`,
    which answers with its own clean not-found failure."""
    did = payload.get("document_id")
    if did is None:
        return None
    try:
        did = int(did)
        with Session(engine) as s:
            doc = s.get(Document, did)
    except (ValueError, TypeError, OverflowError):
        return None
    if doc is not None and doc.fmt == keys_svc.KEY_DOC_FMT:
        return JSONResponse(_NOT_FOUND, status_code=404)
    return None


def _payload_in_org(payload: dict, org_id, ident=None):
    """404 a job run whose payload references an org-bound object in another org.
    R2-3: also 404 when a customer-bound caller (ident) references a project/
    document/connection outside its customer — the customer confinement flows
    through the same wall as org.

    Every job kind's payload is audited for org-bound ids and gated here (the job
    handlers have NO internal org gate, so this is the ONLY wall):
      * translate_document -> document_id / project_id;
      * connector_translate -> connection_id (Finding 1: the connector run executes
        the connection's creds + external Jira/Airtable/Confluence + MT budget with
        no internal org check, so an org-A docs-scoped token must NOT be able to run
        org-B's connection). Mirrors api._conn_in_org: 404 when the connection is
        missing OR belongs to another org (a foreign id must be indistinguishable
        from a missing one).
    No-op with the gate off AND no customer binding (org_id None, ident staff)."""
    # ffadmin Wave 5: a connector_translate job against a DISABLED-kind connection is
    # 404'd BEFORE it is accepted -- and this runs ABOVE the single-tenant early-return
    # below, so it fires on EVERY install (org gate off included). Without it, a
    # single-tenant install (org_id None + no customer binding) would accept the job, the
    # deep seam would stop egress, but the deep-seam error body (naming the kind + setting)
    # would leak back as an existence oracle. Non-enumerable at the door instead.
    for field in ("connection_id", "project_id", "document_id"):
        if payload.get(field) is not None:
            value = _int_id(payload[field])
            if value is None:
                return JSONResponse(
                    {"ok": False, "error": f"{field} must be an id."}, status_code=400
                )
            payload[field] = value
    cid0 = payload.get("connection_id")
    if cid0 is not None:
        from app.kernel import connectors as _connreg
        from app.kernel.connectors import store as _cs

        conn0 = _cs.get(cid0)
        if conn0 is not None and not _connreg.kind_enabled(conn0.kind):
            return JSONResponse(_NOT_FOUND, status_code=404)
    bound_cust = _cust_of(ident)
    if org_id is None and bound_cust is None and not security.auth_required():
        return None  # nothing to confine (internal staff, single-tenant)
    with Session(engine) as s:
        pid = payload.get("project_id")
        if pid is not None and _project_in_org(s, pid, org_id, ident) is None:
            return JSONResponse(_NOT_FOUND, status_code=404)
        did = payload.get("document_id")
        if did is not None:
            doc = s.get(Document, did)
            if doc is None or _project_in_org(s, doc.project_id, org_id, ident) is None:
                return JSONResponse(_NOT_FOUND, status_code=404)
    cid = payload.get("connection_id")
    if cid is not None:
        from app.kernel.connectors import store as conn_store

        conn = conn_store.get(cid)
        if conn is None:
            return JSONResponse(_NOT_FOUND, status_code=404)
        if org_id is not None and getattr(conn, "org_id", None) != org_id:
            return JSONResponse(_NOT_FOUND, status_code=404)
        # R2-3: a bound caller may run only its own customer's (or a global) connection.
        conn_cust = getattr(conn, "customer_id", None)
        if bound_cust is not None and conn_cust is not None and conn_cust != bound_cust:
            return JSONResponse(_NOT_FOUND, status_code=404)
        if conn.project_id is not None:
            with Session(engine) as s:
                if _project_in_org(s, conn.project_id, org_id, ident) is None:
                    return JSONResponse(_NOT_FOUND, status_code=404)
    return None
