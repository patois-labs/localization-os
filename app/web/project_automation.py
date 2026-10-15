"""Project automation: the project's Automation page and the /api routes behind it.

One service (app/services/project_automation.py) behind every door, so the rules cannot drift:

  * web `router`: GET /projects/{id}/automation (content source, schedule, run history) and
    the POSTs it submits (bind, unbind, schedule, pause, resume, run now). Session user, CSRF.
  * `api_router`: the same capabilities for tokens (docs scope, no session read):
      GET  /api/projects/{id}/automation          the binding, schedule and state
      PUT  /api/projects/{id}/automation          change the schedule and run settings
      POST /api/projects/{id}/source              bind a connection (pick, create or clone)
      POST /api/projects/{id}/source/unbind       remove the binding
      POST /api/projects/{id}/automation/pause    pause (reason optional)
      POST /api/projects/{id}/automation/resume   resume
      POST /api/projects/{id}/automation/handover hand claimed items over to / take them from
                                                  another project (body: to_project_id OR
                                                  from_project_id)
      POST /api/projects/{id}/runs                run now (202 + job id, deduplicated)
      GET  /api/projects/{id}/runs                run history

Hand-over: the project in the URL and the other project must both be visible to the caller, and the
caller must be a manager or admin (a manager of the owning project, or an admin). A project the
caller cannot see answers like a missing one.

Permissions: anyone who can see the project may read it (cost figures manager and up, ADR 0042);
changing anything and running needs manager and up on the project. Scope settings beyond
narrowing, and Jira schedules, are admin only. A project or connection the caller cannot see
answers exactly like a missing one (404 before 403, ADR 0010).

Documented in: docs/architecture/project-automation.md
"""

import json
from urllib.parse import quote

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import JSONResponse, RedirectResponse
from sqlmodel import Session

from app.db import engine, get_session
from app.kernel import (
    access,
    audit,
    automation_schedule,
    connected_projects,
    project_automation,
    project_budgets,
    security,
    settings,
)
from app.kernel import connectors as connectors_registry
from app.kernel.connectors import store as conn_store
from app.models import Connection, Project
from app.web.api_cli_common import (
    _NOT_FOUND,
    _gate,
    _int_id,
    _json_object,
    _manage_block,
    _project_in_org,
)
from app.web.deps import (
    _actor_email,
    _error_page,
    _missing_block,
    _visibility_block,
    templates,
)

router = APIRouter()
api_router = APIRouter()

AutomationError = project_automation.AutomationError


def _view_for(project_id: int, user) -> dict:
    """The automation view for a caller: other projects are only named when the caller can see
    them (a global connection can feed projects of other customers)."""
    with Session(engine) as s:
        row = project_automation.get(s, project_id)
        return project_automation.view(
            s,
            row,
            can_see_cost=_can_cost(user),
            visible=lambda pid: access.can_view_project(s, user, pid),
        )


def _fail(exc: "AutomationError") -> JSONResponse:
    return JSONResponse({"ok": False, "error": str(exc)}, status_code=exc.status)


def _admin_for(user) -> bool:
    """Admin for the scope rules. With sign-in off the one local operator counts as admin
    (the same posture as every other admin-only setting on a single-operator install)."""
    return (not security.auth_required()) or security.is_admin(user)


def _can_cost(user) -> bool:
    return (not security.auth_required()) or security.can_manage(user)


def _pick_list(project: Project, session: Session, user) -> list[dict]:
    """The connections this project may pick as its content source (never a lane). Another
    project that a connection feeds is only flagged when the caller can see that project."""
    out = []
    for c in conn_store.list_connections(customer_id=project.customer_id):
        if project_automation.connection_visible_to_project(c, project):
            feeds = c.project_id is not None
            feeds = feeds and access.can_view_project(session, user, c.project_id)
            out.append(
                {
                    "id": c.id,
                    "name": c.name,
                    "kind": c.kind,
                    "status": c.status,
                    "feeds_project_id": c.project_id if feeds else None,
                }
            )
    return out


def _parse_scope_text(text: str) -> dict:
    """`key=value` per line (or a JSON object) -> scope dict. Raises AutomationError."""
    text = (text or "").strip()
    if not text:
        return {}
    if text.startswith("{"):
        try:
            obj = json.loads(text)
        except ValueError as exc:
            raise AutomationError("Scope is not valid JSON.", 400) from exc
        if not isinstance(obj, dict):
            raise AutomationError("Scope must be a JSON object.", 400)
        return obj
    out = {}
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        if "=" not in line:
            raise AutomationError(f"'{line}' is not key=value.", 400)
        k, v = line.split("=", 1)
        out[k.strip()] = v.strip()
    return out


# =====================================================================================
# /api
# =====================================================================================
def _api_project_read(request: Request, pid):
    """Common preamble for a read. Returns (ident, project_id, blocked): the project ring."""
    ident, org_id, blocked = _gate(request)
    if blocked:
        return None, None, blocked
    with Session(engine) as s:
        project = _project_in_org(s, _int_id(pid), org_id, ident)
        if project is None:
            return None, None, JSONResponse(_NOT_FOUND, status_code=404)
        project_id = project.id
    ident["_org_id"] = org_id
    return ident, project_id, None


def _api_project_write(request: Request, pid):
    """Common preamble for a change: the project ring FIRST (404 before 403), then manager."""
    ident, project_id, blocked = _api_project_read(request, pid)
    if blocked:
        return None, None, blocked
    b = _manage_block(ident)
    if b:
        return None, None, b
    return ident, project_id, None


@api_router.get("/api/projects/{pid}/automation")
def api_automation_get(pid: int, request: Request):
    ident, project_id, blocked = _api_project_read(request, pid)
    if blocked:
        return blocked
    out = _view_for(project_id, ident.get("user"))
    with Session(engine) as s:
        row = project_automation.get(s, project_id)
        if row is not None:
            out["active_job_id"] = project_automation.active_job(s, row)
    out["scheduler_enabled"] = project_automation.global_switch_on()
    if not out.get("bound"):
        # A connected project that has no source yet: say so, and where a person connects one.
        with Session(engine) as s:
            proj = s.get(Project, project_id)
            out["intake"] = proj.intake if proj is not None else "upload"
            if proj is not None and connected_projects.is_connected(proj):
                out["setup_url"] = connected_projects.setup_url(project_id)
    return JSONResponse({"ok": True, **out})


@api_router.put("/api/projects/{pid}/automation")
async def api_automation_set(pid: int, request: Request):
    """Change the schedule and run settings; only the fields sent change. Body: enabled,
    mode (delta|full), times (["09:00"] UTC), days (["Mon"]), interval_minutes,
    auto_pause_after, run_cap_usd (null clears), move_schedule."""
    ident, project_id, blocked = _api_project_write(request, pid)
    if blocked:
        return blocked
    body, bad = await _json_object(request)
    if bad:
        return bad
    kwargs = {
        k: body[k] for k in ("enabled", "mode", "times", "days", "interval_minutes") if k in body
    }
    if "auto_pause_after" in body:
        kwargs["auto_pause_after"] = body["auto_pause_after"]
    if "run_cap_usd" in body:
        kwargs["run_cap_usd"] = body["run_cap_usd"]
    try:
        project_automation.configure(
            project_id,
            actor=ident.get("actor", ""),
            is_admin=_admin_for(ident.get("user")),
            move_schedule=bool(body.get("move_schedule")),
            **kwargs,
        )
    except AutomationError as exc:
        return _fail(exc)
    return JSONResponse({"ok": True, **_view_for(project_id, ident.get("user"))})


def _new_connection_block(ident: dict, nc: dict):
    """The SAME checks as POST /api/connectors (api_connector_create), so creating a connection
    while binding is no shortcut: can_tune_ai (admin or linguist), no credential in the body (those
    ride the admin credential routes), a known, enabled, non-archived kind, the Jira schedule
    rule, gdocs impersonation and the scope / live-edit admin rule. Returns a response or None."""
    from app.web.api_discovery import _jira_schedule_admin_block
    from app.web.api_part2 import _conn_role_block, _reject_connector_secret

    blocked = _conn_role_block(ident, security.can_tune_ai)
    if blocked:
        return blocked
    blocked = _reject_connector_secret(nc)
    if blocked:
        return blocked
    kind = str(nc.get("kind") or "").strip().lower()
    if not kind or not str(nc.get("name") or "").strip():
        return JSONResponse({"ok": False, "error": "kind and name are required."}, status_code=400)
    if kind not in {s.kind for s in connectors_registry.all_specs()} or not (
        connectors_registry.kind_enabled(kind)
    ):
        return JSONResponse(
            {"ok": False, "error": f"unknown connector kind: {kind}"}, status_code=400
        )
    if connectors_registry.is_archived(kind):
        return JSONResponse(
            {
                "ok": False,
                "error": f"the {kind} connector is archived and no longer accepts new connections.",
            },
            status_code=400,
        )
    cfg = nc.get("config")
    if cfg is not None and not isinstance(cfg, dict):
        return JSONResponse({"ok": False, "error": "config must be an object."}, status_code=400)
    cfg = cfg or {}
    blocked = _jira_schedule_admin_block(ident, kind, cfg=cfg)
    if blocked:
        return blocked
    is_admin = _admin_for(ident.get("user"))
    imp_err = conn_store.gdocs_impersonation_error(kind, {}, cfg, caller_is_admin=is_admin)
    if imp_err:
        return JSONResponse({"ok": False, "error": imp_err}, status_code=403)
    scope_err = conn_store.scope_admin_error(
        kind, None, cfg, caller_is_admin=is_admin, actor=ident.get("actor") or ""
    )
    if scope_err:
        return JSONResponse({"ok": False, "error": scope_err}, status_code=403)
    return None


@api_router.post("/api/projects/{pid}/source")
async def api_automation_bind(pid: int, request: Request):
    """Bind a content source. Body: connection_id (pick an existing connection) OR
    new_connection {kind, name, config} (create one WITHOUT a credential, under the same rules as
    POST /api/connectors: admin or linguist; the credential is set on the connection afterwards),
    clone (true = the project gets its own copy of the picked connection), scope ({setting:
    value} narrowing this project)."""
    from app.web.api_cli_misc_6 import _connection_in_org

    ident, project_id, blocked = _api_project_write(request, pid)
    if blocked:
        return blocked
    body, bad = await _json_object(request)
    if bad:
        return bad
    cid = body.get("connection_id")
    new_conn = body.get("new_connection")
    if cid is not None:
        with Session(engine) as s:
            if _connection_in_org(s, _int_id(cid), ident.get("_org_id"), ident) is None:
                return JSONResponse(_NOT_FOUND, status_code=404)
    elif not isinstance(new_conn, dict):
        return JSONResponse(
            {"ok": False, "error": "Send connection_id or new_connection."}, status_code=400
        )
    else:
        refused = _new_connection_block(ident, new_conn)
        if refused:
            return refused
    scope = body.get("scope") or {}
    if not isinstance(scope, dict):
        return JSONResponse({"ok": False, "error": "scope must be an object."}, status_code=400)
    try:
        project_automation.bind(
            project_id,
            _int_id(cid) if cid is not None else None,
            actor=ident.get("actor", ""),
            is_admin=_admin_for(ident.get("user")),
            clone=bool(body.get("clone")),
            scope=scope,
            new_connection=new_conn if isinstance(new_conn, dict) else None,
            org_id=ident.get("_org_id"),
            bound_customer_id=ident.get("_cust_id"),
        )
    except AutomationError as exc:
        return _fail(exc)
    return JSONResponse({"ok": True, **_view_for(project_id, ident.get("user"))}, status_code=201)


@api_router.post("/api/projects/{pid}/source/unbind")
def api_automation_unbind(pid: int, request: Request):
    ident, project_id, blocked = _api_project_write(request, pid)
    if blocked:
        return blocked
    try:
        out = project_automation.unbind(project_id, actor=ident.get("actor", ""))
    except AutomationError as exc:
        return _fail(exc)
    return JSONResponse({"ok": True, **out})


@api_router.post("/api/projects/{pid}/automation/pause")
async def api_automation_pause(pid: int, request: Request):
    ident, project_id, blocked = _api_project_write(request, pid)
    if blocked:
        return blocked
    body, bad = await _json_object(request)
    if bad:
        return bad
    try:
        project_automation.pause(
            project_id, reason=str(body.get("reason") or ""), actor=ident.get("actor", "")
        )
    except AutomationError as exc:
        return _fail(exc)
    return JSONResponse({"ok": True, **_view_for(project_id, ident.get("user"))})


@api_router.post("/api/projects/{pid}/automation/resume")
def api_automation_resume(pid: int, request: Request):
    ident, project_id, blocked = _api_project_write(request, pid)
    if blocked:
        return blocked
    try:
        project_automation.resume(project_id, actor=ident.get("actor", ""))
    except AutomationError as exc:
        return _fail(exc)
    return JSONResponse({"ok": True, **_view_for(project_id, ident.get("user"))})


@api_router.post("/api/projects/{pid}/runs")
async def api_automation_run(pid: int, request: Request):
    """Run now. Body (optional): mode (delta|full). Always enqueued: 202 + job id; a run
    already in flight for the connection is handed back (existing: true). Spends money."""
    ident, project_id, blocked = _api_project_write(request, pid)
    if blocked:
        return blocked
    body, bad = await _json_object(request)
    if bad:
        return bad
    with Session(engine) as s:
        row = project_automation.get(s, project_id)
        conn = s.get(Connection, row.run_connection_id) if row else None
        if conn is not None and conn.kind == "jira" and not _admin_for(ident.get("user")):
            return JSONResponse(
                {"ok": False, "error": "Running a Jira batch needs an admin account."},
                status_code=403,
            )
    mode = body.get("mode")
    try:
        jid, created = project_automation.run_now(
            project_id,
            actor=ident.get("actor", ""),
            mode=str(mode) if mode is not None else None,
            trigger="manual",
        )
    except AutomationError as exc:
        return _fail(exc)
    out = {"ok": True, "job_id": jid, "status_url": f"/api/jobs/{jid}"}
    if not created:
        out["existing"] = True
    return JSONResponse(out, status_code=202)


def _handover(
    project_id: int, other_raw, *, give: bool, actor: str, user, org_id=None, ident=None
) -> dict:
    """The one hand-over path behind the API and the web form. `give` True = the page project
    owns the items and gives them to `other`; False = the page project takes them from `other`.
    The other project must be one the caller can see (ring, org, customer, membership): an
    invisible one answers like a missing one. Raises AutomationError."""
    other_id = _int_id(other_raw)
    if other_id is None:
        raise AutomationError("Name the other project.", 400)
    with Session(engine) as s:
        other = _project_in_org(s, other_id, org_id, ident or {"user": user})
        if other is None:
            raise AutomationError("No such project.", 404)
        other_id = other.id
    owner, receiver = (project_id, other_id) if give else (other_id, project_id)
    return project_automation.hand_over(owner, receiver, actor=actor)


@api_router.post("/api/projects/{pid}/automation/handover")
async def api_automation_handover(pid: int, request: Request):
    """Hand claimed items over. Body: to_project_id (this project owns them and gives them to
    that project) OR from_project_id (this project takes them from that project). The receiving
    project then owns the pairs it was blocked on. Manager or admin."""
    ident, project_id, blocked = _api_project_write(request, pid)
    if blocked:
        return blocked
    body, bad = await _json_object(request)
    if bad:
        return bad
    to_id, from_id = body.get("to_project_id"), body.get("from_project_id")
    if (to_id is None) == (from_id is None):
        return JSONResponse(
            {"ok": False, "error": "Send to_project_id or from_project_id (one of them)."},
            status_code=400,
        )
    try:
        out = _handover(
            project_id,
            to_id if to_id is not None else from_id,
            give=to_id is not None,
            actor=ident.get("actor", ""),
            user=ident.get("user"),
            org_id=ident.get("_org_id"),
            ident=ident,
        )
    except AutomationError as exc:
        return _fail(exc)
    return JSONResponse({"ok": True, **out})


@api_router.get("/api/projects/{pid}/runs")
def api_automation_runs(pid: int, request: Request, limit: int = 20):
    ident, project_id, blocked = _api_project_read(request, pid)
    if blocked:
        return blocked
    with Session(engine) as s:
        try:
            runs = project_automation.history(
                s, project_id, limit=limit, can_see_cost=_can_cost(ident.get("user"))
            )
        except AutomationError as exc:
            return _fail(exc)
    return JSONResponse({"ok": True, "project_id": project_id, "runs": runs})


# =====================================================================================
# web
# =====================================================================================
def _redirect(project_id: int, ok: bool, msg: str, return_to: str = "") -> RedirectResponse:
    """Back to the Automation page with a flash, or, for a form that carried a valid `return_to`
    (the connected project's setup menu and Source card post here), back to that project path
    with the flash as `src_ok` / `src_msg` and the Source card anchor."""
    rt = connected_projects.safe_return_to(return_to)
    if rt:
        path = rt.split("#", 1)[0]
        return RedirectResponse(
            connected_projects.with_params(
                path + "#source", src_ok=1 if ok else 0, src_msg=msg[:240]
            ),
            status_code=303,
        )
    return RedirectResponse(
        f"/projects/{project_id}/automation?ok={1 if ok else 0}&msg={quote(msg[:240])}",
        status_code=303,
    )


def _web_project(request: Request, session: Session, project_id: int, *, write: bool):
    """(project, me, blocked). Visibility ring first, then the manager gate for writes."""
    project = session.get(Project, project_id)
    if project is None:
        return None, None, _missing_block(request, session)
    blocked = _visibility_block(request, session, project)
    if blocked:
        return None, None, blocked
    me = security.session_user(request)
    if write and security.auth_required() and not security.can_manage(me):
        return (
            None,
            None,
            _error_page(
                request, 403, "Changing a project's automation needs a manager or admin account."
            ),
        )
    return project, me, None


@router.get("/projects/{project_id}/automation")
def automation_page(project_id: int, request: Request, session: Session = Depends(get_session)):
    project, me, blocked = _web_project(request, session, project_id, write=False)
    if blocked:
        return blocked
    can_manage = (not security.auth_required()) or security.can_manage(me)
    row = project_automation.get(session, project.id)
    auto = project_automation.view(
        session,
        row,
        can_see_cost=_can_cost(me),
        visible=lambda pid: access.can_view_project(session, me, pid),
    )
    runs = (
        project_automation.history(session, project.id, limit=20, can_see_cost=_can_cost(me))
        if row
        else []
    )
    budget = None
    if _can_cost(me):
        budget = project_budgets.get_project_budget(session, project.id)
    sp = connectors_registry.spec(auto.get("connection_kind", "")) if row else None
    return templates.TemplateResponse(
        request,
        "project_automation.html",
        {
            "request": request,
            "project": project,
            "auto": auto,
            "runs": runs,
            "can_manage": can_manage,
            "can_cost": _can_cost(me),
            "is_admin": _admin_for(me),
            "choices": _pick_list(project, session, me) if (can_manage and not row) else [],
            "scheduler_enabled": project_automation.global_switch_on(),
            "active_job_id": project_automation.active_job(session, row) if row else None,
            "budget_limit": getattr(budget, "monthly_usd_limit", 0) if budget else 0,
            "days": automation_schedule.DAY_NAMES,
            "scope_keys": sorted(
                set(project_automation.NARROW_KEYS.get(auto.get("connection_kind", ""), ()))
            )
            if row
            else [],
            "kind_label": sp.name if sp else "",
            "ok": request.query_params.get("ok"),
            "msg": request.query_params.get("msg", ""),
            "settings_hint": settings.get("sync_scheduler_enabled", "1") == "1",
        },
    )


def _web_write(request: Request, project_id: int):
    with Session(engine) as session:
        project, me, blocked = _web_project(request, session, project_id, write=True)
        if blocked:
            return None, None, blocked
        return project.id, me, None


@router.post("/projects/{project_id}/automation/bind")
def automation_bind(
    project_id: int,
    request: Request,
    connection_id: str = Form(""),
    clone: str = Form(""),
    scope: str = Form(""),
    return_to: str = Form(""),
):
    pid, me, blocked = _web_write(request, project_id)
    if blocked:
        return blocked
    cid = _int_id(connection_id)
    if cid is None:
        return _redirect(pid, False, "Pick a connection.", return_to)
    try:
        project_automation.bind(
            pid,
            cid,
            actor=_actor_email(request),
            is_admin=_admin_for(me),
            clone=clone in ("1", "on", "true"),
            scope=_parse_scope_text(scope),
            org_id=getattr(me, "org_id", None),
        )
    except AutomationError as exc:
        return _redirect(pid, False, str(exc), return_to)
    return _redirect(pid, True, "Content source set.", return_to)


@router.post("/projects/{project_id}/automation/unbind")
def automation_unbind(project_id: int, request: Request, return_to: str = Form("")):
    pid, _me, blocked = _web_write(request, project_id)
    if blocked:
        return blocked
    try:
        project_automation.unbind(pid, actor=_actor_email(request))
    except AutomationError as exc:
        return _redirect(pid, False, str(exc), return_to)
    return _redirect(pid, True, "Content source removed.", return_to)


@router.post("/projects/{project_id}/automation/schedule")
async def automation_schedule_save(project_id: int, request: Request):
    pid, me, blocked = _web_write(request, project_id)
    if blocked:
        return blocked
    form = await request.form()
    kind = (form.get("kind") or "times").strip()
    kwargs: dict = {}
    if kind == "interval":
        kwargs["interval_minutes"] = (form.get("interval_minutes") or "0").strip() or "0"
        kwargs["times"] = []
        kwargs["days"] = []
    else:
        kwargs["times"] = (form.get("times") or "").strip()
        kwargs["days"] = list(form.getlist("days"))
        kwargs["interval_minutes"] = 0
    kwargs["mode"] = (form.get("mode") or "delta").strip()
    kwargs["enabled"] = form.get("enabled") in ("1", "on", "true")
    pause_after = (form.get("auto_pause_after") or "").strip()
    if pause_after:
        kwargs["auto_pause_after"] = pause_after
    kwargs["run_cap_usd"] = (form.get("run_cap_usd") or "").strip() or None
    try:
        project_automation.configure(
            pid,
            actor=_actor_email(request),
            is_admin=_admin_for(me),
            move_schedule=form.get("move_schedule") in ("1", "on", "true"),
            **kwargs,
        )
    except AutomationError as exc:
        return _redirect(pid, False, str(exc), form.get("return_to") or "")
    return _redirect(pid, True, "Schedule saved.", form.get("return_to") or "")


@router.post("/projects/{project_id}/automation/handover")
def automation_handover(
    project_id: int,
    request: Request,
    to_project_id: str = Form(""),
    from_project_id: str = Form(""),
):
    pid, me, blocked = _web_write(request, project_id)
    if blocked:
        return blocked
    if bool(to_project_id.strip()) == bool(from_project_id.strip()):
        return _redirect(pid, False, "Pick the project to hand over to, or to take from.")
    try:
        out = _handover(
            pid,
            to_project_id or from_project_id,
            give=bool(to_project_id.strip()),
            actor=_actor_email(request),
            user=me,
            org_id=None,  # can_view_project applies the org ring
        )
    except AutomationError as exc:
        return _redirect(pid, False, str(exc))
    return _redirect(
        pid,
        True,
        f"Handed {out['pairs']} item(s) over: {out['to_name']} now owns them.",
    )


@router.post("/projects/{project_id}/automation/pause")
def automation_pause(
    project_id: int, request: Request, reason: str = Form(""), return_to: str = Form("")
):
    pid, _me, blocked = _web_write(request, project_id)
    if blocked:
        return blocked
    try:
        project_automation.pause(pid, reason=reason, actor=_actor_email(request))
    except AutomationError as exc:
        return _redirect(pid, False, str(exc), return_to)
    return _redirect(pid, True, "Paused.", return_to)


@router.post("/projects/{project_id}/automation/resume")
def automation_resume(project_id: int, request: Request, return_to: str = Form("")):
    pid, _me, blocked = _web_write(request, project_id)
    if blocked:
        return blocked
    try:
        project_automation.resume(pid, actor=_actor_email(request))
    except AutomationError as exc:
        return _redirect(pid, False, str(exc), return_to)
    return _redirect(pid, True, "Resumed.", return_to)


@router.post("/projects/{project_id}/automation/run")
def automation_run(
    project_id: int, request: Request, mode: str = Form(""), return_to: str = Form("")
):
    pid, me, blocked = _web_write(request, project_id)
    if blocked:
        return blocked
    with Session(engine) as s:
        row = project_automation.get(s, pid)
        conn = s.get(Connection, row.run_connection_id) if row else None
        if conn is not None and conn.kind == "jira" and not _admin_for(me):
            return _redirect(pid, False, "Running a Jira batch needs an admin account.", return_to)
    try:
        jid, created = project_automation.run_now(
            pid, actor=_actor_email(request), mode=mode or None, trigger="manual"
        )
    except AutomationError as exc:
        return _redirect(pid, False, str(exc), return_to)
    audit.log(
        actor=_actor_email(request) or "system",
        action="automation.run_clicked",
        target=f"project:{pid}",
        detail=f"job:{jid}{' (already running)' if not created else ''}",
    )
    return _redirect(
        pid,
        True,
        f"A run is already in progress (job {jid})."
        if not created
        else f"Run started (job {jid}).",
        return_to,
    )


@router.post("/projects/{project_id}/intake")
def project_set_intake(
    project_id: int, request: Request, intake: str = Form(""), return_to: str = Form("")
):
    """Connected projects: switch an EMPTY project between uploads and a connected source (the
    setup menu's "use uploads instead"). Same rule as POST /api/projects/{pid}/intake: only
    while the project has no documents, no keys and no bound source. Manager and up."""
    pid, _me, blocked = _web_write(request, project_id)
    if blocked:
        return blocked
    with Session(engine) as s:
        project = s.get(Project, pid)
        try:
            connected_projects.set_intake(s, project, intake, actor=_actor_email(request))
        except connected_projects.IntakeError as exc:
            return _redirect(pid, False, str(exc), return_to or f"/projects/{pid}")
    return RedirectResponse(f"/projects/{pid}", status_code=303)


@router.post("/projects/{project_id}/make-connected")
def project_make_connected(project_id: int, request: Request, connection_id: str = Form("")):
    """ "Make this a connected project" (manager and up): an existing project that a connection
    already feeds becomes a connected project bound to that same connection. Nothing is
    scheduled or run. A refusal lands on the Automation page with its reason."""
    pid, _me, blocked = _web_write(request, project_id)
    if blocked:
        return blocked
    with Session(engine) as s:
        project = s.get(Project, pid)
        try:
            connected_projects.convert_existing(
                s, project, connection_id.strip() or None, actor=_actor_email(request)
            )
        except connected_projects.IntakeError as exc:
            return _redirect(pid, False, str(exc))
    return _redirect(
        pid, True, "This is now a connected project. Nothing is scheduled.", f"/projects/{pid}"
    )
