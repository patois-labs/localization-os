"""Threaded queries + comments — the WEB surface (SPEC-wave3-collaboration §4.2).

A THIN caller of ``app.services.query_threads`` (the ONE truth; §4.1). Every handler,
in order (§4.2):

    feature_block(request, "queries")   # layer-2 kill switch, themed 404 (never 403)
    security.session_user(request)       # the AUTHENTICATED user — never a free actor
    resolve_scope(...)                   # open only: anchor -> full scope tuple
    deps._visibility_block(...)          # open only: the project chokepoint (404/403)
    <service verb>                       # enforces the tenant ring + RBAC on `user`
    audit.log(... customer_id=<derived>) # ON SUCCESS ONLY (refused => no audit row)
    RedirectResponse(303)

The service raises typed ``QueryError`` subclasses whose ``.status`` is the HTTP code;
``_query_error`` renders them as a themed page. Refused attempts (RBAC, cap, closed
thread) never reach ``audit.log`` — the project convention (home.py:244-251).

RBAC (§4.5) is the service's job and is derived from the passed ``user`` — a viewer may
open/post/answer; author/assignee/reviewer+ close/assign. Under ``auth_required()==False``
``session_user`` is the SYSTEM admin, so single-tenant is unrestricted, byte-identical to
every other web surface.

Documented in: docs/architecture/collaboration-and-review.md
"""

from fastapi import APIRouter, Form, Request
from fastapi.responses import RedirectResponse
from sqlmodel import Session, select

from app import features
from app.db import engine
from app.kernel import access, audit, security
from app.kernel import query_threads as qt
from app.models import (
    Project,
    QueryStatus,
    QueryThread,
    Vendor,
    VendorDispatch,
    VendorDispatchStatus,
)
from app.web.deps import (
    _actor_email,
    _error_page,
    _ip,
    _missing_or,
    _visibility_block,
    feature_block,
    templates,
)

router = APIRouter()

_OPEN_LEG = (VendorDispatchStatus.DISPATCHED, VendorDispatchStatus.QUOTED)


# --- helpers ----------------------------------------------------------------
def _query_error(request: Request, exc: qt.QueryError):
    """Render a service refusal as a themed page (the /api twin in api_cli.py renders
    JSON from the SAME service). A 404 stays non-enumerable (the tenant ring)."""
    return _error_page(request, exc.status, str(exc))


def _bool(qp, name: str) -> bool:
    return (qp.get(name) or "").strip().lower() in ("1", "true", "on", "yes")


def _int(qp, name: str):
    raw = (qp.get(name) or "").strip()
    try:
        n = int(raw) if raw else None
    except (TypeError, ValueError):
        return None
    return n if n is None or 0 < n < 2**63 else None


def _form_int(raw):
    raw = (raw or "").strip()
    try:
        n = int(raw) if raw else None
    except (TypeError, ValueError):
        return None
    return n if n is None or 0 < n < 2**63 else None


def _notify(fn_name: str, *args) -> None:
    """Fire a query notification after commit, in the caller's guarded tail (the
    orchestrator idiom): commit first (the service already did), deferred import, and a
    local try/except so a mail side effect can never crash the request."""
    try:
        from app.kernel import notifications

        getattr(notifications, fn_name)(*args)
    except Exception:  # noqa: S110 -- notification mail is a side effect; never crash the request
        pass


def _dispatch_disclosure(session: Session, scope: qt.ScopeTuple) -> str:
    """The §7.2/MED-3b egress disclosure line for the compose form, or "".

    A thread is one conversation visible whole in both directions (§3.1), so opening a
    query on a segment CURRENTLY out with a vendor shares the thread with them — and the
    buyer's client sees the vendor's replies. Written as a property of the thread ("with
    the next package"), not the moment, because a LATER dispatch also carries earlier
    threads. Best-effort: any lookup failure degrades to no line."""
    doc_id = scope.document_id
    if doc_id is None and scope.segment_id is not None:
        from app.models import Segment

        seg = session.get(Segment, scope.segment_id)
        doc_id = seg.document_id if seg is not None else None
    if doc_id is None:
        return ""
    stmt = select(VendorDispatch).where(
        VendorDispatch.document_id == doc_id, VendorDispatch.status.in_(_OPEN_LEG)
    )
    if scope.target_locale:
        stmt = stmt.where(VendorDispatch.target_locale == scope.target_locale)
    leg = session.exec(stmt).first()
    if leg is None:
        return ""
    vendor = session.get(Vendor, leg.vendor_id) if leg.vendor_id else None
    name = (getattr(vendor, "name", "") or "the vendor").strip() or "the vendor"
    return (
        f"This segment is out with {name}. Anything you write here will be sent to them "
        "with the next package, and your client can see this thread."
    )


def _thread_view(request: Request, me, thread, posts, *, disclosure: str = ""):
    """Render one thread + its posts + the lifecycle controls the caller may use."""
    email = getattr(me, "email", "") or ""
    is_author = bool(email) and email == thread.author
    is_assignee = bool(email) and email == thread.assignee_email
    is_reviewer = security.can_review(me)
    return templates.TemplateResponse(
        request,
        "query_thread.html",
        {
            "request": request,
            "thread": thread,
            "posts": posts,
            "disclosure": disclosure,
            "can_close": (
                thread.status != QueryStatus.CLOSED and (is_author or is_assignee or is_reviewer)
            ),
            "can_reopen": (thread.status == QueryStatus.CLOSED and (is_author or is_reviewer)),
            "can_assign": is_author or is_reviewer,
            "QueryStatus": QueryStatus,
        },
    )


# --- list -------------------------------------------------------------------
@router.get("/queries")
def queries_list(request: Request):
    blocked = feature_block(request, "queries")
    if blocked:
        return blocked
    me = security.session_user(request)
    qp = request.query_params
    status = (qp.get("status") or "").strip() or None
    if status is not None and status not in QueryStatus.ALL:
        status = None
    project_id = _int(qp, "project")
    document_id = _int(qp, "document")
    segment_id = _int(qp, "segment")
    key_id = _int(qp, "key")
    mine = _bool(qp, "mine")
    assignee = None
    if _bool(qp, "assignee") or (qp.get("assignee") or "").strip().lower() == "me":
        assignee = (getattr(me, "email", "") or "") or None
    offset = _int(qp, "offset") or 0
    disclosure = ""
    with Session(engine) as session:
        rows, total = qt.list_threads(
            session,
            me,
            project_id=project_id,
            document_id=document_id,
            segment_id=segment_id,
            key_id=key_id,
            status=status,
            assignee=assignee,
            mine=mine,
            limit=qt.LIST_PAGE_MAX,
            offset=offset,
        )
        names = {}
        pids = {r.project_id for r in rows}
        if pids:
            for p in session.exec(select(Project).where(Project.id.in_(list(pids)))).all():
                names[p.id] = p.name
        # Compose affordance: if this list is scoped to a concrete anchor, resolve it so
        # the "open a query" form can carry the scope AND the §7.2 egress disclosure.
        # H-1 (gate fix): resolve_scope runs on RAW user input, so the disclosure MUST be
        # ring-checked before it is computed — a caller who cannot VIEW the resolved
        # project gets the same empty result as a nonexistent anchor (no enumeration
        # oracle on a foreign project's vendor name). The threads themselves are already
        # scope-filtered by list_threads; this closes the disclosure side channel. Also
        # gated on vendors_enabled — with vendors off there is no vendor carry to warn of.
        if segment_id or document_id or key_id or project_id:
            try:
                scope = qt.resolve_scope(
                    session,
                    segment_id=segment_id,
                    document_id=document_id,
                    key_id=key_id,
                    project_id=project_id,
                )
                if features.is_enabled("vendors") and access.can_view_project(
                    session, me, scope.project_id
                ):
                    disclosure = _dispatch_disclosure(session, scope)
            except Exception:  # noqa: S110 -- disclosure banner is advisory; list already built
                # lookup failure (a bad anchor, a DB hiccup) degrades to no banner and never
                # breaks the list, which was already fetched above.
                pass
    # M-11: honest paging — a Prev/Next that preserves the current filters + moves offset.
    from urllib.parse import urlencode

    base = {}
    for k, v in (
        ("status", status),
        ("project", project_id),
        ("document", document_id),
        ("segment", segment_id),
        ("key", key_id),
    ):
        if v:
            base[k] = v
    if mine:
        base["mine"] = "1"
    if assignee:
        base["assignee"] = "me"
    shown = len(rows)
    prev_qs = urlencode(dict(base, offset=max(0, offset - qt.LIST_PAGE_MAX)))
    next_qs = urlencode(dict(base, offset=offset + qt.LIST_PAGE_MAX))
    return templates.TemplateResponse(
        request,
        "queries.html",
        {
            "request": request,
            "threads": rows,
            "total": total,
            "project_names": names,
            "limit": qt.LIST_PAGE_MAX,
            "offset": offset,
            "shown": shown,
            "has_prev": offset > 0,
            "has_next": offset + shown < total,
            "prev_qs": prev_qs,
            "next_qs": next_qs,
            "f_status": status or "",
            "f_project": project_id or "",
            "f_document": document_id or "",
            "f_mine": mine,
            "f_assignee": bool(assignee),
            "QueryStatus": QueryStatus,
            "scope_segment": segment_id,
            "scope_document": document_id,
            "scope_key": key_id,
            "scope_project": project_id,
            "disclosure": disclosure,
        },
    )


# --- one thread -------------------------------------------------------------
@router.get("/queries/{qid}")
def query_show(qid: int, request: Request):
    blocked = feature_block(request, "queries")
    if blocked:
        return blocked
    me = security.session_user(request)
    with Session(engine) as session:
        try:
            thread, posts = qt.get_thread(session, user=me, thread_id=qid)
        except qt.QueryError as exc:
            return _query_error(request, exc)
        # L-9: the REPLY box is also egress to the vendor when the thread's segment is under
        # dispatch, so it carries the same §7.2 disclosure the compose box does. get_thread
        # already ring-checked (the caller can view this thread), so only the vendors gate
        # is re-applied; best-effort (any lookup failure -> no banner).
        disclosure = ""
        if features.is_enabled("vendors") and thread.status != QueryStatus.CLOSED:
            try:  # noqa: SIM105
                disclosure = _dispatch_disclosure(
                    session,
                    qt.ScopeTuple(
                        project_id=thread.project_id,
                        document_id=thread.document_id,
                        segment_id=thread.segment_id,
                        key_id=thread.key_id,
                        target_locale=thread.target_locale,
                    ),
                )
            except Exception:  # noqa: S110 -- disclosure banner is advisory; thread still renders
                pass
        return _thread_view(request, me, thread, posts, disclosure=disclosure)


# --- open -------------------------------------------------------------------
@router.post("/queries")
def query_open(
    request: Request,
    subject: str = Form(""),
    body: str = Form(""),
    assignee_email: str = Form(""),
    segment_id: str = Form(""),
    document_id: str = Form(""),
    key_id: str = Form(""),
    project_id: str = Form(""),
):
    blocked = feature_block(request, "queries")
    if blocked:
        return blocked
    me = security.session_user(request)
    with Session(engine) as session:
        try:
            scope = qt.resolve_scope(
                session,
                segment_id=_form_int(segment_id),
                document_id=_form_int(document_id),
                key_id=_form_int(key_id),
                project_id=_form_int(project_id),
            )
        except qt.QueryNotFound as exc:
            # #586: a missing anchor id answers exactly like one the caller may not see.
            return _missing_or(request, session, _query_error(request, exc))
        except qt.QueryError as exc:
            # A disagreement between anchors must not reveal that a hidden one exists.
            for pid in qt.anchor_projects(
                session,
                segment_id=_form_int(segment_id),
                document_id=_form_int(document_id),
                key_id=_form_int(key_id),
                project_id=_form_int(project_id),
            ):
                # a missing anchor (None) answers exactly like a hidden one for a ringed caller
                vb = (
                    _missing_or(request, session, None)
                    if pid is None
                    else _visibility_block(request, session, pid)
                )
                if vb:
                    return vb
            return _query_error(request, exc)
        # The project chokepoint BEFORE any write (§4.2). The service re-checks the ring.
        vb = _visibility_block(request, session, scope.project_id)
        if vb:
            return vb
        try:
            thread = qt.open_thread(
                session,
                user=me,
                scope=scope,
                subject=subject,
                body=body,
                assignee_email=assignee_email,
            )
        except qt.QueryError as exc:
            return _query_error(request, exc)
        audit.log(
            actor=_actor_email(request),
            action="query.open",
            target=f"thread:{thread.id}",
            detail=f"project={thread.project_id} status={thread.status}",
            ip=_ip(request),
            customer_id=audit.customer_for_project(thread.project_id),
        )
        tid = thread.id
        actor_email = _actor_email(request)
    _notify("on_query_opened", tid, actor_email)  # L-5: skips a self-assigned open
    return RedirectResponse(f"/queries/{tid}", status_code=303)


# --- reply ------------------------------------------------------------------
@router.post("/queries/{qid}/posts")
def query_post(qid: int, request: Request, body: str = Form(""), answer: str = Form("")):
    blocked = feature_block(request, "queries")
    if blocked:
        return blocked
    me = security.session_user(request)
    is_answer = (answer or "").strip().lower() in ("1", "true", "on", "yes")
    with Session(engine) as session:
        try:
            post = qt.add_post(session, user=me, thread_id=qid, body=body, answer=is_answer)
        except qt.QueryError as exc:
            return _query_error(request, exc)
        from app.models import QueryThread

        thread = session.get(QueryThread, qid)
        audit.log(
            actor=_actor_email(request),
            action="query.post",
            target=f"thread:{qid}",
            detail=f"post={post.id} answer={int(is_answer)} status={thread.status}",
            ip=_ip(request),
            customer_id=audit.customer_for_project(thread.project_id),
        )
        actor_email = _actor_email(request)
    _notify("on_query_post", qid, post.id, actor_email)
    return RedirectResponse(f"/queries/{qid}", status_code=303)


# --- status -----------------------------------------------------------------
@router.post("/queries/{qid}/status")
def query_status(qid: int, request: Request, status: str = Form("")):
    blocked = feature_block(request, "queries")
    if blocked:
        return blocked
    me = security.session_user(request)
    with Session(engine) as session:
        # M-2: notify on the TRANSITION, not the target state — a no-op close (already
        # closed) must not re-mail. The prior read is internal only; set_status enforces
        # the ring, so an out-of-scope caller raises before any notification.
        prior = session.get(QueryThread, qid)
        prior_status = prior.status if prior else None
        try:
            thread = qt.set_status(session, user=me, thread_id=qid, status=(status or "").strip())
        except qt.QueryError as exc:
            return _query_error(request, exc)
        audit.log(
            actor=_actor_email(request),
            action="query.status",
            target=f"thread:{qid}",
            detail=f"status={thread.status}",
            ip=_ip(request),
            customer_id=audit.customer_for_project(thread.project_id),
        )
        newly_closed = thread.status == QueryStatus.CLOSED and prior_status != QueryStatus.CLOSED
        actor_email = _actor_email(request)
    if newly_closed:
        _notify("on_query_closed", qid, actor_email)
    return RedirectResponse(f"/queries/{qid}", status_code=303)


# --- assign -----------------------------------------------------------------
@router.post("/queries/{qid}/assign")
def query_assign(qid: int, request: Request, assignee_email: str = Form("")):
    blocked = feature_block(request, "queries")
    if blocked:
        return blocked
    me = security.session_user(request)
    with Session(engine) as session:
        # M-3: notify only when the assignee ACTUALLY CHANGED — re-assigning the same
        # person (or clearing) must not re-mail.
        prior = session.get(QueryThread, qid)
        prior_assignee = (prior.assignee_email or "") if prior else ""
        try:
            thread = qt.assign(session, user=me, thread_id=qid, assignee_email=assignee_email)
        except qt.QueryError as exc:
            return _query_error(request, exc)
        audit.log(
            actor=_actor_email(request),
            action="query.assign",
            target=f"thread:{qid}",
            detail="assignee=%s" % (thread.assignee_email or "(cleared)"),
            ip=_ip(request),
            customer_id=audit.customer_for_project(thread.project_id),
        )
        changed = bool(thread.assignee_email) and thread.assignee_email != prior_assignee
        actor_email = _actor_email(request)
    if changed:
        _notify("on_query_opened", qid, actor_email)  # notifies the NEW assignee (L-5)
    return RedirectResponse(f"/queries/{qid}", status_code=303)
