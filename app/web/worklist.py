"""The reviewer worklist — WEB surface (SPEC-wave3-collaboration §8.4, LC-5).

A THIN caller of `app.services.worklist` (the one truth; §8.1). One route:

    GET /my-work   the authenticated user's cross-project worklist

Handler order (the four-surface machinery, §2.9): `feature_block(request, "worklist")`
first (layer-2 kill switch, themed 404 never 403) -> `security.session_user` -> the
service (which owns the scoping ring). Identity is the session user; under
`auth_required()==False` that is the SYSTEM admin, so single-tenant is unrestricted and
byte-identical to every other web surface.

The page renders three sections; the query section is present only when queries_enabled
is on (the service drops it — §5.2), review + quality survive regardless. Quality is
reviewer+ only (the service returns nothing for a translator). The §8.3 assignment
limitation is stated on the page verbatim.

Documented in: docs/architecture/collaboration-and-review.md
"""

from fastapi import APIRouter, Request
from sqlmodel import Session

from app import features
from app.db import engine
from app.kernel import security, worklist
from app.web.deps import feature_block, templates

router = APIRouter()


def _int(qp, name: str, default=None):
    raw = (qp.get(name) or "").strip()
    try:
        n = int(raw) if raw else None
    except (TypeError, ValueError):
        return default
    return n if n is not None else default


@router.get("/my-work")
def my_work_page(request: Request):
    blocked = feature_block(request, "worklist")
    if blocked:
        return blocked
    me = security.session_user(request)
    qp = request.query_params
    quality = (qp.get("quality") or "").strip().lower()
    quality = "needs_review" if quality == "needs_review" else ""
    days = _int(qp, "days", worklist.QUALITY_WINDOW_DAYS) or worklist.QUALITY_WINDOW_DAYS
    days = max(1, min(days, 3650))  # a sane, declared bound (1 day .. 10 years)
    offset = max(0, _int(qp, "offset", 0) or 0)
    queries_on = features.is_enabled("queries")
    with Session(engine) as session:
        rows, total = worklist.my_work(
            session, me, days=days, quality=quality, limit=worklist.LIST_PAGE_MAX, offset=offset
        )
        # can_review controls whether the quality section renders at all (the service
        # already omits its items for a non-reviewer; the page mirrors that for the
        # empty-state copy so a translator is not told "no quality items" for a section
        # they can never have).
        can_review = security.can_review(me)
    shown = len(rows)
    limit = worklist.LIST_PAGE_MAX
    return templates.TemplateResponse(
        request,
        "my_work.html",
        {
            "request": request,
            "items": rows,
            "total": total,
            "shown": shown,
            "offset": offset,
            "limit": limit,
            "days": days,
            "quality": quality,
            "queries_on": queries_on,
            "can_review": can_review,
            "has_prev": offset > 0,
            "has_next": offset + shown < total,
            "prev_offset": max(0, offset - limit),
            "next_offset": offset + limit,
        },
    )
