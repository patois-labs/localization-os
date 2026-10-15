"""admin: part 2 (layered pure move)

Documented in: docs/architecture/configuration.md
"""

from fastapi import APIRouter, Request
from fastapi.responses import RedirectResponse

from app.kernel import (
    audit,
    security,
)
from app.web.deps import (
    _error_page,
    _my_customer_id,
    _my_org_id,
    templates,
)

router = APIRouter()


# --- segmentation PROFILES (P6, item 8) --------------------------------------
def _seg_redirect(msg: str = "", err: str = "") -> RedirectResponse:
    from urllib.parse import quote

    q = ("srx_msg=" + quote(msg)) if msg else ("srx_error=" + quote(err))
    return RedirectResponse("/settings?" + q + "#segprofiles", status_code=303)


AUDIT_PAGE_SIZE = 50


@router.get("/audit")
def audit_page(request: Request, page: int = 1, actor: str = "", action: str = ""):
    # R2-3: a CUSTOMER-BOUND user may VIEW the audit log — but confined to its own
    # customer's rows (the isolation guarantee). Internal staff keep the admin gate.
    # So the access rule is: admin OR customer-bound. A bound user is never admin
    # (role restriction), so chain-verify + export (admin-only) stay out of reach.
    me = security.session_user(request)
    my_customer = _my_customer_id(request)
    if security.auth_required() and not security.is_admin(me) and my_customer is None:
        return _error_page(request, 403, "This page is limited to administrators.")
    page = max(1, page)
    actor_q = (actor or "").strip()
    action_q = (action or "").strip()
    # pass the filters through only when set, per the audit-service contract
    # recent(limit, offset, actor=None, action=None) / count(actor=None, action=None)
    filters = {}
    if actor_q:
        filters["actor"] = actor_q
    if action_q:
        filters["action"] = action_q
    # Multi-org P8: an ORG-ADMIN sees only their OWN org's audit rows. A super-admin
    # in the CUSTOMER app is JUST an org-admin of their HOME org here (THE LINCHPIN)
    # — cross-org audit is the back-office's alone. _my_org_id is None with the gate
    # off, so the customer audit log stays a single unscoped log single-tenant.
    filters["org_id"] = _my_org_id(request)
    # R2-3: a customer-bound user's view is HARD-confined to its customer's rows —
    # system/NULL rows and every other customer's rows are excluded. The filter is
    # applied SERVER-SIDE from the SESSION user's binding (NOT any query param), so
    # a bound user cannot probe wider via ?actor=/?action=/paging. Internal staff
    # pass None (unconfined by customer — the admin gate governs their view).
    filters["customer_id"] = my_customer
    total = audit.count(**filters)
    pages = max(1, -(-total // AUDIT_PAGE_SIZE))
    page = min(page, pages)
    rows = audit.recent(limit=AUDIT_PAGE_SIZE, offset=(page - 1) * AUDIT_PAGE_SIZE, **filters)
    # hash-chain verification is O(n) CPU but streams in bounded batches (constant
    # memory) — admin-only + still run only on explicit request. A customer-bound
    # user is never admin, so chain-verify is doubly out of reach for them.
    chain = (
        audit.verify_chain()
        if (
            request.query_params.get("verify") == "1"
            and my_customer is None
            and security.is_admin(me)
        )
        else None
    )
    chain_hint = audit.legacy_restore_hint(chain[2]) if chain and not chain[0] else None
    # G-10: a one-line readout of the last SCHEDULED chain verification (distinct from the
    # on-demand `chain` above). Admin-only + never for a customer-bound viewer, exactly like
    # the on-demand verify. The view carries disabled/stale flags so the page can warn when
    # scheduled checking is OFF or has silently stopped, and never shows a false OK (F7).
    chain_monitor = (
        audit.chain_monitor_view() if (my_customer is None and security.is_admin(me)) else None
    )
    return templates.TemplateResponse(
        request,
        "audit.html",
        {
            "request": request,
            "rows": rows,
            "actor_q": actor_q,
            "action_q": action_q,
            "chain": chain,
            "chain_hint": chain_hint,
            "chain_monitor": chain_monitor,
            "customer_bound": my_customer is not None,
            "page": page,
            "pages": pages,
            "total": total,
        },
    )
