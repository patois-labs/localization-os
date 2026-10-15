"""The Translation Memory MANAGEMENT module — the /tm sidebar tab.

Browse the saved segment TM (filter by language pair / project / text / date /
usage), see the NEW usage stats (use_count + last_used_at), and run lifecycle
delete (single entry + bulk-by-filter with a count-first confirm). A management
surface over the EXISTING SegmentTM rows — no new container, no approval lifecycle
(that is the glossary MODULE's shape; TM's ask is browse/usage/lifecycle).

Scoping (repo law): SegmentTM has no org/customer column of its own, so visibility
is resolved through project_id -> Project via access.visible_project_ids (the same
seam Reports v1 uses): None = unrestricted (single-tenant / see-all staff), a list =
confine to exactly those project ids (org + membership + R2-3 customer confinement).
Every list/stats/detail/delete query passes that scope set so a foreign org's /
customer's TM is never listed and a foreign entry id 404s.

Role gates (documented choice): browsing is READ (everyone who can see the page).
DELETE (single + bulk) is gated on can_tune_ai (admin or linguist) — per the maintainer,
managing TM (like glossaries) is the linguist's home turf and should not require
manager sign-off. (Previously gated on can_manage; widened 2026-07 so linguists
can delete without a manager.)

Documented in: docs/architecture/tm-and-glossary.md
"""

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse
from sqlmodel import Session

from app.db import get_session
from app.kernel import access, audit, languages_catalog, security, tm_module
from app.web.deps import _actor_email, _error_page, _ip, templates

router = APIRouter()


# --- scope + gates -----------------------------------------------------------
def _visible_pids(request, session):
    """The project-id scope set for the current web user (None = unrestricted).

    None means see-everything (single-tenant / see-all staff with no customer bind);
    a concrete list confines every TM query to those projects (org + membership +
    R2-3 customer). Passed straight to tm_module's visible_pids= param."""
    me = security.session_user(request)
    return access.visible_project_ids(session, me)


def _no_filters(filters: dict) -> bool:
    """True when every TM filter is at its unset default (no source/target/project/
    query/date/usage bound, "unused" off) -- i.e. a bulk-delete call with these
    filters would hit the caller's ENTIRE visible TM."""
    return all(v in (None, False) for v in filters.values())


def _manage_gate(request):
    """Delete gate (can_tune_ai: admin or linguist). Returns (me, blocked).

    Per the maintainer: TM lifecycle management is the linguist's home turf, same as
    glossary content editing -- deleting TM entries should not require manager
    sign-off. Previously gated on can_manage (>= manager); widened to
    can_tune_ai so linguists (and admins) can delete without a manager."""
    me = security.session_user(request)
    if security.auth_required() and not security.can_tune_ai(me):
        return me, _error_page(
            request, 403, "Deleting translation-memory entries is limited to linguists and admins."
        )
    return me, None


def _parse_date(raw):
    """A YYYY-MM-DD filter value -> that day's midnight UTC (naive), or None. Invalid
    input is ignored (treated as no filter) rather than erroring the page."""
    raw = (raw or "").strip()
    if not raw:
        return None
    try:
        d = datetime.strptime(raw, "%Y-%m-%d")
        return d.replace(tzinfo=timezone.utc).replace(tzinfo=None)
    except ValueError:
        return None


def _int_or_none(raw):
    raw = (raw or "").strip()
    if not raw:
        return None
    try:
        n = int(raw)
        return n if n >= 0 else None
    except ValueError:
        return None


def _content_type_filter(raw):
    """Normalize a content-type filter query value (SPEC-content-types phase 3).

    Blank/None = no filter (every content type). The literal "__none__" = the General
    (NULL-content-type) bucket. A digit string = that content type id. Passed straight
    to tm_module._apply_filters (which understands the "__none__" sentinel)."""
    raw = (raw or "").strip()
    if not raw:
        return None
    if raw == "__none__":
        return "__none__"
    return raw if raw.isdigit() else None


def _filters_from_query(request):
    """Build the tm_module filter kwargs from the request query params (shared by the
    list page + the bulk-delete count/confirm so they always match)."""
    qp = request.query_params
    return {
        "source_locale": (qp.get("source_locale") or "").strip() or None,
        "target_locale": (qp.get("target_locale") or "").strip() or None,
        "project_id": _int_or_none(qp.get("project_id")),
        "content_type_id": _content_type_filter(qp.get("content_type_id")),
        "q": (qp.get("q") or "").strip() or None,
        "created_before": _parse_date(qp.get("created_before")),
        "created_after": _parse_date(qp.get("created_after")),
        "min_use": _int_or_none(qp.get("min_use")),
        "max_use": _int_or_none(qp.get("max_use")),
        "unused": (qp.get("unused") or "") in ("1", "true", "yes", "on"),
    }


# --- list + stats page -------------------------------------------------------
@router.get("/tm")
def tm_index(request: Request, session: Session = Depends(get_session)):
    """Customer-FIRST TM browse (2026-07-14: "All TM should be broken down by
    customer... there is NO global translation memory"). A customer picker (?customer=,
    server-side, no-JS-safe) drives the page:
      * a customer-BOUND user (R2-3) has their customer FORCED (no picker);
      * with exactly ONE visible customer it is auto-selected;
      * with none selected the page shows the picker + a short empty state, NEVER an
        all-customers TM dump.
    TM rows scope through their project's customer_id (base visible-project set ∩ the
    chosen customer's projects). This is BROWSE/organization only — TM matching/lookup
    during translation is untouched."""
    me = security.session_user(request)
    base_pids = _visible_pids(request, session)
    org_id = access.current_org_id(me)
    bound_cid = access.current_customer_id(me)

    qp = request.query_params
    customers = tm_module.browsable_customers(session, org_id=org_id, bound_customer_id=bound_cid)
    selected = tm_module.resolve_browse_customer(customers, qp.get("customer"))
    # A bound user's customer is always forced (their browsable set is exactly [their
    # customer], so resolve_browse_customer auto-selects it) and the picker is hidden.
    show_picker = bound_cid is None and len(customers) > 1

    from app.kernel import content_types

    ct_names_org = access.current_org_id(me)

    if selected is None:
        # No customer resolved -> picker + empty state. NEVER dump all customers' TM.
        return templates.TemplateResponse(
            request,
            "tm.html",
            {
                "request": request,
                "selected_customer": None,
                "customers": customers,
                "show_picker": show_picker,
                "no_customers": len(customers) == 0,
                "can_delete": (not security.auth_required()) or security.can_tune_ai(me),
                "label_for": languages_catalog.label_for,
            },
        )

    # Scope every TM query to the chosen customer's projects (∩ the caller's base ring).
    pids = tm_module.scoped_pids_for_customer(session, base_pids, selected.id)
    filters = _filters_from_query(request)
    try:
        page = max(1, int(qp.get("page") or 1))
    except ValueError:
        page = 1
    order = (qp.get("order") or "recent").strip()
    per_page = 50

    result = tm_module.list_entries(
        session, page=page, per_page=per_page, order=order, visible_pids=pids, **filters
    )
    summary = tm_module.stats(session, visible_pids=pids, top_n=5)
    pairs = tm_module.distinct_pairs(session, visible_pids=pids)
    # De-duplicated per-axis locale lists — the source/target filter dropdowns build
    # from THESE (one option per locale), never from `pairs` (which repeats a locale
    # once per pair — the dropdown-dup bug).
    source_locales = tm_module.distinct_source_locales(session, visible_pids=pids)
    target_locales = tm_module.distinct_target_locales(session, visible_pids=pids)

    # SPEC-content-types phase 3: the content-type filter dropdown source. Scoped to the
    # caller's org + the SELECTED customer, so the type list matches the browsed customer.
    cts = content_types.list_types(session, org_id=ct_names_org, customer_id=selected.id)
    ct_names = {t.id: t.name for t in cts}

    return templates.TemplateResponse(
        request,
        "tm.html",
        {
            "request": request,
            "result": result,
            "summary": summary,
            "pairs": pairs,
            "source_locales": source_locales,
            "target_locales": target_locales,
            "filters": filters,
            "order": order,
            "raw_query": dict(qp),
            "content_types": cts,
            "ct_names": ct_names,
            "selected_customer": selected,
            "customers": customers,
            "show_picker": show_picker,
            "can_delete": (not security.auth_required()) or security.can_tune_ai(me),
            "label_for": languages_catalog.label_for,
        },
    )


# --- entry detail ------------------------------------------------------------
@router.get("/tm/{entry_id}")
def tm_detail(entry_id: int, request: Request, session: Session = Depends(get_session)):
    me = security.session_user(request)
    pids = _visible_pids(request, session)
    e = tm_module.get_entry(session, entry_id, visible_pids=pids)
    if e is None:
        return _error_page(request, 404, "Translation-memory entry not found.")
    can_manage = (not security.auth_required()) or security.can_tune_ai(me)
    return templates.TemplateResponse(
        request,
        "tm_detail.html",
        {
            "request": request,
            "e": e,
            "entry": tm_module.entry_to_dict(e),
            "can_delete": can_manage,
            "can_edit": can_manage,
            "edited": request.query_params.get("edited"),
            "err": request.query_params.get("err"),
            # Customer-first browse continuity: carry ?customer= into the back-link so
            # returning from a detail page lands on the same customer's TM view.
            "customer": (request.query_params.get("customer") or "").strip(),
            "label_for": languages_catalog.label_for,
        },
    )


# --- single edit (target text, in place) -------------------------------------
@router.post("/tm/{entry_id}/edit")
def tm_edit(
    entry_id: int,
    request: Request,
    target: str = Form(""),
    acknowledge_placeholder_loss: str = Form(""),
    session: Session = Depends(get_session),
):
    """Edit a TM entry's TARGET text in place. Gated on can_tune_ai (admin or
    linguist) — the SAME gate as delete, since editing TM content is TM-lifecycle
    management (the linguist's turf); gating it looser than delete would be
    incoherent. The new target runs through the refusal choke point in
    tm_module.edit_entry (an edit is a write site, not a bypass). Source is not
    editable (it is the match key)."""
    from urllib.parse import quote

    me = security.session_user(request)
    if security.auth_required() and not security.can_tune_ai(me):
        return _error_page(
            request, 403, "Editing translation-memory entries is limited to linguists and admins."
        )
    pids = _visible_pids(request, session)
    try:
        e = tm_module.edit_entry(
            session,
            entry_id,
            target,
            visible_pids=pids,
            actor=_actor_email(request),
            ip=_ip(request),
            acknowledge_placeholder_loss=acknowledge_placeholder_loss in ("1", "true", "yes"),
        )
    except (tm_module.TMEditRefused, ValueError) as exc:
        return RedirectResponse(f"/tm/{entry_id}?err=" + quote(str(exc)), status_code=303)
    if e is None:
        return _error_page(request, 404, "Translation-memory entry not found.")
    return RedirectResponse(f"/tm/{entry_id}?edited=1", status_code=303)


# --- single delete -----------------------------------------------------------
@router.post("/tm/{entry_id}/delete")
def tm_delete(
    entry_id: int,
    request: Request,
    customer: str = Form(""),
    session: Session = Depends(get_session),
):
    _me, blocked = _manage_gate(request)
    if blocked:
        return blocked
    pids = _visible_pids(request, session)
    ok = tm_module.delete_entry(session, entry_id, visible_pids=pids)
    if not ok:
        return _error_page(request, 404, "Translation-memory entry not found.")
    audit.log(
        actor=_actor_email(request),
        action="tm.delete",
        target=f"tm entry {entry_id}",
        ip=_ip(request),
    )
    # Customer-first browse: return to the customer's TM view (no unscoped landing).
    cq = f"customer={customer.strip()}&" if customer.strip() else ""
    return RedirectResponse(f"/tm?{cq}deleted=1", status_code=303)


# --- bulk delete by filter (count-first confirm) -----------------------------
@router.post("/tm/bulk-delete")
def tm_bulk_delete(
    request: Request,
    source_locale: str = Form(""),
    target_locale: str = Form(""),
    project_id: str = Form(""),
    content_type_id: str = Form(""),
    q: str = Form(""),
    created_before: str = Form(""),
    created_after: str = Form(""),
    min_use: str = Form(""),
    max_use: str = Form(""),
    unused: str = Form(""),
    confirm: str = Form(""),
    confirm_all: str = Form(""),
    customer: str = Form(""),
    session: Session = Depends(get_session),
):
    """Two-step: without confirm=1 it returns the COUNT that WOULD be deleted (the
    page re-renders a "this will delete N entries — confirm" banner); with confirm=1
    it performs the delete. The dry-run count is the safety step before an
    accidental sweep.

    Server-side hardening (MEDIUM finding): the count-first banner is a UI
    convenience only -- a forged POST straight to this route with confirm=1 and NO
    filters would otherwise wipe the caller's entire visible TM without ever
    computing/showing a count. When no filter field is set, this route now also
    requires confirm_all=1 (the real UI form never needs to send it -- the
    filterless preview banner adds it automatically); confirm=1 alone is no longer
    sufficient for the filterless case. Filtered deletes and dry-runs (no confirm)
    are unaffected."""
    _me, blocked = _manage_gate(request)
    if blocked:
        return blocked
    # #490: scope the delete SERVER side to the posted customer (the page the user
    # is looking at); a missing/foreign customer on a multi-customer ring, or any
    # malformed filter, is refused before anything is counted or deleted.
    me = security.session_user(request)
    try:
        filters = tm_module.strict_bulk_filters(
            {
                "source_locale": source_locale,
                "target_locale": target_locale,
                "project_id": project_id,
                "content_type_id": content_type_id,
                "q": q,
                "created_before": created_before,
                "created_after": created_after,
                "min_use": min_use,
                "max_use": max_use,
                "unused": unused,
            }
        )
        customers = tm_module.browsable_customers(
            session,
            org_id=access.current_org_id(me),
            bound_customer_id=access.current_customer_id(me),
        )
        pids = tm_module.resolve_bulk_delete_scope(
            session,
            _visible_pids(request, session),
            customers=customers,
            customer=customer,
            project_id=filters["project_id"],
        )
    except tm_module.BulkDeleteRefused as exc:
        return _error_page(request, 400, f"TM bulk delete refused: {exc}")
    # Rebuild the query string so the confirm banner + re-run keep the same filters.
    from urllib.parse import urlencode

    keep = {
        k: v
        for k, v in {
            # customer FIRST so the customer-first browse always lands back on the same
            # customer after a bulk delete / preview (no unscoped all-customers landing).
            "customer": customer.strip(),
            "source_locale": source_locale.strip(),
            "target_locale": target_locale.strip(),
            "project_id": project_id.strip(),
            "content_type_id": content_type_id.strip(),
            "q": q.strip(),
            "created_before": created_before.strip(),
            "created_after": created_after.strip(),
            "min_use": min_use.strip(),
            "max_use": max_use.strip(),
            "unused": "1" if filters["unused"] else "",
        }.items()
        if v
    }
    qs = urlencode(keep)
    filterless = _no_filters(filters)

    if confirm not in ("1", "true", "yes", "on"):
        n = tm_module.bulk_delete(session, dry_run=True, visible_pids=pids, **filters)
        sep = "&" if qs else ""
        extra = "&confirm_all=1" if filterless else ""
        return RedirectResponse(f"/tm?{qs}{sep}confirm_n={n}{extra}", status_code=303)

    if filterless and confirm_all not in ("1", "true", "yes", "on"):
        return _error_page(
            request,
            400,
            "Refusing an unfiltered bulk delete without the "
            "confirm-all step (this would delete the entire "
            "visible TM). Use the preview/confirm banner, or add "
            "a filter.",
        )

    n = tm_module.bulk_delete(session, dry_run=False, visible_pids=pids, **filters)
    audit.log(
        actor=_actor_email(request),
        action="tm.bulk_delete",
        target=(
            f"{n} tm entries ({tm_module.scope_label(customer, filters['project_id'])}; "
            f"filters: {keep})"
        ),
        ip=_ip(request),
    )
    sep = "&" if qs else ""
    return RedirectResponse(f"/tm?{qs}{sep}bulk_deleted={n}", status_code=303)
