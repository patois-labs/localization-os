"""/api routes: smaller areas

Documented in: docs/architecture/surfaces-and-parity.md
"""

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from sqlmodel import Session, select

from app.db import engine
from app.kernel import (
    access,
    audit,
    pair_providers,
    security,
)
from app.models import (
    Project,
)
from app.web import deps
from app.web.api_cli_common import (
    _NOT_FOUND,
    _ab_chosen_locale,
    _admin_block,
    _ai_block,
    _content_type_in_scope,
    _cust_of,
    _customer_config_block,
    _customer_in_org,
    _gate,
    _int_id,
    _json,
    _manage_block,
    _mock_spend_block_for_doc,
    _org_list,
    _project_in_org,
    _role_block,
    _visible_document,
    _write_block,
)

router = APIRouter()


# --- project templates: list / show / create / apply / rename / delete -------
# create/apply = can_write; rename = can_manage; delete = is_admin (SPEC row). get_template rings only  # noqa: E501
# by ORG, so every by-id route adds the customer ring via access.customer_ring_blocks
# (a bound caller's foreign-customer template is a 404, never enumerable).
def _template_in_scope(tpl, ident):
    """A ProjectTemplate the caller may see, or None: the org check is already applied by
    get_template(org_id=...); this adds the R2-3 customer ring (a GLOBAL template stays
    visible, a foreign customer's is 404) exactly like _pt_in_org / _grp_in_org."""
    if tpl is None:
        return None
    if access.customer_ring_blocks(_cust_of(ident), getattr(tpl, "customer_id", None)):
        return None
    return tpl


@router.get("/api/templates")
def api_templates_list(request: Request):
    """List project templates (mirrors `template list`). A read — GLOBAL templates plus,
    for a bound caller, its own customer's; a foreign customer's is never returned
    (list_templates(customer_id=...) applies the P4 isolation rule)."""
    ident, org_id, blocked = _gate(request)
    if blocked:
        return blocked
    from app.kernel import templates as tsvc

    rows = tsvc.list_templates(customer_id=_cust_of(ident), org_id=org_id)
    return JSONResponse({"ok": True, "templates": [tsvc.template_row(t) for t in rows]})


@router.get("/api/templates/{tid}")
def api_templates_show(tid: int, request: Request):
    """Show one template's captured setup (mirrors `template show`). Org bound via
    get_template(org_id=...) + the customer ring; a foreign template is 404."""
    ident, org_id, blocked = _gate(request)
    if blocked:
        return blocked
    from app.kernel import templates as tsvc

    tpl = _template_in_scope(tsvc.get_template(tid, org_id=org_id), ident)
    if tpl is None:
        return JSONResponse(_NOT_FOUND, status_code=404)
    return JSONResponse({"ok": True, "template": tsvc.template_row(tpl)})


@router.post("/api/templates")
async def api_templates_create(request: Request):
    """Snapshot a project's setup as a template (mirrors `template create --from-project`).
    can_write. Body: name, project_id (the source project), scope (global|customer),
    description. The source project_id funnels through _project_in_org (a foreign project
    is 404, resolved BEFORE any name validation — scope-before-validate)."""
    ident, org_id, blocked = _gate(request)
    if blocked:
        return blocked
    blocked = _write_block(ident)  # web /projects/{id}/save-as-template is can_write
    if blocked:
        return blocked
    body = await _json(request)
    from app.kernel import templates as tsvc

    with Session(engine) as s:
        pid = _int_id(body.get("project_id"))
        if pid is None or _project_in_org(s, pid, org_id, ident) is None:
            return JSONResponse(_NOT_FOUND, status_code=404)
        try:
            tpl = tsvc.save_as_template(
                s,
                pid,
                str(body.get("name") or ""),
                scope=str(body.get("scope") or "global"),
                description=str(body.get("description") or ""),
                actor=ident["actor"],
                org_id=org_id,
                # #488 item 5: a customer-bound key publishes only into its own customer.
                bound_customer_id=_cust_of(ident),
            )
        except tsvc.TemplateError as exc:
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
        out = {"ok": True, "template": tsvc.template_row(tpl)}
        tid, cust = tpl.id, tpl.customer_id
    audit.log(
        actor=ident["actor"],
        action="template.create",
        target=f"template {tid}",
        ip=deps._ip(request),
        customer_id=cust,
    )
    return JSONResponse(out)


@router.post("/api/templates/{tid}/apply")
async def api_templates_apply(tid: int, request: Request):
    """Create a new project from a template (mirrors `template apply`). can_write. Body:
    name (the new project's name), customer_id (optional). Org+customer bound via
    get_template + the ring; bound_customer_id confines a bound caller so the created
    project always lands in their own customer (apply_template's R2-3 write-leak fix)."""
    ident, org_id, blocked = _gate(request)
    if blocked:
        return blocked
    blocked = _write_block(ident)
    if blocked:
        return blocked
    body = await _json(request)
    from app.kernel import templates as tsvc

    with Session(engine) as s:
        tpl = _template_in_scope(tsvc.get_template(tid, org_id=org_id), ident)
        if tpl is None:
            return JSONResponse(_NOT_FOUND, status_code=404)
        # BLOCKER (adversarial review): the body customer_id is a WRITE target for the
        # new project, so it funnels through the SAME ring every other create route uses
        # BEFORE apply (mirrors api_context_set_profile). Without it, a GLOBAL template's
        # apply took the body id straight through — a bound A1 caller could plant a
        # project into A2 (plant-and-forget) and a staff caller a cross-org FK violation
        # (apply_template's bound guard only covers a CUSTOMER-scoped template's own id).
        raw_cid = body.get("customer_id")
        if raw_cid is not None and str(raw_cid).strip():
            cid = _int_id(raw_cid)
            if cid is None or _customer_in_org(s, cid, org_id, ident) is None:
                return JSONResponse(_NOT_FOUND, status_code=404)
        try:
            proj, warnings = tsvc.apply_template(
                s,
                tid,
                str(body.get("name") or ""),
                customer_id=(body.get("customer_id") or None),
                created_by=ident["actor"],
                creator=ident.get("user"),
                org_id=org_id,
                bound_customer_id=_cust_of(ident),
                require_customer=True,
                # Connected projects: "intake": "connected" creates an EMPTY connected project;
                # a template never binds or schedules a source by itself.
                intake=str(body.get("intake") or "upload"),
            )
        except pair_providers.MockEngineAdminOnlyError as exc:
            # #577: a template that pins the offline Mock engine needs an administrator.
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=403)
        except tsvc.TemplateError as exc:
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
        pid, pname, pcust = proj.id, proj.name, proj.customer_id
        pintake = proj.intake
    audit.log(
        actor=ident["actor"],
        action="template.apply",
        target=f"project {pid} from template {tid}",
        ip=deps._ip(request),
        customer_id=pcust,
    )
    return JSONResponse(
        {"ok": True, "project_id": pid, "name": pname, "intake": pintake, "warnings": warnings}
    )


@router.post("/api/templates/{tid}/rename")
async def api_templates_rename(tid: int, request: Request):
    """Rename a template (mirrors POST /templates/{id}/rename). MANAGE role: the web
    twin (projects.template_rename) requires can_manage ("Renaming templates requires a
    manager role"), so this route must too -- a translator key was renaming a template
    the same account is 403'd from renaming on the web (RBAC-parity gap).

    RING BEFORE ROLE (mirrors the sibling api_templates_delete, the permanent route-sweep
    finding): the customer/org ring resolves to a 404 BEFORE the manage gate, so a
    customer-bound caller (who can NEVER be manager) cannot distinguish an existing
    foreign template from a missing one -- the 404 stays uniform and non-enumerable.
    Only an IN-SCOPE template the caller lacks the manage role for reaches the 403. (The
    web twin is role-first; /api hardens one step further, exactly as delete does.)
    rename_template re-scopes the dup-check to the template's own org (no name oracle)."""
    ident, org_id, blocked = _gate(request)
    if blocked:
        return blocked
    body = await _json(request)
    from app.kernel import templates as tsvc

    with Session(engine) as s:
        tpl = _template_in_scope(tsvc.get_template(tid, org_id=org_id), ident)
        if tpl is None:
            return JSONResponse(_NOT_FOUND, status_code=404)
        blocked = _manage_block(ident)  # can_manage parity (after the ring; see docstring)
        if blocked:
            return blocked
        old_name = tpl.name
        try:
            tpl = tsvc.rename_template(
                s, tid, str(body.get("name") or ""), actor=None, org_id=org_id
            )
        except tsvc.TemplateError as exc:
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
        out = {"ok": True, "template": tsvc.template_row(tpl)}
    audit.log(
        actor=ident["actor"],
        action="template.rename",
        target=f"template {tid}",
        detail=f"{old_name!r} -> {out['template']['name']!r}",
        ip=deps._ip(request),
    )
    return JSONResponse(out)


@router.post("/api/templates/{tid}/delete")
async def api_templates_delete(tid: int, request: Request):
    """Delete a template (mirrors `template delete`). is_admin (SPEC row). The customer
    ring resolves BEFORE the admin gate so a bound caller's foreign template is a 404
    (never a 403 that leaks its existence); a bound caller is never admin, so past the
    ring it still hits the admin gate on its own/global rows."""
    ident, org_id, blocked = _gate(request)
    if blocked:
        return blocked
    from app.kernel import templates as tsvc

    with Session(engine):
        tpl = _template_in_scope(tsvc.get_template(tid, org_id=org_id), ident)
        if tpl is None:
            return JSONResponse(_NOT_FOUND, status_code=404)
        blocked = _role_block(ident, security.is_admin)
        if blocked:
            return blocked
        name = tpl.name
        try:
            ok = tsvc.delete_template(tid, actor=None, org_id=org_id)
        except tsvc.TemplateError as exc:
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
        if not ok:
            return JSONResponse(_NOT_FOUND, status_code=404)
    audit.log(
        actor=ident["actor"],
        action="template.delete",
        target=f"template {tid} '{name}'",
        ip=deps._ip(request),
    )
    return JSONResponse({"ok": True, "id": tid, "deleted": True})


@router.post("/api/context/ab")
async def api_context_ab(request: Request):
    """Run the context A/B measurement (mirrors `context ab`). can_tune_ai, SPEND. Body:
    document, locale?, provider?, max_segments?. The document funnels through
    _visible_document + _project_in_org (404 foreign). mock-409 before spending; run_ab
    writes nothing."""
    ident, org_id, blocked = _gate(request)
    if blocked:
        return blocked
    blocked = _ai_block(ident)
    if blocked:
        return blocked
    body = await _json(request)
    from app.kernel import context_ab

    with Session(engine) as s:
        doc_id = _int_id(body.get("document"))
        doc = _visible_document(s, doc_id)
        if doc is None or _project_in_org(s, doc.project_id, org_id, ident) is None:
            return JSONResponse(_NOT_FOUND, status_code=404)
        project = s.get(Project, doc.project_id)
        locale = str(body.get("locale") or "").strip() or None
        chosen = _ab_chosen_locale(s, doc.id, locale)
        if chosen is None:
            return JSONResponse(
                {"ok": False, "error": f"document {doc.id} has no segments to A/B."},
                status_code=400,
            )
        provider = str(body.get("provider") or "").strip() or None
        mock = _mock_spend_block_for_doc(s, project, chosen, provider)
        if mock:
            return mock
        # #746: an A/B translates the sample twice through the engine; off the event loop.
        try:
            res = await deps.offload_blocking(
                context_ab.run_ab,
                s,
                doc,
                locale=locale,
                provider=provider,
                max_segments=(
                    body.get("max_segments")
                    if body.get("max_segments") is not None
                    else context_ab.DEFAULT_MAX_SEGMENTS
                ),
                org_id=org_id,
            )
        except deps.WorkerBusy as exc:
            return deps.busy_response(str(exc))
        except context_ab.ABError as exc:
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
        data = res.as_dict()
    audit.log(
        actor=ident["actor"], action="context.ab", target=f"document {doc_id}", ip=deps._ip(request)
    )
    return JSONResponse({"ok": True, "ab": data})


# --- context packages: customer-config cards / AI document overrides / reads ---
# Standing context cards + a document override + the resolved read. Every scope id
# (customer_id / content_type_id / project_id / doc_id) funnels through its ring so a
# bound caller can never author a card at another customer's scope, and a foreign scope
# id is a 404.
@router.post("/api/context/profiles")
async def api_context_set_profile(request: Request):
    """Create or update a standing context card at a scope (mirrors `context set-profile`;
    one card per exact scope). Customer scope: can_configure_customer (admin,
    manager, linguist); org-default: admin; project or content-type scope:
    can_tune_ai (admin, linguist).
    Body: customer_id?, content_type_id?,
    project_id? (the scope), name, context (the raw context-package object). A bound
    caller is FORCED to its own customer; every scope id funnels through its ring
    (_customer_in_org / _content_type_in_scope / _project_in_org), all resolved BEFORE
    upsert so a foreign scope id is a 404, never a planted card."""
    ident, org_id, blocked = _gate(request)
    if blocked:
        return blocked
    body = await _json(request)
    from app.kernel import context_pkg

    bound = _cust_of(ident)
    with Session(engine) as s:
        # customer scope: a bound caller may only ever author at its OWN customer.
        customer_id = None
        raw_cid = body.get("customer_id")
        if bound is not None:
            if raw_cid is not None and str(raw_cid).strip() and _int_id(raw_cid) != bound:
                return JSONResponse(_NOT_FOUND, status_code=404)
            customer_id = bound
        elif raw_cid is not None and str(raw_cid).strip():
            cid = _int_id(raw_cid)
            if cid is None or _customer_in_org(s, cid, org_id, ident) is None:
                return JSONResponse(_NOT_FOUND, status_code=404)
            customer_id = cid
        # content-type scope: funnel through the SAME ring the content-type routes use.
        content_type_id = None
        if body.get("content_type_id") is not None and str(body.get("content_type_id")).strip():
            ctid = _int_id(body.get("content_type_id"))
            if ctid is None or _content_type_in_scope(s, ctid, org_id, ident) is None:
                return JSONResponse(_NOT_FOUND, status_code=404)
            content_type_id = ctid
        # project scope: funnel through _project_in_org (enforces the bound ring too).
        project_id = None
        if body.get("project_id") is not None and str(body.get("project_id")).strip():
            pid = _int_id(body.get("project_id"))
            if pid is None or _project_in_org(s, pid, org_id, ident) is None:
                return JSONResponse(_NOT_FOUND, status_code=404)
            project_id = pid
        if customer_id is not None:
            blocked = _customer_config_block(ident)
        elif project_id is None and content_type_id is None:
            blocked = _admin_block(ident)
        else:
            blocked = _ai_block(ident)
        if blocked:
            return blocked
        try:
            prof = context_pkg.upsert_profile(
                s,
                org_id=org_id,
                customer_id=customer_id,
                content_type_id=content_type_id,
                project_id=project_id,
                name=str(body.get("name") or ""),
                context_raw=body.get("context"),
                actor=ident["actor"],
            )
        except context_pkg.ContextError as exc:
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
        row = context_pkg.profile_row(prof)
    audit.log(
        actor=ident["actor"],
        action="context.set_profile",
        target=f"context_profile {row['id']}",
        ip=deps._ip(request),
        customer_id=row.get("customer_id"),
    )
    return JSONResponse({"ok": True, "profile": row})


@router.post("/api/context/documents/{doc_id}")
async def api_context_set_document(doc_id: int, request: Request):
    """Set/edit a document's context OVERRIDE (mirrors `context set-document`).
    can_tune_ai. Body: context (the raw package object, or null/empty to clear). The
    document funnels through _visible_document + _project_in_org (404 foreign)."""
    ident, org_id, blocked = _gate(request)
    if blocked:
        return blocked
    blocked = _ai_block(ident)
    if blocked:
        return blocked
    body = await _json(request)
    from app.kernel import context_pkg

    with Session(engine) as s:
        doc = _visible_document(s, doc_id)
        if doc is None or _project_in_org(s, doc.project_id, org_id, ident) is None:
            return JSONResponse(_NOT_FOUND, status_code=404)
        try:
            stored = context_pkg.set_document_context(s, doc, body.get("context"))
        except context_pkg.ContextError as exc:
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
    audit.log(
        actor=ident["actor"],
        action="context.set_document",
        target=f"document {doc_id}",
        ip=deps._ip(request),
        customer_id=audit.customer_for_project(doc.project_id),
    )
    return JSONResponse({"ok": True, "document_id": doc_id, "context": stored})


@router.get("/api/context/resolve")
def api_context_resolve(
    request: Request, project: int = 0, customer: int = 0, content_type: int = 0, document: int = 0
):
    """Resolve + render the EFFECTIVE context package for a scope (mirrors `context
    show`). A read. Query: project, customer, content_type, document (any subset). Each
    id funnels through its ring so a foreign scope id is a 404, never a resolution over
    another tenant's cards."""
    ident, org_id, blocked = _gate(request)
    if blocked:
        return blocked
    from app.kernel import context_pkg

    with Session(engine) as s:
        doc = proj = None
        if document:
            doc = _visible_document(s, document)
            if doc is None or _project_in_org(s, doc.project_id, org_id, ident) is None:
                return JSONResponse(_NOT_FOUND, status_code=404)
        if project:
            proj = _project_in_org(s, project, org_id, ident)
            if proj is None:
                return JSONResponse(_NOT_FOUND, status_code=404)
        elif doc is not None:
            proj = s.get(Project, doc.project_id)
        cust_arg = None
        if customer:
            if _customer_in_org(s, customer, org_id, ident) is None:
                return JSONResponse(_NOT_FOUND, status_code=404)
            cust_arg = customer
        elif _cust_of(ident) is not None:
            cust_arg = _cust_of(ident)
        ct_arg = None
        if content_type:
            if _content_type_in_scope(s, content_type, org_id, ident) is None:
                return JSONResponse(_NOT_FOUND, status_code=404)
            ct_arg = content_type
        effective, provenance = context_pkg.resolve(
            s,
            document=doc,
            project=proj,
            customer_id=cust_arg,
            content_type_id=ct_arg,
            org_id=org_id,
        )
        block = context_pkg.render_block(effective)
    return JSONResponse(
        {"ok": True, "context": effective or {}, "block": block, "provenance": provenance}
    )


# --- department budgets ----------------------------------------------------
@router.post("/api/budgets")
async def api_budget_set(request: Request):
    """Create/update a department's monthly token cap (mirrors `admin budget set`).
    is_admin, org-stamped. Body: department, limit (<=0 = unlimited)."""
    from app.kernel import budgets

    ident, org_id, blocked = _gate(request)
    if blocked:
        return blocked
    b = _admin_block(ident)
    if b:
        return b
    body = await _json(request)
    dept = (body.get("department") or "").strip()
    if not dept:
        return JSONResponse(
            {"ok": False, "error": "department must not be blank."}, status_code=400
        )
    try:
        limit = int(body.get("limit"))
    except (TypeError, ValueError):
        return JSONResponse({"ok": False, "error": "limit must be an integer."}, status_code=400)
    with Session(engine) as s:
        try:
            row, created = budgets.upsert_budget(
                s, dept, limit, actor=ident.get("actor", ""), org_id=org_id
            )
        except budgets.BudgetLimitError as exc:
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
        return JSONResponse(
            {
                "ok": True,
                "id": row.id,
                "department": row.department,
                "monthly_token_limit": row.monthly_token_limit,
                "created": created,
            }
        )


@router.get("/api/budgets")
def api_budget_list(request: Request):
    """List department budgets + this month's spend (mirrors `admin budget list`),
    org-scoped. is_admin."""
    from app.kernel import budgets
    from app.models import DepartmentBudget

    ident, org_id, blocked = _gate(request)
    if blocked:
        return blocked
    b = _admin_block(ident)
    if b:
        return b
    with Session(engine) as s:
        stmt = _org_list(select(DepartmentBudget), DepartmentBudget, org_id)
        rows = s.exec(stmt.order_by(DepartmentBudget.department)).all()
        payload = [
            {
                "id": r.id,
                "department": r.department,
                "monthly_token_limit": r.monthly_token_limit,
                "spent_this_month": budgets.spend_this_month(s, r.department, org_id=r.org_id),
            }
            for r in rows
        ]
    return JSONResponse({"ok": True, "budgets": payload})


@router.post("/api/budgets/{budget_id}/delete")
def api_budget_delete(request: Request, budget_id: int):
    """Delete a department budget (mirrors `admin budget delete`). is_admin,
    org-ringed (a cross-org id is 404)."""
    from app.kernel import budgets
    from app.models import DepartmentBudget

    ident, org_id, blocked = _gate(request)
    if blocked:
        return blocked
    b = _admin_block(ident)
    if b:
        return b
    bid = _int_id(budget_id)
    with Session(engine) as s:
        row = s.get(DepartmentBudget, bid) if bid is not None else None
        if row is None or (org_id is not None and getattr(row, "org_id", None) != org_id):
            return JSONResponse(_NOT_FOUND, status_code=404)
        if not budgets.delete_budget(s, bid, actor=ident.get("actor", "")):
            return JSONResponse(_NOT_FOUND, status_code=404)
    return JSONResponse({"ok": True, "id": bid, "deleted": True})
