"""Global search: the web results page, the live suggestions and the /api route (#595).

One service (app/services/global_search.py) behind every door, so access and bounds cannot
drift between them:

  * web `router`: GET /search (grouped results, or one group paged) and GET /search/suggest
    (JSON for the top-bar suggestions; session user, never the API);
  * `api_router`: GET /api/search (token, docs scope; no session is read here).

Neither door writes anything or audits a read; both answer a bad query with a plain sentence
(400 on /api and /search/suggest, an inline message on the page).

Documented in: docs/architecture/surfaces-and-parity.md
"""

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from sqlmodel import Session

from app.db import engine, get_session
from app.kernel import global_search, security
from app.web.api_cli_common import _gate
from app.web.deps import templates

router = APIRouter()
api_router = APIRouter()

PAGE_SIZE = global_search.MAX_LIMIT
SUGGEST_LIMIT = 3


def _groups_from(values) -> list[str]:
    """`group=a&group=b` or `group=a,b` -> ['a', 'b'] (unknown names are the service's 400)."""
    out = []
    for raw in values:
        out.extend(p.strip().lower() for p in str(raw).split(",") if p.strip())
    return out


@router.get("/search")
def search_page(
    request: Request,
    q: str = "",
    group: str = "",
    page: str = "1",
    session: Session = Depends(get_session),
):
    me = security.session_user(request)
    text = " ".join((q or "").split())
    ctx = {
        "request": request,
        "q": text,
        "group": "",
        "result": None,
        "group_block": None,
        "message": "",
        "page": 1,
        "pages": 1,
        "labels": global_search.LABELS,
        "min_query": global_search.MIN_QUERY,
        "searched": global_search.SEARCHED,
    }
    if not text:
        return templates.TemplateResponse(request, "search.html", ctx)
    group = (group or "").strip().lower()
    scope = global_search.scope_for_user(session, me)
    try:
        if group:
            # ASCII digits only and short: `"²".isdigit()` is True but int() refuses it,
            # and a 5000-digit string trips Python's int-conversion limit (both were a 500).
            raw_page = str(page)
            page = (
                int(raw_page)
                if raw_page.isascii() and raw_page.isdigit() and len(raw_page) <= 6
                else 1
            )
            page = max(1, page)
            result = global_search.search(
                session,
                scope,
                text,
                groups=[group],
                limit=PAGE_SIZE,
                offset=(page - 1) * PAGE_SIZE,
            )
            block = result["groups"][0]
            ctx.update(group=group, group_block=block, page=page)
            ctx["pages"] = max(
                1, -(-min(block["count"], global_search.MAX_OFFSET + PAGE_SIZE) // PAGE_SIZE)
            )
        else:
            result = global_search.search(session, scope, text, limit=global_search.DEFAULT_LIMIT)
        ctx["result"] = result
    except global_search.SearchQueryError as exc:
        ctx["message"] = str(exc)
    return templates.TemplateResponse(request, "search.html", ctx)


@router.get("/search/suggest")
def search_suggest(request: Request, q: str = "", session: Session = Depends(get_session)):
    """Compact grouped hits for the top-bar suggestions (the same service, a smaller limit)."""
    me = security.session_user(request)
    scope = global_search.scope_for_user(session, me)
    try:
        result = global_search.search(session, scope, q, limit=SUGGEST_LIMIT)
    except global_search.SearchQueryError as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
    return JSONResponse({"ok": True, **result}, headers={"Cache-Control": "no-store"})


@api_router.get("/api/search")
def api_search(request: Request):
    """Search projects, documents (file names), TM, glossary terms, workflows and connections
    the caller may open.

    Query: q (2 to 100 characters), group (repeat or comma-separate: projects, documents, tm,
    glossary, workflows, connections; default all), limit (1 to 25, default 5), offset
    (0 to 1000)."""
    ident, _org_id, blocked = _gate(request)
    if blocked:
        return blocked
    qp = request.query_params
    with Session(engine) as session:
        scope = global_search.scope_for_user(session, (ident or {}).get("user"))
        try:
            result = global_search.search(
                session,
                scope,
                qp.get("q", ""),
                groups=_groups_from(qp.getlist("group")),
                limit=qp.get("limit") or global_search.DEFAULT_LIMIT,
                offset=qp.get("offset") or 0,
            )
        except global_search.SearchQueryError as exc:
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
    return JSONResponse({"ok": True, **result})
