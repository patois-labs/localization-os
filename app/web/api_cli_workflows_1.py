"""/api routes: workflows 1

Documented in: docs/architecture/surfaces-and-parity.md
"""

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from sqlmodel import Session, select

from app.db import engine
from app.kernel import (
    access,
    audit,
    orchestrator,
    qe_autofix,
    security,
    workflows,
)
from app.kernel import ids as ids
from app.kernel import workflow_shapes as wshapes
from app.models import (
    Glossary,
    StepType,
    Workflow,
    WorkflowStep,
)
from app.web import deps
from app.web.api_cli_common import (
    _NOT_FOUND,
    _ai_block,
    _api_workflow_customer,
    _cust_of,
    _gate,
    _json,
    _org_list,
    _role_block,
    _shared_write_block,
    _wf_in_org,
)

router = APIRouter()


# ============================================================================
# Workflows (list / show / run / resume)
# ============================================================================
@router.get("/api/workflows")
def api_workflows_list(request: Request):
    ident, org_id, blocked = _gate(request)
    if blocked:
        return blocked
    with Session(engine) as s:
        wq = _org_list(select(Workflow), Workflow, org_id)
        allowed = access.visible_project_ids(s, ident.get("user"))
        if allowed is not None:
            wq = wq.where(Workflow.project_id.is_(None) | Workflow.project_id.in_(allowed))
        # R2-3: a customer-bound caller sees only GLOBAL workflows + its own
        # customer's (mirrors the web /workflows + dispatch scoping).
        cust = _cust_of(ident)
        if cust is not None:
            # GATE R2 MED-1: ownership on customer_id, never scope. The /api twin of the
            # web dashboard leaked the same foreign promoted workflows.
            wq = wq.where(Workflow.customer_id.is_(None) | (Workflow.customer_id == cust))
        # Archive (residual sweep): hide ARCHIVED workflows by default (parity with the web
        # /workflows dashboard + dispatch pickers). `?archived=all`/`only` escape hatch.
        archived = str(request.query_params.get("archived", "")).strip().lower()
        if archived == "only":
            wq = wq.where(Workflow.archived_at.is_not(None))
        elif archived != "all":
            wq = workflows.workflow_active_only(wq)  # shipped seam (M4)
        wfs = s.exec(wq.order_by(Workflow.created_at.desc())).all()
        # Phase 3 parity: the same shape (primary tag) + origin the in-process
        # `workflow list --json` carries, so --remote never drops a field (verify_multiorg
        # P6 shape-parity guard). Wave A: the SAME single-sourced usage + quality stats
        # the dashboard + CLI use, run-scoped to this caller's visible projects (the real
        # User off the identity dict feeds access.visible_project_ids inside the service —
        # a customer-bound key never counts another customer's runs of a shared workflow).
        from app.kernel import workflow_stats

        stats = workflow_stats.stats_for(s, [w.id for w in wfs], user=ident.get("user"))
        payload = []
        for w in wfs:
            steps = orchestrator.get_steps(s, w.id)
            shapes = wshapes.detect_shapes(steps)
            payload.append(
                {
                    "id": w.id,
                    "name": w.name,
                    "scope": w.scope,
                    "steps": len(steps),
                    "shape": shapes.primary,
                    "shape_tags": shapes.sorted_tags(),
                    "created_via": getattr(w, "created_via", None),
                    "archived": w.archived_at is not None,
                    **stats[w.id],
                }
            )
    return JSONResponse({"ok": True, "workflows": payload})


@router.get("/api/workflows/{wid}")
def api_workflows_show(wid: int, request: Request):
    ident, org_id, blocked = _gate(request)
    if blocked:
        return blocked
    with Session(engine) as s:
        wf = _wf_in_org(s, wid, org_id, ident)
        if wf is None:
            return JSONResponse(_NOT_FOUND, status_code=404)
        steps = orchestrator.get_steps(s, wf.id)
        # Phase 3 parity: workflow-level shape (primary tag) + origin, mirroring the
        # in-process `workflow show --json` top-level keys.
        shapes = wshapes.detect_shapes(steps)
        created_via = getattr(wf, "created_via", None)
        # Issue #17/#19 (remote read-back parity): resolve pinned glossary names in
        # one batch query, exactly like the in-process `workflow show` CLI path, so
        # --remote never drops fields the local path has.
        gnames = {}
        gids = {st.glossary_id for st in steps if st.glossary_id is not None}
        if gids:
            gnames = {
                g.id: g.name for g in s.exec(select(Glossary).where(Glossary.id.in_(gids))).all()
            }
        payload = [
            {
                "index": st.order_index,
                "id": st.id,
                "name": st.name,
                "type": st.step_type,
                "provider": st.provider,
                "model": st.model,
                "fallback_provider": st.fallback_provider,
                "fallback_model": st.fallback_model,
                "chunk_policy": st.chunk_policy,
                "redact_pii": st.redact_pii,
                # Issue #19: the pinned glossary (id + name), the step's own
                # prompt/instruction text (or saved template id), and the
                # engine-layer quality toggles -- previously write-only over --remote.
                "glossary_id": st.glossary_id,
                "glossary_name": gnames.get(st.glossary_id) if st.glossary_id else None,
                "prompt_template_id": st.prompt_template_id,
                "prompt_text": st.prompt_text or "",
                "adaptive_feedback": st.adaptive_feedback,
                "adaptive_feedback_window": st.adaptive_feedback_window,
                "doc_context_prepass": st.doc_context_prepass,
                "qe_gate": st.qe_gate,
                "qe_threshold": st.qe_threshold,
                "qe_provider": st.qe_provider,
                # QE auto-fix (Train B): echoed on BOTH show rows in one diff (the CLI<->/api
                # step-row key-set parity guard).
                "qe_autofix": getattr(st, "qe_autofix", False),
                "qe_autofix_status": qe_autofix.display_status(st),
                "qe_autofix_provider": getattr(st, "qe_autofix_provider", None),
                "qe_autofix_max_rounds": getattr(st, "qe_autofix_max_rounds", 3),
                "qe_autofix_cross_check": getattr(st, "qe_autofix_cross_check", False),
                # feat/lqa-configurable-threshold: the lqa step's pass bar (None =
                # the built-in default 90). Echoed so a headless consumer sees what
                # add/edit-step can set (Issue #19 read-surface rule; the P6 row-shape
                # guard asserts key-set parity with `workflow show --json`).
                "lqa_threshold": getattr(st, "lqa_threshold", None),
                "tm_examples": st.tm_examples,
                "tm_examples_window": st.tm_examples_window,
                # Batch 14 T2 (SPEC §25.5-C): the per-step image-context toggle, echoed so
                # /api + MCP get_workflow can read it AND so the CLI<->/api step-row key-set
                # parity guard stays green (the field lands on BOTH show rows in one diff).
                "use_image_context": getattr(st, "use_image_context", True),
                # Terminology T3 (SPEC §9-T3): the concept-glossary renderer toggle + caps,
                # echoed on BOTH show rows in one diff (CLI<->/api key-set parity guard).
                "concept_glossary_render": getattr(st, "concept_glossary_render", False),
                "glossary_concept_cap": getattr(st, "glossary_concept_cap", 10),
                "glossary_token_cap": getattr(st, "glossary_token_cap", 0),
                # LQA safety floor D1: the TM-protection tristate, mirroring the
                # in-process `workflow show --json` row EXACTLY (the P6 row-shape
                # guard asserts key-set parity).
                "tm_protect": getattr(st, "tm_protect", None),
                # run-190 Item D / M5: the over-translation guard opt-out tristate, mirroring
                # the in-process `workflow show --json` row EXACTLY (row-shape parity guard).
                "over_translation_guard": getattr(st, "over_translation_guard", None),
                # LLMemory step opt-out (PROPOSAL-llmemory §6). memory_mode is now
                # writable over /api too (POST .../steps[/{sid}/edit]), so this read
                # row round-trips with the authoring routes.
                "memory_mode": getattr(st, "memory_mode", None),
                "assignee_email": st.assignee_email,
                # QA-9 (#39): shared-base provenance + linguist-review flag, mirroring
                # the in-process `workflow show --json` step row EXACTLY (the P6 row-shape
                # guard asserts key-set parity). adapted_from_id = the source step a
                # cross-language variant was cloned from (NULL on a plain add-step);
                # needs_review = an LLM-adapted step a linguist should post-edit.
                "needs_review": st.needs_review,
                "adapted_from_id": st.adapted_from_id,
                # SPEC §5 pre-step guard (Phase 2): the read surface exposes the
                # step's guard (None = always run), so an API consumer can see it.
                "run_condition": st.run_condition,
                # SPEC §5 branching (Phase 3): the stable key, post-step branch graph,
                # and per-step loop ceiling (all None on a plain linear step).
                "key": st.key,
                "branch": st.branch,
                "max_visits": st.max_visits,
                # SPEC-parallel-routing §6: the parallel-group slug, mirroring the
                # in-process `workflow show --json` row EXACTLY (the P6 row-shape
                # guard asserts key-set parity).
                "parallel_group": getattr(st, "parallel_group", None),
                # Batch 15 T1 (SPEC-tm-match-step-config §8.C): the six tm_match config
                # overrides, mirroring the CLI `workflow show --json` row EXACTLY (the P6
                # key-set parity guard -- they land on BOTH show rows in this one diff).
                # None on every non-tm_match step (dead there); a concrete snapshot on a
                # tm_match step (NULL = inherit the live global).
                "tm_fill_threshold": getattr(st, "tm_fill_threshold", None),
                "tm_fuzzy_lock_mode": getattr(st, "tm_fuzzy_lock_mode", None),
                "tm_lock_ice": getattr(st, "tm_lock_ice", None),
                "tm_hundreds_enter_lqa": getattr(st, "tm_hundreds_enter_lqa", None),
                "tm_context_floor": getattr(st, "tm_context_floor", None),
                "tm_pool_selection": getattr(st, "tm_pool_selection", None),
            }
            for st in steps
        ]
        # B0 (SPEC-workflows-redesign Track B): the per-step PACKAGE/VERIFY object,
        # single-sourced from package_view so this endpoint mirrors the CLI `workflow
        # show --json` step-row shape EXACTLY (row-shape parity). gnames reused; zip
        # aligns because payload is built in `steps` order.
        from app.kernel import package_view as pv

        for row, pview in zip(
            payload, pv.workflow_package_view(s, wf, steps, glossary_names=gnames), strict=False
        ):
            row["package"] = pview["package"]
            row["verify"] = pview["verify"]
    return JSONResponse(
        {
            "ok": True,
            "id": wf.id,
            "name": wf.name,
            "scope": wf.scope,
            "shape": shapes.primary,
            "shape_tags": shapes.sorted_tags(),
            "created_via": created_via,
            "archived": wf.archived_at is not None,  # L11
            "steps": payload,
        }
    )


# --- workflow / group / prompt-template rename + delete (SPEC-rename-delete) ---
# RENAME mirrors the web family gate (_ai_block = can_tune_ai); every NEW DELETE is
# strictly admin (mirrors the web _admin_block via _role_block(is_admin)). Every
# rename logs an old->new audit entry; delete fails closed with an itemized 409.
@router.post("/api/workflows/{wid}/rename")
async def api_workflows_rename(wid: int, request: Request):
    ident, org_id, blocked = _gate(request)
    if blocked:
        return blocked
    blocked = _ai_block(ident)  # web POST /workflows/{id}/rename is can_tune_ai
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
        old_name, customer_id = wf.name, wf.customer_id
        try:
            workflows.rename_workflow(s, wid, str(body.get("name") or ""))
        except workflows.WorkflowError as exc:
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
        new_name = wf.name
    audit.log(
        actor=ident["actor"],
        action="workflow.rename",
        target=f"workflow {wid}",
        detail=f"{old_name!r} -> {new_name!r}",
        ip=deps._ip(request),
        customer_id=customer_id,
    )
    return JSONResponse({"ok": True, "id": wid, "name": new_name})


@router.post("/api/workflows/{wid}/delete")
async def api_workflows_delete(wid: int, request: Request):
    ident, org_id, blocked = _gate(request)
    if blocked:
        return blocked
    with Session(engine) as s:
        # Resolve the object (incl. the R2-3 customer ring) BEFORE the admin gate so a
        # customer-bound caller's FOREIGN target is a 404 (indistinguishable from
        # missing), never a 403 that leaks the row's existence. A bound caller is never
        # admin, so past the ring it still hits the admin gate on its own/global rows.
        wf = _wf_in_org(s, wid, org_id, ident)
        if wf is None:
            return JSONResponse(_NOT_FOUND, status_code=404)
        blocked = _shared_write_block(ident, getattr(wf, "customer_id", None))
        if blocked:
            return blocked
        blocked = _role_block(ident, security.is_admin)  # web delete is _admin_block
        if blocked:
            return blocked
        name, customer_id = wf.name, wf.customer_id
        try:
            refs = workflows.delete_workflow(s, wid)
        except workflows.WorkflowError as exc:
            return JSONResponse(
                {"ok": False, "error": str(exc), "references": exc.refs}, status_code=409
            )
    audit.log(
        actor=ident["actor"],
        action="workflow.delete",
        target=f"workflow {wid} '{name}'",
        detail=(
            "cascaded %d step(s), %d run(s), %d step-run(s), %d batch(es); un-scoped "  # noqa: UP031
            "%d batch member(s), %d eval candidate(s), %d memory event(s)"
            % (
                refs["steps"],
                refs["runs"],
                refs["step_runs"],
                refs.get("workflow_batches", 0),
                refs.get("batch_members_unscoped", 0),
                refs.get("eval_candidates_unscoped", 0),
                refs.get("memory_events_unscoped", 0),
            )
        ),
        ip=deps._ip(request),
        customer_id=customer_id,
    )
    return JSONResponse({"ok": True, "id": wid, "deleted": True, "references": refs})


@router.post("/api/workflows/{wid}/archive")
async def api_workflows_archive(wid: int, request: Request):
    """Remote-CLI/web parity for workflow archive (residual sweep: archive verbs).
    ADMIN-only (same posture as delete). Ring BEFORE role so a foreign id 404s. Retires
    the workflow from listings/pickers/dispatch + blocks new runs; steps + history
    preserved. Idempotent-safe. The service audits workflow.archive."""
    ident, org_id, blocked = _gate(request)
    if blocked:
        return blocked
    with Session(engine) as s:
        wf = _wf_in_org(s, wid, org_id, ident)
        if wf is None:
            return JSONResponse(_NOT_FOUND, status_code=404)
        blocked = _shared_write_block(ident, getattr(wf, "customer_id", None))
        if blocked:
            return blocked
        blocked = _role_block(ident, security.is_admin)
        if blocked:
            return blocked
        try:
            workflows.archive_workflow(
                s, wid, actor=ident["actor"], ip=deps._ip(request), org_id=org_id
            )
        except workflows.WorkflowError as exc:
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
    return JSONResponse({"ok": True, "id": wid, "archived": True})


@router.post("/api/workflows/{wid}/unarchive")
async def api_workflows_unarchive(wid: int, request: Request):
    """Remote-CLI/web parity for workflow unarchive (residual sweep). ADMIN-only. Restores
    the workflow to listings/pickers/dispatch + makes it runnable. Ring before role. The
    service audits workflow.unarchive."""
    ident, org_id, blocked = _gate(request)
    if blocked:
        return blocked
    with Session(engine) as s:
        wf = _wf_in_org(s, wid, org_id, ident)
        if wf is None:
            return JSONResponse(_NOT_FOUND, status_code=404)
        blocked = _shared_write_block(ident, getattr(wf, "customer_id", None))
        if blocked:
            return blocked
        blocked = _role_block(ident, security.is_admin)
        if blocked:
            return blocked
        try:
            workflows.unarchive_workflow(
                s, wid, actor=ident["actor"], ip=deps._ip(request), org_id=org_id
            )
        except workflows.WorkflowError as exc:
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
    return JSONResponse({"ok": True, "id": wid, "archived": False})


def _api_wf_step_or_none(s: Session, wf, sid):
    """The WorkflowStep `sid` belonging to workflow `wf`, or None (a foreign / missing
    step id is a 404, indistinguishable from a workflow the caller cannot see)."""
    step = s.get(WorkflowStep, sid)
    if step is None or step.workflow_id != wf.id:
        return None
    return step


def _api_glossary_pin(s: Session, wf, org_id, ident, glossary_id):
    """Resolve a step glossary pin exactly as the in-process CLI add-step/edit-step do:
    it MUST be an APPROVED glossary VISIBLE to this caller (a workflow is language-
    agnostic, so every approved glossary is offerable; a pin that does not cover a run's
    languages is skipped at run time with a note). Returns (gid, err); err is a ready
    400 JSONResponse, else None. A hard refusal (not the web's silent drop) so the
    REMOTE CLI surfaces the same clear error its in-process twin raises."""
    gid_raw = ids.coerce_id(glossary_id)
    if gid_raw is None:
        return None, JSONResponse(
            {"ok": False, "error": f"glossary {glossary_id!r} is not a valid id."}, status_code=400
        )
    from app.kernel import glossary_module as gm

    allowed = {
        g.id for g in gm.options_for_step(s, "", "", org_id=org_id, customer_id=_cust_of(ident))
    }
    if gid_raw not in allowed:
        return None, JSONResponse(
            {
                "ok": False,
                "error": f"glossary {gid_raw} is not an approved glossary "
                "(or is not visible to this org).",
            },
            status_code=400,
        )
    return gid_raw, None


def _api_prompt_template_visible(s: Session, org_id, ident, raw):
    """(ptid, err) for a step prompt-template binding: a non-blank id must be a real id
    VISIBLE to this caller -- its org AND (adversarial review H2) its CUSTOMER ring, so a
    customer-A-bound key cannot bind customer B's proprietary template to its own step.
    Passes customer_id=_cust_of(ident) exactly like _api_glossary_pin. The service then
    enforces existence + the source-placeholder rule + dead-field-on-lqa/hr. err is a
    ready 400, else None."""
    pt = ids.coerce_id(raw)
    if pt is None:
        return None, JSONResponse(
            {"ok": False, "error": f"prompt template {raw!r} is not a valid id (or '' to clear)."},
            status_code=400,
        )
    visible = {
        t.id for t in workflows.list_prompt_templates(s, org_id=org_id, customer_id=_cust_of(ident))
    }
    if pt not in visible:
        return None, JSONResponse(
            {
                "ok": False,
                "error": f"prompt template {pt} is not visible to this caller (or does not exist).",
            },
            status_code=400,
        )
    return pt, None


def _api_branch(raw):
    """A step `branch` body value -> the {on_pass, on_fail} dict add_step/update_step
    validate. Accepts the CLI 'slot=target' LIST form (parse_branch_flags, the shared
    parser) OR a {on_pass, on_fail} OBJECT (passed through; the service validates it).
    None stays None. Raises workflows.WorkflowError on a wrong shape (-> a clean 400)."""
    if raw is None:
        return None
    if isinstance(raw, list):
        return workflows.parse_branch_flags(raw)
    if isinstance(raw, dict):
        return raw
    raise workflows.WorkflowError(
        "branch must be a list of 'slot=target' strings or a {on_pass, on_fail} object."
    )


@router.post("/api/workflows")
async def api_workflows_create(request: Request):
    """Create a workflow (mirrors `workflow create`). can_tune_ai; the new row is
    org-stamped (leak vector 12); an archived customer is refused."""
    ident, org_id, blocked = _gate(request)
    if blocked:
        return blocked
    blocked = _ai_block(ident)  # web POST /workflows create is _ai_block (can_tune_ai)
    if blocked:
        return blocked
    body = await _json(request)
    from app.models import Scope as _Scope

    scope = str(body.get("scope") or _Scope.GLOBAL).strip() or _Scope.GLOBAL
    if scope not in (_Scope.GLOBAL, _Scope.CUSTOMER, _Scope.PROJECT):
        return JSONResponse(
            {"ok": False, "error": f"scope {scope!r} must be one of global, customer, project."},
            status_code=400,
        )
    with Session(engine) as s:
        customer_id, blocked = _api_workflow_customer(s, ident, org_id, body.get("customer_id"))
        if blocked:
            return blocked
        try:
            # RAW name so the service refuses a non-string (BUG-A13); clean_name also
            # caps length (BUG-A12). None/blank -> None below (the "blank" 400).
            wf = workflows.create_workflow(
                s,
                body.get("name"),
                scope=scope,
                customer_id=customer_id,
                org_id=org_id,
                created_via="manual",
                # Shared-seam defense (chip task_56b8b2bf):
                # customer_id is already authorized by
                # _api_workflow_customer above, so this only
                # ever agrees -- it keeps the seam the single
                # source of truth for the cross-tenant refusal.
                bound_customer_id=_cust_of(ident),
            )
        except workflows.WorkflowForeignCustomerError:
            # Cannot normally fire (customer_id was pre-authorized) -- map to the same
            # not-enumerable 404 the funnel uses, never a 500.
            return JSONResponse(_NOT_FOUND, status_code=404)
        except ids.InvalidNameError as exc:
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=422)
        if wf is None:
            return JSONResponse(
                {"ok": False, "error": "workflow name must not be blank."}, status_code=400
            )
        wid, wname, wscope, wcust = wf.id, wf.name, wf.scope, wf.customer_id
    audit.log(
        actor=ident["actor"],
        action="workflow.create",
        target=f"workflow {wid} '{wname}'",
        ip=deps._ip(request),
        customer_id=wcust,
    )
    return JSONResponse(
        {"ok": True, "id": wid, "name": wname, "scope": wscope, "customer_id": wcust}
    )


@router.post("/api/workflows/{wid}/steps")
async def api_workflows_add_step(wid: int, request: Request):
    """Append a step (mirrors `workflow add-step`). Full add-step flag surface in the
    JSON body; the {text}/{prior} prompt rule, glossary-pin + prompt-template re-
    validation, dead-field refusal and whole-graph re-validation all run through the
    shared seams. A soft language warning rides back as "warnings"."""
    ident, org_id, blocked = _gate(request)
    if blocked:
        return blocked
    blocked = _ai_block(ident)
    if blocked:
        return blocked
    body = await _json(request)
    step_type = str(body.get("step_type") or StepType.LLM_AGENT).strip() or StepType.LLM_AGENT
    from app.kernel import prompts

    with Session(engine) as s:
        wf = _wf_in_org(s, wid, org_id, ident)
        if wf is None:
            return JSONResponse(_NOT_FOUND, status_code=404)
        blocked = _shared_write_block(ident, getattr(wf, "customer_id", None))
        if blocked:
            return blocked
        try:
            prompt_warning = prompts.validate_step_prompt(
                step_type, str(body.get("prompt_text") or "")
            )
        except prompts.PromptValidationError as exc:
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
        glossary_id = None
        if body.get("glossary_id") is not None:
            glossary_id, err = _api_glossary_pin(s, wf, org_id, ident, body.get("glossary_id"))
            if err:
                return err
        prompt_template_id = None
        if body.get("prompt_template_id") is not None:
            prompt_template_id, err = _api_prompt_template_visible(
                s, org_id, ident, body.get("prompt_template_id")
            )
            if err:
                return err
        # Three-state booleans on ADD, same rule as edit (M1, the other half): a REAL
        # bool sets, JSON null = absent (fall back to add_step's own default), and any
        # OTHER type is REFUSED. bool("false") is True and bool(None) is False, so
        # coercing silently ENABLED two spend-bearing toggles ("false") and DISABLED
        # tm_examples (null) at a cheerful 200.
        bool_defaults = {
            "redact_pii": False,
            "adaptive_feedback": False,
            "doc_context_prepass": False,
            "qe_gate": False,
            # QE auto-fix (Train B): both default OFF (a missing key = no loop).
            "qe_autofix": False,
            "qe_autofix_cross_check": False,
            "tm_examples": True,
            # Batch 14 T2 (SPEC §25.5-C): default TRUE (unlike the default-False qe_gate) —
            # a MISSING key must resolve to the ratified default (opted IN), not False.
            "use_image_context": True,
            # Terminology T3 (SPEC §9-T3): default FALSE (opted OUT) — a missing key renders
            # prompts.glossary_line verbatim (byte-identical).
            "concept_glossary_render": False,
        }
        bools = {}
        for f, default in bool_defaults.items():
            v = body.get(f)
            if f not in body or v is None:
                bools[f] = default
            elif isinstance(v, bool):
                bools[f] = v
            else:
                return JSONResponse(
                    {"ok": False, "error": f"{f} must be true or false."},
                    status_code=400,  # noqa: RUF100, UP032
                )
        try:
            branch = _api_branch(body.get("branch"))
            step = workflows.add_step(
                s,
                wid,
                name=str(body.get("name") or ""),
                step_type=step_type,
                provider=str(body.get("provider") or ""),
                model=str(body.get("model") or ""),
                fallback_provider=str(body.get("fallback_provider") or ""),
                fallback_model=str(body.get("fallback_model") or ""),
                prompt_text=str(body.get("prompt_text") or ""),
                prompt_template_id=prompt_template_id,
                chunk_policy=str(body.get("chunk_policy") or "auto"),
                redact_pii=bools["redact_pii"],
                adaptive_feedback=bools["adaptive_feedback"],
                adaptive_feedback_window=body.get("adaptive_feedback_window", 5),
                doc_context_prepass=bools["doc_context_prepass"],
                qe_gate=bools["qe_gate"],
                qe_threshold=body.get("qe_threshold", 70),
                qe_provider=str(body.get("qe_provider") or ""),
                # QE auto-fix (Train B): the fixer is role-checked in add_step (an NMT or
                # judge engine is refused); rounds must be whole numbers 1-5 (add_step
                # refuses others).
                qe_autofix=bools["qe_autofix"],
                qe_autofix_provider=str(body.get("qe_autofix_provider") or ""),
                qe_autofix_max_rounds=body.get("qe_autofix_max_rounds", 3),
                qe_autofix_cross_check=bools["qe_autofix_cross_check"],
                # feat/lqa-configurable-threshold: the lqa step's numeric pass bar.
                # Absent / null -> None (the step keeps the built-in default gate at 90);
                # a set value gates on it (refused on a non-lqa step by add_step).
                lqa_threshold=body.get("lqa_threshold"),
                tm_examples=bools["tm_examples"],
                tm_examples_window=body.get("tm_examples_window", 3),
                use_image_context=bools["use_image_context"],  # Batch 14 T2 (SPEC §25.5-C)
                # Terminology T3 (SPEC §9-T3): renderer toggle + caps (add_step coerces the
                # caps to >=0; a missing key -> default OFF/10/0 = byte-identical).
                concept_glossary_render=bools["concept_glossary_render"],
                glossary_concept_cap=body.get("glossary_concept_cap", 10),
                glossary_token_cap=body.get("glossary_token_cap", 0),
                tm_protect=body.get("tm_protect", "auto"),
                # run-190 Item D / M5: the over-translation guard opt-out (raw pass-through
                # like tm_protect; absent/null -> None -> inherit the global guard).
                over_translation_guard=body.get("over_translation_guard", "auto"),
                memory_mode=body.get("memory_mode", "auto"),
                # Batch 15 T1 (SPEC-tm-match-step-config §8.C): the six tm_match config
                # overrides. Raw pass-through (like tm_protect): absent/null -> None ->
                # inherit, which SEEDS a concrete snapshot on a NEW tm_match step. add_step
                # coerces + clamps + refuses a value forged onto a non-tm_match step. The
                # tri-state bools default to None (inherit), NOT a concrete bool.
                tm_fill_threshold=body.get("tm_fill_threshold"),
                tm_fuzzy_lock_mode=body.get("tm_fuzzy_lock_mode"),
                tm_lock_ice=body.get("tm_lock_ice"),
                tm_hundreds_enter_lqa=body.get("tm_hundreds_enter_lqa"),
                tm_context_floor=body.get("tm_context_floor"),
                tm_pool_selection=body.get("tm_pool_selection"),
                glossary_id=glossary_id,
                run_condition=body.get("run_condition") or None,
                key=str(body.get("key") or "") or None,
                branch=branch,
                max_visits=body.get("max_visits"),
                parallel_group=str(body.get("parallel_group") or "") or None,
                vendor_id=body.get("vendor_id"),
                external_wait_hours=body.get("external_wait_hours"),
            )
        except workflows.WorkflowError as exc:
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
        payload = {
            "ok": True,
            "id": step.id,
            "workflow_id": wid,
            "order_index": step.order_index,
            "step_type": step.step_type,
            "name": step.name,
            "glossary_id": step.glossary_id,
            "run_condition": step.run_condition,
            "key": step.key,
            "branch": step.branch,
            "max_visits": step.max_visits,
            "parallel_group": step.parallel_group,
        }
        from app.kernel.qe_autofix import configuration_warnings

        save_warnings = ([prompt_warning] if prompt_warning else []) + configuration_warnings(step)
        if save_warnings:
            payload["warnings"] = save_warnings
        cust = wf.customer_id
    audit.log(
        actor=ident["actor"],
        action="workflow.add_step",
        target=f"workflow {wid} step {payload['id']}",
        detail=f"{payload['step_type']} at index {payload['order_index']}",
        ip=deps._ip(request),
        customer_id=cust,
    )
    return JSONResponse(payload)


@router.post("/api/workflows/{wid}/steps/{sid}/edit")
async def api_workflows_edit_step(wid: int, sid: int, request: Request):
    """PARTIAL edit of a step in place (mirrors `workflow edit-step`): ONLY the fields
    present in the body change, including the three-state boolean pattern (JSON
    true/false/absent = set/clear/leave). The whole-graph re-validation + in-flight-run
    guard run in update_step (fail-closed -> 400). `pin`/`unpin` express the deliberate
    variant pin intent, and a body with NO field keys (just pin/unpin) is a pure,
    run-safe metadata flip.

    WHO: the org and customer ring first (a foreign workflow is a 404 before any role
    answer), then can_tune_ai (admin or linguist) for any edit, or a MANAGER for a body that
    carries `prompt_text` and nothing else (2026-10-08: managers may change and restore
    a step's prompt, system_prompts.can_edit_step_prompt; the rest of the step stays
    can_tune_ai)."""
    ident, org_id, blocked = _gate(request)
    if blocked:
        return blocked
    body = await _json(request)
    from app.kernel import prompts, system_prompts

    with Session(engine) as s:
        wf = _wf_in_org(s, wid, org_id, ident)
        if wf is None:
            return JSONResponse(_NOT_FOUND, status_code=404)
        blocked = _shared_write_block(ident, getattr(wf, "customer_id", None))
        if blocked:
            return blocked
        prompt_only = isinstance(body, dict) and set(body) == {"prompt_text"}
        blocked = _role_block(
            ident,
            system_prompts.can_edit_step_prompt if prompt_only else security.can_tune_ai,
        )
        if blocked:
            return blocked
        step = _api_wf_step_or_none(s, wf, sid)
        if step is None:
            return JSONResponse(_NOT_FOUND, status_code=404)
        if body.get("branch") and body.get("clear_branch"):
            return JSONResponse(
                {"ok": False, "error": "pass either branch or clear_branch, not both."},
                status_code=400,
            )
        pin, unpin = bool(body.get("pin")), bool(body.get("unpin"))
        if pin and unpin:
            return JSONResponse(
                {"ok": False, "error": "pass either pin or unpin, not both."}, status_code=400
            )
        # Assemble ONLY the fields present in the body (partial contract); an absent key
        # leaves the field untouched, exactly like the CLI's None sentinel.
        kw: dict = {}
        for f in (
            "name",
            "step_type",
            "provider",
            "model",
            "fallback_provider",
            "fallback_model",
            "prompt_text",
            "chunk_policy",
            "assignee_email",
            "adaptive_feedback_window",
            "qe_threshold",
            "qe_provider",
            # QE auto-fix (Train B): the fixer ref ('' clears back to the step's own
            # provider) and the rounds (clamped 1-5 by update_step).
            "qe_autofix_provider",
            "qe_autofix_max_rounds",
            "lqa_threshold",
            "tm_examples_window",
            "max_visits",
            "vendor_id",
            "external_wait_hours",
            "tm_protect",
            # run-190 Item D / M5: the over-translation guard opt-out (present sets / absent
            # unchanged). Raw pass-through like tm_protect; update_step coerces the tristate.
            "over_translation_guard",
            "memory_mode",
            # Batch 15 T1 (SPEC-tm-match-step-config §8.C): the six tm_match config
            # overrides, three-state per field (present-value sets / present-null clears to
            # inherit / absent leaves unchanged). Raw pass-through like tm_protect -- the
            # two tristate bools accept bool/"on"/"off"/"auto"/null and update_step's
            # coercers normalise; a value forged onto a non-tm_match step is refused there.
            "tm_fill_threshold",
            "tm_fuzzy_lock_mode",
            "tm_lock_ice",
            "tm_hundreds_enter_lqa",
            "tm_context_floor",
            "tm_pool_selection",
            # Terminology T3 (SPEC §9-T3): the two glossary caps, raw pass-through
            # (update_step's _coerce_glossary_cap normalises to >=0). A value forged onto a
            # non-llm_agent step is refused there.
            "glossary_concept_cap",
            "glossary_token_cap",
        ):
            if f in body:
                kw[f] = body[f]
        # Three-state booleans: a REAL bool sets, JSON null = absent = leave unchanged
        # (the CLI's None sentinel), and any OTHER type is REFUSED (adversarial review
        # M1: bool("false") is True and bool(None) is False, so coercing silently stored
        # the opposite of what was sent and reported success -- redact_pii=null even
        # DISABLED PII scrubbing while answering 200).
        for f in (
            "redact_pii",
            "adaptive_feedback",
            "doc_context_prepass",
            "qe_gate",
            "qe_autofix",
            "qe_autofix_cross_check",
            "tm_examples",
            # Batch 14 T2 (SPEC §25.5-C): three-state edit (present-true / present-false /
            # absent-unchanged), exactly what an author toggling one step needs.
            "use_image_context",
            # Terminology T3 (SPEC §9-T3): the renderer toggle, same three-state edit.
            "concept_glossary_render",
        ):
            if f in body:
                v = body[f]
                if v is None:
                    continue
                if not isinstance(v, bool):
                    return JSONResponse(
                        {"ok": False, "error": f"{f} must be true or false."},
                        status_code=400,
                    )
                kw[f] = v
        # Clearable conditional text fields: '' / null clears (validated in the service).
        if "run_condition" in body:
            kw["run_condition"] = body["run_condition"] or None
        if "key" in body:
            # Mirror the CLI's `args.key or None` EXACTLY: pass the raw value to
            # validate_key (its slugifier owns the strip), so '' / whitespace-only clear
            # and a real key slugifies down one shared path (L4). '' -> None, '  ' -> a
            # blank slug -> None inside validate_key.
            kw["key"] = body["key"] or None
        # branch: only clear_branch clears; a non-empty branch sets. An EMPTY [] is a
        # no-op ("leave unchanged"), matching the CLI's truthiness (M2: `is not None` let
        # {"branch": []} silently DELETE an existing branch).
        if body.get("clear_branch"):
            kw["branch"] = None
        elif body.get("branch"):
            try:
                kw["branch"] = _api_branch(body["branch"])
            except workflows.WorkflowError as exc:
                return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
        # parallel_group: join (slug), or leave (no_parallel_group / null / '').
        if body.get("no_parallel_group"):
            kw["parallel_group"] = None
        elif "parallel_group" in body:
            v = body["parallel_group"]
            kw["parallel_group"] = (str(v).strip() or None) if v is not None else None
        if body.get("glossary_id") is not None:
            gid, err = _api_glossary_pin(s, wf, org_id, ident, body["glossary_id"])
            if err:
                return err
            kw["glossary_id"] = gid
        if "prompt_template_id" in body:
            raw = body["prompt_template_id"]
            # '' / null clears the binding; 0 is REFUSED like the CLI (coerce_id(0) is
            # None -> "not a valid id"), not treated as a clear (L4 alignment).
            if raw in (None, ""):
                kw["prompt_template_id"] = None
            else:
                pt, err = _api_prompt_template_visible(s, org_id, ident, raw)
                if err:
                    return err
                kw["prompt_template_id"] = str(pt)
        override_intent = True if pin else False if unpin else None
        # PURE pin/unpin (no field flags): a lightweight, run-safe metadata flip.
        if not kw:
            if override_intent is None:
                return JSONResponse(
                    {
                        "ok": False,
                        "error": "no fields to edit; pass at least one field, or pin / unpin.",
                    },
                    status_code=400,
                )
            updated = workflows.set_step_override(s, sid, override_intent)
            resp = {
                "ok": True,
                "id": updated.id,
                "workflow_id": wid,
                "overridden": updated.overridden,
            }
            cust = wf.customer_id
            audit.log(
                actor=ident["actor"],
                action="workflow.edit_step",
                target=f"workflow {wid} step {sid}",
                detail="pinned" if override_intent else "unpinned",
                ip=deps._ip(request),
                customer_id=cust,
            )
            return JSONResponse(resp)
        # {text}/{prior} rule on the EFFECTIVE (post-edit) type + prompt; the SOFT
        # language warning only when the edit actually touched the prompt (P12e).
        eff_type = kw.get("step_type", step.step_type)
        eff_prompt = kw.get("prompt_text", step.prompt_text)
        try:
            prompt_warning = prompts.validate_step_prompt(eff_type, eff_prompt)
        except prompts.PromptValidationError as exc:
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
        if not ("prompt_text" in kw or "step_type" in kw):
            prompt_warning = ""
        report: dict = {}
        extra = {} if override_intent is None else {"set_override": override_intent}
        try:
            updated = workflows.update_step(s, sid, report=report, **extra, **kw)
        except workflows.WorkflowError as exc:
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)

        def _labels(key):
            return [e.get("label", "?") for e in report.get(key, [])]

        prop = {
            k: _labels(k)
            for k in (
                "propagated",
                "skipped_adapted",
                "pinned",
                "validation_skipped",
                "run_active_skipped",
                "propagated_as_singleton",
            )
        }
        payload = {
            "ok": True,
            "id": updated.id,
            "workflow_id": wid,
            "order_index": updated.order_index,
            "step_type": updated.step_type,
            "name": updated.name,
            "changed": sorted(kw),
            "glossary_id": updated.glossary_id,
            "run_condition": updated.run_condition,
            "key": updated.key,
            "branch": updated.branch,
            "max_visits": updated.max_visits,
            "parallel_group": updated.parallel_group,
            "overridden": updated.overridden,
            "auto_pinned": bool(report.get("auto_pinned")),
            "type_switch_cleared": report.get("type_switch_cleared", []),
            "propagation": prop,
            "dead_fields_cleared": report.get("dead_fields_cleared", []),
        }
        from app.kernel.qe_autofix import configuration_warnings

        save_warnings = ([prompt_warning] if prompt_warning else []) + configuration_warnings(
            updated
        )
        if save_warnings:
            payload["warnings"] = save_warnings
        cust = wf.customer_id
    audit.log(
        actor=ident["actor"],
        action="workflow.edit_step",
        target=f"workflow {wid} step {sid}",
        detail="changed: {}".format(", ".join(sorted(kw))),
        ip=deps._ip(request),
        customer_id=cust,
    )
    return JSONResponse(payload)
