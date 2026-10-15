"""Notification channels and routes: the web page and the /api routes (project automation Part B).

One service (app/services/notify.py) behind every door:

  * web `router`: GET /settings/notifications (channels, routes, delivery log) and its POST forms,
    plus the small attach / mute forms on the project page (manager and above);
  * `api_router`: /api/notify/channels, /api/notify/routes and /api/notify/projects/{id}. Token +
    docs scope; no session is read here.

Isolation order: the org / customer / project ring first (a foreign or missing id answers the same
404), then the role gate (manager and above, 10-13: managers may create channels), then input
validation. The destination URL and the webhook signing secret are write-only: no response, page,
audit row or log ever carries them (the signing secret is shown once, in the response that created
or rotated it, and on the page through the one-time stash).

Documented in: docs/architecture/notifications.md
"""

from urllib.parse import quote

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import JSONResponse, RedirectResponse
from sqlmodel import Session, select

from app.db import engine, get_session
from app.kernel import access, ids, notify, oidc, security
from app.models import Customer, NotifyRoute, Project
from app.web import deps, one_time_stash
from app.web.api_cli_common import (
    _NOT_FOUND,
    _content_type_in_scope,
    _customer_in_org,
    _gate,
    _int_id,
    _json,
    _list_window,
    _manage_block,
    _project_in_org,
)

router = APIRouter()
api_router = APIRouter()

#: Scopes a customer-bound caller may use (its own customer and that customer's projects).
_BOUND_SCOPES = ("project", "customer")


def _err(message: str, status: int = 400) -> JSONResponse:
    return JSONResponse({"ok": False, "error": message}, status_code=status)


def _route_scope_ok(s: Session, scope, ref, org_id, ident):
    """(project_or_None, ref, error_response_or_None) for a route scope inside the caller's ring."""
    scope = str(scope or "").strip().lower()
    if scope not in notify.SCOPES:
        return None, None, _err("scope must be one of: " + ", ".join(notify.SCOPES) + ".")
    bound = ident.get("_cust_id")
    if bound is not None and scope not in _BOUND_SCOPES:
        return (
            None,
            None,
            _err("a customer account can attach to its customer or projects only.", 403),
        )
    if scope == "org" and not security.is_admin(ident.get("user")) and security.auth_required():
        return None, None, _err("attaching a channel to the whole org is limited to admins.", 403)
    if scope == "project":
        pid = _int_id(ref)
        proj = _project_in_org(s, pid, org_id, ident) if pid is not None else None
        if proj is None:
            return None, None, JSONResponse(_NOT_FOUND, status_code=404)
        return proj, pid, None
    if scope == "customer":
        cid = _int_id(ref)
        if cid is None or _customer_in_org(s, cid, org_id, ident) is None:
            return None, None, JSONResponse(_NOT_FOUND, status_code=404)
        return None, cid, None
    if scope == "content_type":
        tid = _int_id(ref)
        if tid is None or _content_type_in_scope(s, tid, org_id, ident) is None:
            return None, None, JSONResponse(_NOT_FOUND, status_code=404)
        return None, tid, None
    return None, ref, None


def _org_route_admin_only(row, user) -> bool:
    """True when `row` is an org-default route and `user` may not remove it. Attaching at org
    scope is admin-only (fix round 1); removing one must be too, or a manager could silently
    drop an admin's org-wide (for example audit) channel. Review gate 10-13."""
    return (
        row is not None
        and row.scope == "org"
        and security.auth_required()
        and not security.is_admin(user)
    )


def _channel(s: Session, cid, org_id, ident):
    return notify.visible(s, cid, org_id=org_id, customer_id=ident.get("_cust_id"))


# --------------------------------------------------------------------------------------------
# /api: channels
# --------------------------------------------------------------------------------------------
@api_router.get("/api/notify/channels")
def api_notify_channels(request: Request):
    """The org's notification channels (manager and above). Never the URL or the signing secret:
    a masked destination hint and presence flags."""
    ident, org_id, blocked = _gate(request)
    if blocked:
        return blocked
    blocked = _manage_block(ident)
    if blocked:
        return blocked
    with Session(engine) as s:
        rows = notify.list_channels(s, org_id=org_id, customer_id=ident.get("_cust_id"))
        out = [notify.to_dict(s, c) for c in rows]
    return JSONResponse(
        {"ok": True, "channels": out, "outbound_enabled": notify.outbound_enabled()}
    )


@api_router.post("/api/notify/channels")
async def api_notify_channel_create(request: Request):
    """Create a channel: `{kind: slack|teams|webhook|email, name, url | recipients,
    customer_id?}`. A webhook channel's `signing_secret` is returned HERE ONCE."""
    ident, org_id, blocked = _gate(request)
    if blocked:
        return blocked
    blocked = _manage_block(ident)
    if blocked:
        return blocked
    body = await _json(request)
    bound = ident.get("_cust_id")
    raw_cust = body.get("customer_id")
    with Session(engine) as s:
        cust_id = bound
        if bound is None and raw_cust not in (None, ""):
            cid = _int_id(raw_cust)
            if cid is None or _customer_in_org(s, cid, org_id, ident) is None:
                return JSONResponse(_NOT_FOUND, status_code=404)
            cust_id = cid
        try:
            ch, shown = notify.create_channel(
                s,
                kind=str(body.get("kind") or "").strip().lower(),
                name=body.get("name"),
                url=str(body.get("url") or ""),
                recipients=body.get("recipients"),
                customer_id=cust_id,
                org_id=org_id,
                created_by=ident["actor"],
                ip=deps._ip(request),
            )
        except notify.NotifyError as exc:
            return _err(str(exc))
        out = notify.to_dict(s, ch)
    res = {"ok": True, "channel": out}
    if shown:
        res["signing_secret"] = shown
        res["note"] = "The signing secret is shown once; deliveries carry X-LocOS-Signature."
    return JSONResponse(res)


@api_router.get("/api/notify/channels/{cid}")
def api_notify_channel_get(request: Request, cid: str):
    ident, org_id, blocked = _gate(request)
    if blocked:
        return blocked
    with Session(engine) as s:
        ch = _channel(s, cid, org_id, ident)
        if ch is None:
            return JSONResponse(_NOT_FOUND, status_code=404)
        blocked = _manage_block(ident)
        if blocked:
            return blocked
        return JSONResponse({"ok": True, "channel": notify.to_dict(s, ch)})


@api_router.post("/api/notify/channels/{cid}")
async def api_notify_channel_update(request: Request, cid: str):
    """Update a channel: any of `name`, `url`, `recipients`, `enabled`, `rotate_signing_secret`."""
    ident, org_id, blocked = _gate(request)
    if blocked:
        return blocked
    with Session(engine) as s:
        ch = _channel(s, cid, org_id, ident)
        if ch is None:
            return JSONResponse(_NOT_FOUND, status_code=404)
        blocked = _manage_block(ident)
        if blocked:
            return blocked
        if not notify.can_modify(ch, ident.get("user"), ident["actor"]):
            return _err("only the channel's creator or an admin can change or delete it.", 403)
        body = await _json(request)
        enabled = body.get("enabled")
        if enabled is not None and not isinstance(enabled, bool):
            return _err("enabled must be true or false.")
        try:
            shown = notify.update_channel(
                s,
                ch,
                name=body.get("name"),
                url=str(body["url"]) if body.get("url") else None,
                recipients=body.get("recipients"),
                enabled=enabled,
                rotate_signing_secret=bool(body.get("rotate_signing_secret")),
                actor=ident["actor"],
                ip=deps._ip(request),
            )
        except notify.NotifyError as exc:
            return _err(str(exc))
        res = {"ok": True, "channel": notify.to_dict(s, ch)}
    if shown:
        res["signing_secret"] = shown
    return JSONResponse(res)


@api_router.delete("/api/notify/channels/{cid}")
def api_notify_channel_delete(request: Request, cid: str):
    ident, org_id, blocked = _gate(request)
    if blocked:
        return blocked
    with Session(engine) as s:
        ch = _channel(s, cid, org_id, ident)
        if ch is None:
            return JSONResponse(_NOT_FOUND, status_code=404)
        blocked = _manage_block(ident)
        if blocked:
            return blocked
        if not notify.can_modify(ch, ident.get("user"), ident["actor"]):
            return _err("only the channel's creator or an admin can change or delete it.", 403)
        notify.delete_channel(s, ch, actor=ident["actor"], ip=deps._ip(request))
    return JSONResponse({"ok": True})


@api_router.post("/api/notify/channels/{cid}/test")
def api_notify_channel_test(request: Request, cid: str):
    """Send a harmless test message now. `ok: false` carries the reason; the HTTP status is 200
    either way (the request itself worked)."""
    ident, org_id, blocked = _gate(request)
    if blocked:
        return blocked
    with Session(engine) as s:
        ch = _channel(s, cid, org_id, ident)
        if ch is None:
            return JSONResponse(_NOT_FOUND, status_code=404)
        blocked = _manage_block(ident)
        if blocked:
            return blocked
        res = notify.test_send(s, ch, actor=ident["actor"], ip=deps._ip(request))
    return JSONResponse({"ok": True, "delivered": res["ok"], "message": res["message"]})


@api_router.get("/api/notify/channels/{cid}/deliveries")
def api_notify_channel_deliveries(request: Request, cid: str):
    """The channel's delivery log, newest first (status, attempts, error class; no content)."""
    ident, org_id, blocked = _gate(request)
    if blocked:
        return blocked
    with Session(engine) as s:
        ch = _channel(s, cid, org_id, ident)
        if ch is None:
            return JSONResponse(_NOT_FOUND, status_code=404)
        blocked = _manage_block(ident)
        if blocked:
            return blocked
        limit, offset, err = _list_window(
            request.query_params.get("limit"), request.query_params.get("offset"), cap=200
        )
        if err:
            return err
        rows = notify.deliveries(s, channel_id=ch.id, org_id=org_id, limit=limit, offset=offset)
    return JSONResponse({"ok": True, "deliveries": rows, "limit": limit, "offset": offset})


@api_router.post("/api/notify/channels/{cid}/retry")
def api_notify_channel_retry(request: Request, cid: str):
    """Put the channel's failed deliveries back in the queue (one more round of retries)."""
    ident, org_id, blocked = _gate(request)
    if blocked:
        return blocked
    with Session(engine) as s:
        ch = _channel(s, cid, org_id, ident)
        if ch is None:
            return JSONResponse(_NOT_FOUND, status_code=404)
        blocked = _manage_block(ident)
        if blocked:
            return blocked
        n = notify.retry_failed(s, ch, actor=ident["actor"])
    return JSONResponse({"ok": True, "requeued": n})


# --------------------------------------------------------------------------------------------
# /api: routes and per-project view
# --------------------------------------------------------------------------------------------
@api_router.get("/api/notify/routes")
def api_notify_routes(request: Request):
    ident, org_id, blocked = _gate(request)
    if blocked:
        return blocked
    blocked = _manage_block(ident)
    if blocked:
        return blocked
    scope = request.query_params.get("scope")
    with Session(engine) as s:
        out = []
        for ch in notify.list_channels(s, org_id=org_id, customer_id=ident.get("_cust_id")):
            for row in s.exec(select(NotifyRoute).where(NotifyRoute.channel_id == ch.id)).all():
                if scope and row.scope != scope:
                    continue
                out.append(notify.route_to_dict(row, ch))
    return JSONResponse({"ok": True, "routes": out, "events": list(notify.EVENTS)})


@api_router.post("/api/notify/routes")
async def api_notify_route_set(request: Request):
    """Attach a channel to a scope: `{scope, ref, channel_id, events?}`. `scope` is project |
    content_type | project_type | customer | org (`ref` is the id, the project type name, or empty
    for org). Empty `events` means the default-on events. Idempotent per (channel, scope, ref)."""
    ident, org_id, blocked = _gate(request)
    if blocked:
        return blocked
    body = await _json(request)
    with Session(engine) as s:
        ch = _channel(s, body.get("channel_id"), org_id, ident)
        if ch is None:
            return JSONResponse(_NOT_FOUND, status_code=404)
        proj, ref, bad = _route_scope_ok(s, body.get("scope"), body.get("ref"), org_id, ident)
        if bad:
            return bad
        blocked = _manage_block(ident)
        if blocked:
            return blocked
        try:
            row = notify.set_route(
                s,
                scope=str(body.get("scope")).strip().lower(),
                ref=ref,
                channel=ch,
                events=body.get("events"),
                project=proj,
                created_by=ident["actor"],
                ip=deps._ip(request),
            )
        except notify.NotifyError as exc:
            return _err(str(exc))
        return JSONResponse({"ok": True, "route": notify.route_to_dict(row, ch)})


@api_router.delete("/api/notify/routes/{rid}")
def api_notify_route_clear(request: Request, rid: str):
    ident, org_id, blocked = _gate(request)
    if blocked:
        return blocked
    with Session(engine) as s:
        n = _int_id(rid)
        row = s.get(NotifyRoute, n) if n is not None else None
        ch = _channel(s, row.channel_id, org_id, ident) if row is not None else None
        if row is None or ch is None:
            return JSONResponse(_NOT_FOUND, status_code=404)
        blocked = _manage_block(ident)
        if blocked:
            return blocked
        if _org_route_admin_only(row, ident.get("user")):
            return _err("removing an org-wide route is limited to admins.", 403)
        notify.clear_route(
            s, row, actor=ident["actor"], ip=deps._ip(request), customer_id=ch.customer_id
        )
    return JSONResponse({"ok": True})


@api_router.get("/api/notify/projects/{pid}")
def api_notify_project(request: Request, pid: str):
    """What a project posts where: the effective channel per event and the level it came from,
    plus whether the project is muted. Anyone who can see the project may read it."""
    ident, org_id, blocked = _gate(request)
    if blocked:
        return blocked
    with Session(engine) as s:
        n = _int_id(pid)
        proj = _project_in_org(s, n, org_id, ident) if n is not None else None
        if proj is None:
            return JSONResponse(_NOT_FOUND, status_code=404)
        return JSONResponse({"ok": True, **notify.effective(s, proj)})


@api_router.post("/api/notify/projects/{pid}/mute")
async def api_notify_project_mute(request: Request, pid: str):
    """`{muted: true|false}`: a muted project posts to no channel at all."""
    ident, org_id, blocked = _gate(request)
    if blocked:
        return blocked
    with Session(engine) as s:
        n = _int_id(pid)
        proj = _project_in_org(s, n, org_id, ident) if n is not None else None
        if proj is None:
            return JSONResponse(_NOT_FOUND, status_code=404)
        blocked = _manage_block(ident)
        if blocked:
            return blocked
        body = await _json(request)
        if not isinstance(body.get("muted"), bool):
            return _err("muted must be true or false.")
        notify.set_project_muted(s, proj, body["muted"], actor=ident["actor"], ip=deps._ip(request))
        return JSONResponse({"ok": True, **notify.effective(s, proj)})


# --------------------------------------------------------------------------------------------
# web
# --------------------------------------------------------------------------------------------
def _manager_block(request: Request):
    me = security.session_user(request)
    if security.auth_required() and not security.can_manage(me):
        return None, deps._error_page(
            request, 403, "Notifications are limited to managers and admins."
        )
    return me, None


def _web_channel(request, session, cid):
    ch = notify.visible(
        session, cid, org_id=deps._my_org_id(request), customer_id=deps._my_customer_id(request)
    )
    if ch is None:
        return None, deps._error_page(request, 404, "Channel not found.")
    return ch, None


def _safe_back(back: str, fallback: str) -> str:
    back = (back or "").strip()
    return fallback if not back or oidc.is_unsafe_local_target(back) else back


def _done(back: str, query: str) -> RedirectResponse:
    sep = "&" if "?" in back else "?"
    return RedirectResponse(f"{back}{sep}{query}", status_code=303)


@router.get("/settings/notifications")
def notifications_page(request: Request, session: Session = Depends(get_session)):
    _me, blocked = _manager_block(request)
    if blocked:
        return blocked
    org_id = deps._my_org_id(request)
    cust = deps._my_customer_id(request)
    channels = notify.list_channels(session, org_id=org_id, customer_id=cust)
    rows = [notify.to_dict(session, c) for c in channels]
    by_id = {c.id: c for c in channels}
    routes = []
    for c in channels:
        for r in session.exec(select(NotifyRoute).where(NotifyRoute.channel_id == c.id)).all():
            d = notify.route_to_dict(r, c)
            if r.scope == "project":
                p = session.get(Project, ids.coerce_id(r.scope_ref))
                d["scope_label"] = f"project {p.name}" if p else f"project #{r.scope_ref}"
            elif r.scope == "customer":
                cu = session.get(Customer, ids.coerce_id(r.scope_ref))
                d["scope_label"] = f"customer {cu.name}" if cu else f"customer #{r.scope_ref}"
            elif r.scope == "org":
                d["scope_label"] = "org default"
            else:
                d["scope_label"] = f"{r.scope.replace('_', ' ')} {r.scope_ref}"
            routes.append(d)
    deliveries = notify.deliveries(
        session, org_id=org_id, channel_ids=[c.id for c in channels], limit=25
    )
    for d in deliveries:
        d["channel"] = getattr(by_id.get(d["channel_id"]), "name", f"#{d['channel_id']}")
    _had, secret = one_time_stash.pop(request, "notify_secret_ref")
    _had_t, test_msg = one_time_stash.pop(request, "notify_test_ref")
    qp = request.query_params
    return deps.templates.TemplateResponse(
        request,
        "notifications.html",
        {
            "request": request,
            "channels": rows,
            "routes": routes,
            "deliveries": deliveries,
            "events": notify.EVENTS,
            "event_labels": notify.EVENT_LABELS,
            "default_on": sorted(notify.DEFAULT_ON),
            "kinds": notify.NotifyChannelKind.ALL,
            "outbound_enabled": notify.outbound_enabled(),
            "new_secret": secret,
            "saved": qp.get("saved"),
            "err": qp.get("err"),
            "tested": qp.get("tested"),
            "test_message": test_msg or "",
        },
    )


@router.post("/settings/notifications/channels")
def notifications_channel_create(
    request: Request,
    kind: str = Form(""),
    name: str = Form(""),
    url: str = Form(""),
    recipients: str = Form(""),
    session: Session = Depends(get_session),
):
    _me, blocked = _manager_block(request)
    if blocked:
        return blocked
    try:
        _ch, shown = notify.create_channel(
            session,
            kind=kind.strip().lower(),
            name=name,
            url=url,
            recipients=recipients,
            customer_id=deps._my_customer_id(request),
            org_id=deps._my_org_id(request),
            created_by=deps._actor_email(request),
            ip=deps._ip(request),
        )
    except notify.NotifyError as exc:
        return RedirectResponse("/settings/notifications?err=" + quote(str(exc)), status_code=303)
    if shown:
        one_time_stash.put(request, "notify_secret_ref", shown)
    return RedirectResponse("/settings/notifications?saved=1", status_code=303)


@router.post("/settings/notifications/channels/{cid}/update")
def notifications_channel_update(
    cid: int,
    request: Request,
    name: str = Form(""),
    url: str = Form(""),
    recipients: str = Form(""),
    enabled: str = Form(""),
    rotate: str = Form(""),
    session: Session = Depends(get_session),
):
    _me, blocked = _manager_block(request)
    if blocked:
        return blocked
    ch, blocked = _web_channel(request, session, cid)
    if blocked:
        return blocked
    if not notify.can_modify(ch, _me, deps._actor_email(request)):
        return deps._error_page(
            request, 403, "Only the channel's creator or an admin can change or delete it."
        )
    try:
        shown = notify.update_channel(
            session,
            ch,
            name=name.strip() or None,
            url=url.strip() or None,
            recipients=recipients.strip() or None,
            enabled=(enabled == "1") if enabled in ("0", "1") else None,
            rotate_signing_secret=rotate == "1",
            actor=deps._actor_email(request),
            ip=deps._ip(request),
        )
    except notify.NotifyError as exc:
        return RedirectResponse("/settings/notifications?err=" + quote(str(exc)), status_code=303)
    if shown:
        one_time_stash.put(request, "notify_secret_ref", shown)
    return RedirectResponse("/settings/notifications?saved=1", status_code=303)


@router.post("/settings/notifications/channels/{cid}/test")
def notifications_channel_test(cid: int, request: Request, session: Session = Depends(get_session)):
    _me, blocked = _manager_block(request)
    if blocked:
        return blocked
    ch, blocked = _web_channel(request, session, cid)
    if blocked:
        return blocked
    res = notify.test_send(session, ch, actor=deps._actor_email(request), ip=deps._ip(request))
    one_time_stash.put(request, "notify_test_ref", res["message"])
    return RedirectResponse(
        f"/settings/notifications?tested={'1' if res['ok'] else '0'}", status_code=303
    )


@router.post("/settings/notifications/channels/{cid}/retry")
def notifications_channel_retry(
    cid: int, request: Request, session: Session = Depends(get_session)
):
    _me, blocked = _manager_block(request)
    if blocked:
        return blocked
    ch, blocked = _web_channel(request, session, cid)
    if blocked:
        return blocked
    notify.retry_failed(session, ch, actor=deps._actor_email(request))
    return RedirectResponse("/settings/notifications?saved=1", status_code=303)


@router.post("/settings/notifications/channels/{cid}/delete")
def notifications_channel_delete(
    cid: int, request: Request, session: Session = Depends(get_session)
):
    _me, blocked = _manager_block(request)
    if blocked:
        return blocked
    ch, blocked = _web_channel(request, session, cid)
    if blocked:
        return blocked
    if not notify.can_modify(ch, _me, deps._actor_email(request)):
        return deps._error_page(
            request, 403, "Only the channel's creator or an admin can change or delete it."
        )
    notify.delete_channel(session, ch, actor=deps._actor_email(request), ip=deps._ip(request))
    return RedirectResponse("/settings/notifications?saved=1", status_code=303)


@router.post("/settings/notifications/routes")
async def notifications_route_set(request: Request, session: Session = Depends(get_session)):
    _me, blocked = _manager_block(request)
    if blocked:
        return blocked
    form = await request.form()
    back = _safe_back(str(form.get("back") or ""), "/settings/notifications")
    ch, blocked = _web_channel(request, session, form.get("channel_id"))
    if blocked:
        return blocked
    ident = {
        "user": security.session_user(request),
        "_cust_id": deps._my_customer_id(request),
        "actor": deps._actor_email(request),
    }
    scope = str(form.get("scope") or "")
    proj, ref, bad = _route_scope_ok(
        session, scope, form.get("ref"), deps._my_org_id(request), ident
    )
    if bad is not None:
        if bad.status_code == 404:
            return deps._error_page(request, 404, "Not found.")
        return _done(back, "err=" + quote("That scope is not available to you."))
    try:
        notify.set_route(
            session,
            scope=scope.strip().lower(),
            ref=ref,
            channel=ch,
            events=[str(e) for e in form.getlist("events")],
            project=proj,
            created_by=deps._actor_email(request),
            ip=deps._ip(request),
        )
    except notify.NotifyError as exc:
        return _done(back, "err=" + quote(str(exc)))
    return _done(back, "saved=1")


@router.post("/settings/notifications/routes/{rid}/delete")
def notifications_route_clear(rid: int, request: Request, session: Session = Depends(get_session)):
    _me, blocked = _manager_block(request)
    if blocked:
        return blocked
    row = session.get(NotifyRoute, rid)
    ch = None
    if row is not None:
        ch, blocked = _web_channel(request, session, row.channel_id)
        if blocked:
            return blocked
    if row is None or ch is None:
        return deps._error_page(request, 404, "Route not found.")
    if _org_route_admin_only(row, _me):
        return deps._error_page(request, 403, "Removing an org-wide route is limited to admins.")
    notify.clear_route(
        session,
        row,
        actor=deps._actor_email(request),
        ip=deps._ip(request),
        customer_id=ch.customer_id,
    )
    form_back = _safe_back(request.query_params.get("back", ""), "/settings/notifications")
    return _done(form_back, "saved=1")


@router.post("/settings/notifications/mute")
async def notifications_project_mute(request: Request, session: Session = Depends(get_session)):
    _me, blocked = _manager_block(request)
    if blocked:
        return blocked
    form = await request.form()
    pid = ids.coerce_id(form.get("project_id"))
    proj = session.get(Project, pid) if pid is not None else None
    me = security.session_user(request)
    if proj is None or not access.can_view_project(session, me, proj):
        return deps._error_page(request, 404, "Project not found.")
    if deps._org_block(request, proj.org_id) is not None:
        return deps._org_block(request, proj.org_id)
    cust = deps._my_customer_id(request)
    if cust is not None and proj.customer_id != cust:
        return deps._error_page(request, 404, "Project not found.")
    notify.set_project_muted(
        session,
        proj,
        str(form.get("muted")) == "1",
        actor=deps._actor_email(request),
        ip=deps._ip(request),
    )
    return _done(_safe_back(str(form.get("back") or ""), f"/projects/{proj.id}"), "saved=1")
