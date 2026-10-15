"""Dashboard, reports, and help pages. (Liveness/readiness probes live in
app/web/health.py so the embedded profile can keep them without this WEB router.)

Documented in: docs/architecture/reporting-and-dashboard.md
"""

from pathlib import Path

from fastapi import APIRouter, Depends, Request
from fastapi.responses import FileResponse, RedirectResponse, Response
from sqlmodel import Session, select

from app import config
from app.db import get_session
from app.kernel import (
    access,
    audit,
    onboarding,
    security,
    settings,
)
from app.kernel import (
    reports as reports_service,
)
from app.models import (
    Customer,
    Document,
    FullTextTM,
    GlossaryEntry,
    LQAResult,
    Project,
    Segment,
    SegmentTM,
    StepRun,
    TargetLocale,
    WorkflowRun,
)
from app.web.deps import _actor_email, _error_page, _ip, feature_block, templates
from app.web.queries import (
    _chunked,
    _count_rows,
    _dashboard_data,
    _group_counts,
    _leverage_band_counts,
    _score_sum_count,
    _without_seeding,
)

router = APIRouter()

_FAVICON_PATH = Path(__file__).resolve().parent.parent / "static" / "favicon.svg"


# --- favicon (bug #12: the bare /favicon.ico path every browser probes 404'd -
# there was no route for it, only the <link rel="icon"> in base.html pointing at
# /static/favicon.svg). Serve the SAME svg here so the bare path resolves too;
# browsers accept an SVG body for a .ico request just fine. ---
@router.get("/favicon.ico")
def favicon():
    return FileResponse(_FAVICON_PATH, media_type="image/svg+xml")


# NOTE: the liveness/readiness probes (/health, /ready) moved to app/web/health.py
# (composable-core phase 0, Train 0a) so the embedded/headless profile can keep them
# mounted while dropping the rest of this WEB router. Their paths + handlers are
# unchanged; they are mounted in every mode via app.web.HEALTH_ROUTERS.


# --- dashboard ---
@router.get("/")
def dashboard(request: Request, session: Session = Depends(get_session)):
    me = security.session_user(request)
    visible = access.visible_project_ids(session, me)
    data = _dashboard_data(session, visible_ids=visible, user=me)
    # First-run onboarding checklist (SPEC-onboarding-tour Phase 1). The service is
    # the single authority for eligibility + per-user state; ?tour=1 (the topbar
    # re-summon control) forces the card even after dismissal. `onboarding` is None
    # when the card should not render (ineligible / dismissed / not forced).
    force_tour = request.query_params.get("tour") == "1"
    onboarding_ctx = onboarding.dashboard_panel(session, me, force=force_tour)
    return templates.TemplateResponse(
        request, "dashboard.html", {"request": request, "onboarding": onboarding_ctx, **data}
    )


# --- onboarding checklist dismiss (SPEC-onboarding-tour Phase 1) ---
@router.post("/onboarding/dismiss")
def onboarding_dismiss(request: Request, session: Session = Depends(get_session)):
    """ "Don't show this again" — stamp onboarding_dismissed_at so the checklist
    never auto-shows again (re-summon via ?tour=1 still overrides display). CSRF is
    enforced by the security middleware like every other mutating web POST. No-op
    for an ineligible / auth-off user; always lands back on the dashboard."""
    me = security.session_user(request)
    if onboarding.is_eligible(me):
        onboarding.dismiss(session, me)
        audit.log(actor=_actor_email(request), action="onboarding.dismiss", ip=_ip(request))
    return RedirectResponse("/", status_code=303)


# --- Reports ---
@router.get("/reports")
def reports(request: Request, session: Session = Depends(get_session)):
    """The reports page: the aggregate pipeline-health dashboard (unchanged) PLUS
    the R2-1 report-builder card. Submitting the builder GETs /reports/run."""
    blocked = feature_block(request, "reports")  # ffadmin Wave 4: flag first (layer 2)
    if blocked:
        return blocked
    return _reports_page(request, session)


def _dashboard_reports_data(request, session):
    visible = access.visible_project_ids(session, security.session_user(request))
    # #514: every figure below is a SQL COUNT / GROUP BY (chunked IN), never a load of every
    # Segment, TM, run, step or finding row just to len() or bucket it. Only the cheap id
    # columns of documents and runs are read, to scope the child tables.
    dq = select(Document.id)
    if visible is not None:
        dq = dq.where(Document.project_id.in_(visible))
    doc_ids = list(session.exec(dq).all())
    status = _without_seeding(
        session,
        doc_ids,
        _group_counts(session, Segment.status, in_col=Segment.document_id, ids=doc_ids),
    )
    seg_total = sum(status.values())
    # EVERY aggregate below is scoped by the same visible-project set as the
    # documents/segments queries — a restricted user must never see another
    # project's runs, LQA, locales, TM/glossary totals or provider usage.
    if visible is None:
        run_status = _group_counts(session, WorkflowRun.status)
        provider_usage = _group_counts(
            session,
            StepRun.provider_used,
            StepRun.provider_used.is_not(None),
            StepRun.provider_used != "",
        )
        sev = _group_counts(session, LQAResult.severity)
        lqa_sum, lqa_n = _score_sum_count(session)
        lang = _group_counts(session, TargetLocale.locale)
        tm_total = _count_rows(session, SegmentTM)
        ft_total = _count_rows(session, FullTextTM)
        gloss_total = _count_rows(session, GlossaryEntry, GlossaryEntry.glossary_id == None)  # noqa: E711
    else:
        run_ids: list = []
        for batch in _chunked(doc_ids):
            run_ids += session.exec(
                select(WorkflowRun.id).where(WorkflowRun.document_id.in_(batch))
            ).all()
        run_status = _group_counts(
            session, WorkflowRun.status, in_col=WorkflowRun.document_id, ids=doc_ids
        )
        provider_usage = _group_counts(
            session,
            StepRun.provider_used,
            StepRun.provider_used.is_not(None),
            StepRun.provider_used != "",
            in_col=StepRun.workflow_run_id,
            ids=run_ids,
        )
        sev = _group_counts(session, LQAResult.severity, in_col=LQAResult.document_id, ids=doc_ids)
        lqa_sum, lqa_n = _score_sum_count(session, doc_ids)
        lang = _group_counts(
            session, TargetLocale.locale, in_col=TargetLocale.project_id, ids=visible
        )
        tm_total = _count_rows(session, SegmentTM, in_col=SegmentTM.project_id, ids=visible)
        ft_total = _count_rows(session, FullTextTM, in_col=FullTextTM.project_id, ids=visible)
        gloss_total = _count_rows(
            session, GlossaryEntry, in_col=GlossaryEntry.project_id, ids=visible
        )
    # WAVE-2 DISCLOSURE (SPEC-wave2 §3.1.5, gate M4): this histogram + avg read persisted
    # LQAResult rows across BOTH write regimes. Train A's locale-filtered pre-score delete
    # now stops an ad-hoc re-score from destroying workflow-step APPEND history, so that
    # history survives re-scores and these dashboard numbers drift UP relative to the
    # pre-Wave-2 behaviour (where a re-score wiped the whole document's findings). This is
    # correct — the numbers were previously under-counting — but it is a visible change.
    # The user-facing caveat belongs to Train B's caveat work (Errata); not reworked here.
    avg_lqa = round(lqa_sum / lqa_n) if lqa_n else None
    # TM-leverage rollup (viz quick-win): the same four-way bucketing, as a SQL CASE over the
    # same visible-project documents.
    tm_leverage = _leverage_band_counts(session, doc_ids)
    return {
        "status": status,
        "provider_usage": provider_usage,
        "sev": sev,
        "avg_lqa": avg_lqa,
        "lang": lang,
        "run_status": run_status,
        "tm_total": tm_total,
        "ft_total": ft_total,
        "gloss_total": gloss_total,
        "segments": seg_total,
        "tm_leverage": tm_leverage,
    }


# --- Report builder (R2-1) ---
def _report_picker_ctx(request, session):
    """Customer + project options for the report builder selects, scoped EXACTLY
    like the aggregate dashboard above (visible_project_ids bakes in org +
    membership + R2-3 customer confinement). A customer-bound user only ever sees
    their own customer's projects and their own customer - never a foreign one."""
    me = security.session_user(request)
    visible = access.visible_project_ids(session, me)  # None = unrestricted
    pstmt = select(Project).order_by(Project.name)
    if visible is not None:
        if not visible:
            return {"customers": [], "projects": []}
        pstmt = pstmt.where(Project.id.in_(visible))
    projects = session.exec(pstmt).all()
    cust_ids = {p.customer_id for p in projects if p.customer_id is not None}
    customers = []
    if cust_ids:
        customers = sorted(
            session.exec(select(Customer).where(Customer.id.in_(cust_ids))).all(),
            key=lambda c: c.name,
        )
    # Ruling (fix 3, orchestrator-decided): report filters cover retained data, so
    # an archived customer stays LISTED here (unlike the user-binding picker,
    # which filters archived out for NEW associations) — reports are retrospective
    # over data that outlives archival. Just label it so it reads clearly in the
    # dropdown; no other behavior change.
    return {
        "customers": [
            {"id": c.id, "name": c.name + (" (archived)" if c.archived_at is not None else "")}
            for c in customers
        ],
        "projects": [{"id": p.id, "name": p.name} for p in projects],
    }


def _build_query(request) -> reports_service.ReportQuery:
    """A ReportQuery from the request query params (clean defaults on garbage). Dates
    are validated here so a bad date is a friendly message, not a 500."""
    qp = request.query_params

    def _int(name):
        raw = (qp.get(name) or "").strip()
        try:
            return int(raw) if raw else None
        except (TypeError, ValueError):
            return None

    return reports_service.ReportQuery(
        kind=(qp.get("kind") or "cost"),
        group_by=(qp.get("group_by") or "none"),
        date_from=reports_service.parse_date(qp.get("from")),
        date_to=reports_service.parse_date(qp.get("to")),
        customer_id=_int("customer_id"),
        project_id=_int("project_id"),
        provider=(qp.get("provider") or "").strip() or None,
    ).normalized()


@router.get("/reports/run")
def reports_run(request: Request, session: Session = Depends(get_session)):
    """Run a report and render the table; with ?format=csv, download the CSV. The
    THREE scoping layers ride on the resolved user (visible_project_ids), so a
    restricted / customer-bound user can never aggregate outside their scope.
    Read/report is a viewer-allowed action within scope - no write role required."""
    blocked = feature_block(request, "reports")  # ffadmin Wave 4: flag first (layer 2)
    if blocked:
        return blocked
    me = security.session_user(request)
    try:
        q = _build_query(request)
    except reports_service.UnknownReportKind as exc:
        # #25: an unknown ?kind= is a clean form error, not a 500 (nor a silent
        # coercion to 'cost'). Distinct message from the date error below.
        return _reports_page(request, session, error=str(exc), error_field="report-kind")
    except ValueError:
        return _reports_page(
            request,
            session,
            error="A date is not valid. Enter dates as YYYY-MM-DD, then run the report again.",
            error_field="report-from",
        )
    # CV-4: the cost report kind is manager+ (spend data). Refuse an under-ranked ident
    # with a real 403 BEFORE any audit row is written (refused attempts leave no audit
    # trail — project convention). tokens/volume stay viewer-readable. Auth-off web
    # resolves `me` to the SYSTEM admin, which clears can_manage, so single-tenant is
    # unaffected.
    if q.kind == "cost" and not reports_service.can_see_cost(me):
        return _error_page(request, 403, "Cost reports require the manager role (or higher).")
    # SPEC-wave2 §4.4: the `actor` axis (qa/postedit) ranks named people by output — it is
    # manager+ data. Refuse an under-ranked ident LOUDLY (never silently rebucket to none)
    # BEFORE any audit row is written, mirroring the cost 403 above. tokens/volume and the
    # other qa/postedit axes stay viewer-readable. Auth-off web resolves `me` to the SYSTEM
    # admin, which clears can_manage.
    if q.group_by == "actor" and not reports_service.can_see_actor_axis(me):
        return _error_page(request, 403, "The actor axis requires the manager role (or higher).")
    # user-derived scoping (org + membership + customer) via visible_project_ids;
    # org_id stays None here (the web user's org confinement rides on `user`).
    report = reports_service.run_report(session, q, user=me, org_id=None)
    fmt = (request.query_params.get("format") or "").strip().lower()
    if fmt == "csv":
        audit.log(
            actor=_actor_email(request),
            action="report.export",
            target=f"kind={q.kind} group_by={q.group_by}",
            detail=f"rows={len(report['rows'])}",
            ip=_ip(request),
        )
        csv_text = reports_service.to_csv(report)
        fname = reports_service.csv_filename(q.kind, q.group_by)
        return Response(
            content=csv_text,
            media_type="text/csv; charset=utf-8",
            headers={"Content-Disposition": f'attachment; filename="{fname}"'},
        )
    audit.log(
        actor=_actor_email(request),
        action="report.run",
        target=f"kind={q.kind} group_by={q.group_by}",
        detail=f"rows={len(report['rows'])}",
        ip=_ip(request),
    )
    return _reports_page(request, session, report=report, q=q)


def _reports_page(request, session, *, report=None, q=None, error=None, error_field=None):
    """Render reports.html - the aggregate dashboard + the builder card (+ optional
    results table). Reuses the dashboard aggregate the plain /reports route builds."""
    data = _dashboard_reports_data(request, session)
    picker = _report_picker_ctx(request, session)
    # CV-4: hide the cost option in the builder for an ident that may not run it, so the
    # form never offers a kind that would 403 on submit (the server still enforces the
    # gate in reports_run — this is the cosmetic mirror, the deps._role_block pattern).
    me = security.session_user(request)
    can_cost = reports_service.can_see_cost(me)
    # SPEC-wave2 §4.4: hide the actor option for an ident that may not run it (the server
    # still 403s a crafted ?group_by=actor). Per-kind axis support drives the JS filter in
    # the template so an unsupported (kind, axis) pair is never offered.
    can_actor = reports_service.can_see_actor_axis(me)
    supported_group_bys = {k: sorted(v) for k, v in reports_service._SUPPORTED_GROUP_BYS.items()}
    ctx = {
        "request": request,
        **data,
        **picker,
        "kinds": reports_service.kinds_catalog(),
        "can_cost": can_cost,
        "can_actor": can_actor,
        "supported_group_bys": supported_group_bys,
        "group_bys": list(reports_service.GROUP_BYS),
        "providers": config.PROVIDERS,
        "report": report,
        "totals": reports_service.totals_row(report) if report else None,
        "q": q,
        # a11y T7: the refusal renders in the page error summary (base.html reads err),
        # linked to and marking the form field it is about.
        "err": error,
        "err_field": error_field,
    }
    return templates.TemplateResponse(request, "reports.html", ctx)


# --- Help & API docs ---
def api_endpoint_count(app) -> int:
    """How many distinct METHOD + `/api` routes this build serves.

    DERIVED, never typed: the Part 2 reference used to state its own size as a
    hardcoded "93", which was wrong by 65 by the time anyone read it again. Counted
    off the router table at render time, the sentence cannot drift from the app --
    and tests/verify_help.py holds the TABLE to the same set, so the number and the
    rows it introduces can never disagree either."""
    from app.web._routes import real_api_routes

    return len(real_api_routes(app))


@router.get("/help")
def help_page(request: Request):
    """The docs page, now ALSO the index of capability cards (SPEC-ask-draft
    section 6).

    DEVIATION, recorded deliberately: the spec says "GET /help - index of cards".
    /help already existed as the long-form Features + API reference and is
    DEEP-LINKED from a dozen places (`/help#connectors-jira-push`,
    `/help#api-companion`, `/help#auth`, `/help#segmentation`, ...) plus the topbar
    and the nav. Replacing its body would break every one of those anchors. So the
    card index is rendered AT THE TOP of /help (it IS the index of cards, first
    thing you see) and each entry links to its `/help/<module>` card page; the
    legacy long-form stays below, unchanged."""
    from app.kernel import assist

    return templates.TemplateResponse(
        request,
        "help.html",
        {
            "request": request,
            "external_base_url": settings.external_base_url(request),
            "providers": config.PROVIDERS,
            "cards": assist.all_cards(),
            "api_endpoint_count": api_endpoint_count(request.app),
            # a11y T10: the "How to report a problem" contact (email, https link or blank).
            "a11y_contact": settings.accessibility_contact(),
            # Derived from the connector specs, never hand-listed: a rebuild that un-archives a
            # kind drops it from the help page's "Legacy / archived" sentence by itself.
            "archived_connector_names": _archived_connector_names(),
        },
    )


def _archived_connector_names() -> list[str]:
    from app.kernel import connectors as _creg

    return sorted(s.name for s in _creg.archived_specs())


# #595 global search: /search and /search/suggest live in app/web/search.py and ride this
# router, so the pinned router assembly in app/modules.py ROUTER_GROUPS is unchanged.
from app.web.search import router as _search_router  # noqa: E402

router.include_router(_search_router)

# Project automation: /projects/{id}/automation (and its POSTs) live in
# app/web/project_automation.py and ride this router for the same reason.
from app.web.project_automation import router as _project_automation_router  # noqa: E402

router.include_router(_project_automation_router)

# #617 item 7 MT formality: POST /formality (the page forms) lives in app/web/formality_routes.py
# and rides this router for the same reason.
from app.web.formality_routes import router as _formality_router  # noqa: E402

router.include_router(_formality_router)

# Project automation Part B: /settings/notifications (page + forms) lives in
# app/web/notify_routes.py and rides this router for the same reason.
from app.web.notify_routes import router as _notify_router  # noqa: E402

router.include_router(_notify_router)
