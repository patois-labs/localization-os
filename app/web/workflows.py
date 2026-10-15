"""Workflow builder, workflow runs (incl. live SSE), and prompt templates.

Transitional compatibility shim for one release (added 2026-09-14, remove the re-export block after
the next tagged release): `__all__` re-exports _notice_url, _redirect_with_notice,
_with_prompt_warning, _shared_base_notice, _wf_customer_block, _wf_create_customer_block,
_archived_customer_block, _WF_SORT_FIELDS, _wf_sort_key, _sort_rows, _grp_or_404,
_copy_pairs_notice, _rail_selected_customer, _wf_glossary_customer, _visible_group_variants,
_group_pair_source, _group_pair_targets_ctx, _visible_prompt_templates, _shape_ctx, _author_draft,
_workflow_review_customers, _STEP_TEXT_FIELDS, _STEP_BOOL_FIELDS,
_MAX_REVIEW_STEPS, _parse_run_condition_fields, _parse_branch_fields, _int_or_none, _run_graph_ctx,
templates_redirect, workflows_index, _wf_copy_targets_ctx, _validated_prompt_template_id,
_render_workflow_review, _parse_review_form, _prompt_template_or_404, group_detail, delete_group,
delete_prompt_template from app.web.workflows_common, app.web.workflows_part2,
app.web.workflows_part3, app.web.workflows_part4.

Documented in: docs/architecture/workflow-engine.md
"""

import html
import json
from copy import copy as _srp_copy

from fastapi import APIRouter, Depends, Form, Query, Request
from fastapi.responses import JSONResponse, PlainTextResponse, RedirectResponse
from sqlmodel import Session, select

from app import config
from app.db import get_session
from app.kernel import (
    access,
    audit,
    batch_runs,
    ids,
    orchestrator,
    run_overlay,
    security,
    system_prompts,
    vendor_dispatch,
)
from app.kernel import (
    customers as customer_service,
)
from app.kernel import workflows as wf_service
from app.kernel.providers import (
    DeadEngineError,
    ProviderCallError,
    ProviderTruncated,
    list_models_for_ref,
)
from app.models import (
    Customer,
    Document,
    RunStatus,
    Scope,
    Segment,
    StepRun,
    StepType,
    Workflow,
    WorkflowBatch,
    WorkflowGroup,
    WorkflowRun,
    WorkflowStep,
)
from app.web import page_guard
from app.web._forms import ParallelGroupForm
from app.web.deps import (
    _actor_email,
    _admin_block,
    _ai_block,
    _error_page,
    _ip,
    _missing_block,
    _missing_or,
    _my_customer_id,
    _my_org_id,
    _org_block,
    _visibility_block,
    _visible_doc,
    offload_parse,
    templates,
)
from app.web.streaming import StreamingResponse
from app.web.workflows_common import (
    _MAX_REVIEW_STEPS,
    _STEP_BOOL_FIELDS,
    _STEP_TEXT_FIELDS,
    _WF_SORT_FIELDS,
    _archived_customer_block,
    _author_draft,
    _copy_pairs_notice,
    _group_pair_source,
    _group_pair_targets_ctx,
    _grp_or_404,
    _int_or_none,
    _notice_url,
    _parse_branch_fields,
    _parse_run_condition_fields,
    _rail_selected_customer,
    _redirect_with_notice,
    _run_graph_ctx,
    _shape_ctx,
    _shared_base_notice,
    _sort_rows,
    _visible_group_variants,
    _visible_prompt_templates,
    _visible_workflows,
    _wf_create_customer_block,
    _wf_customer_block,
    _wf_glossary_customer,
    _wf_sort_key,
    _wf_write_block,
    _with_prompt_warning,
    _workflow_review_customers,
    templates_redirect,
)
from app.web.workflows_common import router as workflows_common_router
from app.web.workflows_part2 import (
    _parse_review_form,
    _prompt_template_or_404,
    _render_workflow_review,
    _validated_prompt_template_id,
    _wf_copy_targets_ctx,
    workflows_index,
)
from app.web.workflows_part2 import router as workflows_part2_router
from app.web.workflows_part3 import delete_group, group_detail
from app.web.workflows_part3 import router as workflows_part3_router
from app.web.workflows_part4 import delete_prompt_template
from app.web.workflows_part4 import router as workflows_part4_router

__all__ = [
    "_MAX_REVIEW_STEPS",
    "_STEP_BOOL_FIELDS",
    "_STEP_TEXT_FIELDS",
    "_WF_SORT_FIELDS",
    "DeadEngineError",
    "ProviderCallError",
    "ProviderTruncated",
    "_archived_customer_block",
    "_author_draft",
    "_copy_pairs_notice",
    "_group_pair_source",
    "_group_pair_targets_ctx",
    "_grp_or_404",
    "_int_or_none",
    "_notice_url",
    "_parse_branch_fields",
    "_parse_review_form",
    "_parse_run_condition_fields",
    "_prompt_template_or_404",
    "_rail_selected_customer",
    "_redirect_with_notice",
    "_render_workflow_review",
    "_run_graph_ctx",
    "_shape_ctx",
    "_shared_base_notice",
    "_sort_rows",
    "_validated_prompt_template_id",
    "_visible_group_variants",
    "_visible_prompt_templates",
    "_wf_copy_targets_ctx",
    "_wf_create_customer_block",
    "_wf_customer_block",
    "_wf_glossary_customer",
    "_wf_sort_key",
    "_with_prompt_warning",
    "_workflow_review_customers",
    "delete_group",
    "delete_prompt_template",
    "group_detail",
    "templates_redirect",
    "workflows_index",
]

router = APIRouter()

# a11y T5 (WCAG 3.1.2): the step types whose StepRun output is the run's TARGET text (a
# translation, a reviewer's edit, a TM fill, a vendor return), so the Run view marks that
# output with the run locale's lang / dir. An explicit allow-list: LQA (whose output the
# page must not claim is target text) and any future step type stay untagged by default.
_TARGET_TEXT_STEP_TYPES = frozenset(
    {StepType.LLM_AGENT, StepType.HUMAN_REVIEW, StepType.TM_MATCH, StepType.EXTERNAL}
)


# #339: the Steps panel used to hard-cut every step output at 2,000 characters with no
# notice. It now renders up to this many characters inline (in a scroll region) and, past
# it, says so and links the full-text endpoint below. Never cut silently.
STEP_OUTPUT_INLINE_CAP = 50_000


@router.get("/runs/{run_id}/steps/{step_run_id}/output.json")
def run_step_output(
    run_id: int,
    step_run_id: int,
    request: Request,
    format: str = "json",
    session: Session = Depends(get_session),
):
    """Full output text of ONE step run (#339), for the run page's "Show full text" link.
    Same visibility ring as the run page; a missing run/step or one the caller may not see
    is a uniform 404 (never confirms an id in another org exists). ?format=text returns
    text/plain for reading in a browser tab."""
    not_found = JSONResponse({"ok": False, "error": "not found"}, status_code=404)
    run = session.get(WorkflowRun, run_id)
    sr = session.get(StepRun, step_run_id)
    if run is None or sr is None or sr.workflow_run_id != run.id:
        return not_found
    doc = session.get(Document, run.document_id)
    if doc is None or _visibility_block(request, session, doc.project_id):
        return not_found
    text = sr.output_text or ""
    if format == "text":
        return PlainTextResponse(text, media_type="text/plain; charset=utf-8")
    return JSONResponse(
        {
            "ok": True,
            "run_id": run.id,
            "step_run_id": sr.id,
            "chars": len(text),
            "text": text,
        }
    )


@router.get("/runs/{run_id}/step-diff")
def run_step_diff(
    run_id: int,
    request: Request,
    # alias: the picker form (and the /api twin) send `from`, a Python-keyword
    # name no parameter may take; without the alias FastAPI binds `frm` to
    # ?frm= and the picker's From select is silently dropped (self-review F1).
    frm: str = Query("", alias="from"),
    to: str = "",
    session: Session = Depends(get_session),
):
    """GitHub #340: the 'Highlight differences' view of a run -- what each step
    changed, segment by segment, word-level, vs the previous translating step.

    Same visibility ring as the run page (below): missing run/doc -> the same
    303, a blocked project -> the same visibility block. Read-only. ?from=/?to=
    name two StepRun ids of THIS run to compare; omitted, the pair defaults to
    the run's newest translating output vs its previous translating step. The
    per-segment before/after/changed data is the SAME shape
    GET /api/runs/{id}/step-diff returns (run_overlay.step_diff_view); the page
    only adds the escaped <ins>/<del> word markup."""
    run = session.get(WorkflowRun, run_id)
    if not run:
        return _missing_block(request, session)
    doc = session.get(Document, run.document_id)
    if not doc:
        return _missing_block(request, session)
    blocked = _visibility_block(request, session, doc.project_id)
    if blocked:
        return blocked
    steps = orchestrator.get_steps(session, run.workflow_id)
    step_runs = session.exec(
        select(StepRun).where(StepRun.workflow_run_id == run_id).order_by(StepRun.id)
    ).all()
    from_id, to_id = _step_diff_pair_ids(frm, to, run, step_runs, steps)
    view = run_overlay.step_diff_view(run, step_runs, steps, from_id=from_id, to_id=to_id)
    return templates.TemplateResponse(
        request,
        "run_step_diff.html",
        {
            "request": request,
            "doc": doc,
            "run": run,
            "step_runs": step_runs,
            "step_rows": [{"sr": sr, "label": _step_diff_label(sr, steps)} for sr in step_runs],
            "diff": view,
            "seg_rows": [
                {"seg": seg, "html": _step_diff_seg_html(seg)} for seg in view["segments"]
            ],
            "from_id": from_id,
            "to_id": to_id,
        },
    )


def _step_diff_pair_ids(frm: str, to: str, run, step_runs, steps):
    """Resolve the ?from=/?to= query ids to a comparable pair for THIS run,
    falling back to the run's default pair (newest translating output vs the
    previous translating step). Unknown / foreign-run / non-integer ids fall
    back too: the page is a read-only convenience, so it degrades to the
    default rather than 404ing on a mistyped link (the /api twin keeps the
    strict 404)."""
    run_ids = {sr.id for sr in step_runs}

    def resolve(raw):
        try:
            value = int(str(raw).strip())
        except (TypeError, ValueError):
            return None
        return value if value in run_ids else None

    from_id = resolve(frm)
    to_id = resolve(to)
    if from_id is None or to_id is None or from_id == to_id:
        default_from, default_to = run_overlay.step_diff_default_pair(step_runs, steps)
        if from_id is None:
            from_id = default_from
        if to_id is None:
            to_id = default_to
        if from_id == to_id:
            from_id = default_from
            to_id = default_to
    return from_id, to_id


def _step_diff_label(sr, steps):
    """The picker label for one step run: '#<id> <step name or type> (<status>)'."""
    step = next((s for s in steps if s.id == sr.step_id), None)
    name = (getattr(step, "name", None) or step.step_type) if step is not None else "step"
    return f"#{sr.id} {name} ({sr.status})"


def _step_diff_seg_html(seg):
    """The escaped word-level markup of ONE changed segment (insertions in
    <ins>, deletions in <del> -- see the diff-ins/diff-del CSS: underline and
    strike-through, never colour-only). Every text token is html.escape-d BEFORE
    it is wrapped in markup, and the finished string is returned as Markup so
    Jinja's autoescape does not re-escape the (already-safe) tags -- no |safe
    on raw input anywhere."""
    from markupsafe import Markup

    parts = []
    for op, text in seg["diff"]:
        esc = html.escape(text, quote=False)
        # sr-only text markers: <ins>/<del> alone are not reliably announced, and
        # the underline / strike-through is visual only (a11y: never colour-only).
        if op == "insert":
            parts.append(
                f'<ins class="diff-ins"><span class="sr-only">[inserted: </span>{esc}'
                '<span class="sr-only">]</span></ins>'
            )
        elif op == "delete":
            parts.append(
                f'<del class="diff-del"><span class="sr-only">[deleted: </span>{esc}'
                '<span class="sr-only">]</span></del>'
            )
        else:
            parts.append(esc)
    return Markup("".join(parts))  # noqa: S704 -- every text piece escaped above


@router.get("/workflows/{workflow_id}/plan")
def workflow_spend_plan(
    workflow_id: int,
    request: Request,
    doc_id: int | None = None,
    locale: str = "",
    session: Session = Depends(get_session),
):
    from app.kernel import workflow_plan

    wf, blocked = _wf_or_404(request, session, workflow_id)
    if blocked:
        return blocked
    preview = None
    if doc_id is not None:
        doc = _visible_doc(session, doc_id)
        if doc is None:
            return _missing_block(request, session)
        blocked = _visibility_block(request, session, doc.project_id)
        if blocked:
            return blocked
        try:
            preview = workflow_plan.plan(session, workflow_id, doc, locale.strip())
        except ValueError as exc:
            return _error_page(request, 400, str(exc))
    return templates.TemplateResponse(
        request,
        "workflow_plan.html",
        {
            "wf": wf,
            "preview": preview,
            "doc_id": doc_id,
            "locale": locale,
        },
    )


def _wf_or_404(request, session, workflow_id, write=False):
    """Fetch a workflow and org-gate it (leak vector 12 — a GLOBAL-scope workflow
    is org-owned now). Returns (wf, blocked): a foreign / missing workflow is a 404
    (not enumerable). No-op with the gate off (same_org True).

    R2-3 (Finding D): also runs the CUSTOMER ring under the org check — a
    customer-bound user must not read a foreign customer-scoped workflow's steps/
    prompts/provider config by direct id (404, not enumerable). A GLOBAL-scope
    workflow (customer_id NULL) stays visible (see _wf_customer_block). No-op for
    internal staff / auth off. A non-null project_id also requires the shared
    project visibility authority, independent of the workflow's scope label."""
    wf = session.get(Workflow, workflow_id)
    if wf is None:
        return None, _error_page(request, 404, "Not found.")
    blocked = _org_block(request, getattr(wf, "org_id", None))
    if blocked:
        return None, blocked
    blocked = _wf_customer_block(request, getattr(wf, "customer_id", None))
    if blocked:
        return None, blocked
    if wf.project_id is not None and not access.can_view_project(
        session, security.session_user(request), wf.project_id
    ):
        return None, _error_page(request, 404, "Not found.")
    if write:
        # #468: every route that CHANGES the workflow passes write=True: a GLOBAL workflow
        # is read-only for a customer-bound user (403; it may still open and clone it).
        blocked = _wf_write_block(request, getattr(wf, "customer_id", None))
        if blocked:
            return None, blocked
    return wf, None


_srp_router_options = router.prefix, router.tags, router.dependencies
router.prefix, router.tags, router.dependencies = "", [], []
router.include_router(workflows_part2_router)
router.prefix, router.tags, router.dependencies = _srp_router_options


@router.post("/workflow-groups")
def create_group(
    request: Request,
    name: str = Form(...),
    customer_id: str = Form(""),
    description: str = Form(""),
    session: Session = Depends(get_session),
):
    _, blocked = _ai_block(request)
    if blocked:
        return blocked
    # #488 item 2: refuse a customer the caller may not scope to (foreign customer, other
    # org, quarantined NULL-org) BEFORE the archived check, so another tenant's customer
    # name never appears in an error (the same order create_workflow uses).
    blocked = _wf_create_customer_block(request, session, customer_id)
    if blocked:
        return blocked
    # T2-01: an archived customer cannot be attached to a new workflow group.
    blocked = _archived_customer_block(
        request, session, customer_id, "create a workflow group for it"
    )
    if blocked:
        return blocked
    me = security.session_user(request)
    try:
        grp = wf_service.create_group(
            session,
            name,
            customer_id=customer_id,
            description=description,
            org_id=_my_org_id(request),
            created_by=getattr(me, "email", "") if me else "",
            # #468: a customer account's group is always its own customer's.
            bound_customer_id=_my_customer_id(request),
        )
    except wf_service.WorkflowForeignCustomerError:
        return _error_page(request, 404, "Not found.")
    if grp is None:
        return RedirectResponse("/workflows", status_code=303)
    return RedirectResponse(f"/workflow-groups/{grp.id}", status_code=303)


_srp_piece_router = workflows_part3_router
workflows_part3_router = _srp_copy(workflows_part3_router)
workflows_part3_router.routes = workflows_part3_router.routes[0:1]
_srp_router_options = router.prefix, router.tags, router.dependencies
router.prefix, router.tags, router.dependencies = "", [], []
router.include_router(workflows_part3_router)
router.prefix, router.tags, router.dependencies = _srp_router_options
workflows_part3_router = _srp_piece_router


def _step_prompt_or_default_block(request, predicate):
    """The role gate of the two doors owner opened to managers on 2026-10-08 (a step's prompt,
    a group's default). Same refusal page as _ai_block; a no-op with the login wall off."""
    me = security.session_user(request)
    if security.auth_required() and not predicate(me):
        return None, _error_page(request, 403, "Your role does not allow this action.")
    return me, None


def _group_default_head_block(request, session, workflow_id, group_id, make_default):
    """#365 gate fix: a group's default is the BASE its pair variants inherit a system prompt
    head from, so a default change that moves a live head (or takes one away) is a system
    prompt change and needs an admin, like every other head write. #386: the same is true of
    moving an ordinary variant whose effective head the move changes. No-op with the
    flag off or the login wall off, and when no head is involved (linguists keep managing
    groups)."""
    if not system_prompts.group_default_change_needs_admin(
        session, workflow_id, group_id, make_default
    ):
        return None
    me = security.session_user(request)
    if security.auth_required() and not system_prompts.can_change_group_default_heads(me):
        return _error_page(request, 403, system_prompts.GROUP_DEFAULT_ADMIN_ERROR)
    return None


@router.post("/workflow-groups/{group_id}/add")
def group_add(
    group_id: int,
    request: Request,
    workflow_id: int = Form(...),
    source_locale: str = Form(""),
    target_locale: str = Form(""),
    make_default: str = Form(""),
    force: str = Form(""),
    session: Session = Depends(get_session),
):
    """R4 (adversarial review): `force` is the web half of the CLI's `--force` — the
    deliberate pair REPLACE the refusal otherwise makes impossible (it removed the only
    way to correct a mistyped pair). Unforced, the refusal stands. The redirect NAMES the
    old pair so a replace is never silent, which was the whole point of #46."""
    _, blocked = _ai_block(request)
    if blocked:
        return blocked
    # #468: a customer account may add its OWN workflow as a variant of a shared group
    # (its variant is visible and dispatched only in its own ring), but making it the
    # group's default changes the group for every customer, so that needs a writable group.
    _grp, blocked = _grp_or_404(request, session, group_id, write=bool(make_default))
    if blocked:
        return blocked
    # org-gate the workflow being added (a scoped user can't fold a foreign one in)
    wf, blocked = _wf_or_404(request, session, workflow_id, write=True)
    if blocked:
        return blocked
    blocked = _group_default_head_block(request, session, workflow_id, group_id, bool(make_default))
    if blocked:
        return blocked
    was_member = wf.group_id == group_id
    old_pair = wf_service.pair_key(wf.source_locale, wf.target_locale)
    try:
        updated = wf_service.add_to_group(
            session,
            workflow_id,
            group_id,
            source_locale=source_locale,
            target_locale=target_locale,
            make_default=bool(make_default),
            force=bool(force),
        )
    except wf_service.WorkflowError as exc:
        # Issue #46: an explicit pair that would REPLACE an existing member's pair. The
        # form only offers UNGROUPED workflows so this is unreachable from the page, but
        # the seam refuses rather than silently rewriting, and a hand-rolled POST must
        # see the same refusal the CLI does.
        return _error_page(request, 400, str(exc))
    notice = ""
    if updated is not None and was_member:
        new_pair = wf_service.pair_key(updated.source_locale, updated.target_locale)
        if new_pair != old_pair:
            notice = (
                f"Replaced {updated.name}'s language pair: {old_pair} -> "
                f"{new_pair}. The group no longer covers {old_pair} unless "
                f"another variant does."
            )
    return _redirect_with_notice(f"/workflow-groups/{group_id}", notice)


@router.post("/workflow-groups/{group_id}/default")
def group_set_default(
    group_id: int,
    request: Request,
    workflow_id: int = Form(...),
    session: Session = Depends(get_session),
):
    """Make an existing member the group's default. Admin, linguist or manager (the maintainer,
    2026-10-08: system_prompts.can_set_group_default); a change that moves an inherited system
    prompt head also needs an admin or a manager (_group_default_head_block)."""
    _, blocked = _step_prompt_or_default_block(request, system_prompts.can_set_group_default)
    if blocked:
        return blocked
    _grp, blocked = _grp_or_404(request, session, group_id, write=True)
    if blocked:
        return blocked
    wf, blocked = _wf_or_404(request, session, workflow_id, write=True)
    if blocked:
        return blocked
    if wf.group_id != group_id:
        return _error_page(request, 400, "Workflow is not a member of this group.")
    blocked = _group_default_head_block(request, session, workflow_id, group_id, True)
    if blocked:
        return blocked
    wf_service.set_default_variant(session, workflow_id)
    return RedirectResponse(f"/workflow-groups/{group_id}", status_code=303)


@router.post("/workflow-groups/{group_id}/rename")
def rename_group(
    group_id: int, request: Request, name: str = Form(""), session: Session = Depends(get_session)
):
    """Rename a workflow group. Family gate (_ai_block), matching the other
    group-mutation routes. Logs old->new name."""
    _, blocked = _ai_block(request)
    if blocked:
        return blocked
    grp, blocked = _grp_or_404(request, session, group_id, write=True)
    if blocked:
        return blocked
    old_name = grp.name
    try:
        wf_service.rename_group(session, group_id, name)
    except wf_service.WorkflowError as exc:
        return _error_page(request, 400, str(exc))
    audit.log(
        actor=_actor_email(request),
        action="workflow_group.rename",
        target=f"workflow_group {group_id}",
        detail=f"{old_name!r} -> {grp.name!r}",
        ip=_ip(request),
        customer_id=grp.customer_id,
    )
    return RedirectResponse(f"/workflow-groups/{group_id}", status_code=303)


_srp_piece_router = workflows_part3_router
workflows_part3_router = _srp_copy(workflows_part3_router)
workflows_part3_router.routes = workflows_part3_router.routes[1:2]
_srp_router_options = router.prefix, router.tags, router.dependencies
router.prefix, router.tags, router.dependencies = "", [], []
router.include_router(workflows_part3_router)
router.prefix, router.tags, router.dependencies = _srp_router_options
workflows_part3_router = _srp_piece_router


@router.post("/workflows/{workflow_id}/copy-to-pairs")
def copy_to_pairs(
    workflow_id: int,
    request: Request,
    target_locales: list[str] = Form(default=[]),
    source_locale: str = Form(""),
    adapt: str = Form(""),
    session: Session = Depends(get_session),
):
    """Copy a workflow onto one or more target language pairs (P5, item 4). The
    source locale defaults to the source workflow's own; each checked target locale
    yields one variant. 'Adapt instructions with LLM' rewrites each step's prompt +
    marks it needs_review (a linguist post-edits before activating)."""
    _, blocked = _ai_block(request)
    if blocked:
        return blocked
    src_wf, blocked = _wf_or_404(request, session, workflow_id, write=True)
    if blocked:
        return blocked
    src = (source_locale or src_wf.source_locale or "").strip() or None
    pairs = [(src, (t or "").strip()) for t in target_locales if (t or "").strip()]
    if not pairs:
        # Issue #46(4): a zero-target submit used to 303 to the builder having created
        # NOTHING — indistinguishable from success, so a miss-click read as "copied".
        return _error_page(
            request,
            400,
            "Pick at least one target language to copy this workflow onto. Nothing was created.",
        )
    try:
        result = wf_service.copy_to_pairs(
            session,
            workflow_id,
            pairs,
            adapt=bool(adapt),
            created_by=_actor_email(request),
            viewer=security.session_user(request),
            bound_customer_id=_my_customer_id(request),
        )
    except wf_service.WorkflowError as exc:
        # R8: an unknown locale code (junk / oversized / markup) is refused at the
        # service seam BEFORE any write, so nothing was created. Say which code.
        return _error_page(request, 400, str(exc))
    return _redirect_with_notice(
        f"/workflow-groups/{result['group_id']}", _copy_pairs_notice(result)
    )


@router.post("/workflow-groups/{group_id}/add-pair")
def group_add_pair(
    group_id: int,
    request: Request,
    target_locales: list[str] = Form(default=[]),
    source_locale: str = Form(""),
    adapt: str = Form(""),
    session: Session = Depends(get_session),
):
    """Issue #46(3): "Add a language pair" from the GROUP page — the affordance the group
    view was missing (adding a third language meant navigating into a variant's builder).
    Copies the group's DEFAULT variant (else its first member) onto each checked target
    through the SAME copy_to_pairs seam, pinned to THIS group, so naming, provenance and
    the duplicate guard are byte-identical to the builder's copy. Same gates as every
    other group-mutation route (_ai_block + the group's own 404 ring).

    R1 (adversarial review, merge-blocking): the copy SOURCE is chosen from the members
    this requester may actually see (_visible_group_variants), never from the raw member
    list. A GLOBAL group may hold a customer-B variant; taking that as the template let a
    customer-A user clone B's prompt text into B's ring — invisible to A, and never
    dispatched for A either (dispatch filters the same way), which is precisely the
    "I added a language and nothing happened" confusion #46 set out to kill."""
    _, blocked = _ai_block(request)
    if blocked:
        return blocked
    # #468: a customer account may add pairs to a shared group: copy_to_pairs stamps
    # every copy with its own customer (bound_customer_id below), so no global variant.
    _grp, blocked = _grp_or_404(request, session, group_id)
    if blocked:
        return blocked
    visible = _visible_group_variants(request, session, group_id)
    src_wf = _group_pair_source(visible)
    if src_wf is None:
        return _error_page(
            request,
            400,
            "There is no workflow in this group you can copy a new language pair from. "
            "Add an existing workflow to the group first, then fan it out from here. "
            "Nothing was created.",
        )
    src = (source_locale or src_wf.source_locale or "").strip() or None
    pairs = [(src, (t or "").strip()) for t in target_locales if (t or "").strip()]
    if not pairs:
        return _error_page(
            request, 400, "Pick at least one target language to add. Nothing was created."
        )
    try:
        # N1: `viewer` makes the duplicate guard count only variants THIS requester can
        # see -- a pair covered solely by a foreign variant is not coverage for them, and
        # refusing it left them unable to add a language they genuinely lack.
        result = wf_service.copy_to_pairs(
            session,
            src_wf.id,
            pairs,
            adapt=bool(adapt),
            group_id=group_id,
            created_by=_actor_email(request),
            viewer=security.session_user(request),
            bound_customer_id=_my_customer_id(request),
        )
    except wf_service.WorkflowError as exc:
        return _error_page(request, 400, str(exc))  # R8: unknown locale code(s)
    return _redirect_with_notice(f"/workflow-groups/{group_id}", _copy_pairs_notice(result))


@router.get("/workflows/new")
def workflow_new(request: Request, session: Session = Depends(get_session)):
    """The dedicated CREATE page (SPEC Workflow Studio Wave A) — the create surface
    MOVED off the /workflows dashboard onto its own page. Hosts the RBAC-gated
    "Design a workflow" describe->draft->review flow as the hero (which continues to
    workflow_author_review.html with its repair button, unchanged), the manual
    blank-start as a quiet zero-JS <details> secondary path, and the multilingual
    group-create as a tertiary path. All three POST to the SAME existing routes
    (/workflows/author, /workflows, /workflow-groups), which already redirect into the
    builder / group page — so no create logic changed, only where it lives.

    WAVE B — the AIDS RAIL: a second pane of click-to-insert intelligence beside the
    hero (role presets, the caller's prompt templates, placeholder chips, shape seeds,
    and — when a customer is selected — a "What we already know" panel of that
    customer's pairs / glossaries / approved style guides / context profiles / TM size).
    The customer selection is a plain ?customer_id= GET (zero-JS re-render); the rail
    data is composed from existing already-scoped services (app.services.workflow_rail),
    so it respects the viewer's org + customer ring everywhere.

    RBAC: gated by the SAME can_tune_ai predicate the create routes enforce
    server-side (_ai_block). A viewer without AI rights is redirected back to the
    dashboard rather than shown a page whose every form would 403 on submit."""
    _, blocked = _ai_block(request)
    if blocked:
        # A non-AI viewer has no create surface — send them to the dashboard (which
        # renders no CTA for them either), never a dead 403 page.
        return RedirectResponse("/workflows", status_code=303)
    from app.kernel import workflow_rail
    from app.kernel import workflow_shapes as wshapes

    me = security.session_user(request)
    # SPEC-customer-archive: the scope/customer pickers are PICKERS — archived customers
    # are retired from them (active_only). A bound caller collapses to its own customer.
    cq = customer_service.active_only(access.org_filter(select(Customer), Customer, me)).order_by(
        Customer.name
    )
    my_cust = access.current_customer_id(me)
    if my_cust is not None:
        cq = cq.where(Customer.id == my_cust)
    customers = session.exec(cq).all()
    # Aids rail: resolve the selected customer against the VISIBLE set, then compose its
    # facts through the already-scoped services (a foreign/archived id -> None -> no
    # panel). Honest empty sections when a fact is absent.
    selected_cid = _rail_selected_customer(request, customers, my_cust)
    selected_customer = session.get(Customer, selected_cid) if selected_cid else None
    customer_facts = (
        workflow_rail.customer_facts(session, selected_cid, org_id=_my_org_id(request))
        if selected_cid is not None
        else None
    )
    return templates.TemplateResponse(
        request,
        "workflows_new.html",
        {
            "request": request,
            "customers": customers,
            "scopes": [Scope.GLOBAL, Scope.CUSTOMER],
            # Promptable setup (Branch B): the "Design a workflow" provider picker offers
            # the same built-in providers as the builder (engine:<id> refs come from the
            # engine_options Jinja global).
            "providers": config.PROVIDERS,
            # Wave B aids rail ---------------------------------------------------------
            # Role presets: the built-in orchestrator quick-start prompts (click inserts
            # the preset text). Marked built-in in the template.
            "role_presets": orchestrator.ROLE_PRESETS,
            # Prompt templates: the SAME visibility seam the builder step picker uses, so
            # the rail and the builder can never drift on scope (click inserts the body).
            "prompt_templates": _visible_prompt_templates(request, session),
            # Shape gallery: the five taxonomy shapes as seed-sentence cards.
            "shape_cards": wshapes.shape_seed_cards(),
            # What-we-already-know panel (None until a customer is selected).
            "selected_customer_id": selected_cid,
            "selected_customer": selected_customer,
            "customer_facts": customer_facts,
            "customer_bound": my_cust is not None,
        },
    )


# --- Building blocks (SPEC Workflow Studio Wave C) ------------------------------
# The former top-level "Templates" page is FOLDED into the Workflows area as the
# "Building blocks" tab: the caller's prompt-template library (full CRUD, unchanged
# routes/RBAC/scoping/guards), the built-in role presets rendered read-only, and the
# clonable global-workflow templates — now with honest cross-links (which visible
# workflows bind each template). REGISTERED HERE (before /workflows/{workflow_id:int})
# so the literal path wins routing — same reason /workflows/new sits above the builder.
@router.get("/workflows/building-blocks")
def building_blocks(request: Request, session: Session = Depends(get_session)):
    me, blocked = _ai_block(request)
    if blocked:
        return blocked
    return _building_blocks_page(request, session, me)


def _building_blocks_page(
    request: Request,
    session: Session,
    me,
    *,
    status_code: int = 200,
    sp_draft: dict | None = None,
    err: str | None = None,
):
    """Render Building blocks. Shared by the GET and by a refused system-prompt save, which
    re-renders with the admin's typed text kept (`sp_draft`) and the refusal as `err`."""
    # Multi-org P8 (pre-gate cleanup): org-scope the prompt-template list so one
    # customer's saved prompts never surface to another. SPEC-pt-attach: ALSO apply the
    # customer-visibility ring — a customer-bound caller sees only GLOBAL + its own
    # customer's templates (foreign customer-scoped templates filtered out via the
    # shared access.customer_ring_blocks seam inside list_prompt_templates). Both are
    # no-ops for internal staff / single-tenant (byte-identical to today).
    my_cid = access.current_customer_id(me)
    prompts = wf_service.list_prompt_templates(
        session, org_id=_my_org_id(request), customer_id=my_cid
    )
    # Multi-org: only the caller's own org's GLOBAL workflows (leak vector 12) —
    # another org's global workflow must not appear as a clonable template. No-op off.
    # GATE R2 MED-1: a clonable "global template" is one NOBODY owns. Keying on
    # `scope == GLOBAL` listed every promoted workflow here too, because eval_promote used
    # to mint those GLOBAL WITH a customer_id set, which put one customer's workflow (and
    # its cross-link anchor) in front of every other customer.
    workflows = session.exec(
        access.org_filter(select(Workflow).where(Workflow.customer_id.is_(None)), Workflow, me)
    ).all()
    workflows = _visible_workflows(request, session, workflows)
    wf_steps = {w.id: len(orchestrator.get_steps(session, w.id)) for w in workflows}
    # Cross-links (Wave C): for each template, the workflows whose steps pin it. Names
    # are shown ONLY for workflows the caller can see — build the caller's full visible-
    # workflow map (same org/customer/project rings as the dashboard), then intersect. A bound
    # linguist thus sees honest counts but never a foreign customer's workflow name.
    vwq = access.org_filter(select(Workflow), Workflow, me)
    if my_cid is not None:
        # GATE R2 MED-1: ownership on customer_id, so the "visible workflow" map used for
        # the cross-link names agrees with _wf_or_404 instead of over-reporting.
        vwq = vwq.where(Workflow.customer_id.is_(None) | (Workflow.customer_id == my_cid))
    visible_wf = {
        w.id: w.name for w in _visible_workflows(request, session, session.exec(vwq).all())
    }
    # Cross-link display policy: a viewer confined by org, customer, or project visibility
    # must not learn hidden-workflow USAGE VOLUME from
    # a shared GLOBAL template — the raw step/workflow count and a "(+N not shown)" hint
    # would leak how heavily OTHER customers lean on it. So for a confined viewer we count
    # ONLY the intersection with their visible workflows and suppress the hidden-count
    # hint; unrestricted staff keep the full count + hidden note. DELIBERATE default:
    # the owner can flip to full-count-for-all if cross-customer volume is deemed benign.
    ring_confined = my_cid is not None or access.visible_project_ids(session, me) is not None
    bindings = {}
    for pt in prompts:
        bound_ids = wf_service.prompt_template_bound_workflow_ids(session, pt.id)
        names = [visible_wf[wid] for wid in bound_ids if wid in visible_wf]
        if ring_confined:
            visible_ids = [wid for wid in bound_ids if wid in visible_wf]
            refs = wf_service.prompt_template_reference_count(
                session, pt.id, workflow_ids=visible_ids
            )
            bindings[pt.id] = {
                "steps": refs["steps"],
                "workflows": len(visible_ids),
                "names": names,
                "hidden": 0,
            }
        else:
            refs = wf_service.prompt_template_reference_count(session, pt.id)
            bindings[pt.id] = {
                "steps": refs["steps"],
                "workflows": len(bound_ids),
                "names": names,
                "hidden": len(bound_ids) - len(names),
            }
    # SPEC-pt-attach: the create form's customer <select>. ACTIVE customers only
    # (respects SPEC-customer-archive), org-scoped; a bound caller sees only its own
    # customer (its new PT is forced to that customer at the service anyway). Internal
    # staff see all active customers (+ the "Global" default option in the template).
    cust_q = customer_service.active_only(
        access.org_filter(select(Customer), Customer, me)
    ).order_by(Customer.name)
    if my_cid is not None:
        cust_q = cust_q.where(Customer.id == my_cid)
    pt_customers = session.exec(cust_q).all()
    return templates.TemplateResponse(
        request,
        "building_blocks.html",
        {
            "request": request,
            "prompts": prompts,
            "workflows": workflows,
            "wf_steps": wf_steps,
            "presets": orchestrator.ROLE_PRESETS,
            "system_prompts": system_prompts.list_system_prompts(session, _my_org_id(request)),
            # GitHub #356 phase 2a: the two editable heads (built-in, org override, locked
            # floor, history) and who may edit them. The editor shows only to an admin while
            # the feature flag is on; everyone else on this page sees the same text read-only.
            "sp_editable": [
                system_prompts.editable_detail(session, _my_org_id(request), k)
                for k in system_prompts.EDITABLE_KEYS
            ],
            "sp_enabled": system_prompts.overrides_enabled(),
            "sp_can_edit": system_prompts.overrides_enabled()
            and ((not security.auth_required()) or security.is_admin(me)),
            "sp_draft": sp_draft or {},
            "sp_notice": _sp_notice(request),
            "pt_customers": pt_customers,
            "my_customer_id": my_cid,
            "bindings": bindings,
            "active_tab": "building-blocks",
            "err": err or request.query_params.get("err"),
        },
        status_code=status_code,
    )


_SP_NOTICES = {"saved": "Saved", "reset": "Reset to the built-in text", "restored": "Restored"}


def _sp_notice(request: Request) -> dict | None:
    """The success notice after a system-prompt write, built from short codes (never reflected
    free text): ?sp=saved|reset|restored|unchanged &key=<editable key> &v=<version> &w=1."""
    code = request.query_params.get("sp")
    key = request.query_params.get("key")
    if key not in system_prompts.EDITABLE_KEYS:
        return None
    if code == "unchanged":
        return {"key": key, "text": "Already the built-in text; nothing to reset.", "warn": False}
    if code not in _SP_NOTICES:
        return None
    ver = request.query_params.get("v", "")
    # GitHub #502: int() is only safe on a bounded run of ASCII digits. A Unicode digit (e.g.
    # the superscript two) passes str.isdigit() but makes int() raise, and an ASCII run longer
    # than CPython's int_max_str_digits (4300) raises ValueError too; both would 500 the page.
    # A version outside that shape shows the notice without a version instead of crashing.
    has_ver = ver.isascii() and ver.isdigit() and len(ver) <= 18
    text = _SP_NOTICES[code] + (f" (version {int(ver)})." if has_ver else ".")
    # Gate fix: name WHICH warning(s) the save produced, from one-letter codes mapped to the
    # service's own fixed texts (never reflected free text). "1" is the original "o" code.
    w = request.query_params.get("w", "").replace("1", "o")
    warnings = [_SP_WARN_TEXT[c] for c in _SP_WARN_ORDER if c in w]
    return {"key": key, "text": text, "warn": bool(warnings), "warnings": warnings}


_SP_WARN_TEXT = {"o": system_prompts.WARN_NO_OUTPUT_ONLY}
_SP_WARN_ORDER = ("o",)
_SP_WARN_CODE = {text: code for code, text in _SP_WARN_TEXT.items()}


def _system_prompt_write(request: Request, session: Session, key: str, op: str, **form):
    """The shared body of the three admin-only system-prompt edit routes. Admin gate first,
    then an unknown key (404), then the feature flag (409 naming the switch); a refused edit
    re-renders Building blocks with the typed text kept, a good one redirects (PRG)."""
    from urllib.parse import quote

    me, blocked = _admin_block(request)
    if blocked:
        return blocked
    if key not in system_prompts.EDITABLE_KEYS:
        return _error_page(request, 404, "Not found.")
    if not system_prompts.overrides_enabled():
        return _error_page(request, 409, system_prompts.OFF_MESSAGE)
    org_id = _my_org_id(request)
    actor = _actor_email(request) or "web"
    try:
        if op == "save":
            out = system_prompts.save_override(session, org_id, key, form["body"], actor)
            code = "saved"
        elif op == "restore":
            out = system_prompts.restore_override(session, org_id, key, form["version"], actor)
            code = "restored"
        else:
            out = system_prompts.reset_override(session, org_id, key, actor)
            code = "reset" if out["changed"] else "unchanged"
    except system_prompts.OverrideError as exc:
        draft = {key: form["body"]} if op == "save" else None
        return _building_blocks_page(
            request, session, me, status_code=400, sp_draft=draft, err=str(exc)
        )
    codes = "".join(_SP_WARN_CODE.get(w, "o") for w in out.get("warnings") or [])
    warn = f"&w={codes}" if codes else ""
    ver = f"&v={out['version']}" if out.get("version") else ""
    return RedirectResponse(
        f"/workflows/building-blocks?sp={code}&key={quote(key)}{ver}{warn}#sp-edit-{quote(key)}",
        status_code=303,
    )


@router.post("/workflows/building-blocks/system-prompts/{key}/save")
def save_system_prompt(
    key: str,
    request: Request,
    body: str = Form(""),
    session: Session = Depends(get_session),
):
    """Save a new version of an editable system-prompt head (admin only, flag on)."""
    return _system_prompt_write(request, session, key, "save", body=body)


@router.post("/workflows/building-blocks/system-prompts/{key}/reset")
def reset_system_prompt(key: str, request: Request, session: Session = Depends(get_session)):
    """Back to the built-in text for an editable head (admin only, flag on)."""
    return _system_prompt_write(request, session, key, "reset")


@router.post("/workflows/building-blocks/system-prompts/{key}/restore")
def restore_system_prompt(
    key: str,
    request: Request,
    version: str = Form(""),
    session: Session = Depends(get_session),
):
    """Save a copy of an older version as the newest (admin only, flag on)."""
    return _system_prompt_write(request, session, key, "restore", version=version)


def _workflow_system_prompt_write(
    request: Request, session: Session, workflow_id: int, key: str, op: str, **form
):
    """The shared body of the three per-workflow system-prompt edit routes (GitHub #356 phase
    2b). WHO MAY EDIT: the workflow must be in the caller's org and customer ring
    (`_wf_or_404`, a foreign workflow is a 404 BEFORE the role gate, so a 403 never reveals that
    the id exists), then an ADMIN or a MANAGER (#630, 2026-10-08: one workflow's heads are
    editable by admins and managers; the org-level heads stay admin only; linguists read; 403 via
    system_prompts.can_edit_workflow_heads, the same predicate as /api). Then an unknown key
    (404), then the feature flag (409 naming the switch); a refused edit re-renders the builder
    with the typed text kept, a good one redirects (PRG)."""
    from urllib.parse import quote

    wf, blocked = _wf_or_404(request, session, workflow_id, write=True)
    if blocked:
        return blocked
    if security.auth_required() and not system_prompts.can_edit_workflow_heads(
        security.session_user(request)
    ):
        return _error_page(
            request, 403, "Only an admin or a manager can edit this workflow's system prompt."
        )
    if key not in system_prompts.EDITABLE_KEYS:
        return _error_page(request, 404, "Not found.")
    if not system_prompts.overrides_enabled():
        return _error_page(request, 409, system_prompts.OFF_MESSAGE)
    org_id = _my_org_id(request)
    actor = _actor_email(request) or "web"
    try:
        if op == "save":
            out = system_prompts.save_workflow_override(
                session, org_id, workflow_id, key, form["body"], actor
            )
            code = "saved"
        elif op == "restore":
            out = system_prompts.restore_workflow_override(
                session, org_id, workflow_id, key, form["version"], actor
            )
            code = "restored"
        else:
            out = system_prompts.reset_workflow_override(session, org_id, workflow_id, key, actor)
            code = "reset" if out["changed"] else "unchanged"
    except system_prompts.OverrideError as exc:
        draft = {key: form["body"]} if op == "save" else None
        return _workflow_builder_page(
            request, session, wf, status_code=400, wsp_draft=draft, err=str(exc)
        )
    codes = "".join(_SP_WARN_CODE.get(w, "o") for w in out.get("warnings") or [])
    warn = f"&w={codes}" if codes else ""
    ver = f"&v={out['version']}" if out.get("version") else ""
    return RedirectResponse(
        f"/workflows/{workflow_id}?sp={code}&key={quote(key)}{ver}{warn}#wsp-{quote(key)}",
        status_code=303,
    )


@router.post("/workflows/{workflow_id}/system-prompts/{key}/save")
def save_workflow_system_prompt(
    workflow_id: int,
    key: str,
    request: Request,
    body: str = Form(""),
    session: Session = Depends(get_session),
):
    """Save a new version of this workflow's own system-prompt head (flag on)."""
    return _workflow_system_prompt_write(request, session, workflow_id, key, "save", body=body)


@router.post("/workflows/{workflow_id}/system-prompts/{key}/reset")
def reset_workflow_system_prompt(
    workflow_id: int, key: str, request: Request, session: Session = Depends(get_session)
):
    """Inherit again (org text, else built-in) for this workflow's head (flag on)."""
    return _workflow_system_prompt_write(request, session, workflow_id, key, "reset")


@router.post("/workflows/{workflow_id}/system-prompts/{key}/restore")
def restore_workflow_system_prompt(
    workflow_id: int,
    key: str,
    request: Request,
    version: str = Form(""),
    session: Session = Depends(get_session),
):
    """Save a copy of an older version of this workflow's text as the newest (flag on)."""
    return _workflow_system_prompt_write(
        request, session, workflow_id, key, "restore", version=version
    )


@router.post("/workflows")
def create_workflow(
    request: Request,
    name: str = Form(...),
    scope: str = Form(Scope.GLOBAL),
    customer_id: str = Form(""),
    session: Session = Depends(get_session),
):
    _, blocked = _ai_block(request)
    if blocked:
        return blocked
    # chip task_56b8b2bf: refuse a cross-tenant customer_id (foreign customer / other
    # org) BEFORE the archived check, so a foreign customer's name never leaks in a 400.
    blocked = _wf_create_customer_block(request, session, customer_id)
    if blocked:
        return blocked
    # T2-01: an archived customer cannot be attached to a new (customer-scoped)
    # workflow — same refusal content.project_create applies to projects.
    blocked = _archived_customer_block(request, session, customer_id, "create a workflow for it")
    if blocked:
        return blocked
    # Stamp the creating user's org (leak vector 12). No-op (NULL) with the gate off.
    # bound_customer_id funnels the caller's own binding through the shared create seam
    # (defense-in-depth: the seam re-refuses a bound/cross-org customer_id even if a
    # future caller forgets the route pre-check).
    try:
        wf = wf_service.create_workflow(
            session,
            name,
            scope=scope,
            customer_id=customer_id,
            org_id=_my_org_id(request),
            bound_customer_id=_my_customer_id(request),
        )
    except wf_service.WorkflowForeignCustomerError:
        return _error_page(request, 404, "Not found.")
    except ids.InvalidNameError as exc:
        from urllib.parse import quote

        return RedirectResponse("/workflows?err=" + quote(str(exc)), status_code=303)
    if wf is None:
        return RedirectResponse("/workflows", status_code=303)
    return RedirectResponse(f"/workflows/{wf.id}", status_code=303)


# --- workflow rename + delete (SPEC-rename-delete, Branch 1) ---
@router.post("/workflows/{workflow_id}/rename")
def rename_workflow(
    workflow_id: int,
    request: Request,
    name: str = Form(""),
    session: Session = Depends(get_session),
):
    """Rename a workflow. RENAME uses the workflow family gate (_ai_block =
    can_tune_ai), matching the other workflow-mutation routes. Logs old->new name."""
    _, blocked = _ai_block(request)
    if blocked:
        return blocked
    wf, blocked = _wf_or_404(request, session, workflow_id, write=True)
    if blocked:
        return blocked
    old_name = wf.name
    try:
        wf_service.rename_workflow(session, workflow_id, name)
    except wf_service.WorkflowError as exc:
        return _error_page(request, 400, str(exc))
    audit.log(
        actor=_actor_email(request),
        action="workflow.rename",
        target=f"workflow {workflow_id}",
        detail=f"{old_name!r} -> {wf.name!r}",
        ip=_ip(request),
        customer_id=wf.customer_id,
    )
    return RedirectResponse(f"/workflows/{workflow_id}", status_code=303)


@router.post("/workflows/{workflow_id}/delete")
def delete_workflow(workflow_id: int, request: Request, session: Session = Depends(get_session)):
    """Delete a workflow. Every NEW delete is strictly admin (_admin_block). Fails
    closed (409) while referenced / in-flight; otherwise cascades owned steps +
    runs + step-runs and logs the itemized cascade counts."""
    # Resolve the object (incl. the R2-3 customer ring) BEFORE the admin gate so a
    # customer-bound caller's FOREIGN workflow is a 404 (indistinguishable from
    # missing), never a 403 that would leak its existence. A bound caller is never
    # admin, so past the ring it still hits _admin_block on its own/global rows.
    wf, blocked = _wf_or_404(request, session, workflow_id, write=True)
    if blocked:
        return blocked
    _, blocked = _admin_block(request)
    if blocked:
        return blocked
    name, customer_id = wf.name, wf.customer_id
    try:
        refs = wf_service.delete_workflow(session, workflow_id)
    except wf_service.WorkflowError as exc:
        return _error_page(request, 409, str(exc))
    audit.log(
        actor=_actor_email(request),
        action="workflow.delete",
        target=f"workflow {workflow_id} '{name}'",
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
        ip=_ip(request),
        customer_id=customer_id,
    )
    return RedirectResponse("/workflows", status_code=303)


@router.post("/workflows/{workflow_id}/archive")
def archive_workflow(workflow_id: int, request: Request, session: Session = Depends(get_session)):
    """Archive (retire) a workflow (residual sweep: archive verbs). Admin-only (same
    posture as delete + the Customer archive precedent). Ring BEFORE the admin gate so a
    foreign workflow is a 404, not a 403 that leaks its existence. Drops it from the
    default list/pickers + dispatch and blocks new runs; steps + history preserved,
    reversible. Idempotent-safe. The service audits workflow.archive."""
    _wf, blocked = _wf_or_404(request, session, workflow_id, write=True)
    if blocked:
        return blocked
    _, blocked = _admin_block(request)
    if blocked:
        return blocked
    try:
        wf_service.archive_workflow(
            session,
            workflow_id,
            actor=_actor_email(request),
            ip=_ip(request),
            org_id=_my_org_id(request),
        )
    except wf_service.WorkflowError as exc:
        return _error_page(request, 400, str(exc))
    return RedirectResponse(f"/workflows/{workflow_id}", status_code=303)


@router.post("/workflows/{workflow_id}/unarchive")
def unarchive_workflow(workflow_id: int, request: Request, session: Session = Depends(get_session)):
    """Unarchive (reactivate) a workflow (residual sweep). Admin-only. Restores it to the
    default list/pickers + dispatch and makes it runnable again. Idempotent-safe. The
    service audits workflow.unarchive."""
    _wf, blocked = _wf_or_404(request, session, workflow_id, write=True)
    if blocked:
        return blocked
    _, blocked = _admin_block(request)
    if blocked:
        return blocked
    try:
        wf_service.unarchive_workflow(
            session,
            workflow_id,
            actor=_actor_email(request),
            ip=_ip(request),
            org_id=_my_org_id(request),
        )
    except wf_service.WorkflowError as exc:
        return _error_page(request, 400, str(exc))
    return RedirectResponse(f"/workflows/{workflow_id}", status_code=303)


def _wsp_ctx(request: Request, session: Session, workflow_id: int, wsp_draft=None):
    """Template context of the builder's System prompt section (GitHub #356 phase 2b): this
    workflow's own override (or inherit) per editable head, who may edit them (an admin or a
    manager, #630, while the feature flag is on; linguists read) and the one-shot notice after a
    write. Gate fix: the section is shown only to the read audience of the same text on /api
    (admins, managers and linguists); a translator who can open the builder does not get the
    heads."""
    me = security.session_user(request)
    can_read = (not security.auth_required()) or system_prompts.can_read_workflow_heads(me)
    return {
        "wsp_can_read": can_read,
        "wsp_editable": [
            system_prompts.workflow_editable_detail(session, _my_org_id(request), workflow_id, k)
            for k in system_prompts.EDITABLE_KEYS
        ]
        if can_read
        else [],
        "wsp_enabled": system_prompts.overrides_enabled(),
        "wsp_can_edit": system_prompts.overrides_enabled()
        and ((not security.auth_required()) or system_prompts.can_edit_workflow_heads(me)),
        "wsp_draft": wsp_draft or {},
        "wsp_notice": _sp_notice(request),
    }


@router.get("/workflows/{workflow_id}")
def workflow_builder(
    workflow_id: int,
    request: Request,
    draft: str = "",
    session: Session = Depends(get_session),
):
    wf, blocked = _wf_or_404(request, session, workflow_id)
    if blocked:
        return blocked
    return _workflow_builder_page(request, session, wf, draft)


def _workflow_builder_page(
    request: Request,
    session: Session,
    wf,
    draft: str = "",
    *,
    status_code: int = 200,
    wsp_draft: dict | None = None,
    err: str | None = None,
):
    """Render the workflow builder. Shared by the GET and by a refused per-workflow
    system-prompt save, which re-renders with the typed text kept (`wsp_draft`) and the
    refusal as `err` (GitHub #356 phase 2b)."""
    workflow_id = wf.id
    steps = orchestrator.get_steps(session, workflow_id)
    from app.kernel import glossary_module as gm
    from app.kernel import languages_catalog, vendors

    grp = session.get(WorkflowGroup, wf.group_id) if wf.group_id else None
    # Glossary dropdown: EVERY approved glossary visible to this caller, with its own
    # language pairs shown in the label (a workflow is language-agnostic; a glossary is
    # applied at run time only for runs whose languages it covers). Blank-pair query =
    # the whole approved set; org/customer confinement and content-type behaviour are
    # unchanged. Feeds match_terms(glossary_id=) at run time.
    glossary_options = [
        {"id": g.id, "label": gm.option_label(session, g)}
        for g in gm.options_for_step(
            session,
            "",
            "",
            org_id=_my_org_id(request),
            customer_id=_wf_glossary_customer(request, session, wf),
        )
    ]
    # External-step vendor picker (Wave A): the org's non-archived vendors UNIONed with
    # any active vendor linked to this workflow's customer (cross-org linking, 2026-08-08 —
    # a customer's connected vendor may live in another org and must still be offerable).
    # The service (add_step/update_step) fail-closes on a vendor not linked to the
    # document's customer, so this list is a convenience, not the security boundary.
    vendor_options = vendors.list_vendors_for_picker(
        session, org_id=_my_org_id(request), customer_id=getattr(wf, "customer_id", None)
    )
    return templates.TemplateResponse(
        request,
        "workflow_builder.html",
        {
            "request": request,
            "wf": wf,
            "steps": steps,
            "providers": config.PROVIDERS,
            "step_types": [
                StepType.LLM_AGENT,
                StepType.LQA,
                StepType.HUMAN_REVIEW,
                StepType.TM_MATCH,
                StepType.EXTERNAL,
            ],
            "presets": orchestrator.ROLE_PRESETS,
            # #630: the prompt a blank Inline prompt falls back to (the locked field's placeholder).
            "default_prompts": {
                getattr(k, "value", k): v for k, v in orchestrator.DEFAULT_PROMPTS.items()
            },
            "draft": draft,
            "group": grp,
            "locales": languages_catalog.enabled_locales(),
            "label_for": languages_catalog.label_for,
            "glossary_options": glossary_options,
            "vendor_options": vendor_options,
            # SPEC-prompt-template-runtime: the step prompt-template picker options (org +
            # caller customer-ring scoped). Empty = the picker offers only "none".
            "prompt_templates": _visible_prompt_templates(request, session),
            # SPEC-workflows-redesign 4b: one-shot banner after a base edit / pin-unpin
            # (None on the author fallback POST — no such query param there).
            "notice": request.query_params.get("notice"),
            **_wf_copy_targets_ctx(request, session, wf),
            **_shape_ctx(session, wf, steps),
            **_wsp_ctx(request, session, workflow_id, wsp_draft),
            "err": err,
        },
        status_code=status_code,
    )


@router.post("/workflows/models")
def workflow_models(request: Request, provider: str = Form("")):
    """Model ids for a provider ref — powers the workflow-builder step model
    dropdowns (Model + Fallback model). Free metadata call; {"models": [...]} with
    [] on any error (never 500).

    AUTH: gated by _ai_block — EXACTLY the predicate add_step enforces (admins +
    linguists), so this route rejects precisely whoever cannot add a step and accepts
    precisely whoever can. Org-scoped: an engine:<id> ref only lists when it belongs
    to the caller's org (list_models_for_ref enforces the ownership gate).

    SECURITY: only a `provider` ref is accepted — NO base_url / api_key form field.
    The seam uses stored/config credentials exclusively, so this route can never be
    used to send a stored cloud/engine key to a caller-supplied host."""
    _me, blocked = _ai_block(request)
    if blocked:
        return blocked
    return JSONResponse({"models": list_models_for_ref(provider, _my_org_id(request))})


@router.post("/workflows/{workflow_id}/steps")
def add_step(
    workflow_id: int,
    request: Request,
    name: str = Form(""),
    step_type: str = Form(StepType.LLM_AGENT),
    provider: str = Form(""),
    model: str = Form(""),
    fallback_provider: str = Form(""),
    fallback_model: str = Form(""),
    prompt_text: str = Form(""),
    prompt_template_id: str = Form(""),
    chunk_policy: str = Form("auto"),
    redact_pii: str = Form(""),
    adaptive_feedback: str = Form(""),
    adaptive_feedback_window: str = Form("5"),
    doc_context_prepass: str = Form(""),
    qe_gate: str = Form(""),
    qe_threshold: str = Form("70"),
    qe_provider: str = Form(""),
    qe_autofix: str = Form(""),
    qe_autofix_provider: str = Form(""),
    qe_autofix_max_rounds: str = Form("3"),
    qe_autofix_cross_check: str = Form(""),
    lqa_threshold: str = Form(""),
    tm_examples: str = Form(""),
    tm_examples_window: str = Form("3"),
    use_image_context: str = Form(""),
    concept_glossary_render: str = Form(""),
    glossary_concept_cap: str = Form("10"),
    glossary_token_cap: str = Form("0"),
    tm_protect: str = Form("auto"),
    over_translation_guard: str = Form("auto"),
    memory_mode: str = Form("auto"),
    tm_fill_threshold: str = Form(""),
    tm_fuzzy_lock_mode: str = Form(""),
    tm_lock_ice: str = Form("auto"),
    tm_hundreds_enter_lqa: str = Form("auto"),
    tm_context_floor: str = Form(""),
    tm_pool_selection: str = Form(""),
    assignee_email: str = Form(""),
    glossary_id: str = Form(""),
    vendor_id: str = Form(""),
    external_wait_hours: str = Form(""),
    run_condition_ref: str = Form(""),
    run_condition_op: str = Form(""),
    run_condition_value: str = Form(""),
    step_key: str = Form(""),
    branch_on_pass: str = Form(""),
    branch_on_fail: str = Form(""),
    max_visits: str = Form(""),
    parallel_group: str = Form(""),
    session: Session = Depends(get_session),
):
    _me, blocked = _ai_block(request)
    if blocked:
        return blocked
    wf, blocked = _wf_or_404(request, session, workflow_id, write=True)
    if blocked:
        return blocked
    # SPEC-prompt-template-runtime: re-validate a submitted prompt-template binding
    # against the caller's visible set (a foreign/stale id silently clears to None); the
    # service then enforces existence + the source-placeholder rule (and refuses it as a
    # dead field on an lqa/human_review step).
    ptid = _validated_prompt_template_id(request, session, prompt_template_id)
    # T1-01: reject an llm_agent step whose non-empty prompt omits {text}/{prior}
    # (it would send the model no source). Shared validator with the CLI add-step.
    from app.kernel import prompts

    try:
        # GitHub #43: the RETURN value is the SOFT language warning (an llm_agent prompt
        # that names no target language). Web+CLI parity (adversarial review P6): the CLI
        # has surfaced it since #43 landed and the builder did not, so the surface where
        # prompts are actually WRITTEN was the one surface that stayed silent about the
        # defect that shipped an English namespace to a customer. Carried to the builder
        # banner on the redirect below, via the existing notice channel; never fatal.
        prompt_warning = prompts.validate_step_prompt(step_type, prompt_text)
    except prompts.PromptValidationError as exc:
        return _error_page(request, 400, str(exc))
    # Glossary dropdown: a submitted glossary_id must be an APPROVED glossary VISIBLE
    # to this caller - re-run the exact same filter the picker used (a blank pair: the
    # whole approved, visible set) so a tampered/stale id can't pin a foreign or
    # unapproved glossary. A non-covering pin is allowed here; the run-time resolver
    # skips it with a note if it does not cover the run's languages. Anything that
    # fails the approved+visible filter -> None (project scoped), never an error.
    gid = None
    gid_raw = ids.coerce_id(glossary_id)
    if gid_raw is not None:
        from app.kernel import glossary_module as gm

        allowed = {
            g.id
            for g in gm.options_for_step(
                session,
                "",
                "",
                org_id=_my_org_id(request),
                customer_id=_wf_glossary_customer(request, session, wf),
            )
        }
        if gid_raw in allowed:
            gid = gid_raw
    # SPEC §5 pre-step guard (Phase 2): assemble the run_condition from the small
    # ref/op/value editor. A blank ref = no guard (always run); a score op reads the
    # numeric value box. add_step validates it against the workflow's earlier steps
    # (fail-closed) and raises WorkflowError, which we surface as a clean 400 page.
    run_condition = None
    if (run_condition_ref or "").strip():
        rc_val = (run_condition_value or "").strip()
        try:
            rc_value = float(rc_val) if rc_val else None
        except ValueError:
            # Leave a non-numeric value string in place so validate_run_condition
            # rejects a score op with a bad value (a clean 400), never a 500.
            rc_value = rc_val
        run_condition = {
            "ref": run_condition_ref.strip(),
            "op": (run_condition_op or "").strip(),
            "value": rc_value,
        }
    # SPEC §5 Phase 3: assemble the post-step branch from the on_pass/on_fail target
    # pickers (each a step key, or the sentinel 'next'/'end'; blank = that slot unset).
    # add_step validates it against the workflow's earlier keys + the gate rule (D3),
    # fail-closed, and raises WorkflowError -> a clean 400. A blank max_visits inherits
    # the run-level default. The step key is slugified + uniqueness-checked in add_step.
    from app.kernel import workflows as _wf

    branch = {}
    if (branch_on_pass or "").strip():
        branch["on_pass"] = _wf.parse_branch_str(branch_on_pass)
    if (branch_on_fail or "").strip():
        branch["on_fail"] = _wf.parse_branch_str(branch_on_fail)
    branch = branch or None
    mv = (max_visits or "").strip() or None
    # SPEC-parallel-routing §6 -- the ADD form's hidden-dead-field dead end (adversarial
    # review, SURFACES-9). The honest-fields JS only HIDES a field; the form still POSTs
    # it. So typing a group slug and THEN switching the type to llm_agent submitted a
    # parallel_group the user could no longer see, and add_step refused it -- an error
    # about a field with no widget on screen, unfixable without knowing to switch the
    # type back and blank it. This is the same shape update_step's TYPE SWITCH already
    # solved, so it gets the same answer: force-clear the now-dead value and SAY SO in
    # the banner. The CLI keeps its hard refusal, where `--parallel-group` is right there
    # in the command the operator typed and nothing is hidden.
    pg = (parallel_group or "").strip() or None
    cleared_note = ""
    if pg and "parallel_group" in wf_service.DEAD_FIELDS_BY_TYPE.get(step_type, ()):
        pg = None
        cleared_note = (
            f"Cleared the parallel group (it has no effect on a {step_type} step -- Phase 1 runs "
            "parallel CHECKS only, so only lqa steps can be group members)."
        )
    try:
        saved_step = wf_service.add_step(
            session,
            workflow_id,
            name=name,
            step_type=step_type,
            provider=provider,
            model=model,
            fallback_provider=fallback_provider,
            fallback_model=fallback_model,
            prompt_text=prompt_text,
            prompt_template_id=ptid,
            chunk_policy=chunk_policy,
            redact_pii=bool(redact_pii),
            # F2 engine-layer upgrades (checkboxes -> truthy string; window is an int
            # field). add_step clamps the window and coerces the flags.
            adaptive_feedback=bool(adaptive_feedback),
            adaptive_feedback_window=adaptive_feedback_window,
            doc_context_prepass=bool(doc_context_prepass),
            # QE routing gate (roadmap gap #1/#6): checkbox -> truthy string; threshold is
            # a number field; qe_provider "" normalises to None in add_step (reuse the
            # step's own provider). add_step refuses invalid thresholds + coerces the flag.
            qe_gate=bool(qe_gate),
            qe_threshold=qe_threshold,
            qe_provider=qe_provider,
            # QE auto-fix (Train B): checkboxes -> truthy string; the fixer "" = the step's
            # own provider; rounds outside 1-5 are refused. The fixer is role-checked there.
            qe_autofix=bool(qe_autofix),
            qe_autofix_provider=qe_autofix_provider,
            qe_autofix_max_rounds=qe_autofix_max_rounds,
            qe_autofix_cross_check=bool(qe_autofix_cross_check),
            # feat/lqa-configurable-threshold: number field, blank -> None (the step keeps
            # the built-in default gate at 90). add_step clamps 0-100 and refuses a real
            # value on a non-lqa step. The builder shows the input only on lqa steps
            # (data-types="lqa"); a hidden field posts "" -> inert everywhere else.
            lqa_threshold=lqa_threshold,
            # TM few-shot examples (SPEC 2B): checkbox -> truthy string; window is a number
            # field. The builder renders the checkbox CHECKED (default ON), so an unchecked
            # box submits "" -> OFF. add_step clamps the window + coerces the flag.
            tm_examples=bool(tm_examples),
            tm_examples_window=tm_examples_window,
            # Batch 14 T2 (SPEC §25): the per-step image-context checkbox. Rendered CHECKED
            # (default ON), so an unchecked box submits "" -> OFF (the tm_examples posture).
            use_image_context=bool(use_image_context),
            # Terminology T3 (SPEC §9-T3): the concept-glossary renderer checkbox (default
            # OFF: unchecked -> "" -> False) + the two number caps (add_step coerces them).
            concept_glossary_render=bool(concept_glossary_render),
            glossary_concept_cap=glossary_concept_cap,
            glossary_token_cap=glossary_token_cap,
            # LQA safety floor D1: the TM provenance guard's tristate select
            # (auto/on/off). add_step's _coerce_tm_protect normalises it; anything
            # unrecognised falls back to 'auto' (inherit the correction-loop default).
            tm_protect=tm_protect,
            # run-190 Item D / M5: the over-translation guard's tristate select (auto/on/off);
            # add_step's _coerce_tm_protect normalises it (unrecognised -> auto -> inherit).
            over_translation_guard=over_translation_guard,
            # LLMemory step-level opt-out (PROPOSAL-llmemory §6): the recall tristate
            # select (auto/off). add_step's _coerce_memory_mode normalises it.
            memory_mode=memory_mode,
            # Batch 15 T1 (SPEC-tm-match-step-config §8.B): the tm_match config controls.
            # Tri-state selects submit "auto"/"inherit" for the two bools (-> NULL inherit,
            # which SEEDS a concrete snapshot from the current globals on a NEW tm_match
            # step); blank number fields -> NULL; the multi-select posts a list of readable
            # pool ids (empty -> NULL = all readable). add_step's coercers + dead-field
            # refusal handle a value forged onto a non-tm_match step.
            tm_fill_threshold=tm_fill_threshold,
            tm_fuzzy_lock_mode=tm_fuzzy_lock_mode,
            tm_lock_ice=tm_lock_ice,
            tm_hundreds_enter_lqa=tm_hundreds_enter_lqa,
            tm_context_floor=tm_context_floor,
            tm_pool_selection=tm_pool_selection,
            # Feature B: only meaningful on a human_review step (add_step ignores it
            # on other step types).
            assignee_email=assignee_email,
            glossary_id=gid,
            # External-step config (Wave A): the vendor + wait override. add_step
            # validates the vendor is selectable + linked to the workflow's customer
            # (fail-closed, WorkflowError -> the 400 page below), and treats both as
            # DEAD fields on every non-external step (a stray value is refused there).
            vendor_id=vendor_id,
            external_wait_hours=external_wait_hours,
            run_condition=run_condition,
            # SPEC §5 Phase 3 (D1/D3/D4): stable key + post-step branch + loop ceiling.
            key=(step_key or "").strip() or None,
            branch=branch,
            max_visits=mv,
            # SPEC-parallel-routing §6: the builder's "Parallel steps" field. Blank =
            # not in a group. add_step slugifies it and re-validates the whole
            # membership graph (fail-closed) -> a clean 400 page, never a 500.
            parallel_group=pg,
        )
    except wf_service.WorkflowError as exc:
        return _error_page(request, 400, str(exc))
    from app.kernel.qe_autofix import configuration_warnings

    prompt_warning = "; ".join(filter(None, [prompt_warning, *configuration_warnings(saved_step)]))
    return _redirect_with_notice(
        f"/workflows/{workflow_id}", _with_prompt_warning(cleared_note, prompt_warning)
    )


@router.post("/workflows/{workflow_id}/steps/{step_id}/delete")
def delete_step(
    workflow_id: int, step_id: int, request: Request, session: Session = Depends(get_session)
):
    _, blocked = _ai_block(request)
    if blocked:
        return blocked
    _wf, blocked = _wf_or_404(request, session, workflow_id, write=True)
    if blocked:
        return blocked
    step = session.get(WorkflowStep, step_id)
    if step is None or step.workflow_id != workflow_id:
        return _error_page(request, 404, "Not found.")
    # delete_step is fail-closed now (an in-flight run, or a delete that would orphan a
    # dependent guard/branch) -- surface the refusal as the same clean 400 page an
    # add/edit refusal produces, never a 500.
    try:
        wf_service.delete_step(session, step_id, workflow_id=workflow_id)
    except wf_service.WorkflowError as exc:
        return _error_page(request, 400, str(exc))
    return RedirectResponse(f"/workflows/{workflow_id}", status_code=303)


@router.post("/workflows/{workflow_id}/steps/{step_id}/edit")
def edit_step(
    workflow_id: int,
    step_id: int,
    request: Request,
    name: str = Form(""),
    step_type: str = Form(StepType.LLM_AGENT),
    provider: str = Form(""),
    model: str = Form(""),
    fallback_provider: str = Form(""),
    fallback_model: str = Form(""),
    prompt_text: str = Form(""),
    prompt_template_id: str = Form(""),
    chunk_policy: str = Form("auto"),
    redact_pii: str = Form(""),
    adaptive_feedback: str = Form(""),
    adaptive_feedback_window: str = Form("5"),
    doc_context_prepass: str = Form(""),
    qe_gate: str = Form(""),
    qe_threshold: str = Form("70"),
    qe_provider: str = Form(""),
    qe_autofix: str = Form(""),
    qe_autofix_provider: str = Form(""),
    qe_autofix_max_rounds: str = Form("3"),
    qe_autofix_cross_check: str = Form(""),
    lqa_threshold: str = Form(""),
    tm_examples: str = Form(""),
    tm_examples_window: str = Form("3"),
    use_image_context: str = Form(""),
    concept_glossary_render: str = Form(""),
    glossary_concept_cap: str = Form("10"),
    glossary_token_cap: str = Form("0"),
    tm_protect: str = Form("auto"),
    over_translation_guard: str = Form("auto"),
    memory_mode: str = Form("auto"),
    tm_fill_threshold: str = Form(""),
    tm_fuzzy_lock_mode: str = Form(""),
    tm_lock_ice: str = Form("auto"),
    tm_hundreds_enter_lqa: str = Form("auto"),
    tm_context_floor: str = Form(""),
    tm_pool_selection: str = Form(""),
    assignee_email: str = Form(""),
    glossary_id: str = Form(""),
    vendor_id: str = Form(""),
    external_wait_hours: str = Form(""),
    run_condition_ref: str = Form(""),
    run_condition_op: str = Form(""),
    run_condition_value: str = Form(""),
    step_key: str = Form(""),
    branch_on_pass: str = Form(""),
    branch_on_fail: str = Form(""),
    max_visits: str = Form(""),
    parallel_group: str = Form(""),
    session: Session = Depends(get_session),
):
    """Edit an EXISTING step in place. The builder's per-step <details> edit form is
    PRE-FILLED with the step's current values and POSTs the FULL field set here, so this
    is a full-config update (not partial) — the same field surface as add-step, run
    through the SAME normalisation/validation seams. The whole-graph re-validation in
    update_step (fail-closed) surfaces as a clean 400 page, never a 500. can_write /
    customer-ring gated (via _ai_block + _wf_or_404), CSRF-protected by the middleware."""
    from app.models import WorkflowStep

    _me, blocked = _ai_block(request)
    if blocked:
        return blocked
    wf, blocked = _wf_or_404(request, session, workflow_id, write=True)
    if blocked:
        return blocked
    step = session.get(WorkflowStep, step_id)
    if step is None or step.workflow_id != workflow_id:
        return _error_page(request, 404, "Not found.")
    # SPEC-prompt-template-runtime: re-validate the submitted prompt-template binding
    # (foreign/stale -> None). The edit form always full-POSTs, so this is a real edit of
    # the binding every time; the service validates + refuses a dead binding on a type
    # switch to lqa/human_review (force-cleared there).
    ptid = _validated_prompt_template_id(request, session, prompt_template_id)
    # T1-01 parity with add-step: reject an llm_agent step whose non-empty prompt omits
    # {text}/{prior} (it would send the model no source).
    from app.kernel import prompts

    try:
        prompt_warning = prompts.validate_step_prompt(step_type, prompt_text)
    except prompts.PromptValidationError as exc:
        return _error_page(request, 400, str(exc))
    # ...but the SOFT language warning only when this edit actually touched the effective
    # prompt (adversarial review P12e). This form FULL-POSTs every field on every save, so
    # without the comparison a rename or a provider change would re-warn about a prompt
    # the operator never looked at — noise that trains the banner out of being read. The
    # HARD rule above is deliberately unconditional (a type switch alone can make a saved
    # prompt illegal).
    if (prompt_text or "") == (step.prompt_text or "") and step_type == step.step_type:
        prompt_warning = ""
    # Glossary re-validation (identical to add-step): a submitted glossary_id must be an
    # APPROVED glossary VISIBLE to this caller (a blank pair: the whole approved, visible
    # set), else it clears to None (project-scoped) - a tampered/stale id can't pin a
    # foreign one. A non-covering pin is allowed; the run-time resolver skips it with a note.
    gid = None
    gid_raw = ids.coerce_id(glossary_id)
    if gid_raw is not None:
        from app.kernel import glossary_module as gm

        allowed = {
            g.id
            for g in gm.options_for_step(
                session,
                "",
                "",
                org_id=_my_org_id(request),
                customer_id=_wf_glossary_customer(request, session, wf),
            )
        }
        if gid_raw in allowed:
            gid = gid_raw
    # Assemble the run_condition from the ref/op/value editor (blank ref = no guard).
    run_condition = None
    if (run_condition_ref or "").strip():
        rc_val = (run_condition_value or "").strip()
        try:
            rc_value = float(rc_val) if rc_val else None
        except ValueError:
            rc_value = rc_val
        run_condition = {
            "ref": run_condition_ref.strip(),
            "op": (run_condition_op or "").strip(),
            "value": rc_value,
        }
    # Assemble the post-step branch from the on_pass/on_fail pickers (blank = slot unset).
    from app.kernel import workflows as _wf

    branch = {}
    if (branch_on_pass or "").strip():
        branch["on_pass"] = _wf.parse_branch_str(branch_on_pass)
    if (branch_on_fail or "").strip():
        branch["on_fail"] = _wf.parse_branch_str(branch_on_fail)
    branch = branch or None
    mv = (max_visits or "").strip() or None
    # SPEC-workflows-redesign 4b: collect the shared-base propagation report so the
    # builder can surface what a BASE edit swept (and a VARIANT edit's auto-pin).
    report: dict = {}
    old_prompt = step.prompt_text or ""
    try:
        saved_step = wf_service.update_step(
            session,
            step_id,
            name=name,
            step_type=step_type,
            provider=provider,
            model=model,
            fallback_provider=fallback_provider,
            fallback_model=fallback_model,
            prompt_text=prompt_text,
            prompt_template_id=ptid,
            chunk_policy=chunk_policy,
            redact_pii=bool(redact_pii),
            adaptive_feedback=bool(adaptive_feedback),
            adaptive_feedback_window=adaptive_feedback_window,
            doc_context_prepass=bool(doc_context_prepass),
            qe_gate=bool(qe_gate),
            qe_threshold=qe_threshold,
            qe_provider=qe_provider,
            # QE auto-fix (Train B): checkboxes -> truthy string; the fixer "" = the step's
            # own provider; rounds outside 1-5 are refused. The fixer is role-checked there.
            qe_autofix=bool(qe_autofix),
            qe_autofix_provider=qe_autofix_provider,
            qe_autofix_max_rounds=qe_autofix_max_rounds,
            qe_autofix_cross_check=bool(qe_autofix_cross_check),
            # feat/lqa-configurable-threshold: the edit form full-POSTs, so lqa_threshold
            # is always supplied — blank clears it back to the built-in default 90; a real
            # value gates on it (refused on a non-lqa step; a switch off lqa force-clears).
            lqa_threshold=lqa_threshold,
            tm_examples=bool(tm_examples),
            tm_examples_window=tm_examples_window,
            # Batch 14 T2 (SPEC §25): the edit form full-POSTs, so use_image_context is
            # always supplied — the checkbox reflects the stored value; unchecked -> OFF.
            use_image_context=bool(use_image_context),
            # Terminology T3 (SPEC §9-T3): the edit form full-POSTs, so these are always
            # supplied — the checkbox reflects the stored value (unchecked -> OFF), the
            # number caps clamp to >=0 (blank/junk -> default).
            concept_glossary_render=bool(concept_glossary_render),
            glossary_concept_cap=glossary_concept_cap,
            glossary_token_cap=glossary_token_cap,
            tm_protect=tm_protect,
            # run-190 Item D / M5: the over-translation guard opt-out (edit form full-POSTs,
            # so it is always supplied; the select reflects the stored tristate).
            over_translation_guard=over_translation_guard,
            # LLMemory step-level opt-out (PROPOSAL-llmemory §6): parity with add-step.
            memory_mode=memory_mode,
            # Batch 15 T1 (SPEC-tm-match-step-config §8.B): the edit form full-POSTs, so
            # each is always supplied -- a blank number / "auto" tri-state clears the
            # override back to inherit; a concrete value is a per-step override. A value on
            # a non-tm_match step is refused; a switch away from tm_match force-clears them.
            tm_fill_threshold=tm_fill_threshold,
            tm_fuzzy_lock_mode=tm_fuzzy_lock_mode,
            tm_lock_ice=tm_lock_ice,
            tm_hundreds_enter_lqa=tm_hundreds_enter_lqa,
            tm_context_floor=tm_context_floor,
            tm_pool_selection=tm_pool_selection,
            assignee_email=assignee_email,
            glossary_id=gid,
            # External-step config (Wave A): parity with add-step. The edit form full-
            # POSTs, so both are always supplied — a switch AWAY from external force-clears
            # vendor_id in update_step, and a non-external step refuses a meaningful value.
            vendor_id=vendor_id,
            external_wait_hours=external_wait_hours,
            run_condition=run_condition,
            key=(step_key or "").strip() or None,
            branch=branch,
            max_visits=mv,
            # SPEC-parallel-routing §6: the edit form full-POSTs, so a blank field is a
            # real "leave the group" edit (the same contract every other text field on
            # this form has).
            parallel_group=(parallel_group or "").strip() or None,
            report=report,
        )
    except wf_service.WorkflowError as exc:
        return _error_page(request, 400, str(exc))
    if (saved_step.prompt_text or "") != old_prompt:
        # #630: a step prompt change (Apply, or Restore default prompt = cleared text) is
        # audited like the API/CLI edit does. Length and hash only, never the prompt text.
        import hashlib

        new_prompt = saved_step.prompt_text or ""
        audit.log(
            actor=_actor_email(request) or "web",
            action="workflow.edit_step",
            target=f"workflow {workflow_id} step {step_id}",
            detail=(
                "changed: prompt_text "
                + (
                    "restored to default (cleared)"
                    if not new_prompt.strip()
                    else f"chars={len(new_prompt)} "
                    f"sha256={hashlib.sha256(new_prompt.encode()).hexdigest()[:16]}"
                )
            ),
            ip=_ip(request),
            customer_id=wf.customer_id,
        )
    from app.kernel.qe_autofix import configuration_warnings

    prompt_warning = "; ".join(filter(None, [prompt_warning, *configuration_warnings(saved_step)]))
    return _redirect_with_notice(
        f"/workflows/{workflow_id}",
        _with_prompt_warning(_shared_base_notice(report), prompt_warning),
    )


def _step_prompt_target(request, session, workflow_id: int, step_id: int):
    """(step, wf, blocked) for the two step-PROMPT-only routes (Apply prompt, Restore default
    prompt). Org and customer ring first (_wf_or_404, write=True: a 404 for a foreign workflow,
    a 403 for a customer-bound user on a global one), then the role gate
    (system_prompts.can_edit_step_prompt: admin, linguist or manager, 2026-10-08), then
    the step must belong to the workflow (404)."""
    from app.models import WorkflowStep

    wf, blocked = _wf_or_404(request, session, workflow_id, write=True)
    if blocked:
        return None, None, blocked
    _me, blocked = _step_prompt_or_default_block(request, system_prompts.can_edit_step_prompt)
    if blocked:
        return None, None, blocked
    step = session.get(WorkflowStep, step_id)
    if step is None or step.workflow_id != workflow_id:
        return None, None, _error_page(request, 404, "Not found.")
    return step, wf, None


@router.post("/workflows/{workflow_id}/steps/{step_id}/prompt")
def apply_step_prompt(
    workflow_id: int,
    step_id: int,
    request: Request,
    prompt_text: str = Form(""),
    session: Session = Depends(get_session),
):
    """Apply ONE step's inline prompt and nothing else: the door a
    manager's Unlock and Apply use on the builder (a manager does not get the full step edit
    form, which stays can_tune_ai). It takes only `prompt_text`; every other step field is
    left as it is. The same {text}/{prior} rule as edit_step (on the step's own type), the
    same shared-base propagation and the same audit row (workflow.edit_step, length and hash
    only, never the text). Gate: _step_prompt_target (ring, then can_edit_step_prompt). An
    empty prompt restores the default prompt, like the API and CLI."""
    import hashlib

    from app.kernel import prompts

    step, wf, blocked = _step_prompt_target(request, session, workflow_id, step_id)
    if blocked:
        return blocked
    try:
        prompt_warning = prompts.validate_step_prompt(step.step_type, prompt_text)
    except prompts.PromptValidationError as exc:
        return _error_page(request, 400, str(exc))
    old_prompt = step.prompt_text or ""
    if (prompt_text or "") == old_prompt:
        return RedirectResponse(f"/workflows/{workflow_id}", status_code=303)
    report: dict = {}
    try:
        saved_step = wf_service.update_step(
            session, step_id, prompt_text=prompt_text, report=report
        )
    except wf_service.WorkflowError as exc:
        return _error_page(request, 400, str(exc))
    new_prompt = saved_step.prompt_text or ""
    audit.log(
        actor=_actor_email(request) or "web",
        action="workflow.edit_step",
        target=f"workflow {workflow_id} step {step_id}",
        detail=(
            "changed: prompt_text "
            + (
                "restored to default (cleared)"
                if not new_prompt.strip()
                else f"chars={len(new_prompt)} "
                f"sha256={hashlib.sha256(new_prompt.encode()).hexdigest()[:16]}"
            )
        ),
        ip=_ip(request),
        customer_id=wf.customer_id,
    )
    return _redirect_with_notice(
        f"/workflows/{workflow_id}",
        _with_prompt_warning(_shared_base_notice(report), prompt_warning),
    )


@router.post("/workflows/{workflow_id}/steps/{step_id}/restore-prompt")
def restore_step_prompt(
    workflow_id: int,
    step_id: int,
    request: Request,
    session: Session = Depends(get_session),
):
    """Restore default prompt for ONE step (#630): clears ONLY the step's inline prompt
    (the step then uses its saved template or the default for its type). It takes no other
    form field, so unsaved edits elsewhere in the step's edit form are never saved by it.
    Gate: system_prompts.can_edit_step_prompt (admin, linguist or manager, 2026-10-08),
    org/customer ring via _wf_or_404, CSRF middleware; the same audit row as edit_step
    (workflow.edit_step). The ring runs BEFORE the role gate, so a 403 never reveals that a
    foreign workflow exists."""
    step, wf, blocked = _step_prompt_target(request, session, workflow_id, step_id)
    if blocked:
        return blocked
    had_prompt = bool((step.prompt_text or "").strip())
    report: dict = {}
    try:
        wf_service.update_step(session, step_id, prompt_text="", report=report)
    except wf_service.WorkflowError as exc:
        return _error_page(request, 400, str(exc))
    if had_prompt:
        audit.log(
            actor=_actor_email(request) or "web",
            action="workflow.edit_step",
            target=f"workflow {workflow_id} step {step_id}",
            detail="changed: prompt_text restored to default (cleared)",
            ip=_ip(request),
            customer_id=wf.customer_id,
        )
    return _redirect_with_notice(f"/workflows/{workflow_id}", _shared_base_notice(report))


@router.post("/workflows/{workflow_id}/steps/{step_id}/move")
def move_step(
    workflow_id: int,
    step_id: int,
    request: Request,
    position: str = Form(""),
    parallel_group: ParallelGroupForm = None,
    session: Session = Depends(get_session),
):
    """Move a step to a 0-based POSITION in the ordered step list — the web half of the
    reorder seam (SPEC-workflow-canvas §4.2). Serves BOTH affordances on the builder:
    the graph's drag-and-drop (fetch POST from /static/wfcanvas.js) and the per-step
    "move up / move down" buttons (a plain form POST, the no-pointer/keyboard path).

    RBAC is the step-edit routes' predicate exactly (_ai_block: admins + linguists, plus
    the workflow's customer ring via _wf_or_404); CSRF-protected by the middleware.
    Success is a 303 back to the builder — the server re-renders the truth and drag state
    is transient, so a reload is the honest answer. A refusal is the standard _error_page
    400 carrying the validator's message VERBATIM (the canvas never paraphrases; the
    message names the step and the rule).

    LANE MEMBERSHIP RIDES THE SAME CALL. `parallel_group` is passed straight through to
    the seam, which applies the move and the slug change TOGETHER and validates the
    composed final state once. This route therefore performs exactly ONE seam call and
    owns no compensating writes: atomicity is a property of the seam, not something the
    route reconstructs. (It used to do move-then-slug with a snapshot/rollback, which
    made dragging a step OUT of a group impossible — the move was judged while the step
    still carried its slug, so it always tripped the consecutiveness rule — and the
    rollback itself could revert a concurrent request's committed move.)

    `parallel_group` defaults to None = "not supplied" (a plain reorder, slug untouched).
    An EMPTY STRING is a real edit meaning "leave the group", matching the edit form's
    contract — that is what the drag sends when a grouped step is dropped on a plain slot.

    The redirect carries the same shared-base propagation notice `edit_step` renders (a
    slug change sweeps to variants, so it must be as visible here as on the form) plus
    the seam's TM-first sentence when the move changed which step sits at index 0.

    `position` is `Form("")`, NOT `Form(...)`, and that is deliberate — it matches every
    other route in this family (see edit_step). A REQUIRED form field is validated by
    FastAPI BEFORE the handler runs, so a request with no body 422s ahead of the RBAC and
    customer-ring checks: a caller bound to customer A probing this route against
    customer B's workflow would get a 422 instead of the 404 that makes a foreign
    resource indistinguishable from a missing one (the sweep in
    tests/verify_connector_isolation_gaps.py [K] pins exactly this for the whole
    workflow-family). With a default, the ring runs first and a blank/garbage position is
    refused by the seam with its own clean 400 ("position '' must be a whole number")."""
    _, blocked = _ai_block(request)
    if blocked:
        return blocked
    _wf, blocked = _wf_or_404(request, session, workflow_id, write=True)
    if blocked:
        return blocked
    report: dict = {}
    kw = {}
    if parallel_group is not None:
        kw["parallel_group"] = (parallel_group or "").strip() or None
    try:
        wf_service.reorder_step(session, workflow_id, step_id, position, report=report, **kw)
    except wf_service.WorkflowError as exc:
        return _error_page(request, 400, str(exc))
    notice = " ".join(
        p for p in (_shared_base_notice(report), report.get("tm_first_note", "")) if p
    )
    target = _notice_url(f"/workflows/{workflow_id}", notice)
    # CONTENT NEGOTIATION, so the notice survives the DRAG path. A fetch cannot read a
    # 303's Location (the canvas uses redirect:'manual', which makes the response opaque
    # on purpose so a success renders once instead of twice), so the drag could only
    # reload the CURRENT url -- and the ?notice= banner was silently dropped on exactly
    # the primary input. A base band drop propagating to variants was therefore invisible,
    # which was the whole point of surfacing it.
    #
    # The fetch is identified by the X-CSRF-Token header, the SAME signal
    # app/main.py::_security_audit already uses to tell a fetch/XHR apart from a
    # full-page form navigation (it returns a plain 403 to the former and a login
    # redirect to the latter). A browser form post never sends it, so non-fetch callers
    # -- the move up / move down buttons, curl, anything scripted -- keep the 303
    # unchanged; only the canvas gets JSON, and it navigates to the very same URL, so the
    # banner renders identically on both paths.
    if request.headers.get("x-csrf-token"):
        # Security review 2026-09: the script navigates to this URL itself (no redirect for
        # the page guard to see), so its ?notice= is recorded for this session here.
        return JSONResponse({"ok": True, "location": page_guard.attest_url(request, target)})
    return RedirectResponse(target, status_code=303)


@router.post("/workflows/{workflow_id}/steps/{step_id}/override")
def override_step(
    workflow_id: int,
    step_id: int,
    request: Request,
    pinned: str = Form(""),
    session: Session = Depends(get_session),
):
    """Pin (pinned truthy) or unpin (blank) a VARIANT step — the deliberate act behind the
    builder's per-step pin/unpin control (SPEC-workflows-redesign 4b). Pinning freezes the
    step against base-edit propagation; unpinning REJOINS from the next base edit onward
    (no retro-sync). Same RBAC as an edit (_ai_block: admins + linguists) + the workflow's
    customer ring (_wf_or_404); CSRF-protected by the middleware. set_step_override does
    not block on in-flight runs (it flips a flag, never the run-time config)."""
    from app.models import WorkflowStep

    _, blocked = _ai_block(request)
    if blocked:
        return blocked
    _wf, blocked = _wf_or_404(request, session, workflow_id, write=True)
    if blocked:
        return blocked
    step = session.get(WorkflowStep, step_id)
    if step is None or step.workflow_id != workflow_id:
        return _error_page(request, 404, "Not found.")
    want = bool((pinned or "").strip())
    wf_service.set_step_override(session, step_id, want)
    label = step.name or step.step_type
    notice = (
        f"Pinned '{label}' to this variant — base edits will skip it until you unpin."
        if want
        else f"Unpinned '{label}' — it rejoins base-edit propagation from the next base "
        "edit onward (no retro-sync)."
    )
    return _redirect_with_notice(f"/workflows/{workflow_id}", notice)


@router.post("/workflows/{workflow_id}/author.json")
def author_step_prompt_json(
    workflow_id: int,
    request: Request,
    description: str = Form(...),
    provider: str = Form(""),
    model: str = Form(""),
    session: Session = Depends(get_session),
):
    """No-refresh author endpoint: the Assist form POSTs here via fetch() and JS
    drops the returned draft into the Prompt textarea IN PLACE, so the rest of the
    Add-a-step form (name, provider, glossary, ...) is never wiped by a page reload.
    RBAC is identical to the full-page route (_ai_block + _wf_or_404); errors are the
    SAME clean messages, returned as JSON for the inline error line."""
    _, blocked = _ai_block(request)
    if blocked:
        return JSONResponse(
            {"ok": False, "error": "AI setup is limited to admins and linguists."}, status_code=403
        )
    _wf, blocked = _wf_or_404(request, session, workflow_id)
    if blocked:
        return JSONResponse({"ok": False, "error": "Workflow not found."}, status_code=404)
    draft, err = _author_draft(description, provider, org_id=_my_org_id(request), model=model)
    if err:
        return JSONResponse({"ok": False, "error": err[1]}, status_code=err[0])
    return JSONResponse({"ok": True, "draft": draft})


@router.post("/workflows/{workflow_id}/author")
def author_step_prompt(
    workflow_id: int,
    request: Request,
    description: str = Form(...),
    provider: str = Form(""),
    model: str = Form(""),
    session: Session = Depends(get_session),
):
    """No-JS fallback: a plain form POST that renders the full builder page with the
    draft pre-filled in the Prompt box. Kept working so the assistant still functions
    with JavaScript disabled; the JS path uses author.json above and does not reload."""
    _, blocked = _ai_block(request)
    if blocked:
        return blocked
    wf, blocked = _wf_or_404(request, session, workflow_id)
    if blocked:
        return blocked
    draft, err = _author_draft(description, provider, org_id=_my_org_id(request), model=model)
    if err:
        return _error_page(request, err[0], err[1])
    steps = orchestrator.get_steps(session, workflow_id)
    # Full context, mirroring the GET builder route above: the template
    # unconditionally iterates `locales` (copy-to-pairs picker) and reads
    # `label_for`/`group`/`glossary_options`, and Jinja's strict Undefined
    # turns a missing name into a 500 — which silently broke this no-JS
    # fallback's whole purpose.
    from app.kernel import glossary_module as gm
    from app.kernel import languages_catalog, vendors

    grp = session.get(WorkflowGroup, wf.group_id) if wf.group_id else None
    glossary_options = [
        {"id": g.id, "label": gm.option_label(session, g)}
        for g in gm.options_for_step(
            session,
            "",
            "",
            org_id=_my_org_id(request),
            customer_id=_wf_glossary_customer(request, session, wf),
        )
    ]
    # Cross-org-aware external-step vendor picker (see the GET builder route above).
    vendor_options = vendors.list_vendors_for_picker(
        session, org_id=_my_org_id(request), customer_id=getattr(wf, "customer_id", None)
    )
    return templates.TemplateResponse(
        request,
        "workflow_builder.html",
        {
            "request": request,
            "wf": wf,
            "steps": steps,
            "providers": config.PROVIDERS,
            "step_types": [
                StepType.LLM_AGENT,
                StepType.LQA,
                StepType.HUMAN_REVIEW,
                StepType.TM_MATCH,
                StepType.EXTERNAL,
            ],
            "presets": orchestrator.ROLE_PRESETS,
            # #630: the prompt a blank Inline prompt falls back to (the locked field's placeholder).
            "default_prompts": {
                getattr(k, "value", k): v for k, v in orchestrator.DEFAULT_PROMPTS.items()
            },
            "draft": draft,
            "group": grp,
            "locales": languages_catalog.enabled_locales(),
            "label_for": languages_catalog.label_for,
            "glossary_options": glossary_options,
            "vendor_options": vendor_options,
            # SPEC-prompt-template-runtime: the step prompt-template picker options (org +
            # caller customer-ring scoped). Empty = the picker offers only "none".
            "prompt_templates": _visible_prompt_templates(request, session),
            # SPEC-workflows-redesign 4b: one-shot banner after a base edit / pin-unpin
            # (None on the author fallback POST — no such query param there).
            "notice": request.query_params.get("notice"),
            **_wf_copy_targets_ctx(request, session, wf),
            **_shape_ctx(session, wf, steps),
            **_wsp_ctx(request, session, wf.id),
        },
    )


@router.post("/workflows/author")
def workflow_author(
    request: Request,
    description: str = Form(...),
    provider: str = Form(""),
    model: str = Form(""),
    session: Session = Depends(get_session),
):
    """Promptable workflow setup (SPEC-promptable-setup B2): the "Describe a workflow"
    section POSTs here; the LLM-proposed, server-VALIDATED draft is rendered as an
    EDITABLE review form. STATELESS -- nothing is persisted. Gated by _ai_block (admins
    + linguists), same predicate as add-step. Provider/parse failures surface as a
    clean error page (400 bad ref / 502 provider-or-parse), never a 500."""
    _me, blocked = _ai_block(request)
    if blocked:
        return blocked
    from app.kernel import setup_author

    try:
        draft = setup_author.author_workflow_setup(
            session,
            description=description,
            provider=(provider or None),
            org_id=_my_org_id(request),
            actor=_actor_email(request),
            bound_customer_id=_my_customer_id(request),
            model=(model or None),
        )
    except setup_author.SetupAuthorError as exc:
        return _error_page(request, exc.status, str(exc))
    return _render_workflow_review(
        request,
        session,
        draft["fields"],
        draft["warnings"],
        description,
        provider=(provider or ""),
        model=(model or ""),
    )


@router.post("/workflows/author/confirm")
async def workflow_author_confirm(request: Request, session: Session = Depends(get_session)):
    """Consume the review form (payload is UNTRUSTED -- FULL re-validation via
    apply_workflow_draft), create the workflow (+ steps) ATOMICALLY, and redirect to the
    new builder. A validation failure re-renders the review page with the errors and
    creates NOTHING. Gated by _ai_block + the standard archived-customer ring block
    (parity with the manual create routes)."""
    _me, blocked = _ai_block(request)
    if blocked:
        return blocked
    form = await request.form()
    payload, description = _parse_review_form(form)
    # The drafting engine rides as hidden inputs so the "Fix this for me" button (which
    # POSTs this same form to /repair) can call the same engine; confirm just relays them
    # back on an error re-render so the repair button stays wired.
    provider = form.get("provider") or ""
    model = form.get("model") or ""
    # Standard ring block: an archived customer cannot be attached (parity with
    # create_workflow); apply_workflow_draft's selectable check + bound confinement
    # cover the rest of the isolation surface.
    if str(payload.get("customer_id") or "").strip().isdigit():
        # #488 item 2: the cross-tenant check comes first, so a foreign customer's name
        # can never surface in the archived-customer error.
        blocked = _wf_create_customer_block(request, session, payload["customer_id"])
        if blocked:
            return blocked
        blocked = _archived_customer_block(
            request, session, payload["customer_id"], "create a workflow for it"
        )
        if blocked:
            return blocked
    from app.kernel import setup_author

    # SPEC-lqa-step-honest-options: strip-and-warn fields (a drafted lqa/human_review
    # step that ignored a now-dead field) collect here rather than blocking the apply
    # -- surfaced as a one-shot notice on the redirect below.
    strip_warnings: list = []
    try:
        wf = await offload_parse(
            setup_author.apply_workflow_draft,
            session,
            payload,
            org_id=_my_org_id(request),
            actor=_actor_email(request),
            bound_customer_id=_my_customer_id(request),
            warnings=strip_warnings,
        )
    except setup_author.WorkflowApplyError as exc:
        # Nothing created; re-render the review page with the errors so the human can
        # fix the payload and resubmit (or click "Fix this for me").
        return _render_workflow_review(
            request,
            session,
            payload,
            warnings=[],
            description=description,
            errors=exc.errors,
            provider=provider,
            model=model,
        )
    except setup_author.SetupAuthorError as exc:
        return _error_page(request, exc.status, str(exc))
    audit.log(
        actor=_actor_email(request),
        action="workflow.author_create",
        target=f"workflow {wf.id} ({wf.name})",
        ip=_ip(request),
        customer_id=wf.customer_id,
    )
    return _redirect_with_notice(f"/workflows/{wf.id}", " ".join(strip_warnings))


@router.post("/workflows/author/repair")
async def workflow_author_repair(request: Request, session: Session = Depends(get_session)):
    """ "Fix this for me" (draft repair): the review form (the CURRENT, possibly hand-
    edited state) POSTs here when a confirm failed validation. The server re-computes the
    authoritative confirm-path errors on the current state, calls the SAME drafting engine
    with a tight repair prompt, re-validates the corrected draft with the SAME confirm
    validation (nothing saved), and re-renders the review page with the corrected values,
    a per-step 'repaired' badge on the changed steps, and any REMAINING errors (the button
    stays -- one repair PASS per click, no auto-loop; a pass is up to 2 provider calls with
    one JSON-parse retry). STATELESS. Same RBAC + archived-ring gate as author/confirm.
    Provider/parse/spend/truncation failures NEVER dead-end: the page re-renders with the
    current form state preserved + a clean message."""
    _me, blocked = _ai_block(request)
    if blocked:
        return blocked
    form = await request.form()
    payload, description = _parse_review_form(form)
    provider = form.get("provider") or ""
    model = form.get("model") or ""
    if str(payload.get("customer_id") or "").strip().isdigit():
        # #488 item 2: the cross-tenant check comes first, so a foreign customer's name
        # can never surface in the archived-customer error.
        blocked = _wf_create_customer_block(request, session, payload["customer_id"])
        if blocked:
            return blocked
        blocked = _archived_customer_block(
            request, session, payload["customer_id"], "create a workflow for it"
        )
        if blocked:
            return blocked
    from app.kernel import setup_author

    # Authoritative CURRENT errors: run the SAME confirm-path validation on the current
    # state (nothing saved), so the repair prompt gets the exact error list computed
    # SERVER-side -- never trusting a client-submitted error string. A dead-field strip on
    # the current state is not an error (it is warned, not blocked), so it does not count
    # here.
    cur_strip: list = []
    current_errors: list = []
    try:
        await offload_parse(
            setup_author.apply_workflow_draft,
            session,
            payload,
            org_id=_my_org_id(request),
            actor=_actor_email(request),
            bound_customer_id=_my_customer_id(request),
            warnings=cur_strip,
            validate_only=True,
        )
    except setup_author.WorkflowApplyError as exc:
        current_errors = list(exc.errors)
    if not current_errors:
        # Racy/forged click on an already-valid draft: re-render as-is with a calm note,
        # preserving every field. Nothing to repair.
        return _render_workflow_review(
            request,
            session,
            payload,
            warnings=cur_strip,
            description=description,
            errors=[],
            provider=provider,
            model=model,
            repair_notice="This draft already passes validation -- nothing to fix. "
            "Review it and select Create workflow.",
        )
    try:
        # Up to 2 provider calls: off the event loop (#495).
        result = await offload_parse(
            setup_author.repair_workflow_draft,
            session,
            payload=payload,
            errors=current_errors,
            provider=(provider or None),
            org_id=_my_org_id(request),
            actor=_actor_email(request),
            bound_customer_id=_my_customer_id(request),
            model=(model or None),
        )
    except setup_author.SetupAuthorError as exc:
        # Provider/parse/spend/truncation failure: never a dead-end error page -- re-render
        # the review screen with the CURRENT form state preserved, the original errors
        # intact + a clean message, and keep the "Fix this for me" button.
        return _render_workflow_review(
            request,
            session,
            payload,
            warnings=[],
            description=description,
            errors=current_errors,
            provider=provider,
            model=model,
            repair_notice=f"Automatic repair could not run: {exc.message}",
        )
    return _render_workflow_review(
        request,
        session,
        result["fields"],
        warnings=result["warnings"],
        description=description,
        errors=result["errors"],
        provider=provider,
        model=model,
        repaired=True,
        repaired_workflow_fields=result["workflow_repaired_fields"],
        # F2: if the repair dropped step(s), surface it honestly as the one-shot notice.
        repair_notice=(" ".join(result["removed_notes"]) or None),
    )


# --- workflow runs ---
#
# The run is STARTED by a POST (CSRF-protected by the middleware, write-role and
# project-visibility gated) which creates the WorkflowRun and kicks off execution
# on a background thread; the caller is redirected to /runs/{id}. The SSE stream
# (GET /runs/stream) is now a READ-ONLY OBSERVER of that run's DB rows — it never
# executes a workflow, so a dropped connection or a second tab safely re-attaches
# to the SAME run instead of spawning a new execution.
@router.post("/documents/{doc_id}/run")
def start_run(
    doc_id: int,
    request: Request,
    workflow_id: int = Form(...),
    locale: str = Form(...),
    session: Session = Depends(get_session),
):
    # SPEC-key-based-projects 2.5 (A-F1): a key project's internal documents are not
    # addressable here. Its own Translate control launches runs over the NAMESPACE
    # documents through the key grid; this DOCUMENT route reaching them let a run be
    # pointed at the trash document holding soft-deleted keys' values.
    doc = _visible_doc(session, doc_id)
    if not doc:
        return _missing_block(request, session)
    blocked = _visibility_block(request, session, doc.project_id)
    if blocked:
        return blocked
    me = security.session_user(request)
    if security.auth_required() and not security.can_write(me):
        return _error_page(request, 403, "Your role does not allow this action.")
    _wf, blocked = _wf_or_404(request, session, workflow_id)
    if blocked:
        return blocked
    # T1-05: reject an empty document (no translatable segments for this locale) up
    # front with a clear message, rather than letting the run end as a bare 'failed'.
    # The orchestrator carries the same guard (durable backstop for every surface).
    if orchestrator.document_has_no_content(session, doc, locale):
        return _error_page(request, 400, "This document has no translatable segments.")
    # P5 dispatch: resolve the multilingual-group variant for this doc's pair BEFORE
    # starting the run (orchestrator signature unchanged). An ungrouped workflow
    # resolves to itself — byte-identical to the pre-P5 path.
    from app.models import Project

    proj = session.get(Project, doc.project_id)
    src_locale = proj.source_locale if proj else None
    workflow_id = wf_service.resolve_variant(
        session,
        workflow_id,
        locale,
        src_locale,
        doc_customer_id=getattr(proj, "customer_id", None),
    )
    # Archive gate (residual sweep): an archived workflow/project is blocked from a new
    # run (start_run also enforces this; catch it here for a clean 400, not a 500).
    from app.kernel import content as content_svc

    try:
        run_id = orchestrator.start_run(
            session, workflow_id, doc_id, locale, created_by=_actor_email(request)
        )
    except orchestrator.ProjectBudgetExhausted as exc:
        # CV-2: the project is at its monthly USD cap — refuse the new run with the same
        # 429 posture the MT-submission seams use (a spend ceiling, not a bad request).
        return _error_page(request, 429, str(exc))
    except (
        content_svc.ProjectArchivedError,
        wf_service.WorkflowArchivedError,
    ) as exc:
        return _error_page(request, 400, str(exc))
    return RedirectResponse(f"/runs/{run_id}?live=1", status_code=303)


@router.post("/documents/{doc_id}/run-batch")
async def start_batch_run(
    doc_id: int,
    request: Request,
    session: Session = Depends(get_session),
):
    """Fan-out trigger (SPEC-workflows-redesign Phase 5a): run one workflow across a
    document's selected target locales as a single batch. Same RBAC / org / customer ring
    as the single-run route (/documents/{id}/run) — the batch layer adds the dispatch
    entity, not a new access path. Redirects to the minimal /batches/{id} status page.

    Form: workflow_id, locales[] (multi-select; ABSENT/empty = ALL the doc's targets),
    max_parallel (clamped 1..8), fail_fast (checkbox).

    Internal key documents are not addressable here either (A-F1) — same helper, same
    404, so the fan-out route cannot be the one that forgot."""
    doc = _visible_doc(session, doc_id)
    if not doc:
        return _missing_block(request, session)
    blocked = _visibility_block(request, session, doc.project_id)
    if blocked:
        return blocked
    me = security.session_user(request)
    if security.auth_required() and not security.can_write(me):
        return _error_page(request, 403, "Your role does not allow this action.")
    form = await request.form()
    try:
        workflow_id = int(form.get("workflow_id") or 0)
    except (TypeError, ValueError):
        return _error_page(request, 400, "Choose a workflow to run.")
    # locales[] multi-select. Absent/empty -> None = ALL the doc's targets (start_batch
    # default). getlist keeps every selected option (a plain .get returns only the last).
    selected = [v for v in form.getlist("locales") if (v or "").strip()]
    locales = selected or None
    fail_fast = bool(form.get("fail_fast"))
    max_parallel = form.get("max_parallel")
    # RBAC-ring the CHOSEN workflow exactly like the builder: the workflow must be visible
    # to this caller (org + customer ring). _wf_or_404 runs the same org + customer gate,
    # so /run-batch can never dispatch a workflow the caller may not open.
    _wf, wf_blocked = _wf_or_404(request, session, workflow_id)
    if wf_blocked:
        return wf_blocked
    try:
        batch = batch_runs.start_batch(
            session,
            workflow_id=workflow_id,
            doc_id=doc_id,
            locales=locales,
            created_by=_actor_email(request),
            max_parallel=max_parallel,
            fail_fast=fail_fast,
            synchronous=False,
        )
    except batch_runs.BatchBudgetExhausted as exc:
        return _error_page(request, 429, str(exc))  # CV-2 M2: unified 429
    except batch_runs.BatchError as exc:
        return _error_page(request, 400, str(exc))
    return RedirectResponse(f"/batches/{batch.id}", status_code=303)


def _batch_or_visibility_block(request, session, batch_id):
    """Fetch a batch + gate it by the SAME project-visibility ring as the run view (org +
    customer + project scoping). Returns (batch, doc, blocked): a foreign/missing batch is a
    404 (indistinguishable from missing — never enumerable). Shared by the GET page, the
    status poll, and the re-run / retry-failed POSTs so the access ring can't drift."""
    batch = session.get(WorkflowBatch, batch_id)
    if not batch:
        # #570: a ring-scoped caller gets exactly what a hidden batch gets; only a caller
        # who sees everything (who can tell missing from hidden anyway) keeps the typed
        # batch_missing text that the status poll and the CLI contract rely on.
        blocked = _visibility_block(request, session, None)
        return (
            None,
            None,
            blocked or _error_page(request, 404, str(batch_runs.BatchMissing(batch_id))),
        )
    doc = session.get(Document, batch.document_id)
    if not doc:
        return None, None, _missing_block(request, session)
    blocked = _visibility_block(request, session, doc.project_id)
    if blocked:
        return None, None, blocked
    return batch, doc, None


@router.get("/batches/{batch_id}")
def batch_detail(batch_id: int, request: Request, session: Session = Depends(get_session)):
    """The language-matrix run monitor (SPEC Phase 5b): rows = the batch's requested
    locales, columns = the pipeline steps, per-cell live status/score/gate/visit-count, with
    an aggregate header + per-locale re-run / retry-failed actions. Server-renders the FULL
    matrix (a correct static snapshot with no JS), then a page-scoped poll live-updates the
    cells while the batch runs. Same project-visibility gate as the run view."""
    batch, doc, blocked = _batch_or_visibility_block(request, session, batch_id)
    if blocked:
        return blocked
    wf = session.get(Workflow, batch.workflow_id)
    view = batch_runs.batch_as_dict(session, batch, detail=True)
    matrix = batch_runs.build_matrix(view)
    return templates.TemplateResponse(
        request,
        "batch_view.html",
        {
            "request": request,
            "doc": doc,
            "wf": wf,
            "batch": batch,
            "view": view,
            "matrix": matrix,
            "rerun_unavailable": batch_runs.rerun_unavailable_reason(session, batch),
            "RunStatus": RunStatus,
            "notice": request.query_params.get("notice"),
            "live": view["status"] == "running",
        },
    )


@router.get("/batches/{batch_id}/status.json")
def batch_status_json(
    batch_id: int, request: Request, detail: str = "", session: Session = Depends(get_session)
):
    """Session-authenticated live poll for the matrix monitor (the web analogue of the
    token-scoped /api/batches/{id}; a browser session can't send an lsu_ key). Same
    project-visibility ring as the page. `?detail=steps` returns the per-member step_runs the
    matrix needs; the light shape stays light. Read-only — creates no state."""
    batch, _doc, blocked = _batch_or_visibility_block(request, session, batch_id)
    if blocked:
        return blocked
    view = batch_runs.batch_as_dict(session, batch, detail=(detail == "steps"))
    payload = {"ok": True, **view}
    if not _can_see_run_cost(request):
        # #574: per-step AI spend and tokens are manager-and-above, as on the run page.
        from app.web.api_cli_common import _scrub_spend

        payload = _scrub_spend({}, payload)
    return JSONResponse(payload)


@router.post("/batches/{batch_id}/rerun")
async def batch_rerun(batch_id: int, request: Request, session: Session = Depends(get_session)):
    """Re-run ONE locale of a batch as a fresh member run INTO the same batch (Phase 5b).
    Same RBAC ring as run-batch (project visibility + can_write). The service refuses a
    re-run of an in-flight member and re-opens a finished batch for the duration. Async
    (daemon dispatch) so the POST redirects straight back to the live matrix."""
    batch, _doc, blocked = _batch_or_visibility_block(request, session, batch_id)
    if blocked:
        return blocked
    me = security.session_user(request)
    if security.auth_required() and not security.can_write(me):
        return _error_page(request, 403, "Your role does not allow this action.")
    form = await request.form()
    locale = (form.get("locale") or "").strip()
    if not locale:
        return _error_page(request, 400, "Choose a locale to re-run.")
    try:
        batch_runs.rerun_batch(
            session,
            batch=batch,
            locales=[locale],
            created_by=_actor_email(request),
            synchronous=False,
        )
    except batch_runs.BatchBudgetExhausted as exc:
        return _error_page(request, 429, str(exc))  # CV-2 M2/N2
    except batch_runs.BatchError as exc:
        return _error_page(request, 400, str(exc))
    return RedirectResponse(f"/batches/{batch_id}", status_code=303)


@router.post("/batches/{batch_id}/retry-failed")
async def batch_retry_failed(
    batch_id: int, request: Request, session: Session = Depends(get_session)
):
    """Re-run EVERY failed locale of a batch as one batch-level action (Phase 5b) — the same
    re-run path for the whole FAILED set, bounded by the batch's max_parallel via the shared
    dispatcher (not N separate client calls). Same RBAC ring as run-batch."""
    batch, _doc, blocked = _batch_or_visibility_block(request, session, batch_id)
    if blocked:
        return blocked
    me = security.session_user(request)
    if security.auth_required() and not security.can_write(me):
        return _error_page(request, 403, "Your role does not allow this action.")
    reason = batch_runs.rerun_unavailable_reason(session, batch)
    if reason:
        return _error_page(request, 400, reason)
    locales = batch_runs.failed_locales(session, batch)
    if not locales:
        return _redirect_with_notice(f"/batches/{batch_id}", "No failed locales to retry.")
    try:
        batch_runs.rerun_batch(
            session,
            batch=batch,
            locales=locales,
            created_by=_actor_email(request),
            synchronous=False,
        )
    except batch_runs.BatchBudgetExhausted as exc:
        return _error_page(request, 429, str(exc))  # CV-2 M2/N2
    except batch_runs.BatchError as exc:
        return _error_page(request, 400, str(exc))
    return RedirectResponse(f"/batches/{batch_id}", status_code=303)


@router.get("/runs/stream")
def runs_stream(run_id: int, request: Request, session: Session = Depends(get_session)):
    # READ-ONLY: polls the run's StepRun/WorkflowRun rows and re-emits the same
    # SSE event shapes the template consumes. It does NOT start or mutate a run,
    # so reconnects/second tabs attach to the same run and a finished run just
    # replays its terminal state and closes. Project-visibility is still enforced
    # (Phase 3 access scoping); no write-role gate is needed because nothing runs.
    run = session.get(WorkflowRun, run_id)
    if not run:
        return _missing_block(request, session)
    doc = session.get(Document, run.document_id)
    if not doc:
        return _missing_block(request, session)
    blocked = _visibility_block(request, session, doc.project_id)
    if blocked:
        return blocked

    def gen():
        for _run, ev in orchestrator.observe_run(run_id):
            yield f"data: {json.dumps(ev)}\n\n"

    return StreamingResponse(gen(), media_type="text/event-stream")


@router.get("/runs/{run_id}/status.json")
def run_status_json(run_id: int, request: Request, session: Session = Depends(get_session)):
    """Short session-authenticated status read for a queued page, in its visibility ring."""
    run = session.get(WorkflowRun, run_id)
    if run is None:
        return _missing_or(request, session, JSONResponse({"ok": False}, status_code=404))
    doc = session.get(Document, run.document_id)
    if doc is None:
        return _missing_or(request, session, JSONResponse({"ok": False}, status_code=404))
    blocked = _visibility_block(request, session, doc.project_id)
    if blocked:
        return blocked
    return JSONResponse({"ok": True, "run_id": run.id, "status": run.status})


def _can_see_run_cost(request) -> bool:
    """#540: AI dollar figures on a run (run total, per-step cost, the Gantt and graph
    overlay cost columns) are manager-and-above data. Auth off: the system admin."""
    return (not security.auth_required()) or security.can_manage(security.session_user(request))


def _without_step_costs(step_runs):
    """Detached copies of the StepRuns with spend and token columns blanked, so a caller
    below manager gets a context that carries no dollar or token figure at all (the
    originals stay attached and untouched)."""
    blank = {"cost_usd": None, "tokens_in": None, "tokens_out": None}
    return [sr.model_copy(update=blank) for sr in step_runs]


@router.get("/runs/{run_id}/graph-fragment")
def run_graph_fragment(run_id: int, request: Request, session: Session = Depends(get_session)):
    # Server-rendered FRAGMENT of the graph+Gantt card the live SSE handler swaps in
    # (Run View v2, piece 2). Identical RBAC/visibility + 404 posture to the run page
    # (below): missing run/doc -> the same redirect, a blocked project -> the same
    # visibility block. Read-only; no run is started or mutated.
    run = session.get(WorkflowRun, run_id)
    if not run:
        return _missing_block(request, session)
    doc = session.get(Document, run.document_id)
    if not doc:
        return _missing_block(request, session)
    blocked = _visibility_block(request, session, doc.project_id)
    if blocked:
        return blocked
    steps = orchestrator.get_steps(session, run.workflow_id)
    step_runs = session.exec(
        select(StepRun).where(StepRun.workflow_run_id == run_id).order_by(StepRun.id)
    ).all()
    if not _can_see_run_cost(request):
        step_runs = _without_step_costs(step_runs)
    ctx = _run_graph_ctx(session, run, steps, step_runs)
    ctx["request"] = request
    return templates.TemplateResponse(request, "_run_graph_card.html", ctx)


@router.get("/runs/{run_id}")
def run_detail(
    run_id: int, request: Request, live: str = "", session: Session = Depends(get_session)
):
    run = session.get(WorkflowRun, run_id)
    if not run:
        return _missing_block(request, session)
    doc = session.get(Document, run.document_id)
    if not doc:
        return _missing_block(request, session)
    blocked = _visibility_block(request, session, doc.project_id)
    if blocked:
        return blocked
    wf = session.get(Workflow, run.workflow_id)
    step_runs = session.exec(
        select(StepRun).where(StepRun.workflow_run_id == run_id).order_by(StepRun.id)
    ).all()
    # The run's own launch locale is authoritative (persisted on the row). Legacy
    # rows predating that column (target_locale == "") fall back to inferring the
    # first target locale present on the document.
    if run.target_locale:
        locales = [run.target_locale]
    else:
        locales = sorted(
            {
                s.target_locale
                for s in session.exec(
                    select(Segment).where(Segment.document_id == run.document_id)
                ).all()
            }
        )
    # Open the read-only live stream while the run is still in flight (or when the
    # launcher just redirected here with ?live=1). A terminal/paused run renders
    # its recorded steps statically — no stream, no execution.
    # Waiting rows are static; no worker tails a run before admission.
    is_live = run.status != RunStatus.QUEUED and (
        run.status in (RunStatus.PENDING, RunStatus.RUNNING) or live == "1"
    )
    can_see_cost = _can_see_run_cost(request)
    if not can_see_cost:
        # #540: no dollar or token figure reaches the template context of a caller below
        # manager (run total, per-step cost, Gantt and graph overlay all read these rows).
        step_runs = _without_step_costs(step_runs)
    steps = orchestrator.get_steps(session, run.workflow_id)
    # Run-view graph overlay (SPEC-conditional-workflows Phase 4 deferred item) + the
    # per-step-visit Gantt (Run View v2 #1), built via the shared helper so the fragment
    # endpoint renders the SAME card. Pure/read-only; degrades to no overlay/Gantt when the
    # workflow was deleted (no steps) or has no StepRun rows yet.
    graph_ctx = _run_graph_ctx(session, run, steps, step_runs)
    overlay = graph_ctx["overlay"]
    # Steps list superseded status (Run View v2 #3): single-source the graph overlay's
    # terminal-run paused-review supersede so the flat Steps list can never disagree with
    # the graph (a completed run's review row shows 'reviewed', not a stale 'paused'). The
    # raw StepRun row + the CLI/api per-visit audit trail stay RAW — this is display-only.
    step_rows = []
    step_type_by_id = {s.id: s.step_type for s in steps}
    step_by_id = {s.id: s for s in steps}
    for sr in step_runs:
        ds_value, ds_label = run_overlay.display_status(sr.status, run.status)
        # a11y T5 (WCAG 3.1.2): only a step whose output is the run's TARGET text gets the
        # run locale as its lang; an LQA step's output is not tagged (wrong lang is worse
        # than none), nor is a step whose workflow step was deleted (type unknown).
        step_type = step_type_by_id.get(sr.step_id, "")
        step_rows.append(
            {
                "sr": sr,
                "ds_value": ds_value,
                "ds_label": ds_label,
                "target_text": step_type in _TARGET_TEXT_STEP_TYPES,
                "step_label": (step_by_id[sr.step_id].name or step_type)
                if sr.step_id in step_by_id
                else "step",
            }
        )
    # Run-total spend (feat/workflow-spend): None-safe sums across the recorded StepRuns
    # so run_view can show a per-run cost/token total. None when no step recorded spend
    # (a legacy/untracked run) -> the template hides the total line entirely.
    _costs = [sr.cost_usd for sr in step_runs if sr.cost_usd is not None]
    _tin = [sr.tokens_in for sr in step_runs if sr.tokens_in is not None]
    _tout = [sr.tokens_out for sr in step_runs if sr.tokens_out is not None]
    run_cost_usd = round(sum(_costs), 6) if _costs else None
    run_tokens_in = sum(_tin) if _tin else None
    run_tokens_out = sum(_tout) if _tout else None
    # HIGH-1 + round-3: the "Human review — Approve & resume" card must NOT render for a run
    # paused at an EXTERNAL vendor step (Wave A parks the leg reusing the human_review pause
    # machinery, so run.status=='paused' alone can't tell them apart). Gate the card on the
    # current pause NOT being a vendor pause -- either an OPEN leg ("dispatched") or a WEDGED
    # RETURNED-but-unresumed leg ("wedged", awaiting `vendor redrive`); a real human_review
    # pause is unaffected and still gets the card.
    _vleg, external_pause_state = vendor_dispatch.vendor_pause_state(session, run_id)
    # external_paused drives the banner-vs-human-card choice. BLOCKER/DEAD_LETTER: a
    # DEAD_LETTER (or ESCALATED) leg has escalated to a human and IS resumable, so it must
    # NOT count as an external pause here (the human card must show) -- gate on
    # blocks_human_resume, not `leg is not None`.
    external_paused = _vleg is not None and vendor_dispatch.blocks_human_resume(
        external_pause_state
    )
    # Wave C (interop R5/R6, decision 5): the vendor-leg panel + the Human half of the
    # spend line. HIGH-C: the vendor surface is ADMIN-ONLY everywhere else (vendors panel,
    # /api dispatches, MCP exempt) because it exposes vendor identity + MSA-derived cost. A
    # non-admin with mere project visibility must NOT receive vendor names / costs / bands,
    # so the data is PRE-FILTERED out of the context entirely (not merely hidden in the
    # template). Non-admins still see the plain external-pause banner (status only).
    me = security.session_user(request)
    _can_see_vendor = (not security.auth_required()) or security.is_admin(me)
    # feat/run-doc-xliff-surfaces: when the CURRENT external pause is an OPEN WIRE-LESS
    # (file-exchange, e.g. `manual`) leg -- DISPATCHED or QUOTED, keyed off the leg's
    # protocol SNAPSHOT -- the operator's return really DOES arrive by hand, so this page
    # (where the maintainer looked first) offers the download-XLIFF + upload-return hand-off inline.
    # Admin-only: the leg's download (GET) + return (POST) routes are _admin_block-gated, so
    # the controls must only RENDER for a caller who would pass that gate (defense-in-depth;
    # the routes stay independently gated). ESCALATED / wedged / retry_wait are unaffected --
    # they keep their existing non-actionable cards. The ids drive the template's links to the
    # EXISTING /vendors/{vid}/dispatches/{did}/xliff + /return routes (no new render route).
    # `state` (round-2 finding #2) lets the template split DISPATCHED (offer the upload form)
    # from QUOTED (quote-gated — a return is REFUSED until approval, so it gets an honest
    # quote-gate card + download link instead of a doomed upload box).
    wireless_handoff = None
    if (
        _can_see_vendor
        and _vleg is not None
        and external_pause_state in ("dispatched", "quoted")
        and vendor_dispatch.leg_is_wireless(_vleg)
    ):
        wireless_handoff = {
            "vendor_id": _vleg.vendor_id,
            "dispatch_id": _vleg.id,
            "state": external_pause_state,
        }
    # Run-cancel affordance (run-cancel train): a "Cancel run" button shows only for an
    # in-flight run (pending/running/paused) to a caller who can_write — same gate as the
    # resume route below. A terminal run (completed/failed/cancelled) shows nothing.
    _can_write = (not security.auth_required()) or security.can_write(me)
    # QUEUED is cancellable from the page too (gate A round 3, HIGH-3): a fan-out's queued
    # row is exactly the work an operator most wants to stop, and the ordinary cancel path
    # handles it (the admission scheduler re-reads the status and never executes it).
    can_cancel = _can_write and run.status in orchestrator._CANCELLABLE_RUN_STATUSES
    from app.kernel import scheduler as _sched
    from app.kernel import vendor_spend as _vspend

    spend = _vspend.spend_summary(session, [run_id])
    vendor_legs = []
    if _can_see_vendor:
        from app.kernel import vendor_cost as _vcost
        from app.kernel import vendor_return as _vret
        from app.models import VendorDispatch as _VD

        _legs = session.exec(
            select(_VD).where(_VD.workflow_run_id == run_id).order_by(_VD.id.desc())
        ).all()
        for _leg in _legs:
            _d = _vret.leg_to_dict(session, _leg)
            _d["band_rows"] = _vcost.estimate_band_rows(_leg.estimate)
            vendor_legs.append(_d)
    else:
        # Strip the Human spend so only the AI total renders for a non-admin. R3 (round 2):
        # rebuild the mask from vendor_spend.ai_only (the canonical zero shape) rather than
        # hand-listing keys, so a future human_* field (e.g. human_quote_usd) is masked
        # automatically instead of leaking into a non-admin's template context.
        spend = _vspend.ai_only(spend)
    return templates.TemplateResponse(
        request,
        "run_view.html",
        {
            "request": request,
            "doc": doc,
            "wf": wf,
            "run": run,
            "step_runs": step_runs,
            "step_rows": step_rows,
            "step_output_cap": STEP_OUTPUT_INLINE_CAP,
            "steps": steps,
            "live": is_live,
            "overlay": overlay,
            "gantt": graph_ctx["gantt"],
            "can_see_cost": can_see_cost,
            "run_cost_usd": run_cost_usd,
            "run_tokens_in": run_tokens_in,
            "run_tokens_out": run_tokens_out,
            "external_paused": external_paused,
            "external_pause_state": external_pause_state,
            "vendor_legs": vendor_legs,
            "spend": spend if can_see_cost else None,
            "wireless_handoff": wireless_handoff,
            "outbound_sync_enabled": _sched.enabled_globally(),
            "can_cancel": can_cancel,
            "locale": locales[0] if locales else "",
            # a11y T5 (gate round 1, LOW 6): the locale the page tags step output with. A
            # legacy run (no target_locale) only borrows the document's locale when there
            # is exactly one; with several, `locale` above is a guess and nothing is tagged.
            "text_locale": locales[0] if len(locales) == 1 else "",
            # a11y T5: the step types whose live output the page tags with the run locale.
            "target_text_step_types": " ".join(sorted(_TARGET_TEXT_STEP_TYPES)),
            "RunStatus": RunStatus,
            # Item A: run-level warnings (e.g. a FULL_TEXT run whose tm_match step did
            # nothing) surfaced as a banner. [] -> nothing renders.
            "run_warnings": (run.context or {}).get("warnings") or [],
            # GitHub #106: WHY a FAILED/CANCELLED run ended that way, when a RECONCILER
            # (boot reconcile after a crash, staleness sweep, operator cancel) put it there.
            # Those paths write no StepRun, so the page's step timeline is empty for them and
            # the status badge was the whole story. None on every other run -> nothing
            # renders.
            "run_reason": orchestrator.run_reason(run) if run else None,
            # GitHub #101: the last in-step progress the run published, so a page opened mid
            # run (or reloaded after the SSE window expired) shows "47 of 102 segments"
            # rather than an empty timeline. None when the run published none.
            "run_progress": orchestrator.run_progress(run) if run else None,
        },
    )


@router.post("/runs/{run_id}/resume")
def resume_run(
    run_id: int,
    request: Request,
    locale: str = Form(...),
    edited_text: str = Form(""),
    session: Session = Depends(get_session),
):
    run = session.get(WorkflowRun, run_id)
    if not run:
        return _missing_or(request, session, RedirectResponse("/", status_code=303))
    doc = session.get(Document, run.document_id)
    blocked = _visibility_block(request, session, doc.project_id if doc else None)
    if blocked:
        return blocked
    me = security.session_user(request)
    if security.auth_required() and not security.can_write(me):
        return _error_page(request, 403, "Your role does not allow this action.")
    # HIGH-1 (step isolation) + round-3 wedge guard: a run paused at an EXTERNAL vendor step
    # is not a human review. Approving/resuming it by hand would advance past the external
    # step and re-commit the stale pre-dispatch seed (SOURCE) over the vendor's content (the
    # F-CRIT clobber). Refuse for BOTH an open (dispatched) leg AND a wedged (RETURNED but
    # never-resumed) one -- the latter is recovered via `vendor redrive`, never a human resume.
    _vleg, _vstate = vendor_dispatch.vendor_pause_state(session, run_id)
    if _vleg is not None and vendor_dispatch.blocks_human_resume(_vstate):
        if _vstate == "dispatched":
            _msg = (
                "This run is paused at an external vendor step; its content must return "
                "through the vendor return path -- it cannot be approved or resumed by hand."
            )
        elif _vstate == "retry_wait":
            _msg = (
                "This run's external dispatch hit a transient failure and is being retried "
                "automatically -- it cannot be approved or resumed by hand while the "
                "package is still trying to go out."
            )
        else:  # wedged
            _msg = (
                "This run's external vendor step returned but did not resume (it is wedged). "
                "Recover it with `vendor redrive` -- it cannot be approved or resumed by hand "
                "(that would overwrite the vendor's content with the pre-dispatch source)."
            )
        return _error_page(request, 409, _msg)
    # LLMemory capture seam 2, web half (PROPOSAL-llmemory §4.2): attribute the
    # resume-derived MemoryEvent(s) to the real actor -- mirrors prepare_resume_seed's
    # actor plumbing on the CLI/api resume paths (this is the surface a human reviewer
    # actually uses, so it must not be the one resume path that captures anonymously).
    # NOTE: requires resume_run_async to accept `actor` (orchestrator.py, Fixer 1).
    # Issue #493: the run's own locale wins over the form field.
    locale = run.target_locale or locale
    orchestrator.resume_run_async(session, run_id, locale, edited_text, actor=_actor_email(request))
    return RedirectResponse(f"/runs/{run_id}?live=1", status_code=303)


@router.post("/runs/{run_id}/cancel")
def cancel_run(
    run_id: int,
    request: Request,
    session: Session = Depends(get_session),
):
    """Abort an in-flight run (pending/running/paused -> CANCELLED). The operator escape
    hatch (AUDIT BUG-A205): the only way out of a run stuck PAUSED at a human_review or
    external-vendor step, and cancelling ALSO unblocks re-authoring (a CANCELLED run stops
    counting as in-flight). Gated like resume (can_write); a foreign-project run is an
    identical visibility block; an already-terminal run answers a clean 409, never a 500.
    Any open vendor leg is closed CANCELLED so no late return can strand the run."""
    run = session.get(WorkflowRun, run_id)
    if not run:
        return _missing_or(request, session, RedirectResponse("/", status_code=303))
    doc = session.get(Document, run.document_id)
    blocked = _visibility_block(request, session, doc.project_id if doc else None)
    if blocked:
        return blocked
    me = security.session_user(request)
    if security.auth_required() and not security.can_write(me):
        return _error_page(request, 403, "Your role does not allow this action.")
    try:
        orchestrator.cancel_run(session, run_id, actor=_actor_email(request))
    except orchestrator.RunCancelError as exc:
        return _error_page(request, 409, str(exc))
    return RedirectResponse(f"/runs/{run_id}", status_code=303)


_srp_router_options = router.prefix, router.tags, router.dependencies
router.prefix, router.tags, router.dependencies = "", [], []
router.include_router(workflows_common_router)
router.prefix, router.tags, router.dependencies = _srp_router_options


@router.post("/templates/prompts")
def create_prompt_template(
    request: Request,
    name: str = Form(...),
    body: str = Form(""),
    customer_id: str = Form(""),
    session: Session = Depends(get_session),
):
    _me, blocked = _ai_block(request)
    if blocked:
        return blocked
    from urllib.parse import quote

    # Stamp the creator's org (P8 pre-gate cleanup) so the template can't leak
    # cross-org. No-op (NULL) with the gate off — templates stay app-global.
    # SPEC-pt-attach: `customer_id` optionally confines the template to one customer
    # (blank = GLOBAL, byte-identical to today). bound_customer_id FORCES a customer-
    # bound caller's template to its own customer at the service (the form can't route
    # around it). A missing/foreign/archived customer is refused with a clean flash.
    try:
        wf_service.create_prompt_template(
            session,
            name,
            body=body,
            org_id=_my_org_id(request),
            customer_id=customer_id,
            bound_customer_id=_my_customer_id(request),
        )
    except wf_service.WorkflowError as exc:
        return RedirectResponse(
            "/workflows/building-blocks?err=" + quote(str(exc)), status_code=303
        )
    return RedirectResponse("/workflows/building-blocks", status_code=303)


@router.post("/templates/prompts/{template_id}/rename")
def rename_prompt_template(
    template_id: int,
    request: Request,
    name: str = Form(""),
    session: Session = Depends(get_session),
):
    """Rename a prompt template. Family gate (_ai_block), matching the create route.
    Logs old->new name."""
    _, blocked = _ai_block(request)
    if blocked:
        return blocked
    tmpl, blocked = _prompt_template_or_404(request, session, template_id, write=True)
    if blocked:
        return blocked
    old_name = tmpl.name
    try:
        wf_service.rename_prompt_template(session, template_id, name)
    except wf_service.WorkflowError as exc:
        return _error_page(request, 400, str(exc))
    audit.log(
        actor=_actor_email(request),
        action="prompt_template.rename",
        target=f"prompt_template {template_id}",
        detail=f"{old_name!r} -> {tmpl.name!r}",
        ip=_ip(request),
    )
    return RedirectResponse("/workflows/building-blocks", status_code=303)


_srp_router_options = router.prefix, router.tags, router.dependencies
router.prefix, router.tags, router.dependencies = "", [], []
router.include_router(workflows_part4_router)
router.prefix, router.tags, router.dependencies = _srp_router_options


@router.post("/workflows/{workflow_id}/clone")
def clone_workflow(workflow_id: int, request: Request, session: Session = Depends(get_session)):
    _, blocked = _ai_block(request)
    if blocked:
        return blocked
    # Org-gate the SOURCE (leak vector 12): a scoped user must not clone another
    # org's workflow. The clone inherits the (now-verified same-org) source's org.
    _wf, blocked = _wf_or_404(request, session, workflow_id)
    if blocked:
        return blocked
    # #468: a customer account's clone of a global workflow is its own customer's copy.
    new = wf_service.clone_workflow(
        session, workflow_id, bound_customer_id=_my_customer_id(request)
    )
    if new is None:
        # Unreachable in practice (the source was just 404-gated), but keep the failure
        # bounce pointed at the live Building blocks tab — the retired /templates page.
        return RedirectResponse("/workflows/building-blocks", status_code=303)
    return RedirectResponse(f"/workflows/{new.id}", status_code=303)
