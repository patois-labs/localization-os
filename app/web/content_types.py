"""Content-type management (SPEC-content-types, phase 1) — the /content-types surface.

Create / rename / describe / delete first-class ContentType rows (per-customer domain
scoping objects). Two entry points share these routes:

  * the GLOBAL list at GET /content-types (NULL-customer types, managed alongside the
    other global assets) — customer_id blank on create;
  * the per-customer section on the Customer 360 page (customer.html), whose create
    form posts here with customer_id set + a `back` redirect target.

Scoping (repo law): the list + every mutation honours the same org (multi-org) + R2-3
customer confinement as the rest of the app — a type is fetched via
content_types.visible(...) so a foreign org's/customer's type 404s, and a
customer-bound caller may only ever create/edit in its OWN customer scope.

Role gate (the maintainer's RBAC ruling 2026-07-06): ContentType CRUD = can_tune_ai (admin or
linguist) — linguists own language assets, matching the /glossaries + Language Set
gates. (Assigning a content type to a PROJECT is a project SETTING and is gated
can_write alongside the status/due routes — see app/web/projects.py.)

Documented in: docs/architecture/content-and-projects.md
"""

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse
from sqlmodel import Session, select

from app.db import get_session
from app.kernel import access, audit, ids, oidc, security
from app.kernel import content_types as ct
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

router = APIRouter()


def _customers(request, session):
    """Org-scoped customer list for the create-form scope dropdown; a customer-bound
    caller only ever sees its OWN customer (R2-3)."""
    from app.kernel import customers as customer_service

    me = security.session_user(request)
    # SPEC-customer-archive: scope PICKER — archived customers filtered out.
    q = customer_service.active_only(access.org_filter(select(Customer), Customer, me)).order_by(
        Customer.name
    )
    my_cust = access.current_customer_id(me)
    if my_cust is not None:
        q = q.where(Customer.id == my_cust)
    return session.exec(q).all()


def _valid_customer_id(session, request, raw):
    """A submitted customer_id, only if it's a customer the caller may scope to.

    R2-3: a customer-BOUND caller is FORCED to its own customer (a blank / foreign
    value can never create a global or foreign-customer type). An internal-staff
    caller may pass a blank (global -- fine) or any of its org's ACTIVE customers
    (fine); any other supplied id -- most notably an ARCHIVED one -- RAISES a clean
    error (T2-02) instead of silently falling back to global. Mirrors
    content.project_create's ArchivedCustomerError contract."""
    my_cust = _my_customer_id(request)
    if my_cust is not None:
        return my_cust  # bound caller: always their own customer, never global/foreign
    raw = (raw or "").strip()
    if not raw:
        return None
    # Single app-wide id law: a malformed id (a Unicode digit int() rejects, or a
    # value SQLite rejects at the bind layer) is refused as a clean ContentTypeError,
    # exactly as a non-digit id already was, never a 500.
    cid = ids.coerce_id(raw)
    if cid is None:
        raise ct.ContentTypeError(f"customer_id {raw!r} is not a valid id.")
    ok = {c.id for c in _customers(request, session)}
    if cid in ok:
        return cid
    # NON-DISCLOSURE (adversarial-review fix): gate the archived-specific refusal
    # behind a SAME-ORG check so a foreign-org id (archived/active/nonexistent alike)
    # always falls through to the identical generic "not available" response -- no
    # existence/name oracle. The archived message echoes the ID ONLY (never
    # cust.name), matching content.project_create's ArchivedCustomerError contract.
    # See app/web/glossaries.py::_valid_customer_id for the full rationale.
    from app.kernel import customers as customer_service

    me = security.session_user(request)
    cust = session.get(Customer, cid)
    if (
        cust is not None
        and access.same_org(me, getattr(cust, "org_id", None))
        and not customer_service.is_active(cust)
    ):
        raise ct.ContentTypeError(f"customer {cid} is archived and cannot be selected.")
    raise ct.ContentTypeError(f"customer {cid} is not available in this scope.")


def _visible_or_404(request, session, ct_id):
    ctype = ct.visible(
        session, ct_id, org_id=_my_org_id(request), customer_id=_my_customer_id(request)
    )
    if ctype is None:
        return None, _error_page(request, 404, "Content type not found.")
    return ctype, None


def _back(request, fallback="/content-types"):
    """The redirect target after a mutation — the form's `back` field (a customer 360
    page for the per-customer section) if it is a safe local path, else the fallback."""
    dest = (request.query_params.get("back") or "").strip()
    # The shared local-target test (security review 2026-10 web-01): no '//', backslash,
    # scheme or control character (browsers drop TAB/LF, so '/<TAB>/host' is '//host').
    if dest and not oidc.is_unsafe_local_target(dest):
        return dest
    return fallback


# --- global list -------------------------------------------------------------
@router.get("/content-types")
def content_types_index(request: Request, session: Session = Depends(get_session)):
    me = security.session_user(request)
    # The global management page shows GLOBAL (NULL-customer) types; per-customer
    # types are managed on the Customer 360 page. A staff caller sees the org's
    # globals; a customer-bound caller has no globals to manage (its section lives
    # on its own 360 page) so it sees an empty global list.
    types = [
        t
        for t in ct.list_types(
            session, org_id=_my_org_id(request), customer_id=_my_customer_id(request)
        )
        if t.customer_id is None
    ]
    rows = [{"ct": t, "summary": ct.to_dict(session, t)} for t in types]
    return templates.TemplateResponse(
        request,
        "content_types.html",
        {
            "request": request,
            "rows": rows,
            "can_edit": (not security.auth_required()) or security.can_tune_ai(me),
        },
    )


# --- create (shared by the global page + the Customer 360 section) -----------
@router.post("/content-types")
def content_type_create(
    request: Request,
    name: str = Form(""),
    description: str = Form(""),
    customer_id: str = Form(""),
    back: str = Form(""),
    session: Session = Depends(get_session),
):
    _, blocked = _ai_block(request)
    if blocked:
        return blocked
    from urllib.parse import quote

    dest = (
        back
        if (back or "").startswith("/") and not (back or "").startswith("//")
        else "/content-types"
    )
    try:
        c = ct.create(
            session,
            name,
            description=description,
            org_id=_my_org_id(request),
            customer_id=_valid_customer_id(session, request, customer_id),
            created_by=_actor_email(request),
        )
    except (ids.InvalidNameError, ct.ContentTypeError) as exc:
        return RedirectResponse(f"{dest}?err=" + quote(str(exc)), status_code=303)
    audit.log(
        actor=_actor_email(request),
        action="content_type.create",
        target=f"content_type {c.id} '{c.name}'",
        ip=_ip(request),
        customer_id=c.customer_id,
    )
    return RedirectResponse(f"{dest}?saved=1", status_code=303)


@router.post("/content-types/{ct_id}/rename")
def content_type_rename(
    ct_id: int,
    request: Request,
    name: str = Form(""),
    back: str = Form(""),
    session: Session = Depends(get_session),
):
    _, blocked = _ai_block(request)
    if blocked:
        return blocked
    ctype, blocked = _visible_or_404(request, session, ct_id)
    if blocked:
        return blocked
    old_name = ctype.name  # captured BEFORE rename (same session -> same object mutates)
    from urllib.parse import quote

    dest = (
        back
        if (back or "").startswith("/") and not (back or "").startswith("//")
        else "/content-types"
    )
    try:
        c = ct.rename(session, ct_id, name)
    except ct.ContentTypeError as exc:
        return RedirectResponse(f"{dest}?err=" + quote(str(exc)), status_code=303)
    audit.log(
        actor=_actor_email(request),
        action="content_type.rename",
        target=f"content_type {ct_id}",
        ip=_ip(request),
        detail=f"{old_name!r} -> {c.name!r}",
        customer_id=ctype.customer_id,
    )
    return RedirectResponse(f"{dest}?saved=1", status_code=303)


@router.post("/content-types/{ct_id}/describe")
def content_type_describe(
    ct_id: int,
    request: Request,
    description: str = Form(""),
    back: str = Form(""),
    session: Session = Depends(get_session),
):
    _, blocked = _ai_block(request)
    if blocked:
        return blocked
    _ctype, blocked = _visible_or_404(request, session, ct_id)
    if blocked:
        return blocked
    dest = (
        back
        if (back or "").startswith("/") and not (back or "").startswith("//")
        else "/content-types"
    )
    ct.set_description(session, ct_id, description)
    return RedirectResponse(f"{dest}?saved=1", status_code=303)


@router.post("/content-types/{ct_id}/delete")
def content_type_delete(
    ct_id: int, request: Request, back: str = Form(""), session: Session = Depends(get_session)
):
    _, blocked = _ai_block(request)
    if blocked:
        return blocked
    ctype, blocked = _visible_or_404(request, session, ct_id)
    if blocked:
        return blocked
    from urllib.parse import quote

    dest = (
        back
        if (back or "").startswith("/") and not (back or "").startswith("//")
        else "/content-types"
    )
    try:
        ct.delete(session, ct_id)  # FAILS CLOSED while referenced
    except ct.ContentTypeError as exc:
        return RedirectResponse(f"{dest}?err=" + quote(str(exc)), status_code=303)
    audit.log(
        actor=_actor_email(request),
        action="content_type.delete",
        target=f"content_type {ct_id}",
        ip=_ip(request),
        customer_id=ctype.customer_id,
    )
    return RedirectResponse(f"{dest}?saved=1", status_code=303)
