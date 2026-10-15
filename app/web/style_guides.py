"""The dedicated Style Guide module (SPEC-workflows-redesign Track B, B2) — the
/style-guides sidebar tab.

CRUD + approval for the flagship new asset: a first-class, approvable, customer-scoped
long-form style guide packaged into translation prompts (verification of adherence is
B3, a separate build). Mirrors the /glossaries module's shape:

Scoping (repo law): the list + every detail action honours the same org (multi-org) +
R2-3 customer confinement as the rest of the app — a guide is fetched via
style_guides.visible(...) so a foreign org's/customer's guide 404s. A guide MAY be
org-global (customer_id NULL), unlike the customer-first glossary policy: a style guide
legitimately applies org-wide.

Role gates (documented choice, mirroring /glossaries):
  * CONTENT editing (create/rename/body/scope/delete) — can_tune_ai (admin or linguist):
    style guides are the linguist's home turf, matching the glossary + workflow-step
    editor gates.
  * APPROVAL (approve/unapprove) — can_tune_ai, same gate (per the maintainer's glossary ruling:
    linguists own the full asset lifecycle; no separate manager sign-off).

NO rename/delete fail-closed-on-references guard is needed here (unlike content types):
nothing references a style guide BY ID at run time — resolution is by SCOPE — so a
delete simply removes that scope's guidance from future resolutions. Documented in
style_guides.delete.

Documented in: docs/architecture/style-guides.md
"""

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse
from sqlmodel import Session, select

from app.db import get_session
from app.kernel import (
    access,
    audit,
    ids,
    languages_catalog,
    security,
)
from app.kernel import (
    content_types as ctsvc,
)
from app.kernel import (
    style_guides as sg,
)
from app.models import Customer
from app.web.deps import (
    _actor_email,
    _ai_block,
    _error_page,
    _ip,
    _my_customer_id,
    _my_org_id,
    templates,
)
from app.web.form_errors import redirect_with_error

router = APIRouter()


def _customers(request, session):
    """Org-scoped ACTIVE customer list for the create-form scope dropdown; a
    customer-bound caller only ever sees its OWN customer (R2-3)."""
    from app.kernel import customers as customer_service

    me = security.session_user(request)
    q = customer_service.active_only(access.org_filter(select(Customer), Customer, me)).order_by(
        Customer.name
    )
    my_cust = access.current_customer_id(me)
    if my_cust is not None:
        q = q.where(Customer.id == my_cust)
    return session.exec(q).all()


def _valid_customer_id(session, request, raw):
    """A submitted customer_id: None if blank (org-global — allowed for style guides),
    the id if it names an ACTIVE customer the caller may scope to, else RAISE. Mirrors
    content_types._valid_customer_id (a bound caller is forced to its own; an archived /
    foreign id is a clean error, never a silent scope change)."""
    my_cust = _my_customer_id(request)
    if my_cust is not None:
        return my_cust
    raw = (raw or "").strip()
    if not raw:
        return None
    # coerce_id absorbs the malformed shapes (a Unicode digit int() rejects, or a
    # >2**63 value SQLite overflows) into the SAME clean StyleGuideError, never a 500.
    cid = ids.coerce_id(raw)
    if cid is None:
        raise sg.StyleGuideError(f"customer_id {raw!r} is not a valid id.")
    ok = {c.id for c in _customers(request, session)}
    if cid in ok:
        return cid
    # NON-DISCLOSURE (mirrors glossaries/content_types): gate the archived-specific
    # refusal behind a same-org check so a foreign-org id never becomes an oracle.
    from app.kernel import customers as customer_service

    me = security.session_user(request)
    cust = session.get(Customer, cid)
    if (
        cust is not None
        and access.same_org(me, getattr(cust, "org_id", None))
        and not customer_service.is_active(cust)
    ):
        raise sg.StyleGuideError(f"customer {cid} is archived and cannot be selected.")
    raise sg.StyleGuideError(f"customer {cid} is not available in this scope.")


def _visible_or_404(request, session, guide_id, write=False):
    """Read seam (sg.visible: org + customer ring, org-global guides visible to all).
    `write=True` (#468) adds the WRITE ring every mutating route uses: an org-global
    guide is read-only for a customer-bound user (403, the shared message)."""
    g = sg.visible(
        session, guide_id, org_id=_my_org_id(request), customer_id=_my_customer_id(request)
    )
    if g is None:
        return None, _error_page(request, 404, "Style guide not found.")
    if write and access.customer_write_blocks(
        _my_customer_id(request), getattr(g, "customer_id", None)
    ):
        return None, _error_page(request, 403, access.SHARED_ASSET_READ_ONLY)
    return g, None


# --- list + create -----------------------------------------------------------
@router.get("/style-guides")
def style_guides_index(request: Request, session: Session = Depends(get_session)):
    me = security.session_user(request)
    org = _my_org_id(request)
    my_cust = _my_customer_id(request)
    guides = sg.list_guides(session, org_id=org, customer_id=my_cust)
    rows = [{"g": g, "summary": sg.to_dict(session, g, include_body=False)} for g in guides]
    return templates.TemplateResponse(
        request,
        "style_guides.html",
        {
            "request": request,
            "rows": rows,
            "customers": _customers(request, session),
            "customer_bound": my_cust is not None,
            "can_edit": (not security.auth_required()) or security.can_tune_ai(me),
            "content_types": ctsvc.list_types(session, org_id=org, customer_id=my_cust),
            "locales": languages_catalog.enabled_locales(),
            "label_for": languages_catalog.label_for,
        },
    )


@router.post("/style-guides")
def style_guide_create(
    request: Request,
    name: str = Form(""),
    customer_id: str = Form(""),
    content_type_id: str = Form(""),
    source_locale: str = Form(""),
    target_locale: str = Form(""),
    body: str = Form(""),
    session: Session = Depends(get_session),
):
    _, blocked = _ai_block(request)
    if blocked:
        return blocked
    try:
        cust = _valid_customer_id(session, request, customer_id)
        g = sg.create(
            session,
            name,
            org_id=_my_org_id(request),
            customer_id=cust,
            content_type_id=content_type_id,
            source_locale=source_locale,
            target_locale=target_locale,
            body=body,
            created_by=_actor_email(request),
        )
    except sg.StyleGuideError as exc:
        return redirect_with_error("/style-guides", str(exc))
    audit.log(
        actor=_actor_email(request),
        action="style_guide.create",
        target=f"style_guide {g.id} '{g.name}'",
        ip=_ip(request),
        customer_id=g.customer_id,
    )
    return RedirectResponse(f"/style-guides/{g.id}", status_code=303)


# --- detail ------------------------------------------------------------------
@router.get("/style-guides/{guide_id}")
def style_guide_detail(guide_id: int, request: Request, session: Session = Depends(get_session)):
    g, blocked = _visible_or_404(request, session, guide_id)
    if blocked:
        return blocked
    me = security.session_user(request)
    cust = session.get(Customer, g.customer_id) if g.customer_id else None
    shared_read_only = access.customer_write_blocks(_my_customer_id(request), g.customer_id)
    return templates.TemplateResponse(
        request,
        "style_guide_detail.html",
        {
            "request": request,
            "g": g,
            "customer": cust,
            "summary": sg.to_dict(session, g),
            # rendered preview of exactly what reaches the prompt (fenced, capped, sanitized)
            "rendered": sg.render_style_guide(g),
            "customers": _customers(request, session),
            "content_types": ctsvc.options_for_project(
                session, org_id=_my_org_id(request), customer_id=g.customer_id
            ),
            # #468: a customer account sees an org-global guide read-only.
            "can_edit": ((not security.auth_required()) or security.can_tune_ai(me))
            and not shared_read_only,
            "can_approve": ((not security.auth_required()) or security.can_tune_ai(me))
            and not shared_read_only,
            "locales": languages_catalog.enabled_locales(),
            "label_for": languages_catalog.label_for,
        },
    )


@router.post("/style-guides/{guide_id}/rename")
def style_guide_rename(
    guide_id: int, request: Request, name: str = Form(""), session: Session = Depends(get_session)
):
    _, blocked = _ai_block(request)
    if blocked:
        return blocked
    _g, blocked = _visible_or_404(request, session, guide_id, write=True)
    if blocked:
        return blocked
    try:
        sg.rename(session, guide_id, name)
    except sg.StyleGuideError as exc:
        return redirect_with_error(f"/style-guides/{guide_id}", str(exc))
    return RedirectResponse(f"/style-guides/{guide_id}", status_code=303)


@router.post("/style-guides/{guide_id}/body")
def style_guide_set_body(
    guide_id: int, request: Request, body: str = Form(""), session: Session = Depends(get_session)
):
    """Replace the long-form body (reverts an approved guide to draft — content edit)."""
    _, blocked = _ai_block(request)
    if blocked:
        return blocked
    _g, blocked = _visible_or_404(request, session, guide_id, write=True)
    if blocked:
        return blocked
    sg.set_body(session, guide_id, body)
    audit.log(
        actor=_actor_email(request),
        action="style_guide.body_set",
        target=f"style_guide {guide_id}",
        ip=_ip(request),
    )
    return RedirectResponse(f"/style-guides/{guide_id}", status_code=303)


@router.post("/style-guides/{guide_id}/scope")
def style_guide_set_scope(
    guide_id: int,
    request: Request,
    content_type_id: str = Form(""),
    source_locale: str = Form(""),
    target_locale: str = Form(""),
    session: Session = Depends(get_session),
):
    """Edit content type + language pair (reverts approval — scope edit). The customer
    is NOT editable here (customer scope is set at create; changing an asset's owning
    customer would break provenance, mirroring the glossary from-NULL-only rule)."""
    _, blocked = _ai_block(request)
    if blocked:
        return blocked
    _g, blocked = _visible_or_404(request, session, guide_id, write=True)
    if blocked:
        return blocked
    try:
        sg.set_scope(
            session,
            guide_id,
            content_type_id=content_type_id,
            source_locale=source_locale,
            target_locale=target_locale,
            change_content_type=True,
            change_pair=True,
        )
    except sg.StyleGuideError as exc:
        return redirect_with_error(f"/style-guides/{guide_id}", str(exc))
    audit.log(
        actor=_actor_email(request),
        action="style_guide.scope_set",
        target=f"style_guide {guide_id}",
        ip=_ip(request),
    )
    return RedirectResponse(f"/style-guides/{guide_id}", status_code=303)


@router.post("/style-guides/{guide_id}/delete")
def style_guide_delete(guide_id: int, request: Request, session: Session = Depends(get_session)):
    _, blocked = _ai_block(request)
    if blocked:
        return blocked
    _g, blocked = _visible_or_404(request, session, guide_id, write=True)
    if blocked:
        return blocked
    sg.delete(session, guide_id)
    audit.log(
        actor=_actor_email(request),
        action="style_guide.delete",
        target=f"style_guide {guide_id}",
        ip=_ip(request),
    )
    return RedirectResponse("/style-guides", status_code=303)


# --- approval (can_tune_ai) --------------------------------------------------
@router.post("/style-guides/{guide_id}/approve")
def style_guide_approve(guide_id: int, request: Request, session: Session = Depends(get_session)):
    _, blocked = _ai_block(request)
    if blocked:
        return blocked
    _g, blocked = _visible_or_404(request, session, guide_id, write=True)
    if blocked:
        return blocked
    sg.approve(session, guide_id, approver_email=_actor_email(request))
    audit.log(
        actor=_actor_email(request),
        action="style_guide.approve",
        target=f"style_guide {guide_id}",
        ip=_ip(request),
    )
    return RedirectResponse(f"/style-guides/{guide_id}", status_code=303)


@router.post("/style-guides/{guide_id}/unapprove")
def style_guide_unapprove(guide_id: int, request: Request, session: Session = Depends(get_session)):
    _, blocked = _ai_block(request)
    if blocked:
        return blocked
    _g, blocked = _visible_or_404(request, session, guide_id, write=True)
    if blocked:
        return blocked
    sg.unapprove(session, guide_id)
    audit.log(
        actor=_actor_email(request),
        action="style_guide.unapprove",
        target=f"style_guide {guide_id}",
        ip=_ip(request),
    )
    return RedirectResponse(f"/style-guides/{guide_id}", status_code=303)
