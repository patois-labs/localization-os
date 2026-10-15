"""/api routes: workflows 2

Documented in: docs/architecture/surfaces-and-parity.md
"""

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from sqlmodel import Session, select

from app.db import engine
from app.kernel import (
    audit,
    batch_runs,
    orchestrator,
    workflows,
)
from app.models import (
    Project,
    Segment,
)
from app.web import deps
from app.web.api_cli_common import (
    _NOT_FOUND,
    _ai_block,
    _batch_wait_response,
    _cust_of,
    _drain_run_to_terminal,
    _gate,
    _grp_in_org,
    _int_id,
    _json,
    _project_in_org,
    _scrub_spend,
    _shared_write_block,
    _stream_wait_route,
    _visible_document,
    _wf_in_org,
    _write_block,
)
from app.web.api_cli_workflows_1 import _api_wf_step_or_none
from app.web.streaming import run_sync as run_in_threadpool

router = APIRouter()


@router.post("/api/workflows/{wid}/steps/{sid}/delete")
async def api_workflows_delete_step(wid: int, sid: int, request: Request):
    """Delete a step (mirrors `workflow delete-step`). can_tune_ai (an authoring edit,
    like the web delete_step route -- NOT the is_admin gate whole-workflow delete
    uses). delete_step is fail-closed on an in-flight run or a delete that would orphan
    a dependent guard/branch -> a clean 400."""
    ident, org_id, blocked = _gate(request)
    if blocked:
        return blocked
    blocked = _ai_block(ident)  # web POST /workflows/{id}/steps/{sid}/delete is _ai_block
    if blocked:
        return blocked
    with Session(engine) as s:
        wf = _wf_in_org(s, wid, org_id, ident)
        if wf is None:
            return JSONResponse(_NOT_FOUND, status_code=404)
        blocked = _shared_write_block(ident, getattr(wf, "customer_id", None))
        if blocked:
            return blocked
        # A foreign / missing step id is a 404 (so delete_step's silent no-op path is
        # never an existence oracle for another workflow's step ids).
        if _api_wf_step_or_none(s, wf, sid) is None:
            return JSONResponse(_NOT_FOUND, status_code=404)
        try:
            workflows.delete_step(s, sid, workflow_id=wid)
        except workflows.WorkflowError as exc:
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
        cust = wf.customer_id
    audit.log(
        actor=ident["actor"],
        action="workflow.delete_step",
        target=f"workflow {wid} step {sid}",
        ip=deps._ip(request),
        customer_id=cust,
    )
    return JSONResponse({"ok": True, "deleted": True, "step_id": sid, "workflow_id": wid})


@router.post("/api/workflows/{wid}/steps/{sid}/move")
async def api_workflows_move_step(wid: int, sid: int, request: Request):
    """Reorder a step (mirrors `workflow move-step`). Body: `position` (0-based) OR
    `before` / `after` (a step key or id, resolved server-side via the SHARED
    resolve_step_ref), plus optional `parallel_group` (join/switch) or
    `clear_parallel_group` (leave) -- move + lane change validated together in ONE
    reorder_step call. Fail-closed on an in-flight run or a move that would break the
    graph -> a clean 400 carrying the validator's message verbatim."""
    ident, org_id, blocked = _gate(request)
    if blocked:
        return blocked
    blocked = _ai_block(ident)
    if blocked:
        return blocked
    body = await _json(request)
    with Session(engine) as s:
        wf = _wf_in_org(s, wid, org_id, ident)
        if wf is None:
            return JSONResponse(_NOT_FOUND, status_code=404)
        blocked = _shared_write_block(ident, getattr(wf, "customer_id", None))
        if blocked:
            return blocked
        steps = orchestrator.get_steps(s, wid)
        if not any(st.id == sid for st in steps):
            return JSONResponse(_NOT_FOUND, status_code=404)
        rest = [st for st in steps if st.id != sid]
        if body.get("position") is not None:
            pos = body.get("position")
        elif body.get("to_position") is not None:
            pos = body.get("to_position")
        elif body.get("before") is not None or body.get("after") is not None:
            before, after = body.get("before"), body.get("after")
            ref = before if before is not None else after
            try:
                at = workflows.resolve_step_ref(rest, str(ref))
            except workflows.WorkflowError as exc:
                return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
            if at is None:
                return JSONResponse(
                    {
                        "ok": False,
                        "error": f"no step matches {ref!r} in workflow {wid} (give another step's "
                        "key or id; the step being moved is not a valid anchor).",
                    },
                    status_code=400,
                )
            pos = at if before is not None else at + 1
        else:
            return JSONResponse(
                {"ok": False, "error": "pass position (0-based), before, or after."},
                status_code=400,
            )
        kw: dict = {}
        if body.get("clear_parallel_group"):
            kw["parallel_group"] = None
        elif "parallel_group" in body:
            v = body["parallel_group"]
            kw["parallel_group"] = (str(v).strip() or None) if v is not None else None
        report: dict = {}
        try:
            moved = workflows.reorder_step(s, wid, sid, pos, report=report, **kw)
        except workflows.WorkflowError as exc:
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
        order = [
            {
                "id": st.id,
                "position": st.order_index,
                "key": st.key,
                "name": st.name,
                "step_type": st.step_type,
            }
            for st in orchestrator.get_steps(s, wid)
        ]
        prop = {
            k: report.get(k, [])
            for k in (
                "propagated",
                "pinned",
                "skipped_adapted",
                "validation_skipped",
                "run_active_skipped",
                "propagated_as_singleton",
            )
        }
        payload = {
            "ok": True,
            "step_id": moved.id,
            "workflow_id": wid,
            "position": moved.order_index,
            "order": order,
            "parallel_group": moved.parallel_group,
            "noop": bool(report.get("noop")),
            "tm_first_note": report.get("tm_first_note", ""),
            "auto_pinned": bool(report.get("auto_pinned")),
            "propagation": prop,
        }
        cust = wf.customer_id
    audit.log(
        actor=ident["actor"],
        action="workflow.move_step",
        target=f"workflow {wid} step {sid}",
        detail="to position {}".format(payload["position"]),
        ip=deps._ip(request),
        customer_id=cust,
    )
    return JSONResponse(payload)


def _run_workflow_blocking(workflow_id: int, doc_id: int, locale: str, actor: str) -> dict:
    """Launch a workflow via `start_run` and POLL it to a terminal state — the `wait=true`
    worker. Returns the finished-run summary shape.

    Called ONLY via `run_in_threadpool` (a worker thread), so nothing here runs on the
    FastAPI event loop (AUDIT Q2).

    F1 (fix round). This USED to call `orchestrator.run_workflow`, which creates a FRESH
    WorkflowRun with NO `_RUN_CREATE_LOCK` and NO double-submit re-attach. Because MCP's
    run_workflow tool ALWAYS sends wait=true, two concurrent same-(wf, doc, locale) calls
    each executed the SAME segment set on two truly-parallel threadpool worker threads —
    SQLite "database is locked", duplicated provider spend, two runs racing writes over one
    set of Segment rows. That is EXACTLY the race `_RUN_CREATE_LOCK` exists to prevent, and
    it was worse than the pre-Q2 code (which the single event loop at least serialized).
    Routing through `start_run` gives the wait path the IDENTICAL dedup the default async
    path has: the second concurrent launch RE-ATTACHES to the first run (staleness floor +
    archive/key-trash refusals preserved) and both callers poll that ONE run to its finish.

    A dedicated Session because a SQLite/SQLModel Session is not safe across threads and the
    request-thread session that validated the launch is already closed by the time we get
    here. `start_run` raises Project/WorkflowArchivedError EAGERLY (before any run row), so
    the wait path refuses an archived launch cleanly, exactly like the async path — the
    exception propagates out of the await."""
    with Session(engine) as s:
        run_id = orchestrator.start_run(s, workflow_id, doc_id, locale, created_by=actor)
    return _drain_run_to_terminal(run_id)


def _run_batch_blocking(
    workflow_id: int, doc_id: int, locales, actor: str, max_parallel, fail_fast: bool
) -> int:
    """Run a whole batch to finalization in a FRESH session — the run-batch `wait=true`
    worker. Called ONLY via `run_in_threadpool`; `start_batch(synchronous=True)` blocks
    the calling (worker) thread until every member is terminal, so this never runs on the
    event loop (AUDIT Q2). Returns the batch id; the route re-reads it for the response
    shape. Raises batch_runs.BatchError on an invalid request (caller maps to 400).

    F1 (fix round) — no per-member race here, unlike the single-run wait path. The single
    /run wait path had to be rerouted because `orchestrator.run_workflow` holds no lock;
    `start_batch(synchronous=True)` runs `batch_runs._dispatch`, and EVERY member's
    check-and-create is serialized under the SAME `orchestrator._RUN_CREATE_LOCK` (with the
    already-running re-attach/skip guard) regardless of the synchronous flag — the async
    daemon path and this synchronous path share that dispatcher verbatim. So two concurrent
    wait=true batches over one (wf, doc) cannot double-run any (wf, doc, locale): the second
    batch's members see the first's committed members and skip. The review's fallback ("take
    the same lock/guard around the synchronous batch") is therefore already satisfied inside
    the dispatcher; this worker just needs to stay OFF the event loop, which it does."""
    with Session(engine) as s:
        batch = batch_runs.start_batch(
            s,
            workflow_id=workflow_id,
            doc_id=doc_id,
            locales=locales,
            created_by=actor,
            max_parallel=max_parallel,
            fail_fast=fail_fast,
            synchronous=True,
        )
        return batch.id


@router.post("/api/workflows/{wid}/run")
@_stream_wait_route
async def api_workflows_run(wid: int, request: Request):
    """Launch a workflow over a document/locale and return a launch ACK immediately.

    CONTRACT CHANGE (2026-08-11, AUDIT-core-qa-part1 Q2 — a DELIBERATE breaking change).
    This route used to run the ENTIRE workflow inline with `orchestrator.run_workflow`,
    an `async def` awaiting a BLOCKING, minutes-to-hours function directly on the event
    loop. That starved the loop: a single remote run made `/ready`, the Fly health check
    and every other request block for the run's whole duration (observed live: HTTP 503
    everywhere, health CRITICAL), and a remote client that timed out at ~60s ABANDONED a
    run that kept executing unobserved. The web route (`POST /documents/{id}/run`) and
    `POST /api/projects/{pid}/keys/translate` already solved this via
    `orchestrator.start_run` (background thread, returns at once); this route was the
    outlier that blocked. THE INVARIANT: the orchestrator's blocking execution must NEVER
    run on the FastAPI event loop.

    DEFAULT (async launch): `start_run` creates the run row + executes on a BACKGROUND
    THREAD and this returns {ok, kind:"run", run_id, status} at once — status is
    "running" (fresh, or re-attached to a running run) or "paused" (re-attached to a
    paused human-review run), NOT the finished run. Poll GET /api/runs/{run_id} to a
    terminal state (the remote CLI `workflow run` does this automatically). start_run's
    double-submit re-attach + staleness floor + archive/key-trash refusals are preserved.

    wait=true: bounded admission on the dedicated stream limiter, then observation
    off the loop up to the observation ceiling. Saturation may return a running
    launch with waited:false and retry_after; poll GET /api/runs/{run_id} (MCP:
    work_status). An admitted wait returns the observed state, which may still be
    running at its ceiling. Default is async; wait is explicit opt-in.

    The document's project must be in the caller's org, and the workflow too."""
    ident, org_id, blocked = getattr(request.state, "stream_wait_identity", None) or _gate(request)
    if blocked:
        return blocked
    blocked = _write_block(ident)  # web /documents/{id}/run (start_run) is can_write
    if blocked:
        return blocked
    body = await _json(request)
    doc_id, locale = body.get("doc_id"), str(body.get("locale") or "").strip()
    wait = bool(body.get("wait")) and getattr(request.state, "stream_wait_admitted", True)
    from app.kernel import content as _content_svc
    from app.kernel import workflows as _wf_service

    with Session(engine) as s:
        wf = _wf_in_org(s, wid, org_id, ident)
        if wf is None:
            return JSONResponse(_NOT_FOUND, status_code=404)
        doc = _visible_document(s, doc_id)
        if doc is None or _project_in_org(s, doc.project_id, org_id, ident) is None:
            return JSONResponse(_NOT_FOUND, status_code=404)
        present = {
            sg.target_locale
            for sg in s.exec(select(Segment).where(Segment.document_id == doc_id)).all()
        }
        if locale not in present:
            return JSONResponse(
                {"ok": False, "error": f"locale {locale!r} not on document {doc_id}."},
                status_code=400,
            )
        # P5 dispatch: resolve the multilingual-group variant for this pair before
        # running (orchestrator signature unchanged; ungrouped -> itself, unchanged).
        proj = s.get(Project, doc.project_id)
        run_wid = _wf_service.resolve_variant(
            s,
            wid,
            locale,
            proj.source_locale if proj else None,
            doc_customer_id=getattr(proj, "customer_id", None),
        )
        resolved_wf = _wf_in_org(s, run_wid, org_id, ident)
        if resolved_wf is None:
            return JSONResponse(_NOT_FOUND, status_code=404)
        resolution = {
            "requested_workflow_id": wid,
            "workflow_id": run_wid,
            "workflow_name": resolved_wf.name,
        }
        if not wait:
            # DEFAULT: async launch on a background thread; return the ack at once.
            # start_run raises Project/WorkflowArchivedError eagerly (before a run row)
            # so an archived launch is a clean 400, not a run that dies on a thread.
            info: dict = {}
            try:
                run_id = orchestrator.start_run(
                    s, run_wid, doc_id, locale, created_by=ident["actor"], detail=info
                )
            except orchestrator.ProjectBudgetExhausted as exc:
                return JSONResponse({"ok": False, "error": str(exc)}, status_code=429)
            except (
                _content_svc.ProjectArchivedError,
                _wf_service.WorkflowArchivedError,
            ) as exc:
                return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
            out = {
                "ok": True,
                **resolution,
                "kind": "run",
                "run_id": run_id,
                "status": "queued"
                if info.get("queued")
                else "paused"
                if info.get("paused")
                else "running",
                "queued": bool(info.get("queued")),
                "reattached": bool(info.get("reattached")),
            }
            if info.get("superseded"):
                out["superseded_run_id"] = info["superseded"]
            return JSONResponse(out)
    # wait=true: execute OFF the event loop, in a threadpool worker (fresh session), then
    # return the finished-run shape. The validation session above is closed here.
    try:
        summary = await run_in_threadpool(
            _run_workflow_blocking, run_wid, doc_id, locale, ident["actor"]
        )
    except orchestrator.ProjectBudgetExhausted as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=429)
    except (
        _content_svc.ProjectArchivedError,
        _wf_service.WorkflowArchivedError,
    ) as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
    status = summary.get("status")
    out = {
        "ok": status not in ("failed", "rejected"),
        **resolution,
        "kind": "run",
        "run_id": summary.get("run_id"),
        "status": status,
        "steps": len(summary.get("steps", [])),
    }
    if status == "paused":
        out["paused_step"] = summary.get("paused_step")
    elif status in ("failed", "rejected"):
        out["error"] = summary.get("error")
    else:
        out["text"] = summary.get("text", "")
    return JSONResponse(out)


@router.post("/api/workflows/{wid}/run-batch")
@_stream_wait_route
async def api_workflows_run_batch(wid: int, request: Request):
    """Fan-out a workflow across a document's target locales as one batch (Phase 5a).

    CONTRACT CHANGE (2026-08-11, AUDIT-core-qa-part1 Q2 — same DoS class as /run). This
    used to fan out SYNCHRONOUSLY (start_batch(synchronous=True) joins every member
    thread) directly on the event loop, so one batch DoSed the whole instance for its
    duration. The web /documents/{id}/run-batch route already dispatches ASYNC
    (synchronous=False → a daemon dispatcher thread, redirect to the monitor); this route
    now mirrors it. THE INVARIANT: batch execution never runs on the FastAPI event loop.

    DEFAULT (async): start_batch spawns the daemon dispatcher and this returns the
    batch_as_dict shape at once — status RUNNING (finished_at unset), counts still
    filling. Poll GET /api/batches/{id} to a terminal status (the remote CLI does this).
    max_parallel / fail_fast / locale fan-out semantics are unchanged, just launched
    async. wait=true uses bounded stream admission. Saturation returns waited:false
    and a current, possibly running matrix; poll GET /api/batches/{id}. An admitted
    wait runs dispatch off the loop and returns the finalized batch. Same auth/scope as
    POST /api/workflows/{wid}/run: docs-scoped lsu_ key, org+customer ring, can_write.

    Body: {doc_id, locales?: [..] (omit = ALL the doc's targets), max_parallel?,
    fail_fast?, wait?}. Returns the batch_as_dict shape (batch fields + members[] +
    counts) — the SAME shape GET /api/batches/{id} returns and the CLI --json prints."""
    ident, org_id, blocked = getattr(request.state, "stream_wait_identity", None) or _gate(request)
    if blocked:
        return blocked
    blocked = _write_block(ident)  # web /documents/{id}/run-batch is can_write
    if blocked:
        return blocked
    body = await _json(request)
    doc_id = body.get("doc_id")
    wait = bool(body.get("wait")) and getattr(request.state, "stream_wait_admitted", True)
    raw_locales = body.get("locales")
    locales = [str(x) for x in raw_locales] if isinstance(raw_locales, list) else None
    with Session(engine) as s:
        wf = _wf_in_org(s, wid, org_id, ident)
        if wf is None:
            return JSONResponse(_NOT_FOUND, status_code=404)
        doc = _visible_document(s, doc_id)
        if doc is None or _project_in_org(s, doc.project_id, org_id, ident) is None:
            return JSONResponse(_NOT_FOUND, status_code=404)
        if not wait:
            # DEFAULT: async daemon dispatch; return the initial (RUNNING) shape at once.
            try:
                batch = batch_runs.start_batch(
                    s,
                    workflow_id=wid,
                    doc_id=doc_id,
                    locales=locales,
                    created_by=ident["actor"],
                    max_parallel=body.get("max_parallel"),
                    fail_fast=bool(body.get("fail_fast")),
                    synchronous=False,
                )
            except batch_runs.BatchBudgetExhausted as exc:
                return JSONResponse({"ok": False, "error": str(exc)}, status_code=429)
            except batch_runs.BatchError as exc:
                return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
            out = batch_runs.batch_as_dict(s, batch)
            return JSONResponse(_scrub_spend(ident, {"ok": out["status"] != "failed", **out}))
    # wait=true: dispatch to finalization OFF the loop, in a threadpool worker, then read
    # the finalized shape back. The validation session above is closed here.
    try:
        batch_id = await run_in_threadpool(
            _run_batch_blocking,
            wid,
            doc_id,
            locales,
            ident["actor"],
            body.get("max_parallel"),
            bool(body.get("fail_fast")),
        )
    except batch_runs.BatchBudgetExhausted as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=429)
    except batch_runs.BatchError as exc:
        return JSONResponse(
            {"ok": False, "error": str(exc)},
            status_code=404 if isinstance(exc, batch_runs.BatchMissing) else 400,
        )
    return _batch_wait_response(batch_id, ident=ident)


# ============================================================================
# Wave R1b — content-ops parity (SPEC-full-remote-parity Wave R1).
#
# The remaining Wave-R1 content surfaces R1a did not port: workflow GROUP
# create/list/show/add/set-default, prompt-template create/list, workflow
# clone/copy/author, project templates, image upload/translate (in images.py),
# style-guide/context A/B, segmentation profiles and context packages.
#
# EVERY route funnels each customer_id / project_id / workflow_id / group_id /
# template_id / glossary_id / doc_id / profile_id (in PATH OR BODY) through the
# SAME org+customer ring the R1a routes use (_project_in_org / _customer_in_org /
# _wf_in_org / _grp_in_org / _pt_in_org / _visible_document / _style_guide_in_scope
# / _content_type_in_scope), so a foreign object is a 404 (never a 403, never a
# name/existence leak) and a customer-BOUND caller can never reach another
# customer's object. Scope-before-validate: the ring is resolved BEFORE any input
# validation or role gate that could reveal existence (the R1a status/settings
# oracle lesson). RBAC mirrors the web sibling per the spec table. Mutations audit.
# ============================================================================


def _mock_spend_block(provider_name):
    """409 when the effective provider would resolve to the keyless OFFLINE MOCK
    (the day-one trust guard; the literal 'mock' is the explicit dev escape hatch).
    Mirrors the document-translate mock-hard-fail on every REAL-engine spend path
    that has NO project/pair to resolve against (workflow author). A keyless real
    provider (provider_is_live False) is the silent fallback this refuses."""
    from app import config
    from app.kernel import providers

    eff = (str(provider_name or "").strip() or config.DEFAULT_PROVIDER or "").lower()
    if eff != "mock" and not providers.provider_is_live(eff):
        return JSONResponse(
            {
                "ok": False,
                "error": "No translation engine configured - set an API key before submitting.",
            },
            status_code=409,
        )
    return None


# --- workflow clone / copy / author ------------------------------------------
@router.post("/api/workflows/{wid}/clone")
async def api_workflows_clone(wid: int, request: Request):
    """Deep-copy a workflow and its steps (mirrors `workflow clone`). can_tune_ai. Bills
    nothing — a structural copy, no engine call. _wf_in_org rings the source (404); the
    clone inherits the source's org so it stays in the caller's own org."""
    ident, org_id, blocked = _gate(request)
    if blocked:
        return blocked
    blocked = _ai_block(ident)
    if blocked:
        return blocked
    with Session(engine) as s:
        wf = _wf_in_org(s, wid, org_id, ident)
        if wf is None:
            return JSONResponse(_NOT_FOUND, status_code=404)
        # #468: a bound caller's clone of a global workflow is its own customer's copy.
        new = workflows.clone_workflow(s, wid, bound_customer_id=_cust_of(ident))
        if new is None:
            return JSONResponse(_NOT_FOUND, status_code=404)
        n = len(orchestrator.get_steps(s, new.id))
        new_id = new.id
        out = {"ok": True, "id": new_id, "name": new.name, "steps": n, "source_id": wid}
        cust = new.customer_id
    audit.log(
        actor=ident["actor"],
        action="workflow.clone",
        target=f"workflow {new_id} (from {wid})",
        ip=deps._ip(request),
        customer_id=cust,
    )
    return JSONResponse(out)


@router.post("/api/workflows/{wid}/copy")
async def api_workflows_copy(wid: int, request: Request):
    """Copy a workflow onto other language pair(s) inside a group (mirrors `workflow
    copy --to-pairs`). can_tune_ai. Body: to_pairs (["src:tgt", ...] or a comma string),
    adapt (LLM-adapt each step's prompt — failures ride back as per-pair warnings, never
    a hard block), group_id (optional target group). _wf_in_org rings the source; a
    supplied group_id funnels through _grp_in_org so a foreign group is a 404."""
    ident, org_id, blocked = _gate(request)
    if blocked:
        return blocked
    blocked = _ai_block(ident)
    if blocked:
        return blocked
    body = await _json(request)
    with Session(engine) as s:
        # SCOPE-BEFORE-VALIDATE: resolve every tenancy ring FIRST so a foreign wid (or a
        # foreign body group_id) 404s regardless of body shape — an invalid/empty to_pairs
        # body must never turn a foreign-object 404 into a distinguishable 400 (existence
        # oracle). Mirrors api_templates_apply / api_context_set_profile: the ring runs
        # before the 400-producing body validation.
        wf = _wf_in_org(s, wid, org_id, ident)
        if wf is None:
            return JSONResponse(_NOT_FOUND, status_code=404)
        blocked = _shared_write_block(ident, getattr(wf, "customer_id", None))
        if blocked:
            return blocked
        group_id = _int_id(body.get("group_id"))
        if body.get("group_id") is not None and body.get("group_id") != "":  # noqa: SIM102
            if group_id is None or _grp_in_org(s, group_id, org_id, ident) is None:
                return JSONResponse(_NOT_FOUND, status_code=404)
        raw = body.get("to_pairs")
        tokens = raw if isinstance(raw, list) else [p for p in str(raw or "").split(",")]  # noqa: C416
        pairs = []
        for tok in tokens:
            if isinstance(tok, (list, tuple)) and len(tok) == 2:
                s0, t0 = str(tok[0]).strip(), str(tok[1]).strip()
            else:
                parts = [x.strip() for x in str(tok).replace("->", ":").split(":") if x.strip()]
                if len(parts) != 2:
                    if not str(tok).strip():
                        continue
                    return JSONResponse(
                        {
                            "ok": False,
                            "error": f"invalid pair {tok!r} (want src:tgt, e.g. en-US:fr-FR).",
                        },
                        status_code=400,
                    )
                s0, t0 = parts[0], parts[1]
            pairs.append((s0, t0))
        if not pairs:
            return JSONResponse(
                {"ok": False, "error": "to_pairs must list at least one src:tgt pair."},
                status_code=400,
            )
        try:
            res = workflows.copy_to_pairs(
                s,
                wid,
                pairs,
                adapt=bool(body.get("adapt")),
                group_id=group_id,
                created_by=ident["actor"],
                viewer=ident.get("user"),
                # #468: a bound caller's copies are always its own customer's.
                bound_customer_id=_cust_of(ident),
            )
        except workflows.WorkflowError as exc:
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
        cust = wf.customer_id
    audit.log(
        actor=ident["actor"],
        action="workflow.copy",
        target=f"workflow {wid} -> group {res.get('group_id')}",
        detail="created=%d skipped=%d" % (len(res.get("created", [])), len(res.get("skipped", []))),  # noqa: UP031
        ip=deps._ip(request),
        customer_id=cust,
    )
    return JSONResponse({"ok": True, **res})


@router.post("/api/workflows/author")
async def api_workflows_author(request: Request):
    """Draft a WHOLE workflow from a plain-language description (mirrors `workflow
    author`). can_tune_ai AND a SPEND path — it calls an LLM to draft the graph. The
    mock-409 guard refuses BEFORE spending if the authoring provider would resolve to a
    keyless offline mock (the draft would be garbage). Returns the validated draft +
    warnings; with `create: true` it applies the draft through the SAME
    apply_workflow_draft seam the web confirm route uses and returns the new workflow.
    Catalogs (customers/providers/glossaries/locales) are built through the caller's
    OWN visibility seams, so the model can only ever pick what the caller could see; a
    bound caller's draft is confined to its own customer."""
    ident, org_id, blocked = _gate(request)
    if blocked:
        return blocked
    blocked = _ai_block(ident)  # web /workflows/author is _ai_block (can_tune_ai)
    if blocked:
        return blocked
    body = await _json(request)
    description = str(body.get("description") or "").strip()
    if not description:
        return JSONResponse({"ok": False, "error": "description is required."}, status_code=400)
    provider = str(body.get("provider") or "").strip()
    # SPEND guard: refuse a keyless-mock authoring call before it spends (or produces
    # garbage). The explicit literal 'mock' stays the dev escape hatch.
    mock = _mock_spend_block(provider)
    if mock:
        return mock
    from app.kernel import setup_author

    model = str(body.get("model") or "").strip() or None
    with Session(engine) as s:
        try:
            # LLM drafting call: off the event loop (#495).
            draft = await deps.offload_parse(
                setup_author.author_workflow_setup,
                s,
                description=description,
                provider=(provider or None),
                org_id=org_id,
                actor=ident["actor"],
                bound_customer_id=_cust_of(ident),
                model=model,
            )
        except setup_author.SetupAuthorError as exc:
            status = getattr(exc, "status", 400) or 400
            return JSONResponse(
                {"ok": False, "error": str(exc)},
                status_code=status if status in (400, 409, 502) else 400,
            )
        fields = draft["fields"]
        warnings = list(draft.get("warnings") or [])
        # Back-compat (SPEC-language-agnostic-workflows T1): an old client may still send
        # source_locale / target_locales / target_locale / pairs / adapt. They are ignored
        # (a workflow takes its languages from the project it runs on); the draft never
        # carries them. Never a 422 -- the request succeeds with a note in `warnings`.
        # The note fires only on a REAL legacy field: a body that merely carries the key
        # with an empty/false value (adapt: false, source_locale: "", target_locales: [])
        # is a clean modern client, not an old one, so it gets no note.
        _legacy_lang = ("source_locale", "target_locales", "target_locale", "pairs", "adapt")
        if any(body.get(k) for k in _legacy_lang):
            warnings.append(
                "source_locale/target_locales/adapt are ignored: a workflow takes its "
                "languages from the project it runs on."
            )
        if not body.get("create"):
            return JSONResponse(
                {"ok": True, "created": False, "draft": fields, "warnings": warnings}
            )
        strip_warnings: list = []
        try:
            wf = await deps.offload_parse(
                setup_author.apply_workflow_draft,
                s,
                fields,
                org_id=org_id,
                actor=ident["actor"],
                bound_customer_id=_cust_of(ident),
                warnings=strip_warnings,
            )
        except setup_author.WorkflowApplyError as exc:
            return JSONResponse(
                {
                    "ok": False,
                    "error": "; ".join(exc.errors) or str(exc),
                    "errors": list(getattr(exc, "errors", [])),
                },
                status_code=400,
            )
        except setup_author.SetupAuthorError as exc:
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
        n = len(orchestrator.get_steps(s, wf.id))
        out = {
            "ok": True,
            "created": True,
            "id": wf.id,
            "name": wf.name,
            "steps": n,
            "warnings": warnings + strip_warnings,
        }
        cust = wf.customer_id
    audit.log(
        actor=ident["actor"],
        action="workflow.author",
        target=f"workflow {out['id']} '{out['name']}'",
        detail="authored+created via LLM (%d step(s))" % out["steps"],  # noqa: UP031
        ip=deps._ip(request),
        customer_id=cust,
    )
    return JSONResponse(out)


@router.get("/api/workflows/{wid}/plan")
def api_workflows_plan(wid: int, request: Request, doc_id: str = "", locale: str = ""):
    """Read-only spend preview using the same variant resolver as launch."""
    from app.kernel import workflow_plan

    ident, org_id, blocked = _gate(request)
    if blocked:
        return blocked
    with Session(engine) as s:
        # SCOPE-BEFORE-VALIDATE: ring the workflow FIRST so a foreign wid 404s exactly
        # like a missing one, whatever the query string holds. A 400 for a missing /
        # malformed doc_id or locale here would be an existence oracle for other orgs.
        if _wf_in_org(s, wid, org_id, ident) is None:
            return JSONResponse(_NOT_FOUND, status_code=404)
        for name, value in (("doc_id", doc_id), ("locale", locale)):
            if not value.strip():
                return JSONResponse({"ok": False, "error": f"{name} is required"}, status_code=400)
        document_id = _int_id(doc_id)
        if document_id is None:
            return JSONResponse(
                {"ok": False, "error": "doc_id must be a positive integer"}, status_code=400
            )
        doc = _visible_document(s, document_id)
        if doc is None or _project_in_org(s, doc.project_id, org_id, ident) is None:
            return JSONResponse(_NOT_FOUND, status_code=404)
        try:
            out = workflow_plan.plan(s, wid, doc, locale.strip())
        except ValueError as exc:
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
    return JSONResponse({"ok": True, **out})
