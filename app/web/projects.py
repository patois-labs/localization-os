"""Projects, customers, TM import/export, and glossary import.

Transitional compatibility shim for one release (added 2026-09-14, remove the re-export block after
the next tagged release): `__all__` re-exports PROJECTS_PAGE_SIZE, _can_manage_members,
_rename_template_compat, customers_index, customer_detail, customer_update_settings,
customer_context_card_save, context_card_delete, customer_add_pair, customer_remove_pair,
customer_pairs_import, customer_link_vendor, customer_unlink_vendor, create_customer,
customer_rename, customer_delete, customer_archive, customer_unarchive, create_project,
author_project_json, project_template_prefill_json, project_apply_language_set, project_add_locale,
project_remove_locale_confirm, project_remove_locale, project_set_status, project_set_due,
project_set_content_type, project_set_customer, project_rename, project_delete_confirm,
project_delete, project_archive, project_unarchive, project_from_template,
project_template_from_draft, project_save_template, template_delete, project_set_segmentation,
project_set_pair_providers, project_export, tm_import, tm_export, _sources_write_block,
glossary_import, glossary_export from app.web.projects_projects,
app.web.projects_project_rename_delete_sp, app.web.projects_glossary.

Documented in: docs/architecture/content-and-projects.md
"""

from copy import copy as _srp_copy

from fastapi import APIRouter, Depends, File, Form, Request, UploadFile
from fastapi.responses import JSONResponse, RedirectResponse, Response
from sqlmodel import Session, select

from app import config
from app.db import get_session
from app.kernel import (
    access,
    audit,
    connected_projects,
    content,
    content_types,
    delivery,
    export,
    glossary,
    glossary_tbx,
    ids,
    languages,
    languages_catalog,
    lifecycle,
    pair_providers,
    project_automation,
    project_budgets,
    project_sources,
    security,
    settings,
    tm,
    vendors,
)
from app.kernel import connectors as connectors_registry
from app.kernel import (
    custom_fields as cf_service,
)
from app.kernel import (
    customers as customer_service,
)
from app.kernel import (
    keys as keys_svc,
)
from app.kernel import (
    segmentation_profiles as segprof,
)
from app.kernel import (
    templates as template_service,
)
from app.models import (
    Customer,
    Document,
    Project,
    ProjectStatus,
    ProjectType,
    TargetLocale,
    TransMode,
)
from app.web.deps import (
    _actor_email,
    _admin_block,
    _ai_block,
    _customer_block,
    _error_page,
    _ip,
    _missing_block,
    _missing_or,
    _my_customer_id,
    _my_org_id,
    _org_block,
    _visibility_block,
    feature_block,
    read_capped_upload,
    templates,
)
from app.web.projects_glossary import (
    _sources_write_block,
    glossary_export,
    glossary_import,
    project_export,
    tm_export,
    tm_import,
)
from app.web.projects_glossary import router as projects_glossary_router
from app.web.projects_project_rename_delete_sp import (
    author_project_json,
    project_add_locale,
    project_apply_language_set,
    project_archive,
    project_delete,
    project_delete_confirm,
    project_from_template,
    project_remove_locale,
    project_remove_locale_confirm,
    project_rename,
    project_save_template,
    project_set_content_type,
    project_set_customer,
    project_set_due,
    project_set_pair_providers,
    project_set_segmentation,
    project_set_status,
    project_template_from_draft,
    project_template_prefill_json,
    project_unarchive,
    template_delete,
)
from app.web.projects_project_rename_delete_sp import (
    router as projects_project_rename_delete_sp_router,
)
from app.web.projects_projects import (
    PROJECTS_PAGE_SIZE,
    _can_manage_members,
    _rename_template_compat,
    context_card_delete,
    create_customer,
    create_project,
    customer_add_pair,
    customer_archive,
    customer_context_card_save,
    customer_delete,
    customer_detail,
    customer_link_vendor,
    customer_pairs_import,
    customer_remove_pair,
    customer_rename,
    customer_unarchive,
    customer_unlink_vendor,
    customer_update_settings,
    customers_index,
)
from app.web.projects_projects import router as projects_projects_router
from app.web.queries import _project_rows, _project_stats

__all__ = [
    "PROJECTS_PAGE_SIZE",
    "File",
    "Response",
    "UploadFile",
    "_admin_block",
    "_ai_block",
    "_can_manage_members",
    "_rename_template_compat",
    "_sources_write_block",
    "author_project_json",
    "context_card_delete",
    "create_customer",
    "create_project",
    "customer_add_pair",
    "customer_archive",
    "customer_context_card_save",
    "customer_delete",
    "customer_detail",
    "customer_link_vendor",
    "customer_pairs_import",
    "customer_remove_pair",
    "customer_rename",
    "customer_unarchive",
    "customer_unlink_vendor",
    "customer_update_settings",
    "customers_index",
    "delivery",
    "export",
    "feature_block",
    "glossary_export",
    "glossary_import",
    "glossary_tbx",
    "ids",
    "project_add_locale",
    "project_apply_language_set",
    "project_archive",
    "project_delete",
    "project_delete_confirm",
    "project_export",
    "project_from_template",
    "project_remove_locale",
    "project_remove_locale_confirm",
    "project_rename",
    "project_save_template",
    "project_set_content_type",
    "project_set_customer",
    "project_set_due",
    "project_set_pair_providers",
    "project_set_segmentation",
    "project_set_status",
    "project_template_from_draft",
    "project_template_prefill_json",
    "project_unarchive",
    "read_capped_upload",
    "template_delete",
    "tm_export",
    "tm_import",
    "vendors",
]

router = APIRouter()


def _ai_optout_status(session, project) -> dict:
    """AI opt-out read-back for the project page (DPAI-08)."""
    from app.kernel import ai_optout

    return ai_optout.status(session, project)


def _comment_status(project) -> dict:
    """#690: the "translate comments" read-back for the project page."""
    from app.kernel import comment_option

    return comment_option.status(project)


@router.get("/projects")
def projects_index(
    request: Request,
    page: int = 1,
    q: str = "",
    status: str = "",
    type: str = "",
    archived: str = "",
    intake: str = "",
    session: Session = Depends(get_session),
):
    from sqlalchemy import func, or_

    me = security.session_user(request)
    visible = access.visible_project_ids(session, me)
    # Multi-org: the customer dropdown (create-project form) is org-scoped too so a
    # scoped user can't see another org's customer names (leak vector 2). No-op off.
    # SPEC-customer-archive: this is a create-project PICKER, so archived customers are
    # filtered out (active_only) — a retired customer can't be chosen for a new project.
    cust_q = customer_service.active_only(
        access.org_filter(select(Customer), Customer, me)
    ).order_by(Customer.name)
    # R2-3 (MEDIUM 3, defense in depth): a customer-bound user's dropdown must
    # collapse to ONLY their own customer — this var isn't rendered by
    # projects.html today, but a future template change must not silently leak
    # every customer's name to a bound account via this list.
    _cid = access.current_customer_id(me)
    if _cid is not None:
        cust_q = cust_q.where(Customer.id == _cid)
    customers = session.exec(cust_q).all()
    cq = select(func.count()).select_from(Project)
    pq = select(Project).order_by(Project.created_at.desc())
    if visible is not None:
        cq = cq.where(Project.id.in_(visible))
        pq = pq.where(Project.id.in_(visible))
    # Status filter (P1, item 2). Default (blank / "default") = everything EXCEPT
    # completed/closed/cancelled (the maintainer's spec: "everything BUT done or closed").
    # "all" clears the filter; a specific status shows only that one.
    status = (status or "").strip().lower()
    if status in ("", "default"):
        status_filter = "default"
        cq = cq.where(Project.status.notin_(ProjectStatus.HIDDEN_BY_DEFAULT))
        pq = pq.where(Project.status.notin_(ProjectStatus.HIDDEN_BY_DEFAULT))
    elif status == "all":
        status_filter = "all"
    elif status in ProjectStatus.ALL:
        status_filter = status
        cq = cq.where(Project.status == status)
        pq = pq.where(Project.status == status)
    else:
        status_filter = "default"
        cq = cq.where(Project.status.notin_(ProjectStatus.HIDDEN_BY_DEFAULT))
        pq = pq.where(Project.status.notin_(ProjectStatus.HIDDEN_BY_DEFAULT))
    # SPEC-key-based-projects section 6: the TYPE filter beside the status chips.
    # Blank/unknown = every type (byte-identical to before this batch), so a
    # bookmarked /projects URL keeps meaning what it meant.
    type_filter = (type or "").strip().lower()
    if type_filter in ProjectType.ALL:
        cq = cq.where(Project.project_type == type_filter)
        pq = pq.where(Project.project_type == type_filter)
    else:
        type_filter = ""
    # Connected projects: the "Connected" filter (content comes from a connected system).
    # Blank/unknown = every project, byte-identical to before.
    intake_filter = "connected" if (intake or "").strip().lower() == "connected" else ""
    if intake_filter:
        cq = cq.where(Project.intake == intake_filter)
        pq = pq.where(Project.intake == intake_filter)
    # Archive filter (residual sweep: archive verbs). DEFAULT = ACTIVE only (archived
    # projects drop out of the default list, like the Customer archive precedent).
    # `archived=only` shows just the archived ones (the retire/restore view); `archived=all`
    # shows both. Any other value falls back to the default (active-only).
    archived_filter = (archived or "").strip().lower()
    if archived_filter == "only":
        cq = cq.where(Project.archived_at.is_not(None))
        pq = pq.where(Project.archived_at.is_not(None))
    elif archived_filter == "all":
        pass  # both active + archived
    else:
        archived_filter = "active"
        # Route the default (active) filter through the shipped seam (M4).
        cq = content.project_active_only(cq)
        pq = content.project_active_only(pq)
    q = (q or "").strip()
    if q:
        # Case-insensitive substring match on the project name OR its customer's
        # name; matching customer ids are resolved first so the Project query
        # itself stays a plain SQLModel select (no join needed).
        needle = f"%{q.lower()}%"
        matching_customer_ids = session.exec(
            select(Customer.id).where(func.lower(Customer.name).like(needle))
        ).all()
        name_or_customer = or_(
            func.lower(Project.name).like(needle),
            Project.customer_id.in_(matching_customer_ids) if matching_customer_ids else False,
        )
        cq = cq.where(name_or_customer)
        pq = pq.where(name_or_customer)
    # Custom project fields (SPEC-project-automation part C): `f.<key>=<expr>` filters,
    # `sort=f.<key>` ordering and the per-account column choice. With no field defined
    # this block does nothing (byte-identical list).
    cf_defs = cf_service.list_defs(session, _my_org_id(request), _cid)
    cf_by_key = cf_service.group_by_key(cf_defs)
    cf_filters: dict = {}
    cf_note = ""
    for fname, fraw in request.query_params.multi_items():
        if not fname.startswith("f.") or not (fraw or "").strip():
            continue
        fdef = cf_by_key.get(fname[2:].lower())
        if fdef is None:
            continue
        try:
            sub = cf_service.project_ids_matching(session, fdef, fraw)
        except cf_service.FieldError as exc:
            cf_note = str(exc)
            continue
        cq = cq.where(Project.id.in_(sub))
        pq = pq.where(Project.id.in_(sub))
        cf_filters[fdef[0].key] = fraw.strip()
    sort_raw = (request.query_params.get("sort") or "").strip()
    sort_dir = "desc" if (request.query_params.get("dir") or "").lower() == "desc" else "asc"
    sort_def = cf_by_key.get(sort_raw[2:].lower()) if sort_raw.startswith("f.") else None
    if sort_def is not None:
        pq = cf_service.apply_sort(pq.order_by(None), sort_def, descending=(sort_dir == "desc"))
    else:
        sort_raw = ""
    total = session.exec(cq).one()
    pages = max(1, -(-total // PROJECTS_PAGE_SIZE))
    page = min(max(1, page), pages)
    projects = session.exec(
        pq.offset((page - 1) * PROJECTS_PAGE_SIZE).limit(PROJECTS_PAGE_SIZE)
    ).all()
    cust_by_id = {c.id: c.name for c in customers}
    cf_owner, _cf_uid = cf_service.column_owner(me)
    cf_cols = cf_service.columns_for(session, cf_owner, cf_defs)
    _col_keys = {d.key for d in cf_cols}
    _col_defs = [d for d in cf_defs if d.key in _col_keys]
    _id_key = {d.id: d.key for d in _col_defs}
    cf_values = {
        pid: {_id_key[fid]: v for fid, v in vals.items()}
        for pid, vals in cf_service.values_for_projects(
            session, [p.id for p in projects], _col_defs
        ).items()
    }
    from urllib.parse import urlencode

    cf_qs = "".join("&" + urlencode({"f." + k: v}) for k, v in cf_filters.items()) + (
        ("&" + urlencode({"sort": sort_raw, "dir": sort_dir})) if sort_raw else ""
    )
    return templates.TemplateResponse(
        request,
        "projects.html",
        {
            "request": request,
            "customers": customers,
            "rows": _project_rows(session, projects, cust_by_id),
            "page": page,
            "pages": pages,
            "total": total,
            "q": q,
            "status_filter": status_filter,
            "type_filter": type_filter,
            "intake_filter": intake_filter,
            # Connected projects: project id -> {label, kind, bound} for the source badge.
            "src_labels": connected_projects.source_labels(session, [p.id for p in projects]),
            "connected_enabled": project_automation.module_enabled(),
            "archived_filter": archived_filter,
            "cf_defs": [
                cf_service.to_dict(ds[0]) for ds in cf_service.group_by_key(cf_defs).values()
            ],
            "cf_admin": (not security.auth_required()) or security.can_manage(me),
            "cf_cols": [cf_service.to_dict(d) for d in cf_cols],
            "cf_values": cf_values,
            "cf_filters": cf_filters,
            "cf_note": cf_note,
            "cf_qs": cf_qs,
            "cf_sort": sort_raw,
            "cf_dir": sort_dir,
            "project_types": ProjectType.ALL,
            "status_labels": ProjectStatus.LABELS,
            "status_all": ProjectStatus.ALL,
            "providers": config.PROVIDERS,
            "default_provider": config.DEFAULT_PROVIDER,
            "default_source": settings.get("default_source_locale"),
            # Language picker (P2, item 1): the enabled subset is offered by default;
            # the "show all" toggle reveals the full catalog (the escape hatch).
            "enabled_locales": languages_catalog.enabled_language_rows(),
            "all_locales": languages_catalog.all_locales(),
            # Segmentation picker (P6, item 8): only GLOBAL profiles are offered at
            # create time (the customer isn't known until the form posts); a
            # customer-scoped profile is picked on the project page afterwards.
            "seg_profiles": [
                p
                for p in segprof.list_profiles(org_id=_my_org_id(request))
                if p.customer_id is None
            ],
            # Templates (P7, item 9): the "Create from template" card. Customer-
            # scoped rows render labeled; applying one lands on its own customer.
            # R2-3 (HIGH 1): a customer-bound caller must see ONLY global + their
            # own customer's templates here — all_templates() returns EVERY
            # customer's rows (the admin/internal-staff view) and must never reach
            # a bound account (name + "customer #<id>" disclosure). No-op for
            # internal staff (_cid is None) — byte-identical to today.
            "templates": (
                template_service.list_templates(customer_id=_cid, org_id=_my_org_id(request))
                if _cid is not None
                else template_service.all_templates(org_id=_my_org_id(request))
            ),
            "tpl_warn": request.query_params.get("tpl_warn"),
            "picker_err": request.query_params.get("err"),
            # SPEC-rename-delete UI: gates the template rename form + Delete button —
            # both /templates/{id}/rename and /templates/{id}/delete routes are
            # strictly can_manage (manager+), so the buttons must match.
            "can_manage": (not security.auth_required()) or security.can_manage(me),
        },
    )


_srp_router_options = router.prefix, router.tags, router.dependencies
router.prefix, router.tags, router.dependencies = "", [], []
router.include_router(projects_projects_router)
router.prefix, router.tags, router.dependencies = _srp_router_options


_srp_piece_router = projects_project_rename_delete_sp_router
projects_project_rename_delete_sp_router = _srp_copy(projects_project_rename_delete_sp_router)
projects_project_rename_delete_sp_router.routes = projects_project_rename_delete_sp_router.routes[
    0:2
]
_srp_router_options = router.prefix, router.tags, router.dependencies
router.prefix, router.tags, router.dependencies = "", [], []
router.include_router(projects_project_rename_delete_sp_router)
router.prefix, router.tags, router.dependencies = _srp_router_options
projects_project_rename_delete_sp_router = _srp_piece_router


@router.get("/projects/{project_id}")
def project_detail(project_id: int, request: Request, session: Session = Depends(get_session)):
    project = session.get(Project, project_id)
    if not project:
        return _missing_block(request, session)
    blocked = _visibility_block(request, session, project)
    if blocked:
        return blocked
    me = security.session_user(request)
    all_locales = session.exec(
        select(TargetLocale).where(TargetLocale.project_id == project_id)
    ).all()
    # Connected projects: a stopped language is kept (its segments, TM and exports stay) but is
    # not an active target any more; it is listed apart, with a way to bring it back.
    locales = [tl for tl in all_locales if tl.stopped_at is None]
    stopped_locales = [tl for tl in all_locales if tl.stopped_at is not None]
    project_is_bound = content.is_bound_project(session, project_id)
    customer = session.get(Customer, project.customer_id) if project.customer_id else None
    # 2026-07-14: a project WITHOUT a customer is flagged for assignment (a banner
    # with a customer picker). The choices are active, org-scoped customers (same picker
    # rule as new-project). Only computed/rendered when the project has no customer.
    customer_choices = []
    if project.customer_id is None:
        customer_choices = session.exec(
            customer_service.active_only(
                access.org_filter(select(Customer), Customer, me)
            ).order_by(Customer.name)
        ).all()
    # Custom project fields: the definitions that apply to this project (archived ones
    # only when they hold a value) and its values, keyed by field key.
    cf_proj_vals = cf_service.project_values(session, project, include_archived=True)
    cf_proj_defs = [
        cf_service.to_dict(d)
        for d in cf_service.defs_for_project(session, project, include_archived=True)
        if d.archived_at is None or d.key in cf_proj_vals
    ]
    documents = session.exec(
        select(Document)
        # SPEC-key-based-projects 2.5: internal key documents are never listed.
        .where(Document.project_id == project_id, Document.fmt != keys_svc.KEY_DOC_FMT)
        .order_by(Document.created_at.desc())
    ).all()
    # CV-1: the 7-band WORD-based TM-leverage breakdown (the real vendor-cost model),
    # alongside the coarse 4-band bar — computed from the SAME segments _project_stats
    # already loads (M4: no second full segment load per render). Ordered identically to
    # the vendor_cost.project_leverage loader seam that /api + CLI use, so the numbers are
    # byte-identical across surfaces.
    stats = _project_stats(session, [d.id for d in documents], with_leverage7=True)
    leverage7 = stats["leverage7"]
    leverage7_rows = stats["leverage7_rows"]
    # Locale-removal permanence: the chip x is only offered for a
    # locale still removable — untranslated (no target text, no run of its own) AND
    # not the project's last remaining locale. translated_target_locales is ONE
    # grouped segment query + ONE grouped run query for the WHOLE project (reusing
    # the `documents` list already fetched above), never per-locale N+1, so this
    # scales the same whether the project has 1 or 50 target locales.
    _translated_norms = content.translated_target_locales(
        session, project_id, doc_ids=[d.id for d in documents]
    )
    # A connected project's languages are stopped, never deleted, so the translated-content
    # permanence rule does not apply to them: every active language is stoppable while more
    # than one is active.
    removable_locale_ids = {
        tl.id
        for tl in locales
        if len(locales) > 1
        and (project_is_bound or languages_catalog.normalize(tl.locale) not in _translated_norms)
    }
    # Active-pairs confinement seam (the maintainer's P2): the add-locale picker's default
    # offering = this customer's active-pair targets reachable from the project's
    # source; falls back to the full enabled subset when the customer has no pairs.
    _apairs_enabled, _apairs_confined = customer_service.confined_target_rows(
        session, customer, source_locale=project.source_locale
    )
    try:
        glossary.parse_cursor(request.query_params.get("glossary_cursor"))
    except ValueError as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
    glossary_page = glossary.page_entries(
        session,
        project_id=project_id,
        cursor=request.query_params.get("glossary_cursor"),
        limit=200,
    )
    # Connected projects: the setup menu / Source card state (None for every other project,
    # so their page renders exactly as before).
    connected = None
    if connected_projects.is_connected(project):
        _can_manage = (not security.auth_required()) or security.can_manage(me)
        _row = project_automation.get(session, project.id)
        _auto = project_automation.view(
            session,
            _row,
            can_see_cost=_can_manage,
            visible=lambda pid: access.can_view_project(session, me, pid),
        )
        connected = connected_projects.card_state(
            session,
            project,
            _row,
            _auto,
            user=me,
            can_manage=_can_manage,
            want_preview=request.query_params.get("preview") == "1",
        )
        connected["auto"] = _auto
        connected["can_manage"] = _can_manage
        connected["is_admin"] = (not security.auth_required()) or security.is_admin(me)
        connected["active_job_id"] = (
            project_automation.active_job(session, _row) if _row is not None else None
        )
        connected["scheduler_enabled"] = project_automation.global_switch_on()
        connected["kind_label"] = ""
        if _row is not None:
            _sp = connectors_registry.spec(_auto.get("connection_kind", ""))
            connected["kind_label"] = _sp.name if _sp else _auto.get("connection_kind", "")
        # ?connection=<id> preselects a connection in step 1 (after "Connect a new system").
        # coerce_id: ASCII digits in range only (a Unicode digit must not reach int()).
        _pre = ids.coerce_id(request.query_params.get("connection", ""))
        connected["preselect"] = (
            _pre
            if _pre is not None and any(c["id"] == _pre for c in connected["choices"])
            else None
        )
        connected["src_msg"] = request.query_params.get("src_msg", "")[:240]
        connected["src_ok"] = request.query_params.get("src_ok") == "1"
    # "Make this a connected project": offered (manager and up) on a plain project that a
    # connection already feeds and that has no content source yet. Never done automatically.
    convert_offer = []
    if connected is None and ((not security.auth_required()) or security.can_manage(me)):
        convert_offer = connected_projects.convert_offer(session, project)
    return templates.TemplateResponse(
        request,
        "project.html",
        {
            "request": request,
            "project": project,
            "connected": connected,
            "convert_offer": convert_offer,
            "cf_defs": cf_proj_defs,
            "cf_vals": cf_proj_vals,
            "locales": locales,
            "stopped_locales": stopped_locales,
            # exports and TM downloads keep covering a stopped language (its text stays)
            "export_locales": all_locales,
            "project_is_bound": project_is_bound,
            "removable_locale_ids": removable_locale_ids,
            "customer": customer,
            "customer_choices": customer_choices,
            "documents": documents,
            # Batch 13: ZIP batch uploads, each offered back as a per-language ZIP.
            "zip_batches": delivery.zip_batches(session, project_id, documents=documents),
            "modes": [TransMode.SEGMENTED, TransMode.FULL_TEXT],
            "leverage7": leverage7,
            "leverage7_rows": leverage7_rows,
            "tm_count": tm.count(session, project_id),
            "ft_count": tm.ft_count(session, project_id),
            "glossary_count": glossary.count(session, project_id),
            "glossary_entries": glossary_page["entries"],
            "glossary_next_cursor": glossary_page["next_cursor"],
            # --- attach an ALREADY-ESTABLISHED TM / glossary (feedback item 5) ---
            # Both lists and both candidate sets are resolved SERVER-SIDE through the
            # project_sources seam (same-org/same-customer predicate + this caller's
            # own project visibility), never filtered in the template: a dropdown must
            # not be able to name something the POST would refuse, and must not become
            # an enumeration oracle. A viewer gets the lists and no candidates — the
            # template hides the forms behind the same can_write gate the POSTs enforce.
            "tm_sources": project_sources.list_tm_sources(session, project_id),
            "tm_source_choices": project_sources.tm_source_candidates(session, project_id, user=me),
            "glossary_pins": project_sources.list_glossary_pins(session, project_id),
            "glossary_pin_choices": project_sources.glossary_pin_candidates(
                session,
                project_id,
                org_id=_my_org_id(request),
                bound_customer_id=_my_customer_id(request),
            ),
            "stats": stats,
            # #563: documents carry a DERIVED lifecycle status (the stored column never moves).
            "doc_statuses": lifecycle.document_status_map(session, [d.id for d in documents]),
            "doc_status_labels": lifecycle.DOC_STATUS_LABELS,
            "members": access.list_members(session, project_id),
            "can_manage_members": _can_manage_members(me, project),
            # Org-scoped language sets in the apply-set dropdown (leak vector 2/4).
            "language_sets": languages.list_sets(org_id=_my_org_id(request)),
            # --- segmentation profile (P6, item 8) ---
            # Offered picks = global + this project's customer's own; the display
            # row shows the RESOLVED profile so "inherit" is never a mystery.
            "seg_choices": [
                p
                for p in segprof.list_profiles(org_id=_my_org_id(request))
                if p.customer_id in (None, project.customer_id)
            ],
            "seg_resolved": segprof.resolve_profile(session, project),
            # --- content type (SPEC-content-types) ---
            # Offered picks = global + this project's customer's own (the attachable
            # set); the display row shows the currently-assigned type.
            "ct_choices": content_types.options_for_project(
                session, org_id=_my_org_id(request), customer_id=project.customer_id
            ),
            "content_type": (
                content_types.get(session, project.content_type_id)
                if project.content_type_id
                else None
            ),
            # --- lifecycle + delivery (P1) ---
            "status_labels": ProjectStatus.LABELS,
            # Issue #54 L4: whether the stored COMPLETED lifecycle is actually backed by
            # every segment being translated. lifecycle.set_status accepts a MANUAL
            # COMPLETED with zero translations, so the project-page chip may say "Completed"
            # while no work is done — the "(all segments translated)" claim must only render
            # when it is TRUE. Computed only for a completed project (the predicate runs a
            # query); False/omitted otherwise keeps the chip a neutral "Completed".
            "all_segments_translated": (
                lifecycle._all_segments_translated(session, project_id)
                if project.status == ProjectStatus.COMPLETED
                else False
            ),
            # --- due date (Feature A): the badge status for the project page ---
            "due_status": content.due_status(project),
            "can_write": (not security.auth_required()) or security.can_write(me),
            # SPEC-rename-delete UI: gates the Delete-project link on the page — the
            # GET /projects/{id}/delete route itself is strictly is_admin (every new
            # delete is), so the link must match or a non-admin just bounces off a 403.
            "is_admin": (not security.auth_required()) or security.is_admin(me),
            # AI opt-out (HECVAT DPAI-08): the effective state + who may change it.
            "ai_status": _ai_optout_status(session, project),
            # #690: the per-project "translate comments" option (Office comments).
            "comment_status": _comment_status(project),
            "can_manage": (not security.auth_required()) or security.can_manage(me),
            # Hero download card shows when the project is delivered-ready.
            "show_download": project.status in (ProjectStatus.COMPLETED, ProjectStatus.CLOSED),
            # Language picker (P2, item 1/4): the enabled subset offered by default,
            # full catalog behind the "show all" toggle. Active-pairs confinement
            # (The maintainer's P2): when this project's customer has active pairs, the add-
            # locale picker offers ONLY that customer's pair targets (reachable from
            # the project's own source) by default; "Show all languages" is the
            # escape hatch. Fresh install / no pairs -> the full enabled subset.
            "enabled_locales": _apairs_enabled,
            "pairs_confined": _apairs_confined,
            "all_locales": languages_catalog.all_locales(),
            # --- per-pair provider mode (Phase 2) ---
            # The current mode + the stored per-pair overrides (target -> provider),
            # so the settings card can render the editable matrix and reflect what's
            # persisted. mapping_for_project returns [] for a UNIFORM project.
            # providers_list = the built-in provider names for the matrix dropdowns
            # (engine:<id> options come from the engine_options Jinja global).
            "provider_mode": project.provider_mode,
            "pair_providers": pair_providers.mapping_for_project(session, project_id),
            "providers_list": config.PROVIDERS,
            # Document-upload client-side size guard (hygiene wave 1 item 4): the
            # SAME cap the upload_document route enforces server-side, so the form
            # can reject an oversized file before the upload even starts instead of
            # only after a round trip that ends in ?err=toolarge_doc. Server-side
            # enforcement (app/web/documents.py) stays authoritative and unchanged.
            "max_upload_bytes": config.MAX_UPLOAD_BYTES,
            # CV-2: the project money-budget meter — MANAGER+ only (spend $ figures are
            # not viewer-visible, matching the CV-4 cost gate + the setup-ai meter
            # decision). None for an under-ranked viewer -> the template renders nothing
            # (no stub). budget_state is safe even with no ProjectBudget row (has_budget
            # False), so a manager always sees the section (with a "set a budget" prompt).
            "budget_state": (
                project_budgets.budget_state(session, project_id)
                if ((not security.auth_required()) or security.can_manage(me))
                else None
            ),
        },
    )


_srp_piece_router = projects_project_rename_delete_sp_router
projects_project_rename_delete_sp_router = _srp_copy(projects_project_rename_delete_sp_router)
projects_project_rename_delete_sp_router.routes = projects_project_rename_delete_sp_router.routes[
    2:19
]
_srp_router_options = router.prefix, router.tags, router.dependencies
router.prefix, router.tags, router.dependencies = "", [], []
router.include_router(projects_project_rename_delete_sp_router)
router.prefix, router.tags, router.dependencies = _srp_router_options
projects_project_rename_delete_sp_router = _srp_piece_router


@router.post("/templates/{template_id}/rename")
def template_rename(
    template_id: int,
    request: Request,
    name: str = Form(...),
    session: Session = Depends(get_session),
):
    """Rename a ProjectTemplate (SPEC-rename-delete). Manage-role gated, matching the
    template delete route; the service org-scopes the lookup so a cross-org id is
    not-found. The rename service function lives in templates.py (Branch 3) — this route
    is the single web surface assigned to Branch 2 (see _rename_template_compat)."""
    me = security.session_user(request)
    if security.auth_required() and not security.can_manage(me):
        return _error_page(request, 403, "Renaming templates requires a manager role.")
    # Org-scoped 404 gate (isolation held by the route regardless of service internals).
    tpl = template_service.get_template(template_id, org_id=_my_org_id(request))
    if tpl is None:
        return _error_page(request, 404, "Not found.")
    # R2-3 (permanent route-sweep finding, defense in depth): the org-scoped gate
    # above doesn't confine a CUSTOMER-scoped template to its own customer — a
    # caller bound to a DIFFERENT customer would otherwise still reach it (global
    # templates, customer_id None, stay reachable everywhere). Mirrors the same
    # check added to template_delete; see that route's comment for why this is
    # belt-and-suspenders (a bound account can never hold manage role today).
    my_cid = _my_customer_id(request)
    if my_cid is not None and tpl.customer_id is not None and tpl.customer_id != my_cid:
        return _error_page(request, 404, "Not found.")
    try:
        _rename_template_compat(
            session,
            template_id,
            name,
            org_id=_my_org_id(request),
            actor=_actor_email(request),
            ip=_ip(request),
        )
    except template_service.TemplateError as exc:
        return _error_page(request, 403, str(exc))
    return RedirectResponse("/projects#templates", status_code=303)


_srp_piece_router = projects_project_rename_delete_sp_router
projects_project_rename_delete_sp_router = _srp_copy(projects_project_rename_delete_sp_router)
projects_project_rename_delete_sp_router.routes = projects_project_rename_delete_sp_router.routes[
    19:23
]
_srp_router_options = router.prefix, router.tags, router.dependencies
router.prefix, router.tags, router.dependencies = "", [], []
router.include_router(projects_project_rename_delete_sp_router)
router.prefix, router.tags, router.dependencies = _srp_router_options
projects_project_rename_delete_sp_router = _srp_piece_router


_srp_piece_router = projects_glossary_router
projects_glossary_router = _srp_copy(projects_glossary_router)
projects_glossary_router.routes = projects_glossary_router.routes[0:1]
_srp_router_options = router.prefix, router.tags, router.dependencies
router.prefix, router.tags, router.dependencies = "", [], []
router.include_router(projects_glossary_router)
router.prefix, router.tags, router.dependencies = _srp_router_options
projects_glossary_router = _srp_piece_router


# --- project membership ---
@router.post("/projects/{project_id}/members")
def member_add(
    project_id: int,
    request: Request,
    email: str = Form(...),
    session: Session = Depends(get_session),
):
    project = session.get(Project, project_id)
    if not project:
        return _missing_or(request, session, RedirectResponse("/projects", status_code=303))
    # Multi-org: a cross-org project is 404 (not enumerable) BEFORE the same-org
    # 403 manage-members check. No-op with the gate off.
    blocked = _org_block(request, getattr(project, "org_id", None))
    if blocked:
        return blocked
    # R2-3 (permanent route-sweep finding): this route was missing the customer
    # ring the rest of the project-mutation routes all have — a customer-bound
    # caller hitting a FOREIGN customer's project fell through to the manage-
    # members role check below and got a 403 (vs. a 303 redirect for a truly
    # missing id), leaking the foreign project's existence. 404 it here instead,
    # BEFORE the role check, matching every other project route in this file.
    blocked = _customer_block(request, project.customer_id)
    if blocked:
        return blocked
    # #575: the project-scoping ring too, BEFORE the role check, so a non-member's 403
    # is the same "no access" answer a missing id now gets.
    blocked = _visibility_block(request, session, project)
    if blocked:
        return blocked
    me = security.session_user(request)
    if not _can_manage_members(me, project):
        return _error_page(
            request, 403, "Only administrators, managers or the project creator can manage members."
        )
    ok, msg = access.add_member(session, project_id, email, added_by=getattr(me, "email", "") or "")
    if ok:
        audit.log(
            actor=_actor_email(request),
            action="project.member_add",
            target=f"project {project_id} {email.strip().lower()}",
            ip=_ip(request),
            customer_id=project.customer_id,
        )
    from urllib.parse import quote

    return RedirectResponse(
        f"/projects/{project_id}?member_ok={1 if ok else 0}&member_msg={quote(msg)}",
        status_code=303,
    )


@router.post("/projects/{project_id}/members/{uid}/remove")
def member_remove(
    project_id: int, uid: int, request: Request, session: Session = Depends(get_session)
):
    project = session.get(Project, project_id)
    if not project:
        return _missing_or(request, session, RedirectResponse("/projects", status_code=303))
    blocked = _org_block(request, getattr(project, "org_id", None))
    if blocked:
        return blocked
    # R2-3 (permanent route-sweep finding): same fix as member_add above — the
    # customer ring was missing here, so a foreign-customer project fell through
    # to the manage-members role check and leaked its existence via a 403
    # instead of a 303 (matching a genuinely missing id).
    blocked = _customer_block(request, project.customer_id)
    if blocked:
        return blocked
    # #575: the project-scoping ring too, BEFORE the role check, so a non-member's 403
    # is the same "no access" answer a missing id now gets.
    blocked = _visibility_block(request, session, project)
    if blocked:
        return blocked
    me = security.session_user(request)
    if not _can_manage_members(me, project):
        return _error_page(
            request, 403, "Only administrators, managers or the project creator can manage members."
        )
    if access.remove_member(session, project_id, uid):
        audit.log(
            actor=_actor_email(request),
            action="project.member_remove",
            target=f"project {project_id} user {uid}",
            ip=_ip(request),
            customer_id=project.customer_id,
        )
    return RedirectResponse(f"/projects/{project_id}", status_code=303)


_srp_piece_router = projects_glossary_router
projects_glossary_router = _srp_copy(projects_glossary_router)
projects_glossary_router.routes = projects_glossary_router.routes[1:3]
_srp_router_options = router.prefix, router.tags, router.dependencies
router.prefix, router.tags, router.dependencies = "", [], []
router.include_router(projects_glossary_router)
router.prefix, router.tags, router.dependencies = _srp_router_options
projects_glossary_router = _srp_piece_router


@router.post("/projects/{project_id}/tm-sources/add")
def tm_source_add(
    project_id: int,
    request: Request,
    source_project_id: str = Form(""),
    session: Session = Depends(get_session),
):
    """Attach another project's TM as a source for this one (a REFERENCE — nothing is
    copied, and detaching restores the previous behaviour byte-identically)."""
    project, stop = _sources_write_block(
        request, session, project_id, "Attaching a translation memory"
    )
    if stop is not None:
        return stop
    me = security.session_user(request)
    try:
        view = project_sources.add_tm_source(
            session, project_id, source_project_id, created_by=_actor_email(request), user=me
        )
    except project_sources.AttachError:
        # Deliberately generic in the URL: a distinct "wrong customer" vs "no such
        # project" signal would let a caller probe for project ids outside its ring.
        return RedirectResponse(f"/projects/{project_id}?err=tm_source", status_code=303)
    audit.log(
        actor=_actor_email(request),
        action="project.tm_source.add",
        target=f"project {project_id} <- project {view['source_project_id']}",
        ip=_ip(request),
        customer_id=project.customer_id,
    )
    # Report REACHABLE vs TOTAL (behaviour review F4). The content-type layer of the
    # read scope is not widened by an attach, so a source of a different content type
    # can be attached successfully and match NOTHING. Saying "attached" alone, next to
    # the source's own row count, was a success message for a silent no-op.
    return RedirectResponse(
        f"/projects/{project_id}?tm_source_added=1"
        f"&tm_src_n={view['entry_count']}&tm_src_total={view['total_count']}",
        status_code=303,
    )


@router.post("/projects/{project_id}/tm-sources/remove")
def tm_source_remove(
    project_id: int,
    request: Request,
    source_project_id: str = Form(""),
    session: Session = Depends(get_session),
):
    project, stop = _sources_write_block(
        request, session, project_id, "Detaching a translation memory"
    )
    if stop is not None:
        return stop
    # Honour the service's answer (behaviour review F9): reporting "detached" for a
    # link that was never there tells an operator a state change happened when none
    # did -- and the most likely reason it was not there is that somebody else already
    # detached it, which is exactly when a false confirmation misleads.
    removed = project_sources.remove_tm_source(session, project_id, source_project_id)
    if removed:
        audit.log(
            actor=_actor_email(request),
            action="project.tm_source.remove",
            target=f"project {project_id} -x- project {source_project_id}",
            ip=_ip(request),
            customer_id=project.customer_id,
        )
    flag = "tm_source_removed" if removed else "tm_source_absent"
    return RedirectResponse(f"/projects/{project_id}?{flag}=1", status_code=303)


@router.post("/projects/{project_id}/glossaries/add")
def project_glossary_add(
    project_id: int,
    request: Request,
    glossary_id: str = Form(""),
    session: Session = Depends(get_session),
):
    """Pin an existing module glossary to this project. A DRAFT glossary may be
    pinned; it contributes nothing until approved (enforced at run time)."""
    project, stop = _sources_write_block(request, session, project_id, "Attaching a glossary")
    if stop is not None:
        return stop
    try:
        view = project_sources.add_glossary_pin(
            session,
            project_id,
            glossary_id,
            created_by=_actor_email(request),
            # The CALLER's module visibility, not just the project's ring (ISO-4):
            # a customer-bound user may not pin a legacy unassigned glossary that
            # /glossaries refuses to show them.
            org_id=_my_org_id(request),
            bound_customer_id=_my_customer_id(request),
        )
    except project_sources.AttachError:
        return RedirectResponse(f"/projects/{project_id}?err=glossary_pin", status_code=303)
    audit.log(
        actor=_actor_email(request),
        action="project.glossary.attach",
        target=f"project {project_id} <- glossary {view['glossary_id']}",
        ip=_ip(request),
        customer_id=project.customer_id,
    )
    return RedirectResponse(f"/projects/{project_id}?glossary_pinned=1", status_code=303)


@router.post("/projects/{project_id}/glossaries/remove")
def project_glossary_remove(
    project_id: int,
    request: Request,
    glossary_id: str = Form(""),
    session: Session = Depends(get_session),
):
    project, stop = _sources_write_block(request, session, project_id, "Detaching a glossary")
    if stop is not None:
        return stop
    removed = project_sources.remove_glossary_pin(session, project_id, glossary_id)
    if removed:
        audit.log(
            actor=_actor_email(request),
            action="project.glossary.detach",
            target=f"project {project_id} -x- glossary {glossary_id}",
            ip=_ip(request),
            customer_id=project.customer_id,
        )
    flag = "glossary_unpinned" if removed else "glossary_pin_absent"
    return RedirectResponse(f"/projects/{project_id}?{flag}=1", status_code=303)


_srp_piece_router = projects_glossary_router
projects_glossary_router = _srp_copy(projects_glossary_router)
projects_glossary_router.routes = projects_glossary_router.routes[3:5]
_srp_router_options = router.prefix, router.tags, router.dependencies
router.prefix, router.tags, router.dependencies = "", [], []
router.include_router(projects_glossary_router)
router.prefix, router.tags, router.dependencies = _srp_router_options
projects_glossary_router = _srp_piece_router


@router.post("/projects/{project_id}/glossary/add")
def glossary_add(
    project_id: int,
    request: Request,
    source_term: str = Form(...),
    target_term: str = Form(""),
    target_locale: str = Form(""),
    source_locale: str = Form(""),
    dnt: str = Form(""),
    note: str = Form(""),
    session: Session = Depends(get_session),
):
    """Persist a single glossary term in-app (no CSV round-trip). Gated by the
    same project-visibility block as the other project mutations; reuses the
    glossary service store (glossary.add_term)."""
    project = session.get(Project, project_id)
    if not project:
        return _missing_or(request, session, RedirectResponse("/projects", status_code=303))
    blocked = _visibility_block(request, session, project)
    if blocked:
        return blocked
    me = security.session_user(request)
    if security.auth_required() and not security.can_write(me):
        return _error_page(request, 403, "Adding a glossary term requires a write role.")
    src = (source_locale or "").strip() or project.source_locale
    tgt = (target_locale or "").strip()
    entry = glossary.add_term(
        session,
        project_id,
        source_term,
        target_term=target_term,
        source_locale=src,
        target_locale=tgt,
        dnt=str(dnt).strip().lower() in ("1", "true", "yes", "y", "on"),
        note=note,
    )
    if entry is not None:
        audit.log(
            actor=_actor_email(request),
            action="glossary.add",
            target=f"project {project_id} '{entry.source_term}'",
            ip=_ip(request),
            customer_id=project.customer_id,
        )
        return RedirectResponse(f"/projects/{project_id}?gl_added=1", status_code=303)
    return RedirectResponse(f"/projects/{project_id}?err=glossary_add", status_code=303)
