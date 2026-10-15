"""projects: projects

Documented in: docs/architecture/content-and-projects.md
"""

import logging

from fastapi import APIRouter, Depends, File, Form, Request, UploadFile
from fastapi.responses import RedirectResponse
from sqlmodel import Session, select

from app import config
from app.db import get_session
from app.kernel import (
    access,
    audit,
    content,
    content_types,
    ids,
    languages_catalog,
    security,
    vendors,
)
from app.kernel import (
    customers as customer_service,
)
from app.kernel import (
    segmentation_profiles as segprof,
)
from app.kernel import (
    templates as template_service,
)
from app.models import (
    Customer,
    Project,
    ProjectStatus,
    ProjectType,
)
from app.web.deps import (
    WorkerBusy,
    _actor_email,
    _admin_block,
    _customer_block,
    _error_page,
    _ip,
    _missing_customer_or,
    _my_customer_id,
    _my_org_id,
    _org_block,
    feature_block,
    offload_blocking,
    read_capped_upload,
    templates,
)

router = APIRouter()
_logger = logging.getLogger("loc_tms")


PROJECTS_PAGE_SIZE = 50


def _can_manage_members(me, project) -> bool:
    """Membership admin: admins/managers, or the project's creator."""
    if not security.auth_required():
        return True
    return bool(me) and (
        security.can_manage(me) or (getattr(me, "email", "") or "") == (project.created_by or "")
    )


def _rename_template_compat(session, template_id, name, *, org_id=None, actor="", ip=""):
    """SPEC-rename-delete branch-coordination: the ProjectTemplate rename SERVICE lives
    in templates.py and is owned by Branch 3 (signature rename_template(session,
    template_id, name, ...)); this single WEB route is assigned to Branch 2. When Branch
    3 has shipped rename_template, delegate to it (the single source of truth); until
    then, perform an equivalent thin rename here (blank + dup-name check mirroring
    save_as_template, audit) so this branch is testable + mergeable independently of
    branch-merge order. The route enforces the org-scoped 404 BEFORE calling this, so
    isolation holds either way. Returns the template, or None if it does not exist."""
    from app.models import ProjectTemplate

    fn = getattr(template_service, "rename_template", None)
    if callable(fn):
        # Positional-only (the guaranteed part of the agreed signature); the service
        # owns its own audit/validation.
        return fn(session, template_id, name)
    new_name = (name or "").strip()
    if not new_name:
        raise template_service.TemplateError("template name is required")
    tpl = session.get(ProjectTemplate, template_id)
    if tpl is None:
        return None
    # templates.get_by_name() is org-UNSCOPED (single-tenant callers rely on
    # that), so it can't be reused here: an org-A caller renaming to a name
    # that only exists in org B would get a false "already exists" refusal —
    # a cross-tenant name-existence oracle, and org A couldn't legitimately
    # reuse org B's name. This compat path deliberately scopes the dup-check
    # to the caller's own org instead of delegating to the service helper.
    dup_q = select(ProjectTemplate).where(ProjectTemplate.name == new_name)
    if org_id is not None:
        dup_q = dup_q.where(ProjectTemplate.org_id == org_id)
    dup = session.exec(dup_q).first()
    if dup is not None and dup.id != template_id:
        raise template_service.TemplateError(f"a template named {new_name!r} already exists")
    old_name = tpl.name
    tpl.name = new_name
    session.add(tpl)
    session.commit()
    audit.log(
        actor=actor or "system",
        action="template.rename",
        target=f"template {template_id}",
        detail=f"{old_name!r} -> {new_name!r}",
        ip=ip,
    )
    return tpl


@router.get("/customers")
def customers_index(request: Request, session: Session = Depends(get_session)):
    # Multi-org: a scoped user sees ONLY their own org's customers (leak vector 2).
    # org_filter is a strict no-op with the gate off (byte-identical to today).
    me = security.session_user(request)
    cq = access.org_filter(select(Customer), Customer, me).order_by(Customer.name)
    pq = access.org_filter(select(Project), Project, me)
    # R2-3: a customer-bound user sees ONLY their own customer here (no cross-
    # customer bleed) — the index collapses to their single customer's card. No-op
    # for internal staff (my_cid is None).
    my_cid = access.current_customer_id(me)
    if my_cid is not None:
        cq = cq.where(Customer.id == my_cid)
        pq = pq.where(Project.customer_id == my_cid)
    # SPEC-customer-archive: this is the MANAGEMENT view, not a picker — it shows
    # archived customers too (in their own labelled section) so an admin can find +
    # unarchive one, and so a customer's card never silently vanishes. Split the
    # org/bound-scoped list into active (the main cards) + archived (the section).
    all_customers = session.exec(cq).all()
    customers = [c for c in all_customers if c.archived_at is None]
    archived_customers = [c for c in all_customers if c.archived_at is not None]
    projects = session.exec(pq).all()
    visible = access.visible_project_ids(session, me)
    if visible is not None:
        projects = [p for p in projects if p.id in visible]
    counts: dict[int, int] = {}
    # Per-customer status breakdown (P4, item 5): the index becomes a table of cards
    # with a project count + an active/completed split so the menu is "actually
    # useful" (the maintainer's ask). "active" = not hidden-by-default (in-flight work).
    status_counts: dict[int, dict] = {}
    for p in projects:
        if p.customer_id:
            counts[p.customer_id] = counts.get(p.customer_id, 0) + 1
            sc = status_counts.setdefault(p.customer_id, {"active": 0, "done": 0})
            if p.status in ProjectStatus.HIDDEN_BY_DEFAULT:
                sc["done"] += 1
            else:
                sc["active"] += 1
    me_admin = (not security.auth_required()) or security.is_admin(me)
    return templates.TemplateResponse(
        request,
        "customers.html",
        {
            "request": request,
            "customers": customers,
            "archived_customers": archived_customers,
            "counts": counts,
            "status_counts": status_counts,
            "is_admin": me_admin,
        },
    )


@router.get("/customers/{customer_id}")
def customer_detail(customer_id: int, request: Request, session: Session = Depends(get_session)):
    """Customer 360 (P4, item 5): a detail page with cards linking to the customer's
    projects, connectors, workflows, language pairs, TM stats + an editable
    preferences/defaults form. Everything is scoped to this one customer (isolation).
    """
    me = security.session_user(request)
    customer = session.get(Customer, customer_id)
    if not customer:
        return _missing_customer_or(request, _error_page(request, 404, "Not found."))
    # Multi-org: a scoped user must not open another org's customer (leak vector 2).
    blocked = _org_block(request, getattr(customer, "org_id", None))
    if blocked:
        return blocked
    # R2-3: a customer-bound user may open ONLY their own customer's 360 (404 for
    # any other — not enumerable). No-op for internal staff.
    blocked = _customer_block(request, customer_id)
    if blocked:
        return blocked
    data = customer_service.get_customer_360(
        session,
        customer_id,
        org_id=_my_org_id(request),
        visible_pids=access.visible_project_ids(session, me),
    )
    # Workflows offered as a default-workflow pick = global + this customer's own,
    # confined to the caller's org exactly like seg_choices and cust_templates below.
    wf_choices = customer_service.customer_workflows(
        session, customer_id, org_id=_my_org_id(request)
    )
    from app.web.workflows_common import _visible_workflows

    wf_choices = _visible_workflows(request, session, wf_choices)
    # Segmentation profiles offered as the default pick = global + this customer's
    # own (P6; the same isolation rule the service enforces on save).
    seg_choices = [
        p
        for p in segprof.list_profiles(org_id=_my_org_id(request))
        if p.customer_id in (None, customer_id)
    ]
    # Templates visible in this customer's context = global + their own (P7).
    cust_templates = template_service.list_templates(
        customer_id=customer_id, org_id=_my_org_id(request)
    )
    # SPEC-content-types: this customer's OWN content types (the manageable section)
    # + the content types a project for this customer could be assigned (own + global)
    # for the default-content-type picker.
    own_content_types = [  # noqa: C416
        t
        for t in content_types.list_types(
            session, org_id=_my_org_id(request), customer_id=customer_id
        )
    ]
    ct_choices = content_types.options_for_project(
        session, org_id=_my_org_id(request), customer_id=customer_id
    )
    # Reference counts include projects, connections and TM outside membership scope.
    # Restricted callers can see the type's name, but not these unscoped totals.
    ct_summaries = (
        {t.id: content_types.to_dict(session, t) for t in own_content_types}
        if access.visible_project_ids(session, me) is None
        else {}
    )
    # SPEC-context-metadata: this customer's standing context cards (the general card +
    # per-content-type cards), keyed for the template by content_type_id (None = general).
    from app.kernel import context_pkg

    ctx_cards = context_pkg.list_profiles(
        session, org_id=_my_org_id(request), customer_id=customer_id
    )
    ctx_cards_by_ct = {c.content_type_id: context_pkg.profile_row(c) for c in ctx_cards}
    # Connected-vendors panel (cross-org linking from the CUSTOMER side, 2026-08-08).
    # Admin-only, like /vendors itself — so the data is only assembled for an admin (a
    # non-admin never sees the panel, and pays no extra queries). `linked_vendors` are
    # ring-scoped views (a foreign link target discloses only name+status, no
    # connection/rate); `vendor_choices` is the OWN-ORG active-vendor dropdown (never an
    # enumeration of other orgs' vendors — a cross-org link is by id via the free-text
    # field), minus the already-linked ones.
    is_admin = (not security.auth_required()) or security.is_admin(me)
    linked_vendors, vendor_choices = [], []
    if is_admin:
        my_org = _my_org_id(request)
        linked_vendors = vendors.linked_vendor_views(session, customer_id, org_id=my_org)
        linked_vendor_ids = {v["id"] for v in linked_vendors}
        vendor_choices = [
            v for v in vendors.list_vendors(session, org_id=my_org) if v.id not in linked_vendor_ids
        ]
    return templates.TemplateResponse(
        request,
        "customer.html",
        {
            "request": request,
            "customer": customer,
            "data": data,
            "ctx_cards_by_ct": ctx_cards_by_ct,
            "linked_vendors": linked_vendors,
            "vendor_choices": vendor_choices,
            "wf_choices": wf_choices,
            "seg_choices": seg_choices,
            "cust_templates": cust_templates,
            "own_content_types": own_content_types,
            "ct_choices": ct_choices,
            "ct_summaries": ct_summaries,
            "providers": config.PROVIDERS,
            "can_write": (not security.auth_required()) or security.can_write(me),
            # Customer configuration (preferences, defaults, context cards, language
            # pairs): admin, manager or linguist, the same predicate as those POSTs.
            "can_edit": (not security.auth_required()) or security.can_configure_customer(me),
            # Content-type CRUD stays can_tune_ai (app/web/content_types.py): a manager
            # must not see Add/Delete buttons whose POST would 403.
            "can_edit_content_types": (not security.auth_required()) or security.can_tune_ai(me),
            # SPEC-rename-delete UI: gates the Delete-customer button — the
            # POST /customers/{id}/delete route itself is strictly is_admin. Also gates
            # the admin-only Connected-vendors panel (data assembled above only when set).
            "is_admin": is_admin,
            # AI opt-out (DPAI-08): manager or admin may switch AI off for the customer.
            "can_manage": (not security.auth_required()) or security.can_manage(me),
            "status_labels": ProjectStatus.LABELS,
            "all_locales": languages_catalog.all_locales(),
            "saved": request.query_params.get("saved"),
            "err": request.query_params.get("err"),
        },
    )


@router.post("/customers/{customer_id}/settings")
def customer_update_settings(
    customer_id: int,
    request: Request,
    notes: str = Form(""),
    primary_contact: str = Form(""),
    technical_contact: str = Form(""),
    default_provider: str = Form(""),
    default_workflow_id: str = Form(""),
    default_segmentation_profile_id: str = Form(""),
    default_content_type_id: str = Form(""),
    session: Session = Depends(get_session),
):
    """Save a customer's preferences (notes/contacts) + scoped defaults."""
    customer = session.get(Customer, customer_id)
    if not customer:
        return _missing_customer_or(request, RedirectResponse("/customers", status_code=303))
    blocked = _org_block(request, getattr(customer, "org_id", None))
    if blocked:
        return blocked
    blocked = _customer_block(request, customer_id)
    if blocked:
        return blocked
    me = security.session_user(request)
    if security.auth_required() and not security.can_configure_customer(me):
        return _error_page(request, 403, "Your role does not allow this action.")
    customer_service.update_customer_settings(
        session,
        customer_id,
        {
            "notes": notes,
            "primary_contact": primary_contact,
            "technical_contact": technical_contact,
        },
    )
    try:
        customer_service.set_customer_defaults(
            session,
            customer_id,
            provider=default_provider,
            workflow_id=default_workflow_id or "",
            segmentation_profile_id=default_segmentation_profile_id or "",
            content_type_id=default_content_type_id or "",
            user=me,
        )
    except content.MockEngineAdminOnlyError as exc:
        # #577: choosing the offline Mock engine is an administrator decision.
        return _error_page(request, 403, str(exc))
    except ValueError as exc:
        # Cross-customer default_workflow_id (item 2) — the picker never offers it,
        # but a forged POST is refused, not 500'd. Preferences above still saved.
        from urllib.parse import quote

        return RedirectResponse(f"/customers/{customer_id}?err=" + quote(str(exc)), status_code=303)
    audit.log(
        actor=_actor_email(request),
        action="customer.settings",
        target=f"customer {customer_id}",
        ip=_ip(request),
        customer_id=customer_id,
    )
    return RedirectResponse(f"/customers/{customer_id}?saved=1", status_code=303)


@router.post("/customers/{customer_id}/context-cards")
def customer_context_card_save(
    customer_id: int,
    request: Request,
    content_type_id: str = Form(""),
    domain: str = Form(""),
    content_kind: str = Form(""),
    audience: str = Form(""),
    formality: str = Form(""),
    tone: str = Form(""),
    notes: str = Form(""),
    char_limit: str = Form(""),
    session: Session = Depends(get_session),
):
    """Create/update a customer context card (SPEC-context-metadata phase 1) — the
    general card (content_type_id blank) or a per-content-type card. Upserts by exact
    scope (one card per scope). Empty content_type_id = the customer's general card."""
    customer = session.get(Customer, customer_id)
    if not customer:
        return _missing_customer_or(request, RedirectResponse("/customers", status_code=303))
    blocked = _org_block(request, getattr(customer, "org_id", None))
    if blocked:
        return blocked
    blocked = _customer_block(request, customer_id)
    if blocked:
        return blocked
    me = security.session_user(request)
    if security.auth_required() and not security.can_configure_customer(me):
        return _error_page(request, 403, "Your role does not allow this action.")
    from urllib.parse import quote

    from app.kernel import context_pkg

    # The service validates existence only; the web surface owns visibility.
    ctid = None
    if (content_type_id or "").strip():
        ctid = ids.coerce_id(content_type_id)
        if (
            ctid is None
            or content_types.visible(
                session, ctid, org_id=_my_org_id(request), customer_id=_my_customer_id(request)
            )
            is None
        ):
            return _error_page(request, 404, "Not found.")

    raw = {
        k: v
        for k, v in (
            ("domain", domain),
            ("content_kind", content_kind),
            ("audience", audience),
            ("formality", formality),
            ("notes", notes),
        )
        if (v or "").strip()
    }
    if (tone or "").strip():
        raw["tone"] = [t.strip() for t in tone.split(",") if t.strip()]
    if (char_limit or "").strip():
        raw["char_limit"] = char_limit
    try:
        context_pkg.upsert_profile(
            session,
            org_id=_my_org_id(request),
            customer_id=customer_id,
            content_type_id=ctid,
            context_raw=raw,
            actor=_actor_email(request),
        )
    except context_pkg.ContextError as exc:
        return RedirectResponse(f"/customers/{customer_id}?err=" + quote(str(exc)), status_code=303)
    audit.log(
        actor=_actor_email(request),
        action="customer.context_card",
        target=f"customer {customer_id}",
        ip=_ip(request),
        customer_id=customer_id,
    )
    return RedirectResponse(f"/customers/{customer_id}?saved=1", status_code=303)


@router.post("/context-cards/{card_id}/delete")
def context_card_delete(
    card_id: int, request: Request, back: str = Form(""), session: Session = Depends(get_session)
):
    """Delete a context card (SPEC-context-metadata phase 1). Org-scoped; redirects
    back to the referring page (customer 360 or settings)."""
    from app.kernel import context_pkg
    from app.web.form_errors import _safe_local_path

    back = _safe_local_path(back) or "/customers"
    prof = context_pkg.get_profile(session, card_id, org_id=_my_org_id(request))
    if prof is None:
        return RedirectResponse(back or "/customers", status_code=303)
    blocked = _org_block(request, getattr(prof, "org_id", None))
    if blocked:
        return blocked
    if prof.customer_id is not None:
        blocked = _customer_block(request, prof.customer_id)
        if blocked:
            return blocked
    me = security.session_user(request)
    # Cards can carry only a project or content-type scope. Check those references
    # as well as the direct customer field before permitting any deletion.
    if prof.project_id is not None:
        project = session.get(Project, prof.project_id)
        if project is None or not access.can_view_project(session, me, project):
            return _error_page(request, 404, "Not found.")
    if (
        prof.content_type_id is not None
        and content_types.visible(
            session,
            prof.content_type_id,
            org_id=_my_org_id(request),
            customer_id=_my_customer_id(request),
        )
        is None
    ):
        return _error_page(request, 404, "Not found.")
    if all(value is None for value in (prof.customer_id, prof.project_id, prof.content_type_id)):
        blocked = _customer_block(request, None)
        if blocked:
            return blocked
        if security.auth_required() and not security.is_admin(me):
            return _error_page(request, 403, "Your role does not allow this action.")
    if security.auth_required() and not security.can_configure_customer(me):
        return _error_page(request, 403, "Your role does not allow this action.")
    context_pkg.delete_profile(session, card_id, org_id=_my_org_id(request))
    audit.log(
        actor=_actor_email(request),
        action="customer.context_card_delete",
        target=f"card {card_id}",
        ip=_ip(request),
        customer_id=prof.customer_id,
    )
    return RedirectResponse(back or "/customers", status_code=303)


@router.post("/customers/{customer_id}/pairs")
def customer_add_pair(
    customer_id: int,
    request: Request,
    pair: str = Form(""),
    session: Session = Depends(get_session),
):
    """Add an explicit active language pair (src->tgt) to a customer."""
    customer = session.get(Customer, customer_id)
    if not customer:
        return _missing_customer_or(request, RedirectResponse("/customers", status_code=303))
    blocked = _org_block(request, getattr(customer, "org_id", None))
    if blocked:
        return blocked
    blocked = _customer_block(request, customer_id)
    if blocked:
        return blocked
    me = security.session_user(request)
    if security.auth_required() and not security.can_configure_customer(me):
        return _error_page(request, 403, "Your role does not allow this action.")
    from urllib.parse import quote

    try:
        customer_service.add_pair(session, customer_id, pair)
    except ValueError as exc:
        return RedirectResponse(f"/customers/{customer_id}?err=" + quote(str(exc)), status_code=303)
    audit.log(
        actor=_actor_email(request),
        action="customer.pair_add",
        target=f"customer {customer_id} {pair}",
        ip=_ip(request),
        customer_id=customer_id,
    )
    return RedirectResponse(f"/customers/{customer_id}?saved=1", status_code=303)


@router.post("/customers/{customer_id}/pairs/remove")
def customer_remove_pair(
    customer_id: int,
    request: Request,
    pair: str = Form(""),
    session: Session = Depends(get_session),
):
    """Remove an explicit active language pair from a customer."""
    customer = session.get(Customer, customer_id)
    if not customer:
        return _missing_customer_or(request, RedirectResponse("/customers", status_code=303))
    blocked = _org_block(request, getattr(customer, "org_id", None))
    if blocked:
        return blocked
    blocked = _customer_block(request, customer_id)
    if blocked:
        return blocked
    me = security.session_user(request)
    if security.auth_required() and not security.can_configure_customer(me):
        return _error_page(request, 403, "Your role does not allow this action.")
    customer_service.remove_pair(session, customer_id, pair)
    audit.log(
        actor=_actor_email(request),
        action="customer.pair_remove",
        target=f"customer {customer_id} {pair}",
        ip=_ip(request),
        customer_id=customer_id,
    )
    return RedirectResponse(f"/customers/{customer_id}?saved=1", status_code=303)


@router.post("/customers/{customer_id}/pairs/import")
async def customer_pairs_import(
    customer_id: int,
    request: Request,
    file: UploadFile = File(...),
    session: Session = Depends(get_session),
):
    """Bulk-import a customer's explicit active language pairs from a CSV upload
    (source_locale,target_locale columns). CLI-parity backlog item 2 -- ATOMIC
    (every row validated before any write) + IDEMPOTENT (already-present pairs
    skipped, reported via the redirect's query params). Same gate as the single
    add/remove routes (can_configure_customer) + the org/customer isolation rings."""
    customer = session.get(Customer, customer_id)
    if not customer:
        return _missing_customer_or(request, RedirectResponse("/customers", status_code=303))
    blocked = _org_block(request, getattr(customer, "org_id", None))
    if blocked:
        return blocked
    blocked = _customer_block(request, customer_id)
    if blocked:
        return blocked
    me = security.session_user(request)
    if security.auth_required() and not security.can_configure_customer(me):
        return _error_page(request, 403, "Your role does not allow this action.")
    from urllib.parse import quote

    data = await read_capped_upload(file, config.MAX_UPLOAD_BYTES)
    if data is None:
        return RedirectResponse(
            f"/customers/{customer_id}?err="
            + quote("file too large -- exceeds the server's upload size limit."),
            status_code=303,
        )
    try:
        # #599: the CSV parse runs on a bounded worker, never on the event loop.
        res = await offload_blocking(customer_service.import_pairs_csv, session, customer_id, data)
    except (WorkerBusy, customer_service.PairImportError, ValueError) as exc:
        return RedirectResponse(f"/customers/{customer_id}?err=" + quote(str(exc)), status_code=303)
    if res["added"]:
        audit.log(
            actor=_actor_email(request),
            action="customer.pairs_import",
            target=f"customer {customer_id}",
            detail=f"added {len(res['added'])}, skipped {len(res['skipped'])}",
            ip=_ip(request),
            customer_id=customer_id,
        )
    return RedirectResponse(
        f"/customers/{customer_id}?pairs_added={len(res['added'])}"
        f"&pairs_skipped={len(res['skipped'])}",
        status_code=303,
    )


# --- connected vendors (cross-org linking from the CUSTOMER side, 2026-08-08) ---
# WEB-ONLY, deliberately: the CLI (`vendor link-customer` / `unlink-customer`) and the
# /api routes already carry this capability from the customer side. What was missing was
# a WEB path that can INITIATE a cross-org link — the only link form lived on the vendor
# detail page, which 404-walls a foreign vendor. These two routes add that path on the
# Customer 360. No API/CLI surface is added here (parity is already met elsewhere).
#
# Gating mirrors the vendor routes (admin-only via _admin_block) AND the sibling customer
# POSTs (customer must exist + be in the caller's org/customer ring). Authorization for
# the link/unlink itself lives in the SERVICE (vendors.link_customer's customer-side buyer
# authority; unlink_customer's either-party rule), which also audits — so no vendor-org
# pre-check and no double-audit here. Non-enumeration is preserved: a foreign-customer or
# unknown-id refusal comes back as the service's generic message, never a distinct code.
@router.post("/customers/{customer_id}/link-vendor")
def customer_link_vendor(
    customer_id: int,
    request: Request,
    vendor_id: str = Form(""),
    vendor_pick: str = Form(""),
    session: Session = Depends(get_session),
):
    """Link a vendor to this customer from the Customer 360 "Connected vendors" panel.
    The panel posts EITHER a dropdown pick of one of the caller's OWN-org vendors
    (`vendor_pick`) OR a free-text vendor id for a cross-org link (`vendor_id`); the
    free-text id wins when both are present. Idempotent (a re-link is a clean saved=1)."""
    blocked = feature_block(request, "vendors")  # ffadmin Wave 3 (HIGH-1): flag first
    if blocked:
        return blocked
    customer = session.get(Customer, customer_id)
    if not customer:
        return _missing_customer_or(request, RedirectResponse("/customers", status_code=303))
    blocked = _org_block(request, getattr(customer, "org_id", None))
    if blocked:
        return blocked
    blocked = _customer_block(request, customer_id)
    if blocked:
        return blocked
    _me, blocked = _admin_block(request)
    if blocked:
        return blocked
    from urllib.parse import quote

    # F2: refuse a link onto an ARCHIVED customer (SPEC-customer-archive: a retired
    # customer is off every picker; the vendor-detail mirror enforces this via active_only).
    # The template also hides the connect form when archived, so this is the forged-POST guard.
    if customer.archived_at is not None:
        return RedirectResponse(
            f"/customers/{customer_id}?err="
            + quote("this customer is archived — unarchive it before connecting a vendor."),
            status_code=303,
        )
    # F1: the panel offers EITHER a dropdown pick OR a free-text id, never both. If both are
    # non-blank and DIFFER, refuse rather than silently letting the typed id win (the review
    # probe: a stray typed id overrode the selected vendor and still reported "Saved.").
    pick = (vendor_pick or "").strip()
    typed = (vendor_id or "").strip()
    if pick and typed and pick != typed:
        return RedirectResponse(
            f"/customers/{customer_id}?err="
            + quote("choose EITHER a vendor from the list OR a vendor id, not both."),
            status_code=303,
        )
    vid = typed or pick
    try:
        vendors.link_customer(
            session,
            vid,
            customer_id,
            org_id=_my_org_id(request),
            actor=_actor_email(request),
            ip=_ip(request),
        )
    except vendors.VendorError as exc:
        return RedirectResponse(f"/customers/{customer_id}?err=" + quote(str(exc)), status_code=303)
    return RedirectResponse(f"/customers/{customer_id}?saved=1", status_code=303)


@router.post("/customers/{customer_id}/unlink-vendor")
def customer_unlink_vendor(
    customer_id: int,
    request: Request,
    vendor_id: str = Form(""),
    session: Session = Depends(get_session),
):
    """Sever a vendor<->customer link from the Customer 360 panel. The service's
    either-party rule authorizes the CUSTOMER-side (buyer) admin here; an archived vendor
    is still unlinkable. A no-op unlink (already severed / unknown id) is non-enumerable —
    it just redirects saved=1, exactly like the vendor-detail unlink route."""
    blocked = feature_block(request, "vendors")  # ffadmin Wave 3 (HIGH-1): flag first
    if blocked:
        return blocked
    customer = session.get(Customer, customer_id)
    if not customer:
        return _missing_customer_or(request, RedirectResponse("/customers", status_code=303))
    blocked = _org_block(request, getattr(customer, "org_id", None))
    if blocked:
        return blocked
    blocked = _customer_block(request, customer_id)
    if blocked:
        return blocked
    _me, blocked = _admin_block(request)
    if blocked:
        return blocked
    vendors.unlink_customer(
        session,
        vendor_id,
        customer_id,
        org_id=_my_org_id(request),
        actor=_actor_email(request),
        ip=_ip(request),
    )
    return RedirectResponse(f"/customers/{customer_id}?saved=1", status_code=303)


# --- customers ---
@router.post("/customers")
def create_customer(
    request: Request, name: str = Form(...), session: Session = Depends(get_session)
):
    # R2-3 (Finding F): manage-role gated — a customer-bound "customer account"
    # user (translator/linguist/viewer, per CUSTOMER_BINDABLE_ROLES, never
    # admin/manager) must not be able to create org-level customer records.
    # Mirrors template_delete's manage-role gate above.
    me = security.session_user(request)
    if security.auth_required() and not security.can_manage(me):
        return _error_page(request, 403, "Creating customers requires a manager role.")
    # creator= stamps the new customer with the acting user's org when multi-org
    # is active (no-op / org_id NULL with the gate off — byte-identical to today).
    try:
        content.customer_create(session, name, creator=me, reject_duplicate=True)
    except ids.InvalidNameError as exc:
        return _error_page(request, 400, str(exc))
    except content.DuplicateNameError as exc:
        # #580: same refusal as /api (409) so a double-clicked form does not mint a twin.
        return _error_page(request, 409, str(exc))
    return RedirectResponse("/customers", status_code=303)


@router.post("/customers/{customer_id}/rename")
def customer_rename(
    customer_id: int,
    request: Request,
    name: str = Form(...),
    session: Session = Depends(get_session),
):
    """Rename a customer (SPEC-rename-delete). Gated like the customer settings update
    route (can_configure_customer: admin, manager or linguist; #475 raised it from
    can_write with its #284 sibling), plus the org + R2-3 customer isolation rings. A
    blank name redirects with a clean error."""
    customer = session.get(Customer, customer_id)
    if not customer:
        return _missing_customer_or(request, RedirectResponse("/customers", status_code=303))
    blocked = _org_block(request, getattr(customer, "org_id", None))
    if blocked:
        return blocked
    blocked = _customer_block(request, customer_id)
    if blocked:
        return blocked
    me = security.session_user(request)
    if security.auth_required() and not security.can_configure_customer(me):
        return _error_page(
            request, 403, "Renaming a customer is limited to admins, managers and linguists."
        )
    from urllib.parse import quote

    try:
        customer_service.rename_customer(
            session,
            customer_id,
            name,
            actor=_actor_email(request),
            ip=_ip(request),
            org_id=_my_org_id(request),
        )
    except content.DuplicateNameError as exc:
        # #617: same refusal as create (a rename may not take another customer's name).
        return _error_page(request, 409, str(exc))
    except ValueError as exc:
        return RedirectResponse(f"/customers/{customer_id}?err=" + quote(str(exc)), status_code=303)
    return RedirectResponse(f"/customers/{customer_id}?saved=1", status_code=303)


@router.post("/customers/{customer_id}/delete")
def customer_delete(customer_id: int, request: Request, session: Session = Depends(get_session)):
    """Delete a customer (SPEC-rename-delete). Admin-only (every new delete is strictly
    is_admin). FAILS CLOSED with an itemized count while anything references it (no
    cascade). Org + R2-3 isolation gated."""
    customer = session.get(Customer, customer_id)
    if not customer:
        return _missing_customer_or(request, RedirectResponse("/customers", status_code=303))
    blocked = _org_block(request, getattr(customer, "org_id", None))
    if blocked:
        return blocked
    blocked = _customer_block(request, customer_id)
    if blocked:
        return blocked
    _me, blocked = _admin_block(request)
    if blocked:
        return blocked
    from urllib.parse import quote

    try:
        customer_service.delete_customer(
            session,
            customer_id,
            actor=_actor_email(request),
            ip=_ip(request),
            org_id=_my_org_id(request),
        )
    except customer_service.CustomerDeleteBlocked as exc:
        return RedirectResponse(f"/customers/{customer_id}?err=" + quote(str(exc)), status_code=303)
    return RedirectResponse("/customers?deleted=1", status_code=303)


@router.post("/customers/{customer_id}/archive")
def customer_archive(customer_id: int, request: Request, session: Session = Depends(get_session)):
    """Archive (retire) a customer (SPEC-customer-archive). Admin-only (same posture as
    the new deletes). Retires the customer from every picker + the default list; its
    existing projects/connections/bound users keep working. Org + R2-3 isolation gated.
    Idempotent-safe (archiving an already-archived customer is a clean error)."""
    customer = session.get(Customer, customer_id)
    if not customer:
        return _missing_customer_or(request, RedirectResponse("/customers", status_code=303))
    blocked = _org_block(request, getattr(customer, "org_id", None))
    if blocked:
        return blocked
    blocked = _customer_block(request, customer_id)
    if blocked:
        return blocked
    _me, blocked = _admin_block(request)
    if blocked:
        return blocked
    from urllib.parse import quote

    try:
        customer_service.archive_customer(
            session,
            customer_id,
            actor=_actor_email(request),
            ip=_ip(request),
            org_id=_my_org_id(request),
        )
    except customer_service.CustomerArchiveError as exc:
        return RedirectResponse(f"/customers/{customer_id}?err=" + quote(str(exc)), status_code=303)
    return RedirectResponse(f"/customers/{customer_id}?saved=1", status_code=303)


@router.post("/customers/{customer_id}/ai-opt-out")
def customer_ai_opt_out(
    customer_id: int,
    request: Request,
    ai_opt_out: str = Form(""),
    session: Session = Depends(get_session),
):
    """AI opt-out for every project of this customer. Manager or admin.
    Org + customer isolation gated. Audited by the service."""
    from app.kernel import ai_optout

    customer = session.get(Customer, customer_id)
    if not customer:
        return _missing_customer_or(request, RedirectResponse("/customers", status_code=303))
    blocked = _org_block(request, getattr(customer, "org_id", None))
    if blocked:
        return blocked
    blocked = _customer_block(request, customer_id)
    if blocked:
        return blocked
    me = security.session_user(request)
    if security.auth_required() and not security.can_manage(me):
        return _error_page(
            request, 403, "Only an admin or manager can switch AI on or off for a customer."
        )
    value = ai_optout.parse_switch(ai_opt_out)
    if value is None:
        from urllib.parse import quote

        return RedirectResponse(
            f"/customers/{customer_id}?err=" + quote("Choose whether AI is on or off."),
            status_code=303,
        )
    result = ai_optout.set_customer(
        session,
        customer_id,
        value,
        actor=_actor_email(request),
        ip=_ip(request),
        org_id=_my_org_id(request),
    )
    if result is None:  # missing / cross-org -> 404, never a false "saved"
        return _error_page(request, 404, "Customer not found.")
    return RedirectResponse(f"/customers/{customer_id}?saved=1#ai-use", status_code=303)


@router.post("/customers/{customer_id}/unarchive")
def customer_unarchive(customer_id: int, request: Request, session: Session = Depends(get_session)):
    """Unarchive (reactivate) a customer (SPEC-customer-archive). Admin-only. Restores
    it to every picker. Org + R2-3 isolation gated. Idempotent-safe."""
    customer = session.get(Customer, customer_id)
    if not customer:
        return _missing_customer_or(request, RedirectResponse("/customers", status_code=303))
    blocked = _org_block(request, getattr(customer, "org_id", None))
    if blocked:
        return blocked
    blocked = _customer_block(request, customer_id)
    if blocked:
        return blocked
    _me, blocked = _admin_block(request)
    if blocked:
        return blocked
    from urllib.parse import quote

    try:
        customer_service.unarchive_customer(
            session,
            customer_id,
            actor=_actor_email(request),
            ip=_ip(request),
            org_id=_my_org_id(request),
        )
    except customer_service.CustomerArchiveError as exc:
        return RedirectResponse(f"/customers/{customer_id}?err=" + quote(str(exc)), status_code=303)
    return RedirectResponse(f"/customers/{customer_id}?saved=1", status_code=303)


# --- projects ---
@router.post("/projects")
def create_project(
    request: Request,
    name: str = Form(...),
    source_locale: str = Form("en-US"),
    target_locales: list[str] = Form([]),
    customer_id: str = Form(""),
    default_provider: str = Form(""),
    segmentation_profile_id: str = Form(""),
    content_type_id: str = Form(""),
    due_date: str = Form(""),
    provider_mode: str = Form("uniform"),
    # SPEC-key-based-projects 2.1: absent (the switch is off, or an older form/CLI
    # posts) means "document", so every existing caller is byte-identical.
    project_type: str = Form(""),
    # Phase 2 per-pair matrix: the popout posts one parallel pair of hidden/select
    # fields per target — pair_target[] = the target locale, pair_provider[] = the
    # chosen provider ("" = use the default). Zipped into the SRC:TGT=PROVIDER form
    # the shared service parses. Empty on a UNIFORM submit.
    pair_target: list[str] = Form([]),
    pair_provider: list[str] = Form([]),
    # Custom project fields (SPEC-project-automation part C): the dialog's script posts
    # the filled fields as one JSON object; validated before anything is written.
    custom_fields_json: str = Form(""),
    # Batch 13: an optional Trados project package (.sdlppx). Its .sdlproj names the
    # source and target languages (they replace the form's), and every target SDLXLIFF
    # becomes a document of the new project (same service as the CLI's
    # `project create --from-sdl-package` and POST /api/documents sdl_new_project=1).
    sdl_package: UploadFile | None = File(None),
    session: Session = Depends(get_session),
):
    me = security.session_user(request)
    if security.auth_required() and not security.can_write(me):
        return _error_page(request, 403, "Creating projects requires a write role.")
    from urllib.parse import quote

    from app.filters.base import InputLimitError
    from app.kernel import connected_projects as _cp
    from app.kernel import ingest as _ingest

    # Connected projects: the dialog's third type choice posts project_type=connected. It
    # means intake=connected with the default (Documents) shape, and it has no upload step.
    intake = ""
    if (project_type or "").strip().lower() == "connected":
        intake, project_type = "connected", ""
        if sdl_package is not None and (sdl_package.filename or "").strip():
            return RedirectResponse(
                "/projects?err=" + quote(_cp.UPLOAD_REFUSED) + "#new", status_code=303
            )

    pkg_data = None
    pkg_name = ""
    if sdl_package is not None and (sdl_package.filename or "").strip():
        pkg_name = sdl_package.filename or "package.sdlppx"
        cap = config.MAX_UPLOAD_BYTES
        pkg_data = sdl_package.file.read(cap + 1)
        if len(pkg_data) > cap:
            return RedirectResponse(
                "/projects?err=" + quote(f"File too large (limit {cap} bytes).") + "#new",
                status_code=303,
            )
        if not pkg_data:
            pkg_data = None
        else:
            try:
                pkg_info = _ingest.inspect_sdl_package(pkg_data, pkg_name)
            except InputLimitError as exc:
                return RedirectResponse(
                    "/projects?err=" + quote(str(exc)) + "#new", status_code=303
                )
            source_locale = pkg_info["source_locale"]
            target_locales = list(pkg_info["target_locales"])
            # The per-pair matrix was built against the form's languages, not the
            # package's: a package project starts with one provider for every pair.
            provider_mode, pair_target, pair_provider = "uniform", [], []
    # The picker posts one `target_locales` checkbox per selected code; a plain
    # text field (or the CLI-style comma list) posts a single value. Join them into
    # the comma/space string content.project_create parses either way — so both the
    # enhanced picker and a legacy free-text submit work unchanged.
    targets = ", ".join(x for x in target_locales if (x or "").strip())
    seg_profile = (segmentation_profile_id or "").strip()
    if pkg_data is not None and (project_type or "").strip() == ProjectType.KEY_BASED:
        return RedirectResponse(
            "/projects?err="
            + quote("A Trados package makes a document project; choose the Documents type.")
            + "#new",
            status_code=303,
        )

    cf_plan = []
    from app.kernel import custom_fields as _cf

    _cf_values = None
    if (custom_fields_json or "").strip():
        import json as _json

        try:
            _cf_values = _json.loads(custom_fields_json[:20000])
        except ValueError:
            _cf_values = None
        if not isinstance(_cf_values, dict):
            return RedirectResponse(
                "/projects?err=" + quote("The project fields could not be read.") + "#new",
                status_code=303,
            )
    if _cf_values is not None or _cf.list_defs(
        session, _my_org_id(request), _my_customer_id(request)
    ):
        try:
            cf_plan = _cf.validate_for_create(
                session,
                _my_org_id(request),
                _my_customer_id(request) or ids.coerce_id(customer_id),
                _cf_values,
            )
        except _cf.FieldError as exc:
            return RedirectResponse("/projects?err=" + quote(str(exc)) + "#new", status_code=303)

    if seg_profile:
        # #5: validate the profile id BEFORE project_create commits anything — a
        # bad/foreign profile must fail the WHOLE create (error surfaced, no
        # project row), not silently swallow the failure (the former `except:
        # pass`) while leaving an orphaned project behind on inherit.
        try:
            segprof.validate_for_create(
                session, seg_profile, customer_id, org_id=_my_org_id(request)
            )
        except segprof.SegProfileError as exc:
            return RedirectResponse("/projects?err=" + quote(str(exc)) + "#new", status_code=303)
    # Phase 2: zip the parallel pair arrays into SRC:TGT=PROVIDER strings (source =
    # the project's source locale). Only pairs with a non-blank provider carry an
    # override; the service canonicalizes + validates. Ignored under UNIFORM.
    # SPEC-key-based-projects: a KEY project needs at least one target locale to be
    # meaningful -- a key's BASE value lives on its per-locale value rows (spec 2.5),
    # so with no targets `set_base_value` refuses and the project can only hold keys
    # that can never hold a value. Refused HERE, at the create surface, with a message
    # that names the fix, rather than letting the user discover it one failed save at
    # a time. (The type selector renders only while the switch is on; the service
    # re-checks the switch itself, so a forged POST is refused there.)
    if (project_type or "").strip() == ProjectType.KEY_BASED and not targets.strip():
        return RedirectResponse(
            "/projects?err="
            + quote(
                "A key-based project needs at least one target "
                "language: key values are stored per locale."
            )
            + "#new",
            status_code=303,
        )
    src_for_pairs = (source_locale or "en-US").strip() or "en-US"
    pair_mapping = [
        f"{src_for_pairs}:{tgt}={prov}"
        for tgt, prov in zip(pair_target, pair_provider, strict=False)
        if (tgt or "").strip() and (prov or "").strip()
    ]
    try:
        project = content.project_create(
            session,
            name,
            source_locale=source_locale,
            target_locales=targets,
            customer_id=customer_id,
            default_provider=default_provider,
            provider_mode=provider_mode,
            pair_providers=pair_mapping,
            org_id=_my_org_id(request),
            created_by=_actor_email(request),
            # creator= stamps the project's org when multi-org is active (no-op with
            # the gate off — org_id stays NULL for the Default-org backfill).
            creator=security.session_user(request),
            # R2-3 (Finding E): a customer-bound caller may only create into their
            # OWN customer — an explicit customer_id posted for a FOREIGN customer
            # is refused (never silently attributed there), and a blank/own value
            # is force-stamped to the caller's own customer. No-op for internal
            # staff (_my_customer_id is None).
            bound_customer_id=_my_customer_id(request),
            # Feature A: optional due date (blank = none). A bad date is refused
            # with a clean flash, never a 500.
            due_date=due_date,
            # SPEC-content-types: optional content type (blank = the shared "General"
            # bucket, or the customer's default inherited in the service). A bad /
            # foreign id is refused with a clean flash (InvalidContentTypeError).
            content_type_id=content_type_id,
            # SPEC-key-based-projects 2.1 (the service validates + kill-switch gates).
            project_type=project_type,
            # Connected projects: "connected" or blank (= upload). Validated in the service.
            intake=intake,
            # 2026-07-14: a customer is REQUIRED on every user-facing create
            # seam. A blank customer_id fails with a clean flash (MissingCustomerError),
            # never a customer-less project. The dialog also hard-gates it (required
            # select) — this is the authoritative server-side enforcement.
            require_customer=True,
            # #580: a same-named active project of this customer is refused, not duplicated.
            reject_duplicate=True,
        )
    except content.MockEngineAdminOnlyError as exc:
        # #577: pinning a project to the offline Mock engine is admin-only.
        return _error_page(request, 403, str(exc))
    except (
        ids.InvalidNameError,
        content.DuplicateNameError,
        content.UnknownCustomerError,
        content.InvalidLocaleError,
        content.DueDateError,
        content.PairProviderError,
        content.InvalidContentTypeError,
        content.ArchivedCustomerError,
        content.MissingCustomerError,
        content.InvalidProjectTypeError,
        _cp.IntakeError,
    ) as exc:
        return RedirectResponse("/projects?err=" + quote(str(exc)) + "#new", status_code=303)
    except content.ForeignCustomerError as exc:
        return RedirectResponse("/projects?err=" + quote(str(exc)) + "#new", status_code=303)
    if project is None:
        return RedirectResponse("/", status_code=303)
    if seg_profile:
        # Already validated above; this just performs the assignment.
        segprof.assign_to_project(session, project.id, seg_profile)
    if cf_plan:
        try:
            _cf_changes = _cf.apply_plan(session, project, cf_plan, actor=_actor_email(request))
        except _cf.FieldError:
            _cf_changes = []
        if _cf_changes:
            audit.log(
                actor=_actor_email(request),
                action="field.set",
                target=f"project {project.id}",
                detail=_cf.audit_detail(_cf_changes),
                ip=_ip(request),
                customer_id=project.customer_id,
            )
    if pkg_data is not None:
        actor = _actor_email(request)
        try:
            _ingest.ingest_sdl_package(
                session, project, pkg_name, pkg_data, "segmented", created_by=actor
            )
        except Exception as exc:
            # All or nothing, like the CLI: a refused import removes the new project.
            session.rollback()
            try:
                content.delete_project(session, project.id, actor=actor)
            except Exception:
                _logger.warning("could not remove project %s after a failed import", project.id)
            if isinstance(exc, (InputLimitError, ValueError)):
                msg = f"Could not import the Trados package: {exc}"
            else:
                _logger.exception("Trados package import failed for %r", pkg_name)
                msg = "Could not import the Trados package: the file could not be read."
            return RedirectResponse("/projects?err=" + quote(msg) + "#new", status_code=303)
        return RedirectResponse(f"/projects/{project.id}#sdl-package", status_code=303)
    if intake == "connected":
        # Straight to the setup menu on the project page.
        return RedirectResponse(_cp.setup_url(project.id), status_code=303)
    return RedirectResponse(f"/projects/{project.id}", status_code=303)
