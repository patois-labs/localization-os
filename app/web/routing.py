"""Admin CRUD for content routing rules (MCP Phase B chunk 1).

Admin-gated (_admin_block). Follows the settings_save/users_create form pattern:
CSRF-protected form POSTs (the _csrf.html include is in routing.html), audited with
actor + ip, RedirectResponse(303) after mutations. validate_rule rejects a rule
whose workflow contains a human_review step at create time.

Documented in: docs/architecture/cost-and-budgets.md
"""

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse
from sqlmodel import Session, select

from app.db import get_session
from app.kernel import (
    access,
    budgets,
    ids,
    project_budgets,
    security,
    spend,
)
from app.kernel import (
    routing as routing_svc,
)
from app.models import DepartmentBudget, Project, ProjectBudget, RoutingRule
from app.web.deps import (
    _admin_block,
    _error_page,
    _ip,
    _my_org_id,
    _visibility_block,
    feature_block,
    templates,
)
from app.web.form_errors import redirect_with_error

router = APIRouter()


def _int_or_none(v: str):
    # coerce_id is the app-wide id law: a malformed referent (a Unicode digit int()
    # rejects, or an over-range value SQLite would overflow) resolves to None -> a null
    # rule field that validate_rule then rejects cleanly, never a 500 at int()/the bind.
    return ids.coerce_id(v)


@router.get("/routing")
def routing_index(request: Request, session: Session = Depends(get_session)):
    # ffadmin Wave 4 (layer 2): flag first. NOTE the middleware (layer 1) 404s /routing
    # by longest match while /routing/budgets stays reachable (department budgets are
    # CORE, not this feature -- CORE_WEB carves them out). The budget routes below are
    # deliberately NOT feature_block'd.
    fblock = feature_block(request, "routing")
    if fblock:
        return fblock
    _me, blocked = _admin_block(request)
    if blocked:
        return blocked
    # Multi-org: every list on the routing admin page is org-scoped (leak vector
    # 12) — rules, the project/workflow/language-set pickers, and the budgets. An
    # org-admin manages only their own org's routing. org_filter is a no-op off.
    su = security.session_user(request)
    rules = session.exec(
        access.org_filter(select(RoutingRule), RoutingRule, su).order_by(
            RoutingRule.priority, RoutingRule.id
        )
    ).all()
    projects = session.exec(
        access.org_filter(select(Project), Project, su).order_by(Project.name)
    ).all()
    # NOTE: the workflow / language-set pickers were removed from the form (QW-D) --
    # those rule targets are reserved for v2 and not consulted at run time yet, so the
    # page no longer queries Workflow / LanguageSet for dropdowns.
    proj_by_id = {p.id: p.name for p in projects}
    # Which rules carry an authored-but-inert reserved field (subject/workflow/language
    # set)? Computed HERE off the ONE kernel presence test (routing_svc.reserved_fields_set
    # -> _field_present) rather than re-derived in the template, so the web "inert" badge,
    # the CLI list/show flag and the refusal can never disagree -- including on a
    # whitespace-only match_subject, where a hand-rolled Jinja truthiness twin would
    # wrongly flag a rule the CLI (and the refusal) treat as clean (QW-D gate LOW-8).
    inert_rule_ids = {
        r.id
        for r in rules
        if routing_svc.reserved_fields_set(
            match_subject=r.match_subject,
            workflow_id=r.workflow_id,
            language_set_id=r.language_set_id,
        )
    }
    # Per-department budgets (chunk 4) with this month's spend for each.
    budget_rows = session.exec(
        access.org_filter(select(DepartmentBudget), DepartmentBudget, su).order_by(
            DepartmentBudget.department
        )
    ).all()
    budget_view = [
        {"row": b, "spent": budgets.spend_this_month(session, b.department, org_id=b.org_id)}
        for b in budget_rows
    ]
    # CV-2: per-PROJECT money budgets (beside the department token budgets), each with
    # its render-ready meter state (enforced $ + committed marker). Org-scoped like
    # every other list on this page.
    pbudget_rows = session.exec(
        access.org_filter(select(ProjectBudget), ProjectBudget, su).order_by(
            ProjectBudget.project_id
        )
    ).all()
    project_budget_view = [
        {
            "row": b,
            "project_name": proj_by_id.get(b.project_id, f"project {b.project_id}"),
            "state": project_budgets.budget_state(session, b.project_id),
        }
        for b in pbudget_rows
    ]
    return templates.TemplateResponse(
        request,
        "routing.html",
        {
            "request": request,
            "rules": rules,
            "projects": projects,
            "proj_by_id": proj_by_id,
            "inert_rule_ids": inert_rule_ids,
            "budgets": budget_view,
            "project_budgets": project_budget_view,
            # CV-5: the same global AI-spend meter Setup AI renders (render-ready
            # figure + fill/tick/pct percentages), so the money picture sits next to
            # the per-department token budgets it constrains. Read-only view of data
            # already on /api/budgets + the spend ledger — no new state.
            "spend": spend.meter_state(),
            "error": request.query_params.get("error"),
            "saved": request.query_params.get("saved"),
        },
    )


@router.post("/routing")
def routing_create(
    request: Request,
    match_ext: str = Form(""),
    match_subject: str = Form(""),
    department: str = Form(""),
    priority: str = Form("100"),
    project_id: str = Form(""),
    workflow_id: str = Form(""),
    language_set_id: str = Form(""),
    provider: str = Form(""),
    session: Session = Depends(get_session),
):
    fblock = feature_block(request, "routing")  # ffadmin Wave 4: flag first (layer 2)
    if fblock:
        return fblock
    me, blocked = _admin_block(request)
    if blocked:
        return blocked
    # Reserved-for-v2 fields (subject/workflow/language-set) are consulted by no reader
    # in v1 -- refuse them before storing dead config (QW-D trap), byte-identical to the
    # CLI + /api. Checked on the RAW form values BEFORE coercion (MED-2): a present,
    # non-empty reserved key refuses regardless of coercibility, so workflow_id="0"/
    # "-1"/"abc" cannot be swallowed by _int_or_none->None and silently create a rule.
    # Empty/absent form fields are allowed (a form posts empty strings). The form no
    # longer renders the workflow/language-set controls; this is the direct-POST defense.
    dead = routing_svc.reserved_field_error(
        match_subject=match_subject, workflow_id=workflow_id, language_set_id=language_set_id
    )
    if dead:
        return redirect_with_error("/routing", dead, param="error")
    wf_id = _int_or_none(workflow_id)
    proj_id = _int_or_none(project_id)
    ls_id = _int_or_none(language_set_id)
    # Guardrail: reject a human-review workflow rule (API path) AND a rule naming a
    # nonexistent workflow/project/language-set (a bad id would otherwise dump a raw
    # FK IntegrityError as a 500 — #26). ORG-SCOPED (#26's LOW residual): a referent
    # in ANOTHER org behaves as nonexistent, matching the org this rule is stamped
    # with below, so a rule can never point cross-org. No-op with the gate off.
    my_org = _my_org_id(request)
    err = routing_svc.validate_rule(
        session, workflow_id=wf_id, project_id=proj_id, language_set_id=ls_id, org_id=my_org
    )
    if err:
        return redirect_with_error("/routing", err, param="error")
    # #586: a project the caller cannot see (another customer's) reads exactly like a missing
    # one: same message validate_rule gives for an id that does not exist.
    if proj_id is not None and _visibility_block(request, session, proj_id) is not None:
        return redirect_with_error("/routing", f"no such project: {proj_id}", param="error")
    # Stamp the creating admin's org so resolve() only applies this rule to the
    # SAME org's submissions (leak vector 12). No-op (NULL) with the gate off.
    try:
        routing_svc.create_rule(
            session,
            priority=priority,
            match_ext=match_ext,
            match_subject=match_subject,
            department=department,
            project_id=proj_id,
            workflow_id=wf_id,
            language_set_id=ls_id,
            provider=provider,
            actor=getattr(me, "email", "system"),
            ip=_ip(request),
            org_id=my_org,
        )
    except routing_svc.RoutingRuleError as e:  # fail-closed backstop, clean not a 500
        return redirect_with_error("/routing", str(e), param="error")
    return RedirectResponse("/routing?saved=1", status_code=303)


@router.post("/routing/{rid}/toggle")
def routing_toggle(rid: int, request: Request, session: Session = Depends(get_session)):
    fblock = feature_block(request, "routing")  # ffadmin Wave 4: flag first (layer 2)
    if fblock:
        return fblock
    me, blocked = _admin_block(request)
    if blocked:
        return blocked
    # Multi-org: a cross-org (or, since it isn't enumerable, missing) rid is 404 —
    # a scoped admin must not toggle another org's rule (leak vector 12). GATED so
    # the gate-off path keeps the historical "missing rid is a harmless no-op
    # redirect" behavior byte-identically.
    if access.multi_org_active():
        rule = session.get(RoutingRule, rid)
        if rule is None or not access.same_org(
            security.session_user(request), getattr(rule, "org_id", None)
        ):
            return _error_page(request, 404, "Not found.")
    routing_svc.toggle_rule(session, rid, actor=getattr(me, "email", "system"), ip=_ip(request))
    return RedirectResponse("/routing", status_code=303)


@router.post("/routing/{rid}/delete")
def routing_delete(rid: int, request: Request, session: Session = Depends(get_session)):
    fblock = feature_block(request, "routing")  # ffadmin Wave 4: flag first (layer 2)
    if fblock:
        return fblock
    me, blocked = _admin_block(request)
    if blocked:
        return blocked
    # Multi-org: a cross-org / missing rid is 404 (leak vector 12). GATED so the
    # gate-off "missing rid is a no-op redirect" behavior is byte-identical.
    if access.multi_org_active():
        rule = session.get(RoutingRule, rid)
        if rule is None or not access.same_org(
            security.session_user(request), getattr(rule, "org_id", None)
        ):
            return _error_page(request, 404, "Not found.")
    routing_svc.delete_rule(session, rid, actor=getattr(me, "email", "system"), ip=_ip(request))
    return RedirectResponse("/routing", status_code=303)


# --- per-department budgets (chunk 4) --------------------------------------
# ffadmin Wave 4: these /routing/budgets* routes are CORE and are DELIBERATELY NOT
# feature_block'd on "routing" -- department spend budgets are independent of content
# routing (plan Q4). CORE_WEB carves /routing/budgets out of the middleware's
# longest-match, so they stay reachable when the routing feature is switched off.
@router.post("/routing/budgets")
def budget_save(
    request: Request,
    department: str = Form(""),
    monthly_token_limit: str = Form("0"),
    session: Session = Depends(get_session),
):
    """Create or update one department's monthly token cap. One row per department:
    saving an existing department updates it in place (upsert). A limit <= 0 means
    unlimited (the row is kept but ignored by budgets.check)."""
    me, blocked = _admin_block(request)
    if blocked:
        return blocked
    dept = (department or "").strip()
    if not dept:
        return redirect_with_error("/routing", "Department is required.", param="error")
    # T4-03: a non-integer limit (a typo like "1O" for "10", or "notanumber")
    # used to be silently coerced to 0 == UNLIMITED by the bare except below —
    # a typo REMOVED the department's cap instead of erroring. Blank still means
    # 0/unlimited (the form's own default value, and the pre-existing behavior
    # for an omitted field); only genuinely non-numeric input is rejected, with
    # a clean redirect banner rather than a silently-accepted 0.
    raw_limit = (monthly_token_limit or "").strip()
    if not raw_limit:
        limit = 0
    else:
        try:
            limit = int(raw_limit)
        except ValueError:
            return redirect_with_error(
                "/routing",
                f"Monthly token limit must be a whole number (got {raw_limit!r}).",
                param="error",
            )
    # Stamp/scope to the acting admin's org so each org keeps its OWN row for a
    # shared department name (leak vector 12). No-op (NULL) with the gate off.
    try:
        budgets.upsert_budget(
            session,
            dept,
            limit,
            actor=getattr(me, "email", "system"),
            ip=_ip(request),
            org_id=_my_org_id(request),
        )
    except budgets.BudgetLimitError as e:
        # task_cca6bb89: a huge/malicious value used to reach a raw sqlite3
        # OverflowError -> 500. upsert_budget's shared bound check catches it
        # first; surface the same clean redirect-banner style as the
        # non-integer-input branch above instead of a crash.
        return redirect_with_error("/routing", str(e), param="error")
    return RedirectResponse("/routing?saved=1", status_code=303)


@router.post("/routing/budgets/{bid}/delete")
def budget_delete(bid: int, request: Request, session: Session = Depends(get_session)):
    me, blocked = _admin_block(request)
    if blocked:
        return blocked
    # Multi-org: a cross-org / missing bid is 404 (leak vector 12). GATED so the
    # gate-off "missing bid is a no-op redirect" behavior is byte-identical.
    if access.multi_org_active():
        row = session.get(DepartmentBudget, bid)
        if row is None or not access.same_org(
            security.session_user(request), getattr(row, "org_id", None)
        ):
            return _error_page(request, 404, "Not found.")
    budgets.delete_budget(session, bid, actor=getattr(me, "email", "system"), ip=_ip(request))
    return RedirectResponse("/routing", status_code=303)


# --- per-project money budgets (CV-2) ----------------------------------------
# Like the department budgets above, these are CORE and DELIBERATELY NOT feature_block'd
# on "routing" — a project spend cap is independent of content routing. Admin-only CRUD
# (matching department budgets). CORE_WEB carves /routing/project-budgets out of the
# middleware longest-match so they stay reachable with routing switched off.
@router.post("/routing/project-budgets")
def project_budget_save(
    request: Request,
    project_id: str = Form(""),
    monthly_usd_limit: str = Form("0"),
    session: Session = Depends(get_session),
):
    """Create or update one project's monthly USD cap (upsert; one row per project).
    A limit <= 0 means unlimited (the row is kept but ignored by project_check)."""
    me, blocked = _admin_block(request)
    if blocked:
        return blocked
    pid = _int_or_none(project_id)
    if pid is None:
        return redirect_with_error("/routing", "A project is required.", param="error")
    # Org-ring the referent: a project in ANOTHER org behaves as nonexistent (leak
    # vector 12), so an org-admin can only budget their own org's projects.
    proj = session.get(Project, pid)
    if proj is None or (
        access.multi_org_active()
        and not access.same_org(security.session_user(request), getattr(proj, "org_id", None))
    ):
        return redirect_with_error("/routing", "No such project.", param="error")
    # #586: a project the caller cannot see (another customer's, or outside their project
    # scope) is "no such project" too, exactly like a missing id.
    if _visibility_block(request, session, proj) is not None:
        return redirect_with_error("/routing", "No such project.", param="error")
    raw = (monthly_usd_limit or "").strip()
    if not raw:
        limit = 0.0
    else:
        try:
            limit = float(raw)
        except ValueError:
            return redirect_with_error(
                "/routing", f"Monthly USD limit must be a number (got {raw!r}).", param="error"
            )
    try:
        project_budgets.upsert_project_budget(
            session, pid, limit, actor=getattr(me, "email", "system"), ip=_ip(request)
        )
    except project_budgets.BudgetLimitError as e:
        return redirect_with_error("/routing", str(e), param="error")
    return RedirectResponse("/routing?saved=1", status_code=303)


@router.post("/routing/project-budgets/{bid}/delete")
def project_budget_delete(bid: int, request: Request, session: Session = Depends(get_session)):
    me, blocked = _admin_block(request)
    if blocked:
        return blocked
    # Multi-org: a cross-org / missing bid is 404 (leak vector 12). GATED so the
    # gate-off "missing bid is a no-op redirect" behavior is byte-identical.
    if access.multi_org_active():
        row = session.get(ProjectBudget, bid)
        if row is None or not access.same_org(
            security.session_user(request), getattr(row, "org_id", None)
        ):
            return _error_page(request, 404, "Not found.")
    project_budgets.delete_project_budget(
        session, bid, actor=getattr(me, "email", "system"), ip=_ip(request)
    )
    return RedirectResponse("/routing", status_code=303)
