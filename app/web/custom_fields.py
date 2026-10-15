"""Custom project fields - the web surface (SPEC-project-automation, MVP part C).

  * ``/settings/custom-fields``  the field schema editor (manager or admin): list, create, edit,
    archive and restore field definitions. A field is never deleted once it has values.
  * ``POST /projects/{id}/fields``  set a project's field values (project editors).
  * ``POST /projects/columns``  save the caller's projects-list column choice.

Every rule lives in app.services.custom_fields (the one service the CLI, /api and MCP
share); this module is a thin shell: scope the caller (org + R2-3 customer ring), gate
the role, call the service, audit, redirect. A foreign project or field answers 404
exactly like a missing one (ADR 0010).

Documented in: docs/architecture/content-and-projects.md
"""

from urllib.parse import quote

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse
from sqlmodel import Session, select

from app.db import get_session
from app.kernel import access, audit, ids, security
from app.kernel import custom_fields as cf
from app.models import Customer, Project
from app.web.deps import (
    _actor_email,
    _error_page,
    _ip,
    _missing_or,
    _my_customer_id,
    _my_org_id,
    _visibility_block,
    templates,
)

router = APIRouter()

_PAGE = "/settings/custom-fields"


def _admin_block(request):
    """Defining fields is a manager-or-admin act (10-13; spec section 6.3 said admin).
    The finer rules (customer scope only for managers, creator-or-admin to change) are
    cf.define_refusal / cf.modify_refusal."""
    me = security.session_user(request)
    if security.auth_required() and not security.can_manage(me):
        return _error_page(
            request, 403, "Defining custom fields is limited to managers and admins."
        )
    return None


def _customers(request, session):
    from app.kernel import customers as customer_service

    me = security.session_user(request)
    q = customer_service.active_only(access.org_filter(select(Customer), Customer, me)).order_by(
        Customer.name
    )
    mine = access.current_customer_id(me)
    if mine is not None:
        q = q.where(Customer.id == mine)
    return session.exec(q).all()


def _scope_customer(request, session, raw):
    """A submitted customer scope, only if the caller may use it (blank = every customer)."""
    mine = _my_customer_id(request)
    if mine is not None:
        return mine
    raw = (raw or "").strip()
    if not raw:
        return None
    cid = ids.coerce_id(raw)
    if cid is None or cid not in {c.id for c in _customers(request, session)}:
        raise cf.FieldError("that customer is not available in this scope.")
    return cid


def _flag(value) -> bool:
    return str(value or "").strip().lower() in ("1", "on", "true", "yes")


@router.get(_PAGE)
def custom_fields_index(request: Request, session: Session = Depends(get_session)):
    blocked = _admin_block(request)
    if blocked:
        return blocked
    defs = cf.list_defs(
        session, _my_org_id(request), _my_customer_id(request), include_archived=True
    )
    names = {c.id: c.name for c in _customers(request, session)}
    me = security.session_user(request)
    actor = _actor_email(request)
    rows = [
        {
            "d": cf.to_dict(d),
            "customer": names.get(d.customer_id, ""),
            "has_values": cf.has_values(session, d.id),
            "can_edit": cf.modify_refusal(me, actor, d) is None,
        }
        for d in defs
    ]
    return templates.TemplateResponse(
        request,
        "custom_fields.html",
        {
            "request": request,
            "rows": rows,
            "types": cf.TYPES,
            "cf_off": not cf.enabled(),
            "customers": _customers(request, session),
            "can_global": cf.define_refusal(me, None) is None,
        },
    )


@router.post(_PAGE)
def custom_fields_create(
    request: Request,
    key: str = Form(""),
    label: str = Form(""),
    type: str = Form("text"),
    options: str = Form(""),
    key_pattern: str = Form(""),
    link_system: str = Form(""),
    help_text: str = Form(""),
    customer_id: str = Form(""),
    required: str = Form(""),
    show_in_list: str = Form(""),
    filterable: str = Form("on"),
    list_order: str = Form("0"),
    session: Session = Depends(get_session),
):
    blocked = _admin_block(request)
    if blocked:
        return blocked
    try:
        scope = _scope_customer(request, session, customer_id)
    except cf.FieldError as exc:
        return RedirectResponse(f"{_PAGE}?err=" + quote(str(exc)), status_code=303)
    refusal = cf.define_refusal(security.session_user(request), scope)
    if refusal:
        return _error_page(request, 403, refusal)
    try:
        d = cf.create_def(
            session,
            key,
            label,
            type,
            options=options,
            key_pattern=key_pattern,
            link_system=link_system,
            help_text=help_text,
            required=_flag(required),
            show_in_list=_flag(show_in_list),
            filterable=_flag(filterable),
            list_order=int(list_order)
            if str(list_order or "0").strip().lstrip("-").isdigit()
            else 0,
            org_id=_my_org_id(request),
            customer_id=scope,
            created_by=_actor_email(request),
        )
    except cf.FieldError as exc:
        return RedirectResponse(f"{_PAGE}?err=" + quote(str(exc)), status_code=303)
    audit.log(
        actor=_actor_email(request),
        action="field.define",
        target=f"custom_field {d.id} '{d.key}'",
        detail=f"type={d.type} required={d.required}",
        ip=_ip(request),
        customer_id=d.customer_id,
    )
    return RedirectResponse(f"{_PAGE}?saved=1", status_code=303)


def _field_or_404(request, session, field_id):
    d = cf.visible(session, field_id, _my_org_id(request), _my_customer_id(request))
    if d is None:
        return None, _error_page(request, 404, "Custom field not found.")
    return d, None


@router.post(_PAGE + "/{field_id}/update")
def custom_fields_update(
    field_id: int,
    request: Request,
    label: str = Form(""),
    options: str = Form(""),
    key_pattern: str = Form(""),
    link_system: str = Form(""),
    help_text: str = Form(""),
    required: str = Form(""),
    show_in_list: str = Form(""),
    filterable: str = Form(""),
    list_order: str = Form("0"),
    session: Session = Depends(get_session),
):
    blocked = _admin_block(request)
    if blocked:
        return blocked
    d, blocked = _field_or_404(request, session, field_id)
    if blocked:
        return blocked
    refusal = cf.modify_refusal(security.session_user(request), _actor_email(request), d)
    if refusal:
        return _error_page(request, 403, refusal)
    before = cf.to_dict(d)
    changes = {
        "label": label,
        "help_text": help_text,
        "required": _flag(required),
        "show_in_list": _flag(show_in_list),
        "filterable": _flag(filterable),
        "list_order": list_order if str(list_order or "0").strip().lstrip("-").isdigit() else 0,
    }
    if d.type == "select":
        changes["options"] = options
    if d.type in ("external_link", "text"):
        changes["key_pattern"] = key_pattern
    if d.type == "external_link":
        changes["link_system"] = link_system
    try:
        changed = cf.update_def(session, d, **changes)
    except cf.FieldError as exc:
        return RedirectResponse(f"{_PAGE}?err=" + quote(str(exc)), status_code=303)
    if changed:
        audit.log(
            actor=_actor_email(request),
            action="field.update",
            target=f"custom_field {d.id} '{d.key}'",
            detail=cf.change_detail(before, cf.to_dict(d), changed),
            ip=_ip(request),
            customer_id=d.customer_id,
        )
    return RedirectResponse(f"{_PAGE}?saved=1", status_code=303)


def _archive(request, session, field_id, archived: bool):
    blocked = _admin_block(request)
    if blocked:
        return blocked
    d, blocked = _field_or_404(request, session, field_id)
    if blocked:
        return blocked
    refusal = cf.modify_refusal(security.session_user(request), _actor_email(request), d)
    if refusal:
        return _error_page(request, 403, refusal)
    if cf.set_archived(session, d, archived):
        audit.log(
            actor=_actor_email(request),
            action="field.archive" if archived else "field.restore",
            target=f"custom_field {d.id} '{d.key}'",
            ip=_ip(request),
            customer_id=d.customer_id,
        )
    return RedirectResponse(f"{_PAGE}?saved=1", status_code=303)


@router.post(_PAGE + "/{field_id}/archive")
def custom_fields_archive(field_id: int, request: Request, session: Session = Depends(get_session)):
    return _archive(request, session, field_id, True)


@router.post(_PAGE + "/{field_id}/restore")
def custom_fields_restore(field_id: int, request: Request, session: Session = Depends(get_session)):
    return _archive(request, session, field_id, False)


def values_from_form(form, defs) -> dict:
    """Build the ``{key: raw value}`` mapping from a project-fields form. Only fields
    the form actually carried are present; a link field assembles its url/key/label."""
    out = {}
    for d in defs:
        base = "cf." + d.key
        if d.type == "external_link":
            parts = {p: str(form.get(f"{base}.{p}") or "").strip() for p in ("url", "key", "label")}
            seen = any(f"{base}.{p}" in form for p in ("url", "key", "label"))
            if seen:
                out[d.key] = {k: v for k, v in parts.items() if v} or None
        elif base in form:
            out[d.key] = str(form.get(base) or "")
    return out


@router.post("/projects/{project_id}/fields")
async def project_set_fields(
    project_id: int, request: Request, session: Session = Depends(get_session)
):
    """Save the custom field values of one project (blank clears). Gated by project
    visibility AND a write role, like the status/due/content-type routes."""
    project = session.get(Project, project_id)
    if not project:
        return _missing_or(request, session, RedirectResponse("/projects", status_code=303))
    blocked = _visibility_block(request, session, project)
    if blocked:
        return blocked
    me = security.session_user(request)
    if security.auth_required() and not security.can_write(me):
        return _error_page(request, 403, "Changing project fields requires a write role.")
    form = await request.form()
    defs = cf.defs_for_project(session, project)
    try:
        changes = cf.set_values(
            session, project, values_from_form(form, defs), actor=_actor_email(request)
        )
    except cf.FieldError as exc:
        return RedirectResponse(f"/projects/{project_id}?err=" + quote(str(exc)), status_code=303)
    if changes:
        audit.log(
            actor=_actor_email(request),
            action="field.set",
            target=f"project {project_id}",
            detail=cf.audit_detail(changes),
            ip=_ip(request),
            customer_id=project.customer_id,
        )
    return RedirectResponse(f"/projects/{project_id}?saved=1", status_code=303)


@router.post("/projects/columns")
async def projects_set_columns(request: Request, session: Session = Depends(get_session)):
    """Save the projects-list custom-field columns for the signed-in account (or the
    shared local owner when sign-in is off)."""
    me = security.session_user(request)
    form = await request.form()
    defs = cf.list_defs(session, _my_org_id(request), _my_customer_id(request))
    owner, uid = cf.column_owner(me)
    cf.save_columns(session, owner, uid, [str(v) for v in form.getlist("col")], defs)
    return RedirectResponse("/projects", status_code=303)
